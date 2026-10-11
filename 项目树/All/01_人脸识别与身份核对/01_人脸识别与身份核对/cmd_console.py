# -*- coding: utf-8 -*-
"""
命令控制台：受控的本地命令 / Python 代码执行。

设计立场（这是本模块存在的理由，改动前请先读完）：
    这是**任意代码执行**能力，等价于本机（或部署该网页的服务器）的完全控制权。
    因此它不追求"方便"，而是尽力做到三件事：
      1. 默认关闭，且**分两级**（判定唯一入口：accounts.cmd_console_tier）：
             所有者           -> full    任意 cmd / python
             管理员（被授权）  -> ollama  仅 `ollama run <模型> [提示词]`
             其他角色         -> none
      2. 每一次执行（**包括被拒绝的尝试**）都写入不可篡改的审计哈希链；
      3. 执行过程与 Flask 主进程隔离，超时/输出爆炸不会拖垮核验业务。

权限只在这里判一次：
    execute() 的 role 是**必填**且 fail-closed —— 拿不到角色就当作 none，
    这样以后新增调用点时哪怕忘了传角色，结果也是"没权限"而不是"意外拿到 full"。

安全边界（务必如实告知使用者，不要留有"以为很安全"的错觉）：
    · **full 层级不做任何命令限制**——"任意代码"本身就无法与白名单共存。
      真正的防线是"谁能登录所有者 + 总开关是否打开"。因此所有者的口令强度
      直接等于本机安全性；出厂默认口令已在公开仓库里，部署前必须改掉。
    · **ollama 层级**才使用白名单（parse_ollama_command）：整条命令按
      `ollama run <模型> [提示词]` 严格匹配，且**不走 shell**。所以提示词里的
      `|` `&` `>` `;` 等元字符是惰性的——它们只能作为文本传给大模型。
    · 子进程以本进程相同的用户身份运行，没有额外的降权隔离。

支持的模式：
    mode="cmd"     系统命令（走 cmd.exe，与在命令行里敲一样）—— 仅 full
    mode="python"  Python 代码（写临时文件后由本解释器执行，报错带真实行号）—— 仅 full
    mode="ollama"  本地大模型单轮问答 —— 管理员层级会被**强制**为该模式
"""
import locale
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import config
from models import VerifyRecord
from pii import sanitize_for_log

# ---------------- 限制参数 ----------------
DEFAULT_TIMEOUT = 60                 # 秒
MAX_TIMEOUT = 600                    # 秒，防止有人填个 999999
MAX_OUTPUT_BYTES = 256 * 1024        # 每个流最多保留 256KB，超出截断
_READ_CHUNK = 8192

# ---------------- 管理员层级白名单：只允许 `ollama run <模型> [提示词]` ----------------
# 这是"所有者(full)"与"管理员(ollama)"两级之间的**唯一防线**，所以按白名单匹配**整条命令**，
# 而不是"过滤危险字符"——黑名单永远会漏，白名单不会。
#
# 关键设计：匹配成功后**不走 shell**，而是把模型名与提示词作为独立 argv 交给 ollama 进程。
# 因此提示词里出现 | & > ; ` $ 之类字符也完全无效——它们只会作为普通文本传给大模型，
# 不存在被 shell 解释的代码路径。这也是为什么不需要（也不应该）去"消毒"提示词。
_OLLAMA_CMD_RE = re.compile(
    r"""^\s*ollama\s+run\s+                            # 固定前缀，大小写敏感
        (?P<model>[A-Za-z0-9][A-Za-z0-9._:\-]{0,63})  # 模型名：限定字符集与长度
        (?:\s+(?P<prompt>.*?))?\s*$                   # 可选提示词（整段作为单个参数）
    """, re.VERBOSE | re.DOTALL)

ADMIN_HINT = "管理员只允许执行 `ollama run <模型> [提示词]`，用于 AI 辅助判断。"


