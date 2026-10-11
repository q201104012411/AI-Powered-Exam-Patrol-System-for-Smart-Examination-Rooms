# -*- coding: utf-8 -*-
"""
GUI 命令控制台测试：入口位置、开关联动、层级自适应。

验证要点：
    1. 入口按钮建在「现场视频」区（row=0），不在「更多操作」里；
    2. 默认关闭时不显示；开启后显示；
    3. 管理员只有在被授权后才看到入口，且窗口里**只能**选 ollama（选项被禁用）；
    4. 关闭窗口后再同步按钮状态不得抛 TclError（控件已随窗口销毁）。

没有图形环境时整体跳过（例如纯命令行 CI），不会让测试套件变红。

运行：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts                                          # noqa: E402

try:
    import tkinter as tk
    _root_probe = tk.Tk()
    _root_probe.withdraw()
    _root_probe.destroy()
    HAS_TK = True
except Exception:                                        # 无显示环境
    HAS_TK = False


@unittest.skipUnless(HAS_TK, "当前环境没有可用的图形界面（Tk）")
class CmdConsoleGuiTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import gui
        cls.gui_mod = gui

    def setUp(self):
        self._orig_main = accounts.cmd_console_enabled()
        self._orig_admin = accounts.cmd_console_admin_allowed()
        accounts.set_cmd_console(False)
        accounts.set_cmd_console_admin(False)

        self.root = tk.Tk()
        self.root.withdraw()                 # 不显示主窗口，避免测试时打扰
        self.g = self.gui_mod.FaceVerifyGUI(self.root)
        self.g.user = {"username": "owner", "role": accounts.ROLE_OWNER,
                       "display_name": "系统所有者"}
        self.g._build_ui()
        self.root.update_idletasks()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        accounts.set_cmd_console(self._orig_main)
        accounts.set_cmd_console_admin(self._orig_admin)

    # ---------------- 入口位置 ----------------
    def test_entry_exists_in_video_area(self):
        # 注意：隐藏状态用的是 grid_remove()，此时 grid_info() 为空（Tk 的正常行为），
        # 所以要先打开开关让它显示出来，再校验位置。
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()

        info = self.g.cmd_nav_btn.grid_info()
        self.assertTrue(info, "显示后入口按钮应使用 grid 布局")
        self.assertEqual(int(info.get("row", -1)), 0,
                         "入口按钮应位于现场视频区的第一行（实时视频右侧）")
        # 更强的位置证明：与「开启实时视频」按钮同属一个父容器（即现场视频区）
        self.assertIs(self.g.cmd_nav_btn.master, self.g.video_btn.master,
                      "入口按钮应与实时视频按钮在同一个容器内")
        self.assertGreater(int(info.get("column", 0)),
                           int(self.g.video_btn.grid_info().get("column", 0)),
                           "入口按钮应在实时视频按钮的右侧")

    def test_entry_not_in_more_frame(self):
        """入口不应出现在「更多操作」折叠区里"""
        self.assertIsNot(self.g.cmd_nav_btn.master, self.g.more_frame)

    # ---------------- 开关联动 ----------------
    def test_hidden_by_default(self):
        self.assertEqual(self.g.cmd_nav_btn.winfo_manager(), "",
                         "默认关闭时不应显示入口")

    def test_visible_after_enabling(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.cmd_nav_btn.winfo_manager(), "grid")

    def test_hidden_again_after_disabling(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        accounts.set_cmd_console(False)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.cmd_nav_btn.winfo_manager(), "")

    def test_admin_needs_delegation_for_entry(self):
        self.g.user = {"username": "teacher1", "role": accounts.ROLE_ADMIN,
                       "display_name": "考务管理员"}
        accounts.set_cmd_console(True)

        # 只开总开关：管理员看不到
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.cmd_nav_btn.winfo_manager(), "",
                         "未授权时管理员不应看到入口")

        # 授权后：管理员可见
        accounts.set_cmd_console_admin(True)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.cmd_nav_btn.winfo_manager(), "grid")

    # ---------------- 关闭窗口后的容错 ----------------
    def test_sync_after_owner_panel_destroyed(self):
        """
        所有者设置面板建在「操作记录」Toplevel 里；窗口关掉后 cmd_btn 变成已销毁控件，
        此时再同步按钮状态**不得**抛 TclError（曾经的真 bug）。
        """
        self.g._show_records()
        self.root.update_idletasks()
        rec = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(rec, "操作记录窗口应已创建")
        self.assertTrue(hasattr(self.g, "cmd_btn"))
        rec[-1].destroy()
        self.root.update_idletasks()
        try:
            self.g._sync_cmd_btn()          # 不应抛异常
        except Exception as e:
            self.fail("关闭所有者面板后同步按钮抛了异常：%s: %s" % (type(e).__name__, e))

    # ---------------- 层级自适应 ----------------
    def test_console_window_owner_full(self):
        accounts.set_cmd_console(True)
        self.g._show_cmd_console()
        self.root.update_idletasks()
        wins = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(wins, "应创建控制台窗口")
        self.assertIn("完整权限", wins[-1].title())
        wins[-1].destroy()

    def test_console_window_admin_locked_to_ollama(self):
        self.g.user = {"username": "teacher1", "role": accounts.ROLE_ADMIN,
                       "display_name": "考务管理员"}
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        self.g._show_cmd_console()
        self.root.update_idletasks()
        wins = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(wins, "管理员应能打开控制台窗口")
        win = wins[-1]
        self.assertIn("ollama", win.title())

        # 收集窗口内所有 Radiobutton，确认只有 ollama 且被禁用
        radios = []

        def walk(w):
            for c in w.winfo_children():
                radios.append(c)
                walk(c)

        walk(win)
        rbs = [w for w in radios if w.__class__.__name__ == "Radiobutton"]
        self.assertTrue(rbs, "应有类型选择控件")
        values = [str(rb.cget("value")) for rb in rbs]
        self.assertEqual(values, ["ollama"], "管理员只应看到 ollama 一个选项")
        self.assertEqual(str(rbs[0].cget("state")), "disabled",
                         "管理员的类型选择应被锁定")
        win.destroy()

    # ---------------- 内置控制台（与视频并排，不弹窗） ----------------
    def test_inline_panel_hidden_by_default(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.console_panel.winfo_manager(), "",
                         "内置控制台默认应是收起状态（不占视频空间）")

    def test_inline_panel_beside_video(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.g._toggle_console_panel()
        self.root.update_idletasks()

        panel = self.g.console_panel
        self.assertEqual(panel.winfo_manager(), "pack", "点击按钮后应展开")
        # 关键位置断言：与视频**同一父容器**（并排），而不是独立窗口
        self.assertIs(panel.master, self.g.video_label.master,
                      "控制台应与视频在同一容器内（并排显示）")
        self.assertNotIsInstance(panel.winfo_toplevel(), tk.Toplevel,
                                 "内置控制台不应是独立窗口")

    def test_inline_panel_toggles_closed(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.g._toggle_console_panel()
        self.assertTrue(self.g.console_panel.winfo_manager())
        self.g._toggle_console_panel()
        self.root.update_idletasks()
        self.assertEqual(self.g.console_panel.winfo_manager(), "")

    def test_inline_panel_auto_hidden_when_switch_off(self):
        """所有者关掉总开关后，已展开的面板必须一起收起（不能留下越权残留）"""
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        self.g._toggle_console_panel()
        self.root.update_idletasks()
        self.assertTrue(self.g.console_panel.winfo_manager())

        accounts.set_cmd_console(False)
        self.g._sync_cmd_btn()
        self.root.update_idletasks()
        self.assertEqual(self.g.console_panel.winfo_manager(), "",
                         "总开关关闭后，已展开的面板必须一起收起")

    def test_owner_mode_box_full(self):
        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        L = self.gui_mod.CONSOLE_MODE_LABELS
        self.assertEqual(str(self.g.console_mode_box.cget("state")), "readonly")
        self.assertEqual(tuple(self.g.console_mode_box.cget("values")),
                         (L["cmd"], L["python"], L["ai"]))

    def test_owner_hint_does_not_leak_ollama_restriction(self):
        """
        回归测试（来自真实反馈）：面板建于"总开关未开"时，标题/提示会按管理员
        (ollama) 写；所有者随后开启总开关时**必须一起改写**——
        否则他会看到"仅可执行 ollama run"而以为自己的完整权限没生效。
        """
        # setUp 里开关是关闭的，此时按 ollama 配置
        self.g._sync_cmd_btn()
        self.assertIn("ollama", self.g.console_hint.cget("text"))

        accounts.set_cmd_console(True)
        self.g._sync_cmd_btn()
        hint = self.g.console_hint.cget("text")
        self.assertNotIn("仅可执行", hint, "所有者不该看到管理员的 ollama 限制提示")
        self.assertIn("任意系统命令", hint)
        self.assertIn("完整权限", self.g.console_panel.cget("text"),
                      "面板标题也要反映所有者层级")
        # 类型选择要能用 cmd / python，而不是被锁在 ollama
        self.assertEqual(str(self.g.console_mode_box.cget("state")), "readonly")
        self.assertIn(self.gui_mod.CONSOLE_MODE_LABELS["python"],
                      tuple(self.g.console_mode_box.cget("values")))

    def test_admin_mode_box_locked_to_ollama(self):
        """管理员登录时，内置控制台的类型选择只能有 ollama 且被禁用"""
        accounts.set_cmd_console(True)
        accounts.set_cmd_console_admin(True)
        root2 = tk.Tk()
        root2.withdraw()
        try:
            g2 = self.gui_mod.FaceVerifyGUI(root2)
            g2.user = {"username": "teacher1", "role": accounts.ROLE_ADMIN,
                       "display_name": "考务管理员"}
            g2._build_ui()
            root2.update_idletasks()
            L = self.gui_mod.CONSOLE_MODE_LABELS
            self.assertEqual(str(g2.console_mode_box.cget("state")), "disabled")
            self.assertEqual(tuple(g2.console_mode_box.cget("values")), (L["ollama"],))
            self.assertEqual(g2.console_mode.get(), L["ollama"])
            # 管理员这一侧才该看到 ollama 的限制提示
            self.assertIn("仅可执行", g2.console_hint.cget("text"))
            self.assertIn("AI 辅助", g2.console_panel.cget("text"))
        finally:
            root2.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
