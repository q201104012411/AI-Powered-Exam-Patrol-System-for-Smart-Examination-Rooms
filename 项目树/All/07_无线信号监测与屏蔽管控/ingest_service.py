# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 频谱采集服务（真设备接入口）

本机没有频谱仪，但**接口是真的**：接上支持"按行推 JSON"的频谱仪/监测站，或由采集
网关把设备数据转成 JSON 推进来，`spectrum.ingest_scan` 就会按同一套算法检测、告警、
定位。没有真设备时用内置模拟器（source=sim），两者走同一条处理链。

协议（HTTP，默认 127.0.0.1:9127）：
    GET  /health                      -> 服务与协议版本
    GET  /api/status                  -> 本模块概览
    GET  /api/latest?station=MON-A    -> 最近一帧（含峰值）
    POST /api/scan                    -> 推一帧频谱（JSON，见下）
    POST /api/heartbeat               -> {"code":"MON-A","status":"online"}

    POST /api/scan 的 body（三种形态任选其一）：
      {"station_code":"MON-A","bins":[[2400.0,-101.2],[2400.2,-100.8], ...]}
      {"station_code":"MON-A","noise_floor_dbm":-100,
       "peaks":[{"freq_mhz":2442.0,"power_dbm":-62.0,"bandwidth_mhz":2.0}]}
      {"station_code":"MON-A","freq_mhz":433.5,"power_dbm":-78.0,
       "bandwidth_mhz":0.025}
    可选字段：band、ts_epoch、room_id、duty、source（服务端强制 real）。

鉴权：
  · 默认只监听 127.0.0.1，回环地址不需要令牌；
  · 跨机必须 `--token`（请求头 X-RF-Token），否则 403；
  · `--host` 不是回环又没有令牌时**拒绝启动**（fail-closed，不裸奔）。

TCP 行协议（可选，`--tcp-port 9128`，**仅允许回环监听**）：
    一行 JSON 一帧，回一行 JSON 结果。TCP 没有请求头可放令牌，所以跨机采集
    一律走 HTTP + X-RF-Token；把 TCP 限制在回环，避免出现"无鉴权的旁路"。
    零第三方依赖：只用标准库 http.server / socketserver / json。
