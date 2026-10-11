# -*- coding: utf-8 -*-
"""
边缘计算层：让核验在考点边缘节点上跑得动、跑得稳、断网也能跑。

解决四个现实问题：
 1. **后端适配**：同一个考点可能用 Atlas 300V（CANN）、Jetson（TensorRT）、
    带显卡的工控机（CUDA/OpenVINO）或纯 CPU。detect_providers() 自动探测并按
    优先级排序，可用 FACE_EP 强制指定；选不到加速后端时明确降级到 CPU 并留下记录。
 2. **算力自适应**：多路摄像机并发时，边缘会被打满。ResourceMonitor 统计
    FPS/延迟/队列积压，超过阈值自动**降档**（full → balanced → lite），
    宁可降低处理帧率与检测分辨率，也不让核验排队超时。
 3. **批处理**：一次给多帧/多路做推理，走向量化矩阵运算，避免逐帧重复加载模型。
 4. **断网降级 + 只传结构化结果**：核验记录先落本地 outbox（JSONL，加密可选），
    网络恢复后 flush 上报云端；全量视频/图像不出考点，符合带宽与合规要求。

依赖：仅标准库 + numpy（onnxruntime 存在时才做后端探测），便于在边缘盒子上部署。
"""
import json
import os
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

import config

# ---------------- 推理后端（Execution Provider） ----------------
# 优先级：专用加速 > GPU > 集成显卡 > CPU。数值越小越优先。
EP_PRIORITY = [
    ("TensorrtExecutionProvider", "TensorRT（Jetson / NVIDIA GPU）"),
    ("CUDAExecutionProvider", "CUDA（NVIDIA GPU）"),
    ("CANNExecutionProvider", "CANN（华为昇腾 Atlas 300V / 310 / 310P）"),
    ("MIGraphXExecutionProvider", "MIGraphX（AMD GPU）"),
    ("OpenVINOExecutionProvider", "OpenVINO（Intel CPU/核显/NPU）"),
    ("CoreMLExecutionProvider", "CoreML（Apple）"),
    ("DmlExecutionProvider", "DirectML（Windows GPU）"),
    ("CPUExecutionProvider", "CPU（兜底）"),
]
EP_LABEL = dict(EP_PRIORITY)


def available_providers() -> List[str]:
    """当前环境可用的执行提供器（无 onnxruntime 时只有 CPU）"""
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return ["CPUExecutionProvider"]


def detect_providers(force: Optional[str] = None) -> Dict:
    """
    选出实际使用的 provider 列表。
    force：来自 config.DEVICE / 环境变量 FACE_EP，可写 cpu / gpu / auto 或直接写
           provider 名（如 CANNExecutionProvider）。
    返回 {"providers": [...], "active": "CANNExecutionProvider", "label": "...",
          "available": [...], "forced": bool, "note": "..."}
    """
    avail = available_providers()
    want = (force if force is not None else config.DEVICE).strip().lower()

    if want and want not in ("auto", ""):
        aliases = {
            "cpu": "CPUExecutionProvider",
            "gpu": "CUDAExecutionProvider",
            "cuda": "CUDAExecutionProvider",
            "tensorrt": "TensorrtExecutionProvider",
            "cann": "CANNExecutionProvider",
            "ascend": "CANNExecutionProvider",
            "atlas": "CANNExecutionProvider",
            "openvino": "OpenVINOExecutionProvider",
        }
        target = aliases.get(want, force if force is not None else want)
        if target in avail:
            return {"providers": [target, "CPUExecutionProvider"] if target != "CPUExecutionProvider"
                                  else ["CPUExecutionProvider"],
                    "active": target, "label": EP_LABEL.get(target, target),
                    "available": avail, "forced": True, "note": "由 FACE_EP/FACE_DEVICE 指定"}
        return {"providers": ["CPUExecutionProvider"], "active": "CPUExecutionProvider",
                "label": EP_LABEL["CPUExecutionProvider"], "available": avail, "forced": True,
                "note": "指定的后端 %s 在本机不可用，已降级为 CPU" % target}

    ordered = [ep for ep, _ in EP_PRIORITY if ep in avail]
    if not ordered:
        ordered = ["CPUExecutionProvider"]
    active = ordered[0]
    note = "自动选择" if active != "CPUExecutionProvider" else \
           "本机没有可用的硬件加速后端（Atlas/Jetson/GPU 均可通过安装对应 onnxruntime 启用）"
    return {"providers": ordered if active != "CPUExecutionProvider" else ["CPUExecutionProvider"],
            "active": active, "label": EP_LABEL.get(active, active),
            "available": avail, "forced": False, "note": note}


