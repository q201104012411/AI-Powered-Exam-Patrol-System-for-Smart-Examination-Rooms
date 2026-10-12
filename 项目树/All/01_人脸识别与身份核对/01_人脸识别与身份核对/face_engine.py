# -*- coding: utf-8 -*-
"""
人脸检测与特征提取（InsightFace buffalo_l / w600k_r50）。

与旧版的关键差异：
1. 相似度**直接返回原始余弦值**，不再做 (cos+1)/2 映射——旧版映射后再套 0.65 阈值，
   等效于原始余弦 0.30 就放行，是造成误识的直接原因；
2. 检测失败/多张人脸/画质不足分别抛出带原因码的异常，便于可解释输出；
3. 不再"默默取最大人脸"——闸机单人核验场景下，画面出现多张脸必须显式拒绝。
"""
import os
import time

import numpy as np

from models import (
    RC_IMAGE_ERROR, RC_LOW_QUALITY, RC_MULTI_FACE, RC_NO_FACE,
)

_APP = None
_APP_DET_SIZE = None


class FaceError(Exception):
    """带原因码的人脸处理异常"""

    def __init__(self, code, msg=""):
        super().__init__(msg or code)
        self.code = code
        self.msg = msg or code


def _providers():
    """推理后端：交给 edge 层按 Atlas(CANN)/Jetson(TensorRT)/CUDA/OpenVINO/CPU 优先级选择"""
    import edge
    return edge.detect_providers()["providers"]


def active_provider():
    import edge
    info = edge.detect_providers()
    return info["active"], info["label"]


def get_app(det_size=None):
    """
    全局单例：避免每次核验都重新加载模型。
    检测分辨率由边缘档位决定（过载降档时会自动用更小的 det_size 重新 prepare）。
    """
    global _APP, _APP_DET_SIZE
    import edge
    want = int(det_size or edge.monitor().profile.det_size)
    if _APP is None:
        from insightface.app import FaceAnalysis
        provs = _providers()
        _APP = FaceAnalysis(name="buffalo_l", providers=provs,
                            allowed_modules=["detection", "recognition"])
        _APP.prepare(ctx_id=0 if "CPUExecutionProvider" not in provs else -1,
                     det_size=(want, want))
        _APP_DET_SIZE = want
    elif _APP_DET_SIZE != want:                  # 边缘降档 -> 用更小分辨率重新准备
        _APP.prepare(ctx_id=0 if "CPUExecutionProvider" not in _providers() else -1,
                     det_size=(want, want))
        _APP_DET_SIZE = want
    return _APP


def imread_unicode(path):
    """
    读取图片（解决 Windows 中文路径问题）。
    cv2.imread 内部使用 ANSI 接口，路径含中文时返回 None——
    本项目路径含"智能考场AI巡考系统/项目树/人脸识别与身份核对"，
    必须走 np.fromfile + cv2.imdecode 才能稳定读图。

    若文件内容以 "enc:" 开头，则先调用 data_crypto 解密（支持参考照加密存储）。
    """
    import cv2
    raw = np.fromfile(str(path), dtype=np.uint8)
    if raw.size == 0:
        return None
    # 判断是否加密文件：前 4 字节为 "enc:" 的 UTF-8
    buf = raw
    if raw.size >= 4:
        header = raw[:4].tobytes()
        if header == b"enc:":
            try:
                from data_crypto import decrypt_bytes
                plain = decrypt_bytes(raw.tobytes().decode("utf-8"))
                buf = np.frombuffer(plain, dtype=np.uint8)
            except Exception:
                # 解密失败时仍尝试按原图解码，避免误伤非加密但碰巧以 enc: 开头的文件
                buf = raw
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def imwrite_unicode(path, img, quality=95, encrypt=False):
    """写图片（同样规避中文路径问题）。encrypt=True 时用 data_crypto 加密后写入。"""
    import cv2
    ext = os.path.splitext(str(path))[1].lower() or ".jpg"
    params = []
    if ext in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        return False
    data = buf.tobytes()
    if encrypt:
        try:
            from data_crypto import encrypt_bytes
            data = encrypt_bytes(data).encode("utf-8")
        except Exception as e:
            raise FaceError(RC_IMAGE_ERROR, "参考照加密写入失败：%s" % e)
    try:
        with open(str(path), "wb") as f:
            f.write(data)
    except Exception as e:
        raise FaceError(RC_IMAGE_ERROR, "图片写入失败：%s" % e)
    return True


def read_image(path_or_array):
    """读取图像：支持路径或 OpenCV BGR 数组"""
    if isinstance(path_or_array, (str, bytes, os.PathLike)):
        if not os.path.exists(str(path_or_array)):
            raise FaceError(RC_IMAGE_ERROR, "文件不存在：%s" % path_or_array)
        img = imread_unicode(path_or_array)
    else:
        img = path_or_array
    if img is None or getattr(img, "size", 0) == 0:
        raise FaceError(RC_IMAGE_ERROR, "图像无法解码或为空")
    return img


def detect(img):
    """返回人脸列表（原始检测结果）"""
    return get_app().get(img)


def _largest(faces):
    return max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))


_SEV_ORDER = {"ok": 0, "warn": 1, "severe": 2}


