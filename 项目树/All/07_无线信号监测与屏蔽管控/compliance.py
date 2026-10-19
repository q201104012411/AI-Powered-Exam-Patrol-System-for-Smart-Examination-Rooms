# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 合规体检与验收指标

把专题文档里的"关键技术要求 + 合规红线 + 验收证据"逐条变成**可执行的检查项**，
每项给出 通过 / 需关注 / 不通过 + 依据（数字或记录），不写"符合要求"这种空话。

检查项分三类：
  合规红线：审批报备、功率上限、定向/扇区、仅考试时段开启、结束自动关闭
  技术指标：异常发现时延、监测覆盖、屏蔽有效率、外溢、设备在线、特征库
  证据完备：审批文件、运营商协同、屏蔽效果实测、外溢实测、审计链
"""
import config
import features as features_mod
import locate
import models
import schedule
import shield

EVIDENCE_WINDOW_SEC = 30 * 86400      # 验收证据的有效期：30 天内的实测才算

CATEGORY_TEXT = {"red": "合规红线", "tech": "技术指标", "evidence": "验收证据"}


def _row(code, name, level, detail, category="tech", weight=1):
    return {"code": code, "name": name, "level": level, "detail": detail,
            "category": category, "category_text": CATEGORY_TEXT.get(category, ""),
            "weight": weight}


def checks(store, cfg=None, now=None):
    """跑一遍合规体检。返回 {rows, pass, warn, fail, score, ok, checked_at}。"""
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    site = cfg.get("site") or ""
    rows = []
    jammers = store.list_devices(kind=config.DEV_JAMMER)
    monitors = store.list_devices(kind=config.DEV_MONITOR)
    plan_bands = config.shield_plan(cfg)

    # ---------------- 合规红线 ----------------
    approval, why = store.valid_approval(site, plan_bands)
    rows.append(_row("approval", "设台审批/报备有效", "pass" if approval else "fail",
                     ("%s（%s~%s，批文 %s）" % (approval.no or approval.approval_id,
                                              approval.valid_from, approval.valid_to,
                                              approval.doc or "未登记文件"))
                     if approval else why, "red", 3))
    if approval is not None:
        if not approval.doc:
            rows.append(_row("approval_doc", "审批文件留档", "warn",
                             "审批已登记但未填批文文件/编号，验收时需补齐", "evidence"))
        have = set(approval.bands or [])
        miss = [b for b in plan_bands if have and b not in have]
        rows.append(_row("approval_bands", "审批频段覆盖屏蔽计划",
                         "warn" if miss else "pass",
                         ("未覆盖：%s" % "、".join(config.band_name(b) for b in miss))
                         if miss else "审批频段覆盖计划内 %d 个频段" % len(plan_bands),
                         "evidence"))

    coords = store.list_approvals(kind=config.APPR_COORDINATION, site=site)
    coord_ok = [a for a in coords if a.valid_today(models.today_str())]
    rows.append(_row("coordination", "与运营商协同记录", "pass" if coord_ok else "warn",
                     ("%s（%s~%s）" % (coord_ok[0].no or coord_ok[0].approval_id,
                                      coord_ok[0].valid_from, coord_ok[0].valid_to))
                     if coord_ok else "未登记与运营商的协同记录（红线要求提前告知并取得配合）",
                     "red", 2))

    over_power = [j for j in jammers if float(j.power_w or 0) > shield.power_cap(cfg) + 1e-6]
    rows.append(_row("power", "屏蔽功率不超上限", "warn" if over_power else "pass",
                     ("超限设备：%s（上限 %.1fW，开机时会被压到上限）"
                      % ("、".join("%s=%.1fW" % (j.code, j.power_w) for j in over_power),
                         shield.power_cap(cfg))) if over_power
                     else "全部 %d 台设备在 %.1fW 以内"
                          % (len(jammers), shield.power_cap(cfg)), "red", 2))

    no_dir = [j for j in jammers if j.direction is None or float(j.direction) < 0]
    wide = [j for j in jammers if 0 < float(j.sector_width or 0) > shield.sector_cap(cfg)]
    if not jammers:
        rows.append(_row("direction", "定向可控", "warn", "尚未登记屏蔽器", "red"))
    elif no_dir:
        rows.append(_row("direction", "定向可控", "fail",
                         "未设置方位角：%s" % "、".join(j.code for j in no_dir), "red", 3))
    elif wide:
        rows.append(_row("direction", "定向可控", "warn",
                         "扇区过宽：%s" % "、".join(
                             "%s=%.0f°" % (j.code, j.sector_width) for j in wide), "red"))
    else:
        rows.append(_row("direction", "定向可控", "pass",
                         "全部设备已设定方位角，扇区 ≤ %.0f°" % shield.sector_cap(cfg),
                         "red"))

    cons = schedule.consistency(store, now=now, cfg=cfg)
    rows.append(_row("switch_consistency", "开关记录与考试时间表一致",
                     "fail" if cons["fail"] else ("warn" if cons["warn"] else "pass"),
                     ("不通过 %d 条、需关注 %d 条（已结束场次 %d 场）"
                      % (cons["fail"], cons["warn"], cons["checked_sessions"]))
                     if (cons["fail"] or cons["warn"]) else
                     "开关记录与 %d 个场次窗口一致" % cons["checked_sessions"],
                     "red", 3))

    if not cfg.get("auto_off", True):
        rows.append(_row("auto_off", "考试结束自动关闭", "warn",
                         "自动关闭被关闭，结束后需人工关闭（一致性核对会暴露超时）",
                         "red", 2))
    else:
        rows.append(_row("auto_off", "考试结束自动关闭", "pass",
                         "已启用：窗口结束自动关闭并留痕", "red"))

    # ---------------- 技术指标 ----------------
    a = store.anomaly_stats()
    p95 = store.latency_percentile(0.95)
    if a["latency_n"] >= 3:
        lvl = "pass" if p95 <= config.TARGET_DISCOVERY_SEC else "fail"
        detail = ("P95 %.1f 秒（%d 条样本，目标 ≤ %.0f 秒）"
                  % (p95, a["latency_n"], config.TARGET_DISCOVERY_SEC))
    elif a["latency_n"] > 0:
        lvl = "pass" if p95 <= config.TARGET_DISCOVERY_SEC else "fail"
        detail = "样本仅 %d 条：P95 %.1f 秒（样本不足，建议补回放）" % (a["latency_n"], p95)
    else:
        lvl = "warn"
        detail = "尚无带时延的异常样本（跑一次回放测试即可产生）"
    rows.append(_row("latency", "异常发现时延", lvl, detail, "tech", 2))

    cov = locate.coverage(store)
    if cov["rate"] >= 0.95:
        lvl = "pass"
    elif cov["rate"] >= 0.80:
        lvl = "warn"
    else:
        lvl = "fail"
    rows.append(_row("coverage", "监测覆盖（考点全域 + 周边 %.0f 米）"
                     % cov["margin_m"], lvl,
                     "覆盖率 %.0f%%（%d/%d 个采样点，%d 个监测站，半径 %.0f 米）"
                     % (cov["rate"] * 100, cov["covered"], cov["points"],
                        cov["stations"], cov["radius_m"]), "tech", 2))

    rate, n_tests = store.shield_rate()
    tests = store.list_shield_tests(limit=2000)
    min_atten = min([float(t.attenuation_db or 0) for t in tests], default=0.0)
    if n_tests == 0:
        rows.append(_row("shield_rate", "屏蔽有效率", "warn",
                         "尚无屏蔽效果实测（目标 ≥ %.0f%%，衰减 ≥ %.0f dB）"
                         % (config.TARGET_SHIELD_RATE * 100, config.TARGET_ATTEN_DB),
                         "tech", 2))
    else:
        lvl = "pass" if (rate >= config.TARGET_SHIELD_RATE
                         and min_atten >= config.TARGET_ATTEN_DB) else "fail"
        rows.append(_row("shield_rate", "屏蔽有效率", lvl,
                         "平均有效率 %.0f%%（%d 次实测），最小衰减 %.1f dB"
                         % (rate * 100, n_tests, min_atten), "tech", 2))

    # ---------------- 验收证据 ----------------
    spills = [t for t in store.list_spills(limit=2000)
              if t.ts_epoch >= now - EVIDENCE_WINDOW_SEC]
    bad = [t for t in spills if t.result != "pass"]
    if not spills:
        rows.append(_row("spill", "外溢实测（周边合法通信）", "warn",
                         "尚无 30 天内的外溢实测记录（限值 %.0f dBm）"
                         % models.to_float(cfg.get("spill_limit_dbm"),
                                           config.TARGET_SPILL_DBM), "evidence", 2))
    else:
        rows.append(_row("spill", "外溢实测（周边合法通信）",
                         "fail" if bad else "pass",
                         ("超限 %d 次：%s" % (len(bad), "、".join(
                             "%s %.1f dBm" % (t.point or "-", t.measured_dbm)
                             for t in bad[:4]))) if bad else
                         "%d 次实测全部 ≤ %.0f dBm"
                         % (len(spills), models.to_float(
                             cfg.get("spill_limit_dbm"), config.TARGET_SPILL_DBM)),
                         "evidence", 2))

    if not tests:
        rows.append(_row("shield_evidence", "屏蔽效果实测证据", "warn",
                         "尚无屏蔽前后场强实测（需按考场/频段补测）", "evidence"))
    else:
        rows.append(_row("shield_evidence", "屏蔽效果实测证据", "pass",
                         "%d 次实测（覆盖 %d 个考场/频段组合）"
                         % (len(tests), len({(t.room_id, t.band) for t in tests})),
                         "evidence"))

    fs = features_mod.summary(store)
    rows.append(_row("features", "作弊信号特征库", "pass" if fs["cheat"] >=
                     config.TARGET_FEATURE_MIN else "warn",
                     "作弊/可疑特征 %d 条（最少 %d 条），合法特征 %d 条"
                     % (fs["cheat"], config.TARGET_FEATURE_MIN, fs["legal"]),
                     "tech"))

    if not monitors:
        rows.append(_row("monitors", "监测站部署", "fail", "尚未登记固定监测站", "tech", 2))
    else:
        off = [m for m in monitors if m.status != config.DEV_ONLINE]
        rows.append(_row("monitors", "监测站在线", "warn" if off else "pass",
                         ("离线/故障：%s" % "、".join(m.code for m in off)) if off
                         else "%d 个监测站在线" % len(monitors), "tech"))

    if not jammers:
        rows.append(_row("jammers", "屏蔽器部署", "warn", "尚未登记屏蔽器", "tech", 2))
    else:
        off = [j for j in jammers if j.status != config.DEV_ONLINE]
        rows.append(_row("jammers", "屏蔽器在线", "warn" if off else "pass",
                         ("离线/故障：%s" % "、".join(j.code for j in off)) if off
                         else "%d 台屏蔽器在线" % len(jammers), "tech"))

    missing_groups = []
    groups = {config.BAND_INDEX[b]["group"] for b in plan_bands if b in config.BAND_INDEX}
    for g, need in (("cellular", "2G/3G/4G/5G"), ("shortrange", "WiFi/蓝牙")):
        if g not in groups:
            missing_groups.append(need)
    rows.append(_row("band_plan", "屏蔽频段覆盖 2G/3G/4G/5G/WiFi/蓝牙",
                     "fail" if missing_groups else "pass",
                     ("缺少：%s" % "、".join(missing_groups)) if missing_groups
                     else "计划覆盖 %d 个频段（含移动通信与短距/对讲）" % len(plan_bands),
                     "red"))

    today_sessions = [s for s in store.list_sessions(limit=500)
                      if s.date >= models.today_str()]
    rows.append(_row("timetable", "考试时间表", "pass" if today_sessions else "warn",
                     "已登记 %d 个（今日及以后）场次" % len(today_sessions)
                     if today_sessions else "尚无今日及以后的场次，无法自动开关",
                     "evidence"))

    chain_ok, n_chain, chain_msg = store.verify_chain()
    rows.append(_row("audit", "审计链完整", "pass" if chain_ok else "fail",
                     chain_msg, "evidence", 2))

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in rows:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    penalty = sum(r["weight"] for r in rows if r["level"] == "fail") * 8 \
        + sum(r["weight"] for r in rows if r["level"] == "warn") * 2
    score = max(0, min(100, 100 - penalty))
    return {"rows": rows, "pass": counts.get("pass", 0), "warn": counts.get("warn", 0),
            "fail": counts.get("fail", 0), "score": score,
            "ok": counts.get("fail", 0) == 0,
            "checked_at": models.now_ts(),
            "summary_text": "通过 %d 项 / 需关注 %d 项 / 不通过 %d 项，合规分 %d"
                            % (counts.get("pass", 0), counts.get("warn", 0),
                               counts.get("fail", 0), score)}


def metrics(store, cfg=None, now=None):
    """验收指标汇总（报告/接口共用，键名稳定）。"""
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    a = store.anomaly_stats()
    rate, n_tests = store.shield_rate()
    tests = store.list_shield_tests(limit=5000)
    spills = store.list_spills(limit=5000)
    cov = locate.coverage(store)
    cons = schedule.consistency(store, now=now, cfg=cfg)
    return {
        "discovery_target_sec": config.TARGET_DISCOVERY_SEC,
        "avg_latency_sec": a["avg_latency"],
        "p95_latency_sec": store.latency_percentile(0.95),
        "max_latency_sec": a["max_latency"],
        "latency_samples": a["latency_n"],
        "coverage_rate": cov["rate"],
        "coverage_points": cov["points"],
        "coverage_covered": cov["covered"],
        "monitor_stations": cov["stations"],
        "shield_rate": rate,
        "shield_tests": n_tests,
        "shield_min_atten_db": min([float(t.attenuation_db or 0) for t in tests],
                                   default=0.0),
        "spill_tests": len(spills),
        "spill_pass": sum(1 for t in spills if t.result == "pass"),
        "spill_limit_dbm": models.to_float(cfg.get("spill_limit_dbm"),
                                           config.TARGET_SPILL_DBM),
        "anomalies": a["total"],
        "open_anomalies": a["open"],
        "critical": a["critical"],
        "switch_fail": cons["fail"],
        "switch_warn": cons["warn"],
        "approval_ok": bool(store.valid_approval(cfg.get("site") or "",
                                                config.shield_plan(cfg))[0]),
        "audit_ok": store.verify_chain()[0],
    }


def describe():
    return {"检查项": "合规红线 / 技术指标 / 验收证据 三类",
            "证据有效期": "%d 天" % (EVIDENCE_WINDOW_SEC // 86400),
            "评分": "不通过项按权重 ×8 扣分，需关注 ×2 扣分"}
