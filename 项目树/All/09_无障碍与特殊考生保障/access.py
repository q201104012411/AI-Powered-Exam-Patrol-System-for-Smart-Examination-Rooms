# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 通道与档案（业务核心之一）

管两件事：
  1) **通道**：三通道并行（智能 / 人工 / 无障碍）是否按考点配齐，硬性尺寸是否达标
     （净宽 ≥900mm、回转 ≥1500mm、低位终端高度可调、语音+文字双提示）；
  2) **特殊考生档案**：需求 -> 自动推荐保障措施 -> 逐项发放留痕。

两条不可让步的规则（写进代码而不是只写进文档）：
  · 未取得**单独同意**的考生：不采集人脸，直接走人工通道，**绝不允许因为设备把人挡在外面**；
  · 措施"已落实"必须有责任人与时间，报告里的数字才站得住。
"""
import config
import models

# 通道校验的问题级别
LV_ERROR = "error"      # 硬性要求不满足（合规评审会直接问）
LV_WARN = "warn"        # 应该改进


def _issue(code, level, text):
    return {"code": code, "level": level, "text": text}


def channel_issues(ch):
    """单条通道的合规校验；返回问题列表（error 必须整改，warn 建议整改）。"""
    out = []
    if ch.kind not in config.CHANNEL_KINDS:
        out.append(_issue("KIND", LV_ERROR, "通道类型非法：%s" % ch.kind))
    if not str(ch.site or "").strip():
        out.append(_issue("SITE", LV_ERROR, "未填考点"))
    # 无障碍专用通道：尺寸与提示方式
    if ch.kind == config.CH_ACCESSIBLE:
        if int(ch.width_mm or 0) < config.MIN_AISLE_WIDTH_MM:
            out.append(_issue("WIDTH", LV_ERROR,
                              "净宽 %dmm < %dmm（轮椅无法通行）"
                              % (int(ch.width_mm or 0), config.MIN_AISLE_WIDTH_MM)))
        if int(ch.turn_radius_mm or 0) < config.MIN_TURN_RADIUS_MM:
            out.append(_issue("TURN", LV_ERROR,
                              "回转空间 %dmm < %dmm（轮椅掉头困难）"
                              % (int(ch.turn_radius_mm or 0),
                                 config.MIN_TURN_RADIUS_MM)))
        if not ch.low_counter:
            out.append(_issue("LOW_COUNTER", LV_ERROR,
                              "缺低位核验终端（轮椅考生够不到高位台面）"))
        else:
            h = int(ch.counter_height_mm or 0)
            if h and h > config.LOW_COUNTER_MAX_MM:
                out.append(_issue("COUNTER_H", LV_ERROR,
                                  "低位终端台面 %dmm > %dmm，仍属高位"
                                  % (h, config.LOW_COUNTER_MAX_MM)))
            if not ch.height_adjustable:
                out.append(_issue("COUNTER_ADJ", LV_WARN, "终端高度不可调（建议可调）"))
    # 语音 + 文字双提示（第二节第 3 条）
    if not ch.text_prompt:
        out.append(_issue("TEXT", LV_ERROR, "缺文字/大字提示（听力障碍考生必需）"))
    if not ch.voice_prompt:
        out.append(_issue("VOICE", LV_WARN, "缺语音提示（视力障碍考生建议配语音引导）"))
    if not (ch.voice_prompt or ch.text_prompt or ch.vibration):
        out.append(_issue("NO_PROMPT", LV_ERROR, "三种提示方式一个都没有"))
    # 状态与提示方式的交叉校验：降级可用必须有替代提示
    if ch.status == config.ST_DEGRADED and not (
            sum(1 for x in (ch.voice_prompt, ch.text_prompt, ch.vibration) if x) >= 2):
        out.append(_issue("DEGRADED", LV_ERROR,
                          "标记为降级可用，但替代提示方式不足 2 种"))
    if ch.status in config.CHANNEL_USABLE and not ch.devices:
        out.append(_issue("DEVICES", LV_WARN, "通道内未登记任何设备编码"))
    if ch.status == config.ST_OK and not ch.last_check_epoch:
        out.append(_issue("CHECK", LV_WARN, "从未做过现场检查（建议记录检查时间）"))
    return out


def validate_channel(ch):
    """校验并返回结论；errors 非空即视为不合规。"""
    issues = channel_issues(ch)
    errors = [i for i in issues if i["level"] == LV_ERROR]
    return {"ok": not errors, "issues": issues, "errors": errors,
            "warns": [i for i in issues if i["level"] == LV_WARN],
            "channel_code": ch.code, "kind": ch.kind,
            "kind_text": config.CHANNEL_TEXT.get(ch.kind, ch.kind),
            "state": ch.status}


def site_status(store, site):
    """
    单考点三通道并行情况：
      · have         ：三类通道各自条数
      · usable       ：三类各自"状态可用（ok/degraded）"的条数
      · usable_valid ：三类各自**状态可用且校验无 error** 的条数（指标用这个，更严）
      · ok           ：三类都有可用通道，且所有通道校验无 error
      · alt_available：替代核验通道（人工/无障碍）里有**合格且可用**的一条
      · channel_errors：汇总问题文本（口径可追溯到具体通道）
    """
    chans = store.list_channels(site=site)
    have, usable, usable_valid, bad = {}, {}, {}, []
    for ch in chans:
        have[ch.kind] = have.get(ch.kind, 0) + 1
        v = validate_channel(ch)
        if ch.usable():
            usable[ch.kind] = usable.get(ch.kind, 0) + 1
            if v["ok"]:
                usable_valid[ch.kind] = usable_valid.get(ch.kind, 0) + 1
        if not v["ok"]:
            bad.extend(["%s：%s" % (ch.code, i["text"]) for i in v["errors"]])
    missing = [k for k in config.CHANNEL_KINDS if not usable_valid.get(k)]
    return {"site": site, "total": len(chans), "have": have, "usable": usable,
            "usable_valid": usable_valid,
            "ok": not missing and not bad,
            "missing": missing,
            "missing_text": [config.CHANNEL_TEXT.get(k, k) for k in missing],
            "channel_errors": bad,
            "alt_available": bool(usable_valid.get(config.CH_MANUAL)
                                  or usable_valid.get(config.CH_ACCESSIBLE))}


def channel_config_rate(store, cfg=None):
    """
    指标一：无障碍通道配置率（要求 100% 考点）。
    口径：考点在 settings.sites 里登记，且该考点"智能+人工+无障碍"三类都有可用通道、
    无障碍通道校验无 error，才算"已配置"。
    """
    cfg = cfg or config.load_settings()
    sites = config.sites(cfg)
    details, ok_sites = [], []
    for s in sites:
        st = site_status(store, s)
        details.append(st)
        if st["ok"]:
            ok_sites.append(s)
    rate = (len(ok_sites) / float(len(sites))) if sites else 0.0
    return {"sites_total": len(sites), "sites_ok": len(ok_sites),
            "rate": round(rate, 4), "ok": rate >= config.TARGET_CHANNEL_RATE,
            "ok_sites": ok_sites, "details": details,
            "target": "100% 考点配齐三通道（含 1 条轮椅可通行的无障碍通道）"}


def alt_channel_available(store, site):
    """
    替代核验通道是否可用（替代核验前的硬性前置条件）。
    口径与指标一致：人工/无障碍通道里必须有一条**状态可用且校验无 error**。
    "能开门"不等于"能顶用"—— 降级可用但只剩一种提示方式的通道不算合格。
    """
    cands = [c for c in store.list_channels(site=site)
             if c.kind in (config.CH_MANUAL, config.CH_ACCESSIBLE)]
    good = [c for c in cands if c.usable() and validate_channel(c)["ok"]]
    if good:
        return True, "可用替代通道：%s" % "、".join(
            "%s(%s)" % (c.code, c.kind_text()) for c in good)
    if not cands:
        return False, "考点「%s」没有替代核验通道（人工/无障碍），必须先设置通道" % site
    problems = []
    for c in cands:
        if not c.usable():
            problems.append("%s 状态=%s" % (c.code, c.state_text()))
            continue
        v = validate_channel(c)
        problems.append("%s 校验不合格：%s" % (c.code,
                                          "；".join(i["text"] for i in v["errors"])))
    return False, ("考点「%s」的替代核验通道都不合格，须先整改：%s"
                   % (site, " ｜ ".join(problems)))


def recommend_measures(needs, cfg=None):
    """按需求类型推荐保障措施（去重保序）。"""
    cfg = cfg or config.load_settings()
    out = []
    for n in (needs or []):
        for m in config.NEED_MEASURES.get(n, ()):  # 需求类型 -> 措施
            if m not in out:
                out.append(m)
    # 双人见证是制度要求：只要走人工/无障碍通道就要有"人工核验替代"
    if any(config.CHANNEL_REQUIRED_FOR.get(n) in (config.CH_MANUAL, config.CH_ACCESSIBLE)
           for n in (needs or [])) and config.M_MANUAL_VERIFY not in out:
        out.append(config.M_MANUAL_VERIFY)
    return out


def channel_hint(needs):
    """建议走哪条通道：有明确映射取映射，否则智能通道即可。"""
    for n in (needs or []):
        k = config.CHANNEL_REQUIRED_FOR.get(n)
        if k == config.CH_ACCESSIBLE:
            return config.CH_ACCESSIBLE
    for n in (needs or []):
        k = config.CHANNEL_REQUIRED_FOR.get(n)
        if k == config.CH_MANUAL:
            return config.CH_MANUAL
    return config.CH_SMART


def add_profile(store, payload, operator=""):
    """
    登记特殊考生无障碍档案（幂等：同考生已有 active 档案则返回 already=true）。
    返回 (ok, profile|reason_text, extra)。
    """
    payload = dict(payload or {})
    cfg = config.load_settings()
    cid = str(payload.get("candidate_id") or "").strip()
    if not cid:
        return False, "缺少考生标识（身份证/准考证号）", {}
    needs = [n for n in (payload.get("needs") or []) if n in config.NEED_KINDS]
    if not needs:
        return False, "至少选择 1 项特殊需求（否则无法配置替代通道）", {}
    if config.NEED_OTHER in needs and not str(payload.get("need_detail") or "").strip():
        return False, "选择「其他」必须写明具体需求", {}

    exist = store.get_profile(candidate_id=cid)
    if exist is not None and (exist.status or "active") == "active" \
            and not payload.get("allow_update"):
        return True, "该考生已有生效档案（%s）" % exist.profile_id, {"already": True,
                                                                   "profile": exist}

    # 单独同意是"敏感个人信息处理"的开关：更新档案时若没重新声明，沿用原状态
    # （不能因为一次"改座位"的更新把已取得的同意悄悄抹掉）
    consent = payload.get("consent")
    if consent is None and exist is not None:
        consent = exist.consent
    consent = bool(consent)
    measures = [m for m in (payload.get("measures") or []) if m in config.MEASURE_KINDS]
    for m in recommend_measures(needs, cfg):
        if m not in measures:
            measures.append(m)
    hint = channel_hint(needs)
    warn = []
    if not consent:
        # 未取得单独同意：不采集人脸，只走人工通道（绝不拒绝入场）
        hint = config.CH_MANUAL
        for m in (config.M_MANUAL_VERIFY, config.M_EXEMPT_FACE):
            if m not in measures:
                measures.append(m)
        warn.append("未取得单独同意：不采集人脸信息，安排人工核验通道"
                    "（%s）" % config.EQUIVALENCE_NOTE)
    else:
        if hint == config.CH_ACCESSIBLE:
            warn.append("建议走无障碍专用通道（轮椅/低位终端）")
    if not payload.get("cert_verified") and (config.NEED_MEDICAL in needs):
        warn.append("医疗/植入类需求建议核验证明后放行（凭证明走人工安检）")

    site = str(payload.get("site") or cfg.get("site") or "").strip()
    store.ensure_site(site)
    # 同意范围/时间：新声明优先，其次沿用原档案，最后给缺省说明
    if payload.get("consent_scope"):
        scope = str(payload["consent_scope"])
    elif consent and exist is not None and exist.consent:
        scope = exist.consent_scope
    else:
        scope = "人脸核验与考试安全管理" if consent else ""
    if payload.get("consent_at"):
        consent_at = str(payload["consent_at"])
    elif consent and exist is not None and exist.consent and exist.consent_at:
        consent_at = exist.consent_at
    else:
        consent_at = models.now_ts() if consent else ""
    p = models.Profile(
        candidate_id=cid,
        candidate_name=str(payload.get("candidate_name") or "").strip(),
        ticket_no=str(payload.get("ticket_no") or "").strip(),
        room_id=str(payload.get("room_id") or "").strip(),
        seat_no=str(payload.get("seat_no") or "").strip(),
        site=site, needs=needs,
        need_detail=str(payload.get("need_detail") or "").strip(),
        measures=measures, channel_hint=hint,
        cert_no=str(payload.get("cert_no") or "").strip(),
        cert_verified=bool(payload.get("cert_verified")),
        contact=str(payload.get("contact") or "").strip(),
        consent=consent,
        consent_scope=scope,
        consent_at=consent_at,
        valid_from=str(payload.get("valid_from") or ""),
        valid_to=str(payload.get("valid_to") or ""),
        source=str(payload.get("source") or config.MANUAL_SOURCE),
        operator=operator, note=str(payload.get("note") or ""))
    if exist is not None:
        p.profile_id = exist.profile_id
    store.save_profile(p)

    # 逐项措施落库（"已落实"与"已落实"的规矩一致：没有责任人就只能先记"已申请"）
    provided = [m for m in (payload.get("provided") or []) if m in measures]
    if provided and not str(payload.get("owner") or "").strip():
        warn.append("声明「已落实」的措施必须有责任人，已先记为「已申请」")
        provided = []
    for m in measures:
        status = config.ACC_PROVIDED if m in provided else config.ACC_REQUESTED
        acc = models.Accommodation(
            profile_id=p.profile_id, measure=m, status=status,
            owner=str(payload.get("owner") or ""),
            place=str(payload.get("place") or ""),
            provided_at=models.now_ts() if status == config.ACC_PROVIDED else "",
            provided_epoch=models.now_epoch() if status == config.ACC_PROVIDED else 0.0,
            operator=operator,
            note="登记时自动生成" if status == config.ACC_REQUESTED else "登记时已落实")
        old = store.get_accommodation(p.profile_id, m)
        if old is not None:                      # 保留已有措施的落实状态
            acc.acc_id = old.acc_id
            acc.status = old.status
            acc.owner = old.owner
            acc.place = old.place
            acc.provided_at = old.provided_at
            acc.provided_epoch = old.provided_epoch
        store.save_accommodation(acc)

    store.log("登记特殊考生档案",
              "%s 需求=%s 措施=%d 项 通道=%s" % (config.mask_id(cid),
                                                config.need_text(needs),
                                                len(measures), hint), operator)
    return True, p, {"already": False, "warn": warn,
                     "recommended": recommend_measures(needs, cfg)}


def set_measure_status(store, profile_id, measure, status, operator="", owner="",
                       place="", note=""):
    """更新一项保障措施的落实状态（"已落实"要记责任人与时间；"不予"要写理由）。"""
    if measure not in config.MEASURE_KINDS:
        return False, "措施类型非法：%s" % measure
    if status not in config.ACC_STATES:
        return False, "状态非法：%s" % status
    acc = store.get_accommodation(profile_id, measure)
    if acc is None:
        pf = store.get_profile(profile_id=profile_id)
        if pf is None:
            return False, "档案不存在：%s" % profile_id
        acc = models.Accommodation(profile_id=profile_id, measure=measure,
                                   candidate_key=pf.candidate_key)
    if status == config.ACC_REJECTED and not str(note or "").strip():
        return False, "「不予」必须写明理由（合规评审会问）"
    if status == config.ACC_PROVIDED and not str(owner or acc.owner or "").strip():
        return False, "「已落实」必须填责任人/岗位（否则报告里的落实率站不住）"
    acc.status = status
    acc.operator = operator or acc.operator
    acc.owner = owner or acc.owner
    acc.place = place or acc.place
    acc.note = note or acc.note
    if status == config.ACC_PROVIDED:
        acc.provided_at = models.now_ts()
        acc.provided_epoch = models.now_epoch()
    store.save_accommodation(acc)
    store.log("保障措施状态", "%s -> %s（档案 %s）" % (measure, status, profile_id),
              operator)
    return True, acc


def measure_coverage(store, site=None, limit=10000):
    """
    措施落实率（报告用）：需求为"必须"的措施中，有多少已落实。
    口径：档案推荐措施里状态为 provided 的比例（rejected 计为未落实并在报告里单列）。
    """
    profiles = store.list_profiles(limit=limit, site=site, status="active")
    total = provided = rejected = pending = 0
    for pf in profiles:
        for acc in store.list_accommodations(profile_id=pf.profile_id):
            total += 1
            if acc.status == config.ACC_PROVIDED:
                provided += 1
            elif acc.status == config.ACC_REJECTED:
                rejected += 1
            else:
                pending += 1
    rate = (provided / float(total)) if total else 0.0
    return {"profiles": len(profiles), "measures": total, "provided": provided,
            "pending": pending, "rejected": rejected, "rate": round(rate, 4)}
