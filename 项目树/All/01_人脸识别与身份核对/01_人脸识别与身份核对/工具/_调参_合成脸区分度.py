r"""
为合成人脸生成器寻找合适的几何变化幅度。

结论（2026-10-13 实测，本脚本保留作为依据）：
  把脸型/五官参数范围放宽**并不能**让合成脸在 InsightFace 特征下真正分开，
  反而会先破坏人脸可检出性：

    | 配置          | 检出率 | 平均库内相似度 |
    | 温和(原版范围) |   7%%   | 0.815          |
    | 中等          |  12%%   | 0.632          |
    | 较大          |  10%%   | 0.616          |

  说明：用椭圆+线段画出来的"脸"只在很窄的几何区间内能被检测器认成脸；
  一旦超出这个区间就 NO_FACE。因此合成数据天然会形成一个高相似度簇，
  它适合用来压测列表/检索性能，**不适合**用来演示"本人通过、他人拦截"。
  要让 01 完整可用，请使用真实照片建档。

运行：python 工具\\_调参_合成脸区分度.py
"""
import importlib.util
import random
import sys

import numpy as np

sys.path.insert(0, ".")
import cv2                                              # noqa: E402
import face_engine                                      # noqa: E402

spec = importlib.util.spec_from_file_location("gen", "工具/生成测试考生.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def draw(rng, cfg, w=480, h=640):
    """按给定幅度配置画一张合成脸（cfg 控制各参数的变化范围）"""
    bg = (rng.randint(190, 235), rng.randint(200, 240), rng.randint(215, 250))
    img = np.full((h, w, 3), bg, np.uint8)
    cx, cy = w // 2, int(h * 0.46)
    fw = int(w * rng.uniform(*cfg["fw"]))
    fh = int(fw * rng.uniform(*cfg["ar"]))
    skin = (rng.randint(140, 210), rng.randint(165, 228), rng.randint(190, 245))

    cv2.rectangle(img, (cx - fw // 2, cy + fh - 10), (cx + fw // 2, cy + fh + 60),
                  tuple(int(c * 0.92) for c in skin), -1)
    cv2.ellipse(img, (cx, cy + fh + 130), (int(fw * 1.9), int(fh * 0.95)), 0, 180, 360,
                (rng.randint(50, 110), rng.randint(55, 115), rng.randint(60, 120)), -1)
    hair = (rng.randint(20, 70), rng.randint(20, 70), rng.randint(20, 75))
    cv2.ellipse(img, (cx, cy - int(fh * rng.uniform(0.02, 0.26))),
                (int(fw * rng.uniform(1.02, 1.30)), int(fh * rng.uniform(0.92, 1.22))),
                0, 0, 360, hair, -1)
    tilt = rng.uniform(*cfg["tilt"])
    cv2.ellipse(img, (cx, cy), (fw, fh), tilt, 0, 360, skin, -1)
    cv2.ellipse(img, (cx, cy + int(fh * rng.uniform(0.18, 0.42))),
                (int(fw * rng.uniform(0.70, 0.95)), int(fh * rng.uniform(0.58, 0.88))),
                tilt, 0, 360, skin, -1)

    eye_dx = int(fw * rng.uniform(*cfg["eye_dx"]))
    brow_y = cy - int(fh * rng.uniform(*cfg["brow_y"]))
    for sgn in (-1, 1):
        cv2.ellipse(img, (cx + sgn * eye_dx, brow_y),
                    (int(fw * rng.uniform(0.16, 0.34)), int(fh * rng.uniform(0.03, 0.07))),
                    sgn * rng.randint(-12, 12), 180, 360, hair, max(3, fw // 16))
    eye_r = max(4, int(fw * rng.uniform(*cfg["eye_r"])))
    eye_y = cy - int(fh * rng.uniform(*cfg["eye_y"]))
    for sgn in (-1, 1):
        ex = cx + sgn * eye_dx
        cv2.ellipse(img, (ex, eye_y), (eye_r, int(eye_r * rng.uniform(0.55, 0.80))),
                    0, 0, 360, (245, 245, 245), -1)
        cv2.circle(img, (ex, eye_y), int(eye_r * 0.46),
                   rng.choice([(70, 55, 40), (60, 80, 90), (45, 60, 75)]), -1)
        cv2.circle(img, (ex, eye_y), int(eye_r * 0.20), (18, 18, 18), -1)

    nose_len = int(fh * rng.uniform(*cfg["nose"]))
    nose_w = max(2, int(fw * rng.uniform(0.05, 0.18)))
    cv2.line(img, (cx, eye_y + int(fh * 0.06)), (cx - nose_w, eye_y + nose_len),
             tuple(int(c * 0.86) for c in skin), max(2, fw // 26))
    cv2.line(img, (cx, eye_y + int(fh * 0.06)), (cx + nose_w, eye_y + nose_len),
             tuple(int(c * 0.86) for c in skin), max(2, fw // 26))
    mouth_y = eye_y + int(fh * rng.uniform(*cfg["mouth_y"]))
    mw = max(4, int(fw * rng.uniform(*cfg["mouth_w"])))
    cv2.ellipse(img, (cx, mouth_y), (mw, int(fh * rng.uniform(0.04, 0.11))), 0, 0, 360,
                (rng.randint(90, 160), rng.randint(110, 175), rng.randint(150, 210)), -1)

    ang = rng.uniform(*cfg["warp_ang"])
    sx = rng.uniform(*cfg["warp_sx"])
    sy = rng.uniform(*cfg["warp_sy"])
    shear = rng.uniform(*cfg["warp_shear"])
    m = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    m[0, 0] *= sx
    m[0, 1] *= sx
    m[1, 0] *= sy
    m[1, 1] *= sy
    m[0, 0] += shear * m[1, 0]
    m[0, 1] += shear * m[1, 1]
    m[0, 2] += cx - (m[0, 0] * cx + m[0, 1] * cy)
    m[1, 2] += cy - (m[1, 0] * cx + m[1, 1] * cy)
    img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(1.0, 1.6))
    img = img.astype(np.uint8)
    return img


# 三套候选配置：温和（贴近原版）/ 中等 / 激进
CONFIGS = {
    "温和(原版范围)": dict(
        fw=(0.30, 0.36), ar=(1.22, 1.36), tilt=(-2, 2),
        eye_dx=(0.42, 0.50), brow_y=(0.30, 0.38), eye_r=(0.15, 0.19), eye_y=(0.14, 0.22),
        nose=(0.24, 0.32), mouth_y=(0.42, 0.50), mouth_w=(0.32, 0.44),
        warp_ang=(-3, 3), warp_sx=(0.96, 1.04), warp_sy=(0.96, 1.04), warp_shear=(-0.03, 0.03)),
    "中等": dict(
        fw=(0.27, 0.40), ar=(1.08, 1.48), tilt=(-5, 5),
        eye_dx=(0.34, 0.58), brow_y=(0.24, 0.44), eye_r=(0.11, 0.23), eye_y=(0.08, 0.28),
        nose=(0.18, 0.38), mouth_y=(0.34, 0.58), mouth_w=(0.22, 0.52),
        warp_ang=(-9, 9), warp_sx=(0.88, 1.14), warp_sy=(0.88, 1.14), warp_shear=(-0.10, 0.10)),
    "较大": dict(
        fw=(0.25, 0.42), ar=(1.02, 1.56), tilt=(-7, 7),
        eye_dx=(0.30, 0.62), brow_y=(0.22, 0.46), eye_r=(0.10, 0.25), eye_y=(0.06, 0.32),
        nose=(0.14, 0.42), mouth_y=(0.30, 0.62), mouth_w=(0.20, 0.56),
        warp_ang=(-11, 11), warp_sx=(0.84, 1.18), warp_sy=(0.84, 1.18), warp_shear=(-0.13, 0.13)),
}


def evaluate(cfg, n=40, seed=20261011):
    rng = random.Random(seed)
    embs, fail = [], 0
    for _ in range(n):
        img = draw(rng, cfg)
        try:
            e, _ = face_engine.extract(img)
            embs.append(np.asarray(e, dtype=np.float32))
        except Exception:
            fail += 1
    if len(embs) < 3:
        return dict(detect=0.0, mean=None, maxsim=None, above=0)
    E = np.vstack(embs)
    m = E @ E.T
    iu = np.triu_indices(len(embs), 1)
    s = m[iu]
    return dict(detect=1.0 - fail / float(n), mean=float(s.mean()),
                maxsim=float(s.max()), above=int((s >= 0.45).sum()), pairs=len(s))


def main():
    print("%-16s %8s %8s %8s %14s" % ("配置", "检出率", "均相似", "最大相似", ">=0.45 对数"))
    print("-" * 62)
    for name, cfg in CONFIGS.items():
        r = evaluate(cfg)
        if r["mean"] is None:
            print("%-16s %7.0f%%   样本不足" % (name, r["detect"] * 100))
            continue
        print("%-16s %7.0f%% %8.3f %8.3f %6d/%d"
              % (name, r["detect"] * 100, r["mean"], r["maxsim"], r["above"], r["pairs"]))
    print("-" * 62)


if __name__ == "__main__":
    main()
