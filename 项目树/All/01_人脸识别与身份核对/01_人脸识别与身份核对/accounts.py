# -*- coding: utf-8 -*-
"""
共享账户层：GUI（操作人员界面）与网页操作面板使用**同一套账户**，避免两套密码。

角色（权限从高到低）：
    owner  所有者  —— 可查看操作记录、可清空日志
    admin  管理员  —— 可查看操作记录，**不能清空日志**
    teacher 教师   —— 仅可核验

默认账户（首次初始化时创建）：
    owner    / EDY_POWERQ    系统所有者
    teacher1 / teacher1      考务管理员（默认口令较弱，登录时会提示尽快修改）

密码用 bcrypt 存储，与网页端 auth.py 一致。
"""
import os
import sqlite3

import config

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"
ROLE_LEVEL = {ROLE_OWNER: 3, ROLE_ADMIN: 2, ROLE_TEACHER: 1}
ROLE_TEXT = {ROLE_OWNER: "所有者", ROLE_ADMIN: "管理员", ROLE_TEACHER: "教师"}

# 默认账户：用户名 -> (密码, 角色, 显示名, 是否强制改密)
DEFAULT_ACCOUNTS = [
    ("owner", "EDY_POWERQ", ROLE_OWNER, "系统所有者", 0),
    ("teacher1", "teacher1", ROLE_ADMIN, "考务管理员", 1),
]

_USERS_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'teacher')),
        display_name TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT
    )"""


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def connect():
    """打开与网页端共用的数据库（users / login_log 表都在这里）"""
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _needs_role_migration(conn):
    """旧库的 users 表 CHECK 里没有 owner，需要重建表"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not row:
        return False
    return "owner" not in (row["sql"] or "")


