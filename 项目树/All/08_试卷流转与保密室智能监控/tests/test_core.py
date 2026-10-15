# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 单元测试（核心业务）

运行：python -m unittest discover -s tests -t .
全程在临时数据目录里跑（EXAM_SECURE_HOME），不碰真实 data\\。
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests import shared_tmp      # noqa: E402

_TMP = shared_tmp()

import alarm_hub      # noqa: E402
import config         # noqa: E402
import flow           # noqa: E402
import models         # noqa: E402
import report         # noqa: E402
import storage        # noqa: E402
import vault          # noqa: E402


def tearDownModule():
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(_TMP, "t_%s.db" % self.id().split(".")[-1])
        if os.path.exists(self.db):
            os.remove(self.db)
        self.store = storage.Store(self.db)
        self.hub = alarm_hub.AlarmHub(self.store)
        self.cams = vault.CameraManager(self.store)
        self.vault = vault.Vault(self.store, self.hub, self.cams)
        self.flow = flow.FlowManager(self.store, self.hub)

    def tearDown(self):
        try:
            if os.path.exists(self.db):
                os.remove(self.db)
        except OSError:
            pass


# ==================== 一、保密室：无死角视频 ====================
class TestCoverage(Base):
    def _fill_room(self, room="SAFE-A", faces=None, channels=("main", "backup")):
        faces = faces or config.COVER_FACES
        for f in faces:
            self.cams.register("C-%s-%s" % (room, f), room, "机位" + f, f,
                               url="rtsp://x/%s" % f,
                               checkpoints=config.FACE_CHECKPOINTS[f],
                               channels=list(channels))
        return room

    def test_full_coverage_100(self):
        self._fill_room()
        rate, detail = self.cams.coverage("SAFE-A")
        self.assertAlmostEqual(rate, 1.0, places=6)
        self.assertEqual(self.cams.blind_spots("SAFE-A"), [])

    def test_missing_face_is_blind_spot(self):
        self._fill_room(faces=[f for f in config.COVER_FACES if f != "top"])
        rate, _ = self.cams.coverage("SAFE-A")
        self.assertAlmostEqual(rate, 5 / 6.0, places=6)
        self.assertEqual(self.cams.blind_spots("SAFE-A"),
                         [config.FACE_TEXT["top"]])

    def test_no_url_counts_as_not_covered(self):
        """画在纸上不算覆盖：有登记但没取流地址，仍算盲区。"""
        for f in config.COVER_FACES:
            self.cams.register("C-%s" % f, "SAFE-A", "机位" + f, f, url="",
                               checkpoints=config.FACE_CHECKPOINTS[f],
                               channels=["main", "backup"])
        rate, _ = self.cams.coverage("SAFE-A")
        self.assertAlmostEqual(rate, 0.0, places=6)

    def test_invalid_face_rejected(self):
        with self.assertRaises(ValueError):
            self.cams.register("C1", "SAFE-A", "X", "ceiling", url="rtsp://x")

    def test_ledger_problems_detects_missing_channel(self):
        self.cams.register("C1", "SAFE-A", "前墙", "front", url="rtsp://x",
                           checkpoints=config.FACE_CHECKPOINTS["front"],
                           channels=["main"])
        probs = self.store.get_camera("C1").ledger_problems()
        self.assertTrue(any("冗余" in p for p in probs), probs)

    def test_ledger_problems_detects_missing_checkpoint(self):
        self.cams.register("C1", "SAFE-A", "前墙", "front", url="rtsp://x",
                           checkpoints=["门禁出入口"], channels=["main", "backup"])
        probs = self.store.get_camera("C1").ledger_problems()
        self.assertTrue(any("必拍要点" in p for p in probs), probs)


# ==================== 二、门禁双因子 / 双人双锁 ====================
class TestAccess(Base):
    def test_correct_two_factors_plus_two_persons(self):
        ok, ev = self.vault.access.request_open(
            "SAFE-A", "张三", factors=["face", "card"], persons=["张三", "李四"])
        self.assertTrue(ok)
        self.assertEqual(ev.result, "allow")
        self.assertEqual(ev.deny_reason, "")

    def test_password_counts_as_second_factor(self):
        ok, _ = self.vault.access.request_open(
            "SAFE-A", "张三", factors=["face", "password"], persons=["张三", "李四"])
        self.assertTrue(ok)

    def test_missing_biometric_denied(self):
        ok, ev = self.vault.access.request_open(
            "SAFE-A", "张三", factors=["card", "password"], persons=["张三", "李四"])
        self.assertFalse(ok)
        self.assertIn("双因子", ev.deny_reason)

    def test_missing_credential_denied(self):
        ok, ev = self.vault.access.request_open(
            "SAFE-A", "张三", factors=["face"], persons=["张三", "李四"])
        self.assertFalse(ok)

    def test_dual_custody_requires_two_persons(self):
        ok, ev = self.vault.access.request_open(
            "SAFE-A", "张三", factors=["face", "card"], persons=["张三"])
        self.assertFalse(ok)
        self.assertIn("双人双锁", ev.deny_reason)

    def test_denied_produces_red_alarm_with_latency(self):
        ep = time.time() - 2.0
        self.vault.access.request_open("SAFE-A", "张三", factors=["card"],
                                       persons=["张三"], event_epoch=ep)
        alarms = self.store.alarms(code="ACCESS_DUAL_FAIL")
        self.assertEqual(len(alarms), 1)
        self.assertTrue(alarms[0].is_red())
        self.assertIsNotNone(alarms[0].latency_sec)
        self.assertGreaterEqual(alarms[0].latency_sec, 2.0)

    def test_abnormal_door_open_alarm(self):
        self.vault.access.report_abnormal_open("SAFE-A", "张三", "门磁异常")
        a = self.store.alarms(code="DOOR_OPEN_ABNORMAL")[0]
        self.assertTrue(a.is_red())

    def test_tailgating_reported(self):
        self.vault.access.request_open("SAFE-A", "张三", factors=["face", "card"],
                                       persons=["张三", "李四"], tailgating=True)
        self.assertEqual(len(self.store.alarms(code="ACCESS_TAILGATING")), 1)


