# -*- coding: utf-8 -*-
r"""
09 无障碍与特殊考生保障 · 桌面界面（tkinter，仅标准库）

这个界面干什么
--------------
把《无障碍与特殊考生保障.md》的"别让智能化设备本身变成新的不公平"落到可点击的七页签上，
每一页都是**真实业务函数**在干活（access / verify / fairness / drill / metrics / ledger /
report / linkage_bridge），界面只负责录入、展示与留痕，指标口径与报告口径完全同源：

  ① 概览            四个硬指标 + 三个过程指标（达标灯）、考点三通道状态、数据目录、
                    加密状态、审计链结论、联动摘要（后台线程，不卡界面）
  ② 无障碍通道      通道台账（净宽/回转/低位终端/双提示）+ 合规校验结论 +
                    新增/改通道（不合格**允许保存但逐条列出 error**，因为现场就是这样去整改）
  ③ 特殊考生档案    档案列表（证件号脱敏）+ 建档（需求多选 + 单独同意）+
                    保障措施逐项落实/不予（已落实必填责任人、不予必写理由）
  ④ 替代核验        人工核验录入（双证比对 + 双人见证）、受阻事件登记与结案、
                    "人工核验与智能核验同等效力"显著声明
  ⑤ 算法公平性      六个分层维度录入 -> 组间最大差 -> 补偿策略 -> 历史与整改趋势
  ⑥ 演练与台账      演练登记（有问题必须写整改）、逐考点可用性、脱敏台账预览与导出
  ⑦ 联动与验收      各专题可达性、全链路探测（可选是否允许往 03 写测试告警）、
                    补发待发事件、一键生成验收报告

为什么这么设计
--------------
· **人优先于设备**：界面上没有"因设备失败拒绝入场"的按钮，只有"转人工核验""登记受阻事件"
  和"结案为替代通道入场"；这是把制度写进交互。
· **如实说明**：本机没有无障碍闸机、低位核验终端、手语翻译等实物，通道尺寸与提示方式是
  **现场实测录入值**，系统只做合规判定、不推算不代填；`source=demo` 的数据在界面上明确标"演示"。
· **联动慢就放后台**：00/01/02/03/06 的调用都是子进程，可能耗时数十秒，
  一律 `threading.Thread` + `root.after` 回填结果，主线程绝不阻塞；
  对端不可达时**原样显示原因**，不假装成功。
· **零第三方依赖**：只用 tkinter/ttk/sqlite3/threading 等标准库；装了 cryptography
  则敏感字段自动加密（未装则如实提示"未加密"）。

自检怎么用这个文件
------------------
`自检.py` 的 [12] GUI 冒烟会执行 `import app; root=Tk(); root.withdraw(); app.App(root)`，
然后检查页签数并逐页切换。所以本文件的硬约束是：

· 类名 `App`，构造签名 `App(root)`；
· 构造后 `self.notebook` 是 ttk.Notebook，页签 7 个（≥7）；
· `select_tab(index_or_title)`：**索引按 1 起算（1 = 第一个页签）**，
  也接受页签标题（如 "③ 特殊考生档案"）；越界不抛异常，返回实际选中页签的 0 基索引或 -1；
· 构造过程**不弹任何对话框、不调用 mainloop、不启动后台线程**（后台任务用 `root.after` 延迟排期，
  自检里 root 被 destroy，排期不会执行）。

运行：`python app.py`（或 pythonw.exe；入口已先调 config.ensure_stdio() 兜住 stdout=None）。
"""
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# 独立运行：把包根加入 sys.path，才能 import 包根的 ui_theme
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.isfile(os.path.join(_p, "ui_theme.py")):
        if _p not in sys.path:
            sys.path.append(_p)
        break

try:
    import ui_theme
except Exception:                                            # noqa: BLE001
    ui_theme = None

import config
import access
import data_crypto
import drill as drill_mod
import fairness as fairness_mod
import ledger as ledger_mod
import linkage_bridge
import metrics as metrics_mod
import models
import report as report_mod
import storage
import verify as verify_mod

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
FONT_T = ("Microsoft YaHei", 15, "bold")
FONT_S = ("Microsoft YaHei", 9)

C_OK = "#0a7d28"
C_WARN = "#e08b00"
C_BAD = "#c00000"
C_DIM = "#555555"
C_TAG = "#0b5394"
SOURCE_TEXT = {config.REAL_SOURCE: "真实", config.MANUAL_SOURCE: "人工录入",
               config.DEMO_SOURCE: "演示（demo）", "sim": "模拟", "real": "真实"}


def label_to_key(mapping, text, default=""):
    """下拉框显示中文 -> 取回字典键（找不到就用 default）。"""
    for k, v in mapping.items():
        if v == text:
            return k
    return default or text


def source_text(src):
    return SOURCE_TEXT.get(str(src or ""), str(src or "-"))


def to_int(text, default=0):
    try:
        return int(str(text).strip() or default)
    except (TypeError, ValueError):
        return default


def to_float(text, default=0.0):
    try:
        return float(str(text).strip() or default)
    except (TypeError, ValueError):
        return default


