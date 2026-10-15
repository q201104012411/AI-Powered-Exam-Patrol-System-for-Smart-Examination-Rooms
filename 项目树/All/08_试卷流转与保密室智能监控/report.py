# -*- coding: utf-8 -*-
"""
关键指标与验收证据报告（文档第三、四节）

指标逐项给出"实测值 / 要求 / 结论"三条，**样本为 0 时一律标"样本不足"，
绝不当达标**（这是本仓库一贯的如实口径）。

| 指标项                   | 要求                     | 数据来源                              |
| 保密室视频覆盖率         | 100%，无盲区             | CameraManager.coverage()              |
| 开柜/开门异常告警时延    | ≤ 5 秒                   | alarms.latency_sec（门禁/试卷柜码）    |
| 交接记录完整率           | 100% 可追溯              | HandoverFlow.completeness_rate()      |
| 录像留存与完整性         | 留存期内连续可回放，无丢段 | RecordGuard.continuity()/retention()  |
| 押运路线偏离告警时延     | 偏离即报，≤ 30 秒         | alarms.latency_sec（ESCORT_ROUTE_DEVIATE）|
"""
import csv
import json
import time

import config


def _verdict(actual, op, target):
    if actual is None:
        return "样本不足"
    try:
        a, t = float(actual), float(target)
    except (TypeError, ValueError):
        return "无法判定"
    if op == ">=":
        return "达标" if a >= t else "未达标"
    if op == "<=":
        return "达标" if a <= t else "未达标"
    if op == "==":
        return "达标" if a == t else "未达标"
    return "无法判定"


def _fmt_pct(v):
    return "—" if v is None else "%.1f%%" % (float(v) * 100)


def _fmt_sec(v):
    return "—" if v is None else "%.3f 秒" % float(v)


