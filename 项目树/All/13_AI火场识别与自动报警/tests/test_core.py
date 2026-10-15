# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 单元测试

覆盖最关键、最容易回归的几处：
    1) 设置读写与**夹取**（settings.json 被手改坏也不能把程序搞崩）
    2) 火/非火判别（真火报警；静态红布、肤色、正常画面不报警）—— 检测算法的底线
    3) 确认闸门（单帧高分不许报警；连续命中才报警；连续无火才解除）
    4) 事件存储往返、误报标记、旧事件清理
    5) **证据路径不被覆盖**（这是实测踩到的 bug：整行保存把已写好的证据路径抹成空）
    6) 界面与弹窗能在无摄像头环境下构建（冒烟）

用法：
    python -m unittest discover -s tests -t .        # 全部
    python tests\test_core.py                        # 单文件
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

#: 全程用临时数据目录，绝不碰真实 data\
_TMP = tempfile.mkdtemp(prefix="fire_test_")
os.environ["EXAM_FIRE_HOME"] = _TMP

try:
    import numpy as np
    import cv2
    HAS_CV = True
except ImportError:
    HAS_CV = False

import config      # noqa: E402
import models      # noqa: E402


def _cleanup():
    shutil.rmtree(_TMP, ignore_errors=True)


def tearDownModule():
    _cleanup()


# ============================================================ 合成画面
def frame_clean(h=360, w=480):
    img = np.full((h, w, 3), 200, np.uint8)
    cv2.rectangle(img, (30, 40), (w - 30, h - 40), (120, 120, 120), -1)
    cv2.putText(img, "ROOM", (int(w * 0.3), int(h * 0.55)),
                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (60, 60, 60), 3)
    return img


