# -*- coding: utf-8 -*-
"""
06 -> 03 报警上报桥接

安检口是第一道防线，报警必须让**智能终端**看得见、能处置，所以复用 00_集成联动：
  · 告警码走 03 的分级表：手机/无线耳机 -> PHONE_DETECTED（红），
    金属/强磁/鞋底 -> SECURITY_ALARM（橙），复检超时 -> SECURITY_REINSPECT_TIMEOUT，
    保管柜超时未取 -> LOCKER_ABNORMAL
  · 03 接入时会按 02 给出的机位画质可信度降权（对安检门而言通常是"未知机位，不降权"）
  · 调用是**子进程隔离**的，03 不在/报错都不会影响 06 本身继续跑

默认不推（config.SETTINGS_DEFAULT["report_to_terminal"]=False），
避免每次报警都刷屏终端；界面「过检与报警」页可以逐条手动上报。
"""
import os
import sys
import threading
import traceback

import config                                                     # noqa: E402

LINKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "00_集成联动")
if LINKAGE_DIR not in sys.path:
    sys.path.insert(0, LINKAGE_DIR)

import models                                                    # noqa: E402

_lk = None
_lock = threading.Lock()


def _linkage():
    global _lk
    if _lk is None:
        import linkage
        _lk = linkage.Linkage()
    return _lk


def available():
    """联动是否可用（00_集成联动/03 是否就位），给界面如实显示。"""
    try:
        lk = _linkage()
        ok, why = lk.p03.available()
        return ok, why
    except Exception as e:                                       # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def _async(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def _candidate_text(alarm):
    if alarm.candidate_name:
        return "%s(%s)" % (alarm.candidate_name, alarm.candidate_id or "未核验")
    return alarm.candidate_id or "未核验"


def _raise(code, device_code, room_id="", seat_no="", detail="", candidate_id="",
           candidate_name="", operator="06_security"):
    return _linkage().raise_alert(code=code, device_code=device_code or "GATE-UNKNOWN",
                                  room_id=room_id, seat_no=str(seat_no or ""),
                                  detail=detail, candidate_id=candidate_id,
                                  candidate_name=candidate_name, operator=operator)


def report_alarm(alarm, event=None, sync=False, operator="06_security"):
    """
    把一条安检报警送到 03 智能终端。
    sync=False（默认）后台线程推送，界面不卡；sync=True 等结果（自检/手动上报用）。
    返回 dict（含 alert_id / level / downgraded）或 None。
    """
    code = models.ALERT_CODE_BY_KIND.get(alarm.kind, "SECURITY_ALARM")
    src = "模拟" if (event is not None and event.source == "sim") else "真机"
    detail = ("安检报警（%s，来源=%s）：%s ｜设备 %s｜通道 %s｜考生 %s｜"
              "请复检并按「放行/暂扣/转人工通道/误报」处置"
              % (alarm.kinds_text(), src, alarm.detail or "-", alarm.device_code or "-",
                 alarm.channel_no or "-", _candidate_text(alarm)))
    if event is not None and event.scores:
        detail += "｜感应值 %s" % "、".join(
            "%s=%.2f" % (k, v) for k, v in sorted(event.scores.items()))

    def _work():
        try:
            r = _raise(code, alarm.device_code, alarm.room_id, "", detail,
                       alarm.candidate_id, alarm.candidate_name, operator)
            return r
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            return None

    if sync:
        return _work()
    _async(_work)
    return {"queued": True, "code": code}


def report_timeout(alarm, sync=False, operator="06_security"):
    """复检超时未处置 -> 上报（安检口堵人风险要让人看见）。"""
    detail = ("安检复检超时：报警 %s（%s）已 %.0f 秒未处置（SLA %d 秒），"
              "现场可能已开始排队，请智能终端催办"
              % (alarm.alarm_id, alarm.kinds_text(),
                 models.now_epoch() - alarm.created_epoch, config.REINSPECT_SLA_SEC))

    def _work():
        try:
            return _raise(models.ALERT_CODE_TIMEOUT, alarm.device_code, alarm.room_id, "",
                          detail, alarm.candidate_id, alarm.candidate_name, operator)
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            return None

    if sync:
        return _work()
    _async(_work)
    return {"queued": True, "code": models.ALERT_CODE_TIMEOUT}


def report_locker(cell, sync=False, operator="06_security"):
    """保管柜超时未取 -> 提醒（考后集中取件、避免手机滞留与纠纷）。"""
    detail = ("手机保管柜超时未取：%s/%s 考生 %s 存入「%s」已 %.0f 分钟"
              % (cell.locker_code, cell.cell_no, _mask(cell.candidate_id),
                 cell.device_desc or "手机",
                 (models.now_epoch() - cell.stored_epoch) / 60.0))

    def _work():
        try:
            return _raise(models.ALERT_CODE_LOCKER, cell.locker_code, "", "",
                          detail, cell.candidate_id, cell.candidate_name, operator)
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            return None

    if sync:
        return _work()
    _async(_work)
    return {"queued": True, "code": models.ALERT_CODE_LOCKER}


def report_special(record, sync=False, operator="06_security"):
    """特殊考生转人工通道（留痕到 03，便于指挥中心掌握替代通道使用情况）。"""
    detail = ("特殊考生人工安检：%s（%s）走人工安检通道，主检 %s、见证 %s，"
              "处置 %s" % (record.reason_text(), _mask(record.candidate_id),
                         record.inspector or "-", record.witness or "-",
                         record.result_text()))

    def _work():
        try:
            return _raise(models.ALERT_CODE_SPECIAL, "SPECIAL-CHANNEL", record.room_id,
                          record.seat_no, detail, record.candidate_id,
                          record.candidate_name, operator)
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            return None

    if sync:
        return _work()
    _async(_work)
    return {"queued": True, "code": models.ALERT_CODE_SPECIAL}


def _mask(value):
    try:
        import config
        return config.mask_id(value)
    except Exception:                                            # noqa: BLE001
        return str(value or "")


def link_status():
    """给界面/自检看的联动状态：03 是否可用、能否上报。"""
    ok, why = available()
    return {"available": ok, "reason": why,
            "note": "上报走 00_集成联动 -> 03 智能终端（子进程隔离，失败不影响本模块）"}
