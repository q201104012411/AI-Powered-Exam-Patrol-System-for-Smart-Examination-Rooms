# -*- coding: utf-8 -*-
"""
认证与权限。

修正的旧版问题：
1. JWT 密钥不再硬编码默认值（旧版 `os.environ.get("JWT_SECRET", "change-me-...")`
   等于把签名密钥公开在源码里，任何拿到源码的人都能伪造 admin token）；
   现在：优先环境变量，其次首次运行随机生成并保存到 .jwt_secret（权限收紧）；
2. require_auth 每次请求都回查数据库，账户被禁用立刻失效（旧版禁用后旧 token 仍可用 8 小时），
   并且角色以数据库为准，避免 token 里的旧角色越权；
3. 登录失败会写入 login_log，便于发现暴力破解。
"""
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
import jwt
from flask import jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
if FEATURE_DIR not in sys.path:
    sys.path.insert(0, FEATURE_DIR)

import config                                    # noqa: E402

# JWT 密钥属于运行期数据：放数据根目录，不进代码树，避免随代码一起被提交/打包
SECRET_FILE = str(config.DATA_ROOT / ".jwt_secret")
TOKEN_EXPIRE_HOURS = int(os.environ.get("TOKEN_EXPIRE_HOURS", "8"))

_INSECURE_DEFAULTS = {"", "change-me-in-production-9f8a7b6c5d4e3f2a1b", "secret", "changeme"}


def _load_secret():
    env = (os.environ.get("JWT_SECRET") or "").strip()
    if env and env not in _INSECURE_DEFAULTS:
        return env
    # 无环境变量：随机生成并持久化（不使用任何写死在源码里的默认密钥）
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved and saved not in _INSECURE_DEFAULTS:
            return saved
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _load_secret()
SESSION_INFO = ("生产环境请通过环境变量 JWT_SECRET 注入密钥，避免依赖本机 .jwt_secret 文件"
                if "JWT_SECRET" not in os.environ else "密钥来自环境变量 JWT_SECRET")


# ---------------- 口令 ----------------
def hash_password(pwd: str) -> str:
    return bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pwd: str, pwd_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pwd.encode("utf-8"), pwd_hash.encode("utf-8"))
    except Exception:
        return False


def validate_password_strength(pwd: str):
    if len(pwd or "") < 8:
        return False, "密码长度至少 8 位"
    if not re.search(r"[a-z]", pwd):
        return False, "密码必须包含小写字母"
    if not re.search(r"[A-Z]", pwd):
        return False, "密码必须包含大写字母"
    if not re.search(r"\d", pwd):
        return False, "密码必须包含数字"
    return True, ""


# ---------------- 令牌 ----------------
def generate_token(user_id: int, username: str, role: str) -> str:
    now = datetime.utcnow()
    payload = {"uid": user_id, "username": username, "role": role,
               "iat": now, "exp": now + timedelta(hours=TOKEN_EXPIRE_HOURS)}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(roles=None):
    """要求登录；可限定角色。角色与启用状态以数据库为准，不信任 token 内的旧值。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "未登录"}), 401
            payload = decode_token(auth[7:])
            if not payload:
                return jsonify({"error": "登录已过期，请重新登录"}), 401

            from db import get_db
            conn = get_db()
            try:
                row = conn.execute("SELECT id, username, role, is_active FROM users WHERE id=?",
                                   (payload.get("uid"),)).fetchone()
            finally:
                conn.close()
            if not row or not row["is_active"]:
                return jsonify({"error": "账户不存在或已被禁用"}), 401

            current = {"uid": row["id"], "username": row["username"], "role": row["role"]}
            if roles and current["role"] not in roles:
                return jsonify({"error": "无权限操作"}), 403
            request.user = current
            return f(*args, **kwargs)
        return wrapper
    return decorator
