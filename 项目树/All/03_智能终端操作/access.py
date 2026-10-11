# -*- coding: utf-8 -*-
"""
权限最小化与视图隔离（《智能终端.md》第二节第 4 条）

规则：
  · 默认拒绝 —— 角色不在 ROLE_ACTIONS 白名单里的动作一律拒绝
  · 监考终端只能看本考场（按 room_id 过滤）
  · 默认脱敏展示；只有完成复核留痕后才允许查看原图/完整信息
  · 终端账户口令用 PBKDF2-HMAC-SHA256 加盐存储，不落明文
"""
import hashlib
import hmac
import json
import os

import config
import models

_ITER = 120000

DEFAULT_ACCOUNTS = [
    # 用户名,    口令,        角色,                 姓名,      可见考场（空=全部）
    ("patrol1", "patrol1", config.ROLE_PATROL,      "巡考员01", []),
    ("inv01",   "inv01",   config.ROLE_INVIGILATOR, "监考员A01", ["A-01"]),
    ("Owner",   "EDY_POWERQ", config.ROLE_OWNER,     "系统所有者", []),
    # 管理员：日常全权（巡检/处置/复核/核验），但没有终端账户管理与危险操作
    ("admin",   "admin",   config.ROLE_ADMIN,       "考务管理员", []),
    ("check1",  "check1",  config.ROLE_CHECKER,     "核验员01", []),
]


class AccessError(Exception):
    pass


def hash_pwd(password, salt=None):
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _ITER).hex()
    return "pbkdf2$%s$%s" % (salt, dk)


def verify_pwd(password, stored):
    try:
        scheme, salt, dk = stored.split("$")
        if scheme != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(salt), _ITER).hex()
        return hmac.compare_digest(calc, dk)
    except (ValueError, AttributeError):
        return False


class Session(object):
    """一次终端登录会话：带角色与可见考场范围"""

    def __init__(self, username, role, display_name="", rooms=None):
        self.username = username
        self.role = role
        self.display_name = display_name or username
        self.rooms = list(rooms or [])

    def can(self, action):
        return action in config.ROLE_ACTIONS.get(self.role, set())

    def require(self, action):
        if not self.can(action):
            raise AccessError("角色「%s」无权执行：%s"
                              % (config.ROLE_TEXT.get(self.role, self.role), action))
        return True

    def room_filter(self):
        """返回 None 表示全部考场；否则为允许的考场列表"""
        if self.role in (config.ROLE_OWNER, config.ROLE_ADMIN,
                         config.ROLE_PATROL, config.ROLE_COMMANDER):
            return None
        return self.rooms or ["__none__"]     # 未分配考场 -> 看不到任何数据

    def masked(self):
        return config.MASK_DEFAULT

    def describe(self):
        return "%s（%s）可见范围：%s" % (
            self.display_name, config.ROLE_TEXT.get(self.role, self.role),
            "全部考场" if self.room_filter() is None else "、".join(self.room_filter()))


