# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 账户与权限单元测试

运行：python -m unittest discover -s tests -t .
全程在临时数据目录里跑（EXAM_CAMERA_HOME），不碰真实 data\\。

覆盖：
  · 登录成功/失败（含审计留痕）
  · 角色权限矩阵（owner / admin 逐项）
  · 越权被拒（管理员删机位 / 看审计 / 管账户 / 切隐藏 cmd 全部拒绝）
  · 账户增删改（不能建同级、不能删 owner、不能删自己、至少留一个）
  · 隐藏 cmd 标记文件读写
  · 桌面端与网页端**同一份账户、同一套哈希**（交叉验证）
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 数据目录用测试包共用的临时目录（tests\__init__.py 里设好），必须在 import config 之前
from tests import shared_tmp   # noqa: E402
_TMP = shared_tmp()

import config          # noqa: E402
import models          # noqa: E402
import storage         # noqa: E402
import web_accounts    # noqa: E402


def tearDownModule():
    """不删共用临时目录，统一由 tests\\__init__.py 退出时清理"""
    return None


class AccountsBase(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(_TMP, "acc_%s.db" % self.id().split(".")[-1])
        if os.path.exists(self.db):
            os.remove(self.db)
        self.store = storage.Store(db_path=self.db)
        self.acc = web_accounts.Accounts(self.store)

    def session(self, username, password):
        s, msg = self.acc.authenticate(username, password)
        self.assertIsNotNone(s, msg)
        return s


# ==================== 登录 ====================
class TestLogin(AccountsBase):
    def test_default_accounts_seeded(self):
        """首次自动预置 owner / teacher1，角色与网页端一致"""
        self.assertEqual(self.acc.usernames(), ["owner", "teacher1"])
        self.assertEqual(self.acc.get("owner")["role"], web_accounts.ROLE_OWNER)
        self.assertEqual(self.acc.get("teacher1")["role"], web_accounts.ROLE_ADMIN)

    def test_login_ok(self):
        s, msg = self.acc.authenticate("owner", "EDY_POWERQ")
        self.assertIsNotNone(s)
        self.assertEqual(s.username, "owner")
        self.assertEqual(s.role, web_accounts.ROLE_OWNER)
        self.assertEqual(s.role_text, "所有者")
        self.assertTrue(s.can("device_delete"))

    def test_login_bad_password(self):
        s, msg = self.acc.authenticate("owner", "wrong")
        self.assertIsNone(s)
        self.assertIn("口令", msg)

    def test_login_unknown_user(self):
        s, _ = self.acc.authenticate("nobody", "x")
        self.assertIsNone(s)

    def test_login_empty(self):
        self.assertIsNone(self.acc.authenticate("", "")[0])

    def test_login_writes_audit_both_ways(self):
        self.acc.authenticate("owner", "EDY_POWERQ")
        self.acc.authenticate("owner", "bad")
        actions = [r["action"] for r in self.store.audit_rows(limit=50)]
        self.assertIn("登录成功", actions)
        self.assertIn("登录失败", actions)
        self.assertTrue(self.store.verify_chain()[0])

    def test_password_hash_format_matches_web(self):
        """哈希格式必须还是 pbkdf2$salt$dk，120000 轮——否则网页端老口令全废"""
        stored = self.acc.get("owner")["pwd"]
        self.assertTrue(stored.startswith("pbkdf2$"))
        salt, dk = stored.split("$")[1], stored.split("$")[2]
        self.assertEqual(len(salt), 32)        # 16 字节十六进制
        self.assertEqual(len(dk), 64)          # sha256 十六进制
        self.assertTrue(web_accounts.verify_password("EDY_POWERQ", stored))
        self.assertFalse(web_accounts.verify_password("edY_powerq", stored))

    def test_verify_rejects_garbage(self):
        self.assertFalse(web_accounts.verify_password("x", ""))
        self.assertFalse(web_accounts.verify_password("x", "md5$aa$bb"))
        self.assertFalse(web_accounts.verify_password("x", "pbkdf2$nothex$zz"))

    def test_old_style_record_still_verifies(self):
        """老库里已存的口令（用网页端原算法生成）必须还能验过：salt/格式完全一致"""
        salt = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        stored = ("pbkdf2$%s$" % salt) + __import__("hashlib").pbkdf2_hmac(
            "sha256", "EDY_POWERQ".encode("utf-8"), bytes.fromhex(salt), 120000).hex()
        self.assertTrue(web_accounts.verify_password("EDY_POWERQ", stored))


# ==================== 权限矩阵 ====================
class TestPermissionMatrix(AccountsBase):
    OWNER_ONLY = ("device_delete", "account_manage", "audit_view")
    BOTH = ("view", "device_add", "baseline_capture", "inspect_upload", "patrol_run",
            "workorder_operate", "report_export", "change_own_pwd")

    def test_owner_can_everything(self):
        for a in list(web_accounts.CAN):
            self.assertTrue(web_accounts.can(a, web_accounts.ROLE_OWNER), a)

    def test_admin_can_daily_work(self):
        for a in self.BOTH:
            self.assertTrue(web_accounts.can(a, web_accounts.ROLE_ADMIN), a)

    def test_admin_cannot_owner_only(self):
        for a in self.OWNER_ONLY:
            self.assertFalse(web_accounts.can(a, web_accounts.ROLE_ADMIN), a)

    def test_unknown_role_and_action_denied(self):
        self.assertFalse(web_accounts.can("device_delete", "teacher"))
        self.assertFalse(web_accounts.can("device_delete", ""))
        self.assertFalse(web_accounts.can("nope", web_accounts.ROLE_OWNER))
        self.assertFalse(web_accounts.can(None, web_accounts.ROLE_OWNER))

    def test_deny_text_is_explicit(self):
        t = web_accounts.require_text("device_delete", web_accounts.ROLE_ADMIN)
        self.assertIn("权限不足", t)
        self.assertIn("删除机位", t)
        self.assertIn("仅所有者", t)

    def test_session_mirrors_matrix(self):
        owner = self.session("owner", "EDY_POWERQ")
        admin = self.session("teacher1", "teacher1")
        self.assertTrue(owner.can("audit_view"))
        self.assertFalse(admin.can("audit_view"))
        self.assertFalse(admin.to_dict()["can_manage_accounts"])
        self.assertTrue(owner.to_dict()["can_manage_accounts"])

    def test_can_manage_levels(self):
        self.assertTrue(web_accounts.can_manage("owner", "admin"))
        self.assertFalse(web_accounts.can_manage("owner", "owner"))
        self.assertFalse(web_accounts.can_manage("admin", "owner"))
        self.assertFalse(web_accounts.can_manage("admin", "admin"))


# ==================== 越权被拒（方法层，不只是按钮） ====================
class TestPrivilegeEscalation(AccountsBase):
    def dev(self, code="CAM-1"):
        d = models.Device(code=code, room_id="A-01", name=code, position="前墙")
        self.store.save_device(d)
        return d

    def test_admin_cannot_delete_device_via_permission(self):
        """管理员删机位：权限判定必须为 False（界面禁用 + 后端拦）"""
        admin = self.session("teacher1", "teacher1")
        self.assertFalse(admin.can("device_delete"))
        self.assertIn("权限不足", admin.deny_text("device_delete"))

    def test_admin_cannot_manage_accounts(self):
        admin = self.session("teacher1", "teacher1")
        self.assertFalse(admin.can("account_manage"))
        ok, msg = self.acc.create("newguy", "pwd12345", web_accounts.ROLE_ADMIN,
                                  by="teacher1")
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)
        self.assertFalse(self.acc.exists("newguy"))

    def test_admin_cannot_reset_owner_password(self):
        """堵死"管理员重置所有者口令 -> 夺权"这条路"""
        self.session("teacher1", "teacher1")
        ok, msg = self.acc.reset_password("owner", "hacked123", by="teacher1")
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)
        self.assertIsNotNone(self.acc.authenticate("owner", "EDY_POWERQ")[0])

    def test_admin_cannot_delete_owner(self):
        ok, msg = self.acc.delete("owner", by="teacher1")
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)
        self.assertTrue(self.acc.exists("owner"))

    def test_owner_cannot_create_peer_owner(self):
        ok, msg = self.acc.create("owner2", "pwd12345", web_accounts.ROLE_OWNER,
                                  by="owner")
        self.assertFalse(ok)
        self.assertIn("权限不足", msg)
        self.assertFalse(self.acc.exists("owner2"))

    def test_admin_cannot_view_audit(self):
        self.assertFalse(web_accounts.can("audit_view", web_accounts.ROLE_ADMIN))

    def test_admin_can_toggle_hidden_cmd(self):
        self.assertTrue(web_accounts.can("hide_cmd_toggle", web_accounts.ROLE_ADMIN))


