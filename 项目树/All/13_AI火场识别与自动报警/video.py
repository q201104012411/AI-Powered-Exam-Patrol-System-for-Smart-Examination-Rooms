# -*- coding: utf-8 -*-
r"""
取流 + 后台检测线程 + 火警证据留档

分工：
    camera_stream.py   取流内核（看门狗、自动重连，**永不卡界面**）—— 照 11 的副本，未改动
    本文件              把取流、检测、证据留档串起来，界面只跟本文件打交道

线程模型（关键，别改乱）：
    · 采集/检测在**后台线程**（VideoWorker）里跑：cv2.read() 与检测都可能慢，
      放主线程会让界面卡住 —— 这是 11 号模块踩过的坑，这里从一开始就避开。
    · 界面线程只做两件事：`snapshot()` 取最新帧显示、`drain_events()` 取走新事件。
    · 证据写盘（截图/录像）在**第三个线程**（EvidenceRecorder 的写线程）里做，
      磁盘慢的时候不拖慢检测，也不卡界面。
"""
from __future__ import annotations

import os
import queue
import threading
import time

import config
import models
import storage
from camera_stream import FrameReader, is_local_device, log as _cs_log

try:
    import cv2
except ImportError:
    cv2 = None


# ============================================================ 证据留档
class EvidenceRecorder(object):
    """
    火警证据：抓拍图 + 标注图 + 告警前后短视频。

    录像策略（如实说明）：
        事件触发时手上只有"当前帧"，真正的"告警前 N 秒"需要一直录才有。
        本实现把内存里缓存的最近若干帧先写进视频开头（近似前 N 秒），
        然后继续写 post_seconds 秒的实况 —— 因此在录像开头会标注"缓存段"。
        这样做的好处是不持续占磁盘：平时一张帧都不落盘。
    """

    def __init__(self, cfg=None, store=None):
        self.cfg = cfg or config.load_settings()
        self.store = store
        self._q = queue.Queue(maxsize=600)
        self._ring = []                      # [(epoch, frame_copy), ...] 仅内存
        self._ring_lock = threading.Lock()
        self._ring_max = 900
        self._writers = {}                   # event_id -> _ClipWriter
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._errors = []
        self._frames_written = 0
        self._last_frame_at = 0.0
        #: 由 VideoWorker 同步过来的"当前活跃事件对象"（用于回填证据路径）
        self.active_event = None

    # ---------------- 帧缓存（供"前 N 秒"用） ----------------
    def push_frame(self, frame):
        """采集线程每帧调用：只做一次浅拷贝入环形缓冲，开销可控。"""
        if frame is None:
            return
        with self._ring_lock:
            self._ring.append((time.time(), frame))
            self._ring_max = max(60, int((self.cfg.get("pre_seconds") or 6) *
                                         max(1.0, 1000.0 / max(1, self.cfg["detect_interval_ms"])) * 3))
            if len(self._ring) > self._ring_max:
                del self._ring[:len(self._ring) - self._ring_max]
        self._last_frame_at = time.time()

    def _pre_frames(self, seconds):
        if seconds <= 0:
            return []
        cutoff = time.time() - float(seconds)
        with self._ring_lock:
            return [f for (t, f) in self._ring if t >= cutoff]

    # ---------------- 启动/停止 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fire-evidence",
                                       daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    # ---------------- 入队动作 ----------------
    def capture_event(self, ev, frame=None):
        """事件触发：保存抓拍图与标注图，并开启 post 段录像。"""
        self.push_frame(frame)          # 保证"当前帧"一定在环形缓冲里
        try:
            self._q.put_nowait(("snapshot", ev.event_id, frame))
        except queue.Full:
            pass
        with self._lock:
            self._writers[ev.event_id] = _ClipSpec(ev.event_id, self.cfg)

    def close_event(self, event_id, save_clip=True):
        try:
            self._q.put_nowait(("finish", event_id, save_clip))
        except queue.Full:
            pass

    def stats(self):
        with self._lock:
            open_clips = len(self._writers)
        return {"queued": self._q.qsize(), "open_clips": open_clips,
                "frames_written": self._frames_written,
                "errors": list(self._errors[-5:]), "ring": len(self._ring)}

    # ---------------- 写线程 ----------------
    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                self._expire_writers()
                continue
            if item is None:
                break
            kind, event_id, payload = item
            try:
                if kind == "snapshot":
                    self._do_snapshot(event_id, payload)
                elif kind == "finish":
                    self._do_finish(event_id, payload)
            except Exception as exc:                    # noqa: BLE001
                self._errors.append("%s: %s" % (kind, str(exc)[:120]))
                _cs_log("火警证据写入失败：%s %s" % (kind, str(exc)[:120]))
        # 收尾：把还开着的录像写完
        for eid in list(self._writers.keys()):
            try:
                self._do_finish(eid, True)
            except Exception:                            # noqa: BLE001
                pass

    def _dir(self, event_id):
        return storage.event_dir(event_id)

    def _do_snapshot(self, event_id, frame):
        ev = self.store.get_event(event_id) if self.store else None
        d = self._dir(event_id)
        saved = {}
        if frame is None or cv2 is None:
            self._errors.append("抓拍跳过：frame=%s cv2=%s"
                                % (frame is None, cv2 is None))
            return
        if self.cfg["save_snapshot"]:
            p = os.path.join(d, "snapshot.jpg")
            if _imwrite(p, frame):
                saved["snapshot"] = p
            else:
                self._errors.append("抓拍写入失败：%s" % p)
        if self.cfg["save_marked"]:
            try:
                import detect as detect_mod
                marked = detect_mod.draw_overlay(
                    frame, _det_like(ev), self.cfg,
                    extra_lines=[make_footer(event_id, ev)])
            except Exception as exc:                        # noqa: BLE001
                self._errors.append("标注图绘制失败：%s" % str(exc)[:100])
                marked = frame
            p = os.path.join(d, "marked.jpg")
            if _imwrite(p, marked):
                saved["marked"] = p
            else:
                self._errors.append("标注图写入失败：%s" % p)

        if saved.get("snapshot") or saved.get("marked"):
            # 两个动作缺一不可：
            #   ① 窄写入（只 UPDATE 路径列）——避免与主线程的整行 save 互相覆盖；
            #   ② 同时回填"当前活跃事件对象"——否则主线程后续 ack/clear 拿的是
            #      没有路径的旧对象，整行保存时又会把路径抹掉（实测踩过）。
            if self.store is not None:
                self.store.set_evidence(event_id, snapshot=saved.get("snapshot"),
                                        marked=saved.get("marked"))
            live = getattr(self, "active_event", None)
            if live is not None and live.event_id == event_id:
                if saved.get("snapshot"):
                    live.snapshot = saved["snapshot"]
                if saved.get("marked"):
                    live.marked = saved["marked"]
            if ev is not None:
                if saved.get("snapshot"):
                    ev.snapshot = saved["snapshot"]
                if saved.get("marked"):
                    ev.marked = saved["marked"]
        elif ev is None and self.store is not None:
            self._errors.append("抓拍时取不到事件行：%s" % event_id)
        # 开启录像写入器
        with self._lock:
            spec = self._writers.get(event_id)
        if spec is None:
            return
        self._start_writer(spec, frame)

    def _start_writer(self, spec, frame):
        if cv2 is None or frame is None:
            return
        d = self._dir(spec.event_id)
        path = os.path.join(d, "clip.avi")
        h, w = frame.shape[:2]
        fps = float(spec.fps)
        writer = None
        for tag in ("MJPG", "XVID", "mp4v"):
            fourcc = cv2.VideoWriter_fourcc(*tag)
            cand = cv2.VideoWriter(path, fourcc, fps, (w, h))
            if cand is not None and cand.isOpened():
                writer = cand
                spec.codec = tag
                break
            try:
                cand.release()
            except Exception:                            # noqa: BLE001
                pass
        if writer is None:
            self._errors.append("无法创建录像文件（编码器都不可用）：%s" % path)
            return
        spec.path = path
        spec.writer = writer
        # 先把"告警前"的内存缓存帧写进去（近似前 N 秒）
        pre = self._pre_frames(self.cfg["pre_seconds"])
        for f in pre:
            if f.shape[0] != h or f.shape[1] != w:
                f = cv2.resize(f, (w, h))
            writer.write(f)
        spec.pre_frames = len(pre)
        spec.started = time.time()
        spec.deadline = spec.started + float(self.cfg["post_seconds"])

    def _expire_writers(self):
        """把到达时长上限的录像收掉（post_seconds 或 clip_max_seconds）。"""
        now = time.time()
        with self._lock:
            items = list(self._writers.items())
        for eid, spec in items:
            if spec.writer is None or spec.deadline <= 0:
                continue
            hard = spec.started + float(self.cfg["clip_max_seconds"])
            if now >= min(spec.deadline, hard):
                self._finish_writer(eid, spec)

    def _finish_writer(self, event_id, spec):
        try:
            if spec.writer is not None:
                spec.writer.release()
        except Exception:                                # noqa: BLE001
            pass
        spec.writer = None
        with self._lock:
            self._writers.pop(event_id, None)
        if spec.path and os.path.isfile(spec.path) and os.path.getsize(spec.path) > 1024:
            if self.store is not None:
                # 同样是"窄写入"，避免和主线程整行保存互相覆盖
                self.store.set_evidence(event_id, clip=spec.path)
            _cs_log("火警证据录像已保存：%s（缓存段 %d 帧 + 后录，编码 %s）"
                    % (os.path.basename(spec.path), spec.pre_frames, spec.codec))
        elif spec.path and os.path.isfile(spec.path):
            # 太小的文件是坏文件，删掉免得误导
            try:
                os.remove(spec.path)
            except OSError:
                pass

    def _do_finish(self, event_id, save_clip=True):
        with self._lock:
            spec = self._writers.get(event_id)
        if spec is None:
            return
        if save_clip:
            self._finish_writer(event_id, spec)
        else:
            try:
                if spec.writer is not None:
                    spec.writer.release()
            except Exception:                            # noqa: BLE001
                pass
            with self._lock:
                self._writers.pop(event_id, None)

    # ---------------- 供采集线程喂帧 ----------------
    def feed(self, event_id, frame):
        """录像中：把实况帧写进对应事件的视频。由采集线程调用（writer.write 很快）。"""
        if frame is None or cv2 is None:
            return
        with self._lock:
            spec = self._writers.get(event_id)
        if spec is None or spec.writer is None:
            return
        try:
            spec.writer.write(frame)
            spec.written += 1
            self._frames_written += 1
        except Exception as exc:                          # noqa: BLE001
            self._errors.append("写帧失败：%s" % str(exc)[:80])


