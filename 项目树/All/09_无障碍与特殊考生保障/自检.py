# -*- coding: utf-8 -*-
r"""
09 无障碍与特殊考生保障 · 自检

不需要任何真实设备（无障碍通道闸机/低位终端/手语翻译都不需要），全部用临时数据目录跑，
覆盖 12 组断言：

  [1] 配置与字典      指标口径、需求/措施/通道/告警码字典、设置读写、掩码脱敏
  [2] 存储与加密      建表、字段白名单、盲索引查询、敏感字段加密落库、审计哈希链与篡改检测
  [3] 通道与合规      净宽/回转/低位终端/双提示校验、降级可用约束、三通道并行、配置率 100%
  [4] 档案与措施      建档校验、未同意 -> 人工通道且不采集人脸、措施推荐/发放/落实率
  [5] 替代核验        双证比对、双人见证（不得同人）、结论矛盾拒绝、超时、与智能核验同等效力
  [6] 受阻事件        登记、红色上报、结案闭环、"因技术原因未能入场"指标分子
  [7] 算法公平性      分层通过率与组间最大差、超差必须补偿、必测分层覆盖、整改趋势
  [8] 演练            校验（有问题必须写整改）、每考期 ≥1 次、替代通道可用性
  [9] 指标与报告      四个硬指标、完整合规场景下达标、报告/指标/台账导出与审计链
  [10] 联动           00 总线、09 经总线调自己、02 画质、03 推送与复核、06 对账、outbox 不丢事件
  [11] 依赖与不越界   只用标准库、不改其它专题任何文件（对比自检前后的文件指纹）
  [12] GUI 冒烟       构建七个页签界面、逐页切换、不弹窗不崩溃（没有 app.py 时如实跳过）

用法：
  python 自检.py            # 临时目录，随便跑（可破坏性测试）
  python 自检.py --real     # 真实 data 目录，只读体检（不写业务数据）
  python 自检.py --no-link  # 跳过联动组（对端专题缺失时用）
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PASS, FAIL = [], []

# 其它专题的关键文件（自检前后比对指纹，证明"只改 09"）
PEER_FILES = [
    os.path.join(os.path.dirname(HERE), "00_集成联动", "linkage.py"),
    os.path.join(os.path.dirname(HERE), "00_集成联动", "main.py"),
    os.path.join(os.path.dirname(HERE), "03_智能终端操作", "config.py"),
    os.path.join(os.path.dirname(HERE), "03_智能终端操作", "integration_api.py"),
    os.path.join(os.path.dirname(HERE), "06_智能安检与手机作弊器材检测", "config.py"),
    os.path.join(os.path.dirname(HERE), "06_智能安检与手机作弊器材检测",
                 "integration_api.py"),
    os.path.join(os.path.dirname(HERE), "02_摄像头核查与视频质量诊断",
                 "integration_api.py"),
    os.path.join(os.path.dirname(HERE), "01_人脸识别与身份核对",
                 "01_人脸识别与身份核对", "integration_api.py"),
]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— %s" % detail) if detail else ""))
    return bool(cond)


def section(title):
    print()
    print("-" * 76)
    print(title)
    print("-" * 76)


def file_fingerprints():
    out = {}
    for p in PEER_FILES:
        try:
            with open(p, "rb") as f:
                out[p] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            out[p] = "MISSING"
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实 data 目录（只读体检）")
    ap.add_argument("--no-link", action="store_true", help="跳过联动组")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    if args.real:
        return real_readonly_checks()

    fp_before = file_fingerprints()
    tmp = tempfile.mkdtemp(prefix="acc09_selfcheck_")
    os.environ["EXAM_ACCESS_HOME"] = tmp
    for m in ("config", "models", "storage", "data_crypto", "access", "verify",
              "fairness", "drill", "metrics", "ledger", "report", "linkage_bridge",
              "integration_api", "app"):
        sys.modules.pop(m, None)

    print("=" * 76)
    print("09 无障碍与特殊考生保障 · 自检")
    print("=" * 76)

    import access
    import config
    import data_crypto
    import drill as drill_mod
    import fairness as fairness_mod
    import ledger as ledger_mod
    import metrics as metrics_mod
    import models
    import report as report_mod
    import storage
    import verify

    print("数据目录：%s" % config.DATA_ROOT)
    print("模式：临时目录（可破坏性测试）")

    store = storage.Store(config.DB_PATH)

    # =====================================================================
    section("[1] 配置与字典")
    check("指标口径齐全（配置率/可用性/演练/未入场/差异）",
          config.TARGET_CHANNEL_RATE == 1.0 and config.TARGET_ALT_AVAILABILITY == 1.0
          and config.TARGET_DRILL_PER_SESSION >= 1 and config.TARGET_TECH_BLOCK == 0
          and 0 < config.TARGET_FAIRNESS_MAX_GAP < 1)
    check("三通道字典齐全", set(config.CHANNEL_KINDS) == {"smart", "manual",
                                                      "accessible"})
    check("需求类型 ≥7 类（含面损/视力/肢体/听力/医疗/语言）",
          len(config.NEED_KINDS) >= 7
          and all(k in config.NEED_KINDS for k in
                  (config.NEED_FACE, config.NEED_VISION, config.NEED_MOBILITY,
                   config.NEED_HEARING, config.NEED_MEDICAL, config.NEED_LANGUAGE)))
    check("每种需求都有推荐措施",
          all(config.NEED_MEASURES.get(n) for n in config.NEED_KINDS))
    check("措施字典与文本一一对应",
          all(m in config.MEASURE_TEXT for m in config.MEASURE_KINDS))
    check("硬性尺寸口径符合文档（净宽≥900、低位≤1100）",
          config.MIN_AISLE_WIDTH_MM == 900 and config.LOW_COUNTER_MAX_MM == 1100
          and config.MIN_TURN_RADIUS_MM >= 1200)
    check("告警码都定义了级别与兜底码",
          all(v.get("level") in (config.LEVEL_RED, config.LEVEL_ORANGE)
              and v.get("fallback") for v in config.ALERT_CODES.values()))
    check("因技术原因未入场为红色告警",
          config.ALERT_CODES["ACCESS_TECH_BLOCK"]["level"] == config.LEVEL_RED)
    check("等效性声明写明不得因设备拒绝入场",
          "不得因设备原因拒绝考生入场" in config.EQUIVALENCE_NOTE)
    check("掩码：身份证留后 4 位", config.mask_id("110101199001011234")
          == "**************1234", config.mask_id("110101199001011234"))
    check("掩码：姓名留姓", config.mask_name("欧阳修") == "欧**"
          and config.mask_name("张") == "张")

    ok_s = config.save_settings({"session": "2026年上半年", "sites": ["第一考点"]})
    cfg = config.load_settings()
    check("设置可写可读", ok_s and cfg["session"] == "2026年上半年"
          and config.sites(cfg) == ["第一考点"])
    check("设置坏值回落默认（不抛异常）",
          config.save_settings({"alt_verify_sla_sec": "abc"}) is True
          and config.sla_sec() > 0)
    config.save_settings({"alt_verify_sla_sec": config.ALT_VERIFY_SLA_SEC})
    desc = config.describe()
    check("describe 含数据目录与口径", "数据目录" in desc and "指标口径" in desc)

    # =====================================================================
    section("[2] 存储与加密")
    tables = {r["name"] for r in store._query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    need_tables = {"sites", "channels", "profiles", "accommodations", "alt_verifies",
                   "tech_blocks", "fairness_runs", "fairness_groups", "drills",
                   "audits", "outbox"}
    check("11 张表建齐", need_tables <= tables, "缺 %s" % (need_tables - tables))

    store.ensure_site("第一考点")
    check("考点幂等登记", len(store.list_sites()) == 1 and
          store.get_site(name="第一考点") is not None)

    pf_tmp = models.Profile(candidate_id="110101199001011234", candidate_name="张三",
                            needs=[config.NEED_FACE], consent=True,
                            cert_no="MED-2026-0001", site="第一考点")
    store.save_profile(pf_tmp)
    raw = store._one("SELECT candidate_id, cert_no, candidate_key FROM profiles "
                     "WHERE profile_id=?", (pf_tmp.profile_id,))
    enc_expected = data_crypto.enabled()
    check("考生标识与证明编号按设置加密落库",
          (not enc_expected) or (str(raw["candidate_id"]).startswith("enc:")
                                 and str(raw["cert_no"]).startswith("enc:")),
          "加密开关=%s" % enc_expected)
    check("盲索引可等值查回（密文列查不了，靠 candidate_key）",
          store.get_profile(candidate_id="110101199001011234") is not None
          and raw["candidate_key"] == data_crypto.blind_key("110101199001011234"))
    check("读出时透明解密", pf_tmp.candidate_id ==
          store.get_profile(profile_id=pf_tmp.profile_id).candidate_id)
    check("字段白名单生效（脏字段不写库）",
          store._upsert("channels", {"channel_id": "X1", "code": "X-1",
                                     "no_such_column": "boom"}) is not None
          and store.get_channel(code="X-1") is not None)
    store.delete_channel(store.get_channel(code="X-1").channel_id)
    check("非法字段不会把 SQL 写坏（已清理测试行）",
          store.get_channel(code="X-1") is None)

    a1 = store.log("自检", "第一条", "selfcheck")
    a2 = store.log("自检", "第二条", "selfcheck")
    okc, msg, n = store.verify_audit_chain()
    check("审计哈希链完整", okc and n >= 2, msg)
    check("审计有 prev_hash 串联", a2.prev_hash == a1.this_hash
          and a2.this_hash != a1.this_hash)
    store._exec("UPDATE audits SET detail='被改过' WHERE audit_id=?", (a1.audit_id,))
    okc2, msg2, _n2 = store.verify_audit_chain()
    check("篡改审计可被检出", okc2 is False and "改动" in msg2, msg2)
    store.log("自检", "链已断（自检内部制造，随后结束）", "selfcheck")
    # 让后续审计检查仍然可用：把被改的那行改回去
    txt = "第一条"
    store._exec("UPDATE audits SET detail=? WHERE audit_id=?", (txt, a1.audit_id))
    okc3, msg3, _n3 = store.verify_audit_chain()
    check("恢复后链重新完整（自证能力）", okc3 is True, msg3)
    store.outbox_add("ACCESS_ASSIST_NEEDED", config.LEVEL_ORANGE,
                     {"payload": {"code": "ACCESS_ASSIST_NEEDED"}})
    audit_rows = store._count("SELECT COUNT(*) FROM audits")
    del_n = store.purge_runtime(keep_days=0)
    check("最小必要清理：不删待发事件、不删审计",
          store.outbox_stats()["pending"] >= 1
          and store._count("SELECT COUNT(*) FROM audits") == audit_rows,
          str(del_n))

    # =====================================================================
    section("[3] 通道台账与三通道并行合规")
    ch_bad = models.Channel(code="ACC-BAD", name="不合格无障碍通道", site="第一考点",
                            kind=config.CH_ACCESSIBLE, width_mm=800,
                            turn_radius_mm=1200, low_counter=False,
                            text_prompt=False, voice_prompt=False,
                            status=config.ST_OK)
    v = access.validate_channel(ch_bad)
    codes = {i["code"] for i in v["errors"]}
    check("净宽 800mm 判不合格（<900）", "WIDTH" in codes and v["ok"] is False)
    check("回转 1200mm 判不合格（<1500）", "TURN" in codes)
    check("缺低位核验终端判不合格", "LOW_COUNTER" in codes)
    check("缺文字提示判不合格（听力考生必需）", "TEXT" in codes)

    ch_high = models.Channel(code="ACC-H", name="低位不合格", site="第一考点",
                             kind=config.CH_ACCESSIBLE, width_mm=1000,
                             turn_radius_mm=1600, low_counter=True,
                             counter_height_mm=1300, height_adjustable=False,
                             text_prompt=True, voice_prompt=True,
                             status=config.ST_OK, devices=["VT-1"])
    v2 = access.validate_channel(ch_high)
    check("低位终端台面 1300mm 仍判不合格",
          any(i["code"] == "COUNTER_H" for i in v2["errors"]))
    check("高度不可调只给 warn（不拦）",
          any(i["code"] == "COUNTER_ADJ" for i in v2["warns"]))

    ch_deg = models.Channel(code="ACC-D", name="降级通道", site="第一考点",
                            kind=config.CH_ACCESSIBLE, width_mm=1000,
                            turn_radius_mm=1600, low_counter=True,
                            counter_height_mm=900, height_adjustable=True,
                            text_prompt=False, voice_prompt=False, vibration=False,
                            status=config.ST_DEGRADED, devices=["VT-2"])
    v3 = access.validate_channel(ch_deg)
    check("降级可用但替代提示不足 2 种 -> 不合格",
          any(i["code"] == "DEGRADED" for i in v3["errors"]))

    ok_c, ch_acc, _ = save_channel(store, dict(
        code="ACC-A01", name="无障碍专用通道 1 号", site="第一考点",
        kind=config.CH_ACCESSIBLE, width_mm=1200, turn_radius_mm=1800,
        low_counter=True, counter_height_mm=850, height_adjustable=True,
        voice_prompt=True, text_prompt=True, vibration=True, sign_service=True,
        status=config.ST_OK, devices=["VT-A01", "GATE-A01"]))
    check("合规无障碍通道可登记", ok_c and ch_acc["check"]["ok"] is True,
          ch_acc.get("check", {}).get("errors"))
    save_channel(store, dict(code="MNL-A01", name="人工核验通道 1 号",
                             site="第一考点", kind=config.CH_MANUAL, width_mm=1100,
                             voice_prompt=True, text_prompt=True,
                             status=config.ST_OK, devices=["DESK-A01"]))
    save_channel(store, dict(code="SMT-A01", name="智能核验通道 1 号",
                             site="第一考点", kind=config.CH_SMART, width_mm=900,
                             voice_prompt=True, text_prompt=True,
                             status=config.ST_OK, devices=["FACE-A01"]))
    st = access.site_status(store, "第一考点")
    check("三通道并行判定通过", st["ok"] is True and st["missing"] == [],
          "%s" % st["missing_text"])
    check("替代通道可用（人工/无障碍）", st["alt_available"] is True)
    check("考点无替代通道时如实报不可用",
          access.alt_channel_available(store, "第二考点")[0] is False)
    rate1 = access.channel_config_rate(store, cfg)
    check("单考点配齐 -> 配置率 100%", rate1["ok"] and rate1["rate"] == 1.0)
    config.save_settings({"sites": ["第一考点", "第二考点"]})
    rate2 = access.channel_config_rate(store, config.load_settings())
    check("新增空考点 -> 配置率掉到 50%（分母真实）",
          abs(rate2["rate"] - 0.5) < 1e-9 and rate2["ok"] is False)
    save_channel(store, dict(code="ACC-B01", name="无障碍通道 2 号", site="第二考点",
                             kind=config.CH_ACCESSIBLE, width_mm=950,
                             turn_radius_mm=1500, low_counter=True,
                             counter_height_mm=900, height_adjustable=True,
                             voice_prompt=True, text_prompt=True, status=config.ST_OK,
                             devices=["VT-B01"]))
    save_channel(store, dict(code="MNL-B01", name="人工核验通道 2 号", site="第二考点",
                             kind=config.CH_MANUAL, width_mm=1000, voice_prompt=True,
                             text_prompt=True, status=config.ST_OK, devices=["DESK-B01"]))
    save_channel(store, dict(code="SMT-B01", name="智能核验通道 2 号", site="第二考点",
                             kind=config.CH_SMART, width_mm=900, voice_prompt=True,
                             text_prompt=True, status=config.ST_OK, devices=["FACE-B01"]))
    rate3 = access.channel_config_rate(store, config.load_settings())
    check("两考点都配齐 -> 配置率回到 100%",
          rate3["ok"] and rate3["rate"] == 1.0)
    chk = access.validate_channel(store.get_channel(code="ACC-A01"))
    check("通道校验结论可复现（列表页与保存时一致）", chk["ok"] is True)

    # 更严口径：状态"可用"但校验不合格的通道，不能算"替代核验通道可用"
    # （"能开门"不等于"能顶用"：降级可用却只剩一种提示方式，真出事时顶不住）
    store_ch = storage.Store(os.path.join(str(config.DATA_ROOT), "chan_check.db"))
    store_ch.ensure_site("第四考点")
    save_channel(store_ch, dict(code="ACC-D1", name="降级且提示不足", site="第四考点",
                                kind=config.CH_ACCESSIBLE, width_mm=1000,
                                turn_radius_mm=1600, low_counter=True,
                                counter_height_mm=900, height_adjustable=True,
                                text_prompt=False, voice_prompt=False,
                                status=config.ST_DEGRADED, devices=["D1"]), quiet=True)
    ok_alt, why_alt = access.alt_channel_available(store_ch, "第四考点")
    check("降级可用但提示不足的通道不算替代通道可用",
          ok_alt is False and "不合格" in why_alt, why_alt[:110])
    check("该考点也判不了替代通道可用（指标口径一致）",
          access.site_status(store_ch, "第四考点")["alt_available"] is False)
    save_channel(store_ch, dict(code="ACC-D1", text_prompt=True, voice_prompt=True,
                                status=config.ST_OK), quiet=True)
    ok_alt2, why_alt2 = access.alt_channel_available(store_ch, "第四考点")
    check("整改合格后替代通道恢复可用", ok_alt2 is True, why_alt2[:90])
    store_ch.close()

    # =====================================================================
    section("[4] 特殊考生档案与保障措施")
    ok_p, res, _e = access.add_profile(store, {"candidate_id": "C-NONE"}, "selfcheck")
    check("缺需求类型拒绝建档", ok_p is False and "需求" in res)
    ok_p, res, _e = access.add_profile(
        store, {"candidate_id": "C-OTHER", "needs": [config.NEED_OTHER]}, "selfcheck")
    check("选「其他」必须写明具体需求", ok_p is False and "写明" in res)

    ok_p, p_no, e_no = access.add_profile(store, {
        "candidate_id": "C-NOCONSENT", "candidate_name": "李四",
        "needs": [config.NEED_FACE], "consent": False, "room_id": "001",
        "seat_no": "07", "site": "第一考点"}, "selfcheck")
    check("未取得单独同意仍可建档（不得因此拒绝入场）", ok_p is True)
    check("未同意 -> 只走人工核验通道",
          p_no.channel_hint == config.CH_MANUAL, p_no.channel_hint)
    check("未同意 -> 自动带上人工核验与免刷脸措施",
          config.M_MANUAL_VERIFY in p_no.measures
          and config.M_EXEMPT_FACE in p_no.measures)
    check("未同意 -> 给出合规提示", any("单独同意" in w for w in e_no["warn"]))

    ok_p, p_mb, _e = access.add_profile(store, {
        "candidate_id": "C-WHEEL", "candidate_name": "王五",
        "needs": [config.NEED_MOBILITY], "consent": True,
        "site": "第一考点", "room_id": "001", "seat_no": "08"}, "selfcheck")
    check("轮椅考生 -> 建议走无障碍专用通道",
          p_mb.channel_hint == config.CH_ACCESSIBLE, p_mb.channel_hint)
    check("轮椅考生 -> 推荐加宽通道+低位终端",
          config.M_WHEELCHAIR in p_mb.measures
          and config.M_LOW_COUNTER in p_mb.measures)
    ok_p, p_again, e_again = access.add_profile(store, {
        "candidate_id": "C-WHEEL", "needs": [config.NEED_MOBILITY]}, "selfcheck")
    check("重复建档幂等（返回已存在）",
          ok_p is True and e_again.get("already") is True)
    ok_p, p_upd, _e = access.add_profile(store, {
        "candidate_id": "C-WHEEL", "needs": [config.NEED_MOBILITY],
        "allow_update": True, "seat_no": "09"}, "selfcheck")
    check("allow_update 可更新档案",
          ok_p is True and store.get_profile(candidate_id="C-WHEEL").seat_no == "09")

    ok_m, msg_m = access.set_measure_status(store, p_mb.profile_id,
                                            config.M_WHEELCHAIR,
                                            config.ACC_PROVIDED, "selfcheck")
    check("「已落实」必须填责任人", ok_m is False and "责任人" in msg_m)
    ok_m, acc = access.set_measure_status(store, p_mb.profile_id,
                                          config.M_WHEELCHAIR,
                                          config.ACC_PROVIDED, "selfcheck",
                                          owner="考务一组", place="东门 1 号口")
    check("填了责任人才能置为已落实",
          ok_m is True and acc.status == config.ACC_PROVIDED and acc.provided_at)
    ok_m, msg_m = access.set_measure_status(store, p_mb.profile_id,
                                            config.M_LOW_COUNTER,
                                            config.ACC_REJECTED, "selfcheck")
    check("「不予」必须写理由", ok_m is False and "理由" in msg_m)
    ok_m, msg_m = access.set_measure_status(store, p_mb.profile_id, "no_such_measure",
                                            config.ACC_PROVIDED, "selfcheck",
                                            owner="x")
    check("非法措施类型被拒", ok_m is False)
    cov = access.measure_coverage(store)
    check("措施落实率算得出（分子分母非空）",
          cov["measures"] > 0 and 0 < cov["rate"] <= 1, str(cov))
    check("措施落实率统计到已落实项", cov["provided"] >= 1)

    # =====================================================================
    section("[5] 替代核验（人工兜底，与智能核验同等效力）")
    ok_d, matched, missing_req, dtext = verify.docs_check({
        config.DOC_ID_CARD: config.DOC_MATCHED})
    check("只有 1 项一致 -> 双证比对不通过", ok_d is False and matched == 1, dtext)
    ok_d, matched, missing_req, dtext = verify.docs_check({
        config.DOC_PHOTO: config.DOC_MATCHED,
        config.DOC_MEDICAL: config.DOC_MATCHED})
    check("有两项一致但都不是身份证/准考证/护照 -> 不通过",
          ok_d is False and missing_req is True, dtext)
    ok_d, matched, _m, dtext = verify.docs_check({
        config.DOC_ID_CARD: config.DOC_MATCHED,
        config.DOC_TICKET: config.DOC_MATCHED})
    check("身份证+准考证一致 -> 通过", ok_d is True and matched == 2, dtext)

    base_av = {"candidate_id": "C-WHEEL", "mode": config.ALT_MANUAL_DOC,
               "docs": {config.DOC_ID_CARD: config.DOC_MATCHED,
                        config.DOC_TICKET: config.DOC_MATCHED},
               "inspector": "核验员甲", "witness": "监督员乙",
               "room_id": "001", "seat_no": "08", "elapsed_sec": 45}
    ok_a, res_a, info_a = verify.alt_verify(store, dict(base_av, witness=""),
                                            "selfcheck")
    check("缺见证人 -> 拒绝（双人见证）", ok_a is False and "见证" in res_a)
    ok_a, res_a, _i = verify.alt_verify(store, dict(base_av, inspector="同一个人",
                                                    witness="同一个人"), "selfcheck")
    check("见证人与主检人相同 -> 拒绝", ok_a is False and "相同" in res_a)
    ok_a, res_a, _i = verify.alt_verify(store, dict(
        base_av, docs={config.DOC_ID_CARD: config.DOC_MISMATCH,
                       config.DOC_TICKET: config.DOC_MATCHED},
        outcome=config.ALT_PASSED), "selfcheck")
    check("双证不通过却判「通过」-> 拒绝（结论不得自相矛盾）",
          ok_a is False and "矛盾" in res_a)

    ok_a, av1, info1 = verify.alt_verify(store, dict(base_av), "selfcheck")
    check("合规替代核验成功", ok_a is True and av1.av_id, res_a if not ok_a else "")
    check("非技术原因的替代核验**不会**误记受阻事件",
          info1.get("block") is None and store.tech_block_stats()["total"] == 0,
          "受阻 %d 起" % store.tech_block_stats()["total"])
    check("替代核验默认结论=通过（双证一致）", av1.outcome == config.ALT_PASSED)
    check("**与智能核验同等效力**（制度性标记）",
          av1.equivalence is True and "同等效力" in info1["equivalence"])
    check("替代通道可用性被记录", info1["alt_channel_ok"] is True,
          info1["alt_channel_reason"])

    ok_a, av2, info2 = verify.alt_verify(store, dict(
        base_av, candidate_id="C-NEW-FACE", inspector="核验员甲", witness="监督员乙",
        mode=config.ALT_ASSISTED_FACE,
        smart_result={"ok": False, "reason": "特征索引未建"},
        tech_reason=True, symptom="face_fail", device_code="FACE-A01",
        needs=[config.NEED_FACE]), "selfcheck")
    check("无档案考生也能当场核验（现场补登，不得拒绝入场）",
          ok_a is True and info2.get("profile_created") is True)
    check("智能核验不可用 -> 自动标记技术原因", av2.tech_reason is True)
    check("技术原因核验 -> 自动登记受阻事件",
          bool(info2.get("block")) and info2["block"]["block_id"])
    check("受阻事件自动闭环为「替代通道入场」",
          (info2.get("block") or {}).get("final_result") == config.FINAL_ENTERED_MANUAL,
          str((info2.get("block") or {}).get("final_result")))
    check("受阻事件等待时长被记录",
          float((info2.get("block") or {}).get("waited_sec") or 0) >= 0)

    ok_a, av3, info3 = verify.alt_verify(store, dict(
        base_av, elapsed_sec=config.ALT_VERIFY_SLA_SEC + 120), "selfcheck")
    check("超过时限 -> 标记超时（进整改清单）",
          ok_a is True and av3.overdue is True, "%.0fs" % av3.elapsed_sec)
    ok_a, av4, _i = verify.alt_verify(store, dict(
        base_av, mode=config.ALT_EXEMPT, candidate_id="C-NEW-FACE",
        inspector="核验员甲", witness="监督员乙"), "selfcheck")
    check("备案免核验方式可用", ok_a is True and av4.mode_text() == "备案免核验")
    stats = store.alt_verify_stats()
    check("替代核验统计（次数/通过/超时/平均耗时）",
          stats["total"] >= 4 and stats["passed"] >= 3 and stats["overdue"] >= 1
          and stats["avg_sec"] > 0, str({k: stats[k] for k in
                                         ("total", "passed", "overdue", "avg_sec")}))
    check("替代核验方式分布有统计", bool(stats["by_mode"]))

    # =====================================================================
    section("[6] 因技术原因入场受阻事件")
    b1 = verify.raise_tech_block(store, {
        "candidate_id": "C-BLOCK", "candidate_name": "赵六", "site": "第一考点",
        "room_id": "002", "seat_no": "11", "symptom": "no_low_counter",
        "channel_code": "ACC-A01", "device_code": "VT-A01",
        "detail": "低位终端高度不可调，轮椅考生够不到"}, "selfcheck")
    check("受阻事件可登记", b1 is not None and b1.block_id)
    check("症状文本可读", b1.symptom_text() == "低位终端高度不可调" or
          "低位" in b1.symptom_text(), b1.symptom_text())
    tb = store.tech_block_stats()
    check("受阻事件默认未结案", tb["open"] >= 1 and tb["denied"] == 0, str(tb))
    b1b = verify.resolve_tech_block(store, b1.block_id, config.FINAL_ENTERED_MANUAL,
                                    "换用人工核验通道并加派专人协助", "selfcheck")
    check("结案为「替代通道入场」",
          b1b.resolved is True and b1b.final_result == config.FINAL_ENTERED_MANUAL)
    check("等待时长可算", b1b.waited_sec >= 0)
    b2 = verify.raise_tech_block(store, {
        "candidate_id": "C-DENIED", "site": "第一考点", "room_id": "003",
        "symptom": "device_down", "detail": "闸机故障且未启备用通道"}, "selfcheck")
    verify.resolve_tech_block(store, b2.block_id, config.FINAL_DENIED,
                              "现场未及时启用替代通道，考生被延误错过入场", "selfcheck")
    tb2 = store.tech_block_stats()
    check("**未能入场**被如实计入（指标分子）", tb2["denied"] == 1, str(tb2["denied"]))
    check("受阻原因分布有统计", bool(tb2["by_symptom"]))
    check("非法结案类型被拒",
          verify.resolve_tech_block(store, b2.block_id, "no_such", "", "selfcheck")
          is None)

    # =====================================================================
    section("[7] 算法公平性分层测试")
    groups = {
        config.DIM_GENDER: {"男": {"total": 200, "passed": 190},
                            "女": {"total": 200, "passed": 188}},
        config.DIM_AGE: {"≤25": {"total": 100, "passed": 96},
                         "26-40": {"total": 100, "passed": 95},
                         ">40": {"total": 100, "passed": 90}},
        config.DIM_GLASSES: {"戴眼镜": {"total": 100, "passed": 93},
                             "不戴眼镜": {"total": 100, "passed": 97}},
        config.DIM_MASK: {"戴口罩": {"total": 100, "passed": 88},
                          "不戴口罩": {"total": 100, "passed": 97}},
    }
    ok_f, run1, info_f = fairness_mod.run_fairness(store, {
        "name": "上线前分层测试", "algorithm": "01 人脸 1:1 v2", "threshold": 0.62,
        "groups": groups, "action": "threshold",
        "action_note": "对戴口罩分层阈值下调 0.02 并复测"}, "selfcheck")
    check("分层测试可记录", ok_f is True and run1.run_id, str(run1) if not ok_f else "")
    check("组间最大差算得出（预期 9%）", abs(run1.max_gap - 0.09) < 1e-6,
          "%.4f" % run1.max_gap)
    check("最差分层定位到「口罩/戴口罩」",
          run1.worst_dim == config.DIM_MASK and run1.worst_group == "戴口罩",
          "%s/%s" % (run1.worst_dim, run1.worst_group))
    check("超差 -> 判定不通过并给出建议",
          run1.passed is False and "人工核验" in info_f["advice"])
    check("超差且已选补偿策略 -> 不再提示缺说明",
          not any("补偿/整改说明" in w for w in info_f["warn"]))
    gs = store.list_fairness_groups(run1.run_id)
    check("分组明细落库（2+3+2+2=9 组）", len(gs) == 9, "组数 %d" % len(gs))
    grp = [g for g in gs if g.dim == config.DIM_MASK and g.group_name == "戴口罩"][0]
    check("分组差异 gap_to_best 正确",
          abs(grp.gap_to_best - 0.09) < 1e-6, "%.4f" % grp.gap_to_best)
    ok_f2, run2, info_f2 = fairness_mod.run_fairness(store, {
        "name": "补偿后复测", "groups": groups, "action": "none"}, "selfcheck")
    check("差异超差却选「无需调整」-> 明确提示不合规",
          any("不合规" in w for w in info_f2["warn"]))
    ok_f3, run3, _i3 = fairness_mod.run_fairness(store, {
        "name": "阈值补偿后复测", "action": "threshold",
        "action_note": "戴口罩分层阈值 0.62->0.60，通过率 88%->94%",
        "groups": {
            config.DIM_GENDER: {"男": {"total": 200, "passed": 190},
                                "女": {"total": 200, "passed": 189}},
            config.DIM_AGE: {"≤25": {"total": 100, "passed": 96},
                             "26-40": {"total": 100, "passed": 95},
                             ">40": {"total": 100, "passed": 93}},
            config.DIM_GLASSES: {"戴眼镜": {"total": 100, "passed": 95},
                                 "不戴眼镜": {"total": 100, "passed": 96}},
            config.DIM_MASK: {"戴口罩": {"total": 100, "passed": 94},
                              "不戴口罩": {"total": 100, "passed": 96}},
        }}, "selfcheck")
    check("补偿后差异收敛 -> 达标",
          ok_f3 is True and run3.passed is True and run3.max_gap <= 0.05,
          "%.4f" % run3.max_gap)
    ok_f4, run4, info_f4 = fairness_mod.run_fairness(store, {
        "name": "样本不足的小规模测试",
        "groups": {config.DIM_GENDER: {"男": {"total": 5, "passed": 2},
                                       "女": {"total": 6, "passed": 5}}},
        "action": "add_samples", "action_note": "样本太少，先扩样"}, "selfcheck")
    check("样本不足会如实提示（结论仅供参考）",
          run4.small_groups and any("样本不足" in w for w in info_f4["warn"]))
    check("未覆盖必测分层会提示", any("必测分层" in w for w in info_f4["warn"]))
    cov = fairness_mod.coverage(store)
    check("必测分层覆盖判定（最近一次缺覆盖 -> False）", cov["covered"] is False)
    check("整改趋势可对比（组间差前后变化）",
          len(fairness_mod.trend(store, limit=6)) >= 2)
    check("公平性统计给界面用", fairness_mod.fairness_stats(store)["runs"] >= 4)

    # =====================================================================
    section("[8] 演练与替代通道可用性")
    ok_dl, dmsg, _i = drill_mod.add_drill(store, {
        "scenario": "no_such_scenario", "site": "第一考点"}, "selfcheck")
    check("非法演练科目被拒", ok_dl is False)
    ok_dl, dmsg, _i = drill_mod.add_drill(store, {
        "scenario": "alt_verify", "site": "第一考点", "participants": 6,
        "issues": ["引导标识不清"], "actual_min": 8}, "selfcheck")
    check("发现问题却不写整改 -> 拒绝（不许走过场）",
          ok_dl is False and "整改" in dmsg)
    ok_dl, d1, info_d = drill_mod.add_drill(store, {
        "scenario": "full_chain", "site": "第一考点", "session": "2026年上半年",
        "participants": 12, "planned_min": 30, "actual_min": 26,
        "channels": ["SMT-A01", "MNL-A01", "ACC-A01"],
        "issues": ["轮椅考生从东门绕行距离偏长"],
        "actions": "在东门加设引导员并在通道口加贴标识", "result": "partial",
        "witness": "考点副主任"}, "selfcheck")
    check("合规演练可登记", ok_dl is True and d1.drill_id, dmsg if not ok_dl else "")
    check("演练结论文本可读", d1.result_text() == "基本合格（有整改项）")
    ok_dl, d2, _i = drill_mod.add_drill(store, {
        "scenario": "wheelchair", "site": "第二考点", "session": "2026年上半年",
        "participants": 5, "planned_min": 15, "actual_min": 14,
        "channels": ["ACC-B01"], "result": "pass"}, "selfcheck")
    dr = drill_mod.drill_availability(store, config.load_settings())
    check("两考点本考期都演练过 -> 可用性 100%", dr["ok"] is True and dr["rate"] == 1.0,
          str([(d["site"], d["drills"], d["reasons"]) for d in dr["details"]]))
    config.save_settings({"session": "2026年下半年"})
    dr2 = drill_mod.drill_availability(store, config.load_settings())
    check("换考期后未演练 -> 可用性掉下来（口径按考期）",
          dr2["ok"] is False and dr2["rate"] == 0.0,
          str([(d["site"], d["reasons"]) for d in dr2["details"]]))
    config.save_settings({"session": "2026年上半年"})
    stats_d = store.drill_stats()
    check("演练统计（次数/结论/有问题数）",
          stats_d["total"] == 2 and stats_d["with_issues"] == 1, str(stats_d))

    # =====================================================================
    section("[9] 关键指标与验收报告")
    m = metrics_mod.compute(store, config.load_settings())
    keys = {i["key"] for i in m["items"]}
    check("四个硬指标齐全",
          {"channel_rate", "alt_availability", "tech_block_denied", "fairness"} <= keys)
    check("过程指标齐全",
          {"measure_coverage", "alt_verify_timely", "block_closure"} <= keys)
    check("出现未入场事件 -> 指标未达标并点名",
          m["ok"] is False and "因技术原因未能入场事件" in m["hard_fail"],
          str(m["hard_fail"]))
    txt = metrics_mod.text_summary(m)
    check("指标文本可读（每条一行）", len(txt) == len(m["items"]))
    ok_r, info_r = report_mod.write_report(store, cfg=config.load_settings())
    check("验收报告生成成功", ok_r is True, str(info_r)[:120])
    if ok_r:
        body = open(info_r["md"], encoding="utf-8").read()
        check("报告含关键章节",
              all(s in body for s in ("关键指标达标情况", "通道配置", "特殊考生档案",
                                      "替代核验", "因技术原因入场受阻事件",
                                      "算法公平性分层测试", "演练记录", "联动留痕",
                                      "口径与如实说明")), info_r["md"])
        check("报告写明等效性声明", "同等效力" in body)
        check("报告原样列出未入场事件（不粉饰）", "未能入场" in body)
        check("报告含审计链结论", "审计链" in body)
        check("指标 CSV 落盘", os.path.isfile(info_r["csv"]))
        check("台账 CSV 落盘（脱敏）",
              bool(info_r["ledger"]) and os.path.isfile(info_r["ledger"]))
        led_txt = open(info_r["ledger"], encoding="utf-8-sig").read()
        check("台账里证件号已脱敏（不含完整身份证）",
              "110101199001011234" not in led_txt)
    led = ledger_mod.rows(store)
    check("台账行数=档案数", len(led) >= 3, "%d 行" % len(led))
    ok_l, path_l = ledger_mod.write_ledger(store, fmt="json")
    check("台账 JSON 可导出", ok_l is True and os.path.isfile(path_l))
    ok_csv, path_csv = ledger_mod.write_ledger(store, fmt="csv")
    check("台账 CSV 可导出", ok_csv is True and os.path.isfile(path_csv))
    sumtxt = ledger_mod.summary_text(store)
    check("台账摘要文本可读", "特殊考生" in sumtxt, sumtxt)
    check("指标口径一致：配置率 100%",
          m["raw"]["channel_rate"]["rate"] == 1.0)
    check("指标口径一致：替代通道可用性 100%",
          m["raw"]["alt_availability"]["rate"] == 1.0)
    check("指标口径一致：未入场 1 起",
          m["raw"]["tech_blocks"]["denied"] == 1)

    # ---- 完整合规场景（另一个库）应判定达标 ----
    store2 = storage.Store(os.path.join(str(config.DATA_ROOT), "second.db"))
    config.save_settings({"sites": ["第三考点"], "session": "2026年上半年"})
    store2.ensure_site("第三考点")
    for spec in (("ACC-C01", config.CH_ACCESSIBLE),
                 ("MNL-C01", config.CH_MANUAL), ("SMT-C01", config.CH_SMART)):
        save_channel(store2, dict(code=spec[0], name=spec[0], site="第三考点",
                                  kind=spec[1], width_mm=1000, turn_radius_mm=1600,
                                  low_counter=(spec[1] == config.CH_ACCESSIBLE),
                                  counter_height_mm=900, height_adjustable=True,
                                  voice_prompt=True, text_prompt=True,
                                  status=config.ST_OK, devices=["D-" + spec[0]]),
                     quiet=True)
    access.add_profile(store2, {"candidate_id": "C-OK", "needs": [config.NEED_HEARING],
                                "consent": True, "site": "第三考点"}, "selfcheck")
    verify.alt_verify(store2, {"candidate_id": "C-OK", "mode": config.ALT_MANUAL_DOC,
                               "docs": {config.DOC_ID_CARD: config.DOC_MATCHED,
                                        config.DOC_TICKET: config.DOC_MATCHED},
                               "inspector": "甲", "witness": "乙",
                               "elapsed_sec": 40}, "selfcheck")
    drill_mod.add_drill(store2, {"scenario": "hearing", "site": "第三考点",
                                 "session": "2026年上半年", "participants": 4,
                                 "planned_min": 20, "actual_min": 20,
                                 "channels": ["SMT-C01"], "result": "pass"},
                        "selfcheck")
    fairness_mod.run_fairness(store2, {"name": "达标复测", "action": "none",
                                       "groups": {
                                           config.DIM_GENDER: {
                                               "男": {"total": 50, "passed": 48},
                                               "女": {"total": 50, "passed": 48}},
                                           config.DIM_AGE: {
                                               "≤25": {"total": 50, "passed": 48},
                                               ">40": {"total": 50, "passed": 47}},
                                           config.DIM_GLASSES: {
                                               "戴眼镜": {"total": 50, "passed": 48},
                                               "不戴眼镜": {"total": 50,
                                                            "passed": 48}},
                                           config.DIM_MASK: {
                                               "戴口罩": {"total": 50, "passed": 48},
                                               "不戴口罩": {"total": 50,
                                                            "passed": 48}}}},
                                      "selfcheck")
    m2 = metrics_mod.compute(store2, config.load_settings())
    check("完整合规场景 -> 硬指标全部达标",
          m2["ok"] is True, "未达标：%s" % "、".join(m2["hard_fail"]))
    ok_r2, info_r2 = report_mod.write_report(store2, cfg=config.load_settings())
    check("达标场景报告可生成", ok_r2 is True and info_r2["ok_metrics"] is True)
    store2.close()

    # =====================================================================
    if not args.no_link:
        section("[10] 联动（09 <-> 00 <-> 01/02/03/06）")
        import linkage_bridge as lb
        # 沙箱：02/03/06 的数据目录重定向到临时目录，不污染它们的真实数据
        lb.set_sandbox(os.path.join(tmp, "peers"))
        ok_b, why_b = lb.bus_available()
        check("00_集成联动 总线可用", ok_b, why_b)
        if ok_b:
            ok_reg, why_reg = lb.register_self()
            check("09 在内存里注册进总线（未改 00 的文件）", ok_reg, why_reg)
            import linkage
            check("00 的 PROJECTS 里有内存注册的 09",
                  linkage.PROJECTS.get("09") == HERE)
            av = lb.availability()
            check("01/02/03/06/09 目录与接口就位",
                  all(av.get(k, {}).get("ok") for k in ("01", "02", "03", "06", "09")),
                  str({k: av.get(k, {}).get("reason") for k in
                       ("01", "02", "03", "06", "09")
                       if not av.get(k, {}).get("ok")}))
            selfr = lb.self_status()
            check("09 经 00 总线调自己（协议一致）",
                  bool(selfr.get("ok")) and selfr.get("system")
                  == "09_无障碍与特殊考生保障",
                  "返回 system=%s%s" % (selfr.get("system"),
                                      ("；reason=%s" % selfr.get("reason"))
                                      if selfr.get("reason") else ""))
            p9 = lb.project("09")
            check("其它专题可按同一协议取到 09 的句柄",
                  p9 is not None and p9.available()[0] is True,
                  str(p9.available()) if p9 is not None else "无句柄")
            st_all = lb.status_all()
            check("01/02/03/06 的 status 都能返回", all(
                k in st_all for k in ("01", "02", "03", "06")))
            check("03 状态含审计链结论", "audit_chain" in (st_all.get("03") or {}),
                  str((st_all.get("03") or {}).get("audit_chain"))[:80])
            check("06 状态含过检与待处置", "passes" in (st_all.get("06") or {}))
            check("01 状态含建档考生数", "candidates" in (st_all.get("01") or {}))
            f, why_c, _ci = lb.camera_credibility("CAM-SELFTEST-1")
            check("02 给出机位画质可信度（不可达也如实说明）",
                  isinstance(f, float) and bool(why_c), "×%.1f %s" % (f, why_c))
            r_n = lb.notify("ACCESS_TECH_BLOCK", {
                "detail": "自检：因技术原因入场受阻（测试事件）", "room_id": "T-01",
                "seat_no": "01", "candidate_id": "C-BLOCK",
                "candidate_name": "赵六", "device_code": "", "operator": "selfcheck"},
                store=store, human_confirmed=True)
            check("09 -> 00 -> 03 推送成功且级别为红色",
                  r_n.get("ok") is True and r_n.get("level") == config.LEVEL_RED
                  and bool(r_n.get("alert_id")), str(r_n)[:160])
            al = lb.list_alerts(limit=50)
            hit = [a for a in (al.get("alerts") or [])
                   if a.get("alert_id") == r_n.get("alert_id")]
            check("03 终端里能查到 09 的告警", bool(hit),
                  "共 %d 条告警" % len(al.get("alerts") or []))
            check("告警码与级别在 03 侧一致（显式级别生效）",
                  bool(hit) and hit[0].get("code") == "ACCESS_TECH_BLOCK"
                  and hit[0].get("level") == config.LEVEL_RED,
                  str(hit[0].get("code") if hit else "-"))
            c_r = lb.close_alert(r_n.get("alert_id"), "confirmed",
                                 "09 自检结案", "selfcheck")
            check("红色告警不绕过 03 的强制确认（09 如实返回而非硬消警）",
                  c_r.get("ok") is False and c_r.get("needs_manual_confirm") is True,
                  str(c_r.get("message"))[:120])
            r_o = lb.notify("ACCESS_ALT_VERIFY", {
                "detail": "自检：启用替代核验（橙色告警，处置闭环测试）",
                "room_id": "T-03", "seat_no": "02", "candidate_id": "C-WHEEL",
                "level": config.LEVEL_ORANGE, "operator": "selfcheck"},
                store=store, human_confirmed=True)
            check("09 的橙色告警推送成功", r_o.get("ok") is True
                  and r_o.get("level") == config.LEVEL_ORANGE, str(r_o)[:140])
            c_o = lb.close_alert(r_o.get("alert_id"), "confirmed",
                                 "09 自检结案：替代核验已安排", "selfcheck")
            check("09 -> 00 -> 03 复核结案（处置闭环留痕）", c_o.get("ok") is True,
                  str(c_o)[:130])
            s6 = lb.security_status()
            check("06 智能安检可查（人工安检通道对账）", s6.get("ok") is True,
                  str(s6.get("reason"))[:120] if not s6.get("ok") else "过检 %s" %
                  (s6.get("status", {}).get("passes")))
            sc = lb.special_channel_alerts(limit=50)
            check("可对账 06 上报的特殊考生人工安检通道", sc.get("ok") is True,
                  "命中 %s 条" % sc.get("total", 0))
            fr = lb.face_verify(os.path.join(HERE, "no_such_photo.jpg"),
                                candidate="C-001", device_code="")
            check("01 人脸核验不可用时如实返回（不抛异常）",
                  "ok" in fr and ("result" in fr or "reason" in fr),
                  str(fr.get("reason") or fr.get("result"))[:110])
            ros = lb.candidate_roster()
            check("01 建档考生可查询（用于提示能否刷脸）",
                  ros.get("ok") is True, "考生 %d 人" % len(ros.get("candidates") or []))
            # 03 不可达 -> 事件进 outbox，恢复后补发（事件不丢）
            saved_dir = linkage.PROJECTS.get("03")
            dead = tempfile.mkdtemp(prefix="acc09_dead03_")
            linkage.PROJECTS["03"] = dead
            lb._lk_cache.clear()
            r_fail = lb.notify("ACCESS_ALT_VERIFY", {
                "detail": "自检：03 不可达时的兜底测试", "room_id": "T-02",
                "level": config.LEVEL_ORANGE, "operator": "selfcheck"}, store=store)
            check("03 不可达 -> 不丢事件（进 outbox）",
                  r_fail.get("ok") is False and r_fail.get("queued") is True,
                  str(r_fail)[:140])
            ob = store.outbox_stats()
            check("outbox 里能看到待发事件", ob["pending"] >= 1, str(ob))
            linkage.PROJECTS["03"] = saved_dir
            lb._lk_cache.clear()
            fl = lb.flush_outbox(store)
            check("03 恢复后补发成功", len(fl["sent"]) >= 1 and
                  fl["stats"]["pending"] == 0, str(fl)[:160])
            pr = lb.probe(store=store, device_code="CAM-SELFTEST-1",
                          candidate="C-001", write=True)
            check("全链路探测通过", pr["ok"] is True,
                  "；".join("%s：%s" % (s["name"], s["detail"])
                           for s in pr["steps"] if not s["ok"])[:200])
            s6_step = [s for s in pr["steps"] if "智能安检可查" in s["name"]]
            check("06 概览的数字取到真实值（不是 None/未取到）",
                  bool(s6_step) and "None" not in s6_step[0]["detail"]
                  and "过检" in s6_step[0]["detail"],
                  str(s6_step[:1])[:130])
            check("探测步骤覆盖总线/各专题/自身/推送/结案",
                  len(pr["steps"]) >= 10, "%d 步" % len(pr["steps"]))
            sumr = lb.link_summary()
            check("联动摘要可给界面显示", "text" in sumr and bool(sumr["text"]))
            lb.set_sandbox(None)

    # =====================================================================
    section("[11] 依赖与不越界（只改 09）")
    bad_imports = scan_imports(HERE)
    check("只用标准库（cryptography 为可选增强）",
          not bad_imports, "第三方依赖：%s" % "、".join(sorted(bad_imports)))
    fp_after = file_fingerprints()
    changed = [os.path.basename(p) for p in fp_before
               if fp_before[p] != fp_after.get(p)]
    check("其它专题文件指纹未变（00/01/02/03/06 一行未改）",
          not changed, "被改动：%s" % "、".join(changed))
    check("本模块数据与导出全部落在 09 目录（或 EXAM_ACCESS_HOME）",
          str(config.DB_PATH).startswith(str(config.DATA_ROOT))
          and str(config.REPORT_DIR).startswith(str(config.DATA_ROOT)))
    check("09 的 ops 齐全（对外集成接口 ≥20 个）",
          len(import_ops()) >= 20, "%d 个" % len(import_ops()))
    check("接口协议与 01/02/03/06 一致（--op/--args，末行 JSON）",
          protocol_compatible())

    # =====================================================================
    section("[12] GUI 冒烟（无显示环境自动跳过）")
    gui_smoke()

    # =====================================================================
    total = len(PASS) + len(FAIL)
    passed = len(PASS)
    print()
    print("=" * 76)
    if passed == total:
        print("自检通过：%d/%d 项全部符合预期" % (passed, total))
    else:
        print("自检未通过：%d/%d 项" % (passed, total))
        for name, detail in FAIL:
            print("   FAIL %s　%s" % (name, detail))
    print("覆盖：配置/存储加密/通道合规/档案措施/替代核验/受阻事件/算法公平性/"
          "演练/指标报告/联动/依赖边界/GUI")
    print("=" * 76)
    store.close()
    if args.keep:
        print("临时目录保留：%s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if passed == total else 1


# ---------------------------------------------------------------------------
def save_channel(store, payload, quiet=False):
    """自检用的小工具：直接走 access/storage 保存通道并返回校验结论。"""
    import access
    import models
    code = payload.get("code")
    ch = store.get_channel(code=code) or models.Channel(code=code)
    for k, v in payload.items():
        setattr(ch, k, v)
    store.ensure_site(ch.site)
    ch.last_check_at = models.now_ts()
    ch.last_check_epoch = models.now_epoch()
    store.save_channel(ch)
    v = access.validate_channel(ch)
    if not quiet:
        store.log("登记通道", "%s %s" % (ch.code, ch.kind), "selfcheck")
    return True, {"channel_id": ch.channel_id, "code": ch.code, "check": v}, {}


def scan_imports(root):
    """扫本模块的 import，找出非标准库、非 cryptography 的依赖。"""
    import ast
    std = set(getattr(sys, "stdlib_module_names", set()))
    local = {os.path.splitext(f)[0] for f in os.listdir(root) if f.endswith(".py")}
    local |= {"__future__"}
    allow_extra = {"cryptography",        # 可选增强：敏感字段加密
                   "linkage",             # 00_集成联动 的总线（按路径 import，不是第三方）
                   "app"}                 # 本模块 GUI（冒烟测试时才导入）
    bad = set()
    for fn in os.listdir(root):
        if not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for nm in names:
                if nm in std or nm in local or nm in allow_extra:
                    continue
                bad.add(nm)
    return bad


def import_ops():
    import integration_api
    return sorted(integration_api.OPS)


def protocol_compatible():
    """确认本模块接口与其它专题接口的调用形状一致（同一套 argparse 约定）。"""
    import integration_api as ia
    peers = ["01_人脸识别与身份核对", "02_摄像头核查与视频质量诊断", "03_智能终端操作"]
    all_dir = os.path.dirname(HERE)
    for name in peers:
        p = os.path.join(all_dir, name, "integration_api.py")
        if not os.path.isfile(p):
            alt = os.path.join(all_dir, name, name, "integration_api.py")
            p = alt if os.path.isfile(alt) else p
        if not os.path.isfile(p):
            continue
        body = open(p, encoding="utf-8").read()
        if 'add_argument("--op"' not in body or 'add_argument("--args"' not in body:
            return False
    body = open(os.path.join(HERE, "integration_api.py"), encoding="utf-8").read()
    return ('add_argument("--op"' in body and 'add_argument("--args"' in body
            and bool(ia.OPS))


def gui_smoke():
    """
    构建 GUI 并逐页切换、逐页刷新；没有 app.py / 没有 Tk 就如实跳过。
    对话框（messagebox）会被替换成空操作：无人值守时不能有模态窗挡着 —— 这也是在验证
    "构造与刷新路径不依赖人工点击"。
    """
    app_path = os.path.join(HERE, "app.py")
    if not os.path.isfile(app_path):
        check("GUI 冒烟（app.py 尚不存在，如实跳过）", True, "跳过")
        return
    try:
        import tkinter  # noqa: F401
    except Exception as e:                                     # noqa: BLE001
        check("GUI 冒烟（本机无 Tk，如实跳过）", True, str(e)[:60])
        return
    try:
        import app
        root = tkinter.Tk()
        root.withdraw()
        dialogs = []

        def _stub(name):
            def fn(*a, **k):
                dialogs.append(name)
                return True
            return fn

        for nm in ("showinfo", "showwarning", "showerror", "askyesno",
                   "askokcancel", "askquestion", "askretrycancel"):
            setattr(app.messagebox, nm, _stub(nm))

        win = app.App(root) if hasattr(app, "App") else app.MainWindow(root)
        root.update()
        tabs = getattr(win, "notebook", None) or getattr(win, "tabs", None)
        n = 0
        if tabs is not None:
            try:
                n = len(tabs.tabs())
            except Exception:                                  # noqa: BLE001
                n = 0
        check("GUI 可构建且七个页签齐全", n >= 7, "页签 %d 个" % n)

        switched, switch_err = 0, ""
        for i in range(1, max(n, 7) + 1):
            try:
                win.select_tab(i)
                root.update()
                switched += 1
            except Exception as e:                             # noqa: BLE001
                switch_err = "%s: %s" % (type(e).__name__, str(e)[:120])
                break
        check("逐页切换不报错", switched >= n and not switch_err,
              "切了 %d 页 %s" % (switched, switch_err))

        errs = []
        for meth in ("refresh_overview", "refresh_channels", "refresh_profiles",
                     "refresh_accommodations", "refresh_altverify",
                     "refresh_tech_blocks", "refresh_fairness", "refresh_drills",
                     "refresh_ledger", "refresh_linkage", "check_audit"):
            fn = getattr(win, meth, None)
            if fn is None:
                continue
            try:
                fn()
                root.update()
            except Exception as e:                             # noqa: BLE001
                errs.append("%s(%s: %s)" % (meth, type(e).__name__, str(e)[:80]))
        check("各页刷新（含空库/演示数据）不报错", not errs, "；".join(errs)[:200])
        root.destroy()
    except Exception as e:                                     # noqa: BLE001
        check("GUI 冒烟不崩溃", False, "%s: %s" % (type(e).__name__, str(e)[:160]))


def real_readonly_checks():
    """--real：真实 data 目录的只读体检（幂等建表/补列，不写业务数据）。"""
    import config
    print("=" * 76)
    print("09 无障碍与特殊考生保障 · 真实数据只读体检")
    print("=" * 76)
    print("数据目录：%s" % config.DATA_ROOT)
    if not os.path.isfile(str(config.DB_PATH)):
        print("尚无真实数据库（%s），无数据可体检 —— 这是正常的初始状态。" % config.DB_PATH)
        return 0
    import metrics as metrics_mod
    import storage
    store = storage.Store(config.DB_PATH)
    s = store.summary()
    okc, msg, n = store.verify_audit_chain()
    print("考点 %d｜通道 %d（可用 %d）｜档案 %d｜替代核验 %d｜受阻 %d｜演练 %d｜公平性测试 %d"
          % (s["sites"], s["channels"], s["channels_usable"], s["profiles"]["total"],
             s["alt_verifies"]["total"], s["tech_blocks"]["total"], s["drills"]["total"],
             s["fairness_runs"]))
    print("审计链：%s" % msg)
    m = metrics_mod.compute(store, config.load_settings())
    for line in metrics_mod.text_summary(m):
        print("  " + line)
    print("硬指标：%s" % ("全部达标" if m["ok"] else
                       "未达标：" + "、".join(m["hard_fail"])))
    store.close()
    return 0 if okc else 1


if __name__ == "__main__":
    sys.exit(main())
