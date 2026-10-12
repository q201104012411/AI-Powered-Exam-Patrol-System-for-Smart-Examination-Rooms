# -*- coding: utf-8 -*-
"""
考场监控 · 逐人行为识别（密集教室版）

针对 40 人同框大教室优化：
  · MobileNet-SSD DNN 行人检测优先（若存在模型文件）
  · HOG 密集模式 + 软 NMS + 自适应阈值
  · 背景差分 / Haar 人头/人脸辅助检测
  · 匈牙利匹配（scipy 可用时）/ 最近邻回退
  · detect_stride 跳帧检测 + 匀速预测
  · max_input_dim 长边缩放检测
  · 密度感知、小人框、小标签

行为 -> 03 告警码映射见 gui.py / terminal_gui.py。
生产环境建议换成 YOLO-pose / 边缘 AI 节点返回的骨骼点，本文件是原型。
"""
import os
import math
from collections import deque
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    from scipy.optimize import linear_sum_assignment
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - scipy 为可选依赖
    _HAS_SCIPY = False

# 行为常量（与 02 gui / 03 terminal 的映射保持一致）
ACT_NORMAL = "正常姿态"
ACT_SIT = "坐下"
ACT_STAND = "起立/站立"
ACT_TURN = "大幅度偏转"      # 侧转/回头：框宽高比突变
ACT_LEAN = "前倾/趴桌"
ACT_LOOK = "东张西望"        # 质心左右高频摆动
ACT_DOWN = "长时间低头"
ACT_LEFT = "靠左"
ACT_RIGHT = "靠右"
ACT_CROSS = "触碰红线"       # 每跨越一次警戒线计一次
ACT_GAZE = "眼神游移"        # 双眼位置不对称/视线漂移（人脸+人眼级联）
ACT_SLEEPY = "闭眼/趴睡"     # 检测到人脸但长时间看不到眼睛
ACT_HEADTURN = "转脸偏头"    # 人脸明显偏向人框一侧

# 需要上报 03 的异常行为
ABNORMAL_ACTIONS = (ACT_CROSS, ACT_STAND, ACT_TURN, ACT_LEAN, ACT_LOOK, ACT_DOWN,
                    ACT_GAZE, ACT_SLEEPY, ACT_HEADTURN)

# MobileNet-SSD 中 "person" 的类别编号
_MOBILENET_PERSON_CLASS = 15

# 模块级提示标志，避免每个实例都重复打印
_DNN_WARNED = False
_MOG_WARNED = False

# 三色语义（BGR）：绿=正常，橙=异常行为，红=越界/贴边
COLOR_OK = (0, 200, 0)
COLOR_WARN = (0, 165, 255)
COLOR_BAD = (0, 0, 255)
COLOR_FACE = (255, 255, 0)
COLOR_LINE = (0, 165, 255)


@lru_cache(maxsize=32)
def _load_font(size=13):
    """加载支持中文的字体（带缓存，避免每帧都读字体文件）"""
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simhei.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simsun.ttc"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# 中文标签位图缓存：同一串文字只渲染一次，之后只做小范围 alpha 混合。
# 这是渲染提速的关键——原先每帧每人都要走一遍 PIL 全图转换 + 文字排版。
_TEXT_PATCH_CACHE = {}
_TEXT_PATCH_MAX = 512


def text_patch(text, color_bgr, font_size=13):
    """把一段文字渲染成 RGBA 小图并缓存。返回 (patch_rgba, w, h)；失败返回 None。"""
    key = (text, tuple(color_bgr), int(font_size))
    cached = _TEXT_PATCH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        font = _load_font(font_size)
        # 先量尺寸
        tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        box = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
        tw = max(1, box[2] - box[0] + 2)
        th = max(1, box[3] - box[1] + 2)
        img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        # PIL 用 RGB 顺序，调用方给的是 BGR
        rgb = (int(color_bgr[2]), int(color_bgr[1]), int(color_bgr[0]), 255)
        ImageDraw.Draw(img).text((-box[0] + 1, -box[1] + 1), text, font=font, fill=rgb)
        patch = np.array(img, dtype=np.uint8)
    except Exception:
        return None
    if len(_TEXT_PATCH_CACHE) >= _TEXT_PATCH_MAX:
        _TEXT_PATCH_CACHE.clear()
    _TEXT_PATCH_CACHE[key] = patch
    return patch


def clear_text_cache():
    """清空标签位图缓存（切换字号/颜色方案后可调用）"""
    _TEXT_PATCH_CACHE.clear()