def _migrate_roles(conn):
    """把 users 表重建为支持 owner 角色的版本（保留全部数据）"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.execute(_USERS_SQL)
    keep = [c for c in ("id", "username", "password_hash", "role", "display_name",
                        "must_change_password", "is_active", "created_at", "last_login")
            if c in cols]
    conn.execute("INSERT INTO users (%s) SELECT %s FROM users_old"
                 % (", ".join(keep), ", ".join(keep)))
    conn.execute("DROP TABLE users_old")
    # 旧库里如果有 admin 账户，保留并提示（不删除任何人的账号）
    return keep


def ensure_schema_and_seed(verbose=False):
    """
    建表 + 角色迁移 + 创建默认账户。可重复调用。
    返回 {"created": [...], "migrated": bool}
    """
    import bcrypt
    result = {"created": [], "migrated": False}
    conn = connect()
    try:
        conn.isolation_level = None
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
                            ).fetchone():
            conn.execute(_USERS_SQL)
        elif _needs_role_migration(conn):
            _migrate_roles(conn)
            result["migrated"] = True

        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, username TEXT, ip TEXT, ok INTEGER NOT NULL, detail TEXT)""")

        for username, password, role, display, must_change in DEFAULT_ACCOUNTS:
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if row:
                continue                       # 已存在则不动（不覆盖已改过的密码）
            pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            conn.execute("""INSERT INTO users (username, password_hash, role, display_name,
                            must_change_password, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (username, pwd_hash, role, display, must_change, _now()))
            result["created"].append((username, role))
            if verbose:
                print("  已创建账户：%-10s 角色=%s（%s）" % (username, role, display))
    finally:
        conn.close()
    return result


def authenticate(username, password, ip="local-gui"):
    """
    校验用户名口令。返回 (user_dict | None, msg)。
    兼容旧库里的 admin 账户（角色仍是 admin，不能清日志）。
    """
    import bcrypt
    username = (username or "").strip()
    if not username or not password:
        return None, "请输入用户名与密码"
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    if not row:
        log_login(username, ip, False, "用户不存在")
        return None, "用户名或密码错误"
    if not row["is_active"]:
        log_login(username, ip, False, "账户已禁用")
        return None, "该账户已被禁用，请联系所有者"
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), (row["password_hash"] or "").encode("utf-8"))
    except Exception:
        ok = False
    if not ok:
        log_login(username, ip, False, "口令错误")
        return None, "用户名或密码错误"

    user = {"uid": row["id"], "username": row["username"], "role": row["role"],
            "display_name": row["display_name"],
            "must_change_password": bool(row["must_change_password"])}
    conn = connect()
    try:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), row["id"]))
        conn.commit()
    finally:
        conn.close()
    log_login(username, ip, True, "登录成功")
    return user, ""


def log_login(username, ip, ok, detail=""):
    try:
        conn = connect()
        try:
            conn.execute("INSERT INTO login_log (ts, username, ip, ok, detail) VALUES (?,?,?,?,?)",
                         (_now(), username, ip, 1 if ok else 0, detail))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def change_password(username, old_password, new_password):
    """修改口令（GUI 与网页端共用）。返回 (ok, msg)"""
    import bcrypt
    if len(new_password or "") < 8:
        return False, "新口令至少 8 位"
    user, msg = authenticate(username, old_password)
    if not user:
        return False, msg
    conn = connect()
    try:
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE username=?",
                     (bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
                      username))
        conn.commit()
    finally:
        conn.close()
    return True, "口令已修改"


def can_clear_logs(role):
    """只有所有者可以清空日志；管理员一律不允许"""
    return role == ROLE_OWNER


# ---------------- 自动核验权限（所有者可授权给管理员） ----------------
AUTO_VERIFY_ADMIN_FLAG = os.path.join(str(config.DATA_ROOT), "auto_verify_allow_admin.on")


def can_auto_verify(role):
    """
    谁可以使用「自动核验」（免点击、人脸匹配即自动通过）：
      · 所有者：始终可以
      · 管理员：需所有者在本机授权（生成标记文件）
      · 教师/其他：不可以
    """
    if role == ROLE_OWNER:
        return True
    if role == ROLE_ADMIN and os.path.exists(AUTO_VERIFY_ADMIN_FLAG):
        return True
    return False


def set_auto_verify_admin(allow):
    """所有者设置：是否允许管理员使用自动核验。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(AUTO_VERIFY_ADMIN_FLAG), exist_ok=True)
        if allow:
            with open(AUTO_VERIFY_ADMIN_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(AUTO_VERIFY_ADMIN_FLAG):
            os.remove(AUTO_VERIFY_ADMIN_FLAG)
    except OSError as e:
        return False, str(e)
    return True, ("已允许管理员使用自动核验" if allow else "已收回管理员的自动核验权限")


def auto_verify_admin_allowed():
    return os.path.exists(AUTO_VERIFY_ADMIN_FLAG)


# ---------------- 手动添加考生权限（所有者可开启，默认关闭） ----------------
MANUAL_ADD_FLAG = os.path.join(str(config.DATA_ROOT), "manual_add.on")


def manual_add_admin_allowed():
    return os.path.exists(MANUAL_ADD_FLAG)


def can_manual_add(role):
    """
    谁可以「手动添加考生」（在界面上逐条录入）：
      · 所有者：始终可以（并可决定是否放开给管理员）
      · 管理员：**默认关闭**，需所有者开启后可用
      · 教师/其他：不可以
    名单导入（文件）与手动添加是两条独立权限，互不影响。
    """
    if role == ROLE_OWNER:
        return True
    if role == ROLE_ADMIN and manual_add_admin_allowed():
        return True
    return False


def set_manual_add(allow):
    """所有者设置：是否允许管理员手动添加考生。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(MANUAL_ADD_FLAG), exist_ok=True)
        if allow:
            with open(MANUAL_ADD_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(MANUAL_ADD_FLAG):
            os.remove(MANUAL_ADD_FLAG)
    except OSError as e:
        return False, str(e)
    return True, ("已允许管理员手动添加考生" if allow else "已关闭管理员的手动添加权限")


# ---------------- 大考模式（所有者开关，默认关闭） ----------------
# 语义（正向，和另外两个标记文件一致）：标记文件存在 = 大考模式开启。
#   · 开启：身份核验**必须使用身份证**（沿用 check_id_card 的加盐哈希比对），
#           核验项 = 身份证号 + 准考证号 + 座位号 + 姓名（四要素）；
#   · 关闭（默认）：身份证**不参与校验**（未登记身份证的考生也能核验），
#           核验项 = 准考证号 + 座位号 + 姓名（三要素），准考证号必填且不一致要拦截。
MAJOR_EXAM_FLAG = os.path.join(str(config.DATA_ROOT), "major_exam.on")


def major_exam_enabled():
    """大考模式是否开启（默认关闭：标记文件不存在即关闭）"""
    return os.path.exists(MAJOR_EXAM_FLAG)


def major_exam_allowed():
    """兼容命名：与 manual_add_admin_allowed 一致的风格"""
    return os.path.exists(MAJOR_EXAM_FLAG)


def set_major_exam(on):
    """所有者设置：是否开启大考模式（必须使用身份证）。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(MAJOR_EXAM_FLAG), exist_ok=True)
        if on:
            with open(MAJOR_EXAM_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(MAJOR_EXAM_FLAG):
            os.remove(MAJOR_EXAM_FLAG)
    except OSError as e:
        return False, str(e)
    return True, ("已开启大考模式：核验必须输入并校验身份证号（四要素）" if on
                  else "已关闭大考模式：核验改用准考证号，身份证不参与校验（三要素）")


# ---------------- 严格模式（所有者开关，默认**开启**） ----------------
# ⚠️ 语义**反向**，与上面两个开关不同，务必注意：
#   · 标记文件**不存在** = 严格模式**开启**（默认，保持原有行为：非现场来源一律拒绝放行）；
#   · 标记文件**存在**   = 严格模式已被所有者**关闭**（宽松：允许用本地图片核验，
#                          但审计记录必须如实标注"非现场来源"，不静默放宽）。
# 之所以反向，是因为默认值是"开启"，而默认状态下不应该存在任何标记文件
# （否则每次全新安装都要先写一个文件才能得到默认行为）。
STRICT_MODE_FLAG = os.path.join(str(config.DATA_ROOT), "strict_mode.on")


def strict_mode_enabled():
    """严格模式是否生效：标记文件不存在即严格（默认开启）"""
    return not os.path.exists(STRICT_MODE_FLAG)


def strict_mode_relaxed():
    """是否为"宽松模式"（所有者已关闭严格模式）"""
    return os.path.exists(STRICT_MODE_FLAG)


def set_strict_mode(on):
    """
    所有者设置：严格模式开关。返回 (ok, msg)

    on=True  严格模式**开启**（删除标记文件，回到默认状态）
    on=False 严格模式**关闭**（写标记文件，允许本地图片核验并留痕）
    """
    try:
        os.makedirs(os.path.dirname(STRICT_MODE_FLAG), exist_ok=True)
        if on:
            if os.path.exists(STRICT_MODE_FLAG):
                os.remove(STRICT_MODE_FLAG)
        else:
            with open(STRICT_MODE_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
    except OSError as e:
        return False, str(e)
    return True, ("已开启严格模式：非现场来源（选择本地图片 / 上传照片）一律拒绝放行" if on
                  else "已关闭严格模式：允许用本地图片核验，审计将如实标注非现场来源")


# ---------------- 命令控制台（所有者开关，默认**关闭**） ----------------
# 语义（正向，与 manual_add.on / major_exam.on 一致）：标记文件存在 = 已开启。
#   · 默认关闭：标记文件不存在，GUI 与网页端都不出现任何命令执行入口；
#   · 开启后分两级（见 cmd_console_tier）：
#       所有者           -> full    完整 cmd / python
#       管理员（被授权）  -> ollama  仅 `ollama run <模型>`，用于 AI 辅助判断
#       其他角色         -> none
#   · 每次执行都会写入审计哈希链（事后删改会被 verify_chain 检出）。
#
# 为什么管理员只能拿到 ollama 这一级：
# 完整命令执行等价于整机控制权（可读写考生生物特征库、改代码、装后门），因此不下放；
# 而 `ollama run` 是本地大模型的单向问答，借它做"辅助判断"不触碰系统状态，
# 属于可以下放的低风险能力。两级之间的边界由 cmd_console.parse_ollama_command() 保证：
#   · 其它程序、其它 ollama 子命令、以 - 开头的模型名 —— 无法表达成这一形式，直接拒绝；
#   · 模型名之后的管道 / 重定向 / 链接符号 —— 会整体成为提示词文本，因为执行不经过
#     shell，它们没有可被解释的路径（是"无效"，不是"被拒绝"，别把这两者混为一谈）。
CMD_CONSOLE_FLAG = os.path.join(str(config.DATA_ROOT), "cmd_console.on")
# 所有者授权：允许管理员使用「ollama AI 辅助判断」（默认关闭，且以总开关开启为前提）
CMD_CONSOLE_ADMIN_FLAG = os.path.join(str(config.DATA_ROOT), "cmd_console_admin.on")
# 人脸通过后是否自动带出报名库证件信息（默认开启；关闭后需手动点「按所选考生带入」）
AUTO_FILL_DOC_FLAG = os.path.join(str(config.DATA_ROOT), "auto_fill_doc.on")

CMD_TIER_NONE = "none"
CMD_TIER_OLLAMA = "ollama"
CMD_TIER_FULL = "full"


def cmd_console_enabled():
    """命令控制台总开关是否已由所有者开启（默认关闭：标记文件不存在即关闭）"""
    return os.path.exists(CMD_CONSOLE_FLAG)


def set_cmd_console(on):
    """所有者设置：是否开启命令控制台总开关。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(CMD_CONSOLE_FLAG), exist_ok=True)
        if on:
            with open(CMD_CONSOLE_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(CMD_CONSOLE_FLAG):
            os.remove(CMD_CONSOLE_FLAG)
    except OSError as e:
        return False, str(e)
    return True, ("已开启命令控制台：所有者可用完整 cmd，每次执行均写入审计哈希链" if on
                  else "已关闭命令控制台：执行入口均已停用（含管理员的 AI 辅助）")


def cmd_console_admin_allowed():
    """所有者是否已授权管理员使用 ollama AI 辅助（默认关闭）"""
    return os.path.exists(CMD_CONSOLE_ADMIN_FLAG)


def set_cmd_console_admin(allow):
    """所有者设置：是否授权管理员使用 ollama AI 辅助判断。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(CMD_CONSOLE_ADMIN_FLAG), exist_ok=True)
        if allow:
            with open(CMD_CONSOLE_ADMIN_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(CMD_CONSOLE_ADMIN_FLAG):
            os.remove(CMD_CONSOLE_ADMIN_FLAG)
    except OSError as e:
        return False, str(e)
    return True, ("已授权管理员使用 AI 辅助判断（仅限 `ollama run <模型>`，不能执行其它命令）"
                  if allow else "已收回管理员的 AI 辅助判断权限")


# 人脸通过后是否自动带出报名库证件信息（默认开启；关闭后需手动点「按所选考生带入」）
AUTO_FILL_DOC_FLAG = os.path.join(str(config.DATA_ROOT), "auto_fill_doc.on")
AUTO_FILL_DOC_OFF_FLAG = AUTO_FILL_DOC_FLAG + ".off"


def auto_fill_doc_enabled():
    """人脸通过后是否自动带出报名库证件信息（默认开启）"""
    # 显式关闭文件存在 = 用户主动关；否则都算开（兼容旧版本未创建任何文件的情况）
    if os.path.exists(AUTO_FILL_DOC_OFF_FLAG):
        return False
    return True


def set_auto_fill_doc(on):
    """所有者设置：人脸通过后是否自动带出报名库证件信息。返回 (ok, msg)"""
    try:
        os.makedirs(os.path.dirname(AUTO_FILL_DOC_FLAG), exist_ok=True)
        if on:
            if os.path.exists(AUTO_FILL_DOC_OFF_FLAG):
                os.remove(AUTO_FILL_DOC_OFF_FLAG)
            with open(AUTO_FILL_DOC_FLAG, "w", encoding="utf-8") as f:
                f.write("1\n")
        else:
            with open(AUTO_FILL_DOC_OFF_FLAG, "w", encoding="utf-8") as f:
                f.write("0\n")
    except OSError as e:
        return False, str(e)
    return True, ("已开启：人脸通过后将自动带出证件信息（只读）"
                  if on else "已关闭：人脸通过后不再自动带出证件信息，需手动查看")


def cmd_console_tier(role):
    """
    当前角色的命令控制台层级 —— **唯一的权限判定入口**，GUI 与网页端都调这里。

        总开关关闭           -> none   （任何人都不行）
        所有者               -> full   完整 cmd / python
        管理员（且被授权）    -> ollama 仅 `ollama run <模型>`
        其他角色             -> none

    管理员层级以**总开关开启**为前提：关掉总开关等于全部停用。
    """
    if not cmd_console_enabled():
        return CMD_TIER_NONE
    if role == ROLE_OWNER:
        return CMD_TIER_FULL
    if role == ROLE_ADMIN and cmd_console_admin_allowed():
        return CMD_TIER_OLLAMA
    return CMD_TIER_NONE


def can_cmd_console(role):
    """该角色是否有命令控制台的任何权限（任一层级即为 True）"""
    return cmd_console_tier(role) != CMD_TIER_NONE


# ---------------- 身份要素模式文案（GUI 与网页端共用，避免各处文案不一致） ----------------
_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


def identity_elements():
    """
    当前模式下参与核验的身份要素（有序列表）。
    注意：其中"姓名"由 verifier.document_info() 自动带出，供工作人员目视核对证件实物，
    不参与哈希/字符串比对（库内即同名，自比无意义）；算法实际比对的是其余各项。
    """
    if major_exam_enabled():
        return ["身份证号", "准考证号", "座位号", "姓名"]
    return ["准考证号", "座位号", "姓名"]


def identity_mode_name():
    """当前模式名：大考模式 / 常规模式"""
    return "大考模式" if major_exam_enabled() else "常规模式"


def identity_mode_title():
    """
    当前核验项名称。非大考模式下**不再叫"四要素"**，避免出现"必须身份证"的误导文案：
      大考模式 -> 「四要素（大考模式）」
      常规模式 -> 「三要素（常规模式）」
    """
    els = identity_elements()
    return "%s要素（%s）" % (_CN_NUM.get(len(els), str(len(els))), identity_mode_name())


def identity_mode_text():
    """一行说清"当前按哪几项核验"，用于界面标题、状态栏与文档"""
    return "%s：%s" % (identity_mode_title(), " + ".join(identity_elements()))


def identity_mode_detail():
    """补充说明（谁必填、身份证如何使用），用于界面小字提示"""
    if major_exam_enabled():
        return ("大考模式已开启：身份证号**必填**，需输入证件原件明文，走库内加盐哈希比对；"
                "未提供明文（例如只带出掩码）会被拦截。")
    return ("大考模式未开启：身份证**不参与校验**（未登记身份证的考生也可核验），"
            "以准考证号为准；准考证号必填，不一致会被拦截。")


def role_text(role):
    return ROLE_TEXT.get(role, role or "-")


# ---------------- 用户管理（GUI 所有者面板与网页端共用） ----------------
def list_users():
    """返回全部账户（不含密码哈希）"""
    conn = connect()
    try:
        rows = conn.execute("""SELECT id, username, role, display_name, is_active,
                               must_change_password, created_at, last_login
                               FROM users ORDER BY id""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _level(role):
    return ROLE_LEVEL.get(role, 0)


def can_manage(by_role, target_role):
    """
    账户管理越级防护（GUI 与网页共用同一判定）：
    只能管理**严格低于自己等级**的账户。
      · 所有者(3)：可管理 管理员(2)、教师(1)；不能新建/管理其他所有者
      · 管理员(2)：只能管理 教师(1)；**不能重置或删除所有者、也不能动同级管理员**
      · 教师(1)：不能管理任何账户
    这条规则专门堵住"管理员重置所有者密码 -> 直接夺权"的越级路径。
    """
    return _level(by_role) > _level(target_role)


def create_user(username, display_name, password, role="teacher", by_role=ROLE_OWNER):
    """新建账户：只能新建等级低于自己的账户（owner 不可被创建）"""
    import bcrypt
    username = (username or "").strip()
    if not username or not password:
        return False, "用户名和密码不能为空"
    if role not in (ROLE_ADMIN, ROLE_TEACHER):
        return False, "角色不合法（仅 admin / teacher）"
    if not can_manage(by_role, role):
        return False, "权限不足：%s不能新建「%s」账户" % (role_text(by_role), role_text(role))
    if len(password) < 8:
        return False, "密码至少 8 位"
    conn = connect()
    try:
        if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            return False, "用户名已存在"
        h = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute("""INSERT INTO users (username, password_hash, role, display_name,
                       must_change_password, created_at) VALUES (?,?,?,?,1,?)""",
                     (username, h, role, display_name or username, _now()))
        conn.commit()
    finally:
        conn.close()
    return True, "用户 %s 创建成功（首次登录需改密）" % username


def reset_user_password(username, new_password, by_role=ROLE_OWNER):
    """
    重置某账户密码（改后该账户下次登录强制改密）。

    ⚠️ 越级防护：只能重置**等级低于自己**的账户——
       管理员不得重置所有者或同级管理员的密码（否则等于直接夺权）。
    """
    import bcrypt
    if len(new_password or "") < 8:
        return False, "新密码至少 8 位"
    conn = connect()
    try:
        row = conn.execute("SELECT id, role FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return False, "用户不存在"
        if not can_manage(by_role, row["role"]):
            return False, ("权限不足：%s不能重置「%s」账户的密码"
                           % (role_text(by_role), role_text(row["role"])))
        h = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1 "
                     "WHERE id=?", (h, row["id"]))
        conn.commit()
    finally:
        conn.close()
    return True, "密码已重置，该账户下次登录需修改密码"


def delete_user(username, by_role=ROLE_OWNER):
    """删除账户：同样只能删等级低于自己的；且需保留至少一名管理员"""
    conn = connect()
    try:
        row = conn.execute("SELECT id, role FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return False, "用户不存在"
        if not can_manage(by_role, row["role"]):
            return False, ("权限不足：%s不能删除「%s」账户"
                           % (role_text(by_role), role_text(row["role"])))
        if row["role"] == ROLE_ADMIN:
            n = conn.execute("SELECT COUNT(*) c FROM users "
                             "WHERE role='admin' AND is_active=1").fetchone()["c"]
            if n <= 1:
                return False, "系统必须保留至少一名管理员"
        conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    return True, "已删除用户 %s" % username
