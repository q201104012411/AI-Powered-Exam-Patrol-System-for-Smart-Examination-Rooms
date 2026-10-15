# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 网页面板后端（Flask，端口 5020）

与桌面端**账户与权限同源**（都 import web_accounts，读写同一个 kv 键），
所以同一账号两边口令必然一致。

实时画面：`GET /api/stream/<cam_id>` 输出 multipart/x-mixed-replace，
浏览器 <img> 直接吃 MJPEG —— 这是"网页里也能看摄像头"的标准做法，
不需要 WebRTC/RTSP 插件。取流仍走 camera_stream.FrameReader（浏览器断开
或卡住时只影响该条连接，不拖垮服务）。
"""
import functools
import json
import os
import secrets
import sys
import threading
import time

# app.py 位于 <项目根>/网页监控面板/server/app.py
#   __file__            -> .../网页监控面板/server/app.py
#   dirname             -> .../网页监控面板/server
#   dirname(dirname)    -> .../网页监控面板      （静态页目录）
#   再上一层            -> 项目根                （放 config/models/... 等模块）
_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(_WEB)
sys.path.insert(0, BASE)

import cv2                                                  # noqa: E402
from flask import (Flask, Response, jsonify, request, send_file,  # noqa: E402
                   session)

import alarm_hub                                            # noqa: E402
import config                                               # noqa: E402
import flow as flow_mod                                     # noqa: E402
import models                                               # noqa: E402
import report as report_mod                                 # noqa: E402
import storage                                              # noqa: E402
import vault as vault_mod                                   # noqa: E402
import web_accounts                                         # noqa: E402


def _console_encoding():
    """
    控制台输出到底该用什么编码。

    为什么不能硬编码 utf-8：`.bat` 里用 `chcp 936`（GBK 是中文 Windows 的 ANSI
    默认代码页，双击时最稳），此时 Python 若按 utf-8 往控制台写，中文就会变成
    「璇曞嵎娴佽浆」这种乱码。所以优先跟随 bat 设的 PYTHONIOENCODING，
    再退到系统 ANSI 代码页，最后才是 utf-8。
    """
    env = (os.environ.get("PYTHONIOENCODING") or "").strip()
    if env:
        return env.split(":")[0] or "utf-8"
    try:
        import locale
        enc = locale.getpreferredencoding(False)
        if enc:
            return enc
    except Exception:                                        # noqa: BLE001
        pass
    return "utf-8"


def _ensure_stdio():
    """pythonw/重定向时 stdout 可能是 None；编码按控制台走，避免中文乱码。"""
    enc = _console_encoding()
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding=enc, errors="replace")
        except Exception:                                    # noqa: BLE001
            pass

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("EXAM_SECURE_SECRET") or secrets.token_hex(32)

STORE = storage.Store()
HUB = alarm_hub.AlarmHub(STORE)
CAMS = vault_mod.CameraManager(STORE)
VAULT = vault_mod.Vault(STORE, HUB, CAMS)
FLOW = flow_mod.FlowManager(STORE, HUB)
KPI = report_mod.KPIReport(STORE, VAULT, FLOW)
EVIDENCE = report_mod.EvidenceReport(STORE, VAULT, FLOW, KPI)
ACCOUNTS = web_accounts.AccountManager(STORE)
ACCOUNTS.ensure_defaults()

# 令牌 -> Session。与桌面端共用账户，但会话独立（网页端 TTL 8 小时）。
_TOKENS = {}
_TOKEN_TTL = 8 * 3600
_TOKEN_LOCK = threading.Lock()


# ---------------- 会话 ----------------
def _issue(session_obj):
    tok = secrets.token_urlsafe(24)
    with _TOKEN_LOCK:
        _TOKENS[tok] = (session_obj, time.time() + _TOKEN_TTL)
    return tok


def _current():
    tok = request.headers.get("X-Token") or session.get("token")
    if not tok:
        return None
    with _TOKEN_LOCK:
        rec = _TOKENS.get(tok)
    if rec is None:
        return None
    s, exp = rec
    if time.time() > exp:
        with _TOKEN_LOCK:
            _TOKENS.pop(tok, None)
        return None
    return s


def require(actions=None):
    """接口鉴权：未登录 401；已登录但无权限 403（越权不是"藏按钮"就算）。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            s = _current()
            if s is None:
                return jsonify({"ok": False, "error": "未登录", "code": 401}), 401
            for act in (actions or []):
                msg = s.need(act)
                if msg:
                    return jsonify({"ok": False, "error": msg, "code": 403}), 403
            return fn(s, *a, **kw)
        return wrap
    return deco


