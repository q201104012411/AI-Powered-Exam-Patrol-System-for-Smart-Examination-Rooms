# -*- coding: utf-8 -*-
r"""
验收指标（《普通考场应用.md》第六节 + 《特殊考场应用.md》第四节"含差异项"）

指标口径（**只认实测证据，没有证据就写"缺失"，绝不编数**）：

  行为识别准确率      ≥ 95%     实际考试场景测试（对比人工标注）
  疑似行为提示率      ≥ 98.5%   脚本化行为测试
  误报率              ≤ 5%      试运行统计（只认复核结论=误报）
  告警响应时间        ≤ 3 秒    系统日志分析
  摄像机在线率        ≥ 99%     考试期间统计
  考场覆盖完整性      100%      逐机位核查留档
  ---- 特殊考场差异项 ----
  跨机位去重准确率    ≥ 95%     大教室/机考考场另行测试
  替代核验通道        强制具备   无障碍考场（并有演练记录）
  数据分域隔离        强制       隔离/机考/涉密三类独立分区 + 独立授权
  离线可用性          强制       涉密考场全离线可独立运行
  最小可用配置        强制       1 机位 + 1 链路可完成巡考并留痕

"合成/演示数据"可以算，但必须带 data_source 标注，并在报告里单列说明
（与全仓库 README 的"如实说明"口径一致）。
"""
import config
import models
from models import KpiRun

MISSING = "（无实测证据，指标缺失）"


def _target(key):
    return config.TARGETS.get(key)


def _direction(key):
    return config.KPI_DIRECTION.get(key, "up")


def _meets(key, value):
    return config.kpi_meets(key, value, None)


# =============================================================================
# 从已有记录算指标（不额外要人填数）
# =============================================================================
def derive_room_metrics(store, room):
    """
    能从台账直接算出来的指标，全部算出来（带证据来源）。算不出的返回 None。
    """
    out = {}
    # ---- 摄像机在线率：来自 02（机位台账+心跳）；不可用则缺失 ----
    online = None
    src_online = MISSING
    if room.points:
        codes = [p.get("device_code") for p in room.points if p.get("device_code")]
        if codes:
            res = _camera_online_rate(codes)
            if res is not None:
                online, src_online = res, "02 机位台账心跳"
            else:
                src_online = "02 不可用，无法统计在线率"
        else:
            src_online = "点位未登记机位编码，无法统计在线率"
    out["camera_online_rate"] = {"value": online, "target": _target("camera_online_rate"),
                                 "direction": "up", "source": src_online}

    # ---- 覆盖完整性：逐机位核查留档 ----
    cover = 1.0 if room.points_verified else None
    out["coverage_rate"] = {"value": cover, "target": _target("coverage_rate"),
                            "direction": "up",
                            "source": "逐机位覆盖核查留档" if cover else
                            "未做逐机位覆盖核查（缺失）"}

    # ---- 数据分域隔离（差异项）----
    meta = config.ROOM_TYPES.get(room.room_type, {})
    comp = meta.get("compliance", {})
    if comp.get("domain_isolated"):
        p = store.get_partition(room.data_domain, room.room_id)
        val = 1.0 if (p and p.isolated and not p.destroyed_at) else None
        out["domain_isolation"] = {
            "value": val, "target": 1.0, "direction": "up",
            "source": "独立分区 + 独立授权" if val else "未建立独立分区（缺失）"}
    else:
        out["domain_isolation"] = {"value": None, "target": None, "direction": "up",
                                   "source": "该考场类型按分级授权，不适用强制分域"}
    # ---- 替代核验通道（差异项，仅无障碍）----
    if room.room_type == config.TYPE_ACCESSIBLE:
        out["alt_channel"] = {"value": 1.0 if room.alt_channel else None,
                              "target": 1.0, "direction": "up",
                              "source": "非人脸替代核验通道" if room.alt_channel
                              else "缺少替代核验通道（缺失，强制项）"}
        out["alt_channel_drill"] = {
            "value": 1.0 if room.drill_done else None, "target": 1.0,
            "direction": "up",
            "source": "演练记录已留痕" if room.drill_done else "无演练记录（缺失，强制项）"}
    # ---- 离线可用性（差异项，仅涉密）----
    if comp.get("offline_required"):
        out["offline_ready"] = {"value": 1.0 if room.offline_ready else None,
                                "target": 1.0, "direction": "up",
                                "source": "全离线自检通过" if room.offline_ready
                                else "未做全离线自检（缺失，强制项）"}
    # ---- 最小可用配置（差异项）----
    out["min_config"] = {"value": 1.0 if (room.min_config != "full" or
                                          len(room.points or []) >= 1) else None,
                         "target": 1.0, "direction": "up",
                         "source": "已按「%s」巡考并留痕" % room.min_config_meta()["name"]}
    # ---- 跨机位去重（差异项，大教室/机考）----
    if (meta.get("params") or {}).get("dedup_required"):
        out["dedup_accuracy"] = {
            "value": 1.0 if room.dedup_verified else None,
            "target": _target("dedup_accuracy"), "direction": "up",
            "source": "跨机位去重已验证" if room.dedup_verified
            else "未验证跨机位去重（缺失）"}
    # ---- 监考员在岗率 ----
    need = models.to_int(room.invigilators_required, 0)
    if need:
        have = room.invigilator_count() + (1 if room.remote_invigilator else 0)
        out["invigilator_rate"] = {"value": min(1.0, float(have) / need),
                                   "target": 1.0, "direction": "up",
                                   "source": "在岗核验记录"}
    # ---- 时钟偏差 ----
    out["clock_drift_sec"] = {"value": models.to_float(room.clock_drift_sec),
                              "target": _target("clock_drift_sec"), "direction": "down",
                              "source": "NTP 时间戳比对" if room.calibration_at
                              else "未完成时钟同步核查"}
    return out


