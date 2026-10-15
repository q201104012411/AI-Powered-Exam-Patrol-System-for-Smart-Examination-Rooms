# -*- coding: utf-8 -*-
"""
09 无障碍与特殊考生保障 · 数据模型

一律用 dataclass，字段与 storage.py 的表一一对应；
时间字段同时给字符串（'YYYY-MM-DD HH:MM:SS'，便于入 SQLite 与报表）与 *_epoch（便于窗口统计）。
"""
import json
import time
import uuid
from dataclasses import dataclass, field, asdict


def now_epoch():
    return time.time()


def now_ts(epoch=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch or time.time()))


def new_id(prefix):
    """短 ID：前缀 + 时间 + 4 位随机，便于人工口头报号。"""
    return "%s%s%04d" % (prefix, time.strftime("%H%M%S"), uuid.uuid4().int % 10000)


def jdump(obj):
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def jload(text, default=None):
    try:
        v = json.loads(text or "")
        return v if v is not None else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


# ---------------- 考点 ----------------
@dataclass
class Site:
    site_id: str = ""
    name: str = ""
    address: str = ""
    gate_total: int = 0             # 入口/通道总数（配置率的分母口径之一）
    note: str = ""
    created_at: str = field(default_factory=now_ts)

    def to_dict(self):
        return asdict(self)


# ---------------- 无障碍通道 / 核验通道 ----------------
@dataclass
class Channel:
    channel_id: str = ""
    code: str = ""                  # 通道编码，如 ACC-A01
    name: str = ""
    site: str = ""
    kind: str = "accessible"        # config.CHANNEL_KINDS
    location: str = ""              # 位置：东门 1 号口 / 一层大厅
    width_mm: int = 0               # 净宽（轮椅通行 ≥900mm）
    turn_radius_mm: int = 0         # 回转空间（≥1500mm）
    low_counter: bool = False       # 是否低位核验终端
    counter_height_mm: int = 0      # 终端台面高度（可调范围取上限；≤1100mm 视为低位）
    height_adjustable: bool = False  # 高度是否可调
    voice_prompt: bool = False      # 语音提示
    text_prompt: bool = False       # 文字/大字提示
    vibration: bool = False         # 震动提示
    sign_service: bool = False      # 手语/书面交流服务
    status: str = "building"        # config.CHANNEL_STATES
    devices: list = field(default_factory=list)   # 通道内设备编码（核验终端/闸机/呼叫器）
    last_check_at: str = ""
    last_check_epoch: float = 0.0
    note: str = ""
    created_at: str = field(default_factory=now_ts)
    source: str = "manual"          # real / manual / demo（**如实标注**）

    def to_dict(self):
        d = asdict(self)
        d["devices"] = list(self.devices or [])
        return d

    def kind_text(self):
        import config
        return config.CHANNEL_TEXT.get(self.kind, self.kind)

    def state_text(self):
        import config
        return config.CHANNEL_STATE_TEXT.get(self.status, self.status)

    def usable(self):
        import config
        return self.status in config.CHANNEL_USABLE


# ---------------- 特殊考生无障碍档案 ----------------
@dataclass
class Profile:
    profile_id: str = ""
    candidate_id: str = ""          # 身份证/准考证（落库前加密，另存 blind key）
    candidate_name: str = ""
    candidate_key: str = ""         # 盲索引：sha256("acc09|" + 考生号)
    ticket_no: str = ""
    room_id: str = ""               # 考场
    seat_no: str = ""               # 座位
    site: str = ""
    needs: list = field(default_factory=list)      # config.NEED_KINDS
    need_detail: str = ""           # 具体说明（脱敏导出，明文加密落库）
    measures: list = field(default_factory=list)   # 推荐/已批准措施（config.MEASURE_KINDS）
    channel_hint: str = ""          # 建议通道（config.CHANNEL_KINDS）
    cert_no: str = ""               # 医疗/残疾证明编号（加密）
    cert_verified: bool = False     # 证明是否已核验
    contact: str = ""               # 紧急联系人/陪考人（加密）
    consent: bool = False           # 是否取得**单独同意**（人脸等敏感信息处理）
    consent_scope: str = ""         # 同意范围说明
    consent_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    status: str = "active"          # active / closed
    source: str = "manual"          # real / manual / demo
    operator: str = ""
    note: str = ""
    created_at: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)

    def to_dict(self):
        d = asdict(self)
        d["needs"] = list(self.needs or [])
        d["measures"] = list(self.measures or [])
        return d

    def needs_text(self):
        import config
        return config.need_text(self.needs)

    def measures_text(self):
        import config
        return config.measures_text(self.measures)


