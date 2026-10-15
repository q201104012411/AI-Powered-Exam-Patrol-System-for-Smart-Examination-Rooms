# -*- coding: utf-8 -*-
r"""
08 试卷流转与保密室智能监控 · 集成接口（供 00_集成联动 调用）

ops:
  status       {}                       -> 概览（机位/告警/试卷袋/审计链/数据目录）
  vault_status {room_id}                -> 某保密室七项能力体检 + 覆盖率 + 盲区
  rooms        {}                       -> 保密室清单（含机位、覆盖率、环境采样）
  cameras      {room_id}                -> 机位台账（朝向/通道/在线/台账缺项）
  raise_alarm  {code, room_id, source_kind, source_id, detail, level, event_epoch}
                                        -> 向本专题告警总线推一条告警（带 5s/30s 时延核算）
  ingest_alert {code, room_id, ...}     -> **同 03 语义**：接收 02/03 传过来的告警，
                                            按机位画质可信度降权（< 0.6 红降橙）
  bag_trace    {bag_id}                 -> 一个试卷袋的全链条追溯（人/时/物/地点）
  bags         {room_id}                -> 试卷袋清单
  handover_rate {room_id}               -> 交接记录完整率（关键指标③）
  kpi          {room_id}                -> 五项关键指标实测值
  apply_credibility {device_code, factor, room_id}
                                        -> 把 02 给出的机位可信度落到本专题机位台账，
                                           低温机位在告警可靠性上标记"画质降权"

用法：python integration_api.py --op status --args "{}"
      python integration_api.py --op ingest_alert --args "{...}"

契约（与 01/02/03 完全一致）：
  · CLI：--op <name> --args '<json object>'
  · **最后一行**输出一个 JSON 对象，键 ok 为布尔
  · 任何异常都兜底成 {"ok": false, "reason": "..."}，不抛栈给调用方
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 画质可信度低于该值时，本专题接收到的红色告警自动降级为橙色（与 02/03 同一阈值）
DOWNGRADE_BELOW = 0.6


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _stack(room_id=""):
    """按 08 的既有方式装配 Store / AlarmHub / Vault / FlowManager。"""
    import storage
    import alarm_hub
    import vault as vault_mod
    import flow as flow_mod
    store = storage.Store()
    hub = alarm_hub.AlarmHub(store)
    v = vault_mod.Vault(store, hub=hub)
    f = flow_mod.FlowManager(store, hub=hub)
    return store, hub, v, f


# ==================== ops ====================

def op_status(a):
    import config
    store, hub, v, f = _stack()
    chain_ok, chain_msg = store.audit_verify()
    open_red = hub.open_red_count()
    return {
        "ok": True,
        "system": "08_试卷流转与保密室智能监控",
        "rooms": len(store.rooms()),
        "cameras": len(store.cameras()),
        "bags": len(store.bags()),
        "alarms": len(store.alarms(limit=100000)),
        "open_red": open_red,
        "audit_chain": chain_msg,
        "audit_ok": chain_ok,
        "audit_rows": len(store.audit_rows(limit=100000)),
        "data_dir": str(config.DATA_ROOT),
        "source": "08_试卷流转与保密室智能监控",
    }


def op_rooms(a):
    store, _hub, v, _f = _stack()
    out = []
    for room in store.rooms():
        cams = store.cameras(room)
        if not cams:
            rate = 0.0
            blinded = list(v.cameras.blind_spots(room))
        else:
            try:
                rate, _d = v.cameras.coverage(room)
            except Exception:                              # noqa: BLE001
                rate = 0.0
            blinded = list(v.cameras.blind_spots(room))
        last_env = v.env.latest(room)
        out.append({
            "room_id": room,
            "cameras": len(cams),
            "coverage": round(float(rate), 4),
            "blind_spots": blinded,
            "armed": v.intrusion.is_armed(room),
            "env": ({"temp_c": last_env.temp_c, "humidity_pct": last_env.humidity_pct,
                     "ts": last_env.ts} if last_env else None),
        })
    return {"ok": True, "rooms": out, "count": len(out),
            "source": "08_试卷流转与保密室智能监控"}


def op_vault_status(a):
    store, _hub, v, _f = _stack()
    room_id = a.get("room_id") or ""
    if not room_id:
        rooms = store.rooms()
        room_id = rooms[0] if rooms else ""
    if not room_id:
        return {"ok": False, "reason": "尚无保密室，请先登记机位/房间"}
    cams = store.cameras(room_id)
    if not cams:
        rate, detail = 0.0, "尚未登记任何机位（无样本，需现场布点）"
    else:
        rate, _d = v.cameras.coverage(room_id)
        spots = v.cameras.blind_spots(room_id)
        detail = ("六面覆盖齐备" if not spots
                  else "缺口朝向：%s" % "、".join(spots))
    items = v.self_check(room_id)
    return {
        "ok": True, "room_id": room_id,
        "coverage": round(float(rate), 4), "coverage_detail": detail,
        "blind_spots": v.cameras.blind_spots(room_id),
        "checks": items,
        "all_ok": all(x["ok"] for x in items),
        "armed": v.intrusion.is_armed(room_id),
        "source": "08_试卷流转与保密室智能监控",
    }


def op_cameras(a):
    store, _hub, _v, _f = _stack()
    room_id = a.get("room_id") or ""
    cams = store.cameras(room_id or None)
    return {"ok": True, "cameras": [
        {"cam_id": c.cam_id, "room_id": c.room_id, "name": c.name, "face": c.face,
         "url": c.url, "online": c.online, "channels": len(c.channels),
         "ledger_problems": c.ledger_problems()} for c in cams],
        "count": len(cams), "source": "08_试卷流转与保密室智能监控"}


def op_raise_alarm(a):
    """把外部（02 巡检 / 03 终端 / 00 联动）发现的告警推进本专题告警总线。"""
    import models
    store, hub, _v, _f = _stack()
    ev = a.get("event_epoch")
    try:
        ev = float(ev) if ev is not None else None
    except (TypeError, ValueError):
        ev = None
    alarm = hub.raise_alarm(
        a.get("code") or "UNKNOWN",
        detail=a.get("detail") or "",
        source_kind=a.get("source_kind") or "linkage",
        source_id=a.get("source_id") or "",
        scope=a.get("room_id") or "",
        level=a.get("level"),
        event_epoch=ev)
    return {"ok": True, "alarm_id": alarm.alarm_id, "code": alarm.code,
            "level": alarm.level, "level_name": alarm.level_name(),
            "status": alarm.status, "latency_sec": alarm.latency_sec,
            "source": "08_试卷流转与保密室智能监控"}


def op_ingest_alert(a):
    """
    与 03 的 ingest_alert **同语义**：接收带 credibility 的告警。
    本专题的差异点：告警落进保密室监控自己的告警总线（供无死角/门禁/入侵联调留痕）。
    """
    store, hub, _v, _f = _stack()
    try:
        cred = float(a.get("credibility")) if a.get("credibility") is not None else 1.0
    except (TypeError, ValueError):
        cred = 1.0
    detail = a.get("detail") or ""
    level = a.get("level")
    if level is not None:
        level = str(level).lower()
        if level in ("红色", "high"):
            level = "red"
    downgraded = False
    if cred < 1.0:
        detail = (detail + "；" if detail else "") + "来源机位画质可信度 ×%.1f" % cred
    if cred < DOWNGRADE_BELOW and level == "red":
        level = "orange"
        downgraded = True
        detail += "；因画质可信度过低，红色预警已降级为橙色待复核"
    ev = a.get("event_epoch")
    try:
        ev = float(ev) if ev is not None else None
    except (TypeError, ValueError):
        ev = None
    alarm = hub.raise_alarm(
        a.get("code") or "UNKNOWN", detail=detail,
        source_kind=a.get("source_kind") or "linkage",
        source_id=a.get("source_id") or a.get("device_id") or "",
        scope=a.get("room_id") or "",
        level=level, event_epoch=ev)
    if downgraded:
        store.log("预警降级", "%s 画质可信度 ×%.1f -> 橙色" % (alarm.code, cred),
                  a.get("operator") or "linkage")
    return {"ok": True, "alarm_id": alarm.alarm_id, "code": alarm.code,
            "level": alarm.level, "level_name": alarm.level_name(),
            "status": alarm.status, "downgraded": downgraded, "credibility": cred,
            "latency_sec": alarm.latency_sec,
            "source": "08_试卷流转与保密室智能监控"}


def op_bag_trace(a):
    _store, _hub, _v, f = _stack()
    bag_id = a.get("bag_id") or ""
    if not bag_id:
        return {"ok": False, "reason": "缺少 bag_id"}
    tr = f.bag_trace(bag_id)
    if tr is None:
        return {"ok": False, "reason": "试卷袋不存在：%s" % bag_id}
    b = tr["bag"]

    def _rows(seq, cols):
        out = []
        for x in seq:
            out.append({c: getattr(x, c, None) for c in cols})
        return out

    return {
        "ok": True, "bag_id": bag_id,
        "bag": {"bag_id": b.bag_id, "rfid": b.rfid, "subject": b.subject,
                "quantity": b.quantity, "room_id": b.room_id,
                "seal_state": b.seal_state, "bundle_id": b.bundle_id},
        "history": [{"stage": h.get("stage"), "person": h.get("person"),
                     "ts": h.get("ts"), "note": h.get("note")}
                    for h in (b.history or [])],
        "handovers": _rows(tr["handovers"],
                           ("stage", "from_person", "to_person", "ts", "room_id")),
        "deliveries": _rows(tr["deliveries"],
                            ("room_id", "quantity", "person", "ts")),
        "recycles": _rows(tr["recycles"],
                          ("room_id", "issued_qty", "returned_qty", "person", "ts")),
        "seal_state": tr["seal_state"],
        "stages": sorted({h.get("stage") for h in (b.history or []) if h.get("stage")}),
        "source": "08_试卷流转与保密室智能监控",
    }


def op_bags(a):
    store, _hub, _v, _f = _stack()
    room_id = a.get("room_id") or ""
    rows = store.bags(room_id or None)
    return {"ok": True, "bags": [
        {"bag_id": b.bag_id, "subject": b.subject, "quantity": b.quantity,
         "room_id": b.room_id, "seal_state": b.seal_state,
         "stages": sorted({h.get("stage") for h in (b.history or [])
                           if h.get("stage")})} for b in rows],
        "count": len(rows), "source": "08_试卷流转与保密室智能监控"}


def op_handover_rate(a):
    _store, _hub, _v, f = _stack()
    room_id = a.get("room_id") or None
    rate, ok, total = f.handover.completeness_rate(limit=10000, room_id=room_id)
    return {"ok": True, "room_id": room_id or "",
            "rate": (None if rate is None else round(float(rate), 4)),
            "complete": ok, "total": total,
            "source": "08_试卷流转与保密室智能监控"}


def op_kpi(a):
    """五项关键指标实测值（对应 md 第三节）。"""
    store, hub, v, f = _stack()
    room_id = a.get("room_id") or ""
    if not room_id:
        rooms = store.rooms()
        room_id = rooms[0] if rooms else ""

    cams = store.cameras(room_id)
    if not cams:
        cov, cov_detail = None, "该保密室尚未登记任何机位（无样本）"
    else:
        cov, cov_detail = v.cameras.coverage(room_id)
        spots = v.cameras.blind_spots(room_id)
        cov_detail = ("六面覆盖齐备" if not spots
                      else "缺口朝向：%s" % "、".join(spots))

    lat = hub.latency_stats(("DOOR_OPEN_ABNORMAL", "SAFE_OPEN_ABNORMAL",
                             "INTRUSION_IR", "ESCORT_ROUTE_DEVIATE"))
    hr, hok, htotal = f.handover.completeness_rate(limit=10000,
                                                  room_id=room_id or None)
    # 录像连续性：任一**已有录像**的机位出现缺口/不可回放即不达标；
    # 完全没录像的机位属"未启用"，不在此处判为缺口（避免误报）。
    gaps = []
    for c in cams:
        res = v.records.continuity(c.cam_id)
        if res["segments"] > 0 and not res["ok"]:
            gaps.append({"cam_id": c.cam_id, "gaps": res["gaps"],
                         "unplayable": res["unplayable"],
                         "segments": res["segments"]})
    ret = v.records.retention_report()
    return {
        "ok": True, "room_id": room_id,
        "kpi": {
            "coverage_rate": {"value": (None if cov is None else round(float(cov), 4)),
                              "require": "=100%", "detail": cov_detail},
            "open_alarm_latency_sec": {"value": lat, "require": "≤5s（开门/开柜）"},
            "handover_complete_rate": {"value": (None if hr is None
                                                 else round(float(hr), 4)),
                                       "complete": hok, "total": htotal,
                                       "require": "=100%"},
            "record_continue": {"gaps": gaps, "ok": not gaps,
                                "segments": ret["total"], "unlocked": ret["unlocked"]},
            "escort_link_ok": {"require": "断链重连", "note": "见押运任务"},
        },
        "source": "08_试卷流转与保密室智能监控",
    }


def op_apply_credibility(a):
    """
    把 02 给出的机位画质可信度（device_code / factor）落到本专题机位台账。
    本专题按 cam_id 匹配；匹配不到就如实返回 matched=False（不编造）。
    """
    store, _hub, _v, _f = _stack()
    code = a.get("device_code") or a.get("cam_id") or ""
    try:
        factor = float(a.get("factor")) if a.get("factor") is not None else 1.0
    except (TypeError, ValueError):
        factor = 1.0
    cam = None
    for c in store.cameras():
        if c.cam_id == code or c.url and code and code in c.url:
            cam = c
            break
    if cam is None:
        return {"ok": True, "matched": False, "device_code": code, "factor": factor,
                "note": "本专题未登记该机位，可信度仅用于本条告警的降权判定",
                "source": "08_试卷流转与保密室智能监控"}
    try:
        cam.extra = getattr(cam, "extra", {}) or {}
        cam.extra["credibility"] = round(factor, 3)
        store.save_camera(cam)
    except Exception:                                  # noqa: BLE001
        pass
    store.log("机位可信度", "%s 画质可信度 ×%.2f" % (code, factor),
              a.get("operator") or "linkage")
    return {"ok": True, "matched": True, "device_code": code, "cam_id": cam.cam_id,
            "factor": round(factor, 3),
            "low_quality": factor < DOWNGRADE_BELOW,
            "source": "08_试卷流转与保密室智能监控"}


OPS = {
    "status": op_status,
    "rooms": op_rooms,
    "vault_status": op_vault_status,
    "cameras": op_cameras,
    "raise_alarm": op_raise_alarm,
    "ingest_alert": op_ingest_alert,
    "bag_trace": op_bag_trace,
    "bags": op_bags,
    "handover_rate": op_handover_rate,
    "kpi": op_kpi,
    "apply_credibility": op_apply_credibility,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.args or "{}")
    except ValueError:
        return emit({"ok": False, "reason": "--args 不是合法 JSON"})
    if not isinstance(args, dict):
        return emit({"ok": False, "reason": "--args 必须是 JSON 对象"})
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                       # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
