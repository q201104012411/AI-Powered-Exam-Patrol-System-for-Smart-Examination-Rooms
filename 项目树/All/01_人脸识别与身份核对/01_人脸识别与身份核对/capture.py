# -*- coding: utf-8 -*-
"""
摄像头实时抓拍：为核验提供"现场图像 + 现场令牌"，替代"手动选择一张图片文件"。

旧版的漏洞：现场照是一个文件路径，把参考照本身当现场照传进去也能通过（相似度 1.000）。
严格模式下，只有本模块产出的（图像 + 令牌）组合才可能被判定为现场采集。

本次加固（针对"抓拍把界面卡死"）：
  · 打开摄像头带超时（camera_stream.open_capture，后台线程 + 超时）；
  · 每次 read 带超时：read 卡住时不再无限等待，**拿不到帧就明确报错**，
    超时上限默认 6 秒，界面线程只是等待这个上限，不会永久卡死；
  · 释放句柄走后台线程（卡住的流 release 也会阻塞）。
"""
import os
import queue as _queue
import threading
import time
from typing import List, Optional, Tuple

import camera_stream
import config
import liveness


class CaptureError(RuntimeError):
    pass


CAPTURE_TIMEOUT_SEC = 6.0        # 一次抓拍的总超时（够本机摄像头预热，又不会把界面挂住）


def capture_frames(n=5, warmup=10, camera_index=0, interval=0.06,
                   device_id="GATE-A-01", timeout_sec=CAPTURE_TIMEOUT_SEC,
                   capture_factory=None) -> Tuple[List, dict]:
    """
    连续抓取 n 帧（丢掉 warmup 帧等待自动曝光），返回 (frames, token)。
    frames 为 OpenCV BGR 数组列表。

    卡死防护：整段抓拍有 `timeout_sec` 上限。超时时**有帧就用已有的帧**（不阻断核验），
    一帧都没有才抛 CaptureError；令牌里会写明 timeout / 已抓帧数，便于事后追查。
    读帧在一个**专用读帧线程**里连续进行（不是每帧新建线程），卡住时只放弃等待。
    """
    deadline = time.time() + max(1.0, float(timeout_sec))
    token = liveness.make_token(device_id, source="camera")
    token["timeout_sec"] = round(float(timeout_sec), 1)

    cap, why = camera_stream.open_capture(
        int(camera_index), timeout=min(4.0, max(1.0, timeout_sec * 0.6)),
        capture_factory=capture_factory)
    if cap is None:
        raise CaptureError("无法打开摄像头（index=%s）：%s"
                           % (camera_index, why or "设备占用或无权限"))

    frames = []
    timed_out = False
    q = _queue.Queue()
    stop_evt = threading.Event()

    def reader():
        while not stop_evt.is_set():
            try:
                ok, frame = cap.read()
            except Exception:                       # noqa: BLE001
                break
            if ok and frame is not None:
                q.put(frame)
            else:
                stop_evt.wait(0.05)                 # 取不到帧：稍等再试，别空转烧 CPU

    reader_thread = threading.Thread(target=reader, name="cap-reader", daemon=True)
    reader_thread.start()
    try:
        need = max(0, int(warmup)) + max(1, int(n))
        got = 0
        while got < need:
            if time.time() > deadline:
                timed_out = True
                break
            try:
                frame = q.get(timeout=0.2)
            except _queue.Empty:
                continue
            got += 1
            if got <= max(0, int(warmup)):
                continue                                # 预热帧丢弃
            frames.append(frame)
            if interval:
                time.sleep(max(0.0, float(interval)))
    finally:
        stop_evt.set()
        # 读帧线程可能卡在 cap.read() 里：不 join（daemon，进程退出自然结束）
        camera_stream.release_async(cap, "抓拍结束")

    token["frame_count"] = len(frames)
    if timed_out:
        token["timeout"] = True
    if not frames:
        raise CaptureError("未采集到任何画面（%.0f 秒内读取超时，请检查摄像头是否被占用或掉线）"
                           % float(timeout_sec))
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
                 save=True, timeout_sec=CAPTURE_TIMEOUT_SEC) -> Tuple[object, dict, Optional[str]]:
    """一步到位：抓拍 -> 选最清晰帧 -> 可选留档。返回 (frame, token, saved_path)。

    留档失败**不阻断核验**：现场图与令牌照常返回，错误写进 token["save_error"]，
    由界面提示。这样即使磁盘/权限异常，也不至于"抓拍功能无法使用"。
    """
    frame, _frames, token, path = capture_best_multi(
        n=n, warmup=warmup, camera_index=camera_index, device_id=device_id,
        save=save, timeout_sec=timeout_sec)
    return frame, token, path


def capture_best_multi(n=5, warmup=10, camera_index=0, device_id="GATE-A-01",
                       save=True, timeout_sec=CAPTURE_TIMEOUT_SEC):
    """同 capture_best，但**保留全部连拍帧**：返回 (最清晰帧, 全部帧, token, 留档路径)。

    保留多帧是为了让核验器做"多帧特征融合"——单帧可能恰好模糊/闭眼/侧脸，
    融合后能显著降低"本人被判不通过"的概率。
    """
    frames, token = capture_frames(n=n, warmup=warmup, camera_index=camera_index,
                                   device_id=device_id, timeout_sec=timeout_sec)
    frame, score = best_frame(frames)
    token["sharpness"] = round(float(score), 2)
    token["frame_count"] = len(frames)
    token["kept_frames"] = len(frames)
    path = None
    if save:
        try:
            path = save_capture(frame, device_id=device_id)
        except Exception as e:
            token["save_error"] = str(e)      # 留档失败不阻断核验，只记入令牌
    return frame, frames, token, path
