# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 屏蔽设备统一管控

这一层负责"能不能开、开多大、开哪些频段、什么时候必须关"，**不是**无条件的开关按钮：

    开启前逐条过闸：
      ① 设备在线、不是故障机
      ② 定向天线方位角已设置、扇区 ≤ 90°（避免全向外溢）
      ③ 功率 ≤ 上限（低功率、贴近考场）
      ④ 有**当天有效**的设台审批/报备（红线条目，没批文不许开）
      ⑤ 此刻在考试窗口内（开考前 pre_open ~ 结束 + post_close）
    任何一条不满足 -> 拒绝开启，并把"为什么拒绝"如实返回（界面/日志都看得到）。

    开启后：
      · 写 jammers_state + switch_logs（谁、何时、哪些频段、多大功率、依据哪份批文）
      · 时间表 tick 到窗口外 -> 自动关闭；审批失效/设备故障 -> 报警并提示人工处置
      · 关闭指令发不出去（设备离线）时**不假装已关**：状态保持开启并报警，
        由一致性核对把它暴露成"超时未关"

关于"外溢"：屏蔽器发的是压制噪声，外溢会不会影响周边居民合法通信，最终只能靠
现场实测。本模块给一个**自由空间路径损耗估算**用于部署前预判（spill_estimate），
并把现场实测结果（spill_test / shield_test）作为验收证据落库 —— 估算值绝不当作验收结论。
"""
import math

import config
import models
import schedule
from models import JammerState, ShieldTest, SpillTest, SwitchLog

#: 屏蔽指令下发失败（设备离线/故障）时的错误码
BLOCK_OFFLINE = "offline"
BLOCK_DIRECTION = "direction"
BLOCK_SECTOR = "sector"
BLOCK_POWER = "power"
BLOCK_BANDS = "bands"
BLOCK_APPROVAL = "approval"
BLOCK_WINDOW = "window"
BLOCK_DRILL = "drill"
BLOCK_ALREADY = "already_on"
BLOCK_NOT_ON = "not_on"


# =============================================================================
# 计划参数
# =============================================================================
def default_bands(cfg=None):
    return list(config.shield_plan(cfg))


def bands_of(jammer, cfg=None):
    """该屏蔽器本次要压制的频段：设备配置优先，未配置用全局计划；全部校验存在。"""
    bands = list(jammer.bands or []) or default_bands(cfg)
    return [b for b in bands if b in config.BAND_INDEX]


def power_cap(cfg=None):
    """允许的功率上限：文档红线 5W 与设置值取小。"""
    cfg = cfg or config.load_settings()
    cap = min(float(config.TARGET_POWER_MAX_W),
              models.to_float(cfg.get("max_power_w"), config.TARGET_POWER_MAX_W))
    return max(0.1, cap)


def sector_cap(cfg=None):
    cfg = cfg or config.load_settings()
    return min(float(config.TARGET_SECTOR_MAX_DEG),
               models.to_float(cfg.get("sector_max_deg"), config.TARGET_SECTOR_MAX_DEG))


def plan_power(jammer, cfg=None):
    return round(min(models.to_float(jammer.power_w, 1.0), power_cap(cfg)), 3)


# =============================================================================
# 开启前的逐条闸门
# =============================================================================
def evaluate(store, jammer, now=None, cfg=None, drill=False, session_id=""):
    """
    返回 {can_on, blockers:[{code,text}], warnings:[{code,text}], plan:{...},
          session, phase, approval}
    """
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    blockers, warnings = [], []
    plan = {"bands": bands_of(jammer, cfg), "power_w": plan_power(jammer, cfg),
            "direction": jammer.direction, "sector_width": jammer.sector_width,
            "session_id": "", "approval_no": "", "window": [0.0, 0.0]}

    if jammer.kind != config.DEV_JAMMER:
        blockers.append({"code": "kind", "text": "该设备不是屏蔽器（%s）"
                                                  % config.DEV_KIND_TEXT.get(jammer.kind, "")})
    if jammer.status != config.DEV_ONLINE:
        blockers.append({"code": BLOCK_OFFLINE,
                         "text": "设备%s，无法下发屏蔽指令" % jammer.status_text()})
    if not plan["bands"]:
        blockers.append({"code": BLOCK_BANDS, "text": "屏蔽频段为空"})
    site = jammer.site or cfg.get("site") or ""
    if jammer.direction is None or float(jammer.direction) < 0:
        blockers.append({"code": BLOCK_DIRECTION,
                         "text": "未设置定向天线方位角（全向屏蔽不符合红线）"})
    elif float(jammer.sector_width or 0) <= 0 or \
            float(jammer.sector_width) > sector_cap(cfg):
        blockers.append({"code": BLOCK_SECTOR,
                         "text": "扇区宽度 %.0f° 超过上限 %.0f°（定向不足，外溢风险）"
                                 % (float(jammer.sector_width or 0), sector_cap(cfg))})
    if float(jammer.power_w or 0) > power_cap(cfg):
        warnings.append({"code": BLOCK_POWER,
                         "text": "设备标称 %.1fW 超过上限 %.1fW，将按上限下发"
                                 % (float(jammer.power_w), power_cap(cfg))})

    # 审批/报备
    approval = None
    if drill:
        if not cfg.get("drill_enabled"):
            blockers.append({"code": BLOCK_DRILL,
                             "text": "演练模式未开启（设置里 drill_enabled=false）"})
        else:
            warnings.append({"code": BLOCK_DRILL,
                             "text": "演练模式：本次开机未经过考试时段/审批校验，"
                                     "记录标记为 drill，仅用于设备联调"})
    elif cfg.get("approval_required", True):
        approval, why = store.valid_approval(site, plan["bands"])
        if approval is None:
            blockers.append({"code": BLOCK_APPROVAL, "text": why})
        else:
            plan["approval_no"] = approval.no or approval.approval_id
            if approval.doc:
                warnings.append({"code": "approval_doc",
                                 "text": "依据批文：%s（%s）" % (approval.no or "-",
                                                               approval.doc)})
            else:
                warnings.append({"code": "approval_doc",
                                 "text": "审批记录未登记批文文件/编号，验收时需补齐"})
            have = set(approval.bands or [])
            if have:
                miss = [b for b in plan["bands"] if b not in have]
                if miss:
                    warnings.append({"code": "approval_bands",
                                     "text": "审批未覆盖频段：%s"
                                             % "、".join(config.band_name(b) for b in miss)})

    # 时间窗口
    sess = None
    phase = ""
    if session_id:
        sess = store.get_session(session_id)
        if sess is None:
            blockers.append({"code": BLOCK_WINDOW, "text": "指定的考试场次不存在"})
        else:
            phase = schedule.state_at(sess, now, cfg)
            if not drill and phase not in ("pre_open", "active", "post_close"):
                blockers.append({"code": BLOCK_WINDOW,
                                 "text": "不在该场次开关窗口内（%s，%s）"
                                         % (sess.window_text(),
                                            schedule.PHASE_TEXT.get(phase, phase))})
    else:
        sess, phase = schedule.find_window(store, now, cfg)
        if sess is None:
            if drill:
                pass
            else:
                blockers.append({"code": BLOCK_WINDOW,
                                 "text": "当前不在任何考试窗口（仅考试时段允许开启）"})
        else:
            plan["window"] = list(schedule.window(sess, cfg)[:2])

    if sess is not None:
        plan["session_id"] = sess.session_id
        if not plan["window"] or plan["window"] == [0.0, 0.0]:
            plan["window"] = list(schedule.window(sess, cfg)[:2])

    # 已在开启状态（turn_on 会再用一次，避免重复记录）
    state = store.get_state(jammer.device_id)
    if state is not None and state.is_on:
        blockers.append({"code": BLOCK_ALREADY, "text": "该屏蔽器已在开启状态"})

    return {"can_on": not blockers, "blockers": blockers, "warnings": warnings,
            "plan": plan, "session": sess, "phase": phase, "approval": approval,
            "drill": bool(drill)}


# =============================================================================
# 开 / 关
# =============================================================================
def turn_on(store, jammer, operator="", reason="", session_id="", bands=None,
            power_w=None, now=None, drill=False, source=None, cfg=None):
    """开启一台屏蔽器（先过闸，过了才落状态与开关记录）。返回 dict。"""
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    ev = evaluate(store, jammer, now, cfg, drill=drill, session_id=session_id)
    if not ev["can_on"]:
        store.log("拒绝开启屏蔽", "%s：%s" % (
            jammer.code, "；".join(b["text"] for b in ev["blockers"])), operator)
        return {"ok": False, "reason": "；".join(b["text"] for b in ev["blockers"]),
                "blockers": ev["blockers"], "warnings": ev["warnings"],
                "plan": ev["plan"], "session": ev["session"].to_dict()
                if ev["session"] else None, "phase": ev["phase"]}
    plan = dict(ev["plan"])
    if bands:
        valid = [b for b in bands if b in config.BAND_INDEX]
        if not valid:
            return {"ok": False, "reason": "指定的频段无效"}
        # 频段可以由现场临时收窄/调整，但**不能超出审批范围**（红线）
        if not drill and cfg.get("approval_required", True):
            ap = ev.get("approval")
            have = set(ap.bands or []) if ap is not None else set()
            extra = [b for b in valid if have and b not in have]
            if extra:
                return {"ok": False,
                        "reason": "所选频段超出审批范围：%s（审批 %s）"
                                  % ("、".join(config.band_name(b) for b in extra),
                                     "、".join(config.band_name(b) for b in have))}
        plan["bands"] = valid
    if power_w is not None:
        plan["power_w"] = round(min(models.to_float(power_w), power_cap(cfg)), 3)
    src = source or (config.SW_SRC_DRILL if drill else config.SW_SRC_MANUAL)
    state = JammerState(device_id=jammer.device_id, is_on=1, since_epoch=now,
                        bands=plan["bands"], power_w=plan["power_w"],
                        session_id=plan.get("session_id") or "",
                        approval_no=plan.get("approval_no") or "",
                        operator=operator or cfg.get("operator") or "",
                        reason=reason or ("演练开机" if drill else "按考试窗口开启"),
                        updated_at=models.ts_text(now))
    store.save_state(state)
    log = SwitchLog(device_id=jammer.device_id, action=config.SW_ON,
                    bands=plan["bands"], power_w=plan["power_w"],
                    session_id=plan.get("session_id") or "",
                    source=src, operator=operator or "", reason=state.reason,
                    ts_epoch=now,
                    extra={"phase": ev["phase"], "window": plan.get("window"),
                           "approval": plan.get("approval_no") or "",
                           "drill": bool(drill)})
    store.save_switch(log)
    store.log("开启屏蔽", "%s（%s）频段 %s，功率 %.2fW，%s"
              % (jammer.code, jammer.name, "、".join(config.band_name(b)
                                                    for b in plan["bands"]),
                 plan["power_w"], state.reason), operator)
    return {"ok": True, "action": "on", "device_id": jammer.device_id,
            "device_code": jammer.code, "bands": plan["bands"],
            "power_w": plan["power_w"], "session_id": plan.get("session_id") or "",
            "phase": ev["phase"], "warnings": ev["warnings"],
            "source": src, "log_id": log.log_id,
            "since": models.ts_text(now)}


def turn_off(store, jammer, operator="", reason="", now=None, source=None, cfg=None):
    """
    关闭一台屏蔽器。设备离线/故障时**不假装已关**：返回 ok=False，状态保持开启，
    这样时间表一致性核对会把它暴露成"超时未关"（这才是现场真实情况）。
    """
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    state = store.get_state(jammer.device_id)
    if state is None or not state.is_on:
        return {"ok": False, "reason": "该屏蔽器当前未开启"}
    if jammer.status != config.DEV_ONLINE:
        msg = ("设备%s，关闭指令未确认（状态保持开启，需现场处置）"
               % jammer.status_text())
        store.log("关闭屏蔽失败", "%s：%s" % (jammer.code, msg), operator)
        return {"ok": False, "reason": msg, "device_id": jammer.device_id,
                "state": "still_on"}
    src = source or config.SW_SRC_MANUAL
    duration = max(0.0, now - float(state.since_epoch or now))
    state.is_on = 0
    state.updated_at = models.ts_text(now)
    state.reason = reason or state.reason
    state.operator = operator or state.operator
    store.save_state(state)
    log = SwitchLog(device_id=jammer.device_id, action=config.SW_OFF,
                    bands=state.bands, power_w=state.power_w,
                    session_id=state.session_id, source=src,
                    operator=operator or "", reason=reason or "关闭",
                    ts_epoch=now, extra={"duration_sec": round(duration, 1),
                                         "auto": src == config.SW_SRC_AUTO})
    store.save_switch(log)
    store.log("关闭屏蔽", "%s（%s）持续 %.1f 分钟，%s"
              % (jammer.code, jammer.name, duration / 60.0, reason or "关闭"),
              operator)
    return {"ok": True, "action": "off", "device_id": jammer.device_id,
            "device_code": jammer.code, "duration_sec": round(duration, 1),
            "reason": reason or "关闭", "source": src, "log_id": log.log_id}


# =============================================================================
# 时间表驱动：自动开 / 自动关
# =============================================================================
def manual_off_override(store, jammer, session=None, now=None):
    """
    判断"本场次内是否有人工关闭、应暂停自动开启"。

    为什么需要：自动策略每 5 秒 tick 一次，如果操作员因为现场风险手动关闭，
    下个 tick 又自动打开，就等于剥夺了人工干预权。规则很简单：
      · 最后一次开关记录是**人工关闭**，且属于**当前场次** -> 本场次不再自动开；
      · 换成下一场（session_id 不同）或人工再开一次 -> 自动策略恢复。
    返回 (bool, 原因)。
    """
    if session is None:
        return False, ""
    last = store.last_switch(jammer.device_id)
    if last is None:
        return False, ""
    if last.action != config.SW_OFF or last.source != config.SW_SRC_MANUAL:
        return False, ""
    if last.session_id and last.session_id == session.session_id:
        return True, "本场次已人工关闭，自动策略暂停（换场次或人工开启后恢复）"
    return False, ""


def tick(store, now=None, cfg=None, operator="auto"):
    """
    每秒/每分钟调用一次的"总控"：
      · 刷新场次状态
      · 窗口内且自动开启 -> 自动开（仍要过审批/功率/定向闸门）
      · 窗口结束 / 审批失效 / 跨场次 -> 自动关
      · 设备离线导致关不掉 -> 报警（写审计，界面标红）
    返回 {actions, in_window, session, phase, on_count, off_count, states_changed}
    """
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    st = schedule.tick(store, now, cfg)
    sess, phase = schedule.find_window(store, now, cfg)
    actions = []
    for jammer in store.list_devices(kind=config.DEV_JAMMER):
        state = store.get_state(jammer.device_id)
        on = bool(state and state.is_on)
        ev = evaluate(store, jammer, now, cfg, drill=False)
        if on:
            valid_window = bool(sess) and phase in ("pre_open", "active", "post_close")
            approval_ok = True
            if cfg.get("approval_required", True):
                ap, why = store.valid_approval(jammer.site, state.bands or jammer.bands)
                approval_ok = ap is not None
            if not valid_window or not approval_ok:
                why = ("不在考试窗口" if not valid_window else "审批失效")
                if not valid_window and not cfg.get("auto_off", True):
                    actions.append({"device_code": jammer.code,
                                    "action": "auto_off_disabled", "ok": False,
                                    "reason": "窗口已结束但自动关闭被关闭，需人工关闭"
                                              "（一致性核对会判为超时未关）"})
                    continue
                r = turn_off(store, jammer, operator=operator,
                             reason="自动关闭：%s" % why, now=now,
                             source=config.SW_SRC_AUTO, cfg=cfg)
                actions.append({"device_code": jammer.code, "action": "auto_off",
                                "ok": bool(r.get("ok")), "reason": r.get("reason") or why,
                                "detail": r})
            elif float(state.power_w or 0) > power_cap(cfg) + 1e-6:
                actions.append({"device_code": jammer.code, "action": "power_over",
                                "ok": False,
                                "reason": "当前功率 %.2fW 超过上限 %.2fW，需人工降功率"
                                          % (float(state.power_w), power_cap(cfg))})
            continue
        # 未开启：只在自动策略打开 + 窗口内 + 场次是"正在进行/开考前"时自动开
        if not cfg.get("auto_shield", True) or sess is None:
            continue
        if phase not in ("pre_open", "active"):
            continue
        override, why = manual_off_override(store, jammer, sess, now)
        if override:
            continue
        ev2 = evaluate(store, jammer, now, cfg, drill=False,
                       session_id=sess.session_id)
        if not ev2["can_on"]:
            actions.append({"device_code": jammer.code, "action": "auto_on_blocked",
                            "ok": False,
                            "reason": "；".join(b["text"] for b in ev2["blockers"])})
            continue
        r = turn_on(store, jammer, operator=operator,
                    reason="自动开启：%s %s" % (sess.date, sess.name), now=now,
                    source=config.SW_SRC_AUTO, cfg=cfg)
        actions.append({"device_code": jammer.code, "action": "auto_on",
                        "ok": bool(r.get("ok")),
                        "reason": r.get("reason") or "已开启", "detail": r})
    on_count = sum(1 for a in actions if a["action"] in ("auto_on",) and a["ok"])
    off_count = sum(1 for a in actions if a["action"] == "auto_off" and a["ok"])
    if actions:
        store.log("自动开关", "；".join("%s %s %s" % (a["device_code"], a["action"],
                                                     "成功" if a["ok"] else "失败")
                                       for a in actions), operator)
    return {"actions": actions, "in_window": bool(sess),
            "session": sess.to_dict() if sess else None, "phase": phase,
            "on_count": on_count, "off_count": off_count,
            "states_changed": st.get("states_changed") or [],
            "jammers_on": len(store.on_states())}


# =============================================================================
# 外溢估算与实测
# =============================================================================
def estimate_spill(store, jammer, distance_m, band=None, cfg=None):
    """
    自由空间路径损耗估算（部署前预判用，**不作为验收结论**）。

      EIRP(dBm) = 10log10(P_W × 1000) + 天线增益(0 dBi)
      FSPL(dB)  = 32.44 + 20log10(f_MHz) + 20log10(d_km)
      接收(dBm) = EIRP - FSPL - 建筑衰减(默认 0)
    """
    cfg = cfg or config.load_settings()
    band = band or ((jammer.bands or default_bands(cfg))[0]
                    if (jammer.bands or default_bands(cfg)) else "ism24")
    low, high = config.band_range(band)
    freq = (low + high) / 2.0 if low else 2400.0
    d = max(1.0, models.to_float(distance_m, 1.0))
    eirp = 10.0 * math.log10(max(0.001, models.to_float(jammer.power_w, 1.0)) * 1000.0)
    fspl = 32.44 + 20.0 * math.log10(freq) + 20.0 * math.log10(d / 1000.0)
    est = eirp - fspl
    limit = models.to_float(cfg.get("spill_limit_dbm"), config.TARGET_SPILL_DBM)
    return {"ok": True, "distance_m": d, "band": band,
            "band_name": config.band_name(band), "freq_mhz": freq,
            "eirp_dbm": round(eirp, 2), "path_loss_db": round(fspl, 2),
            "estimate_dbm": round(est, 2), "limit_dbm": limit,
            "pass": bool(est <= limit),
            "note": "自由空间估算（无遮挡、正对扇区的最坏情况），"
                    "现场必须以实测为准"}


def spill_test(store, jammer, point, distance_m, band, measured_dbm,
               operator="", note="", cfg=None):
    """登记一次外溢实测（考场外围合法通信可用性验证）。返回 (ok, info)。"""
    cfg = cfg or config.load_settings()
    limit = models.to_float(cfg.get("spill_limit_dbm"), config.TARGET_SPILL_DBM)
    measured = models.to_float(measured_dbm)
    result = "pass" if measured <= limit else "fail"
    t = SpillTest(jammer_id=jammer.device_id, point=str(point or ""),
                  distance_m=models.to_float(distance_m), band=band or "",
                  measured_dbm=measured, limit_dbm=limit, result=result,
                  operator=str(operator or ""), note=str(note or ""))
    store.save_spill(t)
    store.log("外溢实测", "%s %s：%.1f dBm（限值 %.1f，%s）"
              % (jammer.code, t.point, measured, limit,
                 "通过" if result == "pass" else "超限"), operator)
    return True, {"test_id": t.test_id, "result": result, "measured_dbm": measured,
                  "limit_dbm": limit, "pass": result == "pass",
                  "measured_at": t.measured_at}


def shield_test(store, room_id, band, before_dbm, after_dbm, rate=None,
                operator="", note="", cfg=None):
    """
    登记一次屏蔽效果实测：衰减 = 屏蔽前 - 屏蔽后；达标需衰减 ≥ 20 dB 且有效率 ≥ 92%。
    rate 不给时按"衰减达标即 100%"计算（单点实测的诚实口径，报告里会注明）。
    """
    cfg = cfg or config.load_settings()
    before = models.to_float(before_dbm)
    after = models.to_float(after_dbm)
    atten = round(before - after, 2)
    if rate is None or rate == "":
        rate = 1.0 if atten >= config.TARGET_ATTEN_DB else 0.0
    rate = max(0.0, min(1.0, models.to_float(rate, 0.0)))
    ok = (atten >= config.TARGET_ATTEN_DB
          and rate >= models.to_float(config.TARGET_SHIELD_RATE, 0.92))
    t = ShieldTest(room_id=str(room_id or ""), band=band or "",
                   before_dbm=before, after_dbm=after, attenuation_db=atten,
                   rate=round(rate, 3), result="pass" if ok else "fail",
                   operator=str(operator or ""), note=str(note or ""))
    store.save_shield_test(t)
    store.log("屏蔽效果实测", "%s %s：%.1f -> %.1f dBm（衰减 %.1f dB，%s）"
              % (t.room_id or "-", config.band_name(t.band), before, after, atten,
                 "通过" if ok else "未达标"), operator)
    return True, {"test_id": t.test_id, "attenuation_db": atten, "rate": t.rate,
                  "result": t.result, "pass": ok, "measured_at": t.measured_at}


# =============================================================================
# 状态视图
# =============================================================================
def state_view(store, jammer, now=None):
    now = float(now if now is not None else models.now_epoch())
    state = store.get_state(jammer.device_id)
    on = bool(state and state.is_on)
    dur = max(0.0, now - float(state.since_epoch)) if on else 0.0
    sess = store.get_session(state.session_id) if (state and state.session_id) else None
    override, override_why = manual_off_override(store, jammer, sess, now)
    return {"device_id": jammer.device_id, "device_code": jammer.code,
            "name": jammer.name, "on": on,
            "since": models.ts_text(state.since_epoch)[5:16] if on else "",
            "duration_min": round(dur / 60.0, 1),
            "bands": list(state.bands or []) if state else [],
            "bands_text": "、".join(config.band_name(b) for b in
                                    (state.bands if state else jammer.bands or [])),
            "power_w": float(state.power_w) if state else plan_power(jammer),
            "session": sess.window_text() if sess else "",
            "approval_no": state.approval_no if state else "",
            "operator": state.operator if state else "",
            "reason": state.reason if state else "",
            "manual_off": bool(override), "manual_off_reason": override_why}


def describe():
    cfg = config.load_settings()
    return {"功率上限": "%.1f W" % power_cap(cfg),
            "定向要求": "必须设置方位角，扇区 ≤ %.0f°" % sector_cap(cfg),
            "审批要求": "必须有当天有效的设台审批/报备" if cfg.get("approval_required", True)
                        else "已关闭（不建议）",
            "自动策略": "开考前自动开机 %s / 结束自动关机 %s"
                        % ("开" if cfg.get("auto_shield", True) else "关",
                           "开" if cfg.get("auto_off", True) else "关"),
            "外溢限值": "%.0f dBm（实测）" % models.to_float(
                cfg.get("spill_limit_dbm"), config.TARGET_SPILL_DBM)}
