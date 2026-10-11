# -*- coding: utf-8 -*-
"""
智能终端操作端 · 桌面界面（tkinter）

按《智能终端.md》落地：
  · 告警分级展示：红色置顶 + 弹窗 + 强制确认；橙色普通提示
  · 复核留痕：结论 / 理由 / 见证人 / 附件
  · 断网可用：状态栏显示在线状态与待补传条数，可手动补传
  · 权限最小化：登录后按角色过滤（监考终端只看本考场），默认脱敏
  · 手持核验：调用项目一人脸核验（子进程桥接）

运行：python terminal_gui.py
"""
import os
import shutil
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import access
import alerts as alerts_mod
import bridge
import clock as clock_mod
import config
import fleet as fleet_mod
import models
import offline as offline_mod
import review as review_mod
import storage

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
MONO = ("Consolas", 10)


class TerminalApp(object):
    def __init__(self, root):
        self.root = root
        self.root.title("智能终端操作端 · 智能考场 AI 巡考系统")
        self.root.geometry("620x460")
        self.store = storage.Store()
        self.accounts = access.AccountStore(self.store)
        self.accounts.ensure_seed()
        self.hub = alerts_mod.AlertHub(self.store, offline=None)
        self.outbox = offline_mod.Outbox(self.store)
        self.hub.offline = self.outbox
        self.reviewer = review_mod.Reviewer(self.store, hub=self.hub)
        self.fleet = fleet_mod.FleetManager(self.store)
        self.clock = clock_mod.StandardClock(self.store, hub=self.hub)
        self.face = bridge.FaceBridge()
        self.session = None
        self._build_login()

    # ================= 登录（建在主窗口内，避免 Toplevel 不显示） =================
    def _build_login(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.geometry("620x460")
        box = ttk.Frame(self.root, padding=28)
        box.pack(expand=True)
        ttk.Label(box, text="智能终端操作端", font=("Microsoft YaHei", 18, "bold")
                  ).pack(pady=(6, 2))
        ttk.Label(box, text="告警处置 · 复核留痕 · 断网可用 · 权限最小化",
                  foreground="#666").pack(pady=(0, 18))
        ttk.Label(box, text="终端账号：").pack(anchor="w")
        self.u_var = tk.StringVar(value="patrol1")
        ttk.Entry(box, textvariable=self.u_var, width=30).pack(pady=(0, 8))
        ttk.Label(box, text="口令：").pack(anchor="w")
        self.p_var = tk.StringVar()
        e = ttk.Entry(box, textvariable=self.p_var, width=30, show="●")
        e.pack(pady=(0, 12))
        e.bind("<Return>", lambda _e: self._do_login())
        ttk.Button(box, text="登  录", command=self._do_login).pack(fill="x")
        self.login_msg = ttk.Label(box, text="", foreground="#c00")
        self.login_msg.pack(pady=(8, 0))
        ttk.Label(box, text="预置账户（口令请向系统所有者索取）\n"
                            "Owner（所有者·最高权限）\n"
                            "admin（管理员·日常全权）\n"
                            "patrol1（巡考员·全部考场）\n"
                            "inv01（监考员·仅 A-01）\n"
                            "check1（核验员）",
                  foreground="#888", justify="left").pack(pady=(12, 0))
        e.focus_set()

    def _do_login(self):
        s = self.accounts.authenticate(self.u_var.get().strip(), self.p_var.get())
        if s is None:
            self.login_msg.config(text="账号或口令错误")
            return
        self.session = s
        self._build_main()

    # ================= 主界面 =================
    def _build_main(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.geometry("1180x820")
        s = self.session

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="智能终端操作端", font=("Microsoft YaHei", 14, "bold")).pack(side="left")
        ttk.Label(top, text="  " + s.describe(), foreground="#1f56b8").pack(side="left")
        ttk.Button(top, text="🔄 切换账号", command=self._build_login).pack(side="right")
        ttk.Button(top, text="退出", command=self.root.destroy).pack(side="right", padx=6)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tab_alerts = ttk.Frame(nb, padding=8)
        self.tab_review = ttk.Frame(nb, padding=8)
        self.tab_verify = ttk.Frame(nb, padding=8)
        self.tab_ops = ttk.Frame(nb, padding=8)
        nb.add(self.tab_alerts, text="  告警处置  ")
        nb.add(self.tab_review, text="  复核留痕  ")
        nb.add(self.tab_verify, text="  手持核验  ")
        nb.add(self.tab_ops, text="  运行状态与管控  ")
        self._build_alerts_tab()
        self._build_review_tab()
        self._build_verify_tab()
        self._build_ops_tab()

        self.status = ttk.Label(self.root, text="就绪", foreground="#666")
        self.status.pack(fill="x", padx=12, pady=(0, 6))
        self.refresh_all()
        self._tick()

    # ---------- 告警处置 ----------
    def _build_alerts_tab(self):
        f = self.tab_alerts
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_alerts).pack(side="left")
        ttk.Button(bar, text="✓ 确认选中", command=self._ack).pack(side="left", padx=4)
        ttk.Button(bar, text="📣 一键上报", command=self._report).pack(side="left", padx=4)
        if self.session.can("dispatch"):
            ttk.Button(bar, text="🧪 生成演示告警",
                       command=self._demo).pack(side="left", padx=(16, 4))
        ttk.Button(bar, text="☁ 立即补传", command=self._flush).pack(side="right")
        self.alerts_hint = ttk.Label(bar, text="", foreground="#c07000")
        self.alerts_hint.pack(side="right", padx=10)

        cols = ("level", "time", "code", "room", "seat", "candidate", "status", "review")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, t, w in (("level", "级别", 80), ("time", "时间", 140), ("code", "告警码", 130),
                        ("room", "考场", 70), ("seat", "座位", 60),
                        ("candidate", "考生(默认脱敏)", 150), ("status", "状态", 80),
                        ("review", "复核结论", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("red", background="#ffe3e3", foreground="#a00000")
        self.tree.tag_configure("orange", background="#fff6e0", foreground="#8a5a00")
        self.tree.tag_configure("overdue", background="#ffd6d6")

        self.detail = tk.Text(f, height=5, wrap="word", font=MONO)
        self.detail.pack(fill="x", pady=(6, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

    # ---------- 复核留痕 ----------
    def _build_review_tab(self):
        f = self.tab_review
        ttk.Label(f, text="复核留痕（结论 / 理由 / 见证人 / 附件）",
                  font=FONT_B).pack(anchor="w")
        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=6)
        ttk.Label(grid, text="告警 ID：").grid(row=0, column=0, sticky="e", pady=3)
        self.rv_alert = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_alert, width=26).grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="结论：").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.rv_conc = tk.StringVar(value=config.REVIEW_CONFIRMED)
        ttk.Combobox(grid, textvariable=self.rv_conc, state="readonly", width=12,
                     values=list(config.REVIEW_CONCLUSIONS.keys())).grid(row=0, column=3, sticky="w")
        ttk.Label(grid, text="见证人：").grid(row=1, column=0, sticky="e", pady=3)
        self.rv_wit = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_wit, width=26).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="附件：").grid(row=1, column=2, sticky="e", padx=(12, 0))
        self.rv_att = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_att, width=22).grid(row=1, column=3, sticky="w")
        ttk.Button(grid, text="选择…", command=self._pick_attach).grid(row=1, column=4, padx=4)
        ttk.Label(f, text="复核理由（必填）：").pack(anchor="w", pady=(6, 0))
        self.rv_reason = tk.Text(f, height=4, wrap="word", font=FONT)
        self.rv_reason.pack(fill="x")
        rbar = ttk.Frame(f)
        rbar.pack(fill="x", pady=6)
        ttk.Button(rbar, text="提交复核", command=self._submit_review).pack(side="left")
        ttk.Button(rbar, text="误报统计（算法迭代闭环）",
                   command=self._fp_stats).pack(side="left", padx=8)
        ttk.Label(f, text="复核记录", font=FONT_B).pack(anchor="w", pady=(8, 2))
        self.rv_list = tk.Text(f, height=12, wrap="word", font=MONO)
        self.rv_list.pack(fill="both", expand=True)
        self.rv_list.tag_config("fp", foreground="#c07000")

    # ---------- 手持核验 ----------
    def _build_verify_tab(self):
        f = self.tab_verify
        ttk.Label(f, text="手持核验终端 · 调用项目一「人脸识别与身份核对」",
                  font=FONT_B).pack(anchor="w")
        ok, why = self.face.available()
        ttk.Label(f, text="桥接状态：" + ("✅ 可用" if ok else "⚠ " + why),
                  foreground=("#0a7d28" if ok else "#c07000"), wraplength=900,
                  justify="left").pack(anchor="w", pady=(4, 8))
        g = ttk.Frame(f)
        g.pack(fill="x")
        ttk.Label(g, text="模式：").grid(row=0, column=0, sticky="e")
        self.v_mode = tk.StringVar(value="1:1")
        ttk.Combobox(g, textvariable=self.v_mode, state="readonly", width=8,
                     values=["1:1", "1:N"]).grid(row=0, column=1, sticky="w")
        ttk.Label(g, text="考生编号：").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.v_cand = tk.StringVar()
        ttk.Entry(g, textvariable=self.v_cand, width=16).grid(row=0, column=3, sticky="w")
        ttk.Label(g, text="图片：").grid(row=1, column=0, sticky="e", pady=(6, 0))
        self.v_img = tk.StringVar()
        ttk.Entry(g, textvariable=self.v_img, width=60).grid(row=1, column=1, columnspan=3,
                                                             sticky="w", pady=(6, 0))
        ttk.Button(g, text="选择…", command=self._pick_image).grid(row=1, column=4, padx=4,
                                                                   pady=(6, 0))
        ttk.Button(f, text="开始核验", command=self._do_verify).pack(anchor="w", pady=8)
        self.v_out = tk.Text(f, height=16, wrap="word", font=MONO)
        self.v_out.pack(fill="both", expand=True)

    # ---------- 运行状态与管控 ----------
    def _build_ops_tab(self):
        f = self.tab_ops
        cols = ttk.Frame(f)
        cols.pack(fill="both", expand=True)
        left = ttk.LabelFrame(cols, text="在线状态 / 断网补传", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.LabelFrame(cols, text="统一管控 / 稳定性 / 时钟", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)

        self.ops_left = tk.Text(left, height=18, wrap="word", font=MONO)
        self.ops_left.pack(fill="both", expand=True)
        self.ops_right = tk.Text(right, height=18, wrap="word", font=MONO)
        self.ops_right.pack(fill="both", expand=True)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="刷新状态", command=self.refresh_ops).pack(side="left")
        ttk.Button(bar, text="写一次稳定性心跳", command=self._soak).pack(side="left", padx=6)
        ttk.Button(bar, text="校验审计链", command=self._chain).pack(side="left", padx=6)
        # 终端账户管理：只有具备 manage 动作的角色（所有者）才看得到
        if self.session.can("manage"):
            ttk.Button(bar, text="👥 终端账户", command=self._show_accounts
                       ).pack(side="left", padx=6)
            ttk.Button(bar, text="🗑 清空本地数据", command=self._wipe_all
                       ).pack(side="left", padx=6)
            self.hide_cmd = tk.BooleanVar(value=os.path.exists(str(config.HIDE_CMD_FLAG)))
            ttk.Checkbutton(bar, text="隐藏命令行窗口（下次启动生效）",
                            variable=self.hide_cmd,
                            command=self._toggle_hide_cmd).pack(side="left", padx=6)

    def _toggle_hide_cmd(self):
        """所有者开关：是否隐藏启动器命令行窗口（写标记文件，由 .bat 读取）"""
        on = self.hide_cmd.get()
        flag = str(config.HIDE_CMD_FLAG)
        try:
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            if on:
                with open(flag, "w", encoding="utf-8") as f:
                    f.write("1\n")
            elif os.path.exists(flag):
                os.remove(flag)
        except OSError as e:
            messagebox.showerror("设置失败", str(e))
            return
        self.store.log("隐藏命令行窗口", "开启" if on else "关闭", self.session.username)
        messagebox.showinfo("已设置", "下次通过「启动终端界面.bat」启动时%s命令行窗口。"
                            % ("隐藏" if on else "显示（便于查看报错）"))

    # ---------- 终端账户管理（所有者） ----------
    def _show_accounts(self):
        try:
            self.session.require("manage")
        except access.AccessError as e:
            messagebox.showerror("权限不足", str(e))
            return
        win = tk.Toplevel(self.root)
        win.title("终端账户管理")
        win.geometry("640x420")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="终端账户（口令加盐存储，界面不显示明文）",
                  font=FONT_B).pack(anchor="w", padx=12, pady=(10, 4))
        cols = ("user", "role", "name", "rooms")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=10)
        for c, t, w in (("user", "用户名", 130), ("role", "角色", 100),
                        ("name", "姓名", 140), ("rooms", "可见考场", 180)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=12)

        def reload():
            for i in tree.get_children():
                tree.delete(i)
            for u, rec in sorted(self.accounts.list_accounts().items()):
                tree.insert("", "end", iid=u,
                            values=(u, config.ROLE_TEXT.get(rec.get("role"), rec.get("role")),
                                    rec.get("name", ""),
                                    "、".join(rec.get("rooms") or []) or "全部"))
        reload()
        msg = ttk.Label(win, text="", foreground="#0a7d28")
        msg.pack(anchor="w", padx=12, pady=4)

        def sel():
            s = tree.selection()
            return s[0] if s else ""

        def do_new():
            dlg = tk.Toplevel(win)
            dlg.title("新建终端账户")
            dlg.geometry("400x300")
            dlg.transient(win)
            dlg.grab_set()
            f = ttk.Frame(dlg, padding=14)
            f.pack(fill="both", expand=True)
            v = {}
            roles = [r for r in config.ROLE_ACTIONS
                     if config.ROLE_LEVEL.get(r, 0) < config.ROLE_LEVEL[config.ROLE_OWNER]]
            for i, (k, label) in enumerate([("username", "用户名"), ("password", "口令"),
                                            ("name", "姓名"), ("rooms", "可见考场(逗号分隔)")]):
                ttk.Label(f, text=label + "：").grid(row=i, column=0, sticky="e", pady=4)
                v[k] = tk.StringVar()
                show = "●" if k == "password" else ""
                ttk.Entry(f, textvariable=v[k], width=26, show=show).grid(row=i, column=1, pady=4)
            ttk.Label(f, text="角色：").grid(row=4, column=0, sticky="e", pady=4)
            role_v = tk.StringVar(value=config.ROLE_PATROL)
            ttk.Combobox(f, textvariable=role_v, state="readonly", width=24,
                         values=roles).grid(row=4, column=1, pady=4)
            em = ttk.Label(f, text="", foreground="#c00")
            em.grid(row=5, column=0, columnspan=2)

            def save():
                rooms = [x.strip() for x in v["rooms"].get().split(",") if x.strip()]
                ok, m = self.accounts.create(v["username"].get(), v["password"].get(),
                                             role_v.get(), v["name"].get(), rooms,
                                             by_role=self.session.role)
                if not ok:
                    em.config(text=m)
                    return
                dlg.destroy()
                msg.config(text=m)
                reload()
            bar2 = ttk.Frame(f)
            bar2.grid(row=6, column=0, columnspan=2, pady=10)
            ttk.Button(bar2, text="保存", command=save).pack(side="left", padx=6)
            ttk.Button(bar2, text="取消", command=dlg.destroy).pack(side="left", padx=6)

        def do_reset():
            u = sel()
            if not u:
                messagebox.showinfo("提示", "请先选一个账户")
                return
            p = simpledialog.askstring("重置口令", "为 %s 设置新口令：" % u, show="●", parent=win)
            if not p:
                return
            ok, m = self.accounts.reset_password(u, p, by_role=self.session.role)
            msg.config(text=("%s" % m), foreground=("#0a7d28" if ok else "#c00"))
            reload()

        def do_del():
            u = sel()
            if not u:
                messagebox.showinfo("提示", "请先选一个账户")
                return
            if not messagebox.askyesno("删除账户", "确定删除 %s？" % u):
                return
            ok, m = self.accounts.delete(u, by_role=self.session.role)
            msg.config(text=m, foreground=("#0a7d28" if ok else "#c00"))
            reload()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=8)
        ttk.Button(bar, text="＋ 新建账户", command=do_new).pack(side="left")
        ttk.Button(bar, text="🔑 重置口令", command=do_reset).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑 删除账户", command=do_del).pack(side="left", padx=6)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")

    def _wipe_all(self):
        """所有者：清空本地运行数据（告警/复核/队列/账户）——需二次确认"""
        try:
            self.session.require("manage")
        except access.AccessError as e:
            messagebox.showerror("权限不足", str(e))
            return
        if not messagebox.askyesno("危险操作",
                                   "将清空本机终端数据（告警记录、复核留痕、补传队列、终端账户）。\n"
                                   "此操作不可恢复，确定继续？"):
            return
        code = simpledialog.askstring("二次确认", "请输入 WIPE 以确认执行：", parent=self.root)
        if code != "WIPE":
            messagebox.showinfo("已取消", "确认码不正确，未执行。")
            return
        self.store.log("清空本地数据", "所有者手动执行", self.session.username)
        import os as _os
        try:
            for f in _os.listdir(str(config.DATA_ROOT)):
                if f.endswith(".db") or f.endswith(".db-wal") or f.endswith(".db-shm"):
                    _os.remove(_os.path.join(str(config.DATA_ROOT), f))
            for d in (config.ATTACH_DIR, config.QUEUE_DIR):
                shutil.rmtree(str(d), ignore_errors=True)
        except OSError as e:
            messagebox.showerror("失败", str(e))
            return
        messagebox.showinfo("已清空", "本地数据已清空，程序将退出，请重新启动。")
        self.root.destroy()

    # ================= 逻辑 =================
    def refresh_all(self):
        self.refresh_alerts()
        self.refresh_reviews()
        self.refresh_ops()

    def refresh_alerts(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        try:
            self.session.require("view")
        except access.AccessError as e:
            self.alerts_hint.config(text=str(e))
            return
        alerts = access.visible_alerts(self.store, self.session, limit=400)
        alerts = self.hub.display_order(alerts)
        red_pending = 0
        for a in alerts:
            if a.needs_force_confirm():
                red_pending += 1
            tag = "red" if a.level == config.LEVEL_RED else "orange"
            if a.overdue() and a.status == models.ST_PENDING:
                tag = "overdue"
            cid = a.candidate_id or "—"
            if self.session.masked() and a.candidate_id:
                cid = models._mask_id(a.candidate_id)
            self.tree.insert("", "end", iid=a.alert_id, tags=(tag,),
                             values=(a.level_name(), a.created_at, a.code,
                                     a.room_id or "—", a.seat_no or "—", cid,
                                     self._st_text(a.status),
                                     config.REVIEW_CONCLUSIONS.get(a.review_conclusion, "—")))
        hint = "共 %d 条" % len(alerts)
        if red_pending:
            hint += "　⚠ 红色待确认 %d 条" % red_pending
        self.alerts_hint.config(text=hint)
        if red_pending:
            self._popup_red()

    def _popup_red(self):
        """红色预警弹窗（只弹一次，避免刷屏）"""
        key = "red_popup_shown_%s" % len(self.tree.get_children())
        if getattr(self, "_red_popup_key", "") == key:
            return
        self._red_popup_key = key
        messagebox.showwarning("红色预警",
                               "存在未确认的红色预警，请立即处置。\n（红色预警必须确认后才可复核）")

    @staticmethod
    def _st_text(st):
        return {"pending": "待处置", "acked": "已确认", "reviewed": "已复核",
                "closed": "已归档"}.get(st, st)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一条告警")
            return None
        return self.store.get_alert(sel[0])

    def _show_detail(self):
        a = self._selected()
        if a is None:
            return
        self.detail.delete("1.0", "end")
        lines = [access.render(a, self.session), "详情：%s" % (a.detail or "—"),
                 "设备：%s　来源：%s" % (a.device_id or "—", a.source),
                 "处置时限：%s" % time_str(a.sla_deadline)]
        for ev in self.reviewer.timeline(a.alert_id):
            lines.append("· %s  %s  %s  %s" % (ev["ts"], ev["event"], ev["who"], ev["detail"]))
        self.detail.insert("1.0", "\n".join(lines))
        self.rv_alert.set(a.alert_id)

    def _ack(self):
        a = self._selected()
        if a is None:
            return
        ok, msg = self.hub.ack(a.alert_id, self.session.username)
        self.status.config(text=msg)
        self.refresh_alerts()

    def _report(self):
        a = self._selected()
        if a is None:
            return
        note = self._ask_text("一键上报", "请填写上报说明：")
        if note is None:
            return
        ok, msg = self.reviewer.report(a.alert_id, note, self.session)
        self.status.config(text=msg)
        if not ok:
            messagebox.showerror("失败", msg)

    def _demo(self):
        created, dup = self.hub.ingest_many(self.hub.simulate(6))
        self.status.config(text="已生成演示告警 %d 条（去重 %d）— 来源标注为 demo，非真实接入"
                                % (len(created), dup))
        self.refresh_alerts()

    def _flush(self):
        r = self.outbox.flush(force=False)
        msg = ("在线：补传 %d，失败 %d，超期 %d，剩余 %d"
               % (r["sent"], r["failed"], r["dropped"], r["pending"]))
        if not r["online"]:
            msg = "当前离线：待补传 %d 条（恢复后会自动补传）" % r["pending"]
        self.status.config(text=msg)
        self.refresh_ops()

    def refresh_reviews(self):
        self.rv_list.delete("1.0", "end")
        for r in self.reviewer.review_rows(limit=60):
            line = "%s  %s  %s  %s  理由：%s\n" % (
                r["created_at"], r["alert_id"], r["operator"],
                config.REVIEW_CONCLUSIONS.get(r["conclusion"], r["conclusion"]),
                (r["reason"] or "")[:60])
            self.rv_list.insert("end", line,
                                "fp" if r.get("closed_loop") else "")

    def _pick_attach(self):
        p = filedialog.askopenfilename(title="选择复核附件")
        if p:
            self.rv_att.set(p)

    def _submit_review(self):
        reason = self.rv_reason.get("1.0", "end").strip()
        att = [self.rv_att.get().strip()] if self.rv_att.get().strip() else []
        ok, res = self.reviewer.review(self.rv_alert.get().strip(), self.rv_conc.get(),
                                       reason, self.session,
                                       witness=self.rv_wit.get().strip(), attachments=att)
        if not ok:
            messagebox.showerror("复核失败", res)
            return
        self.rv_reason.delete("1.0", "end")
        self.rv_att.set("")
        self.status.config(text="复核已留痕：%s" % res)
        self.refresh_all()

    def _fp_stats(self):
        rows = self.reviewer.stats()
        if not rows:
            messagebox.showinfo("误报统计", "暂无复核数据")
            return
        txt = "\n".join("%s：复核 %d 次，误报 %d 次，误报率 %.1f%%"
                        % (r["code"], r["total"], r["false_positive"], r["fp_rate"] * 100)
                        for r in rows)
        messagebox.showinfo("误报统计（算法迭代闭环）", txt)

    def _pick_image(self):
        p = filedialog.askopenfilename(title="选择核验图片",
                                       filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp")])
        if p:
            self.v_img.set(p)

    def _do_verify(self):
        try:
            self.session.require("verify")
        except access.AccessError as e:
            messagebox.showerror("权限不足", str(e))
            return
        self.v_out.delete("1.0", "end")
        self.v_out.insert("end", "核验中…（首次需加载模型，CPU 上约 1 秒/张）\n")
        self.root.update_idletasks()
        r = self.face.verify(self.v_img.get().strip(), self.v_cand.get().strip(),
                             mode=self.v_mode.get(), operator=self.session.username)
        self.v_out.delete("1.0", "end")
        if not r.get("ok"):
            self.v_out.insert("end", "核验未完成：%s\n\n" % r.get("reason"))
            self.v_out.insert("end", "提示：先在 01 模块里导入考生并「建档」，再回到这里核验。\n")
            return
        self.v_out.insert("end", "结果：%s（%s）\n" % (r.get("result"), r.get("reason_code")))
        self.v_out.insert("end", "说明：%s\n" % r.get("reason"))
        self.v_out.insert("end", "相似度：%s　阈值：%s\n" % (r.get("score"), r.get("threshold")))
        self.v_out.insert("end", "考生：%s %s\n" % (r.get("candidate_id"),
                                                  r.get("candidate_name") or ""))
        self.v_out.insert("end", "带出信息：%s\n" % (r.get("doc_info") or "—"))
        self.v_out.insert("end", "来源：%s\n" % r.get("source"))
        # 核验结果作为一条本地留痕（不算告警）
        self.store.log("手持核验", "%s %s 相似度=%s" % (r.get("candidate_id"),
                                                     r.get("result"), r.get("score")),
                       self.session.username)

    def refresh_ops(self):
        st = self.outbox.status()
        s = self.store.summary()
        chain_ok, chain_msg, _ = self.store.verify_chain()
        self.ops_left.delete("1.0", "end")
        self.ops_left.insert("end",
            "在线状态：%s\n补传地址：%s\n待补传：%d 条（超 %d 小时不再重传）\n\n"
            "告警总计：%d\n  红色：%d　橙色：%d\n  待处置：%d　已确认：%d　已复核：%d\n"
            "复核记录：%d 条\n\n审计链：%s\n"
            % ("在线" if st["online"] else "离线（本地功能不受影响）",
               st["platform"], st["pending"], st["max_age_hours"],
               s["total"], s["by_level"].get(config.LEVEL_RED, 0),
               s["by_level"].get(config.LEVEL_ORANGE, 0),
               s["by_status"].get(models.ST_PENDING, 0),
               s["by_status"].get(models.ST_ACKED, 0),
               s["by_status"].get(models.ST_REVIEWED, 0),
               s["reviews"], ("✅ " + chain_msg) if chain_ok else ("❌ " + chain_msg)))
        fs = self.fleet.status()
        ck = self.clock.display()
        self.ops_right.delete("1.0", "end")
        self.ops_right.insert("end",
            "统一管控\n  版本：%s\n  白名单：%s\n  外设：%s\n  待执行指令：%d\n"
            "  擦除待确认：%s\n\n稳定性（目标 %d 小时）\n  心跳：%d 次　最长连续：%.2f 小时\n"
            "  %s\n\n标准时间\n  时间：%s（%s）\n  偏差：%.1f 秒 %s\n  下一条指令：%s\n"
            % (fs["版本"], "、".join(fs["白名单"]),
               "、".join("%s=%s" % kv for kv in sorted(fs["外设"].items())),
               fs["待执行指令"], "是" if fs["擦除待确认"] else "否",
               fs["稳定性"]["target_hours"], fs["稳定性"]["ticks"],
               fs["稳定性"]["longest_hours"], fs["稳定性"]["note"],
               ck["standard_time"], ck["source"], ck["drift_sec"],
               "✅" if ck["drift_ok"] else "⚠ 超限", ck["next"]))

    def _soak(self):
        self.fleet.soak_tick(note="manual")
        self.status.config(text="已写入一次稳定性心跳")
        self.refresh_ops()

    def _chain(self):
        ok, msg, n = self.store.verify_chain()
        (messagebox.showinfo if ok else messagebox.showerror)("审计链校验", msg)

    def _ask_text(self, title, prompt):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("420x220")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=prompt).pack(anchor="w", padx=12, pady=(12, 4))
        t = tk.Text(win, height=6, wrap="word", font=FONT)
        t.pack(fill="both", expand=True, padx=12)
        box = {"v": None}

        def ok():
            box["v"] = t.get("1.0", "end").strip()
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="确定", command=ok).pack(side="right", padx=12)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        t.focus_set()
        self.root.wait_window(win)
        return box["v"]

    def _tick(self):
        """定时：补传重试 + 时钟播报 + 红色弹窗刷新（断网时也不报错）"""
        try:
            self.outbox.flush(force=False)
            for it in self.clock.fire_due():
                self.status.config(text="播报：%s %s" % (it["at"], it["text"]))
            self.clock.check_and_alert()
            self.fleet.soak_tick()
        except Exception as e:
            self.status.config(text="后台任务异常（已忽略）：%s" % str(e)[:80])
        self.root.after(config.OFFLINE_RETRY_SEC * 1000, self._tick)


def time_str(epoch):
    import time
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else "—"


def main():
    root = tk.Tk()
    try:
        TerminalApp(root)
    except Exception:
        import traceback
        log = os.path.join(str(config.LOG_DIR), "gui_crash.log")
        try:
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
        messagebox.showerror("启动失败", traceback.format_exc()[-600:])
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
