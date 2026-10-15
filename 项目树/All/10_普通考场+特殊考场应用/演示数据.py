# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 演示数据（一键铺一个完整场景）

用途：第一次打开界面时不该是空屏。这里铺**普通 2 个 + 特殊 7 类各 1 个**共 9 个考场，
并把每类考场的"该做的事"都做完，让你一眼看到：
  * 普通考场：已就绪（准入自检全过、告警已分级、指标已测算）
  * 备用考场：**降级到最小可用配置**（1 机位 + 1 链路也能巡考并留痕）
  * 隔离考场：健康信息独立分区 + DPIA + 远程监考在岗
  * 无障碍考场：人工核验工位（替代通道）+ 演练记录
  * 机考考场：屏幕行为审计 + 作答内容独立分区
  * 口试考场：48kHz 录音
  * 大教室：跨机位去重已验证
  * 涉密考场：全离线自检通过 + 涉密独立分区

**如实说明**：演示数据全部带 `data_source=synthetic` 标注；
`kpi` 里的"实测值"是演示用的脚本化数字，报告里会写明
"不等于真实考场准确率，真实指标必须用考场现场样本"。
想清空重来：删掉 `data\rooms.db` 即可（或换 `EXAM_ROOM_HOME`）。

用法：
  python 演示数据.py            # 铺到真实 data\（界面就能看到）
  python 演示数据.py --sandbox  # 铺到临时目录（只做验证，不落真实库）
  python 演示数据.py --force    # 已有考场也继续铺（默认已有就跳过）
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PLAN = [
    # (名称, 类型, 座位, 启用原因)
    ("A-101 普通考场", "standard", 30, "常规笔试"),
    ("A-102 普通考场", "standard", 30, "常规笔试"),
    ("B-201 备用考场", "backup", 5, "迟到考生继续考试"),
    ("C-301 隔离考场", "isolation", 4, "发热考生，经考务组审批启用"),
    ("D-401 无障碍考场", "accessible", 10, "视力障碍考生 2 名"),
    ("E-501 机考考场", "computer", 40, "计算机化考试"),
    ("F-601 口试考场", "oral", 1, "外语口试"),
    ("G-701 大教室考场", "large", 90, "合班考试"),
    ("H-801 涉密考场", "classified", 20, "高保密等级专项考试"),
]


def _banner(t):
    print()
    print("-" * 70)
    print(t)
    print("-" * 70)


