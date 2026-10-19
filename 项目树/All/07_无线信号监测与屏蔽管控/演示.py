# -*- coding: utf-8 -*-
r"""
07 无线信号监测与屏蔽管控 · 一键演示（命令行）

跑一遍完整链路，产出可当场看到的数字与报告：

  ① 载入演示数据（监测站/屏蔽器/审批/场次/屏蔽与外溢实测）
  ② 回放 8 个频谱场景（检出率、误报、时延、定位误差）
  ③ 屏蔽管控：按时间表 tick（开考前自动开机）-> 查看状态与开关记录 -> 演示结束关闭
  ④ 合规体检 + 生成验收报告（md/csv）
  ⑤ 联动状态（00/02/03/06 可达性；可选关联 06 安检结论）

用法：
    python 演示.py                # 写入本模块真实 data 目录（演示数据，均带"演示"标记）
    python 演示.py --sandbox      # 写入临时目录，不碰真实数据（推荐先这样看效果）
    python 演示.py --no-link      # 跳过联动探测（现场没有其它模块时更快）
"""
import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", action="store_true", help="用临时数据目录")
    ap.add_argument("--no-link", action="store_true", help="跳过联动探测")
    ap.add_argument("--hours", type=float, default=24 * 30,
                    help="报告统计区间（小时）")
    args = ap.parse_args(argv)

    if args.sandbox and not os.environ.get("EXAM_RF_HOME"):
        os.environ["EXAM_RF_HOME"] = tempfile.mkdtemp(prefix="rf_demo_")

    import compliance
    import config
    import features
    import models
    import report as report_mod
    import samples
    import shield
    import storage

    print("=" * 76)
    print("07 无线信号监测与屏蔽管控 · 一键演示")
    print("数据目录：%s" % config.DATA_ROOT)
    print("=" * 76)

    store = storage.Store()
    features.ensure_builtin(store)
    seed = samples.seed_demo(store)
    print("\n① 演示数据：设备 %d 台（监测 %d / 侦测 %d / 屏蔽 %d）、场次 %d、审批 %d、"
          "屏蔽实测 %d、外溢实测 %d"
          % (seed["devices"], store.summary()["monitors"], store.summary()["probes"],
             store.summary()["jammers"], seed["fields"], seed["approvals"],
             seed["shield_tests"], seed["spills"]))

    print("\n② 回放测试（模拟频谱，验证算法链）")
    run = samples.run_replay(store, operator="演示")
    for it in run["items"]:
        print("   %-3s %-22s %s  %s"
              % (it["scenario_id"], it["name"],
                 "通过" if it["ok"] else "不通过", it["detail"][:70]))
    print("   检出率 %.0f%% ｜ 误报 %d ｜ 平均时延 %.1fs ｜ 最大时延 %.1fs ｜ 定位误差 %.1fm"
          % (float(run["detect_rate"]) * 100, run["false_alarm"],
             float(run["avg_latency"]), float(run["max_latency"]),
             float((run["metrics"] or {}).get("locate_error_avg_m") or 0)))
    print("   （模拟数据均标 source=sim，不代表真实设备检出率）")

    print("\n③ 屏蔽管控（时间表驱动）")
    now = models.now_epoch()
    tick = shield.tick(store, now=now, operator="演示")
    if tick.get("actions"):
        for a in tick["actions"]:
            print("   %s %s：%s" % (a["device_code"], a["action"],
                                   "成功" if a["ok"] else "失败：%s" % a.get("reason")))
    else:
        print("   当前不在考试窗口（%s），未自动开机 —— 这是红线要求的正常行为"
              % (tick.get("phase") or "无场次"))
    # 上一轮演示结束时人工关过机：同一场次内自动策略会暂停（防止把人的决定顶掉）。
    # 演示要能看到"开启状态"，所以这里走一次正规的人工开启（同样过审批/窗口/定向闸门）。
    for jam in store.list_devices(kind=config.DEV_JAMMER):
        st_now = store.get_state(jam.device_id)
        if st_now is not None and st_now.is_on:
            continue
        r = shield.turn_on(store, jam, operator="演示",
                           reason="演示：人工开启（自动策略暂停或未到 tick）",
                           now=models.now_epoch())
        if r.get("ok"):
            print("   %s 人工开启成功（%.1fW）" % (jam.code, r["power_w"]))
        else:
            print("   %s 无法开启：%s" % (jam.code, r.get("reason")))
    for jam in store.list_devices(kind=config.DEV_JAMMER):
        sv = shield.state_view(store, jam)
        print("   %-6s %s ｜ %s ｜ %.1fW ｜ %s"
              % (jam.code, "开启" if sv["on"] else "关闭",
                 sv["bands_text"][:42], sv["power_w"],
                 sv["session"] or "无场次"))
    print("   开关记录 %d 条（含操作人/频段/功率/依据批文，全部可审计）"
          % len(store.list_switches(limit=1000)))
    # 演示结束：把本次演示打开的屏蔽器关掉，避免留下"非考试时段仍在发射"的状态
    closed = 0
    for jam in store.list_devices(kind=config.DEV_JAMMER):
        if (store.get_state(jam.device_id) or models.JammerState()).is_on:
            r = shield.turn_off(store, jam, operator="演示",
                                reason="演示结束，关闭屏蔽", now=models.now_epoch())
            closed += 1 if r.get("ok") else 0
    if closed:
        print("   演示结束：已关闭 %d 台屏蔽器（手动提前关闭会被一致性核对标为"
              "“覆盖不完整”，这是故意的——真实考试必须等结束才关）" % closed)

    print("\n④ 合规体检")
    chk = compliance.checks(store)
    print("   %s" % chk["summary_text"])
    for r in chk["rows"]:
        if r["level"] != "pass":
            print("   [%s] %s：%s" % ("需关注" if r["level"] == "warn" else "不通过",
                                      r["name"], r["detail"][:90]))

    print("\n⑤ 生成验收报告")
    ok, info = report_mod.write_report(store, hours=args.hours, operator="演示")
    if ok:
        print("   %s" % info["conclusion"])
        print("   Markdown：%s" % info["md"])
        print("   CSV     ：%s" % info["csv"])
    else:
        print("   生成失败：%s" % info)

    if not args.no_link:
        print("\n⑥ 联动状态（只调用，不修改其它模块）")
        try:
            import linkage_bridge
            link = linkage_bridge.link_status()["client"]
            print("   链路模式：%s ｜ 03 地址：%s ｜ 沙箱：%s"
                  % (link.get("mode"), link.get("terminal_url"),
                     link.get("sandbox_root")))
            print("   模块：%s" % "、".join(
                "%s=%s" % (k, "可用" if v.get("available") else "不可用")
                for k, v in (link.get("modules") or {}).items()))
            r = linkage_bridge.correlate_06(store, operator="演示")
            if r.get("ok"):
                print("   关联 06 安检结论：新增 %d 条，关注考场 %s"
                      % (r.get("new", 0),
                         "、".join(x["room_id"] for x in
                                  (r.get("focus_rooms") or [])) or "-"))
            else:
                print("   关联 06：%s" % r.get("reason"))
        except Exception as e:                                # noqa: BLE001
            print("   联动探测跳过：%s" % e)

    print("\n" + "=" * 76)
    print("演示完成。下一步：")
    print("  · 双击「启动.bat」看界面（总览/频谱/异常定位/屏蔽管控/时间表/合规/联动）")
    print("  · 双击「运行自检.bat」跑 375 项自检（临时目录，不碰真实数据）")
    print("  · 真实使用前请先改「⑧ 设置与审计」里的考点/操作员，"
          "并在「⑥ 合规与验收」登记真实批文与实测证据")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
