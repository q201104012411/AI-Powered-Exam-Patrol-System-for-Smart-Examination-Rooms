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
import sys
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

# 独立运行：把包根加入 sys.path，才能 import 包根的 sso / ui_theme
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if os.path.isfile(os.path.join(_p, "ui_theme.py")):
        if _p not in sys.path:
            sys.path.append(_p)
        break

try:
    import sso
except Exception:                                            # noqa: BLE001
    sso = None
try:
    import ui_theme
except Exception:                                            # noqa: BLE001
    ui_theme = None

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
import vlm_detect as vlm_mod
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
    "boundary_pct": 92,          # 警戒线位置（画面高度百分比）：
                                 # 越大越靠画面底部，越不容易把中后排考生误判越界
    # ---- 多模态大模型（VLM）：看图判读，作为 YOLO11n 之上的主力检测源 ----
    # provider=off 时与旧版完全一致（纯 YOLO11n）；api/ollama 时多模态为主、
    # YOLO11n 自动降级为兜底（多模态超时/报错/结果过期时用它的框）。
    "vlm_provider": "off",       # off / api / ollama
    "vlm_model": "",             # 例：ollama→qwen2.5vl:3b ／ api→qwen-vl-max
    "vlm_base_url": "",          # 留空则按 provider 取默认（ollama=本机 11434）
    "vlm_api_key": "",           # 只有 api 后端需要
    "vlm_interval": 3.0,         # 每路每 N 秒最多一次多模态调用
    "vlm_max_dim": 896,          # 送模型的图片长边
    "vlm_timeout": 60.0,         # 单次调用超时（秒）
    # ---- 密集考场（40 人）：分块（切片）推理 ----
    # auto = 分析帧长边 ≥ 640×1.35 时自动分块（即"密集档 1280"会自动开，
    # 标准档 640 不分块——分了也没收益，只白花算力）；on/off 可强制。
    "yolo_tiling": "auto",       # auto / on / off
    "yolo_tile_size": 640,       # 分块边长（像素）：480 更准更慢，640 均衡
    "yolo_tile_max": 12,         # 单帧最多几块（限制算力上限）
}

