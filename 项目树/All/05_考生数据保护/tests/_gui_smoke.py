# -*- coding: utf-8 -*-
"""桌面界面（gui.py）无头冒烟测试。

在隐藏的 Tk 根窗口上真实构造 App、切遍六个标签页、调用界面所依赖的
服务层动作，确保「桌面版与网页版功能对齐」不是空话。

用法：python tests/_gui_smoke.py
退出码 0 表示通过。
"""
import os
import sys
import tempfile
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="dpp_gui_")
os.environ["EXAM_DPP_HOME"] = _TMP

import tkinter as tk  # noqa: E402

import gui  # noqa: E402
from dpp import config, crypto, integrity, models, storage  # noqa: E402

PASS = []
FAIL = []


def check(name, fn):
    try:
        r = fn()
        PASS.append((name, r))
        print("  [OK] %s -> %s" % (name, r))
    except Exception as e:                                       # noqa: BLE001
        FAIL.append((name, e))
        print("  [FAIL] %s -> %s" % (name, e))
        traceback.print_exc()


def main():
    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)

    # 0) 登录页控件就位
    def t_login_widgets():
        assert app.e_user.get() == "owner", app.e_user.get()
        return "登录页已构建"

    check("登录页控件", t_login_widgets)

    # 1) 登录（与网页版相同的 authenticate + confirm_risky）
    def t_login():
        app.e_user.delete(0, "end")
        app.e_user.insert(0, "owner")
        app.e_pwd.delete(0, "end")
        app.e_pwd.insert(0, dict((r[0], r[1])
                                 for r in config.DEFAULT_ACCOUNTS)["owner"])
        app._do_login()
        assert app.session is not None, "登录失败：session 为空"
        assert app.session.username == "owner", app.session.username
        return "session=%s role=%s" % (app.session.username, app.session.role)

    check("登录页 -> owner 登录成功", t_login)

    # 2) 六个标签页
    def t_build_tabs():
        names = [app.nb.tab(t, "text") for t in app.nb.tabs()]
        assert len(names) == 6, "标签页数量应为 6，实际 %d：%s" % (len(names), names)
        return " / ".join(names)

    check("主界面 -> 六个标签页全部构建", t_build_tabs)

    # 3) 概览
    def t_overview():
        app._refresh_overview()
        root.update_idletasks()
        n = len(app.over_body.winfo_children())
        assert n > 0
        return "概览控件数=%d" % n

    check("概览页刷新", t_overview)

    # 4) 台账（空表也要不炸）
    def t_ledger():
        app._refresh_ledger()
        root.update_idletasks()
        return "台账行数=%d" % len(app.tree.get_children())

    check("考生台账刷新", t_ledger)

    # 5) 上传登记（走 collect 全链路）
    def t_register():
        app._reg_vars["name"].set("桌面冒烟考生")
        app._reg_vars["id_card_no"].set("110101200001011234")
        app._reg_vars["ticket_no"].set("T-GUI-01")
        app._reg_vars["seat_no"].set("07")
        app._reg_vars["room_id"].set("R-101")
        app._reg_vars["site"].set("冒烟考点")
        app._reg_vars["retention_days"].set("")
        app._do_register()
        root.update_idletasks()
        total = app.store.summary().get("考生数", 0)
        assert total >= 1, "登记后考生数为 %s" % total
        assert "登记成功" in app.reg_out.get("1.0", "end"), "登记输出无成功提示"
        return "登记成功，考生总数=%s" % total

    check("上传登记（哈希+非对称签名）", t_register)

    # 6) 台账此时应有 1 行
    def t_ledger2():
        app._refresh_ledger()
        root.update_idletasks()
        n = len(app.tree.get_children())
        assert n >= 1, "登记后台账仍为空"
        return "台账行数=%d" % n

    check("台账出现新登记考生", t_ledger2)

    # 7) 完整性验签
    def t_integrity():
        app._refresh_integrity()
        root.update_idletasks()
        assert len(app.int_tree.get_children()) >= 1, "签名台账为空"
        return "签名台账行数=%d" % len(app.int_tree.get_children())

    check("完整性验签页刷新", t_integrity)

    # 8) 只读探针（等价 /api/readonly_probe）
    def t_probe():
        app._run_probe()
        root.update_idletasks()
        return "只读探针执行完成（结果走侧栏提示）"

    check("只读探针", t_probe)

    # 9) 篡改/验签链路（临时记录）
    def t_tamper():
        tmp_ref = "GUI_DEMO_" + crypto.secure_bytes(4).hex()
        cs = models.Consent(cand_ref=tmp_ref, name="界面冒烟演示",
                            scope="face", method="演示", operator="owner",
                            evidence_path="consent/demo.pdf")
        app.store.save_consent(cs)
        app.life.collect(app.session, name="界面冒烟演示", ticket_no="DEMO",
                         seat_no="00", room_id="DEMO", site="演示",
                         feature_vector="demo-%s" % tmp_ref,
                         consent_id=cs.consent_id, cand_ref=tmp_ref)
        # 卸触发器 + 直接改库
        import sqlite3
        conn = sqlite3.connect(str(config.DB_PATH))
        conn.execute("DROP TRIGGER IF EXISTS trg_cand_no_update")
        conn.execute("UPDATE candidates SET id_card_no=? WHERE cand_ref=?",
                     ("enc:tampered", tmp_ref))
        conn.commit()
        conn.close()
        va = app.store.verify_all_records()
        assert va["bad_count"] >= 1, "篡改后未被检出：%s" % va
        return "篡改后验签 bad_count=%d（已检出）" % va["bad_count"]

    check("篡改必被检出（临时记录）", t_tamper)

    # 10) 审计与指标
    def t_audit():
        app._refresh_audit()
        root.update_idletasks()
        n = len(app.aud_tree.get_children())
        assert n >= 1, "审计表为空"
        return "审计行数=%d，KPI 区控件=%d" % (
            n, len(app.kpi_body.winfo_children()))

    check("审计与指标页刷新", t_audit)

    # 11) 数据分级目录（等价 /api/catalog）
    def t_catalog():
        app._tab_catalog(app.nb)
        root.update_idletasks()
        return "分级目录页构建完成"

    check("数据分级目录页", t_catalog)

    # 12) 授权查看明文（等价 /api/reveal）
    def t_reveal():
        refs = [s["record_ref"].split(":", 1)[1]
                for s in app.store.list_signatures(kind="candidate", limit=5)]
        assert refs, "没有可查看的考生记录"
        r = app.life.reveal_plain(app.session, refs[0], "界面冒烟查看明文")
        assert r.get("name"), r
        return "姓名=%s 水印=%s" % (r["name"], r.get("watermark", "")[:18])

    check("授权查看明文（留痕）", t_reveal)

    # 13) 导出报告（等价 /api/report）
    def t_report():
        res = app.reporter.export_all()
        assert isinstance(res, dict) and res, res
        for k, p in res.items():
            assert os.path.exists(str(p)), "%s 不存在：%s" % (k, p)
        return "已导出 %d 份：%s" % (len(res), " / ".join(sorted(res.keys())))

    check("导出验收报告", t_report)

    # 14) 退出登录
    def t_logout():
        app._logout()
        assert app.session is None
        assert hasattr(app, "e_user")
        return "已返回登录页"

    check("退出登录", t_logout)

    # 15) 算法如实上报（不谎报）
    def t_algo():
        return "%s / %s" % (crypto.algorithm_text(),
                            integrity.integrity_algorithm_text())

    check("算法如实上报", t_algo)

    try:
        root.destroy()
    except Exception:
        pass

    print("\n" + "=" * 56)
    print("桌面界面冒烟：通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for n, e in FAIL:
            print("  - %s : %s" % (n, e))
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
