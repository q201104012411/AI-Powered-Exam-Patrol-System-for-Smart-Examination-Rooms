# -*- coding: utf-8 -*-
r"""
作弊识别结合层（把 `作弊识别\` 里的 cheatdetect 引擎接进本项目的监控链路）

它回答的问题和本项目原有 AI **不一样**，两者是叠加关系：

    本项目原 AI（person_detect / vlm_detect）
        → 看"这一帧里有什么"：几个人、什么姿态、有没有手机、越没越界
        → **帧级判断**，单帧动作就会出标签

    cheatdetect（结合层驱动）
        → 看"这段动作序列像不像在获取/传递答案"
        → 加权风险分数 + 滑动时间窗口（默认 2s）+ **持续超阈才告警**
        → 单帧瞬时动作被自动抑制（现场那种"坐着没动却报偏转"就属于瞬时误判）

用法（02/03 的监控循环里）::

    import cheat_bridge
    watch = cheat_bridge.CheatWatch(feed_hz=2.0)      # feed_hz = 你实际的送帧频率
    r = watch.push(frame_bgr)
    if r["ok"]:
        print(watch.status_text())                    # 作弊风险：正常 12 · 关注 48 …
    if r["just_triggered"]:
        print("作弊预警：", r["pattern_desc"], r["reason_text"])

如实说明（务必先看）：
  1. **信号可得性由引擎自己声明**（`CAMERA_SIGNAL_AVAIL`）。摄像头路径下：
     头部偏离=**估计**、头部方差=**实测**、姿态往复=**估计**；
     **手部交互 / 违禁物体 / 表情 = 不可得**（没有骨骼点与物体模型）。
     本层把这些原样透出（`signals` 字段），不假装有。
  2. 角度类数值是**按人脸偏移/框几何估计**的，需按机位标定，不是量测。
  3. `feed_hz` 必须和你真实的送帧频率一致：窗口 2s、最短持续 1s 都按帧数换算，
     送 2 帧/秒却填 10 会造成"永远攒不够持续帧 → 永不告警"。
  4. 引擎阈值/权重沿用 cheatdetect 默认（阈值 60、窗口 2s、最短持续 1s、平滑 0.5）。
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHEAT_DIR = HERE / "作弊识别"

# 把内置的作弊识别引擎加进导入路径（每套系统各带一份，互不依赖 Desktop 原目录）
if CHEAT_DIR.is_dir() and str(CHEAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHEAT_DIR))

try:
    from cheatdetect import CheatDetector, Config
    from cheatdetect.features import (FrameFeatures, HandState, HeadPose,
                                      PostureState)
    from cheatdetect.perception import (CAMERA_SIGNAL_AVAIL, AVAIL_CN,
                                        FramePerceiver, PerceptionConfig)
    _IMPORT_ERR = ""
except Exception as e:                      # pragma: no cover - 缺目录/cv2 时
    CheatDetector = Config = FramePerceiver = PerceptionConfig = None
    FrameFeatures = HandState = HeadPose = PostureState = None
    CAMERA_SIGNAL_AVAIL, AVAIL_CN = {}, {}
    _IMPORT_ERR = str(e)


def default_model_path():
    """本项目自带的 YOLO11n ONNX（与 person_detect 用同一个模型，不额外下载）"""
    p = HERE / "models" / "yolo11n.onnx"
    return str(p) if p.exists() else ""


class CheatWatch(object):
    """一路画面的作弊风险观察器（单分析对象，与 cheatdetect 语义一致）。"""

    def __init__(self, feed_hz=2.0, model_path=None, risk_threshold=None,
                 window_seconds=None, min_sustain_seconds=None,
                 target_index=-1, person_conf=None, perception=False):
        """perception=False（默认）：**复用本项目自己的检测结果**，不重复加载/运行 YOLO；
        perception=True：额外启用 cheatdetect 自带的 FramePerceiver（独立跑一遍 YOLO），
        适合单独验证引擎，或在没有本项目跟踪器的场景里用。"""
        self.ok = CheatDetector is not None
        self.import_error = _IMPORT_ERR
        self.feed_hz = float(feed_hz) if feed_hz and feed_hz > 0 else 2.0
        self.use_perception = bool(perception)
        self.cfg = None
        self.det = None
        self.perc = None
        self.alarms = []
        self.last = None
        self._t0 = time.time()
        self._frames = 0
        self._skipped = 0
        self.backend = ""
        # 几何量口径（与 cheatdetect PerceptionConfig 默认值一致）
        self.yaw_full_deg = 60.0
        self.tilt_full = 0.45
        self.lean_height_ratio = 0.72
        self.leave_center_ratio = 0.22
        if not self.ok:
            return
        kwargs = {"fps": self.feed_hz}
        if risk_threshold is not None:
            kwargs["risk_threshold"] = float(risk_threshold)
        if window_seconds is not None:
            kwargs["window_seconds"] = float(window_seconds)
        if min_sustain_seconds is not None:
            kwargs["min_sustain_seconds"] = float(min_sustain_seconds)
        self.cfg = Config(**kwargs)
        self.det = CheatDetector(self.cfg)
        if not self.use_perception:
            self.backend = "项目内检测（人框/人脸/行为/VLM 物体）"
            return
        pcfg = PerceptionConfig(model_path=(model_path if model_path is not None
                                            else default_model_path()),
                                target_index=int(target_index))
        self.yaw_full_deg = float(getattr(pcfg, "yaw_full_deg", 60.0))
        self.tilt_full = float(getattr(pcfg, "tilt_full", 0.45))
        self.lean_height_ratio = float(getattr(pcfg, "lean_height_ratio", 0.72))
        self.leave_center_ratio = float(getattr(pcfg, "leave_center_ratio", 0.22))
        if person_conf is not None:
            pcfg.person_conf = float(person_conf)
        try:
            self.perc = FramePerceiver(pcfg)
            self.backend = self.perc.backend_desc()
        except Exception as e:                     # pragma: no cover
            self.perc = None
            self.ok = False
            self.import_error = "感知层初始化失败：%s" % e

    # ------------------------------------------------------------------
    def describe(self):
        """一行配置摘要（日志/状态栏用）"""
        if not self.ok:
            return "作弊识别不可用：%s" % (self.import_error or "未知原因")
        return ("作弊识别：%s ｜ %s ｜ 送帧 %.1f 帧/秒"
                % (self.backend or "感知层", self.cfg.describe(), self.feed_hz))
    def signal_notes(self):
        """引擎自述的信号可得性（如实展示，不美化）"""
        out = []
        for k, v in (CAMERA_SIGNAL_AVAIL or {}).items():
            out.append("%s=%s" % (k, AVAIL_CN.get(v, v)))
        return "；".join(out)

    # ------------------------------------------------------------------
    def push(self, frame, timestamp=None):
        """喂一帧（走引擎自带感知）。未启用自带感知 / 未检出时 ok=False，并给出原因。"""
        if not self.ok or not self.use_perception or self.perc is None:
            return {"ok": False,
                    "reason": ("未启用引擎自带感知（默认复用本项目检测结果，"
                               "请用 push_persons）" if self.ok else
                               (self.import_error or "作弊识别不可用"))}
        if frame is None:
            return {"ok": False, "reason": "空帧"}
        if timestamp is None:
            timestamp = time.time() - self._t0
        self._frames += 1
        try:
            feats, info = self.perc.process(frame, frame_id=self._frames,
                                            timestamp=float(timestamp))
        except Exception as e:
            return {"ok": False, "reason": "感知异常：%s" % e}
        if feats is None:
            # 引擎自己的约定：没检出考生就**不要**喂假帧
            self._skipped += 1
            res = {"ok": False, "reason": info.reason or "未检出考生",
                   "rect": None, "signals": dict(CAMERA_SIGNAL_AVAIL or {})}
            self.last = res
            return res
        r = self.det.push(feats)
        out = {
            "ok": True,
            "frame_id": r.frame_id,
            "timestamp": r.timestamp,
            "risk": float(r.risk),
            "instant_risk": float(r.instant_risk),
            "threshold": float(r.threshold),
            "level": r.level,
            "pattern": r.pattern,
            "pattern_desc": r.pattern_desc,
            "reason_text": r.reason,
            "triggered": bool(r.triggered),
            "just_triggered": bool(r.just_triggered),
            "released": bool(r.released),
            "suppressed": bool(r.suppressed),
            "streak_frames": int(r.streak_frames),
            "sustain_frames_needed": int(r.sustain_frames_needed),
            "rect": info.rect,
            "face": info.face,
            "yaw_deg": round(float(info.yaw_deg), 1),
            "pitch_deg": round(float(info.pitch_deg), 1),
            "tilt_deg": round(float(info.tilt_deg), 1),
            "leaning": bool(info.leaning),
            "left_area": bool(info.left_area),
            "contributions": dict(r.contributions or {}),
            "signals": dict(CAMERA_SIGNAL_AVAIL or {}),
        }
        self.last = out
        if out["just_triggered"]:
            self.alarms.append({
                "timestamp": out["timestamp"],
                "level": out["level"],
                "pattern": out["pattern"],
                "pattern_desc": out["pattern_desc"],
                "risk": out["risk"],
                "reason_text": out["reason_text"],
                "rect": out["rect"],
            })
        return out

    # ------------------------------------------------------------------
    #  特征来源 A：用**本项目自己的检测结果**构造 FrameFeatures（推荐，不重复跑 YOLO）
    # ------------------------------------------------------------------
    def _pick_target_person(self, persons):
        """分析对象：优先"有异常行为/手机"的人，否则面积最大的那个人。

        cheatdetect 的引擎语义就是"盯住一个考生看时序"，所以这里也必须选一个目标；
        选错了目标等于分析错人（界面会显示是 #几号）。
        """
        if not persons:
            return None
        def key(p):
            r = p.get("rect") or (0, 0, 0, 0)
            sus = 1 if (p.get("behaviors") or p.get("bad") or p.get("phone")) else 0
            return (sus, r[2] * r[3])
        return max(persons, key=key)

    def _baseline(self):
        """本人基线（宽高比 / 框高 / 质心x），指数滑动，与引擎 perception 的取法一致"""
        if not hasattr(self, "_bl"):
            self._bl = {"aspect": None, "height": None, "cx": None, "n": 0}
        return self._bl

    def push_persons(self, persons, frame_shape=None, timestamp=None,
                     vlm_objects=None, doing_write=True):
        """用本项目的人框/人脸/行为/VLM 物体构造特征并推入引擎。

        映射口径（与 cheatdetect 自带 perception 对齐，便于对照）：
          · yaw      = 人脸中心相对人框中心的水平偏移 ÷ 半框宽 × yaw_full_deg(60°)
          · pitch    = 人脸中心在框内的相对高度；无脸时按行为兜（长时间低头≈45°）
          · tilt     = 框宽高比相对**本人基线**（EMA α=0.05）的偏差 → 0~45°
          · 趴桌     = 行为含"前倾/趴桌"，或框高 < 基线高 × 0.72
          · 离开台面 = 行为含"起立/站立"，或质心横向偏离基线 > 画面宽 × 0.22
          · 违禁物体 = VLM 判出的手机/纸条（**这是我们比摄像头路径多出来的信号**）
          · 手部交互 = 不可得（没有骨骼点），如实置 None
        """
        if not self.ok or self.det is None:
            return {"ok": False, "reason": self.import_error or "作弊识别不可用"}
        p = self._pick_target_person(persons)
        if p is None:
            self._skipped += 1
            res = {"ok": False, "reason": "本帧没有可用的人框", "pid": None,
                   "signals": dict(CAMERA_SIGNAL_AVAIL or {})}
            self.last = res
            return res
        if timestamp is None:
            timestamp = time.time() - self._t0
        x, y, w, h = [int(v) for v in (p.get("rect") or (0, 0, 0, 0))]
        w = max(1, w)
        h = max(1, h)
        fh = fw = 0
        if frame_shape:
            fh, fw = int(frame_shape[0]), int(frame_shape[1])
        beh = list(p.get("behaviors") or [])
        beh_text = " ".join(beh) + " " + str(p.get("action") or "")
        bl = self._baseline()
        aspect = w / float(h)
        cx_norm = ((x + w / 2.0) / float(fw)) if fw else None
        tilt = 0.0
        leaning = left = False
        if bl["aspect"] is not None and self.tilt_full > 0:
            dev = abs(aspect - bl["aspect"]) / float(self.tilt_full)
            tilt = min(1.0, dev) * 45.0
        if bl["height"] is not None and h < bl["height"] * self.lean_height_ratio:
            leaning = True
        if bl["cx"] is not None and cx_norm is not None and fw:
            if abs(cx_norm - bl["cx"]) > self.leave_center_ratio:
                left = True
        if ("前倾" in beh_text) or ("趴" in beh_text):
            leaning = True
        if ("起立" in beh_text) or ("站立" in beh_text) or ("离" in beh_text):
            left = True

        # 人脸 → yaw / pitch
        yaw = pitch = 0.0
        face = p.get("face")
        if face:
            fx, fy, fw_, fh_ = [float(v) for v in face]
            face_cx = fx + fw_ / 2.0
            yaw_ratio = ((face_cx - (x + w / 2.0)) / (w / 2.0))
            yaw = max(-60.0, min(60.0, yaw_ratio * 60.0))
            pitch = max(0.0, min(60.0, ((fy + fh_ / 2.0 - y) / float(h)) * 60.0))
        elif ("低头" in beh_text):
            pitch = 45.0

        # 违禁物体：优先用显式传入的（VLM 结构化结果），其次看人身上的 phone 标记
        objs = []
        if vlm_objects:
            for o in vlm_objects:
                label = str((o or {}).get("label") or "")
                if "手机" in label or "phone" in label.lower():
                    objs.append("phone")
                elif "纸条" in label or "note" in label.lower():
                    objs.append("note")
        if p.get("phone") and "phone" not in objs:
            objs.append("phone")

        yaw_thr = float(getattr(self.cfg, "head_yaw_threshold_deg", 30.0))
        if abs(yaw) >= yaw_thr:
            relation = "toward_neighbor" if yaw > 0 else "toward_back"
        elif yaw == 0.0 and pitch == 0.0:
            relation = "on_paper"
        else:
            relation = "off_area"

        feats = FrameFeatures(
            frame_id=self._frames + 1,
            timestamp=float(timestamp),
            head=HeadPose(yaw_deg=yaw, pitch_deg=pitch, relation=relation),
            hand=HandState(below_desk=False, cross_distance=None,
                           doing_write=bool(doing_write)),
            posture=PostureState(tilt_deg=tilt, leaning_on_desk=leaning,
                                 left_area=left),
            prohibited_objects=objs,
            expression_tense=False,
        )
        r = self.det.push(feats)
        self._frames += 1
        # 更新基线（只用"没被判异常"的帧，避免异常姿态把基线带跑）
        alpha = 0.05
        suspicious = bool(beh) or bool(objs) or p.get("bad")
        if not suspicious:
            bl["aspect"] = aspect if bl["aspect"] is None else \
                (1 - alpha) * bl["aspect"] + alpha * aspect
            bl["height"] = float(h) if bl["height"] is None else \
                (1 - alpha) * bl["height"] + alpha * h
            if cx_norm is not None:
                bl["cx"] = cx_norm if bl["cx"] is None else \
                    (1 - alpha) * bl["cx"] + alpha * cx_norm
        bl["n"] = bl.get("n", 0) + 1
        out = {
            "ok": True, "pid": p.get("pid"), "frame_id": r.frame_id,
            "timestamp": r.timestamp, "risk": float(r.risk),
            "instant_risk": float(r.instant_risk), "threshold": float(r.threshold),
            "level": r.level, "pattern": r.pattern, "pattern_desc": r.pattern_desc,
            "reason_text": r.reason, "triggered": bool(r.triggered),
            "just_triggered": bool(r.just_triggered), "released": bool(r.released),
            "suppressed": bool(r.suppressed), "streak_frames": int(r.streak_frames),
            "sustain_frames_needed": int(r.sustain_frames_needed),
            "rect": (x, y, w, h), "face": face, "yaw_deg": round(yaw, 1),
            "pitch_deg": round(pitch, 1), "tilt_deg": round(tilt, 1),
            "leaning": leaning, "left_area": left, "objects": objs,
            "source": "project", "signals": dict(CAMERA_SIGNAL_AVAIL or {}),
            "contributions": dict(r.contributions or {}),
        }
        self.last = out
        if out["just_triggered"]:
            self.alarms.append({
                "timestamp": out["timestamp"], "level": out["level"],
                "pattern": out["pattern"], "pattern_desc": out["pattern_desc"],
                "risk": out["risk"], "reason_text": out["reason_text"],
                "rect": out["rect"], "pid": out.get("pid"),
            })
        return out

    # ------------------------------------------------------------------
    def status_text(self):
        """一行状态（02/03 状态栏直接用）"""
        if not self.ok:
            return "作弊识别：不可用（%s）" % (self.import_error or "原因未知")
        r = self.last
        if not r:
            return "作弊识别：待分析"
        if not r.get("ok"):
            return "作弊识别：%s" % r.get("reason", "未检出")
        txt = "作弊风险：%s %.0f" % (r["level"], r["risk"])
        if r.get("triggered"):
            txt += "【%s】" % (r.get("pattern_desc") or r.get("pattern") or "告警中")
        elif r.get("suppressed"):
            txt += "（单帧动作已被抑制）"
        if self.alarms:
            txt += " ｜ 本次已告警 %d 次" % len(self.alarms)
        return txt

    def badge_text(self):
        """画在人框旁的小标签：低于"关注"就不画，避免糊屏"""
        r = self.last
        if not r or not r.get("ok"):
            return ""
        if r.get("triggered"):
            return "⚠%s %.0f" % (r["level"], r["risk"])
        if r["risk"] >= self.cfg.risk_threshold * 0.7:
            return "%s %.0f" % (r["level"], r["risk"])
        return ""

    def summary(self):
        if not self.ok:
            return {"ok": False, "error": self.import_error}
        s = dict(self.det.summary() or {})
        s.update({"ok": True, "feed_hz": self.feed_hz, "frames": self._frames,
                  "skipped": self._skipped, "alarms": len(self.alarms),
                  "backend": self.backend})
        return s

    def reset(self):
        """换机位/重播时复位（清窗口与告警历史）"""
        self.alarms = []
        self.last = None
        self._frames = 0
        self._skipped = 0
        self._t0 = time.time()
        if self.ok:
            self.det = CheatDetector(self.cfg)
            if self.perc is not None:
                self.perc.reset()


def probe():
    """命令行自检：打印配置、信号可得性，并用内置场景验证"瞬时抑制 vs 持续告警" """
    print("=" * 78)
    print("作弊识别结合层自检")
    print("=" * 78)
    w = CheatWatch(feed_hz=10.0)
    if not w.ok:
        print("❌ 引擎不可用：%s" % w.import_error)
        return 2
    print("引擎：", w.describe())
    print("信号可得性：", w.signal_notes())
    print("模型：", default_model_path() or "（未找到本项目 models\\yolo11n.onnx，引擎将自行搜索）")
    try:
        from cheatdetect import simulate as sim
    except Exception as e:
        print("（跳过场景验证：%s）" % e)
        return 0

    def run(name, frames, sec_per_frame=0.1):
        w.reset()
        alarm_at = None
        peak = 0.0
        for i, f in enumerate(frames):
            f.frame_id, f.timestamp = i, i * sec_per_frame
            r = w.det.push(f)
            peak = max(peak, float(r.risk))
            if r.just_triggered and alarm_at is None:
                alarm_at = i
        return alarm_at, peak

    import random
    rng = random.Random(2026)
    cases = [("安静答题（不该告警）", sim.sc_quiet_writing(rng), False),
             ("伸懒腰（瞬时，不该告警）", sim.sc_stretch(rng), False),
             ("持续偷看邻座（该告警）", sim.sc_steady_peek(rng), True),
             ("传递纸条（该告警）", sim.sc_pass_note(rng), True),
             ("使用手机（该告警）", sim.sc_phone_use(rng), True)]
    bad = 0
    for name, frames, expect in cases:
        at, peak = run(name, frames)
        got = at is not None
        mark = "PASS" if got == expect else "FAIL"
        if got != expect:
            bad += 1
        print("  %s %-22s 告警=%s（第 %s 帧）峰值风险=%.1f  期望告警=%s"
              % (mark, name, "是" if got else "否",
                 "-" if at is None else at, peak, "是" if expect else "否"))
    print("结合层自检：%s" % ("全部通过" if bad == 0 else "%d 项不符合预期" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(probe())
