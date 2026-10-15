# -*- coding: utf-8 -*-
"""管理措施 · 替代通道 · 应急响应 · 验收指标（第四节 / 第六节）单元测试

重点验证"样本不足"语义：**绝不用 0 冒充达标**。
"""
import unittest

from tests._base import DppTestCase
from dpp import config, governance, models


class TestMeasures(DppTestCase):

    def test_seed_creates_default_ledger(self):
        self.gov.seed_default_measures(project_owner="张工", site_owner="李主任")
        summary = self.gov.measure_summary()
        self.assertEqual(summary["total"], 6)
        for k in governance.MEASURE_KINDS:
            self.assertIn(k, summary["by_kind"], "缺少管理措施类别：%s" % k)

    def test_seed_is_idempotent(self):
        self.gov.seed_default_measures()
        self.gov.seed_default_measures()
        self.assertEqual(self.gov.measure_summary()["total"], 6)

    def test_unassigned_owner_is_not_faked(self):
        """责任人未指定时如实标注"未指定"，不假填。"""
        self.gov.seed_default_measures()
        summary = self.gov.measure_summary()
        self.assertGreaterEqual(summary["by_kind"]["责任人"]["unspecified"], 1)

    def test_add_measure_rejects_unknown_kind(self):
        owner = self.login("owner")
        with self.assertRaises(ValueError):
            self.gov.add_measure(owner, "乱写的类别", "x")

    def test_complete_measure_requires_evidence_and_permission(self):
        self.gov.seed_default_measures()
        owner = self.login("owner")
        mid = self.store.list_measures()[0]["measure_id"]
        self.gov.complete_measure(owner, mid, evidence="签收单.pdf")
        rows = {m["measure_id"]: m for m in self.store.list_measures()}
        self.assertEqual(rows[mid]["status"], "done")

        # 无 approve 权限者不能完成措施
        from dpp import access
        oper = self.login("oper1")
        with self.assertRaises(access.AccessError):
            self.gov.complete_measure(oper, mid, evidence="x")

    def test_measure_actions_audited(self):
        owner = self.login("owner")
        self.gov.seed_default_measures()
        self.assertTrue(self.store.audit_actions().get("管理措施"))


class TestChannels(DppTestCase):

    def test_add_and_stats(self):
        cid = self.gov.add_channel("一中考点", name="人工核验通道", kind="manual")
        self.assertTrue(cid)
        st = self.gov.channel_stats()
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["usable"], 0, "未演练前不计入可用")

    def test_drill_makes_usable(self):
        cid = self.gov.add_channel("一中考点", kind="manual")
        owner = self.login("owner")
        self.gov.drill_channel(owner, cid, passed=True, note="撤掉人脸后演练通过")
        st = self.gov.channel_stats()
        self.assertEqual(st["usable"], 1)

    def test_failed_drill_not_usable(self):
        cid = self.gov.add_channel("一中考点", kind="manual")
        owner = self.login("owner")
        self.gov.drill_channel(owner, cid, passed=False)
        st = self.gov.channel_stats()
        self.assertEqual(st["usable"], 0, "演练未通过不得计入可用")

    def test_unavailable_channel_not_usable(self):
        cid = self.gov.add_channel("一中考点", kind="manual", available=0)
        owner = self.login("owner")
        self.gov.drill_channel(owner, cid, passed=True)
        st = self.gov.channel_stats()
        self.assertEqual(st["usable"], 0)

    def test_drill_unknown_channel_raises(self):
        owner = self.login("owner")
        with self.assertRaises(ValueError):
            self.gov.drill_channel(owner, "CH-nope")

    def test_drill_audited(self):
        cid = self.gov.add_channel("一中考点", kind="manual")
        owner = self.login("owner")
        self.gov.drill_channel(owner, cid, passed=True)
        self.assertTrue(self.store.audit_actions().get("替代通道演练"))


class TestIncidents(DppTestCase):

    def test_report_in_time(self):
        owner = self.login("owner")
        out = self.gov.report_incident(owner, "S3", "测试事件", detail="无外泄")
        self.assertTrue(out["ok"])
        self.assertLessEqual(out["delay_hours"], config.INCIDENT["report_within_hours"])

    def test_reject_unknown_level(self):
        owner = self.login("owner")
        with self.assertRaises(ValueError):
            self.gov.report_incident(owner, "S9", "乱级事件")

    def test_late_report_flagged(self):
        """上报时延超过 2 小时 -> not ok。"""
        owner = self.login("owner")
        inc = models.Incident("S2", "迟报事件", "测试")
        inc.found_epoch = models.now_epoch() - 3 * 3600    # 3 小时前发现
        inc.reported_epoch = models.now_epoch()
        inc.status = "reported"
        delay = inc.report_delay_hours()
        self.assertGreater(delay, config.INCIDENT["report_within_hours"])

    def test_incident_stats(self):
        owner = self.login("owner")
        self.gov.report_incident(owner, "S3", "事件1")
        self.gov.report_incident(owner, "S2", "事件2")
        st = self.gov.incident_stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["reported_in_time"], 2)

    def test_incident_audited(self):
        owner = self.login("owner")
        self.gov.report_incident(owner, "S3", "审计事件")
        self.assertTrue(self.store.audit_actions().get("安全事件"))


