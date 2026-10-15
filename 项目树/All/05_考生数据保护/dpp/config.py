# -*- coding: utf-8 -*-
r"""
考生数据保护 · 配置

路径策略与 01/02/03 三个子系统一致：数据默认落在本目录 data\ 下，
可用环境变量 EXAM_DPP_HOME 或本目录下的 dpp_home.txt 重定向。

本模块只放**路径、阈值、角色动作矩阵、保留期限**这些"可调参数"，
数据分类分级目录见 catalog.py（那是本专题第一节的落地）。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent      # 05_考生数据保护\


def _resolve_data_root():
    env = os.environ.get("EXAM_DPP_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    marker = BASE_DIR / "dpp_home.txt"
    if marker.exists():
        try:
            txt = marker.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser().resolve()
        except OSError:
            pass
    return BASE_DIR / "data"


DATA_ROOT = _resolve_data_root()
DB_PATH = DATA_ROOT / "dpp.db"
LOG_DIR = DATA_ROOT / "logs"
KEYRING_DIR = DATA_ROOT / "keyring"        # 主密钥（DPAPI 包裹）与国密密钥
EXPORT_DIR = DATA_ROOT / "exports"         # 导出审批件
REPORT_DIR = DATA_ROOT / "reports"         # 验收证据报告
VAULT_DIR = DATA_ROOT / "vault"            # 加密后的原始人脸图像
SOAK_LOG = LOG_DIR / "soak.log"

for _d in (DATA_ROOT, LOG_DIR, KEYRING_DIR, EXPORT_DIR, REPORT_DIR, VAULT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 隐藏命令行窗口标记（与 01/02/03 同一做法）
HIDE_CMD_FLAG = DATA_ROOT / "hide_cmd.on"

# ---------------- 角色与权限最小化 ----------------
# 与 02/03 的账户模型对齐：口令 PBKDF2-HMAC-SHA256 加盐，存同一 SQLite 的 kv 表。
ROLE_OWNER = "owner"            # 所有者：最高权限（含审批、销毁强制、账户管理、审计链）
ROLE_DPO = "dpo"                # 数据保护负责人：可审批与执行销毁，但不管账户
ROLE_AUDITOR = "auditor"        # 审计员：只读审计链与台账，不做业务操作
ROLE_OPERATOR = "operator"      # 业务操作员：登记考生数据、上传同意书，不能查看明文
ROLE_INVIGILATOR = "invigilator"  # 监考员：仅本考场、仅编号/座次，不可解密人脸
ROLE_VIEWER = "viewer"          # 只读看板

ROLE_LEVEL = {ROLE_VIEWER: 1, ROLE_INVIGILATOR: 1, ROLE_AUDITOR: 2,
              ROLE_OPERATOR: 2, ROLE_DPO: 3, ROLE_OWNER: 4}
ROLE_TEXT = {ROLE_OWNER: "所有者", ROLE_DPO: "数据保护负责人", ROLE_AUDITOR: "审计员",
             ROLE_OPERATOR: "业务操作员", ROLE_INVIGILATOR: "监考员",
             ROLE_VIEWER: "只读"}

# 全部动作；ROLE_ACTIONS 是"默认拒绝，显式放行"的白名单。
ALL_ACTIONS = frozenset({
    "view",             # 查看脱敏台账
    "register",         # 登记考生数据（采集）
    "consent",          # 录入/撤回单独同意
    "use",              # 目的限定使用（发起比对等）
    "reveal",           # 授权解密查看（原图/明文证件号），全程留痕
    "share",            # 发起跨层级共享
    "approve",          # 审批导出/共享
    "destroy",          # 执行销毁（到期自动 + 按申请）
    "force_destroy",    # 人工强制销毁（需双人确认）
    "rights",           # 受理考生权利申请（查阅/复制/更正/删除）
    "rights_decide",    # 决定考生权利申请结果
    "manage",           # 账户管理
    "audit_view",       # 查看审计链
    "hide_cmd_toggle",  # 隐藏命令行窗口
})

ROLE_ACTIONS = {
    ROLE_OWNER:       set(ALL_ACTIONS),
    ROLE_DPO:         set(ALL_ACTIONS) - {"manage", "hide_cmd_toggle"},
    ROLE_AUDITOR:     {"view", "audit_view", "rights_decide"},
    ROLE_OPERATOR:    {"view", "register", "consent", "use", "share", "rights"},
    ROLE_INVIGILATOR: {"view"},
    ROLE_VIEWER:      {"view"},
}

ACTION_TEXT = {
    "view": "查看脱敏台账", "register": "登记考生数据", "consent": "录入/撤回单独同意",
    "use": "目的限定使用", "reveal": "授权解密查看", "share": "发起跨层级共享",
    "approve": "审批导出/共享", "destroy": "执行销毁", "force_destroy": "强制销毁",
    "rights": "受理考生权利申请", "rights_decide": "决定权利申请结果",
    "manage": "账户管理", "audit_view": "查看审计链", "hide_cmd_toggle": "隐藏命令行窗口开关",
}

# 预置账户（首次运行自动写入 kv 的 accounts）
DEFAULT_ACCOUNTS = [
    # 用户名,       口令,          角色,              姓名,            可见考场（空=全部）
    ("owner",      "EDY_POWERQ",   ROLE_OWNER,        "系统所有者",     []),
    ("dpo1",       "dpo1",         ROLE_DPO,          "数据保护负责人", []),
    ("auditor1",   "auditor1",     ROLE_AUDITOR,      "独立审计员",     []),
    ("oper1",      "oper1",        ROLE_OPERATOR,     "考务操作员",     []),
    ("inv01",      "inv01",        ROLE_INVIGILATOR,  "监考员A01",      ["A-01"]),
]

# ---------------- 脱敏与展示 ----------------
MASK_DEFAULT = True                 # 默认脱敏；查看明文必须走 reveal（留痕）
MASK_KEEP_HEAD = 4                  # 证件号保留前 4 位
MASK_KEEP_TAIL = 4                  # 证件号保留后 4 位

# ---------------- 水印（防外泄溯源） ----------------
# 视频回看与截图自动叠加"操作人 + 时间 + 终端"三联水印。
WATERMARK_FMT = "{operator}|{ts}|{terminal}"
WATERMARK_TILE_ALPHA = 0.18          # 平铺水印透明度
WATERMARK_TILE_STEP = (260, 130)     # 平铺步长（像素）

# ---------------- 采集最小化 ----------------
# 不强制 30fps；人脸特征采集"按需最小"。
CAPTURE = {
    "fps_max": 8,                    # 采集帧率上限
    "fps_min_ok": 1,                 # 低于此值判"过度稀薄"，影响核验效果
    "silent_start": False,           # 是否允许静默开启（必须为 False）
    "consent_required": True,        # 采集前必须有单独同意记录
    "retention_image_days": 30,      # 原始图像最短必要保留期（≤30 天）
}

# ---------------- 传输 ----------------
TRANSPORT = {
    "require_private_network": True,  # 专网传输
    "require_encryption": True,       # 加密上传
    "prefer_sm": True,                # 国密优先（SM4/SM3），无国密库时回退 AES-GCM
    "plaintext_hosts_forbidden": True,
}

# ---------------- 到期自动销毁 ----------------
# 由系统强制执行，不接受人工"忘记删除"的例外；销毁生成不可篡改记录。
DESTROY = {
    "auto": True,                     # 自动销毁开关（关闭需在报告里显式标红）
    "grace_days": 0,                  # 到期后宽限天数（0=到期即删）
    "legal_hold_field": "legal_hold",  # 有法律保留必要时不删，但必须留理由
    "double_confirm": True,           # 强制销毁需双人确认
}

# ---------------- 考生权利响应时限（工作日） ----------------
RIGHTS_SLA_DAYS = {
    "access":   5,     # 查阅、复制
    "copy":     5,
    "correct":  5,     # 更正
    "delete":  10,     # 删除
    "withdraw": 0,     # 撤回同意：即时（提供非人脸替代通道）
}

# ---------------- 关键验收指标（第六节） ----------------
KPI = {
    "encrypt_rate_min": 1.00,          # 敏感数据加密率 100%
    "audit_cover_min": 1.00,           # 操作审计覆盖率 100%（登录、查询、导出、删除）
    "destroy_rate_min": 1.00,          # 到期自动销毁执行率 100%
    "privilege_deny_all": True,        # 越权访问测试全部拦截
    "alt_channel_min": 1.00,           # 替代通道可用性 100%
}

# 审计必须覆盖的动作（用于计算覆盖率，缺一即不达标）
AUDIT_REQUIRED_ACTIONS = (
    "登录", "查询", "导出", "删除", "解密查看", "共享", "登记", "同意",
    "权利申请", "销毁", "账户",
)

# ---------------- 应急响应 ----------------
INCIDENT = {
    "report_within_hours": 2,          # 泄露事件 2 小时内上报
    "levels": {"S1": "重大（涉及大量考生敏感信息）", "S2": "较大", "S3": "一般"},
}

# ---------------- 日志脱敏 ----------------
LOG_SANITIZE = True


def describe():
    return {
        "数据目录": str(DATA_ROOT),
        "数据库": str(DB_PATH),
        "密钥目录": str(KEYRING_DIR),
        "角色": " ".join("%s=%s" % (ROLE_TEXT[k], "|".join(sorted(ROLE_ACTIONS[k])))
                        for k in ROLE_ACTIONS),
        "默认脱敏": MASK_DEFAULT,
        "采集帧率上限": CAPTURE["fps_max"],
        "国密优先": TRANSPORT["prefer_sm"],
        "自动销毁": DESTROY["auto"],
        "隐藏命令行窗口": HIDE_CMD_FLAG.exists(),
    }
