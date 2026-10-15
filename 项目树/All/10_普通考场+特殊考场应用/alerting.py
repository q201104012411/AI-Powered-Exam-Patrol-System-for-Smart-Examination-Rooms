# -*- coding: utf-8 -*-
r"""
分级告警路由与处置（普通考场应用.md 第四节 + 特殊考场应用.md 第二节第 4 条）

流程（方案原文）：

    边缘 AI 识别 → 分级告警 → 监考/巡考终端提示 → 人工复核（成立/不成立/误报）
    → 现场处置并留痕 → 复核结果回流平台 → 误报进入算法迭代闭环

本模块落在"分级告警 → 终端提示"这一段，并把链路接到 **03 智能终端操作**
（留痕与复核由 03 负责，本模块不重复造轮子；通过 00_集成联动 的子进程协议调用，
不改 03 一行代码）。

关于画质可信度：告警会带上来源机位的画质系数（来自 02）。
  * 本模块先按系数压低置信度（够不到门槛就不报 —— 治"画质差导致的误报堆积"）
  * 送进 03 时再把系数交给它（03 会在系数 < 0.6 时把红色自动降为橙色）
"""
import config
import behavior as behavior_mod
import linkage_client
import models

STATUS_ROUTED = "routed"        # 已分流（本模块内部台账）
STATUS_SUPPRESSED = "suppressed"
STATUS_DELIVERED = "delivered"  # 已送 03
STATUS_FAILED = "failed"        # 送 03 失败（03 不可用/超时）


def camera_factor_for(room, device_code="", explicit=None):
    """
    取来源机位的画质可信度系数。
    显式给了就用；否则查 02 的最近一次核查结论；02 不可用按 1.0（不降权）并标注。
    返回 (factor, note)
    """
    if explicit is not None:
        return max(0.0, min(1.0, models.to_float(explicit, 1.0))), "调用方给定"
    if not device_code:
        return 1.0, "未指定机位（不降权）"
    res = linkage_client.camera_credibility(device_code)
    if not res.get("ok"):
        return 1.0, "画质链路不可用，按不降权处理：%s" % str(res.get("reason"))[:80]
    d = res.get("data") or {}
    return max(0.0, min(1.0, models.to_float(d.get("factor"), 1.0))), \
        (d.get("reason") or "")