class _ClipSpec(object):
    """一个进行中的录像任务。"""

    __slots__ = ("event_id", "writer", "path", "started", "deadline", "pre_frames",
                 "written", "codec", "fps")

    def __init__(self, event_id, cfg):
        self.event_id = event_id
        self.writer = None
        self.path = ""
        self.started = 0.0
        self.deadline = 0.0
        self.pre_frames = 0
        self.written = 0
        self.codec = ""
        # 火警视频用 12fps 足够看清火势，文件也小
        self.fps = 12.0


def _det_like(ev):
    """把事件还原成一个 Detection，只为复用 draw_overlay 的画法（界面/证据一致）。"""
    if ev is None:
        return None
    return models.Detection(has_fire=True, score=ev.peak_score or ev.score,
                            parts=ev.parts or {}, fire_ratio=ev.fire_ratio,
                            smoke_ratio=ev.smoke_ratio, level=ev.level,
                            regions=ev.regions or [])


def make_footer(event_id, ev):
    if ev is None:
        return "事件 %s" % event_id
    return "%s ｜ %s ｜ 峰值 %.2f ｜ 命中 %d 帧" % (
        ev.event_id, ev.level_name(), ev.peak_score or ev.score, ev.frames_hit)


def _imwrite(path, frame):
    """中文路径安全的 imwrite（cv2.imwrite 在含中文的路径上会静默失败）。"""
    if cv2 is None or frame is None:
        return False
    ext = os.path.splitext(path)[1] or ".jpg"
    try:
        ok, buf = cv2.imencode(ext, frame)
        if not ok:
            return False
        with open(path, "wb") as f:
            f.write(buf.tobytes())
        return True
    except (OSError, cv2.error):
        return False


