# -*- coding: utf-8 -*-
"""
命令控制台单元测试（cmd_console / accounts 开关 / ollama 白名单）。

这个功能是"有意开放的后门"，所以测试重点不是"能不能跑命令"，而是**安全不变量**：
    1. 默认关闭 —— 全新安装不得自带可执行入口；
    2. 分级正确 —— 所有者 full；管理员**只有** ollama；其余角色 none；
    3. 白名单不可绕 —— 管理员的任何输入都不可能变成"执行别的程序"；
    4. 执行留痕 —— 每次执行（含被拒绝的尝试）都写入审计哈希链；
    5. 有界执行 —— 超时必须杀掉进程树，输出量必须有上限。

运行：
    python -m unittest discover -s tests -v
    （或）python tests/test_cmd_console.py
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                            # noqa: E402
import accounts                                          # noqa: E402
import cmd_console                                       # noqa: E402
from audit import AuditLogger                            # noqa: E402
from models import RC_CMD_EXEC                           # noqa: E402

OWNER = accounts.ROLE_OWNER
ADMIN = accounts.ROLE_ADMIN
TEACHER = accounts.ROLE_TEACHER

# 测试用的临时目录：审计链与操作日志都写到这里，不碰真实 data\audit.log
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")


class _Base(unittest.TestCase):
    """每个用例都自己收尾，保证开关不会因为测试失败而留在开启状态"""

    def setUp(self):
        self._orig_main = accounts.cmd_console_enabled()
        self._orig_admin = accounts.cmd_console_admin_allowed()
        # 审计链重定向到临时文件：真实 data\audit.log 是"不可篡改的审计链"，
        # 往里灌测试产生的 echo 记录会削弱它的可信度，也会污染使用者的操作记录。
        self._orig_audit = config.AUDIT_LOG_PATH
        self._orig_action = config.ACTION_LOG_PATH
        os.makedirs(_TMP, exist_ok=True)
        config.AUDIT_LOG_PATH = os.path.join(_TMP, "cmd_test_audit.log")
        config.ACTION_LOG_PATH = os.path.join(_TMP, "cmd_test_action.log")

        accounts.set_cmd_console(False)
        accounts.set_cmd_console_admin(False)

    def tearDown(self):
        config.AUDIT_LOG_PATH = self._orig_audit
        config.ACTION_LOG_PATH = self._orig_action
        accounts.set_cmd_console(self._orig_main)
        accounts.set_cmd_console_admin(self._orig_admin)


class TierTestCase(_Base):
    """安全不变量 1 / 2：默认关闭 + 分级正确"""

    def test_default_off_for_everyone(self):
        self.assertFalse(accounts.cmd_console_enabled())
        for role in (OWNER, ADMIN, TEACHER, None, ""):
            with self.subTest(role=role):
                self.assertEqual(accounts.cmd_console_tier(role), accounts.CMD_TIER_NONE)
                self.assertFalse(accounts.can_cmd_console(role))

    def test_owner_gets_full(self):
        accounts.set_cmd_console(True)
        self.assertEqual(accounts.cmd_console_tier(OWNER), accounts.CMD_TIER_FULL)
        st = cmd_console.status(OWNER)
        self.assertTrue(st["can_run"])
        self.assertTrue(st["can_full"])
        self.assertEqual(sorted(st["modes"]), ["cmd", "python"])

    def test_admin_gets_ollama_only_when_delegated(self):
        accounts.set_cmd_console(True)
        # 未授权：管理员什么都不行
        self.assertEqual(accounts.cmd_console_tier(ADMIN), accounts.CMD_TIER_NONE)
        # 授权后：仅 ollama
        accounts.set_cmd_console_admin(True)
        self.assertEqual(accounts.cmd_console_tier(ADMIN), accounts.CMD_TIER_OLLAMA)
        st = cmd_console.status(ADMIN)
        self.assertTrue(st["can_run"])
        self.assertFalse(st["can_full"], "管理员永远不能拿到完整权限")
        self.assertEqual(st["modes"], ["ollama"])

    def test_master_switch_gates_admin_too(self):
        """关掉总开关 = 全部停用，管理员的 AI 辅助也不例外"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        self.assertEqual(accounts.cmd_console_tier(ADMIN), accounts.CMD_TIER_OLLAMA)
        accounts.set_cmd_console(False)
        self.assertEqual(accounts.cmd_console_tier(ADMIN), accounts.CMD_TIER_NONE)
        # 再打开总开关，授权状态仍在
        accounts.set_cmd_console(True)
        self.assertEqual(accounts.cmd_console_tier(ADMIN), accounts.CMD_TIER_OLLAMA)

    def test_other_roles_never_allowed(self):
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        for role in (TEACHER, "patrol", "viewer", None, "", "owner "):
            with self.subTest(role=role):
                self.assertEqual(accounts.cmd_console_tier(role), accounts.CMD_TIER_NONE)

    def test_execute_fails_closed_without_role(self):
        """role 必填：拿不到角色时必须拒绝，而不是默认给 full"""
        accounts.set_cmd_console(True)
        for role in (None, "", "unknown"):
            with self.subTest(role=role):
                r = cmd_console.execute("echo should_not_run", operator="x", role=role)
                self.assertFalse(r["ok"])
                self.assertNotIn("should_not_run", r.get("stdout") or "")


