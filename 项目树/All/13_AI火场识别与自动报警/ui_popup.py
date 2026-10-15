# -*- coding: utf-8 -*-
r"""
火警弹出窗口（发现火情时自动弹出的那个窗）

要求与对策：
    要求                     做法
    ───────────────────────  ──────────────────────────────────────────────
    发现火势就弹窗            主窗口隐藏/最小化也能弹（deiconify + topmost + 抢焦点）
    弹窗里能看摄像头          弹窗内嵌实时画面，检测框一起画出来
    别弹了看不见              Windows 用 SetForegroundWindow + 闪烁任务栏图标；
                              纯 tkinter 无法 100% 抢焦点（系统限制），所以同时闪任务栏
    别一直响吵死人            静音按钮 / 60 秒未处理自动停止声音（窗口留着）
    值班的人要能响应          确认到场（留痕）/ 误报（留痕，用于后续调参）/ 打开证据目录
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk

import config
import models

#: 报警音节奏（Windows 上用 Beep，其它平台退化为 bell）
BEEP_PATTERN = ((880, 220), (660, 220), (880, 220), (0, 180))

FLASH_BG = ("#7a0000", "#d32020")     # 闪烁两种底色
FG = "#ffffff"


class AlarmPopup(object):
    """
    火警弹窗。由 ui_main 创建与持有；同一时刻只允许一个弹窗，
    后续事件在弹窗里追加到"事件列表"，不叠一堆窗口（叠窗反而看不清）。
    """

    def __init__(self, master, app=None, on_ack=None, on_false=None,
                 on_open_dir=None, on_dismiss=None):
        self.master = master              # 主窗口（被隐藏时用它恢复）
        self.app = app
        self.on_ack = on_ack
        self.on_false = on_false
        self.on_open_dir = on_open_dir
        self.on_dismiss = on_dismiss

        self.top = None
        self._flash_job = None
        self._tick_job = None
        self._sound_job = None
        self._events = []                 # 本次弹窗期间累计的事件
        self._current = None
        self._sound_on = True
        self._flash_on = False
        self._opened_at = 0.0
        self._photo = None                # 保持 PhotoImage 引用，否则会被 GC 掉变白屏
        self._img_item = None
        self._canvas = None
        self._level_label = None
        self._info_label = None
        self._reason_label = None
        self._listbox = None
        self._sound_btn = None

    # ---------------- 开关 ----------------
    @property
    def visible(self):
        return bool(self.top is not None and self.top.winfo_exists())

    def show(self, ev, frame=None, det=None):
        """弹窗（已开着就把新事件追加进去并切到该事件）。"""
        self._events.append(ev)
        if not self.visible:
            self._build()
        self._current = ev
        self._select_event(ev)
        self._refresh_list()
        self._update_frame(frame, det)
        self._raise_and_focus()
        self._start_flash()
        if self._sound_on and config.LEVEL_META.get(ev.level, {}).get("sound", True):
            self._start_sound()
        self._opened_at = time.time()

    def hide(self):
        self._stop_jobs()
        if self.visible:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
        self.top = None

    def notify_clear(self, ev):
        """事件解除：标题改成"已解除"，声音停掉，但**不自动关窗**（要人看过）。"""
        if not self.visible:
            return
        self._stop_sound()
        self._refresh_list()
        if self._current is not None and self._current.event_id == ev.event_id:
            self._set_header("火警已解除 · 请确认现场", "#146b1e", "#1f8c2a")
            self._refresh_detail()

    # ---------------- 构建界面 ----------------
    def _build(self):
        top = tk.Toplevel(self.master)
        self.top = top
        top.title("火警报警 · AI 火场识别")
        top.configure(bg=FLASH_BG[0])
        top.geometry("980x720+60+40")
        top.minsize(760, 560)
        top.protocol("WM_DELETE_WINDOW", self._on_close)
        top.attributes("-topmost", True)
        top.bind("<Escape>", lambda e: self._on_close())
        top.bind("<space>", lambda e: self._do_ack())
        top.bind("f", lambda e: self._do_false())
        top.bind("F", lambda e: self._do_false())

        # ---- 顶部大标题（闪烁） ----
        head = tk.Frame(top, bg=FLASH_BG[0])
        head.pack(fill="x")
        self._head = head
        self._level_label = tk.Label(head, text="确认火警", bg=FLASH_BG[0], fg=FG,
                                     font=("Microsoft YaHei", 30, "bold"), pady=10)
        self._level_label.pack(fill="x")
        self._info_label = tk.Label(head, text="", bg=FLASH_BG[0], fg=FG,
                                    font=("Microsoft YaHei", 12), pady=2)
        self._info_label.pack(fill="x")
        self._reason_label = tk.Label(head, text="", bg=FLASH_BG[0], fg="#ffe9a8",
                                      font=("Microsoft YaHei", 11), pady=4,
                                      wraplength=920, justify="center")
        self._reason_label.pack(fill="x")

        body = tk.Frame(top, bg="#1b1b1b")
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg="#1b1b1b")
        left.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        self._canvas = tk.Canvas(left, bg="#000000", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.create_text(10, 10, anchor="nw", fill="#888888",
                                 text="等待画面…", tags=("hint",))
        self._img_item = self._canvas.create_image(0, 0, anchor="nw")

        right = tk.Frame(body, bg="#141414", width=330)
        right.pack(side="right", fill="y", padx=(0, 8), pady=8)
        right.pack_propagate(False)

        tk.Label(right, text="本次报警事件", bg="#141414", fg="#d6d6d6",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(2, 4))
        lb = tk.Listbox(right, bg="#0d0d0d", fg="#e8e8e8", height=8,
                        font=("Microsoft YaHei", 10), selectbackground="#7a0000",
                        highlightthickness=0, activestyle="none")
        lb.pack(fill="x")
        lb.bind("<<ListboxSelect>>", self._on_pick)
        self._listbox = lb

        tk.Label(right, text="证据（自动留档）", bg="#141414", fg="#d6d6d6",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(10, 4))
        self._evo = tk.Text(right, bg="#0d0d0d", fg="#c9d6c9", height=9, wrap="word",
                            font=("Consolas", 9), highlightthickness=0)
        self._evo.pack(fill="x")
        self._evo.configure(state="disabled")

        tk.Label(right, text="处置（会写入审计日志）", bg="#141414", fg="#d6d6d6",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(10, 4))
        btns = tk.Frame(right, bg="#141414")
        btns.pack(fill="x")
        tk.Button(btns, text="确认到场（空格）", command=self._do_ack,
                  bg="#1f8c2a", fg="white", font=("Microsoft YaHei", 11, "bold"),
                  relief="flat", height=2).pack(fill="x", pady=2)
        tk.Button(btns, text="误报 / 关闭（F）", command=self._do_false,
                  bg="#6b6b6b", fg="white", font=("Microsoft YaHei", 10),
                  relief="flat").pack(fill="x", pady=2)
        tk.Button(btns, text="打开证据目录", command=self._do_open_dir,
                  bg="#33415c", fg="white", font=("Microsoft YaHei", 10),
                  relief="flat").pack(fill="x", pady=2)
        self._sound_btn = tk.Button(btns, text="🔊 静音", command=self._toggle_sound,
                                    bg="#33415c", fg="white",
                                    font=("Microsoft YaHei", 10), relief="flat")
        self._sound_btn.pack(fill="x", pady=2)

        self._status = tk.Label(right, text="", bg="#141414", fg="#9a9a9a",
                                font=("Microsoft YaHei", 9), wraplength=310,
                                justify="left")
        self._status.pack(fill="x", pady=(8, 0))

        tk.Label(top, text="快捷键：空格=确认到场　F=误报　Esc=关闭窗口（不改变报警状态）",
                 bg="#1b1b1b", fg="#8a8a8a", font=("Microsoft YaHei", 9)).pack(fill="x")

    # ---------------- 画面 ----------------
    def _update_frame(self, frame, det=None):
        if not self.visible or frame is None:
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._show_hint_text("未安装 Pillow，无法显示画面（pip install Pillow）")
            return
        import cv2
        import detect as detect_mod

        ev = self._current
        d = det
        if d is None and ev is not None:
            d = models.Detection(has_fire=True, score=ev.peak_score or ev.score,
                                 parts=ev.parts or {}, fire_ratio=ev.fire_ratio,
                                 smoke_ratio=ev.smoke_ratio, level=ev.level,
                                 regions=ev.regions or [])
        try:
            shown = detect_mod.draw_overlay(frame, d, self._cfg(),
                                           extra_lines=[_footer(ev)])
        except Exception:                                  # noqa: BLE001
            shown = frame
        cw = max(320, self._canvas.winfo_width())
        ch = max(240, self._canvas.winfo_height())
        h, w = shown.shape[:2]
        scale = min(cw / float(w), ch / float(h))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        rgb = cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(img)
        try:
            self._canvas.itemconfigure(self._img_item, image=self._photo)
            self._canvas.coords(self._img_item, (cw - nw) // 2, (ch - nh) // 2)
            self._canvas.delete("hint")
        except tk.TclError:
            pass

    def _show_hint_text(self, text):
        if self._canvas is None:
            return
        try:
            self._canvas.delete("hint")
            self._canvas.create_text(12, 12, anchor="nw", fill="#ff8080",
                                     text=text, tags=("hint",))
        except tk.TclError:
            pass

    def _cfg(self):
        if self.app is not None and hasattr(self.app, "cfg"):
            return self.app.cfg
        return config.load_settings()

    # ---------------- 状态与列表 ----------------
    def _select_event(self, ev):
        self._current = ev
        self._set_header(ev.title(), FLASH_BG[0], FLASH_BG[1])
        self._refresh_detail()
        if self._listbox is not None:
            for i, e in enumerate(self._events):
                if e.event_id == ev.event_id:
                    self._listbox.selection_clear(0, "end")
                    self._listbox.selection_set(i)
                    self._listbox.see(i)
                    break

    def _set_header(self, title, bg1, bg2):
        self._flash_colors = (bg1, bg2)
        if self._level_label is not None:
            try:
                self._level_label.configure(text=title)
            except tk.TclError:
                pass

    def _refresh_detail(self):
        ev = self._current
        if ev is None:
            return
        meta = config.LEVEL_META.get(ev.level, {})
        if self._info_label is not None:
            try:
                self._info_label.configure(
                    text="%s ｜ 触发时间 %s ｜ 当前分数 %.2f ｜ 峰值 %.2f ｜ 命中 %d 帧 ｜ %s"
                         % (meta.get("name", ev.level), ev.created_at, ev.score,
                            ev.peak_score, ev.frames_hit, ev.status_text()))
            except tk.TclError:
                pass
        if self._reason_label is not None:
            try:
                self._reason_label.configure(
                    text="判定依据：%s ｜ 火色占比 %.2f%% ｜ 烟雾占比 %.2f%% ｜ 机位 %s"
                         % (ev.reason or "—", ev.fire_ratio * 100,
                            ev.smoke_ratio * 100, ev.camera_name or ev.camera_url))
            except tk.TclError:
                pass
        if self._evo is not None:
            lines = ["事件号 : %s" % ev.event_id,
                     "机位   : %s（%s）" % (ev.camera_name, ev.camera_url),
                     "抓拍图 : %s" % (ev.snapshot or "（写入中…）"),
                     "标注图 : %s" % (ev.marked or "（写入中…）"),
                     "录像   : %s" % (ev.clip or "（告警后继续录 %ds…）"
                                      % self._cfg()["post_seconds"])]
            if ev.acked_by:
                lines.append("处置   : %s 于 %s %s"
                             % (ev.acked_by, ev.acked_at, ev.acked_note or ""))
            try:
                self._evo.configure(state="normal")
                self._evo.delete("1.0", "end")
                self._evo.insert("1.0", "\n".join(lines))
                self._evo.configure(state="disabled")
            except tk.TclError:
                pass

    def _refresh_list(self):
        if self._listbox is None:
            return
        try:
            self._listbox.delete(0, "end")
            for e in self._events:
                mark = "●" if e.status in (models.ST_FIRING, models.ST_ACKED) else "○"
                self._listbox.insert(
                    "end", "%s %s %s %s" % (mark, e.created_at[11:19],
                                            e.level_name(), e.status_text()))
        except tk.TclError:
            pass

    def _on_pick(self, _evt=None):
        if self._listbox is None:
            return
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._events):
            self._current = self._events[idx]
            self._refresh_detail()

    # ---------------- 闪烁 / 声音 / 抢焦点 ----------------
    def _start_flash(self):
        if self._flash_job is not None:
            return
        self._flash_step()

    def _flash_step(self):
        if not self.visible:
            self._flash_job = None
            return
        self._flash_on = not self._flash_on
        colors = getattr(self, "_flash_colors", FLASH_BG)
        c = colors[1] if self._flash_on else colors[0]
        for w in (self.top, getattr(self, "_head", None),
                  self._level_label, self._info_label, self._reason_label):
            if w is None:
                continue
            try:
                w.configure(bg=c)
            except tk.TclError:
                pass
        self._flash_job = self.top.after(450, self._flash_step)

    def _start_sound(self):
        if self._sound_job is not None or not self._sound_on:
            return
        self._beep_step()

    def _beep_step(self):
        if not self.visible or not self._sound_on:
            self._sound_job = None
            return
        if time.time() - self._opened_at > 60:
            # 60 秒没人处理就不再响（窗口留着，避免"响到崩溃/被拔电源"）
            self._sound_job = None
            self._set_status("已连续响铃 60 秒，自动静音（窗口保持显示）")
            return
        _beep_async()
        period = int(self._cfg()["sound_period_ms"])
        self._sound_job = self.top.after(max(300, period), self._beep_step)

    def _stop_sound(self):
        if self._sound_job is not None:
            try:
                self.top.after_cancel(self._sound_job)
            except tk.TclError:
                pass
            self._sound_job = None

    def _toggle_sound(self):
        self._sound_on = not self._sound_on
        if self._sound_btn is not None:
            try:
                self._sound_btn.configure(text="🔊 静音" if self._sound_on else "🔇 已静音")
            except tk.TclError:
                pass
        if self._sound_on:
            self._opened_at = time.time()
            self._start_sound()
        else:
            self._stop_sound()

    def _raise_and_focus(self):
        if not self.visible:
            return
        try:
            # 主窗口如果被隐藏/最小化了，先恢复出来，否则弹窗可能藏在后面
            if self.master is not None and self.master.winfo_exists():
                try:
                    if self.master.state() == "iconic":
                        self.master.deiconify()
                except tk.TclError:
                    pass
            self.top.deiconify()
            self.top.lift()
            self.top.attributes("-topmost", True)
            self.top.focus_force()
            self.top.bell()
        except tk.TclError:
            pass
        _flash_taskbar(self.top)

    def _set_status(self, text):
        if getattr(self, "_status", None) is not None:
            try:
                self._status.configure(text=text)
            except tk.TclError:
                pass

    # ---------------- 处置动作 ----------------
    def _do_ack(self):
        ev = self._current
        if ev is None:
            return
        if self.on_ack:
            ok, msg = self.on_ack(ev)
            self._set_status(msg if not ok else "已确认到场：%s" % msg)
        self._refresh_list()
        self._refresh_detail()

    def _do_false(self):
        ev = self._current
        if ev is not None and self.on_false:
            ok, msg = self.on_false(ev)
            self._set_status(msg)
        self._on_close()

    def _do_open_dir(self):
        ev = self._current
        if ev is None:
            return
        if self.on_open_dir:
            self.on_open_dir(ev)
            return
        d = os.path.join(str(config.EVENT_DIR), ev.event_id)
        _open_path(d if os.path.isdir(d) else str(config.EVENT_DIR))

    def _on_close(self):
        self._stop_jobs()
        if self.on_dismiss:
            try:
                self.on_dismiss()
            except Exception:                              # noqa: BLE001
                pass
        if self.visible:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
        self.top = None

    def _stop_jobs(self):
        for attr in ("_flash_job", "_tick_job", "_sound_job"):
            job = getattr(self, attr, None)
            if job is not None and self.top is not None:
                try:
                    self.top.after_cancel(job)
                except tk.TclError:
                    pass
            setattr(self, attr, None)


# ============================================================ 平台小工具
def _footer(ev):
    if ev is None:
        return ""
    return "%s ｜ %s ｜ 峰值 %.2f" % (ev.event_id, ev.level_name(), ev.peak_score or ev.score)


def _beep_async():
    """异步响一声，绝不让 UI 线程等。（Windows 用 Beep，其它平台退化为 bell）"""
    if os.name == "nt":
        try:
            import winsound
            winsound.Beep(880, 200)
            return
        except Exception:                                  # noqa: BLE001
            pass
    try:
        import threading
        threading.Thread(target=lambda: sys.stdout.write("\a") or sys.stdout.flush(),
                         daemon=True).start()
    except Exception:                                      # noqa: BLE001
        pass


def _flash_taskbar(window):
    """
    闪烁任务栏图标：纯 tkinter 抢焦点在 Windows 上不保证成功，
    闪任务栏是**系统级**提醒，用户一定能看到。失败不影响主流程。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(window.winfo_id())
        user32 = ctypes.windll.user32
        # 需要把子窗口 hwnd 换成顶层窗口 hwnd，否则闪的是内部窗口
        GA_ROOT = 2
        root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x00000003, 0x0000000C

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), wintypes.HWND(root),
                          FLASHW_ALL | FLASHW_TIMERNOFG, 12, 0)
        user32.FlashWindowEx(ctypes.byref(info))
        user32.SetForegroundWindow(wintypes.HWND(root))
    except Exception:                                      # noqa: BLE001
        pass


def _open_path(path):
    """在资源管理器里打开目录/文件（尽力而为）。"""
    try:
        if os.name == "nt":
            os.startfile(path)                             # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:                                      # noqa: BLE001
        pass
