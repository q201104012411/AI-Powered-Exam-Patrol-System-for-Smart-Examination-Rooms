# -*- coding: utf-8 -*-
"""03 清空能力冒烟测试（临时数据目录，不碰真实数据）

验证：
  1) mark 模式：数据仍在库，但标记 purged（可查不可办）
  2) hard 模式：行真的被删
  3) queue 范围：把未同步标记为已同步（丢弃待补传），不删业务数据
  4) 每次清空都写 audit 哈希链 + purge_log 留痕，且链仍完整
  5) reason 必填（数据维护要能解释为什么删）
"""
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="purge_smoke_")
os.environ["EXAM_TERMINAL_HOME"] = TMP
sys.path.insert(0, HERE)

import config                     # noqa: E402  （必须在设置 EXAM_TERMINAL_HOME 之后导入）
import storage                    # noqa: E402
import models                     # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def seed(store, n=3):
    for i in range(n):
        a = models.Alert(code="PHONE_DETECTED", title="测试告警%d" % i,
                         detail="明细%d" % i, room_id="A-01", seat_no="%02d" % (i + 1),
                         candidate_id="33010019900101001%d" % i, candidate_name="考生%d" % i,
                         device_id="CAM-A01-1", source="edge")
        store.save_alert(a)
    return n


print("临时数据目录：%s" % TMP)
store = storage.Store()
print("数据库：%s" % config.DB_PATH)

print("\n=== 1) reason 必填 ===")
ok, msg, _ = store.purge_data("local", operator="owner", reason="")
check("空原因被拒", not ok, msg)

print("\n=== 2) mark 模式：数据仍在，但已标记 ===")
n = seed(store, 3)
ok, msg, detail = store.purge_data("local", mode="mark", operator="owner", reason="月度归档")
check("mark 执行成功", ok, msg)
rows = store.list_alerts(limit=100)
with_purged = [a for a in rows if getattr(a, "purged", 0)]
check("数据仍在库（没有被删）", len(rows) >= 3, "库内 %d 条" % len(rows))
check("已打 purged 标记", len(with_purged) == 3, "标记 %d 条" % len(with_purged))
check("标记里带操作人与原因", all(a.purged_by == "owner" and a.purge_reason == "月度归档"
                                for a in with_purged))

print("\n=== 3) queue 范围：只丢待补传，不删业务数据 ===")
before = len(store.list_alerts(limit=500))
store.purge_data("queue", operator="owner", reason="清理积压")
after = len(store.list_alerts(limit=500))
check("告警条数不变（不误删）", before == after, "%d -> %d" % (before, after))
unsynced = [a for a in store.list_alerts(limit=500) if not a.synced]
check("待补传已清零", len(unsynced) == 0, "剩余未同步 %d" % len(unsynced))

print("\n=== 4) hard 模式：行真的被删 ===")
ok, msg, detail = store.purge_data("local", mode="hard", operator="owner", reason="考试结束清除")
check("hard 执行成功", ok, msg)
left = len(store.list_alerts(limit=500))
check("本机告警已清空", left == 0, "剩余 %d 条" % left)
check("复核留痕已清空", len(store.list_reviews(limit=500)) == 0)

print("\n=== 5) 远端记录清空 + 台账归零 ===")
store.save_remote_batch("PC-A-01", "考场A终端", "check",
                        [{"record_id": "r1", "action": "机位核查", "detail": "模糊",
                          "operator": "op", "ts": models.now_ts()}])
check("远端记录已入库", store.remote_summary()["records"] >= 1)
ok, msg, _ = store.purge_data("remote", operator="owner", reason="归档远端数据")
check("远端记录已清空", store.remote_summary()["records"] == 0, msg)
check("台账当前存量归零", all(d["records"] == 0 for d in store.remote_devices()),
      "台账 %d 台" % len(store.remote_devices()))

print("\n=== 6) 留痕与审计链 ===")
logs = store.purge_rows()
check("purge_log 有记录", len(logs) >= 4, "%d 条" % len(logs))
check("留痕含范围/模式/原因", all(r["scope"] and r["mode"] and r["reason"] for r in logs))
ok_chain, why, cnt = store.verify_chain()
check("审计链仍完整（清空没破坏链）", ok_chain, "%s（%d 条）" % (why, cnt))
audit_actions = [r["action"] for r in store.audit_rows(limit=100)]
check("清空动作已进审计链", audit_actions.count("数据清空") >= 4,
      "共 %d 条清空审计" % audit_actions.count("数据清空"))

print("\n=== 7) 不允许删库文件（WinError 32 的根因）===")
check("数据库文件仍在（没被删）", os.path.exists(str(config.DB_PATH)))
check("清空后仍可正常写入（可继续用）",
      (store.save_alert(models.Alert(code="OFFLINE", title="清空后新告警",
                                     device_id="CAM-A01-1")) or True)
      and len(store.list_alerts(limit=10)) >= 1)

print("\n" + "=" * 60)
print("清空能力冒烟：%d 项通过，%d 项失败" % (PASS, FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
