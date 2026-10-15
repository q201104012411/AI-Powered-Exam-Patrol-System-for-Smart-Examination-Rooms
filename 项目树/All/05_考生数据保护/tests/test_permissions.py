# -*- coding: utf-8 -*-
"""权限最小化与持续校验（第三节第 2 条 / 第六节"越权访问测试"）单元测试

覆盖：
  · 角色 × 动作默认拒绝（白名单之外的组合一律拒绝）
  · 越权提升尝试必须被拦截并留痕
  · 监考员只能看本考场（数据范围最小化）
  · 持续校验：高风险会话（未登记设备/地点、非工作时段）执行敏感动作需二次确认
  · 账户口令用 PBKDF2 存储，不落明文
"""
import unittest

from tests._base import DppTestCase
from dpp import access, config


class TestRoleActions(DppTestCase):

    def test_every_role_has_actions(self):
        for role in config.ROLE_ACTIONS:
            self.assertTrue(config.ROLE_ACTIONS[role], "%s 无任何动作" % role)

    def test_actions_are_valid(self):
        """角色动作必须是 ALL_ACTIONS 的子集（防止写了错别字导致"永不生效"）。"""
        for role, acts in config.ROLE_ACTIONS.items():
            bad = set(acts) - set(config.ALL_ACTIONS)
            self.assertEqual(bad, set(), "%s 含未知动作：%s" % (role, bad))

    def test_default_deny_unknown_action(self):
        """未登记的动作名一律拒绝（默认拒绝）。"""
        s = self.login("viewer1")
        self.assertFalse(s.can("不存在的动作"))
        with self.assertRaises(access.AccessError):
            s.require("不存在的动作")

    def test_viewer_can_only_view(self):
        s = self.login("viewer1")
        self.assertTrue(s.can("view"))
        for a in ("register", "use", "reveal", "share", "approve",
                  "destroy", "force_destroy", "manage", "rights_decide"):
            self.assertFalse(s.can(a), "只读角色不应拥有 %s" % a)

    def test_invigilator_can_only_view(self):
        s = self.login("inv01")
        self.assertEqual(set(config.ROLE_ACTIONS["invigilator"]), {"view"})

    def test_owner_has_all_actions(self):
        s = self.login("owner")
        for a in config.ALL_ACTIONS:
            self.assertTrue(s.can(a), "所有者应拥有 %s" % a)


class TestPrivilegeEscalation(DppTestCase):

    def test_viewer_cannot_register(self):
        s = self.login("viewer1")
        with self.assertRaises(access.AccessError):
            self.life.collect(s, name="越权考生", id_card_no="330100200801011234",
                              consent_id="x")

    def test_viewer_cannot_reveal_plain(self):
        res = self.collect_one(name="被窥考生")
        s = self.login("viewer1")
        with self.assertRaises(access.AccessError):
            self.life.reveal_plain(s, res["cand_ref"], "偷看")

    def test_operator_cannot_destroy(self):
        """业务操作员可采集但不可销毁（职责分离）。"""
        res = self.collect_one(name="供销毁考生")
        s = self.login("oper1")
        with self.assertRaises(access.AccessError):
            self.life.destroy_by_request(s, res["cand_ref"], reason="想删就删")

    def test_operator_cannot_force_destroy(self):
        res = self.collect_one(name="供强删考生")
        s = self.login("oper1")
        with self.assertRaises(access.AccessError):
            self.life.destroy_by_request(s, res["cand_ref"], reason="误采",
                                         force=True, witness="某人")

    def test_operator_cannot_approve_share(self):
        res = self.collect_one(name="供审批考生")
        oper = self.login("oper1")
        sh = self.life.request_share(oper, [res["cand_ref"]], ["ref"])
        with self.assertRaises(access.AccessError):
            self.life.approve_share(oper, sh["req_id"], approve=True)

    def test_auditor_cannot_register(self):
        s = self.login("auditor1")
        with self.assertRaises(access.AccessError):
            self.life.collect(s, name="审计越权", id_card_no="330100200801011234")

    def test_denied_action_is_not_silently_ignored(self):
        """越权必须抛异常，而不是悄悄返回 None（否则调用方会以为成功了）。"""
        s = self.login("auditor1")
        with self.assertRaises(access.AccessError):
            self.life.collect(s, name="x")


class TestRoomScope(DppTestCase):

    def setUp(self):
        super().setUp()
        self.collect_one(name="A01考生", room_id="A-01", seat_no="01")
        self.collect_one(name="A02考生", room_id="A-02", seat_no="02")

    def test_invigilator_sees_only_own_room(self):
        s = self.login("inv01")           # 只可见 A-01
        rows = access.visible_candidates(self.store, s)
        self.assertTrue(rows)
        self.assertTrue(all(r.room_id == "A-01" for r in rows))

    def test_owner_sees_all(self):
        s = self.login("owner")
        rows = access.visible_candidates(self.store, s)
        rooms = {r.room_id for r in rows}
        self.assertIn("A-01", rooms)
        self.assertIn("A-02", rooms)

    def test_room_filter_none_means_all(self):
        self.assertIsNone(self.login("owner").room_filter())
        self.assertEqual(self.login("inv01").room_filter(), ["A-01"])


