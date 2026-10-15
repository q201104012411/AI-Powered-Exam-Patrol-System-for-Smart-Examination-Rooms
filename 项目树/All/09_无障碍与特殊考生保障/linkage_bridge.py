# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 联动桥（09 <-> 00 <-> 01/02/03/06）

**不改别人的文件**是本模块的硬约束，所以联动这样实现：
  · 复用 00_集成联动 的 `linkage.py`（子进程 + JSON 总线），**只 import，不改一行**；
  · 00 的 PROJECTS 字典里没有 "09"（那是 00 的文件，我们不碰），
    本模块在**运行内存里**把自己注册进去（`PROJECTS["09"]=本目录`），
    于是 09 也能被同一条总线调用 —— 协议完全一致，自检里会真的跑一次"经总线调自己"；
  · 对外通知一律走 03 智能终端（告警分级、处置留痕、审计链都在 03 落地）；
  · 机位画质可信度问 02，人脸核验问 01；06 提供人工安检通道的口径（复用同一个告警码）；
  · 03 不可达时事件进 `outbox`（本地库），恢复后 `flush_outbox()` 补发，**事件不丢**。

设计取舍（写下来免得后人误解）：
  · 09 推给 03 的告警码在 03 的分级表里没有（表在 03 的 config.py 里，我们不改它），
    所以本模块**显式带 level**（03 的 ingest_alert 支持 level 参数，这是既有能力）；
    万一显式带级别不可用，就退回 03 已注册的等价码（ALERT_CODES[*]["fallback"]）。
  · 本模块的告警都是**人工现场确认**后发出的（考生就站在通道口），
    所以默认不参与 02 的"画质可信度降权"（避免真事故被当成画质误报压成橙色），
    但会把当时的画质可信度写进告警详情里。自动触发型通知（human_confirmed=False）才降权。