# ==================== 三、入侵探测 ====================
class TestIntrusion(Base):
    def test_armed_window_night(self):
        t = time.mktime(time.strptime("2026-10-14 23:30", "%Y-%m-%d %H:%M"))
        self.assertTrue(vault.IntrusionMonitor.in_armed_window(t))

    def test_armed_window_early_morning(self):
        t = time.mktime(time.strptime("2026-10-14 03:00", "%Y-%m-%d %H:%M"))
        self.assertTrue(vault.IntrusionMonitor.in_armed_window(t))

    def test_disarmed_window_day(self):
        t = time.mktime(time.strptime("2026-10-14 12:00", "%Y-%m-%d %H:%M"))
        self.assertFalse(vault.IntrusionMonitor.in_armed_window(t))

    def test_all_kinds_when_armed(self):
        expect = {"ir_dual": "INTRUSION_IR", "door_mag": "INTRUSION_DOOR_MAG",
                  "vibration": "INTRUSION_VIBRATION", "safe_open": "SAFE_OPEN_ABNORMAL"}
        for kind, code in expect.items():
            self.store._query("DELETE FROM alarms")
            _, alarm = self.vault.intrusion.trigger("SAFE-A", kind, armed=True)
            self.assertIsNotNone(alarm, kind)
            self.assertEqual(alarm.code, code)

    def test_disarmed_no_alarm_except_safe_open(self):
        _, a1 = self.vault.intrusion.trigger("SAFE-A", "ir_dual", armed=False)
        self.assertIsNone(a1)
        _, a2 = self.vault.intrusion.trigger("SAFE-A", "safe_open", armed=False)
        self.assertIsNotNone(a2)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.vault.intrusion.trigger("SAFE-A", "laser")

    def test_manual_arm_override(self):
        self.vault.intrusion.set_armed("SAFE-A", True, operator="tester")
        self.assertTrue(self.vault.intrusion.is_armed("SAFE-A"))
        self.vault.intrusion.set_armed("SAFE-A", False, operator="tester")
        self.assertFalse(self.vault.intrusion.is_armed("SAFE-A"))


# ==================== 四、试卷柜电子锁 ====================
class TestSafe(Base):
    def test_authorized_open_is_orange(self):
        _, alarms = self.vault.safes.open_request("SAFE-A", "S1", "张三",
                                                  authorized=True)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].code, "SAFE_OPEN_NORMAL")
        self.assertFalse(alarms[0].is_red())

    def test_unauthorized_open_is_red(self):
        _, alarms = self.vault.safes.open_request("SAFE-A", "S1", "张三",
                                                  authorized=False)
        a = [x for x in alarms if x.code == "SAFE_OPEN_ABNORMAL"][0]
        self.assertTrue(a.is_red())

    def test_paper_missing_alarm(self):
        _, alarms = self.vault.safes.open_request(
            "SAFE-A", "S1", "张三", authorized=True,
            inner={"paper_present": False})
        self.assertTrue(any(a.code == "SAFE_OPEN_ABNORMAL" for a in alarms))

    def test_tamper_alarm(self):
        _, alarms = self.vault.safes.open_request(
            "SAFE-A", "S1", "张三", authorized=True, inner={"tamper": True})
        self.assertTrue(any("撬动" in a.detail for a in alarms))

    def test_ajar_state_warns(self):
        _, alarms = self.vault.safes.report_state("SAFE-A", "S1", "ajar")
        self.assertEqual(alarms[0].code, "SAFE_AJAR")

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            self.vault.safes.report_state("SAFE-A", "S1", "half")

    def test_inner_humidity_violation(self):
        _, alarms = self.vault.safes.open_request(
            "SAFE-A", "S1", "张三", authorized=True, inner={"humidity": 95.0})
        self.assertTrue(any(a.code == "ENV_HUMID_HIGH" for a in alarms))

    def test_last_state(self):
        self.vault.safes.report_state("SAFE-A", "S1", "open")
        self.assertEqual(self.vault.safes.last_state("SAFE-A", "S1").state, "open")


