# -*- coding: utf-8 -*-
r"""
账户、角色与权限最小化 + 持续校验（《考生数据保护.md》第三节第 4 条）

规则：
  · 默认拒绝 —— 角色不在 ROLE_ACTIONS 白名单里的动作一律拒绝（方法层强校验，
    不是"只把按钮变灰"）；
  · 可见范围 —— 监考员只看本考场（room_filter）；
  · 默认脱敏 —— 查看明文/原图必须走 reveal 动作，且留痕（由 protected.py 执行）；
  · 口令 —— PBKDF2-HMAC-SHA256 加盐，绝不落明文；
  · 持续校验 —— 结合登录地点、设备、时段做动态校验（见 continuous_check），
    高风险会话即使角色够也不放行，并要求二次确认。
"""
import hashlib
import hmac
import os

from dpp import config

_ITER = 120000


class AccessError(Exception):
    pass


# ---------------------------------------------------------------- 口令
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
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------- 会话
class Session(object):
    """
    一次登录会话。额外带**持续校验**所需的上下文：
      place（登录地点）/ device（设备指纹）/ hour（登录时段）
    高风险（异地 / 陌生设备 / 深夜）时 can() 依然按角色判定，但
    require() 会在高风险动作上要求二次确认（confirmed_risky=True）。
    """

    RISKY_ACTIONS = frozenset({"reveal", "share", "destroy", "force_destroy",
                               "approve", "manage"})

    def __init__(self, username, role, display_name="", rooms=None,
                 place="", device="", hour=None):
        self.username = username
        self.role = role
        self.display_name = display_name or username
        self.rooms = list(rooms or [])
        self.place = place
        self.device = device
        import time
        self.hour = time.localtime().tm_hour if hour is None else int(hour)
        self.confirmed_risky = False

    def can(self, action):
        return action in config.ROLE_ACTIONS.get(self.role, set())

    def require(self, action):
        if not self.can(action):
            raise AccessError("权限不足：%s 不能执行「%s」（当前角色：%s）"
                              % (config.ROLE_TEXT.get(self.role, self.role), action,
                                 config.ROLE_TEXT.get(self.role, self.role)))
        if action in self.RISKY_ACTIONS and self.risk_level() == "high" \
                and not self.confirmed_risky:
            raise AccessError("持续校验未通过：当前为高风险会话（%s），"
                              "执行「%s」需二次确认" % (self.risk_text(), action))
        return True

    def room_filter(self):
        """None 表示全部考场；否则为允许的考场列表"""
        if self.role in (config.ROLE_OWNER, config.ROLE_DPO, config.ROLE_AUDITOR,
                         config.ROLE_OPERATOR):
            return None
        return self.rooms or ["__none__"]

    def masked(self):
        return config.MASK_DEFAULT

    # ---- 持续校验 ----
    def risk_level(self):
        """结合时段/设备/地点给出风险级别（简单可解释规则，不搞黑盒）"""
        score = 0
        if self.hour < 6 or self.hour >= 23:
            score += 1                        # 深夜登录
        if not self.device:
            score += 1                        # 未登记设备
        if not self.place:
            score += 1                        # 未登记地点
        if score >= 2:
            return "high"
        if score == 1:
            return "medium"
        return "low"

    def risk_text(self):
        items = []
        if self.hour < 6 or self.hour >= 23:
            items.append("非工作时段 %02d 时" % self.hour)
        if not self.device:
            items.append("未登记设备")
        if not self.place:
            items.append("未登记地点")
        return "、".join(items) or "正常"

    def confirm_risky(self):
        """二次确认（真实系统里应是短信/密码/审批；这里只置状态并留痕）"""
        self.confirmed_risky = True
        return True

    def describe(self):
        return "%s（%s）可见范围：%s ｜ 地点：%s ｜ 设备：%s ｜ 风险：%s" % (
            self.display_name, config.ROLE_TEXT.get(self.role, self.role),
            "全部考场" if self.room_filter() is None else "、".join(self.room_filter()),
            self.place or "未登记", self.device or "未登记", self.risk_level())


