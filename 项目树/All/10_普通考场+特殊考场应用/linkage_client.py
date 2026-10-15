# -*- coding: utf-8 -*-
r"""
10 -> 00_集成联动 的调用客户端（**不改动任何其他模块**）

为什么不自己写 subprocess：00_集成联动/linkage.py 已经把
  * 进程树终止（taskkill /T）
  * 双线程收管道（不用 communicate()，避免孙进程占管道卡死）
  * 沙箱化的各系统数据目录重定向（嵌套调用不漏回真实目录）
  * 最后一行 JSON 解析
全部做完了，而且 01/02/03/06 的 integration_api.py 协议是它定的。
本模块**只复用**它，不复制、不改写，保证对端升级后本模块不用跟。

本模块负责：
  1) 按需 import linkage（不存在就如实报告不可用，不让主流程崩）
  2) 把本模块的沙箱设置翻译成 Linkage(sandbox_root=..., sandbox_keys=...)
  3) 把各系统返回统一成 {"ok": bool, "data": {...}, "reason": str}
  4) 提供业务语义封装：机位画质 / 告警分流 / 人脸核验 / 安检过检 / 状态
"""
import os
import sys
import threading
import time
import traceback

import config

_LOCK = threading.RLock()
_LK = None
_LK_DISABLED_REASON = ""


class LinkageUnavailable(Exception):
    pass


def linkage_dir():
    return str(config.LINKAGE_DIR)


def module_available(key):
    """对端 integration_api.py 是否在位（只看文件，不启动子进程）。"""
    d = str(config.MODULE_DIRS.get(key, ""))
    if not d or not os.path.isdir(d):
        return False, "目录不存在：%s" % d
    api = os.path.join(d, "integration_api.py")
    if not os.path.isfile(api):
        return False, "缺少 integration_api.py"
    return True, "可用"


def linkage_available():
    if not config.LINKAGE_DIR.is_dir():
        return False, "00_集成联动 目录不存在"
    if not (config.LINKAGE_DIR / "linkage.py").is_file():
        return False, "缺少 linkage.py"
    return True, "可用"


def _import_linkage():
    global _LK_DISABLED_REASON
    ok, why = linkage_available()
    if not ok:
        _LK_DISABLED_REASON = why
        raise LinkageUnavailable(why)
    d = linkage_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import linkage                                    # noqa: WPS433
    except Exception as e:                                # noqa: BLE001
        _LK_DISABLED_REASON = "%s: %s" % (type(e).__name__, str(e)[:160])
        raise LinkageUnavailable(_LK_DISABLED_REASON)
    return linkage


def sandbox_settings():
    """当前生效的沙箱设置（来自 config.load_settings）。"""
    s = config.load_settings()
    return (s.get("sandbox_root") or "").strip(), s


def get_linkage(force_new=False):
    """
    取（并缓存）一个 Linkage 句柄。

    沙箱设置变化时会自动重建，避免自检/生产互相串数据目录。
    """
    global _LK
    with _LOCK:
        if force_new:
            _LK = None
        sandbox_root, s = sandbox_settings()
        if _LK is not None:
            cur = getattr(_LK, "_dsh_sandbox_root", None)
            if cur == (sandbox_root or None):
                return _LK
        linkage = _import_linkage()
        try:
            if sandbox_root:
                # 02/03/06 走沙箱；01 用真实数据（否则人脸库没人，核验演示不出效果）
                lk = linkage.Linkage(
                    sandbox_root=sandbox_root,
                    sandbox_keys=("02", "03", "06"),
                    timeout=int(s.get("linkage_timeout") or
                                config.LINKAGE_DEFAULT_TIMEOUT))
            else:
                lk = linkage.Linkage(
                    timeout=int(s.get("linkage_timeout") or
                                config.LINKAGE_DEFAULT_TIMEOUT))
        except Exception as e:                            # noqa: BLE001
            raise LinkageUnavailable("构造 Linkage 失败：%s: %s"
                                     % (type(e).__name__, str(e)[:160]))
        try:
            lk._dsh_sandbox_root = sandbox_root or None
        except Exception:                                 # noqa: BLE001
            pass
        _LK = lk
        return _LK


def reset():
    """丢掉缓存句柄（换沙箱 / 回归测试用）。"""
    global _LK, _LK_DISABLED_REASON
    with _LOCK:
        _LK = None
        _LK_DISABLED_REASON = ""
    reset_device_cache()


def _safe(fn, op, key):
    """统一包装：任何异常都变成 {"ok": False, "reason": ...}，绝不炸主流程。"""
    try:
        data = fn()
    except LinkageUnavailable as e:
        return {"ok": False, "reason": str(e), "unavailable": True, "op": op,
                "module": key}
    except Exception as e:                                # noqa: BLE001
        # 对端（比如 01 的人脸库没建、02 的库被占用）报错时如实回传原因
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                "op": op, "module": key,
                "trace": traceback.format_exc()[-300:]}
    if isinstance(data, dict) and data.get("ok") is False:
        # 各系统"失败"时字段名不统一（02/06 用 reason，03 用 message），
        # 这里统一补一个 reason，调用方只看一个字段就行。
        if not data.get("reason") and data.get("message"):
            data["reason"] = data["message"]
        data.setdefault("op", op)
        data.setdefault("module", key)
        return data
    return {"ok": True, "data": data, "op": op, "module": key}