# ==================== 五、环境监测 ====================
class TestEnv(Base):
    def test_normal_no_alarm(self):
        _, alarms = self.vault.env.sample("SAFE-A", temp_c=25.0, humidity_pct=45.0)
        self.assertEqual(alarms, [])

    def test_temp_high_and_low(self):
        _, hi = self.vault.env.sample("SAFE-A", temp_c=41.0)
        self.assertTrue(any(a.code == "ENV_TEMP_HIGH" for a in hi))
        self.store._query("DELETE FROM alarms")
        _, lo = self.vault.env.sample("SAFE-A", temp_c=-3.0)
        self.assertTrue(any(a.code == "ENV_TEMP_LOW" for a in lo))

    def test_humidity_both_sides(self):
        _, hi = self.vault.env.sample("SAFE-A", humidity_pct=88.0)
        self.assertTrue(any(a.code == "ENV_HUMID_HIGH" for a in hi))
        self.store._query("DELETE FROM alarms")
        _, lo = self.vault.env.sample("SAFE-A", humidity_pct=5.0)
        self.assertTrue(any(a.code == "ENV_HUMID_LOW" for a in lo))

    def test_water_smoke_are_red(self):
        _, alarms = self.vault.env.sample("SAFE-A", water=True, smoke=True)
        for a in alarms:
            self.assertTrue(a.is_red())
        self.assertEqual(len(self.store.alarms()), 2)

    def test_threshold_boundary_not_violation(self):
        t = config.ENV_THRESHOLDS["temp_c"]
        s = models.EnvSample("R", temp_c=t["max"], humidity_pct=45.0)
        self.assertEqual(s.violations(), [])


# ==================== 六、录像留存与完整性 ====================
class TestRecords(Base):
    def test_continuous_segments_ok(self):
        base = time.time() - 7200
        for i in range(2):
            self.vault.records.add_segment("C1", base + i * 3600, 3600.0)
        r = self.vault.records.continuity("C1", "main")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["segments"], 2)

    def test_gap_detected(self):
        base = time.time() - 7200
        self.vault.records.add_segment("C1", base, 3600.0)
        self.vault.records.add_segment("C1", base + 3600 + 600, 3600.0)
        r = self.vault.records.continuity("C1", "main")
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["gaps"]), 1)
        self.assertAlmostEqual(r["gaps"][0]["gap_sec"], 600.0, places=1)

    def test_unplayable_segment_detected(self):
        base = time.time() - 3600
        self.vault.records.add_segment("C1", base, 3600.0, playable=False)
        r = self.vault.records.continuity("C1", "main")
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["unplayable"]), 1)

    def test_overlap_flagged(self):
        base = time.time() - 7200
        self.vault.records.add_segment("C1", base, 3600.0)
        self.vault.records.add_segment("C1", base + 1000, 3600.0)
        r = self.vault.records.continuity("C1", "main")
        self.assertFalse(r["ok"])
        self.assertTrue(any("重叠" in (g.get("why") or "") for g in r["gaps"]))

    def test_redundancy_requires_two_channels(self):
        self.vault.records.add_segment("C1", time.time() - 3600, 3600.0, channel="main")
        ok, chs = self.vault.records.redundancy_ok("C1")
        self.assertFalse(ok)
        self.vault.records.add_segment("C1", time.time() - 3600, 3600.0,
                                       channel="backup")
        ok2, chs2 = self.vault.records.redundancy_ok("C1")
        self.assertTrue(ok2)
        self.assertEqual(chs2, ["backup", "main"])

    def test_retention_worm_locked(self):
        seg = self.vault.records.add_segment("C1", time.time() - 3600, 3600.0)
        self.assertTrue(seg.locked)
        ret = self.vault.records.retention_report()
        self.assertEqual(ret["retain_days"], 180)
        self.assertEqual(ret["unlocked"], 0)

    def test_check_all_raises_alarm_on_gap(self):
        self.cams.register("C1", "SAFE-A", "前墙", "front", url="rtsp://x",
                           checkpoints=config.FACE_CHECKPOINTS["front"],
                           channels=["main", "backup"])
        base = time.time() - 7200
        self.vault.records.add_segment("C1", base, 3600.0, channel="main")
        self.vault.records.add_segment("C1", base + 4000, 3600.0, channel="main")
        probs = self.vault.records.check_all("SAFE-A")
        self.assertTrue(probs)
        self.assertEqual(len(self.store.alarms(code="VIDEO_RECORD_BREAK")), 1)


# ==================== 七、试卷流转 ====================
class TestFlowBag(Base):
    def test_register_and_trace(self):
        self.flow.register_bag("B1", subject="语文", quantity=30)
        self.assertEqual(self.store.get_bag("B1").quantity, 30)
        t = self.flow.bag_trace("B1")
        self.assertIsNotNone(t)
        self.assertEqual(t["bag"].bag_id, "B1")

    def test_bag_trace_missing(self):
        self.assertIsNone(self.flow.bag_trace("NOPE"))


