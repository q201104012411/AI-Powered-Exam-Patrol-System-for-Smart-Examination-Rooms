# -*- coding: utf-8 -*-
"""考生作弊智能识别 · 巡考监控界面（Tkinter，零第三方依赖）。

运行：
    双击 启动界面.bat            （推荐）
    python 界面.py

界面布局：
    ┌──────────────────────────────────────────────────────────────┐
    │ 工具栏：场景选择 / 播放 / 暂停 / 单帧 / 重置 / 参数调节        │
    ├───────────────────┬──────────────────────────────────────────┤
    │ 考场实时画面       │ 风险分数（大号数字 + 评级）               │
    │ （俯视示意 + 姿态）│ 风险分数连续轨迹（阈值线 / 抑制点 / 告警区）│
    │ 当帧原始读数       │ 信号贡献分解 + 持续占比                   │
    │                   │ 窗口时间维度统计                          │
    ├───────────────────┴──────────────────────────────────────────┤
    │ 行为模式 / 可解释理由                                          │
    │ 告警事件列表                                                   │
    └──────────────────────────────────────────────────────────────┘

设计要点：
  · 界面直接驱动 cheatdetect 引擎的真实输出，不做任何"演示专用"的假数据；
  · 画面元素由当帧 FrameFeatures 绘制（头部 yaw/pitch、坐姿倾角、
    手部是否离桌、跨考生距离、违禁物体），所见即引擎输入；
  · 参数可实时调节并**重放**场景，用于直观展示"阈值 / 最短持续时长"
    对误报与漏报的影响；
  · 所有场景均为合成数据，界面底部常驻声明，不做实测精度暗示。
"""

import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheatdetect import CheatDetector, Config, SignalWeights          # noqa: E402
from cheatdetect.scoring import CN_NAME, SIGNAL_KEYS, WEIGHT_DIRECTION  # noqa: E402
from cheatdetect.simulate import FPS, SCENARIOS                        # noqa: E402

# 摄像头/视频接入（OpenCV 为可选依赖；缺失时仅合成场景可用，界面会说明）
try:
    from cheatdetect.videosource import (                              # noqa: E402
        HAS_OPENCV, KIND_CAMERA, KIND_FILE, KIND_STREAM, KIND_SYNTHETIC,
        list_cameras, make_source)
except Exception:                                                      # pragma: no cover
    HAS_OPENCV = False
    KIND_SYNTHETIC, KIND_CAMERA, KIND_FILE, KIND_STREAM = (
        "synthetic", "camera", "file", "stream")

    def list_cameras(max_index=5):
        return []

    def make_source(*a, **kw):
        raise RuntimeError("视频接入模块不可用")

try:
    from cheatdetect.perception import (                               # noqa: E402
        AVAIL_CN, CAMERA_SIGNAL_AVAIL, FramePerceiver, PerceptionConfig)
except Exception:                                                      # pragma: no cover
    FramePerceiver = None
    PerceptionConfig = None
    CAMERA_SIGNAL_AVAIL = {}
    AVAIL_CN = {}

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:                                                      # pragma: no cover
    HAS_PIL = False

# ---------------- 配色 ----------------
BG = "#EEF1F6"
PANEL = "#FFFFFF"
BORDER = "#D3DAE6"
INK = "#1F2937"
INK2 = "#5B6472"
ACCENT = "#1F3764"
OK = "#2E7D32"
WARN = "#B26A00"
ORANGE = "#E8833A"
RED = "#C0392B"
GRID = "#E6EAF2"

LEVEL_COLOR = {"正常": OK, "关注": WARN, "橙色预警": ORANGE, "红色预警": RED}

WEIGHT_COLOR = {"极高": "#C0392B", "高": "#E8833A", "中": "#2F6FB5", "辅助": "#8A93A3"}

TRAJ_LEN = 130          # 轨迹图保留的帧数
ROOM_W, ROOM_H = 418, 196


def pick_font(root, candidates, size, weight="normal"):
    """在系统可用字体中挑选第一款候选字体（避免中文缺字）。"""
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in candidates:
        if name in families:
            return (name, size, weight)
    return ("TkDefaultFont", size, weight)


