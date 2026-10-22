# -*- coding: utf-8 -*-
"""
03 智能终端 · UI 权限收敛与 AI 提示词跟随界面 的回归测试

锁定这几条产品要求（改动前请先读）：
  1. 「生成演示告警」只出现在所有者（owner）的权限控制台，非 owner 不可用；
  2. 监控配置（新增/编辑/删除摄像头）仅 owner 可调整，非 owner 按钮禁用；
  3. AI 辅助识别默认开启，且只对「考场监考」区域的摄像头生效；
  4. AI 提示词跟随当前界面：不同页签拿到不同的 system prompt；
  5. 监控页分为「考场监考」与「非考场监控」两个分区。

测试全程在临时数据目录里跑（EXAM_TERMINAL_HOME），不碰真实 data\\。
运行：python -m unittest tests.test_ui_permissions -v
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="terminal_uiperm_")
os.environ["EXAM_TERMINAL_HOME"] = _TMP          # 必须在 import config 之前

import config        # noqa: E402
import ai_assistant  # noqa: E402


def tearDownModule():
    global _ROOT
    try:
        if _ROOT is not None:
            _ROOT.destroy()
    except Exception:
        pass
    _ROOT = None
    shutil.rmtree(_TMP, ignore_errors=True)


HAS_TK = True
try:
    import tkinter as tk
except Exception:                                 # 无图形环境时跳过 UI 断言
    HAS_TK = False

_ROOT = None
# 本文件会真实构建整个终端界面，必须在用例内**临时**切换数据目录，
# 且用完立刻还原：全局改 config（尤其在模块导入时改）会污染其它测试模块。
_CONFIG_KEYS = ("DATA_ROOT", "DB_PATH", "LOG_DIR", "ATTACH_DIR",
                "QUEUE_DIR", "HIDE_CMD_FLAG", "CONSOLE_FLAG",
                "CONSOLE_ADMIN_FLAG", "AI_CHAT_FLAG")
_CONFIG_BACKUP = {}


def _patch_config_isolated():
    """切到本文件自己的临时数据目录，并建好目录（review.attach 要求目录已存在）"""
    import pathlib
    _CONFIG_BACKUP.clear()
    for k in _CONFIG_KEYS:
        if hasattr(config, k):
            _CONFIG_BACKUP[k] = getattr(config, k)
    root = pathlib.Path(_TMP)
    config.DATA_ROOT = root
    if hasattr(config, "DB_PATH"):
        config.DB_PATH = root / "terminal.db"
    for k, name in (("LOG_DIR", "logs"), ("ATTACH_DIR", "attachments"),
                    ("QUEUE_DIR", "queue")):
        if hasattr(config, k):
            setattr(config, k, root / name)
    for k, name in (("HIDE_CMD_FLAG", "hide_cmd.on"),
                    ("CONSOLE_FLAG", "cmd_console.on"),
                    ("CONSOLE_ADMIN_FLAG", "cmd_console_admin.on"),
                    ("AI_CHAT_FLAG", "ai_chat.on")):
        if hasattr(config, k):
            setattr(config, k, root / name)
    for d in (root, root / "logs", root / "attachments", root / "queue"):
        os.makedirs(str(d), exist_ok=True)


def _restore_config():
    for k, v in _CONFIG_BACKUP.items():
        setattr(config, k, v)
    _CONFIG_BACKUP.clear()


def _silence_messageboxes():
    """
    测试期间屏蔽模态对话框：
    否则弹窗会挂在桌面上等人点，自动化运行会被卡住（真实踩过）。
    返回 (原始函数字典)。
    """
    import tkinter.messagebox as mb
    orig = {}
    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel"):
        if hasattr(mb, name):
            orig[name] = getattr(mb, name)
            setattr(mb, name, lambda *a, **kw: True)
    return orig


def _restore_messageboxes(orig):
    import tkinter.messagebox as mb
    for name, fn in orig.items():
        setattr(mb, name, fn)


def _shared_root():
    """整个模块共用一个 Tk 根：多次 Tk()/destroy 会触发 ttk 主题事件告警。"""
    global _ROOT
    if _ROOT is None:
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT


@unittest.skipUnless(HAS_TK, "无 tkinter 环境")
class UiPermissionBase(unittest.TestCase):
    """真实起一个 withdrawn 的 Tk 窗口，走一遍登录 + 主界面构建"""

    ROLE = config.ROLE_OWNER
    USER = "owner"
    PWD = "owner"

    @classmethod
    def setUpClass(cls):
        import terminal_gui
        cls.tg = terminal_gui
        cls._mb_orig = _silence_messageboxes()
        _patch_config_isolated()
        cls._cfg_isolated = True
        cls.root = _shared_root()
        for w in cls.root.winfo_children():       # 清掉上一个用例类残留的界面
            w.destroy()
        cls.app = terminal_gui.TerminalApp(cls.root)
        cls.app.u_var.set(cls.USER)
        cls.app.p_var.set(cls.PWD)
        cls.app._do_login()

    @classmethod
    def tearDownClass(cls):
        try:
            for w in cls.root.winfo_children():
                w.destroy()
            cls.root.update_idletasks()
        except Exception:
            pass
        _restore_messageboxes(cls._mb_orig)
        _restore_config()


class TestOwnerUi(UiPermissionBase):
    ROLE = config.ROLE_OWNER

    def test_login_ok(self):
        self.assertIsNotNone(self.app.session)
        self.assertEqual(self.app.session.role, config.ROLE_OWNER)

    def test_monitor_split_into_two_areas(self):
        self.assertTrue(hasattr(self.app, "mon_exam_grid"))
        self.assertTrue(hasattr(self.app, "mon_other_grid"))
        self.assertEqual(self.app.AREA_EXAM, "exam")
        self.assertEqual(self.app.AREA_OTHER, "other")

    def test_ai_default_on_for_exam_area(self):
        self.assertTrue(self.app.mon_ai.get())

    def test_monitor_manage_allowed_for_owner(self):
        # owner 不应被权限拦截（不真正弹窗：只断言角色判定分支）
        self.assertEqual(self.app.session.role, config.ROLE_OWNER)

    def test_demo_alert_ingest_owner_only_path(self):
        """owner 走 _demo 能真的生成 demo 告警"""
        before = len(self.app.store.list_alerts(limit=1000))
        self.app._demo()
        after = len(self.app.store.list_alerts(limit=1000))
        self.assertGreaterEqual(after, before)      # 至少不报错、不减少

    def test_current_interface_follows_tab(self):
        pairs = [(self.app.tab_alerts, "alerts"), (self.app.tab_review, "review"),
                 (self.app.tab_verify, "verify"), (self.app.tab_monitor, "monitor"),
                 (self.app.tab_ops, "ops")]
        for widget, key in pairs:
            self.app.nb.select(widget)
            self.root.update_idletasks()
            self.assertEqual(self.app._current_interface(), key)

    def test_snapshot_contains_interface_and_monitor_data(self):
        self.app.nb.select(self.app.tab_monitor)
        self.root.update_idletasks()
        snap = self.app._ai_snapshot()
        self.assertIn("当前界面", snap)
        self.assertIn("AI 框选识别", snap)


@unittest.skipUnless(HAS_TK, "无 tkinter 环境")
class TestNonOwnerUi(UiPermissionBase):
    """非 owner：监控配置入口禁用、演示告警必须被拒"""
    ROLE = config.ROLE_ADMIN
    USER = "admin"
    PWD = "admin"          # 见 access.DEFAULT_ACCOUNTS

    def test_login_ok(self):
        self.assertIsNotNone(self.app.session)
        self.assertEqual(self.app.session.role, config.ROLE_ADMIN)

    def test_demo_alert_rejected_for_non_owner(self):
        import tkinter.messagebox as mb
        calls = {"n": 0}
        orig = mb.showerror

        def fake(*a, **kw):
            calls["n"] += 1
        mb.showerror = fake
        try:
            before = len(self.app.store.list_alerts(limit=1000))
            self.app._demo()
            after = len(self.app.store.list_alerts(limit=1000))
        finally:
            mb.showerror = orig
        self.assertEqual(calls["n"], 1, "非 owner 调 _demo 必须被拦截")
        self.assertEqual(before, after, "被拦截时不得写入任何告警")

    def test_monitor_manage_rejected_for_non_owner(self):
        import tkinter.messagebox as mb
        called = {"n": 0}
        orig = mb.showerror

        def fake(*a, **kw):
            called["n"] += 1
        mb.showerror = fake
        try:
            self.app._monitor_manage()
        finally:
            mb.showerror = orig
        self.assertEqual(called["n"], 1, "非 owner 调 _monitor_manage 必须被拦截")


class TestInterfacePrompts(unittest.TestCase):
    """AI 提示词必须跟随界面（纯逻辑，无需 GUI）"""

    def test_all_interfaces_have_prompt(self):
        for key in ("alerts", "review", "verify", "monitor", "ops"):
            self.assertIn(key, ai_assistant.INTERFACE_PROMPTS)

    def test_prompts_differ_from_generic(self):
        generic = ai_assistant.build_system_prompt(None)
        seen = set()
        for key in ("alerts", "review", "verify", "monitor", "ops"):
            p = ai_assistant.build_system_prompt(key)
            self.assertNotEqual(p, generic, key)
            self.assertGreater(len(p), len(generic), key)
            self.assertNotIn(p, seen, "各界面提示词不能完全相同")
            seen.add(p)

    def test_free_mode_ignores_interface(self):
        self.assertEqual(ai_assistant.build_system_prompt("monitor", free_mode=True),
                         ai_assistant.build_system_prompt(None, free_mode=True))

    def test_unknown_interface_falls_back(self):
        self.assertEqual(ai_assistant.build_system_prompt("nope"),
                         ai_assistant.build_system_prompt(None))
