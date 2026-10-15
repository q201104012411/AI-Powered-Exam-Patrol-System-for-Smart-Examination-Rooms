# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 集成接口
（供 00_集成联动 / 其他专题 / 脚本 / 自检调用）

为什么每套系统各留一个 CLI 接口：01/02/03/06/10 都有同名的 config.py / models.py，
同进程 import 必然撞名。统一用 `子进程 + 最后一行 JSON` 串起来（00 联动层的既有约定）。

ops:
  status        {}                                -> 概览（考场/告警/分区/审计）
  templates     {}                                -> 8 类考场模板 + 差异项
  template      {type}                            -> 单个模板详情
  list_rooms    {room_type?, status?, special?, site?}
  get_room      {room_id | name}
  upsert_room   {name, room_type?, seats?, site?, apply_by?, apply_reason?}
                                                  -> 建档（同名更新）
  apply_template{room_id, room_type?, operator?, device_codes?}
                                                  -> 一键套模板 + 登记点位
  degrade       {room_id, level?, operator?}      -> 降级到最小可用配置
  admit         {room_id, operator?, image_map?, use_camera_ledger?}
                                                  -> 准入自检（不改变启用态）
  arm           {room_id, operator?, image_map?}  -> 自检通过才启用
  disable_room  {room_id, operator?, reason?}
  set_room      {room_id, invigilators?, remote_invigilator?, ledger_sample?,
                 calibration?, flags?, operator?}
  selfcheck     {room_id, operator?, image_map?}  -> 逐机位画质自检（返回系数）
  behaviors     {room_id?, category?}             -> 行为清单 + 本考场适用性
  route_alert   {room_id, behavior, confidence, device_code?, ...}
                                                  -> 分级告警（含抑制与送 03）
  review_alert  {event_id, conclusion, reason, operator, witness?}
  kpi           {room_id?, run?}                  -> 验收指标测算
  session       {room_id, name?, state?, ...}     -> 场次登记
  domain        {room_id}                         -> 该考场数据分区
  grant         {domain, role, operator?, reason?}
  revoke        {domain, role, operator?}
  dpia          {room_id, assessor, conclusion, operator}
  drill         {room_id, operator, witness, detail?}
  offline       {room_id, operator, offline?, note?}
  retention     {room_id, days, operator?}
  destroy       {room_id, operator, witness, reason}
  compliance    {site?}                           -> 合规体检
  report        {site?, hours?}                   -> 生成验收报告，返回文件路径
  linkage_status{deep?}                           -> 01/02/03/06 可达性
  audits        {limit?}                          -> 本模块哈希链审计

共 31 个 op（与 config.INTEGRATION_OPS 对齐，自检会核对两边集合一致）。

用法：python integration_api.py --op admit --args "{\"room_id\":\"RMxxx\"}"
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


def _stack(db=None):
    import storage
    return storage.Store(db)


def _room(store, a):
    rid = a.get("room_id") or ""
    if rid:
        r = store.get_room(rid)
        if r is None and a.get("name"):
            r = store.get_room_by_name(a["name"])
    else:
        r = store.get_room_by_name(a.get("name") or "")
    return r


# =============================================================================
# 基础
# =============================================================================
def op_status(a):
    import config
    store = _stack(a.get("db"))
    s = store.summary()
    hint = ""
    if not s["rooms"]:
        hint = ("尚无考场档案：先在界面「＋ 新建考场」建档并套用模板，"
                "或用 op upsert_room + apply_template")
    return {"ok": True, "system": "10_普通考场+特殊考场应用",
            "rooms": s["rooms"], "standard_rooms": s["standard_rooms"],
            "special_rooms": s["special_rooms"], "armed": s["armed"],
            "checks": s["checks"], "sessions": s["sessions"],
            "alerts": s["alerts"], "kpi_runs": s["kpi_runs"],
            "partitions": s["partitions"],
            "audit": store.verify_chain()[2],
            "template_count": len(config.ROOM_TYPES),
            "special_types": [config.type_name(t) for t in config.SPECIAL_TYPES],
            "behavior_count": len(config.BEHAVIORS),
            "hint": hint,
            "data_dir": str(config.DATA_ROOT)}


def op_templates(a):
    import rooms
    return {"ok": True, "templates": rooms.template_catalog()}


