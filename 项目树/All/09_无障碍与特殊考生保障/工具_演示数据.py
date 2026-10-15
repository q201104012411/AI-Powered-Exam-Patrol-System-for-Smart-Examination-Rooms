# -*- coding: utf-8 -*-
r"""
09 无障碍与特殊考生保障 · 演示数据生成器

为什么需要它：无障碍通道、低位终端、手语翻译这些在现场才有；本机没设备时，
界面和报告都空着，就没法演示"指标怎么算、报告长什么样"。

**如实标注**：本工具造出来的每一条记录 source 都写 `demo`，
界面、台账、报告里都会写明"演示数据"，**绝不冒充真实保障记录**。

用法：
    python 工具_演示数据.py              # 在真实 data 目录造演示数据（会先问一次）
    python 工具_演示数据.py --yes        # 不问，直接造
    python 工具_演示数据.py --reset      # 清掉业务数据（审计不删）后再造
    python 工具_演示数据.py --home D:\tmp\acc09   # 造到指定数据目录（推荐，不碰真实库）
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

SITES = ["第一考点", "第二考点"]


def build(store, config, models):
    import access

    # 演示要把两个考点都纳入统计口径（否则配置率的分母只有 1 个考点）
    config.save_settings({"sites": SITES, "session": "2026年上半年"})
    for s in SITES:
        store.ensure_site(s)

    # ---------------- 通道：两个考点各三通道 ----------------
    specs = [
        # (考点, 编码, 名称, 类型, 净宽, 回转, 低位, 台面, 可调, 语音, 文字, 震动, 手语)
        (SITES[0], "SMT-A01", "智能核验通道 1 号", config.CH_SMART, 900, 0, False, 0,
         False, True, True, False, False),
        (SITES[0], "MNL-A01", "人工核验通道 1 号", config.CH_MANUAL, 1000, 0, False, 0,
         False, True, True, False, False),
        (SITES[0], "ACC-A01", "无障碍专用通道 1 号", config.CH_ACCESSIBLE, 1200, 1800,
         True, 850, True, True, True, True, True),
        (SITES[0], "ACC-A02", "无障碍备用通道 2 号", config.CH_ACCESSIBLE, 950, 1500,
         True, 900, True, True, True, True, False),
        (SITES[1], "SMT-B01", "智能核验通道 1 号", config.CH_SMART, 900, 0, False, 0,
         False, True, True, False, False),
        (SITES[1], "MNL-B01", "人工核验通道 1 号", config.CH_MANUAL, 1000, 0, False, 0,
         False, True, True, False, False),
        (SITES[1], "ACC-B01", "无障碍专用通道 1 号", config.CH_ACCESSIBLE, 980, 1600,
         True, 880, True, True, True, True, True),
    ]
    for (site, code, name, kind, w, turn, low, h, adj, voice, text, vib, sign) in specs:
        ch = models.Channel(code=code, name=name, site=site, kind=kind, width_mm=w,
                            turn_radius_mm=turn, low_counter=low, counter_height_mm=h,
                            height_adjustable=adj, voice_prompt=voice,
                            text_prompt=text, vibration=vib, sign_service=sign,
                            status=config.ST_OK, devices=["D-" + code],
                            source=config.DEMO_SOURCE,
                            note="演示数据（非现场实测值）")
        ch.last_check_at = models.now_ts()
        ch.last_check_epoch = models.now_epoch()
        store.save_channel(ch)

    # ---------------- 特殊考生档案 ----------------
    people = [
        # (考生号, 姓名, 需求, 考场, 座位, 考点, 同意, 证明, 已落实措施)
        ("110101199001011234", "张明", [config.NEED_FACE], "001", "07", SITES[0], True,
         "", [config.M_MANUAL_VERIFY, config.M_TEXT]),
        ("110101199203055678", "李慧", [config.NEED_VISION], "002", "12", SITES[0],
         True, "", [config.M_VOICE, config.M_ESCORT]),
        ("110101199505129012", "王强", [config.NEED_MOBILITY], "003", "03", SITES[1],
         True, "CJ-2026-0355", [config.M_WHEELCHAIR, config.M_LOW_COUNTER]),
        ("110101199807213456", "赵蕾", [config.NEED_HEARING], "004", "21", SITES[0],
         True, "", [config.M_TEXT, config.M_VIBRATION]),
        ("110101200001017890", "陈默", [config.NEED_MEDICAL], "005", "09", SITES[1],
         True, "MED-2026-1180", [config.M_MANUAL_SECURITY, config.M_MEDICAL_PASS]),
        ("110101200103152468", "欧阳娜", [config.NEED_LANGUAGE, config.NEED_FACE],
         "006", "15", SITES[0], True, "", [config.M_MANUAL_VERIFY, config.M_SIGN]),
        ("110101200205301357", "刘星", [config.NEED_OTHER], "007", "02", SITES[1],
         False, "", []),
    ]
    for (cid, name, needs, room, seat, site, consent, cert, provided) in people:
        ok, pf, extra = access.add_profile(store, {
            "candidate_id": cid, "candidate_name": name, "needs": needs,
            "room_id": room, "seat_no": seat, "site": site, "consent": consent,
            "cert_no": cert, "cert_verified": bool(cert),
            "need_detail": "考前已申报（演示数据）" if config.NEED_OTHER in needs else "",
            "source": config.DEMO_SOURCE, "provided": provided,
            "owner": "考务一组" if provided else "", "place": "考点入口",
        }, "演示数据")
        if not ok or not hasattr(pf, "profile_id"):
            continue
        for m in provided:
            access.set_measure_status(store, pf.profile_id, m, config.ACC_PROVIDED,
                                      "演示数据", owner="考务一组", place="考点入口")
    return people


def build_events(store, config, models):
    """替代核验 / 受阻事件 / 公平性 / 演练 / 联动队列。"""
    import drill as drill_mod
    import fairness as fairness_mod
    import verify

    # 替代核验：一次协助刷脸、一次双证人工核验（超时）、一次免核验
    verify.alt_verify(store, {
        "candidate_id": "110101199001011234", "mode": config.ALT_ASSISTED_FACE,
        "docs": {config.DOC_ID_CARD: config.DOC_MATCHED,
                 config.DOC_TICKET: config.DOC_MATCHED,
                 config.DOC_PHOTO: config.DOC_MATCHED},
        "inspector": "核验员甲", "witness": "监督员乙", "elapsed_sec": 90,
        "smart_result": {"ok": True, "result": "fail", "reason_code": "FACE_MISMATCH"},
        "tech_reason": True, "symptom": "face_fail", "device_code": "FACE-A01",
        "reason": "面部术后特征变化，多次刷脸不通过",
        "source": config.DEMO_SOURCE}, "演示数据")
    verify.alt_verify(store, {
        "candidate_id": "110101199203055678", "mode": config.ALT_MANUAL_DOC,
        "docs": {config.DOC_ID_CARD: config.DOC_MATCHED,
                 config.DOC_TICKET: config.DOC_MATCHED},
        "inspector": "核验员丙", "witness": "监督员丁", "elapsed_sec": 235,
        "reason": "视力障碍，不便使用刷脸终端", "drill": False,
        "source": config.DEMO_SOURCE}, "演示数据")
    verify.alt_verify(store, {
        "candidate_id": "110101199505129012", "mode": config.ALT_EXEMPT,
        "docs": {config.DOC_ID_CARD: config.DOC_MATCHED,
                 config.DOC_MEDICAL: config.DOC_MATCHED},
        "inspector": "核验员甲", "witness": "监督员乙", "elapsed_sec": 30,
        "reason": "考前已备案免核验（轮椅考生，凭身份证+残疾证明）",
        "source": config.DEMO_SOURCE}, "演示数据")

    # 受阻事件：一条已闭环为替代通道入场，一条仍在跟进（等待时长用于整改）
    b = verify.raise_tech_block(store, {
        "candidate_id": "110101199807213456", "candidate_name": "赵蕾",
        "room_id": "004", "seat_no": "21", "site": SITES[0],
        "symptom": "no_voice", "device_code": "FACE-A01", "channel_code": "SMT-A01",
        "detail": "语音提示失效，听力考生无法获知核验流程",
        "source": config.DEMO_SOURCE}, "演示数据")
    if b is not None:
        verify.resolve_tech_block(store, b.block_id, config.FINAL_ENTERED_MANUAL,
                                  "改走人工核验通道并由监考员书面提示", "演示数据")
    b2 = verify.raise_tech_block(store, {
        "candidate_id": "110101200103152468", "candidate_name": "欧阳娜",
        "room_id": "006", "seat_no": "15", "site": SITES[0],
        "symptom": "face_fail", "device_code": "FACE-A01",
        "detail": "证件姓名为少数民族多字符，刷脸终端显示不全导致反复失败",
        "source": config.DEMO_SOURCE}, "演示数据")
    if b2 is not None:
        verify.resolve_tech_block(store, b2.block_id, config.FINAL_ENTERED_MANUAL,
                                  "改走人工核验通道（双证 + 双人见证）", "演示数据")

    # 算法公平性：一次超差（含补偿说明），一次补偿后复测达标
    fairness_mod.run_fairness(store, {
        "name": "上线前分层测试（演示）", "algorithm": "01 人脸 1:1 v2",
        "threshold": 0.62, "action": "threshold",
        "action_note": "戴口罩分层阈值 0.62->0.60，并加反光/遮罩提示",
        "source": config.DEMO_SOURCE,
        "groups": {
            config.DIM_GENDER: {"男": {"total": 300, "passed": 285},
                                "女": {"total": 300, "passed": 281}},
            config.DIM_AGE: {"≤25": {"total": 200, "passed": 192},
                             "26-40": {"total": 200, "passed": 188},
                             ">40": {"total": 200, "passed": 176}},
            config.DIM_GLASSES: {"戴眼镜": {"total": 150, "passed": 138},
                                 "不戴眼镜": {"total": 150, "passed": 145}},
            config.DIM_MASK: {"戴口罩": {"total": 150, "passed": 126},
                              "不戴口罩": {"total": 150, "passed": 145}},
        }}, "演示数据")
    fairness_mod.run_fairness(store, {
        "name": "阈值补偿后复测（演示）", "algorithm": "01 人脸 1:1 v2",
        "threshold": 0.60, "action": "none", "source": config.DEMO_SOURCE,
        "groups": {
            config.DIM_GENDER: {"男": {"total": 300, "passed": 288},
                                "女": {"total": 300, "passed": 286}},
            config.DIM_AGE: {"≤25": {"total": 200, "passed": 193},
                             "26-40": {"total": 200, "passed": 191},
                             ">40": {"total": 200, "passed": 187}},
            config.DIM_GLASSES: {"戴眼镜": {"total": 150, "passed": 143},
                                 "不戴眼镜": {"total": 150, "passed": 146}},
            config.DIM_MASK: {"戴口罩": {"total": 150, "passed": 142},
                              "不戴口罩": {"total": 150, "passed": 146}},
        }}, "演示数据")

    # 演练：两个考点各一次
    drill_mod.add_drill(store, {
        "scenario": "full_chain", "site": SITES[0], "participants": 14,
        "planned_min": 30, "actual_min": 27,
        "channels": ["SMT-A01", "MNL-A01", "ACC-A01"],
        "issues": ["轮椅考生从东门到考场绕行距离偏长"],
        "actions": "在东门加设引导员与临时无障碍坡道，下次演练复测用时",
        "result": "partial", "witness": "考点副主任",
        "source": config.DEMO_SOURCE}, "演示数据")
    drill_mod.add_drill(store, {
        "scenario": "hearing", "site": SITES[1], "participants": 6,
        "planned_min": 20, "actual_min": 18, "channels": ["SMT-B01", "MNL-B01"],
        "result": "pass", "source": config.DEMO_SOURCE}, "演示数据")

    # 联动外发队列：造一条"待重发"演示记录（说明 03 不可达时事件也不会丢）
    store.outbox_add("ACCESS_ASSIST_NEEDED", config.LEVEL_ORANGE, {
        "payload": {"code": "ACCESS_ASSIST_NEEDED",
                    "detail": "演示：特殊考生需现场协助（该条为待重发演示记录）",
                    "level": config.LEVEL_ORANGE, "room_id": "004"}})


def reset_business(store):
    """只清业务表，不删审计（留痕不可丢）。"""
    n = {}
    for t in ("channels", "profiles", "accommodations", "alt_verifies", "tech_blocks",
              "fairness_runs", "fairness_groups", "drills", "outbox", "sites"):
        cur = store._exec("DELETE FROM %s" % t)
        n[t] = cur.rowcount
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="", help="数据目录（设置 EXAM_ACCESS_HOME）")
    ap.add_argument("--yes", action="store_true", help="不确认，直接写入")
    ap.add_argument("--reset", action="store_true", help="先清业务数据（审计保留）")
    ns = ap.parse_args(argv)

    if ns.home:
        os.environ["EXAM_ACCESS_HOME"] = ns.home

    import config
    print("=" * 74)
    print("09 无障碍与特殊考生保障 · 演示数据生成（数据 source=demo，界面会写明「演示」）")
    print("=" * 74)
    print("数据目录：%s" % config.DATA_ROOT)
    if not ns.yes and not ns.home:
        try:
            ans = input("将往上述**真实数据目录**写入演示数据，继续？(y/N) ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("已取消。建议：python 工具_演示数据.py --home %%TEMP%%\\acc09_demo")
            return 0

    import models
    import storage
    store = storage.Store(config.DB_PATH)
    if ns.reset:
        n = reset_business(store)
        print("已清业务数据：%s" % "、".join("%s=%d" % (k, v) for k, v in n.items()))

    build(store, config, models)
    build_events(store, config, models)
    store.log("生成演示数据", "source=demo，仅用于界面/报告演示", "工具_演示数据")
    s = store.summary()
    print("已生成：通道 %d｜档案 %d｜替代核验 %d｜受阻 %d｜演练 %d｜公平性测试 %d｜待发事件 %d"
          % (s["channels"], s["profiles"]["total"], s["alt_verifies"]["total"],
             s["tech_blocks"]["total"], s["drills"]["total"], s["fairness_runs"],
             s["outbox"]["pending"]))
    import metrics as metrics_mod
    m = metrics_mod.compute(store, config.load_settings())
    print("-" * 74)
    for line in metrics_mod.text_summary(m):
        print("  " + line)
    print("-" * 74)
    print("硬指标：%s" % ("全部达标" if m["ok"] else "未达标：" + "、".join(m["hard_fail"])))
    print("（上述均为演示数据，报告里会标明 source=demo）")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
