# -*- coding: utf-8 -*-
r"""
试卷流转与保密室智能监控 · 配置

对应《试卷流转与保密室智能监控.md》：
  一、保密室智能监控：无死角视频 / 门禁管控 / 入侵探测 / 试卷柜电子锁 /
      环境监测 / 值守替代 / 录像留存
  二、试卷流转环节：领取交接 / 押运 / 电子铅封 / 分发至考场 / 回收与封存
  三、关键指标：覆盖率100% / 开柜开门告警≤5s / 交接完整率100% /
      录像留存连续可回放 / 押运偏离≤30s
  四、验收证据：无死角核查报告 / 门禁与开柜告警联调记录 /
      交接押运演练记录与台账 / 录像完整性配置证明

路径策略与 01/02/03 一致：数据默认落在本目录 data\，可用 EXAM_SECURE_HOME 重定向。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_SECURE_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "secure_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "secure.db"
LOG_DIR = DATA_ROOT / "logs"
EVIDENCE_DIR = DATA_ROOT / "evidence"        # 告警留证截图（开柜/入侵/环境）
CAPTURE_DIR = DATA_ROOT / "captures"         # 巡检抓帧留档
SEAL_DIR = DATA_ROOT / "seals"               # 电子铅封记录与照片
HANDOVER_DIR = DATA_ROOT / "handovers"       # 交接凭证（扫码/签字/人脸确认）
REPORT_DIR = DATA_ROOT / "reports"           # 验收报告
EXPORT_DIR = DATA_ROOT / "exports"           # 台账导出（csv）
CAMERA_LOG = LOG_DIR / "camera_stream.log"
SOAK_LOG = LOG_DIR / "guard.log"

for _d in (DATA_ROOT, LOG_DIR, EVIDENCE_DIR, CAPTURE_DIR, SEAL_DIR,
           HANDOVER_DIR, REPORT_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------- 隐藏命令行窗口（与 02/03 同一做法） ----------------
HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"


def ensure_stdio(log_name="gui_console.log"):
    """
    pythonw.exe（隐藏命令行窗口）启动时 sys.stdout / sys.stderr 都是 None，
    任何 print 都会抛 AttributeError 把程序直接打挂。
    这里把它们兜到一个日志文件里，保证隐藏窗口模式下也能正常跑。
    """
    import sys
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = LOG_DIR / log_name
    try:
        f = open(str(path), "a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = f
    sys.stderr = f
    return f


# ==================== 一、保密室智能监控 ====================

# ---- 1.1 无死角视频：保密室 6 面覆盖 ----
# 六面 = 前后左右 4 面墙 + 天花板（俯视）+ 地面（柜底/桌底）。
# 逐机位登记朝向，覆盖率按"已登记且在线"的机位 / 要求机位算。
COVER_FACES = ("front", "back", "left", "right", "top", "bottom")
FACE_TEXT = {
    "front": "前墙", "back": "后墙", "left": "左墙", "right": "右墙",
    "top": "天花板（俯视）", "bottom": "地面/柜底",
}
# 每个面的必拍要点（验收时逐机位核对"这台到底拍到了什么"）
FACE_CHECKPOINTS = {
    "front": ["门禁出入口", "试卷柜正面", "值守工位"],
    "back": ["试卷柜背面", "窗户", "后墙通道"],
    "left": ["左墙侧柜", "墙角"],
    "right": ["右墙侧柜", "墙角"],
    "top": ["整体俯视", "柜顶"],
    "bottom": ["柜底", "桌下", "地面"],
}
# 覆盖率要求：100%，无盲区
COVER_RATE_REQUIRED = 1.0
# 双路冗余录像：每个机位要求登记 2 路（主/备）录像通道
REDUNDANT_CHANNELS_REQUIRED = 2

# ---- 1.2 门禁管控：人脸 + 刷卡/密码双因子，双人双锁电子化 ----
# 双因子 = 因子1(生物/凭证) + 因子2(凭证)，两类各取一种才算通过
FACTOR_BIOMETRIC = ("face",)                 # 人脸
FACTOR_CREDENTIAL = ("card", "password")     # 刷卡 / 密码
ACCESS_FACTORS_REQUIRED = 2                  # 必须两个因子同时通过
DUAL_CUSTODY_REQUIRED = 2                    # 双人双锁：至少 2 名授权人到场
# 门禁开门必须留痕并触发视频留证
ACCESS_OPEN_CODE = "DOOR_OPEN_NORMAL"

# ---- 1.3 入侵探测：红外双鉴 / 门窗磁 / 震动 / 开柜检测 ----
INTRUSION_KINDS = {
    "ir_dual": "红外双鉴",
    "door_mag": "门窗磁",
    "vibration": "震动",
    "safe_open": "开柜检测",
}
# 布防时段（非工作时段强布防；工作时段弱布防/撤防）。24 小时制。[起, 止)
ARMED_WINDOWS = [(18, 0, 24, 0), (0, 0, 8, 0)]   # 18:00-次日08:00 为强布防
# 强布防时段任何入侵即刻红色告警
INTRUSION_SLA_SEC = 5.0                          # 开柜/开门异常告警时延 ≤ 5 秒

# ---- 1.4 试卷柜电子锁：电子锁 + 开柜记录 + 柜内状态监测 ----
SAFE_OPEN_NORMAL = "开柜（授权）"
SAFE_OPEN_ABNORMAL = "开柜（未授权）"
SAFE_STATES = ("closed", "open", "ajar")         # 关 / 开 / 虚掩
SAFE_INNER_PROBES = ("paper_present", "tamper", "humidity")  # 柜内状态监测

# ---- 1.5 环境监测：温湿度 / 水浸 / 烟感 ----
ENV_THRESHOLDS = {
    "temp_c":      {"min": 5.0,  "max": 35.0, "unit": "℃", "text": "温度"},
    "humidity_pct": {"min": 20.0, "max": 70.0, "unit": "%",  "text": "相对湿度"},
}
ENV_BINARY_PROBES = {"water": "水浸", "smoke": "烟感"}
ENV_ALERT_ON_TRUE = ("water", "smoke")           # 这两项为真即告警


# ==================== 二、试卷流转环节 ====================

# ---- 2.1 环节定义（与文档表格逐条对应） ----
FLOW_STAGES = {
    "pickup":   {"name": "领取",     "desc": "保密室领出试卷袋"},
    "handover": {"name": "交接",     "desc": "双人签字 + 人脸确认 + 扫码/RFID"},
    "escort":   {"name": "押运",     "desc": "车辆定位 + 车内视频回传 + 路线偏离告警"},
    "deliver":  {"name": "分发至考场", "desc": "分发记录与考场/时间/责任人一一对应"},
    "recycle":  {"name": "回收",     "desc": "回收扫码核验数量"},
    "seal":     {"name": "封存",     "desc": "回收后封存入库并电子封签"},
}
# 交接必须双人（签字 + 人脸确认），缺一不可
HANDOVER_PERSONS_REQUIRED = 2
HANDOVER_METHODS = ("scan", "rfid")              # 扫码 / RFID 电子交接

# ---- 2.2 押运：车辆北斗/GPS 定位 + 车内视频回传 + 路线偏离告警 ----
ESCORT_DEVIATE_SLA_SEC = 30.0                    # 偏离即报，≤ 30 秒
ESCORT_DEVIATE_METERS = 200.0                    # 偏离规划路线 > 200m 判偏离
ESCORT_STOP_MAX_SEC = 600.0                      # 途中静止超过 10 分钟告警
ESCORT_VIDEO_REQUIRED = True                     # 车内视频必须回传
ESCORT_POSITION_SOURCES = ("beidou", "gps")      # 北斗优先

# ---- 2.3 电子铅封：试卷袋电子封签 ----
SEAL_STATES = ("sealed", "broken", "unknown")
SEAL_ALERT_ON_ABNORMAL_OPEN = True               # 异常开启即时告警

# ---- 2.4 分发至考场 ----
DELIVER_REQUIRED_FIELDS = ("bag_id", "room_id", "quantity", "person", "ts")


# ==================== 三、告警分级 ====================
LEVEL_ORANGE = "orange"
LEVEL_RED = "red"
LEVELS = (LEVEL_ORANGE, LEVEL_RED)
LEVEL_META = {
    LEVEL_ORANGE: {"name": "橙色预警", "color": "#e08b00", "sound": False,
                   "top": True, "popup": False, "force_confirm": False, "sla_sec": 300},
    LEVEL_RED:    {"name": "红色预警", "color": "#c00000", "sound": True,
                   "top": True, "popup": True, "force_confirm": True, "sla_sec": 5},
}

# 告警码 -> 分级兜底映射（真实分级由平台下发，这里只兜底）
ALERT_CODE_LEVEL = {
    # 保密室
    "SAFE_OPEN_ABNORMAL":   LEVEL_RED,      # 未授权开柜
    "DOOR_OPEN_ABNORMAL":   LEVEL_RED,      # 未授权开门
    "ACCESS_DUAL_FAIL":     LEVEL_RED,      # 双因子/双人校验失败
    "ACCESS_TAILGATING":    LEVEL_RED,      # 尾随进入
    "INTRUSION_IR":         LEVEL_RED,      # 红外双鉴
    "INTRUSION_DOOR_MAG":   LEVEL_RED,      # 门窗磁
    "INTRUSION_VIBRATION":  LEVEL_RED,      # 震动
    "ENV_WATER":            LEVEL_RED,      # 水浸
    "ENV_SMOKE":            LEVEL_RED,      # 烟感
    "ENV_TEMP_HIGH":        LEVEL_ORANGE,   # 温度超限
    "ENV_TEMP_LOW":         LEVEL_ORANGE,
    "ENV_HUMID_HIGH":       LEVEL_ORANGE,   # 湿度超限
    "ENV_HUMID_LOW":        LEVEL_ORANGE,
    "SAFE_OPEN_NORMAL":     LEVEL_ORANGE,   # 授权开柜（留痕用，不强制确认）
    "DOOR_OPEN_NORMAL":     LEVEL_ORANGE,
    "SAFE_AJAR":            LEVEL_ORANGE,   # 柜门虚掩
    "VIDEO_BLIND_SPOT":     LEVEL_ORANGE,   # 出现盲区
    "VIDEO_OFFLINE":        LEVEL_ORANGE,   # 机位离线
    "VIDEO_RECORD_BREAK":   LEVEL_ORANGE,   # 录像丢段
    "COVER_RATE_LOW":       LEVEL_ORANGE,   # 覆盖率不达标
    # 火场识别（从 02 移入）
    "FIRE_ALARM":           LEVEL_RED,      # 火焰确认
    "FIRE_SMOKE":           LEVEL_ORANGE,   # 烟雾疑似
    # 流转
    "SEAL_BROKEN":          LEVEL_RED,      # 电子铅封异常开启
    "ESCORT_ROUTE_DEVIATE": LEVEL_RED,      # 押运路线偏离
    "ESCORT_VIDEO_LOST":    LEVEL_RED,      # 车内视频中断
    "ESCORT_STOP_TOO_LONG": LEVEL_ORANGE,   # 途中异常停留
    "HANDOVER_PERSON_MISS": LEVEL_RED,      # 交接人数不足（无双人）
    "HANDOVER_FACE_MISS":   LEVEL_RED,      # 交接无人脸确认
    "HANDOVER_INCOMPLETE":  LEVEL_ORANGE,   # 交接记录缺字段
    "RECYCLE_COUNT_MISMATCH": LEVEL_RED,    # 回收数量差异
    "RECYCLE_MISSING":      LEVEL_ORANGE,   # 应回收未回收
    "DELIVER_RECORD_MISS":  LEVEL_ORANGE,   # 分发记录缺失
    "FLOW_ABNORMAL":        LEVEL_ORANGE,   # 流转异常兜底
}

ALERT_CODE_TEXT = {
    "SAFE_OPEN_ABNORMAL": "试卷柜未授权开启",
    "DOOR_OPEN_ABNORMAL": "保密室未授权开门",
    "ACCESS_DUAL_FAIL": "门禁双因子校验失败",
    "ACCESS_TAILGATING": "疑似尾随进入",
    "INTRUSION_IR": "红外双鉴入侵",
    "INTRUSION_DOOR_MAG": "门窗磁报警",
    "INTRUSION_VIBRATION": "震动探测报警",
    "ENV_WATER": "水浸告警",
    "ENV_SMOKE": "烟感告警",
    "ENV_TEMP_HIGH": "温度超上限",
    "ENV_TEMP_LOW": "温度超下限",
    "ENV_HUMID_HIGH": "湿度超上限",
    "ENV_HUMID_LOW": "湿度超下限",
    "SAFE_OPEN_NORMAL": "试卷柜授权开启",
    "DOOR_OPEN_NORMAL": "保密室授权开门",
    "SAFE_AJAR": "试卷柜门虚掩",
    "VIDEO_BLIND_SPOT": "视频出现盲区",
    "VIDEO_OFFLINE": "监控机位离线",
    "VIDEO_RECORD_BREAK": "录像连续性问题",
    "COVER_RATE_LOW": "六面覆盖率不达标",
    "FIRE_ALARM": "火焰确认（火场识别）",
    "FIRE_SMOKE": "烟雾疑似（火场识别）",
    "SEAL_BROKEN": "电子铅封异常开启",
    "ESCORT_ROUTE_DEVIATE": "押运路线偏离",
    "ESCORT_VIDEO_LOST": "押运车内视频中断",
    "ESCORT_STOP_TOO_LONG": "押运途中异常停留",
    "HANDOVER_PERSON_MISS": "交接未达双人要求",
    "HANDOVER_FACE_MISS": "交接缺人脸确认",
    "HANDOVER_INCOMPLETE": "交接记录不完整",
    "RECYCLE_COUNT_MISMATCH": "回收数量核验差异",
    "RECYCLE_MISSING": "应回收未回收",
    "DELIVER_RECORD_MISS": "分发记录缺失",
    "FLOW_ABNORMAL": "流转异常",
}


# ==================== 四、录像留存 ====================
# 按当地考务规定留存，建议 ≥ 6 个月；留存期内不可删改（WORM）。
RECORD_RETAIN_DAYS = 180                     # 建议 ≥ 6 个月
RECORD_WORM = True                           # 留存期内写入即锁定，不可删改
RECORD_GAP_TOLERANCE_SEC = 2.0               # 段间允许的最大缝隙（超过判丢段）
RECORD_SEGMENT_TARGET_HOURS = 1.0            # 期望段长（用于评估连续性）


# ==================== 五、关键指标（文档第三节） ====================
KPI_DEFS = {
    "coverage_rate":    {"name": "保密室视频覆盖率", "required": "100%，无盲区",
                         "op": ">=", "target": 1.0, "unit": "%"},
    "alarm_latency":    {"name": "开柜/开门异常告警时延", "required": "≤ 5 秒",
                         "op": "<=", "target": 5.0, "unit": "s"},
    "handover_complete": {"name": "交接记录完整率", "required": "100% 可追溯",
                         "op": ">=", "target": 1.0, "unit": "%"},
    "record_continuous": {"name": "录像留存与完整性", "required": "留存期内连续可回放，无丢段",
                         "op": ">=", "target": 1.0, "unit": "%"},
    "escort_deviate_latency": {"name": "押运路线偏离告警时延", "required": "偏离即报，≤ 30 秒",
                         "op": "<=", "target": 30.0, "unit": "s"},
}


# ==================== 六、角色与权限 ====================
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_GUARD = "guard"          # 保密室值守员
ROLE_ESCORT = "escort"        # 押运员
ROLE_AUDITOR = "auditor"      # 审计/考务监督
ROLE_VIEWER = "viewer"
ROLE_LEVEL = {ROLE_VIEWER: 1, ROLE_GUARD: 2, ROLE_ESCORT: 2,
              ROLE_AUDITOR: 3, ROLE_ADMIN: 3, ROLE_OWNER: 4}
ROLE_TEXT = {ROLE_OWNER: "所有者", ROLE_ADMIN: "管理员", ROLE_GUARD: "值守员",
             ROLE_ESCORT: "押运员", ROLE_AUDITOR: "考务监督", ROLE_VIEWER: "只读"}

ALL_ACTIONS = frozenset({
    "view",                    # 查看台账/告警/指标
    "device_manage",           # 登记/修改机位
    "live_view",               # 实时画面
    "monitor_operate",         # 布防/撤防、模拟事件注入
    "access_operate",          # 门禁开关门、发卡
    "safe_operate",            # 试卷柜开柜申请/授权
    "flow_operate",            # 交接/分发/回收登记
    "escort_operate",          # 押运任务与轨迹上报
    "alarm_handle",            # 告警确认与处置
    "report_export",           # 导出报告
    "change_own_pwd",
    "device_delete",           # 删除机位（仅所有者）
    "account_manage",          # 账户管理（仅所有者）
    "audit_view",              # 审计链（仅所有者）
    "hide_cmd_toggle",         # 隐藏命令行窗口（仅所有者）
})
ROLE_ACTIONS = {
    ROLE_OWNER:   set(ALL_ACTIONS),
    ROLE_ADMIN:   set(ALL_ACTIONS) - {"account_manage", "device_delete",
                                      "hide_cmd_toggle", "audit_view"},
    ROLE_AUDITOR: {"view", "live_view", "report_export", "audit_view"},
    ROLE_GUARD:   {"view", "live_view", "monitor_operate", "access_operate",
                   "safe_operate", "alarm_handle", "change_own_pwd"},
    ROLE_ESCORT:  {"view", "live_view", "escort_operate", "flow_operate",
                   "alarm_handle", "change_own_pwd"},
    ROLE_VIEWER:  {"view"},
}

# 默认账户（首次运行自动预置）
DEFAULT_ACCOUNTS = {
    "owner":    {"password": "EDY_POWERQ", "role": ROLE_OWNER},
    "teacher1": {"password": "teacher1",   "role": ROLE_ADMIN},
}
PWD_MIN_LEN = 6
PBKDF2_ROUNDS = 120000


# ==================== 七、实时监控 ====================
# 摄像头：默认先找本机设备（0/1/2），找不到再要求手填 RTSP 地址
LIVE_POLL_MS = 40                  # 界面刷新间隔（毫秒）
LIVE_PREVIEW_W = 640               # 预览缩放长边（降低 CPU）
SNAPSHOT_ON_ALARM = True           # 告警时自动抓帧留证
ALARM_SNAPSHOT_DIR = EVIDENCE_DIR
# 本机摄像头序号探测范围
LOCAL_CAM_INDEX_MAX = 6
# 默认测试流地址（无真机时可用于验证取流链路；留空表示不预置）
DEFAULT_RTSP = ""


# ==================== 八、AI 检测（复用 OCR/人形识别，可选） ====================
AI_SETTINGS_PATH = DATA_ROOT / "ai_settings.json"
AI_SETTINGS_DEFAULT = {
    "ai_mode": "standard",     # standard / dense
    "ai_input_dim": 416,
    "model_kind": "auto",      # auto / yolo / caffe
    "model_path": "",
    "prefer_gpu": True,
    "yolo_conf": 0.30,
    "detect_backend": "",      # 只读展示位
}


def load_ai_settings():
    import json
    cfg = dict(AI_SETTINGS_DEFAULT)
    try:
        if AI_SETTINGS_PATH.exists():
            data = json.loads(AI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in AI_SETTINGS_DEFAULT:
                    if k in data and data[k] not in (None, ""):
                        cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_ai_settings(settings):
    import json
    try:
        merged = load_ai_settings()
        merged.update({k: v for k, v in (settings or {}).items()
                       if k in AI_SETTINGS_DEFAULT})
        AI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        AI_SETTINGS_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False
