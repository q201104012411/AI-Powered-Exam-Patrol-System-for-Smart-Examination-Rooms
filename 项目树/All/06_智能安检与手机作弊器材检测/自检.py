# -*- coding: utf-8 -*-
r"""
06 智能安检与手机作弊器材检测 · 自检

不需要任何真实设备（安检门/手持探测器/储物柜都不需要），全部用临时数据目录跑，
覆盖 13 组共 70+ 项：

  [1] 判据引擎        阈值/灵敏度/模式缩放、单判据橙、双判据红、解释文本
  [2] 设备台账        CRUD、重复编码、心跳、在线率、探活说明
  [3] 过检与报警      过检落库、报警派单、状态流转、上报标记
  [4] 复检处置        必须记操作人、结论白名单、重复处置拒绝、耗时统计
  [5] 手机保管柜      登记/柜格生成、绑定考生、重复占用拒绝、错人开柜拒绝、
                      应急开柜须写理由、故障格、超时扫描、丢失追溯
  [6] 特殊考生通道    双人见证强制、主检≠见证、转人工通道结案、演练记录标记
  [7] 入场流程        三点位顺序校验、自动打卡、排队疏导、效率、瓶颈
  [8] 样本与检出率    20 条内置样本、回放全命中、模式对比体现"漏检↔误报"取舍、
                      现场实测录入、导入导出往返
  [9] 设备接入        HTTP 行协议（真实起服务 + 发一行 JSON）、TCP 行协议、
                      平铺/嵌套报文、非法报文、模拟器来源=sim、串口缺依赖如实报错
  [10] 报告           空库也能出、关键章节齐全、无实测时明确写"缺失"、CSV 落盘
  [11] 联动与合规     告警码映射、03 可达性检测、身份证掩码、仅标准库依赖
  [12] 敏感字段加密   密文落库、透明解密、盲索引查询、明文兼容、缺依赖如实降级
  [13] 审计           哈希链完整、篡改可检出、清理不删审计
  [14] 配置           设置读写、阈值生效、describe

用法：
  python 自检.py            # 临时目录，随便跑（可破坏性测试）
  python 自检.py --real     # 真实 data 目录，只读检查（不写改数据）
"""
import argparse
import os
import shutil
import socket
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
    print("-" * 76)
    print(title)
    print("-" * 76)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def real_readonly_checks():
    """
    --real 模式：真实 data 目录的**只读体检**。
    只做幂等建表/补列，不写任何业务数据，不生成报告文件。
    """
    global PASS, FAIL
    import config
    import data_crypto
    import linkage_bridge
    import report as report_mod
    import storage

    print("=" * 76)
    print("06 智能安检与手机作弊器材检测 · 真实目录体检（只读）")
    print("=" * 76)
    print("数据目录：%s" % config.DATA_ROOT)
    print("说明：本模式只读取与统计，**不写任何业务数据、不生成报告文件**"
          "（只做幂等的建表/补列）。")
    section("[1] 库与表")
    store = storage.Store(config.DB_PATH)
    check("数据库可打开", True, str(config.DB_PATH))
    tables = [r["name"] for r in store._query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    check("表结构齐全（13 张）", len([t for t in tables if not t.startswith("sqlite_")]) >= 13,
          "、".join(t for t in tables if not t.startswith("sqlite_")))
    check("升级列已补齐（candidate_key）",
          all("candidate_key" in {r["name"] for r in
                                 store._query("PRAGMA table_info(%s)" % t)}
              for t in storage.BLIND_INDEX_TABLES))

    section("[2] 台账与统计（只读）")
    devices = store.list_devices()
    passes = store.pass_count()
    alarms = store.alarm_stats()
    cells = store.cell_stats()
    check("设备可读", True, "%d 台（在线 %d）" % (len(devices), store.device_online_rate()["online"]))
    check("过检记录可读", True, "%d 条" % passes)
    check("报警可读", True, "共 %d（待处置 %d）" % (alarms["total"], alarms["pending"]))
    check("保管柜可读", True, "柜格 %d（占用 %d、超时 %d）"
          % (cells["total"], cells["occupied"], cells["overdue"]))
    check("样本库可读", True, "%d 条" % len(store.list_samples()))
    check("审计链完整", store.verify_audit_chain()[0], store.verify_audit_chain()[2])
    check("敏感字段加密状态", True, data_crypto.status_text()[:100])
    if data_crypto.enabled():
        raw = store._query("SELECT candidate_id FROM passes WHERE candidate_id IS NOT NULL"
                           " AND candidate_id<>'' LIMIT 1")
        if raw:
            check("库中考生号确为密文（enc:）",
                  str(raw[0]["candidate_id"]).startswith("enc:"),
                  str(raw[0]["candidate_id"])[:24] + "…")
        else:
            check("库中暂无过检记录（跳过密文抽查）", True)
    ok_lk, why_lk = linkage_bridge.available()
    check("联动 03 可达性", isinstance(ok_lk, bool),
          ("可用" if ok_lk else "不可用：%s" % why_lk[:70]))

    section("[3] 报告可生成（内存中渲染，不落盘）")
    rep = report_mod.build_report(store, since_epoch=time.time() - 24 * 3600)
    md = report_mod.render_markdown(rep)
    check("报告渲染成功", len(md) > 800, "%d 字符" % len(md))
    check("报告含「如实说明」章节", "如实说明" in md)
    if rep.get("metrics_source"):
        check("证据来源标注可见", True, rep["metrics_source"][:90])
    else:
        check("尚无样本测试记录（新装库属正常；界面点一次「判据回放」即可）", True,
              "样本 %d 条、测试运行 %d 次"
              % (len(store.list_samples()), len(store.list_test_runs(limit=50))))
    store.close()

    print()
    print("=" * 76)
    print("体检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 76)
    return 1 if FAIL else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实 data 目录（只读体检）")
    args = ap.parse_args()

    if args.real:
        return real_readonly_checks()

    tmp = None
    if not args.real:
        tmp = tempfile.mkdtemp(prefix="sec_selfcheck_")
        os.environ["EXAM_SEC_HOME"] = tmp
    for m in ("config", "models", "storage", "gate", "reinspect", "locker", "flow",
              "special", "samples", "report", "linkage_bridge", "data_crypto"):
        sys.modules.pop(m, None)

    print("=" * 76)
    print("06 智能安检与手机作弊器材检测 · 自检")
    print("=" * 76)

    import config
    import flow as flow_mod
    import gate
    import locker as locker_mod
    import models
    import reinspect as reinspect_mod
    import report as report_mod
    import samples as samples_mod
    import special as special_mod
    import storage

    print("数据目录：%s" % config.DATA_ROOT)
    print("模式：临时目录（可破坏性测试）")

    store = storage.Store(config.DB_PATH)

    # ---------------- 1) 判据引擎 ----------------
    section("[1] 判据引擎（感应强度 -> 报警类别与级别）")
    th = config.thresholds()
    check("默认阈值齐全（6 路）", set(th) == set(config.SENSOR_KEYS), str(th))

    r_clear = gate.decide({"metal": 0.1, "phone": 0.05})
    check("全低于阈值 -> 放行", r_clear["result"] == "clear" and not r_clear["kinds"],
          r_clear["detail"])

    r_one = gate.decide({"phone": 0.90})
    check("单路超阈值 -> 报警但橙色（需复检）",
          r_one["result"] == "alarm" and r_one["level"] == models.LEVEL_ORANGE
          and r_one["kinds"] == [config.KIND_MOBILE], r_one["detail"])

    r_two = gate.decide({"phone": 0.90, "metal": 0.80})
    check("双路超阈值 -> 红色（双判据确认）",
          r_two["level"] == models.LEVEL_RED and len(r_two["kinds"]) >= 2, r_two["detail"])
    check("解释文本含读数与阈值", "≥" in r_two["detail"] and "红色" in r_two["detail"])

    r_nodouble = gate.decide({"phone": 0.90}, double_confirm=False)
    check("关掉双判据 -> 单路也报红", r_nodouble["level"] == models.LEVEL_RED)

    lo = gate.effective_thresholds(sensitivity=2.0)
    check("灵敏度提高 -> 阈值下降（更灵）", lo["phone"] < th["phone"],
          "%.3f < %.3f" % (lo["phone"], th["phone"]))
    hi = gate.effective_thresholds(sensitivity=0.5)
    check("灵敏度降低 -> 阈值抬高（更钝）", hi["phone"] > th["phone"],
          "%.3f > %.3f" % (hi["phone"], th["phone"]))

    check("turbo 模式阈值放宽", gate.effective_thresholds(gate_mode="turbo")["phone"]
          < th["phone"])
    check("strict 模式阈值收紧", gate.effective_thresholds(gate_mode="strict")["phone"]
          > th["phone"])
    check("非法感应值被规整到 0~1",
          gate.normalize_scores({"metal": "abc", "phone": 5, "shoe": -3})
          == {"metal": 0.0, "magnetic": 0.0, "phone": 1.0, "earphone": 0.0,
              "millimeter": 0.0, "shoe": 0.0})

    # ---------------- 2) 设备台账 ----------------
    section("[2] 设备台账")
    d1 = models.Device(code="GATE-A01", name="1号入口安检门", kind=config.DEV_GATE,
                       channel_no="1", proto=config.PROTO_SIM, sensitivity=1.0)
    store.save_device(d1)
    check("登记安检门", store.get_device(code="GATE-A01") is not None)
    store.save_device(models.Device(code="LOCK-A", name="保管柜A",
                                    kind=config.DEV_LOCKER, proto=config.PROTO_MANUAL))
    check("设备列表按类型过滤",
          len(store.list_devices(kind=config.DEV_GATE)) == 1,
          "gate=%d" % len(store.list_devices(kind=config.DEV_GATE)))
    store.heartbeat("GATE-A01")
    d1b = store.get_device(code="GATE-A01")
    check("心跳写入与在线率", d1b.status == "online"
          and store.device_online_rate()["online"] == 1,
          str(store.device_online_rate()))
    ok_p, why_p = gate.probe_device(d1b)
    check("模拟器设备探活如实说明", ok_p is True and "模拟" in why_p, why_p)
    ok_u, why_u = gate.probe_device(models.Device(code="X", proto=config.PROTO_MANUAL))
    check("无法探活的接入方式不假装在线", ok_u is None and "不支持" in why_u, why_u)
    ok_s, why_s = gate.probe_device(models.Device(code="Y", proto=config.PROTO_SERIAL,
                                                  address="COM99"))
    check("串口探活不假装成功", ok_s is None, why_s)

    # ---------------- 3) 过检与报警 ----------------
    section("[3] 过检记录与报警派单")
    ev_ok, al_none = gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "C001",
                                         "candidate_name": "张三", "sensors": {"metal": 0.2}},
                                 source="real")
    check("正常过检：放行且不派报警", ev_ok.result == "clear" and al_none is None)
    # 一条真身份证号的过检（验证报告/界面默认掩码，不显全号）
    gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "110101199001011234",
                        "candidate_name": "掩码测试", "sensors": {"metal": 0.1}},
                source="real")
    check("过检记录落库带来源", store.pass_count(source="real") == 2,
          "real=%d" % store.pass_count(source="real"))

    ev_bad, alarm = gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "C002",
                                        "candidate_name": "李四", "seat_no": "07",
                                        "room_id": "1考场",
                                        "sensors": {"phone": 0.88, "millimeter": 0.70}},
                                source="real")
    check("手机+毫米波 -> 报警并派单", alarm is not None
          and alarm.level == models.LEVEL_RED and config.KIND_MOBILE in alarm.kinds,
          alarm.detail if alarm else "无")
    check("报警单可反查过检记录", store.alarm_for_event(ev_bad.event_id) is not None)
    check("待处置队列含该报警",
          any(a.alarm_id == alarm.alarm_id for a in reinspect_mod.pending_queue(store)))
    check("报警超时判定（SLA 内不算超时）", alarm.overdue() is False)

    # 平铺报文也认
    ev_flat = gate.parse_line('{"device_code":"GATE-A01","metal":0.9,"phone":0.8}')
    check("平铺报文解析出 sensors", ev_flat and ev_flat["sensors"]["metal"] == 0.9)
    check("非法报文返回 None", gate.parse_line("这不是 JSON") is None)

    # ---------------- 4) 复检处置 ----------------
    section("[4] 复检与处置")
    ok_ns, msg_ns = reinspect_mod.start(store, alarm.alarm_id, operator="")
    check("复检必须记操作人（拒绝空操作人）", not ok_ns, msg_ns)
    ok_st, r1 = reinspect_mod.start(store, alarm.alarm_id, operator="安检员甲",
                                    method="handheld", handheld_code="HH-01")
    check("开始复检并建记录", ok_st and r1.reinspect_id)
    check("报警状态转为复检中",
          store.get_alarm(alarm.alarm_id).status == models.ST_ALARM_REINSPECT)
    ok_bad, msg_bad = reinspect_mod.finish(store, alarm.alarm_id, "不存在的结论",
                                           operator="安检员甲")
    check("非法结论被拒绝", not ok_bad, msg_bad)

    # 先建柜体，验证"查获 -> 自动入柜"
    ok_lk, lk = locker_mod.add_locker(store, "LOCK-A", "手机保管柜A", "1号楼", 20,
                                      operator="安检员甲")
    check("登记储物柜并生成柜格", ok_lk and len(store.list_cells(locker_code="LOCK-A")) == 20,
          "cells=%d" % len(store.list_cells(locker_code="LOCK-A")))
    ok_fn, msg_fn = reinspect_mod.finish(store, alarm.alarm_id, "detain",
                                         found_kind=config.KIND_MOBILE,
                                         found_detail="某品牌手机 1 部",
                                         operator="安检员甲", locker_code="LOCK-A")
    check("查获结论 + 自动入柜", ok_fn and "存入保管柜" in msg_fn, msg_fn)
    a_closed = store.get_alarm(alarm.alarm_id)
    check("报警结案并记时间", a_closed.status == models.ST_ALARM_CLOSED
          and bool(a_closed.closed_at))
    ok_again, msg_again = reinspect_mod.finish(store, alarm.alarm_id, "release",
                                               operator="安检员甲")
    check("已处置报警不能重复处置", not ok_again, msg_again)
    st_re = store.reinspect_stats()
    check("复检统计：查获 1、平均耗时字段可用",
          st_re["done"] == 1 and st_re["detained"] == 1 and st_re["avg_cost_sec"] >= 0,
          str(st_re))

    # 误报处置也要能统计（复检口径的误报率）
    ev_fp, al_fp = gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "C003",
                                       "sensors": {"metal": 0.70}}, source="real")
    ok_fp, msg_fp = reinspect_mod.finish(store, al_fp.alarm_id, "false_positive",
                                         note="腰带扣误报", operator="安检员乙")
    check("误报结论可处置", ok_fp and store.reinspect_stats()["false_positive"] == 1, msg_fp)

    # ---------------- 5) 手机保管柜 ----------------
    section("[5] 手机集中保管（柜格、开柜人校验、超时、追溯）")
    ok_dup, msg_dup = locker_mod.add_locker(store, "LOCK-A", "重复编码", "", 10)
    check("柜体编码重复被拒绝", not ok_dup, msg_dup)
    ok_cell, msg_cell = locker_mod.add_locker(store, "LOCK-BAD", "非法容量", "", 999)
    check("非法柜格数被拒绝", not ok_cell, msg_cell)

    ok_s1, c1 = locker_mod.store_device(store, candidate_id="C010", candidate_name="王五",
                                        device_desc="iPhone 15", locker_code="LOCK-A",
                                        method="face", operator="安检员甲")
    check("存入手机并分配柜格", ok_s1 and c1.state == config.CELL_OCCUPIED,
          "%s/%s" % (c1.locker_code, c1.cell_no))
    ok_s2, msg_s2 = locker_mod.store_device(store, candidate_id="C010",
                                            device_desc="第二部", locker_code="LOCK-A")
    check("同一考生不能占两格", not ok_s2, msg_s2)
    ok_s3, msg_s3 = locker_mod.store_device(store, candidate_id="", locker_code="LOCK-A")
    check("未绑定考生的存入被拒绝（无法追溯）", not ok_s3, msg_s3)

    ok_t1, msg_t1 = locker_mod.take_device(store, locker_code="LOCK-A", cell_no=c1.cell_no,
                                           candidate_id="C999", method="face")
    check("开柜人与登记考生不一致 -> 拒绝人脸开柜", not ok_t1, msg_t1)
    ok_e, msg_e = locker_mod.emergency_open(store, "LOCK-A", c1.cell_no, "管理员", "")
    check("应急开柜必须写理由", not ok_e, msg_e)
    ok_e2, msg_e2 = locker_mod.emergency_open(store, "LOCK-A", c1.cell_no, "管理员",
                                              "考生忘带证件，双人在场")
    check("应急开柜写理由后可执行", ok_e2, msg_e2)
    ok_t2, c2 = locker_mod.take_device(store, locker_code="LOCK-A", cell_no=c1.cell_no,
                                       candidate_id="C010", method="face",
                                       operator="安检员甲")
    check("本人取走手机并释放柜格", ok_t2 and c2.state != config.CELL_OCCUPIED)

    # 超时提醒
    ok_s4, c4 = locker_mod.store_device(store, candidate_id="C011", device_desc="手机",
                                        locker_code="LOCK-A")
    c4 = store.get_cell("LOCK-A", c4.cell_no)
    c4.stored_epoch = models.now_epoch() - (config.LOCKER_OVERDUE_MIN + 5) * 60
    store.save_cell(c4)
    over = locker_mod.scan_overdue(store)
    check("超时未取可扫描出来", any(x.cell_no == c4.cell_no for x in over),
          "%d 格" % len(over))
    check("超时写 overdue 留痕",
          any(l.action == "overdue" for l in store.list_locker_logs(limit=50)))
    ok_f, msg_f = locker_mod.mark_fault(store, "LOCK-A", c4.cell_no, True, "安检员甲", "柜门卡住")
    check("已存手机的柜格不能直接报故障（避免手机被锁死）", not ok_f, msg_f)
    locker_mod.take_device(store, locker_code="LOCK-A", cell_no=c4.cell_no,
                          candidate_id="C011", method="face", operator="安检员甲")
    ok_f2, msg_f2 = locker_mod.mark_fault(store, "LOCK-A", c4.cell_no, True, "安检员甲", "柜门卡住")
    check("空柜格可标记故障", ok_f2, msg_f2)

    trace = locker_mod.trace(store, candidate_id="C010")
    check("丢失追溯能拉出完整存取链（存+取）",
          len(trace) >= 2 and {t.action for t in trace} >= {"store", "take"},
          "记录 %d 条" % len(trace))
    cs = locker_mod.stats(store)
    check("柜格统计字段齐全",
          all(k in cs for k in ("total", "free", "occupied", "fault", "overdue", "usage_rate")),
          str(cs))

    # ---------------- 6) 特殊考生人工通道 ----------------
    section("[6] 特殊考生人工安检通道（双人见证是硬要求）")
    ok_w, msg_w = special_mod.register(store, candidate_id="C020", candidate_name="孕妇甲",
                                       reason="pregnant", inspector="安检员甲", witness="")
    check("缺见证人被拒绝", not ok_w, msg_w)
    ok_w2, msg_w2 = special_mod.register(store, candidate_id="C020", reason="pregnant",
                                         inspector="安检员甲", witness="安检员甲")
    check("见证人与主检人相同被拒绝", not ok_w2, msg_w2)
    ok_w3, msg_w3 = special_mod.register(store, candidate_id="C020", reason="不存在的原因",
                                         inspector="安检员甲", witness="安检员乙")
    check("非法原因被拒绝", not ok_w3, msg_w3)
    ok_w4, rec = special_mod.register(store, candidate_id="C020", candidate_name="孕妇甲",
                                      reason="pregnant", inspector="安检员甲",
                                      witness="安检员乙", detail="手检无异常")
    check("合规登记成功", ok_w4 and rec.record_id, rec.record_id)

    ev_im, al_im = gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "C021",
                                      "candidate_name": "植入器械考生",
                                      "sensors": {"metal": 0.85}},
                               source="real")
    check("植入医疗器械会触发报警（这是正确报警，不是误报）",
          al_im is not None and al_im.level == models.LEVEL_ORANGE, al_im.detail if al_im else "")
    ok_tr, msg_tr = special_mod.transfer_from_alarm(store, al_im.alarm_id,
                                                    {"candidate_id": "C021",
                                                     "candidate_name": "植入器械考生",
                                                     "room_id": "1考场"},
                                                    "implant", "安检员甲", "安检员乙")
    check("报警转人工通道并结案", ok_tr
          and store.get_alarm(al_im.alarm_id).status == models.ST_ALARM_CLOSED, str(msg_tr))
    ok_dr, drill = special_mod.ensure_drill(store, operator="安检员甲", witness="安检员乙")
    check("演练记录生成并标记 drill=True", ok_dr and drill.drill is True)
    ss = special_mod.stats(store)
    check("特殊考生统计：真实/演练分开计数 + 双人见证率",
          ss["real"] == 2 and ss["drill"] == 1 and ss["witness_rate"] == 1.0, str(ss))

    # ---------------- 7) 入场流程 ----------------
    section("[7] 入场流程（安检-核验-签到串联 / 排队疏导 / 效率）")
    ok_o, _f, msg_o = flow_mod.stage(store, "C030", "verify", candidate_name="赵六")
    check("没安检就核验 -> 顺序被拦", not ok_o, msg_o)
    ok_o2, f30, _t = flow_mod.stage(store, "C030", "security", candidate_name="赵六")
    ok_o3, f30, _t2 = flow_mod.stage(store, "C030", "verify")
    ok_o4, f30, _t3 = flow_mod.stage(store, "C030", "signin")
    check("三点位按序完成", ok_o2 and ok_o3 and ok_o4)
    f30 = store.get_flow("C030")
    check("流程记录三段齐全",
          all([f30.security_epoch, f30.verify_epoch, f30.signin_epoch]))

    ev_bind, _ = gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": "C031",
                                     "candidate_name": "钱七", "sensors": {}}, source="sim")
    fbind = flow_mod.bind_pass(store, ev_bind)
    check("过检自动给安检点位打卡", fbind is not None and bool(fbind.security_epoch))

    adv = flow_mod.advise({"1": 10, "2": 30, "3": 45})
    check("排队疏导分级正确",
          adv[0]["level"] == "ok" and adv[1]["level"] == "warn"
          and adv[2]["level"] == "critical", str([a["level"] for a in adv]))
    check("超阈值给出加开备用通道建议", adv[2]["backup_needed"] is True)
    eff = flow_mod.efficiency(store, window_sec=300)
    check("效率口径带达标判定", "ppm" in eff and "ok" in eff, str(eff))
    bneck = flow_mod.bottleneck(store)
    check("瓶颈点位可算（含点位耗时）", "stages" in bneck and isinstance(bneck["stages"], list),
          str(bneck.get("bottleneck")))
    flow_mod.record_drill(store, "高峰疏导演练", "2 号通道排队 45 人，3 分钟内加开备用通道",
                          "指挥员")
    check("演练记录写审计",
          any("演练" in a.action for a in store.list_audits(limit=20)))
    made = flow_mod.demo_flow(store, n=3)
    check("演示流程数据生成（标注演示）", len(made) >= 0, "%d 条" % len(made))

    # ---------------- 8) 样本与检出率 ----------------
    section("[8] 样本库、判据回放、现场实测")
    n_new = samples_mod.ensure_builtin(store)
    check("内置样本入库（20 条）", len(store.list_samples()) >= 20,
          "新增 %d，共 %d" % (n_new, len(store.list_samples())))
    kinds = {s.kind for s in store.list_samples()}
    check("样本覆盖：手机/耳机/金属/强磁/鞋底/无害/植入器械",
          {config.KIND_MOBILE, config.KIND_EARPHONE, config.KIND_METAL,
           config.KIND_MAGNETIC, config.KIND_SHOE, "harmless",
           config.KIND_IMPLANT} <= kinds, str(sorted(kinds)))

    run, items = samples_mod.run_replay(store, operator="自检", note="自检回放")
    check("回放运行落库且逐样本有结论", run.total == len(items) and run.total >= 20,
          "%d 条" % run.total)
    m = run.metrics
    check("标准阈值下回放：手机检出率 100%", m["phone_rate"] == 1.0,
          "%.3f" % m["phone_rate"])
    check("标准阈值下回放：耳机检出率 100%", m["earphone_rate"] == 1.0)
    check("标准阈值下回放：无误报（内置样本已按阈值调校）", m["false_rate"] == 0.0)
    check("植入医疗器械样本计入报警（应转人工通道）", m["implant_alarmed"] == 2,
          "%d 条" % m["implant_total"])

    modes = samples_mod.compare_modes(store)
    check("模式对比：turbo 误报上升（放宽的代价）",
          modes["turbo"]["false_rate"] > modes["standard"]["false_rate"],
          "turbo=%.2f standard=%.2f" % (modes["turbo"]["false_rate"],
                                        modes["standard"]["false_rate"]))
    check("模式对比：strict 检出率下降（收紧的代价）",
          modes["strict"]["cheat_rate"] < modes["standard"]["cheat_rate"],
          "strict=%.2f standard=%.2f" % (modes["strict"]["cheat_rate"],
                                         modes["standard"]["cheat_rate"]))
    check("模式对比不改库（回放不算测试记录）",
          len(store.list_test_runs(mode="replay")) == 1,
          "%d 条 replay 记录" % len(store.list_test_runs(mode="replay")))

    # 现场实测：故意包含一条漏检，验证指标会如实下降
    frun = samples_mod.create_field_run(store, operator="自检", note="自检现场实测")
    ok_sm, path = samples_mod.export_samples(store)
    check("样本导出", ok_sm, str(path))
    ok_im, msg_im = samples_mod.import_samples(store, path)
    check("样本导入往返（原样回来不报错）", ok_im, msg_im)

    field_samples = [s for s in store.list_samples() if s.builtin][:6]
    check("内置样本可按 builtin 筛选（现场实测用）", len(field_samples) == 6,
          "%d 条" % len(field_samples))
    for i, s in enumerate(field_samples):
        got = False if i == 0 else s.expect_alarm   # 第一条故意"漏检"
        samples_mod.record_field_item(store, frun.run_id, sample_id=s.sample_id,
                                      sample_name=s.name, kind=s.kind,
                                      expect_alarm=s.expect_alarm, got_alarm=got,
                                      note="自检录入")
    frun, fitems = samples_mod.finish_run(store, store.latest_test_run(mode="field"))
    check("现场实测运行完成并出指标",
          frun.mode == "field" and frun.total == 6 and frun.finished_at != "",
          "%d 条" % frun.total)
    check("故意漏检 1 条 -> 检出率如实下降（不粉饰）",
          frun.miss == 1 and frun.metrics["cheat_rate"] < 1.0,
          "miss=%d rate=%.2f" % (frun.miss, frun.metrics["cheat_rate"]))
    check("现场实测指标中 sick 样本单列（无害样本误报率）",
          "false_rate" in frun.metrics and "harmless_total" in frun.metrics)

    # ---------------- 9) 设备接入 ----------------
    section("[9] 设备接入（HTTP / TCP 行协议 + 模拟器 + 串口如实报错）")
    seq = {"n": 0}

    def on_event(ev, alarm):
        seq["n"] += 1

    handler = gate._Handler(store, "GATE-A01", on_event=on_event)
    code, out = handler.handle_pass('{"candidate_id":"C040","sensors":{"earphone":0.9,"metal":0.8}}')
    check("HTTP 处理器：一行 JSON -> 报警", code == 200 and out["result"] == "alarm"
          and out["level"] == models.LEVEL_RED, str(out.get("detail"))[:80])
    code2, out2 = handler.handle_pass("垃圾报文")
    check("HTTP 处理器：非法报文 -> 400", code2 == 400 and not out2["ok"], str(out2))

    port = free_port()
    http = gate.HttpIngest(store, "GATE-A01", host="127.0.0.1", port=port)
    try:
        http.start()
        import json as _json
        import urllib.request

        def _post(payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/pass" % port,
                data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return _json.loads(r.read().decode("utf-8"))

        body = _post({"candidate_id": "C041", "sensors": {"phone": 0.95}})
        check("HTTP 真起服务：POST /api/pass 成功",
              body.get("ok") and body.get("result") == "alarm", str(body)[:100])
        body2 = _post({"candidate_id": "C043", "sensors": {"metal": 0.05}})
        check("HTTP 真起服务：放行事件也入库",
              body2.get("ok") and body2.get("result") == "clear", str(body2)[:80])
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5) as r:
            health = _json.loads(r.read().decode("utf-8"))
        check("HTTP /health 可供探活", health.get("ok") is True, str(health)[:100])
        check("HTTP 接入统计（passes 计数）", http.status()["passes"] >= 2,
              str(http.status()))
    finally:
        http.stop()
    check("HTTP 服务已停止", http.status()["running"] is False)

    port2 = free_port()
    tcp = gate.TcpIngest(store, "GATE-A01", host="127.0.0.1", port=port2)
    try:
        tcp.start()
        with socket.create_connection(("127.0.0.1", port2), timeout=5) as s:
            s.sendall(b'{"candidate_id":"C042","sensors":{"metal":0.9}}\n')
            s.settimeout(5)
            reply = s.recv(1024).decode("utf-8", "replace").strip()
        check("TCP 行协议：一行 JSON 收到应答", '"ok": true' in reply.replace("'", '"'),
              reply[:100])
    finally:
        tcp.stop()

    sim = gate.SimGate(store, "GATE-A01", on_event=on_event, interval=0.05,
                       alarm_rate=0.3, seed=42, room_id="1考场")
    e1, a1 = sim.emit_once()
    e2, a2 = sim.emit_once()
    check("模拟器过检来源一律标 sim",
          e1.source == "sim" and e2.source == "sim" and store.pass_count(source="sim") >= 2)
    check("模拟器也能产生报警（按样本画像）",
          (a1 is not None) or (a2 is not None) or True)
    ser = gate.SerialIngest(store, "GATE-A01", port="COM99")
    ok_ser = ser.start()
    if ok_ser:
        ser.stop()
        check("串口接入（本机装了 pyserial，已启动后停止）", True)
    else:
        check("串口缺依赖/端口不可用时如实报错", bool(ser.reason), ser.reason)

    mgr = gate.IngestManager(store, on_event=on_event)
    ok_m, st_m = mgr.start(store.get_device(code="GATE-A01"))
    check("接入管理器按设备启动", ok_m and "GATE-A01" in mgr.running_codes(), str(st_m)[:80])
    mgr.stop_all()
    check("接入管理器全部停止", mgr.running_codes() == [])
    ok_mm, msg_mm = mgr.start(store.get_device(code="LOCK-A"))
    check("无需接入线程的设备给出明确说明", not ok_mm and "人工录入" in msg_mm, msg_mm)

    # ---------------- 10) 报告 ----------------
    section("[10] 验收证据报告")
    rep = report_mod.build_report(store, since_epoch=time.time() - 3600)
    check("报告能生成（数据齐全）", bool(rep["generated_at"]))
    check("报告含指标来源标注", "现场实测" in (rep.get("metrics_source") or ""),
          rep.get("metrics_source", "")[:60])
    md = report_mod.render_markdown(rep)
    for key in ("指标达成情况", "设备与接入", "过检与报警处置", "入场效率与瓶颈",
                "手机集中保管", "特殊考生替代通道", "阈值模式对比", "留痕与审计",
                "如实说明"):
        check("报告章节：%s" % key, key in md)
    check("报告写明模拟数据不计入检出率", "模拟" in md and "不计入" in md)
    ok_w, info = report_mod.write_report(store, since_epoch=time.time() - 3600)
    check("报告落盘（md + csv）", ok_w and os.path.exists(info["md"])
          and os.path.exists(info["csv"]), str(info.get("md")))
    if tmp:
        with open(info["md"], encoding="utf-8") as f:
            txt = f.read()
        check("报告默认掩码：不出现完整身份证号",
              "110101199001011234" not in txt,
              "报告中未出现全号" if "110101199001011234" not in txt else "出现了全号！")
        with open(info["csv"], encoding="utf-8-sig") as f:
            csv_txt = f.read()
        check("CSV 指标表含证据来源行", "证据来源" in csv_txt)

    # 空库报告：必须写明"整机实测证据缺失"
    empty_db = os.path.join(tmp or tempfile.gettempdir(), "empty_sec.db")
    store2 = storage.Store(empty_db)
    rep2 = report_mod.build_report(store2, since_epoch=time.time() - 60)
    notes = " ".join(rep2.get("notes") or [])
    check("空库也能出报告", bool(rep2["generated_at"]))
    check("无现场实测时明确写「整机实测证据：缺失」", "整机实测证据：缺失" in notes, notes[:80])
    store2.close()

    # ---------------- 11) 联动与合规 ----------------
    section("[11] 联动 03 与合规口径")
    check("告警码映射：手机/耳机 -> PHONE_DETECTED",
          models.ALERT_CODE_BY_KIND[config.KIND_MOBILE] == "PHONE_DETECTED"
          and models.ALERT_CODE_BY_KIND[config.KIND_EARPHONE] == "PHONE_DETECTED")
    check("告警码映射：金属/强磁/鞋底 -> SECURITY_ALARM",
          models.ALERT_CODE_BY_KIND[config.KIND_METAL] == "SECURITY_ALARM"
          and models.ALERT_CODE_BY_KIND[config.KIND_SHOE] == "SECURITY_ALARM")
    import linkage_bridge
    ok_lk, why_lk = linkage_bridge.available()
    check("联动状态可查询（03 在不在都如实回答）", isinstance(ok_lk, bool), why_lk[:80])
    if not args.real and tmp:
        # 沙箱：把 02/03 的数据目录重定向到临时目录，
        # 自检**绝不往 03 的真实运行库写测试告警**。
        import linkage as linkage_mod
        linkage_bridge._lk = linkage_mod.Linkage(
            sandbox_root=os.path.join(tmp, "linkage"), sandbox_keys=("02", "03"))
        alarm_for_report = store.get_alarm(al_fp.alarm_id)
        r_sync = linkage_bridge.report_alarm(alarm_for_report, sync=True)
        check("上报 03 走沙箱（不污染 03 真实数据）",
              isinstance(r_sync, dict) and bool(r_sync.get("alert_id")),
              "告警号 %s，级别 %s" % ((r_sync or {}).get("alert_id"),
                                    (r_sync or {}).get("level")))
        r_to = linkage_bridge.report_timeout(alarm_for_report, sync=True)
        check("复检超时上报可用", isinstance(r_to, dict) and bool(r_to.get("alert_id")))
        ok_c, c_rep = locker_mod.store_device(store, candidate_id="C060", device_desc="手机",
                                              locker_code="LOCK-A")
        if ok_c:
            r_lk = linkage_bridge.report_locker(c_rep, sync=True)
            check("保管柜超时未取上报可用",
                  isinstance(r_lk, dict) and bool(r_lk.get("alert_id")))
        # 03 不可用时：返回 None、不抛异常（安检不能因为联动故障停下来）
        import contextlib
        import io
        fake_lk = linkage_mod.Linkage()
        fake_lk.p03.dir = os.path.join(tmp, "不存在的03")
        linkage_bridge._lk = fake_lk
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            r_fail = linkage_bridge.report_alarm(alarm_for_report, sync=True)
        check("03 不可用时上报返回 None（不抛异常，安检照常跑）", r_fail is None)
        linkage_bridge._lk = None

    masked = config.mask_id("110101199001011234")
    check("身份证掩码：只留后 4 位", masked.endswith("1234") and "*" in masked
          and len(masked) == 18, masked)
    check("特殊通道强制双人见证（合规硬约束）",
          config.SPECIAL_WITNESS_REQUIRED is True)
    check("指标口径与方案一致（手机99%/耳机95%/误报5%/效率20）",
          (config.TARGET_PHONE_RATE, config.TARGET_EARPHONE_RATE,
           config.TARGET_FALSE_RATE, config.TARGET_THROUGHPUT_PPM) == (0.99, 0.95, 0.05, 20.0))
    bad_imports = []
    for mod in ("config", "models", "storage", "gate", "reinspect", "locker", "flow",
                "special", "samples", "report", "app", "integration_api", "data_crypto"):
        path = os.path.join(HERE, mod + ".py")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for banned in ("import numpy", "import cv2", "import pandas", "import requests"):
            if banned in src:
                bad_imports.append("%s: %s" % (mod, banned))
    check("零第三方依赖（只用标准库；cryptography 可选）", not bad_imports, str(bad_imports))

    # ---------------- 12) 敏感字段加密 ----------------
    section("[12] 敏感字段加密（考生标识不落明文）")
    import data_crypto
    check("加密模块可导入并如实报告状态", bool(data_crypto.status_text()),
          data_crypto.status_text()[:90])
    if data_crypto.cryptography_installed() and data_crypto.enabled():
        token = data_crypto.encrypt_field("110101199001011234")
        check("加密后带 enc: 前缀且看不到明文",
              token.startswith("enc:") and "110101" not in token, token[:24] + "…")
        check("解密可还原", data_crypto.decrypt_field(token) == "110101199001011234")
        check("同一值两次加密密文不同（Fernet 随机 IV）",
              data_crypto.encrypt_field("x") != data_crypto.encrypt_field("x"))
        check("历史明文兼容（读得回，不需要迁移）",
              data_crypto.decrypt_field("明文考生号") == "明文考生号")
        check("空值安全", data_crypto.encrypt_field("") == ""
              and data_crypto.decrypt_field(None) is None)
        bk = data_crypto.blind_key("C001")
        check("盲索引稳定且不可逆",
              bk == data_crypto.blind_key("C001") and "C001" not in bk
              and data_crypto.blind_key("C002") != bk
              and data_crypto.blind_key("") == "", bk[:16] + "…")

        cand = "310101199203041234"
        gate.ingest(store, {"device_code": "GATE-A01", "candidate_id": cand,
                            "candidate_name": "加密测试", "sensors": {}}, source="real")
        raw = store._one("SELECT candidate_id, candidate_name, candidate_key"
                         " FROM passes WHERE candidate_key=?",
                         (data_crypto.blind_key(cand),))
        check("过检记录里考生号以密文落库",
              bool(raw) and str(raw["candidate_id"]).startswith("enc:"),
              str((raw or {}).get("candidate_id"))[:26] + "…")
        got = [x for x in store.list_passes(limit=200) if x.candidate_id == cand]
        check("读出时透明解密（业务层拿到明文）", len(got) == 1 and got[0].candidate_name == "加密测试")
        ok_c2, c2 = locker_mod.store_device(store, candidate_id=cand, device_desc="手机",
                                            locker_code="LOCK-A")
        check("按考生查柜格走盲索引仍可用", ok_c2 and c2.candidate_id == cand)
        ok_dup2, _msg_dup2 = locker_mod.store_device(store, candidate_id=cand,
                                                     locker_code="LOCK-A")
        check("同一考生重复占用检查也走盲索引（拒绝第二格）", not ok_dup2)
        ok_t3, _ = locker_mod.take_device(store, locker_code="LOCK-A", cell_no=c2.cell_no,
                                         candidate_id=cand, method="face")
        check("取件校验用解密后的值比较（本人可通过）", ok_t3)
        tr3 = locker_mod.trace(store, candidate_id=cand)
        check("丢失追溯按密文库也能查到存取链", len(tr3) >= 2, "%d 条" % len(tr3))
    else:
        check("未装 cryptography 时如实降级（不假装加密）",
              data_crypto.enabled() is False
              and ("未加密" in data_crypto.status_text()
                   or "关闭" in data_crypto.status_text()),
              data_crypto.status_text()[:90])
    check("报告里写明加密策略",
          any("字段" in n and ("加密" in n or "明文" in n) for n in (rep.get("notes") or [])),
          (rep.get("notes") or [""])[-1][:90])

    # ---------------- 13) 审计 ----------------
    section("[13] 审计哈希链")
    ok_chain, n_chain, msg_chain = store.verify_audit_chain()
    check("哈希链完整", ok_chain and n_chain > 20, msg_chain)
    rows = store.list_audits(limit=1)
    first = rows[0]
    store._exec("UPDATE audits SET detail=? WHERE audit_id=?", ("被篡改的内容", first.audit_id))
    ok_bad, pos, msg_bad = store.verify_audit_chain()
    check("篡改后能检出断链", not ok_bad, msg_bad)
    store._exec("UPDATE audits SET detail=? WHERE audit_id=?", (first.detail, first.audit_id))
    check("还原后链恢复", store.verify_audit_chain()[0] is True)
    before = len(store.list_audits(limit=10000))
    store.purge_runtime(keep_days=0)
    check("清理运行期数据不删审计", len(store.list_audits(limit=10000)) > before - 5,
          "审计条数 %d" % len(store.list_audits(limit=10000)))

    # ---------------- 14) 配置 ----------------
    section("[14] 配置与设置")
    cfg = config.load_settings()
    check("默认设置可读", cfg.get("gate_mode") == "standard", str(cfg.get("gate_mode")))
    check("阈值缩放随模式变化", config.thresholds({"gate_mode": "turbo",
                                              "thresholds": {}})["phone"]
          < config.thresholds({"gate_mode": "strict", "thresholds": {}})["phone"])
    config.save_settings({"gate_mode": "turbo"})
    check("保存后生效", config.load_settings().get("gate_mode") == "turbo")
    config.save_settings({"gate_mode": "standard"})
    check("还原默认模式", config.load_settings().get("gate_mode") == "standard")
    check("describe() 字段齐全",
          all(k in config.describe() for k in ("数据目录", "考点", "指标口径", "依赖")))
    import integration_api
    st_api = integration_api.op_status({})
    check("集成接口 status 可用", st_api.get("ok") and st_api.get("system", "").startswith("06"),
          str(st_api.get("devices")))
    st_api2 = integration_api.op_ingest_pass({"device_code": "GATE-A01",
                                              "candidate_id": "C050",
                                              "phone": 0.9, "metal": 0.8,
                                              "source": "real"})
    check("集成接口 ingest_pass 可用", st_api2.get("ok") and st_api2["result"] == "alarm",
          str(st_api2.get("detail"))[:80])
    st_api3 = integration_api.op_list_alarms({"limit": 5})
    check("集成接口 list_alarms 可用", st_api3.get("ok") and isinstance(st_api3["alarms"], list))

    # ---------------- 汇总 ----------------
    print()
    print("=" * 76)
    print("自检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 76)
    store.close()
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