@app.after_request
def _no_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ============================ 页面 ============================
@app.route("/")
def index():
    return send_file(os.path.join(_WEB, "web", "index.html"))


# ============================ 认证 ============================
@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    s, why = ACCOUNTS.login(str(data.get("username", "")).strip(),
                            str(data.get("password", "")))
    if s is None:
        return jsonify({"ok": False, "error": why}), 401
    tok = _issue(s)
    session["token"] = tok
    return jsonify({"ok": True, "token": tok, "username": s.username, "role": s.role,
                    "role_text": web_accounts.role_text(s.role),
                    "actions": sorted(config.ROLE_ACTIONS.get(s.role, set()))})


@app.post("/api/logout")
def api_logout():
    tok = request.headers.get("X-Token") or session.get("token")
    with _TOKEN_LOCK:
        _TOKENS.pop(tok, None)
    session.pop("token", None)
    return jsonify({"ok": True})


@app.get("/api/me")
@require()
def api_me(s):
    return jsonify({"ok": True, "username": s.username, "role": s.role,
                    "role_text": web_accounts.role_text(s.role),
                    "actions": sorted(config.ROLE_ACTIONS.get(s.role, set()))})


@app.post("/api/change_password")
@require(["change_own_pwd"])
def api_change_pwd(s):
    d = request.get_json(silent=True) or {}
    ok, msg = ACCOUNTS.change_password(s, s.username, str(d.get("new_password", "")))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


# ============================ 概览 ============================
@app.get("/api/overview")
@require()
def api_overview(s):
    rooms = STORE.rooms()
    room = request.args.get("room") or (rooms[0] if rooms else "")
    rate, _ = CAMS.coverage(room) if room else (None, {})
    cams = STORE.cameras(room or None)
    # 设备在线率：以"已登记机位中在线数"算
    online = sum(1 for c in cams if c.online)
    return jsonify({
        "ok": True, "rooms": rooms, "room": room,
        "coverage_rate": rate,
        "blind_spots": CAMS.blind_spots(room) if room else [],
        "cameras": len(cams), "cameras_online": online,
        "red_alarms": HUB.open_red_count(),
        "alarms_today": HUB.today_count(),
        "armed": VAULT.intrusion.is_armed(room) if room else False,
        "removal": {
            "data_root": str(config.DATA_ROOT),
            "retain_days": config.RECORD_RETAIN_DAYS,
            "worm": config.RECORD_WORM,
            "sla_intrusion": config.INTRUSION_SLA_SEC,
            "sla_escort": config.ESCORT_DEVIATE_SLA_SEC,
        },
    })


@app.get("/api/config")
@require()
def api_config(s):
    return jsonify({
        "ok": True,
        "faces": [{"key": f, "text": config.FACE_TEXT[f],
                   "checkpoints": config.FACE_CHECKPOINTS[f]}
                  for f in config.COVER_FACES],
        "intrusion_kinds": config.INTRUSION_KINDS,
        "env_thresholds": config.ENV_THRESHOLDS,
        "flow_stages": config.FLOW_STAGES,
        "levels": config.LEVEL_META,
        "kpi_defs": config.KPI_DEFS,
        "retain_days": config.RECORD_RETAIN_DAYS,
        "worm": config.RECORD_WORM,
    })


# ============================ 机位与实时画面 ============================
@app.get("/api/cameras")
@require()
def api_cameras(s):
    room = request.args.get("room")
    out = []
    for c in STORE.cameras(room or None):
        st = CAMS.snapshot_state(c.cam_id)
        out.append({
            "cam_id": c.cam_id, "room_id": c.room_id, "name": c.name,
            "face": c.face, "face_text": config.FACE_TEXT.get(c.face, c.face),
            "url": c.url, "checkpoints": c.checkpoints, "channels": c.channels,
            "online": c.online,
            "problems": c.ledger_problems(),
            "live_state": st.get("state"), "live_fps": st.get("fps"),
            "live_status": st.get("status"),
            "live": c.cam_id in CAMS.running_ids(),
        })
    return jsonify({"ok": True, "cameras": out})


