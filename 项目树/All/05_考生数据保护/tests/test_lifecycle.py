# -*- coding: utf-8 -*-
"""全生命周期控制（《考生数据保护.md》第二节）单元测试

覆盖：采集（同意前置/非静默/频率）、传输（专网+加密）、存储（不可逆+加密）、
      使用（目的限定）、共享（最小字段+审批+脱敏导出）、销毁（到期自动/按申请/法律保留）。
"""
import os
import unittest

from tests._base import DppTestCase
from dpp import config, crypto, lifecycle, models


class TestCollect(DppTestCase):

    def test_collect_requires_consent(self):
        """没有单独同意，采集必须硬拒绝（不是警告）。"""
        owner = self.login("owner")
        with self.assertRaises(lifecycle.LifecycleError):
            self.life.collect(owner, name="无同意考生",
                              id_card_no="330100200801011234")
        # 拒绝也要留痕
        actions = self.store.audit_actions()
        self.assertTrue(actions.get("采集"), "拒绝采集必须写审计")

    def test_collect_rejected_after_consent_withdrawn(self):
        """同意被撤回后，采集同样必须被拒（撤回即时生效）。"""
        owner = self.login("owner")
        res = self.collect_one(name="撤回考生")
        consent_id = self.store.list_consents()[0].consent_id
        # 撤回同意：submit 对 withdraw 即时生效（不需二次审批）
        out = self.rights.submit(owner, res["cand_ref"], "withdraw",
                                 detail="考生撤回", identity_verified=True)
        self.assertEqual(out["status"], "done")
        with self.assertRaises(Exception):
            self.life.collect(owner, name="撤回考生",
                              id_card_no="330100200801011234",
                              consent_id=consent_id, cand_ref=res["cand_ref"])

    def test_collect_stores_irreversible_template(self):
        res = self.collect_one(name="模板考生")
        row = self.store.raw_candidate_row(res["cand_ref"])
        self.assertTrue(row["face_template"].startswith("tpl1:"))
        self.assertTrue(res["template"])

    def test_collect_image_is_encrypted_file(self):
        """留下的原始图像必须是加密文件（不是裸图）。"""
        res = self.collect_one(name="留图考生", with_image=True)
        self.assertTrue(res["image_encrypted"])
        row = self.store.raw_candidate_row(res["cand_ref"])
        self.assertTrue(crypto.is_encrypted(row["face_image_path"]))
        # face_image_path 落库是密文，解密后才是 vault 里的真实文件名
        filename = crypto.decrypt_field(row["face_image_path"])
        path = os.path.join(str(config.VAULT_DIR), filename)
        self.assertTrue(os.path.exists(path), "加密图像文件未落盘：%s" % path)
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(8)
        # 落盘内容是 enc: 开头的密文，不是 PNG/JPG 明文头
        self.assertTrue(head.startswith("enc:"), "图像文件疑似明文落盘：%r" % head)

    def test_no_silent_start(self):
        self.assertFalse(config.CAPTURE["silent_start"],
                         "禁止静默开启采集")

    def test_fps_cap_configured(self):
        self.assertGreaterEqual(config.CAPTURE["fps_max"], 1)
        self.assertLessEqual(config.CAPTURE["fps_max"], 25,
                             "采集帧率上限应体现按需最小化")


class TestTransport(DppTestCase):

    def test_private_encrypted_ok(self):
        ok, msg, ev = self.life.check_transport("dpp://edge-1/alert",
                                                encrypted=True, network="private")
        self.assertTrue(ok)
        self.assertTrue(ev)

    def test_reject_public_network(self):
        ok, msg, _ = self.life.check_transport("dpp://edge-1/alert",
                                               encrypted=True, network="public")
        self.assertFalse(ok)
        self.assertIn("专网", msg)

    def test_reject_plaintext(self):
        ok, msg, _ = self.life.check_transport("dpp://edge-1/alert",
                                               encrypted=False, network="private")
        self.assertFalse(ok)
        self.assertIn("未加密", msg)

    def test_reject_http_plaintext_host(self):
        ok, msg, _ = self.life.check_transport("http://cloud.example.com/upload",
                                               encrypted=True, network="private")
        self.assertFalse(ok)
        self.assertIn("明文", msg)