class TestHandover(Base):
    def test_complete_handover_traceable(self):
        h, alarms = self.flow.handover.handover(
            "B1", "甲", "乙", persons=["甲", "乙"], signatures=["甲", "乙"],
            faces=["甲", "乙"], method="rfid", quantity=30, room_id="SAFE-A")
        self.assertTrue(h.complete())
        self.assertEqual(alarms, [])

    def test_missing_person_alarm(self):
        h, alarms = self.flow.handover.handover(
            "B1", "甲", "", persons=["甲"], signatures=["甲"], faces=["甲"],
            method="scan", room_id="SAFE-A")
        self.assertFalse(h.complete())
        self.assertTrue(any(a.code == "HANDOVER_PERSON_MISS" for a in alarms))

    def test_missing_face_alarm_is_red(self):
        h, alarms = self.flow.handover.handover(
            "B1", "甲", "乙", persons=["甲", "乙"], signatures=["甲", "乙"],
            faces=["甲"], method="scan", room_id="SAFE-A")
        a = [x for x in alarms if x.code == "HANDOVER_FACE_MISS"][0]
        self.assertTrue(a.is_red())

    def test_missing_signature_alarm(self):
        _, alarms = self.flow.handover.handover(
            "B1", "甲", "乙", persons=["甲", "乙"], signatures=["甲"],
            faces=["甲", "乙"], method="scan", room_id="SAFE-A")
        self.assertTrue(any(a.code == "HANDOVER_INCOMPLETE" for a in alarms))

    def test_invalid_method_alarm(self):
        _, alarms = self.flow.handover.handover(
            "B1", "甲", "乙", persons=["甲", "乙"], signatures=["甲", "乙"],
            faces=["甲", "乙"], method="sms", room_id="SAFE-A")
        self.assertTrue(any("扫码/RFID" in a.detail for a in alarms))

    def test_completeness_rate(self):
        self.flow.handover.handover("B1", "甲", "乙", persons=["甲", "乙"],
                                    signatures=["甲", "乙"], faces=["甲", "乙"],
                                    method="scan", room_id="R1")
        self.flow.handover.handover("B2", "甲", "乙", persons=["甲"],
                                    signatures=["甲"], faces=["甲"],
                                    method="scan", room_id="R1")
        rate, ok, total = self.flow.handover.completeness_rate()
        self.assertEqual((ok, total), (1, 2))
        self.assertAlmostEqual(rate, 0.5)

    def test_completeness_rate_no_samples(self):
        rate, ok, total = self.flow.handover.completeness_rate()
        self.assertIsNone(rate)
        self.assertEqual((ok, total), (0, 0))

    def test_history_appended(self):
        self.flow.handover.handover("B1", "甲", "乙", persons=["甲", "乙"],
                                    signatures=["甲", "乙"], faces=["甲", "乙"],
                                    method="scan", room_id="R1")
        self.assertEqual(len(self.store.get_bag("B1").history), 1)


class TestSeal(Base):
    def test_seal_sets_state(self):
        self.flow.register_bag("B1", quantity=10)
        b = self.flow.seal.seal("B1", "S-1", "甲")
        self.assertEqual(b.seal_state, "sealed")
        self.assertEqual(b.seal_by, "甲")

    def test_broken_raises_red(self):
        _, a = self.flow.seal.report("B1", "broken", person="甲", detail="演练")
        self.assertIsNotNone(a)
        self.assertTrue(a.is_red())

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            self.flow.seal.report("B1", "melted")

    def test_intact_and_broken_lists(self):
        self.flow.seal.seal("B1", "S-1", "甲")
        self.flow.seal.report("B2", "broken")
        self.assertEqual([b.bag_id for b in self.flow.seal.intact_bags()], ["B1"])
        self.assertEqual([b.bag_id for b in self.flow.seal.broken_bags()], ["B2"])


