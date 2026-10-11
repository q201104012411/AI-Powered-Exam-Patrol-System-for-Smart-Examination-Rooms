# -*- coding: utf-8 -*-
"""
考场监控人形检测与越界判断（演示级）

使用 OpenCV 内置 HOG + SVM 行人检测器，不需要额外模型文件。
在实时画面中绘制人形框：
  · 绿色：正常区域内
  · 红色：触发越界（越过中间警示线或贴近画面边缘）

注意：HOG 检测器对侧脸/遮挡/远距离效果一般，仅用于演示和原型验证。
生产环境建议换成 YOLO / 边缘 AI 节点返回的检测框。
"""
import cv2
import numpy as np
import os
from PIL import Image, ImageDraw, ImageFont


def _load_font(size=18):
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


class PersonDetector(object):
    def __init__(self):
        if not hasattr(cv2, "HOGDescriptor"):
            raise RuntimeError(
                "当前 OpenCV 未包含 HOGDescriptor，无法运行 AI 越界检测。\n"
                "可能原因：安装的是 opencv-python-headless 或被裁剪过的 cv2。\n"
                "可尝试：pip install --upgrade opencv-python")
        if not hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector"):
            raise RuntimeError("当前 OpenCV 缺少 HOG 默认行人检测器。")
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame, win_stride=(8, 8), padding=(16, 16), scale=1.05,
               margin_ratio=0.12):
        """返回检测框列表 [(x, y, w, h), ...]，默认外扩 12% 以框住整个人"""
        # 缩小分辨率以提升速度
        small = cv2.resize(frame, None, fx=0.5, fy=0.5)
        rects, _ = self.hog.detectMultiScale(small, winStride=win_stride,
                                             padding=padding, scale=scale)
        fh, fw = frame.shape[:2]
        out = []
        for (x, y, w, h) in rects:
            # 映射回原图坐标并外扩
            x, y, w, h = x * 2, y * 2, w * 2, h * 2
            mw = int(w * margin_ratio)
            mh = int(h * margin_ratio)
            x = max(0, x - mw)
            y = max(0, y - mh)
            w = min(fw - x, w + mw * 2)
            h = min(fh - y, h + mh * 2)
            out.append((x, y, w, h))
        return out

    @staticmethod
    def is_out_of_bounds(frame, rect, boundary_ratio=0.5, edge_margin=20):
        """
        判断人体框是否越界。
        规则：
          1. 框底部中心越过画面中间线（boundary_ratio）视为越界；
          2. 框贴近画面任意边缘（edge_margin 像素内）视为越界。
        """
        fh, fw = frame.shape[:2]
        x, y, w, h = rect
        cx = x + w // 2
        cy = y + h
        line_y = int(fh * boundary_ratio)
        if cy > line_y:
            return True
        if x < edge_margin or y < edge_margin:
            return True
        if x + w > fw - edge_margin or y + h > fh - edge_margin:
            return True
        return False

    @staticmethod
    def action_of(frame, rect, boundary_ratio=0.5):
        """
        基于人体框位置/尺寸做简单行为推断（演示级）。
        返回动作描述字符串，用于在画面上显示当前在做什么。
        """
        fh, fw = frame.shape[:2]
        x, y, w, h = rect
        bottom = y + h
        line_y = int(fh * boundary_ratio)
        # 起立/站立：人体框较高且底部跨过中间线
        if h > fh * 0.55 and bottom > line_y:
            return "起立/站立"
        # 坐下：框较矮，位于画面下半
        if h < fh * 0.45 and bottom > line_y:
            return "坐下"
        # 身体前倾：框比较宽
        if w > h * 0.55:
            return "前倾/趴桌"
        # 靠近左侧或右侧边界
        if x < fw * 0.12:
            return "靠左"
        if x + w > fw * 0.88:
            return "靠右"
        return "正常姿态"

    def draw(self, frame, boundary_ratio=0.5):
        """在帧上绘制检测框和越界线，返回（标记后的帧，越界数量，行为列表）"""
        fh, fw = frame.shape[:2]
        rects = self.detect(frame)
        violation = 0
        behaviors = []
        for r in rects:
            bad = self.is_out_of_bounds(frame, r, boundary_ratio=boundary_ratio)
            color = (0, 0, 255) if bad else (0, 255, 0)
            action = self.action_of(frame, r, boundary_ratio=boundary_ratio)
            if bad:
                violation += 1
                behaviors.append("越界")
            elif action in ("起立/站立", "前倾/趴桌", "靠左", "靠右"):
                behaviors.append(action)
            x, y, w, h = r
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        line_y = int(fh * boundary_ratio)
        cv2.line(frame, (0, line_y), (fw, line_y), (0, 165, 255), 2)

        # 用 PIL 绘制中文标签（OpenCV putText 不支持中文）
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil)
        font = _load_font(18)
        for r in rects:
            bad = self.is_out_of_bounds(frame, r, boundary_ratio=boundary_ratio)
            color = (0, 0, 255) if bad else (0, 255, 0)
            x, y, w, h = r
            action = self.action_of(frame, r, boundary_ratio=boundary_ratio)
            label = "%s（%s）" % ("越界" if bad else "正常", action)
            # BGR -> RGB
            fill = (color[2], color[1], color[0])
            draw.text((x, y - 22), label, font=font, fill=fill)
        # 越界线标签
        draw.text((10, line_y - 26), "越界线", font=font, fill=(255, 165, 0))
        frame = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        return frame, violation, behaviors
