# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 演练与替代通道可用性

指标口径（《无障碍与特殊考生保障.md》第三节）：
    替代核验通道可用性 100%，且**每考期至少演练 1 次**。

为什么演练要算指标：替代通道平时没人走，真出事那天才第一次用，大概率是堵人的。
演练记录必须留下"发现了什么问题、怎么整改"，否则就是走过场——所以代码里
"有问题的演练必须写整改措施"、"不合格必须重演"都是硬校验。
"""
import config
import models


def add_drill(store, payload, operator=""):
    """登记一次演练。返回 (ok, Drill|reason, info)。"""
    payload = dict(payload or {})
    cfg = config.load_settings()
    scenario = str(payload.get("scenario") or "alt_verify")
    if scenario not in config.DRILL_SCENARIOS:
        return False, "演练科目非法：%s" % scenario, {}
    site = str(payload.get("site") or cfg.get("site") or "").strip()
    if not site:
        return False, "未填考点", {}
    result = str(payload.get("result") or config.DRILL_PASS)
    if result not in config.DRILL_RESULTS:
        return False, "演练结论非法：%s" % result, {}
    issues = [str(x) for x in (payload.get("issues") or []) if str(x).strip()]
    actions = str(payload.get("actions") or "").strip()
    if issues and not actions:
        return False, "演练发现了问题就必须写整改措施（否则等于没演练）", {}
    if result == config.DRILL_FAIL and not actions:
        return False, "不合格的演练必须写重演/整改计划", {}
    participants = int(payload.get("participants") or 0)
    if participants <= 0:
        return False, "演练参与人数必须 ≥1", {}
    planned = float(payload.get("planned_min") or 0.0)
    actual = float(payload.get("actual_min") or 0.0)
    warn = []
    if actual and actual < config.DRILL_TARGET_MIN:
        warn.append("演练仅 %.0f 分钟（建议 ≥%d 分钟，太短演不出问题）"
                    % (actual, config.DRILL_TARGET_MIN))
    chans = [str(c) for c in (payload.get("channels") or []) if str(c).strip()]
    unknown = [c for c in chans if store.get_channel(code=c) is None]
    if unknown:
        warn.append("以下通道编码不在通道台账里：%s" % "、".join(unknown))
    if scenario == "full_chain":
        kinds = {store.get_channel(code=c).kind for c in chans
                 if store.get_channel(code=c)}
        missing = [k for k in config.CHANNEL_KINDS if k not in kinds]
        if missing:
            warn.append("全链路演练未覆盖：%s"
                        % "、".join(config.CHANNEL_TEXT[k] for k in missing))

    d = models.Drill(
        session=str(payload.get("session") or cfg.get("session") or ""),
        site=site, scenario=scenario, channels=chans, participants=participants,
        planned_min=planned, actual_min=actual, issues=issues, actions=actions,
        result=result, witness=str(payload.get("witness") or ""),
        operator=operator, source=str(payload.get("source") or config.MANUAL_SOURCE),
        note=str(payload.get("note") or ""))
    store.save_drill(d)
    store.log("替代通道演练", "%s %s 结论=%s 参与 %d 人"
              % (site, config.DRILL_SCENARIOS.get(scenario, scenario),
                 config.DRILL_RESULT_TEXT.get(result, result), participants),
              operator)
    return True, d, {"warn": warn}


def drill_availability(store, cfg=None):
    """
    替代核验通道可用性（每考期至少演练 1 次 + 考点有可用的替代通道）。
    返回逐考点的明细，便于在界面/报告里指出"哪个考点还没演练"。
    """
    import access
    cfg = cfg or config.load_settings()
    session = str(cfg.get("session") or "")
    sites = config.sites(cfg)
    details, ok_sites = [], []
    for s in sites:
        st = access.site_status(store, s)
        drills = store.list_drills(limit=1000, session=session, site=s)
        n = len(drills)
        failed = [d for d in drills if d.result == config.DRILL_FAIL]
        ok = bool(st["alt_available"]) and n >= config.TARGET_DRILL_PER_SESSION \
            and not failed
        if ok:
            ok_sites.append(s)
        reasons = []
        if not st["alt_available"]:
            reasons.append("无合格可用的替代通道（%s）"
                           % (access.alt_channel_available(store, s)[1][:60]))
        if n < config.TARGET_DRILL_PER_SESSION:
            reasons.append("本考期（%s）未演练" % (session or "-"))
        if failed:
            reasons.append("有不合格演练 %d 次，须重演" % len(failed))
        details.append({"site": s, "alt_available": st["alt_available"],
                        "drills": n, "failed_drills": len(failed),
                        "last_drill": (drills[0].ts if drills else ""),
                        "scenarios": sorted({d.scenario for d in drills}),
                        "ok": ok, "reasons": reasons})
    rate = (len(ok_sites) / float(len(sites))) if sites else 0.0
    return {"sites_total": len(sites), "sites_ok": len(ok_sites),
            "rate": round(rate, 4), "ok": rate >= config.TARGET_ALT_AVAILABILITY,
            "session": session, "details": details, "ok_sites": ok_sites,
            "target": "替代通道可用性 100%，且每考期至少演练 1 次"}
