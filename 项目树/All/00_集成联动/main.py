# -*- coding: utf-8 -*-
"""
00 集成联动 · 端到端自检：把 01 / 02 / 03 / 06 几套真的串起来跑一遍

链路：
  ① 02 登记机位并采基准图
  ② 02 抽查现场画面（正常画面 vs 模糊画面）-> 得出该机位的告警可信度系数
  ③ 03 接入告警：红色预警在低可信度机位上自动降级为橙色（防画质误报堆积）
  ④ 01 做人脸核验；同一机位画质不可信时，结论标注"需人工复核"
  ⑤ 03 复核结论回流留痕 + 审计链校验
  ⑥ 06 安检门判出手机报警 -> 经 00 送进 03 处置 -> 06 复检结论留痕

默认把 02 / 03 / 06 的数据目录重定向到临时目录（不污染真实运行数据）；
01 用真实数据（否则没有人脸库可核验，演示不出效果）。
"""
import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import linkage                                        # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— " + str(detail)[:150]) if detail else ""))
    return bool(cond)


def cheat_smoke():
    """⑦ 作弊识别（时序引擎）自检：瞬时动作被抑制、持续模式才告警。

    用引擎自带场景（合成，非实拍）验证"判定逻辑"，不冒充真实考场准确率。
    """
    import random
    out = {"ok": False, "backend": ""}
    try:
        import cheat_bridge                       # 本目录下的结合层
        from cheatdetect import simulate as sim   # cheat_bridge 已把 作弊识别\ 加进 sys.path
    except Exception as e:
        out["error"] = str(e)
        return out
    w = cheat_bridge.CheatWatch(feed_hz=10.0)
    out["ok"] = bool(w.ok)
    out["backend"] = w.backend or w.import_error
    if not w.ok:
        return out

    def run(frames):
        w.reset()
        at, peak = None, 0.0
        for i, fr in enumerate(frames):
            fr.frame_id, fr.timestamp = i, i * 0.1
            r = w.det.push(fr)
            peak = max(peak, float(r.risk))
            if r.just_triggered and at is None:
                at = i
        return at, peak

    rng = random.Random(2026)
    at, peak = run(sim.sc_quiet_writing(rng))
    out["quiet_alarm"], out["quiet_peak"] = at is not None, peak
    at, peak = run(sim.sc_stretch(rng))
    out["stretch_alarm"], out["stretch_peak"] = at is not None, peak
    at, peak = run(sim.sc_steady_peek(rng))
    out["peek_alarm"], out["peek_at"], out["peek_peak"] = at is not None, at, peak
    at, peak = run(sim.sc_phone_use(rng))
    out["phone_alarm"], out["phone_at"] = at is not None, at
    return out


