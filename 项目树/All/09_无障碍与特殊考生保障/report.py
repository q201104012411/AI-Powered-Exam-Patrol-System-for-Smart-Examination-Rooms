# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 验收证据报告

产出一份**评审能直接看**的报告（Markdown）+ 两份明细（指标 CSV / 台账 CSV）：
  概览 -> 关键指标达标表 -> 三通道配置 -> 特殊考生档案与措施 -> 替代核验（含等效性声明）
  -> 因技术原因受阻事件 -> 算法公平性分层测试 -> 演练 -> 联动留痕 -> 附录（口径与如实说明）

如实说明写死在报告里：
  · 通道尺寸、低位终端高度、语音/文字提示均为**现场实测录入值**，不是系统推算；
  · 没有真实设备时用演示数据，来源标 demo，报告里明确写出"演示"；
  · 因技术原因未入场事件为 0 才算达标，有则**原样列出**（不粉饰）。
"""
import csv
import os

import config
import fairness as fairness_mod
import ledger as ledger_mod
import metrics as metrics_mod
import models


def _md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return lines


def write_report(store, since_epoch=None, cfg=None, hours=None, site=None):
    """生成验收证据。返回 (ok, info_dict|reason)。"""
    cfg = cfg or config.load_settings()
    if since_epoch is None and hours:
        import time
        since_epoch = time.time() - float(hours) * 3600
    import access
    import drill as drill_mod

    m = metrics_mod.compute(store, cfg, since_epoch)
    cr = m["raw"]["channel_rate"]
    dr = m["raw"]["alt_availability"]
    tb = m["raw"]["tech_blocks"]
    av = m["raw"]["alt_verify"]
    cov = m["raw"]["measure_coverage"]
    fs = m["raw"]["fairness"]
    stamp = models.now_ts().replace("-", "").replace(":", "").replace(" ", "_")
    tag = ("_" + site) if site else ""

    L = []
    L.append("# %s" % config.REPORT_TITLE)
    L.append("")
    L.append("> 生成时间：%s ｜ 考点：%s ｜ 考期：%s ｜ 数据来源目录：`%s`"
             % (models.now_ts(), site or "、".join(config.sites(cfg)),
                cfg.get("session"), config.DATA_ROOT))
    L.append("")
    L.append("## 一、关键指标达标情况")
    L.append("")
    L.append("**总体结论：%s**" % ("硬指标全部达标" if m["ok"] else
                              "未达标：" + "、".join(m["hard_fail"])))
    L.append("")
    L += _md_table(["指标", "实测", "要求", "结论", "说明"],
                   [[i["name"], i["value_text"], i["target_text"],
                     "达标" if i["ok"] else ("未达标" if i["level"] == "hard" else "待改进"),
                     i["detail"]] for i in m["items"]])
    L.append("")

    L.append("## 二、通道配置（智能 / 人工 / 无障碍三通道并行）")
    L.append("")
    L.append("- 配置率：%.0f%%（%d/%d 考点）｜硬性尺寸：净宽 ≥%dmm、回转 ≥%dmm、"
             "低位终端 %d~%dmm"
             % (cr["rate"] * 100, cr["sites_ok"], cr["sites_total"],
                config.MIN_AISLE_WIDTH_MM, config.MIN_TURN_RADIUS_MM,
                config.COUNTER_HEIGHT_MIN_MM, config.COUNTER_HEIGHT_MAX_MM))
    L.append("")
    L += _md_table(["考点", "通道数", "智能", "人工", "无障碍", "结论", "问题"],
                   [[d["site"], d["total"], d["usable"].get("smart", 0),
                     d["usable"].get("manual", 0), d["usable"].get("accessible", 0),
                     "已配齐" if d["ok"] else "缺 " + "、".join(d["missing_text"]),
                     "；".join(d["channel_errors"][:3]) or "-"]
                    for d in cr["details"]])
    L.append("")
    chans = store.list_channels(site=site) if site else store.list_channels()
    if chans:
        L.append("### 通道明细（现场实测值）")
        L.append("")
        L += _md_table(["编码", "名称", "考点", "类型", "净宽mm", "回转mm", "低位终端",
                        "台面mm", "高度可调", "语音", "文字", "震动", "状态", "校验"],
                       [[c.code, c.name, c.site, c.kind_text(), c.width_mm,
                         c.turn_radius_mm, "是" if c.low_counter else "否",
                         c.counter_height_mm,
                         "是" if c.height_adjustable else "否",
                         "是" if c.voice_prompt else "否",
                         "是" if c.text_prompt else "否",
                         "是" if c.vibration else "否", c.state_text(),
                         "合格" if access.validate_channel(c)["ok"] else "有 error"]
                        for c in chans])
        L.append("")

    L.append("## 三、特殊考生档案与保障措施")
    L.append("")
    ps = store.profile_stats(since_epoch)
    L.append("- 建档 %d 人（生效 %d 人）｜未取得单独同意 %d 人（一律走人工核验通道，"
             "**不采集人脸、绝不因设备原因拒绝入场**）"
             % (ps["total"], ps["active"], ps["without_consent"]))
    L.append("- 保障措施 %d 项，已落实 %d 项（落实率 %.0f%%），待落实 %d 项，"
             "不予 %d 项" % (cov["measures"], cov["provided"], cov["rate"] * 100,
                            cov["pending"], cov["rejected"]))
    if ps["by_need"]:
        L.append("- 需求分布：%s" % "、".join(
            "%s %d 人" % (config.NEED_TEXT.get(k, k), v)
            for k, v in sorted(ps["by_need"].items(), key=lambda x: -x[1])))
    L.append("")
    led = ledger_mod.rows(store, site=site, since_epoch=since_epoch)
    if led:
        L.append("### 档案明细（脱敏）")
        L.append("")
        L += _md_table(["考生", "姓名", "考场", "座位", "需求", "建议通道", "单独同意",
                        "措施（已落实/共）", "替代核验次数", "受阻次数"],
                       [[r["candidate"], r["name"], r["room_id"], r["seat_no"],
                         r["needs_text"], r["channel_text"], r["consent_text"],
                         "%d/%d" % (r["measures_provided"], len(r["measures"])),
                         r["alt_verify_count"], r["blocks"]] for r in led])
        L.append("")

    L.append("## 四、替代核验（人工兜底，与智能核验同等效力）")
    L.append("")
    L.append("- 替代核验 %d 次：通过 %d、不通过（转考务）%d、待核实 %d；"
             "超时（>%d 秒）%d 次；平均耗时 %.0f 秒"
             % (av["total"], av["passed"], av["failed"], av["deferred"],
                config.ALT_VERIFY_SLA_SEC, av["overdue"], av["avg_sec"]))
    L.append("- **制度声明**：%s" % config.EQUIVALENCE_NOTE)
    L.append("")
    avs = store.list_alt_verifies(limit=200, since_epoch=since_epoch)
    if avs:
        L += _md_table(["编号", "时间", "考生", "方式", "双证比对", "主检", "见证",
                        "结论", "等效", "耗时s", "超时", "触发原因"],
                       [[a.av_id, a.ts, config.mask_id(a.candidate_id),
                         config.ALT_MODE_TEXT.get(a.mode, a.mode), a.docs_text(),
                         a.inspector, a.witness or "-",
                         config.ALT_OUTCOME_TEXT.get(a.outcome, a.outcome),
                         "是" if a.equivalence else "否", "%.0f" % a.elapsed_sec,
                         "是" if a.overdue else "否", a.reason[:40]] for a in avs])
        L.append("")

    L.append("## 五、因技术原因入场受阻事件（指标：0 起未入场）")
    L.append("")
    L.append("- 受阻 %d 起：已入场 %d 起（正常 %d / 替代通道 %d）、未结案 %d 起、"
             "**未能入场 %d 起**；平均等待 %.0f 秒，最长 %.0f 秒"
             % (tb["total"], tb["entered"] + tb["entered_manual"], tb["entered"],
                tb["entered_manual"], tb["open"], tb["denied"], tb["avg_wait_sec"],
                tb["max_wait_sec"]))
    if tb["denied"]:
        L.append("")
        L.append("> **注意**：出现 %d 起因技术原因未能入场事件，"
                 "须逐起追责整改并在下次报告里给出闭环结论。" % tb["denied"])
    L.append("")
    blocks = store.list_tech_blocks(limit=200, since_epoch=since_epoch)
    if blocks:
        L += _md_table(["编号", "考生", "考点/考场", "症状", "设备", "上报",
                        "结案", "等待s", "处置"],
                       [[b.block_id, config.mask_id(b.candidate_id),
                         "%s/%s" % (b.site or "-", b.room_id or "-"),
                         b.symptom_text(), b.device_code or "-",
                         "已上报" if b.escalated else "未上报",
                         b.final_text() if b.resolved else "未结案",
                         "%.0f" % b.waited_sec, (b.resolution or "-")[:50]]
                        for b in blocks])
        L.append("")

    L.append("## 六、算法公平性分层测试")
    L.append("")
    latest = fs.get("latest")
    if latest:
        L.append("- 最近一次：%s（%s），样本 %d，组间通过率最大差 %.2f%%，结论：%s"
                 % (latest["run_id"], latest["ts"], latest["total"],
                    latest["max_gap"] * 100, "差异在可接受范围" if latest["passed"]
                    else "**差异超差**"))
        L.append("- 补偿策略：%s%s"
                 % (config.FAIRNESS_ADJUST_TEXT.get(latest["action"], latest["action"]),
                    ("（%s）" % latest["action_note"]) if latest["action_note"] else ""))
        groups = store.list_fairness_groups(latest["run_id"])
        if groups:
            L += _md_table(["分层", "分组", "样本", "通过", "通过率", "与最高组差",
                            "样本是否充足"],
                           [[config.DIM_TEXT.get(g.dim, g.dim), g.group_name, g.total,
                             g.passed, "%.2f%%" % (g.pass_rate * 100),
                             "%.2f%%" % (g.gap_to_best * 100),
                             "不足" if g.small_sample else "充足"] for g in groups])
            L.append("")
    else:
        L.append("- **尚无分层测试记录**：上线前必须完成分层测试（性别/年龄/眼镜/口罩），"
                 "否则算法公平性无法评估。")
        L.append("")
    tr = fairness_mod.trend(store, limit=6)
    if len(tr) > 1:
        L.append("### 整改前后对比（组间最大差）")
        L.append("")
        L += _md_table(["时间", "测试", "最大差", "结论", "补偿策略", "最差分分组"],
                       [[x["ts"], x["run_id"], "%.2f%%" % (x["max_gap"] * 100),
                         "达标" if x["passed"] else "超差", x["action_text"],
                         "%s/%s" % (config.DIM_TEXT.get(x["worst_dim"], x["worst_dim"]),
                                    x["worst_group"])] for x in tr])
        L.append("")

    L.append("## 七、演练记录（每考期 ≥%d 次）" % config.TARGET_DRILL_PER_SESSION)
    L.append("")
    L.append("- 替代通道可用性 %.0f%%（%d/%d 考点），考期 %s"
             % (dr["rate"] * 100, dr["sites_ok"], dr["sites_total"], dr["session"]))
    L.append("")
    L += _md_table(["考点", "本考期演练次数", "不合格", "最近一次", "覆盖科目", "结论"],
                   [[d["site"], d["drills"], d["failed_drills"], d["last_drill"] or "-",
                     "、".join(config.DRILL_SCENARIOS.get(s, s) for s in d["scenarios"])
                     or "-", "可用" if d["ok"] else "；".join(d["reasons"])]
                    for d in dr["details"]])
    L.append("")
    drills = store.list_drills(limit=100, since_epoch=since_epoch)
    if drills:
        L += _md_table(["编号", "时间", "考点", "科目", "参与", "计划/实际分钟", "发现的问题",
                        "整改措施", "结论"],
                       [[d.drill_id, d.ts, d.site, d.scenario_text(), d.participants,
                         "%.0f/%.0f" % (d.planned_min, d.actual_min),
                         "；".join(d.issues)[:60] or "无", (d.actions or "-")[:60],
                         d.result_text()] for d in drills])
        L.append("")

    L.append("## 八、联动留痕")
    L.append("")
    ob = store.outbox_stats()
    audits = store.list_audits(limit=2000)
    linked = [a for a in audits if "受阻" in (a.action or "")
              or "替代核验" in (a.action or "")]
    ok_chain, chain_msg, n = store.verify_audit_chain()
    L.append("- 联动外发队列：共 %d 条，待重发 %d 条（03 智能终端不可达时事件不丢，"
             "恢复后自动补发）" % (ob["total"], ob["pending"]))
    L.append("- 本模块审计链：%s" % chain_msg)
    L.append("- 上报类留痕 %d 条（受阻/替代核验），最近 5 条：%s"
             % (len(linked), "；".join("%s %s" % (a.ts, a.detail[:40])
                                      for a in linked[:5]) or "无"))
    L.append("")

    L.append("## 九、附录：口径与如实说明")
    L.append("")
    L.append("1. **通道数据是实测录入值**：净宽/回转/台面高度/提示方式由现场测量与验收填写，"
             "系统只做合规判定，不推算、不代填。")
    L.append("2. **无真实设备时的演示数据**：来源标记 `demo` 的记录在界面与报告里都写明"
             "「演示」，不冒充真实保障记录。")
    L.append("3. **未取得单独同意**：一律安排人工核验通道，不采集人脸；"
             "本模块从不以「未同意」为由拒绝入场。")
    L.append("4. **替代核验时限**：%d 秒（超过计超时并进入整改清单），"
             "时限内完成率见指标表。" % config.ALT_VERIFY_SLA_SEC)
    L.append("5. **最小必要**：台账导出默认脱敏（证件号后 4 位、姓名留姓）；"
             "医疗证明编号只在库内加密存储，不导出。")
    L.append("6. **加密**：%s" % _crypto_text())
    L.append("7. **依赖**：仅标准库（tkinter/sqlite3/hashlib）；"
             "与 00/01/02/03/06 通过子进程 + JSON 联动，本模块不修改其它专题任何文件。")
    L.append("")

    md_path = os.path.join(str(config.REPORT_DIR),
                           "无障碍保障验收报告_%s%s.md" % (stamp, tag))
    csv_path = os.path.join(str(config.REPORT_DIR),
                            "无障碍保障指标明细_%s%s.csv" % (stamp, tag))
    ledger_path = ""
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["指标", "实测", "要求", "是否达标", "说明"])
            for i in m["items"]:
                w.writerow([i["name"], i["value_text"], i["target_text"],
                            "达标" if i["ok"] else "未达标", i["detail"]])
        ok_l, ledger_path = ledger_mod.write_ledger(store, fmt="csv", site=site,
                                                    since_epoch=since_epoch, tag=tag)
        if not ok_l:
            ledger_path = ""
    except OSError as e:
        return False, "报告写入失败：%s" % e
    store.log("生成验收报告", os.path.basename(md_path), cfg.get("operator"))
    return True, {"md": md_path, "csv": csv_path, "ledger": ledger_path,
                  "ok_metrics": m["ok"], "hard_fail": m["hard_fail"],
                  "audit_chain": chain_msg, "audit_ok": ok_chain, "audit_rows": n,
                  "generated_at": models.now_ts()}


def _crypto_text():
    try:
        import data_crypto
        return data_crypto.status_text()
    except Exception:                                          # noqa: BLE001
        return "加密模块不可用"
