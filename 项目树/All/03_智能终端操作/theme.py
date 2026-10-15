# -*- coding: utf-8 -*-
r"""
03 智能终端 · 统一主题层（大数据驾驶舱风格）

为什么要有这个文件：
  改造前 03 的颜色是散落字面量（全文没有 ttk.Style，颜色在 10+ 处硬编码），
  想统一改风格就得满文件找。这里把颜色/字体/间距收敛成**单一真源**：
    · 改风格只改这里；
    · 各页面引用语义名（如 T.ACCENT、T.LEVEL_COLOR["red"]），不再写死 #RRGGBB。

风格取向（参照"大数据驾驶舱"）：深空蓝底 + 青蓝霓虹强调 + 高对比数据色，
大字号数字、卡片式分区、清晰的分级色（红/橙/蓝/绿）。

用法：
    import theme as T
    T.apply_ttk(root)                 # 统一 ttk 控件外观（一次）
    T.card(parent, "今日告警")         # 卡片容器
    T.kpi(parent, "总告警", "128", "条", color=T.ACCENT)
    T.bar_chart(canvas, data)         # 画布柱状图（无第三方依赖）
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 配色
BG = "#070d1a"            # 最深：窗口底
BG2 = "#0b1526"           # 次级底：页签/工具条
PANEL = "#101d33"         # 卡片面
PANEL2 = "#16253f"        # 卡片内高亮面
BORDER = "#1e3557"        # 描边
GRID = "#16243c"          # 网格线

FG = "#e6f0ff"            # 主文字
FG_DIM = "#8ba3c7"        # 次文字
FG_MUTE = "#5b739a"       # 更弱

ACCENT = "#22d3ee"        # 青（主强调）
ACCENT2 = "#3b82f6"       # 蓝
ACCENT3 = "#a78bfa"       # 紫
OK = "#34d399"            # 绿
WARN = "#fbbf24"          # 黄
ALARM = "#f43f5e"         # 红

#: 告警分级 -> 颜色（与 03 config.LEVEL_META 的语义对齐）
LEVEL_COLOR = {
    "red": ALARM,
    "orange": WARN,
    "blue": ACCENT2,
    "green": OK,
}

#: 状态 -> 颜色（取流/服务/链路）
STATE_COLOR = {
    "ok": OK, "playing": OK, "idle": FG_DIM, "connecting": WARN,
    "warn": WARN, "error": ALARM, "closed": ALARM, "stopped": FG_MUTE,
}

# ---------------------------------------------------------------- 字体
FONT_UI = ("Microsoft YaHei", 10)
FONT_UI_B = ("Microsoft YaHei", 10, "bold")
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_MONO = ("Consolas", 10)
FONT_TITLE = ("Microsoft YaHei", 22, "bold")     # 大屏标题
FONT_H1 = ("Microsoft YaHei", 14, "bold")
FONT_H2 = ("Microsoft YaHei", 11, "bold")
FONT_KPI = ("Consolas", 26, "bold")              # KPI 大数字
FONT_KPI_S = ("Consolas", 18, "bold")

# ---------------------------------------------------------------- 间距
PAD = 8
PAD_S = 4
PAD_L = 14


def apply_ttk(root):
    """把 ttk 控件统一成深色驾驶舱风格（只需调一次）。

    注意：ttk 的多数外观要经 Style 才能改，直接 config(bg=...) 在部分主题下无效。
    这里同时设置 Style 与几个常用控件的默认字体，保证整体一致。
    """
    st = ttk.Style(root)
    # clam 是少数允许自由改色的内置主题（vista/xpnative 会忽略大部分配色）
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                 font=FONT_UI, bordercolor=BORDER, lightcolor=PANEL,
                 darkcolor=PANEL)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL, relief="flat")
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Card.TLabel", background=PANEL, foreground=FG)
    st.configure("Dim.TLabel", background=PANEL, foreground=FG_DIM)
    st.configure("Title.TLabel", background=BG, foreground=ACCENT, font=FONT_TITLE)
    st.configure("H1.TLabel", background=BG, foreground=FG, font=FONT_H1)
    st.configure("H2.TLabel", background=PANEL, foreground=ACCENT, font=FONT_H2)
    st.configure("KPI.TLabel", background=PANEL, foreground=FG, font=FONT_KPI)
    st.configure("KPIUnit.TLabel", background=PANEL, foreground=FG_DIM, font=FONT_SMALL)
    st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                 focusthickness=0, padding=(10, 5))
    st.map("TButton",
           background=[("active", BORDER), ("pressed", PANEL)],
           foreground=[("disabled", FG_MUTE)])
    st.configure("Accent.TButton", background=ACCENT2, foreground="#ffffff")
    st.map("Accent.TButton", background=[("active", ACCENT)])
    st.configure("TNotebook", background=BG2, borderwidth=0, tabmargins=(2, 4, 2, 0))
    st.configure("TNotebook.Tab", background=BG2, foreground=FG_DIM,
                 padding=(14, 7), borderwidth=0)
    st.map("TNotebook.Tab",
           background=[("selected", PANEL)],
           foreground=[("selected", ACCENT)])
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                 foreground=FG, rowheight=24, bordercolor=BORDER, borderwidth=0)
    st.configure("Treeview.Heading", background=PANEL2, foreground=ACCENT,
                 relief="flat", font=FONT_UI_B)
    st.map("Treeview", background=[("selected", BORDER)],
           foreground=[("selected", "#ffffff")])
    st.configure("TCheckbutton", background=BG, foreground=FG)
    st.map("TCheckbutton", background=[("active", BG)])
    st.configure("TRadiobutton", background=BG, foreground=FG)
    st.map("TRadiobutton", background=[("active", BG)])
    st.configure("TLabelframe", background=BG, foreground=ACCENT, bordercolor=BORDER,
                 relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_UI_B)
    st.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                 insertcolor=ACCENT, bordercolor=BORDER)
    st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                 foreground=FG, arrowcolor=ACCENT)
    st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG2,
                 bordercolor=BG2, arrowcolor=FG_DIM)
    st.configure("Horizontal.TScrollbar", background=PANEL2, troughcolor=BG2,
                 bordercolor=BG2, arrowcolor=FG_DIM)
    st.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2,
                 bordercolor=PANEL2, lightcolor=ACCENT, darkcolor=ACCENT)
    try:
        root.configure(bg=BG)
    except Exception:
        pass
    return st


# ---------------------------------------------------------------- 组件
def card(parent, title="", pady=(0, PAD), manager="pack", **grid_kw):
    """卡片容器：返回 (外框, 内容框)。标题可选。

    manager="pack"（默认）或 "grid" —— 同一个父容器里不能混用两种几何管理器，
    所以图表区用 grid 时必须显式传 manager="grid" 并给 grid 参数。
    """
    outer = ttk.Frame(parent, style="Card.TFrame", padding=PAD_L)
    if manager == "grid":
        outer.grid(**grid_kw)
    else:
        outer.pack(fill="x", pady=pady)
    if title:
        ttk.Label(outer, text=title, style="H2.TLabel").pack(anchor="w", pady=(0, 6))
    return outer, outer


def kpi(parent, label, value, unit="", color=None, width=None):
    """一个 KPI 数字块：大数字 + 小单位 + 说明标签。返回外框。"""
    f = ttk.Frame(parent, style="Card.TFrame", padding=(PAD_L, PAD, PAD_L, PAD))
    f.pack(side="left", fill="both", expand=True, padx=(0, PAD))
    ttk.Label(f, text=label, style="Dim.TLabel").pack(anchor="w")
    row = ttk.Frame(f, style="Card.TFrame")
    row.pack(anchor="w", pady=(2, 0))
    num = tk.Label(row, text=str(value), font=FONT_KPI, bg=PANEL,
                   fg=color or ACCENT)
    num.pack(side="left")
    if unit:
        ttk.Label(row, text=" " + unit, style="KPIUnit.TLabel").pack(side="left",
                                                                    pady=(10, 0))
    f._value_label = num          # 便于后续 set_kpi
    return f


def set_kpi(frame, value, color=None):
    """更新 KPI 数字（避免整页重建）"""
    lbl = getattr(frame, "_value_label", None)
    if lbl is not None:
        try:
            lbl.config(text=str(value))
            if color:
                lbl.config(fg=color)
        except Exception:
            pass


def hline(parent, color=None):
    """一条细分隔线（用 Frame 画，比 Canvas 便宜）"""
    s = tk.Frame(parent, height=1, bg=color or BORDER)
    s.pack(fill="x", pady=PAD_S)
    return s


def bar_chart(canvas, data, color=None, label_color=None, height=None):
    """在 Canvas 上画横向条形图。data = [(标签, 数值), ...]（无第三方依赖）

    返回最大值的标签（便于界面提示"最大项"）。
    """
    canvas.delete("all")
    if not data:
        canvas.create_text(10, 10, anchor="nw", text="暂无数据",
                           fill=FG_MUTE, font=FONT_SMALL)
        return None
    h = height or int(canvas.winfo_height() or 160)
    w = int(canvas.winfo_width() or 360)
    n = len(data)
    row_h = max(14, min(30, (h - 8) // max(1, n)))
    label_w = 96
    value_w = 62
    bar_max = max(1.0, max(v for _, v in data))
    # 网格
    for i in range(1, 4):
        x = label_w + (w - label_w - value_w) * i / 4.0
        canvas.create_line(x, 2, x, 4 + row_h * n, fill=GRID)
    top_label, top_val = None, -1
    for i, (lab, val) in enumerate(data):
        y = 4 + i * row_h
        canvas.create_text(label_w - 8, y + row_h / 2, anchor="e", text=str(lab)[:12],
                           fill=label_color or FG_DIM, font=FONT_SMALL)
        bw = int((w - label_w - value_w) * (float(val) / bar_max))
        canvas.create_rectangle(label_w, y + 3, label_w + max(2, bw), y + row_h - 3,
                                fill=color or ACCENT2, outline="")
        canvas.create_text(w - 4, y + row_h / 2, anchor="e", text=str(val),
                           fill=FG, font=FONT_SMALL)
        if float(val) > top_val:
            top_label, top_val = lab, float(val)
    return top_label


def line_chart(canvas, series, color=None, grid=True):
    """折线图。series = [(标签, 数值), ...]（按顺序连线）"""
    canvas.delete("all")
    pts = [(str(l), float(v)) for l, v in (series or [])]
    if len(pts) < 2:
        canvas.create_text(10, 10, anchor="nw", text="数据不足（至少 2 个点）",
                           fill=FG_MUTE, font=FONT_SMALL)
        return
    w = int(canvas.winfo_width() or 360)
    h = int(canvas.winfo_height() or 140)
    pad_l, pad_b, pad_t = 44, 22, 10
    vmax = max(1.0, max(v for _, v in pts))
    vmin = min(0.0, min(v for _, v in pts))
    span = max(1e-6, vmax - vmin)
    if grid:
        for i in range(5):
            y = pad_t + (h - pad_t - pad_b) * i / 4.0
            canvas.create_line(pad_l, y, w - 6, y, fill=GRID)
            canvas.create_text(pad_l - 6, y, anchor="e",
                               text=("%.0f" % (vmax - span * i / 4.0)),
                               fill=FG_MUTE, font=("Consolas", 8))
    xs = []
    for i, (lab, v) in enumerate(pts):
        x = pad_l + (w - pad_l - 10) * i / float(len(pts) - 1)
        y = pad_t + (h - pad_t - pad_b) * (1.0 - (v - vmin) / span)
        xs.append((x, y))
        if i % max(1, len(pts) // 6) == 0:
            canvas.create_text(x, h - 8, text=lab[-5:], fill=FG_MUTE,
                               font=("Consolas", 8))
    canvas.create_line([c for p in xs for c in p], fill=color or ACCENT, width=2,
                       smooth=False)
    for x, y in xs:
        canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color or ACCENT, outline="")


def bar_chart_unicode():
    """返回一组可用于纯文本"柱状"的字符（无 GUI 时用，如自检输出）"""
    return "▁▂▃▄▅▆▇█"
