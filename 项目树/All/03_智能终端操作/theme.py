# -*- coding: utf-8 -*-
r"""
03 智能终端 · 统一主题层（大数据驾驶舱风格）

为什么要有这个文件：
  改造前 03 的颜色是散落字面量（全文没有 ttk.Style，颜色在 10+ 处硬编码），
  想统一改风格就得满文件找。这里把颜色/字体/间距/图表收敛成**单一真源**：
    · 改风格只改这里；
    · 各页面引用语义名（如 T.ACCENT、T.LEVEL_COLOR["red"]），不再写死 #RRGGBB。

风格取向（参照"大数据驾驶舱"）：深空蓝底 + 青蓝霓虹强调 + 高对比数据色，
大字号数字、卡片式分区、清晰的分级色（红/橙/蓝/绿）。

第三方依赖：**没有**。图表全部用 tkinter Canvas 手绘 —— 考场机器装 matplotlib
往往装不上（无网 + 编译依赖），所以宁可自己画。所有图元都按画布**当前尺寸**
计算，配合 `<Configure>` 回调即可随窗口/大屏缩放自动重排。

用法：
    import theme as T
    T.apply_ttk(root)                       # 统一 ttk 控件外观（一次）
    T.card(parent, "今日告警", accent=T.ALARM)   # 卡片容器
    tile = T.kpi_tile(parent, "总告警", 128, "条", T.ACCENT)
    T.set_tile(tile, 129)                    # 只改数字，不重建页面
    T.donut_chart(canvas, [...] )            # 环形占比图
    T.area_line_chart(canvas, [...])         # 面积折线图
    T.bar_chart(canvas, [...])               # 横向条形图
"""
from __future__ import annotations

import math
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

#: 多系列图表轮换色（超过 6 项时循环使用）
SERIES = (ACCENT, ACCENT2, ACCENT3, OK, WARN, ALARM,
          "#f472b6", "#facc15", "#2dd4bf", "#fb923c")

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
FONT_TINY = ("Microsoft YaHei", 8)
FONT_MONO = ("Consolas", 10)
FONT_TITLE = ("Microsoft YaHei", 22, "bold")     # 大屏标题
FONT_H1 = ("Microsoft YaHei", 14, "bold")
FONT_H2 = ("Microsoft YaHei", 11, "bold")
FONT_KPI = ("Consolas", 26, "bold")              # KPI 大数字
FONT_KPI_S = ("Consolas", 20, "bold")            # 紧凑 KPI 数字
FONT_DONUT = ("Consolas", 22, "bold")            # 环形图中心数字

# ---------------------------------------------------------------- 间距
PAD = 8
PAD_S = 4
PAD_L = 14


# ---------------------------------------------------------------- 小工具
def _rgb(color):
    c = str(color).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def blend(c1, c2, t):
    """把 c1 按比例 t 混向 c2（0=全 c1，1=全 c2），用于做"半透明"色块。"""
    t = max(0.0, min(1.0, float(t)))
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(a[i] * (1 - t) + b[i] * t))
                                   for i in range(3))


def fmt_int(n):
    """整数千分位；非数字原样返回。"""
    try:
        return "{:,}".format(int(round(float(n))))
    except (TypeError, ValueError):
        return str(n)