@app.post("/api/cameras")
@require(["device_manage"])
def api_camera_add(s):
    d = request.get_json(silent=True) or {}
    try:
        cam = CAMS.register(
            str(d.get("cam_id", "")).strip(), str(d.get("room_id", "")).strip(),
            str(d.get("name", "")).strip(), str(d.get("face", "")).strip(),
            url=str(d.get("url", "")).strip(),
            angle_deg=d.get("angle_deg") or 0.0,
            checkpoints=d.get("checkpoints") or [],
            channels=d.get("channels") or ["main", "backup"],
            operator=s.username)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "cam_id": cam.cam_id})


@app.delete("/api/cameras/<cam_id>")
@require(["device_delete"])
def api_camera_del(s, cam_id):
    CAMS.remove(cam_id, operator=s.username)
    return jsonify({"ok": True})


@app.post("/api/cameras/<cam_id>/start")
@require(["live_view"])
def api_cam_start(s, cam_id):
    ok, msg = CAMS.start(cam_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.post("/api/cameras/<cam_id>/stop")
@require(["live_view"])
def api_cam_stop(s, cam_id):
    CAMS.stop(cam_id)
    return jsonify({"ok": True})


@app.post("/api/cameras/<cam_id>/snapshot")
@require(["live_view"])
def api_cam_snapshot(s, cam_id):
    p = CAMS.snapshot(cam_id, reason="manual")
    if not p:
        return jsonify({"ok": False, "error": "当前没有可用画面（请先开始取流）"}), 400
    return jsonify({"ok": True, "path": p})


@app.get("/api/stream/<cam_id>")
def api_stream(cam_id):
    """MJPEG 实时流。浏览器 <img src> 直接显示。

    鉴权用查询串 token（<img> 带不了自定义头）——这是 MJPEG 场景的通行做法；
    令牌本来就是随机 32 字节、8 小时过期，且只用于读画面。
    """
    tok = request.args.get("token") or request.headers.get("X-Token") \
        or session.get("token")
    with _TOKEN_LOCK:
        rec = _TOKENS.get(tok) if tok else None
    if rec is None or time.time() > rec[1]:
        return Response("unauthorized", status=401, mimetype="text/plain")
    s, _ = rec
    if s.need("live_view"):
        return Response(s.need("live_view"), status=403, mimetype="text/plain")

    cam = STORE.get_camera(cam_id)
    if cam is None or not cam.url:
        return Response("no such camera or no url", status=404,
                        mimetype="text/plain")
    if cam_id not in CAMS.running_ids():
        CAMS.start(cam_id)

    boundary = "frame"

    def gen():
        idle = 0
        last_seq = -1
        while True:
            st = CAMS.snapshot_state(cam_id)
            frame = st.get("frame")
            if frame is not None and st.get("seq") != last_seq:
                last_seq = st.get("seq")
                idle = 0
                ok, buf = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                if ok:
                    data = buf.tobytes()
                    yield (b"--" + boundary.encode() + b"\r\n"
                           b"Content-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                           + data + b"\r\n")
            else:
                idle += 1
                if idle % 50 == 0:
                    # 长时间没有新帧也持续维持连接（画面由前端显示"中断"状态）
                    pass
            time.sleep(0.05)

    return Response(gen(),
                    mimetype="multipart/x-mixed-replace; boundary=" + boundary,
                    headers={"Cache-Control": "no-store",
                             "X-Accel-Buffering": "no"})


@app.post("/api/cameras/probe_local")
@require(["live_view"])
def api_probe_local(s):
    return jsonify({"ok": True, "indexes": CAMS.probe_local()})


# ============================ 保密室监控事件 ============================
@app.post("/api/vault/access")
@require(["access_operate"])
def api_access(s):
    d = request.get_json(silent=True) or {}
    if d.get("abnormal"):
        a = VAULT.access.report_abnormal_open(
            d.get("room_id", ""), person=s.username,
            detail=d.get("detail") or "网页面板上报：未授权开门")
        return jsonify({"ok": True, "allow": False, "alarms": [a.code],
                        "alarm": a.to_row()})
    ok, ev = VAULT.access.request_open(
        d.get("room_id", ""), d.get("person") or s.username,
        factors=d.get("factors") or [], persons=d.get("persons") or [],
        tailgating=bool(d.get("tailgating")))
    return jsonify({"ok": True, "allow": ok, "deny_reason": ev.deny_reason,
                    "event_id": ev.event_id})


@app.post("/api/vault/intrusion")
@require(["monitor_operate"])
def api_intrusion(s):
    d = request.get_json(silent=True) or {}
    kind = d.get("kind", "ir_dual")
    try:
        ev, alarm = VAULT.intrusion.trigger(
            d.get("room_id", ""), kind, sensor_id=d.get("sensor_id", ""),
            zone=d.get("zone", ""), armed=True)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "event_id": ev.event_id,
                    "alarm": alarm.to_row() if alarm else None})


