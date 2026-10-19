# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 -> 00_集成联动 / 02 / 03 / 06 的调用客户端

**不改动任何其它模块**：只复用 00_集成联动/linkage.py 已经做好的
  · 子进程 + 最后一行 JSON 协议（各模块都有同名 config/models，同进程 import 必撞）
  · 超时强制终止**整棵进程树**（不用 communicate() 等管道）
  · 沙箱化的数据目录重定向（联动自检不污染真实运行数据）
本模块只负责按需 import、把沙箱设置翻译成 Linkage(...)、统一返回结构、封装业务语义。

两种链路，按现场部署自动选：
  · 同机：03 智能终端在本机 -> 走 00 的子进程调用（`Linkage.raise_alert`）；
  · 跨机：03 跑在另一台设备 -> 配 `terminal_url` / `EXAM_TERMINAL_URL`，
    走 03 `lan_service.py` 的 `POST /api/ingest_alert`（X-Exam-Token 鉴权），
    操作/开关记录走 `POST /api/records`，03 驾驶舱的"远端设备"里能看到 07。

失败绝不炸主流程：任何异常都变成 `{"ok": False, "reason": ...}` 返回，界面如实显示。
"""
import json
import os
import sys
import threading
import time
import traceback

import config

_LOCK = threading.RLock()
_LK = None
_LK_SANDBOX = None
_DISABLED_REASON = ""
LAN_TOKEN_HEADER = "X-Exam-Token"


class LinkageUnavailable(Exception):
    pass


# =============================================================================
# 00 联动层
# =============================================================================
def linkage_dir():
    return str(config.MODULE_DIRS.get("00") or "")


def linkage_available():
    d = config.MODULE_DIRS.get("00")
    if not d or not os.path.isdir(str(d)):
        return False, "00_集成联动 目录不存在：%s" % d
    if not os.path.isfile(os.path.join(str(d), "linkage.py")):
        return False, "缺少 linkage.py"
    return True, "可用"


def module_available(key):
    """对端 integration_api.py 是否在位（只看文件，不启动子进程）。"""
    d = config.MODULE_DIRS.get(key)
    if not d or not os.path.isdir(str(d)):
        return False, "目录不存在：%s" % d
    if not os.path.isfile(os.path.join(str(d), "integration_api.py")):
        return False, "缺少 integration_api.py"
    return True, "可用"


def _import_linkage():
    global _DISABLED_REASON
    ok, why = linkage_available()
    if not ok:
        _DISABLED_REASON = why
        raise LinkageUnavailable(why)
    d = linkage_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import linkage                                        # noqa: WPS433
    except Exception as e:                                    # noqa: BLE001
        _DISABLED_REASON = "%s: %s" % (type(e).__name__, str(e)[:160])
        raise LinkageUnavailable(_DISABLED_REASON)
    return linkage


def _sandbox_root(cfg=None):
    cfg = cfg or config.load_settings()
    return (cfg.get("sandbox_root") or "").strip()


def get_linkage(force_new=False, cfg=None, timeout=None):
    """取（并缓存）一个 Linkage 句柄；沙箱设置变化时自动重建。"""
    global _LK, _LK_SANDBOX
    cfg = cfg or config.load_settings()
    sandbox = _sandbox_root(cfg)
    with _LOCK:
        if force_new:
            _LK = None
            _LK_SANDBOX = None
        if _LK is not None and _LK_SANDBOX == sandbox:
            return _LK
        linkage = _import_linkage()
        sec = int(timeout or cfg.get("linkage_timeout") or 60)
        try:
            if sandbox:
                # 02/03/06 走沙箱；01 用真实数据（本模块不依赖 01）
                lk = linkage.Linkage(sandbox_root=sandbox,
                                     sandbox_keys=("02", "03", "06"),
                                     timeout=sec)
            else:
                lk = linkage.Linkage(timeout=sec)
        except Exception as e:                                # noqa: BLE001
            raise LinkageUnavailable("构造 Linkage 失败：%s: %s"
                                     % (type(e).__name__, str(e)[:160]))
        _LK, _LK_SANDBOX = lk, sandbox
        return lk


def reset():
    global _LK, _LK_SANDBOX, _DISABLED_REASON
    with _LOCK:
        _LK = None
        _LK_SANDBOX = None
        _DISABLED_REASON = ""


def _safe(fn, op, key):
    """统一包装：异常 -> {"ok": False, ...}，绝不把异常抛到界面/接口。"""
    try:
        data = fn()
    except LinkageUnavailable as e:
        return {"ok": False, "reason": str(e), "unavailable": True, "op": op,
                "module": key}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                "op": op, "module": key, "trace": traceback.format_exc()[-300:]}
    if isinstance(data, dict) and data.get("ok") is False:
        if not data.get("reason") and data.get("message"):
            data["reason"] = data["message"]
        data.setdefault("op", op)
        data.setdefault("module", key)
        return data
    return {"ok": True, "data": data, "op": op, "module": key}


# =============================================================================
# 局域网（03 接入服务）
# =============================================================================
def terminal_url(cfg=None):
    cfg = cfg or config.load_settings()
    return (cfg.get("terminal_url") or os.environ.get("EXAM_TERMINAL_URL", "")
            or "").strip().rstrip("/")


def terminal_token(cfg=None):
    cfg = cfg or config.load_settings()
    return (cfg.get("terminal_token") or os.environ.get("EXAM_TERMINAL_TOKEN", "")
            or "").strip()


def lan_mode(cfg=None):
    return bool(terminal_url(cfg))


def _http(method, path, payload=None, cfg=None, timeout=10):
    import urllib.error
    import urllib.request
    url = terminal_url(cfg)
    if not url:
        return False, {"ok": False, "reason": "未配置 03 终端地址（terminal_url）"}
    headers = {"Accept": "application/json"}
    token = terminal_token(cfg)
    if token:
        headers[LAN_TOKEN_HEADER] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as r:
            return True, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            d = json.loads(body)
        except ValueError:
            d = {"ok": False, "reason": "HTTP %s %s" % (e.code, body[:120])}
        d.setdefault("ok", False)
        if not d.get("reason"):
            d["reason"] = "HTTP %s" % e.code
        return False, d
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, {"ok": False, "reason": "请求失败：%s" % str(e)[:160]}


def lan_health(cfg=None):
    ok, data = _http("GET", "/health", cfg=cfg, timeout=5)
    if not ok:
        return {"ok": False, "reason": data.get("reason") or "不可达",
                "url": terminal_url(cfg)}
    return {"ok": True, "url": terminal_url(cfg), "health": data,
            "reason": "可达"}


# =============================================================================
# 业务封装：告警 / 复核 / 记录
# =============================================================================
def _ingest_payload(code, device_code, room_id="", seat_no="", detail="",
                    candidate_id="", candidate_name="", level=None, operator="07_rf",
                    source="07_无线信号监测与屏蔽管控"):
    payload = {"code": code, "device_id": device_code or "RF-UNKNOWN",
               "room_id": room_id or "", "seat_no": str(seat_no or ""),
               "detail": detail or "", "candidate_id": candidate_id or "",
               "candidate_name": candidate_name or "", "operator": operator,
               "source": source}
    if level:
        payload["level"] = level
    return payload


def raise_alert(code, device_code, room_id="", seat_no="", detail="",
                candidate_id="", candidate_name="", level=None, operator="07_rf"):
    """
    把一条 07 的告警送进 03 智能终端。

    同机模式优先走 00 的 `raise_alert`（它会顺带问 02 机位画质可信度）；
    02 不可用/调用失败时**降级为直接调 03**（不做画质降权，仍保证告警送达）。
    配了 terminal_url 时走 03 接入服务的 HTTP。
    """
    payload = _ingest_payload(code, device_code, room_id, seat_no, detail,
                              candidate_id, candidate_name, level, operator)
    if lan_mode():
        def _go():
            ok, data = _http("POST", "/api/ingest_alert", payload)
            if not ok:
                data = dict(data or {})
                data.setdefault("ok", False)
                return data
            data["mode"] = "lan"
            return data
        return _safe(_go, "ingest_alert", "03")

    def _go():
        lk = get_linkage()
        if level:
            # 指定级别（如"关注/警告"->橙色）时直接调 03：00 的 raise_alert 不接 level，
            # 而 03 对 SIGNAL_ABNORMAL 的兜底是红色，不指定会把关注级也变成红。
            try:
                factor, why, _info = lk.camera_credibility(device_code or "RF-UNKNOWN")
                note = "画质可信度 ×%.2f（%s）" % (factor, why)
            except Exception as e:                            # noqa: BLE001
                factor, note = 1.0, "02 不可用，未做画质降权：%s" % str(e)[:80]
            r = lk.p03.call("ingest_alert", dict(payload, credibility=factor))
            r = dict(r or {})
            r["mode"] = "linkage/direct"
            r["credibility_note"] = note
            return r
        try:
            r = lk.raise_alert(code=code, device_code=device_code or "RF-UNKNOWN",
                               room_id=room_id or "", seat_no=str(seat_no or ""),
                               detail=detail or "", candidate_id=candidate_id or "",
                               candidate_name=candidate_name or "", operator=operator)
            r = dict(r or {})
            r["mode"] = "linkage"
            return r
        except Exception:                                     # noqa: BLE001
            # 02 不在（credibility 失败）时兜底：直接送 03，不因画质查询失败丢告警
            r = lk.p03.call("ingest_alert", dict(payload, credibility=1.0))
            r = dict(r or {})
            r["mode"] = "linkage/fallback"
            r["fallback_reason"] = "02 画质可信度查询失败，已跳过降权"
            return r
    return _safe(_go, "ingest_alert", "03")


def review_alert(alert_id, conclusion="confirmed", reason="07 复核",
                 operator="07_rf"):
    def _go():
        lk = get_linkage()
        return lk.close_alert(alert_id, conclusion=conclusion, reason=reason,
                              operator=operator)
    return _safe(_go, "review_alert", "03")


def ack_alert(alert_id, operator="07_rf", note=""):
    r"""
    确认 03 的红色预警。

    03 的规则是"红色预警必须先确认、再填复核结论"，但 03 的 integration_api
    **没有 open 这个 op**。本模块不修改 03，改为在子进程里用 03 **自己的业务对象**
    走一次正规确认（`storage.Store()` + `alerts.AlertHub.ack`），与 03 界面点
    "确认"是同一张表、同一段审计；失败如实返回原因，不静默忽略。
    """
    if lan_mode():
        return {"ok": False, "reason": "远端 03 的确认操作请在其终端界面完成"
                                       "（接入服务只开放告警接入）"}

    def _go():
        lk = get_linkage()
        d = lk.p03.dir
        import tempfile
        script = (
            "# -*- coding: utf-8 -*-\n"
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import storage, alerts\n"
            "store = storage.Store()\n"
            "hub = alerts.AlertHub(store)\n"
            "ok, msg = hub.ack(%r, %r, %r)\n"
            "print(json.dumps({'ok': bool(ok), 'message': str(msg)[:200]},"
            " ensure_ascii=False))\n"
        ) % (d, alert_id, operator, note or "07 频谱告警前置确认")
        fd, path = tempfile.mkstemp(suffix=".py", prefix="rf_ack_")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            env = dict(os.environ)
            env.update(getattr(lk.p03, "env_extra", {}) or {})
            env["PYTHONIOENCODING"] = "utf-8"
            home = getattr(lk.p03, "home", None)
            if home:
                env[lk.p03.HOME_ENV[lk.p03.key]] = home
            import subprocess
            kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "env": env, "cwd": d, "timeout": 90}
            if os.name == "nt":
                kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.run([sys.executable, path], **kw)
            out = (p.stdout or b"").decode("utf-8", "replace").strip()
            for ln in reversed(out.splitlines()):
                if ln.strip().startswith("{"):
                    return json.loads(ln.strip())
            raise RuntimeError("确认无有效输出：%s"
                               % (p.stderr or b"")[:200].decode("utf-8", "replace"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return _safe(_go, "ack_alert", "03")


def list_alerts(limit=20):
    def _go():
        if lan_mode():
            ok, data = _http("GET", "/api/alerts?limit=%d" % int(limit))
            return data
        lk = get_linkage()
        return lk.alerts(limit=limit)
    return _safe(_go, "list_alerts", "03")


def terminal_status():
    def _go():
        if lan_mode():
            ok, data = _http("GET", "/api/status")
            return data
        lk = get_linkage()
        return lk.p03.call("status", {})
    return _safe(_go, "status", "03")


# ---------------- 06 智能安检 ----------------
def security_status():
    def _go():
        return get_linkage().p06.call("status", {})
    return _safe(_go, "status", "06")


def security_alarms(limit=50, pending_only=True):
    def _go():
        return get_linkage().p06.call("list_alarms", {"limit": int(limit),
                                                      "pending_only": bool(pending_only)})
    return _safe(_go, "list_alarms", "06")


# ---------------- 02 摄像头核查 ----------------
def camera_devices():
    def _go():
        return get_linkage().p02.call("devices", {})
    return _safe(_go, "devices", "02")


def camera_credibility(device_code):
    def _go():
        factor, why, info = get_linkage().camera_credibility(device_code)
        return {"factor": factor, "reason": why, "info": info}
    return _safe(_go, "credibility", "02")


# ---------------- 远端记录（03 驾驶舱可见） ----------------
def push_records(store, records, kind="oplog", device_id="RF-07",
                 device_name="07 无线信号监测与屏蔽管控", cfg=None):
    """
    把 07 的操作记录/开关记录/告警摘要推到 03 的局域网接入服务。
    **仅跨机模式可用**：03 的同机 integration_api 没有开放记录写入口（这是 03 的设计）。
    返回 {ok, accepted, duplicated, mode} 或 ok=False + 原因。
    """
    cfg = cfg or config.load_settings()
    if not lan_mode(cfg):
        return {"ok": False, "reason": "未配置 03 终端地址：同机模式请直接在 03 界面查看；"
                                       "跨机部署时配置 terminal_url 即可推送远端记录"}
    if not records:
        return {"ok": True, "accepted": 0, "duplicated": 0, "reason": "空批次"}
    chain = {}
    try:
        ok_chain, n, _msg = store.verify_chain()
        head = (store.list_audits(limit=1) or [None])[0]
        chain = {"head": head.hash if head else "", "count": n,
                 "algo": "sha256", "verified": bool(ok_chain)}
    except Exception:                                         # noqa: BLE001
        chain = {}
    payload = {"device_id": device_id, "device_name": device_name, "kind": kind,
               "records": records, "chain": chain}
    ok, data = _http("POST", "/api/records", payload, cfg=cfg)
    if not ok:
        return {"ok": False, "reason": data.get("reason") or "推送失败",
                "detail": data}
    data = dict(data or {})
    data["mode"] = "lan"
    return data


# =============================================================================
# 状态
# =============================================================================
def status(deep=False, cfg=None):
    cfg = cfg or config.load_settings()
    out = {"linkage_available": linkage_available()[1],
           "linkage_reason": linkage_available()[0],
           "sandbox_root": _sandbox_root(cfg) or "(真实数据目录)",
           "terminal_url": terminal_url(cfg) or "(未配置，走同机子进程)",
           "mode": "lan" if lan_mode(cfg) else "local",
           "modules": {}}
    for key in ("01", "02", "03", "06"):
        ok, why = module_available(key)
        out["modules"][key] = {"name": config.MODULE_NAMES.get(key, key),
                               "available": ok, "reason": why}
    if not deep:
        return out
    # 只深探测 07 真正依赖的 02/03/06：01 与本模块无关，深探测会触发 01 自己的
    # 报名库初始化（属于对其它模块的副作用），所以状态里只保留它的"接口是否在位"。
    for key in ("02", "03", "06"):
        if not out["modules"][key]["available"]:
            continue

        def _mk(k):
            def _go():
                lk = get_linkage()
                proj = {"02": lk.p02, "03": lk.p03, "06": lk.p06}[k]
                return proj.call("status", {})
            return _go
        r = _safe(_mk(key), "status", key)
        out["modules"][key]["status"] = r.get("data") or {}
        out["modules"][key]["ok"] = bool(r.get("ok"))
        if not r.get("ok"):
            out["modules"][key]["reason"] = r.get("reason")
    return out


def available():
    ok, why = linkage_available()
    if not ok:
        return False, why
    return True, "00_集成联动可用（沙箱：%s；03 链路：%s）" % (
        _sandbox_root() or "关闭", "局域网" if lan_mode() else "同机子进程")


def describe():
    st = status()
    return {"联动模式": "局域网 HTTP" if st["mode"] == "lan" else "同机子进程",
            "00 集成联动": st["linkage_available"],
            "03 终端地址": st["terminal_url"],
            "沙箱": st["sandbox_root"],
            "模块": "、".join("%s=%s" % (k, "可用" if v["available"] else "不可用")
                             for k, v in st["modules"].items())}