def op_template(a):
    import config
    import rooms
    t = a.get("type") or config.TYPE_STANDARD
    if t not in config.ROOM_TYPES:
        return {"ok": False, "reason": "未知考场类型：%s" % t}
    one = [x for x in rooms.template_catalog() if x["type"] == t]
    return {"ok": True, "template": one[0] if one else None}


def op_list_rooms(a):
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    rs = store.list_rooms(room_type=a.get("room_type"), status=a.get("status"),
                          special=(None if a.get("special") is None
                                   else bool(a.get("special"))),
                          site=a.get("site"), limit=int(a.get("limit") or 500))
    return {"ok": True, "rooms": [rooms_mod.room_brief(r) for r in rs],
            "count": len(rs)}


def op_get_room(a):
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    out = {"ok": True, "room": r.to_dict(), "brief": rooms_mod.room_brief(r),
           "advice": rooms_mod.capability_advice(r),
           "template_diff": rooms_mod.template_diff(r.room_type)}
    ck = store.latest_check(r.room_id)
    out["latest_check"] = ck.to_dict() if ck else None
    return out


def op_upsert_room(a):
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    name = (a.get("name") or "").strip()
    if not name:
        return {"ok": False, "reason": "考场名称必填"}
    room = rooms_mod.upsert_room(store, name, room_type=a.get("room_type") or
                                 "standard", operator=a.get("operator") or "",
                                 seats=a.get("seats"), site=a.get("site"),
                                 building=a.get("building"), floor=a.get("floor"),
                                 session_name=a.get("session_name"),
                                 apply_by=a.get("apply_by"),
                                 apply_reason=a.get("apply_reason"),
                                 note=a.get("note"))
    store.log("考场建档", "%s（%s）" % (room.name, room.type_name()),
              a.get("operator") or "", room.room_id)
    return {"ok": True, "room_id": room.room_id, "room": room.to_dict()}


# =============================================================================
# 模板与降级
# =============================================================================
def op_apply_template(a):
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    res = rooms_mod.apply_template(r, operator=a.get("operator") or "",
                                   room_type=a.get("room_type"), store=store)
    room = res["room"]
    if a.get("device_codes"):
        pts = []
        for p in (room.points or []):
            q = dict(p)
            code = (a["device_codes"] or {}).get(p.get("no"))
            if code:
                q["device_code"] = code
            pts.append(q)
        room.points = pts
        rooms_mod.recompute_caps(room)
    import config
    if room.status == config.PHASE_APPLY:
        room.status = config.PHASE_TEMPLATE
    store.save_room(room)
    store.log("套用模板", "%s -> %s（差异 %d 项）"
              % (room.name, room.type_name(), len(res["diff"]["items"])),
              a.get("operator") or "", room.room_id)
    return {"ok": True, "room": room.to_dict(), "diff": res["diff"],
            "advice": rooms_mod.capability_advice(room),
            "enabled_behaviors": len(room.enabled_behaviors or [])}


def op_degrade(a):
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    try:
        res = rooms_mod.degrade_to_minimal(r, level=a.get("level") or "minimal",
                                          operator=a.get("operator") or "",
                                          store=store)
    except rooms_mod.RoomError as e:
        return {"ok": False, "reason": str(e)}
    store.save_room(res["room"])
    store.log("降级为最小可用配置", "%s -> %s" % (r.name, res["level_name"]),
              a.get("operator") or "", r.room_id)
    return {"ok": True, "room": res["room"].to_dict(), "kept": res["kept"],
            "dropped": res["dropped"], "message": res["message"],
            "advice": rooms_mod.capability_advice(res["room"])}


# =============================================================================
# 准入自检
# =============================================================================
def op_selfcheck(a):
    import admission
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    pcs, defects, factor = admission.selfcheck_points(
        r, operator=a.get("operator") or "",
        image_map=a.get("image_map") or {},
        use_camera_ledger=bool(a.get("use_camera_ledger")))
    for pc in pcs:
        for p in (r.points or []):
            if p.get("no") == pc.no and pc.device_code and not p.get("device_code"):
                p["device_code"] = pc.device_code
    r.last_check_ok = 0 if defects else 1
    store.save_room(r)
    store.log("机位自检", "%s：%d 个机位，缺陷 %d 项，系数 ×%.2f"
              % (r.name, len(pcs), len(defects), factor),
              a.get("operator") or "", r.room_id)
    return {"ok": True, "room_id": r.room_id, "point_checks": [p.to_dict() for p in pcs],
            "defects": [d.to_dict() for d in defects], "factor": factor,
            "blocked": bool([d for d in defects if d.severity == "block"])}


