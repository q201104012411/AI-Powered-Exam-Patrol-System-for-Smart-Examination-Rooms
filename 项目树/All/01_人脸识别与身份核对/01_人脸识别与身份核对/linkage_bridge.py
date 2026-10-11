# -*- coding: utf-8 -*-
"""
01 -> 03 实时告警上报桥接

通过 00_集成联动 的 Linkage 把 01 人脸核验异常（身份不符、未建档等）
直接送到 03 智能终端。
"""
import os
import sys
import threading
import traceback

LINKAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "00_集成联动")
sys.path.insert(0, LINKAGE_DIR)
import linkage  # noqa: E402

_lk = None
_lock = threading.Lock()


def _linkage():
    global _lk
    if _lk is None:
        _lk = linkage.Linkage()
    return _lk


def report_verify_result(rec_dict, operator="01_verify"):
    """
    根据人脸核验结果，若非 PASS 则向 03 智能终端发告警。
    rec_dict 是 verifier 返回结果的字典形式。
    """
    result = rec_dict.get("result", "")
    if result == "PASS":
        return

    code = rec_dict.get("reason_code", "") or "FACE_VERIFY_FAIL"
    # 把 reason_code 映射成 03 能识别的告警码
    ALERT_MAP = {
        "FACE_MISMATCH": "FACE_MISMATCH",
        "ID_MISMATCH": "FACE_MISMATCH",
        "NOT_FOUND": "NO_INVIGILATOR",
        "LIVENESS_FAIL": "MULTI_FACE",
        "QUALITY_FAIL": "FACE_MISMATCH",
    }
    alert_code = ALERT_MAP.get(code, "FACE_MISMATCH")

    detail = "人脸识别结果：%s（%s）" % (result, rec_dict.get("reason", ""))
    candidate_id = rec_dict.get("candidate_id", "")
    candidate_name = rec_dict.get("candidate_name", "")

    def _push():
        try:
            _linkage().raise_alert(
                code=alert_code,
                device_code="",
                room_id="",
                seat_no=rec_dict.get("seat_no", ""),
                detail=detail,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                operator=operator)
        except Exception:
            traceback.print_exc()

    threading.Thread(target=_push, daemon=True).start()
