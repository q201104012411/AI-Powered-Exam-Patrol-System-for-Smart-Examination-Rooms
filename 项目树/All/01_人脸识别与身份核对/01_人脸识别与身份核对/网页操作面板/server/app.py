# -*- coding: utf-8 -*-
"""
巡考业务平台 · 考生数据管理接口。

修正的旧版问题：
1. CORS 从 `*` 收紧为白名单（环境变量 ALLOWED_ORIGINS，默认只允许本机）；
2. 接口不再返回身份证号明文（只返回掩码），任何角色都拿不到完整号码；
3. 保存考生数据改为事务化 UPSERT，且逐条校验——
   旧版是"先 DELETE 全表再逐条 INSERT"，中途出错会丢库；
4. 新增单独同意登记接口与数据变更留痕；
5. 启动时不打印明文口令（初始口令为账户名 admin，另写本地文件提示）。

本版新增：
6. 登录后**不再强制改密**：/api/login 与 /api/me 照旧返回 must_change_password，
   但前端只做温和提示（顶部横幅），不再弹窗拦截；
7. 新增所有者开关端点 /api/owner-settings（GET 全员可读 / POST 仅 owner）：
     · 大考模式（默认关闭）—— 开启后核验必须使用身份证（四要素）；
     · 严格模式（默认开启）—— 关闭后允许上传照片核验，记录里会留痕；
   /api/verify 也遵守这两个开关。
"""
import os
import sys
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
if FEATURE_DIR not in sys.path:
    sys.path.insert(0, FEATURE_DIR)

import config                                        # noqa: E402
import accounts                                      # noqa: E402
from audit import AuditLogger                        # noqa: E402

# pythonw 隐藏窗口启动时 stdout/stderr 为 None，必须先兜住，否则 print 会让后端起不来
config.ensure_stdio("web_server.log")
from pii import mask_id_card, validate_id_card       # noqa: E402
from store import CandidateStore                     # noqa: E402

from db import get_db, init_db, log_login            # noqa: E402
from auth import (                                   # noqa: E402
    SESSION_INFO, generate_token, hash_password, require_auth,
    validate_password_strength, verify_password,
)

app = Flask(__name__)

_origins = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://127.0.0.1:5000,http://localhost:5000").split(",") if o.strip()]
CORS(app, resources={r"/api/*": {"origins": _origins}})

limiter = Limiter(get_remote_address, app=app, default_limits=[],
                  storage_uri=os.environ.get("RATE_LIMIT_STORAGE", "memory://"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 前端文件在上一级目录：…\01_人脸识别与身份核对\网页操作面板\candidates_editor.html
# （旧版写成 BASE_DIR\..\网页操作面板，多套了一层，导致访问首页 404）
HTML_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))
HTML_FILE = "candidates_editor.html"
VENDOR_DIR = os.path.join(HTML_DIR, "vendor")

store = CandidateStore()


def _mode_payload():
    """
    当前所有者开关的状态（供前端渲染文案与按钮，避免前端硬编码"必须身份证"之类的说法）：
      · major_exam  —— 大考模式（默认关闭）：开启后核验必须使用身份证（四要素）
      · strict_mode —— 严格模式（默认开启）：关闭后允许用本地图片核验（会留痕）
      · cmd_console —— 命令控制台（默认关闭）：开启后所有者可执行完整命令；
                       再由 cmd_console_admin 决定是否放给管理员（仅 ollama 辅助）
    """
    return {
        "major_exam": accounts.major_exam_enabled(),
        "strict_mode": accounts.strict_mode_enabled(),
        "cmd_console": accounts.cmd_console_enabled(),
        "cmd_console_admin": accounts.cmd_console_admin_allowed(),
        "identity_mode": "major" if accounts.major_exam_enabled() else "normal",
        "identity_title": accounts.identity_mode_title(),
        "identity_text": accounts.identity_mode_text(),
        "identity_detail": accounts.identity_mode_detail(),
    }


# ---------------- 静态页 ----------------
@app.route("/")
def index():
    return send_from_directory(HTML_DIR, HTML_FILE)


@app.route("/vendor/<path:fname>")
def vendor(fname):
    """本地化的前端第三方库（考场内网通常无法访问公网 CDN）"""
    return send_from_directory(VENDOR_DIR, fname)


