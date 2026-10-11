# -*- coding: utf-8 -*-
"""
智能终端操作端 · 命令控制台测试（开关 / 权限分层 / 桥接 / 留痕）

运行：python -m unittest discover -s tests -v
测试全程在临时数据目录里跑（EXAM_TERMINAL_HOME），不碰真实 data\\，
也**不往 01 目录写任何文件**——桥接把 01 的数据根指到本端临时目录（见 console.bridge_home）。

为什么 atexit 而不是 tearDownModule 清理临时目录：
    discover 会把本模块与 test_core 放在同一次运行里，而 config 只在第一次 import 时
    解析 EXAM_TERMINAL_HOME —— 谁先被 import 谁的目录就成了**两个模块共用**的数据根。
    模块级 tearDownModule 在本模块测试跑完时立刻执行，那时另一个模块可能还没跑，
    删早了它就会找不到目录。因此清理推迟到进程退出。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="terminal_console_test_")
# setdefault：另一个测试模块若已设过 EXAM_TERMINAL_HOME，就沿用它，两边看同一个数据根
os.environ.setdefault("EXAM_TERMINAL_HOME", _TMP)

import config        # noqa: E402
import bridge        # noqa: E402
import console       # noqa: E402
import storage       # noqa: E402

atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

_CMD_BRIDGE = os.path.join(ROOT, "_cmd_bridge.py")


def run_bridge(cmd_dir, code, mode="cmd", role="owner", operator="tester",
               timeout=30, drop_bytecode_env=False):
    """
    直接跑桥接脚本（不经过 03 端的 CmdBridge），返回解析后的 JSON。

    用途：验证**桥接脚本与 01 那一层**自己的行为（权限拒、只读性、异常回传）。
    drop_bytecode_env=True 时故意摘掉 PYTHONDONTWRITEBYTECODE，只靠
    _cmd_bridge.py 里的 sys.dont_write_bytecode 来保证不写 01 的 .pyc ——
    手工调用桥接脚本时不会有那个环境变量，这条路径必须自己站得住。
    """
    home = console.bridge_home()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if drop_bytecode_env:
        env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["EXAM_HOME"] = home
    env["EXAM_DB"] = os.path.join(home, "exam.db")
    cmd = [sys.executable, _CMD_BRIDGE, "--face-dir", cmd_dir, "--code", code,
           "--mode", mode, "--role", role, "--operator", operator,
           "--timeout", str(timeout), "--source", "unittest"]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 90, env=env)
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace")
    for ln in reversed(out.splitlines()):
        if ln.strip().startswith("{"):
            try:
                return json.loads(ln.strip())
            except ValueError:
                continue
    raise AssertionError("桥接没有输出可解析的 JSON：stdout=%s stderr=%s" % (out[:300], err[:300]))


class ConsoleBase(unittest.TestCase):
    """每个用例都从"开关全关"这个出厂状态开始，避免用例之间互相影响"""

    def setUp(self):
        # 另一个测试模块可能已经把它用的临时目录删了（共享同一个 config.DATA_ROOT）
        os.makedirs(str(config.DATA_ROOT), exist_ok=True)
        console.set_console(False)
        console.set_console_admin(False)
        self.addCleanup(console.set_console, False)
        self.addCleanup(console.set_console_admin, False)

    @staticmethod
    def all_roles():
        return [config.ROLE_OWNER, config.ROLE_ADMIN, config.ROLE_PATROL,
                config.ROLE_INVIGILATOR, config.ROLE_COMMANDER, config.ROLE_CHECKER,
                config.ROLE_VIEWER]


# ==================== 开关与权限分层 ====================
class TestConsoleSwitch(ConsoleBase):
    def test_default_closed_all_roles_none(self):
        """默认关闭（标记文件不存在）：给谁都是 none，且没有任何可选类型"""
        self.assertFalse(console.console_enabled())
        self.assertFalse(os.path.exists(str(config.CONSOLE_FLAG)))
        for r in self.all_roles():
            self.assertEqual(console.console_tier(r), console.CONSOLE_TIER_NONE, r)
            self.assertEqual(console.console_modes(r), [], r)
            self.assertFalse(console.console_status(r)["can_run"], r)

    def test_owner_full_after_master_on(self):
        ok, msg = console.set_console(True)
        self.assertTrue(ok, msg)
        self.assertTrue(console.console_enabled())
        self.assertEqual(console.console_tier(config.ROLE_OWNER), "full")
        self.assertEqual(console.console_modes(config.ROLE_OWNER), ["cmd", "python"])
        self.assertTrue(console.console_status(config.ROLE_OWNER)["can_full"])

    def test_master_on_alone_gives_others_nothing(self):
        console.set_console(True)
        for r in self.all_roles():
            if r == config.ROLE_OWNER:
                continue
            self.assertEqual(console.console_tier(r), "none", r)

    def test_admin_needs_separate_authorization(self):
        """管理员默认拿不到任何层级；单独授权后只到 ollama"""
        console.set_console(True)
        self.assertEqual(console.console_tier(config.ROLE_ADMIN), "none")
        self.assertFalse(console.console_admin_allowed())
        ok, msg = console.set_console_admin(True)
        self.assertTrue(ok, msg)
        self.assertTrue(console.console_admin_allowed())
        self.assertEqual(console.console_tier(config.ROLE_ADMIN), "ollama")
        self.assertEqual(console.console_modes(config.ROLE_ADMIN), ["ollama"])

    def test_admin_never_gets_full(self):
        """授权管理员是"能多一点"，不是"升级成所有者"：任何开关组合下都不给 full"""
        for master in (False, True):
            for allow in (False, True):
                console.set_console(master)
                console.set_console_admin(allow)
                for r in self.all_roles():
                    if r == config.ROLE_OWNER:
                        continue
                    tier = console.console_tier(r)
                    self.assertNotEqual(tier, "full", "@master=%s allow=%s role=%s"
                                        % (master, allow, r))
                    self.assertEqual(tier,
                                     "ollama" if (r == config.ROLE_ADMIN and master and allow)
                                     else "none",
                                     "@master=%s allow=%s role=%s" % (master, allow, r))

    def test_master_off_beats_admin_authorization(self):
        """总开关是前提：授权保留着，开关一关也一律 none"""
        console.set_console(True)
        console.set_console_admin(True)
        console.set_console(False)
        self.assertTrue(console.console_admin_allowed())        # 授权设置仍保留
        self.assertEqual(console.console_tier(config.ROLE_ADMIN), "none")
        self.assertEqual(console.console_tier(config.ROLE_OWNER), "none")

    def test_unknown_or_empty_role_is_none(self):
        console.set_console(True)
        console.set_console_admin(True)
        for r in ("", None, "superuser"):
            self.assertEqual(console.console_tier(r), "none")

    def test_switches_are_marker_files(self):
        """与 01 同一套语义：标记文件存在=开启，删除=关闭"""
        self.assertFalse(os.path.exists(str(config.CONSOLE_FLAG)))
        self.assertFalse(os.path.exists(str(config.CONSOLE_ADMIN_FLAG)))
        console.set_console_admin(True)
        self.assertTrue(os.path.exists(str(config.CONSOLE_ADMIN_FLAG)))
        console.set_console(True)
        self.assertTrue(os.path.exists(str(config.CONSOLE_FLAG)))
        console.set_console(False)
        console.set_console_admin(False)
        self.assertFalse(os.path.exists(str(config.CONSOLE_FLAG)))
        self.assertFalse(os.path.exists(str(config.CONSOLE_ADMIN_FLAG)))

    def test_bridge_home_mirrors_switch_state(self):
        """01 侧读的是它自己数据根下的标记文件，镜像必须与开关一致（含关闭方向）"""
        home = console.bridge_home()
        on = os.path.join(home, "cmd_console.on")
        adm = os.path.join(home, "cmd_console_admin.on")
        self.assertFalse(os.path.exists(on))
        self.assertFalse(os.path.exists(adm))
        console.set_console(True)
        console.set_console_admin(True)
        console.bridge_home()
        self.assertTrue(os.path.exists(on))
        self.assertTrue(os.path.exists(adm))
        console.set_console(False)
        console.bridge_home()
        self.assertFalse(os.path.exists(on), "总开关关闭后 01 侧不能残留'开着'的标记")
        console.set_console_admin(False)
        console.bridge_home()
        self.assertFalse(os.path.exists(adm))


# ==================== 留痕 ====================
class TestConsoleAudit(ConsoleBase):
    def _bridge_with_store(self):
        db = os.path.join(_TMP, "console_%s.db" % uuid.uuid4().hex[:8])
        store = storage.Store(db_path=db)
        return store, bridge.CmdBridge(store=store)

    def _actions(self, store):
        return [r["action"] for r in store.audit_rows(limit=200)]

    def test_refused_attempt_is_logged(self):
        """被拒绝的尝试也要留痕：那是有人在试探，比成功执行更值得看"""
        store, cb = self._bridge_with_store()
        r = cb.execute("echo NOPE", mode="cmd", role=config.ROLE_OWNER, operator="Owner")
        self.assertFalse(r["ok"])
        self.assertIn("命令控制台", self._actions(store))

    def test_admin_refusal_is_logged_with_role(self):
        store, cb = self._bridge_with_store()
        console.set_console(True)                    # 只开总开关，不授权管理员
        r = cb.execute("ollama run m", mode="ollama", role=config.ROLE_ADMIN,
                       operator="admin")
        self.assertFalse(r["ok"])
        row = [x for x in store.audit_rows(limit=50) if "命令控制台" in x["action"]]
        self.assertTrue(row)
        self.assertIn("管理员", row[0]["detail"])

    def test_audit_chain_still_valid_after_logging(self):
        store, cb = self._bridge_with_store()
        cb.execute("echo A", role=config.ROLE_OWNER, operator="Owner")
        cb.execute("echo B", role=config.ROLE_PATROL, operator="patrol1")
        ok, msg, n = store.verify_chain()
        self.assertTrue(ok, msg)
        self.assertGreaterEqual(n, 2)


# ==================== 桥接（需要 01 目录与子进程） ====================
class TestCmdBridge(ConsoleBase):
    def setUp(self):
        ConsoleBase.setUp(self)
        self.bridge = bridge.CmdBridge(store=storage.Store(
            db_path=os.path.join(_TMP, "cb_%s.db" % uuid.uuid4().hex[:8])))
        available, why = self.bridge.available()
        if not available:
            self.skipTest("03->01 命令控制台桥接不可用：%s" % why)

    def run_raw(self, code, mode="cmd", role="owner"):
        """
        直接跑桥接脚本，绕过 03 端的快速拦截，用来验证 **01 自己那一层**判定也在生效。
        """
        return run_bridge(config.CMD_CONSOLE_DIR, code, mode=mode, role=role)

    def test_owner_runs_harmless_command_and_gets_tier_full(self):
        console.set_console(True)
        r = self.bridge.execute("echo CMD_BRIDGE_OK", mode="cmd",
                                role=config.ROLE_OWNER, operator="Owner", timeout=60)
        self.assertTrue(r["ok"], r.get("reason") or r.get("stderr"))
        self.assertEqual(r["tier"], "full")
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("CMD_BRIDGE_OK", r["stdout"])

    def test_bridge_reports_bad_directory_honestly(self):
        console.set_console(True)          # 权限这一关先过，才能看到"目录不可用"的真实原因
        cb = bridge.CmdBridge(cmd_dir=os.path.join(_TMP, "no_such_01_dir"))
        self.assertFalse(cb.available()[0])
        r = cb.execute("echo x", role=config.ROLE_OWNER)
        self.assertFalse(r["ok"])
        self.assertIn("01", r["reason"])

    def test_master_switch_off_blocks_before_spawning(self):
        console.set_console(False)
        r = self.bridge.execute("echo NOPE", mode="cmd", role=config.ROLE_OWNER)
        self.assertFalse(r["ok"])
        self.assertEqual(r["tier"], "none")
        self.assertIsNone(r["exit_code"])
        self.assertTrue(r["blocked"])
        self.assertIn("无权限", r["stderr"])

    def test_non_owner_roles_are_blocked_by_01_itself(self):
        """
        绕过 03 端拦截直接调桥接：01 必须自己拒掉非所有者。
        同时用一个会产生副作用的命令（写文件）证明"没有执行"，而不只是"返回了 ok=false"。
        """
        console.set_console(True)
        console.set_console_admin(True)
        marker = os.path.join(str(config.DATA_ROOT), "should_not_exist_%s.txt"
                              % uuid.uuid4().hex[:6])
        for role in (config.ROLE_PATROL, config.ROLE_INVIGILATOR, config.ROLE_COMMANDER,
                     config.ROLE_CHECKER, config.ROLE_VIEWER):
            r = self.run_raw("echo X > %s" % marker, role=role)
            self.assertFalse(r["ok"], role)
            self.assertEqual(r["tier"], "none", role)
            self.assertIsNone(r["exit_code"], role)
            self.assertFalse(os.path.exists(marker), "%s 竟然执行了命令" % role)

    def test_admin_ollama_whitelist_is_not_relaxed(self):
        """管理员那一级只认 `ollama run ...`：其它写法在 01 里被拒，且没有执行"""
        console.set_console(True)
        console.set_console_admin(True)
        for code in ("ollama rm llama3",            # 别的 ollama 子命令
                     "cmd /c echo HACKED",          # 换个程序
                     "dir"):                        # 普通系统命令
            r = self.bridge.execute(code, mode="ollama", role=config.ROLE_ADMIN,
                                    operator="admin")
            self.assertFalse(r["ok"], code)
            self.assertEqual(r["tier"], "ollama", code)      # 层级在，但命令不合法
            self.assertIsNone(r["exit_code"], code)
            self.assertNotIn("HACKED", r["stdout"], code)
            self.assertIn("ollama", r["stderr"], code)

    def test_empty_command_is_not_executed(self):
        console.set_console(True)
        r = self.bridge.execute("   ", mode="cmd", role=config.ROLE_OWNER, operator="Owner")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["exit_code"])

    def test_owner_python_mode_runs_code(self):
        console.set_console(True)
        r = self.bridge.execute("print('PY_OK_%d' % (1 + 1))", mode="python",
                                role=config.ROLE_OWNER, operator="Owner", timeout=60)
        self.assertTrue(r["ok"], r.get("reason") or r.get("stderr"))
        self.assertEqual(r["tier"], "full")
        self.assertIn("PY_OK_2", r["stdout"])

    def test_01_directory_is_not_touched(self):
        """
        03 调 01 的桥接不能改 01 目录：EXAM_HOME 已把 01 的数据根指到本端，
        桥接子进程还关了字节码缓存（否则 import 01 的模块会往 01\\__pycache__ 写 .pyc）。
        比对"文件名 + 修改时间 + 大小"，只看 01 目录的第一层与它的 __pycache__，
        避免遍历 01 的资源/data（可能很大，也可能正被别处的 01 进程改动）。
        """
        base = config.CMD_CONSOLE_DIR

        def snapshot():
            out = {}
            for d in (base, os.path.join(base, "__pycache__")):
                if not os.path.isdir(d):
                    continue
                for name in sorted(os.listdir(d)):
                    p = os.path.join(d, name)
                    if os.path.isfile(p):
                        st = os.stat(p)
                        out[os.path.relpath(p, base)] = (st.st_mtime_ns, st.st_size)
            return out

        console.set_console(True)
        before = snapshot()
        self.bridge.execute("echo TOUCH_CHECK", mode="cmd", role=config.ROLE_OWNER,
                            operator="Owner")
        self.assertEqual(snapshot(), before, "01 目录被写入了新文件或被改动")


class TestCmdBridgeIsolation(ConsoleBase):
    """
    不依赖真实 01 目录：用临时目录造一个"最小 01"，验证桥接的**只读性**与异常回传。

    为什么要造假的 01：这两件事（不写 01 目录、01 抛异常时如实回传）是桥接自己的职责，
    不该只在"本机正好装好 01"时才被验证；而且造出来的 01 可以稳定地抛出异常。
    """

    _FAKE_CMD = '''# -*- coding: utf-8 -*-
FAKE = True


def execute(code_text, operator="", role="", mode="cmd", source="gui", timeout=60, cwd=None):
    import helper                      # 真实 import 一次子模块，用来验证不写 .pyc
    if code_text == "BOOM":
        raise RuntimeError("模拟 01 内部异常")
    return {"ok": True, "exit_code": 0,
            "stdout": "fake:%s:%s:%s" % (role, mode, helper.VALUE),
            "stderr": "", "duration_ms": 1, "truncated": False, "timed_out": False,
            "cwd": cwd or "", "mode": mode,
            "tier": "full" if role == "owner" else "none"}
'''

    _FAKE_ACCOUNTS = '''# -*- coding: utf-8 -*-


def cmd_console_tier(role):
    return "full" if role == "owner" else "none"
'''

    def _fake_01(self):
        d = os.path.join(_TMP, "fake01_%s" % uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "helper.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 41\n")
        with open(os.path.join(d, "cmd_console.py"), "w", encoding="utf-8") as f:
            f.write(self._FAKE_CMD)
        with open(os.path.join(d, "accounts.py"), "w", encoding="utf-8") as f:
            f.write(self._FAKE_ACCOUNTS)
        return d

    def test_bridge_does_not_write_bytecode_into_01_dir(self):
        """
        import 01 的模块不能顺手往 01\\__pycache__ 写 .pyc：调用 01 必须是只读的。
        这里故意摘掉 PYTHONDONTWRITEBYTECODE，只验证 _cmd_bridge.py 自己的那道保险
        （手工执行桥接脚本时不会有那个环境变量）。
        """
        d = self._fake_01()
        console.set_console(True)
        r = run_bridge(d, "hello", role=config.ROLE_OWNER, drop_bytecode_env=True)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["tier"], "full")
        self.assertIn("fake:owner:cmd:41", r["stdout"])
        self.assertFalse(os.path.exists(os.path.join(d, "__pycache__")),
                         "桥接往 01 目录写了字节码缓存")

    def test_cmd_bridge_also_passes_bytecode_env(self):
        """03 端的 CmdBridge 走的是环境变量那条保险，两条都要在（互为兜底）"""
        d = self._fake_01()
        console.set_console(True)
        r = bridge.CmdBridge(cmd_dir=d).execute("hello", mode="cmd",
                                                role=config.ROLE_OWNER)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertFalse(os.path.exists(os.path.join(d, "__pycache__")))

    def test_bridge_reports_01_exception_honestly(self):
        """01 内部抛异常时要如实回传 ok=false + 异常文本，不能伪装成功"""
        d = self._fake_01()
        console.set_console(True)
        r = bridge.CmdBridge(cmd_dir=d).execute("BOOM", mode="cmd",
                                                role=config.ROLE_OWNER)
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["stderr"])
        self.assertIn("模拟 01 内部异常", r["stderr"])

    def test_01_verdict_is_reported_not_overridden(self):
        """role 原样传给 01，01 的判定结果原样带回：03 端的拦截不会覆盖 01 的结论"""
        d = self._fake_01()
        console.set_console(True)
        console.set_console_admin(True)
        cb = bridge.CmdBridge(cmd_dir=d)
        self.assertEqual(cb.execute("x", role=config.ROLE_OWNER)["tier"], "full")
        # 03 端认为管理员是 ollama，但（假）01 说 none —— 结果以 01 为准
        self.assertEqual(console.console_tier(config.ROLE_ADMIN), "ollama")
        self.assertEqual(cb.execute("x", role=config.ROLE_ADMIN)["tier"], "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)
