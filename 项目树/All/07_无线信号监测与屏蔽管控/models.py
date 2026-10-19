# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 数据模型

设计原则（与 06/10 一致，便于整个项目统一验收）：
  · 全部用 dataclass 表达，字段直白、能一眼对应到《…监测与屏蔽管控.md》里的业务；
  · 时间统一：`*_epoch` 是 float 秒（便于算时延/一致性），`*_at` / `ts` 是可读字符串；
  · 列表/字典字段落库时存 JSON（JSON_FIELDS 声明），读出时还原，不丢结构；
  · 任何模型都能 `to_dict()`，界面、接口、报告共用同一种表示，不各写一套。
"""
import dataclasses
import json
import random
import time
from dataclasses import dataclass, field

DATE_FMT = "%Y-%m-%d"
TS_FMT = "%Y-%m-%d %H:%M:%S"
HHMM_FMT = "%H:%M"


# =============================================================================
# 时间与标识
# =============================================================================
def now_epoch():
    return time.time()


def now_ts():
    return time.strftime(TS_FMT, time.localtime())


def today_str():
    return time.strftime(DATE_FMT, time.localtime())


def date_str(epoch=None):
    return time.strftime(DATE_FMT, time.localtime(epoch if epoch is not None else now_epoch()))


def ts_text(epoch=None):
    return time.strftime(TS_FMT, time.localtime(epoch if epoch is not None else now_epoch()))


def hhmm_text(epoch=None):
    return time.strftime(HHMM_FMT, time.localtime(epoch if epoch is not None else now_epoch()))


def new_id(prefix):
    """短、可读、碰撞概率低的 ID：前缀 + 毫秒时间 + 3 位随机。"""
    return "%s%13d%03d" % (prefix, int(now_epoch() * 1000), random.randint(0, 999))


def parse_hhmm(text):
    """'09:00' / '9:00' / '0900' -> 当天秒数；非法返回 None。"""
    s = str(text or "").strip().replace("：", ":")
    if not s:
        return None
    try:
        if ":" in s:
            hh, mm = s.split(":", 1)
            h, m = int(hh), int(mm)
        elif len(s) == 4 and s.isdigit():
            h, m = int(s[:2]), int(s[2:])
        else:
            return None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 3600 + m * 60
    except (TypeError, ValueError):
        return None


def hhmm_sec(sec):
    """当天秒数 -> 'HH:MM'。"""
    try:
        s = int(sec) % 86400
        return "%02d:%02d" % (s // 3600, (s % 3600) // 60)
    except (TypeError, ValueError):
        return "00:00"


def date_epoch(date_text, sec_of_day=0):
    """'2026-10-19' + 当天秒数 -> epoch；解析失败返回 None。"""
    try:
        import datetime
        d = datetime.datetime.strptime(str(date_text).strip(), DATE_FMT)
        base = d.timestamp()
        return base + int(sec_of_day or 0)
    except (TypeError, ValueError):
        return None


def to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def to_int(value, default=0):
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def clamp(value, low, high):
    return max(low, min(high, value))


def canonical(obj):
    """稳定 JSON 文本（审计/去重用）。"""
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(obj)


def parse_json(text, default):
    if text is None or text == "":
        return default
    if isinstance(text, (list, dict)):
        return text
    try:
        val = json.loads(text)
        return val if val is not None else default
    except (TypeError, ValueError):
        return default


def severity_at_least(sev, floor):
    from config import SEVERITY_ORDER
    return SEVERITY_ORDER.get(sev, 0) >= SEVERITY_ORDER.get(floor, 0)


def max_severity(a, b):
    from config import SEVERITY_ORDER
    return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b


# =============================================================================
# 设备
# =============================================================================
@dataclass
class Device:
    device_id: str = ""
    code: str = ""
    name: str = ""
    kind: str = "monitor"                 # monitor / probe / jammer
    site: str = ""
    room_id: str = ""
    x: float = 0.0                        # 站点平面坐标（米），用于定位解算
    y: float = 0.0
    located: int = 0                      # 是否已登记坐标（(0,0) 也可能是有效坐标，故单列标记）
    direction: float = -1.0               # 定向天线方位角（度）；-1 = 未设置
    sector_width: float = 120.0           # 扇区宽度（度）
    bands: list = field(default_factory=list)   # 屏蔽器支持/计划的频段
    power_w: float = 1.0                  # 屏蔽器当前/额定功率
    max_power_w: float = 5.0
    status: str = "online"
    proto: str = "manual"
    address: str = ""
    last_seen: str = ""
    note: str = ""
    created_at: str = ""

    JSON_FIELDS = frozenset({"bands"})

    def to_dict(self):
        return dataclasses.asdict(self)

    def direction_text(self):
        if self.direction is None or self.direction < 0:
            return "未设置（全向）"
        return "%d°（扇区 %.0f°）" % (int(self.direction), float(self.sector_width or 0))

    def status_text(self):
        import config
        return config.DEV_STATUS_TEXT.get(self.status, self.status)


# =============================================================================
# 扫描帧
# =============================================================================
@dataclass
class Scan:
    scan_id: str = ""
    station_code: str = ""
    band: str = ""
    ts: str = ""
    ts_epoch: float = 0.0
    noise_floor_dbm: float = -100.0
    peak_count: int = 0
    occupancy: float = 0.0
    source: str = "manual"                # real / sim / manual
    peaks: list = field(default_factory=list)   # [{freq_mhz,power_dbm,bandwidth_mhz}]
    raw: dict = field(default_factory=dict)

    JSON_FIELDS = frozenset({"peaks", "raw"})

    def to_dict(self):
        return dataclasses.asdict(self)


# =============================================================================
# 异常/告警
# =============================================================================
@dataclass
class Anomaly:
    anomaly_id: str = ""
    station_code: str = ""
    room_id: str = ""
    kind: str = "unknown"
    band: str = ""
    freq_mhz: float = 0.0
    bandwidth_mhz: float = 0.0
    peak_dbm: float = 0.0
    noise_floor_dbm: float = -100.0
    snr_db: float = 0.0
    severity: str = "watch"
    score: float = 0.0
    feature_code: str = ""
    feature_name: str = ""
    legal: int = 0
    status: str = "open"
    detected_epoch: float = 0.0
    first_seen_epoch: float = 0.0
    last_seen_epoch: float = 0.0
    seen_count: int = 1
    latency_sec: float = 0.0
    located_x: float = -1.0
    located_y: float = -1.0
    radius_m: float = -1.0
    confidence: float = 0.0
    detail: str = ""
    source: str = "manual"
    alert_id: str = ""
    reported_at: str = ""
    review_conclusion: str = ""
    review_reason: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    created_at: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)

    def kind_text(self):
        import config
        return config.KIND_TEXT.get(self.kind, self.kind)

    def severity_text(self):
        import config
        return config.SEVERITY_TEXT.get(self.severity, self.severity)

    def status_text(self):
        import config
        return config.STATUS_TEXT.get(self.status, self.status)

    def band_text(self):
        import config
        return config.band_name(self.band)

    def latency_text(self):
        return "%.1fs" % float(self.latency_sec or 0)

    def located_text(self):
        if self.located_x is None or self.located_x < 0:
            return "未定位"
        return "(%.1f, %.1f) ±%.0fm" % (self.located_x, self.located_y,
                                        self.radius_m if self.radius_m >= 0 else 0)

    def frequency_text(self):
        if self.freq_mhz >= 1000:
            return "%.4f GHz" % (self.freq_mhz / 1000.0)
        return "%.3f MHz" % self.freq_mhz

    def is_open(self):
        import config
        return self.status in config.OPEN_STATUSES


# =============================================================================
# 屏蔽状态与开关记录
# =============================================================================
@dataclass
class JammerState:
    device_id: str = ""
    is_on: int = 0
    since_epoch: float = 0.0
    bands: list = field(default_factory=list)
    power_w: float = 0.0
    session_id: str = ""
    approval_no: str = ""
    operator: str = ""
    reason: str = ""
    updated_at: str = ""

    JSON_FIELDS = frozenset({"bands"})

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class SwitchLog:
    log_id: str = ""
    device_id: str = ""
    action: str = "on"
    bands: list = field(default_factory=list)
    power_w: float = 0.0
    session_id: str = ""
    source: str = "manual"
    operator: str = ""
    reason: str = ""
    ts: str = ""
    ts_epoch: float = 0.0
    extra: dict = field(default_factory=dict)

    JSON_FIELDS = frozenset({"bands", "extra"})

    def to_dict(self):
        return dataclasses.asdict(self)

    def action_text(self):
        import config
        return config.SW_ACTION_TEXT.get(self.action, self.action)

    def source_text(self):
        import config
        return config.SW_SOURCE_TEXT.get(self.source, self.source)


# =============================================================================
# 审批 / 协同
# =============================================================================
@dataclass
class Approval:
    approval_id: str = ""
    kind: str = "approval"
    no: str = ""
    org: str = ""
    site: str = ""
    bands: list = field(default_factory=list)
    valid_from: str = ""                  # YYYY-MM-DD
    valid_to: str = ""                    # YYYY-MM-DD
    doc: str = ""                         # 审批/报备文件路径或编号
    status: str = "approved"
    note: str = ""
    created_at: str = ""

    JSON_FIELDS = frozenset({"bands"})

    def to_dict(self):
        return dataclasses.asdict(self)

    def kind_text(self):
        import config
        return config.APPR_KIND_TEXT.get(self.kind, self.kind)

    def status_text(self):
        import config
        return config.APPR_STATUS_TEXT.get(self.status, self.status)

    def valid_today(self, today=None):
        if self.status != "approved":
            return False
        today = today or today_str()
        if self.valid_from and today < self.valid_from:
            return False
        if self.valid_to and today > self.valid_to:
            return False
        return True


# =============================================================================
# 实测证据：外溢 / 屏蔽效果
# =============================================================================
@dataclass
class SpillTest:
    test_id: str = ""
    jammer_id: str = ""
    point: str = ""
    distance_m: float = 0.0
    band: str = ""
    measured_dbm: float = -100.0
    limit_dbm: float = -85.0
    result: str = "pass"
    measured_at: str = ""
    ts_epoch: float = 0.0
    operator: str = ""
    note: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class ShieldTest:
    test_id: str = ""
    room_id: str = ""
    band: str = ""
    before_dbm: float = -60.0
    after_dbm: float = -90.0
    attenuation_db: float = 0.0
    rate: float = 0.0                    # 屏蔽有效率（该频段实测达标比例）
    result: str = "pass"
    measured_at: str = ""
    ts_epoch: float = 0.0
    operator: str = ""
    note: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)


# =============================================================================
# 考试场次（时间表）
# =============================================================================
@dataclass
class Session:
    session_id: str = ""
    name: str = ""
    subject: str = ""
    date: str = ""                        # YYYY-MM-DD
    start: str = "09:00"                  # HH:MM
    end: str = "11:00"                    # HH:MM
    pre_open_sec: int = 900
    post_close_sec: int = 0
    rooms: list = field(default_factory=list)
    state: str = "planned"
    operator: str = ""
    note: str = ""
    created_at: str = ""

    JSON_FIELDS = frozenset({"rooms"})

    def to_dict(self):
        return dataclasses.asdict(self)

    def state_text(self):
        import config
        return config.SESSION_STATE_TEXT.get(self.state, self.state)

    def window_text(self):
        return "%s %s~%s（提前 %d 分开启）" % (
            self.date, self.start, self.end, int(self.pre_open_sec or 0) // 60)


# =============================================================================
# 作弊信号特征库
# =============================================================================
@dataclass
class Feature:
    feature_code: str = ""
    name: str = ""
    low_mhz: float = 2400.0
    high_mhz: float = 2483.5
    bandwidth_mhz: float = 2.0
    modulation: str = ""
    duty: str = "burst"                   # burst / continuous / hopping
    is_legal: int = 0
    base_confidence: float = 0.8
    enabled: int = 1
    builtin: int = 1
    note: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)

    def range_text(self):
        return "%.4g~%.4g MHz" % (self.low_mhz, self.high_mhz)


# =============================================================================
# 联动关联 / 定位记录
# =============================================================================
@dataclass
class Correlation:
    corr_id: str = ""
    source: str = ""                      # 06 / 02
    ref_id: str = ""
    anomaly_id: str = ""
    room_id: str = ""
    detail: str = ""
    created_epoch: float = 0.0
    raw: dict = field(default_factory=dict)

    JSON_FIELDS = frozenset({"raw"})

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class Location:
    loc_id: str = ""
    anomaly_id: str = ""
    x: float = 0.0
    y: float = 0.0
    radius_m: float = -1.0
    confidence: float = 0.0
    method: str = ""
    stations: list = field(default_factory=list)
    created_epoch: float = 0.0
    note: str = ""

    JSON_FIELDS = frozenset({"stations"})

    def to_dict(self):
        return dataclasses.asdict(self)


# =============================================================================
# 回放测试
# =============================================================================
@dataclass
class ReplayRun:
    run_id: str = ""
    total: int = 0
    hit: int = 0
    miss: int = 0
    false_alarm: int = 0
    avg_latency: float = 0.0
    max_latency: float = 0.0
    detect_rate: float = 0.0
    false_rate: float = 0.0
    metrics: dict = field(default_factory=dict)
    operator: str = ""
    note: str = ""
    created_at: str = ""

    JSON_FIELDS = frozenset({"metrics"})

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclass
class ReplayItem:
    item_id: str = ""
    run_id: str = ""
    scenario_id: str = ""
    name: str = ""
    expected: str = ""
    detected: str = ""
    ok: int = 0
    latency_sec: float = 0.0
    detail: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)


# =============================================================================
# 审计
# =============================================================================
@dataclass
class Audit:
    audit_id: str = ""
    ts: str = ""
    ts_epoch: float = 0.0
    action: str = ""
    detail: str = ""
    operator: str = ""
    prev_hash: str = ""
    hash: str = ""

    JSON_FIELDS = frozenset()

    def to_dict(self):
        return dataclasses.asdict(self)


ALL_MODELS = (Device, Scan, Anomaly, JammerState, SwitchLog, Approval, SpillTest,
              ShieldTest, Session, Feature, Correlation, Location, ReplayRun,
              ReplayItem, Audit)
