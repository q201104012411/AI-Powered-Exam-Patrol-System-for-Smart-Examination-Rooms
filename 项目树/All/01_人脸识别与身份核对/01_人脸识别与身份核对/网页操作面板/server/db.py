# -*- coding: utf-8 -*-
"""
数据库初始化（网页端）：用户表 + 与核验端共用的考生表。

关键变化：
1. 不再自带明文 id_card 列，改由 store.init_schema() 统一建表与迁移，
   人脸端与网页端因此共享同一个数据源（旧版是"GUI 读 JSON、网页写 SQLite"）；
2. 不再使用固定默认口令 admin/Admin@123，改为首次启动随机生成，
   写入 初始密码.txt 并在管理员改密后自动删除；
3. 新增 login_log / data_change_log 记录，谁在什么时候做了什么可追溯。
"""
import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
if FEATURE_DIR not in sys.path:
    sys.path.insert(0, FEATURE_DIR)

import config                                    # noqa: E402
from store import connect, init_schema           # noqa: E402

DB_PATH = str(config.DB_PATH)
# 初始口令文件属于运行期数据，统一放在数据根目录（纯英文路径）
INIT_PWD_FILE = str(config.DATA_ROOT / "init_admin_password.txt")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    return connect(DB_PATH)


def _random_password(length=12):
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)):
            return pwd


def init_db():
    """建表 + 迁移 + 初始化管理员。返回 {"created_admin": bool, "pwd_file": str|None}"""
    init_schema(DB_PATH)
    conn = get_db()
    result = {"created_admin": False, "pwd_file": None}
    try:
        conn.isolation_level = None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'teacher')),
                display_name TEXT NOT NULL,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, username TEXT, ip TEXT, ok INTEGER NOT NULL, detail TEXT)""")

        import bcrypt
        admin_count = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND is_active=1").fetchone()["c"]
        if admin_count == 0:
            pwd = os.environ.get("ADMIN_INIT_PASSWORD") or _random_password()
            pwd_hash = bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            conn.execute("""
                INSERT INTO users (username, password_hash, role, display_name,
                                   must_change_password, created_at)
                VALUES (?, ?, 'admin', '系统管理员', 1, ?)""", ("admin", pwd_hash, _now()))
            if not os.environ.get("ADMIN_INIT_PASSWORD"):
                with open(INIT_PWD_FILE, "w", encoding="utf-8") as f:
                    f.write("初始管理员账户（首次登录必须修改，改密后本文件会被自动删除）\n")
                    f.write("用户名：admin\n")
                    f.write("初始密码：%s\n" % pwd)
                    f.write("生成时间：%s\n" % _now())
                try:
                    os.chmod(INIT_PWD_FILE, 0o600)
                except OSError:
                    pass
                result["pwd_file"] = INIT_PWD_FILE
            result["created_admin"] = True
    finally:
        conn.close()
    return result


def log_login(username, ip, ok, detail=""):
    conn = get_db()
    try:
        conn.execute("INSERT INTO login_log (ts, username, ip, ok, detail) VALUES (?,?,?,?,?)",
                     (_now(), username, ip, 1 if ok else 0, detail))
        conn.commit()
    finally:
        conn.close()


def reset_admin_password():
    """
    把管理员口令重置为新的随机口令（用于消除历史遗留的默认口令 Admin@123）。
    返回 (ok, pwd_file or msg)
    """
    import bcrypt
    conn = get_db()
    try:
        row = conn.execute("SELECT id, username FROM users WHERE role='admin' "
                           "ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False, "库中没有管理员账户，请先执行 python db.py"
        pwd = os.environ.get("ADMIN_INIT_PASSWORD") or _random_password()
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1, is_active=1 "
                     "WHERE id=?",
                     (bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"),
                      row["id"]))
        conn.commit()
    finally:
        conn.close()
    if os.environ.get("ADMIN_INIT_PASSWORD"):
        return True, "已按 ADMIN_INIT_PASSWORD 环境变量重置（未写入文件）"
    with open(INIT_PWD_FILE, "w", encoding="utf-8") as f:
        f.write("管理员口令已重置（首次登录必须修改，改密后本文件会被自动删除）\n")
        f.write("用户名：%s\n" % row["username"])
        f.write("新密码：%s\n" % pwd)
        f.write("重置时间：%s\n" % _now())
    try:
        os.chmod(INIT_PWD_FILE, 0o600)
    except OSError:
        pass
    return True, INIT_PWD_FILE


if __name__ == "__main__":
    import sys as _sys
    if "--reset-admin" in _sys.argv:
        ok, info = reset_admin_password()
        print(("已重置管理员口令，初始密码文件：%s" % info) if ok else info)
        _sys.exit(0 if ok else 1)
    r = init_db()
    print("数据库：%s" % DB_PATH)
    if r["created_admin"]:
        print("已创建管理员账户 admin，初始密码见：%s" % r["pwd_file"])
        print("请立即登录并修改密码（改密后该文件自动删除）。")
    else:
        print("管理员账户已存在，未做改动。")
        print("若历史上使用过默认口令 Admin@123，请执行：python db.py --reset-admin")
