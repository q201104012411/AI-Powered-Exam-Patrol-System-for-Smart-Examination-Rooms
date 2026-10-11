# -*- coding: utf-8 -*-
"""
核验器：身份要素（大考模式四要素 / 常规模式三要素）+ 人脸比对 + 现场性校验
        + 硬失败拦截 + 分级复验 + 审计留痕。

判定分级（这是"异常状态处理"的核心）：
    PASS    —— 相似度达标，且明显高于"最像的另一人"（排除撞脸），画质与姿态可接受
    MANUAL  —— 模糊匹配灰区 / 疑似孪生撞脸 / 画质严重不足 / 极端姿态 → 人工复验
    FAIL    —— 证件不符、无脸、多脸、非现场抓拍、画质正常但相似度显著偏低

修正的旧版缺陷：
1. 相似度口径：直接使用**原始余弦**，阈值 1:1=0.55 / 1:N=0.65（旧版映射后套 0.65，等效 0.30）；
2. 硬失败（证件不符、无脸、多脸、无现场令牌…）返回 FAIL，**不再降级成"转人工"**；
3. 空图像列表不再崩溃（旧版 live_images=[] 会 AttributeError）；
4. 每次尝试都写审计；可解释字段齐全（原因码/分数/阈值/活体/见证人）；
5. 未登记"单独同意"的考生直接拒绝采集核验（合规硬约束）。

本版新增（两个所有者开关）：
6. 身份要素随「大考模式」切换：开启=身份证必校验（四要素）；关闭（默认）=身份证不参与、
   以准考证号核验（三要素）。
7. 「严格模式」可被所有者关闭：关闭后非现场来源（source=file）不再直接拒绝，
   但记录里会写明 strict_mode_off / non_live_source 并在原因中说明，绝不静默放宽。
"""
from datetime import datetime
from typing import Dict, List, Optional

import accounts
import config
import liveness
from face_engine import FaceError
from models import (
    HARD_FAIL_CODES, RC_AMBIGUOUS, RC_AUTO_PASS, RC_CONSENT_MISSING,
    RC_DOC_CHECK_CONFIRMED, RC_DOC_MISMATCH, RC_ID_MISMATCH, RC_IDENTITY_CONFLICT,
    RC_IMAGE_ERROR, RC_IMAGE_QUALITY_REVIEW, RC_LIVENESS_FAILED, RC_LIVENESS_UNAVAILABLE,
    RC_LOW_SIMILARITY, RC_NOT_FOUND, RC_NOT_MATCHED, RC_PASS, RC_POSE_EXTREME,
    RC_SEAT_MISMATCH, RC_TICKET_MISMATCH, RC_TWIN_SUSPECT, REASON_TEXT, RESULT_FAIL,
    RESULT_MANUAL, RESULT_PASS, VerifyRecord, WITNESS_REQUIRED_CODES,
)


def _now():
    return datetime.now().isoformat(timespec="seconds")


