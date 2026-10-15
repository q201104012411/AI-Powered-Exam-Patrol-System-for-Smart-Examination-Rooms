# -*- coding: utf-8 -*-
"""发布命令 · 真实 HTTP 端到端联调

链路（全部走真实网络与真实进程）：
  1) 起 03 的局域网接入服务（ThreadingHTTPServer，独立进程）
  2) 用 HTTP POST /api/command 下发命令（带 X-Exam-Token）
  3) 用 00_集成联动/command_runner.py 以独立进程拉取（GET /api/command）
     并执行，再回执（POST /api/command_result）
  4) 回到 03 侧查台账，确认回执与状态
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ALL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T03 = os.path.join(ALL, "03_智能终端操作")
T00 = os.path.join(ALL, "00_集成联动")
PY = sys.executable
TOKEN = "e2e-token-123"
PORT = 19123
DEV = "PC-E2E-01"

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def http(url, method="GET", payload=None, token=TOKEN, timeout=10):
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Exam-Token"] = token
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


TMP = tempfile.mkdtemp(prefix="cmd_e2e_")
env = dict(os.environ)
env["EXAM_TERMINAL_HOME"] = TMP
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["PYTHONDONTWRITEBYTECODE"] = "1"

print("临时数据目录：%s" % TMP)
print("服务端口：%d\n" % PORT)

# ---- 1) 起服务 ----
proc = subprocess.Popen(
    [PY, os.path.join(T03, "lan_service.py"), "--host", "127.0.0.1",
     "--port", str(PORT), "--token", TOKEN],
    cwd=T03, env=env, stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
url = "http://127.0.0.1:%d" % PORT
up = False
for _ in range(40):
    time.sleep(0.4)
    try:
        st, _r = http(url + "/health")
        if st == 200:
            up = True
            break
    except Exception:
        pass
print("=== 1) 服务启动 ===")
check("局域网接入服务已就绪", up, url)
if not up:
    proc.kill()
    print("服务未能启动，退出")
    sys.exit(1)

try:
    print("\n=== 2) 鉴权：回环免令牌（按设计），跨设备 fail-closed ===")
    # 说明：服务设计是"本机(回环)免令牌；跨设备一律要令牌，没配令牌就直接拒"。
    # 所以 HTTP 层面从 127.0.0.1 发起的无令牌请求本来就应当放行，
    # 这里直接验证鉴权函数本身（比对真实客户端 IP 的判定路径）。
    sys.path.insert(0, T03)
    from lan_service import LanService  # noqa: E402
    import storage as _st               # noqa: E402
    _svc = LanService(store=_st.Store(), token=TOKEN)
    ok_loop, why_loop = _svc.authorized("127.0.0.1", {}, "/api/command")
    check("回环地址免令牌（设计如此）", ok_loop, why_loop)
    ok_lan, why_lan = _svc.authorized("192.168.31.50", {}, "/api/command")
    check("跨设备无令牌被拒（fail-closed）", not ok_lan, why_lan)
    ok_tok, why_tok = _svc.authorized("192.168.31.50", {"X-Exam-Token": TOKEN},
                                      "/api/command")
    check("跨设备带正确令牌放行", ok_tok, why_tok)
    ok_bad, why_bad = _svc.authorized("192.168.31.50", {"X-Exam-Token": "wrong"},
                                      "/api/command")
    check("跨设备带错误令牌被拒", not ok_bad, why_bad)
    _no_token = LanService(store=_st.Store(), token="")
    ok_none, why_none = _no_token.authorized("192.168.31.50", {}, "/api/command")
    check("未配置令牌时跨设备直接拒（不是放行）", not ok_none, why_none)

    print("\n=== 3) HTTP 下发命令 ===")
    st, r = http(url + "/api/command", "POST",
                 {"target": DEV, "kind": "ping", "operator": "owner"})
    check("下发成功", st == 200 and r.get("ok"), r.get("cmd_id", ""))
    cid = r.get("cmd_id", "")
    st, r2 = http(url + "/api/command", "POST",
                  {"target": DEV, "kind": "format_c_drive"})
    check("白名单外命令被拒", st == 400 and not r2.get("ok"),
          str(r2.get("reason"))[:70])

    print("\n=== 4) 设备端执行器（独立进程）拉取并执行 ===")
    cp = subprocess.run(
        [PY, os.path.join(T00, "command_runner.py"), "--module", "02",
         "--url", url, "--token", TOKEN, "--device-id", DEV, "--once"],
        cwd=T00, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    out = (cp.stdout or b"").decode("utf-8", "replace")
    print("    " + "\n    ".join(out.strip().splitlines()[-4:]))
    check("执行器运行成功", cp.returncode == 0, "退出码 %s" % cp.returncode)
    check("执行器报告已执行命令", "执行" in out and "ping" in out)

    print("\n=== 5) 回执已回到 03（HTTP 查台账）===")
    st, r = http(url + "/api/commands?limit=20")
    cmds = r.get("commands") or []
    mine = [c for c in cmds if c.get("cmd_id") == cid]
    check("台账可查", st == 200 and r.get("ok"), "共 %d 条" % len(cmds))
    check("找到该命令", len(mine) == 1)
    if mine:
        c = mine[0]
        check("状态 ok", c.get("status") == "ok")
        check("已标记执行", int(c.get("applied") or 0) == 1)
        ro = c.get("result_obj") or {}
        check("回执含设备回报的 hostname", bool(ro.get("hostname")), str(ro)[:90])
    st, r = http(url + "/api/command?target=%s" % DEV)
    check("已执行的命令不再重复下发", not [c for c in (r.get("commands") or [])
                                      if c.get("cmd_id") == cid])
    st, r = http(url + "/api/status")
    check("原有只读接口未受影响", st == 200 and r.get("ok") is not False)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("\n" + "=" * 60)
print("发布命令 HTTP 端到端联调：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
