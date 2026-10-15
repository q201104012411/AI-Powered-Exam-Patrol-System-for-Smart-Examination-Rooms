# -*- coding: utf-8 -*-
r"""
主界面

布局：
    顶栏    开始/停止监控 · 模拟火警（演练） · 后台运行 · 设置 · 状态灯
    左侧    摄像头（地址/预置/探测本机设备）· 参数面板 · 事件历史
    右侧    实时画面（带检测框）· 状态栏 · 当前信号分解 · 证据面板

线程约定（别改乱）：
    · tkinter 只能在主线程操作。采集/检测/证据三条线程**一律不碰控件**，
      它们只往 queue 里放消息，由本文件的 `_tick()` 在主线程里取出来处理。
    · 关闭窗口 ≠ 退出程序：默认"最小化到托盘继续盯"，只有托盘的"退出程序"
      或菜单里的退出才真的结束（否则关掉窗口就没人看着了，这是安全底线）。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import config
import detect as detect_mod
import detector as detector_mod
import models
import storage
import tray as tray_mod
import video as video_mod
from ui_popup import AlarmPopup

#: 界面配色
BG = "#11161d"
PANEL = "#182029"
PANEL2 = "#1e2933"
FG = "#dfe7ee"
DIM = "#8b9aa8"
ACCENT = "#2f81f7"
OK_GREEN = "#2ea043"
WARN = "#d29922"
ALARM = "#da3633"

STATE_COLOR = {"idle": DIM, "live": OK_GREEN, "connecting": WARN,
               "stalled": ALARM, "error": ALARM, "stopped": DIM}


class FireApp(object):
    """主窗口与全部业务装配。"""

    def __init__(self, cfg=None, hidden=False, simulate=False):
        self.cfg = cfg or config.load_settings()
        self.store = storage.EventStore()
        self.recorder = video_mod.EvidenceRecorder(self.cfg, self.store)
        self.detector = None
        self.detector_note = ""
        self.worker = None
        self.tray = None
        self.popup = None
        self.simulator = None
        self._closing = False
        self._sim_until = 0.0
        self._last_render = 0.0
        self._last_det_seq = -1
        self._photo = None
        self._img_item = None
        self._event_rows = []

        self._build_backend()
        self._build_ui(hidden)

    # ============================================================ 装配
    def _build_backend(self):
        """装配检测器（带确认闸门）——后端选择由 detector.py 决定并如实说明。"""
        on_alarm, on_clear = self._make_callbacks()
        self.detector, self.detector_note = detector_mod.build_detector(
            self.cfg, on_alarm=on_alarm, on_clear=on_clear)
        config.log("检测后端：%s" % self.detector_note)

    def _make_callbacks(self):
        """
        检测器回调在**采集线程**里执行：只允许入队 + 落库，绝不碰控件。
        真正的截图/录像由证据线程做，弹窗由主线程做。
        """
        ui_q = queue.Queue()

        def on_alarm(det, ev):
            try:
                self.store.save_event(ev)
                self.store.log("火警触发", "%s %s 峰值 %.2f" %
                               (ev.event_id, ev.level_name(), ev.peak_score), "detector")
            except Exception as exc:                        # noqa: BLE001
                config.log("事件落库失败：%s" % str(exc)[:120])
            if self.recorder is not None:
                self.recorder.capture_event(ev, None)
            ui_q.put(("alarm", ev, det))

        def on_clear(ev):
            try:
                self.store.save_event(ev)
                self.store.log("火警解除", "%s 持续 %.1fs" % (ev.event_id, ev.duration_sec),
                               "detector")
            except Exception:                               # noqa: BLE001
                pass
            if self.recorder is not None:
                self.recorder.close_event(ev.event_id, save_clip=True)
            ui_q.put(("clear", ev, None))

        self._ui_q = ui_q
        return on_alarm, on_clear

    # ============================================================ 界面
    def _build_ui(self, hidden):
        self.root = tk.Tk()
        self.root.title("AI 火场识别与自动报警 · 智能考场安全底座")
        self.root.geometry("1180x740+40+20")
        self.root.minsize(980, 620)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.bind("<F5>", lambda e: self.toggle_monitor())

        self._build_toolbar()
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        self._build_left(body)
        self._build_right(body)
        self._build_statusbar()

        # 事件弹窗（复用同一个实例：多次报警追加到同一窗，不叠一堆窗）
        self.popup = AlarmPopup(self.root, app=self, on_ack=self._ack_event,
                                on_false=self._false_event,
                                on_open_dir=self._open_event_dir,
                                on_dismiss=lambda: None)

        # 定时器
        self.root.after(33, self._tick)
        self.root.after(1000, self._slow_tick)

        self._setup_tray()
        # 后台运行：窗口保持隐藏（不是"建好再藏"——那会闪一下，也可能被用户看到）。
        # 早先的 `if not hidden: deiconify()` 有个坑：Tk 根窗口**默认就是可见的**，
        # 不 deiconify 并不等于隐藏，必须显式 withdraw()。
        if hidden:
            self.root.withdraw()
            self._hidden_at_start = True
        else:
            self.root.deiconify()
            self._hidden_at_start = False
        if self.cfg["auto_start_monitor"]:
            self.root.after(400, self.start_monitor)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=PANEL, height=54)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        self.btn_mon = tk.Button(bar, text="▶ 开始监控", command=self.toggle_monitor,
                                 bg=OK_GREEN, fg="white", relief="flat",
                                 font=("Microsoft YaHei", 11, "bold"), width=14)
        self.btn_mon.pack(side="left", padx=(10, 6), pady=10)

        tk.Button(bar, text="🔥 模拟一次火警", command=self.simulate_alarm,
                  bg="#8a4b00", fg="white", relief="flat",
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=6, pady=10)
        tk.Button(bar, text="🗕 后台运行", command=self.hide_to_background,
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=6, pady=10)
        tk.Button(bar, text="⚙ 设置", command=self.open_settings,
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=6, pady=10)
        tk.Button(bar, text="📂 事件留档", command=lambda: self._open_path(str(config.EVENT_DIR)),
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=6, pady=10)

        right = tk.Frame(bar, bg=PANEL)
        right.pack(side="right", padx=10)
        self.lamp = tk.Canvas(right, width=16, height=16, bg=PANEL, highlightthickness=0)
        self.lamp.pack(side="left", padx=(0, 6))
        self._lamp_id = self.lamp.create_oval(2, 2, 14, 14, fill=DIM, outline="")
        self.lbl_state = tk.Label(right, text="未监控", bg=PANEL, fg=FG,
                                  font=("Microsoft YaHei", 11, "bold"))
        self.lbl_state.pack(side="left")

    def _build_left(self, parent):
        wrap = tk.Frame(parent, bg=BG, width=360)
        wrap.pack(side="left", fill="y", padx=(8, 4), pady=8)
        wrap.pack_propagate(False)

        # ---- 摄像头 ----
        cam = tk.LabelFrame(wrap, text=" 摄像头 ", bg=PANEL, fg=FG,
                            font=("Microsoft YaHei", 10, "bold"),
                            labelanchor="nw", bd=1, relief="solid")
        cam.pack(fill="x")

        tk.Label(cam, text="名称", bg=PANEL, fg=DIM,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 0))
        self.var_name = tk.StringVar(value=self.cfg["camera_name"])
        tk.Entry(cam, textvariable=self.var_name, bg="#0d1319", fg=FG, insertbackground=FG,
                 relief="flat", font=("Microsoft YaHei", 10)).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=8, pady=(6, 0))

        tk.Label(cam, text="地址", bg=PANEL, fg=DIM,
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=8, pady=(6, 0))
        self.var_url = tk.StringVar(value=str(self.cfg["camera_url"]))
        ent = tk.Entry(cam, textvariable=self.var_url, bg="#0d1319", fg=FG,
                       insertbackground=FG, relief="flat", font=("Consolas", 10))
        ent.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=(6, 0))

        hint = tk.Label(cam, text="", bg=PANEL, fg="#7d8b98",
                        font=("Microsoft YaHei", 8), justify="left", anchor="w")
        hint.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8)
        self.lbl_cam_hint = hint
        self.var_url.trace_add("write", lambda *_: self._update_cam_hint())
        self._update_cam_hint()

        row = tk.Frame(cam, bg=PANEL)
        row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        tk.Button(row, text="探测本机摄像头", command=self.probe_cameras,
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(row, text="选择视频文件", command=self.pick_file,
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        cam.columnconfigure(1, weight=1)

        # ---- 参数 ----
        par = tk.LabelFrame(wrap, text=" 检测参数（可现场标定） ", bg=PANEL, fg=FG,
                            font=("Microsoft YaHei", 10, "bold"),
                            labelanchor="nw", bd=1, relief="solid")
        par.pack(fill="x", pady=(8, 0))

        self.var_sens = tk.DoubleVar(value=float(self.cfg["alarm_threshold"]))
        self._slider(par, 0, "火警阈值", self.var_sens, 0.20, 0.95, 0.01,
                     "分数 ≥ 该值判「确认火警」并弹窗")
        self.var_conf = tk.IntVar(value=int(self.cfg["confirm_frames"]))
        self._slider(par, 1, "确认帧数", self.var_conf, 1, 20, 1,
                     "连续多少帧命中才报警（越小越灵敏、越易误报）")
        self.var_interval = tk.IntVar(value=int(self.cfg["detect_interval_ms"]))
        self._slider(par, 2, "检测间隔", self.var_interval, 50, 1000, 10,
                     "毫秒；越小越灵敏、越吃 CPU", fmt="%d ms")

        row2 = tk.Frame(par, bg=PANEL)
        row2.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        self.var_sound = tk.BooleanVar(value=bool(self.cfg["sound_enable"]))
        tk.Checkbutton(row2, text="报警声", variable=self.var_sound, bg=PANEL, fg=FG,
                       selectcolor=PANEL2, activebackground=PANEL,
                       font=("Microsoft YaHei", 9),
                       command=self.apply_params).pack(side="left")
        self.var_popw = tk.BooleanVar(value=bool(self.cfg["popup_on_warning"]))
        tk.Checkbutton(row2, text="疑似也弹窗", variable=self.var_popw, bg=PANEL, fg=FG,
                       selectcolor=PANEL2, activebackground=PANEL,
                       font=("Microsoft YaHei", 9),
                       command=self.apply_params).pack(side="left", padx=8)
        tk.Button(row2, text="恢复默认", command=self.reset_params, bg=PANEL2, fg=FG,
                  relief="flat", font=("Microsoft YaHei", 9)).pack(side="right")

        # ---- 事件历史 ----
        ev = tk.LabelFrame(wrap, text=" 事件历史 ", bg=PANEL, fg=FG,
                           font=("Microsoft YaHei", 10, "bold"),
                           labelanchor="nw", bd=1, relief="solid")
        ev.pack(fill="both", expand=True, pady=(8, 0))

        cols = ("time", "level", "score", "status")
        self.tree = ttk.Treeview(ev, columns=cols, show="headings", height=8)
        for c, txt, w in (("time", "时间", 74), ("level", "级别", 78),
                          ("score", "峰值", 52), ("status", "状态", 68)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w", stretch=False)
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_pick)
        self.tree.tag_configure("alarm", background="#2a0d0d", foreground="#ff9a9a")
        self.tree.tag_configure("warning", background="#2a2109", foreground="#ffd88a")

        btns = tk.Frame(ev, bg=PANEL)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(btns, text="打开选中事件证据", command=self._open_selected,
                  bg=PANEL2, fg=FG, relief="flat",
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(btns, text="刷新", command=self.refresh_events, bg=PANEL2, fg=FG,
                  relief="flat", font=("Microsoft YaHei", 9)).pack(side="right")

    def _slider(self, parent, row, label, var, lo, hi, step, tip, fmt="%.2f"):
        tk.Label(parent, text=label, bg=PANEL, fg=DIM, font=("Microsoft YaHei", 9),
                 width=8, anchor="w").grid(row=row, column=0, sticky="w", padx=(8, 0))
        val = tk.Label(parent, text="", bg=PANEL, fg=FG, width=7, anchor="e",
                       font=("Consolas", 10))
        val.grid(row=row, column=2, sticky="e", padx=(0, 8))

        def show(*_a):
            val.configure(text=fmt % var.get())
        s = tk.Scale(parent, from_=lo, to=hi, resolution=step, orient="horizontal",
                     variable=var, bg=PANEL, fg=FG, troughcolor=PANEL2,
                     highlightthickness=0, showvalue=False, command=lambda _v: (show(), self.apply_params()))
        s.grid(row=row, column=1, sticky="ew", padx=6, pady=(2, 0))
        parent.columnconfigure(1, weight=1)
        show()
        tk.Label(parent, text=tip, bg=PANEL, fg="#6f7d8a", font=("Microsoft YaHei", 8),
                 anchor="w").grid(row=row + 100, column=1, columnspan=2, sticky="ew", padx=6)
        return s

    def _build_right(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        self.canvas = tk.Canvas(wrap, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(16, 16, anchor="nw", fill="#6f7d8a",
                                text="尚未开始监控。点左上角「▶ 开始监控」，\n"
                                     "或先选好摄像头地址（本机摄像头填 0）。",
                                font=("Microsoft YaHei", 12), tags=("hint",))
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")

        info = tk.Frame(wrap, bg=PANEL, height=132)
        info.pack(fill="x", pady=(8, 0))
        info.pack_propagate(False)
        self.txt_info = tk.Text(info, bg=PANEL, fg=FG, relief="flat", height=7,
                                font=("Consolas", 10), highlightthickness=0)
        self.txt_info.pack(fill="both", expand=True, padx=8, pady=6)
        self.txt_info.insert("1.0", "信号分解会在开始监控后显示在这里。")
        self.txt_info.configure(state="disabled")

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=PANEL, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.lbl_status = tk.Label(bar, text="就绪", bg=PANEL, fg=DIM, anchor="w",
                                   font=("Microsoft YaHei", 9))
        self.lbl_status.pack(side="left", padx=10)
        self.lbl_backend = tk.Label(bar, text="", bg=PANEL, fg=DIM, anchor="e",
                                    font=("Microsoft YaHei", 9))
        self.lbl_backend.pack(side="right", padx=10)
        self.lbl_backend.configure(text="检测后端：%s" % self.detector_note)

    # ============================================================ 托盘
    def _setup_tray(self):
        if not self.cfg["use_tray"]:
            return
        ok, why = tray_mod.available()
        if not ok:
            config.log("托盘不可用：%s" % why)
            return
        self.tray = tray_mod.TrayIcon(tooltip="AI 火场识别 · 正在盯画面",
                                      on_command=self._on_tray_command)
        if self.tray.start():
            config.log("托盘图标已就绪")
        else:
            self.tray = None
            config.log("托盘图标创建失败，降级为普通窗口模式")

    def _on_tray_command(self, action):
        """托盘菜单回调在**托盘线程**里，必须切回 UI 线程再动 tkinter。"""
        try:
            self.root.after(0, lambda: self._tray_action(action))
        except Exception:                                   # noqa: BLE001
            pass

    def _tray_action(self, action):
        if action == "show":
            self.show_window()
        elif action == "toggle":
            self.toggle_monitor()
        elif action == "test":
            self.simulate_alarm()
        elif action == "quit":
            self.quit_app()
        return True

    # ============================================================ 监控
    def toggle_monitor(self):
        if self.worker is not None and self.worker.running:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self):
        if self.worker is not None and self.worker.running:
            return
        self.apply_params(save=True)
        self.cfg = config.load_settings()
        # 地址变了要重建检测器历史（否则旧画面的闪烁历史会污染新机位）
        if self.detector is None:
            self._build_backend()
        self.detector.reset()
        self.recorder.cfg = self.cfg
        self.recorder.start()
        self.worker = video_mod.VideoWorker(self.cfg, store=self.store,
                                            detector=self.detector,
                                            recorder=self.recorder)
        self.worker.start()
        self.btn_mon.configure(text="■ 停止监控", bg=ALARM)
        self._set_state("connecting", "连接中…")
        if self.tray:
            self.tray.update("AI 火场识别 · 监控中")

    def stop_monitor(self):
        if self.worker is not None:
            self.worker.stop("用户停止")
        self.btn_mon.configure(text="▶ 开始监控", bg=OK_GREEN)
        self._set_state("stopped", "已停止")

    def apply_params(self, save=False):
        """把界面上的参数写回设置；save=True 时落盘。"""
        patch = {"camera_name": self.var_name.get().strip() or "未命名机位",
                 "camera_url": self.var_url.get().strip() or "0",
                 "alarm_threshold": round(float(self.var_sens.get()), 2),
                 "confirm_frames": int(self.var_conf.get()),
                 "detect_interval_ms": int(self.var_interval.get()),
                 "sound_enable": bool(self.var_sound.get()),
                 "popup_on_warning": bool(self.var_popw.get())}
        if save:
            ok, msg = config.save_settings(patch)
            if not ok:
                self._status("设置保存失败：%s" % msg, ALARM)
        self.cfg.update(config.coerce_settings(dict(self.cfg, **patch)))
        if self.detector is not None:
            self.detector.cfg = self.cfg
            if getattr(self.detector.backend, "cfg", None) is not None:
                self.detector.backend.cfg = self.cfg
        if self.popup is not None:
            self.popup.cfg = self.cfg

    def reset_params(self):
        cfg = dict(config.SETTINGS_DEFAULT)
        self.var_sens.set(cfg["alarm_threshold"])
        self.var_conf.set(cfg["confirm_frames"])
        self.var_interval.set(cfg["detect_interval_ms"])
        self.var_sound.set(cfg["sound_enable"])
        self.var_popw.set(cfg["popup_on_warning"])
        config.save_settings(cfg)
        self.cfg = config.load_settings()
        self._status("已恢复默认参数", OK_GREEN)

    # ============================================================ 定时器
    def _tick(self):
        """主循环：取事件、刷画面。全部非阻塞。"""
        if self._closing:
            return
        try:
            self._drain_ui_events()
            self._render_frame()
        except Exception as exc:                            # noqa: BLE001
            config.log("界面刷新异常：%s: %s" % (type(exc).__name__, str(exc)[:160]))
        self.root.after(int(self.cfg.get("render_interval_ms") or 60), self._tick)

    def _slow_tick(self):
        """每秒一次：状态灯、托盘提示、其他轻量活。"""
        if self._closing:
            return
        try:
            self._update_state_lamp()
            self._maybe_stop_simulation()
            if self.tray and self.worker is not None and self.worker.running:
                snap = self.worker.snapshot()
                st = snap.get("detector_status") or ""
                self.tray.update("AI 火场识别 · %s" % st[:100])
        except Exception:                                   # noqa: BLE001
            pass
        self.root.after(1000, self._slow_tick)

    def _drain_ui_events(self):
        """把采集/证据线程抛过来的消息在主线程里处理。"""
        handled = 0
        while handled < 20:
            try:
                kind, payload, extra = self._ui_q.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if kind == "alarm":
                self._on_alarm(payload, extra)
            elif kind == "clear":
                self._on_clear(payload)

    def _on_alarm(self, ev, det):
        # 证据：抓拍 + 标注图（交给证据线程，界面不等）
        if self.recorder is not None:
            frame = None
            if self.worker is not None:
                frame = self.worker.snapshot().get("frame")
                if frame is not None:
                    frame = frame.copy()
            self.recorder.capture_event(ev, frame)
        self.refresh_events()
        self._status("⚠ %s：%s（分值 %.2f，%s）"
                     % (ev.level_name(), ev.camera_name or ev.camera_url,
                        ev.score, ev.reason), ALARM)
        self._set_state("error" if ev.level == config.LEVEL_ALARM else "connecting",
                        ev.level_name())
        if self.tray:
            self.tray.update("⚠ %s · %s" % (ev.level_name(), ev.camera_name),
                             level=ev.level)
        want_popup = (ev.level == config.LEVEL_ALARM) or bool(self.cfg["popup_on_warning"])
        if want_popup:
            frame = None
            if self.worker is not None:
                frame = self.worker.snapshot().get("frame")
            self.popup.show(ev, frame, det)
        else:
            config.log("疑似火情未弹窗（设置里「疑似也弹窗」未开）：%s" % ev.event_id)

    def _on_clear(self, ev):
        self.refresh_events()
        self._status("火警已解除：%s（持续 %.1f 秒）"
                     % (ev.camera_name or ev.camera_url, ev.duration_sec), OK_GREEN)
        if self.popup is not None:
            self.popup.notify_clear(ev)
        if self.tray:
            self.tray.update("AI 火场识别 · 监控中", level="idle")

    def _render_frame(self):
        sim = getattr(self.simulator, "_last_frame", None) if self.simulator else None
        if self.worker is None or not self.worker.running:
            # 没有在监控时，演练合成画面也要能看见（否则弹窗里是黑屏，没法判断）
            if sim is not None:
                self._show_frame(detect_mod.draw_overlay(
                    sim, getattr(self, "_sim_det", None), self.cfg,
                    extra_lines=["演练合成源（非真实摄像头）"]))
                self._set_state("connecting", "演练中")
            return
        snap = self.worker.snapshot()
        frame = snap.get("frame")
        if frame is None:
            if sim is not None:                     # 演练期间 worker 还没帧
                self._show_frame(detect_mod.draw_overlay(
                    sim, getattr(self, "_sim_det", None), self.cfg,
                    extra_lines=["演练合成源（非真实摄像头）"]))
                self._set_state("connecting", "演练中")
                return
            cam = snap.get("cam") or {}
            self._set_state(cam.get("state", "connecting"), cam.get("status", ""))
            return
        det = snap.get("det")
        cfg = self.cfg
        shown = detect_mod.draw_overlay(
            frame, det, cfg,
            extra_lines=["%s ｜ %s" % (cfg["camera_name"], cfg["camera_url"]),
                         "检测次数 %d ｜ 后端 %s"
                         % (snap.get("detect_count", 0),
                            getattr(self.detector.backend, "name", "?"))])
        self._show_frame(shown)
        cam = snap.get("cam") or {}
        self._set_state(cam.get("state", "live"),
                        "%s ｜ %.1f fps" % (cam.get("status", ""), cam.get("fps", 0.0)))
        # 详情面板：每 4 次刷新一次即可，避免刷屏
        if snap.get("detect_count", 0) != self._last_det_seq:
            self._last_det_seq = snap.get("detect_count", 0)
            self._update_info(snap)

    def _show_frame(self, shown):
        try:
            from PIL import Image, ImageTk
            import cv2
        except ImportError:
            return
        cw = max(320, self.canvas.winfo_width())
        ch = max(240, self.canvas.winfo_height())
        h, w = shown.shape[:2]
        scale = min(cw / float(w), ch / float(h))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        resized = cv2.resize(shown, (nw, nh), interpolation=interp)
        img = Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfigure(self._img_item, image=self._photo)
        self.canvas.coords(self._img_item, (cw - nw) // 2, (ch - nh) // 2)
        self.canvas.delete("hint")

    def _update_info(self, snap):
        det = snap.get("det")
        cam = snap.get("cam") or {}
        lines = []
        if det is not None:
            lines.append("判定      : %s（%.3f）" % (det.level_name(), det.score))
            lines.append("火色占比  : %.3f%%    烟雾占比: %.3f%%"
                         % (det.fire_ratio * 100, det.smoke_ratio * 100))
            lines.append("火色区域  : %d 个    清晰度: %.0f"
                         % (len(det.regions), det.sharpness))
            parts = "  ".join("%s=%.3f" % (k, v) for k, v in sorted(
                (det.parts or {}).items(), key=lambda kv: -kv[1]) if v > 0)
            lines.append("信号分解  : %s" % (parts or "（全 0）"))
            if det.notes:
                lines.append("说明      : %s" % "；".join(det.notes[:2]))
        lines.append("检测器    : %s" % (snap.get("detector_status") or ""))
        lines.append("取流      : %s ｜ %.1f fps ｜ 重连 %d 次"
                     % (cam.get("status", ""), cam.get("fps", 0.0),
                        cam.get("reconnects", 0)))
        ev_stat = self.recorder.stats() if self.recorder else {}
        lines.append("证据线程  : 队列 %s ｜ 进行中录像 %s ｜ 已写帧 %s"
                     % (ev_stat.get("queued"), ev_stat.get("open_clips"),
                        ev_stat.get("frames_written")))
        if snap.get("error"):
            lines.append("错误      : %s" % snap["error"])
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", "end")
        self.txt_info.insert("1.0", "\n".join(lines))
        self.txt_info.configure(state="disabled")

    def _set_state(self, state, text):
        self._state = state
        try:
            self.lamp.itemconfigure(self._lamp_id, fill=STATE_COLOR.get(state, DIM))
            label = {"idle": "未监控", "stopped": "已停止", "live": "监控中",
                     "connecting": "连接中", "stalled": "画面中断", "error": "异常"}.get(state, state)
            if text:
                label = "%s（%s）" % (label, text) if state in ("stalled", "error") else label
            self.lbl_state.configure(text=label)
        except tk.TclError:
            pass

    def _update_state_lamp(self):
        if self.worker is None or not self.worker.running:
            if getattr(self, "_state", "") not in ("stopped",):
                self._set_state("stopped", "已停止")
            return
        snap = self.worker.snapshot()
        det = snap.get("det")
        if det is not None and det.level == config.LEVEL_ALARM:
            self._set_state("error", "确认火警")
        elif det is not None and det.level == config.LEVEL_WARNING:
            self._set_state("connecting", "疑似火情")
        else:
            cam = snap.get("cam") or {}
            self._set_state(cam.get("state", "live"), "")

    def _status(self, text, color=DIM):
        try:
            self.lbl_status.configure(text=text, fg=color)
        except tk.TclError:
            pass

    # ============================================================ 事件操作
    def refresh_events(self):
        try:
            events = self.store.list_events(limit=120)
        except Exception as exc:                            # noqa: BLE001
            config.log("读取事件失败：%s" % str(exc)[:120])
            return
        self._event_rows = events
        tree = self.tree
        tree.delete(*tree.get_children())
        for e in events:
            tag = "alarm" if e.level == config.LEVEL_ALARM else "warning"
            tree.insert("", "end", iid=e.event_id,
                        values=(e.created_at[11:19], e.level_name(),
                                "%.2f" % (e.peak_score or e.score), e.status_text()),
                        tags=(tag,))

    def _selected_event(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.store.get_event(sel[0])

    def _on_tree_pick(self, _e=None):
        ev = self._selected_event()
        if ev is None:
            return
        d = "%s ｜ %s ｜ 峰值 %.2f ｜ 火色 %.2f%% ｜ %s" % (
            ev.event_id, ev.level_name(), ev.peak_score or ev.score,
            ev.fire_ratio * 100, ev.reason)
        self._status(d, DIM)

    def _open_selected(self):
        ev = self._selected_event()
        if ev is None:
            messagebox.showinfo("提示", "先在列表里选一条事件", parent=self.root)
            return
        self._open_event_dir(ev)

    def _open_event_dir(self, ev):
        d = os.path.join(str(config.EVENT_DIR), ev.event_id)
        self._open_path(d if os.path.isdir(d) else str(config.EVENT_DIR))

    def _open_path(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)                          # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:                            # noqa: BLE001
            self._status("打开失败：%s" % str(exc)[:60], ALARM)

    def _ack_event(self, ev):
        by = os.environ.get("USERNAME") or os.environ.get("USER") or "值班员"
        ok, msg = self.store.ack_event(ev.event_id, by=by, note="弹窗内确认到场")
        if ok:
            self.refresh_events()
            self._status("已确认到场：%s（%s）" % (ev.event_id, by), OK_GREEN)
        return ok, msg

    def _false_event(self, ev):
        by = os.environ.get("USERNAME") or os.environ.get("USER") or "值班员"
        ok, msg = self.store.mark_false(ev.event_id, by=by, note="弹窗内标记误报")
        if ok:
            self.refresh_events()
            self._status("已标记误报：%s（可用于后续调参）" % ev.event_id, WARN)
        return ok, msg

    # ============================================================ 模拟火警（演练）
    def simulate_alarm(self, seconds=6.0):
        """
        演练：把**会抖动的合成火焰**直接喂进检测器，走完整真实链路
        （检测器 -> 确认闸门 -> 事件落库 -> 证据留档 -> 弹窗）。

        为什么这么做而不是直接造一条假事件：演练的目的是验证"报警真的会响"，
        绕过检测器就验不出阈值/闸门/弹窗这条链到底通不通。
        来源在日志与画面上都标注为演练，不冒充真实火情。

        注意：无论监控是否在跑，合成源都要起 —— 早期版本只在"没在监控"时才起
        合成源，结果最常用的场景（监控跑着、摄像头不通/没人）演练反而永远不报警。
        监控在跑时两者并存：真实帧照常检测，合成帧额外注入（画面上会有演练水印）。
        """
        self._sim_until = time.time() + float(seconds)
        self._start_sim_source()
        config.log("开始模拟火警（演练，%ds）" % int(seconds))
        self._status("演练中：正在向检测器注入合成火焰（%ds）…" % int(seconds), WARN)

    def _start_sim_source(self):
        if self.simulator is not None:
            return
        self.simulator = _SyntheticFireSource(self)
        self.simulator.start()
        if self.detector is None:
            self._build_backend()

    def _maybe_stop_simulation(self):
        if self._sim_until and time.time() > self._sim_until:
            self._sim_until = 0.0
            if self.simulator is not None:
                self.simulator.stop()
                self.simulator = None
            config.log("模拟火警结束")
            self._status("演练结束", DIM)

    # ============================================================ 设置
    def open_settings(self):
        cfg = config.load_settings()
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=BG)
        win.geometry("560x560")
        win.transient(self.root)

        txt = tk.Text(win, bg=PANEL, fg=FG, relief="flat", wrap="none",
                      font=("Consolas", 10), highlightthickness=0)
        txt.pack(fill="both", expand=True, padx=10, pady=10)

        vars_map = {}
        order = ["camera_url", "camera_name", "start_minimized", "use_tray",
                 "auto_start_monitor", "detect_interval_ms", "alarm_threshold",
                 "warning_threshold", "confirm_frames", "clear_frames",
                 "hsv_s_min", "hsv_v_min", "min_fire_px", "min_fire_ratio",
                 "skin_dilate_px", "smoke_enable", "smoke_min_ratio",
                 "sound_enable", "sound_period_ms", "popup_on_warning",
                 "pre_seconds", "post_seconds", "event_cooldown_sec", "keep_days",
                 "backend", "onnx_model_path", "onnx_conf"]
        lines = ["# 只列常用项；其余键可直接编辑 data\\settings.json",
                 "# 改完点「保存」即生效（部分项需重启监控）", ""]
        for k in order:
            v = cfg.get(k, config.SETTINGS_DEFAULT.get(k, ""))
            lines.append("%-22s = %s" % (k, json_scalar(v)))
        txt.insert("1.0", "\n".join(lines))

        def save():
            raw = txt.get("1.0", "end")
            patch = {}
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k in config.SETTINGS_DEFAULT:
                    patch[k] = parse_scalar(v)
            ok, msg = config.save_settings(patch)
            if ok:
                self.cfg = config.load_settings()
                self.detector.cfg = self.cfg
                self.detector.backend.cfg = self.cfg
                self.recorder.cfg = self.cfg
                self.var_name.set(self.cfg["camera_name"])
                self.var_url.set(str(self.cfg["camera_url"]))
                self.var_sens.set(self.cfg["alarm_threshold"])
                self.var_conf.set(self.cfg["confirm_frames"])
                self._status("设置已保存（重启监控后全部生效）", OK_GREEN)
                win.destroy()
            else:
                messagebox.showerror("保存失败", msg, parent=win)

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(bar, text="保存", command=save, bg=OK_GREEN, fg="white",
                  relief="flat", font=("Microsoft YaHei", 10), width=10).pack(side="left")
        tk.Button(bar, text="取消", command=win.destroy, bg=PANEL2, fg=FG,
                  relief="flat", font=("Microsoft YaHei", 10), width=10).pack(side="left", padx=6)
        tk.Label(bar, text="设置文件：%s" % config.SETTINGS_PATH, bg=BG, fg=DIM,
                 font=("Microsoft YaHei", 8)).pack(side="right")

    # ============================================================ 摄像头工具
    def _update_cam_hint(self):
        try:
            hint = video_mod.camera_hint(self.var_url.get().strip())
        except Exception:                                   # noqa: BLE001
            hint = ""
        try:
            self.lbl_cam_hint.configure(text="识别为：%s（本机设备同一时刻只能被一个程序占用）" % hint)
        except tk.TclError:
            pass

    def probe_cameras(self):
        if self.worker is not None and self.worker.running:
            messagebox.showwarning("请先停止监控",
                                   "摄像头同一时刻只能被一个程序占用。\n"
                                   "请先点「停止监控」再探测。", parent=self.root)
            return
        self._status("正在探测本机摄像头（0~6）…", WARN)
        self.root.update_idletasks()

        def work():
            found = video_mod.list_local_cameras()
            self.root.after(0, lambda: self._probe_done(found))
        import threading
        threading.Thread(target=work, daemon=True).start()

    def _probe_done(self, found):
        if found:
            self.var_url.set(str(found[0]))
            self._status("找到本机摄像头：%s（已选 %d）"
                         % ("、".join(str(i) for i in found), found[0]), OK_GREEN)
        else:
            self._status("没找到可用的本机摄像头。若被其它程序占用，请先关掉。", ALARM)

    def pick_file(self):
        path = filedialog.askopenfilename(
            title="选择视频文件（用于演示：会循环播放）",
            filetypes=[("视频", "*.mp4 *.avi *.mkv *.mov *.flv *.wmv"), ("全部", "*.*")],
            parent=self.root)
        if path:
            self.var_url.set(path)
            self.var_name.set(os.path.basename(path))
            self._status("已选择视频文件：%s" % path, OK_GREEN)

    # ============================================================ 窗口/后台
    def hide_to_background(self):
        """
        "平时只在后台运行"的落点：
          有托盘 -> 隐藏窗口，托盘继续盯着（点托盘图标或菜单可召回）
          无托盘 -> 最小化（不能隐藏，否则用户找不回来）
        """
        if self.tray is not None:
            self.root.withdraw()
            self._status("已隐藏到后台（托盘图标在，双击可召回）", DIM)
        else:
            self.root.iconify()
            self._status("已最小化（本机托盘不可用，任务栏里可召回）", DIM)

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _on_window_close(self):
        """点右上角 × ：默认继续在后台盯（这是安全底线，不能悄悄退出）。"""
        if self.worker is not None and self.worker.running:
            if messagebox.askyesno(
                    "仍在监控中",
                    "监控正在运行。\n\n"
                    "是 = 最小化到后台继续监控（推荐）\n"
                    "否 = 立即退出程序（将不再报警）", parent=self.root):
                self.hide_to_background()
            else:
                self.quit_app()
        else:
            self.quit_app()

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        try:
            self._status("正在退出…", DIM)
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            if self.worker is not None:
                self.worker.stop("程序退出")
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self.recorder is not None:
                self.recorder.stop()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self.popup is not None:
                self.popup.hide()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.store.log("程序退出", "", "system")
        except Exception:                                   # noqa: BLE001
            pass
        config.log("程序退出")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ============================================================ 运行
    def run(self):
        self.refresh_events()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit_app()


# ============================================================ 演练合成源
class _SyntheticFireSource(object):
    """
    没有摄像头时也能演练：造一个"会抖动的火焰"画面，直接喂给检测器。
    只用于 `模拟一次火警` 按钮与自检，绝不在正常监控路径上启用。
    """

    def __init__(self, app, size=(480, 640)):
        self.app = app
        self.h, self.w = size
        self._stop = False
        self._thread = None
        self._last_frame = None

    def start(self):
        import threading
        self._thread = threading.Thread(target=self._run, name="fire-sim", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def frame(self, t):
        import cv2
        import numpy as np
        img = np.full((self.h, self.w, 3), 190, np.uint8)
        cv2.rectangle(img, (30, 40), (self.w - 30, self.h - 40), (120, 120, 120), -1)
        cx, cy = self.w // 2, int(self.h * 0.62)
        j = int(7 * np.sin(t * 6.0)) + int(5 * np.cos(t * 9.7))
        rw, rh = 62, 104
        pts = np.array([[cx - rw, cy + rh], [cx - rw + 12 + j, cy - rh // 3],
                        [cx, cy - rh - 12 - j], [cx + rw - 8, cy - rh // 4],
                        [cx + rw, cy + rh]], np.int32)
        cv2.fillPoly(img, [pts], (0, 205, 255))
        cv2.circle(img, (cx + j // 2, cy + rh // 3), int(rh * 0.44), (60, 255, 255), -1)
        cv2.circle(img, (cx, cy + rh // 2), int(rh * 0.24), (255, 255, 255), -1)
        cv2.putText(img, "SIMULATED FIRE (drill)", (20, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        return img

    def _run(self):
        import time as _t
        t0 = _t.time()
        while not self._stop and self.app._sim_until > _t.time():
            frame = self.frame(_t.time() - t0)
            try:
                # 直接喂检测器（与真实采集同一条链路），并留一份给界面显示
                det = self.app.detector.update(frame, camera_name="演练合成源",
                                               camera_url="simulated")
                self.app._sim_det = det
                if self.app.recorder is not None:
                    self.app.recorder.push_frame(frame)
            except Exception:                               # noqa: BLE001
                pass
            self._last_frame = frame
            _t.sleep(0.09)


# ============================================================ 设置文本解析
def json_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def parse_scalar(text):
    t = str(text).strip()
    if t.lower() in ("true", "yes", "on", "是"):
        return True
    if t.lower() in ("false", "no", "off", "否"):
        return False
    try:
        if "." in t:
            return float(t)
        return int(t)
    except ValueError:
        return t
