# -*- coding: utf-8 -*-
r"""
考场监控 · 逐人行为识别（密集教室版）

针对 40 人同框大教室优化：
  · 检测后端三级优先：YOLO-ONNX（若存在模型文件） > MobileNet-SSD DNN > HOG+SVM
  · HOG 密集模式 + 软 NMS + 自适应阈值
  · 背景差分 / Haar 人头/人脸辅助检测
  · 匈牙利匹配（scipy 可用时）/ 最近邻回退
  · detect_stride 跳帧检测 + 匀速预测
  · max_input_dim 长边缩放检测
  · 密度感知、小人框、小标签

【模型接入说明】
  本文件会自动在自身目录及 models\ 子目录下按下列顺序找模型：
    1. yolov8n-pose.onnx / yolov8s-pose.onnx ...   （人体框 + 17 骨骼点，推荐）
    2. yolov8n.onnx / yolov5n.onnx / *.onnx        （纯人体检测，通吃 YOLOv5/v8/v11 导出）
    3. MobileNetSSD_deploy.prototxt + .caffemodel  （老模型，零改动可用）
  找不到任何模型文件时静默回落到 HOG —— 开发机（无模型）与考场机（有模型）
  跑的是同一份代码，无需改界面、无需改配置。

  权重文件需自行获取（本仓库不携带权重），放置位置见《使用说明.md》。
  任何后端都跑不通时，行为识别仍按原有 HOG 逻辑工作，不会崩。

行为 -> 03 告警码映射见 gui.py / terminal_gui.py。
"""
import os
import glob
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
_ORT_WARNED = False

# 模型目录名（与 person_detect.py 同级）
MODEL_SUBDIR = "models"

# 三色语义（BGR）：绿=正常，橙=异常行为，红=越界/贴边
COLOR_OK = (0, 200, 0)
COLOR_WARN = (0, 165, 255)
COLOR_BAD = (0, 0, 255)
COLOR_FACE = (255, 255, 0)
COLOR_LINE = (0, 165, 255)
COLOR_POSE = (255, 200, 0)      # 骨骼点绘制颜色（青色）

# YOLO ONNX 导出常见输入边长
_YOLO_DEFAULT_IMGSZ = 640


# =====================================================================
#  推理后端选择（CPU / OpenCL / ONNX Runtime GPU）
# =====================================================================
def _ort_providers():
    """返回本机 onnxruntime 可用且真正带 GPU 的 provider 列表（保序）。"""
    try:
        import onnxruntime as ort
    except Exception:
        return [], []
    try:
        avail = ort.get_available_providers()
    except Exception:
        return [], []
    want = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider",
                        "TensorrtExecutionProvider", "ROCMExecutionProvider")
            if p in avail]
    return want, avail


def detect_backends(prefer_gpu=True):
    """探测本机可用于 DNN 推理的后端。

    返回 {"opencv_cuda": bool, "opencv_opencl": bool, "ort_gpu": [...],
          "ort_providers": [...], "chosen": "cpu"|"opencl"|"cuda"|"ort:XXX"}
    纯探测、不抛异常，任何一项失败都降级而不是报错。
    """
    info = {"opencv_cuda": False, "opencv_opencl": False,
            "ort_gpu": [], "ort_providers": [], "chosen": "cpu"}
    # OpenCV 自身是否编译了 CUDA
    try:
        info["opencv_cuda"] = bool(cv2.cuda.getCudaEnabledDeviceCount() > 0)
    except Exception:
        info["opencv_cuda"] = False
    # OpenCL 是否可用
    try:
        info["opencv_opencl"] = bool(cv2.ocl.haveOpenCL())
    except Exception:
        info["opencv_opencl"] = False

    info["ort_gpu"], info["ort_providers"] = _ort_providers()

    if not prefer_gpu:
        info["chosen"] = "cpu"
    elif info["opencv_cuda"]:
        info["chosen"] = "cuda"
    elif info["ort_gpu"]:
        info["chosen"] = "ort:%s" % info["ort_gpu"][0]
    elif info["opencv_opencl"]:
        # 注意：OpenCL 走的是本机默认设备（笔记本上通常是核显），
        # 且 OpenCV 并非所有算子都有 OpenCL 实现，收益不保证。
        info["chosen"] = "opencl"
    else:
        info["chosen"] = "cpu"
    return info


def describe_backends(prefer_gpu=True):
    """把 detect_backends 的结果转成一行中文说明，便于日志/自检输出。"""
    b = detect_backends(prefer_gpu=prefer_gpu)
    parts = []
    parts.append("CUDA=%s" % ("有" if b["opencv_cuda"] else "无"))
    parts.append("OpenCL=%s" % ("有" if b["opencv_opencl"] else "无"))
    if b["ort_providers"]:
        parts.append("ORT=%s" % ",".join(b["ort_providers"]))
    else:
        parts.append("ORT=未安装")
    return "选用 %s（%s）" % (b["chosen"], " / ".join(parts))


def _apply_net_backend(net, prefer_gpu=True, allow_opencl=False):
    """给 cv2.dnn.Net 设置优选后端，返回实际使用的后端名。

    allow_opencl 默认 False：实测 DNN/传统算子在核显 OpenCL 上并不稳定提速，
    只有显式要求时才开，避免"以为上了 GPU 实际更慢"。
    """
    try:
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    except Exception:
        return "cpu"
    if prefer_gpu and detect_backends(prefer_gpu=True)["opencv_cuda"]:
        try:
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA_FP16)
            return "cuda"
        except Exception:
            pass
    if allow_opencl:
        try:
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_OPENCL)
            return "opencl"
        except Exception:
            pass
    return "cpu"