# ---------------- 登录 / 账户 ----------------
@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = get_remote_address()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()

    if not user or not user["is_active"]:
        log_login(username, ip, False, "账户不存在或已禁用")
        return jsonify({"error": "用户名或密码错误"}), 401
    if not verify_password(password, user["password_hash"]):
        log_login(username, ip, False, "口令错误")
        return jsonify({"error": "用户名或密码错误"}), 401

    conn = get_db()
    try:
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), user["id"]))
        conn.commit()
    finally:
        conn.close()
    log_login(username, ip, True, "")

    return jsonify({
        "token": generate_token(user["id"], user["username"], user["role"]),
        "user": {"id": user["id"], "username": user["username"], "role": user["role"],
                 "display_name": user["display_name"],
                 # 仍然返回 must_change_password：前端据此**提示**（不再强制拦截）
                 "must_change_password": bool(user["must_change_password"]),
                 "can_auto_verify": accounts.can_auto_verify(user["role"]),
                 "can_manual_add": accounts.can_manual_add(user["role"]),
                 "can_clear_logs": accounts.can_clear_logs(user["role"])},
        "modes": _mode_payload(),
    })


@app.route("/api/change-password", methods=["POST"])
@require_auth()
def change_password():
    data = request.get_json(silent=True) or {}
    old_pwd = data.get("old_password") or ""
    new_pwd = data.get("new_password") or ""

    ok, msg = validate_password_strength(new_pwd)
    if not ok:
        return jsonify({"error": msg}), 400
    if old_pwd == new_pwd:
        return jsonify({"error": "新密码不能与旧密码相同"}), 400

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (request.user["uid"],)).fetchone()
        if not user or not verify_password(old_pwd, user["password_hash"]):
            return jsonify({"error": "旧密码错误"}), 401
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                     (hash_password(new_pwd), user["id"]))
        conn.commit()
    finally:
        conn.close()

    # 改密后删除初始密码文件，避免明文口令长期留在磁盘上
    from db import INIT_PWD_FILE
    try:
        if os.path.exists(INIT_PWD_FILE):
            os.remove(INIT_PWD_FILE)
    except OSError:
        pass
    return jsonify({"message": "密码修改成功"})


@app.route("/api/me", methods=["GET"])
@require_auth()
def me():
    conn = get_db()
    try:
        u = conn.execute("SELECT id, username, role, display_name, must_change_password "
                         "FROM users WHERE id=?", (request.user["uid"],)).fetchone()
    finally:
        conn.close()
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    import accounts
    return jsonify({"id": u["id"], "username": u["username"], "role": u["role"],
                    "display_name": u["display_name"],
                    "must_change_password": bool(u["must_change_password"]),
                    "can_auto_verify": accounts.can_auto_verify(u["role"]),
                    "can_manual_add": accounts.can_manual_add(u["role"]),
                    "can_clear_logs": accounts.can_clear_logs(u["role"])})


# ---------------- 所有者设置（两个开关：大考模式 / 严格模式） ----------------
@app.route("/api/owner-settings", methods=["GET"])
@require_auth()
def get_owner_settings():
    """
    读取两个所有者开关。**任何已登录用户都可读**（前端要据此显示正确的核验文案），
    但只有 owner 能改（见 POST）。
    """
    return jsonify(_mode_payload())


@app.route("/api/owner-settings", methods=["POST"])
@require_auth(roles=["owner"])
def set_owner_settings():
    """
    修改所有者开关（服务端强校验 owner，绕过前端也无效）：
      {"major_exam": bool, "strict_mode": bool}
    两个键都可选，只传哪个就改哪个。返回修改后的完整状态。
    """
    from action_logger import ActionLogger
    data = request.get_json(silent=True) or {}
    changed = []
    if "major_exam" in data:
        on = bool(data.get("major_exam"))
        ok, msg = accounts.set_major_exam(on)
        if not ok:
            return jsonify({"error": msg}), 500
        changed.append("大考模式=%s" % ("开启" if on else "关闭"))
    if "strict_mode" in data:
        on = bool(data.get("strict_mode"))
        ok, msg = accounts.set_strict_mode(on)
        if not ok:
            return jsonify({"error": msg}), 500
        changed.append("严格模式=%s" % ("开启" if on else "关闭"))
    if "cmd_console" in data:
        on = bool(data.get("cmd_console"))
        ok, msg = accounts.set_cmd_console(on)
        if not ok:
            return jsonify({"error": msg}), 500
        changed.append("命令控制台=%s" % ("开启" if on else "关闭"))
    if "cmd_console_admin" in data:
        on = bool(data.get("cmd_console_admin"))
        ok, msg = accounts.set_cmd_console_admin(on)
        if not ok:
            return jsonify({"error": msg}), 500
        changed.append("管理员AI辅助=%s" % ("授权" if on else "收回"))
    if not changed:
        return jsonify({"error": "未提供任何可修改项"
                                 "（major_exam / strict_mode / cmd_console / cmd_console_admin）"}), 400

    detail = "；".join(changed)
    store.log_change("SET_OWNER_SWITCH", detail=detail, operator=request.user["username"])
    try:
        ActionLogger().log("所有者设置（网页端）", "操作人=%s %s"
                           % (request.user["username"], detail))
    except Exception:
        pass
    payload = _mode_payload()
    payload["message"] = "已更新：%s（立即生效）" % detail
    return jsonify(payload)


