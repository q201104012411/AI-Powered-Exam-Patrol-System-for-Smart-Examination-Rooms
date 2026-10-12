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
import inspect
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import winsound
    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

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
import camera_stream
import diagnose
import models
import probe
import storage
import web_accounts
import baseline as baseline_mod
import patrol as patrol_mod
import person_detect as pd_mod
import report as report_mod
import workorder as wo_mod

FONT = ("Microsoft YaHei", 10)
MONO = ("Consolas", 10)
FONT_SMALL = ("Microsoft YaHei", 9)
# 统一视觉常量：标题字体 / 内边距 / 灰阶 / 摄像头状态色
FONT_TITLE = ("Microsoft YaHei", 14, "bold")
PAD_TAB = 12            # 各页签内边距
PAD_BAR = 6             # 工具栏与内容的纵向间距
PAD_BTN = 6             # 按钮之间的横向间距
GRAY_MUTED = "#888"     # 日志/占位/次要文字统一灰
GRAY_HINT = "#666"      # 状态栏灰
# 摄像头状态 -> 颜色（未播放灰 / 缓冲中蓝 / 播放中绿 / 画面中断橙 / 失败红）
CAM_COLORS = {
    "idle": "#888",        # 未播放 / 已停止
    "buffering": "#1f56b8",  # 缓冲中
    "playing": "#0a7d28",    # 播放中
    "stalled": "#c07000",    # 画面中断（正在自动重连）
    "error": "#c00",         # 打开失败 / 渲染失败
}
CAM_AI_COLOR = "#c07000"     # AI 识别到越界时的橙色
# 面板 AI 行文本：AI 识别中：N 人[，越界 M]（全局汇总从这里反解）
CAM_AI_RE = re.compile(r"(\d+) 人(?:，越界 (\d+))?")

# 密度等级：按每百万像素人数划分（与 AI 子Agent 新增 dense_mode 对应）
DENSITY_LOW = "low"
DENSITY_MED = "medium"
DENSITY_HIGH = "high"
DENSITY_TEXT = {DENSITY_LOW: "低", DENSITY_MED: "中", DENSITY_HIGH: "高"}
DENSITY_COLOR = {DENSITY_LOW: "#0a7d28", DENSITY_MED: "#1f56b8", DENSITY_HIGH: "#e67e00"}

# AI 设置默认配置
AI_SETTINGS_PATH = config.DATA_ROOT / "ai_settings.json"
AI_SETTINGS_DEFAULT = {
    "mode": "standard",          # standard / dense / energy
    "box_margin_pct": 2,         # 1 / 2 / 5 / 10
    "haar_face": True,
    "sound_beep": True,
}

# pythonw 隐藏窗口启动时 stdout/stderr 为 None，兜住避免任何 print 崩掉界面
config.ensure_stdio("gui_console.log")

# 隐藏命令行窗口标记（所有者可切换；.bat 启动时读它决定用 python 还是 pythonw）
HIDE_CMD_FLAG = str(config.HIDE_CMD_FLAG)


class _BehaviorTracker(object):
    """逐人异常行为计数：同一人同一行为连续达到阈值且冷却完成后才向 03 发告警"""
    THRESHOLD = 20          # 连续 20 帧（约 0.7 秒）视为一次有效异常
    COOLDOWN_SEC = 30       # 同一人同一行为 30 秒内只报一次

    def __init__(self):
        # key: (code, pid, behavior) -> {"count": int, "last_alert": float}
        self._state = {}

    def update(self, code, persons):
        """persons: [{"pid", "behaviors", "crosses"}]，返回 [{pid, behavior, crosses}]"""
        now = time.time()
        seen = set()
        triggered = []
        for p in persons:
            pid = p.get("pid", 0)
            for b in p.get("behaviors", ()):
                key = (code, pid, b)
                seen.add(key)
                s = self._state.get(key)
                if s is None:
                    s = {"count": 0, "last_alert": 0}
                    self._state[key] = s
                s["count"] += 1
                if s["count"] >= self.THRESHOLD \
                        and (now - s["last_alert"]) > self.COOLDOWN_SEC:
                    s["last_alert"] = now
                    triggered.append({"pid": pid, "behavior": b,
                                      "crosses": p.get("crosses", 0)})
        # 当前帧没出现的 (code,pid,behavior)，计数清零
        for key, s in list(self._state.items()):
            if key[0] == code and key not in seen:
                s["count"] = 0
        return triggered


