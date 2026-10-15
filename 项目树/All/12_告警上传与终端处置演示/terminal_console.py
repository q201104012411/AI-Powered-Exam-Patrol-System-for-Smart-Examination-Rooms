# -*- coding: utf-8 -*-
r"""
03 终端处置端控制台（供演示脚本在**本进程内**驱动 03 的处置流程）

为什么处置要就地调用 03 的模块，而不是走它的 integration_api.py：
  · 03 的集成接口只对外开放 review_alert，没有对外的 ack 动作；而红色预警的
    规矩是**必须先确认再复核**（见 03/review.py 的 needs_force_confirm 分支）。
    只走集成接口就演示不出"强制确认"这一环。
  · 处置动作需要角色权限（access.Session）与可见考场范围，这些都在 03 进程里。

隔离做法：把 03 目录插到 sys.path 最前面，并先把 EXAM_TERMINAL_HOME 指到沙箱，
再 import 03 的 config —— 03 的 config 在 import 时读环境变量，顺序不能反。
"""
from __future__ import annotations

import os
import sys


class TerminalConsole(object):
    """03 处置端：确认 / 复核 / 时间线 / 审计链校验。"""

    ROOT_KEY = "03_智能终端操作"

    def __init__(self, terminal_dir, data_home, verbose=False):
        self.dir = os.path.abspath(terminal_dir)
        self.data_home = os.path.abspath(data_home)
        self.verbose = verbose

        if not os.path.isfile(os.path.join(self.dir, "config.py")):
            raise RuntimeError("03 目录不对（找不到 config.py）：%s" % self.dir)

        os.makedirs(self.data_home, exist_ok=True)
        # ③ 必须在 import 03 的 config 之前设好，否则它会落到真实 data\ 目录
        os.environ["EXAM_TERMINAL_HOME"] = self.data_home
        if self.dir in sys.path:
            sys.path.remove(self.dir)
        sys.path.insert(0, self.dir)

        import access          # noqa: E402
        import alerts as alerts_mod   # noqa: E402
        import config          # noqa: E402
        import offline as offline_mod  # noqa: E402
        import review as review_mod   # noqa: E402
        import storage         # noqa: E402

        self._access = access
        self._config = config
        self._models = sys.modules["models"]

        self.store = storage.Store()
        self.accounts = access.AccountStore(self.store)
        self.accounts.ensure_seed()
        self.outbox = offline_mod.Outbox(self.store)
        self.hub = alerts_mod.AlertHub(self.store, offline=self.outbox)
        self.reviewer = review_mod.Reviewer(self.store, hub=self.hub)

    # ---------------- 数据目录核对 ----------------
    @property
    def config(self):
        return self._config

    def data_dir(self):
        return str(self._config.DATA_ROOT)

    def assert_sandbox(self):
        """防止误打到真实运行数据上。"""
        real = os.path.join(self.dir, "data")
        if os.path.normcase(self.data_dir()) == os.path.normcase(real):
            raise RuntimeError(
                "拒绝对真实数据目录操作：%s\n"
                "（演示必须用沙箱数据目录，见 --home 参数）" % real)
        return True

    # ---------------- 会话 ----------------
    def login(self, username):
        """按 03 的账户表建一个真实会话（角色与可见考场都从账户表来）。"""
        data = self.accounts.list_accounts()
        rec = data.get(username)
        if rec is None:
            return None, "账户不存在：%s（可选：%s）" % (username, "、".join(sorted(data)))
        return self._access.Session(username, rec["role"], rec.get("name") or username,
                                    rec.get("rooms") or []), "登录成功"

    # ---------------- 确认 ----------------
    def ack(self, alert_id, session, reason=""):
        return self.hub.ack(alert_id, session.username, reason)

    # ---------------- 复核 ----------------
    def review(self, alert_id, conclusion, reason, session, witness="", attachments=None):
        try:
            ok, res = self.reviewer.review(alert_id, conclusion, reason, session,
                                           witness=witness, attachments=attachments)
        except self._access.AccessError as exc:
            return False, str(exc)
        return ok, ("复核已留痕：%s" % res) if ok else res

    # ---------------- 一键上报 ----------------
    def report(self, alert_id, note, session):
        try:
            return self.reviewer.report(alert_id, note, session)
        except self._access.AccessError as exc:
            return False, str(exc)

    # ---------------- 读取 ----------------
    def get_alert(self, alert_id):
        return self.store.get_alert(alert_id)

    def alerts(self, limit=50):
        return self.store.list_alerts(limit=limit)

    def timeline(self, alert_id):
        return self.reviewer.timeline(alert_id)

    def audit_rows(self, limit=100):
        return self.store.audit_rows(limit=limit)

    def verify_chain(self):
        return self.store.verify_chain()

    def summary(self):
        return self.store.summary()

    def level_name(self, level):
        return self._config.LEVEL_META.get(level, {}).get("name", level)

    def conclusion_text(self, conclusion):
        return self._config.REVIEW_CONCLUSIONS.get(conclusion, conclusion)
