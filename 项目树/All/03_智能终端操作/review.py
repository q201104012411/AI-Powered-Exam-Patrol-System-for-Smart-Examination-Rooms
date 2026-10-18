# -*- coding: utf-8 -*-
"""
复核操作留痕（《智能终端.md》第二节第 2 条）

要求：复核结论（成立/不成立/误报）、理由、附件一并留档；
     误报标记进入算法迭代闭环。
约束：只有具备 review 动作的角色可复核（监考员不能复核，避免自己判自己）；
     红色预警必须先确认（ack）才能复核。
"""
import os
import shutil

import config
import models

_EXT_OK = (".png", ".jpg", ".jpeg", ".webp", ".txt", ".pdf", ".mp4")


class ReviewError(Exception):
    pass


class Reviewer(object):
    def __init__(self, store, hub=None):
        self.store = store
        self.hub = hub

    # ---------- 附件 ----------
    def attach(self, src_path):
        """把附件拷进数据目录留档；返回 (ok, 结果/原因)"""
        if not os.path.exists(src_path):
            return False, "附件不存在：%s" % src_path
        ext = os.path.splitext(src_path)[1].lower()
        if ext not in _EXT_OK:
            return False, "不支持的附件类型：%s" % (ext or "无扩展名")
        name = "%s%s" % (models.new_id("ATT"), ext)
        dst = os.path.join(str(config.ATTACH_DIR), name)
        try:
            shutil.copyfile(src_path, dst)
        except OSError as e:
            return False, str(e)
        return True, name

    # ---------- 复核 ----------
    def review(self, alert_id, conclusion, reason, session, witness="", attachments=None):
        session.require("review")
        if conclusion not in config.REVIEW_CONCLUSIONS:
            return False, "复核结论不合法：%s" % conclusion
        if not (reason or "").strip():
            return False, "必须填写复核理由（留痕要求）"

        a = self.store.get_alert(alert_id)
        if a is None:
            return False, "告警不存在"
        # 可见范围校验：监考员不能复核别的考场
        rooms = session.room_filter()
        if rooms is not None and (a.room_id or "") not in rooms:
            return False, "无权处置该考场告警"
        if a.review_conclusion:
            return False, "该告警已复核（结论：%s）" % config.REVIEW_CONCLUSIONS[a.review_conclusion]
        if a.needs_force_confirm():
            return False, "红色预警必须先确认，再填写复核结论"

        # 附件留档
        saved = []
        for p in (attachments or []):
            ok, res = self.attach(p)
            if not ok:
                return False, "附件留档失败：%s" % res
            saved.append(res)

        rec = models.ReviewRecord(alert_id, conclusion, reason,
                                  operator=session.username, witness=witness,
                                  attachments=saved)
        self.store.save_review(rec)

        a.review_conclusion = conclusion
        a.review_reason = reason
        a.review_by = session.username
        a.review_at = rec.created_at
        a.attachments = saved
        a.false_positive = 1 if conclusion == config.REVIEW_FALSE_POSITIVE else 0
        a.status = models.ST_REVIEWED
        self.store.save_alert(a)

        self.store.log("告警复核",
                       "%s 结论=%s 理由=%s 见证=%s 附件=%d"
                       % (a.code, config.REVIEW_CONCLUSIONS[conclusion],
                          reason[:40], witness or "-", len(saved)),
                       session.username)
        if rec.closed_loop:
            self.store.log("误报闭环", "%s 进入算法迭代闭环" % a.code, session.username)
        return True, rec.review_id

    # ---------- 一键上报（监考终端） ----------
    def report(self, alert_id, note, session):
        session.require("report")
        a = self.store.get_alert(alert_id)
        if a is None:
            return False, "告警不存在"
        rooms = session.room_filter()
        if rooms is not None and (a.room_id or "") not in rooms:
            return False, "无权上报该考场告警"
        self.store.push_command("report", {"alert_id": alert_id, "note": note,
                                           "by": session.username, "room_id": a.room_id},
                                operator=session.username)
        self.store.log("一键上报", "%s：%s" % (alert_id, note[:60]), session.username)
        return True, "已上报"

    # ---------- 统计与导出 ----------
    def stats(self):
        return self.store.false_positive_stats()

    def timeline(self, alert_id):
        """某条告警的全过程留痕（接入->确认->复核）"""
        a = self.store.get_alert(alert_id)
        if a is None:
            return []
        events = [{"ts": a.created_at, "event": "告警接入",
                   "who": a.source, "detail": a.code}]
        if a.acked_at:
            events.append({"ts": a.acked_at, "event": "确认", "who": a.acked_by,
                           "detail": a.level_name()})
        if a.review_at:
            events.append({"ts": a.review_at, "event": "复核",
                           "who": a.review_by,
                           "detail": "%s：%s" % (config.REVIEW_CONCLUSIONS.get(a.review_conclusion),
                                                 a.review_reason)})
        return events

    def review_rows(self, limit=200):
        """复核记录（**字典行**，供界面直接按字段取用）。

        为什么这里要转成字典（而不是直接透传 storage.list_reviews()）：
          storage.list_reviews() 返回的是**解密后的 ReviewRecord 对象**，而界面
          （terminal_gui.refresh_reviews）是按 `r["created_at"]` 这种字典方式取字段的。
          以前这里直接透传对象，于是只要库里存在**任意一条**复核记录，
          登录时的 refresh_all() 就会抛
          `TypeError: 'ReviewRecord' object is not subscriptable`，
          主界面构建直接中断 —— 而空库永远触发不到，所以长期没被发现。
        """
        rows = []
        for rec in self.store.list_reviews(limit=limit):
            rows.append({
                "review_id": rec.review_id,
                "alert_id": rec.alert_id,
                "conclusion": rec.conclusion,
                "reason": rec.reason,
                "operator": rec.operator,
                "witness": rec.witness,
                "attachments": list(rec.attachments or []),
                "created_at": rec.created_at,
                "closed_loop": int(rec.closed_loop or 0),
            })
        return rows

    def edit_review(self, review_id, conclusion, reason, session, witness=None,
                    attachments=None, edit_reason=""):
        """
        修改已有复核记录：保留原记录 review_id，覆盖结论/理由/见证人/附件，
        同时同步回告警表。需要提供修改理由并留痕。
        """
        session.require("review")
        if conclusion not in config.REVIEW_CONCLUSIONS:
            return False, "复核结论不合法：%s" % conclusion
        if not (reason or "").strip():
            return False, "必须填写复核理由"
        if not (edit_reason or "").strip():
            return False, "必须填写修改理由"

        rec = self.store.get_review(review_id)
        if rec is None:
            return False, "复核记录不存在"

        a = self.store.get_alert(rec.alert_id)
        if a is None:
            return False, "关联告警不存在"
        rooms = session.room_filter()
        if rooms is not None and (a.room_id or "") not in rooms:
            return False, "无权处置该考场告警"

        # 附件留档
        saved = []
        for p in (attachments or []):
            ok, res = self.attach(p)
            if not ok:
                return False, "附件留档失败：%s" % res
            saved.append(res)

        rec.conclusion = conclusion
        rec.reason = reason
        rec.operator = session.username
        if witness is not None:
            rec.witness = witness
        rec.attachments = saved or rec.attachments
        self.store.save_review(rec)

        a.review_conclusion = conclusion
        a.review_reason = reason
        a.review_by = session.username
        a.attachments = rec.attachments
        a.false_positive = 1 if conclusion == config.REVIEW_FALSE_POSITIVE else 0
        self.store.save_alert(a)

        self.store.log("复核修改",
                       "%s 结论=%s 修改理由=%s 修改人=%s 附件=%d"
                       % (a.code, config.REVIEW_CONCLUSIONS[conclusion],
                          edit_reason[:40], session.username, len(rec.attachments)),
                       session.username)
        return True, rec.review_id
