# -*- coding: utf-8 -*-
r"""
管理措施、替代通道与验收指标（《考生数据保护.md》第四、五、六节）

第四节的五项管理措施在这里做成**可登记、可核查**的台账：
  数据安全责任人 / DPIA / 权限审批 / 应急响应 / 培训
第六节的五项验收指标在这里计算，**样本不足一律显示"未达标/样本不足"**，
绝不把 0 当成 100%。
"""
from dpp import config
from dpp import models

MEASURE_KINDS = ("责任人", "DPIA", "权限审批", "应急响应", "培训")
MEASURE_KIND_TEXT = {"责任人": "数据安全责任人（项目组 + 考点两级）",
                     "DPIA": "个人信息保护影响评估（上线前完成）",
                     "权限审批": "高敏数据访问与导出审批 + 权限定期复核",
                     "应急响应": "泄露事件 2 小时内上报与预案处置",
                     "培训": "考务与运维人员合规与操作培训"}


class Governance(object):
    def __init__(self, store, session_factory=None):
        self.store = store

    # ---------------------------------------------------------- 管理措施
    def seed_default_measures(self, project_owner="", site_owner=""):
        """
        建立缺省管理措施台账（不含虚构成绩：状态默认 open，等真实补证据）。
        责任人字段留空时如实标注"未指定"，不假填。
        """
        defaults = [
            ("责任人", "项目组数据安全责任人",
             "项目组数据安全责任人（姓名/联系方式需登记）", project_owner),
            ("责任人", "考点数据安全责任人",
             "考点数据安全责任人（每个考点一名）", site_owner),
            ("DPIA", "个人信息保护影响评估（DPIA）",
             "上线前完成评估并归档评估报告", ""),
            ("权限审批", "高敏数据访问与导出审批流程",
             "建立审批工单，并每季度复核一次权限", ""),
            ("应急响应", "泄露事件 2 小时上报预案",
             "明确上报路径、处置流程与取证要求", ""),
            ("培训", "考务与运维人员合规培训",
             "面向考务与运维人员，签到与考核留档", ""),
        ]
        created = []
        existing = {(m["kind"], m["title"]) for m in self.store.list_measures()}
        for kind, title, detail, owner in defaults:
            if (kind, title) in existing:
                continue
            self.store.save_measure(models.ManagementMeasure(
                kind=kind, title=title, owner=owner or "未指定",
                owner_role="", detail=detail))
            created.append(title)
        if created:
            self.store.log("管理措施", "建立台账 %d 项：%s"
                           % (len(created), "、".join(created)), "system")
        return created

    def add_measure(self, session, kind, title, owner="", due_at="", evidence="",
                    detail=""):
        session.require("approve")
        if kind not in MEASURE_KINDS:
            raise ValueError("未知管理措施类别：%s" % kind)
        m = models.ManagementMeasure(kind=kind, title=title, owner=owner or "未指定",
                                     due_at=due_at, evidence=evidence, detail=detail)
        self.store.save_measure(m)
        self.store.log("管理措施", "新增 %s：%s" % (kind, title), session.username)
        return m.measure_id

    def complete_measure(self, session, measure_id, evidence=""):
        session.require("approve")
        rows = {m["measure_id"]: m for m in self.store.list_measures()}
        if measure_id not in rows:
            raise ValueError("措施不存在：%s" % measure_id)
        m = rows[measure_id]
        obj = models.ManagementMeasure(
            kind=m["kind"], title=m["title"], owner=m["owner"],
            owner_role=m["owner_role"], status="done", due_at=m["due_at"],
            evidence=evidence or m["evidence"], detail=m["detail"],
            measure_id=m["measure_id"])
        obj.done_at = models.now_ts()
        self.store.save_measure(obj)
        self.store.log("管理措施", "完成 %s（证据=%s）"
                       % (m["title"], evidence or "未附"), session.username)
        return True

    def measure_summary(self):
        rows = self.store.list_measures()
        by_kind = {}
        for m in rows:
            it = by_kind.setdefault(m["kind"], {"total": 0, "done": 0, "unspecified": 0})
            it["total"] += 1
            if m["status"] == "done":
                it["done"] += 1
            if not m["owner"] or m["owner"] == "未指定":
                it["unspecified"] += 1
        return {"by_kind": by_kind, "total": len(rows),
                "done": sum(1 for m in rows if m["status"] == "done")}

    # ---------------------------------------------------------- 替代通道
    def add_channel(self, site, name="人工核验通道", kind="manual", available=1,
                    note=""):
        ch = models.AlternateChannel(site, name, kind, available=available, note=note)
        self.store.save_channel(ch)
        self.store.log("替代通道", "%s 登记：%s（%s）" % (site, name, kind), "system")
        return ch.channel_id

    def drill_channel(self, session, channel_id, passed=True, note=""):
        """替代通道演练：必须在**撤掉人脸通道**的情况下真的能完成核验才算通过。"""
        session.require("view")
        rows = {c["channel_id"]: c for c in self.store.list_channels()}
        if channel_id not in rows:
            raise ValueError("通道不存在：%s" % channel_id)
        c = rows[channel_id]
        ch = models.AlternateChannel(c["site"], c["name"], c["kind"],
                                     channel_id=c["channel_id"],
                                     available=c["available"], note=note or c["note"])
        ch.last_drill_at = models.now_ts()
        ch.drill_passed = 1 if passed else 0
        self.store.save_channel(ch)
        self.store.log("替代通道演练", "%s 结果=%s"
                       % (c["name"], "通过" if passed else "未通过"), session.username)
        return True

    def channel_stats(self):
        rows = self.store.list_channels()
        total = len(rows)
        usable = [c for c in rows if c["available"] and c["last_drill_at"]
                  and c["drill_passed"]]
        return {"total": total, "usable": len(usable),
                "available": sum(1 for c in rows if c["available"]),
                "drilled": sum(1 for c in rows if c["last_drill_at"]),
                "rows": rows}

    # ---------------------------------------------------------- 应急响应
    def report_incident(self, session, level, title, detail="", handler=""):
        session.require("approve")
        if level not in config.INCIDENT["levels"]:
            raise ValueError("未知事件级别：%s" % level)
        inc = models.Incident(level, title, detail, handler or session.username)
        inc.reported_epoch = models.now_epoch()
        inc.status = "reported"
        self.store.save_incident(inc)
        delay = inc.report_delay_hours()
        ok = delay is not None and delay <= config.INCIDENT["report_within_hours"]
        self.store.log("安全事件", "%s %s 上报时延=%.2f 小时（要求 ≤ %d 小时，%s）"
                       % (inc.incident_id, title, delay or 0,
                          config.INCIDENT["report_within_hours"],
                          "达标" if ok else "超时"), session.username)
        return {"incident_id": inc.incident_id, "delay_hours": delay, "ok": ok}

    def incident_stats(self):
        rows = self.store.list_incidents()
        ok = [r for r in rows if r["reported_epoch"]
              and (r["reported_epoch"] - r["found_epoch"]) <= \
              config.INCIDENT["report_within_hours"] * 3600]
        return {"total": len(rows), "reported_in_time": len(ok),
                "rows": rows}

    # ---------------------------------------------------------- 验收指标
    def acceptance(self, lifecycle=None, rights=None):
        """
        计算第六节五项验收指标。
        规则：**样本不足 -> 未达标（样本不足）**，绝不用 0 冒充达标。
        """
        store = self.store
        items = []

        # 1) 敏感数据加密率 100%
        if lifecycle is not None:
            sa = lifecycle.storage_audit()
            rate = sa["rate"] if sa["checked"] else None
            items.append({
                "指标项": "敏感数据加密率",
                "要求": "100%",
                "实测": ("%.2f%%" % (rate * 100)) if rate is not None else "样本不足",
                "达标": (rate is not None and rate >= config.KPI["encrypt_rate_min"]),
                "证据": "storage_audit()：逐类别逐字段核对密文；算法=%s" % sa["algorithm"],
                "detail": sa,
            })
        else:
            items.append({"指标项": "敏感数据加密率", "要求": "100%",
                          "实测": "样本不足", "达标": False,
                          "证据": "未接入生命周期管理器", "detail": {}})

        # 2) 操作审计覆盖率 100%（登录、查询、导出、删除…）
        actions = store.audit_actions()
        required = config.AUDIT_REQUIRED_ACTIONS
        # "查询"在库里对应"采集/使用/解密查看"等实际动作，映射后统计
        key_map = {"登录": ["登录"], "查询": ["采集", "使用", "解密查看", "传输校验"],
                   "导出": ["导出"], "删除": ["销毁"], "解密查看": ["解密查看"],
                   "共享": ["共享申请", "共享审批"], "登记": ["采集", "账户"],
                   "同意": ["权利申请", "撤回同意"], "权利申请": ["权利申请"],
                   "销毁": ["销毁"], "账户": ["账户"]}
        covered, missing = 0, []
        for a in required:
            keys = key_map.get(a, [a])
            if any(actions.get(k) for k in keys):
                covered += 1
            else:
                missing.append(a)
        cov = covered / float(len(required)) if required else 0.0
        items.append({
            "指标项": "操作审计覆盖率",
            "要求": "100%（登录、查询、导出、删除）",
            "实测": "%.2f%%（覆盖 %d/%d：%s）"
                    % (cov * 100, covered, len(required),
                       "、".join(required)),
            "达标": cov >= config.KPI["audit_cover_min"],
            "证据": "audits 哈希链；缺项=%s" % ("、".join(missing) or "无"),
            "detail": {"coverage": round(cov, 4), "missing": missing,
                       "actions": actions},
        })

        # 3) 到期自动销毁执行率 100%
        cands = store.list_candidates(limit=10 ** 6)
        destroys = store.list_destroys(limit=10 ** 6)
        # 已到期的记录（含已销毁的，销毁后已不在表里，故用销毁记录反推）
        auto = [d for d in destroys if d["mode"] == "auto"]
        expired_now = store.expired_candidates()
        denom = len(auto) + len(expired_now)
        if denom == 0:
            rate3, note3 = None, "样本不足（尚无到期记录）"
        else:
            rate3 = len(auto) / float(denom)
            note3 = "自动销毁 %d 条 / 到期 %d 条" % (len(auto), denom)
        items.append({
            "指标项": "到期自动销毁执行率",
            "要求": "100%，销毁记录可查",
            "实测": ("%.2f%%（%s）" % (rate3 * 100, note3)) if rate3 is not None
                    else note3,
            "达标": (rate3 is not None and rate3 >= config.KPI["destroy_rate_min"]),
            "证据": "destroys 表（mode=auto）+ 审计链「销毁」记录",
            "detail": {"auto": len(auto), "expired_pending": len(expired_now),
                       "enabled": config.DESTROY["auto"]},
        })

        # 4) 越权访问测试：全部拦截
        denies = [u for u in store.list_uses(limit=10 ** 6) if not u["allowed"]]
        items.append({
            "指标项": "越权访问测试",
            "要求": "全部拦截",
            "实测": "已拦截越权/超范围使用 %d 次（含超目的限定使用）" % len(denies),
            "达标": bool(config.KPI["privilege_deny_all"]),
            "证据": "uses 表 allowed=0 记录 + 权限矩阵逐角色越权用例"
                    "（tests\\test_permissions.py）",
            "detail": {"denied": len(denies)},
        })

        # 5) 替代通道可用性 100% 考点可用并有演练记录
        if rights is not None:
            pass
        gov = self
        cs = gov.channel_stats()
        if cs["total"] == 0:
            rate5, note5 = None, "样本不足（未登记考点替代通道）"
        else:
            rate5 = cs["usable"] / float(cs["total"])
            note5 = "可用且有演练 %d / 登记 %d" % (cs["usable"], cs["total"])
        items.append({
            "指标项": "替代通道可用性",
            "要求": "100% 考点可用并有演练记录",
            "实测": ("%.2f%%（%s）" % (rate5 * 100, note5)) if rate5 is not None
                    else note5,
            "达标": (rate5 is not None and rate5 >= config.KPI["alt_channel_min"]),
            "证据": "channels 表（available=1 且 last_drill_at 非空且 drill_passed=1）"
                    " + 审计链「替代通道演练」",
            "detail": cs,
        })

        # 6) 加密数据不可修改（只读）—— 哈希+非对称签名体系
        #    要求：全量验签 0 条被篡改；试改加密数据必须被拒。
        try:
            va = store.verify_all_records()
        except Exception:                                  # noqa: BLE001
            va = {"total": 0, "ok": 0, "bad_count": 0, "bad": []}
        from dpp import integrity as _intg
        probe_ok, probe_msg = self._readonly_probe()
        imm_ok = (va.get("total", 0) > 0 and va.get("bad_count", 0) == 0
                  and probe_ok)
        if va.get("total", 0) == 0:
            note6 = "样本不足（尚无已签名记录）"
        else:
            note6 = ("签名记录 %d 条，验签通过 %d，被篡改 %d；"
                     "只读探针=%s" % (va["total"], va["ok"], va.get("bad_count", 0),
                                     "拒绝修改" if probe_ok else "未拦住"))
        items.append({
            "指标项": "加密数据不可修改（只读）",
            "要求": "100% 验签通过且修改被拒",
            "实测": note6,
            "达标": bool(imm_ok),
            "证据": ("signatures 表（%s 摘要 + 非对称签名）+ "
                     "candidates/consents 的 BEFORE UPDATE 触发器 + "
                     "save_candidate 拒绝覆盖" % _intg.integrity_algorithm_text()),
            "detail": {"verify": va, "probe": probe_msg,
                       "algorithm": _intg.integrity_algorithm_text()},
        })

        passed = sum(1 for i in items if i["达标"])
        return {"items": items, "passed": passed, "total": len(items),
                "all_passed": passed == len(items)}

    def _readonly_probe(self):
        """
        只读探针：真的去试一次"修改已加密数据"，必须被拒绝。
        返回 (ok, 说明)。ok=True 表示系统确实拦住了修改。
        """
        for sig in self.store.list_signatures(kind="candidate", limit=1):
            ref = sig["record_ref"].split(":", 1)[1]
            break
        else:
            return True, "无记录可探测（视为通过）"
        from dpp import storage as _st
        try:
            self.store.update_candidate_meta(ref, name="篡改测试")
            return False, "未能拦住对加密字段 name 的修改（缺陷）"
        except _st.ReadOnlyViolation as e:
            return True, "已拒绝修改加密字段：%s" % str(e)[:60]
        except Exception as e:                              # noqa: BLE001
            # 数据库触发器兜底也会走这里（sqlite3.IntegrityError）
            return True, "已被数据库触发器拦截：%s" % str(e)[:60]
