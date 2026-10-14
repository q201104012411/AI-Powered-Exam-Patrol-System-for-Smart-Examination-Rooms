# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 安检门接入与判据引擎

分两层，**两层都是真实可用的**，只有数据来源不同：

  第 1 层 判据引擎（decide）
    输入：安检门六路感应强度（金属/磁场/手机特征/耳机特征/毫米波/鞋底）
    输出：clear 或 报警 + 报警类别 + 等级（橙/红）
    规则：强度过阈值即"疑似"；≥2 路同时报 -> 红色（双判据确认），
          只有 1 路报 -> 橙色 + 复检（既不放过单路能发现的手机，也不让误报堆积）。

  第 2 层 设备接入（三种真入口 + 一种模拟器）
    HTTP 行协议  POST /api/pass  一行 JSON      -> HttpIngest
    TCP 行协议   一行 JSON（默认 9006 端口）      -> TcpIngest
    串口行协议   一行 JSON（需 pyserial）         -> SerialIngest
    内置模拟器   造数据演示，全部标 source=sim    -> SimGate
    人工录入     复检/演练时手工补录               -> gate.ingest(..., source="manual")

行协议（真设备按这个推，字段能少不能错）：
  {"device_code":"GATE-A01","candidate_id":"...","candidate_name":"...",
   "seat_no":"07","room_id":"1考场","duration_ms":820,
   "sensors":{"metal":0.71,"magnetic":0.20,"phone":0.05,
              "earphone":0.02,"millimeter":0.10,"shoe":0.03}}
也接受把 sensors 的键平铺在顶层（metal/phone/... 直接写在最外层）。

