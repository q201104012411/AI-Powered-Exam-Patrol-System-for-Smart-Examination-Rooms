# -*- coding: utf-8 -*-
r"""
摄像头 AI 识别 · 配置

本子方案**只做两件事**：
  1) 摄像头添加与管理（登记台账）
  2) AI 框架识别（逐人框选 + 行为判定）

**刻意不做**：登录、口令、角色权限、审计链。本目录是纯工具，打开即用。
（需要权限体系的是 01/02/03，那几套有自己的 accounts/权限矩阵，不要混用。）

数据默认落在本目录 data\，可用 EXAM_AI_CAM_HOME 重定向。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _resolve_data_root():
    env = os.environ.get("EXAM_AI_CAM_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "ai_cam_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "cameras.db"
LOG_DIR = DATA_ROOT / "logs"
CAPTURE_DIR = DATA_ROOT / "captures"        # 抓拍留档
STREAM_LOG = LOG_DIR / "camera_stream.log"

for _d in (DATA_ROOT, LOG_DIR, CAPTURE_DIR):
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

# ---------------- AI 识别参数（与 02/03 同一套口径） ----------------
# 分析长边：HOG 的人体检测窗口是 128px 高，长边太小"坐着的人"会小于窗口必然漏检。
# 实测：416 -> 4~5 人 ｜ 640 -> 6 人 ｜ 1080p -> 7 人（合成 24 人教室）。
AI_MODE_DIM = {
    "standard": 640,        # 默认：约 33 ms/次/路
    "dense": 768,           # 密集考场：约 53 ms/次
    "energy": 416,          # 多路优先：约 8 ms/次
}
AI_MODE_INTERVAL_MS = {
    "standard": 500,
    "dense": 900,
    "energy": 1600,
}
BOUNDARY_RATIO = 0.92       # 警戒线：只有走到镜头前（画面底部 8%）才算越界

# 模型文件搜索位置：本目录 与 本目录\models\
MODEL_SUBDIR = "models"

# 渲染参数
RENDER_MS_BY_COUNT = ((2, 66), (4, 90), (9, 130))   # (路数上限, 间隔ms)，超过取最后一档
RENDER_MS_MAX = 200
MAX_PANEL_W = 480
MAX_PANEL_H = 300
INFO_BAR_MS = 500

# AI 设置文件（可手改，也可在界面里改）
AI_SETTINGS_PATH = DATA_ROOT / "ai_settings.json"
AI_SETTINGS_DEFAULT = {
    "ai_mode": "standard",      # standard / dense / energy
    "ai_input_dim": 0,          # 0=按 ai_mode 取预设
    "model_kind": "auto",       # auto / yolo / caffe
    "model_path": "",           # 显式指定模型，留空=自动查找
    "prefer_gpu": True,
    "use_onnxruntime": False,
    "yolo_conf": 0.30,
    "boundary_pct": 92,         # 警戒线位置（画面高度百分比）
    "draw_pose": True,          # 有骨骼点时是否画骨架
}


def load_ai_settings():
    """读 AI 设置（缺文件/坏文件都返回默认值，绝不抛异常）。"""
    import json
    cfg = dict(AI_SETTINGS_DEFAULT)
    try:
        if AI_SETTINGS_PATH.exists():
            data = json.loads(AI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in AI_SETTINGS_DEFAULT:
                    if k in data and data[k] is not None and data[k] != "":
                        cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_ai_settings(settings):
    """写 AI 设置；失败返回 False（不影响主流程）。"""
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


def ai_input_dim(cfg=None):
    """解析 AI 分析长边：显式 ai_input_dim 优先，否则按 ai_mode 取预设。"""
    cfg = cfg or load_ai_settings()
    try:
        explicit = int(cfg.get("ai_input_dim") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    return AI_MODE_DIM.get(str(cfg.get("ai_mode") or "standard"),
                           AI_MODE_DIM["standard"])


def ai_interval_ms(cfg=None):
    cfg = cfg or load_ai_settings()
    return AI_MODE_INTERVAL_MS.get(str(cfg.get("ai_mode") or "standard"),
                                   AI_MODE_INTERVAL_MS["standard"])


def ai_boundary_ratio(cfg=None):
    cfg = cfg or load_ai_settings()
    try:
        pct = float(cfg.get("boundary_pct") or 92)
    except (TypeError, ValueError):
        pct = 92.0
    return min(0.99, max(0.30, pct / 100.0))


def render_interval_ms(n_cameras):
    """按并发路数取渲染间隔（路数越多越慢，避免 CPU 打满）。"""
    for limit, ms in RENDER_MS_BY_COUNT:
        if n_cameras <= limit:
            return ms
    return RENDER_MS_MAX


def describe():
    cfg = load_ai_settings()
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "AI 分析长边": ai_input_dim(cfg),
        "AI 分析间隔": "%d ms" % ai_interval_ms(cfg),
        "警戒线": "画面高度 %.0f%%" % (ai_boundary_ratio(cfg) * 100),
        "模型搜索": "%s 与 %s" % (BASE_DIR, BASE_DIR / MODEL_SUBDIR),
    }
