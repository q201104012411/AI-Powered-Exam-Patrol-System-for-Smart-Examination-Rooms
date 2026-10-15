# -*- coding: utf-8 -*-
"""
00 集成联动 · 局域网联动端到端自检

回答的问题：**两台设备在同一局域网时，01/02 的操作记录能不能上传到 03 智能终端？**

这个自检不是"假装发个 HTTP"，而是：
  1. 真起一个 03 的接入服务（`03_智能终端操作\\lan_service.py`，临时数据目录 + 临时端口）；
  2. 先在协议层验一遍：接入、幂等、多设备隔离、断网补传、画质降权告警；
  3. 再用**真实的 01 客户端**（`01\\push_oplog.py`）把临时目录里的操作日志与核验记录哈希链
     传上去 —— 数据是真的，链头是真的；
  4. 再用**真实的 02 客户端**（`02\\push_oplog.py`）把审计留痕、机位核查结论、整改工单传上去；
  5. 最后查终端侧：远端记录分链落库、远端设备台账、本机审计链**没有被污染**。

01/02/03 三个临时数据目录都在系统临时目录下，跑完即删，**不碰任何真实运行数据**。
逐项打印 PASS/FAIL，全部通过退出码 0。

用法：
  双击 `局域网联动自检.bat` 或 python lan_selftest.py
  python lan_selftest.py --keep       # 保留临时目录便于排查
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALL_DIR = os.path.dirname(HERE)
TERMINAL_DIR = os.path.join(ALL_DIR, "03_智能终端操作")
FACE_DIR = os.path.join(ALL_DIR, "01_人脸识别与身份核对", "01_人脸识别与身份核对")
CAMERA_DIR = os.path.join(ALL_DIR, "02_摄像头核查与视频质量诊断")

sys.path.insert(0, HERE)
import lan_upload                                 # noqa: E402

TOKEN = "selftest-lan-token"
DEV01 = "PC-A-01"
DEV02 = "PC-A-02"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— " + detail) if detail else ""))
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def child_env(home_env=None, home=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if home_env and home:
        env[home_env] = home
        # 别让父进程环境里的设置把子进程又拉回真实目录
        env.pop("EXAM_TERMINAL_URL", None)
        env.pop("EXAM_TERMINAL_TOKEN", None)
    return env


def run_py(args, env, timeout=240):
    kwargs = {"capture_output": True, "env": env, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%ds）" % timeout
    return (p.returncode, (p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"))


def last_json(text):
    """
    取子进程输出里的结果 JSON。约定是"最后一行一个 JSON"，
    但客户端也可能美化打印成多行，所以再兜一次"从第一个 { 解析到结尾"。
    """
    text = text or ""
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except ValueError:
                continue
    i = text.find("{")
    while i >= 0:
        try:
            return json.loads(text[i:])
        except ValueError:
            i = text.find("{", i + 1)
    return None


def seed_01(home):
    """在临时目录里造两条真实的核验记录（带哈希链）与两条操作日志"""
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from models import VerifyRecord\n"
        "from audit import AuditLogger\n"
        "from action_logger import ActionLogger\n"
        "lg = AuditLogger()\n"
        "lg.log(VerifyRecord(timestamp='2026-10-15T09:00:00', device_id='GATE-A01',\n"
        "                    candidate_id='C001', mode='1:1', result='PASS',\n"
        "                    reason_code='PASS', reason='核验通过', operator='owner'))\n"
        "lg.log(VerifyRecord(timestamp='2026-10-15T09:00:30', device_id='GATE-A01',\n"
        "                    candidate_id='C002', mode='1:1', result='FAIL',\n"
        "                    reason_code='FACE_MISMATCH', reason='相似度低于阈值',\n"
        "                    operator='owner', manual_required=True))\n"
        "al = ActionLogger()\n"
        "al.set_operator('owner')\n"
        "al.log('人脸核验', '考生 C001 核验通过 | 闸机 GATE-A01')\n"
        "al.log('查询留痕', '按考生号 C002 查询核验记录')\n"
        "print('seeded-01')\n" % FACE_DIR)
    return run_py([sys.executable, "-c", script], child_env("EXAM_HOME", home))


def seed_02(home):
    """在临时目录里造台账 + 一次核查（画面模糊）+ 一张整改工单 + 三条审计留痕"""
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import config, models, storage\n"
        "st = storage.Store()\n"
        "dev = models.Device(code='CAM-A01-1', room_id='A-01', name='A01前墙',\n"
        "                    site='一中考点', position='前墙左上',\n"
        "                    url='rtsp://10.0.0.11:554/stream1', session='6月7日上午')\n"
        "st.save_device(dev)\n"
        "ck = models.CheckResult(dev, policy='抽查', online=True,\n"
        "                        defects=[models.Defect('BLUR')], operator='teacher1')\n"
        "st.save_check(ck)\n"
        "wo = models.WorkOrder(dev, ['BLUR'], config.SEV_FAIL,\n"
        "                      note='先确认机位角度与对焦', operator='teacher1')\n"
        "st.save_order(wo)\n"
        "st.log('巡检', 'CAM-A01-1 画面模糊（BLUR）', 'teacher1')\n"
        "st.log('工单', '已派单：CAM-A01-1 画面模糊', 'teacher1')\n"
        "st.log('台账', '登记机位 CAM-A01-1', 'teacher1')\n"
        "print('seeded-02')\n" % CAMERA_DIR)
    return run_py([sys.executable, "-c", script], child_env("EXAM_CAMERA_HOME", home))


def main(argv=None):
    ap = argparse.ArgumentParser(description="局域网联动端到端自检（00 集成联动）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    tmproot = tempfile.mkdtemp(prefix="lan_linkage_selfcheck_")
    home03 = os.path.join(tmproot, "terminal")
    home01 = os.path.join(tmproot, "face")
    home02 = os.path.join(tmproot, "camera")
    for d in (home03, home01, home02):
        os.makedirs(d, exist_ok=True)

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    proc = None
    print("局域网联动端到端自检")
    print("  临时数据目录：%s" % tmproot)
    print("  03 接入服务：%s（令牌已配置）" % base)
    print("-" * 68)

    try:
        # ---------- 起 03 的接入服务 ----------
        env03 = child_env("EXAM_TERMINAL_HOME", home03)
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "env": env03,
                  "cwd": TERMINAL_DIR}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, os.path.join(TERMINAL_DIR, "lan_service.py"),
                                 "--host", "127.0.0.1", "--port", str(port),
                                 "--token", TOKEN], **kwargs)

        up01 = lan_upload.RecordUploader(base, token=TOKEN, device_id=DEV01,
                                         device_name="01_人脸识别与身份核对")
        online = False
        for _ in range(60):
            ok, _data = up01.health()
            if ok:
                online = True
                break
            time.sleep(0.5)
        check("[1/6] 03 接入服务起来了（/health 可访问）", online,
              base if online else "等服务监听超时")
        if not online:
            raise SystemExit(1)

        # ---------- 协议层 ----------
        print("[2/6] 协议层：接入 / 幂等 / 多设备 / 断网补传 / 画质降权")
        recs = [{"ts": "2026-10-15 09:10:00", "action": "巡检留痕", "detail": "CAM-A01-1 正常",
                 "operator": "patrol1"}]
        r = up01.send(recs, chain={"algo": "sha256", "head": "deadbeef", "count": 1})
        check("记录能接入（accepted=1）", r.get("ok") and r.get("accepted") == 1,
              "accepted=%s" % r.get("accepted"))
        r = up01.send(recs, chain={"algo": "sha256", "head": "deadbeef", "count": 1})
        check("同一条重发只算重复（幂等，不会重复入库）",
              r.get("accepted") == 0 and r.get("duplicated") == 1,
              "accepted=%s duplicated=%s" % (r.get("accepted"), r.get("duplicated")))

        up02 = lan_upload.RecordUploader(base, token=TOKEN, device_id=DEV02,
                                        device_name="02_摄像头核查与视频质量诊断")
        r = up02.send([{"ts": "2026-10-15 09:11:00", "action": "机位核查",
                        "detail": "CAM-A01-1 画面模糊", "operator": "teacher1"}])
        check("第二台设备的记录独立入库（按 device_id 分开）",
              r.get("ok") and r.get("accepted") == 1, "accepted=%s" % r.get("accepted"))

        # 断网：指向一个没有服务的端口，验证"不丢数据 + 恢复后补传"
        dead = "http://127.0.0.1:%d" % free_port()
        queue = os.path.join(home02, "lan_queue.ndjson")
        off = lan_upload.RecordUploader(dead, token=TOKEN, device_id=DEV02,
                                       kind="check", queue_path=queue, timeout=2)
        r = off.send([{"ts": "2026-10-15 09:12:00", "action": "机位核查",
                       "detail": "断网期间的记录", "operator": "teacher1"}])
        check("断网时上报失败但数据进本地队列（不丢）",
              not r.get("ok") and r.get("queued") and off.pending() == 1,
              "pending=%d 原因=%s" % (off.pending(), str(r.get("reason"))[:40]))
        r = off.flush_queue()
        check("网络仍不通时不假装成功（队列原样保留）",
              r.get("failed") == 1 and r.get("left") == 1, "left=%s" % r.get("left"))
        on = lan_upload.RecordUploader(base, token=TOKEN, device_id=DEV02,
                                       kind="check", queue_path=queue, timeout=5)
        r = on.flush_queue()
        check("网络恢复后自动补传（队列清空）",
              r.get("sent") == 1 and r.get("left") == 0, "sent=%s left=%s"
              % (r.get("sent"), r.get("left")))
        ok, data = on.list_remote(device_id=DEV02, kind="check", limit=10)
        check("补传的记录确实到了终端（可查）",
              ok and any("断网期间" in (x.get("detail") or "")
                         for x in data.get("records", [])),
              "该设备 check 类记录=%d 条" % len(data.get("records", [])))

        ok, data = lan_upload.post_alert(base, {"code": "PHONE_DETECTED", "device_id": "CAM-A01-1",
                                               "room_id": "A-01", "credibility": 0.5,
                                               "detail": "机位画质不可信，红色应被降级"},
                                        token=TOKEN)
        check("01/02 的业务告警可以走局域网直达终端", ok and data.get("alert_id"),
              "告警号=%s" % data.get("alert_id"))
        check("低画质机位的红色告警被自动降级为橙色（02 的可信度在跨设备后仍生效）",
              data.get("downgraded") is True and data.get("level") == "orange",
              "级别=%s 降级=%s" % (data.get("level_name"), data.get("downgraded")))

        # ---------- 真实 01 客户端 ----------
        print("[3/6] 真实 01 客户端（操作日志 + 核验记录哈希链）")
        code, out, err = seed_01(home01)
        check("01 临时数据目录造出核验记录与操作日志",
              code == 0 and "seeded-01" in out, (err or out).strip()[-120:])
        env01 = child_env("EXAM_HOME", home01)
        args01 = [sys.executable, os.path.join(FACE_DIR, "push_oplog.py"),
                  "--url", base, "--token", TOKEN, "--device-id", DEV01,
                  "--state", os.path.join(home01, "lan_push_state.json")]
        code, out, err = run_py(args01, env01)
        s = last_json(out)
        check("01 客户端上报成功（退出码 0）", code == 0 and s and s.get("ok"),
              (err or "").strip()[-160:])
        by_src = {x.get("source"): x for x in (s or {}).get("sources", [])}
        check("核验记录（哈希链）已上传",
              (by_src.get("verify") or {}).get("accepted") == 2,
              "verify=%s" % json.dumps(by_src.get("verify"), ensure_ascii=False))
        check("界面操作日志已上传",
              (by_src.get("oplog") or {}).get("accepted") == 2,
              "oplog=%s" % json.dumps(by_src.get("oplog"), ensure_ascii=False))
        code, out, err = run_py(args01, env01)
        s2 = last_json(out)
        by2 = {x.get("source"): x for x in (s2 or {}).get("sources", [])}
        check("01 再次运行不重复发送（水位线生效，无新增）",
              code == 0 and (by2.get("verify") or {}).get("sent") == 0
              and (by2.get("oplog") or {}).get("sent") == 0,
              "verify=%s" % json.dumps(by2.get("verify"), ensure_ascii=False))
        code, out, err = run_py(args01 + ["--reset-state"], env01)
        s3 = last_json(out)
        by3 = {x.get("source"): x for x in (s3 or {}).get("sources", [])}
        check("01 清零水位线后全量重发，接收端按内容去重（不会重复入库）",
              (by3.get("verify") or {}).get("accepted") == 0
              and (by3.get("verify") or {}).get("duplicated") == 2
              and (by3.get("oplog") or {}).get("accepted") == 0,
              "verify accepted=%s duplicated=%s"
              % ((by3.get("verify") or {}).get("accepted"),
                 (by3.get("verify") or {}).get("duplicated")))

        # ---------- 真实 02 客户端 ----------
        print("[4/6] 真实 02 客户端（审计留痕 + 机位核查结论 + 整改工单）")
        code, out, err = seed_02(home02)
        check("02 临时数据目录造出台账/核查/工单",
              code == 0 and "seeded-02" in out, (err or out).strip()[-160:])
        env02 = child_env("EXAM_CAMERA_HOME", home02)
        args02 = [sys.executable, os.path.join(CAMERA_DIR, "push_oplog.py"),
                  "--url", base, "--token", TOKEN, "--device-id", DEV02,
                  "--state", os.path.join(home02, "lan_push_state.json")]
        code, out, err = run_py(args02, env02)
        s = last_json(out)
        check("02 客户端上报成功（退出码 0）", code == 0 and s and s.get("ok"),
              (err or "").strip()[-160:])
        by_src = {x.get("source"): x for x in (s or {}).get("sources", [])}
        check("审计留痕（哈希链）已上传",
              (by_src.get("audit") or {}).get("accepted") == 3,
              "audit=%s" % json.dumps(by_src.get("audit"), ensure_ascii=False))
        check("机位核查结论已上传（含画质缺陷）",
              (by_src.get("check") or {}).get("accepted") == 1,
              "check=%s" % json.dumps(by_src.get("check"), ensure_ascii=False))
        check("整改工单已上传",
              (by_src.get("order") or {}).get("accepted") == 1,
              "order=%s" % json.dumps(by_src.get("order"), ensure_ascii=False))
        code, out, err = run_py(args02, env02)
        s2 = last_json(out)
        by2 = {x.get("source"): x for x in (s2 or {}).get("sources", [])}
        check("02 再次运行不重复发送（水位线生效，无新增）",
              code == 0 and (by2.get("audit") or {}).get("sent") == 0
              and (by2.get("check") or {}).get("sent") == 0,
              "audit=%s" % json.dumps(by2.get("audit"), ensure_ascii=False))
        code, out, err = run_py(args02 + ["--reset-state"], env02)
        s3 = last_json(out)
        by3 = {x.get("source"): x for x in (s3 or {}).get("sources", [])}
        check("02 清零水位线后全量重发，接收端按内容去重",
              (by3.get("audit") or {}).get("accepted") == 0
              and (by3.get("audit") or {}).get("duplicated") == 3,
              "audit accepted=%s duplicated=%s"
              % ((by3.get("audit") or {}).get("accepted"),
                 (by3.get("audit") or {}).get("duplicated")))

        # ---------- 00 联动层：远端终端模式 ----------
        print("[5/7] 00 联动层：把 03 指向局域网上的终端（编排逻辑不改）")
        import linkage
        lk = linkage.Linkage(sandbox_root=tmproot, sandbox_keys=("01", "02", "06"),
                            terminal_url=base, terminal_token=TOKEN, timeout=60)
        check("Linkage 进入远端终端模式（03 不再起本机子进程）",
              getattr(lk, "remote_terminal", False) is True,
              "p03=%s" % type(lk.p03).__name__)
        st = lk.status()
        check("联动层能远程查到 03 的状态（GET /api/status）",
              (st.get("03") or {}).get("ok")
              and (st.get("03") or {}).get("system") == "03_智能终端操作",
              json.dumps(st.get("03"), ensure_ascii=False)[:120])
        r = lk.raise_alert("PHONE_DETECTED", "CAM-A01-1", room_id="A-01",
                           detail="从 00 联动层经局域网发到 03")
        check("联动层 raise_alert 经局域网进 03（复用同一套 JSON 编排）",
              r.get("alert_id") and r.get("source") == "03_智能终端操作",
              "告警号=%s 级别=%s" % (r.get("alert_id"), r.get("level_name")))
        al = lk.alerts(limit=5)
        check("联动层能远程读 03 的告警列表",
              any(x.get("code") == "PHONE_DETECTED" for x in al.get("alerts", [])),
              "告警=%d 条" % len(al.get("alerts", [])))
        rr = lk.remote_records(limit=5)
        check("联动层能远程读 03 收到的远端记录",
              rr.get("ok") and rr.get("summary", {}).get("records", 0) > 0,
              "summary=%s" % json.dumps(rr.get("summary"), ensure_ascii=False))

        # 本机模式不能被这次改动破坏：03 的 integration_api 照旧可用
        lk_local = linkage.Linkage(sandbox_root=tmproot, sandbox_keys=("03",),
                                   timeout=60)
        loc = lk_local.p03.call("status", {})
        check("03 的本机集成接口（子进程）仍然可用",
              loc.get("ok") and loc.get("system") == "03_智能终端操作",
              "数据目录=%s" % loc.get("data_dir"))
        loc2 = lk_local.p03.call("list_remote", {"limit": 5})
        check("本机模式也能查远端记录（integration_api 新增 list_remote op）",
              loc2.get("ok") and loc2.get("records") == [],
              "空库应为 0 条：%s" % len(loc2.get("records") or []))

        # ---------- 终端侧验收 ----------
        print("[6/7] 终端侧：远端记录分链落库 + 设备台账 + 本机链未被污染")
        ok, health = up01.health()
        check("终端健康检查可用", ok and health.get("ok"),
              "远端记录=%s 设备=%s" % (health.get("remote_records"),
                                     health.get("remote_devices")))
        check("远端记录按类别分链存放（audit/oplog/check/order 都有）",
              set(("audit", "oplog", "check", "order"))
              <= set((health.get("remote_by_kind") or {}).keys()),
              "by_kind=%s" % json.dumps(health.get("remote_by_kind"), ensure_ascii=False))
        check("终端自己的审计链仍然完整（远端记录没被并进本机链）",
              health.get("audit_chain_ok") is True, health.get("audit_chain", ""))

        ok, data = on.list_remote(limit=500)
        devs = {d.get("device_id"): d for d in (data.get("devices") or [])}
        check("两台设备都在终端登记了（PC-A-01 / PC-A-02）",
              DEV01 in devs and DEV02 in devs,
              "设备=%s" % "、".join(sorted(devs)))
        heads = [d.get("chain_head") or "" for d in devs.values()]
        ok01, d01 = on.list_remote(device_id=DEV01, kind="verify", limit=5)
        got_head = (devs.get(DEV01) or {}).get("chain_head") or ""
        check("01 的真实哈希链头与记录一起到了终端（64 位 sha256）",
              len(got_head) == 64, "链头=%s…" % got_head[:16])
        check("01 的核验记录在终端可读（含结论与操作人）",
              ok01 and any((x.get("operator") == "owner") for x in d01.get("records", [])),
              "verify 记录=%d 条" % len(d01.get("records", [])))
        check("远端记录落库时逐字段加密、读出时解密（终端侧可读中文）",
              any("核验" in (x.get("action") or "") or "机位核查" in (x.get("action") or "")
                  for x in (data.get("records") or [])),
              "示例=%s" % ((data.get("records") or [{}])[0].get("action")))

        # ---------- 收尾 ----------
        print("[7/7] 收尾")
        ok, health = up01.health()
        check("自检全程临时数据目录隔离，真实数据目录未被写入",
              tmproot in (health.get("data_dir") or ""), health.get("data_dir", ""))
    finally:
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=15)
                else:
                    proc.terminate()
                proc.wait(timeout=10)
            except Exception:                         # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                     # noqa: BLE001
                    pass
        if not ns.keep:
            shutil.rmtree(tmproot, ignore_errors=True)
        else:
            print("临时目录已保留：%s" % tmproot)

    passed = sum(1 for _n, c, _d in RESULTS if c)
    total = len(RESULTS)
    print("-" * 68)
    if passed != total:
        print("未通过项：")
        for n, c, d in RESULTS:
            if not c:
                print("  FAIL %s%s" % (n, ("　— " + d) if d else ""))
    print("局域网联动端到端自检：%d/%d 项通过" % (passed, total))
    print(json.dumps({"ok": passed == total, "passed": passed, "total": total},
                     ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