def _better(rep_new, rep_old):
    """增强后是否更好：先比严重程度，再比问题条数"""
    if _SEV_ORDER.get(rep_new.severity, 1) != _SEV_ORDER.get(rep_old.severity, 1):
        return _SEV_ORDER.get(rep_new.severity, 1) < _SEV_ORDER.get(rep_old.severity, 1)
    return len(rep_new.issues) < len(rep_old.issues)


def extract(img, allow_multi=False, auto_enhance=None, min_size=None, min_det=None):
    """
    单人取特征 + 画质/姿态评估（异常状态处理的入口）。

    返回 (embedding(512,), info)；info 含：
        quality   : quality.QualityReport（亮度/清晰度/人脸占比/近似姿态/问题清单/分级）
        enhanced  : 是否先做了低画质增强
        det_score : 人脸检测置信度
        face_count: 画面中检出的人脸数
        bbox      : 人脸框

    与旧版的差别（针对极端情况）：
      · 昏暗/模糊导致检不到人脸时，先增强再检测，并比较增强前后哪个更好；
      · 人脸偏小、置信度偏低不再直接抛错，而是记入画质报告，
        由核验器决定"转人工复验"（旧版直接 FAIL，容易冤枉考生）；
      · 只有"没人脸 / 多张脸 / 图读不出来"才是硬错误。
    """
    import config
    import edge
    import quality

    if isinstance(img, (str, bytes, os.PathLike)):
        img = read_image(img)

    prof = edge.monitor().profile                     # 边缘档位（full / balanced / lite）
    if auto_enhance is None:
        auto_enhance = bool(config.AUTO_ENHANCE and prof.auto_enhance)
    min_size = config.MIN_FACE_SIZE if min_size is None else min_size
    min_det = 0.5 if min_det is None else min_det

    t0 = time.perf_counter()
    used, enhanced = img, False
    faces = detect(used)

    if not faces and auto_enhance:                      # 太暗/太糊导致检不到 -> 增强后重试
        cand = quality.enhance(img)
        faces2 = detect(cand)
        if faces2:
            used, faces, enhanced = cand, faces2, True

    if not faces:
        raise FaceError(RC_NO_FACE, "未检测到人脸（已尝试低画质增强仍失败）")
    if len(faces) > 1 and not allow_multi:
        raise FaceError(RC_MULTI_FACE, "检测到 %d 张人脸，闸机核验仅接受单人画面" % len(faces))

    face = _largest(faces)
    kps = getattr(face, "kps", None)
    # 只评估人脸区域（不再做整帧拉普拉斯，省 ~10~40ms/次，让结果更快）
    rep_raw = quality.assess(img, face.bbox, kps)       # 原图质量，用于对照解释
    rep = quality.assess(used, face.bbox, kps)

    # 画质有问题时再增强一次，取更优的结果
    if auto_enhance and not enhanced and rep.issues:
        cand = quality.enhance(img)
        faces2 = detect(cand)
        if faces2:
            face2 = _largest(faces2)
            rep2 = quality.assess(cand, face2.bbox, getattr(face2, "kps", None))
            if _better(rep2, rep):
                used, face, rep, enhanced = cand, face2, rep2, True

    w = float(face.bbox[2] - face.bbox[0])
    h = float(face.bbox[3] - face.bbox[1])
    det_score = float(getattr(face, "det_score", 1.0))

    # 尺寸/置信度不足 -> 记入报告（交由核验器转人工复验），不再直接判死
    if min(w, h) < min_size:
        rep.issues.append("人脸过小(%.0f×%.0f)" % (w, h))
        level = "severe" if min(w, h) < min_size * 0.6 else "warn"
        if _SEV_ORDER[level] > _SEV_ORDER[rep.severity]:
            rep.severity = level
    if det_score < min_det:
        rep.issues.append("检测置信度偏低(%.2f)" % det_score)
        if det_score < min_det * 0.8:
            rep.severity = "severe"

    emb = np.asarray(face.normed_embedding, dtype=np.float32)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    mon = edge.monitor()
    mon.observe(latency_ms)                            # 实测耗时驱动边缘档位自适应
    provider, _label = active_provider()
    info = {
        "quality": rep,                 # 实际用于判定的图像质量（可能是增强后的）
        "quality_raw": rep_raw,         # 原图质量，用于解释"原图昏暗→已增强"
        "enhanced": enhanced,
        "det_score": det_score,
        "face_count": len(faces),
        "bbox": [float(v) for v in face.bbox],
        "edge": {
            "provider": provider,
            "profile": mon.profile_name,
            "degraded": mon.degraded,
            "latency_ms": round(latency_ms, 1),
        },
    }
    return emb, info


def embed_largest(img, allow_multi=False, min_size=None, min_det=None):
    """
    单人核验取特征（兼容旧调用）：返回 (embedding(512,), info)
    入参可以是图片路径，也可以是 OpenCV BGR 数组。
    异常：FaceError(code) —— NO_FACE / MULTI_FACE / IMAGE_ERROR
    """
    return extract(img, allow_multi=allow_multi, min_size=min_size, min_det=min_det)


def default_embedder(img):
    """供 FaceIndex 使用的默认可调用对象"""
    return embed_largest(img)[0]


def cosine(a, b):
    """原始余弦相似度（输入为 L2 归一化向量时等价于点积）"""
    return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32).T))


def sharpness(img):
    """清晰度（拉普拉斯方差），用于从抓拍的多帧里挑最清晰的一张"""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())
