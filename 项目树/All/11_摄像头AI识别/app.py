# -*- coding: utf-8 -*-
r"""
摄像头 AI 识别 · 图形界面

两个页签，没有第三个：

  ① 摄像头管理   名字 + 地址（下拉选，可自动扫描本机摄像头）→ 添加 / 编辑 / 删除
  ② AI 识别      逐人框选 + 行为判定，多路网格

**刻意不带登录与权限**：打开即用。需要口令/角色的是 01/02/03 那几套，本目录不掺和。

启动：双击「启动.bat」，或 python app.py
"""
import os
import inspect
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import config

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

config.ensure_stdio("app_console.log")

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import camera_stream as cs
except Exception:
    cs = None

try:
    import person_detect as pd_mod
    HAS_PD = True
except Exception:
    pd_mod = None
    HAS_PD = False


def yolo_disabled():
    """与 person_detect._yolo_disabled 同口径：EXAM_YOLO_DISABLE=1 时禁用 YOLO"""
    return os.environ.get("EXAM_YOLO_DISABLE", "").strip() in ("1", "true", "True")


def yolo_ready():
    """YOLO11n 是否真的可用：开关 + onnxruntime + 模型文件三者都要满足"""
    if not HAS_PD or yolo_disabled():
        return False
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    path = ""
    if hasattr(pd_mod, "default_yolo_model_path"):
        path = pd_mod.default_yolo_model_path()
    return bool(path) and os.path.exists(path)


def chain_desc():
    """如实描述当前检测链（新版 person_detect 已移除 describe_backends）"""
    if not HAS_PD:
        return "person_detect 未加载（AI 识别不可用）"
    if yolo_ready():
        return "YOLO11n-ONNX 主力（缺帧自动 HOG 兜底）"
    if yolo_disabled():
        return "YOLO11n 已停用（EXAM_YOLO_DISABLE=1），HOG+SVM 兜底"
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return "缺 onnxruntime（pip install onnxruntime），HOG+SVM 兜底"
    return "缺 YOLO11n 模型（运行 工具_YOLO模型导出.py），HOG+SVM 兜底"


from storage import Camera, CameraStore

FONT = ("Microsoft YaHei", 10)
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_TITLE = ("Microsoft YaHei", 13, "bold")

SCAN_MAX_INDEX = 5          # 自动扫描本机摄像头时试到几号
VIDEO_OPTION = "本地视频文件…"


