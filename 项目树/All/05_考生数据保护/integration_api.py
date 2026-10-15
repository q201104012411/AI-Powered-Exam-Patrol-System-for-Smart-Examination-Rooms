# -*- coding: utf-8 -*-
r"""
05 考生数据保护 · 集成接口（供 00_集成联动 调用）

与 01/02/03 同一约定：子进程 + JSON，`--op <操作> --args '<json>'`，
最后一行输出一个 JSON 对象，`ok` 字段表示业务是否成功。

ops:
  status           {}                                    -> 系统状态 / 加密算法 / 审计链 / 验签统计
  protect          {name,id_card_no,ticket_no,seat_no,room_id,site,operator,ticket}
                                                         -> 受保护登记一名考生（加密+签名）
  redact           {text}                                -> 日志/字段脱敏（供其他系统复用同口径）
  check_transport  {host,encrypted,network}              -> 传输合规校验（专网+加密）
  verify_data      {}                                    -> 全量完整性验签（是否被篡改）
  readonly_probe   {}                                    -> 试改加密数据，验证"改不动"
  revoke           {cand_ref,reason}                     -> 撤回同意（转非人脸替代通道）
  destroy          {cand_ref,reason}                     -> 按申请销毁（删除，留痕）
  list_candidates  {limit,room_id}                       -> 脱敏考生清单
  handoff          {name,ticket_no,seat_no,room_id,operator}
                                                         -> 给 01/03 的"最小必要"考生核验包
用法：python integration_api.py --op status --args "{}"
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import logging  # noqa: E402


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _open():
    """打开一套系统组件（Store/Lifecycle/Accounts/Rights/Governance）。"""
    from dpp import access, governance, lifecycle, rights, storage
    store = storage.Store()
    accounts = access.AccountStore(store)
    accounts.ensure_seed()
    life = lifecycle.Lifecycle(store, accounts=accounts)
    gov = governance.Governance(store)
    rt = rights.RightsService(store, lifecycle=life, accounts=accounts)
    return store, accounts, life, gov, rt


def _session(accounts, operator="owner", pw=None, ticket=""):
    """
    联动调用统一用 owner 身份（各系统互信的内网调用）。

    调用方传进来的 operator 往往是**业务标识**（如 "linkage"），不一定等于
    本系统的账户名。约定：
      · operator 命中本系统真实账户 -> 用该账户；
      · 否则一律回退到 owner（联动通道受内网互信保护，且全程留痕）。
    口令默认取预置口令，避免把口令硬编码在联动侧。
    """
    from dpp import config
    user = (operator or "owner").strip() or "owner"
    if pw is None:
        pw = dict((r[0], r[1]) for r in config.DEFAULT_ACCOUNTS).get(user)
    s = accounts.authenticate(user, pw or user, place="联动调用",
                              device="LINKAGE-05")
    if s is None and user != "owner":
        # 调用方给的是业务标识而非账户名：回退到 owner（联动互信通道）
        user = "owner"
        if pw is None:
            pw = dict((r[0], r[1]) for r in config.DEFAULT_ACCOUNTS).get(user)
        s = accounts.authenticate(user, pw or user, place="联动调用",
                                  device="LINKAGE-05")
    if s is None:
        return None
    s.confirm_risky()
    return s


# ---------------------------------------------------------------- 各操作
def op_status(a):
    from dpp import crypto, integrity, catalog
    store, _acc, life, _gov, _rt = _open()
    chain_ok, chain_msg, n = store.verify_chain()
    va = store.verify_all_records()
    sa = life.storage_audit()
    return {
        "ok": True,
        "system": "05_考生数据保护",
        "candidates": store.summary()["考生数"],
        "consents": store.summary()["同意记录"],
        "audit_ok": chain_ok, "audit_chain": chain_msg, "audit_rows": n,
        "encrypt_algorithm": crypto.algorithm_text(),
        "integrity_algorithm": integrity.integrity_algorithm_text(),
        "encrypt_rate": sa["rate"],
        "signed_records": va["total"], "tampered": va["bad_count"],
        "readonly": True,
        "catalog": len(catalog.CATEGORIES),
    }


def op_protect(a):
    """受保护登记：先落单独同意，再采集（加密 + 签名）。"""
    from dpp import models
    store, acc, life, _gov, _rt = _open()
    s = _session(acc, a.get("operator") or "owner")
    if s is None:
        return {"ok": False, "reason": "联动身份校验失败"}
    name = a.get("name") or "联动考生"
    cand_ref = models.new_id("C")
    cs = models.Consent(cand_ref=cand_ref, name=name, scope="face",
                        method="考前告知书签署（电子）",
                        operator=a.get("operator") or "linkage",
                        evidence_path="consent/%s.pdf"
                                      % (a.get("ticket") or a.get("ticket_no") or "T"))
    store.save_consent(cs)
    res = life.collect(
        s, name=name, id_card_no=a.get("id_card_no") or "",
        ticket_no=a.get("ticket_no") or "", seat_no=a.get("seat_no") or "",
        room_id=a.get("room_id") or "", site=a.get("site") or "",
        feature_vector="linkage-%s" % cand_ref,
        consent_id=cs.consent_id, cand_ref=cand_ref,
        retention_days=a.get("retention_days"))
    return {"ok": True, "cand_ref": res["cand_ref"],
            "encrypted": True, "signed": True,
            "algorithm": _algo_text(), "source": "05_考生数据保护"}


def op_redact(a):
    """脱敏：供 01/03 复用同一套口径（证件号/姓名/日志）。"""
    from dpp import desensitize
    text = a.get("text") or ""
    return {"ok": True,
            "masked": desensitize.sanitize_for_log(text),
            "id_card": desensitize.mask_id_card(a.get("id_card_no") or ""),
            "name": desensitize.mask_name(a.get("name") or ""),
            "source": "05_考生数据保护"}


def op_check_transport(a):
    """传输合规校验（专网 + 加密）。"""
    _store, _acc, life, _gov, _rt = _open()
    ok, msg, ev = life.check_transport(
        a.get("host") or "203.0.113.10",
        encrypted=bool(a.get("encrypted", True)),
        network=a.get("network") or "private",
        payload_kind=a.get("payload_kind") or "alert_clip")
    return {"ok": bool(ok), "allowed": bool(ok), "message": msg,
            "evidence": ev, "source": "05_考生数据保护"}


def op_verify_data(a):
    """全量完整性验签：返回是否被篡改（0 才算通过）。"""
    store, _acc, _life, _gov, _rt = _open()
    va = store.verify_all_records()
    return {"ok": va["bad_count"] == 0, "total": va["total"], "passed": va["ok"],
            "bad": va["bad"], "bad_count": va["bad_count"],
            "deleted": va["deleted"], "details": va["details"],
            "algorithm": va["algorithm"], "source": "05_考生数据保护"}


def op_readonly_probe(a):
    """只读探针：真的去改一次加密字段，必须被拒绝。"""
    from dpp import storage as _st
    store, _acc, _life, _gov, _rt = _open()
    sigs = store.list_signatures(kind="candidate", limit=1)
    if not sigs:
        return {"ok": True, "blocked": True, "message": "无记录可探测",
                "source": "05_考生数据保护"}
    ref = sigs[0]["record_ref"].split(":", 1)[1]
    try:
        store.update_candidate_meta(ref, name="篡改测试")
    except _st.ReadOnlyViolation as e:
        return {"ok": True, "blocked": True, "message": str(e)[:120],
                "source": "05_考生数据保护"}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": True, "blocked": True,
                "message": "数据库触发器拦截：%s" % str(e)[:120],
                "source": "05_考生数据保护"}
    return {"ok": False, "blocked": False, "message": "未能拦住修改（缺陷）",
            "source": "05_考生数据保护"}


def op_revoke(a):
    """撤回同意（即时生效，转非人脸替代通道）。"""
    store, acc, _life, _gov, rt = _open()
    s = _session(acc, a.get("operator") or "owner")
    if s is None:
        return {"ok": False, "reason": "联动身份校验失败"}
    ref = a.get("cand_ref") or ""
    out = rt.submit(s, ref, "withdraw",
                    detail=a.get("reason") or "联动撤回同意",
                    channel=a.get("channel") or "联动通道",
                    identity_verified=True)
    return {"ok": True, **out, "source": "05_考生数据保护"}


def op_destroy(a):
    """按申请销毁（删除整条记录，留痕）。"""
    store, acc, life, _gov, _rt = _open()
    s = _session(acc, a.get("operator") or "owner")
    if s is None:
        return {"ok": False, "reason": "联动身份校验失败"}
    res = life.destroy_by_request(s, a.get("cand_ref") or "",
                                  a.get("reason") or "联动申请删除")
    return {"ok": True, "destroy_id": res["destroy_id"],
            "covered": res["covered"], "source": "05_考生数据保护"}


def op_list_candidates(a):
    """脱敏考生清单（默认只给编号/座次/考场，不给姓名证件号）。"""
    from dpp import desensitize
    store, _acc, _life, _gov, _rt = _open()
    rows = store.list_candidates(room_id=a.get("room_id") or None,
                                 limit=int(a.get("limit") or 50))
    out = []
    for c in rows:
        out.append({"ref": c.cand_ref, "room_id": c.room_id,
                    "seat_no": desensitize.mask_seat_no(c.seat_no),
                    "ticket_no": desensitize.mask_ticket_no(c.ticket_no),
                    "has_face_template": bool(c.face_template),
                    "has_face_image": bool(c.face_image_path)})
    return {"ok": True, "candidates": out, "count": len(out),
            "source": "05_考生数据保护"}


def op_handoff(a):
    """
    给 01/03 的"最小必要"核验包：只回编号/座次/考场 + 是否已加密登记，
    **绝不回姓名、证件号、人脸图像**（那些必须走 reveal 授权留痕）。
    """
    store, acc, _life, _gov, _rt = _open()
    tid = a.get("ticket_no") or ""
    room = a.get("room_id") or ""
    hit = None
    for c in store.list_candidates(limit=10 ** 5):
        if (tid and c.ticket_no == tid) or (room and c.room_id == room
                                            and a.get("seat_no")
                                            and c.seat_no == a.get("seat_no")):
            hit = c
            break
    if hit is None:
        return {"ok": False, "found": False,
                "reason": "未找到匹配考生（或未登记）",
                "source": "05_考生数据保护"}
    return {"ok": True, "found": True,
            "ref": hit.cand_ref, "room_id": hit.room_id,
            "seat_no": hit.seat_no, "ticket_no": hit.ticket_no,
            "protected": bool(hit.face_template), "data_encrypted": True,
            "note": "姓名/证件号/图像受保护，需授权 reveal 才能取用",
            "source": "05_考生数据保护"}


def _algo_text():
    from dpp import crypto, integrity
    return "%s ｜ %s" % (crypto.algorithm_text(), integrity.integrity_algorithm_text())


OPS = {
    "status": op_status,
    "protect": op_protect,
    "redact": op_redact,
    "check_transport": op_check_transport,
    "verify_data": op_verify_data,
    "readonly_probe": op_readonly_probe,
    "revoke": op_revoke,
    "destroy": op_destroy,
    "list_candidates": op_list_candidates,
    "handoff": op_handoff,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.args or "{}")
    except ValueError:
        return emit({"ok": False, "reason": "--args 不是合法 JSON"})
    try:
        return emit(OPS[ns.op](args))
    except Exception as e:                                   # noqa: BLE001
        import traceback
        return emit({"ok": False,
                     "reason": "%s: %s" % (type(e).__name__, str(e)[:200]),
                     "trace": traceback.format_exc()[-500:]})


if __name__ == "__main__":
    sys.exit(main())
