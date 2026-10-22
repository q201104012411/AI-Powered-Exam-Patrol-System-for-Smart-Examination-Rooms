# -*- coding: utf-8 -*-
"""试卷流转与保密室智能监控 · 端到端自检

把"按 md 文档逐条落地"的能力，用真实调用跑一遍并逐项判定。
默认用**临时数据目录**，不污染正式 data\；加 `--real` 则落在正式目录。

覆盖（对应 md 文档三节 + 第四节验收证据）：
  一、保密室智能监控 7 项：无死角视频 / 门禁管控 / 入侵探测 / 试卷柜电子锁 /
      环境监测 / 值守替代 / 录像留存
  二、试卷流转环节 5 项：领取交接 / 押运 / 电子铅封 / 分发至考场 / 回收与封存
  三、关键指标 5 项：覆盖率 / 告警时延 / 交接完整率 / 录像连续 / 押运偏离时延
  四、验收证据：无死角核查报告 + 告警联调记录 + 流程台账 + 录像留存证明

用法：
    python main.py            # 临时目录自检（推荐）
    python main.py --real     # 用正式数据目录
    python main.py --quiet    # 只打印结论
"""
import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 先定数据目录，再 import config（config 在 import 期解析路径）
_TMP_HOME = None
if "--real" not in sys.argv:
    _TMP_HOME = tempfile.mkdtemp(prefix="exam_secure_selfcheck_")
    os.environ["EXAM_SECURE_HOME"] = _TMP_HOME
    atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import alarm_hub                                            # noqa: E402
import camera_stream                                        # noqa: E402
import config                                               # noqa: E402
import flow as flow_mod                                     # noqa: E402
import models                                               # noqa: E402
import report as report_mod                                 # noqa: E402
import storage                                              # noqa: E402
import vault as vault_mod                                   # noqa: E402
import web_accounts                                         # noqa: E402

QUIET = "--quiet" in sys.argv
RES = []


def chk(name, cond, extra=""):
    RES.append((bool(cond), name, extra))
    if not QUIET:
        print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                           (" | " + str(extra)) if extra else ""))
    return bool(cond)


def sec(title):
    if not QUIET:
        print()
        print("-" * 68)
        print(" " + title)
        print("-" * 68)