# ---------------- 考生数据 ----------------
@app.route("/api/records", methods=["GET"])
@require_auth()
def get_records():
    """
    操作记录：核验审计（哈希链）+ 界面操作日志。
    所有登录用户都可查看；是否允许清空由角色决定（返回 can_clear 供前端渲染按钮）。
    """
    import accounts
    from action_logger import ActionLogger
    from audit import AuditLogger
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    except (TypeError, ValueError):
        limit = 200
    return jsonify({
        "audit": AuditLogger().query(limit=limit),
        "actions": ActionLogger().query(limit=limit),
        "can_clear": accounts.can_clear_logs(request.user["role"]),
        "role": request.user["role"],
    })


@app.route("/api/records/clear", methods=["POST"])
@require_auth(roles=["owner"])
def clear_records():
    """
    清空日志：**仅所有者（owner）**。管理员调用会在这里被拒（403），
    即使绕过前端按钮也一样，因为角色校验在服务端。
    """
    import accounts
    from action_logger import ActionLogger
    from audit import AuditLogger
    if not accounts.can_clear_logs(request.user["role"]):
        return jsonify({"error": "只有所有者可以清空日志"}), 403
    a = AuditLogger().clear_all(operator=request.user["username"])
    b = ActionLogger().clear(operator=request.user["username"])
    return jsonify({"removed_audit": a["removed"], "removed_actions": b["removed"],
                    "backup_audit": a["backup"], "backup_actions": b["backup"]})


# ---------------- 命令控制台（默认关闭；所有者 full / 管理员 ollama） ----------------
@app.route("/api/cmd-console", methods=["GET"])
@require_auth(roles=["owner", "admin"])
def cmd_console_status():
    """
    命令控制台状态。只对 owner / admin 开放——教师等角色连"这台机器有没有开这个入口"
    都不应该知道，所以这里不用 require_auth() 而是收窄角色。

    返回体里的 tier 说明调用方实际处于哪一档（full / ollama / none），前端据此渲染。
    """
    import cmd_console
    return jsonify(cmd_console.status(request.user["role"]))


