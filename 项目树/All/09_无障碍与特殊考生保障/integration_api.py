# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 集成接口（供 00_集成联动 / 其它专题 / 脚本 / 自检调用）

为什么单独一个脚本：01/02/03/06/09 都有同名的 config.py / models.py，
同进程 import 必然撞名。所以各套各留一个 CLI 接口，联动层用**子进程 + JSON** 串起来。
本接口与 01/02/03/06 的 integration_api.py **协议完全一致**（--op / --args，最后一行一个 JSON），
所以其它专题按同一套写法就能调它；00 的总线里本模块在运行时注册为 "09"。

ops:
  status        {}                                    -> 概览（通道/档案/替代核验/受阻/演练/指标）
  channels      {site?, kind?, status?}               -> 通道台账 + 合规校验
  save_channel  {code, name, site, kind, width_mm?, ...}
  check_channels{site?}                               -> 三通道并行与尺寸合规校验结论
  profiles      {limit?, need?, site?, status?}       -> 特殊考生档案（脱敏视图由 ledger 给）
  add_profile   {candidate_id, needs[], ...}          -> 建档 + 自动推荐保障措施
  measures      {profile_id}                          -> 措施落实状态
  set_measure   {profile_id, measure, status, ...}    -> 更新措施状态（已落实须写责任人）
  alt_verify    {candidate_id, mode, docs{}, inspector, witness, ...}
                                                      -> 替代核验（双证比对 + 双人见证）
  list_alt_verify {limit?}                            -> 替代核验记录
  tech_block    {candidate_id, symptom, detail, ...}  -> 登记"因技术原因入场受阻"（红色上报）
  list_blocks   {limit?, resolved?}                   -> 受阻事件
  resolve_block {block_id, final_result, resolution}  -> 结案（denied 即计入未入场）
  fairness_test {groups{}, ...}                       -> 记录一次算法分层测试
  fairness_runs {limit?}                              -> 分层测试历史
  drill         {scenario, site, ...}                 -> 演练记录
  list_drills   {limit?, session?}                    -> 演练历史
  ledger        {fmt?, site?, desensitize?}           -> 导出特殊考生处置台账（默认脱敏）
  metrics       {}                                    -> 关键指标与达标判定
  report        {hours?}                              -> 生成验收证据报告（md + csv）
  linkage       {probe?, write?, device?, candidate?} -> 联动状态 / 全链路探测
  flush_outbox  {}                                    -> 补发待发的联动事件

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


def _store():
    import storage
    return storage.Store()


def _notifier(store):
    import linkage_bridge
    return lambda code, info: linkage_bridge.notify(code, info, store=store)


def op_status(a):
    import config
    import access
    import metrics as metrics_mod
    store = _store()
    cfg = config.load_settings()
    s = store.summary()
    m = metrics_mod.compute(store, cfg)
    ln = store.verify_audit_chain()
    return {"ok": True, "system": "09_无障碍与特殊考生保障",
            "sites": config.sites(cfg), "session": cfg.get("session"),
            "channels": s["channels"], "channels_usable": s["channels_usable"],
            "channel_rate": access.channel_config_rate(store, cfg)["rate"],
            "profiles": s["profiles"]["total"], "by_need": s["profiles"]["by_need"],
            "without_consent": s["profiles"]["without_consent"],
            "measures": s["accommodations"],
            "alt_verifies": s["alt_verifies"]["total"],
            "alt_verify_modes": s["alt_verifies"]["by_mode"],
            "tech_blocks": s["tech_blocks"],
            "drills": s["drills"],
            "fairness": s["fairness_runs"],
            "metrics_ok": m["ok"], "hard_fail": m["hard_fail"],
            "outbox": s["outbox"], "audit_ok": ln[0], "audit_chain": ln[1],
            "audit_rows": ln[2], "data_dir": str(config.DATA_ROOT),
            "crypto": _crypto_text()}


def _crypto_text():
    try:
        import data_crypto
        return data_crypto.status_text()
    except Exception:                                          # noqa: BLE001
        return "加密模块不可用"


def op_channels(a):
    import access
    store = _store()
    rows = store.list_channels(site=a.get("site") or None,
                               kind=a.get("kind") or None,
                               status=a.get("status") or None)
    return {"ok": True, "channels": [
        dict(c.to_dict(), check=access.validate_channel(c),
             kind_text=c.kind_text(), state_text=c.state_text()) for c in rows]}