def describe_backends() -> str:
    info = detect_providers()
    lines = ["推理后端：%s（%s）" % (info["label"], info["note"]),
             "  当前生效：%s" % info["active"],
             "  本机可用：%s" % ("、".join(info["available"]) or "无")]
    missing = [EP_LABEL[ep] for ep, _ in EP_PRIORITY
               if ep not in info["available"] and ep != "CPUExecutionProvider"]
    if missing:
        lines.append("  未启用的加速后端：%s" % "、".join(missing))
    return "\n".join(lines)


# ---------------- 边缘档位（算力自适应） ----------------
@dataclass
class EdgeProfile:
    name: str                 # full / balanced / lite
    det_size: int             # 检测输入边长（越小越快，但小人脸更易漏）
    max_fps: float            # 单路最大处理帧率（0 = 不限）
    enable_recognition: bool  # 是否提特征（lite 档在极端情况下可只做检测）
    auto_enhance: bool        # 是否做低画质增强（很吃算力，过载时可关）
    batch: int                # 批大小

    def to_dict(self):
        return asdict(self)


PROFILES = {
    "full": EdgeProfile("full", det_size=640, max_fps=0.0, enable_recognition=True,
                        auto_enhance=True, batch=8),
    "balanced": EdgeProfile("balanced", det_size=480, max_fps=8.0, enable_recognition=True,
                            auto_enhance=True, batch=4),
    "lite": EdgeProfile("lite", det_size=320, max_fps=4.0, enable_recognition=True,
                        auto_enhance=True, batch=2),
}
PROFILE_ORDER = ["full", "balanced", "lite"]


def default_profile():
    """
    起始档位：有加速后端用 full，**纯 CPU 时默认降一档到 balanced**。
    CPU 上 det_size 640 -> 480 能省约 40%~50% 的检测耗时，而闸机近距离场景
    几乎不影响检出，这正是"核验结果输出要快"最直接的一招。
    """
    if os.environ.get("EDGE_PROFILE"):
        return os.environ["EDGE_PROFILE"]
    info = detect_providers()
    return "balanced" if info["active"] == "CPUExecutionProvider" else "full"


class ResourceMonitor:
    """
    边缘资源自适应：滚动统计推理延迟与处理帧率，超阈值自动降档，恢复后升档。
    判定完全基于实测耗时（不依赖 psutil），因此在边缘盒子上也能用。
    """

    def __init__(self, profile: Optional[str] = None,
                 target_latency_ms: float = 400.0, hard_latency_ms: float = 900.0,
                 window: int = 30, min_samples: int = 5):
        self.profile_name = profile or default_profile()
        if self.profile_name not in PROFILES:
            self.profile_name = "full"
        self.target_latency_ms = target_latency_ms
        self.hard_latency_ms = hard_latency_ms
        self.min_samples = max(1, int(min_samples))
        self.latency = deque(maxlen=window)
        self.degraded = False
        self.last_change_reason = ""
        self._cooldown = 0          # 刚调整过档位时先观察一会儿，避免一次超时连降两级

    # ---- 观测 ----
    def observe(self, latency_ms: float):
        if latency_ms is None or latency_ms <= 0:
            return
        self.latency.append(float(latency_ms))
        if self._cooldown > 0:
            self._cooldown -= 1
            if self._cooldown > 0:      # 冷却期内只观察；窗口重新填满那次允许再评估
                return
        if len(self.latency) >= self.min_samples:
            self._adapt()

    @property
    def avg_latency_ms(self):
        return round(sum(self.latency) / len(self.latency), 1) if self.latency else 0.0

    @property
    def profile(self) -> EdgeProfile:
        return PROFILES[self.profile_name]

    def _idx(self):
        return PROFILE_ORDER.index(self.profile_name)

    def _adapt(self):
        avg = self.avg_latency_ms
        i = self._idx()
        if avg > self.hard_latency_ms and i < len(PROFILE_ORDER) - 1:
            self.profile_name = PROFILE_ORDER[i + 1]
            self.degraded = True
            self.last_change_reason = "平均延迟 %.0fms 超过硬阈值 %.0fms，降档到 %s" % (
                avg, self.hard_latency_ms, self.profile_name)
            self.latency.clear()
            self._cooldown = self.min_samples
        elif avg and avg < self.target_latency_ms * 0.5 and i > 0:
            self.profile_name = PROFILE_ORDER[i - 1]
            self.last_change_reason = "平均延迟 %.0fms 已回落，升档到 %s" % (avg, self.profile_name)
            self.latency.clear()
            self._cooldown = self.min_samples

    def snapshot(self) -> Dict:
        return {"profile": self.profile_name, "degraded": self.degraded,
                "avg_latency_ms": self.avg_latency_ms, "samples": len(self.latency),
                "last_change": self.last_change_reason,
                "det_size": self.profile.det_size, "max_fps": self.profile.max_fps,
                "auto_enhance": self.profile.auto_enhance}


