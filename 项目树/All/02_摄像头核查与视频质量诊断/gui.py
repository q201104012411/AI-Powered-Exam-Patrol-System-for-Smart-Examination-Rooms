# -*- coding: utf-8 -*-
"""
摄像头核查与视频质量诊断 · 桌面界面（tkinter）

四个页签：
  设备台账   登记机位、采集基准图、删除机位（删除仅所有者）
  巡检核查   跑三级巡检、看逐机位缺陷（红/黄着色）
  整改工单   发现-派单-整改-复核 闭环
  报告与指标 五项关键指标 + 逐考场报告导出

账户与权限（与网页端**共用同一份**）：
  启动先出登录页（登录界面**直接建在主窗口内**，不用 Toplevel + transient，
  01 项目踩过"隐藏父窗口时子窗口不显示"的坑）；
  账户存 Store 的 kv 表（键 web.accounts），与网页端 app.py 同一份数据、同一套哈希；
  所有受控操作入口先用 Session.can() 判权限，越权直接 messagebox 明确提示。

运行：python gui.py      （隐藏命令行窗口见「切换命令行窗口.bat」）
"""
import json
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import cv2
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

import config
import diagnose
import models
import probe
import storage
import web_accounts
import baseline as baseline_mod
import patrol as patrol_mod
import report as report_mod
import workorder as wo_mod

FONT = ("Microsoft YaHei", 10)
MONO = ("Consolas", 10)

# pythonw 隐藏窗口启动时 stdout/stderr 为 None，兜住避免任何 print 崩掉界面
config.ensure_stdio("gui_console.log")

# 隐藏命令行窗口标记（所有者可切换；.bat 启动时读它决定用 python 还是 pythonw）
HIDE_CMD_FLAG = str(config.HIDE_CMD_FLAG)


