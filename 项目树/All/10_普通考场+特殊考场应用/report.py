# -*- coding: utf-8 -*-
r"""
验收证据报告（两份方案的验收章节）

生成逐考场 / 全考点的验收报告（Markdown + CSV），专治两件事：
  1) **无证据不粉饰**：某指标没有实测数据时，报告里明确写"缺失"，而不是留空或填默认值
  2) **特殊考场差异项单列**：普通考场 6 项 + 差异项（去重/替代通道/分域/离线/最小配置）
     逐条列出达标情况，验收时一眼能看清"特殊考场不降标准"
"""
import os

import config
import kpi as kpi_mod
import models
import privacy as privacy_mod
import rooms as rooms_mod


def build_report(store, site=None, hours=24 * 30, include_rooms=True):
    """汇总报告数据（不落盘）。"""
    import time
    since = models.now_epoch() - float(hours) * 3600.0
    rooms = store.list_rooms(site=site, limit=2000)
    room_rows = []
    for room in rooms:
        run = store.latest_kpi(room.room_id)
        ev = kpi_mod.evaluate(store, room, run=None, persist=False)
        check = store.latest_check(room.room_id)
        room_rows.append({
            "room": room.to_dict(),
            "brief": rooms_mod.room_brief(room),
            "summary": ev["summary"],
            "metrics": ev["metrics"],
            "notes": ev["notes"],
            "latest_check": check.to_dict() if check else None,
            "latest_kpi": run.to_dict() if run else None,
            "template_diff": rooms_mod.template_diff(room.room_type),
        })

    alerts = store.list_alerts(limit=1000)
    alert_rows = [a for a in alerts if a.epoch >= since]
    comp = privacy_mod.compliance_report(store, site=site)
    site_sum = kpi_mod.site_summary(store, site=site)

    notes = [
        "指标口径：行为识别准确率 ≥95%、疑似行为提示率 ≥98.5%、误报率 ≤5%、"
        "告警响应 ≤3 秒、摄像机在线率 ≥99%、覆盖 100% 无盲区、时钟偏差 ≤1 秒。",
        "特殊考场差异项：大教室/机考去重准确率 ≥95%；无障碍替代核验通道强制具备"
        "并有演练记录；隔离/机考/涉密强制独立分区 + 独立授权；涉密全离线可独立运行；"
        "1 机位 + 1 链路可完成巡考并留痕。",
        "误报率只认**复核结论=误报**（不拿「告警数/过检数」冒充）；"
        "无实测证据的指标一律标「缺失」，不填默认值。",
        "本模块不自行做画质算法：机位画质结论来自专题 02，"
        "告警处置与留痕由专题 03 承担（通过 00_集成联动 调用，不改对端代码）。",
    ]
    if not any(r["room"]["last_check_at"] for r in room_rows):
        notes.append("整机实测证据：缺失（尚无考场完成准入自检）。")
    if not any(r["latest_kpi"] for r in room_rows):
        notes.append("验收测算证据：缺失（尚无考场录入验收指标）。")
    return {
        "generated_at": models.now_text(),
        "site": site or config.load_settings().get("site", ""),
        "window_hours": hours,
        "rooms": room_rows,
        "site_summary": site_sum,
        "alerts": {"total": len(alert_rows),
                   "stats": store.alert_stats(),
                   "review": _review_stats(store),
                   "top": store.behavior_stats(limit=10)},
        "compliance": comp,
        "domains": store.partition_stats(),
        "audit": store.verify_chain()[2],
        "notes": notes,
    }


def _review_stats(store):
    import alerting
    return alerting.false_positive_rate(store)


