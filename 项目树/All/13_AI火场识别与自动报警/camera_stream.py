# -*- coding: utf-8 -*-
r"""
取流内核：把 OpenCV 的「网络取流」包成**永不卡死界面**的形式。

背景（旧版真实的卡死问题）
    1) `cv2.VideoCapture(url)` 与 `cap.read()` 都是**阻塞调用**：网络断了、RTSP 无响应、
       推流端半死不活（TCP 连上了但不发数据）时，它们会一直不返回；
    2) 旧版把打开流程直接放在 tkinter 主线程里（打开失败还会依次试 FFMPEG/GSTREAMER/默认
       三个后端），于是一路摄像头不通，整个界面就冻住；
    3) 采集线程里的 `cap.read()` 卡住后，`_cam_running` 标志形同虚设 —— 停止按钮点了没反应，
       "画面停住 + 界面还在" 就是这种"半卡死"；
    4) 卡住的线程里 `cap.release()` 也可能阻塞，直接当清理手段反而把界面一起拖死。

本模块的对策
    · **打开带超时**：`open_capture()` 在独立线程里打开，超时就判失败并转后台，
      主线程永远不因为打开流而等待；
    · **读取带看门狗**：`FrameReader` 在后台线程里连续读帧，另有看门狗线程盯着
      "多久没有新帧" —— 一旦超过 `stall_timeout` 就判 `stalled`（画面中断），
      并**另起线程**重连（不等待、不 join 那个卡住的线程）；
    · **释放不阻塞**：`_release_async()` 把 `cap.release()` 丢到后台线程，主线程只更新标志；
    · **给 FFMPEG 装上超时**：打开网络流前设置 `OPENCV_FFMPEG_CAPTURE_OPTIONS`
      （socket 超时 + keepalive + 丢帧），多数情况下 read() 自己就会返回 False，
      看门狗只是最后一道防线；
    · **连接数上限**：卡死的流不会被立刻回收（Python 杀不掉卡在 C 调用里的线程），
      所以限制同时打开的流数量，超出时**快速失败并给出明确原因**，而不是继续堆积。

对外只有三个概念：`open_capture()`、`FrameReader`、`release_async()`。
界面层只需要轮询 `FrameReader.snapshot()`，永远不做阻塞调用。

维护约定：本文件在 01 / 02 / 03 / 11 / 13 各存一份完全相同的副本
（每套都要能整目录拷走独立运行，不能互相 import）。改动时三份必须同步。
"""
import os
import threading
import time

try:
    import cv2
except ImportError:                        # 允许无 OpenCV 环境下 import 本模块（纯台账逻辑）
    cv2 = None

# ---------------- 可调参数 ----------------
OPEN_TIMEOUT_SEC = 8.0        # 单次打开流的超时（含建连）
OPEN_TOTAL_BUDGET_SEC = 14.0  # 一个流依次试多个后端的**总**预算，超了立刻放弃
STALL_TIMEOUT_SEC = 6.0       # 网络流：多久没有新帧判为"画面中断"
STALL_TIMEOUT_LOCAL_SEC = 4.0  # 本机摄像头：阈值更短（本地设备不该长时间不出帧）
RECONNECT_MIN_SEC = 1.0       # 重连最小间隔（失败后指数退避）
RECONNECT_MAX_SEC = 30.0      # 重连最大间隔
MAX_OPEN_STREAMS = 16         # 同时打开的流上限（多路并发的软上限）
MAX_INFLIGHT_READERS = 6      # 单个流允许的"卡在读/开里的线程"上限，超过就停止重连（防线程堆积）

# FFMPEG 取流选项：socket 超时（微秒）+ 保活 + 只留少量缓冲，尽量让 read() 自己返回
_FFMPEG_NET_OPTS = "rtsp_transport;tcp|stimeout;5000000|timeout;5000000|" \
                   "max_delay;500000|reorder_queue_size;0|flags;low_delay|" \
                   "fflags;nobuffer|buffer_size;1048576"
_FFMPEG_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"

_LOG_LOCK = threading.Lock()
_LOG_PATH = None


