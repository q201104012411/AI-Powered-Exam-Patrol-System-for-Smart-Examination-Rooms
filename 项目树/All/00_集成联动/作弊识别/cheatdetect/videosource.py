# -*- coding: utf-8 -*-
"""视频帧源：合成场景 / 摄像头 / 视频文件 / 网络流。

本模块把「帧从哪来」这一件事单独抽出来，让上层（界面、感知层、引擎）
不必关心数据来源。摄像头与文件的实现依赖 OpenCV；**未安装 OpenCV 时
本模块仍可导入**（`HAS_OPENCV=False`），合成场景不受影响，界面会给出
明确提示而不是崩溃。
"""

import hashlib
import os
import shutil
from typing import List, Optional, Tuple

try:
    import cv2
    HAS_OPENCV = True
except Exception:                                    # pragma: no cover
    cv2 = None
    HAS_OPENCV = False


def ascii_staging_dir() -> Optional[str]:
    """返回一个纯 ASCII 且可写的暂存目录。

    为什么需要：OpenCV 的 VideoCapture / imread / CascadeClassifier 在
    Windows 上走窄字符路径 API，遇到中文用户名（如 C:\\Users\\十年叶落\\）
    会**直接失败**（表现为"打不开文件"）。把文件暂存到纯 ASCII 目录
    即可绕开。ProgramData 通常是纯 ASCII 且普通用户可写。
    """
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    if not str(base).isascii():
        return None
    d = os.path.join(base, "cheatdetect", "staging")
    try:
        os.makedirs(d, exist_ok=True)
        return d if os.access(d, os.W_OK) else None
    except Exception:
        return None


def ensure_ascii_media(path: str) -> Tuple[Optional[str], str]:
    """保证给出一个 OpenCV 能打开的路径。

    Returns:
        (可用路径 或 None, 说明文本)。路径本来就是 ASCII 时直接原样返回。
    """
    if not path:
        return None, "空路径"
    if str(path).isascii() and os.path.exists(path):
        return path, ""
    if not os.path.exists(path):
        return None, "文件不存在：{}".format(path)
    stage = ascii_staging_dir()
    if not stage:
        return None, ("路径含非 ASCII 字符，且找不到可用的 ASCII 暂存目录，"
                      "OpenCV 无法打开该文件")
    ext = os.path.splitext(path)[1].lower()
    key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    dst = os.path.join(stage, key + ext)
    try:
        if (not os.path.exists(dst)
                or os.path.getsize(dst) != os.path.getsize(path)):
            shutil.copy2(path, dst)
        return dst, "已暂存到 ASCII 路径以避开 OpenCV 的中文路径限制"
    except Exception as exc:
        return None, "暂存失败：{}".format(type(exc).__name__)


def imread_any(path: str):
    """读取图片，兼容中文路径（cv2.imread 在中文路径下会失败）。"""
    if not HAS_OPENCV:
        return None
    if str(path).isascii():
        return cv2.imread(path)
    try:
        import numpy as _np
        buf = _np.fromfile(path, dtype=_np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        p, _ = ensure_ascii_media(path)
        return cv2.imread(p) if p else None


def imwrite_any(path: str, image) -> bool:
    """保存图片，兼容中文路径。"""
    if not HAS_OPENCV or image is None:
        return False
    if str(path).isascii():
        return bool(cv2.imwrite(path, image))
    try:
        import numpy as _np
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, image)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        return False

# 视频源类型
KIND_SYNTHETIC = "synthetic"      # 合成场景
KIND_CAMERA = "camera"            # 本机摄像头
KIND_FILE = "file"                # 本地视频文件
KIND_STREAM = "stream"            # 网络流（rtsp/http）


class VideoSource:
    """帧源基类。"""

    kind = "base"

    def open(self) -> bool:
        raise NotImplementedError

    def read(self) -> Tuple[bool, object]:
        raise NotImplementedError

    def release(self) -> None:
        pass

    def describe(self) -> str:
        return self.kind

    @property
    def is_open(self) -> bool:
        return False


