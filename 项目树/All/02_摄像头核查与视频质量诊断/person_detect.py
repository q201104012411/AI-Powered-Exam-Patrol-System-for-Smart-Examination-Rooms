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


class PersonDetector(object):
    def __init__(self):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame, win_stride=(8, 8), padding=(16, 16), scale=1.05):
        """返回检测框列表 [(x, y, w, h), ...]"""
        # 缩小分辨率以提升速度
        small = cv2.resize(frame, None, fx=0.5, fy=0.5)
        rects, _ = self.hog.detectMultiScale(small, winStride=win_stride,
                                             padding=padding, scale=scale)
        # 映射回原图坐标
        out = []
        for (x, y, w, h) in rects:
            out.append((x * 2, y * 2, w * 2, h * 2))
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

    def draw(self, frame, boundary_ratio=0.5):
        """在帧上绘制检测框和越界线，返回（标记后的帧，越界数量）"""
        fh, fw = frame.shape[:2]
        rects = self.detect(frame)
        violation = 0
        for r in rects:
            bad = self.is_out_of_bounds(frame, r, boundary_ratio=boundary_ratio)
            color = (0, 0, 255) if bad else (0, 255, 0)
            if bad:
                violation += 1
            x, y, w, h = r
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            label = "越界" if bad else "正常"
            cv2.putText(frame, label, (x, y - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        line_y = int(fh * boundary_ratio)
        cv2.line(frame, (0, line_y), (fw, line_y), (0, 165, 255), 2)
        cv2.putText(frame, "越界线", (10, line_y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
        return frame, violation