class FaceVerifier:
    def __init__(self, candidates: Dict, index, device_id: str, logger,
                 operator: str = "", enforce_consent: Optional[bool] = None,
                 require_liveness: Optional[bool] = None, four_element_checker=None):
        self.candidates = candidates
        self.index = index
        self.device_id = device_id
        self.logger = logger
        self.operator = operator
        self.enforce_consent = config.ENFORCE_CONSENT if enforce_consent is None else enforce_consent
        self.require_liveness = config.REQUIRE_LIVENESS if require_liveness is None else require_liveness
        # 允许注入四要素校验器：仅供自动化自检使用（库中身份证号是哈希，无法反推明文）
        self.four_element_checker = four_element_checker or self._check_four_elements

    # ---------------- 内部：构造记录 ----------------
    def _rec(self, candidate_id, mode, result, reason_code, **kw):
        rec = VerifyRecord(
            timestamp=_now(), device_id=self.device_id, candidate_id=candidate_id,
            mode=mode, result=result, reason_code=reason_code,
            reason=kw.pop("reason", REASON_TEXT.get(reason_code, "")),
            score=kw.pop("score", None), threshold=kw.pop("threshold", None),
            retry_count=kw.pop("retry_count", 0), liveness=kw.pop("liveness", liveness.STATUS_NOT_CHECKED),
            capture_age_sec=kw.pop("capture_age_sec", None),
            manual_required=kw.pop("manual_required", False),
            operator=kw.pop("operator", self.operator), witness=kw.pop("witness", ""),
            consent_status=kw.pop("consent_status", ""),
            required_witness=kw.pop("required_witness", reason_code in WITNESS_REQUIRED_CODES),
        )
        for k, v in kw.items():
            setattr(rec, k, v)
        return rec

    # ---------------- 内部：画质/姿态字段提取 ----------------
    @staticmethod
    def _apply_quality(rec: VerifyRecord, info: Optional[dict]):
        """把画质与姿态信息写入记录，便于事后解释与统计"""
        info = info or {}
        rep = info.get("quality")
        if rep is not None:
            rec.quality_severity = getattr(rep, "severity", "ok")
            rec.quality_issues = "、".join(getattr(rep, "issues", []) or [])
            rec.brightness = round(float(getattr(rep, "brightness", 0.0)), 1)
            rec.sharpness = round(float(getattr(rep, "sharpness", 0.0)), 1)
            rec.face_ratio = round(float(getattr(rep, "face_ratio", 0.0)), 4)
            rec.yaw = round(float(getattr(rep, "yaw", 0.0)), 1)
            rec.pitch = round(float(getattr(rep, "pitch", 0.0)), 1)
            rec.roll = round(float(getattr(rep, "roll", 0.0)), 1)
        rep_raw = info.get("quality_raw")
        if rep_raw is not None:
            rec.quality_issues_raw = "、".join(getattr(rep_raw, "issues", []) or [])
            rec.brightness_raw = round(float(getattr(rep_raw, "brightness", 0.0)), 1)
        rec.enhanced = bool(info.get("enhanced", False))
        return rec

    @staticmethod
    def _apply_edge(rec: VerifyRecord, info: Optional[dict]):
        """记录边缘计算信息：推理后端、档位、是否降档、单次耗时"""
        info = info or {}
        edge = info.get("edge") or {}
        if edge:
            rec.edge_provider = edge.get("provider", "")
            rec.edge_profile = edge.get("profile", "")
            rec.edge_degraded = bool(edge.get("degraded", False))
            rec.edge_latency_ms = edge.get("latency_ms")
        return rec

    @staticmethod
    def _pose_extreme(rep) -> bool:
        """是否为极端视角（仰拍/侧脸/歪头）——这类图像的特征可靠性下降，需复验"""
        if rep is None:
            return False
        import quality as _q
        return (_q.ISSUE_YAW in rep.issues or _q.ISSUE_ROLL in rep.issues
                or _q.ISSUE_PITCH_UP in rep.issues or _q.ISSUE_PITCH_DOWN in rep.issues)

    def _judge(self, score, thr, gray, info, pose_extreme):
        """
        综合判定：返回 (result, reason_code, reason, extra)

        顺序（先排除"不可信"的情形，再谈分数）：
          1. 画质严重不足        -> 复验/重拍（不拿失真的特征下结论）
          2. 更像另一名考生      -> 身份混淆，双人见证复验
          3. 与最像者差距过小    -> 疑似孪生/撞脸，双人见证复验
          4. 极端姿态（含仰拍）  -> 复验（特征可靠性下降）
          5. 灰区（模糊匹配）    -> 人工复验
          6. 达标                -> 放行
          7. 明显偏低            -> 画质有告警则转人工，否则判定非本人
        """
        rep = (info or {}).get("quality")
        other_id = (info or {}).get("best_other_id") or ""
        other_score = (info or {}).get("best_other_score")
        gap = None if other_score is None else round(score - other_score, 4)
        extra = {"twin_candidate_id": other_id, "twin_gap": gap}
        qtext = rep.issue_text() if rep is not None else ""

        # 1) 画质严重不足：不信算法结论
        if rep is not None and getattr(rep, "severity", "ok") == "severe":
            return (RESULT_MANUAL, RC_IMAGE_QUALITY_REVIEW,
                    "画质严重不足（%s）→ 建议重拍或人工核验" % qtext, extra)

        if score >= thr - gray:
            # 2) 更像库里的另一个人 -> 身份混淆
            if other_score is not None and other_score > score:
                return (RESULT_MANUAL, RC_IDENTITY_CONFLICT,
                        "现场人脸与考生 %s 的相似度(%.4f)高于被核验人(%.4f)，存在身份混淆，需双人见证复验"
                        % (other_id, other_score, score),
                        dict(extra, required_witness=True))

            # 3) 与最像者差距过小 -> 孪生/撞脸风险
            if (gap is not None and gap < config.TWIN_MARGIN
                    and other_score is not None and other_score >= thr - gray - config.TWIN_MARGIN):
                return (RESULT_MANUAL, RC_TWIN_SUSPECT,
                        "与考生 %s 的相似度 %.4f 仅相差 %.4f（< %.2f），存在孪生/撞脸风险，需双人见证复验"
                        % (other_id, other_score, gap, config.TWIN_MARGIN),
                        dict(extra, required_witness=True))

            # 4) 极端姿态（含仰拍）：即使分数达标也不单独放行
            if pose_extreme:
                return (RESULT_MANUAL, RC_POSE_EXTREME,
                        "极端视角（%s）影响特征可靠性，需人工复验" % qtext,
                        dict(extra, required_witness=True))

            # 5) 灰区
            if score < thr:
                return (RESULT_MANUAL, RC_AMBIGUOUS,
                        "相似度 %.4f 落在灰区 [%.2f, %.2f)，需人工复验"
                        % (score, thr - gray, thr),
                        dict(extra, required_witness=True))

            # 6) 达标
            return (RESULT_PASS, RC_PASS,
                    "相似度 %.4f ≥ 阈值 %.2f，与最像者相差 %s"
                    % (score, thr, ("%.4f" % gap) if gap is not None else "无对照"), extra)

        # 7) 明显低于灰区
        if rep is not None and getattr(rep, "severity", "ok") == "warn":
            return (RESULT_MANUAL, RC_LOW_SIMILARITY,
                    "相似度 %.4f 偏低且画质存在告警（%s），转人工核验" % (score, qtext),
                    dict(extra, required_witness=True))
        return (RESULT_FAIL, RC_NOT_MATCHED,
                "画质正常但相似度 %.4f 显著低于灰区下界 %.2f，判定非本人"
                % (score, thr - gray), dict(extra, manual_required=True))

    # ---------------- 身份要素（大考模式=四要素 / 常规模式=三要素） ----------------
    def identity_mode(self) -> str:
        """当前身份要素模式：major（大考，身份证必校验）/ normal（常规，身份证不参与）"""
        return "major" if accounts.major_exam_enabled() else "normal"

    def strict_mode_active(self) -> bool:
        """
        生效的严格模式 = 部署级开关(config.REQUIRE_LIVENESS) 且 所有者开关(strict_mode)。
        两者同时为真才严格；所有者关闭后即放宽（但仍会在记录里留痕）。
        """
        return bool(self.require_liveness) and accounts.strict_mode_enabled()

    @staticmethod
    def _id_card_plaintext(id_card):
        """
        身份证号"明文"判定：空值、或带掩码（含 *，例如库中带出的 3301**********1234）
        都视为**未提供明文**，不参与哈希比对。
        这样界面上的掩码展示不会被误当成可校验的号码。
        """
        s = (id_card or "").strip()
        if not s or "*" in s:
            return ""
        return s

    def _check_four_elements(self, candidate, id_card, ticket_no, seat_no):
        """
        身份要素校验（语义由所有者「大考模式」开关决定）：

          · 大考模式**开启** —— 四要素 = 身份证号 + 准考证号 + 座位号 + 姓名：
            身份证号**必填**，必须提供证件原件明文，走 candidate.check_id_card 的
            加盐哈希比对；只带出掩码或留空一律拦截。

          · 大考模式**关闭**（默认）—— 三要素 = 准考证号 + 座位号 + 姓名：
            身份证**不参与校验**（未登记身份证号的考生同样可以核验）；
            但若操作人员填写了身份证明文，仍要求与库内一致——避免"填错也放行"。

        准考证号在两种模式下都是**必填项**：留空或不一致一律拦截（TICKET_MISMATCH）。
        座位号不一致拦截（SEAT_MISMATCH）。姓名由 document_info() 带出供人工目视核对，
        不参与算法比对（库内即同名，自比无意义）。
        """
        mode_major = accounts.major_exam_enabled()
        plain = self._id_card_plaintext(id_card)
        if mode_major:
            if not plain:
                return (False, RC_ID_MISMATCH,
                        "大考模式：必须输入身份证号明文进行校验"
                        "（当前未提供；库中只存掩码/哈希，掩码不能作为校验依据）")
            ok, msg = candidate.check_id_card(plain)
            if not ok:
                return False, RC_ID_MISMATCH, msg
        elif plain:
            ok, msg = candidate.check_id_card(plain)
            if not ok:
                return False, RC_ID_MISMATCH, msg

        if not (ticket_no or "").strip():
            return (False, RC_TICKET_MISMATCH,
                    "准考证号必填：常规模式下以准考证号核验身份（大考模式下与身份证一并校验）")
        if (candidate.ticket_no or "") != (ticket_no or ""):
            return False, RC_TICKET_MISMATCH, REASON_TEXT[RC_TICKET_MISMATCH]
        if (candidate.seat_no or "") != (seat_no or ""):
            return False, RC_SEAT_MISMATCH, REASON_TEXT[RC_SEAT_MISMATCH]
        return True, "", ""

    def _apply_element_meta(self, rec, id_checked: bool):
        """把"本次按哪个模式、校验了哪些要素"写进记录（审计要能解释结论从哪来）"""
        rec.id_mode = self.identity_mode()
        els = ["准考证号", "座位号"]
        if id_checked:
            els.insert(0, "身份证号")
        rec.elements_checked = " + ".join(els)
        if not id_checked:
            rec.id_check_skipped = True
        return rec

    # ---------------- 严格模式关闭时的留痕 ----------------
    def _relax_note(self, live, capture_token):
        """
        图像不是现场实时抓拍、且严格模式未生效时，构造留痕说明。
        返回 dict 或 None；返回非 None 表示"本次按宽松模式放行，必须留痕"。

        严格模式未生效有两种成因，**必须区分清楚**，否则审计会把责任记错人：
          · 所有者关闭了「严格模式」开关（运行时可切，运行时决定）；
          · 部署级 `config.REQUIRE_LIVENESS=False`（环境变量，部署时决定）。
        """
        if live.status == liveness.STATUS_PASS:
            return None
        if self.strict_mode_active():
            return None
        if accounts.strict_mode_relaxed():
            why = "严格模式已关闭（所有者设置）"
        else:
            why = "现场性要求已关闭（部署级开关 FACE_REQUIRE_LIVENESS=0）"
        src = (capture_token or {}).get("source") or "file"
        return {
            "source": str(src),
            "reason": why,
            "text": ("%s，本次为非现场图片来源："
                     "source=%s，未通过设备实时抓拍校验（%s）；"
                     "相近结论仅作参考，已如实留痕" % (why, src, live.detail)),
        }

    @staticmethod
    def _mark_relaxed(rec, note):
        """把宽松放行的留痕写进核验记录（结论照常，但原因里必须写明）"""
        if not note:
            return rec
        rec.strict_mode_off = True
        rec.strict_mode_off_reason = note["reason"]
        rec.non_live_source = note["source"]
        rec.reason = "%s；%s" % (note["text"], rec.reason)
        return rec

    def _strict_reject(self, candidate_id, mode, live, age, consent, code, **kw):
        """严格模式下拒绝非现场来源（保持原有行为）"""
        return self._rec(candidate_id, mode, RESULT_FAIL, code,
                         reason="%s：%s" % (REASON_TEXT[code], live.detail),
                         liveness=live.status, capture_age_sec=age,
                         manual_required=True, consent_status=consent or "", **kw)

    # ---------------- 人脸通过后的证件核对（人脸先过、证件后核） ----------------
    @staticmethod
    def document_info(candidate) -> str:
        """
        生成"自动带出"给工作人员目视核对的证件信息。
        注意：库里只有掩码（明文不可逆），所以这里展示的是掩码，用于与证件实物对照；
        需要哈希级严格比对时，仍由工作人员手工输入完整号码走身份要素校验
        （大考模式下身份证必校验；常规模式下身份证不参与校验）。
        """
        if candidate is None:
            return ""
        return "姓名 %s ｜ 准考证 %s ｜ 座位 %s ｜ 身份证 %s" % (
            candidate.name or "-", candidate.ticket_no or "-", candidate.seat_no or "-",
            candidate.id_card_masked or "（未登记）")

    def confirm_document(self, candidate_id: str, operator: str = "", witness: str = "",
                         ok: bool = True, note: str = "", auto: bool = False) -> VerifyRecord:
        """
        证件核对确认，单独写一条审计记录。
          ok=True  -> DOC_CHECK_CONFIRMED（人工目视核对一致）
          ok=False -> DOC_MISMATCH（证件不符，拦截并转人工）
          auto=True -> AUTO_PASS（自动核验免人工确认；仅自动模式下使用，仍完整留痕）
        mode 记为 "DOC-CHECK"，统计"核验通过率"时应只统计 mode 为 1:1 / 1:N 的记录。
        """
        c = self.candidates.get(candidate_id)
        display = self.document_info(c)
        if ok:
            code = RC_AUTO_PASS if auto else RC_DOC_CHECK_CONFIRMED
            head = "自动核验通过（人脸匹配即放行，免人工确认）" if auto else "证件目视核对一致"
            rec = self._rec(candidate_id, "DOC-CHECK", RESULT_PASS, code,
                            reason="%s（%s）%s" % (head, display, ("；" + note) if note else ""),
                            operator=operator or self.operator,
                            witness=witness or ("自动核验" if auto else ""),
                            consent_status=(c.consent_status if c else ""))
            rec.doc_checked = True
            rec.doc_checked_at = rec.timestamp
            rec.id_card_display = display
        else:
            rec = self._rec(candidate_id, "DOC-CHECK", RESULT_FAIL, RC_DOC_MISMATCH,
                            reason="证件与考生不符，已拦截（%s）%s" % (display, ("；" + note) if note else ""),
                            operator=operator or self.operator, witness=witness,
                            manual_required=True,
                            consent_status=(c.consent_status if c else ""))
            rec.id_card_display = display
        self.logger.log(rec)
        return rec

    # ---------------- 1:1 ----------------
    def verify_1to1(self, candidate_id: str, live_image, id_card: str, ticket_no: str,
                    seat_no: str, capture_token: Optional[dict] = None,
                    witness: str = "", retry_count: int = 0) -> VerifyRecord:
        thr = config.THRESHOLD_1TO1
        common = dict(retry_count=retry_count, witness=witness)

        candidate = self.candidates.get(candidate_id)
        if not candidate:
            return self._rec(candidate_id, "1:1", RESULT_FAIL, RC_NOT_FOUND,
                             manual_required=True, **common)

        if self.enforce_consent and not candidate.consent_ok():
            return self._rec(candidate_id, "1:1", RESULT_FAIL, RC_CONSENT_MISSING,
                             manual_required=True, consent_status=candidate.consent_status, **common)

        ok, code, msg = self.four_element_checker(candidate, id_card, ticket_no, seat_no)
        # 身份证是否参与了校验：大考模式下必定参与；常规模式下仅在提供了明文时参与
        id_checked = accounts.major_exam_enabled() or bool(self._id_card_plaintext(id_card))
        if not ok:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, code, reason=msg,
                            manual_required=True, consent_status=candidate.consent_status, **common)
            return self._apply_element_meta(rec, id_checked)

        # 现场性：严格模式下无现场令牌 = 拒绝放行；
        # 所有者关闭严格模式后放宽，但必须留痕（见 _relax_note / _mark_relaxed）
        live = liveness.check(capture_token, self.device_id, image=live_image)
        age = liveness.capture_age(capture_token)
        if live.status == liveness.STATUS_FAIL:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_LIVENESS_FAILED,
                            reason=live.detail, liveness=live.status, capture_age_sec=age,
                            manual_required=True, consent_status=candidate.consent_status, **common)
            return self._apply_element_meta(rec, id_checked)
        relax = self._relax_note(live, capture_token)
        if live.status != liveness.STATUS_PASS and relax is None:
            rec = self._strict_reject(candidate_id, "1:1", live, age, candidate.consent_status,
                                      RC_LIVENESS_UNAVAILABLE, **common)
            return self._apply_element_meta(rec, id_checked)

        # 人脸比对（含画质/姿态与"最像的另一人"上下文）
        try:
            score, info = self.index.score_with_context(candidate_id, live_image)
        except FaceError as e:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, e.code, reason=e.msg,
                            liveness=live.status, capture_age_sec=age,
                            manual_required=True, consent_status=candidate.consent_status, **common)
            return self._mark_relaxed(self._apply_element_meta(rec, id_checked), relax)
        except Exception as e:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_IMAGE_ERROR,
                            reason="图像处理失败：%s" % str(e)[:80],
                            liveness=live.status, capture_age_sec=age,
                            manual_required=True, consent_status=candidate.consent_status, **common)
            return self._mark_relaxed(self._apply_element_meta(rec, id_checked), relax)

        rep = (info or {}).get("quality")
        pose_extreme = self._pose_extreme(rep)
        result, code, reason, extra = self._judge(score, thr, config.GRAY_MARGIN_1TO1,
                                                  info, pose_extreme)
        kw = dict(common, **extra)
        kw.setdefault("manual_required", result != RESULT_PASS)
        rec = self._rec(candidate_id, "1:1", result, code, reason=reason,
                        score=round(score, 4), threshold=thr, liveness=live.status,
                        capture_age_sec=age, consent_status=candidate.consent_status,
                        gray_zone=(thr - config.GRAY_MARGIN_1TO1 <= score < thr), **kw)
        self._apply_quality(rec, info)
        self._apply_edge(rec, info)
        self._apply_document(rec, candidate)
        self._apply_element_meta(rec, id_checked)
        return self._mark_relaxed(rec, relax)

    # ---------------- 扫脸核验（身份要素可选校验 + 证件人工核对） ----------------
    def verify_face_first(self, candidate_id: str, live_images, capture_token: Optional[dict] = None,
                          witness: str = "", auto_confirm: bool = False,
                          id_card=None, ticket_no=None, seat_no=None) -> VerifyRecord:
        """
        扫脸核验：**以脸为主**，身份信息（姓名/准考证/座位/身份证掩码）由系统自动带出，
        证件由工作人员对照实物目视核对（confirm_document 留痕）。

        身份要素校验（可选，由调用方决定是否启用）：
          传入 id_card / ticket_no / seat_no 中任意一项，就先按当前模式做要素校验
          （大考模式=四要素，身份证必填；常规模式=三要素，准考证必填），不一致直接拦截；
          一项都不传则保持原流程不变（只扫脸 + 人工核对证件）。
          **例外**：大考模式下即使一项都不传也会被拦截（`ID_MISMATCH`）——
          否则调用方只要不传参数就能静默绕过"必须使用身份证"这条要求。

        各要素的来源说明：
          · 大考模式下身份证号必须是**证件原件明文**，掩码（含 *）不算提供；
          · 常规模式下身份证不参与校验，未登记身份证的考生同样可以核验。
        """
        thr = config.THRESHOLD_1TO1
        candidate = self.candidates.get(candidate_id)
        if not candidate:
            return self._rec(candidate_id, "1:1", RESULT_FAIL, RC_NOT_FOUND, manual_required=True)
        if self.enforce_consent and not candidate.consent_ok():
            return self._rec(candidate_id, "1:1", RESULT_FAIL, RC_CONSENT_MISSING, manual_required=True,
                             consent_status=candidate.consent_status)

        # ---- 身份要素校验 ----
        # 调用方传了要素值 -> 按当前模式校验；
        # 大考模式下即使一个都没传也**必须拦**（否则等于静默跳过身份证校验）。
        elements_supplied = any(v is not None for v in (id_card, ticket_no, seat_no))
        id_checked = accounts.major_exam_enabled() or bool(self._id_card_plaintext(id_card))
        if not elements_supplied and accounts.major_exam_enabled():
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_ID_MISMATCH,
                            reason="大考模式：必须输入身份证号明文进行校验"
                                   "（本调用方未提供任何身份要素）",
                            manual_required=True, consent_status=candidate.consent_status,
                            witness=witness)
            self._apply_element_meta(rec, True)
            self.logger.log(rec)
            return rec
        if elements_supplied:
            ok, code, msg = self.four_element_checker(
                candidate, id_card or "", ticket_no or "", seat_no or "")
            if not ok:
                rec = self._rec(candidate_id, "1:1", RESULT_FAIL, code, reason=msg,
                                manual_required=True, consent_status=candidate.consent_status,
                                witness=witness)
                self._apply_element_meta(rec, id_checked)
                self.logger.log(rec)
                return rec

        images = [im for im in (live_images or []) if im is not None]
        if not images:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_IMAGE_ERROR,
                            reason="未提供现场图像", manual_required=True,
                            consent_status=candidate.consent_status)
            self._apply_element_meta(rec, id_checked)
            self.logger.log(rec)
            return rec

        last = None
        for i, img in enumerate(images[:config.MAX_RETRY], start=1):
            live = liveness.check(capture_token, self.device_id, image=img)
            age = liveness.capture_age(capture_token)
            if live.status == liveness.STATUS_FAIL:
                rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_LIVENESS_FAILED,
                                reason=live.detail, liveness=live.status, capture_age_sec=age,
                                manual_required=True, consent_status=candidate.consent_status,
                                retry_count=i, witness=witness)
                self._apply_element_meta(rec, id_checked)
                self.logger.log(rec)
                return rec
            relax = self._relax_note(live, capture_token)
            if live.status != liveness.STATUS_PASS and relax is None:
                rec = self._strict_reject(candidate_id, "1:1", live, age, candidate.consent_status,
                                          RC_LIVENESS_UNAVAILABLE, retry_count=i, witness=witness)
                self._apply_element_meta(rec, id_checked)
                self.logger.log(rec)
                return rec

            try:
                score, info = self.index.score_with_context(candidate_id, img)
            except FaceError as e:
                rec = self._rec(candidate_id, "1:1", RESULT_FAIL, e.code, reason=e.msg,
                                liveness=live.status, capture_age_sec=age, manual_required=True,
                                consent_status=candidate.consent_status, retry_count=i, witness=witness)
                self._apply_element_meta(rec, id_checked)
                self.logger.log(self._mark_relaxed(rec, relax))
                return rec
            except Exception as e:
                rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_IMAGE_ERROR,
                                reason="图像处理失败：%s" % str(e)[:80], liveness=live.status,
                                capture_age_sec=age, manual_required=True,
                                consent_status=candidate.consent_status, retry_count=i, witness=witness)
                self._apply_element_meta(rec, id_checked)
                self.logger.log(self._mark_relaxed(rec, relax))
                return rec

            rep = (info or {}).get("quality")
            result, code, reason, extra = self._judge(score, thr, config.GRAY_MARGIN_1TO1,
                                                      info, self._pose_extreme(rep))
            kw = dict(extra, retry_count=i, witness=witness)
            kw.setdefault("manual_required", result != RESULT_PASS)
            rec = self._rec(candidate_id, "1:1", result, code,
                            reason="%s（证件由人工目视核对）" % reason,
                            score=round(score, 4), threshold=thr, liveness=live.status,
                            capture_age_sec=age, consent_status=candidate.consent_status,
                            gray_zone=(thr - config.GRAY_MARGIN_1TO1 <= score < thr), **kw)
            self._apply_quality(rec, info)
            self._apply_edge(rec, info)
            self._apply_document(rec, candidate)
            self._apply_element_meta(rec, id_checked)
            self._mark_relaxed(rec, relax)
            self.logger.log(rec)
            if rec.result == RESULT_PASS:
                if auto_confirm:
                    self._auto_confirm(rec, candidate)
                return rec
            last = rec
        return last

    def _auto_confirm(self, rec: VerifyRecord, candidate):
        """
        自动核验模式：人脸匹配即自动通过，不需要人工点「证件一致」。
        仍然单独写一条 AUTO_PASS 审计记录（谁授权、什么时候、带出的证件信息），保证可追溯。
        """
        if candidate is None:
            return rec
        doc = self.confirm_document(candidate.candidate_id, operator=self.operator,
                                    witness="自动核验", ok=True, auto=True)
        rec.doc_checked = True
        rec.doc_checked_at = doc.doc_checked_at
        rec.reason += "；已自动通过（免人工确认，见 DOC-CHECK 记录）"
        return rec

    def _apply_document(self, rec: VerifyRecord, candidate):
        """人脸这一条记录里也带上"自动补齐的身份信息"（掩码）留痕"""
        rec.id_card_display = self.document_info(candidate)
        return rec

    # ---------------- 带重试 ----------------
    def verify_with_retry(self, candidate_id: str, live_images: List, **kwargs) -> VerifyRecord:
        if not live_images:
            rec = self._rec(candidate_id, "1:1", RESULT_FAIL, RC_IMAGE_ERROR,
                            reason="未提供现场图像", manual_required=True)
            self.logger.log(rec)
            return rec

        last = None
        attempts = list(live_images)[:config.MAX_RETRY]
        for i, img in enumerate(attempts, start=1):
            rec = self.verify_1to1(candidate_id, img, retry_count=i, **kwargs)
            if rec.result == RESULT_PASS:
                self.logger.log(rec)
                return rec
            if rec.reason_code in HARD_FAIL_CODES:
                # 硬失败：重试无意义（证件不符、无脸、非现场抓拍…），立即拦截并留痕
                self.logger.log(rec)
                return rec
            self.logger.log(rec)          # 每次尝试都留痕，便于回溯
            last = rec

        if last is not None:
            last.result = RESULT_MANUAL
            last.reason += "；重试 %d 次仍未通过，转人工核验" % len(attempts)
            last.manual_required = True
            return last
        return self._rec(candidate_id, "1:1", RESULT_FAIL, RC_IMAGE_ERROR,
                         reason="未提供有效的现场图像", manual_required=True)

    # ---------------- 1:N ----------------
    def verify_1toN(self, live_image, candidate_id: str = None,
                    capture_token: Optional[dict] = None, witness: str = "",
                    auto_confirm: bool = False) -> VerifyRecord:
        thr = config.THRESHOLD_1TON
        live = liveness.check(capture_token, self.device_id, image=live_image)
        age = liveness.capture_age(capture_token)
        relax = self._relax_note(live, capture_token)
        if relax is None and live.status != liveness.STATUS_PASS:
            rec = self._strict_reject(candidate_id, "1:N", live, age, "",
                                      RC_LIVENESS_UNAVAILABLE, witness=witness)
            self.logger.log(rec)
            return rec

        try:
            hits, info = self.index.identify_with_context(live_image, topk=3)
        except FaceError as e:
            rec = self._rec(None, "1:N", RESULT_FAIL, e.code, reason=e.msg,
                            liveness=live.status, capture_age_sec=age, manual_required=True,
                            witness=witness)
            self.logger.log(self._mark_relaxed(rec, relax))
            return rec
        except Exception as e:
            rec = self._rec(None, "1:N", RESULT_FAIL, RC_IMAGE_ERROR,
                            reason="图像处理失败：%s" % str(e)[:80],
                            liveness=live.status, capture_age_sec=age, manual_required=True,
                            witness=witness)
            self.logger.log(self._mark_relaxed(rec, relax))
            return rec

        if not hits:
            rec = self._rec(None, "1:N", RESULT_FAIL, RC_NOT_FOUND, reason="特征索引为空，请先建档",
                            liveness=live.status, capture_age_sec=age, manual_required=True,
                            witness=witness)
            self.logger.log(self._mark_relaxed(rec, relax))
            return rec

        best_id, best_score = hits[0]
        detail = "候选：" + "、".join("%s %.3f" % (c, s) for c, s in hits)
        consent = ""
        if best_id and best_id in self.candidates:
            consent = self.candidates[best_id].consent_status

        rep = (info or {}).get("quality")
        pose_extreme = self._pose_extreme(rep)
        result, code, reason, extra = self._judge(best_score, thr, config.GRAY_MARGIN_1TON,
                                                  info, pose_extreme)
        kw = dict(extra)
        kw.setdefault("manual_required", result != RESULT_PASS)
        rec = self._rec(best_id, "1:N", result, code, reason="%s；%s" % (reason, detail),
                        score=round(best_score, 4), threshold=thr, liveness=live.status,
                        capture_age_sec=age, consent_status=consent, witness=witness,
                        gray_zone=(thr - config.GRAY_MARGIN_1TON <= best_score < thr), **kw)
        rec = self._apply_quality(rec, info)
        rec = self._apply_edge(rec, info)
        cand = self.candidates.get(best_id) if best_id else None
        self._apply_document(rec, cand)
        self._mark_relaxed(rec, relax)
        self.logger.log(rec)
        if rec.result == RESULT_PASS and auto_confirm:
            self._auto_confirm(rec, cand)
        return rec
