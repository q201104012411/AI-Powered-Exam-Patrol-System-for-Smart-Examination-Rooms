# -*- coding: utf-8 -*-
"""03 智能终端 · AI 实时分析对话框。

- 独立对话框，不挤在底部状态栏；
- 本地 Ollama / OpenAI 兼容 API 两种后端，可切换模型；
- 输入问题，或「分析当前界面」让 AI 读取当前页签/告警/核验状态；
- 所有者可在对话框内开启/关闭“允许非所有者 AI 对话”；
- API Key 只保存在本机 data/ai_provider.json（尽量 0600）。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import ai_assistant
import config
import console
from ui_theme import (ACCENT, ALARM, BG, BORDER, DIM, FG, FONT, FONT_B,
                      MUTE, OK, PANEL, PANEL2, WARN)

PROVIDER_TEXT = {"ollama": "本地 Ollama", "openai": "OpenAI 兼容 API"}
TEXT_PROVIDER = {v: k for k, v in PROVIDER_TEXT.items()}


class AIDialog(tk.Toplevel):
    def __init__(self, master, session, get_snapshot, get_interface,
                 on_permission_changed=None):
        tk.Toplevel.__init__(self, master)
        self.title("AI 实时分析 · 智能终端")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.configure(bg=BG)
        self.transient(master)
        self.session = session
        self.get_snapshot = get_snapshot
        self.get_interface = get_interface
        self.on_permission_changed = on_permission_changed
        self.q = queue.Queue()
        self.busy = False
        self.auto_job = None
        self.last_snapshot = ""
        self.cfg = ai_assistant.load_provider_settings()
        self.chat = self._make_chat()
        self._build()
        # Toplevel 默认可能被主窗口盖住：先置顶再取消，确保对话框真正出现在最前
        try:
            self.update_idletasks()
            self.lift()
            self.attributes("-topmost", True)
            self.after(800, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except tk.TclError:
            pass
        self._load_models_async()
        self.after(150, self._poll)
        self.after(800, self._auto_tick)

    # ------------------------------------------------------------ UI
    def _build(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(head, text="🤖 AI 实时分析", bg=BG, fg=FG,
                 font=("Microsoft YaHei", 16, "bold")).pack(side="left")
        tk.Label(head, text="当前界面状态 → 对话/分析；模型或 API 可切换",
                 bg=BG, fg=DIM, font=("Microsoft YaHei", 9)).pack(side="left", padx=12)
        tk.Label(head, text="© 2026 EDY_POWERQ", bg=BG, fg=MUTE,
                 font=("Microsoft YaHei", 8)).pack(side="right")

        box = tk.LabelFrame(self, text=" 后端 / 模型 ", bg=PANEL, fg=ACCENT,
                            font=("Microsoft YaHei", 9, "bold"), bd=1)
        box.pack(fill="x", padx=16, pady=4)
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(row, text="后端：", bg=PANEL, fg=FG).pack(side="left")
        self.provider_var = tk.StringVar(
            value=PROVIDER_TEXT.get(self.cfg.get("provider"), PROVIDER_TEXT["ollama"]))
        self.provider_box = ttk.Combobox(
            row, textvariable=self.provider_var, state="readonly", width=16,
            values=list(PROVIDER_TEXT.values()))
        self.provider_box.pack(side="left")
        self.provider_box.bind("<<ComboboxSelected>>", lambda e: self._provider_changed())

        tk.Label(row, text="模型：", bg=PANEL, fg=FG).pack(side="left", padx=(12, 2))
        self.model_var = tk.StringVar(value=self.cfg.get("model") or
                                      ai_assistant.DEFAULT_MODEL)
        self.model_box = ttk.Combobox(row, textvariable=self.model_var, width=36)
        self.model_box.pack(side="left")
        ttk.Button(row, text="刷新模型", command=self._load_models_async).pack(
            side="left", padx=6)

        self.api_row = tk.Frame(box, bg=PANEL)
        self.api_row.pack(fill="x", padx=8, pady=(2, 4))
        tk.Label(self.api_row, text="API 地址：", bg=PANEL, fg=FG).pack(side="left")
        self.base_var = tk.StringVar(value=self.cfg.get("base_url") or
                                     "http://127.0.0.1:11434")
        self.base_entry = ttk.Entry(self.api_row, textvariable=self.base_var, width=34)
        self.base_entry.pack(side="left")
        tk.Label(self.api_row, text="Key：", bg=PANEL, fg=FG).pack(side="left", padx=(10, 2))
        self.key_var = tk.StringVar(value=self.cfg.get("api_key") or "")
        self.key_entry = ttk.Entry(self.api_row, textvariable=self.key_var, width=26,
                                   show="*")
        self.key_entry.pack(side="left")
        self.btn_save = ttk.Button(self.api_row, text="保存设置", command=self._save)
        self.btn_save.pack(side="left", padx=8)

        perm = tk.Frame(box, bg=PANEL)
        perm.pack(fill="x", padx=8, pady=(0, 6))
        self.allow_var = tk.BooleanVar(value=console.ai_chat_allowed())
        self.allow_chk = ttk.Checkbutton(
            perm, text="允许非所有者使用 AI 对话（所有者可开关）",
            variable=self.allow_var, command=self._toggle_allow)
        self.allow_chk.pack(side="left")
        self.perm_state = tk.Label(perm, text="", bg=PANEL, fg=DIM,
                                   font=("Microsoft YaHei", 8))
        self.perm_state.pack(side="left", padx=10)
        if self.session.role != config.ROLE_OWNER:
            self.btn_save.config(state="disabled")
            self.base_entry.config(state="disabled")
            self.key_entry.config(state="disabled")
            self.allow_chk.config(state="disabled")

        outwrap = tk.Frame(self, bg=PANEL, highlightbackground=BORDER,
                           highlightthickness=1)
        outwrap.pack(fill="both", expand=True, padx=16, pady=(6, 4))
        self.out = tk.Text(outwrap, wrap="word", bg="#0b1526", fg=FG,
                           insertbackground=FG, font=FONT, state="disabled",
                           padx=10, pady=8)
        self.out.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(outwrap, orient="vertical", command=self.out.yview)
        sb.pack(side="right", fill="y")
        self.out.configure(yscrollcommand=sb.set)
        self.out.tag_configure("user", foreground="#60a5fa", font=FONT_B)
        self.out.tag_configure("bot", foreground=OK)
        self.out.tag_configure("err", foreground=ALARM)
        self.out.tag_configure("dim", foreground=DIM)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(2, 4))
        self.input = tk.Text(bar, height=3, wrap="word", bg=PANEL, fg=FG,
                             insertbackground=FG, font=FONT)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Control-Return>", lambda e: (self._send(), "break")[1])
        btns = tk.Frame(bar, bg=BG)
        btns.pack(side="left", fill="y", padx=(8, 0))
        self.send_btn = ttk.Button(btns, text="发送", command=self._send)
        self.send_btn.pack(fill="x")
        ttk.Button(btns, text="分析当前界面",
                   command=self._analyze_now).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="清空", command=self._clear).pack(fill="x", pady=(4, 0))

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=16, pady=(0, 8))
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(foot, text="自动分析（状态变化时，约 15 秒一次）",
                        variable=self.auto_var).pack(side="left")
        self.status = tk.Label(foot, text="就绪", bg=BG, fg=DIM,
                               font=("Microsoft YaHei", 9))
        self.status.pack(side="right")
        self._provider_changed()
        self._append("系统", "AI 对话框已打开。可直接提问，或点「分析当前界面」。",
                     "dim")

    # ------------------------------------------------------------ 配置
    def _provider(self):
        return TEXT_PROVIDER.get(self.provider_var.get(), "ollama")

    def _provider_changed(self):
        is_api = self._provider() == "openai"
        state = "normal" if is_api else "disabled"
        if self.session.role == config.ROLE_OWNER:
            self.base_entry.config(state=state)
            self.key_entry.config(state=state)
            self.btn_save.config(state="normal")
        self.model_var.set(self.cfg.get("model") or
                           ("" if is_api else ai_assistant.DEFAULT_MODEL))
        self._load_models_async()

    def _save(self):
        if self.session.role != config.ROLE_OWNER:
            messagebox.showerror("权限不足", "只有所有者可以保存 AI 后端设置。", parent=self)
            return
        cfg = {
            "provider": self._provider(),
            "model": self.model_var.get().strip() or ai_assistant.DEFAULT_MODEL,
            "base_url": self.base_var.get().strip(),
            "api_key": self.key_var.get().strip(),
        }
        ok, msg = ai_assistant.save_provider_settings(cfg)
        if not ok:
            messagebox.showerror("保存失败", msg, parent=self)
            return
        self.cfg = cfg
        self.chat = self._make_chat()
        self.status.config(text="设置已保存：%s" % PROVIDER_TEXT.get(cfg["provider"], ""))
        messagebox.showinfo("已保存", "AI 后端设置已保存到本机。", parent=self)

    def _make_chat(self):
        return ai_assistant.AIChat(
            model=self.model_var.get().strip() if hasattr(self, "model_var") else None,
            provider=self.cfg.get("provider", "ollama"),
            base_url=self.cfg.get("base_url", ""),
            api_key=self.cfg.get("api_key", ""))

    def _load_models_async(self):
        provider = self._provider()
        base, key = self.base_var.get().strip(), self.key_var.get().strip()
        self.status.config(text="正在获取模型列表…")

        def work():
            try:
                if provider == "openai":
                    models = ai_assistant.openai_models(base, key)
                else:
                    models = ai_assistant.list_models()
            except Exception as exc:                          # noqa: BLE001
                models = []
                err = str(exc)
            else:
                err = ""
            self.q.put(("models", (models, err)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_models(self, models, err):
        if models:
            self.model_box["values"] = models
            if self.model_var.get() not in models:
                self.model_var.set(models[0])
            self.status.config(text="模型 %d 个" % len(models))
        else:
            self.status.config(text="未获取到模型（可手动输入）" + (("：" + err[:80]) if err else ""))

    def _toggle_allow(self):
        if self.session.role != config.ROLE_OWNER:
            self.allow_var.set(console.ai_chat_allowed())
            return
        ok, msg = console.set_ai_chat(self.allow_var.get())
        if not ok:
            messagebox.showerror("保存失败", msg, parent=self)
        self.allow_var.set(console.ai_chat_allowed())
        self.perm_state.config(text="已开启" if self.allow_var.get() else "仅所有者")
        if self.on_permission_changed:
            self.on_permission_changed()

    # ------------------------------------------------------------ 对话
    def _append(self, who, text, tag):
        self.out.config(state="normal")
        self.out.insert("end", "%s：%s\n" % (who, text), tag)
        self.out.see("end")
        self.out.config(state="disabled")

    def _send(self):
        text = self.input.get("1.0", "end").strip()
        if not text or self.busy:
            return
        if not console.can_ai_chat(self.session.role):
            messagebox.showerror("AI 对话未授权",
                                 "当前角色未获得 AI 对话授权，请所有者开启。", parent=self)
            return
        self.input.delete("1.0", "end")
        self._append("你", text, "user")
        self._ask_async(text, source="ai-dialog")
        self.last_snapshot = ""

    def _analyze_now(self):
        if self.busy:
            return
        try:
            snap = self.get_snapshot()
        except Exception as exc:                              # noqa: BLE001
            self._append("系统", "生成状态快照失败：%s" % exc, "err")
            return
        self._append("系统", "分析当前界面…", "dim")
        self._analyze_async(snap)

    def _ask_async(self, text, source="ai-dialog"):
        self.busy = True
        self.send_btn.config(state="disabled")
        provider = self._provider()
        cfg = {"provider": provider, "base_url": self.base_var.get().strip(),
               "api_key": self.key_var.get().strip()}
        model = self.model_var.get().strip() or ai_assistant.DEFAULT_MODEL
        chat = ai_assistant.AIChat(model=model, **cfg)
        interface = self.get_interface()

        def work():
            try:
                ok, reply, transport = chat.ask(
                    text, operator=self.session.username,
                    tier=("full" if self.session.role == config.ROLE_OWNER else "ai"),
                    source=source, timeout=120, interface=interface)
            except Exception as exc:                          # noqa: BLE001
                ok, reply, transport = False, "AI 调用异常：%s" % exc, ""
            self.q.put(("chat", (ok, reply, transport)))

        threading.Thread(target=work, daemon=True).start()

    def _analyze_async(self, snap):
        self.busy = True
        self.send_btn.config(state="disabled")
        provider = self._provider()
        cfg = {"provider": provider, "base_url": self.base_var.get().strip(),
               "api_key": self.key_var.get().strip()}
        model = self.model_var.get().strip() or ai_assistant.DEFAULT_MODEL
        interface = self.get_interface()

        def work():
            try:
                ok, reply, transport = ai_assistant.analyze_activity(
                    snap, model=model, operator=self.session.username,
                    tier=("full" if self.session.role == config.ROLE_OWNER else "ai"),
                    source="ai-dialog-watch", timeout=90, interface=interface,
                    provider=provider, base_url=cfg["base_url"], api_key=cfg["api_key"])
            except Exception as exc:                          # noqa: BLE001
                ok, reply, transport = False, "AI 分析异常：%s" % exc, ""
            self.q.put(("chat", (ok, reply, transport)))

        threading.Thread(target=work, daemon=True).start()

    def _auto_tick(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self.auto_var.get() and not self.busy and console.can_ai_chat(self.session.role):
            try:
                snap = self.get_snapshot()
            except Exception:                                # noqa: BLE001
                snap = ""
            if snap and snap != self.last_snapshot:
                self.last_snapshot = snap
                self._analyze_async(snap)
        self.auto_job = self.after(15000, self._auto_tick)

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "models":
                    self._apply_models(*data)
                elif kind == "chat":
                    ok, reply, transport = data
                    self.busy = False
                    self.send_btn.config(state="normal")
                    self._append("AI" if ok else "错误", reply, "bot" if ok else "err")
                    self.status.config(text=("回复完成 · %s" % (transport or "")) if ok
                                       else "调用失败")
        except queue.Empty:
            pass
        try:
            self.after(150, self._poll)
        except tk.TclError:
            pass

    def _clear(self):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.config(state="disabled")
        self.chat = self._make_chat()
        self._append("系统", "对话已清空。", "dim")
