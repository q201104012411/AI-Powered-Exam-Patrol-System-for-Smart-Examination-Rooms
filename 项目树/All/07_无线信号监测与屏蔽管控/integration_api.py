# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 集成接口（供 00_集成联动 / 脚本 / 自检 / 其它模块调用）

为什么每套系统各留一个 CLI：01/02/03/06/10 都有同名的 config.py / models.py，
同进程 import 必然撞名；本项目统一用"子进程 + 最后一行 JSON"串起来（00 联动层的约定）。

ops（与 config.INTEGRATION_OPS 一一对应，自检会核对两边集合相等）：
  status          {}                                  -> 概览（设备/扫描/异常/屏蔽/合规/联动）
  bands           {}                                  -> 频段表 + 当前屏蔽计划
  features        {}                                  -> 作弊信号特征库
  add_feature     {code,name,low_mhz,high_mhz,bandwidth_mhz,duty,is_legal,base_confidence,...}
  features_test   {freq_mhz,bandwidth_mhz,duty}       -> 单峰特征匹配结果
  devices         {kind?}                             -> 设备清单
  add_device      {code,name,kind,x,y,direction,sector_width,bands,power_w,...}
  heartbeat       {code,status?}                      -> 设备心跳
  scan            {station_code,bins|peaks|freq_mhz,...,report?,locate?}
                                                      -> 吃一帧扫描，产出异常（可选上报/定位）
  scans           {station_code?,band?,limit?}        -> 扫描历史（不含 bins）
  anomalies       {status?,severity?,kind?,limit?}    -> 异常列表 + 统计
  anomaly         {anomaly_id}                        -> 异常详情（含定位/联动）
  locate          {anomaly_id,measurements?}          -> 定位解算
  review_anomaly  {anomaly_id,conclusion,reason,reviewer?,sync_terminal?}
  report_anomaly  {anomaly_id,sync?}                  -> 送 03 智能终端
  jammers         {}                                  -> 屏蔽器 + 当前状态 + 开机闸门预览
  shield_on       {device_code|device_id,session_id?,bands?,power_w?,drill?,reason?}
  shield_off      {device_code|device_id,reason?}
  shield_auto     {}                                  -> 时间表 tick（自动开/关）
  shield_state    {device_code?}                      -> 屏蔽状态
  switch_log      {device_id?,action?,limit?}         -> 开关记录
  sessions        {date?,state?}                      -> 考试场次
  add_session     {name,date,start,end,subject?,rooms?,pre_open_sec?,post_close_sec?}
  session_state   {}                                  -> 当前窗口/场次状态
  approvals       {kind?,limit?}                      -> 审批/协同
  add_approval    {kind,no,org,valid_from,valid_to,doc,bands?,site?}
  spill_test      {device_code,point,distance_m,band,measured_dbm,...}
  spill_estimate  {device_code,distance_m,band?}       -> 外溢估算（非验收结论）
  shield_test     {room_id,band,before_dbm,after_dbm,rate?,...}
  compliance      {}                                  -> 合规体检 + 指标
  metrics         {}                                  -> 验收指标汇总
  replay_test     {}                                  -> 回放测试（检出率/误报/时延/定位误差）
  correlate       {source:"06"|"02",room_id?,limit?}  -> 跨模块关联
  linkage_status  {deep?}                             -> 00/01/02/03/06 可达性
  report          {hours?}                            -> 生成验收报告（md/csv）
  seed_demo       {}                                  -> 载入演示设备/审批/场次/实测证据
  audits          {limit?}                            -> 本模块哈希链审计

用法：python integration_api.py --op status --args "{}"
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return 0


def _stack(a=None):
    import storage
    db = (a or {}).get("db")
    return storage.Store(db)


def _jammer(store, a):
    import config
    dev = store.get_device(code=a.get("device_code") or a.get("code") or "",
                           device_id=a.get("device_id") or "")
    if dev is None:
        return None
    if dev.kind != config.DEV_JAMMER:
        return None
    return dev


