# -*- coding: utf-8 -*-
"""
数据结构定义（仅标准库，供人脸端与网页端共用）。

相比旧版的变化：
1. 身份证号不再落明文：只保留 salt + hash + 掩码；
2. 新增合规字段：单独同意状态、同意时间、保留期限；
3. 核验记录可解释：reason_code + score + threshold + 活体状态 + 见证人 + 操作人；
4. 记录支持哈希链字段，用于防篡改审计。
"""
from dataclasses import dataclass, field, asdict
from typing import Optional

import pii


# ---------------- 结果与原因码 ----------------
RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_MANUAL = "MANUAL"          # 转人工复核（不是通过）
RESULT_VALUES = (RESULT_PASS, RESULT_FAIL, RESULT_MANUAL)

# 原因码：用于可解释输出与统计，禁止只写"疑似作弊/不通过"这种模糊描述
RC_PASS = "PASS"
RC_NOT_FOUND = "NOT_FOUND"                  # 报名库无此考生
RC_ID_MISMATCH = "ID_MISMATCH"              # 身份证号不一致
RC_TICKET_MISMATCH = "TICKET_MISMATCH"      # 准考证号不一致
RC_SEAT_MISMATCH = "SEAT_MISMATCH"          # 座位号不一致
RC_CONSENT_MISSING = "CONSENT_MISSING"      # 未取得单独同意
RC_NO_FACE = "NO_FACE"                      # 未检测到人脸
RC_MULTI_FACE = "MULTI_FACE"                # 检测到多张人脸
RC_LOW_QUALITY = "LOW_QUALITY"              # 人脸过小/画质不足
RC_IMAGE_ERROR = "IMAGE_ERROR"              # 图像无法读取
RC_LIVENESS_UNAVAILABLE = "LIVENESS_UNAVAILABLE"  # 严格模式但非实时抓拍
RC_LIVENESS_FAILED = "LIVENESS_FAILED"      # 活体检测未通过
RC_LOW_SIMILARITY = "LOW_SIMILARITY"        # 相似度不足（转人工）
# ---- 异常状态 / 极端情况相关 ----
RC_AMBIGUOUS = "AMBIGUOUS"                  # 灰区：模糊匹配落入不确信区间 -> 人工复验
RC_TWIN_SUSPECT = "TWIN_SUSPECT"            # 撞脸/孪生风险：与最像的另一人差距过小 -> 双人见证复验
RC_IDENTITY_CONFLICT = "IDENTITY_CONFLICT"  # 身份混淆：现场图像更接近另一名考生 -> 双人见证复验
RC_IMAGE_QUALITY_REVIEW = "IMAGE_QUALITY_REVIEW"  # 画质严重不足（昏暗/模糊/过小）-> 重拍或人工核验
RC_POSE_EXTREME = "POSE_EXTREME"            # 极端视角（仰拍/侧脸/歪头）-> 人工复验
RC_NOT_MATCHED = "NOT_MATCHED"              # 画质正常但相似度显著偏低 -> 判定非本人
# ---- 人脸通过后的"证件核对"环节（人脸先过、证件后核）----
RC_DOC_CHECK_CONFIRMED = "DOC_CHECK_CONFIRMED"  # 已带出证件信息并由工作人员目视核对确认
RC_DOC_MISMATCH = "DOC_MISMATCH"                # 工作人员目视发现证件与考生不符 -> 拦截
RC_ID_CHECK_SKIPPED = "ID_CHECK_SKIPPED"        # 演示模式：显式跳过身份证哈希比对（必须留痕）
RC_AUTO_PASS = "AUTO_PASS"                      # 自动核验通过：人脸匹配即放行（免人工确认，留痕）
# ---- 命令控制台（所有者开关，默认关闭；每次执行都必须留痕）----
RC_CMD_EXEC = "CMD_EXEC"                        # 命令控制台执行了一条命令 / 一段代码

