# -*- coding: utf-8 -*-
"""发布命令链路联调：03 下发 → 01/02 执行器拉取并执行 → 回执落库

用真实代码路径（不是模拟）：
  · 03 侧：lan_service.LanService 的 handle_publish_command / commands_for /
           handle_command_result（HTTP 层只是薄封装，这里直接调业务方法）
  · 设备侧：00_集成联动/command_runner.py 的 execute()（01/02 共用的那份）
"""
import os
import sys
import tempfile

ALL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T03 = os.path.join(ALL, "03_智能终端操作")
T00 = os.path.join(ALL, "00_集成联动")

TMP = tempfile.mkdtemp(prefix="cmd_link_")
os.environ["EXAM_TERMINAL_HOME"] = TMP
sys.path.insert(0, T03)
sys.path.insert(0, T00)

import storage            # noqa: E402  (03 的)
import lan_service        # noqa: E402  (03 的)
import command_runner     # noqa: E402  (设备侧共用)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


store = storage.Store()
svc = lan_service.LanService(store=store, token="test-token")
DEV = "PC-A-01"

print("临时数据目录：%s\n" % TMP)

print("=== 1) 白名单：非法命令必须被拒 ===")
st, out = svc.handle_publish_command({"target": DEV, "kind": "rm -rf /"})
check("非白名单命令被拒", st == 400 and not out.get("ok"), str(out.get("reason"))[:80])
check("被拒的命令没有落库", not store.commands_for(DEV))

print("\n=== 2) 合法命令下发 ===")
st, out = svc.handle_publish_command({"target": DEV, "kind": "ping",
                                      "params": {}, "operator": "owner"})
check("下发成功", st == 200 and out.get("ok"), out.get("cmd_id", ""))
cmd_id = out.get("cmd_id", "")
st2, out2 = svc.handle_publish_command({"target": "PC-B-02", "kind": "status",
                                        "operator": "owner"})
check("给另一台设备下发", st2 == 200 and out2.get("ok"))

print("\n=== 3) 目标设备拉取（只应拿到给自己的） ===")
r = svc.commands_for(DEV)
check("拉取成功", r.get("ok"))
kinds = [c["kind"] for c in r.get("commands") or []]
check("拿到 1 条且是 ping", kinds == ["ping"], "实际=%s" % kinds)
check("命令里带完整参数", (r["commands"][0].get("params") == {}
                       and r["commands"][0].get("operator") == "owner"))
b = svc.commands_for("PC-B-02")
check("另一台只拿到自己的 status", [c["kind"] for c in b.get("commands") or []] == ["status"])

print("\n=== 4) 设备侧执行器执行该命令 ===")
ok, res = command_runner.execute("02", {"kind": "ping", "params": {}})
check("ping 执行成功", ok, "hostname=%s" % (res or {}).get("hostname"))
ok2, res2 = command_runner.execute("02", {"kind": "不存在的命令"})
check("执行器也拒绝白名单外的命令", not ok2, str(res2.get("reason"))[:80])
ok3, res3 = command_runner.execute("02", {"kind": "logs_tail", "params": {"lines": 5}})
check("logs_tail 有明确结果（有日志就读，没有就如实说）",
      ok3 or "未找到日志" in str(res3.get("reason")) or "失败" in str(res3.get("reason")),
      ("读了 %s" % res3.get("file")) if ok3 else str(res3.get("reason"))[:70])

print("\n=== 5) 回执回到 03 并落库 ===")
st, out = svc.handle_command_result({"cmd_id": cmd_id, "ok": bool(ok), "result": res,
                                     "operator": DEV})
check("回执被接受", st == 200 and out.get("recorded"))
ledger = svc.list_commands(limit=20)
mine = [c for c in ledger.get("commands") or [] if c.get("cmd_id") == cmd_id]
check("台账里能找到该命令", len(mine) == 1)
if mine:
    c = mine[0]
    check("已标记执行", int(c.get("applied") or 0) == 1)
    check("状态为 ok", c.get("status") == "ok")
    check("结果里有设备回报的字段", (c.get("result_obj") or {}).get("hostname") is not None)
check("拉取后不再重复下发（applied=1）", not [c for c in (svc.commands_for(DEV).get("commands") or [])
                                          if c["cmd_id"] == cmd_id])
check("缺 cmd_id 的回执被拒", svc.handle_command_result({"ok": True})[0] == 400)

print("\n=== 6) 审计留痕 ===")
actions = [r["action"] for r in store.audit_rows(limit=100)]
check("下发命令已进审计链", "下发命令" in actions, "共 %d 条" % actions.count("下发命令"))
check("命令回执已进审计链", "命令回执" in actions, "共 %d 条" % actions.count("命令回执"))
ok_chain, why, _ = store.verify_chain()
check("审计链完整", ok_chain, why)

print("\n" + "=" * 60)
print("发布命令链路联调：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
