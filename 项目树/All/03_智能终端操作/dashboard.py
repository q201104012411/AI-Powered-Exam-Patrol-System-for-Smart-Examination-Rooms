# -*- coding: utf-8 -*-
r"""
03 智能终端 · 大数据驾驶舱页面

设计取向（对齐"大数据驾驶舱"）：
  · 顶部总览：系统标题 + 实时时钟 + 审计链状态 + 刷新/全屏
  · KPI 磁贴：**单行 10 个**紧凑卡片，大数字 + 单位 + 一句副标题（占比/超时/链路）
  · 图表区：告警分级占比（环形）、告警趋势（面积折线，可切 24 小时/7 天/12 小时）、
            告警类型 TOP（横向条形）—— 全部 Canvas 手绘，随窗口尺寸重绘
  · 明细区：最近告警（分级着色 + 超时行标底）、远端设备台账、全系统模块态势
  · 全屏：一键大屏（Esc 退出），大屏下 KPI 字号自动放大

实现约束与这一版的改动理由（都能对上原来真实存在的问题）：
  · **不再把内容撑出可视区**：改造前驾驶舱内容高 1037px、可视区只有 723px，
    于是"最近告警/远端台账/页脚"整块被静默裁掉。现在用 VScroll：装得下就按权重
    铺满，装不下就能滚，任何窗口尺寸都不丢内容。
  · **图表随尺寸重绘**：改造前只在刷新时画一次，窗口放大/进全屏后图元还停在旧坐标。
    现在每个画布绑 `<Configure>`（合并 120ms 防抖）后重画。
  · **跨模块聚合挪到后台线程**：改造前 `refresh_modules()` 在主线程里串起 8 个子进程
    （首轮约 1.8 秒），期间界面是**假死**的。现在子线程跑，主线程只回填表格。
  · **KPI 用 SQL 聚合**：改造前 `list_alerts(limit=2000)` 后再数，超过 2000 条就偏小、
    且每次都要把 10 万条审计读进内存。现在走 `storage.stats()` 的 COUNT/GROUP BY。
  · **审计链全量校验降频**：哈希链校验是 O(全表)，5 秒一次没必要；改为 20 秒 TTL，
    条数每次刷新都取真实值，校验结论过期前沿用。
  · 数据全部来自 03 自己的库，**不造假数**：取不到就如实显示"暂无数据/不可用"。
  · 页面按需刷新（1 秒走时钟、5 秒刷数据、30 秒刷模块），避免整页重建导致闪烁。
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk

import config
import models
import theme as T

#: 告警状态 -> 中文（03 的状态常量在 models 里，不在 config）
_STATUS_TEXT = {
    models.ST_PENDING: "待处置",
    models.ST_ACKED: "已确认",
    models.ST_REVIEWED: "已复核",
    models.ST_CLOSED: "已归档",
}

#: 告警状态 -> 配色
_STATUS_COLOR = {
    models.ST_PENDING: T.WARN,
    models.ST_ACKED: T.ACCENT2,
    models.ST_REVIEWED: T.OK,
    models.ST_CLOSED: T.FG_MUTE,
}

#: 刷新节奏（秒）
DATA_INTERVAL = 5.0        # 本机数据
MODULE_INTERVAL = 30.0     # 跨模块聚合（起子进程，代价高）
CHAIN_TTL = 20.0           # 审计链全量校验间隔
MODULE_TTL = 30.0          # 模块聚合结果缓存
MODULE_TIMEOUT = 8.0       # 单个模块子进程超时
DRAW_DEBOUNCE_MS = 120     # 窗口缩放后合并重绘的延迟

#: 趋势图可切换的时间窗：(key, 按钮文字, 取数来源)
SPANS = (("24h", "近 24 小时", "hourly"),
         ("7d", "近 7 天", "daily"),
         ("12h", "近 12 小时", "hourly"))


def status_text(status):
    return _STATUS_TEXT.get(status, status or "—")


def status_color(status):
    return _STATUS_COLOR.get(status, T.FG_MUTE)


def code_text(code):
    """告警码 -> 中文；未登记的新码如实显示原码，不硬编一个假名字。"""
    code = str(code or "—")
    return config.ALERT_CODE_TEXT.get(code) or code


def time_buckets(alerts, buckets=24, unit="hour"):
    """把告警按"最近 N 个时间桶"分桶，返回 [(标签, 数量), ...]（旧→新）。

    unit="hour"：桶对齐到整点，标签 HH:00；unit="day"：桶对齐到本地 0 点，标签 MM-DD。
    这是**没有 storage.stats() 时的兜底路径**（正常路径走 SQL 聚合，见 _collect）。
    """
    unit = "day" if str(unit).startswith("d") else "hour"
    span = 3600 if unit == "hour" else 86400
    now = time.time()
    anchor = float(int(now // span) * span)
    if unit == "day":
        lt = time.localtime(now)
        try:
            anchor = float(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                        0, 0, 0, 0, 0, -1)))
        except (OverflowError, ValueError):
            pass
    n = max(1, int(buckets))
    counts = [0] * n
    start = anchor - (n - 1) * span          # 最旧那个桶的起点
    for a in alerts or []:
        try:
            ep = float(getattr(a, "created_epoch", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ep < start:                       # 早于窗口，不计
            continue
        # 用"相对窗口起点"的整数除法定桶：整点/整日对齐后，
        # 上一个桶的事件**不能**算进当前桶（用 anchor-ep 直接除会犯这个错）。
        idx = int((ep - start) // span)
        if 0 <= idx < n:
            counts[idx] += 1
    fmt = "%m-%d" if unit == "day" else "%H:00"
    return [(time.strftime(fmt, time.localtime(start + i * span)), counts[i])
            for i in range(n)]


def hour_buckets(alerts, hours=12):
    """兼容旧名字：按小时分桶（旧→新）。"""
    return time_buckets(alerts, hours, "hour")


def level_series(data):
    """告警分级占比图的数据：[(名称, 数量, 颜色), ...]（03 只有红/橙两级）"""
    by = (data or {}).get("by_level") or {}
    return [("红色预警", int(by.get(config.LEVEL_RED, 0) or 0), T.ALARM),
            ("橙色预警", int(by.get(config.LEVEL_ORANGE, 0) or 0), T.WARN)]


def type_series(data, n=6):
    """告警类型 TOP N：[(中文类型, 数量), ...]（降序，**不把剩余合并成"其它"**）。

    为什么不合并：把尾部几十条并成一项"其它"后，它往往会比任何真实类型都长，
    条形图上直接压过其它项（实测 30 条 vs 12 条），看着像"某一种类型爆了"。
    TOP 榜就该只显示前 N 个真实类型，剩余的用卡片副标题另行说明。
    """
    rows = []
    for code, val in ((data or {}).get("by_code") or []):
        try:
            rows.append((code_text(code), int(val or 0)))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda p: (-p[1], p[0]))
    return rows[:max(1, int(n))]


def type_rest(data, shown):
    """没进 TOP 榜的那些类型还剩多少条（给卡片副标题用）。"""
    total = sum(int(v or 0) for _c, v in ((data or {}).get("by_code") or []))
    return max(0, total - sum(v for _n, v in shown or []))


def fmt_ms(ms):
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return "—"
    return ("%.0f ms" % ms) if ms < 1000 else ("%.1f s" % (ms / 1000.0))


def short_path(path, keep=3):
    """把长路径压成"C:…\\a\\b\\c"（页脚一行放得下）。纯展示，不改变真实路径。"""
    s = str(path or "")
    if not s:
        return "—"
    try:
        parts = [p for p in s.replace("/", "\\").split("\\") if p]
        if len(parts) <= keep:
            return s
        tail = "\\".join(parts[-keep:])
        drive = parts[0] if parts[0].endswith(":") else ""
        return ("%s…\\%s" % (drive, tail)) if drive else ("…\\%s" % tail)
    except Exception:
        return s


class CockpitTab(object):
    """驾驶舱页：由 03 主界面装配，也可传入任意 store 复用（便于自检）。"""

    def __init__(self, parent, app):
        self.app = app                      # TerminalApp（取 store/session/root）
        self.parent = parent
        self.store = app.store
        self.session = getattr(app, "session", None)

        self._after = None                  # 1 秒时钟
        self._draw_after = None             # 缩放后合并重绘
        self._stopped = False
        self._fullscreen = False
        self._span = SPANS[0][0]
        self._data = {}
        self.tiles = {}

        # 跨模块聚合（子进程 → 后台线程）
        self._agg = None
        self._mod_thread = None
        self._mod_pending = None            # (rows, 完成时间) 由子线程写、主线程读
        self._mod_rows = []
        self._last_mod = 0.0
        self._mod_loaded_at = 0.0

        # 审计链校验结果（带 TTL）
        self._chain = (True, "", 0)
        self._chain_at = 0.0

        self._last_data = 0.0
        self._n = 0
        self._build()
        self.refresh(force=True)
        self.refresh_modules()
        self._tick()

    # ================================================================ 布局
    def _build(self):
        shell = ttk.Frame(self.parent, style="TFrame")
        shell.pack(fill="both", expand=True)
        # 滚动容器：装得下就铺满，装不下就能滚（避免像 pack 那样把明细整块裁掉）
        self.scroll = T.VScroll(shell)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=3, minsize=205)     # 图表区
        body.rowconfigure(3, weight=2, minsize=205)     # 明细区

        self._build_header(body)
        self._build_kpis(body)
        self._build_charts(body)
        self._build_tables(body)

        self.lbl_foot = ttk.Label(body, text="", style="TLabel", foreground=T.FG_DIM)
        self.lbl_foot.grid(row=4, column=0, sticky="w", pady=(4, 0))

    # ---- 顶部标题条（整条画在 Canvas 上：渐变底/发光标题/胶囊按钮/警戒线）----
    def _build_header(self, body):
        self.banner = T.Banner(
            body, "智能终端 · 运行态势驾驶舱",
            "告警分级 · 复核留痕 · 断网补传 · 权限最小化",
            buttons=(("refresh", "🔄 刷新", lambda: self.refresh(force=True)),
                     ("full", "⛶ 全屏", self.toggle_fullscreen)))
        self.banner.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    # ---- KPI 磁贴（单行 10 个）----
    def _build_kpis(self, body):
        row = ttk.Frame(body, style="TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        specs = (("total", "告警总数", "条", T.ACCENT),
                 ("red", "红色预警", "条", T.ALARM),
                 ("orange", "橙色预警", "条", T.WARN),
                 ("pending", "待处置", "条", T.ACCENT3),
                 ("review", "已复核", "条", T.OK),
                 ("unsync", "待补传", "条", T.ACCENT2),
                 ("remote", "远端记录", "条", T.ACCENT2),
                 ("devices", "接入设备", "台", T.ACCENT),
                 ("fire", "火警", "起", T.ALARM),
                 ("audit", "审计条数", "条", T.OK))
        last = len(specs) - 1
        for i, (key, label, unit, color) in enumerate(specs):
            tile = T.kpi_tile(row, label, "0", unit, color)
            tile["cv"].pack(side="left", fill="both", expand=True,
                            padx=(0, 0 if i == last else 6))
            self.tiles[key] = tile

    # ---- 图表区（3 张）----
    def _build_charts(self, body):
        row = ttk.Frame(body, style="TFrame")
        row.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        row.columnconfigure(0, weight=11)
        row.columnconfigure(1, weight=17)
        row.columnconfigure(2, weight=13)
        row.rowconfigure(0, weight=1)

        c1, _ = T.card(row, "告警分级占比", manager="grid", row=0, column=0,
                       sticky="nsew", padx=(0, 6), accent=T.ALARM)
        self.cv_level = tk.Canvas(c1, bg=T.PANEL, highlightthickness=0, height=120)
        self.cv_level.pack(fill="both", expand=True)

        c2, _ = T.card(row, "告警趋势", manager="grid", row=0, column=1,
                       sticky="nsew", padx=(0, 6), accent=T.ACCENT)
        self.btn_span = ttk.Button(c2.head, text=SPANS[0][1],
                                   command=self._cycle_span, style="Mini.TButton")
        self.btn_span.pack(side="right", padx=(6, 0))
        self.cv_trend = tk.Canvas(c2, bg=T.PANEL, highlightthickness=0, height=120)
        self.cv_trend.pack(fill="both", expand=True)

        c3, _ = T.card(row, "告警类型 TOP", manager="grid", row=0, column=2,
                       sticky="nsew", accent=T.ACCENT3,
                       subtitle="按告警码统计")
        self.lbl_type = c3.subtitle
        self.cv_type = tk.Canvas(c3, bg=T.PANEL, highlightthickness=0, height=120)
        self.cv_type.pack(fill="both", expand=True)

        for cv in (self.cv_level, self.cv_trend, self.cv_type):
            cv.bind("<Configure>", self._on_chart_resize)

    # ---- 明细区（3 张表）----
    def _build_tables(self, body):
        row = ttk.Frame(body, style="TFrame")
        row.grid(row=3, column=0, sticky="nsew")
        row.columnconfigure(0, weight=12)
        row.columnconfigure(1, weight=8)
        row.columnconfigure(2, weight=20)
        row.rowconfigure(0, weight=1)

        # 最近告警
        c1, _ = T.card(row, "最近告警", manager="grid", row=0, column=0,
                       sticky="nsew", padx=(0, 6), accent=T.WARN)
        cols = ("time", "level", "code", "room", "status")
        self.tree_alerts = ttk.Treeview(c1, columns=cols, show="headings", height=5)
        for c, txt, w, stretch in (("time", "时间", 88, False),
                                   ("level", "级别", 54, False),
                                   ("code", "类型", 96, True),
                                   ("room", "考场", 44, False),
                                   ("status", "状态", 50, False)):
            self.tree_alerts.heading(c, text=txt)
            self.tree_alerts.column(c, width=w, minwidth=w, anchor="w",
                                    stretch=stretch)
        self.tree_alerts.tag_configure("red", foreground=T.ALARM, font=T.FONT_SMALL)
        self.tree_alerts.tag_configure("orange", foreground=T.WARN, font=T.FONT_SMALL)
        self.tree_alerts.tag_configure("muted", foreground=T.FG_DIM, font=T.FONT_SMALL)
        self.tree_alerts.tag_configure("overdue", background="#2a1220")
        self.tree_alerts.pack(fill="both", expand=True)

        # 远端设备台账
        c2, _ = T.card(row, "远端设备", manager="grid", row=0, column=1,
                       sticky="nsew", padx=(0, 6), accent=T.ACCENT2)
        cols2 = ("dev", "records", "last")
        self.tree_devs = ttk.Treeview(c2, columns=cols2, show="headings", height=5)
        for c, txt, w, stretch in (("dev", "设备", 92, True),
                                   ("records", "条数", 38, False),
                                   ("last", "最后活跃", 74, False)):
            self.tree_devs.heading(c, text=txt)
            self.tree_devs.column(c, width=w, minwidth=w, anchor="w", stretch=stretch)
        self.tree_devs.tag_configure("row", font=T.FONT_SMALL)
        self.tree_devs.tag_configure("dim", foreground=T.FG_MUTE, font=T.FONT_SMALL)
        self.tree_devs.pack(fill="both", expand=True)

        # 全系统模块态势（子进程聚合，只读）
        c3, _ = T.card(row, "全系统模块态势", manager="grid", row=0, column=2,
                       sticky="nsew", accent=T.OK, subtitle="后台聚合中…")
        self.lbl_modules = c3.subtitle
        self.tree_modules = ttk.Treeview(c3, columns=("mod", "state", "brief"),
                                         show="headings", height=5)
        for c, txt, w, stretch in (("mod", "模块", 158, False),
                                   ("state", "状态", 44, False),
                                   ("brief", "关键指标", 295, True)):
            self.tree_modules.heading(c, text=txt)
            self.tree_modules.column(c, width=w, minwidth=w, anchor="w", stretch=stretch)
        self.tree_modules.tag_configure("ok", foreground=T.OK, font=T.FONT_SMALL)
        self.tree_modules.tag_configure("bad", foreground=T.FG_MUTE, font=T.FONT_SMALL)
        self.tree_modules.pack(fill="both", expand=True)

    # ================================================================ 定时
    def _tick(self):
        if self._stopped:
            return
        # 先排下一次，再干活：任何一步出异常都不会把定时器弄死
        # （这里踩过坑：时钟格式化写错一个 %，except 里 return 之后整个驾驶舱就不再刷新了）
        try:
            self._after = self.parent.after(1000, self._tick)
        except Exception:
            return
        try:
            now = time.localtime()
            self.banner.set_clock(
                time.strftime("%H:%M:%S", now),
                time.strftime("%Y-%m-%d", now) + "　周" + "日一二三四五六"[now.tm_wday])
        except Exception:
            pass
        # 后台聚合结果回填（子线程只写属性，Tk 控件只在主线程动）
        pending = self._mod_pending
        if pending is not None:
            self._mod_pending = None
            try:
                self._apply_modules(*pending)
            except Exception:
                pass
        self._n += 1
        if not self._is_visible():
            return                      # 页签不可见时不刷，切回来立刻补刷
        now = time.time()
        if now - self._last_data >= DATA_INTERVAL:
            self.refresh()
        busy = self._mod_thread is not None and self._mod_thread.is_alive()
        if not busy and now - self._last_mod >= MODULE_INTERVAL:
            self.refresh_modules()

    def _is_visible(self):
        """驾驶舱是否处于选中的页签（不可见时省掉查库/绘图）。"""
        nb = getattr(self.app, "nb", None)
        if nb is None:
            return True
        try:
            return str(nb.select()) == str(self.parent)
        except Exception:
            return True

    def stop(self):
        """退出前取消定时器（否则销毁窗口时 Tcl 报错）。"""
        self._stopped = True
        for attr in ("_after", "_draw_after"):
            aid = getattr(self, attr, None)
            if aid:
                try:
                    self.parent.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.app.root.unbind("<Escape>")
        except Exception:
            pass

    # ================================================================ 取数
    def _collect(self):
        """一次刷新要的全部数据；优先 SQL 聚合，失败才退回明细统计。"""
        t0 = time.time()
        data = None
        try:
            data = self.store.stats(hours=24, days=7, recent=60)
        except TypeError:                   # 老签名（没有这些参数）
            try:
                data = self.store.stats()
            except Exception:
                data = None
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = self._collect_fallback()
        else:
            data.setdefault("recent", [])
            data.setdefault("by_code", [])
            data.setdefault("hourly", [])
            data.setdefault("daily", [])
        try:
            data["remote"] = self.store.remote_summary()
        except Exception:
            data["remote"] = {}
        try:
            data["devices"] = self.store.remote_devices()
        except Exception:
            data["devices"] = []
        data["cost_ms"] = (time.time() - t0) * 1000.0
        return data

    def _collect_fallback(self):
        """兜底：store 没有 stats() 时用明细现算（与改造前口径一致）。"""
        try:
            alerts = self.store.list_alerts(limit=5000)
        except Exception:
            alerts = []
        try:
            reviews = self.store.list_reviews(limit=1)
        except Exception:
            reviews = []
        by_level, by_code, by_status = {}, {}, {}
        for a in alerts:
            by_level[a.level] = by_level.get(a.level, 0) + 1
            by_code[a.code] = by_code.get(a.code, 0) + 1
            by_status[a.status] = by_status.get(a.status, 0) + 1
        now = time.time()
        return {
            "total": len(alerts),
            "by_level": by_level,
            "by_status": by_status,
            "by_code": sorted(by_code.items(), key=lambda p: -p[1]),
            "reviews": len(reviews),
            "unsynced": len([a for a in alerts if not a.synced]),
            "overdue": len([a for a in alerts
                            if a.status == models.ST_PENDING and a.overdue()]),
            "last_hour": sum(1 for a in alerts
                             if float(a.created_epoch or 0) >= now - 3600),
            "audits": 0,
            "hourly": [(0, 0)],
            "daily": [(0, 0)],
            "recent": alerts[:60],
        }

    def _chain_state(self):
        """审计链校验（O(全表)，所以按 TTL 复用结论）。"""
        now = time.time()
        if self._chain_at and now - self._chain_at < CHAIN_TTL:
            return self._chain
        try:
            ok, why, cnt = self.store.verify_chain()
        except Exception as e:
            ok, why, cnt = False, "校验失败：%s" % str(e)[:60], 0
        self._chain = (bool(ok), why, int(cnt or 0))
        self._chain_at = now
        return self._chain

    # ================================================================ 刷新
    def refresh(self, force=False):
        """刷新 KPI / 图表 / 明细（图表重画也在内，不重建控件）。"""
        if self._stopped:
            return
        now = time.time()
        if not force and self._last_data and now - self._last_data < 1.0:
            return
        self._last_data = now
        data = self._collect()
        self._data = data
        try:
            self._update_kpis(data)
        except Exception:
            pass
        try:
            self._update_tables(data)
        except Exception:
            pass
        self._draw_charts()
        try:
            self._update_footer(data)
        except Exception:
            pass

    def _update_kpis(self, d):
        by = d.get("by_level") or {}
        total = int(d.get("total") or 0)
        red = int(by.get(config.LEVEL_RED, 0) or 0)
        orange = int(by.get(config.LEVEL_ORANGE, 0) or 0)
        pending = int((d.get("by_status") or {}).get(models.ST_PENDING, 0) or 0)
        overdue = int(d.get("overdue") or 0)
        reviews = int(d.get("reviews") or 0)
        unsync = int(d.get("unsynced") or 0)
        rem = d.get("remote") or {}
        codes = dict(d.get("by_code") or [])
        fire = int(codes.get("FIRE_ALARM", 0) or 0)
        smoke = int(codes.get("FIRE_SMOKE", 0) or 0)
        ok_chain, _why, chain_n = self._chain_state()
        audits = int(d.get("audits") or chain_n or 0)

        set_tile = T.set_tile
        share = (lambda n: (float(n) / total) if total else 0.0)
        set_tile(self.tiles["total"], total, T.ACCENT,
                 sub="近 1 小时 +%d" % int(d.get("last_hour") or 0))
        set_tile(self.tiles["red"], red, T.ALARM if red else T.FG_MUTE,
                 sub="占比 %s" % T.fmt_pct(red, total), ratio=share(red))
        set_tile(self.tiles["orange"], orange, T.WARN if orange else T.FG_MUTE,
                 sub="占比 %s" % T.fmt_pct(orange, total), ratio=share(orange))
        set_tile(self.tiles["pending"], pending,
                 T.ACCENT3 if pending else T.FG_MUTE,
                 sub=("超时 %d 条" % overdue) if overdue else "无超时",
                 sub_color=T.ALARM if overdue else T.FG_MUTE, ratio=share(pending))
        set_tile(self.tiles["review"], reviews, T.OK,
                 sub="覆盖率 %s" % T.fmt_pct(reviews, total), ratio=share(reviews))
        set_tile(self.tiles["unsync"], unsync,
                 T.ACCENT2 if unsync else T.FG_MUTE,
                 sub="断网补传队列")
        set_tile(self.tiles["remote"], int(rem.get("records") or 0), T.ACCENT2,
                 sub="经局域网接收")
        devices = int(rem.get("devices") or 0)
        set_tile(self.tiles["devices"], devices, T.ACCENT,
                 sub=(("最近接收 " + (rem.get("last_received") or "")[11:16])
                      if devices and rem.get("last_received") else "无接入设备"))
        set_tile(self.tiles["fire"], fire, T.ALARM if fire else T.FG_MUTE,
                 sub="烟雾 %d 起" % smoke,
                 sub_color=T.WARN if smoke else T.FG_MUTE)
        set_tile(self.tiles["audit"], audits, T.OK if ok_chain else T.ALARM,
                 sub="链完整" if ok_chain else "链异常",
                 sub_color=T.OK if ok_chain else T.ALARM)

        # 顶部"警戒线"：有红色预警时整条变红（大屏上一眼看到"现在有事"）
        self._red_now = red
        self._paint_stripe()
        try:
            self.banner.set_chain("审计链 %s · %s 条"
                                  % ("完整" if ok_chain else "异常", T.fmt_int(audits)),
                                  ok=ok_chain)
        except Exception:
            pass

    def _paint_stripe(self, dim=False):
        """顶栏警戒线：有红色预警=红，否则=暗描边。"""
        banner = getattr(self, "banner", None)
        if banner is None:
            return
        try:
            banner.set_alert(getattr(self, "_red_now", 0) > 0)
        except Exception:
            pass

    def _update_tables(self, d):
        # ---- 最近告警 ----
        rows = list(d.get("recent") or [])[:60]
        tree = self.tree_alerts
        for iid in tree.get_children():
            tree.delete(iid)
        for a in rows:
            tags = []
            if a.level == config.LEVEL_RED:
                tags.append("red")
            elif a.level == config.LEVEL_ORANGE:
                tags.append("orange")
            else:
                tags.append("muted")
            try:
                overdue = (a.status == models.ST_PENDING and a.overdue())
            except Exception:
                overdue = False
            if overdue:
                tags.append("overdue")
            tree.insert("", "end", values=(
                (a.created_at or "")[5:16],
                config.LEVEL_META.get(a.level, {}).get("name", a.level),
                code_text(a.code),
                a.room_id or "—",
                status_text(a.status)),
                tags=tuple(tags))

        # ---- 远端设备台账 ----
        devs = list(d.get("devices") or [])
        tree = self.tree_devs
        for iid in tree.get_children():
            tree.delete(iid)
        if not devs:
            tree.insert("", "end", values=("（暂无设备接入）", "", ""), tags=("dim",))
        for dev in devs[:20]:
            name = dev.get("device_name") or ""
            label = dev.get("device_id", "") or "?"
            # 列宽只有 ~92px：设备名放不下就别硬拼，宁可只显示编码（否则两个都看不清）
            if name and name != label and T.text_width("%s·%s" % (label, name)) <= 88:
                label = "%s·%s" % (label, name)
            tree.insert("", "end", values=(
                label,
                dev.get("records", 0),
                (dev.get("last_seen", "") or "")[5:16]), tags=("row",))

    def _update_footer(self, d):
        rem = d.get("remote") or {}
        who = getattr(self.session, "display_name", None) or "—"
        role = config.ROLE_TEXT.get(getattr(self.session, "role", ""), "—")
        mode = "—"
        try:
            import runmode
            mode = runmode.label(runmode.get(self.store))
        except Exception:
            pass
        try:
            self.lbl_foot.config(
                text="数据库 %s　｜　%s（%s）　｜　运行模式 %s　｜　远端最近接收 %s　｜　"
                     "刷新 %s（每 %d 秒自动）　｜　%s"
                     % (short_path(config.DB_PATH), who, role, mode,
                        (rem.get("last_received") or "—")[:19],
                        fmt_ms(d.get("cost_ms")), int(DATA_INTERVAL),
                        time.strftime("%H:%M:%S")))
        except Exception:
            pass

    # ================================================================ 绘图
    def _on_chart_resize(self, _event=None):
        """窗口/大屏尺寸变化后合并重绘（避免每一像素都重画）。"""
        if self._stopped or self._draw_after:
            return
        try:
            self._draw_after = self.parent.after(DRAW_DEBOUNCE_MS, self._do_draw)
        except Exception:
            self._draw_after = None

    def _do_draw(self):
        self._draw_after = None
        if self._stopped:
            return
        self._draw_charts()

    def _trend_series(self, d):
        _key, _label, src = next((s for s in SPANS if s[0] == self._span), SPANS[0])
        rows = list(d.get(src) or [])
        if self._span == "12h":
            rows = rows[-12:]
        fmt = "%m-%d" if src == "daily" else "%H:00"
        return [(time.strftime(fmt, time.localtime(ts)), n) for ts, n in rows
                if ts]

    def _span_label(self):
        return next((s[1] for s in SPANS if s[0] == self._span), SPANS[0][1])

    def _draw_charts(self):
        d = self._data or {}
        try:
            T.donut_chart(self.cv_level, level_series(d),
                          center_title="告警总数", center_unit="条",
                          empty_text="暂无告警")
        except tk.TclError:
            return
        except Exception:
            pass
        try:
            T.area_line_chart(self.cv_trend, self._trend_series(d),
                              color=T.ACCENT, unit=" 条", show_hover=True,
                              zero_hint="%s无新告警" % self._span_label())
        except tk.TclError:
            return
        except Exception:
            pass
        try:
            types = type_series(d, 6)
            T.bar_chart(self.cv_type, types, unit=" 条",
                        colors=[T.SERIES[i % len(T.SERIES)] for i in range(len(types))],
                        empty_text="暂无告警记录")
            # 没进 TOP 的类型不画成"其它"条（会压过真实类型），改用副标题说明
            rest = type_rest(d, types)
            if self.lbl_type is not None:
                self.lbl_type.config(text=("另有 %d 条" % rest) if rest
                                     else "按告警码统计")
        except tk.TclError:
            return
        except Exception:
            pass

    def _cycle_span(self):
        keys = [s[0] for s in SPANS]
        i = keys.index(self._span) if self._span in keys else 0
        self._span = keys[(i + 1) % len(keys)]
        label = next(s[1] for s in SPANS if s[0] == self._span)
        try:
            self.btn_span.config(text=label)
        except Exception:
            pass
        self._draw_charts()

    # ================================================================ 模块聚合
    def refresh_modules(self, force=False):
        """后台线程聚合跨模块态势。

        改造前在主线程里顺序调用 8 个子进程（首轮 ≈1.8 秒），界面在此期间假死；
        现在子线程负责取数、主线程只在 _tick 里回填表格，切页签也不卡。
        """
        if self._stopped:
            return
        th = self._mod_thread
        if th is not None and th.is_alive():
            return
        self._last_mod = time.time()
        try:
            if self.lbl_modules is not None:
                self.lbl_modules.config(text="聚合中…")
        except Exception:
            pass
        self._mod_thread = threading.Thread(target=self._module_worker,
                                            args=(bool(force),),
                                            name="cockpit-modules", daemon=True)
        self._mod_thread.start()

    def _module_worker(self, force):
        """子线程：只做取数，绝不碰 Tk 控件。"""
        try:
            import remote_status
            agg = self._agg
            if agg is None:
                agg = remote_status.ModuleStatus(ttl=MODULE_TTL, timeout=MODULE_TIMEOUT)
                self._agg = agg
            data = agg.all(force=force)
            rows = remote_status.ModuleStatus.brief(data)
            self._mod_pending = (rows, time.time())
        except Exception:
            self._mod_pending = ([], time.time())

    def _apply_modules(self, rows, done_at):
        tree = getattr(self, "tree_modules", None)
        if tree is None:
            return
        rows = list(rows or [])
        self._mod_rows = rows
        self._mod_loaded_at = done_at or time.time()
        for iid in tree.get_children():
            tree.delete(iid)
        if not rows:
            tree.insert("", "end", values=(
                "（模块聚合不可用）", "—", "remote_status 未就绪或全部超时"),
                tags=("bad",))
        for name, text, ok in rows:
            tree.insert("", "end",
                        values=(name, "可用" if ok else "不可用", text),
                        tags=("ok" if ok else "bad",))
        ok_n = sum(1 for _n, _t, ok in rows if ok)
        try:
            if self.lbl_modules is not None:
                self.lbl_modules.config(
                    text="%d/%d 可用 · %s" % (ok_n, len(rows),
                                              time.strftime("%H:%M:%S",
                                                            time.localtime(self._mod_loaded_at))))
        except Exception:
            pass

    # ================================================================ 全屏
    def toggle_fullscreen(self, on=None):
        """一键大屏（考场大屏展示用）；Esc 退出。"""
        root = self.app.root
        self._fullscreen = (not self._fullscreen) if on is None else bool(on)
        try:
            root.attributes("-fullscreen", self._fullscreen)
        except Exception:
            try:
                root.state("zoomed" if self._fullscreen else "normal")
            except Exception:
                pass
        try:
            if self._fullscreen:
                root.bind("<Escape>", self._on_esc)
                self.banner.set_button_text("full", "⤢ 退出全屏")
            else:
                root.unbind("<Escape>")
                self.banner.set_button_text("full", "⛶ 全屏")
        except Exception:
            pass
        self._apply_density()
        self._set_app_chrome(not self._fullscreen)
        self._on_chart_resize()
        self.refresh(force=True)

    def _on_esc(self, _event=None):
        if self._fullscreen:
            self.toggle_fullscreen(False)
        return "break"

    def _apply_density(self):
        """大屏下把 KPI 数字放大一档（离得远也读得清）。"""
        for tile in self.tiles.values():
            try:
                T.set_tile_big(tile, self._fullscreen)
            except Exception:
                pass
        self.banner.redraw()
        self._paint_stripe()

    def _set_app_chrome(self, visible):
        """大屏模式下收起应用顶栏与 AI 面板（页签保留，好切页）；退出时放回。

        两个坑（都实测过）：
          · 判断"要不要收"不能用 winfo_ismapped()：AI 面板在 1180×820 下本来就被
            pack 挤出可视区（未映射），一进全屏反而露出来 —— 所以收的时候
            无条件 pack_forget()，放回的时候无条件重新 pack；
          · 恢复必须用 pack(before=...) 指明相对位置，否则会被塞到最下面。
        """
        app = self.app
        bar = getattr(app, "top_bar", None)
        nb = getattr(app, "nb", None)
        panel = getattr(app, "ai_panel", None)
        status = getattr(app, "status", None)
        try:
            if bar is not None and nb is not None:
                if visible:
                    bar.pack(before=nb, fill="x")
                else:
                    bar.pack_forget()
        except Exception:
            pass
        try:
            if panel is not None:
                if visible:
                    kw = {"fill": "x", "padx": 10, "pady": (0, 6)}
                    if status is not None:
                        panel.pack(before=status, **kw)
                    else:
                        panel.pack(**kw)
                else:
                    panel.pack_forget()
        except Exception:
            pass