class KPIReport(object):
    def __init__(self, store, vault=None, flow=None):
        self.store = store
        self.vault = vault
        self.flow = flow

    # ---------- 逐项实测 ----------
    def kpi(self, room_id=None):
        """返回逐项指标：[{key,name,required,actual,actual_text,verdict,how}]"""
        out = []

        # 1. 覆盖率
        if self.vault and room_id:
            cams = self.store.cameras(room_id)
            if not cams:
                # 该保密室还没登记任何机位：这是"没样本"，不是"覆盖率不达标"。
                # 本仓库口径：样本为 0 一律标"样本不足"，绝不当达标、也不当未达标。
                rate, how = None, "保密室 %s 尚未登记任何机位（无样本，需现场布点）" % room_id
            else:
                rate, detail = self.vault.cameras.coverage(room_id)
                blind = self.vault.cameras.blind_spots(room_id)
                how = "保密室 %s：已覆盖 %d/6 面" % (room_id, int(round(rate * 6)))
                if blind:
                    how += "；盲区朝向：%s" % "、".join(blind)
        else:
            rate, how = None, "未指定保密室编号，无法计算覆盖率"
        out.append(self._row("coverage_rate", rate, _fmt_pct(rate), how))

        # 2. 开柜/开门异常告警时延
        codes = ("SAFE_OPEN_ABNORMAL", "DOOR_OPEN_ABNORMAL", "ACCESS_DUAL_FAIL",
                 "INTRUSION_IR", "INTRUSION_DOOR_MAG", "INTRUSION_VIBRATION")
        stats = self._latency(codes)
        worst = max([v["max"] for v in stats.values()], default=None)
        n = sum(v["n"] for v in stats.values())
        how = ("共 %d 条带时延记录的异常事件" % n) if n else "本机暂无异常事件样本（需现场触发）"
        if stats:
            how += "；逐码最大时延：" + "、".join(
                "%s %.3fs" % (k, v["max"]) for k, v in sorted(stats.items()))
        out.append(self._row("alarm_latency", worst, _fmt_sec(worst), how))

        # 3. 交接记录完整率
        if self.flow:
            rate_h, ok, total = self.flow.handover.completeness_rate(
                limit=10000, room_id=room_id)
            scope = ("保密室 %s " % room_id) if room_id else ""
            how = ("%s可追溯 %d/%d 条" % (scope, ok, total)) if total \
                else ("保密室 %s 暂无交接记录（无样本）" % room_id if room_id
                      else "暂无交接记录")
        else:
            rate_h, how = None, "未接入流转模块"
        out.append(self._row("handover_complete", rate_h, _fmt_pct(rate_h), how))

        # 4. 录像留存与完整性
        if self.vault:
            cams = self.store.cameras(room_id) if room_id else self.store.cameras()
            checked, ok_cnt, gaps_total, unplayable = 0, 0, 0, 0
            for c in cams:
                chs = self.vault.records.channels(c.cam_id) or ["main"]
                for ch in chs:
                    r = self.vault.records.continuity(c.cam_id, ch)
                    checked += 1
                    ok_cnt += 1 if r["ok"] else 0
                    gaps_total += len(r["gaps"])
                    unplayable += len(r["unplayable"])
            rate_r = (ok_cnt / float(checked)) if checked else None
            ret = self.vault.records.retention_report()
            how = ("检查 %d 条通道，缺口 %d 段，不可回放 %d 段；留存 %d 天，未加锁 %d 段"
                   % (checked, gaps_total, unplayable, ret["retain_days"],
                      ret["unlocked"])) if checked else "暂无录像段登记（需接入录像系统）"
        else:
            rate_r, how = None, "未接入录像模块"
        out.append(self._row("record_continuous", rate_r, _fmt_pct(rate_r), how))

        # 5. 押运路线偏离告警时延
        stats = self._latency(("ESCORT_ROUTE_DEVIATE",))
        worst = stats.get("ESCORT_ROUTE_DEVIATE", {}).get("max")
        n = stats.get("ESCORT_ROUTE_DEVIATE", {}).get("n", 0)
        how = ("共 %d 次偏离告警" % n) if n else "本机暂无押运偏离样本（需现场演练）"
        out.append(self._row("escort_deviate_latency", worst, _fmt_sec(worst), how))
        return out

    def _row(self, key, actual, actual_text, how):
        d = config.KPI_DEFS[key]
        return {"key": key, "name": d["name"], "required": d["required"],
                "actual": actual, "actual_text": actual_text,
                "verdict": _verdict(actual, d["op"], d["target"]),
                "target": d["target"], "op": d["op"], "unit": d["unit"], "how": how}

    def _latency(self, codes):
        """从 alarms 表按码统计时延（max/avg/n）。"""
        out = {}
        for a in self.store.alarms(limit=10000):
            if a.code not in codes or a.latency_sec is None:
                continue
            d = out.setdefault(a.code, {"n": 0, "max": 0.0, "sum": 0.0})
            d["n"] += 1
            d["max"] = max(d["max"], float(a.latency_sec))
            d["sum"] += float(a.latency_sec)
        for d in out.values():
            d["avg"] = round(d["sum"] / d["n"], 3)
            d["max"] = round(d["max"], 3)
            del d["sum"]
        return out

    def summary(self, room_id=None):
        rows = self.kpi(room_id)
        ok = sum(1 for r in rows if r["verdict"] == "达标")
        no = sum(1 for r in rows if r["verdict"] == "未达标")
        na = sum(1 for r in rows if r["verdict"] not in ("达标", "未达标"))
        return {"rows": rows, "pass": ok, "fail": no, "insufficient": na,
                "total": len(rows)}


