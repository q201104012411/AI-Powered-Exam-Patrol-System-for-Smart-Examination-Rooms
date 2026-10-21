# -*- coding: utf-8 -*-
r"""
智能考场 AI 巡考系统 · 总控台（目录树 + 账户登录）

界面：
  启动时统一登录一次（账户 + 口令），之后进入模块不再重复输入；
  左侧「系统结构目录」按 业务 / 安全 / AI / 集成 分组列出全部子系统；
  选中模块看说明，双击（或回车 / 「进入模块」）用自带 runtime 直接启动。

账户：
  默认值与 03 智能终端一致（owner/EDY_POWERQ、admin/admin、patrol1/patrol1、
  inv01/inv01、check1/check1），存 总控台账户.json，PBKDF2 加盐哈希。
  owner 可改口令、增删账户、调整"可进入模块"范围。

诚实说明：
  · 这是**入口门禁**，不是文件系统权限；拿到解压目录的人仍可直接运行模块脚本。
  · 登录一次后，总控台给模块签发 HMAC 令牌（sso.py）：01/02/03/05/08 自动建会话，
    不再要求第二遍口令；直接双击模块自己的 .bat/.py 时仍走原登录页。
  · 总控台退出不会关闭已启动的模块进程。
"""
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import launcher_auth as auth
import sso

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_pkg():
    """定位包根（模块清单.json 所在目录），兼容源码运行与单文件解压后运行。"""
    cands = []
    try:
        cands.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    cands.append(HERE)
    cands.append(os.getcwd())
    for start in cands:
        cur = start
        for _ in range(6):
            if os.path.isfile(os.path.join(cur, "模块清单.json")):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return HERE


PKG = _find_pkg()
MAN_PATH = os.path.join(PKG, "模块清单.json")
ACC_PATH = os.path.join(PKG, "总控台账户.json")
AUDIT_PATH = os.path.join(PKG, "总控台审计.jsonl")

try:
    with open(MAN_PATH, "r", encoding="utf-8") as _f:
        MAN = json.load(_f)
except Exception as _e:
    MAN = {"title": "智能考场 AI 巡考系统", "groups": [], "modules": {}}
    _MAN_ERROR = str(_e)
else:
    _MAN_ERROR = ""


def _runtime_paths():
    """自带运行时的 python.exe / pythonw.exe（兼容 runtime 根目录与 venv 布局）。

    源码树（项目树\\All）里没有 runtime\\ 时，退回当前解释器，
    这样每个模块/总控台都能单独用系统 Python 跑，不依赖 exe 打包目录。
    """
    pyw = os.path.join(PKG, "runtime", "pythonw.exe")
    py = os.path.join(PKG, "runtime", "python.exe")
    if not os.path.isfile(pyw):
        pyw = os.path.join(PKG, "runtime", "Scripts", "pythonw.exe")
        py = os.path.join(PKG, "runtime", "Scripts", "python.exe")
    if not (os.path.isfile(py) and os.path.isfile(pyw)):
        exe = sys.executable or ""
        d = os.path.dirname(exe)
        cand_py = os.path.join(d, "python.exe")
        cand_pyw = os.path.join(d, "pythonw.exe")
        if os.path.isfile(cand_py):
            py = cand_py
        if os.path.isfile(cand_pyw):
            pyw = cand_pyw
        elif os.path.isfile(cand_py):
            pyw = cand_py
    return py, pyw


PY, PYW = _runtime_paths()

# ---------------------------------------------------------------- 配色
BG = "#070d1a"
PANEL = "#0d1a2e"
PANEL2 = "#122542"
BORDER = "#1b3457"
FG = "#e8f1ff"
DIM = "#8ba3c7"
MUTE = "#5b739a"
ACCENT = "#22d3ee"
OK = "#34d399"
WARN = "#fbbf24"
ALARM = "#f43f5e"
FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_TITLE = ("Microsoft YaHei", 17, "bold")
FONT_H1 = ("Microsoft YaHei", 13, "bold")
MODULE_ORDER = ["01", "02", "03", "10", "12",
                "05", "06", "07", "08", "09", "13",
                "11", "00", "04"]


