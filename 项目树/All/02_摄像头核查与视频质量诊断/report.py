# -*- coding: utf-8 -*-
"""
核查报告与验收证据（《摄像头核查与视频质量诊断.md》第五节、第六节）

产出：
  · 逐机位核查报告（含基准图与整改前后对比图路径）
  · 逐考场核查报告
  · 关键指标汇总（在线率 / 发现时延 / 覆盖完整性 / 考前完成率 / 时钟一致性）
  · 验收证据清单（markdown，可直接贴进项目书）
"""
import os
import time

import config
import models

KPI_TEXT = ("在线率", "故障发现时延", "覆盖完整性", "考前核查完成率", "时钟一致性")


class Reporter(object):
    def __init__(self, store, runner=None, orders=None):
        self.store = store
        self.runner = runner
        self.orders = orders

    # ---------------- 逐机位 ----------------
    def device_report(self, device, limit=20):
        ck = self.store.latest_check(device.device_id)
        hist = self.store.list_checks(device_id=device.device_id, limit=limit)
        base = self.store.get_baseline(device.device_id)
        bos = [wo for wo in self.store.list_orders(limit=500)
               if wo.device_id == device.device_id]
        lines = []
        lines.append("### 机位 %s（%s）" % (device.code, device.room_id or "未绑定考场"))
        lines.append("")
        lines.append("| 项目 | 内容 |")
        lines.append("| --- | --- |")
        lines.append("| 安装位置 | %s |" % (device.position or "—"))
        lines.append("| 取流地址 | %s |" % (device.url or "—"))
        lines.append("| 分辨率 | %s |" % device.resolution)
        lines.append("| 标准角度 | 视场 %.1f°，水平 %.1f°，俯仰 %.1f°"
                     % (device.fov_deg, device.pan_deg, device.tilt_deg))
        lines.append("| 基准图 | %s |" % (base["path"] if base else
                                        "**未采集**（" + self._miss(device) + "）"))
        lines.append("| 基准采集时间 | %s |" % (base["captured_at"] if base else "—"))
        lines.append("| 最近核查 | %s |" % (ck.ts if ck else "—"))
        if ck:
            lines.append("| 核查结论 | %s |" % ("正常" if ck.ok else "异常"))
            if ck.defects:
                items = "；".join("%s（%s）：%s"
                                 % (config.defect_name(d.code), d.severity, d.detail)
                                 for d in ck.defects)
                lines.append("| 缺陷明细 | %s |" % items)
            if ck.frame_path:
                lines.append("| 现场抓帧 | %s |" % ck.frame_path)
        lines.append("| 关联工单 | %s |" % (
            "、".join("%s(%s)" % (wo.order_id, wo.status) for wo in bos) if bos else "无"))
        lines.append("")
        if hist:
            lines.append("**最近核查记录**")
            lines.append("")
            lines.append("| 时间 | 策略 | 在线 | 结论 |")
            lines.append("| --- | --- | --- | --- |")
            for h in hist[:10]:
                lines.append("| %s | %s | %s | %s |"
                             % (h.ts, config.PATROLS.get(h.policy, {}).get("name", h.policy),
                                "是" if h.online else "否",
                                "正常" if h.ok else "异常：" + "、".join(
                                    config.defect_name(c) for c in
                                    (h.fail_codes() or h.warn_codes()))))
            lines.append("")
        if bos:
            lines.append("**整改闭环记录**")
            lines.append("")
            lines.append("| 工单 | 缺陷 | 状态 | 整改前 | 整改后 | 复核人 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for wo in bos:
                lines.append("| %s | %s | %s | %s | %s | %s |"
                             % (wo.order_id,
                                "、".join(config.defect_name(c) for c in wo.codes),
                                wo.status, os.path.basename(wo.before_path or "—"),
                                os.path.basename(wo.after_path or "—"),
                                wo.reviewer or "—"))
            lines.append("")
        return "\n".join(lines)

    def _miss(self, device):
        rec = self.store.get_baseline(device.device_id)
        if not rec:
            return "部署完成后需逐机位采集"
        return "基准图文件丢失"

    # ---------------- 逐考场 ----------------
    def room_report(self, room_id, policy=None):
        devices = self.store.list_devices(room_id=room_id)
        checks = self.store.list_checks(room_id=room_id, policy=policy, limit=10 ** 6)
        bad = [c for c in checks if not c.ok]
        lines = ["## 考场 %s 核查报告" % room_id, ""]
        lines.append("- 机位总数：%d" % len(devices))
        lines.append("- 本次核查记录：%d 条（异常 %d 条）" % (len(checks), len(bad)))
        lines.append("- 生成时间：%s" % models.now_ts())
        lines.append("")
        if not devices:
            lines.append("> 该考场尚无登记机位。")
            return "\n".join(lines)
        lines.append("| 机位 | 位置 | 在线 | 结论 | 主要缺陷 | 基准图 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for d in devices:
            ck = self.store.latest_check(d.device_id)
            base = self.store.get_baseline(d.device_id)
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (d.code, d.position or "—",
                            ("是" if ck.online else "否") if ck else "—",
                            ("正常" if ck.ok else "异常") if ck else "未核查",
                            "、".join(config.defect_name(c)
                                      for c in ((ck.fail_codes() or ck.warn_codes())
                                                if ck else [])) or "—",
                            "有" if base else "**缺**"))
        lines.append("")
        lines.append("### 逐机位明细")
        lines.append("")
        for d in devices:
            lines.append(self.device_report(d, limit=5))
        return "\n".join(lines)

    # ---------------- 关键指标 ----------------
    def kpi_report(self, since_epoch=None):
        if self.runner is None:
            return "> 未接入巡检运行器，无法汇总关键指标。"
        k = self.runner.kpi(since_epoch=since_epoch)
        lines = ["## 关键指标", "",
                 "| 指标项 | 实测 | 要求 | 结论 |", "| --- | --- | --- | --- |"]
        on = k["在线率"]
        lines.append("| 摄像机在线率 | %s | ≥ %.0f%% | %s |"
                     % (_pct(on["value"]), on["target"] * 100, _mark(on["pass"])))
        dl = k["故障发现时延"]
        lines.append("| 故障自动发现时延 | %s | ≤ %d 秒 | %s |"
                     % (("%.1f 秒" % dl["max_sec"]) if dl["max_sec"] is not None else "无样本",
                        dl["target_sec"], _mark(dl["pass"])))
        cv = k["覆盖完整性"]
        lines.append("| 覆盖完整性核查 | %s（%d/%d 机位已采集基准图） | 100%% | %s |"
                     % (_pct(cv["value"]), cv["baseline_devices"], cv["devices"],
                        _mark(cv["pass"])))
        pc = k["考前核查完成率"]
        lines.append("| 考前核查完成率 | %s（%d/%d 机位） | 100%% | %s |"
                     % (_pct(pc["value"]), pc["checked"], pc["devices"], _mark(pc["pass"])))
        ck = k["时钟一致性"]
        lines.append("| 时钟一致性偏差 | %s | ≤ %.1f 秒 | %s |"
                     % (("%.3f 秒" % ck["max_drift_sec"])
                        if ck["max_drift_sec"] is not None else "无样本",
                        ck["target_sec"], _mark(ck["pass"])))
        lines.append("")
        lines.append("> 未达标的指标说明证据仍不足：在线率/时延/时钟需要实际巡检样本，"
                     "覆盖率需要逐机位采集基准图。**统计为 0 不代表达标。**")
        return "\n".join(lines)

    # ---------------- 汇总报告 ----------------
    def full_report(self, since_epoch=None, policy=None):
        devices = self.store.list_devices()
        rooms = self.store.rooms()
        checks = self.store.list_checks(since_epoch=since_epoch, policy=policy, limit=10 ** 6)
        bad = [c for c in checks if not c.ok]
        lines = ["# 摄像头核查与视频质量诊断报告", "",
                 "- 生成时间：%s" % models.now_ts(),
                 "- 数据目录：%s" % config.DATA_ROOT,
                 "- 机位总数：%d　考场数：%d" % (len(devices), len(rooms)),
                 "- 核查样本：%d 条（异常 %d 条）" % (len(checks), len(bad)), ""]
        lines.append(self.kpi_report(since_epoch=since_epoch))
        lines.append("")
        lines.append("## 缺陷汇总")
        lines.append("")
        stats = self.store.defect_stats(since_epoch=since_epoch)
        if stats:
            lines.append("| 缺陷码 | 名称 | 级别 | 次数 | 涉及机位 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for s in stats:
                lines.append("| %s | %s | %s | %d | %d |"
                             % (s["code"], s["name"], s["severity"], s["count"],
                                s["device_count"]))
        else:
            lines.append("> 暂无缺陷记录。")
        lines.append("")
        if self.orders is not None:
            st = self.orders.stats()
            lines.append("## 整改闭环")
            lines.append("")
            lines.append("- 工单总数：%d　已闭环：%d　闭环率：%s"
                         % (st["total"], st["closed"], _pct(st["close_rate"])))
            lines.append("- 状态分布：%s" % ("、".join("%s=%d" % (k, v)
                                                    for k, v in sorted(
                                                        st["by_status"].items())) or "无"))
            if st["oldest_pending"]:
                op = st["oldest_pending"]
                lines.append("- 最久未闭环：%s（%s，%s，建单 %s）"
                             % (op["order_id"], op["device"], op["status"], op["created_at"]))
            lines.append("")
        lines.append("## 逐考场报告")
        lines.append("")
        for r in rooms:
            lines.append(self.room_report(r, policy=policy))
            lines.append("")
        lines.append("## 验收证据清单")
        lines.append("")
        lines.append("| 证据 | 本系统对应产出 |")
        lines.append("| --- | --- |")
        lines.append("| 逐考场/逐机位核查报告（含基准图与整改前后对比图） | 本报告的逐考场/逐机位章节，"
                     "基准图见 `data\\baselines\\`，整改前后图见 `data\\attachments\\` |")
        lines.append("| 考前专项核查记录与整改闭环记录 | 巡检记录见 `checks` 表（策略=preexam），"
                     "闭环记录见报告「整改闭环」章节 |")
        lines.append("| 在线率与故障处理时效统计（试运行期） | 报告「关键指标」章节 |")
        lines.append("")
        return "\n".join(lines)

    def export(self, text, name=None):
        name = name or ("核查报告_%s.md" % time.strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(str(config.REPORT_DIR), name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return ""
        self.store.log("导出核查报告", name)
        return path


def _pct(v):
    return "—" if v is None else "%.2f%%" % (float(v) * 100)


def _mark(ok):
    return "达标" if ok else "**未达标/样本不足**"
