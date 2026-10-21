# -*- coding: utf-8 -*-
"""04 巡考机器人 · Windows 端浏览/导出面板。

说明：小车代码本来就是 Python（ROS1 Melodic / ROS2 两套），运行环境是 Ubuntu；
Windows 端不安装 ROS，所以这里提供代码、文档、图示、测试记录的浏览和复制路径，
真车运动请把“巡考机器人”目录拷到 Ubuntu 小车后按 README 运行。
"""
import os
import subprocess
import sys
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

APP_TITLE = "04 巡考机器人"
COPYRIGHT = "© 2026 EDY_POWERQ"
ROOT = os.path.join(HERE, "巡考机器人")
EXT = (".py", ".sh", ".yaml", ".yml", ".json", ".md", ".html", ".txt",
       ".png", ".jpg", ".jpeg", ".log")


def human(n):
    try:
        n = float(n)
    except Exception:                                        # noqa: BLE001
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024


class RobotApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE + " · 智能考场 AI 巡考系统")
        self.root.geometry("980x620")
        self.root.minsize(860, 520)
        self.paths = {}
        if ui_theme is not None:
            ui_theme.apply(root)
        self._build()
        self._load()

    def _build(self):
        bg = getattr(ui_theme, "BG", "#070d1a") if ui_theme else "#070d1a"
        panel = getattr(ui_theme, "PANEL", "#0d1a2e") if ui_theme else "#0d1a2e"
        fg = getattr(ui_theme, "FG", "#e8f1ff") if ui_theme else "#e8f1ff"
        dim = getattr(ui_theme, "DIM", "#8ba3c7") if ui_theme else "#8ba3c7"
        accent = getattr(ui_theme, "ACCENT", "#22d3ee") if ui_theme else "#22d3ee"

        head = tk.Frame(self.root, bg=bg)
        head.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(head, text="04 巡考机器人", bg=bg, fg=fg,
                 font=("Microsoft YaHei", 18, "bold")).pack(side="left")
        tk.Label(head, text="Ubuntu / ROS1 Melodic / ROS2 · Python 代码与真车记录",
                 bg=bg, fg=dim, font=("Microsoft YaHei", 10)).pack(side="left", padx=14)

        tip = tk.Label(
            self.root, bg=bg, fg=accent, justify="left", anchor="w",
            font=("Microsoft YaHei", 10),
            text="说明：小车代码本来就是 Python，运行环境是 Ubuntu + ROS；"
                 "Windows 端提供浏览/复制路径，真车运行请把“巡考机器人”目录拷到小车。")
        tip.pack(fill="x", padx=18, pady=(2, 8))

        bar = tk.Frame(self.root, bg=bg)
        bar.pack(fill="x", padx=16, pady=(0, 8))
        ttk.Button(bar, text="打开选中", command=self.open_sel).pack(side="left")
        ttk.Button(bar, text="打开所在文件夹",
                   command=self.open_dir).pack(side="left", padx=8)
        ttk.Button(bar, text="复制路径", command=self.copy_path).pack(side="left")
        ttk.Button(bar, text="打开项目说明",
                   command=self.open_readme).pack(side="left", padx=8)
        self.status = tk.Label(bar, text="", bg=bg, fg=dim,
                               font=("Microsoft YaHei", 9))
        self.status.pack(side="right")

        wrap = tk.Frame(self.root, bg=panel, highlightbackground="#1b3457",
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        cols = ("name", "kind", "size")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings")
        self.tree.heading("#0", text="目录 / 文件")
        self.tree.heading("name", text="名称")
        self.tree.heading("kind", text="类型")
        self.tree.heading("size", text="大小")
        self.tree.column("#0", width=330)
        self.tree.column("name", width=300)
        self.tree.column("kind", width=90, anchor="center")
        self.tree.column("size", width=90, anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.open_sel())

        foot = tk.Frame(self.root, bg=bg)
        foot.pack(fill="x", padx=18, pady=(2, 10))
        tk.Label(foot, text=COPYRIGHT, bg=bg, fg=dim,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Label(foot, text="EDY_POWERQ", bg=bg, fg=accent,
                 font=("Microsoft YaHei", 9, "bold")).pack(side="right")

    def _load(self):
        if not os.path.isdir(ROOT):
            self.status.config(text="缺少 巡考机器人 目录")
            return
        dirs = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in
                           ("__pycache__", "归档", "05_历史存档")]
            rel = os.path.relpath(dirpath, ROOT)
            if rel != ".":
                dirs.append(rel)
        for rel in sorted(dirs, key=lambda x: x.count(os.sep)):
            parent_rel = os.path.dirname(rel)
            parent = "" if parent_rel in ("", ".") else "d:" + parent_rel
            iid = "d:" + rel
            self.tree.insert(parent, "end", iid=iid, text=os.path.basename(rel),
                             values=("", "目录", "-"), open=False)
            self.paths[iid] = os.path.join(ROOT, rel)
        count = 0
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in
                           ("__pycache__", "归档", "05_历史存档")]
            rel = os.path.relpath(dirpath, ROOT)
            parent = "" if rel == "." else "d:" + rel
            for fn in sorted(filenames):
                if not fn.lower().endswith(EXT):
                    continue
                full = os.path.join(dirpath, fn)
                iid = "f:" + os.path.relpath(full, ROOT)
                try:
                    size = human(os.path.getsize(full))
                except Exception:                            # noqa: BLE001
                    size = "-"
                kind = os.path.splitext(fn)[1].lstrip(".").upper() or "FILE"
                self.tree.insert(parent, "end", iid=iid, text=fn,
                                 values=(fn, kind, size))
                self.paths[iid] = full
                count += 1
        self.status.config(text="%d 个文件 · 双击打开" % count)
    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            return ""
        return self.paths.get(sel[0], "")

    def open_sel(self):
        p = self._sel()
        if not p:
            messagebox.showinfo(APP_TITLE, "先选中一个文件或目录。")
            return
        try:
            os.startfile(p)                                  # type: ignore[attr-defined]
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror(APP_TITLE, "打开失败：\n%s\n\n%s" % (p, exc))

    def open_dir(self):
        p = self._sel()
        if not p:
            return
        d = p if os.path.isdir(p) else os.path.dirname(p)
        try:
            os.startfile(d)                                  # type: ignore[attr-defined]
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(exc))

    def copy_path(self):
        p = self._sel()
        if not p:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(p)
        self.status.config(text="已复制：" + p)

    def open_readme(self):
        for p in (os.path.join(HERE, "巡考机器人.md"),
                  os.path.join(ROOT, "README.md"),
                  os.path.join(ROOT, "备份说明.md")):
            if os.path.isfile(p):
                os.startfile(p)                              # type: ignore[attr-defined]
                return
        messagebox.showinfo(APP_TITLE, "未找到说明文档。")


def main():
    root = tk.Tk()
    RobotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