def parse_ollama_command(text):
    """
    严格白名单解析。返回 (ok, argv | None, msg)。

    只接受 `ollama run <模型>` 或 `ollama run <模型> <提示词>` 这一种形式。

    **会被拒绝**的是那些无法表达成这一形式的输入：
        · 其它程序：`calc`、`dir`、`cmd /c ...`、`echo ollama run ...`、`sudo ollama ...`
        · 其它 ollama 子命令：`ollama pull / rm / serve / push ...`
        · 模型名含允许字符集之外的字符，或以 `-` 开头（防它被当成命令行参数）
        · `ollama run` 之后没有模型名

    **不会被拒绝、但也不需要拒绝**的是模型名之后的 `;` `&&` `|` `>` 换行等元字符：
    它们会整体落进 prompt，成为**单个 argv 元素**里的普通文本。因为执行用
    shell=False，这些字符没有可被解释的路径——既不用"消毒"提示词，也不会跑出第二条命令。

    所以这里的安全保证要如实表述为："元字符逃不出 prompt 这一个参数"，
    **而不是** "元字符会被语法层拒绝"。（曾经写成后者，那是错的。）
    """
    text = text or ""
    if not text.strip():
        return False, None, "请输入 `ollama run <模型> [提示词]`"
    m = _OLLAMA_CMD_RE.match(text)
    if not m:
        return False, None, (
            ADMIN_HINT + "\n（只接受这一种形式：其它程序、其它 ollama 子命令、"
                         "以 - 开头的模型名都不允许）")
    model = m.group("model")
    prompt = (m.group("prompt") or "").strip()
    argv = ["ollama", "run", model]
    if prompt:
        argv.append(prompt)          # 整段提示词作为**一个**参数传入，不做切分
    return True, argv, ""


