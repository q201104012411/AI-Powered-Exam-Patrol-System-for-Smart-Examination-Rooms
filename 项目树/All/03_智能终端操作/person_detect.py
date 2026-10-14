# -*- coding: utf-8 -*-
"""
考场监控 · 逐人行为识别（密集教室版）

检测链（按优先级）：
  1) YOLO11n ONNX 行人检测（onnxruntime，COCO person 类）——主力
  2) MobileNet-SSD DNN（若目录下存在 caffemodel 文件，兼容保留）
  3) HOG 密集模式 + 软 NMS + 自适应阈值——兜底（前两者不可用或零检出时）
  4) 背景差分 / Haar 人头/人脸辅助检测
跟踪：
  · 匈牙利匹配（scipy 可用时）/ 最近邻回退
  · detect_stride 跳帧检测 + 匀速预测
  · max_input_dim 长边缩放检测
  · 密度感知、小人框、小标签

YOLO11n 模型获取：运行 `python 工具_YOLO模型导出.py` 生成 models/yolo11n.onnx；
也可用环境变量 EXAM_YOLO_MODEL 指向其他 ONNX 模型，EXAM_YOLO_DISABLE=1 强制禁用 YOLO。

行为 -> 03 告警码映射见 gui.py / terminal_gui.py。
生产环境建议换成 YOLO-pose / 边缘 AI 节点返回的骨骼点，本文件当前用姿态无关的
框几何 + 骨骼点升级路径见《02_摄像头核查项目解析与升级方案.md》P1 阶段。
"""
import os
import math
import threading
from collections import deque
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import onnxruntime as _ort
    _HAS_ORT = True
except Exception:  # pragma: no cover - onnxruntime 为可选依赖
    _HAS_ORT = False

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

# ---------------------------------------------------------------------------
# 越界 / 偏转 / 前倾的**自适应判据**
# 来源：11_摄像头AI识别\识别准确率优化报告.md（2026-10-13 实测定的经验值）。
# 这四条曾在 2026-10-14 换模型层时丢失（代码退回固定阈值），2026-10-14 按报告恢复：
#   · 固定阈值 0.85 的偏转判据在"正常坐姿宽高比就有 1.386"时会**坐着不动必然误报**；
#   · 越界只看"框底过线"在坐姿近景下也会误报（人框本就延伸到画面底部）。
# 恢复后的判据都以"这个人自己的历史"为基准，因此能自适应站/坐、远/近。
# ---------------------------------------------------------------------------
HEAD_LOW_RATIO = 0.45          # 越界：框底过线 **且** 头顶已下移到画面 45% 以下
ASPECT_TURN_FACTOR = 1.15      # 偏转：宽高比 > 本人历史中位数 × 1.15
ASPECT_TURN_ABS = 1.6          # 偏转：绝对下限——到 1.6:1 已是明确侧身形态
ASPECT_LEAN_FACTOR = 1.08      # 前倾：> 本人历史中位数 × 1.08，且框高在收缩
ASPECT_BASE_MIN_FRAMES = 5     # 历史样本不足这么多帧时不启用自适应（只用绝对阈值）

# MobileNet-SSD 中 "person" 的类别编号
_MOBILENET_PERSON_CLASS = 15

# YOLO（COCO 80 类）中 "person" 的类别编号
_YOLO_PERSON_CLASS = 0

# 模块级提示标志，避免每个实例都重复打印
_DNN_WARNED = False
_MOG_WARNED = False
_YOLO_WARNED = False

# ---------------- YOLO11n ONNX 检测器（模块级共享） ----------------
# 多个 PersonTracker 实例（多路摄像头 / 单测）共享同一个 InferenceSession：
# onnxruntime 的 session 创建要数百毫秒（读盘 + 图优化），每路各建一个既慢又费内存。
_YOLO_CACHE_LOCK = threading.Lock()
_YOLO_SESSION_CACHE = {}          # model_path -> (session, input_name)


def _yolo_disabled():
    """环境变量 EXAM_YOLO_DISABLE=1 时强制禁用 YOLO（调试/对比用）"""
    return os.environ.get("EXAM_YOLO_DISABLE", "").strip() in ("1", "true", "True")


