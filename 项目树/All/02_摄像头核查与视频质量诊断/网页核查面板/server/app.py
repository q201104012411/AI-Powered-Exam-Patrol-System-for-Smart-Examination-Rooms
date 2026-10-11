# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 网页端后端（Flask）

端口默认 5010（01 的网页端用 5000，互不冲突）。

能力与桌面端一致：
  设备台账 / 基准图采集（上传现场画面）/ 三级巡检 / 逐机位缺陷与指标 /
  整改工单闭环（派单-整改-复核，整改必须上传整改后截图）/ 关键指标 / 报告导出

账户（与 01 一致的两账户模型，**与桌面端 gui.py 共用同一份**）：
  owner    / EDY_POWERQ  所有者：全部 + 终端账户管理 + 审计链 + 隐藏 cmd 开关
  teacher1 / teacher1    管理员：台账/基准图/抽查/巡检/工单/报告（不能删机位、不能管账户）
口令用 PBKDF2-HMAC-SHA256 加盐（120000 轮）存 02 自己的库里，键名 web.accounts，
哈希与账户逻辑抽在 web_accounts.py，桌面端 import 同一模块，不依赖 01。

pythonw.exe（隐藏命令行窗口）启动时 sys.stdout 为 None，必须先兜住再 print。
"""
import functools
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parents[2]             # 02 项目根（app.py 在 网页核查面板/server/ 下）
sys.path.insert(0, str(BASE))

import config                                          # noqa: E402
import diagnose                                        # noqa: E402
import models                                          # noqa: E402
import probe                                           # noqa: E402
import storage                                         # noqa: E402
import web_accounts                                    # noqa: E402
import baseline as baseline_mod                        # noqa: E402
import patrol as patrol_mod                            # noqa: E402
import report as report_mod                            # noqa: E402
import workorder as wo_mod                             # noqa: E402

config.ensure_stdio("web_console.log")                 # 隐藏窗口启动时兜住 stdout/stderr

HTML_DIR = str(BASE)
WEB_DIR = str(BASE / "网页核查面板")
PORT = int(os.environ.get("PORT", "5010"))

app = Flask(__name__)
STORE = storage.Store()
BASELINES = baseline_mod.BaselineStore(STORE)
ORDERS = wo_mod.WorkOrderManager(STORE)
RUNNER = patrol_mod.PatrolRunner(STORE, baseline_store=BASELINES)
REPORTER = report_mod.Reporter(STORE, runner=RUNNER, orders=ORDERS)

ROLE_OWNER, ROLE_ADMIN = web_accounts.ROLE_OWNER, web_accounts.ROLE_ADMIN
ROLE_TEXT = dict(web_accounts.ROLE_TEXT)
ACCOUNT_KEY = web_accounts.ACCOUNT_KEY
DEFAULT_ACCOUNTS = dict(web_accounts.DEFAULT_ACCOUNTS)


# ---------------- 口令 ----------------
# 口令哈希/账户存取/权限判定统一放在 web_accounts.py：桌面端 gui.py 与网页端
# 共用同一份实现、同一个 kv 键（web.accounts），所以两边同一账号口令必然一致。
def _hash(pwd, salt=None):
    return web_accounts.hash_password(pwd, salt)


def _verify(pwd, stored):
    return web_accounts.verify_password(pwd, stored)


def ensure_accounts():
    """保证 kv 里有 web.accounts（老库已存在则原样不动）"""
    return web_accounts.ensure_accounts(STORE)


def accounts_store():
    return web_accounts.Accounts(STORE)


def can(action, role):
    """权限判定唯一入口（与桌面端同一张表 web_accounts.CAN）"""
    return web_accounts.can(action, role)


# ---------------- 会话令牌（HMAC 签名） ----------------
def _secret():
    s = STORE.get_kv("web.secret")
    if not s:
        s = secrets.token_hex(32)
        STORE.set_kv("web.secret", s)
    return s.encode("utf-8")


def gen_token(username, role, ttl=8 * 3600):
    payload = json.dumps({"u": username, "r": role, "exp": int(time.time()) + ttl},
                         ensure_ascii=False, sort_keys=True)
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode() + "." + sig


def read_token(token):
    try:
        import base64
        b64, sig = token.split(".")
        payload = base64.urlsafe_b64decode(b64.encode()).decode("utf-8")
        expect = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        d = json.loads(payload)
        if d.get("exp", 0) < time.time():
            return None
        return d
    except (ValueError, TypeError, KeyError):
        return None


def current_user():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return read_token(auth[7:])
    return None


def require(roles=None):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return jsonify({"error": "未登录或登录已过期"}), 401
            if roles and u["r"] not in roles:
                return jsonify({"error": "权限不足（当前角色：%s）"
                                         % ROLE_TEXT.get(u["r"], u["r"])}), 403
            request.user = u
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return send_from_directory(HTML_DIR, "web.html")


@app.route("/vendor/<path:name>")
def vendor(name):
    return send_from_directory(WEB_DIR, name)


# ---------------- 登录 ----------------
@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    acc = ensure_accounts()
    rec = acc.get(u)
    if not rec or not _verify(p, rec["pwd"]):
        STORE.log("网页登录失败", "用户名=%s" % u, u or "-")
        return jsonify({"error": "用户名或密码错误"}), 401
    STORE.log("网页登录成功", "%s（%s）" % (u, ROLE_TEXT.get(rec["role"], rec["role"])), u)
    return jsonify({"token": gen_token(u, rec["role"]),
                    "user": {"username": u, "role": rec["role"], "name": rec["name"],
                             "role_text": ROLE_TEXT.get(rec["role"], rec["role"]),
                             "can_manage_accounts": web_accounts.can("account_manage",
                                                                     rec["role"]),
                             "permissions": _perms(rec["role"])}})


def _perms(role):
    """把权限矩阵发给前端，用于按角色禁用按钮（后端仍会再拦一次）"""
    return {a: web_accounts.can(a, role) for a in web_accounts.CAN}


@app.route("/api/me", methods=["GET"])
@require()
def me():
    acc = ensure_accounts()
    rec = acc.get(request.user["u"], {})
    return jsonify({"username": request.user["u"], "role": request.user["r"],
                    "name": rec.get("name", request.user["u"]),
                    "role_text": ROLE_TEXT.get(request.user["r"], request.user["r"]),
                    "can_manage_accounts": web_accounts.can("account_manage",
                                                            request.user["r"]),
                    "permissions": _perms(request.user["r"])})


@app.route("/api/change-password", methods=["POST"])
@require()
def change_password():
    data = request.get_json(silent=True) or {}
    old, new = data.get("old_password") or "", data.get("new_password") or ""
    ok, msg = accounts_store().change_password(request.user["u"], old, new)
    return jsonify({"message" if ok else "error": msg}), (200 if ok else 400)


# ---------------- 终端账户管理（仅所有者；桌面端同一份账户） ----------------
@app.route("/api/accounts", methods=["GET"])
@require(roles=[ROLE_OWNER])
def list_accounts():
    if not can("account_manage", request.user["r"]):
        return jsonify({"error": web_accounts.require_text("account_manage",
                                                           request.user["r"])}), 403
    return jsonify({"accounts": accounts_store().list_accounts(),
                    "me": request.user["u"],
                    "roles": [{"role": ROLE_OWNER, "text": ROLE_TEXT[ROLE_OWNER]},
                              {"role": ROLE_ADMIN, "text": ROLE_TEXT[ROLE_ADMIN]}]})


@app.route("/api/accounts", methods=["POST"])
@require(roles=[ROLE_OWNER])
def create_account():
    data = request.get_json(silent=True) or {}
    ok, msg = accounts_store().create(data.get("username") or "",
                                      data.get("password") or "",
                                      data.get("role") or ROLE_ADMIN,
                                      data.get("name") or "",
                                      by=request.user["u"])
    return jsonify({"message" if ok else "error": msg}), (200 if ok else 400)


@app.route("/api/accounts/<username>/password", methods=["POST"])
@require(roles=[ROLE_OWNER])
def reset_account_password(username):
    data = request.get_json(silent=True) or {}
    ok, msg = accounts_store().reset_password(username, data.get("new_password") or "",
                                              by=request.user["u"])
    return jsonify({"message" if ok else "error": msg}), (200 if ok else 400)


@app.route("/api/accounts/<username>", methods=["DELETE"])
@require(roles=[ROLE_OWNER])
def delete_account(username):
    ok, msg = accounts_store().delete(username, by=request.user["u"])
    return jsonify({"message" if ok else "error": msg}), (200 if ok else 400)


# ---------------- 隐藏命令行窗口开关（仅所有者；.bat 读该标记文件） ----------------
@app.route("/api/hidden-cmd", methods=["GET"])
@require()
def get_hidden_cmd():
    on = web_accounts.hidden_cmd_on()
    return jsonify({"hidden": on,
                    "can_toggle": can("hide_cmd_toggle", request.user["r"]),
                    "flag": str(config.HIDE_CMD_FLAG),
                    "message": "下次用启动器启动时%s命令行窗口" % ("隐藏" if on else "显示")})


@app.route("/api/hidden-cmd", methods=["POST"])
@require(roles=[ROLE_OWNER])
def set_hidden_cmd():
    if not can("hide_cmd_toggle", request.user["r"]):
        return jsonify({"error": web_accounts.require_text("hide_cmd_toggle",
                                                           request.user["r"])}), 403
    data = request.get_json(silent=True) or {}
    ok, msg = web_accounts.set_hidden_cmd(bool(data.get("hidden")), STORE,
                                          request.user["u"])
    return jsonify({"message" if ok else "error": msg,
                    "hidden": web_accounts.hidden_cmd_on()}), (200 if ok else 500)


# ---------------- 设备台账 ----------------
@app.route("/api/devices/<device_id>/inspect", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def inspect_uploaded(device_id):
    """
    现场画面抽查：上传一张现场画面，对该机位做一次诊断（含基准图比对）。
    **如实标注**：只做图像质量与视野完整性，不含在线状态/录像/时钟/音频
    （这些必须有实时链路与录像平台数据，单张上传图给不出结论）。
    """
    d = STORE.get_device(device_id=device_id)
    if d is None:
        return jsonify({"error": "机位不存在"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未收到画面文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        return jsonify({"error": "仅支持 jpg/png/bmp/webp"}), 400
    tmpdir = os.path.join(str(config.DATA_ROOT), "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    tmp = os.path.join(tmpdir, "inspect_upload" + ext)
    f.save(tmp)
    try:
        img = diagnose.imread_unicode(tmp)
        if img is None:
            return jsonify({"error": "画面无法解码"}), 400
        frame_path = BASELINES.archive_capture(d, img, tag="upload")
        rec, base_gray, base_metrics = BASELINES.get(d)
        defects, metrics = RUNNER.dg.diagnose(img, baseline_gray=base_gray,
                                             baseline_metrics=base_metrics)
        ck = models.CheckResult(d, config.POLICY_UPLOAD, online=True, defects=defects,
                                metrics=metrics, frame_path=frame_path,
                                operator=request.user["u"], note="网页上传现场画面抽查")
        STORE.save_check(ck)
        STORE.log("现场画面抽查",
                  "%s 缺陷=%s" % (d.code, ",".join(x.code for x in defects) or "无"),
                  request.user["u"])
        wo_id = None
        if not ck.ok:
            wo, _m = ORDERS.ensure_from_check(ck, d, operator=request.user["u"])
            wo_id = wo.order_id if wo else None
        fails = [x for x in defects if x.severity == config.SEV_FAIL]
        warns = [x for x in defects if x.severity == config.SEV_WARN]
        return jsonify({"message": "抽查完成：%s（fail %d 项 / warn %d 项）%s"
                                   % ("正常" if ck.ok else "异常", len(fails),
                                      len(warns),
                                      "；已建工单 " + wo_id if wo_id else ""),
                        "check_id": ck.check_id, "ok": ck.ok,
                        # 注意：base_gray 是 numpy 数组，不能直接 bool()（会抛"truth value ambiguous"）
                        "baseline_used": base_gray is not None,
                        "baseline_note": ("" if base_gray is not None
                                          else BASELINES.missing_reason(d)),
                        "defects": [x.to_dict() for x in defects],
                        "metrics": {k: v for k, v in metrics.items()
                                    if k != "baseline_metrics"}})
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@app.route("/api/devices", methods=["GET"])
@require()
def list_devices():
    out = []
    for d in STORE.list_devices(enabled_only=False):
        base = STORE.get_baseline(d.device_id)
        hb_ok, hb_why = probe.heartbeat_status(d)
        ck = STORE.latest_check(d.device_id)
        out.append({"device_id": d.device_id, "code": d.code, "name": d.name,
                    "room_id": d.room_id, "site": d.site, "position": d.position,
                    "url": d.url, "resolution": d.resolution, "fov_deg": d.fov_deg,
                    "pan_deg": d.pan_deg, "tilt_deg": d.tilt_deg,
                    "has_audio": bool(d.has_audio), "session": d.session,
                    "has_baseline": bool(base),
                    "baseline_at": base["captured_at"] if base else "",
                    "heartbeat_ok": hb_ok, "heartbeat": hb_why,
                    "ledger_problems": d.ledger_problems(),
                    "last_check": ({"ts": ck.ts, "ok": ck.ok,
                                    "defects": [x.to_dict() for x in ck.defects]}
                                   if ck else None)})
    return jsonify({"devices": out, "rooms": STORE.rooms()})


@app.route("/api/devices", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def save_device():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "设备编码必填"}), 400
    try:
        fov = float(data.get("fov_deg") or 90)
        pan = float(data.get("pan_deg") or 0)
        tilt = float(data.get("tilt_deg") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "角度必须是数字"}), 400
    old = STORE.get_device(code=code)
    d = models.Device(code=code, name=(data.get("name") or code).strip(),
                      room_id=(data.get("room_id") or "").strip(),
                      site=(data.get("site") or "").strip(),
                      position=(data.get("position") or "").strip(),
                      url=(data.get("url") or "").strip(),
                      resolution=(data.get("resolution") or "1920x1080").strip(),
                      fov_deg=fov, pan_deg=pan, tilt_deg=tilt,
                      has_audio=bool(data.get("has_audio")),
                      session=(data.get("session") or "").strip(),
                      device_id=(old.device_id if old else None))
    d.last_heartbeat = old.last_heartbeat if old else 0.0
    d.baseline_at = old.baseline_at if old else ""
    STORE.save_device(d)
    STORE.log("网页台账登记", code, request.user["u"])
    return jsonify({"message": "已保存机位 %s" % code, "device_id": d.device_id})


@app.route("/api/devices/<device_id>", methods=["DELETE"])
@require(roles=[ROLE_OWNER])
def delete_device(device_id):
    d = STORE.get_device(device_id=device_id)
    if d is None:
        return jsonify({"error": "机位不存在"}), 404
    STORE.delete_device(device_id)
    STORE.log("网页删除机位", d.code, request.user["u"])
    return jsonify({"message": "已删除 %s" % d.code})


@app.route("/api/devices/<device_id>/heartbeat", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def post_heartbeat(device_id):
    """心跳上报接口（便于真实设备/网关调用）"""
    if STORE.get_device(device_id=device_id) is None:
        return jsonify({"error": "机位不存在"}), 404
    STORE.heartbeat(device_id)
    return jsonify({"message": "心跳已记录"})


# ---------------- 基准图 ----------------
@app.route("/api/devices/<device_id>/baseline", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def upload_baseline(device_id):
    d = STORE.get_device(device_id=device_id)
    if d is None:
        return jsonify({"error": "机位不存在"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未收到基准图文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        return jsonify({"error": "仅支持 jpg/png/bmp/webp"}), 400
    tmpdir = os.path.join(str(config.DATA_ROOT), "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    tmp = os.path.join(tmpdir, "baseline_upload" + ext)
    f.save(tmp)
    try:
        ok, res = BASELINES.capture(d, image_path=tmp, operator=request.user["u"],
                                    note="网页上传")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not ok:
        return jsonify({"error": str(res)[:200]}), 400
    return jsonify({"message": "基准图已留档（清晰度 %.1f）" % res["sharpness"],
                    "sharpness": res["sharpness"], "path": res["path"]})


# ---------------- 巡检 ----------------
@app.route("/api/patrol", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def run_patrol():
    data = request.get_json(silent=True) or {}
    policy = data.get("policy") or config.POLICY_DAILY
    if policy not in config.PATROLS:
        return jsonify({"error": "未知巡检策略"}), 400
    room_id = (data.get("room_id") or "").strip() or None
    session = (data.get("session") or "").strip() or None
    s = RUNNER.run(policy=policy, room_id=room_id, session=session,
                   operator=request.user["u"])
    created = []
    for ck in s["results"]:
        d = STORE.get_device(device_id=ck.device_id)
        if d is not None and not ck.ok:
            wo, _msg = ORDERS.ensure_from_check(ck, d, operator=request.user["u"])
            if wo is not None:
                created.append(wo.order_id)
    return jsonify({"message": "%s完成：%d 台，异常 %d 台，在线率 %s"
                               % (s["name"], s["total"], s["bad"],
                                  "—" if s["online_rate"] is None
                                  else "%.2f%%" % (s["online_rate"] * 100)),
                    "total": s["total"], "bad": s["bad"],
                    "online_rate": s["online_rate"],
                    "bad_devices": s["bad_devices"],
                    "orders": sorted(set(created))})


@app.route("/api/checks", methods=["GET"])
@require()
def list_checks():
    policy = request.args.get("policy") or None
    room_id = request.args.get("room_id") or None
    only_bad = request.args.get("only_bad") == "1"
    rows = STORE.list_checks(policy=policy, room_id=room_id, only_bad=only_bad, limit=300)
    return jsonify({"checks": [{
        "check_id": c.check_id, "ts": c.ts, "device_code": c.device_code,
        "room_id": c.room_id, "policy": c.policy,
        "policy_text": config.PATROLS.get(c.policy, {}).get("name", c.policy),
        "online": bool(c.online), "ok": c.ok,
        "defects": [x.to_dict() for x in c.defects],
        "metrics": {k: v for k, v in c.metrics.items() if k != "baseline_metrics"},
        "frame_path": c.frame_path,
    } for c in rows]})


# ---------------- 工单 ----------------
@app.route("/api/orders", methods=["GET"])
@require()
def list_orders():
    status = request.args.get("status") or None
    rows = STORE.list_orders(status=status, limit=300)
    return jsonify({"orders": [{
        "order_id": o.order_id, "device_code": o.device_code, "room_id": o.room_id,
        "codes": o.codes, "code_names": [config.defect_name(c) for c in o.codes],
        "severity": o.severity, "status": o.status,
        "status_text": wo_mod.STATUS_TEXT.get(o.status, o.status),
        "assigned_to": o.assigned_to, "created_at": o.created_at,
        "dispatched_at": o.dispatched_at, "rectified_at": o.rectified_at,
        "reviewed_at": o.reviewed_at, "reviewer": o.reviewer,
        "note": o.note, "review_note": o.review_note,
        "before_path": os.path.basename(o.before_path or ""),
        "after_path": os.path.basename(o.after_path or ""),
    } for o in rows], "stats": ORDERS.stats()})


@app.route("/api/orders/<order_id>/dispatch", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def order_dispatch(order_id):
    data = request.get_json(silent=True) or {}
    ok, msg = ORDERS.dispatch(order_id, data.get("assigned_to") or "",
                              request.user["u"], data.get("note") or "")
    return jsonify({"message": msg}), (200 if ok else 400)


@app.route("/api/orders/<order_id>/rectify", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def order_rectify(order_id):
    """整改：必须上传整改后截图（表单字段 file）"""
    f = request.files.get("file")
    note = request.form.get("note") or ""
    path = None
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".mp4"):
            return jsonify({"error": "整改后截图仅支持 jpg/png/bmp/webp/mp4"}), 400
        tmpdir = os.path.join(str(config.DATA_ROOT), "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        path = os.path.join(tmpdir, "after_upload" + ext)
        f.save(path)
    try:
        ok, msg = ORDERS.rectify(order_id, path, note, request.user["u"])
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    return jsonify({"message": msg}), (200 if ok else 400)


@app.route("/api/orders/<order_id>/review", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def order_review(order_id):
    data = request.get_json(silent=True) or {}
    ok, msg = ORDERS.review(order_id, bool(data.get("ok", True)),
                            data.get("note") or "", request.user["u"])
    return jsonify({"message": msg, "passed": bool(data.get("ok", True))}), \
        (200 if ok else 400)


# ---------------- 指标与报告 ----------------
@app.route("/api/kpi", methods=["GET"])
@require()
def kpi():
    return jsonify(RUNNER.kpi())


@app.route("/api/stats", methods=["GET"])
@require()
def stats():
    return jsonify({"defects": STORE.defect_stats(),
                    "online_rate": STORE.online_rate(),
                    "orders": ORDERS.stats(),
                    "summary": STORE.summary() if hasattr(STORE, "summary") else {}})


@app.route("/api/report", methods=["GET"])
@require()
def report():
    text = REPORTER.full_report()
    if request.args.get("download") == "1":
        return jsonify({"report": text})
    return jsonify({"report": text})


@app.route("/api/report/export", methods=["POST"])
@require(roles=[ROLE_OWNER, ROLE_ADMIN])
def export_report():
    path = REPORTER.export(REPORTER.full_report())
    if not path:
        return jsonify({"error": "报告写入失败"}), 500
    return jsonify({"message": "已导出：%s" % os.path.basename(path),
                    "path": path})


@app.route("/api/templates/<name>")
@require()
def get_template(name):
    """下载名单/台账模板（与 01 的模板口径保持一致的字段顺序）"""
    if name not in ("devices.csv",):
        return jsonify({"error": "模板不存在"}), 404
    body = ("设备编码,设备名称,考场,考点,安装位置,取流地址,分辨率,视场角,水平角,俯仰角,场次,配拾音器\n"
            "CAM-A01-1,A01前墙, A-01,一中考点,前墙左上,rtsp://10.0.0.11:554/stream1,"
            "1920x1080,90,0,-10,6月7日上午,否\n")
    return app.response_class(body.encode("utf-8-sig"), mimetype="text/csv",
                              headers={"Content-Disposition":
                                       "attachment; filename=devices_template.csv"})


@app.route("/api/audit", methods=["GET"])
@require(roles=[ROLE_OWNER])
def audit():
    if not can("audit_view", request.user["r"]):
        return jsonify({"error": web_accounts.require_text("audit_view",
                                                           request.user["r"])}), 403
    ok, msg, n = STORE.verify_chain()
    return jsonify({"chain_ok": ok, "message": msg, "count": n,
                    "rows": STORE.audit_rows(limit=200)})


if __name__ == "__main__":
    ensure_accounts()
    print("=" * 60)
    print("摄像头核查与视频质量诊断 · 网页端")
    print("数据目录：%s" % config.DATA_ROOT)
    print("账户：owner / EDY_POWERQ（所有者）　teacher1 / teacher1（管理员）")
    print("账户与本机桌面界面（gui.py）是同一份：web.accounts @ %s" % config.DB_PATH)
    print("隐藏命令行窗口标记：%s（%s）"
          % (config.HIDE_CMD_FLAG, "开" if web_accounts.hidden_cmd_on() else "关"))
    print("=" * 60)
    app.run(host="127.0.0.1", port=PORT, debug=False)
