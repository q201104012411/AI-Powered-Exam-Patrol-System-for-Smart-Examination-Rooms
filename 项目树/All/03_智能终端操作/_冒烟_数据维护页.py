# -*- coding: utf-8 -*-
"""03 数据维护页 GUI 冒烟（临时数据目录，真实建窗口 + 真实登录）

验证：
  1) owner / admin 能看到「数据维护」页签；patrol 看不到（权限模型生效）
  2) 数据维护页控件齐全（范围/原因/按钮/存量/留痕表）
  3) 清空走库内实现：**不删库文件**、终端账户不被删、审计链完整
"""
import os
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="gui_data_tab_")
os.environ["EXAM_TERMINAL_HOME"] = TMP
sys.path.insert(0, HERE)

import config        # noqa: E402
import storage       # noqa: E402
import access        # noqa: E402
import models        # noqa: E402
import terminal_gui  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("　— " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("　— " + detail) if detail else ""))


def login(app, user, pwd):
    """走真实登录入口，返回是否成功"""
    app.u_var.set(user)
    app.p_var.set(pwd)
    app._do_login()
    return app.session is not None


print("临时数据目录：%s\n" % TMP)

for user, pwd, role, expect_tab in (("owner", "owner", config.ROLE_OWNER, True),
                                    ("admin", "admin", config.ROLE_ADMIN, True),
                                    ("patrol1", "patrol1", config.ROLE_PATROL, False)):
    print("=== %s(%s) ===" % (user, role))
    root = tk.Tk()
    root.withdraw()
    try:
        app = terminal_gui.TerminalApp(root)
        check("登录成功", login(app, user, pwd), "角色=%s" % (app.session.role if app.session else "-"))
        check("数据维护页签可见 = %s" % expect_tab,
              bool(getattr(app, "can_maintain", False)) == expect_tab,
              "can(maintain)=%s" % (app.session.can("maintain") if app.session else "-"))
        if expect_tab:
            check("范围选择控件", hasattr(app, "purge_scope"))
            check("原因输入框", hasattr(app, "purge_reason"))
            check("存量显示", hasattr(app, "maintain_info"))
            check("留痕表格", hasattr(app, "purge_tree"))
            check("留痕表 6 列", len(app.purge_tree["columns"]) == 6)
            app.refresh_maintain()
            shown = app.maintain_info.get("1.0", "end")
            check("存量文本已填充", "远端记录" in shown,
                  (shown.strip().splitlines() or [""])[0])
            can_hard = app.session.role in config.HARD_PURGE_ROLES
            check("硬删资格 = %s（仅所有者）" % (role == config.ROLE_OWNER),
                  can_hard == (role == config.ROLE_OWNER))
    finally:
        root.destroy()

print("\n=== 清空不删库文件、不删账户 ===")
store = storage.Store()
store.save_alert(models.Alert(code="PHONE_DETECTED", title="清空前告警", device_id="CAM-A01-1"))
ok, msg, _ = store.purge_data("all", mode="hard", operator="owner", reason="GUI 冒烟")
check("清空执行成功", ok, msg)
check("数据库文件仍在（WinError 32 的根因已消除）", os.path.exists(str(config.DB_PATH)))
check("告警已清空", len(store.list_alerts(limit=100)) == 0)
acc = access.AccountStore(store)
check("终端账户未被删除", acc.authenticate("owner", "owner") is not None)
ok_chain, why, _ = store.verify_chain()
check("审计链完整", ok_chain, why)

print("\n" + "=" * 60)
print("数据维护页冒烟：%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
