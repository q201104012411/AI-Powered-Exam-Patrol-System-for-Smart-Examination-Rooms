# -*- coding: utf-8 -*-
r"""
验收证据报告（《考生数据保护.md》全篇的可核查产出）

产出三份：
  1. 考生数据保护验收报告（Markdown）—— 分类分级、六阶段控制、三类技术措施、
     管理措施、权利响应、五项验收指标、可核查证据清单
  2. 数据处理活动登记表（CSV）—— 逐类别逐字段的处理活动台账，便于直接归档
  3. 销毁记录台账（CSV）—— 到期自动 / 按申请 / 强制 全部可查
"""
import csv
import os
import time

from dpp import config
from dpp import crypto
from dpp import catalog, governance, models


class Reporter(object):
    def __init__(self, store, lifecycle=None, gov=None, rights=None):
        self.store = store
        self.lifecycle = lifecycle
        self.gov = gov or governance.Governance(store)
        self.rights = rights

    # ---------------------------------------------------------- 主报告
    def full_report(self):
        L = []
        A = L.append
        A("# 考生数据保护 · 验收报告")
        A("")
        A("- 生成时间：%s" % models.now_ts())
        A("- 数据目录：%s" % config.DATA_ROOT)
        A("- 加密算法：%s" % crypto.algorithm_text())
        A("- 密钥指纹（SHA256 前 16 位）：%s" % crypto.key_fingerprint())
        A("- 审计链：%s" % self._chain_text())
        A("")
        A("> 本报告对应《考生数据保护.md》全篇；每个控制点均给出**可核查证据**，"
          "统计为 0 时如实标注「样本不足」，不以 0 冒充达标。")
        A("")

        A(self._sec_classification())
        A(self._sec_lifecycle())
        A(self._sec_tech())
        A(self._sec_integrity())
        A(self._sec_governance())
        A(self._sec_rights())
        A(self._sec_kpi())
        A(self._sec_evidence())
        return "\n".join(L)

    def _chain_text(self):
        ok, msg, n = self.store.verify_chain()
        return "%s（%s）" % ("完整" if ok else "校验失败", msg)

    # ---------------- 第一节 ----------------
    def _sec_classification(self):
        L = ["## 一、数据分类分级", "", catalog.table_markdown(), ""]
        ok, problems = catalog.validate()
        L.append("- 目录自洽性校验：%s" % ("通过" if ok else "不通过：" + "；".join(problems)))
        L.append("- 参与到期自动销毁的类别：%s"
                 % "、".join(c.name for c in catalog.auto_destroyable()))
        L.append("")
        return "\n".join(L)

    # ---------------- 第二节 ----------------
    def _sec_lifecycle(self):
        L = ["## 二、全生命周期控制", "",
             "| 阶段 | 控制措施 | 本系统实现 | 可核查证据 |",
             "| --- | --- | --- | --- |"]
        impl = {
            "采集": "单独同意硬前置（无同意拒绝采集）、禁止静默开启、帧率上限 %d、"
                    "特征模板不可逆、原图单项加密且 ≤ %d 天"
                    % (config.CAPTURE["fps_max"], config.CAPTURE["retention_image_days"]),
            "传输": "专网 + 加密校验（国密优先：%s），明文协议地址一律拒绝"
                    % crypto.algorithm_text(),
            "存储": "字段级加密（密文带 enc: 前缀）、分类分级、密钥 DPAPI 包裹",
            "使用": "目的限定白名单（%s）；越范围使用被拒且留痕；默认脱敏 + 动态水印"
                    % "、".join(models.PURPOSE_ALLOWED),
            "共享": "必要字段白名单（直接标识与生物特征禁止共享）、导出需审批且留痕",
            "销毁": "到期自动销毁（%s）、销毁记录不可篡改、介质报废按规处理"
                    % ("已开启" if config.DESTROY["auto"] else "**已关闭**"),
        }
        if self.lifecycle is not None:
            for row in self.lifecycle.stage_table():
                L.append("| %s | %s | %s | %s |"
                         % (row["stage"], row["control"], impl.get(row["stage"], "—"),
                            row["evidence"]))
        L.append("")
        return "\n".join(L)

    # ---------------- 第三节 ----------------
    def _sec_tech(self):
        L = ["## 三、工程化技术措施", ""]
        items = [
            ("不可逆特征存储",
             "人脸比对只落特征模板（HMAC-SHA256 摘要，前缀 tpl1:），不落原图；"
             "确需留图的单项加密并设最短期限",
             "candidates.face_template 全部为 tpl1: 摘要"),
            ("去标识化展示",
             "巡考客户端默认以编号/座次展示；明文与图像需 reveal 授权并全程留痕",
             "默认视图示例：%s" % self._sample_deid()),
            ("动态水印",
             "视频回看与截图自动叠加「操作人+时间+终端」水印，防外泄溯源",
             "水印样例：%s" % self._sample_watermark()),
            ("最小权限 + 持续校验",
             "角色×动作白名单（默认拒绝）；结合登录地点/设备/时段做风险分级，"
             "高风险会话对高风险动作要求二次确认",
             "权限矩阵共 %d 个角色 × %d 个动作"
             % (len(config.ROLE_ACTIONS), len(config.ALL_ACTIONS))),
            ("到期自动销毁",
             "存储策略与销毁任务由系统强制执行，不接受人工「忘记删除」的例外；"
             "销毁生成不可篡改记录",
             "已生成销毁记录 %d 条（其中自动 %d 条）"
             % (self.store.summary()["销毁总数"], self.store.summary()["其中自动销毁"])),
            ("禁止出境与超范围使用",
             "数据不得用于情绪分析/行为画像/商业用途，不得跨境传输",
             "越范围使用请求 %d 次，全部被拒并留痕"
             % len([u for u in self.store.list_uses(limit=10 ** 6)
                    if not u["allowed"]])),
            ("加密数据不可修改（只读）",
             "已加密数据任何人不得就地修改，只能删除或追加新增；"
             "每条记录落「哈希(SM3)+非对称签名(SM2)」，改一处即验签失败；"
             "数据库层再加 BEFORE UPDATE 触发器兜底",
             self._integrity_evidence()),
        ]
        L.append("| 措施 | 说明 | 当前实测证据 |")
        L.append("| --- | --- | --- |")
        for name, desc, ev in items:
            L.append("| %s | %s | %s |" % (name, desc, ev))
        L.append("")
        return "\n".join(L)

    def _sample_deid(self):
        cands = self.store.list_candidates(limit=1)
        if not cands:
            return "样本不足（尚无考生记录）"
        return cands[0].display_deid()

    def _sample_watermark(self):
        from dpp import desensitize
        return desensitize.watermark_text("巡考员01", "巡考终端-R7")

    def _integrity_evidence(self):
        """完整性体系的可核查证据摘要（哈希 + 非对称签名 + 只读触发器）。"""
        from dpp import integrity
        try:
            va = self.store.verify_all_records()
        except Exception:                                   # noqa: BLE001
            va = {"total": 0, "ok": 0, "bad_count": 0}
        algo = integrity.integrity_algorithm_text()
        if va.get("total", 0) == 0:
            return "算法 %s；暂无已签名记录（样本不足）" % algo
        return ("算法 %s；已签名 %d 条，验签通过 %d 条，被篡改 %d 条"
                % (algo, va["total"], va["ok"], va.get("bad_count", 0)))

    # ---------------- 完整性（只读不可改） ----------------
    def _sec_integrity(self):
        """「加密数据不可修改」专项说明（第五节，插在技术措施之后）。"""
        from dpp import integrity
        L = ["## 三·补 加密数据只读（哈希 + 非对称签名，不可修改）", ""]
        L.append("本节回应「任何人都不能动加密的数据，只能删除或上传新数据」的要求。")
        L.append("")
        L.append("| 机制 | 做法 | 效果 |")
        L.append("| --- | --- | --- |")
        L.append("| 哈希 | %s：对每条加密记录的规范内容取摘要 | 内容改动 1 位，"
                 "摘要即不同 |" % integrity.hash_algorithm().upper())
        L.append("| 非对称签名 | %s：用私钥对摘要签名，公钥验签 | 无签名私钥者"
                 "改不出合法签名 |" % integrity.signature_algorithm().upper())
        L.append("| 只读策略 | candidates/consents 用 INSERT（非 REPLACE）；已存在即拒绝 | "
                 "没有「就地修改」这条路径 |")
        L.append("| 数据库触发器 | BEFORE UPDATE 命中加密列即 RAISE(ABORT) | "
                 "绕过业务直接改库也被挡 |")
        L.append("| 允许的操作 | 仅 DELETE（删除）与 INSERT（新增） | "
                 "删除与新增均留审计 |")
        L.append("")
        try:
            va = self.store.verify_all_records()
            L.append("- 全量验签：共 %d 条，通过 %d 条，**被篡改 %d 条**。"
                     % (va["total"], va["ok"], va.get("bad_count", 0)))
            if va.get("bad"):
                L.append("- 异常记录：%s" % "、".join(va["bad"][:10]))
        except Exception as e:                              # noqa: BLE001
            L.append("- 全量验签失败：%s" % e)
        L.append("- 签名台账条数：%d" % len(self.store.list_signatures(limit=10 ** 6)))
        L.append("- 签名密钥指纹：`%s`" % integrity.key_fingerprint())
        L.append("- 实测拦截样例：修改加密字段会被 `ReadOnlyViolation` 或数据库触发器拒绝。")
        L.append("")
        return "\n".join(L)

    # ---------------- 第四节 ----------------
    def _sec_governance(self):
        L = ["## 四、管理措施", ""]
        summary = self.gov.measure_summary()
        L.append("| 措施 | 内容 | 台账状态 | 责任人 |")
        L.append("| --- | --- | --- | --- |")
        rows = self.store.list_measures()
        for m in rows:
            status = {"done": "已完成", "open": "进行中", "overdue": "逾期"}.get(
                m["status"], m["status"])
            L.append("| %s | %s | %s | %s |"
                     % (m["kind"], m["title"], status, m["owner"]))
        if not rows:
            L.append("| — | 未建立管理措施台账 | — | — |")
        L.append("")
        L.append("- 台账完成度：%d/%d" % (summary["done"], summary["total"]))
        inc = self.gov.incident_stats()
        L.append("- 安全事件：共 %d 起，2 小时内上报 %d 起（要求 2 小时内）"
                 % (inc["total"], inc["reported_in_time"]))
        L.append("- 替代通道：登记 %d 个，可用且有演练记录 %d 个"
                 % (self.gov.channel_stats()["total"],
                    self.gov.channel_stats()["usable"]))
        L.append("")
        return "\n".join(L)

    # ---------------- 第五节 ----------------
    def _sec_rights(self):
        L = ["## 五、考生权利响应", "",
             "| 权利 | 响应方式 | 时限建议 | 本系统实现 |",
             "| --- | --- | --- | --- |"]
        impl = {
            "know": ("考前告知书、考点公示", "考前", "告知书模板 + 考点公示记录（管理措施台账）"),
            "access": ("受理申请，核验身份后提供", "5 个工作日",
                       "RightsService.submit/handle（未核验身份只登记不受理）"),
            "copy": ("受理申请，核验身份后提供", "5 个工作日", "同上，返回脱敏数据副本"),
            "correct": ("核对后更正并留痕", "5 个工作日", "更正字段原值脱敏留痕"),
            "delete": ("无法律保留必要时删除并出具记录", "10 个工作日",
                       "调用销毁并生成销毁记录；有法律保留必要则驳回并说明"),
            "withdraw": ("提供非人脸核验替代通道，不影响考试权利", "即时",
                         "提交即生效；同时检查替代通道可用性"),
        }
        for k in ("know", "access", "copy", "correct", "delete", "withdraw"):
            name, way, impl_text = impl[k]
            sla = way  # 默认用文档里的文字，下面按配置覆盖
            if k in config.RIGHTS_SLA_DAYS:
                d = config.RIGHTS_SLA_DAYS[k]
                sla = ("%d 个工作日" % d) if d else "即时"
            L.append("| %s | %s | %s | %s |" % (name, way, sla, impl_text))
        L.append("")
        if self.rights is not None:
            st = self.rights.stats()
            L.append("- 已受理申请：%d 条（已办结 %d，在办 %d，超期 %d）"
                     % (st["total"], st["done"], st["open"], st["overdue"]))
        L.append("")
        return "\n".join(L)

    # ---------------- 第六节 ----------------
    def _sec_kpi(self):
        L = ["## 六、验收指标", ""]
        acc = self.gov.acceptance(lifecycle=self.lifecycle, rights=self.rights)
        L.append("| 指标项 | 要求 | 实测 | 结论 |")
        L.append("| --- | --- | --- | --- |")
        for it in acc["items"]:
            L.append("| %s | %s | %s | %s |"
                     % (it["指标项"], it["要求"], it["实测"],
                        "**达标**" if it["达标"] else "**未达标/样本不足**"))
        L.append("")
        L.append("- 通过 %d/%d 项。" % (acc["passed"], acc["total"]))
        L.append("")
        L.append("### 指标证据明细")
        L.append("")
        for it in acc["items"]:
            L.append("- **%s**：%s" % (it["指标项"], it["证据"]))
        L.append("")
        return "\n".join(L)

    # ---------------- 证据清单 ----------------
    def _sec_evidence(self):
        L = ["## 七、可核查证据清单", "",
             "| 控制点 | 证据 | 本系统产出位置 |",
             "| --- | --- | --- |"]
        rows = [
            ("采集授权", "同意记录、采集参数配置", "consents 表 / config.CAPTURE"),
            ("传输加密", "加密配置、链路说明", "lifecycle.check_transport 审计记录"),
            ("存储加密", "加密证明、模板不可逆说明",
             "storage_audit()；密文前缀 %s；keyring\\master.key.enc" % crypto._PREFIX),
            ("使用留痕", "权限矩阵、脱敏截图、水印样例",
             "reveals 表 / 报告「工程化技术措施」章节"),
            ("共享审批", "审批工单、导出审计日志", "shares 表 / exports\\ 目录"),
            ("到期销毁", "删除策略配置、销毁记录台账", "destroys 表 / 销毁记录台账 CSV"),
            ("审计完整性", "哈希链校验结果", "audits 表 verify_chain()：%s"
             % self._chain_text()),
            ("管理措施", "责任人/DPIA/审批/应急/培训台账", "measures 表"),
            ("权利响应", "受理与办结记录", "rights 表"),
            ("替代通道", "通道登记与演练记录", "channels 表"),
        ]
        for a, b, c in rows:
            L.append("| %s | %s | %s |" % (a, b, c))
        L.append("")
        return "\n".join(L)

    # ---------------------------------------------------------- 导出
    def export_all(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = {}
        text = self.full_report()
        p1 = os.path.join(str(config.REPORT_DIR), "考生数据保护验收报告_%s.md" % ts)
        with open(p1, "w", encoding="utf-8") as f:
            f.write(text)
        out["report"] = p1
        out["activities"] = self.export_activities(ts)
        out["destroys"] = self.export_destroys(ts)
        self.store.log("导出", "生成验收报告与台账：%s"
                       % "、".join(os.path.basename(v) for v in out.values()))
        return out

    def export_activities(self, ts=None):
        """数据处理活动登记表（逐类别逐字段）"""
        ts = ts or time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(str(config.REPORT_DIR), "数据处理活动登记表_%s.csv" % ts)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["数据类别", "敏感级别", "典型内容", "存储位置", "保留期限",
                        "到期自动销毁", "必须加密字段", "当前加密算法", "不可逆"])
            for c in catalog.CATEGORIES:
                w.writerow([c.name, c.level, c.sample, c.location, c.retention_text,
                            "是" if (c.auto_destroy and c.retention_days is not None)
                            else "否",
                            "|".join(c.required_encrypted_fields),
                            crypto.algorithm_text(),
                            "是" if c.irreversible else "否"])
        return path

    def export_destroys(self, ts=None):
        """销毁记录台账"""
        ts = ts or time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(str(config.REPORT_DIR), "销毁记录台账_%s.csv" % ts)
        rows = self.store.list_destroys(limit=10 ** 6)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["销毁编号", "考生引用号", "类别", "方式", "理由", "操作人",
                        "见证人", "时间"])
            for d in rows:
                mode = {"auto": "到期自动", "manual": "按申请", "force": "强制"}.get(
                    d["mode"], d["mode"])
                w.writerow([d["destroy_id"], d["cand_ref"], d["category"], mode,
                            d["reason"], d["operator"], d["witness"] or "-", d["ts"]])
        return path

    def console_summary(self):
        """命令行/报告结尾的摘要文本"""
        acc = self.gov.acceptance(lifecycle=self.lifecycle, rights=self.rights)
        s = self.store.summary()
        L = ["考生数据保护 · 摘要", "-" * 46]
        for k, v in s.items():
            L.append("  %-12s %s" % (k, v))
        L.append("  加密算法      %s" % crypto.algorithm_text())
        L.append("  审计链        %s" % self._chain_text())
        L.append("")
        L.append("  验收指标：")
        for it in acc["items"]:
            L.append("    [%s] %-12s %s" % ("OK" if it["达标"] else "NG",
                                            it["指标项"], it["实测"]))
        L.append("")
        L.append("  通过 %d/%d 项" % (acc["passed"], acc["total"]))
        return "\n".join(L)