# ============================================================ 检测工作线程
class VideoWorker(object):
    """
    取流 + 检测的后台线程。

    对外（界面线程）只有三个非阻塞接口：
        start() / stop()
        snapshot()      -> {"state","status","frame","seq","fps","reconnects","det",...}
        drain_events()  -> [("alarm", ev, frame) | ("clear", ev) | ("error", msg)]
    """

    def __init__(self, cfg=None, store=None, detector=None, recorder=None,
                 capture_factory=None):
        self.cfg = cfg or config.load_settings()
        self.store = store
        self.recorder = recorder
        self._capture_factory = capture_factory
        self._detector = detector
        self._reader = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._events = queue.Queue()
        self._frame = None
        self._seq = 0
        self._det = models.ok_detection()
        self._last_detect_at = 0.0
        self._detect_count = 0
        self._analyzed_seq = -1
        self._error = ""

    # ---------------- 生命周期 ----------------
    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return True
        url = self.cfg["camera_url"]
        self._reader = FrameReader(url,
                                   width=int(self.cfg["camera_width"] or 0),
                                   height=int(self.cfg["camera_height"] or 0),
                                   fps=int(self.cfg["camera_fps"] or 0),
                                   label=str(self.cfg["camera_name"] or url),
                                   capture_factory=self._capture_factory)
        self._reader.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fire-detect",
                                        daemon=True)
        self._thread.start()
        config.log("开始监控：%s（%s）" % (self.cfg["camera_name"], url))
        return True

    def stop(self, reason="用户停止"):
        self._stop.set()
        if self._reader is not None:
            try:
                self._reader.stop(reason)
            except Exception:                            # noqa: BLE001
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        config.log("停止监控：%s" % reason)

    # ---------------- 对外读取 ----------------
    def snapshot(self):
        with self._lock:
            return {"frame": self._frame, "seq": self._seq, "det": self._det,
                    "detect_count": self._detect_count,
                    "last_detect_at": self._last_detect_at,
                    "error": self._error,
                    "detector_status": (self._detector.status_text()
                                        if self._detector else "未装配检测器"),
                    "active_event": (self._detector.active_event
                                     if self._detector else None),
                    "cam": (self._reader.snapshot() if self._reader else
                            {"state": "stopped", "status": "未启动", "fps": 0.0,
                             "reconnects": 0, "seq": 0})}

    def drain_events(self):
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    # ---------------- 主循环 ----------------
    def _loop(self):
        interval = max(0.02, float(self.cfg["detect_interval_ms"]) / 1000.0)
        seen_seq = -1
        try:
            while not self._stop.is_set():
                snap = self._reader.snapshot() if self._reader else {}
                frame = snap.get("frame")
                seq = snap.get("seq", 0)
                if frame is None or seq == seen_seq:
                    time.sleep(0.01)
                    continue
                seen_seq = seq
                # copy 一份再持有：帧缓冲是复用的，不 copy 的话后面会被覆盖
                frame = frame.copy()
                with self._lock:
                    self._frame = frame
                    self._seq = seq
                if self.detector is None:
                    continue
                # 检测节奏与取流节奏解耦：不必每帧都跑检测
                now = time.perf_counter()
                if now - self._last_detect_at < interval:
                    continue
                self._last_detect_at = now
                self._detect_one(frame, snap)
        except Exception as exc:                          # noqa: BLE001
            self._error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            self._events.put(("error", self._error))
            config.log("检测线程异常：%s" % self._error)

    @property
    def detector(self):
        return self._detector

    def _detect_one(self, frame, snap):
        det = self._detector.update(frame, camera_name=self.cfg["camera_name"],
                                    camera_url=self.cfg["camera_url"])
        with self._lock:
            self._det = det
            self._detect_count += 1
        if self.recorder is not None:
            # 让证据线程在回填路径时也能更新"当前活跃事件"对象，
            # 否则主线程随后 ack/clear 用旧对象整行保存，会把路径抹掉
            self.recorder.active_event = self._detector.active_event
            # 录像中的事件持续喂帧（写成 post 段）
            ev = self._detector.active_event
            if ev is not None:
                self.recorder.feed(ev.event_id, frame)

    # ---------------- 事件登记（由检测器的回调在采集线程里触发） ----------------
    def make_callbacks(self):
        """返回 (on_alarm, on_clear)，交给 FireDetector；回调里只做入队与落库。"""

        def on_alarm(det, ev):
            # 落库 + 记事件（磁盘操作很轻；截图/录像交给证据线程）
            if self.store is not None:
                try:
                    self.store.save_event(ev)
                    self.store.log("火警触发",
                                   "%s %s 峰值 %.2f 命中 %d 帧"
                                   % (ev.event_id, ev.level_name(),
                                      ev.peak_score, ev.frames_hit),
                                   "detector")
                except Exception as exc:                  # noqa: BLE001
                    self._error = "事件落库失败：%s" % str(exc)[:120]
            frame = None
            with self._lock:
                frame = self._frame
            if self.recorder is not None:
                self.recorder.capture_event(ev, frame)
            self._events.put(("alarm", ev, frame))

        def on_clear(ev):
            if self.store is not None:
                try:
                    self.store.save_event(ev)
                    self.store.log("火警解除", "%s 持续 %.1fs"
                                   % (ev.event_id, ev.duration_sec), "detector")
                except Exception:                         # noqa: BLE001
                    pass
            if self.recorder is not None:
                self.recorder.close_event(ev.event_id, save_clip=True)
            self._events.put(("clear", ev))

        return on_alarm, on_clear


# ============================================================ 画面/地址工具
def camera_hint(url):
    """给界面用的人话提示。"""
    if is_local_device(url):
        return "本机摄像头（序号 %s）" % url
    u = str(url or "")
    if u.lower().startswith(("rtsp://", "rtmp://", "http://", "https://")):
        return "网络摄像机"
    if os.path.isfile(u):
        return "本地视频文件（可用来演示：会循环播放）"
    return "自定义地址"


def list_local_cameras(max_index=6, probe_timeout=1.2):
    """
    探测本机可用摄像头序号（打开成功即认为可用）。
    注意：探测会短暂占用设备，且**同一时刻只能被一个程序占用** ——
    监控正在跑的时候不要调它。
    """
    if cv2 is None:
        return []
    found = []
    for i in range(max_index + 1):
        cap = None
        try:
            backend = cv2.CAP_DSHOW if (os.name == "nt" and hasattr(cv2, "CAP_DSHOW")) else 0
            cap = cv2.VideoCapture(i, backend) if backend else cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                ok, _f = cap.read()
                if ok:
                    found.append(i)
        except Exception:                                  # noqa: BLE001
                pass
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:                              # noqa: BLE001
                pass
    return found
