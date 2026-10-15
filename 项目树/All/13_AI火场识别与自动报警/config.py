# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 配置

定位：在既有「智能考场 AI 巡考系统」上补一层**安全底座**——
      平时安静地在后台盯画面，发现火光/烟雾立刻弹窗报警、留证据、记事件。

本模块刻意**不引入登录、口令、角色权限**（与 11_摄像头AI识别 同一取向）：
它是一台"值班设备"，不是业务系统。需要权限体系的告警复核仍走 03_智能终端操作。

数据默认落在本目录 data\，可用 EXAM_FIRE_HOME 重定向。

两条重要约定：
  1) **不谎报能力**：默认检测器是"多信号融合"的传统视觉算法（HSV 火色 + YCbCr 去肤色
     + 时序闪烁 + 区域增长 + 烟雾）。它不是深度学习模型，所以 thresholds 必须按现场标定。
     若本目录 models\ 下有 fire*.onnx，detector 会自动切到 ONNX 后端（见 detect.py）。
  2) 所有阈值都可以在界面上调、并落在 data\settings.json，不写死在代码里。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_FIRE_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "fire_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "fire.db"
LOG_DIR = DATA_ROOT / "logs"
EVENT_DIR = DATA_ROOT / "events"        # 每个火警事件一个子目录：抓拍/标注图/录像
CLIP_DIR = DATA_ROOT / "clips"          # 事件前后的短视频
SETTINGS_PATH = DATA_ROOT / "settings.json"
MODEL_DIR = BASE_DIR / "models"         # 可选：fire*.onnx 放这里
SOAK_LOG = LOG_DIR / "fire.log"

