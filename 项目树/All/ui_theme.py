# -*- coding: utf-8 -*-
r"""
统一深色主题（与总控台同一套配色）。

用法（模块的 GUI 启动处）：
    import ui_theme
    ui_theme.apply(self.root)       # 配好 ttk + tk 默认色
    ...构建界面...
    ui_theme.polish(self.root)      # 把遗漏的浅色 tk 控件统一刷深

着色原则：
  · ttk 控件由 `apply()` 统一配置；
  · tk 控件里只有**中性浅色**（白/浅灰/SystemButtonFace）会被替换，
    状态色（绿/黄/红/蓝）保持原样，避免把告警颜色刷没；
  · 标签/框架优先跟随父容器颜色，避免深色面板上出现浅色小方块。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# 与总控台 总控台.py 的配色逐字一致
BG = "#070d1a"
PANEL = "#0d1a2e"
PANEL2 = "#122542"
BORDER = "#1b3457"
FG = "#e8f1ff"
DIM = "#8ba3c7"
MUTE = "#5b739a"
ACCENT = "#22d3ee"
OK = "#34d399"
WARN = "#fbbf24"
ALARM = "#f43f5e"

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_TITLE = ("Microsoft YaHei", 17, "bold")
FONT_H1 = ("Microsoft YaHei", 13, "bold")

DARK_BG = {BG.lower(), PANEL.lower(), PANEL2.lower(), BORDER.lower()}
LIGHT_BG = {
    "", "systembuttonface", "systemwindow", "systemwindowframe",
    "white", "#ffffff", "#fff", "#f4f4f4", "#f4f6f8",
    "#f5f5f5", "#f9f9f9", "#fbfcfd", "#eeeeee", "#e8edf2", "#f2f2f2",
    "#f0f0f0", "#e0e0e0", "#dddddd", "#e6e6e6", "#efefef",
}
DARK_FG = {"black", "#000", "#000000", "#111", "#111111", "#222", "#222222",
           "#333", "#333333", "#444", "#444444",
           # Windows 原生控件默认前景色名（不处理会在深色底上显示成黑字）
           "systemwindowtext", "systembuttontext", "systemcontroltext"}
GRAY_FG = {"#555", "#555555", "#666", "#666666", "#777", "#777777",
           "#888", "#888888", "#999", "#999999", "#aaa", "#aaaaaa"}


def _style(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:                                        # noqa: BLE001
        pass

    st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                 bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                 troughcolor=BG, focuscolor=ACCENT)

    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=FG, font=FONT)
    st.configure("Dim.TLabel", background=BG, foreground=DIM, font=FONT_SMALL)
    st.configure("Panel.TLabel", background=PANEL, foreground=FG)
    st.configure("PanelDim.TLabel", background=PANEL, foreground=DIM,
                 font=FONT_SMALL)
    st.configure("H.TLabel", background=BG, foreground=FG, font=FONT_H1)
    st.configure("Title.TLabel", background=BG, foreground=FG, font=FONT_TITLE)

    st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                 padding=(10, 6), font=FONT)
    st.map("TButton",
           background=[("disabled", PANEL), ("active", BORDER)],
           foreground=[("disabled", MUTE), ("active", FG)])
    st.configure("Accent.TButton", background="#1d4ed8", foreground="#ffffff",
                 borderwidth=0, padding=(12, 7), font=FONT_B)
    st.map("Accent.TButton",
           background=[("active", "#2563eb"), ("disabled", PANEL2)],
           foreground=[("disabled", MUTE)])
    st.configure("Mini.TButton", background=PANEL2, foreground=DIM,
                 borderwidth=0, padding=(8, 4), font=FONT_SMALL)
    st.map("Mini.TButton", background=[("active", BORDER)])

    st.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                 insertcolor=FG, bordercolor=BORDER, padding=(5, 5))
    st.map("TEntry", fieldbackground=[("readonly", PANEL)],
           foreground=[("readonly", DIM)])
    st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                 foreground=FG, arrowcolor=DIM, bordercolor=BORDER,
                 padding=(4, 4))
    st.map("TCombobox",
           fieldbackground=[("readonly", PANEL2)],
           foreground=[("readonly", FG)],
           background=[("active", BORDER)])
    st.configure("TSpinbox", fieldbackground=PANEL2, background=PANEL2,
                 foreground=FG, arrowcolor=DIM, bordercolor=BORDER)

    st.configure("TCheckbutton", background=BG, foreground=DIM, font=FONT_SMALL,
                 indicatorcolor=PANEL2)
    st.map("TCheckbutton",
           background=[("active", BG)],
           foreground=[("active", FG)],
           indicatorcolor=[("selected", ACCENT)])
    st.configure("TRadiobutton", background=BG, foreground=DIM, font=FONT_SMALL,
                 indicatorcolor=PANEL2)
    st.map("TRadiobutton",
           background=[("active", BG)],
           foreground=[("active", FG)],
           indicatorcolor=[("selected", ACCENT)])

    st.configure("TLabelframe", background=BG, bordercolor=BORDER,
                 borderwidth=1, relief="solid")
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                 font=FONT_B)

    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(2, 4, 2, 0))
    st.configure("TNotebook.Tab", background=PANEL2, foreground=DIM,
                 padding=(16, 8), font=FONT)
    st.map("TNotebook.Tab",
           background=[("selected", PANEL), ("active", BORDER)],
           foreground=[("selected", ACCENT), ("active", FG)])

    st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                 foreground=FG, rowheight=28, borderwidth=0, font=FONT)
    st.configure("Treeview.Heading", background=PANEL2, foreground=DIM,
                 borderwidth=0, font=FONT_B, relief="flat")
    st.map("Treeview",
           background=[("selected", "#1d4ed8")],
           foreground=[("selected", "#ffffff")])
    st.map("Treeview.Heading", background=[("active", BORDER)])

    st.configure("TScrollbar", background=PANEL2, troughcolor=BG,
                 bordercolor=BORDER, arrowcolor=DIM, gripcount=0)
    st.configure("TSeparator", background=BORDER)
    st.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2,
                 bordercolor=BORDER)
    st.configure("TMenubutton", background=PANEL2, foreground=FG,
                 borderwidth=0, padding=(8, 4))
    st.configure("TPanedwindow", background=BG)
    st.configure("Sash", sashthickness=6, gripcount=0, background=BORDER)
    st.configure("TScale", background=BG, troughcolor=PANEL2)
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT)


def apply(root):
    """在构建界面前调用：设置窗口底色、tk 默认色与 ttk 全局样式。"""
    try:
        root.configure(bg=BG)
    except Exception:                                        # noqa: BLE001
        pass
    # tk 控件没有显式配色时使用这套默认值
    try:
        root.option_add("*Background", BG)
        root.option_add("*Foreground", FG)
        root.option_add("*selectBackground", "#1d4ed8")
        root.option_add("*selectForeground", "#ffffff")
        root.option_add("*insertBackground", FG)
        root.option_add("*Toplevel.background", BG)
        root.option_add("*Menu.background", PANEL)
        root.option_add("*Menu.foreground", FG)
        root.option_add("*Menu.activeBackground", BORDER)
        root.option_add("*Menu.activeForeground", FG)
    except Exception:                                        # noqa: BLE001
        pass
    _style(root)


def _parent_bg(widget):
    try:
        parent = widget.master
        while parent is not None:
            try:
                b = str(parent.cget("background")).lower()
                if b in DARK_BG:
                    return b
            except Exception:                                # noqa: BLE001
                pass
            parent = getattr(parent, "master", None)
    except Exception:                                        # noqa: BLE001
        pass
    return BG


def _polish_one(w):
    try:
        cls = w.winfo_class()
    except Exception:                                        # noqa: BLE001
        return
    try:
        bg = str(w.cget("background")).lower()
    except Exception:                                        # noqa: BLE001
        bg = ""
    try:
        fg = str(w.cget("foreground")).lower()
    except Exception:                                        # noqa: BLE001
        fg = ""

    target = _parent_bg(w)

    if cls in ("Frame", "Labelframe", "LabelFrame"):
        if bg in LIGHT_BG or not bg:
            w.configure(background=target)
        if fg in DARK_FG:
            w.configure(foreground=ACCENT if cls in ("Labelframe", "LabelFrame") else FG)
    elif cls == "Label":
        if bg in LIGHT_BG or not bg:
            w.configure(background=target)
        if fg in DARK_FG:
            w.configure(foreground=FG)
        elif fg in GRAY_FG:
            w.configure(foreground=DIM)
    elif cls == "Button":
        # tk.Button 在 Windows 上即使 bg 是深色也可能按原生浅色按钮绘制；
        # flat + 0 边框才会真正使用我们给的深色。
        if bg in LIGHT_BG or bg in DARK_BG or not bg:
            w.configure(background=PANEL2, foreground=FG,
                        activebackground=BORDER, activeforeground=FG,
                        relief="flat", borderwidth=0, highlightthickness=0,
                        highlightbackground=BORDER, highlightcolor=BORDER)
    elif cls in ("Checkbutton", "Radiobutton"):
        if bg in LIGHT_BG or bg in DARK_BG or not bg:
            w.configure(background=target, foreground=DIM,
                        activebackground=target, activeforeground=FG,
                        selectcolor=PANEL2, highlightthickness=0)
    elif cls in ("Entry", "Text", "Listbox", "Spinbox"):
        cfg = {}
        if bg in LIGHT_BG or not bg:
            cfg.update(background=PANEL2, insertbackground=FG,
                       selectbackground="#1d4ed8", selectforeground="#ffffff",
                       highlightbackground=BORDER, highlightcolor=BORDER)
        if fg in DARK_FG:
            cfg["foreground"] = FG
        elif fg in GRAY_FG:
            cfg["foreground"] = DIM
        if cfg:
            w.configure(**cfg)
    elif cls == "Canvas":
        if bg in LIGHT_BG or not bg:
            w.configure(background=BG)
    elif cls == "Toplevel":
        if bg in LIGHT_BG or not bg:
            w.configure(background=BG)
    elif cls == "Menu":
        w.configure(background=PANEL, foreground=FG,
                    activebackground=BORDER, activeforeground=FG)


def polish(root):
    """递归把遗漏的中性浅色 tk 控件刷成深色。"""
    def walk(w):
        _polish_one(w)
        try:
            kids = w.winfo_children()
        except Exception:                                    # noqa: BLE001
            return
        for c in kids:
            walk(c)
    try:
        walk(root)
        root.update_idletasks()
    except Exception:                                        # noqa: BLE001
        pass


def polish_later(root, delay=250):
    try:
        root.after(delay, lambda: polish(root))
    except Exception:                                        # noqa: BLE001
        pass
