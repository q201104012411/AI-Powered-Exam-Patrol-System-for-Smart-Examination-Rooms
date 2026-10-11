# -*- coding: utf-8 -*-
"""
命令控制台桥接脚本（在独立子进程里运行）

为什么单独一个脚本：
    01_人脸识别与身份核对 与本端都有 config.py / models.py，同一进程里 import
    必然撞名（与 _face_bridge.py 同一个原因）。放到子进程后两边命名空间互不干扰，
    本端只通过 stdout 的**最后一行 JSON** 拿结果。

最重要的约束（改动前请先读完）：
    本脚本**不做权限判定**，只把 --role 原样交给 01 的 cmd_console.execute()，
    由 01 内部的 accounts.cmd_console_tier(role) 决定 full / ollama / none。
    在这里"自己判一次然后放行"等于把两级权限体系绕过去：一旦这里判错，
    管理员就会拿到所有者的完整命令执行能力。判定只允许有一份实现。

    同理，也不在 03 复制/改写 01 的 parse_ollama_command() —— 管理员那一级的白名单
    只认 01 的实现，本脚本连"帮忙修正命令"都不做：01 说拒就拒。

用法（一般由 bridge.CmdBridge 调用，不手工执行）：
    python _cmd_bridge.py --face-dir <01目录> --code "echo hi" --mode cmd \
        --role owner --operator Owner [--timeout 60] [--source terminal_gui]
输出：最后一行一个 JSON；失败时 ok=false 并给出 stderr / reason，绝不伪装成功。
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

# 与 01 的 cmd_console 同一套类型名；这里只做"取值是否认识"的输入校验，与权限无关
MODES = ("cmd", "python", "ollama")

# import 01 的模块会顺带往 01\<模块>\__pycache__ 写 .pyc —— 那就等于"改了 01 目录"。
# 这里连同 CmdBridge 传的 PYTHONDONTWRITEBYTECODE=1 一起关掉字节码缓存，
# 保证 03 调用 01 时对 01 目录是**只读**的。
sys.dont_write_bytecode = True


def main():
    ap = argparse.ArgumentParser()
    # 参数名与 _face_bridge.py 保持一致（指向同一个 01 目录），便于两处对照阅读
    ap.add_argument("--face-dir", "--cmd-dir", dest="face_dir", required=True,
                    help="01 模块所在目录")
    ap.add_argument("--code", default="", help="要执行的命令 / 代码")
    ap.add_argument("--mode", default="cmd", choices=list(MODES))
    ap.add_argument("--role", required=True,
                    help="原样传给 01，由 01 判定层级（本脚本不判权限）")
    ap.add_argument("--operator", default="terminal")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--source", default="terminal_gui")
    args = ap.parse_args()

    face_dir = str(Path(args.face_dir).resolve())
    if not os.path.isdir(face_dir):
        return emit({"ok": False, "tier": "none", "stderr": "01 模块目录不存在：%s" % face_dir,
                     "reason": "01 模块目录不存在：%s" % face_dir})
    if not os.path.isfile(os.path.join(face_dir, "cmd_console.py")):
        return emit({"ok": False, "tier": "none", "stderr": "01 目录下没有 cmd_console.py",
                     "reason": "01 目录下没有 cmd_console.py"})

    # 只把 01 目录放进 sys.path 首位；本脚本自身不 import 本端任何模块，避免撞名
    sys.path.insert(0, face_dir)
    cwd = os.getcwd()
    result = None
    try:
        os.chdir(face_dir)                      # 01 的部分模块依赖相对路径
        import cmd_console                      # noqa: E402
        result = cmd_console.execute(
            args.code, operator=args.operator, role=args.role,
            mode=args.mode, source=args.source,
            # 上限交给 01 自己再夹一次（这里是粗筛，避免负数/离谱值传进去）
            timeout=max(1, min(int(args.timeout or 60), 600)))
    except Exception as e:                      # 任何异常都如实回传，不伪装成功
        return emit({"ok": False, "tier": "none", "mode": args.mode,
                     "stderr": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "reason": "桥接执行失败：%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-800:]})
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass

    # 01 的 execute() 返回结构化结果，这里原样摊平给本端（字段名保持可读，
    # 不把 result 整体塞进一个子对象，避免本端要写两层 get）
    return emit({
        "ok": bool(result.get("ok")),
        # 01 在"拦截"路径上（无权限 / 白名单不过）不回传 tier，只回 ok=False；
        # 这时向 01 再问一次它自己的判定结果，仅为**报告**（界面与审计要显示"01 判你是什么层级"）。
        # 能不能执行完全由上面的 execute() 决定，这里不参与放行。
        "tier": result.get("tier") or _tier_verdict_of_01(args.role),
        "mode": result.get("mode", args.mode),
        "exit_code": result.get("exit_code"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "duration_ms": result.get("duration_ms", 0),
        "timed_out": bool(result.get("timed_out")),
        "truncated": bool(result.get("truncated")),
        "cwd": result.get("cwd", ""),
        "command": args.code,
        "reason": "" if result.get("ok") else str(result.get("stderr") or "")[:200],
        "source": "03_智能终端操作",
    })


def _tier_verdict_of_01(role):
    """
    读一次 01 的层级判定（accounts.cmd_console_tier），只用于报告，不用于放行。

    为什么不干脆留空：被拦下时最需要说清的是"01 认为你是什么层级"，
    否则审计里只有一句"无权限"，事后无法分辨是开关没开、还是角色本来就不行。
    """
    try:
        import accounts                          # 01 的账户层（已在 sys.path 首位）
        return accounts.cmd_console_tier(role)
    except Exception:
        return ""


def emit(obj):
    """结果只以最后一行 JSON 呈现：前面可能有 01 模块 import 时的打印，不去动它"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