@app.route("/api/cmd-console/run", methods=["POST"])
@require_auth(roles=["owner", "admin"])
@limiter.limit("60 per minute")
def cmd_console_run():
    """
    执行命令 / 代码。四重校验，缺一不可：
      1. @require_auth(roles=["owner","admin"]) —— 角色每次请求回查数据库，token 里的旧角色无效；
      2. accounts.can_cmd_console()             —— 总开关必须已开（默认关闭）；
      3. cmd_console.execute(role=...)          —— **由服务端按角色决定层级**，不由请求方指定；
      4. 执行过程写入审计哈希链（含被拒绝的尝试）。

    ⚠️ 关键：管理员即使把请求体里的 mode 改成 "python"，也不会得到完整权限——
    层级由 accounts.cmd_console_tier(role) 决定，管理员恒为 ollama，
    execute() 会把 mode 强制为 "ollama" 并对命令做白名单解析。

    请求体：{"code": "...", "mode": "cmd"|"python"|"ollama", "timeout": 秒}
    """
    import accounts as _accounts
    import cmd_console

    if not _accounts.can_cmd_console(request.user["role"]):
        return jsonify({"error": "命令控制台未开启，或当前角色无权执行。"
                                 "请由所有者在「所有者设置」中开启。"}), 403

    data = request.get_json(silent=True) or {}
    code = data.get("code") or ""
    mode = data.get("mode") or "cmd"
    if mode not in ("cmd", "python", "ollama"):
        return jsonify({"error": "mode 只能是 cmd / python / ollama"}), 400
    try:
        timeout = int(data.get("timeout") or cmd_console.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = cmd_console.DEFAULT_TIMEOUT

    result = cmd_console.execute(code, operator=request.user["username"],
                                 role=request.user["role"],
                                 mode=mode, source="web", timeout=timeout)
    return jsonify(result)


# ---------------- AI 助手（ollama 对话 / 状态分析；仅所有者层级） ----------------
def _ai_tier_or_403():
    """
    AI 助手统一门槛：总开关已开 + 当前角色为所有者（full 层级）。
    与 GUI 保持一致：管理员层级只能用命令控制台里的 ollama 单轮问答。
    返回 (tier, error_response | None)。
    """
    import accounts as _accounts
    if not _accounts.can_cmd_console(request.user["role"]):
        return None, (jsonify({"error": "AI 助手未开启：请先由所有者在"
                                        "「所有者设置」中开启「命令控制台」。"}), 403)
    tier = _accounts.cmd_console_tier(request.user["role"])
    if tier != _accounts.CMD_TIER_FULL:
        return tier, (jsonify({"error": "AI 助手仅所有者可用；"
                                        "管理员请使用命令控制台中的 ollama 辅助。"}), 403)
    return tier, None


def _web_snapshot():
    """
    网页端"AI 调取当前我在干什么"的快照：服务器视角的系统状态 +
    最近操作日志尾部（操作日志天然记录"用户刚做了什么"）。
    """
    from action_logger import ActionLogger
    lines = ["时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
    lines.append("操作员：%s（网页端，角色 %s）"
                 % (request.user["username"], request.user["role"]))
    try:
        cands = store.load()
        consent_yes = sum(1 for c in cands.values()
                          if str(c.get("consent_status", "")).startswith("已同意"))
        lines.append("报名库：共 %d 名考生，其中 %d 名已登记单独同意"
                     % (len(cands), consent_yes))
    except Exception:
        pass
    lines.append("大考模式：%s ｜ 严格模式：%s"
                 % ("开启" if accounts.major_exam_enabled() else "关闭",
                    "开启" if accounts.strict_mode_enabled() else "关闭"))
    try:
        recent = ActionLogger().query(limit=6)
        if recent:
            lines.append("最近操作（新→旧）：\n" + "\n".join(recent))
    except Exception:
        pass
    try:                            # 最近核验记录：带结果与原因码，AI 可据此判断"刚发生了什么"
        audit = AuditLogger()
        recs = audit.query(limit=5)
        if recs:
            lines.append("最近核验/操作记录（新→旧）：")
            for r in recs:
                ts = r.get("timestamp", "") or ""
                cand = r.get("candidate_id") or r.get("device_id") or "-"
                res = r.get("result", "")
                rc = r.get("reason_code", "")
                reason = (r.get("reason") or "")[:80]
                lines.append("  [%s] 考生/设备：%s 结果：%s 原因码：%s %s"
                             % (ts, cand, res, rc, reason))
    except Exception:
        pass
    return "\n".join(lines)


@app.route("/api/ai/models", methods=["GET"])
@require_auth(roles=["owner", "admin"])
def ai_models():
    """列出本机 ollama 可用模型（供 AI 助手的模型选择框）"""
    _tier, err = _ai_tier_or_403()
    if err:
        return err
    import ai_assistant
    return jsonify({"models": ai_assistant.list_models(),
                    "default": ai_assistant.DEFAULT_MODEL})


@app.route("/api/ai/chat", methods=["POST"])
@require_auth(roles=["owner", "admin"])
@limiter.limit("20 per minute")
def ai_chat():
    """
    AI 对话。请求体：{"message": "...", "model": "可选",
                     "history": [{"role":"user"|"assistant","content":"..."}]}
    历史由前端维护并回传（服务端无会话状态），服务端只取最近 20 条，
    并强制以服务端快照 + 系统提示开头——前端改请求体也改不了这两点。
    每次对话都写审计哈希链（ai_assistant 内部完成）。
    """
    tier, err = _ai_tier_or_403()
    if err:
        return err
    import ai_assistant

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "消息为空"}), 400
    model = (data.get("model") or ai_assistant.DEFAULT_MODEL).strip()
    history = data.get("history") or []
    if not isinstance(history, list):
        history = []
    # 只保留合法的 user/assistant 文本消息，最多 20 条
    clean_history = []
    for m in history[-20:]:
        if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)):
            clean_history.append({"role": m["role"], "content": m["content"][:4000]})

    free_mode = bool(data.get("free_mode"))
    if free_mode:
        messages = [
            {"role": "system", "content": "你是一个本地 AI 助手，请用中文自然、友好地回答用户。"},
        ] + clean_history + [{"role": "user", "content": message}]
    else:
        user_content = ("【当前系统状态快照】\n%s\n\n【我的消息】\n%s"
                        % (_web_snapshot(), message))
        messages = ([{"role": "system", "content": ai_assistant.SYSTEM_PROMPT}]
                    + clean_history
                    + [{"role": "user", "content": user_content}])
    try:
        timeout = min(max(int(data.get("timeout") or 120), 30), 300)
    except (TypeError, ValueError):
        timeout = 120

    import time as _time
    started = _time.time()
    ok, reply, err_detail, transport = ai_assistant.ollama_chat(
        model, messages, timeout=timeout)
    duration_ms = int((_time.time() - started) * 1000)
    if ok:
        reply = ai_assistant._strip_think(reply)
    ai_assistant._audit_chat("ai-chat", model, message, ok,
                             reply if ok else err_detail, duration_ms,
                             request.user["username"], tier, "web")
    if not ok:
        return jsonify({"ok": False, "error": err_detail, "transport": transport}), 200
    return jsonify({"ok": True, "reply": reply, "transport": transport})


