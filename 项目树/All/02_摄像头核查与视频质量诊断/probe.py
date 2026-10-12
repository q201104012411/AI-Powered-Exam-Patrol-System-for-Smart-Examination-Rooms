# -*- coding: utf-8 -*-
"""
在线状态 / 录像完整性 / 时间一致性 / 音频链路 探针

对应《摄像头核查与视频质量诊断.md》核查内容表的第 1、4、5、6 行。

原则：**探测不到就如实报失败，不假装在线**。
凡是外部依赖（真实 RTSP、真实录像索引、真实音频设备）缺失，都返回明确原因。
"""
import os
import threading
import time

import numpy as np

import camera_stream
import config
import models

try:
    import cv2
except ImportError:                       # 允许无 OpenCV 环境下跑纯台账逻辑
    cv2 = None

# 取流探测的并发上限：卡死的探测线程（C 调用里出不来）是收不回来的，
# 因此宁可让后来的探测**快速失败并说明原因**，也不能让线程无限堆积。
_PROBE_SLOTS = threading.BoundedSemaphore(3)


# ---------------- 在线状态 ----------------
def heartbeat_status(device, now=None, timeout_sec=90):
    """
    心跳判定：返回 (online, 说明)
    设备从未上报心跳（last_heartbeat=0）时判为"无心跳记录"，不算在线。
    """
    now = now if now is not None else models.now_epoch()
    last = float(getattr(device, "last_heartbeat", 0) or 0)
    if last <= 0:
        return False, "无心跳记录"
    age = now - last
    if age > timeout_sec:
        return False, "心跳已中断 %.0f 秒（> %d 秒）" % (age, timeout_sec)
    return True, "心跳正常（%.0f 秒前）" % age


def probe_stream(url, timeout_sec=6.0, device_id=None):
    """
    取流探测：返回 (ok, frame, reason)

    三层保护，保证"探测本身"不会卡死巡检：
      · 独立线程 + 超时（超时立即返回，不 join 卡住的线程）；
      · 打开超时由 camera_stream 兜住（FFMPEG 带 socket 超时选项）；
      · 并发上限：卡死的探测线程回收不了，超过 3 路同时探测时后来的**直接快速失败**，
        绝不会"三路不通就等于整个系统假死"。
    """
    if cv2 is None:
        return False, None, "当前 Python 未安装 OpenCV，无法取流"
    if not url:
        return False, None, "未配置取流地址"

    timeout_sec = max(1.0, float(timeout_sec or 6.0))
    # 打开/读取各自不超过总预算，避免"打开 6 秒 + 读取 6 秒"远超调用方预期
    open_timeout = max(1.5, timeout_sec * 0.6)
    box = {"frame": None, "reason": "取流超时（%.0fs）" % timeout_sec}

    if not _PROBE_SLOTS.acquire(blocking=False):
        return False, None, "取流探测并发已满（有探测卡在网络无响应上），请稍后重试"

    def worker():
        cap = None
        try:
            cap, info = camera_stream.open_capture(url, timeout=open_timeout, log_it=False)
            if cap is None:
                box["reason"] = "取流失败：%s" % (info or ("无法打开 %s" % _short(url)))
                return
            ok, frame = cap.read()
            if not ok or frame is None:
                box["reason"] = "取流失败：读不到帧"
                return
            box["frame"] = frame
            box["reason"] = ""
        except Exception as e:                       # noqa: BLE001
            box["reason"] = "取流异常：%s" % str(e)[:100]
        finally:
            # 释放丢后台：卡住的句柄 release 也可能阻塞，不能拖住探测线程的记账
            camera_stream.release_async(cap, "探测结束")
            _PROBE_SLOTS.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    if box["frame"] is None:
        if t.is_alive():
            # 线程卡在 C 调用里：不 join、不清理，让它在后台自然结束
            return False, None, "取流超时（%.0fs，网络无响应）" % timeout_sec
        return False, None, box["reason"] or "取流失败"
    return True, box["frame"], "取流成功"


def _short(url):
    u = str(url)
    return u if len(u) <= 40 else u[:37] + "..."