def op_save_channel(a):
    import models
    import access
    store = _store()
    code = str(a.get("code") or "").strip()
    if not code:
        return {"ok": False, "reason": "缺少通道编码 code"}
    ch = store.get_channel(code=code) or models.Channel(code=code)
    for k in ("name", "site", "kind", "location", "note", "status"):
        if a.get(k) is not None:
            setattr(ch, k, str(a.get(k)))
    for k in ("width_mm", "turn_radius_mm", "counter_height_mm"):
        if a.get(k) is not None:
            try:
                setattr(ch, k, int(a.get(k)))
            except (TypeError, ValueError):
                return {"ok": False, "reason": "%s 必须是整数" % k}
    for k in ("low_counter", "height_adjustable", "voice_prompt", "text_prompt",
              "vibration", "sign_service"):
        if a.get(k) is not None:
            setattr(ch, k, bool(a.get(k)))
    if a.get("devices") is not None:
        ch.devices = [str(x) for x in (a.get("devices") or [])]
    if a.get("source"):
        ch.source = str(a.get("source"))
    if not ch.site:
        return {"ok": False, "reason": "缺少考点 site"}
    if ch.kind not in ("smart", "manual", "accessible"):
        return {"ok": False, "reason": "通道类型必须是 smart/manual/accessible"}
    store.ensure_site(ch.site)
    store.save_channel(ch)
    ch.last_check_at = models.now_ts()
    ch.last_check_epoch = models.now_epoch()
    store.save_channel(ch)
    store.log("登记通道", "%s %s" % (ch.code, ch.kind_text()), a.get("operator"))
    v = access.validate_channel(ch)
    return {"ok": True, "channel_id": ch.channel_id, "code": ch.code,
            "check": v, "state_text": ch.state_text()}


def op_check_channels(a):
    import access
    store = _store()
    site = a.get("site")
    if site:
        return {"ok": True, "site": access.site_status(store, site)}
    import config
    cfg = config.load_settings()
    return {"ok": True, "rate": access.channel_config_rate(store, cfg)}


def op_profiles(a):
    store = _store()
    rows = store.list_profiles(limit=int(a.get("limit") or 200),
                               need=a.get("need") or None,
                               site=a.get("site") or None,
                               status=a.get("status") or None)
    return {"ok": True, "profiles": [
        {"profile_id": p.profile_id, "candidate_id": p.candidate_id,
         "candidate_name": p.candidate_name, "room_id": p.room_id,
         "seat_no": p.seat_no, "site": p.site, "needs": p.needs,
         "needs_text": p.needs_text(), "measures": p.measures,
         "channel_hint": p.channel_hint, "consent": p.consent,
         "cert_verified": p.cert_verified, "status": p.status,
         "created_at": p.created_at} for p in rows]}


def op_add_profile(a):
    import access
    store = _store()
    ok, res, extra = access.add_profile(store, a, a.get("operator") or "")
    if not ok:
        return {"ok": False, "reason": res}
    p = res
    return {"ok": True, "profile_id": p.profile_id, "already": extra.get("already"),
            "needs": p.needs, "needs_text": p.needs_text(),
            "measures": p.measures, "channel_hint": p.channel_hint,
            "recommended": extra.get("recommended"), "warn": extra.get("warn"),
            "consent": p.consent, "message": (res if isinstance(res, str) else "")}


def op_measures(a):
    store = _store()
    pid = a.get("profile_id") or ""
    pf = store.get_profile(profile_id=pid)
    if pf is None:
        return {"ok": False, "reason": "档案不存在"}
    rows = store.list_accommodations(profile_id=pid)
    return {"ok": True, "profile_id": pid, "candidate_id": pf.candidate_id,
            "needs": pf.needs, "needs_text": pf.needs_text(),
            "channel_hint": pf.channel_hint,
            "measures": [{"measure": x.measure, "measure_text": x.measure_text(),
                          "status": x.status, "status_text": x.state_text(),
                          "owner": x.owner, "place": x.place,
                          "provided_at": x.provided_at, "note": x.note}
                         for x in rows]}


def op_set_measure(a):
    import access
    store = _store()
    ok, res = access.set_measure_status(
        store, a.get("profile_id") or "", a.get("measure") or "",
        a.get("status") or "", operator=a.get("operator") or "",
        owner=a.get("owner") or "", place=a.get("place") or "",
        note=a.get("note") or "")
    if not ok:
        return {"ok": False, "reason": res}
    return {"ok": True, "acc_id": res.acc_id, "measure": res.measure,
            "status": res.status, "status_text": res.state_text(),
            "provided_at": res.provided_at}