class TestEscort(Base):
    ROUTE = [(39.9042, 116.4074), (39.9100, 116.4200), (39.9200, 116.4400)]

    def _task(self):
        return self.flow.escort.start_task("京A1", driver="赵", escort_persons=["赵"],
                                           bag_ids=["B1"], route=self.ROUTE,
                                           video_url="rtsp://car")

    def test_start_requires_plate_and_persons(self):
        with self.assertRaises(ValueError):
            self.flow.escort.start_task("", escort_persons=["赵"])
        with self.assertRaises(ValueError):
            self.flow.escort.start_task("京A1", escort_persons=[])

    def test_on_route_no_alarm(self):
        t = self._task()
        p, a = self.flow.escort.report_position(t.task_id, 39.9045, 116.4078,
                                               speed_kmh=40)
        self.assertIsNone(a)
        self.assertFalse(p.deviated)
        self.assertLess(p.offset_m, config.ESCORT_DEVIATE_METERS)

    def test_off_route_raises_red_with_latency(self):
        t = self._task()
        ep = time.time() - 3.0
        p, a = self.flow.escort.report_position(t.task_id, 39.99, 116.60,
                                               speed_kmh=50, event_epoch=ep)
        self.assertTrue(p.deviated)
        self.assertIsNotNone(a)
        self.assertTrue(a.is_red())
        self.assertLessEqual(a.latency_sec, config.ESCORT_DEVIATE_SLA_SEC)

    def test_deviation_marks_task_abnormal(self):
        t = self._task()
        self.flow.escort.report_position(t.task_id, 39.99, 116.60)
        self.assertEqual(self.store.get_escort_task(t.task_id).status, "abnormal")

    def test_video_loss_raises_red(self):
        t = self._task()
        a = self.flow.escort.report_video(t.task_id, False)
        self.assertTrue(a.is_red())
        self.assertIsNone(self.flow.escort.report_video(t.task_id, True))

    def test_position_on_missing_task_raises(self):
        with self.assertRaises(ValueError):
            self.flow.escort.report_position("NOPE", 1.0, 1.0)

    def test_stop_too_long(self):
        t = self._task()
        base = time.time() - 1200
        for i in range(3):
            self.flow.escort.report_position(t.task_id, 39.9045, 116.4078,
                                             speed_kmh=0.0,
                                             event_epoch=base + i * 600)
        a = self.flow.escort.check_stop(t.task_id)
        self.assertIsNotNone(a)
        self.assertEqual(a.code, "ESCORT_STOP_TOO_LONG")

    def test_finish_task(self):
        t = self._task()
        got = self.flow.escort.finish_task(t.task_id)
        self.assertEqual(got.status, "done")
        self.assertTrue(got.end_ts)

    def test_task_summary(self):
        t = self._task()
        self.flow.escort.report_position(t.task_id, 39.9045, 116.4078, speed_kmh=40)
        self.flow.escort.report_position(t.task_id, 39.99, 116.60, speed_kmh=50)
        s = self.flow.escort.task_summary(t.task_id)
        self.assertEqual(s["points"], 2)
        self.assertEqual(s["deviated_points"], 1)
        self.assertGreater(s["max_offset_m"], config.ESCORT_DEVIATE_METERS)

    def test_haversine_known_distance(self):
        d = flow.hav_distance_m(39.9042, 116.4074, 39.9042, 116.4074)
        self.assertAlmostEqual(d, 0.0, places=3)
        d2 = flow.hav_distance_m(0, 0, 0, 1)
        self.assertAlmostEqual(d2, 111195.0, delta=500.0)

    def test_point_to_polyline_none_when_no_route(self):
        self.assertIsNone(flow.point_to_polyline_m(1.0, 1.0, []))

    def test_point_to_polyline_on_vertex(self):
        d = flow.point_to_polyline_m(self.ROUTE[1][0], self.ROUTE[1][1], self.ROUTE)
        self.assertLess(d, 1.0)


class TestDeliveryRecycle(Base):
    def test_delivery_ok(self):
        d, alarms = self.flow.delivery.deliver("B1", "R001", 30, "甲")
        self.assertEqual(alarms, [])
        self.assertEqual(d.problems(), [])

    def test_delivery_missing_fields_alarm(self):
        _, alarms = self.flow.delivery.deliver("", "", 0, "")
        self.assertGreaterEqual(len(alarms), 3)

    def test_delivery_covered_check(self):
        self.flow.delivery.deliver("B1", "R001", 30, "甲")
        c = self.flow.delivery.covered()
        self.assertTrue(c["ok"])
        self.assertEqual(c["total"], 1)

    def test_recycle_match_no_alarm(self):
        r, alarms = self.flow.recycle.recycle("B1", "R001", 30, 30, person="甲",
                                              seal_id="S-R1")
        self.assertEqual(r.diff, 0)
        self.assertEqual(alarms, [])
        self.assertTrue(r.sealed)

    def test_recycle_mismatch_alarm(self):
        r, alarms = self.flow.recycle.recycle("B1", "R001", 30, 28, person="甲",
                                              seal_id="S-R1")
        self.assertEqual(r.diff, -2)
        self.assertTrue(any(a.code == "RECYCLE_COUNT_MISMATCH" for a in alarms))

    def test_recycle_not_sealed_alarm(self):
        _, alarms = self.flow.recycle.recycle("B1", "R001", 30, 30, auto_seal=False)
        self.assertTrue(any(a.code == "RECYCLE_MISSING" for a in alarms))

    def test_recycle_seals_bag(self):
        self.flow.recycle.recycle("B1", "R001", 30, 30, person="甲", seal_id="S-R1")
        self.assertEqual(self.store.get_bag("B1").seal_state, "sealed")

    def test_diff_rate(self):
        self.flow.recycle.recycle("B1", "R1", 30, 30)
        self.flow.recycle.recycle("B2", "R1", 30, 25)
        rate, bad, total = self.flow.recycle.diff_rate()
        self.assertEqual((bad, total), (1, 2))
        self.assertAlmostEqual(rate, 0.5)