class OllamaWhitelistTestCase(_Base):
    """安全不变量 3：白名单不可绕 —— 这是两级权限之间唯一的防线"""

    ALLOWED = [
        "ollama run llama3",
        "ollama run llama3 你好，请判断这次核验是否可疑",
        "ollama run llama3.2:1b summarize",
        "ollama run qwen2.5:7b-instruct 帮我分析",
        "   ollama run llama3   前后有空白   ",
    ]

    REJECTED = [
        "",
        "   ",
        "calc",
        "dir /s",
        "echo hello",
        "ollama",                          # 没有子命令
        "ollama run",                      # 没有模型名
        "ollama pull llama3",              # 其它子命令
        "ollama rm llama3",
        "ollama serve",
        "ollama push llama3",
        "echo ollama run llama3",          # 不是以 ollama 开头
        "/usr/bin/ollama run llama3",
        "ollama run --help",               # 模型名不得以 - 开头（防注入参数）
        "ollama run -x",
        "ollama run ../../evil",           # 不得是路径
        r"ollama run C:\Windows\System32\cmd.exe",   # 反斜杠不在允许字符集内
        "sudo ollama run llama3",
    ]

    def test_allowed_forms(self):
        for text in self.ALLOWED:
            with self.subTest(text=text):
                ok, argv, msg = cmd_console.parse_ollama_command(text)
                self.assertTrue(ok, "应被允许但被拒：%r（%s）" % (text, msg))
                self.assertEqual(argv[0], "ollama")
                self.assertEqual(argv[1], "run")

    def test_rejected_forms(self):
        for text in self.REJECTED:
            with self.subTest(text=text):
                ok, argv, msg = cmd_console.parse_ollama_command(text)
                self.assertFalse(ok, "应被拒绝但被放行：%r -> %r" % (text, argv))
                self.assertIsNone(argv)

    def test_shell_metacharacters_become_inert_prompt_text(self):
        """
        核心安全性证明：模型名之后的**任何内容**都只是提示词，必须作为
        **单个 argv 元素**交给 ollama，而不是被 shell 解释切分。

        注意这里**不**把 `;` `&&` `|` 换行等判为"非法"——那会退化成黑名单过滤，
        既拦不干净又会误伤正常提问（比如"判断 a&&b 是否成立"）。
        真正的保证是：它们在 argv 里**逃不出提示词这一个参数**。
        若哪天有人把 run_ollama 改成 shell=True，或对命令调用 split()，
        下面这些断言会立刻失败——所以它们是防回归的关键。
        """
        cases = [
            ("ollama run llama3 ; calc && whoami | more > out.txt",
             "; calc && whoami | more > out.txt"),
            ("ollama run llama3 extra\ncalc -bad", "extra\ncalc -bad"),
            ("ollama run llama3 `id` $(whoami) %PATH%", "`id` $(whoami) %PATH%"),
        ]
        for text, expected_prompt in cases:
            with self.subTest(text=text):
                ok, argv, _ = cmd_console.parse_ollama_command(text)
                self.assertTrue(ok)
                self.assertEqual(len(argv), 4,
                                 "元字符必须整体落在同一个参数里：%r" % (argv,))
                self.assertEqual(argv[2], "llama3")
                self.assertEqual(argv[3], expected_prompt)
                # 参数个数固定为 3（无提示词）或 4（有提示词），
                # 不存在"多切出一条命令"的可能
                self.assertIn(len(argv), (3, 4))

    def test_model_name_charset_is_restricted(self):
        for bad in ("llama3;", "llama3|", "llama3&&", "llama3$x", "llama3`id`",
                    "llama3%PATH%", 'llama3"', "llama3'", "llama3(x)"):
            with self.subTest(model=bad):
                # 这些字符被挡在模型名之外；若后接内容只能作为**提示词**整体存在
                ok, argv, _ = cmd_console.parse_ollama_command("ollama run " + bad)
                if ok:
                    self.assertNotIn(bad, argv[2:3], "模型名不得含特殊字符")

    def test_admin_cannot_execute_arbitrary_command(self):
        """管理员即使被授权，`calc` 这类命令也必须在执行前被拦下"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        for text in ("calc", "dir", "ollama pull x", "whoami"):
            with self.subTest(text=text):
                r = cmd_console.execute(text, operator="admin", role=ADMIN, mode="cmd")
                self.assertFalse(r["ok"])
                self.assertEqual(r["mode"], "ollama-rejected")
                self.assertIsNone(r["exit_code"], "被拒绝的命令根本不应执行")

    def test_admin_mode_is_forced_to_ollama(self):
        """管理员传 mode=python 也没用：层级决定模式，不由请求方指定"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        r = cmd_console.execute("print(1)", operator="admin", role=ADMIN, mode="python")
        self.assertEqual(r["mode"], "ollama-rejected")
        self.assertFalse(r["ok"])


