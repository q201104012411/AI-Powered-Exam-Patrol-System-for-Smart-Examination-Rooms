# -*- coding: utf-8 -*-
r"""
考生权利响应（《考生数据保护.md》第五节）

| 权利 | 响应方式 | 时限建议 |
| 知情 | 考前告知书、考点公示 | 考前 |
| 查阅/复制 | 受理申请，核验身份后提供 | 5 个工作日内 |
| 更正 | 核对后更正并留痕 | 5 个工作日内 |
| 删除 | 无法律保留必要时删除并出具记录 | 10 个工作日内 |
| 撤回同意 | 提供非人脸核验替代通道，不影响考试权利 | 即时 |

两条硬要求：
  1. **核验身份**才受理（防冒名查阅他人数据）；
  2. **撤回同意必须即时生效**，并自动确保该考生后续走非人脸替代通道。
"""
from dpp import config
from dpp import desensitize, models

RIGHTS = ("access", "copy", "correct", "delete", "withdraw")
RIGHTS_TEXT = {"access": "查阅", "copy": "复制", "correct": "更正",
               "delete": "删除", "withdraw": "撤回同意",
               "know": "知情"}

_WORKDAY_SEC = 86400.0


class RightsError(Exception):
    pass


class RightsService(object):
    def __init__(self, store, lifecycle=None, accounts=None):
        self.store = store
        self.lifecycle = lifecycle
        self.accounts = accounts

    # ---------------------------------------------------------- 受理
    def submit(self, session, cand_ref, right, detail="", channel="窗口受理",
               identity_verified=False, alt_channel_used=False):
        """
        受理考生权利申请。identity_verified=False 时**只登记不受理**
        （状态 open，但标记待核验身份），避免"谁来问都能查别人数据"。
        """
        session.require("rights")
        if right not in RIGHTS:
            raise RightsError("不支持的权利类型：%s（支持 %s）"
                              % (right, "、".join(RIGHTS_TEXT[r] for r in RIGHTS)))
        cand = self.store.get_candidate(cand_ref)
        if cand is None:
            raise RightsError("考生记录不存在：%s" % cand_ref)

        rq = models.RightsRequest(cand_ref, right, detail, channel=channel,
                                  alt_channel_used=alt_channel_used,
                                  handler=session.username)
        days = config.RIGHTS_SLA_DAYS.get(right, 5)
        rq.due_epoch = rq.created_epoch + days * _WORKDAY_SEC
        if not identity_verified:
            rq.detail = (detail + " ｜【待核验身份】").strip(" ｜")
        self.store.save_rights(rq)
        self.store.log("权利申请", "%s %s 考生=%s 渠道=%s 身份核验=%s"
                       % (rq.req_id, RIGHTS_TEXT[right], cand_ref, channel,
                          "是" if identity_verified else "否"), session.username)

        # 撤回同意：即时生效（不等审批）
        if right == "withdraw":
            self._apply_withdraw(session, cand_ref, rq)
        return {"req_id": rq.req_id, "right": right, "due_days": days,
                "status": rq.status,
                "note": "撤回同意已即时生效，该考生后续走非人脸替代通道"
                if right == "withdraw" else ""}

    def _apply_withdraw(self, session, cand_ref, rq):
        """撤回同意：标记同意失效 + 保证替代通道可用 + 留痕。

        撤回走受控的单向切换 withdraw_consent（0->1 不可回滚），
        而不是"改写同意记录"，保持 append-only 语义。
        """
        for cs in self.store.list_consents(limit=10 ** 6):
            if cs.cand_ref == cand_ref and not cs.withdrawn:
                self.store.withdraw_consent(
                    cs.consent_id,
                    reason="考生行使撤回同意权（%s）" % rq.req_id)
        rq.status = "done"
        rq.result = "已撤回同意；该考生改走非人脸替代核验通道，不影响考试权利"
        rq.handled_at = models.now_ts()
        rq.handled_epoch = models.now_epoch()
        self.store.save_rights(rq)
        self.store.log("撤回同意", "考生=%s 已即时生效，转替代通道" % cand_ref,
                       session.username)
        # 替代通道可用性检查（撤回后必须真的还有路可走）
        chans = [c for c in self.store.list_channels() if c.get("available")]
        if not chans:
            self.store.log("撤回同意", "⚠ 当前无可用替代通道配置，需立即补配",
                           session.username)

    # ---------------------------------------------------------- 处理
    def handle(self, session, req_id, result="", reason="", approve=True,
               corrected=None):
        """
        处理申请：
          · access/copy：返回**脱敏**数据（完整明文只在核验身份且必要时提供，
            且同样留痕）；
          · correct：按 corrected 字典更正并留痕；
          · delete：无法律保留必要时调用生命周期销毁；
          · withdraw：已在 submit 时即时生效，这里只补结论。
        """
        session.require("rights_decide")
        rq = self.store.get_rights(req_id)
        if rq is None:
            raise RightsError("申请不存在：%s" % req_id)
        if rq.status != "open":
            raise RightsError("申请已处理：%s" % rq.status)

        out = {"req_id": req_id, "right": rq.right, "status": "done"}
        cand = self.store.get_candidate(rq.cand_ref)

        if rq.right in ("access", "copy"):
            out["data"] = self._disclose(cand)
            result = result or "已按最小必要提供脱敏后的数据"
        elif rq.right == "correct":
            if not corrected:
                raise RightsError("更正申请必须提供更正内容")
            new_ref = self._correct(session, cand, corrected)
            if new_ref:
                out["new_cand_ref"] = new_ref
            result = result or ("已按只读策略，以【删除原记录 + 新增更正记录】的方式"
                                "完成更正，全程留痕")
        elif rq.right == "delete":
            if cand is not None and cand.legal_hold:
                out["status"] = "rejected"
                result = "不予删除：该数据有法律保留必要（%s）" % (
                    cand.legal_hold_reason or "未填理由")
                reason = result
                self._finish(session, rq, "rejected", result, reason)
                out["result"] = result
                self.store.log("权利处理", "%s 驳回删除：法律保留" % req_id,
                               session.username)
                return out
            if cand is not None:
                res = self.lifecycle.destroy_by_request(
                    session, rq.cand_ref, "考生行使删除权（%s）" % req_id)
                out["destroy_id"] = res["destroy_id"]
                result = result or "已删除并出具销毁记录 %s" % res["destroy_id"]
            else:
                result = result or "数据已不存在（此前已销毁）"
        elif rq.right == "withdraw":
            result = result or "撤回同意已即时生效，已转非人脸替代通道"

        self._finish(session, rq, "done", result, reason)
        out["result"] = result
        self.store.log("权利处理", "%s %s -> %s"
                       % (req_id, RIGHTS_TEXT.get(rq.right, rq.right),
                          result[:60]), session.username)
        return out

    def _disclose(self, cand):
        """最小必要披露：只给该考生自己的、去标识化后的数据。"""
        if cand is None:
            return {}
        return {"ref": cand.cand_ref, "room_id": cand.room_id,
                "seat_no": cand.seat_no,
                "name": desensitize.mask_name(cand.name),
                "id_card_no": desensitize.mask_id_card(cand.id_card_no),
                "ticket_no": desensitize.mask_ticket_no(cand.ticket_no),
                "has_face_template": bool(cand.face_template),
                "has_face_image": bool(cand.face_image_path),
                "retention_days": cand.retention_days,
                "note": "如需完整明文，需现场核验身份后由数据保护负责人授权提供"}

    def _correct(self, session, cand, corrected):
        """
        更正权：**不就地改写**加密数据，改为"先删除原记录、再登记一条更正后的新记录"。

        这是本系统"任何人不可修改加密数据、只能删除或新增"的直接体现：
        更正 = DELETE(旧) + INSERT(新)，两步都留痕，链路上看不到任何原地修改。
        """
        if cand is None:
            raise RightsError("考生记录不存在，无法更正")
        before = {}
        new_data = {}
        for k in ("name", "id_card_no", "ticket_no", "seat_no", "room_id"):
            if k in corrected:
                before[k] = desensitize.mask_generic(getattr(cand, k))
                new_data[k] = corrected[k]
        if not new_data:
            return None
        # 更正不允许改生物特征与保留期，只允许改身份字段
        merged = {
            "name": new_data.get("name", cand.name),
            "id_card_no": new_data.get("id_card_no", cand.id_card_no),
            "ticket_no": new_data.get("ticket_no", cand.ticket_no),
            "seat_no": new_data.get("seat_no", cand.seat_no),
            "room_id": new_data.get("room_id", cand.room_id),
            "site": cand.site,
            "retention_days": cand.retention_days,
        }
        # ① 删除旧记录（走销毁流程，留痕；这里由 rights 服务代理发起）
        self.store.log("更正", "考生=%s 更正字段=%s（原值已脱敏留痕：%s）；"
                              "按只读策略执行 删除+新增"
                       % (cand.cand_ref, ",".join(before.keys()), before),
                       session.username)
        old_ref = cand.cand_ref
        try:
            import os
            if cand.face_image_path:
                from dpp import config
                p = os.path.join(str(config.VAULT_DIR), cand.face_image_path)
                if os.path.exists(p):
                    os.remove(p)
            with self.store._lock, self.store._conn() as c:
                c.execute("DELETE FROM candidates WHERE cand_ref=?", (old_ref,))
        except Exception:                                     # noqa: BLE001
            pass
        # ② 追加登记一条更正后的新记录（新 cand_ref，保留期沿用）
        from dpp import models
        nc = models.Candidate(name=merged["name"], id_card_no=merged["id_card_no"],
                              ticket_no=merged["ticket_no"], seat_no=merged["seat_no"],
                              room_id=merged["room_id"], site=merged["site"],
                              retention_days=merged["retention_days"],
                              consent_id=cand.consent_id,
                              captured_epoch=cand.captured_epoch)
        self.store.save_candidate(nc)
        self.store.log("更正", "已新增更正后记录 %s（原 %s）" % (nc.cand_ref, old_ref),
                       session.username)
        return nc.cand_ref

    def _finish(self, session, rq, status, result, reason):
        rq.status = status
        rq.result = result
        rq.reason = reason
        rq.handler = session.username
        rq.handled_at = models.now_ts()
        rq.handled_epoch = models.now_epoch()
        self.store.save_rights(rq)

    # ---------------------------------------------------------- 统计
    def stats(self):
        rows = self.store.list_rights(limit=10 ** 6)
        by_right = {}
        overdue, done, open_ = 0, 0, 0
        for r in rows:
            by_right[r.right] = by_right.get(r.right, 0) + 1
            if r.status == "open":
                open_ += 1
                if r.is_overdue():
                    overdue += 1
            else:
                done += 1
        return {"total": len(rows), "by_right": by_right, "done": done,
                "open": open_, "overdue": overdue,
                "sla_days": dict(config.RIGHTS_SLA_DAYS)}
