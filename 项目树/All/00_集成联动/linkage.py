# -*- coding: utf-8 -*-
"""
00 集成联动 · 三套系统互相调用

  01 人脸识别与身份核对   ->  who（身份是不是本人）
  02 摄像头核查与视频质量诊断 -> whether（画面可不可信）
  03 智能终端操作         ->  what to do（告警怎么处置、怎么留痕）

隔离方式：各套各有一个 integration_api.py，本模块用**子进程 + JSON** 调用它们。
（三套都有 config.py / models.py，同进程 import 必然撞名，子进程最稳。）

联动链（本模块把这条链真正跑通）：
  ① 02 巡检/抽查发现机位画质异常
  ② 02 给出该机位的告警可信度系数 credibility_factor
  ③ 03 接入告警时按该系数降权；系数 < 0.6 时红色预警自动降级为橙色（防误报堆积）
  ④ 01 做人脸核验；若该机位画质不可信，核验结论也标注"画质降权"
  ⑤ 处置结果（复核结论）回流 03 留痕，画质问题由 02 的工单闭环
"""
import json
import os
import subprocess
import sys

ALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROJECTS = {
    "01": os.path.join(ALL_DIR, "01_人脸识别与身份核对", "01_人脸识别与身份核对"),
    "02": os.path.join(ALL_DIR, "02_摄像头核查与视频质量诊断"),
    "03": os.path.join(ALL_DIR, "03_智能终端操作"),
}


class LinkageError(Exception):
    pass


class Project(object):
    """对某一套系统的调用句柄（子进程 + JSON）"""

    # 各套的数据目录重定向环境变量（用于"沙箱自检"，不污染真实数据）
    HOME_ENV = {"01": "EXAM_HOME", "02": "EXAM_CAMERA_HOME", "03": "EXAM_TERMINAL_HOME"}

    def __init__(self, key, python=None, timeout=180, home=None):
        self.key = key
        self.dir = PROJECTS[key]
        self.python = python or sys.executable
        self.timeout = timeout
        self.script = os.path.join(self.dir, "integration_api.py")
        self.home = home                      # None = 用各套真实数据目录

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
        env["PYTHONIOENCODING"] = "utf-8"
        if self.home:
            os.makedirs(self.home, exist_ok=True)
            env[self.HOME_ENV[self.key]] = self.home
        cmd = [self.python, self.script, "--op", op,
               "--args", json.dumps(args or {}, ensure_ascii=False)]
        try:
            run_kwargs = {"capture_output": True, "timeout": self.timeout,
                          "env": env, "cwd": self.dir}
            if sys.platform == "win32":
                # 禁止在 02/03 GUI 调用时弹出黑色命令行窗口
                run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.run(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            raise LinkageError("[%s] 调用 %s 超时（%ds）" % (self.key, op, self.timeout))
        except OSError as e:
            raise LinkageError("[%s] 无法启动子进程：%s" % (self.key, e))
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        line = ""
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                line = ln.strip()
                break
        if not line:
            err = (p.stderr or b"").decode("utf-8", "replace")[:300]
            raise LinkageError("[%s] %s 无有效输出（返回码 %s）：%s"
                               % (self.key, op, p.returncode, err))
        try:
            data = json.loads(line)
        except ValueError:
            raise LinkageError("[%s] %s 输出无法解析：%s" % (self.key, op, line[:200]))
        if not data.get("ok", True) and data.get("reason"):
            data["_error"] = data["reason"]
        return data


class Linkage(object):
    """三套系统的联动总线"""

    def __init__(self, python=None, sandbox_root=None, sandbox_keys=("02", "03"),
                 sandbox_01=False):
        """
        sandbox_root 给定时，sandbox_keys 里的系统会把自己的数据目录重定向到
        <sandbox_root>/<key>，用于"联动自检"不污染真实运行数据。
        01 默认用真实数据（否则没人脸库可核验，演示不出效果）；需要时用 sandbox_01=True。
        """
        keys = set(sandbox_keys)
        if sandbox_01:
            keys.add("01")
        self.sandbox = bool(sandbox_root)

        def home(key):
            return os.path.join(sandbox_root, key) if (sandbox_root and key in keys) else None

        self.p01 = Project("01", python=python, home=home("01"))
        self.p02 = Project("02", python=python, home=home("02"))
        self.p03 = Project("03", python=python, home=home("03"))

    # ---------- 各自状态 ----------
    def status(self):
        out = {}
        for key, proj in (("01", self.p01), ("02", self.p02), ("03", self.p03)):
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
