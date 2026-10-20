# -*- coding: utf-8 -*-
r"""
06 智能安检与手机作弊器材检测 · 桌面界面（tkinter，仅标准库）

七个页签，对着《智能安检与手机作弊器材检测.md》的四个环节落地：

  ① 总览            指标卡片 + 实时过检 + 待处置报警 + 设备状态（一眼看现场）
  ② 设备与通道      安检门/手持探测器/储物柜/鞋底仪登记、接入、探活
  ③ 过检与报警      过检录入（人工/模拟）+ 报警队列 + 复检处置 + 上报 03
  ④ 手机保管柜      柜体格位图 + 存/取/应急开柜/故障 + 存取留痕 + 丢失追溯
  ⑤ 特殊考生通道    孕妇/植入医疗器械等人工安检通道登记（双人见证）+ 演练
  ⑥ 样本测试与验收  样本库 + 判据回放 + 现场实测 + 检出率/误报率 + 验收报告
  ⑦ 设置            考点/操作员/判据模式/阈值/上报开关/模拟参数/隐藏命令行

运行：python app.py      （或双击「启动.bat」）
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import ui_theme
except Exception:                                            # noqa: BLE001
    ui_theme = None

import config
import data_crypto
import flow as flow_mod
import gate
import linkage_bridge
import locker as locker_mod
import models
import reinspect as reinspect_mod
import report as report_mod
import samples as samples_mod
import special as special_mod
import storage

FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 10, "bold")
FONT_T = ("Microsoft YaHei", 15, "bold")
FONT_S = ("Microsoft YaHei", 9)

C_OK = "#0a7d28"
C_WARN = "#e08b00"
C_BAD = "#c00000"
C_DIM = "#555555"
CELL_COLOR = {config.CELL_FREE: "#e8f5e9", config.CELL_OCCUPIED: "#ffe0b2",
              config.CELL_LOCKED: "#ffcdd2", config.CELL_FAULT: "#e0e0e0"}
CELL_FG = {config.CELL_FREE: "#1b5e20", config.CELL_OCCUPIED: "#e65100",
           config.CELL_LOCKED: "#b71c1c", config.CELL_FAULT: "#616161"}


def label_to_key(mapping, text, default=""):
    for k, v in mapping.items():
        if v == text:
            return k
    return default or text


class App(object):
    def __init__(self, root):
        self.root = root
        root.title("智能安检与手机作弊器材检测 · 项目六专题06")
        root.geometry("1320x860")
        root.minsize(1100, 720)

        self.store = storage.Store()
        samples_mod.ensure_builtin(self.store)
        self.cfg = config.load_settings()
        self.events = queue.Queue()
        self.manager = gate.IngestManager(self.store, on_event=self._on_gate_event)
        self._grid_locker = None
        self._log_lines = []
        self._stop = False
        if ui_theme is not None:
            ui_theme.apply(self.root)

        self._build_header()
        self._build_tabs()
        self._build_status()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_all()
        if ui_theme is not None:
            ui_theme.polish(self.root)
            ui_theme.polish_later(self.root, 600)
        self.root.after(1000, self._tick)

    # ---------------- 事件与刷新 ----------------
    def _on_gate_event(self, ev, alarm):
        """接入线程回调（非 UI 线程）——只入队，不碰界面。"""
        self.events.put((ev, alarm))

    def _tick(self):
        drained = 0
        while drained < 50:
            try:
                ev, alarm = self.events.get_nowait()
            except queue.Empty:
                break
            drained += 1
            note = "过检 %s %s" % (ev.device_code, ev.result)
            if alarm is not None:
                note += " -> %s %s" % (alarm.level_text(), alarm.kinds_text())
            self._log(note)
        if drained:
            self.refresh_all(light=True)
        if int(time.time()) % 5 == 0:
            try:
                locker_mod.scan_overdue(self.store, link=None)
            except Exception:
                pass
        if not self._stop:
            self.root.after(1000, self._tick)

    def _log(self, text):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        self._log_lines.append(line)
        del self._log_lines[:-200]
        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", line + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except Exception:
            pass

    # ---------------- 顶部 ----------------
    def _build_header(self):
        top = tk.Frame(self.root, bg="#12395c", height=64)
        top.pack(fill="x")
        tk.Label(top, text="  智能安检与手机作弊器材检测", bg="#12395c", fg="white",
                 font=FONT_T).pack(side="left", pady=12)
        tk.Label(top, text="事前阻断：安检门 → 复检 → 手机保管 → 人-物-时闭环",
                 bg="#12395c", fg="#bcd7ef", font=FONT).pack(side="left", padx=10)
        self.var_site = tk.StringVar(value="考点：%s" % self.cfg.get("site", ""))
        tk.Label(top, textvariable=self.var_site, bg="#12395c", fg="#ffd54f",
                 font=FONT_B).pack(side="right", padx=18)

    # ---------------- 页签 ----------------
    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self.tab_overview = tk.Frame(self.nb)
        self.tab_devices = tk.Frame(self.nb)
        self.tab_alarms = tk.Frame(self.nb)
        self.tab_lockers = tk.Frame(self.nb)
        self.tab_special = tk.Frame(self.nb)
        self.tab_test = tk.Frame(self.nb)
        self.tab_settings = tk.Frame(self.nb)
        for frame, title in ((self.tab_overview, "① 总览"), (self.tab_devices, "② 设备与通道"),
                             (self.tab_alarms, "③ 过检与报警"), (self.tab_lockers, "④ 手机保管柜"),
                             (self.tab_special, "⑤ 特殊考生通道"),
                             (self.tab_test, "⑥ 样本测试与验收"),
                             (self.tab_settings, "⑦ 设置")):
            self.nb.add(frame, text=title)
        self._build_overview()
        self._build_devices()
        self._build_alarms()
        self._build_lockers()
        self._build_special()
        self._build_test()
        self._build_settings()

    def _build_status(self):
        bar = tk.Frame(self.root, bg="#eeeeee", height=26)
        bar.pack(fill="x", side="bottom")
        self.var_status = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.var_status, bg="#eeeeee", anchor="w",
                 font=FONT_S).pack(side="left", padx=8)
        self.var_link = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.var_link, bg="#eeeeee", fg=C_DIM,
                 font=FONT_S).pack(side="right", padx=8)

    # ================= ① 总览 =================
    def _build_overview(self):
        f = self.tab_overview
        cards = tk.Frame(f)
        cards.pack(fill="x", padx=8, pady=8)
        self.card_vars = {}
        specs = [("passes", "今日过检"), ("alarms", "报警"), ("pending", "待复检"),
                 ("ppm", "通过效率"), ("cells", "保管柜占用"), ("test", "样本检出率"),
                 ("false", "误报率"), ("devices", "设备在线")]
        for i, (key, title) in enumerate(specs):
            box = tk.Frame(cards, bd=1, relief="solid", padx=10, pady=6)
            box.grid(row=0, column=i, padx=4, sticky="nsew")
            cards.grid_columnconfigure(i, weight=1)
            tk.Label(box, text=title, font=FONT_S, fg=C_DIM).pack(anchor="w")
            v = tk.StringVar(value="-")
            self.card_vars[key] = v
            tk.Label(box, textvariable=v, font=("Microsoft YaHei", 16, "bold")).pack(anchor="w")

        mid = tk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        left = tk.LabelFrame(mid, text=" 实时过检（最近 30 条）")
        left.pack(side="left", fill="both", expand=True)
        self.tv_pass = self._tree(left, [("时间", 150), ("设备", 110), ("考生", 130),
                                         ("结果", 90), ("类别", 150), ("来源", 70)],
                                 height=13)
        right = tk.LabelFrame(mid, text=" 待处置报警（红在前、超时优先）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.tv_alarm_top = self._tree(right, [("时间", 150), ("级别", 80), ("类别", 120),
                                               ("设备", 100), ("考生", 120), ("状态", 80)],
                                       height=13)

        bottom = tk.Frame(f)
        bottom.pack(fill="both", expand=True, padx=8, pady=4)
        bl = tk.LabelFrame(bottom, text=" 设备与接入状态")
        bl.pack(side="left", fill="both", expand=True)
        self.tv_dev_top = self._tree(bl, [("编码", 110), ("名称", 150), ("类型", 100),
                                          ("接入", 110), ("状态", 70), ("心跳", 150)],
                                     height=8)
        br = tk.LabelFrame(bottom, text=" 运行日志")
        br.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.txt_log = tk.Text(br, height=8, font=FONT_S, state="disabled", wrap="none")
        self.txt_log.pack(fill="both", expand=True)

        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(btns, text="🔄 刷新", command=self.refresh_all).pack(side="left")
        tk.Button(btns, text="⚡ 一键演示（登记设备 + 开模拟器 + 跑回放测试）",
                  bg="#ffe082", command=self.one_click_demo).pack(side="left", padx=8)
        tk.Button(btns, text="🛑 停止全部接入", command=self.stop_all_ingest).pack(side="left")
        tk.Button(btns, text="🧪 复检超时上报（03）",
                  command=self.escalate).pack(side="left", padx=8)

    # ================= ② 设备与通道 =================
    def _build_devices(self):
        f = self.tab_devices
        top = tk.Frame(f)
        top.pack(fill="both", expand=True, padx=8, pady=6)
        self.tv_dev = self._tree(top, [("编码", 110), ("名称", 160), ("类型", 100),
                                       ("接入", 110), ("地址", 190), ("通道", 70),
                                       ("考场", 90), ("灵敏度", 70), ("状态", 70)],
                                 height=14)
        form = tk.LabelFrame(f, text=" 设备登记 / 修改（选中表格行后修改再保存）")
        form.pack(fill="x", padx=8, pady=4)
        self.dv = {}
        fields = [("编码", "code", 14), ("名称", "name", 18), ("地址/端口", "address", 22),
                  ("通道号", "channel_no", 8), ("关联考场", "room_id", 10),
                  ("灵敏度", "sensitivity", 8)]
        row = tk.Frame(form)
        row.pack(fill="x", padx=6, pady=4)
        for title, key, width in fields:
            tk.Label(row, text=title, font=FONT_S).pack(side="left")
            e = tk.Entry(row, width=width, font=FONT)
            e.pack(side="left", padx=(2, 10))
            self.dv[key] = e
        row2 = tk.Frame(form)
        row2.pack(fill="x", padx=6, pady=4)
        tk.Label(row2, text="类型", font=FONT_S).pack(side="left")
        self.cb_dev_kind = ttk.Combobox(row2, width=14, state="readonly",
                                        values=[config.DEV_KIND_TEXT[k] for k in config.DEV_KINDS])
        self.cb_dev_kind.current(0)
        self.cb_dev_kind.pack(side="left", padx=(2, 10))
        tk.Label(row2, text="接入方式", font=FONT_S).pack(side="left")
        self.cb_proto = ttk.Combobox(row2, width=14, state="readonly",
                                     values=[config.PROTO_TEXT[k] for k in config.PROTOS])
        self.cb_proto.set(config.PROTO_TEXT[config.PROTO_SIM])
        self.cb_proto.pack(side="left", padx=(2, 10))
        tk.Label(row2, text="（地址：http/tcp 写 ip:端口；串口写 COM3；模拟器不用填）",
                 font=FONT_S, fg=C_DIM).pack(side="left")

        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=4)
        for text, cmd in (("➕ 新增", self.device_add), ("💾 保存修改", self.device_save),
                          ("🗑 删除", self.device_delete), ("▶ 启动接入", self.device_start),
                          ("⏹ 停止接入", self.device_stop), ("📡 探活", self.device_probe),
                          ("🧹 清空表单", self.device_clear)):
            tk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)
        self.tv_dev.bind("<<TreeviewSelect>>", lambda e: self.device_pick())
        tk.Label(f, text="协议：真设备按「设备接入协议」推一行 JSON 到 POST /api/pass 或 TCP 端口；"
                         "接不上的设备一律用「内置模拟器」，来源标 sim，绝不冒充真机。",
                 font=FONT_S, fg=C_DIM, wraplength=1200, justify="left").pack(
            fill="x", padx=10, pady=(0, 6))

    # ================= ③ 过检与报警 =================
    def _build_alarms(self):
        f = self.tab_alarms
        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=6)
        left = tk.LabelFrame(body, text=" 报警队列")
        left.pack(side="left", fill="both", expand=True)
        self.tv_alarm = self._tree(left, [("时间", 140), ("报警号", 110), ("级别", 70),
                                          ("类别", 120), ("设备", 100), ("考生", 110),
                                          ("状态", 80), ("上报", 60)], height=15)
        self.tv_alarm.bind("<<TreeviewSelect>>", lambda e: self.alarm_pick())
        filt = tk.Frame(left)
        filt.pack(fill="x", pady=2)
        self.var_pending_only = tk.BooleanVar(value=True)
        tk.Checkbutton(filt, text="只看待处置", variable=self.var_pending_only,
                       command=self.refresh_alarms).pack(side="left")
        tk.Button(filt, text="📤 上报 03 智能终端", command=self.alarm_report).pack(
            side="left", padx=6)
        tk.Button(filt, text="🔄 刷新", command=self.refresh_alarms).pack(side="left")

        right = tk.LabelFrame(body, text=" 复检与处置（报警不是结论，复检才是）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.al = {}
        r1 = tk.Frame(right)
        r1.pack(fill="x", padx=6, pady=3)
        tk.Label(r1, text="报警号", font=FONT_S).pack(side="left")
        self.al["alarm_id"] = tk.Entry(r1, width=14, font=FONT)
        self.al["alarm_id"].pack(side="left", padx=(2, 10))
        tk.Label(r1, text="复检方式", font=FONT_S).pack(side="left")
        self.cb_method = ttk.Combobox(r1, width=16, state="readonly",
                                      values=list(config.REINSPECT_METHODS.values()))
        self.cb_method.set(config.REINSPECT_METHODS["handheld"])
        self.cb_method.pack(side="left", padx=(2, 10))
        tk.Label(r1, text="手持器编码", font=FONT_S).pack(side="left")
        self.al["handheld_code"] = tk.Entry(r1, width=12, font=FONT)
        self.al["handheld_code"].pack(side="left")
        r2 = tk.Frame(right)
        r2.pack(fill="x", padx=6, pady=3)
        tk.Label(r2, text="处置结论", font=FONT_S).pack(side="left")
        self.cb_result = ttk.Combobox(r2, width=22, state="readonly",
                                      values=list(config.REINSPECT_RESULTS.values()))
        self.cb_result.set(config.REINSPECT_RESULTS["release"])
        self.cb_result.pack(side="left", padx=(2, 10))
        tk.Label(r2, text="查获类别", font=FONT_S).pack(side="left")
        self.cb_found = ttk.Combobox(r2, width=12, state="readonly",
                                     values=[config.KIND_TEXT[k] for k in config.ALARM_KINDS])
        self.cb_found.set(config.KIND_TEXT[config.KIND_MOBILE])
        self.cb_found.pack(side="left", padx=(2, 10))
        tk.Label(r2, text="查获物", font=FONT_S).pack(side="left")
        self.al["found_detail"] = tk.Entry(r2, width=18, font=FONT)
        self.al["found_detail"].pack(side="left")
        r3 = tk.Frame(right)
        r3.pack(fill="x", padx=6, pady=3)
        tk.Label(r3, text="操作人", font=FONT_S).pack(side="left")
        self.al["operator"] = tk.Entry(r3, width=12, font=FONT)
        self.al["operator"].insert(0, self.cfg.get("operator", ""))
        self.al["operator"].pack(side="left", padx=(2, 10))
        tk.Label(r3, text="见证人（特殊考生必填）", font=FONT_S).pack(side="left")
        self.al["witness"] = tk.Entry(r3, width=12, font=FONT)
        self.al["witness"].pack(side="left", padx=(2, 10))
        tk.Label(r3, text="保管柜（查获时自动入柜）", font=FONT_S).pack(side="left")
        self.cb_lock_for_detain = ttk.Combobox(r3, width=14, state="readonly")
        self.cb_lock_for_detain.pack(side="left")
        r4 = tk.Frame(right)
        r4.pack(fill="x", padx=6, pady=3)
        tk.Label(r4, text="备注", font=FONT_S).pack(side="left")
        self.al["note"] = tk.Entry(r4, width=52, font=FONT)
        self.al["note"].pack(side="left")
        rb = tk.Frame(right)
        rb.pack(fill="x", padx=6, pady=6)
        tk.Button(rb, text="✅ 开始复检", command=self.reinspect_start).pack(side="left")
        tk.Button(rb, text="💾 提交处置结论", bg="#c8e6c9",
                  command=self.reinspect_finish).pack(side="left", padx=6)
        tk.Button(rb, text="🤱 转人工安检通道（特殊考生）",
                  command=self.reinspect_special).pack(side="left", padx=6)

        manual = tk.LabelFrame(right, text=" 人工录入一次过检（复检重过门 / 演练 / 无设备时）")
        manual.pack(fill="x", padx=6, pady=6)
        self.mn = {}
        mr1 = tk.Frame(manual)
        mr1.pack(fill="x", pady=3)
        for title, key, width in (("设备编码", "device_code", 12), ("考生号", "candidate_id", 14),
                                  ("姓名", "candidate_name", 10), ("座位", "seat_no", 6),
                                  ("考场", "room_id", 8), ("耗时ms", "duration_ms", 8)):
            tk.Label(mr1, text=title, font=FONT_S).pack(side="left")
            e = tk.Entry(mr1, width=width, font=FONT)
            e.pack(side="left", padx=(2, 8))
            self.mn[key] = e
        mr2 = tk.Frame(manual)
        mr2.pack(fill="x", pady=3)
        for key in config.SENSOR_KEYS:
            tk.Label(mr2, text=config.SENSOR_TEXT[key].split("(")[0], font=FONT_S).pack(side="left")
            e = tk.Entry(mr2, width=6, font=FONT)
            e.insert(0, "0.0")
            e.pack(side="left", padx=(2, 8))
            self.mn["s_" + key] = e
        tk.Button(mr2, text="🧮 判定并留痕", command=self.manual_pass).pack(side="left", padx=6)
        tk.Label(mr2, text="（六路感应强度 0~1，阈值见设置页）", font=FONT_S,
                 fg=C_DIM).pack(side="left")

    # ================= ④ 手机保管柜 =================
    def _build_lockers(self):
        f = self.tab_lockers
        top = tk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        lf = tk.LabelFrame(top, text=" 柜体")
        lf.pack(side="left", fill="both", expand=True)
        self.tv_locker = self._tree(lf, [("编码", 90), ("名称", 140), ("位置", 110),
                                         ("柜格", 60), ("已存", 60), ("空闲", 60),
                                         ("使用率", 70)], height=7)
        self.tv_locker.bind("<<TreeviewSelect>>", lambda e: self.locker_pick())
        add = tk.Frame(lf)
        add.pack(fill="x", pady=3)
        self.lk = {}
        for title, key, width in (("编码", "code", 10), ("名称", "name", 12),
                                  ("位置", "site", 10), ("柜格数", "cells_total", 6)):
            tk.Label(add, text=title, font=FONT_S).pack(side="left")
            e = tk.Entry(add, width=width, font=FONT)
            e.pack(side="left", padx=(2, 8))
            self.lk[key] = e
        tk.Button(add, text="➕ 登记柜体", command=self.locker_add).pack(side="left")

        op = tk.LabelFrame(top, text=" 存 / 取（人脸或扫码开柜，全部留痕）")
        op.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.lo = {}
        for title, key, width in (("考生号", "candidate_id", 14), ("姓名", "candidate_name", 10),
                                  ("手机型号", "device_desc", 14)):
            r = tk.Frame(op)
            r.pack(fill="x", padx=6, pady=2)
            tk.Label(r, text=title, font=FONT_S, width=8, anchor="w").pack(side="left")
            e = tk.Entry(r, width=width, font=FONT)
            e.pack(side="left")
            self.lo[key] = e
        r = tk.Frame(op)
        r.pack(fill="x", padx=6, pady=2)
        tk.Label(r, text="柜号", font=FONT_S, width=8, anchor="w").pack(side="left")
        self.lo["cell_no"] = tk.Entry(r, width=10, font=FONT)
        self.lo["cell_no"].pack(side="left")
        tk.Label(r, text="开柜方式", font=FONT_S).pack(side="left", padx=(8, 2))
        self.cb_open = ttk.Combobox(r, width=14, state="readonly",
                                    values=list(config.OPEN_METHODS.values()))
        self.cb_open.set(config.OPEN_METHODS["face"])
        self.cb_open.pack(side="left")
        rb = tk.Frame(op)
        rb.pack(fill="x", padx=6, pady=4)
        tk.Button(rb, text="📥 存入手机", command=self.locker_store).pack(side="left")
        tk.Button(rb, text="📤 取走手机", command=self.locker_take).pack(side="left", padx=6)
        tk.Button(rb, text="🔓 应急开柜（须写理由）",
                  command=self.locker_emergency).pack(side="left", padx=6)
        tk.Button(rb, text="🔧 柜门故障标记", command=self.locker_fault).pack(side="left")
        r = tk.Frame(op)
        r.pack(fill="x", padx=6, pady=2)
        tk.Label(r, text="应急理由", font=FONT_S, width=8, anchor="w").pack(side="left")
        self.lo["reason"] = tk.Entry(r, width=44, font=FONT)
        self.lo["reason"].pack(side="left")

        gridbox = tk.LabelFrame(f, text=" 柜体格位图（点格子选中该格；颜色：绿=空闲 橙=已存 灰=故障）")
        gridbox.pack(fill="x", padx=8, pady=4)
        self.cell_grid = tk.Frame(gridbox)
        self.cell_grid.pack(fill="x", padx=6, pady=6)

        logs = tk.LabelFrame(f, text=" 存取留痕 / 丢失追溯（按考生号过滤后点「追溯」）")
        logs.pack(fill="both", expand=True, padx=8, pady=4)
        self.tv_locker_log = self._tree(logs, [("时间", 150), ("柜体", 90), ("柜格", 70),
                                               ("动作", 90), ("方式", 100), ("考生", 110),
                                               ("物品", 140), ("操作人", 90), ("备注", 260)],
                                       height=8)
        lbtns = tk.Frame(logs)
        lbtns.pack(fill="x", pady=2)
        tk.Button(lbtns, text="🔎 追溯（用上面的考生号）", command=self.locker_trace).pack(side="left")
        tk.Button(lbtns, text="🔄 刷新", command=self.refresh_lockers).pack(side="left", padx=6)
        tk.Button(lbtns, text="⏰ 超时未取扫描", command=self.locker_scan).pack(side="left")

    # ================= ⑤ 特殊考生通道 =================
    def _build_special(self):
        f = self.tab_special
        form = tk.LabelFrame(f, text=" 人工安检通道登记（孕妇 / 植入医疗器械 / 残障等；双人见证）")
        form.pack(fill="x", padx=8, pady=6)
        self.sp = {}
        rows = [("考生号", "candidate_id", 16), ("姓名", "candidate_name", 10),
                ("准考证号", "ticket_no", 14), ("考场", "room_id", 8),
                ("座位", "seat_no", 6), ("主检人", "inspector", 10),
                ("见证人", "witness", 10)]
        for title, key, width in rows:
            tk.Label(form, text=title, font=FONT_S).pack(side="left", padx=(6, 2))
            e = tk.Entry(form, width=width, font=FONT)
            e.pack(side="left")
            self.sp[key] = e
        r = tk.Frame(form)
        r.pack(fill="x", padx=6, pady=4)
        tk.Label(r, text="原因", font=FONT_S).pack(side="left")
        self.cb_reason = ttk.Combobox(r, width=22, state="readonly",
                                      values=list(config.SPECIAL_REASONS.values()))
        self.cb_reason.set(config.SPECIAL_REASONS["pregnant"])
        self.cb_reason.pack(side="left", padx=(2, 10))
        tk.Label(r, text="处置", font=FONT_S).pack(side="left")
        self.cb_sp_result = ttk.Combobox(r, width=16, state="readonly",
                                        values=list(config.SPECIAL_RESULTS.values()))
        self.cb_sp_result.set(config.SPECIAL_RESULTS["passed"])
        self.cb_sp_result.pack(side="left", padx=(2, 10))
        self.var_drill = tk.BooleanVar(value=False)
        tk.Checkbutton(r, text="演练记录（验收证据）", variable=self.var_drill).pack(side="left")
        tk.Label(r, text="替代方式/说明", font=FONT_S).pack(side="left", padx=(10, 2))
        self.sp["decision"] = tk.Entry(r, width=34, font=FONT)
        self.sp["decision"].insert(0, "人工手检 + 手持探测器，不强制过安检门")
        self.sp["decision"].pack(side="left")
        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=4)
        tk.Button(btns, text="💾 登记人工安检", command=self.special_save).pack(side="left")
        tk.Button(btns, text="🧪 生成替代通道演练记录", command=self.special_drill).pack(
            side="left", padx=6)
        tk.Button(btns, text="📤 上报 03（留痕）", command=self.special_report).pack(side="left", padx=6)
        tk.Button(btns, text="🔄 刷新", command=self.refresh_special).pack(side="left")
        tk.Label(f, text="合规要点：" + "；".join(special_mod.policy_notes()[:3]),
                 font=FONT_S, fg=C_DIM, wraplength=1250, justify="left").pack(
            fill="x", padx=10, pady=(0, 4))
        self.tv_special = self._tree(f, [("时间", 150), ("考生", 120), ("原因", 150),
                                         ("主检", 90), ("见证", 90), ("处置", 140),
                                         ("类型", 70), ("说明", 320)], height=12)
        self.tv_special.pack(fill="both", expand=True, padx=8, pady=6)

    # ================= ⑥ 样本测试与验收 =================
    def _build_test(self):
        f = self.tab_test
        body = tk.Frame(f)
        body.pack(fill="both", expand=True, padx=8, pady=6)
        left = tk.LabelFrame(body, text=" 样本库（ground truth：检出率/误报率的分母）")
        left.pack(side="left", fill="both", expand=True)
        self.tv_sample = self._tree(left, [("编号", 80), ("名称", 200), ("类别", 100),
                                           ("应报警", 70), ("说明", 380)], height=16)
        lb = tk.Frame(left)
        lb.pack(fill="x", pady=3)
        tk.Button(lb, text="▶ 跑判据回放（验算法/阈值）", command=self.run_replay).pack(side="left")
        tk.Button(lb, text="➕ 加样本（用当前六路值）", command=self.add_sample).pack(
            side="left", padx=6)
        tk.Button(lb, text="📤 导出样本", command=self.export_samples).pack(side="left")
        tk.Button(lb, text="📥 导入样本", command=self.import_samples).pack(side="left", padx=6)

        right = tk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        fr = tk.LabelFrame(right, text=" 现场实测录入（真设备跑真实样本 —— 这才是验收证据）")
        fr.pack(fill="x")
        r1 = tk.Frame(fr)
        r1.pack(fill="x", padx=6, pady=3)
        tk.Label(r1, text="样本", font=FONT_S).pack(side="left")
        self.cb_field_sample = ttk.Combobox(r1, width=30, state="readonly")
        self.cb_field_sample.pack(side="left", padx=(2, 10))
        tk.Label(r1, text="设备报了吗", font=FONT_S).pack(side="left")
        self.var_got = tk.StringVar(value="alarm")
        tk.Radiobutton(r1, text="报了", variable=self.var_got, value="alarm").pack(side="left")
        tk.Radiobutton(r1, text="没报", variable=self.var_got, value="clear").pack(side="left")
        r2 = tk.Frame(fr)
        r2.pack(fill="x", padx=6, pady=3)
        tk.Label(r2, text="操作人", font=FONT_S).pack(side="left")
        self.e_field_operator = tk.Entry(r2, width=12, font=FONT)
        self.e_field_operator.insert(0, self.cfg.get("operator", ""))
        self.e_field_operator.pack(side="left", padx=(2, 10))
        tk.Button(r2, text="✍ 录入这条结果", command=self.field_record).pack(side="left")
        tk.Button(r2, text="🏁 结束本次实测并出指标", command=self.field_finish).pack(
            side="left", padx=6)
        self.var_field_run = tk.StringVar(value="（尚未开始现场实测）")
        tk.Label(fr, textvariable=self.var_field_run, font=FONT_S, fg=C_DIM).pack(
            fill="x", padx=6, pady=(0, 4))

        mf = tk.LabelFrame(right, text=" 指标（检出率只来自样本测试；误报率只来自无害样本）")
        mf.pack(fill="both", expand=True, pady=6)
        self.txt_metrics = tk.Text(mf, height=14, font=FONT_S, wrap="word")
        self.txt_metrics.pack(fill="both", expand=True, padx=4, pady=4)

        mb = tk.Frame(right)
        mb.pack(fill="x")
        tk.Button(mb, text="📄 生成验收证据报告（Markdown + CSV）",
                  bg="#ffe082", command=self.gen_report).pack(side="left")
        tk.Button(mb, text="📂 打开报告目录", command=self.open_report_dir).pack(
            side="left", padx=6)
        tk.Button(mb, text="📊 刷新指标", command=self.refresh_metrics).pack(side="left")

    # ================= ⑦ 设置 =================
    def _build_settings(self):
        f = self.tab_settings
        form = tk.LabelFrame(f, text=" 运行设置（保存到 data\\security_settings.json）")
        form.pack(fill="x", padx=8, pady=8)
        self.st = {}
        r1 = tk.Frame(form)
        r1.pack(fill="x", padx=8, pady=4)
        for title, key, width in (("考点", "site", 18), ("默认操作员", "operator", 12)):
            tk.Label(r1, text=title, font=FONT_S).pack(side="left")
            e = tk.Entry(r1, width=width, font=FONT)
            e.insert(0, str(self.cfg.get(key, "")))
            e.pack(side="left", padx=(2, 14))
            self.st[key] = e
        tk.Label(r1, text="判据模式", font=FONT_S).pack(side="left")
        self.cb_mode = ttk.Combobox(r1, width=18, state="readonly",
                                    values=[config.GATE_MODE_TEXT[k] for k in config.GATE_MODE_SCALE])
        self.cb_mode.set(config.GATE_MODE_TEXT.get(self.cfg.get("gate_mode"), "标准"))
        self.cb_mode.pack(side="left", padx=(2, 14))

        thr = tk.LabelFrame(form, text=" 判据阈值（0~1，越大越不敏感；调完点保存即生效）")
        thr.pack(fill="x", padx=8, pady=6)
        rt = tk.Frame(thr)
        rt.pack(fill="x", padx=6, pady=4)
        self.th = {}
        for key in config.SENSOR_KEYS:
            tk.Label(rt, text=config.SENSOR_TEXT[key].split("(")[0], font=FONT_S).pack(side="left")
            e = tk.Entry(rt, width=7, font=FONT)
            e.insert(0, str(self.cfg["thresholds"].get(key, config.DEFAULT_THRESHOLDS[key])))
            e.pack(side="left", padx=(2, 12))
            self.th[key] = e

        r2 = tk.Frame(form)
        r2.pack(fill="x", padx=8, pady=4)
        self.var_double = tk.BooleanVar(value=bool(self.cfg.get("double_confirm", True)))
        tk.Checkbutton(r2, text="双判据确认（≥2 路超阈值报红，单路报橙+复检）",
                       variable=self.var_double).pack(side="left")
        self.var_report03 = tk.BooleanVar(value=bool(self.cfg.get("report_to_terminal")))
        tk.Checkbutton(r2, text="报警实时上报 03 智能终端",
                       variable=self.var_report03).pack(side="left", padx=12)
        self.var_backup = tk.BooleanVar(value=bool(self.cfg.get("auto_open_backup", True)))
        tk.Checkbutton(r2, text="排队超阈值自动提示加开备用通道",
                       variable=self.var_backup).pack(side="left")
        r3 = tk.Frame(form)
        r3.pack(fill="x", padx=8, pady=4)
        for title, key, width in (("保管超时提醒(分钟)", "locker_overdue_min", 6),
                                  ("模拟器间隔(秒)", "sim_interval_sec", 6),
                                  ("模拟器报警比例", "sim_alarm_rate", 6)):
            tk.Label(r3, text=title, font=FONT_S).pack(side="left")
            e = tk.Entry(r3, width=width, font=FONT)
            e.insert(0, str(self.cfg.get(key, "")))
            e.pack(side="left", padx=(2, 14))
            self.st[key] = e
        self.var_hide = tk.BooleanVar(value=config.HIDE_CMD_FLAG.exists())
        tk.Checkbutton(r3, text="隐藏命令行窗口启动（写 data\\hide_cmd.on）",
                       variable=self.var_hide).pack(side="left")
        r4 = tk.Frame(form)
        r4.pack(fill="x", padx=8, pady=4)
        self.var_encrypt = tk.BooleanVar(value=bool(self.cfg.get("encrypt_sensitive", True)))
        tk.Checkbutton(r4, text="考生标识字段加密落库（Fernet；缺 cryptography 自动降级并提示）",
                       variable=self.var_encrypt).pack(side="left")
        tk.Label(r4, text=data_crypto.status_text(), font=FONT_S, fg=C_DIM,
                 wraplength=1100, justify="left").pack(side="left", padx=10)

        btns = tk.Frame(f)
        btns.pack(fill="x", padx=8, pady=6)
        tk.Button(btns, text="💾 保存设置", bg="#c8e6c9", command=self.save_settings).pack(side="left")
        tk.Button(btns, text="🔄 还原表单", command=self.reload_settings).pack(side="left", padx=6)
        tk.Button(btns, text="🔐 校验审计哈希链", command=self.check_audit).pack(side="left", padx=6)
        tk.Button(btns, text="📂 打开数据目录", command=self.open_data_dir).pack(side="left", padx=6)
        tk.Button(btns, text="🧹 清理 30 天前运行数据（保留审计）",
                  command=self.purge).pack(side="left", padx=6)

        info = tk.LabelFrame(f, text=" 环境与口径")
        info.pack(fill="both", expand=True, padx=8, pady=4)
        lines = []
        for k, v in config.describe().items():
            lines.append("%s：%s" % (k, v))
        lines.append("")
        lines.append("检出率 = 应报警且报了 / 应报警样本数；误报率 = 无害样本却报警 / 无害样本数。")
        lines.append("模拟器产生的数据 source=sim，一律不计入检出率与误报率。")
        lines.append("报警上报链路：06 -> 00_集成联动 -> 03 智能终端（告警分级与复核留痕在 03）。")
        tk.Label(info, text="\n".join(lines), font=FONT_S, justify="left",
                 anchor="w").pack(fill="x", padx=8, pady=6)

    # ---------------- 通用控件 ----------------
    @staticmethod
    def _tree(parent, cols, height=10):
        frame = tk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tv = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=height)
        for title, width in cols:
            tv.heading(title, text=title)
            tv.column(title, width=width, anchor="w")
        vs = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        return tv

    def _fill(self, tv, rows):
        tv.delete(*tv.get_children())
        for r in rows:
            tv.insert("", "end", values=r)

    # ================= 刷新 =================
    def refresh_all(self, light=False):
        self.refresh_overview()
        if not light:
            self.refresh_devices()
            self.refresh_alarms()
            self.refresh_lockers()
            self.refresh_special()
            self.refresh_samples()
            self.refresh_metrics()
        self._refresh_status()

    def refresh_overview(self):
        s = self.store.summary()
        st = self.store.latest_test_run()
        m = dict(st.metrics or {}) if st else {}
        self.card_vars["passes"].set(str(s["passes"]))
        self.card_vars["alarms"].set(str(s["alarms"]["total"]))
        self.card_vars["pending"].set(str(s["alarms"]["pending"]))
        self.card_vars["ppm"].set("%.1f/分" % s["throughput_ppm"])
        self.card_vars["cells"].set("%d/%d" % (s["cells"]["occupied"], s["cells"]["total"]))
        self.card_vars["test"].set(("%.0f%%" % (m.get("cheat_rate", 0) * 100)) if m else "-")
        self.card_vars["false"].set(("%.0f%%" % (m.get("false_rate", 0) * 100)) if m else "-")
        self.card_vars["devices"].set("%d/%d" % (s["devices"]["online"], s["devices"]["total"]))

        rows = []
        for ev in self.store.list_passes(limit=30):
            rows.append((ev.ts, ev.device_code, ev.candidate_name or ev.candidate_id or "-",
                         "报警" if ev.result == "alarm" else "放行", ev.kinds_text(),
                         {"real": "真机", "sim": "模拟", "manual": "人工"}.get(ev.source, ev.source)))
        self._fill(self.tv_pass, rows)

        arows = []
        for a in reinspect_mod.pending_queue(self.store, limit=30):
            arows.append((a.created_at, a.level_text(), a.kinds_text(), a.device_code,
                          a.candidate_name or a.candidate_id or "-",
                          ("超时!" if a.overdue() else a.status)))
        self._fill(self.tv_alarm_top, arows)

        drows = []
        running = set(self.manager.running_codes())
        for d in self.store.list_devices():
            proto = config.PROTO_TEXT.get(d.proto, d.proto)
            if d.code in running:
                proto += "（运行中）"
            drows.append((d.code, d.name, config.DEV_KIND_TEXT.get(d.kind, d.kind), proto,
                          "在线" if d.status == "online" else "离线", d.last_heartbeat or "-"))
        self._fill(self.tv_dev_top, drows)

    def refresh_devices(self):
        rows = []
        for d in self.store.list_devices():
            rows.append((d.code, d.name, config.DEV_KIND_TEXT.get(d.kind, d.kind),
                         config.PROTO_TEXT.get(d.proto, d.proto), d.address,
                         d.channel_no, d.room_id, "%.2f" % d.sensitivity,
                         "在线" if d.status == "online" else "离线"))
        self._fill(self.tv_dev, rows)

    def refresh_alarms(self):
        rows = []
        for a in self.store.list_alarms(limit=200,
                                        pending_only=self.var_pending_only.get()):
            rows.append((a.created_at, a.alarm_id, a.level_text(), a.kinds_text(),
                         a.device_code, a.candidate_name or a.candidate_id or "-",
                         ("超时!" if a.overdue() else models.ALARM_STATE_TEXT.get(a.status, a.status)),
                         "已报" if a.reported else ""))
        self._fill(self.tv_alarm, rows)
        lockers = [lk.code for lk in self.store.list_lockers()]
        self.cb_lock_for_detain.configure(values=lockers)
        if lockers and not self.cb_lock_for_detain.get():
            self.cb_lock_for_detain.set(lockers[0])

    def refresh_lockers(self):
        rows = []
        for x in locker_mod.usage_by_locker(self.store):
            rows.append((x["code"], x["name"], x["site"], x["total"], x["occupied"],
                         x["free"], "%.0f%%" % (x["usage"] * 100)))
        self._fill(self.tv_locker, rows)
        self._rebuild_cell_grid()
        self._refresh_locker_logs()

    def _selected_locker_code(self):
        sel = self.tv_locker.selection()
        if sel:
            return self.tv_locker.item(sel[0], "values")[0]
        lockers = self.store.list_lockers()
        return lockers[0].code if lockers else ""

    def _rebuild_cell_grid(self, force=False):
        code = self._selected_locker_code()
        if not code:
            for w in self.cell_grid.winfo_children():
                w.destroy()
            tk.Label(self.cell_grid, text="还没有登记储物柜（左侧「登记柜体」先建一台）",
                     font=FONT_S, fg=C_DIM).pack(anchor="w")
            self._grid_locker = None
            return
        cells = self.store.list_cells(locker_code=code)
        sig = (code, tuple((c.cell_no, c.state) for c in cells))
        if not force and getattr(self, "_grid_sig", None) == sig:
            return
        self._grid_sig = sig
        for w in self.cell_grid.winfo_children():
            w.destroy()
        col = 0
        for c in cells:
            b = tk.Button(self.cell_grid, text=c.cell_no, width=6, font=FONT_S,
                          bg=CELL_COLOR.get(c.state, "#ffffff"),
                          fg=CELL_FG.get(c.state, "#000000"),
                          command=lambda no=c.cell_no: self._cell_click(no))
            b.grid(row=col // 12, column=col % 12, padx=2, pady=2)
            col += 1
        info = self.store.cell_stats()
        tk.Label(self.cell_grid,
                 text="　共 %d 格：空闲 %d、已存 %d、故障 %d、超时未取 %d（点格子可选中，"
                      "再点右侧「取走手机」）" % (info["total"], info["free"],
                                            info["occupied"], info["fault"], info["overdue"]),
                 font=FONT_S, fg=C_DIM).grid(row=(col // 12) + 1, column=0, columnspan=12,
                                             sticky="w", pady=(6, 0))

    def _cell_click(self, cell_no):
        self.lo["cell_no"].delete(0, "end")
        self.lo["cell_no"].insert(0, cell_no)
        code = self._selected_locker_code()
        cell = self.store.get_cell(code, cell_no)
        if cell is not None and cell.state == config.CELL_OCCUPIED:
            self.lo["candidate_id"].delete(0, "end")
            self.lo["candidate_id"].insert(0, cell.candidate_id)
            self.lo["candidate_name"].delete(0, "end")
            self.lo["candidate_name"].insert(0, cell.candidate_name)
            self.lo["device_desc"].delete(0, "end")
            self.lo["device_desc"].insert(0, cell.device_desc)

    def _refresh_locker_logs(self):
        rows = []
        for lg in self.store.list_locker_logs(limit=200):
            rows.append((lg.ts, lg.locker_code, lg.cell_no, lg.action_text(),
                         config.OPEN_METHODS.get(lg.method, lg.method),
                         config.mask_id(lg.candidate_id), lg.device_desc,
                         lg.operator or "-", lg.note))
        self._fill(self.tv_locker_log, rows)

    def refresh_special(self):
        rows = []
        for r in self.store.list_specials(limit=200):
            rows.append((r.ts, r.candidate_name or config.mask_id(r.candidate_id),
                         r.reason_text(), r.inspector or "-", r.witness or "-",
                         r.result_text(), "演练" if r.drill else "真实", r.decision))
        self._fill(self.tv_special, rows)

    def refresh_samples(self):
        rows = []
        for s in self.store.list_samples():
            kind = config.KIND_TEXT.get(s.kind, s.kind)
            rows.append((s.sample_id, s.name, kind, "是" if s.expect_alarm else "否", s.note))
        self._fill(self.tv_sample, rows)
        names = ["%s %s" % (s.sample_id, s.name) for s in self.store.list_samples()]
        self.cb_field_sample.configure(values=names)
        if names and not self.cb_field_sample.get():
            self.cb_field_sample.set(names[0])

    def refresh_metrics(self):
        st = self.store.latest_test_run()
        modes = samples_mod.compare_modes(self.store)
        L = []
        if st:
            m = st.metrics or {}
            L.append("最近一次测试：%s（%s，%s）"
                     % (st.run_id, "判据回放" if st.mode == "replay" else "现场实测",
                        st.finished_at))
            L.append("样本 %d 条：检出 %d、漏检 %d、误报 %d、正确放行 %d"
                     % (st.total, st.hit, st.miss, st.false_alarm, st.correct_clear))
            L.append("")
            L.append("手机（含关机）检出率：%.1f%%（目标 ≥%.0f%%）%s"
                     % (m.get("phone_rate", 0) * 100, config.TARGET_PHONE_RATE * 100,
                        self._mark(m.get("pass", {}).get("phone"))))
            L.append("无线耳机检出率：    %.1f%%（目标 ≥%.0f%%）%s"
                     % (m.get("earphone_rate", 0) * 100, config.TARGET_EARPHONE_RATE * 100,
                        self._mark(m.get("pass", {}).get("earphone"))))
            L.append("误报率：            %.1f%%（目标 ≤%.0f%%）%s"
                     % (m.get("false_rate", 0) * 100, config.TARGET_FALSE_RATE * 100,
                        self._mark(m.get("pass", {}).get("false_rate"))))
            L.append("作弊器材整体检出率：%.1f%%（%d 条样本）"
                     % (m.get("cheat_rate", 0) * 100, m.get("cheat_total", 0)))
            L.append("植入医疗器械：%d 条，%d 条报警 -> 应转人工通道"
                     % (m.get("implant_total", 0), m.get("implant_alarmed", 0)))
            if st.mode == "replay":
                L.append("")
                L.append("※ 以上是**判据回放**（验算法与阈值），不能充作整机实测证据；")
                L.append("  请在右上「现场实测录入」里用真设备跑一遍再验收。")
        else:
            L.append("还没有测试记录：先点左下「跑判据回放」，再用真设备做「现场实测」。")
        L.append("")
        L.append("阈值模式对比（同一批样本，判据缩放）：")
        L.append("  %-8s %-9s %-9s %-9s %-9s" % ("模式", "手机", "耳机", "作弊", "误报"))
        for mode in ("turbo", "standard", "strict"):
            mm = modes.get(mode) or {}
            L.append("  %-8s %-9s %-9s %-9s %-9s"
                     % (config.GATE_MODE_TEXT.get(mode, mode),
                        "%.0f%%" % (mm.get("phone_rate", 0) * 100),
                        "%.0f%%" % (mm.get("earphone_rate", 0) * 100),
                        "%.0f%%" % (mm.get("cheat_rate", 0) * 100),
                        "%.0f%%" % (mm.get("false_rate", 0) * 100)))
        s = self.store.summary()
        L.append("")
        L.append("现场运行：过检 %d 条、报警 %d 条（待处置 %d）、复检误报率 %.1f%%、"
                 "通过效率 %.1f 人/分钟（目标 ≥%.0f）"
                 % (s["passes"], s["alarms"]["total"], s["alarms"]["pending"],
                    s["reinspects"]["false_rate"] * 100, s["throughput_ppm"],
                    config.TARGET_THROUGHPUT_PPM))
        self.txt_metrics.delete("1.0", "end")
        self.txt_metrics.insert("end", "\n".join(L))

    @staticmethod
    def _mark(ok):
        if ok is None:
            return "（无样本）"
        return "✔ 达标" if ok else "✘ 未达标"

    def _refresh_status(self):
        s = self.store.summary()
        ok, why = linkage_bridge.available()
        self.var_link.set("联动 03：%s" % ("可用" if ok else "不可用（%s）" % why[:60]))
        self.var_status.set(
            "设备 %d 台（在线 %d）｜运行中接入 %d 路｜待复检 %d ｜ 通过效率 %.1f 人/分钟 ｜ "
            "保管柜占用 %d/%d ｜ 数据目录 %s"
            % (s["devices"]["total"], s["devices"]["online"],
               len(self.manager.running_codes()), s["alarms"]["pending"],
               s["throughput_ppm"], s["cells"]["occupied"], s["cells"]["total"],
               config.DATA_ROOT))

    # ================= ② 设备操作 =================
    def device_pick(self):
        sel = self.tv_dev.selection()
        if not sel:
            return
        code = self.tv_dev.item(sel[0], "values")[0]
        d = self.store.get_device(code=code)
        if d is None:
            return
        for key, val in (("code", d.code), ("name", d.name), ("address", d.address),
                         ("channel_no", d.channel_no), ("room_id", d.room_id),
                         ("sensitivity", "%.2f" % d.sensitivity)):
            self.dv[key].delete(0, "end")
            self.dv[key].insert(0, str(val))
        self.cb_dev_kind.set(config.DEV_KIND_TEXT.get(d.kind, config.DEV_KIND_TEXT[config.DEV_GATE]))
        self.cb_proto.set(config.PROTO_TEXT.get(d.proto, config.PROTO_TEXT[config.PROTO_SIM]))

    def device_clear(self):
        for e in self.dv.values():
            e.delete(0, "end")
        self.dv["sensitivity"].insert(0, "1.0")
        self.dv["name"].insert(0, "1号入口安检门")
        self.cb_dev_kind.set(config.DEV_KIND_TEXT[config.DEV_GATE])
        self.cb_proto.set(config.PROTO_TEXT[config.PROTO_SIM])

    def _device_from_form(self):
        code = self.dv["code"].get().strip()
        if not code:
            messagebox.showwarning("缺少编码", "设备编码必填（如 GATE-A01）")
            return None
        kind = label_to_key(config.DEV_KIND_TEXT, self.cb_dev_kind.get(), config.DEV_GATE)
        proto = label_to_key(config.PROTO_TEXT, self.cb_proto.get(), config.PROTO_SIM)
        try:
            sens = float(self.dv["sensitivity"].get() or 1.0)
        except ValueError:
            messagebox.showwarning("灵敏度非法", "灵敏度要是数字（1.0 为标准）")
            return None
        return models.Device(code=code, name=self.dv["name"].get().strip() or code,
                             kind=kind, proto=proto, address=self.dv["address"].get().strip(),
                             channel_no=self.dv["channel_no"].get().strip(),
                             room_id=self.dv["room_id"].get().strip(),
                             sensitivity=sens)

    def device_add(self):
        dev = self._device_from_form()
        if dev is None:
            return
        if self.store.get_device(code=dev.code):
            messagebox.showwarning("已存在", "编码已存在：%s（改完点「保存修改」）" % dev.code)
            return
        self.store.save_device(dev)
        self.store.log("登记设备", "%s %s（%s）" % (dev.code, dev.name,
                                              config.PROTO_TEXT.get(dev.proto, dev.proto)),
                       self.cfg.get("operator"))
        self.refresh_all()
        self._log("登记设备 %s" % dev.code)

    def device_save(self):
        dev = self._device_from_form()
        if dev is None:
            return
        old = self.store.get_device(code=dev.code)
        if old:
            dev.device_id = old.device_id
            dev.status = old.status
            dev.last_heartbeat = old.last_heartbeat
            dev.created_at = old.created_at
        self.store.save_device(dev)
        self.store.log("修改设备", "%s %s" % (dev.code, dev.name), self.cfg.get("operator"))
        self.refresh_all()

    def device_delete(self):
        sel = self.tv_dev.selection()
        if not sel:
            return
        code = self.tv_dev.item(sel[0], "values")[0]
        if not messagebox.askyesno("确认", "删除设备 %s？（过检记录与报警会保留，便于追溯）" % code):
            return
        self.manager.stop(code)
        d = self.store.get_device(code=code)
        if d:
            self.store.delete_device(d.device_id)
            self.store.log("删除设备", code, self.cfg.get("operator"))
        self.refresh_all()

    def device_start(self):
        sel = self.tv_dev.selection()
        if not sel:
            messagebox.showinfo("先选设备", "先在表格里点一行设备")
            return
        code = self.tv_dev.item(sel[0], "values")[0]
        d = self.store.get_device(code=code)
        ok, info = self.manager.start(d)
        if not ok:
            messagebox.showerror("接入失败", str(info))
        self._log("启动接入 %s：%s" % (code, info))
        self.refresh_all()

    def device_stop(self):
        sel = self.tv_dev.selection()
        if not sel:
            return
        code = self.tv_dev.item(sel[0], "values")[0]
        self.manager.stop(code)
        self._log("停止接入 %s" % code)
        self.refresh_all()

    def device_probe(self):
        sel = self.tv_dev.selection()
        if not sel:
            return
        code = self.tv_dev.item(sel[0], "values")[0]
        d = self.store.get_device(code=code)
        ok, why = gate.probe_device(d)
        if ok is True:
            self.store.heartbeat(code)
        elif ok is False:
            pass
        self._log("探活 %s：%s" % (code, why))
        self.refresh_all()

    def stop_all_ingest(self):
        self.manager.stop_all()
        self._log("已停止全部接入")
        self.refresh_all()

    # ================= 一键演示 =================
    def one_click_demo(self):
        if not messagebox.askyesno(
                "一键演示",
                "将做四件事：\n"
                "1) 登记 1 台模拟安检门 + 1 台手持探测器 + 1 台鞋底仪 + 1 台储物柜（20 格）\n"
                "2) 启动模拟安检门（数据来源标 sim，仅演示）\n"
                "3) 跑一次判据回放（20 条内置样本）\n"
                "4) 生成一份演示流程数据（安检-核验-签到）\n\n"
                "模拟数据不会计入检出率/误报率。继续？"):
            return
        op = self.cfg.get("operator") or "演示"
        specs = [(config.DEV_GATE, "GATE-A01", "1号入口安检门", "1", config.PROTO_SIM, ""),
                 (config.DEV_GATE, "GATE-A02", "2号入口安检门", "2", config.PROTO_SIM, ""),
                 (config.DEV_HANDHELD, "HH-01", "手持探测器1号", "", config.PROTO_MANUAL, ""),
                 (config.DEV_SHOE, "SHOE-01", "鞋底检测仪", "", config.PROTO_MANUAL, ""),
                 (config.DEV_LOCKER, "LOCK-A", "手机保管柜A", "", config.PROTO_MANUAL, "")]
        for kind, code, name, ch, proto, addr in specs:
            if self.store.get_device(code=code):
                continue
            self.store.save_device(models.Device(code=code, name=name, kind=kind,
                                                 channel_no=ch, proto=proto, address=addr,
                                                 site=self.cfg.get("site", ""),
                                                 room_id="1考场"))
        if not self.store.get_locker(code="LOCK-A"):
            locker_mod.add_locker(self.store, "LOCK-A", "手机保管柜A",
                                  self.cfg.get("site", ""), 20, operator=op)
        run, _items = samples_mod.run_replay(self.store, operator=op, note="一键演示回放")
        gate_dev = self.store.get_device(code="GATE-A01")
        self.manager.start(gate_dev)
        flow_mod.demo_flow(self.store, n=10, operator=op)
        self._log("一键演示完成：回放 %d 条样本（检出 %d / 漏检 %d / 误报 %d）"
                  % (run.total, run.hit, run.miss, run.false_alarm))
        self.refresh_all()
        messagebox.showinfo("完成", "演示已就绪：\n"
                            "· 模拟安检门 GATE-A01 正在产生过检数据（source=sim）\n"
                            "· 判据回放：%d 条样本，检出 %d、漏检 %d、误报 %d\n"
                            "· 去「③ 过检与报警」复检处置，去「④ 手机保管柜」存取手机\n"
                            "· 去「⑥ 样本测试与验收」生成验收报告"
                            % (run.total, run.hit, run.miss, run.false_alarm))

    # ================= ③ 报警与复检 =================
    def alarm_pick(self):
        sel = self.tv_alarm.selection()
        if not sel:
            return
        alarm_id = self.tv_alarm.item(sel[0], "values")[1]
        self.al["alarm_id"].delete(0, "end")
        self.al["alarm_id"].insert(0, alarm_id)

    def _current_alarm(self):
        aid = self.al["alarm_id"].get().strip()
        if not aid:
            sel = self.tv_alarm.selection()
            if sel:
                aid = self.tv_alarm.item(sel[0], "values")[1]
        if not aid:
            messagebox.showinfo("先选报警", "在左侧点一条报警，或手填报警号")
            return None
        a = self.store.get_alarm(aid)
        if a is None:
            messagebox.showwarning("找不到", "报警单不存在：%s" % aid)
        return a

    def reinspect_start(self):
        a = self._current_alarm()
        if a is None:
            return
        method = label_to_key(config.REINSPECT_METHODS, self.cb_method.get(), "handheld")
        ok, info = reinspect_mod.start(self.store, a.alarm_id,
                                      operator=self.al["operator"].get().strip(),
                                      method=method,
                                      handheld_code=self.al["handheld_code"].get().strip(),
                                      witness=self.al["witness"].get().strip())
        if not ok:
            messagebox.showwarning("无法开始复检", str(info))
            return
        self._log("开始复检 %s" % a.alarm_id)
        self.refresh_all()

    def reinspect_finish(self):
        a = self._current_alarm()
        if a is None:
            return
        result = label_to_key(config.REINSPECT_RESULTS, self.cb_result.get(), "release")
        found = label_to_key(config.KIND_TEXT, self.cb_found.get(), config.KIND_MOBILE)
        locker_code = self.cb_lock_for_detain.get().strip() or None
        ok, msg = reinspect_mod.finish(
            self.store, a.alarm_id, result, found_kind=found if result == "detain" else "",
            found_detail=self.al["found_detail"].get().strip(),
            note=self.al["note"].get().strip(),
            operator=self.al["operator"].get().strip(), locker_code=locker_code)
        if not ok:
            messagebox.showwarning("没提交成功", str(msg))
            return
        self._log("处置 %s：%s" % (a.alarm_id, msg))
        messagebox.showinfo("已留痕", msg)
        self.refresh_all()

    def reinspect_special(self):
        a = self._current_alarm()
        if a is None:
            return
        ok, msg = reinspect_mod.handle_special_pass(
            self.store, a.alarm_id,
            {"candidate_id": a.candidate_id, "candidate_name": a.candidate_name,
             "room_id": a.room_id, "seat_no": ""},
            reason="implant", inspector=self.al["operator"].get().strip(),
            witness=self.al["witness"].get().strip(),
            detail=self.al["note"].get().strip())
        if not ok:
            messagebox.showwarning("需要双人见证", str(msg))
            return
        self._log("转人工安检通道：%s" % a.alarm_id)
        self.refresh_all()
        messagebox.showinfo("已转人工通道", str(msg.get("detail") if isinstance(msg, dict) else msg))

    def alarm_report(self):
        a = self._current_alarm()
        if a is None:
            return
        r = linkage_bridge.report_alarm(a, sync=True, operator=self.cfg.get("operator") or "06")
        if r is None:
            messagebox.showerror("上报失败", "03 智能终端不可用（先在 03 那边启动/检查数据目录）")
            return
        self.store.mark_alarm_reported(a.alarm_id, str(r)[:200])
        self._log("上报 03：%s -> %s（%s）" % (a.alarm_id, r.get("alert_id"), r.get("level")))
        messagebox.showinfo("已上报", "告警号：%s\n级别：%s\n降级：%s\n可信度：%s"
                            % (r.get("alert_id"), r.get("level"), r.get("downgraded"),
                               r.get("credibility")))
        self.refresh_all()

    def escalate(self):
        ids = reinspect_mod.escalate_overdue(self.store)
        if ids:
            self._log("复检超时上报：%s" % "、".join(ids))
            messagebox.showinfo("已上报", "超时未处置报警已上报 03：%s" % "、".join(ids))
        else:
            messagebox.showinfo("无需上报", "当前没有超时未处置的报警（或 03 不可用）")
        self.refresh_all()

    def manual_pass(self):
        dev = self.mn["device_code"].get().strip() or "MANUAL"
        sensors = {}
        for key in config.SENSOR_KEYS:
            try:
                sensors[key] = float(self.mn["s_" + key].get() or 0)
            except ValueError:
                messagebox.showwarning("数值非法", "%s 要是 0~1 的数字" % config.SENSOR_TEXT[key])
                return
        payload = {"device_code": dev,
                   "candidate_id": self.mn["candidate_id"].get().strip(),
                   "candidate_name": self.mn["candidate_name"].get().strip(),
                   "seat_no": self.mn["seat_no"].get().strip(),
                   "room_id": self.mn["room_id"].get().strip(),
                   "duration_ms": self.mn["duration_ms"].get().strip() or 0,
                   "sensors": sensors}
        ev, alarm = gate.ingest(self.store, payload, source="manual",
                                operator=self.cfg.get("operator") or "人工录入")
        flow_mod.bind_pass(self.store, ev, operator=self.cfg.get("operator"))
        note = "人工录入：%s" % ("放行" if alarm is None else
                            "%s %s" % (alarm.level_text(), alarm.kinds_text()))
        self._log(note)
        messagebox.showinfo("录入完成", note + ("\n（source=manual，人工录入）"))
        self.refresh_all()

    # ================= ④ 保管柜操作 =================
    def locker_add(self):
        try:
            total = int(self.lk["cells_total"].get() or 20)
        except ValueError:
            messagebox.showwarning("柜格数非法", "柜格数要是整数")
            return
        ok, info = locker_mod.add_locker(
            self.store, self.lk["code"].get().strip(), self.lk["name"].get().strip(),
            self.lk["site"].get().strip(), total,
            operator=self.cfg.get("operator"))
        if not ok:
            messagebox.showwarning("登记失败", str(info))
            return
        self._log("登记柜体 %s（%d 格）" % (info.code, info.cells_total))
        self.refresh_all()

    def locker_store(self):
        code = self._selected_locker_code()
        ok, info = locker_mod.store_device(
            self.store, candidate_id=self.lo["candidate_id"].get().strip(),
            candidate_name=self.lo["candidate_name"].get().strip(),
            device_desc=self.lo["device_desc"].get().strip() or "手机",
            locker_code=code, cell_no=self.lo["cell_no"].get().strip(),
            method=label_to_key(config.OPEN_METHODS, self.cb_open.get(), "manual"),
            operator=self.cfg.get("operator"))
        if not ok:
            messagebox.showwarning("存入失败", str(info))
            return
        self._log("存入手机 %s -> %s/%s" % (info.candidate_id, info.locker_code, info.cell_no))
        self.refresh_all()

    def locker_take(self):
        code = self._selected_locker_code()
        ok, info = locker_mod.take_device(
            self.store, locker_code=code, cell_no=self.lo["cell_no"].get().strip(),
            candidate_id=self.lo["candidate_id"].get().strip(),
            method=label_to_key(config.OPEN_METHODS, self.cb_open.get(), "face"),
            operator=self.cfg.get("operator"))
        if not ok:
            messagebox.showwarning("取件未通过", str(info))
            return
        self._log("取走手机 %s/%s" % (info.locker_code, info.cell_no))
        self.refresh_all()

    def locker_emergency(self):
        code = self._selected_locker_code()
        ok, msg = locker_mod.emergency_open(
            self.store, code, self.lo["cell_no"].get().strip(),
            self.cfg.get("operator"), self.lo["reason"].get().strip())
        if not ok:
            messagebox.showwarning("应急开柜被拒", str(msg))
            return
        self._log("应急开柜 %s/%s" % (code, self.lo["cell_no"].get().strip()))
        self.refresh_all()

    def locker_fault(self):
        code = self._selected_locker_code()
        ok, msg = locker_mod.mark_fault(
            self.store, code, self.lo["cell_no"].get().strip(), True,
            self.cfg.get("operator"), self.lo["reason"].get().strip())
        if not ok:
            messagebox.showwarning("操作被拒", str(msg))
            return
        self.refresh_all()

    def locker_trace(self):
        cand = self.lo["candidate_id"].get().strip()
        cell_no = self.lo["cell_no"].get().strip()
        logs = locker_mod.trace(self.store, candidate_id=cand or None,
                                cell_no=cell_no or None)
        if not logs:
            messagebox.showinfo("追溯", "没有查到存取记录")
            return
        lines = ["%s %s %s %s %s" % (lg.ts, lg.locker_code, lg.cell_no, lg.action_text(),
                                     lg.note) for lg in logs]
        messagebox.showinfo("存取追溯（%d 条）" % len(lines), "\n".join(lines[:30]))

    def locker_scan(self):
        out = locker_mod.scan_overdue(self.store, link=None)
        self._log("超时未取扫描：%d 格" % len(out))
        messagebox.showinfo("超时未取", "发现 %d 个柜格超时未取：%s"
                            % (len(out), "、".join("%s/%s" % (c.locker_code, c.cell_no)
                                                 for c in out) or "无"))
        self.refresh_all()

    # ================= ⑤ 特殊考生 =================
    def _special_form(self):
        return models.SpecialRecord(
            candidate_id=self.sp["candidate_id"].get().strip(),
            candidate_name=self.sp["candidate_name"].get().strip(),
            ticket_no=self.sp["ticket_no"].get().strip(),
            room_id=self.sp["room_id"].get().strip(),
            seat_no=self.sp["seat_no"].get().strip(),
            reason=label_to_key(config.SPECIAL_REASONS, self.cb_reason.get(), "pregnant"),
            inspector=self.sp["inspector"].get().strip(),
            witness=self.sp["witness"].get().strip(),
            result=label_to_key(config.SPECIAL_RESULTS, self.cb_sp_result.get(), "passed"),
            decision=self.sp["decision"].get().strip(),
            drill=bool(self.var_drill.get()))

    def special_save(self):
        rec = self._special_form()
        ok, res = special_mod.register(
            self.store, candidate_id=rec.candidate_id, candidate_name=rec.candidate_name,
            ticket_no=rec.ticket_no, room_id=rec.room_id, seat_no=rec.seat_no,
            reason=rec.reason, inspector=rec.inspector, witness=rec.witness,
            method=rec.method, result=rec.result, detail=rec.detail,
            decision=rec.decision, drill=rec.drill)
        if not ok:
            messagebox.showwarning("登记被拒", str(res))
            return
        self._log("登记人工安检：%s %s" % (res.reason_text(),
                                       res.candidate_name or res.candidate_id))
        self.refresh_all()

    def special_drill(self):
        """一键生成替代通道演练记录（验收证据）。"""
        op = self.al["operator"].get().strip() or self.cfg.get("operator") or "安检员"
        witness = self.al["witness"].get().strip() or "演练见证人（第二名安检员）"
        ok, res = special_mod.ensure_drill(self.store, operator=op, witness=witness)
        if not ok:
            messagebox.showwarning("演练记录生成失败", str(res))
            return
        flow_mod.record_drill(self.store, "特殊考生人工安检替代通道演练",
                              "孕妇/植入医疗器械考生走人工手检 + 持探测器，双人见证（%s / %s）"
                              % (op, witness), op)
        self._log("生成替代通道演练记录 %s" % res.record_id)
        self.refresh_all()
        messagebox.showinfo("已留痕", "演练记录已生成：%s（drill=True，报告里单独计数）"
                            % res.record_id)

    def special_report(self):
        rows = self.store.list_specials(limit=1)
        if not rows:
            messagebox.showinfo("没有记录", "先登记一条人工安检记录")
            return
        rec = rows[0]
        r = linkage_bridge.report_special(rec, sync=True)
        if r is None:
            messagebox.showerror("上报失败", "03 智能终端不可用")
            return
        messagebox.showinfo("已上报", "告警号：%s" % r.get("alert_id"))

    # ================= ⑥ 样本与验收 =================
    def run_replay(self):
        run, _items = samples_mod.run_replay(
            self.store, operator=self.cfg.get("operator") or "安检员",
            site=self.cfg.get("site"), note="界面判据回放")
        self._log("判据回放：%d 条样本，检出 %d、漏检 %d、误报 %d"
                  % (run.total, run.hit, run.miss, run.false_alarm))
        self.refresh_metrics()
        messagebox.showinfo("回放完成",
                            "样本 %d 条：检出 %d、漏检 %d、误报 %d、正确放行 %d\n\n"
                            "这是**判据回放**（验算法与阈值），整机实测请在右侧录入。"
                            % (run.total, run.hit, run.miss, run.false_alarm, run.correct_clear))

    def add_sample(self):
        sensors = {}
        for key in config.SENSOR_KEYS:
            try:
                sensors[key] = float(self.mn["s_" + key].get() or 0)
            except ValueError:
                messagebox.showwarning("数值非法", "请先在「③ 过检与报警」页填好六路感应值")
                return
        name = self.mn["candidate_name"].get().strip() or "自定义样本"
        expect = messagebox.askyesno("样本类型", "这是**应报警**的作弊样本吗？\n"
                                     "选「否」= 无害样本（用来算误报率）")
        kind = config.KIND_MOBILE if expect else "harmless"
        if expect:
            kind = label_to_key(config.KIND_TEXT, self.cb_found.get(), config.KIND_MOBILE)
        s = models.Sample(name=name, kind=kind, expect_alarm=bool(expect),
                          scores=sensors, note="界面添加")
        self.store.save_sample(s)
        self._log("新增样本 %s" % s.name)
        self.refresh_samples()
        self.refresh_metrics()

    def export_samples(self):
        ok, info = samples_mod.export_samples(self.store)
        messagebox.showinfo("导出", ("已导出：%s" % info) if ok else str(info))

    def import_samples(self):
        path = filedialog.askopenfilename(title="选择样本 JSON",
                                          filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if not path:
            return
        ok, msg = samples_mod.import_samples(self.store, path)
        messagebox.showinfo("导入", msg)
        self.refresh_samples()

    def field_record(self):
        text = self.cb_field_sample.get()
        if not text:
            messagebox.showinfo("先选样本", "在「样本」下拉里选一条")
            return
        sid = text.split(" ", 1)[0]
        if self.var_field_run.get().startswith("（"):
            run = samples_mod.create_field_run(self.store,
                                               operator=self.e_field_operator.get().strip(),
                                               site=self.cfg.get("site"))
            self._field_run_id = run.run_id
            self.var_field_run.set("现场实测进行中：%s（%s）"
                                   % (run.run_id, self.e_field_operator.get().strip()))
        s = self.store.get_sample(sid)
        if s is None:
            messagebox.showwarning("样本不存在", sid)
            return
        got = self.var_got.get() == "alarm"
        samples_mod.record_field_item(self.store, self._field_run_id, sample_id=sid,
                                      sample_name=s.name, kind=s.kind,
                                      expect_alarm=s.expect_alarm, got_alarm=got,
                                      got_kinds=([s.kind] if got else []),
                                      note="界面现场实测录入")
        self._log("现场实测录入 %s：报=%s" % (s.name, got))
        self.refresh_metrics()

    def field_finish(self):
        rid = getattr(self, "_field_run_id", "")
        if not rid:
            messagebox.showinfo("没有实测", "先点「录入这条结果」开始一次现场实测")
            return
        runs = [r for r in self.store.list_test_runs(limit=50) if r.run_id == rid]
        if not runs:
            return
        run, items = samples_mod.finish_run(self.store, runs[0])
        self._field_run_id = ""
        self.var_field_run.set("最近一次现场实测：%s 完成（%s）"
                               % (run.run_id, run.finished_at))
        self.refresh_metrics()
        messagebox.showinfo("实测完成",
                            "样本 %d 条：检出 %d、漏检 %d、误报 %d\n"
                            "手机检出率 %.0f%%、耳机检出率 %.0f%%、误报率 %.0f%%"
                            % (run.total, run.hit, run.miss, run.false_alarm,
                               (run.metrics or {}).get("phone_rate", 0) * 100,
                               (run.metrics or {}).get("earphone_rate", 0) * 100,
                               (run.metrics or {}).get("false_rate", 0) * 100))

    def gen_report(self):
        ok, info = report_mod.write_report(self.store, site=self.cfg.get("site"))
        if not ok:
            messagebox.showerror("生成失败", str(info))
            return
        self._log("已生成验收报告：%s" % info["md"])
        messagebox.showinfo("已生成",
                            "Markdown：%s\nCSV：%s\n\n报告里写明了口径与前提，"
                            "没有现场实测数据时会明确标注「整机实测证据：缺失」。"
                            % (info["md"], info["csv"]))

    def open_report_dir(self):
        self._open_dir(config.REPORT_DIR)

    def open_data_dir(self):
        self._open_dir(config.DATA_ROOT)

    @staticmethod
    def _open_dir(path):
        try:
            if os.name == "nt":
                os.startfile(str(path))                    # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:                             # noqa: BLE001
            messagebox.showinfo("打开失败", "%s\n%s" % (path, e))

    # ================= ⑦ 设置 =================
    def save_settings(self):
        thresholds = {}
        for key, e in self.th.items():
            try:
                thresholds[key] = float(e.get())
            except ValueError:
                messagebox.showwarning("阈值非法", "%s 要是 0~1 的数字" % key)
                return
        values = {"site": self.st["site"].get().strip(),
                  "operator": self.st["operator"].get().strip(),
                  "gate_mode": label_to_key(config.GATE_MODE_TEXT, self.cb_mode.get(), "standard"),
                  "thresholds": thresholds,
                  "double_confirm": bool(self.var_double.get()),
                  "report_to_terminal": bool(self.var_report03.get()),
                  "auto_open_backup": bool(self.var_backup.get()),
                  "encrypt_sensitive": bool(self.var_encrypt.get())}
        for key in ("locker_overdue_min", "sim_interval_sec", "sim_alarm_rate"):
            raw = self.st[key].get()
            try:
                values[key] = float(raw) if key != "locker_overdue_min" else int(float(raw))
            except ValueError:
                messagebox.showwarning("数值非法", "%s 要是数字" % key)
                return
        ok = config.save_settings(values)
        if self.var_hide.get():
            config.HIDE_CMD_FLAG.parent.mkdir(parents=True, exist_ok=True)
            config.HIDE_CMD_FLAG.write_text("on", encoding="utf-8")
        else:
            try:
                config.HIDE_CMD_FLAG.unlink()
            except OSError:
                pass
        self.cfg = config.load_settings()
        self.var_site.set("考点：%s" % self.cfg.get("site", ""))
        self.store.log("修改设置", str(values)[:300], self.cfg.get("operator"))
        self._log("设置已保存（判据模式 %s）" % config.GATE_MODE_TEXT.get(
            self.cfg.get("gate_mode"), self.cfg.get("gate_mode")))
        messagebox.showinfo("已保存", "设置已生效。\n当前阈值：%s" % config.thresholds())
        self.refresh_all()

    def reload_settings(self):
        self.cfg = config.load_settings()
        self.st["site"].delete(0, "end")
        self.st["site"].insert(0, str(self.cfg.get("site", "")))
        self.st["operator"].delete(0, "end")
        self.st["operator"].insert(0, str(self.cfg.get("operator", "")))
        for key, e in self.th.items():
            e.delete(0, "end")
            e.insert(0, str(self.cfg["thresholds"].get(key, config.DEFAULT_THRESHOLDS[key])))
        self.cb_mode.set(config.GATE_MODE_TEXT.get(self.cfg.get("gate_mode"), "标准"))
        self.var_double.set(bool(self.cfg.get("double_confirm", True)))
        self.var_report03.set(bool(self.cfg.get("report_to_terminal")))
        self.var_backup.set(bool(self.cfg.get("auto_open_backup", True)))
        self.var_encrypt.set(bool(self.cfg.get("encrypt_sensitive", True)))

    def check_audit(self):
        ok, n, msg = self.store.verify_audit_chain()
        messagebox.showinfo("审计链校验", ("完整 ✔\n" if ok else "断链 ✘\n") + msg)

    def purge(self):
        if not messagebox.askyesno("确认", "删除 30 天前的过检记录、已处置报警与开柜日志？\n"
                                   "（审计记录保留）"):
            return
        info = self.store.purge_runtime(30)
        messagebox.showinfo("已清理", str(info))
        self.refresh_all()

    # ---------------- 关闭 ----------------
    def on_close(self):
        self._stop = True
        try:
            self.manager.stop_all()
        except Exception:
            pass
        try:
            self.store.log("关闭界面", "06 安检模块退出", self.cfg.get("operator"))
            self.store.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    config.ensure_stdio("app_console.log")
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", font=FONT_S, rowheight=22)
        style.configure("TNotebook.Tab", font=FONT_B, padding=(10, 4))
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