# ==================== 账户增删改 ====================
class TestAccountCRUD(AccountsBase):
    def test_create_and_login(self):
        ok, msg = self.acc.create("teacher2", "pwd12345", web_accounts.ROLE_ADMIN,
                                  "巡考管理员2", by="owner")
        self.assertTrue(ok, msg)
        s, _ = self.acc.authenticate("teacher2", "pwd12345")
        self.assertIsNotNone(s)
        self.assertEqual(s.role, web_accounts.ROLE_ADMIN)
        self.assertEqual(self.acc.get("teacher2")["name"], "巡考管理员2")

    def test_create_rejects_short_or_duplicate(self):
        self.assertFalse(self.acc.create("t2", "123", web_accounts.ROLE_ADMIN,
                                         by="owner")[0])
        self.assertFalse(self.acc.create("teacher1", "pwd12345",
                                         web_accounts.ROLE_ADMIN, by="owner")[0])
        self.assertFalse(self.acc.create("", "pwd12345", web_accounts.ROLE_ADMIN,
                                         by="owner")[0])
        self.assertFalse(self.acc.create("t3", "pwd12345", "teacher", by="owner")[0])

    def test_reset_password_changes_login(self):
        self.acc.create("teacher2", "pwd12345", web_accounts.ROLE_ADMIN, by="owner")
        ok, msg = self.acc.reset_password("teacher2", "newpwd123", by="owner")
        self.assertTrue(ok, msg)
        self.assertIsNone(self.acc.authenticate("teacher2", "pwd12345")[0])
        self.assertIsNotNone(self.acc.authenticate("teacher2", "newpwd123")[0])
        self.assertFalse(self.acc.reset_password("teacher2", "123", by="owner")[0])
        self.assertFalse(self.acc.reset_password("nobody", "pwd12345", by="owner")[0])

    def test_delete_account(self):
        self.acc.create("teacher2", "pwd12345", web_accounts.ROLE_ADMIN, by="owner")
        ok, msg = self.acc.delete("teacher2", by="owner")
        self.assertTrue(ok, msg)
        self.assertFalse(self.acc.exists("teacher2"))
        self.assertIsNone(self.acc.authenticate("teacher2", "pwd12345")[0])

    def test_cannot_delete_self(self):
        ok, msg = self.acc.delete("owner", by="owner")
        self.assertFalse(ok)
        self.assertIn("不能删除当前登录", msg)
        self.assertTrue(self.acc.exists("owner"))

    def test_cannot_delete_owner(self):
        """所有者账户不可删（提示里点明"不能删除所有者账户"）"""
        ok, msg = self.acc.delete("owner", by="teacher1")
        self.assertFalse(ok)
        self.assertIn("所有者", msg)
        self.assertTrue(self.acc.exists("owner"))

    def test_keep_at_least_one_account(self):
        ok, msg = self.acc.delete("teacher1", by="owner")
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.acc.usernames()), 1)
        # 只剩 owner：再删就应被拒（不存在可删对象时也要给明确提示）
        self.assertFalse(self.acc.delete("ghost", by="owner")[0])

    def test_change_own_password(self):
        ok, msg = self.acc.change_password("teacher1", "teacher1", "teacher1new")
        self.assertTrue(ok, msg)
        self.assertIsNotNone(self.acc.authenticate("teacher1", "teacher1new")[0])
        self.assertFalse(self.acc.change_password("teacher1", "wrong", "x123456")[0])
        self.assertFalse(self.acc.change_password("teacher1", "teacher1new", "123")[0])

    def test_list_accounts_has_no_hash(self):
        rows = self.acc.list_accounts()
        self.assertEqual([r["username"] for r in rows], ["owner", "teacher1"])
        for r in rows:
            self.assertNotIn("pwd", r)
            self.assertIn("role_text", r)
        self.assertNotIn("pbkdf2", web_accounts.dump(self.acc.data))

    def test_accounts_persist_in_same_kv_key(self):
        """账户必须落在 kv 表的 web.accounts 上（网页端读的就是这个键）"""
        raw = self.store.get_kv("web.accounts")
        self.assertIsInstance(raw, dict)
        self.assertIn("owner", raw)
        self.assertTrue(raw["owner"]["pwd"].startswith("pbkdf2$"))
        # 新开一个 Store（相当于换个进程/换网页端）读到的还是同一份
        other = web_accounts.Accounts(storage.Store(db_path=self.db))
        self.assertIsNotNone(other.authenticate("owner", "EDY_POWERQ")[0])