class TestContinuousVerification(DppTestCase):

    def test_normal_session_is_low_risk(self):
        s = self.login("owner")
        self.assertEqual(s.risk_level(), "low")
        self.assertTrue(s.require("reveal"))       # 低风险直接放行

    def test_high_risk_requires_confirm(self):
        """未登记设备 + 未登记地点 -> 高风险，敏感动作需二次确认。"""
        s = self.login("owner", place="", device="")
        self.assertEqual(s.risk_level(), "high")
        with self.assertRaises(access.AccessError):
            s.require("reveal")

    def test_confirm_risky_unlocks(self):
        s = self.login("owner", place="", device="")
        s.confirm_risky()
        self.assertTrue(s.require("reveal"))

    def test_night_login_raises_score(self):
        """深夜登录 + 一个未登记项 -> 高风险（评分 >= 2）。"""
        s = self.login("owner", hour=2, place="", device="")
        self.assertEqual(s.risk_level(), "high")

    def test_single_factor_is_medium(self):
        """只有一个风险因子（深夜登录）时为中风险，不误判为高风险。"""
        s = self.login("owner", hour=2, place="考点考务办", device="DEV-TEST-01")
        self.assertEqual(s.risk_level(), "medium")

    def test_low_risk_action_not_blocked(self):
        """低风险动作（view）在高风险会话下也不应被二次确认拦住。"""
        s = self.login("owner", place="", device="")
        self.assertTrue(s.require("view"))

    def test_high_risk_blocks_share_in_real_flow(self):
        res = self.collect_one(name="高风险共享考生")
        s = self.login("owner", place="", device="")
        with self.assertRaises(access.AccessError):
            self.life.request_share(s, [res["cand_ref"]], ["ref"])


class TestAccounts(DppTestCase):

    def test_password_not_stored_plaintext(self):
        """账户口令必须是 PBKDF2 哈希，不得出现明文。"""
        raw = self.store.get_kv("accounts", [])
        import json
        blob = json.dumps(raw, ensure_ascii=False)
        self.assertNotIn("EDY_POWERQ", blob)
        self.assertIn("pbkdf2$", blob)

    def test_wrong_password_rejected(self):
        """口令错误时认证失败（返回 None），并留下失败审计。"""
        s = self.accounts.authenticate("owner", "wrong-password",
                                       place="考点", device="D1")
        self.assertIsNone(s)
        self.assertTrue(self.store.audit_actions().get("登录"))
        # 失败审计里不得出现明文口令
        for row in self.store.audit_rows(limit=50):
            self.assertNotIn("wrong-password", str(row["detail"]))

    def test_unknown_user_rejected(self):
        s = self.accounts.authenticate("nobody", "x", place="考点", device="D1")
        self.assertIsNone(s)

    def test_authenticate_returns_session_on_success(self):
        s = self.accounts.authenticate("owner", "EDY_POWERQ",
                                       place="考点", device="D1")
        self.assertIsNotNone(s)
        self.assertEqual(s.role, config.ROLE_OWNER)

    def test_create_and_login_new_account(self):
        owner = self.login("owner")
        acc = self.accounts
        ok, msg = acc.create("oper2", "oper2pass", config.ROLE_OPERATOR,
                             display_name="考务操作员B", rooms=["B-01"],
                             by_role="owner")
        self.assertTrue(ok, msg)
        s = acc.authenticate("oper2", "oper2pass", place="考点", device="D9")
        self.assertTrue(s.can("register"))
        self.assertFalse(s.can("destroy"))

    def test_role_escalation_via_by_role_is_blocked(self):
        """非所有者不得创建账户（越权建号）。返回 (False, 原因)。"""
        ok, msg = self.accounts.create("hacker", "p", config.ROLE_VIEWER,
                                       by_role="operator")
        self.assertFalse(ok)
        self.assertIn("权限", msg)
        self.assertNotIn("hacker", self.accounts.list_accounts())

    def test_cannot_create_peer_or_higher_role(self):
        """不能创建与自己同级或更高权限的账户（防止横向扩权）。"""
        ok, msg = self.accounts.create("boss2", "p", config.ROLE_OWNER,
                                       by_role="owner")
        self.assertFalse(ok)
        self.assertIn("同级", msg)

    def test_delete_account_requires_owner(self):
        owner = self.login("owner")
        self.accounts.create("temp1", "p", config.ROLE_VIEWER, by_role="owner")
        ok, msg = self.accounts.delete("temp1", by_role="operator")
        self.assertFalse(ok)
        self.assertIn("temp1", self.accounts.list_accounts())

    def test_account_actions_are_audited(self):
        owner = self.login("owner")
        self.accounts.create("temp2", "p", config.ROLE_VIEWER, by_role="owner")
        self.assertTrue(self.store.audit_actions().get("账户"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