# ---------------- 诊断日志（卡死排查靠它） ----------------
def log(message, path=None):
    """把取流层的关键事件写进日志文件（默认 data/logs/camera_stream.log）。

    这一类问题最麻烦的地方是"事后无据可查"，所以打开失败、画面中断、重连次数
    都要落盘。写日志失败绝不影响播放。
    """
    global _LOG_PATH
    if path is not None:
        _LOG_PATH = str(path)
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with _LOG_LOCK:
            target = _LOG_PATH
            if target is None:
                try:
                    import config
                    target = str(config.LOG_DIR / "camera_stream.log")
                except Exception:
                    return
                _LOG_PATH = target
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


# ---------------- 工具 ----------------
def is_local_device(url):
    """本机摄像头（0/1/2 或 opencv 的 device 编号）"""
    if isinstance(url, int):
        return True
    return str(url or "").strip().isdigit()


def short_url(url, limit=48):
    u = str(url)
    return u if len(u) <= limit else u[:limit - 3] + "..."


def _prepare_ffmpeg_env():
    """打开网络流之前设置 FFMPEG 取流选项（只在没被用户显式设置过时）"""
    if os.environ.get(_FFMPEG_ENV):
        return
    os.environ[_FFMPEG_ENV] = _FFMPEG_NET_OPTS
    # 读失败时少重试，避免 read() 长时间不返回
    os.environ.setdefault("OPENCV_FFMPEG_READ_ATTEMPTS", "3")


def release_async(cap, why=""):
    """把 cap.release() 丢到后台线程：卡住的流 release 也可能阻塞，绝不能在界面线程里做。

    返回后台线程（一般不需要 join）。
    """
    if cap is None:
        return None

    def _do():
        try:
            cap.release()
        except Exception:
            pass

    t = threading.Thread(target=_do, name="cap-release", daemon=True)
    t.start()
    if why:
        log("异步释放取流句柄：%s" % why)
    return t


# ---------------- 打开（带超时，永不阻塞调用方） ----------------
def _try_open_once(url, backend, timeout):
    """在独立线程里打开一个后端；返回 (cap, info)。超时也返回，不抛异常、不 join。"""
    return _try_open_once_with(url, backend, timeout, None)


def _try_open_once_with(url, backend, timeout, capture_factory):
    box = {"cap": None, "info": ""}

    def worker():
        cap = None
        try:
            factory = capture_factory
            if factory is not None:
                cap = factory(url)
            else:
                _prepare_ffmpeg_env()
                if backend:
                    cap = cv2.VideoCapture(url, backend)
                else:
                    cap = cv2.VideoCapture(url)
            if cap is None or not cap.isOpened():
                box["info"] = "无法打开"
                if cap is not None:
                    release_async(cap, "打开失败")
                return
            # 缓冲区降到 1，减少"画面滞后好几秒"（假采集器没有这个属性，忽略即可）
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            box["cap"] = cap
        except Exception as e:                            # noqa: BLE001
            box["info"] = "%s: %s" % (type(e).__name__, str(e)[:80])
            if cap is not None:
                release_async(cap, "打开异常")

    t = threading.Thread(target=worker, name="cap-open", daemon=True)
    t.start()
    t.join(timeout)
    if box["cap"] is None and t.is_alive():
        # 打开卡住了：不等待、不 join（daemon 线程退出进程时自然结束），如实报超时
        return None, "打开超时（%.0fs，网络不可达或取流地址无响应）" % timeout
    return box["cap"], box["info"]


