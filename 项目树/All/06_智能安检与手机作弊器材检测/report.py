# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 验收证据报告

《智能安检与手机作弊器材检测.md》第四节要求的三类证据，本模块逐条给：
  1) 检出率/误报率实测记录（含关机手机、无线耳机样本测试）
  2) 入场效率实测与高峰期疏导演练记录
  3) 特殊考生替代通道演练记录

**口径纪律（报告里会写明，避免拿好看的数字糊验收）**：
  · 检出率只来自**样本测试**（有 ground truth），不拿"报警数/过检数"冒充；
  · 判据回放（replay）验的是算法与阈值，**不能**充作整机实测证据；
    整机实测必须用"现场实测"（field）那一份；
  · 没有现场实测数据时，报告明确写"整机实测证据：缺失"；
  · 模拟器（source=sim）产生的数据在报告里单独计数，绝不与真机数据混合。
"""
import csv
import time

import config
import locker as locker_mod
import models
import samples as samples_mod


def _pct(v):
    try:
        return "%.1f%%" % (float(v) * 100)
    except (TypeError, ValueError):
        return "-"


def _mark(ok):
    if ok is None:
        return "无样本"
    return "达标" if ok else "**未达标**"


def build_report(store, since_epoch=None, window_sec=None, site=None):
    """
    生成报告数据（dict）。报告 = 数据 + 口径说明 + 前提条件，
    任何时候都能生成，缺数据的部分会如实写成"缺失"。
    """
    cfg = config.load_settings()
    site = site or cfg.get("site", "")
    since = float(since_epoch or (time.time() - 24 * 3600))
    window_sec = float(window_sec or config.THROUGHPUT_WINDOW_SEC)

    # ---- 样本测试：回放/实测分开 ----
    replay = store.latest_test_run(mode="replay")
    field = store.latest_test_run(mode="field")
    field_items = store.list_test_items(field.run_id) if field else []
    replay_items = store.list_test_items(replay.run_id) if replay else []

    # ---- 现场运行数据 ----
    passes_all = store.pass_count(since_epoch=since)
    passes_real = len([p for p in store.list_passes(limit=10000, since_epoch=since)
                       if p.source == "real"])
    passes_sim = len([p for p in store.list_passes(limit=10000, since_epoch=since)
                      if p.source == "sim"])
    alarm_stats = store.alarm_stats(since)
    rein = store.reinspect_stats(since)
    ppm = store.throughput_ppm(window_sec=window_sec)
    cells = store.cell_stats()
    lockers = store.list_lockers()
    specials = store.special_stats(since)
    flows = store.flow_stats(since)
    modes = samples_mod.compare_modes(store)
    audit_ok, audit_n, audit_msg = store.verify_audit_chain()
    devices = store.list_devices()

    metrics = {}
    source_note = ""
    if field and field_items:
        metrics = field.metrics or samples_mod.summarize_items(field_items)
        source_note = "现场实测（真设备跑真实样本，run=%s）" % field.run_id
    elif replay and replay_items:
        metrics = replay.metrics or samples_mod.summarize_items(replay_items)
        source_note = ("判据回放（run=%s）——**只验算法与阈值，不能充作整机实测证据**"
                       % replay.run_id)

    return {
        "generated_at": models.now_ts(),
        "site": site,
        "window_hours": round((time.time() - since) / 3600.0, 1),
        "throughput_window_sec": window_sec,
        "devices": [{"code": d.code, "name": d.name, "kind": d.kind, "proto": d.proto,
                     "status": d.status, "channel": d.channel_no} for d in devices],
        "device_online": store.device_online_rate(),
        "passes": {"total": passes_all, "real": passes_real, "sim": passes_sim},
        "alarms": alarm_stats,
        "reinspect": rein,
        "throughput_ppm": ppm,
        "throughput_target": config.TARGET_THROUGHPUT_PPM,
        "throughput_ok": ppm >= config.TARGET_THROUGHPUT_PPM,
        "lockers": {"count": len(lockers), "cells": cells},
        "locker_usage": locker_mod.usage_by_locker(store),
        "specials": specials,
        "flows": flows,
        "metrics": metrics,
        "metrics_source": source_note,
        "test_runs": [{"run_id": r.run_id, "mode": r.mode, "total": r.total,
                       "hit": r.hit, "miss": r.miss, "false_alarm": r.false_alarm,
                       "operator": r.operator, "finished_at": r.finished_at,
                       "note": r.note} for r in store.list_test_runs(limit=10)],
        "modes": modes,
        "audit": {"ok": audit_ok, "count": audit_n, "msg": audit_msg},
        "notes": _honest_notes(store, field, replay),
    }


def _honest_notes(store, field, replay):
    """必须写进报告的"如实说明"——不写清楚就等于糊弄验收。"""
    notes = [
        "检出率的**唯一**算法：应报警且报了 / 应报警样本数；分母来自样本库 ground truth，"
        "不是过检总数。",
        "误报率的唯一定义：无害样本（腰带扣/钥匙串/眼镜/硬币/纯布衣）却报警 / 无害样本数。",
        "**植入医疗器械（起搏器/骨钉）报警不算误报**：设备本就该响，正确处理是转人工安检通道、"
        "双人见证，并提示考生日后主动申报、不要过安检门。",
    ]
    if field is None:
        notes.append("**整机实测证据：缺失**。当前只有判据回放，请务必用真设备跑一遍"
                     "「现场实测」再验收（流程：样本放身上 -> 过门 -> 看报没报 -> 逐条录入）。")
    if replay is None:
        notes.append("尚无判据回放记录；建议先跑一次回放，确认阈值口径后再上真机。")
    sim_n = store.pass_count(source="sim")
    real_n = store.pass_count(source="real")
    if sim_n:
        notes.append("模拟器（source=sim）已产生 %d 条过检记录，**不计入任何检出率/误报率**；"
                     "真机入库记录 %d 条。" % (sim_n, real_n))
    notes.append("鞋内藏匿必须配鞋底检测仪（本方案样本 SP-B03 就是这一路）；"
                 "没配鞋底仪时，鞋内手机会漏 —— 报告不会把这一条算成「已覆盖」。")
    notes.append("无线耳机信号弱（样本 SP-B07 仅 0.55），阈值调钝会漏、调灵会误报，"
                 "本模块用「单路报橙 + 复检兜底」的方式压误报。")
    try:
        import data_crypto
        notes.append("考生标识字段（身份证/姓名/准考证/座位）落库策略：%s"
                     "（加密后按不可逆盲索引查询，兼容历史明文，随时可开关）。"
                     % data_crypto.status_text())
    except Exception:                                          # noqa: BLE001
        notes.append("考生标识字段加密模块不可用：当前为明文落库，仅靠本机文件权限保护。")
    return notes


# ---------------- Markdown 渲染 ----------------
def render_markdown(rep):
    L = []
    L.append("# %s" % config.REPORT_TITLE)
    L.append("")
    L.append("> 生成时间：%s ｜ 考点：%s ｜ 统计窗口：近 %.1f 小时"
             % (rep["generated_at"], rep["site"], rep["window_hours"]))
    L.append("> 数据目录：`%s`" % config.DATA_ROOT)
    L.append("")

    L.append("## 一、指标达成情况（口径见文末说明）")
    L.append("")
    L.append("| 指标项 | 要求 | 实测 | 结论 |")
    L.append("| --- | --- | --- | --- |")
    m = rep.get("metrics") or {}
    if m:
        L.append("| 手机（含关机）检出率 | ≥ %.0f%% | %s | %s |"
                 % (config.TARGET_PHONE_RATE * 100, _pct(m.get("phone_rate")),
                    _mark((m.get("pass") or {}).get("phone"))))
        L.append("| 无线耳机检出率 | ≥ %.0f%% | %s | %s |"
                 % (config.TARGET_EARPHONE_RATE * 100, _pct(m.get("earphone_rate")),
                    _mark((m.get("pass") or {}).get("earphone"))))
        L.append("| 误报率 | ≤ %.0f%% | %s | %s |"
                 % (config.TARGET_FALSE_RATE * 100, _pct(m.get("false_rate")),
                    _mark((m.get("pass") or {}).get("false_rate"))))
        L.append("| 作弊器材整体检出率 | （参考） | %s（%d 条样本） | - |"
                 % (_pct(m.get("cheat_rate")), m.get("cheat_total", 0)))
        L.append("| 植入医疗器械报警→人工通道 | 报警并转人工通道 | %s（%d 条样本） | %s |"
                 % (_pct(m.get("implant_channel_rate")), m.get("implant_total", 0),
                    _mark(m.get("implant_total", 0) > 0)))
    else:
        L.append("| 手机检出率 | ≥ %.0f%% | 无样本测试记录 | **缺证据** |"
                 % (config.TARGET_PHONE_RATE * 100))
        L.append("| 无线耳机检出率 | ≥ %.0f%% | 无样本测试记录 | **缺证据** |"
                 % (config.TARGET_EARPHONE_RATE * 100))
        L.append("| 误报率 | ≤ %.0f%% | 无样本测试记录 | **缺证据** |"
                 % (config.TARGET_FALSE_RATE * 100))
    L.append("| 单人通过效率 | ≥ %.0f 人/分钟 | %.1f 人/分钟（近 %.0f 分钟） | %s |"
             % (config.TARGET_THROUGHPUT_PPM, rep["throughput_ppm"],
                rep["throughput_window_sec"] / 60.0, _mark(rep["throughput_ok"])))
    L.append("")
    if rep.get("metrics_source"):
        L.append("> 检出率/误报率的证据来源：%s" % rep["metrics_source"])
        L.append("")

    L.append("## 二、设备与接入")
    L.append("")
    dev = rep.get("device_online") or {}
    L.append("设备 %d 台，在线 %d 台（在线率 %s）。"
             % (dev.get("total", 0), dev.get("online", 0), _pct(dev.get("rate"))))
    L.append("")
    if rep["devices"]:
        L.append("| 编码 | 名称 | 类型 | 接入方式 | 通道 | 状态 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for d in rep["devices"]:
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (d["code"], d["name"],
                        config.DEV_KIND_TEXT.get(d["kind"], d["kind"]),
                        config.PROTO_TEXT.get(d["proto"], d["proto"]),
                        d["channel"] or "-", "在线" if d["status"] == "online" else "离线"))
    else:
        L.append("尚未登记设备（安检门/手持探测器/储物柜/鞋底仪）。")
    L.append("")

    L.append("## 三、过检与报警处置")
    L.append("")
    p = rep["passes"]
    L.append("- 过检记录：%d 条（真机 real=%d，模拟 sim=%d；**模拟不计入检出率**）"
             % (p["total"], p["real"], p["sim"]))
    a = rep["alarms"]
    L.append("- 报警：%d 条（红色 %d 条，待处置 %d 条）"
             % (a["total"], a["red"], a["pending"]))
    r = rep["reinspect"]
    L.append("- 复检处置：%d 条（查获 %d、转人工通道 %d、误报 %d）"
             % (r["done"], r["detained"], r["to_manual_channel"], r["false_positive"]))
    L.append("- 误报率（复检口径）：%s ｜ 平均处置 %.0f 秒，最长 %.0f 秒（SLA %d 秒）"
             % (_pct(r["false_rate"]), r["avg_cost_sec"], r["max_cost_sec"],
                config.REINSPECT_SLA_SEC))
    L.append("")

    L.append("## 四、入场效率与瓶颈")
    L.append("")
    f = rep["flows"]
    L.append("- 建档考生：%d 人，完成三点位（安检-核验-签到）：%d 人"
             % (f["tracked"], f["completed"]))
    L.append("- 人均入场总耗时：%.0f 秒（最长 %.0f 秒）"
             % (f["avg_total_sec"], f["max_total_sec"]))
    if f.get("avg_stage_sec"):
        L.append("- 各点位平均耗时：%s" % "、".join(
            "%s %.0f 秒" % (config.STAGE_TEXT.get(k, k), v)
            for k, v in f["avg_stage_sec"].items()))
    if f.get("bottleneck"):
        L.append("- 瓶颈点位：**%s**（按点位串行耗时判定）"
                 % config.STAGE_TEXT.get(f["bottleneck"], f["bottleneck"]))
    L.append("- 当前通过效率：%.1f 人/分钟（要求 ≥ %.0f）"
             % (rep["throughput_ppm"], config.TARGET_THROUGHPUT_PPM))
    L.append("")

    L.append("## 五、手机集中保管")
    L.append("")
    lu = rep.get("locker_usage") or []
    c = rep["lockers"]["cells"]
    L.append("- 柜体 %d 台，柜格 %d 个：占用 %d、空闲 %d、故障 %d、超时未取 %d"
             % (rep["lockers"]["count"], c.get("total", 0), c.get("occupied", 0),
                c.get("free", 0), c.get("fault", 0), c.get("overdue", 0)))
    if lu:
        L.append("")
        L.append("| 柜体 | 考点/位置 | 柜格 | 占用 | 空闲 | 故障 | 使用率 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for x in lu:
            L.append("| %s | %s | %d | %d | %d | %d | %s |"
                     % (x["code"], x["site"] or "-", x["total"], x["occupied"],
                        x["free"], x["fault"], _pct(x["usage"])))
    L.append("")

    L.append("## 六、特殊考生替代通道")
    L.append("")
    s = rep["specials"]
    L.append("- 人工安检记录：%d 条（真实 %d、演练 %d），其中双人见证 %d 条"
             % (s["total"], s["real"], s["drill"], s["double_witness"]))
    if s.get("by_reason"):
        L.append("- 按原因：%s" % "、".join(
            "%s %d 条" % (config.SPECIAL_REASONS.get(k, k), v)
            for k, v in s["by_reason"].items()))
    if s["real"] + s["drill"] == 0:
        L.append("- **尚无人工安检记录**：替代通道必需，请至少做一次演练"
                 "（孕妇/植入医疗器械考生走人工手检 + 双人见证）。")
    L.append("")

    L.append("## 七、阈值模式对比（调校依据）")
    L.append("")
    L.append("同一批样本在三种判据模式下的表现（模式=阈值整体缩放，见设置页）：")
    L.append("")
    L.append("| 模式 | 手机检出率 | 耳机检出率 | 作弊整体 | 误报率 | 说明 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    mode_text = {"turbo": "快速通行（阈值放宽，误报↑）",
                 "standard": "标准（默认）",
                 "strict": "从严（阈值收紧，漏检↑）"}
    for mode in ("turbo", "standard", "strict"):
        mm = rep["modes"].get(mode) or {}
        L.append("| %s | %s | %s | %s | %s | %s |"
                 % (config.GATE_MODE_TEXT.get(mode, mode), _pct(mm.get("phone_rate")),
                    _pct(mm.get("earphone_rate")), _pct(mm.get("cheat_rate")),
                    _pct(mm.get("false_rate")), mode_text.get(mode, "")))
    L.append("")

    L.append("## 八、测试运行记录")
    L.append("")
    if rep["test_runs"]:
        L.append("| 运行号 | 模式 | 样本 | 检出 | 漏检 | 误报 | 操作人 | 完成时间 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in rep["test_runs"]:
            L.append("| %s | %s | %d | %d | %d | %d | %s | %s |"
                     % (t["run_id"], "判据回放" if t["mode"] == "replay" else "现场实测",
                        t["total"], t["hit"], t["miss"], t["false_alarm"],
                        t["operator"] or "-", t["finished_at"] or "-"))
    else:
        L.append("尚无测试运行记录（先在「样本测试」页跑一次判据回放或现场实测）。")
    L.append("")

    L.append("## 九、留痕与审计")
    L.append("")
    au = rep["audit"]
    L.append("- 审计哈希链：%s（%s）" % ("完整" if au["ok"] else "**已断链**", au["msg"]))
    L.append("- 报警上报 03 智能终端：见界面「过检与报警」页的 reported 标记；"
             "上报链路走 00_集成联动，与 02/03 共用一套告警分级。")
    L.append("")

    L.append("## 十、如实说明（口径与前提）")
    L.append("")
    for i, n in enumerate(rep.get("notes") or [], 1):
        L.append("%d. %s" % (i, n))
    L.append("")
    L.append("---")
    L.append("")
    L.append("本报告由 `06_智能安检与手机作弊器材检测` 的 `report.py` 自动生成；"
             "所有数字都能在 `data\\security.db` 中逐条回查，审计链可自证完整性。")
    return "\n".join(L)


def write_report(store, since_epoch=None, window_sec=None, site=None, path=None):
    """生成并落盘 Markdown 报告 + 指标 CSV。返回 (ok, 路径说明|原因)"""
    rep = build_report(store, since_epoch=since_epoch, window_sec=window_sec, site=site)
    md = render_markdown(rep)
    ts = time.strftime("%Y%m%d_%H%M%S")
    md_path = path or (config.REPORT_DIR / ("安检验收证据_%s.md" % ts))
    try:
        with open(str(md_path), "w", encoding="utf-8") as f:
            f.write(md)
    except OSError as e:
        return False, "写入失败：%s" % e
    csv_path = str(md_path).rsplit(".", 1)[0] + ".csv"
    ok, info = write_metrics_csv(store, rep, csv_path)
    store.log("生成验收报告", "%s（CSV：%s）" % (md_path, ok), "report")
    return True, {"md": str(md_path), "csv": info if ok else csv_path, "report": rep}


def write_metrics_csv(store, rep, path):
    """关键指标 CSV（便于粘贴进验收表格）。"""
    m = rep.get("metrics") or {}
    rows = [
        ["指标", "要求", "实测", "是否达标"],
        ["手机（含关机）检出率", ">=%.0f%%" % (config.TARGET_PHONE_RATE * 100),
         _pct(m.get("phone_rate")), str((m.get("pass") or {}).get("phone"))],
        ["无线耳机检出率", ">=%.0f%%" % (config.TARGET_EARPHONE_RATE * 100),
         _pct(m.get("earphone_rate")), str((m.get("pass") or {}).get("earphone"))],
        ["误报率", "<=%.0f%%" % (config.TARGET_FALSE_RATE * 100),
         _pct(m.get("false_rate")), str((m.get("pass") or {}).get("false_rate"))],
        ["作弊器材整体检出率", "参考", _pct(m.get("cheat_rate")), "-"],
        ["通过效率", ">=%.0f 人/分钟" % config.TARGET_THROUGHPUT_PPM,
         "%.1f" % rep["throughput_ppm"], str(rep["throughput_ok"])],
        ["复检平均处置时长", "<=%d 秒" % config.REINSPECT_SLA_SEC,
         "%.0f" % rep["reinspect"]["avg_cost_sec"],
         str(rep["reinspect"]["avg_cost_sec"] <= config.REINSPECT_SLA_SEC)],
        ["特殊考生人工通道记录", ">=1（真实或演练）",
         "%d" % rep["specials"]["total"],
         str(rep["specials"]["total"] > 0)],
        ["审计链完整", "是", str(rep["audit"]["ok"]), str(rep["audit"]["ok"])],
        ["证据来源", "-", rep.get("metrics_source") or "无", "-"],
    ]
    try:
        with open(str(path), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerows(rows)
        return True, str(path)
    except OSError as e:
        return False, str(e)
