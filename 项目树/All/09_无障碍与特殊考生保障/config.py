# -*- coding: utf-8 -*-
r"""
09 无障碍与特殊考生保障 · 配置

本专题干的事：**别让智能化设备本身变成新的不公平**。
    特殊考生登记 -> 需求识别 -> 保障措施发放 -> 走替代通道（人工核验/人工安检/无障碍通道）
                 -> 全程留痕 -> 指标与验收证据（配置率/可用性/演练/受阻事件/算法公平性）

与其它专题的关系（本模块**只读**调用，绝不修改它们）：
  · 00_集成联动    : 复用它的子进程 + JSON 联动总线（linkage.py 里没有 09 的键，
                     本模块在**内存里**把自己注册进去，不改 00 的任何文件）
  · 01 人脸识别    : 人脸核验失败（面部损伤/术后/遮挡）-> 触发本模块的**替代核验**
  · 02 摄像头核查  : 机位画质可信度低 -> 核验结论降权 -> 优先走人工核验
  · 03 智能终端    : 本模块的告警/受阻事件按 03 的分级表推送、处置留痕
  · 06 智能安检    : 体检植入/孕妇等免过安检门 -> 与 06 的人工安检通道口径一致
                    （告警码 SECURITY_SPECIAL_CHANNEL 是 06 已经在用的）

**如实说明（重要）**：
  本机没有无障碍通道闸机、低位核验终端、手语翻译等实物，所以：
    · 第 1 层 通道台账/合规校验/档案/替代核验/演练/公平性测试/指标/报告 是**真实可用的业务逻辑**；
    · 第 2 层 通道尺寸、低位终端高度、语音/文字/震动提示都按**现场实测值**录入并做合规判定，
      接上真设备或真测距即可用；
    · 没有真数据时用 `工具_演示数据.py` 造样本，所有演示来源都标 source=demo，
      界面与报告里都写明"演示"，**绝不冒充真实保障记录**。

数据默认落在本目录 data\，可用 EXAM_ACCESS_HOME 重定向。
**零第三方依赖**：只用标准库（tkinter / sqlite3 / hashlib）；装了 cryptography 则敏感字段自动加密。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_ACCESS_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "access_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "accessibility.db"
LOG_DIR = DATA_ROOT / "logs"
REPORT_DIR = DATA_ROOT / "reports"          # 验收证据导出
LEDGER_DIR = DATA_ROOT / "ledgers"          # 特殊考生处置台账导出（脱敏）
ACCESS_LOG = LOG_DIR / "accessibility.log"

for _d in (DATA_ROOT, LOG_DIR, REPORT_DIR, LEDGER_DIR):
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

# ---------------- 关键指标口径（来自《无障碍与特殊考生保障.md》第三节） ----------------
TARGET_CHANNEL_RATE = 1.0           # 无障碍通道配置率 100% 考点
TARGET_ALT_AVAILABILITY = 1.0       # 替代核验通道可用性 100%，每考期至少演练 1 次
TARGET_DRILL_PER_SESSION = 1        # 每考期演练 ≥1 次
TARGET_TECH_BLOCK = 0               # 因技术原因未能入场事件 0 起
TARGET_FAIRNESS_MAX_GAP = 0.05      # 分层通过率最大组间差 ≤5%（可接受范围）

# ---------------- 通道与硬性尺寸（第二节第 1、2 条） ----------------
CH_SMART = "smart"                  # 智能核验通道（人脸/闸机）
CH_MANUAL = "manual"                # 人工核验通道（双证比对 + 双人见证）
CH_ACCESSIBLE = "accessible"        # 无障碍专用通道（轮椅/低位/语音）
CHANNEL_KINDS = (CH_SMART, CH_MANUAL, CH_ACCESSIBLE)
CHANNEL_TEXT = {CH_SMART: "智能核验通道", CH_MANUAL: "人工核验通道",
                CH_ACCESSIBLE: "无障碍专用通道"}

ST_OK = "ok"                        # 可用
ST_DEGRADED = "degraded"            # 降级可用（如语音坏了但文字正常）
ST_DOWN = "down"                    # 不可用
ST_BUILDING = "building"            # 建设中/未验收
CHANNEL_STATES = (ST_OK, ST_DEGRADED, ST_DOWN, ST_BUILDING)
CHANNEL_STATE_TEXT = {ST_OK: "可用", ST_DEGRADED: "降级可用",
                      ST_DOWN: "不可用", ST_BUILDING: "未就绪"}
# 可用性判定：ok / degraded 都算"可用"（降级必须有替代提示方式，见 channel_issues）
CHANNEL_USABLE = (ST_OK, ST_DEGRADED)

MIN_AISLE_WIDTH_MM = 900            # 轮椅通行净宽 ≥900mm
MIN_TURN_RADIUS_MM = 1500           # 轮椅回转/掉头空间 ≥1500mm
COUNTER_HEIGHT_MIN_MM = 700         # 低位核验终端可调高度下限
COUNTER_HEIGHT_MAX_MM = 1100        # 低位核验终端可调高度上限（≤1100mm 视为低位）
LOW_COUNTER_MAX_MM = 1100

# ---------------- 需求类型（适用对象，第一节） ----------------
NEED_FACE = "face"                  # 面部损伤/术后/严重痤疮 -> 人脸核验失败
NEED_VISION = "vision"              # 视力障碍
NEED_MOBILITY = "mobility"          # 肢体障碍/轮椅
NEED_HEARING = "hearing"            # 听力障碍
NEED_MEDICAL = "medical"            # 佩戴医疗器械/体内植入/孕妇
NEED_LANGUAGE = "language"          # 少数民族/外籍（姓名证件格式差异）
NEED_COGNITIVE = "cognitive"        # 认知/心理特殊需要（自闭谱系、重度焦虑）
NEED_OTHER = "other"
NEED_KINDS = (NEED_FACE, NEED_VISION, NEED_MOBILITY, NEED_HEARING,
              NEED_MEDICAL, NEED_LANGUAGE, NEED_COGNITIVE, NEED_OTHER)
NEED_TEXT = {
    NEED_FACE: "面部特征异常（损伤/术后/遮挡）",
    NEED_VISION: "视力障碍",
    NEED_MOBILITY: "肢体障碍/轮椅",
    NEED_HEARING: "听力障碍",
    NEED_MEDICAL: "医疗器械/体内植入/孕期",
    NEED_LANGUAGE: "少数民族/外籍（证件格式差异）",
    NEED_COGNITIVE: "认知或心理特殊需要",
    NEED_OTHER: "其他（须写明）",
}

# ---------------- 保障措施（第一节"保障措施"列 + 第二、四节要求） ----------------
M_VOICE = "voice_guide"             # 语音引导终端
M_TEXT = "text_guide"               # 文字/大字提示
M_VIBRATION = "vibration"           # 震动提示
M_SIGN = "sign_language"            # 手语翻译/书面交流
M_LOW_COUNTER = "low_counter"       # 低位核验终端（高度可调）
M_WHEELCHAIR = "wheelchair_aisle"   # 加宽通道/轮椅通行
M_MANUAL_VERIFY = "manual_verify"   # 人工核验替代（双证比对 + 双人见证）
M_MANUAL_SECURITY = "manual_security"   # 人工安检替代通道（免过安检门）
M_MEDICAL_PASS = "medical_pass"     # 凭医疗器械证明放行
M_EXTRA_TIME = "extra_time"         # 延长入场/交卷时间
M_SEPARATE_WAIT = "separate_wait"   # 单独候考区/休息区
M_ASSISTANT = "assistant"           # 允许协助人员/陪考
M_ENLARGED_PAPER = "enlarged_paper"  # 大字/盲文试卷
M_SEAT_ADJUST = "seat_adjust"       # 座位/考场调整（一层、靠门、靠前）
M_EXEMPT_FACE = "exempt_face"       # 备案免人脸核验
M_ESCORT = "escort"                 # 专人引导护送
MEASURE_KINDS = (M_VOICE, M_TEXT, M_VIBRATION, M_SIGN, M_LOW_COUNTER,
                 M_WHEELCHAIR, M_MANUAL_VERIFY, M_MANUAL_SECURITY,
                 M_MEDICAL_PASS, M_EXTRA_TIME, M_SEPARATE_WAIT, M_ASSISTANT,
                 M_ENLARGED_PAPER, M_SEAT_ADJUST, M_EXEMPT_FACE, M_ESCORT)
MEASURE_TEXT = {
    M_VOICE: "语音引导终端",
    M_TEXT: "文字/大字提示",
    M_VIBRATION: "震动提示",
    M_SIGN: "手语翻译/书面交流",
    M_LOW_COUNTER: "低位核验终端（高度可调）",
    M_WHEELCHAIR: "加宽通道/轮椅通行",
    M_MANUAL_VERIFY: "人工核验替代（双证比对+双人见证）",
    M_MANUAL_SECURITY: "人工安检替代通道（免过安检门）",
    M_MEDICAL_PASS: "凭医疗器械证明放行",
    M_EXTRA_TIME: "延长入场/交卷时间",
    M_SEPARATE_WAIT: "单独候考区/休息区",
    M_ASSISTANT: "允许协助人员/陪考",
    M_ENLARGED_PAPER: "大字/盲文试卷",
    M_SEAT_ADJUST: "座位/考场调整（一层、靠门）",
    M_EXEMPT_FACE: "备案免人脸核验",
    M_ESCORT: "专人引导护送",
}
# 需要"人"而不是"设备"的措施（发放入口不同：一个是运维，一个是考务）
MEASURE_HUMAN = (M_SIGN, M_MANUAL_VERIFY, M_MANUAL_SECURITY, M_MEDICAL_PASS,
                 M_EXTRA_TIME, M_SEPARATE_WAIT, M_ASSISTANT, M_ENLARGED_PAPER,
                 M_SEAT_ADJUST, M_EXEMPT_FACE, M_ESCORT)

# 需求 -> 推荐措施（第一节表格的可执行化；缺项可手工追加）
NEED_MEASURES = {
    NEED_FACE: (M_MANUAL_VERIFY, M_EXEMPT_FACE, M_TEXT),
    NEED_VISION: (M_VOICE, M_ESCORT, M_MANUAL_VERIFY, M_ENLARGED_PAPER),
    NEED_MOBILITY: (M_WHEELCHAIR, M_LOW_COUNTER, M_ESCORT, M_SEAT_ADJUST),
    NEED_HEARING: (M_TEXT, M_VIBRATION, M_SIGN, M_SEAT_ADJUST),
    NEED_MEDICAL: (M_MANUAL_SECURITY, M_MEDICAL_PASS, M_SEPARATE_WAIT),
    NEED_LANGUAGE: (M_MANUAL_VERIFY, M_TEXT, M_SIGN),
    NEED_COGNITIVE: (M_SEPARATE_WAIT, M_ASSISTANT, M_EXTRA_TIME, M_ESCORT),
    NEED_OTHER: (M_MANUAL_VERIFY, M_ESCORT),
}
# 替代核验/人工安检的必备措施（走替代通道就必须把这几个措施置为已发放）
ALT_VERIFY_REQUIRED = (M_MANUAL_VERIFY,)
CHANNEL_REQUIRED_FOR = {NEED_MOBILITY: CH_ACCESSIBLE, NEED_VISION: CH_ACCESSIBLE,
                        NEED_FACE: CH_MANUAL, NEED_LANGUAGE: CH_MANUAL,
                        NEED_MEDICAL: CH_MANUAL, NEED_HEARING: CH_SMART}

# ---------------- 措施发放状态 ----------------
ACC_REQUESTED = "requested"         # 已申请
ACC_APPROVED = "approved"           # 已审批
ACC_PROVIDED = "provided"           # 已落实（现场可用）
ACC_REJECTED = "rejected"           # 不予（须写理由）
ACC_STATES = (ACC_REQUESTED, ACC_APPROVED, ACC_PROVIDED, ACC_REJECTED)
ACC_STATE_TEXT = {ACC_REQUESTED: "已申请", ACC_APPROVED: "已审批",
                  ACC_PROVIDED: "已落实", ACC_REJECTED: "不予（须写明理由）"}

# ---------------- 替代核验（第二节第 4 条：人工兜底不可取消） ----------------
ALT_MANUAL_DOC = "manual_doc"       # 双证人工比对（身份证 + 准考证）
ALT_ASSISTED_FACE = "assisted_face"  # 工作人员协助刷脸（调整姿态/多次尝试）
ALT_DEVICE_SWAP = "device_swap"     # 换设备/换通道复验
ALT_WITNESS_DOC = "witness_doc"     # 双人见证 + 证件原件目视核对
ALT_EXEMPT = "exempt"               # 备案免核验（考前已审批）
ALT_MODES = (ALT_MANUAL_DOC, ALT_ASSISTED_FACE, ALT_DEVICE_SWAP,
             ALT_WITNESS_DOC, ALT_EXEMPT)
ALT_MODE_TEXT = {
    ALT_MANUAL_DOC: "双证人工比对",
    ALT_ASSISTED_FACE: "协助刷脸（多次尝试）",
    ALT_DEVICE_SWAP: "换设备/换通道复验",
    ALT_WITNESS_DOC: "双人见证原件核对",
    ALT_EXEMPT: "备案免核验",
}
DOC_ID_CARD = "id_card"
DOC_TICKET = "ticket_no"
DOC_PHOTO = "photo"
DOC_MEDICAL = "medical_cert"
DOC_PASSPORT = "passport"
DOC_KINDS = (DOC_ID_CARD, DOC_TICKET, DOC_PHOTO, DOC_MEDICAL, DOC_PASSPORT)
DOC_TEXT = {DOC_ID_CARD: "身份证原件", DOC_TICKET: "准考证",
            DOC_PHOTO: "报名照片", DOC_MEDICAL: "医疗器械/医疗证明",
            DOC_PASSPORT: "护照/其它证件"}
DOC_MATCHED = "matched"
DOC_MISMATCH = "mismatch"
DOC_MISSING = "missing"
DOC_STATES = (DOC_MATCHED, DOC_MISMATCH, DOC_MISSING)
DOC_STATE_TEXT = {DOC_MATCHED: "一致", DOC_MISMATCH: "不一致", DOC_MISSING: "未提供"}
# 双证比对至少要有 2 项"一致"，且其中必须包含身份证或准考证
MIN_DOC_MATCHED = 2
MUST_HAVE_DOC = (DOC_ID_CARD, DOC_TICKET, DOC_PASSPORT)

ALT_PASSED = "passed"               # 核验通过，准予入场（与智能核验同等效力）
ALT_FAILED = "failed"               # 核验不通过，转考务处置（不得由设备单方拒绝入场）
ALT_DEFERRED = "deferred"           # 转考务/纪检进一步核实（先行入场候考）
ALT_OUTCOMES = (ALT_PASSED, ALT_FAILED, ALT_DEFERRED)
ALT_OUTCOME_TEXT = {ALT_PASSED: "通过（准予入场）", ALT_FAILED: "不通过（转考务）",
                    ALT_DEFERRED: "待进一步核实（先行入场）"}
WITNESS_REQUIRED = True             # 双人见证：见证人不得与主检人相同（第二节第 4 条）
ALT_VERIFY_SLA_SEC = 180            # 替代核验时限 3 分钟（超过计"超时"，用于整改）
EQUIVALENCE_NOTE = ("人工核验结论与智能核验具有同等效力；"
                    "任何情况下不得因设备原因拒绝考生入场")

# ---------------- 因技术原因入场受阻（指标：0 起） ----------------
BLOCK_SYMPTOMS = {
    "face_fail": "人脸核验多次失败",
    "device_down": "核验设备/闸机故障",
    "channel_down": "无障碍通道被占用或不可用",
    "no_voice": "语音提示失效（听力考生无法获知流程）",
    "no_low_counter": "低位终端缺失/高度不可调",
    "network": "断网导致核验不可用",
    "other": "其他（须写明）",
}
FINAL_ENTERED = "entered"           # 最终正常入场
FINAL_ENTERED_MANUAL = "entered_manual"   # 走替代通道入场
FINAL_DENIED = "denied"             # 未能入场（**指标要求 0 起**）
FINAL_RESULTS = (FINAL_ENTERED, FINAL_ENTERED_MANUAL, FINAL_DENIED)
FINAL_TEXT = {FINAL_ENTERED: "正常入场", FINAL_ENTERED_MANUAL: "替代通道入场",
              FINAL_DENIED: "未能入场（须追责整改）"}

# ---------------- 算法公平性分层测试（第二节第 5 条） ----------------
DIM_GENDER = "gender"
DIM_AGE = "age"
DIM_GLASSES = "glasses"
DIM_MASK = "mask"
DIM_COVER = "face_cover"
DIM_SKIN = "skin_tone"
FAIRNESS_DIMS = (DIM_GENDER, DIM_AGE, DIM_GLASSES, DIM_MASK, DIM_COVER, DIM_SKIN)
DIM_TEXT = {DIM_GENDER: "性别", DIM_AGE: "年龄段", DIM_GLASSES: "是否戴眼镜",
            DIM_MASK: "是否戴口罩", DIM_COVER: "面部遮挡/损伤",
            DIM_SKIN: "肤色分层"}
# 上线前**必须覆盖**的分层维度（其余维度按条件增补）
FAIRNESS_REQUIRED_DIMS = (DIM_GENDER, DIM_AGE, DIM_GLASSES, DIM_MASK)
DIM_GROUPS = {
    DIM_GENDER: ("男", "女"),
    DIM_AGE: ("≤25", "26-40", ">40"),
    DIM_GLASSES: ("戴眼镜", "不戴眼镜"),
    DIM_MASK: ("戴口罩", "不戴口罩"),
    DIM_COVER: ("有遮挡/损伤", "无遮挡"),
    DIM_SKIN: ("浅", "中", "深"),
}
MIN_GROUP_SAMPLES = 20              # 单组样本太少 -> 结论不可靠，如实提示
FAIRNESS_ADJUST_ACTIONS = ("threshold", "retrain", "add_samples", "manual_first",
                           "none")
FAIRNESS_ADJUST_TEXT = {
    "threshold": "调整该分层阈值",
    "retrain": "补充样本重训/换模型",
    "add_samples": "扩大该分层样本量后复测",
    "manual_first": "该分层优先走人工核验",
    "none": "无需调整（差异在可接受范围）",
}

# ---------------- 演练（第二节第 1 条、第三节指标） ----------------
DRILL_SCENARIOS = {
    "alt_verify": "替代核验全流程（人脸失败 -> 双证人工核验）",
    "wheelchair": "轮椅考生入场（加宽通道 + 低位终端）",
    "hearing": "听力考生入场（文字/震动提示）",
    "vision": "视力考生入场（语音引导 + 专人护送）",
    "medical": "医疗器械考生安检（人工安检通道）",
    "full_chain": "三通道并行全链路（含高峰分流）",
}
DRILL_PASS = "pass"
DRILL_PARTIAL = "partial"
DRILL_FAIL = "fail"
DRILL_RESULTS = (DRILL_PASS, DRILL_PARTIAL, DRILL_FAIL)
DRILL_RESULT_TEXT = {DRILL_PASS: "合格", DRILL_PARTIAL: "基本合格（有整改项）",
                     DRILL_FAIL: "不合格（须重新演练）"}
DRILL_TARGET_MIN = 10               # 单次演练建议时长（分钟）——太短演不出问题

# ---------------- 告警分级（推 03 智能终端时用；03 的表里没有的码由本模块显式带级别） ----------------
LEVEL_RED = "red"
LEVEL_ORANGE = "orange"
LEVEL_TEXT = {LEVEL_RED: "红色预警", LEVEL_ORANGE: "橙色预警"}
# 09 的告警码：level 是本模块**要求**的级别；fallback 是万一只能走 03 码表兜底时用的已注册码
ALERT_CODES = {
    "ACCESS_TECH_BLOCK": {"level": LEVEL_RED, "fallback": "FACE_MISMATCH",
                          "text": "因技术原因入场受阻（须立即人工介入）"},
    "ACCESS_ALT_VERIFY": {"level": LEVEL_ORANGE, "fallback": "FACE_MISMATCH",
                          "text": "启用替代核验（人工核验/双人见证）"},
    "ACCESS_CHANNEL_DOWN": {"level": LEVEL_ORANGE, "fallback": "OFFLINE",
                            "text": "无障碍通道不可用（须开启备用通道）"},
    "ACCESS_MEASURE_MISSING": {"level": LEVEL_ORANGE, "fallback": "SECURITY_SPECIAL_CHANNEL",
                               "text": "特殊考生保障措施未落实"},
    "ACCESS_ASSIST_NEEDED": {"level": LEVEL_ORANGE, "fallback": "SECURITY_SPECIAL_CHANNEL",
                             "text": "特殊考生需现场协助（引导/手语/护送）"},
    "ACCESS_CHANNEL_CHECK": {"level": LEVEL_ORANGE, "fallback": "OFFLINE",
                             "text": "无障碍通道合规校验不合格"},
}
# 与 06 智能安检保持一致的人工安检通道码（06 已在用，09 直接复用，避免同义多码）
ALERT_CODE_SPECIAL_SECURITY = "SECURITY_SPECIAL_CHANNEL"

# ---------------- 报告 ----------------
REPORT_TITLE = "无障碍与特殊考生保障 · 验收证据"

# ---------------- 设置文件 ----------------
SETTINGS_PATH = DATA_ROOT / "accessibility_settings.json"
SETTINGS_DEFAULT = {
    "site": "第一考点",
    "operator": "考务员",
    "sites": ["第一考点"],           # 纳入统计的考点清单（配置率的分母）
    "session": "2026年上半年",        # 考期（演练次数按考期统计）
    "witness_required": True,        # 双人见证（特殊考生替代核验强制）
    "allow_without_consent": True,   # 未取得单独同意 -> 只走人工通道，不采集人脸（绝不拒绝入场）
    "alt_verify_sla_sec": ALT_VERIFY_SLA_SEC,
    "fairness_max_gap": TARGET_FAIRNESS_MAX_GAP,
    "min_group_samples": MIN_GROUP_SAMPLES,
    "report_to_terminal": True,      # 受阻事件默认上报 03 智能终端（这类事必须让人看见）
    "desensitize_export": True,      # 台账/报告导出默认脱敏
    "encrypt_sensitive": True,       # 医疗证明等敏感字段加密落库
    "demo_data": False,              # 演示数据标记（界面里会写明"演示"）
}
DEMO_SOURCE = "demo"
REAL_SOURCE = "real"
MANUAL_SOURCE = "manual"


def load_settings():
    """读设置：缺文件/坏文件/缺键都回落默认值，绝不抛异常。"""
    import json
    cfg = {k: (list(v) if isinstance(v, list) else v)
           for k, v in SETTINGS_DEFAULT.items()}
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in SETTINGS_DEFAULT:
                    if k not in data or data[k] is None:
                        continue
                    if isinstance(SETTINGS_DEFAULT[k], list):
                        if isinstance(data[k], list):
                            cfg[k] = [str(x) for x in data[k] if str(x).strip()]
                    elif data[k] != "":
                        cfg[k] = data[k]
    except Exception:
        pass
    if not cfg.get("sites"):
        cfg["sites"] = [cfg.get("site") or "第一考点"]
    return cfg


def save_settings(settings):
    """写设置；失败返回 False（不影响主流程）。"""
    import json
    try:
        merged = load_settings()
        for k, v in (settings or {}).items():
            if k not in SETTINGS_DEFAULT:
                continue
            merged[k] = v
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def sites(cfg=None):
    """纳入统计的考点清单（去重、去空）。"""
    cfg = cfg or load_settings()
    out = []
    for s in (cfg.get("sites") or []):
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    if not out:
        out = [str(cfg.get("site") or "第一考点")]
    return out


def sla_sec(cfg=None):
    cfg = cfg or load_settings()
    try:
        return float(cfg.get("alt_verify_sla_sec") or ALT_VERIFY_SLA_SEC)
    except (TypeError, ValueError):
        return float(ALT_VERIFY_SLA_SEC)


def fairness_max_gap(cfg=None):
    cfg = cfg or load_settings()
    try:
        return float(cfg.get("fairness_max_gap") or TARGET_FAIRNESS_MAX_GAP)
    except (TypeError, ValueError):
        return float(TARGET_FAIRNESS_MAX_GAP)


def min_group_samples(cfg=None):
    cfg = cfg or load_settings()
    try:
        return int(cfg.get("min_group_samples") or MIN_GROUP_SAMPLES)
    except (TypeError, ValueError):
        return int(MIN_GROUP_SAMPLES)


def need_text(needs):
    return "、".join(NEED_TEXT.get(n, n) for n in (needs or [])) or "无"


def measure_text(measure):
    return MEASURE_TEXT.get(measure, measure)


def measures_text(measures):
    return "、".join(MEASURE_TEXT.get(m, m) for m in (measures or [])) or "无"


def mask_id(text, keep=4):
    """身份证/准考证号掩码（界面与导出默认不显全号）。"""
    s = str(text or "").strip()
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def mask_name(name):
    """姓名脱敏：保留姓氏，其余打星（台账导出用）。"""
    s = str(name or "").strip()
    if not s:
        return ""
    if len(s) == 1:
        return s
    return s[0] + "*" * (len(s) - 1)


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
        "考点清单": sites(cfg),
        "考期": cfg.get("session"),
        "硬性尺寸": "净宽≥%dmm、回转≥%dmm、低位终端高度 %d~%dmm"
                    % (MIN_AISLE_WIDTH_MM, MIN_TURN_RADIUS_MM,
                       COUNTER_HEIGHT_MIN_MM, COUNTER_HEIGHT_MAX_MM),
        "指标口径": "通道配置率100%%、替代通道可用性100%%且每考期演练≥1次、"
                    "因技术原因未入场0起、分层差异≤%.0f%%"
                    % (fairness_max_gap(cfg) * 100),
        "敏感字段加密": crypto,
        "依赖": "仅标准库（tkinter/sqlite3）；装了 cryptography 则医疗证明等字段自动加密",
    }
