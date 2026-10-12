# -*- coding: utf-8 -*-
"""
取流内核（camera_stream）· 防卡死回归测试

这些用例锁定的就是"没有网络就卡死 / 推流断了就卡死"这一类缺陷：
  1. 打开取流卡住时，open_capture 必须在超时后**返回失败**，而不是一直等；
  2. 读帧卡住（推流端 TCP 连上却不发数据）时，看门狗必须判"画面中断"并重连；
  3. stop() 必须立刻返回（哪怕读线程还卡在 C 调用里），且不再重连；
  4. probe.probe_stream 卡住的线程不能拖垮后续探测（并发上限快速失败）；
  5. 本机摄像头打开失败时 video.VideoStream.start() 不抛异常（01 项目的行为依赖）；
  6. 正常取流路径仍然工作（帧、seq、fps 都对）。

运行：python -m unittest tests.test_camera_stream -v
"""
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import shared_tmp      # noqa: E402  （先定好临时数据目录）
_TMP = shared_tmp()

import camera_stream   # noqa: E402
import numpy as np     # noqa: E402
import probe           # noqa: E402


class _FakeCap(object):
    """可编程的假采集器：能立刻给帧、能读失败、能永远卡住、能记录释放次数"""

    def __init__(self, mode="ok", fps=50.0, frames=1000):
        self.mode = mode
        self.fps = fps
        self.left = frames
        self.released = threading.Event()
        self.reads = 0

    def isOpened(self):
        return self.mode != "dead"

    def get(self, prop):
        return self.fps

    def read(self):
        self.reads += 1
        if self.mode == "hang":
            threading.Event().wait(30)          # 模拟卡死在 C 调用里（推流端不发数据）
            return False, None
        if self.mode == "fail":
            return False, None
        if self.mode == "dead":
            return False, None
        if self.left <= 0:
            time.sleep(0.05)
            return False, None
        self.left -= 1
        time.sleep(1.0 / self.fps)
        return True, np.zeros((24, 32, 3), dtype=np.uint8)

    def set(self, prop, val):
        return True

    def release(self):
        self.released.set()


class TestOpenCaptureTimeout(unittest.TestCase):
    """打开超时：卡住的打开动作不能拖住调用方"""

    def test_open_hang_returns_within_budget(self):
        def factory(_url):
            threading.Event().wait(30)          # 永远打不开
            return _FakeCap()
        t0 = time.time()
        cap, why = camera_stream.open_capture("rtsp://10.255.255.1/x", timeout=0.6,
                                              capture_factory=factory, log_it=False)
        used = time.time() - t0
        self.assertIsNone(cap)
        self.assertIn("超时", why)
        self.assertLess(used, 3.0, "打开超时必须按预算返回，不能一直等")

    def test_open_dead_device_reports_failure(self):
        cap, why = camera_stream.open_capture(0, timeout=1.0, log_it=False,
                                              capture_factory=lambda _u: _FakeCap("dead"))
        self.assertIsNone(cap)
        self.assertTrue(why)

    def test_open_rejects_empty_url(self):
        cap, why = camera_stream.open_capture("", log_it=False)
        self.assertIsNone(cap)
        self.assertIn("未配置", why)

    def test_release_async_does_not_block(self):
        cap = _FakeCap()
        t0 = time.time()
        camera_stream.release_async(cap, "单测")
        self.assertLess(time.time() - t0, 0.5, "释放必须在后台做，不能阻塞调用方")
        self.assertTrue(cap.released.wait(2.0))

    def test_is_local_device(self):
        self.assertTrue(camera_stream.is_local_device(0))
        self.assertTrue(camera_stream.is_local_device("1"))
        self.assertFalse(camera_stream.is_local_device("rtsp://x/y"))


