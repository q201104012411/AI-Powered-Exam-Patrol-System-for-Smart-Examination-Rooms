# -*- coding: utf-8 -*-
"""
基准图机制（《摄像头核查与视频质量诊断.md》第四节第 1 条）

部署完成后逐机位采集基准图与标准角度参数，后续按固定周期抓帧比对，
偏移超阈值即告警。基准图与"基准时刻的指标"一起留档，比对才有参照。
"""
import os
import time

import cv2

import config
import diagnose
import models
import probe


class BaselineError(Exception):
    pass


class BaselineStore(object):
    def __init__(self, store):
        self.store = store
        self.dg = diagnose.Diagnoser()

    # ---------------- 采集 ----------------
    def capture(self, device, frame=None, image_path=None, operator="", note=""):
        """
        采集/更新基准图。frame 与 image_path 二选一。
        返回 (ok, 结果说明)
        """
        img = frame
        if img is None and image_path:
            img = diagnose.imread_unicode(image_path)
        if img is None:
            return False, "没有可用画面（既未传帧，也未给有效图片路径）"
        m, _g = self.dg.frame_metrics(img)
        if m is None:
            return False, "画面无法解码"

        name = "%s_baseline.jpg" % device.code
        path = os.path.join(str(config.BASELINE_DIR), name)
        if not diagnose.imwrite_unicode(path, img):
            return False, "基准图写入失败：%s" % path

        rec = {"device_id": device.device_id, "code": device.code, "path": path,
               "captured_at": models.now_ts(), "pan_deg": device.pan_deg,
               "tilt_deg": device.tilt_deg, "sharpness": m["sharpness"],
               "coverage": m["edge_density"], "operator": operator, "note": note}
        self.store.save_baseline(rec)
        device.baseline_at = rec["captured_at"]
        self.store.set_baseline_at(device.device_id, rec["captured_at"])
        self.store.log("采集基准图",
                       "%s 清晰度=%.1f 边缘=%.4f 低纹理=%.3f"
                       % (device.code, m["sharpness"], m["edge_density"],
                          m["uniform_ratio"]), operator or "system")
        return True, {"path": path, "sharpness": round(m["sharpness"], 1),
                      "metrics": m}

    # ---------------- 读取 ----------------
    def get(self, device):
        rec = self.store.get_baseline(device.device_id)
        if not rec:
            return None, None, None
        if not os.path.exists(rec["path"]):
            return rec, None, "基准图文件缺失：%s" % rec["path"]
        gray = diagnose.to_gray(diagnose.imread_unicode(rec["path"]))
        if gray is None:
            return rec, None, "基准图无法解码"
        # 基准指标：优先复用采集时存的，缺项则现算
        m, _ = self.dg.frame_metrics(diagnose.imread_unicode(rec["path"]))
        return rec, gray, m

    def missing_reason(self, device):
        rec = self.store.get_baseline(device.device_id)
        if not rec:
            return "尚未采集基准图（部署完成后需逐机位采集）"
        if not os.path.exists(rec["path"]):
            return "基准图文件丢失：%s" % rec["path"]
        return ""

    def coverage_map_ok(self, device):
        """覆盖完整性：是否已登记基准（有基准才谈得上"核查覆盖"）"""
        rec = self.store.get_baseline(device.device_id)
        return bool(rec and os.path.exists(rec["path"]))

    # ---------------- 现场抓帧（供比对与留档） ----------------
    def grab(self, device, timeout_sec=6.0):
        """
        抓一帧现场画面。返回 (ok, frame, reason)
        没有可用取流地址时明确失败（自检/演示可传入本地图片路径作为 url）。
        """
        url = probe.normalize_url(device.url)
        if not device.url:
            return False, None, "未配置取流地址"
        if isinstance(url, int):
            ok, frame, why = probe.probe_stream(url, timeout_sec=timeout_sec)
        elif os.path.exists(str(url)):
            img = diagnose.imread_unicode(url)
            return (True, img, "读取本地文件") if img is not None else \
                (False, None, "本地文件无法解码：%s" % url)
        else:
            ok, frame, why = probe.probe_stream(url, timeout_sec=timeout_sec)
        return ok, frame, why

    def archive_capture(self, device, frame, tag=""):
        """抓帧留档（逐机位核查报告的对比图素材）"""
        ts = time.strftime("%Y%m%d_%H%M%S")
        name = "%s_%s%s.jpg" % (device.code, ts, ("_" + tag) if tag else "")
        path = os.path.join(str(config.CAPTURE_DIR), name)
        return path if diagnose.imwrite_unicode(path, frame) else ""
