# -*- coding: utf-8 -*-
r"""
06 智能安检与手机作弊器材检测 · 配置

本子方案干的事：**事前阻断**——在考点入口把手机、无线耳机、金属作弊器材挡在门外。

   安检门过检 -> 报警 -> 复检处置 -> 手机集中保管 -> 与身份核验关联（人-物-时闭环）
                                              -> 特殊考生走人工安检通道（双人见证）

与 02/03 的关系：
  02/03 是"事中发现"（摄像头 + 智能终端告警处置），本模块是"事前阻断"；
  发现报警时通过 00_集成联动 把告警推到 03 智能终端处置，走同一套告警分级。

**如实说明（重要）**：
  本机没有安检门/手持探测器/储物柜实物，所以：
    · 第 1 层 判据引擎 + 复检 + 保管 + 统计 是**真实可用的业务逻辑**；
    · 第 2 层 设备接入支持 HTTP 行协议 / TCP 行协议 / 手工录入三种**真实**入口，
      接上真设备即可用（协议见 README「设备接入协议」）；
    · 没有真设备时，用 `gate.py` 的**模拟器**造数据，所有模拟来源都标 source=sim，
      界面与报告里都写明"模拟"，**绝不冒充真实检出**。

数据默认落在本目录 data\，可用 EXAM_SEC_HOME 重定向。
**零第三方依赖**：只用标准库（tkinter / sqlite3 / http.server），双击即用。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_SEC_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "sec_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "security.db"
LOG_DIR = DATA_ROOT / "logs"
REPORT_DIR = DATA_ROOT / "reports"          # 验收证据导出
SAMPLE_DIR = DATA_ROOT / "samples"          # 样本清单（可导入导出）
LOCKER_DIR = DATA_ROOT / "lockers"          # 柜体清单导出
SECURITY_LOG = LOG_DIR / "security.log"

for _d in (DATA_ROOT, LOG_DIR, REPORT_DIR, SAMPLE_DIR, LOCKER_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------- 窗口由 pythonw 启动时的 stdout 兜底 ----------------
def ensure_stdio(log_name="app_console.log"):
    """pythonw.exe 启动时 sys.stdout/stderr 是 None，任何 print 都会抛异常把程序打挂。"""
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

# ---------------- 指标口径（来自《智能安检与手机作弊器材检测.md》第二节） ----------------
TARGET_PHONE_RATE = 0.99        # 手机（含关机）检出率 ≥ 99%
TARGET_EARPHONE_RATE = 0.95     # 无线耳机检出率 ≥ 95%
TARGET_FALSE_RATE = 0.05        # 误报率 ≤ 5%
TARGET_THROUGHPUT_PPM = 20.0    # 单人通过效率 ≥ 20 人/分钟

KIND_MOBILE = "mobile"          # 手机（含关机、藏匿）
KIND_EARPHONE = "earphone"      # 无线耳机 / 蓝牙耳机 / 隐形耳机
KIND_METAL = "metal"            # 金属作弊器材（接收器、线圈、金属件）
KIND_MAGNETIC = "magnetic"      # 强磁 / 磁吸器材
KIND_SHOE = "shoe"              # 鞋底/隐蔽部位
KIND_IMPLANT = "implant"        # 体内植入医疗器械（**不算作弊**，须走人工通道）

ALARM_KINDS = (KIND_MOBILE, KIND_EARPHONE, KIND_METAL, KIND_MAGNETIC, KIND_SHOE)
KIND_TEXT = {
    KIND_MOBILE: "手机",
    KIND_EARPHONE: "无线耳机",
    KIND_METAL: "金属器材",
    KIND_MAGNETIC: "强磁器材",
    KIND_SHOE: "鞋底藏匿",
    KIND_IMPLANT: "植入医疗器械",
}

# ---------------- 设备类型 ----------------
DEV_GATE = "gate"               # 智能安检门（电磁感应/毫米波）
DEV_HANDHELD = "handheld"       # 手持金属探测器
DEV_LOCKER = "locker"           # 智能储物柜
DEV_SHOE = "shoe"               # 鞋底检测仪
DEV_KINDS = (DEV_GATE, DEV_HANDHELD, DEV_LOCKER, DEV_SHOE)
DEV_KIND_TEXT = {
    DEV_GATE: "智能安检门",
    DEV_HANDHELD: "手持探测器",
    DEV_LOCKER: "智能储物柜",
    DEV_SHOE: "鞋底检测仪",
}

# 接入协议：真设备按行协议推事件；sim=内置模拟器；manual=人工录入
PROTO_HTTP = "http"             # POST /api/pass  一行 JSON
PROTO_TCP = "tcp"               # telnet 风格一行 JSON（默认 9006 端口）
PROTO_SERIAL = "serial"         # 串口一行 JSON（需要 pyserial，未装则明确报错）
PROTO_SIM = "sim"               # 内置模拟器
PROTO_MANUAL = "manual"         # 人工录入（复检/演练用）
PROTOS = (PROTO_HTTP, PROTO_TCP, PROTO_SERIAL, PROTO_SIM, PROTO_MANUAL)
PROTO_TEXT = {
    PROTO_HTTP: "HTTP 行协议",
    PROTO_TCP: "TCP 行协议",
    PROTO_SERIAL: "串口行协议",
    PROTO_SIM: "内置模拟器",
    PROTO_MANUAL: "人工录入",
}

# ---------------- 判据阈值（安检门的六路感应，0~1 归一化强度） ----------------
# 判据引擎 gate.decide() 用这一组阈值把"感应强度"翻成"报什么警"。
# 阈值可现场按误报/漏报表现调（设置页可改，落在 security_settings.json）。
SENSOR_KEYS = ("metal", "magnetic", "phone", "earphone", "millimeter", "shoe")
SENSOR_TEXT = {
    "metal": "金属感应",
    "magnetic": "磁场扰动",
    "phone": "手机特征(电磁指纹+回波)",
    "earphone": "无线耳机特征(2.4G 微弱辐射)",
    "millimeter": "毫米波成像",
    "shoe": "鞋底探测",
}
DEFAULT_THRESHOLDS = {
    "metal": 0.62,          # 金属感应强度（腰带扣/钥匙也会触发，故偏高）
    "magnetic": 0.70,       # 磁吸支架、强磁作弊器材
    "phone": 0.55,          # 手机关机也保留金属+电池特征，可检出
    "earphone": 0.50,       # 无线耳机信号弱，阈值偏低（靠复检压误报）
    "millimeter": 0.60,     # 毫米波成像（贴身藏匿）
    "shoe": 0.58,           # 鞋底检测仪
}

# 双判据确认：强度过阈值即"疑似"，两个及以上感应器同时报 -> 直接红色报警；
# 只有一路报 -> 橙色报警 + 复检（用来压误报，同时不放过单路也能发现的手机）。
DOUBLE_SENSOR_CONFIRM = True

# ---------------- 复检与处置 ----------------
REINSPECT_SLA_SEC = 60          # 报警后 60 秒内必须完成复检（否则界面标红催办）
REINSPECT_TIMEOUT_ESCALATE = 180  # 超 180 秒未处置 -> 上报 03 智能终端
REINSPECT_RESULTS = {
    "release": "复检无异常，放行",
    "detain": "查获器材，暂扣并登记",
    "manual_channel": "转人工安检通道",
    "false_positive": "确认误报（进算法迭代）",
    "duplicate": "重复过检（同一次进入的重复报警）",
}
REINSPECT_METHODS = {
    "handheld": "手持探测器复检",
    "patdown": "人工手检",
    "recheck_gate": "重新过安检门",
    "manual_channel": "人工安检通道",
}

# ---------------- 手机集中保管（智能储物柜） ----------------
CELL_FREE = "free"
CELL_OCCUPIED = "occupied"
CELL_LOCKED = "locked"          # 已存但柜门异常/锁定
CELL_FAULT = "fault"
CELL_STATES = (CELL_FREE, CELL_OCCUPIED, CELL_LOCKED, CELL_FAULT)
CELL_STATE_TEXT = {
    CELL_FREE: "空闲",
    CELL_OCCUPIED: "已存手机",
    CELL_LOCKED: "异常锁定",
    CELL_FAULT: "故障",
}
OPEN_METHODS = {
    "face": "人脸开柜",
    "qr": "扫码开柜",
    "card": "证件开柜",
    "manual": "管理员应急开柜",
}
LOCKER_OVERDUE_MIN = 30         # 超过 30 分钟未取 -> 超时提醒（考后集中取件）
LOCKER_LOG_ACTIONS = ("store", "take", "open", "fault", "overdue", "emergency")
LOCKER_ACTION_TEXT = {
    "store": "存入",
    "take": "取走",
    "open": "开柜",
    "fault": "柜门异常",
    "overdue": "超时未取提醒",
    "emergency": "应急开柜",
}

# ---------------- 特殊考生（人工安检替代通道） ----------------
SPECIAL_REASONS = {
    "pregnant": "孕妇",
    "implant": "体内植入医疗器械",
    "disability": "残障/行动不便",
    "medical": "有医嘱不宜过安检门",
    "other": "其他（须写明）",
}
SPECIAL_RESULTS = {
    "passed": "人工安检通过",
    "detained": "人工安检发现问题",
    "withdrawn": "考生放弃/改期",
}
SPECIAL_WITNESS_REQUIRED = True   # 双人见证（见证人不能与主检人相同）

# ---------------- 入场流程（安检-核验-签到三点位串联） ----------------
STAGE_ORDER = ("security", "verify", "signin")
STAGE_TEXT = {"security": "安检", "verify": "身份核验", "signin": "签到"}
# 单点位耗时超过这个值就算瓶颈（秒）
STAGE_BOTTLENECK_SEC = {"security": 45, "verify": 20, "signin": 15}
QUEUE_WARN = 25                 # 单通道排队 ≥ 25 人 -> 预警
QUEUE_CRITICAL = 40             # ≥ 40 人 -> 建议开启备用通道
QUEUE_BACKUP_AT = 35            # 建议加开备用/人工通道的阈值
THROUGHPUT_WINDOW_SEC = 300     # 效率统计窗口（5 分钟）

# ---------------- 报告 ----------------
REPORT_TITLE = "智能安检与手机作弊器材检测 · 验收证据"

# ---------------- 设置文件（可手改，也可在界面里改） ----------------
SETTINGS_PATH = DATA_ROOT / "security_settings.json"
SETTINGS_DEFAULT = {
    "site": "第一考点",             # 考点名
    "operator": "安检员",           # 默认操作员（留痕用）
    "gate_mode": "standard",        # standard / turbo（turbo 放宽阈值换效率，须复检兜底）
    "thresholds": dict(DEFAULT_THRESHOLDS),
    "double_confirm": True,         # 双判据确认（压误报）
    "locker_overdue_min": LOCKER_OVERDUE_MIN,
    "report_to_terminal": False,    # 是否把报警实时上报 03 智能终端（默认关，避免刷屏）
    "sim_interval_sec": 3.0,        # 模拟器平均过检间隔
    "sim_alarm_rate": 0.18,         # 模拟器报警比例（演示用）
    "auto_open_backup": True,       # 排队超阈值时自动提示加开备用通道
    "anonymize_in_report": True,    # 报告里身份证号只留掩码
    "encrypt_sensitive": True,      # 考生标识字段加密落库（需 cryptography；缺了自动降级并如实提示）
}

# turbo 模式的阈值缩放（判据放宽 -> 更快通过 -> 靠复检兜底）
GATE_MODE_SCALE = {"standard": 1.0, "turbo": 0.85, "strict": 1.12}
GATE_MODE_TEXT = {"standard": "标准", "turbo": "快速通行（放宽）", "strict": "从严（压漏检）"}


def load_settings():
    """读设置：缺文件/坏文件/缺键都回落默认值，绝不抛异常。"""
    import json
    cfg = {k: (dict(v) if isinstance(v, dict) else v)
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
            else:
                merged[k] = v
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def thresholds(cfg=None):
    """
    当前生效阈值：默认值 -> 设置覆盖 -> 按 gate_mode 缩放。
    返回 {sensor: float}，键与 SENSOR_KEYS 一致。
    """
    cfg = cfg or load_settings()
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in (cfg.get("thresholds") or {}).items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    scale = GATE_MODE_SCALE.get(str(cfg.get("gate_mode") or "standard"), 1.0)
    return {k: max(0.05, min(0.98, round(v * scale, 4))) for k, v in out.items()}


def mask_id(text, keep=4):
    """身份证/准考证号掩码（报告与界面默认不显全号）。"""
    s = str(text or "").strip()
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def describe():
    cfg = load_settings()
    try:
        import data_crypto
        crypto = data_crypto.status_text()
    except Exception:                                          # noqa: BLE001
        crypto = "加密模块不可用"
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "考点": cfg.get("site"),
        "安检模式": GATE_MODE_TEXT.get(cfg.get("gate_mode"), cfg.get("gate_mode")),
        "生效阈值": thresholds(cfg),
        "指标口径": "手机检出≥%.0f%%、耳机检出≥%.0f%%、误报≤%.0f%%、通过≥%.0f人/分钟"
                    % (TARGET_PHONE_RATE * 100, TARGET_EARPHONE_RATE * 100,
                       TARGET_FALSE_RATE * 100, TARGET_THROUGHPUT_PPM),
        "敏感字段加密": crypto,
        "依赖": "仅标准库（tkinter/sqlite3）；装了 cryptography 则考生标识字段自动加密",
    }
