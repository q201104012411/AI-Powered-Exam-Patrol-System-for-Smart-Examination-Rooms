# -*- coding: utf-8 -*-
"""
与既有系统衔接（《智能终端.md》第四节）

1) 手持核验终端 -> 项目一「人脸识别与身份核对」
   01 的模块用的是同名 config/models，直接 import 会与本端撞名，
   因此在**独立子进程**里加载 01，用 JSON 交换结果（_face_bridge.py 负责那一侧）。
   本端只做进程编排与结果归一化，不复制 01 的算法。

2) 巡考业务平台 / 边缘节点 -> HTTP 接口（告警订阅 + 指令下发）
   地址未配置时如实返回"未配置"，不假装联通。

3) 统一身份认证 -> 预留 AUTH_URL；未配置时使用本机终端账户（access.py）。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import config

_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_face_bridge.py")


class BridgeError(Exception):
    pass


class FaceBridge(object):
    """手持核验终端：调用项目一的人脸核验（隔离子进程）"""

    def __init__(self, face_dir=None, python=None, timeout=180):
        self.face_dir = face_dir or config.FACE_MODULE_DIR
        self.python = python or sys.executable
        self.timeout = timeout

    def available(self):
        """如实报告可用性：目录、桥接脚本、模型目录三件事都要在"""
        problems = []
        if not os.path.isdir(self.face_dir):
            problems.append("未找到 01 模块目录：%s" % self.face_dir)
        if not os.path.exists(os.path.join(self.face_dir, "verifier.py")):
            problems.append("01 目录下没有 verifier.py")
        if not os.path.exists(_BRIDGE):
            problems.append("缺少桥接脚本 _face_bridge.py")
        model = os.path.join(os.path.expanduser("~"), ".insightface", "models", "buffalo_l")
        if not os.path.isdir(model):
            problems.append("未找到人脸模型目录：%s" % model)
        return (len(problems) == 0), ("可用" if not problems else "；".join(problems))

    def verify(self, image_path, candidate_id="", mode="1:1", operator="checker"):
        """
        调用 01 做一次核验。返回 dict：
          {ok, result, reason_code, reason, score, threshold, candidate_id, raw}
        失败时 ok=False 且 reason 说明原因（不伪造成功）。
        """
        if not os.path.exists(image_path):
            return {"ok": False, "reason": "图片不存在：%s" % image_path}
        ok, why = self.available()
        if not ok:
            return {"ok": False, "reason": why}
        cmd = [self.python, _BRIDGE, "--face-dir", self.face_dir,
               "--image", image_path, "--mode", mode, "--operator", operator]
        if candidate_id:
            cmd += ["--candidate", candidate_id]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "核验超时（%ds）" % self.timeout}
        except OSError as e:
            return {"ok": False, "reason": "无法启动核验子进程：%s" % e}
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        # 桥接脚本以最后一行 JSON 作为结果
        line = ""
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                line = ln.strip()
                break
        if not line:
            return {"ok": False, "reason": "核验无有效输出（返回码 %s）：%s"
                                          % (p.returncode,
                                             (p.stderr or b"").decode("utf-8", "replace")[:200])}
        try:
            data = json.loads(line)
        except ValueError:
            return {"ok": False, "reason": "核验输出无法解析：%s" % line[:200]}
        data["ok"] = bool(data.get("ok"))
        return data


class PlatformLink(object):
    """业务平台 / 边缘节点 HTTP 接口"""

    def __init__(self, platform_url=None, edge_url=None, timeout=None):
        self.platform_url = (platform_url if platform_url is not None
                             else config.PLATFORM_URL).rstrip("/")
        self.edge_url = (edge_url if edge_url is not None
                         else config.EDGE_URL).rstrip("/")
        self.timeout = timeout or config.HTTP_TIMEOUT_SEC

    def _get(self, url, path):
        if not url:
            return {"ok": False, "reason": "未配置地址"}
        try:
            with urllib.request.urlopen(url + path, timeout=self.timeout) as r:
                return {"ok": True, "data": json.loads(r.read().decode("utf-8"))}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "reason": str(e)[:160]}

    def platform_alerts(self, limit=100):
        return self._get(self.platform_url, "/alerts?limit=%d" % limit)

    def platform_commands(self):
        return self._get(self.platform_url, "/commands")

    def edge_health(self):
        return self._get(self.edge_url, "/health")

    def connectivity(self):
        """联通性自检（验收要点第 1 条）：如实报告每一项"""
        return {
            "业务平台": self._probe(self.platform_url),
            "边缘节点": self._probe(self.edge_url),
            "统一认证": self._probe(config.AUTH_URL.rstrip("/")),
        }

    def _probe(self, url):
        if not url:
            return "未配置"
        try:
            with urllib.request.urlopen(url + "/health", timeout=self.timeout) as r:
                return "连通（HTTP %d）" % r.status
        except (urllib.error.URLError, OSError, ValueError) as e:
            return "不可达：%s" % str(e)[:80]