def render_markdown(rep):
    L = []
    L.append("# 普通考场 + 特殊考场应用 · 验收证据报告")
    L.append("")
    L.append("> 生成时间：%s ｜ 考点：%s ｜ 统计窗口：近 %.0f 小时"
             % (rep["generated_at"], rep["site"] or "（未填）", rep["window_hours"]))
    L.append("")
    s = rep["site_summary"]
    L.append("## 一、总览")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 纳入统计的考场 | %d 个 |" % s["rooms"])
    L.append("| 指标全部达标（无缺失） | %d 个 |" % s["fully_ok"])
    L.append("| 存在未达标指标 | %d 个 |" % s["failing"])
    L.append("| 部分缺失/待补证据 | %d 个 |" % s["partial_or_missing"])
    L.append("| 本次窗口内告警 | %d 条（红 %d / 橙 %d / 抑制 %d / 已送终端 %d） |"
             % (rep["alerts"]["stats"]["total"], rep["alerts"]["stats"]["red"],
                rep["alerts"]["stats"]["orange"], rep["alerts"]["stats"]["suppressed"],
                rep["alerts"]["stats"]["delivered"]))
    fpr = rep["alerts"]["review"]
    L.append("| 误报率（复核结论口径） | %s |"
             % ("%.1f%%（%d/%d 条复核）" % (fpr["rate"] * 100, fpr["false_positive"],
                                          fpr["reviewed"])
                if fpr["rate"] is not None else "缺失（尚无复核记录）"))
    L.append("| 审计哈希链 | %s |" % rep["audit"])
    L.append("")

    L.append("## 二、逐考场指标")
    L.append("")
    for r in rep["rooms"]:
        room = r["room"]
        L.append("### %s（%s）" % (room["name"], room["room_type_name"]))
        L.append("")
        L.append("- 状态：%s ｜ 模板：%s ｜ 配置档位：%s ｜ 数据域：%s"
                 % (room["status_text"], room["room_type_name"],
                    room["min_config_name"], room["data_domain_name"]))
        L.append("- 点位：%d 个（必需 %d）｜ 启用行为：%d 种 ｜ 置信度门槛：%.2f"
                 % (len(room["points"]), len([p for p in room["points"]
                                              if p.get("required")]),
                    room["enabled_behavior_count"], room["confidence_min"]))
        ck = r["latest_check"]
        L.append("- 最近准入自检：%s（%s）系数 ×%.2f"
                 % (ck["ts"] if ck else "未做",
                    ("通过" if ck["ok"] else "阻断 %d 项" % len(
                        [d for d in ck["defects"]
                         if d.get("severity") == config.SEV_BLOCK])) if ck else "-",
                    ck["factor"] if ck else 1.0))
        L.append("- 指标：达标 %d / 未达标 %d / 缺失 %d（%s）"
                 % (r["summary"]["meets"], r["summary"]["fails"],
                    r["summary"]["missing"], r["summary"]["verdict"]))
        L.append("")
        L.append("| 指标 | 实测值 | 要求 | 结论 | 证据来源 |")
        L.append("| --- | --- | --- | --- | --- |")
        for m in r["metrics"]:
            val = "" if m.get("value") is None else (
                "%d" % m["value"] if m["key"] in ("alt_channel", "alt_channel_drill",
                                                  "domain_isolation", "offline_ready",
                                                  "min_config")
                else ("%.3f" % m["value"]))
            verdict = ("达标" if m["meets"] is True else
                       "未达标" if m["meets"] is False else "缺失")
            L.append("| %s | %s | %s | %s | %s |"
                     % (m["name"], val or "—", m["target_text"] or "—", verdict,
                        m["source"]))
        L.append("")
        if r["template_diff"]["items"]:
            L.append("相对普通考场的模板差异（%d 项，摘前 8 项）：" %
                     len(r["template_diff"]["items"]))
            L.append("")
            for it in r["template_diff"]["items"][:8]:
                L.append("- %s：%s %s" % (it["field"], it["change"], it["value"]))
            L.append("")
        if ck and ck["defects"]:
            L.append("准入自检未通过项：")
            L.append("")
            for d in ck["defects"]:
                L.append("- [%s] %s%s" % (d.get("severity"), d.get("name"),
                                          ("（%s）" % d.get("detail"))
                                          if d.get("detail") else ""))
            L.append("")

    L.append("## 三、告警与处置")
    L.append("")
    L.append("| 行为 | 级别 | 条数 |")
    L.append("| --- | --- | --- |")
    for b in rep["alerts"]["top"]:
        L.append("| %s | %s | %d |" % (b["name"] or b["behavior"],
                                       b["level"], b["count"]))
    L.append("")
    L.append("复核结论分布：成立 %d、不成立 %d、误报 %d（误报进入算法迭代闭环）"
             % (fpr["confirmed"], fpr["rejected"], fpr["false_positive"]))
    L.append("")

    L.append("## 四、数据分域与合规")
    L.append("")
    comp = rep["compliance"]
    L.append("- 合规体检：%d 个考场，%d 个满足强制项，%d 个存在缺口"
             % (comp["total"], comp["ok"], len(comp["fail"])))
    for row in comp["fail"]:
        L.append("  - **%s**（%s）：缺 %s" % (row["name"], row["room_type_name"],
                                           "、".join(row["fail"])))
    L.append("")
    L.append("| 数据域 | 说明 | 是否隔离 | 允许跨域汇聚 |")
    L.append("| --- | --- | --- | --- |")
    for d in config.DOMAINS:
        rules = config.DOMAIN_RULES[d]
        L.append("| %s | %s | %s | %s |"
                 % (rules["name"], rules["desc"],
                    "是" if rules["isolated"] else "否",
                    "是" if rules["allow_cross_aggregate"] else "否"))
    L.append("")

    L.append("## 五、如实说明")
    L.append("")
    for n in rep["notes"]:
        L.append("- %s" % n)
    L.append("")
    return "\n".join(L)


def write_report(store, site=None, hours=24 * 30):
    """落盘（Markdown + CSV）。"""
    rep = build_report(store, site=site, hours=hours)
    md = render_markdown(rep)
    stamp = models.now_text().replace("-", "").replace(":", "").replace(" ", "_")
    md_path = os.path.join(str(config.REPORT_DIR), "考场验收报告_%s.md" % stamp)
    csv_path = os.path.join(str(config.REPORT_DIR), "考场验收指标_%s.csv" % stamp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("考场,考场类型,指标,实测值,要求,结论,证据来源\n")
        for r in rep["rooms"]:
            for m in r["metrics"]:
                f.write("%s,%s,%s,%s,%s,%s,%s\n" % (
                    _csv(r["room"]["name"]), r["room"]["room_type_name"],
                    _csv(m["name"]),
                    "" if m.get("value") is None else m["value"],
                    _csv(m["target_text"] or ""),
                    "达标" if m["meets"] is True else
                    "未达标" if m["meets"] is False else "缺失",
                    _csv(m["source"])))
    return True, {"md": md_path, "csv": csv_path, "markdown": md,
                  "rooms": len(rep["rooms"])}


def _csv(v):
    s = str(v if v is not None else "")
    if any(c in s for c in ",\"\n"):
        return '"' + s.replace('"', '""') + '"'
    return s
