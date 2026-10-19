# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 验收报告

报告不是"把界面截图再抄一遍"，而是**逐条对着专题文档的验收证据要求**给结论：

    屏蔽效果实测（各频段场强、屏蔽有效率）
    外溢测试（考点外围合法通信可用性）
    审批/报备文件
    开关时段记录与考试时间表一致性核对
    + 异常发现时延、监测覆盖、特征库、审计链、回放指标

每一条都带"依据"（数字/记录/文件名）；证据缺失写"待补"，不写"符合要求"。
输出 data\reports\无线信号监测与屏蔽管控_验收报告_YYYYMMDD_HHMMSS.md / .csv。
"""
import time

import config
import compliance
import models
import schedule


def _md_table(headers, rows):
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "／") for c in r) + " |")
    return "\n".join(out)


def _result_text(ok):
    return "通过" if ok else "不通过"


def _window_text(epoch):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(epoch))


def _metric_rows(m, replay):
    rows = [
        ["异常发现时延（P95）", "≤ %.0f 秒" % m["discovery_target_sec"],
         ("%.1f 秒（%d 条样本）" % (m["p95_latency_sec"], m["latency_samples"]))
         if m["latency_samples"] else "暂无样本",
         _result_text(m["latency_samples"] > 0
                      and m["p95_latency_sec"] <= m["discovery_target_sec"])
         if m["latency_samples"] else "待补"],
        ["监测覆盖（考点+周边 %.0f 米）" % config.TARGET_COVERAGE_MARGIN_M, "100%",
         "%.0f%%（%d/%d 采样点，%d 站）"
         % (m["coverage_rate"] * 100, m["coverage_covered"],
            m["coverage_points"], m["monitor_stations"]),
         _result_text(m["coverage_rate"] >= 0.95)],
        ["屏蔽有效率", "≥ %.0f%%" % (config.TARGET_SHIELD_RATE * 100),
         ("%.0f%%（%d 次实测，最小衰减 %.0f dB）"
          % (m["shield_rate"] * 100, m["shield_tests"], m["shield_min_atten_db"]))
         if m["shield_tests"] else "暂无实测",
         _result_text(m["shield_tests"] > 0
                      and m["shield_rate"] >= config.TARGET_SHIELD_RATE
                      and m["shield_min_atten_db"] >= config.TARGET_ATTEN_DB)
         if m["shield_tests"] else "待补"],
        ["外溢（周边合法通信）", "≤ %.0f dBm" % m["spill_limit_dbm"],
         ("%d 次实测，%d 次通过" % (m["spill_tests"], m["spill_pass"]))
         if m["spill_tests"] else "暂无实测",
         _result_text(m["spill_tests"] > 0 and m["spill_pass"] == m["spill_tests"])
         if m["spill_tests"] else "待补"],
        ["开关记录与时间表一致", "无不一致",
         "不通过 %d / 需关注 %d" % (m["switch_fail"], m["switch_warn"]),
         _result_text(m["switch_fail"] == 0)],
        ["设台审批/报备", "当天有效", "有效" if m["approval_ok"] else "缺失/过期",
         _result_text(m["approval_ok"])],
        ["审计链", "完整", "完整" if m["audit_ok"] else "断裂",
         _result_text(m["audit_ok"])],
    ]
    if replay and replay.get("total"):
        rows.append(["回放检出率（模拟数据）", "≥ 99%",
                     "%.0f%%（%d 个场景，误报 %d）"
                     % (float(replay.get("detect_rate") or 0) * 100,
                        replay.get("total"), replay.get("false_alarm") or 0),
                     _result_text(float(replay.get("detect_rate") or 0) >= 0.99
                                  and (replay.get("false_alarm") or 0) == 0)])
        rows.append(["回放定位误差（模拟数据）", "≤ 15 米",
                     "平均 %.1f 米 / 最大 %.1f 米"
                     % (float((replay.get("metrics") or {}).get(
                         "locate_error_avg_m") or 0),
                        float((replay.get("metrics") or {}).get(
                            "locate_error_max_m") or 0)),
                     _result_text(float((replay.get("metrics") or {}).get(
                         "locate_error_max_m") or 0) <= 15.0)])
    return rows


def write_report(store, site=None, hours=24 * 30, operator="", cfg=None):
    """生成验收报告（md + csv）。返回 (ok, {md, csv, metrics, conclusion})。"""
    cfg = cfg or config.load_settings()
    site = site or cfg.get("site") or ""
    now = models.now_epoch()
    since = now - float(hours) * 3600
    m = compliance.metrics(store, cfg, now=now)
    chk = compliance.checks(store, cfg, now=now)
    replay = store.latest_run()
    replay_d = replay.to_dict() if replay else None
    if replay_d:
        replay_d["items"] = [it.to_dict() for it in store.list_items(replay.run_id)]
    anomalies = store.list_anomalies(since_epoch=since, limit=200)
    shield_tests = store.list_shield_tests(since_epoch=since, limit=500)
    spills = store.list_spills(since_epoch=since, limit=500)
    approvals = store.list_approvals(limit=200)
    switches = store.list_switches(since_epoch=since, limit=500)
    sessions = store.list_sessions(limit=500)
    cons = schedule.consistency(store, now=now, cfg=cfg)
    locs = store.list_locations(limit=100)
    corrs = store.list_correlations(limit=100)
    stats = store.summary(since_epoch=since)
    chain_ok, chain_n, chain_msg = store.verify_chain()

    metrics_rows = _metric_rows(m, replay_d)
    failed = [r for r in metrics_rows if r[3] == "不通过"]
    pending = [r for r in metrics_rows if r[3] == "待补"]
    if failed:
        conclusion = "不通过：%d 项指标未达标（%s）" % (
            len(failed), "、".join(r[0] for r in failed))
    elif pending:
        conclusion = "有条件通过：%d 项证据待补（%s）" % (
            len(pending), "、".join(r[0] for r in pending))
    else:
        conclusion = "通过：验收指标全部达标，证据齐备"

    lines = []
    lines.append("# %s" % config.REPORT_TITLE)
    lines.append("")
    lines.append("> 考点：**%s** ｜ 生成时间：%s ｜ 统计区间：近 %.0f 小时"
                 % (site or "未指定", models.ts_text(now), float(hours)))
    lines.append("> 数据目录：`%s`" % config.DATA_ROOT)
    lines.append("> 数据来源：真实设备帧会标 `source=real`；本次统计含模拟帧 %d 条"
                 "（界面/接口中如实标注，不冒充真实监测）"
                 % sum(1 for s in store.list_scans(since_epoch=since, limit=10000)
                       if s.source == "sim"))
    lines.append("")
    lines.append("## 一、验收结论")
    lines.append("")
    lines.append("**%s**" % conclusion)
    lines.append("")
    lines.append("- 设备：监测站 %d 个 / 手持侦测仪 %d 个 / 屏蔽器 %d 个"
                 % (stats["monitors"], stats["probes"], stats["jammers"]))
    lines.append("- 运行：扫描 %d 帧、异常 %d 条（待处置 %d）、开关记录 %d 条、场次 %d 场"
                 % (stats["scans"], stats["anomalies"], stats["open_anomalies"],
                    stats["switch_logs"], stats["sessions"]))
    lines.append("- 合规体检：%s" % chk["summary_text"])
    lines.append("- 审计链：%s" % chain_msg)
    lines.append("")
    lines.append("## 二、验收指标")
    lines.append("")
    lines.append(_md_table(("指标", "目标", "实测", "结论"), metrics_rows))
    lines.append("")
    lines.append("## 三、合规红线体检")
    lines.append("")
    chk_rows = []
    level_text = {"pass": "通过", "warn": "需关注", "fail": "不通过"}
    for r in chk["rows"]:
        chk_rows.append([r["category_text"], r["name"], level_text.get(r["level"], r["level"]),
                         r["detail"]])
    lines.append(_md_table(("类别", "检查项", "结论", "依据"), chk_rows))
    lines.append("")
    lines.append("## 四、屏蔽效果实测（考场内各频段场强与屏蔽有效率）")
    lines.append("")
    if shield_tests:
        lines.append(_md_table(
            ("考场", "频段", "屏蔽前 dBm", "屏蔽后 dBm", "衰减 dB", "有效率", "结论", "实测时间"),
            [[t.room_id or "-", config.band_name(t.band), "%.1f" % t.before_dbm,
              "%.1f" % t.after_dbm, "%.1f" % t.attenuation_db,
              "%.0f%%" % (t.rate * 100), "通过" if t.result == "pass" else "未达标",
              (t.measured_at or "")[5:16]] for t in shield_tests]))
    else:
        lines.append("**尚无实测记录**（需按考场/频段补测，验收时必须提供）。")
    lines.append("")
    lines.append("## 五、外溢测试（考点外围合法通信可用性）")
    lines.append("")
    if spills:
        lines.append(_md_table(
            ("测试点", "距考场 m", "频段", "实测 dBm", "限值 dBm", "结论", "实测时间"),
            [[t.point or "-", "%.0f" % t.distance_m, config.band_name(t.band),
              "%.1f" % t.measured_dbm, "%.0f" % t.limit_dbm,
              "通过" if t.result == "pass" else "超限", (t.measured_at or "")[5:16]]
             for t in spills]))
    else:
        lines.append("**尚无外溢实测记录**（红线要求验证考点外围合法通信可用）。")
    lines.append("")
    lines.append("## 六、审批/报备与运营商协同")
    lines.append("")
    if approvals:
        lines.append(_md_table(
            ("类型", "编号", "机构", "考点", "有效期", "状态", "文件/依据"),
            [[a.kind_text(), a.no or a.approval_id, a.org or "-", a.site or "-",
              "%s~%s" % (a.valid_from or "-", a.valid_to or "-"), a.status_text(),
              a.doc or "未登记"] for a in approvals]))
    else:
        lines.append("**未登记审批/报备**：依法不得开启屏蔽（演练除外）。")
    lines.append("")
    lines.append("## 七、开关记录与考试时间表一致性")
    lines.append("")
    lines.append("- 已结束场次：%d 场；开关区间：%d 段；不一致：不通过 %d 条、需关注 %d 条"
                 % (cons["checked_sessions"], cons["intervals"], cons["fail"], cons["warn"]))
    if cons["rows"]:
        lines.append("")
        lines.append(_md_table(("级别", "问题", "设备", "说明", "时间"),
                               [["不通过" if r["level"] == "fail" else "需关注",
                                 r["kind"], r.get("device_id", "-"),
                                 r["detail"], r.get("at", "-")] for r in cons["rows"][:60]]))
    lines.append("")
    lines.append("### 场次与开关明细")
    lines.append("")
    if sessions:
        lines.append(_md_table(
            ("日期", "场次", "时间", "开考前置", "状态"),
            [[s.date, s.name, "%s~%s" % (s.start, s.end),
              "%d 分钟" % (int(s.pre_open_sec or 0) // 60), s.state_text()]
             for s in sessions[:40]]))
    if switches:
        lines.append("")
        lines.append(_md_table(
            ("时间", "设备", "动作", "频段", "功率 W", "来源", "操作人", "原因"),
            [[(s.ts or "")[5:16], s.device_id, s.action_text(),
              "、".join(config.band_name(b) for b in (s.bands or [])),
              "%.2f" % s.power_w, s.source_text(), s.operator or "-",
              (s.reason or "-")[:40]] for s in switches[:80]]))
    if not switches:
        lines.append("本次统计区间内没有开关记录（未开启或尚未到考试窗口）。")
    lines.append("")
    lines.append("## 八、异常信号清单")
    lines.append("")
    if anomalies:
        lines.append(_md_table(
            ("发现时间", "站", "类型", "频率", "带宽 MHz", "峰值 dBm", "时延 s",
             "严重度", "状态", "定位"),
            [[(a.created_at or "")[5:16], a.station_code, a.kind_text(),
              a.frequency_text(), "%.3f" % a.bandwidth_mhz, "%.1f" % a.peak_dbm,
              "%.1f" % a.latency_sec, a.severity_text(), a.status_text(),
              a.located_text()] for a in anomalies[:60]]))
    else:
        lines.append("统计区间内没有异常（或尚未跑回放测试）。")
    lines.append("")
    lines.append("## 九、定位记录")
    lines.append("")
    if locs:
        lines.append(_md_table(
            ("时间", "异常", "坐标 x", "坐标 y", "误差半径 m", "可信度", "方法"),
            [[models.ts_text(l.created_epoch)[5:16], l.anomaly_id,
              "%.1f" % l.x, "%.1f" % l.y, "%.0f" % l.radius_m,
              "%.2f" % l.confidence, l.method] for l in locs[:40]]))
    else:
        lines.append("尚无定位记录。")
    lines.append("")
    lines.append("## 十、联动记录")
    lines.append("")
    if corrs:
        lines.append(_md_table(("时间", "来源", "引用", "考场", "说明"),
                               [[models.ts_text(c.created_epoch)[5:16], c.source,
                                 c.ref_id, c.room_id or "-", c.detail]
                                for c in corrs[:40]]))
    else:
        lines.append("尚无跨模块联动记录（可执行「联动核查 06 安检结论」）。")
    lines.append("")
    lines.append("## 十一、回放测试（模拟数据，仅验证算法链）")
    lines.append("")
    if replay_d:
        lines.append("- 运行：%s ｜ 场景 %d ｜ 检出率 %.0f%% ｜ 误报 %d ｜ 平均时延 %.1fs ｜ "
                     "最大时延 %.1fs"
                     % (replay_d.get("created_at", ""), replay_d.get("total", 0),
                        float(replay_d.get("detect_rate") or 0) * 100,
                        replay_d.get("false_alarm") or 0,
                        float(replay_d.get("avg_latency") or 0),
                        float(replay_d.get("max_latency") or 0)))
        items = replay_d.get("items") or []
        if items:
            lines.append("")
            lines.append(_md_table(
                ("场景", "名称", "期望", "实际", "时延 s", "结论", "说明"),
                [[it["scenario_id"], it["name"], it["expected"], it["detected"],
                  "%.1f" % float(it["latency_sec"] or 0),
                  "通过" if it["ok"] else "不通过", (it["detail"] or "")[:60]]
                 for it in items]))
    else:
        lines.append("尚未运行回放测试（界面「异常与定位」页或接口 `replay_test` 可跑）。")
    lines.append("")
    lines.append("## 十二、待补证据清单")
    lines.append("")
    todo = []
    for r in chk["rows"]:
        if r["level"] in ("warn", "fail") and r["category"] == "evidence":
            todo.append("- %s：%s" % (r["name"], r["detail"]))
    for r in chk["rows"]:
        if r["level"] == "fail" and r["category"] != "evidence":
            todo.append("- **（红线）%s**：%s" % (r["name"], r["detail"]))
    if not todo:
        todo.append("- 无（证据齐备）")
    lines.extend(todo)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 本报告由 `07_无线信号监测与屏蔽管控/report.py` 生成；"
                 "所有数字来自本模块数据库，模拟数据均标注 `source=sim`。"
                 "屏蔽设备的实际发射与外部场强必须由具备资质的机构现场实测确认。")

    # 写文件
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    base = "无线信号监测与屏蔽管控_验收报告_%s" % stamp
    md_path = config.REPORT_DIR / (base + ".md")
    csv_path = config.REPORT_DIR / (base + ".csv")
    try:
        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return False, "写报告失败：%s" % e
    # CSV：指标 + 合规体检 + 开关一致性问题
    import csv
    try:
        with open(str(csv_path), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["类别", "项目", "指标/结论", "依据"])
            for r in metrics_rows:
                w.writerow(["验收指标", r[0], r[2], "目标 %s；%s" % (r[1], r[3])])
            for r in chk["rows"]:
                w.writerow(["合规体检", r["name"],
                            {"pass": "通过", "warn": "需关注",
                             "fail": "不通过"}.get(r["level"], r["level"]),
                            r["detail"]])
            for r in cons["rows"]:
                w.writerow(["时间表一致性", r["kind"],
                            "不通过" if r["level"] == "fail" else "需关注", r["detail"]])
    except OSError as e:
        return False, "写 CSV 失败：%s" % e

    store.log("生成验收报告", "%s（%s）" % (md_path.name, conclusion), operator)
    return True, {"md": str(md_path), "csv": str(csv_path),
                  "metrics": m, "compliance": chk, "conclusion": conclusion,
                  "metrics_rows": metrics_rows}


def describe():
    return {"报告位置": str(config.REPORT_DIR),
            "内容": "验收指标/合规红线/屏蔽实测/外溢/审批/开关一致性/"
                    "异常清单/定位/联动/回放/待补证据"}
