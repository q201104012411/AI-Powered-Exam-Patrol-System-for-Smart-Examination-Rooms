# -*- coding: utf-8 -*-
"""定位"窗口自己关掉"的元凶：包装 root.after，记录每个回调的注册位置，
销毁发生时打印"当前正在执行的回调是哪一个"。
"""
import os
import sys
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import tkinter as tk                                    # noqa: E402

CURRENT = {"where": "<无（主流程/事件）>"}
LOG = []

_orig_after = tk.Misc.after
_orig_destroy = tk.Tk.destroy


def _traced_after(self, ms, func=None, *args):
    if func is None:
        return _orig_after(self, ms)

    # 记录注册位置（跳过 tkinter 内部帧）
    stack = [f for f in traceback.format_stack()[:-1]
             if "tkinter" not in f and "_追踪after" not in f]
    where = stack[-1].strip() if stack else "?"
    LOG.append((ms, where))

    def _wrapped(*a, **kw):
        prev = CURRENT["where"]
        CURRENT["where"] = "%s  (after %sms)" % (where, ms)
        try:
            return func(*a, **kw)
        except Exception:
            print(">>> 回调抛异常（注册于 %s）:\n%s"
                  % (where, traceback.format_exc()), flush=True)
            raise
        finally:
            CURRENT["where"] = prev
    return _orig_after(self, ms, _wrapped, *args)


def _traced_destroy(self, *a, **kw):
    print("=" * 70, flush=True)
    print(">>> 窗口被销毁！正在执行的回调：", flush=True)
    print("    " + CURRENT["where"], flush=True)
    print(">>> 已注册的 after 回调（最近 15 条）：", flush=True)
    for ms, where in LOG[-15:]:
        print("    %6sms  %s" % (ms, where), flush=True)
    print("=" * 70, flush=True)
    return _orig_destroy(self, *a, **kw)


tk.Misc.after = _traced_after
tk.Tk.destroy = _traced_destroy

sys.excepthook = lambda tp, val, tb: print(">>> 未捕获异常:\n%s"
                                          % "".join(traceback.format_exception(tp, val, tb)),
                                          flush=True)

import gui                                              # noqa: E402

print(">>> 启动 ...", flush=True)
root = tk.Tk()
try:
    app = gui.FaceVerifyGUI(root)
    root.after(15000, lambda: (print(">>> 15 秒看门狗：窗口一直正常，主动退出", flush=True),
                               _orig_destroy(root)))
    root.mainloop()
except Exception:
    print(">>> 启动抛异常:\n%s" % traceback.format_exc(), flush=True)
print(">>> 结束", flush=True)