@app.post("/api/vault/armed")
@require(["monitor_operate"])
def api_armed(s):
    d = request.get_json(silent=True) or {}
    room = d.get("room_id", "")
    cur = VAULT.intrusion.is_armed(room)
    want = bool(d.get("armed", not cur))
    VAULT.intrusion.set_armed(room, want, operator=s.username)
    return jsonify({"ok": True, "armed": want})


@app.post("/api/vault/safe")
@require(["safe_operate"])
def api_safe(s):
    d = request.get_json(silent=True) or {}
    room = d.get("room_id", "")
    safe_id = d.get("safe_id") or "SAFE-01"
    if d.get("state"):
        ev, alarms = VAULT.safes.report_state(room, safe_id, d["state"],
                                              person=s.username,
                                              inner=d.get("inner") or {})
    else:
        ev, alarms = VAULT.safes.open_request(
            room, safe_id, s.username, authorized=bool(d.get("authorized")),
            inner=d.get("inner") or {})
    return jsonify({"ok": True, "event_id": ev.event_id,
                    "alarms": [a.to_row() for a in alarms]})


@app.post("/api/vault/env")
@require(["monitor_operate"])
def api_env(s):
    d = request.get_json(silent=True) or {}
    sample, alarms = VAULT.env.sample(
        d.get("room_id", ""), temp_c=d.get("temp_c", 25.0),
        humidity_pct=d.get("humidity_pct", 45.0),
        water=bool(d.get("water")), smoke=bool(d.get("smoke")))
    return jsonify({"ok": True, "sample_id": sample.sample_id,
                    "violations": [c for c, _ in sample.violations()],
                    "alarms": [a.to_row() for a in alarms]})


@app.post("/api/vault/patrol")
@require(["monitor_operate"])
def api_patrol(s):
    d = request.get_json(silent=True) or {}
    room = d.get("room_id", "")
    cams = STORE.cameras(room)
    rec = VAULT.patrol.patrol_round(
        room, by=s.username, result=d.get("result", "ok"),
        note=d.get("note", "网页面板登记的一轮巡查"),
        snapshot_cam=(cams[0].cam_id if cams else None))
    return jsonify({"ok": True, "round": rec})


@app.get("/api/vault/check")
@require()
def api_vault_check(s):
    room = request.args.get("room") or ""
    if not room:
        return jsonify({"ok": False, "error": "缺少 room 参数"}), 400
    return jsonify({"ok": True, "items": VAULT.self_check(room)})


@app.get("/api/vault/events")
@require()
def api_vault_events(s):
    room = request.args.get("room") or None
    lim = int(request.args.get("limit") or 60)
    return jsonify({
        "ok": True,
        "access": [e.to_row() for e in STORE.access_events(room, lim)],
        "intrusion": [e.to_row() for e in STORE.intrusion_events(room, lim)],
        "safes": [e.to_row() for e in STORE.safe_events(room, lim)],
        "env": [e.to_row() for e in STORE.env_samples(room, lim)],
    })