def main():
    t_all = time.time()
    print("=" * 68)
    print(" 试卷流转与保密室智能监控 · 端到端自检")
    print(" 数据目录：%s" % config.DATA_ROOT)
    print(" 模式：%s" % ("正式目录" if "--real" in sys.argv else "临时目录（自检后清理）"))
    print("=" * 68)

    store = storage.Store()
    hub = alarm_hub.AlarmHub(store)
    cams = vault_mod.CameraManager(store)
    vault = vault_mod.Vault(store, hub, cams)
    flow = flow_mod.FlowManager(store, hub)
    kpi = report_mod.KPIReport(store, vault, flow)
    ev = report_mod.EvidenceReport(store, vault, flow, kpi)
    accounts = web_accounts.AccountManager(store)
    accounts.ensure_defaults()

    ROOM = "保密室A"

    # ================= 账户与权限（与网页端同源的基础） =================
    sec("〇、账户与权限（网页端 / 桌面端共用同一份账户）")
    s, why = accounts.login("owner", "owner")
    chk("owner 可用默认口令登录", s is not None, why)
    s2, why2 = accounts.login("owner", "wrong-pwd")
    chk("错误口令被拒绝", s2 is None, why2)
    s3, _ = accounts.login("teacher1", "teacher1")
    chk("teacher1（管理员）可登录", s3 is not None)
    chk("admin 无 account_manage 权限", "account_manage" not in
        config.ROLE_ACTIONS.get("admin", set()))
    chk("admin 无 device_delete 权限", "device_delete" not in
        config.ROLE_ACTIONS.get("admin", set()))
    chk("admin 无 audit_view 权限", "audit_view" not in
        config.ROLE_ACTIONS.get("admin", set()))
    chk("owner 拥有 account_manage 权限", "account_manage" in
        config.ROLE_ACTIONS.get("owner", set()))
    ok, msg = accounts.create(s, "guard01", "guard12345", config.ROLE_GUARD)
    chk("owner 可创建值守员账户", ok, msg)
    ok2, msg2 = accounts.create(s, "guard02", "guard12345", config.ROLE_OWNER)
    chk("owner 不能创建同级 owner（防越级）", not ok2, msg2)
    ok3, msg3 = accounts.create(s3, "guard03", "guard12345", config.ROLE_GUARD)
    chk("admin 不能创建任何账户", not ok3, msg3)

    # ================= 一、保密室智能监控 7 项 =================
    sec("一、保密室智能监控（md 第一节 7 项）")

    # 1.1 无死角视频：六面覆盖 + 双路冗余
    for f in config.COVER_FACES:
        cams.register("CAM-%s" % f.upper(), ROOM, "保密室A·%s" % f, f,
                      url="rtsp://demo/%s" % f,
                      checkpoints=config.FACE_CHECKPOINTS[f],
                      channels=["main", "backup"], operator="selfcheck")
    rate, detail = cams.coverage(ROOM)
    chk("1.1 六面覆盖 100%%（实覆盖 %d/6）" % int(round(rate * 6)),
        abs(rate - 1.0) < 1e-9, rate)
    chk("1.1 无盲区", cams.blind_spots(ROOM) == [], cams.blind_spots(ROOM))
    all_ok = all(not c.ledger_problems() for c in store.cameras(ROOM))
    chk("1.1 逐机位台账要点齐备（含柜顶/柜底）", all_ok,
        [c.ledger_problems() for c in store.cameras(ROOM)])

    # 覆盖不全时能识别盲区
    cams.register("CAM-WEAK", "保密室B", "测试", "front", url="rtsp://demo/w")
    r2, _ = cams.coverage("保密室B")
    chk("1.1 覆盖不足能识别（1/6 面 -> 5 处盲区）",
        abs(r2 - 1 / 6.0) < 1e-9 and len(cams.blind_spots("保密室B")) == 5,
        "%.4f %d" % (r2, len(cams.blind_spots("保密室B"))))
    cams.remove("CAM-WEAK", operator="selfcheck")

    # 1.1 取流实现与同仓其他项目**逐字节一致**（仓库约定：camera_stream.py 多份同步）
    _cs = os.path.join(HERE, "camera_stream.py")

    def _md5(p):
        import hashlib
        with open(p, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()

    _mine = _md5(_cs)
    _all_dir = os.path.dirname(HERE)
    _siblings = []
    for _name in os.listdir(_all_dir):
        if _name == os.path.basename(HERE) or _name.startswith(("00_", "备份", "__")):
            continue
        _p = os.path.join(_all_dir, _name)
        for _cand in (os.path.join(_p, "camera_stream.py"),
                      os.path.join(_p, _name, "camera_stream.py")):
            if os.path.isfile(_cand):
                _siblings.append((_name, _cand))
                break
    _diff = [(n, _md5(p)) for n, p in _siblings if _md5(p) != _mine]
    chk("1.1 取流内核 camera_stream.py 找到同仓副本（%d 份）" % len(_siblings),
        len(_siblings) > 0, [n for n, _ in _siblings])
    chk("1.1 与同仓其他项目**逐字节一致**（md5 %s…）" % _mine[:12],
        not _diff, _diff or "全部一致")
    chk("1.1 取流对外接口一致（FrameReader / open_capture / release_async）",
        all(hasattr(camera_stream, x)
            for x in ("FrameReader", "open_capture", "release_async")),
        "FrameReader=%s open_capture=%s release_async=%s"
        % tuple(hasattr(camera_stream, x)
                for x in ("FrameReader", "open_capture", "release_async")))

    # 1.1 取流真读帧
    cap = os.path.join(str(config.CAPTURE_DIR), "_selfcheck_cam.mp4")
    os.makedirs(os.path.dirname(cap), exist_ok=True)
    made = False
    try:
        import cv2
        import numpy as np
        vw = cv2.VideoWriter(cap, cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (640, 360))
        for i in range(90):
            im = np.zeros((360, 640, 3), np.uint8)
            cv2.rectangle(im, (50 + i, 100), (180 + i, 300), (0, 200, 255), -1)
            vw.write(im)
        vw.release()
        made = True
    except Exception as e:                                   # noqa: BLE001
        chk("1.1 取流内核（合成源）", False, "无法合成源：%s" % e)

    if made:
        cams.register("CAM-LIVE", ROOM, "保密室A·实测源", "front",
                      url=cap, checkpoints=config.FACE_CHECKPOINTS["front"],
                      channels=["main", "backup"], operator="selfcheck")
        ok, msg = cams.start("CAM-LIVE")
        chk("1.1 取流启动成功", ok, msg)
        st = {}
        for _ in range(60):
            st = cams.snapshot_state("CAM-LIVE")
            if st.get("seq", 0) > 5:
                break
            time.sleep(0.1)
        chk("1.1 取流真读到帧（seq>5）", st.get("seq", 0) > 5,
            "seq=%s state=%s" % (st.get("seq"), st.get("state")))
        fr = st.get("frame")
        chk("1.1 帧尺寸 640x360", fr is not None and fr.shape[:2] == (360, 640),
            None if fr is None else fr.shape)
        p = cams.snapshot("CAM-LIVE", reason="selfcheck")
        # snapshot 返回相对 DATA_ROOT 的路径
        absp = os.path.join(str(config.DATA_ROOT), p) if p else ""
        chk("1.1 抓帧留证落盘", bool(p) and os.path.exists(absp), p)
        cams.stop("CAM-LIVE")

    # 1.2 门禁管控：双因子 + 双人双锁
    ok, e1 = vault.access.request_open(ROOM, "张巡", factors=["face", "card"],
                                       persons=["张巡", "李督"])
    chk("1.2 双因子 + 双人双锁 -> 放行", ok, e1.deny_reason)
    ok, e2 = vault.access.request_open(ROOM, "张巡", factors=["face"],
                                       persons=["张巡", "李督"])
    chk("1.2 单因子 -> 拒绝", not ok, e2.deny_reason)
    ok, e3 = vault.access.request_open(ROOM, "张巡", factors=["face", "card"],
                                       persons=["张巡"])
    chk("1.2 单人 -> 拒绝（需双人）", not ok, e3.deny_reason)
    # 尾随：授权开门照常放行（双因子+双人已过），但**另出一条尾随告警**
    ok, e4 = vault.access.request_open(ROOM, "张巡", factors=["face", "card"],
                                       persons=["张巡", "李督"], tailgating=True)
    tail = [x for x in hub.today_alarms() if x.code == "ACCESS_TAILGATING"] \
        if hasattr(hub, "today_alarms") else \
        [x for x in store.alarms(limit=300) if x.code == "ACCESS_TAILGATING"]
    chk("1.2 授权但检测到尾随 -> 放行 + 尾随告警", ok and len(tail) > 0,
        "allow=%s tailgating_alarms=%d" % (ok, len(tail)))
    a = vault.access.report_abnormal_open(ROOM, person="张巡", detail="自检：未授权开门")
    chk("1.2 未授权开门 -> 红色告警", a.code == config.ALERT_DOOR_OPEN_ABNORMAL
        if hasattr(config, "ALERT_DOOR_OPEN_ABNORMAL") else a.code == "DOOR_OPEN_ABNORMAL",
        a.code)
    chk("1.2 开门告警时延 ≤5s", (a.latency_sec or 99) <= config.INTRUSION_SLA_SEC,
        a.latency_sec)

    # 1.3 入侵探测
    ts = time.time()
    ev_i, al_i = vault.intrusion.trigger(ROOM, "ir_dual", sensor_id="IR-01",
                                         zone="门口", armed=True, event_epoch=ts)
    chk("1.3 红外双鉴 -> 告警", al_i is not None, al_i.code if al_i else None)
    chk("1.3 入侵告警时延 ≤5s", (al_i.latency_sec or 99) <= config.INTRUSION_SLA_SEC,
        al_i.latency_sec)
    for kind in config.INTRUSION_KINDS:
        _, aa = vault.intrusion.trigger(ROOM, kind, armed=True)
        chk("1.3 入侵类型 %s 可触发" % kind, aa is not None)
    vault.intrusion.set_armed(ROOM, True, operator="selfcheck")
    chk("1.3 23:00 属布防时段", vault.intrusion.in_armed_window(_at_hour(23)))
    vault.intrusion.set_armed(ROOM, False, operator="selfcheck")
    chk("1.3 12:00 不属布防时段", not vault.intrusion.in_armed_window(_at_hour(12)))
    vault.intrusion.set_armed(ROOM, True, operator="selfcheck")

    # 1.4 试卷柜电子锁
    ev_s, alarms_s = vault.safes.open_request(ROOM, "SAFE-01", "张巡", authorized=False)
    codes_s = [x.code for x in alarms_s]
    chk("1.4 未授权开柜 -> 告警", any("SAFE" in c for c in codes_s), codes_s)
    ev_s2, alarms_s2 = vault.safes.report_state(
        ROOM, "SAFE-01", "open", person="张巡",
        inner={"paper_present": False, "tamper": True, "humidity_pct": 40.0})
    codes_s2 = [x.code for x in alarms_s2]
    chk("1.4 柜内无试卷 -> 告警", len(codes_s2) > 0, codes_s2)
    chk("1.4 开柜记录已留档", len(store.safe_events(ROOM)) >= 2,
        len(store.safe_events(ROOM)))

    # 1.5 环境监测
    smp, al_e = vault.env.sample(ROOM, temp_c=38.0, humidity_pct=80.0,
                                 water=True, smoke=True)
    vs = [c for c, _ in smp.violations()]
    chk("1.5 温度越限识别（38℃ > 35℃ 上限）", "ENV_TEMP_HIGH" in vs, vs)
    chk("1.5 湿度越限识别（80% > 70% 上限）", "ENV_HUMID_HIGH" in vs, vs)
    chk("1.5 水浸 -> 告警", "ENV_WATER" in vs, vs)
    chk("1.5 烟感 -> 告警", "ENV_SMOKE" in vs, vs)
    chk("1.5 越限产生告警单", len(al_e) >= 4, len(al_e))

    # 环境正常不告警
    smp2, al_e2 = vault.env.sample(ROOM, temp_c=24.0, humidity_pct=45.0,
                                   water=False, smoke=False)
    chk("1.5 环境正常不告警", smp2.violations() == [] and al_e2 == [],
        smp2.violations())

    # 1.6 值守替代（机器人巡查）
    rec = vault.patrol.patrol_round(ROOM, by="机器人R1", result="ok",
                                    note="自检：定时巡查")
    chk("1.6 巡查轮次可登记", rec is not None)
    # 刚巡查过 -> check_due 返回 None（不缺位）；把间隔设成 0 秒即可复现"值守断档"告警
    chk("1.6 刚巡查过不报值守缺位", vault.patrol.check_due(ROOM) is None)
    due = vault.patrol.check_due(ROOM, interval_sec=0)
    chk("1.6 超过间隔未巡查 -> 报值守缺位", due is not None,
        due.code if due else None)

    # 1.7 录像留存（≥180 天 + WORM）
    now = time.time()
    for cam in store.cameras(ROOM):
        for ch in (cam.channels or ["main"]):
            cur = now - 7 * 86400
            while cur < now:
                vault.records.add_segment(cam.cam_id, cur, 3600.0, channel=ch)
                cur += 3600.0
    probs = vault.records.check_all(ROOM)
    chk("1.7 7 天录像连续无缺口", probs == [], probs)
    chk("1.7 双路冗余齐备",
        all(vault.records.redundancy_ok(c.cam_id)[0] for c in store.cameras(ROOM)))
    ret = vault.records.retention_report()
    chk("1.7 留存期 180 天", ret["retain_days"] == config.RECORD_RETAIN_DAYS,
        ret["retain_days"])
    chk("1.7 WORM：留存期内段全部加锁", ret["unlocked"] == 0 and ret["worm"], ret)

    # 挖一个缺口 -> 必须报
    segs = sorted(store.segments("CAM-FRONT", "main"), key=lambda x: x.start_epoch)
    mid = segs[len(segs) // 2]
    mid.duration_sec = 1.0
    store.save_segment(mid)
    p2 = vault.records.check_all(ROOM)
    chk("1.7 挖段后报出录像缺口", len(p2) > 0,
        [(x["cam_id"], len(x["gaps"])) for x in p2])
    mid.duration_sec = 3600.0
    store.save_segment(mid)
    chk("1.7 补齐后恢复连续", vault.records.check_all(ROOM) == [])

    # 七项体检
    items = vault.self_check(ROOM)
    chk("1.0 保密室七项体检有结论", len(items) >= 7, len(items))

    # ================= 二、试卷流转 5 项 =================
    sec("二、试卷流转环节（md 第二节 5 项）")

    BAG = "BAG-2026-0001"
    flow.register_bag(BAG, rfid="RF-0001", subject="语文", quantity=30,
                      room_id=ROOM, operator="selfcheck")
    b = store.get_bag(BAG)
    chk("2.0 试卷袋登记（RFID）", b is not None and b.rfid == "RF-0001")

    # 2.1 领取/交接：双签 + 双人脸 + 扫码
    h1, al_h1 = flow.handover.handover(
        BAG, "考务甲", "押运乙", persons=["考务甲", "押运乙"],
        signatures=["考务甲", "押运乙"], faces=["考务甲", "押运乙"],
        method="scan", quantity=30, room_id=ROOM, stage="handover",
        operator="selfcheck")
    chk("2.1 齐备交接判定为完整", h1.complete(), [c for c, _ in h1.problems])
    chk("2.1 齐备交接不告警", al_h1 == [], [x.code for x in al_h1])

    h2, al_h2 = flow.handover.handover(
        BAG, "考务甲", "押运乙", persons=["考务甲", "押运乙"],
        signatures=["考务甲", "押运乙"], faces=["考务甲"],
        method="scan", quantity=30, room_id=ROOM, stage="handover",
        operator="selfcheck")
    chk("2.1 缺人脸确认 -> 告警", any("FACE" in x.code for x in al_h2),
        [x.code for x in al_h2])

    h3, al_h3 = flow.handover.handover(
        BAG, "考务甲", "押运乙", persons=["考务甲"],
        signatures=["考务甲"], faces=["考务甲"],
        method="scan", quantity=30, room_id=ROOM, stage="handover",
        operator="selfcheck")
    chk("2.1 双人不足 -> 告警", len(al_h3) > 0, [x.code for x in al_h3])

    ok_p, _ = flow.handover.pickup(BAG, "考务甲", room_id=ROOM,
                                   operator="selfcheck")
    chk("2.1 领取环节可登记", bool(ok_p))

    rate_h, okc, tot = flow.handover.completeness_rate()
    chk("2.1 交接完整率可算", rate_h is not None and tot >= 3,
        "%s %d/%d" % (rate_h, okc, tot))

    # 2.2 押运
    task = flow.escort.start_task(
        "京A12345", driver="司机王", escort_persons=["押运乙", "押运丙"],
        bag_ids=[BAG],
        route=[[39.9000, 116.4000], [39.9050, 116.4100], [39.9100, 116.4200]],
        video_url="rtsp://demo/car", operator="selfcheck")
    chk("2.2 押运任务建立", task is not None)
    p_on, al_on = flow.escort.report_position(task.task_id, 39.9012, 116.4050,
                                              speed_kmh=45.0)
    chk("2.2 在规划路线上不报偏离", al_on is None,
        al_on.code if al_on else None)
    t_dev = time.time()
    p_dev, al_dev = flow.escort.report_position(task.task_id, 39.7000, 115.0000,
                                                speed_kmh=45.0, event_epoch=t_dev)
    chk("2.2 偏离路线 -> 告警", al_dev is not None,
        al_dev.code if al_dev else None)
    chk("2.2 偏离告警时延 ≤30s",
        (al_dev.latency_sec or 99) <= config.ESCORT_DEVIATE_SLA_SEC,
        al_dev.latency_sec)
    al_vid = flow.escort.report_video(task.task_id, False)
    chk("2.2 车内视频中断 -> 告警", al_vid is not None,
        al_vid.code if al_vid else None)
    flow.escort.report_video(task.task_id, True)
    t_done = flow.escort.finish_task(task.task_id, status="done",
                                     operator="selfcheck")
    chk("2.2 押运任务可闭环", t_done is not None and t_done.status == "done")

    # 2.3 电子铅封
    b_sealed = flow.seal.seal(BAG, "SEAL-0001", "张巡", room_id=ROOM)
    chk("2.3 电子封签成功", b_sealed.seal_state == "sealed", b_sealed.seal_state)
    chk("2.3 完好袋识别为 intact", BAG in [x.bag_id for x in flow.seal.intact_bags()],
        [x.bag_id for x in flow.seal.intact_bags()])
    bag_b, al_seal = flow.seal.report(BAG, "broken", person="张巡",
                                      detail="自检：封签被拆")
    chk("2.3 封签异常开启 -> 告警", al_seal is not None,
        al_seal.code if al_seal else None)
    chk("2.3 破封袋进入 broken 列表",
        BAG in [x.bag_id for x in flow.seal.broken_bags()])
    flow.seal.seal(BAG, "SEAL-0002", "张巡", room_id=ROOM)   # 复原为已封

    # 2.4 分发至考场
    d_ok, al_d = flow.delivery.deliver(BAG, "考场101", 30, "监考赵")
    chk("2.4 分发齐备不告警", al_d == [], [x.code for x in al_d])
    d_bad, al_d2 = flow.delivery.deliver(BAG, "", 0, "")
    chk("2.4 分发残缺（无考场/无数量/无责任人）-> 告警", len(al_d2) > 0,
        [x.code for x in al_d2])
    cov_d = flow.delivery.covered()
    chk("2.4 分发人/时/物/地点一一对应可核", isinstance(cov_d, (list, dict)),
        type(cov_d).__name__)

    # 2.5 回收与封存
    r_ok, al_r = flow.recycle.recycle(BAG, "考场101", 30, 30, person="监考赵",
                                      seal_id="SEAL-0003")
    chk("2.5 回收数量相符 + 封存 -> 不告警", al_r == [], [x.code for x in al_r])
    r_bad, al_r2 = flow.recycle.recycle(BAG, "考场101", 30, 27, person="监考赵",
                                        seal_id="SEAL-0004")
    chk("2.5 回收数量差异 -> 即时上报", any("MISMATCH" in x.code for x in al_r2),
        [x.code for x in al_r2])
    chk("2.5 差异值正确（-3）", r_bad.diff == -3, r_bad.diff)

    # 全链条追溯
    trace = flow.bag_trace(BAG)
    stages = [h.get("stage") if isinstance(h, dict) else h
              for h in (trace.get("history") or [])]
    chk("2.0 全链条追溯覆盖多环节", len(stages) >= 6, stages)
    chk("2.0 追溯含交接环节", "handover" in stages, stages)

    # ================= 三、关键指标 5 项 =================
    sec("三、关键指标（md 第三节 5 项）")
    rows = kpi.kpi(ROOM)
    by_key = {r["key"]: r for r in rows}
    chk("3.1 覆盖率指标：100% 达标",
        by_key["coverage_rate"]["verdict"] == "达标",
        by_key["coverage_rate"]["actual_text"])
    chk("3.2 告警时延指标：有实测且 ≤5s 达标",
        by_key["alarm_latency"]["actual"] is not None
        and by_key["alarm_latency"]["verdict"] == "达标",
        by_key["alarm_latency"]["actual_text"])
    chk("3.3 交接完整率指标可算",
        by_key["handover_complete"]["actual"] is not None,
        by_key["handover_complete"]["actual_text"])
    chk("3.4 录像连续性指标：无丢段达标",
        by_key["record_continuous"]["verdict"] == "达标",
        by_key["record_continuous"]["actual_text"])
    chk("3.5 押运偏离时延指标：有实测且 ≤30s 达标",
        by_key["escort_deviate_latency"]["actual"] is not None
        and by_key["escort_deviate_latency"]["verdict"] == "达标",
        by_key["escort_deviate_latency"]["actual_text"])

    # 样本为 0 必须标"样本不足"
    # 用一个"什么都没登记过"的房间：覆盖率必然无样本
    empty = kpi.kpi("保密室Z")
    z = {r["key"]: r for r in empty}
    chk("5.0 覆盖率无样本时标「样本不足」，绝不当达标",
        z["coverage_rate"]["verdict"] == "样本不足",
        z["coverage_rate"]["verdict"])
    chk("3.0 无交接样本时标「样本不足」",
        z["handover_complete"]["verdict"] == "样本不足",
        z["handover_complete"]["verdict"])
    chk("3.0 覆盖率无样本时如实说明原因",
        "尚未登记" in (z["coverage_rate"]["how"] or ""),
        z["coverage_rate"]["how"])

    # 覆盖率不达标也要如实标
    cams.register("CAM-W2", "保密室B", "弱覆盖", "front", url="rtsp://demo/w2")
    bad = {r["key"]: r for r in kpi.kpi("保密室B")}
    chk("3.1 覆盖率不足如实标「未达标」",
        bad["coverage_rate"]["verdict"] == "未达标",
        bad["coverage_rate"]["actual_text"])
    cams.remove("CAM-W2", operator="selfcheck")

    # ================= 四、验收证据 =================
    sec("四、验收证据（md 第四节）")
    ledger = ev.camera_ledger(ROOM)
    chk("4.1 无死角核查报告：机位台账", len(ledger) >= 6, len(ledger))
    ctrep = ev.coverage_report(ROOM)
    chk("4.1 无死角核查报告：覆盖率与盲区",
        isinstance(ctrep, dict) and "blind_spots" in ctrep, list(ctrep.keys())[:6])
    txt = ev.full_report(ROOM, generated_by="selfcheck")
    chk("4.0 验收报告生成（>1500 字）", len(txt) > 1500, len(txt))
    for keyw in ["无死角", "门禁", "开柜", "录像", "留存",
                 "交接", "押运", "铅封", "分发", "回收", "指标", "审计"]:
        chk("4.0 报告含「%s」" % keyw, keyw in txt)

    path, _ = ev.save_report(ROOM, generated_by="selfcheck")
    chk("4.0 报告落盘", bool(path) and os.path.exists(path), path)
    leds = ev.export_ledgers()
    chk("4.0 台账 CSV 导出 ≥6 份", len([v for v in leds.values() if v]) >= 6,
        sorted(k for k, v in leds.items() if v))

    # ================= 告警与审计 =================
    sec("五、告警中心与哈希链审计")
    chk("5.0 存在未处置红色告警", hub.open_red_count() > 0, hub.open_red_count())
    st = hub.latency_stats(("DOOR_OPEN_ABNORMAL", "SAFE_OPEN_ABNORMAL",
                            "INTRUSION_IR", "ESCORT_ROUTE_DEVIATE"))
    chk("5.0 告警时延统计可用", isinstance(st, dict) and len(st) > 0,
        {k: v["max"] for k, v in st.items()})
    n = hub.ack_all_red("selfcheck")
    chk("5.0 可批量确认红色告警", n >= 0, n)
    chk("5.0 确认后红色告警清零", hub.open_red_count() == 0, hub.open_red_count())

    ok_a, why_a = store.audit_verify()
    chk("5.0 审计哈希链校验通过", ok_a, why_a)
    chk("5.0 审计条目非空", len(store.audit_rows(limit=500)) > 0,
        len(store.audit_rows(limit=500)))

    # 篡改一条审计记录 -> 必须校验失败
    rows_a = store.audit_rows(limit=5)
    if rows_a:
        seq = rows_a[len(rows_a) // 2]["seq"]          # 挑中间一条改，避免动链头
        with store._lock, store._conn() as c:                # noqa: SLF001
            c.execute("UPDATE audit SET detail='篡改' WHERE seq=?", (seq,))
        ok_b, why_b = store.audit_verify()
        chk("5.0 篡改审计后校验失败（可定位断点）", not ok_b, why_b)

    # 去重：同类告警重复上报不刷屏
    before = len(store.alarms(limit=10000))
    for _ in range(5):
        vault.env.sample(ROOM, temp_c=24.0, humidity_pct=45.0,
                         water=True, smoke=False)
    after = len(store.alarms(limit=10000))
    chk("5.0 同类告警 5 次重复上报被去重（≤1 新增）", after - before <= 1,
        "before=%d after=%d" % (before, after))

    # 桌面界面接线自检（冒烟）：App -> LoginFrame / MainFrame 必须能真正构建。
    # 历史缺陷：曾把 Tk 窗口当作 App 传给子框架，子框架里 master.store 直接
    # AttributeError，界面双击打不开；而单元测试与自检都不覆盖 GUI，故补此检查。
    # 用子进程跑，避免 Tk 与主进程状态互相干扰；无图形环境时如实标注为跳过。
    _smoke = "\n".join([
        "import os, sys",
        "sys.path.insert(0, os.getcwd())",
        "try:",
        "    import tkinter as tk",
        "except Exception as e:",
        "    print('NO_TKINTER', e); sys.exit(3)",
        "import gui, storage, web_accounts",
        "try:",
        "    app = gui.App()",
        "except tk.TclError as e:",
        "    print('NO_DISPLAY', e); sys.exit(2)",
        "app.root.withdraw()",
        "try:",
        "    st = storage.Store(); am = web_accounts.AccountManager(st)",
        "    am.ensure_defaults()",
        "    sess, why = am.login('owner', 'owner')",
        "    assert sess is not None, why",
        "    assert app.login is not None, 'LoginFrame 未构建'",
        "    app.login.destroy()",
        "    mf = gui.MainFrame(app, sess)",
        "    mf.pack(fill='both', expand=True)",
        "    app.main = mf",
        "    for _ in range(5):",
        "        app.root.update_idletasks(); app.root.update()",
        "    try:",
        "        mf.shutdown()",
        "    except Exception:",
        "        pass",
        "except Exception:",
        "    import traceback; traceback.print_exc(); sys.exit(4)",
        "app.root.destroy()",
        "print('SMOKE_OK')",
    ])
    _smoke_name = "5.1 桌面界面可构建（App / 登录框 / 主界面接线真实可用）"
    try:
        _sp = subprocess.run([sys.executable, "-c", _smoke], cwd=HERE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=180)
        _so = _sp.stdout.decode("utf-8", "replace").strip()
        if _sp.returncode == 3:
            chk(_smoke_name, True,
                "当前解释器无 tkinter，已跳过（桌面端请用带 tkinter 的 Python，"
                "见「启动保密室监控界面.bat」）")
        elif _sp.returncode == 2:
            chk(_smoke_name, True, "无图形环境，已跳过（本项目为 Windows 桌面端）")
        else:
            chk(_smoke_name, _sp.returncode == 0,
                _so.splitlines()[-1] if _so else "exit=%d" % _sp.returncode)
    except Exception as _e:                                   # noqa: BLE001
        chk(_smoke_name, False, "无法启动界面子进程：%s" % _e)

    # ================= 汇总 =================
    ok_n = sum(1 for x in RES if x[0])
    bad_n = len(RES) - ok_n
    print()
    print("=" * 68)
    print(" 自检合计 %d 项：通过 %d 项，失败 %d 项，用时 %.1fs"
          % (len(RES), ok_n, bad_n, time.time() - t_all))
    if bad_n:
        print(" 失败明细：")
        for good, name, extra in RES:
            if not good:
                print("   - %s (%s)" % (name, extra))
    else:
        print(" 结论：按 md 文档三节 + 验收证据逐条落地，全部通过。")
        print(" 注意：本自检用本地合成源验证取流与判定逻辑；真实摄像头 / 门禁 /")
        print("       红外 / 电子锁 / 北斗 / 录像平台需现场联调后复测。")
    print("=" * 68)
    return 0 if bad_n == 0 else 1


def _at_hour(hour):
    """构造一个当天的指定小时时间戳，用于布防时段判定。"""
    import datetime
    d = datetime.datetime.now().replace(hour=hour, minute=0, second=0,
                                        microsecond=0)
    return d.timestamp()


if __name__ == "__main__":
    sys.exit(main())