**绝不假装检过**：没有真设备时界面与报告都写明 source=sim；本模块不会把模拟
数据写成 real。
"""
import json
import os
import random
import socket
import socketserver
import threading
import time

import config
import models

# 第 1 层：判据引擎 ------------------------------------------------------------

# 感应通道 -> 报警类别（毫米波看的是"贴身藏匿"，按手机/器材两类中最接近的处理）
SENSOR_TO_KIND = {
    "metal": config.KIND_METAL,
    "magnetic": config.KIND_MAGNETIC,
    "phone": config.KIND_MOBILE,
    "earphone": config.KIND_EARPHONE,
    "millimeter": config.KIND_MOBILE,
    "shoe": config.KIND_SHOE,
}
# 同一类别由多路支撑时只报一次（毫米波+手机特征都命中就是"手机"一条）
KIND_ORDER = (config.KIND_MOBILE, config.KIND_EARPHONE, config.KIND_METAL,
              config.KIND_MAGNETIC, config.KIND_SHOE)


def normalize_scores(raw, sensor_keys=None):
    """把任意来源的感应值收成 {sensor: float(0~1)}；缺失补 0，超界截断。"""
    keys = sensor_keys or config.SENSOR_KEYS
    out = {}
    for k in keys:
        try:
            v = float((raw or {}).get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        out[k] = max(0.0, min(1.0, v))
    return out


def effective_thresholds(thresholds=None, sensitivity=1.0, gate_mode=None):
    """
    生效阈值 = 全局阈值 / 灵敏度。
    灵敏度 1.2 表示这台门调得更灵（阈值降低），0.8 表示调钝（阈值抬高）。
    """
    base = dict(thresholds or config.thresholds())
    if gate_mode:
        scale = config.GATE_MODE_SCALE.get(str(gate_mode), 1.0)
        base = {k: v * scale for k, v in base.items()}
    try:
        s = max(0.2, min(3.0, float(sensitivity or 1.0)))
    except (TypeError, ValueError):
        s = 1.0
    return {k: max(0.05, min(0.98, v / s)) for k, v in base.items()}


def decide(scores, thresholds=None, sensitivity=1.0, double_confirm=None,
           gate_mode=None):
    """
    判据引擎主入口。返回 dict：
      result     clear / alarm
      kinds      报警类别（去重、按 KIND_ORDER 排序）
      levels     与 kinds 一一对应的级别
      level      总体级别（有红则红，否则橙）
      exceeded   触发的感应通道及其读数
      detail     人话解释（界面与报告直接显示）
      thresholds 本次实际生效的阈值
    """
    sc = normalize_scores(scores)
    th = effective_thresholds(thresholds, sensitivity, gate_mode)
    if double_confirm is None:
        double_confirm = bool(config.load_settings().get("double_confirm", True))

    exceeded = [(k, sc[k], th[k]) for k in config.SENSOR_KEYS if sc[k] >= th[k]]
    kinds, levels = [], []
    for k, val, lim in exceeded:
        kind = SENSOR_TO_KIND.get(k, config.KIND_METAL)
        if kind not in kinds:
            kinds.append(kind)
            levels.append(val)
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    pairs = sorted(zip(kinds, levels), key=lambda kv: order.get(kv[0], 99))
    kinds = [p[0] for p in pairs]
    levels = [p[1] for p in pairs]

    if not exceeded:
        return {"result": "clear", "kinds": [], "levels": [], "level": "",
                "exceeded": [], "scores": sc, "thresholds": th,
                "detail": "无感应超阈值，放行"}

    # 双判据确认：≥2 路报 -> 红；只有 1 路报 -> 橙（复检兜底）
    if double_confirm and len(exceeded) >= 2:
        level = models.LEVEL_RED
        why = "双判据确认（%d 路同时超阈值）" % len(exceeded)
    elif not double_confirm:
        level = models.LEVEL_RED
        why = "单判据直报（已关闭双判据确认）"
    else:
        level = models.LEVEL_ORANGE
        why = "单路超阈值，需复检确认"

    detail = "%s：%s -> %s" % (
        "、".join(config.SENSOR_TEXT.get(k, k) for k, _v, _t in exceeded),
        "、".join("%s %.2f≥%.2f" % (config.SENSOR_TEXT.get(k, k), v, t)
                  for k, v, t in exceeded),
        ("%s（%s）" % (models.LEVEL_TEXT[level], why)))
    return {"result": "alarm", "kinds": kinds, "levels": levels, "level": level,
            "exceeded": [{"sensor": k, "value": round(v, 3), "threshold": round(t, 3)}
                         for k, v, t in exceeded],
            "scores": sc, "thresholds": th, "detail": detail}


# 第 2 层：过检事件接入 --------------------------------------------------------

def parse_line(line):
    """
    解析一行设备上报（JSON）。返回 dict；无法解析返回 None。
    兼容：sensors 内嵌 或 感应键平铺在顶层。
    """
    try:
        data = json.loads((line or "").strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sensors = data.get("sensors")
    if not isinstance(sensors, dict):
        sensors = {k: data.get(k, 0) for k in config.SENSOR_KEYS if k in data}
    data["sensors"] = normalize_scores(sensors)
    return data


def ingest(store, payload, source="real", device_code=None, channel_no=None,
           room_id=None, operator=None, link=None):
    """
    一次过检的完整处理：判据 -> 落过检记录 -> （报警时）派报警单 -> （可选）上报 03。
    返回 (PassEvent, Alarm|None)。link 为 linkage_bridge 模块（可注入以便自检）。
    """
    payload = dict(payload or {})
    sensors = payload.get("sensors") or payload
    dev_code = device_code or payload.get("device_code") or ""
    dev = store.get_device(code=dev_code) if dev_code else None
    sensitivity = float(dev.sensitivity) if dev else 1.0
    cfg = config.load_settings()
    res = decide(sensors, sensitivity=sensitivity,
                 double_confirm=cfg.get("double_confirm", True))

    ev = models.PassEvent(
        device_code=dev_code,
        channel_no=channel_no or payload.get("channel_no")
        or (dev.channel_no if dev else ""),
        room_id=room_id or payload.get("room_id") or "",
        candidate_id=payload.get("candidate_id") or "",
        candidate_name=payload.get("candidate_name") or "",
        seat_no=str(payload.get("seat_no") or ""),
        duration_ms=int(payload.get("duration_ms") or 0),
        operator=operator or payload.get("operator") or cfg.get("operator") or "",
        source=source, note=payload.get("note") or "",
        scores=res["scores"], result=res["result"], kinds=res["kinds"],
        levels=res["levels"])
    store.save_pass(ev)

    alarm = None
    if res["result"] == "alarm":
        alarm = models.Alarm(
            event_id=ev.event_id, device_code=ev.device_code,
            channel_no=ev.channel_no, room_id=ev.room_id,
            kind=(res["kinds"] or [config.KIND_METAL])[0], kinds=res["kinds"],
            level=res["level"], detail=res["detail"],
            candidate_id=ev.candidate_id, candidate_name=ev.candidate_name)
        store.save_alarm(alarm)
        store.log("安检报警", "%s %s %s" % (alarm.alarm_id, alarm.kinds_text(),
                                        alarm.level_text()),
                  ev.operator or "gate")
        # 实时上报 03 智能终端（默认关；开了才推，避免刷屏）
        if cfg.get("report_to_terminal"):
            bridge = link
            if bridge is None:
                try:
                    import linkage_bridge as bridge  # noqa: F401
                except Exception:
                    bridge = None
            if bridge is not None:
                try:
                    info = bridge.report_alarm(alarm, event=ev)
                    if info:
                        store.mark_alarm_reported(alarm.alarm_id, str(info)[:200])
                except Exception as e:                      # noqa: BLE001
                    store.log("上报终端失败", str(e)[:160], "gate")
    return ev, alarm


# ---------------- HTTP 行协议（真设备入口） ----------------
class _Handler(object):
    """http.server 的处理逻辑（抽出成普通类，便于自检里直接调用而不起服务）。"""

    def __init__(self, store, device_code, on_event=None, seq=None):
        self.store = store
        self.device_code = device_code
        self.on_event = on_event
        self.seq = seq if seq is not None else {"n": 0}

    def handle_pass(self, body_text):
        data = parse_line(body_text)
        if data is None:
            return 400, {"ok": False, "reason": "报文不是合法 JSON"}
        data.setdefault("device_code", self.device_code)
        ev, alarm = ingest(self.store, data, source="real",
                           device_code=data.get("device_code"))
        self.store.heartbeat(ev.device_code)
        self.seq["n"] += 1
        if self.on_event:
            try:
                self.on_event(ev, alarm)
            except Exception:
                pass
        return 200, {"ok": True, "event_id": ev.event_id, "result": ev.result,
                     "kinds": ev.kinds, "level": (alarm.level if alarm else ""),
                     "alarm_id": (alarm.alarm_id if alarm else ""),
                     "detail": (alarm.detail if alarm else "放行")}


class HttpIngest(object):
    """安检门 HTTP 接入：POST /api/pass 一行 JSON，GET /health 供联调探活。"""

    def __init__(self, store, device_code, host="0.0.0.0", port=9006, on_event=None):
        self.store = store
        self.device_code = device_code
        self.host = host
        self.port = int(port)
        self.on_event = on_event
        self._httpd = None
        self._thread = None
        self.handler = _Handler(store, device_code, on_event=on_event)
        self.started_at = ""

    def _build(self):
        import http.server
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):        # 不往 stderr 刷日志
                pass

            def handle_one_request(self):
                # 真设备常直接断连（拔线/超时），别把 ConnectionReset 打一屏 stderr
                try:
                    http.server.BaseHTTPRequestHandler.handle_one_request(self)
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    self.close_connection = True

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") in ("/health", ""):
                    self._send(200, {"ok": True, "device_code": outer.device_code,
                                     "passes": outer.handler.seq["n"],
                                     "source": "06_智能安检",
                                     "protocol": "POST /api/pass 一行 JSON"})
                else:
                    self._send(404, {"ok": False, "reason": "只支持 GET /health"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                    code, obj = outer.handler.handle_pass(body)
                except Exception as e:                       # noqa: BLE001
                    code, obj = 500, {"ok": False,
                                      "reason": "%s: %s" % (type(e).__name__, e)}
                self._send(code, obj)

        return H

    def start(self):
        import http.server
        self._httpd = http.server.ThreadingHTTPServer((self.host, self.port), self._build())
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.3}, daemon=True)
        self._thread.start()
        self.started_at = models.now_ts()
        self.store.heartbeat(self.device_code)
        return True

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        return True

    def status(self):
        return {"kind": "http", "running": self._httpd is not None,
                "listen": "http://%s:%d/api/pass" % (self.host, self.port),
                "passes": self.handler.seq["n"], "started_at": self.started_at}


# ---------------- TCP 行协议（真设备入口） ----------------
class TcpIngest(object):
    """安检门 TCP 接入：连上来丢一行 JSON 即可（老设备常见的 telnet 风格）。"""

    def __init__(self, store, device_code, host="0.0.0.0", port=9006, on_event=None):
        self.store = store
        self.device_code = device_code
        self.host = host
        self.port = int(port)
        self.on_event = on_event
        self._srv = None
        self._thread = None
        self.handler = _Handler(store, device_code, on_event=on_event)
        self.started_at = ""

    def _build(self):
        outer = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if line in ("health", "?"):
                        out = {"ok": True, "device_code": outer.device_code,
                               "passes": outer.handler.seq["n"]}
                    else:
                        _code, out = outer.handler.handle_pass(line)
                    try:
                        self.wfile.write((json.dumps(out, ensure_ascii=False) + "\n")
                                         .encode("utf-8"))
                    except OSError:
                        break

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        return S, H

    def start(self):
        S, H = self._build()
        self._srv = S((self.host, self.port), H)
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        kwargs={"poll_interval": 0.3}, daemon=True)
        self._thread.start()
        self.started_at = models.now_ts()
        self.store.heartbeat(self.device_code)
        return True

    def stop(self):
        if self._srv is not None:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None
        return True

    def status(self):
        return {"kind": "tcp", "running": self._srv is not None,
                "listen": "tcp://%s:%d（一行 JSON）" % (self.host, self.port),
                "passes": self.handler.seq["n"], "started_at": self.started_at}


# ---------------- 串口行协议 ----------------
class SerialIngest(object):
    """
    串口接入（需要 pyserial，**本模块不强制依赖**）。
    没装 pyserial 时 start() 直接返回 (False, 原因)，界面如实显示，
    不会假装"正在接收串口数据"。
    """

    def __init__(self, store, device_code, port="COM3", baudrate=9600, on_event=None):
        self.store = store
        self.device_code = device_code
        self.port = port
        self.baudrate = int(baudrate)
        self.on_event = on_event
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self.handler = _Handler(store, device_code, on_event=on_event)
        self.started_at = ""
        self.reason = ""

    def start(self):
        try:
            import serial                                 # noqa: F401
        except Exception:
            self.reason = "未安装 pyserial（pip install pyserial 后可用）"
            return False
        try:
            import serial
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
        except Exception as e:                            # noqa: BLE001
            self.reason = "串口打不开：%s" % str(e)[:120]
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.started_at = models.now_ts()
        self.store.heartbeat(self.device_code)
        return True

    def _loop(self):
        while not self._stop.is_set() and self._ser is not None:
            try:
                line = self._ser.readline().decode("utf-8", "replace").strip()
            except Exception as e:                        # noqa: BLE001
                self.reason = "串口读取失败：%s" % str(e)[:120]
                break
            if line:
                self.handler.handle_pass(line)

    def stop(self):
        self._stop.set()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        return True

    def status(self):
        return {"kind": "serial", "running": self._ser is not None,
                "listen": "%s@%d" % (self.port, self.baudrate),
                "passes": self.handler.seq["n"], "started_at": self.started_at,
                "reason": self.reason}


# ---------------- 内置模拟器（演示用，来源标 sim） ----------------
class SimGate(object):
    """
    模拟安检门：按样本库的感应特征随机造过检事件。

    **它造的数据 source 一律是 sim**，界面与报告都会写明，
    可用于演示/联调/演练，不能充当真实检出率证据（真实证据见「样本测试-现场实测」）。
    """

    def __init__(self, store, device_code, on_event=None, interval=3.0,
                 alarm_rate=0.18, seed=None, room_id=""):
        self.store = store
        self.device_code = device_code
        self.on_event = on_event
        self.interval = float(interval)
        self.alarm_rate = float(alarm_rate)
        self.room_id = room_id
        self.seed = seed
        self._stop = threading.Event()
        self._thread = None
        self.n = 0
        self.started_at = ""
        self._rnd = random.Random(seed) if seed is not None else random.Random()

    def _pick_profile(self):
        try:
            import samples
            profiles = samples.sim_profiles()
        except Exception:
            profiles = []
        if not profiles:
            # 兜底：直接造一组感应值
            if self._rnd.random() < self.alarm_rate:
                return {"name": "随机报警", "kind": config.KIND_MOBILE,
                        "scores": {"metal": 0.4, "phone": 0.8, "magnetic": 0.2,
                                   "earphone": 0.1, "millimeter": 0.3, "shoe": 0.05}}
            return {"name": "随机放行", "kind": "", "scores": {}}
        # 报警样本按 alarm_rate 抽，正常样本占其余
        alarms = [p for p in profiles if p.get("kind")]
        normals = [p for p in profiles if not p.get("kind")]
        if alarms and self._rnd.random() < self.alarm_rate:
            return self._rnd.choice(alarms)
        return self._rnd.choice(normals or profiles)

    def emit_once(self):
        prof = self._pick_profile()
        scores = dict(prof.get("scores") or {})
        # 加一点噪声，模拟同一部手机每次读数不完全一样
        for k in list(scores.keys()):
            scores[k] = max(0.0, min(1.0, scores[k] + self._rnd.uniform(-0.03, 0.03)))
        payload = {
            "device_code": self.device_code,
            "room_id": self.room_id or "1考场",
            "seat_no": "%02d" % (self._rnd.randint(1, 30)),
            "candidate_id": "SIM%06d" % (self.n + 1),
            "candidate_name": "模拟考生%03d" % (self.n + 1),
            "duration_ms": self._rnd.randint(600, 1400),
            "sensors": scores,
            "note": "内置模拟器（%s）" % prof.get("name", ""),
        }
        ev, alarm = ingest(self.store, payload, source="sim",
                           device_code=self.device_code)
        self.n += 1
        if self.on_event:
            try:
                self.on_event(ev, alarm)
            except Exception:
                pass
        return ev, alarm

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.emit_once()
                self.store.heartbeat(self.device_code)
            except Exception:
                pass
            self._stop.wait(max(0.2, self.interval))

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.started_at = models.now_ts()
        self.store.heartbeat(self.device_code)
        return True

    def stop(self):
        self._stop.set()
        return True

    def status(self):
        return {"kind": "sim", "running": self._thread is not None and self._thread.is_alive(),
                "listen": "内置模拟器，约 %.1f 秒/人次" % self.interval,
                "passes": self.n, "started_at": self.started_at,
                "note": "模拟数据，source=sim，不可作为真实检出证据"}


# ---------------- 接入管理器（界面按设备开关） ----------------
def parse_address(proto, address, default_port=9006):
    """把设备地址解析成 (host, port) 或串口名。"""
    addr = (address or "").strip()
    if proto == config.PROTO_SERIAL:
        return addr or "COM3", 9600
    port = default_port
    host = "0.0.0.0"
    if addr:
        body = addr.split("://", 1)[1] if "://" in addr else addr
        body = body.rstrip("/")
        if ":" in body:
            head, tail = body.rsplit(":", 1)
            host = head or host
            try:
                port = int(tail)
            except ValueError:
                pass
        else:
            host = body or host
    return host, port


class IngestManager(object):
    """统一管理各设备的接入线程：start(device) / stop(code) / status()。"""

    def __init__(self, store, on_event=None):
        self.store = store
        self.on_event = on_event
        self._runners = {}
        self._lock = threading.Lock()

    def start(self, device):
        """按设备的 proto 起接入；返回 (ok, 说明)。"""
        code = device.code
        self.stop(code)
        cfg = config.load_settings()
        proto = device.proto or config.PROTO_MANUAL
        try:
            if proto == config.PROTO_HTTP:
                host, port = parse_address(proto, device.address)
                runner = HttpIngest(self.store, code, host=host, port=port,
                                    on_event=self.on_event)
            elif proto == config.PROTO_TCP:
                host, port = parse_address(proto, device.address)
                runner = TcpIngest(self.store, code, host=host, port=port,
                                   on_event=self.on_event)
            elif proto == config.PROTO_SERIAL:
                port, baud = parse_address(proto, device.address)
                runner = SerialIngest(self.store, code, port=port, baudrate=baud,
                                      on_event=self.on_event)
            elif proto == config.PROTO_SIM:
                runner = SimGate(self.store, code, on_event=self.on_event,
                                 interval=cfg.get("sim_interval_sec", 3.0),
                                 alarm_rate=cfg.get("sim_alarm_rate", 0.18),
                                 room_id=device.room_id)
            else:
                return False, "该设备类型（%s）不需要接入线程，用人工录入" % proto
            ok = runner.start()
            if ok is False:
                return False, getattr(runner, "reason", "启动失败")
            with self._lock:
                self._runners[code] = runner
            self.store.heartbeat(code)
            self.store.log("启动接入", "%s %s %s" % (code, proto, device.address),
                           cfg.get("operator") or "system")
            return True, runner.status()
        except OSError as e:
            return False, "端口/地址不可用：%s" % str(e)[:120]
        except Exception as e:                            # noqa: BLE001
            return False, "%s: %s" % (type(e).__name__, str(e)[:120])

    def stop(self, code):
        with self._lock:
            runner = self._runners.pop(code, None)
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
            self.store.heartbeat(code, status="offline")
            return True
        return False

    def stop_all(self):
        for code in list(self._runners.keys()):
            self.stop(code)

    def status(self):
        with self._lock:
            return {code: r.status() for code, r in self._runners.items()}

    def running_codes(self):
        with self._lock:
            return sorted(self._runners.keys())


# ---------------- 真机探活（HTTP /health） ----------------
def probe_device(device, timeout=1.5):
    """
    探活一台设备：
      http  -> GET /health
      tcp   -> 连一下端口，问一行 health
      其它  -> 返回 (None, 说明)，因为无从探测（不假装在线）
    返回 (ok|None, 说明)
    """
    import urllib.error
    import urllib.request
    proto = (device.proto or "").lower()
    if proto == config.PROTO_HTTP:
        host, port = parse_address(proto, device.address)
        url = "http://%s:%d/health" % (host if host != "0.0.0.0" else "127.0.0.1", port)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return True, "健康检查通过：%s" % json.dumps(data, ensure_ascii=False)[:120]
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, "不可达：%s" % str(e)[:100]
    if proto == config.PROTO_TCP:
        host, port = parse_address(proto, device.address)
        try:
            with socket.create_connection((host if host != "0.0.0.0" else "127.0.0.1",
                                           port), timeout=timeout) as s:
                s.sendall(b"health\n")
                s.settimeout(timeout)
                data = s.recv(256).decode("utf-8", "replace").strip()
            return True, "端口可达，应答：%s" % data[:100]
        except OSError as e:
            return False, "不可达：%s" % str(e)[:100]
    if proto in (config.PROTO_SIM,):
        return True, "模拟器设备：无需探活（数据来源为 sim）"
    return None, "该接入方式（%s）不支持自动探活，请人工确认" % config.PROTO_TEXT.get(
        proto, proto)