def fmt_pct(part, total, digits=0):
    """占比文本，total=0 时返回 '—'（不编 0%）。"""
    try:
        total = float(total)
        if total <= 0:
            return "—"
        return ("%." + str(int(digits)) + "f%%") % (float(part) * 100.0 / total)
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def fmt_duration(sec):
    """秒 -> 人类可读（<60 秒 / n 分 / n 小时 / n 天）。"""
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        return "—"
    if s < 60:
        return "%d 秒" % int(s)
    if s < 3600:
        return "%d 分" % int(s // 60)
    if s < 86400:
        return "%.1f 小时" % (s / 3600.0)
    return "%.1f 天" % (s / 86400.0)


def nice_ticks(vmax, count=4):
    """给数值轴挑"好看"的刻度：返回 (刻度列表, 顶端值)。

    为什么需要：原折线图直接把 vmax 四等分，数据最大值为 1 时会出现
    "0 / 1 / 1 / 0 / 0" 这种重复且无信息的标签。这里保证刻度落在
    1/2/5×10^k 上，并且计数类刻度至少是整数。
    """
    try:
        vmax = float(vmax)
    except (TypeError, ValueError):
        vmax = 0.0
    count = max(1, int(count))
    if vmax <= 0:
        return [0.0, 1.0], 1.0
    raw = vmax / float(count)
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = None
    for m in (1.0, 2.0, 5.0, 10.0):
        step = m * mag
        if step >= raw:
            break
    step = max(step or 1.0, 1.0)             # 计数轴：最小步长 1
    top = math.ceil(vmax / step - 1e-9) * step
    ticks = [round(step * i, 6) for i in range(int(round(top / step)) + 1)]
    return ticks, float(top)


def _truncate(text, max_chars):
    s = str(text)
    if max_chars <= 1 or len(s) <= max_chars:
        return s
    return s[:max_chars - 1] + "…"


def truncate_px(text, max_px, cjk=12, ascii_w=7):
    """按**像素**截断并补省略号（中英混排按字符数截断会时短时长）。"""
    s = str(text)
    if text_width(s, cjk, ascii_w) <= max_px:
        return s
    out = ""
    for ch in s:
        if text_width(out + ch + "…", cjk, ascii_w) > max_px:
            break
        out += ch
    return out + "…"


def text_width(text, cjk=12, ascii_w=7):
    """粗略估算文本像素宽度（中文≈cjk、ASCII≈ascii_w）。

    为什么需要：中英混排时按"字符数×9"估宽度会低估一倍，中文标签就会被
    卡片左边裁掉（改造后首屏就踩到了：类型 TOP 的"心跳丢失"少了一个字）。
    """
    w = 0
    for ch in str(text):
        w += cjk if ord(ch) > 0x2E80 else ascii_w
    return w


def _canvas_size(canvas, min_w=60, min_h=40):
    """取画布当前像素尺寸；尚未布局时返回 None（由调用方跳过一次绘制）。"""
    try:
        w = int(canvas.winfo_width())
        h = int(canvas.winfo_height())
    except Exception:
        return None
    if w < min_w or h < min_h:
        return None
    return w, h


# ---------------------------------------------------------------- ttk 外观
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
    st.configure("CardDim.TLabel", background=PANEL, foreground=FG_DIM,
                 font=FONT_SMALL)
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
    # 卡片标题栏里的小按钮（切换时间窗等），做成不抢眼的样式
    st.configure("Mini.TButton", background=PANEL2, foreground=FG_DIM,
                 borderwidth=0, focusthickness=0, padding=(6, 2), font=FONT_TINY)
    st.map("Mini.TButton",
           background=[("active", BORDER)],
           foreground=[("active", ACCENT)])
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


# ---------------------------------------------------------------- 容器组件
class VScroll(ttk.Frame):
    r"""竖向滚动容器：内容比可视区高时可以滚动，**不会像 pack 那样把内容裁掉**。

    为什么需要：驾驶舱在大屏上要"铺满"，在 1180×820 的小窗口里又必须放得下。
    纯 pack/grid 只要内容超出就被静默裁掉（改造前明细表+页脚就是这么消失的）。
    这里用 Canvas + 内嵌 Frame：内容矮于可视区时按权重拉伸填满，高于可视区时滚动。

    用法：
        sv = VScroll(parent)
        sv.pack(fill="both", expand=True)
        ttk.Label(sv.body, text="...").pack()      # 往 sv.body 里放控件
    """

    def __init__(self, parent, **kw):
        ttk.Frame.__init__(self, parent, style="TFrame", **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, style="TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # 滚轮：只在鼠标进入本容器时拦截，避免和别的页签抢滚轮
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    # ---- 布局同步 ----
    def _on_body_configure(self, _e=None):
        self._sync()

    def _on_canvas_configure(self, e):
        try:
            self.canvas.itemconfigure(self._win, width=e.width)
        except Exception:
            pass
        self._sync()

    def _sync(self):
        try:
            need = int(self.body.winfo_reqheight())
            view = int(self.canvas.winfo_height())
            width = int(self.canvas.winfo_width())
        except Exception:
            return
        # 内容比可视区矮 -> 把内嵌 Frame 撑满（内部 weight 才能生效）
        tall = max(need, view)
        try:
            self.canvas.itemconfigure(self._win, width=width,
                                      height=max(need, view))
            self.canvas.configure(scrollregion=(0, 0, width, tall))
        except Exception:
            pass
        if need > view and view > 0:
            if not self.vsb.winfo_ismapped():
                self.vsb.pack(side="right", fill="y")
        elif self.vsb.winfo_ismapped():
            self.vsb.pack_forget()
            try:
                self.canvas.yview_moveto(0)
            except Exception:
                pass

    def _on_scroll(self, first, last):
        try:
            self.vsb.set(first, last)
        except Exception:
            pass

    # ---- 滚轮 ----
    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        # 内容没超出可视区就不动，免得"空滚"
        try:
            if self.body.winfo_reqheight() <= self.canvas.winfo_height():
                return
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass


def card(parent, title="", pady=(0, PAD), manager="pack", accent=None,
         subtitle="", tools=None, **grid_kw):
    """卡片容器：返回 (外框, 内容框)。标题可选，accent 给标题左侧色条。

    manager="pack"（默认）或 "grid" —— 同一个父容器里不能混用两种几何管理器，
    所以图表区用 grid 时必须显式传 manager="grid" 并给 grid 参数。
    第二个返回值与第一个相同：内容直接放进卡片外框即可（标题会自动排在上方）。

    subtitle 显示在标题栏右侧；tools=[(按钮文字, 回调), ...] 也放标题栏右侧
    （比底部工具条省一整行高度）。标题栏本身挂在返回值的 `.head` 属性上。
    """
    outer = ttk.Frame(parent, style="Card.TFrame",
                      padding=(PAD_L, PAD, PAD_L, PAD))
    if manager == "grid":
        outer.grid(**grid_kw)
    else:
        pady = grid_kw.pop("pady", pady)
        outer.pack(fill="x", pady=pady, **grid_kw)
    if title:
        head = ttk.Frame(outer, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 6))
        outer.head = head
        if accent:
            bar = tk.Frame(head, width=3, height=14, bg=accent)
            bar.pack(side="left", padx=(0, 6))
            bar.pack_propagate(False)
        ttk.Label(head, text=title, style="H2.TLabel").pack(side="left")
        for text, cmd in (tools or []):
            ttk.Button(head, text=text, command=cmd, style="Mini.TButton"
                       ).pack(side="right", padx=(6, 0))
        outer.subtitle = None
        if subtitle:
            lbl = ttk.Label(head, text=subtitle, style="CardDim.TLabel")
            lbl.pack(side="right", padx=(6, 0))
            outer.subtitle = lbl
    return outer, outer


def kpi_tile(parent, label, value="0", unit="", color=None, sub=""):
    """紧凑 KPI 磁贴（一行能排 10 个）：左侧色条 + 标签 + 大数字 + 单位 + 副标题。

    返回 dict，交给 set_tile() 更新；不返回 Tk 控件是为了让调用方只认语义字段。
    """
    color = color or ACCENT
    outer = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                     highlightbackground=BORDER)
    accent = tk.Frame(outer, bg=color, width=3)
    accent.pack(side="left", fill="y")
    inner = tk.Frame(outer, bg=PANEL, padx=9, pady=5)
    inner.pack(side="left", fill="both", expand=True)
    cap = tk.Label(inner, text=label, bg=PANEL, fg=FG_DIM, font=FONT_SMALL,
                   anchor="w")
    cap.pack(anchor="w")
    row = tk.Frame(inner, bg=PANEL)
    row.pack(anchor="w")
    num = tk.Label(row, text=str(value), bg=PANEL, fg=color, font=FONT_KPI_S,
                   anchor="w")
    num.pack(side="left")
    unit_lbl = tk.Label(row, text=(" " + unit) if unit else "", bg=PANEL,
                        fg=FG_MUTE, font=FONT_TINY, anchor="w")
    unit_lbl.pack(side="left", pady=(7, 0))
    sub_lbl = tk.Label(inner, text=sub or " ", bg=PANEL, fg=FG_MUTE,
                       font=FONT_TINY, anchor="w")
    sub_lbl.pack(anchor="w")
    return {"outer": outer, "accent": accent, "cap": cap, "value": num,
            "unit": unit_lbl, "sub": sub_lbl, "color": color}