def op_admit(a):
    import admission
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    res = admission.run_admission(store, r, operator=a.get("operator") or "",
                                  image_map=a.get("image_map") or {},
                                  use_camera_ledger=bool(a.get("use_camera_ledger")),
                                  auto_register=a.get("auto_register", True),
                                  note=a.get("note") or "集成接口准入自检")
    return {"ok": True, "blocked": res["blocked"], "decision": res["decision"],
            "check": res["check"].to_dict(),
            "blockers": res["blockers"], "warnings": res["warnings"],
            "passed": res["passed"], "factor": res["factor"],
            "next_phase": res["next_phase"],
            "next_phase_text": res["next_phase_text"],
            "advice": res["advice"]}


def op_arm(a):
    import admission
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    res = admission.arm(store, r, operator=a.get("operator") or "",
                        image_map=a.get("image_map") or {},
                        note=a.get("note") or "集成接口启用",
                        auto_register=a.get("auto_register", True))
    return {"ok": True, "armed": res.get("armed"), "blocked": res["blocked"],
            "decision": res["decision"], "check": res["check"].to_dict(),
            "blockers": res["blockers"], "warnings": res["warnings"],
            "factor": res["factor"], "status": r.status,
            "status_text": r.status_text()}


def op_disable_room(a):
    import admission
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    admission.disable(store, r, operator=a.get("operator") or "",
                      reason=a.get("reason") or "")
    return {"ok": True, "status": r.status, "status_text": r.status_text()}


def op_set_room(a):
    import admission
    import rooms as rooms_mod
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    op = a.get("operator") or ""
    if a.get("invigilators") is not None or a.get("remote_invigilator") is not None:
        admission.set_invigilators(store, r, a.get("invigilators") or [],
                                   remote=a.get("remote_invigilator"), operator=op)
    if a.get("ledger_sample") is not None:
        admission.set_ledger(store, r, a.get("ledger_sample") or [], operator=op)
    if a.get("calibration"):
        admission.set_calibration(store, r, operator=op,
                                  region=a.get("calibration") or "")
    if a.get("flags"):
        admission.set_flags(store, r, operator=op, **a["flags"])
    if a.get("points"):
        rooms_mod.import_points(r, a["points"])
    if a.get("seats"):
        r.seats = int(a["seats"])
    store.save_room(r)
    return {"ok": True, "room": r.to_dict(),
            "advice": rooms_mod.capability_advice(r)}


# =============================================================================
# 行为与告警
# =============================================================================
def op_behaviors(a):
    import behavior as behavior_mod
    store = _stack(a.get("db"))
    r = _room(store, a) if (a.get("room_id") or a.get("name")) else None
    cat = a.get("category")
    if r is not None:
        rows = []
        for code in (r.enabled_behaviors or []):
            e = behavior_mod.explain(store, r, code)
            if cat and e["category"] != cat:
                continue
            rows.append(e)
        return {"ok": True, "room_id": r.room_id, "count": len(rows),
                "behaviors": rows,
                "summary": behavior_mod.catalog_summary()}
    rows = [_b_dict(t) for t in _template_behaviors(cat)]
    return {"ok": True, "count": len(rows), "behaviors": rows,
            "summary": behavior_mod.catalog_summary()}


def _template_behaviors(cat=None):
    """内置行为清单（不依赖库）。"""
    import config
    out = []
    for t in list(config.BEHAVIORS) + list(config.EXTRA_BEHAVIORS):
        if cat and t[2] != cat:
            continue
        out.append(t)
    return out


def _b_dict(t):
    import config
    return {"code": t[0], "name": t[1], "category": t[2],
            "category_name": config.CATEGORIES.get(t[2], ""), "level": t[3],
            "level_name": config.LEVEL_META.get(t[3], {}).get("name", ""),
            "signals": list(t[4]),
            "alert_code": config.BEHAVIOR_ALERT_CODE.get(t[0], ""),
            "moe": config.MOE_CLASS_MAP.get(t[2], "")}


