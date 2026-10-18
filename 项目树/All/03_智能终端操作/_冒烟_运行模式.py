# -*- coding: utf-8 -*-
"""运行模式（考试 / 非考试·教学）自检：真建库、真切换、真校验。

验证的是"两种模式**真的不一样**"，不是只有一段文案：
  1) 默认是考试模式；非法值回落到考试模式（宁严不松）
  2) 教学模式：红色预警不需要先确认就能复核；新告警打 run_mode=teach 标记
  3) 考试模式：红色预警必须先确认才能复核（原规则）
  4) 切换写审计链（谁切的、从什么切到什么）；重复切换不重复写
  5) 每模式都有"改变了什么"的说明（界面确认弹窗用）
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="smoke_runmode_")
os.environ["EXAM_TERMINAL_HOME"] = TMP
sys.path.insert(0, HERE)

import access      # noqa: E402
import alerts      # noqa: E402
import config      # noqa: E402
import models      # noqa: E402
import review      # noqa: E402
import runmode     # noqa: E402
import storage     # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


print("临时数据目录：%s\n" % TMP)
store = storage.Store()
acc = access.AccountStore(store)
acc.ensure_seed()                     # 临时库第一次要有预置账户
session = acc.authenticate("owner", "EDY_POWERQ")
check("owner 登录", session is not None)

# ---- 1) 默认与非法值 ----
check("默认是考试模式", runmode.get(store) == config.RUN_MODE_EXAM,
      runmode.label(runmode.get(store)))
store.set_kv(runmode.KEY, "乱写的值")
check("非法值回落到考试模式（宁严不松）", runmode.is_exam(store))
store.set_kv(runmode.KEY, config.RUN_MODE_DEFAULT)

# ---- 2) 切换 + 留痕 ----
audit_before = len(store.audit_rows(limit=100000))
ok, msg = runmode.set(store, config.RUN_MODE_TEACH, operator="owner",
                      note="日常教学")
check("切到非考试（教学）模式", ok and runmode.is_teach(store), msg)
audit_after = len(store.audit_rows(limit=100000))
check("切换写审计链", audit_after == audit_before + 1,
      "审计 %d → %d" % (audit_before, audit_after))
rows = store.audit_rows(limit=3)
check("审计里写明动作与目标",
      any(r.get("action") == "切换运行模式" and "教学" in (r.get("detail") or "")
          for r in rows), (rows[0].get("detail") or "")[:60])
ok2, msg2 = runmode.set(store, config.RUN_MODE_TEACH, operator="owner")
check("重复切到同一模式不报错也不重复写",
      ok2 and len(store.audit_rows(limit=100000)) == audit_after, msg2)

# ---- 3) 教学模式：红色不必先确认就能复核 + 告警打标记 ----
hub = alerts.AlertHub(store)
red = models.Alert(code="PHONE_DETECTED", room_id="A-01", seat_no="01")
hub.ingest(red, dedup=False)
check("教学模式下新告警打 run_mode=teach 标记",
      (red.extra or {}).get("run_mode") == config.RUN_MODE_TEACH,
      str(red.extra))
rv = review.Reviewer(store)
ok, res = rv.review(red.alert_id, config.REVIEW_CONFIRMED, "教学演示复核",
                    session)
check("教学模式下红色预警可**直接复核**（不强制先确认）", ok, str(res)[:60])

# ---- 4) 考试模式：必须先确认 ----
ok3, _m = runmode.set(store, config.RUN_MODE_EXAM, operator="owner", note="正式考试")
check("切回考试模式", ok3)
red2 = models.Alert(code="MULTI_FACE", room_id="A-01", seat_no="02")
hub.ingest(red2, dedup=False)
check("考试模式下新告警**不打**教学标记",
      "run_mode" not in (red2.extra or {}), str(red2.extra))
ok, res = rv.review(red2.alert_id, config.REVIEW_CONFIRMED, "考试复核", session)
check("考试模式下红色预警**必须先确认**才能复核", (not ok) and "确认" in str(res),
      str(res)[:60])
hub.ack(red2.alert_id, "owner", reason="考试模式确认")
ok, res = rv.review(red2.alert_id, config.REVIEW_CONFIRMED, "确认后复核", session)
check("确认之后可以复核", ok, str(res)[:60])

# ---- 5) 说明文字 ----
check("两种模式都有【改变了什么】的说明",
      all(len(runmode.effects(m)) >= 3 for m in config.RUN_MODES))
check("模式摘要可读", bool(runmode.summary(store)))

print("\n" + "=" * 60)
print("运行模式冒烟：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