# =============================================================================
# 概览 / 频段 / 特征库
# =============================================================================
def op_status(a):
    import compliance
    import config
    import linkage_bridge
    store = _stack(a)
    s = store.summary()
    metrics = compliance.metrics(store)
    chk = compliance.checks(store)
    run = store.latest_run()
    replay = ({"detect_rate": run.detect_rate, "false_alarm": run.false_alarm,
               "avg_latency": run.avg_latency, "created_at": run.created_at}
              if run is not None else
              {"detect_rate": None, "false_alarm": None, "avg_latency": None,
               "created_at": ""})
    out = {"ok": True, "system": "07_无线信号监测与屏蔽管控",
           "site": config.load_settings().get("site"),
           "devices": s["devices"], "monitors": s["monitors"],
           "probes": s["probes"], "jammers": s["jammers"],
           "scans": s["scans"], "anomalies": s["anomalies"],
           "open_anomalies": s["open_anomalies"], "critical": s["critical"],
           "avg_latency": metrics["avg_latency_sec"],
           "p95_latency": metrics["p95_latency_sec"],
           "coverage_rate": metrics["coverage_rate"],
           "jammers_on": s["jammers_on"], "shield_rate": s["shield_rate"],
           "sessions": s["sessions"], "sessions_today": s["sessions_today"],
           "switch_logs": s["switch_logs"], "approvals": s["approvals"],
           "features": s["features"], "correlations": s["correlations"],
           "compliance": {"score": chk["score"], "ok": chk["ok"],
                          "fail": chk["fail"], "warn": chk["warn"],
                          "summary": chk["summary_text"]},
           "replay": {"detect_rate": replay.get("detect_rate"),
                      "false_alarm": replay.get("false_alarm"),
                      "avg_latency": replay.get("avg_latency"),
                      "created_at": replay.get("created_at")},
           "linkage": linkage_bridge.link_status()["client"],
           "shield_plan": config.shield_plan(),
           "data_dir": str(config.DATA_ROOT)}
    return out


def op_bands(a):
    import config
    return {"ok": True, "bands": config.band_options(),
            "groups": config.BAND_GROUP_TEXT,
            "plan": config.shield_plan(),
            "plan_names": [config.band_name(b) for b in config.shield_plan()]}


def op_features(a):
    import features
    store = _stack(a)
    features.ensure_builtin(store)
    return {"ok": True, "features": features.catalog(store),
            "summary": features.summary(store),
            "weights": {"freq": features.W_FREQ, "bw": features.W_BW,
                        "duty": features.W_DUTY}}


def op_add_feature(a):
    import features
    store = _stack(a)
    ok, info = features.add_feature(
        store, a.get("code") or "", a.get("name") or "",
        a.get("low_mhz"), a.get("high_mhz"), a.get("bandwidth_mhz"),
        modulation=a.get("modulation") or "", duty=a.get("duty") or "burst",
        is_legal=bool(a.get("is_legal")), base_confidence=a.get("base_confidence") or 0.7,
        note=a.get("note") or "", operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "message": info}


def op_features_test(a):
    import features
    store = _stack(a)
    features.ensure_builtin(store)
    m = features.match_peak(store, {"freq_mhz": a.get("freq_mhz") or 0,
                                   "bandwidth_mhz": a.get("bandwidth_mhz") or 0.05,
                                   "duty": a.get("duty") or "burst"})
    return {"ok": True, "matched": m["matched"], "score": m["score"],
            "confidence": m["confidence"], "is_legal": m["is_legal"],
            "reason": m["reason"], "parts": m["parts"],
            "feature": (m["feature"].to_dict() if m.get("feature") else None)}


# =============================================================================
# 设备
# =============================================================================
def op_devices(a):
    import config
    store = _stack(a)
    rows = store.list_devices(kind=a.get("kind") or None)
    return {"ok": True, "devices": [r.to_dict() for r in rows],
            "kinds": config.DEV_KIND_TEXT, "count": len(rows)}


