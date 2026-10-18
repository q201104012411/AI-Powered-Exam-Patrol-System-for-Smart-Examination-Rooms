# -*- coding: utf-8 -*-
r"""
设备端命令执行器（01 人脸识别 / 02 摄像头核查 共用同一份）

作用：作为"被指挥端"，向 03 智能终端的局域网接入服务拉取待执行命令，
      执行后把结果回执给 03。这样智能终端就能**远程指挥 01/02**，
      而不是只能单向接收它们的上报。

安全设计（这份白名单是**第二道闸**，与 03 侧的白名单同时生效）：
  · 只支持有限几种命令，**没有任何"执行任意命令/任意代码"的通道**；
  · 每个命令都映射到本模块自己的 integration_api.py 的某个只读/既有 op；
  · 03 侧的 /api/command 也只接受同一份白名单；
  · 全程带 X-Exam-Token（与既有上报同一令牌），非本机来源必须带令牌。

支持的命令：
  ping          连通性/版本探测（不调业务接口）
  status        回报本模块运行状态（status op）
  logs_tail     回报最近日志若干行（读本模块 data/logs 下最新日志，最多 200 行）
  selftest      跑一次本模块的自检脚本并回报退出码与末尾输出
  rebuild_index 重建人脸特征索引（仅 01 支持）

用法（在 01 或 02 的目录下）：
    python ..\00_集成联动\command_runner.py --module 01 --once
    python ..\00_集成联动\command_runner.py --module 02 --loop 15
环境变量（与既有上报一致）：
    EXAM_TERMINAL_URL    智能终端地址，如 http://192.168.31.176:9113
    EXAM_TERMINAL_TOKEN  接入令牌（03 的 data\lan_token.txt）
    EXAM_DEVICE_ID       本机标识（默认取计算机名）
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))          # 00_集成联动
ALL_DIR = os.path.dirname(HERE)

TOKEN_HEADER = "X-Exam-Token"
PROTOCOL_VERSION = 1

#: 模块代号 -> 目录候选（含 integration_api.py 的那个）
MODULE_DIRS = {
    "01": ("01_人脸识别与身份核对/01_人脸识别与身份核对", "01_人脸识别与身份核对"),
    "02": ("02_摄像头核查与视频质量诊断",),
    "03": ("03_智能终端操作",),
    "05": ("05_考生数据保护",),
    "06": ("06_智能安检与手机作弊器材检测",),
    "08": ("08_试卷流转与保密室智能监控",),
    "09": ("09_无障碍与特殊考生保障",),
    "10": ("10_普通考场+特殊考场应用",),
}

#: 本执行器支持的命令（**与 03 侧 COMMAND_TYPES 保持一致**）
SUPPORTED = ("ping", "status", "logs_tail", "selftest", "rebuild_index")

#: 各模块的自检脚本候选（按顺序找第一个存在的）
SELFTEST_SCRIPTS = ("自检.py", "main.py")


def module_dir(code):
    for rel in MODULE_DIRS.get(code, ()): 
        d = os.path.join(ALL_DIR, *rel.split("/"))
        if os.path.isfile(os.path.join(d, "integration_api.py")):
            return d
    return ""


def device_id(explicit=""):
    return (explicit or os.environ.get("EXAM_DEVICE_ID")
            or socket.gethostname() or "unknown").strip()


def _default_url():
    return (os.environ.get("EXAM_TERMINAL_URL") or "").strip().rstrip("/")


def _default_token():
    return (os.environ.get("EXAM_TERMINAL_TOKEN") or "").strip()


def _http(url, method="GET", payload=None, token="", timeout=15):
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return True, json.loads(body)
        except ValueError:
            return True, {"raw": body[:500]}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return False, json.loads(body)
        except ValueError:
            return False, {"reason": "HTTP %s %s" % (e.code, body[:200])}
    except Exception as e:
        return False, {"reason": "请求失败：%s" % e}


# ---------------------------------------------------------------- 执行
def run_integration(code, op, args=None, timeout=120):
    """在本模块目录里跑 integration_api.py 的某个 op，解析最后一行 JSON"""
    d = module_dir(code)
    if not d:
        return False, {"reason": "找不到模块 %s 的 integration_api.py" % code}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, os.path.join(d, "integration_api.py"),
           "--op", op, "--args", json.dumps(args or {}, ensure_ascii=False)]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "env": env, "cwd": d}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return False, {"reason": "执行超时（%ds）" % timeout}
    out = (p.stdout or b"").decode("utf-8", "replace").splitlines()
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                return True, json.loads(line)
            except ValueError:
                continue
    err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return False, {"reason": (err[-1][:200] if err else
                             "无 JSON 输出（退出码 %s）" % p.returncode)}


def tail_log(code, lines=50):
    """读本模块数据目录下最新的日志文件末尾若干行"""
    d = module_dir(code)
    if not d:
        return False, {"reason": "找不到模块目录"}
    lines = max(1, min(int(lines or 50), 200))
    cands = []
    for root in (os.path.join(d, "data", "logs"), os.path.join(d, "data"), d):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if name.endswith(".log") or name.endswith(".txt"):
                p = os.path.join(root, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        return False, {"reason": "本模块未找到日志文件"}
    cands.sort(reverse=True)
    path = cands[0][1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
    except Exception as e:
        return False, {"reason": "读日志失败：%s" % e}
    return True, {"file": os.path.basename(path), "lines": len(tail),
                  "tail": "".join(tail)[-8000:]}


def run_selftest(code, timeout=600):
    """跑本模块自检脚本，回报退出码与末尾输出"""
    d = module_dir(code)
    if not d:
        return False, {"reason": "找不到模块目录"}
    script = ""
    for name in SELFTEST_SCRIPTS:
        if os.path.isfile(os.path.join(d, name)):
            script = name
            break
    if not script:
        return False, {"reason": "本模块没有自检脚本（找过 %s）" % "、".join(SELFTEST_SCRIPTS)}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.STDOUT, "env": env, "cwd": d}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run([sys.executable, script], timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return False, {"reason": "自检超时（%ds）" % timeout}
    out = (p.stdout or b"").decode("utf-8", "replace")
    tail = "\n".join(out.strip().splitlines()[-25:])
    return p.returncode == 0, {"script": script, "exit_code": p.returncode,
                               "tail": tail[-4000:]}


def execute(code, cmd):
    """执行一条命令，返回 (ok, result)"""
    kind = (cmd or {}).get("kind") or ""
    params = (cmd or {}).get("params") or {}
    if kind not in SUPPORTED:
        return False, {"reason": "本执行器不支持的命令：%s（支持：%s）"
                                 % (kind, "、".join(SUPPORTED))}
    if kind == "ping":
        return True, {"module": code, "device_id": device_id(),
                      "hostname": socket.gethostname(),
                      "platform": platform.platform(),
                      "python": sys.version.split()[0],
                      "cwd_ok": bool(module_dir(code))}
    if kind == "status":
        return run_integration(code, "status", {}, timeout=120)
    if kind == "logs_tail":
        return tail_log(code, params.get("lines") or 50)
    if kind == "selftest":
        return run_selftest(code)
    if kind == "rebuild_index":
        if code != "01":
            return False, {"reason": "rebuild_index 仅 01 支持"}
        return run_integration(code, "status", {}, timeout=180)
    return False, {"reason": "未实现：%s" % kind}


# ---------------------------------------------------------------- 本机 IPv4
def local_ipv4s():
    """本机的全部 IPv4（去重、排除 127.*）。

    为什么要把 IP 告诉终端：指挥端常常只知道"命令发给 192.168.1.20"，
    并不知道这台机器的 EXAM_DEVICE_ID；把本机 IP 一起报上去，终端就能把
    "按 IP 下发的命令"派给我们（两边任一命中即算给它的）。
    """
    ips = []
    for probe in ("", "8.8.8.8"):        # 第二个只用来让系统选出默认出口网卡
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if probe:
                    s.connect((probe, 80))
                else:
                    s.connect(("10.255.255.255", 1))
                ip = s.getsockname()[0]
                if ip and ip not in ips:
                    ips.append(ip)
            finally:
                s.close()
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


# ---------------------------------------------------------------- 主流程
def fetch(url, token, dev, timeout=15, ips=None):
    u = "%s/api/command?target=%s" % (url, urllib.parse.quote(dev))
    if ips:
        u += "&ip=" + urllib.parse.quote(",".join(ips))
    return _http(u, token=token, timeout=timeout)


def post_result(url, token, cmd_id, ok, result, dev):
    payload = {"cmd_id": cmd_id, "ok": bool(ok),
               "result": dict(result or {}, module_device=dev),
               "operator": dev}
    return _http(url + "/api/command_result", method="POST", payload=payload,
                 token=token)


def run_once(code, url, token, dev, verbose=True, ips=None):
    """拉一次待执行命令并逐条执行 + 回执。返回 (ok, 摘要)"""
    if not url:
        return False, "未配置 EXAM_TERMINAL_URL（智能终端地址）"
    if not token:
        return False, "未配置 EXAM_TERMINAL_TOKEN（接入令牌）"
    if ips is None:
        ips = local_ipv4s()
    ok, r = fetch(url, token, dev, ips=ips)
    if not ok:
        return False, "拉取命令失败：%s" % (r.get("reason") or r)
    cmds = r.get("commands") or []
    if not cmds:
        if verbose:
            print("[命令] 无待执行命令（target=%s ip=%s）"
                  % (dev, ",".join(ips) or "-"))
        return True, "无待执行命令"
    done = []
    for c in cmds:
        cid, kind = c.get("cmd_id", ""), c.get("kind", "")
        if verbose:
            print("[命令] 执行 %s（%s）…" % (cid, kind))
        try:
            cok, res = execute(code, c)
        except Exception as e:                        # noqa: BLE001
            cok, res = False, {"reason": "执行异常：%s" % e}
        pok, pr = post_result(url, token, cid, cok, res, dev)
        if verbose:
            print("[命令] %s -> %s；回执%s"
                  % (kind, "成功" if cok else "失败", "已送达" if pok else
                     "失败：%s" % (pr.get("reason") or pr)))
        done.append({"cmd_id": cid, "kind": kind, "ok": cok,
                     "reported": bool(pok)})
    return True, "执行 %d 条" % len(done)


def main(argv=None):
    ap = argparse.ArgumentParser(description="设备端命令执行器（01/02 被智能终端指挥）")
    ap.add_argument("--module", required=True, choices=sorted(MODULE_DIRS),
                    help="本机是哪个模块（01/02/…）")
    ap.add_argument("--url", default=_default_url(), help="智能终端地址（含端口）")
    ap.add_argument("--token", default=_default_token(), help="接入令牌")
    ap.add_argument("--device-id", default="", help="本机标识（默认计算机名）")
    ap.add_argument("--ip", default="",
                    help="本机 IPv4（默认自动探测；多网卡可用逗号分隔）")
    ap.add_argument("--once", action="store_true", help="只拉一次")
    ap.add_argument("--loop", type=int, default=0, help="常驻：每 N 秒拉一次")
    ns = ap.parse_args(argv)

    dev = device_id(ns.device_id)
    ips = [x.strip() for x in (ns.ip or "").split(",") if x.strip()] or local_ipv4s()
    url = (ns.url or "").rstrip("/")
    if not url:
        print("未配置智能终端地址。请设环境变量 EXAM_TERMINAL_URL，或用 --url 指定，")
        print("例如：set EXAM_TERMINAL_URL=http://192.168.31.176:9113")
        return 2
    if not ns.token:
        print("未配置接入令牌。请在 03 上执行 python lan_service.py --print-token 获取，")
        print("然后设环境变量 EXAM_TERMINAL_TOKEN（或 --token）。")
        return 2

    print("设备端命令执行器")
    print("  模块　　：%s（目录：%s）" % (ns.module, module_dir(ns.module) or "未找到"))
    print("  本机标识：%s" % dev)
    print("  本机 IPv4：%s" % (", ".join(ips) or "未探测到（可用 --ip 指定）"))
    print("  终端　　：%s" % url)
    print("  支持命令：%s" % "、".join(SUPPORTED))
    print("  提示：智能终端【发布命令】时，目标填上面任一 IPv4 或本机标识都能收到。")
    if ns.loop and ns.loop > 0:
        print("  常驻模式：每 %d 秒拉一次（Ctrl+C 退出）" % ns.loop)
        while True:
            try:
                ok, msg = run_once(ns.module, url, ns.token, dev, ips=ips)
                if not ok:
                    print("[命令] %s" % msg)
            except KeyboardInterrupt:
                print("已退出")
                return 0
            time.sleep(max(3, ns.loop))
    ok, msg = run_once(ns.module, url, ns.token, dev, ips=ips)
    print("[命令] %s" % msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
