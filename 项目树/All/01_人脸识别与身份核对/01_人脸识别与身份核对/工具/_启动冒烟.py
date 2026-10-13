# -*- coding: utf-8 -*-
"""启动冒烟：完全按 gui.py __main__ 的方式启动界面并泵事件循环，捕获任何异常。

比单元测试更接近"用户双击启动"：会把 root.after 排队的回调（数据加载、
库数据体检、自动播放等）都跑一遍，任何一处抛异常都会被 excepthook 抓到。

运行：python 工具\_启动冒烟.py
退出码：0 = 启动与回调期间没有异常
"""
import os
import sys
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import tkinter as tk                                    # noqa: E402

import config                                           # noqa: E402

ERRORS = []


def _hook(tp, val, tb):
    ERRORS.append("".join(traceback.format_exception(tp, val, tb)))


sys.excepthook = _hook

import accounts                                          # noqa: E402
import gui                                               # noqa: E402


def main():
    print("工作目录:", os.getcwd())
    print("数据目录:", config.DATA_ROOT)
    accounts.MAJOR_EXAM_FLAG = str(config.DATA_ROOT / "major_exam.on")
    accounts.STRICT_MODE_FLAG = str(config.DATA_ROOT / "strict_mode.on")

    root = tk.Tk()
    root.withdraw()                                      # 冒烟测试不弹窗打扰
    try:
        app = gui.FaceVerifyGUI(root)
    except Exception:
        print("[失败] 构造 FaceVerifyGUI 抛异常：")
        traceback.print_exc()
        return 2

    try:
        app.user = {"username": "owner", "role": accounts.ROLE_OWNER,
                    "display_name": "系统所有者"}
        app._build_ui()
    except Exception:
        print("[失败] _build_ui 抛异常：")
        traceback.print_exc()
        return 3

    # 同步跑一遍数据加载（GUI 里是后台线程 + after 回调）
    try:
        app.candidates = app.store.load()
        if os.path.exists(config.INDEX_PATH):
            app.index.load()
        app._after_load(None)
    except Exception:
        print("[失败] 数据加载/_after_load 抛异常：")
        traceback.print_exc()
        return 4

    # 泵事件循环：让 root.after 排队的回调都执行
    for i in range(120):
        try:
            root.update()
        except tk.TclError as e:
            ERRORS.append("事件循环 TclError: %s" % e)
            break
        except Exception:
            ERRORS.append("事件循环异常:\n" + traceback.format_exc())
            break
        root.after(20, lambda: None)
        import time
        time.sleep(0.02)

    try:
        root.destroy()
    except Exception:
        pass

    if ERRORS:
        print("[失败] 启动/回调期间出现 %d 个异常：" % len(ERRORS))
        for e in ERRORS[:5]:
            print("-" * 70)
            print(e)
        return 1
    print("[通过] 界面启动与全部排程回调均无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
