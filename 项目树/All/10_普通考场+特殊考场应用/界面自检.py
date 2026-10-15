# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 界面自检（无头，不弹窗）

Tk 在无显示环境下也能建控件（Windows 正常），所以这里直接建窗口、
**不进入 mainloop**，把每个页签与主要动作都点一遍，验证：
  1) 界面能建起来，7 个页签齐全
  2) 建考场 / 套模板 / 登记点位 / 机位自检 / 准入自检 / 启用 全流程不报错
  3) 告警试判、复核、分域、合规、指标、报告、审计 各面板都能渲染
  4) **全程在临时数据目录**，不碰真实 data\、也不碰 01/02/03/06 真实库

用法：python 界面自检.py
"""
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— %s" % detail) if detail else ""))
    return bool(cond)


def main():
    tmp = tempfile.mkdtemp(prefix="room_gui_selfcheck_")
    os.environ["EXAM_ROOM_HOME"] = tmp
    for m in ("config", "models", "storage", "rooms", "admission", "behavior",
              "alerting", "kpi", "privacy", "report", "linkage_client"):
        sys.modules.pop(m, None)
    try:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                          # noqa: BLE001
                pass
    except Exception:                                  # noqa: BLE001
        pass

    print("=" * 74)
    print("10 普通考场+特殊考场应用 · 界面自检（无头）")
    print("=" * 74)
    print("数据目录：%s" % tmp)

    def _snapshot_dir(path):
        """整个目录的 (文件数, 总字节, 最新修改时间) —— 比对自检前后有没有被动过。"""
        if not os.path.isdir(path):
            return None
        n = 0
        size = 0
        newest = 0
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

    real_data = os.path.join(HERE, "data")
    real_settings = os.path.join(real_data, "settings.json")
    settings_mtime_before = (os.stat(real_settings).st_mtime
                             if os.path.exists(real_settings) else None)
    before = _snapshot_dir(real_data)

    import config
    # 全程沙箱：跨系统调用不许污染 01/02/03/06 的真实运行库
    config.save_settings({"sandbox_root": os.path.join(tmp, "linkage")})

    try:
        import tkinter as tk
    except ImportError as e:                            # pragma: no cover
        check("tkinter 可用", False, str(e))
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as e:                            # pragma: no cover
        print("  SKIP 当前环境没有可用显示，跳过界面自检（%s）" % str(e)[:80])
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    root.withdraw()

    import gui
    import rooms as rooms_mod
    import storage

    app = gui.App(root)
    check("界面可建（App 初始化成功）", app is not None)

    import time as _time

    def pump(seconds=40.0):
        """
        等后台任务跑完（界面把耗时动作放到工作线程了）。
        边等边 root.update() 把队列里的结果渲染出来，模拟真实事件循环。
        """
        t0 = _time.time()
        while app.is_busy() and _time.time() - t0 < seconds:
            root.update()
            _time.sleep(0.05)
        root.update()
        return not app.is_busy()

    tabs = [app.nb.tab(i, "text") for i in app.nb.tabs()]
    check("7 个页签齐全", len(tabs) == 7, "、".join(tabs))
    check("Tk 版本可用", bool(root.tk.call("info", "patchlevel")))

    # ---- 建档 ----
    app.current = rooms_mod.upsert_room(
        app.store, "GUI 普通考场", config.TYPE_STANDARD, operator=app.operator,
        apply_reason="界面自检")
    app.refresh_rooms()
    check("经界面建档成功", app.current is not None
          and len(app.tree.get_children()) == 1,
          "%d 行" % len(app.tree.get_children()))

    # ---- 套模板 ----
    app.var_type.set("%s（普通）" % config.type_name(config.TYPE_STANDARD))
    app.do_apply_template()
    pump()
    app.current = app.store.get_room(app.current.room_id)
    check("界面套模板：点位 5 个", len(app.current.points) == 5,
          "%d 个点位" % len(app.current.points))
    check("界面右侧渲染了模板差异/能力面板",
          "能力建议" in app.txt_diff.get("1.0", "end"))
    check("点位表渲染出 5 行",
          len(app.tree_pts.get_children()) == 5,
          "%d 行" % len(app.tree_pts.get_children()))

    # ---- 一键流程 ----
    app.var_prefix.set("CAM-GUI-")
    app.do_step1()
    check("① 后台任务已结束", pump())
    app.current = app.store.get_room(app.current.room_id)
    check("① 登记点位编码", all(p.get("device_code") for p in app.current.points),
          str([p.get("device_code") for p in app.current.points]))
    import linkage_client
    reg, note = linkage_client.camera_registered_codes(refresh=True)
    check("① 点位已登记进 02 机位台账（单向写入）",
          reg is not None and all(p.get("device_code") in reg
                                  for p in app.current.points),
          note)
    app.do_step2()
    pump()
    check("② 机位自检有输出", "机位画质自检" in app.txt_admit.get("1.0", "end"))
    app.do_step3()
    pump()
    app.current = app.store.get_room(app.current.room_id)
    check("③ 在岗+座位建模+标定已落库",
          app.current.invigilator_count() >= 1 and bool(app.current.calibration_at)
          and app.current.ledger_ready == 1,
          "在岗 %d / 标定 %s" % (app.current.invigilator_count(),
                               app.current.calibration_at))
    app.do_admit()
    pump()
    check("④ 准入自检有结论输出",
          "准入自检结论" in app.txt_admit.get("1.0", "end"))
    app.do_arm()
    pump()
    app.current = app.store.get_room(app.current.room_id)
    check("⑤ 启用成功（状态为已就绪/降级可用）", app.current.armed(),
          app.current.status_text())

    # ---- 特殊考场：翻转开关 ----
    app.var_special_flags["points_verified"].set(True)
    app.do_toggle_flag("points_verified")
    app.current = app.store.get_room(app.current.room_id)
    check("开关写入生效", app.current.points_verified == 1)

    # ---- 告警 ----
    app.refresh_alerts()
    check("行为下拉已按本考场启用清单填充", len(app._beh_map) >= 40,
          "%d 项" % len(app._beh_map))
    labels = list(app._beh_map)
    phone = [x for x in labels if "使用手机" in x]
    if phone:
        app.var_beh.set(phone[0])
    app.var_conf.set("0.97")
    app.do_route_dry()
    pump()
    check("试判一次红色告警并渲染结论",
          "结论：" in app.txt_al.get("1.0", "end"))
    check("告警列表出现记录", len(app.tree_al.get_children()) >= 1,
          "%d 条" % len(app.tree_al.get_children()))
    sel = app.tree_al.get_children()[:1]
    if sel:
        app.tree_al.selection_set(sel[0])
    app.var_concl.set("false_positive")
    app.var_reason.set("界面自检：判为误报")
    app.do_review()
    check("复核留痕（误报进闭环）",
          any(r.result == "false_positive"
              for r in app.store.list_privacy(action="review", limit=10)))

    # ---- 分域 ----
    # 用当前考场**自己的域**（普通考场 = 常规考试记录域）验证授权/拦截，
    # 因为"角色对该考场所在域"才是真正要判的东西。
    app.do_partition()
    app.refresh_domain()
    dom_txt = app.txt_dom.get("1.0", "end")
    check("分区总览渲染", "数据分域" in dom_txt and "授权矩阵" in dom_txt)
    app.var_domain.set(config.DOMAIN_EXAM)
    app.var_role.set("invigilator")
    app.do_access_test()
    check("域规则外角色被拒绝（默认拒绝）", "拒绝" in app.status.cget("text"),
          app.status.cget("text")[:70])
    app.do_grant(True)
    app.do_access_test()
    check("独立授权后放行", "放行" in app.status.cget("text"),
          app.status.cget("text")[:70])
    app.do_grant(False)
    app.do_access_test()
    check("撤权后立即恢复默认拒绝", "拒绝" in app.status.cget("text"),
          app.status.cget("text")[:70])
    # 隔离域：健康信息域必须有独立分区才可访问
    app.var_domain.set(config.DOMAIN_HEALTH)
    app.var_role.set("doctor")
    app.do_access_test()
    check("健康信息域：本考场没有该域分区 -> 拒绝", "拒绝" in app.status.cget("text"),
          app.status.cget("text")[:70])

    # ---- 合规 ----
    app.var_witness.set("数据保护专员")
    app.var_pvdetail.set("界面自检：最小必要")
    app.do_offline()
    app.do_retention()
    app.var_days.set("20")
    app.do_retention()
    app.do_seal()
    app.refresh_privacy()
    check("合规面板渲染", "合规体检" in app.txt_pv.get("1.0", "end"))

    # ---- 指标 ----
    app.do_kpi()
    kpi_txt = app.txt_kpi.get("1.0", "end")
    check("验收指标渲染（含达标/未达标/缺失三分）",
          "达标" in kpi_txt and "证据来源" in kpi_txt)
    app.do_kpi_baseline()
    check("内置基线跑一次并标注合成",
          "合成" in app.txt_kpi.get("1.0", "end"))
    app.refresh_kpi()
    check("考点看板渲染", "考点验收看板" in app.txt_kpi.get("1.0", "end"))

    # ---- 报告 ----
    app.do_report_silent = True
    import report as report_mod
    ok, info = report_mod.write_report(app.store)
    check("界面生成验收报告", ok and os.path.exists(info["md"]),
          os.path.basename(info["md"]))

    # ---- 审计与联动 ----
    app.refresh_audit()
    check("审计面板渲染哈希链", "审计哈希链" in app.txt_aud.get("1.0", "end"))
    app.var_sandbox.set(os.path.join(tmp, "linkage"))
    app.do_sandbox()
    check("沙箱设置可写入", config.load_settings()["sandbox_root"].endswith("linkage"))
    app.do_linkage_probe()
    check("联动探测面板渲染", "00_集成联动" in app.txt_aud.get("1.0", "end"))

    # ---- 收尾 ----
    after = _snapshot_dir(real_data)
    check("自检全程没往真实 data\\ 写任何东西（目录快照前后一致）",
          before == after,
          "前 %s / 后 %s（文件数, 总字节, 最新 mtime）" % (before, after))
    settings_mtime_after = (os.stat(real_settings).st_mtime
                            if os.path.exists(real_settings) else None)
    check("真实 data\\settings.json 未被写入",
          settings_mtime_before == settings_mtime_after,
          "mtime %s -> %s" % (settings_mtime_before, settings_mtime_after))
    check("沙箱设置写进了临时目录",
          os.path.exists(os.path.join(tmp, "settings.json")),
          os.path.join(tmp, "settings.json"))
    check("业务数据落在临时目录",
          os.path.exists(os.path.join(tmp, "rooms.db")),
          os.path.join(tmp, "rooms.db"))
    root.destroy()

    print()
    print("=" * 74)
    print("界面自检结果：%d 项通过，%d 项失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL %s　%s" % (name, detail))
    print("=" * 74)
    if os.environ.get("GUI_SELFCHECK_KEEP"):
        print("（保留临时目录：%s）" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