def op_alt_verify(a):
    import verify
    store = _store()
    ok, res, info = verify.alt_verify(store, a, a.get("operator") or "",
                                      notifier=_notifier(store))
    if not ok:
        return {"ok": False, "reason": res}
    av = res
    return {"ok": True, "av_id": av.av_id, "mode": av.mode,
            "mode_text": av.mode_text(), "docs": av.docs,
            "docs_text": info.get("docs_text"), "doc_matched": av.doc_matched,
            "inspector": av.inspector, "witness": av.witness,
            "outcome": av.outcome, "outcome_text": av.outcome_text(),
            "equivalence": av.equivalence, "equivalence_note": info.get("equivalence"),
            "tech_reason": av.tech_reason, "overdue": av.overdue,
            "profile_created": info.get("profile_created"),
            "alt_channel_ok": info.get("alt_channel_ok"),
            "alt_channel_reason": info.get("alt_channel_reason"),
            "block_id": ((info.get("block") or {}).get("block_id") or ""),
            "block_final": ((info.get("block") or {}).get("final_result") or ""),
            "alert_id": av.alert_id, "warn": info.get("warn"),
            "source": "09_无障碍与特殊考生保障"}


def op_list_alt_verify(a):
    store = _store()
    rows = store.list_alt_verifies(limit=int(a.get("limit") or 100),
                                   drill=None if a.get("drill") is None
                                   else bool(a.get("drill")))
    return {"ok": True, "stats": store.alt_verify_stats(),
            "records": [{"av_id": x.av_id, "ts": x.ts, "candidate_id": x.candidate_id,
                         "mode": x.mode, "docs_text": x.docs_text(),
                         "inspector": x.inspector, "witness": x.witness,
                         "outcome": x.outcome, "overdue": x.overdue,
                         "elapsed_sec": x.elapsed_sec, "tech_reason": x.tech_reason,
                         "equivalence": x.equivalence, "alert_id": x.alert_id}
                        for x in rows]}


def op_tech_block(a):
    import verify
    store = _store()
    b = verify.raise_tech_block(store, a, a.get("operator") or "",
                                notifier=_notifier(store))
    if b is None:
        return {"ok": False, "reason": "缺少考生标识"}
    return {"ok": True, "block_id": b.block_id, "symptom_text": b.symptom_text(),
            "escalated": b.escalated, "alert_id": b.alert_id,
            "final_result": b.final_result or "", "resolved": b.resolved,
            "source": "09_无障碍与特殊考生保障"}


def op_list_blocks(a):
    store = _store()
    rows = store.list_tech_blocks(limit=int(a.get("limit") or 100),
                                  resolved=None if a.get("resolved") is None
                                  else bool(a.get("resolved")))
    return {"ok": True, "stats": store.tech_block_stats(),
            "blocks": [{"block_id": b.block_id, "candidate_id": b.candidate_id,
                        "symptom": b.symptom, "symptom_text": b.symptom_text(),
                        "detail": b.detail, "raised_at": b.raised_at,
                        "resolved": b.resolved, "final_result": b.final_result,
                        "final_text": b.final_text(), "waited_sec": b.waited_sec,
                        "resolution": b.resolution, "escalated": b.escalated,
                        "alert_id": b.alert_id} for b in rows]}


def op_resolve_block(a):
    import verify
    store = _store()
    b = verify.resolve_tech_block(store, a.get("block_id") or "",
                                  a.get("final_result") or "",
                                  a.get("resolution") or "",
                                  a.get("operator") or "",
                                  notifier=_notifier(store))
    if b is None:
        return {"ok": False, "reason": "事件不存在或结案类型非法"}
    return {"ok": True, "block_id": b.block_id, "final_result": b.final_result,
            "final_text": b.final_text(), "waited_sec": b.waited_sec,
            "resolved": b.resolved}


def op_fairness_test(a):
    import fairness
    store = _store()
    ok, res, info = fairness.run_fairness(store, a, a.get("operator") or "")
    if not ok:
        return {"ok": False, "reason": res}
    return {"ok": True, "run_id": res.run_id, "total": res.total,
            "dims": res.dims, "groups_count": res.groups_count,
            "max_gap": res.max_gap, "passed": res.passed,
            "worst_dim": res.worst_dim, "worst_group": res.worst_group,
            "action": res.action, "action_note": res.action_note,
            "advice": info.get("advice"), "warn": info.get("warn"),
            "max_gap_allowed": info.get("max_gap_allowed")}


def op_fairness_runs(a):
    import fairness
    store = _store()
    rows = store.list_fairness_runs(limit=int(a.get("limit") or 20))
    return {"ok": True, "stats": fairness.fairness_stats(store),
            "coverage": fairness.coverage(store),
            "trend": fairness.trend(store, limit=6),
            "runs": [{"run_id": r.run_id, "ts": r.ts, "name": r.name,
                      "total": r.total, "max_gap": r.max_gap, "passed": r.passed,
                      "action": r.action, "action_note": r.action_note,
                      "dims": r.dims} for r in rows]}