def set_tile(tile, value=None, color=None, sub=None, unit=None, sub_color=None):
    """更新 KPI 磁贴（只改文本/颜色，不重建控件，避免闪烁）。"""
    if not tile:
        return
    try:
        if value is not None:
            tile["value"].config(text=fmt_int(value))
        if color:
            tile["value"].config(fg=color)
            tile["accent"].config(bg=color)
            tile["color"] = color
        if sub is not None:
            tile["sub"].config(text=sub or " ")
        if sub_color:
            tile["sub"].config(fg=sub_color)
        if unit is not None:
            tile["unit"].config(text=(" " + unit) if unit else "")
    except Exception:
        pass


def hline(parent, color=None):
    """一条细分隔线（用 Frame 画，比 Canvas 便宜）"""
    s = tk.Frame(parent, height=1, bg=color or BORDER)
    s.pack(fill="x", pady=PAD_S)
    return s


# ---------------------------------------------------------------- 图表
def _empty(canvas, text="暂无数据", w=None, h=None):
    w = w or int(canvas.winfo_width() or 100)
    h = h or int(canvas.winfo_height() or 60)
    canvas.create_text(w // 2, h // 2, text=text, fill=FG_MUTE, font=FONT_SMALL)


def bar_chart(canvas, data, color=None, colors=None, label_color=None,
              height=None, unit="", empty_text="暂无数据"):
    """在 Canvas 上画横向条形图。data = [(标签, 数值), ...]（无第三方依赖）

    返回最大值的标签（便于界面提示"最大项"）。
    """
    canvas.delete("all")
    size = _canvas_size(canvas)
    if size is None:
        return None
    w, h = size
    data = [(str(l), float(v or 0)) for l, v in (data or [])]
    if not data:
        _empty(canvas, empty_text, w, h)
        return None
    n = len(data)
    row_h = max(16, min(34, (h - 6) // max(1, n)))
    # 标签列按**实际像素宽度**给，别按字符数一刀切（中文会少一半）
    longest = max(data, key=lambda p: text_width(p[0]))[0]
    label_w = min(int(w * 0.5), max(56, text_width(longest) + 14))
    value_w = 56
    plot_w = max(20, w - label_w - value_w - 6)
    bar_max = max(1.0, max(v for _, v in data))
    # 竖向网格
    for i in range(1, 4):
        x = label_w + plot_w * i / 4.0
        canvas.create_line(x, 2, x, min(h, 4 + row_h * n), fill=GRID)
    top_label, top_val = None, -1.0
    for i, (lab, val) in enumerate(data):
        y = 3 + i * row_h
        bh = max(6, row_h - 8)
        c = (colors[i] if colors and i < len(colors) else (color or ACCENT2))
        canvas.create_text(label_w - 8, y + bh / 2.0, anchor="e",
                           text=truncate_px(lab, label_w - 14),
                           fill=label_color or FG_DIM, font=FONT_SMALL)
        bw = int(plot_w * (val / bar_max))
        if val > 0 and bw < 3:
            bw = 3
        canvas.create_rectangle(label_w, y, label_w + bw, y + bh,
                                fill=c, outline="")
        # 末端高亮块：让"进度感"更明确（同时不影响数值读取）
        if bw > 4:
            canvas.create_rectangle(label_w + bw - 3, y, label_w + bw, y + bh,
                                    fill=blend(c, "#ffffff", 0.35), outline="")
        canvas.create_text(w - 4, y + bh / 2.0, anchor="e",
                           text=fmt_int(val) + unit, fill=FG, font=FONT_SMALL)
        if val > top_val:
            top_label, top_val = lab, val
    return top_label


def donut_chart(canvas, data, center_title="总数", center_unit="",
                thickness=None, empty_text="暂无数据"):
    """环形占比图。data = [(标签, 数值, 颜色), ...]

    中心显示合计与标题，右侧图例显示"名称 / 数量 / 占比"。占比全部由真实数据
    算出；合计为 0 时画灰色底环并如实标注"暂无数据"，不画假的饼。
    """
    canvas.delete("all")
    size = _canvas_size(canvas)
    if size is None:
        return
    w, h = size
    items = []
    for row in (data or []):
        try:
            lab, val, col = row[0], float(row[1] or 0), row[2]
        except (IndexError, TypeError, ValueError):
            continue
        items.append((str(lab), max(0.0, val), col))
    total = sum(v for _, v, _ in items)
    legend_w = min(int(w * 0.5), 190) if w >= 260 else 0
    cx = (w - legend_w) / 2.0
    cy = h / 2.0
    r = max(18.0, min(cx - 10, h / 2.0 - 8))
    thick = int(thickness or max(9, min(r * 0.30, 20)))
    box = (cx - r, cy - r, cx + r, cy + r)

    # 底环（灰）——即使无数据也能看出"这里是个占比图"
    canvas.create_oval(*box, outline=GRID, width=thick)
    if total > 0:
        start = 90.0
        for _lab, val, col in items:
            if val <= 0:
                continue
            extent = -360.0 * (val / total)
            if extent <= -359.9:
                canvas.create_oval(*box, outline=col, width=thick)
            else:
                # 段间留 1.5° 缝（两端各让 0.75°），视觉上更"分段"
                gap = 1.5 if extent < -4 else 0.0
                seg = extent + gap
                canvas.create_arc(box[0], box[1], box[2], box[3],
                                  start=start + gap / 2.0,
                                  extent=seg if seg < 0 else extent,
                                  style="arc", outline=col, width=thick)
            start += extent
    txt = fmt_int(total) if total > 0 else "0"
    canvas.create_text(cx, cy - 6, text=txt, fill=FG if total else FG_MUTE,
                       font=FONT_DONUT)
    canvas.create_text(cx, cy + 15, text=center_title + (" " + center_unit if center_unit else ""),
                       fill=FG_DIM, font=FONT_TINY)

    if legend_w:
        lx = w - legend_w + 6
        rows = min(len(items), 6)
        ly = cy - rows * 22 / 2.0
        for i, (lab, val, col) in enumerate(items[:6]):
            y = ly + i * 22
            canvas.create_rectangle(lx, y + 6, lx + 9, y + 15, fill=col, outline="")
            canvas.create_text(lx + 16, y + 11, anchor="w",
                               text=_truncate(lab, 7), fill=FG_DIM, font=FONT_SMALL)
            canvas.create_text(lx + legend_w - 60, y + 11, anchor="e",
                               text=fmt_int(val), fill=FG, font=("Consolas", 10, "bold"))
            canvas.create_text(lx + legend_w - 6, y + 11, anchor="e",
                               text=fmt_pct(val, total), fill=FG_MUTE, font=FONT_TINY)


def area_line_chart(canvas, series, color=None, unit="", show_hover=True,
                    empty_text="暂无数据", zero_hint=""):
    """面积折线图（带好刻度、末点标注、悬停提示）。series = [(标签, 数值), ...]"""
    canvas.delete("all")
    size = _canvas_size(canvas)
    if size is None:
        return
    w, h = size
    pts = []
    for lab, v in (series or []):
        try:
            pts.append((str(lab), float(v or 0)))
        except (TypeError, ValueError):
            continue
    color = color or ACCENT
    if len(pts) < 2:
        _empty(canvas, "数据不足（至少 2 个点）" if not pts else empty_text, w, h)
        return
    pad_l, pad_r, pad_b, pad_t = 40, 12, 20, 12
    plot_w = max(10, w - pad_l - pad_r)
    plot_h = max(10, h - pad_t - pad_b)
    vmax = max(v for _, v in pts)
    ticks, top = nice_ticks(vmax, 4)
    if top <= 0:
        top = 1.0
    # 横向网格 + Y 轴标签
    for t in ticks:
        y = pad_t + plot_h * (1.0 - t / top)
        canvas.create_line(pad_l, y, w - pad_r, y, fill=GRID)
        canvas.create_text(pad_l - 6, y, anchor="e", text=("%g" % t),
                           fill=FG_MUTE, font=("Consolas", 8))
    xs = []
    for i, (_lab, v) in enumerate(pts):
        x = pad_l + plot_w * i / float(len(pts) - 1)
        y = pad_t + plot_h * (1.0 - min(v, top) / top)
        xs.append((x, y, _lab, v))
    # 面积填充（混色模拟半透明）
    poly = [(pad_l, pad_t + plot_h)]
    poly += [(x, y) for x, y, _, _ in xs]
    poly += [(xs[-1][0], pad_t + plot_h)]
    canvas.create_polygon([c for p in poly for c in p],
                          fill=blend(color, PANEL, 0.82), outline="")
    # 折线
    canvas.create_line([c for x, y, _, _ in xs for c in (x, y)],
                       fill=color, width=2, smooth=False)
    # X 轴标签：最多 6 个，均匀取样；靠得太近就跳过，避免糊成一团
    step = max(1, int(math.ceil(len(xs) / 6.0)))
    last_lab_x = -1e9
    for i, (x, _y, lab, _v) in enumerate(xs):
        if (i % step == 0 or i == len(xs) - 1) and (x - last_lab_x) >= 34:
            canvas.create_text(x, h - 7, text=lab, fill=FG_MUTE,
                               font=("Consolas", 8))
            last_lab_x = x
    # 全 0 时给一句如实说明，避免"空图"看起来像坏了
    if vmax <= 0 and zero_hint:
        canvas.create_text((pad_l + w - pad_r) / 2.0, pad_t + plot_h / 2.0,
                           text=zero_hint, fill=FG_MUTE, font=FONT_SMALL)
    # 末点高亮 + 数值
    lx, ly, llab, lval = xs[-1]
    canvas.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=color, outline="")
    canvas.create_text(min(lx, w - pad_r - 6), max(pad_t + 6, ly - 12),
                       anchor="e" if lx > w * 0.7 else "w",
                       text="%s%s" % (fmt_int(lval), unit), fill=color,
                       font=FONT_TINY)
    # 峰值标注
    mi = max(range(len(xs)), key=lambda i: xs[i][3])
    if xs[mi][3] > 0 and mi != len(xs) - 1:
        mx, my, mlab, mval = xs[mi]
        canvas.create_text(mx, max(pad_t + 6, my - 10), text="峰值 %s%s" % (fmt_int(mval), unit),
                           fill=WARN, font=FONT_TINY)
    if not show_hover:
        return
    # 悬停：竖线 + 气泡（重复绑定会覆盖，不会泄漏）
    def _hover(event):
        canvas.delete("hover")
        if not xs:
            return
        best = min(xs, key=lambda p: abs(p[0] - event.x))
        bx, by, blab, bval = best
        canvas.create_line(bx, pad_t, bx, pad_t + plot_h, fill=blend(color, BG, 0.5),
                           dash=(3, 3), tags="hover")
        canvas.create_oval(bx - 4, by - 4, bx + 4, by + 4, outline=color, width=2,
                           tags="hover")
        txt = "%s　%s%s" % (blab, fmt_int(bval), unit)
        tw = 8 * len(txt) + 12
        tx = min(max(bx - tw / 2.0, 2), max(2, w - tw - 2))
        ty = max(2, by - 34)
        canvas.create_rectangle(tx, ty, tx + tw, ty + 20, fill=PANEL2,
                                outline=BORDER, tags="hover")
        canvas.create_text(tx + 6, ty + 10, anchor="w", text=txt, fill=FG,
                           font=FONT_TINY, tags="hover")
    canvas.bind("<Motion>", _hover)
    canvas.bind("<Leave>", lambda _e: canvas.delete("hover"))


def line_chart(canvas, series, color=None, grid=True):
    """折线图（兼容旧接口；内部走 area_line_chart，视觉更完整）"""
    area_line_chart(canvas, series, color=color, show_hover=bool(grid))


def bar_chart_unicode():
    """返回一组可用于纯文本"柱状"的字符（无 GUI 时用，如自检输出）"""
    return "▁▂▃▄▅▆▇█"