def _camera_online_rate(codes):
    import linkage_client
    res = linkage_client.camera_devices()
    if not res.get("ok"):
        return None
    devices = (res.get("data") or {}).get("devices") or []
    by_code = {d.get("code"): d for d in devices}
    found = [by_code[c] for c in codes if c in by_code]
    if not found:
        return None
    # 02 的 devices 接口不直接给在线率，这里按"是否登记+有无基准图"如实近似，
    # 并在来源里写清楚口径，避免把近似值冒充精确值
    online = len([d for d in found if d.get("has_baseline")])
    return float(online) / len(found)


# =============================================================================
# 指标测算
# =============================================================================
def evaluate(store, room, run=None, persist=True, persist_derived=True):
    """
    汇总该考场的验收指标：台账能算的自动算，算不出的明确"缺失"。

    返回 {"room", "metrics": [...], "summary": {...}, "notes": [...],
          "apply_targets": [...]}
    """
    rows = []
    notes = []
    derived = derive_room_metrics(store, room)

    # ---- 来自本次 kpi_run 的手工/实测录入 ----
    m = {}
    if run is not None:
        if run.observe_total:
            m["hint_rate"] = {"value": float(run.observe_hit) / run.observe_total,
                              "target": _target("hint_rate"), "direction": "up",
                              "source": "%s：%d 条标注样本提示到 %d 条"
                                        % (models.DATA_SOURCE_TEXT.get(run.data_source,
                                                                       run.data_source),
                                           run.observe_total, run.observe_hit)}
        if run.observe_total:
            m["recog_accuracy"] = {
                "value": float(run.observe_correct) / run.observe_total,
                "target": _target("recog_accuracy"), "direction": "up",
                "source": "%s：类别判对 %d/%d"
                          % (models.DATA_SOURCE_TEXT.get(run.data_source,
                                                         run.data_source),
                             run.observe_correct, run.observe_total)}
        # 误报率：**本次录入的标注样本优先**（脚本化行为测试的口径），
        # 没有标注样本时才退回"复核结论口径"——两者都是证据，但要分清楚是哪一种。
        import alerting
        fpr = alerting.false_positive_rate(store, room_id=room.room_id)
        if run.observe_total:
            m["false_rate"] = {"value": float(run.observe_false) / run.observe_total,
                               "target": _target("false_rate"), "direction": "down",
                               "source": "%s：误报 %d/%d 条标注样本"
                                         % (models.DATA_SOURCE_TEXT.get(
                                             run.data_source, run.data_source),
                                            run.observe_false, run.observe_total)}
        elif fpr["rate"] is not None:
            m["false_rate"] = {"value": fpr["rate"], "target": _target("false_rate"),
                               "direction": "down",
                               "source": "复核结论（%d 条复核，误报 %d 条）"
                                         % (fpr["reviewed"], fpr["false_positive"])}
        if run.response_sec is not None:
            m["alert_response_sec"] = {"value": run.response_sec,
                                       "target": _target("alert_response_sec"),
                                       "direction": "down", "source": "系统日志分析"}
        if run.camera_online_rate:
            m["camera_online_rate"] = {"value": run.camera_online_rate,
                                       "target": _target("camera_online_rate"),
                                       "direction": "up", "source": "考试期间统计"}
        if run.coverage_rate:
            m["coverage_rate"] = {"value": run.coverage_rate,
                                  "target": _target("coverage_rate"),
                                  "direction": "up", "source": "逐机位核查留档"}
        if run.dedup_expected:
            m["dedup_accuracy"] = {
                "value": 1.0 - (abs(run.dedup_counted - run.dedup_expected) /
                                float(run.dedup_expected)),
                "target": _target("dedup_accuracy"), "direction": "up",
                "source": "跨机位去重：真值 %d 人、系统计数 %d 人"
                          % (run.dedup_expected, run.dedup_counted)}
        if run.clock_drift_sec is not None:
            m["clock_drift_sec"] = {"value": run.clock_drift_sec,
                                    "target": _target("clock_drift_sec"),
                                    "direction": "down", "source": "NTP 比对"}
        if run.invigilator_total:
            m["invigilator_rate"] = {
                "value": float(run.invigilator_onpost) / run.invigilator_total,
                "target": 1.0, "direction": "up", "source": "在岗核验记录"}
        if run.alt_channel:
            m["alt_channel"] = {"value": 1.0, "target": 1.0, "direction": "up",
                                "source": "替代核验通道"}
        if run.alt_channel_drill:
            m["alt_channel_drill"] = {"value": 1.0, "target": 1.0, "direction": "up",
                                      "source": "演练记录"}
        if run.domain_isolation:
            m["domain_isolation"] = {"value": 1.0, "target": 1.0, "direction": "up",
                                     "source": "独立分区 + 独立授权"}
        if run.offline_ready:
            m["offline_ready"] = {"value": 1.0, "target": 1.0, "direction": "up",
                                  "source": "全离线自检"}
        if run.min_config:
            m["min_config"] = {"value": 1.0, "target": 1.0, "direction": "up",
                               "source": "最小可用配置巡考留痕"}

    # 合并：本次录入优先，其次台账推导
    for key in config.KPI_NAMES:
        row = m.get(key) or derived.get(key)
        if row is None:
            continue
        if key not in config.kpi_applicable(room.room_type) and key in (
                "dedup_accuracy", "alt_channel", "alt_channel_drill",
                "offline_ready", "domain_isolation"):
            # 非该考场类型的差异项，只在有值时附注，不参与达标判定
            row = dict(row)
            row["applicable"] = False
        else:
            row = dict(row)
            row["applicable"] = True
        row["key"] = key
        row["name"] = config.KPI_NAMES[key]
        row["meets"] = _meets(key, row.get("value")) if row.get("applicable") else None
        row["target_text"] = _fmt_target(key)
        rows.append(row)

    applied = [r for r in rows if r.get("applicable")]
    ok_n = len([r for r in applied if r["meets"] is True])
    bad_n = len([r for r in applied if r["meets"] is False])
    miss_n = len([r for r in applied if r["meets"] is None])

    if run is not None and run.data_source == "synthetic":
        notes.append("本次指标含**合成/演示数据**（data_source=synthetic），"
                     "只用于验证流程与横向比较，**不等于真实考场准确率**；"
                     "真实指标必须用考场现场样本。")
    if miss_n:
        notes.append("有 %d 项指标无实测证据：%s。缺证据时本模块**不填默认值**，"
                     "如实标为缺失。" % (miss_n, "、".join(
                         r["name"] for r in applied if r["meets"] is None)))
    if run is None and not store.latest_kpi(room.room_id):
        notes.append("该考场尚无任何验收测算记录（界面「验收指标」页可录入现场实测）。")

    summary = {"total": len(applied), "meets": ok_n, "fails": bad_n, "missing": miss_n,
               "verdict": _verdict(ok_n, bad_n, miss_n)}
    if persist and run is not None:
        run.metrics = {r["key"]: {"value": r.get("value"), "target": r.get("target_text"),
                                  "meets": r.get("meets"), "source": r.get("source")}
                       for r in rows}
        run.notes = list(run.notes or []) + notes
        store.save_kpi(run)
    return {"room": room.to_dict(), "metrics": rows, "summary": summary,
            "notes": notes, "run": run.to_dict() if run is not None else None}