def op_add_device(a):
    import config
    import models
    store = _stack(a)
    code = str(a.get("code") or "").strip()
    if not code:
        return {"ok": False, "reason": "设备编码 code 必填"}
    kind = a.get("kind") or config.DEV_MONITOR
    if kind not in config.DEV_KINDS:
        return {"ok": False, "reason": "未知设备类型：%s" % kind}
    if kind == config.DEV_JAMMER:
        bands = [b for b in (a.get("bands") or config.shield_plan()) if b in config.BAND_INDEX]
        if not bands:
            return {"ok": False, "reason": "屏蔽频段无效"}
    dev = store.get_device(code=code)
    if dev is None:
        dev = models.Device(code=code)
    dev.name = str(a.get("name") or dev.name or code)
    dev.kind = kind
    dev.site = str(a.get("site") or dev.site or config.load_settings().get("site") or "")
    dev.room_id = str(a.get("room_id") or dev.room_id or "")
    dev.x = models.to_float(a.get("x"), dev.x)
    dev.y = models.to_float(a.get("y"), dev.y)
    if a.get("located") is not None:
        dev.located = 1 if a.get("located") else 0
    elif ("x" in a or "y" in a) and not dev.located:
        dev.located = 1
    dev.direction = models.to_float(a.get("direction"), dev.direction)
    dev.sector_width = models.to_float(a.get("sector_width"), dev.sector_width)
    if kind == config.DEV_JAMMER:
        dev.bands = [b for b in (a.get("bands") or dev.bands or config.shield_plan())
                     if b in config.BAND_INDEX]
    else:
        dev.bands = list(a.get("bands") or dev.bands or [])
    dev.power_w = models.to_float(a.get("power_w"), dev.power_w)
    dev.max_power_w = models.to_float(a.get("max_power_w"), dev.max_power_w)
    if a.get("status") in config.DEV_STATUSES:
        dev.status = a["status"]
    dev.proto = a.get("proto") or dev.proto or config.PROTO_MANUAL
    dev.address = str(a.get("address") or dev.address or "")
    dev.note = str(a.get("note") or dev.note or "")
    store.save_device(dev)
    store.log("设备登记", "%s（%s）坐标(%.0f,%.0f)" % (
        dev.code, config.DEV_KIND_TEXT.get(dev.kind, dev.kind), dev.x, dev.y),
        a.get("operator") or "linkage")
    return {"ok": True, "device": dev.to_dict()}


def op_heartbeat(a):
    store = _stack(a)
    dev = store.heartbeat(a.get("code") or "", a.get("status") or "online")
    if dev is None:
        return {"ok": False, "reason": "设备不存在"}
    return {"ok": True, "device_code": dev.code, "status": dev.status,
            "last_seen": dev.last_seen}


# =============================================================================
# 扫描 / 异常
# =============================================================================
def op_scan(a):
    import config
    import linkage_bridge
    import locate
    import spectrum
    store = _stack(a)
    payload = {k: v for k, v in a.items() if k not in ("db", "report", "locate")}
    r = spectrum.ingest_scan(store, payload, source=payload.get("source") or "manual")
    if not r.get("ok"):
        return r
    reports = []
    actions = []
    do_report = bool(a.get("report")) or bool(config.load_settings().get("report_to_terminal"))
    for aid in r.get("new") or []:
        an = store.get_anomaly(aid)
        if an is None:
            continue
        if a.get("locate"):
            loc = locate.locate_anomaly(store, an)
            actions.append({"anomaly_id": aid, "locate": loc})
        if do_report:
            reports.append(linkage_bridge.report_anomaly(store, an, sync=True))
    r["reports"] = reports
    r["locate"] = actions
    return r


def op_scans(a):
    store = _stack(a)
    rows = store.list_scans(station_code=a.get("station_code") or None,
                            band=a.get("band") or None,
                            since_epoch=a.get("since_epoch") or None,
                            limit=int(a.get("limit") or 50))
    out = []
    for s in rows:
        d = s.to_dict()
        d.pop("raw", None)
        d["peaks"] = (s.peaks or [])[:12]
        out.append(d)
    return {"ok": True, "scans": out, "count": len(out)}


def op_anomalies(a):
    store = _stack(a)
    rows = store.list_anomalies(status=a.get("status") or None,
                                severity=a.get("severity") or None,
                                kind=a.get("kind") or None,
                                station_code=a.get("station_code") or None,
                                room_id=a.get("room_id") or None,
                                limit=int(a.get("limit") or 50))
    return {"ok": True, "anomalies": [r.to_dict() for r in rows],
            "stats": store.anomaly_stats()}


def op_anomaly(a):
    store = _stack(a)
    ad = store.get_anomaly(a.get("anomaly_id") or "")
    if ad is None:
        return {"ok": False, "reason": "异常不存在"}
    return {"ok": True, "anomaly": ad.to_dict(),
            "locations": [x.to_dict() for x in
                          store.list_locations(anomaly_id=ad.anomaly_id)],
            "correlations": [x.to_dict() for x in
                             store.list_correlations(anomaly_id=ad.anomaly_id)]}


def op_locate(a):
    import locate
    store = _stack(a)
    ad = store.get_anomaly(a.get("anomaly_id") or "")
    if ad is None:
        return {"ok": False, "reason": "异常不存在"}
    res = locate.locate_anomaly(store, ad, measurements=a.get("measurements"),
                                operator=a.get("operator") or "linkage")
    return res


