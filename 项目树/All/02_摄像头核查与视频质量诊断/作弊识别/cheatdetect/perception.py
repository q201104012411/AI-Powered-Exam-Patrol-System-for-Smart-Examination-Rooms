# -*- coding: utf-8 -*-
"""感知层：把摄像头/视频的原始画面转成引擎所需的 FrameFeatures。

【注意】 能力边界（**必须如实说明**，本模块不做任何"假装测到"的事）
------------------------------------------------------------------
摄像头路径只使用**无需额外下载**的模型：
  · 人体检测：YOLO11n ONNX（onnxruntime）→ 失败则 HOG+SVM 兜底
  · 人脸检测：OpenCV 自带 Haar 级联（正脸 + 侧脸），人脸提供头部朝向线索
因此可以真实得到的信号只有：

  | 信号 | 摄像头路径 | 说明 |
  | --- | --- | --- |
  | 头部偏转 | 是 估计值 | 由人脸框相对人体框的水平偏移推算左右转头 |
  | 头部姿态方差 | 是 | 由上述序列在窗口内计算，非单帧量 |
  | 姿态重复性 | 是 估计值 | 由人体框宽高比/位置序列推算 |
  | 手部跨考生距离 | 否 不可得 | 需手部关键点，须接姿态模型（YOLO-pose 等） |
  | 违禁物体 | 否 不可得 | 需专用物体模型，建议接《06_智能安检与手机作弊器材检测》 |
  | 面部表情 | 否 不可得 | 需表情模型 |

也就是说：**摄像头路径能识别旁窥、坐姿异常、东张西望这类"头部与姿态"
模式，但无法识别传递物品与使用手机**。界面上会逐项标注哪些信号是
"实测"、哪些是"不可得"，绝不把缺失信号按 0 分混进判断而不加说明。

另外，yaw/pitch/tilt 均为**几何估计值**（非角度测量），需要按
《普通考场应用》"逐考场标定"的要求在真实机位上标定；本模块提供
自标定（对同一目标滚动建立基线），但标定值随机位变化。
"""

import math
import os
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from .features import FrameFeatures, HandState, HeadPose, PostureState

try:
    import cv2
    import numpy as np
    HAS_CV = True
except Exception:                                    # pragma: no cover
    cv2 = None
    np = None
    HAS_CV = False

try:
    import onnxruntime as _ort
    HAS_ORT = True
except Exception:                                    # pragma: no cover
    _ort = None
    HAS_ORT = False


# ================================================================
# 可用信号声明（界面据此逐项标注，避免误导）
# ================================================================
CAMERA_SIGNAL_AVAIL: Dict[str, str] = {
    "head_deviation": "estimate",     # 估计
    "head_variance": "measured",      # 由估计序列在窗口内计算
    "hand_interaction": "unavailable",
    "prohibited_object": "unavailable",
    "posture_repetition": "estimate",
    "expression_aux": "unavailable",
}

AVAIL_CN = {"measured": "实测", "estimate": "估计", "unavailable": "不可得"}


# ================================================================
# 模型/级联路径解析
# ================================================================
def _ascii_dir() -> Optional[str]:
    """返回一个纯 ASCII 且可写的目录（用于存放 OpenCV 级联副本）。

    为什么需要它：OpenCV 的 CascadeClassifier 使用窄字符路径 API，
    在用户名为中文（如 C:\\Users\\十年叶落\\...）时会**静默加载失败**
    （empty() 为真）。把级联复制到纯 ASCII 目录即可绕开。
    """
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    if not str(base).isascii():
        return None
    d = os.path.join(base, "cheatdetect", "cascades")
    try:
        os.makedirs(d, exist_ok=True)
        return d if os.access(d, os.W_OK) else None
    except Exception:
        return None


