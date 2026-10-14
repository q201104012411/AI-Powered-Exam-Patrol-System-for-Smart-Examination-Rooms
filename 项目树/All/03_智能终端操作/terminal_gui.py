# -*- coding: utf-8 -*-
"""
智能终端操作端 · 桌面界面（tkinter）

按《智能终端.md》落地：
  · 告警分级展示：红色置顶 + 弹窗 + 强制确认；橙色普通提示
  · 复核留痕：结论 / 理由 / 见证人 / 附件
  · 断网可用：状态栏显示在线状态与待补传条数，可手动补传
  · 权限最小化：登录后按角色过滤（监考终端只看本考场），默认脱敏
  · 手持核验：调用项目一人脸核验（子进程桥接）
  · 命令控制台：默认关闭；所有者可执行系统命令 / Python 代码，管理员需所有者单独
    授权且仅限 `ollama run <模型> [提示词]`（判定与执行都在 01 侧，见 console.py）

运行：python terminal_gui.py
"""
import os
import shutil
import inspect
import json
import sys
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
try:
    import person_detect as pd_mod
    HAS_PD = True
except Exception:
    HAS_PD = False
try:
    import vlm_detect as vlm_mod          # 多模态（VLM）检测引擎；拿不到就只跑 YOLO11n
    HAS_VLM = True
except Exception:
    vlm_mod = None
    HAS_VLM = False

import access
import alerts as alerts_mod
import bridge
import camera_stream
import clock as clock_mod
import config
import console
import fleet as fleet_mod
import models
import offline as offline_mod
import review as review_mod
import storage

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
MONO = ("Consolas", 10)


class _BehaviorTracker(object):
    """逐人异常行为计数：同一人同一行为连续达到阈值且冷却完成后才在本终端生成告警"""
    THRESHOLD = 20          # 连续 20 帧（约 0.7 秒）
    COOLDOWN_SEC = 30       # 同一人同一行为 30 秒内只报一次

    def __init__(self):
        # key: (url, pid, behavior) -> {"count": int, "last_alert": float}
        self._state = {}

    def update(self, url, persons):
        now = time.time()
        seen = set()
        triggered = []
        for p in persons:
            pid = p.get("pid", 0)
            for b in p.get("behaviors", ()):
                key = (url, pid, b)
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
        for key, s in list(self._state.items()):
            if key[0] == url and key not in seen:
                s["count"] = 0
        return triggered


# 行为 -> 03 告警码（与 02 linkage_bridge 保持一致）
_BEHAVIOR_ALERT_CODE = {
    "触碰红线": "REPEATED_TOUCH_LINE",
    "越界": "PERSON_OUT_OF_BOUNDS",
    "大幅度偏转": "BIG_TURN",
    "起立/站立": "STAND_UP",
    "前倾/趴桌": "LEAN_FORWARD",
    "东张西望": "TURN_HEAD",
    "长时间低头": "LOOK_DOWN",
    "眼神游移": "GAZE_SHIFT",
    "闭眼/趴睡": "SLEEPY",
    "转脸偏头": "HEAD_TURN",
}

# 密度等级（与 02 保持一致）
DENSITY_LOW = "low"
DENSITY_MED = "medium"
DENSITY_HIGH = "high"
DENSITY_TEXT = {DENSITY_LOW: "低", DENSITY_MED: "中", DENSITY_HIGH: "高"}
DENSITY_COLOR = {DENSITY_LOW: "#0a7d28", DENSITY_MED: "#1f56b8", DENSITY_HIGH: "#e67e00"}


def _density_level(persons, frame_or_shape):
    """按每百万像素人数返回密度等级（可传帧或 (h, w) 元组）"""
    if frame_or_shape is None or not persons:
        return DENSITY_LOW
    shape = getattr(frame_or_shape, "shape", frame_or_shape)
    if not shape or len(shape) < 2:
        return DENSITY_LOW
    h, w = shape[0], shape[1]
    mp = max(h * w / 1_000_000.0, 0.01)
    d = len(persons) / mp
    if d >= 30:
        return DENSITY_HIGH
    if d >= 15:
        return DENSITY_MED
    return DENSITY_LOW


