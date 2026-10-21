# -*- coding: utf-8 -*-
r"""
04 巡考机器人 · Windows 控制端

定位：小车本身跑 Ubuntu + ROS（见 巡考机器人/01_代码与程序），Windows 端**不装 ROS**，
只通过网络连接小车上运行的 ``webui/robot_web.py``（默认 0.0.0.0:8080）：
输入小车 IP → 连接 → 遥测 / 遥控 / 三路画面 / 一键巡线 / 参数 / 远端文件。

界面分页：
  ① 遥控台      实时画面 + 按住即走的方向盘 + 遥测 + 雷达避障
  ② 巡线参数    黑线/白线、速度、PID、阈值、锁线参数（读写小车 line_params.json）
  ③ 远端文件    小车上的代码/配置（只读预览、下载）+ 巡更照片浏览/下载
  ④ 本地资料    巡考机器人目录里的代码、文档、图示、测试记录
  ⑤ 使用说明    连接步骤、键盘、安全提醒、故障排查

依赖：Python 3.8+ 标准库 + tkinter；内嵌画面需要 Pillow（打包 runtime 自带，
没有时自动降级为"浏览器查看"）。本模块不 import rospy / cv2。

独立运行：
    python gui.py
    python 自检.py          # 不起界面，直接验证通信模块
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import webbrowser
import zipfile
import math
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# 独立运行：把包根加入 sys.path，才能 import 包根的 ui_theme
for _p in (HERE, os.path.dirname(HERE)):
    if os.path.isfile(os.path.join(_p, "ui_theme.py")):
        if _p not in sys.path:
            sys.path.append(_p)
        break

try:
    import ui_theme
except Exception:                                            # noqa: BLE001
    ui_theme = None

import robot_link
import robot_camera

try:
    import robot_sim
except Exception:                                            # noqa: BLE001
    robot_sim = None

APP_TITLE = "04 巡考机器人 · Windows 控制端"
COPYRIGHT = "© 2026 EDY_POWERQ"
ROOT = os.path.join(HERE, "巡考机器人")
CODE_ROOT = os.path.join(ROOT, "01_代码与程序")
DATA_DIR = os.path.join(HERE, "data")
CAPTURE_DIR = os.path.join(DATA_DIR, "captures")
LOG_DIR = os.path.join(DATA_DIR, "logs")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
GUIDE_FILE = os.path.join(HERE, "使用说明_Windows控制端.md")

# 配色跟随总控台 ui_theme；缺 ui_theme 时用同一套值兜底
BG = getattr(ui_theme, "BG", "#070d1a")
PANEL = getattr(ui_theme, "PANEL", "#0d1a2e")
PANEL2 = getattr(ui_theme, "PANEL2", "#122542")
BORDER = getattr(ui_theme, "BORDER", "#1b3457")
FG = getattr(ui_theme, "FG", "#e8f1ff")
DIM = getattr(ui_theme, "DIM", "#8ba3c7")
MUTE = getattr(ui_theme, "MUTE", "#5b739a")
ACCENT = getattr(ui_theme, "ACCENT", "#22d3ee")
OK = getattr(ui_theme, "OK", "#34d399")
WARN = getattr(ui_theme, "WARN", "#fbbf24")
ALARM = getattr(ui_theme, "ALARM", "#f43f5e")

FONT = getattr(ui_theme, "FONT", ("Microsoft YaHei", 10))
FONT_B = getattr(ui_theme, "FONT_B", ("Microsoft YaHei", 10, "bold"))
FONT_SMALL = getattr(ui_theme, "FONT_SMALL", ("Microsoft YaHei", 9))
FONT_TITLE = getattr(ui_theme, "FONT_TITLE", ("Microsoft YaHei", 17, "bold"))
FONT_H1 = getattr(ui_theme, "FONT_H1", ("Microsoft YaHei", 13, "bold"))

LOCAL_EXTS = (".py", ".sh", ".yaml", ".yml", ".json", ".md", ".html", ".txt",
              ".png", ".jpg", ".jpeg", ".log", ".pdf", ".csv")

# 巡线参数：与小车端 robot_web.py 的 DEFAULT_PARAMS 对齐
PARAM_GROUPS = (
    ("速度与线色", (
        ("line_color", "线色", "choice", "black = 黑线，white = 白线"),
        ("speed", "巡线速度 (m/s)", "float", "直道速度，实测 0.12~0.20"),
        ("v_min", "最低速度 (m/s)", "float", "急弯时不低于该速度"),
        ("max_angular", "最大角速度 (rad/s)", "float", "巡线转向上限，建议 0.50"),
    )),
    ("循迹增益", (
        ("kp", "比例增益 kp", "float", "横向偏差 → 角速度"),
        ("kd", "微分增益 kd", "float", "抑制左右摆动"),
        ("deadband", "死区 (m)", "float", "偏差小于该值不修正"),
    )),
    ("视觉阈值", (
        ("v_thresh", "亮度阈值 V", "float", "黑线 V<阈值；白线相反"),
        ("s_thresh", "饱和度阈值 S", "float", "过滤彩色干扰"),
    )),
    ("锁线与接近", (
        ("lock_dist", "锁线距离 (m)", "float", "多近开始锁线"),
        ("approach_dist", "接近距离 (m)", "float", "开始减速/接近"),
        ("approach_gain", "接近增益", "float", "接近段速度修正强度"),
        ("dir_weight", "方向权重", "float", "前视方向一致性权重"),
        ("full_lock_err", "满舵偏差", "float", "偏差达到该值打满舵"),
        ("over_steer", "过弯补偿", "float", "过弯时的额外舵量"),
    )),
    ("搜索与保护", (
        ("search_speed", "搜索速度 (m/s)", "float", "丢线时的搜索速度"),
        ("search_time", "搜索时间 (s)", "float", "顺弯搜索时长"),
        ("coast_time", "滑行时间 (s)", "float", "丢线后保持原方向的时长"),
    )),
)

LINE_COLORS = (("black", "黑线"), ("white", "白线"))


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def _fmt(value, digits=2, unit="", dash="—"):
    if value is None:
        return dash
    try:
        return ("%." + str(digits) + "f%s") % (float(value), unit)
    except (TypeError, ValueError):
        return dash


def _fmt_uptime(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def _ensure_stdio():
    """pythonw.exe 下 stdout/stderr 是 None：重定向到日志，避免回调异常雪崩。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stream = open(os.path.join(LOG_DIR, "gui_console.log"), "a",
                      encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


# ======================================================================
# 基础控件
# ======================================================================
class Chip(tk.Label):
    """状态药丸：● 已连接 / ● 未连接 / ● 连接中…"""

    def __init__(self, master, text="● 未连接", fg=DIM):
        tk.Label.__init__(self, master, text=text, fg=fg, bg=PANEL2,
                          font=FONT_SMALL, padx=12, pady=4)

    def set(self, text, fg):
        self.config(text=text, fg=fg)


class Card(tk.Frame):
    """深色卡片：标题 + 内容区。"""

    def __init__(self, master, title, subtitle="", bg=PANEL):
        tk.Frame.__init__(self, master, bg=bg, highlightbackground=BORDER,
                          highlightthickness=1)
        head = tk.Frame(self, bg=bg)
        head.pack(fill="x", padx=12, pady=(7, 0))
        tk.Label(head, text=title, bg=bg, fg=FG, font=FONT_B).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=bg, fg=MUTE,
                     font=("Microsoft YaHei", 8)).pack(side="right")
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="both", expand=True, padx=12, pady=(4, 7))


class ScrollFrame(tk.Frame):
    """竖向滚动容器（鼠标进入时接管滚轮，离开时释放）。"""

    def __init__(self, master, bg=BG, width=None):
        tk.Frame.__init__(self, master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0,
                                bd=0, width=width or 0)
        self.scroll = ttk.Scrollbar(self, orient="vertical",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:                                    # noqa: BLE001
            pass


class ImagePanel(tk.Frame):
    """画面容器：黑底 + 居中图片/占位文字，按控件尺寸自适应缩放。"""

    def __init__(self, master, text="暂无画面", min_w=160, min_h=120):
        tk.Frame.__init__(self, master, bg="#05070a",
                          highlightbackground=BORDER, highlightthickness=1)
        self._min_w = min_w
        self._min_h = min_h
        self.canvas = tk.Canvas(self, bg="#05070a", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._img = self.canvas.create_image(0, 0, anchor="center")
        self._txt = self.canvas.create_text(
            0, 0, text=text, fill=MUTE, font=("Microsoft YaHei", 10),
            justify="center", width=460)
        self._jpeg = b""
        self._photo = None
        self._job = None
        self._placeholder = text
        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        self.canvas.coords(self._img, event.width // 2, event.height // 2)
        self.canvas.coords(self._txt, event.width // 2, event.height // 2)
        if self._jpeg:
            if self._job:
                try:
                    self.canvas.after_cancel(self._job)
                except Exception:                            # noqa: BLE001
                    pass
            self._job = self.canvas.after(240, self._render)
        else:
            self.canvas.itemconfigure(self._txt, width=max(160, event.width - 60))

    def set_text(self, text):
        self._jpeg = b""
        self._photo = None
        self.canvas.itemconfigure(self._img, image="")
        self.canvas.itemconfigure(self._txt, text=text, state="normal")

    def set_jpeg(self, data):
        self._jpeg = data or b""
        if not self._jpeg:
            return
        self._render()

    def jpeg(self):
        return self._jpeg

    def _render(self):
        self._job = None
        if not self._jpeg:
            return
        if not robot_camera.HAS_PIL:
            self.set_text("已收到小车画面，但本机缺少 Pillow，无法内嵌显示。\n"
                          "可点「浏览器打开」在小车网页里看画面。")
            return
        width = max(self._min_w, self.canvas.winfo_width() - 6)
        height = max(self._min_h, self.canvas.winfo_height() - 6)
        photo, size = robot_camera.decode_photo(self._jpeg, width, height,
                                                master=self.canvas)
        if photo is None:
            self.set_text("画面解码失败（可能不是 JPEG）")
            return
        self._photo = photo
        self.canvas.itemconfigure(self._img, image=photo)
        self.canvas.itemconfigure(self._txt, text="", state="hidden")


class BatteryBar(tk.Canvas):
    def __init__(self, master, width=286, height=16):
        tk.Canvas.__init__(self, master, bg=PANEL, highlightthickness=0,
                           width=width, height=height)
        self._bar_w = width
        self._bar_h = height
        self._pct = 0.0
        self._color = MUTE
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, pct, color):
        self._pct = max(0.0, min(1.0, float(pct)))
        self._color = color
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width() or self._bar_w
        h = self.winfo_height() or self._bar_h
        self.create_rectangle(0, 0, w, h, fill=PANEL2, outline=BORDER)
        fill_w = max(0, int((w - 2) * self._pct))
        if fill_w:
            self.create_rectangle(1, 1, 1 + fill_w, h - 1,
                                  fill=self._color, outline="")


class Radar(tk.Canvas):
    """车头朝上的半圆雷达示意：前 / 左 / 右 / 后 四向最近距离。"""

    ORDER = (("front", 0.0, "前"), ("left", 68.0, "左"),
             ("right", -68.0, "右"), ("rear", 180.0, "后"))

    def __init__(self, master, width=300, height=196):
        tk.Canvas.__init__(self, master, bg=PANEL, highlightthickness=0,
                           width=width, height=height)
        self._data = {}
        self._limit = 0.35
        self.bind("<Configure>", lambda e: self.redraw())

    def update_data(self, clear, limit):
        self._data = clear if isinstance(clear, dict) else {}
        try:
            self._limit = float(limit or 0.35)
        except (TypeError, ValueError):
            self._limit = 0.35
        self.redraw()

    def _color(self, dist):
        if dist is None:
            return MUTE
        if dist < self._limit:
            return ALARM
        if dist < max(1.0, self._limit * 2.4):
            return WARN
        return OK

    def redraw(self):
        self.delete("all")
        w = self.winfo_width() or 300
        h = self.winfo_height() or 190
        if w <= 1 or h <= 1:
            return
        cx, cy = w / 2.0, h - 28.0
        max_r = max(40.0, min(w / 2.0 - 20.0, h - 58.0))
        scale = max_r / 3.2
        for d in (1, 2, 3):
            r = d * scale
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=0, extent=180,
                            style="arc", outline=BORDER)
            self.create_text(cx, cy - r - 6, text="%dm" % d, fill=MUTE,
                             font=("Microsoft YaHei", 7))
        for deg in (30, 60, 90, 120, 150):
            rad = math.radians(deg)
            self.create_line(cx, cy, cx + math.cos(rad) * max_r,
                             cy - math.sin(rad) * max_r, fill="#152943")
        # 避障停车线（前向 ±25°）
        if self._limit > 0:
            r = min(self._limit, 3.2) * scale
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=65, extent=50,
                            style="arc", outline=ALARM, dash=(3, 3))
        self.create_polygon(cx, cy - 9, cx - 8, cy + 7, cx + 8, cy + 7,
                            fill=ACCENT, outline="")

        for key, angle, label in self.ORDER:
            dist = self._data.get(key)
            if dist is None:
                continue
            color = self._color(dist)
            if key == "rear":
                continue
            r = min(float(dist), 3.2) * scale
            rad = math.radians(angle)
            x = cx + math.sin(rad) * r
            y = cy - math.cos(rad) * r
            self.create_line(cx, cy, x, y, fill=color, width=1,
                             dash=(2, 3) if dist < self._limit else None)
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color,
                             outline="")
            text = "%s %.2fm" % (label, dist) if dist <= 3.2 else \
                "%s >3.2m" % label
            if key == "front":
                self.create_text(cx, max(10, y - 14), text=text, fill=color,
                                 font=("Microsoft YaHei", 8))
            elif key == "left":
                self.create_text(8, y + 10, text=text, fill=color,
                                 font=("Microsoft YaHei", 8), anchor="w")
            else:
                self.create_text(w - 8, y + 10, text=text, fill=color,
                                 font=("Microsoft YaHei", 8), anchor="e")

        rear = self._data.get("rear")
        if rear is not None:
            self.create_text(cx, h - 6, text="后 %.2fm" % rear
                             if rear <= 9.9 else "后 >9.9m",
                             fill=self._color(rear),
                             font=("Microsoft YaHei", 8), anchor="s")


