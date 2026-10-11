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

4) 命令控制台 -> 项目一「命令控制台」（同样是隔离子进程）
   03 与 01 都实现了"所有者完整命令 / 管理员仅 ollama"，但**权限判定只保留一份**：
   CmdBridge 只做本端开关的快速拦截与结果归一化，最终层级由 01 的
   accounts.cmd_console_tier(role) 判定（见 _cmd_bridge.py 的说明）。
   这样做的好处是：即使将来本端界面加了新入口，也绕不过 01 的那一层判定。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import config
import console

_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_face_bridge.py")
_CMD_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cmd_bridge.py")


class BridgeError(Exception):
    pass


def _last_json_line(text):
    """桥接脚本约定：结果放在 stdout 最后一行 JSON。这里从后往前找第一行能解析的"""
    for ln in reversed((text or "").splitlines()):
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            return json.loads(s)
        except ValueError:
            continue
    return None


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


class CmdBridge(object):
    """
    命令控制台：在隔离子进程里调用项目一的 cmd_console（03 端只做编排、拦截与留痕）

    与 FaceBridge 同一套范式，差别有两处，都在这里说明白：
      · 权限：本端先用 console.console_tier() 做一次 fail-closed 的快速拦截
        （总开关关着就不必起子进程），但**不替 01 判层级**——role 原样传过去，
        由 01 的 accounts.cmd_console_tier 做最终判定。两处都过了才会真正执行。
      · 数据根：子进程用 EXAM_HOME 把 01 的数据根指到本端目录（console.bridge_home()），
        让 01 的开关标记、审计链与临时脚本都落在本端，绝不往 01 目录写文件。
    """

    def __init__(self, cmd_dir=None, python=None, store=None,
                 timeout=console.CONSOLE_DEFAULT_TIMEOUT):
        self.cmd_dir = cmd_dir or config.CMD_CONSOLE_DIR
        self.python = python or sys.executable
        self.store = store                 # 传入 storage.Store 即自动留痕（可为 None）
        self.timeout = timeout

    def available(self):
        """如实报告可用性：01 目录、被调的两个模块、桥接脚本都要在"""
        problems = []
        if not os.path.isdir(self.cmd_dir):
            problems.append("未找到 01 模块目录：%s" % self.cmd_dir)
        else:
            for name in ("cmd_console.py", "accounts.py"):
                if not os.path.exists(os.path.join(self.cmd_dir, name)):
                    problems.append("01 目录下没有 %s" % name)
        if not os.path.exists(_CMD_BRIDGE):
            problems.append("缺少桥接脚本 _cmd_bridge.py")
        return (len(problems) == 0), ("可用" if not problems else "；".join(problems))

    def execute(self, code_text, mode="cmd", role="", operator="terminal",
                timeout=None, source="terminal_gui"):
        """
        调用 01 执行一次命令/代码。返回 dict：

          {ok, tier, mode, exit_code, stdout, stderr, duration_ms, timed_out,
           truncated, cwd, command, role, source, blocked, reason}

        失败（无权限 / 白名单不通过 / 起不来 / 超时）一律 ok=False 且 reason 说明原因，
        不伪造成功。无论成功与否都会调用一次 _trace() 写本端审计链。
        """
        tier = console.console_tier(role)
        if tier == console.CONSOLE_TIER_NONE:
            result = self._blocked(code_text, mode,
                                   "无权限：命令控制台未开启，或当前角色（%s）不在允许范围内。"
                                   % (role or "未知"))
            self._trace(code_text, mode, role, operator, result)
            return result
        ok, why = self.available()
        if not ok:
            result = self._blocked(code_text, mode, why, tier=tier)
            self._trace(code_text, mode, role, operator, result)
            return result
        try:
            home = console.bridge_home()
        except OSError as e:
            result = self._blocked(code_text, mode, "无法准备桥接数据目录：%s" % e, tier=tier)
            self._trace(code_text, mode, role, operator, result)
            return result

        timeout = max(1, min(int(timeout or self.timeout), console.CONSOLE_MAX_TIMEOUT))
        cmd = [self.python, _CMD_BRIDGE, "--face-dir", self.cmd_dir,
               "--code", code_text or "", "--mode", mode, "--role", role or "",
               "--operator", operator or "", "--timeout", str(timeout),
               "--source", source]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"          # 与 FaceBridge 一致：中文结果不走乱码
        # 不让子进程往 01 目录写 .pyc：调用 01 必须是只读的（_cmd_bridge.py 里也设了同一件事）
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 把 01 的数据根指到本端目录：01 只往这里写审计与临时文件，不碰 01 目录。
        # EXAM_DB 也一并固定，避免环境里遗留的设置让 01 写到别的库里。
        env["EXAM_HOME"] = home
        env["EXAM_DB"] = os.path.join(home, "exam.db")
        try:
            # 外层再留 30 秒余量：01 到点会自己杀进程树，这里是"连桥接都卡住"的兜底
            p = subprocess.run(cmd, capture_output=True, timeout=timeout + 30, env=env)
        except subprocess.TimeoutExpired:
            result = self._blocked(code_text, mode, "桥接进程超时（%ds）" % (timeout + 30),
                                   tier=tier)
            self._trace(code_text, mode, role, operator, result)
            return result
        except OSError as e:
            result = self._blocked(code_text, mode, "无法启动桥接子进程：%s" % e, tier=tier)
            self._trace(code_text, mode, role, operator, result)
            return result

        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        data = _last_json_line(out)
        if data is None:
            result = self._blocked(code_text, mode,
                                   "桥接无有效输出（返回码 %s）：%s"
                                   % (p.returncode,
                                      (p.stderr or b"").decode("utf-8", "replace")[:200]),
                                   tier=tier)
        else:
            result = data
            result["ok"] = bool(result.get("ok"))
            if not result.get("tier"):
                # 桥接没给出层级（异常/无输出等路径）时才用本端判定值兜底，仅为显示一致
                result["tier"] = tier
            result["command"] = code_text
            result["role"] = role
            result["source"] = source
            result["blocked"] = result.get("exit_code") is None
            if not result["ok"] and not result.get("reason"):
                result["reason"] = ((result.get("stderr") or "").strip()[:200]
                                    or "执行失败")
        self._trace(code_text, mode, role, operator, result)
        return result

    def _blocked(self, code_text, mode, message, tier=console.CONSOLE_TIER_NONE):
        """构造"被拦下 / 没跑起来"的结果：字段与 01 的结果同形，界面不必分两套渲染"""
        return {"ok": False, "tier": tier, "mode": "refused", "exit_code": None,
                "stdout": "", "stderr": message, "duration_ms": 0, "timed_out": False,
                "truncated": False, "cwd": self.cmd_dir, "command": code_text,
                "role": "", "source": "", "blocked": True, "reason": message}

    def _trace(self, code_text, mode, role, operator, result):
        """
        本端留痕：**每一次**调用都写审计哈希链——包括无权限被拒、白名单被拒、
        进程起不来、超时。原因很直接：想知道"谁在什么时候试图执行什么"，
        被拒绝的尝试往往比成功执行更有价值（那是有人在试探）。
        01 侧自己还有一条哈希链记录（落在本端数据目录），两条互不替代。
        """
        if self.store is None:
            return
        head = (code_text or "").strip().splitlines()
        first = head[0][:200] if head else ""
        more = "（共 %d 行）" % len(head) if len(head) > 1 else ""
        detail = ("[%s/%s→%s] 角色=%s %s%s | 退出码=%s 耗时=%sms 输出=%d+%d字节%s"
                  % (result.get("tier"), mode, result.get("source") or "-",
                     config.ROLE_TEXT.get(role, role or "未知"), first, more,
                     result.get("exit_code"), result.get("duration_ms"),
                     len(result.get("stdout") or ""), len(result.get("stderr") or ""),
                     " 已截断" if result.get("truncated") else ""))
        if result.get("timed_out"):
            detail += " | 超时已强制结束进程树"
        if not result.get("ok"):
            detail += " | 未执行/失败：%s" % (result.get("reason") or "")[:120]
        try:
            self.store.log("命令控制台", detail, operator or "unknown")
        except Exception:
            pass        # 留痕失败不能反过来把执行结果丢掉（与 01 的 _audit 同一处理）


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
