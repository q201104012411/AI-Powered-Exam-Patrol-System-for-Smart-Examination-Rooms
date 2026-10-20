# -*- coding: utf-8 -*-
r"""
总控台账户与口令（独立于各子模块的账户体系，只负责"进入模块"这一道门禁）

设计取舍：
  · 总控台是单文件 exe 的入口，必须先有一道统一的登录门禁；
  · 账户默认值与 03 智能终端保持一致（owner/EDY_POWERQ、admin/admin、
    patrol1/patrol1、inv01/inv01、check1/check1），现场不需要再记一套新口令；
  · 口令只存 PBKDF2-HMAC-SHA256 加盐哈希，不落明文；
  · 每个账户带"可进入模块"清单，owner 为 *（全部）。

诚实说明：这是界面入口门禁，不是文件系统级权限。能拿到 exe 解压目录的人
仍可直接用 runtime\python.exe 打开某个模块脚本；要防这一层需要 NTFS ACL
或把模块放到受控服务端。本文件只保证"从总控台点进去"这条路径是受控的。
"""
import hashlib
import hmac
import json
import os
import time

_ITER = 120000

ROLE_TEXT = {
    "owner": "所有者",
    "admin": "管理员",
    "patrol": "巡考员",
    "invigilator": "监考员",
    "checker": "核验员",
}
ROLE_LEVEL = {"owner": 3, "admin": 2, "patrol": 1,
              "invigilator": 1, "checker": 1}

# 默认账户：用户名 / 口令 / 角色 / 姓名 / 可进入模块（"*"=全部）
DEFAULT_ACCOUNTS = [
    ("owner", "EDY_POWERQ", "owner", "系统所有者", "*"),
    ("admin", "admin", "admin", "考务管理员",
     ["01", "02", "03", "05", "06", "07", "08", "09", "10", "11"]),
    ("patrol1", "patrol1", "patrol", "巡考员01",
     ["01", "02", "03", "10", "11"]),
    ("inv01", "inv01", "invigilator", "监考员A01", ["03", "10"]),
    ("check1", "check1", "checker", "核验员01", ["01"]),
]


def hash_pwd(password, salt=None):
    """与 03 的 access.py 同一格式：pbkdf2$<salt hex>$<dk hex>"""
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _ITER).hex()
    return "pbkdf2$%s$%s" % (salt, dk)


def verify_pwd(password, stored):
    try:
        scheme, salt, dk = str(stored).split("$")
        if scheme != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(salt), _ITER).hex()
        return hmac.compare_digest(calc, dk)
    except (ValueError, AttributeError, TypeError):
        return False


def role_text(role):
    return ROLE_TEXT.get(role, role or "-")


class AccountStore(object):
    """总控台账户（JSON + PBKDF2；文件丢失/损坏时自动重建默认账户）"""

    def __init__(self, path):
        self.path = path
        self._data = None
        self.load()

    # ---------------- 持久化 ----------------
    def _seed(self):
        data = {}
        for user, pwd, role, name, modules in DEFAULT_ACCOUNTS:
            data[user] = {
                "pwd": hash_pwd(pwd),
                "role": role,
                "name": name,
                "modules": modules,
                "must_change": True,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        return data

    def load(self):
        if self._data is not None:
            return self._data
        data = None
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not data:
                    data = None
        except Exception:
            data = None
        if data is None:
            # 损坏的旧文件留档，方便排查；随后用默认账户重建
            try:
                if os.path.exists(self.path):
                    os.replace(self.path,
                               self.path + ".bad-%s" % time.strftime("%Y%m%d_%H%M%S"))
            except OSError:
                pass
            data = self._seed()
            self._data = data
            self.save()
        else:
            # 老文件缺字段时补齐（不覆盖已有口令/角色/模块）
            for user, pwd, role, name, modules in DEFAULT_ACCOUNTS:
                rec = data.setdefault(user, {})
                rec.setdefault("pwd", hash_pwd(pwd))
                rec.setdefault("role", role)
                rec.setdefault("name", name)
                rec.setdefault("modules", modules)
                rec.setdefault("must_change", True)
            self._data = data
        return data

    def save(self):
        data = self._data or {}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True, ""
        except Exception as e:                       # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False, str(e)

    # ---------------- 查询 / 认证 ----------------
    def list_accounts(self):
        return self.load()

    def get(self, username):
        return self.load().get(username)

    def authenticate(self, username, password):
        username = (username or "").strip()
        rec = self.load().get(username)
        if not rec:
            return None, "账户不存在"
        if not verify_pwd(password or "", rec.get("pwd", "")):
            return None, "账户或口令不正确"
        # 记录里本来只有 pwd/role/name/modules，没有用户名；返回副本并补上，
        # 供审计、状态栏、03 自动登录和账户管理标题使用。
        out = dict(rec)
        out["username"] = username
        return out, ""

    @staticmethod
    def can_access(rec, code):
        if not rec:
            return False
        mods = rec.get("modules", [])
        if mods == "*" or mods is True:
            return True
        if isinstance(mods, str):
            mods = [x.strip() for x in mods.split(",") if x.strip()]
        return str(code) in [str(m) for m in (mods or [])]

    @staticmethod
    def modules_text(rec):
        mods = rec.get("modules", [])
        if mods == "*" or mods is True:
            return "全部"
        if isinstance(mods, str):
            mods = [x.strip() for x in mods.split(",") if x.strip()]
        return "、".join(str(m) for m in (mods or [])) or "—"

    # ---------------- 账户管理（仅总控台 owner 调用） ----------------
    def create(self, username, password, role, name="", modules="*"):
        username = (username or "").strip()
        if not username:
            return False, "用户名不能为空"
        if len(password or "") < 6:
            return False, "口令至少 6 位"
        if role not in ROLE_TEXT:
            return False, "角色不合法"
        data = self.load()
        if username in data:
            return False, "账户已存在"
        data[username] = {
            "pwd": hash_pwd(password),
            "role": role,
            "name": name or username,
            "modules": modules if modules not in (None, "") else "*",
            "must_change": True,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        ok, err = self.save()
        return (True, "已新增账户 %s" % username) if ok else (False, err)

    def reset_password(self, username, password):
        if len(password or "") < 6:
            return False, "口令至少 6 位"
        data = self.load()
        rec = data.get(username)
        if not rec:
            return False, "账户不存在"
        rec["pwd"] = hash_pwd(password)
        rec["must_change"] = False
        ok, err = self.save()
        return (True, "已重置 %s 的口令" % username) if ok else (False, err)

    def set_modules(self, username, modules):
        rec = self.load().get(username)
        if not rec:
            return False, "账户不存在"
        rec["modules"] = modules if modules not in (None, "") else "*"
        ok, err = self.save()
        return (True, "已更新 %s 的模块范围" % username) if ok else (False, err)

    def delete(self, username):
        data = self.load()
        rec = data.get(username)
        if not rec:
            return False, "账户不存在"
        if rec.get("role") == "owner":
            owners = [u for u, r in data.items() if r.get("role") == "owner"]
            if len(owners) <= 1:
                return False, "至少保留一名所有者账户"
        data.pop(username, None)
        ok, err = self.save()
        return (True, "已删除账户 %s" % username) if ok else (False, err)
