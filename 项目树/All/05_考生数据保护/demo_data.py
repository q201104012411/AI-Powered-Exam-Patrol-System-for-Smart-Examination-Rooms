# -*- coding: utf-8 -*-
r"""
演示数据生成（仅在临时/演示数据目录里用）

用途：让「跑一遍就看到全部功能」成为可能，同时**不冒充真实接入**——
所有演示记录都带 demo=1 标记，报告里也单独说明。

生成内容：
  · 6 名考生（含人脸特征模板；2 名留原始图像）
  · 对应 6 条单独同意（其中 1 条已撤回，用于验证替代通道）
  · 3 个考点的非人脸替代通道 + 演练记录
  · 管理措施台账
  · 1 起安全事件（2 小时内上报）
  · 1 条超目的限定使用（被拒绝，用于验证"越权全部拦截"）
  · 2 条到期记录（用于验证"到期自动销毁执行率 100%"）
"""
import time

from dpp import demo_flags
from dpp import crypto, models


DEMO_CANDIDATES = [
    # 姓名, 证件号, 准考证, 座次, 考场, 考点, 是否留图
    ("张三", "330100200801011234", "2026T0001", "01", "A-01", "一中考点", True),
    ("李四", "330100200802022345", "2026T0002", "02", "A-01", "一中考点", False),
    ("王五", "330100200803033456", "2026T0003", "03", "A-01", "一中考点", False),
    ("赵六", "330100200804044567", "2026T0004", "04", "A-02", "一中考点", True),
    ("钱七", "330100200805055678", "2026T0005", "05", "B-01", "二中考点", False),
    ("孙八", "330100200806066789", "2026T0006", "06", "B-01", "二中考点", False),
]


def _fake_image(seed: bytes, size=48):
    """合成一张"图像"字节（不依赖图像库，保证在没有 Pillow 的机器上也能跑）。"""
    import hashlib
    out = bytearray()
    h = seed
    while len(out) < size * size * 3:
        h = hashlib.sha256(h).digest()
        out.extend(h)
    return bytes(out[:size * size * 3])


def generate(store, accounts, lifecycle, gov, rights, now=None):
    """
    生成一整套演示数据，返回统计摘要。
    需要已登录的 session 由调用方提供（这里用 owner 会话来跑，便于演示）。
    """
    now = now or models.now_epoch()
    sessions = {}

    def sess(username, password):
        s = accounts.authenticate(username, password, place="考点考务办",
                                  device="DEV-DEMO-01")
        sessions[username] = s
        return s

    owner = sess("owner", "owner")
    oper = sess("oper1", "oper1")
    dpo = sess("dpo1", "dpo1")

    created = []
    for i, (name, idno, ticket, seat, room, site, keep_img) in enumerate(DEMO_CANDIDATES):
        # 1) 先登记单独同意（采集的硬前置）。
        #    注意：本系统"加密数据只读"，不能再"先落一条考生占位、稍后再改"。
        #    正确做法是先定 cand_ref -> 写同意（直接绑定该 ref）-> 一次性 collect 登记。
        cand_ref = models.new_id("C")
        cs = models.Consent(cand_ref=cand_ref, name=name, scope="face",
                            method="考前告知书签署（电子）", operator="oper1",
                            evidence_path="consent/%s.pdf" % ticket)
        store.save_consent(cs)
        img = _fake_image(cand_ref.encode()) if keep_img else None
        res = lifecycle.collect(
            oper, name=name, id_card_no=idno, ticket_no=ticket, seat_no=seat,
            room_id=room, site=site,
            feature_vector="feature-%s-%s" % (cand_ref, seat),
            face_image_bytes=img, consent_id=cs.consent_id,
            captured_epoch=(now - 40 * 86400) if i >= 4 else now,
            retention_days=30, cand_ref=cand_ref,
            operator_terminal="考务登记台")
        created.append({"name": name, "ref": res["cand_ref"], "room": room,
                        "img": res["image_encrypted"]})

    # 2) 撤回一名考生的同意（验证"撤回后走替代通道"）
    withdraw_ref = created[1]["ref"]
    rights.submit(oper, withdraw_ref, "withdraw",
                  detail="考生到考点提出不再使用人脸核验",
                  channel="考点窗口受理", identity_verified=True)

    # 3) 替代通道 + 演练
    channel_ids = {}
    for site, kind in (("一中考点", "manual"), ("二中考点", "manual"),
                       ("三中考点", "document")):
        cid = gov.add_channel(site, name="%s人工核验通道" % site, kind=kind)
        channel_ids[site] = cid
        gov.drill_channel(owner, cid, passed=True, note="撤掉人脸通道后演练通过")

    # 4) 管理措施台账
    gov.seed_default_measures(project_owner="项目组·张工",
                              site_owner="各考点考务主任")
    for m in store.list_measures():
        if m["kind"] == "DPIA":
            gov.complete_measure(owner, m["measure_id"],
                                evidence="DPIA 报告 dpia_2026.pdf（已归档）")
        if m["kind"] == "培训":
            gov.complete_measure(owner, m["measure_id"],
                                evidence="培训签到表 2026Q1.xlsx")

    # 5) 一起安全事件（2 小时内上报）
    gov.report_incident(owner, "S3", "值班终端误装第三方输入法",
                        detail="运维巡检发现，未造成数据外泄，已卸载并留痕")

    # 6) 一条超目的限定使用（被拒绝；验证"越权全部拦截"）
    try:
        lifecycle.use(oper, created[2]["ref"], "情绪分析", "生成考生情绪曲线",
                      terminal="巡考终端-R7")
    except Exception:
        pass      # 预期被拒绝，拒绝本身已写进 uses 与审计链

    # 7) 正确的一次使用（作弊判定）
    lifecycle.use(oper, created[0]["ref"], "作弊判定", "关联告警复核",
                  terminal="巡考终端-R7")

    # 8) 一次授权解密查看（留痕 + 水印）
    lifecycle.reveal_plain(oper if oper.can("reveal") else owner, created[0]["ref"],
                           "复核手机告警需要核对证件", terminal="巡考终端-R7")

    # 9) 一次共享申请 + 审批导出（脱敏）
    sh = lifecycle.request_share(
        owner, [created[0]["ref"], created[2]["ref"]],
        ["ref", "room_id", "seat_no", "ticket_no"],
        direction="up", purpose="作弊判定", reason="上级巡考需要汇总异常清单")
    lifecycle.approve_share(dpo, sh["req_id"], approve=True, export=True)

    # 10) 两条到期记录（用于验证自动销毁执行率）
    destroyed = lifecycle.run_auto_destroy(now=now, operator="system")

    # 11) DPIA/审批类措施之外，再补一条权限审批台账
    gov.add_measure(owner, "权限审批", "高敏数据访问与导出审批流程",
                    owner="数据保护负责人", detail="审批工单模板与季度复核记录")

    summary = {
        "考生": len(created), "留图": sum(1 for c in created if c["img"]),
        "替代通道": len(channel_ids), "已自动销毁": destroyed["destroyed"],
        "演示标记": True,
    }
    store.log("演示数据", "已生成演示数据：%s" % summary, "system")
    return summary


def describe():
    return {"演示考生数": len(DEMO_CANDIDATES),
            "说明": "演示数据带 demo 标记，不冒充真实接入"}