"""
import argparse
import json
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config                                                # noqa: E402
import models                                                # noqa: E402
import spectrum                                              # noqa: E402
import storage                                               # noqa: E402

PROTOCOL_VERSION = 1
SERVICE_NAME = "07-rf-ingest"
TOKEN_HEADER = "X-RF-Token"
MAX_BODY = 2 * 1024 * 1024          # 单帧最大 2MB（4000 个 bin 的 JSON 约 100KB）


def is_loopback(host):
    h = str(host or "").strip("[]")
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


class ScanService(object):
    """采集服务业务层（HTTP/TCP 共用；可在自检里直接注入临时 Store）。"""

    def __init__(self, store=None, token="", allow_local_no_token=True):
        self.store = store or storage.Store()
        self.token = str(token or "")
        self.allow_local_no_token = bool(allow_local_no_token)
        self._lock = threading.Lock()
        self.frames = 0
        self.rejected = 0
        self.started = models.now_ts()

    # ---------------- 鉴权 ----------------
    def authorized(self, client_ip, headers):
        token = ""
        try:
            token = (headers.get(TOKEN_HEADER) or "").strip()
        except AttributeError:
            token = ""
        if self.token:
            return token == self.token, ("" if token == self.token
                                          else "令牌不正确")
        if self.allow_local_no_token and is_loopback(client_ip):
            return True, ""
        return False, "跨机访问必须配置令牌（--token）"

    # ---------------- 业务 ----------------
    def health(self):
        return {"ok": True, "service": SERVICE_NAME, "proto": PROTOCOL_VERSION,
                "system": "07_无线信号监测与屏蔽管控",
                "auth": "token" if self.token else "loopback-only",
                "frames": self.frames, "started": self.started}

    def status(self):
        return {"ok": True, "service": SERVICE_NAME, "proto": PROTOCOL_VERSION,
                "summary": self.store.summary(), "frames": self.frames,
                "rejected": self.rejected, "started": self.started,
                "data_dir": str(config.DATA_ROOT)}

    def latest(self, station=""):
        scan = self.store.latest_scan(station_code=station or None)
        if scan is None:
            return {"ok": False, "reason": "还没有扫描帧"}
        d = scan.to_dict()
        d.pop("raw", None)
        return {"ok": True, "scan": d}

    def handle_scan(self, frame, client_ip="-"):
        if not isinstance(frame, dict):
            self.rejected += 1
            return {"ok": False, "reason": "body 必须是 JSON 对象"}
        payload = dict(frame)
        payload["source"] = "real"          # 采集服务来的帧一律视为真设备数据
        with self._lock:
            r = spectrum.ingest_scan(self.store, payload, source="real")
            if r.get("ok"):
                self.frames += 1
            else:
                self.rejected += 1
        r = dict(r or {})
        r["proto"] = PROTOCOL_VERSION
        r["from"] = client_ip
        return r

    def handle_heartbeat(self, body, client_ip="-"):        # noqa: ARG002
        code = str((body or {}).get("code") or "").strip()
        status = str((body or {}).get("status") or "online").strip()
        if not code:
            return {"ok": False, "reason": "缺少 code"}
        dev = self.store.heartbeat(code, status)
        if dev is None:
            return {"ok": False, "reason": "设备不存在：%s" % code}
        return {"ok": True, "device_code": dev.code, "status": dev.status,
                "last_seen": dev.last_seen}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ExamRFIngest/" + str(PROTOCOL_VERSION)
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):                  # noqa: A003
            pass

        def _client(self):
            try:
                return self.client_address[0]
            except (IndexError, TypeError):
                return "-"

        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            try:
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _deny(self, reason):
            service.rejected += 1
            self.store_log("接入被拒", "来源=%s 原因=%s" % (self._client(), reason))
            self._send(403, {"ok": False, "reason": reason, "proto": PROTOCOL_VERSION})

        def store_log(self, action, detail):
            try:
                service.store.log(action, detail, "ingest")
            except Exception:                               # noqa: BLE001
                pass

        def _auth(self):
            ok, why = service.authorized(self._client(), self.headers)
            if not ok:
                self._deny(why)
            return ok

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        def do_GET(self):                               # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path not in ("/health", "/api/status", "/api/latest"):
                self._send(404, {"ok": False, "reason": "未知路径：%s" % path,
                                 "paths": ["/health", "/api/status", "/api/latest",
                                           "POST /api/scan", "POST /api/heartbeat"]})
                return
            if not self._auth():
                return
            if path == "/health":
                self._send(200, service.health())
            elif path == "/api/status":
                self._send(200, service.status())
            else:
                q = parse_qs(parsed.query)
                self._send(200, service.latest((q.get("station") or [""])[0]))

        def do_POST(self):                              # noqa: N802
            path = (urlparse(self.path).path.rstrip("/") or "/")
            if path not in ("/api/scan", "/api/heartbeat"):
                self._send(404, {"ok": False, "reason": "未知路径：%s" % path})
                return
            if not self._auth():
                return
            body = self._read_body()
            if body is None:
                self._send(400, {"ok": False, "reason": "body 必须是 JSON 对象，"
                                                        "且不超过 %d 字节" % MAX_BODY})
                return
            if path == "/api/heartbeat":
                out = service.handle_heartbeat(body, self._client())
            else:
                out = service.handle_scan(body, self._client())
            self._send(200 if out.get("ok") else 400, out)
    return Handler


def build_server(service, host="127.0.0.1", port=9127):
    return ThreadingHTTPServer((host, int(port)), make_handler(service))


class _TcpHandler(socketserver.StreamRequestHandler):
    def handle(self):                                       # noqa: D401
        service = getattr(self.server, "service", None)
        if service is None:
            return
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                out = {"ok": False, "reason": "一行必须是 JSON"}
            else:
                out = service.handle_scan(frame, self.client_address[0])
            out["proto"] = PROTOCOL_VERSION
            try:
                self.wfile.write((json.dumps(out, ensure_ascii=False) + "\n")
                                 .encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_tcp_server(service, host="127.0.0.1", port=9128):
    srv = _TcpServer((host, int(port)), _TcpHandler)
    srv.service = service
    return srv


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main(argv=None):
    ap = argparse.ArgumentParser(description="07 频谱采集服务（真设备接入口）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（跨机用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=9127, help="HTTP 端口，默认 9127")
    ap.add_argument("--token", default=os.environ.get("EXAM_RF_INGEST_TOKEN", ""),
                    help="跨机访问令牌（请求头 X-RF-Token）")
    ap.add_argument("--tcp-port", type=int, default=0,
                    help="可选：同时开 TCP 行协议端口（0=不开）")
    ap.add_argument("--db", default="", help="数据库路径（默认 data/rf.db）")
    args = ap.parse_args(argv)

    host = (args.host or "127.0.0.1").strip()
    token = (args.token or "").strip()
    if not is_loopback(host) and not token:
        print("[错误] 监听非回环地址必须配置 --token（或环境变量 EXAM_RF_INGEST_TOKEN）。")
        print("       这是 fail-closed 设计：不允许无鉴权接收传感器数据。")
        return 1
    store = storage.Store(args.db or None)
    service = ScanService(store=store, token=token)
    if args.tcp_port and not is_loopback(host):
        print("[警告] TCP 行协议没有令牌通道，只允许回环监听；跨机请用 HTTP + 令牌。"
              "已关闭 TCP 端口。")
        args.tcp_port = 0
    try:
        httpd = build_server(service, host, args.port)
    except OSError as e:
        print("[错误] 启动 HTTP 服务失败：%s" % e)
        return 1
    tcpd = None
    if args.tcp_port:
        try:
            tcpd = build_tcp_server(service, "127.0.0.1", args.tcp_port)
        except OSError as e:
            print("[警告] TCP 端口启动失败（只开 HTTP）：%s" % e)
            tcpd = None
    print("=" * 70)
    print("07 无线信号监测与屏蔽管控 · 频谱采集服务")
    print("数据目录：%s" % config.DATA_ROOT)
    print("HTTP   ：http://%s:%d   （POST /api/scan）" % (host, args.port))
    if host in ("0.0.0.0", "::"):
        print("局域网 ：http://%s:%d" % (_local_ip(), args.port))
    if tcpd is not None:
        print("TCP    ：%s:%d（一行 JSON 一帧）" % (host, args.tcp_port))
    print("鉴权   ：%s" % ("令牌（X-RF-Token）" if token else "仅回环地址"))
    print("停止   ：Ctrl+C")
    print("=" * 70)
    threads = []
    if tcpd is not None:
        t = threading.Thread(target=tcpd.serve_forever, daemon=True)
        t.start()
        threads.append(t)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        try:
            httpd.shutdown()
        except Exception:                                   # noqa: BLE001
            pass
        if tcpd is not None:
            try:
                tcpd.shutdown()
            except Exception:                               # noqa: BLE001
                pass
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
