# -*- coding: utf-8 -*-
r"""
03 智能终端 · 大数据驾驶舱页面

设计取向（对齐"大数据驾驶舱"）：
  · 顶部总览：系统标题 + 实时时钟 + 审计链状态 + 在线/待办概览
  · KPI 卡片区：大数字 + 单位 + 语义色（告警总数/红/橙/复核/远端记录/待补传）
  · 图表区：告警分级分布（横向条形）、近 12 小时告警趋势（折线）、
            远端记录来源设备（条形）
  · 明细区：最近告警（带分级色）、远端设备台账
  · 全屏切换：一键进入大屏（Esc 返回）

实现约束：
  · 只用 tkinter/ttk 自带能力，不引入 matplotlib 等第三方依赖（考场机器装依赖麻烦）；
  · 数据全部来自 03 自己的库（storage），不造假数：取不到就如实显示"暂无数据"；
  · 页面按需刷新（1 秒定时器），避免整页重建导致闪烁。
"""
from __future__ import annotations

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


def _status_text(status):
    return _STATUS_TEXT.get(status, status or "—")


def hour_buckets(alerts, hours=12):
    """把告警按"最近 N 小时"分桶，返回 [(标签, 数量), ...]（旧→新）"""
    now = time.time()
    buckets = [0] * hours
    labels = []
    for i in range(hours):
        ts = now - (hours - 1 - i) * 3600
        labels.append(time.strftime("%H", time.localtime(ts)))
    for a in alerts:
        try:
            ep = float(getattr(a, "created_epoch", 0) or 0)
        except Exception:
            continue
        delta = now - ep
        if 0 <= delta < hours * 3600:
            idx = hours - 1 - int(delta // 3600)
            if 0 <= idx < hours:
                buckets[idx] += 1
    return list(zip(labels, buckets))


class CockpitTab(object):
    """驾驶舱页：由 03 主界面装配，也可传入任意 store 复用（便于自检）。"""

    def __init__(self, parent, app):
        self.app = app                      # TerminalApp（取 store/session/root）
        self.parent = parent
        self.store = app.store
        self.session = getattr(app, "session", None)
        self._after = None
        self._fullscreen = False
        self._kpi = {}
        self._build()

    # ------------------------------------------------------------ 布局
    def _build(self):
        f = self.parent
        # ---- 顶部标题条 ----
        head = ttk.Frame(f, style="TFrame")
        head.pack(fill="x", pady=(0, T.PAD))
        ttk.Label(head, text="智能终端 · 运行态势驾驶舱",
                  style="Title.TLabel").pack(side="left")
        self.lbl_clock = ttk.Label(head, text="--:--:--", style="H1.TLabel")
        self.lbl_clock.pack(side="right", padx=(T.PAD, 0))
        self.lbl_chain = ttk.Label(head, text="审计链：校验中…", style="H1.TLabel")
        self.lbl_chain.pack(side="right", padx=(0, T.PAD))
        ttk.Button(head, text="⛶ 全屏切换", command=self.toggle_fullscreen
                   ).pack(side="right", padx=(0, T.PAD))
        ttk.Button(head, text="🔄 刷新", command=self.refresh
                   ).pack(side="right", padx=(0, T.PAD))

        # ---- KPI 卡片区 ----
        row1 = ttk.Frame(f, style="TFrame")
        row1.pack(fill="x")
        self._kpi["total"] = T.kpi(row1, "告警总数", "0", "条", T.ACCENT)
        self._kpi["red"] = T.kpi(row1, "红色预警（须确认）", "0", "条", T.ALARM)
        self._kpi["orange"] = T.kpi(row1, "橙色预警", "0", "条", T.WARN)
        self._kpi["review"] = T.kpi(row1, "已复核", "0", "条", T.OK)
        self._kpi["pending"] = T.kpi(row1, "未同步（待补传）", "0", "条", T.ACCENT3)

        row2 = ttk.Frame(f, style="TFrame")
        row2.pack(fill="x")
        self._kpi["remote"] = T.kpi(row2, "远端记录（局域网）", "0", "条", T.ACCENT2)
        self._kpi["devices"] = T.kpi(row2, "接入设备", "0", "台", T.ACCENT)
        self._kpi["fire"] = T.kpi(row2, "火警（火焰确认）", "0", "起", T.ALARM)
        self._kpi["fire_smoke"] = T.kpi(row2, "烟雾疑似", "0", "起", T.WARN)
        self._kpi["audit"] = T.kpi(row2, "审计条数", "0", "条", T.OK)

        # ---- 图表区 ----
        charts = ttk.Frame(f, style="TFrame")
        charts.pack(fill="both", expand=True, pady=(T.PAD, 0))
        charts.columnconfigure(0, weight=3)
        charts.columnconfigure(1, weight=3)
        charts.columnconfigure(2, weight=2)
        charts.rowconfigure(0, weight=1)

        c1, b1 = T.card(charts, "告警分级分布", manager="grid",
                        row=0, column=0, sticky="nsew", padx=(0, T.PAD))
        self.cv_level = tk.Canvas(b1, bg=T.PANEL, highlightthickness=0, height=170)
        self.cv_level.pack(fill="both", expand=True)

        c2, b2 = T.card(charts, "近 12 小时告警趋势", manager="grid",
                        row=0, column=1, sticky="nsew", padx=(0, T.PAD))
        self.cv_trend = tk.Canvas(b2, bg=T.PANEL, highlightthickness=0, height=170)
        self.cv_trend.pack(fill="both", expand=True)

        c3, b3 = T.card(charts, "远端来源设备", manager="grid",
                        row=0, column=2, sticky="nsew")
        self.cv_dev = tk.Canvas(b3, bg=T.PANEL, highlightthickness=0, height=170)
        self.cv_dev.pack(fill="both", expand=True)

        # ---- 全系统总览（跨模块，子进程聚合，见 remote_status.py）----
        mrow = ttk.Frame(f, style="TFrame")
        mrow.pack(fill="x", pady=(T.PAD, 0))
        mc, mbody = T.card(mrow, "全系统模块态势（子进程聚合，只读）", manager="grid",
                           row=0, column=0, sticky="nsew")
        self.tree_modules = ttk.Treeview(mc, columns=("mod", "state", "brief"),
                                         show="headings", height=8)
        for c, txt, w in (("mod", "模块", 190), ("state", "状态", 70),
                          ("brief", "关键指标", 900)):
            self.tree_modules.heading(c, text=txt)
            self.tree_modules.column(c, width=w, anchor="w")
        self.tree_modules.tag_configure("ok", foreground=T.OK)
        self.tree_modules.tag_configure("bad", foreground=T.FG_MUTE)
        self.tree_modules.pack(fill="both", expand=True)

        # ---- 明细区 ----
        detail = ttk.Frame(f, style="TFrame")
        detail.pack(fill="both", expand=True, pady=(T.PAD, 0))
        detail.columnconfigure(0, weight=3)
        detail.columnconfigure(1, weight=2)
        detail.rowconfigure(0, weight=1)

        d1, bd1 = T.card(detail, "最近告警", manager="grid",
                         row=0, column=0, sticky="nsew", padx=(0, T.PAD))
        cols = ("time", "level", "code", "room", "status")
        self.tree_alerts = ttk.Treeview(bd1, columns=cols, show="headings", height=6)
        for c, txt, w in (("time", "时间", 130), ("level", "级别", 90),
                          ("code", "类型", 140), ("room", "考场", 70),
                          ("status", "状态", 80)):
            self.tree_alerts.heading(c, text=txt)
            self.tree_alerts.column(c, width=w, anchor="w")
        self.tree_alerts.tag_configure("red", foreground=T.ALARM)
        self.tree_alerts.tag_configure("orange", foreground=T.WARN)
        self.tree_alerts.pack(fill="both", expand=True)

        d2, bd2 = T.card(detail, "远端设备台账", manager="grid",
                         row=0, column=1, sticky="nsew")
        cols2 = ("dev", "name", "records", "last")
        self.tree_devs = ttk.Treeview(bd2, columns=cols2, show="headings", height=6)
        for c, txt, w in (("dev", "设备", 90), ("name", "名称", 110),
                          ("records", "条数", 60), ("last", "最后活跃", 130)):
            self.tree_devs.heading(c, text=txt)
            self.tree_devs.column(c, width=w, anchor="w")
        self.tree_devs.pack(fill="both", expand=True)

        self.lbl_foot = ttk.Label(f, text="", style="TLabel", foreground=T.FG_DIM)
        self.lbl_foot.pack(anchor="w", pady=(T.PAD_S, 0))

        self.refresh()
        self.refresh_modules()
        self._tick()

    # ------------------------------------------------------------ 定时
    def _tick(self):
        try:
            self.lbl_clock.config(text=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return
        self._after = self.parent.after(1000, self._tick)
        # 每 5 秒刷新一次数据（1 秒只走时钟，避免频繁查库）
        self._n = getattr(self, "_n", 0) + 1
        if self._n % 5 == 0:
            self.refresh()
        # 跨模块聚合走子进程，代价高：只在缓存过期时刷（默认 30 秒 TTL）
        if self._n % 30 == 0:
            self.refresh_modules()

    def stop(self):
        if self._after:
            try:
                self.parent.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    # ------------------------------------------------------------ 刷新
    def refresh(self):
        try:
            alerts = self.store.list_alerts(limit=2000)
        except Exception:
            alerts = []
        try:
            reviews = self.store.list_reviews(limit=2000)
        except Exception:
            reviews = []
        try:
            rem = self.store.remote_summary()
        except Exception:
            rem = {}
        try:
            devs = self.store.remote_devices()
        except Exception:
            devs = []
        try:
            audit_rows = self.store.audit_rows(limit=100000)
        except Exception:
            audit_rows = []

        by_level = {}
        by_code = {}
        for a in alerts:
            by_level[a.level] = by_level.get(a.level, 0) + 1
            by_code[a.code] = by_code.get(a.code, 0) + 1

        red = by_level.get(config.LEVEL_RED, 0)
        orange = by_level.get(config.LEVEL_ORANGE, 0)
        unsynced = len([a for a in alerts if not a.synced])
        fire_alarm = by_code.get("FIRE_ALARM", 0)
        fire_smoke = by_code.get("FIRE_SMOKE", 0)

        T.set_kpi(self._kpi["total"], len(alerts), T.ACCENT)
        T.set_kpi(self._kpi["red"], red, T.ALARM if red else T.FG_MUTE)
        T.set_kpi(self._kpi["orange"], orange, T.WARN if orange else T.FG_MUTE)
        T.set_kpi(self._kpi["review"], len(reviews), T.OK)
        T.set_kpi(self._kpi["pending"], unsynced, T.ACCENT3 if unsynced else T.FG_MUTE)
        T.set_kpi(self._kpi["remote"], rem.get("records", 0), T.ACCENT2)
        T.set_kpi(self._kpi["devices"], rem.get("devices", 0), T.ACCENT)
        T.set_kpi(self._kpi["fire"], fire_alarm, T.ALARM if fire_alarm else T.FG_MUTE)
        T.set_kpi(self._kpi["fire_smoke"], fire_smoke,
                  T.WARN if fire_smoke else T.FG_MUTE)
        T.set_kpi(self._kpi["audit"], len(audit_rows), T.OK)

        # ---- 图表 ----
        try:
            # 03 只定义橙/红两级（config.LEVELS），这里如实按两级展示
            lv = [("红色预警", red), ("橙色预警", orange)]
            T.bar_chart(self.cv_level, lv, color=T.ACCENT2)
            T.line_chart(self.cv_trend, hour_buckets(alerts, 12), color=T.ACCENT)
            dl = [(d.get("device_id", "?"), int(d.get("records", 0) or 0))
                  for d in devs[:8]]
            T.bar_chart(self.cv_dev, dl, color=T.ACCENT3)
        except Exception:
            pass

        # ---- 明细表 ----
        try:
            for iid in self.tree_alerts.get_children():
                self.tree_alerts.delete(iid)
            for a in alerts[:30]:
                tag = ("red" if a.level == config.LEVEL_RED else
                       "orange" if a.level == config.LEVEL_ORANGE else "")
                self.tree_alerts.insert("", "end", values=(
                    a.created_at or "",
                    config.LEVEL_META.get(a.level, {}).get("name", a.level),
                    config.ALERT_CODE_TEXT.get(a.code, a.code),
                    a.room_id or "—", _status_text(a.status)),
                    tags=(tag,))
        except Exception:
            pass
        try:
            for iid in self.tree_devs.get_children():
                self.tree_devs.delete(iid)
            for d in devs[:30]:
                self.tree_devs.insert("", "end", values=(
                    d.get("device_id", ""), d.get("device_name", "") or "—",
                    d.get("records", 0), (d.get("last_seen", "") or "")[:19]))
        except Exception:
            pass

        try:
            ok, why, cnt = self.store.verify_chain()
            self.lbl_chain.config(text="审计链：%s（%d 条）" % ("完整" if ok else "异常", cnt),
                                  foreground=T.OK if ok else T.ALARM)
        except Exception as e:
            self.lbl_chain.config(text="审计链：校验失败", foreground=T.ALARM)
        try:
            self.lbl_foot.config(
                text="数据库：%s　｜　当前账号：%s（%s）　｜　远端最近接收：%s"
                     % (config.DB_PATH,
                        self.session.display_name if self.session else "—",
                        config.ROLE_TEXT.get(getattr(self.session, "role", ""), "—"),
                        rem.get("last_received") or "—"))
        except Exception:
            pass

    def refresh_modules(self, force=False):
        """刷新"全系统模块态势"表（跨模块子进程聚合；带 TTL 缓存，见 remote_status）"""
        tree = getattr(self, "tree_modules", None)
        if tree is None:
            return
        try:
            import remote_status
        except Exception as e:
            return
        agg = getattr(self, "_agg", None)
        if agg is None:
            agg = self._agg = remote_status.ModuleStatus(ttl=30)
        try:
            data = agg.all(force=force)
        except Exception:
            return
        try:
            for iid in tree.get_children():
                tree.delete(iid)
            for name, text, ok in remote_status.ModuleStatus.brief(data):
                tree.insert("", "end", values=(
                    name, "可用" if ok else "不可用", text),
                    tags=("ok" if ok else "bad",))
        except Exception:
            pass

    # ------------------------------------------------------------ 全屏
    def toggle_fullscreen(self):
        """一键大屏（考场大屏展示用）。Esc 退出由主窗口统一绑定。"""
        root = self.app.root
        self._fullscreen = not self._fullscreen
        try:
            root.attributes("-fullscreen", self._fullscreen)
        except Exception:
            try:
                root.state("zoomed" if self._fullscreen else "normal")
            except Exception:
                pass
        self.refresh()