def open_capture(url, width=0, height=0, fps=0, timeout=OPEN_TIMEOUT_SEC, log_it=True,
                 capture_factory=None):
    """按"网络流优先 FFMPEG，本机设备优先 DirectShow"的策略打开取流。

    返回 (cap|None, 说明)。**调用方可以是界面线程**：最坏情况等 `OPEN_TOTAL_BUDGET_SEC`，
    因此界面层仍然建议放到后台线程里调用（见 FrameReader）。

    `capture_factory` 用于自动化测试注入假采集器；`timeout=None` 表示不设超时
    （只给注入的假采集器用，真实设备一律带超时）。
    """
    if cv2 is None:
        return None, "当前 Python 未安装 OpenCV"
    if url is None or (not isinstance(url, int) and not str(url).strip()):
        return None, "未配置取流地址"

    if capture_factory is not None:
        if timeout is None:
            try:
                cap = capture_factory(url)
            except Exception as e:                        # noqa: BLE001
                return None, "打开异常：%s" % str(e)[:80]
            return (cap, "") if cap is not None else (None, "打开失败")
        return _try_open_once_with(url, None, float(timeout), capture_factory)

    local = is_local_device(url)
    if local:
        try:
            url = int(url)
        except (TypeError, ValueError):
            local = False

    if local:
        backends = []
        if os.name == "nt" and hasattr(cv2, "CAP_DSHOW"):
            backends.append((cv2.CAP_DSHOW, "DirectShow"))
        backends.append((0, "默认"))
    else:
        _prepare_ffmpeg_env()
        backends = []
        if hasattr(cv2, "CAP_FFMPEG"):
            backends.append((cv2.CAP_FFMPEG, "FFMPEG"))
        if hasattr(cv2, "CAP_GSTREAMER"):
            backends.append((cv2.CAP_GSTREAMER, "GSTREAMER"))
        backends.append((0, "默认"))

    deadline = time.time() + max(OPEN_TOTAL_BUDGET_SEC, timeout)
    last = "未尝试任何后端"
    for backend, name in backends:
        left = deadline - time.time()
        if left <= 0.5:
            last = "打开整体超时（已试 %s）" % last
            break
        cap, info = _try_open_once(url, backend, min(timeout, left))
        if cap is None:
            last = "%s 后端%s" % (name, ("：" + info) if info else "失败")
            continue
        # 打开成功：设置分辨率/帧率（失败无所谓，摄像头不支持会自行回退）
        for prop, val in ((cv2.CAP_PROP_FRAME_WIDTH, width),
                          (cv2.CAP_PROP_FRAME_HEIGHT, height),
                          (cv2.CAP_PROP_FPS, fps)):
            if val:
                try:
                    cap.set(prop, val)
                except Exception:
                    pass
        if log_it:
            log("打开成功[%s]：%s" % (name, short_url(url)))
        return cap, ""

    if log_it:
        log("打开失败：%s（%s）" % (last, short_url(url)))
    return None, last