def op_review_anomaly(a):
    import config
    import linkage_client
    import models
    store = _stack(a)
    ad = store.get_anomaly(a.get("anomaly_id") or "")
    if ad is None:
        return {"ok": False, "reason": "异常不存在"}
    conclusion = a.get("conclusion") or config.ST_CONFIRMED
    if conclusion not in config.REVIEW_CONCLUSIONS:
        return {"ok": False, "reason": "复核结论必须是 confirmed/false_positive/closed"}
    if ad.status not in config.OPEN_STATUSES and not a.get("force"):
        return {"ok": False, "reason": "该异常已处置（%s）" % ad.status_text()}
    ad.review_conclusion = conclusion
    ad.review_reason = str(a.get("reason") or "")
    ad.reviewer = str(a.get("reviewer") or a.get("operator") or "linkage")
    ad.reviewed_at = models.now_ts()
    ad.status = conclusion
    store.save_anomaly(ad)
    store.log("异常复核", "%s -> %s（%s）" % (ad.anomaly_id,
                                           config.REVIEW_CONCLUSIONS[conclusion],
                                           ad.review_reason or "-"), ad.reviewer)
    terminal = None
    if a.get("sync_terminal") and ad.alert_id:
        if ad.severity == "critical":
            terminal = {"ack": linkage_client.ack_alert(ad.alert_id, ad.reviewer)}
        terminal = terminal or {}
        terminal["review"] = linkage_client.review_alert(
            ad.alert_id,
            conclusion=("false_positive" if conclusion == config.ST_FALSE
                        else "confirmed"),
            reason=ad.review_reason or "07 复核", operator=ad.reviewer)
    return {"ok": True, "anomaly": ad.to_dict(), "terminal": terminal}


def op_report_anomaly(a):
    import linkage_bridge
    store = _stack(a)
    ad = store.get_anomaly(a.get("anomaly_id") or "")
    if ad is None:
        return {"ok": False, "reason": "异常不存在"}
    r = linkage_bridge.report_anomaly(store, ad, sync=True,
                                      operator=a.get("operator") or "07_rf")
    return {"ok": bool(r.get("ok")), "result": r,
            "alert_id": store.get_anomaly(ad.anomaly_id).alert_id}


# =============================================================================
# 屏蔽管控
# =============================================================================
def op_jammers(a):
    import shield
    store = _stack(a)
    rows = []
    for j in store.list_devices(kind="jammer"):
        ev = shield.evaluate(store, j)
        rows.append({"device": j.to_dict(), "state": shield.state_view(store, j),
                     "gate": {"can_on": ev["can_on"],
                              "blockers": ev["blockers"],
                              "warnings": ev["warnings"],
                              "power_w": ev["plan"]["power_w"],
                              "bands": ev["plan"]["bands"]},
                     "power_cap": shield.power_cap(),
                     "sector_cap": shield.sector_cap()})
    return {"ok": True, "jammers": rows, "states": [s.to_dict() for s in
                                                    store.list_states()]}


def op_shield_on(a):
    import shield
    store = _stack(a)
    jam = _jammer(store, a)
    if jam is None:
        return {"ok": False, "reason": "屏蔽器不存在"}
    r = shield.turn_on(store, jam, operator=a.get("operator") or "linkage",
                       reason=a.get("reason") or "",
                       session_id=a.get("session_id") or "",
                       bands=a.get("bands") or None, power_w=a.get("power_w"),
                       drill=bool(a.get("drill")), now=a.get("now") or None)
    return r


def op_shield_off(a):
    import shield
    store = _stack(a)
    jam = _jammer(store, a)
    if jam is None:
        return {"ok": False, "reason": "屏蔽器不存在"}
    return shield.turn_off(store, jam, operator=a.get("operator") or "linkage",
                           reason=a.get("reason") or "集成接口关闭",
                           now=a.get("now") or None)


def op_shield_auto(a):
    import linkage_bridge
    import shield
    store = _stack(a)
    r = shield.tick(store, now=a.get("now") or None,
                    operator=a.get("operator") or "auto")
    r["reported"] = linkage_bridge.after_tick(store, r)
    r["ok"] = True
    return r


def op_shield_state(a):
    import shield
    store = _stack(a)
    if a.get("device_code") or a.get("device_id"):
        jam = _jammer(store, a)
        if jam is None:
            return {"ok": False, "reason": "屏蔽器不存在"}
        return {"ok": True, "state": shield.state_view(store, jam)}
    return {"ok": True, "states": [shield.state_view(store, j) for j in
                                   store.list_devices(kind="jammer")]}


