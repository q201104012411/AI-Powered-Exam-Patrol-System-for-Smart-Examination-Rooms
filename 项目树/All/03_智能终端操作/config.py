# -*- coding: utf-8 -*-
r"""
智能终端操作端 · 配置

对应《智能终端.md》里的能力：告警分级展示、复核留痕、断网可用、
权限最小化、统一管控、考场时钟、与 01 人脸核验/边缘节点对接。

路径策略与 01 一致：数据默认落在本目录 data\ 下，可用 EXAM_TERMINAL_HOME 重定向。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_TERMINAL_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "terminal_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "terminal.db"
LOG_DIR = DATA_ROOT / "logs"
ATTACH_DIR = DATA_ROOT / "attachments"          # 复核附件（截图/说明）
QUEUE_DIR = DATA_ROOT / "outbox"                # 断网缓存
SOAK_LOG = LOG_DIR / "soak.log"                 # 72 小时稳定性记录

for _d in (DATA_ROOT, LOG_DIR, ATTACH_DIR, QUEUE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 是否隐藏启动器的命令行窗口（由所有者在界面里开关，写标记文件，.bat 读取）
HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"

# ---------------- 命令控制台（所有者显式开启，默认关闭） ----------------
# 语义与 01 一致：标记文件存在 = 已开启；不存在 = 关闭。
#   CONSOLE_FLAG       总开关（只有所有者能拨）。关闭时**所有角色**（含已授权管理员）一律无权限
#   CONSOLE_ADMIN_FLAG 所有者对管理员的单独授权：只放行 `ollama run <模型> [提示词]`，
#                      且以总开关开启为前提
# 为什么拆成两把开关：所有者可能想"临时关掉全部执行入口"却保留对管理员的授权意图，
# 两件事各由一把开关表达；判定入口只有 console.console_tier() 一处。
CONSOLE_FLAG = DATA_ROOT / "cmd_console.on"
CONSOLE_ADMIN_FLAG = DATA_ROOT / "cmd_console_admin.on"

# ---------------- 终端类型（《智能终端.md》第一节） ----------------
TERMINAL_TYPES = {
    "patrol":     {"name": "巡考客户端",   "role": "patrol",   "desc": "告警接收、视频回看、复核判定、处置记录"},
    "invigilate": {"name": "监考终端",     "role": "invigilator", "desc": "本考场告警提示、一键上报、举手呼叫"},
    "command":    {"name": "指挥大屏",     "role": "commander", "desc": "多考场轮巡、告警态势、统计分析"},
    "mobile":     {"name": "移动巡考 APP", "role": "patrol",   "desc": "移动复核、任务派发、拍照留证"},
    "handheld":   {"name": "手持核验终端", "role": "checker",  "desc": "人脸核验、签到、抽签"},
    "clock":      {"name": "考场时钟终端", "role": "viewer",   "desc": "标准时间显示、考试指令播报"},
}

# ---------------- 告警分级（第二节第 1 条：橙/红两级） ----------------
LEVEL_ORANGE = "orange"
LEVEL_RED = "red"
LEVELS = (LEVEL_ORANGE, LEVEL_RED)
LEVEL_META = {
    LEVEL_ORANGE: {"name": "橙色预警", "color": "#e08b00", "sound": False,
                   "top": True, "popup": False, "force_confirm": False, "sla_sec": 300},
    LEVEL_RED:    {"name": "红色预警", "color": "#c00000", "sound": True,
                   "top": True, "popup": True, "force_confirm": True, "sla_sec": 60},
}

# 业务侧告警码 -> 分级兜底（真实分级由业务平台下发，这里只做兜底映射）
ALERT_CODE_LEVEL = {
    "MULTI_FACE": LEVEL_RED,        # 多人同框
    "NO_INVIGILATOR": LEVEL_RED,    # 监考缺位
    "PHONE_DETECTED": LEVEL_RED,    # 手机/作弊器材
    "SIGNAL_ABNORMAL": LEVEL_RED,   # 无线信号异常
    "FACE_MISMATCH": LEVEL_ORANGE,  # 人脸不符
    "LOOK_DOWN": LEVEL_ORANGE,      # 长时间低头
    "TURN_HEAD": LEVEL_ORANGE,      # 频繁转头
    "VIDEO_BLUR": LEVEL_ORANGE,     # 视频模糊
    "OFFLINE": LEVEL_ORANGE,        # 设备离线
    "CLOCK_DRIFT": LEVEL_ORANGE,    # 时钟偏差
}

# ---------------- 复核结论（第二节第 2 条） ----------------
REVIEW_CONFIRMED = "confirmed"      # 成立
REVIEW_REJECTED = "rejected"        # 不成立
REVIEW_FALSE_POSITIVE = "false_positive"   # 误报（进入算法迭代闭环）
REVIEW_CONCLUSIONS = {
    REVIEW_CONFIRMED: "成立",
    REVIEW_REJECTED: "不成立",
    REVIEW_FALSE_POSITIVE: "误报",
}

# ---------------- 角色与视图最小化（第二节第 4 条） ----------------
ROLE_OWNER = "owner"                # 所有者：最高权限（可复核 + 管终端账户 + 全部动作）
ROLE_ADMIN = "admin"                # 管理员：日常全权（巡检/处置/复核/核验），仅账户管理与危险操作归所有者
ROLE_PATROL = "patrol"              # 巡考员：全考点告警 + 复核
ROLE_INVIGILATOR = "invigilator"    # 监考员：仅本考场，可一键上报
ROLE_COMMANDER = "commander"        # 指挥中心：全考点态势 + 统计，不直接复核
ROLE_CHECKER = "checker"            # 核验员：手持核验
ROLE_VIEWER = "viewer"              # 时钟终端等只读
ROLE_LEVEL = {ROLE_VIEWER: 1, ROLE_CHECKER: 2, ROLE_INVIGILATOR: 2,
              ROLE_ADMIN: 3, ROLE_PATROL: 3, ROLE_COMMANDER: 3, ROLE_OWNER: 4}
ROLE_TEXT = {ROLE_OWNER: "所有者", ROLE_ADMIN: "管理员", ROLE_PATROL: "巡考员",
             ROLE_INVIGILATOR: "监考员",
             ROLE_COMMANDER: "指挥中心", ROLE_CHECKER: "核验员", ROLE_VIEWER: "只读"}

# 全部动作（所有者拥有这一整套）
ALL_ACTIONS = frozenset({"view", "review", "report", "dispatch", "export", "verify",
                         "manage"})          # manage = 终端账户管理（仅所有者）

# 各角色能做的动作（权限最小化：默认拒绝，显式放行）
ROLE_ACTIONS = {
    ROLE_OWNER:       set(ALL_ACTIONS),                    # 最高权限
    # 管理员：日常全权，但没有 manage（终端账户管理），也没有清空本地数据
    ROLE_ADMIN:       set(ALL_ACTIONS) - {"manage"},
    ROLE_PATROL:      {"view", "review", "report", "dispatch", "export"},
    ROLE_INVIGILATOR: {"view", "report"},                 # 不能复核（避免自己判自己）
    ROLE_COMMANDER:   {"view", "dispatch", "export"},      # 不直接复核
    ROLE_CHECKER:     {"view", "verify"},
    ROLE_VIEWER:      {"view"},
}

# ---------------- 脱敏（第二节第 4 条：默认脱敏展示） ----------------
MASK_DEFAULT = True                 # 默认脱敏；只有完成复核留痕才可临时查看原图

# ---------------- 断网与补传（第二节第 3 条） ----------------
OFFLINE_RETRY_SEC = 5               # 队列重试间隔
OFFLINE_MAX_BATCH = 50              # 单次补传条数
OFFLINE_MAX_AGE_HOURS = 72          # 缓存最长保留

# ---------------- 统一管控（第二节第 5 条） ----------------
MDM_APP_WHITELIST_DEFAULT = ["terminal_gui", "face_verify", "clock", "video"]
MDM_PERIPHERAL_DEFAULT = {"usb_storage": False, "camera": True, "mic": True,
                          "bluetooth": False, "wifi_direct": False}

# ---------------- 稳定性（第三节：连续运行 ≥72 小时） ----------------
SOAK_TARGET_HOURS = 72
SOAK_HEARTBEAT_SEC = 30

# ---------------- 平台 / 边缘对接（第四节） ----------------
PLATFORM_URL = os.environ.get("EXAM_PLATFORM_URL", "").strip()     # 项目四 巡考业务平台
EDGE_URL = os.environ.get("EXAM_EDGE_URL", "").strip()             # 项目一 边缘 AI 节点
AUTH_URL = os.environ.get("EXAM_AUTH_URL", "").strip()             # 项目三 统一身份认证
HTTP_TIMEOUT_SEC = 5

# 01 人脸核验模块所在目录（"手持核验终端"通过隔离子进程调用它）
FACE_MODULE_DIR = os.environ.get(
    "EXAM_FACE_MODULE_DIR",
    str(BASE_DIR.parent / "01_人脸识别与身份核对" / "01_人脸识别与身份核对")).strip()

# 01 的命令控制台实现目录（_cmd_bridge.py 在隔离子进程里 import 它的 cmd_console）。
# 默认与上面是同一个 01 目录；可单独指到别处，便于把命令控制台部署在另一份副本上。
CMD_CONSOLE_DIR = os.environ.get("EXAM_CMD_CONSOLE_DIR", FACE_MODULE_DIR).strip()

# ---------------- 时钟（第三节：标准时间） ----------------
CLOCK_MAX_DRIFT_SEC = 2             # 与标准时间偏差超过即告警
CLOCK_SYNC_INTERVAL_SEC = 300

# 考试指令播报时刻表（HH:MM -> 播报内容），按需在数据目录覆盖
DEFAULT_BROADCASTS = [
    ("08:45", "请考生入场，核对准考证与身份证"),
    ("08:55", "请考生将手机等电子设备关机放入指定位置"),
    ("09:00", "考试开始，请考生开始答题"),
    ("10:30", "距考试结束还有 30 分钟"),
    ("10:55", "距考试结束还有 5 分钟，请检查答题卡"),
    ("11:00", "考试结束，请立即停笔"),
]


def describe():
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "终端类型数": len(TERMINAL_TYPES),
        "告警分级": "/".join(LEVEL_META[l]["name"] for l in LEVELS),
        "角色": " ".join("%s=%s" % (ROLE_TEXT[k], _sorted_actions(k))
                        for k in ROLE_ACTIONS),
        "默认脱敏": MASK_DEFAULT,
        "命令控制台": ("已开启（所有者可执行系统命令 / Python 代码）" if CONSOLE_FLAG.exists()
                       else "关闭（默认）"),
        "平台对接": PLATFORM_URL or "（未配置，仅本地）",
        "边缘对接": EDGE_URL or "（未配置，仅本地）",
        "人脸模块": FACE_MODULE_DIR,
        "稳定性目标": "%d 小时" % SOAK_TARGET_HOURS,
    }


def _sorted_actions(role):
    return "|".join(sorted(ROLE_ACTIONS.get(role, ())))