class App(object):
    def __init__(self, root):
        self.root = root
        self.root.title("摄像头核查与视频质量诊断")
        self.root.geometry("1180x780")
        self.store = storage.Store()
        self.accounts = web_accounts.Accounts(self.store)
        self.user = None                     # web_accounts.Session
        self.baselines = baseline_mod.BaselineStore(self.store)
        self.orders = wo_mod.WorkOrderManager(self.store)
        self.runner = patrol_mod.PatrolRunner(self.store, baseline_store=self.baselines)
        self.reporter = report_mod.Reporter(self.store, runner=self.runner, orders=self.orders)
        self._build_login()                  # 先登录，登录成功后原地切主界面

    # ---------------- 登录（直接建在主窗口内，最稳） ----------------
    def _build_login(self):
        self.user = None
        for w in self.root.winfo_children():
            w.destroy()
        self.root.unbind("<Return>")
        self.root.title("摄像头核查与视频质量诊断　｜　登录")
        self.root.geometry("560x460")
        box = ttk.Frame(self.root, padding=26)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="智能考场", font=("Microsoft YaHei", 20, "bold")).pack(pady=(24, 2))
        ttk.Label(box, text="摄像头核查与视频质量诊断", font=("Microsoft YaHei", 11),
                  foreground="#555").pack(pady=(0, 20))

        card = ttk.LabelFrame(box, text="操作人员登录", padding=18)
        card.pack(fill="x", padx=16)
        ttk.Label(card, text="用户名：").grid(row=0, column=0, sticky="e", pady=8)
        self.login_user = tk.StringVar()
        e_u = ttk.Entry(card, textvariable=self.login_user, width=22,
                        font=("Consolas", 12))
        e_u.grid(row=0, column=1, pady=8, padx=(0, 6))
        ttk.Label(card, text="口　令：").grid(row=1, column=0, sticky="e", pady=8)
        self.login_pwd = tk.StringVar()
        e_p = ttk.Entry(card, textvariable=self.login_pwd, width=22, show="*",
                        font=("Consolas", 12))
        e_p.grid(row=1, column=1, pady=8, padx=(0, 6))

        self.login_msg = ttk.Label(box, text="", foreground="#d9534f",
                                   font=("Microsoft YaHei", 10))
        self.login_msg.pack(pady=(12, 2))

        bar = ttk.Frame(box)
        bar.pack(pady=6)
        ttk.Button(bar, text="登  录", width=12, command=self._do_login).pack(side="left", padx=8)
        ttk.Button(bar, text="退  出", width=12, command=self.root.destroy).pack(side="left", padx=8)

        ttk.Label(box, text="账户与网页版（http://127.0.0.1:5010/）是同一份",
                  foreground="#999", font=("Microsoft YaHei", 9)).pack(pady=(16, 0))
        ttk.Label(box, text="默认账户：owner（所有者）、teacher1（管理员）　口令由系统管理员掌握，不在界面显示",
                  foreground="#999", font=("Microsoft YaHei", 9)).pack()
        self.root.bind("<Return>", lambda _e: self._do_login())
        e_u.focus_set()

    def _do_login(self):
        s, msg = self.accounts.authenticate(self.login_user.get(), self.login_pwd.get())
        if s is None:
            self.login_msg.config(text="登录失败：%s" % msg)
            self.login_pwd.set("")
            return
        self.user = s
        self._build()                        # 原地切换成主界面
        if self.login_user.get().strip() in web_accounts.DEFAULT_ACCOUNTS:
            self.status.config(text="提示：%s 正在使用默认口令，建议在「🔑 修改口令」里改掉"
                                     % s.username)

    def _switch_account(self):
        if not messagebox.askyesno("切换账号", "确定退出当前账号并返回登录页？"):
            return
        who = self.user.username if self.user else "-"
        self.store.log("切换账号", "操作人=%s" % who, who)
        self._build_login()
        self.login_user.set("")
        self.login_pwd.set("")

    def _logout(self):
        if not messagebox.askyesno("退出", "确定退出登录并关闭程序？"):
            return
        who = self.user.username if self.user else "-"
        self.store.log("退出程序", "操作人=%s" % who, who)
        self.root.destroy()

    # ---------------- 权限 ----------------
    def can(self, action):
        return bool(self.user) and self.user.can(action)

    def _need(self, action):
        """受控操作入口统一走这里：无权限就给明确提示，不静默失败、不崩"""
        if self.can(action):
            return True
        messagebox.showerror("权限不足", self.user.deny_text(action)
                             if self.user else "请先登录")
        return False

    def _guard(self, action, fn):
        """返回一个"先判权限、再执行"的回调，给按钮 command 用"""
        def wrapper(*a, **kw):
            if not self._need(action):
                return None
            return fn(*a, **kw)
        return wrapper

    # ---------------- 主界面 ----------------
    def _build(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.unbind("<Return>")
        self.root.title("摄像头核查与视频质量诊断　｜　%s（%s）"
                        % (self.user.username, self.user.role_text))
        self.root.geometry("1180x780")

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="摄像头核查与视频质量诊断",
                  font=("Microsoft YaHei", 14, "bold")).pack(side="left")
        ttk.Label(top, text="当前账号：%s　角色：%s"
                  % (self.user.username, self.user.role_text),
                  foreground="#1f56b8").pack(side="left", padx=(14, 0))

        ttk.Button(top, text="退出", command=self._logout).pack(side="right")
        ttk.Button(top, text="🔄 切换账号", command=self._switch_account
                   ).pack(side="right", padx=4)
        ttk.Button(top, text="🔑 修改口令", command=self._change_own_pwd
                   ).pack(side="right", padx=4)
        if self.can("account_manage"):
            ttk.Button(top, text="👥 终端账户", command=self._account_manager
                       ).pack(side="right", padx=4)
        else:
            b = ttk.Button(top, text="👥 终端账户（仅所有者）", state="disabled")
            b.pack(side="right", padx=4)
        if self.can("audit_view"):
            ttk.Button(top, text="🔐 审计链", command=self._show_audit
                       ).pack(side="right", padx=4)
        else:
            ttk.Button(top, text="🔐 审计链（仅所有者）", state="disabled"
                       ).pack(side="right", padx=4)

        # 隐藏命令行窗口（仅所有者可切换；管理员勾选框直接禁用）
        self.hide_cmd = tk.BooleanVar(value=web_accounts.hidden_cmd_on())
        cb = ttk.Checkbutton(top, text="隐藏命令行窗口（下次启动生效）",
                             variable=self.hide_cmd, command=self._toggle_hide_cmd)
        cb.pack(side="right", padx=(4, 12))
        if not self.can("hide_cmd_toggle"):
            cb.config(state="disabled")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.t_dev = ttk.Frame(nb, padding=8)
        self.t_chk = ttk.Frame(nb, padding=8)
        self.t_wo = ttk.Frame(nb, padding=8)
        self.t_rep = ttk.Frame(nb, padding=8)
        # 实时监控：仅所有者和管理员可见
        self.can_monitor = self.user.role in (web_accounts.ROLE_OWNER,
                                              web_accounts.ROLE_ADMIN)
        if self.can_monitor:
            self.t_mon = ttk.Frame(nb, padding=8)
        nb.add(self.t_dev, text="  设备台账  ")
        nb.add(self.t_chk, text="  巡检核查  ")
        nb.add(self.t_wo, text="  整改工单  ")
        nb.add(self.t_rep, text="  报告与指标  ")
        if self.can_monitor:
            nb.add(self.t_mon, text="  实时监控  ")
        self._dev_tab()
        self._chk_tab()
        self._wo_tab()
        self._rep_tab()
        if self.can_monitor:
            self._mon_tab()
        self.status = ttk.Label(self.root, text="就绪　｜　%s（%s）"
                                % (self.user.username, self.user.role_text),
                                foreground="#666")
        self.status.pack(fill="x", padx=12, pady=(0, 6))
        self.refresh_all()

    # ---------------- 设备台账 ----------------
    def _dev_tab(self):
        f = self.t_dev
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="＋ 新增机位",
                   command=self._guard("device_add", self._add_device)).pack(side="left")
        ttk.Button(bar, text="📷 采集基准图",
                   command=self._guard("baseline_capture", self._capture)
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_devices).pack(side="left", padx=4)
        if self.can("device_delete"):
            ttk.Button(bar, text="🗑 删除机位", command=self._delete_device
                       ).pack(side="left", padx=4)
        else:
            ttk.Button(bar, text="🗑 删除机位（仅所有者）", state="disabled"
                       ).pack(side="left", padx=4)
        cols = ("code", "room", "site", "position", "res", "fov", "url", "base", "hb")
        self.dtree = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for c, t, w in (("code", "设备编码", 130), ("room", "考场", 70), ("site", "考点", 100),
                        ("position", "安装位置", 110), ("res", "分辨率", 90),
                        ("fov", "视场角", 70), ("url", "取流地址", 240),
                        ("base", "基准图", 70), ("hb", "心跳", 140)):
            self.dtree.heading(c, text=t)
            self.dtree.column(c, width=w, anchor="w")
        self.dtree.pack(fill="both", expand=True)
        self.dtree.tag_configure("nobase", background="#fff6e0")

    def refresh_devices(self):
        for i in self.dtree.get_children():
            self.dtree.delete(i)
        for d in self.store.list_devices(enabled_only=False):
            base = self.store.get_baseline(d.device_id)
            ok, why = probe.heartbeat_status(d)
            self.dtree.insert("", "end", iid=d.device_id,
                              tags=("" if base else "nobase",),
                              values=(d.code, d.room_id or "—", d.site or "—",
                                      d.position or "—", d.resolution,
                                      "%.0f°" % d.fov_deg, d.url or "—",
                                      "有" if base else "缺",
                                      why if ok else "✗ " + why))

    def _add_device(self):
        win = tk.Toplevel(self.root)
        win.title("新增机位")
        win.geometry("460x400")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        fields = [("code", "设备编码*"), ("name", "设备名称"), ("room_id", "考场*"),
                  ("site", "考点"), ("url", "取流地址"),
                  ("resolution", "分辨率"), ("fov_deg", "视场角(度)"),
                  ("pan_deg", "水平角(度)"), ("tilt_deg", "俯仰角(度)"),
                  ("session", "场次")]
        vars_ = {}
        for i, (k, label) in enumerate(fields):
            ttk.Label(f, text=label + "：").grid(row=i, column=0, sticky="e", pady=3)
            v = tk.StringVar(value="90" if k == "fov_deg" else
                             ("1920x1080" if k == "resolution" else ""))
            vars_[k] = v
            ttk.Entry(f, textvariable=v, width=30).grid(row=i, column=1, pady=3)
        r = len(fields)
        audio = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="配拾音器（音频链路核查）", variable=audio
                        ).grid(row=r, column=1, sticky="w", pady=4)
        msg = ttk.Label(f, text="", foreground="#c00")
        msg.grid(row=r + 1, column=0, columnspan=2)

        def save():
            if not self._need("device_add"):
                return
            if not vars_["code"].get().strip():
                msg.config(text="设备编码必填")
                return
            try:
                fov = float(vars_["fov_deg"].get() or 90)
                pan = float(vars_["pan_deg"].get() or 0)
                tilt = float(vars_["tilt_deg"].get() or 0)
            except ValueError:
                msg.config(text="角度必须是数字")
                return
            d = models.Device(code=vars_["code"].get().strip(), name=vars_["name"].get().strip(),
                              room_id=vars_["room_id"].get().strip(),
                              site=vars_["site"].get().strip(),
                              url=vars_["url"].get().strip(),
                              resolution=vars_["resolution"].get().strip() or "1920x1080",
                              fov_deg=fov, pan_deg=pan, tilt_deg=tilt,
                              has_audio=audio.get(),
                              session=vars_["session"].get().strip())
            self.store.save_device(d)
            self.store.log("台账登记机位", d.code, self.user.username)
            win.destroy()
            self.refresh_all()

        bar = ttk.Frame(f)
        bar.grid(row=r + 2, column=0, columnspan=2, pady=10)
        ttk.Button(bar, text="保存", command=save).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=6)

    def _selected_device(self):
        sel = self.dtree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一台机位")
            return None
        return self.store.get_device(device_id=sel[0])

    def _delete_device(self):
        """删除机位（仅所有者；admin 的按钮已禁用，这里再拦一次）"""
        if not self._need("device_delete"):
            return
        d = self._selected_device()
        if d is None:
            return
        if not messagebox.askyesno("删除机位",
                                   "确定删除机位 %s（%s）？\n"
                                   "该机位的基准图登记与心跳记录会一并删除，"
                                   "已产生的核查记录与工单保留（审计留痕）。"
                                   % (d.code, d.room_id or "未绑定考场")):
            return
        self.store.delete_device(d.device_id)
        self.store.log("删除机位", d.code, self.user.username)
        messagebox.showinfo("已删除", "机位 %s 已删除" % d.code)
        self.refresh_all()

    def _capture(self):
        if not self._need("baseline_capture"):
            return
        d = self._selected_device()
        if d is None:
            return
        p = filedialog.askopenfilename(title="选择基准图（现场采集的机位画面）",
                                       filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp")])
        if not p:
            return
        ok, res = self.baselines.capture(d, image_path=p, operator=self.user.username)
        if not ok:
            messagebox.showerror("采集失败", str(res))
            return
        messagebox.showinfo("已采集", "基准图已留档：\n%s\n清晰度 %.1f"
                            % (res["path"], res["sharpness"]))
        self.refresh_all()

    # ---------------- 巡检核查 ----------------
    def _chk_tab(self):
        f = self.t_chk
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="▶ 跑一轮日常巡检",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_DAILY))
                   ).pack(side="left")
        ttk.Button(bar, text="考前专项核查",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_PREEXAM))
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="考前一小时自检",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_BEFORE))
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_checks).pack(side="left", padx=4)
        self.chk_hint = ttk.Label(bar, text="", foreground="#c07000")
        self.chk_hint.pack(side="right")
        cols = ("ts", "code", "room", "online", "defects", "policy")
        self.ctree = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c, t, w in (("ts", "时间", 150), ("code", "机位", 120), ("room", "考场", 70),
                        ("online", "在线", 60), ("defects", "缺陷", 480),
                        ("policy", "策略", 110)):
            self.ctree.heading(c, text=t)
            self.ctree.column(c, width=w, anchor="w")
        self.ctree.pack(fill="both", expand=True)
        self.ctree.tag_configure("bad", background="#ffe3e3", foreground="#a00000")
        self.ctree.tag_configure("warn", background="#fff6e0", foreground="#8a5a00")
        self.ctree.bind("<<TreeviewSelect>>", lambda _e: self._show_check())
        self.detail = tk.Text(f, height=6, wrap="word", font=MONO)
        self.detail.pack(fill="x", pady=(6, 0))

    def _run(self, policy):
        if not self._need("patrol_run"):
            return
        if not self.store.list_devices():
            messagebox.showinfo("提示", "请先在「设备台账」登记机位")
            return
        s = self.runner.run(policy=policy, operator=self.user.username)
        self.chk_hint.config(text="%s：%d 台，异常 %d 台，在线率 %s"
                                  % (s["name"], s["total"], s["bad"],
                                     "—" if s["online_rate"] is None
                                     else "%.1f%%" % (s["online_rate"] * 100)))
        for ck in s["results"]:
            d = self.store.get_device(device_id=ck.device_id)
            if d is not None and not ck.ok:
                wo, _msg = self.orders.ensure_from_check(ck, d,
                                                         operator=self.user.username)
        self.status.config(text="%s完成：异常 %d 台%s"
                                % (s["name"], s["bad"],
                                   "，已自动建整改工单" if s["bad"] else ""))
        self.refresh_all()

    def refresh_checks(self):
        for i in self.ctree.get_children():
            self.ctree.delete(i)
        for ck in self.store.list_checks(limit=200):
            codes = [d for d in ck.defects]
            txt = "、".join("%s(%s)" % (config.defect_name(d.code), d.severity)
                           for d in codes) or "正常"
            tag = "bad" if any(d.severity == config.SEV_FAIL for d in codes) else (
                "warn" if codes else "")
            self.ctree.insert("", "end", iid=ck.check_id, tags=(tag,),
                              values=(ck.ts, ck.device_code, ck.room_id or "—",
                                      "是" if ck.online else "否", txt,
                                      config.PATROLS.get(ck.policy, {}).get("name", ck.policy)))

    def _show_check(self):
        sel = self.ctree.selection()
        if not sel:
            return
        ck = None
        for c in self.store.list_checks(limit=1000):
            if c.check_id == sel[0]:
                ck = c
                break
        if ck is None:
            return
        self.detail.delete("1.0", "end")
        lines = [ck.display()]
        for d in ck.defects:
            lines.append("· %s（%s）：%s" % (config.defect_name(d.code), d.severity, d.detail))
        m = ck.metrics
        keys = ("sharpness", "mean_luma", "contrast", "color_cast", "stripe_ratio",
                "noise", "blockiness", "shift_px", "angle_deg", "texture_loss",
                "time_drift_sec", "baseline_missing_reason")
        lines.append("指标：" + "　".join(
            "%s=%s" % (k, _fmt(m.get(k))) for k in keys if m.get(k) is not None))
        self.detail.insert("1.0", "\n".join(lines))

    # ---------------- 整改工单 ----------------
    def _wo_tab(self):
        f = self.t_wo
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        for text, act in (("派单", "dispatch"), ("提交整改", "rectify"),
                          ("复核通过", "review_ok"), ("复核打回", "review_no")):
            ttk.Button(bar, text=text,
                       command=self._guard("workorder_operate",
                                           lambda a=act: self._wo_act(a))
                       ).pack(side="left", padx=4)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_orders).pack(side="left", padx=4)
        self.wo_hint = ttk.Label(bar, text="", foreground="#c07000")
        self.wo_hint.pack(side="right")
        cols = ("id", "dev", "room", "codes", "sev", "status", "assigned", "created")
        self.wtree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        for c, t, w in (("id", "工单号", 130), ("dev", "机位", 120), ("room", "考场", 70),
                        ("codes", "缺陷", 300), ("sev", "级别", 70), ("status", "状态", 90),
                        ("assigned", "责任人", 100), ("created", "建单时间", 150)):
            self.wtree.heading(c, text=t)
            self.wtree.column(c, width=w, anchor="w")
        self.wtree.pack(fill="both", expand=True)

    def refresh_orders(self):
        for i in self.wtree.get_children():
            self.wtree.delete(i)
        for wo in self.store.list_orders(limit=300):
            self.wtree.insert("", "end", iid=wo.order_id,
                              values=(wo.order_id, wo.device_code, wo.room_id or "—",
                                      "、".join(config.defect_name(c) for c in wo.codes),
                                      wo.severity, wo_orders_text(wo.status),
                                      wo.assigned_to or "—", wo.created_at))
        s = self.orders.stats()
        self.wo_hint.config(text="共 %d 单，已闭环 %d，未闭环 %d"
                                 % (s["total"], s["closed"], s["pending"]))

    def _wo_act(self, act):
        if not self._need("workorder_operate"):
            return
        sel = self.wtree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条工单")
            return
        oid = sel[0]
        who = self.user.username
        if act == "dispatch":
            name = _ask(self.root, "派单", "整改责任人：")
            if name:
                ok, msg = self.orders.dispatch(oid, name, who)
                self._wo_result(ok, msg)
        elif act == "rectify":
            p = filedialog.askopenfilename(title="选择整改后截图（必须提供）",
                                           filetypes=[("图片/视频", "*.jpg *.jpeg *.png *.mp4")])
            if not p:
                return
            note = _ask(self.root, "整改说明", "整改说明：") or ""
            ok, msg = self.orders.rectify(oid, p, note, who)
            self._wo_result(ok, msg)
        elif act == "review_ok":
            note = _ask(self.root, "复核", "复核意见：") or ""
            ok, msg = self.orders.review(oid, True, note, who)
            self._wo_result(ok, msg)
        else:
            note = _ask(self.root, "复核打回", "不通过原因：") or ""
            ok, msg = self.orders.review(oid, False, note, who)
            self._wo_result(ok, msg)

    def _wo_result(self, ok, msg):
        self.status.config(text=msg)
        (messagebox.showinfo if ok else messagebox.showerror)("工单", msg)
        self.refresh_all()

    # ---------------- 报告与指标 ----------------
    def _rep_tab(self):
        f = self.t_rep
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="生成并导出报告",
                   command=self._guard("report_export", self._export)).pack(side="left")
        ttk.Button(bar, text="🔄 刷新指标", command=self.refresh_report).pack(side="left", padx=4)
        self.rep = tk.Text(f, wrap="word", font=MONO)
        self.rep.pack(fill="both", expand=True)

    def refresh_report(self):
        self.rep.delete("1.0", "end")
        text = self.reporter.full_report()
        self.rep.insert("1.0", text)

    def _export(self):
        if not self._need("report_export"):
            return
        text = self.reporter.full_report()
        path = self.reporter.export(text)
        if not path:
            messagebox.showerror("导出失败", "无法写入报告目录")
            return
        self.refresh_report()
        messagebox.showinfo("已导出", path)

    # ---------------- 账户（本人） ----------------
    def _change_own_pwd(self):
        if not self._need("change_own_pwd"):
            return
        old = simpledialog.askstring("修改口令", "原口令：", show="*", parent=self.root)
        if old is None:
            return
        new = simpledialog.askstring("修改口令", "新口令（至少 6 位）：", show="*",
                                     parent=self.root)
        if new is None:
            return
        confirm = simpledialog.askstring("修改口令", "再输一次新口令：", show="*",
                                         parent=self.root)
        if confirm is None:
            return
        if new != confirm:
            messagebox.showerror("修改失败", "两次输入的新口令不一致")
            return
        ok, msg = self.accounts.change_password(self.user.username, old, new)
        (messagebox.showinfo if ok else messagebox.showerror)("修改口令", msg)

    # ---------------- 终端账户管理（仅所有者） ----------------
    def _account_manager(self):
        if not self._need("account_manage"):
            return
        win = tk.Toplevel(self.root)
        win.title("终端账户管理　｜　当前用户：%s（%s）"
                  % (self.user.username, self.user.role_text))
        win.geometry("700x440")
        win.transient(self.root)

        ttk.Label(win, text="账户与网页端共用同一份（kv 键 web.accounts），口令只存哈希、不回显。",
                  foreground="#555").pack(anchor="w", padx=10, pady=(10, 4))
        ttk.Label(win, text="规则：不能新建与自己同级或更高的账户；不能删除所有者；"
                            "不能删除自己；至少保留一个可用账户。",
                  foreground="#555").pack(anchor="w", padx=10)

        cols = ("user", "role", "name", "note")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=10)
        for c, t, w in (("user", "用户名", 150), ("role", "角色", 100),
                        ("name", "名称", 200), ("note", "备注", 180)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=8)

        def selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先选择一个账户", parent=win)
                return None
            return sel[0]

        def reload():
            self.accounts.reload()
            for i in tree.get_children():
                tree.delete(i)
            me = self.user.username
            for a in self.accounts.list_accounts():
                note = []
                if a["username"] == me:
                    note.append("当前登录")
                if a["role"] == web_accounts.ROLE_OWNER:
                    note.append("所有者不可删")
                tree.insert("", "end", iid=a["username"],
                            values=(a["username"], a["role_text"], a["name"],
                                    "／".join(note) or "—"))

        def new_account():
            if not self._need("account_manage"):
                return
            u = simpledialog.askstring("新建账户", "用户名：", parent=win)
            if not u:
                return
            nm = simpledialog.askstring("新建账户", "名称（可空）：", parent=win) or ""
            role = _choose_role(win)
            if not role:
                return
            p1 = simpledialog.askstring("新建账户", "口令（至少 6 位）：", show="*", parent=win)
            if not p1:
                return
            p2 = simpledialog.askstring("新建账户", "再输一次口令：", show="*", parent=win)
            if p1 != p2:
                messagebox.showerror("新建失败", "两次输入的口令不一致", parent=win)
                return
            ok, msg = self.accounts.create(u, p1, role, nm, by=self.user.username)
            (messagebox.showinfo if ok else messagebox.showerror)("新建账户", msg, parent=win)
            reload()

        def reset_pwd():
            if not self._need("account_manage"):
                return
            u = selected()
            if not u:
                return
            p1 = simpledialog.askstring("重置口令", "%s 的新口令（至少 6 位）：" % u,
                                        show="*", parent=win)
            if not p1:
                return
            p2 = simpledialog.askstring("重置口令", "再输一次：", show="*", parent=win)
            if p1 != p2:
                messagebox.showerror("重置失败", "两次输入的口令不一致", parent=win)
                return
            ok, msg = self.accounts.reset_password(u, p1, by=self.user.username)
            (messagebox.showinfo if ok else messagebox.showerror)("重置口令", msg, parent=win)
            reload()

        def del_account():
            if not self._need("account_manage"):
                return
            u = selected()
            if not u:
                return
            if not messagebox.askyesno("删除账户", "确定删除账户 %s？" % u, parent=win):
                return
            ok, msg = self.accounts.delete(u, by=self.user.username)
            (messagebox.showinfo if ok else messagebox.showerror)("删除账户", msg, parent=win)
            reload()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="＋ 新建账户", command=new_account).pack(side="left", padx=4)
        ttk.Button(bar, text="🔑 重置口令", command=reset_pwd).pack(side="left", padx=4)
        ttk.Button(bar, text="🗑 删除账户", command=del_account).pack(side="left", padx=4)
        ttk.Button(bar, text="🔄 刷新", command=reload).pack(side="left", padx=4)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)
        reload()

    # ---------------- 审计链（仅所有者） ----------------
    def _show_audit(self):
        if not self._need("audit_view"):
            return
        ok, msg, n = self.store.verify_chain()
        win = tk.Toplevel(self.root)
        win.title("审计链　｜　%s（%s）" % (self.user.username, self.user.role_text))
        win.geometry("900x560")
        win.transient(self.root)
        ttk.Label(win, text="哈希链校验：%s" % msg,
                  foreground=("#1a7f37" if ok else "#c00"),
                  font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        cols = ("seq", "ts", "action", "detail", "operator", "hash")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=18)
        for c, t, w in (("seq", "#", 50), ("ts", "时间", 150), ("action", "动作", 140),
                        ("detail", "详情", 300), ("operator", "操作人", 100),
                        ("hash", "哈希(前12位)", 130)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=10)
        for r in self.store.audit_rows(limit=300):
            tree.insert("", "end", values=(r["seq"], r["ts"], r["action"],
                                           r["detail"], r["operator"],
                                           (r["hash"] or "")[:12]))
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=8)

    # ---------------- 隐藏命令行窗口 ----------------
    def _toggle_hide_cmd(self):
        """所有者开关：写/删标记文件，.bat 下次启动据此决定用 pythonw 还是 python"""
        if not self._need("hide_cmd_toggle"):
            self.hide_cmd.set(web_accounts.hidden_cmd_on())      # 勾选框回滚
            return
        on = self.hide_cmd.get()
        ok, msg = web_accounts.set_hidden_cmd(on, self.store, self.user.username)
        if not ok:
            self.hide_cmd.set(web_accounts.hidden_cmd_on())
            messagebox.showerror("设置失败", msg)
            return
        messagebox.showinfo("已设置", "%s\n标记文件：%s" % (msg, HIDE_CMD_FLAG))

    # ---------------- 总刷新 ----------------
    def refresh_all(self):
        self.refresh_devices()
        self.refresh_checks()
        self.refresh_orders()
        self.refresh_report()
        if self.can_monitor:
            self.refresh_mon()

    # ---------------- 实时监控（多摄像头，owner/admin 可见） ----------------
    def _mon_tab(self):
        f = self.t_mon
        ttk.Label(f, text="实时监控（基于设备台账取流地址，多摄像头同时播放）",
                  font=FONT).pack(anchor="w")
        if not HAS_CV2 or not HAS_PIL:
            ttk.Label(f, text="⚠ 缺少视频播放依赖：需要 opencv-python 与 Pillow",
                      foreground="#c00").pack(anchor="w", pady=10)
            return

        self._cam_threads = {}
        self._cam_caps = {}
        self._cam_running = {}
        self._cam_photos = {}

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 6))
        ttk.Button(bar, text="🔄 刷新设备", command=self.refresh_mon).pack(side="left")
        ttk.Button(bar, text="▶ 全部播放", command=self._mon_play_all).pack(side="left", padx=6)
        ttk.Button(bar, text="⏹ 全部停止", command=self._mon_stop_all).pack(side="left")
        self.mon_status = ttk.Label(bar, text="就绪", foreground="#666")
        self.mon_status.pack(side="right")

        self.mon_grid = ttk.Frame(f)
        self.mon_grid.pack(fill="both", expand=True)
        self.refresh_mon()

    def refresh_mon(self):
        """根据设备台账重建摄像头网格"""
        if not self.can_monitor:
            return
        self._mon_stop_all()
        for w in self.mon_grid.winfo_children():
            w.destroy()
        devs = self.store.list_devices(enabled_only=False)
        devs = [d for d in devs if getattr(d, "url", "")]
        if not devs:
            ttk.Label(self.mon_grid,
                      text="暂无带取流地址的设备，请在「设备台账」页添加机位和 URL。",
                      foreground="#888").pack(pady=30)
            return
        cols = 2
        for i, dev in enumerate(devs):
            room = dev.room_id or "未分配考场"
            name = dev.name or dev.code or "未命名"
            code = dev.code
            frame = ttk.LabelFrame(self.mon_grid, text=" %s · %s " % (room, name), padding=4)
            frame.grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)
            self.mon_grid.rowconfigure(i // cols, weight=1)
            self.mon_grid.columnconfigure(i % cols, weight=1)
            lbl = tk.Label(frame, bg="#000", relief="sunken")
            lbl.pack(fill="both", expand=True)
            dev._mon_label = lbl
            status = ttk.Label(frame, text="未播放", foreground="#666")
            status.pack(anchor="w")
            dev._mon_status = status
            bbar = ttk.Frame(frame)
            bbar.pack(fill="x", pady=(2, 0))
            dev._mon_btn = ttk.Button(bbar, text="▶ 播放",
                                      command=lambda c=code: self._cam_play(c))
            dev._mon_btn.pack(side="left")
            ttk.Button(bbar, text="⏹ 停止",
                       command=lambda c=code: self._cam_stop(c)).pack(side="left", padx=4)

    @staticmethod
    def _mon_try_open(url):
        """尝试多种后端打开视频流，返回 (cap, backend_name, info)"""
        backends = []
        if hasattr(cv2, "CAP_FFMPEG"):
            backends.append((cv2.CAP_FFMPEG, "FFMPEG"))
        if hasattr(cv2, "CAP_GSTREAMER"):
            backends.append((cv2.CAP_GSTREAMER, "GSTREAMER"))
        backends.append((0, "默认"))
        last_info = ""
        for backend, name in backends:
            try:
                cap = cv2.VideoCapture(url, backend) if backend else cv2.VideoCapture(url)
                if cap.isOpened():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        return cap, name, ""
                    last_info = "%s 后端能打开但读不到帧" % name
                    cap.release()
                else:
                    last_info = "%s 后端无法打开" % name
            except Exception as e:
                last_info = "%s 后端异常：%s" % (name, e)
        return None, "", last_info

    def _cam_play(self, code):
        dev = self._dev_by_code(code)
        if dev is None:
            return
        url = dev.url
        if not url or self._cam_running.get(code):
            return
        self._cam_stop(code)
        cap, backend, info = self._mon_try_open(url)
        if cap is None:
            self._cam_set_status(code, "打开失败：%s" % info, "#c00")
            return
        self._cam_caps[code] = cap
        self._cam_running[code] = True
        self._cam_set_status(code, "[%s后端] 缓冲中…" % backend, "#1f56b8")
        self._cam_btn_text(code, "播放中…")
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        t = threading.Thread(target=lambda: self._cam_loop(code, url), daemon=True)
        self._cam_threads[code] = t
        t.start()

    def _cam_loop(self, code, url):
        cap = self._cam_caps.get(code)
        while self._cam_running.get(code) and cap and cap.isOpened():
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            try:
                self.root.after(0, lambda f=frame, c=code: self._cam_update(c, f))
            except (tk.TclError, RuntimeError):
                break
            time.sleep(0.033)
        try:
            self.root.after(0, lambda c=code: self._cam_stopped_ui(c))
        except (tk.TclError, RuntimeError):
            pass

    def _cam_update(self, code, frame):
        dev = self._dev_by_code(code)
        if dev is None or not hasattr(dev, "_mon_label"):
            return
        lbl = dev._mon_label
        if lbl is None or not lbl.winfo_exists():
            return
        try:
            h = lbl.winfo_height()
            w = lbl.winfo_width()
            if h < 50:
                h = 240
            if w < 50:
                w = 320
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            img.thumbnail((w, h), Image.Resampling.LANCZOS)
            self._cam_photos[code] = ImageTk.PhotoImage(image=img)
            lbl.config(image=self._cam_photos[code])
            self._cam_set_status(code, "播放中", "#0a7d28")
        except Exception as e:
            self._cam_set_status(code, "渲染失败：%s" % e, "#c00")

    def _cam_stop(self, code):
        self._cam_running[code] = False
        cap = self._cam_caps.pop(code, None)
        if cap:
            try:
                cap.release()
            except Exception:
                pass
        self._cam_photos.pop(code, None)
        self._cam_stopped_ui(code)

    def _cam_stopped_ui(self, code):
        dev = self._dev_by_code(code)
        try:
            if dev is not None and hasattr(dev, "_mon_label") and dev._mon_label.winfo_exists():
                dev._mon_label.config(image="")
            self._cam_btn_text(code, "▶ 播放")
            self._cam_set_status(code, "已停止", "#666")
        except tk.TclError:
            pass

    def _dev_by_code(self, code):
        for d in self.store.list_devices(enabled_only=False):
            if d.code == code:
                return d
        return None

    def _cam_set_status(self, code, text, color):
        dev = self._dev_by_code(code)
        if dev is not None and hasattr(dev, "_mon_status") and dev._mon_status.winfo_exists():
            dev._mon_status.config(text=text, foreground=color)

    def _cam_btn_text(self, code, text):
        dev = self._dev_by_code(code)
        if dev is not None and hasattr(dev, "_mon_btn") and dev._mon_btn.winfo_exists():
            dev._mon_btn.config(text=text)

    def _mon_play_all(self):
        for d in self.store.list_devices(enabled_only=False):
            if d.code:
                self._cam_play(d.code)
        self.mon_status.config(text="已启动全部摄像头")

    def _mon_stop_all(self):
        codes = list(self._cam_running.keys())
        for code in codes:
            self._cam_stop(code)
        self.mon_status.config(text="已停止全部摄像头")


def _choose_role(parent):
    """选择要新建的角色（只能低于自己：owner 只能建 admin）"""
    win = tk.Toplevel(parent)
    win.title("选择角色")
    win.geometry("320x160")
    win.transient(parent)
    win.grab_set()
    ttk.Label(win, text="只能新建低于自己等级的账户：\n管理员（admin）").pack(padx=14, pady=(16, 8))
    box = {"v": None}

    def pick(role):
        box["v"] = role
        win.destroy()

    bar = ttk.Frame(win)
    bar.pack(pady=10)
    ttk.Button(bar, text="管理员（admin）", command=lambda: pick(web_accounts.ROLE_ADMIN)
               ).pack(side="left", padx=6)
    ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=6)
    parent.wait_window(win)
    return box["v"]


def wo_orders_text(st):
    return wo_mod.STATUS_TEXT.get(st, st)


def _fmt(v):
    return ("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else v


def _ask(parent, title, prompt):
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("380x150")
    win.transient(parent)
    win.grab_set()
    ttk.Label(win, text=prompt).pack(anchor="w", padx=12, pady=(14, 4))
    v = tk.StringVar()
    e = ttk.Entry(win, textvariable=v, width=36)
    e.pack(padx=12)
    e.focus_set()
    box = {"v": None}

    def ok():
        box["v"] = v.get().strip()
        win.destroy()
    bar = ttk.Frame(win)
    bar.pack(pady=12)
    ttk.Button(bar, text="确定", command=ok).pack(side="right", padx=8)
    ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
    e.bind("<Return>", lambda _e: ok())
    parent.wait_window(win)
    return box["v"]


def main():
    root = tk.Tk()
    try:
        App(root)
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
