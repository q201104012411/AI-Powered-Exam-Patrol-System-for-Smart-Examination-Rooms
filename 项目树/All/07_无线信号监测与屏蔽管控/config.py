# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 配置

本专题补齐 AI 视频/音频覆盖不到的**频谱盲区**：无线作弊器材（隐形耳机、数传模块、
无线图传）靠射频链路传答案，画面上看不出来，必须用频谱手段发现并定位；同时按
"依法报备、定向低功率、考试时段开关"的红线管控屏蔽设备。

   频谱扫描 -> 噪声底估计 -> 峰值检测 -> 作弊特征库匹配 -> 异常分级
            -> 多站测向/场强定位 -> 03 智能终端告警处置
   考试时间表 -> 开考前开启屏蔽 -> 结束即关 -> 开关记录与时间表一致性核对 -> 验收报告

**如实说明（重要）**：
  本机没有频谱仪/测向站/屏蔽器实物，所以：
    · 频谱分析、特征匹配、定位解算、屏蔽状态机、时间表联动、合规体检、报告
      都是**真实可用的业务逻辑**（有自检与回放指标）；
    · 设备接入提供 HTTP / TCP 行协议（`ingest_service.py`，默认 127.0.0.1:9127）
      与人工录入三种**真实**入口，接上真设备即可用（协议见「使用说明.md」）；
      没有真设备时用内置模拟器，所有模拟数据都标 source=sim，界面/报告里写明
      "模拟"，绝不冒充真实监测；
    · 屏蔽设备的"开启"在本机是**状态机 + 开关记录**，不会真的发射射频 ——
      这在没有硬件时是唯一诚实的做法；接上真屏蔽器后由适配层下发指令。

与其它专题的关系（**只调用，不修改**）：
  00_集成联动  复用其"子进程 + 单行 JSON"总线，不复制、不改写
  01 人脸识别  可选：按考生标识回溯，本模块默认不依赖（避免把频谱告警绑到身份）
  02 摄像头核查 可选：定位到考场后，查该考场机位画质/在线情况，作为人工复核参考
  03 智能终端  告警经 00 或 03 的局域网接入服务进入 03，走同一套分级/去重/复核留痕
  06 智能安检  反向联动：06 检出无线耳机/手机时，本模块拉取结论并对相关考场加强监测

数据默认落在本目录 data\，可用 EXAM_RF_HOME 重定向。
**零第三方依赖**：只用标准库（tkinter / sqlite3 / http.server），双击即用。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_RF_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "rf_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "rf.db"
LOG_DIR = DATA_ROOT / "logs"
REPORT_DIR = DATA_ROOT / "reports"          # 验收证据导出（md/csv）
SAMPLE_DIR = DATA_ROOT / "samples"          # 样本/回放数据
EXPORT_DIR = DATA_ROOT / "exports"          # 站点、设备、开关记录导出
RF_LOG = LOG_DIR / "rf.log"