# ==================== 隐藏 cmd 标记 ====================
class TestHiddenCmd(AccountsBase):
    def setUp(self):
        AccountsBase.setUp(self)
        self.flag = str(config.HIDE_CMD_FLAG)
        if os.path.exists(self.flag):
            os.remove(self.flag)

    def tearDown(self):
        if os.path.exists(self.flag):
            os.remove(self.flag)

    def test_flag_path_is_under_data_root(self):
        self.assertTrue(self.flag.startswith(str(config.DATA_ROOT)))
        self.assertTrue(self.flag.endswith("hide_cmd.on"))

    def test_default_is_visible(self):
        self.assertFalse(web_accounts.hidden_cmd_on())

    def test_toggle_on_off(self):
        ok, msg = web_accounts.set_hidden_cmd(True, self.store, "owner")
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(self.flag))
        self.assertTrue(web_accounts.hidden_cmd_on())
        ok, msg = web_accounts.set_hidden_cmd(False, self.store, "owner")
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(web_accounts.hidden_cmd_on())

    def test_toggle_writes_audit(self):
        web_accounts.set_hidden_cmd(True, self.store, "owner")
        actions = [r["action"] for r in self.store.audit_rows(limit=20)]
        self.assertIn("隐藏命令行窗口", actions)
        self.assertEqual(self.store.audit_rows(limit=1)[0]["operator"], "owner")

    def test_toggle_off_when_no_flag_is_not_an_error(self):
        self.assertTrue(web_accounts.set_hidden_cmd(False, self.store, "owner")[0])

    def test_admin_can_toggle(self):
        admin = self.session("teacher1", "teacher1")
        self.assertTrue(admin.can("hide_cmd_toggle"))
        self.assertFalse(web_accounts.hidden_cmd_on())


