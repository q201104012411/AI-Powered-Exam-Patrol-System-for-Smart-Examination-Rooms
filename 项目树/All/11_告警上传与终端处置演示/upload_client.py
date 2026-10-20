# -*- coding: utf-8 -*-
r"""
02 -> 03 告警上传通道

职责边界（很重要）：
  · **上传**走各系统自己的 integration_api.py（子进程 + JSON），与
    00_集成联动/linkage.py 是同一条通道 —— 演示的是真实链路，不是另造一套。
  · **处置**不在这里做。03 的处置（确认 / 复核 / 留痕）由 demo_main.py 在
    03 自己的数据目录里就地调用 03 的模块完成 —— 见 disposition.py 的说明。

为什么上传必须用子进程：02 与 03 各自都有 config.py / models.py，
同进程 import 必然撞名；项目里 00_集成联动 也是因此选了子进程方案。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALL_DIR = os.path.dirname(HERE)

#: 02 与 03 的项目根目录（与 00_集成联动/linkage.py 的 PROJECTS 保持一致）
PROJECTS = {
    "02": os.path.join(ALL_DIR, "02_摄像头核查与视频质量诊断"),
    "03": os.path.join(ALL_DIR, "03_智能终端操作"),
}

#: 各系统重定向数据目录用的环境变量（沙箱隔离，不动真实运行数据）
HOME_ENV = {"02": "EXAM_CAMERA_HOME", "03": "EXAM_TERMINAL_HOME"}

#: 03 判定“红转橙”的画质可信度阈值，与 03/integration_api.py 的 DOWNGRADE_BELOW 一致
DOWNGRADE_BELOW = 0.6


class UploadError(RuntimeError):
    pass


class Project(object):
    """对某一套系统 integration_api.py 的调用句柄（子进程 + JSON，用完即退）。"""

    def __init__(self, key, python=None, home=None, timeout=90):
        if key not in PROJECTS:
            raise ValueError("未知系统：%s" % key)
        self.key = key
        self.dir = PROJECTS[key]
        self.python = python or sys.executable
        self.home = home
        self.timeout = timeout
        self.script = os.path.join(self.dir, "integration_api.py")

    def available(self):
        if not os.path.isdir(self.dir):
            return False, "目录不存在：%s" % self.dir
        if not os.path.isfile(self.script):
            return False, "缺少 integration_api.py"
        return True, "可用"

    def call(self, op, args=None):
        ok, why = self.available()
        if not ok:
            raise UploadError("[%s] %s" % (self.key, why))

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if self.home:
            os.makedirs(self.home, exist_ok=True)
            env[HOME_ENV[self.key]] = self.home

        cmd = [self.python, self.script, "--op", op,
               "--args", json.dumps(args or {}, ensure_ascii=False)]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "env": env, "cwd": self.dir}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, timeout=self.timeout, **kwargs)
        except subprocess.TimeoutExpired:
            raise UploadError("[%s] %s 超时（%ds）" % (self.key, op, self.timeout))

        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        line = ""
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                line = ln.strip()
                break
        if not line:
            raise UploadError("[%s] %s 无有效输出（返回码 %s）：%s"
                              % (self.key, op, proc.returncode, (err or out)[:300]))
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise UploadError("[%s] %s 输出无法解析：%s" % (self.key, op, exc))
        if not data.get("ok", True) and data.get("reason"):
            data["_error"] = data["reason"]
        return data


class CameraLink(object):
    """02 侧：登记机位、诊断画面、查画质可信度。"""

    def __init__(self, python=None, home=None, timeout=90):
        self.p = Project("02", python=python, home=home, timeout=timeout)

    def ensure_device(self, code, name="", room_id="", site="", position="",
                      url="", fov_deg=90, operator="demo"):
        """登记机位（已存在则更新）；沙箱里没有机位时先造一个。"""
        return self.p.call("save_device", {
            "code": code, "name": name or code, "room_id": room_id, "site": site,
            "position": position, "url": url, "fov_deg": fov_deg, "operator": operator})

    def inspect(self, image, device_code="", device_id="", operator="demo"):
        """
        02 诊断一张画面 —— “02 的结论”来自这里，不是演示脚本写死的。
        返回 defects / factor（画质可信度）/ result_ok。
        """
        return self.p.call("inspect", {"image": image, "device_code": device_code,
                                       "device_id": device_id, "operator": operator})

    def credibility(self, device_code):
        """该机位当前告警可信度：有 fail -> ×0.5，仅 warn -> ×0.8，正常 -> ×1.0。"""
        return self.p.call("credibility", {"device_code": device_code})

    def baseline(self, device_code, image, operator="demo"):
        return self.p.call("baseline", {"device_code": device_code, "image": image,
                                        "operator": operator})

    def devices(self):
        return self.p.call("devices", {})

    def status(self):
        return self.p.call("status", {})


class TerminalLink(object):
    """03 侧：接收上传的告警。处置不在这个类里，见 disposition.py。"""

    def __init__(self, python=None, home=None, timeout=90):
        self.p = Project("03", python=python, home=home, timeout=timeout)

    def upload_alert(self, code, device_id="", room_id="", seat_no="", detail="",
                     candidate_id="", candidate_name="", credibility=None,
                     level=None, operator="demo/02", source="02_摄像头核查与视频质量诊断"):
        """
        上传一条告警到 03 终端。

        credibility 给定时交给 03 决定是否降权：03 收到 credibility < 0.6 的
        **红色**告警会自动降为橙色并标注 —— 这是 02/03 既定联动约定
        （防画质问题堆成误报），不是演示脚本另加的规则。
        """
        args = {"code": code, "device_id": device_id, "room_id": room_id,
                "seat_no": seat_no, "detail": detail, "level": level,
                "candidate_id": candidate_id, "candidate_name": candidate_name,
                "operator": operator, "source": source}
        if credibility is not None:
            args["credibility"] = float(credibility)
        return self.p.call("ingest_alert", args)

    def list_alerts(self, limit=20):
        return self.p.call("list_alerts", {"limit": limit})

    def status(self):
        return self.p.call("status", {})


# =====================================================================
#  真局域网模式：02 机上把告警 **经 HTTP** 送到 03 的接入服务（默认 9113）
# =====================================================================
#  同机（子进程）模式与局域网模式的区别：
#    · 同机模式：upload_client 在**本机**以子进程调 03 的 integration_api，
#      适合单机演示与自检；
#    · 局域网模式：02 机把告警 POST 到 03 的 /api/ingest_alert，
#      走 X-Exam-Token 鉴权，**这才是两台机器部署时的真实链路**。
#      03 收到后走的是同一条分级流水线（画质降权、60s 强制确认都一样），
#      因此两种模式在 03 侧的处置结果一致。
#
#  03 侧准备：
#    1) 启动 03\启动局域网接入服务.bat，监听地址填 0.0.0.0，端口 9113
#    2) 记下 --print-token 打印的令牌，配到 02 机的 EXAM_TERMINAL_TOKEN
#    3) Windows 防火墙放行 9113

LAN_TOKEN_HEADER = "X-Exam-Token"


def lan_default_url():
    return (os.environ.get("EXAM_TERMINAL_URL") or "").strip().rstrip("/")


def lan_default_token():
    return (os.environ.get("EXAM_TERMINAL_TOKEN") or "").strip()


class LanTerminalLink(object):
    """03 侧（**跨机器**）：经 HTTP 把告警送到 03 的局域网接入服务。"""

    def __init__(self, url="", token="", timeout=15):
        self.url = (url or lan_default_url()).rstrip("/")
        self.token = token or lan_default_token()
        self.timeout = float(timeout)
        self.last_error = ""

    def available(self):
        """检查 03 的接入服务是否可达（/health 只需回环或正确令牌）"""
        if not self.url:
            return False, "未配置 03 的地址（EXAM_TERMINAL_URL 或 --url）"
        ok, data = self._http("/health")
        if not ok:
            return False, data.get("reason") or "不可达"
        return True, "可达（%s）" % (data.get("system") or data.get("service") or self.url)

    def _http(self, path, payload=None):
        import urllib.error
        import urllib.request
        headers = {"Accept": "application/json"}
        if self.token:
            headers[LAN_TOKEN_HEADER] = self.token
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return True, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                d = json.loads(body)
            except ValueError:
                d = {"reason": "HTTP %s %s" % (e.code, body[:160])}
            self.last_error = d.get("reason") or ("HTTP %s" % e.code)
            return False, d
        except Exception as e:
            self.last_error = str(e)
            return False, {"ok": False, "reason": "请求失败：%s" % e}

    def upload_alert(self, code, device_id="", room_id="", seat_no="", detail="",
                     candidate_id="", candidate_name="", credibility=None,
                     level=None, operator="02", source="02_摄像头核查与视频质量诊断"):
        """与 TerminalLink.upload_alert 同签名，便于演示脚本无缝切换两种模式。"""
        payload = {"code": code, "device_id": device_id, "room_id": room_id,
                   "seat_no": seat_no, "detail": detail, "level": level,
                   "candidate_id": candidate_id, "candidate_name": candidate_name,
                   "operator": operator, "source": source}
        if credibility is not None:
            payload["credibility"] = float(credibility)
        ok, data = self._http("/api/ingest_alert", payload)
        if not ok:
            data = dict(data or {})
            data.setdefault("ok", False)
            data["_error"] = data.get("reason") or self.last_error or "上传失败"
        return data

    def list_alerts(self, limit=20):
        ok, data = self._http("/api/alerts?limit=%d" % int(limit))
        return data if ok else {"ok": False, "reason": data.get("reason")}

    def status(self):
        ok, data = self._http("/api/status")
        return data if ok else {"ok": False, "reason": data.get("reason")}


def make_terminal_link(mode="auto", python=None, home=None, url="", token="",
                       timeout=90):
    """
    按模式返回 03 侧链路对象。

    mode:
      "local"  -> TerminalLink（同机子进程，单机演示/自检用）
      "lan"    -> LanTerminalLink（真局域网 HTTP，两台机器部署用）
      "auto"   -> 配了 EXAM_TERMINAL_URL 就用 lan，否则 local
    """
    m = (mode or "auto").lower()
    if m == "auto":
        m = "lan" if (url or lan_default_url()) else "local"
    if m == "lan":
        return LanTerminalLink(url=url, token=token,
                               timeout=min(float(timeout), 30.0))
    return TerminalLink(python=python, home=home, timeout=timeout)
