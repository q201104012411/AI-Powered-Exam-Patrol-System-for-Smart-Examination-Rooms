# -*- coding: utf-8 -*-
r"""
考生数据保护 · 端到端自检

默认在**临时数据目录**里跑（不污染真实 data\）；加 --real 才动真实目录。
逐项打印 PASS/FAIL，全部通过退出码 0。

覆盖《考生数据保护.md》全篇：
  一 数据分类分级 ｜ 二 全生命周期六阶段 ｜ 三 六项工程化技术措施
  四 管理措施 ｜ 五 考生权利响应 ｜ 六 五项验收指标
自检不替代现场验收：真实同意书、真实密钥管理、真实替代通道演练必须现场补。
"""
import argparse
import os
import shutil
import sys
import tempfile

TMP = None
RESULTS = []


def _prepare(argv):
    global TMP
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="使用真实 data 目录")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)
    if not args.real:
        TMP = tempfile.mkdtemp(prefix="dpp_selfcheck_")
        os.environ["EXAM_DPP_HOME"] = TMP
    return args


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— " + str(detail)) if detail else ""))
    return bool(cond)


def main(argv=None):
    args = _prepare(argv if argv is not None else sys.argv[1:])
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from dpp import (catalog, config, crypto, desensitize,
                     lifecycle as lc_mod, models, governance,
                     report as report_mod, rights as rights_mod,
                     storage as storage_mod)

    print("=" * 74)
    print("考生数据保护 · 端到端自检")
    print("数据目录：%s%s" % (config.DATA_ROOT, "（临时）" if TMP else "（真实）"))
    print("=" * 74)

    store = storage_mod.Store()
    accounts = accounts_mod = __import__("dpp.access", fromlist=["x"]).AccountStore(store)
    accounts.ensure_seed()
    life = lc_mod.Lifecycle(store, accounts)
    gov = governance.Governance(store)
    rights = rights_mod.RightsService(store, life, accounts)
    reporter = report_mod.Reporter(store, life, gov, rights)

    owner = accounts.authenticate("owner", "EDY_POWERQ", place="考务办",
                                  device="DEV-01")
    oper = accounts.authenticate("oper1", "oper1", place="考务办", device="DEV-01")
    inv = accounts.authenticate("inv01", "inv01", place="A-01 考场", device="DEV-02")
    auditor = accounts.authenticate("auditor1", "auditor1", place="审计室",
                                    device="DEV-03")

    # ============ 1. 数据分类分级 ============
    print("\n[1/10] 数据分类分级（第一节）")
    check("分类分级目录非空", len(catalog.CATEGORIES) >= 6,
          "%d 类" % len(catalog.CATEGORIES))
    ok, problems = catalog.validate()
    check("目录自洽性校验通过", ok, "；".join(problems) or "无问题")
    check("人脸特征模板为高敏感且要求加密",
          catalog.get("face_template").level == catalog.LEVEL_HIGH
          and "template_blob" in catalog.required_encrypted_fields("face_template"))
    check("人脸原始图像保留期 ≤ 30 天",
          (catalog.retention_days("face_image") or 999) <= 30,
          "%s 天" % catalog.retention_days("face_image"))
    check("参与自动销毁的类别已标注", len(catalog.auto_destroyable()) >= 3,
          "、".join(c.name for c in catalog.auto_destroyable()))

    # ============ 2. 加密与密钥 ============
    print("\n[2/10] 加密与密钥保护（第三节第 1 条）")
    alg = crypto.active_algorithm()
    check("存在可用加密后端", alg != "none", crypto.algorithm_text())
    plain = "330100200801011234"
    cipher = crypto.encrypt_field(plain)
    check("字段加密往返正确", crypto.decrypt_field(cipher) == plain)
    check("密文带 enc: 前缀", cipher.startswith("enc:"))
    check("字段加密幂等（重复加密不变）", crypto.encrypt_field(cipher) == cipher)
    check("空值安全", crypto.encrypt_field("") == "" and crypto.decrypt_field(None) is None)
    tpl1 = crypto.irreversible_template("feature-A")
    tpl2 = crypto.irreversible_template("feature-A")
    tpl3 = crypto.irreversible_template("feature-B")
    check("特征模板不可逆且可复现",
          tpl1 == tpl2 and tpl1 != tpl3 and tpl1.startswith("tpl1:"))
    check("密钥指纹可获取", crypto.key_fingerprint() != "-")
    from dpp import secure_store
    check("主密钥有平台安全包裹后端", secure_store.backend_name() != "none",
          secure_store.describe()["说明"])

    # ============ 3. 采集（单独同意硬前置） ============
    print("\n[3/10] 采集控制：单独同意硬前置（第二节「采集」）")
    cand = models.Candidate(name="张三", id_card_no="330100200801011234",
                            ticket_no="2026T0001", seat_no="01", room_id="A-01",
                            retention_days=30)
    # 3a) 无同意 -> 必须拒绝
    denied = False
    try:
        life.collect(oper, name="无同意考生", id_card_no="330100200801019999",
                     ticket_no="2026T9999", seat_no="99", room_id="A-01",
                     captured_epoch=models.now_epoch())
    except lc_mod.LifecycleError:
        denied = True
    check("无单独同意时采集被拒绝", denied)
    check("拒绝动作已写入审计链", store.audit_actions().get("采集", 0) >= 1)

    # 3b) 有同意 -> 允许，且模板不可逆、留图加密
    cs = models.Consent(cand_ref=cand.cand_ref, name="张三", scope="face",
                        method="考前告知书签署（电子）", operator="oper1")
    store.save_consent(cs)
    res = life.collect(oper, name="张三", id_card_no="330100200801011234",
                       ticket_no="2026T0001", seat_no="01", room_id="A-01",
                       site="一中考点", feature_vector="feature-张三",
                       face_image_bytes=b"\x00\x01\x02" * 200,
                       consent_id=cs.consent_id, retention_days=30,
                       cand_ref=cand.cand_ref)
    check("有单独同意时采集成功", bool(res["cand_ref"]))
    check("只落不可逆模板（不落可逆特征）", res["template"])
    check("原始图像加密落盘", res["image_encrypted"])
    row = store.raw_candidate_row(res["cand_ref"])
    check("库里姓名为密文", crypto.is_encrypted(row["name"]))
    check("库里证件号为密文", crypto.is_encrypted(row["id_card_no"]))
    check("库里座次为密文", crypto.is_encrypted(row["seat_no"]))
    check("库里图像路径为密文", crypto.is_encrypted(row["face_image_path"]))
    check("库里特征模板为不可逆摘要",
          str(row["face_template"]).startswith("tpl1:"))
    check("有效同意可被查到", store.valid_consent(res["cand_ref"]) is not None)

    # 3c) 采集帧率上限（最小必要）
    check("采集帧率按需最小化（≤ 8fps）", config.CAPTURE["fps_max"] <= 8,
          "%d fps" % config.CAPTURE["fps_max"])
    check("禁止静默开启采集", config.CAPTURE["silent_start"] is False)

    # ============ 4. 传输 ============
    print("\n[4/10] 传输控制：专网 + 加密（第二节「传输」）")
    ok1, msg1, ev1 = life.check_transport("https://10.0.0.5/api", encrypted=True,
                                          network="private")
    check("专网+加密：通过", ok1, msg1[:48])
    ok2, msg2, _ = life.check_transport("http://10.0.0.5/api", encrypted=False,
                                        network="public")
    check("明文+公网：拒绝", not ok2, msg2[:48])
    ok3, msg3, _ = life.check_transport("http://public.example.com/upload",
                                        encrypted=True, network="private")
    check("明文协议地址：拒绝", not ok3, msg3[:40])

    # ============ 5. 使用与解密查看 ============
    print("\n[5/10] 使用控制：目的限定 + 脱敏 + 水印（第二/三节）")
    r_ok = life.use(oper, res["cand_ref"], "作弊判定", "关联告警复核",
                    terminal="巡考终端-R7")
    check("合规用途（作弊判定）放行", r_ok["allowed"])
    bad = False
    try:
        life.use(oper, res["cand_ref"], "情绪分析", "生成情绪曲线",
                 terminal="巡考终端-R7")
    except lc_mod.LifecycleError:
        bad = True
    check("超范围用途（情绪分析）被拒绝", bad)
    bad2 = False
    try:
        life.use(oper, res["cand_ref"], "跨境传输", "上传境外云")
    except lc_mod.LifecycleError:
        bad2 = True
    check("禁止跨境传输", bad2)
    uses = store.list_uses(limit=1000)
    check("被拒使用同样留痕（可核查）",
          any(u["allowed"] == 0 for u in uses),
          "被拒 %d 条" % len([u for u in uses if u["allowed"] == 0]))

    # 默认脱敏
    cand_loaded = store.get_candidate(res["cand_ref"])
    deid = cand_loaded.display_deid()
    check("默认展示去标识化（不含姓名/证件号）",
          "张三" not in deid and "330100200801011234" not in deid, deid)
    check("证件号掩码正确",
          desensitize.mask_id_card("330100200801011234") == "3301**********1234",
          desensitize.mask_id_card("330100200801011234"))

    # 解密查看必须留痕 + 水印
    wm = desensitize.watermark_text("巡考员01", "巡考终端-R7")
    check("水印含操作人/时间/终端三联",
          "巡考员01" in wm and "巡考终端-R7" in wm)
    revealed = life.reveal_plain(oper if oper.can("reveal") else owner,
                                 res["cand_ref"], "复核手机告警需核对证件",
                                 terminal="巡考终端-R7")
    check("授权解密明文成功", revealed["id_card_no"] == "330100200801011234")
    check("解密查看已留痕", len(store.list_reveals()) >= 1)
    check("解密查看带水印", bool(revealed["watermark"]))

    no_reason = False
    try:
        life.reveal_plain(oper if oper.can("reveal") else owner, res["cand_ref"], "")
    except lc_mod.LifecycleError:
        no_reason = True
    check("无理由的解密查看被拒绝", no_reason)

    # ============ 6. 共享与导出 ============
    print("\n[6/10] 共享控制：必要字段 + 导出审批（第二节「共享」）")
    sh = life.request_share(owner, [res["cand_ref"]],
                            ["ref", "room_id", "seat_no"], direction="up",
                            reason="上级巡考汇总异常清单")
    check("合规字段的共享申请已登记", sh["status"] == "pending")
    leaked = False
    try:
        life.request_share(owner, [res["cand_ref"]], ["ref", "id_card_no"],
                           reason="想直接带证件号")
    except lc_mod.LifecycleError:
        leaked = True
    check("共享直接标识（证件号）被拒绝", leaked)
    leaked2 = False
    try:
        life.request_share(owner, [res["cand_ref"]], ["ref", "face_template"],
                           reason="带上特征模板方便比对")
    except lc_mod.LifecycleError:
        leaked2 = True
    check("共享生物特征被拒绝", leaked2)
    app = life.approve_share(owner, sh["req_id"], approve=True, export=True)
    check("审批通过并导出", app["status"] == "approved" and app["rows"] >= 1,
          "导出 %d 条" % app["rows"])
    check("导出动作已写审计", store.audit_actions().get("导出", 0) >= 1)
    # 导出件必须脱敏
    import json
    body = json.load(open(app["export_path"], encoding="utf-8"))
    check("导出件标记 masked=True", body.get("masked") is True)
    check("导出件不含证件号",
          "330100200801011234" not in json.dumps(body, ensure_ascii=False))

    # ============ 7. 越权拦截 ============
    print("\n[7/10] 最小权限与越权拦截（第三节第 4 条）")
    check("监考员不能解密查看", not inv.can("reveal"))
    check("监考员不能销毁", not inv.can("destroy"))
    check("监考员不能管理账户", not inv.can("manage"))
    check("监考员可见范围仅 A-01", inv.room_filter() == ["A-01"], str(inv.room_filter()))
    check("审计员不能登记考生", not auditor.can("register"))
    check("操作员不能审批导出", not oper.can("approve"))
    check("操作员不能管理账户", not oper.can("manage"))
    denied_ops = 0
    for sess, act in ((inv, "reveal"), (inv, "destroy"), (auditor, "register"),
                      (oper, "approve"), (oper, "manage"), (inv, "manage")):
        try:
            sess.require(act)
        except Exception:
            denied_ops += 1
    check("越权操作全部被拦截（6/6）", denied_ops == 6, "拦截 %d 条" % denied_ops)
    check("口令加盐存储（非明文）",
          "EDY_POWERQ" not in str(accounts.list_accounts().get("owner", {}).get("pwd", "")))
    check("错误口令被拒", accounts.authenticate("owner", "wrong") is None)
    check("不能新建同级或更高账户",
          not accounts.create("owner2", "Passw0rd!", config.ROLE_OWNER,
                              by_role=config.ROLE_OWNER)[0])
    check("非所有者不能建账户",
          not accounts.create("x1", "Passw0rd!", config.ROLE_VIEWER,
                              by_role=oper.role)[0])
    # 持续校验：高风险会话对高风险动作要求二次确认
    risky = accounts.authenticate("owner", "EDY_POWERQ", place="", device="",
                                  hour=3)
    blocked = False
    try:
        risky.require("reveal")
    except Exception:
        blocked = True
    check("高风险会话（深夜/陌生设备）要求二次确认", blocked,
          "风险级别=%s" % risky.risk_level())
    risky.confirm_risky()
    ok_after = False
    try:
        risky.require("reveal")
        ok_after = True
    except Exception:
        pass
    check("二次确认后放行", ok_after)

    # ============ 8. 权利响应 ============
    print("\n[8/10] 考生权利响应（第五节）")
    rq = rights.submit(oper, res["cand_ref"], "access",
                       detail="考生申请查阅本人数据", identity_verified=True)
    check("查阅申请已受理", rq["status"] == "open")
    check("查阅时限 5 个工作日", rq["due_days"] == 5)
    out = rights.handle(owner, rq["req_id"])
    check("查阅结果已办结", out["status"] == "done")
    check("披露内容为脱敏态",
          "*" in out["data"]["id_card_no"] and "*" in out["data"]["name"],
          "%s / %s" % (out["data"]["name"], out["data"]["id_card_no"]))

    rq2 = rights.submit(oper, res["cand_ref"], "correct",
                        detail="考生申请更正座次", identity_verified=True)
    out2 = rights.handle(owner, rq2["req_id"], corrected={"seat_no": "02"})
    # 只读策略：更正 = 删除原记录 + 新增一条更正后的记录（不是就地改写）
    new_ref = out2.get("new_cand_ref")
    after = store.get_candidate(new_ref) if new_ref else None
    check("更正以【删除原记录+新增】方式生效并留痕",
          after is not None and after.seat_no == "02"
          and store.get_candidate(res["cand_ref"]) is None,
          "新记录 %s 座次=%s" % (new_ref, after.seat_no if after else "—"))
    # 后续权利动作改用更正后的新记录
    res = {"cand_ref": new_ref} if new_ref else res

    # 撤回同意 -> 即时生效
    rq3 = rights.submit(oper, res["cand_ref"], "withdraw",
                        detail="考生现场提出撤回人脸同意", identity_verified=True)
    check("撤回同意即时办结", rq3["status"] == "done")
    check("撤回后同意不再有效", store.valid_consent(res["cand_ref"]) is None)
    st = rights.stats()
    check("权利申请统计可用", st["total"] >= 3,
          "共 %d 条，在办 %d，超期 %d" % (st["total"], st["open"], st["overdue"]))
    check("撤回同意时限为即时", config.RIGHTS_SLA_DAYS["withdraw"] == 0)

    # 法律保留 -> 删除被驳回
    hc = models.Candidate(name="法律保留考生", id_card_no="330100200801018888",
                          ticket_no="2026T8888", seat_no="88", room_id="A-01",
                          retention_days=30)
    store.save_candidate(hc)
    life.set_legal_hold(owner, hc.cand_ref, on=True, reason="涉刑事案件侦查需要")
    rq4 = rights.submit(oper, hc.cand_ref, "delete", detail="申请删除",
                        identity_verified=True)
    out4 = rights.handle(owner, rq4["req_id"])
    check("有法律保留必要时不删除并说明理由",
          out4["status"] == "rejected" and "法律保留" in out4["result"],
          out4.get("result", "")[:32])
    check("到期记录不被法律保留误删",
          len([c for c in store.expired_candidates()
               if c.cand_ref == hc.cand_ref]) == 0)

    # ============ 9. 到期自动销毁 ============
    print("\n[9/10] 到期自动销毁（第二/三节）")
    # 建图像文件后**一次性**登记（而不是登记后再改），以符合"加密数据只读"策略
    old_img = life._store_encrypted_image(b"\x11" * 300)
    old = models.Candidate(name="到期考生", id_card_no="330100200801017777",
                           ticket_no="2026T7777", seat_no="77", room_id="A-02",
                           face_image_path=old_img,
                           retention_days=30,
                           captured_epoch=models.now_epoch() - 40 * 86400)
    store.save_candidate(old)
    img_path = os.path.join(str(config.VAULT_DIR), old_img)
    before_exists = os.path.exists(img_path)
    dres = life.run_auto_destroy(operator="system")
    check("到期记录被自动销毁", dres["destroyed"] >= 1, "销毁 %d 条" % dres["destroyed"])
    check("销毁后记录已从库中移除", store.get_candidate(old.cand_ref) is None)
    check("销毁后图像文件已删除",
          before_exists and not os.path.exists(img_path))
    recs = store.list_destroys(mode="auto")
    check("生成自动销毁记录", len(recs) >= 1, "%d 条" % len(recs))
    check("销毁记录含覆盖清单",
          bool(recs and recs[0]["covered"] and "fields" in recs[0]["covered"]))
    check("销毁动作已写审计", store.audit_actions().get("销毁", 0) >= 1)
    check("法律保留记录未被销毁", store.get_candidate(hc.cand_ref) is not None)
    # 强制销毁需双人确认
    no_witness = False
    try:
        life.destroy_by_request(owner, hc.cand_ref, "误采需要立即删除", force=True)
    except lc_mod.LifecycleError:
        no_witness = True
    check("强制销毁缺见证人被拒绝", no_witness)
    fres = life.destroy_by_request(owner, hc.cand_ref, "误采需要立即删除",
                                   witness="数据保护负责人 dpo1", force=True)
    check("双人确认后可强制销毁", bool(fres["destroy_id"]))
    check("强制销毁记录 mode=force",
          any(d["mode"] == "force" for d in store.list_destroys(limit=100)))

    # ============ 10. 管理措施 / 替代通道 / 验收指标 ============
    print("\n[10/10] 管理措施 · 替代通道 · 验收指标（第四/六节）")
    gov.seed_default_measures(project_owner="项目组·张工", site_owner="考点考务主任")
    ms = gov.measure_summary()
    check("管理措施台账已建立", ms["total"] >= 5, "%d 项" % ms["total"])
    for m in store.list_measures():
        if m["kind"] in ("DPIA", "责任人"):
            gov.complete_measure(owner, m["measure_id"], evidence="已归档")

    cid = gov.add_channel("一中考点", "一中考点人工核验通道")
    check("替代通道已登记", len(store.list_channels()) >= 1)
    cs_before = gov.channel_stats()
    check("未演练前不计入可用", cs_before["usable"] == 0)
    gov.drill_channel(owner, cid, passed=True, note="撤掉人脸通道演练通过")
    cs_after = gov.channel_stats()
    check("演练通过后计入可用",
          cs_after["usable"] == 1 and cs_after["total"] >= 1,
          "可用 %d/%d" % (cs_after["usable"], cs_after["total"]))

    inc = gov.report_incident(owner, "S3", "值班终端误装第三方输入法",
                              detail="已卸载并留痕，未见数据外泄")
    check("安全事件已上报且 2 小时内", inc["ok"], "时延 %.3f 小时" % (inc["delay_hours"]))

    # 审计链完整性
    cok, cmsg, n = store.verify_chain()
    check("审计链完整", cok, cmsg)
    acts = store.audit_actions()
    for need in ("登录", "采集", "使用", "使用被拒", "解密查看", "共享申请",
                 "共享审批", "导出", "销毁", "权利申请", "替代通道演练", "安全事件"):
        check("审计覆盖「%s」" % need, acts.get(need, 0) >= 1,
              "%d 条" % acts.get(need, 0))

    acc = gov.acceptance(lifecycle=life, rights=rights)
    for it in acc["items"]:
        check("验收指标「%s」达标" % it["指标项"], it["达标"], it["实测"])

    # 篡改检测（真删一条再校验，必须能发现）
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("UPDATE audits SET detail='被篡改' WHERE seq=(SELECT MIN(seq) FROM audits)")
    conn.commit()
    conn.close()
    tok, tmsg, _ = store.verify_chain()
    check("审计链篡改可被发现", not tok, tmsg)

    # 报告导出
    out_files = reporter.export_all()
    check("验收报告已导出", os.path.exists(out_files["report"]))
    check("处理活动登记表已导出", os.path.exists(out_files["activities"]))
    check("销毁记录台账已导出", os.path.exists(out_files["destroys"]))

    print()
    print("=" * 74)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("自检结果：%d/%d 通过" % (passed, len(RESULTS)))
    if failed:
        print("未通过项：")
        for n, d in failed:
            print("  - %s %s" % (n, ("（%s）" % d) if d else ""))
    print("=" * 74)
    print(reporter.console_summary())
    print("=" * 74)

    if TMP and not args.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
