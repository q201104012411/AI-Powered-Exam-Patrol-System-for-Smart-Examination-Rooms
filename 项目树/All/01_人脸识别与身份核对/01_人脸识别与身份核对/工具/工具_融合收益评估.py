# -*- coding: utf-8 -*-
"""多帧特征融合收益评估（降低"本人被判不通过"）。

模型：现场每帧特征 = 真实身份方向 + 各帧独立噪声。
单帧判定：拿某一帧直接比对 -> 低于阈值即"拒识"。
多帧融合：按画质加权平均若干帧后再比对。

统计两种做法的拒识率（FRR），以及"他人被误放行"（冒认）的风险是否上升。

运行：python 工具_融合收益评估.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, ".")
import config
from face_index import FaceIndex

DIM = 512
TRIALS = 3000
FRAMES = 5
THR = config.THRESHOLD_1TO1
rng = np.random.default_rng(20261013)


def make_frame(true_dir, noise_level, ortho):
    """一帧的特征：真身份 + 该帧独立噪声，再归一化。

    ortho 的每一**列**是一个与真身份正交的方向。
    """
    k = int(rng.integers(0, ortho.shape[1]))
    v = true_dir + noise_level * ortho[:, k]
    return v / np.linalg.norm(v)


def main():
    # 固定一组与真身份正交的方向，作为"每帧不同的噪声方向"
    base = np.zeros((DIM, 8), dtype=np.float32)
    for i in range(8):
        base[i, i] = 1.0
    true_dir = np.zeros(DIM, dtype=np.float32)
    true_dir[100] = 1.0
    ortho = base

    single_reject = 0
    fused_reject = 0
    single_accept_other = 0
    fused_accept_other = 0

    for _ in range(TRIALS):
        # 同一个人的 5 帧，噪声强度随机（模拟"有的清晰、有的糊/侧脸"）。
        # 噪声 ~1.52 时单帧余弦正好落在阈值 0.55 附近，所以取 0.8~2.6
        # 让单帧结果跨越阈值，才能看出融合的效果。
        levels = rng.uniform(0.8, 2.6, size=FRAMES).astype(np.float32)
        embs = [make_frame(true_dir, lv, ortho) for lv in levels]

        # ---- 单帧：取"最清晰"那一帧（噪声最小）----
        best = embs[int(np.argmin(levels))]
        s_single = float(np.dot(best, true_dir))
        if s_single < THR:
            single_reject += 1

        # ---- 多帧融合：按 1/噪声 加权（噪声越小权重越高）----
        w = (1.0 / np.maximum(levels, 1e-3))
        fused = FaceIndex.fuse(embs, w)
        s_fused = float(np.dot(fused, true_dir))
        if s_fused < THR:
            fused_reject += 1

        # ---- 冒认风险：拿这 5 帧去比一个"别人"的方向（正交）----
        other = np.zeros(DIM, dtype=np.float32)
        other[200] = 1.0
        if float(np.dot(best, other)) >= THR:
            single_accept_other += 1
        if float(np.dot(fused, other)) >= THR:
            fused_accept_other += 1

    print("阈值 THRESHOLD_1TO1 = %.2f，每种做法模拟 %d 次（每次 %d 帧）"
          % (THR, TRIALS, FRAMES))
    print("-" * 68)
    print("本人被拒率（FRR，越低越好）")
    print("  单帧（取最清晰帧）: %6.2f%%" % (single_reject / TRIALS * 100))
    print("  多帧加权融合      : %6.2f%%" % (fused_reject / TRIALS * 100))
    print("")
    print("他人被误放行（应保持 0，融合不得提高冒认风险）")
    print("  单帧              : %6.2f%%" % (single_accept_other / TRIALS * 100))
    print("  多帧加权融合      : %6.2f%%" % (fused_accept_other / TRIALS * 100))
    print("-" * 68)


if __name__ == "__main__":
    main()