# ============================ 录像 ============================
@app.post("/api/records/simulate")
@require(["monitor_operate"])
def api_records_sim(s):
    """为某保密室的机位生成 7 天连续录像段（验收演示/联调用）。"""
    d = request.get_json(silent=True) or {}
    room = d.get("room_id", "")
    cams = STORE.cameras(room)
    if not cams:
        return jsonify({"ok": False, "error": "该保密室还没有登记机位"}), 400
    now = time.time()
    base = now - 7 * 86400          # 近 7 天，正好落在 180 天留存期内
    n = 0
    for cam in cams:
        for ch in (cam.channels or ["main"]):
            cur = base
            while cur < now:
                VAULT.records.add_segment(cam.cam_id, cur, 3600.0, channel=ch)
                cur += 3600.0
                n += 1
    return jsonify({"ok": True, "segments": n, "cameras": len(cams)})


@app.post("/api/records/gap")
@require(["monitor_operate"])
def api_records_gap(s):
    """制造一段录像缺口，验证连续性告警。"""
    d = request.get_json(silent=True) or {}
    cam_id = d.get("cam_id")
    cams = STORE.cameras(d.get("room_id") or None)
    if not cam_id:
        cam_id = cams[0].cam_id if cams else None
    if not cam_id:
        return jsonify({"ok": False, "error": "没有可操作的机位"}), 400
    ch = (STORE.get_camera(cam_id).channels or ["main"])[0]
    segs = sorted(STORE.segments(cam_id, ch), key=lambda x: x.start_epoch)
    if not segs:
        return jsonify({"ok": False, "error": "该机位还没有录像段，请先生成"}), 400
    mid = segs[len(segs) // 2]
    mid.duration_sec = 1.0
    STORE.save_segment(mid)
    probs = VAULT.records.check_all(d.get("room_id") or None)
    return jsonify({"ok": True, "problems": probs})


@app.get("/api/records")
@require()
def api_records(s):
    room = request.args.get("room")
    out = []
    for c in STORE.cameras(room or None):
        for ch in (c.channels or ["main"]):
            r = VAULT.records.continuity(c.cam_id, ch)
            ok, _ = VAULT.records.redundancy_ok(c.cam_id)
            out.append({"cam_id": c.cam_id, "channel": ch, "ok": r["ok"],
                        "segments": r["segments"], "gaps": len(r["gaps"]),
                        "unplayable": len(r["unplayable"]),
                        "redundant": ok})
    ret = VAULT.records.retention_report()
    return jsonify({"ok": True, "rows": out, "retention": ret})


# ============================ 试卷流转 ============================
@app.get("/api/bags")
@require()
def api_bags(s):
    return jsonify({"ok": True, "bags": [b.to_row() for b in STORE.bags()],
                    "stage_coverage": FLOW.stage_coverage()})


@app.post("/api/bags")
@require(["flow_operate"])
def api_bag_add(s):
    d = request.get_json(silent=True) or {}
    bid = str(d.get("bag_id", "")).strip()
    if not bid:
        return jsonify({"ok": False, "error": "试卷袋编号不能为空"}), 400
    b = FLOW.register_bag(bid, rfid=d.get("rfid", ""),
                          bundle_id=d.get("bundle_id", ""),
                          subject=d.get("subject", ""),
                          quantity=d.get("quantity") or 0,
                          room_id=d.get("room_id", ""), operator=s.username)
    return jsonify({"ok": True, "bag": b.to_row()})


@app.get("/api/bags/<bag_id>/trace")
@require()
def api_bag_trace(s, bag_id):
    t = FLOW.bag_trace(bag_id)
    if t is None:
        return jsonify({"ok": False, "error": "试卷袋不存在"}), 404
    return jsonify({"ok": True,
                    "bag": t["bag"].to_row(),
                    "history": t["history"],
                    "handovers": [h.to_row() for h in t["handovers"]],
                    "deliveries": [d.to_row() for d in t["deliveries"]],
                    "recycles": [r.to_row() for r in t["recycles"]]})


@app.post("/api/flow/handover")
@require(["flow_operate"])
def api_handover(s):
    d = request.get_json(silent=True) or {}
    h, alarms = FLOW.handover.handover(
        d.get("bag_id", ""), d.get("from_person", ""), d.get("to_person", ""),
        persons=d.get("persons") or [], signatures=d.get("signatures") or [],
        faces=d.get("faces") or [], method=d.get("method", "scan"),
        quantity=d.get("quantity") or 0, room_id=d.get("room_id", ""),
        stage=d.get("stage", "handover"), operator=s.username)
    return jsonify({"ok": True, "handover": h.to_row(),
                    "complete": h.complete(),
                    "problems": [c for c, _ in h.problems],
                    "alarms": [a.to_row() for a in alarms]})


@app.post("/api/flow/seal")
@require(["flow_operate"])
def api_seal(s):
    d = request.get_json(silent=True) or {}
    bid = d.get("bag_id", "")
    if d.get("state") == "broken":
        bag, alarm = FLOW.seal.report(bid, "broken", person=s.username,
                                      detail=d.get("detail", "网页面板上报"))
        return jsonify({"ok": True, "bag": bag.to_row(),
                        "alarm": alarm.to_row() if alarm else None})
    bag = FLOW.seal.seal(bid, d.get("seal_id", ""), s.username,
                        room_id=d.get("room_id", ""))
    return jsonify({"ok": True, "bag": bag.to_row(), "alarm": None})


@app.post("/api/flow/deliver")
@require(["flow_operate"])
def api_deliver(s):
    d = request.get_json(silent=True) or {}
    rec, alarms = FLOW.delivery.deliver(
        d.get("bag_id", ""), d.get("room_id", ""), d.get("quantity") or 0,
        d.get("person") or s.username, note=d.get("note", ""))
    return jsonify({"ok": True, "delivery": rec.to_row(),
                    "problems": rec.problems(),
                    "alarms": [a.to_row() for a in alarms]})


@app.post("/api/flow/recycle")
@require(["flow_operate"])
def api_recycle(s):
    d = request.get_json(silent=True) or {}
    rec, alarms = FLOW.recycle.recycle(
        d.get("bag_id", ""), d.get("room_id", ""), d.get("issued_qty") or 0,
        d.get("returned_qty") or 0, person=d.get("person") or s.username,
        seal_id=d.get("seal_id", ""), auto_seal=bool(d.get("auto_seal", True)),
        note=d.get("note", ""))
    return jsonify({"ok": True, "recycle": rec.to_row(),
                    "alarms": [a.to_row() for a in alarms]})


@app.get("/api/escort/tasks")
@require()
def api_escort_tasks(s):
    out = []
    for t in STORE.escort_tasks():
        srm = FLOW.escort.task_summary(t.task_id)
        out.append({"task": t.to_row(), "summary": srm,
                    "points": [p.to_row() for p in STORE.escort_points(t.task_id)]})
    return jsonify({"ok": True, "tasks": out})


@app.post("/api/escort/start")
@require(["escort_operate"])
def api_escort_start(s):
    d = request.get_json(silent=True) or {}
    try:
        t = FLOW.escort.start_task(
            d.get("plate", ""), driver=d.get("driver", ""),
            escort_persons=d.get("escort_persons") or [],
            bag_ids=d.get("bag_ids") or [], route=d.get("route") or [],
            video_url=d.get("video_url", ""), operator=s.username)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "task": t.to_row()})


