# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 关键指标

四个**硬指标**（对应《无障碍与特殊考生保障.md》第三节，报告与界面按同一口径算）：
    ① 无障碍通道配置率            100% 考点
    ② 替代核验通道可用性          100%，且每考期至少演练 1 次
    ③ 因技术原因未能入场事件      0 起
    ④ 算法分层测试                覆盖主要分层且差异在可接受范围

另有三个**过程指标**（评审常追问，但不算硬性红线）：
    · 保障措施落实率（措施 -> 已落实）
    · 替代核验及时率（未超过 3 分钟时限的比例）
    · 受阻事件闭环率（受阻 -> 已入场/已结案）
"""
import config
import fairness as fairness_mod


def _item(key, name, value, value_text, ok, target_text, detail="", hard=True):
    return {"key": key, "name": name, "value": value, "value_text": value_text,
            "ok": bool(ok), "target_text": target_text, "detail": detail,
            "level": "hard" if hard else "advisory"}


def compute(store, cfg=None, since_epoch=None):
    """算全部指标；返回 {"ok": 硬指标是否全达标, "items": [...], "raw": {...}}。"""
    import access
    import drill as drill_mod
    cfg = cfg or config.load_settings()
    items, raw = [], {}

    # ① 通道配置率
    cr = access.channel_config_rate(store, cfg)
    raw["channel_rate"] = cr
    items.append(_item(
        "channel_rate", "无障碍通道配置率", cr["rate"],
        "%.0f%%（%d/%d 考点）" % (cr["rate"] * 100, cr["sites_ok"], cr["sites_total"]),
        cr["ok"], "100% 考点配齐三通道",
        detail="；".join("%s 缺 %s" % (d["site"], "、".join(d["missing_text"]))
                        for d in cr["details"] if not d["ok"]) or "全部考点已配齐"))

    # ② 替代通道可用性 + 演练
    dr = drill_mod.drill_availability(store, cfg)
    raw["alt_availability"] = dr
    items.append(_item(
        "alt_availability", "替代核验通道可用性", dr["rate"],
        "%.0f%%（%d/%d 考点）" % (dr["rate"] * 100, dr["sites_ok"], dr["sites_total"]),
        dr["ok"], "100% 可用且每考期演练 ≥1 次",
        detail="；".join("%s：%s" % (d["site"], "、".join(d["reasons"]))
                        for d in dr["details"] if not d["ok"]) or "全部考点可用且已演练"))

    # ③ 因技术原因未能入场
    tb = store.tech_block_stats(since_epoch)
    raw["tech_blocks"] = tb
    items.append(_item(
        "tech_block_denied", "因技术原因未能入场事件", tb["denied"],
        "%d 起（受阻 %d 起，已入场 %d 起）"
        % (tb["denied"], tb["total"], tb["entered"] + tb["entered_manual"]),
        tb["denied"] <= config.TARGET_TECH_BLOCK, "0 起",
        detail=("有 %d 起受阻但已通过替代通道入场（须复盘原因）"
                % (tb["entered"] + tb["entered_manual"])) if tb["total"] else "无受阻事件"))

    # ④ 算法公平性
    fs = fairness_mod.fairness_stats(store)
    raw["fairness"] = fs
    latest = fs.get("latest") or {}
    passed = bool(fs.get("covered")) and bool(latest.get("passed", False))
    gap_text = ("组间最大差 %.2f%%" % (latest.get("max_gap", 0) * 100)) if latest else "尚无测试"
    items.append(_item(
        "fairness", "算法分层测试与差异", latest.get("max_gap", 0.0),
        gap_text + "（样本 %d）" % latest.get("total", 0) if latest else gap_text,
        passed, "覆盖性别/年龄/眼镜/口罩，组间差 ≤%.0f%%"
                % (config.fairness_max_gap(cfg) * 100),
        detail=_fairness_detail(fs, latest, passed)))

    # ---- 过程指标 ----
    cov = access.measure_coverage(store)
    raw["measure_coverage"] = cov
    items.append(_item(
        "measure_coverage", "保障措施落实率", cov["rate"],
        "%.0f%%（%d/%d 项）" % (cov["rate"] * 100, cov["provided"], cov["measures"]),
        cov["measures"] == 0 or cov["rate"] >= 0.95, "≥95%（过程指标）",
        detail="待落实 %d 项，不予 %d 项" % (cov["pending"], cov["rejected"]),
        hard=False))

    av = store.alt_verify_stats(since_epoch)
    raw["alt_verify"] = av
    timely = 1.0 if not av["total"] else (av["total"] - av["overdue"]) / float(av["total"])
    items.append(_item(
        "alt_verify_timely", "替代核验及时率", round(timely, 4),
        "%.0f%%（%d/%d 未超时）" % (timely * 100, av["total"] - av["overdue"], av["total"]),
        av["total"] == 0 or av["overdue"] == 0, "≤%d 秒内完成（过程指标）"
        % config.ALT_VERIFY_SLA_SEC,
        detail="平均耗时 %.0f 秒；替代核验 %d 次" % (av["avg_sec"], av["total"]),
        hard=False))

    closed = 1.0 if not tb["total"] else (tb["total"] - tb["open"]) / float(tb["total"])
    items.append(_item(
        "block_closure", "受阻事件闭环率", round(closed, 4),
        "%.0f%%（%d 起未结案）" % (closed * 100, tb["open"]),
        tb["open"] == 0, "100% 结案（过程指标）",
        detail="平均等待 %.0f 秒，最长 %.0f 秒"
               % (tb["avg_wait_sec"], tb["max_wait_sec"]), hard=False))

    hard = [i for i in items if i["level"] == "hard"]
    return {"ok": all(i["ok"] for i in hard), "items": items, "raw": raw,
            "hard_fail": [i["name"] for i in hard if not i["ok"]],
            "cfg": {"session": cfg.get("session"), "sites": config.sites(cfg)}}


def _fairness_detail(fs, latest, passed):
    """分层测试指标的说明文本：缺覆盖 > 超差未补偿 > 达标（带补偿说明）。"""
    missing = fs.get("missing") or []
    if missing:
        return "未覆盖必测分层：%s" % "、".join(missing)
    if not latest:
        return "尚无分层测试记录（上线前必须完成）"
    note = (latest.get("action_note") or "").strip()
    if passed:
        return "结论达标" + ("（%s）" % note[:40] if note else "")
    return "差异超差，补偿策略：%s" % (note or "未填写（必须补记）")


def text_summary(m):
    """一行行文本（GUI/报告/自检通用）。"""
    lines = []
    for i in m["items"]:
        mark = "达标" if i["ok"] else ("未达标" if i["level"] == "hard" else "待改进")
        lines.append("%s：%s（要求 %s）→ %s"
                     % (i["name"], i["value_text"], i["target_text"], mark))
    return lines
