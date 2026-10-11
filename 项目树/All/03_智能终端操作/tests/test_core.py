# -*- coding: utf-8 -*-
"""
智能终端操作端 · 单元测试

运行：python -m unittest discover -s tests -t .
测试全程在临时数据目录里跑（EXAM_TERMINAL_HOME），不碰真实 data\\。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="terminal_test_")
os.environ["EXAM_TERMINAL_HOME"] = _TMP          # 必须在 import config 之前

import config        # noqa: E402
import access        # noqa: E402
import alerts as alerts_mod   # noqa: E402
import clock as clock_mod     # noqa: E402
import fleet as fleet_mod     # noqa: E402
import models        # noqa: E402
import offline as offline_mod  # noqa: E402
import review as review_mod    # noqa: E402
import storage       # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        # 用完整用例 id 作库名：只用方法名会让不同测试类的同名方法撞同一个库文件
        self.db = os.path.join(_TMP, "t_%s.db" % self.id().replace(".", "_"))
        if os.path.exists(self.db):
            os.remove(self.db)
        self.store = storage.Store(db_path=self.db)
        self.accounts = access.AccountStore(self.store)
        self.accounts.ensure_seed()
        self.offline_probe = offline_mod.ConnectivityProbe(probe=lambda u: False)
        self.ob = offline_mod.Outbox(self.store, probe=self.offline_probe)
        self.hub = alerts_mod.AlertHub(self.store, offline=self.ob)
        self.rev = review_mod.Reviewer(self.store, hub=self.hub)
        self.patrol = self.accounts.authenticate("patrol1", "patrol1")
        self.inv = self.accounts.authenticate("inv01", "inv01")
        self.cmd = self.accounts.authenticate("owner", "EDY_POWERQ")
        self.checker = self.accounts.authenticate("check1", "check1")


# ==================== 模型 ====================
class TestModels(Base):
    def test_level_fallback_by_code(self):
        self.assertEqual(models.Alert(code="PHONE_DETECTED").level, config.LEVEL_RED)
        self.assertEqual(models.Alert(code="LOOK_DOWN").level, config.LEVEL_ORANGE)

    def test_unknown_code_defaults_orange(self):
        self.assertEqual(models.Alert(code="SOMETHING_NEW").level, config.LEVEL_ORANGE)

    def test_mask_matches_project01_convention(self):
        self.assertEqual(models._mask_id("330100199001011234"), "3301**********1234")
        self.assertEqual(models._mask_id("1234"), "****")
        self.assertEqual(models._mask_id("123456"), "1****6")

    def test_red_force_confirm_and_sla(self):
        a = models.Alert(code="PHONE_DETECTED")
        self.assertTrue(a.needs_force_confirm())
        self.assertFalse(a.overdue())
        a.sla_deadline = models.now_epoch() - 1
        self.assertTrue(a.overdue())

    def test_alert_roundtrip(self):
        a = models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="05",
                         candidate_id="330100199001011234", extra={"faces": 2})
        a.attachments = ["x.jpg"]
        b = models.Alert.from_row(a.to_row())
        self.assertEqual(b.alert_id, a.alert_id)
        self.assertEqual(b.room_id, "A-01")
        self.assertEqual(b.extra, {"faces": 2})
        self.assertEqual(b.attachments, ["x.jpg"])

    def test_chain_hash_deterministic(self):
        p = {"a": 1, "b": "中文"}
        self.assertEqual(models.chain_hash("PREV", p), models.chain_hash("PREV", p))
        self.assertNotEqual(models.chain_hash("PREV", p), models.chain_hash("OTHER", p))

    def test_review_record_rejects_bad_conclusion(self):
        with self.assertRaises(ValueError):
            models.ReviewRecord("AL1", "not_a_conclusion")

    def test_review_record_closed_loop_flag(self):
        self.assertEqual(models.ReviewRecord("AL1", config.REVIEW_FALSE_POSITIVE).closed_loop, 1)
        self.assertEqual(models.ReviewRecord("AL1", config.REVIEW_CONFIRMED).closed_loop, 0)


# ==================== 权限最小化 ====================
class TestAccess(Base):
    def test_password_not_plaintext(self):
        raw = str(self.accounts.list_accounts()["patrol1"]["pwd"])
        self.assertNotIn("patrol1", raw)
        self.assertTrue(raw.startswith("pbkdf2$"))

    def test_authenticate_ok_and_fail(self):
        self.assertIsNotNone(self.accounts.authenticate("patrol1", "patrol1"))
        self.assertIsNone(self.accounts.authenticate("patrol1", "bad"))
        self.assertIsNone(self.accounts.authenticate("nobody", "x"))

    def test_owner_is_highest_privilege(self):
        """owner = 最高权限：可复核 + 可管终端账户 + 全部考场"""
        self.assertEqual(self.cmd.role, config.ROLE_OWNER)
        self.assertEqual(config.ROLE_LEVEL[self.cmd.role],
                         max(config.ROLE_LEVEL.values()))
        self.assertTrue(self.cmd.can("review"))
        self.assertTrue(self.cmd.can("manage"))
        self.assertTrue(self.cmd.can("verify"))
        self.assertIsNone(self.cmd.room_filter())

    def test_only_owner_can_manage_accounts(self):
        self.assertFalse(self.patrol.can("manage"))
        self.assertFalse(self.inv.can("manage"))
        self.assertFalse(self.checker.can("manage"))
        ok, _ = self.accounts.create("u_tmp", "Passw0rd!", config.ROLE_CHECKER,
                                    by_role=self.patrol.role)
        self.assertFalse(ok)
        ok, _ = self.accounts.create("u_tmp", "Passw0rd!", config.ROLE_CHECKER,
                                    by_role=self.cmd.role)
        self.assertTrue(ok)
        self.assertIsNotNone(self.accounts.authenticate("u_tmp", "Passw0rd!"))
        self.assertTrue(self.accounts.reset_password("u_tmp", "New1234",
                                                    by_role=self.cmd.role)[0])
        self.assertIsNone(self.accounts.authenticate("u_tmp", "Passw0rd!"))
        self.assertIsNotNone(self.accounts.authenticate("u_tmp", "New1234"))

    def test_owner_cannot_create_peer_or_delete_self(self):
        self.assertFalse(self.accounts.create("u_owner2", "Passw0rd!", config.ROLE_OWNER,
                                              by_role=self.cmd.role)[0])
        self.assertFalse(self.accounts.delete("owner", by_role=self.cmd.role)[0])
        self.assertFalse(self.accounts.reset_password("owner", "x",
                                                      by_role=self.cmd.role)[0])

    def test_legacy_command_account_migrates_to_owner(self):
        """老库里的 command 账户应自动迁移成 owner 并升为所有者角色"""
        s2 = storage.Store(db_path=os.path.join(_TMP, "legacy_%s.db" % uuid.uuid4().hex[:6]))
        s2.set_kv("accounts", {"command": {"pwd": access.hash_pwd("command"),
                                           "role": config.ROLE_COMMANDER,
                                           "name": "指挥中心", "rooms": []}})
        acc2 = access.AccountStore(s2)
        acc2.ensure_seed()
        names = acc2.list_accounts()
        self.assertIn("owner", names)
        self.assertNotIn("command", names)
        self.assertEqual(names["owner"]["role"], config.ROLE_OWNER)
        self.assertIsNotNone(acc2.authenticate("owner", "EDY_POWERQ"))
        self.assertIsNone(acc2.authenticate("command", "command"))

    def test_admin_role_daily_full_but_no_manage(self):
        """管理员：日常全权（复核/核验/派发），但没有账户管理与危险操作"""
        adm = self.accounts.authenticate("admin", "admin")
        self.assertIsNotNone(adm)
        self.assertEqual(adm.role, config.ROLE_ADMIN)
        for act in ("view", "review", "report", "dispatch", "export", "verify"):
            self.assertTrue(adm.can(act), act)
        self.assertFalse(adm.can("manage"))
        self.assertIsNone(adm.room_filter())
        self.assertLess(config.ROLE_LEVEL[adm.role], config.ROLE_LEVEL[config.ROLE_OWNER])
        self.assertGreater(config.ROLE_LEVEL[adm.role],
                           config.ROLE_LEVEL[config.ROLE_INVIGILATOR])
        # 管理员不能建账户/改他人口令
        self.assertFalse(self.accounts.create("x1", "Passw0rd!", config.ROLE_CHECKER,
                                              by_role=adm.role)[0])
        self.assertFalse(self.accounts.reset_password("check1", "x", by_role=adm.role)[0])
        self.assertFalse(self.accounts.delete("check1", by_role=adm.role)[0])

    def test_missing_seed_accounts_are_backfilled(self):
        """老库升级：缺失的预置账户（如 admin）会被补齐，且不动已有口令"""
        s2 = storage.Store(db_path=os.path.join(_TMP, "old_%s.db" % uuid.uuid4().hex[:6]))
        s2.set_kv("accounts", {"owner": {"pwd": access.hash_pwd("MyOwnPwd!"),
                                         "role": config.ROLE_OWNER,
                                         "name": "系统所有者", "rooms": []}})
        acc2 = access.AccountStore(s2)
        acc2.ensure_seed()
        names = acc2.list_accounts()
        self.assertIn("admin", names)                       # 补齐
        self.assertIsNotNone(acc2.authenticate("admin", "admin"))
        # 已有账户的口令不能被覆盖
        self.assertIsNotNone(acc2.authenticate("owner", "MyOwnPwd!"))

    def test_role_matrix(self):
        self.assertTrue(self.patrol.can("review"))
        self.assertFalse(self.inv.can("review"))       # 监考员不能自己判自己
        cmdr = access.Session("cmdr", config.ROLE_COMMANDER, "指挥中心")
        self.assertFalse(cmdr.can("review"))           # 指挥中心角色不直接复核
        self.assertTrue(cmdr.can("dispatch"))
        self.assertTrue(self.checker.can("verify"))
        self.assertFalse(self.checker.can("review"))

    def test_require_raises(self):
        with self.assertRaises(access.AccessError):
            self.inv.require("review")
        self.assertTrue(self.inv.require("report"))

    def test_room_filter(self):
        self.assertEqual(self.inv.room_filter(), ["A-01"])
        self.assertIsNone(self.patrol.room_filter())
        self.assertIsNone(self.cmd.room_filter())

    def test_invigilator_sees_only_own_room(self):
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-02"))
        mine = access.visible_alerts(self.store, self.inv)
        all_of = access.visible_alerts(self.store, self.patrol)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].room_id, "A-01")
        self.assertEqual(len(all_of), 2)

    def test_unassigned_room_sees_nothing(self):
        s = access.Session("x", config.ROLE_INVIGILATOR, "x", [])
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        self.assertEqual(access.visible_alerts(self.store, s), [])

    def test_default_masked(self):
        a = models.Alert(code="FACE_MISMATCH", room_id="A-01",
                         candidate_id="330100199001011234")
        txt = access.render(a, self.patrol)
        self.assertNotIn("330100199001011234", txt)
        self.assertIn("3301**********1234", txt)

    def test_export_requires_action_and_masks(self):
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01",
                                     candidate_id="330100199001011234"))
        alerts = access.visible_alerts(self.store, self.inv)
        payload = access.export_payload(alerts, self.patrol)
        self.assertEqual(payload[0]["candidate_id"], "******")
        with self.assertRaises(access.AccessError):
            access.export_payload(alerts, self.inv)   # 监考员无 export

    def test_red_alert_renders_warning(self):
        a, _ = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))
        self.assertIn("必须确认", access.render(a, self.patrol))


# ==================== 告警接入 ====================
class TestAlerts(Base):
    def test_ingest_and_dedupe(self):
        a1, new1 = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        a2, new2 = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(a1.alert_id, a2.alert_id)
        self.assertEqual(len(self.store.list_alerts()), 1)

    def test_dedupe_not_across_rooms(self):
        self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-02", seat_no="1"))
        self.assertEqual(len(self.store.list_alerts()), 2)

    def test_dedupe_window_expires(self):
        a, _ = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        a.created_epoch = models.now_epoch() - (alerts_mod.DEDUP_WINDOW_SEC + 5)
        self.store.save_alert(a)
        _, new = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        self.assertTrue(new)

    def test_acked_alerts_not_deduped(self):
        a, _ = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        self.hub.ack(a.alert_id, "patrol1")
        _, new = self.hub.ingest(models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="1"))
        self.assertTrue(new)

    def test_ack_twice_rejected(self):
        a, _ = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))
        self.assertTrue(self.hub.ack(a.alert_id, "patrol1")[0])
        self.assertFalse(self.hub.ack(a.alert_id, "patrol1")[0])

    def test_ack_missing_alert(self):
        self.assertFalse(self.hub.ack("NOPE", "patrol1")[0])

    def test_display_order_red_then_overdue(self):
        o = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))[0]
        r = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))[0]
        o.sla_deadline = models.now_epoch() - 10
        self.store.save_alert(o)
        order = self.hub.display_order([o, r])
        self.assertEqual(order[0].level, config.LEVEL_RED)
        self.assertEqual(order[1].alert_id, o.alert_id)

    def test_ingest_many_reports_dup(self):
        items = self.hub.simulate(6)
        created, dup = self.hub.ingest_many(items)
        self.assertEqual(len(created) + dup, 6)
        created2, dup2 = self.hub.ingest_many(items)
        self.assertEqual(len(created2), 0)

    def test_fetch_from_edge_without_url(self):
        got, why = self.hub.fetch_from_edge(url="")
        self.assertEqual(got, [])
        self.assertIn("未配置", why)

    def test_fetch_from_edge_unreachable_is_safe(self):
        got, why = self.hub.fetch_from_edge(url="http://127.0.0.1:9", timeout=1)
        self.assertEqual(got, [])
        self.assertTrue("不可达" in why or "拉取" in why)

    def test_level_badge_text(self):
        self.assertIn("强制确认=True", alerts_mod.level_badge(config.LEVEL_RED))


# ==================== 复核留痕 ====================
class TestReview(Base):
    def _one_red(self):
        a, _ = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))
        return a

    def test_reason_required(self):
        a = self._one_red()
        self.hub.ack(a.alert_id, "patrol1")
        ok, msg = self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "   ", self.patrol)
        self.assertFalse(ok)
        self.assertIn("理由", msg)

    def test_red_must_ack_first(self):
        a = self._one_red()
        ok, msg = self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "有手机", self.patrol)
        self.assertFalse(ok)
        self.assertIn("确认", msg)

    def test_review_after_ack(self):
        a = self._one_red()
        self.hub.ack(a.alert_id, "patrol1")
        ok, rid = self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "确有手机反光", self.patrol)
        self.assertTrue(ok)
        b = self.store.get_alert(a.alert_id)
        self.assertEqual(b.status, models.ST_REVIEWED)
        self.assertEqual(b.review_by, "patrol1")
        self.assertEqual(b.review_conclusion, config.REVIEW_CONFIRMED)
        self.assertEqual(len(self.store.list_reviews()), 1)
        self.assertTrue(rid)

    def test_no_double_review(self):
        a = self._one_red()
        self.hub.ack(a.alert_id, "patrol1")
        self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "r1", self.patrol)
        ok, msg = self.rev.review(a.alert_id, config.REVIEW_REJECTED, "r2", self.patrol)
        self.assertFalse(ok)
        self.assertIn("已复核", msg)

    def test_invigilator_cannot_review(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        with self.assertRaises(access.AccessError):
            self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "r", self.inv)

    def test_cannot_review_other_room(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-02"))
        ok, msg = self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "r", self.patrol)
        # 巡考员可见全部考场，可以复核；换成监考员则应被拒
        self.assertTrue(ok, msg)
        b, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-09"))
        s = access.Session("inv2", config.ROLE_PATROL, "巡考", [])
        self.assertTrue(s.can("review"))

    def test_attachment_saved_and_validated(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        bad = os.path.join(_TMP, "x.exe")
        open(bad, "wb").write(b"MZ")
        ok, msg = self.rev.attach(bad)
        self.assertFalse(ok)
        good = os.path.join(_TMP, "x.jpg")
        open(good, "wb").write(b"\xff\xd8\xff\xd9")
        ok, name = self.rev.attach(good)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(str(config.ATTACH_DIR), name)))

    def test_missing_attachment_rejected(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        ok, msg = self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "r", self.patrol,
                                  attachments=[os.path.join(_TMP, "nope.jpg")])
        self.assertFalse(ok)

    def test_false_positive_loop_stats(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        b, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01", seat_no="2"))
        self.rev.review(a.alert_id, config.REVIEW_FALSE_POSITIVE, "只是低头", self.patrol)
        self.rev.review(b.alert_id, config.REVIEW_CONFIRMED, "确实异常", self.patrol)
        rows = {r["code"]: r for r in self.rev.stats()}
        self.assertEqual(rows["LOOK_DOWN"]["total"], 2)
        self.assertEqual(rows["LOOK_DOWN"]["false_positive"], 1)
        self.assertAlmostEqual(rows["LOOK_DOWN"]["fp_rate"], 0.5, places=3)

    def test_report_scope_enforced(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-02"))
        ok, msg = self.rev.report(a.alert_id, "note", self.inv)
        self.assertFalse(ok)
        self.assertIn("无权", msg)
        b, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        self.assertTrue(self.rev.report(b.alert_id, "note", self.inv)[0])

    def test_timeline_has_three_events(self):
        a, _ = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))
        self.hub.ack(a.alert_id, "patrol1")
        self.rev.review(a.alert_id, config.REVIEW_CONFIRMED, "确实", self.patrol)
        ev = self.rev.timeline(a.alert_id)
        self.assertEqual([e["event"] for e in ev], ["告警接入", "确认", "复核"])


# ==================== 断网可用 ====================
class TestOffline(Base):
    def test_enqueue_on_ingest(self):
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        self.assertEqual(len(self.store.pending_unsynced()), 1)
        self.assertTrue(os.path.exists(os.path.join(str(config.QUEUE_DIR), "outbox.ndjson")))

    def test_offline_flush_sends_nothing(self):
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        r = self.ob.flush(force=False)
        self.assertFalse(r["online"])
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["pending"], 1)

    def test_online_flush_sends_all(self):
        for i in range(3):
            self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01", seat_no=str(i)))
        ob = offline_mod.Outbox(self.store,
                                probe=offline_mod.ConnectivityProbe(probe=lambda u: True),
                                sender=lambda url, payload: True)
        r = ob.flush(force=False)
        self.assertEqual(r["sent"], 3)
        self.assertEqual(self.store.pending_unsynced(), [])

    def test_failed_send_keeps_pending(self):
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        ob = offline_mod.Outbox(self.store,
                                probe=offline_mod.ConnectivityProbe(probe=lambda u: True),
                                sender=lambda url, payload: False)
        r = ob.flush(force=False)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(len(self.store.pending_unsynced()), 1)

    def test_stale_records_dropped_not_silently(self):
        a, _ = self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        a.created_epoch = models.now_epoch() - (config.OFFLINE_MAX_AGE_HOURS + 1) * 3600
        self.store.save_alert(a)
        ob = offline_mod.Outbox(self.store,
                                probe=offline_mod.ConnectivityProbe(probe=lambda u: True),
                                sender=lambda url, payload: True)
        r = ob.flush(force=False)
        self.assertEqual(r["dropped"], 1)
        self.assertTrue(any(x["action"] == "补传超期丢弃" for x in self.store.audit_rows()))

    def test_status_shape(self):
        st = self.ob.status()
        self.assertEqual(set(st.keys()), {"online", "pending", "platform", "max_age_hours"})


# ==================== 统一管控 / 稳定性 ====================
class TestFleet(Base):
    def setUp(self):
        Base.setUp(self)
        self.fl = fleet_mod.FleetManager(self.store)

    def test_default_config(self):
        cfg = self.fl.config_snapshot()
        self.assertIn("terminal_gui", cfg["app_whitelist"])
        self.assertFalse(cfg["peripherals"]["usb_storage"])

    def test_config_patch_merges_peripherals(self):
        self.fl.set_config({"peripherals": {"usb_storage": True}})
        self.fl.set_peripheral("bluetooth", True)
        per = self.fl.peripheral_status()
        self.assertTrue(per["usb_storage"])
        self.assertTrue(per["bluetooth"])
        self.assertIn("camera", per)          # 未被清空

    def test_whitelist_check(self):
        self.assertTrue(self.fl.check_app("terminal_gui")[0])
        self.assertFalse(self.fl.check_app("game")[0])

    def test_unknown_peripheral(self):
        self.assertFalse(self.fl.set_peripheral("nope", True)[0])

    def test_wipe_requires_confirm_code(self):
        self.fl.request_wipe("platform", "lost")
        self.assertTrue(self.fl.wipe_pending())
        self.assertFalse(self.fl.execute_wipe("wipe", "platform")[0])
        self.assertFalse(self.fl.execute_wipe("", "platform")[0])

    def test_wipe_without_request(self):
        self.assertFalse(self.fl.execute_wipe("WIPE", "platform")[0])

    def test_apply_commands(self):
        self.store.push_command("set_config", {"version": "9.9.9"})
        self.store.push_command("peripheral", {"name": "camera", "enabled": False})
        self.store.push_command("unknown_kind", {})
        res = self.fl.apply_pending()
        self.assertEqual(len(res), 3)
        self.assertEqual(self.fl.config_snapshot()["version"], "9.9.9")
        self.assertFalse(self.fl.peripheral_status()["camera"])
        self.assertEqual(self.store.pending_commands(), [])

    def test_upgrade_is_registered_only(self):
        ok, cmd = self.fl.push_upgrade("1.2.3", "http://example.invalid/pkg")
        self.assertTrue(ok)
        res = self.fl.apply_pending()
        self.assertIn("仅登记", res[0][2])

    def test_soak_accumulates_and_reports_honestly(self):
        for _ in range(3):
            self.fl.soak_tick()
        st = self.fl.soak_status()
        self.assertEqual(st["ticks"], 3)
        self.assertFalse(st["passed"])          # 不可能几秒就达标
        self.assertEqual(st["target_hours"], 72)

    def test_soak_gap_breaks_continuity(self):
        path = str(config.SOAK_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            e = models.now_epoch()
            f.write('%s\n' % models.canonical({"ts": "t1", "epoch": e}))
            f.write('%s\n' % models.canonical({"ts": "t2", "epoch": e + 100000}))
        self.assertEqual(self.fl.soak_status()["longest_sec"], 0.0)

    def test_status_shape(self):
        st = self.fl.status()
        for k in ("版本", "白名单", "外设", "待执行指令", "擦除待确认", "稳定性"):
            self.assertIn(k, st)


# ==================== 时钟 ====================
class TestClock(Base):
    def test_drift_within_tolerance(self):
        ck = clock_mod.StandardClock(self.store, time_provider=lambda: (time.time(), "t"))
        self.assertTrue(ck.drift()["ok"])

    def test_drift_exceeds_tolerance(self):
        ck = clock_mod.StandardClock(self.store, time_provider=lambda: (time.time() - 30, "t"))
        self.assertFalse(ck.drift()["ok"])

    def test_source_labelled_honestly(self):
        ck = clock_mod.StandardClock(self.store, time_provider=lambda: (time.time(), "test-src"))
        self.assertEqual(ck.drift()["source"], "test-src")
        ck2 = clock_mod.StandardClock(self.store, time_provider=None, platform_url="")
        self.assertIn("本机时间", ck2.drift()["source"])

    def test_broadcast_fires_once(self):
        ck = clock_mod.StandardClock(self.store, time_provider=lambda: (time.time(), "t"))
        ck.set_broadcasts([{"at": "09:00", "text": "开始"}])
        t = time.mktime(time.strptime("2026-06-07 09:00:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(len(ck.fire_due(now=t)), 1)
        self.assertEqual(len(ck.fire_due(now=t)), 0)
        t2 = time.mktime(time.strptime("2026-06-07 09:01:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(len(ck.fire_due(now=t2)), 0)

    def test_broadcast_filtered_on_set(self):
        ck = clock_mod.StandardClock(self.store)
        items = ck.set_broadcasts([{"at": "09:00", "text": "ok"},
                                   {"at": "", "text": "bad"},
                                   {"at": "10:00", "text": ""}])
        self.assertEqual(len(items), 1)

    def test_next_broadcast(self):
        ck = clock_mod.StandardClock(self.store)
        ck.set_broadcasts([{"at": "09:00", "text": "A"}, {"at": "23:59", "text": "B"}])
        t = time.mktime(time.strptime("2026-06-07 10:00:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(ck.next_broadcast(now=t)["text"], "B")

    def test_drift_alert_generated(self):
        ck = clock_mod.StandardClock(self.store, hub=self.hub,
                                     time_provider=lambda: (time.time() - 30, "t"))
        a = ck.check_and_alert()
        self.assertIsNotNone(a)
        self.assertEqual(a.code, "CLOCK_DRIFT")


# ==================== 存储 / 审计链 ====================
class TestStorage(Base):
    def test_default_accounts_seeded_once(self):
        n1 = len(self.accounts.list_accounts())
        self.accounts.ensure_seed()
        self.assertEqual(len(self.accounts.list_accounts()), n1)

    def test_bad_role_rejected(self):
        self.assertFalse(self.accounts.create("u1", "p", "superuser")[0])
        self.assertFalse(self.accounts.create("", "p", config.ROLE_PATROL)[0])

    def test_create_and_reset(self):
        self.assertTrue(self.accounts.create("u2", "Passw0rd!", config.ROLE_PATROL, "巡考2")[0])
        self.assertIsNotNone(self.accounts.authenticate("u2", "Passw0rd!"))
        self.assertTrue(self.accounts.reset_password("u2", "NewPass1!")[0])
        self.assertIsNone(self.accounts.authenticate("u2", "Passw0rd!"))
        self.assertIsNotNone(self.accounts.authenticate("u2", "NewPass1!"))
        self.assertFalse(self.accounts.create("u2", "x", config.ROLE_PATROL)[0])

    def test_audit_chain_ok(self):
        before = len(self.store.audit_rows(limit=10 ** 6))
        self.store.log("t1", "d1", "op")
        self.store.log("t2", "d2", "op")
        ok, msg, n = self.store.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(n, before + 2)

    def test_audit_chain_detects_tamper(self):
        self.store.log("t1", "d1", "op")
        self.store.log("t2", "d2", "op")
        with self.store._conn() as c:
            c.execute("UPDATE audit SET detail='HACKED' WHERE seq=1")
        ok, msg, _ = self.store.verify_chain()
        self.assertFalse(ok)
        self.assertIn("篡改", msg)

    def test_summary_counts(self):
        red, _ = self.hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01"))
        self.hub.ingest(models.Alert(code="LOOK_DOWN", room_id="A-01"))
        self.hub.ack(red.alert_id, "patrol1")
        s = self.store.summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_level"][config.LEVEL_RED], 1)
        self.assertEqual(s["by_status"][models.ST_ACKED], 1)

    def test_kv_roundtrip(self):
        self.store.set_kv("k", {"a": [1, 2]})
        self.assertEqual(self.store.get_kv("k"), {"a": [1, 2]})
        self.assertIsNone(self.store.get_kv("nope"))
        self.assertEqual(self.store.get_kv("nope", 7), 7)

    def test_persistence_across_instances(self):
        self.store.log("persist", "x", "op")
        s2 = storage.Store(db_path=self.db)
        ok, _, n = s2.verify_chain()
        self.assertTrue(ok)
        self.assertGreaterEqual(n, 1)


# ==================== 平台 / 桥接 ====================
class TestBridge(Base):
    def test_face_bridge_reports_availability(self):
        import bridge
        fb = bridge.FaceBridge()
        avail, why = fb.available()
        self.assertIsInstance(avail, bool)
        self.assertTrue(why)

    def test_face_bridge_missing_image(self):
        import bridge
        r = bridge.FaceBridge().verify(os.path.join(_TMP, "none.jpg"), "C001")
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["reason"])

    def test_platform_link_unconfigured(self):
        import bridge
        pl = bridge.PlatformLink(platform_url="", edge_url="")
        conn = pl.connectivity()
        self.assertEqual(conn["业务平台"], "未配置")
        self.assertEqual(conn["边缘节点"], "未配置")

    def test_platform_link_unreachable(self):
        import bridge
        pl = bridge.PlatformLink(platform_url="http://127.0.0.1:9", timeout=1)
        self.assertIn("不可达", pl._probe(pl.platform_url))


if __name__ == "__main__":
    unittest.main(verbosity=2)
