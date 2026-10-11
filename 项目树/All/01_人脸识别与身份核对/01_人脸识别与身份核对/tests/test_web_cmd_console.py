# -*- coding: utf-8 -*-
"""
网页端命令控制台：HTTP 层权限测试。

重点验证"绕过前端也拿不到执行权"，以及**管理员永远拿不到完整权限**：
前端只负责显示，真正的判定在服务端的角色校验 + 开关校验 + cmd_console 的层级强制。

运行：
    python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(BASE, "网页操作面板", "server")
for p in (BASE, SERVER):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                         # noqa: E402
import accounts                       # noqa: E402
import auth                           # noqa: E402
import db as dbmod                    # noqa: E402
from db import get_db, init_db        # noqa: E402
import store as storemod              # noqa: E402

import app as webapp                  # noqa: E402

_TMP = os.path.join(BASE, "tests", "_tmp")

OWNER = accounts.ROLE_OWNER
ADMIN = accounts.ROLE_ADMIN


class WebCmdConsoleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """
        隔离到临时库 / 临时标记文件，**绝不碰真实数据**。

        为什么必须隔离（真实教训）：本文件早期版本直接对 config.DB_PATH 调 init_db()，
        单独跑这个文件时就会在你的真实库里**创建一个 admin 账户**并写出初始口令文件。
        test_web.py 用的是模块级重定向，但那会覆盖同进程其它模块的路径；
        这里改用"保存 -> 改 -> 还原"，只在本类运行期间生效，不干扰别人。
        """
        os.makedirs(_TMP, exist_ok=True)
        cls._saved = {
            "db_path": config.DB_PATH,
            "audit": getattr(config, "AUDIT_LOG_PATH", None),
            "action": getattr(config, "ACTION_LOG_PATH", None),
            "flag": accounts.CMD_CONSOLE_FLAG,
            "admin_flag": accounts.CMD_CONSOLE_ADMIN_FLAG,
            "dbmod_path": getattr(dbmod, "DB_PATH", None),
            "pwd_file": getattr(dbmod, "INIT_PWD_FILE", None),
        }

        cls.tmp_db = os.path.join(_TMP, "cmdweb_%s.db" % uuid.uuid4().hex[:8])
        real = str(cls._saved["db_path"] or "")
        # 复制真实库：这样 owner / admin 账户的既有数据可用；临时路径则不必复制
        if os.path.exists(real) and os.sep + "_tmp" + os.sep not in real:
            shutil.copyfile(real, cls.tmp_db)

        config.DB_PATH = Path(cls.tmp_db)
        # 审计链与操作日志都要重定向：/api/owner-settings 会写两者，
        # 只改审计链的话操作日志仍会被灌入测试记录（实测 +16 条）。
        config.AUDIT_LOG_PATH = os.path.join(_TMP, "cmdweb_audit.log")
        config.ACTION_LOG_PATH = os.path.join(_TMP, "cmdweb_action.log")
        accounts.CMD_CONSOLE_FLAG = os.path.join(_TMP, "cmd_console.on")
        accounts.CMD_CONSOLE_ADMIN_FLAG = os.path.join(_TMP, "cmd_console_admin.on")
        dbmod.DB_PATH = cls.tmp_db
        dbmod.INIT_PWD_FILE = os.path.join(_TMP, "初始密码_cmdweb.txt")

        init_db()
        accounts.ensure_schema_and_seed()
        # app.py 的 store 是**在别的测试模块导入 app 时**构造的（CandidateStore 会把
        # config.DB_PATH 固化进 self.db_path），绑定的是那个模块的临时库。这里必须
        # 重新绑定到本类的临时库，否则 store.log_change() 会往一个已删除的库写
        # data_change_log，报 "no such table" 让 /api/owner-settings 返回 500。
        # CandidateStore 构造时内部就会 init_schema()，业务表随之一并建好。
        webapp.store = storemod.CandidateStore(cls.tmp_db)
        cls.client = webapp.app.test_client()
        conn = get_db()
        try:
            cls.users = [dict(r) for r in conn.execute(
                "SELECT id, username, role, is_active FROM users").fetchall()]
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        """还原被改的路径，删掉临时文件——不给真实环境留痕"""
        s = cls._saved
        config.DB_PATH = s["db_path"]
        if s["audit"] is not None:
            config.AUDIT_LOG_PATH = s["audit"]
        if s["action"] is not None:
            config.ACTION_LOG_PATH = s["action"]
        accounts.CMD_CONSOLE_FLAG = s["flag"]
        accounts.CMD_CONSOLE_ADMIN_FLAG = s["admin_flag"]
        if s["dbmod_path"] is not None:
            dbmod.DB_PATH = s["dbmod_path"]
        if s["pwd_file"] is not None:
            dbmod.INIT_PWD_FILE = s["pwd_file"]

        for p in (cls.tmp_db, cls.tmp_db + "-wal", cls.tmp_db + "-shm",
                  os.path.join(_TMP, "cmdweb_audit.log"),
                  os.path.join(_TMP, "cmdweb_action.log"),
                  os.path.join(_TMP, "初始密码_cmdweb.txt"),
                  os.path.join(_TMP, "cmd_console.on"),
                  os.path.join(_TMP, "cmd_console_admin.on")):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def setUp(self):
        self._orig_main = accounts.cmd_console_enabled()
        self._orig_admin = accounts.cmd_console_admin_allowed()
        accounts.set_cmd_console(False)
        accounts.set_cmd_console_admin(False)

    def tearDown(self):
        accounts.set_cmd_console(self._orig_main)
        accounts.set_cmd_console_admin(self._orig_admin)

    # ---------------- 辅助 ----------------
    def _user(self, role):
        return next((u for u in self.users
                     if u["role"] == role and u["is_active"]), None)

    def _headers(self, user):
        if user is None:
            return {}
        return {"Authorization": "Bearer " + auth.generate_token(
            user["id"], user["username"], user["role"])}

    def _run(self, user, code="echo web_case", mode="cmd", timeout=None):
        body = {"code": code, "mode": mode}
        if timeout is not None:
            body["timeout"] = timeout
        return self.client.post("/api/cmd-console/run", json=body,
                                headers=self._headers(user))

    def _status(self, user):
        return self.client.get("/api/cmd-console", headers=self._headers(user))

    # ---------------- 鉴权 ----------------
    def test_unauthenticated_rejected(self):
        self.assertEqual(self.client.get("/api/cmd-console").status_code, 401)
        self.assertEqual(
            self.client.post("/api/cmd-console/run", json={"code": "echo x"}).status_code, 401)

    def test_teacher_cannot_even_read_status(self):
        """教师连"这台机器有没有开这个入口"都不该知道"""
        u = self._user("teacher")
        if not u:
            self.skipTest("无教师账户")
        self.assertEqual(self._status(u).status_code, 403)

    def test_teacher_cannot_execute(self):
        accounts.set_cmd_console(True)
        u = self._user("teacher")
        if not u:
            self.skipTest("无教师账户")
        self.assertEqual(self._run(u).status_code, 403)

    def test_owner_and_admin_can_read_status(self):
        """owner/admin 可读状态（但读得到不等于跑得动）"""
        for role in (OWNER, ADMIN):
            u = self._user(role)
            if not u:
                continue
            with self.subTest(role=role):
                r = self._status(u)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(r.get_json()["can_run"], "默认关闭时不得可执行")

    # ---------------- 默认关闭 ----------------
    def test_owner_blocked_while_disabled(self):
        owner = self._user(OWNER)
        self.assertIsNotNone(owner, "缺少所有者账户")
        d = self._status(owner).get_json()
        self.assertFalse(d["enabled"])
        self.assertFalse(d["can_run"])
        self.assertEqual(d["tier"], "none")
        self.assertEqual(self._run(owner).status_code, 403)

    # ---------------- 所有者：完整权限 ----------------
    def test_owner_gets_full(self):
        owner = self._user(OWNER)
        self.assertIsNotNone(owner, "缺少所有者账户")
        r = self.client.post("/api/owner-settings", json={"cmd_console": True},
                             headers=self._headers(owner))
        self.assertEqual(r.status_code, 200)

        d = self._status(owner).get_json()
        self.assertEqual(d["tier"], "full")
        self.assertTrue(d["can_full"])
        self.assertEqual(sorted(d["modes"]), ["cmd", "python"])

        r = self._run(owner, "echo web_probe_ok", mode="cmd")
        self.assertEqual(r.status_code, 200)
        self.assertIn("web_probe_ok", r.get_json()["stdout"])
        self.assertEqual(r.get_json()["tier"], "full")

    # ---------------- 管理员：只有 ollama ----------------
    def test_admin_blocked_without_delegation(self):
        accounts.set_cmd_console(True)          # 总开关开了，但未授权管理员
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        self.assertEqual(self._run(admin).status_code, 403)
        self.assertEqual(self._status(admin).get_json()["tier"], "none")

    def test_admin_gets_ollama_after_delegation(self):
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        d = self._status(admin).get_json()
        self.assertEqual(d["tier"], "ollama")
        self.assertTrue(d["can_run"])
        self.assertFalse(d["can_full"], "管理员永远不能拿到完整权限")
        self.assertEqual(d["modes"], ["ollama"])

    def test_admin_cannot_run_arbitrary_command(self):
        """核心：授权后管理员发来的任意命令仍必须在执行前被拦下"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        for code in ("calc", "dir /s", "whoami", "ollama pull llama3"):
            with self.subTest(code=code):
                r = self._run(admin, code, mode="cmd")
                self.assertEqual(r.status_code, 200)
                body = r.get_json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["mode"], "ollama-rejected")
                self.assertIsNone(body["exit_code"], "被拒绝的命令根本不应执行")

    def test_admin_cannot_escalate_via_mode_field(self):
        """管理员把 mode 改成 python 也没用：层级由服务端决定"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        r = self._run(admin, "print(1+1)", mode="python")
        self.assertEqual(r.get_json()["mode"], "ollama-rejected")
        self.assertFalse(r.get_json()["ok"])

    def test_admin_whitelisted_command_passes_to_execution(self):
        """
        授权管理员的合法 ollama 命令应通过白名单进入执行阶段。
        用一个不存在的模型名，避免真的拉起模型推理；断言只关注"是否过了白名单"。
        """
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        r = self._run(admin, "ollama run exam_patrol_nonexistent_model_xyz hello",
                      mode="ollama", timeout=15)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["tier"], "ollama")
        self.assertNotEqual(body["mode"], "ollama-rejected",
                            "合法形式不应被白名单拒绝：%s" % body.get("stderr"))

    # ---------------- 开关不可越权修改 ----------------
    def test_admin_cannot_toggle_switches(self):
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        for key in ("cmd_console", "cmd_console_admin"):
            with self.subTest(key=key):
                r = self.client.post("/api/owner-settings", json={key: True},
                                     headers=self._headers(admin))
                self.assertEqual(r.status_code, 403)
        self.assertFalse(accounts.cmd_console_enabled())
        self.assertFalse(accounts.cmd_console_admin_allowed())

    def test_disable_takes_effect_immediately(self):
        owner = self._user(OWNER)
        self.assertIsNotNone(owner, "缺少所有者账户")
        self.client.post("/api/owner-settings", json={"cmd_console": True},
                         headers=self._headers(owner))
        self.assertEqual(self._run(owner).status_code, 200)
        self.client.post("/api/owner-settings", json={"cmd_console": False},
                         headers=self._headers(owner))
        self.assertEqual(self._run(owner).status_code, 403, "关闭后应立即失效，无需重启")

    def test_invalid_mode_rejected(self):
        owner = self._user(OWNER)
        self.assertIsNotNone(owner, "缺少所有者账户")
        accounts.set_cmd_console(True)
        r = self._run(owner, "echo x", mode="evil")
        self.assertEqual(r.status_code, 400)

    # ---------------- 留痕 ----------------
    def test_web_execution_is_audited_with_tier(self):
        from models import RC_CMD_EXEC
        from audit import AuditLogger
        owner = self._user(OWNER)
        self.assertIsNotNone(owner, "缺少所有者账户")
        accounts.set_cmd_console(True)
        before = len(AuditLogger().query())

        self._run(owner, "echo web_audit_probe")

        after = AuditLogger().query()
        self.assertGreater(len(after), before)
        last = after[-1]
        self.assertEqual(last.get("reason_code"), RC_CMD_EXEC)
        self.assertEqual(last.get("operator"), owner["username"])
        self.assertIn("web", last.get("reason") or "")
        self.assertIn("full/", last.get("reason") or "")
        self.assertEqual(after[-1].get("prev_hash"), after[-2].get("hash"))

    def test_admin_rejection_is_audited(self):
        from audit import AuditLogger
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        admin = self._user(ADMIN)
        if not admin:
            self.skipTest("无管理员账户")
        before = len(AuditLogger().query())
        self._run(admin, "calc")
        after = AuditLogger().query()
        self.assertGreater(len(after), before)
        last = after[-1]
        self.assertEqual(last.get("result"), "BLOCKED")
        self.assertIn("ollama/", last.get("reason") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
