# -*- coding: utf-8 -*-
"""
10 普通考场+特殊考场应用 · 数据模型

全部为纯数据对象（dataclass），序列化只走 to_dict/from_row，
方便存储层做字段白名单，也方便集成接口直接吐 JSON。
"""
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

import config


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_epoch():
    return time.time()


def new_id(prefix):
    """短唯一号：前缀 + 毫秒 + 随机盐的哈希片段（跨进程也不会撞）。"""
    salt = ("%s|%s|%s" % (prefix, time.time(), id(object())))
    return "%s%s" % (prefix, hashlib.sha1(salt.encode("utf-8")).hexdigest()[:12])


def sha1(text):
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(text, default=None):
    if text is None or text == "":
        return default if default is not None else {}
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default if default is not None else {}


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# =============================================================================
# 审计（哈希链）
# =============================================================================
@dataclass
class AuditLog:
    audit_id: str = ""
    ts: str = ""
    epoch: float = 0.0
    action: str = ""
    detail: str = ""
    operator: str = ""
    room_id: str = ""
    prev_hash: str = ""
    this_hash: str = ""

    def to_dict(self):
        return {"audit_id": self.audit_id, "ts": self.ts, "action": self.action,
                "detail": self.detail, "operator": self.operator,
                "room_id": self.room_id, "prev_hash": self.prev_hash,
                "this_hash": self.this_hash}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(audit_id=d.get("audit_id") or "", ts=d.get("ts") or "",
                   epoch=to_float(d.get("epoch")), action=d.get("action") or "",
                   detail=d.get("detail") or "", operator=d.get("operator") or "",
                   room_id=d.get("room_id") or "",
                   prev_hash=d.get("prev_hash") or "",
                   this_hash=d.get("this_hash") or "")

    def compute_hash(self):
        raw = "|".join([self.audit_id, self.ts, self.action, self.detail,
                        self.operator, self.room_id, self.prev_hash])
        return sha1(raw)


