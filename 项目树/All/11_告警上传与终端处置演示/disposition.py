# -*- coding: utf-8 -*-
r"""
终端处置流程（03 侧）

处置不是"点一下就闭环"，它是一条有状态、有留痕的链：

    待处置 pending ──确认(ack)──> 已确认 acked ──复核(review)──> 已复核 reviewed

规矩（都在 03 的模块里，本文件只是把它们按顺序串起来，并核对结果）：
  · **红色预警必须先确认再复核**，橙色预警可直接复核
    —— 03/review.py: `if a.needs_force_confirm(): return False, "红色预警必须先确认…"`
  · 复核必须填理由，结论只能是 成立 / 不成立 / 误报
  · 结论为「误报」时自动进入算法迭代闭环（closed_loop=1）
  · 每一步都写哈希链审计
  · 权限：监考员不能复核（避免自己判自己）；可见考场之外处置不了

本模块不伪造这些规则，只调用 03 的 AlertHub / Reviewer，并把真实返回记录下来。
"""
from __future__ import annotations


class Step(object):
    """处置过程中的一步：做了什么、谁做的、03 怎么答的。"""

    __slots__ = ("name", "ok", "detail")

    def __init__(self, name, ok, detail):
        self.name = name
        self.ok = bool(ok)
        self.detail = detail

    def as_dict(self):
        return {"step": self.name, "ok": self.ok, "detail": self.detail}

    def __repr__(self):
        return "Step(%s, ok=%s, %s)" % (self.name, self.ok, self.detail)


class DispositionResult(object):
    """一条告警从上传到处置完毕的全过程记录。"""

    def __init__(self, alert_id, code, level_before="", level_after=""):
        self.alert_id = alert_id
        self.code = code
        self.level_before = level_before
        self.level_after = level_after
        self.steps = []
        self.timeline = []
        self.final_status = ""
        self.closed_loop = False

    def add(self, name, ok, detail):
        self.steps.append(Step(name, ok, detail))
        return self.steps[-1]

    @property
    def ok(self):
        return bool(self.steps) and all(s.ok for s in self.steps)

    def as_dict(self):
        return {"alert_id": self.alert_id, "code": self.code,
                "level_before": self.level_before, "level_after": self.level_after,
                "final_status": self.final_status, "closed_loop": self.closed_loop,
                "ok": self.ok, "steps": [s.as_dict() for s in self.steps],
                "timeline": self.timeline}


class DispositionDesk(object):
    """处置台：在 03 的数据目录里驱动确认 / 复核 / 上报。"""

    #: 允许的复核结论（与 03 的 config.REVIEW_CONCLUSIONS 对齐）
    CONCLUSIONS = ("confirmed", "rejected", "false_positive")

    def __init__(self, console):
        self.tc = console

    # ---------------- 单步 ----------------
    def acknowledge(self, alert_id, session, reason="终端确认收到并开始处置"):
        ok, msg = self.tc.ack(alert_id, session, reason=reason)
        return ok, msg

    def review(self, alert_id, conclusion, reason, session, witness=""):
        if conclusion not in self.CONCLUSIONS:
            return False, "复核结论不合法：%s（可选 %s）" % (
                conclusion, "/".join(self.CONCLUSIONS))
        if not (reason or "").strip():
            return False, "必须填写复核理由（留痕要求）"
        return self.tc.review(alert_id, conclusion, reason, session, witness=witness)

    # ---------------- 完整处置 ----------------
    def dispose(self, alert_id, session, conclusion="confirmed", reason="",
                witness="", auto_ack=True):
        """
        按状态机把一条告警处置到「已复核」。

        红色预警会**自动补一次确认**（这正是终端上的实际操作顺序），
        橙色预警不需要确认，直接复核。
        """
        alert = self.tc.get_alert(alert_id)
        if alert is None:
            res = DispositionResult(alert_id, "")
            res.add("读取告警", False, "告警不存在")
            return res

        res = DispositionResult(alert_id, alert.code,
                               level_before=alert.level, level_after=alert.level)

        # ① 红色预警：必须先确认
        if alert.needs_force_confirm():
            if not auto_ack:
                res.add("确认", False, "红色预警需先确认（本次未允许自动确认）")
                return res
            ok, msg = self.acknowledge(alert_id, session)
            res.add("确认（红色预警强制）", ok, msg)
            if not ok:
                return res
            alert = self.tc.get_alert(alert_id)

        # ② 复核
        ok, msg = self.review(alert_id, conclusion, reason, session, witness=witness)
        res.add("复核（结论=%s）" % self.tc.conclusion_text(conclusion), ok, msg)
        if not ok:
            return res

        # ③ 回收最终状态，作为"留痕真的落库了"的证据
        final = self.tc.get_alert(alert_id)
        res.final_status = final.status
        res.level_after = final.level
        res.closed_loop = bool(final.false_positive)
        res.timeline = self.tc.timeline(alert_id)
        return res

    # ---------------- 上报 ----------------
    def escalate(self, alert_id, session, note):
        ok, msg = self.tc.report(alert_id, note, session)
        return ok, msg