# ---------------------------------------------------------------- 账户仓库
class AccountStore(object):
    """账户存同一 SQLite 的 kv 表（键 accounts），与 02/03 的做法保持一致。"""

    KEY = "accounts"

    def __init__(self, store):
        self.store = store

    def ensure_seed(self):
        data = self.store.get_kv(self.KEY)
        if data is None:
            data = {}
            for u, p, role, name, rooms in config.DEFAULT_ACCOUNTS:
                data[u] = {"pwd": hash_pwd(p), "role": role, "name": name,
                           "rooms": rooms, "device": "", "place": ""}
            self.store.set_kv(self.KEY, data)
            self.store.log("账户", "预置 %d 个账户" % len(data), "system")
            return data
        # 补齐缺失的预置账户（只补不存在的，绝不动已改过的口令）
        changed = []
        for u, p, role, name, rooms in config.DEFAULT_ACCOUNTS:
            if u not in data:
                data[u] = {"pwd": hash_pwd(p), "role": role, "name": name,
                           "rooms": rooms, "device": "", "place": ""}
                changed.append("补齐账户 %s" % u)
        if changed:
            self.store.set_kv(self.KEY, data)
            self.store.log("账户", "；".join(changed), "system")
        return data

    def list_accounts(self):
        return self.store.get_kv(self.KEY, {})

    @staticmethod
    def _check_manage(by_role):
        if "manage" not in config.ROLE_ACTIONS.get(by_role, set()):
            return "权限不足：%s 不能管理账户" % config.ROLE_TEXT.get(by_role, by_role)
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
                          "name": display_name or username, "rooms": list(rooms or []),
                          "device": "", "place": ""}
        self.store.set_kv(self.KEY, data)
        self.store.log("账户", "新建 %s（%s）" % (username, config.ROLE_TEXT[role]),
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
        self.store.log("账户", "重置口令 %s" % username, by_role)
        return True, "已重置 %s 的口令" % username

    def delete(self, username, by_role=config.ROLE_OWNER):
        err = self._check_manage(by_role)
        if err:
            return False, err
        if username == "owner":
            return False, "不能删除所有者账户"
        data = self.list_accounts()
        if username not in data:
            return False, "用户不存在"
        if config.ROLE_LEVEL.get(data[username].get("role"), 0) >= \
                config.ROLE_LEVEL.get(by_role, 0):
            return False, "不能删除同级或更高权限账户"
        if len(data) <= 1:
            return False, "至少保留一个可用账户"
        data.pop(username)
        self.store.set_kv(self.KEY, data)
        self.store.log("账户", "删除 %s" % username, by_role)
        return True, "已删除 %s" % username

    def authenticate(self, username, password, place="", device="", hour=None):
        data = self.list_accounts()
        u = (username or "").strip()
        rec = data.get(u)
        if not rec or not verify_pwd(password or "", rec.get("pwd", "")):
            self.store.log("登录", "失败：用户名=%s" % (u or "-"), u or "-")
            return None
        s = Session(u, rec["role"], rec.get("name"), rec.get("rooms"),
                    place=place or rec.get("place", ""),
                    device=device or rec.get("device", ""), hour=hour)
        self.store.log("登录", "成功：%s" % s.describe(), u)
        return s


# ---------------------------------------------------------------- 可见范围
def visible_candidates(store, session, **kw):
    """按会话可见范围取考生记录；监考员只能看本考场。"""
    rooms = session.room_filter()
    if rooms is None:
        return store.list_candidates(**kw)
    out = []
    for r in rooms:
        kw2 = dict(kw)
        kw2["room_id"] = r
        out.extend(store.list_candidates(**kw2))
    return out


def describe():
    return {"角色数": len(config.ROLE_ACTIONS),
            "角色": " ".join("%s=%s" % (config.ROLE_TEXT[k],
                                        "|".join(sorted(config.ROLE_ACTIONS[k])))
                             for k in config.ROLE_ACTIONS)}