def op_route_alert(a):
    import alerting
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    res = alerting.raise_behavior_alert(
        store, r, a.get("behavior") or a.get("code") or "", a.get("confidence") or 0,
        device_code=a.get("device_code") or "", zone=a.get("zone") or "",
        seat_no=a.get("seat_no") or "", candidate_id=a.get("candidate_id") or "",
        detail=a.get("detail") or "", session_id=a.get("session_id") or "",
        sustained=a.get("sustained", True), multi_person=bool(a.get("multi_person")),
        closeup=bool(a.get("closeup")), camera_factor=a.get("camera_factor"),
        suppress_check=a.get("suppress_check", True), push=a.get("push", True),
        operator=a.get("operator") or "10_room")
    if not res.get("ok"):
        return res
    out = {"ok": True, "event": res["event"].to_dict(), "level": res["level"],
           "level_name": res["level_name"], "delivered": res["delivered"],
           "suppressed": res["suppressed"],
           "suppress_reason": res["suppress_reason"], "pushed_to": res["pushed_to"],
           "external": res["external"], "reason": res["reason"],
           "camera_factor": res.get("camera_factor")}
    return out


def op_review_alert(a):
    import alerting
    store = _stack(a.get("db"))
    ev = store.get_alert(a.get("event_id") or "")
    if ev is None:
        return {"ok": False, "reason": "告警事件不存在"}
    ok, info = alerting.resolve_alert(store, ev, a.get("conclusion") or "confirmed",
                                      reason=a.get("reason") or "",
                                      operator=a.get("operator") or "",
                                      witness=a.get("witness") or "")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "result": info}


def op_alerts(a):
    store = _stack(a.get("db"))
    rows = store.list_alerts(room_id=a.get("room_id"), level=a.get("level"),
                             limit=int(a.get("limit") or 50))
    return {"ok": True, "alerts": [x.to_dict() for x in rows],
            "stats": store.alert_stats(a.get("room_id"))}


# =============================================================================
# 指标 / 场次
# =============================================================================
def op_kpi(a):
    import kpi as kpi_mod
    store = _stack(a.get("db"))
    if a.get("room_id") or a.get("name"):
        r = _room(store, a)
        if r is None:
            return {"ok": False, "reason": "考场不存在"}
        run = None
        if a.get("run"):
            kw = dict(a["run"])
            src = kw.pop("data_source", None)
            run = kpi_mod.new_run(r, session_id=a.get("session_id") or "",
                                  operator=a.get("operator") or "",
                                  data_source=src or "manual", **kw)
        out = kpi_mod.evaluate(store, r, run=run, persist=True)
        return {"ok": True, "room_id": r.room_id, "metrics": out["metrics"],
                "summary": out["summary"], "notes": out["notes"],
                "run": out["run"]}
    out = kpi_mod.site_summary(store, site=a.get("site"))
    return {"ok": True, "site": out}


def op_session(a):
    import models
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    s = models.Session(room_id=r.room_id, room_name=r.name, room_type=r.room_type,
                       name=a.get("name") or r.session_name or "考试场次",
                       subject=a.get("subject") or "",
                       state=a.get("state") or "pre",
                       planned_seats=models.to_int(a.get("planned_seats"), r.seats),
                       actual_seats=models.to_int(a.get("actual_seats")),
                       operator=a.get("operator") or "",
                       note=a.get("note") or "")
    store.save_session(s)
    r.session_name = s.name
    r.session_state = s.state
    store.save_room(r)
    store.log("场次登记", "%s：%s（%s）" % (r.name, s.name, s.state),
              a.get("operator") or "", r.room_id)
    return {"ok": True, "session": s.to_dict()}


# =============================================================================
# 数据分域与合规
# =============================================================================
def op_domain(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    p = privacy.ensure_partition(store, r, operator=a.get("operator") or "")
    return {"ok": True, "partition": p.to_dict(),
            "domain_rules": privacy_mod_rules(r.data_domain),
            "matrix": privacy.domain_matrix(store)}


def privacy_mod_rules(d):
    import config
    return config.DOMAIN_RULES.get(d, {})


def op_grant(a):
    import privacy
    store = _stack(a.get("db"))
    ok, info = privacy.grant(store, a.get("domain") or "", a.get("role") or "",
                             operator=a.get("operator") or "",
                             granted_by=a.get("granted_by") or "",
                             reason=a.get("reason") or "")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "grant": info}


