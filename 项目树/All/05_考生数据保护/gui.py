# -*- coding: utf-8 -*-
r"""
考生数据保护 · 桌面管理界面（tkinter）

与 03 智能终端同一做法：**原生桌面窗口**，双击「启动服务.bat」即开，无需浏览器、无端口。

能力（与原网页后台完全对齐）：
  · 概览：加密算法 / 完整性算法（SM3 + SM2）/ 加密率 / 审计链 / 验签统计 / 角色权限
  · 考生台账（**默认脱敏**；查看明文须走 reveal 授权并留痕）
  · 上传登记（加密 + 签名；已存在即拒绝 —— 加密数据不可修改）
  · 删除（DELETE 留痕；法律保留中须走强制销毁并填见证人）
  · 完整性验签（一键核对是否被篡改）
  · 只读探针（现场演示"改不动"）
  · 篡改演示（在**临时记录**上演示改库必被检出，演示完清理，真实数据不动）
  · 审计链与验收指标（六项 KPI）
  · 数据分类分级目录
  · 处理活动登记表 / 验收报告导出

运行：python gui.py
账户与 CLI / 集成接口共用同一 SQLite（kv.accounts）与同一口令策略。
pythonw.exe（隐藏命令行窗口）启动时 sys.stdout 为 None，先兜住再 print。
"""
import os
import sqlite3
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


# ---- 隐藏窗口启动时 stdout/stderr 可能是 None：先兜住再 print ----
class _NullWriter(object):
    def write(self, *_a):
        pass

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()

import tkinter as tk                                     # noqa: E402
from tkinter import messagebox, ttk                      # noqa: E402

from dpp import (access, catalog, config, crypto, desensitize,   # noqa: E402
                 governance, integrity, lifecycle, models, report,
                 rights, secure_store, storage)

APP_TITLE = "考生数据保护 · 管理后台"
PORT_OR_NA = "桌面端"

# 配色（深色，与专题整体风格一致）
C_BG = "#0f1419"
C_PANEL = "#171d25"
C_PANEL2 = "#1e252f"
C_LINE = "#2a3341"
C_FG = "#e6edf3"
C_DIM = "#8b98a9"
C_ACC = "#3b82f6"
C_RED = "#ef4444"
C_GREEN = "#22c55e"
C_AMBER = "#f59e0b"

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_H = ("Microsoft YaHei UI", 11, "bold")
FONT_MONO = ("Consolas", 10)

KEYRING_ADVICE = (
    "密钥不可用，敏感字段无法加解密：\n%s\n\n"
    "最常见原因：整个项目目录是从**别的电脑或别的 Windows 账户**拷过来的，"
    "DPAPI 主密钥不在这台机器上，密钥永久解不开（不是程序故障）。\n\n"
    "处置：双击「修复密钥环.bat」，或在本目录执行\n"
    "    python cli.py keyring --reset --yes --with-demo\n"
    "（会先把 data\\ 整目录备份成 data_backup_<时间戳>\\，再重建密钥与演示数据）")


def advice(exc):
    """把异常翻译成"人话 + 处置办法"，界面上只显示这个，不甩 traceback。"""
    if isinstance(exc, secure_store.SecureStoreError):
        return KEYRING_ADVICE % exc
    if isinstance(exc, OSError) and "DPAPI" in str(exc):
        return KEYRING_ADVICE % exc                 # 兼容旧版抛的 OSError
    return "操作失败：%s: %s" % (type(exc).__name__, exc)


