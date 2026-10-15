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


# =====================================================================
#  账户管理（供 03 智能终端远程管理本机登录账号）
#  —— 安全前提：本接口本身不做鉴权，**调用方（03）必须在自己的权限模型里
#     先判定并留痕**；这里再做一次越级防护（by_role），两道闸。
#     by_role 是"调用方以什么角色发起"，取值范围与 accounts.ROLE_LEVEL 一致。
# =====================================================================
_ACCT_OPS_NOTE = ("账户管理属敏感操作：03 侧需 owner 权限 + 写审计链后才可调用本接口；"
                  "本接口只做越级防护，不替代 03 的权限判定。")


def op_account_list(a):
    """列出本机账户（不含口令哈希）"""
    import accounts
    return {"ok": True, "accounts": accounts.list_users(),
            "roles": accounts.ROLE_TEXT, "note": _ACCT_OPS_NOTE,
            "source": "01_人脸识别与身份核对"}


def op_account_create(a):
    """新建账户。args: username, password, role(admin/teacher), display_name, by_role"""
    import accounts
    ok, msg = accounts.create_user(
        a.get("username") or "", a.get("display_name") or a.get("username") or "",
        a.get("password") or "", role=a.get("role") or accounts.ROLE_TEACHER,
        by_role=a.get("by_role") or accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "01_人脸识别与身份核对"}


def op_account_reset(a):
    """重置口令（被重置账户下次登录强制改密——这是 accounts 既有行为）"""
    import accounts
    ok, msg = accounts.reset_user_password(
        a.get("username") or "", a.get("password") or "",
        by_role=a.get("by_role") or accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "01_人脸识别与身份核对"}


def op_account_delete(a):
    """删除账户（同样受"只能删等级低于自己"与"至少保留一名管理员"约束）"""
    import accounts
    ok, msg = accounts.delete_user(a.get("username") or "",
                                   by_role=a.get("by_role") or accounts.ROLE_OWNER)
    return {"ok": bool(ok), "reason": "" if ok else msg, "message": msg,
            "source": "01_人脸识别与身份核对"}


OPS = {"verify": op_verify, "status": op_status,
       "account_list": op_account_list, "account_create": op_account_create,
       "account_reset": op_account_reset, "account_delete": op_account_delete}


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