class ControlPad(tk.Frame):
    """按住才动的方向盘（3×3）。"""

    BUTTONS = (
        ("", 0, 0, None), ("▲", 0, 1, "f"), ("", 0, 2, None),
        ("◀", 1, 0, "l"), ("■", 1, 1, "stop"), ("▶", 1, 2, "r"),
        ("", 2, 0, None), ("▼", 2, 1, "b"), ("", 2, 2, None),
    )

    def __init__(self, master, on_press, on_release, on_stop):
        tk.Frame.__init__(self, master, bg=PANEL)
        self.on_press = on_press
        self.on_release = on_release
        self.on_stop = on_stop
        self.buttons = {}
        for text, row, col, direction in self.BUTTONS:
            if direction is None:
                continue
            if direction == "stop":
                btn = tk.Button(self, text=text, command=on_stop,
                                bg=PANEL2, fg=WARN, activebackground=BORDER,
                                activeforeground=WARN, relief="flat", bd=0,
                                highlightthickness=0, font=("Microsoft YaHei", 14, "bold"),
                                width=4, height=1, cursor="hand2")
                btn.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            else:
                btn = tk.Button(self, text=text, bg=PANEL2, fg=FG,
                                activebackground=BORDER, activeforeground=ACCENT,
                                relief="flat", bd=0, highlightthickness=0,
                                font=("Microsoft YaHei", 15, "bold"),
                                width=4, height=1, cursor="hand2",
                                takefocus=0)
                btn.bind("<ButtonPress-1>", lambda e, d=direction: self.on_press(d))
                btn.bind("<ButtonRelease-1>", lambda e, d=direction: self.on_release(d))
                btn.bind("<Leave>", lambda e, d=direction: self.on_release(d))
                btn.grid(row=row, column=col, padx=4, pady=3, sticky="nsew")
            self.buttons[direction] = btn
        for c in range(3):
            self.grid_columnconfigure(c, weight=1)
        for r in range(3):
            self.grid_rowconfigure(r, weight=1)

    def set_enabled(self, enabled):
        for direction, btn in self.buttons.items():
            if direction == "stop":
                btn.config(state="normal" if enabled else "disabled")
            else:
                btn.config(state="normal" if enabled else "disabled")


# ======================================================================
# 扫描对话框
# ======================================================================
class ScanDialog(tk.Toplevel):
    def __init__(self, app, port=robot_link.DEFAULT_PORT):
        tk.Toplevel.__init__(self, app.root)
        self.app = app
        self.port = int(port)
        self.title("局域网搜索小车")
        self.configure(bg=BG)
        self.geometry("560x430")
        self.minsize(480, 360)
        self.transient(app.root)
        self.grab_set()
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._found = 0

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(head, text="扫描本机所在 /24 网段（端口 %d）" % self.port,
                 bg=BG, fg=FG, font=FONT_B).pack(side="left")
        self.progress = tk.Label(head, text="正在扫描…", bg=BG, fg=DIM,
                                 font=FONT_SMALL)
        self.progress.pack(side="right")

        tk.Label(self, text="扫描到的小车会列在下面；双击一行即可连接。"
                            "若一直找不到，请确认小车已启动 webui/robot_web.py、"
                            "Windows 与小车在同一网段。",
                 bg=BG, fg=DIM, justify="left", font=FONT_SMALL,
                 wraplength=520).pack(fill="x", padx=16, pady=(0, 8))

        wrap = tk.Frame(self, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        cols = ("ip", "latency", "mode")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for col, text, width in (("ip", "小车地址", 200),
                                 ("latency", "延迟", 90),
                                 ("mode", "状态", 160)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width,
                             anchor="w" if col == "ip" else "center")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self._connect_selected())

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        ttk.Button(bar, text="连接选中", style="Accent.TButton",
                   command=self._connect_selected).pack(side="left")
        ttk.Button(bar, text="重新扫描",
                   command=self._rescan).pack(side="left", padx=8)
        ttk.Button(bar, text="关闭", command=self._close).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._start_scan()

    def _start_scan(self):
        self._stop = threading.Event()
        self.progress.config(text="正在扫描…")
        self._found = 0
        for item in self.tree.get_children():
            self.tree.delete(item)

        def on_found(item):
            self._q.put(item)

        def work():
            try:
                robot_link.scan_lan(port=self.port, on_found=on_found,
                                    stop_event=self._stop)
            finally:
                self._q.put(None)

        threading.Thread(target=work, daemon=True).start()
        self.after(120, self._poll)

    def _rescan(self):
        self._stop.set()
        self.after(100, self._start_scan)

    def _poll(self):
        if not self.winfo_exists():
            return
        got = 0
        while got < 20:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.progress.config(
                    text="扫描完成：找到 %d 台" % self._found)
                return
            got += 1
            self._found += 1
            self.tree.insert("", "end", values=(
                "%s:%d" % (item["host"], item["port"]),
                "%.0f ms" % item["latency"],
                "巡考小车 · 可连接"))
            self.progress.config(text="已找到 %d 台…" % self._found)
        self.after(150, self._poll)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return ""
        return str(self.tree.item(sel[0], "values")[0])

    def _connect_selected(self):
        addr = self._selected()
        if not addr:
            messagebox.showinfo("局域网搜索", "先选中一台小车。", parent=self)
            return
        self.app.ip_var.set(addr)
        self._close()
        self.app.connect()

    def _close(self):
        self._stop.set()
        try:
            self.grab_release()
        except Exception:                                    # noqa: BLE001
            pass
        self.destroy()


