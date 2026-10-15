# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 配置

本模块把《普通考场应用.md》《特殊考场应用.md》两份方案落成**一套平台、多种模板**：

  普通考场应用.md  ->  POINT_SPECS / AI_CATALOG / ALERT_ROUTING / ACCEPTANCE 的普通口径
  特殊考场应用.md  ->  ROOM_TYPES 里 7 类特殊考场的差异项
                     （配置差异 / 算法参数差异 / 合规差异）

设计原则（照方案第二节"通用设计要求"）：
  1) 一套平台多种模板        ROOM_TYPES + TEMPLATE_FIELDS
  2) 降级可用                MIN_CONFIG_LEVELS + rooms.degrade_to_minimal
  3) 人工兜底                所有模板 manual_fallback=True；无障碍强制非人脸通道
  4) 分级告警                ALERT_POLICY + 各考场 confidence_min 覆盖
  5) 数据分域                DOMAINS/DOMAIN_RULES + privacy.py

数据默认落在本目录 data\，可用 EXAM_ROOM_HOME 重定向（与 01/02/03/06 同款做法）。
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_ROOM_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "room_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "rooms.db"
LOG_DIR = DATA_ROOT / "logs"
REPORT_DIR = DATA_ROOT / "reports"          # 逐考场准入报告
ATTACH_DIR = DATA_ROOT / "attachments"      # 自检/处置截图
DOMAIN_DIR = DATA_ROOT / "domains"          # 数据分域物理隔离落点
SETTINGS_PATH = DATA_ROOT / "settings.json"