class TestAcceptanceInsufficient(DppTestCase):
    """纯空库（只有种子账户）：相关指标应显示"样本不足"且判为未达标，绝不用 0 冒充。"""

    def test_empty_db_reports_insufficient(self):
        acc = self.gov.acceptance(lifecycle=self.life, rights=self.rights)
        items = {it["指标项"]: it for it in acc["items"]}
        # 无数据时加密率与销毁率、通道应为"样本不足"且未达标
        self.assertIn("样本不足", items["敏感数据加密率"]["实测"])
        self.assertFalse(items["敏感数据加密率"]["达标"])
        self.assertIn("样本不足", items["到期自动销毁执行率"]["实测"])
        self.assertIn("样本不足", items["替代通道可用性"]["实测"])
        # 空库时不可能 5/5 全达标
        self.assertLess(acc["passed"], acc["total"])


class TestAcceptanceFull(DppTestCase):

    def _seed_full(self):
        owner = self.login("owner")
        # 一条留图记录用于核对加密率（不能销毁，否则样本清零）
        keep = self.collect_one(name="保留验收考生", with_image=True)
        # 另一条到期记录用于验证自动销毁（直接以"40 天前采集"一次性登记，
        # 不能先采集再改时间——那属于就地修改，已被只读策略禁止）
        cand_ref = models.new_id("C")
        cs = self.make_consent("到期验收考生", save=False, cand_ref=cand_ref)
        self.store.save_consent(cs)
        self.life.collect(
            owner, name="到期验收考生", id_card_no="330100200801018888",
            ticket_no="2026T8888", seat_no="88", room_id="A-08", site="一中考点",
            feature_vector="feature-%s" % cand_ref, consent_id=cs.consent_id,
            captured_epoch=models.now_epoch() - 40 * 86400, retention_days=30,
            cand_ref=cand_ref)
        self.life.run_auto_destroy(operator="system")
        # 替代通道 + 演练
        for site in ("一中考点", "二中考点"):
            cid = self.gov.add_channel(site, kind="manual")
            self.gov.drill_channel(owner, cid, passed=True)
        # 覆盖审计所需的动作集合：查询/使用、解密查看、共享(申请+审批+导出)、
        # 权利申请、传输校验、账户管理
        self.life.use(owner, keep["cand_ref"], "作弊判定", "复核")
        self.life.reveal_plain(owner, keep["cand_ref"], "核对证件")
        sh = self.life.request_share(owner, [keep["cand_ref"]],
                                     ["ref", "room_id", "seat_no"])
        self.life.approve_share(owner, sh["req_id"], approve=True, export=True)
        self.rights.submit(owner, keep["cand_ref"], "access",
                           identity_verified=True)
        self.life.check_transport("dpp://edge-1/alert")
        self.accounts.create("acc1", "p", config.ROLE_VIEWER, by_role="owner")
        # 越权一次
        try:
            self.life.use(owner, keep["cand_ref"], "情绪分析", "画像")
        except Exception:
            pass
        return owner

    def test_all_five_kpis_pass(self):
        self._seed_full()
        acc = self.gov.acceptance(lifecycle=self.life, rights=self.rights)
        by = {it["指标项"]: it for it in acc["items"]}
        self.assertTrue(by["敏感数据加密率"]["达标"], by["敏感数据加密率"]["实测"])
        self.assertTrue(by["操作审计覆盖率"]["达标"], by["操作审计覆盖率"]["实测"])
        self.assertTrue(by["到期自动销毁执行率"]["达标"], by["到期自动销毁执行率"]["实测"])
        self.assertTrue(by["越权访问测试"]["达标"])
        self.assertTrue(by["替代通道可用性"]["达标"], by["替代通道可用性"]["实测"])
        # 第 6 项：加密数据不可修改（只读）—— 哈希+非对称签名
        self.assertIn("加密数据不可修改（只读）", by)
        self.assertTrue(by["加密数据不可修改（只读）"]["达标"],
                        by["加密数据不可修改（只读）"]["实测"])
        self.assertEqual(acc["passed"], acc["total"])
        self.assertTrue(acc["all_passed"])

    def test_deny_count_reflects_real_denials(self):
        self._seed_full()
        acc = self.gov.acceptance(lifecycle=self.life, rights=self.rights)
        by = {it["指标项"]: it for it in acc["items"]}
        self.assertGreaterEqual(by["越权访问测试"]["detail"]["denied"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