class AccountStore(object):
    """终端账户（存在同一 SQLite 的 kv 表里，避免再多一个库）"""

    KEY = "accounts"

    def __init__(self, store):
        self.store = store

    # 旧账户名 -> (新账户名, 新口令, 新角色)。老库里已有的账户自动迁移。
    RENAMES = {"command": ("Owner", "EDY_POWERQ", config.ROLE_OWNER)}

    def ensure_seed(self):
        data = self.store.get_kv(self.KEY)
        if data is None:
            data = {}
            for u, p, role, name, rooms in DEFAULT_ACCOUNTS:
                data[u] = {"pwd": hash_pwd(p), "role": role,
                           "name": name, "rooms": rooms}
            self.store.set_kv(self.KEY, data)
            self.store.log("账户初始化", "预置 %d 个终端账户" % len(data), "system")
            return data
        # 迁移：老库里的旧账户名改名、更新口令并升级为所有者角色
        changed = []
        for old, (new, pwd, role) in self.RENAMES.items():
            if old in data and new not in data:
                rec = data.pop(old)
                rec["pwd"] = hash_pwd(pwd)
                rec["role"] = role
                rec["name"] = "系统所有者"
                data[new] = rec
                changed.append("%s -> %s（升为%s）"
                               % (old, new, config.ROLE_TEXT.get(role, role)))
        # 已有 Owner 但角色仍是老角色 -> 也升上来
        if "Owner" in data and data["Owner"].get("role") != config.ROLE_OWNER:
            data["Owner"]["role"] = config.ROLE_OWNER
            data["Owner"]["name"] = "系统所有者"
            changed.append("Owner 升为%s" % config.ROLE_TEXT[config.ROLE_OWNER])
        # 补齐缺失的预置账户（例如老库升级后缺 admin）。只补"不存在"的，
        # 绝不动已有账户的口令与角色——避免覆盖用户改过的口令。
        for u, p, role, name, rooms in DEFAULT_ACCOUNTS:
            if u not in data:
                data[u] = {"pwd": hash_pwd(p), "role": role,
                           "name": name, "rooms": rooms}
                changed.append("补齐预置账户 %s(%s)" % (u, config.ROLE_TEXT.get(role, role)))
        if changed:
            self.store.set_kv(self.KEY, data)
            self.store.log("账户迁移", "；".join(changed), "system")
        return data

    def list_accounts(self):
        return self.store.get_kv(self.KEY, {})

    # ---------------- 终端账户管理（仅"manage"动作，即所有者） ----------------
    @staticmethod
    def _check_manage(by_role):
        if "manage" not in config.ROLE_ACTIONS.get(by_role, set()):
            return "权限不足：%s 不能管理终端账户" % config.ROLE_TEXT.get(by_role, by_role)
        return ""

    def create(self, username, password, role, display_name="", rooms=None,
               by_role=config.ROLE_OWNER):
        err = self._check_manage(by_role)
        if err:
            return False, err
        username = (username or "").strip()
        if not username or not password:
            return False, "用户名与口令不能为空"
        if role not in config.ROLE_ACTIONS:
            return False, "角色不合法"
        if config.ROLE_LEVEL.get(role, 0) >= config.ROLE_LEVEL.get(by_role, 0):
            return False, "不能创建与自己同级或更高权限的账户"
        data = self.list_accounts()
        if username in data:
            return False, "用户名已存在"
        data[username] = {"pwd": hash_pwd(password), "role": role,
                          "name": display_name or username, "rooms": list(rooms or [])}
        self.store.set_kv(self.KEY, data)
        self.store.log("新建终端账户", "%s(%s)" % (username, config.ROLE_TEXT[role]),
                       by_role)
        return True, "已创建 %s" % username

    def reset_password(self, username, password, by_role=config.ROLE_OWNER):
        err = self._check_manage(by_role)
        if err:
            return False, err
        if len(password or "") < 4:
            return False, "口令至少 4 位"
        data = self.list_accounts()
        if username not in data:
            return False, "用户不存在"
        if config.ROLE_LEVEL.get(data[username].get("role"), 0) >= \
                config.ROLE_LEVEL.get(by_role, 0):
            return False, "不能重置同级或更高权限账户的口令"
        data[username]["pwd"] = hash_pwd(password)
        self.store.set_kv(self.KEY, data)
        self.store.log("重置终端口令", username, by_role)
        return True, "已重置 %s 的口令" % username

    def delete(self, username, by_role=config.ROLE_OWNER):
        err = self._check_manage(by_role)
        if err:
            return False, err
        if username == "Owner":
            return False, "不能删除所有者账户"
        data = self.list_accounts()
        if username not in data:
            return False, "用户不存在"
        if config.ROLE_LEVEL.get(data[username].get("role"), 0) >= \
                config.ROLE_LEVEL.get(by_role, 0):
            return False, "不能删除同级或更高权限账户"
        data.pop(username)
        self.store.set_kv(self.KEY, data)
        self.store.log("删除终端账户", username, by_role)
        return True, "已删除 %s" % username

    def authenticate(self, username, password):
        data = self.list_accounts()
        rec = data.get((username or "").strip())
        if not rec or not verify_pwd(password or "", rec["pwd"]):
            self.store.log("登录失败", "用户名=%s" % username, username or "-")
            return None
        s = Session(username, rec["role"], rec.get("name"), rec.get("rooms"))
        self.store.log("登录成功", s.describe(), username)
        return s


def visible_alerts(store, session, **kw):
    """按会话的可见范围取告警；监考终端只能看本考场"""
    rooms = session.room_filter()
    if rooms is None:
        return store.list_alerts(**kw)
    out = []
    for r in rooms:
        kw2 = dict(kw)
        kw2["room_id"] = r
        out.extend(store.list_alerts(**kw2))
    out.sort(key=lambda a: a.created_epoch, reverse=True)
    return out[:int(kw.get("limit", 500))]


def render(alert, session):
    """渲染一条告警为展示文本（含脱敏与强制确认提示）"""
    txt = alert.display(masked=session.masked())
    if alert.needs_force_confirm():
        txt += "  ⚠ 红色预警：必须确认后才可继续"
    elif alert.overdue():
        txt += "  ⏰ 已超处置时限"
    return txt


def export_payload(alerts, session):
    """导出（受 export 动作限制）——默认脱敏"""
    session.require("export")
    return [{"alert_id": a.alert_id, "code": a.code, "level": a.level,
             "room_id": a.room_id, "seat_no": a.seat_no,
             "candidate_id": ("*" * 6 if session.masked() else a.candidate_id),
             "created_at": a.created_at, "status": a.status,
             "review_conclusion": a.review_conclusion} for a in alerts]


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)
