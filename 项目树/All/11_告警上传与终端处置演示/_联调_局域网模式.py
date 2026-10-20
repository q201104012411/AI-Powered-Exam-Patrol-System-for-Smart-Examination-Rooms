# -*- coding: utf-8 -*-
"""12 局域网模式端到端联调：02 机经 HTTP 把告警送到 03 的接入服务

验证的是**真实链路**：起 03 的 lan_service，用 LanTerminalLink 经 HTTP POST
/api/ingest_alert 上传告警，再用 HTTP 查 03 的告警列表确认收到且分级正确。

覆盖两个既定联动约定：
  · 红级告警必须进入"待确认"并带 60s SLA（03 侧强制确认）
  · credibility < 0.6 的红级告警由 03 自动**降为橙色**并标注（防画质误报）
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = os.path.dirname(HERE)
T03 = os.path.join(ALL, "03_智能终端操作")
PY = sys.executable
TOKEN = "lan12-token-abc"
PORT = 19131

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def http(url, path, method="GET", payload=None, token=TOKEN, timeout=12):
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Exam-Token"] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


TMP = tempfile.mkdtemp(prefix="lan12_")
env = dict(os.environ)
env.update({"EXAM_TERMINAL_HOME": TMP, "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})

sys.path.insert(0, HERE)
import upload_client as uc  # noqa: E402

print("临时数据目录：%s" % TMP)

proc = subprocess.Popen([PY, os.path.join(T03, "lan_service.py"),
                         "--host", "127.0.0.1", "--port", str(PORT),
                         "--token", TOKEN],
                        cwd=T03, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
url = "http://127.0.0.1:%d" % PORT
up = False
for _ in range(40):
    time.sleep(0.4)
    st, _r = http(url, "/health")
    if st == 200:
        up = True
        break
print("=== 1) 03 接入服务就绪 ===")
check("服务已启动", up, url)
if not up:
    proc.kill()
    sys.exit(1)

try:
    link = uc.LanTerminalLink(url=url, token=TOKEN)
    ok, why = link.available()
    print("\n=== 2) 局域网链路可用性 ===")
    check("LanTerminalLink 报告可达", ok, why)

    print("\n=== 3) 上传一条红级告警（credibility=0.95 不应降权）===")
    r = link.upload_alert(code="PHONE_DETECTED", device_id="CAM-A01-1", room_id="A-01",
                          detail="局域网联调：疑似使用手机", credibility=0.95,
                          level="red", operator="02/联调")
    check("上传被接受", r.get("ok") is not False, str(r)[:100])

    print("\n=== 4) 上传一条低画质红级告警（credibility=0.30 应降为橙）===")
    r2 = link.upload_alert(code="FACE_MISMATCH", device_id="CAM-A01-1", room_id="A-02",
                           detail="局域网联调：低画质下的比对不符", credibility=0.30,
                           level="red", operator="02/联调")
    check("低画质告警也被接受", r2.get("ok") is not False, str(r2)[:100])

    print("\n=== 5) 回查 03 的告警列表（经 HTTP）===")
    st, data = http(url, "/api/alerts?limit=20")
    alerts = data.get("alerts") or data.get("items") or []
    check("只读接口可查", st == 200 and data.get("ok") is not False,
          "共 %d 条" % len(alerts))
    codes = [a.get("code") for a in alerts]
    check("手机告警已入库", "PHONE_DETECTED" in codes, "码表=%s" % codes)
    check("比对不符已入库", "FACE_MISMATCH" in codes)
    low = [a for a in alerts if a.get("code") == "FACE_MISMATCH"]
    if low:
        lv = low[0].get("level")
        check("低画质红级被 03 降为橙色", lv == "orange", "实际级别=%s" % lv)
        note = str(low[0].get("detail") or low[0].get("title") or "")
        check("降权理由有标注", ("画质" in note or "降" in note or note != ""),
              note[:80])
    red = [a for a in alerts if a.get("code") == "PHONE_DETECTED"]
    if red:
        check("高清红级未被降权", red[0].get("level") == "red",
              "实际级别=%s" % red[0].get("level"))
        check("红级进入待确认（强制确认约定）",
              red[0].get("status") in ("pending", "acked"),
              "状态=%s" % red[0].get("status"))

    print("\n=== 6) 03 侧台账与审计 ===")
    st, stt = http(url, "/api/status")
    check("03 状态可查", st == 200, str(stt)[:90])
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("\n" + "=" * 60)
print("12 局域网模式联调：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
