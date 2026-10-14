# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 集成接口（供 00_集成联动 / 脚本 / 自检调用）

ops:
  status       {}                                  -> 概览（设备/过检/报警/保管/指标）
  devices      {}                                  -> 设备清单
  ingest_pass  {sensors|metal..., device_code, source?, candidate_id?, ...}
                                                   -> 过一次检（判据引擎 + 报警派单）
  list_alarms  {limit?, pending_only?}             -> 报警队列
  reinspect    {alarm_id, result, operator, note?, found_kind?, found_detail?}
                                                   -> 复检处置结论
  report_alarm {alarm_id}                          -> 把该报警上报 03 智能终端（同步）
  lockers      {}                                  -> 柜体与占用情况
  store_phone  {candidate_id, candidate_name?, device_desc?, locker_code?}
  take_phone   {candidate_id | cell_no, locker_code?, method?}
  replay_test  {operator?}                         -> 跑一次判据回放，返回指标
  metrics      {}                                  -> 最近的检出率/误报率指标
  report       {hours?}                            -> 生成验收报告，返回文件路径

用法：python integration_api.py --op status --args "{}"
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _stack():
    import config                                    # noqa: F401
    import storage
    import samples
    store = storage.Store()
    samples.ensure_builtin(store)
    return store


def op_status(a):
    import config
    import samples
    store = _stack()
    s = store.summary()
    return {"ok": True, "system": "06_智能安检与手机作弊器材检测",
            "devices": len(store.list_devices()),
            "passes": s["passes"], "alarms": s["alarms"],
            "pending_alarms": s["alarms"]["pending"],
            "throughput_ppm": round(s["throughput_ppm"], 1),
            "cells": s["cells"], "specials": s["specials"]["total"],
            "thresholds": config.thresholds(),
            "gate_mode": config.load_settings().get("gate_mode"),
            "metrics": samples.latest_metrics(store),
            "data_dir": str(config.DATA_ROOT)}


def op_devices(a):
    store = _stack()
    return {"ok": True, "devices": [
        {"code": d.code, "name": d.name, "kind": d.kind, "proto": d.proto,
         "address": d.address, "channel_no": d.channel_no, "room_id": d.room_id,
         "sensitivity": d.sensitivity, "status": d.status,
         "last_heartbeat": d.last_heartbeat} for d in store.list_devices()]}


def op_ingest_pass(a):
    import gate
    store = _stack()
    payload = dict(a or {})
    source = payload.pop("source", None) or "real"
    ev, alarm = gate.ingest(store, payload, source=source,
                            device_code=payload.get("device_code"))
    return {"ok": True, "event_id": ev.event_id, "result": ev.result,
            "kinds": ev.kinds, "level": (alarm.level if alarm else ""),
            "alarm_id": (alarm.alarm_id if alarm else ""),
            "detail": (alarm.detail if alarm else "放行"),
            "scores": ev.scores, "source": ev.source}


def op_list_alarms(a):
    store = _stack()
    rows = store.list_alarms(limit=int(a.get("limit") or 20),
                             pending_only=bool(a.get("pending_only")))
    return {"ok": True, "alarms": [
        {"alarm_id": x.alarm_id, "device_code": x.device_code, "kind": x.kind,
         "kinds": x.kinds, "level": x.level, "status": x.status,
         "candidate_id": x.candidate_id, "candidate_name": x.candidate_name,
         "created_at": x.created_at, "overdue": x.overdue(),
         "detail": x.detail} for x in rows]}


def op_reinspect(a):
    import reinspect
    store = _stack()
    ok, info = reinspect.finish(
        store, a.get("alarm_id") or "", a.get("result") or "release",
        found_kind=a.get("found_kind") or "", found_detail=a.get("found_detail") or "",
        note=a.get("note") or "集成联动调用", operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "alarm_id": a.get("alarm_id"), "message": str(info)[:200]}


def op_report_alarm(a):
    import linkage_bridge
    store = _stack()
    alarm = store.get_alarm(a.get("alarm_id") or "")
    if alarm is None:
        return {"ok": False, "reason": "报警单不存在"}
    ev = None
    for p in store.list_passes(limit=50):
        if p.event_id == alarm.event_id:
            ev = p
            break
    r = linkage_bridge.report_alarm(alarm, event=ev, sync=True,
                                    operator=a.get("operator") or "06_security")
    if r is None:
        return {"ok": False, "reason": "上报失败（03 智能终端不可用？）"}
    store.mark_alarm_reported(alarm.alarm_id, json.dumps(r, ensure_ascii=False)[:200])
    return {"ok": True, "alert_id": r.get("alert_id"), "level": r.get("level"),
            "downgraded": r.get("downgraded"), "credibility": r.get("credibility")}


def op_lockers(a):
    import locker
    store = _stack()
    return {"ok": True, "usage": locker.usage_by_locker(store),
            "cells": store.cell_stats()}


def op_add_locker(a):
    import locker
    store = _stack()
    ok, info = locker.add_locker(
        store, a.get("code") or "LOCK-A", a.get("name") or "手机保管柜",
        a.get("site") or "", int(a.get("cells_total") or 20),
        address=a.get("address") or "", operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "locker_id": info.locker_id, "code": info.code,
            "cells_total": info.cells_total}


def op_store_phone(a):
    import locker
    store = _stack()
    ok, info = locker.store_device(
        store, candidate_id=a.get("candidate_id") or "",
        candidate_name=a.get("candidate_name") or "",
        device_desc=a.get("device_desc") or "手机",
        locker_code=a.get("locker_code") or "", cell_no=a.get("cell_no") or "",
        ticket_no=a.get("ticket_no") or "", seat_no=a.get("seat_no") or "",
        method=a.get("method") or "manual", operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "locker_code": info.locker_code, "cell_no": info.cell_no}


def op_take_phone(a):
    import locker
    store = _stack()
    ok, info = locker.take_device(
        store, locker_code=a.get("locker_code") or "", cell_no=a.get("cell_no") or "",
        candidate_id=a.get("candidate_id") or "", method=a.get("method") or "face",
        operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "locker_code": info.locker_code, "cell_no": info.cell_no}


def op_replay_test(a):
    import samples
    store = _stack()
    run, _items = samples.run_replay(store, operator=a.get("operator") or "linkage",
                                     note="集成联动调用")
    return {"ok": True, "run_id": run.run_id, "total": run.total, "hit": run.hit,
            "miss": run.miss, "false_alarm": run.false_alarm,
            "metrics": run.metrics}


def op_metrics(a):
    import samples
    store = _stack()
    return {"ok": True, "metrics": samples.latest_metrics(store)}


def op_report(a):
    import report
    store = _stack()
    hours = float(a.get("hours") or 24)
    import time
    ok, info = report.write_report(store, since_epoch=time.time() - hours * 3600)
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "md": info["md"], "csv": info["csv"]}


OPS = {"status": op_status, "devices": op_devices, "ingest_pass": op_ingest_pass,
       "list_alarms": op_list_alarms, "reinspect": op_reinspect,
       "report_alarm": op_report_alarm, "lockers": op_lockers,
       "add_locker": op_add_locker,
       "store_phone": op_store_phone, "take_phone": op_take_phone,
       "replay_test": op_replay_test, "metrics": op_metrics, "report": op_report}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.args or "{}")
    except ValueError:
        return emit({"ok": False, "reason": "--args 不是合法 JSON"})
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                            # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