# ---------------- 渲染性能参数（解决"画面一多就卡"） ----------------
RENDER_MS_FEW = 66
RENDER_MS_MID = 90
RENDER_MS_MANY = 130
RENDER_MS_HUGE = 200
AI_INTERVAL_MS = 500        # 单路 AI 分析最小间隔（2 次/秒）
# AI 分析用长边。【2026-10 实测调优】HOG 的人体检测窗口是 128 px 高，
# 而"坐着的人"在 416 长边下只有约 70 px 高，必然漏检。实测检出人数：
#     416 -> 4~5 人 ｜ 640 -> 6 人 ｜ 原生 1080p -> 7 人（真值 24 人，HOG 上限如此）
# 同时 person_detect 已默认关闭 dense 密集网格（同检出、耗时约 1/3）。
# 因此默认由 416 提到 640：约 33 ms/次/路，9 路约占单核 60%，仍有余量。
# 【40 人考场】单张 YOLO 推理会把整帧压到 640，后排考生只有十几像素高 —— 必漏。
# 密集档因此改为 1280，并让 person_detect 走"分块推理"（切成 640 的块分别送模型）：
# 实测合成 40 人考场 召回 25% → 67.5%（单路分析约 170 ms）。
# 可在 data\ai_settings.json 里用 ai_input_dim / ai_mode / yolo_tiling 覆盖。
AI_MODE_DIM = {             # 预设：标准 / 密集（40 人考场）/ 节能
    "standard": 640,
    "dense": 1280,
    "energy": 416,
}
AI_INPUT_DIM = AI_MODE_DIM["standard"]   # 兼容旧引用；启动时按设置覆盖
INFO_BAR_MS = 500           # 顶部状态条最小刷新间隔
MAX_PANEL_W = 480
MAX_PANEL_H = 300
PANEL_W = 384               # 单路画面初始像素宽（Label 显示 image 时按像素解释）
PANEL_H = 216               # 单路画面初始像素高
BOUNDARY_RATIO = 0.92       # 警戒线：只有走到镜头前（画面底部 8%）才算越界


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
        # 传入 store：命令控制台的每一次调用（含被拒绝的尝试）都由桥接层写审计链，
        # 放在这里而不是写在界面上，是为了将来新增入口时不会漏掉留痕
        self.cmd_bridge = bridge.CmdBridge(store=self.store)
        self.session = None
        # 关窗口 = 停掉所有取流（卡在网络 read 里的线程是 daemon，退出时自然结束）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_login()

    def _on_close(self):
        """退出前尽力收尾：停取流、取消渲染定时器，然后立刻销毁窗口（不做任何 join）"""
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
                            "owner（所有者·最高权限）\n"
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
        self.nb = nb
        self.tab_alerts = ttk.Frame(nb, padding=8)
        self.tab_review = ttk.Frame(nb, padding=8)
        self.tab_verify = ttk.Frame(nb, padding=8)
        self.tab_ops = ttk.Frame(nb, padding=8)
        # 监控查看：仅所有者、管理员、巡考员可见
        self.can_monitor = self.session.role in (config.ROLE_OWNER,
                                                 config.ROLE_ADMIN,
                                                 config.ROLE_PATROL)
        if self.can_monitor:
            self.tab_monitor = ttk.Frame(nb, padding=8)
        nb.add(self.tab_alerts, text="  告警处置  ")
        nb.add(self.tab_review, text="  复核留痕  ")
        nb.add(self.tab_verify, text="  手持核验  ")
        if self.can_monitor:
            nb.add(self.tab_monitor, text="  监控查看  ")
        nb.add(self.tab_ops, text="  运行状态与管控  ")
        self._build_alerts_tab()
        self._build_review_tab()
        self._build_verify_tab()
        if self.can_monitor:
            self._build_monitor_tab()
        self._build_ops_tab()

        # 常驻 AI 对话面板：放在 4 个标签页下方，切换界面始终可见
        self._build_ai_panel(self.root)

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
        ttk.Button(bar, text="📋 复制编号", command=self._copy_alert_id).pack(side="left", padx=4)
        # 说明：「生成演示告警」是演练用入口，已收敛到「运行状态与管控 → 权限控制台」，
        #       仅所有者可见可点（见 _build_ops_tab）。
        ttk.Button(bar, text="☁ 立即补传", command=self._flush).pack(side="right")
        self.alerts_hint = ttk.Label(bar, text="", foreground="#c07000")
        self.alerts_hint.pack(side="right", padx=10)

        cols = ("id", "level", "time", "code", "room", "seat", "candidate", "status", "review")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, t, w in (("id", "编号", 70), ("level", "级别", 80), ("time", "时间", 140),
                        ("code", "告警码", 130), ("room", "考场", 70), ("seat", "座位", 60),
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
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="🔄 刷新", command=self.refresh_reviews).pack(side="left")

        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=6)
        ttk.Label(grid, text="告警 ID：").grid(row=0, column=0, sticky="e", pady=3)
        self.rv_alert = tk.StringVar()
        self.rv_alert_box = ttk.Combobox(grid, textvariable=self.rv_alert, width=26,
                                         state="readonly")
        self.rv_alert_box.grid(row=0, column=1, sticky="w")
        self.rv_alert_box.bind("<<ComboboxSelected>>", self._rv_alert_changed)
        ttk.Button(grid, text="🔄 同步", command=self._rv_sync_alert).grid(row=0, column=2, padx=6)
        ttk.Label(grid, text="结论：").grid(row=0, column=3, sticky="e", padx=(12, 0))
        self.rv_conc = tk.StringVar(value=config.REVIEW_CONFIRMED)
        ttk.Combobox(grid, textvariable=self.rv_conc, state="readonly", width=12,
                     values=list(config.REVIEW_CONCLUSIONS.keys())).grid(row=0, column=4, sticky="w")
        ttk.Label(grid, text="考生编号：").grid(row=1, column=0, sticky="e", pady=3)
        self.rv_cand = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_cand, width=26,
                  state="readonly").grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="见证人：").grid(row=1, column=2, sticky="e", padx=(12, 0))
        self.rv_wit = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_wit, width=26).grid(row=1, column=3, sticky="w")
        ttk.Label(grid, text="附件：").grid(row=1, column=4, sticky="e", padx=(12, 0))
        self.rv_att = tk.StringVar()
        ttk.Entry(grid, textvariable=self.rv_att, width=22).grid(row=1, column=5, sticky="w")
        ttk.Button(grid, text="选择…", command=self._pick_attach).grid(row=1, column=6, padx=4)
        ttk.Label(f, text="复核理由（必填）：").pack(anchor="w", pady=(6, 0))
        self.rv_reason = tk.Text(f, height=4, wrap="word", font=FONT)
        self.rv_reason.pack(fill="x")
        rbar = ttk.Frame(f)
        rbar.pack(fill="x", pady=6)
        ttk.Button(rbar, text="提交复核", command=self._submit_review).pack(side="left")
        ttk.Button(rbar, text="修改选中复核", command=self._rv_edit_selected).pack(side="left", padx=6)
        ttk.Button(rbar, text="误报统计（算法迭代闭环）",
                   command=self._fp_stats).pack(side="left", padx=8)
        ttk.Label(f, text="复核记录（双击可修改，需填写修改理由）", font=FONT_B).pack(anchor="w", pady=(8, 2))
        self.rv_list = tk.Text(f, height=12, wrap="word", font=MONO)
        self.rv_list.pack(fill="both", expand=True)
        self.rv_list.tag_config("fp", foreground="#c07000")
        self.rv_list.bind("<Double-Button-1>", lambda _e: self._rv_edit_selected())

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
        ttk.Button(f, text="🔄 刷新桥接状态", command=self.refresh_verify).pack(anchor="w", pady=(8, 0))
        ttk.Button(f, text="开始核验", command=self._do_verify).pack(anchor="w", pady=8)
        self.v_out = tk.Text(f, height=16, wrap="word", font=MONO)
        self.v_out.pack(fill="both", expand=True)

    def refresh_verify(self):
        """刷新手持核验页的桥接状态"""
        ok, why = self.face.available()
        self.v_out.delete("1.0", "end")
        self.v_out.insert("end", "桥接状态：%s\n%s\n" % (
            "✅ 可用" if ok else "⚠ " + why, why if not ok else ""))

    # ---------- 监控查看（多摄像头视频流，仅 owner/admin/patrol 可见） ----------
    AREA_EXAM = "exam"      # 考场监考：有 AI 辅助识别
    AREA_OTHER = "other"    # 非考场监控：纯视频，无 AI

    def _monitor_load(self):
        p = config.DATA_ROOT / "monitors.json"
        if not p.exists():
            return []
        try:
            cams = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        # 老配置没有 area 字段：默认按考场监考处理
        for c in cams:
            c.setdefault("area", self.AREA_EXAM)
        return cams

    def _monitor_save(self):
        try:
            (config.DATA_ROOT / "monitors.json").write_text(
                json.dumps(self.monitors, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _build_monitor_tab(self):
        f = self.tab_monitor
        ttk.Label(f, text="实时视频监控（考场监考 + 非考场监控，多摄像头同时播放）",
                  font=FONT_B).pack(anchor="w")
        if not HAS_CV2 or not HAS_PIL:
            ttk.Label(f, text="⚠ 缺少视频播放依赖：需要 opencv-python 与 Pillow",
                      foreground="#c00").pack(anchor="w", pady=10)
            return

        self.monitors = self._monitor_load()
        self._cam_readers = {}      # url -> camera_stream.FrameReader（取流内核）
        self._cam_running = {}      # url -> bool
        self._cam_photos = {}       # url -> PhotoImage
        self._cam_latest = {}       # url -> 最新帧
        self._cam_seen = {}         # url -> (generation, seq) 已渲染过的帧
        self._cam_render_after = {}  # url -> after id（渲染循环，避免重复排程）
        self._person_detectors = {}  # url -> PersonTracker（仅考场路）
        self._cheat_watches = {}     # url -> CheatWatch（作弊识别时序引擎，仅考场路）
        self._behavior_tracker = _BehaviorTracker()
        self._info_ts = 0.0         # 顶部状态条限频用
        self._ai_error_file = config.DATA_ROOT / "ai_detect_error.txt"
        self._mon_log = config.LOG_DIR / "monitor_ui.log"

        # ---- 顶部工具条 ----
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 6))
        # 管理摄像头（增删改推流地址与监控区域）：仅所有者可调整
        if self.session.role == config.ROLE_OWNER:
            ttk.Button(bar, text="📹 管理摄像头",
                       command=self._monitor_manage).pack(side="left")
        else:
            ttk.Button(bar, text="📹 管理摄像头（仅所有者）",
                       state="disabled").pack(side="left")
        ttk.Button(bar, text="▶ 全部播放",
                   command=self._monitor_play_all).pack(side="left", padx=6)
        ttk.Button(bar, text="⏹ 全部停止",
                   command=self._monitor_stop_all).pack(side="left")
        self.mon_ai = tk.BooleanVar(value=True)     # 考场监考默认开启 AI 辅助识别
        if HAS_PD:
            if self.session.role == config.ROLE_OWNER:
                ttk.Checkbutton(
                    bar, text="AI 辅助识别（仅考场监考生效：绿=正常 橙=异常 红=越界）",
                    variable=self.mon_ai).pack(side="left", padx=12)
            else:
                ttk.Label(bar, text="AI 辅助识别：开启（仅所有者可调整）",
                          foreground="#1f56b8").pack(side="left", padx=12)
        self.monitor_status = ttk.Label(bar, text="就绪", foreground="#666")
        self.monitor_status.pack(side="right")

        # ---- 全局状态条 ----
        self.info_bar = ttk.LabelFrame(f, text=" 实时状态 ", padding=6)
        self.info_bar.pack(fill="x", pady=(0, 6))
        self.info_label = ttk.Label(self.info_bar, text="暂无摄像头",
                                    font=FONT, foreground="#333")
        self.info_label.pack(anchor="w")

        # ---- 两个子页签：考场监考（有 AI） / 非考场监控（纯视频） ----
        self.mon_nb = ttk.Notebook(f)
        self.mon_nb.pack(fill="both", expand=True)

        self.exam_tab = ttk.Frame(self.mon_nb, padding=4)
        self.mon_nb.add(self.exam_tab, text=" 🎓 考场监考（AI 辅助识别） ")
        self.mon_exam_canvas, self.mon_exam_grid = self._make_scroll_area(self.exam_tab)

        self.other_tab = ttk.Frame(self.mon_nb, padding=4)
        self.mon_nb.add(self.other_tab, text=" 🚪 非考场监控（走廊/门厅，纯视频） ")
        self.mon_other_canvas, self.mon_other_grid = self._make_scroll_area(self.other_tab)

        self._monitor_render_grid()

    def _make_scroll_area(self, parent):
        """可滚动摄像头网格容器，支持鼠标滚轮；返回 (canvas, grid)"""
        canvas = tk.Canvas(parent, highlightthickness=0, bg="#f4f4f4")
        canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        sb.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=sb.set)
        grid = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>",
                  lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        self._bind_mousewheel(canvas)
        return canvas, grid

    def _monitor_render_grid(self):
        """按区域分别渲染：考场监考 -> AI 网格；非考场监控 -> 纯视频网格

        重建网格前先停掉所有取流与渲染定时器，避免残留循环往已销毁的控件贴图。
        """
        self._monitor_stop_all()
        for grid in (self.mon_exam_grid, self.mon_other_grid):
            for w in grid.winfo_children():
                w.destroy()
        exam_cams = [c for c in self.monitors
                     if c.get("area", self.AREA_EXAM) == self.AREA_EXAM]
        other_cams = [c for c in self.monitors
                      if c.get("area", self.AREA_EXAM) != self.AREA_EXAM]
        self._render_cam_grid(self.mon_exam_grid, exam_cams,
                              empty_text="暂无考场监考摄像头，请在「管理摄像头」新增时选择「考场监考」。")
        self._render_cam_grid(self.mon_other_grid, other_cams,
                              empty_text="暂无非考场监控摄像头，请在「管理摄像头」新增时选择「非考场监控」。")

    def _render_cam_grid(self, grid, cams, empty_text):
        if not cams:
            ttk.Label(grid, text=empty_text,
                      foreground="#888", wraplength=560,
                      justify="center").pack(pady=30)
            return
        cols = 2
        for i, cam in enumerate(cams):
            room = cam.get("room", "未命名考场")
            device = cam.get("device", "未命名设备")
            url = cam.get("url", "")
            is_exam = cam.get("area", self.AREA_EXAM) == self.AREA_EXAM
            prefix = "🎓" if is_exam else "🚪"
            # tk.LabelFrame 支持 highlightbackground，可做状态色边框（绿=正常 橙=异常 红=越界）
            frame = tk.LabelFrame(grid, text=" %s %s · %s " % (prefix, room, device),
                                  bg="#f9f9f9", fg="#333", font=FONT,
                                  highlightthickness=2, highlightbackground="#888")
            frame.grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)
            grid.rowconfigure(i // cols, weight=1)
            grid.columnconfigure(i % cols, weight=1)
            cam["_frame"] = frame
            cam["_border_state"] = None

            # 注意：tk.Label 设置 image 后 width/height 按**像素**解释，
            # 必须先给一张 1x1 黑图再定像素尺寸，否则画面会被压成细缝。
            blank = tk.PhotoImage(width=1, height=1)
            blank.put("#000000", to=(0, 0))
            lbl = tk.Label(frame, bg="#000", relief="sunken",
                           image=blank, width=PANEL_W, height=PANEL_H)
            lbl._blank_img = blank
            lbl.pack(fill="both", expand=True)
            cam["_label"] = lbl
            srow = ttk.Frame(frame)
            srow.pack(fill="x", pady=(2, 0))
            status = ttk.Label(srow, text=("未播放" if is_exam else "未播放（该区域无 AI）"),
                               foreground="#666")
            status.pack(side="left")
            cam["_status"] = status
            cam["_status_text"] = None
            if is_exam:
                density = ttk.Label(srow, text="密度：-", foreground="#888", font=FONT)
                density.pack(side="left", padx=(8, 0))
                cam["_density"] = density
            bbar = ttk.Frame(frame)
            bbar.pack(fill="x", pady=(4, 0))
            cam["_btn"] = ttk.Button(bbar, text="▶ 播放",
                                     command=lambda u=url: self._cam_play(u))
            cam["_btn"].pack(side="left", padx=(0, 4), pady=(2, 2))
            ttk.Button(bbar, text="⏹ 停止",
                       command=lambda u=url: self._cam_stop(u)).pack(side="left", padx=(0, 4), pady=(2, 2))

    def _cam_play(self, url):
        """开始播放一路监控。

        **不在界面线程里打开视频流**：旧版这里同步调 cv2.VideoCapture + cap.read()
        并依次试多个后端，网络不通/推流断开时会把整个终端界面冻住。
        现在交给 camera_stream.FrameReader：后台打开、超时可控、卡死自动重连。
        """
        if not url or self._cam_running.get(url):
            return
        self._cam_stop(url, quiet=True)
        reader = camera_stream.FrameReader(
            url, label="03/%s" % camera_stream.short_url(url, 32))
        self._cam_readers[url] = reader
        self._cam_running[url] = True
        self._cam_seen.pop(url, None)
        self._cam_latest.pop(url, None)
        self._cam_set_status(url, "正在连接…", "#1f56b8")
        self._cam_btn_text(url, "连接中…")
        reader.start()
        self._cam_poll(url)
        self._cam_render(url)

    def _cam_poll(self, url):
        """每 500ms 刷新一路的取流状态（非阻塞），含"画面中断/重连"提示"""
        reader = self._cam_readers.get(url)
        if reader is None or not self._cam_running.get(url):
            return
        snap = reader.snapshot()
        state = snap["state"]
        if state == "live":
            if snap["seq"]:
                self._cam_set_status(url, "播放中 ｜ %.1f fps" % snap["fps"], "#0a7d28")
            else:
                self._cam_set_status(url, "已连接，等待画面…", "#1f56b8")
        elif state == "connecting":
            self._cam_set_status(url, snap["status"] or "正在连接…", "#1f56b8")
        elif state == "stalled":
            self._cam_set_status(url, snap["status"], "#c07000")
        elif state in ("error", "stopped"):
            self._cam_set_status(url, snap["status"] or "取流已停止", "#c00")
            if state == "error":
                self._cam_btn_text(url, "▶ 重试")
        try:
            self.root.after(500, lambda u=url: self._cam_poll(u))
        except (tk.TclError, RuntimeError):
            pass

    def _render_interval(self):
        """按当前播放路数自适应渲染间隔：路数越多，单路帧率越低"""
        n = sum(1 for v in getattr(self, "_cam_running", {}).values() if v)
        if n <= 2:
            return RENDER_MS_FEW
        if n <= 4:
            return RENDER_MS_MID
        if n <= 9:
            return RENDER_MS_MANY
        return RENDER_MS_HUGE

    def _cam_render(self, url):
        """渲染循环：取最新帧贴图。

        性能要点：帧序号不变不重渲染；帧率按路数自适应；
        AI 分析低频跑、中间帧复用缓存的框；缩图走 cv2（不再用 PIL LANCZOS）。
        """
        try:
            reader = self._cam_readers.get(url)
            if reader is None or not self._cam_running.get(url):
                self._cam_render_after.pop(url, None)
                return
            snap = reader.snapshot()
            key = (snap["generation"], snap["seq"])
            if snap["frame"] is not None and snap["seq"] and self._cam_seen.get(url) != key:
                self._cam_seen[url] = key
                self._cam_update(url, snap)
        except Exception as e:                       # noqa: BLE001
            self._cam_set_status(url, "渲染失败：%s" % str(e)[:60], "#c00")
        if not self._cam_running.get(url):
            self._cam_render_after.pop(url, None)
            return
        try:
            self._cam_render_after[url] = self.root.after(
                self._render_interval(), lambda u=url: self._cam_render(u))
        except (tk.TclError, RuntimeError):
            self._cam_render_after.pop(url, None)

    def _mon_log_line(self, text):
        """监控操作日志（取流问题事后可查）"""
        try:
            with open(str(self._mon_log), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    @staticmethod
    def _fit_frame(frame, w, h):
        """等比缩放到不超过 (w,h)（cv2.resize，下采样用 INTER_AREA）"""
        fh, fw = frame.shape[:2]
        if fw <= 0 or fh <= 0:
            return frame
        scale = min(w / float(fw), h / float(fh))
        if scale >= 1.0 and fw <= MAX_PANEL_W and fh <= MAX_PANEL_H:
            return frame
        scale = min(scale, MAX_PANEL_W / float(fw), MAX_PANEL_H / float(fh))
        nw = max(2, int(fw * scale))
        nh = max(2, int(fh * scale))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        return cv2.resize(frame, (nw, nh), interpolation=interp)

    def _cam_update(self, url, snap):
        """渲染一路监控：AI 分析与视频贴图解耦"""
        cam = self._cam_by_url(url)
        if cam is None:
            return
        lbl = cam.get("_label")
        if lbl is None or not lbl.winfo_exists():
            return
        raw = snap.get("frame")
        if raw is None or getattr(raw, "size", 0) == 0:
            return
        try:
            h = lbl.winfo_height()
            w = lbl.winfo_width()
            if h < 50:
                h = 240
            if w < 50:
                w = 320
            disp = self._fit_frame(raw, w, h)

            now_ms = time.time() * 1000.0
            ai_on = (HAS_PD and getattr(self, "mon_ai", None) and self.mon_ai.get()
                     and cam.get("area", self.AREA_EXAM) == self.AREA_EXAM)
            persons = cam.get("_persons", []) or []
            viol = cam.get("_viol", 0)

            if ai_on:
                due = (now_ms - cam.get("_ai_ts", 0.0)) >= AI_INTERVAL_MS
                if due:
                    rh, rw = raw.shape[:2]
                    # 分析长边按设置解析（缓存到 cam，避免每帧读配置）
                    dim = cam.get("_ai_dim")
                    if dim is None:
                        try:
                            cfg = config.load_ai_settings()
                            dim = int(cfg.get("ai_input_dim") or 0) or \
                                AI_MODE_DIM.get(str(cfg.get("ai_mode") or "standard"),
                                                AI_INPUT_DIM)
                        except Exception:
                            dim = AI_INPUT_DIM
                        cam["_ai_dim"] = dim
                    if max(rh, rw) > dim:
                        s = dim / float(max(rh, rw))
                        ai_frame = cv2.resize(raw, (max(2, int(rw * s)),
                                                    max(2, int(rh * s))),
                                              interpolation=cv2.INTER_AREA)
                    else:
                        ai_frame = raw
                    det = self._person_detector_for(url)
                    if det is not None:
                        persons, viol, line_y = det.analyze_only(
                            ai_frame, boundary_ratio=BOUNDARY_RATIO)
                        for p in persons:
                            tr = getattr(det, "_tracks", {}).get(p["pid"])
                            fa = getattr(tr, "face_cache", None) if tr is not None else None
                            p["face"] = (fa or {}).get("face") if fa else None
                        cam["_ai_ts"] = now_ms
                        cam["_persons"] = persons
                        cam["_viol"] = viol
                        cam["_ai_line_y"] = line_y
                        cam["_ai_shape"] = ai_frame.shape
                        cam["_ai_scale"] = disp.shape[1] / float(ai_frame.shape[1])
                        cam["_ai_count"] = (det.stable_count()
                                            if hasattr(det, "stable_count")
                                            else len(persons))
                        # 手机识别：新版检测链（YOLO11n 检测头）只解码 COCO person 类，
                        # 不再产出 has_phone / 手机类峰值置信度，故此处不再统计手机。
                        self._cam_ai_side_effects(url, cam, persons, viol)
                scale = cam.get("_ai_scale")
                if scale is None:
                    scale = disp.shape[1] / float(raw.shape[1])
                    cam["_ai_scale"] = scale
                det = self._person_detectors.get(url)
                if det is not None and persons:
                    det.draw_boxes(disp, persons, line_y=cam.get("_ai_line_y"),
                                   scale=scale, font_size=12)

            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            self._cam_photos[url] = ImageTk.PhotoImage(image=Image.fromarray(rgb))
            lbl.config(image=self._cam_photos[url])
            self._cam_set_status(url, "播放中", "#0a7d28")
        except Exception as e:
            self._cam_set_status(url, "渲染失败：%s" % e, "#c00")

    def _person_detector_for(self, url):
        """取（或创建）该路的 AI 检测器。

        检测链：多模态大模型（开了 vlm_provider 时为主）> YOLO11n ONNX >
        MobileNet-SSD > HOG，全部由代码自动选择；data\\ai_settings.json 可覆盖
        模型/阈值/分析长边/多模态后端（与 02 同一套格式）。
        构造参数按"将要使用的那个类"的可接受参数过滤，参数改名/增删都不会把
        整条识别链打挂（不认识的键自动忽略）。
        """
        det = self._person_detectors.get(url)
        if det is not None:
            return det
        # 读取 AI 检测设置（缺文件/坏文件都返回默认值，不抛异常）
        try:
            ai_cfg = config.load_ai_settings()
        except Exception:
            ai_cfg = {}
        kwargs = {"boundary_ratio": BOUNDARY_RATIO, "max_input_dim": 1280,
                  "confirm_frames": 4, "min_hits": 2}
        for k in ("yolo_model_path", "yolo_conf", "yolo_iou", "yolo_input_dim",
                  "yolo_tiling", "yolo_tile_size", "yolo_tile_max"):
            if ai_cfg.get(k) not in (None, ""):
                kwargs[k] = ai_cfg[k]
        # 多模态参数（provider=off 时不会被接受，等于不传）
        if HAS_VLM:
            for k in vlm_mod.SETTING_KEYS:
                if ai_cfg.get(k) not in (None, ""):
                    kwargs[k] = ai_cfg[k]
        # 与 02 的 gui.py 一样按签名过滤：模型/参数换代也不会 TypeError
        try:
            params = vlm_mod.accepted_params(pd_mod.PersonTracker,
                                            kwargs.get("vlm_provider")) \
                if HAS_VLM else inspect.signature(pd_mod.PersonTracker.__init__).parameters
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass
        det = None
        cls = vlm_mod.tracker_class_for(kwargs.get("vlm_provider")) if HAS_VLM else None
        if cls is not None:
            try:
                det = cls(**kwargs)
            except Exception as e:
                self._mon_log_line("多模态检测器创建失败，回落 YOLO11n：%s" % e)
                det = None
        if det is None:
            base_params = set(inspect.signature(pd_mod.PersonTracker.__init__).parameters)
            base_kwargs = dict((k, v) for k, v in kwargs.items() if k in base_params)
            try:
                det = pd_mod.PersonTracker(**base_kwargs)
                self._person_detectors[url] = det
                # 如实展示当前到底用的哪条链（没模型就是 HOG）
                try:
                    self._log_ai_backend(det)
                except Exception:
                    pass
            except TypeError:
                # 兼容尚未升级的 person_detect 版本
                det = pd_mod.PersonTracker(max_input_dim=1280)
                self._person_detectors[url] = det
            except Exception as e:
                self.mon_ai.set(False)
                self.monitor_status.config(text="AI识别不可用：%s" % e)
                try:
                    self._ai_error_file.write_text(str(e), encoding="utf-8")
                except Exception:
                    pass
                return None
        else:
            self._person_detectors[url] = det
            try:
                self._log_ai_backend(det)
            except Exception:
                pass
        return det

    @staticmethod
    def _ai_chain_name(det):
        """当前这一路到底是谁在出框：多模态 / YOLO11n / MobileNet-SSD / HOG。

        多模态开着但结果过期或报错时，如实写成"多模态→YOLO11n 兜底"，
        不让人误以为一直在用大模型。
        """
        if det is not None and hasattr(det, "vlm_status_dict"):
            try:
                d = det.vlm_status_dict()
                if d.get("enabled"):
                    if d.get("source") == "vlm" and d.get("fresh"):
                        return "多模态(%s)" % d.get("model", "")
                    return "多模态→YOLO11n 兜底"
            except Exception:
                pass
        if getattr(det, "_yolo_session", None) is not None:
            return "YOLO11n-ONNX"
        if getattr(det, "_net", None) is not None:
            return "MobileNet-SSD"
        return "HOG+SVM"

    def _log_ai_backend(self, det):
        """把"当前实际用的是哪条检测链"如实记录下来（没模型就是 HOG+SVM）。

        多模态版本：多模态结果新鲜 = 多模态；否则说明是 YOLO11n/HOG 在兜底。
        只在链路名变化时记录一次，失败不影响识别主流程。
        """
        name = self._ai_chain_name(det)
        if getattr(self, "_ai_backend_name", None) == name:
            return
        self._ai_backend_name = name
        try:
            (config.DATA_ROOT / "ai_backend.txt").write_text(
                "%s\n" % name, encoding="utf-8")
        except Exception:
            pass
        try:
            if name == "HOG+SVM":
                self.monitor_status.config(
                    text="AI 框选：HOG+SVM（YOLO11n 模型不可用；"
                         "可运行 工具_YOLO模型导出.py 生成 models\\yolo11n.onnx）")
            else:
                self.monitor_status.config(text="AI 框选后端：%s" % name)
        except Exception:
            pass

    def _cam_cheat_update(self, url, cam, persons):
        """喂一帧给作弊识别时序引擎 → 返回要拼进状态栏的文字（正常时为空串）。

        与逐帧行为标签的区别：发动机看的是**动作序列**，单帧瞬时动作会被
        时间维度抑制（阈值 60 + 滑动窗口 2s + 最短持续 1s），只有持续/组合成
        "获取或传递答案"的模式才告警。
        """
        if not (getattr(self, "mon_ai", None) and self.mon_ai.get()):
            cam["_cheat"] = None
            return ""
        watch = self._cheat_watches.get(url)
        if watch is None:
            try:
                import cheat_bridge
                watch = cheat_bridge.CheatWatch(feed_hz=self._cheat_feed_hz())
            except Exception as e:
                self._write_mon_log("作弊识别不可用：%s" % e)
                return ""
            self._cheat_watches[url] = watch
            self._write_mon_log(watch.describe())
            notes = watch.signal_notes()
            if notes:
                self._write_mon_log("作弊识别信号可得性（引擎自述）：%s" % notes)
        if not watch.ok:
            cam["_cheat"] = {"ok": False, "reason": watch.import_error}
            return "　｜　作弊识别：%s" % (watch.import_error or "不可用")
        r = watch.push_persons(persons, frame_shape=cam.get("_ai_shape"))
        cam["_cheat"] = r
        if not r.get("ok"):
            return ""
        if r.get("just_triggered"):
            text = "作弊预警 %s #%s：%s（风险 %.0f）" % (
                r.get("level", ""), r.get("pid"), r.get("pattern_desc") or "",
                float(r.get("risk", 0)))
            self._write_mon_log(text)
            try:
                import linkage_bridge
                linkage_bridge.report_behavior(
                    device_code=str(cam.get("name") or url), behavior=text,
                    pid=int(r.get("pid") or 0), crosses=0,
                    room_id="", seat_no="",
                    operator=self.session.username if self.session else "03_ai")
            except Exception:
                pass
        if r.get("triggered"):
            return "　｜　作弊风险：%s %.0f【%s】" % (r.get("level", ""),
                                                float(r.get("risk", 0)),
                                                r.get("pattern_desc") or "告警中")
        if float(r.get("risk", 0)) >= 42.0:
            return "　｜　作弊风险：%s %.0f" % (r.get("level", ""),
                                            float(r.get("risk", 0)))
        return ""

    def _cheat_feed_hz(self):
        """真实送帧频率 = 本终端的 AI 分析频率（窗口/最短持续按帧数换算）"""
        try:
            ms = float(getattr(self, "AI_INTERVAL_MS", 0) or 0)
        except Exception:
            ms = 0
        if ms <= 0:
            ms = 1000.0
        return max(0.2, 1000.0 / ms)

    def _write_mon_log(self, text):
        """追加到监控日志（失败不影响界面）"""
        try:
            self._mon_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self._mon_log, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    def _cam_ai_side_effects(self, url, cam, persons, viol):
        """AI 分析后的界面更新 + 异常上报本终端告警列表"""
        level = _density_level(persons, cam.get("_ai_shape"))
        cam["_density_level"] = level
        density = cam.get("_density")
        if density is not None and density.winfo_exists():
            if cam.get("_density_text") != level:
                cam["_density_text"] = level
                try:
                    density.config(text="密度：%s" % DENSITY_TEXT.get(level, level),
                                   foreground=DENSITY_COLOR.get(level, "#888"))
                except tk.TclError:
                    pass
        # 面板边框：越界红 > 异常橙 > 正常绿
        self._cam_set_border(cam, "error" if viol else
                             ("warn" if any(p.get("behaviors") for p in persons)
                              else "ok"))
        # 作弊识别（时序引擎）：单帧动作被抑制，只有"持续/组合"才告警
        cheat_txt = self._cam_cheat_update(url, cam, persons)
        now_ms = time.time() * 1000.0
        if (now_ms - getattr(self, "_info_ts", 0.0)) >= INFO_BAR_MS:
            self._info_ts = now_ms
            try:
                # 一并显示当前检测链（多模态开着时如实写"多模态(模型)"或
                # "多模态→YOLO11n 兜底"，没模型时写 HOG+SVM），
                # 避免操作员误以为一直在用大模型
                det_now = self._person_detectors.get(url)
                backend = self._ai_chain_name(det_now) if det_now is not None else \
                    getattr(self, "_ai_backend_name", "")
                if det_now is not None and getattr(det_now, "_last_tiling", False):
                    backend += "（分块 %d）" % getattr(det_now, "_last_tile_count", 0)
                extra = ("　｜　后端：%s" % backend) if backend else ""
                self.monitor_status.config(
                    text="AI识别中：%d 人，越界 %d%s%s"
                         % (cam.get("_ai_count", len(persons)), viol,
                            extra, cheat_txt))
            except tk.TclError:
                pass
            self._monitor_update_info_bar()
        # 异常上报：直接进本终端告警列表（可复核/可留痕）
        self._ai_ingest_behaviors(url, cam, persons)

    def _cam_set_border(self, cam, state):
        """按状态给监控面板边框上色（绿=正常 橙=异常 红=越界）"""
        frame = cam.get("_frame") if cam else None
        if frame is None:
            return
        if cam.get("_border_state") == state:
            return
        cam["_border_state"] = state
        color = {"ok": "#0a7d28", "warn": "#c07000",
                 "error": "#c00", "idle": "#888"}.get(state, "#888")
        try:
            frame.config(highlightbackground=color)
        except tk.TclError:
            pass

    def _monitor_update_info_bar(self):
        """刷新顶部状态条和分区域标题人数"""
        total = len(self.monitors)
        playing = sum(1 for v in self._cam_running.values() if v)
        ai_total = 0
        cross_total = 0
        abnormal = 0
        high_count = 0
        exam_count = 0
        for cam in self.monitors:
            persons = cam.get("_persons", []) or []
            n = cam.get("_ai_count", len(persons))
            ai_total += n
            cross_total += sum(1 for p in persons if p.get("bad"))
            abnormal += sum(len(p.get("behaviors", [])) for p in persons)
            if cam.get("_density_level") == DENSITY_HIGH:
                high_count += 1
            if cam.get("area", self.AREA_EXAM) == self.AREA_EXAM:
                exam_count += n
        other_count = ai_total - exam_count
        text = ("播放 %d/%d 路　｜　AI 总人数 %d　越界 %d　异常行为 %d　｜"
                "　高密度机位 %d 路" % (playing, total, ai_total, cross_total, abnormal, high_count))
        # 文本没变就不碰控件（每路每轮都 config 会明显拖慢界面）
        if getattr(self, "_info_text", None) != text:
            self._info_text = text
            try:
                self.info_label.config(text=text)
            except tk.TclError:
                pass
        # 更新两个分区域标题旁的人数
        exam_tab_text = " 🎓 考场监考（%d 人） " % exam_count
        other_tab_text = " 🚪 非考场监控（%d 人） " % other_count
        if getattr(self, "_tab_text", None) != (exam_tab_text, other_tab_text):
            self._tab_text = (exam_tab_text, other_tab_text)
            try:
                if getattr(self, "mon_nb", None) is not None:
                    self.mon_nb.tab(self.exam_tab, text=exam_tab_text)
                    self.mon_nb.tab(self.other_tab, text=other_tab_text)
            except tk.TclError:
                pass

    def _ai_ingest_behaviors(self, url, cam, persons):
        """逐人异常行为达到阈值后，直接在本终端生成告警（进 03 自己的告警列表）"""
        try:
            alerts = self._behavior_tracker.update(url, persons)
        except Exception:
            return
        for a in alerts:
            code = _BEHAVIOR_ALERT_CODE.get(a["behavior"],
                                            "PERSON_BEHAVIOR_ABNORMAL")
            detail = "AI 框选识别：#%d 号人员「%s」" % (a["pid"], a["behavior"])
            if a["crosses"] >= 2:
                detail += "（已触碰警戒线 %d 次）" % a["crosses"]
            detail += "，请复核处置。"
            alert = models.Alert(code=code, detail=detail,
                                 room_id=cam.get("room", ""),
                                 device_id=cam.get("device", "") or url,
                                 source="terminal_ai")
            created = False
            try:
                _, created = self.hub.ingest(alert)
            except Exception:
                pass
            if not created:
                continue
            # 上报留痕 + 界面提示，让"异常已上报到本终端"看得见
            self._ai_report_history = (getattr(self, "_ai_report_history", []) + [{
                "ts": time.strftime("%H:%M:%S"),
                "room": cam.get("room", "-"),
                "device": cam.get("device", "-"),
                "text": "#%d %s" % (a["pid"], a["behavior"]),
            }])[-30:]
            try:
                self._mon_log_line("AI 异常上报：%s %s #%d %s"
                                   % (cam.get("room", "-"), cam.get("device", "-"),
                                      a["pid"], a["behavior"]))
            except Exception:
                pass
            self._show_ai_report(alert, cam, a)
            # 告警列表页正在显示时刷新，避免"报了但看不到"
            try:
                if getattr(self, "session", None) is not None:
                    self._refresh_alerts_if_visible()
            except Exception:
                pass

    def _refresh_alerts_if_visible(self):
        """若告警页签当前可见则刷新列表（不可见时不动，省 CPU）"""
        try:
            nb = getattr(self, "nb", None)
            if nb is None:
                return
            if str(nb.select()) == str(self.tab_alerts):
                self.refresh_alerts()
        except Exception:
            pass

    def _show_ai_report(self, alert, cam, behavior):
        """在监控页顶部状态条给出醒目的"已上报"提示"""
        try:
            room = cam.get("room", "-")
            dev = cam.get("device", "-")
            self.monitor_status.config(
                text="⚠ AI 异常已上报：%s %s #%d %s → 告警 %s"
                     % (room, dev, behavior["pid"], behavior["behavior"],
                        alert.alert_id[-6:]),
                foreground="#c00")
        except tk.TclError:
            pass
        try:
            self.root.bell()
        except Exception:
            pass

    def _cam_stop(self, url, quiet=False):
        """停止一路监控。立刻返回：句柄释放走后台线程（卡住的流 release 也会阻塞）"""
        self._cam_running[url] = False
        reader = self._cam_readers.pop(url, None)
        if reader is not None:
            reader.stop("界面停止播放")
        self._cam_running.pop(url, None)
        after_id = self._cam_render_after.pop(url, None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass
        self._cam_photos.pop(url, None)
        self._cam_seen.pop(url, None)
        tracker = self._person_detectors.pop(url, None)   # 轨迹随停止清空，编号重新计
        if tracker is not None:
            try:
                tracker.stop()
            except Exception:
                pass
        if not quiet:
            self._cam_stopped_ui(url)

    def _cam_stopped_ui(self, url):
        cam = self._cam_by_url(url)
        if cam is None:
            return
        try:
            if cam.get("_label") and cam["_label"].winfo_exists():
                cam["_label"].config(image="")
            self._cam_btn_text(url, "▶ 播放")
            self._cam_set_status(url, "已停止", "#666")
            cam["_persons"] = []
            cam["_viol"] = 0
            cam["_ai_ts"] = 0.0
            cam["_ai_scale"] = None
            cam["_ai_line_y"] = None
            cam["_status_text"] = None
            cam["_density_level"] = DENSITY_LOW
            cam["_density_text"] = None
            density = cam.get("_density")
            if density is not None and density.winfo_exists():
                density.config(text="密度：-", foreground="#888")
            self._cam_set_border(cam, "idle")
            self._monitor_update_info_bar()
        except tk.TclError:
            pass

    def _cam_by_url(self, url):
        for c in self.monitors:
            if c.get("url") == url:
                return c
        return None

    def _cam_set_status(self, url, text, color):
        cam = self._cam_by_url(url)
        if cam is None:
            return
        if cam.get("_status_text") == text:
            return
        cam["_status_text"] = text
        if cam.get("_status") and cam["_status"].winfo_exists():
            try:
                cam["_status"].config(text=text, foreground=color)
            except tk.TclError:
                pass

    def _cam_btn_text(self, url, text):
        cam = self._cam_by_url(url)
        if cam and cam.get("_btn") and cam["_btn"].winfo_exists():
            cam["_btn"].config(text=text)

    def _monitor_play_all(self):
        urls = [c.get("url", "") for c in self.monitors if c.get("url")]
        if not urls:
            self.monitor_status.config(text="没有配置推流地址的摄像头")
            return
        for url in urls:
            self._cam_play(url)
        self._mon_log_line("全部播放：%d 路" % len(urls))
        self.monitor_status.config(
            text="已启动 %d 路摄像头（连接中，不通的会自动重连）" % len(urls))

    def _monitor_stop_all(self):
        urls = [c.get("url", "") for c in list(self.monitors) if c.get("url")]
        for url in urls:
            self._cam_stop(url, quiet=True)
        for url in urls:
            self._cam_stopped_ui(url)
        self._mon_log_line("全部停止：%d 路" % len(urls))
        self.monitor_status.config(text="已停止全部摄像头")

    def _monitor_manage(self):
        """管理摄像头配置（仅所有者）：考场编号、设备名称、监控区域、推流URL"""
        if self.session.role != config.ROLE_OWNER:
            messagebox.showerror("权限不足", "摄像头配置（新增/编辑/删除）仅所有者可调整。")
            return
        win = tk.Toplevel(self.root)
        win.title("管理摄像头")
        win.geometry("780x420")
        win.transient(self.root)
        win.grab_set()

        AREA_TEXT = {self.AREA_EXAM: "🎓 考场监考", self.AREA_OTHER: "🚪 非考场监控"}
        AREA_VALUE = {v: k for k, v in AREA_TEXT.items()}

        cols = ("room", "device", "area", "url")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=10)
        tree.heading("room", text="考场/区域编号")
        tree.heading("device", text="设备名称")
        tree.heading("area", text="监控区域")
        tree.heading("url", text="推流地址")
        tree.column("room", width=100)
        tree.column("device", width=120)
        tree.column("area", width=120)
        tree.column("url", width=360)
        tree.pack(fill="both", expand=True, padx=10, pady=6)

        def reload():
            for i in tree.get_children():
                tree.delete(i)
            for c in self.monitors:
                tree.insert("", "end", values=(c.get("room", ""),
                                               c.get("device", ""),
                                               AREA_TEXT.get(c.get("area", self.AREA_EXAM),
                                                             AREA_TEXT[self.AREA_EXAM]),
                                               c.get("url", "")))
        reload()

        def add_or_edit(item=None):
            if item is None:
                old = ("", "", AREA_TEXT[self.AREA_EXAM], "")
            else:
                old = tuple(tree.item(item)["values"])
            dlg = tk.Toplevel(win)
            dlg.title("摄像头" + ("编辑" if item else "新增"))
            dlg.geometry("520x230")
            dlg.transient(win)
            dlg.grab_set()
            f = ttk.Frame(dlg, padding=12)
            f.pack(fill="both", expand=True)
            v_room = tk.StringVar(value=old[0])
            v_dev = tk.StringVar(value=old[1])
            v_area = tk.StringVar(value=old[2])
            v_url = tk.StringVar(value=old[3])
            ttk.Label(f, text="考场/区域编号：").grid(row=0, column=0, sticky="e", pady=4)
            ttk.Entry(f, textvariable=v_room, width=46).grid(row=0, column=1, sticky="w")
            ttk.Label(f, text="设备名称：").grid(row=1, column=0, sticky="e", pady=4)
            ttk.Entry(f, textvariable=v_dev, width=46).grid(row=1, column=1, sticky="w")
            ttk.Label(f, text="监控区域：").grid(row=2, column=0, sticky="e", pady=4)
            cb = ttk.Combobox(f, textvariable=v_area, state="readonly", width=43,
                              values=[AREA_TEXT[self.AREA_EXAM], AREA_TEXT[self.AREA_OTHER]])
            cb.grid(row=2, column=1, sticky="w")
            ttk.Label(f, text="　考场监考：AI 辅助识别逐人行为；非考场监控：纯视频，无 AI",
                      foreground="#888", font=("Microsoft YaHei", 9)
                      ).grid(row=3, column=0, columnspan=2, sticky="w")
            ttk.Label(f, text="推流地址：").grid(row=4, column=0, sticky="e", pady=4)
            ttk.Entry(f, textvariable=v_url, width=46).grid(row=4, column=1, sticky="w")

            def save():
                room, device, url = v_room.get().strip(), v_dev.get().strip(), v_url.get().strip()
                area = AREA_VALUE.get(v_area.get().strip(), self.AREA_EXAM)
                if not room or not device or not url:
                    messagebox.showwarning("提示", "考场/区域编号、设备名称、推流地址均不能为空")
                    return
                new = {"room": room, "device": device, "url": url, "area": area}
                if item is None:
                    self.monitors.append(new)
                else:
                    idx = tree.index(item)
                    old_cam = self.monitors[idx]
                    new["_label"] = old_cam.get("_label")
                    new["_status"] = old_cam.get("_status")
                    new["_btn"] = old_cam.get("_btn")
                    self.monitors[idx] = new
                self._monitor_save()
                reload()
                dlg.destroy()

            bbar = ttk.Frame(f)
            bbar.grid(row=5, column=0, columnspan=2, pady=10)
            ttk.Button(bbar, text="保存", command=save).pack(side="left", padx=6)
            ttk.Button(bbar, text="取消", command=dlg.destroy).pack(side="left", padx=6)

        def do_add():
            add_or_edit()

        def do_edit():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先选择一行")
                return
            add_or_edit(sel[0])

        def do_del():
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            url = self.monitors[idx].get("url", "")
            self._cam_stop(url)
            self._cam_latest.pop(url, None)
            del self.monitors[idx]
            self._monitor_save()
            reload()

        def on_close():
            self._monitor_stop_all()
            self._monitor_render_grid()
            win.destroy()

        bbar = ttk.Frame(win)
        bbar.pack(fill="x", padx=10, pady=6)
        ttk.Button(bbar, text="＋ 新增", command=do_add).pack(side="left")
        ttk.Button(bbar, text="✏ 编辑", command=do_edit).pack(side="left", padx=6)
        ttk.Button(bbar, text="🗑 删除", command=do_del).pack(side="left")
        ttk.Button(bbar, text="关闭", command=on_close).pack(side="right")

        win.protocol("WM_DELETE_WINDOW", on_close)

    def refresh_monitor(self):
        """监控页状态由视频线程自行维护，这里只保证网格与配置一致"""
        pass

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
        ttk.Button(bar, text="🔄 刷新状态", command=self.refresh_ops).pack(side="left")
        ttk.Button(bar, text="📝 写一次稳定性心跳", command=self._soak).pack(side="left", padx=6)
        ttk.Button(bar, text="🔗 校验审计链", command=self._chain).pack(side="left", padx=6)
        # 终端账户管理：只有具备 manage 动作的角色（所有者）才看得到
        if self.session.can("manage"):
            ttk.Button(bar, text="👥 终端账户", command=self._show_accounts
                       ).pack(side="left", padx=6)
            ttk.Button(bar, text="🗑 清空本地数据", command=self._wipe_all
                       ).pack(side="left", padx=6)

        # 权限控制台：把所有改变权限/配置的开关集中在一个带标题的框里，避免误点
        perm_frame = ttk.LabelFrame(f, text=" 权限控制台（仅所有者可见）", padding=8)
        perm_frame.pack(fill="x", pady=(4, 0))
        pbar = ttk.Frame(perm_frame)
        pbar.pack(fill="x")
        self.admin_chk = None      # 非所有者时不创建；重建界面后必须清掉旧引用
        self.console_on = None
        self.console_admin = None
        self.ai_chat_chk = None
        self.hide_cmd = tk.BooleanVar(value=os.path.exists(str(config.HIDE_CMD_FLAG)))
        if self.session.can("manage"):
            self.console_on = tk.BooleanVar(value=console.console_enabled())
            ttk.Checkbutton(pbar, text="☐ 命令控制台总开关",
                            variable=self.console_on,
                            command=self._toggle_console).pack(side="left")
            self.console_admin = tk.BooleanVar(value=console.console_admin_allowed())
            self.admin_chk = ttk.Checkbutton(
                pbar, text="☐ 授权管理员 ollama",
                variable=self.console_admin, command=self._toggle_console_admin)
            self.admin_chk.pack(side="left", padx=12)
            self.ai_chat_on = tk.BooleanVar(value=console.ai_chat_allowed())
            self.ai_chat_chk = ttk.Checkbutton(
                pbar, text="☐ 允许非所有者 AI 对话",
                variable=self.ai_chat_on, command=self._toggle_ai_chat)
            self.ai_chat_chk.pack(side="left", padx=12)
            ttk.Checkbutton(pbar, text="☐ 隐藏命令行窗口（下次启动生效）",
                            variable=self.hide_cmd,
                            command=self._toggle_hide_cmd).pack(side="left", padx=12)
            # 生产演示告警：仅所有者可见可点（演练用，不冒充真实接入）
            ttk.Button(pbar, text="🧪 生成演示告警",
                       command=self._demo).pack(side="left", padx=12)
        self.console_btn = ttk.Button(pbar, text="🖥 打开命令控制台",
                                      command=self._open_console)
        self.console_hint = ttk.Label(pbar, text="", foreground="#666")
        self.console_hint.pack(side="left", padx=8)
        self._refresh_console_ui()

    def _toggle_hide_cmd(self):
        """所有者开关：是否隐藏启动器命令行窗口（写标记文件，由 .bat 读取）"""
        on = self.hide_cmd.get()
        ok_report, _ = self._report_sensitive_action(
            "隐藏命令行窗口", "%s" % ("开启" if on else "关闭"))
        if not ok_report:
            self.hide_cmd.set(os.path.exists(str(config.HIDE_CMD_FLAG)))
            return
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

    # ---------- 敏感权限操作上报（owner 直接执行，其它角色需上报说明） ----------
    def _report_sensitive_action(self, action_name, detail=""):
        """
        对改变权限/配置/数据的高危操作进行上报：
          · owner 拥有最高权限，直接记审计即可；
          · 其它角色必须填写上报说明，否则拒绝执行。
        返回 (ok, report_note)。
        """
        if self.session.role == config.ROLE_OWNER:
            return True, ""
        note = self._ask_text("权限操作上报",
                              "[%s] 需要向上级/所有者上报。\n请填写操作说明：" % action_name)
        if note is None or not note.strip():
            messagebox.showwarning("已取消", "%s 需要上报说明，未填写则取消执行。" % action_name)
            return False, ""
        # 写审计并尝试推送平台（有配置时）
        full = "%s | %s | %s" % (action_name, detail, note.strip())
        self.store.log("权限操作上报", full, self.session.username)
        try:
            link = bridge.PlatformLink()
            if link.connectivity():
                link.post_report({"action": action_name, "detail": detail,
                                  "note": note.strip(), "operator": self.session.username})
        except Exception:
            pass
        return True, note.strip()

    # ---------- 命令控制台（默认关闭；管理员需所有者单独授权） ----------
    def _toggle_console(self):
        """
        所有者开关：命令控制台总开关。

        开启前必须二次确认：它不是普通开关，而是"把本机控制权交给登录所有者账户的人"。
        用户取消时把复选框状态改回去，避免界面显示"开着"而实际没开。
        """
        on = self.console_on.get()
        if on and not messagebox.askyesno(
                "高风险操作",
                "开启「命令控制台」后，登录所有者账户的人可以在本终端执行\n"
                "任意系统命令与 Python 代码，等同于本机（终端）的完全控制权。\n\n"
                "· 每一次执行（包括被拒绝的尝试）都会写入审计留痕；\n"
                "· 关闭总开关会同时停用管理员的 AI 辅助入口；\n"
                "· 出厂默认口令是公开的，部署前请先改掉所有者口令。\n\n"
                "确定开启？"):
            self.console_on.set(False)          # 取消 -> 回退勾选，不留"看起来开着"的假象
            return
        ok_report, _ = self._report_sensitive_action(
            "命令控制台总开关", "设置为 %s" % ("开启" if on else "关闭"))
        if not ok_report:
            self.console_on.set(console.console_enabled())
            return
        ok, msg = console.set_console(on)
        if not ok:
            self.console_on.set(console.console_enabled())
            messagebox.showerror("设置失败", msg)
            return
        self.store.log("命令控制台总开关", "开启" if on else "关闭", self.session.username)
        self._refresh_console_ui()
        messagebox.showinfo("已设置", msg)

    def _toggle_console_admin(self):
        """所有者开关：单独授权管理员使用 ollama AI 辅助（总开关关闭时授权不生效）"""
        allow = self.console_admin.get()
        ok_report, _ = self._report_sensitive_action(
            "命令控制台管理员授权", "%s" % ("授权" if allow else "收回"))
        if not ok_report:
            self.console_admin.set(console.console_admin_allowed())
            return
        ok, msg = console.set_console_admin(allow)
        if not ok:
            self.console_admin.set(console.console_admin_allowed())
            messagebox.showerror("设置失败", msg)
            return
        self.store.log("命令控制台管理员授权", "授权" if allow else "收回", self.session.username)
        self._refresh_console_ui()
        messagebox.showinfo("已设置", msg)

    def _toggle_ai_chat(self):
        """所有者开关：授权非所有者角色（管理员/巡考员等）使用 AI 多轮对话"""
        allow = self.ai_chat_on.get()
        ok_report, _ = self._report_sensitive_action(
            "AI 对话授权", "%s" % ("授权" if allow else "收回"))
        if not ok_report:
            self.ai_chat_on.set(console.ai_chat_allowed())
            return
        ok, msg = console.set_ai_chat(allow)
        if not ok:
            self.ai_chat_on.set(console.ai_chat_allowed())
            messagebox.showerror("设置失败", msg)
            return
        self.store.log("AI 对话授权", "授权" if allow else "收回", self.session.username)
        self._refresh_console_ui()
        messagebox.showinfo("已设置", msg)

    def _refresh_console_ui(self):
        """
        按"当前角色 + 开关状态"刷新入口按钮与提示。

        每次开关变动都要重算：管理员能不能看到入口，取决于所有者刚拨的总开关，
        不重算就会出现"开关关了按钮还在"（或反之）的错位。
        """
        try:
            st = console.console_status(self.session.role)
            if self.admin_chk is not None:
                # 总开关关闭时授权无从生效，把复选框置灰，免得误解为"授权了就能用"
                self.admin_chk.config(state=("normal" if st["enabled"] else "disabled"))
            if self.ai_chat_chk is not None:
                # AI 对话授权开关：总开关关时置灰；所有者始终能 AI 对话，但复选框只控制非所有者
                self.ai_chat_chk.config(state=("normal" if st["enabled"] else "disabled"))
            # 命令控制台按钮只按命令控制台层级显示：AI 对话走底部常驻面板，不混在这里
            if st["tier"] != console.CONSOLE_TIER_NONE:
                self.console_btn.pack(side="left", padx=6)
            else:
                self.console_btn.pack_forget()
            self.console_hint.config(text=st["hint"],
                                     foreground=("#0a7d28" if st["can_run"] else "#666"))
        except tk.TclError:
            pass        # 界面正在销毁（切换账号 / 关窗）时的竞态，忽略即可

    def _open_console(self):
        """打开命令控制台窗口（类型、命令、超时、执行、输出；新增 AI 对话模式）"""
        tier = console.console_tier(self.session.role)
        ai_chat_ok = console.can_ai_chat(self.session.role)
        if tier == console.CONSOLE_TIER_NONE and not ai_chat_ok:
            messagebox.showerror("权限不足",
                                 "命令控制台未开启，或当前角色（%s）不在允许范围内。"
                                 % config.ROLE_TEXT.get(self.session.role, self.session.role))
            return
        modes = console.console_modes(self.session.role)
        available, why = self.cmd_bridge.available()

        import ai_assistant

        win = tk.Toplevel(self.root)
        win.title("命令控制台 · %s" % console.TIER_TEXT.get(tier, tier))
        win.geometry("920x660")
        win.transient(self.root)

        head = ttk.Frame(win, padding=(12, 10, 12, 2))
        head.pack(fill="x")
        ttk.Label(head, text=console.TIER_TEXT.get(tier, tier), font=FONT_B).pack(anchor="w")
        ttk.Label(head, text=console.RISK_HINT, foreground="#c07000", wraplength=880,
                  justify="left").pack(anchor="w", pady=(2, 0))
        if tier != console.CONSOLE_TIER_NONE:
            ttk.Label(head, text=("桥接状态：" + ("✅ 可用" if available else "⚠ " + why)),
                      foreground=("#0a7d28" if available else "#c00000"), wraplength=880,
                      justify="left").pack(anchor="w", pady=(2, 0))

        g = ttk.Frame(win, padding=(12, 6))
        g.pack(fill="x")
        ttk.Label(g, text="类型：").grid(row=0, column=0, sticky="e")
        mode_var = tk.StringVar(value=console.MODE_TEXT[modes[0]])
        combo = ttk.Combobox(g, textvariable=mode_var, width=18, state="readonly",
                             values=[console.MODE_TEXT[m] for m in modes])
        combo.grid(row=0, column=1, sticky="w")
        if tier != console.CONSOLE_TIER_FULL:
            # 非所有者时选择框禁用，避免"看起来能选系统命令"的误解；
            # AI 对话模式已通过 console_modes 控制可选值。
            combo.config(state="disabled")

        ttk.Label(g, text="模型：").grid(row=0, column=2, sticky="e", padx=(12, 0))
        ai_model_var = tk.StringVar(value=ai_assistant.DEFAULT_MODEL)
        ai_model_box = ttk.Combobox(g, textvariable=ai_model_var, width=30,
                                    values=(ai_assistant.DEFAULT_MODEL,))
        ai_model_box.grid(row=0, column=3, sticky="w")
        ttk.Button(g, text="🔄", width=3,
                   command=lambda: self._ai_refresh_models_popup(ai_model_box)).grid(row=0, column=4, padx=(2, 0))

        ttk.Label(g, text="超时（秒）：").grid(row=0, column=5, sticky="e", padx=(12, 0))
        to_var = tk.StringVar(value=str(console.CONSOLE_DEFAULT_TIMEOUT))
        ttk.Entry(g, textvariable=to_var, width=8).grid(row=0, column=6, sticky="w")
        ttk.Label(g, text="最长 %d 秒" % console.CONSOLE_MAX_TIMEOUT, foreground="#888"
                  ).grid(row=0, column=7, sticky="w", padx=(6, 0))

        ttk.Label(win, text="命令 / 代码 / AI 消息（所有者：系统命令或 Python 代码；"
                            "管理员：ollama run；授权角色：AI 对话；输入 EDY_POWERQ 切换自由对话）",
                  foreground="#333").pack(anchor="w", padx=12, pady=(4, 2))
        src = tk.Text(win, height=8, wrap="word", font=MONO)
        src.pack(fill="x", padx=12)
        bar = ttk.Frame(win, padding=(12, 6))
        bar.pack(fill="x")
        run_btn = ttk.Button(bar, text="▶ 执行", command=lambda: run())
        run_btn.pack(side="left")
        ttk.Button(bar, text="清空输出", command=lambda: set_out("（已清空）")).pack(side="left", padx=6)
        ttk.Label(bar, text="执行目录：%s（子进程与本程序同用户身份，无额外降权隔离）"
                            % config.CMD_CONSOLE_DIR, foreground="#888").pack(side="left", padx=8)
        out = tk.Text(win, height=18, wrap="word", font=MONO, state="normal")
        out.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        # AI 对话状态
        ai_chat = ai_assistant.AIChat(model=ai_model_var.get())
        ai_free_var = tk.BooleanVar(value=False)
        ai_busy = [False]

        def set_out(text):
            out.config(state="normal")
            out.delete("1.0", "end")
            out.insert("end", text)
            out.see("end")
            out.config(state="disabled")

        def append_out(text):
            out.config(state="normal")
            if out.get("1.0", "end").strip() in ("（尚未执行）", "（已清空）"):
                out.delete("1.0", "end")
            out.insert("end", text.rstrip() + "\n")
            out.see("end")
            out.config(state="disabled")

        def remove_thinking():
            out.config(state="normal")
            text = out.get("1.0", "end")
            lines = text.split("\n")
            for i in range(len(lines) - 1, -1, -1):
                if "正在思考，请稍候" in lines[i]:
                    start = "%d.0" % (i + 1)
                    end = "%d.end+1c" % (i + 1)
                    out.delete(start, end)
                    break
            out.config(state="disabled")

        def sync_to_main(text):
            try:
                box = getattr(self, "ops_right", None)
                if box and box.winfo_exists():
                    box.insert("end", text + "\n")
                    box.see("end")
            except Exception:
                pass

        def ai_toggle_free():
            on = not ai_free_var.get()
            ai_free_var.set(on)
            append_out("🤖 %s" % ("已进入自由对话模式，提示词限制已关闭。"
                                   if on else "已退出自由对话模式，恢复智能终端助手提示词。"))

        def run():
            code = src.get("1.0", "end").strip()
            if not code:
                messagebox.showinfo("提示", "请输入命令、代码或 AI 消息", parent=win)
                return
            try:
                timeout = int(float(to_var.get() or console.CONSOLE_DEFAULT_TIMEOUT))
            except ValueError:
                messagebox.showerror("参数错误", "超时必须是整数秒", parent=win)
                return
            timeout = max(1, min(timeout, console.CONSOLE_MAX_TIMEOUT))
            mode = console.MODE_BY_TEXT.get(mode_var.get(), "cmd")

            # ---------- AI 对话模式 ----------
            if mode == "ai":
                text = code
                if text == "EDY_POWERQ":
                    ai_toggle_free()
                    src.delete("1.0", "end")
                    return
                if ai_busy[0]:
                    append_out("⚠ 上一条 AI 请求还未完成，请稍候再试。")
                    return
                nonlocal ai_chat
                model_want = ai_model_var.get().strip() or ai_assistant.DEFAULT_MODEL
                if ai_chat.model != model_want:
                    ai_chat = ai_assistant.AIChat(model=model_want)
                ai_busy[0] = True
                run_btn.config(state="disabled")
                append_out("🧑 你：%s" % text)
                append_out("🤖 AI 正在思考，请稍候…")
                src.delete("1.0", "end")

                def ai_worker():
                    try:
                        ok, reply, transport = ai_chat.ask(
                            text, operator=self.session.username,
                            tier=("full" if tier == console.CONSOLE_TIER_FULL else "ai"),
                            source="terminal_gui", timeout=timeout,
                            free_mode=bool(ai_free_var.get()),
                            interface=self._current_interface())
                    except Exception as e:
                        ok, reply, transport = False, "AI 调用异常：%s" % e, ""

                    def ai_apply():
                        try:
                            if not win.winfo_exists():
                                return
                        except tk.TclError:
                            return
                        ai_busy[0] = False
                        remove_thinking()
                        line = "%s：%s" % ("🤖 AI" if ok else "⚠ AI 调用失败", reply)
                        append_out(line)
                        sync_to_main(line)
                        run_btn.config(state="normal")
                    try:
                        win.after(0, ai_apply)
                    except tk.TclError:
                        pass
                threading.Thread(target=ai_worker, daemon=True).start()
                return

            # ---------- 命令 / Python / ollama 单次 ----------
            run_btn.config(state="disabled")
            out.delete("1.0", "end")
            out.insert("end", "执行中…（最长 %d 秒，超时会连带结束子进程树）\n" % timeout)
            out.see("end")

            def done(r):
                # 回到 UI 线程后再碰控件；窗口可能已经被关掉，所以两处都要判断
                if not win.winfo_exists():
                    return
                try:
                    run_btn.config(state="normal")
                    out.insert("end", format_console_result(r))
                    out.see("end")
                except tk.TclError:
                    pass

            def work():
                # 命令最长 600 秒：必须离开 UI 线程，否则整个界面会卡死（红线要求）
                try:
                    r = self.cmd_bridge.execute(code, mode=mode, role=self.session.role,
                                                operator=self.session.username,
                                                timeout=timeout, source="terminal_gui")
                except Exception as e:              # 兜底：线程里抛异常会让界面永远停在"执行中"
                    r = {"ok": False, "tier": tier, "mode": mode, "exit_code": None,
                         "stdout": "", "stderr": "%s: %s" % (type(e).__name__, e),
                         "duration_ms": 0, "timed_out": False, "truncated": False,
                         "reason": "执行线程异常：%s" % e}
                try:
                    win.after(0, lambda: done(r))
                except (tk.TclError, RuntimeError):
                    pass

            threading.Thread(target=work, daemon=True).start()

        src.focus_set()

    def _ai_refresh_models_popup(self, box):
        """给命令控制台弹窗里的模型下拉框刷新模型列表"""
        import ai_assistant

        def work():
            try:
                names = ai_assistant.list_models()
            except Exception:
                return

            def apply():
                try:
                    if not box.winfo_exists():
                        return
                    cur = box.get().strip()
                    vals = tuple(names)
                    if cur and cur not in names:
                        vals = (cur,) + vals
                    box.config(values=vals)
                    if not cur and names:
                        box.set(names[0])
                except tk.TclError:
                    pass
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    # ---------- 常驻 AI 对话面板（Notebook 下方，跨标签可见） ----------
    INTERFACE_LABELS = {
        "alerts": "告警处置",
        "review": "复核留痕",
        "verify": "手持核验",
        "monitor": "监控查看",
        "ops": "运行状态与管控",
    }

    def _current_interface(self):
        """当前页签对应的界面标识：AI 提示词跟着界面走"""
        try:
            cur = self.nb.select()
            w = self.root.nametowidget(cur) if cur else None
        except Exception:
            return "alerts"
        for widget, key in ((getattr(self, "tab_alerts", None), "alerts"),
                            (getattr(self, "tab_review", None), "review"),
                            (getattr(self, "tab_verify", None), "verify"),
                            (getattr(self, "tab_monitor", None), "monitor"),
                            (getattr(self, "tab_ops", None), "ops")):
            if widget is not None and widget is w:
                return key
        return "alerts"

    def _refresh_ai_mode_label(self):
        lbl = getattr(self, "ai_mode_label", None)
        if lbl is None or not lbl.winfo_exists():
            return
        if getattr(self, "ai_free_var", None) is not None and self.ai_free_var.get():
            text = "自由对话"
        else:
            key = self._current_interface()
            text = "智能终端助手 · %s" % self.INTERFACE_LABELS.get(key, key)
        lbl.config(text="模式：%s" % text)

    def _on_tab_changed(self, _event=None):
        """切换页签：刷新 AI 模式标签，并让下次自动分析重新触发"""
        self._ai_last_snapshot = ""
        try:
            self._refresh_ai_mode_label()
        except Exception:
            pass

    def _build_ai_panel(self, parent):
        import ai_assistant

        self.ai_chat = ai_assistant.AIChat()
        self.ai_free_var = tk.BooleanVar(value=False)
        self.ai_busy = False

        frame = ttk.LabelFrame(parent, text="🤖 AI 助手（本地 ollama）", padding=6)
        frame.pack(fill="x", padx=10, pady=(0, 6))

        allowed = console.can_ai_chat(self.session.role)
        if not allowed:
            ttk.Label(frame, text="AI 对话未授权：需所有者开启命令控制台总开关并勾选「允许非所有者使用 AI 对话」。",
                      foreground="#888").pack(anchor="w")
            return

        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="模型：").pack(side="left")
        self.ai_model_var = tk.StringVar(value=ai_assistant.DEFAULT_MODEL)
        self.ai_model_box = ttk.Combobox(
            top, textvariable=self.ai_model_var, width=30,
            values=(ai_assistant.DEFAULT_MODEL,))
        self.ai_model_box.pack(side="left")
        ttk.Button(top, text="🔄", width=3,
                   command=self._ai_refresh_models).pack(side="left", padx=(2, 8))
        mode_text = "自由对话" if self.ai_free_var.get() else "智能终端助手"
        self.ai_mode_label = ttk.Label(top, text="模式：%s" % mode_text,
                                       foreground="#1f56b8")
        self.ai_mode_label.pack(side="left", padx=(12, 0))
        # 提示词跟随页签：切换界面时刷新模式标签
        try:
            self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        except Exception:
            pass
        self._refresh_ai_mode_label()

        self.ai_watch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="AI 自动分析（无对话时自动分析状态）",
                        variable=self.ai_watch_var).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="立即分析", command=self._ai_force_analyze).pack(side="right")
        ttk.Button(top, text="清空记录", command=self._ai_clear).pack(side="right", padx=(0, 6))

        self.ai_out = tk.Text(frame, height=6, wrap="word", font=MONO,
                              background="#0f172a", foreground="#e2e8f0")
        self.ai_out.pack(fill="x", pady=(4, 0))
        self.ai_out.tag_config("ai_user", foreground="#60a5fa")
        self.ai_out.tag_config("ai_bot", foreground="#34d399")
        self.ai_out.tag_config("ai_err", foreground="#fca5a5")
        self.ai_out.tag_config("ai_dim", foreground="#64748b")
        self.ai_out.insert("end", "🤖 AI 助手就绪\n")
        self.ai_out.config(state="disabled")

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(4, 0))
        self.ai_input = tk.Text(bar, height=2, wrap="word", font=FONT)
        self.ai_input.pack(side="left", fill="x", expand=True)
        self.ai_input.bind("<Return>", lambda e: (self._ai_send(), "break")[1])
        self.ai_send_btn = ttk.Button(bar, text="发送", command=self._ai_send)
        self.ai_send_btn.pack(side="left", padx=(6, 0))

        # 启动 AI 自动分析守护（无对话且状态变化时触发）
        self._ai_last_snapshot = ""
        self.root.after(2000, self._ai_watch_tick)

    def _ai_force_analyze(self):
        """手动强制触发一次 AI 自动分析（用户等不及自动轮询时点）"""
        self._ai_last_snapshot = ""   # 强制认为状态有变化
        self._ai_watch_maybe()

    def _ai_watch_tick(self):
        """AI 巡视调度：无对话且开启自动分析时，定期请 AI 分析当前状态"""
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            self._ai_watch_maybe()
        except Exception as e:
            # 不要把异常吞掉，否则用户永远不知道为什么不自动分析
            self._ai_append_out("⚠ AI 自动分析调度异常：%s" % e, tag="ai_err")
        try:
            self.root.after(10000, self._ai_watch_tick)
        except (tk.TclError, RuntimeError):
            pass

    def _ai_watch_maybe(self):
        if not self.ai_watch_var.get():
            return
        if self.ai_busy:
            return
        if not console.can_ai_chat(self.session.role):
            self._ai_append_out("⚠ AI 自动分析未启用：当前角色未获得 AI 对话授权。",
                                tag="ai_err")
            return
        try:
            snapshot = self._ai_snapshot()
        except Exception as e:
            self._ai_append_out("⚠ 生成状态快照失败：%s" % e, tag="ai_err")
            return
        if snapshot == self._ai_last_snapshot:
            return
        self._ai_last_snapshot = snapshot
        import ai_assistant
        model = self.ai_model_var.get().strip() or ai_assistant.DEFAULT_MODEL
        if self.ai_chat.model != model:
            self.ai_chat = ai_assistant.AIChat(model=model)
        self.ai_busy = True
        self.ai_send_btn.config(state="disabled")
        self._ai_append_out("🔎 AI 自动分析：状态有变化，正在分析…", tag="ai_dim")

        def work():
            try:
                ok, reply, transport = ai_assistant.analyze_activity(
                    snapshot, model=model, operator=self.session.username,
                    tier=("full" if self.session.role == config.ROLE_OWNER else "ai"),
                    source="terminal_watch", timeout=60,
                    interface=self._current_interface())
            except Exception as e:
                ok, reply = False, "AI 自动分析异常：%s" % e

            def apply():
                self.ai_busy = False
                self.ai_send_btn.config(state="normal")
                self._ai_remove_thinking()
                if ok:
                    self._ai_append_out("🔎 AI 自动分析：\n%s" % reply, tag="ai_bot")
                else:
                    self._ai_append_out("⚠ AI 自动分析失败：%s" % reply, tag="ai_err")
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _ai_snapshot(self):
        """终端当前状态快照（供 AI 自动分析使用）"""
        lines = ["时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
        lines.append("操作员：%s（%s）" % (self.session.username,
                                          config.ROLE_TEXT.get(self.session.role, self.session.role)))
        try:
            key = self._current_interface()
            lines.append("当前界面：%s" % self.INTERFACE_LABELS.get(key, key))
        except Exception:
            pass
        try:
            st = self.outbox.status()
            lines.append("平台在线：%s　待补传：%d" % ("是" if st.get("online") else "否",
                                                      st.get("pending", 0)))
        except Exception:
            pass
        try:
            alerts = self.store.list_alerts(limit=1000)
            red = sum(1 for a in alerts if a.level == config.LEVEL_RED)
            orange = sum(1 for a in alerts if a.level == config.LEVEL_ORANGE)
            pending = sum(1 for a in alerts if a.status == models.ST_PENDING)
            acked = sum(1 for a in alerts if a.status == models.ST_ACKED)
            reviewed = sum(1 for a in alerts if a.status == models.ST_REVIEWED)
            lines.append("告警统计：红 %d　橙 %d　待处置 %d　已确认 %d　已复核 %d" %
                         (red, orange, pending, acked, reviewed))
        except Exception:
            pass
        try:
            revs = self.store.list_reviews(limit=20)
            if revs:
                lines.append("最近复核：%d 条" % len(revs))
        except Exception:
            pass
        try:
            logs = self.store.audit_rows(limit=5)
            if logs:
                lines.append("最近操作：")
                for r in logs:
                    lines.append("  %s %s" % (r.get("action"), r.get("operator") or "-"))
        except Exception:
            pass
        # ---- 按界面补充"AI 真正需要知道的现场数据" ----
        key = ""
        try:
            key = self._current_interface()
        except Exception:
            pass
        if key == "alerts":
            try:
                sel = self.tree.selection()
                if sel:
                    vals = self.tree.item(sel[0])["values"]
                    lines.append("当前选中的告警：编号=%s 级别=%s 告警码=%s 考场=%s 状态=%s"
                                 % tuple(list(vals)[:5]))
            except Exception:
                pass
        elif key == "monitor":
            try:
                cams = getattr(self, "monitors", []) or []
                exam = sum(1 for c in cams
                           if c.get("area", self.AREA_EXAM) == self.AREA_EXAM)
                other = len(cams) - exam
                playing = sum(1 for v in getattr(self, "_cam_running", {}).values() if v)
                ai_on = bool(getattr(self, "mon_ai", None) and self.mon_ai.get())
                lines.append("摄像头：考场监考 %d 路，非考场监控 %d 路，正在播放 %d 路"
                             % (exam, other, playing))
                lines.append("AI 框选识别：%s（仅考场监考生效）" % ("开启" if ai_on else "关闭"))
            except Exception:
                pass
        elif key == "ops":
            try:
                off = self.clock.offset_sec() if hasattr(self.clock, "offset_sec") else None
                if off is not None:
                    lines.append("考场时钟偏差：%.1f 秒" % off)
            except Exception:
                pass
        return "\n".join(lines)

    def _ai_clear(self):
        """清空 AI 对话记录与历史"""
        try:
            self.ai_out.config(state="normal")
            self.ai_out.delete("1.0", "end")
            self.ai_out.insert("end", "🤖 记录已清空\n")
            self.ai_out.see("end")
            self.ai_out.config(state="disabled")
        except tk.TclError:
            pass
        import ai_assistant
        self.ai_chat = ai_assistant.AIChat(model=self.ai_model_var.get().strip()
                                           or ai_assistant.DEFAULT_MODEL)

    def _ai_refresh_models(self):
        import ai_assistant

        def work():
            try:
                names = ai_assistant.list_models()
            except Exception:
                return

            def apply():
                if not self.ai_model_box.winfo_exists():
                    return
                try:
                    cur = self.ai_model_var.get().strip()
                    vals = tuple(names)
                    if cur and cur not in names:
                        vals = (cur,) + vals
                    self.ai_model_box.config(values=vals)
                    if not cur and names:
                        self.ai_model_var.set(names[0])
                except tk.TclError:
                    pass
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _ai_send(self):
        if self.ai_busy:
            self._ai_append_out("⚠ 上一条 AI 请求还未完成，请稍候再试。", tag="ai_err")
            return
        text = self.ai_input.get("1.0", "end").strip()
        if not text:
            return
        import ai_assistant
        model = self.ai_model_var.get().strip() or ai_assistant.DEFAULT_MODEL
        if self.ai_chat.model != model:
            self.ai_chat = ai_assistant.AIChat(model=model)

        if text == "EDY_POWERQ":
            self._ai_toggle_free_mode()
            self.ai_input.delete("1.0", "end")
            return

        self.ai_busy = True
        self.ai_send_btn.config(state="disabled")
        self._ai_append_out("🧑 你：%s" % text, tag="ai_user")
        self._ai_append_out("🤖 AI 正在思考，请稍候…", tag="ai_dim")
        self.ai_input.delete("1.0", "end")

        free_mode = bool(self.ai_free_var.get())
        interface = self._current_interface()

        def work():
            try:
                ok, reply, transport = self.ai_chat.ask(
                    text, operator=self.session.username,
                    tier=("full" if self.session.role == config.ROLE_OWNER else "ai"),
                    source="terminal_panel", timeout=120, free_mode=free_mode,
                    interface=interface)
            except Exception as e:
                ok, reply, transport = False, "AI 调用异常：%s" % e, ""

            def apply():
                self.ai_busy = False
                self.ai_send_btn.config(state="normal")
                self._ai_remove_thinking()
                line = "%s：%s" % ("🤖 AI" if ok else "⚠ AI 调用失败", reply)
                self._ai_append_out(line, tag=("ai_bot" if ok else "ai_err"))
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _ai_toggle_free_mode(self):
        on = not self.ai_free_var.get()
        self.ai_free_var.set(on)
        self._refresh_ai_mode_label()
        self._ai_append_out("🤖 %s" % ("已进入自由对话模式，提示词限制已关闭。"
                                       if on else "已退出自由对话模式，恢复智能终端助手提示词。"),
                            tag="ai_bot")

    def _ai_append_out(self, text, tag=None):
        try:
            self.ai_out.config(state="normal")
            if tag:
                self.ai_out.insert("end", text.rstrip() + "\n", tag)
            else:
                self.ai_out.insert("end", text.rstrip() + "\n")
            self.ai_out.see("end")
            self.ai_out.config(state="disabled")
        except tk.TclError:
            pass

    def _ai_remove_thinking(self):
        try:
            self.ai_out.config(state="normal")
            txt = self.ai_out.get("1.0", "end")
            lines = txt.split("\n")
            for i in range(len(lines) - 1, -1, -1):
                if "正在思考，请稍候" in lines[i]:
                    start = "%d.0" % (i + 1)
                    end = "%d.end+1c" % (i + 1)
                    self.ai_out.delete(start, end)
                    break
            self.ai_out.config(state="disabled")
        except Exception:
            pass

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
                ok_report, _ = self._report_sensitive_action(
                    "新建终端账户", "用户名=%s 角色=%s" % (v["username"].get(), role_v.get()))
                if not ok_report:
                    return
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
            ok_report, _ = self._report_sensitive_action("重置终端账户口令", "用户名=%s" % u)
            if not ok_report:
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
            ok_report, _ = self._report_sensitive_action("删除终端账户", "用户名=%s" % u)
            if not ok_report:
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
        """所有者：清空本地运行数据（告警/复核/队列/账户）——需二次确认+非所有者上报"""
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
        ok_report, note = self._report_sensitive_action("清空本地数据", "WIPE 二次确认通过")
        if not ok_report:
            return
        self.store.log("清空本地数据", "手动执行" + (" | 上报说明：%s" % note if note else ""),
                       self.session.username)
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
        if self.can_monitor:
            self.refresh_monitor()
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
            code_text = config.ALERT_CODE_TEXT.get(a.code, a.code)
            self.tree.insert("", "end", iid=a.alert_id, tags=(tag,),
                             values=(a.alert_id, a.level_name(), a.created_at,
                                     "%s（%s）" % (a.code, code_text),
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

    def _bind_mousewheel(self, widget):
        """给可滚动控件绑定鼠标滚轮"""
        def on_wheel(event):
            # Windows 滚轮事件 delta 是 120 的倍数
            widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        widget.bind("<MouseWheel>", on_wheel)
        # 鼠标进入/离开时可获取焦点，滚轮更稳定
        widget.bind("<Enter>", lambda _e: widget.focus_set())

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
        if not messagebox.askyesno("确认告警",
                                   "确定要确认告警 %s 吗？\n确认后才可复核。" % a.alert_id):
            return
        reason = ""
        if a.level == config.LEVEL_RED:
            reason = self._ask_text("红色预警确认", "红色预警必须填写确认理由：")
            if reason is None or not reason.strip():
                messagebox.showwarning("已取消", "红色预警确认必须填写理由。")
                return
        ok, msg = self.hub.ack(a.alert_id, self.session.username, reason=reason)
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

    def _copy_alert_id(self):
        """复制选中告警的编号到剪贴板"""
        a = self._selected()
        if a is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(a.alert_id)
            self.status.config(text="已复制告警编号：%s" % a.alert_id)
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def _demo(self):
        # 生产演示告警：仅所有者可用（入口只出现在权限控制台，这里再做一层拦截）
        if self.session.role != config.ROLE_OWNER:
            messagebox.showerror("权限不足", "生成演示告警仅所有者可用。")
            return
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
        # 同步复核页告警 ID 下拉框（显示所有待复核/未复核的告警）
        try:
            alerts = self.store.list_alerts(limit=500)
            ids = [a.alert_id for a in alerts if a.status != models.ST_REVIEWED]
            self.rv_alert_box.config(values=ids)
        except Exception:
            pass

        self.rv_list.delete("1.0", "end")
        for r in self.reviewer.review_rows(limit=60):
            line = "%s  %s  %s  %s  理由：%s\n" % (
                r["created_at"], r["review_id"], r["operator"],
                config.REVIEW_CONCLUSIONS.get(r["conclusion"], r["conclusion"]),
                (r["reason"] or "")[:60])
            tag = r["review_id"]
            tags = [tag]
            if r.get("closed_loop"):
                tags.append("fp")
            self.rv_list.insert("end", line, tuple(tags))
            self.rv_list.tag_config(tag, foreground="#000")

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

    def _rv_sync_alert(self):
        """手动同步复核页告警下拉框（选中告警页那行时也可用它）"""
        self.refresh_reviews()
        self.status.config(text="复核页告警列表已刷新")

    def _rv_alert_changed(self, _event=None):
        """告警 ID 下拉框变化时，自动带出考生编号"""
        aid = self.rv_alert.get().strip()
        if not aid:
            self.rv_cand.set("")
            return
        a = self.store.get_alert(aid)
        if a is None:
            self.rv_cand.set("")
            return
        self.rv_cand.set(a.candidate_id or "（未关联考生）")

    def _rv_edit_selected(self):
        """双击复核记录：修改复核结论/理由/见证人/附件，必须填写修改理由"""
        try:
            idx = self.rv_list.index("insert")
            line = self.rv_list.index("%s linestart" % idx)
            tags = self.rv_list.tag_names(line)
            review_id = None
            for t in tags:
                if t.startswith("RV"):
                    review_id = t
                    break
        except Exception:
            review_id = None
        if not review_id:
            messagebox.showinfo("提示", "请先在复核记录列表中双击一条记录")
            return
        rec = self.store.get_review(review_id)
        if rec is None:
            messagebox.showerror("错误", "复核记录不存在")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("修改复核记录")
        dlg.geometry("460x380")
        dlg.transient(self.root)
        dlg.grab_set()
        f = ttk.Frame(dlg, padding=14)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="复核结论：").grid(row=0, column=0, sticky="e", pady=4)
        conc_v = tk.StringVar(value=rec.conclusion)
        ttk.Combobox(f, textvariable=conc_v, state="readonly", width=20,
                     values=list(config.REVIEW_CONCLUSIONS.keys())).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="见证人：").grid(row=1, column=0, sticky="e", pady=4)
        wit_v = tk.StringVar(value=rec.witness or "")
        ttk.Entry(f, textvariable=wit_v, width=28).grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="附件路径（可选）：").grid(row=2, column=0, sticky="e", pady=4)
        att_v = tk.StringVar()
        ttk.Entry(f, textvariable=att_v, width=28).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="选择…",
                   command=lambda: att_v.set(filedialog.askopenfilename(title="选择复核附件") or "")).grid(row=2, column=2)
        ttk.Label(f, text="复核理由：").grid(row=3, column=0, sticky="ne", pady=4)
        reason_tx = tk.Text(f, height=4, width=36, wrap="word")
        reason_tx.grid(row=3, column=1, columnspan=2, sticky="w")
        reason_tx.insert("1.0", rec.reason or "")
        ttk.Label(f, text="修改理由（必填）：").grid(row=4, column=0, sticky="ne", pady=4)
        edit_tx = tk.Text(f, height=3, width=36, wrap="word")
        edit_tx.grid(row=4, column=1, columnspan=2, sticky="w")
        em = ttk.Label(f, text="", foreground="#c00")
        em.grid(row=5, column=0, columnspan=3)

        def save():
            att = [att_v.get().strip()] if att_v.get().strip() else []
            ok, m = self.reviewer.edit_review(
                review_id, conc_v.get(), reason_tx.get("1.0", "end").strip(),
                self.session, witness=wit_v.get().strip(), attachments=att,
                edit_reason=edit_tx.get("1.0", "end").strip())
            if not ok:
                em.config(text=m)
                return
            dlg.destroy()
            self.status.config(text="复核记录已修改：%s" % review_id)
            self.refresh_all()

        bbar = ttk.Frame(f)
        bbar.grid(row=6, column=0, columnspan=3, pady=10)
        ttk.Button(bbar, text="保存", command=save).pack(side="left", padx=6)
        ttk.Button(bbar, text="取消", command=dlg.destroy).pack(side="left", padx=6)

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


