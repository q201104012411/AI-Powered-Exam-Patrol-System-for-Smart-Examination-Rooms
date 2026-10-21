# -*- coding: utf-8 -*-
r"""
robot_sim.py —— 巡考小车"本机演示"模拟服务（Windows 端 · 纯标准库）

用途：没有真车时，在 Windows 上跑一个和 ``webui/robot_web.py`` 同款 HTTP 接口的
模拟小车，用来演示 / 自测 04 模块的 Windows 控制端：

    python robot_sim.py                  # 默认 127.0.0.1:18080
    python robot_sim.py --port 18080 --host 0.0.0.0

模拟内容：
    · /api/state   电量缓降、里程计随指令积分、IMU 轻微摆动、前方障碍周期变近
    · /api/cmd     速度指令 + 小车看门狗（0.6s 无指令自动停）
    · /api/estop   急停锁定 / 解除
    · /api/line    一键巡线（状态按 FOLLOW / REACQUIRE 变化）
    · /api/params  巡线参数读取/写入
    · /api/snap    用 Pillow 画一张"仿真走廊"画面（无 Pillow 时退化为纯色 PNG）
    · /api/photo*  巡更拍照、照片列表、照片下载
    · /api/files   浏览本机 巡考机器人/ 代码目录（只读）

它只用于演示，不会连接 ROS，也不会驱动任何真实设备。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from robot_link import LINE_PARAMS, DEMO_PORT
except Exception:                                            # noqa: BLE001
    LINE_PARAMS = {
        "line_color": "black", "speed": 0.18, "kp": 0.85, "kd": 0.18,
        "deadband": 0.07, "max_angular": 0.50, "v_min": 0.035,
        "search_speed": 0.05, "search_time": 10.0,
        "v_thresh": 75.0, "s_thresh": 90.0,
        "dir_weight": 0.75, "lock_dist": 0.30, "approach_dist": 0.80,
        "approach_gain": 0.05,
        "coast_time": 3.0, "full_lock_err": 0.45, "over_steer": 0.65,
    }
    DEMO_PORT = 18080

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except Exception:                                            # noqa: BLE001
    Image = ImageDraw = ImageFont = None
    HAS_PIL = False

HERE = os.path.dirname(os.path.abspath(__file__))
# 演示用代码目录（只读）：对应真车上的 ~/patrol_robot（含 tools/、webui/）
WS_ROOT = os.path.join(HERE, "巡考机器人", "01_代码与程序")
if not os.path.isdir(os.path.join(WS_ROOT, "webui")):
    WS_ROOT = os.path.join(HERE, "巡考机器人")
MIN_V, MAX_V = 0.0, 0.20
MIN_W, MAX_W = 0.0, 0.60
CAM_NAMES = {"astra": "Astra RGB", "depth": "Astra Depth", "usb": "USB Wide"}


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _fmt_ts():
    return time.strftime("%H:%M:%S")


class SimCar(object):
    """模拟小车：状态 + HTTP 服务 + 20Hz 物理积分。"""

    def __init__(self, host="127.0.0.1", port=DEMO_PORT):
        self.host = host
        self.port = int(port)
        self.lock = threading.Lock()
        self.started_at = time.time()
        self._stop = threading.Event()

        self.voltage = 25.2
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.v = 0.0
        self.w = 0.0
        self.estop = False
        self.reason = ""
        self.last_cmd_t = 0.0
        self.uptime = 0.0

        self.line_running = False
        self.line_color = "black"
        self.line_since = 0.0
        self.line_corners = 0
        self.line_status = None
        self.line_log = []
        self._last_phase = -1
        self.params = dict(LINE_PARAMS)

        self.cam_src = "both"
        self.photos = []
        self.photo_dir = tempfile.mkdtemp(prefix="patrol_sim_photos_")
        self.index = 0

        self._snap_cache = {}
        self._snap_lock = threading.Lock()

        self._server = None
        self._serve_thread = None
        self._tick_thread = None

    # ------------------------------------------------------------ 生命周期
    def start(self):
        handler = _make_handler(self)
        last_error = None
        for candidate in (self.port, 0):
            try:
                self._server = ThreadingHTTPServer((self.host, candidate), handler)
                break
            except OSError as exc:                           # noqa: BLE001
                last_error = exc
                self._server = None
        if self._server is None:
            raise RuntimeError("模拟小车启动失败：%s" % last_error)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever, name="sim-http")
        self._serve_thread.daemon = True
        self._serve_thread.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, name="sim-tick")
        self._tick_thread.daemon = True
        self._tick_thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:                                # noqa: BLE001
                pass
            self._server = None
        try:
            for name in os.listdir(self.photo_dir):
                os.remove(os.path.join(self.photo_dir, name))
            os.rmdir(self.photo_dir)
        except OSError:
            pass

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port)

    def address(self):
        return "%s:%d" % (self.host, self.port)

    # ------------------------------------------------------------ 物理
    def _tick_loop(self):
        dt = 0.05
        last = time.time()
        while not self._stop.is_set():
            now = time.time()
            step = min(0.2, max(0.001, now - last))
            last = now
            with self.lock:
                self.uptime = now - self.started_at
                v, w = self.v, self.w
                if self.estop or (self.last_cmd_t and now - self.last_cmd_t > 0.6):
                    v = w = 0.0
                self.x += v * math.cos(self.yaw) * step
                self.y += v * math.sin(self.yaw) * step
                self.yaw = _wrap_pi(self.yaw + w * step)
                if v > 0:
                    self.voltage = max(21.6, self.voltage - 0.00055 * step)
                if self.line_running:
                    self._update_line(now)
            self._stop.wait(dt)

    def _update_line(self, now):
        """巡线状态：FOLLOW / REACQUIRE 交替，让界面有东西看。"""
        elapsed = now - self.line_since
        phase = int(elapsed) % 14
        state = "REACQUIRE" if 9 <= phase <= 10 else "FOLLOW"
        if phase == 11 and self._last_phase != phase:
            self.line_corners += 1
        self._last_phase = phase
        err = 0.12 * math.sin(elapsed * 0.9)
        self.line_status = {
            "ts": now,
            "state": state,
            "err": round(err, 3),
            "bands": 3 if state == "FOLLOW" else 0,
            "cover": round(0.55 + 0.15 * math.sin(elapsed), 3),
            "corners": self.line_corners,
            "v": round(self.params.get("speed", 0.18), 3),
            "w": round(-0.35 * err, 3),
        }
        if int(now * 2) != int((now - 0.05) * 2):
            if state == "FOLLOW":
                self._log_line("%s  FOLLOW err=%+.2f  v=%.2f  w=%+.2f"
                               % (_fmt_ts(), err, self.params.get("speed", 0.18),
                                  -0.35 * err))
            else:
                self._log_line("%s  REACQUIRE 找不到线，顺弯搜索…" % _fmt_ts())

    def _log_line(self, text):
        self.line_log.append(text)
        if len(self.line_log) > 200:
            del self.line_log[:100]

    # ------------------------------------------------------------ 状态
    def state(self):
        with self.lock:
            now = time.time()
            t = now - self.started_at
            front = 0.28 + 1.05 * abs(math.sin(t / 9.0))
            left = 0.85 + 0.35 * abs(math.sin(t / 11.0 + 1.0))
            right = 1.05 + 0.45 * abs(math.sin(t / 13.0 + 2.0))
            rear = 2.0 + 0.6 * abs(math.sin(t / 17.0))
            pitch = 0.9 * math.sin(t / 6.0)
            roll = 0.7 * math.sin(t / 8.0 + 0.7)
            yaw_deg = math.degrees(self.yaw)
            blocked = ""
            if self.estop:
                blocked = "急停"
            elif self.v > 0 and front < 0.35:
                blocked = "前方 %.2fm 障碍" % front
            line_status = None
            if self.line_running and self.line_status:
                if now - self.line_status.get("ts", 0) < 3.0:
                    line_status = dict(self.line_status)
            tail = "\n".join(self.line_log[-18:])
            return {
                "ok": 1,
                "estop": 1 if self.estop else 0,
                "blocked": blocked,
                "reason": self.reason,
                "dry_run": 0,
                "battery": round(self.voltage, 2),
                "odom": {"x": round(self.x, 3), "y": round(self.y, 3),
                         "yaw": round(yaw_deg, 1)},
                "imu": {"yaw": round(yaw_deg, 1), "pitch": round(pitch, 2),
                        "roll": round(roll, 2)},
                "clear": {"front": round(front, 3), "left": round(left, 3),
                          "right": round(right, 3), "rear": round(rear, 3)},
                "cmd": {"v": round(self.v, 3), "w": round(self.w, 3),
                        "age": round(now - self.last_cmd_t, 2)
                        if self.last_cmd_t else 99.0},
                "limits": {"max_linear": MAX_V, "max_angular": MAX_W,
                           "stop_distance": 0.35, "max_tilt": 3.0},
                "uptime": round(self.uptime, 1),
                "line": {"running": 1 if self.line_running else 0,
                         "rc": None, "status": line_status, "log": tail},
            }

    # ------------------------------------------------------------ 指令
    def set_cmd(self, v, w):
        with self.lock:
            self.v = _clamp(float(v), -MAX_V, MAX_V)
            self.w = _clamp(float(w), -MAX_W, MAX_W)
            self.last_cmd_t = time.time()
            self.reason = ""

    def stop_cmd(self):
        with self.lock:
            self.v = self.w = 0.0
            self.last_cmd_t = time.time()
            self.reason = ""

    def set_estop(self, on):
        with self.lock:
            self.estop = bool(on)
            if self.estop:
                self.v = self.w = 0.0
                self.reason = "急停锁定"
                if self.line_running:
                    self._stop_line_locked()
            else:
                self.reason = ""

    def line_on(self, color=None):
        with self.lock:
            if color in ("black", "white"):
                self.line_color = color
            if self.estop:
                return {"ok": 0, "err": "急停锁定中，先解除急停"}
            if self.line_running:
                return {"ok": 1, "already": 1}
            self.line_running = True
            self.line_since = time.time()
            self.line_corners = 0
            self.line_status = None
            self._last_phase = -1
            self._log_line("%s  [sim] 启动巡线：%s 线  speed=%.2f"
                           % (_fmt_ts(), "黑" if self.line_color == "black" else "白",
                              self.params.get("speed", 0.18)))
            return {"ok": 1, "pid": 10000 + random.randint(1, 900)}

    def line_off(self):
        with self.lock:
            if not self.line_running:
                return {"ok": 1, "already": 1}
            self._stop_line_locked()
            return {"ok": 1}

    def _stop_line_locked(self):
        self.line_running = False
        self.line_status = None
        self._log_line("%s  [sim] 巡线已停止（共 %d 个弯）"
                       % (_fmt_ts(), self.line_corners))

    def save_params(self, incoming):
        with self.lock:
            for key, val in (incoming or {}).items():
                if key not in LINE_PARAMS:
                    continue
                if key == "line_color":
                    self.params[key] = str(val)
                else:
                    try:
                        self.params[key] = float(val)
                    except (TypeError, ValueError):
                        pass
            return dict(self.params)

    # ------------------------------------------------------------ 画面
    def snapshot(self, cam="astra"):
        if cam not in CAM_NAMES:
            return None
        now = time.time()
        with self._snap_lock:
            cached = self._snap_cache.get(cam)
            if cached and now - cached[0] < 0.25:
                return cached[1]
        data = self._render(cam)
        with self._snap_lock:
            self._snap_cache[cam] = (now, data)
        return data

    def _render(self, cam):
        if not HAS_PIL:
            return _png_gradient(320, 240)
        width, height = 640, 480
        img = Image.new("RGB", (width, height), (8, 13, 22))
        draw = ImageDraw.Draw(img)
        title_font = _font(22, bold=True)
        font = _font(15)
        small = _font(13)

        if cam == "depth":
            for y in range(height):
                ratio = y / float(height)
                color = (int(20 + 40 * ratio), int(90 + 120 * ratio),
                         int(160 + 80 * ratio))
                draw.line([(0, y), (width, y)], fill=color)
            for i, (x0, y0, x1, y1, shade) in enumerate((
                    (80, 250, 260, 430, (255, 180, 40)),
                    (330, 210, 520, 400, (60, 230, 220)),
                    (500, 300, 620, 460, (40, 120, 255)))):
                draw.rectangle((x0, y0, x1, y1), fill=shade)
            draw.text((22, 20), "SIM DEPTH 0.3-3.0m", font=title_font,
                      fill=(255, 255, 255))
        else:
            for y in range(height):
                ratio = y / float(height)
                color = (int(9 + 10 * ratio), int(15 + 16 * ratio),
                         int(26 + 24 * ratio))
                draw.line([(0, y), (width, y)], fill=color)
            horizon = 196
            vanishing = (width // 2, horizon)
            # 走廊墙面
            draw.polygon([(0, 0), (205, horizon), (205, height), (0, height)],
                         fill=(15, 24, 38))
            draw.polygon([(width, 0), (435, horizon), (435, height),
                          (width, height)], fill=(18, 28, 44))
            # 地面
            draw.polygon([(205, horizon), (435, horizon), (width, height),
                          (0, height)], fill=(24, 31, 44))
            # 透视线
            for i in range(-6, 7):
                x = width // 2 + i * 90
                draw.line([vanishing, (x, height)], fill=(44, 58, 82), width=1)
            for i in range(1, 7):
                y = horizon + int((height - horizon) * (i / 7.0) ** 1.9)
                draw.line([(0, y), (width, y)], fill=(38, 50, 70), width=1)
            # 地面引导线（黑线 / 白线）
            line_color = (18, 18, 20) if self.line_color == "black" else (235, 238, 242)
            draw.polygon([(308, horizon), (332, horizon), (420, height),
                          (220, height)], fill=line_color)
            # 墙上的"考场"标记
            draw.rectangle((70, 120, 170, 176), outline=(70, 96, 130), width=2)
            draw.text((82, 136), "ROOM 101", font=small, fill=(120, 150, 188))
            draw.rectangle((470, 120, 570, 176), outline=(70, 96, 130), width=2)
            draw.text((482, 136), "ROOM 102", font=small, fill=(120, 150, 188))

        # 顶部 HUD
        draw.rectangle((0, 0, width, 52), fill=(5, 9, 16))
        draw.text((18, 12), "SIMULATED CAMERA", font=title_font,
                  fill=(34, 211, 238))
        draw.text((width - 210, 16), CAM_NAMES.get(cam, cam), font=font,
                  fill=(180, 200, 225))
        # 底部信息
        draw.rectangle((0, height - 46, width, height), fill=(5, 9, 16))
        draw.text((18, height - 34),
                  "DEMO ONLY - NO REAL VIDEO   %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                  font=small, fill=(130, 155, 185))
        draw.text((width - 190, height - 34),
                  "BAT %.1fV  %s" % (self.voltage, _mode_text(self)),
                  font=small, fill=(130, 155, 185))
        # 十字准星
        draw.line([(width // 2, 70), (width // 2, height - 60)],
                  fill=(40, 60, 84), width=1)
        draw.line([(30, height // 2), (width - 30, height // 2)],
                  fill=(40, 60, 84), width=1)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        return buf.getvalue()

    def save_photo(self, cam, note):
        data = self.snapshot(cam)
        if not data:
            return None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(ch for ch in (note or "") if ch not in '\\/:*?"<>|') or "photo"
        name = "%s_sim_%s_%s.jpg" % (stamp, cam, safe)
        path = os.path.join(self.photo_dir, name)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            return None
        self.photos.insert(0, name)
        return {"name": name, "bytes": len(data)}

    def photo_list(self):
        items = []
        for name in self.photos[:30]:
            path = os.path.join(self.photo_dir, name)
            try:
                items.append({"name": name, "size": os.path.getsize(path),
                              "mtime": time.strftime(
                                  "%m-%d %H:%M",
                                  time.localtime(os.path.getmtime(path)))})
            except OSError:
                continue
        return items

    # ------------------------------------------------------------ 文件
    def _safe_path(self, rel):
        if not rel or ".." in rel.replace("\\", "/"):
            return None
        root = os.path.realpath(WS_ROOT)
        path = os.path.realpath(os.path.join(WS_ROOT, rel))
        if not path.startswith(root + os.sep):
            return None
        return path

    def file_list(self):
        items = []
        for sub in ("", "tools", "webui"):
            folder = os.path.join(WS_ROOT, sub) if sub else WS_ROOT
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                if not name.endswith((".py", ".sh", ".yaml", ".yml", ".md",
                                      ".html", ".json")):
                    continue
                rel = (sub + "/" + name) if sub else name
                items.append({
                    "rel": rel,
                    "size": os.path.getsize(path),
                    "mtime": time.strftime("%m-%d %H:%M",
                                           time.localtime(os.path.getmtime(path))),
                    "editable": False,
                })
        return items

    # ------------------------------------------------------------ HTTP 路由
    def api(self, method, path, query, body):
        """返回 (status, content_type, body_bytes)。"""
        if method == "POST" and path == "/api/params":
            try:
                payload = json.loads((body or b"{}").decode("utf-8"))
            except ValueError as exc:
                return _json(400, {"ok": 0, "err": "请求体不是 JSON：%s" % exc})
            incoming = payload.get("params") if isinstance(payload, dict) else None
            params = self.save_params(incoming or {})
            return _json(200, {"ok": 1, "params": params})

        if method == "POST" and path == "/api/file":
            return _json(403, {"ok": 0, "err": "演示模式只读，不能改小车代码"})

        if method != "GET":
            return _json(405, {"ok": 0, "err": "演示服务不支持该请求"})

        if path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", _index_html(self)

        if path == "/api/state":
            return _json(200, self.state())

        if path == "/api/cmd":
            try:
                v = float(query.get("v", 0.0))
                w = float(query.get("w", 0.0))
            except ValueError:
                return _json(400, {"ok": 0, "err": "v/w 必须是数字"})
            self.set_cmd(v, w)
            return _json(200, {"ok": 1, "cmd": {"v": v, "w": w}})

        if path == "/api/stop":
            self.stop_cmd()
            return _json(200, {"ok": 1, "stopped": 1})

        if path == "/api/estop":
            self.set_estop(str(query.get("on", "1")) in ("1", "true", "yes"))
            return _json(200, {"ok": 1, "estop": 1 if self.estop else 0})

        if path == "/api/line":
            on = str(query.get("on", "0")) in ("1", "true", "yes")
            color = query.get("color")
            return _json(200, self.line_on(color) if on else self.line_off())

        if path == "/api/line/log":
            return _json(200, {"ok": 1, "log": "\n".join(self.line_log[-80:])})

        if path == "/api/params":
            with self.lock:
                return _json(200, {"ok": 1, "params": dict(self.params)})

        if path == "/api/camsource":
            src = str(query.get("src", "astra"))
            if src not in ("astra", "usb", "both"):
                return _json(400, {"ok": 0, "err": "src 只能是 astra / usb / both"})
            with self.lock:
                self.cam_src = src
                self._log_line("%s  [sim] 切换相机源：%s" % (_fmt_ts(), src))
            return _json(200, {"ok": 1, "src": src,
                               "detail": "演示模式：已切换到 %s（无需等待真机重启）" % src})

        if path == "/api/snap":
            cam = str(query.get("cam", "astra"))
            data = self.snapshot(cam)
            if data is None:
                return _json(400, {"ok": 0, "err": "未知相机：%s" % cam})
            return 200, "image/jpeg" if HAS_PIL else "image/png", data

        if path == "/api/photo":
            cam = str(query.get("cam", "usb"))
            note = str(query.get("note", ""))[:40]
            saved = self.save_photo(cam, note)
            if not saved:
                return _json(500, {"ok": 0, "err": "拍照失败"})
            return _json(200, {"ok": 1, "name": saved["name"],
                               "bytes": saved["bytes"]})

        if path == "/api/photos":
            return _json(200, {"ok": 1, "photos": self.photo_list()})

        if path == "/api/photo/get":
            name = os.path.basename(str(query.get("name", "")))
            full = os.path.join(self.photo_dir, name)
            if not name or not os.path.isfile(full):
                return _json(404, {"ok": 0, "err": "照片不存在"})
            with open(full, "rb") as f:
                return 200, "image/jpeg", f.read()

        if path == "/api/files":
            return _json(200, {"ok": 1, "root": WS_ROOT, "files": self.file_list()})

        if path == "/api/file":
            full = self._safe_path(str(query.get("path", "")))
            if not full or not os.path.isfile(full):
                return _json(400, {"ok": 0, "err": "文件不存在或路径非法"})
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    return _json(200, {"ok": 1, "path": query.get("path"),
                                       "code": f.read()})
            except OSError as exc:
                return _json(500, {"ok": 0, "err": str(exc)})

        if path == "/api/code":
            follower = os.path.join(WS_ROOT, "tools", "line_follow_white.py")
            try:
                with open(follower, "r", encoding="utf-8", errors="replace") as f:
                    return _json(200, {"ok": 1, "path": follower, "code": f.read()})
            except OSError as exc:
                return _json(500, {"ok": 0, "err": str(exc)})

        if path.startswith("/api"):
            return _json(404, {"ok": 0, "err": "演示服务没有这个接口"})
        return 200, "text/html; charset=utf-8", _index_html(self)


# ======================================================================
# HTTP 适配
# ======================================================================
def _make_handler(sim):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PatrolSim/1.0"

        def log_message(self, fmt, *args):              # 静默
            pass

        def _do(self, method):
            try:
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, ctype, payload = sim.api(method, parsed.path, query, body)
            except Exception as exc:                    # noqa: BLE001
                status, ctype, payload = _json(500, {"ok": 0,
                                                     "err": "模拟服务异常：%s" % exc})
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

    return Handler


def _json(status, obj):
    return status, "application/json; charset=utf-8", \
        json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _font(size, bold=False):
    if not HAS_PIL:
        return None
    windir = os.environ.get("WINDIR", r"C:\Windows")
    names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "simhei.ttf")
    for name in names:
        path = os.path.join(windir, "Fonts", name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default()
    except Exception:                                        # noqa: BLE001
        return None


def _wrap_pi(angle):
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _mode_text(sim):
    if sim.estop:
        return "E-STOP"
    if sim.line_running:
        return "LINE"
    if sim.v or sim.w:
        return "MOVE"
    return "IDLE"


def _png_gradient(width, height):
    """无 Pillow 时的退化画面：深蓝渐变 PNG（纯标准库）。"""
    rows = []
    for y in range(height):
        ratio = y / float(height)
        row = b"".join(bytes((int(8 + 20 * ratio), int(14 + 40 * ratio),
                              int(26 + 80 * ratio))) for _ in range(width))
        rows.append(row)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    return png


def _index_html(sim):
    return ("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>巡考小车 · 本机演示</title>
<style>body{background:#070d1a;color:#e8f1ff;font-family:"Microsoft YaHei";
padding:28px}code{color:#22d3ee}a{color:#22d3ee}</style>
<h2>巡考小车 · 本机演示服务</h2>
<p>这是 04 巡考机器人的 Windows 本机模拟服务，接口与小车端
<code>webui/robot_web.py</code> 一致，不会驱动任何真实设备。</p>
<table cellpadding=6><tr><td>地址</td><td><code>%s</code></td></tr>
<tr><td>状态</td><td><a href="/api/state">/api/state</a></td></tr>
<tr><td>画面</td><td><a href="/api/snap?cam=astra">/api/snap?cam=astra</a> ·
<a href="/api/snap?cam=depth">depth</a> · <a href="/api/snap?cam=usb">usb</a></td></tr>
<tr><td>运行时长</td><td>%.0f s</td></tr></table>
<p>在 04 模块的 Windows 控制端点「本机演示」即可连接本服务。</p>
</html>""" % (sim.address(), time.time() - sim.started_at)).encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="巡考小车本机演示服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEMO_PORT)
    args = parser.parse_args(argv)
    sim = SimCar(args.host, args.port).start()
    print("=" * 58)
    print("巡考小车 · 本机演示服务已启动")
    print("  控制接口：%s" % sim.url)
    print("  演示画面：%s/api/snap?cam=astra" % sim.url)
    print("  在 04 模块 Windows 控制端连接：%s" % sim.address())
    print("  按 Ctrl+C 退出")
    print("=" * 58)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[退出] 关闭模拟服务")
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
