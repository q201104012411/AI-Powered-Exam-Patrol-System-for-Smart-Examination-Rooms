# -*- coding: utf-8 -*-
"""
摄像头实时抓拍：为核验提供"现场图像 + 现场令牌"，替代"手动选择一张图片文件"。

旧版的漏洞：现场照是一个文件路径，把参考照本身当现场照传进去也能通过（相似度 1.000）。
严格模式下，只有本模块产出的（图像 + 令牌）组合才可能被判定为现场采集。
"""
import os
import time
from typing import List, Optional, Tuple

import config
import liveness


class CaptureError(RuntimeError):
    pass


def capture_frames(n=5, warmup=10, camera_index=0, interval=0.06,
                   device_id="GATE-A-01") -> Tuple[List, dict]:
    """
    连续抓取 n 帧（丢掉 warmup 帧等待自动曝光），返回 (frames, token)。
    frames 为 OpenCV BGR 数组列表。
    """
    import cv2
    cap = cv2.VideoCapture(int(camera_index))
    if not cap.isOpened():
        raise CaptureError("无法打开摄像头（index=%s），请检查设备占用或权限" % camera_index)
    frames = []
    try:
        for _ in range(max(0, int(warmup))):          # 预热，等自动曝光稳定
            cap.read()
        for _ in range(max(1, int(n))):
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
            time.sleep(interval)
    finally:
        cap.release()

    if not frames:
        raise CaptureError("未采集到任何画面")
    token = liveness.make_token(device_id, source="camera")
    return frames, token


def best_frame(frames):
    """从多帧中挑最清晰的一帧（拉普拉斯方差最大）"""
    from face_engine import sharpness
    best, best_score = frames[0], -1.0
    for f in frames:
        try:
            s = sharpness(f)
        except Exception:
            s = 0.0
        if s > best_score:
            best, best_score = f, s
    return best, best_score


def save_capture(frame, device_id="GATE-A-01", directory=None) -> str:
    """把现场抓拍留档（用于人工复核与审计），文件名含设备与时间戳"""
    from face_engine import imwrite_unicode
    d = str(directory or config.CAPTURE_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise CaptureError("留档目录创建失败：%s" % e)
    name = "%s_%s_%03d.jpg" % (device_id, time.strftime("%Y%m%d_%H%M%S"),
                               int(time.time() * 1000) % 1000)
    path = os.path.join(d, name)
    if not imwrite_unicode(path, frame):
        raise CaptureError("抓拍留档写入失败：%s" % path)
    return path


def capture_best(n=5, warmup=10, camera_index=0, device_id="GATE-A-01",
                 save=True) -> Tuple[object, dict, Optional[str]]:
    """一步到位：抓拍 -> 选最清晰帧 -> 可选留档。返回 (frame, token, saved_path)。

    留档失败**不阻断核验**：现场图与令牌照常返回，错误写进 token["save_error"]，
    由界面提示。这样即使磁盘/权限异常，也不至于"抓拍功能无法使用"。
    """
    frames, token = capture_frames(n=n, warmup=warmup, camera_index=camera_index,
                                   device_id=device_id)
    frame, score = best_frame(frames)
    token["sharpness"] = round(float(score), 2)
    token["frame_count"] = len(frames)
    path = None
    if save:
        try:
            path = save_capture(frame, device_id=device_id)
        except Exception as e:
            token["save_error"] = str(e)      # 留档失败不阻断核验，只记入令牌
    return frame, token, path
