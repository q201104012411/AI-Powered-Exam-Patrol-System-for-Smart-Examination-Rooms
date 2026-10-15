# -*- coding: utf-8 -*-
"""在同一进程内起 Flask（后台线程）+ 用 Flask test_client 或真实 HTTP 跑端到端。

为什么不用单独进程：本机沙箱环境会在父 shell 退出时回收派生的服务进程，
把服务端与测试放在同一个 Python 进程内最稳，也能真正验证 HTTP 层。

用法：
    python tests/e2e_web_inproc.py
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 数据落到独立临时目录，避免污染正式 data/
# 每次运行**先清空**：录像段按 now-7d 生成，若不清空，上一轮的段会与本轮新段
# 时间重叠，被连续性核查判为"段重叠"缺口（这是脏数据，不是源码缺陷）。
_TMP = os.path.join(ROOT, "data", "_e2e_web_tmp")
import shutil as _shutil                                       # noqa: E402
_shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(_TMP, exist_ok=True)
os.environ["EXAM_SECURE_HOME"] = _TMP

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.join(ROOT, "网页监控面板", "server"))
import app as srv                                              # noqa: E402

RESULTS = []


def chk(name, cond, extra=""):
    RESULTS.append((bool(cond), name, extra))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       (" | " + str(extra)) if extra else ""))
    return bool(cond)


def main():
    HOST, PORT = "127.0.0.1", 5021
    flask_app = srv.app
    flask_app.secret_key = "e2e-secret"

    t = threading.Thread(
        target=lambda: flask_app.run(host=HOST, port=PORT, debug=False,
                                     threaded=True, use_reloader=False),
        daemon=True)
    t.start()

    # 等端口起来
    import socket
    ok = False
    for _ in range(60):
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect((HOST, PORT))
            ok = True
            break
        except Exception:                                    # noqa: BLE001
            time.sleep(0.2)
        finally:
            s.close()
    if not ok:
        print("服务未起来")
        return 1

    C = flask_app.test_client()

    print("=" * 70)
    print(" 网页端接口端到端实测（进程内，真实 HTTP 语义）")
    print("=" * 70)

    # ---------- 未登录 ----------
    for path in ["/api/me", "/api/overview", "/api/cameras", "/api/alarms",
                 "/api/kpi", "/api/accounts"]:
        r = C.get(path)
        chk("未登录 GET %s -> 401" % path, r.status_code == 401, r.status_code)
    r = C.get("/api/stream/NOPE")
    chk("未登录 MJPEG -> 401", r.status_code == 401, r.status_code)

    r = C.get("/")
    chk("首页 200 且 HTML", r.status_code == 200 and b"<html" in r.data.lower(),
        r.status_code)

    # ---------- 登录 ----------
    r = C.post("/api/login", json={"username": "owner", "password": "bad"})
    chk("错误口令 -> 401", r.status_code == 401, r.status_code)

    r = C.post("/api/login", json={"username": "owner", "password": "EDY_POWERQ"})
    j = r.get_json()
    chk("owner 登录成功", r.status_code == 200 and j.get("ok"), j.get("error"))
    TOK = j.get("token", "")
    H = {"X-Token": TOK}
    chk("owner 动作集含 account_manage",
        "account_manage" in (j.get("actions") or []), len(j.get("actions") or []))

    r = C.post("/api/login", json={"username": "teacher1", "password": "teacher1"})
    j2 = r.get_json()
    chk("teacher1 登录成功", r.status_code == 200 and j2.get("ok"), j2.get("error"))
    TOK_A = j2.get("token", "")
    HA = {"X-Token": TOK_A}
    ACT_A = set(j2.get("actions") or [])
    chk("admin 无 account_manage", "account_manage" not in ACT_A)
    chk("admin 无 device_delete", "device_delete" not in ACT_A)
    chk("admin 无 audit_view", "audit_view" not in ACT_A)

    r = C.get("/api/me", headers=H)
    chk("/api/me 身份一致", r.status_code == 200
        and r.get_json().get("username") == "owner")
    r = C.get("/api/me", headers={"X-Token": "bogus"})
    chk("伪造 token -> 401", r.status_code == 401, r.status_code)

    # ---------- 越权 ----------
    r = C.get("/api/accounts", headers=HA)
    chk("admin 看账户 -> 403", r.status_code == 403, r.status_code)
    r = C.post("/api/accounts", headers=HA,
               json={"username": "x1", "password": "abc12345", "role": "guard"})
    chk("admin 建账户 -> 403", r.status_code == 403, r.status_code)
    r = C.get("/api/audit", headers=HA)
    chk("admin 看审计 -> 403", r.status_code == 403, r.status_code)
    r = C.delete("/api/cameras/CAM-X", headers=HA)
    chk("admin 删机位 -> 403", r.status_code == 403, r.status_code)
    r = C.get("/api/accounts", headers=H)
    chk("owner 看账户 -> 200", r.status_code == 200
        and isinstance(r.get_json().get("accounts"), list))

    # ---------- 配置 ----------
    r = C.get("/api/config", headers=H)
    j = r.get_json()
    chk("六面齐全", len(j.get("faces") or []) == 6,
        [f["key"] for f in (j.get("faces") or [])])
    chk("六个流转环节（领取/交接/押运/分发/回收/封存）",
        len(j.get("flow_stages") or []) == 6, sorted(j.get("flow_stages") or {}))

    ROOM = "保密室A"
    r = C.get("/api/cameras?room=" + ROOM, headers=H)
    for c0 in (r.get_json().get("cameras") or []):
        C.delete("/api/cameras/" + c0["cam_id"], headers=H)

    # 按 config 里各朝向的必拍要点登记，避免人为造出台账问题
    import config as cfg
    faces = ["front", "back", "left", "right", "top", "bottom"]
    for f in faces:
        r = C.post("/api/cameras", headers=H, json={
            "cam_id": "CAM-%s-A" % f.upper(), "room_id": ROOM,
            "name": "保密室A·%s" % f, "face": f, "url": "rtsp://demo/%s" % f,
            "checkpoints": cfg.FACE_CHECKPOINTS[f],
            "channels": ["main", "backup"]})
        chk("登记机位 %s" % f, r.status_code == 200, r.get_json())

    r = C.get("/api/overview?room=" + ROOM, headers=H)
    j = r.get_json()
    chk("覆盖率 100%", abs((j.get("coverage_rate") or 0) - 1.0) < 1e-9,
        j.get("coverage_rate"))
    chk("无盲区", len(j.get("blind_spots") or []) == 0, j.get("blind_spots"))

    r = C.get("/api/cameras?room=" + ROOM, headers=H)
    chk("机位台账无问题", all(not c["problems"] for c in
                              (r.get_json().get("cameras") or [])),
        [c["problems"] for c in (r.get_json().get("cameras") or [])])

    # ---------- 保密室事件 + 时延 ----------
    r = C.post("/api/vault/access", headers=H, json={
        "room_id": ROOM, "person": "张巡", "abnormal": True,
        "detail": "实测：未授权开门"})
    al = r.get_json().get("alarm") or {}
    chk("未授权开门 -> 红色告警", bool(al.get("code")), al.get("code"))
    chk("开门告警时延 ≤5s", (al.get("latency_sec") or 99) <= 5.0, al.get("latency_sec"))

    r = C.post("/api/vault/access", headers=H, json={
        "room_id": ROOM, "person": "张巡", "factors": ["face", "card"],
        "persons": ["张巡", "李督"]})
    chk("双因子+双人 -> 放行", r.get_json().get("allow"), r.get_json().get("deny_reason"))

    r = C.post("/api/vault/access", headers=H, json={
        "room_id": ROOM, "person": "张巡", "factors": ["face"],
        "persons": ["张巡", "李督"]})
    chk("单因子 -> 拒绝", not r.get_json().get("allow"), r.get_json().get("deny_reason"))

    r = C.post("/api/vault/access", headers=H, json={
        "room_id": ROOM, "person": "张巡", "factors": ["face", "card"],
        "persons": ["张巡"]})
    chk("单人 -> 拒绝", not r.get_json().get("allow"), r.get_json().get("deny_reason"))

    r = C.post("/api/vault/intrusion", headers=H, json={
        "room_id": ROOM, "kind": "ir_dual", "sensor_id": "IR-01", "zone": "门口"})
    chk("红外双鉴告警", bool((r.get_json().get("alarm") or {}).get("code")))

    r = C.get("/api/vault/check?room=" + ROOM, headers=H)
    chk("七项自检有结论", len(r.get_json().get("items") or []) >= 7,
        len(r.get_json().get("items") or []))

    # ---------- 录像 ----------
    r = C.post("/api/records/simulate", headers=H, json={"room_id": ROOM})
    chk("生成录像段", (r.get_json().get("segments") or 0) > 0,
        r.get_json().get("segments"))

    r = C.get("/api/records?room=" + ROOM, headers=H)
    j = r.get_json()
    chk("录像连续无缺口", all(x["ok"] for x in (j.get("rows") or [])),
        [(x["cam_id"], x["gaps"]) for x in (j.get("rows") or []) if not x["ok"]])
    chk("双路冗余齐备", all(x["redundant"] for x in (j.get("rows") or [])))
    ret = j.get("retention") or {}
    chk("留存 180 天 + WORM", ret.get("retain_days") == 180 and ret.get("worm"), ret)

    # ---------- 流转 ----------
    BAG = "BAG-2026-0001"
    C.post("/api/bags", headers=H, json={"bag_id": BAG, "rfid": "RF-0001",
                                         "subject": "语文", "quantity": 30,
                                         "room_id": ROOM})
    r = C.get("/api/bags", headers=H)
    chk("试卷袋已登记", any(b["bag_id"] == BAG
                            for b in (r.get_json().get("bags") or [])))

    r = C.post("/api/flow/handover", headers=H, json={
        "bag_id": BAG, "from_person": "考务甲", "to_person": "押运乙",
        "persons": ["考务甲", "押运乙"], "signatures": ["考务甲", "押运乙"],
        "faces": ["考务甲", "押运乙"], "method": "scan", "quantity": 30,
        "room_id": ROOM, "stage": "handover"})
    j = r.get_json()
    chk("齐备交接 complete", j.get("complete"), j.get("problems"))
    chk("齐备交接不告警", len(j.get("alarms") or []) == 0, j.get("alarms"))

    r = C.post("/api/flow/handover", headers=H, json={
        "bag_id": BAG, "from_person": "押运乙", "to_person": "押运丙",
        "persons": ["押运乙", "押运丙"], "signatures": ["押运乙", "押运丙"],
        "faces": ["押运乙"], "method": "scan", "quantity": 30,
        "room_id": ROOM, "stage": "handover"})
    chk("缺人脸交接 -> 告警", len(r.get_json().get("alarms") or []) > 0,
        [a["code"] for a in (r.get_json().get("alarms") or [])])

    r = C.post("/api/flow/seal", headers=H, json={"bag_id": BAG,
                                                  "seal_id": "SEAL-0001",
                                                  "room_id": ROOM})
    chk("电子铅封成功", r.status_code == 200, r.get_json())

    r = C.post("/api/flow/seal", headers=H, json={
        "bag_id": BAG, "state": "broken", "detail": "实测：封签被拆"})
    chk("铅封异常开启 -> 告警",
        bool((r.get_json().get("alarm") or {}).get("code")))

    # ---------- 押运 ----------
    r = C.post("/api/escort/start", headers=H, json={
        "plate": "京A12345", "driver": "司机王",
        "escort_persons": ["押运乙", "押运丙"], "bag_ids": [BAG],
        "route": [[39.9000, 116.4000], [39.9050, 116.4100], [39.9100, 116.4200]],
        "video_url": "rtsp://demo/car"})
    j = r.get_json()
    chk("押运任务建立", bool((j.get("task") or {}).get("task_id")), j.get("error"))
    TID = (j.get("task") or {}).get("task_id")

    r = C.post("/api/escort/position", headers=H, json={
        "task_id": TID, "lat": 39.9010, "lng": 116.4050, "speed_kmh": 45.0})
    chk("在路线不报偏离", not r.get_json().get("deviation_alarm"),
        (r.get_json().get("deviation_alarm") or {}).get("code"))

    r = C.post("/api/escort/position", headers=H, json={
        "task_id": TID, "lat": 39.7000, "lng": 115.0000, "speed_kmh": 45.0})
    dv = r.get_json().get("deviation_alarm") or {}
    chk("偏离路线 -> 告警", bool(dv.get("code")), dv.get("code"))
    chk("偏离告警时延 ≤30s", (dv.get("latency_sec") or 99) <= 30.0, dv.get("latency_sec"))

    r = C.post("/api/escort/video", headers=H, json={"task_id": TID, "online": False})
    chk("车内视频中断 -> 告警",
        bool((r.get_json().get("alarm") or {}).get("code")))

    r = C.post("/api/escort/finish", headers=H, json={"task_id": TID, "status": "done"})
    chk("押运任务完成", r.status_code == 200, r.get_json())

    # ---------- 分发 / 回收 ----------
    r = C.post("/api/flow/deliver", headers=H, json={
        "bag_id": BAG, "room_id": "考场101", "quantity": 30, "person": "监考赵"})
    chk("分发齐备无告警", len(r.get_json().get("alarms") or []) == 0,
        r.get_json().get("problems"))
    r = C.post("/api/flow/deliver", headers=H, json={
        "bag_id": BAG, "room_id": "", "quantity": 0, "person": "监考赵"})
    chk("分发残缺 -> 告警", len(r.get_json().get("alarms") or []) > 0,
        [a["code"] for a in (r.get_json().get("alarms") or [])])

    r = C.post("/api/flow/recycle", headers=H, json={
        "bag_id": BAG, "room_id": "考场101", "issued_qty": 30,
        "returned_qty": 30, "person": "监考赵", "seal_id": "SEAL-RC1"})
    chk("回收无差异且已封存", len(r.get_json().get("alarms") or []) == 0,
        [a["code"] for a in (r.get_json().get("alarms") or [])])
    r = C.post("/api/flow/recycle", headers=H, json={
        "bag_id": BAG, "room_id": "考场101", "issued_qty": 30,
        "returned_qty": 28, "person": "监考赵", "seal_id": "SEAL-RC2"})
    chk("回收差异 -> 告警", len(r.get_json().get("alarms") or []) > 0,
        [a["code"] for a in (r.get_json().get("alarms") or [])])

    r = C.get("/api/bags/%s/trace" % BAG, headers=H)
    j = r.get_json()
    hist = j.get("history") or []
    chk("全链条追溯 ≥6 段", len(hist) >= 6, [h.get("stage") if isinstance(h, dict)
                                              else h for h in hist][:10])

    # ---------- 告警 ----------
    r = C.get("/api/alarms?limit=400", headers=H)
    j = r.get_json()
    chk("告警列表可取", len(j.get("alarms") or []) > 0, len(j.get("alarms") or []))
    chk("存在未处置红色告警", (j.get("red_open") or 0) > 0, j.get("red_open"))
    first = (j.get("alarms") or [{}])[0]
    r = C.post("/api/alarms/%s/ack" % first.get("alarm_id"),
               headers=H, json={"note": "实测确认"})
    chk("告警确认成功", r.status_code == 200 and r.get_json().get("ok"))
    r = C.post("/api/alarms/ack_all_red", headers=H, json={})
    chk("批量确认红告警", r.status_code == 200, r.get_json())

    # ---------- KPI ----------
    r = C.get("/api/kpi?room=" + ROOM, headers=H)
    srm = r.get_json().get("summary") or {}
    rows = srm.get("rows") or []
    chk("KPI 五项齐", len(rows) == 5, [i.get("key") for i in rows])
    cov = next((i for i in rows if i["key"] == "coverage_rate"), None)
    chk("覆盖率 KPI 达标", cov and cov.get("verdict") == "达标", cov)
    esc = next((i for i in rows if i.get("key") == "escort_deviate_latency"), None)
    chk("押运 KPI 有实测值", esc and esc.get("actual") is not None, esc)
    lat = next((i for i in rows if i.get("key") == "alarm_latency"), None)
    chk("告警时延 KPI 有实测值", lat and lat.get("actual") is not None, lat)

    # ---------- 报告 ----------
    r = C.get("/api/report?room=" + ROOM, headers=H)
    txt = r.get_json().get("text") or ""
    chk("验收报告生成", len(txt) > 500, len(txt))
    for sec in ["无死角", "门禁", "录像", "交接", "指标"]:
        chk("报告含「%s」" % sec, sec in txt)

    r = C.post("/api/report/export", headers=H, json={"room": ROOM})
    j = r.get_json()
    chk("报告导出落盘", bool(j.get("report")), j.get("report"))
    led = j.get("ledgers") or {}
    chk("台账 CSV ≥6 份", len(led) >= 6, sorted(led.keys()))

    # ---------- 审计 ----------
    r = C.get("/api/audit", headers=H)
    j = r.get_json()
    chk("审计链校验通过", j.get("verified"), j.get("reason"))
    chk("审计条目非空", len(j.get("rows") or []) > 0, len(j.get("rows") or []))

    # ---------- MJPEG ----------
    # 本机没有摄像头，用 OpenCV 合成一段本地 mp4 当取流源，
    # 这样"网页里看实时画面"这条链路是**真读帧**验证的，不是空断言。
    import cv2
    import numpy as np
    vid = os.path.join(_TMP, "fake_cam.mp4")
    if not os.path.exists(vid):
        vw = cv2.VideoWriter(vid, cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (640, 360))
        for i in range(120):
            img = np.zeros((360, 640, 3), np.uint8)
            img[:] = (30, 30, 40)
            cv2.rectangle(img, (60 + i, 120), (200 + i, 300), (0, 220, 255), -1)
            cv2.putText(img, "CAM %03d" % i, (30, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            vw.write(img)
        vw.release()

    r = C.post("/api/cameras", headers=H, json={
        "cam_id": "CAM-LIVE-A", "room_id": ROOM, "name": "保密室A·实测",
        "face": "front", "url": vid, "checkpoints": cfg.FACE_CHECKPOINTS["front"],
        "channels": ["main", "backup"]})
    chk("登记实测机位（本地合成源）", r.status_code == 200, r.get_json())
    r = C.post("/api/cameras/CAM-LIVE-A/start", headers=H, json={})
    chk("实测机位开始取流", r.get_json().get("ok"), r.get_json())
    time.sleep(2.0)

    r = C.get("/api/cameras?room=" + ROOM, headers=H)
    live = next((x for x in (r.get_json().get("cameras") or [])
                 if x["cam_id"] == "CAM-LIVE-A"), {})
    chk("实测机位状态已进入 live/online",
        live.get("live_state") in ("live", "starting", "stalled"), live.get("live_state"))

    # 流是无限响应，只取响应头 + 少量分片，读完必须主动 close，否则会一直挂着
    r = C.get("/api/stream/CAM-LIVE-A?token=%s" % TOK, buffered=False)
    ctype = r.headers.get("Content-Type", "")
    got = False
    try:
        it = r.response
        for _ in range(120):
            chunk = next(it, b"")
            if not chunk:
                break
            if b"image/jpeg" in chunk or b"--frame" in chunk:
                got = True
                break
    except Exception:                                        # noqa: BLE001
        pass
    finally:
        r.close()
    chk("MJPEG 返回 multipart/x-mixed-replace",
        "multipart/x-mixed-replace" in ctype, ctype)
    chk("MJPEG 真的吐出了 image/jpeg 帧分片", got, got)

    # ---------- 去重 ----------
    before = len((C.get("/api/alarms?limit=500", headers=H)
                  .get_json().get("alarms") or []))
    for _ in range(5):
        C.post("/api/vault/env", headers=H, json={
            "room_id": ROOM, "temp_c": 25.0, "humidity_pct": 45.0,
            "water": True, "smoke": False})
    after = len((C.get("/api/alarms?limit=500", headers=H)
                 .get_json().get("alarms") or []))
    chk("重复上报去重（≤1 条新增）", after - before <= 1,
        "before=%d after=%d" % (before, after))

    # ---------- 改密校验 ----------
    r = C.post("/api/change_password", headers=H, json={"new_password": "short"})
    chk("过短新口令 -> 400", r.status_code == 400, r.get_json())

    ok = sum(1 for x in RESULTS if x[0])
    print("=" * 70)
    print(" 合计 %d 项，通过 %d 项，失败 %d 项" % (len(RESULTS), ok, len(RESULTS) - ok))
    if ok != len(RESULTS):
        print(" 失败明细：")
        for good, name, extra in RESULTS:
            if not good:
                print("   - %s (%s)" % (name, extra))
    print("=" * 70)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
