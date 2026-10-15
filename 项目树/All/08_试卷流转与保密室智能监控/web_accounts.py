# -*- coding: utf-8 -*-
"""
试卷流转与保密室智能监控 · 账户与权限

桌面端与网页端**共用本模块**、读写同一个 SQLite kv 键 `web.accounts`，
所以同一账号两边口令必然一致（换一边改口令，另一边立刻认）。

口令：PBKDF2-HMAC-SHA256（120000 轮、16 字节随机盐），存 `pbkdf2$salt$dk`。
权限：config.ROLE_ACTIONS 白名单，默认拒绝、显式放行。
越级防护：只能管理**严格低于**自己等级的账户。
"""
import hashlib
import hmac
import json
import os

import config

KV_KEY = "web.accounts"


# ---------------- 口令哈希 ----------------
def hash_password(password, salt=None, rounds=None):
    if isinstance(password, bytes):
        password = password.decode("utf-8", "replace")
    salt = salt or os.urandom(16)
    rounds = int(rounds or config.PBKDF2_ROUNDS)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2$%s$%s" % (salt.hex(), dk.hex())


def verify_password(password, stored):
    if not stored:
        return False
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    for rounds in (config.PBKDF2_ROUNDS,):
        want = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        if hmac.compare_digest(want.hex(), dk_hex):
            return True
    return False


# ---------------- 权限 ----------------
def can(role, action):
    """角色能不能做这个动作。默认拒绝。"""
    return action in config.ROLE_ACTIONS.get(role, set())


def role_level(role):
    return config.ROLE_LEVEL.get(role, 1)


def role_text(role):
    return config.ROLE_TEXT.get(role, role)


class Session(object):
    """一次登录会话。桌面端存内存，网页端由令牌映射到本对象。"""

    def __init__(self, username, role):
        self.username = username
        self.role = role

    def can(self, action):
        return can(self.role, action)

    def need(self, action):
        """返回 None 表示有权限；否则返回"权限不足"的说明文案。"""
        if self.can(action):
            return None
        acts = config.ROLE_ACTIONS.get(self.role, set())
        holders = [config.ROLE_TEXT.get(r, r) for r in config.ROLE_LEVEL
                   if action in config.ROLE_ACTIONS.get(r, set())]
        return "权限不足：%s 仅 %s 可用（当前角色：%s）" % (
            _action_text(action), "/".join(holders) or "无角色", role_text(self.role))


_ACTION_TEXT = {
    "view": "查看", "device_manage": "机位管理", "live_view": "实时画面",
    "monitor_operate": "布防/撤防", "access_operate": "门禁操作",
    "safe_operate": "试卷柜操作", "flow_operate": "流转登记",
    "escort_operate": "押运操作", "alarm_handle": "告警处置",
    "report_export": "导出报告", "change_own_pwd": "修改口令",
    "device_delete": "删除机位", "account_manage": "账户管理",
    "audit_view": "查看审计链", "hide_cmd_toggle": "隐藏命令行窗口",
}


def _action_text(action):
    return _ACTION_TEXT.get(action, action)


# ---------------- 账户管理 ----------------
DEFAULT_STORE_KEYS = ("owner", "teacher1")