class LoginDialog(object):
    """账户 + 口令弹窗；成功时 result = {"rec":..., "pwd":...}"""

    def __init__(self, app, module_code=None, title=None, require_owner=False,
                 subtitle=None):
        self.app = app
        self.code = module_code
        self.require_owner = require_owner
        self.subtitle = subtitle
        self.result = None
        self.win = tk.Toplevel(app.root)
        self.win.title(title or "登录")
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.transient(app.root)
        self.win.grab_set()
        w, h = 500, 380
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                           max(0, (sh - h) // 2 - 40)))

        meta = MAN.get("modules", {}).get(module_code or "", {})
        top = tk.Frame(self.win, bg=PANEL, height=86)
        top.pack(fill="x")
        top.pack_propagate(False)
        heading = ("🔐  进入模块" if module_code
                   else ("🔐  统一登录" if subtitle else "🔐  账户验证"))
        tk.Label(top, text=heading,
                 bg=PANEL, fg=ACCENT, font=FONT_H1, anchor="w",
                 padx=18).pack(anchor="w", pady=(16, 2))
        sub = (subtitle or meta.get("name") or module_code or "总控台账户管理")
        tk.Label(top, text=sub, bg=PANEL, fg=FG, font=FONT,
                 anchor="w", padx=18).pack(anchor="w")

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True, padx=22, pady=12)

        tk.Label(body, text="账户", bg=BG, fg=DIM, anchor="w",
                 font=FONT_SMALL).pack(anchor="w")
        self.user_var = tk.StringVar(value=app.last_user or "owner")
        self.user_entry = ttk.Entry(body, textvariable=self.user_var, width=42)
        self.user_entry.pack(fill="x", pady=(2, 8))

        tk.Label(body, text="口令", bg=BG, fg=DIM, anchor="w",
                 font=FONT_SMALL).pack(anchor="w")
        self.pwd_var = tk.StringVar()
        self.pwd_entry = ttk.Entry(body, textvariable=self.pwd_var,
                                   width=42, show="●")
        self.pwd_entry.pack(fill="x", pady=(2, 4))

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="显示口令", variable=self.show_var,
                        command=self._toggle_show).pack(side="left")
        self.err = tk.Label(row, text="", bg=BG, fg=ALARM, font=FONT_SMALL,
                            anchor="e")
        self.err.pack(side="right")

        if meta.get("roles"):
            tk.Label(body, text="本模块允许角色：%s" % "、".join(
                auth.role_text(r) for r in meta["roles"]),
                bg=BG, fg=MUTE, font=FONT_SMALL, anchor="w").pack(
                fill="x", pady=(8, 0))
        if self.require_owner:
            tk.Label(body, text="账户管理仅允许「所有者」账户。",
                     bg=BG, fg=WARN, font=FONT_SMALL, anchor="w").pack(
                fill="x", pady=(6, 0))

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="取 消", command=self.close).pack(side="right")
        ttk.Button(btns, text="进 入", style="Accent.TButton",
                   command=self.submit).pack(side="right", padx=(0, 8))

        self.win.bind("<Return>", lambda _e: self.submit())
        self.win.bind("<Escape>", lambda _e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.after(80, self._focus)

    def _focus(self):
        try:
            if self.user_var.get().strip():
                self.pwd_entry.focus_set()
            else:
                self.user_entry.focus_set()
        except tk.TclError:
            pass

    def _toggle_show(self):
        try:
            self.pwd_entry.config(show="" if self.show_var.get() else "●")
        except tk.TclError:
            pass

    def submit(self):
        user = self.user_var.get().strip()
        pwd = self.pwd_var.get()
        if not user:
            self.err.config(text="请输入账户")
            return
        rec, why = self.app.accounts.authenticate(user, pwd)
        if rec is None:
            self.err.config(text=why or "账户或口令不正确")
            self.app.audit("login", user,
                           self.code or ("总控台登录" if self.subtitle else "-"),
                           False, why)
            try:
                self.win.bell()
            except Exception:
                pass
            self.pwd_var.set("")
            self.pwd_entry.focus_set()
            return
        if self.require_owner and rec.get("role") != "owner":
            self.err.config(text="需要所有者账户")
            self.app.audit("login", user, "账户管理", False, "非所有者")
            return
        if self.code and not self.app.accounts.can_access(rec, self.code):
            self.err.config(text="该账户无权进入本模块（可进入：%s）"
                                 % self.app.accounts.modules_text(rec))
            self.app.audit("enter", user, self.code, False, "无模块权限")
            return
        self.result = {"rec": rec, "pwd": pwd}
        self.app.last_user = user
        self.app.audit("login", user,
                       self.code or ("总控台登录" if self.subtitle else "账户管理"),
                       True, "")
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def show(self):
        self.win.wait_window()
        return self.result


class App(object):
    def __init__(self, root):
        self.root = root
        self.accounts = auth.AccountStore(ACC_PATH)
        self.last_user = "owner"
        self.session = None                  # 一次登录，后续进入模块直接复用
        self._procs = {}
        self._current = None
        self._missing = set()
        self._build_style()
        self._build_window()
        self._set_window_icon()
        self._build_header()
        self._build_body()
        self._build_footer()
        self._populate_tree()
        self._check_env()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 启动即要求统一登录；登录一次后进入任何模块都不再弹口令框。
        root.after(180, self._startup_login)

    # ---------------------------------------------------------------- 外观
    def _build_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG, font=FONT)
        st.configure("Dim.TLabel", background=BG, foreground=DIM, font=FONT_SMALL)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("PanelDim.TLabel", background=PANEL, foreground=DIM,
                     font=FONT_SMALL)
        st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                     padding=(10, 6), font=FONT)
        st.map("TButton", background=[("active", BORDER)])
        st.configure("Accent.TButton", background="#1d4ed8", foreground="#ffffff",
                     borderwidth=0, padding=(12, 7), font=FONT_B)
        st.map("Accent.TButton", background=[("active", ACCENT),
                                             ("disabled", PANEL2)])
        st.configure("Mini.TButton", background=PANEL2, foreground=DIM,
                     borderwidth=0, padding=(8, 4), font=FONT_SMALL)
        st.map("Mini.TButton", background=[("active", BORDER)])
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                     insertcolor=FG, bordercolor=BORDER)
        st.configure("TCheckbutton", background=BG, foreground=DIM, font=FONT_SMALL)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=FG, rowheight=30, borderwidth=0, font=FONT)
        st.map("Treeview", background=[("selected", "#1d4ed8")],
               foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=PANEL2, foreground=ACCENT,
                     borderwidth=0, font=FONT_B)
        st.configure("TScrollbar", background=PANEL2, troughcolor=BG,
                     bordercolor=BORDER, arrowcolor=DIM)
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=6, gripcount=0,
                     background=BORDER)

    def _build_window(self):
        self.root.title("智能考场 AI 巡考系统 · 总控台")
        self.root.geometry("1220x780")
        self.root.minsize(1040, 660)
        self.root.configure(bg=BG)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1260, max(1040, int(sw * 0.8))), min(840, max(660, int(sh * 0.8)))
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                            max(0, (sh - h) // 2 - 20)))

    def _set_window_icon(self):
        """窗口/任务栏图标用 icon\\icon.png（exe 文件图标由启动器资源嵌入）。"""
        try:
            p = os.path.join(PKG, "icon", "icon.png")
            if os.path.isfile(p):
                img = tk.PhotoImage(file=p)
                self.root.iconphoto(True, img)
                self.root.iconname("智能考场 AI 巡考系统")
                self._icon_photo = img          # 保住引用，避免被 GC
        except Exception:
            pass

    def _build_header(self):
        head = tk.Frame(self.root, bg=PANEL, height=92)
        head.pack(fill="x")
        head.pack_propagate(False)
        left = tk.Frame(head, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=18)
        tk.Label(left, text="智能考场 · AI 巡考系统", bg=PANEL, fg=FG,
                 font=FONT_TITLE, anchor="w").pack(anchor="w", pady=(16, 0))
        tk.Label(left, text="总控台 · 登录一次，所有模块直接进入（不再重复输入口令）",
                 bg=PANEL, fg=DIM, font=FONT_SMALL, anchor="w").pack(anchor="w")
        right = tk.Frame(head, bg=PANEL)
        right.pack(side="right", padx=14)
        self.user_badge = tk.Label(right, text="👤 未登录", bg=PANEL, fg=DIM,
                                   font=FONT_SMALL)
        self.user_badge.pack(side="left", padx=(0, 8))
        ttk.Button(right, text="🔁 切换账号", style="Mini.TButton",
                   command=self._switch_account).pack(side="left", padx=4)
        ttk.Button(right, text="👥 账户管理", style="Mini.TButton",
                   command=self._account_admin).pack(side="left", padx=4)
        ttk.Button(right, text="📜 审计日志", style="Mini.TButton",
                   command=self._open_audit).pack(side="left", padx=4)
        ttk.Button(right, text="❓ 使用说明", style="Mini.TButton",
                   command=self._show_help).pack(side="left", padx=4)
        ttk.Button(right, text="退出", style="Mini.TButton",
                   command=self._on_close).pack(side="left", padx=(10, 0))

    def _build_body(self):
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=14, pady=(10, 6))

        # 左：结构目录
        left = tk.Frame(body, bg=PANEL, highlightthickness=1,
                        highlightbackground=BORDER)
        body.add(left, weight=3)
        lh = tk.Frame(left, bg=PANEL)
        lh.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(lh, text="系统结构目录", bg=PANEL, fg=ACCENT,
                 font=FONT_H1, anchor="w").pack(side="left")
        tk.Label(lh, text="共 %d 个模块" % len(MAN.get("modules", {})),
                 bg=PANEL, fg=DIM, font=FONT_SMALL).pack(side="right")
        wrap = tk.Frame(left, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tree = ttk.Treeview(wrap, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("group", font=("Microsoft YaHei", 10, "bold"),
                                foreground=ACCENT)
        self.tree.tag_configure("module", foreground=FG)
        self.tree.tag_configure("disabled", foreground=MUTE)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self._try_enter())
        self.tree.bind("<Return>", lambda _e: self._try_enter())
        tk.Label(left, text="单击查看说明 · 双击 / 回车 / 「进入模块」弹出登录",
                 bg=PANEL, fg=MUTE, font=FONT_SMALL, anchor="w").pack(
            fill="x", padx=12, pady=(0, 10))

        # 右：模块详情
        right = tk.Frame(body, bg=PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        body.add(right, weight=4)
        self.d_code = tk.Label(right, text="—", bg=PANEL, fg=ACCENT,
                               font=("Consolas", 24, "bold"), anchor="w")
        self.d_code.pack(fill="x", padx=18, pady=(16, 0))
        self.d_title = tk.Label(right, text="请选择一个模块", bg=PANEL, fg=FG,
                                font=("Microsoft YaHei", 15, "bold"),
                                anchor="w", justify="left", wraplength=640)
        self.d_title.pack(fill="x", padx=18)
        self.d_desc = tk.Label(right, text="", bg=PANEL, fg=DIM, font=FONT,
                               anchor="w", justify="left", wraplength=640)
        self.d_desc.pack(fill="x", padx=18, pady=(8, 10))

        meta = tk.Frame(right, bg=PANEL)
        meta.pack(fill="x", padx=18)
        self.d_entry = tk.Label(meta, text="入口：—", bg=PANEL, fg=DIM,
                                font=FONT_SMALL, anchor="w", justify="left",
                                wraplength=640)
        self.d_entry.pack(fill="x")
        self.d_roles = tk.Label(meta, text="允许角色：—", bg=PANEL, fg=DIM,
                                font=FONT_SMALL, anchor="w")
        self.d_roles.pack(fill="x", pady=(3, 0))
        self.d_hint = tk.Label(meta, text="", bg=PANEL, fg=MUTE,
                               font=FONT_SMALL, anchor="w", justify="left",
                               wraplength=640)
        self.d_hint.pack(fill="x", pady=(6, 0))

        self.d_status = tk.Label(right, text="", bg=PANEL, fg=WARN,
                                 font=FONT_SMALL, anchor="w", justify="left",
                                 wraplength=640)
        self.d_status.pack(fill="x", padx=18, pady=(12, 0))

        btns = tk.Frame(right, bg=PANEL)
        btns.pack(fill="x", padx=18, pady=16, side="bottom")
        self.btn_enter = ttk.Button(btns, text="🚀 进入模块", style="Accent.TButton",
                                    command=self._try_enter)
        self.btn_enter.pack(side="left")
        ttk.Button(btns, text="🧾 模块说明", style="Mini.TButton",
                   command=self._module_help).pack(side="left", padx=10)

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=16, pady=(0, 8))
        self.status = tk.Label(foot, text="就绪", bg=BG, fg=DIM, font=FONT_SMALL,
                               anchor="w")
        self.status.pack(side="left")
        tk.Label(foot, text="© 2026 EDY_POWERQ", bg=BG, fg=MUTE,
                 font=FONT_SMALL).pack(side="left", padx=14)
        self.runtime_label = tk.Label(
            foot, text="运行时：%s" % (PYW if os.path.isfile(PYW) else "缺失"),
            bg=BG, fg=MUTE, font=FONT_SMALL, anchor="e")
        self.runtime_label.pack(side="right")

    # ---------------------------------------------------------------- 树
    def _populate_tree(self):
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        modules = MAN.get("modules", {})
        first_module = None
        for gi, group in enumerate(MAN.get("groups", [])):
            gid = "g%d" % gi
            self.tree.insert("", "end", iid=gid,
                             text="▾ %s" % group.get("name", ""),
                             open=True, tags=("group",))
            for code in group.get("codes", []):
                meta = modules.get(code)
                if not meta:
                    continue
                iid = "m%s" % code
                ok = (meta.get("entry") and os.path.isfile(
                    os.path.join(PKG, meta.get("dir", "").replace("/", os.sep),
                                 meta["entry"])))
                if not ok:
                    self._missing.add(code)
                label = "%s  %s%s" % (code, meta.get("short") or meta.get("name"),
                                       "" if ok else "  （缺入口）")
                self.tree.insert(gid, "end", iid=iid, text=label,
                                 tags=("module",) if ok else ("disabled",))
                if first_module is None and code == "03":
                    first_module = iid
        if first_module is None:
            first_module = "m03" if self.tree.exists("m03") else None
        if first_module:
            self.tree.selection_set(first_module)
            self.tree.focus(first_module)
            self.tree.see(first_module)
            self._show_detail("03")

    def _selected_code(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if not str(iid).startswith("m"):
            return None
        return str(iid)[1:]

    def _on_select(self, _e=None):
        code = self._selected_code()
        if code:
            self._show_detail(code)
        else:
            sel = self.tree.selection()
            if sel:
                gid = sel[0]
                # 显示分组说明
                groups = MAN.get("groups", [])
                try:
                    gi = int(str(gid)[1:])
                    g = groups[gi]
                    self.d_code.config(text="目录")
                    self.d_title.config(text=g.get("name", ""))
                    self.d_desc.config(text=g.get("desc", ""))
                    self.d_entry.config(text="包含模块：%s" % "、".join(g.get("codes", [])))
                    self.d_roles.config(text="")
                    self.d_hint.config(text="")
                    self.d_status.config(text="")
                    self.btn_enter.state(["disabled"])
                except Exception:
                    pass

    def _show_detail(self, code):
        meta = MAN.get("modules", {}).get(code, {})
        self._current = code
        self.d_code.config(text=code)
        self.d_title.config(text=meta.get("name", code))
        self.d_desc.config(text=meta.get("desc", ""))
        self.d_entry.config(text="入口：%s%s" % (
            meta.get("dir", ""), "/" + meta.get("entry", "") if meta.get("entry") else ""))
        self.d_roles.config(text="允许角色：%s" % (
            "、".join(auth.role_text(r) for r in meta.get("roles", [])) or "—"))
        self.d_hint.config(text=meta.get("hint", ""))
        entry = os.path.join(PKG, meta.get("dir", "").replace("/", os.sep),
                             meta.get("entry", "")) if meta.get("entry") else ""
        if not meta.get("entry"):
            self.d_status.config(text="⚠ 该模块暂未提供可运行入口（方案文档）。",
                                 fg=WARN)
            self.btn_enter.state(["disabled"])
        elif not os.path.isfile(entry):
            self.d_status.config(text="⚠ 找不到入口文件：%s" % entry, fg=ALARM)
            self.btn_enter.state(["disabled"])
        else:
            self.d_status.config(text="✅ 入口就绪 · 双击或点「进入模块」", fg=OK)
            self.btn_enter.state(["!disabled"])

    # ---------------------------------------------------------------- 统一登录
    def _startup_login(self):
        """启动时登录一次；之后进入模块、账户管理都复用这个会话。"""
        if self.session is not None:
            return
        dlg = LoginDialog(
            self, module_code=None,
            title="登录 · 智能考场 AI 巡考系统",
            subtitle="输入一次账户 + 口令；进入各模块不再重复输入")
        res = dlg.show()
        if not res:
            self._on_close()
            return
        self.session = res
        self._refresh_user_badge()
        rec = res["rec"]
        self.status.config(
            text="已登录：%s（%s） · 可直接双击模块进入"
                 % (rec.get("username"), auth.role_text(rec.get("role"))),
            fg=OK)

    def _refresh_user_badge(self):
        try:
            if self.session:
                rec = self.session["rec"]
                text = "👤 %s（%s）" % (rec.get("username"),
                                       auth.role_text(rec.get("role")))
                self.user_badge.config(text=text, fg=ACCENT)
            else:
                self.user_badge.config(text="👤 未登录", fg=DIM)
        except tk.TclError:
            pass

    def _switch_account(self):
        if self.session is None:
            self._startup_login()
            return
        if not messagebox.askyesno("切换账号", "退出当前账号并重新登录？"):
            return
        self.session = None
        self._refresh_user_badge()
        self.status.config(text="已退出登录", fg=DIM)
        self.root.after(80, self._startup_login)

    # ---------------------------------------------------------------- 登录 / 启动
    def _try_enter(self):
        code = self._selected_code()
        if not code:
            messagebox.showinfo("提示", "请先在左侧结构目录里选择一个模块。")
            return
        meta = MAN.get("modules", {}).get(code, {})
        if not meta.get("entry"):
            messagebox.showinfo("未就绪", "该模块暂未提供可运行入口。")
            return
        entry = os.path.join(PKG, meta.get("dir", "").replace("/", os.sep),
                             meta["entry"])
        if not os.path.isfile(entry):
            messagebox.showerror("无法进入", "找不到入口文件：\n%s" % entry)
            return
        proc = self._procs.get(code)
        if proc is not None and proc.poll() is None:
            messagebox.showinfo("已在运行",
                                "%s 已在运行（PID %s）。\n请到任务栏查看它的窗口。"
                                % (meta.get("name", code), proc.pid))
            return
        if self.session is None:
            self._startup_login()
            if self.session is None:
                return
        rec = self.session["rec"]
        if not self.accounts.can_access(rec, code):
            messagebox.showwarning(
                "无权进入",
                "当前账户 %s 无权进入「%s」。\n可进入：%s\n\n"
                "如需切换，请点右上角「切换账号」。"
                % (rec.get("username"), meta.get("name", code),
                   self.accounts.modules_text(rec)))
            return
        self._launch(code, rec, self.session.get("pwd", ""))

    def _launch(self, code, rec, pwd):
        meta = MAN.get("modules", {}).get(code, {})
        d = os.path.join(PKG, meta.get("dir", "").replace("/", os.sep))
        entry = os.path.join(d, meta.get("entry", ""))
        if not os.path.isfile(entry):
            messagebox.showerror("启动失败", "入口不存在：\n%s" % entry)
            return
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 单点登录：签名令牌 + 包根 PYTHONPATH，模块读 sso.py 后直接建会话。
        token = sso.make_token(rec.get("username", ""), rec.get("role", ""),
                               rec.get("name", ""), [code], ttl=900)
        env["EXAM_SSO_TOKEN"] = token
        env["EXAM_SSO_USER"] = rec.get("username", "")
        env["EXAM_SSO_ROLE"] = rec.get("role", "")
        env["EXAM_SSO_NAME"] = rec.get("name", "")
        # 兼容 03 既有的 EXAM_LAUNCH_* 直达方式（令牌校验失败时仍可兜底）；
        # 其他模块只拿签名令牌，不把明文口令传给子进程。
        if code == "03":
            env["EXAM_LAUNCH_USER"] = rec.get("username", "")
            env["EXAM_LAUNCH_PWD"] = pwd or ""
            env["EXAM_LAUNCH_AUTO"] = "1"
        env["PYTHONPATH"] = PKG + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            if meta.get("kind") == "console":
                if not os.path.isfile(PY):
                    raise RuntimeError("缺少 runtime\\python.exe")
                cmdline = subprocess.list2cmdline([PY, entry])
                proc = subprocess.Popen(["cmd.exe", "/k", cmdline], cwd=d,
                                        env=env,
                                        creationflags=getattr(subprocess,
                                                              "CREATE_NEW_CONSOLE", 0))
            else:
                if not os.path.isfile(PYW):
                    raise RuntimeError("缺少 runtime\\pythonw.exe")
                proc = subprocess.Popen([PYW, entry], cwd=d, env=env,
                                        creationflags=getattr(subprocess,
                                                              "CREATE_NO_WINDOW", 0))
        except Exception as e:                       # noqa: BLE001
            self.audit("enter", rec.get("username", "?"), code, False, str(e))
            messagebox.showerror("启动失败", "%s\n\n%s" % (e, d))
            return
        self._procs[code] = proc
        self.audit("enter", rec.get("username", "?"), code, True,
                   "pid=%s" % proc.pid)
        msg = "已启动 %s（账户 %s，PID %s）" % (
            meta.get("name", code), rec.get("username"), proc.pid)
        if rec.get("must_change"):
            msg += " ｜ ⚠ 该账户仍为默认口令，请到「账户管理」修改"
        self.status.config(text=msg, fg=WARN if rec.get("must_change") else OK)
        self.root.after(600, lambda: self._refresh_running_status(code))

    def _refresh_running_status(self, code):
        proc = self._procs.get(code)
        if proc is None:
            return
        if proc.poll() is None:
            self.d_status.config(text="● 正在运行（PID %s）" % proc.pid, fg=OK)
        else:
            self.d_status.config(text="○ 已退出（退出码 %s）" % proc.returncode,
                                 fg=DIM)

    # ---------------------------------------------------------------- 帮助
    def _module_help(self):
        code = self._selected_code() or self._current
        if not code:
            return
        meta = MAN.get("modules", {}).get(code, {})
        messagebox.showinfo(meta.get("name", code), "%s\n\n%s\n\n入口：%s/%s"
                            % (meta.get("desc", ""), meta.get("hint", ""),
                               meta.get("dir", ""), meta.get("entry", "")))

    def _show_help(self):
        win = tk.Toplevel(self.root)
        win.title("总控台使用说明")
        win.configure(bg=BG)
        win.transient(self.root)
        win.geometry("760x620")
        txt = tk.Text(win, bg=PANEL, fg=FG, insertbackground=FG, wrap="word",
                      relief="flat", padx=14, pady=14, font=FONT,
                      highlightthickness=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        content = """智能考场 AI 巡考系统 · 总控台

一、怎么进入模块
  1) 启动后先在「统一登录」输入一次账户 + 口令；
  2) 左侧「系统结构目录」单击模块 -> 右侧看说明；
  3) 双击模块（或回车 / 点「进入模块」）-> 直接启动，不再重复输入口令；
     模块内部登录由总控台签发的 SSO 令牌自动完成。

二、默认账户（首次登录后请到「账户管理」修改）
  owner    / EDY_POWERQ   所有者：全部模块
  admin    / admin        管理员：业务与保障模块（不含 00 集成联动）
  patrol1  / patrol1      巡考员：01/02/03/10/11/12
  inv01    / inv01        监考员：03/10
  check1   / check1       核验员：01

三、账户与权限
  · 账户存  总控台账户.json  （PBKDF2-HMAC-SHA256 加盐哈希，不存明文）
  · owner 可新增账户、重置口令、调整“可进入模块”范围；
  · 该门禁是“从总控台进入”这条路径的唯一入口；登录后进入各模块
    由 SSO 令牌自动完成，01/02/03/05/08 不再要求二次输入口令。
  · 直接双击模块自己的 .bat/.py（不经过总控台）时，模块仍保留原有登录页。

四、数据与审计
  · 登录/进入成功与失败都写  总控台审计.jsonl ；
  · 已启动的模块是独立进程，关闭总控台不会关闭它们；
  · 单文件 exe 首次运行会解压到
    %LOCALAPPDATA%\\智能考场 AI 巡考系统\\app-<hash> ，
    之后直接复用；旧版本的 app-<旧hash> 目录不会自动迁移数据。

五、如实说明
  · 这是界面入口门禁，不是文件系统级权限；能进解压目录的人仍可直接
    运行 runtime\\python.exe <模块脚本>。要防这一层需要 NTFS ACL 或
    把模块放到受控服务端。
  · 04 巡考机器人只有方案文档，未提供可运行入口。
"""
        txt.insert("1.0", content)
        txt.config(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(side="bottom",
                                                                pady=8)

    # ---------------------------------------------------------------- 账户管理
    def _account_admin(self):
        if self.session is None:
            self._startup_login()
            if self.session is None:
                return
        rec = self.session["rec"]
        if rec.get("role") != "owner":
            messagebox.showwarning(
                "需要所有者",
                "账户管理仅允许「所有者」账户。\n当前账户：%s（%s）\n\n"
                "请点右上角「切换账号」，用 owner 登录。"
                % (rec.get("username"), auth.role_text(rec.get("role"))))
            return
        self._open_account_manager(rec)

    def _open_account_manager(self, owner_rec):
        win = tk.Toplevel(self.root)
        win.title("总控台账户管理（所有者）")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.geometry("840x540")
        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="总控台账户管理", bg=PANEL, fg=ACCENT,
                 font=FONT_H1, padx=14, pady=10).pack(side="left")
        tk.Label(head, text="当前：%s（%s）" % (owner_rec.get("username"),
                                               auth.role_text(owner_rec.get("role"))),
                 bg=PANEL, fg=DIM, font=FONT_SMALL).pack(side="right", padx=14)

        cols = ("user", "role", "modules", "name", "must")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=12)
        for c, t, w in (("user", "账户", 120), ("role", "角色", 90),
                        ("modules", "可进入模块", 300), ("name", "姓名", 140),
                        ("must", "默认口令", 90)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.pack(fill="both", expand=True, padx=14, pady=10)

        def reload():
            for i in tv.get_children():
                tv.delete(i)
            for u, rec in sorted(self.accounts.list_accounts().items()):
                tv.insert("", "end", values=(
                    u, auth.role_text(rec.get("role")),
                    self.accounts.modules_text(rec),
                    rec.get("name", ""),
                    "是" if rec.get("must_change") else "否"))

        def selected_user():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("提示", "请先选择一个账户", parent=win)
                return None
            return tv.item(sel[0])["values"][0]

        def add_account():
            d = tk.Toplevel(win)
            d.title("新增账户")
            d.configure(bg=BG)
            d.transient(win)
            d.grab_set()
            d.geometry("460x360")
            f = tk.Frame(d, bg=BG)
            f.pack(fill="both", expand=True, padx=18, pady=14)
            fields = {}
            for i, (key, label, show) in enumerate((
                    ("user", "账户", False), ("name", "姓名", False),
                    ("pwd", "口令（≥6位）", True), ("pwd2", "再输一次", True))):
                tk.Label(f, text=label, bg=BG, fg=DIM, font=FONT_SMALL,
                         anchor="w").grid(row=i * 2, column=0, sticky="w",
                                          pady=(6 if i else 0, 2))
                v = tk.StringVar()
                e = ttk.Entry(f, textvariable=v, width=42, show="●" if show else "")
                e.grid(row=i * 2 + 1, column=0, sticky="w")
                fields[key] = v
            tk.Label(f, text="角色", bg=BG, fg=DIM, font=FONT_SMALL,
                     anchor="w").grid(row=8, column=0, sticky="w", pady=(8, 2))
            role_var = tk.StringVar(value="patrol")
            ttk.Combobox(f, textvariable=role_var, state="readonly", width=20,
                         values=list(auth.ROLE_TEXT.keys())).grid(
                row=9, column=0, sticky="w")
            tk.Label(f, text="可进入模块（* = 全部；或逗号分隔，如 01,02,03）",
                     bg=BG, fg=DIM, font=FONT_SMALL, anchor="w").grid(
                row=10, column=0, sticky="w", pady=(8, 2))
            mod_var = tk.StringVar(value="01,02,03,10,11,12")
            ttk.Entry(f, textvariable=mod_var, width=42).grid(
                row=11, column=0, sticky="w")

            def save():
                if fields["pwd"].get() != fields["pwd2"].get():
                    messagebox.showerror("新增失败", "两次口令不一致", parent=d)
                    return
                ok, msg = self.accounts.create(
                    fields["user"].get(), fields["pwd"].get(),
                    role_var.get(), fields["name"].get(), mod_var.get().strip() or "*")
                if not ok:
                    messagebox.showerror("新增失败", msg, parent=d)
                    return
                messagebox.showinfo("新增成功", msg, parent=win)
                d.destroy()
                reload()

            bar = tk.Frame(f, bg=BG)
            bar.grid(row=12, column=0, sticky="e", pady=(12, 0))
            ttk.Button(bar, text="保存", style="Accent.TButton",
                       command=save).pack(side="left", padx=4)
            ttk.Button(bar, text="取消", command=d.destroy).pack(side="left")

        def reset_pwd():
            u = selected_user()
            if not u:
                return
            p1 = simpledialog.askstring("重置口令", "新口令（≥6位）：",
                                        show="*", parent=win)
            if p1 is None:
                return
            p2 = simpledialog.askstring("重置口令", "再输一次：",
                                        show="*", parent=win)
            if p1 != p2:
                messagebox.showerror("重置失败", "两次口令不一致", parent=win)
                return
            ok, msg = self.accounts.reset_password(u, p1)
            (messagebox.showinfo if ok else messagebox.showerror)(
                "重置口令", msg, parent=win)
            reload()

        def edit_modules():
            u = selected_user()
            if not u:
                return
            rec = self.accounts.get(u) or {}
            cur = self.accounts.modules_text(rec)
            cur = "*" if cur == "全部" else cur.replace("、", ",")
            v = simpledialog.askstring(
                "可进入模块", "填 * 表示全部，或逗号分隔模块号（如 01,02,03）：",
                initialvalue=cur, parent=win)
            if v is None:
                return
            ok, msg = self.accounts.set_modules(u, v.strip() or "*")
            (messagebox.showinfo if ok else messagebox.showerror)(
                "模块范围", msg, parent=win)
            reload()

        def del_account():
            u = selected_user()
            if not u:
                return
            if u == owner_rec.get("username"):
                messagebox.showwarning("不能删除", "不能删除当前登录的所有者账户",
                                       parent=win)
                return
            if not messagebox.askyesno("确认删除", "确定删除账户 %s？" % u,
                                       parent=win):
                return
            ok, msg = self.accounts.delete(u)
            (messagebox.showinfo if ok else messagebox.showerror)(
                "删除账户", msg, parent=win)
            reload()

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text="＋ 新增账户", command=add_account).pack(side="left")
        ttk.Button(bar, text="🔑 重置口令", command=reset_pwd).pack(side="left", padx=6)
        ttk.Button(bar, text="🧩 修改模块范围", command=edit_modules).pack(side="left")
        ttk.Button(bar, text="🗑 删除账户", command=del_account).pack(side="left", padx=6)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        tk.Label(win, text="提示：口令只存 PBKDF2 哈希；重置后请通知使用人。"
                           "默认口令账户会在列表里标「是」。",
                 bg=BG, fg=MUTE, font=FONT_SMALL, anchor="w").pack(
            fill="x", padx=14, pady=(0, 10))
        reload()

    # ---------------------------------------------------------------- 审计 / 退出
    def audit(self, action, user, module, ok, detail=""):
        try:
            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action, "user": user, "module": module,
                "ok": bool(ok), "detail": detail,
            }
            try:
                if os.path.getsize(AUDIT_PATH) > 2 * 1024 * 1024:
                    os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
            except OSError:
                pass
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _open_audit(self):
        if not os.path.exists(AUDIT_PATH):
            messagebox.showinfo("审计日志", "还没有记录。")
            return
        try:
            os.startfile(AUDIT_PATH)
        except Exception as e:                       # noqa: BLE001
            messagebox.showerror("打不开", str(e))

    def _check_env(self):
        problems = []
        if not os.path.isfile(PYW):
            problems.append("缺少 runtime\\pythonw.exe")
        if _MAN_ERROR:
            problems.append("模块清单读取失败：%s" % _MAN_ERROR)
        if self._missing:
            real_missing = []
            for code in sorted(self._missing):
                meta = MAN.get("modules", {}).get(code, {})
                if meta.get("kind") == "docs" or not meta.get("entry"):
                    continue          # 04 这类"按设计没有入口"的不算故障
                real_missing.append(code)
            if real_missing:
                problems.append("缺入口：%s" % "、".join(real_missing))
        if problems:
            self.status.config(text="⚠ " + "；".join(problems), fg=WARN)
        else:
            self.status.config(
                text="就绪 · 账户 %d 个 · 选中模块后双击进入" %
                     len(self.accounts.list_accounts()), fg=DIM)

    def _on_close(self):
        running = [c for c, p in self._procs.items() if p.poll() is None]
        if running:
            if not messagebox.askyesno(
                    "退出总控台",
                    "以下模块仍在运行：%s\n\n关闭总控台不会关闭它们。确定退出？"
                    % "、".join(running)):
                return
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    root = tk.Tk()
    try:
        App(root)
    except Exception as e:                           # noqa: BLE001
        messagebox.showerror("总控台启动失败", "%s\n\n%s" % (e, PKG))
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