class App:
    """摄像头 AI 识别主窗口。"""

    def __init__(self, root):
        self.root = root
        self.root.title("摄像头 AI 识别")
        self.root.geometry("1240x860")

        self.store = CameraStore()
        self.ai_cfg = config.load_ai_settings()

        # 运行态
        self._readers = {}        # cam_id -> FrameReader
        self._trackers = {}       # cam_id -> PersonTracker
        self._panels = {}         # cam_id -> 面板控件字典
        self._photos = {}         # cam_id -> PhotoImage（防回收）
        self._cam_state = {}      # cam_id -> 渲染缓存
        self._ai_on = tk.BooleanVar(value=True)
        self._ai_backend_name = ""
        self._tick_id = None
        self._closing = False
        self._editing_id = None
        self._addr_options = []    # 下拉候选
        if ui_theme is not None:
            ui_theme.apply(self.root)

        self._build_ui()
        if ui_theme is not None:
            ui_theme.polish(self.root)
            ui_theme.polish_later(self.root, 500)
        self._reload_table()
        self._refresh_ai_tab()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()
        # 启动后就扫一次本机摄像头，省得用户自己猜序号
        self.root.after(300, lambda: self._scan_devices(auto=True))

    # =================================================================
    #  界面搭建
    # =================================================================
    def _build_ui(self):
        head = ttk.Frame(self.root, padding=(10, 8))
        head.pack(fill="x")
        ttk.Label(head, text="摄像头 AI 识别", font=FONT_TITLE).pack(side="left")
        ttk.Label(head, text="（无登录、无权限；打开即用）",
                  foreground="#888", font=FONT_SMALL).pack(side="left", padx=(8, 0))

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._tab_mgr = ttk.Frame(nb, padding=8)
        self._tab_ai = ttk.Frame(nb, padding=8)
        nb.add(self._tab_mgr, text="① 摄像头管理")
        nb.add(self._tab_ai, text="② AI 识别")
        self._notebook = nb

        self._build_manager_tab()
        self._build_ai_tab()

        self.status = ttk.Label(self.root, text="就绪", foreground="#0a7d28",
                                font=FONT_SMALL, anchor="w")
        self.status.pack(fill="x", side="bottom", padx=12, pady=(0, 6))

    # ---------------- 页签①：摄像头管理 ----------------
    def _build_manager_tab(self):
        f = self._tab_mgr

        # --- 表格 ---
        top = ttk.Frame(f)
        top.pack(fill="both", expand=True)
        cols = ("id", "name", "addr")
        titles = ("#", "摄像头名字", "地址")
        widths = (50, 260, 560)
        tv = ttk.Treeview(top, columns=cols, show="headings", height=15)
        for c, t, w in zip(cols, titles, widths):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w" if c != "id" else "center")
        vs = ttk.Scrollbar(top, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        tv.bind("<<TreeviewSelect>>", lambda e: self._on_row_select())
        tv.bind("<Double-1>", lambda e: self._on_row_select())
        self.tv = tv

        # --- 表单：只有名字 + 地址 ---
        form = ttk.LabelFrame(f, text="添加摄像头", padding=10)
        form.pack(fill="x", pady=(10, 6))

        self.v_name = tk.StringVar()
        self.v_addr = tk.StringVar()

        ttk.Label(form, text="名字：").grid(row=0, column=0, sticky="e", pady=6)
        e_name = ttk.Entry(form, textvariable=self.v_name, width=26, font=FONT)
        e_name.grid(row=0, column=1, sticky="w", padx=(0, 16))
        e_name.bind("<Return>", lambda e: self._submit())

        ttk.Label(form, text="地址：").grid(row=0, column=2, sticky="e", pady=6)
        self.cb_addr = ttk.Combobox(form, textvariable=self.v_addr, width=30, font=FONT)
        self.cb_addr.grid(row=0, column=3, sticky="w", padx=(0, 8))
        self.cb_addr.bind("<<ComboboxSelected>>", self._on_addr_pick)
        ttk.Button(form, text="🔍 扫描", command=self._scan_devices).grid(
            row=0, column=4, sticky="w")

        btns = ttk.Frame(form)
        btns.grid(row=1, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self.btn_submit = ttk.Button(btns, text="➕ 添加", command=self._submit)
        self.btn_submit.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="🗑 删除", command=self._delete_selected).pack(
            side="left", padx=6)
        ttk.Button(btns, text="✖ 取消编辑", command=self._clear_form).pack(
            side="left", padx=6)

        ttk.Label(form, text="双击表格行即可编辑名字或地址；地址可从下拉里选，或点「扫描」自动找本机摄像头。",
                  foreground="#888", font=FONT_SMALL).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))

        self.mgr_status = ttk.Label(f, text="", foreground="#1f56b8", font=FONT_SMALL)
        self.mgr_status.pack(fill="x", pady=(4, 0))

    # ---------------- 页签②：AI 识别 ----------------
    def _build_ai_tab(self):
        f = self._tab_ai

        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Checkbutton(bar, text="AI 框选识别", variable=self._ai_on,
                        command=self._on_ai_toggle).pack(side="left")
        ttk.Label(bar, text="　检测模式：").pack(side="left")
        self.v_mode = tk.StringVar(value=str(self.ai_cfg.get("ai_mode") or "standard"))
        cb = ttk.Combobox(bar, textvariable=self.v_mode, state="readonly", width=12,
                          values=["standard", "dense", "energy"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_mode())
        ttk.Label(bar, text="标准640 / 密集768 / 节能416", foreground="#888",
                  font=FONT_SMALL).pack(side="left", padx=(6, 12))

        ttk.Button(bar, text="🔄 重新加载", command=self._restart_readers).pack(side="left")
        ttk.Button(bar, text="⏹ 全部停止", command=self._stop_all).pack(side="left", padx=6)
        self.ai_backend_lbl = ttk.Label(bar, text="", foreground="#1f56b8", font=FONT_SMALL)
        self.ai_backend_lbl.pack(side="left", padx=(12, 0))

        info = ttk.Label(f, text="", foreground="#1f56b8", font=FONT_SMALL, anchor="w")
        info.pack(fill="x", pady=(4, 4))
        self.ai_info = info

        # 可滚动网格
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self.ai_grid = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=self.ai_grid, anchor="nw")
        self._ai_canvas = canvas
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self.ai_grid.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        if not HAS_PD:
            ttk.Label(self.ai_grid, text="⚠ person_detect 未加载：AI 识别不可用",
                      foreground="#c00").grid(row=0, column=0, sticky="w", padx=6, pady=6)

    # =================================================================
    #  设备扫描（下拉候选）
    # =================================================================
    def _scan_devices(self, auto=False):
        """扫描本机摄像头序号，填充地址下拉。

        没有摄像头 / OpenCV 不可用时，至少给出 0~2 的候选，让用户能先登记。
        """
        if self.cb_addr.get() == "":
            self.v_addr.set("")
        if cs is None or cv2 is None:
            self._set_addr_options(["0", "1", "2"])
            self.mgr_status.config(text="无 OpenCV，无法扫描；可手填地址或选本机序号")
            return

        self.mgr_status.config(text="正在扫描本机摄像头 %d~%d …" % (0, SCAN_MAX_INDEX))
        base = self._addr_options or []

        def work():
            found = []
            for idx in range(0, SCAN_MAX_INDEX + 1):
                cap = None
                try:
                    # 注意：open_capture 返回 (cap|None, 说明)，不是 cap 本身
                    cap, _why = cs.open_capture(str(idx), timeout=2.5, log_it=False)
                    if cap is not None:
                        ok, frame = cap.read()
                        if ok and frame is not None and getattr(frame, "size", 0) > 0:
                            found.append(str(idx))
                except Exception:
                    pass
                finally:
                    if cap is not None:
                        try:
                            cs.release_async(cap, "扫描结束")
                        except Exception:
                            pass
            self.root.after(0, lambda: self._after_scan(found, auto))

        threading.Thread(target=work, daemon=True).start()

    def _after_scan(self, found, auto=False):
        opts = list(found)
        for extra in ("0", "1", "2"):
            if extra not in opts:
                opts.append(extra)
        self._set_addr_options(opts)
        if found:
            self.v_addr.set(found[0])
            self.mgr_status.config(text="扫描到本机摄像头：%s（已自动选中最小的一个）"
                                        % "、".join(found))
        else:
            self.mgr_status.config(text="没扫到可用本机摄像头（可能被占用或无设备）；"
                                        "可手填 RTSP 地址，或用本地视频文件试")
        if not auto:
            pass

    def _set_addr_options(self, opts):
        self._addr_options = list(opts)
        self.cb_addr["values"] = list(opts) + [VIDEO_OPTION]

    def _on_addr_pick(self, event=None):
        if self.v_addr.get() == VIDEO_OPTION:
            p = filedialog.askopenfilename(
                title="选择本地视频文件",
                filetypes=[("视频", "*.mp4 *.avi *.mkv *.mov *.flv *.mjpg"),
                           ("所有文件", "*.*")])
            if p:
                self.v_addr.set(p)
            else:
                self.v_addr.set("")

    # =================================================================
    #  页签① 逻辑
    # =================================================================
    def _reload_table(self):
        for i in self.tv.get_children():
            self.tv.delete(i)
        for c in self.store.all():
            self.tv.insert("", "end", iid=str(c.id), values=(c.id, c.name, c.url))
        self.mgr_status.config(text="共 %d 路摄像头。双击表格行可编辑。" % self.store.count())

    def _on_row_select(self):
        sel = self.tv.selection()
        if not sel:
            return
        cam = self.store.get(int(sel[0]))
        if not cam:
            return
        self._editing_id = cam.id
        self.v_name.set(cam.name)
        self.v_addr.set(cam.url)
        self.btn_submit.config(text="💾 保存修改")
        self.mgr_status.config(text="正在编辑 #%d（改完点「保存修改」）" % cam.id)

    def _clear_form(self):
        self._editing_id = None
        self.v_name.set("")
        self.v_addr.set("")
        self.btn_submit.config(text="➕ 添加")
        self.tv.selection_remove(*self.tv.selection())
        self.mgr_status.config(text="已取消编辑")

    def _submit(self):
        """有编辑中的 id 就保存修改，否则新增。"""
        name = self.v_name.get().strip()
        addr = self.v_addr.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写摄像头名字。")
            return
        if not addr:
            messagebox.showwarning("提示", "请选择或填写地址（可点「扫描」自动找）。")
            return

        if self._editing_id:
            cam = self.store.get(self._editing_id)
            if cam is None:
                self._clear_form()
                self._reload_table()
                return
            cam.name, cam.url = name, addr
            ok, msg = self.store.update(cam)
        else:
            ok, msg = self.store.add(Camera(name=name, url=addr))
        if not ok:
            messagebox.showerror("保存失败", msg)
            return
        self._clear_form()
        self._reload_table()
        self._refresh_ai_tab()
        self.mgr_status.config(text=msg)

    def _delete_selected(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的摄像头。")
            return
        cam = self.store.get(int(sel[0]))
        if not cam:
            return
        if not messagebox.askyesno("确认删除", "确定删除「%s」？" % cam.name):
            return
        self._stop_reader(cam.id)
        self.store.delete(cam.id)
        self._clear_form()
        self._reload_table()
        self._refresh_ai_tab()
        self.mgr_status.config(text="已删除「%s」" % cam.name)

    # =================================================================
    #  页签② 逻辑
    # =================================================================
    def _make_tracker(self):
        """按当前 AI 设置建一个检测器（每路一个，避免轨迹串号）。

        检测链由 person_detect 自动选择（YOLO11n ONNX > MobileNet-SSD > HOG），
        构造参数按它的实际签名过滤：模型/参数换代时不会因 TypeError 整条链路
        掉进"降级分支"而丢掉设置。
        """
        if not HAS_PD:
            return None
        cfg = config.load_ai_settings()
        kw = dict(max_input_dim=1280,
                  boundary_ratio=config.ai_boundary_ratio(cfg),
                  confirm_frames=4, min_hits=2)
        for k in ("yolo_model_path", "yolo_conf", "yolo_iou", "yolo_input_dim"):
            if cfg.get(k) not in (None, ""):
                kw[k] = cfg[k]
        try:
            params = inspect.signature(pd_mod.PersonTracker.__init__).parameters
            kw = {k: v for k, v in kw.items() if k in params}
        except (TypeError, ValueError):
            pass
        try:
            return pd_mod.PersonTracker(**kw)
        except TypeError:
            return pd_mod.PersonTracker(max_input_dim=1280)
        except Exception as e:
            self._set_info("AI 检测器创建失败：%s" % str(e)[:80])
            return None

    def _refresh_ai_tab(self):
        """按已登记的摄像头重建网格与取流器。"""
        cams = self.store.all()
        keep = set(c.id for c in cams)
        for cid in list(self._panels.keys()):
            if cid not in keep:
                self._stop_reader(cid)
                w = self._panels.pop(cid, None)
                if w and w["frame"].winfo_exists():
                    w["frame"].destroy()
                self._photos.pop(cid, None)
                self._cam_state.pop(cid, None)
                self._trackers.pop(cid, None)

        for idx, cam in enumerate(cams):
            if cam.id in self._panels:
                continue
            self._panels[cam.id] = self._make_panel(cam, idx)
            self._cam_state[cam.id] = {"ts": 0.0, "persons": [], "viol": 0,
                                       "line_y": 0, "scale": 1.0, "count": 0}
            self._start_reader(cam)
        self._layout_panels()
        self._update_ai_info()
        self._notebook.tab(1, text="② AI 识别（%d 路）" % len(cams))

    def _make_panel(self, cam, idx):
        fr = ttk.LabelFrame(self.ai_grid, text="#%d %s" % (cam.id, cam.name), padding=4)
        img = tk.Label(fr, text="未开启\n（点「🔄 重新加载」启动）", background="#111",
                       foreground="#aaa", width=48, height=14)
        img.pack()
        st = ttk.Label(fr, text="状态：—", foreground="#666", font=FONT_SMALL)
        st.pack(anchor="w")
        return {"frame": fr, "image": img, "status": st, "cam": cam,
                "info": ttk.Label(fr, text="", foreground="#1f56b8", font=FONT_SMALL)}

    def _layout_panels(self):
        for w in self.ai_grid.winfo_children():
            w.grid_forget()
        for i, cam in enumerate(self.store.all()):
            p = self._panels.get(cam.id)
            if p:
                p["frame"].grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="nw")
                p["info"].pack(anchor="w")

    def _start_reader(self, cam):
        if cs is None or cam.id in self._readers:
            return
        try:
            r = cs.FrameReader(cam.url, label=cam.name)
            r.start()
            self._readers[cam.id] = r
            self._trackers[cam.id] = self._make_tracker()
        except Exception as e:
            self._set_panel_status(cam.id, "取流启动失败：%s" % str(e)[:60], "#c00")

    def _stop_reader(self, cam_id):
        r = self._readers.pop(cam_id, None)
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass

    def _stop_all(self):
        for cid in list(self._readers.keys()):
            self._stop_reader(cid)
        for cid in self._panels:
            self._set_panel_status(cid, "已停止", "#666")
        self._set_info("已停止全部取流")

    def _restart_readers(self):
        for cid in list(self._readers.keys()):
            self._stop_reader(cid)
        for cam in self.store.all():
            self._start_reader(cam)
        self._set_info("已重新加载 %d 路取流" % len(self._readers))

    def _on_ai_toggle(self):
        self._set_info("AI 框选识别：%s" % ("开启" if self._ai_on.get() else "关闭"))

    def _apply_mode(self):
        cfg = dict(self.ai_cfg)
        cfg["ai_mode"] = self.v_mode.get()
        cfg["ai_input_dim"] = 0
        if config.save_ai_settings(cfg):
            self.ai_cfg = config.load_ai_settings()
            self._set_info("检测模式已切到 %s（分析长边 %d）"
                           % (self.v_mode.get(), config.ai_input_dim(self.ai_cfg)))
        for cid in list(self._trackers.keys()):
            self._trackers[cid] = self._make_tracker()

    def _set_info(self, text):
        try:
            self.ai_info.config(text=text)
        except tk.TclError:
            pass

    def _set_panel_status(self, cam_id, text, color="#666"):
        p = self._panels.get(cam_id)
        if not p:
            return
        try:
            p["status"].config(text="状态：%s" % text, foreground=color)
        except tk.TclError:
            pass

    def _update_ai_info(self):
        self._set_info("分析长边 %d ｜ 间隔 %d ms ｜ 警戒线 %.0f%% ｜ 检测链：%s"
                       % (config.ai_input_dim(self.ai_cfg),
                          config.ai_interval_ms(self.ai_cfg),
                          config.ai_boundary_ratio(self.ai_cfg) * 100,
                          chain_desc()))

    # =================================================================
    #  渲染主循环
    # =================================================================
    def _tick(self):
        if self._closing:
            return
        try:
            self._render_once()
        except Exception as e:
            try:
                self.status.config(text="渲染异常：%s" % str(e)[:80], foreground="#c00")
            except tk.TclError:
                pass
        interval = config.render_interval_ms(max(1, len(self._readers)))
        self._tick_id = self.root.after(interval, self._tick)

    def _render_once(self):
        if not self._panels:
            return
        now_ms = time.time() * 1000.0
        ai_on = bool(self._ai_on.get()) and HAS_PD
        total_persons = total_viol = 0
        cfg = self.ai_cfg
        ai_dim = config.ai_input_dim(cfg)
        ai_interval = config.ai_interval_ms(cfg)
        boundary = config.ai_boundary_ratio(cfg)

        for cam in self.store.all():
            cid = cam.id
            p = self._panels.get(cid)
            r = self._readers.get(cid)
            if not p or r is None:
                continue
            snap = r.snapshot()
            frame = snap.get("frame")
            state = snap.get("state")
            if frame is None or getattr(frame, "size", 0) == 0:
                self._set_panel_status(cid, snap.get("status") or state, "#c07000")
                continue
            st = self._cam_state.setdefault(cid, {"ts": 0.0, "persons": [], "viol": 0,
                                                  "line_y": 0, "scale": 1.0, "count": 0})
            try:
                disp = self._fit_to_panel(frame, p["image"])
                persons = st.get("persons") or []
                viol = st.get("viol", 0)

                if ai_on:
                    if now_ms - st.get("ts", 0.0) >= ai_interval:
                        tracker = self._trackers.get(cid)
                        if tracker is None:
                            tracker = self._make_tracker()
                            self._trackers[cid] = tracker
                        if tracker is not None:
                            ai_frame = self._prepare_ai_frame(frame, ai_dim)
                            persons, viol, line_y = tracker.analyze_only(
                                ai_frame, boundary_ratio=boundary)
                            for per in persons:
                                tr = getattr(tracker, "_tracks", {}).get(per["pid"])
                                fa = getattr(tr, "face_cache", None) if tr is not None else None
                                per["face"] = (fa or {}).get("face") if fa else None
                            st.update({"ts": now_ms, "persons": persons, "viol": viol,
                                       "line_y": line_y,
                                       "scale": disp.shape[1] / float(ai_frame.shape[1]),
                                       "count": (tracker.stable_count()
                                                 if hasattr(tracker, "stable_count")
                                                 else len(persons))})
                            self._note_backend(tracker)
                    scale = st.get("scale") or (disp.shape[1] / float(frame.shape[1]))
                    tracker = self._trackers.get(cid)
                    if tracker is not None and persons:
                        tracker.draw_boxes(disp, persons, line_y=st.get("line_y"),
                                           scale=scale, font_size=12)
                    total_persons += int(st.get("count", len(persons)))
                    total_viol += int(viol)

                self._show_frame(cid, disp)
                self._set_panel_status(
                    cid, "播放中 %.1f fps" % (snap.get("fps") or 0.0), "#0a7d28")
                p["info"].config(text="人数 %d ｜ 越界 %d ｜ %s"
                                 % (st.get("count", len(persons)), viol,
                                    snap.get("status") or ""))
            except Exception as e:
                self._set_panel_status(cid, "渲染失败：%s" % str(e)[:50], "#c00")

        if ai_on:
            self.status.config(
                text="AI 识别中：共 %d 人，越界 %d ｜ 检测链 %s"
                     % (total_persons, total_viol,
                        self._ai_backend_name or chain_desc()),
                foreground="#0a7d28")
        else:
            self.status.config(text="AI 识别已关闭（仅预览画面）", foreground="#666")

    @staticmethod
    def _fit_to_panel(frame, label_widget):
        """等比缩放到面板尺寸（下采样用 INTER_AREA 更清晰）。"""
        try:
            w = label_widget.winfo_width()
            h = label_widget.winfo_height()
        except tk.TclError:
            w = h = 0
        if w < 60:
            w = 384
        if h < 60:
            h = 216
        fh, fw = frame.shape[:2]
        scale = min(w / float(fw), h / float(fh), config.MAX_PANEL_W / float(fw),
                    config.MAX_PANEL_H / float(fh))
        if scale >= 1.0:
            return frame
        nw, nh = max(2, int(fw * scale)), max(2, int(fh * scale))
        return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _prepare_ai_frame(frame, dim):
        """按分析长边缩放；已经比目标小就不放大（放大只是白烧算力）。"""
        fh, fw = frame.shape[:2]
        long_edge = max(fh, fw)
        if long_edge <= dim:
            return frame
        s = dim / float(long_edge)
        return cv2.resize(frame, (max(2, int(fw * s)), max(2, int(fh * s))),
                          interpolation=cv2.INTER_AREA)

    def _show_frame(self, cam_id, bgr):
        try:
            from PIL import Image, ImageTk
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
            p = self._panels.get(cam_id)
            if p is None:
                return
            self._photos[cam_id] = photo        # 持引用，防被回收
            p["image"].config(image=photo, text="", width=0, height=0)
        except Exception:
            pass

    def _note_backend(self, tracker):
        """如实显示当前检测链（没放模型时会显示 HOG+SVM）。

        新版 person_detect 不再提供 backend_name()/backend_info()，这里直接看
        实际建起来的会话：YOLO11n 会话 -> YOLO11n；Caffe 网络 -> MobileNet-SSD；
        都没有就是 HOG+SVM。
        """
        if getattr(tracker, "_yolo_session", None) is not None:
            name = "YOLO11n-ONNX"
        elif getattr(tracker, "_net", None) is not None:
            name = "MobileNet-SSD"
        else:
            name = "HOG+SVM"
        if name == self._ai_backend_name:
            return
        self._ai_backend_name = name
        try:
            tag = "YOLO11n 模型不可用" if name == "HOG+SVM" else "主力检测器"
            self.ai_backend_lbl.config(text="检测链：%s（%s）" % (name, tag))
            (config.DATA_ROOT / "ai_backend.txt").write_text(name + "\n", encoding="utf-8")
        except Exception:
            pass

    # =================================================================
    #  收尾
    # =================================================================
    def _on_close(self):
        self._closing = True
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        for cid in list(self._readers.keys()):
            self._stop_reader(cid)
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    if cv2 is None:
        print("缺少 OpenCV，请先运行「安装环境.bat」或 pip install opencv-python<5")
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