@app.route("/api/ai/watch", methods=["POST"])
@require_auth(roles=["owner", "admin"])
@limiter.limit("10 per minute")
def ai_watch():
    """AI 巡视：手动触发一次"分析我最近在干什么"（网页端按操作日志分析）"""
    tier, err = _ai_tier_or_403()
    if err:
        return err
    import ai_assistant
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or ai_assistant.DEFAULT_MODEL).strip()
    ok, text, transport = ai_assistant.analyze_activity(
        _web_snapshot(), model=model, operator=request.user["username"],
        tier=tier, source="web")
    if not ok:
        return jsonify({"ok": False, "error": text, "transport": transport}), 200
    return jsonify({"ok": True, "reply": text, "transport": transport})


_VERIFIER_CACHE = {"candidates": None, "index": None}


def _cached_index():
    """报名库与特征索引缓存：避免每次核验都重新 DPAPI 解密索引 + 读库（提速关键）"""
    from face_index import FaceIndex
    if _VERIFIER_CACHE["index"] is None:
        _VERIFIER_CACHE["candidates"] = store.load()
        index = FaceIndex()
        index.load()
        _VERIFIER_CACHE["index"] = index
    return _VERIFIER_CACHE["candidates"], _VERIFIER_CACHE["index"]


def _invalidate_verifier_cache():
    _VERIFIER_CACHE["candidates"] = None
    _VERIFIER_CACHE["index"] = None


def _verifier(device_id, operator):
    """网页端与 GUI 共用同一套核验器（报名库 + 特征索引 + 审计日志）"""
    from verifier import FaceVerifier
    candidates, index = _cached_index()
    return FaceVerifier(candidates, index, device_id=device_id, logger=AuditLogger(),
                        operator=operator)