for _d in (DATA_ROOT, LOG_DIR, REPORT_DIR, ATTACH_DIR, DOMAIN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"


def ensure_stdio(log_name="gui_console.log"):
    """pythonw.exe 启动时 stdout/stderr 为 None，兜到日志文件，避免 print 直接打挂。"""
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


# =============================================================================
# 一、考场类型（普通 + 特殊七类）
# =============================================================================
TYPE_STANDARD = "standard"
TYPE_BACKUP = "backup"
TYPE_ISOLATION = "isolation"
TYPE_ACCESSIBLE = "accessible"
TYPE_COMPUTER = "computer"
TYPE_ORAL = "oral"
TYPE_LARGE = "large"
TYPE_CLASSIFIED = "classified"

# 特殊考场集合（《特殊考场应用.md》第一节表格的七类）
SPECIAL_TYPES = (TYPE_BACKUP, TYPE_ISOLATION, TYPE_ACCESSIBLE,
                 TYPE_COMPUTER, TYPE_ORAL, TYPE_LARGE, TYPE_CLASSIFIED)

# 需求等级：必需 / 期望（缺了不挡启用，但出警告）
REQ_MUST = "must"
REQ_EXPECT = "expect"

# 能力来源（决定"缺了是阻断还是提示"，也决定怎么补齐）：
#   point  —— 有实体点位，配了就有（纯看配置快照）
#   job    —— 要**做一件事**才算有（DPIA / 演练 / 离线自检 / 分配置域 /
#             登记网络日志代理 …）。套模板后必然是"缺"的，这是正常的，
#             因为那件事还没做；补齐后即消失。
CAP_SRC_POINT = "point"
CAP_SRC_JOB = "job"


def _cap(name, level=REQ_MUST, src=CAP_SRC_POINT):
    return {"name": name, "level": level, "src": src}


ROOM_TYPES = {
    # ---------------- 普通考场（普通考场应用.md） ----------------
    TYPE_STANDARD: {
        "name": "普通考场",
        "short": "普通",
        "special": False,
        "scope": "标准化考点内的常规笔试考场（30 人/场，2 名监考员）",
        "seats_default": 30,
        "invigilators": 2,
        "points": [
            {"no": "前部全景机位", "where": "讲台侧上方", "angle": "仰角约 20°~25°",
             "target": "全班人脸/体态全景，用于群体行为", "required": True},
            {"no": "侧/后部特写机位", "where": "教室后侧或侧面", "angle": "俯角约 15°",
             "target": "考生手部与桌面特写，用于个体行为", "required": True},
            {"no": "通道机位", "where": "考场门口/走道", "angle": "双向覆盖",
             "target": "人员进出、试卷流转", "required": True},
            {"no": "拾音器", "where": "教室中部吊装",
             "angle": "全向 80Hz~16KHz、≥ -34dB、覆盖 80 ㎡",
             "target": "交谈声、耳机反馈音", "required": True},
            {"no": "备用机位", "where": "与主特写同向", "angle": "冗余",
             "target": "单机故障不停巡", "required": False},
        ],
        "dev_spec": "800 万像素、全局快门、宽温、0.002Lux",
        "points_min": 3,              # 最小可用配置：1 机位 + 1 链路（见 MIN_CONFIG_LEVELS）
        "api_mix": ("前部全景机位", "侧/后部特写机位", "通道机位"),
        "mic": {"no": "拾音器", "level": REQ_MUST,
                "spec": "全向 80Hz~16KHz、≥ -34dB、覆盖 80 ㎡"},
        "extras": [_cap("备用机位（单机故障不停巡）", REQ_EXPECT)],
        "caps": {
            "multi_angle": _cap("多机位覆盖（全景+特写+通道）"),
            "audio": _cap("拾音覆盖（交谈声/耳机反馈音）", REQ_EXPECT),
            "redundant_camera": _cap("备用机位冗余", REQ_EXPECT),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "普通考场全量启用 44 种行为"},
        "params": {},                  # 普通考场为基准，无差异参数
        "alerts": {},
        "compliance": {
            "data_domain": "exam",
            "domain_isolated": False,
            "min_retention_days": 30,
            "dpia_required": False,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "行为识别准确率 ≥95%、告警响应 ≤3 秒、覆盖 100% 无盲区",
        },
        "scenarios": ["常规笔试", "标准化考点"],
        "identity_selfcheck": True,   # 普通考场要演示人脸核验通道（非硬门槛）
    },

    # ---------------- 备用考场 ----------------
    TYPE_BACKUP: {
        "name": "备用考场",
        "short": "备用",
        "special": True,
        "scope": "迟到、突发疾病、违规带离后继续考试",
        "seats_default": 5,
        "invigilators": 1,
        "points": [
            {"no": "前部全景机位", "where": "讲台侧上方", "angle": "仰角约 20°",
             "target": "全班全景（人少，特写权重可提高）", "required": True},
            {"no": "侧/后部特写机位", "where": "教室侧面", "angle": "俯角约 15°",
             "target": "个体行为特写", "required": True},
            {"no": "通道机位", "where": "备用考场门口", "angle": "双向覆盖",
             "target": "人员进出", "required": True},
            {"no": "拾音器", "where": "教室中部", "angle": "全向", "target": "交谈声",
             "required": False},
        ],
        "dev_spec": "设备预置、即插即用；1 人 1 机位也要覆盖",
        "points_min": 1,
        "api_mix": ("前部全景机位", "侧/后部特写机位", "通道机位"),
        "mic": {"no": "拾音器", "level": REQ_EXPECT, "spec": "全向"},
        "extras": [_cap("设备预置即插即用（快速启用）"),
                   _cap("1 人 1 机位覆盖", REQ_EXPECT)],
        "caps": {
            "multi_angle": _cap("多机位覆盖（含特写）"),
            "fast_enable": _cap("即插即用预置（临时启用不停巡）", REQ_MUST, CAP_SRC_JOB),
            "audio": _cap("拾音覆盖", REQ_EXPECT),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "座位少、区域小，提高特写权重：特写机位行为置信度 ×1.15"},
        "params": {
            "closeup_weight": 1.15,
            "algo_weights": {"个体行为": 1.2, "群体行为": 0.8},
        },
        "alerts": {},
        "compliance": {
            "data_domain": "exam",
            "domain_isolated": False,
            "min_retention_days": 30,
            "dpia_required": False,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "与普通考场一致",
        },
        "scenarios": ["迟到", "突发疾病", "违规带离后继续考试"],
        "identity_selfcheck": False,
    },

    # ---------------- 隔离考场 ----------------
    TYPE_ISOLATION: {
        "name": "隔离考场",
        "short": "隔离",
        "special": True,
        "scope": "发热、传染风险、健康异常",
        "seats_default": 4,
        "invigilators": 0,            # 现场人员最少化，改远程在岗
        "remote_invigilators": 2,
        "points": [
            {"no": "前部全景机位", "where": "隔离室讲台侧", "angle": "仰角约 20°",
             "target": "独立机位、独立通道画面", "required": True},
            {"no": "侧/后部特写机位", "where": "隔离室侧后方", "angle": "俯角约 15°",
             "target": "个体行为特写", "required": True},
            {"no": "通道机位", "where": "独立通道口", "angle": "双向覆盖",
             "target": "独立通道进出管控", "required": True},
            {"no": "远程监考视讯", "where": "考生位正对", "angle": "正面",
             "target": "远程监考员在岗检测 + 医生协同", "required": True},
            {"no": "拾音器", "where": "室内中部", "angle": "全向", "target": "交谈/异常声响",
             "required": True},
        ],
        "dev_spec": "独立机位 + 独立通风 + 独立通道；支持远程监考与医生协同",
        "points_min": 1,
        "api_mix": ("前部全景机位", "侧/后部特写机位", "通道机位", "远程监考视讯"),
        "mic": {"no": "拾音器", "level": REQ_MUST, "spec": "全向"},
        "extras": [_cap("独立通风"), _cap("独立通道"), _cap("医生协同通道")],
        "caps": {
            "multi_angle": _cap("独立机位覆盖"),
            "remote_invigilate": _cap("远程监考在岗检测", REQ_MUST, CAP_SRC_JOB),
            "doctor_link": _cap("医生协同（现场/远程）", REQ_MUST, CAP_SRC_JOB),
            "audio": _cap("拾音覆盖"),
            "privacy_domain": _cap("健康信息独立分区（不与其他数据混库）", REQ_MUST, CAP_SRC_JOB),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "监考员远程在岗检测加强；减少现场人员"},
        "params": {
            "remote_invigilator_check": True,   # 监考员在岗检测按远程口径
            "onsite_personnel_min": True,       # 现场人员最少化
            "confidence_min": 0.70,             # 误报容忍度低于普通考场
        },
        "alerts": {"suppress_nominal": ["安全冗余/多机位一致性提示"],
                   "push_extra": ["考点医生（远程）"]},
        "compliance": {
            "data_domain": "health",
            "domain_isolated": True,
            "min_retention_days": 15,
            "dpia_required": True,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "健康信息属敏感个人信息：单独存储、更高保护等级、不与其他数据混库",
        },
        "scenarios": ["发热", "传染风险", "健康异常"],
        "identity_selfcheck": False,
    },

    # ---------------- 无障碍/特殊需求考场 ----------------
    TYPE_ACCESSIBLE: {
        "name": "无障碍/特殊需求考场",
        "short": "无障碍",
        "special": True,
        "scope": "视力/听力/肢体障碍、佩戴医疗器械",
        "seats_default": 10,
        "invigilators": 2,
        "points": [
            {"no": "低位核验终端", "where": "门口低位安装",
             "angle": "高度可调、坐姿可核验",
             "target": "轮椅/低位核验，避免抬高动作", "required": True},
            {"no": "前部全景机位", "where": "讲台侧上方", "angle": "仰角约 20°",
             "target": "全班全景", "required": True},
            {"no": "加宽通道机位", "where": "加宽通道口", "angle": "双向覆盖",
             "target": "加宽通道进出（轮椅通行）", "required": True},
            {"no": "语音+文字双提示终端", "where": "考生位可视范围",
             "angle": "语音播报 + 屏幕文字", "target": "听力/视力障碍提示送达",
             "required": True},
            {"no": "人工核验工位", "where": "通道旁",
             "angle": "监考员操作位", "target": "免刷脸替代核验通道", "required": True},
        ],
        "dev_spec": "低位核验终端、加宽通道、语音+文字双提示、人工核验通道",
        "points_min": 1,
        "api_mix": ("前部全景机位", "加宽通道机位"),
        "mic": {"no": "语音提示终端（含拾音）", "level": REQ_MUST, "spec": "语音播报"},
        "extras": [_cap("加宽通道"), _cap("低位设备"),
                   _cap("语音+文字双提示")],
        "caps": {
            "multi_angle": _cap("多机位覆盖"),
            "alt_verify": _cap("免刷脸替代核验（人工核验与智能核验同效）"),
            "non_face_channel": _cap("非人脸替代通道（不得因技术原因拒考）"),
            "accessibility": _cap("低位/加宽/双提示无障碍配置"),
            "drill": _cap("替代通道演练记录", REQ_MUST, CAP_SRC_JOB),
            "audio": _cap("语音提示覆盖"),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "免刷脸替代核验，人工核验与智能核验同效"},
        "params": {
            "face_required": False,         # 人脸不得作为唯一准入
            "non_face_fallback": True,
            "identity_mode": "manual-first",
            "algo_weights": {"个体行为": 1.0},   # 肢体障碍动作差异大，不按普通口径加严
        },
        "alerts": {"suppress_nominal": ["长时间低头（肢体障碍/助视器）",
                                       "起立/站立（行动不便）"]},
        "compliance": {
            "data_domain": "exam",
            "domain_isolated": False,
            "min_retention_days": 30,
            "dpia_required": False,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "manual-first",
            "drill_required": True,          # 必须"有演练记录"
            "acceptance_note": "替代核验通道强制具备并有演练记录；不得因技术原因拒考",
        },
        "scenarios": ["视力障碍", "听力障碍", "肢体障碍", "佩戴医疗器械"],
        "identity_selfcheck": True,
    },

    # ---------------- 机考/计算机化考场 ----------------
    TYPE_COMPUTER: {
        "name": "机考/计算机化考场",
        "short": "机考",
        "special": True,
        "scope": "计算机化考试、上机操作科目",
        "seats_default": 40,
        "invigilators": 2,
        "points": [
            {"no": "正面机位", "where": "考生位正前方", "angle": "正面平视",
             "target": "考生正脸与整体姿态", "required": True},
            {"no": "侧后方 45° 机位", "where": "考生侧后方 45°", "angle": "俯角约 30°",
             "target": "屏幕内容 + 手部操作", "required": True},
            {"no": "通道机位", "where": "机房门口", "angle": "双向覆盖",
             "target": "人员进出", "required": True},
            {"no": "屏幕行为审计代理", "where": "考生机（软件代理）",
             "angle": "切屏/进程/窗口日志",
             "target": "屏幕切屏、异常进程、多窗口切换", "required": True},
        ],
        "dev_spec": "双机位（正面 + 侧后方 45°）、屏幕行为审计、网络行为日志",
        "points_min": 1,
        "api_mix": ("正面机位", "侧后方 45° 机位", "通道机位"),
        "mic": {"no": "拾音器", "level": REQ_EXPECT, "spec": "全向"},
        "extras": [_cap("网络行为日志")],
        "caps": {
            "dual_camera": _cap("双机位（正面 + 侧后方 45°）"),
            "screen_audit": _cap("屏幕行为审计（切屏/异常进程/多窗口）", REQ_MUST, CAP_SRC_JOB),
            "network_log": _cap("网络行为日志", REQ_MUST, CAP_SRC_JOB),
            "multi_angle": _cap("多机位覆盖"),
            "audio": _cap("拾音覆盖", REQ_EXPECT),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "屏幕切屏、异常进程、多窗口切换识别"},
        "params": {
            "screen_audit_required": True,
            "screen_cut_alert_level": "red",     # 切屏属重点
            "algo_weights": {"屏幕行为": 1.3},
        },
        "alerts": {"push_extra": ["网络管理员（异常进程/外联）"]},
        "compliance": {
            "data_domain": "exam_content",
            "domain_isolated": True,
            "min_retention_days": 30,
            "dpia_required": True,
            "offline_required": False,
            "offline_mode": "断网降级（本地作答继续）",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "屏幕录制含作答内容，属考试数据，严格保密；独立分区",
        },
        "scenarios": ["计算机化考试", "上机操作科目"],
        "identity_selfcheck": False,
    },

    # ---------------- 口试/小语种/面试考场 ----------------
    TYPE_ORAL: {
        "name": "口试/小语种/面试考场",
        "short": "口试",
        "special": True,
        "scope": "外语口试、面试、技能测试",
        "seats_default": 1,
        "invigilators": 2,
        "points": [
            {"no": "考生正面机位", "where": "考生正前方", "angle": "正面平视",
             "target": "考生正面与口型", "required": True},
            {"no": "考官机位", "where": "考官侧", "angle": "侧向",
             "target": "考官与考生互动", "required": True},
            {"no": "高保真录音", "where": "考生位独立拾音",
             "angle": "≥ 48kHz 或按要求", "target": "音频质量与存档",
             "required": True},
            {"no": "通道机位", "where": "候考室/考场门口", "angle": "双向覆盖",
             "target": "候考与进出", "required": True},
        ],
        "dev_spec": "高保真录音（≥ 48kHz 或按要求）、多机位、独立拾音",
        "points_min": 1,
        "api_mix": ("考生正面机位", "考官机位", "通道机位"),
        "mic": {"no": "高保真录音", "level": REQ_MUST, "spec": "≥ 48kHz 或按要求"},
        "extras": [_cap("独立拾音"), _cap("音频存档链路"),
                   _cap("录音留存按考务规定")],
        "caps": {
            "hifi_audio": _cap("高保真录音（≥ 48kHz）与存档", REQ_MUST, CAP_SRC_JOB),
            "independent_mic": _cap("独立拾音（减少串音）", REQ_MUST, CAP_SRC_JOB),
            "multi_angle": _cap("多机位覆盖"),
            "audio": _cap("音频链路重点"),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "音频质量与存档为重点，行为识别权重降低"},
        "params": {
            "algo_weights": {"个体行为": 0.7, "群体行为": 0.5, "音频质量": 2.0},
            "audio_sample_rate_min": 48000,
            "audio_check_required": True,
        },
        "alerts": {"suppress_nominal": ["长时间低头（看试题纸）"]},
        "compliance": {
            "data_domain": "exam",       # 声音属个人信息，但归考试记录域
            "domain_isolated": False,
            "min_retention_days": 30,
            "dpia_required": False,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "录音留存按考务规定；注意考生声音属个人信息",
        },
        "scenarios": ["外语口试", "面试", "技能测试"],
        "identity_selfcheck": False,
    },

    # ---------------- 大教室/合班考场 ----------------
    TYPE_LARGE: {
        "name": "大教室/合班考场",
        "short": "大教室",
        "special": True,
        "scope": "超过标准人数（60-120 人）",
        "seats_default": 90,
        "invigilators": 3,
        "points": [
            {"no": "前部全景机位", "where": "讲台侧上方", "angle": "仰角约 25°",
             "target": "前区全景", "required": True},
            {"no": "中部全景机位", "where": "教室中部吊装", "angle": "俯角约 30°",
             "target": "中区全景（分区管理）", "required": True},
            {"no": "后部全景机位", "where": "教室后方", "angle": "俯角约 25°",
             "target": "后区全景", "required": True},
            {"no": "侧/后部特写机位", "where": "侧面", "angle": "俯角约 15°",
             "target": "个体行为特写", "required": False},
            {"no": "通道机位", "where": "两个门口", "angle": "双向覆盖",
             "target": "人员进出", "required": True},
        ],
        "dev_spec": "多机位拼接或阵列部署，分区管理",
        "points_min": 2,
        "api_mix": ("前部全景机位", "中部全景机位", "后部全景机位", "通道机位"),
        "mic": {"no": "拾音器阵列", "level": REQ_EXPECT, "spec": "分区覆盖"},
        "extras": [_cap("分区管理"), _cap("跨机位去重计数")],
        "caps": {
            "multi_angle": _cap("多机位阵列覆盖（分前/中/后区）"),
            "zone_partition": _cap("分区管理（按分区告警）", REQ_EXPECT),
            "dedup": _cap("跨机位去重计数（避免重复误报）", REQ_MUST, CAP_SRC_JOB),
            "audio": _cap("拾音覆盖", REQ_EXPECT),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "跨机位去重计数，按分区告警，避免重复误报"},
        "params": {
            "dedup_required": True,
            "zone_alert": True,
            "algo_weights": {"群体行为": 1.2},
        },
        "alerts": {"dedup_window_sec": 20,
                   "note": "同人去重窗口内跨机位只报一次"},
        "compliance": {
            "data_domain": "exam",
            "domain_isolated": False,
            "min_retention_days": 30,
            "dpia_required": False,
            "offline_required": False,
            "offline_mode": "断网降级",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "大教室/机考考场另行测试：去重准确率 ≥95%",
        },
        "scenarios": ["合班考试", "60-120 人"],
        "identity_selfcheck": False,
    },

    # ---------------- 涉密/专项考试 ----------------
    TYPE_CLASSIFIED: {
        "name": "涉密/专项考试",
        "short": "涉密",
        "special": True,
        "scope": "高保密等级考试",
        "seats_default": 20,
        "invigilators": 2,
        "points": [
            {"no": "前部全景机位", "where": "讲台侧上方", "angle": "仰角约 20°",
             "target": "全景", "required": True},
            {"no": "侧/后部特写机位", "where": "教室侧后方", "angle": "俯角约 15°",
             "target": "个体行为", "required": True},
            {"no": "通道机位", "where": "专用通道口", "angle": "双向覆盖",
             "target": "专用链路进出", "required": True},
        ],
        "dev_spec": "全离线部署，数据不出考点，专用设备与专用链路",
        "points_min": 1,
        "api_mix": ("前部全景机位", "侧/后部特写机位", "通道机位"),
        "mic": {"no": "拾音器", "level": REQ_EXPECT, "spec": "全向"},
        "extras": [_cap("专用设备与专用链路"), _cap("专项审计与保密审批")],
        "caps": {
            "multi_angle": _cap("多机位覆盖"),
            "offline": _cap("全离线部署（断网仍可独立运行）", REQ_MUST, CAP_SRC_JOB),
            "no_external": _cap("算法本地运行、不上传云端", REQ_MUST, CAP_SRC_JOB),
            "privacy_domain": _cap("涉密数据独立分区 + 专项审计", REQ_MUST, CAP_SRC_JOB),
            "audio": _cap("拾音覆盖", REQ_EXPECT),
        },
        "algo": {"individual": 12, "group": 8, "invigilator": 20, "protocol": 4,
                 "note": "算法本地运行，不上传云端"},
        "params": {
            "require_offline": True,
            "no_cloud": True,
            "confidence_min": 0.75,          # 误报容忍度最低
            "external_call_allowed": False,  # 不得调用考点外的服务
        },
        "alerts": {"suppress_nominal": ["轻微姿态类提示"],
                   "push_extra": ["保密员"]},
        "compliance": {
            "data_domain": "classified",
            "domain_isolated": True,
            "min_retention_days": 60,
            "dpia_required": True,
            "offline_required": True,
            "offline_mode": "全离线可独立运行",
            "identity_mode": "face+manual",
            "drill_required": False,
            "acceptance_note": "数据不出域；专项审计与保密审批；全离线可独立运行",
        },
        "scenarios": ["高保密等级考试", "专项考试"],
        "identity_selfcheck": False,
    },
}

TYPE_ORDER = (TYPE_STANDARD, TYPE_BACKUP, TYPE_ISOLATION, TYPE_ACCESSIBLE,
              TYPE_COMPUTER, TYPE_ORAL, TYPE_LARGE, TYPE_CLASSIFIED)

# 模板可一键套用的字段（《特殊考场应用.md》第二节第 1 条）
TEMPLATE_FIELDS = ("points", "algo", "params", "alerts", "compliance",
                   "identity_selfcheck", "mic", "extras")


def type_name(t):
    return ROOM_TYPES.get(t, {}).get("name", t)


def type_is_special(t):
    return bool(ROOM_TYPES.get(t, {}).get("special"))


def room_type_list():
    """给 GUI/接口用的模板清单。"""
    out = []
    for t in TYPE_ORDER:
        r = ROOM_TYPES[t]
        out.append({
            "type": t, "name": r["name"], "short": r["short"], "special": r["special"],
            "scope": r["scope"], "seats_default": r["seats_default"],
            "points": len(r["points"]),
            "points_min": r["points_min"],
            "algo_total": sum(r["algo"][k] for k in ("individual", "group",
                                                     "invigilator", "protocol")),
            "data_domain": r["compliance"]["data_domain"],
            "domain_isolated": r["compliance"]["domain_isolated"],
            "offline_required": r["compliance"]["offline_required"],
            "drill_required": r["compliance"]["drill_required"],
            "cap_musts": sorted(k for k, v in r["caps"].items() if v["level"] == REQ_MUST),
            "scenarios": r["scenarios"],
        })
    return out


# =============================================================================
# 二、考试场次与生命周期
# =============================================================================
PHASE_APPLY = "apply"          # 启用申请
PHASE_TEMPLATE = "template"    # 套用模板
PHASE_SELFCHECK = "selfcheck"  # 机位自检（含质量诊断）
PHASE_ROLE = "role"            # 监考/巡考在岗核验
PHASE_DPIA = "dpia"            # 个人信息保护影响评估（敏感场景）
PHASE_ARMED = "armed"          # 已就绪，可开考
PHASE_DEGRADED = "degraded"    # 降级可用（最小可用配置）
PHASE_SUSPENDED = "suspended"  # 停用
PHASE_CLOSED = "closed"        # 封存

PHASES = (PHASE_APPLY, PHASE_TEMPLATE, PHASE_SELFCHECK, PHASE_ROLE, PHASE_DPIA,
          PHASE_ARMED, PHASE_DEGRADED, PHASE_SUSPENDED, PHASE_CLOSED)

PHASE_TEXT = {
    PHASE_APPLY: "启用申请",
    PHASE_TEMPLATE: "已套模板",
    PHASE_SELFCHECK: "机位自检中",
    PHASE_ROLE: "在岗核验中",
    PHASE_DPIA: "待评估（DPIA）",
    PHASE_ARMED: "已就绪",
    PHASE_DEGRADED: "降级可用",
    PHASE_SUSPENDED: "已停用",
    PHASE_CLOSED: "已封存",
}

# 强制流程顺序（《特殊考场应用.md》第三节 + 第五节风险提示）
FORCED_FLOW = (PHASE_APPLY, PHASE_TEMPLATE, PHASE_SELFCHECK, PHASE_ROLE, PHASE_ARMED)

SESSION_PRE = "pre"            # 考前
SESSION_RUNNING = "running"    # 考试中
SESSION_DONE = "done"          # 考试结束
SESSION_STATES = (SESSION_PRE, SESSION_RUNNING, SESSION_DONE)
SESSION_TEXT = {SESSION_PRE: "考前", SESSION_RUNNING: "考试中", SESSION_DONE: "已结束"}

# =============================================================================
# 三、准入判据（启用自检）
# =============================================================================
SEV_BLOCK = "block"     # 阻断项：不通过则不得启用
SEV_WARN = "warn"       # 警告项：可放行但必须留痕
SEV_INFO = "info"

# 准入缺陷目录：code -> (中文, 严重级)
DEFECTS = {
    # 模板与流程
    "TEMPLATE_MISSING":     ("未套用考场模板（逐场手工调参风险）", SEV_BLOCK),
    "FLOW_OUT_OF_ORDER":    ("未按强制流程顺序推进（启用申请→套模板→机位自检→在岗核验）", SEV_BLOCK),
    "APPLY_MISSING":        ("缺少启用申请记录（考务组）", SEV_BLOCK),
    "APPLY_REASON_MISSING": ("启用申请未写原因（临时启用风险）", SEV_WARN),
    # 机位与覆盖
    "POINTS_INSUFFICIENT":  ("机位数量低于该考场类型的最小配置", SEV_BLOCK),
    "POINT_MISSING":        ("必需点位缺失（该考场模板要求）", SEV_BLOCK),
    "COVERAGE_BLIND":       ("覆盖存在盲区（点位未覆盖全部座位区/通道）", SEV_BLOCK),
    "COVERAGE_UNVERIFIED":  ("覆盖完整性未逐机位核查留档", SEV_WARN),
    "CAPABILITY_MISSING":   ("该考场类型的必需能力未配置", SEV_BLOCK),
    "CAPABILITY_WEAK":      ("期望能力未配置（不影响启用，建议补齐）", SEV_WARN),
    "CAP_JOB_PENDING":      ("准入前置的待办事项未完成（DPIA / 演练 / 离线自检 / 去重验证 / 登记日志代理 / 设备预置 等）", SEV_BLOCK),
    # 画质（来自 02）
    "QUALITY_FAIL":         ("机位画质诊断不通过（含遮挡/视角偏移）", SEV_BLOCK),
    "QUALITY_WARN":         ("机位画质告警项（可启用但需复核）", SEV_WARN),
    "QUALITY_UNCHECKED":    ("机位未完成画质自检", SEV_BLOCK),
    "CAMERA_UNREGISTERED":  ("机位编码不在摄像头核查台账（该机位无画质证据，不得假装通过）", SEV_BLOCK),
    "LINKAGE_UNAVAILABLE":  ("画质诊断链路不可用，无法完成机位自检", SEV_BLOCK),
    # 时钟与台账
    "CLOCK_DRIFT":          ("摄像机与平台时钟偏差 > 1 秒", SEV_WARN),
    "LEDGER_MISSING":       ("未录入座位表与考生编号映射（无法行为—座位—考生定位）", SEV_WARN),
    "LEDGER_MISMATCH":      ("座位表/考生编号映射与考场不符", SEV_WARN),
    "LEDGER_NAME_EXPOSED":  ("座位映射录入含考生姓名（应仅编号）", SEV_BLOCK),
    "CALIBRATION_MISSING":  ("未形成逐考场标定记录（机位/角度/区域）", SEV_WARN),
    # 人员
    "ROLE_MISSING":         ("监考/巡考在岗未核验", SEV_BLOCK),
    "ROLE_COUNT_LOW":       ("监考员数量低于该考场类型要求", SEV_WARN),
    "REMOTE_INVIGILATOR_MISSING": ("隔离考场远程监考员未在岗", SEV_BLOCK),
    # 数据与合规
    "DOMAIN_UNISOLATED":    ("敏感数据未独立分区（禁止跨域汇聚）", SEV_BLOCK),
    "DOMAIN_MIXED":         ("检测到跨域数据混放（隔离/机考/涉密必须独立分区）", SEV_BLOCK),
    "DPIA_MISSING":         ("未完成个人信息保护影响评估（DPIA）即启用", SEV_BLOCK),
    "RETENTION_MISSING":    ("未设置最短必要保存期限与销毁计划", SEV_WARN),
    # 特殊考场专项
    "ALT_CHANNEL_MISSING":  ("无障碍考场缺少非人脸替代核验通道（不得因技术原因拒考）", SEV_BLOCK),
    "DRILL_MISSING":        ("无障碍替代通道无演练记录（强制项）", SEV_BLOCK),
    "OFFLINE_UNVERIFIED":   ("涉密考场未完成全离线自检（断网可独立运行）", SEV_BLOCK),
    "OFFLINE_UNSUPPORTED":  ("当前配置依赖外部服务，不满足涉密全离线要求", SEV_BLOCK),
    "DEDUP_UNVERIFIED":     ("大教室/机考未验证跨机位去重（重复误报风险）", SEV_WARN),
    "SCREEN_AUDIT_MISSING": ("机考考场未启用屏幕行为审计", SEV_BLOCK),
    "AUDIO_QUALITY_LOW":    ("口试考场音频采样率低于要求", SEV_BLOCK),
    "MIN_CONFIG_USED":      ("已降级为最小可用配置（1 机位 + 1 链路）", SEV_WARN),
    "SELFCHECK_OK":         ("机位自检全部通过", SEV_INFO),
}

BLOCK_CODES = {k for k, (_, s) in DEFECTS.items() if s == SEV_BLOCK}
WARN_CODES = {k for k, (_, s) in DEFECTS.items() if s == SEV_WARN}


def defect_name(code):
    return DEFECTS.get(code, (code, SEV_WARN))[0]


def defect_sev(code):
    return DEFECTS.get(code, (code, SEV_WARN))[1]


# 最小可用配置（《特殊考场应用.md》第二节第 2 条）
MIN_CONFIG_LEVELS = {
    "full":    {"name": "标准配置", "min_points": None, "min_links": 2,
                "desc": "按模板全量点位"},
    "reduced": {"name": "精简配置", "min_points": 2, "min_links": 1,
                "desc": "保留必需点位，允许缺期望点位"},
    "minimal": {"name": "最小可用配置", "min_points": 1, "min_links": 1,
                "desc": "1 台摄像机 + 1 条链路也能完成巡考并留痕"},
}
MIN_CONFIG_ORDER = ("full", "reduced", "minimal")
# 只有这些考场允许降到最小可用配置（特殊考场临时启用）
MINIMAL_ALLOWED_TYPES = (TYPE_BACKUP, TYPE_ISOLATION, TYPE_ACCESSIBLE,
                         TYPE_CLASSIFIED)

# =============================================================================
# 四、AI 识别清单（普通考场全量启用）
# =============================================================================
CAT_INDIVIDUAL = "individual"
CAT_GROUP = "group"
CAT_INVIGILATOR = "invigilator"
CAT_PROTOCOL = "protocol"
CAT_SCREEN = "screen"
CAT_AUDIO = "audio"

CATEGORIES = {
    CAT_INDIVIDUAL: "考生个体行为",
    CAT_GROUP: "考生群体行为",
    CAT_INVIGILATOR: "监考员行为",
    CAT_PROTOCOL: "考务规范行为",
    CAT_SCREEN: "屏幕行为（机考）",
    CAT_AUDIO: "音频质量（口试）",
}

# 普通考场：个体 12 + 群体 8 + 监考员 20 + 考务规范 4 = 44 种（与方案一致）
BEHAVIORS = [
    # ---- 考生个体行为 12 种 ----
    ("B_PEER_PEEK",        "旁窥",           CAT_INDIVIDUAL, "red",    ("side_glance", "sustained")),
    ("B_WHISPER",          "交头接耳",       CAT_INDIVIDUAL, "orange", ("talk", "sustained")),
    ("B_PASS_ITEM",        "传递物品",       CAT_INDIVIDUAL, "red",    ("handover",)),
    ("B_USE_PHONE",        "使用手机",       CAT_INDIVIDUAL, "red",    ("object", "vlm")),
    ("B_READ_NOTE",        "翻看纸条",       CAT_INDIVIDUAL, "red",    ("object", "vlm")),
    ("B_TURN_HEAD",        "频繁转头",       CAT_INDIVIDUAL, "orange", ("side_glance", "frequent")),
    ("B_LOOK_DOWN",        "长时间低头",     CAT_INDIVIDUAL, "orange", ("posture", "sustained")),
    ("B_STAND_UP",         "起立/站立",      CAT_INDIVIDUAL, "orange", ("posture",)),
    ("B_LEAN_FORWARD",     "前倾/趴桌",      CAT_INDIVIDUAL, "orange", ("posture",)),
    ("B_GAZE_SHIFT",       "眼神游移",       CAT_INDIVIDUAL, "orange", ("gaze", "frequent")),
    ("B_SLEEPY",           "闭眼/趴睡",      CAT_INDIVIDUAL, "orange", ("posture", "sustained")),
    ("B_HAND_SIGNAL",      "手部异常动作",   CAT_INDIVIDUAL, "orange", ("hand", "frequent")),
    # ---- 考生群体行为 8 种 ----
    ("G_MULTI_TALK",       "多人聚集交谈",   CAT_GROUP, "red",    ("talk", "multi_person")),
    ("G_COLLECTIVE_TURN",  "集体转头",       CAT_GROUP, "orange", ("side_glance", "multi_person")),
    ("G_GROUP_ABNORMAL",   "群体性异常动作", CAT_GROUP, "red",    ("motion", "multi_person")),
    ("G_CHAIN_PASS",       "串传递物品",     CAT_GROUP, "red",    ("handover", "multi_person")),
    ("G_CROWD_GATHER",     "异常聚集",       CAT_GROUP, "orange", ("motion", "multi_person")),
    ("G_COLLECTIVE_STAND", "集体起立",       CAT_GROUP, "orange", ("posture", "multi_person")),
    ("G_NOISE_RISE",       "环境噪声骤升",   CAT_GROUP, "orange", ("audio",)),
    ("G_LEAVE_SEAT",       "多人同时离座",   CAT_GROUP, "orange", ("motion", "multi_person")),
    # ---- 监考员行为 20 种 ----
    ("I_ABSENT",           "监考缺位",       CAT_INVIGILATOR, "red",    ("position", "sustained")),
    ("I_LEAVE_POST",       "离岗/离场",      CAT_INVIGILATOR, "red",    ("position", "sustained")),
    ("I_PHONE",            "长时间看手机",   CAT_INVIGILATOR, "red",    ("object", "sustained")),
    ("I_READ",             "看书报",         CAT_INVIGILATOR, "orange", ("object", "sustained")),
    ("I_SIT",              "久坐不动",       CAT_INVIGILATOR, "orange", ("position", "sustained")),
    ("I_CHAT",             "监考员交谈",     CAT_INVIGILATOR, "orange", ("talk",)),
    ("I_SLEEP",            "打瞌睡",         CAT_INVIGILATOR, "red",    ("posture", "sustained")),
    ("I_EAT",              "进食",           CAT_INVIGILATOR, "orange", ("object",)),
    ("I_BACK_TO_ROOM",     "背对考生区",     CAT_INVIGILATOR, "orange", ("position", "sustained")),
    ("I_LEAVE_DOOR",       "门口长时间逗留", CAT_INVIGILATOR, "orange", ("position", "sustained")),
    ("I_PHONE_CARRY",      "携带手机入场未存", CAT_INVIGILATOR, "red",  ("object", "vlm")),
    ("I_ABNORMAL_MOVE",    "频繁走动",       CAT_INVIGILATOR, "orange", ("motion", "frequent")),
    ("I_TWO_ABSENT",       "双人同时缺位",   CAT_INVIGILATOR, "red",    ("position", "multi_person")),
    ("I_OUT_OF_ZONE",      "进入考生区异常", CAT_INVIGILATOR, "orange", ("position",)),
    ("I_BEND_OVER",        "长时间俯身看桌面", CAT_INVIGILATOR, "orange", ("posture", "sustained")),
    ("I_NO_PATROL",        "未执行巡场",     CAT_INVIGILATOR, "orange", ("position",)),
    ("I_WHISPER_STUDENT",  "靠近考生低语",   CAT_INVIGILATOR, "red",    ("talk", "near_student")),
    ("I_READ_ANSWER",      "注视考生答卷异常长时间", CAT_INVIGILATOR, "red", ("gaze", "sustained")),
    ("I_UNIFORM_MISSING",  "未着监考标识",   CAT_INVIGILATOR, "orange", ("vlm",)),
    ("I_COUNT_MISMATCH",   "人数清点不符",   CAT_INVIGILATOR, "orange", ("count",)),
    # ---- 考务规范行为 4 种 ----
    ("P_PAPER_DISTRIBUTE", "试卷分发规范",   CAT_PROTOCOL, "orange", ("protocol",)),
    ("P_PAPER_COLLECT",    "试卷回收规范",   CAT_PROTOCOL, "red",    ("protocol",)),
    ("P_TIME_NODE",        "时间节点执行",   CAT_PROTOCOL, "orange", ("protocol",)),
    ("P_SEAL_PACKAGE",     "封装密封规范",   CAT_PROTOCOL, "red",    ("protocol",)),
]

# 机考/口试的附加行为（按模板启用，不计入普通考场 44 种）
EXTRA_BEHAVIORS = [
    ("S_SCREEN_SWITCH", "屏幕切屏",   CAT_SCREEN, "red",    ("screen", "sustained")),
    ("S_ABNORMAL_PROC", "异常进程",   CAT_SCREEN, "red",    ("process",)),
    ("S_MULTI_WINDOW",  "多窗口切换", CAT_SCREEN, "orange", ("screen", "frequent")),
    ("S_NETWORK_OUT",   "异常外联",   CAT_SCREEN, "red",    ("network",)),
    ("A_AUDIO_LOW",     "录音质量低", CAT_AUDIO,  "orange", ("audio_quality",)),
    ("A_AUDIO_SILENT",  "录音无声",   CAT_AUDIO,  "red",    ("audio_quality", "sustained")),
    ("A_CROSSTALK",     "串音/环境噪声", CAT_AUDIO, "orange", ("audio_quality",)),
]

# 行为 -> 告警码（与 03_智能终端操作 config.ALERT_CODE_LEVEL 兼容的码优先复用）
BEHAVIOR_ALERT_CODE = {
    "B_PEER_PEEK": "TURN_HEAD", "B_WHISPER": "PERSON_BEHAVIOR_ABNORMAL",
    "B_PASS_ITEM": "PERSON_BEHAVIOR_ABNORMAL", "B_USE_PHONE": "PHONE_DETECTED",
    "B_READ_NOTE": "PERSON_BEHAVIOR_ABNORMAL", "B_TURN_HEAD": "TURN_HEAD",
    "B_LOOK_DOWN": "LOOK_DOWN", "B_STAND_UP": "STAND_UP",
    "B_LEAN_FORWARD": "LEAN_FORWARD", "B_GAZE_SHIFT": "GAZE_SHIFT",
    "B_SLEEPY": "SLEEPY", "B_HAND_SIGNAL": "PERSON_BEHAVIOR_ABNORMAL",
    "G_MULTI_TALK": "PERSON_BEHAVIOR_ABNORMAL",
    "G_COLLECTIVE_TURN": "TURN_HEAD", "G_GROUP_ABNORMAL": "PERSON_BEHAVIOR_ABNORMAL",
    "G_CHAIN_PASS": "PERSON_BEHAVIOR_ABNORMAL", "G_CROWD_GATHER": "MULTI_FACE",
    "G_COLLECTIVE_STAND": "STAND_UP", "G_NOISE_RISE": "PERSON_BEHAVIOR_ABNORMAL",
    "G_LEAVE_SEAT": "PERSON_BEHAVIOR_ABNORMAL",
    "I_ABSENT": "NO_INVIGILATOR", "I_LEAVE_POST": "NO_INVIGILATOR",
    "I_PHONE": "PHONE_DETECTED", "I_READ": "PERSON_BEHAVIOR_ABNORMAL",
    "I_SIT": "PERSON_BEHAVIOR_ABNORMAL", "I_CHAT": "PERSON_BEHAVIOR_ABNORMAL",
    "I_SLEEP": "PERSON_BEHAVIOR_ABNORMAL", "I_EAT": "PERSON_BEHAVIOR_ABNORMAL",
    "I_BACK_TO_ROOM": "PERSON_BEHAVIOR_ABNORMAL",
    "I_LEAVE_DOOR": "PERSON_BEHAVIOR_ABNORMAL", "I_PHONE_CARRY": "PHONE_DETECTED",
    "I_ABNORMAL_MOVE": "PERSON_BEHAVIOR_ABNORMAL",
    "I_TWO_ABSENT": "NO_INVIGILATOR", "I_OUT_OF_ZONE": "PERSON_OUT_OF_BOUNDS",
    "I_BEND_OVER": "PERSON_BEHAVIOR_ABNORMAL", "I_NO_PATROL": "NO_INVIGILATOR",
    "I_WHISPER_STUDENT": "PERSON_BEHAVIOR_ABNORMAL",
    "I_READ_ANSWER": "PERSON_BEHAVIOR_ABNORMAL",
    "I_UNIFORM_MISSING": "PERSON_BEHAVIOR_ABNORMAL",
    "I_COUNT_MISMATCH": "MULTI_FACE",
    "P_PAPER_DISTRIBUTE": "PERSON_BEHAVIOR_ABNORMAL",
    "P_PAPER_COLLECT": "PERSON_BEHAVIOR_ABNORMAL",
    "P_TIME_NODE": "PERSON_BEHAVIOR_ABNORMAL",
    "P_SEAL_PACKAGE": "PERSON_BEHAVIOR_ABNORMAL",
    "S_SCREEN_SWITCH": "PERSON_BEHAVIOR_ABNORMAL",
    "S_ABNORMAL_PROC": "PERSON_BEHAVIOR_ABNORMAL",
    "S_MULTI_WINDOW": "PERSON_BEHAVIOR_ABNORMAL",
    "S_NETWORK_OUT": "SIGNAL_ABNORMAL",
    "A_AUDIO_LOW": "PERSON_BEHAVIOR_ABNORMAL",
    "A_AUDIO_SILENT": "PERSON_BEHAVIOR_ABNORMAL",
    "A_CROSSTALK": "PERSON_BEHAVIOR_ABNORMAL",
}

# 算法分类符合教育部《人工智能识别分析异常行为分类参考》的映射声明
MOE_CLASS_MAP = {
    CAT_INDIVIDUAL: "考生异常行为/个体",
    CAT_GROUP: "考生异常行为/群体",
    CAT_INVIGILATOR: "监考人员行为",
    CAT_PROTOCOL: "考务规范行为",
    CAT_SCREEN: "机考屏幕行为",
    CAT_AUDIO: "音频质量",
}


def behaviors_of(cat):
    return [b for b in BEHAVIORS + EXTRA_BEHAVIORS if b[2] == cat]


def behavior_dict():
    return {b[0]: {"code": b[0], "name": b[1], "category": b[2], "level": b[3],
                   "signals": list(b[4]), "cat_name": CATEGORIES[b[2]],
                   "moe": MOE_CLASS_MAP[b[2]]}
            for b in BEHAVIORS + EXTRA_BEHAVIORS}


# =============================================================================
# 五、分级告警与推送（普通考场应用.md 第四节）
# =============================================================================
LEVEL_ORANGE = "orange"
LEVEL_RED = "red"
LEVELS = (LEVEL_ORANGE, LEVEL_RED)
LEVEL_META = {
    LEVEL_ORANGE: {"name": "橙色预警", "color": "#e08b00", "force_confirm": False,
                   "sla_sec": 300},
    LEVEL_RED:    {"name": "红色预警", "color": "#c00000", "force_confirm": True,
                   "sla_sec": 60},
}
LEVEL_ORDER = {LEVEL_ORANGE: 1, LEVEL_RED: 2}

# 普通考场推送对象（方案第四节表格，原样落地）
ALERT_ROUTING = {
    LEVEL_ORANGE: ["监考终端（即时提示）", "考点巡考员"],
    LEVEL_RED:    ["考点巡考员", "上级巡考平台"],
    "response": {LEVEL_ORANGE: "当场提醒、记录", LEVEL_RED: "即时介入、留存证据"},
}

# 特殊考场推送差异（《特殊考场应用.md》第一节"合规差异"+第二节第 4 条）
SPECIAL_PUSH = {
    TYPE_BACKUP:     {"extra": ["备用考场监考终端"], "note": "与普通考场一致"},
    TYPE_ISOLATION:  {"extra": ["远程监考员", "考点医生（远程）"],
                      "remove": ["现场监考终端（现场人员最少化）"],
                      "note": "远程在岗 + 医生协同"},
    TYPE_ACCESSIBLE: {"extra": ["人工核验工位", "无障碍服务人员"],
                      "note": "人工兜底优先，智能只作增强"},
    TYPE_COMPUTER:   {"extra": ["网络管理员"], "note": "屏幕/网络类告警同步网管"},
    TYPE_ORAL:       {"extra": ["录音存档节点"], "note": "音频质量告警单独推送"},
    TYPE_LARGE:      {"extra": ["分区巡考员"], "note": "按分区推送，避免重复"},
    TYPE_CLASSIFIED: {"extra": ["保密员"], "note": "全离线内网推送，不推外网"},
}

# 分级告警策略（各考场可用 params.confidence_min 覆盖）
ALERT_POLICY = {
    "confidence_min": 0.60,        # 普通考场基准置信度门槛
    "red_min": 0.85,               # 红色预警最低置信度
    "orange_min": 0.60,            # 橙色预警最低置信度
    "dedup_window_sec": 30,        # 同类告警抑制窗口（普通考场）
    "suppress_after_n": 0,         # 0 = 不限制
    "low_quality_penalty": 0.70,   # 机位画质 fail 时置信度系数
    "low_quality_warn_penalty": 0.90,
    "min_level_for_high_trust": 0.9,
}

# 误报闭环（普通考场应用.md 第四节流程图）
REVIEW_CONFIRMED = "confirmed"          # 成立
REVIEW_REJECTED = "rejected"            # 不成立
REVIEW_FALSE_POSITIVE = "false_positive"  # 误报（回流算法迭代）
REVIEW_CONCLUSIONS = {
    REVIEW_CONFIRMED: "成立",
    REVIEW_REJECTED: "不成立",
    REVIEW_FALSE_POSITIVE: "误报",
}

# =============================================================================
# 六、数据分域（《特殊考场应用.md》第二节第 5 条 / 第四节验收差异项）
# =============================================================================
DOMAIN_EXAM = "exam"                  # 常规考试记录域（普通/备用/无障碍/口试）
DOMAIN_HEALTH = "health"              # 隔离考场健康信息（敏感个人信息）
DOMAIN_EXAM_CONTENT = "exam_content"  # 机考作答内容（考试数据）
DOMAIN_CLASSIFIED = "classified"      # 涉密数据
DOMAINS = (DOMAIN_EXAM, DOMAIN_HEALTH, DOMAIN_EXAM_CONTENT, DOMAIN_CLASSIFIED)

DOMAIN_RULES = {
    DOMAIN_EXAM: {
        "name": "常规考试记录域",
        "isolated": False,
        "sensitivity": "personal",
        "allow_cross_aggregate": True,
        "encrypt_fields": True,
        "roles": ["owner", "admin", "patrol", "commander"],
        "desc": "普通考场记录，按分级授权",
    },
    DOMAIN_HEALTH: {
        "name": "健康信息域（隔离考场）",
        "isolated": True,
        "sensitivity": "sensitive_personal",
        "allow_cross_aggregate": False,
        "encrypt_fields": True,
        "roles": ["owner", "admin", "doctor", "remote_invigilator"],
        "desc": "健康信息属敏感个人信息：单独存储、更高保护等级、不与其他数据混库",
    },
    DOMAIN_EXAM_CONTENT: {
        "name": "作答内容域（机考考场）",
        "isolated": True,
        "sensitivity": "exam_data",
        "allow_cross_aggregate": False,
        "encrypt_fields": True,
        "roles": ["owner", "admin", "patrol", "network_admin"],
        "desc": "屏幕录制含作答内容，属考试数据，严格保密",
    },
    DOMAIN_CLASSIFIED: {
        "name": "涉密域（涉密/专项考试）",
        "isolated": True,
        "sensitivity": "classified",
        "allow_cross_aggregate": False,
        "encrypt_fields": True,
        "roles": ["owner", "security_officer"],
        "desc": "数据不出域；专项审计与保密审批",
    },
}

PARTITION_ACTIVE = "active"
PARTITION_PENDING = "pending"
PARTITION_UNVERIFIED = "unverified"

# 隐私与合规动作（留痕用）
PRIVACY_ACTIONS = {
    "dpia": "个人信息保护影响评估",
    "drill": "替代通道演练",
    "grant": "分区授权",
    "revoke": "分区撤权",
    "access_denied": "跨域访问被拒绝",
    "retention": "最短必要保存期限设置",
    "destroy": "到期销毁",
    "seal": "考试结束数据封存",
    "review": "告警复核结论",
    "offline": "全离线自检",
    "doctor": "医生协同通道登记",
    "network_log": "网络行为日志登记",
    "fast_enable": "备用考场即插即用预置",
    "hifi_audio": "高保真录音与独立拾音登记",
}

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 7

# =============================================================================
# 七、验收指标口径（两份方案第六节/第四节）
# =============================================================================
KPI_NAMES = {
    "recog_accuracy": "行为识别准确率",
    "hint_rate": "疑似行为提示率",
    "false_rate": "误报率",
    "alert_response_sec": "告警响应时间",
    "camera_online_rate": "摄像机在线率",
    "coverage_rate": "考场覆盖完整性",
    "alt_channel": "替代核验通道",
    "alt_channel_drill": "替代通道演练记录",
    "domain_isolation": "数据分域隔离",
    "offline_ready": "离线可用性",
    "min_config": "最小可用配置",
    "dedup_accuracy": "跨机位去重准确率",
    "invigilator_rate": "监考员在岗率",
    "clock_drift_sec": "时钟一致性偏差",
}

# 通用验收口径（普通考场应用.md 第六节 + 特殊考场应用.md 第四节）
TARGETS = {
    "recog_accuracy": 0.95,          # ≥ 95%
    "hint_rate": 0.985,              # ≥ 98.5%
    "false_rate": 0.05,              # ≤ 5%
    "alert_response_sec": 3.0,       # ≤ 3 秒
    "camera_online_rate": 0.99,      # ≥ 99%
    "coverage_rate": 1.00,           # 100% 无盲区
    "dedup_accuracy": 0.95,          # 大教室/机考：去重准确率 ≥ 95%
    "clock_drift_sec": 1.0,          # ≤ 1 秒
    "invigilator_rate": 1.00,        # 在岗率（考核用）
    "alt_channel": 1.0,              # 无障碍：强制具备
    "alt_channel_drill": 1.0,        # 无障碍：强制有演练记录
    "domain_isolation": 1.0,         # 隔离/机考/涉密：强制独立分区 + 独立授权
    "offline_ready": 1.0,            # 涉密：全离线可独立运行
    "min_config": 1.0,               # 1 机位 + 1 链路可巡考并留痕
}

# 指标方向：越大越好 / 越小越好
KPI_DIRECTION = {
    "recog_accuracy": "up", "hint_rate": "up", "false_rate": "down",
    "alert_response_sec": "down", "camera_online_rate": "up",
    "coverage_rate": "up", "dedup_accuracy": "up", "clock_drift_sec": "down",
    "invigilator_rate": "up", "alt_channel": "up", "alt_channel_drill": "up",
    "domain_isolation": "up", "offline_ready": "up", "min_config": "up",
}

# 不同考场类型的指标差异（特殊考场应用.md 第四节"差异要求"列）
KPI_VARIANTS = {
    TYPE_STANDARD:   {"applicable": ["recog_accuracy", "hint_rate", "false_rate",
                                     "alert_response_sec", "camera_online_rate",
                                     "coverage_rate", "clock_drift_sec"],
                      "alt_channel": "建议具备"},
    TYPE_BACKUP:     {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "min_config"],
                      "alt_channel": "建议具备"},
    TYPE_ISOLATION:  {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "domain_isolation",
                                     "invigilator_rate"],
                      "alt_channel": "建议具备",
                      "domain_isolation": "强制独立分区 + 独立授权"},
    TYPE_ACCESSIBLE: {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "alt_channel",
                                     "alt_channel_drill", "min_config"],
                      "alt_channel": "强制具备并有演练记录"},
    TYPE_COMPUTER:   {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "dedup_accuracy",
                                     "domain_isolation"],
                      "dedup_accuracy": "去重准确率 ≥ 95%",
                      "domain_isolation": "强制独立分区 + 独立授权"},
    TYPE_ORAL:       {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "camera_online_rate"],
                      "audio_quality": "≥ 48kHz 或按要求"},
    TYPE_LARGE:      {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "dedup_accuracy",
                                     "camera_online_rate"],
                      "dedup_accuracy": "去重准确率 ≥ 95%"},
    TYPE_CLASSIFIED: {"applicable": ["recog_accuracy", "alert_response_sec",
                                     "coverage_rate", "domain_isolation",
                                     "offline_ready"],
                      "offline_ready": "全离线可独立运行",
                      "domain_isolation": "强制独立分区 + 独立授权"},
}