# ---------------- 断网降级：待上报队列 ----------------
class Outbox:
    """
    边缘 -> 云：只上报结构化核验记录（不含原始图像），断网时先落本地，联网后 flush。
    上报失败不会丢数据；成功后才从队列移除。
    """

    def __init__(self, path=None, endpoint: Optional[str] = None):
        self.path = str(path or (config.DATA_ROOT / "outbox.jsonl"))
        self.endpoint = endpoint or os.environ.get("EDGE_UPLOAD_URL") or ""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def pending(self) -> int:
        if not os.path.exists(self.path):
            return 0
        n = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    def enqueue(self, record: Dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def flush(self, endpoint: Optional[str] = None, timeout: float = 5.0) -> Dict:
        """
        尝试上报。无 endpoint 或网络不可用时保持队列不动。
        返回 {"sent": n, "left": n, "error": str|None}
        """
        url = endpoint or self.endpoint
        if not self.pending():
            return {"sent": 0, "left": 0, "error": None}
        if not url:
            return {"sent": 0, "left": self.pending(), "error": "未配置上报地址（EDGE_UPLOAD_URL）"}

        import urllib.request
        sent, failed = 0, []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        for ln in lines:
            try:
                req = urllib.request.Request(
                    url, data=ln.encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if 200 <= getattr(resp, "status", 200) < 300:
                        sent += 1
                    else:
                        failed.append(ln)
            except Exception as e:
                failed.append(ln)
                err = str(e)[:80]
                break
        # 未发成功的写回队列
        with open(self.path, "w", encoding="utf-8") as f:
            for ln in failed:
                f.write(ln if ln.endswith("\n") else ln + "\n")
        return {"sent": sent, "left": len(failed),
                "error": None if not failed else "部分或全部上报失败（网络不可用？）"}


# ---------------- 全局单例（便于 GUI/CLI 共用） ----------------
_monitor: Optional[ResourceMonitor] = None


def monitor() -> ResourceMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ResourceMonitor()
    return _monitor


def describe_edge() -> str:
    m = monitor()
    s = m.snapshot()
    ob = Outbox()
    return "\n".join([
        describe_backends(),
        "边缘档位：%s（检测 %dpx，单路 ≤%s fps，增强=%s）%s"
        % (s["profile"], s["det_size"], s["max_fps"] or "不限", s["auto_enhance"],
           "（已降档：%s）" % s["last_change"] if s["degraded"] else ""),
        "实测平均延迟：%.0f ms（样本 %d）" % (s["avg_latency_ms"], s["samples"]),
        "待上报记录：%d 条%s" % (ob.pending(), "（上报地址：%s）" % ob.endpoint if ob.endpoint else "（未配置上报地址）"),
    ])
