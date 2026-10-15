# -*- coding: utf-8 -*-
"""
告警中心（AlarmHub）

职责：
  · 接入保密室/流转两侧的所有告警，统一分级（橙/红）
  · 去重：同来源同码在时间窗内只保留一条（避免"柜门一直虚掩"刷屏）
  · **记录时延**：事件 epoch -> 告警产生 epoch 的差值，专用于核查
    「开柜/开门异常告警时延 ≤ 5 秒」「押运路线偏离 ≤ 30 秒」这两条指标
  · 抓帧留证（可选）：告警发生时从对应机位取一帧落盘
  · 全部接入动作写哈希链审计
"""
import json
import time

import config
import models

DEDUP_WINDOW_SEC = 60


class AlarmHub(object):
    def __init__(self, store, camera_manager=None):
        self.store = store
        self.cameras = camera_manager          # 可选：用于抓帧留证

    # ---------- 接入 ----------
    def raise_alarm(self, code, detail="", source_kind="", source_id="",
                    scope="", level=None, event_epoch=None, evidence_cam=None,
                    extra=None):
        """产生一条告警。

        event_epoch：**事件**发生的时刻（不是调用时刻）。给了它就会算出时延，
        用于指标核查；不给则时延为 None，指标里按"样本不足"处理，
        绝不假装达标。
        """
        now = models.now_epoch()
        latency = None
        if event_epoch is not None:
            latency = max(0.0, now - float(event_epoch))
        alarm = models.Alarm(code, detail=detail, source_kind=source_kind,
                             source_id=source_id, scope=scope, level=level,
                             latency_sec=round(latency, 3) if latency is not None else None,
                             extra=extra)
        # 去重命中时返回**已存在的那条**（调用方拿到的永远是 Alarm 对象，
        # 不必关心是新建还是折叠；是否新建可另用 create_alarm() 判断）
        got, _created = self._ingest(alarm, evidence_cam=evidence_cam)
        return got

    def create_alarm(self, *args, **kwargs):
        """同 raise_alarm，但返回 (alarm, created)。created=False 表示被去重折叠。"""
        return self._ingest(models.Alarm(*args, **kwargs),
                            evidence_cam=kwargs.get("evidence_cam"))

    def _ingest(self, alarm, dedup=True, evidence_cam=None):
        if alarm.level not in config.LEVELS:
            alarm.level = config.ALERT_CODE_LEVEL.get(alarm.code, config.LEVEL_ORANGE)
        if dedup:
            dup = self._find_dup(alarm)
            if dup is not None:
                return dup, False
        if evidence_cam and config.SNAPSHOT_ON_ALARM and self.cameras is not None:
            try:
                path = self.cameras.snapshot(evidence_cam, reason=alarm.code)
                if path:
                    alarm.evidence = path
            except Exception:
                pass
        self.store.save_alarm(alarm)
        self.store.log("告警接入",
                       "%s %s %s %s" % (alarm.level, alarm.code,
                                        alarm.scope or "-", alarm.source_id or "-"),
                       "alarm_hub")
        return alarm, True

    def _find_dup(self, alarm):
        """同 告警码 + 来源类型 在窗口内已有未处置的，算重复。

        为什么要用 SQL 精确查：同一台设备（如环境监测）在一个窗口内会持续
        上报，若只按"来源ID"去重则每条样本的 sample_id 都不同、根本挡不住刷屏；
        反过来若完全不看来源，又可能把两个保密室的同类告警错并成一条。
        所以按 **码 + 来源类型 + 对象(scope)** 三个维度去重：
        同一房间的水浸持续存在 -> 只留一条；换成另一个房间 -> 各留一条。
        """
        cutoff = alarm.created_epoch - DEDUP_WINDOW_SEC
        rows = self.store._query(
            "SELECT * FROM alarms WHERE code=? AND source_kind=? AND scope=? "
            "AND status=? AND created_epoch>=? ORDER BY created_epoch DESC LIMIT 1",
            (alarm.code, alarm.source_kind, alarm.scope,
             models.ST_PENDING, cutoff))
        if not rows:
            return None
        return models.Alarm.from_row(rows[0])

    # ---------- 处置 ----------
    def ack(self, alarm_id, operator, note=""):
        n = self.store.ack_alarm(alarm_id, operator, note)
        if n:
            self.store.log("告警确认", "%s %s" % (alarm_id, note or ""), operator)
        return bool(n)

    def handle(self, alarm_id, operator, note=""):
        n = self.store.handle_alarm(alarm_id, operator, note)
        if n:
            self.store.log("告警处置", "%s %s" % (alarm_id, note or ""), operator)
        return bool(n)

    def ack_all_red(self, operator, note="批量确认红色告警"):
        n = 0
        for a in self.store.alarms(status=models.ST_PENDING, limit=500):
            if a.is_red() and self.ack(a.alarm_id, operator, note):
                n += 1
        return n

    # ---------- 统计（供界面与报告） ----------
    def open_red_count(self):
        return len(self.store.open_red_alarms())

    def latency_stats(self, codes, limit=500):
        """按告警码统计时延（秒）。返回 {"code": {"n":n,"max":x,"avg":y}}。

        只统计**有 latency 记录**的告警；没有样本的码不出现在结果里
        （报告会如实标"样本不足"，不当作达标）。
        """
        out = {}
        for a in self.store.alarms(limit=limit):
            if a.code not in codes:
                continue
            if a.latency_sec is None:
                continue
            d = out.setdefault(a.code, {"n": 0, "max": 0.0, "sum": 0.0})
            d["n"] += 1
            d["max"] = max(d["max"], float(a.latency_sec))
            d["sum"] += float(a.latency_sec)
        for d in out.values():
            d["avg"] = round(d["sum"] / d["n"], 3) if d["n"] else None
            d["max"] = round(d["max"], 3)
            del d["sum"]
        return out

    def today_count(self):
        day = time.strftime("%Y-%m-%d")
        return sum(1 for a in self.store.alarms(limit=1000)
                   if (a.created_at or "").startswith(day))