for _d in (DATA_ROOT, LOG_DIR, REPORT_DIR, SAMPLE_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------- 窗口由 pythonw 启动时的 stdout 兜底 ----------------
def ensure_stdio(log_name="app_console.log"):
    """pythonw.exe 启动时 sys.stdout/stderr 是 None，任何 print 都会抛异常。"""
    import sys
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = LOG_DIR / log_name
    try:
        f = open(str(path), "a", encoding="utf-8", buffering=1)
    except OSError:
        try:
            f = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return None
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return str(path)


HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"

# ---------------- 指标口径（来自《无线信号监测与屏蔽管控.md》第二节） ----------------
TARGET_DISCOVERY_SEC = 10.0      # 异常信号发现 ≤ 10 秒
TARGET_COVERAGE_MARGIN_M = 100.0  # 考点全域 + 周边 100 米可疑区域
TARGET_SHIELD_RATE = 0.92        # 屏蔽有效率（实测达标频段占比）
TARGET_SPILL_DBM = -85.0         # 外溢限值：考点外围合法通信可用性验证的实测门限
TARGET_ATTEN_DB = 20.0           # 屏蔽前后场强衰减至少 20 dB 才算有效
TARGET_SECTOR_MAX_DEG = 90.0     # 定向天线扇区宽度上限（防止全向外溢）
TARGET_POWER_MAX_W = 5.0         # 单台屏蔽器功率上限（低功率、贴近考场）
TARGET_FEATURE_MIN = 4           # 作弊信号特征库最少条数
MONITOR_RADIUS_M = 250.0         # 单台监测站的有效覆盖半径（按设备型号/天线增益可在部署时改）

# ---------------- 频段表（监测与屏蔽共用一套口径） ----------------
# code, 名称, 低端 MHz, 高端 MHz, 分组, 是否属于屏蔽计划, 说明
BANDS = (
    {"code": "gsm900", "name": "GSM900 / 2G", "low": 890.0, "high": 960.0,
     "group": "cellular", "shield": True, "note": "2G 语音/短信，作弊耳机常配 GSM 模块"},
    {"code": "dcs1800", "name": "DCS1800 / LTE1800", "low": 1710.0, "high": 1880.0,
     "group": "cellular", "shield": True, "note": "2G/4G 上行与下行"},
    {"code": "lte2100", "name": "LTE2100 / 3G", "low": 1920.0, "high": 2170.0,
     "group": "cellular", "shield": True, "note": "3G/4G 主流频段"},
    {"code": "tdd2300", "name": "LTE 2300 (B40)", "low": 2300.0, "high": 2400.0,
     "group": "cellular", "shield": True, "note": "4G TDD"},
    {"code": "nr2600", "name": "5G NR 2.6G (n41)", "low": 2515.0, "high": 2675.0,
     "group": "cellular", "shield": True, "note": "5G 主力频段"},
    {"code": "nr3500", "name": "5G NR 3.5G (n78)", "low": 3300.0, "high": 3600.0,
     "group": "cellular", "shield": True, "note": "5G 主力频段"},
    {"code": "nr4900", "name": "5G NR 4.9G (n79)", "low": 4800.0, "high": 4900.0,
     "group": "cellular", "shield": True, "note": "5G 扩展频段"},
    {"code": "ism24", "name": "2.4G ISM（WiFi/蓝牙）", "low": 2400.0, "high": 2483.5,
     "group": "shortrange", "shield": True, "note": "WiFi 2.4G、蓝牙、2.4G 数传作弊模块"},
    {"code": "unii1", "name": "5.1G WiFi", "low": 5150.0, "high": 5350.0,
     "group": "shortrange", "shield": True, "note": "WiFi 5G 低段"},
    {"code": "unii3", "name": "5.8G WiFi/图传", "low": 5725.0, "high": 5850.0,
     "group": "shortrange", "shield": True, "note": "WiFi 5.8G 与无线图传"},
    {"code": "uhf", "name": "UHF 400-470（对讲/隐形耳机）", "low": 400.0, "high": 470.0,
     "group": "intercom", "shield": True, "note": "考务对讲常用；如本考点对讲在此段，"
                                                  "屏蔽计划须排除该频段（否则会屏蔽自己）"},
    {"code": "vhf", "name": "VHF 136-174（对讲/隐形耳机）", "low": 136.0, "high": 174.0,
     "group": "intercom", "shield": True, "note": "VHF 对讲与部分隐形耳机"},
)
BAND_INDEX = {b["code"]: b for b in BANDS}
BAND_ORDER = tuple(b["code"] for b in BANDS)
BAND_GROUP_TEXT = {
    "cellular": "公众移动通信",
    "shortrange": "短距/ISM",
    "intercom": "对讲/专用",
    "other": "其他",
}
DEFAULT_SHIELD_BANDS = tuple(b["code"] for b in BANDS if b["shield"])


def band_of(freq_mhz):
    """按频率反查频段码；落在多个频段交叠处时取**排序靠前**的那个。查不到返回空串。"""
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return ""
    for code in BAND_ORDER:
        b = BAND_INDEX[code]
        if b["low"] <= f <= b["high"]:
            return code
    return ""


def band_name(code):
    b = BAND_INDEX.get(code or "")
    return b["name"] if b else (code or "未知频段")


def band_range(code):
    b = BAND_INDEX.get(code or "")
    return (b["low"], b["high"]) if b else (0.0, 0.0)


def band_options():
    return [{"code": b["code"], "name": b["name"], "low": b["low"], "high": b["high"],
             "group": b["group"], "group_text": BAND_GROUP_TEXT.get(b["group"], ""),
             "shield": bool(b["shield"]), "note": b["note"]} for b in BANDS]


# ---------------- 信号检测阈值（可在设置里调，落在 rf_settings.json） ----------------
DEFAULT_THRESHOLDS = {
    "noise_margin_db": 6.0,      # 高于噪声底多少 dB 判为"有信号"
    "min_snr_db": 6.0,           # 最小信噪比
    "sustain_frames": 3,         # 连续多少帧算"持续发射"
    "sustain_window_sec": 30.0,  # 持续发射统计窗口
    "dedup_freq_mhz": 0.6,       # 同一异常的频率合并容差
    "dedup_window_sec": 180.0,   # 同一异常的合并时间窗
    "wideband_mhz": 20.0,        # 超过该带宽判为宽带异常
    "scan_interval_sec": 2.0,    # 建议扫描周期（也是发现时延的分辨率）
    "self_suppress_db": 6.0,     # 屏蔽开启时，带内检测阈值抬高多少 dB（避免把自己的信号当作弊）
    "occupancy_threshold_db": 6.0,
}
THRESHOLD_MIN = {"noise_margin_db": 2.0, "min_snr_db": 2.0, "sustain_frames": 1,
                 "sustain_window_sec": 5.0, "dedup_freq_mhz": 0.05,
                 "dedup_window_sec": 10.0, "wideband_mhz": 5.0,
                 "scan_interval_sec": 0.5, "self_suppress_db": 0.0,
                 "occupancy_threshold_db": 1.0}
THRESHOLD_MAX = {"noise_margin_db": 30.0, "min_snr_db": 30.0, "sustain_frames": 60,
                 "sustain_window_sec": 600.0, "dedup_freq_mhz": 20.0,
                 "dedup_window_sec": 3600.0, "wideband_mhz": 200.0,
                 "scan_interval_sec": 60.0, "self_suppress_db": 30.0,
                 "occupancy_threshold_db": 30.0}
THRESHOLD_TEXT = {
    "noise_margin_db": "噪声门限（dB）",
    "min_snr_db": "最小信噪比（dB）",
    "sustain_frames": "持续判定帧数",
    "sustain_window_sec": "持续判定窗口（秒）",
    "dedup_freq_mhz": "同频合并容差（MHz）",
    "dedup_window_sec": "同频合并窗口（秒）",
    "wideband_mhz": "宽带判定（MHz）",
    "scan_interval_sec": "扫描周期（秒）",
    "self_suppress_db": "屏蔽时带内抬高（dB）",
    "occupancy_threshold_db": "占用率门限（dB）",
}

# ---------------- 异常类型 ----------------
KIND_CHEAT_BURST = "cheat_burst"      # 特征匹配到数传作弊（突发/跳频）
KIND_CHEAT_NARROW = "cheat_narrow"    # 窄带持续（隐形耳机/模拟发射）
KIND_SUSTAIN = "sustained"            # 未识别但持续发射
KIND_WIDEBAND = "wideband"            # 宽带异常（图传/干扰源）
KIND_ILLEGAL = "illegal_band"         # 非法/未授权频段发射
KIND_UNKNOWN = "unknown"              # 未识别异常
KIND_SELF = "self_shield"             # 自身屏蔽信号（不计告警，只留台账）
KIND_DEVICE = "device_fault"          # 设备侧异常（离线/故障）

ANOMALY_KINDS = (KIND_CHEAT_BURST, KIND_CHEAT_NARROW, KIND_SUSTAIN, KIND_WIDEBAND,
                 KIND_ILLEGAL, KIND_UNKNOWN)
KIND_TEXT = {
    KIND_CHEAT_BURST: "数传作弊信号（突发）",
    KIND_CHEAT_NARROW: "窄带可疑信号（隐形耳机）",
    KIND_SUSTAIN: "持续发射（未识别）",
    KIND_WIDEBAND: "宽带异常（图传/干扰）",
    KIND_ILLEGAL: "非法频段发射",
    KIND_UNKNOWN: "未识别异常",
    KIND_SELF: "自身屏蔽信号",
    KIND_DEVICE: "设备异常",
}

#: 异常类型 -> 默认严重度（可被特征置信度上调/下调）
KIND_SEVERITY = {
    KIND_CHEAT_BURST: "critical",
    KIND_CHEAT_NARROW: "critical",
    KIND_SUSTAIN: "warn",
    KIND_WIDEBAND: "warn",
    KIND_ILLEGAL: "warn",
    KIND_UNKNOWN: "watch",
}
SEVERITY_ORDER = {"watch": 1, "warn": 2, "critical": 3}
SEVERITY_TEXT = {"watch": "关注", "warn": "警告", "critical": "严重"}
#: 严重度 -> 03 智能终端的预警级别（03 只认 orange / red）
SEVERITY_LEVEL = {"watch": "orange", "warn": "orange", "critical": "red"}

#: 异常类型 -> 03 的告警码（**只使用 03 已登记分级的码**：SIGNAL_ABNORMAL 红、OFFLINE 橙）
ALERT_CODE_BY_KIND = {
    KIND_CHEAT_BURST: "SIGNAL_ABNORMAL",
    KIND_CHEAT_NARROW: "SIGNAL_ABNORMAL",
    KIND_SUSTAIN: "SIGNAL_ABNORMAL",
    KIND_WIDEBAND: "SIGNAL_ABNORMAL",
    KIND_ILLEGAL: "SIGNAL_ABNORMAL",
    KIND_UNKNOWN: "SIGNAL_ABNORMAL",
    KIND_DEVICE: "OFFLINE",
}
ALERT_CODE_TIMEOUT = "OFFLINE"      # 屏蔽未按时开/未按时关（设备侧问题，03 里为橙色）
ALERT_CODE_SELF = "OFFLINE"

# ---------------- 异常处置状态 ----------------
ST_OPEN = "open"                 # 待处置
ST_ACKED = "acked"               # 已确认
ST_CONFIRMED = "confirmed"       # 复核：成立
ST_FALSE = "false_positive"      # 复核：误报
ST_CLOSED = "closed"             # 已归档
ANOMALY_STATUSES = (ST_OPEN, ST_ACKED, ST_CONFIRMED, ST_FALSE, ST_CLOSED)
STATUS_TEXT = {ST_OPEN: "待处置", ST_ACKED: "已确认", ST_CONFIRMED: "成立",
               ST_FALSE: "误报", ST_CLOSED: "已归档"}
OPEN_STATUSES = (ST_OPEN, ST_ACKED)
REVIEW_CONCLUSIONS = {ST_CONFIRMED: "成立", ST_FALSE: "误报", ST_CLOSED: "已处置归档"}

# ---------------- 设备 ----------------
DEV_MONITOR = "monitor"          # 固定监测站（频谱扫描/测向）
DEV_PROBE = "probe"              # 手持侦测仪（逐步逼近定位）
DEV_JAMMER = "jammer"            # 屏蔽器（定向/低功率）
DEV_KINDS = (DEV_MONITOR, DEV_PROBE, DEV_JAMMER)
DEV_KIND_TEXT = {DEV_MONITOR: "固定监测站", DEV_PROBE: "手持侦测仪",
                 DEV_JAMMER: "屏蔽器"}
DEV_ONLINE = "online"
DEV_OFFLINE = "offline"
DEV_FAULT = "fault"
DEV_STATUSES = (DEV_ONLINE, DEV_OFFLINE, DEV_FAULT)
DEV_STATUS_TEXT = {DEV_ONLINE: "在线", DEV_OFFLINE: "离线", DEV_FAULT: "故障"}

#: 设备接入协议：真设备按行协议推扫描/心跳；sim=内置模拟器；manual=人工录入
PROTO_HTTP = "http"
PROTO_TCP = "tcp"
PROTO_SIM = "sim"
PROTO_MANUAL = "manual"
PROTOS = (PROTO_HTTP, PROTO_TCP, PROTO_SIM, PROTO_MANUAL)
PROTO_TEXT = {PROTO_HTTP: "HTTP 行协议", PROTO_TCP: "TCP 行协议",
              PROTO_SIM: "内置模拟器", PROTO_MANUAL: "人工录入"}

# ---------------- 考试时间表与开关窗口 ----------------
PRE_OPEN_DEFAULT_SEC = 900       # 默认开考前 15 分钟允许开启屏蔽（发卷前完成压制）
POST_CLOSE_DEFAULT_SEC = 0       # 默认考试结束立即关闭
SWITCH_GRACE_SEC = 30            # 开关记录与时间表核对的容差（秒）
SESSION_STATE_PLANNED = "planned"
SESSION_STATE_ACTIVE = "active"
SESSION_STATE_CLOSED = "closed"
SESSION_STATES = (SESSION_STATE_PLANNED, SESSION_STATE_ACTIVE, SESSION_STATE_CLOSED)
SESSION_STATE_TEXT = {SESSION_STATE_PLANNED: "未开始", SESSION_STATE_ACTIVE: "进行中",
                      SESSION_STATE_CLOSED: "已结束"}

# ---------------- 报备/审批 ----------------
APPR_APPROVAL = "approval"          # 无线电管理机构设台批准
APPR_COORDINATION = "coordination"  # 与运营商协同记录
APPR_KINDS = (APPR_APPROVAL, APPR_COORDINATION)
APPR_KIND_TEXT = {APPR_APPROVAL: "设台审批/报备", APPR_COORDINATION: "运营商协同"}
APPR_APPROVED = "approved"
APPR_PENDING = "pending"
APPR_EXPIRED = "expired"
APPR_REVOKED = "revoked"
APPR_STATUSES = (APPR_APPROVED, APPR_PENDING, APPR_EXPIRED, APPR_REVOKED)
APPR_STATUS_TEXT = {APPR_APPROVED: "有效", APPR_PENDING: "待批",
                    APPR_EXPIRED: "已过期", APPR_REVOKED: "已撤销"}

# ---------------- 开关记录 ----------------
SW_ON = "on"
SW_OFF = "off"
SW_FAULT = "fault"
SW_OVERRIDE = "override"
SW_ACTIONS = (SW_ON, SW_OFF, SW_FAULT, SW_OVERRIDE)
SW_ACTION_TEXT = {SW_ON: "开启", SW_OFF: "关闭", SW_FAULT: "故障",
                  SW_OVERRIDE: "人工强制"}
SW_SRC_MANUAL = "manual"
SW_SRC_AUTO = "auto"
SW_SRC_DRILL = "drill"
SW_SOURCES = (SW_SRC_MANUAL, SW_SRC_AUTO, SW_SRC_DRILL)
SW_SOURCE_TEXT = {SW_SRC_MANUAL: "人工", SW_SRC_AUTO: "时间表自动",
                  SW_SRC_DRILL: "演练"}

# ---------------- 报告 ----------------
REPORT_TITLE = "无线信号监测与屏蔽管控 · 验收证据"

# ---------------- 设置文件 ----------------
SETTINGS_PATH = DATA_ROOT / "rf_settings.json"
SETTINGS_DEFAULT = {
    "site": "第一考点",
    "operator": "频谱管理员",
    "approval_required": True,       # 无有效报备/审批不得开启屏蔽（演练除外）
    "auto_shield": True,             # 按考试时间表自动开启（开考前 pre_open 秒）
    "auto_off": True,                # 考试结束自动关闭
    "pre_open_sec": PRE_OPEN_DEFAULT_SEC,
    "post_close_sec": POST_CLOSE_DEFAULT_SEC,
    "max_power_w": TARGET_POWER_MAX_W,
    "spill_limit_dbm": TARGET_SPILL_DBM,
    "sector_max_deg": TARGET_SECTOR_MAX_DEG,
    "report_to_terminal": False,     # 是否把异常实时上报 03（默认关，避免刷屏）
    "terminal_url": "",              # 03 局域网接入服务地址（留空=同机子进程）
    "terminal_token": "",
    "sandbox_root": "",              # 联动自检/演练时把 02/03/06 数据重定向到此处
    "linkage_timeout": 60,
    "thresholds": dict(DEFAULT_THRESHOLDS),
    "sim_interval_sec": 2.0,
    "sim_duration_sec": 20.0,
    "anonymize_in_report": True,
    "exclude_bands": [],             # 屏蔽计划要排除的频段（如考务对讲所在 UHF）
    "drill_enabled": False,          # 演练模式：允许未经审批临时开机（留痕为 drill）
}


def load_settings():
    """读设置：缺文件/坏文件/缺键都回落默认值，绝不抛异常。"""
    import json
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in SETTINGS_DEFAULT.items()}
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in SETTINGS_DEFAULT:
                    if k not in data or data[k] is None:
                        continue
                    if isinstance(SETTINGS_DEFAULT[k], dict):
                        if isinstance(data[k], dict):
                            cfg[k].update({kk: vv for kk, vv in data[k].items()
                                           if kk in SETTINGS_DEFAULT[k]})
                    elif isinstance(SETTINGS_DEFAULT[k], list):
                        if isinstance(data[k], list):
                            cfg[k] = list(data[k])
                    elif data[k] != "":
                        cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_settings(settings):
    """写设置；失败返回 False（不影响主流程）。"""
    import json
    try:
        merged = load_settings()
        for k, v in (settings or {}).items():
            if k not in SETTINGS_DEFAULT:
                continue
            if isinstance(SETTINGS_DEFAULT[k], dict) and isinstance(v, dict):
                merged[k].update({kk: vv for kk, vv in v.items()
                                  if kk in SETTINGS_DEFAULT[k]})
            elif isinstance(SETTINGS_DEFAULT[k], list) and isinstance(v, list):
                merged[k] = list(v)
            else:
                merged[k] = v
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def thresholds(cfg=None):
    """当前生效阈值：默认值 -> 设置覆盖 -> 上下限夹紧。"""
    cfg = cfg or load_settings()
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in (cfg.get("thresholds") or {}).items():
        if k not in out:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    for k, v in out.items():
        lo = THRESHOLD_MIN.get(k, v)
        hi = THRESHOLD_MAX.get(k, v)
        out[k] = max(lo, min(hi, float(v)))
    out["sustain_frames"] = int(round(out["sustain_frames"]))
    return out


