# -*- coding: utf-8 -*-
r"""设备端"接收命令"的聊天窗口（01 / 02 共用）

为什么要有它：
  `command_runner.py` 是纯命令行常驻进程，值班的人看不到"刚才收到了什么命令、
  执行结果如何"。这个窗口把**同一套**拉取 / 白名单执行 / 回执逻辑做成聊天式界面：

    · 后台线程轮询 03：GET /api/command?target=<本机标识>&ip=<本机IPv4,…>
    · 收到命令 → 白名单执行（复用 command_runner.execute）→ POST /api/command_result
    · 每条消息按"收到 / 执行 / 回执 / 系统 / 错误"分色显示，可清空、可暂停
    · 界面只负责展示，**执行仍在白名单内**，没有任意命令通道

用法：
  在 01 / 02 的图形界面里点「📡 接收命令」即可打开（会带上它们自己的配置）；
  也可以在命令行直接开：
      python 00_集成联动\cmd_chat.py --module 01
      python 00_集成联动\cmd_chat.py --module 02 --url http://192.168.1.10:9113 --token xxx
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import command_runner as CR          # noqa: E402  同目录，复用它的白名单执行与 HTTP

# ---------------------------------------------------------------- 配色（与 03 驾驶舱一致）
BG = "#060c18"
BG2 = "#0b1526"
PANEL = "#0d1a2e"
PANEL2 = "#122542"
BORDER = "#1b3457"
FG = "#e8f1ff"
FG_DIM = "#8ba3c7"
FG_MUTE = "#5b739a"
ACCENT = "#22d3ee"
OK = "#34d399"
WARN = "#fbbf24"
ALARM = "#f43f5e"
ACCENT3 = "#a78bfa"

FONT_UI = ("Microsoft YaHei", 10)
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_MONO = ("Consolas", 10)


def _apply_style(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                 font=FONT_UI, bordercolor=BORDER, lightcolor=PANEL,
                 darkcolor=PANEL)
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
    st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                 padding=(10, 5))
    st.map("TButton", background=[("active", BORDER)], foreground=[("disabled", FG_MUTE)])
    st.configure("Go.TButton", background=ACCENT3, foreground="#ffffff")
    st.map("Go.TButton", background=[("active", ACCENT)])
    st.configure("Stop.TButton", background="#5a1529", foreground="#ffe4e9")
    st.map("Stop.TButton", background=[("active", ALARM)])
    st.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                 insertcolor=ACCENT, bordercolor=BORDER)
    st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                 foreground=FG, arrowcolor=ACCENT, bordercolor=BORDER)
    st.map("TCombobox",
           fieldbackground=[("readonly", PANEL2)],
           foreground=[("readonly", FG)])
    st.configure("TLabelframe", background=BG, foreground=ACCENT,
                 bordercolor=BORDER, relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
    st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG2,
                 bordercolor=BG2, arrowcolor=FG_DIM)
    try:
        root.configure(bg=BG)
    except Exception:
        pass
    return st


class CommandChatClient(object):
    """轮询 / 执行 / 回执（不碰界面，线程安全地回调 on_message）。"""

    #: 消息类型：(标签文字, 颜色)
    KINDS = {"sys": ("系统", FG_MUTE), "recv": ("收到", ACCENT),
             "exec": ("执行", ACCENT3), "sent": ("回执", OK),
             "err": ("错误", ALARM), "info": ("提示", WARN)}

    def __init__(self, module, url="", token="", device_id="", interval=8,
                 on_message=None):
        self.module = module
        self.url = (url or CR._default_url() or "").rstrip("/")
        self.token = token or CR._default_token() or ""
        self.device_id = CR.device_id(device_id or "")
        self.interval = max(3, int(interval or 8))
        self.on_message = on_message
        self.ips = []
        self._stop = threading.Event()
        self._thread = None
        self._seen = set()
        self.last_ok = None
        self.count = 0

    # ---- 状态 ----
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def say(self, kind, text):
        if self.on_message:
            try:
                self.on_message(kind, text)
            except Exception:
                pass

    def describe(self):
        return ("本机标识 %s ｜ IPv4 %s ｜ 终端 %s"
                % (self.device_id, ", ".join(self.ips) or "-", self.url or "未配置"))

    # ---- 单次轮询 ----
    def poll_once(self):
        if not self.url:
            return False, "未配置智能终端地址（EXAM_TERMINAL_URL 或界面里填）"
        if not self.token:
            return False, "未配置接入令牌（EXAM_TERMINAL_TOKEN 或界面里填）"
        if not self.ips:
            self.ips = CR.local_ipv4s()
        ok, r = CR.fetch(self.url, self.token, self.device_id, ips=self.ips)
        if not ok:
            return False, "拉取命令失败：%s" % (r.get("reason") or r)
        cmds = r.get("commands") or []
        for c in cmds:
            cid, kind = c.get("cmd_id", ""), c.get("kind", "")
            if not cid or cid in self._seen:
                continue
            self._seen.add(cid)
            self.count += 1
            self.say("recv", "收到命令 %s（%s）%s"
                     % (cid, kind,
                        ("　参数 " + str(c.get("params"))) if c.get("params") else ""))
            try:
                cok, res = CR.execute(self.module, c)
            except Exception as e:                      # noqa: BLE001
                cok, res = False, {"reason": "执行异常：%s" % e}
            self.say("exec", "执行 %s → %s" % (kind, "成功" if cok else "失败"))
            pok, pr = CR.post_result(self.url, self.token, cid, cok, res,
                                     self.device_id)
            if pok:
                self.say("sent", "回执已送达：%s %s" % (cid, "成功" if cok else "失败"))
            else:
                self.say("err", "回执发送失败：%s" % (pr.get("reason") or pr))
        return True, "本轮 %d 条" % len(cmds)

    # ---- 后台线程 ----
    def _loop(self):
        self.say("sys", "开始接收命令（每 %d 秒轮询）" % self.interval)
        self.ips = CR.local_ipv4s()
        self.say("sys", self.describe())
        while not self._stop.is_set():
            ok, msg = self.poll_once()
            self.last_ok = ok
            if not ok:
                self.say("err", msg)
            self._stop.wait(self.interval)
        self.say("sys", "已停止接收")

    def start(self):
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cmd-chat",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._thread = None


class ChatWindow(object):
    """聊天式命令接收窗口（Toplevel，深色，与 03 驾驶舱同色系）。"""

    def __init__(self, parent, module, url="", token="", device_id="",
                 interval=8):
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title("📡 接收命令 · 模块 %s（来自智能终端）" % module)
        self.win.geometry("820x620")
        self.win.minsize(680, 480)
        _apply_style(self.win)
        self.client = CommandChatClient(module, url=url, token=token,
                                        device_id=device_id, interval=interval,
                                        on_message=self._on_message)
        self._closed = False
        # 后台线程不能直接碰 Tk（跨线程调用 after/create 会抛异常，且被 except 吞掉后
        # 界面就"永远收不到消息"）。这里改成：线程只往队列里塞，主线程定时取。
        self._q = queue.Queue()
        self._build()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._drain()

    # ---- 界面 ----
    def _build(self):
        head = ttk.Frame(self.win, padding=(12, 10, 12, 4))
        head.pack(fill="x")
        ttk.Label(head, text="📡 接收命令（%s）" % self.client.module,
                  font=("Microsoft YaHei", 13, "bold")).pack(side="left")
        self.lbl_state = ttk.Label(head, text="● 未开始", foreground=FG_MUTE,
                                   font=FONT_SMALL)
        self.lbl_state.pack(side="right")

        cfg = ttk.LabelFrame(self.win, text=" 连接设置 ", padding=8)
        cfg.pack(fill="x", padx=12, pady=4)
        row = ttk.Frame(cfg)
        row.pack(fill="x")
        ttk.Label(row, text="终端地址：").pack(side="left")
        self.url_var = tk.StringVar(value=self.client.url)
        ttk.Entry(row, textvariable=self.url_var, width=30).pack(side="left", padx=(0, 8))
        ttk.Label(row, text="令牌：").pack(side="left")
        self.token_var = tk.StringVar(value=self.client.token)
        ttk.Entry(row, textvariable=self.token_var, width=20, show="●").pack(side="left")
        row2 = ttk.Frame(cfg)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="本机标识：").pack(side="left")
        self.dev_var = tk.StringVar(value=self.client.device_id)
        ttk.Entry(row2, textvariable=self.dev_var, width=18).pack(side="left", padx=(0, 8))
        ttk.Label(row2, text="轮询间隔(秒)：").pack(side="left")
        self.itv_var = tk.StringVar(value=str(self.client.interval))
        ttk.Entry(row2, textvariable=self.itv_var, width=5).pack(side="left", padx=(0, 8))
        ttk.Label(row2, text=self.client.describe(), foreground=FG_MUTE,
                  font=FONT_SMALL).pack(side="left", padx=(6, 0))

        bar = ttk.Frame(self.win, padding=(12, 6))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="▶ 开始接收", style="Go.TButton",
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■ 停止", style="Stop.TButton",
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="🧹 清空记录", command=self._clear).pack(side="left", padx=6)
        ttk.Label(bar, text="命令只在白名单内执行（ping/status/logs_tail/selftest/rebuild_index）",
                  foreground=FG_MUTE, font=FONT_SMALL).pack(side="left", padx=(10, 0))

        box = ttk.Frame(self.win, padding=(12, 2, 12, 10))
        box.pack(fill="both", expand=True)
        self.txt = tk.Text(box, wrap="word", font=FONT_MONO, bg=PANEL, fg=FG,
                           insertbackground=ACCENT, relief="flat",
                           highlightthickness=1, highlightbackground=BORDER,
                           padx=10, pady=8)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for kind, (label, color) in CommandChatClient.KINDS.items():
            self.txt.tag_configure(kind, foreground=color)
            self.txt.tag_configure(kind + "_who", foreground=color,
                                   font=("Microsoft YaHei", 9, "bold"))
        self.txt.tag_configure("ts", foreground=FG_MUTE, font=("Consolas", 9))
        self.txt.configure(state="disabled")
        self._say("sys", "窗口已就绪：填好终端地址与令牌后点「▶ 开始接收」。")
        self._say("sys", self.client.describe())

    # ---- 消息 ----
    def _on_message(self, kind, text):
        """后台线程回调：只入队，绝不在这里动 Tk。"""
        self._q.put((kind, text))

    def _drain(self):
        """主线程定时把队列里的消息刷到聊天区（每 200ms）。"""
        if self._closed:
            return
        try:
            while True:
                kind, text = self._q.get_nowait()
                self._say(kind, text)
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.win.after(200, self._drain)
        except Exception:
            pass

    def _say(self, kind, text):
        if self._closed:
            return
        label, _color = CommandChatClient.KINDS.get(kind, ("信息", FG))
        try:
            self.txt.configure(state="normal")
            self.txt.insert("end", time.strftime("[%H:%M:%S] "), "ts")
            self.txt.insert("end", "[%s] " % label, kind + "_who")
            self.txt.insert("end", str(text) + "\n", kind)
            self.txt.see("end")
            self.txt.configure(state="disabled")
        except Exception:
            pass

    # ---- 按钮 ----
    def _start(self):
        self.client.url = (self.url_var.get() or "").strip().rstrip("/")
        self.client.token = (self.token_var.get() or "").strip()
        self.client.device_id = CR.device_id((self.dev_var.get() or "").strip())
        try:
            self.client.interval = max(3, int(self.itv_var.get()))
        except (TypeError, ValueError):
            self.client.interval = 8
        self.itv_var.set(str(self.client.interval))
        if not self.client.url or not self.client.token:
            self._say("err", "终端地址与接入令牌都必须填（令牌在 03 的"
                             "「启动局域网接入服务.bat」里显示）")
            return
        self.client.start()
        self.lbl_state.config(text="● 接收中（每 %d 秒）" % self.client.interval,
                              foreground=OK)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def _stop(self):
        self.client.stop()
        self.lbl_state.config(text="● 已停止", foreground=WARN)
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _clear(self):
        try:
            self.txt.configure(state="normal")
            self.txt.delete("1.0", "end")
            self.txt.configure(state="disabled")
        except Exception:
            pass

    def close(self):
        self._closed = True
        try:
            self.client.stop()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


def open_chat_window(parent, module, url="", token="", device_id="", interval=8):
    """给 01/02 的 GUI 调：打开一个命令聊天窗口（返回 ChatWindow）。"""
    return ChatWindow(parent, module, url=url, token=token,
                      device_id=device_id, interval=interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="设备端命令聊天窗口（01/02 共用）")
    ap.add_argument("--module", required=True, choices=sorted(CR.MODULE_DIRS),
                    help="本机是哪个模块（01/02/…）")
    ap.add_argument("--url", default=CR._default_url(), help="智能终端地址")
    ap.add_argument("--token", default=CR._default_token(), help="接入令牌")
    ap.add_argument("--device-id", default="", help="本机标识（默认计算机名）")
    ap.add_argument("--interval", type=int, default=8, help="轮询间隔（秒）")
    ns = ap.parse_args(argv)
    root = tk.Tk()
    root.withdraw()                      # 只显示聊天窗口，不要多一个空主窗
    win = ChatWindow(root, ns.module, url=ns.url, token=ns.token,
                     device_id=ns.device_id, interval=ns.interval)
    win.win.protocol("WM_DELETE_WINDOW",
                     lambda: (win.close(), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