def blend_patch(frame_bgr, patch, x, y):
    """把 RGBA 文字小图以 alpha 混合贴到 BGR 帧上（越界自动裁剪）。"""
    if patch is None or frame_bgr is None:
        return
    fh, fw = frame_bgr.shape[:2]
    ph, pw = patch.shape[:2]
    x = int(x)
    y = int(y)
    if x >= fw or y >= fh:
        return
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(fw, x + pw)
    y1 = min(fh, y + ph)
    if x1 <= x0 or y1 <= y0:
        return
    sub = patch[y0 - y:y1 - y, x0 - x:x1 - x]
    alpha = sub[:, :, 3:4].astype(np.float32) / 255.0
    if not alpha.any():
        return
    region = frame_bgr[y0:y1, x0:x1].astype(np.float32)
    # sub 是 RGBA(RGB 序)，转成 BGR 再混合
    region = region * (1.0 - alpha) + sub[:, :, :3][:, :, ::-1].astype(np.float32) * alpha
    frame_bgr[y0:y1, x0:x1] = region.astype(np.uint8)


def draw_label(frame_bgr, text, color_bgr, x, y, font_size=13):
    """在 (x, y) 左上角贴一段中文标签（带黑底提高可读性）"""
    patch = text_patch(text, color_bgr, font_size)
    if patch is None:
        return
    ph, pw = patch.shape[:2]
    blend_patch(frame_bgr, patch, x, y)
    del ph, pw


def _centroid(rect):
    x, y, w, h = rect
    return x + w // 2, y + h // 2


