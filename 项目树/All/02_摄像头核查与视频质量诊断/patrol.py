# -*- coding: utf-8 -*-
"""
三级巡检（《摄像头核查与视频质量诊断.md》第三节）与关键指标

日常自动巡检  每 30 分钟，全部考点        -> 在线率、质量异常清单
考前专项核查  考前 7 天，全部考场          -> 逐考场核查报告、整改工单
考前一小时自检 开考前 30 分钟，本场全部机位 -> 自检通过确认、异常即时报修

所有外部探测器（取流/录像/时间/音频）都可注入，便于离线自检与单测；
未注入时走真实探针，探测不到就如实记缺陷，不假装正常。
"""
import os
import time

import config
import diagnose
import models
import probe


class PatrolRunner(object):
    def __init__(self, store, frame_provider=None, recording_provider=None,
                 time_provider=None, audio_provider=None, diagnoser=None,
                 baseline_store=None):
        self.store = store
        self.dg = diagnoser or diagnose.Diagnoser()
        self.baselines = baseline_store
        self._frame = frame_provider
        self._rec = recording_provider
        self._time = time_provider
        self._audio = audio_provider

    # ---------------- 单机位核查 ----------------
    def check_device(self, device, policy=config.POLICY_DAILY, frame=None,
                     prev_deltas=None, segments=None, times=None, audio=None,
                     operator="", note=""):
        """对一台设备做一次完整核查，返回 models.CheckResult"""
        defects = []
        metrics = {}
        online = True

        # 1) 心跳
        hb_ok, hb_why = probe.heartbeat_status(device)
        metrics["heartbeat"] = hb_why
        if not hb_ok:
            defects.append(models.Defect("HEARTBEAT_LOST", {"reason": hb_why}, hb_why))

        # 2) 台账核对
        problems = device.ledger_problems()
        if problems:
            defects.append(models.Defect("LEDGER_MISMATCH", {"problems": problems},
                                         "；".join(problems)))

        # 3) 取流 + 画质/视野诊断
        if frame is None:
            if self._frame is not None:
                got, frame, why = self._frame(device)
            elif self.baselines is not None:
                got, frame, why = self.baselines.grab(device)
            else:
                got, frame, why = False, None, "未配置取流方式"
            if not got:
                online = False
                defects.append(models.Defect("STREAM_FAIL", {"reason": why}, why))
        if frame is not None:
            base_gray = base_metrics = None
            base_path = ""
            if self.baselines is not None:
                rec, base_gray, base_metrics = self.baselines.get(device)
                if rec:
                    base_path = rec.get("path", "")
                if base_gray is None:
                    miss = self.baselines.missing_reason(device)
                    if miss:
                        metrics["baseline_missing_reason"] = miss
            d, m = self.dg.diagnose(frame, baseline_gray=base_gray,
                                    baseline_metrics=base_metrics,
                                    prev_deltas=prev_deltas)
            defects.extend(d)
            metrics.update(m)
            metrics["baseline_path"] = base_path

        # 4) 录像完整性
        segs = segments if segments is not None else \
            (self._rec(device) if self._rec is not None else None)
        if segs is not None:
            rc = probe.recording_completeness(segs)
            metrics["recording"] = rc
            if rc.get("missing"):
                defects.append(models.Defect("REC_MISSING", rc, rc.get("reason", "")))
            elif not rc.get("ok"):
                defects.append(models.Defect("REC_GAP", rc, rc.get("reason", "")))

        # 5) 时间一致性
        if times is not None or self._time is not None:
            tw = times if times is not None else self._time(device)
            if tw:
                ok, drift, why = probe.time_drift(tw[0], tw[1])
                metrics["time_drift_sec"] = drift
                if not ok:
                    defects.append(models.Defect("TIME_DRIFT", {"drift_sec": drift}, why))

        # 6) 音频链路
        if device.has_audio and (audio is not None or self._audio is not None):
            aw = audio if audio is not None else self._audio(device)
            if aw:
                ar = probe.audio_level(aw[0], aw[1] if len(aw) > 1 else 16000)
                metrics["audio"] = ar
                if not ar["ok"]:
                    code = "AUDIO_NOISE" if "噪声" in ar["reason"] else "AUDIO_SILENT"
                    defects.append(models.Defect(code, ar, ar["reason"]))

        ck = models.CheckResult(device, policy, online=online, defects=defects,
                                metrics=metrics, operator=operator, note=note)
        self.store.save_check(ck)
        self.store.log("机位核查",
                       "%s %s -> %s（缺陷 %d）"
                       % (device.code, config.PATROLS.get(policy, {}).get("name", policy),
                          "正常" if ck.ok else "异常", len(defects)),
                       operator or "system")
        return ck

    # ---------------- 批量巡检 ----------------
    def run(self, policy=config.POLICY_DAILY, devices=None, room_id=None, session=None,
            operator="", prev_deltas_map=None):
        """按策略跑一轮；返回汇总 dict"""
        if devices is None:
            devices = self.store.list_devices(room_id=room_id, session=session)
        results = []
        for dev in devices:
            pd = (prev_deltas_map or {}).get(dev.code)
            results.append(self.check_device(dev, policy=policy, prev_deltas=pd,
                                             operator=operator))
        return self.summarize(policy, results)

    @staticmethod
    def summarize(policy, results):
        total = len(results)
        online = sum(1 for r in results if r.online)
        bad = [r for r in results if not r.ok]
        return {"policy": policy,
                "name": config.PATROLS.get(policy, {}).get("name", policy),
                "total": total, "online": online,
                "online_rate": round(online / total, 4) if total else None,
                "ok": total - len(bad), "bad": len(bad),
                "bad_devices": [r.device_code for r in bad],
                "results": results}

    # ---------------- 三级巡检的排期辅助 ----------------
    @staticmethod
    def daily_due(last_run_epoch, now=None):
        """日常巡检是否到点（默认每 30 分钟）"""
        interval = config.PATROLS[config.POLICY_DAILY]["interval_min"] * 60
        now = now if now is not None else models.now_epoch()
        return (now - float(last_run_epoch or 0)) >= interval

    @staticmethod
    def preexam_window(exam_epoch, now=None):
        """考前专项核查是否在窗口内（考前 7 天）"""
        lead = config.PATROLS[config.POLICY_PREEXAM]["lead_days"] * 86400
        now = now if now is not None else models.now_epoch()
        left = float(exam_epoch) - now
        return 0 <= left <= lead, left

    @staticmethod
    def before_session_window(exam_epoch, now=None):
        """考前一小时自检是否到点（开考前 30 分钟）"""
        lead = config.PATROLS[config.POLICY_BEFORE]["lead_min"] * 60
        now = now if now is not None else models.now_epoch()
        left = float(exam_epoch) - now
        return 0 < left <= lead, left

    # ---------------- 关键指标 ----------------
    def kpi(self, since_epoch=None, policy=None):
        rate = self.store.online_rate(since_epoch=since_epoch, policy=policy)
        checks = self.store.list_checks(since_epoch=since_epoch, policy=policy, limit=10 ** 6)
        # 故障自动发现时延：从"设备最后一次心跳"到"首次判定失败"的间隔
        latencies = []
        for ck in checks:
            if not ck.online:
                dev = self.store.get_device(device_id=ck.device_id)
                if dev and dev.last_heartbeat:
                    latencies.append(max(0.0, ck.epoch - dev.last_heartbeat))
        # 考前核查完成率：已核查机位 / 应核查机位
        devices = self.store.list_devices()
        covered = {ck.device_id for ck in self.store.list_checks(
            policy=config.POLICY_PREEXAM, limit=10 ** 6)}
        completion = (len([d for d in devices if d.device_id in covered]) / len(devices)
                      if devices else None)
        # 覆盖完整性：已采集基准图的比例（无基准无法谈视野核查）
        base_ok = sum(1 for d in devices if self.baselines and
                      self.baselines.coverage_map_ok(d)) if self.baselines else 0
        coverage = (base_ok / len(devices)) if devices else None
        # 时钟一致性：最近一次实测偏差
        drifts = [ck.metrics.get("time_drift_sec") for ck in checks
                  if isinstance(ck.metrics.get("time_drift_sec"), (int, float))]
        max_drift = max((abs(x) for x in drifts), default=None)
        return {
            "在线率": {"value": rate["rate"], "target": config.KPI["online_rate_min"],
                       "pass": (rate["rate"] is not None
                                and rate["rate"] >= config.KPI["online_rate_min"]),
                       "samples": rate["samples"], "online": rate["online"]},
            "故障发现时延": {"max_sec": round(max(latencies), 1) if latencies else None,
                             "target_sec": config.KPI["discover_latency_sec"],
                             "pass": (bool(latencies)
                                      and max(latencies) <= config.KPI["discover_latency_sec"]),
                             "samples": len(latencies)},
            "覆盖完整性": {"value": coverage, "target": config.KPI["coverage_rate_min"],
                           "pass": coverage is not None
                                   and coverage >= config.KPI["coverage_rate_min"],
                           "baseline_devices": base_ok, "devices": len(devices)},
            "考前核查完成率": {"value": completion, "target": config.KPI["preexam_complete_min"],
                               "pass": completion is not None
                                       and completion >= config.KPI["preexam_complete_min"],
                               "checked": len(covered), "devices": len(devices)},
            "时钟一致性": {"max_drift_sec": round(max_drift, 3) if max_drift is not None else None,
                           "target_sec": config.KPI["clock_drift_max_sec"],
                           "pass": max_drift is not None
                                   and max_drift <= config.KPI["clock_drift_max_sec"],
                           "samples": len(drifts)},
        }

    # ---------------- 与巡考联动：告警可信度降权 ----------------
    def credibility_factor(self, device_code):
        """
        质量异常点位的告警可信度自动降权（第四节第 4 条）。
        依据该机位最近一次核查结果：有 fail -> ×0.5，仅 warn -> ×0.8，正常 -> ×1.0。
        返回 (factor, 说明)
        """
        dev = self.store.get_device(code=device_code)
        if dev is None:
            return config.CREDIBILITY["normal_factor"], "未知机位，不降权"
        ck = self.store.latest_check(dev.device_id)
        if ck is None:
            return config.CREDIBILITY["normal_factor"], "尚无核查记录，不降权"
        codes = {d.code for d in ck.defects}
        cfg = config.CREDIBILITY
        if codes & set(config.FAIL_CODES):
            f = max(cfg["min_factor"], cfg["fail_factor"])
            return f, "该机位存在 %s，可信度 ×%.1f" % (
                "、".join(config.defect_name(c) for c in sorted(codes & set(config.FAIL_CODES))), f)
        if codes & set(config.WARN_CODES):
            f = max(cfg["min_factor"], cfg["warn_factor"])
            return f, "该机位存在画质告警 %s，可信度 ×%.1f" % (
                "、".join(config.defect_name(c) for c in sorted(codes & set(config.WARN_CODES))), f)
        return cfg["normal_factor"], "机位正常，不降权"

    def adjust_alert_score(self, device_code, score):
        f, why = self.credibility_factor(device_code)
        try:
            s = float(score) * f
        except (TypeError, ValueError):
            s = score
        return s, f, why


def now_str(epoch=None):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(epoch if epoch else time.time()))


def exists(path):
    return bool(path) and os.path.exists(str(path))