@app.route("/api/verify", methods=["POST"])
@require_auth()
def api_verify():
    """
    网页端实时核验：接收浏览器摄像头抓拍的照片，跑与 GUI 完全相同的核验流程。
    body: { mode: "1:1"|"1:N", candidate_id?, image: base64, witness?, device_id?,
            source?: "camera"|"file",
            id_card?, ticket_no?, seat_no?   # 身份要素（1:1 时按当前模式校验） }
    任何已登录工作人员（owner/admin/teacher）都可调用。

    两个所有者开关在这里生效（服务端判定，绕过前端也无效）：
      · 严格模式开启（默认）-> source=file（上传照片）一律拒绝放行（LIVENESS_UNAVAILABLE）；
      · 严格模式关闭 -> 允许 file 来源继续核验，但记录里写明 strict_mode_off / non_live_source；
      · 大考模式开启 -> 身份证号必填并走哈希比对；关闭（默认）-> 身份证不参与校验，以准考证号核验。
    """
    import base64
    import cv2
    import liveness
    import numpy as np
    from face_engine import FaceError

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "1:1")
    cid = (data.get("candidate_id") or "").strip()
    img_b64 = (data.get("image") or "").strip()
    witness = (data.get("witness") or "").strip()
    device_id = (data.get("device_id") or "WEB-BROWSER").strip()
    auto_confirm = bool(data.get("auto_confirm", False))   # 自动模式：人脸匹配即放行
    source = (data.get("source") or "camera").strip()      # camera=实时摄像头 / file=上传照片
    # 身份要素（仅 1:1 有意义；未传则保持"只扫脸 + 人工核对证件"的原流程）
    id_card = data.get("id_card")
    ticket_no = data.get("ticket_no")
    seat_no = data.get("seat_no")
    # 自动通过属高危能力：仅所有者可用，或所有者已授权给管理员（服务端强校验，绕过前端也无效）
    import accounts
    if auto_confirm and not accounts.can_auto_verify(request.user["role"]):
        auto_confirm = False

    if not img_b64:
        return jsonify({"error": "缺少图像"}), 400
    if img_b64.startswith("data:") and "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(img_b64)
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return jsonify({"error": "图像解码失败"}), 400
    if frame is None:
        return jsonify({"error": "图像解码失败"}), 400
    if mode == "1:1" and not cid:
        return jsonify({"error": "1:1 核验需指定考生编号"}), 400

    operator = request.user["username"]
    v = _verifier(device_id, operator)
    # 现场性：实时摄像头帧才算现场采集；上传照片标记为 file。
    # 严格模式开启时 file 会被拒（LIVENESS_UNAVAILABLE）；关闭后放行但会在记录里留痕。
    token = liveness.make_token(device_id, source=("camera" if source == "camera" else "file"))
    try:
        if mode == "1:1":
            rec = v.verify_face_first(cid, live_images=[frame], capture_token=token,
                                      witness=witness, auto_confirm=auto_confirm,
                                      id_card=id_card, ticket_no=ticket_no, seat_no=seat_no)
        else:
            rec = v.verify_1toN(frame, capture_token=token, witness=witness,
                                auto_confirm=auto_confirm)
    except FaceError as e:
        return jsonify({"error": e.msg}), 400
    except Exception as e:
        return jsonify({"error": "核验失败：%s" % str(e)[:120]}), 500

    d = rec.to_dict()
    d["doc_info"] = (v.document_info(v.candidates.get(rec.candidate_id))
                     if rec.candidate_id else "")
    # 把当前模式一并回传，前端据此显示"本次按几要素核验 / 严格模式是否关闭"
    d["modes"] = _mode_payload()
    return jsonify(d)


@app.route("/api/doc-check", methods=["POST"])
@require_auth()
def api_doc_check():
    """证件核对确认：核验通过后，工作人员对照证件实物点「证件一致 / 证件不符」"""
    data = request.get_json(silent=True) or {}
    cid = (data.get("candidate_id") or "").strip()
    ok = bool(data.get("ok", True))
    witness = (data.get("witness") or "").strip()
    note = (data.get("note") or "").strip()
    if not cid:
        return jsonify({"error": "缺少考生编号"}), 400
    v = _verifier("WEB-BROWSER", request.user["username"])
    rec = v.confirm_document(cid, operator=request.user["username"], witness=witness,
                             ok=ok, note=note)
    d = rec.to_dict()
    d["doc_info"] = v.document_info(v.candidates.get(cid))
    return jsonify(d)