for _d in (DATA_ROOT, LOG_DIR, EVENT_DIR, CLIP_DIR, MODEL_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

#: data\hide_cmd.on 存在 -> 启动 .bat 会用 pythonw.exe 零黑窗口拉起
HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"


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


def log(message):
    """写运行日志（失败绝不影响主流程）。"""
    import time
    try:
        with open(str(SOAK_LOG), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


# ============================================================ 默认设置
# 火警级别：warning=疑似（提示）/ alarm=确认火警（弹窗+声音）
LEVEL_WARNING = "warning"
LEVEL_ALARM = "alarm"
LEVELS = (LEVEL_WARNING, LEVEL_ALARM)
LEVEL_META = {
    LEVEL_WARNING: {"name": "疑似火情", "color": "#e08b00", "sound": False,
                    "popup": True, "sla_sec": 120},
    LEVEL_ALARM:   {"name": "确认火警", "color": "#c00000", "sound": True,
                    "popup": True, "sla_sec": 30},
}

SETTINGS_DEFAULT = {
    # ---------- 摄像头 ----------
    # 本机摄像头用序号（0/1/2…）当地址；网络摄像机填 rtsp:// 或 http://
    "camera_url": "0",
    "camera_name": "本机摄像头",
    "camera_width": 0,          # 0 = 用设备默认
    "camera_height": 0,
    "camera_fps": 0,

    # ---------- 运行方式 ----------
    "start_minimized": True,    # 启动即最小化/隐藏到后台（托盘图标）
    "use_tray": True,           # 尝试建托盘图标（失败自动降级为普通最小化）
    "auto_start_monitor": True,  # 启动后立刻开始盯画面
    "render_interval_ms": 60,   # 界面刷新间隔（与检测节奏解耦）
    "detect_interval_ms": 200,  # 检测节奏：越小越灵敏越吃 CPU

    # ---------- 火色判据（HSV） ----------
    # OpenCV 的 H 是 0~179。火色跨越 红(0~10 / 160~179) 橙(10~25) 黄(25~35)
    "hsv_h_low_max": 35,        # 低段火色上界（红->黄）
    "hsv_h_red_min": 160,       # 高段红色下界
    "hsv_s_min": 90,            # 饱和度下限（火很艳）
    "hsv_v_min": 170,           # 亮度下限（火很亮）

    # ---------- 去误报：肤色排除（YCbCr） ----------
    # 人脸/手臂在 HSV 里也偏红黄，必须排掉；否则人脸一动就报警
    "skin_y_min": 60,
    "skin_y_max": 255,
    "skin_cb_min": 77,
    "skin_cb_max": 133,
    "skin_cr_min": 133,
    "skin_cr_max": 180,
    "skin_dilate_px": 9,        # 肤色掩膜外扩，避免只排掉中间、边缘仍判火

    # ---------- 形态学与区域 ----------
    "morph_kernel": 5,          # 开运算核（去椒盐）
    "min_fire_px": 220,         # 火色连通块最小面积（像素）——小亮点不算火
    "min_fire_ratio": 0.0012,   # 火色面积占画面比例下限
    "max_fire_ratio": 0.60,     # 超过该比例多为"整屏偏红"的异常，按可疑低权处理
    "max_regions": 6,           # 最多保留几个火色区域

    # ---------- 时序：闪烁 + 增长 ----------
    "flicker_window": 12,       # 闪烁统计窗口（帧）
    "flicker_v_min": 2.0,       # 亮度抖动下限（std，0~255）
    "flicker_area_min": 0.02,   # 火色面积相对抖动下限
    "growth_min": -0.15,        # 区域面积增长下限（-0.15 允许小幅收缩）

    # ---------- 烟雾（早期征兆） ----------
    "smoke_enable": True,
    "smoke_v_min": 70,          # 烟雾亮度下限（偏灰白）
    "smoke_v_max": 235,
    "smoke_s_max": 70,          # 烟雾饱和度上限（灰）
    "smoke_edge_max": 0.06,     # 边缘密度上限（烟很"糊"）
    "smoke_min_ratio": 0.06,    # 烟雾面积占比下限
    "smoke_rise_ratio": 0.03,   # 需要比基线涨这么多才算"在起烟"

    # ---------- 判定与确认闸门 ----------
    # 单帧不算数：必须连续 N 帧命中才出告警（防闪光灯/车灯/红布等瞬时误报）
    "confirm_frames": 4,
    "clear_frames": 12,         # 连续 N 帧无火才判"已解除"（防抖动反复弹窗）
    "alarm_threshold": 0.62,    # >= 该分数判"确认火警"
    "warning_threshold": 0.40,  # >= 该分数判"疑似火情"

    # ---------- 报警行为 ----------
    "sound_enable": True,
    "sound_period_ms": 900,     # 报警音节奏
    "popup_on_warning": False,  # 疑似火情是否也弹窗（默认只记事件+状态条变色）
    "popup_fullscreen_alert": True,  # 弹窗时把主窗口带到最前
    "pre_seconds": 6,           # 录像保留告警前 N 秒（环形缓冲）
    "post_seconds": 10,         # 告警后继续录 N 秒
    "clip_max_seconds": 60,     # 单个事件录像最长
    "save_snapshot": True,
    "save_marked": True,        # 存一张画了框的标注图（便于复核）
    "event_cooldown_sec": 20,   # 同一路摄像机两次建事件的最小间隔（防事件风暴）
    "keep_days": 30,            # 事件留档天数（超过自动清理，0=不清理）

    # ---------- 检测后端 ----------
    "backend": "auto",          # auto / vision / onnx
    "onnx_model_path": "",      # 留空 = 自动找 models\fire*.onnx
    "onnx_conf": 0.35,
    "onnx_input_dim": 640,
    "onnx_fire_classes": "fire,smoke",   # 模型里哪几类的名字算火/烟
}


def _as_int(v, lo, hi, default):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_float(v, lo, hi, default):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):    # NaN / inf
        return default
    return max(lo, min(hi, n))


#: 各键的合法范围：(类型, 下界, 上界)。读设置时一律夹取，防止手改 settings.json 写坏导致崩溃。
_RANGES = {
    "camera_url": ("str", None, None),
    "camera_name": ("str", None, None),
    "camera_width": ("int", 0, 7680),
    "camera_height": ("int", 0, 4320),
    "camera_fps": ("int", 0, 120),
    "start_minimized": ("bool", None, None),
    "use_tray": ("bool", None, None),
    "auto_start_monitor": ("bool", None, None),
    "render_interval_ms": ("int", 16, 2000),
    "detect_interval_ms": ("int", 50, 5000),
    "hsv_h_low_max": ("int", 5, 90),
    "hsv_h_red_min": ("int", 120, 179),
    "hsv_s_min": ("int", 30, 255),
    "hsv_v_min": ("int", 60, 255),
    "skin_y_min": ("int", 0, 255), "skin_y_max": ("int", 0, 255),
    "skin_cb_min": ("int", 0, 255), "skin_cb_max": ("int", 0, 255),
    "skin_cr_min": ("int", 0, 255), "skin_cr_max": ("int", 0, 255),
    "skin_dilate_px": ("int", 0, 61),
    "morph_kernel": ("int", 1, 31),
    "min_fire_px": ("int", 20, 200000),
    "min_fire_ratio": ("float", 0.0, 1.0),
    "max_fire_ratio": ("float", 0.05, 1.0),
    "max_regions": ("int", 1, 40),
    "flicker_window": ("int", 4, 120),
    "flicker_v_min": ("float", 0.0, 80.0),
    "flicker_area_min": ("float", 0.0, 1.0),
    "growth_min": ("float", -1.0, 1.0),
    "smoke_enable": ("bool", None, None),
    "smoke_v_min": ("int", 0, 255), "smoke_v_max": ("int", 0, 255),
    "smoke_s_max": ("int", 0, 255),
    "smoke_edge_max": ("float", 0.0, 1.0),
    "smoke_min_ratio": ("float", 0.0, 1.0),
    "smoke_rise_ratio": ("float", 0.0, 1.0),
    "confirm_frames": ("int", 1, 240),
    "clear_frames": ("int", 1, 600),
    "alarm_threshold": ("float", 0.05, 1.0),
    "warning_threshold": ("float", 0.0, 1.0),
    "sound_enable": ("bool", None, None),
    "sound_period_ms": ("int", 100, 10000),
    "popup_on_warning": ("bool", None, None),
    "popup_fullscreen_alert": ("bool", None, None),
    "pre_seconds": ("int", 0, 60),
    "post_seconds": ("int", 0, 300),
    "clip_max_seconds": ("int", 1, 600),
    "save_snapshot": ("bool", None, None),
    "save_marked": ("bool", None, None),
    "event_cooldown_sec": ("int", 0, 3600),
    "keep_days": ("int", 0, 3650),
    "backend": ("str", None, None),
    "onnx_model_path": ("str", None, None),
    "onnx_conf": ("float", 0.05, 1.0),
    "onnx_input_dim": ("int", 160, 1920),
    "onnx_fire_classes": ("str", None, None),
}

BOOL_TRUE = {"1", "true", "yes", "on", "是", "开"}


def coerce_settings(raw):
    """把任意来源（settings.json / 界面输入）的设置规整成安全值。永不抛异常。"""
    cfg = dict(SETTINGS_DEFAULT)
    if not isinstance(raw, dict):
        return cfg
    for k, spec in _RANGES.items():
        if k not in raw:
            continue
        kind, lo, hi = spec
        v = raw[k]
        if kind == "bool":
            if isinstance(v, bool):
                cfg[k] = v
            elif isinstance(v, (int, float)):
                cfg[k] = bool(v)
            else:
                cfg[k] = str(v).strip().lower() in BOOL_TRUE
        elif kind == "int":
            cfg[k] = _as_int(v, lo, hi, SETTINGS_DEFAULT[k])
        elif kind == "float":
            cfg[k] = _as_float(v, lo, hi, SETTINGS_DEFAULT[k])
        else:
            s = str(v)
            if k == "backend" and s not in ("auto", "vision", "onnx"):
                s = "auto"
            cfg[k] = s
    # 跨字段一致性
    if cfg["warning_threshold"] > cfg["alarm_threshold"]:
        cfg["warning_threshold"] = cfg["alarm_threshold"]
    if cfg["smoke_v_min"] > cfg["smoke_v_max"]:
        cfg["smoke_v_min"], cfg["smoke_v_max"] = cfg["smoke_v_max"], cfg["smoke_v_min"]
    if cfg["hsv_h_red_min"] <= cfg["hsv_h_low_max"]:
        cfg["hsv_h_red_min"] = min(179, cfg["hsv_h_low_max"] + 10)
    return cfg


def load_settings():
    """读设置（缺文件/坏文件/被写坏都返回可用的默认值，绝不抛异常）。"""
    import json
    raw = None
    try:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    return coerce_settings(raw)


def save_settings(settings):
    """写设置；返回 (ok, 说明)。只接受已知键，写入前先规整。"""
    import json
    try:
        merged = load_settings()
        incoming = {k: v for k, v in (settings or {}).items() if k in _RANGES}
        merged.update(incoming)
        merged = coerce_settings(merged)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(SETTINGS_PATH))     # 原子替换，写一半不会毁掉旧设置
        return True, str(SETTINGS_PATH)
    except (OSError, ValueError) as exc:
        return False, str(exc)


