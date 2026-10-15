# -*- coding: utf-8 -*-
r"""
行为识别清单与告警分级路由

对应《普通考场应用.md》第三节（AI 识别清单 44 种）、第四节（橙色/红色分级与推送）、
以及《特殊考场应用.md》第一节"算法/参数差异"与第二节第 4 条（特殊考场分级告警）。

本模块回答四个问题：
  1) 这个行为**在该类考场该不该报**（明确不适用的行为不报，避免按普通口径硬判）
  2) 该报**哪一级**（模板差异 + 类别权重 + 画质可信度）
  3) 到了门槛没有（各考场 confidence_min 不同：隔离/涉密更严）
  4) 要不要**抑制**（同类窗口内重复上报、去重窗口）
"""
import config
import models

# 类别权重（模板 params.algo_weights 覆盖）
DEFAULT_WEIGHTS = {
    config.CAT_INDIVIDUAL: 1.0,
    config.CAT_GROUP: 1.0,
    config.CAT_INVIGILATOR: 1.0,
    config.CAT_PROTOCOL: 1.0,
    config.CAT_SCREEN: 1.0,
    config.CAT_AUDIO: 1.0,
}


def behavior_catalog():
    """44 种普通行为 + 机考/口试附加行为，按类别分组。"""
    out = {}
    for t in list(config.BEHAVIORS) + list(config.EXTRA_BEHAVIORS):
        b = config.behavior_dict()[t[0]]
        out.setdefault(b["category"], []).append(b)
    return out


def catalog_summary():
    """普通考场口径的四类计数（12/8/20/4）+ 附加类别。"""
    d = behavior_catalog()
    return {
        "考生个体行为": len(d.get(config.CAT_INDIVIDUAL, [])),
        "考生群体行为": len(d.get(config.CAT_GROUP, [])),
        "监考员行为": len(d.get(config.CAT_INVIGILATOR, [])),
        "考务规范行为": len(d.get(config.CAT_PROTOCOL, [])),
        "屏幕行为（机考）": len(d.get(config.CAT_SCREEN, [])),
        "音频质量（口试）": len(d.get(config.CAT_AUDIO, [])),
        "总数": sum(len(v) for v in d.values()),
    }


def weights_for(room):
    w = dict(DEFAULT_WEIGHTS)
    params = (room.type_meta().get("params") or {}) if room else {}
    w.update(params.get("algo_weights") or {})
    return w


def resolve_behavior(store, code):
    """库里（可人工开关）优先，其次 config 内置定义。"""
    b = store.get_behavior(code) if store else None
    if b is not None:
        return b
    d = config.behavior_dict().get(code)
    if not d:
        return None
    return models.BehaviorDef(code=d["code"], name=d["name"],
                              category=d["category"], level=d["level"],
                              signals=list(d["signals"]))


def is_enabled(store, room, code):
    """
    该行为在本考场是否启用。

    三道闸：库里的全局开关 -> 该考场类型的"明确不适用"清单 -> 该考场模板的启用清单。
    """
    b = resolve_behavior(store, code)
    if b is None:
        return False, "未知行为码：%s" % code
    if not b.enabled:
        return False, "该行为已在全局清单中停用"
    if room is None:
        return True, ""
    if room.room_type in (b.rooms_off or []):
        return False, "该行为不适用于%s" % config.type_name(room.room_type)
    if room.enabled_behaviors and code not in room.enabled_behaviors:
        return False, "该行为不在本考场（%s）的启用清单内" % room.type_name()
    return True, ""


def base_level(store, code):
    b = resolve_behavior(store, code)
    if b is None:
        return config.LEVEL_ORANGE
    return b.level