def op_revoke(a):
    import privacy
    store = _stack(a.get("db"))
    ok, info = privacy.revoke(store, a.get("domain") or "", a.get("role") or "",
                              operator=a.get("operator") or "",
                              reason=a.get("reason") or "")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "message": info}


def op_dpia(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    ok, info = privacy.do_dpia(store, r, operator=a.get("operator") or "",
                               assessor=a.get("assessor") or "",
                               conclusion=a.get("conclusion") or "",
                               witness=a.get("witness") or "")
    if not ok:
        return {"ok": False, "reason": info}
    row = None
    for x in privacy.compliance_report(store)["rows"]:
        if x["room_id"] == r.room_id:
            row = x
            break
    return {"ok": True, "message": info, "compliance": row}


def op_drill(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    ok, info = privacy.do_drill(store, r, operator=a.get("operator") or "",
                                witness=a.get("witness") or "",
                                detail=a.get("detail") or "")
    return {"ok": bool(ok), "reason": "" if ok else info, "message": info if ok else ""}


def op_offline(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    ok, info = privacy.do_offline_check(store, r, operator=a.get("operator") or "",
                                        offline=a.get("offline", True),
                                        note=a.get("note") or "")
    return {"ok": bool(ok), "message": info}


def op_retention(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    ok, info = privacy.set_retention(store, r, a.get("days"),
                                     operator=a.get("operator") or "",
                                     note=a.get("note") or "")
    return {"ok": bool(ok), "reason": "" if ok else info, "message": info if ok else ""}


def op_destroy(a):
    import privacy
    store = _stack(a.get("db"))
    r = _room(store, a)
    if r is None:
        return {"ok": False, "reason": "考场不存在"}
    ok, info = privacy.destroy(store, r, operator=a.get("operator") or "",
                               witness=a.get("witness") or "",
                               reason=a.get("reason") or "")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "destroy": info}


def op_compliance(a):
    import privacy
    store = _stack(a.get("db"))
    rep = privacy.compliance_report(store, site=a.get("site"))
    rep["ok"] = True
    rep["matrix"] = privacy.domain_matrix(store)
    rep["partitions"] = privacy.partition_view(store)
    rep["overdue_destroy"] = [p.to_dict() for p in privacy.overdue_destroy(store)]
    return rep


def op_report(a):
    import report
    store = _stack(a.get("db"))
    ok, info = report.write_report(store, site=a.get("site"),
                                   hours=float(a.get("hours") or 24 * 30))
    if not ok:
        return {"ok": False, "reason": str(info)[:200]}
    return {"ok": True, "md": info["md"], "csv": info["csv"],
            "rooms": info["rooms"]}


def op_linkage_status(a):
    import linkage_client
    return {"ok": True, "linkage": linkage_client.status(deep=bool(a.get("deep")))}


def op_audits(a):
    store = _stack(a.get("db"))
    rows = store.list_audits(limit=int(a.get("limit") or 50))
    ok, n, msg = store.verify_chain()
    return {"ok": True, "audits": [x.to_dict() for x in rows], "chain_ok": ok,
            "chain_count": n, "chain_message": msg}


OPS = {
    "status": op_status, "templates": op_templates, "template": op_template,
    "list_rooms": op_list_rooms, "get_room": op_get_room,
    "upsert_room": op_upsert_room, "apply_template": op_apply_template,
    "degrade": op_degrade, "selfcheck": op_selfcheck, "admit": op_admit,
    "arm": op_arm, "disable_room": op_disable_room, "set_room": op_set_room,
    "behaviors": op_behaviors, "route_alert": op_route_alert,
    "review_alert": op_review_alert, "alerts": op_alerts, "kpi": op_kpi,
    "session": op_session, "domain": op_domain, "grant": op_grant,
    "revoke": op_revoke, "dpia": op_dpia, "drill": op_drill,
    "offline": op_offline, "retention": op_retention, "destroy": op_destroy,
    "compliance": op_compliance, "report": op_report,
    "linkage_status": op_linkage_status, "audits": op_audits,
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
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                       # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
