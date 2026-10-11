# -*- coding: utf-8 -*-
"""
考场监控 · 逐人行为识别（演示级）

在 HOG+SVM 行人检测之上加了一层"逐人跟踪 + 行为分析"：
  · 每个人分配稳定编号 #1 #2 …（质心最近邻跟踪，丢帧容忍）
  · 每人每帧推断当前动作：坐下 / 起立 / 大幅度偏转 / 前倾趴桌 / 东张西望 / 长时间低头 …
  · 每人单独累计"触碰红线次数"（跨越警戒线一次算一次）
  · 框比原 HOG 输出收紧（只留 5% 边距），人多时不糊成一团

行为 -> 03 告警码映射见 gui.py / terminal_gui.py。
生产环境建议换成 YOLO-pose / 边缘 AI 节点返回的骨骼点，本文件是原型。
"""
import os
from collections import deque

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 行为常量（与 02 gui / 03 terminal 的映射保持一致）
ACT_NORMAL = "正常姿态"
ACT_SIT = "坐下"
ACT_STAND = "起立/站立"
ACT_TURN = "大幅度偏转"      # 侧转/回头：框宽高比突变
ACT_LEAN = "前倾/趴桌"
ACT_LOOK = "东张西望"        # 质心左右高频摆动
ACT_DOWN = "长时间低头"
ACT_LEFT = "靠左"
ACT_RIGHT = "靠右"
ACT_CROSS = "触碰红线"       # 每跨越一次警戒线计一次
ACT_GAZE = "眼神游移"        # 双眼位置不对称/视线漂移（人脸+人眼级联）
ACT_SLEEPY = "闭眼/趴睡"     # 检测到人脸但长时间看不到眼睛
ACT_HEADTURN = "转脸偏头"    # 人脸明显偏向人框一侧

# 需要上报 03 的异常行为
ABNORMAL_ACTIONS = (ACT_CROSS, ACT_STAND, ACT_TURN, ACT_LEAN, ACT_LOOK, ACT_DOWN,
                    ACT_GAZE, ACT_SLEEPY, ACT_HEADTURN)


def _load_font(size=16):
    """加载支持中文的字体，找不到则回退到默认字体"""
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simhei.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simsun.ttc"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _centroid(rect):
    x, y, w, h = rect
    return x + w // 2, y + h // 2


class _Track(object):
    """一个人的跟踪状态"""
    __slots__ = ("pid", "rect", "missed", "centroids", "heights",
                 "prev_side", "crosses", "cross_epochs",
                 "face_cache", "face_age")

    def __init__(self, pid, rect, line_y):
        self.pid = pid
        self.rect = rect
        self.missed = 0
        self.centroids = deque(maxlen=20)   # 最近 20 帧质心，用于东张西望判定
        self.heights = deque(maxlen=20)     # 最近 20 帧框高，用于低头判定
        self.prev_side = self._side(rect, line_y)
        self.crosses = 0                    # 累计跨线次数
        self.cross_epochs = deque(maxlen=10)
        self.face_cache = None              # 最近一次人脸/人眼分析结果
        self.face_age = 99                  # 距上次人脸分析的帧数

    @staticmethod
    def _side(rect, line_y):
        """框底中心在警戒线上方(-1)还是下方(+1)"""
        return 1 if rect[1] + rect[3] > line_y else -1