def cascade_path(name: str) -> Optional[str]:
    """取得可用的级联文件路径（必要时复制到 ASCII 目录）。"""
    if not HAS_CV:
        return None
    src = os.path.join(cv2.data.haarcascades, name)
    if not os.path.exists(src):
        return None
    if str(src).isascii():                 # 路径本身就是 ASCII，直接用
        return src
    cache = _ascii_dir()
    if cache:
        dst = os.path.join(cache, name)
        try:
            if (not os.path.exists(dst)
                    or os.path.getsize(dst) != os.path.getsize(src)):
                shutil.copy2(src, dst)
            return dst
        except Exception:
            pass
    # 兜底：进程级一次性 chdir 后用相对名加载（调用方在 __init__ 中同步执行）
    return None


def find_yolo_model(explicit: str = "") -> str:
    """定位 YOLO11n ONNX 模型（onnxruntime 支持中文路径，无需复制）。

    搜索顺序：显式指定 → 环境变量 EXAM_YOLO_MODEL → 本目录 models\\ →
    本工程其他模块 models\\。
    """
    cands: List[str] = []
    if explicit:
        cands.append(explicit)
    env = os.environ.get("EXAM_YOLO_MODEL", "")
    if env:
        cands.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(os.path.dirname(here), "models", "yolo11n.onnx"))
    cands.append(os.path.join(here, "models", "yolo11n.onnx"))
    # 本工程其他模块自带的同名模型
    home = os.path.expanduser("~")
    for rel in (
        r"Desktop\All\All\11_摄像头AI识别\models\yolo11n.onnx",
        r"Desktop\All\All\03_智能终端操作\models\yolo11n.onnx",
        r"Desktop\All\All\02_摄像头核查与视频质量诊断\models\yolo11n.onnx",
    ):
        cands.append(os.path.join(home, rel))
    for p in cands:
        if p and os.path.exists(p) and os.path.getsize(p) > 1024 * 1024:
            return p
    return ""


# ================================================================
# 人体检测
# ================================================================
def _letterbox(frame, new=640, color=(114, 114, 114)):
    """等比缩放 + 居中填充到 new x new，返回 (图, 缩放比, 左填充, 上填充)。"""
    h, w = frame.shape[:2]
    r = min(new / float(h), new / float(w))
    nw, nh = int(round(w * r)), int(round(h * r))
    if (nw, nh) != (w, h):
        interp = cv2.INTER_LINEAR if r > 1 else cv2.INTER_AREA
        frame = cv2.resize(frame, (nw, nh), interpolation=interp)
    canvas = np.full((new, new, 3), color, dtype=np.uint8)
    dw, dh = (new - nw) // 2, (new - nh) // 2
    canvas[dh:dh + nh, dw:dw + nw] = frame
    return canvas, r, dw, dh


def _nms(rects, scores, iou_thresh=0.45, max_keep=50):
    """纯 numpy NMS。rects 为 (N,4) 数组。"""
    if rects is None or len(rects) == 0:
        return []
    boxes = np.array(rects, dtype=np.float32)
    sc = np.array(scores, dtype=np.float32)
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 0] + boxes[:, 2], boxes[:, 1] + boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = sc.argsort()[::-1]
    keep = []
    while order.size > 0 and len(keep) < max_keep:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(1e-6, areas[i] + areas[rest] - inter)
        order = rest[iou <= iou_thresh]
    return keep


