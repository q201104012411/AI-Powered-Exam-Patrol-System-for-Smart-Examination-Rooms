# -*- coding: utf-8 -*-
"""诊断：新关联逻辑下"同一目标累计换过多个编号"的来源。"""
import random
import sys

import numpy as np

sys.path.insert(0, ".")
import person_detect as pd

random.seed(7)
np.random.seed(7)

N, FRAMES, W, H = 20, 60, 416, 234
truth = []
for i in range(N):
    r, c = divmod(i, 5)
    truth.append([40 + c * 80, 40 + r * 45, 26, 40])
truth = np.array(truth, dtype=float)


def gen_detections(frame_idx, noise=2.5, dup_p=0.25, spurious=2):
    t = truth.copy()
    idx = np.arange(N)
    t[:, 0] += np.sin(frame_idx / 8.0 + idx) * 6
    t[:, 1] += np.cos(frame_idx / 10.0 + idx) * 3
    t[:, 0] += np.random.randn(N) * noise
    t[:, 1] += np.random.randn(N) * noise
    dets = [tuple(int(v) for v in row) for row in t]
    for i in range(N):
        if random.random() < dup_p:
            x, y, w, h = dets[i]
            dets.append((x + random.randint(-6, 6), y + random.randint(-6, 6), w, h))
    for _ in range(spurious):
        dets.append((random.randint(0, W - 40), random.randint(0, H - 50), 30, 45))
    return dets


def main():
    tr = pd.PersonTracker(max_input_dim=1280, dense_mode=False, min_hits=2)
    created_then_merged = 0
    confirmed_seen = {}
    for f in range(FRAMES):
        dets = gen_detections(f)
        tr._detect = lambda frame, d=dets: d
        before = set(tr._tracks.keys())
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        tr.update(frame, boundary_ratio=0.92)
        after = set(tr._tracks.keys())
        # 本帧新建、且在同帧内就被合并掉的轨迹
        created_then_merged += len((after - before) - after)
        for pid, t2 in tr.confirmed_tracks().items():
            confirmed_seen.setdefault(pid, 0)
            confirmed_seen[pid] += 1
    print("累计创建轨迹编号（_next_pid-1）: %d" % (tr._next_pid - 1))
    print("曾进入 confirmed_tracks 的编号数: %d" % len(confirmed_seen))
    live = sum(1 for v in confirmed_seen.values() if v >= 5)
    short = sum(1 for v in confirmed_seen.values() if v < 5)
    print("  其中存活 >=5 帧的（稳定目标）: %d" % live)
    print("  其中存活 <5 帧的（瞬态/重复框）: %d" % short)
    print("真值目标数: %d" % N)


if __name__ == "__main__":
    main()