# ==================== 两端共用同一份账户（交叉验证） ====================
class TestSharedAccounts(AccountsBase):
    def test_desktop_and_web_share_one_store(self):
        """
        模拟"桌面端改口令 -> 网页端立刻认新口令"：
        两端都通过 web_accounts.Accounts 读写同一个 kv 键，不另起一套。
        """
        desktop = web_accounts.Accounts(self.store)
        ok, msg = desktop.change_password("teacher1", "teacher1", "shared123")
        self.assertTrue(ok, msg)
        # 网页端进程（新 Store，同库）读到的账户与口令
        web = web_accounts.Accounts(storage.Store(db_path=self.db))
        self.assertIsNotNone(web.authenticate("teacher1", "shared123")[0])
        self.assertIsNone(web.authenticate("teacher1", "teacher1")[0])

    def test_web_seed_visible_to_desktop(self):
        """老库（只有网页端账户）在桌面端登录：不该重新预置、不该改口令"""
        salt = "aabbccddeeff00112233445566778899"
        import hashlib
        old = {"owner": {"pwd": "pbkdf2$%s$%s" % (
            salt, hashlib.pbkdf2_hmac("sha256", "EDY_POWERQ".encode(),
                                      bytes.fromhex(salt), 120000).hex()),
            "role": "owner", "name": "系统所有者"}}
        self.store.set_kv("web.accounts", old)
        acc = web_accounts.Accounts(self.store)
        self.assertEqual(acc.usernames(), ["owner"])
        self.assertIsNotNone(acc.authenticate("owner", "EDY_POWERQ")[0])
        self.assertEqual(self.store.get_kv("web.accounts")["owner"]["pwd"], old["owner"]["pwd"])

    def test_fix_broken_records_without_crash(self):
        self.store.set_kv("web.accounts", {"owner": {"pwd": "x", "role": "boss"},
                                           "bad": "not-a-dict"})
        acc = web_accounts.Accounts(self.store)
        for u in acc.usernames():
            self.assertIn(acc.get(u)["role"], web_accounts.ROLE_LEVEL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
