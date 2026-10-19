# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 桌面界面（tkinter，仅标准库）

八个页签，对着《无线信号监测与屏蔽管控.md》的五个环节 + 合规验收落地：

  ① 总览            指标卡片 + 最近异常 + 一键演示/回放/出报告
  ② 频谱监测        选站选频段 -> 模拟扫描 / 连续监视 -> 频谱图 + 峰值表 + 扫描历史
  ③ 异常与定位      异常队列 + 详情 + 地图定位 + 复核 + 上报 03 + 联动 02/06
  ④ 屏蔽管控        屏蔽器状态 + 开机闸门 + 频段/功率 + 开关记录 + 设备登记
  ⑤ 考试时间表      场次维护 + 当前窗口 + 自动开关 tick + 一致性核对
  ⑥ 合规与验收      合规体检 + 审批/协同 + 屏蔽效果/外溢实测 + 生成验收报告
  ⑦ 联动            00/02/03/06 可达性 + 测试告警 + 关联安检/机位 + 远端记录推送
  ⑧ 设置与审计      运行参数 + 阈值 + 审计链校验 + 数据维护

运行：python gui.py      （或双击「启动.bat」）
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import compliance
import config
import features
import linkage_bridge
import locate
import models
import report as report_mod
import samples
import schedule
import shield
import spectrum
import storage

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
FONT_T = ("Microsoft YaHei", 15, "bold")
FONT_S = ("Microsoft YaHei", 9)

C_OK = "#0a7d28"
C_WARN = "#e08b00"
C_BAD = "#c00000"
C_DIM = "#555555"
C_BLUE = "#12395c"

PRESETS = (
    ("2.4G 数传作弊（2442 MHz / -62 dBm / 2 MHz）", "ism24", 2442.0, 2.0, -62.0, None),
    ("UHF 隐形耳机（433.5 MHz / -78 dBm / 0.025 MHz）", "uhf", 433.5, 0.025, -78.0, None),
    ("5.8G 无线图传（5800 MHz / -70 dBm / 8 MHz）", "unii3", 5800.0, 8.0, -70.0, None),
    ("合法 WiFi AP（2437 MHz / -50 dBm / 20 MHz）", "ism24", 2437.0, 20.0, -50.0, None),
    ("非法频段发射（520 MHz / -80 dBm / 0.05 MHz）", "", 520.0, 0.05, -80.0, (500.0, 540.0)),
    ("纯背景噪声（无注入）", "ism24", 2442.0, 2.0, None, None),
)


def _fmt_epoch(epoch):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(epoch)))
    except (TypeError, ValueError):
        return "-"


def _short(text, n=90):
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