# ---------------- 保障措施发放（措施级留痕） ----------------
@dataclass
class Accommodation:
    acc_id: str = ""
    profile_id: str = ""
    candidate_key: str = ""
    measure: str = "manual_verify"  # config.MEASURE_KINDS
    status: str = "requested"       # config.ACC_STATES
    owner: str = ""                 # 责任人/岗位（谁去落实）
    place: str = ""                 # 落实位置（候考区/通道号）
    provided_at: str = ""
    provided_epoch: float = 0.0
    operator: str = ""
    note: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)

    def to_dict(self):
        return asdict(self)

    def measure_text(self):
        import config
        return config.measure_text(self.measure)

    def state_text(self):
        import config
        return config.ACC_STATE_TEXT.get(self.status, self.status)


# ---------------- 替代核验（人工兜底，与智能核验同等效力） ----------------
@dataclass
class AltVerify:
    av_id: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    candidate_key: str = ""
    ticket_no: str = ""
    room_id: str = ""
    seat_no: str = ""
    site: str = ""
    mode: str = "manual_doc"        # config.ALT_MODES
    docs: dict = field(default_factory=dict)   # {config.DOC_*: config.DOC_STATES}
    doc_matched: int = 0
    inspector: str = ""              # 主检人
    witness: str = ""                # 见证人（双人见证，不得与主检人相同）
    needs: list = field(default_factory=list)
    reason: str = ""                 # 为什么要替代核验（人脸失败/无证明/画质不可信...）
    tech_reason: bool = False        # 是否由技术原因触发（设备/算法/画质）
    device_code: str = ""            # 相关机位/终端
    smart_result: dict = field(default_factory=dict)   # 智能核验结论（01/02 返回原文）
    camera_credibility: float = 1.0  # 02 给的机位画质可信度
    outcome: str = "passed"          # config.ALT_OUTCOMES
    equivalence: bool = True         # 与智能核验同等效力（制度性，不因设备而变）
    sla_sec: int = 180
    elapsed_sec: float = 0.0
    overdue: bool = False            # 是否超过替代核验时限
    alert_id: str = ""               # 推 03 智能终端后的告警号
    drill: bool = False              # True=演练记录
    operator: str = ""
    source: str = "manual"
    note: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)

    def to_dict(self):
        d = asdict(self)
        d["needs"] = list(self.needs or [])
        d["docs"] = dict(self.docs or {})
        return d

    def mode_text(self):
        import config
        return config.ALT_MODE_TEXT.get(self.mode, self.mode)

    def outcome_text(self):
        import config
        return config.ALT_OUTCOME_TEXT.get(self.outcome, self.outcome)

    def docs_text(self):
        import config
        if not self.docs:
            return "无"
        return "、".join("%s=%s" % (config.DOC_TEXT.get(k, k),
                                    config.DOC_STATE_TEXT.get(v, v))
                         for k, v in sorted(self.docs.items()))