# ======================================================================
# 主应用
# ======================================================================
class RobotApp(object):
    def __init__(self, root):
        self.root = root
        self.cfg = robot_link.load_config()
        self.link = None
        self.poller = None
        self.sender = None
        self.cam_worker = None
        self.sim = None
        self._demo = False
        self._closing = False

        self._ui_q = queue.Queue()
        self._pad_dirs = set()
        self._cam_seq = -1
        self._cam_frames = 0
        self._cam_fps_ts = time.time()
        self._cam_fps = 0.0
        self._last_state = None
        self._state_ok = False
        self._link_down_logged = False
        self._cam_busy = False
        self._connecting = False
        self._auto_attempt = False
        self._params_loaded = False
        self._lib_paths = {}
        self._file_items = {}
        self._photo_items = {}
        self._file_pending = ""
        self._photo_pending = ""
        self._photo_preview = None
        self._param_vars = {}
        self._param_widgets = []
        self._link_buttons = []

        self.root.title(APP_TITLE)
        self.root.geometry("1360x900")
        self.root.minsize(1240, 760)
        if ui_theme is not None:
            ui_theme.apply(self.root)
        self._setup_styles()
        self._build()
        if ui_theme is not None:
            ui_theme.polish_later(self.root)

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.root.bind("<ButtonRelease-1>", self._on_global_release)
        self.root.after(200, self._tick)

        if self.cfg.get("host") and self.cfg.get("auto_connect"):
            self._auto_attempt = True
            self.root.after(600, self.connect)

    # ------------------------------------------------------------ 样式
    def _setup_styles(self):
        st = ttk.Style(self.root)
        st.configure("Danger.TButton", background="#7f1d1d", foreground="#ffffff",
                     borderwidth=0, padding=(12, 8), font=FONT_B)
        st.map("Danger.TButton",
               background=[("active", "#991b1b"), ("disabled", PANEL2)],
               foreground=[("disabled", MUTE)])
        st.configure("Success.TButton", background="#065f46", foreground="#ffffff",
                     borderwidth=0, padding=(12, 7), font=FONT_B)
        st.map("Success.TButton",
               background=[("active", "#047857"), ("disabled", PANEL2)],
               foreground=[("disabled", MUTE)])
        st.configure("Small.TButton", background=PANEL2, foreground=DIM,
                     borderwidth=0, padding=(7, 3),
                     font=("Microsoft YaHei", 8))
        st.map("Small.TButton", background=[("active", BORDER)],
               foreground=[("active", FG)])
        st.configure("Card.TFrame", background=PANEL)
        st.configure("Card.TLabel", background=PANEL, foreground=FG)
        st.configure("CardDim.TLabel", background=PANEL, foreground=DIM,
                     font=FONT_SMALL)
        st.configure("CardAccent.TLabel", background=PANEL, foreground=ACCENT,
                     font=FONT_B)
        st.configure("Value.TLabel", background=PANEL, foreground=FG,
                     font=("Microsoft YaHei", 10, "bold"))
        st.configure("Unit.TLabel", background=PANEL, foreground=MUTE,
                     font=("Microsoft YaHei", 8))
        st.configure("TNotebook.Tab", padding=(14, 7))

    # ------------------------------------------------------------ 界面
    def _build(self):
        self._build_header()
        self._build_connbar()
        self._build_status()
        self._build_log()
        self._build_notebook()
        self._set_link_ui(False)

    def _build_header(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(12, 4))
        title = tk.Frame(head, bg=BG)
        title.pack(side="left")
        tk.Label(title, text="04 巡考机器人", bg=BG, fg=FG,
                 font=("Microsoft YaHei", 20, "bold")).pack(side="left")
        tk.Label(title, text="Windows 控制端 · 输入小车 IP 即可连接 · 无需安装 ROS",
                 bg=BG, fg=DIM, font=FONT).pack(side="left", padx=14)

        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        self.chip = Chip(right, "● 未连接", DIM)
        self.chip.pack(side="right")
        tk.Label(right, text="EDY_POWERQ", bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei", 9, "bold")).pack(side="right", padx=12)

    def _build_connbar(self):
        bar = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", padx=16, pady=(2, 6))
        inner = tk.Frame(bar, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        tk.Label(inner, text="小车 IP", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left")
        self.ip_var = tk.StringVar(value=self.cfg.get("host") or "")
        self.ip_box = ttk.Combobox(inner, textvariable=self.ip_var, width=22,
                                   values=self.cfg.get("history") or [])
        self.ip_box.pack(side="left", padx=(6, 10))
        self.ip_box.bind("<Return>", lambda e: self.connect())

        tk.Label(inner, text="端口", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.get("port") or
                                               robot_link.DEFAULT_PORT))
        port_entry = ttk.Entry(inner, textvariable=self.port_var, width=7)
        port_entry.pack(side="left", padx=(5, 10))
        port_entry.bind("<Return>", lambda e: self.connect())

        self.btn_connect = ttk.Button(inner, text="连接", style="Accent.TButton",
                                      command=self.connect)
        self.btn_connect.pack(side="left")
        self.btn_disconnect = ttk.Button(inner, text="断开", command=self.disconnect)
        self.btn_disconnect.pack(side="left", padx=6)
        self.btn_demo = ttk.Button(inner, text="本机演示", command=self.start_demo)
        self.btn_demo.pack(side="left", padx=(0, 6))
        self.btn_scan = ttk.Button(inner, text="局域网搜索",
                                   command=self.open_scan)
        self.btn_scan.pack(side="left")

        self.conn_hint = tk.Label(
            inner, text="提示：小车上先运行 webui/robot_web.py（默认 8080）",
            bg=PANEL, fg=MUTE, font=("Microsoft YaHei", 8))
        self.conn_hint.pack(side="right")

    def _build_status(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(side="bottom", fill="x", padx=18, pady=(2, 8))
        self.status_text = tk.Label(bar, text="未连接 · 请输入小车 IP 后点「连接」",
                                    bg=BG, fg=DIM, font=FONT_SMALL, anchor="w")
        self.status_text.pack(side="left", fill="x", expand=True)
        tk.Label(bar, text="%s ｜ 04 · Windows 控制端" % COPYRIGHT, bg=BG,
                 fg=MUTE, font=("Microsoft YaHei", 8)).pack(side="right")

    def _build_log(self):
        wrap = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(side="bottom", fill="x", padx=16, pady=(4, 2))
        head = tk.Frame(wrap, bg=PANEL)
        head.pack(fill="x", padx=10, pady=(5, 0))
        tk.Label(head, text="运行日志", bg=PANEL, fg=DIM,
                 font=("Microsoft YaHei", 8, "bold")).pack(side="left")
        ttk.Button(head, text="清空", style="Small.TButton",
                   command=self.clear_log).pack(side="right")
        ttk.Button(head, text="导出", style="Small.TButton",
                   command=self.export_log).pack(side="right", padx=4)
        body = tk.Frame(wrap, bg=PANEL)
        body.pack(fill="x", padx=10, pady=(2, 6))
        self.log_box = tk.Text(body, height=4, bg="#050a14", fg=FG,
                               insertbackground=FG, relief="flat", bd=0,
                               highlightthickness=0, wrap="none",
                               font=("Consolas", 9), padx=8, pady=4)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.tag_configure("info", foreground=DIM)
        self.log_box.tag_configure("ok", foreground=OK)
        self.log_box.tag_configure("warn", foreground=WARN)
        self.log_box.tag_configure("err", foreground=ALARM)
        self.log_box.configure(state="disabled")

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 2))
        self.tab_drive = tk.Frame(self.nb, bg=BG)
        self.tab_params = tk.Frame(self.nb, bg=BG)
        self.tab_remote = tk.Frame(self.nb, bg=BG)
        self.tab_local = tk.Frame(self.nb, bg=BG)
        self.tab_help = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_drive, text="  ① 遥控台  ")
        self.nb.add(self.tab_params, text="  ② 巡线参数  ")
        self.nb.add(self.tab_remote, text="  ③ 远端文件  ")
        self.nb.add(self.tab_local, text="  ④ 本地资料  ")
        self.nb.add(self.tab_help, text="  ⑤ 使用说明  ")
        self._build_tab_drive(self.tab_drive)
        self._build_tab_params(self.tab_params)
        self._build_tab_remote(self.tab_remote)
        self._build_tab_local(self.tab_local)
        self._build_tab_help(self.tab_help)

    # ------------------------------------------------------------ 遥控台
    def _build_tab_drive(self, tab):
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=0)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(2, weight=0)

        # ---- 左：运动控制 ----
        left_scroll = ScrollFrame(tab, bg=BG, width=306)
        left_scroll.grid(row=0, column=0, sticky="nsew", padx=(2, 10), pady=6)
        left = left_scroll.inner

        card = Card(left, "运动控制", "按住才动 · 松开即停")
        card.pack(fill="x", pady=(0, 8))
        body = card.body
        tk.Label(body, text="方向键 / WASD 按住移动；转向会自动带 0.09 m/s 前进"
                            "（阿克曼底盘不能原地转）",
                 bg=PANEL, fg=DIM, font=("Microsoft YaHei", 8),
                 justify="left", wraplength=270).pack(fill="x", pady=(0, 4))
        self.pad = ControlPad(body, self._pad_press, self._pad_release,
                              self._stop_motion)
        self.pad.pack()
        self._link_buttons.extend(self.pad.buttons.values())

        sliders = tk.Frame(body, bg=PANEL)
        sliders.pack(fill="x", pady=(8, 2))
        self.linear_var = tk.DoubleVar(value=float(self.cfg.get("linear", 0.15)))
        self.angular_var = tk.DoubleVar(value=float(self.cfg.get("angular", 0.45)))
        self._slider_row(sliders, "线速度", self.linear_var, 0.05, 0.20,
                         "m/s", "%+.2f")
        self._slider_row(sliders, "角速度", self.angular_var, 0.10, 0.60,
                         "rad/s", "%.2f")

        self.btn_stop = ttk.Button(body, text="停止", style="Accent.TButton",
                                   command=self._stop_motion)
        self.btn_stop.pack(fill="x", pady=(8, 4))
        self.btn_estop = tk.Button(
            body, text="⛔ 急停 E-STOP", command=self.toggle_estop,
            bg="#7f1d1d", fg="#ffffff", activebackground="#b91c1c",
            activeforeground="#ffffff", relief="flat", bd=0,
            highlightthickness=0, font=FONT_B, cursor="hand2", pady=6)
        self.btn_estop.pack(fill="x")
        self._link_buttons.append(self.btn_estop)
        self.motion_label = tk.Label(body, text="当前指令 v=0.00  w=0.00",
                                     bg=PANEL, fg=MUTE,
                                     font=("Microsoft YaHei", 8))
        self.motion_label.pack(fill="x", pady=(5, 0))

        card2 = Card(left, "一键跟随线", "黑线 / 白线")
        card2.pack(fill="x", pady=(0, 8))
        b2 = card2.body
        self.line_color_var = tk.StringVar(value="黑线")
        row = tk.Frame(b2, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="线色", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left")
        self.line_color_box = ttk.Combobox(
            row, textvariable=self.line_color_var, state="readonly",
            values=[label for _key, label in LINE_COLORS], width=10)
        self.line_color_box.pack(side="left", padx=6)
        self.btn_line_start = ttk.Button(row, text="开始跟随",
                                         style="Success.TButton",
                                         command=lambda: self.set_line(True))
        self.btn_line_start.pack(side="left")
        self._link_buttons.extend([self.btn_line_start, self.line_color_box])
        foot = tk.Frame(b2, bg=PANEL)
        foot.pack(fill="x", pady=(6, 0))
        self.line_state_label = tk.Label(foot, text="巡线未运行", bg=PANEL,
                                         fg=MUTE, font=("Microsoft YaHei", 8),
                                         anchor="w")
        self.line_state_label.pack(side="left", fill="x", expand=True)
        self.btn_line_stop = ttk.Button(foot, text="停止", style="Small.TButton",
                                        command=lambda: self.set_line(False))
        self.btn_line_stop.pack(side="right", padx=(0, 4))
        self.btn_line_log = ttk.Button(foot, text="日志", style="Small.TButton",
                                       command=self.show_line_log)
        self.btn_line_log.pack(side="right")
        self._link_buttons.extend([self.btn_line_stop, self.btn_line_log])

        hint = tk.Frame(left, bg=BG)
        hint.pack(fill="x", pady=(2, 6))
        tk.Label(hint,
                 text="安全：急停锁定 / 前方 0.35 m 避障 / 松手 0.6 s 看门狗；\n"
                      "考试时段严禁把小车开进考场内部。",
                 bg=BG, fg=MUTE, justify="left",
                 font=("Microsoft YaHei", 8), wraplength=280).pack(fill="x")

        # ---- 中：画面 ----
        center = tk.Frame(tab, bg=BG)
        center.grid(row=0, column=1, sticky="nsew", pady=6)
        center.grid_rowconfigure(0, weight=1)
        center.grid_columnconfigure(0, weight=1)

        cam_card = tk.Frame(center, bg=PANEL, highlightbackground=BORDER,
                            highlightthickness=1)
        cam_card.grid(row=0, column=0, sticky="nsew")
        cam_card.grid_rowconfigure(3, weight=1)
        cam_card.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(cam_card, bg=PANEL)
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(7, 2))
        tk.Label(toolbar, text="画面来源", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left")
        self.cam_var = tk.StringVar(value=self.cfg.get("cam", "astra"))
        for key, label in (("astra", "Astra RGB"), ("depth", "深度"), ("usb", "USB")):
            rb = ttk.Radiobutton(toolbar, text=label, value=key,
                                 variable=self.cam_var,
                                 command=self._cam_source_changed)
            rb.pack(side="left", padx=(6, 0))
            self._link_buttons.append(rb)
        tk.Label(toolbar, text="帧率", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left", padx=(12, 2))
        self.fps_var = tk.IntVar(value=int(self.cfg.get("fps", 10)))
        fps = ttk.Spinbox(toolbar, from_=1, to=20, width=4,
                          textvariable=self.fps_var, command=self._fps_changed)
        fps.pack(side="left")
        fps.bind("<FocusOut>", lambda e: self._fps_changed())
        self._link_buttons.append(fps)
        self.cam_source_label = tk.Label(toolbar, text="等待连接", bg=PANEL,
                                         fg=MUTE, font=("Microsoft YaHei", 8),
                                         anchor="e")
        self.cam_source_label.pack(side="right")

        actions = tk.Frame(cam_card, bg=PANEL)
        actions.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.btn_browser = ttk.Button(actions, text="浏览器打开",
                                      style="Small.TButton",
                                      command=self.open_car_web)
        self.btn_browser.pack(side="left")
        self.btn_photo = ttk.Button(actions, text="拍照到小车",
                                    style="Small.TButton",
                                    command=self.photo_to_car)
        self.btn_photo.pack(side="left", padx=6)
        self.btn_save_frame = ttk.Button(actions, text="保存画面到本机",
                                         style="Small.TButton",
                                         command=self.save_frame)
        self.btn_save_frame.pack(side="left")
        self._link_buttons.extend([self.btn_save_frame, self.btn_photo,
                                   self.btn_browser])

        hw = tk.Frame(cam_card, bg=PANEL)
        hw.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        tk.Label(hw, text="车载源", bg=PANEL, fg=DIM,
                 font=FONT_SMALL).pack(side="left")
        for text, src in (("Astra", "astra"), ("USB", "usb"), ("双开", "both")):
            btn = ttk.Button(hw, text=text, style="Small.TButton",
                             command=lambda s=src: self.switch_camsource(s))
            btn.pack(side="left", padx=(5, 0))
            self._link_buttons.append(btn)
        self.camsrc_label = tk.Label(hw, text="真机切换需 10~15 秒", bg=PANEL,
                                     fg=MUTE, font=("Microsoft YaHei", 8),
                                     anchor="w")
        self.camsrc_label.pack(side="left", padx=10)

        self.camera = ImagePanel(cam_card, "未连接小车\n输入小车 IP 后点「连接」")
        self.camera.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 6))

        info = tk.Frame(cam_card, bg=PANEL)
        info.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 7))
        self.cam_info = tk.Label(info, text="", bg=PANEL, fg=MUTE,
                                 font=("Microsoft YaHei", 8), anchor="w")
        self.cam_info.pack(side="left")
        self.cam_quality = tk.Label(info, text=robot_camera.availability_text(),
                                    bg=PANEL, fg=MUTE,
                                    font=("Microsoft YaHei", 8), anchor="e")
        self.cam_quality.pack(side="right")

        # ---- 右：遥测 ----
        right_scroll = ScrollFrame(tab, bg=BG, width=352)
        right_scroll.grid(row=0, column=2, sticky="nsew", padx=(10, 2), pady=6)
        right = right_scroll.inner

        card = Card(right, "运行与安全", "低于 24 V 扭矩不足")
        card.pack(fill="x", pady=(0, 7))
        grid = card.body
        self.v_link = self._info_row(grid, 0, "控制服务")
        self.v_latency = self._info_row(grid, 1, "延迟 / 运行")
        self.v_safe = self._info_row(grid, 2, "安全状态")
        self.v_mode = self._info_row(grid, 3, "模式")
        self.v_battery = self._info_row(grid, 4, "电池电压")
        self.v_cmd = self._info_row(grid, 5, "当前指令")
        self.v_line = self._info_row(grid, 6, "巡线状态")
        self.battery_bar = BatteryBar(card.body, width=310)
        self.battery_bar.grid(row=7, column=0, columnspan=2, sticky="ew",
                              pady=(5, 0))

        card = Card(right, "位姿与姿态")
        card.pack(fill="x", pady=(0, 7))
        grid = card.body
        self.v_odom = self._info_row(grid, 0, "里程计 x/y")
        self.v_yaw = self._info_row(grid, 1, "航向 yaw")
        self.v_tilt = self._info_row(grid, 2, "倾角 pitch/roll")

        card = Card(right, "雷达避障", "前方 ±25° 停车保护")
        card.pack(fill="x")
        self.radar = Radar(card.body, height=200)
        self.radar.pack(fill="x")
        self.v_limit = tk.Label(card.body, text="限速 0.20 m/s · 避障 0.35 m",
                                bg=PANEL, fg=MUTE, font=("Microsoft YaHei", 8))
        self.v_limit.pack(fill="x", pady=(2, 0))

    def _slider_row(self, parent, label, var, lo, hi, unit, fmt):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=PANEL, fg=DIM, width=7, anchor="w",
                 font=("Microsoft YaHei", 8)).pack(side="left")
        value_label = tk.Label(row, text=(fmt % var.get()) + " " + unit,
                               bg=PANEL, fg=FG, width=12, anchor="e",
                               font=("Microsoft YaHei", 8, "bold"))
        value_label.pack(side="right")
        scale = tk.Scale(row, variable=var, from_=lo, to=hi, resolution=0.01,
                         orient="horizontal", showvalue=0, bg=PANEL,
                         fg=FG, troughcolor=PANEL2, activebackground=ACCENT,
                         highlightthickness=0, bd=0, sliderrelief="flat",
                         sliderlength=14, length=150, width=8,
                         command=lambda v: value_label.config(
                             text=(fmt % float(v)) + " " + unit))
        scale.pack(side="left", fill="x", expand=True, padx=(2, 4))
        var.trace_add("write", lambda *a: value_label.config(
            text=(fmt % var.get()) + " " + unit))
        return scale

    def _info_row(self, parent, row, label):
        tk.Label(parent, text=label, bg=PANEL, fg=DIM, width=10, anchor="w",
                 font=("Microsoft YaHei", 8), bd=0, padx=0, pady=0).grid(
            row=row, column=0, sticky="w")
        value = tk.Label(parent, text="—", bg=PANEL, fg=FG, anchor="w",
                         font=("Microsoft YaHei", 8), bd=0, padx=0, pady=0)
        value.grid(row=row, column=1, sticky="w")
        parent.grid_columnconfigure(1, weight=1)
        return value

    # ------------------------------------------------------------ 参数页
    def _build_tab_params(self, tab):
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", padx=6, pady=(8, 4))
        tk.Label(bar, text="巡线参数", bg=BG, fg=FG, font=FONT_H1).pack(side="left")
        self.btn_params_read = ttk.Button(bar, text="读取小车参数",
                                          command=self.read_params)
        self.btn_params_read.pack(side="left", padx=(14, 6))
        self.btn_params_save = ttk.Button(bar, text="保存到小车",
                                          style="Accent.TButton",
                                          command=self.save_params)
        self.btn_params_save.pack(side="left")
        self.btn_params_reset = ttk.Button(bar, text="恢复默认",
                                           command=self.reset_params)
        self.btn_params_reset.pack(side="left", padx=6)
        self.btn_params_web = ttk.Button(bar, text="小车网页参数面板",
                                         command=self.open_car_web)
        self.btn_params_web.pack(side="left")
        self._link_buttons.extend([self.btn_params_read, self.btn_params_save,
                                   self.btn_params_reset])
        self.params_status = tk.Label(bar, text="连接小车后可读取/保存",
                                      bg=BG, fg=MUTE, font=FONT_SMALL)
        self.params_status.pack(side="right")

        tk.Label(tab, text="参数写入小车 webui/line_params.json，下一次「开始跟随」生效；"
                           "带 * 的值会被小车端做语法/范围校验。",
                 bg=BG, fg=DIM, font=FONT_SMALL, anchor="w").pack(
            fill="x", padx=8, pady=(0, 4))

        scroll = ScrollFrame(tab, bg=BG)
        scroll.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        inner = scroll.inner
        grid = tk.Frame(inner, bg=BG)
        grid.pack(fill="both", expand=True)
        grid.grid_columnconfigure(0, weight=1, uniform="param")
        grid.grid_columnconfigure(1, weight=1, uniform="param")
        for idx, (title, items) in enumerate(PARAM_GROUPS):
            card = Card(grid, title)
            card.grid(row=idx // 2, column=idx % 2, sticky="nsew",
                      padx=(0, 8), pady=(0, 8))
            body = card.body
            for key, label, kind, hint in items:
                row = tk.Frame(body, bg=PANEL)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=label, bg=PANEL, fg=FG, width=17,
                         anchor="w", font=FONT_SMALL).pack(side="left")
                if key == "line_color":
                    var = self.line_color_var
                else:
                    var = tk.StringVar(
                        value=str(robot_link.LINE_PARAMS.get(key, "")))
                self._param_vars[key] = var
                if kind == "choice":
                    widget = ttk.Combobox(
                        row, textvariable=var, state="readonly", width=13,
                        values=[label for _k, label in LINE_COLORS])
                    widget.pack(side="left", padx=(0, 8))
                else:
                    widget = ttk.Entry(row, textvariable=var, width=14)
                    widget.pack(side="left", padx=(0, 8))
                self._link_buttons.append(widget)
                self._param_widgets.append(widget)
                tk.Label(row, text=hint, bg=PANEL, fg=MUTE,
                         font=("Microsoft YaHei", 8)).pack(side="left")

    # ------------------------------------------------------------ 远端页
    def _build_tab_remote(self, tab):
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", padx=6, pady=(8, 4))
        tk.Label(bar, text="小车上的文件", bg=BG, fg=FG,
                 font=FONT_H1).pack(side="left")
        self.btn_files = ttk.Button(bar, text="刷新列表",
                                    command=self.refresh_remote)
        self.btn_files.pack(side="left", padx=(14, 6))
        ttk.Button(bar, text="打开小车网页",
                   command=self.open_car_web).pack(side="left")
        ttk.Button(bar, text="巡线日志",
                   command=self.show_line_log).pack(side="left", padx=6)
        self.files_status = tk.Label(bar, text="连接后显示小车代码/配置（只读预览）",
                                     bg=BG, fg=MUTE, font=FONT_SMALL)
        self.files_status.pack(side="right")
        self._link_buttons.append(self.btn_files)

        sub = ttk.Notebook(tab)
        sub.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # ---- 代码/配置 ----
        page = tk.Frame(sub, bg=BG)
        sub.add(page, text="  代码与配置  ")
        pane = ttk.Panedwindow(page, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=4)

        left = tk.Frame(pane, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        cols = ("rel", "size", "mtime")
        self.file_tree = ttk.Treeview(left, columns=cols, show="headings")
        for col, text, width in (("rel", "文件", 230), ("size", "大小", 70),
                                 ("mtime", "修改", 90)):
            self.file_tree.heading(col, text=text)
            self.file_tree.column(col, width=width,
                                  anchor="w" if col == "rel" else "center")
        self.file_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.file_tree.yview)
        sb.pack(side="right", fill="y")
        self.file_tree.configure(yscrollcommand=sb.set)
        self.file_tree.bind("<<TreeviewSelect>>", self._file_selected)
        pane.add(left, weight=1)

        right = tk.Frame(pane, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        head = tk.Frame(right, bg=PANEL)
        head.pack(fill="x", padx=8, pady=(5, 2))
        self.file_title = tk.Label(head, text="选择左侧文件预览", bg=PANEL,
                                   fg=DIM, font=FONT_SMALL, anchor="w")
        self.file_title.pack(side="left")
        ttk.Button(head, text="保存到本机", style="Small.TButton",
                   command=self.save_remote_file).pack(side="right")
        self.file_text = tk.Text(right, bg="#050a14", fg=FG,
                                 insertbackground=FG, relief="flat", bd=0,
                                 highlightthickness=0, wrap="none",
                                 font=("Consolas", 9), padx=8, pady=6)
        self.file_text.pack(side="left", fill="both", expand=True)
        fsb = ttk.Scrollbar(right, orient="vertical", command=self.file_text.yview)
        fsb.pack(side="right", fill="y")
        self.file_text.configure(yscrollcommand=fsb.set, state="disabled")
        pane.add(right, weight=3)

        # ---- 照片 ----
        page2 = tk.Frame(sub, bg=BG)
        sub.add(page2, text="  巡更照片  ")
        bar2 = tk.Frame(page2, bg=BG)
        bar2.pack(fill="x", pady=4)
        self.btn_photos = ttk.Button(bar2, text="刷新照片",
                                     command=self.refresh_photos)
        self.btn_photos.pack(side="left")
        ttk.Button(bar2, text="保存选中到本机", style="Small.TButton",
                   command=self.save_remote_photo).pack(side="left", padx=8)
        self.photo_status = tk.Label(bar2, text="", bg=BG, fg=MUTE,
                                     font=FONT_SMALL)
        self.photo_status.pack(side="right")
        self._link_buttons.append(self.btn_photos)

        pane2 = ttk.Panedwindow(page2, orient="horizontal")
        pane2.pack(fill="both", expand=True)
        left2 = tk.Frame(pane2, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        self.photo_tree = ttk.Treeview(left2, columns=("name", "size"),
                                       show="headings")
        self.photo_tree.heading("name", text="照片")
        self.photo_tree.heading("size", text="大小")
        self.photo_tree.column("name", width=250, anchor="w")
        self.photo_tree.column("size", width=80, anchor="center")
        self.photo_tree.pack(side="left", fill="both", expand=True)
        psb = ttk.Scrollbar(left2, orient="vertical",
                            command=self.photo_tree.yview)
        psb.pack(side="right", fill="y")
        self.photo_tree.configure(yscrollcommand=psb.set)
        self.photo_tree.bind("<<TreeviewSelect>>", self._photo_selected)
        pane2.add(left2, weight=1)
        right2 = tk.Frame(pane2, bg=PANEL, highlightbackground=BORDER,
                          highlightthickness=1)
        self.photo_view = ImagePanel(right2, "选择左侧照片预览", min_w=200,
                                     min_h=160)
        self.photo_view.pack(fill="both", expand=True, padx=6, pady=6)
        pane2.add(right2, weight=3)

    # ------------------------------------------------------------ 本地页
    def _build_tab_local(self, tab):
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", padx=6, pady=(8, 4))
        tk.Label(bar, text="本地资料", bg=BG, fg=FG,
                 font=FONT_H1).pack(side="left")
        tk.Label(bar, text="巡考机器人/ 代码 · 文档 · 图示 · 测试记录",
                 bg=BG, fg=DIM, font=FONT_SMALL).pack(side="left", padx=12)
        ttk.Button(bar, text="打开选中", command=self.open_local).pack(side="right")
        ttk.Button(bar, text="打开所在文件夹",
                   command=self.open_local_dir).pack(side="right", padx=6)
        ttk.Button(bar, text="复制路径", command=self.copy_local_path).pack(side="right")
        ttk.Button(bar, text="导出部署包",
                   command=self.export_deploy).pack(side="right", padx=6)
        ttk.Button(bar, text="打开说明",
                   command=self.open_guide).pack(side="right")

        wrap = tk.Frame(tab, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        cols = ("kind", "size")
        self.lib_tree = ttk.Treeview(wrap, columns=cols,
                                     show="tree headings")
        self.lib_tree.heading("#0", text="目录 / 文件")
        self.lib_tree.heading("kind", text="类型")
        self.lib_tree.heading("size", text="大小")
        self.lib_tree.column("#0", width=520, stretch=True)
        self.lib_tree.column("kind", width=90, anchor="center", stretch=False)
        self.lib_tree.column("size", width=100, anchor="e", stretch=False)
        self.lib_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.lib_tree.yview)
        sb.pack(side="right", fill="y")
        self.lib_tree.configure(yscrollcommand=sb.set)
        self.lib_tree.bind("<Double-1>", lambda e: self.open_local())
        self._load_library()

    def _build_tab_help(self, tab):
        wrap = tk.Frame(tab, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        text = tk.Text(wrap, bg="#050a14", fg=FG, relief="flat", bd=0,
                       highlightthickness=0, wrap="word", padx=18, pady=14,
                       font=("Microsoft YaHei", 10), spacing1=2, spacing3=4)
        text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        sb.pack(side="right", fill="y")
        text.configure(yscrollcommand=sb.set)
        text.tag_configure("h1", foreground=ACCENT,
                           font=("Microsoft YaHei", 14, "bold"),
                           spacing1=8, spacing3=6)
        text.tag_configure("h2", foreground=FG,
                           font=("Microsoft YaHei", 11, "bold"),
                           spacing1=8, spacing3=4)
        text.tag_configure("dim", foreground=DIM)
        text.tag_configure("warn", foreground=WARN)
        text.tag_configure("ok", foreground=OK)
        text.tag_configure("code", foreground="#9ad7ff", font=("Consolas", 9),
                           background="#0a1524")
        self._fill_help(text)
        text.configure(state="disabled")

    def _fill_help(self, text):
        def add(txt, tag=None):
            text.insert("end", txt, tag or ())

        add("04 巡考机器人 · Windows 控制端\n", "h1")
        add("小车端是 Ubuntu + ROS1/ROS2，Windows 端不需要安装 ROS，"
            "只通过局域网 HTTP 控制。\n", "dim")
        add("一、连接步骤\n", "h2")
        add("1. 在小车上启动控制服务（二选一）：\n")
        add("   cd ~/patrol_robot && python3 webui/robot_web.py --dry-run   # 先空跑\n"
            "   cd ~/patrol_robot && python3 webui/robot_web.py              # 实车\n", "code")
        add("2. 确认小车 IP（小车终端执行 hostname -I），Windows 与小车接同一路由/热点。\n")
        add("3. 在顶部输入小车 IP（如 192.168.1.50，默认端口 8080），点「连接」。\n")
        add("4. 连接成功后：中间看画面，左侧方向盘按住才动，右侧看遥测与雷达。\n")
        add("5. 没有真车时点「本机演示」：自动在本机 18080 起一个模拟小车，"
            "界面功能可完整演示。\n")
        add("二、画面说明\n", "h2")
        add("画面来自小车 /api/snap?cam=astra|depth|usb，由 tools/snapshot_server.py "
            "把三路相机写成快照；没有画面时先在小车确认：\n")
        add("   python3 tools/snapshot_server.py --fps 10\n", "code")
        add("车载相机源（Astra / USB / 双开）在左侧切换，真机需要 10~15 秒重启相机。\n")
        add("三、键盘与安全\n", "h2")
        add("· W / ↑ 前进，S / ↓ 后退，A / ← 左转，D / → 右转，空格停车；\n"
            "· 松开按键/鼠标立即下发零速，小车 0.6 s 看门狗再兜底；\n"
            "· 转向自动附带 0.09 m/s 前进速度（阿克曼底盘不能原地转）；\n"
            "· E-STOP 后小车拒绝所有速度指令，解除后需要重新给方向；\n"
            "· 前方 ±25°、0.35 m 内有障碍时前进被拦截（后退不受限）。\n", "warn")
        add("四、巡线参数\n", "h2")
        add("「② 巡线参数」读写小车 webui/line_params.json；保存后下一次"
            "「开始跟随」生效。参数含义见每个字段后的说明。\n")
        add("五、故障排查\n", "h2")
        add("· 连接超时：检查 IP、同一网段、小车是否开机；Windows 防火墙放行出站；\n"
            "· 端口拒绝：小车没运行 robot_web.py，或端口不是 8080；\n"
            "· 能连接但无画面：snapshot_server.py 没启动，或相机被另一路占用；\n"
            "· 界面不显示画面：本机缺 Pillow —— 打包运行时已自带，"
            "源码运行时执行 python -m pip install pillow；\n"
            "· 指令无效：急停是否锁定、巡线是否在跑（巡线时手动被锁定）、"
            "是否被障碍拦截（看右侧「安全状态」）。\n", "dim")
        add("六、本模块的 Python 文件\n", "h2")
        add("· gui.py          本界面\n"
            "· robot_link.py   小车 HTTP 协议客户端 + 轮询/指令线程 + 局域网发现\n"
            "· robot_camera.py 快照抓图线程 + JPEG 显示/保存\n"
            "· robot_sim.py    本机演示用模拟小车（与 robot_web.py 同接口）\n"
            "· 自检.py         无界面自测：起模拟车 → 验证全部接口\n", "code")
        add("七、定位与边界\n", "h2")
        add("机器人用于考点公共区域（楼道、连廊、保密室、机房）巡更与夜间值守；"
            "考场内部考试进行中不部署。详细方案见 巡考机器人.md / 02_文档。\n",
            "dim")

    # ------------------------------------------------------------ 连接
    def connect(self):
        if self._connecting:
            return
        text = self.ip_var.get().strip()
        try:
            target = robot_link.parse_target(text, self.port_var.get())
        except ValueError as exc:
            messagebox.showwarning("地址不正确", str(exc), parent=self.root)
            return
        if self.link is not None:
            self.disconnect()
        self._connecting = True
        self.chip.set("● 连接中…", WARN)
        self.conn_hint.config(text="正在连接 %s …" % target, fg=WARN)
        self.btn_connect.config(state="disabled")
        self.status_text.config(text="正在连接 %s …" % target, fg=WARN)
        self._log("正在连接 %s" % target, "info")

        def work():
            try:
                state, latency = robot_link.RobotLink(target).probe()
            except Exception as exc:                         # noqa: BLE001
                self._ui_q.put(("connect_fail", (target, str(exc))))
            else:
                self._ui_q.put(("connect_ok", (target, state, latency)))

        threading.Thread(target=work, daemon=True).start()

    def _connect_ok(self, target, state, latency):
        self._connecting = False
        self._auto_attempt = False
        self.btn_connect.config(state="normal")
        self.link = robot_link.RobotLink(target)
        self.poller = robot_link.Poller(self.link, interval=0.3)
        self.poller.start()
        self.sender = robot_link.CommandSender(self.link)
        self.sender.start()
        self.cam_worker = robot_camera.CameraWorker(
            self.link, cam=self.cam_var.get(), fps=self.fps_var.get())
        self.cam_worker.start()
        self._cam_seq = -1
        self._params_loaded = False
        self._link_down_logged = False
        self._demo = (target.host in ("127.0.0.1", "localhost")
                      and self.sim is not None)
        self.ip_var.set(str(target))
        self.port_var.set(str(target.port))
        self._set_link_ui(True)
        self._apply_state(state)
        self.chip.set("● 已连接 %d ms" % latency, OK)
        self.conn_hint.config(
            text=("演示模式：%s" % target) if self._demo
            else "已连接 %s · 小车端 robot_web.py" % target,
            fg=OK if not self._demo else ACCENT)
        self.status_text.config(text="已连接 %s" % target, fg=OK)
        self._log("连接成功：%s（%.0f ms）%s"
                  % (target, latency, " · 本机演示" if self._demo else ""), "ok")
        history = robot_link.remember_host(target.host, target.port)
        self.ip_box.config(values=history)
        cfg = robot_link.load_config()
        cfg.update({"host": target.host, "port": target.port,
                    "auto_connect": True})
        robot_link.save_config(cfg)
        self.read_params(silent=True)
        self.refresh_remote(silent=True)

    def _connect_fail(self, target, message):
        self._connecting = False
        self.btn_connect.config(state="normal")
        self.chip.set("● 未连接", DIM)
        self.conn_hint.config(text="连接失败 · 检查 IP / 网段 / 小车服务", fg=ALARM)
        self.status_text.config(text="连接失败：%s" % message, fg=ALARM)
        self._log("连接失败 %s：%s" % (target, message), "err")
        if self._auto_attempt:
            self._auto_attempt = False
            self.conn_hint.config(text="自动连接失败 · 可手动重试或点「本机演示」",
                                  fg=WARN)
            return
        messagebox.showerror(
            "连接失败",
            "无法连接 %s\n\n%s\n\n排查建议：\n"
            "1) 小车端是否已运行 webui/robot_web.py（默认 0.0.0.0:8080）；\n"
            "2) Windows 与小车是否在同一网段（小车执行 hostname -I 确认）；\n"
            "3) 端口是否正确，Windows 防火墙是否允许出站；\n"
            "4) 没有真车可点「本机演示」体验完整功能。" % (target, message),
            parent=self.root)

    def disconnect(self):
        if self.link is None and not self._connecting:
            return
        link = self.link
        self.link = None
        self._connecting = False
        if self.poller is not None:
            self.poller.stop()
            self.poller = None
        if self.sender is not None:
            self.sender.stop()
            self.sender = None
        if self.cam_worker is not None:
            self.cam_worker.stop()
            self.cam_worker = None
        if link is not None:
            def safe_stop():
                try:
                    link.stop()
                except Exception:                            # noqa: BLE001
                    pass
            threading.Thread(target=safe_stop, daemon=True).start()
        self._pad_dirs.clear()
        self._set_link_ui(False)
        self._reset_telemetry()
        self.chip.set("● 未连接", DIM)
        self.conn_hint.config(text="提示：小车上先运行 webui/robot_web.py（默认 8080）",
                              fg=MUTE)
        self.status_text.config(text="已断开连接", fg=DIM)
        self.cam_info.config(text="")
        self.cam_source_label.config(text="等待连接")
        self.camera.set_text("未连接小车\n输入小车 IP 后点「连接」")
        self._log("已断开连接", "info")

    def _set_link_ui(self, connected):
        state = "normal" if connected else "disabled"
        for widget in self._link_buttons:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.config(state="readonly" if connected else "disabled")
                else:
                    widget.config(state=state)
            except Exception:                                # noqa: BLE001
                pass
        self.btn_disconnect.config(state="normal" if connected else "disabled")
        if not connected:
            self.pad.set_enabled(False)
        self.btn_params_read.config(state=state)
        self.btn_params_save.config(state=state)
        self.btn_params_reset.config(state=state)
        self.btn_files.config(state=state)
        self.btn_photos.config(state=state)
        self.params_status.config(
            text="连接小车后可读取/保存" if not connected else "已连接，可读取/保存")

    def _reset_telemetry(self):
        for label in (self.v_link, self.v_latency, self.v_safe, self.v_mode,
                      self.v_odom, self.v_yaw, self.v_tilt, self.v_cmd,
                      self.v_line):
            label.config(text="—", fg=FG)
        self.v_battery.config(text="—", fg=DIM)
        self.battery_bar.set(0, MUTE)
        self.v_limit.config(text="限速 0.20 m/s · 避障 0.35 m")
        self.radar.update_data({}, 0.35)
        self.camsrc_label.config(text="未切换", fg=MUTE)
        self.motion_label.config(text="当前指令 v=0.00  w=0.00")
        self.line_state_label.config(text="巡线未运行", fg=MUTE)
        self.btn_estop.config(text="⛔ 急停 E-STOP", bg="#7f1d1d")

    # ------------------------------------------------------------ 演示
    def start_demo(self):
        if robot_sim is None:
            messagebox.showerror("本机演示", "缺少 robot_sim.py，无法启动模拟小车。",
                                 parent=self.root)
            return
        if self.sim is None:
            try:
                self.sim = robot_sim.SimCar("127.0.0.1",
                                            robot_link.DEMO_PORT).start()
            except Exception as exc:                         # noqa: BLE001
                messagebox.showerror("本机演示", "模拟小车启动失败：\n%s" % exc,
                                     parent=self.root)
                return
            self._log("本机演示小车已启动：%s" % self.sim.url, "info")
        self.ip_var.set("127.0.0.1")
        self.port_var.set(str(self.sim.port))
        self.connect()

    def open_scan(self):
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = robot_link.DEFAULT_PORT
        ScanDialog(self, port)

    # ------------------------------------------------------------ 动作
    def _pad_press(self, direction):
        if self.link is None or not self._state_ok:
            return
        self._pad_dirs.add(direction)
        self._apply_motion()

    def _pad_release(self, direction):
        self._pad_dirs.discard(direction)
        self._apply_motion()

    def _on_global_release(self, event):
        if self._pad_dirs:
            self._pad_dirs.clear()
            self._apply_motion()

    def _apply_motion(self):
        if self.link is None or self.sender is None:
            return
        state = self._last_state or {}
        if state.get("estop"):
            self.sender.set(0.0, 0.0)
            return
        if (state.get("line") or {}).get("running"):
            self.sender.set(0.0, 0.0)
            return
        linear = float(self.linear_var.get())
        angular = float(self.angular_var.get())
        dirs = self._pad_dirs
        v = w = 0.0
        if "f" in dirs:
            v = linear
        elif "b" in dirs:
            v = -linear * 0.6
        if "l" in dirs or "r" in dirs:
            w = angular if "l" in dirs else -angular
            if "f" not in dirs and "b" not in dirs:
                v = min(linear, 0.09)
            elif "b" in dirs:
                v = -min(linear * 0.4, 0.06)
            else:
                w *= 0.85
        self.sender.set(v, w)
        self.motion_label.config(text="当前指令 v=%+.2f  w=%+.2f" % (v, w))

    def _stop_motion(self):
        self._pad_dirs.clear()
        if self.sender is not None:
            self.sender.set(0.0, 0.0)
        self.motion_label.config(text="已下发停车 v=0.00  w=0.00")

    def _on_key_press(self, event):
        if self._focus_is_text(event.widget):
            return
        key = (event.keysym or "").lower()
        mapping = {"w": "f", "up": "f", "s": "b", "down": "b",
                   "a": "l", "left": "l", "d": "r", "right": "r"}
        if key == "space":
            self._stop_motion()
            return "break"
        direction = mapping.get(key)
        if direction is None or self.link is None:
            return
        if direction not in self._pad_dirs:
            self._pad_dirs.add(direction)
            self._apply_motion()
        return "break"

    def _on_key_release(self, event):
        if self._focus_is_text(event.widget):
            return
        key = (event.keysym or "").lower()
        mapping = {"w": "f", "up": "f", "s": "b", "down": "b",
                   "a": "l", "left": "l", "d": "r", "right": "r"}
        direction = mapping.get(key)
        if direction and direction in self._pad_dirs:
            self._pad_dirs.discard(direction)
            self._apply_motion()
            return "break"

    @staticmethod
    def _focus_is_text(widget):
        return isinstance(widget, (tk.Entry, ttk.Entry, tk.Text,
                                   ttk.Combobox, ttk.Spinbox, tk.Scale))

    def toggle_estop(self):
        if self.link is None:
            return
        state = self._last_state or {}
        on = not bool(state.get("estop"))
        if on:
            if not messagebox.askyesno(
                    "急停", "立即锁定小车（所有速度指令被拒绝）？",
                    parent=self.root):
                return
        else:
            if not messagebox.askyesno("解除急停", "确认解除急停，允许再次移动？",
                                       parent=self.root):
                return
        if on:
            self._pad_dirs.clear()
            if self.sender is not None:
                self.sender.set(0.0, 0.0)
        self._io(lambda: self.link.estop(on),
                 ok=lambda res: self._after_estop(on, res),
                 err=lambda exc: self._action_failed("急停", exc))

    def _after_estop(self, on, result):
        self._log("已急停（E-STOP）" if on else "已解除急停",
                  "warn" if on else "ok")
        self.status_text.config(text="急停锁定中" if on else "急停已解除",
                                fg=ALARM if on else OK)

    def set_line(self, on):
        if self.link is None:
            return
        state = self._last_state or {}
        if on and state.get("estop"):
            messagebox.showwarning("巡线", "急停锁定中，先解除急停再启动巡线。",
                                   parent=self.root)
            return
        color = "black"
        for key, label in LINE_COLORS:
            if label == self.line_color_var.get():
                color = key
                break
        self.line_state_label.config(
            text="正在启动巡线…" if on else "正在停止巡线…", fg=WARN)
        self._io(lambda: self.link.line(on, color if on else None),
                 ok=lambda res: self._after_line(on, res),
                 err=lambda exc: self._action_failed("巡线", exc))

    def _after_line(self, on, result):
        if not result.get("ok", 1):
            messagebox.showwarning("巡线", result.get("err", "操作失败"),
                                   parent=self.root)
            self._action_failed("巡线", result.get("err", "操作失败"))
            return
        if on:
            self._log("已启动一键巡线（%s）" % self.line_color_var.get(), "ok")
            self.line_state_label.config(text="巡线运行中…", fg=OK)
        else:
            self._log("已停止巡线", "info")
            self.line_state_label.config(text="巡线未运行", fg=MUTE)

    def switch_camsource(self, src):
        if self.link is None:
            return
        names = {"astra": "Astra RGB", "usb": "USB 广角", "both": "双开"}
        if not messagebox.askyesno(
                "切换相机源",
                "切换到「%s」需要重启小车相机，约 10~15 秒，期间画面会中断。\n继续？"
                % names.get(src, src), parent=self.root):
            return
        self._cam_busy = True
        self.camsrc_label.config(text="切换中：%s（约 15 秒）" % names.get(src, src),
                                 fg=WARN)
        self._log("切换车载相机源：%s" % names.get(src, src), "warn")
        self._io(lambda: self.link.set_camsource(src),
                 ok=lambda res: self._after_camsource(src, res),
                 err=lambda exc: self._camsource_failed(exc))

    def _after_camsource(self, src, result):
        self._cam_busy = False
        detail = result.get("detail") or "已切换"
        self.camsrc_label.config(text=detail, fg=OK)
        self._log("相机源切换完成：%s" % detail, "ok")

    def _camsource_failed(self, exc):
        self._cam_busy = False
        self.camsrc_label.config(text="切换失败：%s" % exc, fg=ALARM)
        self._action_failed("切换相机源", exc)

    # ------------------------------------------------------------ 相机
    def _cam_source_changed(self):
        cam = self.cam_var.get()
        if self.cam_worker is not None:
            self.cam_worker.set_cam(cam)
            self._cam_seq = -1
            self.camera.set_text("正在切换画面来源：%s …" % cam)
        cfg = robot_link.load_config()
        cfg["cam"] = cam
        robot_link.save_config(cfg)

    def _fps_changed(self):
        try:
            fps = int(self.fps_var.get())
        except (TypeError, ValueError):
            fps = 4
        fps = min(20, max(1, fps))
        if self.cam_worker is not None:
            self.cam_worker.set_fps(fps)
        cfg = robot_link.load_config()
        cfg["fps"] = fps
        robot_link.save_config(cfg)

    def save_frame(self):
        jpeg = self.camera.jpeg()
        if not jpeg:
            messagebox.showinfo("保存画面", "当前没有画面可保存。", parent=self.root)
            return
        try:
            path = robot_camera.save_jpeg(jpeg, CAPTURE_DIR, self.cam_var.get())
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror("保存画面", str(exc), parent=self.root)
            return
        self._log("画面已保存：%s" % path, "ok")
        self.status_text.config(text="画面已保存：%s" % path, fg=OK)
        messagebox.showinfo("已保存", path, parent=self.root)

    def photo_to_car(self):
        if self.link is None:
            return
        note = simpledialog.askstring("拍照到小车", "照片备注（可空，如：教室前排）：",
                                      parent=self.root)
        if note is None:
            return
        cam = self.cam_var.get()
        if cam == "depth":
            cam = "usb"
        self._io(lambda: self.link.photo(cam, note.strip()),
                 ok=self._after_photo,
                 err=lambda exc: self._action_failed("拍照", exc))

    def _after_photo(self, result):
        name = result.get("name") or "照片"
        self._log("已拍照并保存到小车：%s" % name, "ok")
        self.status_text.config(text="已拍照到小车：%s" % name, fg=OK)
        self.refresh_photos(silent=True)

    def open_car_web(self):
        if self.link is not None:
            webbrowser.open(self.link.index_url())
        else:
            host = (self.ip_var.get() or "").strip()
            if host:
                webbrowser.open("http://%s/" % host)
            else:
                messagebox.showinfo("打开小车网页",
                                    "先输入小车 IP 或连接小车。", parent=self.root)

    # ------------------------------------------------------------ 参数
    def read_params(self, silent=False):
        if self.link is None:
            if not silent:
                messagebox.showinfo("巡线参数", "请先连接小车。", parent=self.root)
            return
        if not silent:
            self.params_status.config(text="正在读取…", fg=WARN)
        self._io(lambda: self.link.params(),
                 ok=lambda res: self._fill_params(res.get("params") or {}),
                 err=lambda exc: self._params_failed("读取参数", exc))

    def _fill_params(self, params):
        self._params_loaded = True
        for key, var in self._param_vars.items():
            if key not in params:
                continue
            value = params.get(key)
            if key == "line_color":
                var.set(dict(LINE_COLORS).get(value, "黑线"))
            else:
                var.set(("%.3f" % float(value)).rstrip("0").rstrip(".")
                        if isinstance(value, (int, float)) else str(value))
        self.params_status.config(text="已读取小车参数", fg=OK)

    def save_params(self):
        if self.link is None:
            messagebox.showinfo("巡线参数", "请先连接小车。", parent=self.root)
            return
        values = {}
        color_map = dict((label, key) for key, label in LINE_COLORS)
        try:
            for key, var in self._param_vars.items():
                text = var.get().strip()
                if key == "line_color":
                    values[key] = color_map.get(text, "black")
                else:
                    if text == "":
                        raise ValueError("「%s」不能为空" % key)
                    values[key] = float(text)
        except ValueError as exc:
            messagebox.showwarning("参数不合法", str(exc), parent=self.root)
            return
        self.params_status.config(text="正在保存…", fg=WARN)
        self._io(lambda: self.link.save_params(values),
                 ok=lambda res: self._after_save_params(res),
                 err=lambda exc: self._params_failed("保存参数", exc))

    def _after_save_params(self, result):
        if not result.get("ok", 1):
            self._params_failed("保存参数", result.get("err", "保存失败"))
            return
        self._fill_params(result.get("params") or {})
        self.params_status.config(text="已保存到小车（下次巡线生效）", fg=OK)
        self._log("巡线参数已保存到小车", "ok")
        self.status_text.config(text="巡线参数已保存到小车", fg=OK)

    def reset_params(self):
        if self.link is None:
            messagebox.showinfo("巡线参数", "请先连接小车。", parent=self.root)
            return
        if not messagebox.askyesno("恢复默认",
                                   "把巡线参数恢复为小车端默认值？",
                                   parent=self.root):
            return
        self.params_status.config(text="正在恢复默认…", fg=WARN)
        self._io(lambda: self.link.save_params(dict(robot_link.LINE_PARAMS)),
                 ok=lambda res: self._after_save_params(res),
                 err=lambda exc: self._params_failed("恢复默认", exc))

    def _params_failed(self, what, exc):
        self.params_status.config(text="%s失败：%s" % (what, exc), fg=ALARM)
        self._log("%s失败：%s" % (what, exc), "err")

    # ------------------------------------------------------------ 远端文件
    def refresh_remote(self, silent=False):
        if self.link is None:
            if not silent:
                messagebox.showinfo("远端文件", "请先连接小车。", parent=self.root)
            return
        if not silent:
            self.files_status.config(text="正在读取…", fg=WARN)
        self._io(lambda: self.link.files(),
                 ok=self._fill_remote_files,
                 err=lambda exc: self._remote_failed("读取文件列表", exc))

    def _fill_remote_files(self, result):
        files = result.get("files") or []
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self._file_items = {}
        for item in files:
            rel = item.get("rel") or ""
            iid = self.file_tree.insert("", "end", values=(
                rel, human_size(item.get("size", 0)),
                item.get("mtime", "")))
            self._file_items[iid] = item
        root = result.get("root") or "小车目录"
        self.files_status.config(text="%d 个文件 · 只读预览" % len(files),
                                 fg=DIM)
        self._log("已读取小车文件列表：%d 个（%s）" % (len(files), root), "info")

    def _file_selected(self, _event=None):
        sel = self.file_tree.selection()
        if not sel or self.link is None:
            return
        item = self._file_items.get(sel[0]) or {}
        rel = item.get("rel")
        if not rel:
            return
        self._file_pending = rel
        self.file_title.config(text="正在读取 %s …" % rel, fg=WARN)
        self._io(lambda: self.link.file(rel),
                 ok=lambda res, rel=rel: self._show_remote_file(res, rel),
                 err=lambda exc: self._remote_failed("读取文件", exc))

    def _show_remote_file(self, result, rel=None):
        if rel is not None and rel != self._file_pending:
            return
        rel = result.get("path") or rel or ""
        code = result.get("code") or ""
        self.file_title.config(text="%s（只读预览）" % rel, fg=DIM)
        self.file_text.configure(state="normal")
        self.file_text.delete("1.0", "end")
        self.file_text.insert("1.0", code)
        self.file_text.configure(state="disabled")
        self._file_text_value = code
        self._file_title_value = rel

    def save_remote_file(self):
        code = getattr(self, "_file_text_value", "")
        if not code:
            messagebox.showinfo("保存文件", "先选择一个文件。", parent=self.root)
            return
        name = os.path.basename(getattr(self, "_file_title_value", "remote.txt"))
        path = filedialog.asksaveasfilename(
            parent=self.root, title="保存到本机", initialfile=name,
            defaultextension=os.path.splitext(name)[1],
            filetypes=[("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(code)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._log("远端文件已保存到本机：%s" % path, "ok")
        self.status_text.config(text="已保存：%s" % path, fg=OK)

    def refresh_photos(self, silent=False):
        if self.link is None:
            if not silent:
                messagebox.showinfo("巡更照片", "请先连接小车。", parent=self.root)
            return
        self._io(lambda: self.link.photos(),
                 ok=self._fill_photos,
                 err=lambda exc: self._remote_failed("读取照片列表", exc))

    def _fill_photos(self, result):
        photos = result.get("photos") or []
        for item in self.photo_tree.get_children():
            self.photo_tree.delete(item)
        self._photo_items = {}
        for item in photos:
            iid = self.photo_tree.insert("", "end", values=(
                item.get("name", ""), human_size(item.get("size", 0))))
            self._photo_items[iid] = item
        self.photo_status.config(text="%d 张照片" % len(photos), fg=DIM)

    def _photo_selected(self, _event=None):
        sel = self.photo_tree.selection()
        if not sel or self.link is None:
            return
        item = self._photo_items.get(sel[0]) or {}
        name = item.get("name")
        if not name:
            return
        self._photo_pending = name
        self.photo_view.set_text("正在读取 %s …" % name)
        self._io(lambda: self.link.photo_bytes(name),
                 ok=lambda data, name=name: self._show_photo(data, name),
                 err=lambda exc: self._remote_failed("读取照片", exc))

    def _show_photo(self, data, name=None):
        if name is not None and name != self._photo_pending:
            return
        self.photo_view.set_jpeg(data)

    def save_remote_photo(self):
        sel = self.photo_tree.selection()
        if not sel:
            messagebox.showinfo("保存照片", "先选中一张照片。", parent=self.root)
            return
        item = self._photo_items.get(sel[0]) or {}
        name = item.get("name")
        if not name or self.link is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="保存照片到本机", initialfile=name,
            defaultextension=".jpg", filetypes=[("JPEG 图片", "*.jpg")])
        if not path:
            return
        try:
            data = self.link.photo_bytes(name)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._log("照片已保存到本机：%s" % path, "ok")
        self.status_text.config(text="照片已保存：%s" % path, fg=OK)

    def _remote_failed(self, what, exc):
        self.files_status.config(text="%s失败：%s" % (what, exc), fg=ALARM)
        self._log("%s失败：%s" % (what, exc), "err")

    def show_line_log(self):
        if self.link is None:
            messagebox.showinfo("巡线日志", "请先连接小车。", parent=self.root)
            return
        self._io(lambda: self.link.line_log(), ok=self._show_log_window,
                 err=lambda exc: self._action_failed("读取巡线日志", exc))

    def _show_log_window(self, result):
        win = tk.Toplevel(self.root)
        win.title("小车巡线日志")
        win.configure(bg=BG)
        win.geometry("760x480")
        win.transient(self.root)
        text = tk.Text(win, bg="#050a14", fg=FG, relief="flat", bd=0,
                       highlightthickness=0, font=("Consolas", 9),
                       padx=10, pady=8)
        text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        sb.pack(side="right", fill="y")
        text.configure(yscrollcommand=sb.set)
        text.insert("1.0", result.get("log") or "（暂无日志）")
        text.configure(state="disabled")

    # ------------------------------------------------------------ 本地资料
    def _load_library(self):
        if not os.path.isdir(ROOT):
            return
        skip = {"__pycache__", ".git", "05_历史存档"}
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            rel = os.path.relpath(dirpath, ROOT)
            if rel == ".":
                parent = ""
            else:
                parent_rel = os.path.dirname(rel)
                parent = ("d:" + parent_rel) if parent_rel else ""
            if rel != ".":
                iid = "d:" + rel
                self.lib_tree.insert(parent, "end", iid=iid,
                                     text=os.path.basename(rel),
                                     values=("目录", "-"), open=False)
                self._lib_paths[iid] = dirpath
            for name in sorted(filenames):
                if not name.lower().endswith(LOCAL_EXTS):
                    continue
                full = os.path.join(dirpath, name)
                iid = "f:" + os.path.relpath(full, ROOT)
                try:
                    size = human_size(os.path.getsize(full))
                except OSError:
                    size = "-"
                kind = os.path.splitext(name)[1].lstrip(".").upper() or "FILE"
                self.lib_tree.insert(parent, "end", iid=iid, text=name,
                                     values=(kind, size))
                self._lib_paths[iid] = full

    def _local_selected(self):
        sel = self.lib_tree.selection()
        if not sel:
            return ""
        return self._lib_paths.get(sel[0], "")

    def open_local(self):
        path = self._local_selected()
        if not path:
            messagebox.showinfo("本地资料", "先选中一个文件或目录。", parent=self.root)
            return
        try:
            os.startfile(path)                               # type: ignore[attr-defined]
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror("打开失败", "%s\n\n%s" % (path, exc),
                                 parent=self.root)

    def open_local_dir(self):
        path = self._local_selected()
        if not path:
            return
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            os.startfile(folder)                             # type: ignore[attr-defined]
        except Exception as exc:                             # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def copy_local_path(self):
        path = self._local_selected()
        if not path:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        self.status_text.config(text="已复制路径：%s" % path, fg=OK)

    def open_guide(self):
        for path in (GUIDE_FILE, os.path.join(HERE, "巡考机器人.md")):
            if os.path.isfile(path):
                os.startfile(path)                           # type: ignore[attr-defined]
                return
        messagebox.showinfo("说明", "未找到说明文档。", parent=self.root)

    def export_deploy(self):
        if not os.path.isdir(CODE_ROOT):
            messagebox.showerror("导出部署包",
                                 "找不到代码目录：\n%s" % CODE_ROOT,
                                 parent=self.root)
            return
        os.makedirs(EXPORT_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = os.path.join(EXPORT_DIR, "巡考机器人_部署包_%s.zip" % stamp)

        def work():
            count = 0
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, dirnames, filenames in os.walk(CODE_ROOT):
                    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                    for name in filenames:
                        if name.endswith((".pyc", ".pyo")):
                            continue
                        full = os.path.join(dirpath, name)
                        rel = os.path.relpath(full, CODE_ROOT)
                        zf.write(full, os.path.join("patrol_robot", rel))
                        count += 1
            return count, target

        self._io(work, ok=lambda res: self._after_export(res),
                 err=lambda exc: self._action_failed("导出部署包", exc))
        self.status_text.config(text="正在打包小车部署文件…", fg=WARN)

    def _after_export(self, result):
        count, path = result
        self._log("已导出小车部署包：%s（%d 个文件）" % (path, count), "ok")
        messagebox.showinfo(
            "导出完成",
            "已导出 %d 个文件：\n%s\n\n把这个 zip 拷到小车，解压到 ~/patrol_robot "
            "即可（覆盖前先备份）。" % (count, path), parent=self.root)

    # ------------------------------------------------------------ 异步/日志
    def _io(self, fn, ok=None, err=None):
        def work():
            try:
                result = fn()
            except Exception as exc:                         # noqa: BLE001
                self._ui_q.put(("io_err", (str(exc), err, exc)))
            else:
                self._ui_q.put(("io_ok", (result, ok)))

        threading.Thread(target=work, daemon=True).start()

    def _action_failed(self, what, exc):
        msg = str(exc)
        self._log("%s失败：%s" % (what, msg), "err")
        self.status_text.config(text="%s失败：%s" % (what, msg), fg=ALARM)

    def _drain_queue(self):
        for _ in range(60):
            try:
                kind, payload = self._ui_q.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "connect_ok":
                    target, state, latency = payload
                    self._connect_ok(target, state, latency)
                elif kind == "connect_fail":
                    target, message = payload
                    self._connect_fail(target, message)
                elif kind == "io_ok":
                    result, callback = payload
                    if callback is not None:
                        callback(result)
                elif kind == "io_err":
                    message, callback, exc = payload
                    if callback is not None:
                        callback(message)
                    else:
                        self._action_failed("后台操作", message)
            except Exception as exc:                         # noqa: BLE001
                self._log("界面回调异常：%s" % exc, "err")

    def _log(self, text, level="info"):
        stamp = time.strftime("%H:%M:%S")
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", "[%s] %s\n" % (stamp, text), level)
            lines = int(self.log_box.index("end-1c").split(".")[0])
            if lines > 400:
                self.log_box.delete("1.0", "%d.0" % (lines - 300))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:                                    # noqa: BLE001
            pass

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def export_log(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出运行日志", defaultextension=".txt",
            initialfile="巡考机器人日志_%s.txt" % time.strftime("%Y%m%d_%H%M%S"),
            filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_box.get("1.0", "end"))
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status_text.config(text="日志已导出：%s" % path, fg=OK)

    # ------------------------------------------------------------ 轮询刷新
    def _tick(self):
        if self._closing:
            return
        self._drain_queue()
        self._update_state()
        self._update_camera()
        self.root.after(200, self._tick)

    def _update_state(self):
        if self.poller is None:
            return
        info = self.poller.read()
        state = info.get("state")
        if info.get("ok") and state:
            self._state_ok = True
            self._last_state = state
            if self._link_down_logged:
                self._link_down_logged = False
                self._log("链路已恢复", "ok")
                self.chip.set("● 已连接 %d ms" % info.get("latency", 0), OK)
            self._apply_state(state)
            self._apply_sender_error()
        else:
            self._state_ok = False
            if not self._cam_busy:
                self.chip.set("● 重连中…", WARN)
            if not self._link_down_logged and info.get("fails", 0) >= 2 \
                    and not self._cam_busy:
                self._link_down_logged = True
                self._log("链路中断：%s" % info.get("error", "未知原因"), "warn")
                self.status_text.config(
                    text="链路中断，正在自动重试…", fg=WARN)
            self._apply_sender_error()

    def _apply_sender_error(self):
        if self.sender is None:
            return
        error, when = self.sender.last_error()
        if error and time.time() - when < 3:
            self.status_text.config(text="指令发送失败：%s" % error, fg=ALARM)

    def _apply_state(self, state):
        estop = bool(state.get("estop"))
        line = state.get("line") or {}
        running = bool(line.get("running"))
        limits = state.get("limits") or {}

        self.v_link.config(text="%s" % (self.link.target if self.link else "—"),
                           fg=FG)
        latency = 0
        if self.poller is not None:
            latency = self.poller.read().get("latency", 0)
        self.v_latency.config(text="%.0f ms · %s" % (
            latency, _fmt_uptime(state.get("uptime"))), fg=FG)
        safe = "急停锁定" if estop else (state.get("blocked") or "正常")
        self.v_safe.config(text=safe, fg=ALARM if (estop or state.get("blocked"))
                           else OK)
        self.v_mode.config(
            text="本机演示（模拟）" if self._demo
            else ("DRY-RUN（不动车）" if state.get("dry_run") else "实车"),
            fg=ACCENT if self._demo else (WARN if state.get("dry_run") else FG))

        battery = state.get("battery")
        if battery is None:
            self.v_battery.config(text="— 无电压数据", fg=DIM)
            self.battery_bar.set(0, MUTE)
        else:
            voltage = float(battery)
            pct = max(0.0, min(1.0, (voltage - 21.6) / (25.6 - 21.6)))
            color = OK if voltage >= 24.5 else WARN if voltage >= 24.0 else ALARM
            self.v_battery.config(
                text="%.2f V · %s" % (voltage,
                                      "电量充足" if voltage >= 24.5
                                      else "注意：低于 24 V 扭矩不足"
                                      if voltage >= 24.0 else "电压偏低，建议回充"),
                fg=color)
            self.battery_bar.set(pct, color)

        odom = state.get("odom") or {}
        imu = state.get("imu") or {}
        if odom:
            self.v_odom.config(text="x %+.2f m   y %+.2f m"
                                    % (odom.get("x", 0), odom.get("y", 0)))
        else:
            self.v_odom.config(text="—（无里程计）", fg=DIM)
        self.v_yaw.config(text=_fmt(odom.get("yaw"), 1, "°")
                          if odom else "—")
        if imu:
            self.v_tilt.config(text="pitch %+.1f°   roll %+.1f°"
                                    % (imu.get("pitch", 0), imu.get("roll", 0)))
        else:
            self.v_tilt.config(text="—（无 IMU）", fg=DIM)

        clear = state.get("clear") or {}
        limit = limits.get("stop_distance", 0.35)
        self.radar.update_data(clear, limit)
        self.v_limit.config(
            text="限速 %.2f m/s / %.2f rad/s · 避障 %.2f m · 倾角 %.1f°"
                 % (limits.get("max_linear", 0.2),
                    limits.get("max_angular", 0.6), limit,
                    limits.get("max_tilt", 3.0)))

        cmd = state.get("cmd") or {}
        self.v_cmd.config(text="v %+.2f m/s   w %+.2f rad/s%s"
                               % (cmd.get("v", 0), cmd.get("w", 0),
                                  "  (已过期)" if cmd.get("age", 0) > 0.6 else ""),
                          fg=DIM if cmd.get("age", 0) > 0.6 else FG)

        if running:
            status = line.get("status") or {}
            state_name = status.get("state") or "…"
            corners = status.get("corners", 0)
            err = status.get("err")
            self.v_line.config(
                text="运行中 · %s · 弯 %s · err %s"
                     % (state_name, corners, _fmt(err, 2)), fg=OK)
            self.line_state_label.config(
                text="巡线运行中 · %s · 已过 %s 个弯" % (state_name, corners),
                fg=OK)
        else:
            self.v_line.config(text="未运行", fg=MUTE)
            self.line_state_label.config(text="巡线未运行", fg=MUTE)

        estop_text = "解除急停" if estop else "⛔ 急停 E-STOP"
        self.btn_estop.config(text=estop_text,
                              bg="#b91c1c" if estop else "#7f1d1d")
        try:
            self.btn_line_start.config(state="disabled" if (estop or running)
                                       else "normal")
            self.btn_line_stop.config(state="normal" if running else "disabled")
        except Exception:                                    # noqa: BLE001
            pass
        enabled = (not estop) and (not running)
        self.pad.set_enabled(enabled)
        if estop:
            self._pad_dirs.clear()
        if running and self._pad_dirs:
            self._pad_dirs.clear()
            if self.sender is not None:
                self.sender.set(0.0, 0.0)

        if self._demo:
            self.chip.set("● 演示模式 %d ms" % latency, ACCENT)
        elif self._state_ok:
            self.chip.set("● 已连接 %d ms" % latency, OK)

    def _update_camera(self):
        if self.cam_worker is None:
            return
        info = self.cam_worker.read()
        if info["seq"] != self._cam_seq:
            self._cam_seq = info["seq"]
            if info["jpeg"]:
                self.camera.set_jpeg(info["jpeg"])
                self._cam_frames += 1
                size = robot_camera.image_size(info["jpeg"])
                self.cam_source_label.config(
                    text="%s · %.1f fps"
                         % (robot_camera.CAMERAS.get(info["cam"], info["cam"]),
                            self._cam_fps), fg=DIM)
                self.cam_info.config(
                    text="%s · %s · %s"
                         % (time.strftime("%H:%M:%S"),
                            ("%d×%d" % size) if size else "JPEG",
                            human_size(len(info["jpeg"]))), fg=MUTE)
            elif info["error"]:
                self.camera.set_text("相机暂无画面\n%s\n\n"
                                     "检查小车端 snapshot_server.py 与相机源"
                                     % info["error"])
                self.cam_info.config(text="无画面：%s" % info["error"], fg=WARN)
                self.cam_source_label.config(text="无画面", fg=WARN)
        now = time.time()
        if now - self._cam_fps_ts >= 1.0:
            self._cam_fps = self._cam_frames / (now - self._cam_fps_ts)
            self._cam_frames = 0
            self._cam_fps_ts = now

    # ------------------------------------------------------------ 关闭
    def close(self):
        if self._closing:
            return
        self._closing = True
        self._pad_dirs.clear()
        link = self.link
        self.link = None
        for worker in (self.poller, self.sender, self.cam_worker):
            if worker is not None:
                try:
                    worker.stop()
                except Exception:                            # noqa: BLE001
                    pass
        if link is not None:
            try:
                link.stop()
            except Exception:                                # noqa: BLE001
                pass
        if self.sim is not None:
            try:
                self.sim.stop()
            except Exception:                                # noqa: BLE001
                pass
        try:
            cfg = robot_link.load_config()
            cfg.update({"linear": round(float(self.linear_var.get()), 3),
                        "angular": round(float(self.angular_var.get()), 3),
                        "cam": self.cam_var.get(),
                        "fps": int(self.fps_var.get()),
                        "port": int(self.port_var.get() or 8080)})
            robot_link.save_config(cfg)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                                    # noqa: BLE001
            pass


def main():
    _ensure_stdio()
    root = tk.Tk()
    app = RobotApp(root)

    def report(exc_type, exc_value, exc_tb):
        import traceback
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "gui_error.log"), "a",
                      encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), detail))
        except OSError:
            pass
        if not app._closing:
            try:
                messagebox.showerror("界面异常", detail[-1200:], parent=root)
            except Exception:                                # noqa: BLE001
                pass

    root.report_callback_exception = report
    root.mainloop()


if __name__ == "__main__":
    main()
