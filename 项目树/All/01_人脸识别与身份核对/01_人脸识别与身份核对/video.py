# -*- coding: utf-8 -*-
"""
实时视频流：给 GUI 提供"边看边核验"的现场画面。

设计要点（本次加固，全部针对"卡死"）：
  · 采集在**独立线程**里跑，界面线程只取最新帧，绝不阻塞；
  · 用 camera_stream.FrameReader 作为取流内核：
      - 打开带超时（本机摄像头被占用/驱动异常时不会把界面挂住）；
      - 读取带**看门狗**：连续不出画面就判"画面中断"，另起连接重连，不 join 卡死的线程；
      - 释放走后台线程（卡住的流 release 也可能阻塞）；
  · `start()` 有明确上限（默认 3 秒）：等不到画面就回退到"选择图片"通道，界面不会被挂住；
  · 只保留最新一帧（丢帧策略），推理慢也不会越积越延迟；
  · 实时统计采集 FPS（最近 1 秒窗口），供界面显示；
  · 采集不到摄像头时**不抛异常**，返回明确错误信息；
  · 现场性（防翻拍）不受影响：核验用的是采集线程刚取到的真实相机帧 + 现场令牌。

可注入 capture_factory 以便在无摄像头的机器上做自动化测试。
"""
import time

import camera_stream
import config


class VideoStream:
    """本机摄像头实时取流（对 camera_stream.FrameReader 的兼容封装）"""

    START_WAIT_SEC = 3.0          # start() 最多等这么久，超时即判失败（界面不再被挂住）

    def __init__(self, camera_index=0, width=640, height=480, capture_factory=None,
                 fps_window=240):
        self.camera_index = int(camera_index)
        self.width = int(width)
        self.height = int(height)
        self.start_wait = self.START_WAIT_SEC
        self._reader = camera_stream.FrameReader(
            self.camera_index, width=self.width, height=self.height, fps=30,
            label="本机摄像头 %d" % self.camera_index, capture_factory=capture_factory)
        self.error = ""
        self.camera_fps = 0.0              # 驱动上报的帧率（可能为 0，仅供参考）

    # ---------------- 生命周期 ----------------
    def start(self):
        """启动采集。返回是否可用（等首帧/开流结果，最长 START_WAIT_SEC 秒）。

        注意：这里等的是"打开结果"，不是"首帧" —— 打不开就立刻返回 False，
        打得开但要过一会儿才有画面时也先算可用（界面会显示"等待画面"）。
        """
        self.error = ""
        self._reader.start()
        deadline = time.time() + self.start_wait
        while time.time() < deadline:
            snap = self._reader.snapshot()
            state = snap["state"]
            if state in ("live", "stalled"):
                # live=已连接；stalled=连上了但暂时不出帧（会自行重连），都不算失败
                return True
            if state in ("error", "stopped"):
                self.error = self._open_error(snap)
                self._reader.stop("启动失败")
                return False
            time.sleep(0.02)
        # 打开动作卡住了（网络摄像头/驱动无响应）：如实报错，不拖住调用方
        self.error = "打开摄像头超时（%.0f 秒）；可改用「选择图片」通道" % self.start_wait
        self._reader.stop("启动超时")
        return False

    @staticmethod
    def _open_error(snap):
        status = snap.get("status") or ""
        if "打开失败" in status:
            return "无法打开摄像头：%s" % status.split("：", 1)[-1]
        return status or "无法打开摄像头"

    def stop(self):
        self._reader.stop("关闭实时视频")

    @property
    def running(self):
        return self._reader.running

    # ---------------- 取帧与统计 ----------------
    def latest(self):
        """返回 (frame, seq)；没有新帧时 frame 为 None

        注意：FrameReader 每帧都是新分配的数组，这里不再 copy（省一份内存拷贝）；
        调用方若要长期持有请自行 copy。
        """
        frame, seq = self._reader.latest()
        return frame, seq

    @property
    def frames(self):
        return self._reader.snapshot()["seq"]

    @property
    def fps(self):
        """实时采集帧率：统计**最近 1 秒内**到达的帧数。"""
        return self._reader.fps

    def status_text(self):
        snap = self._reader.snapshot()
        if snap["state"] == "stalled":
            return "%s（已自动重连 %d 次）" % (snap["status"], snap["reconnects"])
        if snap["state"] == "error":
            return snap["status"]
        if not self.running:
            return self.error or "视频未开启"
        frame = snap["frame"]
        h, w = (frame.shape[:2] if frame is not None else (0, 0))
        return "实时采集中 ｜ 分辨率 %d×%d ｜ 采集 FPS %.1f ｜ 累计 %d 帧" % (
            w, h, self.fps, snap["seq"])


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