class PersonDetector:
    """人体检测器：YOLO11n ONNX 为主，HOG+SVM 兜底。"""

    def __init__(self, model_path: str = "", conf: float = 0.35,
                 iou: float = 0.45):
        self.conf = float(conf)
        self.iou = float(iou)
        self.session = None
        self.input_name = ""
        self.backend = "none"
        self.last_error = ""
        self._hog = None

        if HAS_ORT:
            path = find_yolo_model(model_path)
            if path:
                try:
                    so = _ort.SessionOptions()
                    so.log_severity_level = 3
                    self.session = _ort.InferenceSession(
                        path, so, providers=["CPUExecutionProvider"])
                    self.input_name = self.session.get_inputs()[0].name
                    self.backend = "YOLO11n-ONNX"
                    self.model_path = path
                except Exception:
                    self.session = None
        if self.session is None and HAS_CV:
            try:
                hog = cv2.HOGDescriptor()
                hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
                self._hog = hog
                self.backend = "HOG+SVM"
            except Exception:
                self._hog = None
        self.model_path = getattr(self, "model_path", "")

    # ---------------- 推理 ----------------
    def detect(self, frame) -> List[Tuple[int, int, int, int, float]]:
        """返回 [(x, y, w, h, score), ...]（原图坐标）。

        主检测器异常时**记录原因**并退到兜底检测器 —— 不静默吞掉错误，
        否则"检测不到人"会被误当成"画面里没人"。
        """
        if self.session is not None:
            try:
                return self._detect_yolo(frame)
            except Exception as exc:
                self.last_error = "{}: {}".format(type(exc).__name__, exc)
                self.backend = "HOG+SVM（YOLO 失败）"
        if self._hog is not None:
            try:
                return self._detect_hog(frame)
            except Exception as exc:
                self.last_error = "{}: {}".format(type(exc).__name__, exc)
        return []

    def _detect_yolo(self, frame):
        img, r, dw, dh = _letterbox(frame, 640)
        blob = img[:, :, ::-1].astype(np.float32) / 255.0     # BGR->RGB
        blob = np.ascontiguousarray(blob.transpose(2, 0, 1)[None])
        out = self.session.run(None, {self.input_name: blob})[0]
        pred = out[0].T                                        # (8400, 84)
        boxes = pred[:, :4]
        cls = pred[:, 4:]
        scores = cls.max(axis=1)
        labels = cls.argmax(axis=1)
        m = (scores >= self.conf) & (labels == 0)              # 只取 person
        if not m.any():
            return []
        b = boxes[m]
        s = scores[m]
        # cx,cy,w,h -> x,y,w,h（去 letterbox）
        cx, cy, bw, bh = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
        x = (cx - bw / 2.0 - dw) / r
        y = (cy - bh / 2.0 - dh) / r
        w = bw / r
        h = bh / r
        rects = np.stack([x, y, w, h], axis=1)
        idx = _nms(rects, s, self.iou)
        fh, fw = frame.shape[:2]
        out_rects = []
        for i in idx:
            xx, yy, ww, hh = rects[i]
            xx = max(0.0, min(float(xx), fw - 2.0))
            yy = max(0.0, min(float(yy), fh - 2.0))
            ww = max(2.0, min(float(ww), fw - xx))
            hh = max(2.0, min(float(hh), fh - yy))
            out_rects.append((int(xx), int(yy), int(ww), int(hh), float(s[i])))
        return out_rects

    def _detect_hog(self, frame):
        rects, weights = self._hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(8, 8), scale=1.05)
        out = []
        for (x, y, w, h), wt in zip(rects, weights):
            if wt >= 0.5:
                out.append((int(x), int(y), int(w), int(h), float(wt)))
        return out