class OpenCVSource(VideoSource):
    """基于 OpenCV 的帧源：摄像头序号 / 视频文件 / 网络流地址。

    Args:
        target: 摄像头序号（int）或文件/流地址（str）。
        width/height: 期望分辨率（摄像头有效，用于降低解码与推理负担）。
        fps: 期望帧率。
        prefer_dshow: Windows 上优先用 DirectShow 打开摄像头，
            实测比默认后端打开更快、更稳。
    """

    kind = KIND_CAMERA

    def __init__(self, target, width: int = 640, height: int = 480,
                 fps: int = 30, prefer_dshow: bool = True):
        self.target = target
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.prefer_dshow = prefer_dshow
        self._cap = None
        self._backend = ""
        self._staged = ""
        self._open_error = ""

        if isinstance(target, int):
            self.kind = KIND_CAMERA
        elif isinstance(target, str) and target.lower().startswith(
                ("rtsp://", "rtmp://", "http://", "https://", "udp://")):
            self.kind = KIND_STREAM
        else:
            self.kind = KIND_FILE

    # ---------------- 打开 ----------------
    def open(self) -> bool:
        if not HAS_OPENCV:
            raise RuntimeError(
                "未安装 OpenCV，无法打开摄像头/视频文件。"
                "请先执行 pip install opencv-python")
        if self._cap is not None:
            self.release()

        if self.kind == KIND_CAMERA:
            cap, backend = self._open_camera(int(self.target))
        else:
            # 视频文件可能位于中文路径下，OpenCV 打不开 —— 先暂存到 ASCII 目录
            use_path = str(self.target)
            if self.kind == KIND_FILE:
                staged, note = ensure_ascii_media(use_path)
                if staged is None:
                    self._open_error = note
                    return False
                self._staged = staged
                use_path = staged
            cap = cv2.VideoCapture(use_path)
            backend = "default"
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            self._cap = None
            return False
        self._open_error = ""

        self._backend = backend
        if self.kind == KIND_CAMERA:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            try:                       # 只留最新帧，避免延迟堆积
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        self._cap = cap
        return True

    def _open_camera(self, index: int):
        """依次尝试 DirectShow 与默认后端。"""
        if self.prefer_dshow and hasattr(cv2, "CAP_DSHOW"):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if cap is not None and cap.isOpened():
                return cap, "DSHOW"
            if cap is not None:
                cap.release()
        cap = cv2.VideoCapture(index)
        if cap is not None and cap.isOpened():
            return cap, "default"
        return cap, "none"

    # ---------------- 读取 ----------------
    def read(self):
        if self._cap is None:
            return False, None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return False, None
        return True, frame

    def release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def describe(self) -> str:
        if self.kind == KIND_CAMERA:
            return "摄像头 #{}（{}）".format(self.target, self._backend or "-")
        if self.kind == KIND_STREAM:
            return "网络流 {}".format(self.target)
        extra = "（经 ASCII 暂存）" if self._staged else ""
        return "视频文件 {}{}".format(os.path.basename(str(self.target)), extra)

    def actual_size(self) -> Tuple[int, int]:
        """实际分辨率 (宽, 高)。"""
        if self._cap is None:
            return (0, 0)
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return (w, h)


def list_cameras(max_index: int = 5) -> List[int]:
    """探测本机可用摄像头序号（打开成功即视为可用）。"""
    if not HAS_OPENCV:
        return []
    found = []
    for i in range(max(1, max_index)):
        cap = None
        try:
            if hasattr(cv2, "CAP_DSHOW"):
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            else:
                cap = cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                found.append(i)
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    return found


def make_source(kind: str, target=None, width: int = 640, height: int = 480,
                fps: int = 30) -> VideoSource:
    """按类型构造帧源。

    Args:
        kind: KIND_CAMERA / KIND_FILE / KIND_STREAM。
        target: 摄像头序号，或文件路径/流地址。

    Raises:
        ValueError: 类型不支持或参数缺失。
    """
    if kind == KIND_CAMERA:
        if target is None:
            target = 0
        return OpenCVSource(int(target), width, height, fps)
    if kind in (KIND_FILE, KIND_STREAM):
        if not target:
            raise ValueError("未指定视频文件或网络流地址")
        return OpenCVSource(str(target), width, height, fps)
    raise ValueError("不支持的视频源类型：{}".format(kind))
