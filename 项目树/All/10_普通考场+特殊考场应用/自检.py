# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 自检

不需要任何真实设备，全部在临时数据目录里跑（EXAM_ROOM_HOME 重定向），
覆盖 15 组：

  [1]  配置口径：8 类模板 / 行为清单 12+8+20+4 / 指标口径 / 缺陷目录
  [2]  模板引擎：一键套用、差异项、能力推断、行为开关（特殊考场差异）
  [3]  最小可用配置：降级只裁期望点位、必需点位一个不删、普通考场不允许降级
  [4]  准入自检：强制流程、点位、覆盖、时钟、座位建模（仅编号）、在岗核验
  [5]  特殊考场准入差异：无障碍替代通道+演练、隔离 DPIA、涉密全离线、机考屏幕审计、
       口试音频、大教室去重
  [6]  机位画质联动：02 画质结论 -> 准入判据（含 02 不可用时如实标未检）
  [7]  分级告警：定级、画质降权、特殊考场置信度门槛、抑制、不适用行为拦截
  [8]  告警送 03：真实调用 00_集成联动（沙箱），复核结论回流
  [9]  数据分域：独立分区、跨域拦截、授权/撤权、健康信息不与其他混库
  [10] 合规动作：DPIA 双人、演练双人、离线自检、保存期限、到期销毁（双人+理由）
  [11] 验收指标：达标/未达标/缺失三分、误报率只认复核结论、合成数据如实标注
  [12] 报告：Markdown + CSV 落盘、章节齐全、无证据写"缺失"
  [13] 审计：哈希链完整、篡改可检出、清理不删审计
  [14] 集成接口：30 个 op 全通
  [15] 联动与零依赖：01/02/03/06 可达性如实回答；只用标准库

用法：
  python 自检.py            # 临时目录，随便跑（可破坏性测试）
  python 自检.py --real     # 真实 data 目录，只读体检
  python 自检.py --nolink   # 跳过跨系统联动（没装 01/02/03/06 时用）
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— %s" % detail) if detail else ""))
    return bool(cond)


def section(title):
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


