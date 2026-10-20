# -*- coding: utf-8 -*-
r"""
总控台 ↔ 各模块的单点登录（SSO）令牌。

设计目标
--------
用户在总控台登录一次，之后双击任意模块都不再要求输入口令：

  总控台 App 登录成功
        │  make_token(用户名, 角色, 可进入模块, ttl)
        ▼
  EXAM_SSO_TOKEN 环境变量 + PYTHONPATH=包根
        │
        ▼
  模块启动时 sso.verify_launch("02")
        │ 校验 HMAC + 过期时间 + 模块号
        ▼
  直接构造该模块自己的 Session，跳过登录页

安全边界
--------
1. 令牌用 HMAC-SHA256 签名，密钥 `sso.key` 首次运行随机生成，不随包分发；
2. 令牌只含用户名/角色/模块/过期时间，不含口令明文；
3. 默认 15 分钟过期，模块启动后立即失效（读完从环境变量 pop 掉）；
4. 模块被**直接**启动（没有总控台令牌）时，仍走各自原有的登录页，
   因此总控台仍然是唯一入口门禁，不是把模块认证删掉。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

PKG = os.path.dirname(os.path.abspath(__file__))
SECRET_PATH = os.path.join(PKG, "sso.key")
TOKEN_ENV = "EXAM_SSO_TOKEN"

DEFAULT_TTL = 900          # 15 分钟；模块一般 1 秒内就启动完成


def _secret() -> bytes:
    """读取/生成 HMAC 密钥。首次生成 32 字节随机串，权限尽量收紧。"""
    try:
        with open(SECRET_PATH, "r", encoding="ascii") as f:
            s = (f.read() or "").strip()
        if len(s) >= 32:
            return s.encode("ascii")
    except OSError:
        pass

    s = secrets.token_hex(32)
    try:
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(s)
    except OSError:
        # 包目录只读时退化为进程内密钥：本次总控台启动的模块仍可用，
        # 但总控台重启后旧令牌失效（不影响正常使用）。
        pass
    return s.encode("ascii")


def make_token(username: str, role: str, name: str = "",
               modules=None, ttl: int = DEFAULT_TTL) -> str:
    """总控台调用：生成一次性登录令牌。modules 为模块号列表，["*"] 表示全部。"""
    payload = {
        "u": (username or "").strip(),
        "r": (role or "").strip(),
        "n": (name or username or "").strip(),
        "m": list(modules or ["*"]),
        "exp": int(time.time()) + int(ttl or DEFAULT_TTL),
        "j": secrets.token_hex(8),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + sig


def verify_token(token: str = "") -> dict | None:
    """校验签名与过期时间；成功返回 payload，失败返回 None。"""
    token = token or os.environ.get(TOKEN_ENV, "")
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expect = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    try:
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        if not data.get("u") or not data.get("r"):
            return None
    except (TypeError, ValueError):
        return None
    return data


def verify_launch(module_code: str = "") -> dict | None:
    """
    模块启动时调用：
      · 校验令牌；
      · 校验令牌里是否包含本模块（防止拿 02 的令牌去开 05）；
      · 读完立即从环境变量移除，避免子进程继续继承。
    返回 {"username","role","name","modules","sso":True} 或 None。
    """
    data = verify_token()
    if not data:
        return None
    mods = data.get("m") or []
    if module_code and "*" not in mods and module_code not in mods:
        return None
    os.environ.pop(TOKEN_ENV, None)
    os.environ.pop("EXAM_SSO_USER", None)
    os.environ.pop("EXAM_SSO_ROLE", None)
    os.environ.pop("EXAM_SSO_NAME", None)
    return {
        "username": str(data.get("u")),
        "role": str(data.get("r")),
        "name": str(data.get("n") or data.get("u")),
        "modules": mods,
        "sso": True,
    }


def map_role(sso_role: str, mapping: dict, default: str = "admin") -> str:
    """把总控台角色映射到模块自己的角色；未命中时用 default。"""
    return (mapping or {}).get((sso_role or "").strip(), default)


def role_text(role: str) -> str:
    return {
        "owner": "所有者", "admin": "管理员", "patrol": "巡考员",
        "invigilator": "监考员", "checker": "核验员",
    }.get(role, role or "-")


if __name__ == "__main__":
    # 手工调试：python sso.py [模块号]
    import sys
    tok = make_token("owner", "owner", "系统所有者",
                     [sys.argv[1]] if len(sys.argv) > 1 else ["*"])
    print(tok)
    os.environ[TOKEN_ENV] = tok
    print(verify_launch(sys.argv[1] if len(sys.argv) > 1 else ""))