def op_drill(a):
    import drill
    store = _store()
    ok, res, info = drill.add_drill(store, a, a.get("operator") or "")
    if not ok:
        return {"ok": False, "reason": res}
    return {"ok": True, "drill_id": res.drill_id, "site": res.site,
            "session": res.session, "scenario": res.scenario,
            "scenario_text": res.scenario_text(), "result": res.result,
            "result_text": res.result_text(), "warn": info.get("warn"),
            "availability": drill.drill_availability(store)["rate"]}


def op_list_drills(a):
    import drill
    store = _store()
    rows = store.list_drills(limit=int(a.get("limit") or 100),
                             session=a.get("session") or None,
                             site=a.get("site") or None)
    return {"ok": True, "stats": store.drill_stats(session=a.get("session") or None),
            "availability": drill.drill_availability(store),
            "drills": [{"drill_id": d.drill_id, "ts": d.ts, "site": d.site,
                        "session": d.session, "scenario": d.scenario,
                        "scenario_text": d.scenario_text(), "participants":
                        d.participants, "issues": d.issues, "actions": d.actions,
                        "result": d.result, "result_text": d.result_text()}
                       for d in rows]}


def op_ledger(a):
    import ledger
    store = _store()
    fmt = (a.get("fmt") or "md").lower()
    if fmt not in ("md", "csv", "json"):
        return {"ok": False, "reason": "fmt 必须是 md/csv/json"}
    ok, res = ledger.write_ledger(store, fmt=fmt, site=a.get("site") or None,
                                  desensitize=a.get("desensitize")
                                  if a.get("desensitize") is not None else True,
                                  tag=a.get("tag") or "")
    if not ok:
        return {"ok": False, "reason": res}
    return {"ok": True, "path": res, "rows": len(ledger.rows(store, site=a.get("site")
                                                            or None)),
            "summary": ledger.summary_text(store, site=a.get("site") or None)}


def op_metrics(a):
    import config
    import metrics as metrics_mod
    store = _store()
    m = metrics_mod.compute(store, config.load_settings())
    return {"ok": True, "metrics_ok": m["ok"], "items": m["items"],
            "hard_fail": m["hard_fail"], "text": metrics_mod.text_summary(m),
            "raw": {"channel_rate": m["raw"]["channel_rate"]["rate"],
                    "alt_availability": m["raw"]["alt_availability"]["rate"],
                    "tech_block_denied": m["raw"]["tech_blocks"]["denied"],
                    "measure_coverage": m["raw"]["measure_coverage"]["rate"]}}


def op_report(a):
    import report
    store = _store()
    hours = float(a.get("hours") or 24)
    ok, info = report.write_report(store, hours=hours if a.get("hours") else None,
                                   site=a.get("site") or None)
    if not ok:
        return {"ok": False, "reason": info}
    return {"ok": True, "md": info["md"], "csv": info["csv"],
            "ledger": info["ledger"], "metrics_ok": info["ok_metrics"],
            "hard_fail": info["hard_fail"], "audit_ok": info["audit_ok"],
            "audit_chain": info["audit_chain"]}


def op_linkage(a):
    import linkage_bridge
    if a.get("probe"):
        store = _store()
        r = linkage_bridge.probe(store=store, device_code=a.get("device") or "",
                                 candidate=a.get("candidate") or "",
                                 write=bool(a.get("write")),
                                 operator=a.get("operator") or "09_access")
        return {"ok": True, "probe_ok": r["ok"], "steps": r["steps"],
                "outbox": r.get("outbox") or {}}
    return {"ok": True, "available": linkage_bridge.availability(),
            "summary": linkage_bridge.link_summary(),
            "note": "09 在内存里注册进 00 的总线（不改 00 的文件）；"
                    "推送走 03 智能终端，失败进 outbox 补发"}


def op_flush_outbox(a):
    import linkage_bridge
    store = _store()
    r = linkage_bridge.flush_outbox(store, operator=a.get("operator") or "09_access")
    return {"ok": True, "sent": r["sent"], "failed": r["failed"],
            "stats": r["stats"]}


OPS = {"status": op_status, "channels": op_channels, "save_channel": op_save_channel,
       "check_channels": op_check_channels, "profiles": op_profiles,
       "add_profile": op_add_profile, "measures": op_measures,
       "set_measure": op_set_measure, "alt_verify": op_alt_verify,
       "list_alt_verify": op_list_alt_verify, "tech_block": op_tech_block,
       "list_blocks": op_list_blocks, "resolve_block": op_resolve_block,
       "fairness_test": op_fairness_test, "fairness_runs": op_fairness_runs,
       "drill": op_drill, "list_drills": op_list_drills, "ledger": op_ledger,
       "metrics": op_metrics, "report": op_report, "linkage": op_linkage,
       "flush_outbox": op_flush_outbox}


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
    except Exception as e:                                     # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
