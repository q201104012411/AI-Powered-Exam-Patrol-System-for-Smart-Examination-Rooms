# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 跨模块联动业务

把"频谱发现"翻译成其它模块能接住的动作（**只调用，不改任何模块**）：

    07 异常  -> 03 智能终端：SIGNAL_ABNORMAL（严重=红 / 关注警告=橙），
                详情带频率、带宽、峰值、信噪比、站点、定位、时延与处置建议
    07 设备/合规问题 -> 03：OFFLINE（橙），提示现场与合规责任人
    06 安检检出无线耳机/手机 -> 07：拉取 06 待处置报警做**同考场关联**，
                提示"该考场曾有无线耳机入场，优先复核该区域频谱"
    02 机位画质 -> 07：定位到考场后查该考场机位在线/画质，作为人工复核参考
    07 开关记录/告警摘要 -> 03 局域网接入服务的远端记录（跨机部署时驾驶舱可见）

所有调用都是**子进程或 HTTP**（同机走 00 联动层），失败只返回原因，不影响 07 主流程。
"""
import threading
import traceback

import config
import linkage_client
import models


def _async(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def link_status():
    return {"client": linkage_client.status(),
            "note": "同机走 00 集成联动子进程；跨机配 terminal_url 走 03 接入服务；"
                    "失败不影响本模块运行"}


# =============================================================================
# 07 -> 03：异常 / 设备 / 合规
# =============================================================================
def report_anomaly(store, anomaly, sync=False, operator=None):
    """把一条频谱异常送到 03（严重=红、其余=橙）。返回 dict。"""
    if anomaly is None:
        return {"ok": False, "reason": "异常不存在"}
    operator = operator or config.SOURCE_TAG
    code = config.ALERT_CODE_BY_KIND.get(anomaly.kind, "SIGNAL_ABNORMAL")
    level = config.SEVERITY_LEVEL.get(anomaly.severity, "orange")
    detail = ("频谱异常（%s）：%s｜频率 %s｜带宽 %.3f MHz｜峰值 %.1f dBm｜"
              "信噪比 %.1f dB｜监测站 %s｜定位 %s｜发现时延 %.1fs"
              % (anomaly.severity_text(), anomaly.kind_text(),
                 anomaly.frequency_text(), float(anomaly.bandwidth_mhz),
                 float(anomaly.peak_dbm), float(anomaly.snr_db),
                 anomaly.station_code, anomaly.located_text(),
                 float(anomaly.latency_sec or 0)))
    if anomaly.room_id:
        detail += "｜考场 %s" % anomaly.room_id
    if anomaly.feature_name:
        detail += "｜特征库：%s" % anomaly.feature_name
    detail += "｜建议：现场复核并用手持侦测仪逼近；确认作弊后按预案处置并留存证据"

    def _work():
        r = linkage_client.raise_alert(
            code=code, device_code=anomaly.station_code, room_id=anomaly.room_id or "",
            seat_no="", detail=detail, level=level, operator=operator)
        try:
            if r.get("ok"):
                anomaly.alert_id = str(r.get("alert_id") or "queued")
                anomaly.reported_at = models.now_ts()
                store.save_anomaly(anomaly)
                store.save_correlation(models.Correlation(
                    source="03", ref_id=anomaly.alert_id,
                    anomaly_id=anomaly.anomaly_id, room_id=anomaly.room_id or "",
                    detail="上报 03：%s（%s）" % (r.get("alert_id"),
                                              r.get("level_name") or r.get("level")),
                    raw=r))
                store.log("上报智能终端", "异常 %s -> %s（%s）"
                          % (anomaly.anomaly_id, anomaly.alert_id,
                             r.get("level") or level), operator)
            else:
                store.log("上报智能终端失败", "异常 %s：%s"
                          % (anomaly.anomaly_id, r.get("reason")), operator)
        except Exception:                                     # noqa: BLE001
            traceback.print_exc()
        return r

    if sync:
        return _work()
    _async(_work)
    return {"ok": True, "queued": True, "alert_id": "", "level": level,
            "code": code, "detail": detail}


def report_device(store, jammer, reason, sync=False, operator=None, level=None):
    """设备离线/故障/自动关闭失败 -> 03 橙色提醒（现场与合规责任人都要看见）。"""
    operator = operator or config.SOURCE_TAG
    detail = ("屏蔽设备异常：%s（%s）%s｜考点 %s｜考场 %s｜"
              "请立即现场确认设备状态并人工处置（窗口结束后未关属合规事件）"
              % (jammer.code, jammer.name, reason or "", jammer.site or "-",
                 jammer.room_id or "-"))

    def _work():
        r = linkage_client.raise_alert(
            code=config.ALERT_CODE_TIMEOUT, device_code=jammer.code,
            room_id=jammer.room_id or "", detail=detail,
            level=level or "orange", operator=operator)
        try:
            store.log("上报设备异常", "%s：%s" % (jammer.code, r.get("reason") or "已上报"),
                      operator)
        except Exception:                                     # noqa: BLE001
            pass
        return r

    if sync:
        return _work()
    _async(_work)
    return {"ok": True, "queued": True, "code": config.ALERT_CODE_TIMEOUT}


def report_compliance(store, chk, sync=False, operator=None):
    """合规体检出现"不通过"时，把最严重几条送到 03（红色，强制确认）。"""
    operator = operator or config.SOURCE_TAG
    fails = [r for r in (chk.get("rows") or []) if r.get("level") == "fail"]
    if not fails:
        return {"ok": True, "sent": False, "reason": "合规体检无不通过项"}
    detail = "频谱合规红线不通过 %d 项：%s。请按《无线信号监测与屏蔽管控》红线立即整改" % (
        len(fails), "；".join(r["name"] + "：" + r["detail"] for r in fails[:5]))

    def _work():
        return linkage_client.raise_alert(
            code="SIGNAL_ABNORMAL", device_code=config.DEVICE_ID_PREFIX,
            detail=detail, level="red", operator=operator)
    r = _work() if sync else {"ok": True, "queued": True, "level": "red"}
    if not sync:
        _async(lambda: linkage_client.raise_alert(
            code="SIGNAL_ABNORMAL", device_code=config.DEVICE_ID_PREFIX,
            detail=detail, level="red", operator=operator))
    return {"ok": True, "sent": True, "fails": len(fails), "result": r}


def after_tick(store, tick_result, operator=None):
    """tick 之后的联动：自动关闭失败/自动开启失败 -> 上报 03。返回上报列表。"""
    operator = operator or config.SOURCE_TAG
    sent = []
    for act in (tick_result.get("actions") or []):
        if act.get("ok"):
            continue
        if act.get("action") not in ("auto_on", "auto_off", "auto_off_disabled",
                                     "auto_on_blocked"):
            continue
        jam = store.get_device(code=act.get("device_code"))
        if jam is None:
            continue
        r = report_device(store, jam, "自动%s失败：%s" % (
            "开启" if "on" in act.get("action", "") else "关闭",
            act.get("reason") or "-"), operator=operator)
        sent.append({"device_code": jam.code, "ok": bool(r.get("ok")),
                     "reason": act.get("reason")})
    return sent


# =============================================================================
# 06 -> 07：安检结论关联
# =============================================================================
WIRELESS_KINDS = ("earphone", "mobile", "implant")


def correlate_06(store, limit=50, operator=None):
    """
    拉取 06 的待处置安检报警，挑出"无线相关"的（耳机/手机）做关联留痕。

    这不是再判一次作弊，而是给频谱侧一个**关注方向**：某考场安检检出无线耳机，
    该考场及相邻区域的 2.4G/UHF 监测要优先复核（耳机没有接收机就传不了答案）。
    """
    operator = operator or config.SOURCE_TAG
    r = linkage_client.security_alarms(limit=limit, pending_only=True)
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("reason") or "06 不可用"}
    alarms = ((r.get("data") or {}).get("alarms") or [])
    existing = {c.ref_id for c in store.list_correlations(source="06", limit=500)}
    new, focus = [], {}
    for a in alarms:
        aid = str(a.get("alarm_id") or "")
        kinds = [str(x) for x in (a.get("kinds") or [])]
        if a.get("kind"):
            kinds.append(str(a.get("kind")))
        if not any(k in WIRELESS_KINDS for k in kinds):
            continue
        room = str(a.get("room_id") or "")
        if room:
            focus[room] = focus.get(room, 0) + 1
        if aid in existing:
            continue
        detail = ("06 安检检出无线相关器材（%s）：%s｜考场 %s｜设备 %s｜"
                  "建议优先复核该区域 2.4G/UHF 频谱"
                  % ("、".join(sorted(set(kinds))), a.get("detail") or "-",
                     room or "-", a.get("device_code") or "-"))
        store.save_correlation(models.Correlation(
            source="06", ref_id=aid, room_id=room, detail=detail,
            raw=a, created_epoch=models.now_epoch()))
        new.append({"alarm_id": aid, "room_id": room, "kinds": sorted(set(kinds)),
                    "detail": detail})
    if new:
        store.log("关联06安检结论", "新增 %d 条；关注考场 %s"
                  % (len(new), "、".join(focus) or "-"), operator)
    return {"ok": True, "pulled": len(alarms), "new": len(new),
            "focus_rooms": [{"room_id": k, "count": v} for k, v in focus.items()],
            "items": new,
            "note": "06 的报警已关联到本模块；未改变 06 的任何数据"}


# =============================================================================
# 02 -> 07：机位画质参考
# =============================================================================
def correlate_02(store, room_id="", device_code="", operator=None):
    """查 02 的机位台账/画质可信度，作为异常现场复核的参考（不参与分级）。"""
    operator = operator or config.SOURCE_TAG
    r = linkage_client.camera_devices()
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("reason") or "02 不可用"}
    devs = (r.get("data") or {}).get("devices") or []
    hit = None
    if device_code:
        hit = next((d for d in devs if d.get("code") == device_code), None)
    if hit is None and room_id:
        hit = next((d for d in devs if d.get("room_id") == room_id), None)
    if hit is None:
        return {"ok": False, "reason": "02 台账里没有匹配的机位（room_id=%s）"
                                       % (room_id or "-")}
    code = hit.get("code") or ""
    cred = linkage_client.camera_credibility(code)
    factor = (cred.get("data") or {}).get("factor") if cred.get("ok") else None
    detail = ("02 机位 %s（考场 %s）：%s；画质可信度 %s（仅作人工复核参考，"
              "不据此调整频谱告警级别）"
              % (code, hit.get("room_id") or "-", hit.get("name") or "-",
                 ("×%.2f" % factor) if factor is not None else "未知"))
    store.save_correlation(models.Correlation(
        source="02", ref_id=code, room_id=room_id or hit.get("room_id") or "",
        detail=detail, raw={"device": hit, "credibility": cred.get("data")
                            if cred.get("ok") else cred},
        created_epoch=models.now_epoch()))
    store.log("关联02机位", detail, operator)
    return {"ok": True, "device": hit, "factor": factor, "detail": detail}


# =============================================================================
# 07 -> 03 远端记录（跨机部署）
# =============================================================================
def switch_records(store, limit=50):
    rows = store.list_switches(limit=int(limit))
    out = []
    for s in rows:
        out.append({"record_id": s.log_id, "ts": s.ts, "action": s.action_text(),
                    "detail": "屏蔽%s %s｜功率 %.2fW｜%s｜%s"
                              % (s.action_text(), "、".join(
                                  config.band_name(b) for b in (s.bands or [])),
                                 float(s.power_w), s.source_text(),
                                 s.reason or "-"),
                    "operator": s.operator or "",
                    "raw": s.to_dict()})
    return out


def anomaly_records(store, limit=50):
    rows = store.list_anomalies(limit=int(limit))
    out = []
    for a in rows:
        out.append({"record_id": a.anomaly_id, "ts": a.created_at,
                    "action": a.kind_text(),
                    "detail": "%s｜%s｜峰值 %.1f dBm｜%s"
                              % (a.frequency_text(), a.severity_text(),
                                 float(a.peak_dbm), a.status_text()),
                    "operator": a.reviewer or "",
                    "raw": a.to_dict()})
    return out


def push_records(store, which="switch", limit=50):
    """把开关记录/异常摘要推到 03 接入服务（仅跨机模式；同机直接看 03 库）。"""
    if which == "anomaly":
        records = anomaly_records(store, limit)
        kind = "alert"
    else:
        records = switch_records(store, limit)
        kind = "oplog"
    return linkage_client.push_records(store, records, kind=kind)


def describe():
    st = linkage_client.status()
    return {"03 链路": st["mode"],
            "03 地址": st["terminal_url"],
            "联动模块": "、".join("%s:%s" % (k, "可用" if v["available"] else "不可用")
                                 for k, v in st["modules"].items()),
            "07 告警码": "SIGNAL_ABNORMAL（红/橙）、OFFLINE（设备异常）",
            "说明": "只调用 00/02/03/06 的接口，不修改任何其它模块"}