# ---------------- 因技术原因入场受阻事件（指标：0 起） ----------------
@dataclass
class TechBlock:
    block_id: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    candidate_key: str = ""
    room_id: str = ""
    seat_no: str = ""
    site: str = ""
    symptom: str = "face_fail"      # config.BLOCK_SYMPTOMS
    device_code: str = ""
    detail: str = ""
    channel_code: str = ""          # 当时用的通道
    raised_at: str = field(default_factory=now_ts)
    raised_epoch: float = field(default_factory=now_epoch)
    resolved: bool = False
    final_result: str = ""          # config.FINAL_RESULTS
    resolution: str = ""            # 怎么解决的（替代核验/换通道/专人引导）
    waited_sec: float = 0.0         # 受阻到入场/处置的等待时长
    resolved_at: str = ""
    resolved_epoch: float = 0.0
    alert_id: str = ""              # 推 03 后的告警号
    escalated: bool = False         # 是否已上报 03
    operator: str = ""
    source: str = "manual"
    note: str = ""

    def to_dict(self):
        return asdict(self)

    def symptom_text(self):
        import config
        return config.BLOCK_SYMPTOMS.get(self.symptom, self.symptom)

    def final_text(self):
        import config
        return config.FINAL_TEXT.get(self.final_result, self.final_result or "未结案")


# ---------------- 算法公平性分层测试 ----------------
@dataclass
class FairnessRun:
    run_id: str = ""
    name: str = ""
    site: str = ""
    algorithm: str = ""             # 被测算法/模型与版本（如 01 人脸 1:1 v2）
    threshold: float = 0.0          # 当次生效阈值
    total: int = 0
    dims: list = field(default_factory=list)       # 覆盖的分层维度
    groups_count: int = 0
    max_gap: float = 0.0            # 组间通过率最大差
    min_rate: float = 0.0
    max_rate: float = 0.0
    worst_dim: str = ""
    worst_group: str = ""
    passed: bool = False            # 差异是否在可接受范围
    action: str = "none"            # config.FAIRNESS_ADJUST_ACTIONS
    action_note: str = ""
    small_groups: list = field(default_factory=list)   # 样本不足的分组（结论不可靠）
    operator: str = ""
    source: str = "manual"
    note: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)

    def to_dict(self):
        d = asdict(self)
        d["dims"] = list(self.dims or [])
        d["small_groups"] = list(self.small_groups or [])
        return d

    def action_text(self):
        import config
        return config.FAIRNESS_ADJUST_TEXT.get(self.action, self.action)


@dataclass
class FairnessGroup:
    group_id: str = ""
    run_id: str = ""
    dim: str = "gender"
    group_name: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0
    gap_to_best: float = 0.0        # 与最好一组的差值（正值=该组吃亏）
    small_sample: bool = False
    note: str = ""

    def to_dict(self):
        return asdict(self)


# ---------------- 演练 ----------------
@dataclass
class Drill:
    drill_id: str = ""
    session: str = ""               # 考期（每考期 ≥1 次的分母口径）
    site: str = ""
    scenario: str = "alt_verify"    # config.DRILL_SCENARIOS
    channels: list = field(default_factory=list)   # 演练涉及的通道编码
    participants: int = 0
    planned_min: float = 0.0
    actual_min: float = 0.0
    issues: list = field(default_factory=list)     # 发现的问题
    actions: str = ""               # 整改措施
    result: str = "pass"            # config.DRILL_RESULTS
    witness: str = ""               # 演练见证/组织人
    operator: str = ""
    source: str = "manual"
    note: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)

    def to_dict(self):
        d = asdict(self)
        d["channels"] = list(self.channels or [])
        d["issues"] = list(self.issues or [])
        return d

    def scenario_text(self):
        import config
        return config.DRILL_SCENARIOS.get(self.scenario, self.scenario)

    def result_text(self):
        import config
        return config.DRILL_RESULT_TEXT.get(self.result, self.result)


# ---------------- 审计（哈希链） ----------------
@dataclass
class Audit:
    audit_id: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)
    action: str = ""
    detail: str = ""
    operator: str = ""
    prev_hash: str = ""
    this_hash: str = ""

    def to_dict(self):
        return asdict(self)


# ---------------- 联动外发队列（03 不可达时不丢事件） ----------------
@dataclass
class Outbox:
    msg_id: str = ""
    code: str = ""
    level: str = ""
    payload: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_ts)
    created_epoch: float = field(default_factory=now_epoch)
    tries: int = 0
    sent: bool = False
    sent_at: str = ""
    alert_id: str = ""
    last_error: str = ""

    def to_dict(self):
        d = asdict(self)
        d["payload"] = dict(self.payload or {})
        return d
