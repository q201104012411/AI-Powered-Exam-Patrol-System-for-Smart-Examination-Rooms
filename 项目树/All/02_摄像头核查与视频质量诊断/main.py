# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 端到端自检

默认在**临时数据目录**里跑（不污染真实 data\\），加 --real 才动真实目录。
用合成画面覆盖全部 7 类核查内容与 3 级巡检，逐项打印 PASS/FAIL。

注意：本自检用的是**合成缺陷图**，用于验证算法判定正确；
真实摄像头/录像/音频链路的指标必须靠现场巡检样本积累，自检不替代现场验收。
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

import numpy as np

TMP = None
RESULTS = []


def _prepare(argv):
    global TMP
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="使用真实 data 目录")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)
    if not args.real:
        TMP = tempfile.mkdtemp(prefix="camera_selfcheck_")
        os.environ["EXAM_CAMERA_HOME"] = TMP
    return args


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— " + str(detail)) if detail else ""))
    return bool(cond)


# ---------------- 合成场景 ----------------
def base_scene(w=1280, h=720):
    rng = np.random.RandomState(7)
    img = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        img[y, :, :] = (60 + 80 * y // h, 70 + 70 * y // h, 90 + 60 * y // h)
    for _ in range(90):
        x1, y1 = rng.randint(0, w - 60), rng.randint(0, h - 60)
        x2, y2 = x1 + rng.randint(20, 160), y1 + rng.randint(15, 120)
        col = tuple(int(c) for c in rng.randint(40, 230, 3))
        if rng.rand() < 0.5:
            cv2.rectangle(img, (x1, y1), (x2, y2), col, rng.randint(1, 4))
        else:
            cv2.circle(img, (x1, y1), rng.randint(8, 50), col, rng.randint(1, 4))
    for i in range(12):
        cv2.putText(img, "A%d" % i, (40 + i * 95, 120 + (i % 3) * 200),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (235, 235, 235), 2)
    return img


def make_cases(base):
    out = {}
    out["正常"] = base.copy()
    out["模糊"] = cv2.GaussianBlur(base, (15, 15), 0)
    out["过暗"] = np.clip(base.astype(np.float32) * 0.10, 0, 255).astype(np.uint8)
    out["过曝"] = np.clip(base.astype(np.float32) * 3.2 + 70, 0, 255).astype(np.uint8)
    out["低对比"] = np.clip(base.astype(np.float32) * 0.10 + 120, 0, 255).astype(np.uint8)
    cast = base.astype(np.float32).copy()
    cast[:, :, 0] = np.clip(cast[:, :, 0] * 1.9 + 25, 0, 255)
    out["偏色"] = cast.astype(np.uint8)
    yy = np.arange(base.shape[0], dtype=np.float32)[:, None, None]
    out["条纹"] = np.clip(base.astype(np.float32) + 42 * np.sin(2 * np.pi * yy / 9.0),
                          0, 255).astype(np.uint8)
    rng = np.random.RandomState(3)
    out["雪花"] = np.clip(base.astype(np.float32) + rng.normal(0, 22, base.shape),
                          0, 255).astype(np.uint8)
    small = cv2.resize(base, (base.shape[1] // 10, base.shape[0] // 10))
    out["马赛克"] = cv2.resize(small, (base.shape[1], base.shape[0]),
                              interpolation=cv2.INTER_NEAREST)
    occ = base.copy()
    occ[120:620, 200:1080] = 128
    out["遮挡"] = occ
    m = np.float32([[1, 0, 70], [0, 1, 26]])
    out["偏移"] = cv2.warpAffine(base, m, (base.shape[1], base.shape[0]),
                                 borderMode=cv2.BORDER_REPLICATE)
    return out


EXPECT = {
    "正常": set(),
    "模糊": {"BLUR"},
    "过暗": {"UNDEREXPOSED"},
    "过曝": {"OVEREXPOSED"},
    "低对比": {"LOW_CONTRAST"},
    "偏色": {"COLOR_CAST"},
    "条纹": {"STRIPES"},
    "雪花": {"SNOW"},
    "马赛克": {"MOSAIC"},
    # 无参考（没有基准图）时：遮挡表现为大面积低对比，视角偏移**无法**判定
    "遮挡": {"LOW_CONTRAST"},
    "偏移": set(),
}
# 只有比对基准图才能判定的项
EXPECT_BASELINE = {"遮挡": {"OCCLUDED"}, "偏移": {"VIEW_OFFSET"}, "正常": set(),
                   "模糊": {"BLUR"}}


def main(argv=None):
    args = _prepare(argv if argv is not None else sys.argv[1:])
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    import diagnose
    import models
    import probe
    import storage
    import web_accounts
    import baseline as baseline_mod
    import patrol as patrol_mod
    import workorder as wo_mod
    import report as report_mod

    global cv2
    import cv2

    print("=" * 78)
    print("摄像头核查与视频质量诊断 · 端到端自检")
    print("数据目录：%s%s" % (config.DATA_ROOT, "（临时）" if TMP else "（真实）"))
    print("=" * 78)

    store = storage.Store()
    dg = diagnose.Diagnoser()
    baselines = baseline_mod.BaselineStore(store)
    base = base_scene()
    cases = make_cases(base)
    bm, bg = diagnose.baseline_metrics(base, dg)

    # ---------- 1. 图像质量（无参考） ----------
    print("\n[1/11] 图像质量诊断（无参考，7 类）")
    for name, img in cases.items():
        defects, metrics = dg.diagnose(img)          # 只做无参考，不比对基准
        codes = {d.code for d in defects}
        if name == "正常":
            check("正常画面不应误报质量缺陷", not codes,
                  "误报：" + "、".join(sorted(codes)))
        else:
            check("%s -> %s" % (name, "、".join(sorted(EXPECT[name]))),
                  EXPECT[name] <= codes, "实测：" + "、".join(sorted(codes)) or "无")

    # ---------- 2. 基准图比对（视野完整性） ----------
    print("\n[2/11] 视野完整性（基准图比对）")
    for name in ("正常", "遮挡", "偏移", "模糊"):
        defects, metrics = dg.diagnose(cases[name], baseline_gray=bg,
                                       baseline_metrics=bm)
        codes = {d.code for d in defects}
        if name == "正常":
            check("正常画面比对基准不误报遮挡/偏移",
                  not ({"OCCLUDED", "VIEW_OFFSET"} & codes),
                  "实测：" + "、".join(sorted(codes)))
        else:
            check("%s 比对基准能识别 %s" % (name, "、".join(sorted(EXPECT_BASELINE[name]))),
                  EXPECT_BASELINE[name] <= codes,
                  "实测：" + "、".join(sorted(codes)) or "无")
    d, m = dg.diagnose(cases["偏移"], baseline_gray=bg, baseline_metrics=bm)
    check("偏移量换算成角度可读",
          isinstance(m.get("angle_deg"), float) and m.get("shift_px", 0) > 18,
          "平移 %.1f px ≈ %.1f°" % (m.get("shift_px", 0), m.get("angle_deg", 0)))

    # ---------- 3. 冻结帧 ----------
    print("\n[3/11] 冻结帧（帧间差异）")
    fz = dg.frozen_defect([0.0, 0.0, 0.0])
    check("连续无变化判为冻结", fz is not None and fz.code == "FROZEN")
    check("有变化时不判冻结", dg.frozen_defect([12.0, 0.1, 0.1]) is None)
    check("帧数不足时不判冻结", dg.frozen_defect([0.0]) is None)

    # ---------- 4. 设备台账与心跳 ----------
    print("\n[4/11] 设备台账与在线状态")
    devs = []
    for i, code in enumerate(["CAM-A01-1", "CAM-A01-2", "CAM-A02-1"]):
        d = models.Device(code=code, room_id="A-01" if i < 2 else "A-02",
                          name="考场摄像头%s" % (i + 1), site="一中考点",
                          position="前墙左上" if i == 0 else "后墙右上",
                          url=str(os.path.join(str(config.DATA_ROOT), "cam_%s.jpg" % code)),
                          fov_deg=90.0, pan_deg=0.0, tilt_deg=-10.0,
                          has_audio=(i == 0), session="6月7日上午")
        store.save_device(d)
        devs.append(d)
    check("台账登记 3 台机位", len(store.list_devices()) == 3)
    check("按考场查询", len(store.list_devices(room_id="A-01")) == 2)
    bad_dev = models.Device(code="CAM-A03-1")
    check("台账不齐能被识别", bool(bad_dev.ledger_problems()),
          "；".join(bad_dev.ledger_problems()))
    d0 = devs[0]
    ok, why = probe.heartbeat_status(d0, now=models.now_epoch())
    check("无心跳记录判为离线", not ok, why)
    store.heartbeat(d0.device_id, models.now_epoch() - 5)
    d0b = store.get_device(device_id=d0.device_id)
    check("心跳正常判为在线", probe.heartbeat_status(d0b)[0])
    store.heartbeat(d0.device_id, models.now_epoch() - 500)
    check("心跳超时判为离线",
          not probe.heartbeat_status(store.get_device(device_id=d0.device_id))[0])
    store.heartbeat(d0.device_id)

    # ---------- 5. 基准图采集 ----------
    print("\n[5/11] 基准图采集")
    ok, res = baselines.capture(devs[0], frame=base, operator="admin", note="部署采集")
    check("基准图采集成功", ok and res.get("path"), str(res)[:80])
    check("基准图文件落盘", os.path.exists(res["path"]))
    rec, gray, bmetrics = baselines.get(devs[0])
    check("基准图可回读且指标齐全",
          gray is not None and bmetrics is not None and "sharpness" in bmetrics)
    for d in devs[1:]:
        baselines.capture(d, frame=base, operator="admin")
    check("三台机位均有基准图", all(baselines.coverage_map_ok(d) for d in devs))
    check("未采基准的机位会给出原因",
          "尚未采集" in baselines.missing_reason(bad_dev))

    # ---------- 6. 三级巡检 ----------
    print("\n[6/11] 三级巡检（日常 / 考前专项 / 考前一小时自检）")
    frames = {"CAM-A01-1": cases["正常"], "CAM-A01-2": cases["模糊"],
              "CAM-A02-1": cases["遮挡"]}

    def rec_provider(d):
        """CAM-A02-1 故意留一个丢段，其余机位录像连续"""
        if d.code == "CAM-A02-1":
            return [{"id": 1, "start": 1000.0, "duration": 300, "playable": True},
                    {"id": 2, "start": 1600.0, "duration": 300, "playable": True}]
        return [{"id": 1, "start": 1000.0, "duration": 300, "playable": True},
                {"id": 2, "start": 1300.0, "duration": 300, "playable": True}]

    def time_provider(d):
        """CAM-A02-1 时钟超限，其余一致"""
        if d.code == "CAM-A02-1":
            return (1000.0, 1002.5)
        return (1000.0, 1000.4)

    def audio_provider(d):
        """CAM-A01-1 音频正常，其余无声"""
        return probe.synth_audio("normal" if d.code == "CAM-A01-1" else "silent")

    runner = patrol_mod.PatrolRunner(
        store,
        frame_provider=lambda d: (True, frames.get(d.code), "合成画面"),
        recording_provider=rec_provider,
        time_provider=time_provider,
        audio_provider=audio_provider,
        baseline_store=baselines)
    s1 = runner.run(policy=config.POLICY_DAILY, operator="admin")
    check("日常巡检跑完 3 台机位", s1["total"] == 3, str(s1["bad_devices"]))
    check("模糊机位被判异常", "CAM-A01-2" in s1["bad_devices"])
    check("遮挡机位被判异常", "CAM-A02-1" in s1["bad_devices"])
    check("正常机位判正常", "CAM-A01-1" not in s1["bad_devices"],
          "缺陷：" + str([c for c in
                          (store.latest_check(devs[0].device_id).defects)]))
    s2 = runner.run(policy=config.POLICY_PREEXAM, room_id="A-01", operator="admin")
    check("考前专项可按考场跑", s2["total"] == 2, str(s2["total"]))
    s3 = runner.run(policy=config.POLICY_BEFORE, session="6月7日上午", operator="admin")
    check("考前一小时自检可按场次跑", s3["total"] == 3, str(s3["total"]))
    due = patrol_mod.PatrolRunner.daily_due(models.now_epoch() - 31 * 60)
    check("日常巡检到点判定（30 分钟）", due)
    check("不到点不触发",
          not patrol_mod.PatrolRunner.daily_due(models.now_epoch() - 5 * 60))
    now = models.now_epoch()
    in_win, left = patrol_mod.PatrolRunner.preexam_window(now + 3 * 86400, now)
    check("考前 7 天窗口判定", in_win, "剩余 %.1f 天" % (left / 86400))
    in_win2, _ = patrol_mod.PatrolRunner.before_session_window(now + 10 * 60, now)
    check("考前一小时自检窗口（开考前 30 分钟内）", in_win2)

    # ---------- 7. 录像 / 时间 / 音频 ----------
    print("\n[7/11] 录像完整性 / 时间一致性 / 音频链路")
    rc_ok = probe.recording_completeness(
        [{"id": 1, "start": 0, "duration": 300, "playable": True},
         {"id": 2, "start": 300, "duration": 300, "playable": True}])
    check("录像连续判正常", rc_ok["ok"], rc_ok["reason"])
    rc_gap = probe.recording_completeness(
        [{"id": 1, "start": 0, "duration": 300, "playable": True},
         {"id": 2, "start": 900, "duration": 300, "playable": True}])
    check("丢段被判出并给出缺口", (not rc_gap["ok"]) and rc_gap["gaps"],
          str(rc_gap["gaps"])[:80])
    rc_none = probe.recording_completeness([])
    check("无录像段如实报'无录像可回放'", rc_none["missing"], rc_none["reason"])
    okd, drift, why = probe.time_drift(1000.0, 1000.5)
    check("时钟偏差 ≤1 秒判一致", okd and abs(abs(drift) - 0.5) < 1e-6, why)
    okd2, drift2, why2 = probe.time_drift(1000.0, 1003.0)
    check("时钟偏差 >1 秒判超限", not okd2, why2)
    a_ok = probe.audio_level(*probe.synth_audio("normal"))
    check("音频链路正常判通过", a_ok["ok"], a_ok["reason"])
    a_sil = probe.audio_level(*probe.synth_audio("silent"))
    check("拾音器无声被判出", not a_sil["ok"], a_sil["reason"])
    a_noi = probe.audio_level(*probe.synth_audio("noise"))
    check("持续噪声被判出", not a_noi["ok"], a_noi["reason"])

    # ---------- 8. 闭环整改工单 ----------
    print("\n[8/11] 闭环整改工单（发现-派单-整改-复核）")
    wm = wo_mod.WorkOrderManager(store)
    bad_ck = None
    for d in devs:
        ck = store.latest_check(d.device_id)
        if ck and not ck.ok:
            bad_ck = (ck, d)
            break
    check("找到异常机位用于建单", bad_ck is not None)
    ck, dev = bad_ck
    wo, msg = wm.ensure_from_check(ck, dev, operator="admin")
    check("异常自动建单", wo is not None, msg)
    wo2, msg2 = wm.ensure_from_check(ck, dev, operator="admin")
    check("同机位同缺陷不重复建单", wo2 is not None and wo2.order_id == wo.order_id, msg2)
    check("未派单时不能整改", not wm.rectify(wo.order_id, None, "x", "admin")[0])
    check("派单必须指定责任人", not wm.dispatch(wo.order_id, "", "admin")[0])
    check("派单成功", wm.dispatch(wo.order_id, "运维-李工", "admin")[0])
    after = os.path.join(str(config.DATA_ROOT), "after.jpg")
    diagnose.imwrite_unicode(after, base)
    check("整改必须附整改后截图",
          not wm.rectify(wo.order_id, None, "无图", "admin")[0])
    check("提交整改（含整改后截图）", wm.rectify(wo.order_id, after, "已调整镜头", "admin")[0])
    check("复核不通过会打回",
          not wm.review(wo.order_id, False, "仍模糊", "admin")[0])
    check("打回后状态回到已派单",
          store.get_order(wo.order_id).status == "dispatched")
    wm.rectify(wo.order_id, after, "再次清洁镜头", "admin")
    check("二次整改后可复核通过", wm.review(wo.order_id, True, "已清晰", "admin")[0])
    check("闭环后状态为 closed", store.get_order(wo.order_id).status == "closed")
    st = wm.stats()
    check("闭环率可统计", st["total"] >= 1 and st["close_rate"] is not None, str(st)[:90])

    # ---------- 9. 与巡考联动降权 ----------
    print("\n[9/11] 与巡考联动：质量异常点位告警降权")
    f1, why1 = runner.credibility_factor("CAM-A01-2")
    check("异常机位被降权", f1 < 1.0, why1[:80])
    f2, why2 = runner.credibility_factor("CAM-A01-1")
    check("正常机位不降权", f2 == 1.0, why2)
    adj, f, why3 = runner.adjust_alert_score("CAM-A01-2", 0.9)
    check("告警分数按系数下调", abs(adj - 0.9 * f) < 1e-9,
          "0.9 -> %.3f（×%.1f）" % (adj, f))
    check("未知机位不降权", runner.credibility_factor("NOPE")[0] == 1.0)

    # ---------- 10. 报告与关键指标 ----------
    print("\n[10/10] 报告与关键指标")
    rep = report_mod.Reporter(store, runner=runner, orders=wm)
    kpi = runner.kpi()
    check("五项关键指标齐全", set(kpi.keys()) == set(report_mod.KPI_TEXT), str(list(kpi)))
    check("在线率有样本", kpi["在线率"]["samples"] > 0, str(kpi["在线率"]))
    check("时钟一致性有样本", kpi["时钟一致性"]["samples"] > 0, str(kpi["时钟一致性"]))
    check("覆盖完整性按基准图统计",
          kpi["覆盖完整性"]["baseline_devices"] == 3, str(kpi["覆盖完整性"]))
    check("考前核查完成率已统计",
          kpi["考前核查完成率"]["value"] is not None, str(kpi["考前核查完成率"]))
    text = rep.full_report()
    check("汇总报告含关键指标章节", "关键指标" in text)
    check("汇总报告含缺陷汇总", "缺陷汇总" in text)
    check("汇总报告含整改闭环", "整改闭环" in text)
    check("汇总报告含验收证据清单", "验收证据清单" in text)
    check("逐考场报告生成", "A-01" in rep.room_report("A-01"))
    path = rep.export(text)
    check("报告导出成功", bool(path) and os.path.exists(path), path)
    ok, msg, n = store.verify_chain()
    check("哈希链审计完整", ok, msg)

    # ---------- 11. 账户与权限（桌面端 / 网页端共用一份） ----------
    print("\n[11/11] 账户与权限（所有者 / 管理员）+ 隐藏命令行窗口")
    acc = web_accounts.Accounts(store)
    check("首次自动预置 owner / teacher1 两个账户",
          acc.usernames() == ["owner", "teacher1"], "、".join(acc.usernames()))
    stored = acc.get("owner")["pwd"]
    check("口令按 pbkdf2$salt$dk 存（不落明文）",
          stored.startswith("pbkdf2$") and "EDY_POWERQ" not in stored)
    check("所有者口令校验通过",
          web_accounts.verify_password("EDY_POWERQ", stored))
    check("错误口令校验失败",
          not web_accounts.verify_password("edY_powerq", stored))
    s_owner, msg_o = acc.authenticate("owner", "EDY_POWERQ")
    check("桌面端登录 owner 成功", s_owner is not None, msg_o)
    s_admin, msg_a = acc.authenticate("teacher1", "teacher1")
    check("桌面端登录 teacher1 成功", s_admin is not None, msg_a)
    check("登录失败会被拒",
          acc.authenticate("owner", "wrong")[0] is None)
    check("登录成功/失败都进审计链",
          {"登录成功", "登录失败"} <= {r["action"] for r in store.audit_rows(limit=50)})
    check("网页端与桌面端读到的是同一份账户（同一 kv 键 web.accounts）",
          web_accounts.Accounts(storage.Store()).usernames() == acc.usernames())

    owner_only = ("device_delete", "account_manage", "audit_view", "hide_cmd_toggle")
    daily = ("view", "device_add", "baseline_capture", "inspect_upload", "patrol_run",
             "workorder_operate", "report_export", "change_own_pwd")
    check("所有者拥有全部权限",
          all(web_accounts.can(a, web_accounts.ROLE_OWNER) for a in web_accounts.CAN))
    check("管理员日常操作（台账/基准图/抽查/巡检/工单/报告）全部可用",
          all(web_accounts.can(a, web_accounts.ROLE_ADMIN) for a in daily))
    check("管理员被拒：删机位 / 管账户 / 看审计 / 隐藏cmd开关",
          not any(web_accounts.can(a, web_accounts.ROLE_ADMIN) for a in owner_only))
    check("未知角色与未知动作一律拒绝",
          not web_accounts.can("device_delete", "teacher")
          and not web_accounts.can("nope", web_accounts.ROLE_OWNER))
    check("越权提示写明「仅所有者可用」",
          "仅所有者" in s_admin.deny_text("device_delete"),
          s_admin.deny_text("device_delete"))
    check("管理员不能新建账户",
          not acc.create("hacker", "pwd12345", web_accounts.ROLE_ADMIN, by="teacher1")[0])
    check("管理员不能重置所有者口令（堵死夺权路径）",
          not acc.reset_password("owner", "hacked123", by="teacher1")[0]
          and acc.authenticate("owner", "EDY_POWERQ")[0] is not None)
    check("管理员不能删除所有者",
          not acc.delete("owner", by="teacher1")[0])
    ok_n, msg_n = acc.create("teacher2", "pwd12345", web_accounts.ROLE_ADMIN,
                             "巡考管理员2", by="owner")
    check("所有者可新建管理员账户", ok_n, msg_n)
    check("新建账户可登录",
          acc.authenticate("teacher2", "pwd12345")[0] is not None)
    check("不能新建与自己同级的所有者账户",
          not acc.create("owner2", "pwd12345", web_accounts.ROLE_OWNER, by="owner")[0])
    check("不能创建重名账户",
          not acc.create("teacher2", "pwd12345", web_accounts.ROLE_ADMIN, by="owner")[0])
    check("口令太短被拒", not acc.create("t9", "123", web_accounts.ROLE_ADMIN,
                                          by="owner")[0])
    check("重置口令后新口令生效、旧口令失效",
          acc.reset_password("teacher2", "newpwd123", by="owner")[0]
          and acc.authenticate("teacher2", "newpwd123")[0] is not None
          and acc.authenticate("teacher2", "pwd12345")[0] is None)
    check("不能删除当前登录账户（自己）",
          not acc.delete("owner", by="owner")[0] and acc.exists("owner"))
    check("删除管理员账户成功",
          acc.delete("teacher2", by="owner")[0] and not acc.exists("teacher2"))
    check("列表接口不回显口令哈希",
          all("pwd" not in r for r in acc.list_accounts()))

    check("隐藏命令行窗口标记默认关闭", not web_accounts.hidden_cmd_on())
    ok_h, msg_h = web_accounts.set_hidden_cmd(True, store, "owner")
    check("开启隐藏 cmd：标记文件已写入",
          ok_h and os.path.exists(str(config.HIDE_CMD_FLAG)), msg_h)
    ok_h2, _ = web_accounts.set_hidden_cmd(False, store, "owner")
    check("关闭隐藏 cmd：标记文件已删除",
          ok_h2 and not os.path.exists(str(config.HIDE_CMD_FLAG)))
    check("隐藏 cmd 开关进审计链",
          "隐藏命令行窗口" in {r["action"] for r in store.audit_rows(limit=50)})
    check("审计链在账户操作后依然完整", store.verify_chain()[0])

    # ---------- 汇总 ----------
    total = len(RESULTS)
    passed = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 78)
    if passed == total:
        print("自检通过：%d/%d 项全部符合预期" % (passed, total))
    else:
        print("自检未通过：%d/%d 项" % (passed, total))
        for name, o, detail in RESULTS:
            if not o:
                print("   FAIL %s　%s" % (name, detail))
    print("提示：本自检用合成画面验证算法判定；真实在线率/时延/覆盖率")
    print("      必须靠现场巡检样本积累，统计为 0 不代表达标。")
    print("=" * 78)

    if TMP and not args.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    elif TMP:
        print("临时目录保留在：%s" % TMP)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