class TestStageCoverage(Base):
    def test_all_stages_present_in_definition(self):
        cov = self.flow.stage_coverage()
        for k in config.FLOW_STAGES:
            self.assertIn(k, cov)

    def test_counts_reflect_records(self):
        self.flow.handover.handover("B1", "甲", "乙", persons=["甲", "乙"],
                                    signatures=["甲", "乙"], faces=["甲", "乙"],
                                    method="scan", room_id="R1")
        self.flow.delivery.deliver("B1", "R1", 30, "甲")
        self.flow.recycle.recycle("B1", "R1", 30, 30)
        cov = self.flow.stage_coverage()
        self.assertEqual(cov["handover"]["count"], 1)
        self.assertEqual(cov["deliver"]["count"], 1)
        self.assertEqual(cov["recycle"]["count"], 1)


# ==================== 八、值守替代 ====================
class TestPatrol(Base):
    def test_round_recorded(self):
        rec = self.vault.patrol.patrol_round("SAFE-A", by="robot", result="ok")
        self.assertEqual(len(self.vault.patrol.rounds("SAFE-A")), 1)
        self.assertEqual(self.vault.patrol.last_round_epoch("SAFE-A"), rec["epoch"])

    def test_due_when_no_round(self):
        a = self.vault.patrol.check_due("SAFE-A", interval_sec=1800)
        self.assertIsNotNone(a)

    def test_not_due_right_after_round(self):
        self.vault.patrol.patrol_round("SAFE-A")
        self.assertIsNone(self.vault.patrol.check_due("SAFE-A", interval_sec=1800))

    def test_due_when_stale(self):
        self.vault.patrol.patrol_round("SAFE-A", event_epoch=time.time() - 7200)
        a = self.vault.patrol.check_due("SAFE-A", interval_sec=1800)
        self.assertIsNotNone(a)
        self.assertIn("值守缺位", a.detail)


# ==================== 九、告警中心 ====================
class TestAlarmHub(Base):
    def test_latency_recorded(self):
        ep = time.time() - 4.5
        a = self.hub.raise_alarm("DOOR_OPEN_ABNORMAL", source_kind="door",
                                 scope="SAFE-A", event_epoch=ep)
        self.assertIsNotNone(a.latency_sec)
        self.assertGreaterEqual(a.latency_sec, 4.4)

    def test_no_event_epoch_means_no_latency(self):
        a = self.hub.raise_alarm("DOOR_OPEN_ABNORMAL", scope="SAFE-A")
        self.assertIsNone(a.latency_sec)

    def test_dedup_same_code_scope(self):
        for _ in range(3):
            self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.assertEqual(len(self.store.alarms(code="ENV_WATER")), 1)

    def test_dedup_allows_different_scope(self):
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-B")
        self.assertEqual(len(self.store.alarms(code="ENV_WATER")), 2)

    def test_dedup_after_handled(self):
        a = self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.hub.handle(a.alarm_id, "tester", "已处置")
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.assertEqual(len(self.store.alarms(code="ENV_WATER")), 2)

    def test_create_alarm_reports_created_flag(self):
        _, c1 = self.hub.create_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        _, c2 = self.hub.create_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.assertTrue(c1)
        self.assertFalse(c2)

    def test_ack_and_handle(self):
        a = self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.assertTrue(self.hub.ack(a.alarm_id, "tester", "看一下"))
        self.assertEqual(self.store.get_alarm(a.alarm_id).status, models.ST_ACKED)
        self.assertTrue(self.hub.handle(a.alarm_id, "tester", "已清理"))
        self.assertEqual(self.store.get_alarm(a.alarm_id).status, models.ST_HANDLED)

    def test_ack_all_red(self):
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.hub.raise_alarm("ENV_SMOKE", source_kind="env", scope="SAFE-A")
        self.hub.raise_alarm("DOOR_OPEN_NORMAL", source_kind="door", scope="SAFE-A")
        n = self.hub.ack_all_red("tester")
        self.assertEqual(n, 2)

    def test_open_red_count(self):
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="SAFE-A")
        self.assertEqual(self.hub.open_red_count(), 1)

    def test_unknown_code_defaults_orange(self):
        a = self.hub.raise_alarm("SOME_NEW_CODE", scope="SAFE-A")
        self.assertEqual(a.level, config.LEVEL_ORANGE)

    def test_latency_stats_only_real_samples(self):
        self.hub.raise_alarm("DOOR_OPEN_ABNORMAL", event_epoch=time.time() - 1.0)
        self.hub.raise_alarm("DOOR_OPEN_ABNORMAL")          # 无时延样本
        stats = self.hub.latency_stats(("DOOR_OPEN_ABNORMAL",))
        self.assertEqual(stats["DOOR_OPEN_ABNORMAL"]["n"], 1)


