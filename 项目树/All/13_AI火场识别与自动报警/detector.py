# -*- coding: utf-8 -*-
r"""
检测后端装配：自动在「多信号传统视觉」与「ONNX 火焰/烟雾模型」之间选择

选择顺序（backend=auto 时）：
    1) 本目录 models\ 下的 fire*.onnx（或 settings 里显式指定的 onnx_model_path）
       -> 用 ONNX 后端（需要 onnxruntime）
    2) 否则用多信号传统视觉后端（detect.VisionFireBackend，零额外依赖）

**不谎报能力**：ONNX 后端只有在模型真的能加载、输出真的能解析时才启用；
任何一步失败都如实回落到传统视觉，并把原因写进返回的说明里（describe_choice()）。

ONNX 模型期望的输出格式（本文件支持这几种常见 YOLO 导出）：
    · (1, N, 5+C)  每行 [cx, cy, w, h, obj, cls0, cls1, ...]（YOLOv5/v7 style）
    · (1, 4+C, N)  每列 [cx, cy, w, h, cls0, ...]（YOLOv8/v11 无 obj 的导出）
    坐标按"输入图尺度"理解（letterbox 后），本文件会反映射回原图。
类别名取自 settings 的 onnx_fire_classes（默认 "fire,smoke"），
命中的类别按"火在前、烟在后"取置信度；两个都没命中则视为无火。
"""
from __future__ import annotations

import os

import config

try:
    import numpy as np
except ImportError:
    np = None


class OnnxFireBackend(object):
    """ONNX 火焰/烟雾检测后端。加载失败时由 factory 捕获并回落。"""

    name = "onnx"

    def __init__(self, model_path, cfg=None, providers=None):
        if np is None:
            raise RuntimeError("缺少 numpy")
        import onnxruntime  # 延迟 import：没装时由调用方回落

        self.cfg = cfg or config.load_settings()
        self.model_path = model_path
        self.input_dim = int(self.cfg.get("onnx_input_dim") or 640)
        self.conf = float(self.cfg.get("onnx_conf") or 0.35)
        wanted = str(self.cfg.get("onnx_fire_classes") or "fire,smoke")
        self.class_names = [s.strip().lower() for s in wanted.split(",") if s.strip()]

        so = onnxruntime.SessionOptions()
        so.log_severity_level = 3                    # 静音 ORT 的啰嗦日志
        try:
            self._sess = onnxruntime.InferenceSession(
                model_path, sess_options=so, providers=providers)
        except Exception as exc:                     # noqa: BLE001
            raise RuntimeError("加载 ONNX 模型失败：%s" % str(exc)[:160])
        self._in = self._sess.get_inputs()[0]
        self._input_name = self._in.name
        shape = list(getattr(self._in, "shape", []) or [])
        # 有些导出的 NCHW 里 H/W 是动态的（'height'/'width'），取不到就用设置里的
        for i, tag in ((2, "height"), (3, "width")):
            if i < len(shape) and isinstance(shape[i], int) and shape[i] > 0:
                self.input_dim = int(shape[i])
                break
        self._loaded_ok = True

    # ---------------- 推理 ----------------
    def reset(self):
        """ONNX 后端无时序状态（闪烁/增长由上层不参与），保留接口一致性。"""

    def analyze(self, frame):
        import cv2
        import models

        if frame is None or getattr(frame, "ndim", 0) != 3:
            return models.ok_detection(self.name)
        h0, w0 = frame.shape[:2]
        blob, scale, pad = self._letterbox(frame)
        try:
            outs = self._sess.run(None, {self._input_name: blob})
        except Exception as exc:                     # noqa: BLE001
            return models.Detection(has_fire=False, score=0.0, backend=self.name,
                                    notes=["推理失败：%s" % str(exc)[:120]])
        boxes, scores, class_ids = self._decode(outs)
        if len(boxes) == 0:
            return models.ok_detection(self.name)

        keep = self._nms(boxes, scores, iou_thr=0.45)
        regions, best_fire, best_smoke, names = [], 0.0, 0.0, []
        total = float(max(1, h0 * w0))
        for i in keep:
            x1, y1, x2, y2 = boxes[i]
            # 反映射回原图
            x1 = (x1 - pad[0]) / scale
            y1 = (y1 - pad[1]) / scale
            x2 = (x2 - pad[0]) / scale
            y2 = (y2 - pad[1]) / scale
            x1, y1 = max(0, int(round(x1))), max(0, int(round(y1)))
            x2, y2 = min(w0, int(round(x2))), min(h0, int(round(y2)))
            bw, bh = max(1, x2 - x1), max(1, y2 - y1)
            cname = self._class_name(int(class_ids[i]))
            score = float(scores[i])
            names.append("%s %.2f" % (cname, score))
            if "fire" in cname or "flame" in cname or "火" in cname:
                best_fire = max(best_fire, score)
                regions.append((x1, y1, bw, bh, bw * bh))
            elif "smoke" in cname or "烟" in cname:
                best_smoke = max(best_smoke, score)
        area = sum(r[4] for r in regions) / total
        score = max(best_fire, best_smoke * 0.8)
        parts = {}
        if best_fire > 0:
            parts["模型-火"] = round(best_fire, 4)
        if best_smoke > 0:
            parts["模型-烟"] = round(best_smoke * 0.8, 4)
        return models.Detection(has_fire=score > 0, score=score, parts=parts,
                                fire_ratio=area, smoke_ratio=best_smoke,
                                regions=regions, backend=self.name,
                                notes=["检出：%s" % "、".join(names[:4])] if names else [])

    # ---------------- 预处理 ----------------
    def _letterbox(self, frame):
        import cv2
        h0, w0 = frame.shape[:2]
        dim = self.input_dim
        scale = min(dim / float(w0), dim / float(h0))
        nw, nh = max(1, int(round(w0 * scale))), max(1, int(round(h0 * scale)))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((dim, dim, 3), 114, np.uint8)
        dx, dy = (dim - nw) // 2, (dim - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = resized
        blob = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        return blob[None, ...], scale, (dx, dy)

    # ---------------- 后处理 ----------------
    def _class_name(self, cid):
        if 0 <= cid < len(self.class_names):
            return self.class_names[cid]
        return "class%d" % cid

    def _decode(self, outs):
        """
        把 (1,N,5+C) 或 (1,4+C,N) 统一解成 (boxes_xyxy, scores, class_ids)。
        识别不了就返回空（上层据此如实报"无检出"，不猜）。
        """
        if not outs:
            return [], [], []
        pred = np.asarray(outs[0])
        if pred.ndim == 3:
            pred = pred[0]
        if pred.ndim != 2:
            return [], [], []
        rows, cols = pred.shape
        # 转成 (N, features)：列数少的那一维是 features
        if cols > rows:
            pred = pred.T
        n, feat = pred.shape
        if feat < 6:
            return [], [], []

        if feat == 6:                        # [x1,y1,x2,y2,conf,cls]（已 NMS 的导出）
            boxes = pred[:, :4].astype(np.float32)
            scores = pred[:, 4].astype(np.float32)
            class_ids = pred[:, 5].astype(np.int32)
        else:
            # 判断有没有 obj 通道：YOLOv5/v7 是 5+C，v8/v11 是 4+C
            n_cls = len(self.class_names)
            boxed = pred[:, :4].astype(np.float32)
            rest = pred[:, 4:]
            if rest.shape[1] == n_cls + 1:
                obj = rest[:, 0]
                cls_scores = rest[:, 1:]
                class_ids = np.argmax(cls_scores, axis=1)
                scores = obj * cls_scores[np.arange(len(obj)), class_ids]
            else:
                cls_scores = rest
                class_ids = np.argmax(cls_scores, axis=1)
                scores = cls_scores[np.arange(len(class_ids)), class_ids]
            # cx,cy,w,h -> x1,y1,x2,y2
            cx, cy, bw, bh = boxed[:, 0], boxed[:, 1], boxed[:, 2], boxed[:, 3]
            boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

        keep = scores >= self.conf
        if not np.any(keep):
            return [], [], []
        return boxes[keep], scores[keep], class_ids[keep]

    @staticmethod
    def _nms(boxes, scores, iou_thr=0.45):
        """纯 numpy NMS，避免依赖 torchvision。返回保留的下标列表。"""
        if len(boxes) == 0:
            return []
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(int(i))
            if order.size == 1:
                break
            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])
            inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            union = areas[i] + areas[rest] - inter
            iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
            order = rest[iou <= iou_thr]
        return keep