def raise_behavior_alert(store, room, code, confidence, device_code="", zone="",
                         seat_no="", candidate_id="", detail="", session_id="",
                         sustained=True, multi_person=False, closeup=False,
                         frames=0, camera_factor=None, suppress_check=True, push=True,
                         operator="10_room"):
    """
    一次行为告警的完整处置：
      行为适用性 -> 置信度修正 -> 定级 -> 同类抑制 -> 落库 -> 送 03

    返回 dict：
      {"ok", "event": AlertEvent, "level", "level_name", "delivered",
       "suppressed", "suppress_reason", "pushed_to", "external": {...},
       "reason": "为什么不报/为什么降级"}
    """
    if room is None:
        return {"ok": False, "reason": "考场不存在"}
    if not room.armed():
        return {"ok": False, "reason": "考场未启用（%s），不产生告警"
                                      % room.status_text()}
    factor, fnote = camera_factor_for(room, device_code, camera_factor)
    ev, meta = behavior_mod.build_event(
        store, room, code, confidence, device_code=device_code, zone=zone,
        seat_no=seat_no, candidate_id=candidate_id, detail=detail,
        session_id=session_id, sustained=sustained, multi_person=multi_person,
        closeup=closeup, camera_factor=factor, frames=frames, source="10_room")

    if not meta.get("enabled"):
        ev.detail = (ev.detail + "；" if ev.detail else "") + \
            "未上报告警：%s" % meta.get("reason")
        store.save_alert(ev)
        store.log("告警被拦（行为不适用）",
                  "%s %s：%s" % (room.name, ev.behavior_name, meta.get("reason")),
                  operator, room.room_id)
        return {"ok": True, "event": ev, "level": ev.level,
                "level_name": config.LEVEL_META.get(ev.level, {}).get("name", ""),
                "delivered": False, "suppressed": True,
                "suppress_reason": meta.get("reason"), "pushed_to": [],
                "external": {}, "reason": meta.get("reason"),
                "level_reason": meta.get("level_reason") or "",
                "camera_factor": factor, "camera_note": fnote}

    if meta.get("level_reason"):
        ev.detail = (ev.detail + "；" if ev.detail else "") + meta["level_reason"]

    if suppress_check:
        sup, why = behavior_mod.should_suppress(store, room, code)
        if sup:
            ev.suppressed = 1
            ev.suppress_reason = why
            ev.detail = (ev.detail + "；" if ev.detail else "") + why
            store.save_alert(ev)
            store.log("告警抑制", "%s %s：%s" % (room.name, ev.behavior_name, why),
                      operator, room.room_id)
            return {"ok": True, "event": ev, "level": ev.level,
                    "level_name": config.LEVEL_META.get(ev.level, {}).get("name", ""),
                    "delivered": False, "suppressed": True, "suppress_reason": why,
                    "pushed_to": ev.pushed_to, "external": {},
                    "reason": "同类告警窗口内抑制，避免重复误报",
                    "level_reason": meta.get("level_reason") or "",
                    "camera_factor": factor, "camera_note": fnote}

    store.save_alert(ev)
    store.log("分级告警", "%s %s（%s，置信度 %.2f，推送：%s）"
              % (room.name, ev.behavior_name,
                 config.LEVEL_META.get(ev.level, {}).get("name", ev.level),
                 ev.confidence, "、".join(ev.pushed_to or [])),
              operator, room.room_id)

    ext = {}
    if push:
        ext = push_to_terminal(room, ev, operator=operator)
        ev.external = ext
        ev.delivered = 1 if ext.get("ok") else 0
        store.save_alert(ev)
        if ext.get("ok"):
            store.log("告警已送终端", "%s %s -> 03 告警号 %s（级别 %s%s）"
                      % (room.name, ev.behavior_name, ext.get("alert_id"),
                         ext.get("level"), "，已降级" if ext.get("downgraded") else ""),
                      operator, room.room_id)

    return {"ok": True, "event": ev, "level": ev.level,
            "level_name": config.LEVEL_META.get(ev.level, {}).get("name", ""),
            "delivered": bool(ev.delivered), "suppressed": False,
            "suppress_reason": "", "pushed_to": ev.pushed_to, "external": ext,
            "reason": meta.get("level_reason") or "",
            "level_reason": meta.get("level_reason") or "",
            "camera_factor": factor, "camera_note": fnote,
            "force_confirm": bool(config.LEVEL_META.get(ev.level, {}).get(
                "force_confirm"))}


def push_to_terminal(room, ev, operator="10_room"):
    """把告警送进 03 智能终端（走 00 联动；画质系数一起带上）。"""
    detail = "[%s/%s] %s" % (room.name, room.type_name(), ev.detail or ev.behavior_name)
    if ev.zone:
        detail += "；分区 %s" % ev.zone
    res = linkage_client.raise_alert(
        code=ev.alert_code or "PERSON_BEHAVIOR_ABNORMAL",
        device_code=ev.device_code, room_id=room.name, seat_no=ev.seat_no,
        detail=detail, candidate_id=ev.candidate_id, level=ev.level,
        operator=operator)
    if not res.get("ok"):
        store_note = str(res.get("reason"))[:200]
        return {"ok": False, "reason": store_note, "status": STATUS_FAILED}
    d = res.get("data") or {}
    return {"ok": True, "alert_id": d.get("alert_id"), "level": d.get("level"),
            "downgraded": d.get("downgraded"),
            "credibility": d.get("credibility"),
            "force_confirm": d.get("force_confirm"), "status": STATUS_DELIVERED,
            "source": "03_智能终端操作"}