"""
import os
import sys

import config

BASE = os.path.dirname(os.path.abspath(__file__))
ALL_DIR = os.path.dirname(BASE)
LINKAGE_DIR = os.path.join(ALL_DIR, "00_集成联动")
if LINKAGE_DIR not in sys.path:
    sys.path.insert(0, LINKAGE_DIR)

_lk_cache = {}
_registered = False
# 沙箱开关：自检/联调时把 02/03/06 的数据目录重定向到临时目录，避免污染它们的真实数据
_SANDBOX = {"root": None, "keys": ("02", "03", "06")}


def set_sandbox(root=None, keys=("02", "03", "06")):
    """给 02/03/06 设置数据目录沙箱（root=None 表示用它们的真实目录）。"""
    _SANDBOX["root"] = root
    _SANDBOX["keys"] = tuple(keys or ("02", "03", "06"))
    _lk_cache.clear()
    return _SANDBOX


def bus_available():
    """00_集成联动 是否就位（只看文件，不起子进程）。"""
    if not os.path.isdir(LINKAGE_DIR):
        return False, "目录不存在：%s" % LINKAGE_DIR
    if not os.path.isfile(os.path.join(LINKAGE_DIR, "linkage.py")):
        return False, "缺少 linkage.py"
    return True, "可用"


def register_self():
    """
    在内存里把 09 注册进 00 的总线（不改 00 的任何文件）。
    这样 09 既是调用方，也是被调用方 —— 其它专题可以按同一协议调它。
    """
    global _registered
    ok, why = bus_available()
    if not ok:
        return False, why
    import linkage
    linkage.PROJECTS.setdefault("09", BASE)
    linkage.Project.HOME_ENV.setdefault("09", "EXAM_ACCESS_HOME")
    _registered = True
    return True, "已注册到 00_集成联动 总线（内存注册，未修改 00 的文件）"


def _linkage(sandbox_root=None, sandbox_keys=("02", "03", "06")):
    """
    取联动总线句柄。sandbox_root 给定时，02/03/06 的数据目录重定向到临时目录
    （自检/联调用，避免污染它们的真实数据）。
    """
    ok, why = register_self()
    if not ok:
        raise RuntimeError("联动不可用：%s" % why)
    if sandbox_root is None and _SANDBOX["root"]:
        sandbox_root, sandbox_keys = _SANDBOX["root"], _SANDBOX["keys"]
    key = (sandbox_root, tuple(sandbox_keys))
    lk = _lk_cache.get(key)
    if lk is None:
        import linkage
        if sandbox_root:
            lk = linkage.Linkage(sandbox_root=sandbox_root,
                                 sandbox_keys=tuple(sandbox_keys))
        else:
            lk = linkage.Linkage()
        _lk_cache[key] = lk
    return lk


def project(key):
    """取某个专题的调用句柄（用 00 的 Project，协议一致）。"""
    lk = _linkage()
    return getattr(lk, "p%s" % key, None) or _project_by_key(lk, key)


def _project_by_key(lk, key):
    import linkage
    if key not in linkage.PROJECTS:
        raise RuntimeError("未知专题：%s" % key)
    return linkage.Project(key, env_extra={})


def availability():
    """各专题是否具备被调用的条件（只看文件，快速，GUI 用）。"""
    ok, why = bus_available()
    out = {"bus": {"ok": ok, "reason": why}}
    if not ok:
        return out
    try:
        import linkage
        register_self()
        for key, name in (("01", "人脸识别与身份核对"),
                          ("02", "摄像头核查与视频质量诊断"),
                          ("03", "智能终端操作"),
                          ("06", "智能安检与手机作弊器材检测"),
                          ("09", "无障碍与特殊考生保障（本模块）")):
            if key == "09":
                p = linkage.Project("09")
            else:
                p = linkage.Project(key)
            p_ok, p_why = p.available()
            out[key] = {"ok": p_ok, "reason": p_why, "name": name,
                        "dir": p.dir}
    except Exception as e:                                     # noqa: BLE001
        out["bus"] = {"ok": False, "reason": "%s: %s" % (type(e).__name__,
                                                         str(e)[:150])}
    return out


def status_all(timeout=None):
    """真起子进程问各套 status（慢，点了"联动探测"才做）。"""
    lk = _linkage()
    out = {}
    for key in ("01", "02", "03", "06"):
        p = getattr(lk, "p%s" % key, None)
        if p is None:
            out[key] = {"ok": False, "reason": "00 总线未提供该专题句柄"}
            continue
        p_ok, p_why = p.available()
        if not p_ok:
            out[key] = {"ok": False, "reason": p_why}
            continue
        try:
            out[key] = p.call("status", {})
        except Exception as e:                                 # noqa: BLE001
            out[key] = {"ok": False, "reason": "%s: %s" % (type(e).__name__,
                                                           str(e)[:200])}
    out["09"] = self_status()
    return out


def self_status():
    """经 00 的总线调用**本模块自己**（证明 09 说的是同一种协议）。"""
    try:
        import linkage
        register_self()
        p = linkage.Project("09")
        p_ok, p_why = p.available()
        if not p_ok:
            return {"ok": False, "reason": p_why}
        return p.call("status", {})
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200])}


# ---------------- 02：机位画质可信度 ----------------
def camera_credibility(device_code):
    """问 02 这个机位的告警可信度系数；02 不可达就如实返回 1.0 + 原因。"""
    if not device_code:
        return 1.0, "未指定机位（不需要画质上下文）", {}
    try:
        lk = _linkage()
        factor, why, info = lk.camera_credibility(device_code)
        return factor, why, info
    except Exception as e:                                     # noqa: BLE001
        return 1.0, "02 不可达：%s" % str(e)[:120], {}


# ---------------- 01：人脸核验 ----------------
def face_verify(image, candidate="", mode="1:1", device_code="", operator="09_access"):
    """
    调 01 做人脸核验（并带上 02 的机位画质上下文）。
    **不管 01 成功还是失败，本函数都不抛异常** —— 01 不可用时返回 ok=False + reason，
    调用方据此走人工核验（这正是本专题存在的意义）。
    """
    try:
        lk = _linkage()
        r = lk.face_verify(image, candidate=candidate, mode=mode,
                           device_code=device_code, operator=operator)
        return {"ok": True, "source": "01_人脸识别与身份核对", "result": r}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": "01 人脸核验不可用：%s" % str(e)[:200],
                "result": {}}


def candidate_roster():
    """01 已建档考生清单（用于界面提示"这个考生能不能刷脸"）。"""
    try:
        lk = _linkage()
        st = lk.p01.call("status", {})
        return {"ok": True, "candidates": st.get("candidates") or [],
                "index_ready": st.get("index_ready")}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": str(e)[:150], "candidates": []}


# ---------------- 03：告警推送与处置留痕 ----------------
def notify(code, info, store=None, human_confirmed=True, operator="09_access"):
    """
    把 09 的事件推给 03 智能终端。
    成功 -> {"ok": True, "alert_id":..., "level":..., "downgraded":...}
    失败 -> 写 outbox（有 store 时）+ {"ok": False, "queued": True, "reason":...}
    """
    info = dict(info or {})
    meta = config.ALERT_CODES.get(code, {})
    level = info.get("level") or meta.get("level") or config.LEVEL_ORANGE
    device_code = str(info.get("device_code") or info.get("device_id") or "")
    fallback = meta.get("fallback") or "SECURITY_SPECIAL_CHANNEL"

    factor, why, _cinfo = (1.0, "", {})
    if device_code:
        factor, why, _cinfo = camera_credibility(device_code)
    credibility = 1.0 if human_confirmed else float(factor)
    detail = str(info.get("detail") or "")
    if device_code and factor < 1.0:
        detail += "；来源机位 %s 画质可信度 ×%.1f（%s）%s" % (
            device_code, factor, why,
            "；本告警为现场人工确认，不因画质降级" if human_confirmed else "")
    payload = {
        "code": code, "level": level, "room_id": info.get("room_id") or "",
        "seat_no": str(info.get("seat_no") or ""),
        "device_id": device_code, "detail": detail,
        "candidate_id": info.get("candidate_id") or "",
        "candidate_name": info.get("candidate_name") or "",
        "credibility": credibility, "operator": operator,
        "source": "linkage/09",
    }

    err = ""
    try:
        lk = _linkage()
        p03 = getattr(lk, "p03", None)
        ok, why3 = (p03.available() if p03 is not None else (False, "00 总线无 03 句柄"))
        if ok:
            r = p03.call("ingest_alert", payload)
            return {"ok": True, "alert_id": r.get("alert_id") or "",
                    "level": r.get("level") or level, "code": code,
                    "downgraded": bool(r.get("downgraded")),
                    "credibility": r.get("credibility"), "force_confirm":
                    r.get("force_confirm"), "reason": why3,
                    "path": "00总线->03.ingest_alert(显式级别)",
                    "camera_credibility": factor}
        err = why3
    except Exception as e:                                     # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, str(e)[:150])

    # 退路 1：用 03 已注册的等价码走 00 的 raise_alert
    try:
        lk = _linkage()
        r = lk.raise_alert(code=fallback, device_code=device_code or "ACCESS-09",
                           room_id=info.get("room_id") or "",
                           seat_no=str(info.get("seat_no") or ""),
                           detail=("[%s] %s" % (meta.get("text") or code, detail)),
                           candidate_id=info.get("candidate_id") or "",
                           candidate_name=info.get("candidate_name") or "",
                           operator=operator)
        return {"ok": True, "alert_id": r.get("alert_id") or "",
                "level": r.get("level") or "", "code": fallback,
                "downgraded": bool(r.get("downgraded")),
                "credibility": r.get("credibility"),
                "path": "00总线->03.raise_alert(等价码兜底)", "fallback": True,
                "camera_credibility": factor}
    except Exception as e:                                     # noqa: BLE001
        err = "%s ｜ 兜底也失败：%s" % (err, str(e)[:150])

    # 退路 2：进本地 outbox（事件不丢，恢复后补发）
    queued = False
    if store is not None:
        try:
            store.outbox_add(code, level, {"payload": payload, "fallback": fallback})
            queued = True
        except Exception:                                      # noqa: BLE001
            queued = False
    return {"ok": False, "queued": queued, "reason": err or "03 不可达",
            "code": code, "level": level}


def flush_outbox(store, limit=20, operator="09_access"):
    """补发 outbox 里的事件（03 恢复后调用；界面/自检都会用）。"""
    sent, failed = [], []
    for it in store.list_outbox(pending_only=True, limit=limit):
        p = (it.payload or {}).get("payload") or {}
        code = p.get("code") or it.code
        # 补发时不再套 outbox（避免自己给自己加队列）
        r = notify(code, {
            "detail": p.get("detail") or "", "room_id": p.get("room_id") or "",
            "seat_no": p.get("seat_no") or "", "device_id": p.get("device_id") or "",
            "candidate_id": p.get("candidate_id") or "",
            "candidate_name": p.get("candidate_name") or "",
            "level": p.get("level") or it.level,
        }, store=None, human_confirmed=True, operator=operator)
        if r.get("ok"):
            store.mark_outbox(it.msg_id, sent=True, alert_id=r.get("alert_id") or "")
            sent.append(it.msg_id)
        else:
            store.mark_outbox(it.msg_id, sent=False, last_error=r.get("reason") or "")
            failed.append(it.msg_id)
    return {"sent": sent, "failed": failed,
            "stats": store.outbox_stats()}


def list_alerts(limit=20):
    """查 03 的告警（用于确认 09 推的事件真的进了终端）。"""
    try:
        lk = _linkage()
        r = lk.alerts(limit=limit)
        return {"ok": True, "alerts": r.get("alerts") or []}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": str(e)[:150], "alerts": []}


def close_alert(alert_id, conclusion="confirmed", reason="09 现场处置完成",
                operator="09_access"):
    """
    把 09 推的告警在 03 里复核结案（处置闭环留痕在 03）。

    注意：03 对**红色预警**要求"先人工确认、再填复核结论"（防止一键消警），
    确认动作只在 03 的终端界面做，不在集成接口里 —— 所以红色告警本函数会返回
    `needs_manual_confirm=True`，这不是失败，而是 09 **不绕过** 03 的强制确认。
    橙色告警（本模块的常态，如"启用替代核验"）可直接复核结案。
    """
    try:
        lk = _linkage()
        r = lk.close_alert(alert_id, conclusion=conclusion, reason=reason,
                           operator=operator)
        msg = r.get("message") or ""
        need_confirm = ("必须先确认" in msg) or ("强制确认" in msg)
        return {"ok": bool(r.get("ok")), "message": msg, "raw": r,
                "needs_manual_confirm": need_confirm,
                "note": ("03 要求红色预警先由终端人工确认，再复核留痕；"
                         "09 不绕过该强制确认" if need_confirm else "")}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": str(e)[:150], "needs_manual_confirm": False}


# ---------------- 06：人工安检通道口径 ----------------
def security_status():
    try:
        lk = _linkage()
        return {"ok": True, "status": lk.security_status()}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": str(e)[:150]}


def security_alarms(limit=20, pending_only=True):
    try:
        lk = _linkage()
        return {"ok": True, "alarms": lk.security_alarms(limit=limit,
                                                         pending_only=pending_only)}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": str(e)[:150], "alarms": []}


def special_channel_alerts(limit=50):
    """
    从 03 的告警里找 06 上报的**特殊考生人工安检通道**记录
    （06 -> 00 -> 03 的既有链路，09 只读它，用来和本模块的档案对账）。
    """
    r = list_alerts(limit=limit)
    if not r.get("ok"):
        return r
    hit = [a for a in r["alerts"]
           if a.get("code") == config.ALERT_CODE_SPECIAL_SECURITY]
    return {"ok": True, "alerts": hit, "total": len(r["alerts"])}


# ---------------- 全链路探测 ----------------
def probe(store=None, device_code="", candidate="", write=False, operator="09_access"):
    """
    一次性把联动探到底（界面"联动探测"按钮 / 自检 / 联调脚本共用）：
      00 总线 -> 各专题可达性 -> 09 经总线调自己 -> 02 画质 -> 03 推送（可选）-> 06 概览
    write=False 时**不往 03 写数据**（只读探测），避免污染真实终端库。
    """
    out = {"at": _now_ts(), "steps": [], "ok": True}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:220]})
        if not ok:
            out["ok"] = False
        return bool(ok)

    bus_ok, bus_why = bus_available()
    step("00_集成联动 总线可用", bus_ok, bus_why)
    if not bus_ok:
        return out
    reg_ok, reg_why = register_self()
    step("09 已在总线注册（内存注册，不改 00 文件）", reg_ok, reg_why)

    for key, name in (("01", "人脸识别与身份核对"), ("02", "摄像头核查与视频质量诊断"),
                      ("03", "智能终端操作"), ("06", "智能安检与手机作弊器材检测")):
        ok, why = _project_available(key)
        step("%s %s 可调用" % (key, name), ok, why)

    self_r = self_status()
    step("09 经 00 总线调用自己（协议一致）", bool(self_r.get("ok")),
         self_r.get("system") or self_r.get("reason"))

    if device_code:
        f, why, _ = camera_credibility(device_code)
        step("02 给出机位 %s 的画质可信度" % device_code, "不可达" not in why,
             "×%.1f %s" % (f, why))
    if candidate:
        r = candidate_roster()
        step("01 已建档考生可查询", bool(r.get("ok")),
             "考生 %d 人；特征索引 %s" % (len(r.get("candidates") or []),
                                      "就绪" if r.get("index_ready") else "未建"))

    if write:
        r = notify("ACCESS_ASSIST_NEEDED", {
            "detail": "联动探测：09 无障碍保障链路自检（可忽略，非真实事件）",
            "room_id": "SELFTEST", "seat_no": "", "level": config.LEVEL_ORANGE,
            "device_id": device_code or "", "operator": operator}, store=store,
            human_confirmed=True, operator=operator)
        step("09 -> 00 -> 03 告警推送", bool(r.get("ok")),
             r.get("alert_id") or r.get("reason"))
        if r.get("ok"):
            al = list_alerts(limit=30)
            hit = [a for a in (al.get("alerts") or [])
                   if a.get("alert_id") == r.get("alert_id")]
            step("03 里能查到该告警", bool(hit),
                 "共 %d 条告警" % len(al.get("alerts") or []))
            if hit:
                c = close_alert(r.get("alert_id"), "confirmed",
                                "09 联动探测结案：非真实事件", operator)
                step("09 -> 00 -> 03 复核结案留痕", bool(c.get("ok")),
                     c.get("message") or c.get("reason"))

    s6 = security_status()
    step("06 智能安检可查（人工安检通道对账）", bool(s6.get("ok")),
         ("过检 %s 条，待处置 %s" % (s6.get("status", {}).get("passes"),
                                  s6.get("status", {}).get("pending_alarms")))
         if s6.get("ok") else s6.get("reason"))

    sc = special_channel_alerts(limit=50)
    step("可对账 06 上报的特殊考生人工安检通道记录", bool(sc.get("ok")),
         "命中 %s 条" % sc.get("total", 0) if sc.get("ok") else sc.get("reason"))
    out["outbox"] = store.outbox_stats() if store is not None else {}
    return out


def _now_ts():
    """不 import models 也能给出时间戳（本模块要能被极早调用）。"""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _project_available(key):
    try:
        import linkage
        register_self()
        p = linkage.Project(key) if key != "09" else linkage.Project("09")
        return p.available()
    except Exception as e:                                     # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:120])


def link_summary():
    """给界面/自检的一行摘要（不发子进程）。"""
    av = availability()
    if not av.get("bus", {}).get("ok"):
        return {"ok": False, "text": "00_集成联动 不可用：%s"
                % av.get("bus", {}).get("reason")}
    ok_keys = [k for k, v in av.items() if k != "bus" and v.get("ok")]
    bad = {k: v.get("reason") for k, v in av.items()
           if k != "bus" and not v.get("ok")}
    return {"ok": not bad, "text": "可调用：%s%s" % (
        "、".join(sorted(ok_keys)),
        ("；不可用：%s" % "；".join("%s(%s)" % (k, str(v)[:60])
                                 for k, v in bad.items())) if bad else ""),
        "available": av}


def main(argv=None):
    """命令行联调：python linkage_bridge.py --probe [--write] [--device CAM-1]"""
    import argparse
    import json
    ap = argparse.ArgumentParser(description="09 联动联调（只读探测，默认不写数据）")
    ap.add_argument("--probe", action="store_true", help="跑一次全链路探测")
    ap.add_argument("--write", action="store_true", help="允许往 03 写测试告警")
    ap.add_argument("--device", default="", help="顺带查该机位的画质可信度")
    ap.add_argument("--candidate", default="", help="顺带查 01 的建档考生")
    ap.add_argument("--real", action="store_true", help="用各专题真实数据目录")
    ns = ap.parse_args(argv)
    if not ns.probe:
        print(json.dumps({"available": availability(), "summary": link_summary()},
                         ensure_ascii=False, indent=2))
        return 0
    r = probe(device_code=ns.device, candidate=ns.candidate, write=ns.write)
    for s in r["steps"]:
        print("  %s %s — %s" % ("PASS" if s["ok"] else "FAIL", s["name"], s["detail"]))
    print("联动探测：%s" % ("通过" if r["ok"] else "有不可用环节"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