# =============================================================================
# 业务语义封装
# =============================================================================
def camera_credibility(device_code):
    """问 02：该机位告警可信度系数（画质不可信 -> 降权）。"""
    def _go():
        lk = get_linkage()
        factor, why, info = lk.camera_credibility(device_code)
        return {"factor": float(factor), "reason": why,
                "defects": (info or {}).get("defects", []),
                "checked_at": (info or {}).get("checked_at", "")}
    return _safe(_go, "credibility", "02")


def inspect_camera(image, device_code="", device_id="", operator="10_room"):
    """让 02 现场诊断一张画面（有真实图时用；无图时由本模块如实标未检）。"""
    def _go():
        lk = get_linkage()
        return lk.inspect_site(image, device_code=device_code,
                               device_id=device_id, operator=operator)
    return _safe(_go, "inspect", "02")


def raise_alert(code, device_code, room_id="", seat_no="", detail="",
                candidate_id="", candidate_name="", level=None,
                operator="10_room"):
    """把告警送进 03（02 的画质可信度由 00 联动自动带上，低可信度红色自动降级）。"""
    def _go():
        lk = get_linkage()
        # level 是可选下传：00 的 raise_alert 不接 level，这里只在需要时直接调 03
        if level:
            factor, why, _info = lk.camera_credibility(device_code)
            r = lk.p03.call("ingest_alert", {
                "code": code, "device_id": device_code, "room_id": room_id,
                "seat_no": seat_no, "detail": detail,
                "candidate_id": candidate_id, "candidate_name": candidate_name,
                "level": level, "credibility": factor, "operator": operator,
                "source": "10_room"})
            r["credibility"] = factor
            r["credibility_reason"] = why
            return r
        return lk.raise_alert(code, device_code, room_id=room_id, seat_no=seat_no,
                              detail=detail, candidate_id=candidate_id,
                              candidate_name=candidate_name, operator=operator)
    return _safe(_go, "ingest_alert", "03")


def review_alert(alert_id, conclusion="confirmed", reason="10 复核", operator="10_room"):
    def _go():
        lk = get_linkage()
        return lk.close_alert(alert_id, conclusion=conclusion, reason=reason,
                              operator=operator)
    return _safe(_go, "review_alert", "03")