def op_switch_log(a):
    store = _stack(a)
    rows = store.list_switches(device_id=a.get("device_id") or None,
                               action=a.get("action") or None,
                               limit=int(a.get("limit") or 100))
    return {"ok": True, "logs": [r.to_dict() for r in rows], "count": len(rows)}


# =============================================================================
# 时间表 / 审批
# =============================================================================
def op_sessions(a):
    import schedule
    store = _stack(a)
    rows = store.list_sessions(date=a.get("date") or None,
                               state=a.get("state") or None,
                               limit=int(a.get("limit") or 100))
    out = []
    for s in rows:
        op, cl, st, en = schedule.window(s)
        d = s.to_dict()
        phase = schedule.state_at(s)
        d.update({"window_open": op, "window_close": cl, "phase": phase,
                  "phase_text": schedule.PHASE_TEXT.get(phase, ""),
                  "remaining": schedule.remaining_text(s)})
        out.append(d)
    cur, phase = schedule.find_window(store)
    return {"ok": True, "sessions": out,
            "now": {"in_window": bool(cur), "phase": phase,
                    "phase_text": schedule.PHASE_TEXT.get(phase, ""),
                    "session": cur.to_dict() if cur else None}}


def op_add_session(a):
    import schedule
    store = _stack(a)
    ok, info = schedule.add_session(
        store, a.get("name") or "考试场次", a.get("date") or "",
        a.get("start") or "", a.get("end") or "",
        subject=a.get("subject") or "", pre_open_sec=a.get("pre_open_sec"),
        post_close_sec=a.get("post_close_sec"), rooms=a.get("rooms") or [],
        operator=a.get("operator") or "linkage", note=a.get("note") or "")
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "session": info.to_dict()}


def op_session_state(a):
    import schedule
    store = _stack(a)
    r = schedule.tick(store, now=a.get("now") or None)
    r["ok"] = True
    return r


def op_approvals(a):
    import config
    store = _stack(a)
    rows = store.list_approvals(kind=a.get("kind") or None,
                                site=a.get("site") or None,
                                limit=int(a.get("limit") or 100))
    return {"ok": True, "approvals": [r.to_dict() for r in rows],
            "kinds": config.APPR_KIND_TEXT}


def op_add_approval(a):
    import config
    import models
    store = _stack(a)
    kind = a.get("kind") or config.APPR_APPROVAL
    if kind not in config.APPR_KINDS:
        return {"ok": False, "reason": "未知审批类型：%s" % kind}
    no = str(a.get("no") or "").strip()
    if not no:
        return {"ok": False, "reason": "审批/报备编号 no 必填"}
    site = a.get("site") or config.load_settings().get("site") or ""
    vf = a.get("valid_from") or models.today_str()
    vt = a.get("valid_to") or models.date_str(models.now_epoch() + 30 * 86400)
    if vf > vt:
        return {"ok": False, "reason": "生效日期不能晚于失效日期"}
    ap = models.Approval(kind=kind, no=no, org=a.get("org") or "",
                         site=site,
                         bands=[b for b in (a.get("bands") or config.shield_plan())
                                if b in config.BAND_INDEX],
                         valid_from=vf, valid_to=vt, doc=a.get("doc") or "",
                         status=a.get("status") or config.APPR_APPROVED,
                         note=a.get("note") or "")
    store.save_approval(ap)
    store.log("登记审批", "%s %s（%s~%s）" % (
        config.APPR_KIND_TEXT.get(kind, kind), no, vf, vt),
        a.get("operator") or "linkage")
    return {"ok": True, "approval": ap.to_dict()}


# =============================================================================
# 实测 / 合规 / 指标 / 报告
# =============================================================================
def op_spill_test(a):
    import shield
    store = _stack(a)
    jam = _jammer(store, a)
    if jam is None:
        return {"ok": False, "reason": "屏蔽器不存在"}
    ok, info = shield.spill_test(store, jam, a.get("point") or "",
                                 a.get("distance_m") or 0,
                                 a.get("band") or "gsm900",
                                 a.get("measured_dbm") or -100,
                                 operator=a.get("operator") or "linkage",
                                 note=a.get("note") or "")
    return {"ok": bool(ok), "result": info}