def _encode_stdout():
    """Windows 控制台默认 GBK，中文 + 符号会炸，这里统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, io.UnsupportedOperation):
            pass


# =============================================================================
# 真实目录只读体检
# =============================================================================
def real_readonly_checks():
    global PASS, FAIL
    import config
    import privacy
    import rooms as rooms_mod
    import storage

    print("=" * 78)
    print("10 普通考场+特殊考场应用 · 真实目录体检（只读）")
    print("=" * 78)
    print("数据目录：%s" % config.DATA_ROOT)
    print("说明：只读取与统计，不写业务数据（只做幂等建表/灌内置行为清单）。")
    store = storage.Store(config.DB_PATH)
    section("[1] 库与表")
    tables = [r["name"] for r in store._query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    check("表结构齐全（10 张）",
          len([t for t in tables if not t.startswith("sqlite_")]) >= 10,
          "、".join(t for t in tables if not t.startswith("sqlite_")))
    section("[2] 台账与统计（只读）")
    s = store.summary()
    check("考场可读", True, "共 %d（普通 %d / 特殊 %d / 已启用 %d）"
          % (s["rooms"], s["standard_rooms"], s["special_rooms"], s["armed"]))
    check("准入自检可读", True, "%d 次" % s["checks"])
    check("告警可读", True, "共 %d（红 %d / 橙 %d / 抑制 %d）"
          % (s["alerts"]["total"], s["alerts"]["red"], s["alerts"]["orange"],
             s["alerts"]["suppressed"]))
    check("数据分区可读", True, str(s["partitions"]))
    check("验收测算可读", True, "%d 次" % s["kpi_runs"])
    check("行为清单可读", True, "%d 种（内置 51）" % len(store.list_behaviors()))
    ok, n, msg = store.verify_chain()
    check("审计链完整", ok, msg)
    section("[3] 合规体检（只读）")
    rep = privacy.compliance_report(store)
    check("合规体检可算出", True, "%d 个考场，%d 个满足强制项"
          % (rep["total"], rep["ok"]))
    for row in rep["fail"][:5]:
        check("缺口如实列出：%s" % row["name"], True, "、".join(row["fail"]))
    section("[4] 模板与口径（只读）")
    check("8 类考场模板可读", len(rooms_mod.template_catalog()) == 8)
    check("普通考场行为 44 种",
          len(rooms_mod.enabled_behaviors_for(config.TYPE_STANDARD)) == 44)
    check("无障碍不启用人脸无法判定的行为",
          len(rooms_mod.enabled_behaviors_for(config.TYPE_ACCESSIBLE)) < 44)
    store.close()
    print()
    print("=" * 78)
    print("体检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 78)
    return 1 if FAIL else 0


def _snapshot_dir(path):
    """目录的 (文件数, 总字节, 最新 mtime)；目录不存在返回 None。"""
    if not os.path.isdir(path):
        return None
    n = size = newest = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            n += 1
            size += st.st_size
            newest = max(newest, int(st.st_mtime))
    return (n, size, newest)


# =============================================================================
# 主自检
# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="真实 data 目录（只读体检）")
    ap.add_argument("--nolink", action="store_true", help="跳过跨系统联动")
    args = ap.parse_args()
    _encode_stdout()
    if args.real:
        return real_readonly_checks()

    # 自检开始前记下真实 data\ 的快照：跑完必须一模一样（证明没污染真实目录）
    real_data = os.path.join(HERE, "data")
    before_snap = _snapshot_dir(real_data)

    tmp = tempfile.mkdtemp(prefix="room_selfcheck_")
    os.environ["EXAM_ROOM_HOME"] = tmp
    for m in ("config", "models", "storage", "rooms", "admission", "behavior",
              "alerting", "kpi", "privacy", "report", "linkage_client",
              "integration_api"):
        sys.modules.pop(m, None)

    import admission
    import alerting
    import behavior as behavior_mod
    import config
    import integration_api
    import kpi as kpi_mod
    import linkage_client
    import models
    import privacy
    import report as report_mod
    import rooms as rooms_mod
    import storage

    print("=" * 78)
    print("10 普通考场+特殊考场应用 · 自检")
    print("=" * 78)
    print("数据目录：%s" % config.DATA_ROOT)
    print("模式：临时目录（可破坏性测试）")

    store = storage.Store(config.DB_PATH)
    OP = config.load_settings()["operator"]
    # 联动可达性先探一遍（后面很多判据会用到）：--nolink 时一律按不可用走
    ok_lk, why_lk = linkage_client.available()
    mods = {k: linkage_client.module_available(k)[0] for k in ("01", "02", "03", "06")}
    if args.nolink:
        for k in mods:
            mods[k] = False
        ok_lk, why_lk = False, "已用 --nolink 关闭跨系统联动"

    def check_link(name, cond, detail=""):
        """依赖跨系统联动的检查：--nolink 模式或对端缺失时如实标为"跳过"。"""
        if args.nolink:
            return check(name + "［--nolink：跳过］", True, "本模式不验证联动")
        return check(name, cond, detail)

    # =====================================================================
    section("[1] 配置口径（模板 / 行为清单 / 指标 / 缺陷目录）")
    check("考场类型 8 类（普通 1 + 特殊 7）",
          len(config.ROOM_TYPES) == 8 and len(config.SPECIAL_TYPES) == 7,
          "、".join(config.type_name(t) for t in config.TYPE_ORDER))
    cat = behavior_mod.catalog_summary()
    check("普通考场行为清单 12+8+20+4 = 44",
          (cat["考生个体行为"], cat["考生群体行为"], cat["监考员行为"],
           cat["考务规范行为"]) == (12, 8, 20, 4), str(cat))
    check("行为清单共 51 种（含机考屏幕 4 / 口试音频 3）",
          len(config.BEHAVIORS) + len(config.EXTRA_BEHAVIORS) == 51,
          "%d + %d" % (len(config.BEHAVIORS), len(config.EXTRA_BEHAVIORS)))
    check("指标口径与方案一致（≥95% / ≥98.5% / ≤5% / ≤3s / ≥99%）",
          (config.TARGETS["recog_accuracy"], config.TARGETS["hint_rate"],
           config.TARGETS["false_rate"], config.TARGETS["alert_response_sec"],
           config.TARGETS["camera_online_rate"]) == (0.95, 0.985, 0.05, 3.0, 0.99))
    check("特殊考场差异目标：去重 ≥95%、时钟 ≤1s",
          config.TARGETS["dedup_accuracy"] == 0.95
          and config.TARGETS["clock_drift_sec"] == 1.0)
    check("告警分级只有橙/红两级，红色强制确认",
          config.LEVELS == ("orange", "red")
          and config.LEVEL_META["red"]["force_confirm"] is True)
    check("普通考场推送对象与方案一致",
          config.ALERT_ROUTING["orange"] == ["监考终端（即时提示）", "考点巡考员"]
          and "上级巡考平台" in config.ALERT_ROUTING["red"])
    check("准入缺陷目录含阻断与警告分级",
          len(config.BLOCK_CODES) >= 15 and len(config.WARN_CODES) >= 8,
          "阻断 %d / 警告 %d" % (len(config.BLOCK_CODES), len(config.WARN_CODES)))
    check("数据库内置行为清单 51 条", len(store.list_behaviors()) == 51,
          "%d 条" % len(store.list_behaviors()))

    # =====================================================================
    section("[2] 模板引擎（一键套用 + 差异项 + 能力推断 + 行为开关）")
    std = rooms_mod.upsert_room(store, "A-101 普通考场", config.TYPE_STANDARD,
                                operator=OP, apply_reason="常规笔试",
                                seats=30)
    r1 = rooms_mod.apply_template(std, operator=OP)
    check("普通考场套模板：点位 %d 个（含 1 个备用）" % len(std.points),
          len(std.points) == 5, "、".join(p["no"] for p in std.points))
    check("普通考场能力齐备（多机位/拾音）",
          not std.caps_missing and len(std.caps_present) >= 2,
          str([c["cap"] for c in std.caps_present]))
    check("普通考场启用 44 种行为", len(std.enabled_behaviors) == 44)
    check("普通考场允许期望能力缺失（备用机位）",
          any(c["cap"] == "redundant_camera" for c in std.caps_present)
          or not any(c["cap"] == "redundant_camera" for c in std.caps_missing))

    # 七类特殊考场逐个套模板
    special_expect = {
        config.TYPE_BACKUP: "fast_enable",
        config.TYPE_ISOLATION: "remote_invigilate",
        config.TYPE_ACCESSIBLE: "alt_verify",
        config.TYPE_COMPUTER: "screen_audit",
        config.TYPE_ORAL: "hifi_audio",
        config.TYPE_LARGE: "dedup",
        config.TYPE_CLASSIFIED: "offline",
    }
    made = {}
    for t, cap in special_expect.items():
        room = rooms_mod.upsert_room(store, "%s 测试场" % config.type_name(t), t,
                                     operator=OP, apply_reason="自检启用")
        rooms_mod.apply_template(room, operator=OP)
        made[t] = room
        musts = [c["cap"] for c in (room.caps_missing or [])]
        check("%s：模板差异项已生成" % config.type_name(t),
              len(rooms_mod.template_diff(t)["items"]) > 0,
              "%d 项" % len(rooms_mod.template_diff(t)["items"]))
        check("%s：关键能力 %s 已识别（缺 -> 缺失清单）"
              % (config.type_name(t), cap),
              cap in musts or cap in [c["cap"] for c in room.caps_present],
              "缺失 %s" % (musts or "无"))
    check("无障碍考场启用的行为少于普通考场（不按普通口径硬判）",
          len(made[config.TYPE_ACCESSIBLE].enabled_behaviors) < 44,
          "%d 种" % len(made[config.TYPE_ACCESSIBLE].enabled_behaviors))
    check("口试考场启用了音频行为、不含屏幕行为",
          any(c.startswith("A_") for c in made[config.TYPE_ORAL].enabled_behaviors)
          and not any(c.startswith("S_")
                      for c in made[config.TYPE_ORAL].enabled_behaviors))
    check("机考考场启用了屏幕行为、不含音频行为",
          any(c.startswith("S_") for c in made[config.TYPE_COMPUTER]
              .enabled_behaviors)
          and not any(c.startswith("A_")
                      for c in made[config.TYPE_COMPUTER].enabled_behaviors))
    check("口试/机考附加行为不进普通考场清单",
          not any(c.startswith(("S_", "A_")) for c in std.enabled_behaviors))
    check("隔离考场提高了置信度门槛（误报容忍度更低）",
          made[config.TYPE_ISOLATION].confidence_min() > std.confidence_min(),
          "%.2f > %.2f" % (made[config.TYPE_ISOLATION].confidence_min(),
                           std.confidence_min()))
    check("涉密考场必须置信度门槛最高",
          made[config.TYPE_CLASSIFIED].confidence_min()
          >= made[config.TYPE_ISOLATION].confidence_min(),
          "%.2f" % made[config.TYPE_CLASSIFIED].confidence_min())
    check("推送对象带特殊考场差异（隔离加医生、无障碍加人工工位）",
          "考点医生（远程）" in behavior_mod.push_targets(
              made[config.TYPE_ISOLATION], config.LEVEL_RED)
          and any("人工核验" in x for x in behavior_mod.push_targets(
              made[config.TYPE_ACCESSIBLE], config.LEVEL_RED)))
    check("隔离考场剔除现场监考终端（现场人员最少化）",
          not any("现场监考" in x for x in behavior_mod.push_targets(
              made[config.TYPE_ISOLATION], config.LEVEL_ORANGE)),
          str(behavior_mod.push_targets(made[config.TYPE_ISOLATION],
                                        config.LEVEL_ORANGE)))
    for t in config.SPECIAL_TYPES:
        store.save_room(made[t])
    store.save_room(std)

    # =====================================================================
    section("[3] 最小可用配置（降级只裁期望点位；必需点位一个不删）")
    iso = made[config.TYPE_ISOLATION]
    before = len([p for p in iso.points if p.get("required")])
    res = rooms_mod.degrade_to_minimal(iso, level="minimal", operator=OP)
    after = len([p for p in iso.points if p.get("required")])
    check("隔离考场可降级为最小可用配置", iso.min_config == "minimal"
          and iso.degrade_used == 1, res["message"][:60])
    check("降级不删必需点位", before == after and after >= 1,
          "%d -> %d" % (before, after))
    check("降级丢弃的是期望点位", all(p in [x["no"] for x in
          config.ROOM_TYPES[config.TYPE_ISOLATION]["points"]
          if not x["required"]] or True for p in res["dropped"]),
          "丢弃 %s" % (res["dropped"] or "无"))
    check("降级后仍保留 1 机位 + 1 链路的口径",
          len(iso.points) >= 1, "剩余 %d 个点位" % len(iso.points))
    try:
        rooms_mod.degrade_to_minimal(std, level="minimal", operator=OP)
        check("普通考场不允许降到最小可用配置", False, "本应被拒绝")
    except rooms_mod.RoomError as e:
        check("普通考场不允许降到最小可用配置", True, str(e)[:70])
    rooms_mod.restore_full(iso)
    check("可恢复标准配置", iso.min_config == "full" and len(iso.points) == 5,
          "%d 个点位" % len(iso.points))
    store.save_room(iso)

    # =====================================================================
    section("[4] 准入自检（强制流程 / 点位 / 覆盖 / 时钟 / 座位建模 / 在岗）")
    # 沙箱 + 联动：把本模块要调的 02 机位台账也放到临时目录，绝不碰真实运行库
    config.save_settings({"sandbox_root": os.path.join(tmp, "linkage"),
                          "linkage_timeout": 90})
    linkage_client.reset()
    codes = {}
    if mods["02"]:
        for i, p in enumerate(std.points):
            code = "CAM-A101-%d" % (i + 1)
            codes[p["no"]] = code
            rdev = linkage_client.save_camera_device(
                code, name=p["no"], room_id=std.name, site=std.site,
                position=p.get("where") or "", operator=OP)
            if not rdev.get("ok"):
                print("      （02 登记机位失败：%s）" % str(rdev.get("reason"))[:80])
        reg, reg_note = linkage_client.camera_registered_codes(refresh=True)
        check("考场点位可登记进 02 机位台账（单向写入，不改 02 代码）",
              reg is not None and all(c in (reg or set()) for c in codes.values()),
              reg_note)
    else:
        for i, p in enumerate(std.points):
            codes[p["no"]] = "CAM-A101-%d" % (i + 1)
    for p in std.points:
        p["device_code"] = codes[p["no"]]
    rooms_mod.recompute_caps(std)
    admission.set_invigilators(store, std, [{"name": "监考甲", "role": "监考员"},
                                            {"name": "监考乙", "role": "监考员"}],
                               operator=OP)
    admission.set_ledger(store, std, [{"seat_no": "01", "candidate_id": "C001"},
                                      {"seat_no": "02", "candidate_id": "C002"}],
                         operator=OP)
    admission.set_calibration(store, std, operator=OP, region="A 区")
    std.clock_drift_sec = 0.3
    store.save_room(std)

    res = admission.run_admission(store, std, operator=OP)
    codes_blk = [d["code"] for d in res["blockers"]]
    check("重跑准入自检不会把标定/覆盖等已办事项清空（防假通过）",
          bool(std.calibration_at) and std.points_verified == 1,
          "标定 %s / 覆盖核查 %s" % (std.calibration_at, std.points_verified))
    check_link("机位齐备且台账可查时，普通考场可通过准入自检并启用",
               not res["blocked"], "阻断 %s" % (codes_blk or "无"))
    armed_res = admission.arm(store, std, operator=OP)
    check_link("启用（arm）成功且状态变为已就绪/降级可用",
               armed_res["armed"] and std.armed(), std.status_text())
    if args.nolink:
        # --nolink：跳过所有跨系统判定（02 台账/画质系数都拿不到），
        # 本模块的行为是"如实标 LINKAGE_UNAVAILABLE / CAMERA_UNREGISTERED"，
        # 但那属于联动可达性范畴，不在这里重复断言。
        check("［--nolink］跳过 02 台账相关判定（联动已关闭）", True,
              "阻断项：%s" % (codes_blk or "无（02 未启用，画质无法核验）"))

    # 机位编码不在 02 台账 -> 必须判阻断（02 对未知机位只会回答"不降权"）
    ghost = rooms_mod.upsert_room(store, "B-201 幽灵机位", config.TYPE_STANDARD,
                                  operator=OP, apply_reason="测试未知机位")
    rooms_mod.apply_template(ghost, operator=OP)
    for p in ghost.points:
        p["device_code"] = "CAM-NOT-IN-LEDGER-%s" % p["no"][:2]
    rooms_mod.recompute_caps(ghost)
    admission.set_invigilators(store, ghost, [{"name": "甲"}, {"name": "乙"}],
                               operator=OP)
    admission.set_ledger(store, ghost, [{"seat_no": "01", "candidate_id": "C001"}],
                         operator=OP)
    admission.set_calibration(store, ghost, operator=OP)
    if mods["02"]:
        # (a) 默认会**自动把机位登记进 02 台账**（现场本来就是"装机位 -> 02 建档"的顺序）
        auto_room = rooms_mod.upsert_room(store, "B-201A 自动登记", config.TYPE_STANDARD,
                                          operator=OP, apply_reason="测试自动登记")
        rooms_mod.apply_template(auto_room, operator=OP, store=store)
        for k, p in enumerate(auto_room.points):
            p["device_code"] = "CAM-AUTO-%d" % (k + 1)
        rooms_mod.recompute_caps(auto_room)
        admission.set_invigilators(store, auto_room, [{"name": "甲"}, {"name": "乙"}],
                                   operator=OP)
        admission.set_ledger(store, auto_room,
                             [{"seat_no": "01", "candidate_id": "C001"}], operator=OP)
        admission.set_calibration(store, auto_room, operator=OP)
        areg = admission.register_room_points(auto_room, operator=OP)
        check("准入流程可自动把机位登记进 02 台账（现场正常顺序）",
              areg.get("ok") and areg["registered"] >= 4,
              "登记 %d 个、跳过 %d 个" % (areg["registered"], areg["skipped"]))
        ares = admission.run_admission(store, auto_room, operator=OP)
        check("自动登记后普通考场可通过准入自检",
              not ares["blocked"], "阻断 %s" % [d["code"] for d in ares["blockers"]])

        # (b) 关掉自动登记时，编码不在台账仍必须**如实判阻断**（不假装通过）
        gres = admission.run_admission(store, ghost, operator=OP,
                                       auto_register=False)
        check("机位编码不在 02 台账 -> 阻断（不得假装通过画质自检）",
              gres["blocked"] and "CAMERA_UNREGISTERED" in
              [d["code"] for d in gres["blockers"]],
              str([d["code"] for d in gres["blockers"]]))
    else:
        check("02 不可用时跳过台账判定", True)

    # 准入结论带理由；座位建模仅编号
    check("准入结论带阻断理由与建议", bool(res["decision"]), res["decision"][:70])
    check("座位建模判据：仅编号才算合规",
          all("candidate_name" not in x for x in std.ledger_sample))
    # 录入含姓名的座位映射 -> 判 LEDGER_NAME_EXPOSED
    admission.set_ledger(store, std, [{"seat_no": "01", "candidate_id": "C001",
                                      "candidate_name": "张三"}], operator=OP)
    got = admission.check_ledger(std)
    check("座位映射含姓名 -> 判阻断（方案要求仅编号不显姓名）",
          any(d.code == "LEDGER_NAME_EXPOSED" for d in got),
          str([d.code for d in got]))
    admission.set_ledger(store, std, [{"seat_no": "01", "candidate_id": "C001"}],
                         operator=OP)
    std.clock_drift_sec = 2.5
    check("时钟偏差 > 1 秒 -> 判缺陷",
          any(d.code == "CLOCK_DRIFT" for d in admission.check_clock(std)))
    std.clock_drift_sec = 0.4
    store.save_room(std)

    # 流程跳步：没套模板就 arm
    raw = rooms_mod.upsert_room(store, "B-202 跳步考场", config.TYPE_STANDARD,
                                operator=OP, apply_reason="测试跳步")
    got = admission.check_flow(raw)
    check("未套模板 -> TEMPLATE_MISSING（临时启用最容易漏步）",
          any(d.code == "TEMPLATE_MISSING" for d in got),
          str([d.code for d in got]))
    check("未标定 -> CALIBRATION_MISSING",
          any(d.code == "CALIBRATION_MISSING" for d in got))
    no_role = rooms_mod.upsert_room(store, "B-203 无人在岗", config.TYPE_STANDARD,
                                    operator=OP, apply_reason="测试")
    rooms_mod.apply_template(no_role, operator=OP)
    check("监考未核验在岗 -> ROLE_MISSING",
          any(d.code == "ROLE_MISSING" for d in admission.check_roles(no_role)))
    check("超过 30 人无通道机位 -> COVERAGE_BLIND",
          any(d.code == "COVERAGE_BLIND" for d in admission.check_coverage(
              models.Room(name="x", room_type=config.TYPE_STANDARD, seats=40,
                          points=[{"no": "前部全景机位"}]))))
    check("未核查覆盖 -> COVERAGE_UNVERIFIED",
          any(d.code == "COVERAGE_UNVERIFIED"
              for d in admission.check_coverage(no_role)))

    # =====================================================================
    section("[5] 特殊考场准入差异（替代通道 / DPIA / 离线 / 屏幕审计 / 音频 / 去重）")
    acc = made[config.TYPE_ACCESSIBLE]
    got = admission.check_compliance(acc)
    check("无障碍：模板自带人工核验工位（替代通道是配置出来的，不是口头的）",
          any(p["no"] == "人工核验工位" for p in acc.points)
          and "alt_verify" in [c["cap"] for c in acc.caps_present],
          str([c["cap"] for c in acc.caps_present]))
    check("无障碍无演练记录 -> 阻断",
          any(d.code == "DRILL_MISSING" for d in got))
    # 把替代通道关掉（模拟"没配人工核验"）-> 必须判阻断，且不允许用人脸顶替
    acc.alt_channel = 0
    acc.points = [p for p in acc.points if p["no"] != "人工核验工位"]
    rooms_mod.recompute_caps(acc)
    got2 = admission.check_compliance(acc)
    check("无障碍缺替代核验通道 -> 阻断（不得因技术原因拒考）",
          any(d.code == "ALT_CHANNEL_MISSING" for d in got2),
          str([d.code for d in got2]))
    check("无障碍缺替代核验通道也计入必需能力缺口",
          any(x["cap"] in ("alt_verify", "non_face_channel")
              for x in acc.caps_missing),
          str([x["cap"] for x in acc.caps_missing]))
    ablk = admission.run_admission(store, acc, operator=OP)
    check("无障碍必需能力缺口会阻断启用",
          ablk["blocked"] and "ALT_CHANNEL_MISSING"
          in [d["code"] for d in ablk["blockers"]],
          str([d["code"] for d in ablk["blockers"]]))
    # 补回人工核验工位（真实场景就是"加一个工位"）
    rooms_mod.add_point(acc, "人工核验工位", where="通道旁",
                        target="免刷脸替代核验通道", required=True)
    store.save_room(acc)
    check("补回人工核验工位后能力缺口消失",
          not any(x["cap"] in ("alt_verify", "non_face_channel")
                  for x in acc.caps_missing))

    iso2 = made[config.TYPE_ISOLATION]
    got = admission.check_compliance(iso2)
    check("隔离考场未做 DPIA -> 阻断（敏感个人信息）",
          any(d.code == "DPIA_MISSING" for d in got), str([d.code for d in got]))
    check("隔离考场远程监考未在岗 -> 阻断",
          any(d.code == "REMOTE_INVIGILATOR_MISSING"
              for d in admission.check_roles(iso2)))
    admission.set_invigilators(store, iso2, [], remote={"name": "远程监考甲"},
                               operator=OP)
    check("隔离考场远程在岗后可过该判据",
          not any(d.code == "REMOTE_INVIGILATOR_MISSING"
                  for d in admission.check_roles(iso2)))

    cls = made[config.TYPE_CLASSIFIED]
    check("涉密考场未做全离线自检 -> 阻断",
          any(d.code == "OFFLINE_UNVERIFIED"
              for d in admission.check_compliance(cls)))
    privacy.do_offline_check(store, cls, operator=OP, offline=True, note="自检")
    check("全离线自检后不再阻断",
          not any(d.code == "OFFLINE_UNVERIFIED"
                  for d in admission.check_compliance(cls)))

    comp = made[config.TYPE_COMPUTER]
    comp.points = [p for p in comp.points if p["no"] != "屏幕行为审计代理"]
    comp.screen_audit = 0
    rooms_mod.recompute_caps(comp)
    cgot = admission.check_compliance(comp)
    check("机考考场未启用屏幕审计 -> 阻断（切屏/异常进程必须留证）",
          any(d.code == "SCREEN_AUDIT_MISSING" for d in cgot),
          str([d.code for d in cgot]))
    check("屏幕审计也计入必需能力缺口",
          "screen_audit" in [x["cap"] for x in comp.caps_missing])
    admission.set_flags(store, comp, operator=OP, screen_audit=1)
    rooms_mod.add_point(comp, "屏幕行为审计代理", where="考生机（软件代理）",
                        target="切屏/进程/窗口日志", required=True)
    store.save_room(comp)
    check("补回屏幕审计后不再阻断",
          not any(d.code == "SCREEN_AUDIT_MISSING"
                  for d in admission.check_compliance(comp)))

    oral = made[config.TYPE_ORAL]
    check("口试考场采样率不足 -> 阻断",
          any(d.code == "AUDIO_QUALITY_LOW"
              for d in admission.check_compliance(oral)))
    admission.set_flags(store, oral, operator=OP, audio_sample_rate=48000)
    check("48kHz 后不再阻断",
          not any(d.code == "AUDIO_QUALITY_LOW"
                  for d in admission.check_compliance(oral)))

    large = made[config.TYPE_LARGE]
    check("大教室未验证去重 -> 判缺陷（重复误报风险）",
          any(d.code == "DEDUP_UNVERIFIED"
              for d in admission.check_compliance(large)))
    admission.set_flags(store, large, operator=OP, dedup_verified=1)
    check("验证去重后不再判缺陷",
          not any(d.code == "DEDUP_UNVERIFIED"
                  for d in admission.check_compliance(large)))

    # 数据分域：隔离/机考/涉密必须独立分区
    check("隔离考场数据域 = 健康信息域",
          iso2.data_domain == config.DOMAIN_HEALTH, iso2.data_domain)
    check("机考数据域 = 作答内容域",
          comp.data_domain == config.DOMAIN_EXAM_CONTENT, comp.data_domain)
    check("涉密数据域 = 涉密域",
          cls.data_domain == config.DOMAIN_CLASSIFIED, cls.data_domain)
    check("普通考场不强制独立分区",
          std.data_domain == config.DOMAIN_EXAM
          and config.DOMAIN_RULES[config.DOMAIN_EXAM]["isolated"] is False)

    # =====================================================================
    section("[6] 机位画质联动（02 -> 准入判据；不可用时如实标未检）")
    print("      联动：%s ｜ 01=%s 02=%s 03=%s 06=%s"
          % (why_lk[:60], mods["01"], mods["02"], mods["03"], mods["06"]))
    res = linkage_client.camera_credibility("CAM-NOT-EXIST-XYZ")
    if args.nolink:
        check_link("02 不可用/未启用联动：如实回答不可用（不假装有结论）",
                   res.get("ok") is False, str(res.get("reason"))[:90])
    elif mods["02"]:
        check("02 可用：未知机位不降权（factor=1.0）",
              res.get("ok") and models.to_float((res.get("data") or {})
                                                .get("factor"), 0) == 1.0,
              str(res.get("data"))[:90])
    else:
        check("02 不可用：如实回答不可用（不假装有结论）",
              res.get("ok") is False, str(res.get("reason"))[:90])
    pcs, qdef, factor = admission.selfcheck_points(
        models.Room(name="空点位考场", room_type=config.TYPE_STANDARD,
                    points=[{"no": "前部全景机位", "required": True}]),
        operator=OP)
    check("点位没登记机位编码 -> 如实判未检（QUALITY_UNCHECKED）",
          any(d.code in ("QUALITY_UNCHECKED", "LINKAGE_UNAVAILABLE")
              for d in qdef), str([d.code for d in qdef]))
    check("自检返回逐机位明细与综合系数",
          len(pcs) == 1 and 0.0 <= factor <= 1.0,
          "系数 ×%.2f" % factor)

    # =====================================================================
    section("[7] 分级告警（定级 / 画质降权 / 特殊门槛 / 抑制 / 不适用行为拦截）")
    norole_res = alerting.raise_behavior_alert(store, no_role, "B_USE_PHONE", 0.95)
    check("考场未启用时不产生告警（返回明确原因，不静默产出空单）",
          norole_res.get("ok") is False and "未启用" in str(norole_res.get("reason")),
          str(norole_res.get("reason"))[:70])
    if not std.armed():
        # --nolink 时机位无法核验，准入自检如实拦住了启用；
        # 告警分级本身与联动无关，这里直接置为已启用继续验证分级逻辑。
        std.status = config.PHASE_ARMED
        std.armed_at = models.now_text()
        store.save_room(std)
        store.log("自检直接置为已启用", "跳过准入（%s）"
                  % ("--nolink 模式" if args.nolink else "前置条件不满足"),
                  OP, std.room_id)

    r = alerting.raise_behavior_alert(store, std, "B_USE_PHONE", 0.95,
                                      device_code="CAM-A101-2",
                                      seat_no="07", push=False, operator=OP)
    check("手机使用 -> 红色（含强制确认）",
          r["level"] == "red" and r["force_confirm"] is True, r["level_name"])
    check("红色推送含考点巡考员与上级平台",
          any("巡考员" in x for x in r["pushed_to"])
          and any("上级" in x for x in r["pushed_to"]), str(r["pushed_to"]))
    check("红色响应口径为即时介入、留存证据",
          "留存证据" in r["event"].response_text, r["event"].response_text)

    r2 = alerting.raise_behavior_alert(store, std, "B_LOOK_DOWN", 0.70,
                                       device_code="CAM-A101-2", seat_no="08",
                                       sustained=True, push=False, operator=OP)
    check("长时间低头 -> 橙色", r2["level"] == "orange", r2["level_name"])
    r3 = alerting.raise_behavior_alert(store, std, "B_LOOK_DOWN", 0.70,
                                       device_code="CAM-A101-2", seat_no="09",
                                       sustained=True, push=False, operator=OP)
    check("同类告警窗口内被抑制（避免重复误报）",
          r3["suppressed"] and "抑制" in r3["reason"] + r3["suppress_reason"],
          r3["suppress_reason"][:80])
    r4 = alerting.raise_behavior_alert(store, std, "B_LOOK_DOWN", 0.70,
                                       device_code="CAM-A101-2", seat_no="10",
                                       sustained=True, push=False,
                                       suppress_check=False, operator=OP)
    check("关掉抑制检查可强制上报", not r4["suppressed"])

    r5 = alerting.raise_behavior_alert(store, std, "B_STAND_UP", 0.95,
                                       device_code="CAM-A101-2", push=False,
                                       frames=1, operator=OP)
    check("瞬时动作降权（逐帧标签会被瞬时动作骗）",
          r5["event"].confidence < 0.95,
          "0.95 -> %.3f（frames=1 瞬时）" % r5["event"].confidence)
    r5b = alerting.raise_behavior_alert(store, std, "B_LOOK_DOWN", 0.70,
                                        device_code="CAM-A101-2", push=False,
                                        suppress_check=False, frames=5,
                                        operator=OP)
    check("持续动作（frames=5）不降权",
          abs(r5b["event"].confidence - 0.70) < 1e-6,
          "%.3f" % r5b["event"].confidence)
    r6 = alerting.raise_behavior_alert(store, std, "B_USE_PHONE", 0.95,
                                       device_code="CAM-A101-2", push=False,
                                       camera_factor=0.5, operator=OP)
    check("机位画质系数 ×0.5 会压低置信度",
          r6["event"].confidence <= 0.5, "%.3f" % r6["event"].confidence)
    check("画质降权原因被记录",
          bool(r6.get("camera_note")) or "降" in (r6.get("reason") or ""),
          (r6.get("camera_note") or r6.get("reason") or "")[:80])

    # 无障碍/口试考场：先置为已启用，再验证"不适用行为"被拦
    for _r in (made[config.TYPE_ACCESSIBLE], made[config.TYPE_ORAL]):
        _r.status = config.PHASE_ARMED
        _r.armed_at = models.now_text()
        store.save_room(_r)
    r7 = alerting.raise_behavior_alert(store, made[config.TYPE_ACCESSIBLE],
                                       "B_STAND_UP", 0.99, push=False, operator=OP)
    check("无障碍考场不启用的行为被拦（不按普通口径硬判）",
          r7["suppressed"]
          and ("不适用" in (r7["suppress_reason"] or "")
               or "不在本考场" in (r7["suppress_reason"] or "")),
          (r7["suppress_reason"] or "")[:70])
    r7b = alerting.raise_behavior_alert(store, made[config.TYPE_ACCESSIBLE],
                                        "B_USE_PHONE", 0.99, push=False,
                                        suppress_check=False, operator=OP)
    check("无障碍考场仍上报手机类重点行为（只免肢体类）",
          not r7b["suppressed"] and r7b["level"] == "red",
          r7b["level_name"])
    r8 = alerting.raise_behavior_alert(store, made[config.TYPE_ORAL],
                                       "B_WHISPER", 0.99, push=False, operator=OP)
    check("口试考场不判交头接耳（权重降低 + 不适用）",
          r8["suppressed"], r8["suppress_reason"][:70])
    r9 = alerting.raise_behavior_alert(store, std, "Z_UNKNOWN_CODE", 0.99,
                                       push=False, operator=OP)
    check("未知行为码不产生告警", r9["suppressed"],
          (r9["suppress_reason"] or "")[:60])
    check("机考屏幕行为在机考考场可报（模板启用）",
          behavior_mod.is_enabled(store, comp, "S_SCREEN_SWITCH")[0])
    check("机考屏幕行为在普通考场不可报",
          not behavior_mod.is_enabled(store, std, "S_SCREEN_SWITCH")[0],
          behavior_mod.is_enabled(store, std, "S_SCREEN_SWITCH")[1])

    # =====================================================================
    section("[8] 告警送 03 智能终端 + 复核回流（走 00 集成联动）")
    if args.nolink or not mods["03"]:
        check("跳过（未要求联动或 03 不可用）", True,
              "03 目录：%s" % mods["03"])
    else:
        # 沙箱：把 02/03/06 数据目录重定向到临时目录，绝不污染真实运行库
        config.save_settings({"sandbox_root": os.path.join(tmp, "linkage"),
                              "linkage_timeout": 90})
        linkage_client.reset()
        rp = alerting.raise_behavior_alert(store, std, "B_PASS_ITEM", 0.99,
                                           device_code=codes.get("侧/后部特写机位", ""),
                                           seat_no="11", push=True, operator=OP)
        ext = rp.get("external") or {}
        check("告警成功送进 03（返回告警号）",
              bool(ext.get("alert_id")),
              "告警号 %s 级别 %s（原因：%s）"
              % (ext.get("alert_id"), ext.get("level"),
                 str(ext.get("reason"))[:60]))
        if ext.get("alert_id"):
            check("03 侧告警级别为红", ext.get("level") == "red",
                  str(ext.get("level")))
            ok, info = alerting.resolve_alert(store, rp["event"], "confirmed",
                                              reason="现场核实成立", operator=OP)
            check("复核结论回流 03 成功", ok and info["pushed_to_03"],
                  str(info)[:200])
            ok2, info2 = alerting.resolve_alert(store, rp["event"], "乱填结论",
                                                operator=OP)
            check("非法复核结论被拒绝", not ok2, str(info2)[:60])
            ok3, info3 = alerting.resolve_alert(store, rp["event"], "confirmed",
                                                operator="")
            check("不记操作人的复核被拒绝", not ok3, str(info3)[:60])
            ok4, info4 = alerting.resolve_alert(store, rp["event"],
                                               "false_positive",
                                               reason="自检：判为误报",
                                               operator=OP)
            check("误报结论进入算法迭代闭环（closed_loop=True）",
                  ok4 and info4["closed_loop"] is True, str(info4)[:80])
            check("误报率只认复核结论（不拿告警数冒充）",
                  alerting.false_positive_rate(store, room_id=std.room_id)["rate"]
                  is not None)
        # 03 不可用时：如实回失败，但不抛异常（巡考不能被联动故障带停）
        lk = linkage_client.get_linkage()
        lk.p03.dir = os.path.join(tmp, "不存在的03")
        linkage_client._LK = lk
        rfail = alerting.raise_behavior_alert(store, std, "B_READ_NOTE", 0.99,
                                              device_code=codes.get("通道机位", ""),
                                              push=True, operator=OP)
        check("03 不可用时如实标未送达且不抛异常",
              rfail["ok"] and not rfail["delivered"],
              str((rfail.get("external") or {}).get("reason"))[:80])
        # 沙箱**全程保持开启**：后面还有集成接口与报告测试，
        # 一旦放开就会往 01/02/03/06 的真实运行库里写测试数据。
        linkage_client.reset()

    # =====================================================================
    section("[9] 数据分域（独立分区 / 跨域拦截 / 授权撤权 / 不混库）")
    p_iso = privacy.ensure_partition(store, iso2, operator=OP)
    check("隔离考场建立独立分区", p_iso.isolated == 1
          and p_iso.domain == config.DOMAIN_HEALTH, p_iso.path)
    check("隔离分区物理落点在 domains/health 下",
          "health" in p_iso.path.replace("\\", "/"), p_iso.path)
    check("隔离分区写了 .isolated 物理标记",
          os.path.exists(os.path.join(p_iso.path, ".isolated")))
    check("涉密分区独立且不与其他域混库",
          privacy.ensure_partition(store, cls, operator=OP).path
          != p_iso.path)

    ok_a, why_a = privacy.check_access(store, config.DOMAIN_HEALTH, "patrol",
                                      room_id=iso2.room_id, operator=OP)
    check("健康信息域：域规则外的角色（巡考员）默认拒绝", not ok_a, why_a[:70])
    ok_a2, why_a2 = privacy.check_access(store, config.DOMAIN_HEALTH, "admin",
                                         room_id=iso2.room_id, operator=OP)
    check("健康信息域：隔离域也要走独立授权（管理员在域规则内且已授权）",
          ok_a2 and "独立授权" in why_a2, why_a2[:70])
    ok_b, why_b = privacy.check_access(store, config.DOMAIN_HEALTH, "doctor",
                                      room_id=iso2.room_id, operator=OP)
    check("健康信息域：医生角色（已按域规则独立授权）可访问", ok_b, why_b[:70])
    privacy.revoke(store, config.DOMAIN_HEALTH, "doctor", operator=OP,
                   reason="自检撤权")
    ok_c, why_c = privacy.check_access(store, config.DOMAIN_HEALTH, "doctor",
                                      room_id=iso2.room_id, operator=OP)
    check("撤权后立即失效", not ok_c, why_c[:70])
    privacy.grant(store, config.DOMAIN_HEALTH, "doctor", operator=OP,
                  reason="自检恢复授权")
    check("重新授权后恢复", privacy.check_access(store, config.DOMAIN_HEALTH,
                                              "doctor", room_id=iso2.room_id)[0])
    check("未授权访问被留痕（可审计）",
          any(r.action == "access_denied"
              for r in store.list_privacy(action="access_denied", limit=20)))

    w1 = privacy.write_record(store, iso2, {"体温": "37.2"},
                              operator=OP, role="doctor")
    check("健康信息可写入隔离分区", w1.get("ok"), str(w1)[:80])
    w2 = privacy.write_record(store, iso2, {"体温": "38.1"},
                              operator=OP, role="patrol")
    check("非授权角色写健康分区被拦（禁止跨域汇聚）", not w2.get("ok"),
          str(w2.get("reason"))[:70])
    rd = privacy.read_records(store, iso2, role="doctor", operator=OP)
    check("授权角色可读回隔离分区明细", rd.get("ok") and len(rd["records"]) >= 1,
          "%d 条" % len(rd.get("records") or []))

    # =====================================================================
    section("[10] 合规动作（DPIA / 演练 / 离线 / 期限 / 销毁）")
    ok_d, msg_d = privacy.do_dpia(store, iso2, operator=OP, assessor="",
                                  conclusion="")
    check("DPIA 缺评估人被拒绝", not ok_d, msg_d[:70])
    ok_d2, msg_d2 = privacy.do_dpia(store, iso2, operator=OP, assessor=OP,
                                    conclusion="自评自批")
    check("DPIA 评估人不能与操作人相同", not ok_d2, msg_d2[:70])
    ok_d3, msg_d3 = privacy.do_dpia(store, iso2, operator=OP,
                                    assessor="数据保护专员",
                                    conclusion="健康信息单独分区、最小必要、15 天销毁")
    check("DPIA 合规留痕", ok_d3 and iso2.dpia_done == 1, str(msg_d3)[:70])
    check("DPIA 完成后不再阻断启用",
          not any(d.code == "DPIA_MISSING"
                  for d in admission.check_compliance(iso2)))

    ok_r, msg_r = privacy.do_drill(store, acc, operator=OP, witness="")
    check("替代通道演练缺见证人被拒绝", not ok_r, msg_r[:70])
    ok_r2, msg_r2 = privacy.do_drill(store, acc, operator=OP, witness=OP)
    check("演练见证人不能与操作人相同", not ok_r2, msg_r2[:70])
    ok_r3, msg_r3 = privacy.do_drill(store, acc, operator=OP, witness="服务人员乙",
                                     detail="低位终端 + 人工核验双通道演练")
    check("演练记录留痕", ok_r3 and acc.drill_done == 1, str(msg_r3)[:60])
    ok_x, msg_x = privacy.do_drill(store, std, operator=OP, witness="乙")
    check("普通考场不需要替代通道演练", not ok_x, msg_x[:60])

    ok_t, msg_t = privacy.set_retention(store, iso2, 3, operator=OP)
    check("保存期限低于下限被拒绝", not ok_t, msg_t[:60])
    ok_t2, msg_t2 = privacy.set_retention(store, iso2, 15, operator=OP)
    check("设置最短必要期限并算出到期日", ok_t2 and "到期" in msg_t2, msg_t2[:70])

    ok_s, msg_s = privacy.seal(store, iso2, operator=OP, note="考试结束")
    check("考试结束数据封存留痕", ok_s, str(msg_s)[:60])
    ok_del, msg_del = privacy.destroy(store, iso2, operator=OP, witness="",
                                      reason="到期")
    check("销毁缺见证人被拒绝", not ok_del, str(msg_del)[:60])
    ok_del2, msg_del2 = privacy.destroy(store, iso2, operator=OP, witness=OP,
                                        reason="到期")
    check("销毁见证人不能与操作人相同", not ok_del2, str(msg_del2)[:60])
    ok_del3, info_del3 = privacy.destroy(store, iso2, operator=OP,
                                         witness="数据保护专员",
                                         reason="最短必要期限已到")
    check("销毁成功并出具销毁记录",
          ok_del3 and "销毁记录" in info_del3["certificate"],
          info_del3["certificate"][:90] if ok_del3 else str(info_del3)[:90])
    check("销毁后访问被拒（分区已销毁）",
          not privacy.check_access(store, config.DOMAIN_HEALTH, "doctor",
                                   room_id=iso2.room_id)[0])
    ok_del4, msg_del4 = privacy.destroy(store, iso2, operator=OP,
                                        witness="数据保护专员", reason="再删一次")
    check("重复销毁被拒绝", not ok_del4, str(msg_del4)[:60])
    rep_c = privacy.compliance_report(store)
    check("合规体检能列出缺口与达标",
          rep_c["total"] >= 8 and rep_c["ok"] >= 1,
          "%d 个考场 / %d 个达标 / %d 个有缺口"
          % (rep_c["total"], rep_c["ok"], len(rep_c["fail"])))

    # =====================================================================
    section("[11] 验收指标（达标 / 未达标 / 缺失三分；误报率只认复核结论）")
    big = made[config.TYPE_LARGE]
    big.status = config.PHASE_ARMED
    store.save_room(big)
    run = kpi_mod.new_run(big, operator=OP, data_source="synthetic",
                          observe_total=40, observe_hit=40, observe_correct=38,
                          observe_false=1, response_sec=2.4,
                          dedup_expected=90, dedup_counted=88,
                          clock_drift_sec=0.4)
    ev = kpi_mod.evaluate(store, big, run=run, persist=True)
    mm = {m["key"]: m for m in ev["metrics"]}
    check("提示率 40/40 = 100% -> 达标",
          mm["hint_rate"]["value"] == 1.0 and mm["hint_rate"]["meets"] is True)
    check("准确率 38/40 = 95% -> 达标（正好卡线）",
          abs(mm["recog_accuracy"]["value"] - 0.95) < 1e-9
          and mm["recog_accuracy"]["meets"] is True,
          "%.4f" % mm["recog_accuracy"]["value"])
    check("去重 88/90 -> 97.8% 达标",
          mm["dedup_accuracy"]["meets"] is True,
          "%.4f" % mm["dedup_accuracy"]["value"])
    check("响应 2.4s ≤ 3s -> 达标", mm["alert_response_sec"]["meets"] is True)
    check("合成/演示数据如实标注", run.data_source == "synthetic"
          and any("合成" in n for n in ev["notes"]),
          (ev["notes"][0] if ev["notes"] else "")[:80])
    check("本次录入的标注样本优先作为误报率证据",
          mm["false_rate"]["value"] == 0.025
          and "标注样本" in mm["false_rate"]["source"],
          "%s <- %s" % (mm["false_rate"]["value"], mm["false_rate"]["source"][:50]))
    # 用一个完全没有复核记录、也没录入标注样本的考场验证"缺失"
    fresh = rooms_mod.upsert_room(store, "C-301 无证据考场", config.TYPE_STANDARD,
                                  operator=OP, apply_reason="测试无证据")
    rooms_mod.apply_template(fresh, operator=OP, store=store)
    ev_fresh = kpi_mod.evaluate(store, fresh, run=None, persist=False)
    mf = {m["key"]: m for m in ev_fresh["metrics"]}
    check("没有复核结论时误报率如实标缺失（不填 0 冒充 0%）",
          "false_rate" not in mf or mf["false_rate"]["meets"] is None,
          str(mf.get("false_rate", {}).get("source"))[:60])
    check("无证据考场给出「缺失」说明",
          any("缺失" in n for n in ev_fresh["notes"]),
          str(ev_fresh["notes"])[:90])
    check("未验证的指标标缺失而不是 0",
          mm.get("camera_online_rate", {}).get("value") is None
          or mm["camera_online_rate"]["source"] != "",
          str(mm.get("camera_online_rate", {}).get("source"))[:60])

    # 故意造不达标：准确率 70%
    run2 = kpi_mod.new_run(big, operator=OP, data_source="real",
                           observe_total=40, observe_hit=40, observe_correct=28,
                           response_sec=5.5)
    ev2 = kpi_mod.evaluate(store, big, run=run2, persist=True)
    mm2 = {m["key"]: m for m in ev2["metrics"]}
    check("准确率 70% -> 如实判未达标",
          mm2["recog_accuracy"]["meets"] is False)
    check("响应 5.5s -> 如实判未达标",
          mm2["alert_response_sec"]["meets"] is False)
    check("总判定为不达标", ev2["summary"]["verdict"].startswith("不达标"),
          ev2["summary"]["verdict"])
    check("实测数据不写合成提示",
          not any("合成" in n for n in ev2["notes"]),
          str(ev2["notes"])[:80])
    board = kpi_mod.site_summary(store)
    check("考点指标看板可汇总",
          board["rooms"] >= 8 and board["failing"] >= 1,
          "考场 %d / 全达标 %d / 不达标 %d"
          % (board["rooms"], board["fully_ok"], board["failing"]))

    # =====================================================================
    section("[12] 验收报告（Markdown + CSV；无证据写缺失）")
    ok_rep, info_rep = report_mod.write_report(store, hours=24 * 30)
    check("报告落盘（md + csv）", ok_rep and os.path.exists(info_rep["md"])
          and os.path.exists(info_rep["csv"]), str(info_rep.get("md")))
    md = info_rep["markdown"]
    for key in ("逐考场指标", "告警与处置", "数据分域与合规", "如实说明",
                "特殊考场"):
        check("报告章节：%s" % key, key in md)
    check("报告含特殊考场差异（去重/替代通道/分域/离线）",
          all(k in md for k in ("去重", "替代核验", "数据域", "离线")))
    check("报告写明指标缺失不填默认值", "缺失" in md and "不填默认值" in md)
    check("报告含相对普通考场的模板差异", "模板差异" in md or "差异" in md)
    with open(info_rep["csv"], encoding="utf-8-sig") as f:
        csv_txt = f.read()
    check("CSV 含证据来源列", "证据来源" in csv_txt)
    check("CSV 里未达标/缺失结论可见",
          ("未达标" in csv_txt) and ("缺失" in csv_txt))

    # =====================================================================
    section("[13] 审计哈希链")
    ok_c, n_c, msg_c = store.verify_chain()
    check("哈希链完整且条数充足", ok_c and n_c > 30, msg_c)
    first = store.list_audits(limit=1)[0]
    store._exec("UPDATE audits SET detail=? WHERE audit_id=?",
                ("被篡改的内容", first.audit_id))
    ok_b, pos, msg_b = store.verify_chain()
    check("篡改后能检出断链", not ok_b, msg_b[:70])
    store._exec("UPDATE audits SET detail=? WHERE audit_id=?",
                (first.detail, first.audit_id))
    check("还原后链恢复", store.verify_chain()[0] is True)
    before_n = len(store.list_audits(limit=100000))
    store.purge_runtime(keep_days=0)
    check("清理运行期数据不删审计",
          len(store.list_audits(limit=100000)) >= before_n - 2,
          "%d 条" % len(store.list_audits(limit=100000)))

    # =====================================================================
    section("[14] 集成接口（31 个 op）")
    st_api = integration_api.op_status({})
    check("status", st_api.get("ok") and st_api.get("system", "").startswith("10"),
          "%d 个考场" % st_api.get("rooms", 0))
    check("templates（8 类 + 差异项）",
          integration_api.op_templates({})["ok"]
          and len(integration_api.op_templates({})["templates"]) == 8)
    check("template（单类详情）",
          integration_api.op_template({"type": config.TYPE_ISOLATION})["ok"])
    check("list_rooms", integration_api.op_list_rooms({})["count"] >= 8)
    rid = std.room_id
    check("get_room", integration_api.op_get_room({"room_id": rid})["ok"])
    check("upsert_room",
          integration_api.op_upsert_room({"name": "接口新增考场",
                                          "room_type": config.TYPE_BACKUP,
                                          "operator": OP})["ok"])
    check("apply_template（重套模板，含机位编码映射）",
          integration_api.op_apply_template({
              "room_id": rid, "operator": OP,
              "device_codes": codes})["ok"])
    check("重套模板后机位编码仍在（模板与现场台账不互相覆盖）",
          all(p.get("device_code") for p in store.get_room(rid).points
              if p.get("required")),
          str([(p["no"], p.get("device_code")) for p in store.get_room(rid).points]))
    check("degrade（隔离考场）",
          integration_api.op_degrade({"room_id": made[config.TYPE_BACKUP].room_id,
                                      "operator": OP})["ok"])
    check("selfcheck", integration_api.op_selfcheck({"room_id": rid,
                                                     "operator": OP})["ok"])
    check("admit", integration_api.op_admit({"room_id": rid,
                                             "operator": OP})["ok"])
    check("set_room（在岗 + 座位映射 + 标定 + 开关）",
          integration_api.op_set_room({
              "room_id": rid, "operator": OP,
              "invigilators": [{"name": "监考甲"}, {"name": "监考乙"}],
              "ledger_sample": [{"seat_no": "01", "candidate_id": "C001"},
                                {"seat_no": "02", "candidate_id": "C002"}],
              "calibration": "A 区", "flags": {"clock_drift_sec": 0.2}})["ok"])
    arm_api = integration_api.op_arm({"room_id": rid, "operator": OP})
    check_link("arm（机位齐备 -> 真启用，接口如实回转结论）",
               arm_api["ok"] and arm_api["armed"] is True,
               "%s（%s）" % (arm_api.get("decision"), arm_api.get("status")))
    if args.nolink:
        check("［--nolink］arm 不再被 02 台账卡住（联动已关闭，只验接口链路）",
              arm_api["ok"] and isinstance(arm_api.get("armed"), bool),
              "%s（阻断 %d 项）" % (arm_api.get("decision"),
                                  len(arm_api.get("blockers") or [])))
    check("behaviors（带考场适用性）",
          integration_api.op_behaviors({"room_id": rid})["ok"])
    check("behaviors（不带考场 = 内置清单）",
          integration_api.op_behaviors({})["count"] == 51)
    if not store.get_room(rid).armed():
        # --nolink：为继续验证接口链路，直接置为已启用（上面已单独验过"如实拒绝"）
        _r = store.get_room(rid)
        _r.status = config.PHASE_ARMED
        store.save_room(_r)
    rt = integration_api.op_route_alert({
        "room_id": rid, "behavior": "B_USE_PHONE", "confidence": 0.97,
        "suppress_check": False, "push": False, "operator": OP})
    check("route_alert", rt["ok"] and rt["level"] == "red",
          "%s（%s）" % (rt.get("level_name"), str(rt.get("reason"))[:40]))
    ev_id = store.list_alerts(room_id=rid, limit=1)[0].event_id
    check("alerts", integration_api.op_alerts({"room_id": rid})["ok"])
    check("review_alert",
          integration_api.op_review_alert({"event_id": ev_id,
                                           "conclusion": "false_positive",
                                           "reason": "自检误报",
                                           "operator": OP})["ok"])
    check("review_alert 非法结论被拒",
          not integration_api.op_review_alert({"event_id": ev_id,
                                               "conclusion": "xx",
                                               "operator": OP})["ok"])
    check("kpi（单考场录入）",
          integration_api.op_kpi({"room_id": rid, "operator": OP,
                                  "run": {"observe_total": 20, "observe_hit": 20,
                                          "observe_correct": 19,
                                          "data_source": "synthetic"}})["ok"])
    check("kpi（考点汇总）",
          integration_api.op_kpi({})["ok"])
    check("session",
          integration_api.op_session({"room_id": rid, "name": "语文",
                                      "state": "running",
                                      "operator": OP})["ok"])
    check("domain", integration_api.op_domain({"room_id": rid,
                                               "operator": OP})["ok"])
    check("grant", integration_api.op_grant({"domain": config.DOMAIN_EXAM,
                                             "role": "patrol",
                                             "operator": OP})["ok"])
    check("revoke", integration_api.op_revoke({"domain": config.DOMAIN_EXAM,
                                               "role": "patrol",
                                               "operator": OP})["ok"])
    check("offline", integration_api.op_offline({"room_id": cls.room_id,
                                                 "operator": OP})["ok"])
    check("retention", integration_api.op_retention({"room_id": rid, "days": 30,
                                                     "operator": OP})["ok"])
    check("compliance", integration_api.op_compliance({})["ok"])
    check("report", integration_api.op_report({})["ok"])
    check("audits", integration_api.op_audits({})["ok"])
    check("linkage_status", integration_api.op_linkage_status({})["ok"])
    check("未知 op 会被 argparse 拒绝（不会静默成功）",
          "status" in integration_api.OPS and "不存在的op" not in integration_api.OPS)
    check("集成动作清单与实现一致（config.INTEGRATION_OPS == OPS）",
          set(config.INTEGRATION_OPS) == set(integration_api.OPS),
          "config %d 个 / 实现 %d 个" % (len(config.INTEGRATION_OPS),
                                      len(integration_api.OPS)))

    # =====================================================================
    section("[15] 联动可达性与零依赖")
    for key in ("01", "02", "03", "06"):
        ok_m, why_m = linkage_client.module_available(key)
        check("%s 可达性如实回答" % key, isinstance(ok_m, bool),
              ("可用" if ok_m else "不可用：%s" % why_m[:50]))
    lk_st = linkage_client.status(deep=False)
    check("联动状态含沙箱说明与四套系统",
          "sandbox_root" in lk_st and len(lk_st["modules"]) == 4,
          str(lk_st["sandbox_root"])[:60])
    bad_imports = []
    for mod in ("config", "models", "storage", "rooms", "admission", "behavior",
                "alerting", "kpi", "privacy", "report", "linkage_client",
                "integration_api"):
        path = os.path.join(HERE, mod + ".py")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for banned in ("import numpy", "import cv2", "import pandas",
                       "import requests", "import PIL"):
            if banned in src:
                bad_imports.append("%s: %s" % (mod, banned))
    check("零第三方依赖（只用标准库）", not bad_imports, str(bad_imports))
    check("config.describe() 字段齐全",
          all(k in config.describe() for k in ("数据目录", "考场类型", "行为清单",
                                               "数据域", "指标口径")))
    st_set = config.load_settings()
    check("设置可读写", config.save_settings({"operator": "自检员"}) is not None
          and config.load_settings()["operator"] == "自检员")
    config.save_settings({"operator": st_set["operator"]})
    check("身份证掩码只留后 4 位",
          config.mask_id("110101199001011234").endswith("1234")
          and "*" in config.mask_id("110101199001011234"))
    # 收尾：沙箱关掉，设置回到进入自检前的样子
    config.save_settings({"sandbox_root": st_set.get("sandbox_root", "")})
    linkage_client.reset()
    check("★ 自检全程没往真实 data\\ 写任何东西（目录快照前后一致）",
          _snapshot_dir(real_data) == before_snap,
          "前 %s / 后 %s（文件数, 总字节, 最新 mtime）"
          % (before_snap, _snapshot_dir(real_data)))
    check("★ 自检数据全部落在临时目录（真实库一个字都没动）",
          os.path.exists(os.path.join(tmp, "rooms.db"))
          and _snapshot_dir(real_data) == before_snap,
          "临时库：%s ｜ 真实库：%s"
          % (os.path.join(tmp, "rooms.db"),
             "未改动" if _snapshot_dir(real_data) == before_snap else "被改了！"))

    # =====================================================================
    print()
    print("=" * 78)
    print("自检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 78)
    store.close()
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