# ---------------- 录像完整性 ----------------
def recording_completeness(segments, expected_interval=None, tolerance=None,
                           since=None, until=None):
    """
    按录像段索引核查连续性。
    segments: [{"start": epoch, "duration": sec, "size": bytes, "playable": bool}, ...]
    返回 dict：{ok, gaps:[...], missing, total_sec, expected_sec, playable}
    抽不到任何段 => 明确判"无录像可回放"，不谎报完整。
    """
    expected_interval = float(expected_interval or config.RECORDING["segment_sec"])
    tolerance = float(tolerance if tolerance is not None else
                      config.RECORDING["gap_tolerance_sec"])
    segs = sorted([s for s in (segments or []) if s.get("start") is not None],
                  key=lambda x: float(x["start"]))
    if not segs:
        return {"ok": False, "gaps": [], "missing": True, "total_sec": 0.0,
                "expected_sec": 0.0, "playable": False,
                "reason": "无录像段（无录像可回放）"}

    gaps = []
    total = 0.0
    for i, s in enumerate(segs):
        dur = float(s.get("duration") or expected_interval)
        total += dur
        if i == 0:
            continue
        prev = segs[i - 1]
        prev_end = float(prev["start"]) + float(prev.get("duration") or expected_interval)
        gap = float(s["start"]) - prev_end
        if gap > tolerance:
            gaps.append({"after": prev.get("id") or i - 1,
                         "before": s.get("id") or i,
                         "gap_sec": round(gap, 1),
                         "at": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(prev_end))})
    span = (float(until) - float(since)) if (since and until) else total
    unplayable = [s for s in segs if s.get("playable") is False]
    ok = (not gaps) and (not unplayable) and total > 0
    return {"ok": ok, "gaps": gaps, "missing": False, "total_sec": round(total, 1),
            "expected_sec": round(span, 1), "playable": not unplayable,
            "unplayable_count": len(unplayable),
            "reason": ("录像连续（%d 段 / %.0f 秒）" % (len(segs), total)) if ok
                      else ("发现 %d 处丢段、%d 段不可回放" % (len(gaps), len(unplayable)))}


# ---------------- 时间一致性 ----------------
def time_drift(device_epoch, standard_epoch, max_drift=None):
    """返回 (ok, drift_sec, 说明)。关键指标要求 ≤1 秒。"""
    max_drift = float(max_drift if max_drift is not None else
                      config.KPI["clock_drift_max_sec"])
    if device_epoch is None or standard_epoch is None:
        return False, None, "缺少时间戳，无法比对"
    d = float(device_epoch) - float(standard_epoch)
    ok = abs(d) <= max_drift
    return ok, round(d, 3), ("时钟一致（偏差 %.3f 秒）" % d if ok
                             else "时钟偏差 %.3f 秒 > %.1f 秒" % (d, max_drift))


# ---------------- 音频链路 ----------------
def audio_level(samples, sample_rate=16000, cfg=None):
    """
    音频电平检测。samples 为 -1~1 的浮点序列（或 int16，会自动归一）。
    返回 dict：{ok, rms, zcr, reason}
    """
    c = dict(config.AUDIO)
    c.update(cfg or {})
    if samples is None:
        return {"ok": False, "rms": None, "zcr": None, "reason": "未接入音频采样"}
    a = np.asarray(samples, dtype=np.float32).ravel()
    if a.size == 0:
        return {"ok": False, "rms": None, "zcr": None, "reason": "音频采样为空"}
    if np.abs(a).max() > 1.5:                     # 看起来是 int16
        a = a / 32768.0
    seconds = a.size / float(sample_rate)
    if seconds < c["min_seconds"]:
        return {"ok": False, "rms": None, "zcr": None,
                "reason": "采样时长不足（%.2fs < %.2fs）" % (seconds, c["min_seconds"])}
    rms = float(np.sqrt(np.mean(a * a)))
    zcr = float(np.mean(np.abs(np.diff(np.sign(a))) > 0))
    if rms < c["rms_silent"]:
        return {"ok": False, "rms": round(rms, 5), "zcr": round(zcr, 4),
                "reason": "拾音器无声（RMS %.5f）" % rms}
    if rms > c["rms_noise"] and zcr > c["zcr_noise"]:
        return {"ok": False, "rms": round(rms, 5), "zcr": round(zcr, 4),
                "reason": "持续噪声（RMS %.3f / 过零率 %.2f）" % (rms, zcr)}
    return {"ok": True, "rms": round(rms, 5), "zcr": round(zcr, 4),
            "reason": "音频链路正常（RMS %.4f）" % rms}


def synth_audio(kind="normal", seconds=3.0, sample_rate=16000):
    """生成测试音频（自检与单测用）：normal / silent / noise"""
    n = int(seconds * sample_rate)
    t = np.arange(n, dtype=np.float32) / sample_rate
    if kind == "silent":
        return np.zeros(n, dtype=np.float32), sample_rate
    if kind == "noise":
        rng = np.random.RandomState(1)
        return (rng.normal(0, 0.55, n)).astype(np.float32), sample_rate
    return (0.08 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sample_rate


# ---------------- 取流地址规范化 ----------------
def normalize_url(url):
    """
    把用户填的地址整理成 OpenCV 能吃的形式：
      纯数字（0/1/2）-> 本机摄像头序号
      rtsp/http/文件路径 -> 原样
    """
    u = str(url or "").strip()
    if u.isdigit():
        return int(u)
    return u


def url_kind(url):
    u = str(url or "").strip()
    if u.isdigit():
        return "本机摄像头"
    low = u.lower()
    if low.startswith("rtsp://"):
        return "RTSP"
    if low.startswith("http://") or low.startswith("https://"):
        return "HTTP 流"
    if not u:
        return "未配置"
    if os.path.exists(u):
        return "本地视频文件"
    return "其他"
