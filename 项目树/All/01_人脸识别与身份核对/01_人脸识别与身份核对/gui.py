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

# pythonw 隐藏窗口启动时 stdout/stderr 为 None，兜住避免 print 崩溃
config.ensure_stdio("gui_console.log")


class FaceVerifyGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("智能考场 · 人脸识别与身份核对")
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

        # 视频画面
        self.video_label = tk.Label(live, text="视频未开启\n（点「开启实时视频」后此处显示实时画面与 FPS）",
                                    background="#111", foreground="#aaa", width=80, height=14)
        self.video_label.grid(row=1, column=0, columnspan=7, pady=8)
        self.video_status = ttk.Label(live, text="视频未开启", foreground="#666")
        self.video_status.grid(row=2, column=0, columnspan=7, sticky="w")

        # 操作按钮：并入视频块；只留常用项，其余收进「更多操作」
        ttk.Separator(live, orient="horizontal").grid(row=3, column=0, columnspan=7,
                                                      sticky="ew", pady=(10, 6))
        act = ttk.Frame(live)
        act.grid(row=4, column=0, columnspan=7, sticky="w")
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
        ttk.Entry(elem, textvariable=self.id_card_var, width=24, state="readonly"
                  ).grid(row=4, column=1, sticky="w", padx=4)
        ttk.Label(elem, text="准考证：").grid(row=4, column=2, sticky="w", padx=(12, 0))
        self.ticket_var = tk.StringVar()
        ttk.Entry(elem, textvariable=self.ticket_var, width=16, state="readonly"
                  ).grid(row=4, column=3, sticky="w", padx=4)
        ttk.Label(elem, text="座位：").grid(row=4, column=4, sticky="w", padx=(12, 0))
        self.seat_var = tk.StringVar()
        ttk.Entry(elem, textvariable=self.seat_var, width=10, state="readonly"
                  ).grid(row=4, column=5, sticky="w", padx=4)
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
        self.action_log.log("自动补齐身份信息",
                            "考生=%s 准考证=%s 座位=%s" % (c.candidate_id, c.ticket_no, c.seat_no))

    def _enable_doc_check(self, candidate_id, enabled):
        """人脸通过后，把该考生的证件信息（掩码）显示出来供目视核对"""
        c = self.candidates.get(candidate_id)
        if enabled and c is not None:
            self._auto_fill_from(candidate_id)          # 自动补齐并跳出
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
        if w > 480:
            frame = cv2.resize(frame, (480, max(1, int(h * 480.0 / w))),
                               interpolation=cv2.INTER_AREA)
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
        self.root.after(55, self._update_video)        # ≈18Hz，肉眼流畅且更省界面线程

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
        """后台预加载人脸模型，避免第一次核验时的长时间无响应"""
        def work():
            try:
                import face_engine
                self.root.after(0, self.status.config, "正在预加载人脸模型…")
                face_engine.get_app()                # 加载检测+识别模型（一次性，之后复用）
                self.root.after(0, self.status.config, "人脸模型已就绪，可开始核验")
                self.action_log.log("模型预热", "完成")
            except Exception as e:
                self.root.after(0, self.status.config, "模型预热失败：%s" % str(e)[:60])
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