def op_spill_estimate(a):
    import shield
    store = _stack(a)
    jam = _jammer(store, a)
    if jam is None:
        return {"ok": False, "reason": "屏蔽器不存在"}
    return shield.estimate_spill(store, jam, a.get("distance_m") or 100,
                                 band=a.get("band") or None)


def op_shield_test(a):
    import shield
    store = _stack(a)
    ok, info = shield.shield_test(store, a.get("room_id") or "", a.get("band") or "",
                                  a.get("before_dbm") or -60,
                                  a.get("after_dbm") or -90,
                                  rate=a.get("rate"),
                                  operator=a.get("operator") or "linkage",
                                  note=a.get("note") or "")
    return {"ok": bool(ok), "result": info}


def op_compliance(a):
    import compliance
    import schedule
    store = _stack(a)
    chk = compliance.checks(store)
    compliant = chk["ok"]
    chk["metrics"] = compliance.metrics(store)
    chk["consistency"] = schedule.consistency(store)
    chk["compliant"] = compliant
    chk["ok"] = True
    return chk


def op_metrics(a):
    import compliance
    import samples
    store = _stack(a)
    return {"ok": True, "metrics": compliance.metrics(store),
            "anomaly_stats": store.anomaly_stats(),
            "shield_rate": store.shield_rate(),
            "replay": samples.latest_metrics(store)}


def op_replay_test(a):
    import samples
    store = _stack(a)
    r = samples.run_replay(store, operator=a.get("operator") or "linkage",
                           note=a.get("note") or "集成接口回放")
    return r


def op_correlate(a):
    import linkage_bridge
    store = _stack(a)
    source = str(a.get("source") or "06")
    if source == "02":
        return linkage_bridge.correlate_02(store, room_id=a.get("room_id") or "",
                                           device_code=a.get("device_code") or "",
                                           operator=a.get("operator") or "07_rf")
    return linkage_bridge.correlate_06(store, limit=int(a.get("limit") or 50),
                                       operator=a.get("operator") or "07_rf")


def op_linkage_status(a):
    import linkage_bridge
    return {"ok": True, "linkage": linkage_bridge.link_status()}


def op_report(a):
    import report
    store = _stack(a)
    ok, info = report.write_report(store, site=a.get("site") or None,
                                   hours=float(a.get("hours") or 24 * 30),
                                   operator=a.get("operator") or "linkage")
    if not ok:
        return {"ok": False, "reason": str(info)[:300]}
    return {"ok": True, "md": info["md"], "csv": info["csv"],
            "conclusion": info["conclusion"],
            "metrics": info["metrics"],
            "compliance": {"score": info["compliance"]["score"],
                           "fail": info["compliance"]["fail"],
                           "warn": info["compliance"]["warn"]}}


def op_seed_demo(a):
    import samples
    store = _stack(a)
    return {"ok": True, "result": samples.seed_demo(store),
            "note": "演示数据，不替代真实现场部署与报备"}


def op_audits(a):
    store = _stack(a)
    rows = store.list_audits(limit=int(a.get("limit") or 100))
    ok, n, msg = store.verify_chain()
    return {"ok": True, "audits": [x.to_dict() for x in rows], "chain_ok": ok,
            "chain_count": n, "chain_message": msg}


OPS = {
    "status": op_status, "bands": op_bands, "features": op_features,
    "add_feature": op_add_feature, "features_test": op_features_test,
    "devices": op_devices, "add_device": op_add_device, "heartbeat": op_heartbeat,
    "scan": op_scan, "scans": op_scans,
    "anomalies": op_anomalies, "anomaly": op_anomaly, "locate": op_locate,
    "review_anomaly": op_review_anomaly, "report_anomaly": op_report_anomaly,
    "jammers": op_jammers, "shield_on": op_shield_on, "shield_off": op_shield_off,
    "shield_auto": op_shield_auto, "shield_state": op_shield_state,
    "switch_log": op_switch_log,
    "sessions": op_sessions, "add_session": op_add_session,
    "session_state": op_session_state,
    "approvals": op_approvals, "add_approval": op_add_approval,
    "spill_test": op_spill_test, "spill_estimate": op_spill_estimate,
    "shield_test": op_shield_test,
    "compliance": op_compliance, "metrics": op_metrics,
    "replay_test": op_replay_test, "correlate": op_correlate,
    "linkage_status": op_linkage_status, "report": op_report,
    "seed_demo": op_seed_demo, "audits": op_audits,
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
    except Exception as e:                            # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:300]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
