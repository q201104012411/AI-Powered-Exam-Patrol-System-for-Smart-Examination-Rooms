#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_calib_diagrams.py —— 生成"标定示意图"（俯视示意 + 尺寸标注）

布局原则：固定画布 → 标题带 / 图形面板 / 注释带 各自占独立区域，不重叠。

输出到 标定图示/：
  01_路线标定.png        路线距离、转角、打卡点怎么量
  02_圆弧转弯标定.png    阿克曼圆弧：弦长→半径；三点调头画法
  03_里程标定.png        1m 直线里程标定 + 比例误差计算
  04_雷达扇区标定.png    避障扇区、补偿角 0/180、四向净空
"""
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle, Wedge
from matplotlib.lines import Line2D

OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "标定图示"))
os.makedirs(OUT, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP", "Droid Sans Fallback"]
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

BG = "#fbfcfe"
C_WALL = "#9aa7b6"
C_FLOOR = "#eaeff6"
C_CAR = "#2f6fed"
C_DIM = "#a05a12"
C_OK = "#1a7f37"
C_BAD = "#c62828"
C_TXT = "#111827"
C_GRAY = "#6b7280"
C_BLUE2 = "#0b5cad"


# ----------------------------------------------------------------- 基础件
def new_fig(w, h):
    fig = plt.figure(figsize=(w, h))
    fig.patch.set_facecolor(BG)
    return fig


def head(fig, title_text, sub, y_title=0.962, y_sub=0.918):
    fig.text(0.022, y_title, title_text, fontsize=21, fontweight="bold",
             color=C_TXT, ha="left", va="center")
    fig.text(0.022, y_sub, sub, fontsize=11.5, color=C_GRAY, ha="left", va="center")


def panel(fig, rect, xlim, ylim, equal=True, tag=None):
    ax = fig.add_axes(rect)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.axis("off")
    if equal:
        ax.set_aspect("equal")
    if tag:
        ax.text(xlim[0], ylim[1], tag, fontsize=12.5, fontweight="bold",
                color=C_BLUE2, ha="left", va="top")
    return ax


def car(ax, x, y, yaw_deg, L=0.52, W=0.36, color=C_CAR, alpha=1.0, z=6, label=None):
    t = math.radians(yaw_deg)
    ca, sa = math.cos(t), math.sin(t)
    pts = [(x + cx * ca - cy * sa, y + cx * sa + cy * ca)
           for cx, cy in [(-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2)]]
    ax.add_patch(Polygon(pts, closed=True, fc=color, ec="white", lw=1.2, alpha=alpha, zorder=z))
    nose = [(x + (L / 2) * ca, y + (L / 2) * sa),
            (x + (L / 2 - 0.12) * ca - 0.08 * sa, y + (L / 2 - 0.12) * sa + 0.08 * ca),
            (x + (L / 2 - 0.12) * ca + 0.08 * sa, y + (L / 2 - 0.12) * sa - 0.08 * ca)]
    ax.add_patch(Polygon(nose, closed=True, fc="white", ec="none", alpha=0.95, zorder=z + 1))
    if label:
        ax.text(x - 0.26, y + 0.30, label, fontsize=10, color=color, fontweight="bold",
                ha="center", zorder=z + 2)


def dim(ax, p1, p2, text, off=0.0, perp=(0, 1), color=C_DIM, fs=11.5):
    x1, y1 = p1
    x2, y2 = p2
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="<|-|>", color=color, lw=1.5, shrinkA=0, shrinkB=0),
                zorder=9)
    mx = (x1 + x2) / 2.0 + perp[0] * off
    my = (y1 + y2) / 2.0 + perp[1] * off
    ax.text(mx, my, text, fontsize=fs, color=color, ha="center", va="center", fontweight="bold",
            zorder=10, bbox=dict(boxstyle="round,pad=0.28", fc="white", ec=color, lw=1.0))


def box(ax, x, y, text, fs=10.8, color=C_TXT, ec="#cbd5e1", fc="white", ha="left", va="center"):
    ax.text(x, y, text, fontsize=fs, color=color, ha=ha, va=va, zorder=30, linespacing=1.6,
            bbox=dict(boxstyle="round,pad=0.45", fc=fc, ec=ec, lw=1.0, alpha=0.98))


def note_fig(fig, x, y, text, fs=10.8, color=C_TXT, ec="#cbd5e1", fc="white"):
    return fig.text(x, y, text, fontsize=fs, color=color, ha="left", va="top", linespacing=1.65,
                    bbox=dict(boxstyle="round,pad=0.5", fc=fc, ec=ec, lw=1.0))


def arrow(ax, p1, p2, color=C_CAR, lw=2.0, ms=13, ls="-", z=7):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=ms, color=color,
                                 lw=lw, ls=ls, zorder=z, shrinkA=0, shrinkB=0))


def star(ax, x, y, size=170, color=C_BAD):
    ax.scatter([x], [y], marker="*", s=size, color=color, zorder=12, edgecolors="white", lw=1.2)


def walls(ax, pts, lw=7.0):
    ax.add_line(Line2D([p[0] for p in pts], [p[1] for p in pts], color=C_WALL, lw=lw,
                       solid_capstyle="round", solid_joinstyle="round", zorder=3))


# ------------------------------------------------------------- 01 路线标定
def fig_route():
    fig = new_fig(14.0, 8.6)
    head(fig, "① 路线标定：距离 / 转角 / 打卡点怎么量",
         "俯视示意（不按比例）· 距离沿“车体中心线”量，转角量“车头朝向变化”，打卡点量“车头到门”")

    ax = panel(fig, [0.02, 0.12, 0.96, 0.76], (0.0, 14.0), (0.0, 7.0))

    ax.add_patch(Rectangle((0.9, 3.4), 7.6, 2.0, fc=C_FLOOR, ec="none", zorder=1))
    ax.add_patch(Rectangle((8.5, 1.2), 2.0, 4.2, fc=C_FLOOR, ec="none", zorder=1))
    walls(ax, [(0.9, 3.4), (0.9, 5.4), (10.5, 5.4), (10.5, 1.2), (8.5, 1.2), (8.5, 3.4), (0.9, 3.4)])

    car(ax, 1.8, 4.5, 0)
    ax.text(1.55, 5.02, "待机点（每次从这里出发）", fontsize=9.6, color=C_CAR, fontweight="bold")
    arrow(ax, (2.3, 4.5), (3.6, 4.5))
    dim(ax, (1.8, 3.80), (8.4, 3.80), "① forward 2.00 m", off=0.30, fs=11.5)

    ax.add_patch(Wedge((8.6, 4.5), 0.62, 270, 360, width=0.05, fc="none", ec=C_DIM, lw=1.6, zorder=6))
    car(ax, 8.95, 4.02, -50, alpha=0.35)
    ax.annotate("② turn -90°  (右转 90°)", xy=(8.95, 4.12), xytext=(6.15, 6.15), fontsize=11.5,
                color=C_DIM, fontweight="bold", ha="center",
                arrowprops=dict(arrowstyle="->", color=C_DIM, lw=1.4),
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=C_DIM, lw=1.0), zorder=12)

    dim(ax, (9.55, 3.40), (9.55, 1.75), "③ forward 1.50 m", off=0.60, perp=(1, 0), fs=11.5)
    arrow(ax, (9.55, 3.25), (9.55, 2.45))

    star(ax, 9.55, 1.62)
    ax.add_patch(Rectangle((10.62, 1.30), 0.75, 0.62, fc="#ffe8bf", ec=C_DIM, lw=1.4, zorder=4))
    ax.text(10.99, 1.61, "门", fontsize=9.5, ha="center", va="center", color=C_DIM, zorder=5)
    dim(ax, (9.55, 1.62), (10.60, 1.62), "0.5m", off=0.24, fs=10, color=C_BAD)
    ax.annotate("④ checkpoint 8.0\n（停 8 秒 + 打卡留痕）", xy=(9.55, 1.62), xytext=(11.45, 3.10),
                fontsize=11.2, color=C_BAD, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=C_BAD, lw=1.4),
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=C_BAD, lw=1.0), zorder=12)

    box(ax, 0.95, 1.62,
        "① 距离：从“每次停车的待机点”出发，沿走道中心线用卷尺量\n"
        "     → 写进 forward / back 的米数（就是车走过的路程）\n"
        "② 转角：车头朝向变化量。右转为负（-90），左转为正（+90）\n"
        "     量法：量角器/手机水平仪对车头中线，或看两面墙的夹角\n"
        "③ 打卡点：车停稳后车头到门/柜的距离（建议 0.4~0.6m），\n"
        "     写成 {checkpoint: 8.0} —— 8.0 是停留秒数，不是距离",
        fs=10.6)

    box(ax, 11.5, 5.55,
        "route_patrol_ros1.yaml\n"
        "steps:\n"
        "  - {forward: 2.00, name: \"楼道\"}\n"
        "  - {turn: -90, name: \"右转\"}\n"
        "  - {forward: 1.50}\n"
        "  - {checkpoint: 8.0, name: \"保密室\"}",
        fs=10.2, ec="#93b4dd", fc="#f5f9ff")

    p = os.path.join(OUT, "01_路线标定.png")
    fig.savefig(p, dpi=170, facecolor=BG)
    plt.close(fig)
    return p


# --------------------------------------------------------- 02 圆弧转弯标定
def curved(ax, p1, p2, rad=0.3, color=C_CAR, lw=2.5, ms=15):
    ax.add_patch(FancyArrowPatch(p1, p2, connectionstyle="arc3,rad=%s" % rad,
                                 arrowstyle="-|>", mutation_scale=ms, color=color, lw=lw, zorder=6))


def fig_arc():
    fig = new_fig(14.0, 8.6)
    head(fig, "② 圆弧转弯标定：用“弦长”反算半径",
         "阿克曼不能原地转 → 90°也是一个圆弧；量起点/终点两点距离即可算出半径 R")

    ax = panel(fig, [0.04, 0.34, 0.42, 0.52], (-0.75, 2.30), (-0.60, 1.95), tag="几何关系")
    R = 1.20
    S, E, C = (R, 0.0), (0.0, R), (0.0, 0.0)
    arc = [math.radians(90.0) * i / 160.0 for i in range(161)]
    ax.plot([C[0] + R * math.cos(a) for a in arc], [C[1] + R * math.sin(a) for a in arc],
            color=C_CAR, lw=3.2, zorder=6, solid_capstyle="round")
    ax.plot([S[0], E[0]], [S[1], E[1]], ls="--", color=C_DIM, lw=1.6, zorder=5)
    # 半径：中心到弧中点
    mid = math.radians(45)
    ax.plot([C[0], R * math.cos(mid)], [C[1], R * math.sin(mid)],
            color=C_GRAY, lw=1.1, ls=":", zorder=4)
    ax.text(0.46, 0.55, "R", fontsize=13, color=C_GRAY, fontweight="bold")
    ax.plot([C[0]], [C[1]], marker="o", ms=5.5, color=C_GRAY, zorder=6)
    ax.text(-0.02, -0.14, "曲率中心", fontsize=9.5, color=C_GRAY, ha="left")

    car(ax, S[0], S[1], 90, L=0.42, W=0.28)
    car(ax, E[0], E[1], 180, L=0.42, W=0.28, alpha=0.92)
    ax.text(S[0] + 0.14, S[1] - 0.52, "起点（车头朝上）", fontsize=9.4, color=C_CAR, ha="left")
    ax.text(E[0] - 0.18, E[1] + 0.30, "终点（车头朝左）", fontsize=9.4, color=C_CAR, ha="left")
    ax.text(1.02, 1.62, "车头转了 90°", fontsize=10.5, color=C_DIM, fontweight="bold")

    # 弦长标签：右下角，箭头指向弦中点
    ax.annotate("弦长 C = 两点直线距离", xy=(0.62, 0.58), xytext=(1.05, 0.10),
                fontsize=10.4, color=C_DIM, fontweight="bold", ha="left",
                arrowprops=dict(arrowstyle="->", color=C_DIM, lw=1.2),
                bbox=dict(boxstyle="round,pad=0.28", fc="white", ec=C_DIM, lw=1.0), zorder=12)

    note_fig(fig, 0.045, 0.275,
             "步骤\n"
             "1) 起点车停正，车头中心在地上贴一条胶带\n"
             "2) 跑：python patrol_move_ros1.py --turn 90 --akm-turn-speed 0.06\n"
             "3) 停稳后在车头中心再贴一条，量两条线距离 = 弦长 C\n"
             "4) R = C / (2·sin(θ/2)) →  θ=90° 时 R = C / 1.414（例：C=1.58m → R≈1.12m）",
             fs=10.4, ec=C_DIM, fc="#fffaf3")

    # ---------- 右：三点调头 ----------
    ax = panel(fig, [0.53, 0.34, 0.42, 0.52], (0.10, 3.45), (0.05, 2.35), tag="窄走道里怎么掉头")
    ax.add_patch(Rectangle((0.25, 0.25), 3.05, 1.85, fc=C_FLOOR, ec="none", zorder=1))
    walls(ax, [(0.25, 0.25), (0.25, 2.10), (3.30, 2.10), (3.30, 0.25), (0.25, 0.25)])
    ax.text(3.18, 0.40, "走道尽头", fontsize=9.2, color=C_GRAY, ha="right", zorder=5)

    car(ax, 0.56, 1.55, 0, L=0.34, W=0.24)
    car(ax, 1.62, 1.02, -50, L=0.34, W=0.24, alpha=0.95)
    car(ax, 2.62, 0.88, 140, L=0.34, W=0.24, color="#e08a1e", alpha=0.95)
    car(ax, 1.24, 1.80, 182, L=0.34, W=0.24, color=C_OK, alpha=0.95)
    for (x, y, n, c) in [(0.34, 1.55, "1", C_CAR), (1.66, 0.76, "2", C_CAR),
                         (2.72, 0.58, "3", "#b06f14"), (1.02, 1.80, "4", C_OK)]:
        ax.text(x, y, n, fontsize=11, color=c, fontweight="bold", ha="center")

    curved(ax, (0.80, 1.55), (1.44, 1.10), rad=-0.22, color=C_CAR)
    curved(ax, (1.80, 0.98), (2.44, 0.90), rad=-0.26, color="#e08a1e")
    curved(ax, (2.44, 0.98), (1.48, 1.72), rad=0.26, color=C_OK)

    note_fig(fig, 0.54, 0.275,
             "steps（走道尽头调头）\n"
             "  - {turn: -60, name: \"1→2 前进右弧\"}\n"
             "  - {turn_rev: -60, name: \"2→3 倒车右弧\"}\n"
             "  - {turn: -60, name: \"3→4 前进右弧\"}   # 合计≈180°",
             fs=10.4, ec="#93b4dd", fc="#f5f9ff")

    note_fig(fig, 0.045, 0.115,
             "蓝=前进圆弧（turn）  橙=倒车圆弧（turn_rev）  绿=再次前进",
             fs=10.4)
    note_fig(fig, 0.54, 0.115,
             "每段都闭环：转不够自动补，超时停车并写报告；空间紧就降 --akm-turn-speed 到 0.05",
             fs=10.4)

    p = os.path.join(OUT, "02_圆弧转弯标定.png")
    fig.savefig(p, dpi=170, facecolor=BG)
    plt.close(fig)
    return p


# ------------------------------------------------------------- 03 里程标定
def fig_odo():
    fig = new_fig(14.0, 7.6)
    head(fig, "③ 长距离里程标定：1 m 直线 + 地面贴线",
         "目的：确认 odom 的“1 米”和真实 1 米差多少（比例误差）——目前只验证过 0.5m 以内的闭环")

    ax = panel(fig, [0.03, 0.40, 0.94, 0.46], (-0.2, 10.2), (0.2, 2.6))
    ax.add_patch(Rectangle((0.3, 0.40), 9.0, 1.95, fc=C_FLOOR, ec="none", zorder=1))
    ax.add_line(Line2D([0.3, 9.3], [2.35, 2.35], color=C_WALL, lw=6, zorder=2))
    ax.add_line(Line2D([0.3, 9.3], [0.40, 0.40], color=C_WALL, lw=6, zorder=2))

    x0, x1 = 1.2, 4.8
    ax.add_line(Line2D([x0, x0], [0.55, 2.20], color=C_BAD, lw=3.4, zorder=4))
    ax.add_line(Line2D([x1, x1], [0.55, 2.20], color=C_OK, lw=3.4, zorder=4, ls="--"))
    ax.text(x0, 2.44, "起点线", fontsize=10, color=C_BAD, ha="left", fontweight="bold")
    ax.text(x1, 2.44, "终点线（走完再贴）", fontsize=10, color=C_OK, ha="left", fontweight="bold")

    car(ax, x0 - 0.25, 1.45, 0, L=0.50, W=0.34)          # 车头正好压在起点线
    car(ax, x1 - 0.25, 1.45, 0, L=0.50, W=0.34, alpha=0.30)   # 车头压在终点线（理想）
    car(ax, x1 + 0.05, 1.45, 0, L=0.50, W=0.34, alpha=0.15)   # 越过终点线（odom 偏小）
    ax.text(x0 - 0.25, 1.10, "①", fontsize=11, color=C_CAR, ha="center", fontweight="bold")
    ax.text(x1 + 0.30, 1.95, "虚线车＝理想停车位置", fontsize=8.8, color=C_GRAY, ha="left")

    dim(ax, (x0, 0.78), (x1, 0.78), "① 先量 1.00 m，贴两条线", off=-0.26, fs=10.8)
    dim(ax, (x0, 1.95), (x1 + 0.26, 1.95), "② 让脚本走 --forward 1.0", off=0.24, fs=10.8, color=C_BLUE2)

    note_fig(fig, 0.045, 0.315,
             "怎么判读\n"
             "· 车头正好压终点线 → 比例准（<1%），可直接用\n"
             "· 车头越过终点线 → odom 偏小，车实际走得更远\n"
             "· 车头没到终点线 → odom 偏大",
             fs=10.6)
    note_fig(fig, 0.525, 0.315,
             "比例误差 = (真实位移 − odom 位移) / 真实位移 × 100%\n"
             "例：真实 1.12m、odom 1.03m → 误差 −8%\n"
             "判据：|误差| > 3% 就要校准底盘轮径 / 减速比参数",
             fs=10.6, ec=C_DIM, fc="#fffaf3")
    note_fig(fig, 0.045, 0.135,
             "前提：地面平整、环境静态；测量时人不要站在车头雷达/相机视野里"
             "（上次有人站在车前，深度相机读数被污染，结论不成立）；重复 3 次取平均更可靠。",
             fs=10.5, ec=C_BAD, fc="#fff5f5")

    p = os.path.join(OUT, "03_里程标定.png")
    fig.savefig(p, dpi=170, facecolor=BG)
    plt.close(fig)
    return p


# --------------------------------------------------------- 04 雷达扇区标定
def fig_sector():
    fig = new_fig(13.6, 7.8)
    head(fig, "④ 雷达扇区标定：补偿角填错 = 避障静默失效",
         "扇区必须落在“车头”方向且该方向要有回波；否则看着有避障，实际永远不触发")

    ax = panel(fig, [0.03, 0.30, 0.94, 0.56], (-3.85, 3.85), (-1.95, 2.05))
    for cx, ok, ttl in ((-1.85, True, "补偿角 0°（本车实测正确）"),
                        (1.85, False, "补偿角 180°（错：扇区指到盲区）")):
        ax.text(cx, 1.72, ttl, fontsize=13, fontweight="bold",
                color=C_OK if ok else C_BAD, ha="center")
        car(ax, cx, 0.0, 0, L=0.92, W=0.60, alpha=0.95)
        ax.plot([cx], [0], marker="o", ms=4, color="yellow", zorder=11)
        ax.text(cx, -0.52, "雷达", fontsize=8.5, color="#374151", ha="center", zorder=10)
        if ok:
            ax.add_patch(Wedge((cx, 0), 1.42, 55, 125, width=1.42, fc=C_OK, alpha=0.15, zorder=2))
            for a in (55, 125):
                ax.plot([cx, cx + 1.42 * math.cos(math.radians(a))],
                        [0, 1.42 * math.sin(math.radians(a))],
                        color=C_OK, lw=1.3, ls="--", zorder=3)
            ax.text(cx, 1.06, "前方 ±35°", fontsize=10.5, color=C_OK, ha="center", fontweight="bold")
            ax.text(cx, -1.28, "正确：该扇区有 61~79 个有效回波\n前方 <0.35m 能可靠拦停",
                    fontsize=10.6, color=C_OK, ha="center", fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.35", fc="#f0fff4", ec=C_OK, lw=1.2))
        else:
            ax.add_patch(Wedge((cx, 0), 1.42, 235, 305, width=1.42, fc=C_BAD, alpha=0.15, zorder=2))
            for a in (235, 305):
                ax.plot([cx, cx + 1.42 * math.cos(math.radians(a))],
                        [0, 1.42 * math.sin(math.radians(a))],
                        color=C_BAD, lw=1.3, ls="--", zorder=3)
            ax.text(cx, -1.06, "扇区指到后方", fontsize=10.5, color=C_BAD, ha="center", fontweight="bold")
            ax.text(cx, -1.72, "错误：该方向 448 点里 0 个有效回波（盲区）\n避障永不触发",
                    fontsize=10.6, color=C_BAD, ha="center", fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.35", fc="#fff5f5", ec=C_BAD, lw=1.2))

    note_fig(fig, 0.045, 0.245,
             "怎么判定：python tools/scan_align_check.py     （深度相机剖面 × 雷达剖面交叉验证，输出最吻合的补偿角）\n"
             "            python patrol_move_ros1.py --scan-check   （不发动车，打印 12 个扇区的最近距离）\n"
             "自检：脚本启动时会数“扇区有效回波”，<3 个会显式报警并写进报告事件 scan_sector_empty",
             fs=10.6, ec="#93b4dd", fc="#f5f9ff")
    note_fig(fig, 0.045, 0.115,
             "四向净空（网页/报告里都一样）：前 -25°~+25° ｜ 左 45°~90° ｜ 右 -90°~-45° ｜ 后 |a|>150°",
             fs=10.6)

    p = os.path.join(OUT, "04_雷达扇区标定.png")
    fig.savefig(p, dpi=170, facecolor=BG)
    plt.close(fig)
    return p


if __name__ == "__main__":
    for fn in (fig_route, fig_arc, fig_odo, fig_sector):
        print("生成：%s" % fn())