def ack_alert(alert_id, operator="10_room", note=""):
    r"""
    确认红色预警（ack）。

    为什么要这一步：03 的复核规则是"**红色预警必须先确认，再填写复核结论**"
    （03/review.py 第 61-62 行）。而 03 对外只开了 integration_api 的四个 op
    （ingest_alert / review_alert / list_alerts / status）——**没有 ack 的 op**。

    本模块不修改 03 的任何代码，改为用 03 **自己的业务对象**走一次正规确认：
      storage.Store() + alerts.AlertHub(...).ack(alert_id, operator, note)
    这与 03 界面点"确认"走的是同一个方法、同一张表、同一条审计，
    （03/alerts.py 第 69-83 行）。所以留痕、状态流转、审计链都不会缺。
    03 以后改了方法名，这里会如实返回失败原因让调用方看见，不静默失败。
    """
    def _go():
        import json as _json
        import os as _os
        import subprocess as _sp
        import sys as _sys
        import tempfile
        lk = get_linkage()
        d = lk.p03.dir
        script = (
            "# -*- coding: utf-8 -*-\n"
            "import json, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "import storage, alerts\n"
            "store = storage.Store()\n"
            "hub = alerts.AlertHub(store)\n"
            "ok, msg = hub.ack(%r, %r, %r)\n"
            "print(json.dumps({'ok': bool(ok), 'message': str(msg)[:200]},"
            " ensure_ascii=False))\n"
        ) % (d, alert_id, operator, note or "10 考场复核前置确认")
        fd, path = tempfile.mkstemp(suffix=".py", prefix="dsh_ack_")
        _os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            env = dict(_os.environ)
            env.update(lk.p03.env_extra)
            env["PYTHONIOENCODING"] = "utf-8"
            if lk.p03.home:
                env[lk.p03.HOME_ENV[lk.p03.key]] = lk.p03.home
            kw = {"stdin": _sp.DEVNULL, "stdout": _sp.PIPE, "stderr": _sp.PIPE,
                  "env": env, "cwd": d, "timeout": 90}
            if _os.name == "nt":
                kw["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0)
            p = _sp.run([_sys.executable, path], **kw)
            out = (p.stdout or b"").decode("utf-8", "replace").strip()
            for ln in reversed(out.splitlines()):
                if ln.strip().startswith("{"):
                    return _json.loads(ln.strip())
            raise RuntimeError("ack 无有效输出：%s"
                               % (p.stderr or b"")[:200].decode("utf-8", "replace"))
        finally:
            try:
                _os.remove(path)
            except OSError:
                pass
    return _safe(_go, "ack_alert", "03")


def list_terminal_alerts(limit=20):
    def _go():
        lk = get_linkage()
        return lk.alerts(limit=limit)
    return _safe(_go, "list_alerts", "03")


def face_verify(image, candidate="", mode="1:1", device_code="", operator="10_room"):
    """01 人脸核验（**仅增强**：无障碍等场景本模块会拒绝把它当唯一准入）。"""
    def _go():
        lk = get_linkage()
        return lk.face_verify(image, candidate=candidate, mode=mode,
                              device_code=device_code, operator=operator)
    return _safe(_go, "verify", "01")


def security_pass(payload):
    """06 安检过检（事前阻断）。"""
    def _go():
        lk = get_linkage()
        return lk.security_pass(payload)
    return _safe(_go, "ingest_pass", "06")


def security_reinspect(alarm_id, result="release", operator="10_room", note="10 转办"):
    def _go():
        lk = get_linkage()
        return lk.security_reinspect(alarm_id, result=result, operator=operator,
                                     note=note)
    return _safe(_go, "reinspect", "06")


def camera_devices():
    """02 机位台账（考场套模板后自动带出机位编码）。"""
    def _go():
        lk = get_linkage()
        return lk.p02.call("devices", {})
    return _safe(_go, "devices", "02")


_DEV_CACHE = {"key": None, "codes": None, "at": 0.0}


def camera_registered_codes(refresh=False, max_age_sec=15.0):
    """
    取 02 台账里已登记的机位编码集合（带缓存）。

    为什么要这一步：02 对**未知机位**的回答是"不降权"（factor=1.0），
    如果只看系数，一个填错的机位编码也会"看起来通过"——那就成了假通过。
    所以必须能把"台账里根本没这个机位"单独认出来。
    返回 (set|None, 说明)：None 表示 02 不可用（调用方需如实判为无法自检）。

    缓存策略：**失败结果不缓存**（02 暂时不可用/沙箱刚切换时，不能把"读不到"
    记成"没有机位"，否则后续一直误判）。
    """
    sandbox = config.load_settings().get("sandbox_root") or ""
    cache_key = (sandbox, str(config.MODULE_DIRS.get("02")))
    if not refresh and _DEV_CACHE["key"] == cache_key \
            and _DEV_CACHE["codes"] is not None \
            and (time.time() - _DEV_CACHE["at"]) < float(max_age_sec):
        return _DEV_CACHE["codes"], "缓存（%d 个机位）" % len(_DEV_CACHE["codes"])
    res = camera_devices()
    if not res.get("ok"):
        return None, str(res.get("reason"))[:160]
    devices = (res.get("data") or {}).get("devices") or []
    codes = {d.get("code") for d in devices if d.get("code")}
    _DEV_CACHE["key"] = cache_key
    _DEV_CACHE["codes"] = codes
    _DEV_CACHE["at"] = time.time()
    return codes, "02 台账 %d 个机位" % len(codes)


def reset_device_cache():
    _DEV_CACHE["key"] = None
    _DEV_CACHE["codes"] = None
    _DEV_CACHE["at"] = 0.0


def save_camera_device(code, name="", room_id="", site="", position="",
                       url="", fov_deg=90, pan_deg=0, tilt_deg=0, has_audio=False,
                       operator="10_room"):
    """把本模块的考场点位登记进 02 的机位台账（单向写入，不改 02 代码）。"""
    def _go():
        lk = get_linkage()
        return lk.p02.call("save_device", {
            "code": code, "name": name or code, "room_id": room_id, "site": site,
            "position": position, "url": url, "fov_deg": fov_deg,
            "pan_deg": pan_deg, "tilt_deg": tilt_deg, "has_audio": bool(has_audio),
            "operator": operator})
    return _safe(_go, "save_device", "02")


def status(deep=False):
    """各系统状态。deep=True 时真的逐个起子进程问（慢，但自检要它）。"""
    out = {"linkage_available": linkage_available()[1],
           "sandbox_root": sandbox_settings()[0] or "(真实数据目录)",
           "modules": {}}
    for key in ("01", "02", "03", "06"):
        ok, why = module_available(key)
        out["modules"][key] = {"available": ok, "reason": why}
    if not deep:
        return out
    for key in ("01", "02", "03", "06"):
        if not out["modules"][key]["available"]:
            continue
        def _mk(k):
            def _go():
                lk = get_linkage()
                proj = {"01": lk.p01, "02": lk.p02, "03": lk.p03,
                        "06": lk.p06}[k]
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
    return True, "00_集成联动可用（沙箱：%s）" % (sandbox_settings()[0] or "关闭")