class TestUse(DppTestCase):

    def test_allowed_purposes(self):
        res = self.collect_one(name="使用考生")
        owner = self.login("owner")
        for p in models.PURPOSE_ALLOWED:
            out = self.life.use(owner, res["cand_ref"], p, "审核")
            self.assertTrue(out["allowed"])

    def test_forbidden_purpose_recorded_and_rejected(self):
        """情绪分析等超目的使用必须拒绝，且拒绝记录要落库（可核查）。"""
        res = self.collect_one(name="越权使用考生")
        owner = self.login("owner")
        for p in models.PURPOSE_FORBIDDEN:
            with self.assertRaises(Exception):
                self.life.use(owner, res["cand_ref"], p, "生成画像")
        denied = [u for u in self.store.list_uses() if not u["allowed"]]
        self.assertEqual(len(denied), len(models.PURPOSE_FORBIDDEN))

    def test_reveal_requires_reason(self):
        res = self.collect_one(name="查看考生")
        owner = self.login("owner")
        with self.assertRaises(Exception):
            self.life.reveal_plain(owner, res["cand_ref"], "")

    def test_reveal_leaves_watermark_and_audit(self):
        res = self.collect_one(name="水印考生")
        owner = self.login("owner")
        out = self.life.reveal_plain(owner, res["cand_ref"], "复核告警",
                                     terminal="巡考终端-R7")
        self.assertIn("owner", out["watermark"])
        self.assertIn("巡考终端-R7", out["watermark"])
        self.assertTrue(self.store.audit_actions().get("解密查看"))
        reveals = self.store.list_reveals()
        self.assertEqual(len(reveals), 1)
        self.assertIn("巡考终端-R7", reveals[0]["watermark"])


