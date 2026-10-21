# -*- coding: utf-8 -*-
r"""
robot_link.py —— 巡考小车通信模块（Windows 端 · 纯标准库 · 不依赖 ROS）

小车端运行的是 ``巡考机器人/01_代码与程序/webui/robot_web.py``（默认 0.0.0.0:8080）。
Windows 端只需要一根网线/WiFi 和这个模块，就可以通过 HTTP 控制小车：

  · 输入 IP 连接：192.168.1.50 / 192.168.1.50:8080 / http://192.168.1.50:8080
  · 遥测：电量、里程计位姿、IMU 姿态、四向雷达距离、指令、巡线状态
  · 控制：速度指令（小车 0.6s 看门狗，松手自动停）、停车、急停、一键巡线
  · 画面：按需拉取 /api/snap?cam=astra|depth|usb（JPEG）
  · 资料：远端代码/配置浏览、巡更照片列表与下载
  · 参数：巡线参数读取/写入（/api/params）

本模块只负责"协议 + 线程"，不碰 Tk，方便单独测试（见 自检.py）。

线程模型（GUI 里这样用，主线程永不阻塞）：
    link    = RobotLink("192.168.1.50:8080")
    poller  = Poller(link, interval=0.3)          # 5Hz 拉 /api/state
    sender  = CommandSender(link, keepalive=0.2)  # 10Hz 发 /api/cmd
    poller.start(); sender.start()
    sender.set(v, w)                              # 松手 set(0, 0)
    poller.read()                                 # 任意线程读取最新状态
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

DEFAULT_PORT = 8080
DEMO_PORT = 18080                     # robot_sim.py 本机演示默认端口
USER_AGENT = "ExamPatrolRobot/1.0 (Windows)"

# 小车端 robot_web.py 的 DEFAULT_PARAMS（参数面板用，保持逐字一致）
LINE_PARAMS = {
    "line_color": "black", "speed": 0.18, "kp": 0.85, "kd": 0.18,
    "deadband": 0.07, "max_angular": 0.50, "v_min": 0.035,
    "search_speed": 0.05, "search_time": 10.0,
    "v_thresh": 75.0, "s_thresh": 90.0,
    "dir_weight": 0.75, "lock_dist": 0.30, "approach_dist": 0.80,
    "approach_gain": 0.05,
    "coast_time": 3.0, "full_lock_err": 0.45, "over_steer": 0.65,
}


class LinkError(Exception):
    """与小车通信失败（超时 / 拒连 / 非小车服务 / 接口报错）。"""


class Target(object):
    """小车地址：host:port。"""

    def __init__(self, host, port=DEFAULT_PORT):
        self.host = str(host)
        self.port = int(port)

    @property
    def base(self):
        return "http://%s:%d" % (self.host, self.port)

    @property
    def web_url(self):
        return self.base + "/"

    def __str__(self):
        return "%s:%d" % (self.host, self.port)

    def __repr__(self):
        return "Target(%r, %d)" % (self.host, self.port)

    def __eq__(self, other):
        return (isinstance(other, Target) and self.host == other.host
                and self.port == other.port)


def parse_target(text, default_port=DEFAULT_PORT):
    """解析用户输入的小车地址。

    支持：``192.168.1.50``、``192.168.1.50:8080``、
    ``http://192.168.1.50:8080/``、``robot.local``。
    """
    if isinstance(text, Target):
        return text
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请输入小车 IP，例如 192.168.1.50")
    try:
        default_port = _parse_port(default_port)
    except ValueError:
        default_port = DEFAULT_PORT

    scheme = ""
    if "://" in raw:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError("地址格式不支持：%s（只支持 http://）" % raw)
        hostport = parts.netloc or parts.path
        path = parts.path
    else:
        hostport = raw.split("/", 1)[0]
        path = raw[len(hostport):]

    if path and path not in ("/", ""):
        raise ValueError("请只填 IP 和端口，不要带路径：%s" % raw)
    if not hostport:
        raise ValueError("地址为空，请输入小车 IP")

    host, port = hostport, int(default_port)
    if hostport.startswith("["):                       # [IPv6]:port
        end = hostport.find("]")
        if end < 0:
            raise ValueError("IPv6 地址缺少右括号：%s" % hostport)
        raise ValueError("暂不支持 IPv6，请填 IPv4 地址，例如 192.168.1.50")
    elif hostport.count(":") == 1:
        host, port_text = hostport.rsplit(":", 1)
        port = _parse_port(port_text)
    elif hostport.count(":") > 1:
        raise ValueError("地址格式不正确（IPv6 暂不支持）：%s" % hostport)

    host = host.strip()
    if not host:
        raise ValueError("IP 为空，请输入小车 IP")
    if any(ch.isspace() for ch in host):
        raise ValueError("IP 中不能有空格：%s" % host)
    if scheme == "https":
        raise ValueError("小车服务是 http，请不要用 https://")

    return Target(host, port)


def _parse_port(text):
    try:
        port = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError("端口必须是数字：%s" % text)
    if not 1 <= port <= 65535:
        raise ValueError("端口超出范围：%d" % port)
    return port


def format_target(target):
    if isinstance(target, Target):
        return str(target)
    return str(target)


def local_ipv4():
    """本机局域网 IPv4（不实际发包）。"""
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


def is_robot_state(data):
    """判断响应是否像 robot_web.py 的 /api/state。"""
    if not isinstance(data, dict):
        return False
    if data.get("ok") != 1:
        return False
    return ("clear" in data or "limits" in data or "odom" in data) \
        and "line" in data


class RobotLink(object):
    """robot_web.py HTTP 客户端（线程安全：每次请求独立连接）。"""

    def __init__(self, target, timeout=2.0):
        self.target = parse_target(target) if not isinstance(target, Target) else target
        self.timeout = float(timeout)

    # ------------------------------------------------------------ 基础
    @property
    def host(self):
        return self.target.host

    @property
    def port(self):
        return self.target.port

    @property
    def base(self):
        return self.target.base

    def _request(self, method, path, params=None, body=None, timeout=None):
        timeout = self.timeout if timeout is None else float(timeout)
        url = path
        if params:
            url += "?" + urlencode(params, doseq=True)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, image/jpeg, */*",
            "Cache-Control": "no-store",
            "Connection": "close",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        conn = None
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            conn.request(method, url, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            ctype = resp.getheader("Content-Type", "") or ""
            reason = resp.reason
        except socket.timeout:
            raise LinkError("连接超时（%s，%.1fs）——检查是否同一网段、小车是否开机"
                            % (self.target, timeout))
        except ConnectionRefusedError:
            raise LinkError("端口未开放（%s 拒绝连接）——小车端是否已运行 "
                            "webui/robot_web.py？" % self.target)
        except socket.gaierror:
            raise LinkError("地址无法解析：%s" % self.host)
        except OSError as exc:
            raise LinkError("网络错误（%s）：%s" % (self.target, exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                            # noqa: BLE001
                    pass

        if status >= 400:
            msg = _error_text(data, status, reason)
            raise LinkError(msg)
        return data, status, ctype

    def _json(self, method, path, params=None, body=None, timeout=None):
        data, status, ctype = self._request(method, path, params, body, timeout)
        if not data:
            raise LinkError("小车返回空响应（%s）" % path)
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise LinkError("响应不是 JSON（%s）——该地址可能不是巡考小车服务" % path)

    # ------------------------------------------------------------ 接口
    def state(self):
        return self._json("GET", "/api/state")

    def probe(self):
        """连接探测：返回 (state, 延迟毫秒)，并校验是小车服务。"""
        t0 = time.time()
        data = self.state()
        latency = (time.time() - t0) * 1000.0
        if not is_robot_state(data):
            raise LinkError("该地址有 HTTP 服务，但不是巡考小车控制接口")
        return data, latency

    def cmd(self, v, w):
        return self._json("GET", "/api/cmd", {"v": "%.3f" % float(v),
                                              "w": "%.3f" % float(w)})

    def stop(self):
        return self._json("GET", "/api/stop")

    def estop(self, on=True):
        return self._json("GET", "/api/estop", {"on": "1" if on else "0"})

    def line(self, on=True, color=None):
        params = {"on": "1" if on else "0"}
        if color:
            params["color"] = color
        return self._json("GET", "/api/line", params, timeout=20)

    def line_log(self):
        return self._json("GET", "/api/line/log")

    def params(self):
        return self._json("GET", "/api/params")

    def save_params(self, params):
        return self._json("POST", "/api/params", body={"params": params})

    def set_camsource(self, src):
        """切相机源（小车端要重启相机，可能阻塞 10~15 秒）。"""
        return self._json("GET", "/api/camsource", {"src": src}, timeout=45)

    def snap(self, cam="astra"):
        """取一张 JPEG 快照；相机没启用时抛 LinkError。"""
        data, status, ctype = self._request("GET", "/api/snap",
                                            {"cam": cam, "_": int(time.time() * 1000)},
                                            timeout=max(3.0, self.timeout))
        if not data:
            raise LinkError("相机返回空画面")
        return data

    def photo(self, cam="usb", note=""):
        return self._json("GET", "/api/photo", {"cam": cam, "note": note}, timeout=10)

    def photos(self):
        return self._json("GET", "/api/photos")

    def photo_bytes(self, name):
        data, status, ctype = self._request("GET", "/api/photo/get",
                                            {"name": name}, timeout=8)
        return data

    def files(self):
        return self._json("GET", "/api/files", timeout=6)

    def file(self, rel):
        return self._json("GET", "/api/file", {"path": rel}, timeout=8)

    def save_file(self, rel, code):
        return self._json("POST", "/api/file", body={"path": rel, "code": code},
                          timeout=10)

    def index_url(self):
        return self.target.web_url


def _error_text(data, status, reason):
    """把小车端 JSON 错误转成一句人话。"""
    detail = ""
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
        if isinstance(obj, dict):
            detail = str(obj.get("err") or "")
            more = obj.get("detail")
            if more:
                detail += " ｜ " + str(more)[-300:]
    except Exception:                                        # noqa: BLE001
        try:
            detail = data.decode("utf-8", "replace")[:200]
        except Exception:                                    # noqa: BLE001
            detail = ""
    msg = "小车接口返回 %s %s" % (status, reason or "")
    if detail:
        msg += "：" + detail.strip()
    return msg.strip()


# ======================================================================
# 后台轮询
# ======================================================================
class Poller(threading.Thread):
    """5Hz 轮询 /api/state，断线自动重试；read() 可在任意线程调用。"""

    def __init__(self, link, interval=0.3):
        threading.Thread.__init__(self, name="robot-poller")
        self.daemon = True
        self.link = link
        self.interval = max(0.1, float(interval))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = None
        self._ok = False
        self._error = ""
        self._latency = 0.0
        self._ts = 0.0
        self._fails = 0

    def run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                data = self.link.state()
                latency = (time.time() - t0) * 1000.0
                if not is_robot_state(data):
                    raise LinkError("该地址不是巡考小车控制接口")
                with self._lock:
                    self._state = data
                    self._ok = True
                    self._error = ""
                    self._latency = latency
                    self._ts = time.time()
                    self._fails = 0
            except Exception as exc:                         # noqa: BLE001
                with self._lock:
                    self._ok = False
                    self._error = str(exc)
                    self._fails += 1
                    self._ts = time.time()
            self._stop.wait(self.interval)

    def read(self):
        with self._lock:
            return {
                "state": self._state,
                "ok": self._ok,
                "error": self._error,
                "latency": self._latency,
                "ts": self._ts,
                "fails": self._fails,
            }

    def stop(self):
        self._stop.set()


# ======================================================================
# 后台指令
# ======================================================================
class CommandSender(threading.Thread):
    """按最新目标速度持续发 /api/cmd，松手自动归零（配合小车看门狗）。"""

    def __init__(self, link, keepalive=0.2):
        threading.Thread.__init__(self, name="robot-cmd")
        self.daemon = True
        self.link = link
        self.keepalive = max(0.1, float(keepalive))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._target = (0.0, 0.0)
        self._last = (0.0, 0.0)
        self._last_t = 0.0
        self._error = ""
        self._error_t = 0.0
        self._sent = 0

    def set(self, v, w):
        with self._lock:
            self._target = (float(v), float(w))

    def target(self):
        with self._lock:
            return self._target

    def last_error(self):
        with self._lock:
            return self._error, self._error_t

    def run(self):
        while not self._stop.is_set():
            with self._lock:
                target = self._target
                last = self._last
            now = time.time()
            need = False
            if target != (0.0, 0.0):
                if target != last or (now - self._last_t) >= self.keepalive:
                    need = True
            elif last != (0.0, 0.0):
                need = True                       # 松手：补一帧零速立即停车

            if need:
                try:
                    self.link.cmd(target[0], target[1])
                    with self._lock:
                        self._last = target
                        self._last_t = time.time()
                        self._error = ""
                        self._sent += 1
                except Exception as exc:                 # noqa: BLE001
                    with self._lock:
                        self._error = str(exc)
                        self._error_t = time.time()
                    self._stop.wait(0.6)                 # 断线时别刷屏
                    continue
            self._stop.wait(0.05)

    def sent_count(self):
        with self._lock:
            return self._sent

    def stop(self):
        self._stop.set()


# ======================================================================
# 局域网发现
# ======================================================================
def candidate_hosts(extra_prefixes=()):
    """本机所在 /24 + 回环；可追加 192.168.1 / 192.168.0 等常用网段。"""
    ip = local_ipv4()
    prefixes = []
    if ip and ip.count(".") == 3 and not ip.startswith("127."):
        prefixes.append(ip.rsplit(".", 1)[0])
    prefixes.extend(extra_prefixes)
    seen = set()
    hosts = ["127.0.0.1"]
    for prefix in prefixes:
        for i in range(1, 255):
            h = "%s.%d" % (prefix, i)
            if h not in seen:
                seen.add(h)
                hosts.append(h)
    return hosts


def scan_lan(port=DEFAULT_PORT, timeout=0.45, workers=64, stop_event=None,
             on_found=None, extra_prefixes=()):
    """扫描局域网里的 robot_web 小车，返回 [{host, port, state, latency}]。"""
    hosts = candidate_hosts(extra_prefixes)
    found = []

    def probe_one(host):
        if stop_event is not None and stop_event.is_set():
            return
        link = RobotLink(Target(host, port), timeout=timeout)
        try:
            state, latency = link.probe()
        except Exception:                                    # noqa: BLE001
            return
        item = {"host": host, "port": port, "state": state,
                "latency": latency}
        found.append(item)
        if on_found is not None:
            try:
                on_found(item)
            except Exception:                                # noqa: BLE001
                pass

    with ThreadPoolExecutor(max_workers=max(4, int(workers))) as pool:
        list(pool.map(probe_one, hosts))
    found.sort(key=lambda x: tuple(int(p) for p in x["host"].split("."))
               if x["host"].count(".") == 3 else (999, 999, 999, 999))
    return found


# ======================================================================
# 客户端配置（记住上次 IP / 速度 / 画面来源）
# ======================================================================
CONFIG_KEYS = ("host", "port", "linear", "angular", "cam", "fps",
               "auto_reconnect", "auto_connect", "history")


def config_dir():
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    path = os.path.join(base, "ExamPatrolRobot")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.expanduser("~")
    return path


def config_path():
    return os.path.join(config_dir(), "client.json")


def default_config():
    return {
        "host": "",
        "port": DEFAULT_PORT,
        "linear": 0.15,
        "angular": 0.45,
        "cam": "astra",
        "fps": 4,
        "auto_reconnect": True,
        "auto_connect": False,
        "history": [],
    }


def load_config():
    cfg = default_config()
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in CONFIG_KEYS:
                if key in data:
                    cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    # 修正明显不合法的值
    try:
        cfg["port"] = _parse_port(cfg.get("port"))
    except ValueError:
        cfg["port"] = DEFAULT_PORT
    try:
        cfg["linear"] = min(0.20, max(0.03, float(cfg.get("linear"))))
        cfg["angular"] = min(0.60, max(0.10, float(cfg.get("angular"))))
    except (TypeError, ValueError):
        cfg["linear"], cfg["angular"] = 0.15, 0.45
    if cfg.get("cam") not in ("astra", "usb", "depth"):
        cfg["cam"] = "astra"
    try:
        cfg["fps"] = min(10, max(1, int(cfg.get("fps"))))
    except (TypeError, ValueError):
        cfg["fps"] = 4
    hist = cfg.get("history")
    cfg["history"] = [str(h) for h in hist][:10] if isinstance(hist, list) else []
    return cfg


def save_config(cfg):
    """合并写入（失败静默：配置不是关键路径）。"""
    data = load_config()
    for key in CONFIG_KEYS:
        if key in cfg:
            data[key] = cfg[key]
    path = config_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def remember_host(host, port, history=None):
    """把成功连接的地址放进历史列表（最多 8 条）。"""
    cfg = load_config()
    item = "%s:%d" % (host, int(port))
    items = [item]
    for old in (history if history is not None else cfg.get("history", [])):
        if old and old != item:
            items.append(str(old))
    cfg["host"] = host
    cfg["port"] = int(port)
    cfg["history"] = items[:8]
    save_config(cfg)
    return cfg["history"]