def kpi_applicable(room_type):
    return list(KPI_VARIANTS.get(room_type, {}).get("applicable", []))


def kpi_target(key, room_type=None):
    """取指标目标值；特殊考场有差异时按差异走。"""
    return TARGETS.get(key)


def kpi_meets(key, value, room_type=None):
    """按方向判定是否达标；value 为 None 表示无证据。"""
    if value is None:
        return None
    target = kpi_target(key, room_type)
    if target is None:
        return None
    return value <= target if KPI_DIRECTION.get(key) == "down" else value >= target


# =============================================================================
# 八、联动（不改动其他模块，只按它们已有的 integration_api 协议调用）
# =============================================================================
ALL_DIR = BASE_DIR.parent
LINKAGE_DIR = ALL_DIR / "00_集成联动"

MODULE_DIRS = {
    "01": ALL_DIR / "01_人脸识别与身份核对" / "01_人脸识别与身份核对",
    "02": ALL_DIR / "02_摄像头核查与视频质量诊断",
    "03": ALL_DIR / "03_智能终端操作",
    "06": ALL_DIR / "06_智能安检与手机作弊器材检测",
}

# 本模块数据目录重定向变量（与既有模块同款做法）
HOME_ENV = "EXAM_ROOM_HOME"

# 跨系统调用的沙箱重定向变量（传给 00 联动的 Linkage，由它转给子进程）
LINKAGE_HOME_ENV = {
    "01": "EXAM_HOME",
    "02": "EXAM_CAMERA_HOME",
    "03": "EXAM_TERMINAL_HOME",
    "06": "EXAM_SEC_HOME",
}
LINKAGE_DEFAULT_TIMEOUT = 60