class PersonTracker(object):
    """HOG 检测 + 质心最近邻逐人跟踪 + Haar 人脸/人眼微表情分析"""

    def __init__(self, margin_ratio=0.05, max_dist=90, max_missed=12):
        if not hasattr(cv2, "HOGDescriptor"):
            raise RuntimeError(
                "当前 OpenCV 未包含 HOGDescriptor，无法运行 AI 框选识别。\n"
                "请运行「项目树\\All\\安装环境.bat」自动修复。")
        if not hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector"):
            raise RuntimeError("当前 OpenCV 缺少 HOG 默认行人检测器。")
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.margin_ratio = margin_ratio    # 收紧框：只留 5% 边距
        self.max_dist = max_dist            # 质心匹配最大距离（像素）
        self.max_missed = max_missed        # 连续丢失多少帧后删除轨迹
        self._tracks = {}                   # pid -> _Track
        self._next_pid = 1
        self._frame_no = 0
        # Haar 人脸/人眼级联（OpenCV 自带，无需下载模型）
        self._face_cascade = None
        self._eye_cascade = None
        try:
            base = cv2.data.haarcascades
            self._face_cascade = cv2.CascadeClassifier(
                base + "haarcascade_frontalface_default.xml")
            self._eye_cascade = cv2.CascadeClassifier(
                base + "haarcascade_eye.xml")
        except Exception:
            self._face_cascade = None
            self._eye_cascade = None

    # ---------------- 检测 ----------------
    def _detect(self, frame):
        """HOG 原始检测 -> 收紧后的框列表"""
        small = cv2.resize(frame, None, fx=0.5, fy=0.5)
        rects, _ = self.hog.detectMultiScale(small, winStride=(8, 8),
                                             padding=(12, 12), scale=1.05)
        fh, fw = frame.shape[:2]
        out = []
        for (x, y, w, h) in rects:
            x, y, w, h = x * 2, y * 2, w * 2, h * 2
            mw = int(w * self.margin_ratio)
            mh = int(h * self.margin_ratio)
            x = max(0, x - mw)
            y = max(0, y - mh)
            w = min(fw - x, w + mw * 2)
            h = min(fh - y, h + mh * 2)
            out.append((x, y, w, h))
        return out

    # ---------------- 跟踪 ----------------
    def update(self, frame, boundary_ratio=0.5):
        """每帧调用：检测 -> 匹配轨迹 -> 更新跨线计数，返回 tracks 列表"""
        self._frame_no += 1
        fh = frame.shape[0]
        line_y = int(fh * boundary_ratio)
        rects = self._detect(frame)
        cents = [_centroid(r) for r in rects]

        used = set()
        # 1) 已有轨迹逐个找最近检测框
        for pid, tr in list(self._tracks.items()):
            best, best_d = None, self.max_dist
            tcx, tcy = _centroid(tr.rect)
            for i, (cx, cy) in enumerate(cents):
                if i in used:
                    continue
                d = ((cx - tcx) ** 2 + (cy - tcy) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = i, d
            if best is not None:
                used.add(best)
                tr.rect = rects[best]
                tr.missed = 0
                cx, cy = cents[best]
                tr.centroids.append((cx, cy))
                tr.heights.append(tr.rect[3])
                tr.face_age += 1
                # 跨线计数：上->下 或 下->上 都算一次触碰
                side = tr._side(tr.rect, line_y)
                if side != tr.prev_side:
                    tr.crosses += 1
                    tr.cross_epochs.append(cv2.getTickCount())
                    tr.prev_side = side
            else:
                tr.missed += 1
                tr.face_age += 1
                if tr.missed > self.max_missed:
                    del self._tracks[pid]

        # 2) 未匹配的检测框开新轨迹
        for i, rect in enumerate(rects):
            if i in used:
                continue
            tr = _Track(self._next_pid, rect, line_y)
            self._tracks[self._next_pid] = tr
            self._next_pid += 1
        return self._tracks

    # ---------------- 人脸 / 眼神（微表情近似） ----------------
    def _face_analyze(self, frame, track):
        """
        在人框上半部分做人脸+人眼检测（每 4 帧一次，轨迹级缓存）。
        返回 {"face": (fx,fy,fw,fh)|None, "eyes": n, "face_dx": 偏头量}
        """
        if self._face_cascade is None or self._eye_cascade is None:
            return None
        if track.face_cache is not None and track.face_age < 4:
            return track.face_cache
        x, y, w, h = track.rect
        fh, fw = frame.shape[:2]
        head_h = max(24, int(h * 0.45))          # 头部区域：人框上 45%
        roi = frame[y:y + head_h, x:x + w]
        if roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(gray, 1.1, 4,
                                                    minSize=(24, 24))
        if len(faces) == 0:
            track.face_cache = {"face": None, "eyes": 0, "face_dx": 0.0}
        else:
            fx, fy, fw2, fh2 = max(faces, key=lambda r: r[2] * r[3])
            fgray = gray[fy:fy + fh2, fx:fx + fw2]
            eyes = self._eye_cascade.detectMultiScale(fgray, 1.1, 3,
                                                      minSize=(10, 10))
            face_cx = fx + fw2 // 2
            face_dx = (face_cx - w / 2.0) / max(1.0, float(w))   # 人脸偏离人框中心比例
            track.face_cache = {"face": (x + fx, y + fy, fw2, fh2),
                                "eyes": int(len(eyes)),
                                "face_dx": face_dx}
        track.face_age = 0
        return track.face_cache

    # ---------------- 行为分析 ----------------
    def analyze(self, frame, track, boundary_ratio=0.5, edge_margin=20):
        """
        对一条轨迹做行为推断。
        返回 (action, bad, behaviors)
          action    主标签（画在框上）
          bad       是否越界（红框）
          behaviors 需要上报的异常行为列表
        """
        fh, fw = frame.shape[:2]
        x, y, w, h = track.rect
        bottom = y + h
        line_y = int(fh * boundary_ratio)
        bad = bottom > line_y

        behaviors = []
        action = ACT_NORMAL

        # 1) 触碰红线：跨线次数 >= 2 视为"多次触线"
        if track.crosses >= 2:
            behaviors.append(ACT_CROSS)
        elif bad:
            action = "越界"

        # 2) 大幅度偏转：框宽高比 > 0.72（侧转/回头时人框明显变宽）
        if h > 0 and (w / float(h)) > 0.72:
            action = ACT_TURN
            behaviors.append(ACT_TURN)
        # 3) 起立/站立：框很高且底部越过中线
        elif h > fh * 0.55 and bottom > line_y:
            action = ACT_STAND
            behaviors.append(ACT_STAND)
        # 4) 前倾/趴桌：框偏宽
        elif h > 0 and (w / float(h)) > 0.58:
            action = ACT_LEAN
            behaviors.append(ACT_LEAN)
        # 5) 坐下：框较矮、在画面下半
        elif h < fh * 0.45 and bottom > line_y:
            action = ACT_SIT

        # 6) 东张西望：近 20 帧质心横向摆动大、纵向基本不动
        if len(track.centroids) >= 12:
            xs = np.array([c[0] for c in track.centroids], dtype=float)
            ys = np.array([c[1] for c in track.centroids], dtype=float)
            if xs.std() > max(18.0, w * 0.25) and ys.std() < max(14.0, h * 0.12):
                behaviors.append(ACT_LOOK)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_LOOK

        # 7) 长时间低头：框高持续变矮 + 质心持续下移
        if len(track.heights) >= 15:
            hs = list(track.heights)
            if hs[-1] < hs[0] * 0.82 and track.centroids[-1][1] > track.centroids[0][1] + 10:
                behaviors.append(ACT_DOWN)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_DOWN

        # 8) 人脸/眼神微表情（有级联时才跑，失败不影响其它判定）
        try:
            fa = self._face_analyze(frame, track)
        except Exception:
            fa = None
        if fa and fa.get("face") is not None:
            # 转脸偏头：人脸明显偏向人框一侧
            if abs(fa.get("face_dx", 0.0)) > 0.22:
                behaviors.append(ACT_HEADTURN)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_HEADTURN
            # 闭眼/趴睡：检测到人脸但看不到眼睛
            if fa.get("eyes", 0) == 0:
                behaviors.append(ACT_SLEEPY)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_SLEEPY
            # 眼神游移：只看到单眼（视线明显偏向一边）
            elif fa.get("eyes", 0) == 1:
                behaviors.append(ACT_GAZE)
                if action in (ACT_NORMAL, ACT_SIT):
                    action = ACT_GAZE

        # 9) 贴边
        if x < edge_margin or y < edge_margin \
                or x + w > fw - edge_margin or y + h > fh - edge_margin:
            bad = True
            if x < fw * 0.12:
                action = ACT_LEFT
            elif x + w > fw * 0.88:
                action = ACT_RIGHT

        # 去重保序
        seen, uniq = set(), []
        for b in behaviors:
            if b not in seen:
                seen.add(b)
                uniq.append(b)
        return action, bad, uniq

    # ---------------- 绘制 ----------------
    def draw(self, frame, boundary_ratio=0.5):
        """
        返回 (标记后的帧, 越界人数, 逐人信息列表)
        逐人信息: {"pid", "rect", "action", "bad", "crosses", "behaviors"}
        """
        fh, fw = frame.shape[:2]
        line_y = int(fh * boundary_ratio)
        tracks = self.update(frame, boundary_ratio=boundary_ratio)

        persons = []
        for pid, tr in sorted(tracks.items()):
            action, bad, behaviors = self.analyze(frame, tr,
                                                  boundary_ratio=boundary_ratio)
            persons.append({"pid": pid, "rect": tr.rect, "action": action,
                            "bad": bad, "crosses": tr.crosses,
                            "behaviors": behaviors})
            # 三色：红=越界/贴边，橙=异常行为未越界，绿=正常
            if bad:
                color = (0, 0, 255)
            elif behaviors:
                color = (0, 165, 255)
            else:
                color = (0, 200, 0)
            x, y, w, h = tr.rect
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 1)  # 细框=更小的视觉占用
            # 人脸小框（微表情辅助，青色）
            fa = getattr(tr, "face_cache", None)
            if fa and fa.get("face") is not None:
                fx, fy, fw2, fh2 = fa["face"]
                cv2.rectangle(frame, (fx, fy), (fx + fw2, fy + fh2),
                              (255, 255, 0), 1)

        cv2.line(frame, (0, line_y), (fw, line_y), (0, 165, 255), 1)

        # PIL 画中文标签
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil)
        font = _load_font(15)
        for p in persons:
            if p["bad"]:
                color = (0, 0, 255)
            elif p["behaviors"]:
                color = (0, 165, 255)
            else:
                color = (0, 200, 0)
            x, y, w, h = p["rect"]
            label = "#%d %s" % (p["pid"], p["action"])
            if p["crosses"] >= 2:
                label += " ×%d" % p["crosses"]
            fill = (color[2], color[1], color[0])
            ty = y - 20 if y > 24 else y + h + 2
            draw.text((x, ty), label, font=font, fill=fill)
        draw.text((10, line_y - 22), "警戒线", font=font, fill=(255, 165, 0))
        frame = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        violations = sum(1 for p in persons if p["bad"])
        return frame, violations, persons


# 兼容旧名：外部原来用 PersonDetector 的地方继续可用
class PersonDetector(PersonTracker):
    def detect(self, frame, **kw):
        return self._detect(frame)

    def is_out_of_bounds(self, frame, rect, boundary_ratio=0.5, edge_margin=20):
        fh, fw = frame.shape[:2]
        x, y, w, h = rect
        line_y = int(fh * boundary_ratio)
        if y + h > line_y:
            return True
        if x < edge_margin or y < edge_margin:
            return True
        if x + w > fw - edge_margin or y + h > fh - edge_margin:
            return True
        return False
