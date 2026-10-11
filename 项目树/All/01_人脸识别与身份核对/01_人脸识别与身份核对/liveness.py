# -*- coding: utf-8 -*-
"""
现场性（活体）判定。

⚠️ 诚实声明：本作品**尚未实现**真正的活体防伪（翻拍照片/屏幕回放/面具攻击检测）。
   因此这里不假装"活体检测通过"，而是做两件事：
   1. 用"设备实时抓拍令牌"判定图像是否来自现场采集（时间窗 + 设备一致性 + 随机数）；
   2. 留出可插拔接口 set_checker()，后续接入真实活体模型即可生效。

严格模式（config.REQUIRE_LIVENESS=1）下：无法证明来自现场采集的图像一律**拒绝放行**，
而不是像旧版那样拿参考照比对也能判 PASS。
"""
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import config

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_CHECKED = "NOT_CHECKED"


@dataclass
class LivenessResult:
    status: str
    detail: str = ""


_custom_checker: Optional[Callable] = None


def set_checker(fn: Optional[Callable]):
    """接入真实活体模型：fn(image, token) -> LivenessResult"""
    global _custom_checker
    _custom_checker = fn


def make_token(device_id: str, source: str = "camera") -> dict:
    """采集端生成现场令牌（由 capture.py 在抓拍成功后调用）"""
    return {
        "device_id": device_id,
        "source": source,
        "ts": time.time(),
        "nonce": uuid.uuid4().hex,
    }


def check(token: Optional[dict], device_id: str, image=None) -> LivenessResult:
    if _custom_checker is not None:
        try:
            r = _custom_checker(image, token)
            if isinstance(r, LivenessResult):
                return r
        except Exception as e:
            return LivenessResult(STATUS_FAIL, "活体模型异常：%s" % str(e)[:60])

    if not token:
        return LivenessResult(STATUS_UNKNOWN, "无现场抓拍令牌（图像来自文件选择，无法证明为实时采集）")
    if token.get("source") != "camera":
        return LivenessResult(STATUS_UNKNOWN, "令牌来源非摄像头采集")
    if token.get("device_id") != device_id:
        return LivenessResult(STATUS_UNKNOWN, "抓拍设备(%s)与当前设备(%s)不一致"
                              % (token.get("device_id"), device_id))
    age = time.time() - float(token.get("ts", 0))
    if age < 0 or age > config.CAPTURE_MAX_AGE_SEC:
        return LivenessResult(STATUS_UNKNOWN, "抓拍已超过有效期（%.0f 秒 > %d 秒）"
                              % (age, config.CAPTURE_MAX_AGE_SEC))
    return LivenessResult(STATUS_PASS, "现场抓拍在有效期内（%.0f 秒）" % age)


def capture_age(token: Optional[dict]):
    if not token:
        return None
    try:
        return int(time.time() - float(token.get("ts", 0)))
    except (TypeError, ValueError):
        return None
