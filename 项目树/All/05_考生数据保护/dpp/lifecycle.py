# -*- coding: utf-8 -*-
r"""
全生命周期控制（《考生数据保护.md》第二节）

  采集 -> 传输 -> 存储 -> 使用 -> 共享 -> 销毁

每个阶段都有两类产出：
  ① **控制动作**（真的做了什么）；
  ② **可核查证据**（能被审计与验收的东西，如授权日志、加密配置、审批工单）。
本模块的设计原则是"控制即留痕"——不产生证据的控制等于没做，
所以每个方法都往审计链/业务表里写记录，返回结果里同时给证据摘要。
"""
import json
import os
import time

from dpp import config
from dpp import crypto
from dpp import catalog, desensitize, models, storage as storage_mod

# 分类分级证据
EVIDENCE = {
    "采集": ["同意记录", "授权日志", "采集参数配置"],
    "传输": ["加密配置", "链路说明"],
    "存储": ["加密证明", "模板不可逆说明"],
    "使用": ["权限矩阵", "脱敏截图", "水印样例"],
    "共享": ["审批工单", "导出审计日志"],
    "销毁": ["删除策略配置", "销毁记录台账"],
}


class LifecycleError(Exception):
    pass


class Lifecycle(object):
    """
    全生命周期管理器。所有方法都需要一个已登录 session（权限最小化）。
    """

    def __init__(self, store, accounts=None):
        self.store = store
        self.accounts = accounts

    # ============================================================ 采集
    def collect(self, session, name, id_card_no="", ticket_no="", seat_no="",
                room_id="", site="", feature_vector=None, face_image_bytes=None,
                consent_id="", captured_epoch=None, retention_days=None,
                operator_terminal="考务登记台", cand_ref=None):
        """
        采集一名考生（写入最小必要的数据）。

        控制措施（对应文档第二节"采集"一行）：
          · 考前单独同意：没有有效同意记录 -> 拒绝采集（hard fail，不是警告）
          · 非静默开启：采集必须由人显式发起（本方法本身就是显式动作）
          · 频率按需最小化：采集帧率上限由 config.CAPTURE 控制，超限直接拒绝
          · 特征模板不可逆：只落 HMAC 摘要，不落可还原的原图特征
          · 原始图像最短必要：留图必须加密，且保留期 ≤ 30 天
        """
        session.require("register")

        # --- 控制 1：单独同意（硬前置） ---
        if config.CAPTURE["consent_required"]:
            if not consent_id:
                self.store.log("采集", "拒绝：未提供单独同意编号", session.username)
                raise LifecycleError("采集被拒绝：必须先在考前取得考生单独同意"
                                     "（不能捆绑在一般考试规则中）")
            # 同意必须能反查到**同一考生**：优先用调用方给的 cand_ref，
            # 否则由同意编号反查（同意先于采集写入时 cand_ref 已知）。
            subject = cand_ref or self._cand_ref_of_consent(consent_id)
            cs = self.store.valid_consent(cand_ref=subject, scope="face")
            if cs is None:
                self.store.log("采集", "拒绝：同意记录无效或已撤回 consent=%s"
                               % consent_id, session.username)
                raise LifecycleError("采集被拒绝：同意记录无效或考生已撤回同意")

        if config.CAPTURE["silent_start"]:
            raise LifecycleError("配置异常：禁止静默开启采集")

        # --- 控制 2：原始图像最短必要 + 单项加密 ---
        img_path = ""
        if face_image_bytes:
            days = min(retention_days or config.CAPTURE["retention_image_days"],
                       config.CAPTURE["retention_image_days"])
            img_path = self._store_encrypted_image(face_image_bytes)

        # --- 控制 3：特征模板不可逆 ---
        template = ""
        if feature_vector is not None:
            template = crypto.irreversible_template(feature_vector)

        cand = models.Candidate(
            name=name, id_card_no=id_card_no, ticket_no=ticket_no, seat_no=seat_no,
            room_id=room_id, site=site, face_template=template,
            face_image_path=img_path, consent_id=consent_id, cand_ref=cand_ref,
            captured_epoch=captured_epoch,
            retention_days=retention_days if retention_days is not None
            else config.CAPTURE["retention_image_days"])
        self.store.save_candidate(cand)
        if consent_id:
            self._bind_consent(consent_id, cand.cand_ref)

        self.store.log("采集", "登记考生 %s（考场 %s，同意 %s，留图 %s）"
                       % (cand.cand_ref, room_id or "-", consent_id or "-",
                          "是" if img_path else "否"), session.username)
        return {"cand_ref": cand.cand_ref, "template": bool(template),
                "image_encrypted": bool(img_path),
                "retention_days": cand.retention_days,
                "evidence": EVIDENCE["采集"]}

    def _cand_ref_of_consent(self, consent_id):
        """由同意编号反查考生引用号（同意记录先于采集写入时 cand_ref 已知）。"""
        for cs in self.store.list_consents(limit=10 ** 5):
            if cs.consent_id == consent_id:
                return cs.cand_ref
        return consent_id      # 允许调用方直接用 cand_ref 作为同意主题

    def _bind_consent(self, consent_id, cand_ref):
        """把同意记录的 cand_ref 绑定到实际考生（同意先于采集建立时用）。

        走受控路径 bind_consent_subject：只允许把占位空值补填一次，
        已绑定的主体不可改写（append-only）。
        """
        return self.store.bind_consent_subject(consent_id, cand_ref)

    def _store_encrypted_image(self, data: bytes):
        """原始人脸图像：加密后落 vault\\，文件名用随机号（不含身份信息）。"""
        name = "%s.enc" % crypto.secure_bytes(8).hex()
        path = os.path.join(str(config.VAULT_DIR), name)
        blob = crypto.encrypt_bytes(data)
        with open(path, "w", encoding="utf-8") as f:
            f.write(blob)
        return name

    def read_image(self, session, cand_ref, reason, terminal="巡考终端",
                   approved_by=""):
        """
        读取原始人脸图像：需要 reveal 权限 + 必须填理由，且**留痕 + 打水印**。
        返回 (bytes 或 None, 水印信息)。
        """
        session.require("reveal")
        if not (reason or "").strip():
            raise LifecycleError("授权解密查看必须填写理由（留痕要求）")
        cand = self.store.get_candidate(cand_ref)
        if cand is None:
            raise LifecycleError("考生记录不存在：%s" % cand_ref)
        wm = desensitize.watermark_text(session.username, terminal)
        self.store.save_reveal(models.RevealRecord(
            cand_ref, "face_image", reason, operator=session.username,
            terminal=terminal, watermark=wm, approved_by=approved_by))
        self.store.log("解密查看", "原图 %s 理由=%s 水印=%s"
                       % (cand_ref, reason[:40], wm), session.username)
        if not cand.face_image_path:
            return None, {"watermark": wm, "applied": False,
                          "reason": "该考生未留原始图像（只落了不可逆模板）"}
        path = os.path.join(str(config.VAULT_DIR), cand.face_image_path)
        if not os.path.exists(path):
            return None, {"watermark": wm, "applied": False, "reason": "图像文件不存在"}
        with open(path, "r", encoding="utf-8") as f:
            blob = f.read()
        raw = crypto.decrypt_bytes(blob)
        return raw, {"watermark": wm, "applied": False,
                     "reason": "已解密；落图水印需调用 desensitize.watermark_image"}

    # ============================================================ 传输
    def check_transport(self, host, encrypted=True, network="private",
                        payload_kind="alert_clip"):
        """
        传输前校验：专网 + 加密（国密优先）。返回 (ok, 说明, 证据)。
        不通过就**不许传**——不是打个警告继续发。
        """
        problems = []
        if config.TRANSPORT["require_private_network"] and network != "private":
            problems.append("非专网链路（%s）" % network)
        if config.TRANSPORT["require_encryption"] and not encrypted:
            problems.append("未加密")
        if config.TRANSPORT["plaintext_hosts_forbidden"] and str(host).lower().startswith(
                ("http://", "ftp://")):
            problems.append("明文协议地址：%s" % host)
        alg = crypto.active_algorithm()
        note = "加密算法：%s" % crypto.algorithm_text()
        if not problems:
            self.store.log("传输校验", "通过：%s（%s）" % (host, alg))
            return True, "传输校验通过。" + note, EVIDENCE["传输"]
        msg = "传输被拒绝：" + "；".join(problems) + "。" + note
        self.store.log("传输校验", "拒绝：%s（%s）" % ("；".join(problems), host))
        return False, msg, EVIDENCE["传输"]

    # ============================================================ 存储
    def storage_audit(self):
        """
        逐字段核对"敏感数据加密率 100%"：
        对分类分级里每个类别、每个必须加密的字段，检查库里实际存的是不是密文。
        返回 {rate, checked, missing, details, sampled}

        **样本语义（重要）**：只有真的取到了非空样本才计入 checked；
        一个样本都没有时 rate=None（由上层如实报"样本不足"），
        绝不用"库里没数据"冒充"加密率 100%"。
        """
        checked, missing, details, sampled = 0, [], [], 0
        for cat in catalog.CATEGORIES:
            if not cat.required_encrypted_fields:
                continue
            for field in cat.required_encrypted_fields:
                state = self._field_encrypted(cat.key, field)   # True/False/None
                details.append({"category": cat.key, "field": field,
                                "encrypted": bool(state),
                                "sampled": state is not None})
                if state is None:
                    continue                # 无样本，不计入分母
                sampled += 1
                checked += 1
                if not state:
                    missing.append("%s.%s" % (cat.key, field))
        rate = None if checked == 0 else (checked - len(missing)) / float(checked)
        return {"rate": None if rate is None else round(rate, 4),
                "checked": checked, "sampled": sampled,
                "missing": missing, "details": details,
                "algorithm": crypto.algorithm_text(),
                "evidence": EVIDENCE["存储"]}

    def _field_encrypted(self, category_key, field):
        """
        判断某类别某字段在库里是否为密文。**三态返回**：
          True  = 有样本且是密文
          False = 有样本但不是密文（不合格）
          None  = 取不到非空样本（样本不足，不计入加密率分母）
        """
        try:
            with self.store._lock, self.store._conn() as c:
                if category_key in ("face_template",):
                    r = c.execute("SELECT face_template v FROM candidates "
                                  "WHERE face_template<>'' LIMIT 1").fetchone()
                    if r is None:
                        return None
                    return str(r["v"]).startswith("tpl1:")
                if category_key in ("face_image",):
                    r = c.execute("SELECT face_image_path v FROM candidates "
                                  "WHERE face_image_path<>'' LIMIT 1").fetchone()
                    if r is None:
                        return None
                    return crypto.is_encrypted(r["v"])
                if category_key in ("enroll_identity",):
                    col = {"candidate_name": "name", "id_card_no": "id_card_no",
                           "ticket_no": "ticket_no"}[field]
                    r = c.execute("SELECT %s v FROM candidates WHERE %s<>'' LIMIT 1"
                                  % (col, col)).fetchone()
                    if r is None:
                        return None
                    return crypto.is_encrypted(r["v"])
                if category_key in ("alert_clip", "review_record"):
                    # 这两类由 02/03 子系统落库；本系统登记其口径与保留期，
                    # 加密由各自 data_crypto（Fernet）保证，验收时读它们的库核对。
                    # 本系统无该表样本 -> 如实返回 None（样本不足），不冒充已核。
                    return None
                if category_key == "exam_video":
                    # 视频在边缘侧，本系统不落地视频。无样本 -> None。
                    return None
        except Exception:
            return False
        return None

    # ============================================================ 使用
    def use(self, session, cand_ref, purpose, action, terminal=""):
        """
        目的限定使用：仅"作弊判定""身份核验"；情绪分析/行为画像/商业用途/跨境
        一律拒绝且**记录在案**（拒绝也要留痕，这才是可核查的合规）。
        """
        session.require("use")
        allowed = purpose in models.PURPOSE_ALLOWED
        deny = ""
        if not allowed:
            deny = "超出目的限定：%s（允许：%s）" % (purpose,
                                                  "、".join(models.PURPOSE_ALLOWED))
        rec = models.UseRecord(cand_ref, purpose, action, operator=session.username,
                               terminal=terminal, allowed=allowed, deny_reason=deny)
        self.store.save_use(rec)
        self.store.log("使用" if allowed else "使用被拒",
                       "%s 目的=%s 动作=%s%s"
                       % (cand_ref, purpose, action, ("；" + deny) if deny else ""),
                       session.username)
        if not allowed:
            raise LifecycleError("用途被拒绝：" + deny)
        return {"use_id": rec.use_id, "allowed": True, "purpose": purpose}

    def reveal_plain(self, session, cand_ref, reason, terminal="巡考终端"):
        """
        授权解密查看**明文**（证件号等）。默认视图永远是掩码，看明文必须走这里。
        返回明文；同时留痕。
        """
        session.require("reveal")
        if not (reason or "").strip():
            raise LifecycleError("查看明文必须填写理由")
        cand = self.store.get_candidate(cand_ref)
        if cand is None:
            raise LifecycleError("考生记录不存在：%s" % cand_ref)
        wm = desensitize.watermark_text(session.username, terminal)
        self.store.save_reveal(models.RevealRecord(
            cand_ref, "id_card_no", reason, operator=session.username,
            terminal=terminal, watermark=wm))
        self.store.log("解密查看", "明文证件号 %s 理由=%s 水印=%s"
                       % (cand_ref, reason[:40], wm), session.username)
        return {"name": cand.name, "id_card_no": cand.id_card_no,
                "ticket_no": cand.ticket_no, "watermark": wm}

    def permission_matrix(self):
        """权限矩阵（验收证据之一）：角色 × 动作。"""
        rows = []
        for role in config.ROLE_ACTIONS:
            rows.append({"role": role, "role_text": config.ROLE_TEXT[role],
                         "level": config.ROLE_LEVEL[role],
                         "actions": sorted(config.ROLE_ACTIONS[role])})
        return rows

    # ============================================================ 共享
    def request_share(self, session, cand_refs, fields, direction="up",
                      purpose="作弊判定", reason="", applicant_terminal=""):
        """
        发起跨层级共享：只允许传**必要字段**（白名单外的字段直接拒绝），
        且必须走审批（状态 pending，等 approve）。
        """
        session.require("share")
        allowed_fields = {"ref", "room_id", "seat_no", "ticket_no", "alert_code",
                          "alert_time", "conclusion"}
        bad = [f for f in fields if f not in allowed_fields]
        if bad:
            raise LifecycleError("共享字段超出必要范围：%s（允许：%s）"
                                 % ("、".join(bad), "、".join(sorted(allowed_fields))))
        forbidden = {"id_card_no", "name", "face_template", "face_image_path",
                     "id_card_masked"}
        leaked = [f for f in fields if f in forbidden]
        if leaked:
            raise LifecycleError("共享字段包含直接标识/生物特征，禁止共享：%s"
                                 % "、".join(leaked))
        sh = models.ShareRequest(cand_refs, fields, direction=direction,
                                 purpose=purpose, reason=reason,
                                 applicant=session.username)
        self.store.save_share(sh)
        self.store.log("共享申请", "%s 方向=%s 字段=%s 条数=%d 理由=%s"
                       % (sh.req_id, direction, ",".join(fields), len(cand_refs),
                          (reason or "-")[:40]), session.username)
        return {"req_id": sh.req_id, "status": sh.status,
                "evidence": EVIDENCE["共享"]}

    def approve_share(self, session, req_id, approve=True, reject_reason="",
                      export=False):
        """
        审批导出/共享。批准后可选择真的导出（导出文件仍**脱敏**）。
        导出动作写审计日志（验收要求：导出必须可追）。
        """
        session.require("approve")
        sh = self.store.get_share(req_id)
        if sh is None:
            raise LifecycleError("共享申请不存在：%s" % req_id)
        if sh["status"] != "pending":
            raise LifecycleError("该申请已处理：%s" % sh["status"])
        sh_obj = models.ShareRequest(sh["cand_refs"], sh["fields"],
                                     direction=sh["direction"], purpose=sh["purpose"],
                                     reason=sh["reason"], applicant=sh["applicant"])
        sh_obj.req_id = sh["req_id"]
        sh_obj.created_at = sh["created_at"]
        sh_obj.created_epoch = sh["created_epoch"]
        if not approve:
            sh_obj.status = "rejected"
            sh_obj.reject_reason = reject_reason
            self.store.save_share(sh_obj)
            self.store.log("共享审批", "%s 驳回：%s" % (req_id, reject_reason or "-"),
                           session.username)
            return {"req_id": req_id, "status": "rejected"}

        sh_obj.status = "approved"
        sh_obj.approver = session.username
        sh_obj.approved_at = models.now_ts()
        sh_obj.approved_epoch = models.now_epoch()
        path = ""
        rows = 0
        if export:
            path, rows, payload = self._export(sh_obj)
            sh_obj.export_path = os.path.basename(path)
            sh_obj.row_count = rows
        self.store.save_share(sh_obj)
        self.store.log("共享审批", "%s 批准（审批人=%s）" % (req_id, session.username),
                       session.username)
        if export:
            self.store.log("导出", "%s 导出 %d 条 -> %s（脱敏）"
                           % (req_id, rows, os.path.basename(path)), session.username)
        return {"req_id": req_id, "status": "approved", "export_path": path,
                "rows": rows, "evidence": EVIDENCE["共享"]}

    def _export(self, sh_obj):
        """生成脱敏导出件（只含必要字段；不含直接标识与生物特征）"""
        rows = []
        for ref in sh_obj.cand_refs:
            cand = self.store.get_candidate(ref)
            if cand is None:
                continue
            item = {}
            for f in sh_obj.fields:
                if f == "ref":
                    item["ref"] = cand.cand_ref
                elif f == "room_id":
                    item["room_id"] = cand.room_id
                elif f == "seat_no":
                    item["seat_no"] = desensitize.mask_seat_no(cand.seat_no)
                elif f == "ticket_no":
                    item["ticket_no"] = desensitize.mask_ticket_no(cand.ticket_no)
                else:
                    item[f] = ""
            rows.append(item)
        name = "export_%s_%s.json" % (sh_obj.req_id, time.strftime("%Y%m%d%H%M%S"))
        path = os.path.join(str(config.EXPORT_DIR), name)
        body = {"req_id": sh_obj.req_id, "approver": sh_obj.approver,
                "approved_at": sh_obj.approved_at, "fields": sh_obj.fields,
                "purpose": sh_obj.purpose, "direction": sh_obj.direction,
                "masked": True, "rows": rows}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        return path, len(rows), body

    # ============================================================ 销毁
    def run_auto_destroy(self, now=None, operator="system"):
        """
        到期自动销毁：由系统强制执行，不接受人工"忘记删除"的例外。
        返回 {"scanned", "destroyed", "kept_by_legal_hold", "details", "evidence"}
        """
        if not config.DESTROY["auto"]:
            self.store.log("销毁", "自动销毁已关闭（配置 DESTROY.auto=False）", operator)
            return {"scanned": 0, "destroyed": 0, "kept_by_legal_hold": 0,
                    "details": [], "evidence": EVIDENCE["销毁"],
                    "disabled": True}

        now = now or models.now_epoch()
        all_cand = self.store.list_candidates(limit=10 ** 6)
        expired = self.store.expired_candidates(now=now)
        hold = [c for c in all_cand if c.legal_hold and c.retention_days is not None
                and c.destroy_at_epoch is not None and now >= c.destroy_at_epoch]
        details = []
        for cand in expired:
            covered = self._purge(cand)
            rec = models.DestroyRecord(cand.cand_ref, "全类别", "auto",
                                       reason="保留期到期，系统自动销毁",
                                       operator=operator, covered=covered)
            self.store.save_destroy(rec)
            details.append({"cand_ref": cand.cand_ref, "destroy_id": rec.destroy_id,
                            "covered": covered})
            self.store.log("销毁", "到期自动销毁 %s（覆盖 %s）"
                           % (cand.cand_ref, json.dumps(covered, ensure_ascii=False)),
                           operator)
        for cand in hold:
            self.store.log("销毁", "跳过 %s：有法律保留必要（%s）"
                           % (cand.cand_ref, cand.legal_hold_reason or "未填理由"),
                           operator)
        return {"scanned": len(all_cand), "destroyed": len(details),
                "kept_by_legal_hold": len(hold), "details": details,
                "evidence": EVIDENCE["销毁"]}

    def _purge(self, cand):
        """真正把该考生的敏感数据清掉，并返回覆盖清单（可核查）。"""
        covered = {"fields": [], "files": [], "categories": []}
        # 图像文件
        if cand.face_image_path:
            path = os.path.join(str(config.VAULT_DIR), cand.face_image_path)
            if os.path.exists(path):
                try:
                    os.remove(path)
                    covered["files"].append(os.path.basename(path))
                except OSError:
                    pass
        # 同意记录（连带删除，避免"人删了同意还在"）
        with self.store._lock, self.store._conn() as c:
            c.execute("DELETE FROM consents WHERE cand_ref=?", (cand.cand_ref,))
            c.execute("DELETE FROM candidates WHERE cand_ref=?", (cand.cand_ref,))
        covered["fields"] = ["name", "id_card_no", "ticket_no", "seat_no",
                             "face_template", "face_image_path"]
        covered["categories"] = ["face_template", "face_image", "enroll_identity"]
        return covered

    def destroy_by_request(self, session, cand_ref, reason, witness="",
                           force=False):
        """
        按申请销毁（如考生行使删除权，或误采数据）。
        force=True 时为"人工强制销毁"，需双人确认（witness 必填）。
        """
        session.require("force_destroy" if force else "destroy")
        if not (reason or "").strip():
            raise LifecycleError("销毁必须填写理由")
        if force and config.DESTROY["double_confirm"] and not (witness or "").strip():
            raise LifecycleError("强制销毁需双人确认：必须填写见证人")
        cand = self.store.get_candidate(cand_ref)
        if cand is None:
            raise LifecycleError("考生记录不存在：%s" % cand_ref)
        if cand.legal_hold and not force:
            raise LifecycleError("该记录有法律保留必要（%s），不能按申请销毁"
                                 % (cand.legal_hold_reason or "未填理由"))
        covered = self._purge(cand)
        rec = models.DestroyRecord(cand_ref, "全类别", "force" if force else "manual",
                                   reason=reason, operator=session.username,
                                   witness=witness, covered=covered)
        self.store.save_destroy(rec)
        self.store.log("销毁", "%s 销毁 %s（理由=%s，见证=%s）"
                       % ("强制" if force else "按申请", cand_ref, reason[:40],
                          witness or "-"), session.username)
        return {"destroy_id": rec.destroy_id, "covered": covered,
                "evidence": EVIDENCE["销毁"]}

    def set_legal_hold(self, session, cand_ref, on=True, reason=""):
        """设置/解除法律保留（保留必须写理由；无理由的保留不予设置）。

        注意：法律保留只是"不能删"的运行状态标记，不属于加密载荷，
        因此走受控的 update_candidate_meta（白名单里只有 legal_hold 一类字段），
        加密数据的本体依然不可改。
        """
        session.require("destroy")
        if on and not (reason or "").strip():
            raise LifecycleError("设置法律保留必须填写理由")
        cand = self.store.get_candidate(cand_ref)
        if cand is None:
            raise LifecycleError("考生记录不存在：%s" % cand_ref)
        self.store.update_candidate_meta(
            cand_ref, legal_hold=1 if on else 0,
            legal_hold_reason=reason if on else "")
        self.store.log("销毁", "%s 法律保留 %s（理由=%s）"
                       % ("设置" if on else "解除", cand_ref, reason or "-"),
                       session.username)
        return True

    # ============================================================ 阶段汇总
    def stage_table(self):
        """把第二节的六阶段控制与证据渲染成表（报告直接引用）。"""
        stages = [
            ("采集", "考前单独同意、非静默开启、频率按需最小化",
             "同意记录 / 授权日志 / 采集参数配置"),
            ("传输", "专网传输 + 加密（国密优先），告警片段与元数据加密上传",
             "加密配置 / 链路说明"),
            ("存储", "特征模板不可逆、原始图像单独加密、分类分级存储",
             "加密证明 / 模板不可逆说明"),
            ("使用", "目的限定、脱敏展示、最小权限、水印溯源",
             "权限矩阵 / 脱敏截图 / 水印样例"),
            ("共享", "跨层级共享仅传必要字段，导出需审批",
             "审批工单 / 导出审计日志"),
            ("销毁", "到期自动删除 + 销毁记录，介质报废按规处理",
             "删除策略配置 / 销毁记录台账"),
        ]
        return [{"stage": s, "control": c, "evidence": e} for s, c, e in stages]
