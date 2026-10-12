# -*- coding: utf-8 -*-
"""
统一配置：路径、阈值、合规开关。

【路径设计】
  代码在中文路径下无法避免（项目本身就叫"智能考场AI巡考系统"），
  但**运行数据**（数据库 / 特征索引 / 参考照 / 抓拍 / 日志）可以搬到纯英文短路径，
  这样既躲开 cv2.imread 之类的非 ASCII 坑，也不用动代码目录。

  数据根目录解析优先级：
    1) 环境变量 EXAM_HOME
    2) 本目录下的 exam_home.txt（一行文本，便于双击运行的场景）
    3) 缺省：本目录下的 data\\（与原结构兼容）

  一次性迁移：python enroll.py migrate-data

原则：所有可调参数集中在此，业务代码不写死常量。
"""
import os
import shutil
from pathlib import Path

# ============ 路径 ============
BASE_DIR = Path(__file__).resolve().parent          # 代码目录（可能是中文路径）
HOME_MARKER = BASE_DIR / "exam_home.txt"


def _read_marker():
    try:
        if HOME_MARKER.exists():
            for line in HOME_MARKER.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def _resolve_data_root() -> Path:
    env = (os.environ.get("EXAM_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    marker = _read_marker()
    if marker:
        return Path(marker).expanduser()
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()                    # 运行数据根目录
_LEGACY_DATA = BASE_DIR / "data"
# 数据根目录即数据目录：不再多套一层 data\，路径更短更直观
DATA_DIR = DATA_ROOT

PHOTO_DIR = DATA_ROOT / "ref_photos"
CAPTURE_DIR = DATA_ROOT / "captures"
LOG_DIR = DATA_ROOT / "logs"

# 报名库（唯一数据源）：核验端与网页端共用
DB_PATH = Path(os.environ.get("EXAM_DB", str(DATA_ROOT / "exam.db")))
# 兼容旧数据：JSON 报名库仅作为迁移来源
LEGACY_JSON_PATH = DATA_DIR / "candidates.json"
# 迁移来源（旧布局：数据库放在网页端目录里）
LEGACY_DB_PATH = BASE_DIR / "网页操作面板" / "server" / "exam.db"

# 特征索引（加密存储，替代明文参考照）
INDEX_PATH = DATA_ROOT / "face_index.enc"
INDEX_KEY_PATH = DATA_ROOT / "index.key"

AUDIT_LOG_PATH = DATA_ROOT / "audit.log"
ACTION_LOG_PATH = LOG_DIR / "gui_action.log"

for _d in (DATA_ROOT, DATA_DIR, LOG_DIR, PHOTO_DIR, CAPTURE_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def migrate_legacy_layout(dry_run=False):
    """
    把旧布局的运行数据收拢到当前数据根目录（幂等）。
    旧布局：data\\{audit.log, face_index.enc, ref_photos, logs} + 网页操作面板\\server\\exam.db
    新布局：<DATA_ROOT>\\{exam.db, face_index.enc, audit.log, ref_photos, logs}
    """
    moves = []
    pairs = [
        (LEGACY_DB_PATH, DB_PATH),
        (_LEGACY_DATA / "face_index.enc", INDEX_PATH),
        (_LEGACY_DATA / "index.key", INDEX_KEY_PATH),
        (_LEGACY_DATA / "audit.log", AUDIT_LOG_PATH),
        (_LEGACY_DATA / "audit.log.v1bak", DATA_ROOT / "audit.log.v1bak"),
        (_LEGACY_DATA / "audit.log.purged.bak", DATA_ROOT / "audit.log.purged.bak"),
        (_LEGACY_DATA / "candidates.json", LEGACY_JSON_PATH),
        (BASE_DIR / "logs" / "gui_action.log", LOG_DIR / "gui_action.log"),
        (BASE_DIR / "logs" / "gui_start.log", LOG_DIR / "gui_start.log"),
    ]
    for src, dst in pairs:
        try:
            if src.resolve() == dst.resolve():
                continue
        except OSError:
            pass
        if src.exists() and not dst.exists():
            moves.append((src, dst))

    for src, dst in ((_LEGACY_DATA / "ref_photos", PHOTO_DIR),
                     (_LEGACY_DATA / "captures", CAPTURE_DIR),
                     (DATA_ROOT / "data" / "ref_photos", PHOTO_DIR),
                     (DATA_ROOT / "data" / "captures", CAPTURE_DIR)):
        try:
            if src.resolve() == dst.resolve() or not src.exists():
                continue
        except OSError:
            continue
        for f in src.iterdir():
            if f.is_file() and not (dst / f.name).exists():
                moves.append((f, dst / f.name))

    for suffix in ("-wal", "-shm"):
        src = Path(str(LEGACY_DB_PATH) + suffix)
        dst = Path(str(DB_PATH) + suffix)
        try:
            same = str(src.resolve()) == str(dst.resolve())
        except OSError:
            same = False
        if not same and src.exists() and not dst.exists():
            moves.append((src, dst))

    if dry_run:
        return {"moved": 0, "计划": ["%s -> %s" % (s, d) for s, d in moves],
                "数据根目录": str(DATA_ROOT)}

    done, failed = [], []
    for src, dst in moves:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append("%s -> %s" % (src.name, dst))
        except OSError as e:
            failed.append("%s：%s" % (src, e))

    # 照片搬家后，报名库里的路径要跟着改写，否则建档会找不到文件
    rebased = rebase_photo_paths()
    return {"moved": len(done), "明细": done, "失败": failed,
            "路径改写": rebased["updated"], "路径失效": rebased["missing"],
            "数据根目录": str(DATA_ROOT)}


def resolve_photo(p):
    """
    解析参考照路径，兼容四种历史写法（数据目录搬迁后仍能工作）：
      1) 绝对路径且文件存在 —— 原样返回；
      2) 绝对路径但文件已不在（数据目录被搬走）—— 去当前 ref_photos 找同名文件；
      3) 相对代码目录（旧版 data/ref_photos/xxx.jpg）—— 依次在 cwd / 代码目录 / 数据目录找；
      4) 仅文件名 —— 直接在 ref_photos 下找。
    """
    if not p:
        return p
    raw = Path(str(p))
    if raw.is_absolute():
        candidates = [raw, PHOTO_DIR / raw.name]
        fallback = PHOTO_DIR / raw.name
    else:
        candidates = [Path.cwd() / raw, BASE_DIR / raw, DATA_ROOT / raw, PHOTO_DIR / raw.name]
        fallback = BASE_DIR / raw
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return fallback


def rebase_photo_paths():
    """
    把报名库里指向旧位置的参考照路径改写到当前数据根目录。
    迁移数据目录后必须执行一次，否则建档会报"文件不存在"。
    返回 {"updated": n, "missing": [...]}
    """
    import sqlite3
    if not Path(DB_PATH).exists():
        return {"updated": 0, "missing": []}
    try:
        import data_crypto
    except Exception:                                    # pragma: no cover - 极端降级
        data_crypto = None
    updated, missing = 0, []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT candidate_id, reference_photo FROM candidates").fetchall()
        for r in rows:
            raw = (r["reference_photo"] or "").strip()
            if not raw:
                continue
            # 参考照路径可能是加密存储：先解密再解析，回写时重新加密
            old = data_crypto.decrypt_field(raw) if data_crypto else raw
            old = (old or "").strip()
            if not old:
                continue
            new = resolve_photo(old)
            if not Path(new).exists():
                missing.append("%s：%s" % (r["candidate_id"], old))
                continue
            if str(new) != old:
                stored = data_crypto.encrypt_field(str(new)) if data_crypto else str(new)
                conn.execute("UPDATE candidates SET reference_photo=? WHERE candidate_id=?",
                             (stored, r["candidate_id"]))
                updated += 1
        conn.commit()
    except sqlite3.Error as e:
        return {"updated": 0, "missing": ["数据库读写失败：%s" % e]}
    finally:
        conn.close()
    return {"updated": updated, "missing": missing}


# ============ 识别阈值（原始余弦相似度口径，不是映射后的值） ============
# InsightFace buffalo_l(w600k_r50) 512 维特征，余弦相似度经验区间：
#   同一人通常 > 0.6；不同人通常 < 0.35。
# 误识率随底库规模放大，因此 1:N 阈值必须严于 1:1。
# 上线前必须用自建验证集重新标定（见 README「阈值标定」）。
def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


THRESHOLD_1TO1 = _f("FACE_THRESHOLD_1TO1", 0.55)
THRESHOLD_1TON = _f("FACE_THRESHOLD_1TON", 0.65)

MAX_RETRY = int(os.environ.get("FACE_MAX_RETRY", "2"))

# ---- 模糊匹配（灰区）----
# 落在 [阈值-灰区宽度, 阈值) 区间内的相似度不直接判不通过，而是转人工复验。
# 这样既不放过，也不仅凭一个数字就否定考生。
GRAY_MARGIN_1TO1 = _f("FACE_GRAY_MARGIN_1TO1", 0.10)   # 0.45 ~ 0.55 为灰区
GRAY_MARGIN_1TON = _f("FACE_GRAY_MARGIN_1TON", 0.10)

# ---- 双胞胎 / 撞脸 ----
# 若"本人得分"与"最像的另一名考生得分"差距小于该值，视为存在孪生/撞脸风险，
# 必须转人工复验（双人见证 + 辅助证件核验），不能由算法单独放行。
TWIN_MARGIN = _f("FACE_TWIN_MARGIN", 0.08)
# 建档时，任意两名考生的特征相似度超过该值即告警（提前发现撞脸考生）
TWIN_ENROLL_WARN = _f("FACE_TWIN_ENROLL_WARN", 0.45)

# 特征提取：最小可接受人脸边长（像素），低于此值视为画质不足
MIN_FACE_SIZE = int(os.environ.get("FACE_MIN_SIZE", "80"))
DET_SIZE = int(os.environ.get("FACE_DET_SIZE", "640"))

# 推理设备：auto / cpu / gpu
DEVICE = os.environ.get("FACE_DEVICE", "auto")


# ============ 图像质量与姿态（极端条件的处理） ============
# 昏暗 / 模糊 / 人脸过小 / 极端视角都会让特征失真，必须区别对待：
#   严重（severe）-> 一律转人工复验，要求重拍或人工核验；
#   告警（warn）  -> 结合得分决定，并且极端姿态单独转复验。
QUALITY_MIN_BRIGHTNESS = _f("FACE_QUALITY_MIN_BRIGHTNESS", 45)   # 亮度均值下限(L通道)
QUALITY_SEVERE_BRIGHTNESS = _f("FACE_QUALITY_SEVERE_BRIGHTNESS", 28)
QUALITY_MIN_SHARPNESS = _f("FACE_QUALITY_MIN_SHARPNESS", 35)     # 拉普拉斯方差下限
QUALITY_SEVERE_SHARPNESS = _f("FACE_QUALITY_SEVERE_SHARPNESS", 12)
QUALITY_MIN_FACE_RATIO = _f("FACE_QUALITY_MIN_FACE_RATIO", 0.06)  # 人脸框占画面面积比
QUALITY_SEVERE_FACE_RATIO = _f("FACE_QUALITY_SEVERE_FACE_RATIO", 0.02)

# 近似姿态阈值（基于 5 点关键点的估计值，用于触发复验，不是标定角度）
POSE_MAX_YAW = _f("FACE_POSE_MAX_YAW", 35)        # 侧脸
POSE_MAX_ROLL = _f("FACE_POSE_MAX_ROLL", 30)      # 歪头
POSE_MAX_PITCH_UP = _f("FACE_POSE_MAX_PITCH_UP", 35)   # 仰拍（相机低于人脸）
POSE_MAX_PITCH_DOWN = _f("FACE_POSE_MAX_PITCH_DOWN", 30)  # 俯拍

# 低质量图像是否先做增强（CLAHE 提亮 + 去噪 + 锐化）再重新检测/提特征
AUTO_ENHANCE = os.environ.get("FACE_AUTO_ENHANCE", "1") == "1"


# ============ 活体 / 现场性 ============
# 严格模式：不接受"文件选择的照片"作为现场证据，必须是设备实时抓拍且在时间窗内。
# 说明：本作品尚未实现真正的活体防伪（翻拍/面具检测），因此不宣称具备该能力；
#      严格模式是"缺少活体时的安全降级"——宁可拒绝，也不放行。
REQUIRE_LIVENESS = os.environ.get("FACE_REQUIRE_LIVENESS", "1") == "1"
CAPTURE_MAX_AGE_SEC = int(os.environ.get("FACE_CAPTURE_MAX_AGE", "30"))


# ============ 合规 ============
# 未登记"单独同意"的考生，是否禁止核验（合规硬约束，建议保持开启）
ENFORCE_CONSENT = os.environ.get("EXAM_ENFORCE_CONSENT", "1") == "1"
CONSENT_OK_VALUES = ("已同意", "已取得单独同意")

# 身份证号是否强制校验位校验。
# 默认关闭：当前使用演示数据（号码多为编造，校验位不成立）；
# 生产环境请设为 1，以校验位挡掉手误与伪造号码。
STRICT_ID_CHECKSUM = os.environ.get("STRICT_ID_CHECKSUM", "0") == "1"

# 数据保留期限（天）：到期由 audit.purge_expired / enroll --purge 清理
RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

# 日志轮转阈值（字节）
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(10 * 1024 * 1024)))

# 审计日志是否启用哈希链（关闭会降低防篡改能力，不建议）
AUDIT_HASH_CHAIN = os.environ.get("AUDIT_HASH_CHAIN", "1") == "1"


def describe():
    """打印当前生效配置，便于现场排查与答辩演示。"""
    return {
        "代码目录": str(BASE_DIR),
        "数据根目录": str(DATA_ROOT),
        "报名库(SQLite)": str(DB_PATH),
        "特征索引": str(INDEX_PATH),
        "阈值 1:1 / 1:N": "%.2f / %.2f" % (THRESHOLD_1TO1, THRESHOLD_1TON),
        "灰区宽度 1:1 / 1:N": "%.2f / %.2f（区间内转人工复验）" % (GRAY_MARGIN_1TO1, GRAY_MARGIN_1TON),
        "撞脸余量 / 建档告警": "%.2f / %.2f" % (TWIN_MARGIN, TWIN_ENROLL_WARN),
        "画质阈值 亮度/清晰度/人脸占比": "%.0f / %.0f / %.2f"
                                     % (QUALITY_MIN_BRIGHTNESS, QUALITY_MIN_SHARPNESS,
                                        QUALITY_MIN_FACE_RATIO),
        "姿态阈值 侧脸/歪头/仰拍/俯拍": "%.0f / %.0f / %.0f / %.0f"
                                       % (POSE_MAX_YAW, POSE_MAX_ROLL,
                                          POSE_MAX_PITCH_UP, POSE_MAX_PITCH_DOWN),
        "自动增强(低画质)": AUTO_ENHANCE,
        "最大重试": MAX_RETRY,
        "严格现场模式(需实时抓拍)": REQUIRE_LIVENESS,
        "抓拍有效期(秒)": CAPTURE_MAX_AGE_SEC,
        "强制单独同意": ENFORCE_CONSENT,
        "身份证校验位校验": STRICT_ID_CHECKSUM,
        "数据保留(天)": RETENTION_DAYS,
        "审计哈希链": AUDIT_HASH_CHAIN,
        "推理设备": DEVICE,
    }


def ensure_stdio(name="app.log"):
    """
    保证 stdout / stderr 可用。

    背景：用 pythonw.exe 隐藏命令行窗口启动时，sys.stdout 与 sys.stderr 都是 None，
    任何 print() 都会抛 AttributeError 导致程序直接退出（网页后端就"起不来"）。
    这里把缺失的输出重定向到 <数据目录>/logs/<name>，既避免崩溃，也留下排查日志。
    """
    import sys
    try:
        path = Path(DATA_ROOT) / "logs" / name
        if sys.stdout is not None and sys.stderr is not None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(str(path), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
        return str(path)
    except Exception:
        # 实在不行就塞一个丢弃型的输出对象，保证 print 不崩
        class _Null:
            def write(self, *a, **k):
                pass

            def flush(self):
                pass

            def isatty(self):
                return False
        try:
            import sys as _s
            if _s.stdout is None:
                _s.stdout = _Null()
            if _s.stderr is None:
                _s.stderr = _Null()
        except Exception:
            pass
        return None
