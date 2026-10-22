# -*- coding: utf-8 -*-
r"""
试卷流转与保密室智能监控 · 桌面界面（tkinter）

首页签是**实时画面**：连接摄像头（本机序号 / RTSP / 本地视频文件），
多路网格显示，每路带状态条（连接中 / 播放中 / 画面中断 / 打开失败 / 帧率 / 重连次数）。
取流走 camera_stream.FrameReader，界面只轮询 snapshot()，**永不做阻塞调用**。

其余页签：保密室监控、试卷流转、告警中心、关键指标、系统设置（含账户管理/审计链）。

权限按 web_accounts.Session 控权，每个受控入口先 session.need(action)，
不足则弹"权限不足：…"而不是把按钮藏起来（藏按钮不算越权防护）。
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# 独立运行：把包根加入 sys.path，才能 import 包根的 sso / ui_theme
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
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
import cv2
import models

try:
    import fire_detect          # 从 02 移入：火焰/烟雾识别（自包含，不依赖 onnx）
except Exception:                                            # noqa: BLE001
    fire_detect = None
import report as report_mod
import web_accounts
from alarm_hub import AlarmHub
from flow import FlowManager
from storage import Store
from vault import CameraManager, Vault


# ---------------- 图片显示（无 Pillow 时降级） ----------------
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def bgr_to_photo(frame, max_w, max_h):
    """BGR ndarray -> ImageTk.PhotoImage（等比缩放到 max_w×max_h 内）。"""
    if frame is None or not _HAS_PIL:
        return None
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None
    scale = min(max_w / float(w), max_h / float(h), 1.0)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    if (nw, nh) != (w, h):
        img = img.resize((nw, nh), Image.BILINEAR)
    return ImageTk.PhotoImage(img)


STATE_TEXT = {
    "connecting": "连接中…",
    "live": "播放中",
    "stalled": "画面中断",
    "error": "打开失败",
    "stopped": "已停止",
}


# ==================================================================
# 登录页
# ==================================================================
class LoginFrame(ttk.Frame):
    def __init__(self, app, on_ok):
        # app 是 App 外壳（持有 .root/.store）；父容器是窗口，业务对象从 app 取，
        # 二者不能混为一谈——早先曾把 Tk 窗口当 app 传进来，导致 master.store 崩溃。
        ttk.Frame.__init__(self, app.root)
        self.app = app
        self.on_ok = on_ok
        self.store = app.store
        self.am = web_accounts.AccountManager(self.store)
        self.am.ensure_defaults()

        box = ttk.Frame(self)
        box.place(relx=0.5, rely=0.45, anchor="center")
        ttk.Label(box, text="试卷流转与保密室智能监控",
                  font=("Microsoft YaHei", 16, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(box, text="专题 08 · 智能化装备与考生数据保护",
                  foreground=(ui_theme.DIM if ui_theme else "#666")).grid(
            row=1, column=0, columnspan=2, pady=(0, 18))

        ttk.Label(box, text="用户名").grid(row=2, column=0, sticky="e", padx=6, pady=6)
        self.u = ttk.Entry(box, width=22)
        self.u.grid(row=2, column=1, pady=6)
        self.u.insert(0, "owner")

        ttk.Label(box, text="口令").grid(row=3, column=0, sticky="e", padx=6, pady=6)
        self.p = ttk.Entry(box, width=22, show="*")
        self.p.grid(row=3, column=1, pady=6)

        self.hint = ttk.Label(box, text="",
                              foreground=(ui_theme.ALARM if ui_theme else "#c00000"))
        self.hint.grid(row=4, column=0, columnspan=2, pady=(0, 6))

        ttk.Button(box, text="登 录", command=self._login).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(box, text="首次运行默认账户：owner / owner（所有者）\n"
                            "teacher1 / teacher1（管理员）——登录后请尽快改口令",
                  foreground=(ui_theme.DIM if ui_theme else "#888"),
                  justify="center").grid(
            row=6, column=0, columnspan=2, pady=(12, 0))
        self.p.bind("<Return>", lambda e: self._login())

    def _login(self):
        sess, why = self.am.login(self.u.get().strip(), self.p.get())
        if sess is None:
            self.hint.config(text="登录失败：%s" % why)
            return
        self.on_ok(sess)


# ==================================================================
# 主界面
# ==================================================================
class MainFrame(ttk.Frame):
    def __init__(self, app, session):
        # 父容器是 window（app.root）；self.master 保持 tkinter 赋值的窗口对象，
        # 外壳引用另存 self.app，避免破坏 tkinter 内部对 master 的依赖。
        ttk.Frame.__init__(self, app.root)
        self.app = app
        self.session = session
        self.store = app.store
        self.hub = AlarmHub(self.store)
        self.cameras = CameraManager(self.store)
        self.vault = Vault(self.store, self.hub, self.cameras)
        self.flow = FlowManager(self.store, self.hub)
        self.kpi = report_mod.KPIReport(self.store, self.vault, self.flow)
        self.evidence = report_mod.EvidenceReport(self.store, self.vault, self.flow,
                                                  self.kpi)

        self.current_room = tk.StringVar(value="")
        self.status = tk.StringVar(value="就绪")
        self._photos = {}
        self._live_after = None
        # 火场识别（从 02 移入）：每路一个 FireWatch，后台线程分析，UI 队列回填
        self.fire_on = tk.BooleanVar(value=True)
        self._fire_watches = {}
        self._fire_last = {}
        self._fire_busy = set()
        self._fire_state = {}
        self._tile_w, self._tile_h = 320, 200
        self._ui_queue = queue.Queue()

        self._build()
        self.refresh_rooms()
        self._poll_ui_queue()

    # ---------------- 权限 ----------------
    def need(self, action):
        msg = self.session.need(action)
        if msg:
            messagebox.showwarning("权限不足", msg, parent=self)
            return False
        return True

    # ---------------- 骨架 ----------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(top, text="试卷流转与保密室智能监控",
                  font=("Microsoft YaHei", 13, "bold")).pack(side="left")
        ttk.Label(top, text="  当前用户：%s（%s）"
                  % (self.session.username,
                     web_accounts.role_text(self.session.role)),
                  foreground=(ui_theme.DIM if ui_theme else "#666")).pack(side="left")
        self.red_lbl = ttk.Label(top, text="",
                                 foreground=(ui_theme.ALARM if ui_theme else "#c00000"),
                                 font=("Microsoft YaHei", 11, "bold"))
        self.red_lbl.pack(side="right", padx=8)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=6)
        self.nb = nb

        self.tab_live = ttk.Frame(nb)
        self.tab_vault = ttk.Frame(nb)
        self.tab_flow = ttk.Frame(nb)
        self.tab_alarm = ttk.Frame(nb)
        self.tab_kpi = ttk.Frame(nb)
        self.tab_sys = ttk.Frame(nb)
        nb.add(self.tab_live, text="  实时画面  ")
        nb.add(self.tab_vault, text="  保密室监控  ")
        nb.add(self.tab_flow, text="  试卷流转  ")
        nb.add(self.tab_alarm, text="  告警中心  ")
        nb.add(self.tab_kpi, text="  关键指标  ")
        nb.add(self.tab_sys, text="  系统设置  ")

        self._build_live()
        self._build_vault()
        self._build_flow()
        self._build_alarm()
        self._build_kpi()
        self._build_sys()

        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar).pack(fill="x")
        ttk.Label(bar, textvariable=self.status,
                  foreground=(ui_theme.DIM if ui_theme else "#444")).pack(
            side="left", padx=8, pady=3)

    # ================= 页签 1：实时画面 =================
    def _build_live(self):
        f = self.tab_live
        ctl = ttk.LabelFrame(f, text="连接摄像头 / 取流")
        ctl.pack(fill="x", padx=6, pady=6)

        ttk.Label(ctl, text="来源").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.src_kind = tk.StringVar(value="local")
        ttk.Radiobutton(ctl, text="本机摄像头", value="local",
                        variable=self.src_kind,
                        command=self._on_src_change).grid(row=0, column=1, padx=2)
        ttk.Radiobutton(ctl, text="RTSP/HTTP 地址", value="url",
                        variable=self.src_kind,
                        command=self._on_src_change).grid(row=0, column=2, padx=2)
        ttk.Radiobutton(ctl, text="本地视频文件", value="file",
                        variable=self.src_kind,
                        command=self._on_src_change).grid(row=0, column=3, padx=2)

        self.src_box = ttk.Frame(ctl)
        self.src_box.grid(row=1, column=0, columnspan=8, sticky="ew", pady=4)

        ttk.Label(self.src_box, text="摄像头序号").grid(row=0, column=0, padx=4)
        self.cam_index = ttk.Combobox(self.src_box, width=8, values=["0", "1", "2"])
        self.cam_index.set("0")
        self.cam_index.grid(row=0, column=1, padx=2)
        ttk.Button(self.src_box, text="探测本机摄像头",
                   command=self._probe_local).grid(row=0, column=2, padx=6)

        ttk.Label(self.src_box, text="地址").grid(row=0, column=3, padx=4)
        self.src_url = ttk.Entry(self.src_box, width=46)
        self.src_url.grid(row=0, column=4, padx=2)
        ttk.Label(self.src_box, text="（RTSP 例：rtsp://user:pwd@ip:554/stream1；"
                                     "本地文件：rtsp.mp4）",
                  foreground=(ui_theme.DIM if ui_theme else "#888")).grid(
            row=0, column=5, padx=4)
        ttk.Button(self.src_box, text="浏览…", command=self._pick_file).grid(
            row=0, column=6, padx=4)

        acts = ttk.Frame(ctl)
        acts.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(2, 4))
        ttk.Label(acts, text="机位").pack(side="left", padx=4)
        self.cam_id_var = tk.StringVar(value="LIVE1")
        ttk.Entry(acts, textvariable=self.cam_id_var, width=12).pack(side="left")
        ttk.Button(acts, text="▶ 播放", command=self.start_live).pack(side="left", padx=4)
        ttk.Button(acts, text="■ 停止", command=self.stop_live).pack(side="left", padx=4)
        ttk.Button(acts, text="同步到机位台账", command=self._register_live_cam).pack(
            side="left", padx=4)
        ttk.Button(acts, text="📷 抓拍留证", command=self._shot_current).pack(
            side="left", padx=4)
        ttk.Button(acts, text="⟳ 刷新画面", command=self._tick_live_once).pack(
            side="left", padx=4)
        self.fire_chk = ttk.Checkbutton(
            acts, text="🔥 火场识别（火焰/烟雾）", variable=self.fire_on)
        self.fire_chk.pack(side="left", padx=(12, 4))
        ttk.Label(acts, text="火警进入下方「告警中心」",
                  foreground=(ui_theme.DIM if ui_theme else "#888")).pack(
            side="left", padx=2)

        self.live_hint = ttk.Label(
            ctl, text="", foreground=(ui_theme.DIM if ui_theme else "#666"))
        self.live_hint.grid(row=3, column=0, columnspan=8, sticky="w", padx=6)

        # 画面网格
        grid = ttk.LabelFrame(f, text="画面（多路网格）")
        grid.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.canvas = tk.Canvas(grid, background="#101418", highlightthickness=0)
        vsb = ttk.Scrollbar(grid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.tiles = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.tiles, anchor="nw")
        self.tiles.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))

        self._tile_widgets = {}
        self._on_src_change()
        self._tick_live_once()

    def _on_src_change(self):
        kind = self.src_kind.get()
        for w in self.src_box.grid_slaves():
            pass
        self.cam_index.configure(state="normal" if kind == "local" else "disabled")
        self.src_url.configure(state="normal" if kind != "local" else "disabled")
        tip = {"local": "选择本机摄像头序号后点「探测本机摄像头」确认可用性",
               "url": "填入 RTSP/HTTP 取流地址（支持带帐号密码）",
               "file": "选择本地视频文件作为测试流（算法与取流链路验证用）"}[kind]
        self.live_hint.config(text=tip)

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.avi *.mkv *.mov *.flv *.wmv"),
                       ("全部文件", "*.*")])
        if p:
            self.src_kind.set("file")
            self.src_url.delete(0, "end")
            self.src_url.insert(0, p)
            self._on_src_change()

    def _current_source(self):
        kind = self.src_kind.get()
        if kind == "local":
            return int(self.cam_index.get() or 0)
        return self.src_url.get().strip()

    def _probe_local(self):
        self.status.set("正在探测本机摄像头…")
        self.update_idletasks()

        def work():
            found = self.cameras.probe_local()
            self._ui_queue.put(("probe", found))
        threading.Thread(target=work, daemon=True).start()

    def start_live(self):
        if not self.need("live_view"):
            return
        src = self._current_source()
        if src == "" or src is None:
            messagebox.showinfo("提示", "请先选择或填写取流来源", parent=self)
            return
        cam_id = self.cam_id_var.get().strip() or "LIVE1"
        # 若该机位已登记，直接用它登记的地址
        cam = self.store.get_camera(cam_id)
        if cam is not None and cam.url and self.src_kind.get() == "local":
            src = cam.url
        ok, msg = self.cameras.start(cam_id, fps=0)
        if not ok:
            # 台账里没有这个机位：临时取流（不入库）
            self._start_adhoc(cam_id, src)
            return
        self.status.set("已开始取流：%s（%s）" % (cam_id, src))
        self._ensure_tile(cam_id, str(src))
        self._tick_live_once()

    def _start_adhoc(self, cam_id, src):
        """台账外的临时取流（本机摄像头未登记时用）。"""
        import camera_stream
        if cam_id in self.cameras._readers:
            self.cameras._readers[cam_id].stop()
        r = camera_stream.FrameReader(src, label="%s(临时)" % cam_id)
        r.start()
        self.cameras._readers[cam_id] = r
        self.status.set("已开始临时取流：%s（%s）" % (cam_id, src))
        self._ensure_tile(cam_id, str(src))
        self._tick_live_once()

    def stop_live(self):
        cam_id = self.cam_id_var.get().strip() or "LIVE1"
        self.cameras.stop(cam_id)
        self.status.set("已停止取流：%s" % cam_id)
        self._drop_tile(cam_id)

    def _register_live_cam(self):
        if not self.need("device_manage"):
            return
        cam_id = self.cam_id_var.get().strip() or "LIVE1"
        src = str(self._current_source())
        dlg = _CameraDialog(self, self.store, cam_id, src)
        self.wait_window(dlg)
        if dlg.result:
            self.refresh_vault()
            self.refresh_rooms()
            self.status.set("已登记机位 %s" % cam_id)

    def _shot_current(self):
        cam_id = self.cam_id_var.get().strip() or "LIVE1"
        p = self.cameras.snapshot(cam_id, reason="manual")
        if p:
            messagebox.showinfo("抓拍成功", "留证图片已保存：\n%s" % p, parent=self)
        else:
            messagebox.showwarning("抓拍失败", "当前没有可用画面（请先播放）", parent=self)

    # ---- 画面网格 ----
    def _ensure_tile(self, cam_id, src):
        if cam_id in self._tile_widgets:
            return
        row = len(self._tile_widgets) // 3
        col = len(self._tile_widgets) % 3
        wrap = tk.Frame(self.tiles, bd=2, relief="groove", background="#1b2027")
        wrap.grid(row=row, column=col, padx=6, pady=6, sticky="nw")
        lab = tk.Label(wrap, background="#101418", width=44, height=13,
                       text="连接中…", foreground="#ddd")
        lab.pack()
        info = tk.Label(wrap, text="%s  %s" % (cam_id, src), background="#1b2027",
                        foreground="#8fd", font=("Consolas", 9), anchor="w",
                        justify="left")
        info.pack(fill="x", padx=2)
        self._tile_widgets[cam_id] = {"wrap": wrap, "label": lab, "info": info,
                                      "last_seq": -1}

    def _drop_tile(self, cam_id):
        w = self._tile_widgets.pop(cam_id, None)
        if w:
            w["wrap"].destroy()
        self._relayout_tiles()

    def _relayout_tiles(self):
        for i, (cid, w) in enumerate(self._tile_widgets.items()):
            w["wrap"].grid(row=i // 3, column=i % 3, padx=6, pady=6, sticky="nw")

    def _tick_live_once(self):
        for cam_id, w in list(self._tile_widgets.items()):
            st = self.cameras.snapshot_state(cam_id)
            frame = st.get("frame")
            txt_state = STATE_TEXT.get(st.get("state"), st.get("state"))
            extra = ""
            if st.get("state") == "live":
                extra = "  %.1f fps" % (st.get("fps") or 0.0)
            if st.get("reconnects"):
                extra += "  重连 %d 次" % st["reconnects"]
            if frame is not None and st.get("seq") != w["last_seq"]:
                if self.fire_on.get():
                    self._fire_dispatch(cam_id, frame)
                ph = bgr_to_photo(frame, self._tile_w, self._tile_h)
                if ph is not None:
                    w["label"].configure(image=ph, text="", width=ph.width(),
                                         height=ph.height())
                    w["label"].image = ph
                    self._photos[cam_id] = ph
                    w["last_seq"] = st.get("seq")
            elif frame is None and st.get("state") != "live":
                w["label"].configure(image="", text=txt_state, width=44, height=13)
            w["info"].configure(text="%s  [%s]%s  %s%s"
                                     % (cam_id, txt_state, extra,
                                        st.get("status") or "",
                                        self._fire_bar(cam_id)))
        if self._live_after:
            try:
                self.after_cancel(self._live_after)
            except Exception:
                pass
        self._live_after = self.after(config.LIVE_POLL_MS, self._tick_live_once)

    # ---------------- 火场识别（从 02 移入） ----------------
    def _fire_watch_for(self, cam_id):
        w = self._fire_watches.get(cam_id)
        if w is None:
            if fire_detect is None:
                raise RuntimeError("fire_detect 不可用")
            w = fire_detect.FireWatch()
            self._fire_watches[cam_id] = w
        return w

    def _fire_dispatch(self, cam_id, frame):
        """限制频率 + 每路单飞，后台线程分析，绝不阻塞取流/渲染。"""
        if fire_detect is None:
            return
        now = time.time()
        if now - self._fire_last.get(cam_id, 0.0) < 0.8:
            return
        if cam_id in self._fire_busy:
            return
        self._fire_last[cam_id] = now
        self._fire_busy.add(cam_id)
        try:
            frm = frame.copy()
        except Exception:
            self._fire_busy.discard(cam_id)
            return
        threading.Thread(target=self._fire_worker,
                         args=(cam_id, frm), daemon=True).start()

    def _fire_worker(self, cam_id, frame):
        try:
            w = self._fire_watch_for(cam_id)
            r = w.push_frame(frame, camera_name=cam_id)
        except Exception as exc:                             # noqa: BLE001
            r = {"ok": False, "reason": "火检异常：%s" % str(exc)[:80]}
        finally:
            self._fire_busy.discard(cam_id)
        self._ui_queue.put(("fire", (cam_id, r)))

    def _fire_bar(self, cam_id):
        r = self._fire_state.get(cam_id)
        if not r:
            return ""
        if not r.get("ok"):
            return "  ｜ 火检：%s" % (r.get("reason") or "不可用")
        lv = r.get("level")
        if lv:
            return "  ｜ 火情：%s %.2f" % (lv, float(r.get("score") or 0.0))
        return ""

    def _on_fire_result(self, cam_id, r):
        self._fire_state[cam_id] = r
        if not r.get("ok") or not r.get("just_triggered"):
            return
        level = r.get("level") or "warning"
        code = "FIRE_ALARM" if level == "alarm" else "FIRE_SMOKE"
        cam = self.store.get_camera(cam_id)
        scope = getattr(cam, "room_id", "") if cam else ""
        name = getattr(cam, "name", "") if cam else ""
        detail = ("实时监控火场识别：%s（%s），判定分 %.2f；依据：%s。"
                  "火情属人身安全事件，请立即核实并按消防预案处置。"
                  % (name or cam_id, cam_id, float(r.get("score") or 0.0),
                     r.get("reason") or "无"))
        try:
            self.hub.raise_alarm(code, detail=detail, source_kind="fire",
                                 source_id=cam_id, scope=scope,
                                 event_epoch=time.time(), evidence_cam=cam_id)
        except Exception:                                    # noqa: BLE001
            pass
        self.status.set("火警：%s · %s" % (code, detail))
        try:
            self.refresh_alarms()
            self._update_red_badge()
        except Exception:                                    # noqa: BLE001
            pass

    # ================= 页签 2：保密室监控 =================
    def _build_vault(self):
        f = self.tab_vault
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="保密室编号").pack(side="left", padx=4)
        self.room_combo = ttk.Combobox(bar, width=16, textvariable=self.current_room)
        self.room_combo.pack(side="left")
        self.room_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_vault())
        ttk.Button(bar, text="⟳ 刷新", command=self.refresh_vault).pack(side="left", padx=4)
        ttk.Button(bar, text="＋ 新增保密室机位", command=self._add_camera).pack(
            side="left", padx=4)
        ttk.Button(bar, text="▶ 全部开始取流", command=self._start_room_cams).pack(
            side="left", padx=4)
        ttk.Button(bar, text="■ 全部停止", command=lambda: self.cameras.stop_all()).pack(
            side="left", padx=4)
        ttk.Button(bar, text="🗑 删除机位", command=self._del_camera).pack(
            side="left", padx=4)

        # 六面覆盖
        cov = ttk.LabelFrame(f, text="一、无死角视频 —— 六面覆盖核查")
        cov.pack(fill="x", padx=6, pady=4)
        self.cov_lbl = ttk.Label(cov, text="—", font=("Microsoft YaHei", 10, "bold"))
        self.cov_lbl.pack(anchor="w", padx=6, pady=2)
        self.cov_tree = ttk.Treeview(cov, columns=("face", "cams", "state", "need",
                                                   "missing"),
                                     show="headings", height=6)
        for k, t, w in (("face", "朝向", 110), ("cams", "机位", 170),
                        ("state", "状态", 70), ("need", "该朝向必拍要点", 230),
                        ("missing", "未覆盖要点", 200)):
            self.cov_tree.heading(k, text=t)
            self.cov_tree.column(k, width=w, anchor="w")
        self.cov_tree.pack(fill="x", padx=6, pady=(0, 6))

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, padx=6, pady=4)

        # 机位表
        lf = ttk.LabelFrame(mid, text="机位台账（逐机位留档验证）")
        lf.pack(side="left", fill="both", expand=True)
        self.cam_tree = ttk.Treeview(lf, columns=("id", "name", "face", "url",
                                                  "ch", "state", "problems"),
                                     show="headings", height=8)
        for k, t, w in (("id", "机位ID", 100), ("name", "名称", 120),
                        ("face", "朝向", 80), ("url", "取流地址", 200),
                        ("ch", "冗余通道", 90), ("state", "状态", 60),
                        ("problems", "台账问题", 220)):
            self.cam_tree.heading(k, text=t)
            self.cam_tree.column(k, width=w, anchor="w")
        self.cam_tree.pack(fill="both", expand=True, padx=4, pady=4)

        rf = ttk.LabelFrame(mid, text="七项能力体检")
        rf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.check_tree = ttk.Treeview(rf, columns=("name", "ok", "detail"),
                                       show="headings", height=8)
        for k, t, w in (("name", "监控项", 170), ("ok", "状态", 60),
                        ("detail", "说明", 320)):
            self.check_tree.heading(k, text=t)
            self.check_tree.column(k, width=w, anchor="w")
        self.check_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # 事件模拟
        sim = ttk.LabelFrame(f, text="事件注入（联调用；真实部署接传感器/门禁/电子锁）")
        sim.pack(fill="x", padx=6, pady=4)
        ttk.Button(sim, text="① 双因子开门（正确）",
                   command=lambda: self._sim_access(True)).grid(row=0, column=0,
                                                                padx=4, pady=4)
        ttk.Button(sim, text="② 双因子开门（缺因子）",
                   command=lambda: self._sim_access(False)).grid(row=0, column=1,
                                                                 padx=4, pady=4)
        ttk.Button(sim, text="③ 未授权开门",
                   command=self._sim_abnormal_door).grid(row=0, column=2, padx=4)
        ttk.Button(sim, text="④ 红外双鉴入侵",
                   command=lambda: self._sim_intrusion("ir_dual")).grid(
            row=0, column=3, padx=4)
        ttk.Button(sim, text="⑤ 门窗磁",
                   command=lambda: self._sim_intrusion("door_mag")).grid(
            row=0, column=4, padx=4)
        ttk.Button(sim, text="⑥ 未授权开柜",
                   command=self._sim_safe_open).grid(row=0, column=5, padx=4)
        ttk.Button(sim, text="⑦ 环境异常（水浸/烟感）",
                   command=self._sim_env).grid(row=1, column=0, padx=4, pady=4)
        ttk.Button(sim, text="⑧ 机器人巡查一轮",
                   command=self._sim_patrol).grid(row=1, column=1, padx=4)
        ttk.Button(sim, text="⑨ 生成 7 天录像段（连续性核查）",
                   command=self._sim_segments).grid(row=1, column=2, padx=4)
        ttk.Button(sim, text="⑩ 制造录像丢段（验证告警）",
                   command=self._sim_record_gap).grid(row=1, column=3, padx=4)
        ttk.Button(sim, text="🛡 布防 / 撤防",
                   command=self._toggle_armed).grid(row=1, column=4, padx=4)

    def _sim_room(self):
        r = self.current_room.get().strip()
        if not r:
            messagebox.showinfo("提示", "请先填写/选择保密室编号", parent=self)
        return r

    def _sim_access(self, ok):
        if not self.need("access_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        if ok:
            ev = self.vault.access.request_open(
                r, self.session.username, factors=["face", "card"],
                persons=[self.session.username, "witness1"])
        else:
            ev = self.vault.access.request_open(
                r, self.session.username, factors=["card"],
                persons=[self.session.username, "witness1"])
        self.after_event("门禁事件已注入（%s）" % ("放行" if ev[0] else "拒绝"))

    def _sim_abnormal_door(self):
        if not self.need("access_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        self.vault.access.report_abnormal_open(r, person=self.session.username,
                                               detail="门磁检测到未授权开启（联调注入）")
        self.after_event("未授权开门告警已产生")

    def _sim_intrusion(self, kind):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        self.vault.intrusion.trigger(r, kind, sensor_id="%s-01" % kind,
                                     zone="保密室内区", armed=True)
        self.after_event("入侵事件已注入：%s" % config.INTRUSION_KINDS[kind])

    def _sim_safe_open(self):
        if not self.need("safe_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        self.vault.safes.open_request(r, "SAFE-01", self.session.username,
                                      authorized=False,
                                      inner={"paper_present": True})
        self.after_event("未授权开柜告警已产生")

    def _sim_env(self):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        self.vault.env.sample(r, temp_c=26.0, humidity_pct=78.0, water=True,
                              smoke=False)
        self.after_event("环境异常已注入（湿度超限 + 水浸）")

    def _sim_patrol(self):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        cams = self.store.cameras(r)
        self.vault.patrol.patrol_round(r, by="patrol_robot", result="ok",
                                       note="联调注入的一轮巡查",
                                       snapshot_cam=cams[0].cam_id if cams else None)
        self.after_event("已记录一轮机器人巡查")

    def _sim_segments(self):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        cams = self.store.cameras(r)
        if not cams:
            messagebox.showinfo("提示", "该保密室还没有机位，请先登记", parent=self)
            return
        now = time.time()
        base = now - 7 * 86400
        for cam in cams:
            for ch in (cam.channels or ["main"]):
                cur = base
                while cur < now:
                    self.vault.records.add_segment(cam.cam_id, cur, 3600.0, channel=ch)
                    cur += 3600.0
        self.after_event("已为 %d 个机位生成 7 天×%s 录像段" % (len(cams), "双路"))

    def _sim_record_gap(self):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        cams = self.store.cameras(r)
        if not cams:
            return
        cam = cams[0]
        ch = (cam.channels or ["main"])[0]
        # 在中间挖掉 3 小时 -> 连续性核查应当报缺口
        last = self.store.segments(cam.cam_id, ch)
        if not last:
            messagebox.showinfo("提示", "请先点「生成 7 天录像段」", parent=self)
            return
        mid = sorted(last, key=lambda s: s.start_epoch)[len(last) // 2]
        mid.duration_sec = 1.0
        self.store.save_segment(mid)
        probs = self.vault.records.check_all(r)
        self.after_event("已制造录像丢段，核查发现 %d 处问题" % len(probs))

    def _toggle_armed(self):
        if not self.need("monitor_operate"):
            return
        r = self._sim_room()
        if not r:
            return
        cur = self.vault.intrusion.is_armed(r)
        self.vault.intrusion.set_armed(r, not cur, operator=self.session.username)
        self.after_event("已%s保密室 %s" % ("撤防" if cur else "布防", r))

    def _add_camera(self):
        if not self.need("device_manage"):
            return
        dlg = _CameraDialog(self, self.store, "", "")
        self.wait_window(dlg)
        if dlg.result:
            self.refresh_rooms()
            self.refresh_vault()

    def _del_camera(self):
        if not self.need("device_delete"):
            return
        sel = self.cam_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在机位表里选中一行", parent=self)
            return
        cam_id = self.cam_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno(
                "确认",
                "删除机位 %s？\n（删除后该机位台账与录像记录不再出现在报告里）"
                % cam_id, parent=self):
            return
        self.cameras.remove(cam_id, operator=self.session.username)
        self.refresh_vault()

    def _start_room_cams(self):
        if not self.need("live_view"):
            return
        r = self.current_room.get().strip()
        started = 0
        for c in self.store.cameras(r or None):
            if c.url:
                ok, _ = self.cameras.start(c.cam_id)
                if ok:
                    self._ensure_tile(c.cam_id, c.url)
                    started += 1
        self.status.set("已开始 %d 路取流" % started)

    # ================= 页签 3：试卷流转 =================
    def _build_flow(self):
        f = self.tab_flow
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=6, pady=6)
        for txt, cmd in (("＋ 登记试卷袋", self._flow_register_bag),
                         ("① 领取/交接", self._flow_handover),
                         ("② 电子铅封", self._flow_seal),
                         ("③ 押运任务", self._flow_escort),
                         ("④ 分发至考场", self._flow_deliver),
                         ("⑤ 回收与封存", self._flow_recycle),
                         ("⟳ 刷新", self.refresh_flow)):
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=3)

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, padx=6)

        left = ttk.LabelFrame(mid, text="试卷袋台账")
        left.pack(side="left", fill="both", expand=True)
        self.bag_tree = ttk.Treeview(left, columns=("id", "subject", "qty", "seal",
                                                    "state", "stage"),
                                     show="headings", height=10)
        for k, t, w in (("id", "试卷袋", 110), ("subject", "科目", 90),
                        ("qty", "数量", 60), ("seal", "封签号", 120),
                        ("state", "铅封状态", 90), ("stage", "当前环节", 90)):
            self.bag_tree.heading(k, text=t)
            self.bag_tree.column(k, width=w, anchor="w")
        self.bag_tree.pack(fill="both", expand=True, padx=4, pady=4)

        right = ttk.LabelFrame(mid, text="全链条追溯（选中试卷袋后）")
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.trace_txt = tk.Text(right, height=14, width=58, wrap="word",
                                 font=("Consolas", 9))
        self.trace_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.bag_tree.bind("<<TreeviewSelect>>", lambda e: self._show_trace())

        bot = ttk.Frame(f)
        bot.pack(fill="both", expand=True, padx=6, pady=6)
        self.flow_tree = ttk.Treeview(bot, columns=("stage", "count"), show="headings",
                                      height=6)
        self.flow_tree.heading("stage", text="流转环节")
        self.flow_tree.column("stage", width=200, anchor="w")
        self.flow_tree.heading("count", text="记录数")
        self.flow_tree.column("count", width=100, anchor="e")
        self.flow_tree.pack(fill="both", expand=True)

    def _flow_register_bag(self):
        if not self.need("flow_operate"):
            return
        bag_id = simpledialog.askstring("登记试卷袋", "试卷袋编号（RFID/二维码）：",
                                        parent=self)
        if not bag_id:
            return
        subject = simpledialog.askstring("登记试卷袋", "科目：", parent=self) or ""
        qty = simpledialog.askinteger("登记试卷袋", "份数：", parent=self,
                                      initialvalue=30) or 0
        self.flow.register_bag(bag_id.strip(), subject=subject, quantity=qty,
                               room_id=self.current_room.get().strip(),
                               operator=self.session.username)
        self.refresh_flow()

    def _pick_bag(self, title="选择试卷袋"):
        bags = self.store.bags()
        if not bags:
            messagebox.showinfo("提示", "还没有试卷袋，请先登记", parent=self)
            return None
        dlg = _PickDialog(self, title, [b.bag_id for b in bags],
                          ["%s / %s / %d份" % (b.bag_id, b.subject or "-", b.quantity)
                           for b in bags])
        self.wait_window(dlg)
        return dlg.result

    def _flow_handover(self):
        if not self.need("flow_operate"):
            return
        bag = self._pick_bag("选择要交接的试卷袋")
        if not bag:
            return
        who = self.session.username
        h, alarms = self.flow.handover.handover(
            bag, from_person="保密室", to_person=who,
            persons=["保密室", who], signatures=["保密室", who],
            faces=["保密室", who], method="scan",
            quantity=(self.store.get_bag(bag).quantity if self.store.get_bag(bag) else 0),
            room_id=self.current_room.get().strip(), stage="handover",
            operator=who)
        self.after_event("交接已登记（%s）%s"
                         % ("可追溯" if h.complete() else "不完整",
                            "；产生 %d 条告警" % len(alarms) if alarms else ""))

    def _flow_seal(self):
        if not self.need("flow_operate"):
            return
        bag = self._pick_bag("选择要封签的试卷袋")
        if not bag:
            return
        mode = messagebox.askyesno(
            "电子铅封", "点「是」= 正常封签；点「否」= 上报【异常开启】（演练用）",
            parent=self)
        if mode:
            seal_id = simpledialog.askstring("电子铅封", "封签编号：", parent=self,
                                             initialvalue="SEAL-%04d" % (int(time.time()) % 10000)) or ""
            self.flow.seal.seal(bag, seal_id, self.session.username,
                                room_id=self.current_room.get().strip())
            self.after_event("已电子封签 %s" % bag)
        else:
            self.flow.seal.report(bag, "broken", person=self.session.username,
                                  detail="人为上报的异常开启（演练）")
            self.after_event("已上报电子铅封异常开启 -> 红色告警")

    def _flow_escort(self):
        if not self.need("escort_operate"):
            return
        dlg = _EscortDialog(self, self.store)
        self.wait_window(dlg)
        if dlg.result:
            self.after_event("押运任务已创建：%s" % dlg.result)

    def _flow_deliver(self):
        if not self.need("flow_operate"):
            return
        bag = self._pick_bag("选择要分发的试卷袋")
        if not bag:
            return
        room = simpledialog.askstring("分发至考场", "考场编号：", parent=self,
                                      initialvalue="R001") or ""
        b = self.store.get_bag(bag)
        qty = simpledialog.askinteger("分发至考场", "分发份数：", parent=self,
                                      initialvalue=(b.quantity if b else 30)) or 0
        d, alarms = self.flow.delivery.deliver(bag, room, qty,
                                               self.session.username)
        self.after_event("分发已登记（%s）%s" % (room,
                         "；%d 条告警" % len(alarms) if alarms else ""))

    def _flow_recycle(self):
        if not self.need("flow_operate"):
            return
        bag = self._pick_bag("选择要回收的试卷袋")
        if not bag:
            return
        b = self.store.get_bag(bag)
        issued = b.quantity if b else 0
        got = simpledialog.askinteger("回收核验", "回收扫码实测份数（应发 %d）：" % issued,
                                      parent=self, initialvalue=issued) or 0
        auto = messagebox.askyesno("回收与封存", "是否同时完成电子封存？", parent=self)
        r, alarms = self.flow.recycle.recycle(
            bag, self.current_room.get().strip(), issued, got,
            person=self.session.username,
            seal_id=("SEAL-R%d" % (int(time.time()) % 10000)) if auto else "",
            auto_seal=auto)
        self.after_event("回收已登记（差异 %+d）%s" % (r.diff,
                         "；%d 条告警" % len(alarms) if alarms else ""))

    def _show_trace(self):
        sel = self.bag_tree.selection()
        if not sel:
            return
        bag_id = self.bag_tree.item(sel[0], "values")[0]
        t = self.flow.bag_trace(bag_id)
        if not t:
            return
        L = []
        b = t["bag"]
        L.append("试卷袋 %s" % b.bag_id)
        L.append("  科目：%s   份数：%d   考场：%s" % (b.subject or "-", b.quantity,
                                                b.room_id or "-"))
        L.append("  封签：%s  状态：%s  封签时间：%s"
                 % (b.seal_id or "-", b.seal_state, b.seal_at or "-"))
        L.append("  当前环节：%s" % (b.current_stage or "-"))
        L.append("")
        L.append("流转链条（人 / 时 / 物 / 地点）：")
        for h in t["history"]:
            L.append("  · [%s] %s  操作人=%s  %s"
                     % (h.get("ts", ""), h.get("stage_text", h.get("stage", "")),
                        h.get("person", "-"), h.get("note", "")))
        L.append("")
        L.append("交接记录 %d 条：" % len(t["handovers"]))
        for h in t["handovers"]:
            L.append("  · %s %s 双方=%s->%s 参与 %d 人 人脸 %d 人 %s"
                     % (h.ts, h.stage, h.from_person or "-", h.to_person or "-",
                        len(h.persons), len(h.faces),
                        "可追溯" if h.complete() else "不完整"))
        L.append("")
        L.append("分发记录 %d 条：" % len(t["deliveries"]))
        for d in t["deliveries"]:
            L.append("  · %s -> 考场 %s  %d 份  责任人 %s"
                     % (d.ts, d.room_id, d.quantity, d.person or "-"))
        L.append("")
        L.append("回收记录 %d 条：" % len(t["recycles"]))
        for r in t["recycles"]:
            L.append("  · %s 回收 %d/%d 差异 %+d 封存=%s"
                     % (r.ts, r.returned_qty, r.issued_qty, r.diff,
                        "是" if r.sealed else "否"))
        self.trace_txt.delete("1.0", "end")
        self.trace_txt.insert("1.0", "\n".join(L))

    def refresh_flow(self):
        self.bag_tree.delete(*self.bag_tree.get_children())
        for b in self.store.bags():
            self.bag_tree.insert("", "end", values=(
                b.bag_id, b.subject or "-", b.quantity, b.seal_id or "-",
                b.seal_state, b.current_stage or "-"))
        self.flow_tree.delete(*self.flow_tree.get_children())
        for key, d in self.flow.stage_coverage().items():
            self.flow_tree.insert("", "end", values=(d["name"], d["count"]))

    # ================= 页签 4：告警中心 =================
    def _build_alarm(self):
        f = self.tab_alarm
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=6, pady=6)
        self.alarm_filter = tk.StringVar(value="全部")
        ttk.Label(bar, text="筛选").pack(side="left")
        cb = ttk.Combobox(bar, width=12, textvariable=self.alarm_filter,
                          values=["全部", "待处置", "已确认", "已处置", "红色", "橙色"])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_alarms())
        ttk.Button(bar, text="⟳ 刷新", command=self.refresh_alarms).pack(side="left", padx=4)
        ttk.Button(bar, text="✔ 确认选中", command=self._ack_alarm).pack(side="left", padx=4)
        ttk.Button(bar, text="✅ 处置选中", command=self._handle_alarm).pack(side="left",
                                                                          padx=4)
        ttk.Button(bar, text="✔✔ 确认全部红色",
                   command=self._ack_all_red).pack(side="left", padx=4)

        self.alarm_tree = ttk.Treeview(
            f, columns=("ts", "level", "code", "title", "scope", "latency", "status",
                        "by", "note"), show="headings", height=18)
        for k, t, w in (("ts", "时间", 140), ("level", "级别", 70),
                        ("code", "告警码", 170), ("title", "告警项", 200),
                        ("scope", "对象", 110), ("latency", "时延", 80),
                        ("status", "状态", 70), ("by", "处置人", 80),
                        ("note", "备注", 180)):
            self.alarm_tree.heading(k, text=t)
            self.alarm_tree.column(k, width=w, anchor="w")
        self.alarm_tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.alarm_tree.tag_configure("red", background="#ffe5e5")
        self.alarm_tree.tag_configure("orange", background="#fff6e0")

    def _alarm_rows(self):
        f = self.alarm_filter.get()
        rows = self.store.alarms(limit=500)
        if f == "待处置":
            rows = [a for a in rows if a.status == models.ST_PENDING]
        elif f == "已确认":
            rows = [a for a in rows if a.status == models.ST_ACKED]
        elif f == "已处置":
            rows = [a for a in rows if a.status == models.ST_HANDLED]
        elif f == "红色":
            rows = [a for a in rows if a.level == config.LEVEL_RED]
        elif f == "橙色":
            rows = [a for a in rows if a.level == config.LEVEL_ORANGE]
        return rows

    def refresh_alarms(self):
        self.alarm_tree.delete(*self.alarm_tree.get_children())
        for a in self._alarm_rows():
            self.alarm_tree.insert("", "end", iid=a.alarm_id, values=(
                a.created_at, a.level_name(), a.code, a.title, a.scope or "-",
                ("%.3fs" % a.latency_sec) if a.latency_sec is not None else "—",
                a.status, a.handled_by or a.acked_by or "-", a.handle_note or ""),
                tags=("red" if a.is_red() else "orange",))
        self._update_red_badge()

    def _sel_alarm(self):
        sel = self.alarm_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条告警", parent=self)
            return None
        return sel[0]

    def _ack_alarm(self):
        if not self.need("alarm_handle"):
            return
        aid = self._sel_alarm()
        if not aid:
            return
        note = simpledialog.askstring("确认告警", "备注（可选）：", parent=self) or ""
        self.hub.ack(aid, self.session.username, note)
        self.refresh_alarms()

    def _handle_alarm(self):
        if not self.need("alarm_handle"):
            return
        aid = self._sel_alarm()
        if not aid:
            return
        note = simpledialog.askstring("处置告警", "处置说明：", parent=self) or ""
        self.hub.handle(aid, self.session.username, note)
        self.refresh_alarms()

    def _ack_all_red(self):
        if not self.need("alarm_handle"):
            return
        n = self.hub.ack_all_red(self.session.username)
        messagebox.showinfo("完成", "已确认 %d 条红色告警" % n, parent=self)
        self.refresh_alarms()

    def _update_red_badge(self):
        n = self.hub.open_red_count()
        self.red_lbl.config(text=("🔴 未处置红色告警 %d 条" % n) if n else "")

    # ================= 页签 5：关键指标 =================
    def _build_kpi(self):
        f = self.tab_kpi
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="保密室编号").pack(side="left", padx=4)
        ttk.Entry(bar, textvariable=self.current_room, width=16).pack(side="left")
        ttk.Button(bar, text="⟳ 重新核算", command=self.refresh_kpi).pack(side="left",
                                                                        padx=4)
        ttk.Button(bar, text="📄 生成验收报告", command=self._gen_report).pack(
            side="left", padx=4)
        ttk.Button(bar, text="📤 导出台账 CSV", command=self._export_csv).pack(
            side="left", padx=4)
        ttk.Button(bar, text="🔐 校验审计链", command=self._verify_audit).pack(
            side="left", padx=4)

        self.kpi_tree = ttk.Treeview(f, columns=("name", "required", "actual",
                                                 "verdict", "how"),
                                     show="headings", height=8)
        for k, t, w in (("name", "指标项", 220), ("required", "要求", 220),
                        ("actual", "实测", 110), ("verdict", "结论", 90),
                        ("how", "数据来源", 340)):
            self.kpi_tree.heading(k, text=t)
            self.kpi_tree.column(k, width=w, anchor="w")
        self.kpi_tree.pack(fill="x", padx=6, pady=4)
        self.kpi_tree.tag_configure("ok", background="#e8f5e9")
        self.kpi_tree.tag_configure("bad", background="#ffe5e5")
        self.kpi_tree.tag_configure("na", background="#f2f2f2")

        box = ttk.LabelFrame(f, text="报告预览")
        box.pack(fill="both", expand=True, padx=6, pady=6)
        self.report_txt = tk.Text(box, wrap="none", font=("Consolas", 9))
        ys = ttk.Scrollbar(box, orient="vertical", command=self.report_txt.yview)
        xs = ttk.Scrollbar(box, orient="horizontal", command=self.report_txt.xview)
        self.report_txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.report_txt.pack(fill="both", expand=True, padx=4, pady=4)

    def refresh_kpi(self):
        room = self.current_room.get().strip() or None
        s = self.kpi.summary(room)
        self.kpi_tree.delete(*self.kpi_tree.get_children())
        for r in s["rows"]:
            tag = {"达标": "ok", "未达标": "bad"}.get(r["verdict"], "na")
            self.kpi_tree.insert("", "end", values=(
                r["name"], r["required"], r["actual_text"], r["verdict"], r["how"]),
                tags=(tag,))
        text = self.evidence.full_report(room, generated_by=self.session.username)
        self.report_txt.delete("1.0", "end")
        self.report_txt.insert("1.0", text)

    def _gen_report(self):
        if not self.need("report_export"):
            return
        room = self.current_room.get().strip() or None
        path, _ = self.evidence.save_report(room, self.session.username)
        self.status.set("报告已生成：%s" % path)
        messagebox.showinfo("已生成", "验收报告已保存：\n%s" % path, parent=self)

    def _export_csv(self):
        if not self.need("report_export"):
            return
        out = self.evidence.export_ledgers()
        lines = ["%s -> %s" % (k, v or "(无数据)") for k, v in out.items()]
        messagebox.showinfo("导出完成", "\n".join(lines), parent=self)

    def _verify_audit(self):
        if not self.need("audit_view"):
            return
        ok, why = self.store.audit_verify()
        dlg = _AuditDialog(self, self.store, ok, why)
        self.wait_window(dlg)

    # ================= 页签 6：系统设置 =================
    def _build_sys(self):
        f = self.tab_sys
        left = ttk.LabelFrame(f, text="账户")
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.acc_tree = ttk.Treeview(left, columns=("user", "role", "state", "created"),
                                     show="headings", height=10)
        for k, t, w in (("user", "用户名", 120), ("role", "角色", 100),
                        ("state", "状态", 80), ("created", "创建时间", 150)):
            self.acc_tree.heading(k, text=t)
            self.acc_tree.column(k, width=w, anchor="w")
        self.acc_tree.pack(fill="both", expand=True, padx=4, pady=4)
        ab = ttk.Frame(left)
        ab.pack(fill="x", padx=4, pady=4)
        ttk.Button(ab, text="🔑 修改我的口令",
                   command=self._change_pwd).pack(side="left", padx=3)
        ttk.Button(ab, text="＋ 新建账户", command=self._new_account).pack(side="left",
                                                                        padx=3)
        ttk.Button(ab, text="🗑 删除账户", command=self._del_account).pack(side="left",
                                                                        padx=3)
        ttk.Button(ab, text="⏸ 停用/启用", command=self._toggle_account).pack(
            side="left", padx=3)

        right = ttk.LabelFrame(f, text="运行与合规")
        right.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=6)
        self.hide_cmd_var = tk.BooleanVar(value=web_accounts.hide_cmd_enabled())
        self.hide_chk = ttk.Checkbutton(
            right, text="隐藏启动时的命令行黑窗口（所有者）",
            variable=self.hide_cmd_var, command=self._toggle_hide_cmd)
        self.hide_chk.pack(anchor="w", padx=6, pady=6)
        ttk.Button(right, text="🔐 查看审计链", command=self._verify_audit).pack(
            anchor="w", padx=6, pady=3)
        ttk.Button(right, text="⏹ 停止全部取流", command=self._stop_all).pack(
            anchor="w", padx=6, pady=3)

        info = tk.Text(right, height=14, width=54, wrap="word", font=("Consolas", 9))
        info.pack(fill="both", expand=True, padx=6, pady=6)
        info.insert("1.0", self._sys_info())
        info.configure(state="disabled")
        ttk.Button(right, text="🚪 退出登录/关闭",
                   command=self.app.close_app).pack(anchor="e", padx=6, pady=6)

    def _sys_info(self):
        return (
            "数据目录：%s\n"
            "数据库：%s\n"
            "录像留存：%d 天（WORM：%s）\n"
            "告警阈值：开柜/开门 ≤ %.0f 秒；押运偏离 ≤ %.0f 秒\n"
            "覆盖率要求：%.0f%%（%s）\n"
            "门禁：双因子 %d 项 + 双人到场 %d 人\n"
            "环境阈值：温度 %.0f~%.0f℃，湿度 %.0f~%.0f%%\n"
            "布防时段：%s\n"
            % (config.DATA_ROOT, config.DB_PATH, config.RECORD_RETAIN_DAYS,
               "开" if config.RECORD_WORM else "关",
               config.INTRUSION_SLA_SEC, config.ESCORT_DEVIATE_SLA_SEC,
               config.COVER_RATE_REQUIRED * 100, "无盲区",
               config.ACCESS_FACTORS_REQUIRED, config.DUAL_CUSTODY_REQUIRED,
               config.ENV_THRESHOLDS["temp_c"]["min"],
               config.ENV_THRESHOLDS["temp_c"]["max"],
               config.ENV_THRESHOLDS["humidity_pct"]["min"],
               config.ENV_THRESHOLDS["humidity_pct"]["max"],
               "、".join("%02d:00-%02d:00" % (a, b)
                         for a, b, _, _ in config.ARMED_WINDOWS)))

    def _stop_all(self):
        self.cameras.stop_all()
        for cid in list(self._tile_widgets):
            self._drop_tile(cid)
        self.status.set("已停止全部取流")

    # ---- 账户 ----
    def refresh_accounts(self):
        am = web_accounts.AccountManager(self.store)
        self.acc_tree.delete(*self.acc_tree.get_children())
        for name, a in sorted(am.accounts().items()):
            self.acc_tree.insert("", "end", values=(
                name, web_accounts.role_text(a.get("role")),
                "已停用" if a.get("disabled") else "启用", a.get("created") or "-"))

    def _change_pwd(self):
        if not self.need("change_own_pwd"):
            return
        new = simpledialog.askstring("修改口令", "新口令（至少 %d 位）："
                                     % config.PWD_MIN_LEN, parent=self, show="*")
        if not new:
            return
        ok, msg = web_accounts.AccountManager(self.store).change_password(
            self.session, self.session.username, new)
        messagebox.showinfo("结果", msg, parent=self)

    def _new_account(self):
        if not self.need("account_manage"):
            return
        name = simpledialog.askstring("新建账户", "用户名：", parent=self)
        if not name:
            return
        pwd = simpledialog.askstring("新建账户", "初始口令（≥%d 位）："
                                     % config.PWD_MIN_LEN, parent=self, show="*")
        roles = [web_accounts.role_text(r) for r in config.ROLE_LEVEL
                 if web_accounts.role_level(r) < web_accounts.role_level(self.session.role)]
        if not roles:
            messagebox.showwarning("权限不足", "你的等级下没有可新建的角色", parent=self)
            return
        dlg = _PickDialog(self, "选择角色", roles, roles)
        self.wait_window(dlg)
        if not dlg.result:
            return
        role = [r for r in config.ROLE_LEVEL
                if web_accounts.role_text(r) == dlg.result][0]
        ok, msg = web_accounts.AccountManager(self.store).create(self.session, name,
                                                                 pwd, role)
        messagebox.showinfo("结果", msg, parent=self)
        self.refresh_accounts()

    def _del_account(self):
        if not self.need("account_manage"):
            return
        sel = self.acc_tree.selection()
        if not sel:
            return
        name = self.acc_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno("确认", "删除账户 %s？" % name, parent=self):
            return
        ok, msg = web_accounts.AccountManager(self.store).delete(self.session, name)
        messagebox.showinfo("结果", msg, parent=self)
        self.refresh_accounts()

    def _toggle_account(self):
        if not self.need("account_manage"):
            return
        sel = self.acc_tree.selection()
        if not sel:
            return
        vals = self.acc_tree.item(sel[0], "values")
        name, state = vals[0], vals[2]
        ok, msg = web_accounts.AccountManager(self.store).set_disabled(
            self.session, name, state != "已停用")
        messagebox.showinfo("结果", msg, parent=self)
        self.refresh_accounts()

    def _toggle_hide_cmd(self):
        if not self.need("hide_cmd_toggle"):
            self.hide_cmd_var.set(web_accounts.hide_cmd_enabled())
            return
        web_accounts.set_hide_cmd(self.hide_cmd_var.get())
        self.status.set("隐藏命令行窗口：%s（下次启动生效）"
                        % ("开" if self.hide_cmd_var.get() else "关"))

    # ================= 通用刷新 =================
    def refresh_rooms(self):
        rooms = self.store.rooms()
        if not self.current_room.get() and rooms:
            self.current_room.set(rooms[0])
        self.room_combo.configure(values=rooms)

    def refresh_vault(self):
        room = self.current_room.get().strip()
        if not room:
            return
        rate, detail = self.vault.cameras.coverage(room)
        blind = self.vault.cameras.blind_spots(room)
        self.cov_lbl.config(
            text="六面覆盖率：%.0f%%（要求 100%%）%s"
                 % (rate * 100,
                    "   ⚠ 盲区：%s" % "、".join(blind) if blind else "   ✔ 无盲区"),
            foreground=((ui_theme.ALARM if ui_theme else "#c00000") if blind
                        else (ui_theme.OK if ui_theme else "#1b7f1b")))
        self.cov_tree.delete(*self.cov_tree.get_children())
        for f in config.COVER_FACES:
            d = detail[f]
            self.cov_tree.insert("", "end", values=(
                d["face_text"], "、".join(d["cams"]) or "未登记",
                "已配置" if d["configured"] else "缺失",
                "、".join(config.FACE_CHECKPOINTS[f]),
                "、".join(d["missing_checkpoints"]) or "无"))

        self.cam_tree.delete(*self.cam_tree.get_children())
        for c in self.store.cameras(room):
            self.cam_tree.insert("", "end", values=(
                c.cam_id, c.name, config.FACE_TEXT.get(c.face, c.face), c.url,
                "、".join(c.channels), "在线" if c.online else "离线",
                "；".join(c.ledger_problems()) or "无"))

        self.check_tree.delete(*self.check_tree.get_children())
        for it in self.vault.self_check(room):
            self.check_tree.insert("", "end", values=(
                it["name"], "✔" if it["ok"] else "✘", it["detail"]))

    def after_event(self, msg):
        self.status.set(msg)
        self.refresh_vault()
        self.refresh_flow()
        self.refresh_alarms()
        self.refresh_kpi()
        self._update_red_badge()

    # ---------------- UI 队列（后台线程回报） ----------------
    def _poll_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "fire":
                    self._on_fire_result(payload[0], payload[1])
                elif kind == "probe":
                    if payload:
                        self.cam_index.configure(values=[str(i) for i in payload])
                        self.cam_index.set(str(payload[0]))
                        self.status.set("探测到本机摄像头序号：%s"
                                        % "、".join(str(i) for i in payload))
                    else:
                        self.status.set("未探测到可用本机摄像头"
                                        "（可改用 RTSP 地址或本地视频文件）")
                        messagebox.showinfo(
                            "未发现摄像头",
                            "本机没有可用的摄像头设备（或系统未授权）。\n\n"
                            "可以：\n"
                            "· 改用「RTSP/HTTP 地址」接真实网络摄像机；\n"
                            "· 或用「本地视频文件」验证取流链路与画面显示。",
                            parent=self)
        except queue.Empty:
            pass
        self.after(200, self._poll_ui_queue)

    # ---------------- 关窗 ----------------
    def shutdown(self):
        try:
            if self._live_after:
                self.after_cancel(self._live_after)
        except Exception:
            pass
        self.cameras.stop_all()


# ==================================================================
# 子对话框
# ==================================================================
class _CameraDialog(tk.Toplevel):
    """登记/编辑一个机位。"""

    def __init__(self, parent, store, cam_id, url):
        tk.Toplevel.__init__(self, parent)
        self.title("登记机位")
        self.result = None
        self.store = store
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="机位ID").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self.cam_id = ttk.Entry(self, width=26)
        self.cam_id.grid(row=0, column=1, pady=6)
        self.cam_id.insert(0, cam_id)

        ttk.Label(self, text="保密室编号").grid(row=1, column=0, sticky="e", padx=6)
        self.room = ttk.Entry(self, width=26)
        self.room.grid(row=1, column=1, pady=6)

        ttk.Label(self, text="机位名称").grid(row=2, column=0, sticky="e", padx=6)
        self.name = ttk.Entry(self, width=26)
        self.name.grid(row=2, column=1, pady=6)

        ttk.Label(self, text="朝向（六面）").grid(row=3, column=0, sticky="e", padx=6)
        self.face = ttk.Combobox(self, width=24, state="readonly",
                                 values=["%s（%s）" % (config.FACE_TEXT[f], f)
                                         for f in config.COVER_FACES])
        self.face.current(0)
        self.face.grid(row=3, column=1, pady=6)

        ttk.Label(self, text="取流地址").grid(row=4, column=0, sticky="e", padx=6)
        self.url = ttk.Entry(self, width=26)
        self.url.grid(row=4, column=1, pady=6)
        self.url.insert(0, url)

        ttk.Label(self, text="安装角度(°）").grid(row=5, column=0, sticky="e", padx=6)
        self.angle = ttk.Entry(self, width=26)
        self.angle.insert(0, "0")
        self.angle.grid(row=5, column=1, pady=6)

        ttk.Label(self, text="必拍要点").grid(row=6, column=0, sticky="ne", padx=6)
        self.cps = tk.Listbox(self, selectmode="multiple", height=5, width=30,
                              exportselection=False)
        for f in config.COVER_FACES:
            for c in config.FACE_CHECKPOINTS[f]:
                self.cps.insert("end", c)
        self.cps.grid(row=6, column=1, pady=6)
        self.face.bind("<<ComboboxSelected>>", lambda e: self._sync_cps())

        ttk.Label(self, text="冗余录像通道").grid(row=7, column=0, sticky="ne", padx=6)
        self.ch_main = tk.BooleanVar(value=True)
        self.ch_bak = tk.BooleanVar(value=True)
        chf = ttk.Frame(self)
        chf.grid(row=7, column=1, sticky="w", pady=6)
        ttk.Checkbutton(chf, text="主通道 main", variable=self.ch_main).pack(anchor="w")
        ttk.Checkbutton(chf, text="备通道 backup", variable=self.ch_bak).pack(anchor="w")

        btns = ttk.Frame(self)
        btns.grid(row=8, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="保存", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)
        self._sync_cps()

    def _sync_cps(self):
        face = config.COVER_FACES[self.face.current()]
        need = config.FACE_CHECKPOINTS[face]
        self.cps.selection_clear(0, "end")
        for i in range(self.cps.size()):
            if self.cps.get(i) in need:
                self.cps.selection_set(i)

    def _save(self):
        cid = self.cam_id.get().strip()
        if not cid:
            messagebox.showwarning("提示", "机位ID不能为空", parent=self)
            return
        try:
            angle = float(self.angle.get() or 0)
        except ValueError:
            angle = 0.0
        face = config.COVER_FACES[self.face.current()]
        cps = [self.cps.get(i) for i in self.cps.curselection()]
        chs = []
        if self.ch_main.get():
            chs.append("main")
        if self.ch_bak.get():
            chs.append("backup")
        try:
            cam = models.Camera(cid, self.room.get().strip(), self.name.get().strip(),
                                face, url=self.url.get().strip(), angle_deg=angle,
                                checkpoints=cps, channels=chs)
            self.store.save_camera(cam)
            self.store.log("登记机位", "%s %s" % (cid, self.name.get().strip()), "gui")
            self.result = cid
        except Exception as e:                                  # noqa: BLE001
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.destroy()


class _PickDialog(tk.Toplevel):
    """通用单选对话框：values 与 labels 一一对应。"""

    def __init__(self, parent, title, values, labels):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.result = None
        self.values = list(values)
        self.transient(parent)
        self.grab_set()
        ttk.Label(self, text=title).pack(padx=10, pady=(10, 4))
        self.lb = tk.Listbox(self, height=min(12, max(3, len(labels))), width=52,
                             exportselection=False)
        for lb in labels:
            self.lb.insert("end", lb)
        self.lb.pack(padx=10)
        if labels:
            self.lb.selection_set(0)
        b = ttk.Frame(self)
        b.pack(pady=10)
        ttk.Button(b, text="确定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.lb.bind("<Double-Button-1>", lambda e: self._ok())

    def _ok(self):
        sel = self.lb.curselection()
        if sel:
            self.result = self.values[sel[0]]
        self.destroy()


class _EscortDialog(tk.Toplevel):
    """创建押运任务 + 逐点上报轨迹（演练用）。"""

    def __init__(self, parent, store):
        tk.Toplevel.__init__(self, parent)
        self.title("押运任务（北斗/GPS 定位 + 车内视频 + 路线偏离）")
        self.result = None
        self.store = store
        self.parent = parent
        self.transient(parent)
        self.grab_set()
        self.flow = parent.flow

        r = 0
        ttk.Label(self, text="车牌").grid(row=r, column=0, sticky="e", padx=6, pady=5)
        self.plate = ttk.Entry(self, width=24)
        self.plate.insert(0, "京A·12345")
        self.plate.grid(row=r, column=1, pady=5); r += 1
        ttk.Label(self, text="驾驶员").grid(row=r, column=0, sticky="e", padx=6)
        self.driver = ttk.Entry(self, width=24)
        self.driver.grid(row=r, column=1, pady=5); r += 1
        ttk.Label(self, text="押运人员（逗号分隔）").grid(row=r, column=0, sticky="e",
                                                      padx=6)
        self.persons = ttk.Entry(self, width=24)
        self.persons.insert(0, "押运员甲,押运员乙")
        self.persons.grid(row=r, column=1, pady=5); r += 1
        ttk.Label(self, text="试卷袋（逗号分隔）").grid(row=r, column=0, sticky="e",
                                                     padx=6)
        self.bags = ttk.Entry(self, width=24)
        self.bags.grid(row=r, column=1, pady=5); r += 1
        ttk.Label(self, text="车内视频地址").grid(row=r, column=0, sticky="e", padx=6)
        self.vurl = ttk.Entry(self, width=24)
        self.vurl.insert(0, "rtsp://car-cam/stream1")
        self.vurl.grid(row=r, column=1, pady=5); r += 1

        ttk.Label(self, text="规划路线（每行 lat,lng）").grid(row=r, column=0, sticky="ne",
                                                          padx=6)
        self.route = tk.Text(self, height=5, width=26, font=("Consolas", 9))
        self.route.insert("1.0", "39.9042,116.4074\n39.9100,116.4200\n"
                                 "39.9200,116.4400")
        self.route.grid(row=r, column=1, pady=5); r += 1

        ttk.Label(self, text="上报轨迹点（每行 lat,lng,speed）").grid(
            row=r, column=0, sticky="ne", padx=6)
        self.pts = tk.Text(self, height=5, width=26, font=("Consolas", 9))
        self.pts.insert("1.0", "39.9045,116.4078,40\n39.9102,116.4205,45\n"
                               "39.9300,116.5100,50")
        self.pts.grid(row=r, column=1, pady=5); r += 1

        b = ttk.Frame(self)
        b.grid(row=r, column=0, columnspan=2, pady=10)
        ttk.Button(b, text="创建任务并模拟上报", command=self._run).pack(side="left",
                                                                    padx=6)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="left", padx=6)

    @staticmethod
    def _parse_route(txt):
        out = []
        for line in txt.strip().splitlines():
            parts = [p.strip() for p in line.replace("，", ",").split(",") if p.strip()]
            if len(parts) >= 2:
                try:
                    out.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
        return out

    @staticmethod
    def _parse_pts(txt):
        out = []
        for line in txt.strip().splitlines():
            parts = [p.strip() for p in line.replace("，", ",").split(",") if p.strip()]
            if len(parts) >= 2:
                try:
                    spd = float(parts[2]) if len(parts) > 2 else 0.0
                    out.append((float(parts[0]), float(parts[1]), spd))
                except ValueError:
                    pass
        return out

    def _run(self):
        route = self._parse_route(self.route.get("1.0", "end"))
        pts = self._parse_pts(self.pts.get("1.0", "end"))
        bags = [b.strip() for b in self.bags.get().replace("，", ",").split(",")
                if b.strip()]
        persons = [p.strip() for p in self.persons.get().replace("，", ",").split(",")
                   if p.strip()]
        try:
            t = self.flow.escort.start_task(
                self.plate.get().strip(), driver=self.driver.get().strip(),
                escort_persons=persons, bag_ids=bags, route=route,
                video_url=self.vurl.get().strip(),
                operator=self.parent.session.username)
        except ValueError as e:
            messagebox.showwarning("参数不合法", str(e), parent=self)
            return
        dev = 0
        for i, (lat, lng, spd) in enumerate(pts):
            _, alarm = self.flow.escort.report_position(t.task_id, lat, lng,
                                                       speed_kmh=spd,
                                                       source="beidou")
            if alarm:
                dev += 1
        self.flow.escort.report_video(t.task_id, True)
        self.result = t.task_id
        messagebox.showinfo("完成",
                            "押运任务已创建：%s\n上报 %d 个轨迹点，其中 %d 次触发偏离告警"
                            % (t.task_id, len(pts), dev), parent=self)
        self.destroy()


class _AuditDialog(tk.Toplevel):
    def __init__(self, parent, store, ok, why):
        tk.Toplevel.__init__(self, parent)
        self.title("审计哈希链")
        self.transient(parent)
        ttk.Label(self, text="校验结果：%s（%s）" % ("通过" if ok else "失败", why),
                  foreground="#1b7f1b" if ok else "#c00000",
                  font=("Microsoft YaHei", 10, "bold")).pack(padx=10, pady=8)
        t = tk.Text(self, width=110, height=22, font=("Consolas", 9))
        t.pack(padx=10, pady=(0, 8))
        for r in store.audit_rows(limit=400):
            t.insert("end", "#%-4d %s  %-12s %-28s %s  %s…\n"
                     % (r["seq"], r["ts"], r["action"], r["detail"][:28],
                        r["operator"], (r["hash"] or "")[:12]))
        t.configure(state="disabled")
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=(0, 10))


# ==================================================================
# 应用外壳
# ==================================================================
class App(object):
    def __init__(self):
        config.ensure_stdio()
        self.store = Store()
        self.root = tk.Tk()
        self.root.title("试卷流转与保密室智能监控 · 专题 08")
        self.root.geometry("1280x860")
        if ui_theme is not None:
            ui_theme.apply(self.root)
        self.main = None
        if not self._try_sso():
            self._show_login()

    def _try_sso(self):
        """总控台一次登录后直达 08：不再要求输入第二遍账户/口令。"""
        if sso is None:
            return False
        info = sso.verify_launch("08")
        if not info:
            return False
        role = sso.map_role(info["role"], {
            "owner": config.ROLE_OWNER,
            "admin": config.ROLE_ADMIN,
            "patrol": config.ROLE_GUARD,
            "invigilator": config.ROLE_GUARD,
            "checker": config.ROLE_AUDITOR,
        }, config.ROLE_GUARD)
        sess = web_accounts.Session(info["username"], role)
        try:
            self.store.log("单点登录", "%s（%s，来自总控台）"
                           % (sess.username, web_accounts.role_text(sess.role)),
                           sess.username)
        except Exception:                                    # noqa: BLE001
            pass
        try:
            self._on_login(sess)
            if ui_theme is not None:
                ui_theme.polish_later(self.root)
            return True
        except Exception:                                    # noqa: BLE001
            self.main = None
            return False

    def _show_login(self):
        self.login = LoginFrame(self, self._on_login)
        self.login.pack(fill="both", expand=True)
        if ui_theme is not None:
            ui_theme.polish(self.root)

    def _on_login(self, session):
        if getattr(self, "login", None) is not None:
            self.login.destroy()
            self.login = None
        self.main = MainFrame(self, session)
        self.main.pack(fill="both", expand=True)
        self.main.refresh_accounts()
        self.main.refresh_vault()
        self.main.refresh_flow()
        self.main.refresh_alarms()
        self.main.refresh_kpi()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def close_app(self):
        if self.main is not None:
            try:
                self.main.shutdown()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