class AccountManager(object):
    def __init__(self, store):
        self.store = store

    # ---- 读写 ----
    def _load(self):
        raw = self.store.get_kv(KV_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _save(self, data):
        self.store.set_kv(KV_KEY, json.dumps(data, ensure_ascii=False))

    def ensure_defaults(self):
        """首次运行预置默认账户；已存在则不覆盖（保护已改过的口令）。"""
        data = self._load()
        changed = False
        for name, spec in config.DEFAULT_ACCOUNTS.items():
            if name not in data:
                data[name] = {"pwd": hash_password(spec["password"]),
                              "role": spec["role"],
                              "created": _now(), "disabled": False}
                changed = True
        if changed:
            self._save(data)
        return changed

    def accounts(self):
        self.ensure_defaults()
        return self._load()

    def names(self):
        return sorted(self.accounts().keys())

    def get(self, username):
        return self.accounts().get(username)

    # ---- 登录 ----
    def login(self, username, password):
        """返回 (Session|None, 说明)。"""
        acc = self.get(username)
        if acc is None:
            return None, "账户不存在"
        if acc.get("disabled"):
            return None, "账户已停用"
        if not verify_password(password, acc.get("pwd", "")):
            return None, "口令错误"
        sess = Session(username, acc.get("role", config.ROLE_VIEWER))
        self.store.log("登录", "%s（%s）" % (username, role_text(sess.role)), username)
        return sess, ""

    # ---- 改口令 ----
    def change_password(self, actor_session, target, new_password):
        acc_all = self.accounts()
        if target not in acc_all:
            return False, "账户不存在"
        if actor_session.username != target:
            # 改别人的口令需要 account_manage，且必须严格高于对方
            if not actor_session.can("account_manage"):
                return False, actor_session.need("account_manage")
            if role_level(acc_all[target].get("role")) >= role_level(actor_session.role):
                return False, "只能重置严格低于自己等级的账户口令"
        if len(str(new_password or "")) < config.PWD_MIN_LEN:
            return False, "口令至少 %d 位" % config.PWD_MIN_LEN
        acc_all[target] = dict(acc_all[target])
        acc_all[target]["pwd"] = hash_password(new_password)
        self._save(acc_all)
        self.store.log("修改口令", "目标账户 %s" % target, actor_session.username)
        return True, "口令已更新"

    # ---- 新建 ----
    def create(self, actor_session, username, password, role):
        if not actor_session.can("account_manage"):
            return False, actor_session.need("account_manage")
        username = (username or "").strip()
        if not username:
            return False, "用户名不能为空"
        if role not in config.ROLE_LEVEL:
            return False, "未知角色：%s" % role
        # 越级防护：只能新建**严格低于**自己等级的账户（owner 不能被新建）
        if role_level(role) >= role_level(actor_session.role):
            return False, "不能新建同级或更高等级的账户（%s 等级 %d，你的等级 %d）" % (
                role_text(role), role_level(role), role_level(actor_session.role))
        acc_all = self.accounts()
        if username in acc_all:
            return False, "账户已存在"
        if len(str(password or "")) < config.PWD_MIN_LEN:
            return False, "口令至少 %d 位" % config.PWD_MIN_LEN
        acc_all[username] = {"pwd": hash_password(password), "role": role,
                             "created": _now(), "disabled": False}
        self._save(acc_all)
        self.store.log("新建账户", "%s（%s）" % (username, role_text(role)),
                       actor_session.username)
        return True, "已新建账户 %s" % username

    # ---- 删除 ----
    def delete(self, actor_session, username):
        if not actor_session.can("account_manage"):
            return False, actor_session.need("account_manage")
        acc_all = self.accounts()
        if username not in acc_all:
            return False, "账户不存在"
        if username == actor_session.username:
            return False, "不能删除当前登录账户"
        target_role = acc_all[username].get("role")
        if target_role == config.ROLE_OWNER:
            return False, "不能删除所有者账户"
        if role_level(target_role) >= role_level(actor_session.role):
            return False, "只能删除严格低于自己等级的账户"
        alive = [n for n in acc_all if n != username]
        if not alive:
            return False, "至少保留一个可用账户"
        del acc_all[username]
        self._save(acc_all)
        self.store.log("删除账户", "目标账户 %s" % username, actor_session.username)
        return True, "已删除账户 %s" % username

    # ---- 停用/启用 ----
    def set_disabled(self, actor_session, username, disabled):
        if not actor_session.can("account_manage"):
            return False, actor_session.need("account_manage")
        acc_all = self.accounts()
        if username not in acc_all:
            return False, "账户不存在"
        if username == actor_session.username:
            return False, "不能停用当前登录账户"
        if acc_all[username].get("role") == config.ROLE_OWNER and disabled:
            return False, "不能停用所有者账户"
        if role_level(acc_all[username].get("role")) >= role_level(actor_session.role):
            return False, "只能操作严格低于自己等级的账户"
        acc_all[username] = dict(acc_all[username])
        acc_all[username]["disabled"] = bool(disabled)
        self._save(acc_all)
        self.store.log("停用账户" if disabled else "启用账户", username,
                       actor_session.username)
        return True, "已%s账户 %s" % ("停用" if disabled else "启用", username)

    # ---- 清理（所有者） ----
    def clear_all_but_owner(self, actor_session):
        if actor_session.role != config.ROLE_OWNER:
            return False, "仅所有者可清理账户"
        acc_all = self.accounts()
        own = acc_all.get(actor_session.username)
        if own is None:
            return False, "所有者账户缺失"
        self._save({actor_session.username: own})
        self.store.log("清理账户", "仅保留 %s" % actor_session.username,
                       actor_session.username)
        return True, "已仅保留所有者账户"


# ---------------- 隐藏命令行窗口标记 ----------------
def hide_cmd_enabled():
    return config.HIDE_CMD_FLAG.exists()


def set_hide_cmd(on):
    try:
        if on:
            config.HIDE_CMD_FLAG.parent.mkdir(parents=True, exist_ok=True)
            config.HIDE_CMD_FLAG.write_text("1", encoding="utf-8")
        else:
            if config.HIDE_CMD_FLAG.exists():
                config.HIDE_CMD_FLAG.unlink()
        return True
    except OSError:
        return False


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
