# -*- coding: utf-8 -*-
"""
03 智能终端操作 · 集成接口（供 00_集成联动 调用）

ops:
  ingest_alert {code, room_id, seat_no, device_id, detail, credibility, source}
        -> 告警接入；**带可信度降权**：
           画质可信度 < 0.6 时，红色预警自动降为橙色并标注，避免画质问题造成误报堆积
  review_alert {alert_id, conclusion, reason, operator}
  list_alerts  {limit}
  status       {}
用法：python integration_api.py --op ingest_alert --args "{...}"
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 可信度低于该值时，红色预警自动降级（与《02》第四节第 4 条联动）
DOWNGRADE_BELOW = 0.6


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _stack():
    import offline as offline_mod
    import storage
    import alerts as alerts_mod
    import review as review_mod
    store = storage.Store()
    ob = offline_mod.Outbox(store)
    hub = alerts_mod.AlertHub(store, offline=ob)
    rev = review_mod.Reviewer(store, hub=hub)
    return store, hub, rev


def op_ingest_alert(a):
    import config
    import models
    store, hub, _rev = _stack()
    cred = a.get("credibility")
    try:
        cred = float(cred) if cred is not None else 1.0
    except (TypeError, ValueError):
        cred = 1.0
    alert = models.Alert(code=a.get("code") or "UNKNOWN",
                         detail=a.get("detail") or "",
                         room_id=a.get("room_id") or "",
                         seat_no=a.get("seat_no") or "",
                         candidate_id=a.get("candidate_id") or "",
                         candidate_name=a.get("candidate_name") or "",
                         device_id=a.get("device_id") or "",
                         level=a.get("level"),
                         source=a.get("source") or "linkage")
    downgraded = False
    if cred < 1.0:
        alert.extra["credibility"] = round(cred, 3)
        alert.detail = (alert.detail + "；" if alert.detail else "") + \
            "来源机位画质可信度 ×%.1f" % cred
    if cred < DOWNGRADE_BELOW and alert.level == config.LEVEL_RED:
        alert.level = config.LEVEL_ORANGE
        downgraded = True
        alert.detail += "；因画质可信度过低，红色预警已降级为橙色待复核"
    saved, created = hub.ingest(alert)
    if downgraded:
        store.log("预警降级", "%s 画质可信度 ×%.1f -> 橙色" % (saved.code, cred),
                  a.get("operator") or "linkage")
    return {"ok": True, "alert_id": saved.alert_id, "level": saved.level,
            "level_name": saved.level_name(), "created": created,
            "downgraded": downgraded, "credibility": cred,
            "status": saved.status, "force_confirm": saved.needs_force_confirm(),
            "source": "03_智能终端操作"}


def op_review_alert(a):
    import access
    store, _hub, rev = _stack()
    s = access.Session(a.get("operator") or "linkage", "patrol", "联动复核")
    ok, res = rev.review(a.get("alert_id") or "", a.get("conclusion") or "confirmed",
                         a.get("reason") or "联动复核", s)
    return {"ok": bool(ok), "message": res if not ok else "复核已留痕：%s" % res,
            "source": "03_智能终端操作"}


def op_list_alerts(a):
    store, _hub, _rev = _stack()
    rows = store.list_alerts(limit=int(a.get("limit") or 20))
    return {"ok": True, "alerts": [
        {"alert_id": x.alert_id, "code": x.code, "level": x.level,
         "level_name": x.level_name(), "room_id": x.room_id, "device_id": x.device_id,
         "status": x.status, "detail": x.detail,
         "credibility": x.extra.get("credibility"),
         "review_conclusion": x.review_conclusion} for x in rows]}


def op_status(a):
    import config
    store, _hub, _rev = _stack()
    chain_ok, chain_msg, n = store.verify_chain()
    return {"ok": True, "system": "03_智能终端操作",
            "alerts": store.summary()["total"], "reviews": len(store.list_reviews()),
            "audit_chain": chain_msg, "audit_ok": chain_ok, "audit_rows": n,
            "data_dir": str(config.DATA_ROOT)}


OPS = {"ingest_alert": op_ingest_alert, "review_alert": op_review_alert,
       "list_alerts": op_list_alerts, "status": op_status}


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
    except Exception as e:                       # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
