# -*- coding: utf-8 -*-
"""追踪 gui.py 启动过程中到底是谁销毁了主窗口。

现象：双击启动后窗口很快自己关掉、退出码 0、控制台无输出。mainloop 正常返回
只可能是 root 被 destroy 了，这里给 Tk.destroy 打补丁，打印调用栈。
"""
import os
import sys
import time
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import tkinter as tk                                    # noqa: E402

_orig_destroy = tk.Tk.destroy
_destroy_calls = []


def _traced_destroy(self, *a, **kw):
    stack = "".join(traceback.format_stack()[:-1])
    _destroy_calls.append(stack)
    print(">>> root.destroy() 被调用，调用栈：", flush=True)
    print(stack, flush=True)
    return _orig_destroy(self, *a, **kw)


tk.Tk.destroy = _traced_destroy

# 记录所有 after 回调里的异常
sys.excepthook = lambda tp, val, tb: print(">>> 未捕获异常:\n%s"
                                          % "".join(traceback.format_exception(tp, val, tb)),
                                          flush=True)

import gui                                              # noqa: E402

print(">>> 创建 Tk 与 FaceVerifyGUI ...", flush=True)
root = tk.Tk()
t0 = time.time()
try:
    app = gui.FaceVerifyGUI(root)
    print(">>> FaceVerifyGUI 构造完成，进入 mainloop", flush=True)
    # 看门狗：如果 12 秒内 mainloop 还在跑，说明窗口是稳的，主动退出
    root.after(12000, lambda: (print(">>> 12 秒看门狗触发：窗口一直在运行（正常）", flush=True),
                               root.destroy()))
    root.mainloop()
    print(">>> mainloop 返回，存活 %.1f 秒" % (time.time() - t0), flush=True)
except Exception:
    print(">>> 启动抛异常:\n%s" % traceback.format_exc(), flush=True)

print(">>> destroy 调用次数: %d" % len(_destroy_calls), flush=True)
if not _destroy_calls:
    print(">>> 没有任何 destroy 调用（说明 mainloop 因其他原因返回）", flush=True)