class App(object):
    def __init__(self, root):
        config.ensure_stdio()
        self.root = root
        root.title("无线信号监测与屏蔽管控 · 项目六专题07")
        root.geometry("1360x880")
        root.minsize(1140, 740)

        self.store = storage.Store()
        features.ensure_builtin(self.store)
        self.cfg = config.load_settings()
        self.events = queue.Queue()
        self._stop = False
        self._last_tick = 0.0
        self._last_scan = None
        self._cur_anomaly = None
        self._cur_jammer = None
        self._monitor_job = None
        self._logs = []

        self._build_header()
        self._build_tabs()
        self._build_status()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_all()
        root.after(1000, self._tick)

    # ================================================================= 基础设施
    def _log(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        self._logs.append(line)
        del self._logs[:-300]
        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", line + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except Exception:
            pass

    def _bg(self, label, fn, on_done=None):
        def work():
            try:
                r = fn()
            except Exception as e:                            # noqa: BLE001
                r = {"ok": False, "reason": "%s: %s" % (type(e).__name__,
                                                        str(e)[:200])}
            self.events.put((label, r, on_done))
        threading.Thread(target=work, daemon=True).start()

    def _tick(self):
        self.var_clock.set(time.strftime("%Y-%m-%d %H:%M:%S"))
        drained = 0
        while drained < 20:
            try:
                label, r, cb = self.events.get_nowait()
            except queue.Empty:
                break
            drained += 1
            ok = bool(r.get("ok", True)) if isinstance(r, dict) else True
            reason = (r.get("reason") or r.get("message") or "") if isinstance(r, dict) else ""
            self._log("%s：%s%s" % (label, "完成" if ok else "失败",
                                   ("（%s）" % _short(reason, 100)) if reason else ""))
            if cb is not None:
                try:
                    cb(r)
                except Exception:                             # noqa: BLE001
                    pass
        if drained:
            self.refresh_overview()
        # 时间表自动开关：每 5 秒 tick 一次（重活都在本地库，不卡界面）
        now = time.time()
        if self.var_auto_tick.get() and now - self._last_tick >= 5.0:
            self._last_tick = now
            try:
                r = shield.tick(self.store, operator="界面自动")
                acts = r.get("actions") or []
                if acts:
                    for a in acts:
                        self._log("自动开关：%s %s %s" % (
                            a.get("device_code"), a.get("action"),
                            "成功" if a.get("ok") else "失败：" + (a.get("reason") or "")))
                    self.refresh_shield()
                    self.refresh_overview()
            except Exception as e:                            # noqa: BLE001
                self._log("自动开关异常：%s" % e)
        if not self._stop:
            self.root.after(1000, self._tick)

    # ==================================================================== 头部
    def _build_header(self):
        top = tk.Frame(self.root, bg=C_BLUE, height=64)
        top.pack(fill="x")
        tk.Label(top, text="  无线信号监测与屏蔽管控", bg=C_BLUE, fg="white",
                 font=FONT_T).pack(side="left", pady=12)
        tk.Label(top, text="频谱发现 → 定位 → 上报处置 ｜ 时间表驱动屏蔽开关 ｜ 合规留痕",
                 bg=C_BLUE, fg="#bcd7ef", font=FONT).pack(side="left", padx=10)
        self.var_site = tk.StringVar(value="考点：%s" % self.cfg.get("site", ""))
        tk.Label(top, textvariable=self.var_site, bg=C_BLUE, fg="#ffd54f",
                 font=FONT_B).pack(side="right", padx=18)
        self.var_clock = tk.StringVar(value="")
        tk.Label(top, textvariable=self.var_clock, bg=C_BLUE, fg="white",
                 font=FONT_B).pack(side="right", padx=10)

    # ==================================================================== 页签
    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self.tab_overview = tk.Frame(self.nb)
        self.tab_spectrum = tk.Frame(self.nb)
        self.tab_anomaly = tk.Frame(self.nb)
        self.tab_shield = tk.Frame(self.nb)
        self.tab_schedule = tk.Frame(self.nb)
        self.tab_compliance = tk.Frame(self.nb)
        self.tab_linkage = tk.Frame(self.nb)
        self.tab_settings = tk.Frame(self.nb)
        for frame, title in ((self.tab_overview, "① 总览"),
                             (self.tab_spectrum, "② 频谱监测"),
                             (self.tab_anomaly, "③ 异常与定位"),
                             (self.tab_shield, "④ 屏蔽管控"),
                             (self.tab_schedule, "⑤ 考试时间表"),
                             (self.tab_compliance, "⑥ 合规与验收"),
                             (self.tab_linkage, "⑦ 联动"),
                             (self.tab_settings, "⑧ 设置与审计")):
            self.nb.add(frame, text=title)
        self._build_overview()
        self._build_spectrum()
        self._build_anomaly()
        self._build_shield()
        self._build_schedule()
        self._build_compliance()
        self._build_linkage()
        self._build_settings()

    def _build_status(self):
        bar = tk.Frame(self.root, bg="#eeeeee", height=26)
        bar.pack(fill="x", side="bottom")
        self.var_status = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.var_status, bg="#eeeeee", anchor="w",
                 font=FONT_S).pack(side="left", padx=8)
        self.var_link = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.var_link, bg="#eeeeee", fg=C_DIM,
                 font=FONT_S).pack(side="right", padx=8)

    # ================================================================ ① 总览
    def _build_overview(self):
        f = self.tab_overview
        cards = tk.Frame(f)
        cards.pack(fill="x", padx=8, pady=8)
        self.card_vars = {}
        specs = [("monitors", "监测站"), ("scans", "24h 扫描"),
                 ("open", "待处置异常"), ("critical", "严重异常"),
                 ("on", "屏蔽开启"), ("latency", "平均时延"),
                 ("coverage", "监测覆盖"), ("score", "合规分")]
        for i, (key, title) in enumerate(specs):
            box = tk.Frame(cards, bd=1, relief="solid", padx=10, pady=6)
            box.grid(row=0, column=i, padx=4, sticky="nsew")
            cards.columnconfigure(i, weight=1)
            tk.Label(box, text=title, font=FONT_S, fg=C_DIM).pack()
            var = tk.StringVar(value="-")
            self.card_vars[key] = var
            tk.Label(box, textvariable=var, font=FONT_B, fg=C_BLUE).pack()

        mid = tk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8)
        left = tk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="最近异常", font=FONT_B).pack(anchor="w")
        cols = ("time", "station", "kind", "freq", "dbm", "latency", "sev", "status", "loc")
        self.tree_recent = self._tree(left, cols, (110, 70, 170, 120, 70, 60, 60, 70, 130))
        self.tree_recent.bind("<Double-1>", lambda e: self.nb.select(self.tab_anomaly))

        right = tk.Frame(mid)
        right.pack(side="right", fill="y", padx=(8, 0))
        tk.Label(right, text="一键演练 / 验收", font=FONT_B).pack(anchor="w")
        tk.Button(right, text="载入演示数据（设备/审批/场次/实测）", width=34,
                  command=self.on_seed_demo).pack(pady=3)
        tk.Button(right, text="跑回放测试（8 个场景）", width=34,
                  command=self.on_replay).pack(pady=3)
        tk.Button(right, text="生成验收报告（md + csv）", width=34,
                  command=self.on_report).pack(pady=3)
        tk.Button(right, text="打开报告目录", width=34,
                  command=self.on_open_reports).pack(pady=3)
        tk.Button(right, text="自动开关：立即 tick 一次", width=34,
                  command=self.on_shield_tick).pack(pady=3)
        tk.Button(right, text="载入演示并跑回放（一条龙）", width=34,
                  command=self.on_full_demo).pack(pady=3)

        tk.Label(f, text="运行日志", font=FONT_B).pack(anchor="w", padx=8, pady=(6, 0))
        self.txt_log = tk.Text(f, height=8, font=FONT_S, state="disabled",
                               bg="#111111", fg="#d0ffd0")
        self.txt_log.pack(fill="x", padx=8, pady=(0, 6))

    def refresh_overview(self):
        try:
            s = self.store.summary()
            chk = compliance.checks(self.store)
            cov = locate.coverage(self.store)
            self.card_vars["monitors"].set("%d / %d" % (s["monitors"], s["devices"]))
            self.card_vars["scans"].set(str(s["scans"]))
            self.card_vars["open"].set(str(s["open_anomalies"]))
            self.card_vars["critical"].set(str(s["critical"]))
            self.card_vars["on"].set("%d / %d" % (s["jammers_on"], s["jammers"]))
            self.card_vars["latency"].set("%.1fs" % float(s["avg_latency"] or 0))
            self.card_vars["coverage"].set("%.0f%%" % (cov["rate"] * 100))
            self.card_vars["score"].set("%d" % chk["score"])
            self.var_status.set("异常 %d（待处置 %d）｜扫描 %d 帧｜开关记录 %d 条｜%s"
                                % (s["anomalies"], s["open_anomalies"], s["scans"],
                                   s["switch_logs"], chk["summary_text"]))
        except Exception as e:                                # noqa: BLE001
            self.var_status.set("概览刷新失败：%s" % e)
        self._fill_recent()

    def _fill_recent(self):
        rows = self.store.list_anomalies(limit=120)
        self.tree_recent.delete(*self.tree_recent.get_children())
        for a in rows:
            self.tree_recent.insert("", "end", iid=a.anomaly_id, values=(
                _fmt_epoch(a.detected_epoch), a.station_code, a.kind_text(),
                a.frequency_text(), "%.1f" % a.peak_dbm,
                "%.1f" % float(a.latency_sec or 0), a.severity_text(),
                a.status_text(), a.located_text()))

    # ============================================================ ② 频谱监测
    def _build_spectrum(self):
        f = self.tab_spectrum
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="监测站", font=FONT).pack(side="left")
        self.cmb_station = ttk.Combobox(top, width=14, state="readonly")
        self.cmb_station.pack(side="left", padx=4)
        tk.Label(top, text="预设信号", font=FONT).pack(side="left", padx=(12, 0))
        self.cmb_preset = ttk.Combobox(top, width=44, state="readonly",
                                       values=[p[0] for p in PRESETS])
        self.cmb_preset.current(0)
        self.cmb_preset.pack(side="left", padx=4)
        tk.Button(top, text="扫描一帧", command=self.on_scan_once).pack(side="left", padx=4)
        tk.Button(top, text="连续监视", command=self.on_toggle_monitor).pack(side="left", padx=4)
        self.var_monitor = tk.StringVar(value="已停止")
        tk.Label(top, textvariable=self.var_monitor, fg=C_DIM, font=FONT_S).pack(
            side="left", padx=6)
        tk.Button(top, text="人工录入峰值", command=self.on_manual_peak).pack(side="right")

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8)
        self.cv_spectrum = tk.Canvas(body, bg="white", height=330,
                                     highlightthickness=1, highlightbackground="#cccccc")
        self.cv_spectrum.pack(fill="both", expand=True)
        self.cv_spectrum.bind("<Configure>", lambda e: self._draw_spectrum(self._last_scan))

        bottom = tk.Frame(f)
        bottom.pack(fill="x", padx=8, pady=6)
        left = tk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="本帧峰值", font=FONT_B).pack(anchor="w")
        cols = ("freq", "bw", "power", "snr", "band", "legal")
        self.tree_peaks = self._tree(left, cols, (110, 90, 80, 70, 160, 90), height=5)
        right = tk.Frame(bottom)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="扫描历史", font=FONT_B).pack(anchor="w")
        cols2 = ("time", "station", "band", "peaks", "noise", "occ", "source")
        self.tree_scans = self._tree(right, cols2, (110, 70, 160, 60, 80, 60, 70), height=5)
        self.tree_scans.bind("<<TreeviewSelect>>", self.on_scan_selected)

    def refresh_spectrum(self):
        stations = [d for d in self.store.list_devices()
                    if d.kind in (config.DEV_MONITOR, config.DEV_PROBE)]
        codes = [d.code for d in stations]
        self.cmb_station.configure(values=codes)
        if codes and not self.cmb_station.get():
            self.cmb_station.current(0)
        rows = self.store.list_scans(limit=80)
        self.tree_scans.delete(*self.tree_scans.get_children())
        for s in rows:
            self.tree_scans.insert("", "end", iid=s.scan_id, values=(
                _fmt_epoch(s.ts_epoch), s.station_code, config.band_name(s.band),
                s.peak_count, "%.1f" % s.noise_floor_dbm,
                "%.1f%%" % (s.occupancy * 100), s.source))
        if self._last_scan is not None:
            self._draw_spectrum(self._last_scan)

    def _draw_spectrum(self, scan):
        cv = self.cv_spectrum
        cv.delete("all")
        w = max(400, cv.winfo_width())
        h = max(260, cv.winfo_height())
        if scan is None:
            cv.create_text(w / 2, h / 2, text="点「扫描一帧」生成频谱（模拟数据会标 source=sim）",
                           fill=C_DIM, font=FONT)
            return
        bins = []
        raw = scan.raw or {}
        for item in (raw.get("bins") or raw.get("spectrum") or []):
            try:
                if isinstance(item, (list, tuple)):
                    bins.append((float(item[0]), float(item[1])))
                elif isinstance(item, dict):
                    bins.append((float(item["freq_mhz"]), float(item["power_dbm"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not bins:
            for p in (scan.peaks or []):
                bins.append((float(p.get("freq_mhz")), float(p.get("power_dbm"))))
        if not bins:
            cv.create_text(w / 2, h / 2, text="本帧没有频谱数据", fill=C_DIM, font=FONT)
            return
        bins.sort()
        fmin, fmax = bins[0][0], bins[-1][0]
        pmin = min(p for _f, p in bins)
        pmax = max(p for _f, p in bins)
        lo = min(pmin - 5, scan.noise_floor_dbm - 8)
        hi = pmax + 8
        if hi - lo < 20:
            hi = lo + 20
        pad_l, pad_r, pad_t, pad_b = 56, 16, 18, 34
        def px(freq):
            return pad_l + (freq - fmin) / max(1e-9, fmax - fmin) * (w - pad_l - pad_r)
        def py(power):
            return pad_t + (hi - power) / max(1e-9, hi - lo) * (h - pad_t - pad_b)
        # 网格与坐标
        for i in range(5):
            y = pad_t + i * (h - pad_t - pad_b) / 4.0
            p = hi - i * (hi - lo) / 4.0
            cv.create_line(pad_l, y, w - pad_r, y, fill="#eeeeee")
            cv.create_text(pad_l - 6, y, text="%.0f" % p, anchor="e",
                           font=FONT_S, fill=C_DIM)
        for i in range(5):
            x = pad_l + i * (w - pad_l - pad_r) / 4.0
            f = fmin + i * (fmax - fmin) / 4.0
            cv.create_line(x, pad_t, x, h - pad_b, fill="#f5f5f5")
            cv.create_text(x, h - pad_b + 12, text="%.0f" % f, font=FONT_S, fill=C_DIM)
        cv.create_text((pad_l + w - pad_r) / 2, h - 8, text="频率 MHz",
                       font=FONT_S, fill=C_DIM)
        # 噪声底/门限
        thr = float(scan.noise_floor_dbm) + config.thresholds()["noise_margin_db"]
        cv.create_line(pad_l, py(scan.noise_floor_dbm), w - pad_r,
                       py(scan.noise_floor_dbm), fill="#888888", dash=(4, 2))
        cv.create_text(w - pad_r - 4, py(scan.noise_floor_dbm) - 8,
                       text="噪声底 %.1f dBm" % scan.noise_floor_dbm,
                       anchor="e", font=FONT_S, fill="#666666")
        cv.create_line(pad_l, py(thr), w - pad_r, py(thr), fill=C_WARN, dash=(2, 3))
        cv.create_text(w - pad_r - 4, py(thr) - 8, text="检测门限 %.1f dBm" % thr,
                       anchor="e", font=FONT_S, fill=C_WARN)
        # 频谱折线（bin 多时抽样，避免卡界面）
        step = max(1, len(bins) // 1200)
        pts = []
        for i in range(0, len(bins), step):
            f, p = bins[i]
            pts.extend([px(f), py(p)])
        if len(pts) >= 4:
            cv.create_line(*pts, fill="#1976d2", width=1)
        # 峰值
        for p in (scan.peaks or []):
            f = float(p.get("freq_mhz"))
            power = float(p.get("power_dbm"))
            x, y = px(f), py(power)
            cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C_BAD, outline="")
            label = "%s\n%.1f dBm" % (("%.4f" % (f / 1000.0)) if f >= 1000
                                      else ("%.3f" % f), power)
            cv.create_text(x, y - 18, text=label, font=FONT_S, fill=C_BAD)
        cv.create_text(pad_l, pad_t - 6, anchor="w", font=FONT_S, fill=C_DIM,
                       text="%s ｜ %s ｜ %s ｜ 峰值 %d ｜ 源=%s"
                            % (scan.station_code, config.band_name(scan.band),
                               _fmt_epoch(scan.ts_epoch), scan.peak_count,
                               scan.source))

    def _refresh_peaks(self, scan):
        self.tree_peaks.delete(*self.tree_peaks.get_children())
        if scan is None:
            return
        for i, p in enumerate(scan.peaks or []):
            f = float(p.get("freq_mhz"))
            bw = float(p.get("bandwidth_mhz") or 0)
            m = features.match_peak(self.store, p)
            legal = "合法（不告警）" if m.get("is_legal") else (
                "作弊/可疑" if m.get("matched") else "未匹配")
            self.tree_peaks.insert("", "end", iid="%s#%d" % (scan.scan_id, i), values=(
                ("%.4f GHz" % (f / 1000.0)) if f >= 1000 else ("%.3f MHz" % f),
                "%.3f" % bw, "%.1f" % float(p.get("power_dbm") or 0),
                "%.1f" % float(p.get("snr_db") or 0), config.band_name(scan.band),
                legal))

    def on_scan_selected(self, _event=None):
        sel = self.tree_scans.selection()
        if not sel:
            return
        scan = self.store.get_scan(sel[0])
        if scan is None:
            return
        self._last_scan = scan
        self._draw_spectrum(scan)
        self._refresh_peaks(scan)

    def on_scan_once(self, inject=True):
        station = (self.cmb_station.get() or "").strip()
        if not station:
            messagebox.showwarning("缺少监测站", "请先载入演示数据或在「屏蔽管控」页登记监测站。")
            return None
        idx = max(0, self.cmb_preset.current())
        _text, band, freq, bw, power, rng = PRESETS[idx]
        if not inject:
            power = None
        frame = samples.demo_frame(station=station, freq=freq, bw=bw, power=power,
                                   band=band, freq_range=rng, source="sim")
        r = spectrum.ingest_scan(self.store, frame, source="sim")
        if not r.get("ok"):
            self._log("扫描失败：%s" % r.get("reason"))
            return None
        self._last_scan = self.store.get_scan(r["scan_id"])
        self._draw_spectrum(self._last_scan)
        self._refresh_peaks(self._last_scan)
        self._log("扫描 %s %s：峰值 %d，新增异常 %d，合并 %d，合法 %d，自身信号 %d"
                  % (station, config.band_name(r.get("band")), r.get("peaks", 0),
                     len(r.get("new") or []), len(r.get("merged") or []),
                     r.get("legal", 0), r.get("self_suppressed", 0)))
        for aid in (r.get("new") or []):
            ad = self.store.get_anomaly(aid)
            if ad is not None:
                loc = locate.locate_anomaly(self.store, ad)
                if loc.get("ok"):
                    self._log("定位 %s -> (%.1f, %.1f) ±%.0fm（%s）"
                              % (aid, loc["x"], loc["y"], loc["radius_m"],
                                 loc["method"]))
        self.refresh_all()
        return r

    def on_toggle_monitor(self):
        if self._monitor_job is not None:
            self.root.after_cancel(self._monitor_job)
            self._monitor_job = None
            self.var_monitor.set("已停止")
            self._log("连续监视已停止")
            return
        self.var_monitor.set("监视中（每 2 秒一帧，噪声）")
        self._log("连续监视开始（只生成噪声帧；点「扫描一帧」可注入预设信号）")
        self._monitor_loop()

    def _monitor_loop(self):
        if self._stop:
            return
        try:
            station = (self.cmb_station.get() or "").strip()
            if station:
                frame = samples.demo_frame(station=station, freq=2442.0, bw=2.0,
                                           power=None, band="ism24", source="sim")
                r = spectrum.ingest_scan(self.store, frame, source="sim")
                if r.get("ok"):
                    self._last_scan = self.store.get_scan(r["scan_id"])
                    self._draw_spectrum(self._last_scan)
        except Exception as e:                                # noqa: BLE001
            self._log("监视帧失败：%s" % e)
        self._monitor_job = self.root.after(2000, self._monitor_loop)

    def on_manual_peak(self):
        station = (self.cmb_station.get() or "").strip()
        if not station:
            messagebox.showwarning("缺少监测站", "请先选择监测站")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("人工录入峰值（现场手持侦测仪读数）")
        dlg.transient(self.root)
        dlg.grab_set()
        fields = {}
        specs = [("freq", "频率 MHz", "433.500"), ("bw", "带宽 MHz", "0.025"),
                 ("power", "场强 dBm", "-78.0"), ("duty", "占空 burst/continuous/hopping", "continuous")]
        for i, (key, label, default) in enumerate(specs):
            tk.Label(dlg, text=label).grid(row=i, column=0, sticky="e", padx=6, pady=4)
            var = tk.StringVar(value=default)
            tk.Entry(dlg, textvariable=var, width=20).grid(row=i, column=1, padx=6, pady=4)
            fields[key] = var

        def submit():
            try:
                freq = float(fields["freq"].get())
                bw = float(fields["bw"].get())
                power = float(fields["power"].get())
            except ValueError:
                messagebox.showerror("格式错误", "频率/带宽/场强必须是数字", parent=dlg)
                return
            payload = {"station_code": station, "band": config.band_of(freq),
                       "freq_mhz": freq, "bandwidth_mhz": bw, "power_dbm": power,
                       "duty": fields["duty"].get().strip(), "source": "manual"}
            r = spectrum.ingest_scan(self.store, payload, source="manual")
            dlg.destroy()
            if not r.get("ok"):
                messagebox.showerror("录入失败", r.get("reason") or "")
                return
            self._log("人工录入 %.3f MHz：新增异常 %d" % (freq, len(r.get("new") or [])))
            for aid in (r.get("new") or []):
                ad = self.store.get_anomaly(aid)
                if ad is not None:
                    locate.locate_anomaly(self.store, ad)
            self.refresh_all()
        tk.Button(dlg, text="提交", command=submit, width=12).grid(
            row=len(specs), column=0, columnspan=2, pady=8)

    # ============================================================ ③ 异常与定位
    def _build_anomaly(self):
        f = self.tab_anomaly
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        self.var_open_only = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="只看待处置", variable=self.var_open_only,
                       command=self.refresh_anomalies).pack(side="left")
        tk.Label(top, text="严重度", font=FONT).pack(side="left", padx=(12, 0))
        self.cmb_sev = ttk.Combobox(top, width=10, state="readonly",
                                    values=["全部", "critical", "warn", "watch"])
        self.cmb_sev.current(0)
        self.cmb_sev.bind("<<ComboboxSelected>>", lambda e: self.refresh_anomalies())
        self.cmb_sev.pack(side="left", padx=4)
        tk.Button(top, text="刷新", command=self.refresh_anomalies).pack(side="left", padx=4)
        tk.Button(top, text="关联 06 安检", command=self.on_correlate_06).pack(side="right", padx=3)
        tk.Button(top, text="关联 02 机位", command=self.on_correlate_02).pack(side="right", padx=3)

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8)
        left = tk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        cols = ("time", "station", "kind", "freq", "dbm", "snr", "latency", "sev", "status")
        self.tree_anomalies = self._tree(left, cols, (110, 70, 180, 110, 70, 60, 60, 60, 70))
        self.tree_anomalies.bind("<<TreeviewSelect>>", self.on_anomaly_selected)

        right = tk.Frame(body, width=520)
        right.pack(side="right", fill="both", padx=(8, 0))
        right.pack_propagate(False)
        tk.Label(right, text="详情", font=FONT_B).pack(anchor="w")
        self.txt_anomaly = tk.Text(right, height=9, font=FONT_S, wrap="word")
        self.txt_anomaly.pack(fill="x")
        self.cv_map = tk.Canvas(right, bg="#fbfbfb", height=250,
                                highlightthickness=1, highlightbackground="#cccccc")
        self.cv_map.pack(fill="both", expand=True, pady=4)
        btns = tk.Frame(right)
        btns.pack(fill="x")
        for text, cmd in (("定位", self.on_locate),
                          ("复核：成立", lambda: self.on_review(config.ST_CONFIRMED)),
                          ("复核：误报", lambda: self.on_review(config.ST_FALSE)),
                          ("归档", lambda: self.on_review(config.ST_CLOSED)),
                          ("上报 03", self.on_report_anomaly)):
            tk.Button(btns, text=text, command=cmd).pack(side="left", padx=2, pady=2)
        self.var_anomaly_hint = tk.StringVar(value="")
        tk.Label(right, textvariable=self.var_anomaly_hint, fg=C_DIM,
                 font=FONT_S).pack(anchor="w")

    def refresh_anomalies(self):
        sev = self.cmb_sev.get() if hasattr(self, "cmb_sev") else "全部"
        rows = self.store.list_anomalies(
            severity=None if sev in ("", "全部") else sev, limit=300)
        if self.var_open_only.get():
            rows = [a for a in rows if a.is_open()]
        self.tree_anomalies.delete(*self.tree_anomalies.get_children())
        for a in rows:
            self.tree_anomalies.insert("", "end", iid=a.anomaly_id, values=(
                _fmt_epoch(a.detected_epoch), a.station_code, a.kind_text(),
                a.frequency_text(), "%.1f" % a.peak_dbm, "%.1f" % a.snr_db,
                "%.1f" % float(a.latency_sec or 0), a.severity_text(),
                a.status_text()))
        if self._cur_anomaly is not None:
            cur = self.store.get_anomaly(self._cur_anomaly.anomaly_id)
            if cur is not None:
                self._show_anomaly(cur)

    def on_anomaly_selected(self, _event=None):
        sel = self.tree_anomalies.selection()
        if not sel:
            return
        ad = self.store.get_anomaly(sel[0])
        if ad is None:
            return
        self._cur_anomaly = ad
        self._show_anomaly(ad)

    def _show_anomaly(self, ad):
        self.txt_anomaly.configure(state="normal")
        self.txt_anomaly.delete("1.0", "end")
        lines = [
            "编号：%s" % ad.anomaly_id,
            "类型：%s（严重度 %s，状态 %s）" % (ad.kind_text(), ad.severity_text(),
                                             ad.status_text()),
            "频率：%s ｜ 带宽 %.3f MHz ｜ 峰值 %.1f dBm ｜ 信噪比 %.1f dB"
            % (ad.frequency_text(), ad.bandwidth_mhz, ad.peak_dbm, ad.snr_db),
            "监测站：%s ｜ 考场：%s ｜ 来源：%s"
            % (ad.station_code, ad.room_id or "-", ad.source),
            "特征：%s" % (ad.feature_name or "未命中特征库"),
            "首次出现：%s ｜ 本次发现：%s ｜ 时延 %.1fs ｜ 已见 %d 帧"
            % (_fmt_epoch(ad.first_seen_epoch), _fmt_epoch(ad.detected_epoch),
               float(ad.latency_sec or 0), int(ad.seen_count or 0)),
            "定位：%s（%s）" % (ad.located_text(),
                              ("可信度 %.2f" % ad.confidence) if ad.confidence else "-"),
            "上报 03：%s" % (ad.alert_id or "未上报"),
            "详情：%s" % (ad.detail or "-"),
        ]
        if ad.review_conclusion:
            lines.append("复核：%s ｜ %s ｜ %s ｜ %s"
                         % (ad.review_conclusion, ad.review_reason or "-",
                            ad.reviewer or "-", ad.reviewed_at or "-"))
        self.txt_anomaly.insert("end", "\n".join(lines))
        self.txt_anomaly.configure(state="disabled")
        self._draw_map(ad)
        self.var_anomaly_hint.set("提示：先「定位」再「复核」；复核结论会留痕到审计链")

    def _draw_map(self, anomaly=None):
        cv = self.cv_map
        cv.delete("all")
        w = max(300, cv.winfo_width())
        h = max(200, cv.winfo_height())
        devs = [d for d in self.store.list_devices() if d.located]
        pts = [(d.x, d.y) for d in devs]
        if anomaly is not None and anomaly.located_x is not None and anomaly.located_x >= 0:
            pts.append((anomaly.located_x, anomaly.located_y))
            pts.append((anomaly.located_x + max(10, anomaly.radius_m),
                        anomaly.located_y))
        if not pts:
            cv.create_text(w / 2, h / 2, text="设备未登记坐标，无法画平面图",
                           fill=C_DIM, font=FONT)
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs) - 20, max(xs) + 20
        y0, y1 = min(ys) - 20, max(ys) + 20
        def px(x):
            return 24 + (x - x0) / max(1e-9, x1 - x0) * (w - 48)
        def py(y):
            return h - 24 - (y - y0) / max(1e-9, y1 - y0) * (h - 48)
        cv.create_line(24, h - 24, w - 12, h - 24, fill="#cccccc")
        cv.create_line(24, h - 24, 24, 12, fill="#cccccc")
        cv.create_text(w - 8, h - 10, text="x →", anchor="e", font=FONT_S, fill=C_DIM)
        cv.create_text(6, 12, text="y ↑", font=FONT_S, fill=C_DIM)
        for d in devs:
            x, y = px(d.x), py(d.y)
            if d.kind == config.DEV_JAMMER:
                cv.create_polygon(x, y - 7, x - 7, y + 6, x + 7, y + 6,
                                  fill="#ffe0b2", outline=C_WARN)
            elif d.kind == config.DEV_PROBE:
                cv.create_rectangle(x - 6, y - 6, x + 6, y + 6,
                                    fill="#e0e0e0", outline="#666666")
            else:
                cv.create_rectangle(x - 7, y - 7, x + 7, y + 7,
                                    fill="#cfe8ff", outline=C_BLUE)
            cv.create_text(x, y - 12, text=d.code, font=FONT_S, fill=C_DIM)
        if anomaly is not None and anomaly.located_x is not None and anomaly.located_x >= 0:
            x, y = px(anomaly.located_x), py(anomaly.located_y)
            r = max(6, (anomaly.radius_m if anomaly.radius_m and anomaly.radius_m > 0
                        else 10) / max(1e-9, x1 - x0) * (w - 48))
            cv.create_oval(x - r, y - r, x + r, y + r, outline=C_BAD, width=2)
            cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C_BAD, outline="")
            cv.create_text(x, y + r + 10, text="估计位置 ±%.0fm" % (anomaly.radius_m or 0),
                           font=FONT_S, fill=C_BAD)
        cv.create_text(26, 12, anchor="w", font=FONT_S, fill=C_DIM,
                       text="□监测站  △屏蔽器  ◇侦测仪  ○估计位置")

    def on_locate(self):
        if self._cur_anomaly is None:
            messagebox.showinfo("提示", "先选中一条异常")
            return
        res = locate.locate_anomaly(self.store, self._cur_anomaly,
                                    operator=self.cfg.get("operator") or "界面")
        if not res.get("ok"):
            messagebox.showwarning("定位失败", res.get("reason") or "")
            return
        self._log("定位：(%s, %s) ±%.0fm，%s，参与 %d 站，%s"
                  % (res["x"], res["y"], res["radius_m"], res["method"],
                     res["n"], res.get("note") or ""))
        self.refresh_all()
        self._show_anomaly(self.store.get_anomaly(self._cur_anomaly.anomaly_id))

    def on_review(self, conclusion):
        if self._cur_anomaly is None:
            messagebox.showinfo("提示", "先选中一条异常")
            return
        reason = self._ask_text("复核结论", "请填写复核依据（会写入审计链）", "")
        if reason is None:
            return
        from integration_api import op_review_anomaly
        r = op_review_anomaly({"anomaly_id": self._cur_anomaly.anomaly_id,
                               "conclusion": conclusion, "reason": reason or "界面复核",
                               "reviewer": self.cfg.get("operator") or "界面",
                               "sync_terminal": True})
        self._log("复核：%s" % (r.get("reason") or "已留痕"))
        self.refresh_all()
        cur = self.store.get_anomaly(self._cur_anomaly.anomaly_id)
        if cur is not None:
            self._show_anomaly(cur)

    def on_report_anomaly(self):
        if self._cur_anomaly is None:
            messagebox.showinfo("提示", "先选中一条异常")
            return
        ad = self._cur_anomaly

        def work():
            return linkage_bridge.report_anomaly(self.store, ad, sync=True,
                                                 operator=self.cfg.get("operator") or "07")
        self._bg("上报 03", work, lambda r: self.refresh_all())

    def on_correlate_06(self):
        self._bg("关联 06 安检",
                 lambda: linkage_bridge.correlate_06(self.store),
                 lambda r: self._after_correlate(r, "06"))

    def on_correlate_02(self):
        room = (self._cur_anomaly.room_id if self._cur_anomaly else "") or self._ask_text(
            "关联 02 机位", "输入考场 room_id（留空则按机位编码）", "")
        self._bg("关联 02 机位",
                 lambda: linkage_bridge.correlate_02(self.store, room_id=room or ""),
                 lambda r: self._after_correlate(r, "02"))

    def _after_correlate(self, r, source):
        if not r.get("ok"):
            self._log("关联 %s 失败：%s" % (source, r.get("reason")))
            return
        if source == "06":
            self._log("关联 06：拉取 %d 条，新增 %d 条；关注考场 %s"
                      % (r.get("pulled", 0), r.get("new", 0),
                         "、".join(x["room_id"] for x in (r.get("focus_rooms") or [])) or "-"))
        else:
            self._log("关联 02：%s" % _short(r.get("detail"), 160))

    # ============================================================ ④ 屏蔽管控
    def _build_shield(self):
        f = self.tab_shield
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="屏蔽器", font=FONT).pack(side="left")
        self.cmb_jammer = ttk.Combobox(top, width=18, state="readonly")
        self.cmb_jammer.bind("<<ComboboxSelected>>", lambda e: self.refresh_shield())
        self.cmb_jammer.pack(side="left", padx=4)
        self.var_auto_tick = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="按时间表自动开/关（每 5 秒）",
                       variable=self.var_auto_tick).pack(side="left", padx=10)
        tk.Button(top, text="立即 tick", command=self.on_shield_tick).pack(side="left")
        tk.Button(top, text="新增/编辑设备", command=self.on_device_dialog).pack(side="right", padx=3)

        info = tk.Frame(f)
        info.pack(fill="x", padx=8)
        self.var_jammer = tk.StringVar(value="未选择")
        tk.Label(info, textvariable=self.var_jammer, font=FONT_B, fg=C_BLUE,
                 justify="left", anchor="w").pack(fill="x")
        self.txt_gate = tk.Text(info, height=5, font=FONT_S, wrap="word")
        self.txt_gate.pack(fill="x", pady=4)

        ctrl = tk.Frame(f)
        ctrl.pack(fill="x", padx=8, pady=4)
        tk.Label(ctrl, text="屏蔽频段（多选）", font=FONT_B).pack(anchor="w")
        self.lst_bands = tk.Listbox(ctrl, selectmode="multiple", height=6, width=46,
                                    font=FONT_S, exportselection=False)
        self.lst_bands.pack(side="left")
        for b in config.BANDS:
            self.lst_bands.insert("end", "%s（%s）" % (b["name"], b["code"]))
        ops = tk.Frame(ctrl)
        ops.pack(side="left", padx=10, anchor="n")
        tk.Label(ops, text="功率 W").grid(row=0, column=0, sticky="e")
        self.var_power = tk.StringVar(value="2.0")
        tk.Spinbox(ops, from_=0.1, to=5.0, increment=0.1, width=8,
                   textvariable=self.var_power).grid(row=0, column=1, padx=4)
        self.var_drill = tk.BooleanVar(value=False)
        tk.Checkbutton(ops, text="演练模式（未经审批，仅联调）",
                       variable=self.var_drill).grid(row=1, column=0, columnspan=2,
                                                     sticky="w", pady=4)
        tk.Label(ops, text="开机理由").grid(row=2, column=0, sticky="e")
        self.var_reason = tk.StringVar(value="按考试窗口开启")
        tk.Entry(ops, textvariable=self.var_reason, width=22).grid(row=2, column=1, padx=4)
        tk.Button(ops, text="开启屏蔽", width=16, command=self.on_shield_on).grid(
            row=3, column=0, columnspan=2, pady=6)
        tk.Button(ops, text="关闭屏蔽", width=16, command=self.on_shield_off).grid(
            row=4, column=0, columnspan=2)

        bottom = tk.Frame(f)
        bottom.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="开关记录（合规证据）", font=FONT_B).pack(anchor="w")
        cols = ("time", "device", "action", "bands", "power", "source", "operator", "reason")
        self.tree_switch = self._tree(left, cols, (110, 70, 60, 240, 60, 70, 80, 150), height=8)
        right = tk.Frame(bottom)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="设备台账", font=FONT_B).pack(anchor="w")
        cols2 = ("code", "name", "kind", "room", "coord", "status")
        self.tree_devices = self._tree(right, cols2, (80, 150, 90, 70, 110, 60), height=8)

    def refresh_shield(self):
        jammers = self.store.list_devices(kind=config.DEV_JAMMER)
        codes = [j.code for j in jammers]
        self.cmb_jammer.configure(values=codes)
        if codes and not self.cmb_jammer.get():
            self.cmb_jammer.current(0)
        code = self.cmb_jammer.get()
        jam = self.store.get_device(code=code) if code else None
        self._cur_jammer = jam
        states = {s.device_id: s for s in self.store.list_states()}
        devs = self.store.list_devices()
        self.tree_devices.delete(*self.tree_devices.get_children())
        for d in devs:
            self.tree_devices.insert("", "end", iid=d.device_id, values=(
                d.code, d.name, config.DEV_KIND_TEXT.get(d.kind, d.kind),
                d.room_id or "-",
                ("(%0.f,%0.f)" % (d.x, d.y)) if d.located else "未标定",
                d.status_text()))
        if jam is not None:
            st = shield.state_view(self.store, jam)
            ev = shield.evaluate(self.store, jam)
            power = shield.power_cap()
            lines = [
                "设备：%s ｜ %s ｜ %s ｜ 考场 %s ｜ 坐标 %s"
                % (jam.code, jam.name, jam.status_text(), jam.room_id or "-",
                   ("(%0.f,%0.f)" % (jam.x, jam.y)) if jam.located else "未标定"),
                "状态：%s ｜ 已开启 %s ｜ 持续 %s 分钟"
                % ("开启" if st["on"] else "关闭", st["since"] or "-",
                   st["duration_min"]),
                "频段：%s" % (st["bands_text"] or "（未设置）"),
                "功率：%.2f W（上限 %.1f W）｜ 定向：%s ｜ 审批：%s"
                % (float(st["power_w"] or 0), power, jam.direction_text(),
                   st["approval_no"] or "-"),
                "开机闸门：%s" % ("允许开启" if ev["can_on"] else "不允许"),
            ]
            if st.get("manual_off"):
                lines.append("自动策略：%s" % st.get("manual_off_reason"))
            self.var_jammer.set("\n".join(lines))
            self.txt_gate.configure(state="normal")
            self.txt_gate.delete("1.0", "end")
            for b in ev["blockers"]:
                self.txt_gate.insert("end", "✗ %s\n" % b["text"])
            for w in ev["warnings"]:
                self.txt_gate.insert("end", "! %s\n" % w["text"])
            if not ev["blockers"] and not ev["warnings"]:
                self.txt_gate.insert("end", "✓ 全部闸门通过\n")
            self.txt_gate.configure(state="disabled")
            # 频段勾选
            self.lst_bands.selection_clear(0, "end")
            want = (states[jam.device_id].bands if jam.device_id in states
                    else jam.bands) or config.shield_plan()
            for i, b in enumerate(config.BANDS):
                if b["code"] in want:
                    self.lst_bands.selection_set(i)
            self.var_power.set("%.1f" % float(st["power_w"] or jam.power_w or 1.0))
        logs = self.store.list_switches(limit=120)
        self.tree_switch.delete(*self.tree_switch.get_children())
        for lg in logs:
            self.tree_switch.insert("", "end", iid=lg.log_id, values=(
                _fmt_epoch(lg.ts_epoch), lg.device_id, lg.action_text(),
                "、".join(config.band_name(b) for b in (lg.bands or [])),
                "%.2f" % lg.power_w, lg.source_text(), lg.operator or "-",
                _short(lg.reason, 60)))

    def _selected_bands(self):
        out = []
        for idx in self.lst_bands.curselection():
            out.append(config.BANDS[int(idx)]["code"])
        return out

    def on_shield_on(self):
        jam = self._cur_jammer
        if jam is None:
            messagebox.showinfo("提示", "先选择屏蔽器（没有设备请点「新增/编辑设备」）")
            return
        try:
            power = float(self.var_power.get())
        except ValueError:
            messagebox.showerror("格式错误", "功率必须是数字")
            return
        r = shield.turn_on(self.store, jam,
                           operator=self.cfg.get("operator") or "界面",
                           reason=self.var_reason.get() or "界面开启",
                           bands=self._selected_bands() or None, power_w=power,
                           drill=bool(self.var_drill.get()))
        if not r.get("ok"):
            messagebox.showwarning("拒绝开启", r.get("reason") or "")
            self._log("拒绝开启 %s：%s" % (jam.code, r.get("reason")))
        else:
            self._log("开启 %s：%s，%.1fW，%s" % (jam.code,
                                                "、".join(config.band_name(b)
                                                          for b in r["bands"]),
                                                r["power_w"], r["source"]))
        self.refresh_all()

    def on_shield_off(self):
        jam = self._cur_jammer
        if jam is None:
            return
        r = shield.turn_off(self.store, jam,
                            operator=self.cfg.get("operator") or "界面",
                            reason="界面关闭")
        if not r.get("ok"):
            messagebox.showwarning("关闭失败", r.get("reason") or "")
            self._log("关闭失败 %s：%s" % (jam.code, r.get("reason")))
        else:
            self._log("关闭 %s，持续 %.1f 分钟" % (jam.code, r["duration_sec"] / 60.0))
        self.refresh_all()

    def on_shield_tick(self):
        r = shield.tick(self.store, operator=self.cfg.get("operator") or "界面")
        for a in r.get("actions") or []:
            self._log("tick：%s %s %s" % (a.get("device_code"), a.get("action"),
                                          "成功" if a.get("ok") else
                                          "失败：" + (a.get("reason") or "")))
        if not (r.get("actions") or []):
            self._log("tick：无需动作（窗口 %s，%s）"
                      % ("内" if r.get("in_window") else "外",
                         r.get("phase") or "-"))
        self.refresh_all()

    def on_device_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("设备登记（监测站 / 手持侦测仪 / 屏蔽器）")
        dlg.transient(self.root)
        dlg.grab_set()
        fields = {}
        current = self.store.get_device(code=self.cmb_jammer.get() or "")
        specs = [("code", "设备编码", current.code if current else "JAM-C"),
                 ("name", "名称", current.name if current else "屏蔽器 C（RM-103）"),
                 ("kind", "类型 monitor/probe/jammer",
                  current.kind if current else config.DEV_JAMMER),
                 ("x", "坐标 x（米）", "%.0f" % current.x if current else "20"),
                 ("y", "坐标 y（米）", "%.0f" % current.y if current else "60"),
                 ("direction", "方位角（-1=未设）", "%.0f" % current.direction if current else "90"),
                 ("sector", "扇区宽度（度）", "%.0f" % current.sector_width if current else "60"),
                 ("power", "功率 W", "%.1f" % current.power_w if current else "1.5"),
                 ("room", "考场", current.room_id if current else "RM-103"),
                 ("status", "状态 online/offline/fault",
                  current.status if current else config.DEV_ONLINE)]
        for i, (key, label, default) in enumerate(specs):
            tk.Label(dlg, text=label).grid(row=i, column=0, sticky="e", padx=6, pady=3)
            var = tk.StringVar(value=str(default))
            tk.Entry(dlg, textvariable=var, width=26).grid(row=i, column=1, padx=6, pady=3)
            fields[key] = var
        var_bands = tk.BooleanVar(value=True)
        tk.Checkbutton(dlg, text="屏蔽器默认使用全局屏蔽计划频段", variable=var_bands).grid(
            row=len(specs), column=0, columnspan=2, sticky="w", padx=6)

        def submit():
            code = fields["code"].get().strip()
            if not code:
                messagebox.showerror("缺少编码", "设备编码必填", parent=dlg)
                return
            args = {"code": code, "name": fields["name"].get().strip(),
                    "kind": fields["kind"].get().strip() or config.DEV_MONITOR,
                    "x": fields["x"].get(), "y": fields["y"].get(),
                    "direction": fields["direction"].get(),
                    "sector_width": fields["sector"].get(),
                    "power_w": fields["power"].get(),
                    "room_id": fields["room"].get().strip(),
                    "status": fields["status"].get().strip(),
                    "site": self.cfg.get("site") or "",
                    "operator": self.cfg.get("operator") or "界面"}
            if var_bands.get() and args["kind"] == config.DEV_JAMMER:
                args["bands"] = config.shield_plan()
            from integration_api import op_add_device
            r = op_add_device(args)
            dlg.destroy()
            if not r.get("ok"):
                messagebox.showerror("登记失败", r.get("reason") or "")
                return
            self._log("设备已登记：%s" % code)
            self.refresh_all()
        tk.Button(dlg, text="保存", command=submit, width=12).grid(
            row=len(specs) + 1, column=0, columnspan=2, pady=8)

    # ========================================================== ⑤ 考试时间表
    def _build_schedule(self):
        f = self.tab_schedule
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        self.var_window = tk.StringVar(value="")
        tk.Label(top, textvariable=self.var_window, font=FONT_B, fg=C_BLUE).pack(side="left")
        tk.Button(top, text="刷新场次状态", command=self.on_refresh_sessions).pack(side="right", padx=3)
        tk.Button(top, text="开关一致性核对", command=self.on_consistency).pack(side="right", padx=3)

        form = tk.Frame(f)
        form.pack(fill="x", padx=8, pady=4)
        self.ses_vars = {}
        specs = [("name", "场次名称", "语文（演示）", 18),
                 ("date", "日期 YYYY-MM-DD", models.today_str(), 12),
                 ("start", "开始 HH:MM", "09:00", 7),
                 ("end", "结束 HH:MM", "11:00", 7),
                 ("pre", "提前(分)", "15", 5),
                 ("rooms", "考场（逗号分隔）", "RM-101,RM-102", 18)]
        for i, (key, label, default, width) in enumerate(specs):
            tk.Label(form, text=label, font=FONT_S).grid(row=0, column=i * 2,
                                                         sticky="e", padx=3)
            var = tk.StringVar(value=default)
            tk.Entry(form, textvariable=var, width=width).grid(row=0, column=i * 2 + 1,
                                                               padx=3)
            self.ses_vars[key] = var
        tk.Button(form, text="保存场次", command=self.on_add_session).grid(
            row=0, column=len(specs) * 2, padx=6)

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8)
        left = tk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="场次表", font=FONT_B).pack(anchor="w")
        cols = ("date", "name", "time", "pre", "state", "window", "remaining")
        self.tree_sessions = self._tree(left, cols,
                                        (90, 180, 70, 60, 70, 220, 220), height=8)
        right = tk.Frame(body)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="开关记录与时间表一致性", font=FONT_B).pack(anchor="w")
        cols2 = ("level", "kind", "device", "detail", "at")
        self.tree_consistency = self._tree(right, cols2, (60, 100, 80, 330, 110), height=8)

    def refresh_schedule(self):
        rows = self.store.list_sessions(limit=200)
        self.tree_sessions.delete(*self.tree_sessions.get_children())
        for s in rows:
            op, cl, st, en = schedule.window(s)
            self.tree_sessions.insert("", "end", iid=s.session_id, values=(
                s.date, s.name, "%s~%s" % (s.start, s.end),
                "%d 分" % (int(s.pre_open_sec or 0) // 60), s.state_text(),
                "%s~%s" % (_fmt_epoch(op), _fmt_epoch(cl)),
                schedule.remaining_text(s)))
        sess, phase = schedule.find_window(self.store)
        self.var_window.set("当前：%s ｜ %s ｜ %s"
                            % (schedule.PHASE_TEXT.get(phase, "无窗口"),
                               sess.window_text() if sess else "无场次窗口",
                               schedule.remaining_text(sess) if sess else ""))

    def on_refresh_sessions(self):
        r = schedule.tick(self.store)
        for s in r.get("states_changed") or []:
            self._log("场次状态：%s -> %s" % (s["name"], s["state"]))
        self._log("场次状态已刷新：%s；%s" % (
            "窗口内" if r["in_window"] else "窗口外", r["phase"] or "-"))
        self.refresh_all()

    def on_add_session(self):
        try:
            pre = int(float(self.ses_vars["pre"].get() or 15)) * 60
        except ValueError:
            pre = 900
        rooms = [x.strip() for x in (self.ses_vars["rooms"].get() or "").split(",")
                 if x.strip()]
        ok, info = schedule.add_session(
            self.store, self.ses_vars["name"].get(), self.ses_vars["date"].get(),
            self.ses_vars["start"].get(), self.ses_vars["end"].get(),
            rooms=rooms, pre_open_sec=pre,
            operator=self.cfg.get("operator") or "界面")
        if not ok:
            messagebox.showerror("保存失败", info)
            return
        self._log("场次已保存：%s %s" % (info.date, info.name))
        self.refresh_all()

    def on_consistency(self):
        c = schedule.consistency(self.store)
        self.tree_consistency.delete(*self.tree_consistency.get_children())
        for i, r in enumerate(c["rows"]):
            self.tree_consistency.insert("", "end", iid="C%d" % i, values=(
                "不通过" if r["level"] == "fail" else "需关注",
                r["kind"], r.get("device_id") or r.get("session_id") or "-",
                _short(r["detail"], 160), r.get("at") or "-"))
        self._log("一致性核对：不通过 %d、需关注 %d（已结束场次 %d）"
                  % (c["fail"], c["warn"], c["checked_sessions"]))
        if not c["rows"]:
            messagebox.showinfo("一致性核对", "开关记录与考试时间表一致。")

    # ========================================================== ⑥ 合规与验收
    def _build_compliance(self):
        f = self.tab_compliance
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        self.var_compliance = tk.StringVar(value="")
        tk.Label(top, textvariable=self.var_compliance, font=FONT_B, fg=C_BLUE).pack(side="left")
        tk.Button(top, text="重新体检", command=self.refresh_compliance).pack(side="right", padx=3)
        tk.Button(top, text="生成验收报告", command=self.on_report).pack(side="right", padx=3)
        tk.Button(top, text="打开报告目录", command=self.on_open_reports).pack(side="right", padx=3)

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8)
        left = tk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="合规体检（红线 / 技术 / 证据）", font=FONT_B).pack(anchor="w")
        cols = ("category", "name", "level", "detail")
        self.tree_compliance = self._tree(left, cols, (70, 180, 60, 460), height=11)

        right = tk.Frame(body)
        right.pack(side="right", fill="both", padx=(8, 0))
        tk.Label(right, text="审批 / 报备", font=FONT_B).pack(anchor="w")
        cols2 = ("kind", "no", "valid", "status", "doc")
        self.tree_approvals = self._tree(right, cols2, (90, 120, 150, 60, 180), height=4)
        apf = tk.Frame(right)
        apf.pack(fill="x", pady=3)
        self.ap_vars = {}
        for i, (key, label, default, width) in enumerate(
                (("kind", "类型", "approval", 10), ("no", "编号", "WX-2026-1020", 16),
                 ("org", "机构", "省无线电管理机构", 16), ("doc", "批文", "批文扫描件", 16),
                 ("from", "生效", models.today_str(), 11),
                 ("to", "失效", models.date_str(models.now_epoch() + 30 * 86400), 11))):
            tk.Label(apf, text=label, font=FONT_S).grid(row=i // 3, column=(i % 3) * 2,
                                                         sticky="e", padx=2)
            var = tk.StringVar(value=default)
            tk.Entry(apf, textvariable=var, width=width).grid(row=i // 3,
                                                              column=(i % 3) * 2 + 1,
                                                              padx=2, pady=2)
            self.ap_vars[key] = var
        tk.Button(apf, text="登记审批/协同", command=self.on_add_approval).grid(
            row=2, column=0, columnspan=6, pady=3)

        tk.Label(right, text="实测证据录入", font=FONT_B).pack(anchor="w", pady=(6, 0))
        testf = tk.Frame(right)
        testf.pack(fill="x")
        self.tst_vars = {}
        for i, (key, label, default, width) in enumerate(
                (("room", "考场", "RM-101", 8), ("band", "频段", "gsm900", 10),
                 ("before", "屏蔽前", "-55", 7), ("after", "屏蔽后", "-90", 7),
                 ("rate", "有效率", "0.97", 6))):
            tk.Label(testf, text=label, font=FONT_S).grid(row=0, column=i * 2, sticky="e")
            var = tk.StringVar(value=default)
            tk.Entry(testf, textvariable=var, width=width).grid(row=0, column=i * 2 + 1,
                                                                padx=2)
            self.tst_vars[key] = var
        tk.Button(testf, text="登记屏蔽效果", command=self.on_add_shield_test).grid(
            row=0, column=10, padx=4)
        spillf = tk.Frame(right)
        spillf.pack(fill="x", pady=3)
        self.spill_vars = {}
        for i, (key, label, default, width) in enumerate(
                (("point", "测试点", "考点东门", 12), ("dist", "距离 m", "150", 7),
                 ("band", "频段", "gsm900", 9), ("dbm", "实测 dBm", "-93", 8))):
            tk.Label(spillf, text=label, font=FONT_S).grid(row=0, column=i * 2, sticky="e")
            var = tk.StringVar(value=default)
            tk.Entry(spillf, textvariable=var, width=width).grid(row=0, column=i * 2 + 1,
                                                                 padx=2)
            self.spill_vars[key] = var
        tk.Button(spillf, text="登记外溢实测", command=self.on_add_spill).grid(
            row=0, column=8, padx=4)
        tk.Label(right, text="最近屏蔽效果", font=FONT_B).pack(anchor="w", pady=(6, 0))
        self.tree_shield_tests = self._tree(
            right, ("room", "band", "atten", "rate", "result"),
            (70, 120, 60, 60, 60), height=4)
        tk.Label(right, text="最近外溢", font=FONT_B).pack(anchor="w", pady=(6, 0))
        self.tree_spills = self._tree(
            right, ("point", "dist", "dbm", "limit", "result"),
            (110, 60, 70, 70, 60), height=4)

    def refresh_compliance(self):
        chk = compliance.checks(self.store)
        self.var_compliance.set(chk["summary_text"])
        self.tree_compliance.delete(*self.tree_compliance.get_children())
        level_text = {"pass": "通过", "warn": "需关注", "fail": "不通过"}
        for i, r in enumerate(chk["rows"]):
            tag = r["level"]
            self.tree_compliance.insert("", "end", iid="K%d" % i, values=(
                r["category_text"], r["name"], level_text.get(tag, tag),
                _short(r["detail"], 220)), tags=(tag,))
        self.tree_compliance.tag_configure("fail", foreground=C_BAD)
        self.tree_compliance.tag_configure("warn", foreground=C_WARN)
        self.tree_compliance.tag_configure("pass", foreground=C_OK)
        rows = self.store.list_approvals(limit=60)
        self.tree_approvals.delete(*self.tree_approvals.get_children())
        for a in rows:
            self.tree_approvals.insert("", "end", iid=a.approval_id, values=(
                a.kind_text(), a.no, "%s~%s" % (a.valid_from, a.valid_to),
                a.status_text(), _short(a.doc, 50)))
        self.tree_shield_tests.delete(*self.tree_shield_tests.get_children())
        for t in self.store.list_shield_tests(limit=40):
            self.tree_shield_tests.insert("", "end", iid=t.test_id, values=(
                t.room_id or "-", config.band_name(t.band),
                "%.1f" % t.attenuation_db, "%.0f%%" % (t.rate * 100),
                "通过" if t.result == "pass" else "未达标"))
        self.tree_spills.delete(*self.tree_spills.get_children())
        for t in self.store.list_spills(limit=40):
            self.tree_spills.insert("", "end", iid=t.test_id, values=(
                t.point or "-", "%.0f" % t.distance_m, "%.1f" % t.measured_dbm,
                "%.0f" % t.limit_dbm,
                "通过" if t.result == "pass" else "超限"))

    def on_add_approval(self):
        from integration_api import op_add_approval
        r = op_add_approval({
            "kind": self.ap_vars["kind"].get() or "approval",
            "no": self.ap_vars["no"].get(), "org": self.ap_vars["org"].get(),
            "doc": self.ap_vars["doc"].get(),
            "valid_from": self.ap_vars["from"].get(),
            "valid_to": self.ap_vars["to"].get(),
            "site": self.cfg.get("site") or "",
            "operator": self.cfg.get("operator") or "界面"})
        if not r.get("ok"):
            messagebox.showerror("登记失败", r.get("reason") or "")
            return
        self._log("审批/协同已登记：%s" % self.ap_vars["no"].get())
        self.refresh_all()

    def on_add_shield_test(self):
        try:
            before = float(self.tst_vars["before"].get())
            after = float(self.tst_vars["after"].get())
            rate = float(self.tst_vars["rate"].get())
        except ValueError:
            messagebox.showerror("格式错误", "屏蔽前后/有效率必须是数字")
            return
        ok, info = shield.shield_test(self.store, self.tst_vars["room"].get(),
                                      self.tst_vars["band"].get(), before, after,
                                      rate=rate,
                                      operator=self.cfg.get("operator") or "界面")
        self._log("屏蔽效果实测：衰减 %.1f dB，%s"
                  % (info["attenuation_db"], "通过" if info["pass"] else "未达标"))
        self.refresh_all()

    def on_add_spill(self):
        jammers = self.store.list_devices(kind=config.DEV_JAMMER)
        if not jammers:
            messagebox.showinfo("提示", "先登记屏蔽器")
            return
        try:
            dist = float(self.spill_vars["dist"].get())
            dbm = float(self.spill_vars["dbm"].get())
        except ValueError:
            messagebox.showerror("格式错误", "距离/场强必须是数字")
            return
        ok, info = shield.spill_test(self.store, jammers[0],
                                     self.spill_vars["point"].get(), dist,
                                     self.spill_vars["band"].get(), dbm,
                                     operator=self.cfg.get("operator") or "界面")
        self._log("外溢实测：%.1f dBm（%s）" % (dbm, "通过" if info["pass"] else "超限"))
        self.refresh_all()

    # ============================================================== ⑦ 联动
    def _build_linkage(self):
        f = self.tab_linkage
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        tk.Button(top, text="刷新联动状态", command=self.on_link_status).pack(side="left")
        tk.Button(top, text="深度探测 02/03/06", command=self.on_link_deep).pack(side="left", padx=4)
        tk.Button(top, text="推送测试告警到 03", command=self.on_link_test_alert).pack(side="left", padx=4)
        tk.Button(top, text="关联 06 安检结论", command=self.on_correlate_06).pack(side="left", padx=4)
        tk.Button(top, text="关联 02 机位", command=self.on_correlate_02).pack(side="left", padx=4)
        tk.Button(top, text="推送开关记录到 03（跨机）",
                  command=self.on_push_records).pack(side="left", padx=4)

        self.txt_link = tk.Text(f, height=10, font=FONT_S, wrap="word")
        self.txt_link.pack(fill="x", padx=8)
        cfgf = tk.Frame(f)
        cfpf = cfgf
        cfpf.pack(fill="x", padx=8, pady=4)
        tk.Label(cfpf, text="03 接入地址", font=FONT).grid(row=0, column=0, sticky="e")
        self.var_terminal_url = tk.StringVar(value=self.cfg.get("terminal_url") or "")
        tk.Entry(cfpf, textvariable=self.var_terminal_url, width=34).grid(row=0, column=1, padx=3)
        tk.Label(cfpf, text="令牌", font=FONT).grid(row=0, column=2, sticky="e")
        self.var_terminal_token = tk.StringVar(value=self.cfg.get("terminal_token") or "")
        tk.Entry(cfpf, textvariable=self.var_terminal_token, width=24).grid(row=0, column=3, padx=3)
        tk.Label(cfpf, text="沙箱目录（自检用，留空=真实数据）", font=FONT).grid(
            row=1, column=0, columnspan=2, sticky="e")
        self.var_sandbox = tk.StringVar(value=self.cfg.get("sandbox_root") or "")
        tk.Entry(cfpf, textvariable=self.var_sandbox, width=44).grid(row=1, column=2,
                                                                    columnspan=2, padx=3,
                                                                    sticky="w")
        tk.Button(cfpf, text="保存联动设置", command=self.on_save_linkage).grid(
            row=0, column=4, rowspan=2, padx=6)

        tk.Label(f, text="联动关联记录", font=FONT_B).pack(anchor="w", padx=8, pady=(4, 0))
        cols = ("time", "source", "ref", "room", "detail")
        self.tree_correlations = self._tree(f, cols, (110, 60, 130, 70, 640), height=7)

    def refresh_linkage(self):
        st = linkage_bridge.link_status()["client"]
        lines = ["链路模式：%s ｜ 03 地址：%s ｜ 沙箱：%s"
                 % (st["mode"], st["terminal_url"], st["sandbox_root"]),
                 "00 集成联动：%s" % st["linkage_available"],
                 "本地设备/异常：%d 台设备、%d 条异常、%d 条关联记录"
                 % (len(self.store.list_devices()), len(self.store.list_anomalies(limit=500)),
                    len(self.store.list_correlations(limit=500)))]
        for key, mod in (st.get("modules") or {}).items():
            lines.append("  %s %s：%s（%s）"
                         % (key, mod.get("name"), "可用" if mod.get("available") else "不可用",
                            mod.get("reason")))
        lines.append("说明：07 只调用 00/02/03/06 的接口，不修改任何其它模块；"
                     "上报失败不影响本模块运行。")
        self.txt_link.configure(state="normal")
        self.txt_link.delete("1.0", "end")
        self.txt_link.insert("end", "\n".join(lines))
        self.txt_link.configure(state="disabled")
        rows = self.store.list_correlations(limit=120)
        self.tree_correlations.delete(*self.tree_correlations.get_children())
        for c in rows:
            self.tree_correlations.insert("", "end", iid=c.corr_id, values=(
                _fmt_epoch(c.created_epoch), c.source, c.ref_id, c.room_id or "-",
                _short(c.detail, 220)))

    def on_link_status(self):
        self._bg("刷新联动状态", lambda: linkage_bridge.link_status(),
                 lambda r: (self.refresh_linkage(), self._log(
                     "联动状态已刷新")))

    def on_link_deep(self):
        from linkage_client import status as link_status
        self._bg("深度探测 02/03/06", lambda: link_status(deep=True),
                 lambda r: self._log(
                     "深度探测完成：%s" % "、".join(
                         "%s=%s" % (k, "可用" if v.get("ok", v.get("available"))
                                    else "不可用")
                         for k, v in (r.get("modules") or {}).items())))

    def on_link_test_alert(self):
        def work():
            from linkage_client import raise_alert
            return raise_alert("SIGNAL_ABNORMAL", "RF-TEST", room_id="RM-101",
                               detail="07 联动测试：这是一条来自频谱监测的测试告警，"
                                      "用于确认 03 智能终端链路与分级展示",
                               level="orange", operator="07_rf/测试")
        self._bg("推送测试告警", work, lambda r: self._log(
            "测试告警：%s" % (r.get("reason") or ("alert_id=%s level=%s"
                                                % (r.get("alert_id"), r.get("level"))))))

    def on_push_records(self):
        def work():
            return linkage_bridge.push_records(self.store, which="switch", limit=50)
        self._bg("推送开关记录", work, lambda r: self._log(
            "推送结果：%s" % (r.get("reason") or "accepted=%s duplicated=%s"
                            % (r.get("accepted"), r.get("duplicated")))))

    def on_save_linkage(self):
        cfg = config.load_settings()
        cfg["terminal_url"] = self.var_terminal_url.get().strip()
        cfg["terminal_token"] = self.var_terminal_token.get().strip()
        cfg["sandbox_root"] = self.var_sandbox.get().strip()
        if config.save_settings({"terminal_url": cfg["terminal_url"],
                                 "terminal_token": cfg["terminal_token"],
                                 "sandbox_root": cfg["sandbox_root"]}):
            from linkage_client import reset as link_reset
            link_reset()
            self.cfg = config.load_settings()
            self._log("联动设置已保存")
            self.refresh_linkage()
        else:
            messagebox.showerror("保存失败", "写入 rf_settings.json 失败")

    # ========================================================== ⑧ 设置与审计
    def _build_settings(self):
        f = self.tab_settings
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="运行参数", font=FONT_B).pack(side="left")
        tk.Button(top, text="保存设置", command=self.on_save_settings).pack(side="right", padx=3)
        tk.Button(top, text="校验审计链", command=self.on_verify_chain).pack(side="right", padx=3)
        tk.Button(top, text="清理 7 天前扫描帧", command=self.on_purge).pack(side="right", padx=3)

        form = tk.Frame(f)
        form.pack(fill="x", padx=8)
        self.set_vars = {}
        t = config.thresholds(self.cfg)
        specs = [("site", "考点", self.cfg.get("site") or "", 20),
                 ("operator", "默认操作员", self.cfg.get("operator") or "", 16),
                 ("pre_open", "开考前窗口（分钟）",
                  str(models.to_int(self.cfg.get("pre_open_sec"), 900) // 60), 6),
                 ("max_power", "屏蔽功率上限 W", "%.1f" % self.cfg.get("max_power_w", 5), 6),
                 ("spill", "外溢限值 dBm", "%.0f" % self.cfg.get("spill_limit_dbm", -85), 6),
                 ("margin", "噪声门限 dB", "%.1f" % t["noise_margin_db"], 6),
                 ("min_snr", "最小信噪比 dB", "%.1f" % t["min_snr_db"], 6),
                 ("self", "屏蔽自激抬高 dB", "%.1f" % t["self_suppress_db"], 6),
                 ("wideband", "宽带判定 MHz", "%.0f" % t["wideband_mhz"], 6),
                 ("sustain", "持续判定帧数", "%d" % t["sustain_frames"], 6)]
        for i, (key, label, default, width) in enumerate(specs):
            row, col = i // 3, i % 3
            tk.Label(form, text=label, font=FONT_S).grid(row=row * 2, column=col,
                                                         sticky="w", padx=4, pady=(4, 0))
            var = tk.StringVar(value=default)
            tk.Entry(form, textvariable=var, width=width).grid(row=row * 2 + 1, column=col,
                                                               sticky="w", padx=4)
            self.set_vars[key] = var
        flags = tk.Frame(form)
        flags.grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        self.var_approval_req = tk.BooleanVar(value=bool(self.cfg.get("approval_required", True)))
        self.var_auto_shield = tk.BooleanVar(value=bool(self.cfg.get("auto_shield", True)))
        self.var_auto_off = tk.BooleanVar(value=bool(self.cfg.get("auto_off", True)))
        self.var_report_to = tk.BooleanVar(value=bool(self.cfg.get("report_to_terminal", False)))
        for text, var in (("必须审批/报备", self.var_approval_req),
                          ("按时间表自动开启", self.var_auto_shield),
                          ("结束自动关闭", self.var_auto_off),
                          ("异常自动上报 03", self.var_report_to)):
            tk.Checkbutton(flags, text=text, variable=var).pack(side="left", padx=6)

        tk.Label(f, text="审计链（哈希链，任何改动都会暴露）", font=FONT_B).pack(
            anchor="w", padx=8, pady=(8, 0))
        cols = ("time", "action", "detail", "operator", "hash")
        self.tree_audits = self._tree(f, cols, (120, 120, 520, 100, 120), height=10)
        self.var_chain = tk.StringVar(value="")
        tk.Label(f, textvariable=self.var_chain, font=FONT_S, fg=C_DIM).pack(anchor="w",
                                                                             padx=8)

    def refresh_settings(self):
        rows = self.store.list_audits(limit=200)
        self.tree_audits.delete(*self.tree_audits.get_children())
        for a in rows:
            self.tree_audits.insert("", "end", iid=a.audit_id, values=(
                a.ts, a.action, _short(a.detail, 220), a.operator or "-",
                (a.hash or "")[:12]))
        ok, n, msg = self.store.verify_chain()
        self.var_chain.set(("✓ " if ok else "✗ ") + msg)

    def on_save_settings(self):
        def num(key, default):
            try:
                return float(self.set_vars[key].get())
            except (KeyError, ValueError):
                return default
        t = config.thresholds(self.cfg)
        th = {"noise_margin_db": num("margin", t["noise_margin_db"]),
              "min_snr_db": num("min_snr", t["min_snr_db"]),
              "self_suppress_db": num("self", t["self_suppress_db"]),
              "wideband_mhz": num("wideband", t["wideband_mhz"]),
              "sustain_frames": int(num("sustain", t["sustain_frames"]))}
        values = {"site": self.set_vars["site"].get().strip(),
                  "operator": self.set_vars["operator"].get().strip(),
                  "pre_open_sec": int(num("pre_open", 15) * 60),
                  "max_power_w": num("max_power", 5.0),
                  "spill_limit_dbm": num("spill", -85.0),
                  "approval_required": bool(self.var_approval_req.get()),
                  "auto_shield": bool(self.var_auto_shield.get()),
                  "auto_off": bool(self.var_auto_off.get()),
                  "report_to_terminal": bool(self.var_report_to.get()),
                  "thresholds": th}
        if not config.save_settings(values):
            messagebox.showerror("保存失败", "写入 rf_settings.json 失败")
            return
        self.cfg = config.load_settings()
        self.var_site.set("考点：%s" % self.cfg.get("site", ""))
        self._log("设置已保存（功率上限 %.1fW、门限 %.1fdB、自动开启=%s）"
                  % (self.cfg["max_power_w"], th["noise_margin_db"],
                     self.cfg["auto_shield"]))
        self.refresh_all()

    def on_verify_chain(self):
        ok, n, msg = self.store.verify_chain()
        self.var_chain.set(("✓ " if ok else "✗ ") + msg)
        messagebox.showinfo("审计链校验", msg)

    def on_purge(self):
        if not messagebox.askyesno("确认", "清理 7 天前的扫描帧/异常？\n（审批、实测证据、"
                                            "开关记录不清理）"):
            return
        r = self.store.purge_runtime(keep_days=7)
        self._log("数据维护：清理扫描 %d 帧、异常 %d 条" % (r["scans"], r["anomalies"]))
        self.refresh_all()

    # ============================================================== 一键动作
    def on_seed_demo(self):
        r = samples.seed_demo(self.store)
        self._log("演示数据：设备 %d、场次 %d、审批 %d、屏蔽实测 %d、外溢 %d"
                  % (r["devices"], r["fields"], r["approvals"],
                     r["shield_tests"], r["spills"]))
        self.refresh_all()

    def on_replay(self):
        self.var_status.set("正在跑回放测试…")
        self._bg("回放测试", lambda: samples.run_replay(self.store, operator="界面"),
                 self._after_replay)

    def _after_replay(self, r):
        if not r.get("ok"):
            messagebox.showerror("回放失败", r.get("reason") or "")
            return
        self._log("回放：检出率 %.0f%%、误报 %d、平均时延 %.1fs、定位误差 %.1fm"
                  % (float(r.get("detect_rate") or 0) * 100,
                     r.get("false_alarm") or 0, float(r.get("avg_latency") or 0),
                     float((r.get("metrics") or {}).get("locate_error_avg_m") or 0)))
        for it in r.get("items") or []:
            self._log("  %s %s：%s" % (it["scenario_id"], it["name"],
                                       "通过" if it["ok"] else "不通过"))
        self.refresh_all()

    def on_report(self):
        self.var_status.set("正在生成验收报告…")
        self._bg("生成验收报告",
                 lambda: report_mod.write_report(self.store,
                                                 operator=self.cfg.get("operator") or "界面"),
                 self._after_report)

    def _after_report(self, r):
        if not r.get("ok"):
            messagebox.showerror("生成报告失败", r.get("reason") or "")
            return
        self._log("报告已生成：%s" % r.get("md"))
        self._log("结论：%s" % r.get("conclusion"))
        messagebox.showinfo("验收报告", "已生成：\n%s\n%s\n\n结论：%s"
                            % (r.get("md"), r.get("csv"), r.get("conclusion")))
        self.refresh_all()

    def on_open_reports(self):
        try:
            os.startfile(str(config.REPORT_DIR))
        except Exception as e:                                # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def on_full_demo(self):
        if not messagebox.askyesno("一键演练", "将载入演示数据并跑回放测试（模拟数据），继续？"):
            return
        self.on_seed_demo()
        self.on_replay()

    # ============================================================== 通用
    def _tree(self, parent, columns, widths, height=8, show="headings"):
        frame = tk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show=show, height=height)
        for col, width in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w", stretch=False)
        ysb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _ask_text(self, title, prompt, default=""):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text=prompt).pack(padx=10, pady=6)
        var = tk.StringVar(value=default)
        ent = tk.Entry(dlg, textvariable=var, width=60)
        ent.pack(padx=10, pady=4)
        ent.focus_set()
        result = {"value": None}

        def ok():
            result["value"] = var.get()
            dlg.destroy()

        def cancel():
            dlg.destroy()
        btns = tk.Frame(dlg)
        btns.pack(pady=6)
        tk.Button(btns, text="确定", command=ok, width=10).pack(side="left", padx=4)
        tk.Button(btns, text="取消", command=cancel, width=10).pack(side="left", padx=4)
        dlg.wait_window()
        return result["value"]

    # ============================================================== 刷新
    def refresh_all(self):
        try:
            self.cfg = config.load_settings()
            self.refresh_overview()
            self.refresh_spectrum()
            self.refresh_anomalies()
            self.refresh_shield()
            self.refresh_schedule()
            self.refresh_compliance()
            self.refresh_linkage()
            self.refresh_settings()
            self.var_clock.set(time.strftime("%Y-%m-%d %H:%M:%S"))
            st = linkage_bridge.link_status()["client"]
            self.var_link.set("联动：%s ｜ 00=%s ｜ 03=%s"
                              % (st["mode"], "可用" if st["linkage_available"] else "不可用",
                                 "可用" if (st["modules"].get("03") or {}).get(
                                     "available") else "不可用"))
        except Exception as e:                                # noqa: BLE001
            self.var_status.set("刷新失败：%s" % e)

    def on_close(self):
        self._stop = True
        if self._monitor_job is not None:
            try:
                self.root.after_cancel(self._monitor_job)
            except Exception:
                pass
        for fn in (self.store.close,):
            try:
                fn()
            except Exception:
                pass
        self.root.destroy()


def main():
    config.ensure_stdio()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
