# -*- coding: utf-8 -*-
"""
01 人脸识别与身份核对 · 集成接口（供 00_集成联动 调用）

为什么单独一个脚本：01/02/03 都有同名的 config.py / models.py，
同进程 import 必然撞名。所以各套各留一个 CLI 接口，联动层用**子进程 + JSON** 串起来。

用法：
  python integration_api.py --op verify --args "{\"image\":\"...\",\"candidate\":\"C001\",\"mode\":\"1:1\"}"
输出：最后一行一个 JSON。
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def op_verify(a):
    import config
    import liveness
    from audit import AuditLogger
    from face_engine import imread_unicode
    from face_index import FaceIndex
    from store import CandidateStore
    from verifier import FaceVerifier

    image = a.get("image") or ""
    img = imread_unicode(str(config.resolve_photo(image)))
    if img is None:
        return {"ok": False, "reason": "图片无法解码：%s" % image}
    cands = CandidateStore().load()
    idx = FaceIndex()
    try:
        idx.load()
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "reason": "特征索引不可用（需先建档）：%s" % str(e)[:120]}
    device = a.get("device") or "LINKAGE"
    v = FaceVerifier(cands, idx, device_id=device, logger=AuditLogger(),
                     operator=a.get("operator") or "linkage")
    token = liveness.make_token(device, source="file")
    mode = a.get("mode") or "1:1"
    if mode == "1:1":
        cid = a.get("candidate") or ""
        if cid not in cands:
            return {"ok": False, "reason": "考生不存在：%s（现有：%s）"
                                          % (cid, "、".join(sorted(cands)))}
        # 身份要素（可选）：联动层传了就按当前模式校验
        #   大考模式 -> id_card 必须是证件原件明文，缺失即 ID_MISMATCH
        #   常规模式 -> 准考证号必填；id_card 可留空（身份证不参与校验）
        rec = v.verify_face_first(cid, live_images=[img], capture_token=token,
                                  id_card=a.get("id_card"), ticket_no=a.get("ticket_no"),
                                  seat_no=a.get("seat_no"))
    else:
        rec = v.verify_1toN(img, capture_token=token)
    cand = cands.get(rec.candidate_id)
    return {"ok": True, "result": rec.result, "reason_code": rec.reason_code,
            "reason": rec.reason, "score": rec.score, "threshold": rec.threshold,
            "candidate_id": rec.candidate_id or "",
            "candidate_name": (cand.name if cand else ""),
            "doc_info": getattr(rec, "id_card_display", "") or "",
            "quality_severity": getattr(rec, "quality_severity", "") or "",
            "source": "01_人脸识别与身份核对"}


def op_status(a):
    import config
    from store import CandidateStore
    try:
        idx_ok = True
        from face_index import FaceIndex
        fi = FaceIndex()
        fi.load()
    except Exception:                            # noqa: BLE001
        idx_ok = False
    cands = CandidateStore().load()
    return {"ok": True, "system": "01_人脸识别与身份核对",
            "candidates": sorted(cands), "index_ready": idx_ok,
            "data_dir": str(config.DATA_ROOT)}


OPS = {"verify": op_verify, "status": op_status}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.args or "{}")
    except ValueError:
        return emit({"ok": False, "reason": "--args 不是合法 JSON"})
    if not isinstance(args, dict):
        return emit({"ok": False, "reason": "--args 必须是 JSON 对象"})
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                       # noqa: BLE001
        import traceback
        return emit({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
