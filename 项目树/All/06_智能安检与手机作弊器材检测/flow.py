# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 入场流程（安检-核验-签到三点位串联）

《智能安检与手机作弊器材检测.md》实施要点第 1 条：**流程衔接**——
安检、核验、签到三点位串联，避免重复排队；设置备用通道防止拥堵。

本模块干三件事：
  1) 三点位串联：一个考生一条 flow 记录，安检/核验/签到各打一个时间戳，
     顺序错乱（没安检就去核验）会被标出来，便于现场纠偏。
  2) 效率口径：通过效率（人/分钟）按 THROUGHPUT_WINDOW_SEC 窗口实时算，
     与指标 ≥20 人/分钟 比对；同时算各点位平均耗时找**瓶颈点位**。
  3) 排队疏导：单通道排队人数超阈值就给"加开备用/人工通道"的明确建议，
     并把关键动作写审计（演练记录也是验收证据之一）。
"""
import config
import models


def _fmt_stage(stage):
    return config.STAGE_TEXT.get(stage, stage)


def stage(store, candidate_id, stage_name, candidate_name="", ticket_no="",
          room_id="", seat_no="", event_id="", note="", operator="",
          allow_disorder=False):
    """
    给某考生的某个点位打时间戳（安检/核验/签到）。
    返回 (ok, FlowRecord|原因, 提示)
    """
    cand = (candidate_id or "").strip() or (ticket_no or "").strip()
    if not cand:
        return False, "必须能定位到考生（身份证/准考证号至少给一个）", ""
    if stage_name not in config.STAGE_ORDER:
        return False, "点位取值非法：%s" % stage_name, ""
    f = store.get_flow(cand, room_id=room_id)
    if f is None:
        f = models.FlowRecord(candidate_id=cand, candidate_name=candidate_name,
                              ticket_no=ticket_no, room_id=room_id,
                              seat_no=str(seat_no or ""))
    if candidate_name:
        f.candidate_name = candidate_name
    if ticket_no:
        f.ticket_no = ticket_no
    if room_id:
        f.room_id = room_id
    if seat_no:
        f.seat_no = str(seat_no)
    if note:
        f.note = (f.note + "；" + note) if f.note else note

    # 顺序校验：安检 -> 核验 -> 签到
    idx = config.STAGE_ORDER.index(stage_name)
    missing = [s for s in config.STAGE_ORDER[:idx] if not f.stage_epochs().get(s)]
    if missing and not allow_disorder:
        return False, f, "顺序不对：还没完成%s，不能直接做%s" % (
            "、".join(_fmt_stage(s) for s in missing), _fmt_stage(stage_name))

    tip = ""
    ep = models.now_epoch()
    ts = models.now_ts(ep)
    if stage_name == "security":
        if f.security_epoch:
            f.reentries += 1
            tip = "重复过安检第 %d 次（会记入重复排队）" % f.reentries
        f.security_epoch = ep
        f.security_at = ts
        if event_id:
            f.security_event = event_id
    elif stage_name == "verify":
        f.verify_epoch = ep
        f.verify_at = ts
    else:
        f.signin_epoch = ep
        f.signin_at = ts
    store.save_flow(f)
    store.log("点位打卡", "%s %s（%s）" % (config.mask_id(cand), _fmt_stage(stage_name),
                                       operator or "system"), operator or "system")
    return True, f, tip


def bind_pass(store, event, operator=""):
    """过检记录 -> 自动给该考生打"安检"点位（现场不用再手工点一次）。"""
    if not (event.candidate_id or event.candidate_name):
        return None
    ok, f, _tip = stage(store, event.candidate_id or event.candidate_name, "security",
                        candidate_name=event.candidate_name, room_id=event.room_id,
                        seat_no=event.seat_no, event_id=event.event_id,
                        note="由过检记录自动打卡（%s）" % (
                            "模拟" if event.source == "sim" else event.source),
                        operator=operator, allow_disorder=True)
    return f if ok else None


def advise(queue, open_channels=1, auto_open_backup=None):
    """
    排队疏导建议。
    queue: {通道号: 排队人数} 或 单个整数
    返回 [{channel, queue, level, advice}]
    """
    if auto_open_backup is None:
        auto_open_backup = bool(config.load_settings().get("auto_open_backup", True))
    if isinstance(queue, (int, float)):
        queue = {"1": int(queue)}
    out = []
    for ch, n in sorted((queue or {}).items()):
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n >= config.QUEUE_CRITICAL:
            level, advice = "critical", "严重拥堵：立即加开备用通道 + 人工安检通道，并加强现场引导"
        elif n >= config.QUEUE_WARN:
            level, advice = "warn", "排队偏长：准备加开备用通道（%s）" % (
                "已开自动提示" if auto_open_backup else "自动提示已关")
        else:
            level, advice = "ok", "通畅"
        out.append({"channel": ch, "queue": n, "level": level, "advice": advice,
                    "backup_needed": n >= config.QUEUE_BACKUP_AT})
    return out


def log_queue(store, queue, operator=""):
    """把一次排队观测写进审计（高峰期疏导的证据）。"""
    adv = advise(queue)
    worst = max(adv, key=lambda x: x["queue"]) if adv else None
    if worst and worst["level"] != "ok":
        store.log("排队疏导", "通道%s 排队%d人 -> %s"
                  % (worst["channel"], worst["queue"], worst["advice"]), operator or "system")
    return adv


def efficiency(store, window_sec=None, device_code=None):
    """
    通过效率：窗口内 人/分钟，并给出是否达标（≥TARGET_THROUGHPUT_PPM）。
    """
    window_sec = float(window_sec or config.THROUGHPUT_WINDOW_SEC)
    ppm = store.throughput_ppm(window_sec=window_sec, device_code=device_code)
    return {"window_sec": window_sec, "ppm": ppm,
            "target_ppm": config.TARGET_THROUGHPUT_PPM,
            "ok": ppm >= config.TARGET_THROUGHPUT_PPM}


def efficiency_by_device(store, window_sec=None):
    """按通道分别看效率（找最慢的那条通道）。"""
    window_sec = float(window_sec or config.THROUGHPUT_WINDOW_SEC)
    since = models.now_epoch() - window_sec
    rows = []
    for dev in store.list_devices(kind=config.DEV_GATE):
        n = store.pass_count(since_epoch=since, device_code=dev.code)
        rows.append({"device": dev.code, "name": dev.name, "channel": dev.channel_no,
                     "passes": n, "ppm": n / (window_sec / 60.0),
                     "status": dev.status})
    return sorted(rows, key=lambda r: r["ppm"])


def bottleneck(store, since_epoch=None, limit=500):
    """各点位平均耗时与瓶颈点位（安检/核验/签到串联后谁是短板）。"""
    st = store.flow_stats(since_epoch=since_epoch)
    detail = []
    for k, v in (st.get("avg_stage_sec") or {}).items():
        lim = config.STAGE_BOTTLENECK_SEC.get(k, 60)
        detail.append({"stage": k, "stage_text": _fmt_stage(k), "avg_sec": v,
                       "limit_sec": lim, "over": v > lim})
    return {"summary": st, "stages": detail,
            "bottleneck": _fmt_stage(st.get("bottleneck")) if st.get("bottleneck") else ""}


def record_drill(store, title, detail, operator=""):
    """
    演练记录（验收证据）：高峰期疏导、备用通道启用、特殊考生替代通道演练都走这里。
    """
    store.log("演练记录：" + (title or "入场流程演练"), detail, operator or "system")
    return {"title": title, "detail": detail, "ts": models.now_ts(), "operator": operator}


def demo_flow(store, n=10, operator="演示"):
    """
    **演示用**：给最近 n 条过检记录补上"核验/签到"时间戳，
    用来演示三点位串联与瓶颈统计。补出来的记录 note 里写明"演示数据"。
    """
    made = []
    for ev in store.list_passes(limit=n):
        if not (ev.candidate_id or ev.candidate_name):
            continue
        base = ev.epoch
        ok, f, _tip = stage(store, ev.candidate_id or ev.candidate_name, "security",
                            candidate_name=ev.candidate_name, room_id=ev.room_id,
                            seat_no=ev.seat_no, event_id=ev.event_id,
                            note="演示数据", operator=operator, allow_disorder=True)
        if not ok:
            continue
        f = store.get_flow(f.candidate_id, room_id=f.room_id)
        # 模拟核验 8~25 秒后、签到再 5~18 秒后完成
        f.verify_epoch = base + 8 + (hash(f.flow_id) % 17)
        f.verify_at = models.now_ts(f.verify_epoch)
        f.signin_epoch = f.verify_epoch + 5 + (hash(f.flow_id[::-1]) % 13)
        f.signin_at = models.now_ts(f.signin_epoch)
        store.save_flow(f)
        made.append(f)
    store.log("生成演示流程数据", "%d 条（演示数据，非真实核验/签到）" % len(made), operator)
    return made