def seed(operator="演示管理员", force=False, quiet=False):
    import admission
    import alerting
    import config
    import kpi as kpi_mod
    import linkage_client
    import privacy
    import rooms as rooms_mod
    import storage

    store = storage.Store()
    if store.list_rooms() and not force:
        print("已有 %d 个考场，跳过（要覆盖请加 --force）" % len(store.list_rooms()))
        return {"skipped": True, "rooms": len(store.list_rooms())}

    made = {}
    _banner("① 建档 + 一键套用模板（8 类：普通 1 类 + 特殊 7 类）")
    for i, (name, rtype, seats, reason) in enumerate(PLAN):
        room = rooms_mod.upsert_room(store, name, rtype, operator=operator,
                                     seats=seats, apply_reason=reason)
        rooms_mod.apply_template(room, operator=operator, store=store)
        # 机位编码 + 准入时自动登记进 02 台账（02 不可用则如实判未检）
        prefix = "CAM-%s-" % name.split()[0]
        for j, p in enumerate(room.points):
            p["device_code"] = "%s%d" % (prefix, j + 1)
        rooms_mod.recompute_caps(room)
        made.setdefault(rtype, room)
        print("  %-18s %-16s 点位 %d 个，行为 %d 种"
              % (name, room.type_name(), len(room.points),
                 len(room.enabled_behaviors or [])))
        store.save_room(room)

    _banner("② 逐考场做该做的事（在岗 / 座位建模 / 标定 / 各类型强制项）")
    for name, rtype, _seats, _reason in PLAN:
        room = store.get_room_by_name(name)
        notes = []
        if room.room_type == config.TYPE_ISOLATION:
            admission.set_invigilators(store, room, [],
                                       remote={"name": "远程监考员甲"},
                                       operator=operator)
            privacy.ensure_partition(store, room, operator=operator)
            privacy.do_doctor_link(store, room, operator=operator,
                                   doctor="市一院发热门诊 张医生（远程会诊）",
                                   note="独立通道 + 远程会诊已打通")
            privacy.do_dpia(store, room, operator=operator,
                            assessor="数据保护专员",
                            conclusion="健康信息单独分区、最小必要、15 天到期销毁；"
                                       "仅远程监考员与考点医生可见")
            privacy.set_retention(store, room, 15, operator=operator)
            notes.append("健康信息独立分区 + DPIA + 医生协同")
        elif room.room_type == config.TYPE_CLASSIFIED:
            admission.set_invigilators(store, room,
                                       [{"name": "保密监考甲"},
                                        {"name": "保密监考乙"}], operator=operator)
            privacy.ensure_partition(store, room, operator=operator)
            privacy.do_offline_check(store, room, operator=operator, offline=True,
                                     note="断开外网后本地算法独立运行 30 分钟")
            privacy.do_dpia(store, room, operator=operator,
                            assessor="保密专员",
                            conclusion="全离线运行、数据不出域、专项审计")
            notes.append("全离线自检 + 涉密独立分区")
        elif room.room_type == config.TYPE_ACCESSIBLE:
            admission.set_invigilators(store, room,
                                       [{"name": "监考甲"}, {"name": "监考乙"}],
                                       operator=operator)
            admission.set_flags(store, room, operator=operator, alt_channel=1)
            privacy.do_drill(store, room, operator=operator,
                             witness="无障碍服务人员乙",
                             detail="低位核验终端 + 人工核验双通道演练，含免刷脸全流程")
            notes.append("人工核验工位 + 演练记录")
        elif room.room_type == config.TYPE_COMPUTER:
            admission.set_invigilators(store, room,
                                       [{"name": "监考甲"}, {"name": "监考乙"}],
                                       operator=operator)
            privacy.do_network_log(store, room, operator=operator,
                                   agent="机房行为审计代理 10.0.3.21",
                                   note="切屏/进程/外联日志已接入")
            privacy.ensure_partition(store, room, operator=operator)
            privacy.do_dpia(store, room, operator=operator,
                            assessor="数据保护专员",
                            conclusion="屏幕录制含作答内容，独立分区、严格保密")
            notes.append("屏幕审计 + 网络日志 + 作答内容独立分区")
        elif room.room_type == config.TYPE_ORAL:
            admission.set_invigilators(store, room,
                                       [{"name": "考官甲"}, {"name": "考官乙"}],
                                       operator=operator)
            privacy.do_hifi_audio(store, room, operator=operator, rate=48000,
                                  note="独立拾音 + 双通道备份录音")
            notes.append("48kHz 高保真录音 + 独立拾音")
        elif room.room_type == config.TYPE_LARGE:
            admission.set_invigilators(store, room,
                                       [{"name": "巡考甲"}, {"name": "巡考乙"},
                                        {"name": "巡考丙"}], operator=operator)
            admission.set_flags(store, room, operator=operator, dedup_verified=1)
            notes.append("跨机位去重已验证 + 分区管理")
        elif room.room_type == config.TYPE_BACKUP:
            admission.set_invigilators(store, room,
                                       [{"name": "监考甲"}], operator=operator)
            privacy.do_fast_enable(store, room, operator=operator,
                                   note="2 套机位 + 2 条链路已预置到备用考场")
            notes.append("设备预置即插即用")
        else:
            admission.set_invigilators(store, room,
                                       [{"name": "监考甲"}, {"name": "监考乙"}],
                                       operator=operator)
        admission.set_ledger(
            store, room,
            [{"seat_no": "%02d" % (k + 1), "candidate_id": "C%03d" % (k + 1)}
             for k in range(min(6, max(1, room.seats // 8)))],
            operator=operator)
        admission.set_calibration(store, room, operator=operator, region="A 区")
        print("  %-18s 现场 %-2d 人 / %-22s %s"
              % (name, room.invigilator_count(),
                 config.DOMAIN_RULES.get(room.data_domain, {}).get("name", ""),
                 "；".join(notes)))

    # 备用考场演示"降级可用"（1 机位 + 1 链路也能巡考并留痕）
    bk = store.get_room_by_name("B-201 备用考场")
    if bk is not None:
        rooms_mod.degrade_to_minimal(bk, level="minimal", operator=operator,
                                     store=store)
        print("  %-18s 已降级：%s（%s）"
              % (bk.name, bk.min_config_meta()["name"],
                 bk.min_config_meta()["desc"]))

    _banner("③ 准入自检 + 启用")
    for name, _t, _s, _r in PLAN:
        room = store.get_room_by_name(name)
        res = admission.arm(store, room, operator=operator)
        tag = "" if res.get("armed") else "   ← 被拦住：" + "；".join(
            d["name"] for d in res["blockers"][:2])
        print("  %-18s %-26s %s%s"
              % (name, res["decision"], res["next_phase_text"], tag))

    _banner("④ 分级告警演示（含 1 条误报复核 -> 进算法迭代闭环）")
    std = store.get_room_by_name("A-101 普通考场")
    if std is not None and std.armed():
        demo_alerts = [
            ("B_USE_PHONE", 0.97, "07", "俯视桌面检出疑似手机，请现场核实"),
            ("B_PEER_PEEK", 0.91, "12", "持续侧头看向邻座超过 4 秒"),
            ("B_WHISPER", 0.78, "15", "疑似交头接耳（持续 3 秒）"),
            ("I_ABSENT", 0.95, "", "监考员离开监考位超过 90 秒"),
        ]
        for code, conf, seat, detail in demo_alerts:
            r = alerting.raise_behavior_alert(
                store, std, code, conf,
                device_code=rooms_mod.point_device(std, "侧/后部特写机位"),
                seat_no=seat, detail=detail, suppress_check=False,
                push=False, operator=operator)
            print("  %-16s -> %-6s 置信度 %.2f  推送：%s"
                  % (r["event"].behavior_name, r["level_name"],
                     r["event"].confidence, "、".join(r["pushed_to"] or [])))
        # 一条"交头接耳"判为误报（复核闭环）
        ev = [a for a in store.list_alerts(room_id=std.room_id, limit=20)
              if a.behavior == "B_WHISPER"]
        if ev:
            alerting.resolve_alert(store, ev[0], "false_positive",
                                   reason="是同桌借橡皮，非交头接耳",
                                   operator=operator)
            print("  已复核 1 条为「误报」-> 进入算法迭代闭环")

    _banner("⑤ 验收指标（演示值，全部标注为合成数据）")
    demo_kpi = {
        "A-101 普通考场": dict(observe_total=40, observe_hit=40, observe_correct=38,
                            observe_false=1, response_sec=2.4,
                            camera_online_rate=1.0, coverage_rate=1.0),
        "G-701 大教室考场": dict(observe_total=90, observe_hit=89, observe_correct=86,
                             observe_false=3, response_sec=2.8,
                             dedup_expected=90, dedup_counted=88),
        "E-501 机考考场": dict(observe_total=40, observe_hit=40, observe_correct=38,
                            observe_false=2, response_sec=2.6,
                            dedup_expected=40, dedup_counted=39),
    }
    for name, kw in demo_kpi.items():
        room = store.get_room_by_name(name)
        if room is None:
            continue
        run = kpi_mod.new_run(room, operator=operator, data_source="synthetic",
                              **kw)
        ev = kpi_mod.evaluate(store, room, run=run, persist=True)
        print("  %-18s %s" % (name, ev["summary"]["verdict"]))

    _banner("⑥ 生成验收报告")
    import report as report_mod
    ok, info = report_mod.write_report(store)
    if ok:
        print("  Markdown: %s" % info["md"])
        print("  CSV     : %s" % info["csv"])

    s = store.summary()
    print()
    print("=" * 70)
    print("演示数据铺好了：%d 个考场（普通 %d / 特殊 %d），已启用 %d，"
          "告警 %d 条，验收测算 %d 次"
          % (s["rooms"], s["standard_rooms"], s["special_rooms"], s["armed"],
             s["alerts"]["total"], s["kpi_runs"]))
    print("审计哈希链：%s" % store.verify_chain()[2])
    print("=" * 70)
    print("提示：演示指标是合成数据，验收时要换成考场现场实测（数据来源选 real）。")
    store.close()
    return {"ok": True, "summary": s}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", action="store_true",
                    help="铺到临时目录（不落真实 data\\）")
    ap.add_argument("--force", action="store_true", help="已有考场也继续铺")
    ap.add_argument("--operator", default="演示管理员")
    args = ap.parse_args()
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            pass

    tmp = None
    if args.sandbox:
        tmp = tempfile.mkdtemp(prefix="room_demo_")
        os.environ["EXAM_ROOM_HOME"] = tmp
        for m in list(sys.modules):
            if m in ("config", "models", "storage", "rooms", "admission",
                     "alerting", "kpi", "privacy", "report", "linkage_client"):
                sys.modules.pop(m, None)
        # 联动也走沙箱，演示过程不往 01/02/03/06 真实库写东西
        import config
        config.save_settings({"sandbox_root": os.path.join(tmp, "linkage")})
        print("沙箱模式：%s（跑完自动删除）" % tmp)

    print("=" * 70)
    print("10 普通考场+特殊考场应用 · 演示数据")
    print("=" * 70)
    try:
        seed(operator=args.operator, force=args.force)
    finally:
        if tmp:
            import config as _c
            _c.save_settings({"sandbox_root": ""})
            shutil.rmtree(tmp, ignore_errors=True)
            print("（临时目录已清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
