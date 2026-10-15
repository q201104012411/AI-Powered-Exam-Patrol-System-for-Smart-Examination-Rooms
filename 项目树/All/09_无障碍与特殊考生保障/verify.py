# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 替代核验与受阻事件

**替代核验**：人脸核验失败（面部损伤/术后/遮挡）、设备故障、画质不可信、无单独同意时，
人工核验必须能顶上，且**结论与智能核验同等效力**（第二节第 4 条）。

代码里落实的三条硬规则：
  1) 双人见证：见证人不得与主检人相同（同一个人"自己见证自己"不算见证）；
  2) 双证比对：至少 2 项证件"一致"，且必须包含身份证/准考证/护照之一；
     双证比对不通过 -> 结论是"转考务处置"，**不是**"设备拒绝入场"（措辞与留痕都要分清）；
  3) 任何一次因技术原因启用人工核验，都要登记一条**受阻事件**并尽量闭环到"已入场"，
     这样"因技术原因未能入场 0 起"这个指标才有真实的分子分母。

对外通知（推 03 智能终端）通过注入的 notifier 完成，本模块不直接依赖联动层，离线可测。
"""
import config
import models


def docs_check(docs):
    """
    双证比对判定。docs 形如 {"id_card": "matched", "ticket_no": "matched"}。
    返回 (ok, matched_count, missing_required, detail_text)。
    """
    docs = dict(docs or {})
    matched = [k for k, v in docs.items() if v == config.DOC_MATCHED]
    must_have = [k for k in matched if k in config.MUST_HAVE_DOC]
    ok = (len(matched) >= config.MIN_DOC_MATCHED) and bool(must_have)
    detail = "一致 %d 项（%s）" % (
        len(matched), "、".join(config.DOC_TEXT.get(k, k) for k in matched) or "无")
    if not must_have:
        detail += "；缺少身份证/准考证/护照原件核对"
    return ok, len(matched), (not must_have), detail


def _notify(notifier, code, info):
    """通知失败不能影响本地留痕：异常吞掉并返回错误说明。"""
    if notifier is None:
        return {"ok": False, "reason": "未接联动（离线模式）"}
    try:
        return notifier(code, info) or {"ok": False, "reason": "无返回"}
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def alt_verify(store, payload, operator="", notifier=None, create_block=None):
    """
    执行一次替代核验并留痕。
    返回 (ok, alt_verify_obj|reason_text, info_dict)。
    """
    payload = dict(payload or {})
    cfg = config.load_settings()
    cid = str(payload.get("candidate_id") or "").strip()
    if not cid:
        return False, "缺少考生标识（身份证/准考证号）", {}
    mode = str(payload.get("mode") or config.ALT_MANUAL_DOC)
    if mode not in config.ALT_MODES:
        return False, "替代核验方式非法：%s" % mode

    # 档案：没有档案也必须能核验（**不得因为没登记就拒绝考生入场**），当场补登最小档案
    pf = store.get_profile(candidate_id=cid)
    info = {"profile_created": False}
    if pf is None:
        needs = [n for n in (payload.get("needs") or []) if n in config.NEED_KINDS] \
            or [config.NEED_FACE]
        ok_pf, pf, _x = access_add_profile(store, {
            "candidate_id": cid,
            "candidate_name": payload.get("candidate_name") or "",
            "room_id": payload.get("room_id") or "",
            "seat_no": payload.get("seat_no") or "",
            "site": payload.get("site") or cfg.get("site") or "",
            "needs": needs,
            "consent": bool(payload.get("consent")),
            "need_detail": "替代核验时现场补登档案（原因为技术原因或未提前申报）",
            "source": payload.get("source") or config.MANUAL_SOURCE,
        }, operator)
        if not ok_pf:
            return False, "补登档案失败：%s" % pf, {}
        info["profile_created"] = True

    # 前置条件：替代通道必须可用（不可用则先报通道问题，再照常核验——人优先）
    alt_ok, alt_why = access_alt_channel_available(store, pf.site or cfg.get("site"))
    info["alt_channel_ok"] = alt_ok
    info["alt_channel_reason"] = alt_why
    chan_warn = [] if alt_ok else [alt_why]

    docs = {k: v for k, v in (payload.get("docs") or {}).items()
            if k in config.DOC_KINDS and v in config.DOC_STATES}
    doc_ok, matched, missing_required, doc_text = docs_check(docs)

    inspector = str(payload.get("inspector") or operator or "").strip()
    witness = str(payload.get("witness") or "").strip()
    witness_required = bool(cfg.get("witness_required", True))
    if not inspector:
        return False, "必须填主检人（谁核验谁负责）", {}
    if witness_required:
        if not witness:
            return False, "双人见证未满足：必须填见证人", {}
        if witness == inspector:
            return False, "双人见证未满足：见证人不得与主检人相同", {}

    outcome = str(payload.get("outcome") or "")
    if outcome not in config.ALT_OUTCOMES:
        outcome = config.ALT_PASSED if doc_ok else config.ALT_FAILED
    if outcome == config.ALT_PASSED and not doc_ok:
        return False, "结论自相矛盾：双证比对未通过，不能判「通过」（应转考务）", {}
    if outcome == config.ALT_FAILED and doc_ok:
        # 允许（例如发现冒名顶替线索），但要留下理由
        if not str(payload.get("reason") or "").strip():
            return False, "双证一致却判不通过，必须写明理由", {}

    smart = dict(payload.get("smart_result") or {})
    cred = payload.get("camera_credibility")
    try:
        cred = float(cred) if cred is not None else 1.0
    except (TypeError, ValueError):
        cred = 1.0
    tech_reason = bool(payload.get("tech_reason"))
    if smart and smart.get("ok") is False:
        tech_reason = True                       # 智能核验不可用，本身就是技术原因
    sla = int(payload.get("sla_sec") or config.sla_sec(cfg))
    elapsed = float(payload.get("elapsed_sec") or 0.0)
    overdue = bool(elapsed and elapsed > sla)

    av = models.AltVerify(
        candidate_id=cid, candidate_name=payload.get("candidate_name")
        or (pf.candidate_name if pf else ""),
        ticket_no=str(payload.get("ticket_no") or (pf.ticket_no if pf else "")),
        room_id=str(payload.get("room_id") or (pf.room_id if pf else "")),
        seat_no=str(payload.get("seat_no") or (pf.seat_no if pf else "")),
        site=pf.site or cfg.get("site") or "",
        mode=mode, docs=docs, doc_matched=matched, inspector=inspector,
        witness=witness, needs=list(pf.needs or []),
        reason=str(payload.get("reason") or _default_reason(mode, smart, cred)),
        tech_reason=tech_reason, device_code=str(payload.get("device_code") or ""),
        smart_result=smart, camera_credibility=cred, outcome=outcome,
        equivalence=True, sla_sec=sla, elapsed_sec=elapsed, overdue=overdue,
        drill=bool(payload.get("drill")), operator=operator or inspector,
        source=str(payload.get("source") or config.MANUAL_SOURCE),
        note=str(payload.get("note") or ""))
    store.save_alt_verify(av)

    # 因技术原因启用人工核验 -> 登记受阻事件（并尽量闭环）
    block = None
    if create_block is None:
        create_block = bool(tech_reason)
    if create_block:
        block = raise_tech_block(store, {
            "candidate_id": cid,
            "candidate_name": av.candidate_name,
            "room_id": av.room_id, "seat_no": av.seat_no, "site": av.site,
            "symptom": payload.get("symptom") or "face_fail",
            "device_code": av.device_code,
            "channel_code": payload.get("channel_code") or "",
            "detail": "替代核验（%s）：%s" % (config.ALT_MODE_TEXT.get(mode, mode),
                                            av.reason),
            "source": av.source,
        }, operator, notifier=notifier)
        if block is not None and outcome in (config.ALT_PASSED, config.ALT_DEFERRED):
            block = resolve_tech_block(
                store, block.block_id,
                config.FINAL_ENTERED_MANUAL if outcome == config.ALT_PASSED
                else config.FINAL_ENTERED_MANUAL,
                "%s（%s），已准予入场" % (config.ALT_MODE_TEXT.get(mode, mode), doc_text),
                operator, notifier=None) or block

    if payload.get("notify"):
        r = _notify(notifier, "ACCESS_ALT_VERIFY", {
            "detail": "替代核验：%s｜%s｜%s｜主检 %s / 见证 %s｜%s"
                      % (config.ALT_MODE_TEXT.get(mode, mode), config.mask_id(cid),
                         doc_text, inspector, witness or "-", av.reason),
            "candidate_id": cid, "candidate_name": av.candidate_name,
            "room_id": av.room_id, "seat_no": av.seat_no,
            "device_code": av.device_code, "level": config.LEVEL_ORANGE,
            "operator": operator or inspector})
        av.alert_id = (r or {}).get("alert_id") or ""
        if av.alert_id:
            store.save_alt_verify(av)

    store.log("替代核验", "%s %s 结论=%s 双证=%s"
              % (config.mask_id(cid), config.ALT_MODE_TEXT.get(mode, mode),
                 config.ALT_OUTCOME_TEXT.get(outcome, outcome), doc_text),
              operator or inspector)
    info.update({"docs_text": doc_text, "doc_ok": doc_ok, "matched": matched,
                 "missing_required": missing_required, "warn": chan_warn,
                 "equivalence": config.EQUIVALENCE_NOTE,
                 "block": (block.to_dict() if block is not None else None),
                 "overdue": overdue})
    return True, av, info


def _default_reason(mode, smart, cred):
    if smart and smart.get("result") in ("fail", "mismatch"):
        return "智能核验不通过（%s），按制度转人工核验" % smart.get("reason_code", "")
    if smart and smart.get("ok") is False:
        return "智能核验不可用（%s），转人工核验" % str(smart.get("reason"))[:80]
    if cred < 0.8:
        return "机位画质可信度 ×%.1f，核验结论不可信，转人工核验" % cred
    if mode == config.ALT_EXEMPT:
        return "考前已备案免核验"
    return "考生申请替代核验"


def raise_tech_block(store, payload, operator="", notifier=None):
    """登记一条"因技术原因入场受阻"事件，并按设置上报 03 智能终端（红色）。"""
    payload = dict(payload or {})
    cid = str(payload.get("candidate_id") or "").strip()
    if not cid:
        return None
    cfg = config.load_settings()
    symptom = str(payload.get("symptom") or "other")
    if symptom not in config.BLOCK_SYMPTOMS:
        symptom = "other"
    b = models.TechBlock(
        candidate_id=cid, candidate_name=str(payload.get("candidate_name") or ""),
        room_id=str(payload.get("room_id") or ""), seat_no=str(payload.get("seat_no") or ""),
        site=str(payload.get("site") or cfg.get("site") or ""), symptom=symptom,
        device_code=str(payload.get("device_code") or ""),
        detail=str(payload.get("detail") or config.BLOCK_SYMPTOMS[symptom]),
        channel_code=str(payload.get("channel_code") or ""),
        operator=operator or str(payload.get("operator") or ""),
        source=str(payload.get("source") or config.MANUAL_SOURCE),
        note=str(payload.get("note") or ""))
    store.save_tech_block(b)

    alert_id = ""
    if cfg.get("report_to_terminal", True) and payload.get("notify", True):
        r = _notify(notifier, "ACCESS_TECH_BLOCK", {
            "detail": "因技术原因入场受阻：%s｜%s｜考场 %s 座位 %s｜设备 %s｜%s"
                      "｜要求：立即人工介入，不得因此拒绝考生入场"
                      % (config.BLOCK_SYMPTOMS[symptom], config.mask_id(cid),
                         b.room_id or "-", b.seat_no or "-", b.device_code or "-",
                         b.detail),
            "candidate_id": cid, "candidate_name": b.candidate_name,
            "room_id": b.room_id, "seat_no": b.seat_no,
            "device_code": b.device_code, "level": config.LEVEL_RED,
            "operator": operator})
        alert_id = (r or {}).get("alert_id") or ""
        b.escalated = bool(alert_id)
        b.alert_id = alert_id
        store.save_tech_block(b)
    store.log("技术原因受阻", "%s %s" % (config.mask_id(cid),
                                    config.BLOCK_SYMPTOMS[symptom]), operator)
    return b


def resolve_tech_block(store, block_id, final_result, resolution, operator="",
                       notifier=None):
    """结案一条受阻事件；final_result=denied 即计入"因技术原因未能入场"（指标要求 0）。"""
    b = store.get_tech_block(block_id)
    if b is None:
        return None
    if final_result not in config.FINAL_RESULTS:
        return None
    b.resolved = True
    b.final_result = final_result
    b.resolution = str(resolution or "")
    b.resolved_at = models.now_ts()
    b.resolved_epoch = models.now_epoch()
    b.waited_sec = round(max(0.0, b.resolved_epoch - b.raised_epoch), 1)
    if operator:
        b.operator = operator
    store.save_tech_block(b)
    store.log("受阻事件结案", "%s -> %s（等待 %.0fs）"
              % (config.mask_id(b.candidate_id), config.FINAL_TEXT.get(final_result,
                                                                      final_result),
                 b.waited_sec), operator)
    if final_result == config.FINAL_DENIED and notifier is not None:
        # 真出事了：必须留一条审计 + 上报，供事后追责整改
        _notify(notifier, "ACCESS_TECH_BLOCK", {
            "detail": "**因技术原因未能入场**：%s｜%s｜%s"
                      % (config.mask_id(b.candidate_id),
                         config.BLOCK_SYMPTOMS.get(b.symptom, b.symptom),
                         b.resolution or "未说明处置"),
            "candidate_id": b.candidate_id, "room_id": b.room_id,
            "seat_no": b.seat_no, "device_code": b.device_code,
            "level": config.LEVEL_RED, "operator": operator})
    return b


# ---------------------------------------------------------------------------
# 依赖注入：避免 verify.py <-> access.py 循环 import（access 不依赖 verify）
def access_add_profile(store, payload, operator=""):
    import access
    return access.add_profile(store, payload, operator)


def access_alt_channel_available(store, site):
    import access
    return access.alt_channel_available(store, site)