def resolve_alert(store, event, conclusion, reason="", operator="", witness=""):
    """
    复核结论回流：成立/不成立/误报。
    * 结论非法直接拒绝（避免把"误报"记成"成立"糊过去）
    * 误报进入算法迭代闭环（本模块统计误报率，见 kpi.py）
    * 同时把结论回流 03（如果有对应告警号）
    """
    if event is None:
        return False, "告警事件不存在"
    if conclusion not in config.REVIEW_CONCLUSIONS:
        return False, "非法复核结论：%s（可选：%s）" % (
            conclusion, "、".join(config.REVIEW_CONCLUSIONS))
    if not (operator or "").strip():
        return False, "复核必须记操作人（留痕要求）"
    ext_ok, ext_msg = True, ""
    alert_id = (event.external or {}).get("alert_id")
    if alert_id:
        r = linkage_client.review_alert(alert_id, conclusion=conclusion,
                                       reason=reason or "10 考场复核",
                                       operator=operator)
        # 03 的规则：红色预警必须先确认（ack）才能复核。
        # 03 的 integration_api 没开 ack 的 op，这里先补一次确认（走 03 自己的
        # 存储层 API，**不改 03 代码**），再重试复核。
        if not r.get("ok") and "必须先确认" in str(r.get("reason") or ""):
            ack = linkage_client.ack_alert(alert_id, operator=operator,
                                          note="10 考场复核前置确认")
            if not ack.get("ok"):
                ext_ok = False
                ext_msg = "红色预警需先确认，确认失败：%s" % str(ack.get("reason"))[:120]
            else:
                store.log("红色预警确认", "%s %s -> 已确认后复核"
                          % (event.room_name, event.behavior_name), operator,
                          event.room_id)
                r = linkage_client.review_alert(
                    alert_id, conclusion=conclusion,
                    reason=reason or "10 考场复核", operator=operator)
                ext_ok = bool(r.get("ok"))
                ext_msg = "" if ext_ok else str(r.get("reason"))[:160]
        else:
            ext_ok = bool(r.get("ok"))
            ext_msg = "" if ext_ok else str(r.get("reason"))[:160]
    rec = models.PrivacyRecord(action="review", room_id=event.room_id,
                               room_name=event.room_name,
                               domain=event.room_type,
                               operator=operator, witness=witness,
                               result=conclusion,
                               detail="告警复核：%s（%s）%s"
                                      % (event.behavior_name,
                                         config.REVIEW_CONCLUSIONS[conclusion],
                                         reason or ""))
    store.save_privacy(rec)
    store.log("告警复核", "%s %s -> %s%s"
              % (event.room_name, event.behavior_name,
                 config.REVIEW_CONCLUSIONS[conclusion],
                 "（未回流 03：%s）" % ext_msg if not ext_ok else "（已回流 03）"),
              operator, event.room_id)
    return True, {"conclusion": conclusion,
                  "conclusion_text": config.REVIEW_CONCLUSIONS[conclusion],
                  "closed_loop": conclusion == config.REVIEW_FALSE_POSITIVE,
                  "pushed_to_03": ext_ok, "message": ext_msg}


def false_positive_rate(store, room_id=None):
    """误报率：只认复核结论为"误报"的告警（不拿"告警数/识别数"冒充）。"""
    reviews = store.list_privacy(room_id=room_id, action="review", limit=10000)
    total = len(reviews)
    fp = len([r for r in reviews if r.result == config.REVIEW_FALSE_POSITIVE])
    confirmed = len([r for r in reviews if r.result == config.REVIEW_CONFIRMED])
    rejected = len([r for r in reviews if r.result == config.REVIEW_REJECTED])
    return {"reviewed": total, "false_positive": fp, "confirmed": confirmed,
            "rejected": rejected,
            "rate": (float(fp) / total) if total else None,
            "source": "复核结论" if total else "（尚无复核记录）"}


def stats(store, room_id=None):
    s = store.alert_stats(room_id=room_id)
    s["false_positive"] = false_positive_rate(store, room_id=room_id)
    s["top_behaviors"] = store.behavior_stats(limit=8)
    return s