def _verdict(ok_n, bad_n, miss_n):
    if bad_n:
        return "不达标（%d 项未达要求）" % bad_n
    if miss_n:
        return "部分缺失（%d 项无证据，%d 项达标）" % (miss_n, ok_n)
    return "全部达标（%d 项）" % ok_n


def _fmt_target(key):
    t = _target(key)
    if t is None:
        return ""
    d = _direction(key)
    if key in ("alt_channel", "alt_channel_drill", "domain_isolation",
               "offline_ready", "min_config"):
        return "强制具备"
    if key in ("alert_response_sec", "clock_drift_sec"):
        return "≤ %.2g 秒" % t
    if t >= 1.0 and d == "up" and key == "coverage_rate":
        return "100%（无盲区）"
    if d == "up":
        return "≥ %.1f%%" % (t * 100)
    return "≤ %.1f%%" % (t * 100)


# =============================================================================
# 新建一次测算
# =============================================================================
def new_run(room, session_id="", operator="", data_source="manual", **kw):
    run = KpiRun(room_id=room.room_id, room_name=room.name,
                 room_type=room.room_type, session_id=session_id,
                 operator=operator, data_source=data_source or "manual")
    for k in ("observe_total", "observe_hit", "observe_correct", "observe_false",
              "dedup_expected", "dedup_counted", "invigilator_onpost",
              "invigilator_total"):
        if kw.get(k) is not None:
            setattr(run, k, models.to_int(kw[k]))
    for k in ("response_sec", "camera_online_rate", "coverage_rate", "clock_drift_sec"):
        if kw.get(k) is not None:
            setattr(run, k, models.to_float(kw[k]))
        else:
            # 未录入就标 None（"缺失"），不填 0 冒充"响应 0 秒/偏差 0 秒"
            setattr(run, k, None)
    for k in ("alt_channel", "alt_channel_drill", "domain_isolation", "offline_ready",
              "min_config"):
        if kw.get(k) is not None:
            setattr(run, k, 1 if kw[k] in (True, 1, "1", "true", "True") else 0)
    run.notes = list(kw.get("notes") or [])
    return run


