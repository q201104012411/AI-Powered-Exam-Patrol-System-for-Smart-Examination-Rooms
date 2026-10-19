# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 考试时间表与开关窗口

红线的第三条是"时间严格限定：仅考试时段开启，结束后立即关闭并留存开关记录"。这条
不能靠人记，必须由时间表驱动：

    考试时间表（场次） -> 每场的开关窗口 [开考前 pre_open, 结束 + post_close]
                      -> 屏蔽器只能在窗口内开启（提前/延后都会被拦下并留痕）
                      -> 结束后自动关闭（auto_off）
                      -> 开关记录与时间表做**一致性核对**，不一致就是验收问题

窗口口径（默认）：
    开考前 15 分钟允许开启（发卷、考生入场前完成压制），考试结束当刻关闭。
    留出 `config.SWITCH_GRACE_SEC`（30 秒）容差给设备指令执行延迟。
"""
import config
import models
from models import Session

PHASE_TEXT = {
    "before": "未到窗口",
    "pre_open": "开考前窗口（已允许开启）",
    "active": "考试进行中",
    "post_close": "结束后延时窗口",
    "closed": "窗口已结束",
}
PHASE_ORDER = ("before", "pre_open", "active", "post_close", "closed")


# =============================================================================
# 单场次的时间计算
# =============================================================================
def window(session, cfg=None):
    """
    返回 (open_epoch, close_epoch, start_epoch, end_epoch)。
    日期/时间非法时返回 (0, 0, 0, 0) —— 调用方按"无效场次"处理，不猜。
    """
    cfg = cfg or config.load_settings()
    start_sec = models.parse_hhmm(session.start)
    end_sec = models.parse_hhmm(session.end)
    if start_sec is None or end_sec is None or not session.date:
        return 0.0, 0.0, 0.0, 0.0
    if end_sec <= start_sec:                        # 跨零点（夜考/演练）
        end_sec += 86400
    start = models.date_epoch(session.date, start_sec)
    if start is None:
        return 0.0, 0.0, 0.0, 0.0
    end = start + (end_sec - start_sec)
    pre = models.to_int(session.pre_open_sec, models.to_int(cfg.get("pre_open_sec"),
                                                          config.PRE_OPEN_DEFAULT_SEC))
    post = models.to_int(session.post_close_sec,
                         models.to_int(cfg.get("post_close_sec"),
                                       config.POST_CLOSE_DEFAULT_SEC))
    return (start - max(0, pre), end + max(0, post), start, end)


def state_at(session, now=None, cfg=None):
    """场次在某一时刻处于哪个阶段（before/pre_open/active/post_close/closed）。"""
    now = float(now if now is not None else models.now_epoch())
    op, cl, st, en = window(session, cfg)
    if not st:
        return "closed"
    if now < op:
        return "before"
    if now < st:
        return "pre_open"
    if now <= en:
        return "active"
    if now <= cl:
        return "post_close"
    return "closed"


def allowed_to_shield(session, now=None, cfg=None):
    """该场次此刻是否允许开启屏蔽（窗口内 = 允许）。"""
    return state_at(session, now, cfg) in ("pre_open", "active", "post_close")


def remaining_text(session, now=None, cfg=None):
    """给界面看的"距开始/结束还有多久"。"""
    now = float(now if now is not None else models.now_epoch())
    op, cl, st, en = window(session, cfg)
    if not st:
        return "时间未设置"
    phase = state_at(session, now, cfg)
    if phase == "before":
        return "距开考 %.1f 分钟（窗口将在 %s 打开）" % (
            (st - now) / 60.0, models.ts_text(op)[11:16])
    if phase == "pre_open":
        return "距开考 %.1f 分钟，屏蔽可开启" % ((st - now) / 60.0)
    if phase == "active":
        return "考试中，距结束 %.1f 分钟" % ((en - now) / 60.0)
    if phase == "post_close":
        return "已结束，关闭窗口还剩 %.1f 分钟" % ((cl - now) / 60.0)
    return "已结束"


# =============================================================================
# 场次查找
# =============================================================================
def _all_valid(store):
    out = []
    for s in store.list_sessions(limit=2000):
        op, cl, st, en = window(s)
        if st:
            out.append((s, op, cl, st, en))
    return out


def find_window(store, now=None, cfg=None):
    """
    找此刻处于开关窗口内的场次（优先"考试中"，其次"开考前/结束后延时"）。
    返回 (Session|None, phase)；没有则 (None, "")。
    """
    now = float(now if now is not None else models.now_epoch())
    hits = []
    for s, op, cl, st, en in _all_valid(store):
        if op <= now <= cl:
            phase = state_at(s, now, cfg)
            priority = {"active": 0, "pre_open": 1, "post_close": 2}.get(phase, 3)
            hits.append((priority, s, phase))
    if not hits:
        return None, ""
    hits.sort(key=lambda x: x[0])
    return hits[0][1], hits[0][2]


def active_session(store, now=None):
    """正在考试中的场次（[start, end] 内）。"""
    now = float(now if now is not None else models.now_epoch())
    for s, op, cl, st, en in _all_valid(store):
        if st <= now <= en:
            return s
    return None


def next_session(store, now=None):
    """下一场还没到窗口的场次。"""
    now = float(now if now is not None else models.now_epoch())
    cands = [(op, s) for s, op, cl, st, en in _all_valid(store) if op > now]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def sessions_on(store, date=None):
    date = date or models.today_str()
    return store.list_sessions(date=date)


# =============================================================================
# 场次维护
# =============================================================================
def add_session(store, name, date, start, end, subject="", pre_open_sec=None,
                post_close_sec=None, rooms=None, operator="", note=""):
    """新增/更新一场考试（同日期+同名则更新）。返回 (ok, info|Session)。"""
    name = str(name or "").strip() or "考试场次"
    date = str(date or models.today_str()).strip()
    if models.parse_hhmm(start) is None or models.parse_hhmm(end) is None:
        return False, "起止时间格式必须是 HH:MM"
    start_sec = models.parse_hhmm(start)
    end_sec = models.parse_hhmm(end)
    if end_sec <= start_sec:
        return False, "结束时间必须晚于开始时间（跨零点请拆成两场）"
    if models.date_epoch(date) is None:
        return False, "日期格式必须是 YYYY-MM-DD"
    cfg = config.load_settings()
    pre = models.to_int(pre_open_sec, models.to_int(cfg.get("pre_open_sec"),
                                                   config.PRE_OPEN_DEFAULT_SEC))
    post = models.to_int(post_close_sec, models.to_int(cfg.get("post_close_sec"),
                                                      config.POST_CLOSE_DEFAULT_SEC))
    if not (0 <= pre <= 7200):
        return False, "开考前窗口必须在 0~7200 秒之间"
    if not (0 <= post <= 7200):
        return False, "结束后延时必须在 0~7200 秒之间"
    existing = None
    for s in store.list_sessions(date=date, limit=500):
        if s.name == name:
            existing = s
            break
    s = existing or Session()
    s.name = name
    s.subject = str(subject or s.subject or "")
    s.date = date
    s.start = models.hhmm_sec(start_sec)
    s.end = models.hhmm_sec(end_sec)
    s.pre_open_sec = pre
    s.post_close_sec = post
    s.rooms = list(rooms if rooms is not None else (s.rooms or []))
    s.operator = str(operator or s.operator or "")
    s.note = str(note or s.note or "")
    if not s.state:
        s.state = config.SESSION_STATE_PLANNED
    store.save_session(s)
    store.log("场次登记" if not existing else "场次更新",
              "%s %s %s~%s（提前 %d 秒开启）"
              % (s.date, s.name, s.start, s.end, s.pre_open_sec), operator)
    return True, s


def refresh_states(store, now=None, cfg=None):
    """把场次状态同步成 planned/active/closed；返回状态发生变化的场次列表。"""
    now = float(now if now is not None else models.now_epoch())
    changed = []
    for s, op, cl, st, en in _all_valid(store):
        if now > en:
            want = config.SESSION_STATE_CLOSED
        elif now >= st:
            want = config.SESSION_STATE_ACTIVE
        else:
            want = config.SESSION_STATE_PLANNED
        if s.state != want:
            s.state = want
            store.save_session(s)
            changed.append(s)
    return changed


def tick(store, now=None, cfg=None):
    """时间推进：刷新场次状态，返回 {changed, in_window, phase, remaining}。"""
    now = float(now if now is not None else models.now_epoch())
    changed = refresh_states(store, now, cfg)
    sess, phase = find_window(store, now, cfg)
    return {"now": now, "states_changed": [s.to_dict() for s in changed],
            "in_window": bool(sess), "session": sess.to_dict() if sess else None,
            "phase": phase, "phase_text": PHASE_TEXT.get(phase, ""),
            "remaining": remaining_text(sess, now, cfg) if sess else "无窗口"}


# =============================================================================
# 开关记录 <-> 时间表 一致性核对
# =============================================================================
def switch_intervals(store, device_id=None, now=None):
    """
    把开关记录还原成"开启区间"。

    返回 (intervals, problems)：
      intervals: [{device_id, start, end, start_log, end_log, open_now}]
      problems:  开关记录本身的异常（有 off 无 on、有 on 无 off 且已超窗口）
    """
    now = float(now if now is not None else models.now_epoch())
    logs = store.list_switches(device_id=device_id, limit=5000)
    logs = sorted(logs, key=lambda x: (x.ts_epoch, x.log_id))
    intervals, problems = [], []
    open_log = {}
    for lg in logs:
        dev = lg.device_id
        if lg.action == config.SW_ON:
            if dev in open_log:
                problems.append({"device_id": dev, "kind": "duplicate_on",
                                 "detail": "重复开启（上一条开启没有对应关闭）",
                                 "at": lg.ts})
            open_log[dev] = lg
        elif lg.action == config.SW_OFF:
            if dev not in open_log:
                problems.append({"device_id": dev, "kind": "off_without_on",
                                 "detail": "关闭记录没有对应的开启记录",
                                 "at": lg.ts})
                continue
            on_log = open_log.pop(dev)
            if lg.ts_epoch < on_log.ts_epoch:
                problems.append({"device_id": dev, "kind": "time_reversed",
                                 "detail": "关闭早于开启（设备时钟异常？）",
                                 "at": lg.ts})
                continue
            intervals.append({"device_id": dev, "start": on_log.ts_epoch,
                              "end": lg.ts_epoch, "start_log": on_log,
                              "end_log": lg, "open_now": False})
        elif lg.action == config.SW_FAULT:
            problems.append({"device_id": dev, "kind": "fault",
                             "detail": "设备故障记录：%s" % (lg.reason or "-"),
                             "at": lg.ts})
    for dev, on_log in open_log.items():
        intervals.append({"device_id": dev, "start": on_log.ts_epoch, "end": now,
                          "start_log": on_log, "end_log": None, "open_now": True})
    intervals.sort(key=lambda x: x["start"])
    return intervals, problems


def consistency(store, now=None, cfg=None):
    """
    开关记录与考试时间表一致性核对（验收必查项）。

    逐条检查：
      · 开启时不在任何场次窗口内                 -> fail「非考试时段开启」
      · 早于窗口打开时间（超过容差）             -> fail「提前开启」
      · 晚于窗口关闭时间（超过容差）             -> fail「超时未关」
      · 窗口内却没有任何屏蔽区间覆盖              -> fail「考试时段未开屏蔽」
      · 区间没覆盖整场（开始晚/结束早）          -> warn「覆盖不完整」
      · 开关记录本身异常（重复开启/关闭无开启）   -> warn
    返回 {rows, fail, warn, checked_sessions, intervals, closed_ok}
    """
    cfg = cfg or config.load_settings()
    now = float(now if now is not None else models.now_epoch())
    grace = float(config.SWITCH_GRACE_SEC)
    windows = _all_valid(store)
    intervals, problems = switch_intervals(store, now=now)
    rows = []
    for p in problems:
        rows.append({"level": "warn", "kind": p["kind"], "detail": p["detail"],
                     "device_id": p["device_id"], "at": p["at"]})
    for it in intervals:
        overlaps = [(s, op, cl, st, en) for (s, op, cl, st, en) in windows
                    if it["start"] <= cl and it["end"] >= op]
        if not overlaps:
            rows.append({"level": "fail", "kind": "off_schedule",
                         "detail": "开启区间不在任何考试窗口内（%s ~ %s）"
                                   % (models.ts_text(it["start"])[5:16],
                                      models.ts_text(it["end"])[5:16]),
                         "device_id": it["device_id"], "at": models.ts_text(it["start"])})
            continue
        # 取覆盖最深的窗口做核对
        s, op, cl, st, en = max(overlaps, key=lambda x: min(it["end"], x[2]) -
                                                      max(it["start"], x[1]))
        if it["start"] < op - grace:
            rows.append({"level": "fail", "kind": "early_on",
                         "detail": "提前开启 %.1f 分钟（窗口 %s 才允许）"
                                   % ((op - it["start"]) / 60.0, models.ts_text(op)[5:16]),
                         "device_id": it["device_id"], "session_id": s.session_id,
                         "at": models.ts_text(it["start"])})
        if it["open_now"] and now > cl + grace:
            rows.append({"level": "fail", "kind": "still_on",
                         "detail": "窗口已于 %s 关闭，设备仍在开启（已超 %.1f 分钟）"
                                   % (models.ts_text(cl)[5:16], (now - cl) / 60.0),
                         "device_id": it["device_id"], "session_id": s.session_id,
                         "at": models.ts_text(now)})
        elif (not it["open_now"]) and it["end"] > cl + grace:
            rows.append({"level": "fail", "kind": "late_off",
                         "detail": "晚关闭 %.1f 分钟（窗口 %s 已结束）"
                                   % ((it["end"] - cl) / 60.0, models.ts_text(cl)[5:16]),
                         "device_id": it["device_id"], "session_id": s.session_id,
                         "at": models.ts_text(it["end"])})
        covered_start = it["start"] <= st + grace
        covered_end = (it["end"] >= en - grace) or it["open_now"]
        if not (covered_start and covered_end):
            rows.append({"level": "warn", "kind": "partial",
                         "detail": "未覆盖整场（%s~%s，实际 %s~%s）"
                                   % (s.start, s.end,
                                      models.ts_text(it["start"])[11:16],
                                      models.ts_text(it["end"])[11:16]),
                         "device_id": it["device_id"], "session_id": s.session_id,
                         "at": models.ts_text(it["start"])})

    checked = 0
    for s, op, cl, st, en in windows:
        if en > now:                        # 还没结束的场次不判"漏开"
            continue
        checked += 1
        covered = any(it["start"] <= en and it["end"] >= st for it in intervals)
        if not covered:
            rows.append({"level": "fail", "kind": "missing_shield",
                         "detail": "整场考试没有任何屏蔽开启记录（%s %s~%s）"
                                   % (s.date, s.start, s.end),
                         "session_id": s.session_id, "at": "%s %s" % (s.date, s.start)})
    fail = sum(1 for r in rows if r["level"] == "fail")
    warn = sum(1 for r in rows if r["level"] == "warn")
    return {"rows": rows, "fail": fail, "warn": warn, "checked_sessions": checked,
            "intervals": len(intervals), "closed_ok": fail == 0,
            "note": "容差 %d 秒；已结束场次 %d 场" % (int(grace), checked)}


def describe():
    cfg = config.load_settings()
    return {"窗口口径": "开考前 %d 分钟可开启；结束 %d 秒后必须关闭"
                        % (models.to_int(cfg.get("pre_open_sec"), 900) // 60,
                           models.to_int(cfg.get("post_close_sec"), 0)),
            "容差": "%d 秒" % config.SWITCH_GRACE_SEC,
            "状态": "、".join(PHASE_TEXT[p] for p in PHASE_ORDER)}