def run_ollama(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """执行白名单命令：arg 列表 + shell=False，不存在被 shell 解释的可能"""
    return _execute(argv=argv, cwd=cwd, timeout=timeout, prefer_utf8=True)


def _oem_encoding():
    """Windows 控制台程序（cmd/dir/git 等）输出用的是 OEM 代码页，中文机器上通常是 cp936"""
    if os.name != "nt":
        return locale.getpreferredencoding(False) or "utf-8"
    try:
        import ctypes
        return "cp%d" % int(ctypes.windll.kernel32.GetOEMCP())
    except Exception:
        return "cp936"


def _decode(raw: bytes, prefer_utf8: bool = True) -> str:
    """
    尽力解码子进程输出。

    **顺序很关键：必须先试 UTF-8。**
    原因：UTF-8 是自校验编码，真正的 GBK 中文几乎不可能同时是合法 UTF-8，
    所以 UTF-8 失败后能自然回落到 GBK。反过来（先试 GBK）则会踩坑——
    有些 UTF-8 字节序列**恰好也是合法 GBK**，于是被"成功"解成乱码而**不抛异常**，
    永远轮不到 UTF-8。真实案例（本项目实测）：

        echo 正常   实际输出 b'UI \\xe6\\xad\\xa3\\xe5\\xb8\\xb8'
          先按 GBK 解  -> 'UI 姝ｅ父'   ← 乱码，且因为能解出来所以不报错
          先按 UTF-8 解 -> 'UI 正常'    ← 正确

    早期版本对 cmd 模式用"先 OEM 代码页"，只有当字节碰巧不是合法 GBK 时才对
    （例如 echo 中文输出测试），因此表现为"有些中文正常、有些乱码"，极难排查。

    prefer_utf8 参数保留是为了兼容调用方，但两种模式现在都统一 UTF-8 优先。
    """
    if not raw:
        return ""
    order = ["utf-8", _oem_encoding(), "gbk", "big5"]
    if not prefer_utf8:
        # 即使调用方声明"更可能是本地代码页"，也仍然 UTF-8 优先：
        # 上面那条真实案例证明反过来会静默产出乱码。
        order = ["utf-8", _oem_encoding(), "gbk", "big5"]
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class _Sink:
    """带容量上限的输出收集器（子进程狂刷输出时不会把内存吃光）"""

    def __init__(self, limit: int = MAX_OUTPUT_BYTES):
        self.limit = limit
        self.chunks = []
        self.size = 0
        self.truncated = False

    def feed(self, data: bytes):
        if not data:
            return
        room = self.limit - self.size
        if room <= 0:
            self.truncated = True
            return
        if len(data) > room:
            self.chunks.append(data[:room])
            self.size = self.limit
            self.truncated = True
        else:
            self.chunks.append(data)
            self.size += len(data)

    def bytes(self) -> bytes:
        return b"".join(self.chunks)


def _pump(pipe, sink: _Sink):
    """把管道内容抽到收集器；必须放在线程里，否则子进程写满管道会死锁"""
    try:
        while True:
            chunk = pipe.read(_READ_CHUNK)
            if not chunk:
                break
            sink.feed(chunk)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _kill_tree(proc):
    """
    结束进程树。

    只 kill 直接子进程是不够的：`cmd /c python long_task.py` 被杀掉后，
    python 孙进程仍会继续跑并占着管道。Windows 上用 taskkill /T 连根拔。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _execute(argv=None, shell_cmd=None, cwd=None, timeout=DEFAULT_TIMEOUT,
             env_extra=None, prefer_utf8=False):
    """
    真正干活的函数：起子进程 -> 双线程收输出 -> 超时杀进程树 -> 返回结构化结果。
    """
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    workdir = str(cwd or config.BASE_DIR)

    env = os.environ.copy()
    # 让 Python 子进程走 UTF-8，避免中文代码里的字符串在管道里变成乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if env_extra:
        env.update(env_extra)

    started = time.time()
    try:
        proc = subprocess.Popen(
            shell_cmd if shell_cmd is not None else argv,
            shell=shell_cmd is not None,      # cmd 模式交给 cmd.exe 解析，避免 list2cmdline 把路径结尾的反斜杠吃掉
            cwd=workdir,
            stdin=subprocess.DEVNULL,          # 不给交互输入：需要输入的命令直接拿到 EOF，而不是挂住
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except (OSError, ValueError) as e:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "无法启动子进程：%s" % e,
                "duration_ms": 0, "truncated": False, "timed_out": False, "cwd": workdir}

    out_sink, err_sink = _Sink(), _Sink()
    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_sink), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_sink), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    # 给读线程一点时间把残留缓冲收完（进程已退出，不会再有新数据）
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    code = proc.returncode
    return {
        "ok": (not timed_out) and code == 0,
        "exit_code": code,
        "stdout": _decode(out_sink.bytes(), prefer_utf8),
        "stderr": _decode(err_sink.bytes(), prefer_utf8),
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": out_sink.truncated or err_sink.truncated,
        "timed_out": timed_out,
        "cwd": workdir,
    }


def run_shell(command: str, cwd=None, timeout=DEFAULT_TIMEOUT):
    """执行系统命令（等价于在 cmd 里敲这一行）"""
    return _execute(shell_cmd=command, cwd=cwd, timeout=timeout, prefer_utf8=False)


def run_python(code: str, cwd=None, timeout=DEFAULT_TIMEOUT):
    """
    执行 Python 代码。

    落临时文件而不是 `python -c`：多行脚本报错时能给出真实行号，也避免命令行长度上限。
    """
    tmp_dir = os.path.join(str(config.DATA_ROOT), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, "cmd_console_%s.py" % datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(code if code.endswith("\n") else code + "\n")
    try:
        return _execute(argv=[sys.executable, path], cwd=cwd, timeout=timeout, prefer_utf8=True)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------- 审计 ----------------
def _audit(code_text: str, mode: str, operator: str, source: str, result: dict,
           tier: str = "full"):
    """
    写入审计哈希链 + 界面操作日志。

    注意：AuditLogger 会对 reason 做统一脱敏（身份证号/手机号），
    所以如果命令里含身份证号，链上记录会是掩码——这是本项目一贯的隐私策略，不要绕过它。
    """
    if result.get("timed_out"):
        outcome = "TIMEOUT"
    elif result.get("exit_code") is None:
        outcome = "BLOCKED"          # 被权限层级或白名单拦下，根本没有执行
    elif result.get("exit_code") == 0:
        outcome = "PASS"
    else:
        outcome = "FAIL"

    # 只记首行 + 长度，避免把整段脚本/输出灌进链里把日志撑爆
    head = (code_text or "").strip().splitlines()
    first = head[0][:200] if head else ""
    more = "（共 %d 行）" % len(head) if len(head) > 1 else ""
    reason = ("命令控制台执行[%s/%s→%s]：%s%s | 退出码=%s 耗时=%sms 输出=%d+%d字节%s"
              % (tier, mode, source, first, more, result.get("exit_code"),
                 result.get("duration_ms"),
                 len(result.get("stdout") or ""), len(result.get("stderr") or ""),
                 " 已截断" if result.get("truncated") else ""))
    if result.get("timed_out"):
        reason += " | 超时已强制终止进程树"

    try:
        from audit import AuditLogger
        AuditLogger().log(VerifyRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            device_id="CMD_CONSOLE", candidate_id=None,
            mode="CMD", result=outcome, reason_code="CMD_EXEC",
            reason=reason, operator=operator or "unknown", manual_required=False,
        ))
    except Exception:
        pass                      # 审计失败不能反过来让执行结果丢失

    try:
        from action_logger import ActionLogger
        ActionLogger().log("命令控制台[%s]" % source, sanitize_for_log(reason))
    except Exception:
        pass


def _blocked(code_text, mode, operator, source, tier, message):
    """构造"被拦下"的结果并留痕（拦下也要记审计：谁在什么时候试图执行了什么）"""
    result = {"ok": False, "exit_code": None, "stdout": "", "stderr": message,
              "duration_ms": 0, "truncated": False, "timed_out": False,
              "mode": mode, "command": code_text,
              # 拦截路径也要回传 tier：调用方（含 03 的桥接）需要据此说明
              # "是开关没开、还是角色本来就不行"，否则审计里只剩一句"无权限"
              "tier": tier}
    _audit(code_text, mode, operator, source, result, tier)
    return result


def execute(code_text: str, operator: str, role, mode: str = "cmd", source: str = "gui",
            timeout: int = DEFAULT_TIMEOUT, cwd=None):
    """
    统一入口：判定层级 -> 执行 -> 审计。GUI 与网页端都调这里，
    保证"谁能做什么"与"留痕"这两件事在两端只有一份实现。

    role 是**必填**且默认拒绝（fail-closed）：任何拿不到角色的调用都得不到执行权限，
    这样以后新增调用点时哪怕忘了传角色，结果也是"没权限"而不是"意外获得 full"。

    层级（由 accounts.cmd_console_tier 决定，见那里的说明）：
        full   -> mode="cmd" 系统命令 / mode="python" Python 代码
        ollama -> 只允许 `ollama run <模型> [提示词]`，mode 被强制为 "ollama"
        none   -> 拒绝

    返回：执行结果 dict，另含 "mode"、"command"、"tier" 字段。
    """
    import accounts

    code_text = code_text or ""
    tier = accounts.cmd_console_tier(role)

    if tier == accounts.CMD_TIER_NONE:
        return _blocked(code_text, "refused", operator, source, tier,
                        "无权限：命令控制台未开启，或当前角色（%s）不在允许范围内。"
                        % (role or "未知"))

    if tier == accounts.CMD_TIER_OLLAMA:
        if not code_text.strip():
            return _blocked(code_text, "ollama", operator, source, tier,
                            "请输入 `ollama run <模型> [提示词]`")
        ok, argv, msg = parse_ollama_command(code_text)
        if not ok:
            # 白名单不通过：不是"帮你改成合法命令"，而是直接拒绝并留痕
            return _blocked(code_text, "ollama-rejected", operator, source, tier, msg)
        result = run_ollama(argv, cwd=cwd, timeout=timeout)
        result["mode"] = "ollama"
    else:
        if not code_text.strip():
            return _blocked(code_text, mode, operator, source, tier, "命令为空")
        if mode == "python":
            result = run_python(code_text, cwd=cwd, timeout=timeout)
        else:
            mode = "cmd"
            result = run_shell(code_text, cwd=cwd, timeout=timeout)
        result["mode"] = mode

    result["command"] = code_text
    result["tier"] = tier
    _audit(code_text, result["mode"], operator, source, result, tier)
    return result


def status(role: str):
    """给界面用的状态：当前角色处于哪一层级、能不能用、以及可用的模式"""
    import accounts
    tier = accounts.cmd_console_tier(role)
    return {
        "enabled": accounts.cmd_console_enabled(),
        "admin_allowed": accounts.cmd_console_admin_allowed(),
        "tier": tier,
        "can_run": tier != accounts.CMD_TIER_NONE,
        "can_full": tier == accounts.CMD_TIER_FULL,      # 能否执行任意 cmd / python
        "modes": (["cmd", "python"] if tier == accounts.CMD_TIER_FULL
                  else (["ollama"] if tier == accounts.CMD_TIER_OLLAMA else [])),
        "role": role,
        "cwd": str(config.BASE_DIR),
        "default_timeout": DEFAULT_TIMEOUT,
        "max_timeout": MAX_TIMEOUT,
    }