# 硬失败：属于"必须拦截"，不因重试而改变结论，也不得降级为转人工
HARD_FAIL_CODES = {
    RC_NOT_FOUND,
    RC_ID_MISMATCH,
    RC_TICKET_MISMATCH,
    RC_SEAT_MISMATCH,
    RC_CONSENT_MISSING,
    RC_NO_FACE,
    RC_MULTI_FACE,
    RC_LOW_QUALITY,
    RC_IMAGE_ERROR,
    RC_LIVENESS_UNAVAILABLE,
    RC_LIVENESS_FAILED,
    RC_NOT_MATCHED,
}

# 需要"双人见证"的人工复验情形（风险更高，必须留痕到人）
WITNESS_REQUIRED_CODES = {
    RC_TWIN_SUSPECT,
    RC_IDENTITY_CONFLICT,
    RC_AMBIGUOUS,
    RC_LOW_SIMILARITY,
    RC_POSE_EXTREME,
}

# 原因码中文说明（用于界面与报告）
REASON_TEXT = {
    RC_PASS: "核验通过",
    RC_NOT_FOUND: "报名库中无此考生",
    RC_ID_MISMATCH: "身份证号不一致",
    RC_TICKET_MISMATCH: "准考证号不一致",
    RC_SEAT_MISMATCH: "座位号不一致",
    RC_CONSENT_MISSING: "未登记单独同意，禁止采集与核验",
    RC_NO_FACE: "画面中未检测到人脸",
    RC_MULTI_FACE: "画面中检测到多张人脸，需单人核验",
    RC_LOW_QUALITY: "人脸区域过小或画质不足",
    RC_IMAGE_ERROR: "图像无法读取或未提供现场图像",
    RC_LIVENESS_UNAVAILABLE: "非设备实时抓拍图像，严格模式下拒绝放行",
    RC_LIVENESS_FAILED: "活体检测未通过",
    RC_LOW_SIMILARITY: "相似度低于阈值，转人工核验",
    RC_AMBIGUOUS: "相似度落在灰区（不确信区间），需人工复验",
    RC_TWIN_SUSPECT: "存在孪生/撞脸风险（与最像的另一人差距过小），需双人见证复验",
    RC_IDENTITY_CONFLICT: "身份混淆：现场人脸与另一名考生更像，需双人见证复验",
    RC_IMAGE_QUALITY_REVIEW: "画质严重不足（昏暗/模糊/人脸过小），需重拍或人工核验",
    RC_POSE_EXTREME: "极端视角（仰拍/侧脸/歪头）影响特征可靠性，需人工复验",
    RC_NOT_MATCHED: "画质正常但相似度显著偏低，判定非本人",
    RC_DOC_CHECK_CONFIRMED: "证件已人工目视核对，确认一致",
    RC_DOC_MISMATCH: "工作人员发现证件与考生不符，已拦截",
    RC_ID_CHECK_SKIPPED: "演示模式：已显式跳过身份证哈希比对（审计留痕）",
    RC_AUTO_PASS: "自动核验通过：人脸匹配即放行（免人工确认，已留痕）",
    RC_CMD_EXEC: "命令控制台执行（仅所有者可用，命令与结果明细见 reason 字段）",
}


@dataclass
class Candidate:
    """考生报名库记录（身份证号仅以 salt/hash/掩码 形式存在）"""
    candidate_id: str
    name: str = ""
    ticket_no: str = ""
    seat_no: str = ""
    reference_photo: str = ""          # 建档用；建档后可删除，核验不再依赖原图
    id_card_salt: str = ""
    id_card_hash: str = ""
    id_card_masked: str = ""
    consent_status: str = "未登记"      # 未登记 / 已同意 / 已拒绝 / 替代通道
    consent_at: str = ""
    retention_until: str = ""           # ISO 日期，到期应清理
    updated_at: str = ""
    updated_by: str = ""

    # ---- 身份要素校验（身份证号走哈希比对，不落明文） ----
    # 身份证是否参与校验由所有者「大考模式」开关决定，见 verifier._check_four_elements
    def check_id_card(self, id_card_plain: str):
        if not self.id_card_hash:
            return False, "该考生未登记身份证号，需人工核验"
        if not pii.verify_id_card(id_card_plain, self.id_card_salt, self.id_card_hash):
            return False, REASON_TEXT[RC_ID_MISMATCH]
        return True, ""

    def consent_ok(self):
        return self.consent_status in ("已同意", "已取得单独同意", "替代通道")


