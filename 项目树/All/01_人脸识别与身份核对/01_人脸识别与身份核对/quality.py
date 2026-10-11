# -*- coding: utf-8 -*-
"""
图像质量评估、近似姿态估计与低质量增强。

存在的意义：考场现场的条件往往不理想——
  · 教室后排光线昏暗，人脸区域偏暗且噪声大；
  · 摄像机吊装位置偏低，考生抬头时形成"仰拍"极端视角；
  · 逆光、投影幕反光、走动中的运动模糊……
这些情况下特征向量会明显失真，如果仍然拿一个固定阈值去判定，
就会出现"本人被判不通过"或"他人被判通过"两类事故。

本模块做三件事：
  1. assess()  评估亮度/对比度/清晰度/人脸占比，并用 5 点关键点估计偏航、俯仰、滚转；
  2. enhance() 针对昏暗(CLAHE+伽马)、模糊(锐化)、噪声(双边滤波)做条件化增强；
  3. severity() 把问题分级为 ok / warn / severe，供核验器决定"放行 / 复验 / 拒绝"。

⚠️ 姿态是**近似估计**（基于 5 点关键点的几何比例映射），
   用于触发人工复验，不作为判定依据，也不是标定角度。
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import config

SEV_OK = "ok"
SEV_WARN = "warn"
SEV_SEVERE = "severe"

ISSUE_LOW_LIGHT = "光线昏暗"
ISSUE_SEVERE_DARK = "严重欠曝"
ISSUE_LOW_CONTRAST = "对比度低"
ISSUE_BLUR = "画面模糊"
ISSUE_SEVERE_BLUR = "严重模糊"
ISSUE_SMALL_FACE = "人脸过小"
ISSUE_SEVERE_SMALL = "人脸严重过小"
ISSUE_YAW = "侧脸"
ISSUE_ROLL = "歪头"
ISSUE_PITCH_UP = "仰拍（相机低于人脸）"
ISSUE_PITCH_DOWN = "俯拍（相机高于人脸）"


@dataclass
class QualityReport:
    brightness: float = 0.0        # L 通道均值 (0-255)
    contrast: float = 0.0          # L 通道标准差
    sharpness: float = 0.0         # 拉普拉斯方差
    dark_ratio: float = 0.0        # 过暗像素占比
    face_ratio: float = 0.0        # 人脸框面积 / 画面面积
    yaw: float = 0.0               # 近似偏航角（度，正=脸朝画面右侧）
    pitch: float = 0.0             # 近似俯仰角（度，**负=仰拍**，正=俯拍）
    roll: float = 0.0              # 近似滚转角（度）
    issues: List[str] = field(default_factory=list)
    severity: str = SEV_OK
    enhanced: bool = False

    def to_dict(self):
        d = asdict(self)
        d["issues"] = "、".join(self.issues)
        return d

    def issue_text(self):
        return "、".join(self.issues) if self.issues else "正常"


def _pose_from_kps(kps, bbox):
    """
    由 5 点关键点估计姿态。insightface 顺序：[左眼, 右眼, 鼻, 左嘴角, 右嘴角]
    返回 (yaw, pitch, roll)，单位度。pitch 为负表示仰拍。
    """
    try:
        (e1, e2, nose, m1, m2) = kps[0], kps[1], kps[2], kps[3], kps[4]
    except (IndexError, TypeError):
        return 0.0, 0.0, 0.0

    eye_mid = ((e1[0] + e2[0]) / 2.0, (e1[1] + e2[1]) / 2.0)
    mouth_mid = ((m1[0] + m2[0]) / 2.0, (m1[1] + m2[1]) / 2.0)
    eye_dist = max(((e2[0] - e1[0]) ** 2 + (e2[1] - e1[1]) ** 2) ** 0.5, 1e-6)
    face_h = max(mouth_mid[1] - eye_mid[1], 1e-6)

    # 偏航：鼻尖相对双眼中点的水平偏移（用眼距归一化）
    yaw = (nose[0] - eye_mid[0]) / eye_dist * 90.0

    # 滚转：双眼连线与水平线夹角
    import math
    roll = math.degrees(math.atan2(e2[1] - e1[1], e2[0] - e1[0]))

    # 俯仰：鼻到嘴的距离占"眼到嘴"的比例。正面约 0.45~0.55；
    # 仰拍时额头被压缩、鼻下比例变大；俯拍时相反。经验映射，仅用于分级。
    r = (mouth_mid[1] - nose[1]) / face_h
    pitch = (0.5 - r) * 150.0

    clamp = lambda v, m: max(-m, min(m, v))
    return clamp(yaw, 90.0), clamp(pitch, 90.0), clamp(roll, 90.0)


def assess(img, bbox=None, kps=None) -> QualityReport:
    """评估图像质量与姿态。bbox/kps 来自人脸检测结果（可选）。"""
    import cv2
    import numpy as np

    rep = QualityReport()
    if img is None or getattr(img, "size", 0) == 0:
        rep.issues = ["图像为空"]
        rep.severity = SEV_SEVERE
        return rep

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    rep.sharpness = float(lap.var())
    rep.contrast = float(gray.std())
    h, w = gray.shape[:2]

    if bbox is not None:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 > x1 and y2 > y1:
            roi = gray[y1:y2, x1:x2]
            rep.brightness = float(roi.mean())
            rep.dark_ratio = float((roi < 50).sum()) / float(roi.size)
            rep.face_ratio = float((x2 - x1) * (y2 - y1)) / float(w * h)
            # 人脸区域清晰度更能反映可用性
            rep.sharpness = float(cv2.Laplacian(roi, cv2.CV_64F).var())
    if rep.brightness == 0.0:                     # 没有人脸框时退回整幅画面
        rep.brightness = float(gray.mean())
        rep.dark_ratio = float((gray < 50).sum()) / float(gray.size)

    if kps is not None:
        rep.yaw, rep.pitch, rep.roll = _pose_from_kps(kps, bbox)

    # ---- 分级 ----
    issues, sev = [], SEV_OK

    def bump(level):
        nonlocal sev
        order = {SEV_OK: 0, SEV_WARN: 1, SEV_SEVERE: 2}
        if order[level] > order[sev]:
            sev = level

    if rep.brightness < config.QUALITY_SEVERE_BRIGHTNESS:
        issues.append(ISSUE_SEVERE_DARK); bump(SEV_SEVERE)
    elif rep.brightness < config.QUALITY_MIN_BRIGHTNESS:
        issues.append(ISSUE_LOW_LIGHT); bump(SEV_WARN)

    if rep.sharpness < config.QUALITY_SEVERE_SHARPNESS:
        issues.append(ISSUE_SEVERE_BLUR); bump(SEV_SEVERE)
    elif rep.sharpness < config.QUALITY_MIN_SHARPNESS:
        issues.append(ISSUE_BLUR); bump(SEV_WARN)

    if rep.face_ratio and rep.face_ratio < config.QUALITY_SEVERE_FACE_RATIO:
        issues.append(ISSUE_SEVERE_SMALL); bump(SEV_SEVERE)
    elif rep.face_ratio and rep.face_ratio < config.QUALITY_MIN_FACE_RATIO:
        issues.append(ISSUE_SMALL_FACE); bump(SEV_WARN)

    if rep.contrast < 20:
        issues.append(ISSUE_LOW_CONTRAST); bump(SEV_WARN)

    if abs(rep.yaw) > config.POSE_MAX_YAW:
        issues.append(ISSUE_YAW); bump(SEV_WARN)
    if abs(rep.roll) > config.POSE_MAX_ROLL:
        issues.append(ISSUE_ROLL); bump(SEV_WARN)
    if rep.pitch < -config.POSE_MAX_PITCH_UP:
        issues.append(ISSUE_PITCH_UP); bump(SEV_WARN)
    elif rep.pitch > config.POSE_MAX_PITCH_DOWN:
        issues.append(ISSUE_PITCH_DOWN); bump(SEV_WARN)

    rep.issues = issues
    rep.severity = sev
    return rep


def enhance(img):
    """
    条件化增强，专门针对考场常见劣化：
      · 昏暗  -> LAB 空间 CLAHE（局部对比度）+ 伽马提亮
      · 低对比-> CLAHE
      · 模糊  -> 非锐化掩模(USM)
      · 噪声  -> 双边滤波（保边去噪）
    返回增强后的图像（BGR）。
    """
    import cv2
    import numpy as np

    if img is None or getattr(img, "size", 0) == 0:
        return img

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    out = img

    # 1) 提亮 + 局部对比度（昏暗时）
    if mean < config.QUALITY_MIN_BRIGHTNESS * 1.6:
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        l = clahe.apply(l)
        out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        if mean < config.QUALITY_MIN_BRIGHTNESS:          # 仍然偏暗再补伽马
            # 提亮要用 gamma < 1：out = 255 * (i/255)^gamma
            # （写成 1/gamma 会变成压暗，这个方向搞反过一次）
            gamma = 0.65
            table = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)]).astype("uint8")
            out = cv2.LUT(out, table)

    # 2) 去噪（保边）
    out = cv2.bilateralFilter(out, 5, 45, 45)

    # 3) 锐化（模糊时）
    if sharp < config.QUALITY_MIN_SHARPNESS * 3:
        blur = cv2.GaussianBlur(out, (0, 0), 3)
        out = cv2.addWeighted(out, 1.5, blur, -0.5, 0)

    return out


def is_severe(rep: QualityReport) -> bool:
    return rep.severity == SEV_SEVERE


def severity_of(rep: QualityReport) -> str:
    return rep.severity