class App(object):
    """桌面管理界面：登录 -> 主界面（分页）。"""

    def __init__(self, root):
        self.root = root
        self.store = storage.Store()
        self.accounts = access.AccountStore(self.store)
        self.accounts.ensure_seed()
        self.life = lifecycle.Lifecycle(self.store, accounts=self.accounts)
        self.gov = governance.Governance(self.store)
        self.rights = rights.RightsService(self.store, lifecycle=self.life,
                                           accounts=self.accounts)
        self.reporter = report.Reporter(self.store, lifecycle=self.life,
                                        gov=self.gov, rights=self.rights)
        self.session = None
        self.rows = []                       # 当前台账行（供选中）

        root.title(APP_TITLE)
        root.configure(bg=C_BG)
        root.geometry("1180x760")
        root.minsize(980, 640)
        # Tkinter 回调里未捕获的异常统一走这里：给中文提示，不往控制台甩 traceback
        root.report_callback_exception = self._tk_error
        self._style()
        self._center()
        self._build_login()

    # ------------------------------------------------------------ 错误提示
    def _tk_error(self, exc_type, exc_value, tb):
        try:
            sys.stderr.write("".join(traceback.format_exception(exc_type, exc_value, tb)))
        except Exception:                                    # noqa: BLE001
            pass
        try:
            messagebox.showerror(APP_TITLE, advice(exc_value))
        except Exception:                                    # noqa: BLE001
            pass

    def _refresh_all(self):
        """刷新全部标签页；某一页失败不影响其余页，汇总成一条提示。"""
        problems = []
        for fn in (self._refresh_overview, self._refresh_ledger,
                   self._refresh_integrity, self._refresh_audit):
            try:
                fn()
            except Exception as exc:                         # noqa: BLE001
                problems.append(advice(exc))
                try:
                    sys.stderr.write("".join(traceback.format_exception(
                        type(exc), exc, exc.__traceback__)))
                except Exception:                            # noqa: BLE001
                    pass
        if problems:
            uniq = []
            for p in problems:
                if p not in uniq:
                    uniq.append(p)
            messagebox.showwarning(
                APP_TITLE, "部分数据加载失败（界面其余功能仍可用）：\n\n" + "\n\n".join(uniq))

    # ------------------------------------------------------------ 工具
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:                                # noqa: BLE001
            pass
        st.configure("TNotebook", background=C_BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=C_PANEL, foreground=C_DIM,
                     padding=(18, 9), font=FONT)
        st.map("TNotebook.Tab", background=[("selected", C_PANEL2)],
               foreground=[("selected", C_FG)])
        st.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL,
                     foreground=C_FG, rowheight=26, borderwidth=0, font=FONT)
        st.configure("Treeview.Heading", background=C_PANEL2, foreground=C_DIM,
                     font=FONT_B, relief="flat")
        st.map("Treeview", background=[("selected", C_ACC)],
               foreground=[("selected", "#ffffff")])
        st.configure("TFrame", background=C_BG)
        st.configure("Card.TFrame", background=C_PANEL)
        st.configure("TLabel", background=C_BG, foreground=C_FG, font=FONT)
        st.configure("Dim.TLabel", background=C_BG, foreground=C_DIM, font=FONT)
        st.configure("H.TLabel", background=C_BG, foreground=C_FG, font=FONT_H)
        st.configure("CardH.TLabel", background=C_PANEL, foreground=C_DIM,
                     font=FONT_B)

    def _center(self):
        self.root.update_idletasks()
        w, h = 1180, 760
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))

    def _clear(self):
        for w in self.root.winfo_children():
            w.destroy()

    def _card(self, parent, title=None):
        outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_LINE,
                         highlightthickness=1)
        outer.pack(fill="x", padx=12, pady=(0, 10))
        if title:
            tk.Label(outer, text=title, bg=C_PANEL, fg=C_DIM,
                     font=FONT_B).pack(anchor="w", padx=14, pady=(10, 4))
        body = tk.Frame(outer, bg=C_PANEL)
        body.pack(fill="x", padx=14, pady=(0, 12))
        return outer, body

    def _btn(self, parent, text, cmd, kind="acc", width=None):
        colors = {"acc": C_ACC, "ghost": C_PANEL2, "danger": C_RED}
        b = tk.Button(parent, text=text, command=cmd, bg=colors.get(kind, C_ACC),
                      fg="#ffffff" if kind != "ghost" else C_FG,
                      activebackground=colors.get(kind, C_ACC),
                      activeforeground="#ffffff" if kind != "ghost" else C_FG,
                      relief="flat", bd=0, font=FONT, padx=13, pady=6,
                      cursor="hand2",
                      highlightbackground=C_LINE if kind == "ghost" else colors[kind],
                      highlightthickness=1 if kind == "ghost" else 0)
        if width:
            b.configure(width=width)
        return b

    def _entry(self, parent, textvariable=None, width=24):
        return tk.Entry(parent, textvariable=textvariable, bg=C_PANEL2,
                        fg=C_FG, insertbackground=C_FG, relief="flat", bd=0,
                        font=FONT, width=width,
                        highlightbackground=C_LINE, highlightthickness=1)

    def _kv_grid(self, parent, items, cols=4):
        """items: list[(key, value)]；网格展示。"""
        g = tk.Frame(parent, bg=C_PANEL)
        g.pack(fill="x")
        for i, (k, v) in enumerate(items):
            r, c = divmod(i, cols)
            box = tk.Frame(g, bg=C_PANEL2)
            box.grid(row=r, column=c, sticky="nsew", padx=5, pady=5)
            tk.Label(box, text=str(k), bg=C_PANEL2, fg=C_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w",
                                                          padx=10, pady=(7, 0))
            tk.Label(box, text=str(v), bg=C_PANEL2, fg=C_FG, font=FONT,
                     wraplength=230, justify="left").pack(anchor="w", padx=10,
                                                          pady=(2, 7))
        for c in range(cols):
            g.columnconfigure(c, weight=1)

    def _toast(self, msg, kind="ok"):
        color = {"ok": C_GREEN, "err": C_RED, "info": C_ACC}.get(kind, C_ACC)
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.configure(bg=color)
        tk.Label(top, text=msg, bg=C_PANEL2, fg=C_FG, font=FONT,
                 wraplength=460, justify="left", padx=14, pady=10).pack(
            padx=2, pady=2, fill="both")
        top.update_idletasks()
        w = top.winfo_width()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        top.geometry("+%d+%d" % (sw - w - 30, sh - top.winfo_height() - 60))
        top.after(4200, top.destroy)

    # ============================================================ 登录
    def _build_login(self):
        self._clear()
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.place(relx=0.5, rely=0.42, anchor="center")

        tk.Label(wrap, text="🔐 考生数据保护", bg=C_BG, fg=C_FG,
                 font=("Microsoft YaHei UI", 20, "bold")).pack(pady=(0, 4))
        tk.Label(wrap, text="加密（SM4/SM3）+ 完整性签名（SM3/SM2）· 加密数据只读不可改",
                 bg=C_BG, fg=C_DIM, font=FONT).pack(pady=(0, 18))

        card = tk.Frame(wrap, bg=C_PANEL, highlightbackground=C_LINE,
                        highlightthickness=1)
        card.pack(ipadx=26, ipady=22)

        tk.Label(card, text="账户", bg=C_PANEL, fg=C_DIM, font=FONT).pack(
            anchor="w", pady=(0, 3))
        self.e_user = self._entry(card, width=26)
        self.e_user.pack(ipady=5)
        self.e_user.insert(0, "owner")

        tk.Label(card, text="口令", bg=C_PANEL, fg=C_DIM, font=FONT).pack(
            anchor="w", pady=(12, 3))
        self.e_pwd = self._entry(card, width=26)
        self.e_pwd.configure(show="●")
        self.e_pwd.pack(ipady=5)

        self._btn(card, "登 录", self._do_login).pack(fill="x", pady=(18, 0))
        tk.Label(card, text="通用口令均为账号名：owner/EDY_POWERQ ｜ security/security\n"
                            "dpo1/dpo1 ｜ auditor1/auditor1 ｜ oper1/oper1 ｜ inv01/inv01",
                 bg=C_PANEL, fg=C_DIM, font=("Microsoft YaHei UI", 9),
                 justify="center").pack(pady=(12, 0))
        self.root.bind("<Return>", lambda _e: self._do_login())

    def _do_login(self):
        user = self.e_user.get().strip()
        pwd = self.e_pwd.get()
        s = self.accounts.authenticate(user, pwd, place="桌面后台",
                                       device="DESKTOP-GUI")
        if s is None:
            messagebox.showerror(APP_TITLE, "账户或口令不正确")
            return
        s.confirm_risky()
        self.session = s
        self.store.log("登录", "桌面后台登录 %s（角色 %s）" % (s.username, s.role),
                       s.username)
        self.root.unbind("<Return>")
        try:
            self._build_main()
        except Exception as exc:                             # noqa: BLE001
            self.session = None
            try:
                sys.stderr.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
            except Exception:                                # noqa: BLE001
                pass
            self._build_login()          # 退回登录页，别把界面留在半截状态
            messagebox.showerror(APP_TITLE, "进入主界面失败。\n\n" + advice(exc))

    def _logout(self):
        if self.session:
            self.store.log("登出", "桌面后台登出", self.session.username)
        self.session = None
        self._build_login()

    # ============================================================ 主界面
    def _build_main(self):
        self._clear()
        s = self.session

        # 顶栏
        top = tk.Frame(self.root, bg=C_PANEL, height=54)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="🔐 考生数据保护 · 管理后台", bg=C_PANEL, fg=C_FG,
                 font=FONT_H).pack(side="left", padx=16)
        tk.Label(top, text="SM3 + SM2", bg=C_PANEL, fg=C_GREEN,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 6))
        tk.Label(top, text="只读不可改", bg=C_PANEL, fg=C_AMBER,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Label(top, text="%s（%s）" % (s.display_name,
                                        config.ROLE_TEXT.get(s.role, s.role)),
                 bg=C_PANEL, fg=C_DIM, font=FONT).pack(side="right", padx=(10, 4))
        self._btn(top, "退出", self._logout, kind="ghost").pack(side="right",
                                                               padx=12, pady=11)

        # 分页
        nb = ttk.Notebook(self.root)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_over = self._tab_overview(nb)
        self.tab_led = self._tab_ledger(nb)
        self.tab_reg = self._tab_register(nb)
        self.tab_int = self._tab_integrity(nb)
        self.tab_aud = self._tab_audit(nb)
        self.tab_cat = self._tab_catalog(nb)

        nb.add(self.tab_over, text="概览")
        nb.add(self.tab_led, text="考生台账")
        nb.add(self.tab_reg, text="上传登记")
        nb.add(self.tab_int, text="完整性验签")
        nb.add(self.tab_aud, text="审计与指标")
        nb.add(self.tab_cat, text="数据分级目录")

        self._refresh_all()

    # ---------------------------------------------------------- 概览
    def _tab_overview(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        tk.Label(f, text="系统概览", bg=C_BG, fg=C_FG, font=FONT_H).pack(
            anchor="w", padx=14, pady=(12, 0))
        tk.Label(f, text="加密数据只读：任何人不能修改，只能删除或新增；"
                         "一切绕过业务层的改库都会被 SM3+SM2 验签检出。",
                 bg=C_BG, fg=C_DIM, font=FONT, wraplength=1040,
                 justify="left").pack(anchor="w", padx=14, pady=(4, 8))
        _outer, body = self._card(f)
        self.over_body = body
        self._btn(f, "刷新", self._refresh_overview, kind="ghost").pack(
            anchor="w", padx=14)
        return f

    def _refresh_overview(self):
        for w in self.over_body.winfo_children():
            w.destroy()
        summ = self.store.summary()
        sa = self.life.storage_audit()
        chain_ok, chain_msg, n = self.store.verify_chain()
        va = self.store.verify_all_records()
        rate = sa["rate"]
        rate_txt = "样本不足" if rate is None else ("%.2f%%" % (rate * 100))
        items = [
            ("加密算法", crypto.algorithm_text()),
            ("完整性算法", integrity.integrity_algorithm_text()),
            ("签名密钥指纹", integrity.key_fingerprint()),
            ("敏感数据加密率", rate_txt),
            ("已签名记录", "%s 条" % va["total"]),
            ("验签通过", "%s 条" % va["ok"]),
            ("被篡改", "%s 条" % va["bad_count"]),
            ("已删除（允许）", "%s 条" % va["deleted"]),
            ("考生数", summ.get("考生数", 0)),
            ("同意记录", summ.get("同意记录", 0)),
            ("销毁总数", summ.get("销毁总数", 0)),
            ("审计条数", summ.get("审计条数", 0)),
        ]
        self._kv_grid(self.over_body, items, cols=4)
        # 审计链状态行
        line = tk.Frame(self.over_body, bg=C_PANEL)
        line.pack(fill="x", pady=(8, 0))
        ok = bool(chain_ok)
        tk.Label(line, text="审计链：" + chain_msg, bg=C_PANEL,
                 fg=C_GREEN if ok else C_RED, font=FONT).pack(anchor="w")

    # ---------------------------------------------------------- 台账
    def _tab_ledger(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(bar, text="考生台账（默认脱敏）", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        tk.Label(bar, text="  考场过滤：", bg=C_BG, fg=C_DIM, font=FONT).pack(
            side="left", padx=(14, 4))
        self.v_room = tk.StringVar()
        tk.Entry(bar, textvariable=self.v_room, bg=C_PANEL2, fg=C_FG,
                 insertbackground=C_FG, relief="flat", bd=0, font=FONT,
                 width=12, highlightbackground=C_LINE,
                 highlightthickness=1).pack(side="left", ipady=4)
        self._btn(bar, "刷新", self._refresh_ledger, kind="ghost").pack(
            side="left", padx=8)
        self._btn(bar, "查看明文（授权留痕）", self._reveal_new_window,
                  kind="acc").pack(side="right")
        self._btn(bar, "删除所选", self._destroy_selected, kind="danger").pack(
            side="right", padx=8)

        cols = ("ref", "name", "id_card", "ticket", "seat", "room", "site",
                "retention", "hold", "consent")
        heads = ("编号", "姓名", "证件号", "准考证号", "座次", "考场", "考点",
                 "留限(天)", "法律保留", "同意")
        wrap = tk.Frame(f, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        for c, h in zip(cols, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=110 if c != "ref" else 130, anchor="w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._reveal_from_row())
        tk.Label(f, text="提示：双击一行 = 授权查看该考生明文（会留痕）。"
                         "「删除所选」为允许操作，但不可修改已有记录。",
                 bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 9)).pack(
            anchor="w", padx=14, pady=(0, 8))
        return f

    def _refresh_ledger(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.rows = []
        room = (self.v_room.get().strip() or None) if hasattr(self, "v_room") else None
        try:
            rows = access.visible_candidates(self.store, self.session,
                                             room_id=room, limit=500)
        except Exception as e:                                   # noqa: BLE001
            self._toast("读取台账失败：%s" % e, "err")
            return
        for c in rows:
            self.rows.append(c)
            self.tree.insert("", "end", values=(
                c.cand_ref,
                desensitize.mask_name(c.name),
                desensitize.mask_id_card(c.id_card_no),
                desensitize.mask_ticket_no(c.ticket_no),
                desensitize.mask_seat_no(c.seat_no),
                c.room_id or "-", c.site or "-",
                c.retention_days if c.retention_days else "-",
                "是" if c.legal_hold else "否",
                "已签" if c.consent_id else "-",
            ))

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.rows):
            return self.rows[idx]
        return None

    def _reveal_from_row(self):
        row = self._selected_row()
        if row is None:
            return
        self._reveal_dialog(row.cand_ref)

    def _reveal_new_window(self):
        row = self._selected_row()
        if row is None:
            messagebox.showinfo(APP_TITLE, "请先在表格里选中一名考生")
            return
        self._reveal_dialog(row.cand_ref)

    def _reveal_dialog(self, cand_ref):
        d = tk.Toplevel(self.root)
        d.title("授权查看明文 · %s" % cand_ref)
        d.configure(bg=C_BG)
        d.geometry("460x250")
        tk.Label(d, text="查看明文必须填写理由（留痕要求）", bg=C_BG, fg=C_FG,
                 font=FONT).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(d, text="只读铁律：这里只能「看」，不能改；查看动作会写入审计链。",
                 bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 9),
                 wraplength=420, justify="left").pack(anchor="w", padx=16)
        e = self._entry(d, width=44)
        e.pack(padx=16, pady=12, ipady=5)
        e.focus_set()
        out = tk.Text(d, height=5, bg=C_PANEL2, fg=C_FG, relief="flat",
                      font=FONT_MONO, insertbackground=C_FG)

        def do():
            reason = e.get().strip()
            if not reason:
                messagebox.showwarning(APP_TITLE, "请填写查看理由")
                return
            try:
                res = self.life.reveal_plain(self.session, cand_ref, reason,
                                             terminal="桌面后台")
            except Exception as ex:                              # noqa: BLE001
                messagebox.showerror(APP_TITLE, str(ex))
                return
            out.delete("1.0", "end")
            out.insert("end", "姓名：%s\n证件号：%s\n准考证号：%s\n水印：%s\n"
                       % (res["name"], res["id_card_no"], res["ticket_no"],
                          res["watermark"]))
            self._toast("已授权查看明文并留痕", "info")

        self._btn(d, "确认查看并留痕", do).pack(padx=16, anchor="w")
        out.pack(fill="both", expand=True, padx=16, pady=12)

    def _destroy_selected(self):
        row = self._selected_row()
        if row is None:
            messagebox.showinfo(APP_TITLE, "请先选中一名考生")
            return
        if not messagebox.askyesno(APP_TITLE,
                                   "确认删除考生 %s？\n"
                                   "删除是允许操作（不可恢复），会全程留痕。"
                                   % row.cand_ref):
            return
        reason = self._ask_text("删除理由", "请填写删除理由（留痕要求）：")
        if not reason:
            return
        witness = ""
        if row.legal_hold:
            force = messagebox.askyesno(APP_TITLE,
                                        "该记录处于法律保留中。是否走【强制销毁】？")
            if not force:
                return
            witness = self._ask_text("见证人", "强制销毁需双人确认，请填写见证人：")
            if not witness:
                return
        try:
            res = self.life.destroy_by_request(self.session, row.cand_ref, reason,
                                               witness=witness,
                                               force=bool(row.legal_hold))
        except Exception as e:                                   # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._toast("已删除 %s（销毁单 %s）" % (row.cand_ref, res["destroy_id"]))
        self._refresh_ledger()
        self._refresh_overview()
        self._refresh_audit()

    def _ask_text(self, title, prompt):
        d = tk.Toplevel(self.root)
        d.title(title)
        d.configure(bg=C_BG)
        d.geometry("420x170")
        tk.Label(d, text=prompt, bg=C_BG, fg=C_FG, font=FONT, wraplength=390,
                 justify="left").pack(anchor="w", padx=16, pady=(16, 6))
        e = self._entry(d, width=40)
        e.pack(padx=16, ipady=5)
        e.focus_set()
        box = {"v": None}

        def ok():
            box["v"] = e.get().strip() or None
            d.destroy()

        self._btn(d, "确定", ok).pack(padx=16, pady=12, anchor="w")
        d.transient(self.root)
        d.grab_set()
        self.root.wait_window(d)
        return box["v"]

    # ---------------------------------------------------------- 上传登记
    def _tab_register(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        tk.Label(f, text="上传登记（加密 + 签名）", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(f, text="登记即加密（SM4-CBC+SM3）并签名（SM3+SM2）；"
                         "已存在的记录一律拒绝修改 —— 只能删除或新增。",
                 bg=C_BG, fg=C_DIM, font=FONT).pack(anchor="w", padx=14,
                                                    pady=(4, 10))
        _outer, body = self._card(f, "考生信息")
        self._reg_vars = {}
        fields = [("name", "姓名 *"), ("id_card_no", "证件号"),
                  ("ticket_no", "准考证号"), ("seat_no", "座次"),
                  ("room_id", "考场"), ("site", "考点"),
                  ("retention_days", "留限期(天，默认30)")]
        grid = tk.Frame(body, bg=C_PANEL)
        grid.pack(fill="x")
        for i, (k, lab) in enumerate(fields):
            r, c = divmod(i, 3)
            cell = tk.Frame(grid, bg=C_PANEL)
            cell.grid(row=r, column=c, sticky="ew", padx=6, pady=6)
            tk.Label(cell, text=lab, bg=C_PANEL, fg=C_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            v = tk.StringVar()
            self._reg_vars[k] = v
            tk.Entry(cell, textvariable=v, bg=C_PANEL2, fg=C_FG,
                     insertbackground=C_FG, relief="flat", bd=0, font=FONT,
                     width=26, highlightbackground=C_LINE,
                     highlightthickness=1).pack(ipady=5, fill="x")
        for c in range(3):
            grid.columnconfigure(c, weight=1)
        self._btn(f, "加密登记", self._do_register).pack(anchor="w", padx=14)

        self.reg_out = tk.Text(f, height=8, bg=C_PANEL, fg=C_FG, relief="flat",
                               font=FONT_MONO, insertbackground=C_FG)
        self.reg_out.pack(fill="both", expand=True, padx=14, pady=12)
        return f

    def _do_register(self):
        name = self._reg_vars["name"].get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "姓名不能为空")
            return
        cand_ref = models.new_id("C")
        cs = models.Consent(cand_ref=cand_ref, name=name, scope="face",
                            method="考前告知书签署（电子）",
                            operator=self.session.username,
                            evidence_path="consent/%s.pdf"
                                          % (self._reg_vars["ticket_no"].get()
                                             or "T"))
        self.store.save_consent(cs)
        rd = self._reg_vars["retention_days"].get().strip()
        try:
            res = self.life.collect(
                self.session, name=name,
                id_card_no=self._reg_vars["id_card_no"].get().strip(),
                ticket_no=self._reg_vars["ticket_no"].get().strip(),
                seat_no=self._reg_vars["seat_no"].get().strip(),
                room_id=self._reg_vars["room_id"].get().strip(),
                site=self._reg_vars["site"].get().strip(),
                feature_vector="gui-%s" % cand_ref,
                consent_id=cs.consent_id, cand_ref=cand_ref,
                retention_days=int(rd) if rd.isdigit() else None)
        except Exception as e:                                   # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.reg_out.delete("1.0", "end")
        self.reg_out.insert("end",
                            "登记成功\n"
                            "  编号：%s\n"
                            "  加密：%s\n  签名：%s\n"
                            "  说明：已加密并签名；本记录此后不可修改，只能删除或新增。\n"
                            % (res.get("cand_ref", cand_ref),
                               crypto.algorithm_text(),
                               integrity.integrity_algorithm_text()))
        self._toast("已加密登记 %s" % res.get("cand_ref", cand_ref))
        self._refresh_ledger()
        self._refresh_overview()
        self._refresh_integrity()
        self._refresh_audit()

    # ---------------------------------------------------------- 完整性
    def _tab_integrity(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(bar, text="完整性验签（哈希 + 非对称签名）", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        self._btn(bar, "只读探针（试改必被拒）", self._run_probe,
                  kind="ghost").pack(side="right")
        self._btn(bar, "篡改演示（临时记录）", self._run_tamper_demo,
                  kind="danger").pack(side="right", padx=8)

        _outer, body = self._card(f)
        self.int_body = body
        tk.Label(f, text="签名台账（最近 200 条）", bg=C_BG, fg=C_DIM,
                 font=FONT_B).pack(anchor="w", padx=14, pady=(4, 4))
        wrap = tk.Frame(f, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("ref", "kind", "alg", "digest", "sig", "time")
        heads = ("记录", "类型", "算法", "摘要(SM3/SHA256)", "签名", "签名时间")
        self.int_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, h in zip(cols, heads):
            self.int_tree.heading(c, text=h)
            self.int_tree.column(c, width={"ref": 150, "kind": 80, "alg": 90,
                                          "digest": 260, "sig": 130,
                                          "time": 150}[c], anchor="w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.int_tree.yview)
        self.int_tree.configure(yscrollcommand=vs.set)
        self.int_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        return f

    def _refresh_integrity(self):
        for w in self.int_body.winfo_children():
            w.destroy()
        va = self.store.verify_all_records()
        bad = va["bad_count"]
        items = [
            ("算法", integrity.integrity_algorithm_text()),
            ("哈希", integrity.hash_algorithm()),
            ("签名", integrity.signature_algorithm()),
            ("密钥指纹", integrity.key_fingerprint()),
            ("签名记录", va["total"]),
            ("验签通过", va["ok"]),
            ("被篡改", bad),
            ("已删除(允许)", va["deleted"]),
        ]
        self._kv_grid(self.int_body, items, cols=4)
        line = tk.Frame(self.int_body, bg=C_PANEL)
        line.pack(fill="x", pady=(8, 0))
        if bad == 0:
            tk.Label(line, text="✔ 全部记录验签通过，未被篡改",
                     bg=C_PANEL, fg=C_GREEN, font=FONT_B).pack(anchor="w")
        else:
            tk.Label(line, text="✘ 检出 %d 条被篡改：%s" % (bad, "、".join(va["bad"])),
                     bg=C_PANEL, fg=C_RED, font=FONT_B, wraplength=980,
                     justify="left").pack(anchor="w")
        # 台账
        for i in self.int_tree.get_children():
            self.int_tree.delete(i)
        for sg in self.store.list_signatures(limit=200):
            ref = sg.get("record_ref", "")
            self.int_tree.insert("", "end", values=(
                ref, sg.get("kind", ""),
                "%s+%s" % (sg.get("hash_alg", ""), sg.get("sig_alg", "")),
                (sg.get("digest", "") or "")[:48] + "…",
                (sg.get("signature", "") or "")[:20] + "…",
                sg.get("signed_at", "")))

    def _run_probe(self):
        sigs = self.store.list_signatures(kind="candidate", limit=1)
        if not sigs:
            self._toast("暂无已签名记录，先在上传登记里登记一条", "info")
            return
        ref = sigs[0]["record_ref"].split(":", 1)[1]
        try:
            self.store.update_candidate_meta(ref, name="篡改测试")
        except storage.ReadOnlyViolation as e:
            self.store.log("只读演示", "桌面后台演示拒绝修改 %s" % ref,
                           self.session.username)
            self._toast("✔ 被业务层拒绝：%s" % str(e)[:80], "ok")
            return
        except Exception as e:                                   # noqa: BLE001
            self._toast("✔ 被数据库触发器拦截：%s" % str(e)[:80], "ok")
            return
        self._toast("✘ 未能拦住修改（缺陷）", "err")

    def _run_tamper_demo(self):
        if not messagebox.askyesno(APP_TITLE,
                                   "将在一条【临时记录】上演示「改库必被检出」，\n"
                                   "演示完自动清理，真实数据不受影响。继续？"):
            return
        tmp_ref = "DEMOTAMPER" + crypto.secure_bytes(4).hex()
        cs = models.Consent(cand_ref=tmp_ref, name="篡改演示临时记录",
                            scope="face", method="演示用",
                            operator=self.session.username,
                            evidence_path="consent/demo.pdf")
        self.store.save_consent(cs)
        self.life.collect(self.session, name="篡改演示临时记录",
                          id_card_no="000000000000000000", ticket_no="DEMO",
                          seat_no="00", room_id="DEMO", site="演示",
                          feature_vector="demo-%s" % tmp_ref,
                          consent_id=cs.consent_id, cand_ref=tmp_ref)
        before_ok, before_msg, _ = self.store.verify_record(
            "candidates:%s" % tmp_ref)
        conn = sqlite3.connect(str(config.DB_PATH))
        detected, after_msg = False, ""
        try:
            conn.execute("DROP TRIGGER IF EXISTS trg_cand_no_update")
            conn.execute("UPDATE candidates SET id_card_no=? WHERE cand_ref=?",
                         ("enc:篡改", tmp_ref))
            conn.commit()
            conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_cand_no_update
              BEFORE UPDATE ON candidates
              WHEN (OLD.name IS NOT NEW.name OR OLD.id_card_no IS NOT NEW.id_card_no
                 OR OLD.ticket_no IS NOT NEW.ticket_no OR OLD.seat_no IS NOT NEW.seat_no
                 OR OLD.room_id IS NOT NEW.room_id OR OLD.site IS NOT NEW.site
                 OR OLD.face_template IS NOT NEW.face_template
                 OR OLD.face_image_path IS NOT NEW.face_image_path
                 OR OLD.captured_epoch IS NOT NEW.captured_epoch
                 OR OLD.retention_days IS NOT NEW.retention_days
                 OR OLD.created_at IS NOT NEW.created_at)
              BEGIN SELECT RAISE(ABORT, '只读不可改：考生加密数据禁止修改（只能 DELETE 或 INSERT）'); END""")
            conn.commit()
            ok, after_msg, _ = self.store.verify_record("candidates:%s" % tmp_ref)
            detected = not ok
        finally:
            conn.close()
        try:
            self.life.destroy_by_request(self.session, tmp_ref,
                                         "篡改演示后清理临时记录")
        except Exception:                                        # noqa: BLE001
            pass
        self.store.log("篡改演示", "桌面后台在临时记录上演示改库：%s"
                       % ("已检出篡改" if detected else "未检出"),
                       self.session.username)
        if detected:
            self._toast("✔ 改库被检出：%s（临时记录已清理）" % after_msg, "ok")
        else:
            self._toast("✘ 未检出（异常）", "err")
        self._refresh_integrity()
        self._refresh_overview()

    # ---------------------------------------------------------- 审计 / 指标
    def _tab_audit(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        bar = tk.Frame(f, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(bar, text="审计链与验收指标", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(side="left")
        self._btn(bar, "导出验收报告", self._export_report,
                  kind="ghost").pack(side="right")
        self._btn(bar, "刷新", self._refresh_audit, kind="ghost").pack(
            side="right", padx=8)

        _outer1, self.aud_body = self._card(f, "审计链状态")
        _outer2, self.kpi_body = self._card(f, "验收指标（六项）")

        tk.Label(f, text="最近审计记录", bg=C_BG, fg=C_DIM,
                 font=FONT_B).pack(anchor="w", padx=14, pady=(4, 4))
        wrap = tk.Frame(f, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("ts", "action", "detail", "operator", "hash")
        heads = ("时间", "动作", "明细", "操作人", "哈希链")
        self.aud_tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, h in zip(cols, heads):
            self.aud_tree.heading(c, text=h)
            self.aud_tree.column(c, width={"ts": 150, "action": 90,
                                          "detail": 420, "operator": 100,
                                          "hash": 110}[c], anchor="w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.aud_tree.yview)
        self.aud_tree.configure(yscrollcommand=vs.set)
        self.aud_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        return f

    def _refresh_audit(self):
        for w in self.aud_body.winfo_children():
            w.destroy()
        for w in self.kpi_body.winfo_children():
            w.destroy()
        ok, msg, n = self.store.verify_chain()
        acts = self.store.audit_actions()
        self._kv_grid(self.aud_body, [
            ("审计链状态", msg),
            ("审计条数", n),
            ("覆盖动作", "、".join(sorted(acts.keys())) or "-"),
        ], cols=3)

        acc = self.gov.acceptance(lifecycle=self.life, rights=self.rights)
        passed = acc.get("passed", 0)
        total = acc.get("total", 0)
        head = tk.Frame(self.kpi_body, bg=C_PANEL)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text="通过 %d/%d 项" % (passed, total), bg=C_PANEL,
                 fg=C_GREEN if passed == total else C_AMBER,
                 font=FONT_B).pack(anchor="w")
        for item in acc.get("items", []):
            row = tk.Frame(self.kpi_body, bg=C_PANEL)
            row.pack(fill="x", pady=1)
            okk = item.get("passed")
            tk.Label(row, text="✔" if okk else "✘", bg=C_PANEL,
                     fg=C_GREEN if okk else C_RED, font=FONT_B).pack(side="left")
            tk.Label(row, text=item.get("name", ""), bg=C_PANEL, fg=C_FG,
                     font=FONT, width=24, anchor="w").pack(side="left")
            actual = item.get("actual", "")
            tk.Label(row, text=str(actual)[:120], bg=C_PANEL, fg=C_DIM,
                     font=FONT, anchor="w").pack(side="left")

        for i in self.aud_tree.get_children():
            self.aud_tree.delete(i)
        for r in self.store.audit_rows(limit=200):
            self.aud_tree.insert("", "end", values=(
                r.get("ts", ""), r.get("action", ""),
                str(r.get("detail", ""))[:180], r.get("operator", ""),
                str(r.get("hash", ""))[:16]))

    def _export_report(self):
        try:
            res = self.reporter.export_all()
        except Exception as e:                                   # noqa: BLE001
            messagebox.showerror(APP_TITLE, "导出失败：%s" % e)
            return
        self._toast("已导出验收报告与台账：%s" % res)
        messagebox.showinfo(APP_TITLE, "已导出：\n%s" % res)

    # ---------------------------------------------------------- 分级目录
    def _tab_catalog(self, nb):
        f = tk.Frame(nb, bg=C_BG)
        tk.Label(f, text="数据分类分级目录（唯一口径）", bg=C_BG, fg=C_FG,
                 font=FONT_H).pack(anchor="w", padx=14, pady=(12, 6))
        wrap = tk.Frame(f, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("key", "name", "level", "location", "retention", "irr", "auto")
        heads = ("键", "类别", "级别", "存储位置", "留限期", "不可逆", "自动销毁")
        tv = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, h in zip(cols, heads):
            tv.heading(c, text=h)
            tv.column(c, width={"key": 130, "name": 150, "level": 70,
                                "location": 220, "retention": 130,
                                "irr": 70, "auto": 80}[c], anchor="w")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        for c in catalog.CATEGORIES:
            tv.insert("", "end", values=(
                c.key, c.name, c.level, c.location, c.retention_text,
                "是" if c.irreversible else "否",
                "是" if c.auto_destroy else "否"))
        # 加密字段清单
        tk.Label(f, text="必须加密的字段：", bg=C_BG, fg=C_DIM,
                 font=FONT_B).pack(anchor="w", padx=14, pady=(4, 2))
        txt = tk.Text(f, height=4, bg=C_PANEL, fg=C_FG, relief="flat",
                      font=FONT_MONO, insertbackground=C_FG)
        txt.pack(fill="x", padx=14, pady=(0, 12))
        for c in catalog.CATEGORIES:
            txt.insert("end", "%-16s %s\n" % (c.key,
                                              "、".join(c.required_encrypted_fields)
                                              or "（无）"))
        txt.configure(state="disabled")
        return f


def main():
    root = tk.Tk()
    try:
        App(root)
    except Exception as exc:                                     # noqa: BLE001
        err = traceback.format_exc()
        try:
            messagebox.showerror(APP_TITLE, "启动失败。\n\n" + advice(exc)
                                 + "\n\n（诊断信息）\n" + err[-600:])
        except Exception:                                        # noqa: BLE001
            print(err)
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
