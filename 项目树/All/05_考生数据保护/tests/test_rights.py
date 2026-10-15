# -*- coding: utf-8 -*-
"""考生权利响应（《考生数据保护.md》第五节）单元测试

覆盖六项权利：知情 / 查阅 / 复制 / 更正 / 删除 / 撤回同意。
两条硬要求：
  1. 必须核验身份才受理（未核验只登记）；
  2. 撤回同意必须即时生效，并确保该考生后续走非人脸替代通道。
另测：SLA 时限、删除遇到法律保留应驳回、处理动作留痕。
"""
import unittest

from tests._base import DppTestCase
from dpp import config, models, rights


class TestRightsSubmit(DppTestCase):

    def test_supported_rights(self):
        self.assertEqual(set(rights.RIGHTS),
                         {"access", "copy", "correct", "delete", "withdraw"})

    def test_reject_unknown_right(self):
        res = self.collect_one(name="权利考生")
        owner = self.login("owner")
        with self.assertRaises(rights.RightsError):
            self.rights.submit(owner, res["cand_ref"], "随便查")

    def test_reject_unknown_candidate(self):
        owner = self.login("owner")
        with self.assertRaises(rights.RightsError):
            self.rights.submit(owner, "CD-not-exist", "access")

    def test_unverified_identity_is_only_registered(self):
        """身份未核验时只登记不受理，且明细带"待核验身份"标记。"""
        res = self.collect_one(name="未核验考生")
        owner = self.login("owner")
        out = self.rights.submit(owner, res["cand_ref"], "access",
                                 detail="我要查", identity_verified=False)
        rq = self.store.get_rights(out["req_id"])
        self.assertEqual(rq.status, "open")
        self.assertIn("待核验身份", rq.detail)

    def test_sla_days_match_config(self):
        res = self.collect_one(name="SLA考生")
        owner = self.login("owner")
        for right, days in config.RIGHTS_SLA_DAYS.items():
            out = self.rights.submit(owner, res["cand_ref"], right,
                                     identity_verified=True)
            self.assertEqual(out["due_days"], days,
                             "%s 的时限应为 %s 天" % (right, days))

    def test_due_epoch_computed(self):
        res = self.collect_one(name="时限考生")
        owner = self.login("owner")
        out = self.rights.submit(owner, res["cand_ref"], "delete",
                                 identity_verified=True)
        rq = self.store.get_rights(out["req_id"])
        self.assertGreater(rq.due_epoch, rq.created_epoch)

    def test_submit_is_audited(self):
        res = self.collect_one(name="留痕考生")
        owner = self.login("owner")
        self.rights.submit(owner, res["cand_ref"], "access",
                           identity_verified=True)
        self.assertTrue(self.store.audit_actions().get("权利申请"))


class TestWithdrawConsent(DppTestCase):

    def test_withdraw_takes_effect_immediately(self):
        """撤回同意即时生效：submit 后状态即 done。"""
        res = self.collect_one(name="撤回即时考生")
        owner = self.login("owner")
        out = self.rights.submit(owner, res["cand_ref"], "withdraw",
                                 identity_verified=True)
        self.assertEqual(out["status"], "done")
        self.assertIn("替代通道", out["note"])

    def test_valid_consent_none_after_withdraw(self):
        res = self.collect_one(name="撤回后同意考生")
        owner = self.login("owner")
        self.assertIsNotNone(self.store.valid_consent(res["cand_ref"]))
        self.rights.submit(owner, res["cand_ref"], "withdraw",
                           identity_verified=True)
        self.assertIsNone(self.store.valid_consent(res["cand_ref"]),
                          "撤回后不应再有有效同意")

    def test_withdraw_marks_consent_row(self):
        res = self.collect_one(name="标记撤回考生")
        owner = self.login("owner")
        self.rights.submit(owner, res["cand_ref"], "withdraw",
                           identity_verified=True)
        rows = [c for c in self.store.list_consents() if c.cand_ref == res["cand_ref"]]
        self.assertTrue(rows)
        self.assertTrue(all(c.withdrawn for c in rows))
        self.assertTrue(rows[0].withdrawn_reason)

    def test_withdraw_warns_when_no_alt_channel(self):
        """撤回后若无可用替代通道，必须打告警（不能默默失去核验路径）。"""
        res = self.collect_one(name="无通道撤回考生")
        owner = self.login("owner")
        self.rights.submit(owner, res["cand_ref"], "withdraw",
                           identity_verified=True)
        logs = " ".join(str(r["detail"]) for r in self.store.audit_rows(limit=200))
        self.assertIn("无可用替代通道", logs)

    def test_withdraw_then_collect_rejected(self):
        """撤回后再次采集必须被拒（同意前置闭环）。"""
        res = self.collect_one(name="撤回再采考生")
        owner = self.login("owner")
        consent_id = [c.consent_id for c in self.store.list_consents()
                      if c.cand_ref == res["cand_ref"]][0]
        self.rights.submit(owner, res["cand_ref"], "withdraw",
                           identity_verified=True)
        with self.assertRaises(Exception):
            self.life.collect(owner, name="撤回再采考生",
                              id_card_no="330100200801011234",
                              consent_id=consent_id, cand_ref=res["cand_ref"])


