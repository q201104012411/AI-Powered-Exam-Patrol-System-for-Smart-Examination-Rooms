# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 特殊考生人工安检通道

《智能安检与手机作弊器材检测.md》关键指标最后一条、实施要点第 2 条：
  **孕妇 / 体内植入医疗器械考生提供人工安检替代通道**，
  单独记录、双人见证。

本模块就是这条合规要求的落地：
  · 登记：原因（孕妇/植入医疗器械/残障/医嘱/其他）+ 主检人 + **见证人**（必须两人）
  · 替代方式：人工手检、手持探测器、脱鞋检查（单独房间）、女性安检员陪检等
  · 演练：验收证据要的"替代通道演练记录"，一键生成（标 drill=True，不冒充真实事件）
  · 合规提示：把话说清楚——不允许因为"智能安检"把特殊考生挡在门外

纪律：
  · 见证人必填且不能与主检人相同（双人见证才有意义）；
  · 演练记录必须标 drill，报告里分开计数，**不与真实事件混在一起**；
  · 只记"安检结论与替代方式"，不采集任何医疗细节（最小必要）。
"""
import config
import models


def alternatives():
    """可用的替代安检方式（界面下拉/提示用）。"""
    return [
        "人工手检（同性别安检员，单独房间）",
        "手持金属探测器全身复检",
        "脱鞋 + 鞋底目视检查（单独房间）",
        "免过安检门，改由人工目视随身物品",
        "考生提供医院证明，考点备案后免检随身医疗器械",
    ]


def policy_notes():
    """合规提示（界面直接显示）。"""
    return [
        "孕妇、体内植入医疗器械（起搏器/骨钉/人工关节）考生**不得强制过安检门**，"
        "须提供人工安检替代通道。",
        "替代通道全程**双人见证**（主检人 + 见证人），单独记录、可追溯。",
        "植入医疗器械会触发金属感应——这是**正确报警**，不是误报；"
        "处理方式是转人工通道，并提醒考生今后主动申报。",
        "检查在单独房间进行，同性别安检员操作；只记录安检结论，不记录医疗细节。",
        "替代通道可用性应作为验收指标：做一次演练并留痕。",
    ]


def validate(candidate_id, candidate_name, reason, inspector, witness):
    """返回 (ok, 原因)。"""
    if not (candidate_id or candidate_name):
        return False, "至少填考生号或姓名（否则无法追溯）"
    if reason not in config.SPECIAL_REASONS:
        return False, "原因取值非法：%s" % reason
    if not inspector:
        return False, "主检人必填（留痕要求）"
    if config.SPECIAL_WITNESS_REQUIRED and not witness:
        return False, "特殊考生人工安检必须双人见证（见证人必填）"
    if witness and witness == inspector:
        return False, "见证人不能与主检人相同（双人见证要求两名不同人员）"
    return True, ""


def register(store, candidate_id="", candidate_name="", ticket_no="", room_id="",
             seat_no="", reason="pregnant", inspector="", witness="",
             method="patdown", result="passed", detail="", decision="",
             drill=False):
    """
    登记一条人工安检记录。返回 (ok, SpecialRecord|原因)。
    """
    ok, why = validate(candidate_id, candidate_name, reason, inspector, witness)
    if not ok:
        return False, why
    if result not in config.SPECIAL_RESULTS:
        return False, "处置取值非法：%s" % result
    rec = models.SpecialRecord(
        candidate_id=candidate_id or "", candidate_name=candidate_name or "",
        ticket_no=ticket_no or "", room_id=room_id or "", seat_no=str(seat_no or ""),
        reason=reason, inspector=inspector, witness=witness, method=method,
        result=result, detail=detail or "", drill=bool(drill),
        decision=decision or "人工手检 + 手持探测器，不强制过安检门")
    store.save_special(rec)
    store.log("特殊考生人工安检",
              "%s %s 主检=%s 见证=%s 处置=%s%s"
              % (rec.record_id, rec.reason_text(), inspector, witness, rec.result_text(),
                 "（演练）" if rec.drill else ""), inspector or "system")
    return True, rec


def ensure_drill(store, operator="", witness="演练见证人", reason="implant",
                 candidate_id="DRILL-001", candidate_name="演练考生"):
    """
    生成/更新一条**演练**记录（验收证据：替代通道演练）。
    演练记录一律 drill=True，报告里单独计数。
    """
    if not operator:
        operator = config.load_settings().get("operator") or "安检员"
    if not witness or witness == operator:
        witness = "演练见证人（第二名安检员）"
    return register(store, candidate_id=candidate_id, candidate_name=candidate_name,
                    reason=reason, inspector=operator, witness=witness,
                    method="patdown", result="passed", drill=True,
                    detail="替代通道演练：孕妇/植入医疗器械考生走人工手检通道，双人见证",
                    decision="人工手检 + 手持探测器，不强制过安检门")


def list_records(store, limit=200, real_only=False, drill_only=False):
    if drill_only:
        return store.list_specials(limit=limit, drill=True)
    if real_only:
        return store.list_specials(limit=limit, drill=False)
    return store.list_specials(limit=limit)


def stats(store, since_epoch=None):
    st = store.special_stats(since_epoch)
    st["alternatives"] = alternatives()
    st["witness_rate"] = (st["double_witness"] / float(st["total"])) if st["total"] else 0.0
    st["available"] = st["total"] > 0
    return st


def transfer_from_alarm(store, alarm_id, candidate, reason, inspector, witness,
                        method="patdown", detail=""):
    """
    报警 -> 判定为特殊考生 -> 转人工安检通道（并把报警单结案为 manual_channel）。
    双人见证不合格时直接拒绝（合规不能让步）。
    """
    ok, res = register(store, candidate_id=candidate.get("candidate_id", ""),
                       candidate_name=candidate.get("candidate_name", ""),
                       ticket_no=candidate.get("ticket_no", ""),
                       room_id=candidate.get("room_id", ""),
                       seat_no=candidate.get("seat_no", ""), reason=reason,
                       inspector=inspector, witness=witness, method=method,
                       detail=detail)
    if not ok:
        return False, res
    if alarm_id:
        import reinspect
        ok2, msg = reinspect.finish(
            store, alarm_id, "manual_channel",
            note="转人工安检通道（%s，双人见证 %s）" % (res.reason_text(), witness),
            operator=inspector)
        if not ok2:
            return False, msg
    return True, res
