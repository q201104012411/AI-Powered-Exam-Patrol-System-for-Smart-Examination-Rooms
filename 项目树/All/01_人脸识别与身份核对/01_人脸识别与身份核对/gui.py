# -*- coding: utf-8 -*-
"""
图形界面（tkinter）：现场抓拍 + 身份要素 + 人脸核验 + 审计留痕。

相比旧版的关键变化：
1. 现场图像默认来自**摄像头实时抓拍**（带现场令牌）；"选择图片"属非现场来源，
   严格模式下会被明确拒绝（点按钮即给出提示），不再出现"拿参考照也能通过"的情况；
2. 身份要素校验的语义由所有者开关决定（原本注入的"永远放行"假校验器已移除）：
     · 大考模式开启 -> 四要素 = 身份证号 + 准考证号 + 座位号 + 姓名，身份证必填走哈希比对；
     · 大考模式关闭 -> 三要素 = 准考证号 + 座位号 + 姓名，身份证不参与校验，
       未登记身份证的考生也能核验；准考证号必填，不一致会被拦截。
3. 严格模式由所有者开关控制（默认开启）：关闭后允许用本地图片核验，
   但核验记录会写明 strict_mode_off / non_live_source，并在原因里标注，绝不静默放宽；
4. 结果区展示原因码、相似度、阈值、活体状态、同意状态、见证人，做到可解释；
5. 数据来自 SQLite 报名库（与网页操作面板同源），不再读 candidates.json。
"""
import json
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import capture
import accounts
import config
import liveness
from action_logger import ActionLogger
from audit import AuditLogger
from face_index import FaceIndex
from models import REASON_TEXT
from store import CandidateStore
from verifier import FaceVerifier

# 所有者可切换：是否隐藏启动器的命令行窗口（由 .bat 读取该标记文件）
HIDE_CMD_FLAG = os.path.join(str(config.DATA_ROOT), "hide_cmd.on")

# 命令控制台的执行类型：下拉框显示中文，执行时再映射回 cmd / python / ollama / ai
CONSOLE_MODE_LABELS = {"cmd": "系统命令 (cmd)",
                       "python": "Python 代码",
                       "ollama": "AI 辅助 (ollama)",
                       "ai": "🤖 AI 对话"}
CONSOLE_MODE_KEYS = dict((v, k) for k, v in CONSOLE_MODE_LABELS.items())

# pythonw 隐藏窗口启动时 stdout/stderr 为 None，兜住避免 print 崩溃
config.ensure_stdio("gui_console.log")


class FaceVerifyGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("智能考场 · 人脸识别与身份核对")
        # 注意：这里设的尺寸会被登录页立刻覆盖（_build_login_ui 用 560x440），
        # 真正的主界面尺寸在 _do_login 里设置（1180x900）。
        # 要调窗口大小请改 _do_login，改这里没有任何效果。
        self.root.geometry("980x780")

        self.store = CandidateStore()
        self.candidates = {}
        self.index = FaceIndex()
        self.audit = AuditLogger()
        self.action_log = ActionLogger()
        self.verifier = None
        self._pending_doc_id = None          # 待证件核对的考生（人脸通过后自动带出）
        self.user = None                     # 登录用户 {"username","role","display_name"}

        self.video_stream = None        # 实时视频流（video.VideoStream）
        self._video_seq = -1            # 已显示的帧序号
        self._photo = None              # 防止 tkinter 回收图像对象
        self.live_image = None          # 内存中的现场图像（OpenCV BGR）
        # 自动核验（仅所有者面板可开启）
        self.auto_verify = tk.BooleanVar(value=False)
        self._verifying = False         # 防止自动核验并发叠加
        self._auto_sched = False        # after 调度是否已启动
        self.live_token = None          # 现场令牌
        self.live_desc = "未采集"

        # 所有者设置：人脸通过后是否自动带出报名库证件信息（默认开启）
        self.auto_fill_doc = tk.BooleanVar(value=accounts.auto_fill_doc_enabled())

        # AI 助手忙状态守护（防止 ollama 请求卡死后 UI 永远锁死）
        self._ai_busy_since = 0
        self._ai_guard_sched = False

        # 操作人员必须先登录（登录界面直接建在主窗口内，避免 Toplevel/transient 不显示的问题）
        self._build_login_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 登录（直接建在主窗口内，最稳） ----------------
    def _build_login_ui(self):
        """登录界面：不使用 Toplevel，避免弹窗在某些环境下不出现"""
        try:
            accounts.ensure_schema_and_seed()
        except Exception as e:
            messagebox.showerror("账户初始化失败", str(e)[:200])
            self.root.destroy()
            return

        self.root.title("智能考场 · 人脸识别与身份核对　｜　登录")
        self.root.geometry("560x440")
        box = ttk.Frame(self.root, padding=30)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="智能考场", font=("Microsoft YaHei", 20, "bold")).pack(pady=(30, 2))
        ttk.Label(box, text="人脸识别与身份核对系统", font=("Microsoft YaHei", 11),
                  foreground="#555").pack(pady=(0, 22))

        card = ttk.LabelFrame(box, text="操作人员登录", padding=18)
        card.pack(fill="x", padx=20)
        ttk.Label(card, text="用户名：").grid(row=0, column=0, sticky="e", pady=8)
        self.login_user = tk.StringVar()
        e_u = ttk.Entry(card, textvariable=self.login_user, width=24, font=("Consolas", 12))
        e_u.grid(row=0, column=1, pady=8, padx=(0, 6))
        ttk.Label(card, text="口　令：").grid(row=1, column=0, sticky="e", pady=8)
        self.login_pwd = tk.StringVar()
        e_p = ttk.Entry(card, textvariable=self.login_pwd, width=24, show="*", font=("Consolas", 12))
        e_p.grid(row=1, column=1, pady=8, padx=(0, 6))

        self.login_msg = ttk.Label(box, text="", foreground="#d9534f", font=("Microsoft YaHei", 10))
        self.login_msg.pack(pady=(14, 4))

        bar = ttk.Frame(box)
        bar.pack(pady=6)
        ttk.Button(bar, text="登  录", command=self._do_login, width=12).pack(side="left", padx=8)
        ttk.Button(bar, text="退  出", command=self.root.destroy, width=12).pack(side="left", padx=8)

        ttk.Label(box, text="提示：所有者 owner ｜ 管理员 teacher1（账户与网页端通用）",
                  foreground="#999", font=("Microsoft YaHei", 9)).pack(pady=(16, 0))
        self.root.bind("<Return>", lambda e: self._do_login())
        e_p.focus_set()

    def _do_login(self):
        user, err = accounts.authenticate(self.login_user.get(), self.login_pwd.get(),
                                          ip="local-gui")
        if not user:
            self.login_msg.config(text=err)
            self.login_pwd.set("")
            return
        self.user = user
        self.action_log.set_operator(user["username"])
        # 清空登录界面，构建主界面
        for w in self.root.winfo_children():
            w.destroy()
        self.root.unbind("<Return>")
        self.root.geometry("1180x900")
        self._build_ui()
        self.root.title("智能考场 · 人脸识别与身份核对　｜　%s（%s）"
                        % (user.get("display_name") or user["username"],
                           accounts.role_text(user["role"])))
        self.action_log.log("GUI启动", "操作人=%s 角色=%s"
                            % (user["username"], accounts.role_text(user["role"])))
        if user.get("must_change_password"):
            messagebox.showwarning("建议修改口令",
                                   "账户 %s 使用的是默认口令，建议尽快修改：\n"
                                   "「操作记录」→「修改口令」。" % user["username"])
        self._load_data_async()

    # ---------------- UI ----------------
    def _build_ui(self):
        # 可滚动主区：内容变多时整页可滚，底部结果区不会被挤掉
        canvas = tk.Canvas(self.root, highlightthickness=0)
        vsb = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        self._canvas = canvas
        self._body = body
        canvas.create_window((0, 0), window=body, anchor="nw", tags="bodywin")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("bodywin", width=e.width))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        top = ttk.LabelFrame(body, text="核验参数", padding=10)
        top.pack(fill="x", padx=10, pady=6)
        ttk.Label(top, text="设备编号：").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar(value="GATE-A-01")
        ttk.Entry(top, textvariable=self.device_var, width=16).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(top, text="操作人：").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.operator_var = tk.StringVar(value=os.environ.get("USERNAME", "operator"))
        ttk.Entry(top, textvariable=self.operator_var, width=14).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(top, text="见证人(人工核验)：").grid(row=0, column=4, sticky="w", padx=(12, 0))
        self.witness_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.witness_var, width=14).grid(row=0, column=5, sticky="w", padx=4)

        ttk.Label(top, text="考生编号：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.candidate_var = tk.StringVar()
        self.candidate_combo = ttk.Combobox(top, textvariable=self.candidate_var, width=14, state="readonly")
        self.candidate_combo.grid(row=1, column=1, sticky="w", padx=4, pady=(8, 0))
        ttk.Label(top, text="核验模式：").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(8, 0))
        self.mode_var = tk.StringVar(value="1:1")
        ttk.Radiobutton(top, text="1:1 入场核验", variable=self.mode_var, value="1:1").grid(row=1, column=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(top, text="1:N 考场抽查", variable=self.mode_var, value="1:N").grid(row=1, column=4, sticky="w", pady=(8, 0))

        self.consent_label = ttk.Label(top, text="同意状态：-", foreground="#b00")
        self.consent_label.grid(row=1, column=5, sticky="w", padx=(12, 0), pady=(8, 0))

        # 证件核对（人脸通过后自动带出）—— 置于顶部，核验确认是最后一道关，最该显眼
        self.doc_frame = ttk.LabelFrame(body, text="证件核对（人脸通过后自动带出，请对照证件实物确认）", padding=10)
        self.doc_frame.pack(fill="x", padx=10, pady=(6, 2))
        self.doc_label = ttk.Label(self.doc_frame, text="尚未核验", foreground="#c07000",
                                   font=("Microsoft YaHei", 13, "bold"))
        self.doc_label.grid(row=0, column=0, columnspan=4, sticky="w")
        self.doc_ok_btn = ttk.Button(self.doc_frame, text="✓ 证件一致（确认）",
                                     command=lambda: self._confirm_doc(True), state="disabled")
        self.doc_ok_btn.grid(row=1, column=0, padx=4, pady=(8, 0))
        self.doc_bad_btn = ttk.Button(self.doc_frame, text="✗ 证件不符（拦截）",
                                      command=lambda: self._confirm_doc(False), state="disabled")
        self.doc_bad_btn.grid(row=1, column=1, padx=4, pady=(8, 0))
        ttk.Label(self.doc_frame, text="库中只存掩码（明文不可逆）：此处供人工目视核对证件实物。",
                  foreground="#888").grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 现场图像：实时视频（推荐）
        live = ttk.LabelFrame(body, text="现场视频（实时，边看边核验）", padding=10)
        live.pack(fill="x", padx=10, pady=6)
        ttk.Label(live, text="摄像头：").grid(row=0, column=0, sticky="w")
        self.camera_var = tk.StringVar(value="0")
        ttk.Entry(live, textvariable=self.camera_var, width=4).grid(row=0, column=1, sticky="w", padx=4)
        self.video_btn = ttk.Button(live, text="开启实时视频", command=self._toggle_video)
        self.video_btn.grid(row=0, column=2, padx=4)
        ttk.Button(live, text="用当前画面核验", command=self._use_live_frame).grid(row=0, column=3, padx=4)
        ttk.Button(live, text="单帧抓拍（多帧取最清晰）", command=self._capture).grid(row=0, column=4, padx=4)
        self.photo_btn = ttk.Button(live, text="选择图片", command=self._choose_photo)
        self.photo_btn.grid(row=0, column=5, padx=4)
        self.fps_label = ttk.Label(live, text="FPS: 0.0", foreground="#1f56b8",
                                   font=("Consolas", 12, "bold"))
        self.fps_label.grid(row=0, column=6, sticky="e", padx=(16, 4))
        live.columnconfigure(6, weight=1)

        # 命令控制台开关按钮：位于实时视频区右上。
        # 它只负责**显示 / 隐藏视频右侧的内置控制台**（不再弹独立窗口）。
        # 仅在有权限且总开关开启时才出现——默认关闭，所以全新安装看不到任何入口。
        # 显隐统一由 _sync_cmd_btn() 控制（所有者开关随时可变，这里不写死）。
        self.cmd_nav_btn = ttk.Button(live, text="⌨ 命令控制台",
                                      command=self._toggle_console_panel)
        self.cmd_nav_btn.grid(row=0, column=7, padx=(10, 2))

        # 「🤖 AI 助手」独立入口：一键展开控制台并切到 AI 对话模式——
        # "想和 AI 对话"不该要求用户先理解命令控制台是什么。
        # 可见性与命令控制台一致，且仅所有者层级（显隐同样由 _sync_cmd_btn 控制）。
        self.ai_nav_btn = ttk.Button(live, text="🤖 AI 助手",
                                     command=self._open_ai_chat)
        self.ai_nav_btn.grid(row=0, column=8, padx=(2, 2))

        # 视频画面 + 内置命令控制台：**并排**放在同一行（视频在左，cmd 在右）。
        # 单独用一个容器才能让两者 side-by-side；控制台默认隐藏，点上面的按钮展开。
        media = ttk.Frame(live)
        media.grid(row=1, column=0, columnspan=8, sticky="ew", pady=8)

        self.video_label = tk.Label(media, text="视频未开启\n（点「开启实时视频」后此处显示实时画面与 FPS）",
                                    background="#111", foreground="#aaa", width=56, height=14)
        self.video_label.pack(side="left", anchor="n")

        self.console_panel = ttk.LabelFrame(media, text="命令控制台", padding=6)
        self._build_console_panel(self.console_panel)   # 先把内容建好，但暂不 pack（默认隐藏）
        self._sync_cmd_btn()

        self.video_status = ttk.Label(live, text="视频未开启", foreground="#666")
        self.video_status.grid(row=2, column=0, columnspan=8, sticky="w")

        # 操作按钮：并入视频块；只留常用项，其余收进「更多操作」
        ttk.Separator(live, orient="horizontal").grid(row=3, column=0, columnspan=8,
                                                      sticky="ew", pady=(10, 6))
        act = ttk.Frame(live)
        act.grid(row=4, column=0, columnspan=8, sticky="w")
        self.verify_btn = ttk.Button(act, text="开始核验", command=self._start_verify)
        self.verify_btn.pack(side="left", padx=4)

        # 自动核验开关：所有者始终可用；管理员需所有者授权（标记文件）
        can_auto = bool(self.user) and accounts.can_auto_verify(self.user.get("role"))
        self.auto_check = ttk.Checkbutton(act, text="自动核验（免点击）",
                                          variable=self.auto_verify,
                                          command=self._toggle_auto_verify)
        self.auto_check.pack(side="left", padx=(14, 4))
        if not can_auto:
            self.auto_check.config(state="disabled")
            ttk.Label(act, text="（需所有者授权）", foreground="#b00").pack(side="left")

        self.more_btn = ttk.Button(act, text="更多操作 ▾", width=12, command=self._toggle_more)
        self.more_btn.pack(side="left", padx=(16, 4))

        # 折叠区（次要操作）
        self._more_shown = False
        self.more_frame = ttk.Frame(live)
        ttk.Button(self.more_frame, text="为该考生建档",
                   command=lambda: self._build_index(only_selected=True)).pack(side="left", padx=4)
        ttk.Button(self.more_frame, text="登记单独同意",
                   command=self._register_consent).pack(side="left", padx=4)
        ttk.Button(self.more_frame, text="操作记录",
                   command=self._show_records).pack(side="left", padx=4)
        ttk.Button(self.more_frame, text="校验审计链",
                   command=self._check_chain).pack(side="left", padx=4)
        ttk.Button(self.more_frame, text="清空结果",
                   command=self._clear_result).pack(side="left", padx=4)
        ttk.Button(self.more_frame, text="📥 名单导入",
                   command=self._import_roster).pack(side="left", padx=4)
        self.manual_btn = ttk.Button(self.more_frame, text="➕ 手动添加考生",
                                     command=self._manual_add_candidate)
        self.manual_btn.pack(side="left", padx=4)
        if not accounts.can_manual_add((self.user or {}).get("role")):
            self.manual_btn.config(state="disabled")

        # 注：命令控制台入口已移到「现场视频」区右侧（见 _build_ui 中 cmd_nav_btn），
        # 不再放在「更多操作」里，避免和次要操作混在一起、也不容易被误点。

        ttk.Button(self.more_frame, text="🔄 切换账号",
                   command=self._switch_account).pack(side="left", padx=(16, 4))

        # 身份要素：语义随所有者「大考模式」开关切换
        #   大考模式开启 -> 四要素（身份证号必校验）
        #   大考模式关闭 -> 三要素（身份证不参与校验，以准考证号核验）——不再显示"必须身份证"
        elem = ttk.LabelFrame(body, text="身份要素校验", padding=10)
        elem.pack(fill="x", padx=10, pady=6)
        self.mode_label = ttk.Label(elem, text=accounts.identity_mode_text(),
                                    font=("Microsoft YaHei", 10, "bold"), foreground="#1f56b8")
        self.mode_label.grid(row=0, column=0, columnspan=7, sticky="w")

        # ---- 核验输入：开始核验时先校验这些值，不一致直接拦截 ----
        ttk.Label(elem, text="身份证号：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.elem_id_var = tk.StringVar()
        ttk.Entry(elem, textvariable=self.elem_id_var, width=24
                  ).grid(row=1, column=1, sticky="w", padx=4, pady=(8, 0))
        ttk.Label(elem, text="准考证号：").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(8, 0))
        self.elem_ticket_var = tk.StringVar()
        ttk.Entry(elem, textvariable=self.elem_ticket_var, width=16
                  ).grid(row=1, column=3, sticky="w", padx=4, pady=(8, 0))
        ttk.Label(elem, text="座位号：").grid(row=1, column=4, sticky="w", padx=(12, 0), pady=(8, 0))
        self.elem_seat_var = tk.StringVar()
        ttk.Entry(elem, textvariable=self.elem_seat_var, width=10
                  ).grid(row=1, column=5, sticky="w", padx=4, pady=(8, 0))
        ttk.Button(elem, text="⇩ 按所选考生带入",
                   command=self._fill_elements_from_candidate
                   ).grid(row=1, column=6, sticky="w", padx=(12, 0), pady=(8, 0))
        self.mode_hint = ttk.Label(elem, text=accounts.identity_mode_detail(),
                                   foreground="#888", wraplength=880, justify="left")
        self.mode_hint.grid(row=2, column=0, columnspan=7, sticky="w", pady=(6, 0))

        # ---- 报名库带出（只读）：人脸通过后供工作人员对照证件实物目视核对 ----
        ttk.Separator(elem, orient="horizontal").grid(row=3, column=0, columnspan=7,
                                                      sticky="ew", pady=(8, 4))
        ttk.Label(elem, text="报名库带出（只读）：").grid(row=4, column=0, sticky="w")
        self.id_card_var = tk.StringVar()
        self.id_card_entry = ttk.Entry(elem, textvariable=self.id_card_var, width=24,
                                       state="readonly")
        self.id_card_entry.grid(row=4, column=1, sticky="w", padx=4)
        ttk.Label(elem, text="准考证：").grid(row=4, column=2, sticky="w", padx=(12, 0))
        self.ticket_var = tk.StringVar()
        self.ticket_entry = ttk.Entry(elem, textvariable=self.ticket_var, width=16,
                                      state="readonly")
        self.ticket_entry.grid(row=4, column=3, sticky="w", padx=4)
        ttk.Label(elem, text="座位：").grid(row=4, column=4, sticky="w", padx=(12, 0))
        self.seat_var = tk.StringVar()
        self.seat_entry = ttk.Entry(elem, textvariable=self.seat_var, width=10,
                                    state="readonly")
        self.seat_entry.grid(row=4, column=5, sticky="w", padx=4)
        ttk.Label(elem, text="身份证号仅显示掩码（库中只存加盐哈希，明文不可逆）：请对照证件实物目视核对。",
                  foreground="#888").grid(row=5, column=0, columnspan=7, sticky="w", pady=(6, 0))

        out = ttk.LabelFrame(body, text="核验结果", padding=8)
        out.pack(fill="both", expand=True, padx=10, pady=6)
        self.output = scrolledtext.ScrolledText(out, wrap="word", height=18)
        self.output.pack(fill="both", expand=True)
        self.output.tag_config("PASS", foreground="#0a7d28")
        self.output.tag_config("FAIL", foreground="#c00000")
        self.output.tag_config("MANUAL", foreground="#c07000")

        self.status = ttk.Label(body, text="初始化中…", foreground="#666")
        self.status.pack(fill="x", padx=12, pady=(0, 6))

    def _toggle_more(self):
        """展开/收起次要操作按钮（为该考生建档 / 登记同意 / 操作记录 / 校验链 / 清空）"""
        self._more_shown = not getattr(self, "_more_shown", False)
        if self._more_shown:
            self.more_frame.grid(row=5, column=0, columnspan=7, sticky="w", pady=(6, 0))
            self.more_btn.config(text="收起操作 ▴")
        else:
            self.more_frame.grid_forget()
            self.more_btn.config(text="更多操作 ▾")

    def _import_roster(self):
        """名单导入（Excel/CSV）——仅所有者可用"""
        if not (self.user and accounts.can_clear_logs(self.user.get("role"))):
            messagebox.showerror("权限不足", "名单导入仅所有者（owner）可用。")
            return
        path = filedialog.askopenfilename(
            title="选择考生名单（Excel 或 CSV）",
            filetypes=[("名单文件", "*.xlsx *.xlsm *.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            import roster
            info = roster.parse_roster(path)          # 先解析（不写库）
            items, warnings = info
        except Exception as e:
            messagebox.showerror("名单解析失败", str(e)[:300])
            return
        preview = "\n".join("  %s ｜ %s ｜ 准考证 %s ｜ 座位 %s"
                            % (it["candidate_id"], it["name"] or "-",
                               it["ticket_no"] or "-", it["seat_no"] or "-")
                            for it in items[:8])
        more = "\n  ……" if len(items) > 8 else ""
        warn = ("\n\n注意：\n" + "\n".join("  " + w for w in warnings[:5])) if warnings else ""
        if not messagebox.askyesno(
                "确认导入名单",
                "文件：%s\n\n共解析出 %d 名考生：\n%s%s%s\n\n"
                "导入将按考生编号新增/覆盖（身份证号加盐哈希存储）。确定继续？"
                % (os.path.basename(path), len(items), preview, more, warn)):
            return
        try:
            import roster
            r = roster.import_roster(path, operator=self.user["username"])
        except Exception as e:
            messagebox.showerror("导入失败", str(e)[:300])
            self.action_log.log("名单导入失败", str(e)[:120])
            return
        self.action_log.log("名单导入", "文件=%s 写入=%d 共=%d"
                            % (os.path.basename(path), r["imported"], r["total"]))
        messagebox.showinfo("导入完成",
                            "共 %d 行，写入/更新 %d 条考生记录。\n"
                            "身份证号已加盐哈希存储，界面仅显示掩码。\n\n"
                            "下一步：按「为该考生建档」提取人脸特征。"
                            % (r["total"], r["imported"]))
        self._load_data_async()                       # 重新加载报名库

    def _switch_account(self):
        """切换账号：停止视频/自动核验 -> 清空界面 -> 回到登录页"""
        if not messagebox.askyesno("切换账号", "确定退出当前账号并返回登录界面？"):
            return
        who = (self.user or {}).get("username", "-")
        self.action_log.log("切换账号", "操作人=%s" % who)
        try:
            if self.video_stream is not None:
                self.video_stream.stop()
        except Exception:
            pass
        self.video_stream = None
        try:
            self.auto_verify.set(False)
        except Exception:
            pass
        try:
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind("<Return>")
        except Exception:
            pass
        for w in self.root.winfo_children():
            w.destroy()
        self.user = None
        self.root.geometry("560x440")
        self._build_login_ui()

    def _on_mousewheel(self, event):
        """鼠标滚轮滚动整页；结果区 ScrolledText 自己滚，不拦截"""
        try:
            if isinstance(event.widget, tk.Text):
                return
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    # ---------------- 数据与模型 ----------------
    def _load_data_async(self):
        def work():
            try:
                self.candidates = self.store.load()
                if os.path.exists(config.INDEX_PATH):
                    self.index.load()
                self.root.after(0, self._after_load, None)
                self._warmup_model()                  # 数据加载完就后台预热模型
            except Exception as e:
                self.root.after(0, self._after_load, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _after_load(self, err):
        if err:
            self.status.config(text="加载失败：%s" % err)
            self.action_log.log("加载失败", str(err)[:120])
            return
        cids = sorted(self.candidates)
        self.candidate_combo["values"] = cids
        if cids:
            self.candidate_var.set(cids[0])
            self._refresh_consent()
        self.candidate_combo.bind("<<ComboboxSelected>>", lambda e: self._on_candidate_change())
        self.verifier = FaceVerifier(self.candidates, self.index,
                                     device_id=self.device_var.get(), logger=self.audit,
                                     operator=self.operator_var.get())
        # 说明：这里**不再注入"永远放行"的假校验器**。核验器使用默认的
        # _check_four_elements，语义随所有者「大考模式」开关切换
        # （大考=四要素、身份证必校验；常规=三要素、以准考证号核验）。
        missing = [c for c in self.candidates if not self.index.has(c)]
        self.status.config(text="报名库 %d 人；特征索引 %d 人；未建档 %d 人"
                                % (len(self.candidates), self.index.size, len(missing)))
        self.action_log.log("加载报名库", "共 %d 人，特征索引 %d 人" % (len(self.candidates), self.index.size))
        for k, v in config.describe().items():
            self._append("  %-22s %s" % (k, v))
        self._append("  %-22s %s" % ("身份要素模式", accounts.identity_mode_text()))
        self._append("  %-22s %s" % ("当前严格模式", "开启（非现场来源拒绝放行）"
                                     if accounts.strict_mode_enabled() else "已关闭（允许本地图片，留痕）"))
        self._append("-" * 64)
        self._refresh_identity_mode()

    def _on_candidate_change(self):
        self._refresh_consent()
        self.action_log.log("切换考生", "考生编号=%s" % self.candidate_var.get())
        self._auto_fill_from(self.candidate_var.get())
        self._enable_doc_check(None, False)

    def _refresh_consent(self):
        c = self.candidates.get(self.candidate_var.get())
        if not c:
            return
        txt = "同意状态：%s" % (c.consent_status or "未登记")
        self.consent_label.config(text=txt,
                                  foreground="#0a7d28" if c.consent_ok() else "#b00")

    def _auto_fill_from(self, candidate_id):
        """
        自动补齐身份要素**展示项**（只读）：身份证掩码 / 准考证 / 座位。
        仅用于人工目视核对证件实物；参与算法比对的输入在「身份要素校验」的核验输入框里，
        由操作人员填写（见 _fill_elements_from_candidate）。
        """
        c = self.candidates.get(candidate_id)
        if not c:
            return
        self.id_card_var.set(c.id_card_masked or "（未登记）")
        self.ticket_var.set(c.ticket_no or "-")
        self.seat_var.set(c.seat_no or "-")
        self._flash_doc_fields()
        self.action_log.log("自动补齐身份信息",
                            "考生=%s 准考证=%s 座位=%s" % (c.candidate_id, c.ticket_no, c.seat_no))

    def _flash_doc_fields(self):
        """报名信息带出时给只读字段一个黄色高亮闪烁，提醒工作人员「这一片刚更新了」"""
        fields = [getattr(self, n, None) for n in ("id_card_entry", "ticket_entry", "seat_entry")]
        fields = [w for w in fields if self._widget_alive(w)]
        if not fields:
            return
        for w in fields:
            try:
                w.config(background="#fef08a")
            except tk.TclError:
                pass
        def restore():
            for w in fields:
                try:
                    w.config(background="")
                except tk.TclError:
                    pass
        try:
            self.root.after(900, restore)
        except (tk.TclError, RuntimeError):
            pass

    def _enable_doc_check(self, candidate_id, enabled):
        """人脸通过后，把该考生的证件信息（掩码）显示出来供目视核对"""
        c = self.candidates.get(candidate_id)
        if enabled and c is not None:
            if self.auto_fill_doc.get():
                self._auto_fill_from(candidate_id)          # 自动补齐并高亮
            self.doc_label.config(text=self.verifier.document_info(c), foreground="#1f56b8")
            self.doc_ok_btn.config(state="normal")
            self.doc_bad_btn.config(state="normal")
            self._pending_doc_id = candidate_id
        else:
            self.doc_label.config(text="尚未核验（扫脸通过后自动带出身份信息）", foreground="#666")
            self.doc_ok_btn.config(state="disabled")
            self.doc_bad_btn.config(state="disabled")
            self._pending_doc_id = None

    def _confirm_doc(self, ok):
        cid = getattr(self, "_pending_doc_id", None)
        if not cid or self.verifier is None:
            return
        rec = self.verifier.confirm_document(
            cid, operator=self.operator_var.get().strip(),
            witness=self.witness_var.get().strip(), ok=ok)
        self._append("-" * 64)
        self._append("证件核对：%s（%s）" % (rec.result, rec.reason_code))
        self._append("          %s" % rec.id_card_display)
        if not ok:
            messagebox.showerror("证件不符", "已记录拦截：%s\n\n请立即按考务规定处置。"
                                 % REASON_TEXT.get(rec.reason_code, rec.reason_code))
        self.action_log.log("证件核对", "考生=%s 结论=%s" % (cid, rec.reason_code))
        self._enable_doc_check(None, False)

    # ---------------- 操作记录与日志（仅所有者可清空） ----------------
    def _show_records(self):
        """查看操作记录：核验审计（哈希链）+ 界面操作日志。所有者可清空日志。"""
        win = tk.Toplevel(self.root)
        win.title("操作记录　｜　当前用户：%s（%s）"
                  % (self.user["username"], accounts.role_text(self.user["role"])))
        win.geometry("900x620")
        can_clear = accounts.can_clear_logs(self.user["role"])

        # 所有者专属：自动核验开关
        if can_clear:
            owner_box = ttk.LabelFrame(win, text="所有者设置", padding=8)
            owner_box.pack(fill="x", padx=10, pady=(10, 2))
            ttk.Checkbutton(owner_box, text="启用自动核验（实时视频下每 2.5 秒自动扫脸并输出结果）",
                            variable=self.auto_verify, command=self._toggle_auto_verify).pack(anchor="w")
            ttk.Label(owner_box,
                      text="开启后需保证主界面「实时视频」已开启；1:1 模式需选定考生。关闭即停止。",
                      foreground="#888").pack(anchor="w", pady=(2, 0))
            ttk.Button(owner_box, text="👥 用户管理（新建/改密/删除管理员）",
                       command=self._show_user_manager).pack(anchor="w", pady=(6, 0))
            self.hide_cmd = tk.BooleanVar(value=os.path.exists(HIDE_CMD_FLAG))
            ttk.Checkbutton(owner_box, text="隐藏启动时的命令行窗口（下次用快捷启动生效）",
                            variable=self.hide_cmd,
                            command=self._toggle_hide_cmd).pack(anchor="w", pady=(4, 0))
            self.allow_admin_auto = tk.BooleanVar(value=accounts.auto_verify_admin_allowed())
            ttk.Checkbutton(owner_box, text="允许管理员使用自动核验（默认仅所有者可用）",
                            variable=self.allow_admin_auto,
                            command=self._toggle_admin_auto).pack(anchor="w", pady=(4, 0))
            self.allow_manual_add = tk.BooleanVar(value=accounts.manual_add_admin_allowed())
            ttk.Checkbutton(owner_box, text="允许管理员手动添加考生（默认关闭）",
                            variable=self.allow_manual_add,
                            command=self._toggle_manual_add).pack(anchor="w", pady=(4, 0))

            # ---- 大考模式（默认关闭）：核验是否必须使用身份证 ----
            self.major_exam = tk.BooleanVar(value=accounts.major_exam_enabled())
            ttk.Checkbutton(owner_box, text="大考模式（必须使用身份证）",
                            variable=self.major_exam,
                            command=self._toggle_major_exam).pack(anchor="w", pady=(10, 0))
            ttk.Label(owner_box,
                      text="开启后：四要素 = 身份证号 + 准考证号 + 座位号 + 姓名，身份证必填并走库内哈希比对；\n"
                           "关闭（默认）：三要素 = 准考证号 + 座位号 + 姓名，身份证不参与校验，以准考证号核验。",
                      foreground="#888", justify="left").pack(anchor="w", pady=(2, 0))

            # ---- 严格模式（默认开启）：非现场来源是否拒绝放行 ----
            self.strict_mode = tk.BooleanVar(value=accounts.strict_mode_enabled())
            ttk.Checkbutton(owner_box, text="严格模式（关闭后可选本地图片核验）",
                            variable=self.strict_mode,
                            command=self._toggle_strict_mode).pack(anchor="w", pady=(10, 0))
            ttk.Label(owner_box,
                      text="默认开启：非设备实时抓拍（「选择图片」/上传照片）一律拒绝放行；\n"
                           "关闭后允许用本地图片核验，但审计记录会如实标注“严格模式已关闭、本次为非现场来源”。",
                      foreground="#888", justify="left").pack(anchor="w", pady=(2, 0))

            # ---- 命令控制台（默认关闭）：所有者完整权限 / 管理员仅 ollama ----
            self.cmd_console = tk.BooleanVar(value=accounts.cmd_console_enabled())
            ttk.Checkbutton(owner_box, text="命令控制台（默认关闭）",
                            variable=self.cmd_console,
                            command=self._toggle_cmd_console).pack(anchor="w", pady=(10, 0))
            ttk.Label(owner_box,
                      text="默认关闭。开启后**所有者**可执行任意命令 / Python 代码；\n"
                           "该能力等价于本机完全控制权，每次执行都写入审计哈希链"
                           "（可事后追溯，但无法阻止执行）。",
                      foreground="#b45309", justify="left").pack(anchor="w", pady=(2, 0))

            self.cmd_console_admin = tk.BooleanVar(value=accounts.cmd_console_admin_allowed())
            ttk.Checkbutton(owner_box, text="允许管理员使用「AI 辅助判断」（仅 ollama）",
                            variable=self.cmd_console_admin,
                            command=self._toggle_cmd_console_admin).pack(anchor="w", pady=(6, 0))
            ttk.Label(owner_box,
                      text="勾选后管理员也能用命令控制台，但**只能**执行 "
                           "`ollama run <模型> [提示词]` 让本地大模型辅助判断；\n"
                           "其它程序与其它 ollama 子命令一律被拒。执行不经过 shell，"
                           "所以模型名之后的管道 / 重定向等符号\n"
                           "只会成为提示词文本（无效），不会被执行。\n"
                           "需先开启上面的「命令控制台」总开关；关掉总开关会一并停用。",
                      foreground="#888", justify="left").pack(anchor="w", pady=(2, 0))

            self.cmd_btn = ttk.Button(owner_box, text="⌨ 打开命令控制台",
                                      command=self._show_cmd_console)
            self.cmd_btn.pack(anchor="w", pady=(6, 0))

            # ---- 人脸通过后是否自动带出报名库证件信息（默认开启）----
            ttk.Checkbutton(owner_box, text="人脸通过后自动带出证件信息（只读）",
                            variable=self.auto_fill_doc,
                            command=self._toggle_auto_fill_doc).pack(anchor="w", pady=(10, 0))
            ttk.Label(owner_box,
                      text="开启：扫脸通过后，下方「报名库带出（只读）」自动显示该考生身份证掩码、准考证、座位；\n"
                           "关闭：不自动跳出，工作人员需手动点「按所选考生带入」或查看报名库。",
                      foreground="#888", justify="left").pack(anchor="w", pady=(2, 0))

            self._sync_cmd_btn()

        ttk.Label(win, text="核验审计记录（哈希链，含操作人 / 见证人 / 结果 / 原因码）",
                  font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        txt = scrolledtext.ScrolledText(win, height=18, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=10)
        for r in self.audit.query(limit=300):
            txt.insert("end", "%s | %-6s %-20s 考生=%-6s 操作人=%-10s 见证人=%-8s 分数=%s\n"
                       % (r.get("timestamp"), r.get("result"), r.get("reason_code"),
                          r.get("candidate_id") or "-", r.get("operator") or "-",
                          r.get("witness") or "-", r.get("score")))
        txt.see("end")
        txt.config(state="disabled")

        ttk.Label(win, text="界面操作日志", font=("Microsoft YaHei", 10, "bold")
                  ).pack(anchor="w", padx=10, pady=(8, 2))
        txt2 = scrolledtext.ScrolledText(win, height=8, font=("Consolas", 9))
        txt2.pack(fill="both", expand=True, padx=10)
        for line in self.action_log.query(limit=200):
            txt2.insert("end", line + "\n")
        txt2.see("end")
        txt2.config(state="disabled")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=8)

        def clear_logs():
            # 双保险：界面按钮 + 这里再次校验角色
            if not accounts.can_clear_logs(self.user["role"]):
                messagebox.showerror("权限不足", "只有所有者（owner）可以清空日志。")
                return
            if not messagebox.askyesno("确认清空",
                                       "将清空核验审计日志与界面操作日志。\n"
                                       "清空前会自动备份；该动作本身会记入新日志。\n\n"
                                       "确定继续吗？"):
                return
            a = self.audit.clear_all(operator=self.user["username"])
            b = self.action_log.clear(operator=self.user["username"])
            self.action_log.log("清空日志", "操作人=%s 审计%d条 操作日志%d条"
                                % (self.user["username"], a["removed"], b["removed"]))
            messagebox.showinfo("已清空",
                                "审计日志清除 %d 条（备份：%s）\n操作日志清除 %d 条（备份：%s）"
                                % (a["removed"], a["backup"] or "无",
                                   b["removed"], b["backup"] or "无"))
            win.destroy()

        def change_pwd():
            self._change_password_dialog()

        if can_clear:
            ttk.Button(bar, text="清空日志（所有者）", command=clear_logs).pack(side="left", padx=4)
        else:
            ttk.Label(bar, text="当前角色为「%s」，无权清空日志（仅所有者可清空）"
                      % accounts.role_text(self.user["role"]), foreground="#b00").pack(side="left", padx=4)
        ttk.Button(bar, text="修改口令", command=change_pwd).pack(side="left", padx=4)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)

    def _toggle_hide_cmd(self):
        """所有者开关：是否隐藏启动器的命令行窗口（写标记文件，由 .bat 读取）"""
        on = self.hide_cmd.get()
        try:
            os.makedirs(os.path.dirname(HIDE_CMD_FLAG), exist_ok=True)
            if on:
                with open(HIDE_CMD_FLAG, "w", encoding="utf-8") as f:
                    f.write("1\n")
            elif os.path.exists(HIDE_CMD_FLAG):
                os.remove(HIDE_CMD_FLAG)
        except OSError as e:
            messagebox.showerror("设置失败", str(e))
            return
        self.action_log.log("隐藏命令行窗口", "开启" if on else "关闭")
        messagebox.showinfo("已设置", "下次通过快捷启动时%s命令行窗口。"
                            % ("隐藏" if on else "显示（便于查看报错）"))

    def _toggle_admin_auto(self):
        """所有者开关：是否允许管理员使用自动核验"""
        on = self.allow_admin_auto.get()
        ok, msg = accounts.set_auto_verify_admin(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            return
        self.action_log.log("自动核验权限", "管理员%s" % ("可用" if on else "不可用"))
        messagebox.showinfo("已设置", "%s。\n管理员需重新登录后生效。" % msg)

    def _toggle_manual_add(self):
        """所有者开关：是否允许管理员手动添加考生（默认关闭）"""
        on = self.allow_manual_add.get()
        ok, msg = accounts.set_manual_add(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            return
        self.action_log.log("手动添加权限", "管理员%s" % ("可用" if on else "不可用"))
        if hasattr(self, "manual_btn"):
            allowed = accounts.can_manual_add((self.user or {}).get("role"))
            self.manual_btn.config(state=("normal" if allowed else "disabled"))
        messagebox.showinfo("已设置", "%s。\n管理员需重新登录后生效。" % msg)

    def _manual_add_candidate(self):
        """手动添加考生（逐条录入）——受所有者开关控制"""
        if not accounts.can_manual_add((self.user or {}).get("role")):
            messagebox.showerror("权限不足",
                                 "手动添加考生当前已关闭。\n"
                                 "请由所有者在「操作记录 → 所有者设置」中开启。")
            return
        win = tk.Toplevel(self.root)
        win.title("手动添加考生")
        win.geometry("430x400")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        fields = [("candidate_id", "考生编号*", ""), ("name", "姓名", ""),
                  ("id_card", "身份证号", ""), ("ticket_no", "准考证号", ""),
                  ("seat_no", "座位号", ""), ("reference_photo", "参考照路径", "")]
        vars_ = {}
        for i, (key, label, default) in enumerate(fields):
            ttk.Label(f, text=label + "：").grid(row=i, column=0, sticky="e", pady=5)
            v = tk.StringVar(value=default)
            vars_[key] = v
            ttk.Entry(f, textvariable=v, width=28).grid(row=i, column=1, pady=5, padx=(0, 6))
        r = len(fields)
        ttk.Label(f, text="同意状态：").grid(row=r, column=0, sticky="e", pady=5)
        consent = tk.StringVar(value="未登记")
        ttk.Combobox(f, textvariable=consent, width=25, state="readonly",
                     values=["未登记", "已同意", "已拒绝", "替代通道"]
                     ).grid(row=r, column=1, pady=5, padx=(0, 6))
        msg = ttk.Label(f, text="", foreground="#d9534f")
        msg.grid(row=r + 1, column=0, columnspan=2, pady=6)
        ttk.Label(f, text="* 必填；身份证号将加盐哈希存储，明文不落库。",
                  foreground="#888").grid(row=r + 2, column=0, columnspan=2, sticky="w")

        def save():
            item = {k: v.get().strip() for k, v in vars_.items()}
            item["consent_status"] = consent.get()
            if not item["candidate_id"]:
                msg.config(text="考生编号必填")
                return
            try:
                from store import CandidateStore
                n = CandidateStore().upsert_many([item], operator=self.user["username"])
            except Exception as e:
                msg.config(text=str(e)[:120])
                return
            self.action_log.log("手动添加考生", "考生=%s 写入=%d" % (item["candidate_id"], n))
            messagebox.showinfo("已添加", "考生 %s 已%s。\n下一步可按「为该考生建档」提取人脸特征。"
                                % (item["candidate_id"], "写入" if n else "更新"))
            win.destroy()
            self._load_data_async()

        bar = ttk.Frame(f)
        bar.grid(row=r + 3, column=0, columnspan=2, pady=10)
        ttk.Button(bar, text="保存", command=save).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=6)

    def _show_user_manager(self):
        from tkinter import simpledialog
        if not accounts.can_clear_logs(self.user["role"]):
            messagebox.showerror("权限不足", "只有所有者可管理账户。")
            return
        win = tk.Toplevel(self.root)
        win.title("用户管理（所有者）")
        win.geometry("600x440")
        win.transient(self.root)

        box = scrolledtext.ScrolledText(win, height=14, font=("Consolas", 10))
        box.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        def refresh():
            users = accounts.list_users()
            box.config(state="normal")
            box.delete("1.0", "end")
            box.insert("end", "%-5s %-12s %-8s %-8s %s\n" % ("ID", "用户名", "角色", "状态", "最后登录"))
            box.insert("end", "-" * 68 + "\n")
            for u in users:
                box.insert("end", "%-5s %-12s %-8s %-8s %s\n" % (
                    u["id"], u["username"], accounts.role_text(u["role"]),
                    "启用" if u["is_active"] else "禁用", u["last_login"] or "-"))
            box.config(state="disabled")
        refresh()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=8)

        def create():
            dlg = tk.Toplevel(win)
            dlg.title("新建账户")
            dlg.geometry("340x270")
            dlg.transient(win); dlg.grab_set()
            f = ttk.Frame(dlg, padding=14); f.pack(fill="both", expand=True)
            ttk.Label(f, text="用户名：").grid(row=0, column=0, sticky="e", pady=4)
            u = tk.StringVar(); ttk.Entry(f, textvariable=u).grid(row=0, column=1)
            ttk.Label(f, text="显示名：").grid(row=1, column=0, sticky="e", pady=4)
            d = tk.StringVar(); ttk.Entry(f, textvariable=d).grid(row=1, column=1)
            ttk.Label(f, text="密码(≥8位)：").grid(row=2, column=0, sticky="e", pady=4)
            p = tk.StringVar(); ttk.Entry(f, textvariable=p, show="*").grid(row=2, column=1)
            ttk.Label(f, text="角色：").grid(row=3, column=0, sticky="e", pady=4)
            r = tk.StringVar(value="teacher")
            ttk.Combobox(f, textvariable=r, values=["teacher", "admin"],
                         state="readonly", width=8).grid(row=3, column=1)
            msg = ttk.Label(f, text="", foreground="#d9534f"); msg.grid(row=4, column=0, columnspan=2, pady=6)
            def go():
                ok, m = accounts.create_user(u.get(), d.get(), p.get(), r.get(), by_role=self.user["role"])
                msg.config(text=m, foreground="#0a7d28" if ok else "#d9534f")
                if ok:
                    refresh(); dlg.after(900, dlg.destroy)
            ttk.Button(f, text="创建", command=go).grid(row=5, column=0, columnspan=2)

        def reset():
            uname = simpledialog.askstring("重置密码", "输入要重置密码的用户名：", parent=win)
            if not uname:
                return
            pwd = simpledialog.askstring("重置密码", "输入新密码（≥8位）：", parent=win, show="*")
            if not pwd:
                return
            ok, m = accounts.reset_user_password(uname, pwd, by_role=self.user["role"])
            messagebox.showinfo("结果", m)
            refresh()

        def delete():
            uname = simpledialog.askstring("删除账户", "输入要删除的用户名：", parent=win)
            if not uname:
                return
            if not messagebox.askyesno("确认", "确定删除账户 %s？" % uname):
                return
            ok, m = accounts.delete_user(uname, by_role=self.user["role"])
            messagebox.showinfo("结果", m)
            refresh()

        ttk.Button(bar, text="新建账户", command=create).pack(side="left", padx=4)
        ttk.Button(bar, text="重置密码", command=reset).pack(side="left", padx=4)
        ttk.Button(bar, text="删除账户", command=delete).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", command=refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right", padx=4)

    def _change_password_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("修改口令")
        win.geometry("380x220")
        win.transient(self.root)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="当前口令：").grid(row=0, column=0, sticky="e", pady=4)
        o = tk.StringVar(); ttk.Entry(f, textvariable=o, show="*", width=22).grid(row=0, column=1)
        ttk.Label(f, text="新口令（≥8位）：").grid(row=1, column=0, sticky="e", pady=4)
        n = tk.StringVar(); ttk.Entry(f, textvariable=n, show="*", width=22).grid(row=1, column=1)
        ttk.Label(f, text="确认新口令：").grid(row=2, column=0, sticky="e", pady=4)
        n2 = tk.StringVar(); ttk.Entry(f, textvariable=n2, show="*", width=22).grid(row=2, column=1)
        msg = ttk.Label(f, text="", foreground="#d9534f")
        msg.grid(row=3, column=0, columnspan=2, pady=6)

        def submit():
            if n.get() != n2.get():
                msg.config(text="两次输入的新口令不一致")
                return
            ok, text = accounts.change_password(self.user["username"], o.get(), n.get())
            if not ok:
                msg.config(text=text)
                return
            self.action_log.log("修改口令", "操作人=%s" % self.user["username"])
            messagebox.showinfo("完成", text)
            win.destroy()

        ttk.Button(f, text="确定", command=submit).grid(row=4, column=0, columnspan=2)

    # ---------------- 身份要素模式（大考模式开关）与严格模式（开关） ----------------
    def _refresh_identity_mode(self):
        """按当前所有者开关刷新界面文案：核验项名称 / 说明 / 「选择图片」可用性"""
        if hasattr(self, "mode_label"):
            self.mode_label.config(text=accounts.identity_mode_text())
        if hasattr(self, "mode_hint"):
            self.mode_hint.config(text=accounts.identity_mode_detail())
        self._refresh_photo_button()

    def _refresh_photo_button(self):
        """
        「选择图片」只在严格模式**关闭**时可用。
        注意：这里不把按钮 state 设为 disabled——禁用后点击毫无反馈，正是"静默失败"；
        改为保留可点击 + 文案明示 + 点击时给出明确提示（见 _choose_photo）。
        """
        if not hasattr(self, "photo_btn"):
            return
        if accounts.strict_mode_relaxed():
            self.photo_btn.config(text="选择图片")
        else:
            self.photo_btn.config(text="选择图片（严格模式已禁用）")

    def _fill_elements_from_candidate(self):
        """
        按所选考生把身份要素带入"核验输入"框（便利按钮，不等于自动填充）。
        只带入准考证号 / 座位号；身份证号**只带掩码、明文不可反推**，
        因此大考模式下必须人工输入证件原件号码，这里不会替你填。
        """
        cid = self.candidate_var.get()
        c = self.candidates.get(cid)
        if not c:
            messagebox.showwarning("提示", "请先选择考生。")
            return
        self.elem_ticket_var.set(c.ticket_no or "")
        self.elem_seat_var.set(c.seat_no or "")
        if accounts.major_exam_enabled():
            self.elem_id_var.set("")            # 掩码不是明文，留空由人工输入
            tip = "已带入准考证号/座位号；大考模式要求身份证号，请手工输入证件原件明文。"
        else:
            tip = "已带入准考证号/座位号；常规模式身份证不参与校验，可留空。"
        self.action_log.log("带入身份要素", "考生=%s 准考证=%s 座位=%s（身份证需人工输入）"
                            % (cid, c.ticket_no, c.seat_no))
        self.status.config(text=tip)

    def _toggle_major_exam(self):
        """所有者开关：大考模式（必须使用身份证）"""
        on = self.major_exam.get()
        ok, msg = accounts.set_major_exam(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            return
        self.action_log.log("大考模式", "开启" if on else "关闭")
        self.store.log_change("SET_MAJOR_EXAM", detail="大考模式=%s" % ("开启" if on else "关闭"),
                              operator=(self.user or {}).get("username", "unknown"))
        self._refresh_identity_mode()
        self._append("[设置] %s" % msg)
        self._append("        当前核验项：%s" % accounts.identity_mode_text())
        messagebox.showinfo("已设置", "%s\n\n当前核验项：%s\n（立即生效，无需重启）"
                            % (msg, accounts.identity_mode_text()))

    def _toggle_strict_mode(self):
        """所有者开关：严格模式（关闭后可选本地图片核验）"""
        on = self.strict_mode.get()
        ok, msg = accounts.set_strict_mode(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            return
        self.action_log.log("严格模式", "开启" if on else "关闭")
        self.store.log_change("SET_STRICT_MODE", detail="严格模式=%s" % ("开启" if on else "关闭"),
                              operator=(self.user or {}).get("username", "unknown"))
        self._refresh_photo_button()
        self._append("[设置] %s" % msg)
        messagebox.showinfo("已设置", "%s\n\n（立即生效，无需重启）" % msg)

    # ---------------- 命令控制台（仅所有者 + 默认关闭） ----------------
    def _toggle_cmd_console(self):
        """所有者开关：是否开启命令控制台（默认关闭）"""
        on = self.cmd_console.get()
        if on and not messagebox.askyesno(
                "确认开启命令控制台",
                "开启后，所有者可在本机 GUI 与网页端执行任意系统命令 / Python 代码。\n\n"
                "这等价于把本机（或部署该网页的服务器）的完全控制权交出去：\n"
                "能执行命令，就能读写考生生物特征库、改动程序、安装后门。\n\n"
                "每次执行都会写入审计哈希链，可事后追溯，但**无法阻止执行**。\n\n"
                "确定要开启吗？"):
            self.cmd_console.set(False)
            return
        ok, msg = accounts.set_cmd_console(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            self.cmd_console.set(accounts.cmd_console_enabled())   # 回滚勾选状态
            return
        self.action_log.log("命令控制台开关",
                            "操作人=%s %s" % ((self.user or {}).get("username", "?"), msg))
        self._sync_cmd_btn()
        self._append("[设置] %s" % msg)
        messagebox.showinfo("已设置", "%s\n\n（立即生效，无需重启）" % msg)

    @staticmethod
    def _widget_alive(w):
        """
        控件是否还活着。

        所有者设置面板建在「操作记录」这个 Toplevel 里，窗口一关里面的控件就被销毁，
        但 self.cmd_btn 仍持有那个已死控件的引用；此时再 config() 会抛
        TclError: invalid command name —— 所以每次操作前必须先确认控件存在。
        """
        if w is None:
            return False
        try:
            return bool(w.winfo_exists())
        except tk.TclError:
            return False

    def _toggle_auto_fill_doc(self):
        """所有者开关：人脸通过后是否自动带出报名库证件信息"""
        on = self.auto_fill_doc.get()
        ok, msg = accounts.set_auto_fill_doc(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            self.auto_fill_doc.set(accounts.auto_fill_doc_enabled())
            return
        self.action_log.log("自动带出证件信息", "开启" if on else "关闭")


    def _toggle_cmd_console_admin(self):
        """所有者开关：是否授权管理员使用 ollama AI 辅助（默认关闭）"""
        on = self.cmd_console_admin.get()
        if on and not messagebox.askyesno(
                "确认授权管理员",
                "授权后，「管理员」也能使用命令控制台，但只能执行：\n\n"
                "    ollama run <模型> [提示词]\n\n"
                "用于让本地大模型辅助判断。管理员**不能**执行其它任何命令——\n"
                "白名单只匹配这一种形式，且执行不经过 shell，\n"
                "管道 / 重定向 / 链式命令在语法层即被拒绝。\n\n"
                "需要「命令控制台」总开关已开启；关掉总开关会一并停用。\n\n"
                "确定要授权吗？"):
            self.cmd_console_admin.set(False)
            return
        ok, msg = accounts.set_cmd_console_admin(on)
        if not ok:
            messagebox.showerror("设置失败", msg)
            self.cmd_console_admin.set(accounts.cmd_console_admin_allowed())
            return
        self.action_log.log("管理员AI辅助授权",
                            "操作人=%s %s" % ((self.user or {}).get("username", "?"), msg))
        self._append("[设置] %s" % msg)
        messagebox.showinfo("已设置", "%s\n\n（立即生效，无需重启）" % msg)

    def _sync_cmd_btn(self):
        """入口按钮可用性/可见性跟随开关状态（真正的权限判定仍在校验函数里）"""
        # 所有者设置面板里的按钮（随「操作记录」窗口生灭）
        btn = getattr(self, "cmd_btn", None)
        if self._widget_alive(btn):
            try:
                btn.config(state=("normal" if accounts.cmd_console_enabled() else "disabled"))
            except tk.TclError:
                pass

        # 实时视频区右上的开关按钮：按 grid 管理，用 grid_remove 保留布局参数
        nav = getattr(self, "cmd_nav_btn", None)
        role = (self.user or {}).get("role")
        allowed = accounts.can_cmd_console(role)
        if self._widget_alive(nav):
            try:
                if allowed:
                    nav.grid()
                else:
                    nav.grid_remove()
            except tk.TclError:
                pass

        # 「🤖 AI 助手」入口：仅所有者层级可见（对话/巡视都只对这个层级开放）
        ai_nav = getattr(self, "ai_nav_btn", None)
        if self._widget_alive(ai_nav):
            try:
                if allowed and accounts.cmd_console_tier(role) == accounts.CMD_TIER_FULL:
                    ai_nav.grid()
                else:
                    ai_nav.grid_remove()
            except tk.TclError:
                pass

        # 权限被收回（或总开关关闭）时，已展开的内置控制台也必须一起收起来，
        # 否则会出现"按钮没了但面板还开着"的越权残留。
        panel = getattr(self, "console_panel", None)
        if not allowed and self._widget_alive(panel) and panel.winfo_manager():
            try:
                panel.pack_forget()
            except tk.TclError:
                pass

        # 层级变了要重配类型选择框
        self._sync_console_controls()

    @staticmethod
    def _console_hint_text(is_full):
        """控制台提示语：所有者说清能干什么，管理员说清**只能**干什么"""
        if is_full:
            return ("输入任意系统命令 / Python 代码（Ctrl+Enter 执行）；\n"
                    "直接输入中文问题 = 与 AI 对话，或点右上「🤖 AI 助手」")
        return "仅可执行：ollama run <模型> [提示词]（管理员层级）"

    # AI 对话模式的提示语（选中「🤖 AI 对话」时替换默认提示）
    AI_HINT = ("🤖 AI 对话：直接输入问题，AI 能看到你当前的界面状态；\n"
               "输入 ollama run <模型> 可随时切换模型。")

    def _sync_console_controls(self):
        """
        按当前层级重配内置控制台的标题 / 提示语 / 类型选择框。

        为什么必须重配：面板是在 _build_ui 时一次性建好的，而总开关默认关闭，
        所以那一刻层级通常是 none —— 标题、提示语、类型框都会按"管理员 ollama"来写。
        所有者随后开启总开关时若不重配，就会：
            · 类型框被锁死在 ollama（用不了完整权限）；
            · 提示语仍写着"仅可执行 ollama run"（**真实反馈过的问题**，会误导所有者）。
        这三处必须一起改，只改其中一处就会自相矛盾。
        """
        is_full = (accounts.cmd_console_tier((self.user or {}).get("role"))
                   == accounts.CMD_TIER_FULL)

        panel = getattr(self, "console_panel", None)
        if self._widget_alive(panel):
            try:
                panel.config(text=("命令控制台 · 完整权限" if is_full
                                   else "命令控制台 · AI 辅助（仅 ollama）"))
            except tk.TclError:
                pass

        hint = getattr(self, "console_hint", None)
        if self._widget_alive(hint):
            try:
                in_ai = (is_full and getattr(self, "console_mode", None) is not None
                         and self.console_mode.get() == CONSOLE_MODE_LABELS["ai"])
                hint.config(text=(self.AI_HINT if in_ai
                                  else self._console_hint_text(is_full)))
            except tk.TclError:
                pass

        box_w = getattr(self, "console_mode_box", None)
        if not self._widget_alive(box_w):
            return
        try:
            if is_full:
                vals = (CONSOLE_MODE_LABELS["cmd"],
                        CONSOLE_MODE_LABELS["python"],
                        CONSOLE_MODE_LABELS["ai"])
                box_w.config(values=vals, state="readonly")
                if self.console_mode.get() not in vals:
                    self.console_mode.set(CONSOLE_MODE_LABELS["cmd"])
            else:
                box_w.config(values=(CONSOLE_MODE_LABELS["ollama"],), state="disabled")
                self.console_mode.set(CONSOLE_MODE_LABELS["ollama"])
        except tk.TclError:
            pass

    def _show_cmd_console(self):
        """
        命令控制台窗口（所有者 = 完整权限；管理员 = 仅 ollama）。

        注意：界面上的锁定只是体验，真正的判定是 accounts.cmd_console_tier()；
        这里校验一次，执行时 cmd_console.execute(role=...) 还会再判一次。
        网页端同理——两边都不依赖界面状态。
        """
        role = (self.user or {}).get("role")
        tier = accounts.cmd_console_tier(role)
        if tier == accounts.CMD_TIER_NONE:
            messagebox.showerror("权限不足",
                                 "命令控制台当前不可用。\n\n"
                                 "· 所有者：请先勾选「命令控制台」总开关\n"
                                 "· 管理员：需所有者开启总开关，并授权「AI 辅助判断」")
            return
        import cmd_console

        is_full = (tier == accounts.CMD_TIER_FULL)
        import ai_assistant
        win = tk.Toplevel(self.root)
        win.title(("命令控制台（所有者 · 完整权限）" if is_full
                   else "AI 辅助判断（管理员 · 仅 ollama）")
                  + "　｜　%s（%s）"
                  % (self.user["username"], accounts.role_text(role)))
        win.geometry("880x700")

        if is_full:
            warn = ("⚠ 这里的输入会以本机当前用户身份直接执行，等价于交出本机控制权。\n"
                    "   每次执行都会写入审计哈希链（操作人 / 层级 / 命令 / 退出码 / 耗时）。\n"
                    "   执行工作目录：%s" % config.BASE_DIR)
            warn_fg = "#b45309"
        else:
            warn = ("ℹ 你是管理员：只能执行 `ollama run <模型> [提示词]`，"
                    "让本地大模型辅助判断。\n"
                    "   其它程序与其它 ollama 子命令一律无法执行；执行不经过 shell，\n"
                    "   模型名之后的管道 / 重定向等符号只会成为提示词文本。\n"
                    "   每次执行都会写入审计哈希链。执行工作目录：%s" % config.BASE_DIR)
            warn_fg = "#075985"
        ttk.Label(win, text=warn, foreground=warn_fg, justify="left",
                  font=("Microsoft YaHei", 9)).pack(anchor="w", padx=10, pady=(10, 8))

        top = ttk.Frame(win)
        top.pack(fill="x", padx=10)
        ttk.Label(top, text="类型：").pack(side="left")
        if is_full:
            mode_var = tk.StringVar(value="cmd")
            ttk.Radiobutton(top, text="系统命令", value="cmd",
                            variable=mode_var).pack(side="left")
            ttk.Radiobutton(top, text="Python 代码", value="python",
                            variable=mode_var).pack(side="left", padx=(6, 0))
            ttk.Radiobutton(top, text="AI 对话", value="ai",
                            variable=mode_var).pack(side="left", padx=(6, 16))
        else:
            # 管理员锁定为 ollama：界面不给选择，执行时服务端还会强制一次
            mode_var = tk.StringVar(value="ollama")
            rb = ttk.Radiobutton(top, text="AI 辅助判断（ollama run）", value="ollama",
                                 variable=mode_var)
            rb.pack(side="left", padx=(6, 16))
            rb.config(state="disabled")

        # AI 模式下才需要模型选择（命令/python/ollama 模式可折叠，但为简化布局始终显示）
        ttk.Label(top, text="模型：").pack(side="left")
        ai_model_var = tk.StringVar(value=ai_assistant.DEFAULT_MODEL)
        ai_model_box = ttk.Combobox(top, textvariable=ai_model_var, width=30,
                                    values=(ai_assistant.DEFAULT_MODEL,))
        ai_model_box.pack(side="left")
        ttk.Button(top, text="🔄", width=3,
                   command=lambda: self._ai_refresh_models_popup(ai_model_box)).pack(side="left", padx=(2, 12))

        ttk.Label(top, text="超时(秒)：").pack(side="left")
        timeout_var = tk.StringVar(value=str(cmd_console.DEFAULT_TIMEOUT))
        ttk.Spinbox(top, from_=1, to=cmd_console.MAX_TIMEOUT, width=6,
                    textvariable=timeout_var).pack(side="left")

        input_label = ttk.Label(win, text="命令 / 代码 / AI 消息（Ctrl+Enter 执行）")
        input_label.pack(anchor="w", padx=10, pady=(10, 2))
        code_box = scrolledtext.ScrolledText(win, height=8, font=("Consolas", 10))
        code_box.pack(fill="both", expand=True, padx=10)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=6)
        run_btn = ttk.Button(bar, text="▶ 执行")
        run_btn.pack(side="left")
        ttk.Button(bar, text="清空输出", command=lambda: set_out("（已清空）")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        meta = ttk.Label(bar, text="", foreground="#374151")
        meta.pack(side="left", padx=12)

        ttk.Label(win, text="输出").pack(anchor="w", padx=10, pady=(4, 2))
        out_box = scrolledtext.ScrolledText(win, height=16, font=("Consolas", 9),
                                            background="#0f172a", foreground="#e2e8f0")
        out_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        out_box.insert("end", "（尚未执行）")
        out_box.config(state="disabled")

        # AI 对话模式需要的状态
        ai_chat = ai_assistant.AIChat(model=ai_model_var.get())
        ai_free_var = tk.BooleanVar(value=False)
        ai_busy = [False]          # 用可变对象闭包

        def set_out(text):
            out_box.config(state="normal")
            out_box.delete("1.0", "end")
            out_box.insert("end", text)
            out_box.see("end")
            out_box.config(state="disabled")

        def append_out(text, tag=None):
            out_box.config(state="normal")
            if out_box.get("1.0", "end").strip() in ("（尚未执行）", "（已清空）"):
                out_box.delete("1.0", "end")
            if tag:
                out_box.insert("end", text.rstrip() + "\n", tag)
            else:
                out_box.insert("end", text.rstrip() + "\n")
            out_box.see("end")
            out_box.config(state="disabled")

        def remove_thinking_line():
            out_box.config(state="normal")
            text = out_box.get("1.0", "end")
            lines = text.split("\n")
            for i in range(len(lines) - 1, -1, -1):
                if "正在思考，请稍候" in lines[i]:
                    start = "%d.0" % (i + 1)
                    end = "%d.end+1c" % (i + 1)
                    out_box.delete(start, end)
                    break
            out_box.config(state="disabled")

        def mode_label():
            m = mode_var.get()
            return {"cmd": "系统命令", "python": "Python 代码",
                    "ai": "AI 对话", "ollama": "ollama 单次"}.get(m, m)

        def ai_toggle_free_mode():
            on = not ai_free_var.get()
            ai_free_var.set(on)
            append_out("🤖 %s" % ("已进入自由对话模式，提示词限制已关闭。"
                                   if on else "已退出自由对话模式，恢复考场助手提示词。"))

        def do_run(event=None):
            code = code_box.get("1.0", "end").rstrip("\n")
            if not code.strip():
                messagebox.showwarning("提示", "请输入要执行的命令、代码或 AI 消息" if is_full
                                       else "请输入 `ollama run <模型> [提示词]`")
                return "break"
            try:
                to = int(timeout_var.get())
            except (TypeError, ValueError):
                to = cmd_console.DEFAULT_TIMEOUT
            # 模式由层级决定，不信任界面控件：管理员恒为 ollama
            mode = mode_var.get() if is_full else "ollama"

            # ---------- AI 对话模式：走 ai_assistant 多轮聊天 ----------
            if mode == "ai":
                text = code.strip()
                if text == "EDY_POWERQ":
                    ai_toggle_free_mode()
                    code_box.delete("1.0", "end")
                    return "break"
                if ai_busy[0]:
                    append_out("⚠ 上一条 AI 请求还未完成，请稍候再试。")
                    return "break"
                nonlocal ai_chat
                model_want = ai_model_var.get().strip() or ai_assistant.DEFAULT_MODEL
                if ai_chat.model != model_want:
                    ai_chat = ai_assistant.AIChat(model=model_want)
                ai_busy[0] = True
                run_btn.config(state="disabled")
                meta.config(text="AI 思考中…（%s）" % ("自由对话" if ai_free_var.get() else "考场助手"))
                append_out("🧑 你：%s" % text)
                append_out("🤖 AI 正在思考，请稍候…")
                code_box.delete("1.0", "end")

                def ai_worker():
                    try:
                        ok, reply, transport = ai_chat.ask(
                            text, operator=self.user["username"], tier=tier,
                            source="gui-cmd", timeout=to,
                            free_mode=bool(ai_free_var.get()))
                    except Exception as e:
                        ok, reply, transport = False, "AI 调用异常：%s" % e, ""

                    def ai_apply():
                        try:
                            if not win.winfo_exists():
                                return
                        except tk.TclError:
                            return
                        ai_busy[0] = False
                        remove_thinking_line()
                        tag = "ai_ok" if ok else "ai_err"
                        line = "%s：%s" % ("🤖 AI" if ok else "⚠ AI 调用失败", reply)
                        append_out(line, tag=tag)
                        # 同步到主界面结果区
                        self._append_to_main_output(line)
                        meta.config(text=("✅ 已回复 ｜ %s" % transport if ok
                                          else "❌ 失败"))
                        run_btn.config(state="normal")
                    try:
                        win.after(0, ai_apply)
                    except tk.TclError:
                        pass
                threading.Thread(target=ai_worker, daemon=True).start()
                return "break"

            # ---------- 命令 / Python / ollama 单次 ----------
            run_btn.config(state="disabled")
            meta.config(text="执行中…")
            set_out("（执行中，请稍候…）")

            def worker():
                # 放后台线程：命令最长可跑 600 秒，直接在 UI 线程执行会把界面卡死
                try:
                    res = cmd_console.execute(code, operator=self.user["username"],
                                              role=role, mode=mode,
                                              source="gui", timeout=to)
                except Exception as e:                      # 兜底，绝不让线程静默死掉
                    res = {"ok": False, "exit_code": None, "stdout": "",
                           "stderr": "执行异常：%s" % e, "duration_ms": 0,
                           "truncated": False, "timed_out": False}

                def apply():
                    # 窗口可能已被用户关掉，必须先判断再操作控件
                    try:
                        if not win.winfo_exists():
                            return
                    except tk.TclError:
                        return
                    if res.get("timed_out"):
                        flag = "⏱ 超时已终止进程树"
                    elif res.get("exit_code") == 0:
                        flag = "✅ 成功"
                    else:
                        flag = "❌ 失败"
                    meta.config(text="%s ｜ 退出码 %s ｜ 耗时 %sms%s"
                                % (flag, res.get("exit_code"), res.get("duration_ms"),
                                   " ｜ 输出已截断" if res.get("truncated") else ""))
                    body = res.get("stdout") or ""
                    if res.get("stderr"):
                        body += "\n--- stderr ---\n" + res["stderr"]
                    set_out(body or "（无输出）")
                    run_btn.config(state="normal")

                try:
                    win.after(0, apply)          # 回到 UI 线程更新控件
                except tk.TclError:
                    pass

            threading.Thread(target=worker, daemon=True).start()
            return "break"

        run_btn.config(command=do_run)
        code_box.bind("<Control-Return>", do_run)
        code_box.focus_set()

        # 标签颜色
        out_box.tag_config("ai_ok", foreground="#34d399")
        out_box.tag_config("ai_err", foreground="#fca5a5")

    # ---------------- 内置命令控制台（与视频并排，不弹窗） ----------------
    def _build_console_panel(self, box):
        """
        构建"视频右侧"的内置命令控制台（内容先建好；是否显示由 _toggle_console_panel 决定）。

        层级判定全部走 accounts.cmd_console_tier()：所有者 full、管理员 ollama、其它 none。
        界面只负责**显示**：真正拦得住的是 cmd_console.execute(role=...)，
        所以这里把类型选择框禁用掉只是体验，绕过它也不会多拿到权限。
        """
        import cmd_console
        import ai_assistant

        role = (self.user or {}).get("role")
        is_full = accounts.cmd_console_tier(role) == accounts.CMD_TIER_FULL

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Label(bar, text="类型：").pack(side="left")
        self.console_mode = tk.StringVar(
            value=CONSOLE_MODE_LABELS["cmd" if is_full else "ollama"])
        self.console_mode_box = ttk.Combobox(
            bar, textvariable=self.console_mode, width=16,
            values=((CONSOLE_MODE_LABELS["cmd"], CONSOLE_MODE_LABELS["python"],
                     CONSOLE_MODE_LABELS["ai"]) if is_full
                    else (CONSOLE_MODE_LABELS["ollama"],)),
            state=("readonly" if is_full else "disabled"))
        self.console_mode_box.pack(side="left")
        self.console_mode_box.bind("<<ComboboxSelected>>", self._console_mode_changed)

        ttk.Label(bar, text="超时").pack(side="left", padx=(6, 2))
        self.console_timeout = tk.StringVar(value=str(cmd_console.DEFAULT_TIMEOUT))
        ttk.Spinbox(bar, from_=1, to=cmd_console.MAX_TIMEOUT, width=5,
                    textvariable=self.console_timeout).pack(side="left")

        # ---- AI 对话工具行：模型选择（可手输）+ 刷新 + AI 巡视开关 ----
        # 「AI 巡视」即用户要求的"关闭对话功能后 AI 自动分析我在干什么"：
        # 勾选且不在 AI 对话模式时，界面状态一变化就自动请大模型分析。
        ai_bar = ttk.Frame(box)
        ai_bar.pack(fill="x", pady=(4, 0))
        ttk.Label(ai_bar, text="模型：").pack(side="left")
        self.ai_model_var = tk.StringVar(value=ai_assistant.DEFAULT_MODEL)
        self.ai_model_box = ttk.Combobox(
            ai_bar, textvariable=self.ai_model_var, width=30,
            values=(ai_assistant.DEFAULT_MODEL,))
        self.ai_model_box.pack(side="left")
        ttk.Button(ai_bar, text="🔄", width=3,
                   command=self._ai_refresh_models).pack(side="left", padx=(2, 8))
        self.ai_watch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ai_bar, text="AI 巡视（不在对话时自动分析我在干什么）",
                        variable=self.ai_watch_var).pack(side="left")

        # 自由对话模式：不显示 UI 开关，只能通过输入 EDY_POWERQ 触发
        self.ai_free_var = tk.BooleanVar(value=False)

        # AI 会话状态（懒创建：第一次对话时才实例化）
        self.ai_chat = None
        self._ai_busy = False                 # 有请求在飞时巡视让路，避免并发压本地模型
        self._ai_last_snapshot = ""           # 上次巡视的快照（没变就不重复分析）

        # 提示语必须**随层级变化**（见 _sync_console_controls）：
        # 面板是登录时一次性建好的，若那一刻总开关还没开，这里会先写成 ollama 提示；
        # 所有者随后开启时必须同步改写，否则他会以为自己也只能用 ollama（真实反馈过的问题）。
        self.console_hint = ttk.Label(box, foreground="#888", wraplength=320, justify="left",
                                      text=self._console_hint_text(is_full))
        self.console_hint.pack(anchor="w", pady=(6, 3))

        # width 给控制台一个**最小宽度**：panel 用 pack(expand) 吃剩余空间，
        # 若内部控件不声明宽度，视频一宽就会把控制台挤成一条缝（实测只有 157px）。
        self.console_input = scrolledtext.ScrolledText(box, height=3, width=34,
                                                      font=("Consolas", 9))
        self.console_input.pack(fill="x")
        self.console_input.bind("<Control-Return>", self._console_hotkey)

        runbar = ttk.Frame(box)
        runbar.pack(fill="x", pady=(4, 2))
        self.console_run_btn = ttk.Button(runbar, text="▶ 执行", command=self._console_run)
        self.console_run_btn.pack(side="left")
        # 输出很长时还可以弹出大窗口看（复用原有弹窗实现）
        ttk.Button(runbar, text="↗ 弹出", width=7,
                   command=self._show_cmd_console).pack(side="left", padx=4)
        self.console_meta = ttk.Label(runbar, text="", foreground="#374151")
        self.console_meta.pack(side="left", padx=6)

        self.console_out = scrolledtext.ScrolledText(
            box, height=11, width=34, font=("Consolas", 9), wrap="word",
            background="#0f172a", foreground="#e2e8f0")
        self.console_out.pack(fill="both", expand=True, pady=(2, 0))
        # 对话气泡配色：你说的话 / AI 回答 / AI 巡视 / 错误 一眼可辨
        self.console_out.tag_config("ai_user", foreground="#7dd3fc")    # 浅蓝：你的输入
        self.console_out.tag_config("ai_bot", foreground="#86efac")     # 浅绿：AI 回答
        self.console_out.tag_config("ai_watch", foreground="#fcd34d")   # 浅黄：AI 巡视
        self.console_out.tag_config("ai_err", foreground="#fca5a5")     # 浅红：失败
        self.console_out.tag_config("ai_dim", foreground="#64748b")     # 灰：分隔线/提示
        self.console_out.insert("end", "（尚未执行）")
        self.console_out.config(state="disabled")

        # 后台：刷新模型列表 + 启动 AI 巡视轮询（仅所有者层级会真正触发分析）
        self.root.after(600, self._ai_refresh_models)
        self.root.after(self.AI_WATCH_INTERVAL_MS, self._ai_watch_tick)

    def _console_hotkey(self, event=None):
        self._console_run()
        return "break"

    def _toggle_console_panel(self):
        """显示 / 隐藏视频右侧的内置控制台（不弹窗）"""
        panel = getattr(self, "console_panel", None)
        if not self._widget_alive(panel):
            return
        role = (self.user or {}).get("role")
        if panel.winfo_manager():                 # 已显示 -> 收起
            panel.pack_forget()
            if self._widget_alive(getattr(self, "cmd_nav_btn", None)):
                self.cmd_nav_btn.config(text="⌨ 命令控制台")
            return
        if not accounts.can_cmd_console(role):
            messagebox.showerror(
                "权限不足",
                "命令控制台当前不可用。\n\n"
                "· 所有者：请先在「更多操作 → 操作记录 → 所有者设置」勾选「命令控制台」\n"
                "· 管理员：需所有者开启总开关，并授权「AI 辅助判断」")
            return
        panel.pack(side="left", fill="both", expand=True, padx=(10, 0), anchor="n")
        if self._widget_alive(getattr(self, "cmd_nav_btn", None)):
            self.cmd_nav_btn.config(text="⌨ 收起控制台")

    def _console_set_out(self, text):
        out = getattr(self, "console_out", None)
        if not self._widget_alive(out):
            return
        out.config(state="normal")
        out.delete("1.0", "end")
        out.insert("end", text)
        out.see("end")
        out.config(state="disabled")

    def _console_run(self):
        """
        在内置控制台里执行。执行放后台线程——命令最长可跑 600 秒，
        直接在 UI 线程跑会把整个界面卡死（连视频都会停）。
        """
        import cmd_console

        role = (self.user or {}).get("role")
        tier = accounts.cmd_console_tier(role)
        if tier == accounts.CMD_TIER_NONE:
            messagebox.showerror("权限不足", "命令控制台当前不可用。")
            return
        code = self.console_input.get("1.0", "end").rstrip("\n")
        if not code.strip():
            messagebox.showwarning("提示", "请输入要执行的命令或代码" if tier == accounts.CMD_TIER_FULL
                                   else "请输入 `ollama run <模型> [提示词]`")
            return
        try:
            to = int(self.console_timeout.get())
        except (TypeError, ValueError):
            to = cmd_console.DEFAULT_TIMEOUT
        # 下拉框显示的是中文标签，这里映射回 cmd / python / ai；管理员恒为 ollama
        # （模式由层级决定，不信任界面控件）
        mode = (CONSOLE_MODE_KEYS.get(self.console_mode.get(), "cmd")
                if tier == accounts.CMD_TIER_FULL else "ollama")

        # ---- AI 对话入口 ----
        # 1) 任何模式下输入裸的 `ollama run <模型>`（不带提示词）：
        #    原行为是拉起交互式 REPL，输出一堆 ANSI 控制序列（用户反馈的乱码来源）。
        #    现在按用户预期处理：切换/选中该模型并进入 AI 对话。
        # 2) 类型选「🤖 AI 对话」：输入内容即对话消息（自动附带当前界面快照）。
        ok_cmd, argv, _msg = cmd_console.parse_ollama_command(code)
        if tier == accounts.CMD_TIER_FULL and ok_cmd and len(argv) == 3:
            self._ai_start_chat(argv[2])
            return
        if mode == "ai":
            self._ai_chat_send(code, tier)
            return
        # 中文自然语言 → 识别为"和 AI 说话"，而不是系统命令。
        # 真实反馈：在 cmd 模式输入「你好」得到"不是内部或外部命令"，体验断裂；
        # 用户的心智模型是"打字 = 问 AI，命令 = 执行"。系统命令几乎不含 CJK 字符，
        # 含则判为对话，同时自动把类型切到「🤖 AI 对话」让后续输入一路聊下去。
        if (tier == accounts.CMD_TIER_FULL
                and any("一" <= ch <= "鿿" for ch in code)):
            self.console_mode.set(CONSOLE_MODE_LABELS["ai"])
            self._console_mode_changed()
            self._ai_chat_send(code, tier)
            return

        self.console_run_btn.config(state="disabled")
        self.console_meta.config(text="执行中…")
        self._console_set_out("（执行中，请稍候…）")
        self.action_log.set_operator(self.user["username"])

        def worker():
            try:
                res = cmd_console.execute(code, operator=self.user["username"],
                                          role=role, mode=mode, source="gui", timeout=to)
            except Exception as e:                  # 兜底，绝不让线程静默死掉
                res = {"ok": False, "exit_code": None, "stdout": "",
                       "stderr": "执行异常：%s" % e, "duration_ms": 0,
                       "truncated": False, "timed_out": False}

            def apply():
                try:
                    if not self.root.winfo_exists():
                        return
                except tk.TclError:
                    return
                if res.get("timed_out"):
                    flag = "⏱ 超时已终止进程树"
                elif res.get("exit_code") == 0:
                    flag = "✅ 成功"
                else:
                    flag = "❌ 失败"
                self.console_meta.config(
                    text="%s ｜ 退出码 %s ｜ %sms%s"
                         % (flag, res.get("exit_code"), res.get("duration_ms"),
                            " ｜ 已截断" if res.get("truncated") else ""))
                body = res.get("stdout") or ""
                if res.get("stderr"):
                    body += "\n--- stderr ---\n" + res["stderr"]
                self._console_set_out(body or "（无输出）")
                self.console_run_btn.config(state="normal")

            # 主循环若已退出，after 会抛 RuntimeError（普通 TclError 捕获不到），一并兜住
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- AI 对话 / AI 巡视（ollama，仅所有者层级） ----------------
    AI_WATCH_INTERVAL_MS = 30000        # AI 巡视轮询间隔（毫秒）：30 秒，降低 CPU 占用

    def _console_mode_changed(self, event=None):
        """类型切换时同步提示语（选中 AI 对话时换成对话说明）"""
        hint = getattr(self, "console_hint", None)
        if not self._widget_alive(hint):
            return
        try:
            if self.console_mode.get() == CONSOLE_MODE_LABELS["ai"]:
                hint.config(text=self.AI_HINT)
            else:
                is_full = (accounts.cmd_console_tier((self.user or {}).get("role"))
                           == accounts.CMD_TIER_FULL)
                hint.config(text=self._console_hint_text(is_full))
        except tk.TclError:
            pass

    def _ai_append_out(self, text, tag=None):
        """向控制台输出区**追加**一段内容（对话与巡视都走这里，不覆盖历史）"""
        out = getattr(self, "console_out", None)
        if not self._widget_alive(out):
            return
        try:
            out.config(state="normal")
            if out.get("1.0", "end").strip() in ("（尚未执行）", "（执行中，请稍候…）"):
                out.delete("1.0", "end")
            # 不要传空 tuple 作 tag：部分 tkinter 版本/主题会把它当成无名标签处理异常
            if tag:
                out.insert("end", text.rstrip() + "\n", tag)
            else:
                out.insert("end", text.rstrip() + "\n")
            out.insert("end", "─" * 38 + "\n", "ai_dim")
            out.see("end")
            out.config(state="disabled")
        except Exception as e:
            # 兜底：控制台写失败不能吞掉 AI 结果，至少记到操作日志
            try:
                self.action_log.log("AI 输出写入失败", "%s | %s" % (e, text[:200]))
            except Exception:
                pass

    def _ai_remove_thinking_line(self):
        """删除控制台输出区里最后一条“AI 正在思考，请稍候…”的临时提示"""
        out = getattr(self, "console_out", None)
        if not self._widget_alive(out):
            return
        try:
            out.config(state="normal")
            # 从后往前找包含“正在思考，请稍候”的行并删除
            text = out.get("1.0", "end")
            lines = text.split("\n")
            for i in range(len(lines) - 1, -1, -1):
                if "正在思考，请稍候" in lines[i]:
                    start = "%d.0" % (i + 1)
                    end = "%d.end+1c" % (i + 1)
                    out.delete(start, end)
                    break
            out.config(state="disabled")
        except Exception:
            pass

    def _append_to_main_output(self, text):
        """把 AI 回复也写到主「核验结果」区，确保控制台收起时用户也能看到"""
        try:
            box = getattr(self, "output", None)
            if self._widget_alive(box):
                box.insert("end", text + "\n")
                box.see("end")
        except Exception:
            pass

    def _open_ai_chat(self):
        """「🤖 AI 助手」入口：展开控制台 → 切到 AI 对话模式 → 聚焦输入框"""
        role = (self.user or {}).get("role")
        if accounts.cmd_console_tier(role) != accounts.CMD_TIER_FULL:
            messagebox.showerror(
                "权限不足",
                "AI 助手当前不可用。\n\n"
                "请先在「更多操作 → 操作记录 → 所有者设置」勾选「命令控制台」总开关。")
            return
        panel = getattr(self, "console_panel", None)
        if self._widget_alive(panel) and not panel.winfo_manager():
            self._toggle_console_panel()          # 面板收着则展开（按钮文案同步交给 toggle）
        self.console_mode.set(CONSOLE_MODE_LABELS["ai"])
        self._console_mode_changed()
        if self.ai_chat is None:
            self._ai_start_chat(self.ai_model_var.get())
        try:
            self.console_input.focus_set()
        except tk.TclError:
            pass

    def _ai_start_chat(self, model):
        """输入 `ollama run <模型>` 后进入 AI 对话：选定模型、切换类型、新建会话"""
        import ai_assistant
        model = (model or "").strip() or ai_assistant.DEFAULT_MODEL
        self.ai_model_var.set(model)
        if (accounts.cmd_console_tier((self.user or {}).get("role"))
                == accounts.CMD_TIER_FULL):
            self.console_mode.set(CONSOLE_MODE_LABELS["ai"])
        self._console_mode_changed()
        self.ai_chat = ai_assistant.AIChat(model=model)
        self.action_log.log("AI对话开始", "模型=%s" % model)
        self._ai_append_out(
            "🤖 已加载模型 %s，进入 AI 对话。\n"
            "直接输入问题即可——我能看到你当前的界面状态（考生、视频、核验结果等），\n"
            "可以问我「我现在在干什么」「刚才为什么被拦截」「下一步该做什么」。" % model,
            tag="ai_bot")

    def _ai_chat_send(self, text, tier):
        """发送一条对话消息（后台线程调 ollama，不卡界面）"""
        import ai_assistant
        model = (self.ai_model_var.get() or "").strip() or ai_assistant.DEFAULT_MODEL
        if self.ai_chat is None or self.ai_chat.model != model:
            self.ai_chat = ai_assistant.AIChat(model=model)
        try:
            to = max(int(self.console_timeout.get()), 120)
        except (TypeError, ValueError):
            to = 180
        if self._ai_busy:
            self._ai_append_out("⚠ 上一条 AI 请求还未完成，请稍候再试。", tag="ai_err")
            return
        # EDY_POWERQ：第一次开自由对话，第二次关（切换）
        if text.strip() == "EDY_POWERQ":
            self._ai_toggle_free_mode()
            return
        free_mode = bool(self.ai_free_var.get())
        snapshot = "" if free_mode else self._ai_snapshot()
        operator = (self.user or {}).get("username", "")
        self._ai_busy = True
        self._ai_busy_since = time.time()
        self._ensure_ai_guard()
        self.console_run_btn.config(state="disabled")
        mode_hint = "自由对话" if free_mode else "考场助手"
        self.console_meta.config(text="AI 思考中…（%s｜%s）" % (mode_hint, model))
        self._ai_append_out("🧑 你：%s" % text, tag="ai_user")
        # 立即给出“正在思考”反馈，避免用户以为卡死（ollama 本地 4B 可能需要几秒到几十秒）
        self._ai_append_out("🤖 AI 正在思考，请稍候…", tag="ai_dim")
        try:                                # 发出去就清空输入框，像正常聊天软件一样
            self.console_input.delete("1.0", "end")
        except tk.TclError:
            pass
        chat = self.ai_chat

        def work():
            try:
                ok, reply, transport = chat.ask(
                    text, snapshot=snapshot, operator=operator,
                    tier=tier, source="gui", timeout=to, free_mode=free_mode)
            except Exception as e:
                ok, reply, transport = False, "AI 调用异常：%s" % e, ""

            def apply():
                try:
                    if not self.root.winfo_exists():
                        return
                except tk.TclError:
                    return
                self._ai_busy = False
                # 把之前的“正在思考…”提示删掉，换成正式回复（删除失败也不影响后续输出）
                try:
                    self._ai_remove_thinking_line()
                except Exception:
                    pass
                tag = "🤖 AI" if ok else "⚠ AI 调用失败"
                line = "%s：%s" % (tag, reply)
                self._ai_append_out(line, tag=("ai_bot" if ok else "ai_err"))
                self._append_to_main_output(line)           # 同步到主结果区，确保看得见
                self.console_meta.config(
                    text=("✅ 已回复" if ok else "❌ 失败") + " ｜ 模型 " + model)
                self.console_run_btn.config(state="normal")
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _ai_toggle_free_mode(self):
        """输入 EDY_POWERQ 切换自由对话模式：开/关"""
        on = not bool(self.ai_free_var.get())
        self.ai_free_var.set(on)
        self._ai_append_out(
            "🤖 %s" % ("已进入自由对话模式，提示词限制已关闭。现在可以正常聊天了。"
                       if on else "已退出自由对话模式，恢复考场助手提示词。"),
            tag="ai_bot")

    def _ensure_ai_guard(self):
        """启动 AI 忙状态守护（首次调用时），防止 ollama 卡死后 UI 永远锁死"""
        if self._ai_guard_sched:
            return
        self._ai_guard_sched = True
        self._ai_guard_tick()

    def _ai_guard_tick(self):
        """
        每 10 秒检查一次：如果 _ai_busy 已持续超过最大预期时间 + 15 秒缓冲，
        就认为 ollama 请求已经卡死/丢失回调，强制恢复 UI。
        """
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if self._ai_busy:
            try:
                limit = max(120, int(self.console_timeout.get() or 120)) + 15
            except (TypeError, ValueError):
                limit = 135
            elapsed = time.time() - self._ai_busy_since
            if elapsed > limit:
                self._ai_busy = False
                try:
                    self.console_run_btn.config(state="normal")
                    self.console_meta.config(text="")
                except tk.TclError:
                    pass
                self._ai_append_out("⚠ AI 请求超时或卡死，已强制恢复，可重新发送。",
                                    tag="ai_err")
                self.action_log.log("AI 守护", "强制重置忙状态（%.0fs 无响应）" % elapsed)
        try:
            self.root.after(10000, self._ai_guard_tick)
        except (tk.TclError, RuntimeError):
            pass

    def _ai_snapshot(self):
        """
        界面状态快照 —— "AI 可以调取当前我在干什么"的数据来源。
        只取界面状态与结果摘要（结果区本身已按隐私策略脱敏），不含证件明文。
        """
        lines = ["时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
        u = self.user or {}
        try:
            role_text = accounts.role_text(u.get("role")) if u.get("role") else "未登录"
        except Exception:
            role_text = u.get("role") or "未登录"
        lines.append("操作员：%s（%s）" % (u.get("username", "-"), role_text))

        def _get(var, default="-"):
            try:
                return var.get().strip() or default
            except Exception:
                return default

        lines.append("设备编号：%s" % _get(self.device_var))
        cid = _get(self.candidate_var, "未选择")
        cname = ""
        if cid and cid != "未选择" and cid in self.candidates:
            cname = (self.candidates[cid].get("name") or "").strip()
        lines.append("当前考生：%s%s" % (cid, "（姓名：%s）" % cname if cname else ""))
        lines.append("核验模式：%s" % ("1:1 入场核验" if _get(self.mode_var) == "1:1"
                                       else "1:N 考场抽查"))
        for label, name in ((self.consent_label, "同意状态"),
                            (self.doc_label, "证件核对")):
            try:
                lines.append("%s：%s" % (name, label.cget("text")))
            except Exception:
                pass
        vs = self.video_stream
        lines.append("实时视频：%s" % ("开启中（FPS=%.1f）" % vs.fps
                                      if vs is not None and vs.running else "未开启"))
        lines.append("现场图像：%s" % (self.live_desc or "未采集"))
        try:
            lines.append("自动核验：%s" % ("开启" if self.auto_verify.get() else "关闭"))
        except Exception:
            pass
        try:                            # 结果区尾部：最近一次核验的结果与原因
            tail = self.output.get("end-8l", "end-1l").strip()
            if tail:
                lines.append("最近的界面输出（尾部）：\n" + tail)
        except Exception:
            pass
        return "\n".join(lines)

    def _ai_refresh_models(self):
        """后台刷新模型下拉框（`ollama list` 可能耗时，放线程里）"""
        import ai_assistant

        def work():
            try:
                names = ai_assistant.list_models()
            except Exception:
                return

            def apply():
                box_w = getattr(self, "ai_model_box", None)
                if not self._widget_alive(box_w):
                    return
                try:
                    cur = self.ai_model_var.get().strip()
                    vals = tuple(names)
                    if cur and cur not in names:
                        vals = (cur,) + vals
                    box_w.config(values=vals)
                    if not cur and names:
                        self.ai_model_var.set(names[0])
                except tk.TclError:
                    pass
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _ai_refresh_models_popup(self, box):
        """给命令控制台弹窗里的模型下拉框刷新模型列表"""
        import ai_assistant

        def work():
            try:
                names = ai_assistant.list_models()
            except Exception:
                return

            def apply():
                if not self._widget_alive(box):
                    return
                try:
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

    def _ai_watch_tick(self):
        """AI 巡视调度：固定间隔检查一次界面状态，有变化才请 AI 分析"""
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            self._ai_watch_maybe()
        except Exception:
            pass                            # 巡视是辅助功能，任何异常都不能影响主界面
        try:
            self.root.after(self.AI_WATCH_INTERVAL_MS, self._ai_watch_tick)
        except (tk.TclError, RuntimeError):
            pass

    def _ai_watch_maybe(self):
        """
        用户要求："如果关闭了 AI 对话功能，AI 自动分析我在干什么"。
        触发条件（同时满足）：
          · 勾选了「AI 巡视」；
          · 当前**不在** AI 对话模式（对话时用户自己在问，巡视不打扰）；
          · 没有请求在飞；            · 界面状态相比上次有变化。
        面板收不收起都巡视——分析结果会同步写进主结果输出框，不依赖控制台可见。
        """
        if not self.ai_watch_var.get() or self._ai_busy:
            return
        role = (self.user or {}).get("role")
        if not accounts.can_cmd_console(role):
            return
        tier = accounts.cmd_console_tier(role)
        if tier != accounts.CMD_TIER_FULL:
            return                          # 巡视沿用对话的层级限制：仅所有者
        if self.console_mode.get() == CONSOLE_MODE_LABELS["ai"]:
            return                          # 正在 AI 对话模式 = 功能开启中，不自动分析
        snapshot = self._ai_snapshot()
        if snapshot == self._ai_last_snapshot:
            return
        self._ai_last_snapshot = snapshot

        import ai_assistant
        model = (self.ai_model_var.get() or "").strip() or ai_assistant.DEFAULT_MODEL
        operator = (self.user or {}).get("username", "")
        if self._ai_busy:
            return                                  # 聊天或上一轮巡视还没完，先让路
        self._ai_busy = True
        self._ai_busy_since = time.time()
        self._ensure_ai_guard()
        self.console_meta.config(text="🔎 AI 巡视分析中…")
        self._append("🔎 AI 巡视：检测到界面状态变化，正在分析…")

        def work():
            try:
                ok, text, _t = ai_assistant.analyze_activity(
                    snapshot, model=model, operator=operator, tier=tier,
                    source="gui", timeout=ai_assistant.WATCH_TIMEOUT)
            except Exception as e:
                ok, text = False, "AI 巡视异常：%s" % e

            def apply():
                try:
                    if not self.root.winfo_exists():
                        return
                except tk.TclError:
                    return
                self._ai_busy = False
                if ok:
                    line = "🔎 AI 巡视（自动分析当前操作）：\n%s" % text
                    self._ai_append_out(line, tag="ai_watch")
                else:
                    line = "⚠ AI 巡视失败：%s" % text
                    self._ai_append_out(line, tag="ai_err")
                # 同步写到主结果输出框：巡视是"不盯着控制台也能看到"的提醒
                for l in line.splitlines():
                    self._append_to_main_output(l)
                self.console_meta.config(text="")
            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _build_index(self, only_selected=False):
        targets = self.candidates
        if only_selected:
            cid = self.candidate_var.get()
            if cid in self.candidates:
                targets = {cid: self.candidates[cid]}
        if not targets:
            messagebox.showwarning("提示", "没有可建档的考生。")
            return

        def work():
            stats = self.index.build(targets, progress=lambda cid, d, t: self.root.after(
                0, self.status.config, {"text": "建档 %d/%d：%s" % (d, t, cid)}))
            try:
                path = self.index.save()
                saved = "已保存到 %s" % os.path.basename(path)
            except Exception as e:
                saved = "保存失败：%s" % e
            self.root.after(0, self._after_build, stats, saved)

        self.status.config(text="正在建档…（首次加载模型约需数秒）")
        self.action_log.log("开始建档", "目标 %d 人" % len(targets))
        threading.Thread(target=work, daemon=True).start()

    def _after_build(self, stats, saved):
        self.status.config(text="建档完成：新增 %d，跳过 %d，失败 %d；%s"
                                % (stats["built"], stats["skipped"], len(stats["failed"]), saved))
        self.action_log.log("建档完成", "新增 %d 失败 %d" % (stats["built"], len(stats["failed"])))
        for cid, why in stats["failed"].items():
            self._append("  ⚠️ 建档失败 %s：%s" % (cid, why))

    def _register_consent(self):
        cid = self.candidate_var.get()
        if not cid:
            messagebox.showwarning("提示", "请先选择考生。")
            return
        if not messagebox.askyesno("登记单独同意",
                                   "确认已取得考生 %s 的人脸信息处理单独同意？\n"
                                   "（须独立于考试规则、单独签署）" % cid):
            return
        op = self.operator_var.get() or "operator"
        ok = self.store.set_consent(cid, "已同意", operator=op)
        self.candidates = self.store.load()
        self._refresh_consent()
        self.action_log.log("登记单独同意", "考生=%s 结果=%s" % (cid, ok))
        self._append("[合规] 考生 %s 单独同意登记：%s" % (cid, "成功" if ok else "失败"))

    # ---------------- 实时视频 ----------------
    def _toggle_video(self):
        if self.video_stream is not None and self.video_stream.running:
            self.video_stream.stop()
            self.video_stream = None
            self.video_btn.config(text="开启实时视频")
            self.fps_label.config(text="FPS: 0.0")
            self.video_label.config(image="", text="视频已关闭", background="#111", foreground="#aaa")
            self.video_status.config(text="视频已关闭", foreground="#666")
            self.action_log.log("关闭实时视频", "")
            return

        import video as video_mod
        try:
            idx = int(self.camera_var.get().strip() or "0")
        except ValueError:
            idx = 0
        self.video_status.config(text="正在打开摄像头…", foreground="#666")
        self.root.update_idletasks()
        stream = video_mod.VideoStream(camera_index=idx)
        ok = stream.start()
        self.video_stream = stream
        if not ok:
            self.video_status.config(text=stream.error or "摄像头打开失败", foreground="#b00")
            self.video_label.config(image="", text="无法获取视频\n%s" % (stream.error or ""),
                                    background="#111", foreground="#b00")
            self.action_log.log("开启实时视频失败", stream.error)
            self.video_stream = None
            return
        self.video_btn.config(text="关闭实时视频")
        self.action_log.log("开启实时视频", "摄像头编号=%d" % idx)
        self._update_video()

    def _frame_to_photo(self, frame):
        """BGR -> tkinter 可显示的图像对象（优先 Pillow，缺失则用 PNG+base64 回退）。
        预览统一降采样到宽 ≤480，既省界面线程开销，也让实时预览更跟手。"""
        import cv2
        h, w = frame.shape[:2]
        # 只用 INTER_AREA 做大幅下采样（>2 倍），小幅缩放改用更快的 INTER_LINEAR
        if w > 480:
            scale = 480.0 / w
            new_w, new_h = 480, max(1, int(h * scale))
            interp = cv2.INTER_AREA if scale < 0.5 else cv2.INTER_LINEAR
            frame = cv2.resize(frame, (new_w, new_h), interpolation=interp)
        try:
            from PIL import Image, ImageTk
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return ImageTk.PhotoImage(image=Image.fromarray(rgb))
        except Exception:
            import base64
            ok, buf = cv2.imencode(".png", frame)
            if not ok:
                return None
            return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))

    def _update_video(self):
        """界面线程定时取最新帧刷新（不阻塞采集线程）"""
        s = self.video_stream
        if s is None or not s.running:
            if s is not None and s.error:
                self.video_status.config(text=s.error, foreground="#b00")
            return
        frame, seq = s.latest()
        if frame is not None and seq != self._video_seq:
            self._video_seq = seq
            try:
                photo = self._frame_to_photo(frame)
                if photo is not None:
                    self._photo = photo                # 持有引用，避免被回收
                    self.video_label.config(image=photo, text="", width=0, height=0)
            except Exception as e:
                self.video_status.config(text="画面刷新失败：%s" % str(e)[:60], foreground="#b00")
        self.fps_label.config(text="FPS: %.1f" % s.fps)
        self.video_status.config(text=s.status_text(),
                                 foreground="#0a7d28" if not s.error else "#b00")
        # 30 FPS（33ms）让预览更顺滑；界面转换已做降采样，开销可控
        self.root.after(33, self._update_video)

    def _use_live_frame(self):
        """便利化：没开视频就自动开；抓当前帧后立即核验（真实相机帧 + 现场令牌）"""
        s = self.video_stream
        if s is None or not s.running:
            self._toggle_video()                 # 自动开启实时视频
            s = self.video_stream
            if s is None or not s.running:
                messagebox.showwarning("提示", "无法打开摄像头：%s"
                                       % (s.error if s else "未知错误"))
                return
            time.sleep(0.6)                      # 等自动曝光稳定
        frame, _seq = s.latest()
        if frame is None:
            messagebox.showwarning("提示", "尚未取到画面，请稍候再试。")
            return
        self.live_image = frame
        self.live_token = liveness.make_token(self.device_var.get().strip() or "GATE-A-01",
                                              source="camera")
        self.live_desc = "实时视频取帧（%s）" % time.strftime("%H:%M:%S")
        self.video_status.config(text="已取当前画面，开始核验… ｜ " + self.live_desc,
                                 foreground="#0a7d28")
        self.action_log.log("取实时画面", "采集FPS=%.1f" % s.fps)
        self._start_verify()          # 抓帧后立即核验，点一下就有结果

    # ---------------- 自动核验（仅所有者面板可开启） ----------------
    AUTO_INTERVAL_MS = 2500           # 自动核验间隔（毫秒）

    def _toggle_auto_verify(self):
        on = self.auto_verify.get()
        self.action_log.log("自动核验开关", "开启" if on else "关闭")
        if on:
            messagebox.showinfo("自动核验已开启",
                                "实时视频开启后，系统将每隔 %.1f 秒自动抓帧核验并输出结果。\n"
                                "1:1 模式需先在主界面选定考生；关闭本开关即停止。"
                                % (self.AUTO_INTERVAL_MS / 1000.0))
            self._start_auto_verify()
        else:
            self.status.config(text="自动核验已关闭")

    def _start_auto_verify(self):
        if self._auto_sched:
            return
        self._auto_sched = True
        self.root.after(self.AUTO_INTERVAL_MS, self._schedule_auto_verify)

    def _schedule_auto_verify(self):
        if not self.auto_verify.get():        # 被关闭 -> 停止调度
            self._auto_sched = False
            return
        self._try_auto_verify()
        self.root.after(self.AUTO_INTERVAL_MS, self._schedule_auto_verify)

    def _try_auto_verify(self):
        """主线程调度：取最新帧，交后台线程核验，避免并发叠加"""
        if self._verifying:
            return
        s = self.video_stream
        if s is None or not s.running:
            self.status.config(text="自动核验已开启，但实时视频未开启")
            return
        frame, _seq = s.latest()
        if frame is None:
            return
        cid = self.candidate_var.get()
        mode = self.mode_var.get()
        witness = self.witness_var.get().strip()
        device = self.device_var.get().strip() or "GATE-A-01"
        if mode == "1:1" and not cid:
            self.status.config(text="自动核验：1:1 模式需先选定考生")
            return
        self._verifying = True
        self.status.config(text="自动核验中…")
        token = liveness.make_token(device, source="camera")
        # 身份要素在主线程取值（Tk 不是线程安全的）；自动核验同样遵守大考/常规模式
        elements = (self.elem_id_var.get().strip(),
                    self.elem_ticket_var.get().strip(),
                    self.elem_seat_var.get().strip())

        def work():
            try:
                auto = self.auto_verify.get()          # 自动核验模式：人脸匹配即自动通过
                if mode == "1:1":
                    rec = self.verifier.verify_face_first(cid, live_images=[frame],
                                                          capture_token=token, witness=witness,
                                                          auto_confirm=auto,
                                                          id_card=elements[0],
                                                          ticket_no=elements[1],
                                                          seat_no=elements[2])
                else:
                    rec = self.verifier.verify_1toN(frame, capture_token=token, witness=witness,
                                                    auto_confirm=auto)
                self.root.after(0, self._finish_auto_verify, rec)
            except Exception as e:
                self.root.after(0, self._finish_auto_verify_error, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _finish_auto_verify(self, rec):
        self._verifying = False
        self._show_result(rec.to_dict())
        self.status.config(text="自动核验完成，等待下一次" if self.auto_verify.get()
                           else "已获取现场图像")

    def _finish_auto_verify_error(self, msg):
        self._verifying = False
        self._show_error(msg)

    # ---------------- 现场图像 ----------------
    def _capture(self):
        # 单帧抓拍与实时视频共用摄像头：先释放视频，避免"摄像头被占用"
        if self.video_stream is not None:
            try:
                self.video_stream.stop()
            except Exception:
                pass
            self.video_stream = None
            self.video_btn.config(text="开启实时视频")
            self.fps_label.config(text="FPS: 0.0")
        try:
            idx = int(self.camera_var.get().strip() or "0")
        except ValueError:
            idx = 0
        def work():
            try:
                frame, token, path = capture.capture_best(
                    camera_index=idx, device_id=self.device_var.get())
                self.root.after(0, self._after_capture, frame, token, path, None)
            except Exception as e:
                self.root.after(0, self._after_capture, None, None, None, str(e))
        self.status.config(text="单帧抓拍中…请面向摄像头（多帧取最清晰）")
        threading.Thread(target=work, daemon=True).start()

    def _after_capture(self, frame, token, path, err):
        if err:
            self.status.config(text="抓拍失败：%s" % err)
            self.action_log.log("抓拍失败", err)
            messagebox.showerror("抓拍失败", "%s\n\n请确认摄像头未被占用，且设备编号正确。" % err)
            return
        self.live_image = frame
        self.live_token = token
        self.live_desc = "摄像头实拍（%d 帧取最清晰，清晰度 %.1f）" % (
            token.get("frame_count", 0), token.get("sharpness", 0))
        if path:
            self.live_desc += " ｜ 留档 %s" % os.path.basename(path)
        if token.get("save_error"):
            self.live_desc += " ｜ 留档失败（不影响核验）"
        self.video_status.config(text=self.live_desc, foreground="#0a7d28")
        self.status.config(text="已获取现场图像（有效期内需完成核验）")
        # 直接把抓拍的照片显示在视频区域
        try:
            photo = self._frame_to_photo(frame)
            if photo is not None:
                self._photo = photo                       # 持有引用，防止被回收
                self.video_label.config(image=photo, text="", width=0, height=0)
        except Exception:
            pass
        self.fps_label.config(text="已抓拍", foreground="#0a7d28")
        self.action_log.log("摄像头抓拍", self.live_desc)

    def _choose_photo(self):
        """选择本地图片核验：**仅严格模式关闭时可用**；严格模式下给出明确提示而非静默失败"""
        if not accounts.strict_mode_relaxed():
            self.action_log.log("选择图片被拒", "严格模式开启，非现场来源不允许核验")
            messagebox.showwarning(
                "严格模式已开启",
                "当前处于**严格模式**，非现场实时抓拍（选择本地图片）不允许用于核验，\n"
                "以避免拿照片翻拍顶替现场人脸。\n\n"
                "如确需用本地图片核验（例如离线演示、事后复核），请由：\n"
                "「更多操作 → 操作记录 → 所有者设置」关闭「严格模式（关闭后可选本地图片核验）」。\n\n"
                "关闭后本功能立即可用，且每次核验都会在审计里标注“严格模式已关闭、非现场来源”。")
            self.status.config(text="严格模式开启：「选择图片」已禁用（可由所有者在所有者设置中关闭严格模式）")
            return
        path = filedialog.askopenfilename(
            title="选择现场照片（非现场采集；严格模式下会被拒绝）",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp"), ("所有文件", "*.*")])
        if not path:
            return
        from face_engine import FaceError, read_image
        try:
            img = read_image(path)          # 走 imdecode，规避中文路径问题
        except FaceError as e:
            messagebox.showerror("错误", "无法读取该图片：%s" % e.msg)
            return
        self.live_image = img
        self.live_token = liveness.make_token(self.device_var.get().strip() or "GATE-A-01",
                                              source="file")   # 文件来源：显式标记为非现场
        self.live_desc = "文件选择：%s（非现场来源，严格模式已关闭，将留痕）" % os.path.basename(path)
        self.video_status.config(text=self.live_desc, foreground="#c07000")
        self.action_log.log("选择图片", self.live_desc)

    # ---------------- 核验 ----------------
    def _start_verify(self):
        if self.live_image is None:
            messagebox.showwarning("提示", "请先抓拍或选择现场图像。")
            return
        cid = self.candidate_var.get()
        if self.mode_var.get() == "1:1" and not cid:
            messagebox.showwarning("提示", "请选择考生编号。")
            return

        self.verifier.device_id = self.device_var.get().strip() or "GATE-A-01"
        self.verifier.operator = self.operator_var.get().strip()
        self._enable_doc_check(None, False)          # 新一轮核验先清空上一轮的证件核对
        self.verify_btn.config(state="disabled")
        self.status.config(text="核验中…（首次需加载模型，约 5~15 秒，请稍候）")
        self._append("=" * 64)
        self._append("[核验开始] 模式=%s 考生=%s 设备=%s 操作人=%s"
                     % (self.mode_var.get(), cid, self.verifier.device_id, self.verifier.operator))
        self.action_log.log("开始核验", "考生=%s 模式=%s 图像=%s"
                            % (cid, self.mode_var.get(), self.live_desc))
        # 在主线程读 Tk 变量并传入，避免后台线程访问 Tk 对象（Tk 不是线程安全的）
        mode = self.mode_var.get()
        witness = self.witness_var.get().strip()
        # 身份要素（大考模式=四要素 / 常规模式=三要素）：在主线程取值；1:N 抽查不涉及
        elements = (self.elem_id_var.get().strip(),
                    self.elem_ticket_var.get().strip(),
                    self.elem_seat_var.get().strip())
        if mode == "1:1":
            self._append("[身份要素] %s ｜ 本次输入：身份证号=%s 准考证号=%s 座位号=%s"
                         % (accounts.identity_mode_text(),
                            elements[0] or "（空）", elements[1] or "（空）", elements[2] or "（空）"))
        threading.Thread(target=self._run_verify, args=(cid, mode, witness, elements),
                         daemon=True).start()

    def _run_verify(self, cid, mode, witness, elements):
        try:
            if mode == "1:1":
                # 扫脸核验：身份要素按当前模式校验（大考模式必须身份证；常规模式以准考证号为准），
                # 证件再由工作人员对照实物目视核对（confirm_document 留痕）
                rec = self.verifier.verify_face_first(
                    cid, live_images=[self.live_image],
                    capture_token=self.live_token, witness=witness,
                    id_card=elements[0], ticket_no=elements[1], seat_no=elements[2])
            else:
                rec = self.verifier.verify_1toN(
                    self.live_image, capture_token=self.live_token, witness=witness)
            self.root.after(0, self._show_result, rec.to_dict())
        except Exception as e:
            self.root.after(0, self._show_error, str(e))

    def _show_result(self, rec):
        self.verify_btn.config(state="normal")
        self._append("-" * 64)
        self._append("结果：%s（%s）" % (rec["result"], rec.get("reason_code", "")))
        self._append("原因：%s" % rec["reason"])
        self._append("考生：%s    相似度：%s    阈值：%s"
                     % (rec["candidate_id"], rec.get("score"), rec.get("threshold")))
        # 异常状态诊断：画质 / 姿态 / 是否增强 / 灰区 / 撞脸
        self._append("画质：%s（%s）｜ 原图问题：%s"
                     % (rec.get("quality_issues") or "正常", rec.get("quality_severity"),
                        rec.get("quality_issues_raw") or "无"))
        self._append("亮度：原图 %s → 判定用 %s%s"
                     % (rec.get("brightness_raw"), rec.get("brightness"),
                        "（已自动增强）" if rec.get("enhanced") else ""))
        self._append("姿态：yaw=%s° pitch=%s° roll=%s°（pitch 负值为仰拍）"
                     % (rec.get("yaw"), rec.get("pitch"), rec.get("roll")))
        if rec.get("gray_zone"):
            self._append("→ 相似度落在灰区（模糊匹配区间）")
        if rec.get("twin_candidate_id"):
            self._append("→ 与考生 %s 相似度差距 %s（撞脸/孪生风险）"
                         % (rec.get("twin_candidate_id"), rec.get("twin_gap")))
        if rec.get("edge_provider"):
            self._append("边缘：后端 %s ｜ 档位 %s%s ｜ 耗时 %s ms"
                         % (rec.get("edge_provider"), rec.get("edge_profile"),
                            "（已降档）" if rec.get("edge_degraded") else "",
                            rec.get("edge_latency_ms")))
        self._append("活体/现场性：%s（抓拍于 %s 秒前）    同意状态：%s"
                     % (rec.get("liveness"), rec.get("capture_age_sec"), rec.get("consent_status")))
        # 身份要素模式与严格模式留痕（放宽必须可见，不静默）
        if rec.get("id_mode"):
            self._append("身份要素：%s ｜ %s"
                         % (rec.get("elements_checked") or "-",
                            "大考模式：身份证必校验" if rec.get("id_mode") == "major"
                            else "常规模式：身份证不参与校验"))
        if rec.get("strict_mode_off"):
            self._append("⚠ %s：本次为非现场来源（source=%s），已如实留痕"
                         % (rec.get("strict_mode_off_reason") or "严格模式未生效",
                            rec.get("non_live_source") or "-"))
        self._append("设备：%s    操作人：%s    见证人：%s"
                     % (rec["device_id"], rec.get("operator"), rec.get("witness") or "-"))
        self._append("时间：%s    重试：%d" % (rec["timestamp"], rec.get("retry_count", 0)))
        self._append("-" * 64)
        self.output.insert("end", "→ %s  %s\n" % (rec["result"], REASON_TEXT.get(rec.get("reason_code"), "")),
                           rec["result"])
        self.output.see("end")
        # 人脸通过 -> 自动带出该考生证件信息，进入人工证件核对环节
        if rec.get("result") == "PASS" and rec.get("candidate_id"):
            self._enable_doc_check(rec["candidate_id"], True)
            self._append("→ 已自动带出证件信息，请对照证件实物核对后点「证件一致」或「证件不符」")
        else:
            self._enable_doc_check(None, False)
        self.action_log.log("核验完成", "考生=%s 结果=%s 原因码=%s 相似度=%s"
                            % (rec["candidate_id"], rec["result"], rec.get("reason_code"), rec.get("score")))

        # 需要人工复验的情形：当场提示走复验流程（双人见证）
        if rec.get("required_witness") and not (rec.get("witness") or "").strip():
            messagebox.showwarning(
                "需人工复验",
                "%s\n\n本情形不可由算法单独放行，请按复验流程处理：\n"
                "1) 核对身份证件与准考证原件；\n"
                "2) 由两名工作人员（含见证人）共同确认；\n"
                "3) 在「见证人」栏填写姓名并重新核验留存记录。"
                % REASON_TEXT.get(rec.get("reason_code"), rec.get("reason_code")))

    def _show_error(self, msg):
        self.verify_btn.config(state="normal")
        self.status.config(text="核验失败：%s" % str(msg)[:80])
        self._append("[错误] %s" % msg)
        self.action_log.log("核验异常", msg)

    # ---------------- 模型预热 ----------------
    def _warmup_model(self):
        """
        后台预加载人脸模型，避免第一次核验时的长时间无响应。

        注意 after() 的传参方式：`after(0, self.status.config, "文本")` 会把文本当成
        **位置参数**，tkinter 继而把它解释成选项名，于是抛
        `TclError: unknown option "-文本"`，状态栏永远停在"初始化中…"。
        必须传 kwargs 字典（与本文件其它几处 after 保持一致）。
        """
        def work():
            try:
                import face_engine
                self.root.after(0, self.status.config, {"text": "正在预加载人脸模型…"})
                face_engine.get_app()                # 加载检测+识别模型（一次性，之后复用）
                self.root.after(0, self.status.config, {"text": "人脸模型已就绪，可开始核验"})
                self.action_log.log("模型预热", "完成")
            except Exception as e:
                self.root.after(0, self.status.config,
                                {"text": "模型预热失败：%s" % str(e)[:60]})
                self.action_log.log("模型预热失败", str(e)[:120])
        threading.Thread(target=work, daemon=True).start()

    # ---------------- 日志 ----------------
    def _show_audit(self):
        cid = self.candidate_var.get().strip() or None
        records = self.audit.query(cid, limit=500)
        self.action_log.log("查看审计日志", "考生=%s 共%d条" % (cid or "全部", len(records)))
        self._popup("审计日志 - %s" % (cid or "全部"),
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in records) or "暂无记录。")

    def _check_chain(self):
        r = self.audit.verify_chain()
        self.action_log.log("校验审计链", r["reason"])
        messagebox.showinfo("审计链校验",
                            "%s\n\n记录数：%d\n断点行号：%s" % (r["reason"], r["total"], r["broken_at"]))

    def _show_action_log(self):
        records = self.action_log.query(limit=300)
        self.action_log.log("查看操作日志", "共%d条" % len(records))
        self._popup("操作日志", "\n".join(records) or "暂无记录。")

    def _popup(self, title, text):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("820x520")
        box = scrolledtext.ScrolledText(win, wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("end", text)

    # ---------------- 工具 ----------------
    def _append(self, msg):
        self.output.insert("end", msg + "\n")
        self.output.see("end")

    def _clear_result(self):
        self.output.delete("1.0", "end")
        self.action_log.log("清空结果")

    def _on_close(self):
        try:
            if self.video_stream is not None:
                self.video_stream.stop()          # 退出时释放摄像头
        except Exception:
            pass
        try:
            self.action_log.log("GUI关闭", "操作人=%s" % ((self.user or {}).get("username","-")))
        except Exception:
            pass
        self.root.destroy()

    def _on_close_legacy(self):
        self.action_log.log("GUI关闭", "用户关闭窗口")
        self.root.destroy()


if __name__ == "__main__":
    import sys
    import traceback

    def _excepthook(tp, val, tb):
        # 未捕获异常 -> 写崩溃日志 + 弹窗，便于定位"无法正常运行"的具体原因
        msg = "".join(traceback.format_exception(tp, val, tb))
        try:
            p = os.path.join(config.DATA_ROOT, "logs", "gui_crash.log")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n%s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass
        try:
            from tkinter import messagebox
            messagebox.showerror("程序异常", msg[-1500:])
        except Exception:
            pass
    sys.excepthook = _excepthook

    # 不再 os.chdir：config 内所有路径都是绝对路径；
    # 全局切换工作目录会影响同进程内的其他逻辑，属于隐患。
    root = tk.Tk()
    try:
        app = FaceVerifyGUI(root)
        root.mainloop()
    except Exception:
        import traceback
        from tkinter import messagebox
        messagebox.showerror("启动失败", traceback.format_exc()[-1500:])
