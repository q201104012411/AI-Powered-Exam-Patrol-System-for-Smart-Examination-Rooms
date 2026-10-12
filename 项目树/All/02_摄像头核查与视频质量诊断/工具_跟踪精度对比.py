# -*- coding: utf-8 -*-
"""跟踪框架精度对比脚本（旧关联逻辑 vs 新关联逻辑）。

用合成真值轨迹 + 带抖动/重复框/虚假框的检测输出，模拟 HOG 在密集考场的真实表现，
衡量两件事：
  1. 人数估计误差与波动（用户反馈"人数不准"）
  2. ID 切换次数（编号乱跳）

运行：python 工具_跟踪精度对比.py
"""
import random
import sys

import numpy as np

sys.path.insert(0, ".")
import person_detect as pd

random.seed(7)
np.random.seed(7)

N, FRAMES, W, H = 20, 60, 416, 234
# 归属半径：真值目标在帧间移动幅度有限，取 15px 可把随机虚假框排除在 ID 统计外
_ASSIGN_RADIUS = 15.0

# 20 个"考生"的真值位置（4 行 5 列）
truth = []
for i in range(N):
    r, c = divmod(i, 5)
    truth.append([40 + c * 80, 40 + r * 45, 26, 40])
truth = np.array(truth, dtype=float)


def gen_detections(frame_idx, noise=2.5, dup_p=0.25, spurious=2):
    """带抖动 + 重复框 + 虚假框的检测输出"""
    t = truth.copy()
    idx = np.arange(N)
    t[:, 0] += np.sin(frame_idx / 8.0 + idx) * 6
    t[:, 1] += np.cos(frame_idx / 10.0 + idx) * 3
    t[:, 0] += np.random.randn(N) * noise
    t[:, 1] += np.random.randn(N) * noise
    dets = [tuple(int(v) for v in row) for row in t]
    # 同一个人被检出两个略有偏移的框（HOG 常见）
    for i in range(N):
        if random.random() < dup_p:
            x, y, w, h = dets[i]
            dets.append((x + random.randint(-6, 6), y + random.randint(-6, 6), w, h))
    # 虚假框（反光/桌椅误检）
    for _ in range(spurious):
        dets.append((random.randint(0, W - 40), random.randint(0, H - 50), 30, 45))
    return dets


def _legacy_match(tr):
    """还原旧行为：固定 90px 纯质心最近邻，不做重复合并"""
    def _m(rects, line_y, frame_w=None):
        cents = [pd._centroid(r) for r in rects]
        tracks = list(tr._tracks.items())
        used = set()
        for pid, t2 in tracks:
            best, bd = None, 90
            tcx, tcy = pd._centroid(t2.rect)
            for j, (cx, cy) in enumerate(cents):
                if j in used:
                    continue
                d = ((cx - tcx) ** 2 + (cy - tcy) ** 2) ** 0.5
                if d < bd:
                    best, bd = j, d
            if best is not None:
                used.add(best)
                t2.rect = rects[best]
                t2.missed = 0
                t2.hits += 1
        for j, r in enumerate(rects):
            if j in used:
                continue
            nt = pd._Track(tr._next_pid, r, line_y)
            nt.hits = 1
            tr._tracks[tr._next_pid] = nt
            tr._next_pid += 1
    return _m


def _assign_truth(tracks, truth):
    """贪心一对一：把轨迹质心配到真值目标，返回 {目标序号: pid}。

    必须一对一，否则多条轨迹会挤到同一个目标上，ID 统计就失真了。
    """
    items = []
    for pid, t2 in tracks.items():
        cx, cy = pd._centroid(t2.rect)
        for j in range(len(truth)):
            d = ((truth[j, 0] + truth[j, 2] / 2 - cx) ** 2
                 + (truth[j, 1] + truth[j, 3] / 2 - cy) ** 2) ** 0.5
            items.append((d, j, pid))
    items.sort()
    used_pid, used_t = set(), set()
    out = {}
    for d, j, pid in items:
        if d > _ASSIGN_RADIUS:        # 太远的不算归属（避免虚假框污染统计）
            break
        if pid in used_pid or j in used_t:
            continue
        used_pid.add(pid)
        used_t.add(j)
        out[j] = pid
    return out


def run(legacy):
    tr = pd.PersonTracker(max_input_dim=1280, dense_mode=False, min_hits=2)
    if legacy:
        tr.min_hits = 1
        tr._merge_duplicate_tracks = lambda *a, **k: None
        tr._match_and_update = _legacy_match(tr)

    counts, switches = [], 0
    # 每个真值目标历史上出现过的 pid 序列
    history = {}
    last_pid = {}
    for f in range(FRAMES):
        dets = gen_detections(f)
        tr._detect = lambda frame, d=dets: d
        frame = np.zeros((H, W, 3), dtype=np.uint8)
        tr.update(frame, boundary_ratio=0.92)
        tracks = tr.confirmed_tracks()
        counts.append(len(tracks))
        cur = _assign_truth(tracks, truth)
        for j, pid in cur.items():
            seen = history.setdefault(j, set())
            seen.add(pid)
            # 同一目标换了新的 pid（且旧 pid 已不再归属它）记一次切换
            if j in last_pid and last_pid[j] != pid:
                switches += 1
            last_pid[j] = pid
    # 每个目标一共用过几个不同 pid
    churn = sum(max(0, len(s) - 1) for s in history.values())
    return counts, switches, churn


def main():
    print("场景：%d 人，%d 帧；检测含 ±2.5px 抖动、25%% 重复框、每帧 2 个虚假框"
          % (N, FRAMES))
    print("-" * 78)
    for name, legacy in (("旧关联逻辑", True), ("新关联逻辑", False)):
        counts, sw, churn = run(legacy)
        tail = counts[10:]           # 跳过前 10 帧的建立期
        avg = sum(tail) / len(tail)
        err = (avg - N) / float(N) * 100.0
        print("%s：平均人数 %.1f（真值 %d，偏差 %+.0f%%）｜人数区间 %d~%d"
              % (name, avg, N, err, min(tail), max(tail)))
        print("%s  ID 切换 %d 次｜同一目标累计换过 %d 个编号"
              % (" " * len(name), sw, churn))
    print("-" * 78)


if __name__ == "__main__":
    main()
