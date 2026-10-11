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