# ================================================================
# 人脸检测（Haar 正脸 + 侧脸）
# ================================================================
class FaceDetector:
    """Haar 级联人脸检测；正脸与侧脸分别检测，用于推断头部左右偏转。

    加载策略（应对中文用户名下 OpenCV 窄字符路径失效）：
      1) 优先用 `cascade_path()` 复制到纯 ASCII 目录后的绝对路径；
      2) 若复制不可行，则**切到 cv2 数据目录、用相对名直接加载出分类器对象、
         再切回** —— 注意必须当场加载对象，因为切回后相对路径就失效了。
    """

    _NAMES = ("haarcascade_frontalface_default.xml",
              "haarcascade_profileface.xml",
              "haarcascade_eye.xml")

    def __init__(self):
        self.frontal = None
        self.profile = None
        self.eye = None
        self.load_note = ""
        if not HAS_CV:
            self.load_note = "未安装 OpenCV"
            return

        # —— 策略 1：ASCII 副本路径 ——
        for attr, name in (("frontal", self._NAMES[0]),
                           ("profile", self._NAMES[1]),
                           ("eye", self._NAMES[2])):
            p = cascade_path(name)
            if p:
                clf = self._load(p)
                if clf is not None:
                    setattr(self, attr, clf)
        if self.frontal is not None or self.profile is not None:
            self.load_note = "级联(ASCII 副本)"
            return

        # —— 策略 2：切目录用相对名当场加载 ——
        old = os.getcwd()
        try:
            os.chdir(cv2.data.haarcascades)
            for attr, name in (("frontal", self._NAMES[0]),
                               ("profile", self._NAMES[1]),
                               ("eye", self._NAMES[2])):
                clf = self._load(name)
                if clf is not None:
                    setattr(self, attr, clf)
            self.load_note = "级联(相对路径)"
        except Exception as exc:
            self.load_note = "级联加载失败：{}".format(type(exc).__name__)
        finally:
            try:
                os.chdir(old)
            except Exception:
                pass

    @staticmethod
    def _load(path):
        try:
            c = cv2.CascadeClassifier(path)
            return None if c.empty() else c
        except Exception:
            return None

    @property
    def ready(self) -> bool:
        return self.frontal is not None or self.profile is not None

    def detect(self, gray, roi: Optional[Tuple[int, int, int, int]] = None):
        """在 ROI（默认整幅）内检测人脸。

        Returns:
            (x, y, w, h, kind)，kind ∈ {"frontal", "profile"}；未检出返回 None。
        """
        if not self.ready:
            return None
        fh, fw = gray.shape[:2]
        if roi:
            rx, ry, rw, rh = roi
            rx = max(0, rx)
            ry = max(0, ry)
            rw = max(1, min(rw, fw - rx))
            rh = max(1, min(rh, fh - ry))
            sub = gray[ry:ry + rh, rx:rx + rw]
        else:
            rx = ry = 0
            sub = gray
        if sub.size == 0:
            return None
        msz = max(24, min(sub.shape[:2]) // 8)

        best = None
        if self.frontal is not None:
            faces = self.frontal.detectMultiScale(sub, 1.1, 5, minSize=(msz, msz))
            for (x, y, w, h) in faces:
                if best is None or w * h > best[2] * best[3]:
                    best = (x, y, w, h, "frontal")
        if best is None and self.profile is not None:
            faces = self.profile.detectMultiScale(sub, 1.1, 5, minSize=(msz, msz))
            for (x, y, w, h) in faces:
                if best is None or w * h > best[2] * best[3]:
                    best = (x, y, w, h, "profile")
        if best is None:
            return None
        return (best[0] + rx, best[1] + ry, best[2], best[3], best[4])


# ================================================================
# 感知配置与结果
# ================================================================
@dataclass
class PerceptionConfig:
    """感知层参数。角度类参数都是**估计用比例**，需按机位标定。"""

    model_path: str = ""              # 显式指定 YOLO onnx；空则自动搜索
    person_conf: float = 0.35
    yaw_full_deg: float = 60.0        # 人脸水平偏移满量程对应的估计偏转角
    yaw_off_threshold_deg: float = 30.0   # 与引擎阈值保持一致，用于判 relation
    tilt_full: float = 0.45           # 宽高比相对偏差满量程
    tilt_max_deg: float = 45.0
    lean_height_ratio: float = 0.72   # 人体框高度低于基线的该比例 → 视为趴桌
    leave_center_ratio: float = 0.22  # 质心横向偏离基线超过画面宽度该比例 → 离位
    baseline_alpha: float = 0.05      # 基线指数滑动系数
    baseline_warmup: int = 25         # 前 N 帧用于建立基线
    target_index: int = -1            # -1 = 自动选最大；>=0 = 固定第几大的人


@dataclass
class TargetInfo:
    """本帧感知结果说明（供界面显示与排错）。"""

    ok: bool = False
    reason: str = ""
    rect: Optional[Tuple[int, int, int, int]] = None
    score: float = 0.0
    face: Optional[Tuple[int, int, int, int]] = None
    face_kind: str = ""
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    relation: str = "on_paper"
    tilt_deg: float = 0.0
    leaning: bool = False
    left_area: bool = False
    baselines: Dict[str, float] = field(default_factory=dict)
    signals: Dict[str, str] = field(default_factory=lambda: dict(CAMERA_SIGNAL_AVAIL))


# ================================================================
# 逐帧感知
# ================================================================
class FramePerceiver:
    """把一帧画面转成引擎所需的 FrameFeatures。"""

    def __init__(self, cfg: Optional[PerceptionConfig] = None):
        self.cfg = cfg or PerceptionConfig()
        self.person = PersonDetector(self.cfg.model_path, self.cfg.person_conf)
        self.face = FaceDetector()
        self._base_ratio = None      # 人脸高/人体高
        self._base_aspect = None     # 人体框宽/高
        self._base_cx = None         # 人体框质心 x（归一化）
        self._base_h = None          # 人体框高
        self._n = 0
        self._prev_rect = None
        self._face_hist = deque(maxlen=20)   # 近若干帧人脸检出情况
        self._last_yaw_sign = 1.0

    # ---------------- 能力自述 ----------------
    def backend_desc(self) -> str:
        parts = ["人体:" + self.person.backend]
        parts.append("人脸:" + ("Haar级联" if self.face.ready else "不可用"))
        return " · ".join(parts)

    def reset(self) -> None:
        self._base_ratio = self._base_aspect = None
        self._base_cx = self._base_h = None
        self._n = 0
        self._prev_rect = None
        self._face_hist.clear()
        self._last_yaw_sign = 1.0

    # ---------------- 主流程 ----------------
    def process(self, frame, frame_id: int = 0,
                timestamp: float = 0.0) -> Tuple[Optional[FrameFeatures],
                                                 TargetInfo]:
        """处理一帧。

        Returns:
            (FrameFeatures 或 None, TargetInfo)。
            未检出考生时 FrameFeatures 为 None，界面应显示"未检出"，
            而不是喂一个全 0 的假帧进引擎。
        """
        info = TargetInfo()
        if not HAS_CV:
            info.reason = "未安装 OpenCV"
            return None, info

        fh, fw = frame.shape[:2]
        persons = self.person.detect(frame)
        if not persons:
            info.reason = "未检出考生（人体检测）"
            self._decay()
            return None, info

        # —— 目标选择：优先沿用上一帧的目标，否则取最大 ——
        target = self._pick_target(persons)
        x, y, w, h, score = target
        # 上游检测器可能给出越界框（不同实现/自定义模型），此处统一夹回画面内，
        # 否则后续 ROI 裁剪与人脸偏移计算都会失真。
        x = max(0, min(int(x), fw - 2))
        y = max(0, min(int(y), fh - 2))
        w = max(2, min(int(w), fw - x))
        h = max(2, min(int(h), fh - y))
        info.ok = True
        info.rect = (x, y, w, h)
        info.score = float(score)

        # —— 人脸（限定在人体框上部，减少误检与耗时）——
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        roi = (x, y, w, int(max(1, h * 0.6)))
        face = self.face.detect(gray, roi)
        if face is not None:
            info.face = (int(face[0]), int(face[1]), int(face[2]), int(face[3]))
            info.face_kind = str(face[4]) if len(face) > 4 else ""

        # —— 几何量 ——
        pcx = x + w / 2.0
        fh_ratio = (info.face[3] / float(h)) if info.face else None
        aspect = w / float(max(1, h))
        cx_norm = pcx / float(max(1, fw))

        self._n += 1
        warm = self._n <= max(1, self.cfg.baseline_warmup)
        # 基线只在"看起来正常"时更新：正脸、水平居中、未见明显俯仰
        stable = (info.face is not None and info.face_kind == "frontal")
        self._update_baseline(fh_ratio, aspect, cx_norm, h, warm, stable)

        # 头部左右偏转：人脸框相对人体框中心的水平偏移
        self._face_hist.append(1 if info.face else 0)
        face_rate = sum(self._face_hist) / float(len(self._face_hist))
        info.face_rate = face_rate

        if info.face:
            fcx = info.face[0] + info.face[2] / 2.0
            dx = (fcx - pcx) / max(1.0, w / 2.0)
            dx = max(-1.0, min(1.0, dx))
            yaw = dx * self.cfg.yaw_full_deg
            info.head_known = True
            if abs(dx) > 0.08:
                self._last_yaw_sign = 1.0 if dx > 0 else -1.0
        elif face_rate >= 0.6:
            # 此前人脸稳定可见、本帧突然检不到 —— 这是"转头离开"的**上下文证据**，
            # 可以合理推断头部已偏离；方向沿用最近一次已知的偏转方向。
            yaw = self._last_yaw_sign * self.cfg.yaw_full_deg * 0.55
            info.head_known = True
        else:
            # 人脸一直检不到（距离远、逆光、背对、或本就不是面向镜头的考生）：
            # **不能**推断为转头 —— 这是检测能力边界，不是观测到异常。
            # 保持中性，不产生任何头部信号，避免系统性误报。
            yaw = 0.0
            info.head_known = False
        info.yaw_deg = yaw

        # 头部俯仰：以"人脸占人体框高比例"相对基线判断
        # 比例明显偏小 → 低头看自己试卷（正常，按 on_paper 处理，不计分）
        # 比例明显偏大 → 抬头张望（可疑）
        pitch = 0.0
        if fh_ratio is not None and self._base_ratio:
            rel = (fh_ratio - self._base_ratio) / max(1e-6, self._base_ratio)
            if rel > 0.18:
                pitch = min(60.0, rel * 90.0)
        info.pitch_deg = pitch

        # —— relation：与引擎语义对齐 ——
        #   on_paper     = 低头看自己试卷/屏幕（正常，头部偏转不计分）
        #   off_area     = 抬头张望（偏离考试区域）
        #   toward_*     = 明显左右转头
        abs_yaw = abs(yaw)
        if not info.head_known:
            relation = "unknown"      # 朝向不可判：引擎按严重度 0 处理，不产生头部信号
        elif abs_yaw >= self.cfg.yaw_off_threshold_deg * 1.35:
            relation = "toward_neighbor"
        elif abs_yaw >= self.cfg.yaw_off_threshold_deg or pitch > 25.0:
            relation = "off_area"
        else:
            relation = "on_paper"
        info.relation = relation

        # —— 坐姿 ——
        tilt = 0.0
        if self._base_aspect:
            dev = (aspect - self._base_aspect) / max(1e-6, self._base_aspect)
            tilt = max(-1.0, min(1.0, dev / self.cfg.tilt_full)) \
                * self.cfg.tilt_max_deg
        info.tilt_deg = tilt

        leaning = bool(self._base_h and h < self._base_h
                       * self.cfg.lean_height_ratio)
        info.leaning = leaning
        left_area = bool(self._base_cx is not None
                         and abs(cx_norm - self._base_cx) > self.cfg.leave_center_ratio)
        info.left_area = left_area

        info.baselines = {
            "face_h_ratio": self._base_ratio or 0.0,
            "aspect": self._base_aspect or 0.0,
            "center_x": self._base_cx or 0.0,
            "height": self._base_h or 0.0,
        }

        feats = FrameFeatures(
            frame_id=frame_id,
            timestamp=timestamp,
            head=HeadPose(yaw_deg=yaw, pitch_deg=pitch, relation=relation),
            # 以下三项摄像头路径不可得：保持中性值，并已在 info.signals 中标注
            hand=HandState(below_desk=False, cross_distance=None,
                           doing_write=True),
            posture=PostureState(tilt_deg=tilt, leaning_on_desk=leaning,
                                 left_area=left_area),
            prohibited_objects=[],
            expression_tense=False,
        )
        return feats, info

    # ---------------- 内部 ----------------
    def _pick_target(self, persons):
        """目标选择：优先与上一帧重叠度最高的，否则取面积最大的。"""
        persons = sorted(persons, key=lambda r: r[2] * r[3], reverse=True)
        if self.cfg.target_index >= 0:
            i = min(self.cfg.target_index, len(persons) - 1)
            self._prev_rect = persons[i]
            return persons[i]
        if self._prev_rect is not None:
            px, py, pw, ph = self._prev_rect[:4]
            best, best_iou = None, 0.0
            for r in persons:
                iou = _iou(px, py, pw, ph, r[0], r[1], r[2], r[3])
                if iou > best_iou:
                    best, best_iou = r, iou
            if best is not None and best_iou > 0.25:
                self._prev_rect = best
                return best
        self._prev_rect = persons[0]
        return persons[0]

    def _update_baseline(self, fh_ratio, aspect, cx_norm, h, warm, stable):
        """滚动建立"正常坐姿"基线（只用稳定帧更新，避免把异常写成正常）。"""
        a = 1.0 if self._base_ratio is None else self.cfg.baseline_alpha

        def ema(old, new):
            return new if old is None else old * (1 - a) + new * a

        if warm or stable:
            if fh_ratio is not None:
                self._base_ratio = ema(self._base_ratio, fh_ratio)
        if warm or stable:
            self._base_aspect = ema(self._base_aspect, aspect)
            self._base_cx = ema(self._base_cx, cx_norm)
            self._base_h = ema(self._base_h, h)

    def _decay(self):
        """未检出目标时缓慢衰减，避免长时间丢失后基线彻底失效。"""
        self._prev_rect = None

    # ---------------- 画面叠加 ----------------
    def draw_overlay(self, frame, info: TargetInfo, result=None):
        """在画面上绘制检测框与估计值，便于现场核对（不改变任何判定）。"""
        if not HAS_CV or frame is None:
            return frame
        col_target = (0, 0, 220) if (result is not None and result.triggered) \
            else (200, 120, 30)
        if info.rect:
            x, y, w, h = info.rect
            cv2.rectangle(frame, (x, y), (x + w, y + h), col_target, 2)
            label = "#target {:.2f}".format(info.score)
            cv2.putText(frame, label, (x, max(14, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col_target, 1,
                        cv2.LINE_AA)
        if info.face:
            fx, fy, fw, fh = info.face
            cv2.rectangle(frame, (fx, fy), (fx + fw, fy + fh), (60, 180, 60), 2)
            cv2.putText(frame, info.face_kind, (fx, max(14, fy - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (60, 180, 60), 1,
                        cv2.LINE_AA)
        txt = "yaw~{:.0f} pitch~{:.0f} tilt~{:.0f} {}".format(
            info.yaw_deg, info.pitch_deg, info.tilt_deg, info.relation)
        cv2.putText(frame, txt, (8, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2,
                    cv2.LINE_AA)
        cv2.putText(frame, txt, (8, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 1,
                    cv2.LINE_AA)
        if result is not None:
            risk_txt = "risk {:.1f} {}".format(result.risk, result.level)
            cv2.putText(frame, risk_txt, (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
                        cv2.LINE_AA)
            cv2.putText(frame, risk_txt, (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, col_target, 1,
                        cv2.LINE_AA)
        return frame


def _iou(ax, ay, aw, ah, bx, by, bw, bh) -> float:
    """两个 (x,y,w,h) 的 IoU。"""
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix = max(0.0, min(ax2, bx2) - max(ax, bx))
    iy = max(0.0, min(ay2, by2) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0