# =====================================================================
#  模型发现
# =====================================================================
def _search_dirs(model_dir=None):
    """模型搜索目录：显式指定 > 模块同级 > 模块同级 models\\"""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    if model_dir:
        dirs.append(model_dir)
    dirs.append(here)
    dirs.append(os.path.join(here, MODEL_SUBDIR))
    seen, out = set(), []
    for d in dirs:
        try:
            key = os.path.abspath(d).lower()
        except Exception:
            continue
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def find_model(model_dir=None, model_kind=None):
    """按优先级找模型文件。

    model_kind: None/"auto" / "yolo" / "caffe" —— 指定优先族（找不到仍会回落）
    返回 (kind, path, extra)：
      kind = "onnx-yolo"   path = .onnx 路径，extra = None
      kind = "caffe-ssd"   path = .prototxt 路径，extra = .caffemodel 路径
      kind = None          没找到任何模型
    """
    kind = (model_kind or "auto").lower()
    if kind not in ("auto", "yolo", "caffe"):
        kind = "auto"
    onnx_files, prototxts = [], []
    for d in _search_dirs(model_dir):
        onnx_files.extend(glob.glob(os.path.join(d, "*.onnx")))
        prototxts.extend(glob.glob(os.path.join(d, "*.prototxt")))

    # YOLO-pose 优先于普通检测模型（能拿到骨骼点）
    def _onnx_rank(p):
        n = os.path.basename(p).lower()
        if "pose" in n and n.startswith("yolo"):
            return 0
        if n.startswith("yolo"):
            return 1
        if "pose" in n:
            return 2
        return 3

    onnx_files.sort(key=lambda p: (_onnx_rank(p), os.path.basename(p).lower()))

    caffe_pick = None
    for pt in sorted(prototxts):
        base = os.path.splitext(pt)[0]
        for ext in (".caffemodel", ".caffemodel.bin"):
            if os.path.exists(base + ext):
                caffe_pick = (pt, base + ext)
                break
        if caffe_pick:
            break

    order = ["caffe", "onnx"] if kind == "caffe" else ["onnx", "caffe"]
    for which in order:
        if which == "onnx" and onnx_files:
            return "onnx-yolo", onnx_files[0], None
        if which == "caffe" and caffe_pick:
            return "caffe-ssd", caffe_pick[0], caffe_pick[1]
    return None, None, None


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
                 "face_cache", "face_age", "action_hist", "hits",
                 "vx", "vy", "last_seen_frame", "score")

    def __init__(self, pid, rect, line_y):
        self.pid = pid
        self.rect = rect
        self.missed = 0
        self.hits = 0                       # 累计被检测命中的次数（确认用）
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
        self.score = 0.0                    # 检测器置信度（HOG 时为归一化分数）

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
    """多源检测 + 匈牙利/最近邻跟踪 + 密度感知 + 人脸微表情分析

    检测后端自动选择：YOLO-ONNX > MobileNet-SSD Caffe > HOG+SVM。
    自检/界面可调 backend_name() 查看当前实际使用的后端。
    """

    def __init__(self, margin_ratio=0.02, max_dist=90, max_missed=12,
                 max_input_dim=640, detect_stride=1, dense_mode=True,
                 enable_face_dense=False, dnn_threshold=0.5,
                 boundary_ratio=0.92, min_box_w=16, min_box_h=32,
                 max_aspect=1.8, confirm_frames=3,
                 match_dist_ratio=0.12, min_hits=2,
                 model_path=None, model_kind="auto", prefer_gpu=True,
                 allow_opencl=False, yolo_imgsz=None, yolo_conf=None,
                 use_onnxruntime=False):
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
        # 匹配半径按画面宽度比例自适应（密集场景下固定 90px 会导致 ID 互抢）
        self.match_dist_ratio = float(match_dist_ratio)
        # 最少连续命中次数：只有命中的轨迹才算"确认的人"，
        # 单帧噪声（反光/桌椅误检）不会计入人数
        self.min_hits = max(1, int(min_hits))
        self._last_frame_w = 0

        # ---- 模型/后端参数 ----
        self.model_path = model_path              # 显式指定模型文件（None=自动查找）
        self.model_kind = (model_kind or "auto").lower()
        self.prefer_gpu = bool(prefer_gpu)
        self.allow_opencl = bool(allow_opencl)
        self.use_onnxruntime = bool(use_onnxruntime)
        self.yolo_imgsz = int(yolo_imgsz) if yolo_imgsz else None
        # YOLO 的置信度阈值与 MobileNet-SSD 分开：YOLO 召回高，阈值可略高
        self.yolo_conf = float(yolo_conf) if yolo_conf is not None else 0.30
        self.yolo_iou = 0.45
        self._ort_sess = None
        self._ort_input = None

        self._tracks = {}                         # pid -> _Track
        self._next_pid = 1
        self._frame_no = 0
        self._density_level = "low"
        self._count_hist = deque(maxlen=5)        # 人数平滑（取中位数，避免跳变）
        self._stable_count = 0
        self._model_dir = os.path.dirname(os.path.abspath(__file__))
        self._last_pose = None
        self._last_scores = None

        # 检测后端状态（供 backend_name() / 自检读取）
        self._backend_kind = "hog"                # hog / caffe-ssd / onnx-yolo
        self._backend_file = ""
        self._backend_target = "cpu"
        self._pose_enabled = False
        self._net_input_size = (300, 300)

        # Haar 级联（OpenCV 自带）
        self._face_cascade = None
        self._eye_cascade = None
        self._head_cascade = None
        self._init_cascades()

        # 检测网络（YOLO-ONNX 优先，其次 MobileNet-SSD；都没有则 None -> HOG）
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

    def _open_onnxruntime_gpu(self, path):
        """尝试用 onnxruntime 的 GPU provider 加载模型；不行返回 None。

        这条路径只在显式 use_onnxruntime=True 时启用：cv2.dnn 在无 CUDA 编译的
        OpenCV 上拿不到独显，而 onnxruntime 换包（onnxruntime-gpu）即可用上。
        """
        global _ORT_WARNED
        gpu, _ = _ort_providers()
        if not gpu:
            return None
        try:
            import onnxruntime as ort
            sess = ort.InferenceSession(path, providers=gpu)
            inps = sess.get_inputs()
            if not inps:
                return None
            self._ort_input = inps[0].name
            return sess
        except Exception as e:
            if not _ORT_WARNED:
                print("[person_detect] onnxruntime GPU 加载失败，回落 cv2.dnn：%s" % e)
                _ORT_WARNED = True
            return None

    @staticmethod
    def _onnx_input_dim(path, default=_YOLO_DEFAULT_IMGSZ):
        """读 ONNX 元信息拿输入边长（H 或 W，取较大者）；读不出用默认值。"""
        try:
            import onnx
            m = onnx.load(path, load_external_data=False)
            dims = m.graph.input[0].type.tensor_type.shape.dim
            vals = [d.dim_value if d.dim_value > 0 else 0 for d in dims]
            if len(vals) == 4:
                hw = [v for v in vals[2:4] if v > 0]
                if hw:
                    return int(max(hw))
        except Exception:
            pass
        return int(default)

    def _init_dnn(self):
        """按 YOLO-ONNX > MobileNet-SSD > HOG 的顺序装载检测后端。"""
        global _DNN_WARNED

        # --- 1) 显式指定了模型文件 ---
        kind, path, extra = None, None, None
        if self.model_path:
            if not os.path.exists(self.model_path):
                print("[person_detect] 指定的模型文件不存在：%s" % self.model_path)
            else:
                low = self.model_path.lower()
                if low.endswith(".onnx"):
                    kind, path = "onnx-yolo", self.model_path
                elif low.endswith(".prototxt"):
                    base = os.path.splitext(self.model_path)[0]
                    for ext in (".caffemodel", ".caffemodel.bin"):
                        if os.path.exists(base + ext):
                            kind, path, extra = "caffe-ssd", self.model_path, base + ext
                            break
                elif low.endswith(".caffemodel"):
                    base = os.path.splitext(self.model_path)[0]
                    if os.path.exists(base + ".prototxt"):
                        kind, path, extra = "caffe-ssd", base + ".prototxt", self.model_path

        # --- 2) 自动查找 ---
        if kind is None:
            kind, path, extra = find_model(model_dir=self._model_dir,
                                           model_kind=self.model_kind)

        # --- 3) 都没有 -> HOG ---
        if kind is None:
            self._net = None
            self._backend_kind = "hog"
            if not _DNN_WARNED:
                print("[person_detect] 未找到任何模型文件（*.onnx / *.prototxt+*.caffemodel），"
                      "使用 HOG+SVM 模式。把模型放进 %s 或 %s 即可自动启用。"
                      % (self._model_dir, os.path.join(self._model_dir, MODEL_SUBDIR)))
                _DNN_WARNED = True
            return

        # --- 4) 装载 ---
        try:
            if kind == "onnx-yolo":
                self._load_yolo(path)
            else:
                self._load_caffe(path, extra)
        except Exception as e:
            print("[person_detect] 模型装载失败，回退 HOG：%s" % e)
            self._net = None
            self._backend_kind = "hog"
            self._backend_file = ""

    def _load_yolo(self, path):
        """装载 YOLO ONNX（YOLOv5/v8/v11 导出的 detect 或 pose 头）。"""
        dim = self.yolo_imgsz or self._onnx_input_dim(path)
        self._net_input_size = (dim, dim)
        self._net = cv2.dnn.readNetFromONNX(path)
        self._backend_target = _apply_net_backend(
            self._net, prefer_gpu=self.prefer_gpu, allow_opencl=self.allow_opencl)
        if self.use_onnxruntime:
            self._ort_sess = self._open_onnxruntime_gpu(path)
            if self._ort_sess is not None:
                self._backend_target = "ort-gpu"
        self._backend_kind = "onnx-yolo"
        self._backend_file = path
        self._pose_enabled = "pose" in os.path.basename(path).lower()
        print("[person_detect] 已装载 YOLO 模型：%s（输入 %d，姿态=%s，后端=%s）"
              % (os.path.basename(path), self._net_input_size[0],
                 "是" if self._pose_enabled else "否", self._backend_target))

    def _load_caffe(self, prototxt, caffemodel):
        """装载 MobileNet-SSD（Caffe）。"""
        self._net = cv2.dnn.readNetFromCaffe(prototxt, caffemodel)
        self._backend_target = _apply_net_backend(
            self._net, prefer_gpu=self.prefer_gpu, allow_opencl=self.allow_opencl)
        self._net_input_size = (300, 300)
        self._backend_kind = "caffe-ssd"
        self._backend_file = prototxt
        self._pose_enabled = False
        print("[person_detect] 已装载 MobileNet-SSD：%s（后端=%s）"
              % (os.path.basename(prototxt), self._backend_target))

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

    # ---------------- 后端自述 ----------------
    def backend_name(self):
        """返回当前检测后端的可读名称，供自检/界面/报告展示。"""
        if self._backend_kind == "onnx-yolo":
            tag = "YOLO-pose" if self._pose_enabled else "YOLO"
            return "%s(ONNX)@%s" % (tag, self._backend_target)
        if self._backend_kind == "caffe-ssd":
            return "MobileNet-SSD@%s" % self._backend_target
        return "HOG+SVM(CPU)"

    def backend_info(self):
        """结构化后端信息，便于写入报告/自检输出。"""
        return {"kind": self._backend_kind,
                "file": self._backend_file,
                "target": self._backend_target,
                "pose": self._pose_enabled,
                "name": self.backend_name(),
                "input": self._net_input_size[0]}

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

    # ---------------- 检测：MobileNet-SSD ----------------
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

    # ---------------- 检测：YOLO ONNX ----------------
    @staticmethod
    def _postprocess_yolo(out, conf_thres, pose=False):
        """YOLO 头后处理 -> (boxes[N,4], scores[N], keypoints[N,17,3]|None)

        自动适配两种常见导出布局：
          · [1, C, N]  YOLOv8/v11 风格（C = 4+nc 或 4+3*17）
          · [1, N, C]  YOLOv5/tf 风格
        C 里前 4 个是 cx,cy,w,h（输入像素尺度）。
        """
        a = np.asarray(out)
        if a.ndim == 3 and a.shape[0] == 1:
            a = np.squeeze(a, axis=0)
        if a.ndim != 2:
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32), None
        if a.shape[0] < a.shape[1]:
            a = a.T                      # -> [N, C]
        if a.shape[1] < 5:
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32), None

        cx, cy, bw, bh = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        rest = a[:, 4:]
        nz = rest.shape[1]
        # 姿态模型：剩余通道 = 17*3（x,y,conf）
        is_pose = bool(pose) or nz == 51
        kp = None
        if is_pose and nz >= 51:
            kp = rest[:, :51].reshape(-1, 17, 3)
            scores = kp[:, :, 2].mean(axis=1) if kp.shape[0] else np.zeros(0, np.float32)
            scores = np.clip(scores, 0.0, 1.0)
        else:
            # 检测模型：单类取通道最大值；多类（含 80 类 COCO）取第 0 类 person
            if nz <= 2:
                scores = rest.max(axis=1)
            elif nz >= 80:
                scores = rest[:, 0]
            else:
                scores = rest.max(axis=1)

        keep = scores >= conf_thres
        if not np.any(keep):
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32), None
        cx, cy, bw, bh = cx[keep], cy[keep], bw[keep], bh[keep]
        sc = scores[keep].astype(np.float32)
        boxes = np.stack([cx - bw / 2.0, cy - bh / 2.0, bw, bh], axis=1).astype(np.float32)
        kps = kp[keep].astype(np.float32) if kp is not None else None
        return boxes, sc, kps

    def _detect_yolo(self, frame):
        """YOLO ONNX 行人检测；返回 (rects, scores, keypoints|None)。

        坐标已映射回传入 frame 的像素坐标系。
        """
        if self._net is None and self._ort_sess is None:
            return [], [], None
        fh, fw = frame.shape[:2]
        imgsz = self._net_input_size[0] or _YOLO_DEFAULT_IMGSZ
        # letterbox：保持长宽比，避免密集教室被拉伸后小目标变形
        scale = min(imgsz / float(fw), imgsz / float(fh))
        nw, nh = int(round(fw * scale)), int(round(fh * scale))
        resized = cv2.resize(frame, (nw, nh))
        canvas = np.full((imgsz, imgsz, 3), 114, dtype=np.uint8)
        dw, dh = (imgsz - nw) // 2, (imgsz - nh) // 2
        canvas[dh:dh + nh, dw:dw + nw] = resized

        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (imgsz, imgsz),
                                     swapRB=True, crop=False)
        if self._ort_sess is not None:
            outs = self._ort_sess.run(None, {self._ort_input: blob})
            out = outs[0]
        else:
            self._net.setInput(blob)
            out = self._net.forward()

        boxes, scores, kps = self._postprocess_yolo(
            out, self.yolo_conf, pose=self._pose_enabled)
        if boxes.shape[0] == 0:
            return [], [], None

        # 去掉 letterbox 边距并映射回原图
        boxes[:, 0] = (boxes[:, 0] - dw) / scale
        boxes[:, 1] = (boxes[:, 1] - dh) / scale
        boxes[:, 2] = boxes[:, 2] / scale
        boxes[:, 3] = boxes[:, 3] / scale
        rects, kept_scores, kept_kps = [], [], []
        for i in range(boxes.shape[0]):
            x, y, w, h = boxes[i]
            if w < 8 or h < 8:
                continue
            x1 = max(0, int(round(x)))
            y1 = max(0, int(round(y)))
            rw = int(round(w))
            rh = int(round(h))
            if x1 + rw > fw:
                rw = fw - x1
            if y1 + rh > fh:
                rh = fh - y1
            if rw < 8 or rh < 8:
                continue
            rects.append((x1, y1, rw, rh))
            kept_scores.append(float(scores[i]))
            if kps is not None:
                k = kps[i].copy()
                k[:, 0] = (k[:, 0] - dw) / scale
                k[:, 1] = (k[:, 1] - dh) / scale
                kept_kps.append(k)
        if not rects:
            return [], [], None
        return (rects, kept_scores,
                np.asarray(kept_kps, dtype=np.float32) if kept_kps else None)

    def _detect_hog(self, frame, dense):
        """HOG 行人检测。

        【为什么 dense 要按尺寸自适应（实测数据，2026-10）】
        dense=True 用 winStride=(4,4) 把滑窗步长缩到一半，实测在**小图上纯亏损**：
        耗时涨约 5 倍，检出数反而更少：

            416 长边   dense=True 45.9 ms 检出 4 个 ｜ dense=False 7.3 ms 检出 5 个
            640 长边   dense=True 134.0 ms 检出 4 个 ｜ dense=False 33.2 ms 检出 5 个

        原因是 HOG 自带的人体窗口是 128 px 高，而 416/640 长边下"坐着的人"只有
        70~108 px 高——框本来就小于检测窗口，把网格铺得更密也检不出更多，只是白烧 CPU。
        反过来在 ≥1000 px 的大图上网格太稀会漏掉大目标，dense 才有价值。

        因此调用方（_detect）按实际送检尺寸决定是否启用 dense，本方法只负责执行。
        """
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

    # dense 密集网格的启用门槛（输入长边像素）。
    # 【2026-10 实测】dense 把 winStride 从 (8,8) 缩到 (4,4)，滑窗数量约 4 倍：
    #   · 小图（416~640，生产实际用的尺寸）：耗时涨 5 倍而检出**不增反减**
    #     （416: 45.9ms/4人  vs 7.3ms/5人；640: 134ms/4人 vs 33.2ms/5人）——纯亏损
    #   · 大图（≥1000，例如 1280x720 原始帧、大分辨率单人画面）：网格太稀会漏掉
    #     大目标，此时 dense 才有价值
    # 所以不再一刀切，而是**按实际送检尺寸自适应**：小图自动走正常网格省下 5 倍算力，
    # 大图保留密集网格以免漏检。
    DENSE_MIN_EDGE = 1000

    def _detect(self, frame):
        """综合检测：模型（YOLO/SSD）-> HOG -> 背景/人头辅助，映射回原图并收紧"""
        small, scale = self._scale_frame(frame)
        orig_h, orig_w = frame.shape[:2]

        # 密集 HOG 网格：仅在小图以外的场景启用（见 DENSE_MIN_EDGE 注释）
        # 密度感知：原本"高密度就强制 dense"，但实测在 416~640 的小图上
        # dense 只烧 CPU 不提召回，因此改为"尺寸够大 且 高密度"才启用。
        big_enough = max(small.shape[:2]) >= self.DENSE_MIN_EDGE
        dense = big_enough and (self.dense_mode
                                or self._density_level in ("medium", "high"))

        use_net = (self._net is not None) or (self._ort_sess is not None)

        # 1) 有模型：模型结果即主检测，不再叠加 HOG，避免两套框互相污染；
        #    仅在"人数偏少"时用 Haar 人头补框（模型漏检场景）
        if use_net:
            rects, scores, kps = self._detect_net(small)
            orig_rects = [self._rect_to_orig(r, scale) for r in rects]
            shrunk = [self._shrink_rect(r, orig_w, orig_h) for r in orig_rects]
            shrunk = self._sanitize_boxes(shrunk, orig_w, orig_h)

            pose = None
            if kps is not None and len(kps):
                keep_idx = [i for i in range(len(shrunk))]
                pose = []
                for i in keep_idx:
                    if i >= len(kps):
                        break
                    kk = kps[i].copy()
                    kk[:, 0] = kk[:, 0] / scale
                    kk[:, 1] = kk[:, 1] / scale
                    pose.append(kk)
                if len(shrunk) < 8:
                    heads = [self._rect_to_orig(r, scale)
                             for r in self._detect_heads(small)]
                    for hrect in heads:
                        if all(_iou(hrect, m) < 0.3 for m in shrunk):
                            shrunk.append(hrect)
                            scores.append(self.yolo_conf)
                            pose.append(np.zeros((17, 3), np.float32))
            self._last_scores = scores
            self._last_pose = pose
            return shrunk

        # 2) 没有模型：原有 HOG + 辅助来源逻辑（完全保留）
        self._last_pose = None
        rects = []
        dnn_rects = []

        # 2.1) MobileNet-SSD（配置成 caffe 但装载失败时 _net 为 None，此支不生效）
        if self._net is not None:
            dnn_rects = self._detect_dnn(small)
            rects.extend(dnn_rects)

        # 2.2) HOG 回退 / 补充
        if not dnn_rects or self._density_level == "high":
            hog_rects = self._detect_hog(small, dense)
            rects = self._merge_sources(rects, [hog_rects], iou_thresh=0.35)

        # 2.3) 教室人数偏少或高密度时，用背景差分 + 人头检测辅助
        if len(rects) < 8 or self._density_level == "high":
            bg_rois = self._detect_bg_rois(small)
            head_rects = self._detect_heads(small)
            rects = self._merge_sources(rects, [bg_rois, head_rects], iou_thresh=0.30)

        # 2.4) 映射回原图、收紧、剔除不合理框、最终 NMS
        orig_rects = [self._rect_to_orig(r, scale) for r in rects]
        shrunk = [self._shrink_rect(r, orig_w, orig_h) for r in orig_rects]
        cleaned = self._sanitize_boxes(shrunk, orig_w, orig_h)
        self._last_scores = [1.0] * len(cleaned)
        return _soft_nms(cleaned, iou_thresh=0.35, sigma=0.5)

    def _detect_net(self, frame):
        """统一入口：按已装载的模型族调用对应检测器。"""
        if self._backend_kind == "onnx-yolo" and (self._net is not None
                                                  or self._ort_sess is not None):
            return self._detect_yolo(frame)
        if self._backend_kind == "caffe-ssd" and self._net is not None:
            rects = self._detect_dnn(frame)
            return rects, [1.0] * len(rects), None
        return [], [], None

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

    def _match_dist(self, frame_w):
        """匹配半径随画面宽度自适应。

        原实现固定 90px：在 416px 宽的 AI 分析帧上，90px 接近画面宽度的 22%，
        相邻两个考生会互相"抢"轨迹，表现为编号乱跳、人数忽多忽少。
        这里改成按画面宽度取比例（默认 12%），并受 max_dist 作为上限约束。
        """
        adaptive = int(frame_w * self.match_dist_ratio)
        return max(12, min(int(self.max_dist), adaptive))

    @staticmethod
    def _pair_cost(tr_rect, dt_rect, dist, max_dist):
        """轨迹-检测框的关联代价：质心距离 + IoU 共同决定。

        纯质心距离在密集场景里会把"隔壁同学"当成同一个人；加入 IoU 后，
        重叠度高的候选会被优先匹配，ID 切换明显减少。
        """
        if dist > max_dist:
            return 1e6
        iou = _iou(tr_rect, dt_rect)
        # 距离项归一化到 0~1，IoU 项取 1-iou；IoU 权重更大
        d_norm = dist / float(max_dist)
        return 0.35 * d_norm + 0.65 * (1.0 - iou)

    def _match_and_update(self, rects, line_y, frame_w=None, scores=None):
        """用匈牙利或贪婪最近邻把检测框匹配到已有轨迹"""
        cents = [_centroid(r) for r in rects]
        tracks = list(self._tracks.items())
        n_tr, n_dt = len(tracks), len(rects)
        max_dist = self._match_dist(frame_w or (self._last_frame_w or 640))

        assigned_tr = set()
        assigned_dt = set()

        if n_tr > 0 and n_dt > 0:
            cost = np.full((n_tr, n_dt), 1e6, dtype=float)
            for i, (_, tr) in enumerate(tracks):
                tcx, tcy = _centroid(tr.rect)
                for j, (cx, cy) in enumerate(cents):
                    d = math.hypot(cx - tcx, cy - tcy)
                    cost[i, j] = self._pair_cost(tr.rect, rects[j], d, max_dist)

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
                tr.hits += 1
                tr.last_seen_frame = self._frame_no
                if scores is not None and j < len(scores):
                    tr.score = float(scores[j])
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
            tr.hits = 1
            if scores is not None and j < len(scores):
                tr.score = float(scores[j])
            self._tracks[self._next_pid] = tr
            self._next_pid += 1

        self._merge_duplicate_tracks()

    @staticmethod
    def _same_person(a, b):
        """判断两个框是否"同一个人"。

        不能用纯 IoU：小框对偏移极其敏感——26×40 的框偏移 6px，IoU 只有 0.49，
        会被 0.55 的阈值判成两个人，于是同一人刷出两条轨迹、人数虚高、编号乱跳。
        改用"中心距 + 尺寸相近"判据，对小框同样稳健。
        """
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        if ah <= 0 or bh <= 0 or aw <= 0 or bw <= 0:
            return False
        acx, acy = ax + aw / 2.0, ay + ah / 2.0
        bcx, bcy = bx + bw / 2.0, by + bh / 2.0
        dist = math.hypot(acx - bcx, acy - bcy)
        # 中心距不超过较矮那个框的一半高度（且至少 10px）
        limit = max(10.0, 0.5 * min(ah, bh))
        if dist > limit:
            return False
        # 尺寸要相近（避免把大人和小孩/远近不同的人合并）
        rw = aw / float(bw)
        rh = ah / float(bh)
        return 0.6 <= rw <= 1.67 and 0.6 <= rh <= 1.67

    def _merge_duplicate_tracks(self, max_missed_for_merge=1):
        """合并"同一个人被检出两个框"造成的重复轨迹。

        HOG 在密集画面上经常对同一人给出两个略有偏移的框，于是同一个人占了
        两个编号、人数虚高。这里把判定为同一人的两条轨迹合并：保留命中次数
        多的那条，把跨线次数继承过来，删掉另一条。

        神经网络后端（YOLO/SSD）自带 NMS，重复框概率低；此时收紧合并条件，
        避免把"两个挨得很近的考生"错误并成一个（那会直接少算人数）。
        """
        if self._backend_kind != "hog":
            max_missed_for_merge = 0
        items = [(pid, tr) for pid, tr in self._tracks.items()
                 if tr.missed <= max_missed_for_merge and tr.hits >= self.min_hits]
        if len(items) < 2:
            return
        removed = set()
        for a in range(len(items)):
            pid_a, tr_a = items[a]
            if pid_a in removed:
                continue
            for b in range(a + 1, len(items)):
                pid_b, tr_b = items[b]
                if pid_b in removed:
                    continue
                if not self._same_person(tr_a.rect, tr_b.rect):
                    continue
                # 保留命中多、更早出现的轨迹
                if (tr_b.hits, -pid_b) > (tr_a.hits, -pid_a):
                    pid_a, tr_a, pid_b, tr_b = pid_b, tr_b, pid_a, tr_a
                tr_a.crosses = max(tr_a.crosses, tr_b.crosses)
                tr_a.hits = max(tr_a.hits, tr_b.hits)
                removed.add(pid_b)
        for pid in removed:
            self._tracks.pop(pid, None)

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

    def update(self, frame, boundary_ratio=None, with_pose=False):
        """每帧调用：检测 -> 匹配/预测 -> 更新跨线计数。

        with_pose=True 时返回 (tracks, pose_map)，pose_map: pid -> (17,3) 骨骼点；
        默认只返回 tracks，保持与既有调用方兼容。
        """
        self._frame_no += 1
        self._last_frame_w = frame.shape[1]
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)

        pose_map = {}
        run_detect = ((self._frame_no - 1) % self.detect_stride) == 0
        if run_detect:
            rects = self._detect(frame)
            scores = self._last_scores
            self._match_and_update(rects, line_y, frame_w=self._last_frame_w,
                                   scores=scores)
            # 姿态点归位到 pid：用骨架外接框与轨迹框的 IoU 做最近归属
            kps = self._last_pose
            if kps is not None and self._backend_kind == "onnx-yolo":
                for k in kps:
                    valid = k[k[:, 2] > 0.3]
                    if len(valid) == 0:
                        continue
                    kx1, ky1 = valid[:, 0].min(), valid[:, 1].min()
                    kx2, ky2 = valid[:, 0].max(), valid[:, 1].max()
                    kbox = (int(kx1), int(ky1), int(kx2 - kx1), int(ky2 - ky1))
                    best_pid, best_iou = None, 0.35
                    for pid, tr in self._tracks.items():
                        v = _iou(kbox, tr.rect)
                        if v > best_iou:
                            best_pid, best_iou = pid, v
                    if best_pid is not None:
                        pose_map[best_pid] = k
        else:
            self._predict_tracks(line_y)
            self._merge_duplicate_tracks()

        fw = frame.shape[1]
        self._density_level = _density_level(len(self._tracks), fh * fw)
        if with_pose:
            return self._tracks, pose_map
        return self._tracks

    def confirmed_tracks(self):
        """只返回"连续命中达到 min_hits"的轨迹。

        单次命中多半是噪声（反光、桌椅误检），把它们算进人数就是人数虚高的
        直接来源。界面/告警都应以这里的结果为准。
        """
        return {pid: tr for pid, tr in self._tracks.items()
                if tr.hits >= self.min_hits}

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

    @staticmethod
    def _pose_metrics(keypoints):
        """从 17 点 COCO 骨骼点算行为几何量（比框宽高比稳定得多）。

        COCO 顺序：
          0 鼻 1 左眼 2 右眼 3 左耳 4 右耳 5 左肩 6 右肩 7 左肘 8 右肘
          9 左腕 10 右腕 11 左髋 12 右髋 13 左膝 14 右膝 15 左踝 16 右踝
        返回 dict；肩部不可信时返回 None（调用方自动回退框逻辑）。
        """
        if keypoints is None:
            return None
        k = np.asarray(keypoints, dtype=np.float32)
        if k.shape != (17, 3):
            return None
        conf = k[:, 2]
        if int((conf[[0, 5, 6]] > 0.3).sum()) < 2:
            return None

        def pt(i):
            return k[i, :2] if conf[i] > 0.3 else None

        nose, lsh, rsh = pt(0), pt(5), pt(6)
        lhip, rhip = pt(11), pt(12)

        out = {}
        shoulder_w = 0.0
        if lsh is not None and rsh is not None:
            shoulder_w = float(np.linalg.norm(lsh - rsh))
        out["shoulder_w"] = shoulder_w
        if shoulder_w < 1.0:
            return out

        # 肩线倾斜角 —— 趴桌/严重歪斜时明显
        if lsh is not None and rsh is not None:
            out["shoulder_tilt_deg"] = float(abs(math.degrees(
                math.atan2(float(rsh[1] - lsh[1]), float(rsh[0] - lsh[0])))))
        # 头相对肩中心横向偏移（归一化到肩宽）—— 转脸/偏头
        if nose is not None and lsh is not None and rsh is not None:
            sh_cx = (lsh[0] + rsh[0]) / 2.0
            sh_cy = (lsh[1] + rsh[1]) / 2.0
            out["head_dx_ratio"] = float((nose[0] - sh_cx) / shoulder_w)
            # 头肩纵向距离 / 肩宽 —— 低头时鼻子贴近肩线
            out["nose_sh_dy_ratio"] = float((sh_cy - nose[1]) / shoulder_w)
        # 肩髋纵向距离 / 肩宽 —— 趴桌时躯干塌陷
        if lhip is not None and rhip is not None and lsh is not None and rsh is not None:
            hip_cy = (lhip[1] + rhip[1]) / 2.0
            sh_cy = (lsh[1] + rsh[1]) / 2.0
            out["torso_ratio"] = float((hip_cy - sh_cy) / shoulder_w)
        # 眼睛/耳朵置信度 —— 低头闭眼掉眼点，侧头掉耳点
        out["eye_conf"] = float((conf[1] + conf[2]) / 2.0)
        out["ear_min_conf"] = float(min(conf[3], conf[4]))
        return out

    def analyze(self, frame, track, boundary_ratio=None, edge_margin=20,
                keypoints=None):
        """
        对一条轨迹做行为推断。
        返回 (action, bad, behaviors)
          action    主标签（画在框上）
          bad       是否越界（红框）
          behaviors 需要上报的异常行为列表

        keypoints 为 YOLO-pose 结果时优先用骨骼几何判行为（更准）；
        否则沿用基于框宽高比/质心的原有逻辑（HOG 后端就走这条路）。
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

        # === 优先：骨骼点几何判定（YOLO-pose 后端） ===
        pm = self._pose_metrics(keypoints) if keypoints is not None else None
        if pm and pm.get("shoulder_w", 0) > 1.0:
            # 1) 触碰红线：跨线次数 >= 2 视为"多次触线"
            if track.crosses >= 2:
                behaviors.append(ACT_CROSS)
            elif bad:
                action = "越界"

            head_dx = pm.get("head_dx_ratio")
            nose_dy = pm.get("nose_sh_dy_ratio")
            tilt = pm.get("shoulder_tilt_deg")
            torso = pm.get("torso_ratio")
            ear_min = pm.get("ear_min_conf")

            # 2) 转脸偏头：鼻尖明显偏离肩中心
            if head_dx is not None and abs(head_dx) > 0.45:
                action = ACT_HEADTURN
                behaviors.append(ACT_HEADTURN)
            # 3) 长时间低头：鼻尖贴近甚至低于肩线
            if nose_dy is not None and nose_dy < 0.35:
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_DOWN
                behaviors.append(ACT_DOWN)
            # 4) 前倾/趴桌：肩线明显倾斜 或 躯干严重塌陷
            if ((tilt is not None and tilt > 35.0)
                    or (torso is not None and torso < 0.9)):
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_LEAN
                behaviors.append(ACT_LEAN)
            # 5) 起立/站立：框很高且底部越过警戒线
            if h > fh * 0.55 and bad:
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_STAND
                behaviors.append(ACT_STAND)
            # 6) 闭眼/趴睡：眼点置信度极低而耳点仍可见（脸在前面）
            eye_conf = pm.get("eye_conf", 1.0)
            if eye_conf < 0.15 and ear_min is not None and ear_min > 0.4:
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_SLEEPY
                behaviors.append(ACT_SLEEPY)
            # 7) 坐下：框较矮、在画面下半
            if action == ACT_NORMAL and h < fh * 0.45 and y > fh * 0.35:
                action = ACT_SIT
        else:
            # === 回退：原有基于框/质心的判定（HOG、SSD、骨骼点不可用时） ===
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
        self._last_frame_w = 0
        self._last_pose = None
        self._last_scores = None

    def start(self):
        """启动/复位跟踪器（与 stop 后状态一致即可继续运行）"""
        self.stop()

    def stable_count(self):
        """平滑后的人数（最近 5 次分析的中位数），避免数字跳来跳去"""
        return self._stable_count

    # ---------------- 绘制 ----------------
    def analyze_only(self, frame, boundary_ratio=None, min_hits=None,
                     with_pose=True):
        """只做检测/跟踪/行为分析，**不绘制**。

        返回 (persons, violations, line_y)。persons[i]["rect"] 为像素坐标；
        persons[i]["pose"] 为 (17,3) 骨骼点（仅 YOLO-pose 后端有，否则 None）。
        把"分析"与"落笔"拆开，界面层就能按低频率跑分析、按高频率贴图，
        既省 CPU 又不会出现"框时有时无"的闪烁。

        min_hits 可覆盖"最少连续命中次数"：视频流保持默认（滤掉单帧噪声），
        对单张图片做分析时传 min_hits=1。
        """
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)
        _, pose_map = self.update(frame, boundary_ratio=boundary_ratio,
                                  with_pose=True)
        need = self.min_hits if min_hits is None else max(1, int(min_hits))
        tracks = {pid: tr for pid, tr in self._tracks.items() if tr.hits >= need}
        persons = []
        for pid, tr in sorted(tracks.items()):
            kp = pose_map.get(pid) if with_pose else None
            action, bad, behaviors = self.analyze(
                frame, tr, boundary_ratio=boundary_ratio, keypoints=kp)
            persons.append({"pid": pid, "rect": tr.rect, "action": action,
                            "bad": bad, "crosses": tr.crosses,
                            "behaviors": behaviors,
                            "score": tr.score,
                            "pose": kp,
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
                   font_size=13, draw_line=True, draw_pose=True):
        """把 analyze_only 的结果画到帧上（矩形用 cv2，中文标签走位图缓存）。

        scale/offset 用于把"分析分辨率"下的坐标映射到"显示分辨率"。
        draw_pose：有骨骼点时把 17 点骨架画出来（青色细线）。
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

        # COCO 骨架连接（仅用于可视化）
        skel = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
                (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
                (0, 1), (0, 2), (1, 3), (2, 4), (0, 5), (0, 6))

        for p in persons:
            x, y, w, h = p["rect"]
            color = self.person_color(p)
            rx = int(x * scale + ox)
            ry = int(y * scale + oy)
            rw = max(2, int(w * scale))
            rh = max(2, int(h * scale))
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), color, 1)

            if draw_pose and p.get("pose") is not None:
                kp = np.asarray(p["pose"], dtype=np.float32)
                if kp.shape == (17, 3):
                    pts = []
                    for i in range(17):
                        if kp[i, 2] > 0.3:
                            pts.append((int(kp[i, 0] * scale + ox),
                                        int(kp[i, 1] * scale + oy)))
                        else:
                            pts.append(None)
                    for a, b in skel:
                        if pts[a] is not None and pts[b] is not None:
                            cv2.line(frame, pts[a], pts[b], COLOR_POSE, 1)
                    for pt in pts:
                        if pt is not None:
                            cv2.circle(frame, pt, 2, COLOR_POSE, -1)

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

    def draw(self, frame, boundary_ratio=0.5, min_hits=None, with_pose=True):
        """
        返回 (标记后的帧, 越界人数, 逐人信息列表)
        逐人信息: {"pid", "rect", "action", "bad", "crosses",
                   "behaviors", "score", "pose", "density_level"}

        注意：本方法会在传入的 frame 上**原地**绘制并返回同一个数组，
        调用方若需要保留原图请先自行 copy。
        单张图片分析请传 min_hits=1（默认要求连续命中，用于滤掉视频单帧噪声）。
        """
        persons, violations, line_y = self.analyze_only(
            frame, boundary_ratio=boundary_ratio, min_hits=min_hits,
            with_pose=with_pose)
        # 人脸框一并带出来，交给 draw_boxes 统一绘制
        for p in persons:
            tr = self._tracks.get(p["pid"])
            fa = getattr(tr, "face_cache", None) if tr is not None else None
            p["face"] = (fa or {}).get("face") if fa else None
        self.draw_boxes(frame, persons, line_y=line_y, draw_pose=with_pose)
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