@dataclass
class VerifyRecord:
    """核验记录：可解释 + 可审计（含哈希链字段）"""
    timestamp: str
    device_id: str
    candidate_id: Optional[str]
    mode: str                                  # 1:1 / 1:N
    result: str                                # PASS / FAIL / MANUAL
    reason_code: str = RC_PASS
    reason: str = ""
    score: Optional[float] = None              # 原始余弦相似度
    threshold: Optional[float] = None
    retry_count: int = 0
    liveness: str = "NOT_CHECKED"              # PASS / FAIL / UNKNOWN / NOT_CHECKED
    capture_age_sec: Optional[int] = None
    manual_required: bool = False              # 是否需人工介入（即使结果是 FAIL）
    operator: str = ""                         # 操作人
    witness: str = ""                          # 人工核验见证人
    consent_status: str = ""
    # ---- 异常状态 / 极端情况的可解释字段 ----
    quality_severity: str = "ok"               # ok / warn / severe
    quality_issues: str = ""                   # 具体问题，如"光线昏暗、仰拍（相机低于人脸）"
    quality_issues_raw: str = ""               # 原图（增强前）的问题，用于解释"是不是真的暗"
    brightness: Optional[float] = None         # 判定所用图像的人脸区域亮度均值
    brightness_raw: Optional[float] = None     # 原图亮度（增强前），可与 brightness 对比看增强效果
    sharpness: Optional[float] = None          # 人脸区域清晰度（拉普拉斯方差）
    face_ratio: Optional[float] = None         # 人脸框占画面面积比
    yaw: Optional[float] = None                # 近似偏航角
    pitch: Optional[float] = None              # 近似俯仰角（负 = 仰拍）
    roll: Optional[float] = None               # 近似滚转角
    enhanced: bool = False                     # 是否经过低画质增强后再判定
    gray_zone: bool = False                    # 是否落入灰区（模糊匹配不确信区间）
    twin_candidate_id: str = ""                # 与之"撞脸"的另一名考生
    twin_gap: Optional[float] = None           # 本人得分 - 撞脸者得分
    required_witness: bool = False             # 是否必须双人见证复验
    # ---- 人脸通过后的"证件核对"环节 ----
    id_card_display: str = ""                  # 当时向工作人员展示的证件信息（掩码），留痕用
    doc_checked: bool = False                  # 是否已完成人工证件核对
    doc_checked_at: str = ""                   # 核对时间
    id_check_skipped: bool = False             # 是否跳过了身份证哈希比对（仅演示，必须留痕）
    # ---- 身份要素模式（所有者「大考模式」开关）与严格模式留痕 ----
    id_mode: str = ""                          # major=大考模式(四要素,身份证必校验) / normal=常规模式(三要素)
    elements_checked: str = ""                 # 本次实际参与校验的身份要素，如"准考证号 + 座位号"
    strict_mode_off: bool = False              # 本次判定时严格模式未生效（非现场来源据此放行）
    strict_mode_off_reason: str = ""           # 严格模式为何未生效：所有者开关 / 部署级开关
    non_live_source: str = ""                  # 非现场来源说明，如"file（文件选择/上传照片）"
    # ---- 边缘计算相关 ----
    edge_provider: str = ""                    # 实际使用的推理后端（TensorRT/CUDA/CANN/OpenVINO/CPU）
    edge_profile: str = ""                     # 边缘档位：full / balanced / lite
    edge_degraded: bool = False                # 是否因边缘过载而降档处理
    edge_latency_ms: Optional[float] = None    # 本次推理耗时（毫秒）
    # 哈希链
    prev_hash: str = ""
    hash: str = ""

    def to_dict(self):
        return asdict(self)

    def core_for_hash(self):
        """参与哈希计算的字段（不含 hash 自身，prev_hash 参与）"""
        d = asdict(self)
        d.pop("hash", None)
        return d
