# -*- coding: utf-8 -*-
r"""
12 告警上传与终端处置演示 · 桌面界面

两个入口：
  ① 本机上传：填一条告警 -> 12 用 03 的 integration_api.py 真正写入 03；
  ② 局域网接收：12 在本机开一个 HTTP 服务（默认 0.0.0.0:5030），
     其他电脑/手机浏览器打开 http://<本机IP>:5030/ 就能填表上传，
     上传结果与 03 最近告警在同一页/GUI 里可见。

说明：12 不复制 03 的处置逻辑；确认/复核仍在 03 智能终端里完成，
      本页只是“不同设备把告警送进 03”的入口和回显。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, urlparse

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

import upload_client

APP_TITLE = "12 告警上传与终端处置演示"
DEFAULT_PORT = 5030


def _python_exe():
    """优先用同目录 runtime\\python.exe（pythonw 下子进程没有可用 stdout）。"""
    exe = sys.executable or ""
    d = os.path.dirname(exe)
    cand = os.path.join(d, "python.exe")
    if os.path.isfile(cand):
        return cand
    return exe or "python"


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:                                        # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:                                    # noqa: BLE001
            return "127.0.0.1"


class UploadApp(object):
    def __init__(self, root):
        self.root = root
        self.link = upload_client.TerminalLink(python=_python_exe())
        self.server = None
        self.server_thread = None
        self._received = 0
        # HTTP/子进程线程绝不直接碰 Tk：统一投递到主线程队列
        self.ui_queue = queue.Queue()
        if ui_theme is not None:
            ui_theme.apply(root)
        self._build()
        self.root.after(150, self._poll_queue)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "lan":
                    ip, data, r = payload
                    self._lan_received(ip, data, r)
                elif kind == "upload":
                    p, r, from_lan, on_done = payload
                    self._upload_done(p, r, from_lan, on_done)
                elif kind == "upload_fail":
                    p, exc, from_lan, on_done = payload
                    self._upload_fail(p, exc, from_lan, on_done)
                elif kind == "alerts":
                    self._show_alerts(payload)
        except queue.Empty:
            pass
        except Exception:                                    # noqa: BLE001
            pass
        try:
            self.root.after(200, self._poll_queue)
        except Exception:                                    # noqa: BLE001
            pass

    # ------------------------------------------------------------ 界面
    def _build(self):
        self.root.title(APP_TITLE + " · 智能考场 AI 巡考系统")
        self.root.geometry("1180x780")
        self.root.minsize(980, 640)
        self.root.configure(bg=(ui_theme.BG if ui_theme else "#070d1a"))

        head = ttk.Frame(self.root, padding=(14, 10))
        head.pack(fill="x")
        ttk.Label(head, text="12 告警上传与终端处置",
                  font=("Microsoft YaHei", 16, "bold")).pack(side="left")
        ttk.Label(head, text="本机上传 / 多设备局域网 HTTP 上传 / 03 最近告警",
                  foreground=(ui_theme.DIM if ui_theme else "#8ba3c7")
                  ).pack(side="left", padx=(14, 0))
        self.status = ttk.Label(head, text="就绪",
                                foreground=(ui_theme.DIM if ui_theme else "#8ba3c7"))
        self.status.pack(side="right")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.tab_upload = ttk.Frame(nb, padding=10)
        self.tab_lan = ttk.Frame(nb, padding=10)
        self.tab_alerts = ttk.Frame(nb, padding=10)
        nb.add(self.tab_upload, text="  上传告警  ")
        nb.add(self.tab_lan, text="  局域网接收  ")
        nb.add(self.tab_alerts, text="  03 最近告警  ")
        self._build_upload()
        self._build_lan()
        self._build_alerts()

    def _log(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", line + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except Exception:                                    # noqa: BLE001
            pass

    def _build_upload(self):
        f = self.tab_upload
        box = ttk.LabelFrame(f, text="告警内容", padding=10)
        box.pack(fill="x")
        self.v_code = tk.StringVar(value="REPEATED_TOUCH_LINE")
        self.v_device = tk.StringVar(value="CAM-A-101-1")
        self.v_room = tk.StringVar(value="A-101")
        self.v_seat = tk.StringVar(value="")
        self.v_cand = tk.StringVar(value="")
        self.v_level = tk.StringVar(value="auto")
        self.v_cred = tk.StringVar(value="")
        self.v_oper = tk.StringVar(value="12/局域网")

        rows = [
            ("告警码*", self.v_code, 26),
            ("设备编号", self.v_device, 20),
            ("考场", self.v_room, 14),
            ("座位", self.v_seat, 10),
            ("考生编号", self.v_cand, 20),
            ("画质可信度（0~1，可空）", self.v_cred, 10),
            ("操作人", self.v_oper, 18),
        ]
        for i, (label, var, width) in enumerate(rows):
            ttk.Label(box, text=label + "：").grid(row=i, column=0, sticky="e", pady=3)
            ttk.Entry(box, textvariable=var, width=width).grid(
                row=i, column=1, sticky="w", pady=3)
        ttk.Label(box, text="级别：").grid(row=len(rows), column=0, sticky="e", pady=3)
        ttk.Combobox(box, textvariable=self.v_level, state="readonly", width=12,
                     values=["auto", "red", "orange"]).grid(
            row=len(rows), column=1, sticky="w", pady=3)
        ttk.Label(box, text="auto = 03 按告警码分级；red/orange 可强制指定",
                  foreground=(ui_theme.DIM if ui_theme else "#8ba3c7")
                  ).grid(row=len(rows), column=1, sticky="w", padx=(110, 0), pady=3)

        ttk.Label(f, text="详情").pack(anchor="w", pady=(10, 2))
        self.txt_detail = tk.Text(f, height=5, wrap="word")
        self.txt_detail.pack(fill="x")
        self.txt_detail.insert("1.0", "12 演示：不同设备通过局域网上传的告警，"
                                     "会写入 03 智能终端，待终端确认/复核。")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="📤 上传到 03", style="Accent.TButton",
                   command=self._upload_click).pack(side="left")
        ttk.Button(bar, text="🧹 清空详情/日志", command=self._clear).pack(
            side="left", padx=8)
        ttk.Label(bar, text="上传成功后可在「03 最近告警」里看到",
                  foreground=(ui_theme.DIM if ui_theme else "#8ba3c7")).pack(
            side="left", padx=10)

        self.txt_log = tk.Text(f, height=9, wrap="word", font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True, pady=(4, 0))
        self.txt_log.configure(state="disabled")

    def _build_lan(self):
        f = self.tab_lan
        box = ttk.LabelFrame(f, text="局域网接收服务（供其他电脑/手机上传）", padding=10)
        box.pack(fill="x")
        self.v_host = tk.StringVar(value="0.0.0.0")
        self.v_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.v_token = tk.StringVar(value="")
        ttk.Label(box, text="监听地址：").grid(row=0, column=0, sticky="e", pady=3)
        ttk.Entry(box, textvariable=self.v_host, width=16).grid(
            row=0, column=1, sticky="w", pady=3)
        ttk.Label(box, text="端口：").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ttk.Entry(box, textvariable=self.v_port, width=8).grid(
            row=0, column=3, sticky="w", pady=3)
        ttk.Label(box, text="访问令牌（可空）：").grid(
            row=0, column=4, sticky="e", padx=(12, 0))
        ttk.Entry(box, textvariable=self.v_token, width=18).grid(
            row=0, column=5, sticky="w", pady=3)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=8)
        self.btn_lan = ttk.Button(bar, text="▶ 启动接收", style="Accent.TButton",
                                  command=self._toggle_lan)
        self.btn_lan.pack(side="left")
        self.lan_url = ttk.Label(bar, text="未启动", foreground=(
            ui_theme.DIM if ui_theme else "#8ba3c7"))
        self.lan_url.pack(side="left", padx=12)
        ttk.Label(f, text="其他设备在同一局域网内打开上面的地址，填表即可上传；"
                          "手机浏览器也可以。Windows 防火墙需放行该端口。",
                  foreground=(ui_theme.DIM if ui_theme else "#8ba3c7")
                  ).pack(anchor="w", pady=(0, 6))

        cols = ("time", "from", "code", "room", "result")
        self.lan_tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("time", "时间", 70), ("from", "来源", 110),
                        ("code", "告警码", 180), ("room", "考场", 80),
                        ("result", "结果", 300)):
            self.lan_tree.heading(c, text=t)
            self.lan_tree.column(c, width=w, anchor="w")
        self.lan_tree.pack(fill="both", expand=True)

    def _build_alerts(self):
        f = self.tab_alerts
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="🔄 刷新 03 告警", command=self.refresh_alerts).pack(
            side="left")
        ttk.Label(bar, text="只读：确认/复核请到 03 智能终端「告警处置」",
                  foreground=(ui_theme.DIM if ui_theme else "#8ba3c7")).pack(
            side="left", padx=10)
        cols = ("id", "level", "time", "code", "room", "device", "status")
        self.alert_tree = ttk.Treeview(f, columns=cols, show="headings", height=15)
        for c, t, w in (("id", "编号", 90), ("level", "级别", 70), ("time", "时间", 150),
                        ("code", "告警码", 190), ("room", "考场", 80),
                        ("device", "设备", 140), ("status", "状态", 90)):
            self.alert_tree.heading(c, text=t)
            self.alert_tree.column(c, width=w, anchor="w")
        self.alert_tree.pack(fill="both", expand=True)
        self.alert_detail = tk.Text(f, height=4, wrap="word", font=("Consolas", 9))
        self.alert_detail.pack(fill="x", pady=(6, 0))
        self.alert_tree.bind("<<TreeviewSelect>>", lambda _e: self._show_alert())

    # ------------------------------------------------------------ 上传
    def _payload(self):
        p = {
            "code": self.v_code.get().strip(),
            "device_id": self.v_device.get().strip(),
            "room_id": self.v_room.get().strip(),
            "seat_no": self.v_seat.get().strip(),
            "candidate_id": self.v_cand.get().strip(),
            "detail": self.txt_detail.get("1.0", "end").strip(),
            "operator": self.v_oper.get().strip() or "12/局域网",
            "source": "12_告警上传与终端处置演示",
        }
        level = self.v_level.get().strip()
        if level and level != "auto":
            p["level"] = level
        cred = self.v_cred.get().strip()
        if cred:
            try:
                p["credibility"] = float(cred)
            except ValueError:
                raise ValueError("画质可信度必须是 0~1 的数字")
        if not p["code"]:
            raise ValueError("告警码必填")
        return p

    def _upload_click(self):
        try:
            p = self._payload()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self.root)
            return
        self._upload_async(p, from_lan=False)

    def _upload_async(self, p, from_lan=False, on_done=None):
        def work():
            try:
                r = self.link.upload_alert(
                    code=p.get("code"), device_id=p.get("device_id", ""),
                    room_id=p.get("room_id", ""), seat_no=p.get("seat_no", ""),
                    detail=p.get("detail", ""), candidate_id=p.get("candidate_id", ""),
                    candidate_name=p.get("candidate_name", ""),
                    credibility=p.get("credibility"), level=p.get("level"),
                    operator=p.get("operator") or "12/局域网",
                    source=p.get("source") or "12_告警上传与终端处置演示")
                self.ui_queue.put(("upload", (p, r, from_lan, on_done)))
            except Exception as exc:                          # noqa: BLE001
                self.ui_queue.put(("upload_fail", (p, exc, from_lan, on_done)))
        threading.Thread(target=work, daemon=True).start()

    def _upload_done(self, p, r, from_lan, on_done):
        ok = bool(r.get("ok", True)) and not r.get("_error")
        text = ("%s 上传 %s：%s；告警 %s；返回 %s"
                % ("局域网" if from_lan else "本机", "成功" if ok else "失败",
                   p.get("code"), r.get("alert_id") or "-",
                   json.dumps(r, ensure_ascii=False)[:220]))
        self._log(text)
        self.status.config(text=("上传成功：" + str(r.get("alert_id") or "")) if ok
                           else ("上传失败：" + str(r.get("_error") or r.get("reason") or "")))
        if on_done:
            on_done(r)
        if ok:
            self.refresh_alerts()

    def _upload_fail(self, p, exc, from_lan, on_done):
        msg = "%s 上传 %s 失败：%s" % ("局域网" if from_lan else "本机",
                                     p.get("code"), exc)
        self._log(msg)
        self.status.config(text=msg)
        if on_done:
            on_done({"ok": False, "reason": str(exc)})

    def _clear(self):
        self.txt_detail.delete("1.0", "end")
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------ 局域网
    def _toggle_lan(self):
        if self.server is not None:
            self._stop_lan()
            return
        try:
            host = self.v_host.get().strip() or "0.0.0.0"
            port = int(self.v_port.get().strip() or DEFAULT_PORT)
            token = self.v_token.get().strip()
            app = self

            class Handler(BaseHTTPRequestHandler):
                server_version = "ExamAlert12/1.0"

                def log_message(self, fmt, *args):
                    app.ui_queue.put(("log", "HTTP %s" % (fmt % args)))

                def _send(self, code, body, ctype="application/json; charset=utf-8"):
                    if isinstance(body, str):
                        body = body.encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Headers",
                                     "Content-Type, X-Exam-Token")
                    self.send_header("Access-Control-Allow-Methods",
                                     "GET, POST, OPTIONS")
                    self.end_headers()
                    self.wfile.write(body)

                def _json(self, code, obj):
                    self._send(code, json.dumps(obj, ensure_ascii=False))

                def _token_ok(self, data=None):
                    if not token:
                        return True
                    got = self.headers.get("X-Exam-Token", "")
                    if not got and data:
                        got = str(data.get("token") or "")
                    return got == token

                def do_OPTIONS(self):
                    self._send(204, b"")

                def do_GET(self):
                    path = urlparse(self.path).path
                    if path == "/":
                        self._send(200, app._html_page(bool(token)),
                                   "text/html; charset=utf-8")
                        return
                    if path == "/health":
                        self._json(200, {"ok": True, "service": "12-alert-lan",
                                         "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                        return
                    if path in ("/api/alerts", "/api/status"):
                        if not self._token_ok():
                            self._json(403, {"ok": False, "reason": "令牌错误"})
                            return
                        try:
                            if path == "/api/alerts":
                                q = parse_qs(urlparse(self.path).query)
                                limit = int((q.get("limit") or ["20"])[0])
                                self._json(200, app.link.list_alerts(limit=limit))
                            else:
                                self._json(200, app.link.status())
                        except Exception as exc:              # noqa: BLE001
                            self._json(500, {"ok": False, "reason": str(exc)})
                        return
                    self._json(404, {"ok": False, "reason": "not found"})

                def do_POST(self):
                    path = urlparse(self.path).path
                    if path not in ("/api/alert", "/api/ingest_alert"):
                        self._json(404, {"ok": False, "reason": "not found"})
                        return
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(n) if n else b"{}"
                        ctype = (self.headers.get("Content-Type") or "").lower()
                        if "application/json" in ctype:
                            data = json.loads(raw.decode("utf-8", "replace") or "{}")
                        else:
                            data = {k: v[0] for k, v in parse_qs(
                                raw.decode("utf-8", "replace")).items()}
                    except Exception as exc:                  # noqa: BLE001
                        self._json(400, {"ok": False, "reason": "请求解析失败：%s" % exc})
                        return
                    if not self._token_ok(data):
                        self._json(403, {"ok": False, "reason": "令牌错误"})
                        return
                    if not str(data.get("code") or "").strip():
                        self._json(400, {"ok": False, "reason": "code 必填"})
                        return
                    try:
                        cred = data.get("credibility")
                        cred = float(cred) if cred not in (None, "") else None
                    except (TypeError, ValueError):
                        cred = None
                    try:
                        r = app.link.upload_alert(
                            code=str(data.get("code")).strip(),
                            device_id=str(data.get("device_id") or ""),
                            room_id=str(data.get("room_id") or ""),
                            seat_no=str(data.get("seat_no") or ""),
                            detail=str(data.get("detail") or ""),
                            candidate_id=str(data.get("candidate_id") or ""),
                            candidate_name=str(data.get("candidate_name") or ""),
                            credibility=cred,
                            level=data.get("level") or None,
                            operator=str(data.get("operator") or "lan"),
                            source=str(data.get("source") or "12_局域网"))
                    except Exception as exc:                  # noqa: BLE001
                        r = {"ok": False, "reason": str(exc)}
                    app.ui_queue.put(("lan", (self.client_address[0], data, r)))
                    self._json(200 if r.get("ok", True) and not r.get("_error") else 502, r)

            self.server = ThreadingHTTPServer((host, port), Handler)
            self.server.daemon_threads = True
            self.server_thread = threading.Thread(target=self.server.serve_forever,
                                                  daemon=True)
            self.server_thread.start()
            ip = _local_ip()
            url = "http://%s:%d/" % (ip, port)
            self.lan_url.config(text=url, foreground=(ui_theme.ACCENT if ui_theme else "#22d3ee"))
            self.btn_lan.config(text="■ 停止接收")
            self.status.config(text="局域网接收已启动：" + url)
            self._log("局域网接收已启动：%s（监听 %s:%d）" % (url, host, port))
        except Exception as exc:                              # noqa: BLE001
            self.server = None
            messagebox.showerror(APP_TITLE, "启动失败：%s" % exc, parent=self.root)
            self.status.config(text="启动失败：%s" % exc)

    def _stop_lan(self):
        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
        except Exception:                                    # noqa: BLE001
            pass
        self.server = None
        self.btn_lan.config(text="▶ 启动接收")
        self.lan_url.config(text="未启动")
        self.status.config(text="局域网接收已停止")
        self._log("局域网接收已停止")

    def _lan_received(self, ip, data, r):
        self._received += 1
        ok = bool(r.get("ok", True)) and not r.get("_error")
        self.lan_tree.insert("", 0, values=(
            time.strftime("%H:%M:%S"), ip, data.get("code") or "-",
            data.get("room_id") or "-",
            ("成功 " + str(r.get("alert_id") or "")) if ok
            else ("失败：" + str(r.get("_error") or r.get("reason") or ""))[:120]))
        self._log("收到局域网告警：%s <- %s -> %s" % (data.get("code"), ip,
                                                    r.get("alert_id") or r.get("reason") or ""))
        if ok:
            self.refresh_alerts()

    def _html_page(self, has_token=False):
        token_hint = ("<p>访问令牌：<input name='token' placeholder='必填'></p>"
                      if has_token else "")
        return """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>12 告警上传 · 智能考场</title>
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#070d1a;color:#e8f1ff;margin:0;padding:18px}
.card{max-width:760px;margin:0 auto;background:#0d1a2e;border:1px solid #1b3457;border-radius:12px;padding:18px}
h1{font-size:20px;margin:0 0 4px} .sub{color:#8ba3c7;font-size:13px;margin-bottom:14px}
label{display:block;color:#8ba3c7;font-size:13px;margin:8px 0 3px}
input,textarea{width:100%;box-sizing:border-box;background:#122542;color:#e8f1ff;border:1px solid #1b3457;border-radius:6px;padding:8px}
textarea{height:70px} button{margin-top:12px;background:#1d4ed8;color:#fff;border:0;border-radius:6px;padding:10px 18px;font-size:15px}
pre{white-space:pre-wrap;background:#122542;border-radius:6px;padding:10px;color:#a7f3d0;min-height:20px}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px} td,th{border-bottom:1px solid #1b3457;padding:5px;text-align:left}
</style>
<div class="card"><h1>12 告警上传</h1>
<div class="sub">同一局域网内任意电脑/手机都可提交；提交后写入 03 智能终端，待终端确认/复核。</div>
<label>告警码 *</label><input id="code" value="REPEATED_TOUCH_LINE">
<label>设备编号</label><input id="device_id" value="CAM-A-101-1">
<label>考场</label><input id="room_id" value="A-101">
<label>座位</label><input id="seat_no">
<label>考生编号</label><input id="candidate_id">
<label>画质可信度（0~1，可空）</label><input id="credibility">
<label>操作人</label><input id="operator" value="局域网设备">
<label>详情</label><textarea id="detail">来自局域网设备的告警</textarea>
__TOKEN__
<button onclick="send()">上传到 03</button>
<pre id="out">等待上传…</pre>
<h3 style="font-size:14px">03 最近告警</h3><table id="alerts"></table>
</div>
<script>
async function send(){
  const body={code:v('code'),device_id:v('device_id'),room_id:v('room_id'),seat_no:v('seat_no'),
    candidate_id:v('candidate_id'),credibility:v('credibility'),operator:v('operator'),
    detail:v('detail'),source:'12_局域网网页'};
  const t=document.querySelector("input[name=token]"); if(t) body.token=t.value;
  const r=await fetch('/api/alert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json(); document.getElementById('out').textContent=JSON.stringify(j,null,2);
  load();
}
function v(id){return document.getElementById(id).value}
async function load(){
  try{const r=await fetch('/api/alerts?limit=10');const j=await r.json();const a=j.alerts||[];
    document.getElementById('alerts').innerHTML='<tr><th>编号</th><th>级别</th><th>告警码</th><th>状态</th></tr>'+
      a.map(x=>`<tr><td>${x.alert_id||''}</td><td>${x.level_name||x.level||''}</td><td>${x.code||''}</td><td>${x.status||''}</td></tr>`).join('');
  }catch(e){}
}
load();
</script></html>""".replace("__TOKEN__", token_hint)

    # ------------------------------------------------------------ 03 告警
    def refresh_alerts(self):
        def work():
            try:
                r = self.link.list_alerts(limit=50)
            except Exception as exc:                          # noqa: BLE001
                r = {"ok": False, "reason": str(exc)}
            self.ui_queue.put(("alerts", r))
        threading.Thread(target=work, daemon=True).start()

    def _show_alerts(self, r):
        for i in self.alert_tree.get_children():
            self.alert_tree.delete(i)
        if not r.get("ok", True):
            self.status.config(text="读取 03 告警失败：%s" % (r.get("reason") or r.get("_error")))
            return
        for a in r.get("alerts") or []:
            tag = "red" if a.get("level") == "red" else "orange"
            self.alert_tree.insert("", "end", iid=a.get("alert_id"),
                                   tags=(tag,),
                                   values=(a.get("alert_id"), a.get("level_name") or a.get("level"),
                                           a.get("created_at"), a.get("code"),
                                           a.get("room_id") or "—", a.get("device_id") or "—",
                                           a.get("status")))
        try:
            self.alert_tree.tag_configure("red", background="#3a1a1e",
                                          foreground="#fca5a5")
            self.alert_tree.tag_configure("orange", background="#3a2f14",
                                          foreground="#fbbf24")
        except Exception:                                    # noqa: BLE001
            pass
        self.status.config(text="已刷新 03 最近告警：%d 条" % len(r.get("alerts") or []))

    def _show_alert(self):
        sel = self.alert_tree.selection()
        if not sel:
            return
        vals = self.alert_tree.item(sel[0], "values")
        self.alert_detail.delete("1.0", "end")
        self.alert_detail.insert("1.0", " | ".join(str(x) for x in vals))


def main():
    root = tk.Tk()
    app = UploadApp(root)
    root.mainloop()
    try:
        if app.server is not None:
            app.server.shutdown()
    except Exception:                                        # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