def format_console_result(r):
    """
    把命令控制台的执行结果排成纯文本。

    为什么坚持纯文本（而不是着色/富文本）：输出里可能含 ANSI 转义、控制字符、
    甚至被截断的半个字符序列，按原样 insert 到 tk.Text 里既安全又不会"看起来像 HTML"。
    """
    lines = ["层级：%s　模式：%s　退出码：%s　耗时：%sms"
             % (console.TIER_TEXT.get(r.get("tier", ""), r.get("tier") or "—"),
                r.get("mode") or "—", r.get("exit_code"), r.get("duration_ms")),
             "执行目录：%s" % (r.get("cwd") or "—")]
    if r.get("timed_out"):
        lines.append("⚠ 已超时，进程树已被强制结束")
    if r.get("truncated"):
        lines.append("⚠ 输出超过上限，已截断")
    if r.get("blocked"):
        lines.append("⚠ 本次调用被拦下，命令没有执行")
    lines.append("-" * 70)
    if r.get("stdout"):
        lines.append("[标准输出]")
        lines.append(r["stdout"].rstrip("\n"))
    if r.get("stderr"):
        lines.append("[标准错误]")
        lines.append(r["stderr"].rstrip("\n"))
    lines.append("-" * 70)
    lines.append(("结果：成功" if r.get("ok")
                  else "结果：未执行 / 失败 —— %s" % (r.get("reason") or "未知原因")))
    lines.append("（本次调用已写入本机审计链）")
    return "\n".join(lines) + "\n"


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
