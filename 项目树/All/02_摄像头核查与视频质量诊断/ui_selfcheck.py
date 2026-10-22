# -*- coding: utf-8 -*-
"""
02 摄像头核查与视频质量诊断 · UI 启动自检

不进入 mainloop，直接走一遍真实启动流程：
  建窗口 -> 登录(owner) -> 建主界面全部页签 -> 刷新各页数据 -> 校验关键控件
结果写入 data/logs/ui_selfcheck.txt，并打印到控制台。

用法：python ui_selfcheck.py        # 退出码 0 = 自检通过
"""
import io
import os
import sys
import traceback
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

REPORT = []


def line(msg, ok=None):
    tag = "[OK]  " if ok is True else ("[FAIL]" if ok is False else "      ")
    REPORT.append("%s %s" % (tag, msg))
    print(REPORT[-1])
    sys.stdout.flush()


def main():
    import tkinter as tk

    line("02 UI 启动自检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    line("python: %s" % sys.executable)

    # ---- 依赖检查 ----
    try:
        import cv2
        line("opencv-python %s" % cv2.__version__, True)
        line("HOGDescriptor（AI 越界检测必要条件）",
             hasattr(cv2, "HOGDescriptor")
             and hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector"))
    except Exception as e:
        line("opencv 导入失败：%s" % e, False)

    # ---- 多模态大模型（VLM）----
    try:
        import gui as _gui                     # 本段在 main() 里早于下面的 import gui，这里补一次
        import vlm_detect as vlm_mod
        line("vlm_detect 模块导入", True)
        line("多模态 Tracker 选择（off→原版 / ollama→多模态）",
             vlm_mod.tracker_class_for("off") is None
             and vlm_mod.tracker_class_for("ollama") is vlm_mod.VlmPersonTracker)
        line("参数表：开多模态才带 vlm_* 键",
             "vlm_provider" in vlm_mod.accepted_params(_gui.pd_mod.PersonTracker, "ollama")
             and "vlm_provider" not in vlm_mod.accepted_params(_gui.pd_mod.PersonTracker, "off"))
        line("设置默认键齐全",
             all(k in _gui.AI_SETTINGS_DEFAULT for k in vlm_mod.SETTING_KEYS))
    except Exception as e:
        line("多模态模块检查失败：%s" % str(e)[:80], False)

    try:
        import PIL
        line("Pillow %s" % PIL.__version__, True)
    except Exception as e:
        line("Pillow 导入失败：%s" % e, False)

    try:
        import numpy
        line("numpy %s" % numpy.__version__, True)
    except Exception as e:
        line("numpy 导入失败：%s" % e, False)

    # ---- 模块与类结构 ----
    import gui
    line("gui 模块导入", True)
    line("App 类存在", hasattr(gui, "App"))
    for name in ("_build_login", "_do_login", "_build", "_mon_tab", "refresh_mon",
                 "_cam_play", "_cam_update", "_cam_stop", "_cam_enlarge",
                 "refresh_devices", "refresh_checks", "refresh_orders",
                 "refresh_report", "refresh_all"):
        line("App.%s 方法存在" % name, hasattr(gui.App, name))
    line("_BehaviorTracker 在模块级（不在 App 内部）",
         hasattr(gui, "_BehaviorTracker")
         and not hasattr(gui.App, "_BehaviorTracker"))

    # ---- 真实走一遍启动流程 ----
    root = tk.Tk()
    root.withdraw()                      # 自检不弹窗
    try:
        app = gui.App(root)
        line("登录页构建", True)
    except Exception:
        line("登录页构建失败：%s" % traceback.format_exc().splitlines()[-1], False)
        raise

    app.login_user.set("owner")
    app.login_pwd.set("owner")
    app._do_login()
    line("owner 登录并切换主界面", app.user is not None)

    for attr, label in (("t_dev", "设备台账页"), ("t_chk", "巡检核查页"),
                        ("t_wo", "整改工单页"), ("t_rep", "报告与指标页"),
                        ("t_mon", "实时监控页")):
        w = getattr(app, attr, None)
        line("%s已构建" % label, bool(w is not None and w.winfo_exists()))

    app.refresh_all()
    line("refresh_all 全量刷新", True)

    # 监控页关键对象
    line("_mon_panels 面板字典", isinstance(getattr(app, "_mon_panels", None), dict))
    line("_behavior_tracker 行为计数器",
         isinstance(getattr(app, "_behavior_tracker", None), gui._BehaviorTracker))
    line("AI 检测开关控件", hasattr(app, "mon_ai_chk"))
    line("隐藏命令行窗口勾选框", hasattr(app, "hide_cmd"))

    # 多模态：AI 设置弹窗能真实构建（含新增的"多模态大模型"一栏）
    try:
        app._open_ai_settings()
        root.update()
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        line("AI 设置弹窗构建（含多模态一栏）", bool(tops))
        if tops:
            texts = []

            def _collect(w):
                for c in w.winfo_children():
                    try:
                        texts.append(str(c.cget("text")))
                    except Exception:
                        pass
                    _collect(c)

            _collect(tops[-1])
            joined = " ".join(texts)
            line("弹窗含『多模态大模型』", "多模态大模型" in joined)
            line("弹窗含『测试连接』按钮", "测试连接" in joined)
            line("弹窗含『API Key』输入框", "API Key" in joined)
            tops[-1].destroy()
    except Exception as e:
        line("AI 设置弹窗构建失败：%s" % str(e)[:80], False)

    # 台账操作链路（不真的保存：只校验方法可调用性）
    line("设备清单条数：%d" % len(app.store.list_devices()))
    line("账户清单条数：%d" % len(app.accounts.list_accounts()))

    root.destroy()

    fails = sum(1 for r in REPORT if r.startswith("[FAIL]"))
    line("=" * 46)
    line("自检结果：%s（FAIL 项 %d）" % ("通过" if fails == 0 else "未通过", fails),
         fails == 0)

    log_dir = os.path.join(str(__import__("config").LOG_DIR))
    os.makedirs(log_dir, exist_ok=True)
    out = os.path.join(log_dir, "ui_selfcheck.txt")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT) + "\n")
    print("报告已写入：%s" % out)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