@app.post("/api/escort/position")
@require(["escort_operate"])
def api_escort_pos(s):
    d = request.get_json(silent=True) or {}
    try:
        p, alarm = FLOW.escort.report_position(
            d.get("task_id", ""), d.get("lat"), d.get("lng"),
            speed_kmh=d.get("speed_kmh") or 0.0,
            source=d.get("source", "beidou"))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "point": p.to_row(),
                    "deviation_alarm": alarm.to_row() if alarm else None})


@app.post("/api/escort/video")
@require(["escort_operate"])
def api_escort_video(s):
    d = request.get_json(silent=True) or {}
    alarm = FLOW.escort.report_video(d.get("task_id", ""), bool(d.get("online")))
    return jsonify({"ok": True, "alarm": alarm.to_row() if alarm else None})


@app.post("/api/escort/finish")
@require(["escort_operate"])
def api_escort_finish(s):
    d = request.get_json(silent=True) or {}
    t = FLOW.escort.finish_task(d.get("task_id", ""),
                                status=d.get("status", "done"),
                                operator=s.username)
    if t is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "task": t.to_row()})


# ============================ 告警 ============================
@app.get("/api/alarms")
@require()
def api_alarms(s):
    status = request.args.get("status") or None
    code = request.args.get("code") or None
    lim = int(request.args.get("limit") or 300)
    rows = STORE.alarms(status=status, code=code, limit=lim)
    return jsonify({"ok": True, "alarms": [a.to_row() for a in rows],
                    "red_open": HUB.open_red_count()})


