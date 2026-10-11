# -*- coding: utf-8 -*-
"""
02 -> 03 实时告警上报桥接

通过 00_集成联动 的 Linkage 把 02 发现的机位异常直接送到 03 智能终端。
调用是子进程隔离的，不会影响 02 本身的运行。
"""
import os
import sys
import threading
import traceback

# 让本文件能找到 00_集成联动 下的 linkage.py
LINKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "00_集成联动")
sys.path.insert(0, LINKAGE_DIR)
import linkage  # noqa: E402

_lk = None
_lock = threading.Lock()


def _linkage():
    global _lk
    if _lk is None:
        _lk = linkage.Linkage()
    return _lk


def report_check(device, check_result, operator="02_patrol"):
    """
    把一次机位核查的异常结果上报到 03 智能终端。
    正常结果不上报，避免噪音。
    """
    if check_result.ok or not check_result.defects:
        return
    # 取最高级别缺陷作为告警码；严重优先
    sev_order = {"fail": 0, "warn": 1, "info": 2}
    defects = sorted(check_result.defects,
                     key=lambda d: sev_order.get(getattr(d, "severity", ""), 99))
    code = defects[0].code
    detail = "；".join("%s(%s)" % (d.code, getattr(d, "severity", "-")) for d in defects)

    def _push():
        try:
            _linkage().raise_alert(
                code=code,
                device_code=device.code,
                room_id=device.room_id or "",
                seat_no="",
                detail=detail,
                operator=operator)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=_push, daemon=True).start()


def report_behavior(device_code, behavior, pid=0, crosses=0,
                    room_id="", seat_no="", operator="02_ai_monitor"):
    """
    把 02 实时监控 AI 逐人识别到的异常行为上报到 03 智能终端。
    由智能终端做分级展示、强制确认、复核留痕。
    """
    code_map = {
        "触碰红线": "REPEATED_TOUCH_LINE",
        "越界": "PERSON_OUT_OF_BOUNDS",
        "大幅度偏转": "BIG_TURN",
        "起立/站立": "STAND_UP",
        "前倾/趴桌": "LEAN_FORWARD",
        "东张西望": "TURN_HEAD",
        "长时间低头": "LOOK_DOWN",
    }
    alert_code = code_map.get(behavior, "PERSON_BEHAVIOR_ABNORMAL")
    detail = "AI 实时监控：#%d 号人员「%s」" % (pid, behavior)
    if crosses >= 2:
        detail += "（已触碰警戒线 %d 次）" % crosses
    detail += "，请智能终端复核处置。"

    def _push():
        try:
            _linkage().raise_alert(
                code=alert_code,
                device_code=device_code,
                room_id=room_id,
                seat_no=seat_no,
                detail=detail,
                operator=operator)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=_push, daemon=True).start()
