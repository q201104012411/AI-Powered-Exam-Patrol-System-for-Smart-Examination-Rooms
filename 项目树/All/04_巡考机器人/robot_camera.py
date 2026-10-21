# -*- coding: utf-8 -*-
r"""
robot_camera.py —— 巡考小车摄像头画面模块（Windows 端）

小车端 ``tools/snapshot_server.py`` 把三路画面写成 /tmp/snap_*.jpg，
``webui/robot_web.py`` 的 ``/api/snap?cam=astra|depth|usb`` 按需返回 JPEG。
本模块做三件事：

  1. ``CameraWorker``：后台线程按设定帧率轮询快照，主线程永不卡顿；
  2. ``decode_photo``：JPEG → Tk 可显示的 PhotoImage（需要 Pillow，
     打包运行时 runtime/ 已自带 Pillow；没有时自动降级为"浏览器查看"）；
  3. ``save_jpeg``：把当前画面存到本机（默认 04_巡考机器人/data/captures）。

注意：Pillow 只在主线程创建 PhotoImage，所以 decode 由 GUI 调用。
"""
from __future__ import annotations

import io
import os
import threading
import time

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:                                            # noqa: BLE001
    Image = None
    ImageTk = None
    HAS_PIL = False

CAMERAS = {
    "astra": "Astra RGB",
    "depth": "Astra 深度",
    "usb": "USB 广角",
}

CAMERA_NOTES = {
    "astra": "Astra RGB：寻路 / 巡线主画面（/camera/rgb/image_raw）",
    "depth": "Astra 深度：伪彩色，越亮越远（0.3~3 m）",
    "usb": "USB 广角 800×480：拍摄 / 巡检画面（/usb_cam/image_raw）",
}


class CameraError(Exception):
    pass


class CameraWorker(threading.Thread):
    """后台抓图线程：只拉 /api/snap，不解析图像。"""

    def __init__(self, link, cam="astra", fps=10.0):
        threading.Thread.__init__(self, name="robot-camera")
        self.daemon = True
        self.link = link
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cam = cam if cam in CAMERAS else "astra"
        self._fps = min(20.0, max(0.5, float(fps)))
        self._jpeg = b""
        self._seq = 0
        self._ts = 0.0
        self._error = ""
        self._error_ts = 0.0
        self._frames = 0

    # ---------------------------------------------------------- 控制
    def set_cam(self, cam):
        if cam not in CAMERAS:
            return
        with self._lock:
            if cam == self._cam:
                return
            self._cam = cam
            self._jpeg = b""
            self._seq += 1
            self._error = ""
        self._wake.set()

    def cam(self):
        with self._lock:
            return self._cam

    def set_fps(self, fps):
        with self._lock:
            self._fps = min(20.0, max(0.5, float(fps)))
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    # ---------------------------------------------------------- 读取
    def read(self):
        with self._lock:
            return {
                "seq": self._seq,
                "jpeg": self._jpeg,
                "ts": self._ts,
                "error": self._error,
                "error_ts": self._error_ts,
                "cam": self._cam,
                "frames": self._frames,
                "fps": self._fps,
            }

    # ---------------------------------------------------------- 线程
    def run(self):
        while not self._stop.is_set():
            with self._lock:
                cam = self._cam
                fps = self._fps
            try:
                data = self.link.snap(cam)
                if data:
                    with self._lock:
                        self._jpeg = data
                        self._ts = time.time()
                        self._seq += 1
                        self._frames += 1
                        self._error = ""
                else:
                    raise CameraError("画面为空")
            except Exception as exc:                         # noqa: BLE001
                with self._lock:
                    self._error = _short_error(exc)
                    self._error_ts = time.time()
                    self._jpeg = b""
                    self._seq += 1
                self._stop.wait(1.2)
                continue
            self._wake.wait(1.0 / max(0.5, fps))
            self._wake.clear()


def _short_error(exc):
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 160:
        text = text[:157] + "..."
    return text


# ======================================================================
# 图像工具（主线程调用）
# ======================================================================
def decode_photo(jpeg, max_w=760, max_h=520, master=None):
    """JPEG 字节 → (PhotoImage, (宽, 高))；无 Pillow 或坏图返回 (None, None)。"""
    if not HAS_PIL or not jpeg:
        return None, None
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        if max_w > 0 and max_h > 0:
            img.thumbnail((int(max_w), int(max_h)), resample)
        photo = ImageTk.PhotoImage(img, master=master)
        return photo, img.size
    except Exception:                                        # noqa: BLE001
        return None, None


def image_size(jpeg):
    """不解码整张图，读 JPEG 尺寸（Pillow 可用时）。"""
    if not HAS_PIL or not jpeg:
        return None
    try:
        img = Image.open(io.BytesIO(jpeg))
        return img.size
    except Exception:                                        # noqa: BLE001
        return None


def save_jpeg(jpeg, folder, cam="astra", note=""):
    """把画面存成本机 JPEG，返回绝对路径。"""
    if not jpeg:
        raise CameraError("当前没有画面可保存")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise CameraError("创建目录失败：%s" % exc)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(ch for ch in (note or "") if ch not in '\\/:*?"<>|').strip()
    name = "%s_%s%s.jpg" % (stamp, cam, ("_" + safe) if safe else "")
    path = os.path.join(folder, name)
    i = 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s_%s%s_%d.jpg"
                            % (stamp, cam, ("_" + safe) if safe else "", i))
        i += 1
    try:
        with open(path, "wb") as f:
            f.write(jpeg)
    except OSError as exc:
        raise CameraError("写入失败：%s" % exc)
    return path


def decode_image(jpeg):
    """JPEG → PIL.Image（预览/另存用）；无 Pillow 返回 None。"""
    if not HAS_PIL or not jpeg:
        return None
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
        return img
    except Exception:                                        # noqa: BLE001
        return None


def availability_text():
    if HAS_PIL:
        return "内嵌画面可用（Pillow 已加载）"
    return "未检测到 Pillow：画面请在浏览器查看（python -m pip install pillow）"