# 本模块对外集成动作（与 integration_api.OPS 对齐；供文档与自检核对）
INTEGRATION_OPS = (
    "status", "templates", "template", "list_rooms", "get_room", "upsert_room",
    "apply_template", "degrade", "admit", "selfcheck", "arm", "disable_room",
    "set_room", "route_alert", "review_alert", "alerts", "behaviors", "kpi",
    "session", "domain", "grant", "revoke", "dpia", "drill", "offline",
    "retention", "destroy", "compliance", "report", "linkage_status", "audits",
)

# =============================================================================
# 九、设置
# =============================================================================
DEFAULT_SETTINGS = {
    "operator": "考务管理员",
    "site": "标准化考点",
    "strict_mode": True,          # 严格模式：阻断项一律不放行
    "auto_linkage": False,        # 是否自动把告警推给 03（自检里显式开）
    "linkage_timeout": 60,
    "sandbox_root": "",           # 非空则跨系统调用走沙箱（自检用）
    "default_retention_days": DEFAULT_RETENTION_DAYS,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                s.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(patch):
    s = load_settings()
    s.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    try:
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except OSError:
        return False
    return s


def mask_id(value):
    """身份证/考生编号掩码：只留后 4 位（与 06 同口径）。"""
    v = str(value or "")
    if len(v) <= 4:
        return "*" * len(v)
    return "*" * (len(v) - 4) + v[-4:]


def describe():
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "考场类型": "%d 类（普通 1 + 特殊 %d）" % (len(ROOM_TYPES), len(SPECIAL_TYPES)),
        "行为清单": "%d 种（普通考场 44 + 机考/口试附加 %d）"
                    % (len(BEHAVIORS), len(EXTRA_BEHAVIORS)),
        "准入缺陷目录": "%d 项（阻断 %d / 警告 %d）"
                        % (len(DEFECTS), len(BLOCK_CODES), len(WARN_CODES)),
        "数据域": " / ".join(DOMAIN_RULES[d]["name"] for d in DOMAINS),
        "指标口径": "识别 ≥95%%、提示 ≥98.5%%、误报 ≤5%%、响应 ≤3s、在线率 ≥99%%",
        "联动": "00_集成联动（01/02/03/06），子进程 + JSON，不改动对端代码",
    }
