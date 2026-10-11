# -*- coding: utf-8 -*-
"""
闭环整改工单（《摄像头核查与视频质量诊断.md》第四节第 3 条）

发现 -> 派单 -> 整改 -> 复核，整改前后截图留档。
复核不通过会**打回重开**（回到 dispatched），形成真正的闭环而不是"点一下就关"。
"""
import os
import shutil

import config
import models

_EXT_OK = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".txt", ".pdf")
STATUS_TEXT = {"open": "待派单", "dispatched": "已派单", "rectified": "待复核",
               "closed": "已闭环"}


class WorkOrderError(Exception):
    pass


class WorkOrderManager(object):
    def __init__(self, store):
        self.store = store

    # ---------------- 发现 -> 建单 ----------------
    def ensure_from_check(self, check, device, operator="system"):
        """
        核查异常时自动建单（同机位同缺陷已有未关闭单则不重复建）。
        返回 (WorkOrder or None, 说明)
        """
        bad = [d for d in check.defects
               if d.severity in (config.SEV_FAIL, config.SEV_WARN)]
        if not bad:
            return None, "核查正常，无需建单"
        codes = sorted({d.code for d in bad})
        exist = self.store.open_order_for(device.device_id, codes)
        if exist:
            return exist, "已有未关闭工单 %s（%s）" % (exist.order_id,
                                                   STATUS_TEXT.get(exist.status, exist.status))
        severity = config.SEV_FAIL if any(d.severity == config.SEV_FAIL for d in bad) \
            else config.SEV_WARN
        note = "；".join("%s：%s" % (config.defect_name(d.code), d.detail) for d in bad)
        wo = models.WorkOrder(device, codes, severity, note=note, operator=operator)
        if check.frame_path:
            wo.before_path = check.frame_path
        self.store.save_order(wo)
        self.store.log("生成整改工单",
                       "%s %s 缺陷=%s" % (device.code, wo.order_id, ",".join(codes)),
                       operator)
        return wo, "已生成工单 %s" % wo.order_id

    def attach_before(self, order_id, image_path, operator=""):
        """把发现时的截图留档为整改前证据"""
        wo = self.store.get_order(order_id)
        if wo is None:
            return False, "工单不存在"
        if wo.before_path and os.path.exists(wo.before_path):
            return True, "已有整改前截图：%s" % os.path.basename(wo.before_path)
        ok, res = self._save_copy(image_path, "BEFORE")
        if not ok:
            return False, res
        wo.before_path = res
        self.store.save_order(wo)
        self.store.log("整改前截图留档", "%s %s" % (order_id, os.path.basename(res)),
                       operator)
        return True, res

    # ---------------- 派单 ----------------
    def dispatch(self, order_id, assigned_to, operator="", note=""):
        wo = self.store.get_order(order_id)
        if wo is None:
            return False, "工单不存在"
        if wo.status != "open":
            return False, "当前状态（%s）不能派单" % STATUS_TEXT.get(wo.status, wo.status)
        if not (assigned_to or "").strip():
            return False, "必须指定整改责任人"
        wo.status = "dispatched"
        wo.assigned_to = assigned_to.strip()
        wo.dispatched_at = models.now_ts()
        if note:
            wo.note = (wo.note + "；" + note) if wo.note else note
        self.store.save_order(wo)
        self.store.log("工单派单", "%s -> %s" % (order_id, wo.assigned_to), operator)
        return True, "已派单给 %s" % wo.assigned_to

    # ---------------- 整改 ----------------
    def rectify(self, order_id, after_image_path=None, note="", operator=""):
        wo = self.store.get_order(order_id)
        if wo is None:
            return False, "工单不存在"
        if wo.status not in ("dispatched", "open"):
            return False, "当前状态（%s）不能提交整改" % STATUS_TEXT.get(wo.status, wo.status)
        if after_image_path:
            ok, res = self._save_copy(after_image_path, "AFTER")
            if not ok:
                return False, res
            wo.after_path = res
        if not wo.after_path:
            return False, "必须提供整改后截图（整改前后留档是验收要求）"
        wo.status = "rectified"
        wo.rectified_at = models.now_ts()
        if note:
            wo.note = (wo.note + "；整改说明：" + note) if wo.note else ("整改说明：" + note)
        self.store.save_order(wo)
        self.store.log("工单整改", "%s 已提交整改" % order_id, operator)
        return True, "已提交整改，等待复核"

    # ---------------- 复核 ----------------
    def review(self, order_id, ok, note="", operator=""):
        wo = self.store.get_order(order_id)
        if wo is None:
            return False, "工单不存在"
        if wo.status != "rectified":
            return False, "只有「待复核」状态可以复核（当前 %s）" % STATUS_TEXT.get(
                wo.status, wo.status)
        wo.reviewer = operator
        wo.review_note = note
        if ok:
            wo.status = "closed"
            wo.reviewed_at = models.now_ts()
            self.store.save_order(wo)
            self.store.log("工单闭环", "%s 复核通过" % order_id, operator)
            return True, "复核通过，工单已闭环"
        # 复核不通过 -> 打回，继续整改（真正的闭环）
        wo.status = "dispatched"
        wo.reviewed_at = models.now_ts()
        self.store.save_order(wo)
        self.store.log("工单打回", "%s 复核未通过：%s" % (order_id, note[:60]), operator)
        return False, "复核未通过，已打回继续整改"

    # ---------------- 统计 ----------------
    def stats(self):
        s = self.store.workorder_summary()
        pending = [wo for wo in self.store.list_orders(limit=1000) if wo.status != "closed"]
        oldest = None
        for wo in pending:
            if oldest is None:
                oldest = wo
        s["pending"] = len(pending)
        s["oldest_pending"] = ({"order_id": oldest.order_id, "device": oldest.device_code,
                                "status": STATUS_TEXT.get(oldest.status, oldest.status),
                                "created_at": oldest.created_at} if oldest else None)
        return s

    def _save_copy(self, src, tag):
        if not src or not os.path.exists(str(src)):
            return False, "文件不存在：%s" % src
        ext = os.path.splitext(str(src))[1].lower()
        if ext not in _EXT_OK:
            return False, "不支持的附件类型：%s" % (ext or "无扩展名")
        name = "%s_%s%s" % (models.new_id("WOE"), tag, ext)
        dst = os.path.join(str(config.ATTACH_DIR), name)
        try:
            shutil.copyfile(str(src), dst)
        except OSError as e:
            return False, "附件留档失败：%s" % e
        return True, dst