class App(object):
    def __init__(self, root):
        self.root = root
        self.root.title("摄像头核查与视频质量诊断")
        self.root.geometry("1280x820")
        self.root.minsize(1024, 700)
        self.store = storage.Store()
        self.accounts = web_accounts.Accounts(self.store)
        self.user = None                     # web_accounts.Session
        self.baselines = baseline_mod.BaselineStore(self.store)
        self.orders = wo_mod.WorkOrderManager(self.store)
        self.runner = patrol_mod.PatrolRunner(self.store, baseline_store=self.baselines)
        self.reporter = report_mod.Reporter(self.store, runner=self.runner, orders=self.orders)
        # 关窗口 = 停止所有取流（否则后台读线程/网络连接会拖住进程退出）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_login()                  # 先登录，登录成功后原地切主界面

    def _on_close(self):
        """退出前尽力收尾：停取流、取消定时任务，然后销毁窗口。

        注意：可能卡在网络 read 里的线程是 daemon，进程退出时自然结束，
        所以这里**不做任何 join/wait**，保证"点关闭立刻关掉"。
        """
        try:
            for after_id in list(getattr(self, "_cam_render_after", {}).values()):
                try:
                    self.root.after_cancel(after_id)
                except (tk.TclError, ValueError):
                    pass
            self._cam_render_after.clear()
        except Exception:
            pass
        for reader in list(getattr(self, "_cam_readers", {}).values()):
            try:
                reader.stop("程序退出")
            except Exception:
                pass
        try:
            self._cam_readers.clear()
        except Exception:
            pass
        self.root.destroy()

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
                                   font=FONT)
        self.login_msg.pack(pady=(12, 2))

        bar = ttk.Frame(box)
        bar.pack(pady=6)
        ttk.Button(bar, text="登  录", width=12, command=self._do_login).pack(side="left", padx=8)
        ttk.Button(bar, text="退  出", width=12, command=self.root.destroy).pack(side="left", padx=8)

        ttk.Label(box, text="账户与网页版（http://127.0.0.1:5010/）是同一份",
                  foreground=GRAY_MUTED, font=FONT_SMALL).pack(pady=(16, 0))
        ttk.Label(box, text="默认账户：owner（所有者）、teacher1（管理员）　口令由系统管理员掌握，不在界面显示",
                  foreground=GRAY_MUTED, font=FONT_SMALL).pack()
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
        self.root.geometry("1280x820")

        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="摄像头核查与视频质量诊断",
                  font=FONT_TITLE).pack(side="left")
        ttk.Label(top, text="当前账号：%s　角色：%s"
                  % (self.user.username, self.user.role_text),
                  foreground="#1f56b8").pack(side="left", padx=(14, 0))

        ttk.Button(top, text="退出", command=self._logout).pack(side="right")
        ttk.Button(top, text="🔄 切换账号", command=self._switch_account
                   ).pack(side="right", padx=PAD_BTN)
        ttk.Button(top, text="🔑 修改口令", command=self._change_own_pwd
                   ).pack(side="right", padx=PAD_BTN)
        if self.can("account_manage"):
            ttk.Button(top, text="👥 终端账户", command=self._account_manager
                       ).pack(side="right", padx=PAD_BTN)
        else:
            b = ttk.Button(top, text="👥 终端账户（仅所有者）", state="disabled")
            b.pack(side="right", padx=PAD_BTN)
        if self.can("audit_view"):
            ttk.Button(top, text="🔐 审计链", command=self._show_audit
                       ).pack(side="right", padx=PAD_BTN)
        else:
            ttk.Button(top, text="🔐 审计链（仅所有者）", state="disabled"
                       ).pack(side="right", padx=PAD_BTN)

        # 隐藏命令行窗口（仅所有者可切换；管理员勾选框直接禁用）
        self.hide_cmd = tk.BooleanVar(value=web_accounts.hidden_cmd_on())
        cb = ttk.Checkbutton(top, text="隐藏命令行窗口（下次启动生效）",
                             variable=self.hide_cmd, command=self._toggle_hide_cmd)
        cb.pack(side="right", padx=(PAD_BTN, 12))
        if not self.can("hide_cmd_toggle"):
            cb.config(state="disabled")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=PAD_TAB, pady=(0, PAD_BAR))
        self.t_dev = ttk.Frame(nb, padding=PAD_TAB)
        self.t_chk = ttk.Frame(nb, padding=PAD_TAB)
        self.t_wo = ttk.Frame(nb, padding=PAD_TAB)
        self.t_rep = ttk.Frame(nb, padding=PAD_TAB)
        # 实时监控：仅所有者和管理员可见
        self.can_monitor = self.user.role in (web_accounts.ROLE_OWNER,
                                              web_accounts.ROLE_ADMIN)
        if self.can_monitor:
            self.t_mon = ttk.Frame(nb, padding=PAD_TAB)
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
                                foreground=GRAY_HINT)
        self.status.pack(fill="x", padx=PAD_TAB, pady=(0, PAD_BAR))
        self.refresh_all()

    # ---------------- 设备台账 ----------------
    def _dev_tab(self):
        f = self.t_dev
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, PAD_BAR))
        ttk.Button(bar, text="＋ 新增机位",
                   command=self._guard("device_add", self._add_device)).pack(side="left")
        ttk.Button(bar, text="📷 采集基准图",
                   command=self._guard("baseline_capture", self._capture)
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_devices
                   ).pack(side="left", padx=PAD_BTN)
        if self.can("device_delete"):
            ttk.Button(bar, text="🗑 删除机位", command=self._delete_device
                       ).pack(side="left", padx=PAD_BTN)
        else:
            ttk.Button(bar, text="🗑 删除机位（仅所有者）", state="disabled"
                       ).pack(side="left", padx=PAD_BTN)
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
        bar.pack(fill="x", pady=(0, PAD_BAR))
        ttk.Button(bar, text="▶ 跑一轮日常巡检",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_DAILY))
                   ).pack(side="left")
        ttk.Button(bar, text="考前专项核查",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_PREEXAM))
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="考前一小时自检",
                   command=self._guard("patrol_run",
                                       lambda: self._run(config.POLICY_BEFORE))
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_checks
                   ).pack(side="left", padx=PAD_BTN)
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
        self.detail.pack(fill="x", pady=(PAD_BAR, 0))

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
        bar.pack(fill="x", pady=(0, PAD_BAR))
        for text, act in (("派单", "dispatch"), ("提交整改", "rectify"),
                          ("复核通过", "review_ok"), ("复核打回", "review_no")):
            ttk.Button(bar, text=text,
                       command=self._guard("workorder_operate",
                                           lambda a=act: self._wo_act(a))
                       ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_orders
                   ).pack(side="left", padx=PAD_BTN)
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
        bar.pack(fill="x", pady=(0, PAD_BAR))
        ttk.Button(bar, text="生成并导出报告",
                   command=self._guard("report_export", self._export)).pack(side="left")
        ttk.Button(bar, text="🔄 刷新指标", command=self.refresh_report
                   ).pack(side="left", padx=PAD_BTN)
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
        ttk.Button(bar, text="＋ 新建账户", command=new_account
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🔑 重置口令", command=reset_pwd
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🗑 删除账户", command=del_account
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="🔄 刷新", command=reload).pack(side="left", padx=PAD_BTN)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=PAD_BTN)
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

    # ---------------- AI 设置（全局） ----------------
    def _load_ai_settings(self):
        """从 data/ai_settings.json 读取 AI 设置，缺失字段用默认值补齐"""
        try:
            if AI_SETTINGS_PATH.exists():
                data = json.loads(AI_SETTINGS_PATH.read_text(encoding="utf-8"))
            else:
                data = {}
        except Exception:
            data = {}
        settings = dict(AI_SETTINGS_DEFAULT)
        settings.update(data)
        return settings

    def _save_ai_settings(self, settings):
        """保存 AI 设置到 data/ai_settings.json"""
        try:
            AI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            AI_SETTINGS_PATH.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return False

    def _ai_margin_ratio(self):
        """当前框边距百分比 -> 小数"""
        return self._ai_settings.get("box_margin_pct", 2) / 100.0

    def _ai_tracker_kwargs(self):
        """构造 PersonTracker 初始化参数，兼容当前与 AI 优化子Agent 新增参数"""
        sig = inspect.signature(pd_mod.PersonTracker.__init__)
        params = sig.parameters
        kw = {"margin_ratio": self._ai_margin_ratio()}
        settings = self._ai_settings
        # AI 子Agent 预期新增参数（按实际 person_detect.py 命名）
        for key, val in (("dense_mode", settings.get("mode") == "dense"),
                         ("max_input_dim", 1280 if settings.get("mode") == "energy" else 1920),
                         ("detect_stride", 2 if settings.get("mode") == "energy" else 1),
                         ("enable_face_dense", settings.get("haar_face", True))):
            if key in params:
                kw[key] = val
        return kw

    def _open_ai_settings(self):
        """全局 AI 设置弹窗"""
        win = tk.Toplevel(self.root)
        win.title("AI 设置")
        win.geometry("420x320")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)

        settings = self._load_ai_settings()

        ttk.Label(f, text="检测模式：").grid(row=0, column=0, sticky="e", pady=6)
        mode_var = tk.StringVar(value=settings.get("mode", "standard"))
        ttk.Combobox(f, textvariable=mode_var, state="readonly", width=18,
                     values=["standard", "dense", "energy"]).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="标准：常规考场；密集：40人考场；节能：隔帧检测", foreground="#888",
                  font=FONT_SMALL).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(f, text="框边距：").grid(row=1, column=0, sticky="e", pady=6)
        margin_var = tk.IntVar(value=settings.get("box_margin_pct", 2))
        margin_bar = ttk.Frame(f)
        margin_bar.grid(row=1, column=1, sticky="w")
        for pct in (1, 2, 5, 10):
            ttk.Radiobutton(margin_bar, text="%d%%" % pct, variable=margin_var,
                            value=pct).pack(side="left", padx=(0, 8))

        ttk.Label(f, text="Haar 人脸微表情：").grid(row=2, column=0, sticky="e", pady=6)
        haar_var = tk.BooleanVar(value=settings.get("haar_face", True))
        ttk.Checkbutton(f, text="启用", variable=haar_var).grid(row=2, column=1, sticky="w")

        ttk.Label(f, text="异常声音提示：").grid(row=3, column=0, sticky="e", pady=6)
        sound_var = tk.BooleanVar(value=settings.get("sound_beep", True))
        ttk.Checkbutton(f, text="启用（异常行为时 beep）", variable=sound_var).grid(row=3, column=1, sticky="w")

        msg = ttk.Label(f, text="", foreground="#c00")
        msg.grid(row=4, column=0, columnspan=3, pady=(10, 0))

        def apply():
            new_settings = {
                "mode": mode_var.get(),
                "box_margin_pct": margin_var.get(),
                "haar_face": haar_var.get(),
                "sound_beep": sound_var.get(),
            }
            if self._save_ai_settings(new_settings):
                self._ai_settings = new_settings
                # 边距实时生效：重建检测器（下次 _cam_update 会按新设置创建）
                self._person_detector = None
                msg.config(text="已保存并生效", foreground="#0a7d28")
                self.status.config(text="AI 设置已更新：模式=%s 边距=%d%%" % (
                    new_settings["mode"], new_settings["box_margin_pct"]))
                self._mon_log_line("AI 设置更新：%s" % new_settings)

        bbar = ttk.Frame(f)
        bbar.grid(row=5, column=0, columnspan=3, pady=(16, 0))
        ttk.Button(bbar, text="保存", command=apply).pack(side="left", padx=6)
        ttk.Button(bbar, text="关闭", command=win.destroy).pack(side="left", padx=6)

    def _beep(self):
        """异常时声音提示（优先 winsound，否则 tkinter bell）"""
        if not self._ai_settings.get("sound_beep", True):
            return
        try:
            if HAS_WINSOUND:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            else:
                self.root.bell()
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

    # ---------------- 密度 / 提示 / 边框 / 信息条 辅助 ----------------
    @staticmethod
    def _density_level(persons, frame):
        """按每百万像素人数返回密度等级 low/medium/high"""
        if frame is None or not persons:
            return DENSITY_LOW
        h, w = frame.shape[:2]
        mp = max(h * w / 1_000_000.0, 0.01)
        d = len(persons) / mp
        if d >= 30:
            return DENSITY_HIGH
        if d >= 15:
            return DENSITY_MED
        return DENSITY_LOW

    def _cam_bind_tooltip(self, widget, code):
        """给画面控件绑定悬停提示"""
        tip = None

        def enter(event):
            nonlocal tip
            self._cam_hide_tooltip(tip)
            tip = self._cam_show_tooltip(code, event.x_root, event.y_root)

        def leave(event):
            nonlocal tip
            self._cam_hide_tooltip(tip)
            tip = None

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
        widget.bind("<Motion>", lambda e: None if tip is None else tip.geometry("+%d+%d" % (e.x_root + 16, e.y_root + 16)))

    def _cam_show_tooltip(self, code, x, y):
        """显示一路摄像头的悬停提示"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return None
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry("+%d+%d" % (x + 16, y + 16))
        f = ttk.Frame(win, padding=6, relief="solid", borderwidth=1)
        f.pack()
        persons = panel.get("persons", []) or []
        level = panel.get("density_level") or DENSITY_LOW
        lines = ["路数：%s" % code,
                 "人数：%d" % len(persons),
                 "密度：%s" % DENSITY_TEXT.get(level, level)]
        if persons:
            behaviors = []
            for p in persons:
                for b in p.get("behaviors", []):
                    behaviors.append("#%d %s" % (p.get("pid", 0), b))
            if behaviors:
                lines.append("异常：" + "、".join(behaviors[:6]))
        txt = "\n".join(lines)
        lbl = ttk.Label(f, text=txt, font=FONT_SMALL, background="#ffffe0",
                        foreground="#333", justify="left")
        lbl.pack()
        return win

    def _cam_hide_tooltip(self, tip):
        if tip is not None and tip.winfo_exists():
            try:
                tip.destroy()
            except tk.TclError:
                pass

    def _cam_set_border(self, code, state):
        """根据状态设置面板边框颜色：正常绿 / 异常橙 / 越界红 / 未播放灰"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        frame = panel.get("frame")
        if frame is None or not frame.winfo_exists():
            return
        color = CAM_COLORS.get(state, CAM_COLORS["idle"])
        try:
            frame.config(highlightbackground=color)
        except tk.TclError:
            pass

    def _cam_update_info_bar(self):
        """刷新顶部信息条：播放路数/总路数、AI人数/越界/异常、高密度路数、最近异常"""
        total = len(self._mon_panels)
        playing = sum(1 for v in self._cam_running.values() if v)
        ai_total = 0
        cross_total = 0
        abnormal_total = 0
        high_count = 0
        for panel in self._mon_panels.values():
            persons = panel.get("persons", []) or []
            ai_total += len(persons)
            cross_total += sum(1 for p in persons if p.get("bad"))
            abnormal_total += sum(len(p.get("behaviors", [])) for p in persons)
            if panel.get("density_level") == DENSITY_HIGH:
                high_count += 1
        recent = []
        for item in list(self._behavior_history)[-3:]:
            recent.append("%s %s：%s" % (item["ts"][-8:], item["code"], item["text"]))
        text = ("播放 %d/%d 路　｜　AI 总人数 %d　越界 %d　异常行为 %d　｜"
                "　高密度机位 %d 路" % (playing, total, ai_total, cross_total,
                                      abnormal_total, high_count))
        if recent:
            text += "　｜　最近异常：" + "　".join(recent)
        try:
            self.info_label.config(text=text)
        except tk.TclError:
            pass

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
                      foreground=CAM_COLORS["error"]).pack(anchor="w", pady=10)
            return

        self._cam_readers = {}      # code -> camera_stream.FrameReader（取流内核）
        self._cam_running = {}      # code -> bool
        self._cam_photos = {}
        self._cam_latest = {}       # code -> 最新帧（用于放大窗口）
        self._cam_seen = {}         # code -> (generation, seq) 已渲染过的帧序号
        self._cam_render_after = {}  # code -> after id（渲染循环，避免重复排程）
        self._mon_log = config.LOG_DIR / "monitor_ui.log"
        self._ai_settings = self._load_ai_settings()
        self._behavior_history = []  # 最近异常行为摘要，每项 {"ts", "code", "text"}

        # ---- 工具栏：左=操作按钮组，中=AI 开关+设置，右=状态标签 ----
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(PAD_BAR, 0))
        left = ttk.Frame(bar)
        left.pack(side="left")
        ttk.Button(left, text="🔄 刷新设备", command=self.refresh_mon).pack(side="left", padx=PAD_BTN)
        ttk.Button(left, text="▶ 全部播放", command=self._mon_play_all
                   ).pack(side="left", padx=PAD_BTN)
        ttk.Button(left, text="⏹ 全部停止", command=self._mon_stop_all).pack(side="left", padx=PAD_BTN)
        ttk.Button(left, text="⚙ AI 设置", command=self._open_ai_settings
                   ).pack(side="left", padx=PAD_BTN)

        mid = ttk.Frame(bar)
        mid.pack(side="left", padx=(18, 12))
        self.mon_ai = tk.BooleanVar(value=True)     # 考场监控默认开启 AI 辅助识别
        self.mon_ai_chk = ttk.Checkbutton(
            mid, text="AI 辅助识别（逐人：绿=正常 橙=异常 红=越界，考场监控默认开启）",
            variable=self.mon_ai)
        self.mon_ai_chk.pack(side="left")

        self.mon_status = ttk.Label(bar, text="就绪", foreground=GRAY_HINT)
        self.mon_status.pack(side="right")

        # ---- 顶部信息条：全局状态一目了然 ----
        self.info_bar = ttk.LabelFrame(f, text=" 实时状态 ", padding=6)
        self.info_bar.pack(fill="x", pady=(PAD_BAR, PAD_BAR))
        self.info_label = ttk.Label(self.info_bar, text="暂无摄像头",
                                    font=FONT_SMALL, foreground="#333")
        self.info_label.pack(anchor="w")

        # ---- 图例：颜色含义一目了然 ----
        ttk.Label(f, text="图例：绿框=行为正常　橙框=异常行为　红框=越界/贴线　"
                          "青色小框=人脸　边框：绿=正常　橙=异常　红=越界　灰=未播放",
                  font=FONT_SMALL, foreground="#555").pack(anchor="w", pady=(4, PAD_BAR))

        self._person_detector = None
        self._ai_error_file = config.DATA_ROOT / "ai_detect_error.txt"
        self._behavior_tracker = _BehaviorTracker()

        # ---- 可滚动摄像头网格（Canvas + 竖滚动条 + 鼠标滚轮） ----
        self.mon_canvas, self.mon_grid = self._make_scroll_area(f)
        self._mon_panels = {}       # code -> {label, status, ai, btn, ...}
        self.refresh_mon()
        # 页面打开后自动播放全部摄像头（延迟一点让 UI 先完成布局）
        self.root.after(300, self._mon_play_all)

    def _make_scroll_area(self, parent):
        """可滚动容器：Canvas + 竖向滚动条 + 滚轮绑定，返回 (canvas, 内容 Frame)"""
        canvas = tk.Canvas(parent, highlightthickness=0, bg="#f4f4f4",
                           yscrollincrement=30)
        canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        sb.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=sb.set)
        grid = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>",
                  lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        # 内容宽度跟随 Canvas，保证两列摄像头随窗口一起变宽
        canvas.bind("<Configure>",
                    lambda e, c=canvas, w=window: c.itemconfigure(w, width=e.width))
        self._bind_mousewheel(canvas, recurse=False)
        return canvas, grid

    def _on_mousewheel(self, event):
        """滚轮：滚动实时监控页的摄像头网格"""
        canvas = getattr(self, "mon_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return "break"
        # Windows 滚轮事件 delta 是 120 的倍数
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _bind_mousewheel(self, widget, recurse=True):
        """给可滚动控件（默认含其全部子控件）绑定鼠标滚轮"""
        try:
            # 不用 add="+"：重复绑定会叠加处理器，导致滚轮一次滚多行
            widget.bind("<MouseWheel>", self._on_mousewheel)
        except tk.TclError:
            return
        if recurse:
            for child in widget.winfo_children():
                self._bind_mousewheel(child)

    def _mon_bind_wheel(self):
        """网格重建后重新绑定滚轮（Canvas 与网格内容各绑一次，避免重复滚动）"""
        canvas = getattr(self, "mon_canvas", None)
        if canvas is None:
            return
        self._bind_mousewheel(canvas, recurse=False)
        grid = getattr(self, "mon_grid", None)
        if grid is not None:
            self._bind_mousewheel(grid)

    def refresh_mon(self):
        """根据设备台账重建摄像头网格"""
        if not self.can_monitor:
            return
        # 先彻底停掉取流与渲染循环，避免重建网格后残留线程往已销毁的控件贴图
        self._mon_stop_all()
        self._cam_readers.clear()
        self._cam_seen.clear()
        self._cam_latest.clear()
        self._mon_panels.clear()
        for w in self.mon_grid.winfo_children():
            w.destroy()
        devs = self.store.list_devices(enabled_only=False)
        devs = [d for d in devs if getattr(d, "url", "")]
        if not devs:
            ttk.Label(self.mon_grid,
                      text="暂无带取流地址的设备，请在「设备台账」页添加机位和 URL。",
                      foreground=GRAY_MUTED).pack(pady=30)
            self._mon_bind_wheel()
            self.mon_status.config(text="暂无摄像头", foreground=GRAY_HINT)
            return
        cols = 2
        for i, dev in enumerate(devs):
            room = dev.room_id or "未分配考场"
            name = dev.name or dev.code or "未命名"
            code = dev.code
            # tk.LabelFrame 支持 highlightbackground，可做状态色边框
            frame = tk.LabelFrame(self.mon_grid, text=" %s · %s " % (room, name),
                                  bg="#f9f9f9", fg="#333", font=FONT_SMALL,
                                  highlightthickness=2, highlightbackground=CAM_COLORS["idle"])
            frame.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            self.mon_grid.rowconfigure(i // cols, weight=1)
            self.mon_grid.columnconfigure(i % cols, weight=1)
            # 黑底画面区：先给一个初始高度，播放时按真实尺寸贴图
            lbl = tk.Label(frame, bg="#000", relief="sunken", cursor="hand2",
                           height=9)
            lbl.pack(fill="both", expand=True, padx=2, pady=2)
            lbl.bind("<Button-1>", lambda _e, c=code: self._cam_enlarge(c))
            # 状态行：播放状态 + 密度等级 + AI 检测人数
            srow = ttk.Frame(frame)
            srow.pack(fill="x", pady=(2, 0), padx=2)
            status = ttk.Label(srow, text="未播放", foreground=CAM_COLORS["idle"],
                               font=FONT_SMALL)
            status.pack(side="left")
            density = ttk.Label(srow, text="密度：-", foreground=GRAY_MUTED,
                                font=FONT_SMALL)
            density.pack(side="left", padx=(8, 0))
            ai = ttk.Label(srow, text="", foreground=GRAY_MUTED, font=FONT_SMALL)
            ai.pack(side="right")
            bbar = ttk.Frame(frame)
            bbar.pack(fill="x", pady=(4, 2), padx=2)
            btn = ttk.Button(bbar, text="▶ 播放",
                             command=lambda c=code: self._cam_play(c))
            btn.pack(side="left", padx=(0, PAD_BTN), pady=(2, 2))
            stop = ttk.Button(bbar, text="⏹ 停止",
                              command=lambda c=code: self._cam_stop(c))
            stop.pack(side="left", padx=(0, PAD_BTN), pady=(2, 2))
            big = ttk.Button(bbar, text="🔍 放大",
                             command=lambda c=code: self._cam_enlarge(c))
            big.pack(side="left", padx=(0, PAD_BTN), pady=(2, 2))
            self._mon_panels[code] = {"label": lbl, "status": status, "ai": ai,
                                      "density": density, "btn": btn,
                                      "stop": stop, "big": big, "frame": frame,
                                      "persons": [], "density_level": None,
                                      "history": []}
            # 悬停提示
            self._cam_bind_tooltip(lbl, code)
            # 点标题/面板空白处同样可以放大（子控件的点击会冒泡到 LabelFrame）
            for w in (frame, lbl, srow, status, ai, density):
                w.bind("<Button-1>", lambda _e, c=code: self._cam_enlarge(c))
        self._mon_bind_wheel()
        self._mon_ai_summary()

    def _cam_play(self, code):
        """开始播放一路摄像头。

        **绝不在界面线程里打开视频流**：历史上这里的 cv2.VideoCapture + cap.read()
        会依次试 FFMPEG/GSTREAMER/默认三个后端，网络不通时把界面冻死。
        现在只创建一个 FrameReader（后台线程负责打开+读帧+卡死重连），界面线程立刻返回。
        """
        dev = self._dev_by_code(code)
        if dev is None:
            return
        url = dev.url
        if not url or self._cam_running.get(code):
            return
        self._cam_stop(code, quiet=True)
        reader = camera_stream.FrameReader(url, label="%s/%s" % (code, camera_stream.short_url(url, 32)))
        self._cam_readers[code] = reader
        self._cam_running[code] = True
        self._cam_seen.pop(code, None)
        self._cam_latest.pop(code, None)
        self._cam_set_status(code, "正在连接…", CAM_COLORS["buffering"])
        self._cam_btn_text(code, "连接中…")
        reader.start()
        self._cam_poll(code)
        self._cam_render(code)
        self._mon_log_line("播放 %s：%s" % (code, url))

    def _cam_poll(self, code):
        """每 500ms 看一次取流状态，把状态如实显示到界面（这一步也不阻塞）。"""
        reader = self._cam_readers.get(code)
        if reader is None or not self._cam_running.get(code):
            return
        snap = reader.snapshot()
        state = snap["state"]
        if state == "live":
            if snap["seq"]:
                self._cam_set_status(code, "播放中 ｜ %.1f fps" % snap["fps"],
                                     CAM_COLORS["playing"])
            else:
                self._cam_set_status(code, "已连接，等待画面…", CAM_COLORS["buffering"])
        elif state == "connecting":
            self._cam_set_status(code, snap["status"] or "正在连接…",
                                 CAM_COLORS["buffering"])
        elif state == "stalled":
            self._cam_set_status(code, snap["status"], CAM_COLORS["stalled"])
            self._cam_set_placeholder(code, "画面中断，正在自动重连…\n%s"
                                      % snap["status"])
        elif state in ("error", "stopped"):
            self._cam_set_status(code, snap["status"] or "取流已停止", CAM_COLORS["error"])
            if state == "error":
                self._cam_set_placeholder(code, "无法取流\n%s" % snap["status"])
                self._cam_btn_text(code, "▶ 重试")
        try:
            self.root.after(500, lambda c=code: self._cam_poll(c))
        except (tk.TclError, RuntimeError):
            pass

    def _cam_render(self, code):
        """渲染循环：从 FrameReader 取最新帧贴图（非阻塞）。

        关键点：帧序号 (generation, seq) 没变就**不重复渲染**，因此"画面中断"
        时界面不会死转；同时这个循环是 after 排程的，不会出现多份循环叠加。
        """
        try:
            reader = self._cam_readers.get(code)
            if reader is None or not self._cam_running.get(code):
                return
            snap = reader.snapshot()
            key = (snap["generation"], snap["seq"])
            if snap["frame"] is not None and snap["seq"] and self._cam_seen.get(code) != key:
                self._cam_seen[code] = key
                self._cam_update(code, snap["frame"])
        except Exception as e:                       # noqa: BLE001
            self._cam_set_status(code, "渲染失败：%s" % str(e)[:60], CAM_COLORS["error"])
        self._cam_render_after[code] = self.root.after(
            33, lambda c=code: self._cam_render(c))

    def _mon_log_line(self, text):
        """监控操作日志（取流问题事后可查；写失败不影响运行）"""
        try:
            with open(str(self._mon_log), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    def _cam_update(self, code, frame):
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        lbl = panel.get("label")
        if lbl is None or not lbl.winfo_exists():
            return
        try:
            self._cam_latest[code] = frame.copy()
            viol = 0
            persons = []
            # AI 越界检测：勾选了才跑，避免不必要的 CPU 占用
            if getattr(self, "mon_ai", None) and self.mon_ai.get():
                if self._person_detector is None:
                    try:
                        self._person_detector = pd_mod.PersonTracker(
                            **self._ai_tracker_kwargs())
                    except TypeError:
                        # 兼容当前尚未升级 PersonTracker 的场景
                        self._person_detector = pd_mod.PersonDetector(
                            margin_ratio=self._ai_margin_ratio())
                    except Exception as e:
                        err = str(e)
                        self.mon_ai.set(False)
                        self.mon_ai_chk.config(state="disabled")
                        self.mon_status.config(text="AI检测不可用：%s" % err,
                                               foreground=CAM_COLORS["error"])
                        try:
                            self._ai_error_file.write_text(err, encoding="utf-8")
                        except Exception:
                            pass
                if self._person_detector is not None:
                    frame, viol, persons = self._person_detector.draw(frame)
                    # 密度等级
                    level = self._density_level(persons, frame)
                    panel["density_level"] = level
                    panel["persons"] = persons
                    self._cam_set_density(code, level)
                    # 状态边框：越界红 > 异常橙 > 正常绿
                    if viol:
                        self._cam_set_border(code, "error")
                    elif any(p.get("behaviors") for p in persons):
                        self._cam_set_border(code, "stalled")
                    else:
                        self._cam_set_border(code, "playing")
                    # 逐路显示识别人数（每路独立，避免与全局状态重复）
                    self._cam_set_ai(code, len(persons), viol)
                    # 全局状态只给汇总，避免"最新一路"覆盖成误导信息
                    self._mon_ai_summary()
                    self._cam_update_info_bar()
                    # 逐人多次异常行为上报 03 智能终端 + 历史摘要
                    alerts = self._behavior_tracker.update(code, persons)
                    if alerts:
                        self._beep()
                        dev = self._dev_by_code(code)
                        for a in alerts:
                            text = "#%d %s" % (a["pid"], a["behavior"])
                            if a["crosses"] >= 2:
                                text += "（触线%d次）" % a["crosses"]
                            self._behavior_history.append({
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "code": code, "text": text})
                            # 保持最近 50 条，界面只展示 3 条
                            self._behavior_history = self._behavior_history[-50:]
                            # 放大窗口历史时间线
                            panel["history"] = (panel.get("history", []) + [{
                                "ts": time.strftime("%H:%M:%S"),
                                "text": text}])[-10:]
                            try:
                                import linkage_bridge
                                linkage_bridge.report_behavior(
                                    device_code=code,
                                    behavior=a["behavior"],
                                    pid=a["pid"],
                                    crosses=a["crosses"],
                                    room_id=dev.room_id if dev else "",
                                    seat_no="",
                                    operator=self.user.username if self.user else "02_ai")
                            except Exception:
                                pass
            else:
                # AI 未开启时恢复绿色边框
                self._cam_set_border(code, "playing")
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
            # 播放状态由 _cam_poll 统一维护（含 fps / 画面中断提示），这里不覆盖
        except Exception as e:
            self._cam_set_status(code, "渲染失败：%s" % e, CAM_COLORS["error"])

    def _cam_set_density(self, code, level):
        """更新单路密度显示"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        density = panel.get("density")
        if density is None or not density.winfo_exists():
            return
        try:
            density.config(text="密度：%s" % DENSITY_TEXT.get(level, level),
                           foreground=DENSITY_COLOR.get(level, GRAY_MUTED))
        except tk.TclError:
            pass

    def _cam_set_ai(self, code, n_person, n_cross):
        """逐路显示 AI 识别人数：正常绿、有越界橙红，未开 AI 时留空"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        ai = panel.get("ai")
        if ai is None or not ai.winfo_exists():
            return
        if n_cross:
            text, color = "AI 识别中：%d 人，越界 %d" % (n_person, n_cross), CAM_AI_COLOR
        else:
            text, color = "AI 识别中：%d 人" % n_person, CAM_COLORS["playing"]
        try:
            ai.config(text=text, foreground=color)
        except tk.TclError:
            pass

    def _mon_ai_summary(self):
        """全局状态标签：汇总已开启 AI 的机位人数、越界数、异常行为数、高密度机位数"""
        total_p, total_c, n = 0, 0, 0
        abnormal = 0
        high = 0
        for panel in self._mon_panels.values():
            ai = panel.get("ai")
            try:
                text = ai.cget("text") if ai is not None else ""
            except tk.TclError:
                continue
            m = CAM_AI_RE.search(text or "")
            if m is None:
                continue
            n += 1
            total_p += int(m.group(1))
            if m.group(2):
                total_c += int(m.group(2))
            for p in panel.get("persons", []) or []:
                abnormal += len(p.get("behaviors", []))
            if panel.get("density_level") == DENSITY_HIGH:
                high += 1
        if n:
            self.mon_status.config(text="AI：%d 路　%d 人　越界 %d　异常 %d　高密度 %d"
                                        % (n, total_p, total_c, abnormal, high),
                                   foreground=GRAY_HINT)
        elif any(self._cam_running.values()):
            self.mon_status.config(text="AI 待命（画面播放中，尚未产出识别结果）",
                                   foreground=GRAY_HINT)
        else:
            self.mon_status.config(text="就绪（摄像头未播放）", foreground=GRAY_HINT)
        return n

    def _mon_set_status(self, text):
        """设全局状态：正在播放时优先显示 AI 汇总，避免被提示语覆盖"""
        if any(self._cam_running.values()) and self._mon_ai_summary():
            return
        self.mon_status.config(text=text, foreground=GRAY_HINT)

    def _cam_stop(self, code, quiet=False):
        """停止一路摄像头。立刻返回：句柄释放交给后台线程（卡住的流 release 也会阻塞）"""
        self._cam_running[code] = False
        reader = self._cam_readers.pop(code, None)
        if reader is not None:
            reader.stop("界面停止播放")        # 内部异步释放，不阻塞界面
        self._cam_running.pop(code, None)
        after_id = self._cam_render_after.pop(code, None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass
        self._cam_photos.pop(code, None)
        self._cam_seen.pop(code, None)
        if not quiet:
            self._cam_stopped_ui(code)

    def _cam_stopped_ui(self, code):
        panel = self._mon_panels.get(code)
        try:
            if panel is not None and panel.get("label") and panel["label"].winfo_exists():
                panel["label"].config(image="")
            self._cam_btn_text(code, "▶ 播放")
            self._cam_set_status(code, "未播放（已停止）", CAM_COLORS["idle"])
            ai = panel.get("ai") if panel else None
            if ai is not None and ai.winfo_exists():
                ai.config(text="", foreground=GRAY_MUTED)
            self._cam_set_density(code, DENSITY_LOW)
            panel["density_level"] = None
            panel["persons"] = []
            self._cam_set_border(code, "idle")
            self._mon_ai_summary()
            self._cam_update_info_bar()
        except tk.TclError:
            pass

    def _cam_set_placeholder(self, code, text):
        """无图像或打开失败时显示黑底白字占位图"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        lbl = panel.get("label")
        if lbl is None or not lbl.winfo_exists():
            return
        try:
            h = max(lbl.winfo_height(), 120)
            w = max(lbl.winfo_width(), 160)
            img = Image.new("RGB", (w, h), "black")
            from PIL import ImageDraw, ImageFont
            draw = ImageDraw.Draw(img)
            # 简单多行文字（字体回退到默认）
            try:
                font = ImageFont.truetype("msyh.ttc", 16)
            except Exception:
                font = ImageFont.load_default()
            for i, line in enumerate(text.split("\n")):
                draw.text((10, 10 + i * 22), line, fill=(255, 255, 255), font=font)
            self._cam_photos[code] = ImageTk.PhotoImage(image=img)
            lbl.config(image=self._cam_photos[code])
        except Exception:
            pass

    def _cam_enlarge(self, code):
        """点击摄像头画面：弹出放大窗口，左侧大图 + 右侧边栏"""
        dev = self._dev_by_code(code)
        title = "摄像头放大 - %s" % (dev.name or dev.code if dev else code)
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("1100x720")
        win.transient(self.root)

        paned = ttk.PanedWindow(win, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        lbl = tk.Label(left, bg="#000", relief="sunken")
        lbl.pack(fill="both", expand=True)
        status = ttk.Label(left, text="就绪", foreground=GRAY_HINT)
        status.pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(paned, padding=8, relief="ridge", borderwidth=1)
        paned.add(right, weight=1)
        ttk.Label(right, text="AI 辅助分析", font=FONT_TITLE).pack(anchor="w", pady=(0, 8))

        # 人员列表
        person_box = ttk.LabelFrame(right, text=" 人员列表 ", padding=6)
        person_box.pack(fill="both", expand=True, pady=(0, 8))
        person_txt = tk.Text(person_box, height=8, wrap="word", font=FONT_SMALL)
        person_txt.pack(fill="both", expand=True)
        person_txt.config(state="disabled")

        # 历史异常时间线
        hist_box = ttk.LabelFrame(right, text=" 最近 10 条异常 ", padding=6)
        hist_box.pack(fill="both", expand=True, pady=(0, 8))
        hist_txt = tk.Text(hist_box, height=8, wrap="word", font=FONT_SMALL)
        hist_txt.pack(fill="both", expand=True)
        hist_txt.config(state="disabled")

        # AI 设置区（本机位临时调整，点保存会写全局配置）
        set_box = ttk.LabelFrame(right, text=" AI 设置 ", padding=6)
        set_box.pack(fill="x", pady=(0, 8))
        margin_var = tk.IntVar(value=self._ai_settings.get("box_margin_pct", 2))
        ttk.Label(set_box, text="框边距：").grid(row=0, column=0, sticky="e")
        margin_cb = ttk.Combobox(set_box, textvariable=margin_var,
                                 values=[1, 2, 5, 10], state="readonly", width=6)
        margin_cb.grid(row=0, column=1, sticky="w", padx=(4, 0))
        ttk.Label(set_box, text="%").grid(row=0, column=2, sticky="w")
        haar_var = tk.BooleanVar(value=self._ai_settings.get("haar_face", True))
        ttk.Checkbutton(set_box, text="Haar 人脸微表情分析", variable=haar_var
                        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def apply_local():
            self._ai_settings["box_margin_pct"] = margin_var.get()
            self._ai_settings["haar_face"] = haar_var.get()
            self._save_ai_settings(self._ai_settings)
            self._person_detector = None
            status.config(text="AI 设置已保存并生效", foreground=CAM_COLORS["playing"])

        ttk.Button(set_box, text="保存到全局", command=apply_local).grid(
            row=2, column=0, columnspan=3, pady=(8, 0), sticky="w")

        ttk.Button(right, text="关闭", command=win.destroy).pack(anchor="e", pady=(4, 0))
        self._cam_enlarge_tick(code, lbl, status, win, person_txt, hist_txt)

    def _cam_enlarge_tick(self, code, lbl, status, win, person_txt, hist_txt):
        if not win.winfo_exists():
            return
        frame = self._cam_latest.get(code)
        if frame is not None:
            try:
                h = max(lbl.winfo_height(), 480)
                w = max(lbl.winfo_width(), 640)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                img.thumbnail((w, h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image=img)
                lbl.photo_ref = photo
                lbl.config(image=photo)
                status.config(text="播放中", foreground=CAM_COLORS["playing"])
            except Exception as e:
                status.config(text="渲染失败：%s" % e, foreground=CAM_COLORS["error"])
        else:
            status.config(text="等待视频帧…", foreground=GRAY_MUTED)
        # 更新侧边栏
        panel = self._mon_panels.get(code)
        if panel is not None:
            persons = panel.get("persons", []) or []
            p_lines = []
            for p in persons:
                acts = ", ".join(p.get("behaviors", [])) or p.get("action", "正常")
                p_lines.append("#%d：%s" % (p.get("pid", 0), acts))
            self._text_set(person_txt, "\n".join(p_lines) or "（暂无人员）")
            history = panel.get("history", []) or []
            h_lines = ["%s　%s" % (h["ts"], h["text"]) for h in history]
            self._text_set(hist_txt, "\n".join(h_lines) or "（最近 10 条异常时间线）")
        self.root.after(100, lambda: self._cam_enlarge_tick(code, lbl, status, win, person_txt, hist_txt))

    @staticmethod
    def _text_set(widget, text):
        """安全设置 Text 控件内容"""
        if widget is None or not widget.winfo_exists():
            return
        try:
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.config(state="disabled")
        except tk.TclError:
            pass

    def _dev_by_code(self, code):
        for d in self.store.list_devices(enabled_only=False):
            if d.code == code:
                return d
        return None

    def _cam_set_status(self, code, text, color):
        panel = self._mon_panels.get(code)
        if panel is not None and panel.get("status") and panel["status"].winfo_exists():
            panel["status"].config(text=text, foreground=color)

    def _cam_btn_text(self, code, text):
        panel = self._mon_panels.get(code)
        if panel is not None and panel.get("btn") and panel["btn"].winfo_exists():
            panel["btn"].config(text=text)

    def _mon_play_all(self):
        devs = [d for d in self.store.list_devices(enabled_only=False) if d.code and d.url]
        if not devs:
            self._mon_set_status("没有可播放的摄像头（请先在设备台账填取流地址）")
            return
        for d in devs:
            self._cam_play(d.code)
        self._mon_log_line("全部播放：%d 路" % len(devs))
        self._mon_set_status("已启动 %d 路摄像头（连接中，不通的会自动重连）" % len(devs))

    def _mon_stop_all(self):
        codes = list(self._cam_running.keys())
        for code in codes:
            self._cam_stop(code, quiet=True)
        for code in list(self._mon_panels):
            self._cam_stopped_ui(code)
        self._mon_log_line("全部停止：%d 路" % len(codes))
        self._mon_set_status("已停止全部摄像头")


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
