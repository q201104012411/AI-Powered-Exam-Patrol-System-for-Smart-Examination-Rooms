# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 单元测试

运行：python -m unittest discover -s tests -t .
全程在临时数据目录里跑（EXAM_CAMERA_HOME），不碰真实 data\\。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 数据目录用测试包共用的那个临时目录（tests\__init__.py 里设好 EXAM_CAMERA_HOME，
# 保证与别的测试模块落在同一个目录，谁也不删谁的），必须在 import config 之前生效。
from tests import shared_tmp      # noqa: E402
_TMP = shared_tmp()

import cv2            # noqa: E402
import config         # noqa: E402
import diagnose       # noqa: E402
import models         # noqa: E402
import probe          # noqa: E402
import storage        # noqa: E402
import baseline as baseline_mod   # noqa: E402
import patrol as patrol_mod       # noqa: E402
import report as report_mod       # noqa: E402
import workorder as wo_mod        # noqa: E402


def tearDownModule():
    """本模块不删共用临时目录（别的模块还在用），统一由 tests\\__init__.py 退出时清理"""
    return None


def _scene(w=640, h=360, seed=7):
    rng = np.random.RandomState(seed)
    img = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        img[y, :, :] = (60 + 80 * y // h, 70 + 70 * y // h, 90 + 60 * y // h)
    for _ in range(60):
        x1, y1 = rng.randint(0, w - 40), rng.randint(0, h - 40)
        cv2.rectangle(img, (x1, y1), (x1 + rng.randint(10, 90), y1 + rng.randint(10, 70)),
                      tuple(int(c) for c in rng.randint(40, 230, 3)), rng.randint(1, 3))
    for i in range(6):
        cv2.putText(img, "A%d" % i, (30 + i * 90, 90 + (i % 2) * 150),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (235, 235, 235), 2)
    return img


class Base(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(_TMP, "t_%s.db" % self.id().split(".")[-1])
        if os.path.exists(self.db):
            os.remove(self.db)
        self.store = storage.Store(db_path=self.db)
        self.dg = diagnose.Diagnoser()
        self.base = _scene()
        self.bm, self.bg = diagnose.baseline_metrics(self.base, self.dg)

    def dev(self, code="CAM-1", room="A-01", url="", **kw):
        d = models.Device(code=code, room_id=room, name=code, position="前墙",
                          url=url, **kw)
        self.store.save_device(d)
        return d


# ==================== 图像质量 ====================
class TestQuality(Base):
    def test_normal_no_defect_with_baseline(self):
        """真实部署一定有基准图；有基准时正常画面不应报任何缺陷"""
        defects, _ = self.dg.diagnose(self.base, baseline_gray=self.bg,
                                      baseline_metrics=self.bm)
        self.assertEqual([d.code for d in defects], [])

    def test_no_baseline_is_flagged_in_metrics(self):
        """没有基准图时要在指标里如实标注（退化模式，不假装做过视野比对）"""
        _, m = self.dg.diagnose(self.base)
        self.assertEqual(m.get("baseline_missing"), 1)

    def test_blur(self):
        d, _ = self.dg.diagnose(cv2.GaussianBlur(self.base, (15, 15), 0))
        self.assertIn("BLUR", [x.code for x in d])

    def test_underexposed(self):
        img = np.clip(self.base.astype(np.float32) * 0.1, 0, 255).astype(np.uint8)
        d, m = self.dg.diagnose(img)
        self.assertIn("UNDEREXPOSED", [x.code for x in d])
        self.assertLess(m["mean_luma"], 45)

    def test_overexposed(self):
        img = np.clip(self.base.astype(np.float32) * 3.2 + 70, 0, 255).astype(np.uint8)
        self.assertIn("OVEREXPOSED", [x.code for x in self.dg.diagnose(img)[0]])

    def test_low_contrast_not_reported_as_blur(self):
        img = np.clip(self.base.astype(np.float32) * 0.1 + 120, 0, 255).astype(np.uint8)
        codes = [d.code for d in self.dg.diagnose(img)[0]]
        self.assertIn("LOW_CONTRAST", codes)
        self.assertNotIn("BLUR", codes)          # 融合：极低对比不叠加模糊

    def test_color_cast(self):
        img = self.base.astype(np.float32).copy()
        img[:, :, 0] = np.clip(img[:, :, 0] * 1.9 + 25, 0, 255)
        self.assertIn("COLOR_CAST", [x.code for x in
                                    self.dg.diagnose(img.astype(np.uint8))[0]])

    def test_stripes_not_reported_as_snow(self):
        yy = np.arange(self.base.shape[0], dtype=np.float32)[:, None, None]
        img = np.clip(self.base.astype(np.float32) + 42 * np.sin(2 * np.pi * yy / 9.0),
                      0, 255).astype(np.uint8)
        codes = [d.code for d in self.dg.diagnose(img)[0]]
        self.assertIn("STRIPES", codes)
        self.assertNotIn("SNOW", codes)

    def test_snow(self):
        rng = np.random.RandomState(3)
        img = np.clip(self.base.astype(np.float32) + rng.normal(0, 22, self.base.shape),
                      0, 255).astype(np.uint8)
        self.assertIn("SNOW", [x.code for x in self.dg.diagnose(img)[0]])

    def test_mosaic(self):
        small = cv2.resize(self.base, (self.base.shape[1] // 10, self.base.shape[0] // 10))
        img = cv2.resize(small, (self.base.shape[1], self.base.shape[0]),
                         interpolation=cv2.INTER_NEAREST)
        self.assertIn("MOSAIC", [x.code for x in self.dg.diagnose(img)[0]])

    def test_exposure_suppresses_derived_defects(self):
        """过暗时不应同时报模糊/低对比/块效应（多算法融合）"""
        img = np.clip(self.base.astype(np.float32) * 0.1, 0, 255).astype(np.uint8)
        codes = {d.code for d in self.dg.diagnose(img)[0]}
        self.assertIn("UNDEREXPOSED", codes)
        self.assertFalse(codes & {"BLUR", "LOW_CONTRAST", "MOSAIC", "OCCLUDED"})

    def test_unreadable_image(self):
        defects, _ = self.dg.diagnose(None)
        self.assertEqual(defects[0].code, "STREAM_FAIL")

    def test_metrics_on_work_width(self):
        wide = _scene(1280, 720)
        m, g = self.dg.frame_metrics(wide)
        self.assertEqual(g.shape[1], config.QUALITY["work_width"])


# ==================== 基准图与视野 ====================
class TestBaseline(Base):
    def test_occlusion_detected_with_baseline(self):
        occ = self.base.copy()
        occ[40:340, 60:600] = 128                      # 遮住大部分画面
        codes = [d.code for d in self.dg.diagnose(occ, baseline_gray=self.bg,
                                                 baseline_metrics=self.bm)[0]]
        self.assertIn("OCCLUDED", codes)

    def test_view_offset_detected(self):
        m = np.float32([[1, 0, 40], [0, 1, 15]])
        img = cv2.warpAffine(self.base, m, (self.base.shape[1], self.base.shape[0]),
                             borderMode=cv2.BORDER_REPLICATE)
        defects, metrics = self.dg.diagnose(img, baseline_gray=self.bg,
                                            baseline_metrics=self.bm)
        self.assertIn("VIEW_OFFSET", [d.code for d in defects])
        self.assertGreater(metrics["shift_px"], config.BASELINE["offset_max_px"])
        self.assertGreater(metrics["angle_deg"], 0)

    def test_normal_not_flagged_with_baseline(self):
        codes = [d.code for d in self.dg.diagnose(self.base, baseline_gray=self.bg,
                                                 baseline_metrics=self.bm)[0]]
        self.assertFalse(set(codes) & {"OCCLUDED", "VIEW_OFFSET", "COVERAGE_LOW"})

    def test_capture_and_readback(self):
        dev = self.dev()
        bs = baseline_mod.BaselineStore(self.store)
        ok, res = bs.capture(dev, frame=self.base, operator="admin")
        self.assertTrue(ok, res)
        self.assertTrue(os.path.exists(res["path"]))
        rec, gray, m = bs.get(dev)
        self.assertIsNotNone(gray)
        self.assertIn("sharpness", m)
        self.assertTrue(bs.coverage_map_ok(dev))

    def test_capture_without_frame(self):
        bs = baseline_mod.BaselineStore(self.store)
        ok, msg = bs.capture(self.dev(), frame=None, image_path=None)
        self.assertFalse(ok)

    def test_capture_does_not_wipe_heartbeat(self):
        """回归：采集基准图不能把已写入的心跳抹掉"""
        dev = self.dev()
        self.store.heartbeat(dev.device_id, models.now_epoch())
        baseline_mod.BaselineStore(self.store).capture(dev, frame=self.base)
        again = self.store.get_device(device_id=dev.device_id)
        self.assertGreater(again.last_heartbeat, 0)
        self.assertTrue(probe.heartbeat_status(again)[0])

    def test_missing_baseline_reason(self):
        bs = baseline_mod.BaselineStore(self.store)
        self.assertIn("尚未采集", bs.missing_reason(self.dev()))


# ==================== 冻结帧 ====================
class TestFrozen(Base):
    def test_frozen(self):
        d = self.dg.frozen_defect([0.0, 0.0, 0.0])
        self.assertIsNotNone(d)
        self.assertEqual(d.code, "FROZEN")

    def test_not_frozen(self):
        self.assertIsNone(self.dg.frozen_defect([9.0, 0.1, 0.1]))

    def test_insufficient_frames(self):
        self.assertIsNone(self.dg.frozen_defect([0.0]))


# ==================== 在线/录像/时间/音频 ====================
class TestProbe(Base):
    def test_heartbeat_states(self):
        dev = self.dev()
        self.assertFalse(probe.heartbeat_status(dev)[0])           # 无记录
        self.store.heartbeat(dev.device_id, models.now_epoch() - 5)
        self.assertTrue(probe.heartbeat_status(self.store.get_device(
            device_id=dev.device_id))[0])
        self.store.heartbeat(dev.device_id, models.now_epoch() - 5000)
        self.assertFalse(probe.heartbeat_status(self.store.get_device(
            device_id=dev.device_id))[0])

    def test_stream_without_url(self):
        ok, frame, why = probe.probe_stream("")
        self.assertFalse(ok)
        self.assertIn("未配置", why)

    def test_stream_bad_url_does_not_hang(self):
        ok, frame, why = probe.probe_stream("rtsp://127.0.0.1:1/none", timeout_sec=2)
        self.assertFalse(ok)
        self.assertIsNone(frame)

    def test_recording_continuous(self):
        r = probe.recording_completeness(
            [{"id": 1, "start": 0, "duration": 300, "playable": True},
             {"id": 2, "start": 300, "duration": 300, "playable": True}])
        self.assertTrue(r["ok"])

    def test_recording_start_zero_not_dropped(self):
        """回归：start=0 的段不能被当成空值过滤掉"""
        r = probe.recording_completeness(
            [{"id": 1, "start": 0, "duration": 300, "playable": True},
             {"id": 2, "start": 900, "duration": 300, "playable": True}])
        self.assertEqual(len(r["gaps"]), 1)
        self.assertFalse(r["ok"])

    def test_recording_empty(self):
        r = probe.recording_completeness([])
        self.assertTrue(r["missing"])
        self.assertFalse(r["ok"])

    def test_recording_unplayable(self):
        r = probe.recording_completeness(
            [{"id": 1, "start": 0, "duration": 300, "playable": False}])
        self.assertFalse(r["ok"])
        self.assertEqual(r["unplayable_count"], 1)

    def test_time_drift(self):
        self.assertTrue(probe.time_drift(1000.0, 1000.5)[0])
        self.assertFalse(probe.time_drift(1000.0, 1003.0)[0])
        self.assertFalse(probe.time_drift(None, 1000.0)[0])

    def test_audio(self):
        self.assertTrue(probe.audio_level(*probe.synth_audio("normal"))["ok"])
        self.assertFalse(probe.audio_level(*probe.synth_audio("silent"))["ok"])
        self.assertFalse(probe.audio_level(*probe.synth_audio("noise"))["ok"])
        self.assertFalse(probe.audio_level(None)["ok"])
        self.assertFalse(probe.audio_level(np.zeros(10, dtype=np.float32))["ok"])

    def test_url_kind(self):
        self.assertEqual(probe.url_kind("0"), "本机摄像头")
        self.assertEqual(probe.url_kind("rtsp://x/y"), "RTSP")
        self.assertEqual(probe.url_kind(""), "未配置")


# ==================== 台账 ====================
class TestDevice(Base):
    def test_ledger_problems(self):
        self.assertTrue(models.Device(code="X").ledger_problems())
        d = models.Device(code="X", room_id="A-01", position="前墙", fov_deg=90)
        self.assertEqual(d.ledger_problems(), [])

    def test_save_and_query(self):
        self.dev("CAM-1", "A-01")
        self.dev("CAM-2", "A-02")
        self.assertEqual(len(self.store.list_devices()), 2)
        self.assertEqual(len(self.store.list_devices(room_id="A-01")), 1)
        self.assertEqual(self.store.rooms(), ["A-01", "A-02"])

    def test_update_device(self):
        d = self.dev("CAM-1")
        d.position = "后墙"
        self.store.save_device(d)
        self.assertEqual(self.store.get_device(code="CAM-1").position, "后墙")

    def test_delete_device(self):
        d = self.dev("CAM-1")
        self.store.delete_device(d.device_id)
        self.assertIsNone(self.store.get_device(code="CAM-1"))


# ==================== 巡检 ====================
class TestPatrol(Base):
    def setUp(self):
        Base.setUp(self)
        self.bs = baseline_mod.BaselineStore(self.store)
        self.d1 = self.dev("CAM-1", "A-01", session="S1")
        self.d2 = self.dev("CAM-2", "A-01", session="S1")
        for d in (self.d1, self.d2):
            self.bs.capture(d, frame=self.base)
            self.store.heartbeat(d.device_id)
        self.runner = patrol_mod.PatrolRunner(
            self.store,
            frame_provider=lambda d: (True, self.base, "ok"),
            recording_provider=lambda d: [{"id": 1, "start": 0, "duration": 300,
                                           "playable": True}],
            time_provider=lambda d: (1000.0, 1000.2),
            baseline_store=self.bs)

    def test_clean_run(self):
        s = self.runner.run(policy=config.POLICY_DAILY)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["bad"], 0, s["bad_devices"])
        self.assertEqual(s["online_rate"], 1.0)

    def test_stream_fail_marks_offline(self):
        r = patrol_mod.PatrolRunner(self.store,
                                    frame_provider=lambda d: (False, None, "取流失败"),
                                    baseline_store=self.bs)
        ck = r.check_device(self.d1)
        self.assertFalse(ck.online)
        self.assertIn("STREAM_FAIL", [x.code for x in ck.defects])
        self.assertFalse(ck.ok)

    def test_defect_creates_check_record(self):
        blur = cv2.GaussianBlur(self.base, (15, 15), 0)
        r = patrol_mod.PatrolRunner(self.store,
                                    frame_provider=lambda d: (True, blur, "ok"),
                                    baseline_store=self.bs)
        ck = r.check_device(self.d1)
        self.assertIn("BLUR", ck.fail_codes())
        self.assertFalse(ck.ok)

    def test_ledger_problem_reported(self):
        bad = models.Device(code="CAM-9")
        ck = self.runner.check_device(bad)
        self.assertIn("LEDGER_MISMATCH", [x.code for x in ck.defects])

    def test_online_rate_and_kpi(self):
        self.runner.run(policy=config.POLICY_DAILY)
        self.assertEqual(self.store.online_rate()["rate"], 1.0)
        k = self.runner.kpi()
        self.assertEqual(set(k.keys()), set(report_mod.KPI_TEXT))
        self.assertIsNotNone(k["在线率"]["value"])

    def test_schedule_windows(self):
        now = 1_000_000.0
        self.assertTrue(patrol_mod.PatrolRunner.daily_due(now - 31 * 60, now))
        self.assertFalse(patrol_mod.PatrolRunner.daily_due(now - 60, now))
        self.assertTrue(patrol_mod.PatrolRunner.preexam_window(now + 3 * 86400, now)[0])
        self.assertFalse(patrol_mod.PatrolRunner.preexam_window(now + 30 * 86400, now)[0])
        self.assertTrue(patrol_mod.PatrolRunner.before_session_window(now + 600, now)[0])
        self.assertFalse(patrol_mod.PatrolRunner.before_session_window(now - 600, now)[0])

    def test_credibility_downweight(self):
        # 直接构造核查结果，避免依赖图像算法（测试要确定性）
        bad = models.CheckResult(self.d1, config.POLICY_DAILY, online=True,
                                 defects=[models.Defect("BLUR")])
        self.store.save_check(bad)
        good = models.CheckResult(self.d2, config.POLICY_DAILY, online=True, defects=[])
        self.store.save_check(good)
        f_bad, why = self.runner.credibility_factor("CAM-1")
        f_ok, _ = self.runner.credibility_factor("CAM-2")
        self.assertLess(f_bad, 1.0, why)
        self.assertEqual(f_ok, 1.0)
        self.assertGreaterEqual(f_bad, config.CREDIBILITY["min_factor"])
        self.assertEqual(self.runner.credibility_factor("NOPE")[0], 1.0)
        score, f, _ = self.runner.adjust_alert_score("CAM-1", 1.0)
        self.assertAlmostEqual(score, f, places=6)


# ==================== 工单闭环 ====================
class TestWorkOrder(Base):
    def setUp(self):
        Base.setUp(self)
        self.wm = wo_mod.WorkOrderManager(self.store)
        self.dev = self.dev("CAM-1")
        self.ck = models.CheckResult(self.dev, config.POLICY_DAILY, online=True,
                                     defects=[models.Defect("BLUR", {"sharpness": 3.0},
                                                            "糊")])
        self.store.save_check(self.ck)
        self.after = os.path.join(_TMP, "after.jpg")
        diagnose.imwrite_unicode(self.after, self.base)

    def test_create_and_dedupe(self):
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.assertIsNotNone(wo)
        wo2, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.assertEqual(wo.order_id, wo2.order_id)

    def test_no_order_when_clean(self):
        ok_ck = models.CheckResult(self.dev, config.POLICY_DAILY, online=True, defects=[])
        self.assertIsNone(self.wm.ensure_from_check(ok_ck, self.dev)[0])

    def test_full_closed_loop(self):
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.assertFalse(self.wm.rectify(wo.order_id, None, "x", "op")[0])
        self.assertFalse(self.wm.dispatch(wo.order_id, "", "op")[0])
        self.assertTrue(self.wm.dispatch(wo.order_id, "李工", "op")[0])
        self.assertFalse(self.wm.rectify(wo.order_id, None, "无图", "op")[0])
        self.assertTrue(self.wm.rectify(wo.order_id, self.after, "已处理", "op")[0])
        self.assertEqual(self.store.get_order(wo.order_id).status, "rectified")
        self.assertTrue(self.wm.review(wo.order_id, True, "已清晰", "op")[0])
        self.assertEqual(self.store.get_order(wo.order_id).status, "closed")

    def test_review_failure_reopens(self):
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.wm.dispatch(wo.order_id, "李工", "op")
        self.wm.rectify(wo.order_id, self.after, "已处理", "op")
        self.assertFalse(self.wm.review(wo.order_id, False, "仍糊", "op")[0])
        self.assertEqual(self.store.get_order(wo.order_id).status, "dispatched")

    def test_status_guard(self):
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.assertFalse(self.wm.review(wo.order_id, True, "x", "op")[0])   # 未整改

    def test_bad_attachment_rejected(self):
        bad = os.path.join(_TMP, "x.exe")
        open(bad, "wb").write(b"MZ")
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.wm.dispatch(wo.order_id, "李工", "op")
        self.assertFalse(self.wm.rectify(wo.order_id, bad, "x", "op")[0])

    def test_stats(self):
        wo, _ = self.wm.ensure_from_check(self.ck, self.dev)
        self.wm.dispatch(wo.order_id, "李工", "op")
        self.wm.rectify(wo.order_id, self.after, "ok", "op")
        self.wm.review(wo.order_id, True, "ok", "op")
        s = self.wm.stats()
        self.assertEqual(s["closed"], 1)
        self.assertEqual(s["close_rate"], 1.0)


# ==================== 报告与审计 ====================
class TestReport(Base):
    def test_report_sections(self):
        d = self.dev("CAM-1", "A-01")
        self.store.heartbeat(d.device_id)
        r = patrol_mod.PatrolRunner(self.store,
                                    frame_provider=lambda x: (True, self.base, "ok"))
        r.run(policy=config.POLICY_DAILY)
        rep = report_mod.Reporter(self.store, runner=r,
                                  orders=wo_mod.WorkOrderManager(self.store))
        text = rep.full_report()
        for section in ("关键指标", "缺陷汇总", "整改闭环", "逐考场报告", "验收证据清单"):
            self.assertIn(section, text)
        self.assertIn("CAM-1", rep.room_report("A-01"))
        path = rep.export(text, name="t_report.md")
        self.assertTrue(os.path.exists(path))

    def test_kpi_not_faked_when_empty(self):
        r = patrol_mod.PatrolRunner(self.store)
        k = r.kpi()
        self.assertIsNone(k["在线率"]["value"])
        self.assertFalse(k["在线率"]["pass"])          # 无样本 -> 不算达标

    def test_audit_chain(self):
        self.store.log("a", "1", "op")
        self.store.log("b", "2", "op")
        ok, msg, n = self.store.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        with self.store._conn() as c:
            c.execute("UPDATE audit SET detail='HACK' WHERE seq=1")
        self.assertFalse(self.store.verify_chain()[0])

    def test_defect_stats(self):
        d = self.dev("CAM-1", "A-01")
        ck = models.CheckResult(d, config.POLICY_DAILY, online=True,
                                defects=[models.Defect("BLUR"), models.Defect("MOSAIC")])
        self.store.save_check(ck)
        stats = {s["code"]: s for s in self.store.defect_stats()}
        self.assertEqual(stats["BLUR"]["count"], 1)
        self.assertEqual(stats["BLUR"]["device_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