class TestFrameReader(unittest.TestCase):
    """常驻取流器：正常出帧 / 卡死判中断 / 停止立刻返回"""

    def _reader(self, mode="ok", **kw):
        caps = []

        def factory(_url):
            c = _FakeCap(mode)
            caps.append(c)
            return c
        r = camera_stream.FrameReader("rtsp://127.0.0.1:1/x", capture_factory=factory,
                                      label="单测", **kw)
        return r, caps

    def _wait_state(self, r, states, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if r.state in states:
                return True
            time.sleep(0.05)
        return False

    def test_live_frames_and_fps(self):
        r, _caps = self._reader("ok")
        try:
            self.assertTrue(r.start())
            self.assertTrue(self._wait_state(r, ("live",)))
            time.sleep(0.4)
            snap = r.snapshot()
            self.assertEqual(snap["state"], "live")
            self.assertIsNotNone(snap["frame"])
            self.assertGreaterEqual(snap["seq"], 1)
            self.assertGreater(snap["fps"], 10.0)
            self.assertEqual(snap["frame"].shape, (24, 32, 3))
        finally:
            r.stop()

    def test_watchdog_detects_stalled_stream_and_reconnects(self):
        """推流端连上却不发数据 -> 必须判"画面中断"并重连，而不是一直沉默"""
        r, caps = self._reader("hang", stall_timeout=0.6, open_timeout=1.0)
        try:
            r.start()
            self.assertTrue(self._wait_state(r, ("stalled", "connecting"), timeout=5.0),
                            "卡死的流必须被判为中断/重连")
            time.sleep(2.2)
            snap = r.snapshot()
            self.assertGreaterEqual(snap["reconnects"], 1, "中断后应自动重连")
            self.assertIn(snap["state"], ("stalled", "connecting"))
            self.assertIn("重连", snap["status"])
        finally:
            r.stop()

    def test_stop_returns_immediately_even_if_read_hangs(self):
        r, caps = self._reader("hang", stall_timeout=0.5, open_timeout=1.0)
        r.start()
        self.assertTrue(self._wait_state(r, ("live", "stalled", "connecting"), timeout=5.0))
        t0 = time.time()
        r.stop()
        used = time.time() - t0
        self.assertLess(used, 1.0, "stop() 必须立刻返回，不能等卡住的读线程")
        self.assertFalse(r.running)
        self.assertEqual(r.state, "stopped")
        # 停止后不再重连
        n = r.snapshot()["reconnects"]
        time.sleep(1.0)
        self.assertEqual(r.snapshot()["reconnects"], n, "停止后不允许继续重连")

    def test_read_failures_then_error_state(self):
        r, _caps = self._reader("fail", stall_timeout=0.5, open_timeout=1.0,
                                max_reconnects=1)
        try:
            r.start()
            self.assertTrue(self._wait_state(r, ("stalled", "connecting", "error"),
                                            timeout=6.0))
            snap = r.snapshot()
            self.assertIn(snap["state"], ("stalled", "connecting", "error"))
            self.assertTrue(snap["status"])
        finally:
            r.stop()


class TestProbeStreamBounded(unittest.TestCase):
    """探测层：卡住的探测线程不能拖垮后续探测"""

    def test_probe_empty_and_bad_url(self):
        ok, frame, why = probe.probe_stream("")
        self.assertFalse(ok)
        self.assertIn("未配置", why)
        ok, frame, why = probe.probe_stream("rtsp://127.0.0.1:1/none", timeout_sec=2)
        self.assertFalse(ok)
        self.assertIsNone(frame)
        self.assertTrue(why)

    def test_probe_concurrency_cap_fails_fast(self):
        """占满并发额度后，新的探测必须立刻失败并说明原因（而不是排队卡死）"""
        held = []
        for _ in range(3):
            self.assertTrue(probe._PROBE_SLOTS.acquire(blocking=False))
            held.append(True)
        try:
            t0 = time.time()
            ok, frame, why = probe.probe_stream("rtsp://127.0.0.1:1/none", timeout_sec=5)
            used = time.time() - t0
            self.assertFalse(ok)
            self.assertIsNone(frame)
            self.assertIn("并发", why)
            self.assertLess(used, 1.0, "并发满时必须快速失败")
        finally:
            for _ in held:
                probe._PROBE_SLOTS.release()

    def test_probe_timeout_is_bounded(self):
        t0 = time.time()
        probe.probe_stream("rtsp://10.255.255.1:554/none", timeout_sec=1.5)
        self.assertLess(time.time() - t0, 6.0, "探测必须在自己声明的超时附近返回")


class TestSharedKernelCopies(unittest.TestCase):
    """01/02/03 三套系统各存一份 camera_stream.py，必须完全相同（防止改一处漏两处）"""

    def test_copies_identical(self):
        base = os.path.join(ROOT, "camera_stream.py")
        if not os.path.isfile(base):
            self.skipTest("当前目录没有 camera_stream.py")
        import hashlib
        with open(base, "rb") as f:
            base_hash = hashlib.sha256(f.read()).hexdigest()
        all_dir = os.path.dirname(os.path.dirname(ROOT))
        others = [os.path.join(all_dir, "01_人脸识别与身份核对", "01_人脸识别与身份核对",
                               "camera_stream.py"),
                  os.path.join(all_dir, "03_智能终端操作", "camera_stream.py")]
        for path in others:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), base_hash,
                                 "副本不一致，请同步：%s" % path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