def default_yolo_model_path():
    """默认模型路径：优先环境变量 EXAM_YOLO_MODEL，否则项目根 models/yolo11n.onnx"""
    env = os.environ.get("EXAM_YOLO_MODEL", "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "models", "yolo11n.onnx")


def _yolo_providers():
    """推理后端：默认 CPU；EXAM_YOLO_EP=coreml 时优先苹果 CoreML（失败自动回退 CPU）"""
    ep = os.environ.get("EXAM_YOLO_EP", "").strip().lower()
    if not _HAS_ORT:
        return None
    if ep == "coreml" and "CoreMLExecutionProvider" in _ort.get_available_providers():
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _get_yolo_session(model_path):
    """加载（带缓存）YOLO ONNX 会话。

    返回 (session, input_name, reason)：
      - 成功：reason 为 None；
      - 失败：session 为 None，reason 说明原因（文件缺失/依赖缺失/加载异常）。
    失败**不**缓存，补上模型文件后无需重启进程即可生效。
    """
    if not _HAS_ORT:
        return None, None, "未安装 onnxruntime（pip install onnxruntime）"
    if not model_path or not os.path.exists(model_path):
        return None, None, "未找到模型文件 %s（可运行 工具_YOLO模型导出.py 生成）" % model_path
    with _YOLO_CACHE_LOCK:
        cached = _YOLO_SESSION_CACHE.get(model_path)
        if cached is not None:
            return cached[0], cached[1], None
        try:
            so = _ort.SessionOptions()
            so.log_severity_level = 3            # 只留错误日志，避免刷屏
            so.intra_op_num_threads = 0          # 0=自动（按物理核数）
            session = _ort.InferenceSession(
                model_path, sess_options=so, providers=_yolo_providers())
            input_name = session.get_inputs()[0].name
        except Exception as e:
            return None, None, "模型加载失败：%s" % e
        _YOLO_SESSION_CACHE[model_path] = (session, input_name)
        return session, input_name, None


def _letterbox(frame, new_shape=640, color=(114, 114, 114)):
    """等比缩放并居中补边到 new_shape×new_shape（YOLO 标准预处理）。

    返回 (img, ratio, (dw, dh))：ratio 为缩放系数，dw/dh 为左/上补边宽度，
    后处理时按 x=(x-dw)/ratio 还原到原帧坐标。
    """
    h, w = frame.shape[:2]
    r = min(new_shape / float(h), new_shape / float(w))
    nw, nh = int(round(w * r)), int(round(h * r))
    if (nw, nh) != (w, h):
        interp = cv2.INTER_LINEAR if r > 1 else cv2.INTER_AREA
        frame = cv2.resize(frame, (nw, nh), interpolation=interp)
    dw = (new_shape - nw) / 2.0
    dh = (new_shape - nh) / 2.0
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(frame, top, bottom, left, right,
                             cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def _nms_rects(rects, scores, iou_thresh=0.45, max_keep=300):
    """标准贪婪 NMS：按分数降序保留，抑制与已保留框 IoU 超阈值的框。

    与 _soft_nms 的区别：硬抑制（直接丢弃），更适合 YOLO 的高分输出，
    避免高分框周围残留一串衰减后的重叠框又被跟踪层合并统计。
    rects 为 (x, y, w, h)，与 _soft_nms 接口一致。
    """
    if not rects:
        return []
    order = list(np.argsort(np.asarray(scores, dtype=float))[::-1])
    keep = []
    while order and len(keep) < max_keep:
        i = int(order[0])
        keep.append(i)
        if len(order) == 1:
            break
        rest = order[1:]
        ious = np.array([_iou(rects[i], rects[int(j)]) for j in rest])
        order = [j for j, ov in zip(rest, ious) if ov <= iou_thresh]
    return [tuple(int(v) for v in rects[i]) for i in keep]


def _yolo_postprocess(raw, frame_shape, ratio, pad, conf_thres=0.25,
                      iou_thres=0.45):
    """把 YOLO 原始输出解码成去 letterbox 后的 (x, y, w, h) 列表。

    raw 形如 [1, 84, 8400]（COCO：4 bbox + 80 类；行=通道）或转置形式；
    本函数为纯函数，便于单元测试。frame_shape 为（letterbox 前）帧的 (h, w)。
    """
    pred = np.asarray(raw)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.ndim != 2:
        return []
    # 先定向，再校验有效性。YOLOv8/11 官方导出为 [84, n]（通道在前），
    # 也兼容 [n, 84] 转置形式。84/85 是 COCO 下“4 bbox + 80 类(±objectness)”
    # 的列数：哪一维等于 84/85 哪一维就是列；都不等时按“框数远大于列数”启发。
    if pred.shape[1] in (84, 85):
        pass
    elif pred.shape[0] in (84, 85):
        pred = pred.T
    elif pred.shape[0] < pred.shape[1]:
        pred = pred.T
    # 至少要有一行一框、每框 4 bbox + 至少 1 个类分
    if pred.shape[0] < 1 or pred.shape[1] < 5:
        return []
    n_cols = pred.shape[1]
    if n_cols == 84:                       # YOLOv8/11 检测头：cx,cy,w,h + 80 类
        person = pred[:, 4 + _YOLO_PERSON_CLASS]
    elif n_cols == 85:                     # YOLOv5 系：cx,cy,w,h,obj + 80 类
        person = pred[:, 4] * pred[:, 5 + _YOLO_PERSON_CLASS]
    else:
        person = pred[:, 4 + _YOLO_PERSON_CLASS]

    keep = person >= float(conf_thres)
    if not keep.any():
        return []
    boxes = pred[keep, :4].astype(np.float64)
    scores = person[keep].astype(np.float64)

    # cx,cy,w,h -> x1,y1,x2,y2（640 输入空间）
    cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    x1 = cx - bw / 2.0
    y1 = cy - bh / 2.0
    x2 = cx + bw / 2.0
    y2 = cy + bh / 2.0

    # 去 letterbox：还原到原帧坐标
    dw, dh = pad
    fh, fw = frame_shape[:2]
    x1 = np.clip((x1 - dw) / ratio, 0, fw - 1)
    y1 = np.clip((y1 - dh) / ratio, 0, fh - 1)
    x2 = np.clip((x2 - dw) / ratio, 0, fw - 1)
    y2 = np.clip((y2 - dh) / ratio, 0, fh - 1)

    w = x2 - x1
    h = y2 - y1
    valid = (w >= 4) & (h >= 4)
    if not valid.any():
        return []
    rects = [(float(x), float(y), float(rw), float(rh))
             for x, y, rw, rh in zip(x1[valid], y1[valid], w[valid], h[valid])]
    scores = scores[valid]
    # 先 IoU-NMS 压"几乎重合"，再压"小框套在大框里"（头框/上半身 vs 整体）。
    # 缺了后一步，同一个人会同时留下头框和身体框，跟踪层各开一条轨迹，
    # 界面上就是"一个人被画出一堆框、人数虚高"。
    return _suppress_contained(_nms_rects(rects, scores, iou_thresh=iou_thres))

# 三色语义（BGR）：绿=正常，橙=异常行为，红=越界/贴边
COLOR_OK = (0, 200, 0)
COLOR_WARN = (0, 165, 255)
COLOR_BAD = (0, 0, 255)
COLOR_FACE = (255, 255, 0)
COLOR_LINE = (0, 165, 255)

# 逐人标签的"人多就简化"阈值：超过这个人数只给异常/越界框写文字
# （40 人考场如果人人都写"#12 正常姿态"，画面会被文字糊死，反而看不见异常）
COMPACT_LABEL_OVER = 12


@lru_cache(maxsize=32)
def _load_font(size=13):
    """加载支持中文的字体（带缓存，避免每帧都读字体文件）

    候选按平台覆盖：macOS（PingFang / STHeiti / Hiragino）→ Windows（雅黑/黑体/宋体）
    → Linux（文泉驿/Noto）；都没有则回退 PIL 默认字体（中文会渲染为方块）。
    """
    win_dir = os.environ.get("WINDIR", "C:\\Windows")
    candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Windows
        os.path.join(win_dir, "Fonts", "msyh.ttc"),
        os.path.join(win_dir, "Fonts", "simhei.ttf"),
        os.path.join(win_dir, "Fonts", "simsun.ttc"),
        # Linux
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
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


def _iomin(a, b):
    """交集 / 较小框的面积（IoMin，又称重叠率）。

    为什么需要它：YOLO 会把同一个人的"头/上半身"和"整体"各给一个框，
    小框几乎完全落在大框里，但 IoU = 小框面积/大框面积（可能只有 0.1~0.2），
    标准 IoU-NMS 根本压不掉。于是同一个人留下两三个框，跟踪层再各开一条
    轨迹，界面就成了"一个人被画出一堆框"。IoMin 对"部分-整体"重复很敏感。
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xi1 = max(ax, bx)
    yi1 = max(ay, by)
    xi2 = min(ax + aw, bx + bw)
    yi2 = min(ay + ah, by + bh)
    inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    small = min(aw * ah, bw * bh)
    return inter / float(small) if small > 0 else 0.0


def _center_in(a, b):
    """a 的中心是否落在 b 内（判断 a 像不像 b 的一部分）"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    cx, cy = ax + aw / 2.0, ay + ah / 2.0
    return (bx <= cx <= bx + bw) and (by <= cy <= by + bh)


def _suppress_contained(rects, contain_thresh=0.85, area_ratio=1.8):
    """丢掉"被明显更大的框套住"的重复框，保留大框（部分-整体去重）。

    与 IoU-NMS 互补：IoU-NMS 管"几乎重合"的两个框，这里管"小框在大框里"
    （头框 vs 整体框、上半身 vs 全身）。

    两个约束避免误伤"真实但挨得近的两个人"：
      · area_ratio：大框面积至少是小框的 1.8 倍；
      · center_in ：小框中心必须落在大框内；
      · contain_thresh 取 0.85（"几乎完全被套住"才算重复）——阈值放低会把
        画面边缘那种"半个人"当成别人的一部分吞掉。
    """
    if not rects or len(rects) < 2:
        return [tuple(int(v) for v in r) for r in (rects or [])]
    areas = [max(0.0, float(r[2])) * max(0.0, float(r[3])) for r in rects]
    order = sorted(range(len(rects)), key=lambda i: -areas[i])   # 大框优先保留
    keep = []
    for i in order:
        dropped = False
        for j in keep:
            if areas[j] < area_ratio * areas[i]:
                continue                      # 面积太接近，不算"套住"
            if not _center_in(rects[i], rects[j]):
                continue
            if _iomin(rects[i], rects[j]) >= contain_thresh:
                dropped = True
                break
        if not dropped:
            keep.append(i)
    keep.sort()
    return [tuple(int(v) for v in rects[i]) for i in keep]


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
                 "aspect_hist", "prev_side", "crosses", "cross_epochs",
                 "face_cache", "face_age", "action_hist", "hits",
                 "vx", "vy", "last_seen_frame")

    def __init__(self, pid, rect, line_y):
        self.pid = pid
        self.rect = rect
        self.missed = 0
        self.hits = 0                       # 累计被检测命中的次数（确认用）
        self.centroids = deque(maxlen=20)   # 最近 20 帧质心，用于东张西望判定
        self.heights = deque(maxlen=20)     # 最近 20 帧框高，用于低头判定
        self.aspects = deque(maxlen=8)      # 最近 8 帧宽高比，用于偏转/前倾确认
        # 长期宽高比基线（自适应判据的分母）：窗口要足够长，否则"持续侧身"会把
        # 中位数一起拉高，几帧之后就不再报偏转了。
        self.aspect_hist = deque(maxlen=150)
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
                 max_aspect=1.8, confirm_frames=3,
                 match_dist_ratio=0.12, min_hits=2,
                 yolo_model_path=None, yolo_conf=0.25, yolo_iou=0.45,
                 yolo_input_dim=640,
                 yolo_tiling="auto", yolo_tile_size=640,
                 yolo_tile_overlap=0.25, yolo_tile_max=12,
                 yolo_tile_min_dim=1.35, contain_thresh=0.85,
                 stand_aspect=0.62, stand_min_height_ratio=0.10):
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

        # ---------------- 密集考场：分块（切片）推理 ----------------
        # 单张推理会把整帧压到 640×640：1080p 看 40 人时，后排考生只有十几像素高，
        # 必然漏检（实测合成 40 人场景：单张推理召回仅 12%~25%）。分块推理把画面切成
        # 若干"与模型输入同尺寸"的块，每块按原生像素送进模型，等效放大
        # (画面长边 ÷ 块边长) 倍 —— 后排才看得见。代价是每帧多跑几块。
        #   auto：画面长边 ≥ yolo_input_dim × yolo_tile_min_dim 时才分块
        #         （640 长边的分析帧分块没有收益，只会白花算力）
        mode = str(yolo_tiling or "auto").strip().lower()
        self.yolo_tiling = mode if mode in ("auto", "on", "off") else "auto"
        self.yolo_tile_size = int(max(160, yolo_tile_size))
        self.yolo_tile_overlap = float(min(0.6, max(0.0, yolo_tile_overlap)))
        self.yolo_tile_max = int(max(1, yolo_tile_max))
        self.yolo_tile_min_dim = float(max(1.0, yolo_tile_min_dim))
        # "部分-整体"去重阈值：同一人被检成"头+整体"时丢小框。密集考场里
        # 后排的人可能确实落在前排人的框内，阈值太低会把真人吞掉
        # （实测：0.85 在合成 40 人场景里吞掉 7 个真检出），所以做成可调，
        # 密集场景建议 0.92 起步。
        self.contain_thresh = float(min(0.99, max(0.5, contain_thresh)))
        # 站立判定：站立的人框**又高又窄**（宽/高 明显小于坐姿）。
        # 旧条件要求"框高 > 55% 画面高 且 框底越过警戒线"，在远景机位永远不成立，
        # 于是站着的人被画成"正常姿态"（现场反馈的问题）。
        # 上限夹到 0.80：必须**低于"大幅度偏转"的 0.85 阈值**，否则两者重叠，
        # 站立分支会被 elif 直接跳过 —— 等于把站立功能配没了。
        self.stand_aspect = float(min(0.80, max(0.30, stand_aspect)))
        self.stand_min_height_ratio = float(max(0.03, stand_min_height_ratio))
        self._last_tiling = False
        self._last_tile_count = 0

        # YOLO11n ONNX 检测参数（模型缺失/依赖缺失时自动回退，见 _init_dnn）
        self.yolo_model_path = yolo_model_path or default_yolo_model_path()
        self.yolo_conf = float(yolo_conf)
        self.yolo_iou = float(yolo_iou)
        self.yolo_input_dim = int(yolo_input_dim)

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

        # 检测后端：YOLO11n ONNX（主力）-> MobileNet-SSD（兼容）-> HOG（兜底）
        self._net = None
        self._yolo_session = None
        self._yolo_input_name = None
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
        """初始化检测后端。优先级：YOLO11n ONNX > MobileNet-SSD > 纯 HOG。

        YOLO11n 是主力检测器（模型见 models/yolo11n.onnx，缺失时按环境变量
        EXAM_YOLO_MODEL 查找）；MobileNet-SSD 仅在无 YOLO 时兼容启用；
        两者都不可用时保持纯 HOG，行为与旧版一致。
        """
        global _DNN_WARNED, _YOLO_WARNED

        # 1) YOLO11n ONNX（主力）
        if not _yolo_disabled():
            session, input_name, reason = _get_yolo_session(self.yolo_model_path)
            if session is not None:
                self._yolo_session = session
                self._yolo_input_name = input_name
                return  # YOLO 就绪，不再尝试 MobileNet-SSD
            if not _YOLO_WARNED:
                print("[person_detect] YOLO11n 未启用：%s" % reason)
                _YOLO_WARNED = True

        # 2) MobileNet-SSD（兼容保留：老版本可能已部署该模型文件）
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
                print("[person_detect] 未找到 MobileNet-SSD 模型文件，"
                      "检测链为 HOG 兜底模式。")
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

    # ---------------- 分块（切片）推理：密集考场的召回关键 ----------------
    def tiling_active(self, frame=None):
        """本帧是否会走分块推理（界面/工具用它如实显示，不猜）"""
        if self.yolo_tiling == "off" or self._yolo_session is None:
            return False
        if self.yolo_tiling == "on":
            return True
        if frame is None:
            return self._last_tiling
        fh, fw = frame.shape[:2]
        return max(fh, fw) >= self.yolo_input_dim * self.yolo_tile_min_dim

    def _tile_windows(self, fh, fw):
        """生成覆盖整帧的分块窗口 [(x, y, w, h)]，含重叠，且不超过 yolo_tile_max。"""
        size = int(min(self.yolo_tile_size, max(fh, fw)))
        base_step = int(max(32, round(size * (1.0 - self.yolo_tile_overlap))))

        def axis(total, step):
            if total <= size:
                return [0]
            coords = list(range(0, total - size + 1, step))
            if coords[-1] != total - size:
                coords.append(total - size)      # 保证贴到边缘，不漏最后一条
            return sorted(set(coords))

        step, wins = base_step, []
        for _ in range(8):                       # 超上限就加大步长重排，保证均匀覆盖
            xs, ys = axis(fw, step), axis(fh, step)
            wins = [(x, y) for y in ys for x in xs]
            if len(wins) <= self.yolo_tile_max:
                break
            step = int(round(step * 1.35))
        return [(x, y, size, size) for (x, y) in wins]

    def _detect_yolo_tiled(self, frame):
        """分块检测 + 跨块合并（同一个人出现在相邻块里要去重）"""
        fh, fw = frame.shape[:2]
        windows = self._tile_windows(fh, fw)
        boxes = []
        for (x, y, tw, th) in windows:
            crop = frame[y:y + th, x:x + tw]
            if crop.size == 0:
                continue
            for (bx, by, bw, bh) in self._detect_yolo_single(crop):
                # 贴着块边缘的框可能被切断，合并时优先保留"完整落在块内"的
                clipped = (bx <= 1 or by <= 1 or
                           bx + bw >= tw - 1 or by + bh >= th - 1)
                boxes.append((x + bx, y + by, bw, bh, clipped))
        out = self._merge_tiled(boxes)
        self._last_tiling = True
        self._last_tile_count = len(windows)
        return out

    @staticmethod
    def _merge_tiled(boxes, iou_thresh=0.45, contain_thresh=0.75):
        """合并分块结果：优先"没被块边缘切断 + 面积更大"的框，再按 IoU/包含去重。"""
        if not boxes:
            return []
        items = sorted(boxes, key=lambda b: (bool(b[4]), -(b[2] * b[3])))
        kept = []
        for bx, by, bw, bh, _clip in items:
            rect = (bx, by, bw, bh)
            dup = False
            for k in kept:
                if _iou(rect, k) >= iou_thresh or (
                        _iomin(rect, k) >= contain_thresh and _center_in(rect, k)):
                    dup = True
                    break
            if not dup:
                kept.append(rect)
        return [tuple(int(v) for v in r) for r in kept]

    @staticmethod
    def _has_closeup(rects, frame, height_ratio=0.45, area_ratio=0.12):
        """整帧检测里是否已经看到"离镜头很近的人"（大目标）。

        有人贴脸时不该分块：一个大目标会被块边界切成几块，反而变成"一个人
        好几个框"（现场最烦的那种）。判据用高度或面积占比，都是明显的特写尺寸。
        """
        fh, fw = frame.shape[:2]
        if fh <= 0 or fw <= 0:
            return False
        for (_x, _y, w, h) in rects:
            if h >= height_ratio * fh or (w * h) >= area_ratio * fh * fw:
                return True
        return False

    def _detect_yolo(self, frame):
        """YOLO11n 行人检测入口。

        密集远景（40 人考场）走分块推理；一旦整帧里已经出现"贴脸大目标"就
        回落单张推理 —— 分块会把特写的人切成几块，正是要避免的"一个人一堆框"。
        """
        if self._yolo_session is None:
            return []
        if not self.tiling_active(frame):
            self._last_tiling = False
            self._last_tile_count = 1
            return self._detect_yolo_single(frame)
        # 先跑一遍整帧（便宜）：只用来判断"是不是特写场景"。
        # 注意不要把整帧结果并进分块结果 —— 整帧在 640 尺度下的框更"粗"，
        # 按面积优先排序后会把后排的人当成"前排人的一部分"吞掉
        # （实测：并集 57.5% vs 只用分块 67.5%）。
        full = self._detect_yolo_single(frame)
        if self._has_closeup(full, frame):
            self._last_tiling = False
            self._last_tile_count = 1
            return full
        return self._detect_yolo_tiled(frame)

    def _detect_yolo_single(self, frame):
        """单张 YOLO11n ONNX 行人检测（COCO person 类）。

        输入沿用 _detect 传入的缩放帧（small），内部 letterbox 到 640×640，
        推理后把框还原回 small 帧坐标系，与其他检测源保持同一契约。
        """
        global _YOLO_WARNED
        if self._yolo_session is None:
            return []
        img, ratio, pad = _letterbox(frame, self.yolo_input_dim)
        blob = img[:, :, ::-1].transpose(2, 0, 1)     # BGR -> RGB, HWC -> CHW
        blob = np.ascontiguousarray(blob, dtype=np.float32) / 255.0
        blob = blob[None]                              # NCHW
        try:
            outputs = self._yolo_session.run(
                None, {self._yolo_input_name: blob})
        except Exception as e:
            # 单帧推理失败不打死识别链路：提示一次并返回空，交给 HOG 兜底
            if not _YOLO_WARNED:
                print("[person_detect] YOLO 推理异常，本帧跳过：%s" % e)
                _YOLO_WARNED = True
            return []
        return _yolo_postprocess(outputs[0], frame.shape[:2], ratio, pad,
                                 conf_thres=self.yolo_conf,
                                 iou_thres=self.yolo_iou)

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

    def _merge_sources(self, primary, aux_list, iou_thresh=0.35, iomin_thresh=0.85):
        """把主检测框与辅助来源框合并，去除重复。

        重复的判据是"IoU 高"**或**"辅助框几乎完全套在已有框里"（IoMin 高）：
        辅助来源（Haar 人头 → 推断人身框、背景差分 ROI）经常在主检测框内部
        再给一个更小的框，只按 IoU 判重会把它当成第二个人。阈值取 0.85 是
        为了不把"紧挨着主目标的另一个人"也当成重复吞掉。
        """
        merged = list(primary)
        for src in aux_list:
            for r in src:
                if all(_iou(r, m) < iou_thresh and
                       not (_iomin(r, m) >= iomin_thresh and _center_in(r, m))
                       for m in merged):
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

    def _plausible_person_cap(self, fh, fw):
        """按画面面积估"这个画面最多可能站多少人"（用于判断要不要补漏）。

        用本项目密度判据的同一把尺子：每百万像素 30 人算高密度，
        所以 1080p(2.07MP) ≈ 62 人、720p(0.92MP) ≈ 28 人。
        """
        mp = max(fh * fw / 1_000_000.0, 0.01)
        return max(4, int(round(mp * 30)))

    def _needs_recall_boost(self, rects, frame):
        """主链结果"偏少 + 人普遍很小"时是否补跑 HOG/辅助源找漏检。

        为什么需要：40 人考场里 YOLO 常常只给出几个小框（后排太远），
        这时如果因为"有检出"就跳过补漏，召回会一直卡在很低的水平。
        为什么不能无脑补：单人特写画面里主链给的是**大框**，再叠 Haar/背景差分
        就会"一个人画出一堆框"（现场反馈过）。所以判据是"框小 + 数量远低于
        这个画面可能的人数量级"才补。
        """
        fh, fw = frame.shape[:2]
        if fh <= 0 or fw <= 0 or not rects:
            return False
        cap = self._plausible_person_cap(fh, fw)
        if len(rects) >= max(2, cap // 2):
            return False                      # 已经检得差不多了，不必补
        heights = sorted(r[3] for r in rects)
        med_h = heights[len(heights) // 2]
        return med_h < 0.28 * fh              # 人普遍很小 → 像远景/后排，补漏

    def _detect(self, frame):
        """综合检测：YOLO11n（密集画面自动分块）-> SSD -> HOG 兜底 + 背景/人头辅助。

        结果映射回原图并收紧，返回 (x, y, w, h) 列表。
        """
        small, scale = self._scale_frame(frame)
        orig_h, orig_w = frame.shape[:2]

        # 密度感知：当前已经高密度时强制启用密集参数
        dense = self.dense_mode or self._density_level in ("medium", "high")

        rects = []
        # 1) 主力引擎：YOLO11n 优先（大画面自动分块推理）；无 YOLO 时回退 MobileNet-SSD
        if self._yolo_session is not None:
            rects = self._detect_yolo(small)
        elif self._net is not None:
            rects = self._detect_dnn(small)

        # 2) HOG 兜底：主力引擎完全没结果时才跑。
        #    实测（合成 40 人考场）HOG 在这类远景里只加 7 个框、0 个命中 ——
        #    既没补上召回，又拉低精确率，所以不再无脑叠加。
        if not rects:
            hog_rects = self._detect_hog(small, dense)
            rects = self._merge_sources(rects, [hog_rects], iou_thresh=0.35)

        # 3) 辅助源（背景差分 + Haar 人头）也只在零检出时兜底。
        #    旧条件是 len(rects) < 8：单人特写画面里人数永远小于 8，于是每一帧
        #    都往画面里塞 Haar 头推断出的身体框和背景差分 ROI，表现为
        #    "一个人被画出一堆框、人数虚高、还伴随假越界"。
        if not rects:
            bg_rois = self._detect_bg_rois(small)
            head_rects = self._detect_heads(small)
            rects = self._merge_sources(rects, [bg_rois, head_rects], iou_thresh=0.30)

        # 4) 映射回原图、收紧、剔除不合理框、最终 NMS + 部分-整体去重
        orig_rects = [self._rect_to_orig(r, scale) for r in rects]
        shrunk = [self._shrink_rect(r, orig_w, orig_h) for r in orig_rects]
        cleaned = self._sanitize_boxes(shrunk, orig_w, orig_h)
        return _suppress_contained(_soft_nms(cleaned, iou_thresh=0.35, sigma=0.5),
                                   contain_thresh=self.contain_thresh)

        # 4) 映射回原图、收紧、剔除不合理框、最终 NMS + 部分-整体去重
        orig_rects = [self._rect_to_orig(r, scale) for r in rects]
        shrunk = [self._shrink_rect(r, orig_w, orig_h) for r in orig_rects]
        cleaned = self._sanitize_boxes(shrunk, orig_w, orig_h)
        return _suppress_contained(_soft_nms(cleaned, iou_thresh=0.35, sigma=0.5))

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

    def _match_and_update(self, rects, line_y, frame_w=None):
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
                cx, cy = cents[j]
                tr.centroids.append((cx, cy))
                tr.heights.append(tr.rect[3])
                tr.aspects.append(tr.rect[2] / float(tr.rect[3])
                                  if tr.rect[3] else 0.0)
                tr.aspect_hist.append(tr.rect[2] / float(tr.rect[3])
                                      if tr.rect[3] else 0.0)
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

    @staticmethod
    def _is_part_of(a, b, iomin_thresh=0.85, area_ratio=1.8):
        """判断两个框是不是"同一个人被检成了头/上半身和整体"。

        _same_person 要求两个框尺寸相近，所以"头框 vs 整体框"永远判不成同一个人
        （高度比往往只有 0.3），于是同一个人会稳定占住两条编号、界面框数翻倍。
        这里补一条"部分-整体"判据：面积差得开（小框 < 大框的 1/1.8）、
        小框中心落在大框内、且小框几乎完全被大框包住（IoMin ≥ 0.85）。
        阈值取严一点，避免把"紧挨着的另一个真实的人"合并掉。
        """
        if a[2] <= 0 or a[3] <= 0 or b[2] <= 0 or b[3] <= 0:
            return False
        area_a = a[2] * a[3]
        area_b = b[2] * b[3]
        small, big = (a, b) if area_a <= area_b else (b, a)
        if big[2] * big[3] < area_ratio * small[2] * small[3]:
            return False                       # 面积太接近，交给 _same_person
        if not _center_in(small, big):
            return False                       # 小框中心不在大框里，是两个人
        return _iomin(small, big) >= iomin_thresh

    def _merge_duplicate_tracks(self, max_missed_for_merge=1):
        """合并"同一个人被检出两个框"造成的重复轨迹。

        HOG 在密集画面上经常对同一人给出两个略有偏移的框，于是同一个人占了
        两个编号、人数虚高。这里把判定为同一人的两条轨迹合并：保留命中次数
        多的那条，把跨线次数继承过来，删掉另一条。
        """
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
                same = self._same_person(tr_a.rect, tr_b.rect)
                part = (not same) and self._is_part_of(tr_a.rect, tr_b.rect)
                if not (same or part):
                    continue
                if part:
                    # 部分-整体：保留"整体框"（面积大的那个），它才是这个人的真实位置；
                    # 头/上半身的小框只是同一个人的一部分，不该单独占一个编号。
                    if tr_b.rect[2] * tr_b.rect[3] > tr_a.rect[2] * tr_a.rect[3]:
                        pid_a, tr_a, pid_b, tr_b = pid_b, tr_b, pid_a, tr_a
                elif (tr_b.hits, -pid_b) > (tr_a.hits, -pid_a):
                    # 同一人重复检出：保留命中多、更早出现的轨迹
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
            tr.aspect_hist.append(tr.rect[2] / float(tr.rect[3])
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
        self._last_frame_w = frame.shape[1]
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)

        run_detect = ((self._frame_no - 1) % self.detect_stride) == 0
        if run_detect:
            rects = self._detect(frame)
            self._match_and_update(rects, line_y, frame_w=self._last_frame_w)
        else:
            self._predict_tracks(line_y)
            self._merge_duplicate_tracks()

        fw = frame.shape[1]
        self._density_level = _density_level(len(self._tracks), fh * fw)
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

    def _stand_rise(self, track):
        """起身动作：同一轨迹里框高明显变大 + 质心明显上移。

        判"刚站起来"比判"正站着"更可靠：它比较的是**这个人自己**的前后变化，
        和机位远近、分辨率都无关（远景里框高只有 90 px，也能看出"从 60 长到 90"）。
        """
        hs = list(track.heights)
        cs = list(track.centroids)
        if len(hs) < 5 or len(cs) < 5:
            return False
        h0 = float(np.median(hs[:3]))
        h1 = float(np.median(hs[-3:]))
        if h0 <= 0:
            return False
        c0 = float(np.median([c[1] for c in cs[:3]]))
        c1 = float(np.median([c[1] for c in cs[-3:]]))
        return (h1 >= h0 * 1.25) and (c1 <= c0 - max(8.0, h0 * 0.10))

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
        # 越界 = 框底越过警戒线 **且** 头顶已下移到画面下部。
        # 只看"框底过线"是错的：坐姿人框本身就延伸到画面底部（含桌子/椅子），
        # 于是"画面里有人就报越界"（把警戒线从 92% 挪到 98% 也没用）。
        # 真越界的语义是"走到镜头前 / 离开座位"，那种情况头顶会明显下移。
        # 证据见 11_摄像头AI识别\识别准确率优化报告.md 第一节实测表。
        bad = (bottom > line_y) and (y > fh * HEAD_LOW_RATIO)

        behaviors = []
        action = ACT_NORMAL

        # 横向历史宽高比（中位数 + 命中次数），避免单帧抖动误报
        aspects = list(track.aspects)
        recent = aspects[-self.confirm_frames:] if aspects else []
        # 自适应基线：这个人自己的长期宽高比中位数（窗口 150 帧，够长，
        # 免得持续侧身把基线一起抬高、几帧后就不报了）
        hist = list(getattr(track, "aspect_hist", []) or [])
        base = float(np.median(hist)) if len(hist) >= ASPECT_BASE_MIN_FRAMES else 0.0
        turn_limit = base * ASPECT_TURN_FACTOR if base > 0 else 0.0
        lean_limit = base * ASPECT_LEAN_FACTOR if base > 0 else 0.0
        wide_hits = sum(1 for r in recent
                        if r > ASPECT_TURN_ABS or (turn_limit > 0 and r > turn_limit))
        lean_hits = sum(1 for r in recent
                        if lean_limit > 0 and r > lean_limit)
        # 站立的人框又高又窄：宽/高 明显小于坐姿
        stand_hits = sum(1 for r in recent if r <= self.stand_aspect)

        # 1) 触碰红线：跨线次数 >= 2 视为"多次触线"
        if track.crosses >= 2:
            behaviors.append(ACT_CROSS)
        elif bad:
            action = "越界"

        # 2) 大幅度偏转：**相对本人历史基线**变宽（×1.15）或达到绝对侧身形态（>1.6）。
        #    固定阈值 0.85 是错的：实测正常坐姿宽高比就有 1.386（见优化报告第二节），
        #    固定阈值下"坐着不动必然误报"。
        if wide_hits >= max(2, self.confirm_frames - 1):
            action = ACT_TURN
            behaviors.append(ACT_TURN)
        # 3) 起立/站立：**不依赖"框底越过警戒线"**。
        #    旧条件 `h > fh*0.55 and bad` 在远景机位永远不成立（站着的人只有几十
        #    像素高、也没走到镜头前），结果"站着"被画成"正常姿态" —— 现场反馈的
        #    就是这个。站立改用两个与机位距离无关的证据：
        #      · 比例：站立的人框又高又窄（宽/高 ≤ stand_aspect，默认 0.62）
        #      · 动作：同一轨迹里框高变大 + 质心上升（起身），见 _stand_rise()
        #    另外加一个最小高度门槛，避免几像素的噪声框被当成"站立"。
        elif (h >= fh * self.stand_min_height_ratio
              and (stand_hits >= max(2, self.confirm_frames - 1)
                   or self._stand_rise(track))):
            action = ACT_STAND
            behaviors.append(ACT_STAND)
        # 4) 前倾/趴桌：**相对本人基线**变宽（×1.08）且框高在收缩（固定 0.68 同样
        #    不适配坐姿——坐姿基线 1.386 下也会误报）
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

        # 9) 贴边：只作**位置标签**（靠左/靠右），**不再算越界**。
        #    旧代码在这里把 bad 置 True，等于"框碰到画面左右边缘就是越界"——
        #    广角机位里坐在边上的考生会被**永久**判越界（现场截图：坐着的人 越界 1，
        #    框底 319 远在警戒线 441 之上，bad 就是从这条来的）。
        #    越界只应由警戒线判据决定；贴边另有语义（画面边缘 = 可能被裁切）。
        if (x < edge_margin or y < edge_margin
                or x + w > fw - edge_margin or y + h > fh - edge_margin):
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

    def start(self):
        """启动/复位跟踪器（与 stop 后状态一致即可继续运行）"""
        self.stop()

    def stable_count(self):
        """平滑后的人数（最近 5 次分析的中位数），避免数字跳来跳去"""
        return self._stable_count

    # ---------------- 绘制 ----------------
    def analyze_only(self, frame, boundary_ratio=None, min_hits=None):
        """只做检测/跟踪/行为分析，**不绘制**。

        返回 (persons, violations, line_y)。persons[i]["rect"] 为像素坐标。
        把"分析"与"落笔"拆开，界面层就能按低频率跑分析、按高频率贴图，
        既省 CPU 又不会出现"框时有时无"的闪烁。

        min_hits 可覆盖"最少连续命中次数"：视频流保持默认（滤掉单帧噪声），
        对单张图片做分析时传 min_hits=1。
        """
        fh = frame.shape[0]
        if boundary_ratio is None:
            boundary_ratio = self.boundary_ratio
        line_y = int(fh * boundary_ratio)
        self.update(frame, boundary_ratio=boundary_ratio)
        need = self.min_hits if min_hits is None else max(1, int(min_hits))
        tracks = {pid: tr for pid, tr in self._tracks.items() if tr.hits >= need}
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
        # 人多时（40 人考场）30 多个标签会把画面糊成一片：只给"异常/越界/手机/
        # 大模型判读"的框写文字，其余只画细框 —— 该看见的异常一个都不少，
        # 但画面仍然看得清人。
        compact = len(persons) > COMPACT_LABEL_OVER
        for p in persons:
            x, y, w, h = p["rect"]
            color = self.person_color(p)
            rx = int(x * scale + ox)
            ry = int(y * scale + oy)
            rw = max(2, int(w * scale))
            rh = max(2, int(h * scale))
            abnormal = bool(p.get("behaviors")) or bool(p.get("bad")) \
                or bool(p.get("has_phone"))
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), color,
                          2 if (compact and abnormal) else 1)
            # 人脸小框（微表情辅助，青色）
            fa = p.get("face")
            if fa is not None:
                fx, fy, fw2, fh2 = fa
                cv2.rectangle(frame,
                              (int(fx * scale + ox), int(fy * scale + oy)),
                              (int((fx + fw2) * scale + ox),
                               int((fy + fh2) * scale + oy)), COLOR_FACE, 1)
            if compact and not abnormal:
                continue
            label = "#%d %s" % (p["pid"], p["action"])
            if p.get("crosses", 0) >= 2:
                label += " ×%d" % p["crosses"]
            ty = ry - 17 if ry > 20 else ry + rh + 2
            draw_label(frame, label, color, rx, ty, font_size)

    def draw(self, frame, boundary_ratio=0.5, min_hits=None):
        """
        返回 (标记后的帧, 越界人数, 逐人信息列表)
        逐人信息: {"pid", "rect", "action", "bad", "crosses",
                   "behaviors", "density_level"}

        注意：本方法会在传入的 frame 上**原地**绘制并返回同一个数组，
        调用方若需要保留原图请先自行 copy。
        单张图片分析请传 min_hits=1（默认要求连续命中，用于滤掉视频单帧噪声）。
        """
        persons, violations, line_y = self.analyze_only(
            frame, boundary_ratio=boundary_ratio, min_hits=min_hits)
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