@app.route("/api/roster-import", methods=["POST"])
@require_auth(roles=["admin", "owner"])
def roster_import():
    """
    名单导入（Excel/CSV）：**仅所有者可用**（服务端强校验，绕过前端也无效）。
    身份证号经 store.upsert_many 加盐哈希存储，明文不落库。
    """
    import roster
    if not accounts.can_clear_logs(request.user["role"]):
        return jsonify({"error": "名单导入仅所有者（owner）可用"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未收到名单文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".csv"):
        return jsonify({"error": "仅支持 .xlsx / .xlsm / .csv 名单文件"}), 400
    tmpdir = os.path.join(str(config.DATA_ROOT), "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    tmp = os.path.join(tmpdir, "roster_import" + ext)
    f.save(tmp)
    try:
        r = roster.import_roster(tmp, operator=request.user["username"])
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    _invalidate_verifier_cache()
    return jsonify({"message": "共 %d 行，写入/更新 %d 条考生记录（身份证号已加盐哈希）"
                               % (r["total"], r["imported"]),
                    "total": r["total"], "imported": r["imported"],
                    "warnings": r["warnings"]})


@app.route("/api/candidates", methods=["GET"])
@require_auth()
def list_candidates():
    """只返回脱敏后的身份证号；完整号码不再通过任何接口下发。
    姓名/准考证号/座位号/参考照路径从加密存储中解密后返回（已登录管理员/所有者可见）。"""
    cands = store.load()
    out = []
    for c in cands.values():
        out.append({
            "candidate_id": c.candidate_id,
            "name": c.name,
            "id_card_masked": c.id_card_masked,
            "ticket_no": c.ticket_no,
            "seat_no": c.seat_no,
            "reference_photo": c.reference_photo,
            "consent_status": c.consent_status,
            "consent_at": c.consent_at,
            "retention_until": c.retention_until,
            "updated_at": c.updated_at,
            "updated_by": c.updated_by,
        })
    out.sort(key=lambda x: x["candidate_id"])
    return jsonify(out)


@app.route("/api/candidates", methods=["POST"])
@require_auth(roles=["admin", "owner"])
def save_candidates():
    """
    事务化 UPSERT：
      - item.id_card 非空 -> 重新登记（校验 + 加盐哈希 + 掩码）
      - item.id_card 为空 -> 保留库中已有身份证信息（仅改其他字段）
    任一条非法则整批回滚。
    """
    # 手动添加/编辑考生：受所有者开关控制（默认关闭）；文件名单导入是另一条独立权限
    if not accounts.can_manual_add(request.user["role"]):
        return jsonify({"error": "手动添加/编辑考生当前已关闭。"
                                 "请由所有者在「操作记录 → 所有者设置」中开启。"}), 403
    data = request.get_json(silent=True) or {}
    items = data.get("candidates") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "数据格式错误或为空"}), 400

    for it in items:
        plain = (it.get("id_card") or "").strip()
        if plain:
            ok, msg = validate_id_card(plain)
            if not ok:
                return jsonify({"error": "%s：%s" % (it.get("candidate_id", "?"), msg)}), 400

    try:
        n = store.upsert_many(items, operator=request.user["username"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "保存失败：%s" % str(e)[:120]}), 500

    _invalidate_verifier_cache()
    return jsonify({"message": "已保存 %d 条考生数据（身份证号已加盐哈希存储，界面仅显示掩码）" % n})


@app.route("/api/candidates/<cid>/consent", methods=["POST"])
@require_auth(roles=["admin", "owner"])
def set_consent(cid):
    """登记单独同意状态：未登记同意的考生在核验端会被直接拒绝（合规硬约束）"""
    data = request.get_json(silent=True) or {}
    status = (data.get("consent_status") or "").strip()
    allowed = ("未登记", "已同意", "已拒绝", "替代通道")
    if status not in allowed:
        return jsonify({"error": "状态不合法，可选：%s" % "、".join(allowed)}), 400
    ok = store.set_consent(cid, status, operator=request.user["username"])
    if not ok:
        return jsonify({"error": "考生不存在"}), 404
    _invalidate_verifier_cache()
    return jsonify({"message": "考生 %s 的同意状态已更新为：%s" % (cid, status)})