# ============================================================ 工厂
def build_backend(cfg=None, force=None):
    """
    按设置装配后端。返回 (backend, 说明)。
    **永不抛异常**：ONNX 任何环节失败都回落到传统视觉，并把原因写在说明里。
    """
    import detect as detect_mod

    cfg = cfg or config.load_settings()
    mode = (force or cfg.get("backend") or "auto").lower()

    if mode == "vision":
        return detect_mod.VisionFireBackend(cfg), "多信号传统视觉（按设置强制）"

    model = config.onnx_model_path(cfg)
    if mode == "onnx" and not model:
        b = detect_mod.VisionFireBackend(cfg)
        return b, "设置要求 ONNX，但 models\\ 下没有 fire*.onnx —— 已回落到传统视觉"

    if model:
        try:
            b = OnnxFireBackend(model, cfg)
            return b, "ONNX 模型：%s" % os.path.basename(model)
        except ImportError as exc:
            reason = "未安装 onnxruntime（%s）" % str(exc)[:60]
        except Exception as exc:                     # noqa: BLE001
            reason = str(exc)[:160]
        b = detect_mod.VisionFireBackend(cfg)
        return b, "ONNX 模型不可用（%s）—— 已回落到传统视觉" % reason

    return detect_mod.VisionFireBackend(cfg), "多信号传统视觉（当前没有可用 ONNX 模型）"


def build_detector(cfg=None, on_alarm=None, on_clear=None, force=None):
    """装配带确认闸门的 FireDetector。返回 (detector, 说明)。"""
    import detect as detect_mod

    cfg = cfg or config.load_settings()
    backend, why = build_backend(cfg, force=force)
    det = detect_mod.FireDetector(backend=backend, cfg=cfg,
                                 on_alarm=on_alarm, on_clear=on_clear)
    return det, why


def describe_choice(cfg=None):
    """只探测不动手：告诉用户"现在会用哪个后端、为什么"。"""
    cfg = cfg or config.load_settings()
    model = config.onnx_model_path(cfg)
    try:
        import onnxruntime  # noqa: F401
        ort = True
    except Exception:
        ort = False
    mode = (cfg.get("backend") or "auto").lower()
    if mode == "vision":
        return {"后端": "多信号传统视觉", "原因": "设置为 vision"}
    if model and ort:
        return {"后端": "ONNX 模型", "模型": model, "原因": "找到模型且 onnxruntime 可用"}
    if model and not ort:
        return {"后端": "多信号传统视觉", "模型": model,
                "原因": "有模型但未安装 onnxruntime，已回落"}
    return {"后端": "多信号传统视觉",
            "原因": "models\\ 下没有 fire*.onnx（可放一个启用模型后端）"}