# ---------------- 渲染性能参数（解决"画面一多就卡"） ----------------
# 渲染与 AI 分析解耦：视频按下面这些频率贴图，AI 分析按更低频率跑并复用结果。
RENDER_MS_FEW = 66          # ≤2 路：约 15fps
RENDER_MS_MID = 90          # ≤4 路：约 11fps
RENDER_MS_MANY = 130        # ≤9 路：约 8fps
RENDER_MS_HUGE = 200        # >9 路：约 5fps
AI_INTERVAL_MS = {          # 单路 AI 分析最小间隔（毫秒）
    "standard": 500,        # 标准：2 次/秒
    "dense": 900,           # 密集模式更慢，进一步降频
    "energy": 1600,         # 节能模式
}
AI_INPUT_DIM = {            # AI 分析用长边（越大越能看清后排，也越费算力）
    # YOLO11n 检测器输入固定 640×640：
    #   · 标准/节能档：整帧压到 640 送模型，够用且省算力；
    #   · **密集档（40 人考场）**：长边给到 1280，再由 person_detect 做"分块推理"
    #     （把画面切成 640 的块分别送模型）。单张推理永远把整帧压到 640，
    #     1280/1920 的分辨率会被白白丢掉 —— 实测合成 40 人考场：
    #     单张 640 → 召回 12.5%；单张 1280/1920 → 25%；**1280 + 分块 → 67.5%**，
    #     1920 + 分块 → 72.5%（代价：单路分析约 170~215 ms）。
    "standard": 640,
    "dense": 1280,
    "energy": 640,
}
INFO_BAR_MS = 500           # 顶部信息条/汇总的最小刷新间隔
MAX_PANEL_W = 480           # 单路面板最大渲染宽度（超过就缩，省 CPU）
MAX_PANEL_H = 300
PANEL_W = 384               # 单路画面初始像素宽度（Label 显示 image 时按像素解释）
PANEL_H = 216               # 单路画面初始像素高度（16:9）
CAM_COLS = 2                # 监控网格列数
TOOLTIP_THROTTLE_MS = 120   # 悬停提示移动时的最小重定位间隔

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
        if ui_theme is not None:
            ui_theme.apply(self.root)
        self.store = storage.Store()
        self.accounts = web_accounts.Accounts(self.store)
        self.user = None                     # web_accounts.Session
        self.baselines = baseline_mod.BaselineStore(self.store)
        self.orders = wo_mod.WorkOrderManager(self.store)
        self.runner = patrol_mod.PatrolRunner(self.store, baseline_store=self.baselines)
        self.reporter = report_mod.Reporter(self.store, runner=self.runner, orders=self.orders)
        # 关窗口 = 停止所有取流（否则后台读线程/网络连接会拖住进程退出）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self._try_sso():
            self._build_login()              # 直接启动时仍走原登录页

    def _try_sso(self):
        """总控台一次登录后直达 02：不再要求输入第二遍账户/口令。"""
        if sso is None:
            return False
        info = sso.verify_launch("02")
        if not info:
            return False
        role = sso.map_role(info["role"], {
            "owner": web_accounts.ROLE_OWNER,
            "admin": web_accounts.ROLE_ADMIN,
            "patrol": web_accounts.ROLE_ADMIN,
            "invigilator": web_accounts.ROLE_ADMIN,
            "checker": web_accounts.ROLE_ADMIN,
        }, web_accounts.ROLE_ADMIN)
        self.user = web_accounts.Session(
            info["username"], role, info.get("name") or info["username"])
        try:
            self.store.log("单点登录", "%s（%s，来自总控台）"
                           % (self.user.username,
                              web_accounts.role_text(self.user.role)),
                           self.user.username)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            self._build()
            if ui_theme is not None:
                ui_theme.polish_later(self.root)
            return True
        except Exception:                                    # noqa: BLE001
            self.user = None
            return False

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
                  foreground=(ui_theme.DIM if ui_theme else "#555")).pack(pady=(0, 20))

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
        if ui_theme is not None:
            ui_theme.polish(self.root)
        e_u.focus_set()

    def _do_login(self):
        s, msg = self.accounts.authenticate(self.login_user.get(), self.login_pwd.get())
        if s is None:
            self.login_msg.config(text="登录失败：%s" % msg)
            self.login_pwd.set("")
            return
        self.user = s
        self._build()                        # 原地切换成主界面
        if ui_theme is not None:
            ui_theme.polish_later(self.root)
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

    def _open_cmd_chat(self):
        """「📡 接收命令」：设备端收 03（智能终端）下发的命令，聊天式显示。

        用的是 00_集成联动\\cmd_chat.py（与 01 共用同一套轮询 / 白名单执行 / 回执），
        与 02 自己的"终端账户/审计链"无关，也不需要命令控制台总开关。
        """
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            target = ""
            cur = here
            for _ in range(3):
                cand = os.path.join(os.path.dirname(cur), "00_集成联动")
                if os.path.isdir(cand):
                    target = cand
                    break
                cur = os.path.dirname(cur)
            if not target:
                messagebox.showerror("找不到组件",
                                     "未找到 00_集成联动\\cmd_chat.py（命令聊天窗口）")
                return
            if target not in sys.path:
                sys.path.insert(0, target)
            import cmd_chat
            cmd_chat.open_chat_window(self.root, "02",
                                      url=os.environ.get("EXAM_TERMINAL_URL", ""),
                                      token=os.environ.get("EXAM_TERMINAL_TOKEN", ""))
        except Exception as e:
            messagebox.showerror("打开失败", "命令聊天窗口打开失败：%s" % e)

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
        # 设备端「📡 接收命令」：收 03 下发的命令（聊天式，白名单执行 + 自动回执）
        ttk.Button(top, text="📡 接收命令", command=self._open_cmd_chat
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
        ttk.Button(bar, text="✏ 编辑机位",
                   command=self._guard("device_add", self._edit_device)
                   ).pack(side="left", padx=PAD_BTN)
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
        self.dtree.tag_configure(
            "nobase",
            background=("#3a2f14" if ui_theme else "#fff6e0"),
            foreground=("#fbbf24" if ui_theme else "#000000"))
        self.dtree.bind("<Double-1>", lambda _e: self._edit_device())

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
        self._device_dialog(None)

    def _edit_device(self):
        d = self._selected_device()
        if d is not None:
            self._device_dialog(d)

    def _device_dialog(self, existing=None):
        """新增/编辑机位。按钮固定在底部，避免老窗口高度不够把“保存”裁掉。"""
        win = tk.Toplevel(self.root)
        win.title("编辑机位" if existing else "新增机位")
        win.geometry("600x700")
        win.minsize(540, 620)
        win.transient(self.root)
        win.grab_set()
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("600x700+%d+%d" % (max(0, (sw - 600) // 2),
                                       max(0, (sh - 700) // 2 - 30)))

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        foot = ttk.Frame(outer)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        msg = ttk.Label(foot, text="", foreground=("#f43f5e" if ui_theme else "#c00"))
        msg.pack(anchor="w")
        btns = ttk.Frame(foot)
        btns.pack(anchor="e", pady=(6, 0))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)

        fields = [("code", "设备编码*"), ("name", "设备名称"), ("room_id", "考场*"),
                  ("site", "考点"), ("position", "安装位置"), ("url", "取流地址"),
                  ("resolution", "分辨率"), ("fov_deg", "视场角(度)"),
                  ("pan_deg", "水平角(度)"), ("tilt_deg", "俯仰角(度)"),
                  ("session", "场次")]
        defaults = {"resolution": "1920x1080", "fov_deg": "90"}
        vars_ = {}
        for i, (k, label) in enumerate(fields):
            ttk.Label(body, text=label + "：").grid(row=i, column=0, sticky="e",
                                                   pady=3, padx=(0, 6))
            v = tk.StringVar(value=defaults.get(k, ""))
            if existing is not None:
                raw = getattr(existing, k, "")
                if k == "fov_deg":
                    raw = "%.1f" % float(raw or 90)
                v.set("" if raw is None else str(raw))
            vars_[k] = v
            ttk.Entry(body, textvariable=v, width=36).grid(row=i, column=1,
                                                           pady=3, sticky="ew")
        body.columnconfigure(1, weight=1)

        r = len(fields)
        audio = tk.BooleanVar(value=bool(existing.has_audio) if existing else False)
        ttk.Checkbutton(body, text="配拾音器（音频链路核查）", variable=audio
                        ).grid(row=r, column=1, sticky="w", pady=(4, 2))
        ttk.Label(body, text="取流地址可填：0/1/2（本机摄像头）、rtsp://…、"
                             "http://… 或本地视频文件路径；留空表示暂不取流。",
                  foreground=(ui_theme.DIM if ui_theme else "#666"),
                  font=FONT_SMALL, wraplength=420).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        def save():
            if not self._need("device_add"):
                return
            code = vars_["code"].get().strip()
            room = vars_["room_id"].get().strip()
            if not code:
                msg.config(text="设备编码必填")
                return
            if not room:
                msg.config(text="考场必填（例如 A-101）")
                return
            try:
                fov = float(vars_["fov_deg"].get() or 90)
                pan = float(vars_["pan_deg"].get() or 0)
                tilt = float(vars_["tilt_deg"].get() or 0)
            except ValueError:
                msg.config(text="角度必须是数字")
                return
            if fov <= 0:
                msg.config(text="视场角必须大于 0")
                return
            d = models.Device(
                code=code, name=vars_["name"].get().strip(), room_id=room,
                site=vars_["site"].get().strip(),
                position=vars_["position"].get().strip(),
                url=vars_["url"].get().strip(),
                resolution=vars_["resolution"].get().strip() or "1920x1080",
                fov_deg=fov, pan_deg=pan, tilt_deg=tilt,
                has_audio=audio.get(), session=vars_["session"].get().strip(),
                device_id=existing.device_id if existing else None)
            if existing is not None:
                # 编辑时保留基准图 / 心跳 / 启用状态，别被表单覆盖
                d.baseline_at = existing.baseline_at
                d.last_heartbeat = existing.last_heartbeat
                d.enabled = existing.enabled
                d.expected_segments = existing.expected_segments
                d.extra = dict(existing.extra or {})
            try:
                self.store.save_device(d)
            except Exception as exc:                         # noqa: BLE001
                msg.config(text="保存失败：%s" % str(exc)[:160])
                return
            self.store.log("台账修改机位" if existing else "台账登记机位",
                           d.code, self.user.username)
            win.destroy()
            self.refresh_all()

        ttk.Button(btns, text="保存", style="Accent.TButton",
                   command=save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=6)
        win.bind("<Return>", lambda _e: save())
        win.bind("<Escape>", lambda _e: win.destroy())

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
        self.ctree.tag_configure(
            "bad", background=("#3a1a1e" if ui_theme else "#ffe3e3"),
            foreground=("#f87171" if ui_theme else "#a00000"))
        self.ctree.tag_configure(
            "warn", background=("#3a2f14" if ui_theme else "#fff6e0"),
            foreground=("#fbbf24" if ui_theme else "#8a5a00"))
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

    def _ai_boundary_ratio(self):
        """当前警戒线位置（画面高度百分比 -> 小数）"""
        pct = self._ai_settings.get("boundary_pct", 92)
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = 92.0
        return min(0.99, max(0.30, pct / 100.0))

    def _ai_tracker_kwargs(self):
        """构造检测器初始化参数（多模态 / 纯 YOLO 自适应）。

        参数表按"将要使用的那个类"的可接受参数过滤：
          · 多模态开启 → VlmPersonTracker 认识 vlm_* 参数；
          · 关闭       → 纯 PersonTracker，vlm_* 会被自动丢掉（不会 TypeError）。
        """
        settings = self._ai_settings
        params = vlm_mod.accepted_params(pd_mod.PersonTracker,
                                         settings.get("vlm_provider"))
        kw = {"margin_ratio": self._ai_margin_ratio()}
        # 界面层已按 AI_INPUT_DIM 缩放后再送进来，这里把内部再缩放阈值放高，
        # 避免"缩两次"浪费 CPU（真实长边已经 <= 576）。
        for key, val in (("dense_mode", settings.get("mode") == "dense"),
                         ("max_input_dim", 1280),
                         ("detect_stride", 2 if settings.get("mode") == "energy" else 1),
                         ("enable_face_dense", settings.get("haar_face", True)),
                         ("boundary_ratio", self._ai_boundary_ratio()),
                         ("confirm_frames", 4 if settings.get("mode") != "energy" else 3),
                         # 视频流要求连续两帧命中才算"确认的人"，滤掉单帧噪声
                         ("min_hits", 2),
                         ("match_dist_ratio", 0.12)):
            if key in params:
                kw[key] = val
        # 多模态参数（provider=off 时不在 params 里，自然不会传下去）
        for key in vlm_mod.SETTING_KEYS:
            val = settings.get(key)
            if key in params and val not in (None, ""):
                kw[key] = val
        # 密集考场：分块推理参数（老版本 person_detect 没有这些键，自动跳过）
        for key in ("yolo_tiling", "yolo_tile_size", "yolo_tile_max"):
            val = settings.get(key)
            if key in params and val not in (None, ""):
                kw[key] = val
        return kw

    def _reset_ai_trackers(self):
        """清空每路检测器（改设置/重播时调用，轨迹按新参数重建）"""
        self._person_detector = None
        for watch in list(getattr(self, "_cheat_watches", {}).values()):
            try:
                watch.reset()
            except Exception:
                pass
        getattr(self, "_cheat_watches", {}).clear()
        for tracker in list(getattr(self, "_cam_trackers", {}).values()):
            try:
                tracker.stop()
            except Exception:
                pass
        getattr(self, "_cam_trackers", {}).clear()
        # 如实记一笔当前生效的密集场景配置（排查"后排看不见/人数不对"时先看它）
        try:
            mode = (self._ai_settings or {}).get("mode", "standard")
            dim = AI_INPUT_DIM.get(mode, AI_INPUT_DIM["standard"])
            tiling = (self._ai_settings or {}).get("yolo_tiling", "auto")
            dense_note = ("密集场景：分析长边 %d，分块推理 %s" % (dim, tiling)
                          if mode == "dense" or tiling == "on" else
                          "分析长边 %d，分块推理 %s" % (dim, tiling))
            self._mon_log_line("AI 检测器重建：模式=%s ｜ %s" % (mode, dense_note))
        except Exception:
            pass

    def _open_ai_settings(self):
        """全局 AI 设置弹窗"""
        win = tk.Toplevel(self.root)
        win.title("AI 设置")
        win.geometry("700x740")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)

        settings = self._load_ai_settings()

        ttk.Label(f, text="检测模式：").grid(row=0, column=0, sticky="e", pady=6)
        mode_var = tk.StringVar(value=settings.get("mode", "standard"))
        ttk.Combobox(f, textvariable=mode_var, state="readonly", width=18,
                     values=["standard", "dense", "energy"]).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="标准：常规考场（640）；密集：40 人考场（1280 + 分块推理）；节能：隔帧 640",
                  foreground="#888",
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

        ttk.Label(f, text="警戒线位置：").grid(row=4, column=0, sticky="e", pady=6)
        boundary_var = tk.IntVar(value=int(settings.get("boundary_pct", 92)))
        boundary_bar = ttk.Frame(f)
        boundary_bar.grid(row=4, column=1, sticky="w")
        for pct in (50, 75, 92, 98):
            ttk.Radiobutton(boundary_bar, text="%d%%" % pct, variable=boundary_var,
                            value=pct).pack(side="left", padx=(0, 8))
        ttk.Label(f, text="越大越靠画面底部；92% 表示只有走到镜头前才算越界",
                  foreground="#888", font=FONT_SMALL).grid(row=4, column=2, sticky="w", padx=(8, 0))

        # ---------------- 多模态大模型（VLM）----------------
        # 只做"看图判读"这一件事：YOLO11n 仍是兜底，多模态不可用时自动回落，
        # 所以这里就算填错也不会把识别整条链路搞挂。
        ttk.Separator(f, orient="horizontal").grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(14, 6))

        label_to_provider = dict((vlm_mod.PROVIDER_LABELS[p], p)
                                 for p in vlm_mod.PROVIDERS)

        ttk.Label(f, text="多模态大模型：").grid(row=6, column=0, sticky="e", pady=4)
        cur_provider = vlm_mod.VlmConfig.from_settings(settings).provider
        prov_var = tk.StringVar(
            value=vlm_mod.PROVIDER_LABELS.get(cur_provider,
                                              vlm_mod.PROVIDER_LABELS["off"]))
        ttk.Combobox(f, textvariable=prov_var, state="readonly", width=18,
                     values=[vlm_mod.PROVIDER_LABELS[p]
                             for p in vlm_mod.PROVIDERS]).grid(row=6, column=1, sticky="w")
        ttk.Label(f, text="本机 Ollama 数据不出本机；API 需填 Key",
                  foreground="#888", font=FONT_SMALL).grid(row=6, column=2,
                                                           sticky="w", padx=(8, 0))

        ttk.Label(f, text="模型名：").grid(row=7, column=0, sticky="e", pady=4)
        vlm_model_var = tk.StringVar(value=settings.get("vlm_model", ""))
        ttk.Entry(f, textvariable=vlm_model_var, width=46).grid(
            row=7, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="接口地址：").grid(row=8, column=0, sticky="e", pady=4)
        vlm_url_var = tk.StringVar(value=settings.get("vlm_base_url", ""))
        ttk.Entry(f, textvariable=vlm_url_var, width=46).grid(
            row=8, column=1, columnspan=2, sticky="w")
        ttk.Label(f, text="留空=默认（ollama：127.0.0.1:11434）",
                  foreground="#888", font=FONT_SMALL).grid(row=8, column=2,
                                                           sticky="w", padx=(8, 0))

        ttk.Label(f, text="API Key：").grid(row=9, column=0, sticky="e", pady=4)
        vlm_key_var = tk.StringVar(value=settings.get("vlm_api_key", ""))
        ttk.Entry(f, textvariable=vlm_key_var, width=46, show="*").grid(
            row=9, column=1, columnspan=2, sticky="w")

        ttk.Label(f, text="调用间隔(秒)：").grid(row=10, column=0, sticky="e", pady=4)
        vlm_iv_var = tk.StringVar(value=str(settings.get("vlm_interval", 3.0)))
        ttk.Entry(f, textvariable=vlm_iv_var, width=8).grid(row=10, column=1, sticky="w")
        ttk.Label(f, text="每路每 N 秒最多一次；多模态较慢，不建议小于 2 秒",
                  foreground="#888", font=FONT_SMALL).grid(row=10, column=2,
                                                           sticky="w", padx=(8, 0))

        vlm_msg = ttk.Label(f, text="", foreground="#1f56b8", font=FONT_SMALL)
        vlm_msg.grid(row=11, column=0, columnspan=2, sticky="w", pady=(2, 0))

        def _vlm_cfg_from_form():
            return vlm_mod.VlmConfig(
                provider=label_to_provider.get(prov_var.get(), "off"),
                model=vlm_model_var.get().strip(),
                base_url=vlm_url_var.get().strip(),
                api_key=vlm_key_var.get().strip(),
                interval=vlm_iv_var.get().strip() or 3.0)

        def test_vlm():
            """探测后端可用性（在后台线程里跑，别卡住设置窗口）"""
            cfg = _vlm_cfg_from_form()
            if not cfg.enabled:
                vlm_msg.config(text="配置不完整：%s" % cfg.missing_reason(),
                               foreground="#c00")
                return
            vlm_msg.config(text="正在探测 %s …" % cfg.model, foreground="#1f56b8")

            def work():
                try:
                    ok, text = vlm_mod.probe_text(cfg)
                except Exception as e:                  # 探测本身出错也要如实说
                    ok, text = False, "探测异常：%s" % e

                def show():
                    try:
                        vlm_msg.config(text=text,
                                       foreground=("#0a7d28" if ok else "#c00"))
                    except tk.TclError:
                        pass
                try:
                    self.root.after(0, show)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(f, text="测试连接", command=test_vlm).grid(row=11, column=2,
                                                              sticky="e")

        # ---------------- 密集考场（40 人）：分块推理 ----------------
        ttk.Separator(f, orient="horizontal").grid(
            row=12, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        ttk.Label(f, text="密集考场分块推理：").grid(row=13, column=0, sticky="e", pady=4)
        tiling_var = tk.StringVar(value=str(settings.get("yolo_tiling", "auto")))
        ttk.Combobox(f, textvariable=tiling_var, state="readonly", width=8,
                     values=["auto", "on", "off"]).grid(row=13, column=1, sticky="w")
        ttk.Label(f, text="单张推理会把整帧压到 640，40 人考场后排只有十几像素高必然漏检；"
                          "分块后每块按原生像素送模型（实测召回 25%→68%）",
                  foreground="#888", font=FONT_SMALL, wraplength=360,
                  justify="left").grid(row=14, column=0, columnspan=3, sticky="w")

        msg = ttk.Label(f, text="", foreground="#c00")
        msg.grid(row=15, column=0, columnspan=3, pady=(10, 0))

        def apply():
            new_settings = dict(settings)
            new_settings.update({
                "mode": mode_var.get(),
                "box_margin_pct": margin_var.get(),
                "haar_face": haar_var.get(),
                "sound_beep": sound_var.get(),
                "boundary_pct": boundary_var.get(),
                "vlm_provider": label_to_provider.get(prov_var.get(), "off"),
                "vlm_model": vlm_model_var.get().strip(),
                "vlm_base_url": vlm_url_var.get().strip(),
                "vlm_api_key": vlm_key_var.get().strip(),
                "vlm_interval": vlm_iv_var.get().strip() or 3.0,
                "yolo_tiling": tiling_var.get(),
            })
            if self._save_ai_settings(new_settings):
                self._ai_settings = new_settings
                # 边距/模式/多模态实时生效：清掉每路的检测器，下次分析按新设置重建
                self._reset_ai_trackers()
                vlm_desc = vlm_mod.VlmConfig.from_settings(new_settings).describe()
                msg.config(text="已保存并生效", foreground="#0a7d28")
                self.status.config(text="AI 设置已更新：模式=%s 边距=%d%% 警戒线=%d%% ｜ %s"
                                        % (new_settings["mode"],
                                           new_settings["box_margin_pct"],
                                           new_settings["boundary_pct"], vlm_desc))
                self._mon_log_line("AI 设置更新：%s" % new_settings)

        bbar = ttk.Frame(f)
        bbar.grid(row=16, column=0, columnspan=3, pady=(16, 0))
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
    def _density_level(persons, frame_or_shape):
        """按每百万像素人数返回密度等级 low/medium/high。

        frame_or_shape 可以是帧（用 .shape），也可以是 (h, w) 元组。
        """
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

    def _cam_bind_tooltip(self, widget, code):
        """给画面控件绑定悬停提示（移动时限频重定位，避免鼠标一动就重建窗口）"""
        tip = None
        state = {"ts": 0.0}

        def enter(event):
            nonlocal tip
            self._cam_hide_tooltip(tip)
            tip = self._cam_show_tooltip(code, event.x_root, event.y_root)
            state["ts"] = time.time() * 1000.0

        def leave(event):
            nonlocal tip
            self._cam_hide_tooltip(tip)
            tip = None

        def motion(event):
            if tip is None:
                return
            now = time.time() * 1000.0
            if now - state["ts"] < TOOLTIP_THROTTLE_MS:
                return
            state["ts"] = now
            try:
                tip.geometry("+%d+%d" % (event.x_root + 16, event.y_root + 16))
            except tk.TclError:
                pass

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
        widget.bind("<Motion>", motion)

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
        if panel.get("border_state") == state:
            return
        panel["border_state"] = state
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
        self._cam_latest = {}       # code -> 最新帧（用于放大窗口，仅放大时才存）
        self._cam_enlarge_active = {}  # code -> bool（放大窗口是否打开）
        self._cam_seen = {}         # code -> (generation, seq) 已渲染过的帧序号
        self._cam_render_after = {}  # code -> after id（渲染循环，避免重复排程）
        self._cam_trackers = {}     # code -> PersonTracker（每路独立，避免轨迹串号）
        self._info_ts = 0.0         # 顶部信息条上次刷新时间（限频用）
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
        # 作弊识别（cheatdetect 时序引擎）：与逐帧标签叠加，看"动作序列像不像作弊"，
        # 单帧瞬时动作会被时间维度抑制（正是"坐着没动却报偏转"那类误报的解药）
        self.mon_cheat = tk.BooleanVar(value=True)
        self._cheat_watches = {}
        self.mon_ai_chk = ttk.Checkbutton(
            mid, text="AI 辅助识别（逐人：绿=正常 橙=异常 红=越界，考场监控默认开启）",
            variable=self.mon_ai)
        self.mon_ai_chk.pack(side="left")
        self.mon_cheat_chk = ttk.Checkbutton(
            mid, text="作弊识别（时序：动作持续才告警，单帧动作自动抑制）",
            variable=self.mon_cheat)
        self.mon_cheat_chk.pack(side="left", padx=(12, 0))

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
        canvas = tk.Canvas(parent, highlightthickness=0,
                           bg=(ui_theme.BG if ui_theme else "#f4f4f4"),
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
        self._cam_enlarge_active.clear()
        self._cam_trackers.clear()
        self._dev_cache = None       # 台账已重建，设备缓存作废
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
        cols = CAM_COLS
        for i, dev in enumerate(devs):
            room = dev.room_id or "未分配考场"
            name = dev.name or dev.code or "未命名"
            code = dev.code
            # tk.LabelFrame 支持 highlightbackground，可做状态色边框
            frame = tk.LabelFrame(self.mon_grid, text=" %s · %s " % (room, name),
                                  bg=(ui_theme.PANEL if ui_theme else "#f9f9f9"),
                                  fg=(ui_theme.FG if ui_theme else "#333"),
                                  font=FONT_SMALL,
                                  highlightthickness=2, highlightbackground=CAM_COLORS["idle"])
            frame.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            self.mon_grid.rowconfigure(i // cols, weight=1)
            # 两列都给权重：单路时才不会一个面板横跨整宽、比例失衡
            for c in range(cols):
                self.mon_grid.columnconfigure(c, weight=1)
            # 黑底画面区。
            # 注意：tk.Label 一旦设置了 image，width/height 会被按**像素**解释，
            # 所以这里必须先给一张 1x1 黑图再设置像素尺寸——否则 height=9 会
            # 变成"9 像素高"，画面被压成一条细缝（旧版的实际表现）。
            blank = tk.PhotoImage(width=1, height=1)
            blank.put("#000000", to=(0, 0))
            lbl = tk.Label(frame, bg="#000", relief="sunken", cursor="hand2",
                           image=blank, width=PANEL_W, height=PANEL_H)
            lbl._blank_img = blank          # 保住引用，避免被 GC
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
                                      "density_text": None, "border_state": None,
                                      "ai_text": None, "ai_n": 0, "ai_cross": 0,
                                      "ai_persons": [], "ai_viol": 0, "ai_ts": 0.0,
                                      "ai_scale": None, "ai_line_y": None,
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
        # 重播清掉上一轮的检测器与 AI 缓存，避免轨迹/框残留
        old = self._cam_trackers.pop(code, None)
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        panel = self._mon_panels.get(code)
        if panel is not None:
            panel["ai_persons"] = []
            panel["ai_viol"] = 0
            panel["ai_ts"] = 0.0
            panel["ai_text"] = None
            panel["ai_scale"] = None
            panel["ai_line_y"] = None
            panel["persons"] = []
            panel["status_text"] = None
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

    def _render_interval(self):
        """按当前播放路数自适应渲染间隔：路数越多，单路帧率越低（总 CPU 才压得住）"""
        n = sum(1 for v in getattr(self, "_cam_running", {}).values() if v)
        if n <= 2:
            return RENDER_MS_FEW
        if n <= 4:
            return RENDER_MS_MID
        if n <= 9:
            return RENDER_MS_MANY
        return RENDER_MS_HUGE

    def _cam_render(self, code):
        """渲染循环：从 FrameReader 取最新帧贴图（非阻塞）。

        性能要点（原实现"路数一多就卡"的原因）：
          1. 帧序号 (generation, seq) 没变就**不重复渲染**；
          2. 渲染频率按播放路数自适应下调（_render_interval）；
          3. 缩图用 cv2.resize（INTER_AREA），不再用 PIL 的 LANCZOS；
          4. AI 分析按更低的固定间隔跑，中间帧复用上一次的框，不做重复检测；
          5. 只有一个 after 循环，不会叠加。
        """
        try:
            reader = self._cam_readers.get(code)
            if reader is None or not self._cam_running.get(code):
                self._cam_render_after.pop(code, None)
                return
            snap = reader.snapshot()
            key = (snap["generation"], snap["seq"])
            if snap["frame"] is not None and snap["seq"] and self._cam_seen.get(code) != key:
                self._cam_seen[code] = key
                self._cam_update(code, snap)
        except Exception as e:                       # noqa: BLE001
            self._cam_set_status(code, "渲染失败：%s" % str(e)[:60], CAM_COLORS["error"])
        if not self._cam_running.get(code):
            self._cam_render_after.pop(code, None)
            return
        try:
            self._cam_render_after[code] = self.root.after(
                self._render_interval(), lambda c=code: self._cam_render(c))
        except (tk.TclError, RuntimeError):
            self._cam_render_after.pop(code, None)

    def _mon_log_line(self, text):
        """监控操作日志（取流问题事后可查；写失败不影响运行）"""
        try:
            with open(str(self._mon_log), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    @staticmethod
    def _fit_frame(frame, w, h):
        """把帧等比缩放到不超过 (w,h) 的尺寸（cv2.resize，下采样用 INTER_AREA 更快更好）"""
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

    def _cam_ai_interval_ms(self):
        mode = (self._ai_settings or {}).get("mode", "standard")
        return AI_INTERVAL_MS.get(mode, AI_INTERVAL_MS["standard"])

    def _cam_ai_input_dim(self):
        mode = (self._ai_settings or {}).get("mode", "standard")
        return AI_INPUT_DIM.get(mode, AI_INPUT_DIM["standard"])

    def _cam_tracker(self, code):
        """每路一个独立的检测器（共用一个会让不同考场的轨迹互相串号）。

        多模态开启时用 VlmPersonTracker（多模态为主 + YOLO11n 兜底）；
        构造失败或没开多模态时，退回原来的 PersonTracker。
        """
        tracker = self._cam_trackers.get(code)
        if tracker is not None:
            return tracker
        kwargs = self._ai_tracker_kwargs()
        tracker = None
        cls = vlm_mod.tracker_class_for(kwargs.get("vlm_provider"))
        if cls is not None:
            try:
                tracker = cls(**kwargs)
            except Exception as e:
                self._mon_log_line("多模态检测器创建失败，回落 YOLO11n：%s" % e)
                tracker = None
        if tracker is None:
            base_params = set(inspect.signature(pd_mod.PersonTracker.__init__).parameters)
            base_kwargs = dict((k, v) for k, v in kwargs.items() if k in base_params)
            try:
                tracker = pd_mod.PersonTracker(**base_kwargs)
            except TypeError:
                tracker = pd_mod.PersonDetector(margin_ratio=self._ai_margin_ratio())
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
                return None
        self._cam_trackers[code] = tracker
        return tracker

    def _cam_update(self, code, snap):
        """把一帧渲染到单路面板上。

        AI 分析（低频、按 AI_INPUT_DIM 缩放）+ 视频贴图（高频、按面板尺寸缩放）分离：
        分析结果里保存的是**分析分辨率**下的框，贴图时按比例映射到显示分辨率。
        """
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        lbl = panel.get("label")
        if lbl is None or not lbl.winfo_exists():
            return
        raw = snap.get("frame")
        if raw is None or getattr(raw, "size", 0) == 0:
            return
        try:
            # ---- 1) 计算显示尺寸并缩放（不修改原始帧，避免污染取流内核的缓存） ----
            h = lbl.winfo_height()
            w = lbl.winfo_width()
            if h < 50:
                h = 240
            if w < 50:
                w = 320
            disp = self._fit_frame(raw, w, h)
            # 放大窗口用原帧（引用即可，本方法不再改动它）
            if self._cam_enlarge_active.get(code):
                self._cam_latest[code] = raw

            now_ms = time.time() * 1000.0
            ai_on = bool(getattr(self, "mon_ai", None) and self.mon_ai.get())
            viol = panel.get("ai_viol", 0)
            persons = panel.get("ai_persons", []) or []

            if ai_on:
                due = (now_ms - panel.get("ai_ts", 0.0)) >= self._cam_ai_interval_ms()
                if due:
                    # ---- 2) AI 分析：在按 AI_INPUT_DIM 缩放的副本上跑 ----
                    dim = self._cam_ai_input_dim()
                    rh, rw = raw.shape[:2]
                    if max(rh, rw) > dim:
                        s = dim / float(max(rh, rw))
                        ai_frame = cv2.resize(raw, (max(2, int(rw * s)),
                                                    max(2, int(rh * s))),
                                              interpolation=cv2.INTER_AREA)
                    else:
                        ai_frame = raw
                    tracker = self._cam_tracker(code)
                    if tracker is not None:
                        persons, viol, line_y = tracker.analyze_only(
                            ai_frame, boundary_ratio=self._ai_boundary_ratio())
                        panel["ai_ts"] = now_ms
                        panel["ai_persons"] = persons
                        panel["ai_viol"] = viol
                        panel["ai_line_y"] = line_y
                        panel["ai_shape"] = ai_frame.shape
                        # 平滑人数（中位数），界面读数不再跳变
                        panel["ai_count"] = tracker.stable_count() \
                            if hasattr(tracker, "stable_count") else len(persons)
                        # 分析分辨率 -> 显示分辨率 的比例
                        panel["ai_scale"] = disp.shape[1] / float(ai_frame.shape[1])
                        # 人脸框带出来给 draw_boxes 画
                        for p in persons:
                            tr = getattr(tracker, "_tracks", {}).get(p["pid"])
                            fa = getattr(tr, "face_cache", None) if tr is not None else None
                            p["face"] = (fa or {}).get("face") if fa else None
                        self._cam_ai_side_effects(code, panel, persons, viol,
                                                  ai_frame.shape, ai_frame)
                # ---- 3) 贴图：用缓存的框画在显示帧上（cv2 + 位图缓存，几乎零成本） ----
                scale = panel.get("ai_scale")
                if scale is None:
                    scale = disp.shape[1] / float(raw.shape[1])
                    panel["ai_scale"] = scale
                tracker = self._cam_trackers.get(code)
                if tracker is not None and persons:
                    tracker.draw_boxes(disp, persons,
                                       line_y=panel.get("ai_line_y"),
                                       scale=scale, font_size=13)
            else:
                # AI 未开启时恢复绿色边框
                self._cam_set_border(code, "playing")

            # ---- 4) 转 RGB 贴到 Label ----
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
            self._cam_photos[code] = photo
            lbl.config(image=photo)
        except Exception as e:
            self._cam_set_status(code, "渲染失败：%s" % e, CAM_COLORS["error"])

    def _cam_ai_side_effects(self, code, panel, persons, viol, ai_shape=None, frame=None):
        """AI 分析后的界面更新与异常上报（只在真正跑过分析的帧上执行）"""
        level = self._density_level(persons, ai_shape or panel.get("ai_shape"))
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
        # 显示平滑后的人数（避免读数在 15/16/17 之间乱跳）
        # —— 作弊识别要在这一行之前喂帧，否则面板上的作弊风险会慢一帧
        self._cam_cheat_update(code, panel, persons, ai_shape)
        self._cam_set_ai(code, panel.get("ai_count", len(persons)), viol)
        # 顶部信息条与汇总限频刷新，避免每路每次分析都去改 Tk 控件
        now_ms = time.time() * 1000.0
        if (now_ms - getattr(self, "_info_ts", 0.0)) >= INFO_BAR_MS:
            self._info_ts = now_ms
            self._mon_ai_summary()
            self._cam_update_info_bar()
        # 逐人多次异常行为上报 03 智能终端 + 历史摘要
        try:
            alerts = self._behavior_tracker.update(code, persons)
        except Exception:
            alerts = []
        if not alerts:
            return
        self._beep()
        dev = self._dev_by_code(code)
        for a in alerts:
            text = "#%d %s" % (a["pid"], a["behavior"])
            if a["crosses"] >= 2:
                text += "（触线%d次）" % a["crosses"]
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._behavior_history.append({"ts": ts, "code": code, "text": text})
            self._behavior_history = self._behavior_history[-50:]
            panel["history"] = (panel.get("history", []) + [
                {"ts": ts[-8:], "text": text}])[-10:]
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
        self._cam_update_info_bar()

    def _cam_set_density(self, code, level):
        """更新单路密度显示"""
        panel = self._mon_panels.get(code)
        if panel is None:
            return
        density = panel.get("density")
        if density is None or not density.winfo_exists():
            return
        if panel.get("density_text") == level:
            return
        panel["density_text"] = level
        try:
            density.config(text="密度：%s" % DENSITY_TEXT.get(level, level),
                           foreground=DENSITY_COLOR.get(level, GRAY_MUTED))
        except tk.TclError:
            pass

    # ---------------- 作弊识别（cheatdetect 时序引擎） ----------------
    def _cheat_feed_hz(self):
        """真实送帧频率 = AI 分析频率（窗口 2s / 最短持续 1s 都按帧数换算，必须如实填）"""
        return max(0.2, 1000.0 / float(max(1, self._cam_ai_interval_ms())))

    def _cheat_watch_for(self, code):
        watch = getattr(self, "_cheat_watches", {}).get(code)
        if watch is None:
            try:
                import cheat_bridge
            except Exception as e:
                self._mon_log_line("作弊识别不可用：%s" % e)
                return None
            watch = cheat_bridge.CheatWatch(feed_hz=self._cheat_feed_hz())
            self._cheat_watches[code] = watch
            self._mon_log_line(watch.describe())
            notes = watch.signal_notes()
            if notes:
                self._mon_log_line("作弊识别信号可得性（引擎自述）：%s" % notes)
        return watch

    @staticmethod
    def _cheat_bar_text(panel):
        """面板 AI 行里追加的作弊风险文字（正常时不占地方）"""
        r = (panel or {}).get("cheat")
        if not r:
            return ""
        if not r.get("ok"):
            return "　｜　作弊识别：%s" % (r.get("reason") or "不可用")
        if r.get("triggered"):
            return "　｜　作弊风险：%s %.0f【%s】" % (
                r.get("level", ""), r.get("risk", 0),
                r.get("pattern_desc") or "告警中")
        if float(r.get("risk", 0)) >= 42.0:        # 阈值 60 的 70%
            return "　｜　作弊风险：%s %.0f" % (r.get("level", ""), r.get("risk", 0))
        return ""

    def _cam_cheat_update(self, code, panel, persons, ai_shape):
        """把本帧的逐人结果喂给时序引擎；只有"持续/组合"成模式时才告警"""
        if not (getattr(self, "mon_cheat", None) and self.mon_cheat.get()):
            panel["cheat"] = None
            return
        watch = self._cheat_watch_for(code)
        if watch is None:
            return
        if not watch.ok:
            panel["cheat"] = {"ok": False, "reason": watch.import_error}
            return
        r = watch.push_persons(persons,
                               frame_shape=(ai_shape or panel.get("ai_shape")))
        panel["cheat"] = r
        if not r.get("ok") or not r.get("just_triggered"):
            return
        dev = self._dev_by_code(code)
        text = "作弊预警 %s #%s：%s（风险 %.0f）" % (
            r.get("level", ""), r.get("pid"), r.get("pattern_desc") or "",
            float(r.get("risk", 0)))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._behavior_history.append({"ts": ts, "code": code, "text": text})
        self._behavior_history = self._behavior_history[-50:]
        panel["history"] = (panel.get("history", []) + [
            {"ts": ts[-8:], "text": text}])[-10:]
        self._mon_log_line("【%s】%s 风险 %.0f：%s"
                           % (code, r.get("pattern_desc") or "作弊行为模式",
                              float(r.get("risk", 0)), r.get("reason_text") or ""))
        self._beep()
        self._cam_set_border(code, "error")
        try:
            import linkage_bridge
            linkage_bridge.report_behavior(
                device_code=code,
                behavior="作弊行为模式：%s" % (r.get("pattern_desc")
                                        or r.get("pattern") or ""),
                pid=int(r.get("pid") or 0), crosses=0,
                room_id=dev.room_id if dev else "", seat_no="",
                operator=self.user.username if self.user else "02_ai")
        except Exception:
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
        text += self._cheat_bar_text(panel)
        # 内容没变就不碰控件（Tk 的 config 会触发重绘，路数一多非常吃 CPU）
        if panel.get("ai_text") == text:
            return
        panel["ai_text"] = text
        panel["ai_n"] = n_person
        panel["ai_cross"] = n_cross
        try:
            ai.config(text=text, foreground=color)
        except tk.TclError:
            pass

    def _vlm_bar_text(self):
        """状态栏里的多模态一句话（没配多模态就返回空串，界面保持原样）。

        如实反映"现在到底是谁在出框"：有路拿到模型结果算"新鲜"，
        否则算"回落 YOLO11n" —— 不美化、不假装。
        """
        try:
            cfg = vlm_mod.VlmConfig.from_settings(self._ai_settings or {})
        except Exception:
            return ""
        if not cfg.enabled:
            return ""
        trackers = [t for t in (getattr(self, "_cam_trackers", {}) or {}).values()
                    if hasattr(t, "vlm_status_dict")]
        if not trackers:
            return "　｜　多模态：%s（待启动）" % cfg.model
        fresh = 0
        for t in trackers:
            try:
                if t.vlm_status_dict().get("fresh"):
                    fresh += 1
            except Exception:
                pass
        if fresh == len(trackers):
            state = "结果新鲜"
        elif fresh:
            state = "%d/%d 路新鲜，其余回落 YOLO11n" % (fresh, len(trackers))
        else:
            state = "回落 YOLO11n"
        return "　｜　多模态 %s：%s" % (cfg.model, state)

    def _mon_ai_summary(self):
        """全局状态标签：汇总已开启 AI 的机位人数、越界数、异常行为数、高密度机位数"""
        total_p, total_c, n = 0, 0, 0
        abnormal = 0
        high = 0
        cheat_alarm = 0
        for panel in self._mon_panels.values():
            if panel.get("ai_text") is None:
                continue
            n += 1
            total_p += panel.get("ai_n", 0)
            total_c += panel.get("ai_cross", 0)
            for p in panel.get("persons", []) or []:
                abnormal += len(p.get("behaviors", []))
            if panel.get("density_level") == DENSITY_HIGH:
                high += 1
            cr = panel.get("cheat") or {}
            if cr.get("triggered"):
                cheat_alarm += 1
        if n:
            cheat_txt = ("　作弊预警 %d 路" % cheat_alarm) if cheat_alarm else ""
            self.mon_status.config(text="AI：%d 路　%d 人　越界 %d　异常 %d　高密度 %d%s%s"
                                        % (n, total_p, total_c, abnormal, high,
                                           cheat_txt, self._vlm_bar_text()),
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
        self._cam_latest.pop(code, None)
        self._cam_enlarge_active.pop(code, None)
        tracker = self._cam_trackers.pop(code, None)
        if tracker is not None:
            try:
                tracker.stop()
            except Exception:
                pass
        if not quiet:
            self._cam_stopped_ui(code)

    def _cam_stopped_ui(self, code):
        panel = self._mon_panels.get(code)
        try:
            if panel is not None and panel.get("label") and panel["label"].winfo_exists():
                panel["label"].config(image="")
            self._cam_btn_text(code, "▶ 播放")
            self._cam_set_status(code, "未播放（已停止）", CAM_COLORS["idle"])
            ai = panel["ai"] if panel else None
            if ai is not None and ai.winfo_exists():
                ai.config(text="", foreground=GRAY_MUTED)
            if panel is not None:
                panel["ai_text"] = None
                panel["ai_n"] = 0
                panel["ai_cross"] = 0
                panel["ai_persons"] = []
                panel["ai_viol"] = 0
                panel["ai_ts"] = 0.0
                panel["ai_scale"] = None
                panel["ai_line_y"] = None
                panel["density_level"] = None
                panel["persons"] = []
            self._cam_set_density(code, DENSITY_LOW)
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
        # 只有放大窗口打开时才需要保留原帧，关闭后立刻释放，省内存也省拷贝
        self._cam_enlarge_active[code] = True

        def _on_close():
            self._cam_enlarge_active.pop(code, None)
            self._cam_latest.pop(code, None)
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)

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
            self._reset_ai_trackers()
            status.config(text="AI 设置已保存并生效", foreground=CAM_COLORS["playing"])

        ttk.Button(set_box, text="保存到全局", command=apply_local).grid(
            row=2, column=0, columnspan=3, pady=(8, 0), sticky="w")

        ttk.Button(right, text="关闭", command=win.destroy).pack(anchor="e", pady=(4, 0))
        self._cam_enlarge_tick(code, lbl, status, win, person_txt, hist_txt)

    def _cam_enlarge_tick(self, code, lbl, status, win, person_txt, hist_txt):
        if not win.winfo_exists():
            self._cam_enlarge_active.pop(code, None)
            self._cam_latest.pop(code, None)
            return
        frame = self._cam_latest.get(code)
        if frame is not None:
            try:
                h = max(lbl.winfo_height(), 480)
                w = max(lbl.winfo_width(), 640)
                disp = self._fit_frame(frame, w, h)
                rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
                lbl.photo_ref = photo
                lbl.config(image=photo)
                status.config(text="播放中（%dx%d）" % (disp.shape[1], disp.shape[0]),
                              foreground=CAM_COLORS["playing"])
            except Exception as e:
                status.config(text="渲染失败：%s" % e, foreground=CAM_COLORS["error"])
        else:
            status.config(text="等待视频帧…", foreground=GRAY_MUTED)
        # 更新侧边栏（只在内容变化时写 Text，避免每 100ms 全量重写）
        panel = self._mon_panels.get(code)
        if panel is not None:
            persons = panel.get("persons", []) or []
            p_lines = []
            for p in persons:
                acts = ", ".join(p.get("behaviors", [])) or p.get("action", "正常")
                p_lines.append("#%d：%s" % (p.get("pid", 0), acts))
            p_text = "\n".join(p_lines) or "（暂无人员）"
            if panel.get("_p_text") != p_text:
                panel["_p_text"] = p_text
                self._text_set(person_txt, p_text)
            history = panel.get("history", []) or []
            h_lines = ["%s　%s" % (x["ts"], x["text"]) for x in history]
            h_text = "\n".join(h_lines) or "（最近 10 条异常时间线）"
            if panel.get("_h_text") != h_text:
                panel["_h_text"] = h_text
                self._text_set(hist_txt, h_text)
        self.root.after(150, lambda: self._cam_enlarge_tick(code, lbl, status, win,
                                                            person_txt, hist_txt))

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
        """按设备编码取设备（带缓存）。

        这个方法在异常上报路径上会被频繁调用，而 store.list_devices() 每次都要
        查 SQLite 并逐条解密设备名/位置——高频调用时是明显的额外开销。
        """
        cache = getattr(self, "_dev_cache", None)
        if cache is None:
            cache = {}
            try:
                for d in self.store.list_devices(enabled_only=False):
                    if d.code:
                        cache[d.code] = d
            except Exception:
                cache = {}
            self._dev_cache = cache
        dev = cache.get(code)
        if dev is not None:
            return dev
        # 缓存里没有（台账刚变过）就查一次并补进缓存
        for d in self.store.list_devices(enabled_only=False):
            if d.code == code:
                cache[code] = d
                return d
        return None

    def _cam_set_status(self, code, text, color):
        panel = self._mon_panels.get(code)
        if panel is None or panel.get("status") is None:
            return
        if not panel["status"].winfo_exists():
            return
        if panel.get("status_text") == text:
            return
        panel["status_text"] = text
        try:
            panel["status"].config(text=text, foreground=color)
        except tk.TclError:
            pass

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
