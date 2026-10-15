# -*- coding: utf-8 -*-
"""
00 集成联动 · 多套系统互相调用

  01 人脸识别与身份核对   ->  who（身份是不是本人）
  02 摄像头核查与视频质量诊断 -> whether（画面可不可信）
  03 智能终端操作         ->  what to do（告警怎么处置、怎么留痕）
  06 智能安检与手机作弊器材检测 -> block（手机/无线耳机/金属器材挡在门外）

隔离方式：各套各有一个 integration_api.py，本模块用**子进程 + JSON** 调用它们。
（都有 config.py / models.py，同进程 import 必然撞名，子进程最稳。）

**局域网（跨设备）**：设备 A 跑 01/02（+00）、设备 B 跑 03 时，用
`Linkage(terminal_url="http://<设备B的IP>:9113", terminal_token="...")`
把 p03 换成 `RemoteTerminal`（走 `03_智能终端操作\\lan_service.py`），
上层编排逻辑一行都不用改；01/02 的操作记录另有 `01/02\\push_oplog.py` 持续上报。

联动链（本模块把这条链真正跑通）：
  ① 02 巡检/抽查发现机位画质异常
  ② 02 给出该机位的告警可信度系数 credibility_factor
  ③ 03 接入告警时按该系数降权；系数 < 0.6 时红色预警自动降级为橙色（防误报堆积）
  ④ 01 做人脸核验；若该机位画质不可信，核验结论也标注"画质降权"
  ⑤ 处置结果（复核结论）回流 03 留痕，画质问题由 02 的工单闭环
  ⑥ 06 安检门一次过检 -> 判据引擎 -> 报警；报警经本模块进 03（手机/耳机走
     PHONE_DETECTED 红色，金属/强磁走 SECURITY_ALARM 橙色），复检结论在 06 留痕
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

ALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROJECTS = {
    "01": os.path.join(ALL_DIR, "01_人脸识别与身份核对", "01_人脸识别与身份核对"),
    "02": os.path.join(ALL_DIR, "02_摄像头核查与视频质量诊断"),
    "03": os.path.join(ALL_DIR, "03_智能终端操作"),
    "06": os.path.join(ALL_DIR, "06_智能安检与手机作弊器材检测"),
}


class LinkageError(Exception):
    pass


def kill_tree(proc, grace=5.0):
    """
    杀掉子进程**及其整个进程树**。

    为什么必须杀树：跨项目调用是 `python integration_api.py`，它内部可能再起
    ollama / 取流等孙进程。只 kill 父进程的话，孙进程会占着 stdout/stderr 管道不放，
    父进程的收输出就会一直等下去 —— 这正是旧版"超时了还卡着"的根因。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass


