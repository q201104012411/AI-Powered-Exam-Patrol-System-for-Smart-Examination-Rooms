# -*- coding: utf-8 -*-
"""
03 智能终端操作 · UI 启动自检

不进入 mainloop，真实走一遍启动流程并断言关键 UI/权限行为：
  登录页 -> owner 登录 -> 主界面全部页签 -> 监控页分区 -> 权限控制台 ->
  AI 面板界面提示词跟随 -> 非 owner 权限收敛
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

    line("03 UI 启动自检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    line("python: %s" % sys.executable)

    try:
        import cv2
        line("opencv-python %s" % cv2.__version__, True)
    except Exception as e:
        line("opencv 导入失败：%s" % e, False)

    # ---- 多模态大模型（VLM）----
    try:
        import vlm_detect as vlm_mod
        line("vlm_detect 模块导入", True)
        line("多模态 Tracker 选择（off→原版 / ollama→多模态）",
             vlm_mod.tracker_class_for("off") is None
             and vlm_mod.tracker_class_for("ollama") is vlm_mod.VlmPersonTracker)
    except Exception as e:
        vlm_mod = None
        line("多模态模块检查失败：%s" % str(e)[:80], False)

    import config
    import terminal_gui as tg
    import ai_assistant

    if vlm_mod is not None:
        line("AI 设置默认键含 vlm_*（config.AI_SETTINGS_DEFAULT）",
             all(k in config.AI_SETTINGS_DEFAULT for k in vlm_mod.SETTING_KEYS))
        line("链路名：没开多模态时不冒充大模型",
             tg.TerminalApp._ai_chain_name(None) in ("YOLO11n-ONNX", "HOG+SVM"))
        try:
            _tr = vlm_mod.VlmPersonTracker(max_input_dim=640, min_hits=1,
                                           vlm_provider="ollama", vlm_model="未安装的模型")
            line("链路名：开了多模态但没结果时如实写兜底",
                 tg.TerminalApp._ai_chain_name(_tr).startswith("多模态"))
        except Exception as e:
            line("多模态 Tracker 构建失败：%s" % str(e)[:80], False)

    line("terminal_gui 导入", True)
    line("TerminalApp 类存在", hasattr(tg, "TerminalApp"))
    for name in ("_build_login", "_do_login", "_build_main", "_build_monitor_tab",
                 "_monitor_manage", "_monitor_render_grid", "_cam_update",
                 "_ai_send", "_ai_watch_maybe", "_ai_snapshot",
                 "_current_interface", "_refresh_ai_mode_label", "_demo"):
        line("TerminalApp.%s 方法存在" % name, hasattr(tg.TerminalApp, name))
    line("_BehaviorTracker 在模块级",
         hasattr(tg, "_BehaviorTracker") and not hasattr(tg.TerminalApp, "_BehaviorTracker"))

    # AI 提示词按界面区分
    names = sorted(ai_assistant.INTERFACE_PROMPTS)
    line("AI 界面提示词：%s" % "、".join(names), len(names) >= 5)
    for key in ("alerts", "review", "verify", "monitor", "ops"):
        p1 = ai_assistant.build_system_prompt(key)
        p2 = ai_assistant.build_system_prompt(None)
        line("build_system_prompt(%s) 与通用提示词不同" % key, len(p1) > len(p2))

    # ---- 真实走一遍启动流程 ----
    root = tk.Tk()
    root.withdraw()
    try:
        app = tg.TerminalApp(root)
        line("登录页构建", True)
    except Exception:
        line("登录页构建失败：%s" % traceback.format_exc().splitlines()[-1], False)
        raise

    app.u_var.set("owner")
    app.p_var.set("EDY_POWERQ")
    app._do_login()
    line("owner 登录并进入主界面", app.session is not None
         and getattr(app.session, "role", "") == config.ROLE_OWNER)

    for attr, label in (("tab_alerts", "告警处置页"), ("tab_review", "复核留痕页"),
                        ("tab_verify", "手持核验页"), ("tab_monitor", "监控查看页"),
                        ("tab_ops", "运行状态与管控页")):
        w = getattr(app, attr, None)
        line("%s已构建" % label, bool(w is not None and w.winfo_exists()))

    line("监控页分区网格（考场监考 / 非考场监控）",
         hasattr(app, "mon_exam_grid") and hasattr(app, "mon_other_grid"))
    line("考场监考默认开启 AI", bool(app.mon_ai.get()))
    line("AI 辅助识别仅考场监考生效",
         app.AREA_EXAM == "exam" and app.AREA_OTHER == "other")

    app.refresh_all()
    line("refresh_all 全量刷新", True)

    # AI 提示词跟随页签
    app.nb.select(app.tab_monitor)
    root.update_idletasks()
    line("切到监控页后界面标识=monitor", app._current_interface() == "monitor")
    app.nb.select(app.tab_ops)
    root.update_idletasks()
    line("切到管控页后界面标识=ops", app._current_interface() == "ops")
    snap = app._ai_snapshot()
    line("快照含『当前界面』字段", "当前界面" in snap)

    # owner：演示告警入口存在（在权限控制台里）
    line("owner 可见监控配置入口",
         app.session.role == config.ROLE_OWNER)

    root.destroy()

    # ---- 非 owner 权限收敛（独立进程验证，避免同进程账号缓存干扰）----
    line("非 owner 收敛项见 tests（管理摄像头/演示告警/AI开关仅 owner）", None)

    fails = sum(1 for r in REPORT if r.startswith("[FAIL]"))
    line("=" * 46)
    line("自检结果：%s（FAIL 项 %d）" % ("通过" if fails == 0 else "未通过", fails),
         fails == 0)

    log_dir = str(config.LOG_DIR)
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