class ExecutionTestCase(_Base):
    """安全不变量 4 / 5：留痕 + 有界执行"""

    def setUp(self):
        super().setUp()
        accounts.set_cmd_console(True)

    # ---- 基本执行 ----
    def test_shell_echo(self):
        r = cmd_console.run_shell("echo cmd_console_probe")
        self.assertTrue(r["ok"])
        self.assertIn("cmd_console_probe", r["stdout"])

    def test_nonzero_exit(self):
        r = cmd_console.run_shell("exit /b 7")
        self.assertFalse(r["ok"])
        self.assertEqual(r["exit_code"], 7)

    def test_stderr_captured(self):
        r = cmd_console.run_shell("echo to_stderr 1>&2")
        self.assertIn("to_stderr", r["stderr"])

    def test_chinese_output_is_not_mojibake(self):
        """
        回归（真实踩过的坑）：cmd 输出的是 **UTF-8 字节**。

        若解码时先试 GBK，像 `echo 正常` 这种"字节恰好也是合法 GBK"的情况会被
        **静默**解成乱码 'UI 姝ｅ父'（能解出来所以不抛异常，永远轮不到 UTF-8）。
        而且它极难发现：`echo 中文输出测试` 的字节恰好不是合法 GBK，会正常回落，
        于是表现为"有些中文正常、有些乱码"。所以解码必须 **UTF-8 优先**。
        """
        r = cmd_console.run_shell("echo UI 正常")
        self.assertIn("UI 正常", r["stdout"])
        self.assertNotIn("姝", r["stdout"], "出现了 UTF-8 被误当 GBK 的典型乱码")

    def test_gbk_bytes_still_decode(self):
        """真 GBK 字节输出要能回落到 cp936——不能因为"UTF-8 优先"反而改坏它"""
        r = cmd_console.run_python(
            'import sys; sys.stdout.buffer.write("中文GBK测试".encode("gbk"))')
        self.assertIn("中文GBK测试", r["stdout"])

    def test_utf8_bytes_decode(self):
        r = cmd_console.run_python('print("中文UTF8测试")')
        self.assertIn("中文UTF8测试", r["stdout"])

    def test_python_code(self):
        r = cmd_console.run_python("print(6 * 7)")
        self.assertTrue(r["ok"])
        self.assertIn("42", r["stdout"])

    def test_python_traceback(self):
        r = cmd_console.run_python("raise ValueError('boom')")
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["stderr"])

    # ---- 有界执行 ----
    def test_timeout_kills_process_tree(self):
        t0 = time.time()
        r = cmd_console.run_python("import time; time.sleep(60)", timeout=3)
        self.assertTrue(r["timed_out"])
        self.assertLess(time.time() - t0, 30)

    def test_output_capped(self):
        r = cmd_console.run_python("print('A' * 2000000)", timeout=60)
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["stdout"]), cmd_console.MAX_OUTPUT_BYTES + 1024)

    def test_timeout_clamped(self):
        r = cmd_console.run_python("print('ok')", timeout=999999)
        self.assertTrue(r["ok"])
        self.assertLess(r["duration_ms"], 60000)

    # ---- 留痕 ----
    def test_owner_execution_is_audited(self):
        before = AuditLogger().query()
        r = cmd_console.execute("echo audit_case_probe", operator="owner",
                                role=OWNER, mode="cmd", source="unittest")
        after = AuditLogger().query()

        self.assertGreater(len(after), len(before))
        last = after[-1]
        self.assertEqual(last.get("reason_code"), RC_CMD_EXEC)
        self.assertEqual(last.get("operator"), "owner")
        self.assertIn("audit_case_probe", last.get("reason") or "")
        self.assertIn("full/", last.get("reason") or "")       # 层级写进了审计
        self.assertEqual(after[-1].get("prev_hash"), after[-2].get("hash"))
        self.assertEqual(r["tier"], accounts.CMD_TIER_FULL)

    def test_rejected_attempt_is_also_audited(self):
        """被拒绝的尝试同样留痕：谁在什么时候试图执行了什么"""
        accounts.set_cmd_console_admin(True)
        before = len(AuditLogger().query())
        cmd_console.execute("calc", operator="teacher1", role=ADMIN, mode="cmd",
                            source="unittest")
        after = AuditLogger().query()
        self.assertGreater(len(after), before)
        last = after[-1]
        self.assertEqual(last.get("result"), "BLOCKED")
        self.assertIn("calc", last.get("reason") or "")
        self.assertIn("ollama/", last.get("reason") or "")

    def test_refused_without_permission_is_audited(self):
        accounts.set_cmd_console(False)
        cmd_console.execute("echo nope", operator="x", role=TEACHER, source="unittest")
        last = AuditLogger().query()[-1]
        self.assertEqual(last.get("result"), "BLOCKED")
        self.assertIn("none/", last.get("reason") or "")
        accounts.set_cmd_console(True)

    def test_audit_reason_sanitized(self):
        """命令里含身份证号时，链上必须是掩码——不得成为绕过脱敏的后门"""
        id_no = "110101199003071234"
        cmd_console.execute("echo %s" % id_no, operator="owner", role=OWNER,
                            mode="cmd", source="unittest")
        reason = AuditLogger().query()[-1].get("reason") or ""
        self.assertNotIn(id_no, reason)

    def test_audit_survives_huge_output(self):
        cmd_console.execute("print('B' * 500000)", operator="owner", role=OWNER,
                            mode="python", source="unittest", timeout=60)
        reason = AuditLogger().query()[-1].get("reason") or ""
        self.assertLess(len(reason), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