# ==================== 十、指标与报告 ====================
class TestKPI(Base):
    def _full_room(self):
        for f in config.COVER_FACES:
            self.cams.register("C-%s" % f, "SAFE-A", "机位" + f, f,
                               url="rtsp://x/%s" % f,
                               checkpoints=config.FACE_CHECKPOINTS[f],
                               channels=["main", "backup"])

    def test_coverage_kpi_pass_when_full(self):
        self._full_room()
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-A") if r["key"] == "coverage_rate"][0]
        self.assertEqual(row["verdict"], "达标")
        self.assertEqual(row["actual_text"], "100.0%")

    def test_coverage_kpi_fail_when_partial(self):
        self.cams.register("C1", "SAFE-A", "前墙", "front", url="rtsp://x",
                           checkpoints=config.FACE_CHECKPOINTS["front"],
                           channels=["main", "backup"])
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-A") if r["key"] == "coverage_rate"][0]
        self.assertEqual(row["verdict"], "未达标")

    def test_insufficient_sample_not_counted_as_pass(self):
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-A") if r["key"] == "alarm_latency"][0]
        self.assertEqual(row["verdict"], "样本不足")
        self.assertIsNone(row["actual"])

    def test_empty_room_coverage_is_insufficient_not_fail(self):
        """一间还没登记任何机位的保密室：全局 0.0 既不叫"达标"也不叫"未达标"，
        而应如实标"样本不足"——这是本仓库一贯口径。"""
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("EMPTY-ROOM") if r["key"] == "coverage_rate"][0]
        self.assertEqual(row["verdict"], "样本不足")
        self.assertIsNone(row["actual"])
        self.assertIn("尚未登记", row["how"])

    def test_partial_room_coverage_is_fail(self):
        """登记了机位但覆盖不全 -> 才是真正的"未达标"。"""
        self.cams.register("C1", "SAFE-B", "前墙", "front", url="rtsp://x",
                           checkpoints=config.FACE_CHECKPOINTS["front"],
                           channels=["main", "backup"])
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-B") if r["key"] == "coverage_rate"][0]
        self.assertEqual(row["verdict"], "未达标")

    def test_handover_kpi_is_room_scoped(self):
        """有房间把交接记录记在 SAFE-A，问 SAFE-B 的完整率不能拿 SAFE-A 的数据顶。"""
        self.flow.register_bag("B1", quantity=5, room_id="SAFE-A")
        self.flow.handover.handover("B1", "甲", "乙",
                                    persons=["甲", "乙"],
                                    signatures=["甲", "乙"],
                                    faces=["甲", "乙"], method="scan",
                                    quantity=5, room_id="SAFE-A")
        k = report.KPIReport(self.store, self.vault, self.flow)
        a = [r for r in k.kpi("SAFE-A") if r["key"] == "handover_complete"][0]
        b = [r for r in k.kpi("SAFE-B") if r["key"] == "handover_complete"][0]
        self.assertIsNotNone(a["actual"])
        self.assertIsNone(b["actual"])
        self.assertEqual(b["verdict"], "样本不足")

    def test_latency_kpi_measured(self):
        self.hub.raise_alarm("SAFE_OPEN_ABNORMAL", source_kind="safe",
                             scope="SAFE-A", event_epoch=time.time() - 2.0)
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-A") if r["key"] == "alarm_latency"][0]
        self.assertEqual(row["verdict"], "达标")
        self.assertLessEqual(row["actual"], 5.0)

    def test_escort_kpi_measured(self):
        self.hub.raise_alarm("ESCORT_ROUTE_DEVIATE", source_kind="flow",
                             scope="京A1", event_epoch=time.time() - 5.0)
        k = report.KPIReport(self.store, self.vault, self.flow)
        row = [r for r in k.kpi("SAFE-A") if r["key"] == "escort_deviate_latency"][0]
        self.assertEqual(row["verdict"], "达标")

    def test_summary_counts(self):
        k = report.KPIReport(self.store, self.vault, self.flow)
        s = k.summary("SAFE-A")
        self.assertEqual(s["total"], len(config.KPI_DEFS))
        self.assertEqual(s["pass"] + s["fail"] + s["insufficient"], s["total"])

    def test_report_contains_all_sections(self):
        self._full_room()
        er = report.EvidenceReport(self.store, self.vault, self.flow)
        text = er.full_report("SAFE-A", generated_by="test")
        for sec in ("【一】保密室无死角核查", "【二】门禁与开柜告警联调测试记录",
                    "【三】交接 / 押运流程演练记录与台账样例",
                    "【四】录像完整性与留存期限配置证明", "【五】关键指标",
                    "【六】告警台账", "【七】审计哈希链校验"):
            self.assertIn(sec, text)

    def test_report_save(self):
        er = report.EvidenceReport(self.store, self.vault, self.flow)
        path, text = er.save_report("SAFE-A", generated_by="test")
        self.assertTrue(os.path.exists(path))
        self.assertGreater(len(text), 1500)

    def test_export_ledgers(self):
        self.flow.register_bag("B1", quantity=5)
        er = report.EvidenceReport(self.store, self.vault, self.flow)
        out = er.export_ledgers()
        self.assertTrue(os.path.exists(out["handovers"]))
        self.assertTrue(os.path.exists(out["alarms"]))