def frame_fire(t, h=360, w=480, scale=1.0):
    """会抖动的火焰 —— 真火的关键特征就是"抖"。"""
    img = frame_clean(h, w)
    cx, cy = w // 2, int(h * 0.62)
    j = int(6 * np.sin(t * 6.0)) + int(4 * np.cos(t * 9.7))
    rw, rh = int(50 * scale), int(85 * scale)
    pts = np.array([[cx - rw, cy + rh], [cx - rw + 10 + j, cy - rh // 3],
                    [cx, cy - rh - 10 - j], [cx + rw - 6, cy - rh // 4],
                    [cx + rw, cy + rh]], np.int32)
    cv2.fillPoly(img, [pts], (0, 205, 255))
    cv2.circle(img, (cx + j // 2, cy + rh // 3), int(rh * 0.42), (60, 255, 255), -1)
    cv2.circle(img, (cx, cy + rh // 2), int(rh * 0.22), (255, 255, 255), -1)
    return img


def frame_static_red(h=360, w=480):
    """静止红布：颜色像火，但完全不抖、不增长。"""
    img = frame_clean(h, w)
    cv2.rectangle(img, (int(w * 0.42), int(h * 0.42)),
                  (int(w * 0.58), int(h * 0.78)), (40, 40, 200), -1)
    return img


def frame_skin(h=360, w=480, shift=0):
    """肤色块（BGR 偏黄红）：HSV 也像火，靠 YCbCr 排除。"""
    img = frame_clean(h, w)
    cv2.ellipse(img, (w // 2 + shift, h // 2), (52, 70), 0, 0, 360,
                (150, 180, 215), -1)
    return img


# ============================================================ 1) 配置
class TestConfig(unittest.TestCase):
    def test_defaults_load(self):
        cfg = config.load_settings()
        self.assertEqual(cfg["camera_url"], "0")
        self.assertIn("alarm_threshold", cfg)

    def test_coerce_clamps_out_of_range(self):
        """手改 settings.json 写坏也不能崩：一律夹到合法区间。"""
        cfg = config.coerce_settings({
            "alarm_threshold": 99,          # 上限 1.0
            "confirm_frames": -5,           # 下限 1
            "detect_interval_ms": 10 ** 9,  # 上限 5000
            "hsv_s_min": "不是数字",
            "smoke_enable": "是",
            "backend": "乱写",
            "min_fire_ratio": float("nan"),
        })
        self.assertLessEqual(cfg["alarm_threshold"], 1.0)
        self.assertGreaterEqual(cfg["confirm_frames"], 1)
        self.assertLessEqual(cfg["detect_interval_ms"], 5000)
        self.assertEqual(cfg["hsv_s_min"], config.SETTINGS_DEFAULT["hsv_s_min"])
        self.assertTrue(cfg["smoke_enable"])
        self.assertEqual(cfg["backend"], "auto")
        self.assertEqual(cfg["min_fire_ratio"],
                         config.SETTINGS_DEFAULT["min_fire_ratio"])

    def test_coerce_rejects_garbage_container(self):
        cfg = config.coerce_settings(["不是字典"])
        self.assertEqual(cfg["camera_url"], config.SETTINGS_DEFAULT["camera_url"])
        cfg2 = config.coerce_settings(None)
        self.assertEqual(cfg2["alarm_threshold"],
                         config.SETTINGS_DEFAULT["alarm_threshold"])

    def test_cross_field_consistency(self):
        """疑似阈值不许高于火警阈值；烟雾亮度的上下界要正过来。"""
        cfg = config.coerce_settings({"alarm_threshold": 0.5,
                                      "warning_threshold": 0.9,
                                      "smoke_v_min": 200, "smoke_v_max": 100})
        self.assertLessEqual(cfg["warning_threshold"], cfg["alarm_threshold"])
        self.assertLessEqual(cfg["smoke_v_min"], cfg["smoke_v_max"])

    def test_save_and_reload(self):
        ok, msg = config.save_settings({"camera_name": "测试机位",
                                        "confirm_frames": 7})
        self.assertTrue(ok, msg)
        cfg = config.load_settings()
        self.assertEqual(cfg["camera_name"], "测试机位")
        self.assertEqual(cfg["confirm_frames"], 7)
        config.save_settings({"camera_name": config.SETTINGS_DEFAULT["camera_name"],
                              "confirm_frames": config.SETTINGS_DEFAULT["confirm_frames"]})

    def test_unknown_keys_ignored(self):
        ok, _ = config.save_settings({"不存在的键": 1})
        self.assertTrue(ok)
        self.assertNotIn("不存在的键", config.load_settings())

    def test_level_for_score(self):
        cfg = config.coerce_settings({"alarm_threshold": 0.6, "warning_threshold": 0.4})
        self.assertEqual(config.level_for_score(0.8, cfg), config.LEVEL_ALARM)
        self.assertEqual(config.level_for_score(0.5, cfg), config.LEVEL_WARNING)
        self.assertIsNone(config.level_for_score(0.1, cfg))

    def test_broken_settings_file_is_survivable(self):
        path = config.SETTINGS_PATH
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        try:
            path.write_text("{这不是合法 JSON", encoding="utf-8")
            cfg = config.load_settings()
            self.assertEqual(cfg["camera_url"], "0")      # 回落默认值而不是抛异常
        finally:
            if backup is not None:
                path.write_text(backup, encoding="utf-8")


# ============================================================ 2) 检测算法
@unittest.skipUnless(HAS_CV, "缺 cv2/numpy")
class TestDetectorDiscrimination(unittest.TestCase):
    """检测算法的底线：真火要报，会误报的典型干扰物不能报。"""

    def _run(self, frames, cfg=None):
        import detect as detect_mod
        cfg = cfg or config.load_settings()
        det = detect_mod.FireDetector(cfg=cfg)
        for f in frames:
            det.update(f, camera_name="t", camera_url="0")
        return det

    def test_clean_scene_never_alarms(self):
        det = self._run([frame_clean() for _ in range(15)])
        self.assertIsNone(det.active_event)
        self.assertLess(det.last_detection.score, 0.40)

    def test_flickering_fire_alarms(self):
        det = self._run([frame_fire(i * 0.12) for i in range(24)])
        self.assertIsNotNone(det.active_event, "真火没有报警，检测阈值过严")
        self.assertGreaterEqual(det.active_event.score, 0.62)

    def test_static_red_object_does_not_alarm(self):
        """静止红布：颜色像火，但没有闪烁/增长 —— 不许进火警，也不该踩到疑似线。"""
        for (h, w) in ((360, 480), (480, 640)):
            det = self._run([frame_static_red(h, w) for _ in range(24)])
            self.assertIsNone(det.active_event,
                              "静止红布被当成火警了（会让系统天天误报）")
            self.assertLess(det.last_detection.score, 0.40,
                            "静止红布的分数 %.3f 已踩到疑似线（%dx%d）"
                            % (det.last_detection.score, h, w))

    def test_skin_tone_does_not_alarm(self):
        """肤色在 HSV 里也是红黄，靠 YCbCr 排除。"""
        det = self._run([frame_skin(shift=(i % 4) * 3) for i in range(24)])
        self.assertIsNone(det.active_event, "肤色被当成火警了")

    def test_fire_next_to_skin_still_alarms(self):
        """火和人脸同框：排肤色不能把真火一起排掉。"""
        frames = []
        for i in range(24):
            img = frame_skin()
            fire = frame_fire(i * 0.12)
            h, w = img.shape[:2]
            img[:, : w // 2] = fire[:, : w // 2]
            frames.append(img)
        det = self._run(frames)
        self.assertIsNotNone(det.active_event, "火与人脸同框时漏报了")

    def test_signal_parts_are_reported(self):
        """必须能说清"凭什么判的"：parts 里要有非零信号。"""
        det = self._run([frame_fire(i * 0.12) for i in range(24)])
        parts = det.last_detection.parts
        self.assertTrue(any(v > 0 for v in parts.values()), "信号分解全为 0")
        self.assertTrue(det.last_detection.reason_text())

    def test_backend_reports_its_name(self):
        import detect as detect_mod
        b = detect_mod.VisionFireBackend(config.load_settings())
        self.assertTrue(detect_mod.describe_backend(b))


# ============================================================ 3) 确认闸门
@unittest.skipUnless(HAS_CV, "缺 cv2/numpy")
class TestConfirmGate(unittest.TestCase):
    def _det(self, **patch):
        import detect as detect_mod
        cfg = config.coerce_settings(dict(config.SETTINGS_DEFAULT, **patch))
        return detect_mod.FireDetector(cfg=cfg)

    def test_single_spike_does_not_alarm(self):
        """单帧高分不算火警（闪光灯/车灯扫过就是这种形态）。"""
        det = self._det(confirm_frames=4)
        det.update(frame_fire(0.0), camera_name="t")
        self.assertIsNone(det.active_event, "单帧就报警了，闸门没起作用")

    def test_needs_consecutive_frames(self):
        det = self._det(confirm_frames=4)
        for i in range(3):
            det.update(frame_fire(i * 0.12), camera_name="t")
        self.assertIsNone(det.active_event, "未达确认帧数就报警了")
        for i in range(3, 6):
            det.update(frame_fire(i * 0.12), camera_name="t")
        self.assertIsNotNone(det.active_event, "达到确认帧数仍未报警")

    def test_interruption_resets_counter(self):
        """中间断了要重新计数，不许"隔帧凑数"。"""
        det = self._det(confirm_frames=3)
        det.update(frame_fire(0.0), camera_name="t")
        det.update(frame_fire(0.1), camera_name="t")
        det.update(frame_clean(), camera_name="t")      # 打断
        self.assertEqual(det.hit_streak, 0)
        det.update(frame_fire(0.2), camera_name="t")
        self.assertIsNone(det.active_event)

    def test_clear_after_quiet_frames(self):
        det = self._det(confirm_frames=2, clear_frames=4)
        for i in range(6):
            det.update(frame_fire(i * 0.12), camera_name="t")
        self.assertIsNotNone(det.active_event)
        for _ in range(6):
            det.update(frame_clean(), camera_name="t")
        self.assertIsNone(det.active_event, "连续无火后没有解除")

    def test_callbacks_fire(self):
        events = {"alarm": 0, "clear": 0}

        def on_a(det, ev):
            events["alarm"] += 1

        def on_c(ev):
            events["clear"] += 1

        import detect as detect_mod
        cfg = config.coerce_settings(dict(config.SETTINGS_DEFAULT,
                                          confirm_frames=2, clear_frames=3))
        det = detect_mod.FireDetector(cfg=cfg, on_alarm=on_a, on_clear=on_c)
        for i in range(5):
            det.update(frame_fire(i * 0.12), camera_name="t")
        for _ in range(5):
            det.update(frame_clean(), camera_name="t")
        self.assertEqual(events["alarm"], 1)
        self.assertEqual(events["clear"], 1)

    def test_status_text_is_human_readable(self):
        det = self._det()
        self.assertIn("监控", det.status_text())


# ============================================================ 4) 存储
class TestStorage(unittest.TestCase):
    def setUp(self):
        import storage
        self.store = storage.EventStore(
            os.path.join(_TMP, "t_%d.db" % int(time.time() * 1000 % 100000)))

    def test_event_roundtrip(self):
        ev = models.FireEvent(camera_name="A01", camera_url="0", score=0.71,
                              reason="火色+闪烁", parts={"火色": 0.5, "闪烁": 0.2},
                              fire_ratio=0.031, smoke_ratio=0.0,
                              regions=[(1, 2, 3, 4, 12)])
        self.assertTrue(self.store.save_event(ev))
        got = self.store.get_event(ev.event_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.camera_name, "A01")
        self.assertAlmostEqual(got.score, 0.71, places=3)
        self.assertEqual(got.parts["火色"], 0.5)
        self.assertEqual(got.regions[0][0], 1)

    def test_list_and_summary(self):
        for i in range(3):
            ev = models.FireEvent(camera_name="c%d" % i, score=0.7 + i * 0.05)
            ev.level = config.LEVEL_ALARM if i else config.LEVEL_WARNING
            self.store.save_event(ev)
        self.assertEqual(len(self.store.list_events(limit=10)), 3)
        s = self.store.summary()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["alarm"], 2)
        self.assertEqual(s["warning"], 1)

    def test_ack_and_false_marks(self):
        ev = models.FireEvent(camera_name="A", score=0.8)
        self.store.save_event(ev)
        ok, msg = self.store.ack_event(ev.event_id, by="张工", note="已到现场")
        self.assertTrue(ok, msg)
        got = self.store.get_event(ev.event_id)
        self.assertEqual(got.status, models.ST_ACKED)
        self.assertEqual(got.acked_by, "张工")
        ok, _ = self.store.mark_false(ev.event_id, by="张工", note="焊接火花")
        self.assertTrue(ok)
        self.assertEqual(self.store.get_event(ev.event_id).status, models.ST_FALSE)
        self.assertEqual(self.store.summary()["false_alarm"], 1)

    def test_audit_appends(self):
        self.store.log("测试动作", "细节", "tester")
        rows = self.store.audit_rows(limit=5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["action"], "测试动作")

    def test_purge_old_removes_only_old(self):
        fresh = models.FireEvent(camera_name="新", score=0.8)
        old = models.FireEvent(camera_name="旧", score=0.8)
        old.created_epoch = time.time() - 100 * 86400
        self.store.save_event(fresh)
        self.store.save_event(old)
        n, _f, _b = self.store.purge_old(keep_days=30)
        self.assertEqual(n, 1)
        self.assertIsNone(self.store.get_event(old.event_id))
        self.assertIsNotNone(self.store.get_event(fresh.event_id))

    def test_purge_disabled_when_zero(self):
        ev = models.FireEvent(camera_name="旧", score=0.8)
        ev.created_epoch = time.time() - 999 * 86400
        self.store.save_event(ev)
        n, _f, _b = self.store.purge_old(keep_days=0)
        self.assertEqual(n, 0)
        self.assertIsNotNone(self.store.get_event(ev.event_id))

    def test_evidence_paths_survive_a_stale_row_save(self):
        """
        回归测试：曾经的 bug —— 证据线程写盘后回填了路径，主线程随后用
        "还没有路径的旧对象"整行保存，把路径抹成空（磁盘上有图、库里却没路径）。
        storage.save_event 现在必须保护证据列。
        """
        ev = models.FireEvent(camera_name="A", score=0.8)
        self.store.save_event(ev)
        # 模拟证据线程回填
        fake = os.path.join(_TMP, "snap.jpg")
        self.store.set_evidence(ev.event_id, snapshot=fake,
                                marked=fake.replace("snap", "marked"))
        # 模拟主线程拿着旧对象整行保存（它的三个路径字段都是空的）
        stale = self.store.get_event(ev.event_id)
        stale.snapshot = ""
        stale.marked = ""
        stale.clip = ""
        self.store.save_event(stale)
        got = self.store.get_event(ev.event_id)
        self.assertEqual(got.snapshot, fake, "证据路径被空值覆盖了")
        self.assertTrue(got.marked)

    def test_set_evidence_is_narrow_write(self):
        """窄写入不能碰到其他字段。"""
        ev = models.FireEvent(camera_name="A", score=0.8)
        self.store.save_event(ev)
        self.store.ack_event(ev.event_id, by="李工")
        self.store.set_evidence(ev.event_id, clip="x.avi")
        got = self.store.get_event(ev.event_id)
        self.assertEqual(got.status, models.ST_ACKED)     # 没被窄写入影响
        self.assertEqual(got.acked_by, "李工")
        self.assertEqual(got.clip, "x.avi")


# ============================================================ 5) 模型
class TestModels(unittest.TestCase):
    def test_detection_level_from_score(self):
        d = models.Detection(has_fire=True, score=0.9)
        self.assertEqual(d.level, config.LEVEL_ALARM)
        d2 = models.Detection(has_fire=False, score=0.0)
        self.assertIsNone(d2.level)
        self.assertEqual(d2.level_name(), "正常")

    def test_detection_to_dict(self):
        d = models.Detection(has_fire=True, score=0.7, parts={"火色": 0.5},
                             regions=[(0, 0, 10, 10, 100)])
        blob = d.to_dict()
        self.assertIn("reason", blob)
        self.assertIn("parts", blob)
        self.assertEqual(blob["level"], config.LEVEL_ALARM)

    def test_event_observe_upgrades_level(self):
        ev = models.FireEvent(camera_name="A", level=config.LEVEL_WARNING, score=0.45)
        ev.observe(models.Detection(has_fire=True, score=0.8))
        self.assertEqual(ev.level, config.LEVEL_ALARM)
        self.assertGreaterEqual(ev.frames_hit, 2)
        self.assertAlmostEqual(ev.peak_score, 0.8, places=3)

    def test_event_clear_and_false(self):
        ev = models.FireEvent(camera_name="A", score=0.8)
        ev.ack(by="甲")
        ev.clear()
        self.assertEqual(ev.status, models.ST_CLEARED)
        ev2 = models.FireEvent(camera_name="B", score=0.8)
        ev2.mark_false(by="乙")
        ev2.clear()
        self.assertEqual(ev2.status, models.ST_FALSE, "误报不该被改成已解除")

    def test_from_row_tolerates_bad_json(self):
        ev = models.FireEvent.from_row({"event_id": "X", "level": "alarm",
                                        "parts": "不是JSON", "regions": "{坏"})
        self.assertEqual(ev.parts, {})
        self.assertEqual(ev.regions, [])


# ============================================================ 6) 界面冒烟
def _tk_available():
    try:
        import tkinter
        r = tkinter.Tk()
        r.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "本环境无图形界面，跳过界面冒烟")
@unittest.skipUnless(HAS_CV, "缺 cv2/numpy")
class TestUiSmoke(unittest.TestCase):
    """无摄像头也要能把界面和弹窗建起来（否则真出事时弹不出来）。"""

    def test_main_window_builds(self):
        import ui_main
        app = ui_main.FireApp(hidden=True)
        try:
            self.assertIsNotNone(app.root)
            self.assertTrue(app.lbl_backend.cget("text"))
            app.refresh_events()
            app._render_frame()          # 未启动监控时应安全返回
            app.apply_params()
            app._status("冒烟", "#fff")
        finally:
            app.quit_app()

    def test_popup_builds_and_ack(self):
        import tkinter as tk
        import ui_popup
        root = tk.Tk()
        root.withdraw()
        tries = {"ack": 0}
        pop = ui_popup.AlarmPopup(root, app=None,
                                  on_ack=lambda ev: (tries.__setitem__("ack", tries["ack"] + 1)
                                                     or (True, "ok")),
                                  on_false=lambda ev: (True, "ok"))
        ev = models.FireEvent(camera_name="A01", score=0.83, reason="火色 0.5、闪烁 0.2")
        try:
            pop.show(ev, frame_fire(0.1), models.Detection(has_fire=True, score=0.83))
            self.assertTrue(pop.visible)
            pop._do_ack()
            self.assertEqual(tries["ack"], 1)
            pop._refresh_list()
            pop._refresh_detail()
        finally:
            pop.hide()
            root.destroy()

    def test_popup_multiple_events_do_not_stack(self):
        import tkinter as tk
        import ui_popup
        root = tk.Tk()
        root.withdraw()
        pop = ui_popup.AlarmPopup(root, app=None, on_ack=lambda ev: (True, "ok"),
                                  on_false=lambda ev: (True, "ok"))
        try:
            for i in range(3):
                ev = models.FireEvent(camera_name="A%d" % i, score=0.8)
                pop.show(ev, frame_fire(i * 0.1))
            self.assertTrue(pop.visible)
            self.assertEqual(len(pop._events), 3)     # 追加而不是叠三个窗
        finally:
            pop.hide()
            root.destroy()

    def test_tray_available_is_non_fatal(self):
        import tray
        ok, why = tray.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(why)


# ============================================================ 7) 取流/工作线程
@unittest.skipUnless(HAS_CV, "缺 cv2/numpy")
class TestVideoWorker(unittest.TestCase):
    def test_worker_runs_with_injected_capture(self):
        """注入假采集器验证后台检测线程：不碰真设备、不卡界面。"""
        import video as vm
        import detector as dm
        import storage

        state = {"n": 0}

        class FakeCap:
            def read(self):
                state["n"] += 1
                if state["n"] % 3 == 0:
                    return True, frame_fire(state["n"] * 0.1)
                return True, frame_clean()

            def release(self):
                pass

            def isOpened(self):
                return True

        store = storage.EventStore(os.path.join(_TMP, "w.db"))
        cfg = config.coerce_settings(dict(config.SETTINGS_DEFAULT,
                                          camera_url="0", confirm_frames=2,
                                          detect_interval_ms=50))
        alarms = []
        det = None

        def on_alarm(d, ev):
            alarms.append(ev)

        det, _why = dm.build_detector(cfg, on_alarm=on_alarm)
        worker = vm.VideoWorker(cfg, store=store, detector=det,
                                recorder=None,
                                capture_factory=lambda url: FakeCap())
        worker.start()
        try:
            t0 = time.time()
            while time.time() - t0 < 6.0 and not alarms:
                time.sleep(0.1)
            snap = worker.snapshot()
            self.assertIsNotNone(snap.get("frame"), "后台线程没有拿到帧")
            self.assertGreater(snap.get("detect_count", 0), 0)
        finally:
            worker.stop("测试结束")
        self.assertFalse(worker.running)

    def test_camera_hint(self):
        import video as vm
        self.assertIn("本机", vm.camera_hint("0"))
        self.assertIn("网络", vm.camera_hint("rtsp://1.2.3.4/s"))
        self.assertIn("文件", vm.camera_hint(__file__))


if __name__ == "__main__":
    unittest.main(verbosity=2)
