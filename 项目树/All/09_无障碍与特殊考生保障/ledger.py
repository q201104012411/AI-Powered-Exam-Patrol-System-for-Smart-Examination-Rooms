# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 特殊考生处置台账（脱敏导出）

验收证据里要交"特殊考生处置台账样例（脱敏）"。台账按**考生**聚合：
    需求 -> 保障措施落实情况 -> 替代核验记录 -> 受阻与结案结果

默认脱敏：准考证/身份证只留后 4 位、姓名只留姓；单独同意与证明编号一律不导出。
"""
import csv
import os

import config
import models


def rows(store, site=None, since_epoch=None, limit=1000, desensitize=True):
    """生成台账行（按考生聚合）。"""
    cfg = config.load_settings()
    desensitize = bool(desensitize if desensitize is not None
                       else cfg.get("desensitize_export", True))
    out = []
    for pf in store.list_profiles(limit=limit, site=site):
        if since_epoch and pf.epoch < float(since_epoch):
            continue
        avs = store.list_alt_verifies(limit=200, candidate_id=pf.candidate_id)
        blocks = [b for b in store.list_tech_blocks(limit=500)
                  if b.candidate_key == pf.candidate_key]
        accs = store.list_accommodations(profile_id=pf.profile_id)
        last_av = avs[0] if avs else None
        last_block = blocks[0] if blocks else None
        provided = [a for a in accs if a.status == config.ACC_PROVIDED]
        out.append({
            "candidate": (config.mask_id(pf.candidate_id) if desensitize
                          else pf.candidate_id),
            "name": (config.mask_name(pf.candidate_name) if desensitize
                     else pf.candidate_name),
            "room_id": pf.room_id, "seat_no": pf.seat_no, "site": pf.site,
            "needs": pf.needs, "needs_text": pf.needs_text(),
            "channel_hint": pf.channel_hint,
            "channel_text": config.CHANNEL_TEXT.get(pf.channel_hint, ""),
            "consent": bool(pf.consent),
            "consent_text": "已取得单独同意" if pf.consent else "未取得单独同意（走人工通道）",
            "measures": [{"measure": a.measure, "measure_text": a.measure_text(),
                          "status": a.status, "status_text": a.state_text(),
                          "owner": a.owner, "place": a.place,
                          "provided_at": a.provided_at} for a in accs],
            "measures_text": "、".join("%s(%s)" % (a.measure_text(), a.state_text())
                                     for a in accs) or "无",
            "measures_provided": len(provided),
            "alt_verify_count": len(avs),
            "last_alt_verify": ({
                "av_id": last_av.av_id, "ts": last_av.ts,
                "mode": config.ALT_MODE_TEXT.get(last_av.mode, last_av.mode),
                "outcome": config.ALT_OUTCOME_TEXT.get(last_av.outcome, last_av.outcome),
                "docs": last_av.docs_text(), "inspector": last_av.inspector,
                "witness": last_av.witness, "elapsed_sec": last_av.elapsed_sec,
                "equivalence": last_av.equivalence,
            } if last_av else None),
            "blocks": len(blocks),
            "block": ({
                "block_id": last_block.block_id, "symptom_text": last_block.symptom_text(),
                "raised_at": last_block.raised_at, "resolved": last_block.resolved,
                "final_text": last_block.final_text(),
                "waited_sec": last_block.waited_sec,
                "resolution": last_block.resolution,
            } if last_block else None),
            "status": pf.status,
        })
    return out


def _md(rows_, title, meta):
    lines = ["# %s" % title, ""]
    for k, v in meta.items():
        lines.append("- %s：%s" % (k, v))
    lines += ["", "| 考生 | 姓名 | 考点 | 考场 | 座位 | 需求 | 建议通道 | 单独同意 | "
                  "保障措施落实 | 替代核验 | 受阻/结案 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows_:
        blk = "-"
        if r["block"]:
            blk = "%s / %s%s" % (r["block"]["symptom_text"],
                                 r["block"]["final_text"],
                                 "（%.0fs）" % r["block"]["waited_sec"]
                                 if r["block"]["waited_sec"] else "")
        av = "-"
        if r["last_alt_verify"]:
            av = "%s %s" % (r["last_alt_verify"]["mode"],
                            r["last_alt_verify"]["outcome"])
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s/%d | %s | %s |"
                     % (r["candidate"], r["name"], r["site"], r["room_id"],
                        r["seat_no"], r["needs_text"], r["channel_text"],
                        r["consent_text"], r["measures_provided"],
                        len(r["measures"]), av, blk))
    lines += ["", "> 说明：本台账默认脱敏（证件号只留后 4 位、姓名只留姓）；"
                  "单独同意与医疗证明编号不导出。"]
    return "\n".join(lines) + "\n"


def write_ledger(store, fmt="md", site=None, since_epoch=None, desensitize=True,
                 tag=""):
    """
    导出特殊考生处置台账。fmt: md / csv / json。返回 (ok, path|reason)。
    只落在本模块 data/ledgers/ 下，不往别处写。
    """
    cfg = config.load_settings()
    data = rows(store, site=site, since_epoch=since_epoch,
                desensitize=desensitize)
    stamp = models.now_ts().replace("-", "").replace(":", "").replace(" ", "_")
    suffix = ("_" + tag) if tag else ""
    meta = {
        "考点": site or "全部", "考期": cfg.get("session"),
        "导出时间": models.now_ts(), "记录数": len(data),
        "脱敏": "是" if desensitize else "否（含明文证件号，注意保密）",
        "口径": "一名考生一行；替代核验按最近一次显示；受阻事件按最近一次显示",
    }
    try:
        if fmt == "json":
            path = os.path.join(str(config.LEDGER_DIR),
                                "特殊考生处置台账_%s%s.json" % (stamp, suffix))
            store.export_json(data, path)
        elif fmt == "csv":
            path = os.path.join(str(config.LEDGER_DIR),
                                "特殊考生处置台账_%s%s.csv" % (stamp, suffix))
            cols = ["candidate", "name", "site", "room_id", "seat_no", "needs_text",
                    "channel_text", "consent_text", "measures_provided",
                    "measures_text", "alt_verify_count", "blocks", "status"]
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["考生", "姓名", "考点", "考场", "座位", "需求", "建议通道",
                            "单独同意", "已落实措施数", "措施明细", "替代核验次数",
                            "受阻次数", "档案状态"])
                for r in data:
                    w.writerow([r.get(c, "") for c in cols])
        else:
            path = os.path.join(str(config.LEDGER_DIR),
                                "特殊考生处置台账_%s%s.md" % (stamp, suffix))
            with open(path, "w", encoding="utf-8") as f:
                f.write(_md(data, "特殊考生处置台账（脱敏）", meta))
    except (OSError, TypeError, ValueError) as e:
        # 不吞数据：导出失败如实返回原因（OSError=路径/权限，Type/Value=内容不可序列化）
        return False, "导出失败：%s: %s" % (type(e).__name__, str(e)[:150])
    store.log("导出台账", "%s -> %s" % (fmt, os.path.basename(path)), cfg.get("operator"))
    return True, path


def summary_text(store, site=None):
    data = rows(store, site=site)
    n = len(data)
    no_consent = len([r for r in data if not r["consent"]])
    with_av = len([r for r in data if r["alt_verify_count"]])
    blocked = len([r for r in data if r["blocks"]])
    entered = len([r for r in data if r["block"] and r["block"]["final_text"]
                   not in ("未结案", "未能入场（须追责整改）")])
    return ("特殊考生 %d 人｜未取得单独同意 %d 人｜走过替代核验 %d 人｜"
            "受阻 %d 人（其中已入场 %d 人）" % (n, no_consent, with_av, blocked, entered))
