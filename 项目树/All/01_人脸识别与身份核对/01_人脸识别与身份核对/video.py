# -*- coding: utf-8 -*-
"""
实时视频流：给 GUI 提供"边看边核验"的现场画面。

设计要点：
  · 采集在**独立线程**里跑，界面线程只取最新帧，绝不阻塞（旧版一抓拍就卡界面）；
  · 只保留最新一帧（丢帧策略），所以即使推理很慢，画面也不会越积越延迟；
  · 实时统计采集 FPS（滚动平均），供界面显示；
  · 采集不到摄像头时**不抛异常**，返回明确错误信息，界面回退到"选择图片"通道；
  · 现场性（防翻拍）不受影响：核验用的是采集线程刚取到的真实相机帧 + 现场令牌。

可注入 capture_factory 以便在无摄像头的机器上做自动化测试。
"""
import threading
import time
from collections import deque

import config


class VideoStream:
    def __init__(self, camera_index=0, width=640, height=480, capture_factory=None,
                 fps_window=240):
        self.camera_index = int(camera_index)
        self.width = int(width)
        self.height = int(height)
        self.fps_window = fps_window
        self._capture_factory = capture_factory
        self._cap = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame = None                 # 最新一帧（BGR）
        self._seq = 0                      # 帧序号（用于判断是否有新帧）
        self._frames = 0                   # 累计帧数
        self._failed = 0                   # 读取失败次数
        self._times = deque(maxlen=fps_window)
        self.error = ""
        self.camera_fps = 0.0              # 驱动上报的帧率（可能为 0，仅供参考）

    # ---------------- 生命周期 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="video-stream", daemon=True)
        self._thread.start()
        # 等第一帧（或失败）后再返回，避免界面先显示空白
        for _ in range(60):
            if self._frame is not None or self.error:
                break
            time.sleep(0.05)
        return self._frame is not None

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # ---------------- 采集线程 ----------------
    def _open(self):
        if self._capture_factory is not None:
            return self._capture_factory(self.camera_index)
        import cv2
        cap = cv2.VideoCapture(self.camera_index)
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        except Exception:
            pass
        return cap

    def _run(self):
        import cv2
        cap = self._open()
        if cap is None or not cap.isOpened():
            self.error = "无法打开摄像头（编号 %d）；可改用「选择图片」通道" % self.camera_index
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            return
        with self._lock:
            self._cap = cap
        try:
            self.camera_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except Exception:
            self.camera_fps = 0.0

        while not self._stop.is_set():
            try:
                ok, frame = cap.read()
            except Exception as e:
                self.error = "读取视频失败：%s" % str(e)[:80]
                break
            if not ok or frame is None:
                self._failed += 1
                if self._failed > 30:          # 连续读不到 -> 认为设备掉线
                    self.error = "摄像头读取中断（连续 %d 次失败），请检查连接" % self._failed
                    break
                time.sleep(0.01)
                continue
            self._failed = 0
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._frames += 1
                self._times.append(time.perf_counter())
        try:
            cap.release()
        except Exception:
            pass

    # ---------------- 取帧与统计 ----------------
    def latest(self):
        """返回 (frame, seq)；没有新帧时 frame 为 None"""
        with self._lock:
            return (None if self._frame is None else self._frame.copy()), self._seq

    @property
    def frames(self):
        return self._frames

    @property
    def fps(self):
        """
        实时采集帧率：统计**最近 1 秒内**到达的帧数。
        （早期版本用"样本数/首末时间差"，遇到不限速的源会算出天文数字，
          按时间窗口统计才是真正的 FPS。）
        """
        now = time.perf_counter()
        with self._lock:
            ts = [t for t in self._times if now - t <= 1.0]
        if len(ts) < 2:
            return 0.0 if not ts else 1.0
        span = max(ts[-1] - ts[0], 1e-6)
        return round((len(ts) - 1) / span, 1)

    def status_text(self):
        if self.error:
            return self.error
        if not self.running:
            return "视频未开启"
        h, w = (self._frame.shape[:2] if self._frame is not None else (0, 0))
        return "实时采集中 ｜ 分辨率 %d×%d ｜ 采集 FPS %.1f ｜ 累计 %d 帧 ｜ 失败 %d 次" % (
            w, h, self.fps, self._frames, self._failed)


class VideoRecorder:
    """把实时画面存成视频文件（可选功能：留档用，默认不开启以省磁盘）"""

    def __init__(self, path, fps=15.0, size=None):
        import cv2
        self.path = str(path)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(self.path, fourcc, float(fps), size) if size else None
        self.count = 0

    def write(self, frame):
        if self.writer is None:
            import cv2
            h, w = frame.shape[:2]
            self.writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"), 15.0, (w, h))
        self.writer.write(frame)
        self.count += 1

    def close(self):
        if self.writer is not None:
            try:
                self.writer.release()
            except Exception:
                pass
        return self.path


def default_capture_dir():
    return config.CAPTURE_DIR