class TestRightsHandle(DppTestCase):

    def _open_req(self, name, right, verified=True):
        res = self.collect_one(name=name)
        owner = self.login("owner")
        out = self.rights.submit(owner, res["cand_ref"], right,
                                 identity_verified=verified)
        return res, out["req_id"]

    def test_access_returns_masked_data(self):
        res, rid = self._open_req("查阅考生", "access")
        owner = self.login("owner")
        out = self.rights.handle(owner, rid, result="已提供查阅")
        self.assertEqual(out["status"], "done")
        data = out["data"]
        self.assertIn("*", data["id_card_no"])
        self.assertNotIn("330100200801011234", str(data))

    def test_copy_same_as_access(self):
        res, rid = self._open_req("复制考生", "copy")
        owner = self.login("owner")
        out = self.rights.handle(owner, rid)
        self.assertIn("data", out)

    def test_correct_requires_payload(self):
        res, rid = self._open_req("更正考生", "correct")
        owner = self.login("owner")
        with self.assertRaises(rights.RightsError):
            self.rights.handle(owner, rid)
        out = self.rights.handle(owner, rid, corrected={"seat_no": "88"})
        self.assertEqual(out["status"], "done")
        # 只读策略下，更正 = 删除原记录 + 新增一条更正后的记录：
        # 原 ref 不复存在，新 ref 的座次是更正后的值。
        self.assertIsNone(self.store.get_candidate(res["cand_ref"]),
                          "更正走'删除+新增'，原记录应已删除")
        new_ref = out.get("new_cand_ref")
        self.assertTrue(new_ref, "更正应产生新的 cand_ref")
        self.assertEqual(self.store.get_candidate(new_ref).seat_no, "88")

    def test_delete_purges_data(self):
        res, rid = self._open_req("删除考生", "delete")
        owner = self.login("owner")
        out = self.rights.handle(owner, rid)
        self.assertIn("destroy_id", out)
        self.assertIsNone(self.store.get_candidate(res["cand_ref"]))

    def test_delete_rejected_under_legal_hold(self):
        """有法律保留必要时应驳回删除，并说明理由。"""
        res, rid = self._open_req("保留删除考生", "delete")
        owner = self.login("owner")
        self.life.set_legal_hold(owner, res["cand_ref"], on=True, reason="涉考务争议")
        out = self.rights.handle(owner, rid)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("法律保留", out["result"])
        self.assertIsNotNone(self.store.get_candidate(res["cand_ref"]))

    def test_cannot_handle_twice(self):
        res, rid = self._open_req("重复处理考生", "access")
        owner = self.login("owner")
        self.rights.handle(owner, rid)
        with self.assertRaises(rights.RightsError):
            self.rights.handle(owner, rid)

    def test_handle_requires_decision_permission(self):
        """只有 rights_decide 权限（owner/dpo/auditor）能出结论。"""
        res, rid = self._open_req("权限考生", "access")
        oper = self.login("oper1")
        from dpp import access
        with self.assertRaises(access.AccessError):
            self.rights.handle(oper, rid)

    def test_handle_is_audited(self):
        res, rid = self._open_req("处理留痕考生", "access")
        owner = self.login("owner")
        self.rights.handle(owner, rid)
        self.assertTrue(self.store.audit_actions().get("权利处理"))


class TestRightsStats(DppTestCase):

    def test_stats_counts(self):
        res = self.collect_one(name="统计考生")
        owner = self.login("owner")
        self.rights.submit(owner, res["cand_ref"], "access",
                           identity_verified=True)
        self.rights.submit(owner, res["cand_ref"], "withdraw",
                           identity_verified=True)   # 即时 done
        st = self.rights.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["done"], 1)
        self.assertEqual(st["open"], 1)

    def test_overdue_detection(self):
        """超过时限未处理应记为 overdue。"""
        res = self.collect_one(name="超期考生")
        owner = self.login("owner")
        out = self.rights.submit(owner, res["cand_ref"], "access",
                                 identity_verified=True)
        rq = self.store.get_rights(out["req_id"])
        rq.due_epoch = models.now_epoch() - 86400     # 把时限调到昨天
        self.store.save_rights(rq)
        st = self.rights.stats()
        self.assertEqual(st["overdue"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