def make_images(tmp):
    """造两张测试画面：一张正常、一张模糊（模糊会让该机位可信度下降）"""
    sys.path.insert(0, os.path.join(linkage.PROJECTS["02"], "tests"))
    sys.path.insert(0, linkage.PROJECTS["02"])        # diagnose 在 02 根目录下
    import cv2
    import diagnose
    from test_core import _scene
    base = _scene(1280, 720)
    # 拉一下对比度，确保"正常画面"各项指标都稳稳落在阈值内（合成场景本身偏灰，
    # 下采样到 640 工作宽度后对比度会压到 22 下限附近，容易误报"对比度过低"）
    import numpy as np
    base = np.clip((base.astype(np.float32) - 95.0) * 1.6 + 118.0, 0, 255).astype(np.uint8)
    blur = cv2.GaussianBlur(base, (15, 15), 0)
    # 用 PNG（无损）：JPEG 重编码会把合成画面的对比度压到阈值边缘，
    # 导致"正常画面"被误报为对比度过低，干扰联动结论
    p_base = os.path.join(tmp, "normal.png")
    p_blur = os.path.join(tmp, "blur.png")
    diagnose.imwrite_unicode(p_base, base)
    diagnose.imwrite_unicode(p_blur, blur)
    return p_base, p_blur


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="02/03 也用真实数据目录")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--device", default="CAM-LINK-1", help="联动演示用的机位编码")
    ap.add_argument("--candidate", default="C001", help="人脸核验用的考生编号")
    ns = ap.parse_args(argv or sys.argv[1:])

    tmp = None if ns.real else tempfile.mkdtemp(prefix="linkage_selfcheck_")
    try:
        print("=" * 78)
        print("00 集成联动 · 端到端自检（01 人脸核验 + 02 画质核查 + 03 终端处置）")
        print("02/03 数据目录：%s" % (tmp if tmp else "真实目录"))
        print("=" * 78)
        lk = linkage.Linkage(sandbox_root=tmp, sandbox_keys=("02", "03", "06"))
        work = tempfile.mkdtemp(prefix="linkage_imgs_")
        p_normal, p_blur = make_images(work)

        # ---------- 0. 各套连通性 ----------
        print("\n[1/7] 各套系统连通性")
        st = lk.status()
        for key, name in (("01", "人脸识别与身份核对"), ("02", "摄像头核查与视频质量诊断"),
                          ("03", "智能终端操作"), ("06", "智能安检与手机作弊器材检测")):
            ok = bool(st.get(key, {}).get("ok"))
            check("%s %s 可调用" % (key, name), ok,
                  st.get(key, {}).get("reason") or st.get(key, {}).get("system", ""))
        check("01 已建档考生可供核验",
              bool(st.get("01", {}).get("candidates")),
              "考生：%s" % "、".join(st.get("01", {}).get("candidates") or []) or "无")
        check("06 样本库就绪（检出率的分母）",
              bool(st.get("06", {}).get("metrics")) or
              bool(st.get("06", {}).get("thresholds")),
              "阈值 %s" % (st.get("06", {}).get("thresholds") or "无"))

        # ---------- 1. 02 登记机位 + 基准图 ----------
        print("\n[2/7] 02 登记机位并采集基准图")
        d = lk.p02.call("save_device", {"code": ns.device, "room_id": "A-01",
                                        "position": "前墙左上", "fov_deg": 90,
                                        "operator": "linkage"})
        check("机位已登记（同时上报心跳）", d.get("ok"), d.get("code"))
        bl = lk.p02.call("baseline", {"device_code": ns.device, "image": p_normal,
                                      "operator": "linkage"})
        check("基准图已采集", bl.get("ok"), "清晰度 %s" % bl.get("sharpness"))

        # ---------- 2. 02 现场抽查：正常 vs 模糊 ----------
        print("\n[3/6] 02 现场画面诊断（无参考 + 基准比对）")
        r_ok = lk.inspect_site(p_normal, device_code=ns.device)
        check("正常画面判为正常", r_ok.get("result_ok") is True,
              "缺陷：%s" % ([x["name"] for x in r_ok.get("defects", [])] or "无"))
        check("正常机位可信度不降权", float(r_ok.get("factor", 0)) >= 1.0,
              "factor=%s" % r_ok.get("factor"))
        r_bad = lk.inspect_site(p_blur, device_code=ns.device)
        codes = [x["code"] for x in r_bad.get("defects", [])]
        check("模糊画面判为异常且识别为 BLUR", r_bad.get("result_ok") is False
              and "BLUR" in codes, "缺陷：%s" % codes)
        check("该机位可信度被降权", float(r_bad.get("factor", 1)) < 1.0,
              "factor=%s" % r_bad.get("factor"))

        # ---------- 3. 02 -> 03 告警联动降级 ----------
        print("\n[4/6] 02 -> 03 告警联动（画质不可信则预警降级）")
        f, why, _ = lk.camera_credibility(ns.device)
        check("02 给出该机位可信度系数", f < 1.0, "×%.1f　%s" % (f, why))
        a_bad = lk.raise_alert("PHONE_DETECTED", ns.device, room_id="A-01", seat_no="12",
                              detail="疑似手机屏幕反光")
        check("告警已进 03 终端", bool(a_bad.get("alert_id")), a_bad.get("alert_id"))
        check("低可信度机位的红色预警被降级为橙色",
              a_bad.get("downgraded") is True and a_bad.get("level") == "orange",
              "level=%s 降级=%s" % (a_bad.get("level"), a_bad.get("downgraded")))
        check("告警里带上了画质可信度", a_bad.get("credibility") == f,
              "credibility=%s" % a_bad.get("credibility"))

        # 换一个画质正常的机位：红色预警应保持红色
        d2 = lk.p02.call("save_device", {"code": ns.device + "-OK", "room_id": "A-01",
                                         "position": "后墙右上", "operator": "linkage"})
        lk.p02.call("baseline", {"device_code": d2.get("code"), "image": p_normal})
        lk.inspect_site(p_normal, device_code=d2.get("code"))
        a_ok = lk.raise_alert("PHONE_DETECTED", d2.get("code"), room_id="A-01",
                              seat_no="15", detail="疑似手机屏幕反光")
        check("画质正常机位的红色预警保持红色且需强制确认",
              a_ok.get("level") == "red" and a_ok.get("downgraded") is False
              and a_ok.get("force_confirm") is True,
              "level=%s force_confirm=%s" % (a_ok.get("level"), a_ok.get("force_confirm")))

        # ---------- 3.5 作弊识别（时序引擎）-> 03 告警 ----------
        print("\n[4/7] 作弊识别：动作序列判定（瞬时动作被抑制 / 持续模式才告警）")
        cm = cheat_smoke()
        check("作弊识别引擎可用", cm.get("ok") is True,
              cm.get("backend") or cm.get("error", ""))
        if cm.get("ok"):
            check("安静答题不告警", cm.get("quiet_alarm") is False,
                  "峰值风险 %.1f" % cm.get("quiet_peak", 0.0))
            check("伸懒腰（瞬时动作）被时间维度抑制", cm.get("stretch_alarm") is False,
                  "峰值风险 %.1f" % cm.get("stretch_peak", 0.0))
            check("持续偷看邻座触发告警", cm.get("peek_alarm") is True,
                  "第 %s 帧触发，峰值 %.1f" % (cm.get("peek_at"), cm.get("peek_peak", 0.0)))
            check("使用手机模式触发告警", cm.get("phone_alarm") is True,
                  "第 %s 帧触发" % cm.get("phone_at"))
            a_cheat = lk.raise_alert("CHEAT_PATTERN", ns.device, room_id="A-01",
                                     seat_no="12",
                                     detail="持续侧头面向邻座并出现手机，构成获取答案的动作模式")
            check("作弊告警已送进 03 终端", bool(a_cheat.get("alert_id")),
                  a_cheat.get("alert_id"))
            check("作弊告警同样带机位画质可信度并按可信度降级",
                  a_cheat.get("credibility") == f and a_cheat.get("downgraded") is True,
                  "level=%s credibility=%s" % (a_cheat.get("level"),
                                               a_cheat.get("credibility")))

        # ---------- 4. 01 人脸核验 + 画质上下文 ----------
        print("\n[5/6] 01 人脸核验（并带上机位画质上下文）")
        cands = st.get("01", {}).get("candidates") or []
        if cands:
            ref = os.path.join(linkage.PROJECTS["01"], "data", "ref_photos",
                               "%s.jpg" % ns.candidate)
            if not os.path.exists(ref):
                ref = p_normal
            fv = lk.face_verify(ref, candidate=ns.candidate, mode="1:1",
                                device_code=ns.device)
            check("01 返回核验结论", fv.get("ok") and bool(fv.get("result")),
                  "%s / %s" % (fv.get("result"), fv.get("reason_code")))
            check("核验结论附带了机位画质可信度",
                  "camera_credibility" in fv,
                  "×%s" % fv.get("camera_credibility"))
            check("画质不可信的机位会给出人工复核提示",
                  fv.get("trustworthy") is False and bool(fv.get("warning")),
                  fv.get("warning"))
            fv2 = lk.face_verify(ref, candidate=ns.candidate, mode="1:1",
                                 device_code=d2.get("code"))
            check("画质正常机位不触发降权提示",
                  fv2.get("trustworthy") is True and not fv2.get("warning"),
                  "×%s" % fv2.get("camera_credibility"))
        else:
            check("01 有考生数据（跳过核验演示）", False, "01 尚未导入考生/建档")

        # ---------- 5. 复核回流 + 审计 ----------
        print("\n[6/7] 处置结果回流 03 并留痕")
        cl = lk.close_alert(a_bad.get("alert_id"), "confirmed", "画质不可信，已现场核实",
                            operator="linkage")
        check("复核结论已回流留痕", cl.get("ok"), cl.get("message"))
        al = lk.alerts(limit=10)
        ids = [x["alert_id"] for x in al.get("alerts", [])]
        check("终端可查到刚接入的告警", a_bad.get("alert_id") in ids,
              "共 %d 条" % len(ids))
        reviewed = [x for x in al.get("alerts", [])
                    if x["alert_id"] == a_bad.get("alert_id")]
        check("告警状态更新为已复核",
              reviewed and reviewed[0].get("review_conclusion") == "confirmed",
              reviewed[0].get("review_conclusion") if reviewed else "—")
        s3 = lk.p03.call("status", {})
        check("03 审计链完整", s3.get("audit_ok") is True
              and s3.get("system") == "03_智能终端操作", s3.get("audit_chain"))

        # ---------- 6. 06 安检 -> 03 终端 ----------
        print("\n[7/7] 06 智能安检报警进 03 终端（事前阻断 -> 处置留痕）")
        sp = lk.security_pass({"device_code": "GATE-LINK-1", "room_id": "A-01",
                               "seat_no": "21", "candidate_id": "LINK-1",
                               "candidate_name": "联动考生",
                               "sensors": {"phone": 0.91, "millimeter": 0.72}})
        check("06 判出手机报警（双判据 -> 红）",
              sp.get("ok") and sp.get("result") == "alarm" and sp.get("level") == "red",
              "%s %s" % (sp.get("kinds"), str(sp.get("detail"))[:60]))
        rp = lk.p06.call("report_alarm", {"alarm_id": sp.get("alarm_id")})
        check("安检报警已送进 03 终端", rp.get("ok") and bool(rp.get("alert_id")),
              "告警号 %s 级别 %s" % (rp.get("alert_id"), rp.get("level")))
        al3 = lk.alerts(limit=30)
        items3 = al3.get("alerts", [])
        hit = [x for x in items3 if x["alert_id"] == rp.get("alert_id")]
        check("03 能查到该安检告警", bool(hit), "共 %d 条" % len(items3))
        check("告警码为 PHONE_DETECTED（手机/耳机类）",
              bool(hit) and hit[0].get("code") == "PHONE_DETECTED",
              hit[0].get("code") if hit else "-")
        lk6 = lk.p06.call("add_locker", {"code": "LOCK-LINK", "name": "联动保管柜",
                                         "cells_total": 10})
        check("06 保管柜可登记（查获器材要能入柜）", lk6.get("ok"),
              "柜格 %s" % lk6.get("cells_total"))
        rj = lk.security_reinspect(sp.get("alarm_id"), "detain", operator="linkage")
        check("复检结论在 06 留痕（查获暂扣并入柜）",
              rj.get("ok") and "保管柜" in str(rj.get("message")), rj.get("message"))
        st6 = lk.security_status()
        check("06 概览可查（过检/待处置/效率/样本指标）",
              st6.get("ok") and "throughput_ppm" in st6,
              "过检 %s 条，待处置 %s，效率 %.1f 人/分钟"
              % (st6.get("passes"), st6.get("pending_alarms"),
                 st6.get("throughput_ppm", 0)))

        # ---------- 汇总 ----------
        total = len(RESULTS)
        passed = sum(1 for _, o, _ in RESULTS if o)
        print("\n" + "=" * 78)
        if passed == total:
            print("联动自检通过：%d/%d 项全部符合预期" % (passed, total))
        else:
            print("联动自检未通过：%d/%d 项" % (passed, total))
            for name, o, detail in RESULTS:
                if not o:
                    print("   FAIL %s　%s" % (name, detail))
        print("链路：02 查画质 -> 02 出可信度 -> 03 告警按可信度降级 -> 01 核验带画质上下文")
        print("      -> 复核回流 03 留痕 -> 02 画质问题走工单闭环")
        print("      -> 06 安检判据出报警 -> 经 00 进 03 处置 -> 06 复检结论留痕")
        print("=" * 78)
        return 0 if passed == total else 1
    finally:
        if ns.keep:
            print("临时目录保留：%s" % tmp)
        else:
            for p in (tmp,):
                if p:
                    shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