def _pump(stream, sink):
    """把管道内容读进 sink（bytes 列表）。读不到就结束，绝不在主管道里阻塞调用方。"""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            sink.append(chunk)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class Project(object):
    """对某一套系统的调用句柄（子进程 + JSON）"""

    # 各套的数据目录重定向环境变量（用于"沙箱自检"，不污染真实数据）
    HOME_ENV = {"01": "EXAM_HOME", "02": "EXAM_CAMERA_HOME", "03": "EXAM_TERMINAL_HOME",
                "06": "EXAM_SEC_HOME"}

    # 单次子进程调用的默认上限：旧版 180 秒，只要对端卡在取流/网络等待上，
    # 调用方（界面/巡检）就会跟着"沉默"三分钟，看起来就是卡死。
    DEFAULT_TIMEOUT = 60

    def __init__(self, key, python=None, timeout=DEFAULT_TIMEOUT, home=None,
                 env_extra=None):
        self.key = key
        self.dir = PROJECTS[key]
        self.python = python or sys.executable
        self.timeout = timeout
        self.script = os.path.join(self.dir, "integration_api.py")
        self.home = home                      # None = 用各套真实数据目录
        # 其它系统的沙箱重定向也要跟着传下去：跨系统调用是**嵌套**的
        # （例如 06 收到报警后会自己再调 03），只传自己那一个变量的话，
        # 嵌套调用就会漏回真实数据目录 —— 沙箱自检会污染真实运行数据。
        self.env_extra = {k: v for k, v in (env_extra or {}).items() if v}

    def available(self):
        if not os.path.isdir(self.dir):
            return False, "目录不存在：%s" % self.dir
        if not os.path.isfile(self.script):
            return False, "缺少 integration_api.py"
        return True, "可用"

    def call(self, op, args=None):
        ok, why = self.available()
        if not ok:
            raise LinkageError("[%s] %s" % (self.key, why))
        env = dict(os.environ)
        env.update(self.env_extra)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.home:
            os.makedirs(self.home, exist_ok=True)
            env[self.HOME_ENV[self.key]] = self.home
        cmd = [self.python, self.script, "--op", op,
               "--args", json.dumps(args or {}, ensure_ascii=False)]
        try:
            popen_kwargs = {"stdin": subprocess.DEVNULL,
                            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                            "env": env, "cwd": self.dir}
            if os.name == "nt":
                # 禁止在 02/03 GUI 调用时弹出黑色命令行窗口；并新建进程组便于整树终止
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                popen_kwargs["start_new_session"] = True
            p = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            raise LinkageError("[%s] 无法启动子进程：%s" % (self.key, e))

        # 双线程收输出：绝不用 communicate() —— 孙进程占着管道不放时它会一直等
        out_chunks, err_chunks = [], []
        t_out = threading.Thread(target=_pump, args=(p.stdout, out_chunks), daemon=True)
        t_err = threading.Thread(target=_pump, args=(p.stderr, err_chunks), daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        started = time.time()
        try:
            p.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(p)                     # 超时 -> 连同孙进程一起终止，避免残留卡住后续调用
        t_out.join(timeout=3)
        t_err.join(timeout=3)

        out = b"".join(out_chunks).decode("utf-8", "replace").strip()
        err = b"".join(err_chunks).decode("utf-8", "replace").strip()
        if timed_out:
            raise LinkageError("[%s] 调用 %s 超时（%ds，已强制终止进程树）"
                               % (self.key, op, self.timeout))
        line = ""
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                line = ln.strip()
                break
        if not line:
            raise LinkageError("[%s] %s 无有效输出（返回码 %s，耗时 %.1fs）：%s"
                               % (self.key, op, p.returncode, time.time() - started,
                                  err[:300]))
        try:
            data = json.loads(line)
        except ValueError:
            raise LinkageError("[%s] %s 输出无法解析：%s" % (self.key, op, line[:200]))
        if not data.get("ok", True) and data.get("reason"):
            data["_error"] = data["reason"]
        return data


class RemoteTerminal(object):
    """
    把"03 智能终端跑在**另一台设备**上"这件事接进联动总线（局域网联动）。

    背景：Project 是本机的子进程句柄。设备 A 跑 01/02（+00），设备 B 跑 03 时，
    p03 换成这个对象即可 —— 上层 Linkage 的编排逻辑一行都不用改（同一套 op + JSON）。

    只走 03 接入服务开放的那些口子，**不多开一个**：
      ingest_alert   写：告警接入（复用 03 的分级/降权/去重/强制确认）
      status         读：终端状态
      list_alerts    读：告警列表
      list_remote    读：远端记录（01/02 送上来的操作记录与核验/核查结论）
    复核、远程擦除、命令控制台**不经网络**，所以这里没有对应 op —— 这不是遗漏。

    令牌与地址：
      Linkage(terminal_url="http://192.168.1.20:9113", terminal_token="...")
      也可用环境变量 EXAM_TERMINAL_URL / EXAM_TERMINAL_TOKEN。
    """

    # op -> (HTTP 方法, 路径, 是否用 args 组装查询串)
    ROUTES = {
        "ingest_alert": ("POST", "/api/ingest_alert", False),
        "status": ("GET", "/api/status", False),
        "list_alerts": ("GET", "/api/alerts", True),
        "list_remote": ("GET", "/api/records", True),
    }

    def __init__(self, url, token="", timeout=10):
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = float(timeout)
        self.dir = self.url          # 与 Project 同形：状态展示时会读它

    def available(self):
        if not self.url:
            return False, "未配置终端接入地址（EXAM_TERMINAL_URL）"
        return True, "远端接入：%s" % self.url

    def call(self, op, args=None):
        import urllib.error
        import urllib.parse
        import urllib.request
        if op not in self.ROUTES:
            raise LinkageError("[03-远端] 接入服务不提供 op=%s（写入口只开放告警接入）" % op)
        method, path, use_query = self.ROUTES[op]
        args = dict(args or {})
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Exam-Token"] = self.token
        if method == "GET" and use_query and args:
            path = path + "?" + urllib.parse.urlencode(
                {k: v for k, v in args.items() if v not in (None, "")})
        data = None
        if method == "POST":
            data = json.dumps(args, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except (ValueError, TypeError):
                out = {"ok": False, "reason": "HTTP %s" % getattr(e, "code", "?")}
            out.setdefault("ok", False)
            out.setdefault("_error", out.get("reason"))
            return out
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LinkageError("[03-远端] 无法连接 %s：%s" % (self.url, str(e)[:160]))
        if not isinstance(out, dict):
            raise LinkageError("[03-远端] %s 返回的不是 JSON 对象" % op)
        if not out.get("ok", True) and out.get("reason"):
            out["_error"] = out["reason"]
        return out


class Linkage(object):
    """三套系统的联动总线"""

    def __init__(self, python=None, sandbox_root=None, sandbox_keys=("02", "03"),
                 sandbox_01=False, timeout=Project.DEFAULT_TIMEOUT,
                 terminal_url="", terminal_token="", terminal_timeout=10):
        """
        sandbox_root 给定时，sandbox_keys 里的系统会把自己的数据目录重定向到
        <sandbox_root>/<key>，用于"联动自检"不污染真实运行数据。
        01 默认用真实数据（否则没人脸库可核验，演示不出效果）；需要时用 sandbox_01=True。
        timeout 是单次跨系统子进程调用的上限（超时会**强制终止整个进程树**）。

        terminal_url 给定时，03 走**局域网接入服务**（不再起本机子进程）：
        设备 A 跑 01/02+00、设备 B 跑 03 的部署方式就用这个参数。
        地址/令牌留空时依次回退到环境变量 EXAM_TERMINAL_URL / EXAM_TERMINAL_TOKEN。
        """
        keys = set(sandbox_keys)
        if sandbox_01:
            keys.add("01")
        self.sandbox = bool(sandbox_root)

        def home(key):
            return os.path.join(sandbox_root, key) if (sandbox_root and key in keys) else None

        # 沙箱重定向环境变量要**整套传给每个子进程**（嵌套调用才不会漏回真实目录）
        redirects = {}
        for key in ("01", "02", "03", "06"):
            h = home(key)
            if h:
                redirects[Project.HOME_ENV[key]] = h

        self.p01 = Project("01", python=python, timeout=timeout, home=home("01"),
                           env_extra=redirects)
        self.p02 = Project("02", python=python, timeout=timeout, home=home("02"),
                           env_extra=redirects)
        url = (terminal_url or os.environ.get("EXAM_TERMINAL_URL", "")).strip()
        token = (terminal_token or os.environ.get("EXAM_TERMINAL_TOKEN", "")).strip()
        if url:
            # 远端终端：不再需要沙箱重定向（数据在另一台机器上）
            self.p03 = RemoteTerminal(url, token=token, timeout=terminal_timeout)
            self.remote_terminal = True
        else:
            self.p03 = Project("03", python=python, timeout=timeout, home=home("03"),
                               env_extra=redirects)
            self.remote_terminal = False
        self.p06 = Project("06", python=python, timeout=timeout, home=home("06"),
                           env_extra=redirects)

    # ---------- 各自状态 ----------
    def status(self):
        out = {}
        for key, proj in (("01", self.p01), ("02", self.p02), ("03", self.p03),
                          ("06", self.p06)):
            ok, why = proj.available()
            if not ok:
                out[key] = {"ok": False, "reason": why}
                continue
            try:
                out[key] = proj.call("status", {})
            except LinkageError as e:
                out[key] = {"ok": False, "reason": str(e)}
        return out

    # ---------- 联调主链 ----------
    def camera_credibility(self, device_code):
        """② 问 02：这个机位的告警可信度是多少"""
        r = self.p02.call("credibility", {"device_code": device_code})
        return float(r.get("factor", 1.0)), r.get("reason", ""), r

    def raise_alert(self, code, device_code, room_id="", seat_no="", detail="",
                    candidate_id="", candidate_name="", operator="linkage"):
        """
        ③ 02 -> 03：把告警送到终端，并带上该机位的画质可信度
        返回 dict，含 credibility / downgraded / alert_id
        """
        factor, why, cinfo = self.camera_credibility(device_code)
        r = self.p03.call("ingest_alert", {
            "code": code, "device_id": device_code, "room_id": room_id,
            "seat_no": seat_no, "detail": detail,
            "candidate_id": candidate_id, "candidate_name": candidate_name,
            "credibility": factor, "operator": operator,
            "source": "linkage/02"})
        r["credibility_reason"] = why
        r["camera_defects"] = cinfo.get("defects", [])
        return r

    def face_verify(self, image, candidate="", mode="1:1", device_code="",
                    operator="linkage"):
        """
        ④ 01 + 02：人脸核验；若该机位画质不可信，结论一并标注降权
        """
        r = self.p01.call("verify", {"image": image, "candidate": candidate,
                                     "mode": mode, "device": device_code or "LINKAGE",
                                     "operator": operator})
        if device_code:
            factor, why, _ = self.camera_credibility(device_code)
            r["camera_credibility"] = factor
            r["camera_credibility_reason"] = why
            r["trustworthy"] = factor >= 0.8
            if factor < 0.8:
                r["warning"] = ("该机位画质可信度 ×%.1f，核验结论需人工复核：%s"
                                % (factor, why))
        return r

    def inspect_site(self, image, device_code="", device_id="", operator="linkage"):
        """② 02：单机位现场画面诊断（画质+视野）"""
        return self.p02.call("inspect", {"image": image, "device_code": device_code,
                                         "device_id": device_id, "operator": operator})

    def close_alert(self, alert_id, conclusion="confirmed", reason="联动复核",
                    operator="linkage"):
        """⑤ 处置结果回流 03 留痕"""
        return self.p03.call("review_alert", {"alert_id": alert_id,
                                              "conclusion": conclusion,
                                              "reason": reason, "operator": operator})

    def alerts(self, limit=20):
        return self.p03.call("list_alerts", {"limit": limit})

    def remote_records(self, limit=50, kind="", device_id=""):
        """
        看 03 收到的**远端设备记录**（01 的操作日志/核验记录、02 的核查结论/工单）。
        本机模式走 03 的 integration_api 的 list_remote op；远端模式走接入服务的
        GET /api/records —— 两种模式同一个调用方法。
        """
        return self.p03.call("list_remote", {"limit": limit, "kind": kind,
                                             "device_id": device_id})

    # ---------- 06 智能安检（事前阻断） ----------
    def security_pass(self, payload):
        """
        让 06 判一次过检：payload 给 device_code / candidate_id / sensors 等，
        返回 {result, kinds, level, alarm_id, detail}。真设备走各自的行协议，
        这里给的是**脚本/联动自检**用的入口（06 自己也会主动把报警推给 03）。
        """
        return self.p06.call("ingest_pass", payload)

    def security_status(self):
        """06 概览：设备数、过检数、待处置报警、保管柜占用、检出率指标"""
        return self.p06.call("status", {})

    def security_alarms(self, limit=20, pending_only=True):
        return self.p06.call("list_alarms", {"limit": limit,
                                             "pending_only": pending_only})

    def security_reinspect(self, alarm_id, result="release", operator="linkage",
                           note="联动处置"):
        """把 06 的一条安检报警结案（放行/暂扣/转人工通道/误报）"""
        return self.p06.call("reinspect", {"alarm_id": alarm_id, "result": result,
                                           "operator": operator, "note": note})