def shield_plan(cfg=None):
    """当前屏蔽计划：默认全频段 - 设置里排除的频段。"""
    cfg = cfg or load_settings()
    drop = set(cfg.get("exclude_bands") or [])
    return [c for c in DEFAULT_SHIELD_BANDS if c not in drop]


def mask_id(text, keep=4):
    """考生/证件标识掩码（报告与界面默认不显全号）。"""
    s = str(text or "").strip()
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


# ---------------- 联动：路径（只读定位，不写其它模块） ----------------
ALL_DIR = BASE_DIR.parent
MODULE_DIRS = {
    "00": ALL_DIR / "00_集成联动",
    "01": ALL_DIR / "01_人脸识别与身份核对" / "01_人脸识别与身份核对",
    "02": ALL_DIR / "02_摄像头核查与视频质量诊断",
    "03": ALL_DIR / "03_智能终端操作",
    "06": ALL_DIR / "06_智能安检与手机作弊器材检测",
}
MODULE_NAMES = {"00": "集成联动", "01": "人脸识别", "02": "摄像头核查",
                "03": "智能终端", "06": "智能安检"}

#: 07 对 03 上报时使用的来源标识（03 驾驶舱/远端记录里能认出来）
SOURCE_TAG = "07_rf"
DEVICE_ID_PREFIX = "RF-07"