class EvidenceReport(object):
    """验收证据（文档第四节）。"""

    def __init__(self, store, vault=None, flow=None, kpi=None):
        self.store = store
        self.vault = vault
        self.flow = flow
        self.kpi = kpi or KPIReport(store, vault, flow)

    # ---------- 逐机位台账（无死角核查报告的数据基础） ----------
    def camera_ledger(self, room_id=None):
        rows = []
        for c in self.store.cameras(room_id):
            rows.append({
                "cam_id": c.cam_id, "room_id": c.room_id, "name": c.name,
                "face": c.face, "face_text": config.FACE_TEXT.get(c.face, c.face),
                "url": c.url, "angle_deg": c.angle_deg,
                "checkpoints": "、".join(c.checkpoints),
                "channels": "、".join(c.channels),
                "online": "在线" if c.online else "离线",
                "problems": "；".join(c.ledger_problems()) or "无",
            })
        return rows

    def coverage_report(self, room_id):
        rate, detail = self.vault.cameras.coverage(room_id)
        rows = []
        for f in config.COVER_FACES:
            d = detail[f]
            rows.append({
                "face_text": d["face_text"],
                "cams": "、".join(d["cams"]) or "未登记",
                "configured": "已配置" if d["configured"] else "缺失",
                "checkpoints": "、".join(config.FACE_CHECKPOINTS[f]),
                "missing": "、".join(d["missing_checkpoints"]) or "无",
            })
        return {"room_id": room_id, "rate": rate, "rows": rows,
                "blind_spots": self.vault.cameras.blind_spots(room_id)}

    # ---------- 导出 ----------
    def export_csv(self, rows, path, headers=None):
        """导出 CSV。**没有数据也写出文件（仅表头或空文件）**——验收附件里
        "这次确实导了、只是当期无记录"和"根本没导"是两件事，不能都返回空串。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not rows:
                # 无数据：写出空文件（占位，便于确认导出动作已执行）
                path.write_text("", encoding="utf-8-sig")
                return str(path)
            headers = headers or list(rows[0].keys())
            with open(str(path), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=headers)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in headers})
            return str(path)
        except OSError:
            return ""

    def export_text(self, text, path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    # ---------- 全量报告 ----------
    def full_report(self, room_id=None, generated_by="system"):
        lines = []
        A = lines.append
        A("=" * 78)
        A("试卷流转与保密室智能监控 · 验收报告")
        A("生成时间：%s    生成人：%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), generated_by))
        if room_id:
            A("保密室编号：%s" % room_id)
        A("=" * 78)

        # 一、六面无死角核查
        A("")
        A("【一】保密室无死角核查（含机位图数据与实拍画面清单）")
        A("-" * 78)
        if room_id and self.vault:
            cr = self.coverage_report(room_id)
            A("六面覆盖率：%.0f%%（要求 100%%，无盲区）" % (cr["rate"] * 100))
            if cr["blind_spots"]:
                A("!! 盲区朝向：%s" % "、".join(cr["blind_spots"]))
            A("")
            A("%-12s %-22s %-10s %-28s %s"
              % ("朝向", "机位", "状态", "该朝向必拍要点", "未覆盖要点"))
            for r in cr["rows"]:
                A("%-12s %-22s %-10s %-28s %s"
                  % (r["face_text"], r["cams"], r["configured"], r["checkpoints"],
                     r["missing"]))
        else:
            A("未指定保密室编号；下列为全部机位台账：")
        A("")
        led = self.camera_ledger(room_id)
        A("机位台账（逐机位留档验证）：共 %d 个" % len(led))
        A("%-14s %-12s %-16s %-10s %-8s %-22s %s"
          % ("机位ID", "保密室", "名称", "朝向", "状态", "冗余通道", "台账问题"))
        for r in led:
            A("%-14s %-12s %-16s %-10s %-8s %-22s %s"
              % (r["cam_id"], r["room_id"], r["name"], r["face_text"], r["online"],
                 r["channels"], r["problems"]))

        # 二、门禁与开柜告警联调记录
        A("")
        A("【二】门禁与开柜告警联调测试记录")
        A("-" * 78)
        acc = self.store.access_events(limit=30)
        A("门禁事件（最近 %d 条）：" % len(acc))
        A("%-22s %-12s %-24s %-8s %s"
          % ("时间", "人员", "因子/到场", "结果", "拒绝原因"))
        for e in acc:
            A("%-22s %-12s %-24s %-8s %s"
              % (e.ts, e.person or "-",
                 "%s / %d人" % ("+".join(e.factors) or "-", len(e.persons)),
                 "放行" if e.result == "allow" else "拒绝", e.deny_reason or "-"))
        safes = self.store.safe_events(limit=30)
        A("")
        A("试卷柜开柜事件（最近 %d 条）：" % len(safes))
        A("%-22s %-12s %-14s %-10s %s" % ("时间", "柜号", "人员", "授权", "状态"))
        for e in safes:
            A("%-22s %-12s %-14s %-10s %s"
              % (e.ts, e.safe_id, e.person or "-",
                 "是" if e.authorized else "否", e.state))
        intr = self.store.intrusion_events(limit=20)
        A("")
        A("入侵探测事件（最近 %d 条）：" % len(intr))
        for e in intr:
            A("  %s  %s  %s  布防=%s" % (e.ts, config.INTRUSION_KINDS.get(e.kind, e.kind),
                                        e.zone or "-", "是" if e.armed else "否"))

        # 三、交接/押运演练记录与台账样例
        A("")
        A("【三】交接 / 押运流程演练记录与台账样例")
        A("-" * 78)
        hs = self.store.handovers(limit=30)
        A("交接记录（最近 %d 条）：" % len(hs))
        A("%-22s %-12s %-10s %-16s %-8s %-8s %s"
          % ("时间", "试卷袋", "环节", "交接双方", "人数", "人脸", "是否可追溯"))
        for h in hs:
            A("%-22s %-12s %-10s %-16s %-8d %-8d %s"
              % (h.ts, h.bag_id, h.stage,
                 "%s->%s" % (h.from_person or "-", h.to_person or "-"),
                 len(h.persons), len(h.faces),
                 "是" if h.complete() else "否（%s）"
                 % "、".join(c for c, _ in h.problems)))
        A("")
        tasks = self.store.escort_tasks()
        A("押运任务（共 %d 个）：" % len(tasks))
        for t in tasks:
            s = self.flow.escort.task_summary(t.task_id) if self.flow else {}
            A("  %s 车 %s 驾驶员 %s 状态 %s 轨迹点 %d 偏离点 %d 最大偏移 %.0fm 视频 %s"
              % (t.start_ts, t.plate, t.driver or "-", t.status,
                 s.get("points", 0), s.get("deviated_points", 0),
                 s.get("max_offset_m", 0.0),
                 "在线" if t.video_online else "中断"))
        A("")
        dl = self.store.deliveries(limit=30)
        A("分发至考场记录（最近 %d 条）：" % len(dl))
        for d in dl:
            A("  %s  %s -> 考场 %s  %d 份  责任人 %s"
              % (d.ts, d.bag_id, d.room_id, d.quantity, d.person or "-"))
        rc = self.store.recycles(limit=30)
        A("")
        A("回收与封存记录（最近 %d 条）：" % len(rc))
        for r in rc:
            A("  %s  %s  回收 %d/%d  差异 %+d  封存 %s"
              % (r.ts, r.bag_id, r.returned_qty, r.issued_qty, r.diff,
                 "是" if r.sealed else "否"))
        A("")
        A("试卷袋电子铅封状态：")
        for b in self.store.bags():
            A("  %s  科目 %s  数量 %d  封签 %s  状态 %s"
              % (b.bag_id, b.subject or "-", b.quantity, b.seal_id or "-",
                 b.seal_state))

        # 四、录像完整性与留存配置
        A("")
        A("【四】录像完整性与留存期限配置证明")
        A("-" * 78)
        A("留存期限：%d 天（建议 ≥ 180 天）；写入即锁定（WORM）：%s；段间容许缝隙：%.1f 秒"
          % (config.RECORD_RETAIN_DAYS, "开启" if config.RECORD_WORM else "关闭",
             config.RECORD_GAP_TOLERANCE_SEC))
        cams = self.store.cameras(room_id)
        A("逐机位频道连续性：")
        for c in cams:
            chs = self.vault.records.channels(c.cam_id) if self.vault else []
            if not chs:
                A("  %s  无录像段记录" % c.cam_id)
                continue
            for ch in chs:
                r = self.vault.records.continuity(c.cam_id, ch)
                A("  %-14s %-8s 段数 %-5d 连续 %s%s"
                  % (c.cam_id, ch, r["segments"], "是" if r["ok"] else "否",
                     "" if r["ok"] else "（缺口 %d 段）" % len(r["gaps"])))
        if self.vault:
            ret = self.vault.records.retention_report()
            A("留存核查：共 %d 段，其中留存期内 %d 段，未加锁 %d 段（应为 0），已过期 %d 段"
              % (ret["total"], ret["within_retention"], ret["unlocked"],
                 ret["expired"]))

        # 五、关键指标
        A("")
        A("【五】关键指标")
        A("-" * 78)
        s = self.kpi.summary(room_id)
        A("%-24s %-26s %-14s %s" % ("指标项", "要求", "实测", "结论"))
        for r in s["rows"]:
            A("%-24s %-26s %-14s %s" % (r["name"], r["required"], r["actual_text"],
                                        r["verdict"]))
        A("")
        A("汇总：达标 %d 项 / 未达标 %d 项 / 样本不足 %d 项（共 %d 项）"
          % (s["pass"], s["fail"], s["insufficient"], s["total"]))
        A("")
        A("逐项数据来源说明：")
        for r in s["rows"]:
            A("  · %s：%s" % (r["name"], r["how"]))

        # 六、告警台账
        A("")
        A("【六】告警台账（最近 50 条）")
        A("-" * 78)
        A("%-22s %-8s %-26s %-14s %-10s %s"
          % ("时间", "级别", "告警项", "来源", "时延", "状态"))
        for a in self.store.alarms(limit=50):
            A("%-22s %-8s %-26s %-14s %-10s %s"
              % (a.created_at, a.level_name(), a.title, a.source_id or "-",
                 ("%.3fs" % a.latency_sec) if a.latency_sec is not None else "—",
                 a.status))

        # 七、审计链
        A("")
        A("【七】审计哈希链校验")
        A("-" * 78)
        ok, why = self.store.audit_verify()
        A("审计链：%s（%s）" % ("校验通过" if ok else "校验失败", why))

        A("")
        A("=" * 78)
        A("说明：涉及真实摄像头/传感器/录像平台/北斗定位的指标需现场核验，")
        A("      本报告只呈现**系统实测到的样本**；无样本的指标标「样本不足」，")
        A("      不视为达标。")
        A("=" * 78)
        return "\n".join(lines)

    def save_report(self, room_id=None, generated_by="system"):
        text = self.full_report(room_id, generated_by)
        name = "验收报告_%s%s.txt" % (room_id + "_" if room_id else "",
                                   time.strftime("%Y%m%d_%H%M%S"))
        path = config.REPORT_DIR / name
        self.export_text(text, path)
        return str(path), text

    def export_ledgers(self):
        """一次导出全部台账 CSV（验收附件）。无记录的台账也落一个空文件。"""
        out = {}
        out["camera_ledger"] = self.export_csv(
            self.camera_ledger(), config.EXPORT_DIR / "机位台账.csv")
        out["handovers"] = self.export_csv(
            [h.to_row() for h in self.store.handovers(limit=10000)],
            config.EXPORT_DIR / "交接台账.csv")
        out["deliveries"] = self.export_csv(
            [d.to_row() for d in self.store.deliveries(limit=10000)],
            config.EXPORT_DIR / "分发台账.csv")
        out["recycles"] = self.export_csv(
            [r.to_row() for r in self.store.recycles(limit=10000)],
            config.EXPORT_DIR / "回收台账.csv")
        out["alarms"] = self.export_csv(
            [a.to_row() for a in self.store.alarms(limit=10000)],
            config.EXPORT_DIR / "告警台账.csv")
        out["escort"] = self.export_csv(
            [t.to_row() for t in self.store.escort_tasks()],
            config.EXPORT_DIR / "押运任务台账.csv")
        return out
