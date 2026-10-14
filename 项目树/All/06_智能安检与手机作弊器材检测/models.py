# -*- coding: utf-8 -*-
"""
06 智能安检与手机作弊器材检测 · 数据模型

一律用 dataclass，字段与 storage.py 的表一一对应；
所有时间字段都是**字符串**（'YYYY-MM-DD HH:MM:SS'）便于直接入 SQLite 与报表，
同时保留 *_epoch 便于统计窗口计算。
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
    """
    短 ID：前缀 + 时分秒 + 6 位随机（hex）。便于人工口头报号。

    随机位用 **6 位 hex** 而不是 4 位数字：同一秒内连续落库的几条记录（例如一次现场
    实测逐样本录入）在 4 位数字下会偶发撞号，撞号会被 INSERT OR REPLACE 覆盖 ——
    表现为"少了一条记录"。这类偶发丢数据必须从 ID 生成上根除。
    """
    return "%s%s%s" % (prefix, time.strftime("%H%M%S"), uuid.uuid4().hex[:6].upper())


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


# ---------------- 设备（安检门 / 手持探测器 / 储物柜 / 鞋底仪） ----------------
@dataclass
class Device:
    device_id: str = ""
    code: str = ""                      # 设备编码，如 GATE-A01
    name: str = ""
    kind: str = "gate"                  # config.DEV_*
    site: str = ""                      # 考点/楼栋
    channel_no: str = ""                # 通道号（第几号入口）
    room_id: str = ""                   # 关联考场（可选）
    proto: str = "sim"                  # config.PROTOS
    address: str = ""                   # http://ip:port / ip:port / COM3
    sensitivity: float = 1.0            # 灵敏度倍数（1.0=按全局阈值）
    enabled: bool = True
    status: str = "offline"             # online / offline
    last_heartbeat: str = ""
    note: str = ""
    created_at: str = field(default_factory=now_ts)

    def to_dict(self):
        return asdict(self)


# ---------------- 过检记录（一次通过安检门/鞋底仪） ----------------
@dataclass
class PassEvent:
    event_id: str = ""
    device_code: str = ""
    channel_no: str = ""
    room_id: str = ""
    candidate_id: str = ""              # 身份证/准考证（可掩码）
    candidate_name: str = ""
    seat_no: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)
    result: str = "clear"               # clear / alarm
    kinds: list = field(default_factory=list)      # 报警类别 [mobile, metal, ...]
    levels: list = field(default_factory=list)     # 每类对应级别 red / orange
    scores: dict = field(default_factory=dict)     # 六路感应原始强度
    duration_ms: int = 0                # 通过耗时（效率统计）
    operator: str = ""
    source: str = "manual"              # real / sim / manual（**如实标注**）
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        d["kinds"] = list(self.kinds or [])
        d["levels"] = list(self.levels or [])
        return d

    def kinds_text(self):
        import config
        return "、".join(config.KIND_TEXT.get(k, k) for k in (self.kinds or [])) or "无"


# ---------------- 报警（由过检记录派生，进复检队列） ----------------
ST_ALARM_PENDING = "pending"            # 待复检
ST_ALARM_REINSPECT = "reinspecting"     # 复检中
ST_ALARM_CLOSED = "closed"              # 已处置
ST_ALARM_ESCALATED = "escalated"        # 已上报 03 智能终端
ALARM_STATES = (ST_ALARM_PENDING, ST_ALARM_REINSPECT, ST_ALARM_CLOSED, ST_ALARM_ESCALATED)
ALARM_STATE_TEXT = {
    ST_ALARM_PENDING: "待复检",
    ST_ALARM_REINSPECT: "复检中",
    ST_ALARM_CLOSED: "已处置",
    ST_ALARM_ESCALATED: "已上报终端",
}

LEVEL_RED = "red"
LEVEL_ORANGE = "orange"
LEVEL_TEXT = {LEVEL_RED: "红色报警", LEVEL_ORANGE: "橙色报警"}

# 报警类别 -> 上报 03 智能终端的告警码
ALERT_CODE_BY_KIND = {
    "mobile": "PHONE_DETECTED",
    "earphone": "PHONE_DETECTED",
    "metal": "SECURITY_ALARM",
    "magnetic": "SECURITY_ALARM",
    "shoe": "SECURITY_ALARM",
}
# 储物柜异常 -> 03
ALERT_CODE_LOCKER = "LOCKER_ABNORMAL"
# 复检超时未处置 -> 03
ALERT_CODE_TIMEOUT = "SECURITY_REINSPECT_TIMEOUT"
# 特殊考生转人工通道 -> 03
ALERT_CODE_SPECIAL = "SECURITY_SPECIAL_CHANNEL"


@dataclass
class Alarm:
    alarm_id: str = ""
    event_id: str = ""
    device_code: str = ""
    channel_no: str = ""
    room_id: str = ""
    kind: str = "mobile"
    kinds: list = field(default_factory=list)
    level: str = LEVEL_ORANGE
    detail: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    status: str = ST_ALARM_PENDING
    created_at: str = field(default_factory=now_ts)
    created_epoch: float = field(default_factory=now_epoch)
    closed_at: str = ""
    reported: bool = False              # 是否已上报 03
    reported_at: str = ""
    report_reason: str = ""

    def to_dict(self):
        d = asdict(self)
        d["kinds"] = list(self.kinds or [])
        return d

    def level_text(self):
        return LEVEL_TEXT.get(self.level, self.level)

    def kinds_text(self):
        import config
        return "、".join(config.KIND_TEXT.get(k, k) for k in (self.kinds or [self.kind]))

    def overdue(self, sla=None):
        import config
        if self.status == ST_ALARM_CLOSED:
            return False
        return (now_epoch() - self.created_epoch) > float(sla or config.REINSPECT_SLA_SEC)


# ---------------- 复检与处置 ----------------
@dataclass
class Reinspect:
    reinspect_id: str = ""
    alarm_id: str = ""
    event_id: str = ""
    operator: str = ""                  # 主检人
    witness: str = ""                   # 见证人（特殊考生人工通道要双人）
    method: str = "handheld"            # config.REINSPECT_METHODS
    handheld_code: str = ""             # 用了哪台手持探测器
    result: str = ""                    # config.REINSPECT_RESULTS
    found_kind: str = ""                # 实际查获的类别（可空）
    found_detail: str = ""              # 查获物描述（型号/数量）
    storage_cell: str = ""              # 暂扣后放进哪个保管柜格
    started_epoch: float = field(default_factory=now_epoch)
    finished_epoch: float = 0.0
    started_at: str = field(default_factory=now_ts)
    finished_at: str = ""
    note: str = ""

    def to_dict(self):
        return asdict(self)

    def cost_sec(self):
        if not self.finished_epoch:
            return max(0.0, now_epoch() - self.started_epoch)
        return max(0.0, self.finished_epoch - self.started_epoch)

    def result_text(self):
        import config
        return config.REINSPECT_RESULTS.get(self.result, self.result or "未处置")


# ---------------- 智能储物柜 ----------------
@dataclass
class Locker:
    locker_id: str = ""
    code: str = ""
    name: str = ""
    site: str = ""
    cells_total: int = 20
    address: str = ""
    proto: str = "manual"
    enabled: bool = True
    status: str = "offline"
    note: str = ""
    created_at: str = field(default_factory=now_ts)

    def to_dict(self):
        return asdict(self)


@dataclass
class Cell:
    cell_id: str = ""
    locker_id: str = ""
    locker_code: str = ""
    cell_no: str = ""                   # 柜格号，如 A-07
    state: str = "free"
    candidate_id: str = ""
    candidate_name: str = ""
    ticket_no: str = ""
    seat_no: str = ""
    device_desc: str = ""               # 手机型号/数量（存进去的是什么）
    event_id: str = ""                  # 关联的安检过检记录（人-物-时闭环）
    stored_at: str = ""
    stored_epoch: float = 0.0
    taken_at: str = ""
    taken_epoch: float = 0.0
    method: str = ""                    # 最近一次开柜方式
    overdue_notified: bool = False
    note: str = ""

    def to_dict(self):
        return asdict(self)

    def state_text(self):
        import config
        return config.CELL_STATE_TEXT.get(self.state, self.state)

    def overdue(self, minutes=None):
        import config
        if self.state != "occupied" or not self.stored_epoch:
            return False
        return (now_epoch() - self.stored_epoch) > float(
            minutes or config.LOCKER_OVERDUE_MIN) * 60


@dataclass
class LockerLog:
    log_id: str = ""
    locker_code: str = ""
    cell_no: str = ""
    action: str = "store"               # config.LOCKER_LOG_ACTIONS
    method: str = "manual"
    operator: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    device_desc: str = ""
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)
    note: str = ""

    def to_dict(self):
        return asdict(self)

    def action_text(self):
        import config
        return config.LOCKER_ACTION_TEXT.get(self.action, self.action)


# ---------------- 特殊考生人工安检通道 ----------------
@dataclass
class SpecialRecord:
    record_id: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    ticket_no: str = ""
    room_id: str = ""
    seat_no: str = ""
    reason: str = "pregnant"            # config.SPECIAL_REASONS
    inspector: str = ""                 # 主检人
    witness: str = ""                   # 见证人（双人见证）
    method: str = "patdown"             # 人工手检 / 手持探测器
    result: str = "passed"              # config.SPECIAL_RESULTS
    detail: str = ""
    decision: str = ""                  # 替代方式说明（如"手检+随身物品目视"）
    ts: str = field(default_factory=now_ts)
    epoch: float = field(default_factory=now_epoch)
    drill: bool = False                 # True=演练记录（验收证据里的演练）

    def to_dict(self):
        return asdict(self)

    def reason_text(self):
        import config
        return config.SPECIAL_REASONS.get(self.reason, self.reason)

    def result_text(self):
        import config
        return config.SPECIAL_RESULTS.get(self.result, self.result)


# ---------------- 入场流程（安检-核验-签到三点位串联） ----------------
@dataclass
class FlowRecord:
    flow_id: str = ""
    candidate_id: str = ""
    candidate_name: str = ""
    ticket_no: str = ""
    room_id: str = ""
    seat_no: str = ""
    security_at: str = ""
    security_epoch: float = 0.0
    verify_at: str = ""
    verify_epoch: float = 0.0
    signin_at: str = ""
    signin_epoch: float = 0.0
    security_event: str = ""            # 关联过检记录
    reentries: int = 0                  # 重复排队次数
    note: str = ""

    def to_dict(self):
        return asdict(self)

    def stage_epochs(self):
        return {"security": self.security_epoch, "verify": self.verify_epoch,
                "signin": self.signin_epoch}

    def done_stages(self):
        return [k for k, v in self.stage_epochs().items() if v]

    def total_sec(self):
        """从安检完成到签到完成的耗时（秒）；未完成为 0。"""
        if self.security_epoch and self.signin_epoch:
            return max(0.0, self.signin_epoch - self.security_epoch)
        return 0.0

    def stage_costs(self):
        """各点位耗时：安检=到核验的间隔，核验=到签到的间隔。"""
        out = {}
        if self.security_epoch and self.verify_epoch:
            out["security"] = self.verify_epoch - self.security_epoch
        if self.verify_epoch and self.signin_epoch:
            out["verify"] = self.signin_epoch - self.verify_epoch
        return out


# ---------------- 样本库与验收测试 ----------------
@dataclass
class Sample:
    sample_id: str = ""
    name: str = ""
    kind: str = "mobile"                # 期望类别；"harmless"=无害物，"implant"=植入医疗器械
    expect_alarm: bool = True           # 期望报警（ground truth）
    scores: dict = field(default_factory=dict)   # 记录下来的六路感应强度（回放用）
    note: str = ""
    builtin: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass
class TestRun:
    run_id: str = ""
    mode: str = "replay"                # replay=判据回放 / field=现场实测录入
    site: str = ""
    operator: str = ""
    started_at: str = field(default_factory=now_ts)
    finished_at: str = ""
    note: str = ""
    total: int = 0
    hit: int = 0                        # 应报且报了
    miss: int = 0                       # 应报没报（漏检）
    false_alarm: int = 0                # 不该报却报了（误报）
    correct_clear: int = 0              # 不该报也没报
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class TestItem:
    item_id: str = ""
    run_id: str = ""
    sample_id: str = ""
    sample_name: str = ""
    kind: str = ""
    expect_alarm: bool = True
    got_alarm: bool = False
    got_kinds: list = field(default_factory=list)
    level: str = ""
    verdict: str = ""                   # hit / miss / false_alarm / correct_clear
    duration_ms: int = 0
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        d["kinds"] = list(self.got_kinds or [])
        return d


# ---------------- 审计 ----------------
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
