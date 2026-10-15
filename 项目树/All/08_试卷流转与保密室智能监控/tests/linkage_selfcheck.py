# -*- coding: utf-8 -*-
r"""
08 试卷流转与保密室智能监控 · 与 00_集成联动 的联动自检

验证本专题确实能被 00 的联动总线（子进程 + JSON）调起来，并与 01/02/03 串成一条链：

  ① 01/02/03/08 四套系统连通性（各自 integration_api.py --op status）
  ② 02 登记机位 + 采集基准图
  ③ 02 现场诊断：正常画面 -> 可信度 ×1.0；模糊画面 -> BLUR + 可信度 ×0.5
  ④ **02 -> 08**：把机位画质告警送进本专题告警总线，低可信度红色**自动降级为橙色**
  ⑤ 08 自身：机位台账 / 六面覆盖率 / 交接完整率 / 五项 KPI / 试卷袋全链条追溯
  ⑥ 03 <-> 08 降权语义一致性对照

默认用**临时沙箱目录**（不污染各套正式数据）。

用法：
    python linkage_selfcheck.py
    python linkage_selfcheck.py --keep      # 保留临时目录
    python linkage_selfcheck.py --real      # 用各套真实数据目录
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # <08>/tests
PROJ = os.path.dirname(HERE)                               # <08>
ALL_DIR = os.path.dirname(PROJ)                            # <All>
sys.path.insert(0, os.path.join(ALL_DIR, "00_集成联动"))
sys.path.insert(0, PROJ)

import linkage                                              # noqa: E402

RES = []


def chk(name, cond, extra=""):
    RES.append((bool(cond), name, extra))
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         (" | " + str(extra)) if extra else ""))
    return bool(cond)


def sec(title):
    print()
    print("-" * 70)
    print(" " + title)
    print("-" * 70)


def make_images(tmp):
    """造两张图：正常 / 模糊。

    与 02 的测试场景同构——**必须有足够高频细节**（随机矩形 + 文字），
    GaussianBlur 后才会被画质诊断判为 BLUR；纯色/规则网格模糊后仍可能"够清晰"。
    不依赖任何项目内部的测试模块，避免被别的项目的测试包结构影响。
    """
    import cv2
    import numpy as np
    rng = np.random.RandomState(7)
    h, w = 720, 1280
    img = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        img[y, :, :] = (60 + 80 * y // h, 70 + 70 * y // h, 90 + 60 * y // h)
    for _ in range(200):
        x1, y1 = rng.randint(0, w - 60), rng.randint(0, h - 60)
        cv2.rectangle(img, (x1, y1),
                      (x1 + rng.randint(10, 110), y1 + rng.randint(10, 90)),
                      tuple(int(c) for c in rng.randint(30, 235, 3)),
                      rng.randint(1, 3))
    for i in range(8):
        cv2.putText(img, "A%d" % i, (40 + i * 150, 120 + (i % 2) * 300),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (240, 240, 240), 2)
    base = np.clip((img.astype(np.float32) - 95.0) * 1.6 + 118.0,
                   0, 255).astype(np.uint8)
    blur = cv2.GaussianBlur(base, (21, 21), 0)
    p_n = os.path.join(tmp, "normal.png")
    p_b = os.path.join(tmp, "blur.png")
    cv2.imencode(".png", base)[1].tofile(p_n)
    cv2.imencode(".png", blur)[1].tofile(p_b)
    return p_n, p_b


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用各套真实数据目录")
    ap.add_argument("--keep", action="store_true", help="保留临时沙箱目录")
    ap.add_argument("--device", default="CAM-LINK-08", help="联动演示机位编码")
    ns = ap.parse_args(argv or sys.argv[1:])

    tmp = None if ns.real else tempfile.mkdtemp(prefix="linkage08_")
    rooms = "保密室A"
    work = None
    try:
        print("=" * 70)
        print(" 08 试卷流转与保密室智能监控 · 联动自检（00 集成联动总线）")
        print(" 沙箱目录：%s" % (tmp if tmp else "各套真实目录"))
        print("=" * 70)

        lk = linkage.Linkage(sandbox_root=tmp, sandbox_01=False)
        work = tempfile.mkdtemp(prefix="linkage08_imgs_")
        p_normal, p_blur = make_images(work)

        # ---------- 1. 四套连通性 ----------
        sec("[1/6] 四套系统连通性（子进程 + JSON）")
        st = lk.status()
        for key, name in (("01", "人脸识别与身份核对"),
                          ("02", "摄像头核查与视频质量诊断"),
                          ("03", "智能终端操作"),
                          ("08", "试卷流转与保密室智能监控")):
            ok = bool(st.get(key, {}).get("ok"))
            chk("%s %s 可调用" % (key, name), ok,
                st.get(key, {}).get("reason") or st.get(key, {}).get("system", ""))

        # ---------- 2. 02 登记机位 + 基准图 ----------
        sec("[2/6] 02 登记机位并采集基准图")
        d = lk.p02.call("save_device", {"code": ns.device, "room_id": "A-01",
                                        "position": "前墙左上", "fov_deg": 90,
                                        "operator": "linkage08"})
        chk("机位已登记", d.get("ok"), d.get("code") or d.get("reason"))
        bl = lk.p02.call("baseline", {"device_code": ns.device, "image": p_normal,
                                      "operator": "linkage08"})
        chk("基准图已采集", bl.get("ok"), "清晰度 %s" % bl.get("sharpness"))

        # ---------- 3. 02 现场诊断 ----------
        sec("[3/6] 02 现场画面诊断")
        r_ok = lk.inspect_site(p_normal, device_code=ns.device)
        chk("正常画面判为正常", r_ok.get("result_ok") is True,
            "缺陷：%s" % ([x.get("name") for x in r_ok.get("defects", [])] or "无"))
        chk("正常机位可信度不降权", float(r_ok.get("factor", 0) or 0) >= 1.0,
            "factor=%s" % r_ok.get("factor"))
        r_bad = lk.inspect_site(p_blur, device_code=ns.device)
        codes = [x.get("code") for x in r_bad.get("defects", [])]
        chk("模糊画面判为异常且识别为 BLUR",
            r_bad.get("result_ok") is False and "BLUR" in codes, "缺陷：%s" % codes)
        chk("该机位可信度被降权", float(r_bad.get("factor", 1) or 1) < 1.0,
            "factor=%s" % r_bad.get("factor"))
        factor_bad = float(r_bad.get("factor", 1) or 1)

        # ---------- 4. ★ 02 -> 08 ----------
        sec("[4/6] 02 -> 08：画质告警进本专题告警总线（带可信度降权）")
        ra = lk.secure_alert("CAM_BLUR", room_id=rooms, device_code=ns.device,
                             detail="联动：该机位画面模糊", level="red",
                             source_kind="02", operator="linkage08")
        chk("08 已接收 02 的告警", ra.get("ok"), ra.get("alarm_id") or ra.get("reason"))
        chk("携带了 02 的可信度系数",
            abs(float(ra.get("credibility", 1) or 1) - factor_bad) < 1e-6,
            "credibility=%s" % ra.get("credibility"))
        if factor_bad < 0.6:
            chk("低可信度机位的红色告警被降级为橙色",
                ra.get("level") == "orange" and ra.get("downgraded") is True,
                "level=%s downgraded=%s" % (ra.get("level"), ra.get("downgraded")))
        else:
            chk("可信度未低于阈值，告警保持原级",
                ra.get("level") == "red" and ra.get("downgraded") is False,
                "level=%s" % ra.get("level"))
        chk("02 缺陷清单已透传到 08",
            "BLUR" in (ra.get("camera_defects") or []),
            ra.get("camera_defects"))

        # 二次上报：该机位最近一次核查结果仍是"模糊"，可信度仍低 ->
        # 08 的告警去重按 "code+source_kind+scope+pending+60s" 折叠，换房间可避开。
        ra_hi = lk.secure_alert("CAM_BLUR", room_id=rooms + "-2",
                                device_code=ns.device, detail="联动：二次上报对照",
                                level="red", source_kind="02", operator="linkage08")
        chk("同一低可信机位再报：降权行为稳定可复现",
            ra_hi.get("level") == "orange" and ra_hi.get("downgraded") is True,
            "level=%s credibility=%s" % (ra_hi.get("level"), ra_hi.get("credibility")))
        chk("换房间后未被去重折叠（各自独立告警）",
            ra_hi.get("alarm_id") != ra.get("alarm_id"),
            "%s vs %s" % (ra.get("alarm_id"), ra_hi.get("alarm_id")))

        # ---------- 5. 08 自身能力 ----------
        sec("[5/6] 08 本专题能力（机位台账 / 覆盖率 / 交接 / KPI / 追溯）")
        st08 = lk.p08.call("status", {})
        chk("08 status 返回数据目录", bool(st08.get("data_dir")), st08.get("data_dir"))
        chk("08 status 审计链可校验", st08.get("audit_ok") is True,
            st08.get("audit_chain"))
        chk("08 已记录联动告警",
            int(st08.get("alarms") or 0) >= 1, "alarms=%s" % st08.get("alarms"))

        rooms_r = lk.vault_rooms()
        chk("08 保密室清单可读（当前 %d 个）" % rooms_r.get("count", 0),
            rooms_r.get("ok") is True, [x["room_id"] for x in rooms_r.get("rooms", [])])

        vs = lk.vault_status(rooms)
        chk("08 七项能力体检有结论", len(vs.get("checks") or []) >= 7,
            "%d 项，全部达标=%s" % (len(vs.get("checks") or []), vs.get("all_ok")))
        chk("无样本保密室覆盖率如实标为 0（不假装达标）",
            float(vs.get("coverage") or 0) == 0.0,
            "coverage=%s" % vs.get("coverage"))
        chk("未登记机位的朝向全部列为盲区（六面）",
            len(vs.get("blind_spots") or []) == 6, vs.get("blind_spots"))

        hr = lk.handover_rate(rooms)
        chk("08 交接完整率可读（无记录时为 null 而非编造 100%%）",
            hr.get("ok") is True and hr.get("rate") is None, hr)

        kpi = lk.secure_kpi(rooms)
        k = (kpi.get("kpi") or {})
        chk("08 五项 KPI 已输出", len(k) >= 5, list(k))
        chk("覆盖率在无样本时标为 null（不假装 100%%）",
            k.get("coverage_rate", {}).get("value") is None,
            k.get("coverage_rate", {}).get("detail"))

        bt = lk.bag_trace("NO-SUCH-BAG")
        chk("08 追溯不存在的袋 -> 明确报错（不返回空壳）",
            bt.get("ok") is False and "不存在" in (bt.get("reason") or ""),
            bt.get("reason"))

        cams08 = lk.secure_cameras(rooms)
        chk("08 机位台账可读", cams08.get("ok") is True,
            "count=%s" % cams08.get("count"))

        # ---------- 6. 03 <-> 08 语义一致性 ----------
        sec("[6/6] 03 与 08 的降权语义一致性对照")
        room3 = "R-CONSIST"
        r3 = lk.p03.call("ingest_alert", {"code": "DOOR_OPEN_ABNORMAL",
                                          "device_id": "CAM-CONSIST",
                                          "room_id": room3,
                                          "credibility": 0.4, "level": "red"})
        r8 = lk.p08.call("ingest_alert", {"code": "DOOR_OPEN_ABNORMAL",
                                          "device_id": "CAM-CONSIST",
                                          "room_id": room3,
                                          "credibility": 0.4, "level": "red"})
        chk("低可信度（0.4）：03 与 08 都降级为橙色",
            r3.get("level") == "orange" and r8.get("level") == "orange"
            and r3.get("downgraded") is True and r8.get("downgraded") is True,
            "03=%s 08=%s" % (r3.get("level"), r8.get("level")))
        room8 = "R-CONSIST-8"
        r8b = lk.p08.call("ingest_alert", {"code": "DOOR_OPEN_ABNORMAL",
                                           "device_id": "CAM-CONSIST",
                                           "room_id": room8,
                                           "credibility": 1.0, "level": "red"})
        chk("高可信度（1.0）：08 保持红色",
            r8b.get("level") == "red" and r8b.get("downgraded") is False,
            "level=%s" % r8b.get("level"))

        # ---------- 汇总 ----------
        ok_n = sum(1 for x in RES if x[0])
        bad_n = len(RES) - ok_n
        print()
        print("=" * 70)
        print(" 联动自检合计 %d 项：通过 %d 项，失败 %d 项" % (len(RES), ok_n, bad_n))
        if bad_n:
            print(" 失败明细：")
            for good, name, extra in RES:
                if not good:
                    print("   - %s (%s)" % (name, extra))
        else:
            print(" 结论：08 已接入 00 集成联动总线，与 01/02/03 联调通过。")
        print("=" * 70)
        return 0 if bad_n == 0 else 1
    finally:
        if tmp and not ns.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        if work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