# ---------------- 常驻取流（看门狗 + 自动重连） ----------------
class FrameReader(object):
    """常驻取流器：后台线程读帧，看门狗盯"是否还在出新帧"，卡住就换新连接重试。

    界面的正确用法（全部非阻塞）：
        r = FrameReader(url); r.start()
        ... 每隔 30ms ...
        snap = r.snapshot()          # {"state","frame","seq","fps","status","reconnects"}
        if snap["state"] == "live":  显示 snap["frame"]
        elif snap["state"] == "stalled": 显示"画面中断，正在重连"
        r.stop()                     # 立即返回，不等待卡住的线程

    state 取值：
        connecting  正在打开/重连
        live        正常出新帧
        stalled     已建连但不出帧（网络掉线/推流端停止）
        error       打开失败且已放弃（仍会按退避重试，直到 stop()）
        stopped     已停止
    """

    def __init__(self, url, width=0, height=0, fps=0,
                 stall_timeout=None, open_timeout=OPEN_TIMEOUT_SEC,
                 max_reconnects=None, label=None, capture_factory=None):
        self.url = url
        self.width = width
        self.height = height
        self.fps_req = fps
        self.label = label or short_url(url, 40)
        # 注入假采集器（自动化测试用）：打开超时交给 open_capture 处理
        self._capture_factory = capture_factory
        self.stall_timeout = float(
            stall_timeout if stall_timeout is not None
            else (STALL_TIMEOUT_LOCAL_SEC if is_local_device(url) else STALL_TIMEOUT_SEC))
        self.open_timeout = float(open_timeout)
        self.max_reconnects = max_reconnects      # None = 无限重试（掉线自愈）

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()            # 让看门狗立刻响应（重启/停止）
        self._generation = 0
        self._reader = None
        self._watchdog = None
        self._cap = None
        self._frame = None
        self._seq = 0
        self._state = "stopped"
        self._status = "未开启"
        self._ping = 0.0                          # 最近一次"有活动"的时间（开帧/读帧）
        self._connect_at = 0.0
        self._times = []
        self._reconnects = 0
        self._fail_streak = 0
        self._backoff = RECONNECT_MIN_SEC
        self._stopped_by_user = False
        # 当前活跃的读线程数：Python 杀不掉卡在 C 调用里的线程，只能记账。
        # 网络一直不通时重连会不断堆积线程，所以到上限就停止重连并如实告知用户。
        self._readers = 0

    # ---------- 生命周期 ----------
    def start(self):
        if self.running:
            return True
        self._stop.clear()
        self._stopped_by_user = False
        self._state = "connecting"
        self._status = "正在连接…"
        self._ping = time.time()
        self._connect_at = self._ping
        with self._lock:
            self._generation += 1
            gen = self._generation
        self._spawn(gen)
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch, name="cam-watchdog",
                                              daemon=True)
            self._watchdog.start()
        log("开始取流：%s（阈值 %.0fs）" % (self.label, self.stall_timeout))
        return True

    def stop(self, reason="用户停止"):
        self._stopped_by_user = True
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._generation += 1                 # 让在跑的读线程/看门狗都失效
            cap, self._cap = self._cap, None
            self._state = "stopped"
            self._status = "已停止"
            self._frame = None
        # 绝不在调用方线程里 release（可能阻塞）
        release_async(cap, "%s：%s" % (self.label, reason))
        log("停止取流：%s（%s）" % (self.label, reason))

    @property
    def running(self):
        return self._state not in ("stopped",)

    @property
    def generation(self):
        with self._lock:
            return self._generation

    # ---------- 读线程 ----------
    def _spawn(self, gen):
        t = threading.Thread(target=self._read_loop, args=(gen,),
                             name="cam-reader", daemon=True)
        with self._lock:
            self._reader = t
        t.start()

    def _read_loop(self, gen):
        """读帧循环。gen 是本线程的代号：被看门狗换代后立刻退出，绝不覆盖新状态。"""
        self._readers += 1
        cap = None
        try:
            cap, info = open_capture(self.url, width=self.width, height=self.height,
                                     fps=self.fps_req, timeout=self.open_timeout,
                                     capture_factory=self._capture_factory)
            # 打开动作可能来自旧线程：看门狗已换代时，**严禁**再写状态
            if self._stale(gen):
                release_async(cap, "读线程已换代")
                return
            if cap is None:
                with self._lock:
                    self._state = "error"
                    self._status = "打开失败：%s" % info
                    self._fail_streak += 1
                log("打开失败：%s（%s）" % (self.label, info))
                return
            with self._lock:
                self._cap = cap
                # 先不转 live：等真正读到帧再说。这样"打开成功但 read() 卡住"时
                # 状态一直停留在 connecting，看门狗按 stall_timeout 判中断更稳，
                # 界面也不会闪烁"已连接/等待画面"。
                self._fail_streak = 0
                self._backoff = RECONNECT_MIN_SEC

            while not self._stop.is_set() and not self._stale(gen):
                ok, frame = cap.read()
                if self._stale(gen):
                    break
                if not ok or frame is None:
                    with self._lock:
                        self._fail_streak += 1
                        streak = self._fail_streak
                        # 读失败**不刷新 _ping**，让看门狗按 stall_timeout 判定中断并重连
                        if self._state != "connecting":
                            self._state = "stalled"
                        self._status = "读取中断（连续 %d 次读不到帧），等待重连…" % streak
                    if streak >= 5:
                        log("读取中断：%s（连续 %d 次失败）" % (self.label, streak))
                        break
                    time.sleep(0.05)
                    continue
                now = time.perf_counter()
                with self._lock:
                    self._frame = frame
                    self._seq += 1
                    self._state = "live"
                    self._status = "播放中"
                    self._ping = time.time()
                    self._fail_streak = 0
                    self._times.append(now)
                    if len(self._times) > 240:
                        del self._times[:120]
        except Exception as e:                             # noqa: BLE001
            if not self._stale(gen):
                with self._lock:
                    self._state = "stalled"
                    self._status = "读取异常：%s" % str(e)[:80]
            log("读取异常：%s（%s）" % (self.label, str(e)[:120]))
        finally:
            with self._lock:
                if self._cap is not None and self._generation == gen:
                    self._cap = None
            release_async(cap, "%s 读线程结束" % self.label)
            self._readers -= 1

    # ---------- 看门狗：卡死检测 + 重连 ----------
    def _stale(self, gen):
        with self._lock:
            return self._generation != gen

    def _watch(self):
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                state = self._state
                idle = time.time() - self._ping
                gen = self._generation
                reconnects = self._reconnects
                backoff = self._backoff
            if state in ("live", "connecting") and idle > self.stall_timeout:
                # 关键：读线程卡在 C 调用里出不来 —— 判"画面中断"，另起新连接，不 join
                with self._lock:
                    if self._generation != gen:
                        continue
                    self._state = "stalled"
                    self._status = ("画面中断 %.0f 秒（网络掉线或推流停止），准备重连…"
                                    % idle)
                    cap, self._cap = self._cap, None
                log("画面中断：%s（%.1fs 无新帧）" % (self.label, idle))
                release_async(cap, "%s 看门狗判定中断" % self.label)
                self._reconnect()
                continue
            if state in ("stalled", "error"):
                with self._lock:
                    readers = self._readers
                if readers >= MAX_INFLIGHT_READERS:
                    with self._lock:
                        self._state = "error"
                        self._status = ("连续 %d 路取流线程卡死，已停止自动重连"
                                        "（请检查网络或取流地址后点「播放」重试）" % readers)
                    log("停止重连（卡死读线程 %d 个）：%s" % (readers, self.label))
                    return
                if self.max_reconnects is not None and reconnects >= self.max_reconnects:
                    with self._lock:
                        self._state = "error"
                        self._status = "重连 %d 次仍失败，已停止重试" % reconnects
                    log("重连超限放弃：%s" % self.label)
                    return
                if idle >= backoff:
                    self._reconnect()

    def _reconnect(self):
        with self._lock:
            if self._state not in ("stalled", "error", "connecting"):
                return
            self._reconnects += 1
            self._backoff = min(max(self._backoff * 1.6, RECONNECT_MIN_SEC),
                                RECONNECT_MAX_SEC)
            self._generation += 1
            gen = self._generation
            n = self._reconnects
            self._state = "connecting"
            self._status = "第 %d 次重连中…" % n
            self._ping = time.time()              # 重置计时，避免刚重连又被判中断
        log("重连 %s：第 %d 次（退避 %.1fs）" % (self.label, n, self._backoff))
        self._spawn(gen)

    # ---------- 取帧（非阻塞） ----------
    def snapshot(self):
        """界面线程调用：拿最新帧与状态，绝不阻塞。

        返回 {"state","status","frame","seq","generation","fps","reconnects"}
        frame 为 None 或 BGR ndarray（**引用**，界面层如需长期持有请自行 copy）。
        """
        with self._lock:
            frame = self._frame
            return {
                "state": self._state,
                "status": self._status,
                "frame": frame,
                "seq": self._seq,
                "generation": self._generation,
                "fps": self._fps_locked(),
                "reconnects": self._reconnects,
            }

    def latest(self):
        """兼容旧接口：返回 (frame, seq)"""
        with self._lock:
            return self._frame, self._seq

    def _fps_locked(self):
        now = time.perf_counter()
        ts = [t for t in self._times if now - t <= 1.0]
        if len(ts) < 2:
            return 0.0 if not ts else 1.0
        span = max(ts[-1] - ts[0], 1e-6)
        return round((len(ts) - 1) / span, 1)

    @property
    def fps(self):
        with self._lock:
            return self._fps_locked()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def status(self):
        with self._lock:
            return self._status
