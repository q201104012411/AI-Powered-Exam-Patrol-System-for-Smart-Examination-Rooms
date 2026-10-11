r"""
摄像头核查与视频质量诊断 · 配置

对应《摄像头核查与视频质量诊断.md》：
  在线状态 / 图像质量 / 视野完整性 / 录像完整性 / 时间一致性 / 音频链路 / 设备台账
  三级巡检：日常(30min)、考前专项(考前7天)、考前一小时自检(开考前30min)

数据默认落在本目录 data\，可用 EXAM_CAMERA_HOME 重定向。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_CAMERA_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "camera_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "camera.db"
LOG_DIR = DATA_ROOT / "logs"
BASELINE_DIR = DATA_ROOT / "baselines"      # 基准图（逐机位）
CAPTURE_DIR = DATA_ROOT / "captures"        # 巡检抓帧留档
ATTACH_DIR = DATA_ROOT / "attachments"      # 整改前后截图
REPORT_DIR = DATA_ROOT / "reports"          # 核查报告
SOAK_LOG = LOG_DIR / "patrol.log"

for _d in (DATA_ROOT, LOG_DIR, BASELINE_DIR, CAPTURE_DIR, ATTACH_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------- 隐藏命令行窗口（cmd 召唤可选择隐藏） ----------------
# 存在这个标记文件 ->「启动核查界面.bat」「启动核查网页版.bat」改用 pythonw.exe 零黑窗口启动。
# 由所有者界面里的勾选框写/删（03 与 01 项目同一做法）。
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
        try:
            f = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return None
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return str(path)

# ---------------- 缺陷目录（核查内容表） ----------------
SEV_INFO = "info"
SEV_WARN = "warn"
SEV_FAIL = "fail"

DEFECTS = {
    # 在线状态
    "HEARTBEAT_LOST":  ("心跳丢失", SEV_FAIL),
    "STREAM_FAIL":     ("取流失败", SEV_FAIL),
    "DEVICE_OFFLINE":  ("设备离线", SEV_FAIL),
    # 图像质量
    "BLUR":            ("画面模糊/失焦", SEV_FAIL),
    "UNDEREXPOSED":    ("画面过暗", SEV_WARN),
    "OVEREXPOSED":     ("画面过曝", SEV_WARN),
    "LOW_CONTRAST":    ("对比度过低", SEV_WARN),
    "COLOR_CAST":      ("偏色", SEV_WARN),
    "STRIPES":         ("条纹干扰", SEV_WARN),
    "SNOW":            ("雪花/噪声过大", SEV_WARN),
    "MOSAIC":          ("马赛克/块效应", SEV_WARN),
    "FROZEN":          ("画面冻结", SEV_FAIL),
    # 视野完整性
    "OCCLUDED":        ("镜头被遮挡/喷涂", SEV_FAIL),
    "VIEW_OFFSET":     ("视角偏移/被转向", SEV_FAIL),
    "COVERAGE_LOW":    ("覆盖完整性不足（疑有盲区）", SEV_WARN),
    # 录像完整性
    "REC_GAP":         ("录像丢段/不连续", SEV_FAIL),
    "REC_MISSING":     ("无录像可回放", SEV_FAIL),
    # 时间一致性
    "TIME_DRIFT":      ("时钟偏差超限", SEV_WARN),
    # 音频链路
    "AUDIO_SILENT":    ("拾音器无声", SEV_WARN),
    "AUDIO_NOISE":     ("持续噪声", SEV_WARN),
    # 台账
    "LEDGER_MISMATCH": ("台账不符（编码/考场/角度参数缺失）", SEV_WARN),
}

# 缺陷分级（与巡检/巡考联动共用）
FAIL_CODES = {k for k, (_, s) in DEFECTS.items() if s == SEV_FAIL}
WARN_CODES = {k for k, (_, s) in DEFECTS.items() if s == SEV_WARN}


def defect_name(code):
    return DEFECTS.get(code, (code, SEV_WARN))[0]


def defect_sev(code):
    return DEFECTS.get(code, (code, SEV_WARN))[1]


# ---------------- 图像质量阈值（无参考评价） ----------------
QUALITY = {
    "work_width": 640,          # 指标统一在 640 宽上计算，保证跨分辨率可比
    "blur_min": 60.0,           # 拉普拉斯方差下限（越小越糊）
    "contrast_min": 22.0,       # 亮度标准差下限
    "dark_mean": 45.0,          # 平均亮度低于此判过暗
    "dark_ratio": 0.55,         # 暗像素（<16）占比上限
    "bright_mean": 212.0,       # 平均亮度高于此判过曝
    "bright_ratio": 0.35,       # 亮像素（>240）占比上限
    "color_cast_max": 26.0,     # 通道均值最大偏离
    "stripe_max": 0.15,         # 行/列投影的周期分量占比
    "noise_max": 6.5,           # 中值滤波残差标准差上限
    "blockiness_max": 2.00,     # 块边界强度比（在原始分辨率上算）
    # 有基准图时的"相对"阈值倍数（多算法融合，压低误报）
    "stripe_gain": 1.8,
    "noise_gain": 1.6,
    "blockiness_gain": 1.20,
}

# ---------------- 基准图比对（视野完整性） ----------------
BASELINE = {
    "offset_max_px": 18.0,      # 平移超过该像素数判视角偏移
    "hfov_deg": 90.0,           # 水平视场角（用于像素->角度换算）
    "angle_max_deg": 5.0,       # 换算后的角度阈值
    "phase_min_response": 0.06, # 相位相关最低响应，低于则不做偏移判定（避免误判）
    "occlude_uniform_ratio": 0.45,   # 基准有纹理处丢失纹理的比例（遮挡判定主阈值）
    "occlude_uniform_gain": 1.15,    # 相对基准低纹理率的上升倍数
    "occlude_edge_drop": 0.45,       # 边缘密度相对基准的下降比例
    "coverage_min": 0.55,            # 有效纹理覆盖率下限（疑盲区）
    "diff_warn": 60.0,               # 与基准的平均绝对差（辅助信息）
}

# ---------------- 冻结帧 / 帧间差异 ----------------
FROZEN = {
    "frames": 3,                # 连续几帧几乎相同即判冻结
    "diff_max": 0.8,            # 帧间平均绝对差（0-255）低于此视为无变化
}

# ---------------- 录像完整性 ----------------
RECORDING = {
    "segment_sec": 300,         # 单段时长
    "gap_tolerance_sec": 30,    # 允许缺口
    "min_hours_for_check": 0.5, # 至少要有的时长
}

# ---------------- 时间一致性 ----------------
TIME_DRIFT_MAX_SEC = 1.0        # 关键指标：≤1 秒

# ---------------- 音频链路 ----------------
AUDIO = {
    "rms_silent": 0.004,        # 低于此判无声
    "rms_noise": 0.35,          # 高于此判持续噪声
    "zcr_noise": 0.35,          # 过零率过高视为噪声
    "min_seconds": 2.0,         # 至少要有这么多秒的采样
}

# ---------------- 三级巡检策略 ----------------
POLICY_DAILY = "daily"          # 日常自动巡检：每 30 分钟，全部考点
POLICY_PREEXAM = "preexam"      # 考前专项核查：考前 7 天，全部考场
POLICY_BEFORE = "before"        # 考前一小时自检：开考前 30 分钟，本场全部机位
POLICY_UPLOAD = "upload"        # 现场画面抽查：上传一张现场画面单独诊断（无摄像头时可用）

PATROLS = {
    POLICY_DAILY:   {"name": "日常自动巡检", "interval_min": 30,   "scope": "all",
                     "output": "在线率、质量异常清单"},
    POLICY_PREEXAM: {"name": "考前专项核查", "lead_days": 7,       "scope": "all_rooms",
                     "output": "逐考场核查报告、整改工单"},
    POLICY_BEFORE:  {"name": "考前一小时自检", "lead_min": 30,     "scope": "session_rooms",
                     "output": "自检通过确认、异常即时报修"},
    POLICY_UPLOAD:  {"name": "现场画面抽查", "scope": "one_device",
                     "output": "单机位质量与视野诊断（不含在线/录像/时钟/音频）"},
}

# ---------------- 关键指标（第五节） ----------------
KPI = {
    "online_rate_min": 0.99,        # 摄像机在线率 ≥ 99%
    "discover_latency_sec": 300,    # 故障自动发现时延 ≤ 5 分钟
    "coverage_rate_min": 1.00,      # 覆盖完整性 100%
    "preexam_complete_min": 1.00,   # 考前核查完成率 100%
    "clock_drift_max_sec": 1.0,     # 时钟一致性偏差 ≤ 1 秒
}

# ---------------- 与巡考联动（第四节第 4 条） ----------------
# 质量异常点位的告警可信度自动降权
CREDIBILITY = {
    "fail_factor": 0.5,             # 有 fail 级质量缺陷 -> 可信度 × 0.5
    "warn_factor": 0.8,             # 仅 warn 级 -> × 0.8
    "normal_factor": 1.0,
    "min_factor": 0.3,              # 降权下限，避免完全失效
}

# ---------------- 巡检抓帧 ----------------
CAPTURE_KEEP_DAYS = 30


def describe():
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "缺陷种类": len(DEFECTS),
        "巡检策略": " / ".join(p["name"] for p in PATROLS.values()),
        "在线率要求": "≥ %.0f%%" % (KPI["online_rate_min"] * 100),
        "故障发现时延": "≤ %d 秒" % KPI["discover_latency_sec"],
        "时钟偏差": "≤ %.1f 秒" % KPI["clock_drift_max_sec"],
    }