@app.post("/api/alarms/<alarm_id>/ack")
@require(["alarm_handle"])
def api_alarm_ack(s, alarm_id):
    d = request.get_json(silent=True) or {}
    ok = HUB.ack(alarm_id, s.username, d.get("note", ""))
    return jsonify({"ok": ok}), (200 if ok else 400)


@app.post("/api/alarms/<alarm_id>/handle")
@require(["alarm_handle"])
def api_alarm_handle(s, alarm_id):
    d = request.get_json(silent=True) or {}
    ok = HUB.handle(alarm_id, s.username, d.get("note", ""))
    return jsonify({"ok": ok}), (200 if ok else 400)


@app.post("/api/alarms/ack_all_red")
@require(["alarm_handle"])
def api_alarm_ack_all(s):
    n = HUB.ack_all_red(s.username)
    return jsonify({"ok": True, "acked": n})


# ============================ 指标与报告 ============================
@app.get("/api/kpi")
@require()
def api_kpi(s):
    room = request.args.get("room") or None
    srm = KPI.summary(room)
    return jsonify({"ok": True, "summary": srm})


@app.get("/api/report")
@require()
def api_report(s):
    room = request.args.get("room") or None
    text = EVIDENCE.full_report(room, generated_by=s.username)
    return jsonify({"ok": True, "text": text})


@app.post("/api/report/export")
@require(["report_export"])
def api_report_export(s):
    d = request.get_json(silent=True) or {}
    path, _ = EVIDENCE.save_report(d.get("room") or None, s.username)
    ledgers = EVIDENCE.export_ledgers()
    return jsonify({"ok": True, "report": path,
                    "ledgers": {k: v for k, v in ledgers.items() if v}})


@app.get("/api/audit")
@require(["audit_view"])
def api_audit(s):
    ok, why = STORE.audit_verify()
    return jsonify({"ok": True, "verified": ok, "reason": why,
                    "rows": STORE.audit_rows(limit=300)})


# ============================ 账户管理 ============================
@app.get("/api/accounts")
@require(["account_manage"])
def api_accounts(s):
    out = []
    for name, a in sorted(ACCOUNTS.accounts().items()):
        out.append({"username": name, "role": a.get("role"),
                    "role_text": web_accounts.role_text(a.get("role")),
                    "disabled": bool(a.get("disabled")),
                    "created": a.get("created")})
    return jsonify({"ok": True, "accounts": out,
                    "roles": [{"key": r, "text": web_accounts.role_text(r),
                               "level": web_accounts.role_level(r)}
                              for r in config.ROLE_LEVEL]})


@app.post("/api/accounts")
@require(["account_manage"])
def api_account_create(s):
    d = request.get_json(silent=True) or {}
    ok, msg = ACCOUNTS.create(s, str(d.get("username", "")).strip(),
                              str(d.get("password", "")), str(d.get("role", "")))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.delete("/api/accounts/<name>")
@require(["account_manage"])
def api_account_delete(s, name):
    ok, msg = ACCOUNTS.delete(s, name)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.post("/api/accounts/<name>/disabled")
@require(["account_manage"])
def api_account_disable(s, name):
    d = request.get_json(silent=True) or {}
    ok, msg = ACCOUNTS.set_disabled(s, name, bool(d.get("disabled")))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.post("/api/accounts/<name>/password")
@require(["account_manage"])
def api_account_pwd(s, name):
    d = request.get_json(silent=True) or {}
    ok, msg = ACCOUNTS.change_password(s, name, str(d.get("password", "")))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


# ============================ 启动 ============================
def create_app():
    return app


def main():
    import argparse
    ap = argparse.ArgumentParser(description="试卷流转与保密室智能监控 · 网页面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    print("=" * 66)
    print(" 试卷流转与保密室智能监控 · 网页面板")
    print(" 地址：http://%s:%d/" % (args.host, args.port))
    print(" 数据目录：%s" % config.DATA_ROOT)
    print(" 默认账户：owner / EDY_POWERQ（所有者）、teacher1 / teacher1（管理员）")
    print("=" * 66)
    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
