# -*- coding: utf-8 -*-
r"""
10 普通考场+特殊考场应用 · 图形界面

一套平台、多种模板：
  左：考场清单（普通 + 特殊七类，一眼看出哪个是特殊考场、哪个已就绪）
  右：7 个页签
      [考场台账]   套模板 / 降级最小可用配置 / 看模板差异 / 看能力缺口
      [准入自检]   一键流程：套模板 -> 登记点位 -> 机位自检 -> 在岗核验 -> 启用
      [分级告警]   选行为 -> 试判级 -> 看推送对象与抑制原因 -> 复核留痕
      [数据分域]   分区建立 / 授权矩阵 / 跨域拦截演示
      [合规留痕]   DPIA / 演练 / 离线自检 / 保存期限 / 封存 / 销毁
      [验收指标]   录入现场实测 -> 达标/未达标/缺失三分 -> 生成验收报告
      [审计与联动] 哈希链校验 + 01/02/03/06 可达性

启动：双击 启动考场应用界面.bat，或 python gui.py
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import config                                                  # noqa: E402

# 独立运行：把包根加入 sys.path，才能 import 包根的 ui_theme
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.isfile(os.path.join(_p, "ui_theme.py")):
        if _p not in sys.path:
            sys.path.append(_p)
        break

try:
    import ui_theme                                            # noqa: E402
except Exception:                                              # noqa: BLE001
    ui_theme = None

config.ensure_stdio("gui_console.log")

import admission                                                # noqa: E402
import alerting                                                 # noqa: E402
import behavior as behavior_mod                                 # noqa: E402
import kpi as kpi_mod                                           # noqa: E402
import linkage_client                                           # noqa: E402
import models                                                   # noqa: E402
import privacy                                                  # noqa: E402
import report as report_mod                                     # noqa: E402
import rooms as rooms_mod                                       # noqa: E402
import storage                                                  # noqa: E402

TITLE = "普通考场 + 特殊考场应用 · 智能考场 AI 巡考系统"
BG = ui_theme.BG if ui_theme else "#f4f6f8"
ACCENT = ui_theme.PANEL if ui_theme else "#1c5f9e"
SPECIAL_TAG = ui_theme.WARN if ui_theme else "#b45309"


class App(object):
    def __init__(self, root):
        self.root = root
        self.store = storage.Store()
        self.settings = config.load_settings()
        self.operator = self.settings.get("operator") or "考务管理员"
        self.current = None                 # 当前选中的 Room
        # 树控件 iid -> 业务主键 的映射（回调可能早于首次填充触发，先给空表）
        self._room_index = {}
        self._alert_index = {}
        self._beh_map = {}
        self.kpi_detail_room = None         # 指标页当前展示的考场明细
        if ui_theme is not None:
            ui_theme.apply(self.root)
        self._build()
        if ui_theme is not None:
            ui_theme.polish(self.root)
        self.refresh_rooms()
        if ui_theme is not None:
            ui_theme.polish_later(self.root)

    # =====================================================================
    # 布局
    # =====================================================================
    def _build(self):
        self.root.title(TITLE)
        # 先按屏幕大小算一个不放超出屏幕的尺寸（小屏笔记本上也要能看全）
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1280, max(1000, sw - 80))
        h = min(800, max(620, sh - 120))
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                            max(0, (sh - h) // 3)))
        self.root.minsize(980, 600)
        self.root.configure(bg=BG)

        # ---- 顶栏 ----
        top = tk.Frame(self.root, bg=ACCENT, height=52)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="  普通考场 + 特殊考场应用", bg=ACCENT, fg="white",
                 font=("Microsoft YaHei", 14, "bold")).pack(side="left")
        tk.Label(top, text="一套平台 · 多种模板 · 特殊考场不降标准不添门槛  ",
                 bg=ACCENT, fg="#d8e6f5",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.lbl_op = tk.Label(top, text="操作人：%s" % self.operator, bg=ACCENT,
                               fg="white", font=("Microsoft YaHei", 9))
        self.lbl_op.pack(side="right", padx=10)
        tk.Button(top, text="切换操作人", command=self._switch_operator,
                  relief="flat", bg="#2c74b8", fg="white",
                  font=("Microsoft YaHei", 9)).pack(side="right", padx=4)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # ---- 左：考场清单 ----
        left = tk.Frame(body, bg=BG, width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        box = tk.LabelFrame(left, text=" 考场清单 ", bg=BG, font=("Microsoft YaHei", 9))
        box.pack(fill="both", expand=True)

        bar = tk.Frame(box, bg=BG)
        bar.pack(fill="x", padx=4, pady=4)
        tk.Button(bar, text="＋ 新建考场", command=self.new_room,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="刷新", command=self.refresh_rooms,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=4)
        self.show_special_only = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="只看特殊", variable=self.show_special_only,
                       bg=BG, font=("Microsoft YaHei", 9),
                       command=self.refresh_rooms).pack(side="left")
        tk.Button(bar, text="一键演示数据", command=self.do_demo,
                  font=("Microsoft YaHei", 9), fg="#0b5",
                  relief="flat").pack(side="left", padx=6)

        cols = ("type", "status", "cfg")
        self.tree = ttk.Treeview(box, columns=cols, show="tree headings",
                                 height=22, selectmode="browse")
        self.tree.heading("#0", text="考场")
        self.tree.heading("type", text="类型")
        self.tree.heading("status", text="状态")
        self.tree.heading("cfg", text="配置")
        self.tree.column("#0", width=140, anchor="w")
        self.tree.column("type", width=70, anchor="center")
        self.tree.column("status", width=76, anchor="center")
        self.tree.column("cfg", width=52, anchor="center")
        self.tree.tag_configure("special", foreground=SPECIAL_TAG)
        self.tree.tag_configure("armed", foreground="#137333")
        self.tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)

        self.lbl_sum = tk.Label(box, text="", bg=BG, justify="left", anchor="w",
                                font=("Microsoft YaHei", 8), fg="#555")
        self.lbl_sum.pack(fill="x", padx=6, pady=4)

        # ---- 右：页签 ----
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self._tab_rooms()
        self._tab_admit()
        self._tab_alerts()
        self._tab_domain()
        self._tab_privacy()
        self._tab_kpi()
        self._tab_audit()

        # 页签第一次被打开时先渲染一次，避免"切过去是空白"（用户以为没内容）
        self._rendered = set()
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.status = tk.Label(self.root, text="就绪", bg="#e8edf2", anchor="w",
                               font=("Microsoft YaHei", 9))
        self.status.pack(fill="x", side="bottom")

    def msg(self, text, ok=True):
        self.status.configure(text=str(text)[:200],
                              bg="#e6f4ea" if ok else "#fce8e6")
        self.status.update_idletasks()

    # =====================================================================
    # 后台任务：所有跨系统调用（02 画质/台账、03 告警）都要走这里
    # =====================================================================
    def _q(self):
        if getattr(self, "_qbox", None) is None:
            self._qbox = queue.Queue()
            self._busy = False
        return self._qbox

    def is_busy(self):
        """是否有后台任务在跑（有的话拒绝再开一个，避免并发写库）。"""
        return bool(getattr(self, "_busy", False))

    def run_task(self, title, fn, on_done=None, log_lines=0):
        """
        把耗时动作丢到后台线程执行，界面不冻结。

        为什么必须这样：准入自检要**逐个起子进程**问 02 要画质结论，
        一个考场 4~6 个机位就是 2~6 秒；以前直接在当前线程跑，
        Windows 会判定"未响应"，用户以为程序挂了（实际就在等子进程）。
        """
        if self.is_busy():
            self.msg("上一个任务还在执行（%s），请稍候…" % getattr(self, "_task_name", ""),
                     ok=False)
            return False
        self._q()
        self._busy = True
        self._task_name = title
        self._task_fn = fn
        self._task_done = on_done
        self._task_lines = []
        self.msg("%s…（正在调用 02/03，界面保持可用）" % title)
        if log_lines:
            self._set_text(self._log_widget(log_lines),
                           "%s…\n\n正在调用其他专题（子进程 + JSON），请稍候。\n"
                           "期间界面不会冻结，可以继续点别的页签。\n" % title)
        t = threading.Thread(target=self._work, name="room-task", daemon=True)
        t.start()
        self.root.after(120, self._poll)
        return True

    def _work(self):
        """后台线程：只碰 store / 文件，不碰控件（结果经队列回到主线程渲染）。"""
        try:
            res = self._task_fn()
            self._q().put(("ok", res, None))
        except Exception as e:                               # noqa: BLE001
            self._q().put(("error", None,
                           "%s: %s\n%s" % (type(e).__name__, e,
                                           traceback.format_exc()[-800:])))

    def _poll(self):
        try:
            kind, res, err = self._q().get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll)
            return
        self._busy = False
        if kind == "ok":
            try:
                if self._task_done:
                    self._task_done(res)
            except Exception as e:                           # noqa: BLE001
                self.msg("结果渲染出错：%s: %s" % (type(e).__name__, e), ok=False)
        else:
            self.msg("%s失败：%s" % (self._task_name, str(err).splitlines()[0]),
                     ok=False)
            if self._task_lines:
                self._task_lines.append("\n[失败] %s" % err)
                self._flush_lines()

    def _log_widget(self, which):
        return {1: self.txt_admit, 2: self.txt_al, 3: self.txt_dom,
                4: self.txt_pv, 5: self.txt_kpi, 6: self.txt_aud}[which]

    def _flush_lines(self):
        if getattr(self, "_log_widget_idx", 0):
            self._set_text(self._log_widget(self._log_widget_idx),
                           "\n".join(self._task_lines))

    def _bind_log(self, which):
        self._log_widget_idx = which

    def _on_tab_changed(self, _evt=None):
        """
        切到某个页签时把它渲染一次——**每页都有可看的初始内容**，
        新用户不至于以为"点了没反应"。只对还没渲染过的页签做，不打断后续操作。
        """
        try:
            idx = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if idx in self._rendered:
            return
        self._rendered.add(idx)
        try:
            if idx == 0 and self.current:
                self._paint_room()
            elif idx == 1 and self.current:
                self.do_admit()
            elif idx == 2:
                self.refresh_alerts()
            elif idx == 3:
                self.refresh_domain()
            elif idx == 4:
                self.refresh_privacy()
            elif idx == 5:
                self.refresh_kpi()
            elif idx == 6:
                self.refresh_audit()
        except Exception as e:                              # noqa: BLE001
            self.msg("页签渲染出错：%s: %s" % (type(e).__name__, e), ok=False)

    def _switch_operator(self):
        win = tk.Toplevel(self.root)
        win.title("切换操作人")
        win.geometry("320x120")
        win.configure(bg=BG)
        tk.Label(win, text="操作人（复核/启用/销毁都要留这个名）", bg=BG,
                 font=("Microsoft YaHei", 9)).pack(pady=8)
        var = tk.StringVar(value=self.operator)
        tk.Entry(win, textvariable=var, width=28).pack()

        def ok():
            name = var.get().strip()
            if not name:
                messagebox.showwarning("提示", "操作人不能为空")
                return
            self.operator = name
            config.save_settings({"operator": name})
            self.lbl_op.configure(text="操作人：%s" % name)
            win.destroy()
            self.msg("操作人已切换为 %s" % name)
        tk.Button(win, text="确定", command=ok).pack(pady=10)

    # =====================================================================
    # 页签 1：考场台账
    # =====================================================================
    def _tab_rooms(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="考场台账")

        top = tk.LabelFrame(f, text=" 一键套用模板（《特殊考场应用.md》第二节第 1 条） ",
                            bg=BG, font=("Microsoft YaHei", 9))
        top.pack(fill="x", padx=6, pady=6)
        self.var_type = tk.StringVar()
        tk.Label(top, text="考场类型", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, padx=6, pady=6)
        self.type_labels = ["%s（%s）" % (
            config.type_name(t), "特殊" if config.type_is_special(t) else "普通")
            for t in config.TYPE_ORDER]
        self.type_by_label = dict(zip(self.type_labels, config.TYPE_ORDER))
        self.cmb_type = ttk.Combobox(top, textvariable=self.var_type,
                                     state="readonly", width=32,
                                     values=self.type_labels)
        self.cmb_type.grid(row=0, column=1, padx=6, pady=6)
        self.cmb_type.bind("<<ComboboxSelected>>", self._on_type_pick)
        self.var_type.set(self.type_labels[0])       # 默认「普通考场（普通）」
        tk.Button(top, text="套用模板", command=self.do_apply_template,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=6)
        tk.Button(top, text="降级最小可用配置", command=self.do_degrade,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=3, padx=6)
        tk.Button(top, text="恢复标准配置", command=self.do_restore,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=4, padx=6)

        mid = tk.Frame(f, bg=BG)
        mid.pack(fill="both", expand=True, padx=6)
        left = tk.LabelFrame(mid, text=" 点位（来自模板 + 02 台账机位编码） ", bg=BG,
                             font=("Microsoft YaHei", 9))
        left.pack(side="left", fill="both", expand=True)
        self.tree_pts = ttk.Treeview(left, columns=("where", "code", "req"),
                                     show="headings", height=9)
        for c, t, w in (("where", "安装位置", 150), ("code", "机位编码", 130),
                        ("req", "必需", 50)):
            self.tree_pts.heading(c, text=t)
            self.tree_pts.column(c, width=w, anchor="w")
        self.tree_pts.pack(fill="both", expand=True, padx=4, pady=4)

        right = tk.LabelFrame(mid, text=" 与本考场类型的差异 / 能力缺口 ", bg=BG,
                              font=("Microsoft YaHei", 9))
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.txt_diff = tk.Text(right, height=14, wrap="word",
                                font=("Microsoft YaHei", 9))
        self.txt_diff.pack(fill="both", expand=True, padx=4, pady=4)

    # =====================================================================
    # 页签 2：准入自检
    # =====================================================================
    def _tab_admit(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="准入自检")

        bar = tk.LabelFrame(f, text=" 一键流程（方案第三节「隔离考场」示例的操作链） ",
                            bg=BG, font=("Microsoft YaHei", 9))
        bar.pack(fill="x", padx=6, pady=6)
        tk.Label(bar, text="机位编码前缀", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, padx=6, pady=6)
        self.var_prefix = tk.StringVar(value="CAM-A101-")
        tk.Entry(bar, textvariable=self.var_prefix, width=16).grid(row=0, column=1)
        tk.Button(bar, text="① 套模板 + 登记点位", command=self.do_step1,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=6)
        tk.Button(bar, text="② 机位画质自检（调 02）", command=self.do_step2,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=3, padx=6)
        tk.Button(bar, text="③ 在岗核验 + 座位建模 + 标定", command=self.do_step3,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=4, padx=6)
        tk.Button(bar, text="④ 准入自检", command=self.do_admit,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=5, padx=6)
        tk.Button(bar, text="⑤ 启用（arm）", command=self.do_arm,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=6, padx=6)
        tk.Button(bar, text="停用", command=self.do_disable,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=7, padx=6)

        opt = tk.Frame(f, bg=BG)
        opt.pack(fill="x", padx=10)
        # 6 个"已办事项"开关：勾上 = 这件事做完了。带 ★ 的会由「合规留痕」页
        # 的正规动作（双人/理由）来置位，直接勾选只作演示与应急。
        self.flag_specs = (
            ("alt_channel", "非人脸替代核验通道"),
            ("drill_done", "替代通道演练"),
            ("fast_enable", "备用考场即插即用预置"),
            ("doctor_link", "医生协同通道"),
            ("offline_ready", "全离线自检"),
            ("network_log", "网络行为日志"),
            ("dedup_verified", "跨机位去重已验证"),
            ("screen_audit", "屏幕行为审计"),
            ("independent_mic", "独立拾音"),
            ("points_verified", "覆盖完整性已核查"),
        )
        self.var_special_flags = {}
        for idx, (key, label) in enumerate(self.flag_specs):
            v = tk.BooleanVar(value=False)
            self.var_special_flags[key] = v
            tk.Checkbutton(opt, text=label, variable=v, bg=BG,
                           font=("Microsoft YaHei", 8), anchor="w",
                           command=lambda k=key: self.do_toggle_flag(k)).grid(
                row=idx // 3, column=idx % 3, sticky="w", padx=4, pady=1)
        rate_box = tk.Frame(f, bg=BG)
        rate_box.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(rate_box, text="录音采样率（口试考场 ≥48000）", bg=BG,
                 font=("Microsoft YaHei", 8)).pack(side="left")
        self.var_rate = tk.StringVar(value="48000")
        tk.Entry(rate_box, textvariable=self.var_rate, width=8).pack(side="left",
                                                                    padx=4)
        tk.Button(rate_box, text="写入", command=self.do_rate,
                  font=("Microsoft YaHei", 8)).pack(side="left")

        self.txt_admit = tk.Text(f, wrap="word", font=("Consolas", 9), bg="#fbfcfd")
        self.txt_admit.pack(fill="both", expand=True, padx=6, pady=6)

    # =====================================================================
    # 页签 3：分级告警
    # =====================================================================
    def _tab_alerts(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="分级告警")

        box = tk.LabelFrame(f, text=" 试判一次行为告警（定级 / 降权 / 抑制 / 推送） ",
                            bg=BG, font=("Microsoft YaHei", 9))
        box.pack(fill="x", padx=6, pady=6)
        tk.Label(box, text="行为", bg=BG, font=("Microsoft YaHei", 9)).grid(
            row=0, column=0, padx=4, pady=4)
        self.var_beh = tk.StringVar()
        self.cmb_beh = ttk.Combobox(box, textvariable=self.var_beh, width=34,
                                    state="readonly")
        self.cmb_beh.grid(row=0, column=1, padx=4)
        tk.Label(box, text="置信度", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=4)
        self.var_conf = tk.StringVar(value="0.95")
        tk.Entry(box, textvariable=self.var_conf, width=7).grid(row=0, column=3)
        self.var_cam = tk.StringVar(value="1.0")
        tk.Label(box, text="机位可信度", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=4, padx=4)
        tk.Entry(box, textvariable=self.var_cam, width=6).grid(row=0, column=5)
        self.var_frames = tk.StringVar(value="5")
        tk.Label(box, text="持续帧数", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=6, padx=4)
        tk.Entry(box, textvariable=self.var_frames, width=5).grid(row=0, column=7)
        tk.Button(box, text="试判（不推送）", command=self.do_route_dry,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=8, padx=6)
        tk.Button(box, text="真实上报（送 03）", command=self.do_route_push,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=9, padx=6)
        tk.Button(box, text="刷新该考场告警", command=self.refresh_alerts,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=10, padx=6)

        cols = ("ts", "beh", "level", "conf", "state", "push")
        self.tree_al = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, t, w in (("ts", "时间", 130), ("beh", "行为", 150),
                        ("level", "级别", 60), ("conf", "置信度", 60),
                        ("state", "状态", 100), ("push", "推送对象", 240)):
            self.tree_al.heading(c, text=t)
            self.tree_al.column(c, width=w, anchor="w")
        self.tree_al.pack(fill="x", padx=6)

        rv = tk.LabelFrame(f, text=" 复核结论回流（成立 / 不成立 / 误报） ", bg=BG,
                           font=("Microsoft YaHei", 9))
        rv.pack(fill="x", padx=6, pady=6)
        tk.Label(rv, text="结论", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, padx=4, pady=4)
        self.var_concl = tk.StringVar(value=config.REVIEW_CONFIRMED)
        ttk.Combobox(rv, textvariable=self.var_concl, width=12, state="readonly",
                     values=list(config.REVIEW_CONCLUSIONS)).grid(row=0, column=1)
        tk.Label(rv, text="理由", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=4)
        self.var_reason = tk.StringVar(value="现场核实")
        tk.Entry(rv, textvariable=self.var_reason, width=40).grid(row=0, column=3)
        tk.Button(rv, text="提交复核", command=self.do_review,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=4, padx=6)

        self.txt_al = tk.Text(f, wrap="word", font=("Consolas", 9), bg="#fbfcfd",
                              height=9)
        self.txt_al.pack(fill="both", expand=True, padx=6, pady=6)

    # =====================================================================
    # 页签 4：数据分域
    # =====================================================================
    def _tab_domain(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="数据分域")

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", padx=6, pady=6)
        tk.Button(bar, text="建立/查看本考场分区", command=self.do_partition,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="刷新分区总览", command=self.refresh_domain,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Label(bar, text="域", bg=BG, font=("Microsoft YaHei", 9)).pack(
            side="left", padx=(12, 2))
        self.var_domain = tk.StringVar(value=config.DOMAIN_HEALTH)
        ttk.Combobox(bar, textvariable=self.var_domain, width=22, state="readonly",
                     values=list(config.DOMAINS)).pack(side="left")
        tk.Label(bar, text="角色", bg=BG, font=("Microsoft YaHei", 9)).pack(
            side="left", padx=(12, 2))
        self.var_role = tk.StringVar(value="doctor")
        ttk.Combobox(bar, textvariable=self.var_role, width=18,
                     values=["owner", "admin", "patrol", "commander", "doctor",
                             "remote_invigilator", "network_admin",
                             "security_officer", "invigilator"]).pack(side="left")
        tk.Button(bar, text="授权", command=lambda: self.do_grant(True),
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Button(bar, text="撤权", command=lambda: self.do_grant(False),
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="试访问（跨域拦截）", command=self.do_access_test,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)

        self.txt_dom = tk.Text(f, wrap="word", font=("Consolas", 9), bg="#fbfcfd")
        self.txt_dom.pack(fill="both", expand=True, padx=6, pady=6)

    # =====================================================================
    # 页签 5：合规留痕
    # =====================================================================
    def _tab_privacy(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="合规留痕")

        self.txt_pv = tk.Text(f, wrap="word", font=("Microsoft YaHei", 9),
                              bg="#fbfcfd", height=10)
        self.txt_pv.pack(fill="x", padx=6, pady=6)

        acts = tk.LabelFrame(f, text=" 合规动作（全部要求双人/理由，缺一不可） ", bg=BG,
                             font=("Microsoft YaHei", 9))
        acts.pack(fill="x", padx=6)
        tk.Label(acts, text="评估人/见证人", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, padx=4, pady=4)
        self.var_witness = tk.StringVar(value="数据保护专员")
        tk.Entry(acts, textvariable=self.var_witness, width=18).grid(row=0, column=1)
        tk.Label(acts, text="结论/理由", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=4)
        self.var_pvdetail = tk.StringVar(
            value="健康信息单独分区、最小必要、15 天销毁")
        tk.Entry(acts, textvariable=self.var_pvdetail, width=34).grid(row=0, column=3)
        tk.Label(acts, text="保存天数", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=4, padx=4)
        self.var_days = tk.StringVar(value="15")
        tk.Entry(acts, textvariable=self.var_days, width=6).grid(row=0, column=5)

        row2 = tk.Frame(f, bg=BG)
        row2.pack(fill="x", padx=6, pady=6)
        for text, fn in (("做 DPIA", self.do_dpia),
                         ("记录替代通道演练", self.do_drill),
                         ("全离线自检", self.do_offline),
                         ("设置保存期限", self.do_retention),
                         ("考试结束封存", self.do_seal),
                         ("到期销毁（出销毁记录）", self.do_destroy),
                         ("合规体检", self.refresh_privacy)):
            tk.Button(row2, text=text, command=fn,
                      font=("Microsoft YaHei", 9)).pack(side="left", padx=4)

        self.tree_pv = ttk.Treeview(f, columns=("ts", "act", "op", "wit", "detail"),
                                    show="headings", height=9)
        for c, t, w in (("ts", "时间", 130), ("act", "动作", 140),
                        ("op", "操作人", 90), ("wit", "见证/评估人", 100),
                        ("detail", "明细", 380)):
            self.tree_pv.heading(c, text=t)
            self.tree_pv.column(c, width=w, anchor="w")
        self.tree_pv.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # =====================================================================
    # 页签 6：验收指标
    # =====================================================================
    def _tab_kpi(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="验收指标")

        box = tk.LabelFrame(f, text=" 录入一次验收测算（方案第六节 / 第四节差异项） ",
                            bg=BG, font=("Microsoft YaHei", 9))
        box.pack(fill="x", padx=6, pady=6)
        fields = [("标注样本总数", "total", "40"), ("系统提示到", "hit", "40"),
                  ("类别判对", "correct", "38"), ("误报条数", "false", "1"),
                  ("响应秒数", "resp", "2.4"), ("在线率", "online", "0.99"),
                  ("覆盖完整性", "cover", "1.0"), ("去重真值人数", "ddexp", "90"),
                  ("去重计数", "ddcnt", "88"), ("时钟偏差秒", "drift", "0.4")]
        self.var_kpi = {}
        grid = tk.Frame(box, bg=BG)
        grid.pack(fill="x", padx=4, pady=4)
        for i, (label, key, dflt) in enumerate(fields):
            r, c = divmod(i, 5)
            tk.Label(grid, text=label, bg=BG, font=("Microsoft YaHei", 9)).grid(
                row=r, column=c * 2, padx=4, pady=4, sticky="e")
            v = tk.StringVar(value=dflt)
            self.var_kpi[key] = v
            tk.Entry(grid, textvariable=v, width=8).grid(row=r, column=c * 2 + 1)
        tk.Label(grid, text="数据来源", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=10, padx=4)
        self.var_source = tk.StringVar(value="real")
        ttk.Combobox(grid, textvariable=self.var_source, width=12, state="readonly",
                     values=list(models.DATA_SOURCE_TEXT)).grid(row=0, column=11)

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", padx=6, pady=4)
        tk.Button(bar, text="计算并保存指标", command=self.do_kpi,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="用内置基线跑一次（标注为合成）",
                  command=self.do_kpi_baseline,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Button(bar, text="生成验收报告（md + csv）", command=self.do_report,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Button(bar, text="刷新考点看板", command=self.refresh_kpi,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)

        self.txt_kpi = tk.Text(f, wrap="word", font=("Consolas", 9), bg="#fbfcfd")
        self.txt_kpi.pack(fill="both", expand=True, padx=6, pady=6)

    # =====================================================================
    # 页签 7：审计与联动
    # =====================================================================
    def _tab_audit(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="审计与联动")

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", padx=6, pady=6)
        tk.Button(bar, text="校验审计哈希链", command=self.refresh_audit,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="探联动（01/02/03/06）", command=self.do_linkage_probe,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Label(bar, text="  联动沙箱目录（留空=真实数据）", bg=BG,
                 font=("Microsoft YaHei", 9)).pack(side="left", padx=(16, 2))
        self.var_sandbox = tk.StringVar(
            value=self.settings.get("sandbox_root") or "")
        tk.Entry(bar, textvariable=self.var_sandbox, width=32).pack(side="left")
        tk.Button(bar, text="应用", command=self.do_sandbox,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=4)

        self.txt_aud = tk.Text(f, wrap="word", font=("Consolas", 9), bg="#fbfcfd")
        self.txt_aud.pack(fill="both", expand=True, padx=6, pady=6)

    # =====================================================================
    # 数据刷新
    # =====================================================================
    def refresh_rooms(self):
        self._refresh_room_tree()
        s = self.store.summary()
        self.lbl_sum.configure(
            text="共 %d 个考场（普通 %d / 特殊 %d），已就绪 %d\n"
                 "告警 %d（红 %d / 橙 %d / 抑制 %d），验收测算 %d 次"
                 % (s["rooms"], s["standard_rooms"], s["special_rooms"], s["armed"],
                    s["alerts"]["total"], s["alerts"]["red"], s["alerts"]["orange"],
                    s["alerts"]["suppressed"], s["kpi_runs"]))
        if self.current:
            self.current = self.store.get_room(self.current.room_id)
            self._paint_room()

    def _refresh_room_tree(self):
        """只重画左边的考场树 + 汇总，**不动右侧页签**（页签有自己的刷新时机）。"""
        for i in self.tree.get_children():
            self.tree.delete(i)
        rooms = self.store.list_rooms(special=(True if self.show_special_only.get()
                                               else None), limit=2000)
        self._room_index = {}
        for r in sorted(rooms, key=lambda x: (x.room_type, x.name)):
            tags = []
            if r.is_special():
                tags.append("special")
            if r.armed():
                tags.append("armed")
            iid = self.tree.insert(
                "", "end", text=r.name, tags=tags,
                values=(config.ROOM_TYPES[r.room_type]["short"], r.status_text(),
                        r.min_config_meta()["name"].replace("配置", "")))
            self._room_index[iid] = r.room_id
        s = self.store.summary()
        self.lbl_sum.configure(
            text="共 %d 个考场（普通 %d / 特殊 %d），已就绪 %d\n"
                 "告警 %d（红 %d / 橙 %d / 抑制 %d），验收测算 %d 次"
                 % (s["rooms"], s["standard_rooms"], s["special_rooms"], s["armed"],
                    s["alerts"]["total"], s["alerts"]["red"], s["alerts"]["orange"],
                    s["alerts"]["suppressed"], s["kpi_runs"]))
        # 首次填充时自动选中第一个考场，避免右边整片空白（也让新用户知道该点哪）
        kids = self.tree.get_children()
        if kids and not self.tree.selection():
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
            self.tree.see(kids[0])

    def _on_type_pick(self, _evt=None):
        """选类型时顺手把"座位数/该类型要点"提示出来，避免选错模板。"""
        t = self.type_by_label.get(self.var_type.get())
        if not t:
            return
        meta = config.ROOM_TYPES[t]
        self.msg("%s：%s ｜ 默认 %d 座、必需点位 %d 个%s"
                 % (meta["name"], meta["scope"], meta["seats_default"],
                    len([p for p in meta["points"] if p["required"]]),
                    "（特殊考场，套模板后请按提示补齐强制项）"
                    if meta["special"] else ""))

    def do_demo(self):
        """一键铺演示数据：第一次打开界面不该是空屏。"""
        if self.store.list_rooms() and not messagebox.askyesno(
                "已有考场", "当前已有 %d 个考场。\n"
                          "继续会在其基础上补充演示数据（不会删除已有的）。\n\n继续？"
                          % len(self.store.list_rooms())):
            return
        try:
            import importlib
            demo = importlib.import_module("演示数据")
        except ImportError as e:
            self.msg("找不到 演示数据.py：%s" % e, ok=False)
            return
        self._bind_log(1)
        self.run_task(
            "铺演示数据（9 个考场，含 02 登记与准入自检）",
            lambda: demo.seed(operator=self.operator, force=True, quiet=True),
            self._demo_done, log_lines=1)

    def _demo_done(self, _res):
        self.refresh_rooms()
        s = self.store.summary()
        self._set_text(self.txt_admit,
                       "演示数据已就绪：\n"
                       "  考场 %d 个（普通 %d / 特殊 %d），已启用 %d\n"
                       "  告警 %d 条，验收测算 %d 次\n\n"
                       "说明：演示指标是合成数据（data_source=synthetic），"
                       "只用于跑通流程；验收时必须改成考场现场实测（来源选 real）。\n"
                       "想清空重来：关闭界面后删除 data\\rooms.db 即可。"
                       % (s["rooms"], s["standard_rooms"], s["special_rooms"],
                          s["armed"], s["alerts"]["total"], s["kpi_runs"]))
        self.msg("演示数据已就绪：%d 个考场（普通 %d / 特殊 %d），已启用 %d，告警 %d 条"
                 % (s["rooms"], s["standard_rooms"], s["special_rooms"],
                    s["armed"], s["alerts"]["total"]))

    def _on_pick(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        rid = self._room_index.get(sel[0])
        self.current = self.store.get_room(rid) if rid else None
        self._paint_room()

    def _paint_room(self):
        r = self.current
        if r is None:
            return
        self.var_type.set("%s（%s）" % (
            r.type_name(), "特殊" if r.is_special() else "普通"))
        # 点位表
        for i in self.tree_pts.get_children():
            self.tree_pts.delete(i)
        for p in (r.points or []):
            self.tree_pts.insert("", "end", text=p.get("no") or "",
                                 values=(p.get("where") or "", p.get("device_code") or "",
                                         "是" if p.get("required") else "否"))
        # 差异 + 能力
        adv = rooms_mod.capability_advice(r)
        lines = ["【%s】%s" % (r.type_name(), r.status_text()),
                 "配置档位：%s ｜ 数据域：%s"
                 % (r.min_config_meta()["name"],
                    config.DOMAIN_RULES.get(r.data_domain, {}).get("name", "")),
                 "置信度门槛：%.2f ｜ 启用行为：%d 种"
                 % (r.confidence_min(), len(r.enabled_behaviors or [])),
                 "能力建议：%s" % adv["message"], ""]
        if r.caps_missing:
            lines.append("必需能力缺口（阻断）：")
            lines += ["  - %s" % x["name"] for x in r.caps_missing] + [""]
        if r.caps_weak:
            lines.append("期望能力未配置（不阻断）：")
            lines += ["  - %s" % x["name"] for x in r.caps_weak] + [""]
        diff = rooms_mod.template_diff(r.room_type)
        if diff["items"]:
            lines.append("相对普通考场的差异（共 %d 项）：" % len(diff["items"]))
            lines += ["  · %s：%s %s" % (i["field"], i["change"], i["value"])
                      for i in diff["items"][:14]]
        else:
            lines.append("普通考场为基准模板，无差异项。")
        self._set_text(self.txt_diff, "\n".join(lines))
        # 准入页的开关状态
        v = {"alt_channel": r.alt_channel, "drill_done": r.drill_done,
             "fast_enable": r.fast_enable, "doctor_link": r.doctor_link,
             "offline_ready": r.offline_ready, "network_log": r.network_log,
             "dedup_verified": r.dedup_verified, "screen_audit": r.screen_audit,
             "independent_mic": r.independent_mic,
             "points_verified": r.points_verified}
        for k, var in self.var_special_flags.items():
            var.set(bool(v.get(k, 0)))
        self.var_rate.set(str(r.audio_sample_rate or 48000))
        self.refresh_alerts()
        self.refresh_kpi()

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _need_room(self):
        if self.current is None:
            messagebox.showinfo("提示", "先在左边选一个考场")
            return None
        return self.current

    # =====================================================================
    # 动作：考场台账
    # =====================================================================
    def new_room(self):
        win = tk.Toplevel(self.root)
        win.title("新建考场")
        win.geometry("420x260")
        win.configure(bg=BG)
        rows = [("考场名称", "name", "A-101"),
                ("座位数", "seats", "30"),
                ("启用申请原因", "reason", "常规笔试")]
        vs = {}
        for i, (label, key, dflt) in enumerate(rows):
            tk.Label(win, text=label, bg=BG,
                     font=("Microsoft YaHei", 9)).grid(row=i, column=0, padx=8,
                                                       pady=8, sticky="e")
            v = tk.StringVar(value=dflt)
            vs[key] = v
            tk.Entry(win, textvariable=v, width=30).grid(row=i, column=1)
        tk.Label(win, text="考场类型", bg=BG,
                 font=("Microsoft YaHei", 9)).grid(row=3, column=0, padx=8, sticky="e")
        vt = tk.StringVar(value=config.TYPE_STANDARD)
        ttk.Combobox(win, textvariable=vt, width=28, state="readonly",
                     values=list(config.TYPE_ORDER)).grid(row=3, column=1)

        def ok():
            name = vs["name"].get().strip()
            if not name:
                messagebox.showwarning("提示", "考场名称必填")
                return
            room = rooms_mod.upsert_room(
                self.store, name, vt.get(), operator=self.operator,
                seats=models.to_int(vs["seats"].get(), 30),
                apply_reason=vs["reason"].get().strip())
            self.store.log("考场建档", "%s（%s）" % (name, room.type_name()),
                           self.operator, room.room_id)
            self.current = room
            win.destroy()
            self.refresh_rooms()
            self.msg("已建档：%s（%s）。下一步：套用模板。" % (name, room.type_name()))
        tk.Button(win, text="创建", command=ok).grid(row=4, column=1, pady=14)

    def do_apply_template(self):
        r = self._need_room()
        if not r:
            return
        t = self.var_type.get().split("（")[0]
        rt = next((k for k, v in config.ROOM_TYPES.items() if v["name"] == t),
                  r.room_type)
        res = rooms_mod.apply_template(r, operator=self.operator, room_type=rt,
                                       store=self.store)
        self.store.log("套用模板", "%s -> %s（差异 %d 项）"
                       % (r.name, r.type_name(), len(res["diff"]["items"])),
                       self.operator, r.room_id)
        self.refresh_rooms()
        self.msg("已套用「%s」模板：%d 个点位、%d 种行为、差异 %d 项"
                 % (r.type_name(), len(r.points), len(r.enabled_behaviors or []),
                    len(res["diff"]["items"])))

    def do_degrade(self):
        r = self._need_room()
        if not r:
            return
        try:
            res = rooms_mod.degrade_to_minimal(r, level="minimal",
                                               operator=self.operator,
                                               store=self.store)
        except rooms_mod.RoomError as e:
            self.msg(str(e), ok=False)
            return
        self.store.log("降级最小可用配置", "%s：%s" % (r.name, res["message"]),
                       self.operator, r.room_id)
        self.refresh_rooms()
        self.msg(res["message"])

    def do_restore(self):
        r = self._need_room()
        if not r:
            return
        rooms_mod.restore_full(r)
        self.store.save_room(r)
        self.store.log("恢复标准配置", r.name, self.operator, r.room_id)
        self.refresh_rooms()
        self.msg("已恢复标准配置：%d 个点位" % len(r.points))

    # =====================================================================
    # 动作：准入自检
    # =====================================================================
    def do_step1(self):
        r = self._need_room()
        if not r:
            return
        prefix = self.var_prefix.get().strip() or "CAM-"
        self._bind_log(1)
        self.run_task(
            "① 套模板 + 登记点位（并写入 02 机位台账）",
            lambda: self._step1_work(r.room_id, prefix),
            self._step1_done, log_lines=1)

    def _step1_work(self, room_id, prefix):
        room = self.store.get_room(room_id)
        rooms_mod.apply_template(room, operator=self.operator, store=self.store)
        for i, p in enumerate(room.points):
            if not p.get("device_code"):
                p["device_code"] = "%s%d" % (prefix, i + 1)
        rooms_mod.recompute_caps(room)
        self.store.save_room(room)
        # 登记进 02 台账（单向写入，不改 02 代码）；02 不可用时如实回失败
        reg = admission.register_room_points(room, operator=self.operator)
        self.store.log("登记点位编码",
                       "%s：%d 个点位，前缀 %s，02 台账登记 %d 个"
                       % (room.name, len(room.points), prefix,
                          reg.get("registered") or 0),
                       self.operator, room.room_id)
        return {"room_id": room_id, "rooms": None, "reg": reg}

    def _step1_done(self, res):
        room = self.store.get_room(res["room_id"])
        self.current = room
        reg = res["reg"] or {}
        lines = ["① 已套用「%s」模板并登记 %d 个点位（机位编码前缀已写入）"
                 % (room.type_name(), len(room.points)), ""]
        lines += ["  %-18s %s" % (p.get("no"), p.get("device_code"))
                  for p in room.points]
        lines += ["", "02 机位台账：登记 %d 个、已存在跳过 %d 个"
                  % (reg.get("registered") or 0, reg.get("skipped") or 0)]
        if reg.get("failed"):
            lines += ["登记失败："] + ["  - %s" % x for x in reg["failed"]]
        if reg.get("ok") is False:
            lines += ["", "02 不可用：%s" % str(reg.get("reason"))[:160],
                      "（准入自检会如实判为无法核验，不会假装通过）"]
        self._set_text(self.txt_admit, "\n".join(lines))
        self.refresh_rooms()
        self.msg("① 完成：%d 个点位，02 台账新增 %d 个"
                 % (len(room.points), reg.get("registered") or 0))

    def do_step2(self):
        r = self._need_room()
        if not r:
            return
        self._bind_log(1)
        self.run_task(
            "② 机位画质自检（逐个机位问 02）",
            lambda: self._step2_work(r.room_id),
            self._step2_done, log_lines=1)

    def _step2_work(self, room_id):
        room = self.store.get_room(room_id)
        pcs, defects, factor = admission.selfcheck_points(room,
                                                          operator=self.operator)
        for pc in pcs:
            for p in (room.points or []):
                if p.get("no") == pc.no and pc.device_code \
                        and not p.get("device_code"):
                    p["device_code"] = pc.device_code
        room.last_check_ok = 0 if defects else 1
        self.store.save_room(room)
        self.store.log("机位自检", "%s：%d 个机位，缺陷 %d 项，系数 ×%.2f"
                       % (room.name, len(pcs), len(defects), factor),
                       self.operator, room.room_id)
        return {"room_id": room_id, "pcs": [p.to_dict() for p in pcs],
                "defects": [d.to_dict() for d in defects], "factor": factor}

    def _step2_done(self, res):
        lines = ["② 机位画质自检（结论来自专题 02，本模块不做画质算法）", ""]
        lines.append("  %-16s %-18s %-8s %s" % ("点位", "机位编码", "结论", "证据来源"))
        for p in res["pcs"]:
            lines.append("  %-16s %-18s %-8s %s"
                         % (p["no"], p["device_code"] or "(未登记)",
                            "通过" if p["ok"] else "不通过", p["source"]))
        lines += ["", "综合可信度系数：×%.2f" % res["factor"]]
        if res["defects"]:
            lines += ["", "缺陷 %d 项：" % len(res["defects"])]
            lines += ["  [%s] %s  %s" % (d["severity"], d["name"], d.get("detail") or "")
                      for d in res["defects"]]
        else:
            lines += ["", "全部机位通过。"]
        self._set_text(self.txt_admit, "\n".join(lines))
        room = self.store.get_room(res["room_id"])
        self.current = room
        self.refresh_rooms()
        self.msg("② 机位自检完成：%d 个机位、%d 项缺陷、系数 ×%.2f"
                 % (len(res["pcs"]), len(res["defects"]), res["factor"]),
                 ok=not res["defects"])

    def do_step3(self):
        r = self._need_room()
        if not r:
            return
        need = models.to_int(r.invigilators_required, 2) or 1
        inv = [{"name": "监考员%d" % (i + 1), "role": "监考员"} for i in range(need)]
        if r.room_type == config.TYPE_ISOLATION:
            admission.set_invigilators(self.store, r, [],
                                       remote={"name": "远程监考员甲"},
                                       operator=self.operator)
        else:
            admission.set_invigilators(self.store, r, inv, operator=self.operator)
        admission.set_ledger(self.store, r,
                             [{"seat_no": "%02d" % (i + 1),
                               "candidate_id": "C%03d" % (i + 1)}
                              for i in range(min(6, max(1, r.seats // 10)))],
                             operator=self.operator)
        admission.set_calibration(self.store, r, operator=self.operator, region="A 区")
        self.refresh_rooms()
        self.msg("已在岗核验 %d 人、录入座位映射（仅编号）、完成逐考场标定"
                 % (need if r.room_type != config.TYPE_ISOLATION else 1))

    def do_toggle_flag(self, key):
        """
        勾选"已办事项"开关。

        有正规动作的（演练/离线自检/即插即用/医生协同/日志代理/高保真录音）
        走合规留痕入口——要双人、要理由，留痕才完整；
        其余（屏幕审计/去重验证/覆盖核查/替代通道）直接置位。
        """
        r = self._need_room()
        if not r:
            self.var_special_flags[key].set(bool(getattr(r, key, 0)) if r else False)
            return
        on = bool(self.var_special_flags[key].get())
        witness = self.var_witness.get().strip()
        detail = self.var_pvdetail.get().strip()
        ok, info = True, ""
        if key == "drill_done":
            if on:
                ok, info = privacy.do_drill(self.store, r, operator=self.operator,
                                            witness=witness, detail=detail)
            else:
                admission.set_flags(self.store, r, operator=self.operator,
                                    drill_done=0)
        elif key == "offline_ready":
            ok, info = privacy.do_offline_check(self.store, r,
                                                operator=self.operator, offline=on,
                                                note=detail)
        elif key == "fast_enable":
            if on:
                ok, info = privacy.do_fast_enable(self.store, r,
                                                  operator=self.operator,
                                                  note=detail)
            else:
                admission.set_flags(self.store, r, operator=self.operator,
                                    fast_enable=0)
        elif key == "doctor_link":
            if on:
                ok, info = privacy.do_doctor_link(
                    self.store, r, operator=self.operator,
                    doctor=witness or "考点医生（远程）", note=detail)
            else:
                admission.set_flags(self.store, r, operator=self.operator,
                                    doctor_link=0)
        elif key == "network_log":
            if on:
                ok, info = privacy.do_network_log(
                    self.store, r, operator=self.operator,
                    agent=detail or "网管日志代理", note=detail)
            else:
                admission.set_flags(self.store, r, operator=self.operator,
                                    network_log=0)
        elif key == "independent_mic":
            if on:
                ok, info = privacy.do_hifi_audio(
                    self.store, r, operator=self.operator,
                    rate=models.to_int(self.var_rate.get(), 48000), note=detail)
            else:
                admission.set_flags(self.store, r, operator=self.operator,
                                    independent_mic=0)
        else:
            admission.set_flags(self.store, r, operator=self.operator,
                                **{key: 1 if on else 0})
            info = "%s = %s" % (key, on)
        self.current = self.store.get_room(r.room_id)
        self.refresh_privacy()
        self._paint_room()
        self.msg(info if isinstance(info, str) else str(info), ok=ok)

    def do_rate(self):
        r = self._need_room()
        if not r:
            return
        rate = models.to_int(self.var_rate.get(), 0)
        if r.room_type == config.TYPE_ORAL and rate >= 48000:
            # 口试考场：录音链路登记走正规留痕（同时置上独立拾音）
            ok, info = privacy.do_hifi_audio(self.store, r,
                                             operator=self.operator, rate=rate)
        else:
            admission.set_flags(self.store, r, operator=self.operator,
                                audio_sample_rate=rate)
            ok, info = True, "录音采样率已写入：%s Hz" % rate
        self.current = self.store.get_room(r.room_id)
        self.refresh_privacy()
        self._paint_room()
        self.msg(info if isinstance(info, str) else str(info), ok=ok)

    def do_admit(self):
        r = self._need_room()
        if not r:
            return
        self._bind_log(1)
        self.run_task("④ 准入自检（含 02 台账登记与逐机位画质核验）",
                      lambda: self._admit_work(r.room_id),
                      self._admit_done, log_lines=1)

    def _admit_work(self, room_id):
        room = self.store.get_room(room_id)
        res = admission.run_admission(self.store, room, operator=self.operator,
                                      note="界面准入自检")
        return {
            "room_id": room_id, "decision": res["decision"],
            "blocked": res["blocked"], "factor": res["factor"],
            "blockers": res["blockers"], "warnings": res["warnings"],
            "passed": res["passed"], "next_phase_text": res["next_phase_text"],
            "job_defects": [d for d in res["blockers"]
                            if d.get("code") == "CAP_JOB_PENDING"],
        }

    def _render_admit(self, res, title="准入自检结论"):
        lines = ["%s：%s" % (title, res["decision"]), "",
                 "综合可信度系数：×%.2f" % res["factor"], ""]
        if res["blockers"]:
            lines.append("阻断项（不修好不许启用）：")
            lines += ["  [阻断] %s  %s" % (d["name"], d.get("detail") or "")
                      for d in res["blockers"]]
            lines.append("")
        if res["warnings"]:
            lines.append("警告项（可放行但必须留痕）：")
            lines += ["  [警告] %s  %s" % (d["name"], d.get("detail") or "")
                      for d in res["warnings"]]
            lines.append("")
        lines.append("已通过判据：")
        lines += ["  ✓ %s" % x for x in res["passed"]]
        if res.get("job_defects"):
            lines += ["", "提示：上面带「待办事项未完成」的，到「准入自检」页"
                          "勾选对应开关（演练/离线自检/日志/去重…）即可消除。"]
        lines += ["", "下一步：%s" % res["next_phase_text"]]
        return "\n".join(lines)

    def _admit_done(self, res):
        self._set_text(self.txt_admit, self._render_admit(res))
        room = self.store.get_room(res["room_id"])
        self.current = room
        self.refresh_rooms()
        self.msg(res["decision"], ok=not res["blocked"])

    def do_arm(self):
        r = self._need_room()
        if not r:
            return
        self._bind_log(1)
        self.run_task("⑤ 启用（arm）",
                      lambda: self._arm_work(r.room_id), self._arm_done,
                      log_lines=1)

    def _arm_work(self, room_id):
        room = self.store.get_room(room_id)
        res = admission.arm(self.store, room, operator=self.operator)
        return {"room_id": room_id, "armed": bool(res.get("armed")),
                "decision": res["decision"], "factor": res["factor"],
                "blockers": res["blockers"], "warnings": res["warnings"],
                "passed": res["passed"], "blocked": res["blocked"],
                "next_phase_text": res["next_phase_text"],
                "job_defects": [d for d in res["blockers"]
                                if d.get("code") == "CAP_JOB_PENDING"]}

    def _arm_done(self, res):
        self._set_text(self.txt_admit,
                       self._render_admit(res, title="启用结论"))
        room = self.store.get_room(res["room_id"])
        self.current = room
        self.refresh_rooms()
        if res["armed"]:
            self.msg("已启用：%s（%s）" % (room.name, room.status_text()))
        else:
            self.msg("启用被拦住：%s" % res["decision"], ok=False)

    def do_disable(self):
        r = self._need_room()
        if not r:
            return
        admission.disable(self.store, r, operator=self.operator, reason="界面停用")
        self.refresh_rooms()
        self.msg("已停用：%s" % r.name)

    # =====================================================================
    # 动作：分级告警
    # =====================================================================
    def _fill_behaviors(self):
        r = self.current
        codes = (r.enabled_behaviors if r else
                 [b[0] for b in config.BEHAVIORS + config.EXTRA_BEHAVIORS])
        self._beh_map = {}
        vals = []
        for c in codes:
            d = config.behavior_dict().get(c)
            if not d:
                continue
            label = "%s %s" % (c, d["name"])
            self._beh_map[label] = c
            vals.append(label)
        self.cmb_beh.configure(values=vals)
        if vals and self.var_beh.get() not in self._beh_map:
            self.var_beh.set(vals[0])

    def refresh_alerts(self):
        self._fill_behaviors()
        for i in self.tree_al.get_children():
            self.tree_al.delete(i)
        r = self.current
        if r is None:
            return
        self._alert_index = {}
        for a in self.store.list_alerts(room_id=r.room_id, limit=60):
            state = ("已抑制：" + a.suppress_reason[:24]) if a.suppressed else (
                "已送终端" if a.delivered else "已分流")
            iid = self.tree_al.insert("", "end", values=(
                a.ts, a.behavior_name, config.LEVEL_META.get(a.level, {}).get(
                    "name", a.level), "%.2f" % a.confidence, state,
                "、".join(a.pushed_to or [])))
            self._alert_index[iid] = a.event_id

    def _behavior_code(self):
        return self._beh_map.get(self.var_beh.get(), "")

    def _route(self, push):
        r = self._need_room()
        if not r:
            return
        code = self._behavior_code()
        if not code:
            self.msg("先选一个行为", ok=False)
            return
        conf = models.to_float(self.var_conf.get(), 0.9)
        cam = models.to_float(self.var_cam.get(), 1.0)
        frames = models.to_int(self.var_frames.get(), 5)
        dev = (rooms_mod.point_device(r, "侧/后部特写机位")
               or rooms_mod.point_device(r, "前部全景机位"))
        self._bind_log(2)
        self.run_task(
            "行为告警%s（%s）" % ("上报 03" if push else "试判", code),
            lambda: self._route_work(r.room_id, code, conf, dev, frames, cam, push),
            self._route_done, log_lines=2)

    def _route_work(self, room_id, code, conf, dev, frames, cam, push):
        room = self.store.get_room(room_id)
        res = alerting.raise_behavior_alert(
            self.store, room, code, conf, device_code=dev, frames=frames,
            camera_factor=cam, suppress_check=False, push=push,
            operator=self.operator)
        if not res.get("ok"):
            return {"ok": False, "reason": str(res.get("reason"))}
        return {"ok": True, "push": push, "event": res["event"].to_dict(),
                "level": res["level"], "level_name": res["level_name"],
                "delivered": res["delivered"], "suppressed": res["suppressed"],
                "suppress_reason": res["suppress_reason"],
                "pushed_to": res["pushed_to"], "external": res["external"],
                "reason": res["reason"], "camera_factor": res.get("camera_factor"),
                "camera_note": res.get("camera_note"),
                "force_confirm": res.get("force_confirm")}

    def _route_done(self, res):
        if not res.get("ok"):
            self._set_text(self.txt_al, "本次未产生告警：%s" % res.get("reason"))
            self.refresh_alerts()
            self.msg(str(res.get("reason")), ok=False)
            return
        ev = res["event"]
        lines = [
            "行为：%s（%s / %s）" % (ev["behavior_name"], ev["behavior"],
                                   ev.get("category_name") or ""),
            "结论：%s ｜ 置信度 %.3f ｜ 告警码 %s"
            % (res["level_name"], ev["confidence"], ev["alert_code"]),
            "推送对象：%s" % "、".join(res["pushed_to"] or []),
            "响应要求：%s" % ev["response_text"],
            "SLA：%d 秒%s" % (ev["sla_sec"],
                             "（红色强制确认）" if res.get("force_confirm") else ""),
            "画质系数：×%.2f（%s）" % (res.get("camera_factor", 1.0),
                                     res.get("camera_note") or ""),
            "定级说明：%s" % (res.get("reason") or "按行为基级直接定级"),
        ]
        if res.get("suppressed"):
            lines.append("已抑制：%s" % res.get("suppress_reason"))
        ext = res.get("external") or {}
        if ext:
            lines.append("送 03 结果：%s%s"
                         % ("成功，告警号 %s" % ext.get("alert_id")
                            if ext.get("ok")
                            else "失败：%s" % str(ext.get("reason"))[:80],
                            "（03 已按画质系数降级）" if ext.get("downgraded") else ""))
        elif not res["push"]:
            lines.append("（本次是试判，未推送到 03）")
        self._set_text(self.txt_al, "\n".join(lines))
        self.refresh_alerts()
        self.msg("%s -> %s" % (ev["behavior_name"], res["level_name"]),
                 ok=not res.get("suppressed"))

    def do_route_dry(self):
        self._route(False)

    def do_route_push(self):
        self._route(True)

    def do_review(self):
        sel = self.tree_al.selection()
        if not sel:
            self.msg("先在上面选一条告警", ok=False)
            return
        ev = self.store.get_alert(self._alert_index.get(sel[0]))
        ok, info = alerting.resolve_alert(
            self.store, ev, self.var_concl.get(), reason=self.var_reason.get(),
            operator=self.operator)
        if not ok:
            self.msg(str(info), ok=False)
            return
        self.refresh_alerts()
        self.refresh_privacy()
        self.msg("复核已留痕：%s%s" % (info["conclusion_text"],
                                     "（误报已进算法迭代闭环）"
                                     if info["closed_loop"] else ""))

    # =====================================================================
    # 动作：数据分域
    # =====================================================================
    def do_partition(self):
        r = self._need_room()
        if not r:
            return
        p = privacy.ensure_partition(self.store, r, operator=self.operator)
        self.refresh_domain()
        self.msg("已建立分区：%s（%s）"
                 % (config.DOMAIN_RULES.get(p.domain, {}).get("name", p.domain),
                    p.path))

    def refresh_domain(self):
        parts = privacy.partition_view(self.store)
        matrix = privacy.domain_matrix(self.store)
        lines = ["== 数据分域（隔离/机考/涉密强制独立分区 + 独立授权）==", ""]
        for p in parts:
            lines.append("  %-22s %-10s %-6s %-8s 文件 %-3d 到期 %s %s"
                         % (p["room_name"], p["domain_name"],
                            "独立" if p["isolated"] else "常规",
                            p["state"], p["files"], p["destroy_at"] or "-",
                            "【已销毁】" if p["destroyed_at"] else ""))
        lines += ["", "== 域 x 角色 授权矩阵（隔离域默认拒绝，必须独立授权）==", ""]
        for dom, info in matrix.items():
            lines.append("  【%s】%s%s" % (info["name"],
                                        "（独立分区）" if info["isolated"] else "",
                                        "" if info["allow_cross_aggregate"]
                                        else "（禁止跨域汇聚）"))
            for row in info["roles"]:
                lines.append("      %-20s 域规则允许=%-5s 已授权=%-5s 生效=%s"
                             % (row["role_text"], row["by_rule"], row["granted"],
                                row["effective"]))
        comp = privacy.compliance_report(self.store)
        lines += ["", "== 合规体检 ==",
                  "  %d 个考场：%d 个满足强制项，%d 个有缺口"
                  % (comp["total"], comp["ok"], len(comp["fail"]))]
        for row in comp["fail"]:
            lines.append("      %s（%s）缺：%s"
                         % (row["name"], row["room_type_name"],
                            "、".join(row["fail"])))
        self._set_text(self.txt_dom, "\n".join(lines))

    def do_grant(self, grant=True):
        d, role = self.var_domain.get(), self.var_role.get().strip()
        if grant:
            ok, info = privacy.grant(self.store, d, role, operator=self.operator,
                                     reason="界面独立授权")
        else:
            ok, info = privacy.revoke(self.store, d, role, operator=self.operator,
                                      reason="界面撤权")
        self.refresh_domain()
        self.msg(info if isinstance(info, str) else str(info), ok=ok)

    def do_access_test(self):
        d, role = self.var_domain.get(), self.var_role.get().strip()
        rid = self.current.room_id if self.current else None
        ok, why = privacy.check_access(self.store, d, role, room_id=rid,
                                       operator=self.operator, action="界面试访问")
        self.refresh_domain()
        self.msg("%s / %s：%s（%s）" % (d, role, "放行" if ok else "拒绝", why), ok=ok)

    # =====================================================================
    # 动作：合规留痕
    # =====================================================================
    def refresh_privacy(self):
        r = self.current
        comp = privacy.compliance_report(self.store)
        lines = ["== 合规体检 ==",
                 "%d 个考场：%d 个满足强制项，%d 个有缺口"
                 % (comp["total"], comp["ok"], len(comp["fail"]))]
        for row in comp["fail"]:
            lines.append("   %s（%s）" % (row["name"], row["room_type_name"]))
            for c in row["checks"]:
                if not c["ok"]:
                    lines.append("        缺：%s（要求 %s）" % (c["name"], c["require"]))
        if r:
            lines += ["", "当前考场（%s）合规动作：" % r.name]
            for c in (comp["rows"] or []):
                if c["room_id"] == r.room_id:
                    for x in c["checks"]:
                        lines.append("   %s %s" % ("✓" if x["ok"] else "✗", x["name"]))
        self._set_text(self.txt_pv, "\n".join(lines))
        for i in self.tree_pv.get_children():
            self.tree_pv.delete(i)
        for rec in self.store.list_privacy(
                room_id=r.room_id if r else None, limit=80):
            self.tree_pv.insert("", "end", values=(
                rec.ts, rec.action_text(), rec.operator, rec.witness,
                rec.detail[:90]))

    def do_dpia(self):
        r = self._need_room()
        if not r:
            return
        ok, info = privacy.do_dpia(self.store, r, operator=self.operator,
                                   assessor=self.var_witness.get(),
                                   conclusion=self.var_pvdetail.get())
        self.refresh_privacy()
        self.refresh_rooms()
        self.msg(info, ok=ok)

    def do_drill(self):
        r = self._need_room()
        if not r:
            return
        ok, info = privacy.do_drill(self.store, r, operator=self.operator,
                                    witness=self.var_witness.get(),
                                    detail=self.var_pvdetail.get())
        self.refresh_privacy()
        self.refresh_rooms()
        self.msg(info, ok=ok)

    def do_offline(self):
        r = self._need_room()
        if not r:
            return
        ok, info = privacy.do_offline_check(self.store, r, operator=self.operator,
                                            offline=True,
                                            note=self.var_pvdetail.get())
        self.refresh_privacy()
        self.refresh_rooms()
        self.msg(info, ok=ok)

    def do_retention(self):
        r = self._need_room()
        if not r:
            return
        ok, info = privacy.set_retention(self.store, r,
                                         models.to_int(self.var_days.get(), 30),
                                         operator=self.operator)
        self.refresh_privacy()
        self.msg(info, ok=ok)

    def do_seal(self):
        r = self._need_room()
        if not r:
            return
        ok, info = privacy.seal(self.store, r, operator=self.operator)
        self.refresh_privacy()
        self.msg(info, ok=ok)

    def do_destroy(self):
        r = self._need_room()
        if not r:
            return
        if not messagebox.askyesno("确认销毁",
                                   "将删除 %s 的分区文件并出具销毁记录，\n"
                                   "需要见证人（%s）。确定继续？"
                                   % (r.name, self.var_witness.get())):
            return
        ok, info = privacy.destroy(self.store, r, operator=self.operator,
                                   witness=self.var_witness.get(),
                                   reason=self.var_pvdetail.get() or "最短必要期限已到")
        self.refresh_privacy()
        self.refresh_rooms()
        if ok:
            messagebox.showinfo("销毁记录", info["certificate"])
            self.msg(info["certificate"])
        else:
            self.msg(str(info), ok=False)

    # =====================================================================
    # 动作：验收指标
    # =====================================================================
    def _kpi_kwargs(self):
        v = self.var_kpi
        return dict(observe_total=models.to_int(v["total"].get()),
                    observe_hit=models.to_int(v["hit"].get()),
                    observe_correct=models.to_int(v["correct"].get()),
                    observe_false=models.to_int(v["false"].get()),
                    response_sec=models.to_float(v["resp"].get()),
                    camera_online_rate=models.to_float(v["online"].get()),
                    coverage_rate=models.to_float(v["cover"].get()),
                    dedup_expected=models.to_int(v["ddexp"].get()),
                    dedup_counted=models.to_int(v["ddcnt"].get()),
                    clock_drift_sec=models.to_float(v["drift"].get()))

    def _paint_kpi(self, ev):
        lines = ["== %s（%s）验收指标 ==" % (ev["room"]["name"],
                                          ev["room"]["room_type_name"]),
                 ev["summary"]["verdict"], "",
                 "%-22s %-10s %-12s %-8s %s" % ("指标", "实测值", "要求", "结论",
                                                "证据来源")]
        for m in ev["metrics"]:
            val = "—" if m.get("value") is None else (
                "%.3f" % m["value"] if isinstance(m["value"], float) else str(m["value"]))
            verdict = ("达标" if m["meets"] is True else
                       "未达标" if m["meets"] is False else "缺失")
            lines.append("%-22s %-10s %-12s %-8s %s"
                         % (m["name"], val, m["target_text"] or "—", verdict,
                            m["source"][:52]))
        if ev["notes"]:
            lines += ["", "说明："]
            lines += ["  - %s" % n for n in ev["notes"]]
        self._set_text(self.txt_kpi, "\n".join(lines))

    def do_kpi(self):
        r = self._need_room()
        if not r:
            return
        self.kpi_detail_room = r.room_id
        run = kpi_mod.new_run(r, operator=self.operator,
                              data_source=self.var_source.get(),
                              **self._kpi_kwargs())
        ev = kpi_mod.evaluate(self.store, r, run=run, persist=True)
        self._paint_kpi(ev)
        self._refresh_room_tree()
        self.msg(ev["summary"]["verdict"], ok=ev["summary"]["fails"] == 0)

    def do_kpi_baseline(self):
        r = self._need_room()
        if not r:
            return
        self.kpi_detail_room = r.room_id
        run = kpi_mod.run_from_baseline(self.store, r, operator=self.operator,
                                        data_source="synthetic")
        ev = kpi_mod.evaluate(self.store, r, run=run, persist=True)
        self._paint_kpi(ev)
        self._refresh_room_tree()
        self.msg("已用内置基线跑一次（标注为合成/演示数据，不等于真实考场准确率）")

    def do_report(self):
        ok, info = report_mod.write_report(self.store)
        if not ok:
            self.msg(str(info), ok=False)
            return
        self.msg("验收报告已生成：%s" % info["md"])
        messagebox.showinfo("报告已生成",
                            "Markdown：\n%s\n\nCSV：\n%s" % (info["md"], info["csv"]))

    def refresh_kpi(self):
        """考点看板；如果刚看过某个考场的明细，就把明细跟在后面一起显示。"""
        board = kpi_mod.site_summary(self.store)
        lines = ["== 考点验收看板（%d 个考场）==" % board["rooms"],
                 "全达标 %d ｜ 未达标 %d ｜ 部分缺失 %d"
                 % (board["fully_ok"], board["failing"], board["partial_or_missing"]),
                 "", "%-22s %-16s %-10s %s" % ("考场", "类型", "结论", "说明")]
        for b in board["board"]:
            s = b["summary"]
            lines.append("%-22s %-16s 达标 %-3d 未达标 %-3d 缺失 %-3d %s"
                         % (b["name"][:22], b["room_type_name"][:16], s["meets"],
                            s["fails"], s["missing"],
                            "（已录测算）" if b["has_run"] else "（未录测算）"))
        text = "\n".join(lines)
        rid = getattr(self, "kpi_detail_room", None)
        if rid:
            room = self.store.get_room(rid)
            if room is not None:
                ev = kpi_mod.evaluate(self.store, room, run=None, persist=False)
                text += ("\n" + "—" * 62 + "\n" + self._kpi_detail(ev))
        self._set_text(self.txt_kpi, text)

    def _kpi_detail(self, ev):
        lines = ["== %s 明细 ==" % ev["room"]["name"], ev["summary"]["verdict"], ""]
        for m in ev["metrics"]:
            val = "—" if m.get("value") is None else (
                "%.3f" % m["value"] if isinstance(m["value"], float) else str(m["value"]))
            verdict = ("达标" if m["meets"] is True else
                       "未达标" if m["meets"] is False else "缺失")
            lines.append("%-22s %-10s %-12s %-8s %s"
                         % (m["name"], val, m["target_text"] or "—", verdict,
                            m["source"][:52]))
        if ev["notes"]:
            lines += ["", "说明："] + ["  - %s" % n for n in ev["notes"]]
        return "\n".join(lines)

    # =====================================================================
    # 动作：审计与联动
    # =====================================================================
    def refresh_audit(self):
        ok, n, msg = self.store.verify_chain()
        lines = ["审计哈希链：%s" % msg, ""]
        for a in self.store.list_audits(limit=60):
            lines.append("%s  %-16s %-10s %s" % (a.ts, a.action, a.operator,
                                                 a.detail[:70]))
        self._set_text(self.txt_aud, "\n".join(lines))
        self.msg(msg, ok=ok)

    def do_linkage_probe(self):
        st = linkage_client.status(deep=True)
        lines = ["== 00_集成联动 ==",
                 "可用性：%s" % st["linkage_available"],
                 "沙箱：%s" % st["sandbox_root"], ""]
        for k, v in sorted(st["modules"].items()):
            lines.append("[%s] 目录：%s" % (k, "可用" if v["available"]
                                          else "不可用（%s）" % v["reason"]))
            if v.get("status"):
                lines.append("      状态：%s" % str(v["status"])[:150])
            elif v.get("reason"):
                lines.append("      原因：%s" % str(v["reason"])[:150])
        lines += ["", "== 本模块审计 ==",
                  self.store.verify_chain()[2]]
        self._set_text(self.txt_aud, "\n".join(lines))
        self.msg("联动探测完成")

    def do_sandbox(self):
        config.save_settings({"sandbox_root": self.var_sandbox.get().strip()})
        linkage_client.reset()
        s = config.load_settings()
        self.msg("已应用沙箱设置：%s"
                 % (s.get("sandbox_root") or "（真实数据目录）"))


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


# =============================================================================
# 带错误上报的入口（推荐用这个：pythonw.exe 下异常也能被看见）
# =============================================================================
LOG_DIR = os.path.join(HERE, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "launch.log")


def _log(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (models.now_text(), msg))
    except OSError:
        pass


def _alert(title, message):
    """弹窗报错；弹窗失败再退回系统弹窗（总之不能静默）。"""
    _log("ERROR %s | %s" % (title, message.replace("\n", " / ")))
    try:
        from tkinter import messagebox
        box = tk.Tk()
        box.withdraw()
        try:
            box.attributes("-topmost", True)
        except tk.TclError:
            pass
        messagebox.showerror(title, message)
        box.destroy()
    except Exception as e:                                 # noqa: BLE001
        _log("弹窗失败：%s: %s" % (type(e).__name__, e))
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
        except Exception:                                  # noqa: BLE001
            pass


def preflight():
    """启动前把"最容易让人以为双击没反应"的几件事逐项查清楚。"""
    _log("=" * 60)
    _log("启动：exe=%s python=%s" % (sys.executable,
                                   sys.version.split()[0]))
    _log("工作目录：%s" % os.getcwd())
    try:
        import tkinter                                   # noqa: F401
        import sqlite3                                   # noqa: F401
    except ImportError as e:
        return (False, "缺少图形/数据库组件",
                "当前 Python 缺少 %s。\n\n请重装 Python 3.10+，在安装器里勾选：\n"
                "    tcl/tk and IDLE\n    Add python.exe to PATH\n\n当前解释器：\n%s"
                % (e, sys.executable))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        probe = os.path.join(LOG_DIR, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return (False, "数据目录不可写",
                "模块目录下写不了文件：\n%s\n\n%s\n\n"
                "请把整个文件夹拷到有写权限的位置再启动。" % (LOG_DIR, e))
    try:
        st = storage.Store(config.DB_PATH)
        st.close()
        _log("数据库就绪：%s" % config.DB_PATH)
    except Exception as e:                                 # noqa: BLE001
        return (False, "数据初始化失败",
                "%s: %s\n\n完整日志：\n%s" % (type(e).__name__, e, LOG_PATH))
    return (True, "", "")


def run():
    """
    启动入口（给 launcher.py / 启动考场应用界面.bat 调用）：
    任何异常都写 data\\logs\\launch.log 并弹窗，绝不让双击"没反应"。
    """
    ok, title, why = preflight()
    if not ok:
        _alert("普通考场+特殊考场应用 · 启动失败（%s）" % title,
               "%s\n\n（详细日志：%s）" % (why, LOG_PATH))
        return 1
    try:
        _log("进入主界面")
        rc = main()
        _log("正常退出 rc=%s" % rc)
        return rc or 0
    except Exception as e:                                 # noqa: BLE001
        tb = traceback.format_exc()
        _log("运行异常：\n%s" % tb)
        _alert("普通考场+特殊考场应用 · 运行出错",
               "界面启动过程中出错：\n\n%s: %s\n\n"
               "完整堆栈已写入：\n%s" % (type(e).__name__, e, LOG_PATH))
        return 1


if __name__ == "__main__":
    sys.exit(run())