def _iou(a, b):
    """两个矩形之间的 IoU"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1 = max(ax, bx)
    yi1 = max(ay, by)
    xi2 = min(ax + aw, bx + bw)
    yi2 = min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _soft_nms(rects, scores=None, iou_thresh=0.5, sigma=0.5, score_thresh=0.001):
    """基于高斯惩罚的贪婪软 NMS，返回保留下来的矩形"""
    if not rects:
        return []
    N = len(rects)
    rects_arr = np.array(rects, dtype=float)
    scores_arr = np.array(scores if scores is not None else [1.0] * N, dtype=float)
    indices = np.argsort(scores_arr)[::-1]
    keep = []
    while len(indices) > 0:
        current = indices[0]
        keep.append(current)
        current_box = rects_arr[current]
        rest = indices[1:]
        if len(rest) == 0:
            break
        ious = np.array([_iou(tuple(current_box), tuple(rects_arr[i])) for i in rest])
        scores_arr[rest] *= np.exp(-(ious * ious) / sigma)
        indices = rest[scores_arr[rest] > score_thresh]
    return [tuple(int(v) for v in rects_arr[i]) for i in keep]


def _density_level(n_persons, frame_area):
    """根据每 10 万像素的人数估算密度等级（40 人 1080P 教室视为 high）"""
    density = n_persons * 100000.0 / max(1, frame_area)
    if density < 0.5:
        return "low"
    elif density < 1.5:
        return "medium"
    return "high"


class _Track(object):
    """一个人的跟踪状态"""
    __slots__ = ("pid", "rect", "missed", "centroids", "heights", "aspects",
                 "prev_side", "crosses", "cross_epochs",
                 "face_cache", "face_age", "action_hist",
                 "vx", "vy", "last_seen_frame")

    def __init__(self, pid, rect, line_y):
        self.pid = pid
        self.rect = rect
        self.missed = 0
        self.centroids = deque(maxlen=20)   # 最近 20 帧质心，用于东张西望判定
        self.heights = deque(maxlen=20)     # 最近 20 帧框高，用于低头判定
        self.aspects = deque(maxlen=8)      # 最近 8 帧宽高比，用于偏转/前倾确认
        self.action_hist = deque(maxlen=6)  # 最近 6 次行为推断，用于稳定确认
        self.prev_side = self._side(rect, line_y)
        self.crosses = 0                    # 累计跨线次数
        self.cross_epochs = deque(maxlen=10)
        self.face_cache = None              # 最近一次人脸/人眼分析结果
        self.face_age = 99                  # 距上次人脸分析的帧数
        self.vx = 0.0                       # 水平速度（像素/帧）
        self.vy = 0.0                       # 垂直速度（像素/帧）
        self.last_seen_frame = 0            # 上次被检测命中的帧号

    @staticmethod
    def _side(rect, line_y):
        """框底中心在警戒线上方(-1)还是下方(+1)"""
        return 1 if rect[1] + rect[3] > line_y else -1

    def predict(self):
        """按当前速度预测下一帧位置"""
        x, y, w, h = self.rect
        return (int(round(x + self.vx)), int(round(y + self.vy)), w, h)

    def update_motion(self, new_rect):
        """更新位置并平滑估计速度"""
        cx1, cy1 = _centroid(self.rect)
        cx2, cy2 = _centroid(new_rect)
        self.vx = self.vx * 0.7 + (cx2 - cx1) * 0.3
        self.vy = self.vy * 0.7 + (cy2 - cy1) * 0.3
        self.rect = new_rect


class PersonTracker(object):
    """多源检测 + 匈牙利/最近邻跟踪 + 密度感知 + 人脸微表情分析"""

    def __init__(self, margin_ratio=0.02, max_dist=90, max_missed=12,
                 max_input_dim=640, detect_stride=1, dense_mode=True,
                 enable_face_dense=False, dnn_threshold=0.5,
                 boundary_ratio=0.92, min_box_w=16, min_box_h=32,
                 max_aspect=1.8, confirm_frames=3):
        if not hasattr(cv2, "HOGDescriptor"):
            raise RuntimeError(
                "当前 OpenCV 未包含 HOGDescriptor，无法运行 AI 框选识别。\n"
                "请运行「项目树\\All\\安装环境.bat」自动修复。")
        if not hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector"):
            raise RuntimeError("当前 OpenCV 缺少 HOG 默认行人检测器。")
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

        self.margin_ratio = margin_ratio          # 收紧框：默认 2% 边距
        self.max_dist = max_dist                  # 匹配最大距离（像素）
        self.max_missed = max_missed              # 连续丢失多少帧后删除轨迹
        self.max_input_dim = max_input_dim        # 检测用长边上限
        self.detect_stride = max(1, int(detect_stride))  # 每 N 帧做一次完整检测
        self.dense_mode = dense_mode              # 是否启用 HOG 密集参数
        self.enable_face_dense = enable_face_dense  # 密集场景仍强制做人脸分析
        self.dnn_threshold = dnn_threshold        # DNN 置信度阈值

        # 越界判定：警戒线默认放在画面 92% 高度处。
        # 原默认 50%（画面中线）会把"坐在中后排"的考生全部判成越界，
        # 实测 17 人有 16 人报越界，属于明显误判。
        self.boundary_ratio = float(boundary_ratio)
        self.min_box_w = int(min_box_w)           # 最小框宽（像素）
        self.min_box_h = int(min_box_h)           # 最小框高（像素）
        self.max_aspect = float(max_aspect)       # 框宽/高超过此值视为误检（太扁）
        self.confirm_frames = max(1, int(confirm_frames))  # 行为需连续确认的帧数

        self._tracks = {}                         # pid -> _Track
        self._next_pid = 1
        self._frame_no = 0
        self._density_level = "low"
        self._count_hist = deque(maxlen=5)        # 人数平滑（取中位数，避免跳变）
        self._stable_count = 0
        self._model_dir = os.path.dirname(os.path.abspath(__file__))

        # Haar 级联（OpenCV 自带）
        self._face_cascade = None
        self._eye_cascade = None
        self._head_cascade = None
        self._init_cascades()

        # DNN MobileNet-SSD
        self._net = None
        self._init_dnn()

        # 背景差分
        self._bg_subtractor = None
        self._init_bg_subtractor()

    # ---------------- 初始化 ----------------
    def _init_cascades(self):
        try:
            base = cv2.data.haarcascades
            self._face_cascade = cv2.CascadeClassifier(
                base + "haarcascade_frontalface_default.xml")
            self._eye_cascade = cv2.CascadeClassifier(
                base + "haarcascade_eye.xml")
            # 用 frontalface 在上半画面辅助找头部/人脸，并推断人身框
            self._head_cascade = cv2.CascadeClassifier(
                base + "haarcascade_frontalface_default.xml")
        except Exception:
            self._face_cascade = None
            self._eye_cascade = None
            self._head_cascade = None

    def _init_dnn(self):
        global _DNN_WARNED
        prototxt = os.path.join(self._model_dir, "MobileNetSSD_deploy.prototxt")
        caffemodel = os.path.join(self._model_dir, "MobileNetSSD_deploy.caffemodel")
        if os.path.exists(prototxt) and os.path.exists(caffemodel):
            try:
                self._net = cv2.dnn.readNetFromCaffe(prototxt, caffemodel)
            except Exception as e:
                print("[person_detect] DNN 初始化失败，回退 HOG：%s" % e)
                self._net = None
        else:
            self._net = None
            if not _DNN_WARNED:
                print("[person_detect] 未找到 MobileNet-SSD 模型文件（"
                      "MobileNetSSD_deploy.prototxt / .caffemodel），使用 HOG 模式。")
                _DNN_WARNED = True

    def _init_bg_subtractor(self):
        global _MOG_WARNED
        try:
            self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
                history=120, varThreshold=36, detectShadows=False)
        except Exception:
            self._bg_subtractor = None
            if not _MOG_WARNED:
                print("[person_detect] 背景差分器初始化失败，将仅依赖检测器。")
                _MOG_WARNED = True

    # ---------------- 辅助 ----------------
    def _scale_frame(self, frame):
        """若长边超过 max_input_dim 则等比缩小，返回（缩放图，缩放系数）"""
        h, w = frame.shape[:2]
        long_edge = max(h, w)
        if long_edge <= self.max_input_dim:
            return frame, 1.0
        scale = self.max_input_dim / float(long_edge)
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        return cv2.resize(frame, (new_w, new_h)), scale

    @staticmethod
    def _rect_to_orig(rect, scale):
        """把缩放图上的矩形映射回原图坐标"""
        return tuple(int(round(v / scale)) for v in rect)

    def _shrink_rect(self, rect, fw, fh):
        """按 margin_ratio 收紧矩形，并保证最小 24×24"""
        x, y, w, h = rect
        mw = max(0, int(w * self.margin_ratio))
        mh = max(0, int(h * self.margin_ratio))
        x = max(0, x + mw)
        y = max(0, y + mh)
        w = max(24, min(fw - x, w - mw * 2))
        h = max(24, min(fh - y, h - mh * 2))
        if x + w > fw:
            w = fw - x
        if y + h > fh:
            h = fh - y
        return (x, y, w, h)

    # ---------------- 检测 ----------------
    def _detect_dnn(self, frame):
        """MobileNet-SSD 行人检测"""
        if self._net is None:
            return []
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, 0.007843, (300, 300), 127.5)
        self._net.setInput(blob)
        detections = self._net.forward()
        rects, scores = [], []
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence < self.dnn_threshold:
                continue
            class_id = int(detections[0, 0, i, 1])
            if class_id != _MOBILENET_PERSON_CLASS:
                continue
            box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
            x1, y1, x2, y2 = box.astype(int)
            rw, rh = x2 - x1, y2 - y1
            if rw < 8 or rh < 8:
                continue
            rects.append((x1, y1, rw, rh))
            scores.append(float(confidence))
        return _soft_nms(rects, scores, iou_thresh=0.35, sigma=0.5)

    def _detect_hog(self, frame, dense):
        """HOG 行人检测；dense=True 时使用更密的 winStride/scale"""
        if dense:
            win_stride = (4, 4)
            padding = (16, 16)
            scale = 1.03
            score_thresh = 0.15
        else:
            win_stride = (8, 8)
            padding = (12, 12)
            scale = 1.05
            score_thresh = 0.05
        try:
            found, weights = self.hog.detectMultiScale(
                frame, winStride=win_stride, padding=padding, scale=scale)
        except Exception:
            return []
        if len(found) == 0:
            return []
        weights = np.asarray(weights).ravel()
        if weights.size == 0:
            return []
        w_min, w_max = weights.min(), weights.max()
        rng = w_max - w_min
        if rng > 0:
            scores = (weights - w_min) / rng
        else:
            scores = np.ones_like(weights)
        rects = [tuple(int(v) for v in r) for r, s in zip(found, scores) if s > score_thresh]
        scores = [float(s) for s in scores if s > score_thresh]
        return _soft_nms(rects, scores, iou_thresh=0.45, sigma=0.5)

    def _detect_bg_rois(self, frame):
        """背景差分补充候选框"""
        if self._bg_subtractor is None:
            return []
        fg = self._bg_subtractor.apply(frame)
        if self._frame_no < 10:
            return []
        _, fg = cv2.threshold(fg, 128, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rois = []
        h, w = frame.shape[:2]
        for c in cnts:
            bx, by, bw, bh = cv2.boundingRect(c)
            area = bw * bh
            if area < 400 or area > h * w * 0.25:
                continue
            ar = bw / float(bh) if bh > 0 else 0
            if ar < 0.15 or ar > 1.3:
                continue
            rois.append((bx, by, bw, bh))
        return rois

    def _detect_heads(self, frame):
        """在上半画面用 Haar frontalface 找头并推断人身框"""
        if self._head_cascade is None:
            return []
        h, w = frame.shape[:2]
        roi = frame[:int(h * 0.65), :]
        if roi.size == 0:
            return []
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        heads = self._head_cascade.detectMultiScale(
            gray, scaleFactor=1.05, minNeighbors=2,
            minSize=(16, 16), maxSize=(w // 4, h // 3))
        out = []
        for hx, hy, hw, hh in heads:
            body_w = int(hw * 2.2)
            body_h = int(hh * 3.5)
            bx = max(0, hx + hw // 2 - body_w // 2)
            by = max(0, hy)
            bw = min(w - bx, body_w)
            bh = min(h - by, body_h)
            if bw >= 24 and bh >= 24:
                out.append((bx, by, bw, bh))
        return out

    def _merge_sources(self, primary, aux_list, iou_thresh=0.35):
        """把主检测框与辅助来源框合并，去除重复"""
        merged = list(primary)
        for src in aux_list:
            for r in src:
                if all(_iou(r, m) < iou_thresh for m in merged):
                    merged.append(r)
        return merged

    def _sanitize_boxes(self, rects, fw, fh):
        """过滤"不像人"的框，减少虚警导致的人数虚高与假行为。

        典型误检：桌子/椅子/墙面被检成很扁的宽框（宽高比 > max_aspect），
        或过小的碎片框。这类框一旦进入跟踪，还会把"大幅度偏转/前倾"刷成
        持续假警报，所以必须在源头掐掉。
        """
        out = []
        for x, y, w, h in rects:
            if w < self.min_box_w or h < self.min_box_h:
                continue
            if h <= 0:
                continue
            if (w / float(h)) > self.max_aspect:
                continue
            # 完全在画面外的框丢弃
            if x + w <= 0 or y + h <= 0 or x >= fw or y >= fh:
                continue
            out.append((int(x), int(y), int(w), int(h)))
        return out

    def _detect(self, frame):
        """综合检测：DNN -> HOG -> 背景/人头辅助，结果映射回原图并收紧"""
        small, scale = self._scale_frame(frame)
        orig_h, orig_w = frame.shape[:2]

        # 密度感知：当前已经高密度时强制启用密集参数
        dense = self.dense_mode or self._density_level in ("medium", "high")

        rects = []
        # 1) 优先 DNN
        dnn_rects = self._detect_dnn(small)
        rects.extend(dnn_rects)

        # 2) HOG 回退 / 补充
        if not dnn_rects or self._density_level == "high":
            hog_rects = self._detect_hog(small, dense)
            rects = self._merge_sources(rects, [hog_rects], iou_thresh=0.35)

        # 3) 教室人数偏少或高密度时，用背景差分 + 人头检测辅助
        if len(rects) < 8 or self._density_level == "high":
            bg_rois = self._detect_bg_rois(small)
            head_rects = self._detect_heads(small)
            rects = self._merge_sources(rects, [bg_rois, head_rects], iou_thresh=0.30)

        # 4) 映射回原图、收紧、剔除不合理框、最终 NMS
        orig_rects = [self._rect_to_orig(r, scale) for r in rects]
        shrunk = [self._shrink_rect(r, orig_w, orig_h) for r in orig_rects]
        cleaned = self._sanitize_boxes(shrunk, orig_w, orig_h)
        return _soft_nms(cleaned, iou_thresh=0.35, sigma=0.5)

    # ---------------- 跟踪 ----------------
    def _greedy_assignment(self, cost):
        """scipy 不可用时使用的贪婪最近邻匹配"""
        rows, cols = cost.shape
        row_ind, col_ind = [], []
        remaining_rows = list(range(rows))
        remaining_cols = list(range(cols))
        while remaining_rows and remaining_cols:
            best = 1e9
            bi, bj = -1, -1
            for i in remaining_rows:
                for j in remaining_cols:
                    if cost[i, j] < best:
                        best = cost[i, j]
                        bi, bj = i, j
            if best >= 1e6:
                break
            row_ind.append(bi)
            col_ind.append(bj)
            remaining_rows.remove(bi)
            remaining_cols.remove(bj)
        return np.array(row_ind, dtype=int), np.array(col_ind, dtype=int)

    def _match_and_update(self, rects, line_y):
        """用匈牙利或贪婪最近邻把检测框匹配到已有轨迹"""
        cents = [_centroid(r) for r in rects]
        tracks = list(self._tracks.items())
        n_tr, n_dt = len(tracks), len(rects)

        assigned_tr = set()
        assigned_dt = set()

        if n_tr > 0 and n_dt > 0:
            cost = np.zeros((n_tr, n_dt), dtype=float)
            for i, (_, tr) in enumerate(tracks):
                tcx, tcy = _centroid(tr.rect)
                for j, (cx, cy) in enumerate(cents):
                    d = math.hypot(cx - tcx, cy - tcy)
                    cost[i, j] = d if d <= self.max_dist else 1e6

            if _HAS_SCIPY:
                row_ind, col_ind = linear_sum_assignment(cost)
            else:
                row_ind, col_ind = self._greedy_assignment(cost)

            for i, j in zip(row_ind, col_ind):
                if cost[i, j] >= 1e6:
                    continue
                pid, tr = tracks[i]
                tr.update_motion(rects[j])
                tr.missed = 0
                tr.last_seen_frame = self._frame_no
                cx, cy = cents[j]
                tr.centroids.append((cx, cy))
                tr.heights.append(tr.rect[3])
                tr.aspects.append(tr.rect[2] / float(tr.rect[3])
                                  if tr.rect[3] > 0 else 0.0)
                tr.face_age += 1
                side = tr._side(tr.rect, line_y)
                if side != tr.prev_side:
                    tr.crosses += 1
                    tr.cross_epochs.append(cv2.getTickCount())
                    tr.prev_side = side
                assigned_tr.add(i)
                assigned_dt.add(j)

        # 未匹配到的轨迹标记丢失
        for i, (pid, tr) in enumerate(tracks):
            if i in assigned_tr:
                continue
            tr.missed += 1
            tr.face_age += 1
            if tr.missed > self.max_missed:
                del self._tracks[pid]

        # 未匹配到的检测框开新轨迹
        for j, rect in enumerate(rects):
            if j in assigned_dt:
                continue
            tr = _Track(self._next_pid, rect, line_y)
            tr.last_seen_frame = self._frame_no
            self._tracks[self._next_pid] = tr
            self._next_pid += 1

    def _predict_tracks(self, line_y):
        """非检测帧：用匀速预测更新已有轨迹"""
        for pid, tr in list(self._tracks.items()):
            tr.rect = tr.predict()
            tr.missed += 1
            tr.face_age += 1
            cx, cy = _centroid(tr.rect)
            tr.centroids.append((cx, cy))
            tr.heights.append(tr.rect[3])
            tr.aspects.append(tr.rect[2] / float(tr.rect[3])
                              if tr.rect[3] > 0 else 0.0)
            side = tr._side(tr.rect, line_y)
            if side != tr.prev_side:
                tr.crosses += 1
                tr.cross_epochs.append(cv2.getTickCount())
                tr.prev_side = side
            if tr.missed > self.max_missed:
                del self._tracks[pid]

    def update(self, frame, boundary_ratio=None):
        """每帧调用：检测 -> 匹配/预测 -> 更新跨线计数，返回 tracks 列表"""
        self._frame_no += 1
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)

        run_detect = ((self._frame_no - 1) % self.detect_stride) == 0
        if run_detect:
            rects = self._detect(frame)
            self._match_and_update(rects, line_y)
        else:
            self._predict_tracks(line_y)

        fw = frame.shape[1]
        self._density_level = _density_level(len(self._tracks), fh * fw)
        return self._tracks

    # ---------------- 人脸 / 眼神（微表情近似） ----------------
    def _face_analyze(self, frame, track, total_persons):
        """
        在人框上半部分做人脸+人眼检测（每 6 帧一次，轨迹级缓存）。
        密集场景默认跳过，除非 enable_face_dense=True 或 total_persons <= 20。
        返回 {"face": (fx,fy,fw,fh)|None, "eyes": n, "face_dx": 偏头量}
        """
        if self._face_cascade is None or self._eye_cascade is None:
            return None
        if track.face_cache is not None and track.face_age < 6:
            return track.face_cache
        if total_persons > 20 and not self.enable_face_dense:
            return None

        x, y, w, h = track.rect
        head_h = max(24, int(h * 0.45))          # 头部区域：人框上 45%
        roi = frame[y:y + head_h, x:x + w]
        if roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(gray, 1.1, 4,
                                                    minSize=(24, 24))
        if len(faces) == 0:
            track.face_cache = {"face": None, "eyes": 0, "face_dx": 0.0}
        else:
            fx, fy, fw2, fh2 = max(faces, key=lambda r: r[2] * r[3])
            fgray = gray[fy:fy + fh2, fx:fx + fw2]
            eyes = self._eye_cascade.detectMultiScale(fgray, 1.1, 3,
                                                      minSize=(10, 10))
            face_cx = fx + fw2 // 2
            face_dx = (face_cx - w / 2.0) / max(1.0, float(w))
            track.face_cache = {"face": (x + fx, y + fy, fw2, fh2),
                                "eyes": int(len(eyes)),
                                "face_dx": face_dx}
        track.face_age = 0
        return track.face_cache

    # ---------------- 行为分析 ----------------
    def _confirmed(self, track, action, hit, need=None):
        """把"这一帧像异常"升级成"连续若干次分析都像异常"才算数。

        原实现只看单帧宽高比，HOG 框一抖动就报"大幅度偏转/前倾"，
        实测 17 个人能刷出 17 条异常。现在要求最近 confirm_frames 次
        分析里至少有 confirm_frames-1 次命中，才真正采纳。
        """
        need = need or max(2, self.confirm_frames - 1)
        hist = list(track.action_hist)
        hist.append(1 if hit else 0)
        track.action_hist.append(1 if hit else 0)
        return sum(hist[-self.confirm_frames:]) >= need

    def analyze(self, frame, track, boundary_ratio=None, edge_margin=20):
        """
        对一条轨迹做行为推断。
        返回 (action, bad, behaviors)
          action    主标签（画在框上）
          bad       是否越界（红框）
          behaviors 需要上报的异常行为列表
        """
        fh, fw = frame.shape[:2]
        x, y, w, h = track.rect
        bottom = y + h
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)
        # 越界 = 人已越过警戒线（默认画面 92% 高度，即贴近镜头/走出考区）
        bad = bottom > line_y

        behaviors = []
        action = ACT_NORMAL

        # 横向历史宽高比（中位数 + 命中次数），避免单帧抖动误报
        aspects = list(track.aspects)
        recent = aspects[-self.confirm_frames:] if aspects else []
        wide_hits = sum(1 for r in recent if r > 0.85)
        lean_hits = sum(1 for r in recent if r > 0.68)

        # 1) 触碰红线：跨线次数 >= 2 视为"多次触线"
        if track.crosses >= 2:
            behaviors.append(ACT_CROSS)
        elif bad:
            action = "越界"

        # 2) 大幅度偏转：连续多次宽高比 > 0.85（侧转/回头时人框明显变宽）
        if wide_hits >= max(2, self.confirm_frames - 1):
            action = ACT_TURN
            behaviors.append(ACT_TURN)
        # 3) 起立/站立：框很高且底部越过警戒线（起立才会越过 92% 线）
        elif h > fh * 0.55 and bad:
            action = ACT_STAND
            behaviors.append(ACT_STAND)
        # 4) 前倾/趴桌：连续多次宽高比 > 0.68 且高度在收缩
        elif lean_hits >= max(2, self.confirm_frames - 1) and len(track.heights) >= 4:
            hs = list(track.heights)
            if hs[-1] <= hs[0] * 1.02:
                action = ACT_LEAN
                behaviors.append(ACT_LEAN)
        # 5) 坐下：框较矮、在画面下半
        elif h < fh * 0.45 and y > fh * 0.35:
            action = ACT_SIT

        # 6) 东张西望：近 20 帧质心横向摆动大、纵向基本不动
        if len(track.centroids) >= 12:
            xs = np.array([c[0] for c in track.centroids], dtype=float)
            ys = np.array([c[1] for c in track.centroids], dtype=float)
            if xs.std() > max(18.0, w * 0.25) and ys.std() < max(14.0, h * 0.12):
                behaviors.append(ACT_LOOK)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_LOOK

        # 7) 长时间低头：框高持续变矮 + 质心持续下移
        if len(track.heights) >= 15:
            hs = list(track.heights)
            if hs[-1] < hs[0] * 0.82 and track.centroids[-1][1] > track.centroids[0][1] + 10:
                behaviors.append(ACT_DOWN)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_DOWN

        # 8) 人脸/眼神微表情（有级联时才跑，失败不影响其它判定）
        try:
            fa = self._face_analyze(frame, track, len(self._tracks))
        except Exception:
            fa = None
        if fa and fa.get("face") is not None:
            # 转脸偏头：人脸明显偏向人框一侧
            if abs(fa.get("face_dx", 0.0)) > 0.22:
                behaviors.append(ACT_HEADTURN)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_HEADTURN
            # 闭眼/趴睡：检测到人脸但看不到眼睛
            if fa.get("eyes", 0) == 0:
                behaviors.append(ACT_SLEEPY)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_SLEEPY
            # 眼神游移：只看到单眼（视线明显偏向一边）
            elif fa.get("eyes", 0) == 1:
                behaviors.append(ACT_GAZE)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_GAZE

        # 9) 贴边（只有真正贴到画面边缘才算，不再和"越界"混为一谈）
        if (x < edge_margin or y < edge_margin
                or x + w > fw - edge_margin or y + h > fh - edge_margin):
            if x < edge_margin or x + w > fw - edge_margin:
                bad = True
            if x < fw * 0.12:
                action = ACT_LEFT
            elif x + w > fw * 0.88:
                action = ACT_RIGHT

        # 去重保序
        seen, uniq = set(), []
        for b in behaviors:
            if b not in seen:
                seen.add(b)
                uniq.append(b)
        return action, bad, uniq

    # ---------------- 生命周期 ----------------
    def stop(self):
        """清空所有轨迹，停止/重置跟踪器"""
        self._tracks.clear()
        self._next_pid = 1
        self._frame_no = 0
        self._density_level = "low"
        self._count_hist.clear()
        self._stable_count = 0

    def start(self):
        """启动/复位跟踪器（与 stop 后状态一致即可继续运行）"""
        self.stop()

    def stable_count(self):
        """平滑后的人数（最近 5 次分析的中位数），避免数字跳来跳去"""
        return self._stable_count

    # ---------------- 绘制 ----------------
    def analyze_only(self, frame, boundary_ratio=None):
        """只做检测/跟踪/行为分析，**不绘制**。

        返回 (persons, violations, line_y)。persons[i]["rect"] 为像素坐标。
        把"分析"与"落笔"拆开，界面层就能按低频率跑分析、按高频率贴图，
        既省 CPU 又不会出现"框时有时无"的闪烁。
        """
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)
        tracks = self.update(frame, boundary_ratio=boundary_ratio)
        persons = []
        for pid, tr in sorted(tracks.items()):
            action, bad, behaviors = self.analyze(frame, tr,
                                                 boundary_ratio=boundary_ratio)
            persons.append({"pid": pid, "rect": tr.rect, "action": action,
                            "bad": bad, "crosses": tr.crosses,
                            "behaviors": behaviors,
                            "density_level": self._density_level})
        violations = sum(1 for p in persons if p["bad"])
        # 人数平滑：最近 5 次分析取中位数，界面读数不再跳变
        self._count_hist.append(len(persons))
        if self._count_hist:
            ordered = sorted(self._count_hist)
            self._stable_count = ordered[len(ordered) // 2]
        return persons, violations, line_y

    @staticmethod
    def person_color(person):
        """三色：红=越界/贴边，橙=异常行为未越界，绿=正常"""
        if person.get("bad"):
            return COLOR_BAD
        if person.get("behaviors"):
            return COLOR_WARN
        return COLOR_OK

    def draw_boxes(self, frame, persons, line_y=None, scale=1.0, offset=(0, 0),
                   font_size=13, draw_line=True):
        """把 analyze_only 的结果画到帧上（矩形用 cv2，中文标签走位图缓存）。

        scale/offset 用于把"分析分辨率"下的坐标映射到"显示分辨率"。
        """
        fh, fw = frame.shape[:2]
        if draw_line and line_y is not None:
            ly = int(line_y * scale + offset[1])
            if 0 <= ly < fh:
                cv2.line(frame, (0, ly), (fw, ly), COLOR_LINE, 1)
                draw_label(frame, "警戒线", COLOR_LINE,
                           max(2, offset[0] + 6), max(0, ly - 18), font_size)
        if not persons:
            return
        ox, oy = offset
        for p in persons:
            x, y, w, h = p["rect"]
            color = self.person_color(p)
            rx = int(x * scale + ox)
            ry = int(y * scale + oy)
            rw = max(2, int(w * scale))
            rh = max(2, int(h * scale))
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), color, 1)
            # 人脸小框（微表情辅助，青色）
            fa = p.get("face")
            if fa is not None:
                fx, fy, fw2, fh2 = fa
                cv2.rectangle(frame,
                              (int(fx * scale + ox), int(fy * scale + oy)),
                              (int((fx + fw2) * scale + ox),
                               int((fy + fh2) * scale + oy)), COLOR_FACE, 1)
            label = "#%d %s" % (p["pid"], p["action"])
            if p.get("crosses", 0) >= 2:
                label += " ×%d" % p["crosses"]
            ty = ry - 17 if ry > 20 else ry + rh + 2
            draw_label(frame, label, color, rx, ty, font_size)

    def draw(self, frame, boundary_ratio=0.5):
        """
        返回 (标记后的帧, 越界人数, 逐人信息列表)
        逐人信息: {"pid", "rect", "action", "bad", "crosses",
                   "behaviors", "density_level"}

        注意：本方法会在传入的 frame 上**原地**绘制并返回同一个数组，
        调用方若需要保留原图请先自行 copy。
        """
        persons, violations, line_y = self.analyze_only(
            frame, boundary_ratio=boundary_ratio)
        # 人脸框一并带出来，交给 draw_boxes 统一绘制
        for p in persons:
            tr = self._tracks.get(p["pid"])
            fa = getattr(tr, "face_cache", None) if tr is not None else None
            p["face"] = (fa or {}).get("face") if fa else None
        self.draw_boxes(frame, persons, line_y=line_y)
        return frame, violations, persons


# 兼容旧名：外部原来用 PersonDetector 的地方继续可用
class PersonDetector(PersonTracker):
    def detect(self, frame, **kw):
        return self._detect(frame)

    def is_out_of_bounds(self, frame, rect, boundary_ratio=0.5, edge_margin=20):
        fh, fw = frame.shape[:2]
        x, y, w, h = rect
        line_y = int(fh * boundary_ratio)
        if y + h > line_y:
            return True
        if x < edge_margin or y < edge_margin:
            return True
        if x + w > fw - edge_margin or y + h > fh - edge_margin:
            return True
        return False