class App(object):
    """09 无障碍与特殊考生保障 主窗口（构造时只建界面 + 读本地库，不弹窗、不阻塞）。"""

    def __init__(self, root):
        self.root = root
        self.cfg = config.load_settings()
        self.store = storage.Store()
        self._bg_busy = 0
        self._log_lines = []

        root.title("无障碍与特殊考生保障 · 专题09（人工兜底不可取消）")
        root.geometry("1360x880")
        root.minsize(1120, 720)
        if ui_theme is not None:
            ui_theme.apply(self.root)

        self._build_header()
        self._build_tabs()
        self._build_status()

        # 首次填充：只读本地 SQLite，毫秒级；联动的慢操作一律走 root.after 排期
        try:
            self.refresh_all()
        except Exception as e:                                     # noqa: BLE001
            self._log("首次刷新异常：%s: %s" % (type(e).__name__, str(e)[:120]))
        if ui_theme is not None:
            ui_theme.polish(self.root)
            ui_theme.polish_later(self.root, 600)
        try:
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        except Exception:                                          # noqa: BLE001
            pass
        # 后台任务延迟排期（自检里 root 随即 destroy，这些回调不会执行）
        self.root.after(300, self._bg_link_summary)
        self.root.after(700, self._bg_link_availability)

    # ================= 线程与排期 =================
    def _ui(self, fn, *args):
        """把回调排到主线程（工作线程只允许用这一个入口回填界面）。"""
        try:
            self.root.after(0, lambda: self._safe(fn, *args))
        except Exception:                                          # noqa: BLE001
            pass

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:                                     # noqa: BLE001
            self._log("界面回填异常：%s: %s" % (type(e).__name__, str(e)[:140]))

    def _run_bg(self, name, work, done, busy_text=""):
        """
        在后台线程跑 work()（跨模块子进程调用都在这里），完成后把结果交 done(result, err)
        回主线程执行。异常不吞：err 里带类型与消息，界面原样显示。
        """
        self._bg_busy += 1
        if busy_text:
            self.var_status.set(busy_text)
        self._log("[后台] %s 开始（%s）" % (name, time.strftime("%H:%M:%S")))
        self.root.update_idletasks()

        def runner():
            result, err = None, ""
            try:
                result = work()
            except Exception as e:                                 # noqa: BLE001
                err = "%s: %s" % (type(e).__name__, str(e)[:200])
            def finish():
                self._bg_busy = max(0, self._bg_busy - 1)
                self._log("[后台] %s 结束%s" % (name, ("：失败 " + err) if err else ""))
                self._safe(done, result, err)
            self._ui(finish)

        threading.Thread(target=runner, name="acc09-bg", daemon=True).start()

    def _notifier(self):
        """推 03 智能终端的通知器（03 不可达时 linkage_bridge 会写 outbox，事件不丢）。"""
        return lambda code, info: linkage_bridge.notify(code, info, store=self.store)

    # ================= 顶部 / 底部 =================
    def _build_header(self):
        top = tk.Frame(self.root, bg="#12395c", height=62)
        top.pack(fill="x")
        tk.Label(top, text="  无障碍与特殊考生保障", bg="#12395c", fg="white",
                 font=FONT_T).pack(side="left", pady=12)
        tk.Label(top, text="特殊考生登记 → 需求识别 → 保障措施 → 替代通道（人工核验不可取消）"
                           " → 全程留痕",
                 bg="#12395c", fg="#bcd7ef", font=FONT).pack(side="left", padx=10)
        self.var_site = tk.StringVar(
            value="考点：%s ｜ 考期：%s" % ("、".join(config.sites(self.cfg)),
                                          self.cfg.get("session", "")))
        tk.Label(top, textvariable=self.var_site, bg="#12395c", fg="#ffd54f",
                 font=FONT_B).pack(side="right", padx=18)

    def _build_status(self):
        bar = tk.Frame(self.root, bg="#eeeeee", height=26)
        bar.pack(fill="x", side="bottom")
        self.var_status = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.var_status, bg="#eeeeee", anchor="w",
                 font=FONT_S).pack(side="left", padx=8)
        self.var_data_dir = tk.StringVar(value="数据目录：%s" % config.DATA_ROOT)
        tk.Label(bar, textvariable=self.var_data_dir, bg="#eeeeee", fg=C_DIM,
                 font=FONT_S).pack(side="right", padx=8)

    def _log(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        self._log_lines.append(line)
        del self._log_lines[:-300]
        tv = getattr(self, "txt_log", None)
        if tv is None:
            return
        try:
            tv.configure(state="normal")
            tv.insert("end", line + "\n")
            tv.see("end")
            tv.configure(state="disabled")
        except Exception:                                          # noqa: BLE001
            pass

    # ================= 页签 =================
    def _build_tabs(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=4)
        self.tab_overview = tk.Frame(self.notebook)
        self.tab_channels = tk.Frame(self.notebook)
        self.tab_profiles = tk.Frame(self.notebook)
        self.tab_altverify = tk.Frame(self.notebook)
        self.tab_fairness = tk.Frame(self.notebook)
        self.tab_drill = tk.Frame(self.notebook)
        self.tab_linkage = tk.Frame(self.notebook)
        for frame, title in ((self.tab_overview, "① 概览"),
                             (self.tab_channels, "② 无障碍通道"),
                             (self.tab_profiles, "③ 特殊考生档案"),
                             (self.tab_altverify, "④ 替代核验"),
                             (self.tab_fairness, "⑤ 算法公平性"),
                             (self.tab_drill, "⑥ 演练与台账"),
                             (self.tab_linkage, "⑦ 联动与验收")):
            self.notebook.add(frame, text=title)
        self._build_overview()
        self._build_channels()
        self._build_profiles()
        self._build_altverify()
        self._build_fairness()
        self._build_drill()
        self._build_linkage()

    @property
    def tabs(self):
        """给自检用的兼容属性（等价于 self.notebook）。"""
        return self.notebook

    def select_tab(self, index_or_title):
        """
        切换页签。**索引按 1 起算**（1 = ① 概览，…，7 = ⑦ 联动与验收），
        也接受页签标题文本（如 "⑤ 算法公平性"）。
        越界或找不到：不抛异常，返回 -1 并在状态栏说明。返回值为 0 基索引（成功时）。
        """
        try:
            n = len(self.notebook.tabs())
        except Exception:                                          # noqa: BLE001
            return -1
        title = str(index_or_title)
        idx = -1
        try:
            i = int(title)
        except (TypeError, ValueError):
            i = None
        if i is not None:
            if 1 <= i <= n:
                idx = i - 1                                        # 1 基
            elif 0 <= i < n and i == 0:
                idx = 0                                            # 容忍 0 当第一个
        if idx < 0:
            for k in range(n):
                if self.notebook.tab(k, "text") == title or title in self.notebook.tab(k, "text"):
                    idx = k
                    break
        if idx < 0:
            self.var_status.set("页签不存在或越界：%s（共 %d 个页签，索引 1 起算）"
                                % (title, n))
            return -1
        self.notebook.select(idx)
        self.var_status.set("已切换到「%s」" % self.notebook.tab(idx, "text"))
        return idx

    # ================= 通用控件 =================
    @staticmethod
    def _tree(parent, cols, height=10, select_mode="browse"):
        frame = tk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tv = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                          height=height, selectmode=select_mode)
        for title, width in cols:
            tv.heading(title, text=title)
            tv.column(title, width=width, anchor="w")
        vs = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        return tv

    @staticmethod
    def _fill(tv, rows, tags=None):
        tv.delete(*tv.get_children())
        for r in rows:
            tv.insert("", "end", values=r)

    @staticmethod
    def _entry(parent, key, store, width=14, value=""):
        e = tk.Entry(parent, width=width, font=FONT)
        if value:
            e.insert(0, str(value))
        store[key] = e
        return e

    @staticmethod
    def _label(parent, text, fg=None):
        return tk.Label(parent, text=text, font=FONT_S, fg=fg or "#000000")

    def _combo(self, parent, key, store, values, width=16, current=0):
        cb = ttk.Combobox(parent, width=width, state="readonly", values=list(values))
        if values:
            cb.current(min(max(current, 0), len(values) - 1))
        cb.pack(side="left", padx=(2, 10))
        store[key] = cb
        return cb

    # =====================================================================
    # ① 概览
    # =====================================================================
    def _build_overview(self):
        f = self.tab_overview

        note = tk.Label(
            f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1300,
            text="如实说明：本机没有无障碍闸机/低位核验终端/手语翻译等实物，"
                 "「硬性尺寸」与「提示方式」均为现场实测录入值，系统只做合规判定、不推算不代填；"
                 "source=demo 的记录在界面上标「演示」，绝不冒充真实保障记录。"
                 "指标口径：通道配置率 100%%、替代通道可用性 100%% 且每考期演练≥1 次、"
                 "因技术原因未入场 0 起、分层差异 ≤%.0f%%。"
                 % (config.fairness_max_gap(self.cfg) * 100))
        note.pack(fill="x", padx=8, pady=(6, 2))

        cards = tk.Frame(f)
        cards.pack(fill="x", padx=8, pady=4)
        self.card_vars = {}
        self.card_specs = [
            ("channel_rate", "无障碍通道配置率"), ("alt_availability", "替代核验通道可用性"),
            ("tech_block_denied", "因技术原因未入场"), ("fairness", "算法分层测试"),
            ("measure_coverage", "保障措施落实率"), ("alt_verify_timely", "替代核验及时率"),
            ("block_closure", "受阻事件闭环率"),
        ]
        for i, (key, title) in enumerate(self.card_specs):
            box = tk.Frame(cards, bd=1, relief="solid", padx=8, pady=4)
            box.grid(row=0, column=i, padx=3, sticky="nsew")
            cards.grid_columnconfigure(i, weight=1)
            tk.Label(box, text=title, font=FONT_S, fg=C_DIM).pack(anchor="w")
            v = tk.StringVar(value="-")
            self.card_vars[key] = v
            tk.Label(box, textvariable=v, font=("Microsoft YaHei", 13, "bold")).pack(anchor="w")
            v2 = tk.StringVar(value="")
            self.card_vars[key + "__target"] = v2
            tk.Label(box, textvariable=v2, font=("Microsoft YaHei", 8),
                     fg=C_DIM, wraplength=170, justify="left").pack(anchor="w")

        mid = tk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.LabelFrame(mid, text=" 关键指标（硬指标 vs 过程指标；口径与验收报告同源）")
        left.pack(side="left", fill="both", expand=True)
        self.tv_metrics = self._tree(left, [("类别", 70), ("指标", 150), ("实测", 200),
                                            ("要求", 210), ("结论", 70), ("说明", 330)],
                                     height=9)
        self.tv_metrics.tag_configure("ok", foreground=C_OK)
        self.tv_metrics.tag_configure("bad", foreground=C_BAD)
        self.tv_metrics.tag_configure("warn", foreground=C_WARN)

        right = tk.LabelFrame(mid, text=" 运行日志（只在本机记，不外发）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        holder = tk.Frame(right)
        holder.pack(fill="both", expand=True)
        self.txt_log = tk.Text(holder, height=9, font=FONT_S, state="disabled",
                               wrap="none")
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for line in self._log_lines[-80:]:
            self._log(line.split("  ", 1)[-1])

        mid2 = tk.Frame(f)
        mid2.pack(fill="both", expand=True, padx=8, pady=4)
        l2 = tk.LabelFrame(mid2, text=" 考点三通道并行状态（access.channel_config_rate 明细）")
        l2.pack(side="left", fill="both", expand=True)
        self.tv_site3 = self._tree(l2, [("考点", 120), ("通道数", 60), ("智能", 55),
                                        ("人工", 55), ("无障碍", 65), ("替代可用", 70),
                                        ("结论", 150), ("缺什么/问题", 380)], height=6)
        self.tv_site3.tag_configure("bad", foreground=C_BAD)
        self.tv_site3.tag_configure("ok", foreground=C_OK)

        r2 = tk.LabelFrame(mid2, text=" 数据目录 / 加密 / 审计链 / 联动摘要")
        r2.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.txt_env = tk.Text(r2, height=6, font=FONT_S, wrap="word", state="disabled")
        self.txt_env.pack(fill="both", expand=True)

        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(btns, text="🔄 刷新全部", command=self.refresh_all).pack(side="left")
        tk.Button(btns, text="📊 重算指标（后台）",
                  command=lambda: self._run_bg("重算指标", self._work_metrics,
                                               self._done_metrics)).pack(side="left", padx=6)
        tk.Button(btns, text="🔗 联动摘要（后台）",
                  command=self._bg_link_summary).pack(side="left")
        tk.Button(btns, text="🩺 全链路探测（后台，只读）",
                  command=lambda: self.select_tab("⑦ 联动与验收")).pack(side="left", padx=6)
        tk.Button(btns, text="🧾 生成验收报告（后台）",
                  command=self.gen_report_bg).pack(side="left")

    def refresh_all(self):
        self.refresh_overview()
        self.refresh_channels()
        self.refresh_profiles()
        self.refresh_accommodations()
        self.refresh_altverify()
        self.refresh_tech_blocks()
        self.refresh_fairness()
        self.refresh_drills()
        self.refresh_ledger()
        self.var_status.set("已刷新（本地库读取，未发起任何跨模块调用）")

    def _work_metrics(self):
        return metrics_mod.compute(self.store, config.load_settings())

    def _done_metrics(self, m, err):
        if err or not m:
            self._log("指标重算失败：%s" % err)
            return
        self._render_metrics(m)
        self.var_status.set("指标已重算：硬指标%s" % ("全部达标" if m["ok"]
                                                     else "未达标：" + "、".join(m["hard_fail"])))

    def refresh_overview(self):
        try:
            m = metrics_mod.compute(self.store, config.load_settings())
        except Exception as e:                                     # noqa: BLE001
            self._log("指标计算异常：%s: %s" % (type(e).__name__, str(e)[:140]))
            m = None
        if m is not None:
            self._render_metrics(m)
        self._render_site3(m)
        self._render_env()

    def _render_metrics(self, m):
        by_key = {i["key"]: i for i in m["items"]}
        for key, _title in self.card_specs:
            item = by_key.get(key)
            if not item:
                continue
            self.card_vars[key].set(item["value_text"])
            self.card_vars[key + "__target"].set(
                ("硬指标" if item["level"] == "hard" else "过程指标") + "｜"
                + item["target_text"])
        rows, tags = [], []
        for i in m["items"]:
            mark = "达标" if i["ok"] else ("未达标" if i["level"] == "hard" else "待改进")
            rows.append(("硬指标" if i["level"] == "hard" else "过程指标", i["name"],
                         i["value_text"], i["target_text"], mark, i["detail"]))
            tags.append("ok" if i["ok"] else ("bad" if i["level"] == "hard" else "warn"))
        self._fill(self.tv_metrics, rows)
        for row, tag in zip(self.tv_metrics.get_children(), tags):
            self.tv_metrics.item(row, tags=(tag,))

    def _render_site3(self, m):
        cr = (m or {}).get("raw", {}).get("channel_rate")
        if cr is None:
            try:
                cr = access.channel_config_rate(self.store, config.load_settings())
            except Exception:                                      # noqa: BLE001
                cr = {"details": []}
        rows, tags = [], []
        for d in cr.get("details", []):
            prob = "；".join(d["channel_errors"][:2]) or "-"
            rows.append((d["site"], d["total"], d["usable"].get(config.CH_SMART, 0),
                         d["usable"].get(config.CH_MANUAL, 0),
                         d["usable"].get(config.CH_ACCESSIBLE, 0),
                         "可用" if d["alt_available"] else "不可用",
                         "已配齐" if d["ok"] else "缺 " + "、".join(d["missing_text"]),
                         prob))
            tags.append("ok" if d["ok"] else "bad")
        if not rows:
            rows = [("-", 0, 0, 0, 0, "-", "尚无考点通道数据", "到「② 无障碍通道」登记通道")]
            tags = ["bad"]
        self._fill(self.tv_site3, rows)
        for row, tag in zip(self.tv_site3.get_children(), tags):
            self.tv_site3.item(row, tags=(tag,))

    def _render_env(self):
        try:
            ok_chain, chain_msg, n = self.store.verify_audit_chain()
        except Exception as e:                                     # noqa: BLE001
            ok_chain, chain_msg, n = False, "审计链校验异常：%s" % str(e)[:80], 0
        try:
            ob = self.store.outbox_stats()
        except Exception:                                          # noqa: BLE001
            ob = {"total": 0, "pending": 0}
        try:
            crypto = data_crypto.status_text()
        except Exception:                                          # noqa: BLE001
            crypto = "加密模块不可用"
        info = {
            "数据目录": str(config.DATA_ROOT),
            "数据库": str(config.DB_PATH),
            "报告导出": str(config.REPORT_DIR),
            "台账导出": str(config.LEDGER_DIR),
            "考点清单": "、".join(config.sites(self.cfg)),
            "考期": self.cfg.get("session", ""),
            "硬性尺寸": "净宽≥%dmm、回转≥%dmm、低位终端 %d~%dmm"
                        % (config.MIN_AISLE_WIDTH_MM, config.MIN_TURN_RADIUS_MM,
                           config.COUNTER_HEIGHT_MIN_MM, config.COUNTER_HEIGHT_MAX_MM),
            "敏感字段加密": crypto,
            "审计链": "%s %s" % ("完整 ✔" if ok_chain else "异常 ✘", chain_msg),
            "联动外发队列": "共 %d 条，待补发 %d 条（03 不可达时事件不丢）"
                            % (ob.get("total", 0), ob.get("pending", 0)),
            "联动摘要": self._link_text or "尚未获取（点「联动摘要（后台）」）",
            "等效性声明": config.EQUIVALENCE_NOTE,
            "依赖": "仅标准库 tkinter/sqlite3/threading；cryptography 为可选增强",
        }
        self.txt_env.configure(state="normal")
        self.txt_env.delete("1.0", "end")
        for k, v in info.items():
            self.txt_env.insert("end", "%s：%s\n" % (k, v))
        self.txt_env.configure(state="disabled")

    _link_text = ""

    def _bg_link_summary(self):
        self._run_bg("联动摘要", linkage_bridge.link_summary, self._done_link_summary,
                     busy_text="后台获取联动摘要…")

    def _done_link_summary(self, r, err):
        if err or not r:
            self._link_text = "联动摘要获取失败：%s" % (err or "无返回")
        else:
            self._link_text = r.get("text", "")
        self._render_env()
        self.var_status.set(self._link_text)

    # =====================================================================
    # ② 无障碍通道
    # =====================================================================
    def _build_channels(self):
        f = self.tab_channels
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1300,
                 text="合规口径：无障碍专用通道 净宽≥%dmm、回转≥%dmm、必须有低位核验终端"
                      "（台面≤%dmm 且高度可调）；三种提示（语音/文字/震动）至少 1 种、"
                      "文字提示为听力考生必需；标记「降级可用」时替代提示方式须≥2 种。"
                      "不合格**允许保存**（现场就是要拿这张问题清单去整改），"
                      "但 error 会逐条列出来并写进审计。"
                      % (config.MIN_AISLE_WIDTH_MM, config.MIN_TURN_RADIUS_MM,
                         config.LOW_COUNTER_MAX_MM)).pack(fill="x", padx=8, pady=(6, 2))

        top = tk.Frame(f)
        top.pack(fill="both", expand=True, padx=8, pady=4)
        self.tv_chan = self._tree(top, [("编码", 90), ("名称", 150), ("考点", 100),
                                        ("类型", 110), ("位置", 110), ("净宽mm", 65),
                                        ("回转mm", 65), ("低位", 50), ("台面mm", 65),
                                        ("高可调", 55), ("语音", 50), ("文字", 50),
                                        ("震动", 50), ("状态", 80), ("校验", 200),
                                        ("来源", 80)], height=12)
        self.tv_chan.tag_configure("bad", foreground=C_BAD)
        self.tv_chan.tag_configure("ok", foreground=C_OK)
        self.tv_chan.bind("<<TreeviewSelect>>", lambda e: self.channel_pick())

        form = tk.LabelFrame(f, text=" 新增 / 修改通道（选中表格行后改字段再保存；通道编码是主键）")
        form.pack(fill="x", padx=8, pady=4)
        self.ch = {}
        r1 = tk.Frame(form)
        r1.pack(fill="x", padx=6, pady=3)
        for title, key, width in (("编码", "code", 12), ("名称", "name", 18),
                                  ("考点", "site", 12), ("位置", "location", 14)):
            self._label(r1, title).pack(side="left")
            self._entry(r1, key, self.ch, width)
            self.ch[key].pack(side="left", padx=(2, 10))
        self._label(r1, "类型").pack(side="left")
        self.cb_ch_kind = self._combo(r1, "kind", self.ch,
                                      [config.CHANNEL_TEXT[k] for k in config.CHANNEL_KINDS], 14)
        self._label(r1, "状态").pack(side="left")
        self.cb_ch_status = self._combo(r1, "status", self.ch,
                                        [config.CHANNEL_STATE_TEXT[k] for k in config.CHANNEL_STATES], 12)
        self.cb_ch_status.set(config.CHANNEL_STATE_TEXT[config.ST_OK])

        r2 = tk.Frame(form)
        r2.pack(fill="x", padx=6, pady=3)
        for title, key, width in (("净宽mm", "width_mm", 7), ("回转mm", "turn_radius_mm", 7),
                                  ("台面高度mm", "counter_height_mm", 7),
                                  ("设备编码(逗号分隔)", "devices", 26)):
            self._label(r2, title).pack(side="left")
            self._entry(r2, key, self.ch, width)
            self.ch[key].pack(side="left", padx=(2, 10))
        self.ch_checks = {}
        for key, text in (("low_counter", "低位核验终端"), ("height_adjustable", "高度可调"),
                          ("voice_prompt", "语音提示"), ("text_prompt", "文字/大字提示"),
                          ("vibration", "震动提示"), ("sign_service", "手语/书面交流")):
            v = tk.BooleanVar(value=(key in ("text_prompt", "voice_prompt", "vibration",
                                             "height_adjustable")))
            self.ch_checks[key] = v
            tk.Checkbutton(r2, text=text, variable=v, font=FONT_S).pack(side="left", padx=2)

        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=4)
        tk.Button(btns, text="➕ 新增（清空表单）", command=self.channel_clear).pack(side="left")
        tk.Button(btns, text="💾 保存并校验", bg="#c8e6c9",
                  command=self.channel_save).pack(side="left", padx=6)
        tk.Button(btns, text="🧹 清空表单", command=self.channel_clear).pack(side="left")
        tk.Button(btns, text="✅ 检查三通道并行（access.site_status）",
                  command=self.channel_check_site).pack(side="left", padx=6)
        tk.Button(btns, text="📡 登记现场检查时间", command=self.channel_mark_check).pack(side="left")

    def refresh_channels(self):
        rows, tags = [], []
        try:
            chans = self.store.list_channels()
        except Exception as e:                                     # noqa: BLE001
            self._log("通道列表读取失败：%s" % str(e)[:120])
            chans = []
        for c in chans:
            try:
                v = access.validate_channel(c)
            except Exception as e:                                 # noqa: BLE001
                v = {"ok": False, "errors": [{"text": "校验异常：%s" % str(e)[:60]}]}
            if v["ok"]:
                chk = "合规（warn %d 项）" % len(v.get("warns") or [])
            else:
                chk = "error %d 项：" % len(v["errors"]) + "；".join(
                    i["text"] for i in v["errors"][:2])
            rows.append((c.code, c.name, c.site, c.kind_text(), c.location or "-",
                         c.width_mm, c.turn_radius_mm, "是" if c.low_counter else "否",
                         c.counter_height_mm, "是" if c.height_adjustable else "否",
                         "是" if c.voice_prompt else "否", "是" if c.text_prompt else "否",
                         "是" if c.vibration else "否", c.state_text(), chk,
                         source_text(c.source)))
            tags.append("ok" if v["ok"] else "bad")
        self._fill(self.tv_chan, rows)
        for row, tag in zip(self.tv_chan.get_children(), tags):
            self.tv_chan.item(row, tags=(tag,))

    def channel_clear(self):
        for key, e in self.ch.items():
            if isinstance(e, tk.Entry):
                e.delete(0, "end")
        for v in self.ch_checks.values():
            v.set(False)
        self.ch_checks["text_prompt"].set(True)
        self.ch_checks["voice_prompt"].set(True)
        self.cb_ch_kind.set(config.CHANNEL_TEXT[config.CH_ACCESSIBLE])
        self.cb_ch_status.set(config.CHANNEL_STATE_TEXT[config.ST_OK])
        self.ch["site"].insert(0, config.sites(self.cfg)[0] if config.sites(self.cfg) else "")
        self.ch["name"].insert(0, "无障碍专用通道")
        self.ch["width_mm"].insert(0, str(config.MIN_AISLE_WIDTH_MM))
        self.ch["turn_radius_mm"].insert(0, str(config.MIN_TURN_RADIUS_MM))
        self.ch["counter_height_mm"].insert(0, str(config.COUNTER_HEIGHT_MIN_MM + 100))
        self.ch_checks["low_counter"].set(True)
        self.ch_checks["height_adjustable"].set(True)

    def channel_pick(self):
        sel = self.tv_chan.selection()
        if not sel:
            return
        code = self.tv_chan.item(sel[0], "values")[0]
        c = self.store.get_channel(code=code)
        if c is None:
            return
        self.channel_clear()
        for key in ("code", "name", "site", "location"):
            e = self.ch[key]
            e.delete(0, "end")
            e.insert(0, str(getattr(c, key, "") or ""))
        for key in ("width_mm", "turn_radius_mm", "counter_height_mm"):
            e = self.ch[key]
            e.delete(0, "end")
            val = getattr(c, key, 0)
            if val:
                e.insert(0, str(val))
        e = self.ch["devices"]
        e.delete(0, "end")
        e.insert(0, "、".join(c.devices or []))
        self.cb_ch_kind.set(config.CHANNEL_TEXT.get(c.kind, c.kind))
        self.cb_ch_status.set(config.CHANNEL_STATE_TEXT.get(c.status, c.status))
        for key, v in self.ch_checks.items():
            v.set(bool(getattr(c, key, False)))

    def channel_save(self):
        code = self.ch["code"].get().strip()
        if not code:
            messagebox.showwarning("缺编码", "通道编码必填（如 ACC-A01）")
            return
        ch = self.store.get_channel(code=code) or models.Channel(code=code)
        ch.name = self.ch["name"].get().strip()
        ch.site = self.ch["site"].get().strip()
        ch.kind = label_to_key(config.CHANNEL_TEXT, self.cb_ch_kind.get(), config.CH_ACCESSIBLE)
        ch.status = label_to_key(config.CHANNEL_STATE_TEXT, self.cb_ch_status.get(), config.ST_OK)
        ch.location = self.ch["location"].get().strip()
        ch.width_mm = to_int(self.ch["width_mm"].get(), 0)
        ch.turn_radius_mm = to_int(self.ch["turn_radius_mm"].get(), 0)
        ch.counter_height_mm = to_int(self.ch["counter_height_mm"].get(), 0)
        raw_dev = self.ch["devices"].get().replace("，", ",").replace("、", ",")
        ch.devices = [x.strip() for x in raw_dev.split(",") if x.strip()]
        for key, v in self.ch_checks.items():
            setattr(ch, key, bool(v.get()))
        if ch.kind == config.CH_ACCESSIBLE and not ch.counter_height_mm and ch.low_counter:
            ch.counter_height_mm = config.LOW_COUNTER_MAX_MM
        if not ch.site:
            messagebox.showwarning("缺考点", "考点必填（配置率的分母按 settings.sites 算）")
            return
        if ch.kind not in config.CHANNEL_KINDS:
            messagebox.showwarning("类型非法", "通道类型必须是 智能/人工/无障碍 之一")
            return
        ch.source = ch.source or config.MANUAL_SOURCE
        self.store.ensure_site(ch.site)
        ch.last_check_at = models.now_ts()
        ch.last_check_epoch = models.now_epoch()
        self.store.save_channel(ch)

        v = access.validate_channel(ch)
        self.store.log("登记/修改通道",
                       "%s %s 考点=%s 校验=%s" % (ch.code, ch.kind, ch.site,
                                                "合规" if v["ok"] else
                                                "error %d 项" % len(v["errors"])),
                       self.cfg.get("operator", ""))
        self.refresh_channels()
        if v["ok"]:
            warns = "；".join(i["text"] for i in v.get("warns") or []) or "无"
            messagebox.showinfo("已保存且合规", "通道 %s 校验通过。\n建议整改项（warn）：%s"
                                % (ch.code, warns))
        else:
            lines = ["%s 已保存，但**有 %d 项 error 必须整改**（现场就是要拿这张清单去改）："
                     % (ch.code, len(v["errors"])), ""]
            for i in v["errors"]:
                lines.append("· [%s] %s" % (i["code"], i["text"]))
            if v.get("warns"):
                lines += ["", "建议整改（warn）："]
                lines += ["· %s" % i["text"] for i in v["warns"]]
            lines += ["", "保存已写明：本条通道尚未合规，报告里会按 error 列出。"]
            messagebox.showwarning("已保存但未合规", "\n".join(lines))
        self._log("通道 %s 保存：%s" % (ch.code, "合规" if v["ok"] else
                                      "error %d 项" % len(v["errors"])))

    def channel_mark_check(self):
        sel = self.tv_chan.selection()
        if not sel:
            messagebox.showinfo("先选通道", "在表格里点一条通道，再登记现场检查时间")
            return
        code = self.tv_chan.item(sel[0], "values")[0]
        ch = self.store.get_channel(code=code)
        if ch is None:
            return
        ch.last_check_at = models.now_ts()
        ch.last_check_epoch = models.now_epoch()
        self.store.save_channel(ch)
        self.store.log("通道现场检查", "%s 记录检查时间" % code, self.cfg.get("operator", ""))
        self.refresh_channels()
        self._log("通道 %s 现场检查时间已登记（%s）" % (code, ch.last_check_at))

    def channel_check_site(self):
        sites = config.sites(self.cfg)
        if not sites:
            messagebox.showinfo("无考点", "settings 里没有考点清单，先到设置或建档时写考点")
            return
        lines = []
        all_ok = True
        for s in sites:
            st = access.site_status(self.store, s)
            all_ok = all_ok and st["ok"]
            if st["ok"]:
                lines.append("✔ %s：三通道并行已配齐（可用 智能 %d / 人工 %d / 无障碍 %d）"
                             % (s, st["usable"].get(config.CH_SMART, 0),
                                st["usable"].get(config.CH_MANUAL, 0),
                                st["usable"].get(config.CH_ACCESSIBLE, 0)))
            else:
                lines.append("✘ %s：缺 %s" % (s, "、".join(st["missing_text"]) or "-"))
                for t in st["channel_errors"][:8]:
                    lines.append("    · %s" % t)
            lines.append("    替代核验通道：%s"
                         % ("可用" if st["alt_available"] else "不可用（必须先恢复通道）"))
        self.store.log("三通道并行检查", "；".join(lines[:6])[:400], self.cfg.get("operator", ""))
        self.refresh_overview()
        messagebox.showinfo("三通道并行检查（access.site_status）",
                            ("全部考点已配齐\n\n" if all_ok else "存在缺口，需整改\n\n")
                            + "\n".join(lines))

    # =====================================================================
    # ③ 特殊考生档案
    # =====================================================================
    def _build_profiles(self):
        f = self.tab_profiles
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1300,
                 text="两条不可让步的规则：① 未取得**单独同意**的考生——不采集人脸，"
                      "直接安排人工核验通道，**绝不因为设备把人挡在门外**；"
                      "② 措施「已落实」必须填责任人、时间，「不予」必须写理由，否则报告里的落实率站不住。"
                      "证件号在本界面与导出中一律脱敏（只留后 4 位）。").pack(
            fill="x", padx=8, pady=(6, 2))

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.LabelFrame(body, text=" 特殊考生档案（store.list_profiles）")
        left.pack(side="left", fill="both", expand=True)
        self.tv_pf = self._tree(left, [("档案号", 110), ("考生(脱敏)", 110), ("姓名", 80),
                                       ("考场", 60), ("座位", 55), ("考点", 90),
                                       ("需求", 220), ("建议通道", 110), ("单独同意", 100),
                                       ("证明", 60), ("来源", 80), ("状态", 60)], height=12)
        self.tv_pf.tag_configure("noconsent", foreground=C_WARN)
        self.tv_pf.tag_configure("demo", foreground=C_TAG)
        self.tv_pf.bind("<<TreeviewSelect>>", lambda e: self.profile_pick())
        pbtn = tk.Frame(left)
        pbtn.pack(fill="x", pady=2)
        tk.Button(pbtn, text="➕ 新增档案", bg="#c8e6c9",
                  command=self.profile_dialog).pack(side="left")
        tk.Button(pbtn, text="🔄 刷新", command=self.refresh_profiles).pack(side="left", padx=6)

        right = tk.LabelFrame(body, text=" 保障措施落实（选中档案；已落实必填责任人、不予必写理由）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.var_pf_now = tk.StringVar(value="未选中档案")
        tk.Label(right, textvariable=self.var_pf_now, font=FONT_B, fg=C_TAG,
                 anchor="w").pack(fill="x", padx=6, pady=2)
        self.tv_acc = self._tree(right, [("措施", 250), ("状态", 110), ("责任人/岗位", 110),
                                         ("落实位置", 110), ("落实时间", 140), ("说明", 180)],
                                 height=10)
        self.tv_acc.tag_configure("ok", foreground=C_OK)
        self.tv_acc.tag_configure("bad", foreground=C_BAD)
        self.tv_acc.tag_configure("warn", foreground=C_WARN)
        abtn = tk.Frame(right)
        abtn.pack(fill="x", pady=2)
        self.acc_owner = tk.Entry(abtn, width=12, font=FONT)
        self._label(abtn, "责任人").pack(side="left")
        self.acc_owner.pack(side="left", padx=(2, 8))
        self.acc_place = tk.Entry(abtn, width=10, font=FONT)
        self._label(abtn, "位置").pack(side="left")
        self.acc_place.pack(side="left", padx=(2, 8))
        self.acc_note = tk.Entry(abtn, width=16, font=FONT)
        self._label(abtn, "说明/不予理由").pack(side="left")
        self.acc_note.pack(side="left", padx=(2, 8))
        abtn2 = tk.Frame(right)
        abtn2.pack(fill="x", pady=2)
        tk.Button(abtn2, text="✔ 置为已落实", bg="#c8e6c9",
                  command=lambda: self.acc_set(config.ACC_PROVIDED)).pack(side="left")
        tk.Button(abtn2, text="✘ 置为不予（须写理由）",
                  command=lambda: self.acc_set(config.ACC_REJECTED)).pack(side="left", padx=6)
        tk.Button(abtn2, text="📝 置为已申请",
                  command=lambda: self.acc_set(config.ACC_REQUESTED)).pack(side="left")
        tk.Button(abtn2, text="🔎 看该考生全部措施",
                  command=self.acc_refresh).pack(side="left", padx=6)

    def refresh_profiles(self):
        rows, tags = [], []
        try:
            profiles = self.store.list_profiles(limit=500)
        except Exception as e:                                     # noqa: BLE001
            self._log("档案读取失败：%s" % str(e)[:120])
            profiles = []
        for p in profiles:
            rows.append((p.profile_id, config.mask_id(p.candidate_id),
                         p.candidate_name or "-", p.room_id or "-", p.seat_no or "-",
                         p.site or "-", config.need_text(p.needs),
                         config.CHANNEL_TEXT.get(p.channel_hint, p.channel_hint or "-"),
                         "已取得" if p.consent else "未取得（走人工通道）",
                         "已核验" if p.cert_verified else ("待核验" if p.cert_no else "-"),
                         source_text(p.source), p.status or "active"))
            if p.source == config.DEMO_SOURCE:
                tags.append("demo")
            elif not p.consent:
                tags.append("noconsent")
            else:
                tags.append("")
        self._fill(self.tv_pf, rows)
        for row, tag in zip(self.tv_pf.get_children(), tags):
            if tag:
                self.tv_pf.item(row, tags=(tag,))

    def _current_profile_id(self):
        sel = self.tv_pf.selection()
        if not sel:
            return ""
        return self.tv_pf.item(sel[0], "values")[0]

    def profile_pick(self):
        pid = self._current_profile_id()
        if not pid:
            return
        p = self.store.get_profile(profile_id=pid)
        if p is None:
            return
        self.var_pf_now.set("档案 %s ｜ %s ｜ 需求：%s ｜ 建议通道：%s ｜ 单独同意：%s"
                            % (p.profile_id, config.mask_id(p.candidate_id),
                               config.need_text(p.needs),
                               config.CHANNEL_TEXT.get(p.channel_hint, p.channel_hint or "-"),
                               "已取得" if p.consent else "未取得（不采集人脸）"))
        self.acc_refresh()

    def acc_refresh(self):
        pid = self._current_profile_id()
        if not pid:
            self._fill(self.tv_acc, [])
            return
        rows, tags = [], []
        for a in self.store.list_accommodations(profile_id=pid):
            rows.append((a.measure_text(), a.state_text(), a.owner or "-", a.place or "-",
                         a.provided_at or "-", a.note or "-"))
            tags.append({config.ACC_PROVIDED: "ok", config.ACC_REJECTED: "bad"}.get(
                a.status, "warn"))
        self._fill(self.tv_acc, rows)
        for row, tag in zip(self.tv_acc.get_children(), tags):
            self.tv_acc.item(row, tags=(tag,))

    def refresh_accommodations(self):
        self.acc_refresh()

    def acc_set(self, status):
        pid = self._current_profile_id()
        if not pid:
            messagebox.showinfo("先选档案", "在左边档案表里点一位考生，再操作措施")
            return
        sel = self.tv_acc.selection()
        if not sel:
            messagebox.showinfo("先选措施", "在措施表里点一项措施")
            return
        measure_text = self.tv_acc.item(sel[0], "values")[0]
        measure = label_to_key(config.MEASURE_TEXT, measure_text, "")
        if measure not in config.MEASURE_KINDS:
            messagebox.showwarning("措施无法识别", "请重新选择措施行：%s" % measure_text)
            return
        ok, res = access.set_measure_status(
            self.store, pid, measure, status, self.cfg.get("operator", ""),
            owner=self.acc_owner.get().strip(), place=self.acc_place.get().strip(),
            note=self.acc_note.get().strip())
        if not ok:
            messagebox.showwarning("没提交成功", str(res))
            return
        self.acc_refresh()
        self.refresh_overview()
        messagebox.showinfo("已留痕", "措施「%s」-> %s（已写审计）"
                            % (config.measure_text(measure),
                               config.ACC_STATE_TEXT.get(status, status)))
        self._log("措施 %s -> %s（档案 %s）" % (measure, status, pid))

    def profile_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("新增特殊考生无障碍档案")
        win.geometry("880x640")
        win.transient(self.root)

        tk.Label(win, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=840,
                 text="建档即触发保障措施推荐（access.recommend_measures）；"
                      "「单独同意」是敏感信息处理的开关：不勾选 -> 不采集人脸、"
                      "自动安排人工核验通道、**绝不拒绝入场**。").pack(fill="x", padx=8, pady=6)

        body = tk.Frame(win)
        body.pack(fill="both", expand=True, padx=8)
        ent = {}
        r1 = tk.Frame(body)
        r1.pack(fill="x", pady=3)
        for title, key, width in (("考生标识(身份证/准考证)", "candidate_id", 22),
                                  ("准考证号", "ticket_no", 16), ("姓名", "candidate_name", 12)):
            self._label(r1, title).pack(side="left")
            e = tk.Entry(r1, width=width, font=FONT)
            e.pack(side="left", padx=(2, 8))
            ent[key] = e
        r2 = tk.Frame(body)
        r2.pack(fill="x", pady=3)
        for title, key, width in (("考点", "site", 12), ("考场", "room_id", 8),
                                  ("座位", "seat_no", 6), ("证明编号(加密落库)", "cert_no", 16)):
            self._label(r2, title).pack(side="left")
            e = tk.Entry(r2, width=width, font=FONT)
            e.insert(0, config.sites(self.cfg)[0] if key == "site"
                     and config.sites(self.cfg) else "")
            e.pack(side="left", padx=(2, 8))
            ent[key] = e

        needs_frame = tk.LabelFrame(body, text=" 需求类型（多选，config.NEED_KINDS）")
        needs_frame.pack(fill="x", pady=6)
        need_vars = {}
        for i, n in enumerate(config.NEED_KINDS):
            v = tk.BooleanVar(value=False)
            need_vars[n] = v
            tk.Checkbutton(needs_frame, text=config.NEED_TEXT[n], variable=v,
                           font=FONT_S).grid(row=i // 2, column=i % 2, sticky="w", padx=4)

        r3 = tk.Frame(body)
        r3.pack(fill="x", pady=3)
        self._label(r3, "需求说明（选「其他」必填）").pack(side="left")
        e_detail = tk.Entry(r3, width=60, font=FONT)
        e_detail.pack(side="left", padx=(2, 8))
        ent["need_detail"] = e_detail
        r4 = tk.Frame(body)
        r4.pack(fill="x", pady=3)
        self._label(r4, "紧急联系人/陪考人(加密)").pack(side="left")
        e_contact = tk.Entry(r4, width=18, font=FONT)
        e_contact.pack(side="left", padx=(2, 8))
        ent["contact"] = e_contact
        self._label(r4, "建档备注").pack(side="left")
        e_note = tk.Entry(r4, width=34, font=FONT)
        e_note.pack(side="left", padx=(2, 8))
        ent["note"] = e_note

        consent_v = tk.BooleanVar(value=False)
        cert_v = tk.BooleanVar(value=False)
        r5 = tk.Frame(body)
        r5.pack(fill="x", pady=3)
        tk.Checkbutton(r5, text="已取得单独同意（人脸等敏感信息处理）", variable=consent_v,
                       font=FONT_B).pack(side="left")
        tk.Checkbutton(r5, text="医疗/残疾证明已核验", variable=cert_v,
                       font=FONT_S).pack(side="left", padx=12)
        hint = tk.StringVar(value="当前：未勾选单独同意 → 只走人工通道、不采集人脸、"
                                  "绝不拒绝入场（%s）" % config.EQUIVALENCE_NOTE)
        tk.Label(body, textvariable=hint, font=FONT_S, fg=C_WARN,
                 anchor="w", justify="left", wraplength=840).pack(fill="x", pady=2)

        def on_consent():
            if consent_v.get():
                hint.set("当前：已取得单独同意 → 可按建议通道走智能核验（仍保留人工兜底：%s）"
                         % config.EQUIVALENCE_NOTE)
            else:
                hint.set("当前：未勾选单独同意 → 只走人工通道、不采集人脸、"
                         "绝不拒绝入场（%s）" % config.EQUIVALENCE_NOTE)
        consent_v.trace_add("write", lambda *a: on_consent())

        btns = tk.Frame(win)
        btns.pack(fill="x", pady=8, padx=8)

        def submit():
            needs = [n for n, v in need_vars.items() if v.get()]
            if not needs:
                messagebox.showwarning("缺需求", "至少选择 1 项特殊需求（否则无法配置替代通道）",
                                       parent=win)
                return
            payload = {k: e.get().strip() for k, e in ent.items()}
            payload.update({"needs": needs, "consent": bool(consent_v.get()),
                            "cert_verified": bool(cert_v.get()),
                            "source": config.MANUAL_SOURCE,
                            "owner": self.cfg.get("operator", "")})
            ok, res, extra = access.add_profile(self.store, payload,
                                                self.cfg.get("operator", ""))
            if not ok:
                messagebox.showwarning("建档失败", str(res), parent=win)
                return
            if (extra or {}).get("already"):
                messagebox.showinfo("已有生效档案",
                                    "%s\n（如需修改，请在数据库中改档案或在原档案上更新）"
                                    % res, parent=win)
            else:
                msg = ["已建档：%s" % res.profile_id,
                       "建议通道：%s" % config.CHANNEL_TEXT.get(res.channel_hint,
                                                            res.channel_hint),
                       "自动推荐措施 %d 项：%s" % (len(res.measures),
                                              config.measures_text(res.measures))]
                msg += ["", "提示："] + ["· " + w for w in (extra or {}).get("warn") or []]
                if not res.consent:
                    msg += ["· 未勾单独同意：不采集人脸、只走人工核验通道、"
                            "**任何情况下不得因此拒绝考生入场**"]
                messagebox.showinfo("建档完成", "\n".join(msg), parent=win)
            self._log("建档 %s 需求=%s 通道=%s"
                      % (config.mask_id(payload.get("candidate_id", "")),
                         config.need_text(needs),
                         config.CHANNEL_TEXT.get(res.channel_hint, res.channel_hint)
                         if not (extra or {}).get("already") else "已有档案"))
            self.refresh_profiles()
            self.refresh_overview()
            win.destroy()

        tk.Button(btns, text="💾 建档", bg="#c8e6c9", width=14, command=submit).pack(side="left")
        tk.Button(btns, text="取消", width=10, command=win.destroy).pack(side="left", padx=8)

    # =====================================================================
    # ④ 替代核验
    # =====================================================================
    def _build_altverify(self):
        f = self.tab_altverify
        banner = tk.Label(f, anchor="w", justify="left", font=FONT_B, fg="white",
                          bg="#b3261e", wraplength=1320,
                          text="  %s  " % config.EQUIVALENCE_NOTE)
        banner.pack(fill="x", padx=8, pady=(6, 4))
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1320,
                 text="硬规则：① 双人见证——见证人不得与主检人相同；"
                      "② 双证比对至少 2 项「一致」且必须含身份证/准考证/护照原件；"
                      "③ 双证不通过 -> 结论是「转考务处置」，**不是**设备拒绝入场；"
                      "④ 技术原因启用人工核验会自动登记受阻事件，指标要求「因技术原因未能入场 0 起」。"
                      "无档案考生也能当场核验（现场补登最小档案），绝不因未登记而挡人。"
                 ).pack(fill="x", padx=8, pady=(0, 4))

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=2)
        left = tk.LabelFrame(body, text=" 人工替代核验录入（verify.alt_verify）")
        left.pack(side="left", fill="both", expand=True)
        self.av = {}
        g1 = tk.Frame(left)
        g1.pack(fill="x", padx=6, pady=2)
        for title, key, width in (("考生标识", "candidate_id", 18), ("姓名", "candidate_name", 10),
                                  ("考场", "room_id", 6), ("座位", "seat_no", 5)):
            self._label(g1, title).pack(side="left")
            self._entry(g1, key, self.av, width)
            self.av[key].pack(side="left", padx=(2, 8))
        g2 = tk.Frame(left)
        g2.pack(fill="x", padx=6, pady=2)
        self._label(g2, "核验方式").pack(side="left")
        self.cb_av_mode = self._combo(g2, "mode", self.av,
                                      [config.ALT_MODE_TEXT[k] for k in config.ALT_MODES], 20)
        self._label(g2, "主检人").pack(side="left")
        self._entry(g2, "inspector", self.av, 12).pack(side="left", padx=(2, 8))
        self.av["inspector"].insert(0, self.cfg.get("operator", ""))
        self._label(g2, "见证人(须不同人)").pack(side="left")
        self._entry(g2, "witness", self.av, 12).pack(side="left", padx=(2, 8))
        self._label(g2, "耗时秒").pack(side="left")
        self._entry(g2, "elapsed_sec", self.av, 6).pack(side="left", padx=(2, 8))
        self.av["elapsed_sec"].insert(0, "60")
        self._label(g2, "机位/终端").pack(side="left")
        self._entry(g2, "device_code", self.av, 10).pack(side="left", padx=(2, 8))

        doc_frame = tk.LabelFrame(left, text=" 双证比对（4 类证件；至少 2 项一致且含身份证/准考证/护照）")
        doc_frame.pack(fill="x", padx=6, pady=4)
        self.doc_vars = {}
        for i, doc in enumerate(config.DOC_KINDS):
            tk.Label(doc_frame, text=config.DOC_TEXT[doc], font=FONT_S).grid(
                row=i // 2, column=(i % 2) * 2, sticky="e", padx=4, pady=2)
            cb = ttk.Combobox(doc_frame, width=12, state="readonly",
                              values=[config.DOC_STATE_TEXT[s] for s in config.DOC_STATES])
            cb.set(config.DOC_STATE_TEXT[config.DOC_MATCHED if doc in
                                         (config.DOC_ID_CARD, config.DOC_TICKET)
                                         else config.DOC_MISSING])
            cb.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=4)
            self.doc_vars[doc] = cb

        g3 = tk.Frame(left)
        g3.pack(fill="x", padx=6, pady=2)
        self._label(g3, "触发原因").pack(side="left")
        self._entry(g3, "reason", self.av, 30).pack(side="left", padx=(2, 8))
        g4 = tk.Frame(left)
        g4.pack(fill="x", padx=6, pady=2)
        self.var_tech_reason = tk.BooleanVar(value=False)
        tk.Checkbutton(g4, text="技术原因触发（设备/算法/画质）→ 自动登记受阻事件并按红级上报 03",
                       variable=self.var_tech_reason, font=FONT_S).pack(side="left")
        self.var_av_notify = tk.BooleanVar(value=True)
        tk.Checkbutton(g4, text="上报 03 智能终端（橙色）", variable=self.var_av_notify,
                       font=FONT_S).pack(side="left", padx=10)
        self.var_av_symptom = tk.StringVar(value=config.BLOCK_SYMPTOMS["face_fail"])
        ttk.Combobox(g4, width=26, state="readonly", textvariable=self.var_av_symptom,
                     values=[config.BLOCK_SYMPTOMS[k] for k in config.BLOCK_SYMPTOMS]
                     ).pack(side="left", padx=4)
        g5 = tk.Frame(left)
        g5.pack(fill="x", padx=6, pady=2)
        self._label(g5, "结论").pack(side="left")
        self.cb_av_outcome = self._combo(
            g5, "outcome", self.av, [config.ALT_OUTCOME_TEXT[k] for k in config.ALT_OUTCOMES], 22)
        self.var_av_block = tk.BooleanVar(value=True)
        tk.Checkbutton(g5, text="技术原因时登记受阻事件并尽量闭环", variable=self.var_av_block,
                       font=FONT_S).pack(side="left", padx=6)
        ab = tk.Frame(left)
        ab.pack(fill="x", padx=6, pady=4)
        tk.Button(ab, text="✅ 提交替代核验（后台执行，不卡界面）", bg="#c8e6c9",
                  command=self.altverify_submit).pack(side="left")
        tk.Button(ab, text="🧹 清空", command=self.altverify_clear).pack(side="left", padx=6)
        tk.Button(ab, text="⚠ 登记受阻事件",
                  command=self.tech_block_dialog).pack(side="left", padx=6)

        right = tk.LabelFrame(body, text=" 替代核验历史与统计（store.list_alt_verifies / alt_verify_stats）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.var_av_stats = tk.StringVar(value="-")
        tk.Label(right, textvariable=self.var_av_stats, font=FONT_S, fg=C_DIM,
                 anchor="w", justify="left", wraplength=600).pack(fill="x", padx=6)
        self.tv_av = self._tree(right, [("时间", 140), ("考生(脱敏)", 100), ("方式", 130),
                                        ("双证比对", 220), ("主检", 80), ("见证", 80),
                                        ("结论", 110), ("耗时s", 55), ("超时", 50),
                                        ("等效", 50), ("触发原因", 200)], height=10)
        self.tv_av.tag_configure("bad", foreground=C_BAD)
        self.tv_av.tag_configure("warn", foreground=C_WARN)

        blk = tk.LabelFrame(right, text=" 受阻事件（因技术原因入场受阻；结案 denied 即计入「未能入场」指标）")
        blk.pack(fill="both", expand=True, pady=(6, 0))
        self.tv_block = self._tree(blk, [("编号", 110), ("时间", 140), ("考生(脱敏)", 100),
                                         ("症状", 190), ("设备", 90), ("上报", 60),
                                         ("结案", 130), ("等待s", 60), ("处置", 220)],
                                   height=8)
        self.tv_block.tag_configure("bad", foreground=C_BAD)
        self.tv_block.tag_configure("open", foreground=C_WARN)
        self.tv_block.bind("<<TreeviewSelect>>", lambda e: self.block_pick())
        bb = tk.Frame(blk)
        bb.pack(fill="x", pady=2)
        self._label(bb, "结案为").pack(side="left")
        self.cb_block_final = self._combo(
            bb, "final", {}, [config.FINAL_TEXT[k] for k in config.FINAL_RESULTS], 24)
        tk.Button(bb, text="✔ 结案（后台）", command=self.block_resolve).pack(side="left")
        tk.Button(bb, text="🔄 刷新", command=self.refresh_tech_blocks).pack(side="left", padx=6)
        tk.Label(bb, text="注意：denied（未能入场）会红字计入硬指标，必须追责整改",
                 font=FONT_S, fg=C_BAD).pack(side="left", padx=8)

    def altverify_clear(self):
        for key in ("candidate_id", "candidate_name", "room_id", "seat_no", "reason",
                    "device_code"):
            e = self.av[key]
            e.delete(0, "end")
        self.av["inspector"].delete(0, "end")
        self.av["inspector"].insert(0, self.cfg.get("operator", ""))
        self.av["witness"].delete(0, "end")
        self.av["elapsed_sec"].delete(0, "end")
        self.av["elapsed_sec"].insert(0, "60")
        self.var_tech_reason.set(False)

    def refresh_altverify(self):
        rows, tags = [], []
        try:
            avs = self.store.list_alt_verifies(limit=200)
            st = self.store.alt_verify_stats()
        except Exception as e:                                     # noqa: BLE001
            self._log("替代核验读取失败：%s" % str(e)[:120])
            avs, st = [], {}
        for a in avs:
            rows.append((a.ts, config.mask_id(a.candidate_id),
                         config.ALT_MODE_TEXT.get(a.mode, a.mode), a.docs_text(),
                         a.inspector or "-", a.witness or "-",
                         config.ALT_OUTCOME_TEXT.get(a.outcome, a.outcome),
                         "%.0f" % (a.elapsed_sec or 0), "超时" if a.overdue else "-",
                         "是" if a.equivalence else "否", (a.reason or "")[:60]))
            tags.append("bad" if a.outcome == config.ALT_FAILED else
                        ("warn" if a.overdue else ""))
        self._fill(self.tv_av, rows)
        for row, tag in zip(self.tv_av.get_children(), tags):
            if tag:
                self.tv_av.item(row, tags=(tag,))
        if st:
            self.var_av_stats.set(
                "累计 %d 次：通过 %d、不通过（转考务）%d、待核实 %d；超时(>%d秒) %d 次；"
                "平均耗时 %.0f 秒；技术原因触发 %d 次；同等效力标记 %d 次；演练记录 %d 次\n%s"
                % (st["total"], st["passed"], st["failed"], st["deferred"],
                   config.ALT_VERIFY_SLA_SEC, st["overdue"], st["avg_sec"],
                   st["tech_reason"], st["equivalence_ok"], st["drill"],
                   config.EQUIVALENCE_NOTE))

    def altverify_submit(self):
        payload = {
            "candidate_id": self.av["candidate_id"].get().strip(),
            "candidate_name": self.av["candidate_name"].get().strip(),
            "room_id": self.av["room_id"].get().strip(),
            "seat_no": self.av["seat_no"].get().strip(),
            "device_code": self.av["device_code"].get().strip(),
            "mode": label_to_key(config.ALT_MODE_TEXT, self.cb_av_mode.get(),
                                 config.ALT_MANUAL_DOC),
            "inspector": self.av["inspector"].get().strip(),
            "witness": self.av["witness"].get().strip(),
            "elapsed_sec": to_float(self.av["elapsed_sec"].get(), 0.0),
            "reason": self.av["reason"].get().strip(),
            "tech_reason": bool(self.var_tech_reason.get()),
            "outcome": label_to_key(config.ALT_OUTCOME_TEXT, self.cb_av_outcome.get(), ""),
            "docs": {d: label_to_key(config.DOC_STATE_TEXT, cb.get(), config.DOC_MISSING)
                     for d, cb in self.doc_vars.items()},
            "notify": True,
            "symptom": label_to_key(config.BLOCK_SYMPTOMS, self.var_av_symptom.get(),
                                    "face_fail"),
            "source": config.MANUAL_SOURCE,
        }
        if not payload["candidate_id"]:
            messagebox.showwarning("缺考生标识", "考生标识（身份证/准考证号）必填")
            return
        if not payload["inspector"]:
            messagebox.showwarning("缺主检人", "必须填主检人（谁核验谁负责）")
            return
        if not payload["witness"]:
            messagebox.showwarning("缺见证人", "双人见证未满足：必须填见证人（不得与主检人相同）")
            return
        if payload["witness"] == payload["inspector"]:
            messagebox.showwarning("见证无效", "见证人不得与主检人相同（自己见证自己不算见证）")
            return
        ok_doc, matched, missing_required, doc_text = verify_mod.docs_check(payload["docs"])
        create_block = bool(self.var_av_block.get()) if payload["tech_reason"] else None
        operator = self.cfg.get("operator", "")

        def work():
            return verify_mod.alt_verify(self.store, payload, operator,
                                         notifier=self._notifier(),
                                         create_block=create_block)

        def done(res, err):
            if err or not res:
                messagebox.showwarning("替代核验异常", err or "无返回")
                return
            ok, obj, info = res
            self.refresh_altverify()
            self.refresh_overview()
            if not ok:
                messagebox.showwarning("替代核验未通过校验（未落库）", str(obj))
                return
            lines = ["替代核验已留痕：%s（%s）" % (obj.av_id, obj.ts),
                     "方式：%s ｜ 结论：%s"
                     % (config.ALT_MODE_TEXT.get(obj.mode, obj.mode),
                        config.ALT_OUTCOME_TEXT.get(obj.outcome, obj.outcome)),
                     "双证比对：%s" % info.get("docs_text", ""),
                     "主检 %s / 见证 %s；耗时 %.0f 秒%s"
                     % (obj.inspector, obj.witness or "-", obj.elapsed_sec,
                        "（**超时**，进整改清单）" if obj.overdue else ""),
                     "替代通道：%s" % info.get("alt_channel_reason", "-"),
                     "制度声明：%s" % info.get("equivalence", config.EQUIVALENCE_NOTE)]
            if info.get("profile_created"):
                lines.append("· 该考生原先没有档案：已现场补登最小档案（绝不因未登记挡人）")
            for w in info.get("warn") or []:
                lines.append("· 提示：%s" % w)
            b = info.get("block")
            if b:
                lines.append("· 受阻事件 %s：%s -> %s（等待 %.0f 秒）"
                             % (b.get("block_id"), config.BLOCK_SYMPTOMS.get(
                                 b.get("symptom"), b.get("symptom")),
                                config.FINAL_TEXT.get(b.get("final_result"),
                                                      b.get("final_result") or "未结案"),
                                float(b.get("waited_sec") or 0)))
            self.refresh_tech_blocks()
            messagebox.showinfo("替代核验完成", "\n".join(lines))
            self._log("替代核验 %s 结论=%s 双证=%s" % (config.mask_id(payload["candidate_id"]),
                                                    obj.outcome, info.get("docs_text", "")))
            if payload["tech_reason"]:
                self._log("技术原因替代核验：已按红级上报 03（不可达则进 outbox，恢复后补发）")

        self._run_bg("替代核验", work, done, busy_text="后台执行替代核验…")

    def refresh_tech_blocks(self):
        rows, tags = [], []
        try:
            blocks = self.store.list_tech_blocks(limit=200)
        except Exception as e:                                     # noqa: BLE001
            self._log("受阻事件读取失败：%s" % str(e)[:120])
            blocks = []
        for b in blocks:
            rows.append((b.block_id, b.raised_at, config.mask_id(b.candidate_id),
                         b.symptom_text(), b.device_code or "-",
                         "已上报" if b.escalated else "未上报",
                         b.final_text() if b.resolved else "未结案",
                         "%.0f" % (b.waited_sec or 0), (b.resolution or "-")[:40]))
            if b.resolved and b.final_result == config.FINAL_DENIED:
                tags.append("bad")
            elif not b.resolved:
                tags.append("open")
            else:
                tags.append("")
        self._fill(self.tv_block, rows)
        for row, tag in zip(self.tv_block.get_children(), tags):
            if tag:
                self.tv_block.item(row, tags=(tag,))

    def block_pick(self):
        sel = self.tv_block.selection()
        if not sel:
            return
        bid = self.tv_block.item(sel[0], "values")[0]
        b = self.store.get_tech_block(bid)
        if b is None:
            return
        self.cb_block_final.set(config.FINAL_TEXT.get(
            b.final_result or config.FINAL_ENTERED_MANUAL,
            config.FINAL_TEXT[config.FINAL_ENTERED_MANUAL]))
        self.var_status.set("受阻事件 %s：%s（%s）"
                            % (b.block_id, b.symptom_text(),
                               "已结案" if b.resolved else "未结案"))

    def block_resolve(self):
        sel = self.tv_block.selection()
        if not sel:
            messagebox.showinfo("先选事件", "在受阻事件表里点一条，再选结案类型")
            return
        bid = self.tv_block.item(sel[0], "values")[0]
        final = label_to_key(config.FINAL_TEXT, self.cb_block_final.get(),
                             config.FINAL_ENTERED_MANUAL)
        if final == config.FINAL_DENIED and not messagebox.askyesno(
                "确认：未能入场？",
                "把该事件结案为「未能入场」会直接计入硬指标"
                "（要求 0 起），并留审计、按红级上报 03。\n确定这是事实吗？"):
            return
        resolution = self.av["reason"].get().strip()
        if final == config.FINAL_DENIED:
            resolution = resolution or "未能入场（须追责整改）"
        else:
            resolution = resolution or "走替代通道/人工核验后准予入场"
        operator = self.cfg.get("operator", "")

        def work():
            return verify_mod.resolve_tech_block(self.store, bid, final, resolution,
                                                 operator, notifier=self._notifier())

        def done(res, err):
            if err:
                messagebox.showwarning("结案异常", err)
                return
            if res is None:
                messagebox.showwarning("结案失败", "找不到受阻事件或结案类型非法：%s" % bid)
                return
            self.refresh_tech_blocks()
            self.refresh_overview()
            note = "（**计入「因技术原因未能入场」指标**，必须追责整改）" \
                if res.final_result == config.FINAL_DENIED else ""
            messagebox.showinfo("已结案", "%s -> %s，等待 %.0f 秒%s"
                                % (res.block_id, res.final_text(), res.waited_sec, note))
            self._log("受阻事件结案 %s -> %s" % (res.block_id, res.final_result))

        self._run_bg("受阻事件结案", work, done, busy_text="后台结案…")

    def tech_block_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("登记「因技术原因入场受阻」事件")
        win.geometry("720x460")
        win.transient(self.root)
        tk.Label(win, anchor="w", justify="left", font=FONT_S, fg=C_BAD, wraplength=680,
                 text="受阻事件是硬指标「因技术原因未能入场 0 起」的分子分母来源；"
                      "登记后按红级推 03 智能终端（不可达则进本地 outbox，恢复后补发，事件不丢）。"
                 ).pack(fill="x", padx=8, pady=6)
        ent = {}
        for title, key in (("考生标识", "candidate_id"), ("姓名", "candidate_name"),
                           ("考点", "site"), ("考场", "room_id"), ("座位", "seat_no"),
                           ("设备/机位编码", "device_code"), ("当时通道编码", "channel_code")):
            r = tk.Frame(win)
            r.pack(fill="x", padx=8, pady=2)
            self._label(r, title).pack(side="left")
            e = tk.Entry(r, width=40, font=FONT)
            if key == "site":
                e.insert(0, config.sites(self.cfg)[0] if config.sites(self.cfg) else "")
            e.pack(side="left", padx=(2, 8))
            ent[key] = e
        r = tk.Frame(win)
        r.pack(fill="x", padx=8, pady=2)
        self._label(r, "症状").pack(side="left")
        cb_sym = ttk.Combobox(r, width=34, state="readonly",
                              values=[config.BLOCK_SYMPTOMS[k] for k in config.BLOCK_SYMPTOMS])
        cb_sym.current(0)
        cb_sym.pack(side="left", padx=(2, 8))
        tk.Label(win, text="详情（写给事后复盘看）", font=FONT_S).pack(anchor="w", padx=8)
        txt = tk.Text(win, height=5, font=FONT_S)
        txt.pack(fill="x", padx=8, pady=2)
        var_notify = tk.BooleanVar(value=True)
        tk.Checkbutton(win, text="按红级上报 03 智能终端（要求立即人工介入）",
                       variable=var_notify, font=FONT_S).pack(anchor="w", padx=8)
        btns = tk.Frame(win)
        btns.pack(fill="x", padx=8, pady=8)

        def submit():
            cid = ent["candidate_id"].get().strip()
            if not cid:
                messagebox.showwarning("缺考生标识", "受阻事件必须能追到人：填考生标识",
                                       parent=win)
                return
            payload = {k: e.get().strip() for k, e in ent.items()}
            payload["symptom"] = label_to_key(config.BLOCK_SYMPTOMS, cb_sym.get(), "other")
            payload["detail"] = txt.get("1.0", "end").strip()
            payload["notify"] = bool(var_notify.get())
            payload["source"] = config.MANUAL_SOURCE
            operator = self.cfg.get("operator", "")

            def work():
                return verify_mod.raise_tech_block(self.store, payload, operator,
                                                   notifier=self._notifier())

            def done(b, err):
                if err or b is None:
                    messagebox.showwarning("登记失败", err or "无返回", parent=self.root)
                    return
                self.refresh_tech_blocks()
                self.refresh_overview()
                messagebox.showinfo(
                    "已登记", "%s：%s\n上报状态：%s\n（未上报成功的事件已进 outbox，"
                              "恢复后可用「补发待发事件」补发）"
                    % (b.block_id, b.symptom_text(),
                       "已上报 03（告警号 %s）" % b.alert_id if b.escalated else "未上报"),
                    parent=self.root)
                win.destroy()

            self._run_bg("登记受阻事件", work, done, busy_text="后台登记受阻事件…")

        tk.Button(btns, text="登记并上报", bg="#ffcdd2", width=14,
                  command=submit).pack(side="left")
        tk.Button(btns, text="取消", width=10, command=win.destroy).pack(side="left", padx=8)

    # =====================================================================
    # ⑤ 算法公平性
    # =====================================================================
    def _build_fairness(self):
        f = self.tab_fairness
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1320,
                 text="为什么要做：人脸核验在戴眼镜/戴口罩/面部损伤/不同年龄段上的通过率天然有差异，"
                      "差异大而没人知道，特殊考生就会在闸机前反复被拒——这就是技术造成的不公平。"
                      "按分层维度录入「样本数/通过数」，算出组间通过率最大差，与可接受范围"
                      "（默认 %.0f%%）比；超差必须写补偿策略与整改说明，并在下一轮复测看是否收敛。"
                      % (config.fairness_max_gap(self.cfg) * 100)).pack(
            fill="x", padx=8, pady=(6, 2))

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=2)
        left = tk.LabelFrame(body, text=" 分层数据录入（6 个维度，config.DIM_GROUPS）")
        left.pack(side="left", fill="both", expand=True)
        self.fg = {}
        header = tk.Frame(left)
        header.pack(fill="x", padx=6, pady=2)
        for text, width in (("维度", 12), ("分组", 14), ("样本数", 9), ("通过数", 9)):
            tk.Label(header, text=text, font=FONT_B, width=width, anchor="w").pack(side="left")
        for dim in config.FAIRNESS_DIMS:
            box = tk.Frame(left, bd=1, relief="groove")
            box.pack(fill="x", padx=6, pady=2)
            tk.Label(box, text="%s%s" % (config.DIM_TEXT[dim],
                                         "（必测）" if dim in config.FAIRNESS_REQUIRED_DIMS
                                         else ""),
                     font=FONT_B, width=16, anchor="w").pack(side="left", padx=4)
            for gname in config.DIM_GROUPS[dim]:
                cell = tk.Frame(box)
                cell.pack(side="left", padx=6)
                tk.Label(cell, text=gname, font=FONT_S, width=10, anchor="w").pack(side="left")
                e_t = tk.Entry(cell, width=7, font=FONT)
                e_t.pack(side="left")
                e_p = tk.Entry(cell, width=7, font=FONT)
                e_p.pack(side="left", padx=(2, 0))
                self.fg[(dim, gname)] = (e_t, e_p)
        r = tk.Frame(left)
        r.pack(fill="x", padx=6, pady=2)
        self._label(r, "被测算法/模型与版本").pack(side="left")
        self.fa_name = tk.Entry(r, width=14, font=FONT)
        self.fa_name.pack(side="left", padx=(2, 8))
        self.fa_name.insert(0, "分层测试")
        self._label(r, "算法").pack(side="left")
        self.fa_algo = tk.Entry(r, width=20, font=FONT)
        self.fa_algo.pack(side="left", padx=(2, 8))
        self.fa_algo.insert(0, "01 人脸 1:1")
        self._label(r, "当次阈值").pack(side="left")
        self.fa_thr = tk.Entry(r, width=8, font=FONT)
        self.fa_thr.pack(side="left", padx=(2, 8))
        self.fa_thr.insert(0, "0.6")
        r2 = tk.Frame(left)
        r2.pack(fill="x", padx=6, pady=2)
        self._label(r2, "补偿策略").pack(side="left")
        self.cb_fa_action = self._combo(
            r2, "action", {}, [config.FAIRNESS_ADJUST_TEXT[k]
                               for k in config.FAIRNESS_ADJUST_ACTIONS], 26)
        self._label(r2, "整改说明（超差必填）").pack(side="left")
        self.fa_note = tk.Entry(r2, width=46, font=FONT)
        self.fa_note.pack(side="left", padx=(2, 8))
        btns = tk.Frame(left)
        btns.pack(fill="x", padx=6, pady=4)
        tk.Button(btns, text="▶ 运行分层测试（后台）", bg="#c8e6c9",
                  command=self.fairness_run).pack(side="left")
        tk.Button(btns, text="🧪 填入示例样本（每组 30/27）",
                  command=self.fairness_demo_fill).pack(side="left", padx=6)
        tk.Button(btns, text="🧹 清空", command=self.fairness_clear).pack(side="left")
        self.var_fa_result = tk.StringVar(value="尚未运行分层测试")
        tk.Label(left, textvariable=self.var_fa_result, font=FONT_S, fg=C_BAD,
                 anchor="w", justify="left", wraplength=640).pack(fill="x", padx=6, pady=2)

        right = tk.LabelFrame(body, text=" 历史测试与整改趋势（store.list_fairness_runs / fairness.trend）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.tv_fa = self._tree(right, [("时间", 140), ("编号", 110), ("算法", 130),
                                        ("样本", 60), ("分组数", 60), ("最大差", 70),
                                        ("最差分分组", 150), ("结论", 70),
                                        ("补偿策略", 150)], height=8)
        self.tv_fa.tag_configure("ok", foreground=C_OK)
        self.tv_fa.tag_configure("bad", foreground=C_BAD)
        self.tv_fa.bind("<<TreeviewSelect>>", lambda e: self.fairness_pick())
        self.tv_fg = self._tree(right, [("分层", 100), ("分组", 100), ("样本", 60),
                                        ("通过", 60), ("通过率", 80), ("与最高组差", 90),
                                        ("样本是否充足", 100)], height=8)
        self.var_fa_trend = tk.StringVar(value="-")
        tk.Label(right, textvariable=self.var_fa_trend, font=FONT_S, fg=C_DIM,
                 anchor="w", justify="left", wraplength=620).pack(fill="x", padx=6, pady=2)

    def fairness_clear(self):
        for e_t, e_p in self.fg.values():
            e_t.delete(0, "end")
            e_p.delete(0, "end")

    def fairness_demo_fill(self):
        for (dim, gname), (e_t, e_p) in self.fg.items():
            e_t.delete(0, "end")
            e_p.delete(0, "end")
            e_t.insert(0, "30")
            e_p.insert(0, "27")
        self.var_fa_result.set("已填入示例样本（每组 30 样本 / 27 通过）。"
                               "注意：这是**录入示例**，不是真实测试数据，请按现场实测值改写。")

    def refresh_fairness(self):
        rows, tags = [], []
        try:
            runs = self.store.list_fairness_runs(limit=100)
        except Exception as e:                                     # noqa: BLE001
            self._log("公平性记录读取失败：%s" % str(e)[:120])
            runs = []
        for r in runs:
            rows.append((r.ts, r.run_id, r.algorithm or "-", r.total, r.groups_count,
                         "%.2f%%" % (r.max_gap * 100),
                         "%s/%s" % (config.DIM_TEXT.get(r.worst_dim, r.worst_dim or "-"),
                                    r.worst_group or "-"),
                         "达标" if r.passed else "超差", r.action_text()))
            tags.append("ok" if r.passed else "bad")
        self._fill(self.tv_fa, rows)
        for row, tag in zip(self.tv_fa.get_children(), tags):
            self.tv_fa.item(row, tags=(tag,))
        if runs:
            self.fairness_pick(runs[0].run_id)
        try:
            tr = fairness_mod.trend(self.store, limit=6)
        except Exception:                                          # noqa: BLE001
            tr = []
        if tr:
            self.var_fa_trend.set("整改前后对比（组间最大差）：" + " → ".join(
                "%s %.2f%%%s" % (x["ts"][5:16], x["max_gap"] * 100,
                                 "" if x["passed"] else "(超差)") for x in tr))
        else:
            self.var_fa_trend.set("尚无分层测试记录：上线前必须完成分层测试"
                                  "（性别/年龄/眼镜/口罩为必测分层）。")

    def fairness_pick(self, run_id=None):
        if run_id is None:
            sel = self.tv_fa.selection()
            if not sel:
                return
            run_id = self.tv_fa.item(sel[0], "values")[1]
        rows = []
        try:
            for g in self.store.list_fairness_groups(run_id):
                rows.append((config.DIM_TEXT.get(g.dim, g.dim), g.group_name, g.total,
                             g.passed, "%.2f%%" % (g.pass_rate * 100),
                             "%.2f%%" % (g.gap_to_best * 100),
                             "不足（结论仅参考）" if g.small_sample else "充足"))
        except Exception as e:                                     # noqa: BLE001
            self._log("分组明细读取失败：%s" % str(e)[:120])
        self._fill(self.tv_fg, rows)

    def fairness_run(self):
        groups = {}
        for (dim, gname), (e_t, e_p) in self.fg.items():
            total = to_int(e_t.get(), 0)
            if total <= 0:
                continue
            groups.setdefault(dim, {})[gname] = {"total": total,
                                                "passed": to_int(e_p.get(), 0)}
        if not groups:
            messagebox.showinfo("没有数据", "至少填一个分组（样本数 > 0）")
            return
        payload = {
            "groups": groups,
            "name": self.fa_name.get().strip() or "分层测试",
            "algorithm": self.fa_algo.get().strip(),
            "threshold": to_float(self.fa_thr.get(), 0.0),
            "action": label_to_key(config.FAIRNESS_ADJUST_TEXT, self.cb_fa_action.get(),
                                   "none"),
            "action_note": self.fa_note.get().strip(),
            "site": config.sites(self.cfg)[0] if config.sites(self.cfg) else "",
            "source": config.MANUAL_SOURCE,
        }
        operator = self.cfg.get("operator", "")

        def work():
            return fairness_mod.run_fairness(self.store, payload, operator)

        def done(res, err):
            if err or not res:
                messagebox.showwarning("分层测试异常", err or "无返回")
                return
            ok, run, info = res
            if not ok:
                messagebox.showwarning("无法运行分层测试", str(run))
                return
            self.refresh_fairness()
            self.refresh_overview()
            advice = info.get("advice", "")
            warn = info.get("warn") or []
            self.var_fa_result.set(
                "最近一次 %s：样本 %d，组间通过率最大差 %.2f%%（可接受 ≤%.0f%%），"
                "最差分层 %s/%s，结论：%s\n%s\n%s"
                % (run.run_id, run.total, run.max_gap * 100,
                   info.get("max_gap_allowed", 0) * 100,
                   config.DIM_TEXT.get(run.worst_dim, run.worst_dim or "-"),
                   run.worst_group or "-", "达标" if run.passed else "**超差需补偿**",
                   advice, "；".join(warn)))
            messagebox.showinfo("分层测试结论",
                                "组间最大差 %.2f%%（允许 ≤%.0f%%）\n结论：%s\n\n%s%s"
                                % (run.max_gap * 100, info.get("max_gap_allowed", 0) * 100,
                                   "达标" if run.passed else "超差，必须落实补偿策略",
                                   advice,
                                   ("\n\n提醒：\n· " + "\n· ".join(warn)) if warn else ""))
            self._log("分层测试 %s 最大差 %.2f%% 结论=%s"
                      % (run.run_id, run.max_gap * 100, "达标" if run.passed else "超差"))

        self._run_bg("算法公平性分层测试", work, done, busy_text="后台运行分层测试…")

    # =====================================================================
    # ⑥ 演练与台账
    # =====================================================================
    def _build_drill(self):
        f = self.tab_drill
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1320,
                 text="指标口径：替代通道可用性 100%，且**每考期至少演练 1 次**。"
                      "替代通道平时没人走，真出事那天才第一次用，大概率是堵人的；"
                      "所以「发现了问题就必须写整改措施」「不合格必须写重演计划」是硬校验。"
                 ).pack(fill="x", padx=8, pady=(6, 2))

        form = tk.LabelFrame(f, text=" 演练登记（drill.add_drill）")
        form.pack(fill="x", padx=8, pady=4)
        self.dr = {}
        r1 = tk.Frame(form)
        r1.pack(fill="x", padx=6, pady=2)
        self._label(r1, "科目").pack(side="left")
        self.cb_dr_scenario = self._combo(
            r1, "scenario", self.dr, [config.DRILL_SCENARIOS[k] for k in config.DRILL_SCENARIOS], 34)
        self._label(r1, "考点").pack(side="left")
        self._entry(r1, "site", self.dr, 12).pack(side="left", padx=(2, 8))
        self.dr["site"].insert(0, config.sites(self.cfg)[0] if config.sites(self.cfg) else "")
        self._label(r1, "考期").pack(side="left")
        self._entry(r1, "session", self.dr, 12).pack(side="left", padx=(2, 8))
        self.dr["session"].insert(0, str(self.cfg.get("session", "")))
        self._label(r1, "参与人数").pack(side="left")
        self._entry(r1, "participants", self.dr, 6).pack(side="left", padx=(2, 8))
        self.dr["participants"].insert(0, "6")
        self._label(r1, "计划分钟").pack(side="left")
        self._entry(r1, "planned_min", self.dr, 6).pack(side="left", padx=(2, 8))
        self.dr["planned_min"].insert(0, "20")
        self._label(r1, "实际分钟").pack(side="left")
        self._entry(r1, "actual_min", self.dr, 6).pack(side="left", padx=(2, 8))
        self.dr["actual_min"].insert(0, "25")
        r2 = tk.Frame(form)
        r2.pack(fill="x", padx=6, pady=2)
        self._label(r2, "结论").pack(side="left")
        self.cb_dr_result = self._combo(
            r2, "result", self.dr, [config.DRILL_RESULT_TEXT[k] for k in config.DRILL_RESULTS], 22)
        self._label(r2, "见证/组织人").pack(side="left")
        self._entry(r2, "witness", self.dr, 12).pack(side="left", padx=(2, 8))
        self._label(r2, "涉及通道编码（多选，Ctrl 可多选）").pack(side="left")
        lf = tk.Frame(form)
        lf.pack(fill="x", padx=6, pady=2)
        self.lb_dr_ch = tk.Listbox(lf, selectmode="extended", height=4,
                                   font=FONT_S, exportselection=False, width=40)
        self.lb_dr_ch.pack(side="left")
        tk.Button(lf, text="↻ 载入通道台账", command=self.drill_load_channels).pack(
            side="left", padx=6, anchor="n")
        tk.Label(lf, text="（通道编码来自「② 无障碍通道」；全链路演练建议覆盖三类通道）",
                 font=FONT_S, fg=C_DIM).pack(side="left", anchor="n")
        r3 = tk.Frame(form)
        r3.pack(fill="x", padx=6, pady=2)
        tk.Label(r3, text="发现的问题（每行一条）", font=FONT_S).pack(side="left", anchor="n")
        self.txt_dr_issues = tk.Text(r3, width=46, height=3, font=FONT_S)
        self.txt_dr_issues.pack(side="left", padx=(2, 10))
        tk.Label(r3, text="整改措施（有问题必填）", font=FONT_S).pack(side="left", anchor="n")
        self.txt_dr_actions = tk.Text(r3, width=50, height=3, font=FONT_S)
        self.txt_dr_actions.pack(side="left")
        btns = tk.Frame(form)
        btns.pack(fill="x", padx=6, pady=4)
        tk.Button(btns, text="➕ 登记演练", bg="#c8e6c9",
                  command=self.drill_add).pack(side="left")
        tk.Button(btns, text="🔄 刷新", command=self.refresh_drills).pack(side="left", padx=6)
        self.var_dr_avail = tk.StringVar(value="-")
        tk.Label(f, textvariable=self.var_dr_avail, font=FONT_S, fg=C_DIM, anchor="w",
                 justify="left", wraplength=1320).pack(fill="x", padx=8)

        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.LabelFrame(body, text=" 演练记录（store.list_drills）")
        left.pack(side="left", fill="both", expand=True)
        self.tv_dr = self._tree(left, [("时间", 140), ("考点", 90), ("考期", 110),
                                       ("科目", 240), ("参与", 55), ("计划/实际", 90),
                                       ("发现问题", 220), ("整改措施", 220),
                                       ("结论", 130), ("来源", 70)], height=10)
        self.tv_dr.tag_configure("bad", foreground=C_BAD)
        self.tv_dr.tag_configure("warn", foreground=C_WARN)

        right = tk.LabelFrame(body, text=" 逐考点替代通道可用性（drill.drill_availability）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.tv_dravail = self._tree(right, [("考点", 110), ("替代通道", 80),
                                             ("本考期演练", 80), ("不合格", 60),
                                             ("最近一次", 140), ("结论", 90),
                                             ("原因", 300)], height=6)
        self.tv_dravail.tag_configure("ok", foreground=C_OK)
        self.tv_dravail.tag_configure("bad", foreground=C_BAD)

        led = tk.LabelFrame(body, text=" 特殊考生处置台账（ledger.rows 预览，默认脱敏；导出落在 data/ledgers/）")
        led.pack(side="left", fill="both", expand=True, padx=(6, 0))
        btns2 = tk.Frame(led)
        btns2.pack(fill="x", pady=2)
        self.var_led_desens = tk.BooleanVar(value=bool(self.cfg.get("desensitize_export", True)))
        tk.Checkbutton(btns2, text="导出脱敏（证件号后 4 位、姓名留姓；默认勾选）",
                       variable=self.var_led_desens, font=FONT_S).pack(side="left")
        for text, fmt in (("导出 MD", "md"), ("导出 CSV", "csv"), ("导出 JSON", "json")):
            tk.Button(btns2, text=text,
                      command=lambda f_=fmt: self.ledger_export(f_)).pack(side="left", padx=3)
        self.var_led_sum = tk.StringVar(value="-")
        tk.Label(led, textvariable=self.var_led_sum, font=FONT_S, fg=C_DIM, anchor="w",
                 justify="left", wraplength=420).pack(fill="x", padx=6)
        self.tv_led = self._tree(led, [("考生(脱敏)", 100), ("姓名", 70), ("需求", 200),
                                       ("建议通道", 100), ("单独同意", 120),
                                       ("措施(已落实/共)", 110), ("替代核验", 70),
                                       ("受阻", 50)], height=8)

    def drill_load_channels(self):
        self.lb_dr_ch.delete(0, "end")
        try:
            chans = self.store.list_channels()
        except Exception as e:                                     # noqa: BLE001
            self._log("通道载入失败：%s" % str(e)[:100])
            chans = []
        for c in chans:
            self.lb_dr_ch.insert("end", "%s｜%s｜%s" % (c.code, c.kind_text(), c.site))
        if not chans:
            self.lb_dr_ch.insert("end", "（通道台账为空：先到「② 无障碍通道」登记）")

    def refresh_drills(self):
        try:
            drills = self.store.list_drills(limit=200)
        except Exception as e:                                     # noqa: BLE001
            self._log("演练记录读取失败：%s" % str(e)[:120])
            drills = []
        rows, tags = [], []
        for d in drills:
            rows.append((d.ts, d.site, d.session, d.scenario_text(), d.participants,
                         "%.0f/%.0f" % (d.planned_min, d.actual_min),
                         "；".join(d.issues)[:60] or "无", (d.actions or "-")[:60],
                         d.result_text(), source_text(d.source)))
            tags.append("bad" if d.result == config.DRILL_FAIL else
                        ("warn" if d.result == config.DRILL_PARTIAL else ""))
        self._fill(self.tv_dr, rows)
        for row, tag in zip(self.tv_dr.get_children(), tags):
            if tag:
                self.tv_dr.item(row, tags=(tag,))
        try:
            da = drill_mod.drill_availability(self.store, config.load_settings())
        except Exception as e:                                     # noqa: BLE001
            self._log("可用性统计失败：%s" % str(e)[:120])
            da = {"details": [], "rate": 0.0, "ok": False, "session": ""}
        arows, atags = [], []
        for d in da.get("details", []):
            arows.append((d["site"], "可用" if d["alt_available"] else "不可用",
                          d["drills"], d["failed_drills"], d["last_drill"] or "-",
                          "达标" if d["ok"] else "未达标",
                          "；".join(d["reasons"]) or "-"))
            atags.append("ok" if d["ok"] else "bad")
        if not arows:
            arows = [("-", "-", 0, 0, "-", "尚无考点数据", "先在「② 无障碍通道」登记通道")]
            atags = ["bad"]
        self._fill(self.tv_dravail, arows)
        for row, tag in zip(self.tv_dravail.get_children(), atags):
            self.tv_dravail.item(row, tags=(tag,))
        self.var_dr_avail.set("替代通道可用性 %.0f%%（%d/%d 考点，考期 %s）：%s"
                              % (da.get("rate", 0) * 100, da.get("sites_ok", 0),
                                 da.get("sites_total", 0), da.get("session", ""),
                                 da.get("target", "")))
        self.drill_load_channels()
        if not self.lb_dr_ch_loaded_once():
            pass

    _dr_ch_loaded = False

    def lb_dr_ch_loaded_once(self):
        self._dr_ch_loaded = True
        return self._dr_ch_loaded

    def drill_add(self):
        issues = [x.strip() for x in self.txt_dr_issues.get("1.0", "end").splitlines()
                  if x.strip()]
        channels = []
        for i in self.lb_dr_ch.curselection():
            item = self.lb_dr_ch.get(i)
            channels.append(item.split("｜")[0].strip())
        payload = {
            "scenario": label_to_key(config.DRILL_SCENARIOS, self.cb_dr_scenario.get(),
                                     "alt_verify"),
            "site": self.dr["site"].get().strip(),
            "session": self.dr["session"].get().strip(),
            "participants": to_int(self.dr["participants"].get(), 0),
            "planned_min": to_float(self.dr["planned_min"].get(), 0.0),
            "actual_min": to_float(self.dr["actual_min"].get(), 0.0),
            "channels": channels,
            "issues": issues,
            "actions": self.txt_dr_actions.get("1.0", "end").strip(),
            "result": label_to_key(config.DRILL_RESULT_TEXT, self.cb_dr_result.get(),
                                   config.DRILL_PASS),
            "witness": self.dr["witness"].get().strip(),
            "source": config.MANUAL_SOURCE,
        }
        ok, res, info = drill_mod.add_drill(self.store, payload,
                                            self.cfg.get("operator", ""))
        if not ok:
            messagebox.showwarning("演练登记被拒绝（这是硬校验）",
                                   "%s\n\n规则：有问题必须写整改措施；不合格必须写重演计划；"
                                   "参与人数≥1。" % res)
            return
        self.refresh_drills()
        self.refresh_overview()
        warn = info.get("warn") or []
        messagebox.showinfo("演练已登记",
                            "%s ｜ %s ｜ %s\n%s"
                            % (res.drill_id, res.site, res.scenario_text(),
                               ("提醒：\n· " + "\n· ".join(warn)) if warn else
                               "无额外提醒。"))
        self._log("演练登记 %s %s 结论=%s" % (res.drill_id, res.site, res.result))
        self.txt_dr_issues.delete("1.0", "end")
        self.txt_dr_actions.delete("1.0", "end")

    def refresh_ledger(self):
        try:
            rows = ledger_mod.rows(self.store)
            summ = ledger_mod.summary_text(self.store)
        except Exception as e:                                     # noqa: BLE001
            self._log("台账生成失败：%s: %s" % (type(e).__name__, str(e)[:120]))
            rows, summ = [], "台账生成失败：%s" % str(e)[:80]
        self.var_led_sum.set(summ + "\n（预览与导出默认脱敏；单独同意与医疗证明编号不导出）")
        out = []
        for r in rows:
            out.append((r["candidate"], r["name"], r["needs_text"], r["channel_text"],
                        r["consent_text"], "%d/%d" % (r["measures_provided"],
                                                     len(r["measures"])),
                        r["alt_verify_count"], r["blocks"]))
        self._fill(self.tv_led, out)

    def ledger_export(self, fmt):
        desens = bool(self.var_led_desens.get())
        ok, path = ledger_mod.write_ledger(self.store, fmt=fmt, desensitize=desens,
                                          tag="gui")
        if not ok:
            messagebox.showwarning("导出失败", str(path))
            return
        self.refresh_ledger()
        extra = "" if desens else "\n注意：本次导出**未脱敏**（含明文证件号），请按涉密材料保管。"
        messagebox.showinfo("台账已导出",
                            "格式：%s%s\n路径：%s\n\n默认脱敏（证件号只留后 4 位、姓名只留姓）；"
                            "单独同意与医疗证明编号不导出。"
                            % (fmt.upper(), extra, path))
        self._log("台账导出 %s -> %s" % (fmt, path))
        if messagebox.askyesno("打开目录", "要打开导出目录吗？\n%s"
                               % os.path.dirname(path)):
            self._open_dir(os.path.dirname(path))

    def _open_dir(self, path):
        """打开目录（资源管理器）；任何异常都吞掉，不能让界面挂掉。"""
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)                                 # noqa: S606
            else:
                messagebox.showinfo("目录路径", path)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showwarning("无法自动打开目录", "请手工打开：%s\n（%s）"
                                   % (path, str(e)[:100]))

    # =====================================================================
    # ⑦ 联动与验收
    # =====================================================================
    def _build_linkage(self):
        f = self.tab_linkage
        tk.Label(f, anchor="w", justify="left", font=FONT_S, fg=C_TAG, wraplength=1320,
                 text="联动方式：本模块**只 import 00_集成联动 的 linkage.py，不改任何其它专题文件**；"
                      "对外通知走 03 智能终端（03 不可达时事件进 outbox，恢复后补发，事件不丢）；"
                      "机位画质问 02、人脸核验问 01、人工安检口径与 06 一致。"
                      "所有跨模块调用都在后台线程执行，界面不阻塞；对端不可达时**原样显示原因**。"
                 ).pack(fill="x", padx=8, pady=(6, 2))

        top = tk.Frame(f)
        top.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.LabelFrame(top, text=" 各专题可达性（linkage_bridge.availability，只看文件不起子进程）")
        left.pack(side="left", fill="both", expand=True)
        self.var_bus = tk.StringVar(value="-")
        tk.Label(left, textvariable=self.var_bus, font=FONT_S, fg=C_DIM, anchor="w",
                 justify="left", wraplength=560).pack(fill="x", padx=6)
        self.tv_avail = self._tree(left, [("专题", 90), ("名称", 260), ("可达", 70),
                                          ("说明 / 目录", 420)], height=7)
        self.tv_avail.tag_configure("ok", foreground=C_OK)
        self.tv_avail.tag_configure("bad", foreground=C_BAD)

        right = tk.LabelFrame(top, text=" 全链路探测（linkage_bridge.probe）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.var_allow_write = tk.BooleanVar(value=False)
        tk.Checkbutton(right,
                       text="允许往 03 智能终端写入 1 条测试告警（write=True；会真实落库到 03，"
                            "默认不勾选＝只读探测）",
                       variable=self.var_allow_write, font=FONT_S, fg=C_BAD,
                       wraplength=560, justify="left").pack(anchor="w", padx=6, pady=2)
        r = tk.Frame(right)
        r.pack(fill="x", padx=6)
        self._label(r, "顺带探测机位编码（问 02 画质）").pack(side="left")
        self.e_dev = tk.Entry(r, width=16, font=FONT)
        self.e_dev.pack(side="left", padx=(2, 8))
        self._label(r, "顺带查 01 考生（可空）").pack(side="left")
        self.e_cand = tk.Entry(r, width=14, font=FONT)
        self.e_cand.pack(side="left", padx=(2, 8))
        btns = tk.Frame(right)
        btns.pack(fill="x", padx=6, pady=3)
        tk.Button(btns, text="🔎 全链路探测（后台）", bg="#ffe082",
                  command=self.linkage_probe).pack(side="left")
        tk.Button(btns, text="📤 补发待发事件（后台）",
                  command=self.linkage_flush).pack(side="left", padx=6)
        tk.Button(btns, text="📡 各专题 status（后台）",
                  command=self.linkage_status_all).pack(side="left")
        self.tv_probe = self._tree(right, [("步骤", 380), ("结果", 60), ("详情", 480)], height=9)
        self.tv_probe.tag_configure("ok", foreground=C_OK)
        self.tv_probe.tag_configure("bad", foreground=C_BAD)
        self.var_outbox = tk.StringVar(value="-")
        tk.Label(right, textvariable=self.var_outbox, font=FONT_S, fg=C_DIM, anchor="w",
                 justify="left", wraplength=560).pack(fill="x", padx=6, pady=2)

        bottom = tk.LabelFrame(f, text=" 验收证据生成（report.write_report，落地 data/reports/）")
        bottom.pack(fill="both", expand=True, padx=8, pady=4)
        b1 = tk.Frame(bottom)
        b1.pack(fill="x", padx=6, pady=3)
        tk.Button(b1, text="🧾 生成验收报告（后台）", bg="#c8e6c9",
                  command=self.gen_report_bg).pack(side="left")
        tk.Button(b1, text="📂 打开报告目录",
                  command=lambda: self._open_dir(str(config.REPORT_DIR))).pack(side="left", padx=6)
        tk.Button(b1, text="📂 打开台账目录",
                  command=lambda: self._open_dir(str(config.LEDGER_DIR))).pack(side="left")
        tk.Button(b1, text="🩺 校验审计链",
                  command=self.check_audit).pack(side="left", padx=6)
        self.var_report = tk.StringVar(value="尚未生成报告")
        tk.Label(bottom, textvariable=self.var_report, font=FONT_S, fg=C_DIM, anchor="w",
                 justify="left", wraplength=1300).pack(fill="x", padx=6)
        self.tv_status_all = self._tree(bottom, [("专题", 90), ("结论", 400)], height=5)

    def refresh_linkage(self):
        self._bg_link_availability()

    def _bg_link_availability(self):
        self._run_bg("各专题可达性", linkage_bridge.availability,
                     self._done_link_availability, busy_text="后台检查各专题可达性…")

    def _done_link_availability(self, av, err):
        if err or not av:
            self.var_bus.set("可达性检查失败：%s" % (err or "无返回"))
            return
        bus = av.get("bus") or {}
        if not bus.get("ok"):
            self.var_bus.set("00_集成联动 不可用：%s（本模块仍可独立使用：通道/档案/替代核验/"
                             "公平性/演练/台账/报告全部可用，只是推不到 03）"
                             % bus.get("reason"))
        else:
            self.var_bus.set("00_集成联动 总线可用（09 已在内存里注册，未修改 00 的文件）")
        rows, tags = [], []
        for key in ("01", "02", "03", "06", "09"):
            v = av.get(key)
            if v is None:
                continue
            rows.append((key, v.get("name", ""), "可达" if v.get("ok") else "不可达",
                         (str(v.get("dir") or "")) if v.get("ok")
                         else str(v.get("reason") or "")[:120]))
            tags.append("ok" if v.get("ok") else "bad")
        self._fill(self.tv_avail, rows)
        for row, tag in zip(self.tv_avail.get_children(), tags):
            self.tv_avail.item(row, tags=(tag,))
        self._log("可达性检查完成：%s" % (", ".join(
            "%s=%s" % (r[0], r[2]) for r in rows) or "无"))

    def linkage_probe(self):
        allow = bool(self.var_allow_write.get())
        if allow and not messagebox.askyesno(
                "确认写入 03？",
                "勾选了「允许写入」：探测过程会往 03 智能终端写入 1 条测试告警"
                "（ACCESS_ASSIST_NEEDED，橙色，标注为联动探测自检），并在 03 里复核结案留痕。\n"
                "这会真实落在 03 的库里。确定继续吗？"):
            return
        dev = self.e_dev.get().strip()
        cand = self.e_cand.get().strip()

        def work():
            return linkage_bridge.probe(store=self.store, device_code=dev,
                                        candidate=cand, write=allow)

        def done(r, err):
            if err or not r:
                messagebox.showwarning("探测异常", err or "无返回")
                return
            rows, tags = [], []
            for s in r.get("steps", []):
                rows.append((s["name"], "PASS" if s["ok"] else "FAIL", s["detail"]))
                tags.append("ok" if s["ok"] else "bad")
            self._fill(self.tv_probe, rows)
            for row, tag in zip(self.tv_probe.get_children(), tags):
                self.tv_probe.item(row, tags=(tag,))
            ob = r.get("outbox") or {}
            self.var_outbox.set("探测结论：%s ｜ outbox：共 %s 条、待补发 %s 条"
                                % ("全部环节通过" if r.get("ok") else
                                   "有不可用环节（见上表 FAIL 行，原因已如实列出）",
                                   ob.get("total", 0), ob.get("pending", 0)))
            self.refresh_ledger()
            self._done_link_availability(linkage_bridge.availability(), "")
            self.var_status.set("全链路探测完成：%s"
                                % ("通过" if r.get("ok") else "有不可用环节"))

        self._run_bg("全链路探测（可能耗时数十秒）", work, done, busy_text="后台全链路探测中…")

    def linkage_status_all(self):
        def work():
            return linkage_bridge.status_all()

        def done(r, err):
            if err or not r:
                messagebox.showwarning("status 异常", err or "无返回")
                return
            rows = []
            for key in ("01", "02", "03", "06", "09"):
                v = r.get(key) or {}
                if v.get("ok") is False:
                    txt = "不可达：%s" % str(v.get("reason"))[:200]
                else:
                    txt = "；".join("%s=%s" % (k, v2) for k, v2 in v.items()
                                   if k not in ("ok",))[:260] or "OK"
                rows.append((key, txt))
            self._fill(self.tv_status_all, rows)
            self.var_status.set("各专题 status 已取回（09 是经 00 总线调自己，证明协议一致）")

        self._run_bg("各专题 status", work, done, busy_text="后台询问各专题 status…")

    def linkage_flush(self):
        def work():
            return linkage_bridge.flush_outbox(self.store)

        def done(r, err):
            if err or not r:
                messagebox.showwarning("补发异常", err or "无返回")
                return
            st = r.get("stats") or {}
            self.var_outbox.set("补发结果：成功 %d 条、失败 %d 条 ｜ outbox：共 %s 条、待补发 %s 条"
                                % (len(r.get("sent") or []), len(r.get("failed") or []),
                                   st.get("total", 0), st.get("pending", 0)))
            self.refresh_overview()
            if not (r.get("sent") or r.get("failed")):
                messagebox.showinfo("无待发事件", "outbox 里没有待补发事件（共 %s 条）"
                                    % st.get("total", 0))
            else:
                messagebox.showinfo("补发完成", "成功 %d 条、失败 %d 条\n"
                                    "失败的会留在队列里（事件不丢），对端恢复后再补。"
                                    % (len(r.get("sent") or []), len(r.get("failed") or [])))
            self._log("outbox 补发：成功 %d / 失败 %d"
                      % (len(r.get("sent") or []), len(r.get("failed") or [])))

        self._run_bg("补发待发事件", work, done, busy_text="后台补发待发事件…")

    def gen_report_bg(self):
        def work():
            return report_mod.write_report(self.store, cfg=config.load_settings())

        def done(r, err):
            if err or not r:
                messagebox.showwarning("报告生成异常", err or "无返回")
                return
            ok, info = r
            if not ok:
                messagebox.showwarning("报告生成失败", str(info))
                return
            self.var_report.set(
                "报告：%s\n指标明细：%s\n台账：%s\n硬指标：%s\n审计链：%s"
                % (info.get("md"), info.get("csv"), info.get("ledger") or "（未生成）",
                   "全部达标" if info.get("ok_metrics")
                   else "未达标：" + "、".join(info.get("hard_fail") or []),
                   info.get("audit_chain")))
            self._log("验收报告已生成：%s" % info.get("md"))
            if messagebox.askyesno("报告已生成",
                                   "硬指标：%s\n\n要打开报告目录吗？\n%s"
                                   % ("全部达标" if info.get("ok_metrics")
                                      else "未达标：" + "、".join(info.get("hard_fail") or []),
                                      config.REPORT_DIR)):
                self._open_dir(str(config.REPORT_DIR))

        self._run_bg("生成验收报告", work, done, busy_text="后台生成验收报告…")

    def check_audit(self):
        """注意：store.verify_audit_chain() 返回三元组 (ok, msg, rows)。"""
        try:
            ok, msg, rows = self.store.verify_audit_chain()
        except Exception as e:                                     # noqa: BLE001
            messagebox.showwarning("校验失败", "%s: %s" % (type(e).__name__, str(e)[:150]))
            return
        self._render_env()
        messagebox.showinfo("审计链校验",
                            ("完整 ✔\n" if ok else "异常 ✘\n") + "%s（共 %d 行）" % (msg, rows))

    # ================= 关闭 =================
    def on_close(self):
        self._log("关闭界面：09 无障碍模块退出")
        try:
            self.store.log("关闭界面", "09 无障碍与特殊考生保障退出",
                           self.cfg.get("operator", ""))
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.store.close()
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                                          # noqa: BLE001
            pass


def main():
    config.ensure_stdio("app_console.log")     # pythonw 下 sys.stdout/stderr 为 None 的兜底
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", font=FONT_S, rowheight=22)
        style.configure("TNotebook.Tab", font=FONT_B, padding=(10, 4))
    except Exception:                                          # noqa: BLE001
        pass
    App(root)
    root.mainloop()
    return 0


def _write_fatal(tb):
    """
    把启动期异常写进 data\\logs\\app_console.log。
    **无窗口启动（pythonw）时这是唯一的线索**，所以绝不能让异常静默消失。
    """
    try:
        path = os.path.join(str(config.LOG_DIR), "app_console.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n[%s] 启动失败\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), tb))
        return path
    except Exception:                                          # noqa: BLE001
        return str(getattr(config, "LOG_DIR", ""))


def run():
    """
    程序入口（比裸 main() 多一层兜底）：
    任何启动异常都写日志 + 弹窗告诉用户日志在哪 —— 因为默认是无窗口启动，
    "双击了没反应"是最难排查的故障，这里必须让它有话说。
    """
    try:
        return main()
    except Exception:                                          # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        log = _write_fatal(tb)
        last = (tb.strip().splitlines() or ["未知错误"])[-1]
        try:
            messagebox.showerror(
                "无障碍与特殊考生保障 · 启动失败",
                "%s\n\n完整日志：\n%s" % (last, log))
        except Exception:                                      # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run())