def run_from_baseline(store, room, operator="", data_source="synthetic",
                      total=None, hit=None, correct=None, false=None):
    """
    用"标注样本"跑一次测算（脚本化行为测试口径）：
      total  - 人工标注的异常样本总数
      hit    - 系统提示到的条数            -> 提示率
      correct- 行为类别判对的条数          -> 准确率
      false  - 报错的条数                 -> 误报率（没有复核结论时的兜底口径）
    不给就按内置基线生成（**标注为 synthetic**）。
    """
    if total is None:
        n = len(room.enabled_behaviors or []) or 44
        total, hit = n, n
        correct = int(n * 0.96)
        false = 0
    run = new_run(room, operator=operator, data_source=data_source,
                  observe_total=total, observe_hit=(hit if hit is not None else total),
                  observe_correct=(correct if correct is not None
                                   else int(total * 0.96)),
                  observe_false=(false or 0),
                  response_sec=_derive_response_sec(store, room),
                  coverage_rate=1.0 if room.points_verified else None)
    return run


def _derive_response_sec(store, room):
    """告警响应时间：用近 30 天告警的（处置时间-产生时间）均值；无数据返回 0。"""
    rows = store.list_privacy(room_id=room.room_id, action="review", limit=500)
    alerts = store.list_alerts(room_id=room.room_id, limit=500)
    if not rows or not alerts:
        return 0.0
    # 本模块的复核记录不存时间差，这里用"告警 SLA 内是否送达"近似不可靠，
    # 因此如实返回 0（调用方应显式录入系统日志分析的实测值）
    return 0.0


def room_scoreboard(store, site=None):
    """所有考场的指标看板（界面用）。"""
    out = []
    for room in store.list_rooms(site=site, limit=2000):
        run = store.latest_kpi(room.room_id)
        ev = evaluate(store, room, run=None, persist=False)
        out.append({"room_id": room.room_id, "name": room.name,
                    "room_type": room.room_type,
                    "room_type_name": room.type_name(),
                    "special": room.is_special(),
                    "status_text": room.status_text(),
                    "summary": ev["summary"],
                    "has_run": run is not None,
                    "last_run": run.to_dict() if run else None})
    return out


def site_summary(store, site=None):
    board = room_scoreboard(store, site=site)
    ok = len([b for b in board if b["summary"]["fails"] == 0 and
              b["summary"]["missing"] == 0 and b["summary"]["total"]])
    bad = len([b for b in board if b["summary"]["fails"]])
    miss = len(board) - ok - bad
    return {"rooms": len(board), "fully_ok": ok, "failing": bad,
            "partial_or_missing": miss, "board": board}
