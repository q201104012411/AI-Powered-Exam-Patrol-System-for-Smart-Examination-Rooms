# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 复检与处置

报警不是结论，复检才是。本模块负责把"安检门响了"变成一条**有责任人的处置结论**：

   报警单 -> 复检（手持探测器/手检/重过门/转人工通道）-> 结论四种：
     release         复检无异常，放行
     detain          查获器材，暂扣并登记（可自动分配保管柜格）
     manual_channel  转人工安检通道（孕妇/植入医疗器械等特殊考生）
     false_positive  确认误报（回写算法迭代清单，用来调阈值）
     duplicate       重复过检（同一人刚报过，避免同一次进入被罚两次）

配套还管两件事：
  · SLA 催办：报警后 REINSPECT_SLA_SEC 秒内没处置，界面标红；
    超过 REINSPECT_TIMEOUT_ESCALATE 秒仍未处置，自动上报 03 智能终端。
  · 复检耗时统计：平均/最长处置时长，是"不成为入场瓶颈"的证据之一。
"""
import config
import models


def pending_queue(store, limit=100):
    """待复检队列：红色优先、超时优先、越早越靠前。"""
    rows = store.list_alarms(pending_only=True, limit=limit)

    def key(a):
        return (0 if a.level == models.LEVEL_RED else 1,
                0 if a.overdue() else 1,
                a.created_epoch)
    return sorted(rows, key=key)


def start(store, alarm_id, operator="", method="handheld", handheld_code="",
          witness=""):
    """
    开始复检：把报警单从"待复检"推进到"复检中"，并建立一条复检记录。
    返回 (ok, Reinspect|原因)
    """
    alarm = store.get_alarm(alarm_id)
    if alarm is None:
        return False, "报警单不存在"
    if alarm.status == models.ST_ALARM_CLOSED:
        return False, "该报警已处置，不能重复复检"
    if not operator:
        return False, "复检必须记录操作人（留痕要求）"
    r = models.Reinspect(alarm_id=alarm.alarm_id, event_id=alarm.event_id,
                         operator=operator, method=method,
                         handheld_code=handheld_code, witness=witness)
    store.save_reinspect(r)
    store.set_alarm_status(alarm.alarm_id, models.ST_ALARM_REINSPECT)
    store.log("开始复检", "%s %s 由 %s 复检（%s）"
              % (alarm.alarm_id, alarm.kinds_text(), operator,
                 config.REINSPECT_METHODS.get(method, method)), operator)
    return True, r


def finish(store, alarm_id, result, found_kind="", found_detail="",
           storage_cell="", note="", operator="", locker_code=None, auto_lock=True):
    """
    完成复检：写结论 -> 关闭报警单 -> （查获时）把手机送进保管柜 -> 审计。
    返回 (ok, 说明|Reinspect)
    """
    alarm = store.get_alarm(alarm_id)
    if alarm is None:
        return False, "报警单不存在"
    if alarm.status == models.ST_ALARM_CLOSED:
        return False, "该报警已处置（%s）" % (alarm.closed_at or "")
    if result not in config.REINSPECT_RESULTS:
        return False, "结论取值非法：%s（可选：%s）" % (
            result, "/".join(config.REINSPECT_RESULTS))

    r = store.get_reinspect(alarm.alarm_id)
    if r is None:
        ok, r = start(store, alarm.alarm_id, operator=operator or "复检员")
        if not ok:
            return False, r
    r.result = result
    r.found_kind = found_kind or r.found_kind
    r.found_detail = found_detail or r.found_detail
    r.storage_cell = storage_cell or r.storage_cell
    r.note = (r.note + "；" + note) if (r.note and note) else (note or r.note)
    if operator:
        r.operator = operator
    r.finished_epoch = models.now_epoch()
    r.finished_at = models.now_ts()
    store.save_reinspect(r)
    store.set_alarm_status(alarm.alarm_id, models.ST_ALARM_CLOSED, closed=True)

    msg = "结论：%s（耗时 %.0f 秒）" % (r.result_text(), r.cost_sec())
    # 查获器材 -> 自动分配保管柜格（"人-物-时"闭环）
    if result == "detain" and auto_lock:
        import locker as locker_mod
        ok2, info = locker_mod.store_device(
            store, locker_code=locker_code or "",
            candidate_id=alarm.candidate_id or "未核验",
            candidate_name=alarm.candidate_name or "", event_id=alarm.event_id,
            device_desc=(found_detail or config.KIND_TEXT.get(found_kind, "查获器材")),
            method="manual", operator=r.operator, note="复检查获自动入柜")
        if ok2:
            r.storage_cell = info.cell_no
            store.save_reinspect(r)
            msg += "；已存入保管柜 %s/%s" % (info.locker_code, r.storage_cell)
        else:
            msg += "；入柜失败：%s" % info
    store.log("复检处置", "%s %s -> %s" % (alarm.alarm_id, alarm.kinds_text(),
                                        r.result_text()), r.operator)
    return True, msg


def escalate_overdue(store, link=None, limit=20):
    """
    把超过 REINSPECT_TIMEOUT_ESCALATE 秒仍未处置的报警上报 03 智能终端
    （安检口堵人的风险要让人看得见）。返回已上报的报警号列表。
    """
    now = models.now_epoch()
    out = []
    for a in store.list_alarms(pending_only=True, limit=limit):
        if a.reported:
            continue
        if now - a.created_epoch < config.REINSPECT_TIMEOUT_ESCALATE:
            continue
        bridge = link
        if bridge is None:
            try:
                import linkage_bridge as bridge       # noqa: F401
            except Exception:
                bridge = None
        if bridge is None:
            break
        try:
            info = bridge.report_timeout(a)
        except Exception as e:                        # noqa: BLE001
            store.log("超时上报失败", "%s %s" % (a.alarm_id, str(e)[:120]), "system")
            continue
        store.mark_alarm_reported(a.alarm_id, str(info)[:200])
        store.log("复检超时上报", "%s 超 %d 秒未处置"
                  % (a.alarm_id, int(now - a.created_epoch)), "system")
        out.append(a.alarm_id)
    return out


def handle_special_pass(store, alarm_id, candidate, reason, inspector, witness,
                        method="patdown", detail=""):
    """
    报警 -> 判定为特殊考生（孕妇/植入医疗器械）-> 转人工安检通道。
    登记与双人见证校验统一走 special 模块（单一校验路径，避免两处规则不一致）。
    """
    import special
    ok, res = special.transfer_from_alarm(store, alarm_id, candidate, reason, inspector,
                                         witness, method=method, detail=detail)
    if not ok:
        return False, res
    return True, {"record_id": res.record_id, "reason": res.reason,
                  "witness": witness, "detail": "已转人工安检通道并留痕"}


def stats(store, since_epoch=None):
    return store.reinspect_stats(since_epoch)


def label_of(result):
    return config.REINSPECT_RESULTS.get(result, result)
