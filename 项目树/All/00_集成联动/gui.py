# -*- coding: utf-8 -*-
"""00 集成联动 · 状态面板（深色 UI）。

之前 00 从总控台进入是命令行 main.py，答辩时只看到黑窗口，容易误以为“没反应”。
这里给一个只读状态面板：刷新 9 个专题的 integration_api 状态，并可一键打开
原有端到端自检（39/39）的控制台窗口。
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if os.path.isfile(os.path.join(_p, "ui_theme.py")):
        if _p not in sys.path:
            sys.path.append(_p)
        break

try:
    import ui_theme
except Exception:                                            # noqa: BLE001
    ui_theme = None

import linkage

APP_TITLE = "00 集成联动"
COPYRIGHT = "© 2026 EDY_POWERQ"


def python_exe():
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            return cand
    return exe


class LinkageApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE + " · 智能考场 AI 巡考系统")
        self.root.geometry("960x560")
        self.root.minsize(820, 480)
        self.queue = queue.Queue()
        self.link = linkage.Linkage(timeout=30)
        self.busy = False

        if ui_theme is not None:
            ui_theme.apply(root)
        self._build()
        self.root.after(150, self._poll)
        self.refresh()

    def _build(self):
        bg = getattr(ui_theme, "BG", "#070d1a") if ui_theme else "#070d1a"
        panel = getattr(ui_theme, "PANEL", "#0d1a2e") if ui_theme else "#0d1a2e"
        fg = getattr(ui_theme, "FG", "#e8f1ff") if ui_theme else "#e8f1ff"
        dim = getattr(ui_theme, "DIM", "#8ba3c7") if ui_theme else "#8ba3c7"
        accent = getattr(ui_theme, "ACCENT", "#22d3ee") if ui_theme else "#22d3ee"

        head = tk.Frame(self.root, bg=bg)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="00 集成联动", bg=bg, fg=fg,
                 font=("Microsoft YaHei", 18, "bold")).pack(side="left")
        tk.Label(head, text="9 个专题 · 子进程 JSON 总线 · 告警/命令/数据回流",
                 bg=bg, fg=dim, font=("Microsoft YaHei", 10)).pack(side="left", padx=14)

        bar = tk.Frame(self.root, bg=bg)
        bar.pack(fill="x", padx=16, pady=(4, 8))
        self.btn_refresh = ttk.Button(bar, text="刷新状态", command=self.refresh)
        self.btn_refresh.pack(side="left")
        ttk.Button(bar, text="运行端到端自检（控制台）",
                   command=self.run_selftest).pack(side="left", padx=8)
        self.status = tk.Label(bar, text="就绪", bg=bg, fg=dim,
                               font=("Microsoft YaHei", 9))
        self.status.pack(side="right")

        wrap = tk.Frame(self.root, bg=panel, highlightbackground="#1b3457",
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        cols = ("code", "name", "ok", "detail")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        for c, text, width, anchor in (
            ("code", "序号", 70, "center"),
            ("name", "专题", 260, "w"),
            ("ok", "状态", 90, "center"),
            ("detail", "说明 / 错误", 500, "w"),
        ):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor=anchor, stretch=(c == "detail"))
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        try:
            self.tree.tag_configure("ok", foreground="#34d399")
            self.tree.tag_configure("bad", foreground="#f87171")
        except Exception:                                    # noqa: BLE001
            pass

        tip = tk.Label(self.root, text="说明：状态来自各专题 integration_api.py；"
                                      "自检会打开命令行窗口并保留输出。",
                       bg=bg, fg=dim, font=("Microsoft YaHei", 9), anchor="w")
        tip.pack(fill="x", padx=18)
        foot = tk.Frame(self.root, bg=bg)
        foot.pack(fill="x", padx=18, pady=(2, 10))
        tk.Label(foot, text=COPYRIGHT, bg=bg, fg=dim,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Label(foot, text="EDY_POWERQ", bg=bg, fg=accent,
                 font=("Microsoft YaHei", 9, "bold")).pack(side="right")

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.status.config(text="正在查询 9 个专题 ...")
        self.btn_refresh.config(state="disabled")

        def work():
            try:
                st = self.link.status()
            except Exception as exc:                          # noqa: BLE001
                st = {"_error": str(exc)}
            self.queue.put(("status", st))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == "status":
                    self._show_status(data)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _show_status(self, st):
        self.busy = False
        self.btn_refresh.config(state="normal")
        for item in self.tree.get_children():
            self.tree.delete(item)
        if "_error" in st:
            self.status.config(text="查询失败：" + st["_error"])
            return
        okn = 0
        for code in sorted(st):
            v = st[code]
            ok = bool(v.get("ok"))
            okn += 1 if ok else 0
            detail = v.get("reason") or v.get("system") or ""
            self.tree.insert("", "end", values=(code, v.get("name") or "",
                                                "可用" if ok else "不可用",
                                                str(detail)[:300]),
                             tags=("ok" if ok else "bad",))
        self.status.config(text="可用 %d / %d" % (okn, len(st)))

    def run_selftest(self):
        script = os.path.join(HERE, "main.py")
        if not os.path.isfile(script):
            messagebox.showerror(APP_TITLE, "找不到 main.py：\n" + script)
            return
        try:
            subprocess.Popen([python_exe(), script], cwd=HERE,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as exc:                              # noqa: BLE001
            messagebox.showerror(APP_TITLE, "启动自检失败：\n" + str(exc))


def main():
    root = tk.Tk()
    LinkageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