def level_for_score(score, cfg=None):
    """按当前阈值把 0~1 的分数映射到级别；不达疑似线返回 None（=正常）。"""
    cfg = cfg or load_settings()
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s >= float(cfg["alarm_threshold"]):
        return LEVEL_ALARM
    if s >= float(cfg["warning_threshold"]):
        return LEVEL_WARNING
    return None


def onnx_model_path(cfg=None):
    """找可选的 ONNX 火/烟检测模型。找不到返回 ""（调用方据此回落传统视觉）。"""
    cfg = cfg or load_settings()
    explicit = str(cfg.get("onnx_model_path") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    env = os.environ.get("EXAM_FIRE_MODEL", "").strip()
    if env and os.path.isfile(env):
        return env
    try:
        cands = sorted(MODEL_DIR.glob("fire*.onnx")) + sorted(MODEL_DIR.glob("*.onnx"))
    except OSError:
        return ""
    for p in cands:
        if p.is_file():
            return str(p)
    return ""


def describe(cfg=None):
    """给界面/自检用的一页式概览。"""
    cfg = cfg or load_settings()
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "事件留档": str(EVENT_DIR),
        "设置文件": str(SETTINGS_PATH),
        "摄像头": "%s（%s）" % (cfg["camera_url"], cfg["camera_name"]),
        "检测后端": cfg["backend"],
        "ONNX 模型": onnx_model_path(cfg) or "（无，用多信号传统视觉）",
        "确认帧数": cfg["confirm_frames"],
        "火警阈值": cfg["alarm_threshold"],
        "疑似阈值": cfg["warning_threshold"],
    }