class ScenarioPlayer:
    """场景播放器：按帧把合成场景喂给真实的 CheatDetector。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.det = CheatDetector(cfg)
        self.frames = []
        self.idx = 0
        self.spec = None

    def load(self, spec, cfg, seed=2026):
        """载入场景并重建引擎（参数改变时需要重建）。"""
        self.spec = spec
        self.cfg = cfg
        self.det = CheatDetector(cfg)
        self.frames = spec.generator(random.Random(seed))
        self.idx = 0

    @property
    def total(self):
        return len(self.frames)

    @property
    def finished(self):
        return self.idx >= len(self.frames)

    def step(self):
        """推进一步，返回 DetectionResult；已到末尾返回 None。"""
        if self.finished:
            return None
        frame = self.frames[self.idx]
        self.idx += 1
        return self.det.push(frame)

    def current_frame(self):
        """当前帧（刚处理完的那一帧）。"""
        if self.idx == 0:
            return None
        return self.frames[self.idx - 1]

    def finish(self):
        self.det.finish()


# ================================================================
# 实时视频显示（摄像头 / 视频文件 / 网络流）
# ================================================================
class VideoView(tk.Canvas):
    """把 OpenCV 帧显示到 Tk 画布（BGR→RGB→PIL→PhotoImage）。

    只做显示；检测框与估计值由感知层的 draw_overlay 画在帧上。
    """

    def __init__(self, master, width, height, font_ui, font_small, **kw):
        super().__init__(master, width=width, height=height, bg="#101418",
                         highlightthickness=0, **kw)
        self.f_ui = font_ui
        self.f_small = font_small
        self._photo = None            # 必须持有引用，否则会被回收成空白

    def show(self, frame_bgr):
        """显示一帧；尺寸按画布等比缩放后居中。"""
        if frame_bgr is None or not HAS_PIL:
            return
        try:
            import cv2
            cw = max(60, self.winfo_width() or int(self["width"]))
            chh = max(60, self.winfo_height() or int(self["height"]))
            h, w = frame_bgr.shape[:2]
            scale = min(cw / float(w), chh / float(h))
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            interp = cv2.INTER_LINEAR if scale > 1 else cv2.INTER_AREA
            small = cv2.resize(frame_bgr, (nw, nh), interpolation=interp)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            self._photo = ImageTk.PhotoImage(img)
            self.delete("all")
            self.create_image(cw // 2, chh // 2, image=self._photo)
        except Exception:
            pass

    def clear(self, text="未连接"):
        self.delete("all")
        self._photo = None
        w = self.winfo_width() or int(self["width"])
        h = self.winfo_height() or int(self["height"])
        self.create_text(w // 2, h // 2, text=text, fill="#8A93A3",
                         font=self.f_ui)


class CaptureWorker(threading.Thread):
    """后台采集线程：读帧 → 感知 → 投递到队列。**不触碰任何 Tk 对象**。

    推理（YOLO + Haar）放在后台线程，避免卡住界面；结果通过有界队列
    交给主线程消费，队列满时丢弃最旧的帧，保证显示的是最新画面。
    """

    def __init__(self, source, perceiver, fps, out_queue, stop_event):
        super().__init__(daemon=True)
        self.source = source
        self.perceiver = perceiver
        self.fps = max(1.0, float(fps))
        self.q = out_queue
        self.stop_event = stop_event
        self.error = ""
        self.frames = 0
        self.fps_measured = 0.0      # 实测处理速率（EMA）

    def run(self):
        interval = 1.0 / self.fps
        fid = 0
        fails = 0
        last_t = time.time()
        try:
            while not self.stop_event.is_set():
                t0 = time.time()
                ok, frame = self.source.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 40:
                        self.error = "连续读取失败，视频源可能已断开"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                try:
                    feats, info = self.perceiver.process(
                        frame, fid, fid / self.fps)
                except Exception as exc:
                    self.error = "感知失败：{}: {}".format(
                        type(exc).__name__, exc)
                    break
                if feats is not None:
                    fid += 1
                    self.frames += 1
                # 实测速率：反映"真实处理能力是否跟得上标称帧率"。
                # 若明显低于标称，引擎按 fid/fps 换算的时间轴会与真实时间脱节
                # （2 秒窗口会被拉长），界面必须如实提示，而不是假装实时。
                now = time.time()
                dt = now - last_t
                last_t = now
                if dt > 0:
                    inst = 1.0 / dt
                    self.fps_measured = (inst if self.fps_measured <= 0
                                         else self.fps_measured * 0.9 + inst * 0.1)
                try:
                    self.q.put_nowait((frame, feats, info))
                except queue.Full:
                    try:                       # 丢最旧的一帧
                        self.q.get_nowait()
                        self.q.put_nowait((frame, feats, info))
                    except Exception:
                        pass
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
        except Exception as exc:
            self.error = "采集线程异常：{}: {}".format(type(exc).__name__, exc)
        finally:
            try:
                self.source.release()
            except Exception:
                pass



class RoomView(tk.Canvas):
    """考场俯视示意图：把当帧 FrameFeatures 画成可读的姿态图形。

    绘制内容全部来自引擎输入，不是装饰：
      · 头部圆点 + 视线方向线（由 yaw 决定，relation 决定颜色）
      · 躯干椭圆（按 tilt 旋转）
      · 双手圆点（below_desk 时画到桌子下方；cross_distance 越小越靠邻座）
      · 违禁物体角标
    """

    def __init__(self, master, font_ui, font_small, **kw):
        super().__init__(master, width=ROOM_W, height=ROOM_H, bg=PANEL,
                         highlightthickness=0, **kw)
        self.f_ui = font_ui
        self.f_small = font_small

    def render(self, frame, result, spec_label=""):
        self.delete("all")
        if frame is None:
            self.create_text(ROOM_W // 2, ROOM_H // 2, text="按“播放”开始",
                             fill=INK2, font=self.f_ui)
            return

        # 3 列 x 2 排课桌；#07 为分析对象，#08 为其右侧邻座
        subject_idx, neighbor_idx = 4, 5
        flagged = result is not None and result.triggered

        for i in range(6):
            r, c = divmod(i, 3)
            cx = 70 + c * 140
            cy = 52 + r * 90
            is_sub = (i == subject_idx)
            outline = RED if (is_sub and flagged) else (ACCENT if is_sub else BORDER)
            self.create_rectangle(cx - 52, cy - 4, cx + 52, cy + 34,
                                  outline=outline, width=2 if is_sub else 1,
                                  fill="#F7F9FC")
            if is_sub:
                self._draw_subject(cx, cy, frame, flagged)
            elif i == neighbor_idx:
                self._draw_neighbor(cx, cy, frame)
            else:
                self._draw_other(cx, cy)      # 其余座位仅画示意轮廓

    def _draw_other(self, cx, cy):
        """无关座位：只画一个中性剪影，避免让观者误以为是检测结果。"""
        self.create_oval(cx - 11, cy - 34, cx + 11, cy - 12,
                         outline="#C7CDD9", width=1, fill="#EDEFF3")
        self.create_text(cx, cy + 44, text="—", fill="#B9C0CC",
                         font=self.f_small)

    def _draw_neighbor(self, cx, cy, frame):
        """邻座：示意轮廓 + 跨考生手部靠近时的虚线提示。"""
        self.create_oval(cx - 11, cy - 34, cx + 11, cy - 12,
                         outline=INK2, width=1, fill="#EDEFF3")
        self.create_text(cx, cy + 44, text="#08", fill=INK2,
                         font=self.f_small)
        d = frame.hand.cross_distance
        if d is not None and d < 0.08:
            # 两只手在桌面下方接近 —— 用虚线示意交互
            self.create_line(cx - 46, cy + 14, cx - 18, cy + 20,
                             dash=(3, 2), fill=ORANGE, width=2)

    def _draw_subject(self, cx, cy, frame, flagged):
        """分析对象：绘制引擎真实输入（头部朝向、坐姿、双手、违禁物体）。"""
        head, hand, posture = frame.head, frame.hand, frame.posture

        # —— 头部（relation 决定颜色）——
        rel = head.relation
        head_color = OK if rel == "on_paper" else (
            RED if rel in ("toward_neighbor", "toward_back") else ORANGE)
        hx, hy = cx, cy - 24
        self.create_oval(hx - 11, hy - 11, hx + 11, hy + 11,
                         outline=head_color, width=2, fill="#FFFFFF")

        # —— 视线方向：0 度朝下（看自己试卷），正角度向右偏 ——
        ang = math.radians(90.0 - head.yaw_deg)
        self.create_line(hx, hy, hx + 24 * math.cos(ang),
                         hy + 24 * math.sin(ang),
                         fill=head_color, width=2, arrow="last")

        # —— 躯干（按 tilt 平移，示意偏转）——
        tilt = max(-40.0, min(40.0, posture.tilt_deg))
        dx = math.sin(math.radians(tilt)) * 8.0
        self.create_oval(cx - 15 + dx, cy - 8, cx + 15 + dx, cy + 16,
                         outline=(ORANGE if abs(tilt) > 25 else BORDER),
                         width=1, fill="#FAFBFD")

        # —— 双手 ——
        if hand.below_desk:
            hy2, hand_color = cy + 28, ORANGE
        else:
            hy2, hand_color = cy + 10, INK2
        d = hand.cross_distance
        shift = max(0.0, (0.08 - d) / 0.08) * 34.0 if d is not None else 0.0
        self.create_oval(cx - 28, hy2 - 4, cx - 20, hy2 + 4,
                         outline=hand_color, fill="#FFFFFF")
        self.create_oval(cx + 6 + shift, hy2 - 4, cx + 14 + shift, hy2 + 4,
                         outline=(RED if shift > 6 else hand_color),
                         fill="#FFFFFF")

        # —— 违禁物体角标 ——
        if frame.prohibited_objects:
            first = frame.prohibited_objects[0]
            name = str(first[0]) if isinstance(first, (tuple, list)) else str(first)
            self.create_rectangle(cx + 16, cy - 44, cx + 16 + 30, cy - 28,
                                  outline=RED, width=1, fill="#FDECEA")
            self.create_text(cx + 31, cy - 36, text=name[:4], fill=RED,
                             font=self.f_small)

        self.create_text(cx, cy + 44, text="#07", fill=ACCENT,
                         font=self.f_small)
        if flagged:
            self.create_rectangle(cx - 56, cy - 50, cx + 56, cy + 40,
                                  outline=RED, width=2, dash=(4, 3))


# ================================================================
# 风险分数轨迹图
# ================================================================
class TrajectoryChart(tk.Canvas):
    """风险分数的连续轨迹：瞬时值、平滑值、阈值线、抑制点、告警区。"""

    def __init__(self, master, font_small, **kw):
        super().__init__(master, width=680, height=130, bg=PANEL,
                         highlightthickness=0, **kw)
        self.f_small = font_small
        self.bind("<Configure>", lambda e: self._redraw())

    def set_data(self, history, cfg):
        self.history = history
        self.cfg = cfg
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width() or 700
        h = self.winfo_height() or 130
        ml, mr, mt, mb = 42, 12, 28, 22     # mt 留出顶部图例的位置
        pw, ph = w - ml - mr, h - mt - mb
        cfg = getattr(self, "cfg", None)
        hist = list(getattr(self, "history", []))
        if cfg is None or not pw > 20 or not ph > 20:
            return

        # —— 坐标网格 ——
        for v in range(0, 101, 20):
            y = mt + ph - ph * v / 100.0
            self.create_line(ml, y, ml + pw, y, fill=GRID)
            self.create_text(ml - 6, y, anchor="e", text=str(v),
                             fill=INK2, font=self.f_small)
        self.create_text(ml + pw, mt - 14, anchor="e",
                         text="风险分", fill=INK2, font=self.f_small)

        # —— 阈值线 ——
        ty = mt + ph - ph * cfg.risk_threshold / 100.0
        self.create_line(ml, ty, ml + pw, ty, fill=RED, dash=(5, 3))
        thr_txt = "触发阈值 {:.0f}".format(cfg.risk_threshold)
        tb = self.bbox(self.create_text(ml + pw - 6, ty - 9, anchor="e",
                                        text=thr_txt, fill=RED,
                                        font=self.f_small))
        if tb:                              # 给阈值标签加白底，压住曲线
            self.create_rectangle(tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1,
                                  outline="", fill=PANEL)
            self.create_text(ml + pw - 6, ty - 9, anchor="e", text=thr_txt,
                             fill=RED, font=self.f_small)

        if not hist:
            self.create_text(ml + pw / 2, mt + ph / 2,
                             text="等待数据…", fill=INK2, font=self.f_small)
            return

        data = hist[-TRAJ_LEN:]
        n = len(data)
        step = pw / max(1, TRAJ_LEN - 1)
        color = ACCENT                     # 单点时也要有颜色，故先给默认值

        def px(i):
            return ml + i * step

        def py(v):
            return mt + ph - ph * max(0.0, min(100.0, v)) / 100.0

        # —— 告警区间底色 ——
        i = 0
        while i < n:
            if data[i].triggered:
                j = i
                while j + 1 < n and data[j + 1].triggered:
                    j += 1
                self.create_rectangle(px(i), mt, px(j) + step, mt + ph,
                                      outline="", fill="#FDECEA")
                i = j + 1
            else:
                i += 1

        # —— 瞬时风险（细线）——
        if n >= 2:
            pts = []
            for i, r in enumerate(data):
                pts += [px(i), py(r.instant_risk)]
            self.create_line(*pts, fill="#9DB4D6", width=1)

        # —— 平滑风险（主轨迹 + 填充）——
        if n >= 2:
            area = [px(0), mt + ph]
            line = []
            for i, r in enumerate(data):
                line += [px(i), py(r.risk)]
            area += line + [px(n - 1), mt + ph]
            self.create_polygon(*area, fill="#E4ECF8", outline="")
            color = RED if data[-1].risk >= cfg.risk_threshold else ACCENT
            self.create_line(*line, fill=color, width=2)
        else:
            color = RED if data[-1].risk >= cfg.risk_threshold else ACCENT

        # —— 抑制点（帧级判据会报警但被时间维度抑制）——
        for i, r in enumerate(data):
            if r.suppressed and not r.triggered:
                self.create_line(px(i), mt + ph - 4, px(i), mt + ph,
                                 fill=ORANGE, width=2)

        # —— 当前点 ——
        cur = data[-1]
        self.create_oval(px(n - 1) - 3, py(cur.risk) - 3,
                         px(n - 1) + 3, py(cur.risk) + 3,
                         fill=color, outline="")

        # —— 时间轴 ——
        self.create_line(ml, mt + ph, ml + pw, mt + ph, fill=BORDER)
        self.create_text(ml, h - 6, anchor="w",
                         text="{:.1f}s".format(data[0].timestamp),
                         fill=INK2, font=self.f_small)
        self.create_text(ml + pw, h - 6, anchor="e",
                         text="{:.1f}s".format(cur.timestamp),
                         fill=INK2, font=self.f_small)
        # 图例（画在绘图区上方，避免压住曲线）
        ly = mt - 16
        self.create_line(ml + 4, ly, ml + 24, ly, fill="#9DB4D6")
        self.create_text(ml + 28, ly, anchor="w", text="瞬时风险",
                         fill=INK2, font=self.f_small)
        self.create_line(ml + 92, ly, ml + 112, ly, fill=ACCENT, width=2)
        self.create_text(ml + 116, ly, anchor="w", text="平滑风险（用于判定）",
                         fill=INK2, font=self.f_small)
        self.create_line(ml + 268, ly - 4, ml + 268, ly + 4, fill=ORANGE,
                         width=2)
        self.create_text(ml + 274, ly, anchor="w", text="瞬时已抑制",
                         fill=INK2, font=self.f_small)


# ================================================================
# 信号贡献分解
# ================================================================
class ContributionView(tk.Canvas):
    """六个信号的加权贡献、强度与持续占比。

    每行高度 30px：上方为标签+轨道+贡献分，下方一行小字为
    强度 / 持续占比 / 基础权重（对应"可解释输出"的三个要素）。
    """

    ROW_H = 28

    def __init__(self, master, font_small, **kw):
        super().__init__(master, width=680,
                         height=self.ROW_H * len(SIGNAL_KEYS) + 8,
                         bg=PANEL, highlightthickness=0, **kw)
        self.f_small = font_small
        self.bind("<Configure>", lambda e: self._redraw())

    def set_data(self, result, weights):
        self.result = result
        self.weights = weights
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width() or 700
        r = getattr(self, "result", None)
        if r is None:
            self.create_text(8, 14, anchor="w", text="等待数据…",
                             fill=INK2, font=self.f_small)
            return
        contrib = r.contributions or {}
        inten = r.intensities.as_dict() if r.intensities else {}
        pers = r.persistence or {}
        base = self.weights.as_dict()

        label_w, val_w = 122, 70
        bar_x = label_w + 6
        bar_w = max(60, w - bar_x - val_w - 8)

        for i, key in enumerate(SIGNAL_KEYS):
            y = 4 + i * self.ROW_H
            cy = y + 9                       # 轨道垂直中心
            direction = WEIGHT_DIRECTION[key]
            color = WEIGHT_COLOR[direction]

            self.create_text(4, cy, anchor="w",
                             text="{} {}".format(CN_NAME[key], direction),
                             fill=INK, font=self.f_small)
            self.create_rectangle(bar_x, cy - 6, bar_x + bar_w, cy + 6,
                                  outline="", fill="#F0F3F8")
            v = max(0.0, min(100.0, contrib.get(key, 0.0)))
            bw = bar_w * v / 100.0
            if bw > 0:
                self.create_rectangle(bar_x, cy - 6, bar_x + bw, cy + 6,
                                      outline="", fill=color)
            self.create_text(bar_x + bar_w + 6, cy, anchor="w",
                             text="{:5.1f}分".format(contrib.get(key, 0.0)),
                             fill=color if v > 0.5 else INK2, font=self.f_small)
            # 小字放在轨道**下方**独立的一行，避免与彩条重叠
            self.create_text(bar_x + 2, y + 16, anchor="nw",
                             text="强度 {:.2f}   持续占比 {:.2f}   "
                                  "基础权重 {:.2f}".format(
                                      inten.get(key, 0.0), pers.get(key, 0.0),
                                      base.get(key, 0.0)),
                             fill="#8A93A3", font=self.f_small)


# ================================================================
# 主界面
# ================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("智能考场 · 考生作弊智能识别（原型）")
        self.geometry("1280x840+20+8")
        self.minsize(1100, 700)
        # 上限取屏幕可用高度，避免窗口超出桌面导致底部被裁掉
        self.maxsize(self.winfo_screenwidth(), self.winfo_screenheight() - 56)
        self.configure(bg=BG)

        self.f_title = pick_font(self, ["Microsoft YaHei UI", "Microsoft YaHei",
                                        "微软雅黑", "SimHei"], 13, "bold")
        self.f_ui = pick_font(self, ["Microsoft YaHei UI", "Microsoft YaHei",
                                     "微软雅黑", "SimHei"], 10)
        self.f_ui_b = pick_font(self, ["Microsoft YaHei UI", "Microsoft YaHei",
                                       "微软雅黑", "SimHei"], 10, "bold")
        self.f_small = pick_font(self, ["Microsoft YaHei UI", "Microsoft YaHei",
                                        "微软雅黑", "SimHei"], 9)
        self.f_big = pick_font(self, ["Microsoft YaHei UI", "Microsoft YaHei",
                                      "微软雅黑", "SimHei"], 32, "bold")

        self.weights = SignalWeights()
        self.spec = SCENARIOS[0]
        self.history = deque(maxlen=TRAJ_LEN)
        self.playing = False
        self._after_id = None
        self._cur_row = None         # 事件表中"进行中"那一行的 id
        self.speed_ms = 100          # 每帧间隔（毫秒）

        # —— 摄像头/视频接入状态 ——
        self.worker = None           # 后台采集线程
        self.perceiver = None        # 感知层（首次连接时构造）
        self.cam_detector = None     # 摄像头模式下的引擎实例
        self.cam_queue = None
        self.cam_stop = None
        self.cam_info = None         # 本帧感知说明（TargetInfo）
        self.cam_frames = 0
        self.cam_start = 0.0

        # 参数变量必须先于 _build_cfg 建立
        self.var_thr = tk.DoubleVar(value=60.0)
        self.var_sus = tk.DoubleVar(value=1.0)
        self.var_win = tk.DoubleVar(value=2.0)
        self.cfg = self._build_cfg()
        self.player = ScenarioPlayer(self.cfg)

        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self._load_scenario(SCENARIOS[0])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 统一访问器 ----------------
    # 合成场景与摄像头两条链路共用同一套面板刷新逻辑，避免出现两套代码。
    @property
    def live(self) -> bool:
        """当前是否为摄像头/视频实时模式。"""
        return self.cam_detector is not None

    @property
    def engine(self):
        """当前生效的作弊识别引擎。"""
        if self.live:
            return self.cam_detector
        return self.player.det

    @property
    def frame_count(self) -> int:
        return self.cam_frames if self.live else self.player.idx

    # ---------------- 配置 ----------------
    def _build_cfg(self):
        """按界面参数构造引擎配置（参数变更后重建引擎）。"""
        return Config(fps=FPS,
                      risk_threshold=float(self.var_thr.get()),
                      min_sustain_seconds=float(self.var_sus.get()),
                      window_seconds=float(self.var_win.get()))

    # ---------------- 工具栏 ----------------
    def _build_toolbar(self):
        # —— 第一行：数据源 + 播放控制 ——
        bar = tk.Frame(self, bg=PANEL, height=50)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", side="top")

        tk.Label(bar, text="智能考场 · 考生作弊智能识别", bg=PANEL, fg=ACCENT,
                 font=self.f_title).pack(side="left", padx=(12, 14))

        # —— 视频源选择 ——
        self.var_src = tk.StringVar(value="合成场景")
        ttk.Combobox(bar, textvariable=self.var_src, width=9,
                     state="readonly", font=self.f_ui,
                     values=("合成场景", "摄像头", "视频文件", "网络流")
                     ).pack(side="left", padx=(0, 4))
        self.var_src.trace_add("write", lambda *_: self._on_src_kind())

        self.var_target = tk.StringVar(value="0")
        # 模式相关控件放在同一"插槽"里切换：合成场景→场景下拉；摄像头/文件→地址输入
        self.src_slot = tk.Frame(bar, bg=PANEL)
        self.src_slot.pack(side="left", padx=(0, 4))

        self.var_scn = tk.StringVar(value=SCENARIOS[0].label)
        self.cmb = ttk.Combobox(self.src_slot, textvariable=self.var_scn,
                                width=22, state="readonly", font=self.f_ui,
                                values=[s.label for s in SCENARIOS])
        self.cmb.bind("<<ComboboxSelected>>", self._on_scenario)

        self.ent_target = ttk.Entry(self.src_slot, textvariable=self.var_target,
                                    width=20, font=self.f_ui)

        self.btn_conn = tk.Button(bar, text="连接", width=6, font=self.f_ui,
                                  bg="#E7EBF3", relief="flat",
                                  command=self.toggle_connection)
        self.btn_conn.pack(side="left", padx=(0, 10))

        self.btn_play = tk.Button(bar, text="播放", width=7, font=self.f_ui,
                                  bg=ACCENT, fg="white", relief="flat",
                                  activebackground="#2A4A80",
                                  activeforeground="white",
                                  command=self.toggle_play)
        self.btn_play.pack(side="left", padx=3)
        tk.Button(bar, text="单帧", width=6, font=self.f_ui, relief="flat",
                  bg="#E7EBF3", command=self.step_once).pack(side="left", padx=3)
        tk.Button(bar, text="重置", width=6, font=self.f_ui, relief="flat",
                  bg="#E7EBF3", command=self.reset).pack(side="left", padx=3)

        tk.Label(bar, text="速度", bg=PANEL, fg=INK2,
                 font=self.f_ui).pack(side="left", padx=(12, 4))
        self.var_speed = tk.IntVar(value=100)
        ttk.Scale(bar, from_=200, to=30, variable=self.var_speed, length=80,
                  command=self._on_speed).pack(side="left")

        # —— 第二行：参数 + 源状态 ——
        bar2 = tk.Frame(self, bg="#F7F9FC", height=30)
        bar2.pack(fill="x", side="top")
        bar2.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", side="top")

        pf = tk.Frame(bar2, bg="#F7F9FC")
        pf.pack(side="left", padx=(12, 0))
        tk.Label(pf, text="参数：", bg="#F7F9FC", fg=INK2,
                 font=self.f_small).pack(side="left")
        for text, var, lo, hi, inc in (
                ("阈值", self.var_thr, 30.0, 95.0, 5.0),
                ("最短持续(s)", self.var_sus, 0.2, 3.0, 0.1),
                ("窗口(s)", self.var_win, 0.5, 4.0, 0.5)):
            tk.Label(pf, text=text, bg="#F7F9FC", fg=INK2,
                     font=self.f_small).pack(side="left", padx=(8, 3))
            ttk.Spinbox(pf, from_=lo, to=hi, increment=inc, width=5,
                        textvariable=var, font=self.f_small,
                        command=self._on_params).pack(side="left")

        self.lbl_src = tk.Label(bar2, text="", bg="#F7F9FC", fg=INK2,
                                font=self.f_small, anchor="w")
        self.lbl_src.pack(side="left", padx=(18, 6))

        self._on_src_kind(initial=True)

    # ---------------- 视频源 ----------------
    def _src_kind(self) -> str:
        """当前选择的数据源类型。"""
        label = self.var_src.get()
        return {"合成场景": KIND_SYNTHETIC, "摄像头": KIND_CAMERA,
                "视频文件": KIND_FILE, "网络流": KIND_STREAM}.get(
                    label, KIND_SYNTHETIC)

    def _on_src_kind(self, initial: bool = False):
        """切换数据源类型：换掉插槽控件、断开旧连接、刷新提示。"""
        kind = self._src_kind()
        self.cmb.pack_forget()
        self.ent_target.pack_forget()
        if kind == KIND_SYNTHETIC:
            self.cmb.pack(side="left")
            self.btn_conn.configure(state="disabled", text="连接")
            tip = "合成场景：内置 14 个场景，全部信号可用"
        else:
            self.ent_target.pack(side="left")
            self.btn_conn.configure(state="normal")
            if kind == KIND_CAMERA:
                cams = list_cameras() if HAS_OPENCV else []
                self.var_target.set(str(cams[0]) if cams else "0")
                tip = ("摄像头：填入序号后点“连接”。"
                       + ("检测到：" + ",".join(str(c) for c in cams)
                          if cams else "未探测到可用摄像头"))
            elif kind == KIND_FILE:
                tip = "视频文件：填入完整路径后点“连接”"
            else:
                tip = "网络流：填入 rtsp:// 或 http:// 地址后点“连接”"
        if not HAS_OPENCV and kind != KIND_SYNTHETIC:
            tip = "未安装 OpenCV，无法接入摄像头/视频，请先 pip install opencv-python"
        self.lbl_src.configure(text=tip)
        if not initial:
            self._teardown_live()
            self._show_panel_for(kind)

    def toggle_connection(self):
        if self.worker is not None:
            self._teardown_live()
            self._set_status("已断开")
            return
        self._connect()

    def _connect(self):
        kind = self._src_kind()
        if kind == KIND_SYNTHETIC:
            return
        if not HAS_OPENCV:
            messagebox.showerror("缺少依赖",
                                 "未安装 OpenCV，无法接入摄像头/视频。\n\n"
                                 "请执行：pip install opencv-python")
            return
        raw = self.var_target.get().strip()
        if not raw:
            messagebox.showwarning("参数缺失", "请先填写摄像头序号或视频地址")
            return
        target = int(raw) if (kind == KIND_CAMERA and raw.isdigit()) else raw

        self.pause()
        try:
            source = make_source(kind, target, 640, 480, int(FPS))
        except Exception as exc:
            messagebox.showerror("无法创建视频源", str(exc))
            return
        if not source.open():
            note = getattr(source, "_open_error", "") or ""
            messagebox.showerror(
                "打开失败",
                "无法打开 {}。\n{}\n\n常见原因：\n"
                "· 摄像头被其他程序（含本程序另一路）占用\n"
                "· 序号不对（试 0 / 1）\n"
                "· 文件路径不存在或格式不受支持".format(
                    source.describe(), note))
            return

        # 感知层：只在首次连接时构造（加载模型较慢）
        if self.perceiver is None:
            try:
                self.perceiver = FramePerceiver(PerceptionConfig())
            except Exception as exc:
                source.release()
                messagebox.showerror("感知层初始化失败", str(exc))
                return

        self.cfg = self._build_cfg()
        self.perceiver.reset()
        self.cam_detector = CheatDetector(self.cfg)
        self.history.clear()
        self.tree.delete(*self.tree.get_children())
        self._cur_row = None
        self.cam_frames = 0
        self.cam_start = time.time()
        self.cam_queue = queue.Queue(maxsize=3)
        self.cam_stop = threading.Event()
        self.worker = CaptureWorker(source, self.perceiver, FPS,
                                    self.cam_queue, self.cam_stop)
        self.worker.start()
        self.btn_conn.configure(text="断开")
        self._show_panel_for(kind)
        self._set_status("已连接 {}".format(source.describe()))
        self._poll_camera()

    def _teardown_live(self):
        """停止采集线程、释放视频源、清空实时引擎。

        清空 cam_detector 很重要：它同时也是"是否处于实时模式"的判据，
        否则断开后界面仍会走实时分支，读到空的引擎结果。
        """
        if self.cam_stop is not None:
            self.cam_stop.set()
        if self.worker is not None:
            self.worker.join(timeout=1.5)
            self.worker = None
        self.cam_queue = None
        self.cam_stop = None
        self.cam_detector = None
        self.cam_info = None
        self.btn_conn.configure(text="连接")

    def _poll_camera(self):
        """主线程轮询采集队列：推引擎 → 刷新面板 + 显示画面。"""
        if self.cam_queue is None:
            return
        latest = None
        try:
            while True:
                latest = self.cam_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            frame, feats, info = latest
            result = self.cam_detector.push(feats) if feats is not None else None
            if result is not None:
                self.cam_frames += 1
                self.history.append(result)
                self._track_event(result)
            self.cam_info = info
            # 叠加层用当前 result 上色，再显示
            try:
                import cv2
                self.perceiver.draw_overlay(frame, info, result)
                self.video.show(frame)
            except Exception:
                pass
            self._refresh(result, camera=True)
        if self.worker is not None and self.worker.error:
            err = self.worker.error
            self._teardown_live()
            self._set_status("采集已停止：{}".format(err))
            messagebox.showwarning("采集已停止", err)
            return
        if self.worker is not None:
            self.after(25, self._poll_camera)

    def _show_panel_for(self, kind):
        """合成场景显示考场示意图；摄像头显示实时画面。"""
        if kind == KIND_SYNTHETIC:
            self.video.pack_forget()
            self.room.pack()
            self.room.render(None, None)
            self.lbl_left_title.configure(text="考场实时画面（俯视示意）")
        else:
            self.room.pack_forget()
            self.video.pack()
            self.video.clear("正在连接…")
            self.lbl_left_title.configure(
                text="实时画面（摄像头）｜检测框=目标考生，人脸框=朝向估计依据")

    def _set_status(self, text):
        self.lbl_src.configure(text=text)

    # ---------------- 主体 ----------------
    def _build_body(self):
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # —— 左列 ——
        left = tk.Frame(body, bg=BG, width=ROOM_W + 24)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        left.grid_propagate(False)
        left.rowconfigure(5, weight=1)       # 读数框所在行可伸缩

        self.lbl_left_title = self._panel_title(
            left, "考场实时画面（俯视示意）｜#07 为分析对象、"
                  "右侧 #08 为邻座、虚线表示跨考生手部接近",
            row=0, col=0)
        card = self._card(left, row=1, col=0)
        self.room = RoomView(card, self.f_ui, self.f_small)
        self.video = VideoView(card, ROOM_W, ROOM_H, self.f_ui, self.f_small)
        self.room.pack()

        self._panel_title(left, "当帧原始读数（引擎输入）", row=4, col=0)
        card2 = self._card(left, row=5, col=0, sticky="nsew")
        self.txt_read = tk.Text(card2, height=8, width=50, font=self.f_small,
                                bg="#FBFCFE", fg=INK, relief="flat",
                                highlightthickness=0, wrap="none")
        self.txt_read.pack(fill="both", expand=True, padx=8, pady=6)
        self.txt_read.configure(state="disabled")

        # —— 右列 ——
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)      # 轨迹图所在行可伸缩

        # 风险分数 + 时间维度统计
        top = tk.Frame(right, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=0)
        top.columnconfigure(1, weight=1)

        self._panel_title(top, "风险分数", row=0, col=0)
        score_card = self._card(top, row=1, col=0, sticky="nsw")
        inner = tk.Frame(score_card, bg=PANEL)
        inner.pack(padx=16, pady=6)
        self.lbl_score = tk.Label(inner, text="0.0", bg=PANEL, fg=INK,
                                  font=self.f_big)
        self.lbl_score.pack()
        self.lbl_level = tk.Label(inner, text="正常", bg=PANEL, fg=OK,
                                  font=self.f_ui_b)
        self.lbl_level.pack(pady=(0, 4))
        self.lbl_thr = tk.Label(inner, text="阈值 60.0", bg=PANEL, fg=INK2,
                                font=self.f_small)
        self.lbl_thr.pack()

        self._panel_title(top, "窗口时间维度统计", row=0, col=1)
        stat_card = self._card(top, row=1, col=1, sticky="nsew")
        self.txt_stat = tk.Text(stat_card, height=6, width=52, font=self.f_small,
                                bg="#FBFCFE", fg=INK, relief="flat",
                                highlightthickness=0, wrap="none")
        self.txt_stat.pack(fill="both", expand=True, padx=8, pady=6)
        self.txt_stat.configure(state="disabled")

        # 轨迹图
        self._panel_title(right, "风险分数连续轨迹", row=2, col=0)
        traj_card = self._card(right, row=3, col=0, sticky="nsew")
        self.traj = TrajectoryChart(traj_card, self.f_small)
        self.traj.pack(fill="both", expand=True, padx=6, pady=6)

        # 贡献分解
        self._panel_title(right, "信号贡献分解（加权风险评分的可解释输出）",
                          row=4, col=0)
        con_card = self._card(right, row=5, col=0, sticky="ew")
        self.contrib = ContributionView(con_card, self.f_small)
        self.contrib.pack(fill="x", padx=6, pady=6)

    # ---------------- 底部 ----------------
    def _build_footer(self):
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="both", padx=10, pady=(0, 8))
        foot.columnconfigure(0, weight=1)
        foot.rowconfigure(2, weight=1)       # 事件表所在行可伸缩

        pat = tk.Frame(foot, bg=BG)
        pat.grid(row=0, column=0, sticky="ew")
        pat.columnconfigure(0, weight=0)
        pat.columnconfigure(1, weight=1)
        self.lbl_pattern = tk.Label(pat, text="行为模式：未见可疑模式", bg=BG,
                                    fg=INK, font=self.f_ui_b, anchor="w")
        self.lbl_pattern.grid(row=0, column=0, sticky="w")
        # 对比信息放在同一行右侧，省一行高度
        self.lbl_compare = tk.Label(pat, text="", bg=BG, fg=INK2,
                                    font=self.f_small, anchor="e")
        self.lbl_compare.grid(row=0, column=1, sticky="e")
        self.lbl_reason = tk.Label(pat, text="", bg=BG, fg=INK2,
                                   font=self.f_small, anchor="w",
                                   justify="left", wraplength=1240)
        self.lbl_reason.grid(row=1, column=0, columnspan=2, sticky="w")

        self._panel_title(foot, "告警事件列表", row=1, col=0)
        card = self._card(foot, row=2, col=0, sticky="nsew")
        cols = ("no", "onset", "confirm", "dur", "peak", "level", "pattern")
        heads = ("#", "行为起点", "确认触发", "持续(s)", "峰值", "级别",
                 "行为模式")
        widths = (40, 90, 90, 70, 60, 90, 300)
        self.tree = ttk.Treeview(card, columns=cols, show="headings", height=2)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)

        tk.Label(self, text="【注意】数据来源：本界面驱动的是合成场景，"
                            "所有数值为算法逻辑验证结果，不是真实考场实测数据。",
                 bg=BG, fg=WARN, font=self.f_small,
                 anchor="w").pack(fill="x", padx=12, pady=(0, 6))

    # ---------------- 小组件 ----------------
    def _card(self, parent, row=0, col=0, sticky="ew"):
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=row, column=col, sticky=sticky, pady=(0, 8))
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return inner

    def _panel_title(self, parent, text, row=0, col=0):
        lbl = tk.Label(parent, text=text, bg=BG, fg=INK2, font=self.f_small,
                       anchor="w")
        lbl.grid(row=row, column=col, sticky="w", padx=2, pady=(2, 2))
        return lbl

    # ---------------- 交互 ----------------
    def _on_scenario(self, _evt=None):
        label = self.var_scn.get()
        spec = next((s for s in SCENARIOS if s.label == label), SCENARIOS[0])
        self._load_scenario(spec)

    def _load_scenario(self, spec):
        """载入合成场景。若当前连着摄像头，先断开，避免两条链路同时跑。"""
        if self.live or self.worker is not None:
            self._teardown_live()
            self.cam_detector = None
            self._show_panel_for(KIND_SYNTHETIC)
        self.pause()
        self.spec = spec
        self.cfg = self._build_cfg()
        self.player.load(spec, self.cfg)
        self.history.clear()
        self.tree.delete(*self.tree.get_children())
        self._cur_row = None
        self._refresh(None)

    def _on_params(self):
        """参数变更：重建引擎并重放（合成场景）或重置（摄像头）。"""
        if self.live:
            self.cfg = self._build_cfg()
            self.cam_detector = CheatDetector(self.cfg)
            self.history.clear()
            self.tree.delete(*self.tree.get_children())
            self._cur_row = None
            if self.perceiver is not None:
                self.perceiver.reset()
            self._set_status("参数已更新（窗口 {:.1f}s / 阈值 {:.0f}）".format(
                self.cfg.window_seconds, self.cfg.risk_threshold))
            return
        self._load_scenario(self.spec)

        # 参数变更后重建引擎并重放，直观展示参数对判定的影响
        self._load_scenario(self.spec)

    def _on_speed(self, _v=None):
        self.speed_ms = int(self.var_speed.get())
        if self.playing:
            self._schedule()

    def toggle_play(self):
        if self.live:
            self._set_status("摄像头模式为实时处理，无需播放；请用“连接/断开”控制")
            return
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.player.finished:
            self._load_scenario(self.spec)
        self.playing = True
        self.btn_play.configure(text="暂停")
        self._schedule()

    def pause(self):
        self.playing = False
        self.btn_play.configure(text="播放")
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(max(20, self.speed_ms), self._tick)

    def _tick(self):
        self._after_id = None
        if not self.playing:
            return
        result = self.player.step()
        if result is None:
            self.pause()
            return
        self.history.append(result)
        self._track_event(result)
        self._refresh(result)
        self._schedule()

    def _track_event(self, result):
        """维护告警事件表：触发时插入行，解除时回填真实持续时长。

        事件在触发瞬间尚未结束，此时 duration 无意义，故先显示“进行中”，
        待风险回落后再回填 —— 避免表格里一直显示 0.0s。
        """
        if result.just_triggered:
            ev = self.engine.events[-1]
            self._cur_row = self.tree.insert("", "end", values=(
                len(self.engine.events),
                "{:.1f}s".format(ev.onset_time),
                "{:.1f}s".format(ev.confirm_time or 0.0),
                "进行中", "{:.1f}".format(ev.peak_risk),
                ev.level, ev.peak_pattern))
            self.tree.see(self._cur_row)
        elif result.released and self._cur_row is not None:
            ev = self.engine.events[-1]
            self.tree.set(self._cur_row, "dur", "{:.1f}".format(ev.duration))
            self._cur_row = None

    def step_once(self):
        self.pause()
        result = self.player.step()
        if result is None:
            return
        self.history.append(result)
        self._track_event(result)
        self._refresh(result)

    def reset(self):
        self._load_scenario(self.spec)

    # ---------------- 刷新 ----------------
    def _refresh(self, result, camera: bool = False):
        frame = None if camera else self.player.current_frame()
        if not camera:
            self.room.render(frame, result, self.spec.label)
        self.traj.set_data(list(self.history), self.cfg)
        self.contrib.set_data(result, self.weights)

        if result is None:
            self.lbl_score.configure(text="0.0", fg=INK)
            self.lbl_level.configure(text="正常", fg=OK)
            self.lbl_thr.configure(text="阈值 {:.0f}".format(
                self.cfg.risk_threshold))
            self.lbl_pattern.configure(text="行为模式：未见可疑模式", fg=INK)
            self.lbl_reason.configure(text="")
            self.lbl_compare.configure(text="")
            self._set_text(self.txt_read, "")
            self._set_text(self.txt_stat, "")
            return

        color = LEVEL_COLOR.get(result.level, INK)
        self.lbl_score.configure(text="{:.1f}".format(result.risk), fg=color)
        self.lbl_level.configure(text=result.level, fg=color)
        self.lbl_thr.configure(
            text="阈值 {:.0f}｜瞬时 {:.1f}".format(self.cfg.risk_threshold,
                                                   result.instant_risk))
        self.lbl_pattern.configure(
            text="行为模式：{}".format(result.pattern), fg=color)
        self.lbl_reason.configure(text="理由：{}".format(result.reason))

        # 帧级判据 vs 时空建模 的对比（体现"瞬时抑制"的价值）
        suppressed = sum(1 for r in self.history if r.suppressed)
        events = len(self.engine.events)
        self.lbl_compare.configure(
            text="已处理 {} 帧｜帧级判据命中且被时间维度抑制 {} 帧｜"
                 "时空建模触发告警 {} 次（当前参数：窗口 {:.1f}s / "
                 "最短持续 {:.1f}s）".format(
                     self.frame_count, suppressed, events,
                     self.cfg.window_seconds, self.cfg.min_sustain_seconds))

        self._refresh_readout(frame, result)
        self._refresh_stats(result)

    def _refresh_readout(self, frame, result):
        if self.live:
            self._refresh_readout_camera()
            return
        if frame is None:
            self._set_text(self.txt_read, "")
            return
        h, hd, p = frame.head, frame.hand, frame.posture
        objs = ", ".join(
            str(o[0]) if isinstance(o, (tuple, list)) else str(o)
            for o in frame.prohibited_objects) or "无"
        lines = [
            "帧号 {:>4}   时间 {:.2f}s".format(frame.frame_id, frame.timestamp),
            "-" * 44,
            "头部 yaw {:>6.1f}°  pitch {:>6.1f}°  朝向 {}".format(
                h.yaw_deg, h.pitch_deg, _REL_CN.get(h.relation, h.relation)),
            "手部 桌下 {:<3} 书写 {:<3} 跨考生距离 {}".format(
                "是" if hd.below_desk else "否", "是" if hd.doing_write else "否",
                "—" if hd.cross_distance is None
                else "{:.3f}".format(hd.cross_distance)),
            "坐姿 倾角 {:>6.1f}°  趴桌 {:<3} 离区 {:<3} 表情紧张 {}".format(
                p.tilt_deg, "是" if p.leaning_on_desk else "否",
                "是" if p.left_area else "否",
                "是" if frame.expression_tense else "否"),
            "违禁物体 {}".format(objs),
            "-" * 44,
            "帧级判据 {}".format("命中（待时间维度确认）" if result.suppressed
                                 else ("已在告警" if result.triggered else "未命中")),
        ]
        self._set_text(self.txt_read, "\n".join(lines))

    def _refresh_readout_camera(self):
        """摄像头模式：显示感知层给出的**估计值**与信号可用性对照。

        这里逐项标注哪些信号是实测/估计、哪些不可得，避免把缺失信号
        当成 0 分悄悄混进判断里。
        """
        info = self.cam_info
        per = self.perceiver
        head = ["帧号 {:>4}   已处理 {} 帧".format(
            self.frame_count, self.frame_count),
            "-" * 44]
        if self.worker is not None:
            rate = self.worker.fps_measured
            warn = ""
            if rate > 0 and rate < self.worker.fps * 0.6:
                warn = "  ← 低于标称 {:.0f}fps，时间窗口会被拉长，判定偏迟钝".format(
                    self.worker.fps)
            head.append("采集 {}  实测 {:.1f} fps（标称 {:.0f}）{}".format(
                "运行中" if self.worker.is_alive() else "已停止",
                rate, self.worker.fps, warn))
        if per is not None:
            head.append("检测链 {:<26}".format(per.backend_desc()))
        if info is None or not info.ok:
            head.append("目标 {}".format(
                info.reason if info else "等待首帧…"))
        else:
            head.append("目标框 {}".format(info.rect))
            head.append("人脸   {}{}   近20帧检出率 {:.2f}".format(
                info.face if info.face else "未检出",
                "  [{}]".format(info.face_kind) if info.face_kind else "",
                info.face_rate))
            head.append("yaw~{:>6.1f}°  pitch~{:>6.1f}°  朝向 {}{}".format(
                info.yaw_deg, info.pitch_deg,
                _REL_CN.get(info.relation, info.relation),
                "" if info.head_known else "（朝向不可判，不计头部信号）"))
            head.append("坐姿 tilt~{:>6.1f}°  趴桌 {:<3} 离区 {:<3}".format(
                info.tilt_deg, "是" if info.leaning else "否",
                "是" if info.left_area else "否"))
            base = info.baselines or {}
            head.append("基线  脸/身高{:.3f} 宽高比{:.3f} 质心{:.3f}".format(
                base.get("face_h_ratio", 0.0), base.get("aspect", 0.0),
                base.get("center_x", 0.0)))
        head.append("-" * 44)
        head.append("信号可用性（摄像头路径）")
        for key in SIGNAL_KEYS:
            st = CAMERA_SIGNAL_AVAIL.get(key, "unavailable")
            head.append("  {:<14} {}".format(
                CN_NAME[key], AVAIL_CN.get(st, st)))
        self._set_text(self.txt_read, "\n".join(head))

    def _refresh_stats(self, result):
        w = result.window
        if w is None:
            self._set_text(self.txt_stat, "")
            return
        lines = [
            "窗口 {} 帧 / {:.1f}s      超阈帧占比 {:.2f}".format(
                w.size, w.duration, w.over_threshold_ratio),
            "头部姿态方差 {:.4f} rad^2  (yaw {:.4f} + pitch {:.4f})".format(
                w.head_variance, w.head_var_yaw, w.head_var_pitch),
            "姿态往复 {:.2f} 次/秒      手部交互连续 {} 帧".format(
                w.repetition_rate, w.hand_streak),
            "违禁物体出现 {} 帧（占比 {:.2f}）".format(
                w.object_frames, w.object_ratio),
            "连续超阈 {} 帧 / 需 {} 帧      平滑系数 α={:.2f}".format(
                result.streak_frames, result.sustain_frames_needed,
                self.cfg.smoothing_alpha),
        ]
        self._set_text(self.txt_stat, "\n".join(lines))

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _on_close(self):
        self.pause()
        self._teardown_live()      # 释放摄像头，避免设备被本进程一直占用
        self.destroy()


_REL_CN = {
    "on_paper": "面向试卷/屏幕",
    "off_area": "偏离考试区域",
    "toward_neighbor": "朝向邻座",
    "toward_back": "朝向后方",
    "unknown": "未识别",
}


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