def adjust_confidence(room, code, confidence, camera_factor=1.0,
                      sustained=True, multi_person=False, closeup=False,
                      frames=0):
    """
    按考场类型与证据强度修正置信度。

    规则（都能在《特殊考场应用.md》第一节找到出处）：
      * 类别权重：机考屏幕 ×1.3、口试个体 ×0.7、大教室群体 ×1.2、备用特写 ×1.2 ...
      * 瞬时动作降权 ×0.7：逐帧标签会被瞬时动作骗（全仓库口径：时间维度抑制）
        —— frames<=1 视为瞬时，sustained=False 也算瞬时
      * 群体行为多人同框加成 ×1.05；多人同框缺人群证据时降权 ×0.9
      * 备用考场特写机位加成 ×closeup_weight（模板参数）
      * 机位画质可信度相乘（画质差 -> 置信度低 -> 够不到门槛 -> 不报）
    """
    w = weights_for(room)
    d = config.behavior_dict().get(code) or {}
    cat = d.get("category")
    transient = (not sustained) or (frames is not None and 0 < frames <= 1)
    conf = float(confidence or 0.0) * float(w.get(cat, 1.0))
    if transient:
        conf *= 0.70
    if multi_person:
        conf *= 1.05
    elif cat == config.CAT_GROUP:
        conf *= 0.90
    params = (room.type_meta().get("params") or {}) if room else {}
    cw = models.to_float(params.get("closeup_weight"), 1.0)
    if closeup and cw != 1.0:
        conf *= cw
    conf *= max(0.0, min(1.0, float(camera_factor)))
    return round(min(1.0, max(0.0, conf)), 4)


def route_level(room, code, confidence, camera_factor=1.0):
    """
    定级：先取行为基级，再按考场类型参数与模板覆盖。
    返回 (level, reason)。
    """
    params = (room.type_meta().get("params") or {}) if room else {}
    level = base_level(None, code)
    d = config.behavior_dict().get(code) or {}
    # 机考"切屏"等重点行为：模板可抬级
    if code == "S_SCREEN_SWITCH" and params.get("screen_cut_alert_level") == "red":
        level = config.LEVEL_RED
    conf = adjust_confidence(room, code, confidence, camera_factor=camera_factor)
    policy = dict(config.ALERT_POLICY)
    policy.update((room.alert_policy or {}) if room else {})
    red_min = models.to_float(policy.get("red_min"), 0.85)
    orange_min = models.to_float(policy.get("orange_min"), 0.60)
    cmin = room.confidence_min() if room else models.to_float(
        policy.get("confidence_min"), 0.60)

    if level == config.LEVEL_RED and conf < red_min:
        # 红色证据不足 -> 降为橙色待复核（不放过，也不误报成大警）
        if conf >= orange_min:
            return config.LEVEL_ORANGE, ("红色证据不足（%.2f < %.2f），降为橙色待复核"
                                         % (conf, red_min))
        return config.LEVEL_ORANGE, ("置信度偏低（%.2f），按橙色兜底"
                                     % conf)
    if level == config.LEVEL_ORANGE and conf < cmin:
        return config.LEVEL_ORANGE, ("置信度 %.2f 低于本考场门槛 %.2f（%s 误报容忍度低）"
                                     % (conf, cmin, room.type_name() if room else "普通考场"))
    return level, ""


def push_targets(room, level):
    """推送对象：普通考场口径 + 特殊考场差异（SPECIAL_PUSH）。"""
    base = list(config.ALERT_ROUTING.get(level, []))
    if room is None:
        return base
    sp = config.SPECIAL_PUSH.get(room.room_type) or {}
    base = [x for x in base if x not in (sp.get("remove") or [])]
    for x in (sp.get("extra") or []):
        if x not in base:
            base.append(x)
    return base


def response_text(room, level):
    base = config.ALERT_ROUTING["response"].get(level, "")
    if room is not None and room.room_type == config.TYPE_ISOLATION:
        return base + "；远程监考员同步、必要时医生协同"
    if room is not None and room.room_type == config.TYPE_ACCESSIBLE:
        return base + "；人工兜底优先，不因智能判定影响考生"
    if room is not None and room.room_type == config.TYPE_CLASSIFIED:
        return base + "；全程内网处置，不得外传"
    return base


