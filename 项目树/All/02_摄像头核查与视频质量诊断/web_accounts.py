# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 账户与权限（桌面端 gui.py 与网页端 app.py **共用这一份**）

设计要点（务必别改坏）：
1. 账户只存一处：`Store` 的 kv 表，键名 `web.accounts`（沿用网页端既有键名，
   老库里的账户不会丢）。桌面端**不再另起一套**，两边同一账号口令必然一致。
2. 口令格式与网页端历史实现逐字节一致：`pbkdf2$<salt_hex>$<dk_hex>`，
   PBKDF2-HMAC-SHA256、120000 轮、盐 16 字节十六进制、比较用 hmac.compare_digest。
   改了这里的任何一个参数，老库里已存的口令就再也验不过。
3. 权限判定只有一处：本模块的 CAN 表 + can()。网页端 @require(roles=...) 与
   桌面端 Session.can() 都走同一张表，避免"网页能删、桌面也能删，但规则不一样"。
4. 隐藏命令行窗口用标记文件（所有者界面勾选 → 写/删标记；.bat 启动时判断是否存在），
   与 01 项目同一做法。
"""
import hashlib
import hmac
import json
import os

import config

# ---------------- 角色 ----------------
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_LEVEL = {ROLE_OWNER: 3, ROLE_ADMIN: 2}
ROLE_TEXT = {ROLE_OWNER: "所有者", ROLE_ADMIN: "管理员"}

ACCOUNT_KEY = "web.accounts"
ITERATIONS = 120000
MIN_PWD_LEN = 6

# 首次运行自动预置（与网页端原有默认账户一致）
DEFAULT_ACCOUNTS = {
    "owner": {"pwd": "owner", "role": ROLE_OWNER, "name": "系统所有者"},
    "teacher1": {"pwd": "teacher1", "role": ROLE_ADMIN, "name": "考务管理员"},
}

# ---------------- 权限矩阵（唯一判定表） ----------------
# True  = 该角色可用；False = 界面禁用/隐藏，且方法层直接拒绝
CAN = {
    # 只读：查看台账、核查记录、工单、指标、报告
    "view":               {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 台账与基准图
    "device_add":         {ROLE_OWNER: True,  ROLE_ADMIN: True},
    "baseline_capture":   {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 现场画面抽查（网页上传一张画面做诊断）
    "inspect_upload":     {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 三级巡检与工单闭环
    "patrol_run":         {ROLE_OWNER: True,  ROLE_ADMIN: True},
    "workorder_operate":  {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 报告导出
    "report_export":      {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 修改本人口令（谁都能改自己的）
    "change_own_pwd":     {ROLE_OWNER: True,  ROLE_ADMIN: True},
    # 所有者专属
    "device_delete":      {ROLE_OWNER: True,  ROLE_ADMIN: False},
    "account_manage":     {ROLE_OWNER: True,  ROLE_ADMIN: False},
    "audit_view":         {ROLE_OWNER: True,  ROLE_ADMIN: False},
    "hide_cmd_toggle":    {ROLE_OWNER: True,  ROLE_ADMIN: True},
}

ACTION_TEXT = {
    "view": "查看台账/核查记录/工单/指标/报告",
    "device_add": "登记与修改机位",
    "baseline_capture": "采集/更新基准图",
    "inspect_upload": "上传现场画面抽查",
    "patrol_run": "执行三级巡检",
    "workorder_operate": "工单派单/提交整改/复核",
    "report_export": "导出核查报告",
    "change_own_pwd": "修改本人口令",
    "device_delete": "删除机位",
    "account_manage": "终端账户管理（新建/重置口令/删除）",
    "audit_view": "查看审计链",
    "hide_cmd_toggle": "命令行窗口隐藏开关",
}


def role_text(role):
    return ROLE_TEXT.get(role, role or "-")


def role_level(role):
    return ROLE_LEVEL.get(role, 0)


def can(action, role):
    """权限判定唯一入口：动作名 + 角色 -> 是否允许（未知角色一律拒绝）"""
    row = CAN.get(action)
    if not row:
        return False
    return bool(row.get(role, False))


def require_text(action, role=None):
    """越权提示文案：直接说清"XX 仅所有者可用"，不用静默失败"""
    name = ACTION_TEXT.get(action, action)
    return "权限不足：%s 仅所有者可用（当前角色：%s）" % (name, role_text(role))


def can_manage(by_role, target_role):
    """
    账户管理越级防护：只能管理**严格低于自己等级**的账户。
      · 所有者(3)：可管理 管理员(2)；不能新建/重置/删除其他所有者
      · 管理员(2)：不能管理任何账户（本表里 account_manage 已经是 owner-only）
    这条规则专门堵住"管理员重置所有者口令 -> 直接夺权"的越级路径。
    """
    return role_level(by_role) > role_level(target_role)


# ---------------- 口令哈希（与网页端历史格式完全一致） ----------------
def hash_password(pwd, salt=None):
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                             bytes.fromhex(salt), ITERATIONS).hex()
    return "pbkdf2$%s$%s" % (salt, dk)


def verify_password(pwd, stored):
    try:
        scheme, salt, dk = stored.split("$")
        if scheme != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                                   bytes.fromhex(salt), ITERATIONS).hex()
        return hmac.compare_digest(calc, dk)
    except (ValueError, AttributeError, TypeError):
        return False


# 兼容别名（网页端历史函数名）
_hash = hash_password
_verify = verify_password


# ---------------- 隐藏命令行窗口标记 ----------------
def hidden_cmd_on():
    return os.path.exists(str(config.HIDE_CMD_FLAG))


def set_hidden_cmd(on, store=None, operator=""):
    """写/删隐藏 cmd 标记文件；传了 store 就一并写审计。返回 (ok, 提示)"""
    path = str(config.HIDE_CMD_FLAG)
    try:
        if on:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("1\n")
        elif os.path.exists(path):
            os.remove(path)
    except OSError as e:
        return False, "设置失败：%s" % e
    if store is not None:
        store.log("隐藏命令行窗口", "开启" if on else "关闭", operator or "-")
    return True, ("已开启：下次启动用 pythonw 隐藏命令行窗口"
                  if on else "已关闭：下次启动会显示命令行窗口（便于看报错）")


# ---------------- 会话与权限 ----------------
class Session(object):
    """当前登录会话：用户名 + 角色 + can() 判定。桌面端与网页端语义一致。"""

    def __init__(self, username, role, name=""):
        self.username = username
        self.role = role
        self.name = name or username

    @property
    def role_text(self):
        return role_text(self.role)

    def can(self, action):
        return can(action, self.role)

    def deny_text(self, action):
        return require_text(action, self.role)

    def to_dict(self):
        return {"username": self.username, "role": self.role, "name": self.name,
                "role_text": self.role_text,
                "can_manage_accounts": self.can("account_manage")}


# ---------------- 账户存取（Store.kv -> web.accounts） ----------------
class Accounts(object):
    """
    账户仓库：读写 Store 的 kv 表（键 web.accounts），与网页端同一份数据。
    任何时候都只存**口令哈希**，明文不落库、不回显。
    """

    def __init__(self, store):
        self.store = store
        self.data = self.ensure()

    # ---- 读 ----
    def ensure(self):
        data = self.store.get_kv(ACCOUNT_KEY)
        if not isinstance(data, dict) or not data:
            data = {u: {"pwd": hash_password(v["pwd"]), "role": v["role"],
                        "name": v["name"]} for u, v in DEFAULT_ACCOUNTS.items()}
            self.store.set_kv(ACCOUNT_KEY, data)
            self.store.log("网页账户初始化", "预置 %d 个账户" % len(data), "system")
        fixed = False
        for u, rec in list(data.items()):
            if not isinstance(rec, dict):
                data[u] = {"pwd": hash_password("teacher1"), "role": ROLE_ADMIN,
                           "name": u}
                fixed = True
                continue
            if rec.get("role") not in ROLE_LEVEL:
                rec["role"] = ROLE_ADMIN
                fixed = True
            rec.setdefault("name", u)
        if fixed:
            self.store.set_kv(ACCOUNT_KEY, data)
        self.data = data
        return data

    def reload(self):
        return self.ensure()

    def usernames(self):
        return sorted(self.data.keys())

    def get(self, username):
        return (self.data or {}).get(username)

    def exists(self, username):
        return username in (self.data or {})

    def list_accounts(self):
        """给界面用：不含口令哈希，只给用户名/角色/名称/角色说明"""
        out = []
        for u in self.usernames():
            rec = self.data[u]
            out.append({"username": u, "role": rec.get("role", ROLE_ADMIN),
                        "name": rec.get("name", u),
                        "role_text": role_text(rec.get("role"))})
        return out

    def _save(self):
        self.store.set_kv(ACCOUNT_KEY, self.data)

    # ---- 登录 ----
    def authenticate(self, username, password):
        """
        校验口令并写审计（成功/失败都写），与网页端 /api/login 行为一致。
        返回 (Session 或 None, 提示文案)
        """
        u = (username or "").strip()
        self.ensure()
        rec = self.get(u)
        if not rec or not verify_password(password or "", rec.get("pwd", "")):
            self.store.log("登录失败", "用户名=%s（%s）" % (u or "-", "桌面端"),
                           u or "-")
            return None, "用户名或口令错误"
        role = rec.get("role", ROLE_ADMIN)
        self.store.log("登录成功", "%s（%s，桌面端）" % (u, role_text(role)), u)
        return Session(u, role, rec.get("name", u)), "登录成功"

    # ---- 写 ----
    def create(self, username, password, role, name="", by=None):
        """新建账户：角色只能低于操作者（owner 不能被新建）；返回 (ok, 提示)"""
        by = by or ROLE_OWNER
        u = (username or "").strip()
        pwd = password or ""
        if not u:
            return False, "用户名不能为空"
        if role not in ROLE_LEVEL:
            return False, "角色不合法（仅 owner / admin）"
        if not can_manage(by, role):
            return False, "权限不足：%s不能新建「%s」账户" % (role_text(by), role_text(role))
        if len(pwd) < MIN_PWD_LEN:
            return False, "口令至少 %d 位" % MIN_PWD_LEN
        self.ensure()
        if self.exists(u):
            return False, "用户名已存在：%s" % u
        self.data[u] = {"pwd": hash_password(pwd), "role": role,
                        "name": (name or u).strip() or u}
        self._save()
        self.store.log("账户新建", "%s（%s）" % (u, role_text(role)), by)
        return True, "已新建账户 %s（%s）" % (u, role_text(role))

    def reset_password(self, username, new_password, by=None):
        self.ensure()
        by = by or ROLE_OWNER
        u = (username or "").strip()
        rec = self.get(u)
        if not rec:
            return False, "账户不存在：%s" % u
        if not can_manage(by, rec.get("role", ROLE_ADMIN)):
            return False, "权限不足：%s不能重置「%s」账户的口令" % (by, role_text(rec.get("role")))
        if len(new_password or "") < MIN_PWD_LEN:
            return False, "口令至少 %d 位" % MIN_PWD_LEN
        rec["pwd"] = hash_password(new_password)
        self._save()
        self.store.log("重置口令", u, by)
        return True, "已重置 %s 的口令" % u

    def delete(self, username, by=None):
        """删除账户：不能删自己、不能删 owner、至少保留一个可用账户"""
        self.ensure()
        by = by or ROLE_OWNER
        u = (username or "").strip()
        rec = self.get(u)
        if not rec:
            return False, "账户不存在：%s" % u
        if u == by:
            return False, "不能删除当前登录的账户（%s）" % u
        if rec.get("role") == ROLE_OWNER:
            return False, "权限不足：不能删除所有者账户（%s）" % u
        if not can_manage(by, rec.get("role", ROLE_ADMIN)):
            return False, "权限不足：%s不能删除「%s」账户" % (role_text(by), role_text(rec.get("role")))
        if len(self.data) <= 1:
            return False, "至少保留一个可用账户"
        del self.data[u]
        self._save()
        self.store.log("账户删除", "%s（%s）" % (u, role_text(rec.get("role"))), by)
        return True, "已删除账户 %s" % u

    def change_password(self, username, old_password, new_password):
        """修改本人口令（桌面端与网页端同一判定）"""
        self.ensure()
        u = (username or "").strip()
        rec = self.get(u)
        if not rec:
            return False, "账户不存在：%s" % u
        if not verify_password(old_password or "", rec.get("pwd", "")):
            return False, "原口令不正确"
        if len(new_password or "") < MIN_PWD_LEN:
            return False, "新口令至少 %d 位" % MIN_PWD_LEN
        rec["pwd"] = hash_password(new_password)
        self._save()
        self.store.log("修改口令", u, u)
        return True, "口令已修改"


# ---------------- 给网页端用的模块级便捷函数（沿用原 app.py 的调用方式） ----------------
def ensure_accounts(store):
    """网页端起服务时调用：确保 kv 里有 web.accounts（老库不会被动过）"""
    return Accounts(store).ensure()


def dump(data):
    """调试用：只输出用户名/角色，绝不回显口令哈希"""
    return json.dumps({u: {"role": r.get("role")} for u, r in (data or {}).items()},
                      ensure_ascii=False)