# =============================================================================
# 考场
# =============================================================================
@dataclass
class Room:
    room_id: str = ""
    name: str = ""
    room_type: str = config.TYPE_STANDARD
    site: str = ""
    building: str = ""
    floor: str = ""
    seats: int = 30
    status: str = config.PHASE_APPLY
    session_name: str = ""
    session_state: str = config.SESSION_PRE
    apply_by: str = ""
    apply_reason: str = ""
    applied_at: str = ""
    template_applied: int = 0
    template_at: str = ""
    min_config: str = "full"            # full / reduced / minimal
    degrade_used: int = 0
    points: list = field(default_factory=list)      # 实际点位清单
    points_missing: list = field(default_factory=list)
    caps_present: list = field(default_factory=list)
    caps_missing: list = field(default_factory=list)
    caps_weak: list = field(default_factory=list)
    enabled_behaviors: list = field(default_factory=list)
    alert_policy: dict = field(default_factory=dict)
    invigilators: list = field(default_factory=list)     # [{name, role, at, remote}]
    invigilators_required: int = 2
    remote_invigilator: dict = field(default_factory=dict)  # 隔离考场：远程监考员在岗
    ledger_ready: int = 0               # 座位表/考生编号映射是否录入
    ledger_sample: list = field(default_factory=list)   # 抽样（仅编号，用于核查"不显姓名"）
    points_verified: int = 0            # 覆盖完整性是否逐机位核查留档
    calibration_at: str = ""            # 逐考场标定记录时间
    clock_drift_sec: float = 1.0
    data_domain: str = config.DOMAIN_EXAM
    offline_ready: int = 0              # 涉密：全离线自检是否通过
    fast_enable: int = 0                # 备用考场：设备预置即插即用是否已就绪
    doctor_link: int = 0                # 隔离考场：医生协同通道是否已建立
    network_log: int = 0                # 机考考场：网络行为日志代理是否已登记
    independent_mic: int = 0            # 口试考场：独立拾音是否已就绪
    dpia_done: int = 0
    drill_done: int = 0
    dedup_verified: int = 0
    screen_audit: int = 0
    audio_sample_rate: int = 0
    alt_channel: int = 0                # 非人脸替代核验通道是否具备
    last_check_at: str = ""
    last_check_ok: int = 0
    armed_at: str = ""
    closed_at: str = ""
    retention_days: int = config.DEFAULT_RETENTION_DAYS
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def type_meta(self):
        return config.ROOM_TYPES.get(self.room_type, config.ROOM_TYPES[config.TYPE_STANDARD])

    def type_name(self):
        return self.type_meta()["name"]

    def is_special(self):
        return bool(self.type_meta()["special"])

    def status_text(self):
        return config.PHASE_TEXT.get(self.status, self.status)

    def min_config_meta(self):
        return config.MIN_CONFIG_LEVELS.get(self.min_config,
                                            config.MIN_CONFIG_LEVELS["full"])

    def confidence_min(self):
        """本考场的置信度门槛：模板差异优先，其次告警策略基准。"""
        v = self.type_meta().get("params", {}).get("confidence_min")
        if v is None:
            v = (self.alert_policy or {}).get("confidence_min")
        if v is None:
            v = config.ALERT_POLICY["confidence_min"]
        return float(v)

    def armed(self):
        return self.status in (config.PHASE_ARMED, config.PHASE_DEGRADED)

    def to_dict(self):
        return {
            "room_id": self.room_id, "name": self.name, "room_type": self.room_type,
            "room_type_name": self.type_name(), "special": self.is_special(),
            "site": self.site, "building": self.building, "floor": self.floor,
            "seats": self.seats, "status": self.status,
            "status_text": self.status_text(), "session_name": self.session_name,
            "session_state": self.session_state,
            "session_state_text": config.SESSION_TEXT.get(self.session_state, ""),
            "apply_by": self.apply_by, "apply_reason": self.apply_reason,
            "applied_at": self.applied_at,
            "template_applied": bool(self.template_applied),
            "template_at": self.template_at,
            "min_config": self.min_config,
            "min_config_name": self.min_config_meta()["name"],
            "degrade_used": bool(self.degrade_used),
            "points": self.points, "points_missing": self.points_missing,
            "caps_present": self.caps_present, "caps_missing": self.caps_missing,
            "caps_weak": self.caps_weak,
            "enabled_behaviors": self.enabled_behaviors,
            "enabled_behavior_count": len(self.enabled_behaviors),
            "alert_policy": self.alert_policy,
            "confidence_min": self.confidence_min(),
            "invigilators": self.invigilators,
            "invigilators_required": self.invigilators_required,
            "invigilator_onsite": self.invigilator_count(),
            "invigilator_total": self.invigilator_total(),
            "remote_invigilator": self.remote_invigilator,
            "remote_onpost": bool(self.remote_invigilator),
            "ledger_ready": bool(self.ledger_ready),
            "ledger_sample": self.ledger_sample,
            "points_verified": bool(self.points_verified),
            "calibration_at": self.calibration_at,
            "clock_drift_sec": self.clock_drift_sec,
            "data_domain": self.data_domain,
            "data_domain_name": config.DOMAIN_RULES.get(self.data_domain, {}).get("name", ""),
            "offline_ready": bool(self.offline_ready),
            "fast_enable": bool(self.fast_enable),
            "doctor_link": bool(self.doctor_link),
            "network_log": bool(self.network_log),
            "independent_mic": bool(self.independent_mic),
            "dpia_done": bool(self.dpia_done),
            "drill_done": bool(self.drill_done),
            "dedup_verified": bool(self.dedup_verified),
            "screen_audit": bool(self.screen_audit),
            "audio_sample_rate": self.audio_sample_rate,
            "alt_channel": bool(self.alt_channel),
            "last_check_at": self.last_check_at,
            "last_check_ok": bool(self.last_check_ok),
            "armed_at": self.armed_at, "closed_at": self.closed_at,
            "retention_days": self.retention_days, "note": self.note,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def invigilator_count(self):
        return len([x for x in self.invigilators if not x.get("remote")])

    def invigilator_total(self):
        return len(self.invigilators)

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        r = cls(
            room_id=d.get("room_id") or "", name=d.get("name") or "",
            room_type=d.get("room_type") or config.TYPE_STANDARD,
            site=d.get("site") or "", building=d.get("building") or "",
            floor=d.get("floor") or "", seats=to_int(d.get("seats"), 30),
            status=d.get("status") or config.PHASE_APPLY,
            session_name=d.get("session_name") or "",
            session_state=d.get("session_state") or config.SESSION_PRE,
            apply_by=d.get("apply_by") or "", apply_reason=d.get("apply_reason") or "",
            applied_at=d.get("applied_at") or "",
            template_applied=to_int(d.get("template_applied")),
            template_at=d.get("template_at") or "",
            min_config=d.get("min_config") or "full",
            degrade_used=to_int(d.get("degrade_used")),
            points=jload(d.get("points"), []),
            points_missing=jload(d.get("points_missing"), []),
            caps_present=jload(d.get("caps_present"), []),
            caps_missing=jload(d.get("caps_missing"), []),
            caps_weak=jload(d.get("caps_weak"), []),
            enabled_behaviors=jload(d.get("enabled_behaviors"), []),
            alert_policy=jload(d.get("alert_policy"), {}),
            invigilators=jload(d.get("invigilators"), []),
            invigilators_required=to_int(d.get("invigilators_required"), 2),
            remote_invigilator=jload(d.get("remote_invigilator"), {}),
            ledger_ready=to_int(d.get("ledger_ready")),
            ledger_sample=jload(d.get("ledger_sample"), []),
            points_verified=to_int(d.get("points_verified")),
            calibration_at=d.get("calibration_at") or "",
            clock_drift_sec=to_float(d.get("clock_drift_sec"), 1.0),
            data_domain=d.get("data_domain") or config.DOMAIN_EXAM,
            offline_ready=to_int(d.get("offline_ready")),
            fast_enable=to_int(d.get("fast_enable")),
            doctor_link=to_int(d.get("doctor_link")),
            network_log=to_int(d.get("network_log")),
            independent_mic=to_int(d.get("independent_mic")),
            dpia_done=to_int(d.get("dpia_done")),
            drill_done=to_int(d.get("drill_done")),
            dedup_verified=to_int(d.get("dedup_verified")),
            screen_audit=to_int(d.get("screen_audit")),
            audio_sample_rate=to_int(d.get("audio_sample_rate")),
            alt_channel=to_int(d.get("alt_channel")),
            last_check_at=d.get("last_check_at") or "",
            last_check_ok=to_int(d.get("last_check_ok")),
            armed_at=d.get("armed_at") or "", closed_at=d.get("closed_at") or "",
            retention_days=to_int(d.get("retention_days"), config.DEFAULT_RETENTION_DAYS),
            note=d.get("note") or "", created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "")
        return r


# =============================================================================
# 准入自检
# =============================================================================
@dataclass
class Defect:
    code: str = ""
    name: str = ""
    severity: str = config.SEV_WARN
    detail: str = ""
    point: str = ""

    def to_dict(self):
        return {"code": self.code, "name": self.name or config.defect_name(self.code),
                "severity": self.severity, "detail": self.detail, "point": self.point}

    @classmethod
    def make(cls, code, detail="", point=""):
        return cls(code=code, name=config.defect_name(code),
                   severity=config.defect_sev(code), detail=detail, point=point)

    @classmethod
    def from_dict(cls, d):
        return cls(code=d.get("code") or "", name=d.get("name") or "",
                   severity=d.get("severity") or config.SEV_WARN,
                   detail=d.get("detail") or "", point=d.get("point") or "")


@dataclass
class PointCheck:
    no: str = ""
    device_code: str = ""
    ok: int = 0
    defects: list = field(default_factory=list)     # [{code, severity, name}]
    factor: float = 1.0
    source: str = ""
    detail: str = ""

    def to_dict(self):
        return {"no": self.no, "device_code": self.device_code, "ok": bool(self.ok),
                "defects": self.defects, "factor": self.factor,
                "source": self.source, "detail": self.detail}

    @classmethod
    def from_dict(cls, d):
        return cls(no=d.get("no") or "", device_code=d.get("device_code") or "",
                   ok=to_int(d.get("ok")), defects=d.get("defects") or [],
                   factor=to_float(d.get("factor"), 1.0),
                   source=d.get("source") or "", detail=d.get("detail") or "")


@dataclass
class CheckRun:
    check_id: str = ""
    room_id: str = ""
    room_name: str = ""
    room_type: str = ""
    phase: str = config.PHASE_SELFCHECK
    operator: str = ""
    ok: int = 0
    blocked: int = 0
    defects: list = field(default_factory=list)      # [Defect 序列化]
    point_checks: list = field(default_factory=list)
    factor: float = 1.0
    passed: list = field(default_factory=list)        # 通过的检查项文字
    min_config: str = "full"
    note: str = ""
    ts: str = ""
    epoch: float = 0.0

    def to_dict(self):
        return {"check_id": self.check_id, "room_id": self.room_id,
                "room_name": self.room_name, "room_type": self.room_type,
                "room_type_name": config.type_name(self.room_type),
                "phase": self.phase, "operator": self.operator,
                "ok": bool(self.ok), "blocked": bool(self.blocked),
                "defects": self.defects, "point_checks": self.point_checks,
                "factor": self.factor, "passed": self.passed,
                "min_config": self.min_config,
                "min_config_name": config.MIN_CONFIG_LEVELS.get(
                    self.min_config, {}).get("name", self.min_config),
                "note": self.note, "ts": self.ts, "epoch": self.epoch}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(check_id=d.get("check_id") or "", room_id=d.get("room_id") or "",
                   room_name=d.get("room_name") or "",
                   room_type=d.get("room_type") or "",
                   phase=d.get("phase") or "", operator=d.get("operator") or "",
                   ok=to_int(d.get("ok")), blocked=to_int(d.get("blocked")),
                   defects=jload(d.get("defects"), []),
                   point_checks=jload(d.get("point_checks"), []),
                   factor=to_float(d.get("factor"), 1.0),
                   passed=jload(d.get("passed"), []),
                   min_config=d.get("min_config") or "full",
                   note=d.get("note") or "", ts=d.get("ts") or "",
                   epoch=to_float(d.get("epoch")))


# =============================================================================
# 场次与 KPI
# =============================================================================
@dataclass
class Session:
    session_id: str = ""
    room_id: str = ""
    room_name: str = ""
    room_type: str = ""
    name: str = ""
    subject: str = ""
    starts_at: str = ""
    ends_at: str = ""
    state: str = config.SESSION_PRE
    planned_seats: int = 0
    actual_seats: int = 0
    operator: str = ""
    note: str = ""
    created_at: str = ""
    epoch: float = 0.0

    def to_dict(self):
        return {"session_id": self.session_id, "room_id": self.room_id,
                "room_name": self.room_name, "room_type": self.room_type,
                "room_type_name": config.type_name(self.room_type),
                "name": self.name, "subject": self.subject,
                "starts_at": self.starts_at, "ends_at": self.ends_at,
                "state": self.state,
                "state_text": config.SESSION_TEXT.get(self.state, self.state),
                "planned_seats": self.planned_seats,
                "actual_seats": self.actual_seats, "operator": self.operator,
                "note": self.note, "created_at": self.created_at,
                "epoch": self.epoch}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(session_id=d.get("session_id") or "",
                   room_id=d.get("room_id") or "",
                   room_name=d.get("room_name") or "",
                   room_type=d.get("room_type") or "", name=d.get("name") or "",
                   subject=d.get("subject") or "",
                   starts_at=d.get("starts_at") or "",
                   ends_at=d.get("ends_at") or "", state=d.get("state") or "",
                   planned_seats=to_int(d.get("planned_seats")),
                   actual_seats=to_int(d.get("actual_seats")),
                   operator=d.get("operator") or "", note=d.get("note") or "",
                   created_at=d.get("created_at") or "",
                   epoch=to_float(d.get("epoch")))


@dataclass
class KpiRun:
    """一次验收指标测算。

    证据口径：**只认实测样本**。合成/演示数据用 data_source 标注，
    并计入 notes，绝不冒充考场实测（与全仓库"如实说明"口径一致）。
    """
    run_id: str = ""
    room_id: str = ""
    room_name: str = ""
    room_type: str = ""
    session_id: str = ""
    operator: str = ""
    data_source: str = "manual"        # real / synthetic / manual / replay
    observe_total: int = 0             # 人工标注的异常样本总数（分母）
    observe_hit: int = 0               # 系统提示到的（分子）-> 提示率
    observe_correct: int = 0           # 行为类别判对的 -> 准确率
    observe_false: int = 0             # 报错了的 -> 误报率
    response_sec: float = 0.0
    camera_online_rate: float = 0.0
    coverage_rate: float = 0.0
    dedup_expected: int = 0            # 跨机位实际人数（真值）
    dedup_counted: int = 0             # 系统去重后计数
    clock_drift_sec: float = 0.0
    invigilator_onpost: int = 0
    invigilator_total: int = 0
    alt_channel: int = 0
    alt_channel_drill: int = 0
    domain_isolation: int = 0
    offline_ready: int = 0
    min_config: int = 0
    metrics: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    ts: str = ""
    epoch: float = 0.0

    def to_dict(self):
        return {"run_id": self.run_id, "room_id": self.room_id,
                "room_name": self.room_name, "room_type": self.room_type,
                "room_type_name": config.type_name(self.room_type),
                "session_id": self.session_id, "operator": self.operator,
                "data_source": self.data_source,
                "data_source_text": DATA_SOURCE_TEXT.get(self.data_source,
                                                         self.data_source),
                "observe_total": self.observe_total,
                "observe_hit": self.observe_hit,
                "observe_correct": self.observe_correct,
                "observe_false": self.observe_false,
                "response_sec": self.response_sec,
                "camera_online_rate": self.camera_online_rate,
                "coverage_rate": self.coverage_rate,
                "dedup_expected": self.dedup_expected,
                "dedup_counted": self.dedup_counted,
                "clock_drift_sec": self.clock_drift_sec,
                "invigilator_onpost": self.invigilator_onpost,
                "invigilator_total": self.invigilator_total,
                "alt_channel": bool(self.alt_channel),
                "alt_channel_drill": bool(self.alt_channel_drill),
                "domain_isolation": bool(self.domain_isolation),
                "offline_ready": bool(self.offline_ready),
                "min_config": bool(self.min_config),
                "metrics": self.metrics, "notes": self.notes,
                "ts": self.ts, "epoch": self.epoch}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(run_id=d.get("run_id") or "", room_id=d.get("room_id") or "",
                   room_name=d.get("room_name") or "",
                   room_type=d.get("room_type") or "",
                   session_id=d.get("session_id") or "",
                   operator=d.get("operator") or "",
                   data_source=d.get("data_source") or "manual",
                   observe_total=to_int(d.get("observe_total")),
                   observe_hit=to_int(d.get("observe_hit")),
                   observe_correct=to_int(d.get("observe_correct")),
                   observe_false=to_int(d.get("observe_false")),
                   response_sec=to_float(d.get("response_sec")),
                   camera_online_rate=to_float(d.get("camera_online_rate")),
                   coverage_rate=to_float(d.get("coverage_rate")),
                   dedup_expected=to_int(d.get("dedup_expected")),
                   dedup_counted=to_int(d.get("dedup_counted")),
                   clock_drift_sec=to_float(d.get("clock_drift_sec")),
                   invigilator_onpost=to_int(d.get("invigilator_onpost")),
                   invigilator_total=to_int(d.get("invigilator_total")),
                   alt_channel=to_int(d.get("alt_channel")),
                   alt_channel_drill=to_int(d.get("alt_channel_drill")),
                   domain_isolation=to_int(d.get("domain_isolation")),
                   offline_ready=to_int(d.get("offline_ready")),
                   min_config=to_int(d.get("min_config")),
                   metrics=jload(d.get("metrics"), {}),
                   notes=jload(d.get("notes"), []),
                   ts=d.get("ts") or "", epoch=to_float(d.get("epoch")))


DATA_SOURCE_TEXT = {
    "real": "考场实测",
    "synthetic": "合成/演示数据",
    "manual": "人工录入",
    "replay": "历史回放",
}


# =============================================================================
# 行为与告警
# =============================================================================
@dataclass
class BehaviorDef:
    code: str = ""
    name: str = ""
    category: str = config.CAT_INDIVIDUAL
    level: str = config.LEVEL_ORANGE
    signals: list = field(default_factory=list)
    enabled: int = 1
    rooms_off: list = field(default_factory=list)   # 明确不适用的考场类型

    def to_dict(self):
        return {"code": self.code, "name": self.name, "category": self.category,
                "category_name": config.CATEGORIES.get(self.category, self.category),
                "level": self.level, "level_name": config.LEVEL_META.get(
                    self.level, {}).get("name", self.level),
                "signals": self.signals, "enabled": bool(self.enabled),
                "rooms_off": self.rooms_off,
                "alert_code": config.BEHAVIOR_ALERT_CODE.get(self.code, ""),
                "moe": config.MOE_CLASS_MAP.get(self.category, "")}

    @classmethod
    def from_tuple(cls, t):
        return cls(code=t[0], name=t[1], category=t[2], level=t[3],
                   signals=list(t[4]))

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(code=d.get("code") or "", name=d.get("name") or "",
                   category=d.get("category") or "", level=d.get("level") or "",
                   signals=jload(d.get("signals"), []),
                   enabled=to_int(d.get("enabled"), 1),
                   rooms_off=jload(d.get("rooms_off"), []))


@dataclass
class AlertEvent:
    """一次告警的路由与处置（本模块负责分级、抑制、分流；留痕由 03 兜底）。"""
    event_id: str = ""
    room_id: str = ""
    room_name: str = ""
    room_type: str = ""
    session_id: str = ""
    behavior: str = ""
    behavior_name: str = ""
    category: str = ""
    alert_code: str = ""
    level: str = config.LEVEL_ORANGE
    confidence: float = 0.0
    device_code: str = ""
    zone: str = ""
    seat_no: str = ""
    candidate_id: str = ""
    detail: str = ""
    delivered: int = 0
    suppressed: int = 0
    suppress_reason: str = ""
    pushed_to: list = field(default_factory=list)
    response_text: str = ""
    sla_sec: int = 0
    source: str = "10_room"
    ts: str = ""
    epoch: float = 0.0
    external: dict = field(default_factory=dict)     # 03 返回的 alert_id 等

    def to_dict(self):
        return {"event_id": self.event_id, "room_id": self.room_id,
                "room_name": self.room_name, "room_type": self.room_type,
                "room_type_name": config.type_name(self.room_type),
                "session_id": self.session_id, "behavior": self.behavior,
                "behavior_name": self.behavior_name, "category": self.category,
                "category_name": config.CATEGORIES.get(self.category, self.category),
                "alert_code": self.alert_code, "level": self.level,
                "level_name": config.LEVEL_META.get(self.level, {}).get("name", self.level),
                "confidence": self.confidence, "device_code": self.device_code,
                "zone": self.zone, "seat_no": self.seat_no,
                "candidate_id": config.mask_id(self.candidate_id) if self.candidate_id else "",
                "detail": self.detail, "delivered": bool(self.delivered),
                "suppressed": bool(self.suppressed),
                "suppress_reason": self.suppress_reason,
                "pushed_to": self.pushed_to, "response_text": self.response_text,
                "sla_sec": self.sla_sec, "source": self.source,
                "ts": self.ts, "epoch": self.epoch, "external": self.external}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(event_id=d.get("event_id") or "", room_id=d.get("room_id") or "",
                   room_name=d.get("room_name") or "",
                   room_type=d.get("room_type") or "",
                   session_id=d.get("session_id") or "",
                   behavior=d.get("behavior") or "",
                   behavior_name=d.get("behavior_name") or "",
                   category=d.get("category") or "",
                   alert_code=d.get("alert_code") or "",
                   level=d.get("level") or config.LEVEL_ORANGE,
                   confidence=to_float(d.get("confidence")),
                   device_code=d.get("device_code") or "",
                   zone=d.get("zone") or "", seat_no=d.get("seat_no") or "",
                   candidate_id=d.get("candidate_id") or "",
                   detail=d.get("detail") or "",
                   delivered=to_int(d.get("delivered")),
                   suppressed=to_int(d.get("suppressed")),
                   suppress_reason=d.get("suppress_reason") or "",
                   pushed_to=jload(d.get("pushed_to"), []),
                   response_text=d.get("response_text") or "",
                   sla_sec=to_int(d.get("sla_sec")), source=d.get("source") or "",
                   ts=d.get("ts") or "", epoch=to_float(d.get("epoch")),
                   external=jload(d.get("external"), {}))


# =============================================================================
# 数据分域与合规
# =============================================================================
@dataclass
class Partition:
    partition_id: str = ""
    domain: str = config.DOMAIN_EXAM
    room_id: str = ""
    room_name: str = ""
    state: str = config.PARTITION_PENDING
    isolated: int = 1
    encrypted: int = 0
    path: str = ""
    access_roles: list = field(default_factory=list)
    retention_days: int = config.DEFAULT_RETENTION_DAYS
    destroy_at: str = ""
    destroyed_at: str = ""
    destroy_operator: str = ""
    created_at: str = ""
    note: str = ""

    def to_dict(self):
        rules = config.DOMAIN_RULES.get(self.domain, {})
        return {"partition_id": self.partition_id, "domain": self.domain,
                "domain_name": rules.get("name", self.domain),
                "sensitivity": rules.get("sensitivity", ""),
                "desc": rules.get("desc", ""),
                "room_id": self.room_id, "room_name": self.room_name,
                "state": self.state, "isolated": bool(self.isolated),
                "encrypted": bool(self.encrypted),
                "allow_cross_aggregate": bool(rules.get("allow_cross_aggregate")),
                "path": self.path, "access_roles": self.access_roles,
                "retention_days": self.retention_days,
                "destroy_at": self.destroy_at, "destroyed_at": self.destroyed_at,
                "destroy_operator": self.destroy_operator,
                "created_at": self.created_at, "note": self.note}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(partition_id=d.get("partition_id") or "",
                   domain=d.get("domain") or config.DOMAIN_EXAM,
                   room_id=d.get("room_id") or "", room_name=d.get("room_name") or "",
                   state=d.get("state") or config.PARTITION_PENDING,
                   isolated=to_int(d.get("isolated"), 1),
                   encrypted=to_int(d.get("encrypted")),
                   path=d.get("path") or "",
                   access_roles=jload(d.get("access_roles"), []),
                   retention_days=to_int(d.get("retention_days"),
                                         config.DEFAULT_RETENTION_DAYS),
                   destroy_at=d.get("destroy_at") or "",
                   destroyed_at=d.get("destroyed_at") or "",
                   destroy_operator=d.get("destroy_operator") or "",
                   created_at=d.get("created_at") or "", note=d.get("note") or "")


@dataclass
class Grant:
    grant_id: str = ""
    domain: str = ""
    role: str = ""
    operator: str = ""
    granted_by: str = ""
    revoked: int = 0
    reason: str = ""
    ts: str = ""
    epoch: float = 0.0

    def to_dict(self):
        return {"grant_id": self.grant_id, "domain": self.domain,
                "domain_name": config.DOMAIN_RULES.get(self.domain, {}).get("name", ""),
                "role": self.role, "operator": self.operator,
                "granted_by": self.granted_by, "revoked": bool(self.revoked),
                "reason": self.reason, "ts": self.ts}

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(grant_id=d.get("grant_id") or "", domain=d.get("domain") or "",
                   role=d.get("role") or "", operator=d.get("operator") or "",
                   granted_by=d.get("granted_by") or "",
                   revoked=to_int(d.get("revoked")), reason=d.get("reason") or "",
                   ts=d.get("ts") or "", epoch=to_float(d.get("epoch")))


@dataclass
class PrivacyRecord:
    record_id: str = ""
    action: str = ""
    room_id: str = ""
    room_name: str = ""
    domain: str = ""
    operator: str = ""
    witness: str = ""
    result: str = ""
    detail: str = ""
    ts: str = ""
    epoch: float = 0.0

    def to_dict(self):
        return {"record_id": self.record_id, "action": self.action,
                "action_name": config.PRIVACY_ACTIONS.get(self.action, self.action),
                "room_id": self.room_id, "room_name": self.room_name,
                "domain": self.domain,
                "domain_name": config.DOMAIN_RULES.get(self.domain, {}).get("name", ""),
                "operator": self.operator, "witness": self.witness,
                "result": self.result, "detail": self.detail, "ts": self.ts}

    def action_text(self):
        return config.PRIVACY_ACTIONS.get(self.action, self.action)

    @classmethod
    def from_row(cls, row):
        d = dict(row)
        return cls(record_id=d.get("record_id") or "", action=d.get("action") or "",
                   room_id=d.get("room_id") or "", room_name=d.get("room_name") or "",
                   domain=d.get("domain") or "", operator=d.get("operator") or "",
                   witness=d.get("witness") or "", result=d.get("result") or "",
                   detail=d.get("detail") or "", ts=d.get("ts") or "",
                   epoch=to_float(d.get("epoch")))