# ---------------- 用户管理 ----------------
@app.route("/api/users", methods=["GET"])
@require_auth(roles=["owner"])
def list_users():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, username, role, display_name, must_change_password, is_active,
                   created_at, last_login FROM users ORDER BY id""").fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@require_auth(roles=["owner"])
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "teacher"
    display_name = (data.get("display_name") or username).strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if role not in ("admin", "teacher"):
        return jsonify({"error": "角色不合法"}), 400
    # 越级防护：只能新建等级低于自己的账户（管理员建不了管理员，更建不了所有者）
    if not accounts.can_manage(request.user["role"], role):
        return jsonify({"error": "权限不足：%s 不能新建「%s」账户"
                        % (accounts.role_text(request.user["role"]),
                           accounts.role_text(role))}), 403
    ok, msg = validate_password_strength(password)
    if not ok:
        return jsonify({"error": msg}), 400

    conn = get_db()
    try:
        if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error": "用户名已存在"}), 409
        conn.execute("""
            INSERT INTO users (username, password_hash, role, display_name,
                               must_change_password, created_at)
            VALUES (?, ?, ?, ?, 1, ?)""",
                     (username, hash_password(password), role, display_name,
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    store.log_change("CREATE_USER", detail="新建用户 %s（%s）" % (username, role),
                     operator=request.user["username"])
    return jsonify({"message": "用户 %s 创建成功（首次登录需改密）" % username})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@require_auth(roles=["owner"])
def delete_user(uid):
    if uid == request.user["uid"]:
        return jsonify({"error": "不能删除当前登录账户"}), 400
    conn = get_db()
    try:
        target = conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        # 越级防护：只能删除等级低于自己的账户（管理员删不了管理员/所有者）
        if not accounts.can_manage(request.user["role"], target["role"]):
            return jsonify({"error": "权限不足：%s 不能删除「%s」账户"
                            % (accounts.role_text(request.user["role"]),
                               accounts.role_text(target["role"]))}), 403
        if target["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) AS c FROM users "
                                  "WHERE role='admin' AND is_active=1").fetchone()["c"]
            if admins <= 1:
                return jsonify({"error": "系统必须保留至少一名启用状态的管理员"}), 400
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    store.log_change("DELETE_USER", detail="删除用户 %s" % target["username"],
                     operator=request.user["username"])
    return jsonify({"message": "已删除"})


@app.route("/api/users/<int:uid>/reset-password", methods=["POST"])
@require_auth(roles=["owner"])
def reset_password(uid):
    data = request.get_json(silent=True) or {}
    new_pwd = data.get("new_password") or ""
    ok, msg = validate_password_strength(new_pwd)
    if not ok:
        return jsonify({"error": msg}), 400
    conn = get_db()
    try:
        target = conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        # 越级防护（关键）：管理员不得重置所有者/同级管理员的密码，否则等于直接夺权
        if not accounts.can_manage(request.user["role"], target["role"]):
            return jsonify({"error": "权限不足：%s 不能重置「%s」账户的密码"
                            % (accounts.role_text(request.user["role"]),
                               accounts.role_text(target["role"]))}), 403
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                     (hash_password(new_pwd), uid))
        conn.commit()
    finally:
        conn.close()
    store.log_change("RESET_PASSWORD",
                     detail="重置用户 %s（%s）的密码"
                            % (target["username"], accounts.role_text(target["role"])),
                     operator=request.user["username"])
    return jsonify({"message": "密码已重置，该用户下次登录需修改密码"})


# ---------------- 运行状态（便于现场演示与答辩） ----------------
@app.route("/api/status", methods=["GET"])
@require_auth(roles=["admin", "owner"])
def status():
    logger = AuditLogger()
    chain = logger.verify_chain()
    idx = config.INDEX_PATH
    conn = get_db()
    try:
        cands = conn.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()["c"]
        no_consent = conn.execute(
            "SELECT COUNT(*) AS c FROM candidates "
            "WHERE consent_status NOT IN ('已同意','已取得单独同意','替代通道')").fetchone()["c"]
    finally:
        conn.close()
    return jsonify({
        "配置": config.describe(),
        "所有者开关": _mode_payload(),
        "考生数": cands,
        "未登记单独同意": no_consent,
        "特征索引": {
            "路径": str(idx),
            "存在": os.path.exists(idx),
            "大小KB": round(os.path.getsize(idx) / 1024.0, 1) if os.path.exists(idx) else 0,
        },
        "审计链": chain,
        "密钥来源": SESSION_INFO,
        "CORS 白名单": _origins,
    })


# ---------------- 启动 ----------------
if __name__ == "__main__":
    info = init_db()
    print("=" * 58)
    print("数据库：%s" % config.DB_PATH)
    if info["created_admin"]:
        print("已初始化管理员账户 admin（口令 admin，即账户名）")
        print("初始密码文件：%s" % info["pwd_file"])
        print("首次登录必须改密；改密后该文件会被自动删除。")
    else:
        print("管理员账户已存在。")
    print("密钥来源：%s" % SESSION_INFO)
    print("CORS 白名单：%s" % _origins)
    print("=" * 58)
    print("提示：生产环境请置于反向代理 + HTTPS 之后，不要直接对外暴露本服务。")
    # 绑定地址：默认 127.0.0.1（仅本机）。
    # 需要局域网访问时设置环境变量 HOST=0.0.0.0 —— 但那会同时把**整个面板**
    # （考生数据接口、命令控制台）暴露给同网段，请确认考场网络可信后再开。
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        print("!" * 58)
        print("警告：正在监听 %s —— 整个面板（含命令控制台）对同网段可见。" % host)
        print("      命令控制台等价于本机完全控制权，开启前请确认网络可信、口令已改强。")
        print("!" * 58)
    app.run(host=host, port=int(os.environ.get("PORT", "5000")), debug=False)