def dedup_window(room):
    policy = dict(config.ALERT_POLICY)
    policy.update((room.alert_policy or {}) if room else {})
    return models.to_int(policy.get("dedup_window_sec"),
                         config.ALERT_POLICY["dedup_window_sec"])


def should_suppress(store, room, code, window_sec=None):
    """
    同类告警窗口内抑制（大教室跨机位去重也走这里）。
    返回 (bool, reason)
    """
    w = window_sec if window_sec is not None else dedup_window(room)
    if w <= 0:
        return False, ""
    recent = store.recent_same_behavior(room.room_id, code, w)
    if recent:
        return True, ("%d 秒内同考场同类告警已报过（%s %s），本次抑制避免重复误报"
                      % (w, recent[0].behavior_name or code, recent[0].ts))
    return False, ""


def explain(store, room, code):
    """给界面/GUI 的"为什么报/为什么不报"解释。"""
    ok, why = is_enabled(store, room, code)
    d = config.behavior_dict().get(code) or {}
    return {
        "code": code, "name": d.get("name", code),
        "category": d.get("category", ""),
        "category_name": config.CATEGORIES.get(d.get("category"), ""),
        "enabled": ok, "reason": why or "已启用",
        "base_level": base_level(store, code),
        "base_level_name": config.LEVEL_META.get(base_level(store, code),
                                                 {}).get("name", ""),
        "alert_code": config.BEHAVIOR_ALERT_CODE.get(code, ""),
        "weight": weights_for(room).get(d.get("category"), 1.0),
        "confidence_min": room.confidence_min() if room else
        config.ALERT_POLICY["confidence_min"],
        "push": {"orange": push_targets(room, config.LEVEL_ORANGE),
                 "red": push_targets(room, config.LEVEL_RED)},
        "dedup_window_sec": dedup_window(room),
        "rooms_off": list((resolve_behavior(store, code) or
                           models.BehaviorDef()).rooms_off or []),
        "moe": config.MOE_CLASS_MAP.get(d.get("category"), ""),
    }


def build_event(store, room, code, confidence, device_code="", zone="",
                seat_no="", candidate_id="", detail="", session_id="",
                sustained=True, multi_person=False, closeup=False,
                camera_factor=1.0, frames=0, source="10_room"):
    """
    生成一条告警事件（含分级、抑制判定、推送对象）。
    **不落库、不推 03**——由 alerting.py 决定。
    返回 (AlertEvent, meta dict)
    """
    meta = {"enabled": True, "reason": "", "level_reason": "",
            "suppress": False, "suppress_reason": "", "confidence_raw": confidence}
    b = resolve_behavior(store, code)
    name = b.name if b else code
    cat = b.category if b else ""
    alert_code = config.BEHAVIOR_ALERT_CODE.get(code, "PERSON_BEHAVIOR_ABNORMAL")
    ev = models.AlertEvent(room_id=room.room_id, room_name=room.name,
                           room_type=room.room_type, session_id=session_id,
                           behavior=code, behavior_name=name, category=cat,
                           alert_code=alert_code, device_code=device_code,
                           zone=zone, seat_no=seat_no,
                           candidate_id=candidate_id, detail=detail, source=source)
    ok, why = is_enabled(store, room, code)
    if not ok:
        meta["enabled"] = False
        meta["reason"] = why
        ev.suppressed = 1
        ev.suppress_reason = why
        return ev, meta

    conf = adjust_confidence(room, code, confidence, camera_factor=camera_factor,
                             sustained=sustained, multi_person=multi_person,
                             closeup=closeup, frames=frames)
    level, lwhy = route_level(room, code, confidence, camera_factor=camera_factor)
    ev.confidence = conf
    ev.level = level
    ev.sla_sec = models.to_int(config.LEVEL_META.get(level, {}).get("sla_sec"), 0)
    ev.pushed_to = push_targets(room, level)
    ev.response_text = response_text(room, level)
    meta["level_reason"] = lwhy
    meta["confidence"] = conf
    meta["dedup"] = False
    return ev, meta