# ==================== 十一、审计哈希链 ====================
class TestAudit(Base):
    def test_chain_valid(self):
        self.store.log("A", "1", "u1")
        self.store.log("B", "2", "u1")
        ok, why = self.store.audit_verify()
        self.assertTrue(ok, why)

    def test_empty_chain_valid(self):
        ok, _ = self.store.audit_verify()
        self.assertTrue(ok)

    def test_tamper_detected(self):
        self.store.log("A", "1", "u1")
        self.store.log("B", "2", "u1")
        with self.store._conn() as c:
            c.execute("UPDATE audit SET detail='X' WHERE seq=1")
        ok, why = self.store.audit_verify()
        self.assertFalse(ok)
        self.assertIn("不符", why)

    def test_genesis_prev_hash(self):
        self.store.log("A", "1", "u1")
        rows = self.store.audit_rows()
        self.assertEqual(rows[0]["prev_hash"], models.GENESIS)

    def test_calc_is_deterministic(self):
        h1 = models.AuditChain.calc(models.GENESIS, "t", "a", "d", "o")
        h2 = models.AuditChain.calc(models.GENESIS, "t", "a", "d", "o")
        self.assertEqual(h1, h2)
        h3 = models.AuditChain.calc(models.GENESIS, "t", "a", "d", "o2")
        self.assertNotEqual(h1, h3)


# ==================== 十二、模型与边界 ====================
class TestModels(Base):
    def test_alarm_default_level_by_code(self):
        a = models.Alarm("SAFE_OPEN_ABNORMAL")
        self.assertEqual(a.level, config.LEVEL_RED)
        self.assertTrue(a.is_red())

    def test_alarm_roundtrip(self):
        a = models.Alarm("ENV_WATER", detail="d", scope="R", extra={"k": 1})
        a.latency_sec = 1.25
        self.store.save_alarm(a)
        got = self.store.get_alarm(a.alarm_id)
        self.assertEqual(got.detail, "d")
        self.assertEqual(got.extra, {"k": 1})
        self.assertEqual(got.latency_sec, 1.25)

    def test_env_violations_multiple(self):
        s = models.EnvSample("R", temp_c=50.0, humidity_pct=95.0, water=True,
                             smoke=True)
        self.assertEqual(len(s.violations()), 4)

    def test_handover_check_lists_all_problems(self):
        h = models.Handover("", "handover", persons=["a"], signatures=[],
                            faces=[], method="x", room_id="")
        codes = [c for c, _ in h.check()]
        self.assertIn("HANDOVER_PERSON_MISS", codes)
        self.assertIn("HANDOVER_FACE_MISS", codes)
        self.assertIn("HANDOVER_INCOMPLETE", codes)

    def test_camera_roundtrip_json(self):
        c = models.Camera("C1", "R1", "n", "front", url="u", angle_deg=12.5,
                          checkpoints=["a"], channels=["main"],
                          ledger={"pos": "x"})
        self.store.save_camera(c)
        got = self.store.get_camera("C1")
        self.assertEqual(got.checkpoints, ["a"])
        self.assertEqual(got.ledger, {"pos": "x"})
        self.assertAlmostEqual(got.angle_deg, 12.5)

    def test_record_segment_end_epoch(self):
        s = models.RecordSegment("C1", 1000.0, 3600.0)
        self.assertAlmostEqual(s.end_epoch(), 4600.0)
        self.assertAlmostEqual(s.retain_until, 1000.0 + 180 * 86400)

    def test_recycle_diff_signed(self):
        self.assertEqual(models.Recycle("B", "R", 30, 31).diff, 1)
        self.assertEqual(models.Recycle("B", "R", 30, 29).diff, -1)


# ==================== 十三、存储 ====================
class TestStorage(Base):
    def test_camera_delete(self):
        self.cams.register("C1", "R1", "n", "front", url="u")
        self.assertEqual(len(self.store.cameras("R1")), 1)
        self.store.delete_camera("C1")
        self.assertEqual(len(self.store.cameras("R1")), 0)

    def test_rooms_listing(self):
        self.cams.register("C1", "R1", "n", "front", url="u")
        self.cams.register("C2", "R2", "n", "front", url="u")
        self.assertEqual(self.store.rooms(), ["R1", "R2"])

    def test_kv_upsert(self):
        self.store.set_kv("k", "v1")
        self.store.set_kv("k", "v2")
        self.assertEqual(self.store.get_kv("k"), "v2")

    def test_set_camera_online(self):
        self.cams.register("C1", "R1", "n", "front", url="u")
        self.store.set_camera_online("C1", True)
        self.assertTrue(self.store.get_camera("C1").online)
        self.store.set_camera_online("C1", False)
        self.assertFalse(self.store.get_camera("C1").online)

    def test_alarm_filters(self):
        self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="R")
        self.hub.raise_alarm("ENV_SMOKE", source_kind="env", scope="R")
        self.assertEqual(len(self.store.alarms(code="ENV_WATER")), 1)
        self.assertEqual(len(self.store.alarms(status=models.ST_PENDING)), 2)

    def test_ack_alarm_no_double_ack(self):
        a = self.hub.raise_alarm("ENV_WATER", source_kind="env", scope="R")
        self.assertEqual(self.store.ack_alarm(a.alarm_id, "u"), 1)
        self.assertEqual(self.store.ack_alarm(a.alarm_id, "u"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