class TestShare(DppTestCase):

    def test_share_allows_necessary_fields(self):
        res = self.collect_one(name="共享考生")
        owner = self.login("owner")
        out = self.life.request_share(owner, [res["cand_ref"]],
                                      ["ref", "room_id", "seat_no", "ticket_no"])
        self.assertEqual(out["status"], "pending")

    def test_share_rejects_direct_identifiers(self):
        """共享字段含姓名/证件号/生物特征必须被拒。"""
        res = self.collect_one(name="禁止共享考生")
        owner = self.login("owner")
        for bad in ("name", "id_card_no", "face_template", "face_image_path"):
            with self.assertRaises(Exception):
                self.life.request_share(owner, [res["cand_ref"]], ["ref", bad])

    def test_share_rejects_unknown_field(self):
        res = self.collect_one(name="未知字段考生")
        owner = self.login("owner")
        with self.assertRaises(Exception):
            self.life.request_share(owner, [res["cand_ref"]], ["ref", "email"])

    def test_approve_and_export_is_masked(self):
        res = self.collect_one(name="导出考生", id_card_no="330100200801011234",
                               ticket_no="2026T0009", seat_no="09")
        owner = self.login("owner")
        sh = self.life.request_share(owner, [res["cand_ref"]],
                                     ["ref", "room_id", "seat_no", "ticket_no"])
        out = self.life.approve_share(owner, sh["req_id"], approve=True, export=True)
        self.assertEqual(out["status"], "approved")
        self.assertTrue(out["export_path"])
        with open(out["export_path"], "r", encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("330100200801011234", body)
        self.assertIn('"masked": true', body)
        self.assertTrue(self.store.audit_actions().get("导出"))

    def test_share_cannot_be_approved_twice(self):
        res = self.collect_one(name="重复审批考生")
        owner = self.login("owner")
        sh = self.life.request_share(owner, [res["cand_ref"]], ["ref"])
        self.life.approve_share(owner, sh["req_id"], approve=True)
        with self.assertRaises(Exception):
            self.life.approve_share(owner, sh["req_id"], approve=True)


class TestDestroy(DppTestCase):

    def _make_expired(self, name):
        """造一条已过期（保留期 -1 天）的考生记录。

        注意：加密数据只读，不能"先采集再把时间改到过去"（那是就地修改）。
        这里用 captured_epoch 直接构造一条 40 天前的记录，一次性登记。
        """
        owner = self.login("owner")
        cand_ref = models.new_id("C")
        cs = self.make_consent(name, save=False, cand_ref=cand_ref)
        self.store.save_consent(cs)
        res = self.life.collect(
            owner, name=name, id_card_no="330100200801019999",
            ticket_no="2026T9999", seat_no="99", room_id="A-09", site="一中考点",
            feature_vector="feature-%s" % cand_ref, consent_id=cs.consent_id,
            captured_epoch=models.now_epoch() - 40 * 86400, retention_days=30,
            cand_ref=cand_ref)
        return res["cand_ref"]

    def test_auto_destroy_removes_expired(self):
        ref = self._make_expired("到期考生")
        out = self.life.run_auto_destroy(operator="system")
        self.assertGreaterEqual(out["destroyed"], 1)
        self.assertIsNone(self.store.get_candidate(ref), "到期记录应被销毁")
        recs = self.store.list_destroys(mode="auto")
        self.assertTrue(recs)
        import json as _json
        covered = recs[0]["covered"]
        if isinstance(covered, str):
            covered = _json.loads(covered)
        self.assertIn("face_template", covered["categories"])

    def test_auto_destroy_removes_consent_too(self):
        """销毁要连带清掉同意记录（否则"人删了同意还在"）。"""
        ref = self._make_expired("连带同意考生")
        before = len(self.store.list_consents())
        self.assertTrue(before >= 1)
        self.life.run_auto_destroy(operator="system")
        remaining = [c for c in self.store.list_consents() if c.cand_ref == ref]
        self.assertEqual(remaining, [])

    def test_auto_destroy_skips_legal_hold(self):
        ref = self._make_expired("法律保留考生")
        owner = self.login("owner")
        self.life.set_legal_hold(owner, ref, on=True, reason="涉考务争议在查")
        out = self.life.run_auto_destroy(operator="system")
        self.assertGreaterEqual(out["kept_by_legal_hold"], 1)
        self.assertIsNotNone(self.store.get_candidate(ref), "法律保留期间不得销毁")

    def test_destroy_by_request_requires_reason(self):
        res = self.collect_one(name="删除权考生")
        owner = self.login("owner")
        with self.assertRaises(Exception):
            self.life.destroy_by_request(owner, res["cand_ref"], reason="")

    def test_force_destroy_requires_witness(self):
        res = self.collect_one(name="强制销毁考生")
        owner = self.login("owner")
        with self.assertRaises(Exception):
            self.life.destroy_by_request(owner, res["cand_ref"], reason="误采",
                                         force=True)
        out = self.life.destroy_by_request(owner, res["cand_ref"], reason="误采",
                                           force=True, witness="监考员B")
        self.assertTrue(out["destroy_id"])
        recs = self.store.list_destroys(mode="force")
        self.assertEqual(len(recs), 1)

    def test_legal_hold_blocks_request_destroy(self):
        res = self.collect_one(name="保留中考生")
        owner = self.login("owner")
        self.life.set_legal_hold(owner, res["cand_ref"], on=True, reason="在查")
        with self.assertRaises(Exception):
            self.life.destroy_by_request(owner, res["cand_ref"], reason="删除权")

    def test_set_legal_hold_requires_reason(self):
        res = self.collect_one(name="保留理由考生")
        owner = self.login("owner")
        with self.assertRaises(Exception):
            self.life.set_legal_hold(owner, res["cand_ref"], on=True, reason="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