#: 本模块 integration_api 支持的全部 op（自检会核对 OPS 字典与这里完全一致）
INTEGRATION_OPS = (
    "status", "bands", "features", "add_feature", "features_test",
    "devices", "add_device", "heartbeat", "scan", "scans",
    "anomalies", "anomaly", "locate", "review_anomaly", "report_anomaly",
    "jammers", "shield_on", "shield_off", "shield_auto", "shield_state",
    "switch_log", "sessions", "add_session", "session_state",
    "approvals", "add_approval", "spill_test", "spill_estimate", "shield_test",
    "compliance", "metrics", "replay_test", "correlate",
    "linkage_status", "report", "seed_demo", "audits",
)


def describe():
    cfg = load_settings()
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "考点": cfg.get("site"),
        "频段数": len(BANDS),
        "屏蔽计划": "、".join(band_name(c) for c in shield_plan(cfg)) or "（空）",
        "指标口径": "异常发现≤%.0f秒；覆盖考点+周边 %.0f 米；屏蔽有效率≥%.0f%%；"
                    "外溢≤%.0f dBm" % (TARGET_DISCOVERY_SEC, TARGET_COVERAGE_MARGIN_M,
                                      TARGET_SHIELD_RATE * 100, cfg.get("spill_limit_dbm")),
        "审批要求": "必须（无有效报备不得开启屏蔽）" if cfg.get("approval_required")
                    else "已关闭（不建议）",
        "自动开关": "开机 %s / 关机 %s" % ("开" if cfg.get("auto_shield") else "关",
                                          "开" if cfg.get("auto_off") else "关"),
        "依赖": "仅标准库（tkinter/sqlite3/http.server）",
    }
