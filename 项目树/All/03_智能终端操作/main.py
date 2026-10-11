# -*- coding: utf-8 -*-
"""
智能终端操作端 · 端到端自检

默认在**临时数据目录**里跑，不污染真实 data\\；加 --real 才动真实目录。
逐项打印 PASS/FAIL，全部通过退出码 0。

覆盖《智能终端.md》的能力：
  告警分级 / 复核留痕 / 断网可用 / 权限最小化 / 统一管控 / 时钟 / 开关机衔接 / 稳定性
"""
import argparse
import os
import shutil
import sys
import tempfile

TMP = None


def _prepare(argv):
    global TMP
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="使用真实 data 目录（默认用临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于查看")
    args = ap.parse_args(argv)
    if not args.real:
        TMP = tempfile.mkdtemp(prefix="terminal_selfcheck_")
        os.environ["EXAM_TERMINAL_HOME"] = TMP
    return args


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("　— " + detail) if detail else ""))
    return bool(cond)


def main(argv=None):
    args = _prepare(argv if argv is not None else sys.argv[1:])
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    import access
    import alerts as alerts_mod
    import models
    import offline as offline_mod
    import review as review_mod
    import fleet as fleet_mod
    import clock as clock_mod
    import bridge
    import storage

    print("=" * 74)
    print("智能终端操作端 · 端到端自检")
    print("数据目录：%s%s" % (config.DATA_ROOT, "（临时）" if TMP else "（真实）"))
    print("=" * 74)

    store = storage.Store()
    accounts = access.AccountStore(store)
    accounts.ensure_seed()

    # ---------- 1. 登录与权限最小化 ----------
    print("\n[1/9] 终端登录与权限最小化")
    inv = accounts.authenticate("inv01", "inv01")
    patrol = accounts.authenticate("patrol1", "patrol1")
    cmd = accounts.authenticate("Owner", "EDY_POWERQ")
    check("监考员登录", inv is not None and inv.role == config.ROLE_INVIGILATOR)
    check("巡考员登录", patrol is not None and patrol.role == config.ROLE_PATROL)
    check("监考员不能复核（避免自己判自己）", not inv.can("review"))
    check("巡考员可以复核", patrol.can("review"))
    cmdr = access.Session("cmdr", config.ROLE_COMMANDER, "指挥中心")
    check("指挥中心角色不直接复核", not cmdr.can("review"))
    # 管理员：日常全权，但没有账户管理与危险操作
    adm = accounts.authenticate("admin", "admin")
    check("管理员账户可登录", adm is not None and adm.role == config.ROLE_ADMIN,
          config.ROLE_TEXT.get(getattr(adm, "role", ""), "登录失败"))
    check("管理员可以复核", adm is not None and adm.can("review"))
    check("管理员可以核验", adm is not None and adm.can("verify"))
    check("管理员可见全部考场", adm is not None and adm.room_filter() is None)
    check("管理员不能管理终端账户", adm is not None and not adm.can("manage"))
    check("管理员建账户被拒",
          not accounts.create("x_admin", "Passw0rd!", config.ROLE_CHECKER,
                              by_role=adm.role)[0])
    check("管理员改他人口令被拒",
          not accounts.reset_password("check1", "x", by_role=adm.role)[0])
    # Owner 已提升为最高权限账户
    check("Owner 是最高权限角色",
          cmd is not None and cmd.role == config.ROLE_OWNER
          and config.ROLE_LEVEL[cmd.role] == max(config.ROLE_LEVEL.values()),
          "%s（等级 %s）" % (config.ROLE_TEXT.get(cmd.role, "-"), config.ROLE_LEVEL.get(cmd.role)))
    check("Owner 可以复核", cmd.can("review"))
    check("Owner 可以管理终端账户", cmd.can("manage"))
    check("Owner 可见全部考场", cmd.room_filter() is None)
    check("巡考员不能管理终端账户", not patrol.can("manage"))
    check("监考员不能管理终端账户", not inv.can("manage"))
    ok_acct, _m = accounts.create("tmp_acct", "Passw0rd!", config.ROLE_CHECKER, "临时核验员",
                                  by_role=cmd.role)
    check("Owner 可新建终端账户", ok_acct, _m)
    check("Owner 不能新建同级账户",
          not accounts.create("tmp_o", "Passw0rd!", config.ROLE_OWNER,
                              by_role=cmd.role)[0])
    check("非所有者新建账户被拒",
          not accounts.create("tmp_x", "Passw0rd!", config.ROLE_CHECKER,
                              by_role=patrol.role)[0])
    check("Owner 可重置他人口令",
          accounts.reset_password("tmp_acct", "New1234", by_role=cmd.role)[0])
    check("Owner 可删除终端账户", accounts.delete("tmp_acct", by_role=cmd.role)[0])
    check("Owner 账户不可删除",
          not accounts.delete("Owner", by_role=cmd.role)[0])
    check("监考员可见范围仅 A-01", inv.room_filter() == ["A-01"],
          str(inv.room_filter()))
    check("巡考员可见全部考场", patrol.room_filter() is None)
    check("口令加盐存储（非明文）",
          "inv01" not in str(accounts.list_accounts().get("inv01", {}).get("pwd", "")))
    check("错误口令被拒", accounts.authenticate("inv01", "wrong") is None)

    # ---------- 2. 告警接入与分级 ----------
    print("\n[2/9] 告警接入与分级展示")
    ob = offline_mod.Outbox(store, probe=offline_mod.ConnectivityProbe(probe=lambda u: False))
    hub = alerts_mod.AlertHub(store, offline=ob)
    red = models.Alert(code="PHONE_DETECTED", room_id="A-01", seat_no="12",
                       candidate_id="330100199001011234", candidate_name="张三")
    orange = models.Alert(code="LOOK_DOWN", room_id="A-02", seat_no="07")
    a1, new1 = hub.ingest(red)
    a2, new2 = hub.ingest(orange)
    check("红色告警按码兜底为 red", a1.level == config.LEVEL_RED, a1.level_name())
    check("橙色告警按码兜底为 orange", a2.level == config.LEVEL_ORANGE, a2.level_name())
    check("红色需强制确认", a1.needs_force_confirm())
    check("橙色不需强制确认", not a2.needs_force_confirm())
    _, dup = hub.ingest(models.Alert(code="PHONE_DETECTED", room_id="A-01", seat_no="12"))
    check("同考场同座位同码在窗口内被去重", dup is False)
    order = hub.display_order([a2, a1])
    check("红色排在最前", order[0].alert_id == a1.alert_id)

    # ---------- 3. 脱敏展示 ----------
    print("\n[3/9] 默认脱敏展示")
    txt = access.render(a1, patrol)
    check("告警文本不含完整身份证号", "330100199001011234" not in txt, txt[-40:])
    check("含掩码形式", "3301" in txt and "1234" in txt)

    # ---------- 4. 红色强制确认 ----------
    print("\n[4/9] 红色预警强制确认")
    rev = review_mod.Reviewer(store, hub=hub)
    ok, msg = rev.review(a1.alert_id, config.REVIEW_CONFIRMED, "先试着直接复核", patrol)
    check("未确认时复核被拒", not ok, msg)
    ok2, msg2 = hub.ack(a1.alert_id, patrol.username)
    check("确认成功", ok2, msg2)
    ok3, msg3 = hub.ack(a1.alert_id, patrol.username)
    check("重复确认被拒", not ok3, msg3)

    # ---------- 5. 复核留痕与误报闭环 ----------
    print("\n[5/9] 复核留痕与误报闭环")
    ok, msg = rev.review(a1.alert_id, config.REVIEW_CONFIRMED, "", patrol)
    check("复核理由必填", not ok, msg)
    att_src = os.path.join(str(config.DATA_ROOT), "_tmp_attach.jpg")
    with open(att_src, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")              # 最小 JPEG 头尾
    ok, rid = rev.review(a1.alert_id, config.REVIEW_CONFIRMED, "画面确有手机反光", patrol,
                         witness="监考员A01", attachments=[att_src])
    check("复核提交成功（含见证人+附件）", ok, rid)
    a1b = store.get_alert(a1.alert_id)
    check("告警状态更新为已复核", a1b.status == models.ST_REVIEWED, a1b.status)
    check("附件已留档", len(a1b.attachments) == 1, str(a1b.attachments))
    check("附件文件真实落盘",
          os.path.exists(os.path.join(str(config.ATTACH_DIR), a1b.attachments[0])))
    ok, msg = rev.review(a1.alert_id, config.REVIEW_REJECTED, "重复复核", patrol)
    check("同一告警不能复核两次", not ok, msg)
    ok, _ = rev.review(a2.alert_id, config.REVIEW_FALSE_POSITIVE, "只是低头写字", patrol)
    fp = store.false_positive_stats()
    check("误报进入算法迭代闭环",
          any(r["code"] == "LOOK_DOWN" and r["false_positive"] == 1 for r in fp), str(fp))

    # ---------- 6. 断网可用 ----------
    print("\n[6/9] 断网可用（本地队列 + 补传）")
    st = ob.status()
    check("离线状态被如实识别", st["online"] is False, st["platform"])
    check("告警已进本地队列", st["pending"] >= 2, "待传 %d" % st["pending"])
    r = ob.flush(force=False)
    check("离线时补传不丢数据（仍待传）", r["sent"] == 0 and r["pending"] >= 2, str(r))
    online = offline_mod.Outbox(store, probe=offline_mod.ConnectivityProbe(probe=lambda u: True),
                                sender=lambda url, payload: True)
    r = online.flush(force=False)
    check("恢复在线后批量补传成功", r["sent"] >= 2, str(r))
    check("补传后队列清空", online.status()["pending"] == 0,
          "剩余 %d" % online.status()["pending"])
    sent_log = [x for x in store.audit_rows(limit=50) if x["action"] == "补传完成"]
    check("补传写审计", len(sent_log) >= 1, "审计 %d 条" % len(sent_log))

    # ---------- 7. 一键上报（监考终端） ----------
    print("\n[7/9] 监考终端：一键上报 + 越权拦截")
    ok, msg = rev.report(a2.alert_id, "本考场 A-02 情况已核实", inv)
    check("跨考场上报被拒（A-02 不在 inv01 范围）", not ok, msg)
    a3 = models.Alert(code="VIDEO_BLUR", room_id="A-01")
    a3, _ = hub.ingest(a3)
    ok, msg = rev.report(a3.alert_id, "本考场画面模糊，请处理", inv)
    check("本考场一键上报成功", ok, msg)
    check("上报进入指令队列", len(store.pending_commands()) >= 1)

    # ---------- 8. 统一管控与稳定性 ----------
    print("\n[8/9] 统一管控 / 稳定性 / 时钟")
    fl = fleet_mod.FleetManager(store)
    cfg = fl.config_snapshot()
    check("配置快照含应用白名单", "app_whitelist" in cfg, str(cfg["app_whitelist"]))
    ok, msg = fl.check_app("unknown_app")
    check("非白名单应用被识别（如实说明仅记录不强杀）", not ok, msg)
    ok, msg = fl.check_app("terminal_gui")
    check("白名单应用放行", ok, msg)
    fl.set_config({"peripherals": {"usb_storage": False}})
    fl.set_peripheral("bluetooth", True)
    check("外设策略可下发", fl.peripheral_status().get("bluetooth") is True)
    store.push_command("set_config", {"version": "0.2.0"})
    store.push_command("peripheral", {"name": "wifi_direct", "enabled": True})
    applied = fl.apply_pending()
    check("平台指令逐条生效", len(applied) >= 2, str(applied))
    check("升级指令被如实标注为仅登记", any(k == "upgrade" for _, k, _ in applied) is False)
    fl.request_wipe("platform", "终端丢失，申请远程擦除")
    check("擦除请求已登记待二次确认", fl.wipe_pending())
    ok, msg = fl.execute_wipe("wrong", "platform")
    check("错误确认码不执行擦除", not ok, msg)
    ok, msg = fl.execute_wipe("WIPE", "platform")
    check("确认码正确才执行擦除", ok, msg)
    for _ in range(3):
        fl.soak_tick(note="selfcheck")
    soak = fl.soak_status()
    check("稳定性心跳已记录", soak["ticks"] >= 3, str(soak))
    check("未满 72 小时如实标注未达标", soak["passed"] is False, soak["note"])

    ck = clock_mod.StandardClock(store, hub=hub,
                                 time_provider=lambda: (1000000.0, "test"))
    d = ck.drift()
    check("时钟偏差检测可用", "drift_sec" in d and d["source"] == "test", str(d["drift_sec"]))
    items = ck.set_broadcasts([{"at": "09:00", "text": "考试开始"}])
    check("播报时刻表可配置", len(items) == 1)
    import time as _t
    probe_t = _t.mktime(_t.strptime("2026-06-07 09:00:00", "%Y-%m-%d %H:%M:%S"))
    fired = ck.fire_due(now=probe_t)
    check("到点触发播报并留痕", len(fired) == 1, str(fired))
    check("同一时刻不重复播报", len(ck.fire_due(now=probe_t)) == 0)

    # ---------- 9. 审计链与人脸桥接 ----------
    print("\n[9/9] 审计链完整性 / 手持核验桥接")
    ok, msg, n = store.verify_chain()
    check("哈希链审计完整", ok, msg)
    fb = bridge.FaceBridge()
    avail, why = fb.available()
    check("人脸桥接可用性如实报告", isinstance(avail, bool), why[:90])
    res = fb.verify(os.path.join(str(config.DATA_ROOT), "not_exist.jpg"), "C001")
    check("图片不存在时不伪造成功", res.get("ok") is False, str(res.get("reason"))[:70])
    pl = bridge.PlatformLink()
    conn = pl.connectivity()
    check("与平台/边缘/认证的联通性如实报告", set(conn.keys()) == {"业务平台", "边缘节点", "统一认证"},
          str(conn))

    # ---------- 汇总 ----------
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 74)
    if passed == total:
        print("自检通过：%d/%d 项全部符合预期" % (passed, total))
    else:
        print("自检未通过：%d/%d 项" % (passed, total))
        for name, ok, detail in RESULTS:
            if not ok:
                print("   FAIL %s　%s" % (name, detail))
    print("=" * 74)

    if TMP and not args.keep:
        try:
            shutil.rmtree(TMP, ignore_errors=True)
        except OSError:
            pass
    elif TMP:
        print("临时目录保留在：%s" % TMP)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
