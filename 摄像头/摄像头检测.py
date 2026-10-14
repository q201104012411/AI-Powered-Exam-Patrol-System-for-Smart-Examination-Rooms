# -*- coding: utf-8 -*-
r"""
本机摄像头检测 / 切换助手（放在 智能考场AI巡考系统 根目录）

为什么需要它：巡考系统里"本机摄像头"是用**序号**当取流地址的（填 0、1、2 …），
但序号和实物（内置 / 外接 USB / 红外）对不上号 —— 有两个摄像头时根本不知道
该填哪个。本工具把这三件事一次做完：

  1. **列设备**：Windows 认到的摄像头（内置 / USB / 红外）名称与状态；
  2. **逐个试**：0~N 号谁真的能出画面、分辨率、帧率、能不能稳定读到帧；
  3. **对号入座**：每个能用的序号存一张快照到 `摄像头快照\`，人眼一看就知道
     哪个序号是哪个摄像头，然后把该填的**取流地址**告诉你。

用法：
  python 摄像头检测.py                # 列设备 + 逐个试 + 存快照，最后给建议
  python 摄像头检测.py --preview 1    # 打开 1 号摄像头的实时预览（按 0-9 切序号，S 存图，Q 退出）
  python 摄像头检测.py --list         # 只看设备与序号，不存快照
  python 摄像头检测.py --max-index 5  # 试到几号（默认 5）
  python 摄像头检测.py --no-snapshot  # 不存快照

说明：
  · 摄像头同一时刻只能被一个程序占用 —— 请先关掉巡考界面 / 其它预览软件，
    否则会看到"打开失败（可能被占用）"，那是真的被占用，不是摄像头坏了。
  · Windows 下优先用 DirectShow（`cv2.CAP_DSHOW`）：比默认 MSMF 快且稳定。
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP_DIR = HERE / "摄像头快照"

try:
    import cv2
except ImportError:
    print("[错误] 没装 opencv：请先运行项目根目录的「install_pip环境.bat」"
          "或在项目目录里执行 安装环境.bat")
    sys.exit(2)

import numpy as np  # noqa: E402


# ---------------------------------------------------------------------
#  1) Windows 设备清单
# ---------------------------------------------------------------------
def windows_camera_devices():
    """读 Windows 认到的摄像头设备（名称 + 状态）。失败就返回空列表，不报错。"""
    if os.name != "nt":
        return []
    ps = ("Get-PnpDevice -Class Camera,Image -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Status -eq 'OK' } | "
          "Select-Object -Property FriendlyName,Status | "
          "ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25)
        text = (out.stdout or "").strip()
        if not text:
            return []
        import json
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        return [(d.get("FriendlyName", ""), d.get("Status", "")) for d in data]
    except Exception:
        return []


# ---------------------------------------------------------------------
#  2) 逐个序号试能不能出画面
# ---------------------------------------------------------------------
def _open(index):
    """打开本机摄像头：优先 DirectShow（Windows），失败再退回默认后端。"""
    if os.name == "nt" and hasattr(cv2, "CAP_DSHOW"):
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if cap is not None and cap.isOpened():
            return cap, "DirectShow"
        try:
            cap.release()
        except Exception:
            pass
    cap = cv2.VideoCapture(index)
    return cap, "默认后端"


def probe(index, warmup=8):
    """试一个序号 → dict(ok, w, h, fps, backend, frame, note)"""
    t0 = time.time()
    try:
        cap, backend = _open(index)
    except Exception as e:
        return {"index": index, "ok": False, "note": "打开异常：%s" % e,
                "ms": (time.time() - t0) * 1000, "frame": None}
    if cap is None or not cap.isOpened():
        try:
            cap.release()
        except Exception:
            pass
        return {"index": index, "ok": False, "note": "打不开（没这个设备，或被其它程序占用）",
                "ms": (time.time() - t0) * 1000, "frame": None}
    # 前几帧常常是黑的/没曝光好，丢掉；只认真实读到的帧
    frame = None
    got = 0
    t_read = time.time()
    for _ in range(max(1, warmup)):
        ok, f = cap.read()
        if ok and f is not None and getattr(f, "size", 0) > 0:
            frame = f
            got += 1
    read_s = max(1e-3, time.time() - t_read)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    try:
        cap.release()
    except Exception:
        pass
    info = {"index": index, "ok": frame is not None, "w": w, "h": h, "fps": fps,
            "fps_measured": got / read_s, "backend": backend,
            "ms": (time.time() - t0) * 1000, "frame": frame, "got": got, "note": ""}
    if frame is None:
        info["note"] = "能打开但读不到画面（%d 帧里 0 帧有效，多为红外/虚拟设备）" % warmup
        info["black"] = False
    else:
        # 能读到帧 ≠ 看得见东西：隐私挡板关闭 / 镜头被遮 / 红外设备都会给全黑帧，
        # 这种情况必须如实标出来，否则使用者会以为"摄像头是好的"。
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean = float(gray.mean())
        dark_ratio = float((gray < 12).mean())
        info["mean_luma"] = mean
        info["black"] = (mean < 10.0 and dark_ratio > 0.97)
        if info["black"]:
            info["note"] = ("画面全黑（平均亮度 %.1f）：多为隐私挡板关闭 / 镜头被遮 / "
                            "红外或虚拟设备不给普通画面" % mean)
    return info


def save_snapshot(info, note=""):
    """把这一路的一帧存下来，供人工对号入座"""
    if info.get("frame") is None:
        return None
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        path = SNAP_DIR / ("摄像头_%d号_%s.jpg" % (info["index"], stamp))
        img = info["frame"]
        tag = "index=%d  %dx%d  %.1ffps  %s" % (
            info["index"], img.shape[1], img.shape[0],
            info.get("fps_measured", info.get("fps") or 0.0), info.get("backend", ""))
        if info.get("black"):
            tag += "  [ALL BLACK mean=%.1f]" % info.get("mean_luma", 0.0)
        if note:
            tag += "  %s" % note
        cv2.putText(img, tag, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 0, 255), 2, cv2.LINE_AA)
        cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])[1].tofile(str(path))
        return path
    except Exception as e:
        print("  （存快照失败：%s）" % e)
        return None


# ---------------------------------------------------------------------
#  3) 实时预览（换摄像头时用它确认画面对不对）
# ---------------------------------------------------------------------
def preview(start_index, max_index):
    """多路预览：按数字键切换序号，S 存图，Q/ESC 退出。"""
    print("=" * 74)
    print("实时预览：按 0-%d 切换摄像头序号 ｜ S 存快照 ｜ Q / ESC 退出" % max_index)
    print("=" * 74)
    print("提示：摄像头同一时刻只能被一个程序占用；若提示读不到画面，")
    print("      先关掉巡考界面/相机 App（或结束 camera_stream.py 进程）再试。")
    cap = None
    index = start_index
    backend = ""
    fail_since = 0
    reopen_tries = 0

    def open_index(i):
        nonlocal cap, backend, fail_since, reopen_tries
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        c, b = _open(i)
        cap, backend, fail_since = c, b, 0
        if c is None or not c.isOpened():
            print("[%d 号] 打不开（没这个设备 / 被其它程序占用）—— 可换序号或按 Q 退出" % i)
            cap = None
            return False
        # 等几帧再报"已打开"：有些设备刚打开时给不出帧
        for _ in range(6):
            ok, f = c.read()
            if ok and f is not None:
                print("[%d 号] 已打开（%s，%dx%d）—— 按 S 存快照"
                      % (i, b, f.shape[1], f.shape[0]))
                reopen_tries = 0
                return True
        print("[%d 号] 打开了但读不到画面（多为被抢占/隐私挡板/红外设备）" % i)
        return False

    open_index(index)
    try:
        while True:
            if cap is None:
                key = cv2.waitKey(50) & 0xFF
                if key in (ord("q"), 27):
                    break
                if ord("0") <= key <= ord("9"):
                    index = key - ord("0")
                    if index <= max_index:
                        open_index(index)
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                fail_since += 1
                if fail_since > 60:
                    # 不疯狂重开：每 2 秒最多重试一次，连续 5 次失败就退出并说清原因
                    time.sleep(2.0)
                    reopen_tries += 1
                    if reopen_tries > 5:
                        print("连续 5 次都读不到画面，先退出。常见原因：")
                        print("  · 该设备被其它程序占用（关掉巡考界面/相机 App 后重试）")
                        print("  · 是红外摄像头（不给普通彩色画面）")
                        print("  · 物理隐私挡板关着 / Windows 摄像头隐私开关关着")
                        break
                    print("重试第 %d 次打开 %d 号 …" % (reopen_tries, index))
                    open_index(index)
                key = cv2.waitKey(10) & 0xFF
                if key in (ord("q"), 27):
                    break
                continue
            fail_since = 0
            tag = "index=%d  %dx%d  %s" % (index, frame.shape[1], frame.shape[0], backend)
            cv2.putText(frame, tag, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 0, 255), 2, cv2.LINE_AA)
            cv2.imshow("camera preview (0-9 switch, S save, Q quit)", frame)
            key = cv2.waitKey(15) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("s"):
                p = save_snapshot({"index": index, "frame": frame,
                                   "backend": backend}, note="(预览手动保存)")
                if p:
                    print("  已保存：%s" % p)
            if ord("0") <= key <= ord("9"):
                new_index = key - ord("0")
                if new_index <= max_index and new_index != index:
                    index = new_index
                    open_index(index)
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="本机摄像头检测与切换助手")
    ap.add_argument("--list", action="store_true", help="只列设备，不逐个试/不存快照")
    ap.add_argument("--max-index", dest="max_index", type=int, default=5,
                    help="试到几号（默认 5）")
    ap.add_argument("--no-snapshot", dest="no_snapshot", action="store_true",
                    help="不存快照")
    ap.add_argument("--preview", type=int, default=None, metavar="INDEX",
                    help="只开这一路的实时预览（按 0-9 可切换）")
    args = ap.parse_args()

    if args.preview is not None:
        return preview(args.preview, args.max_index)

    print("=" * 74)
    print("本机摄像头检测  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 74)

    devices = windows_camera_devices()
    print("\n【Windows 认到的摄像头设备】")
    if devices:
        for i, (name, status) in enumerate(devices, 1):
            print("  %d) %-28s %s" % (i, name, status))
    else:
        print("  （读不到设备清单；可能是非 Windows 或权限受限——不影响下面的序号检测）")
    print("  注：设备清单的先后顺序**不等于** OpenCV 的序号，具体看下面逐个试的结果。")

    if args.list:
        print("\n（--list 模式：不逐个试序号、不存快照。要检测序号请直接运行本脚本）")
        return 0

    print("\n【逐个序号试：谁能出画面】")
    print("  提示：摄像头同一时刻只能被一个程序占用，请先关掉巡考界面/其它预览软件。\n")
    results = []
    for idx in range(0, max(0, args.max_index) + 1):
        info = probe(idx)
        results.append(info)
        if info["ok"] and not info.get("black"):
            print("  %d 号：✅ 可用   %dx%d  实测 %.1f fps  %s  （打开 %.0f ms）"
                  % (idx, info["w"], info["h"], info.get("fps_measured", 0.0),
                     info["backend"], info["ms"]))
        elif info["ok"] and info.get("black"):
            print("  %d 号：⚠ 能出帧但**全黑**   %dx%d  %s  —— %s"
                  % (idx, info["w"], info["h"], info["backend"], info["note"]))
        else:
            print("  %d 号：—      %s" % (idx, info["note"]))

    usable = [r for r in results if r["ok"] and not r.get("black")]
    dark = [r for r in results if r["ok"] and r.get("black")]
    if (usable or dark) and not args.no_snapshot:
        print("\n【每个能打开的序号存一张快照（人眼对号入座，全黑的也存，便于确认）】")
        for r in usable + dark:
            p = save_snapshot(r)
            if p:
                print("  %d 号 -> %s" % (r["index"], p))
        print("  快照目录：%s" % SNAP_DIR)

    print("\n【结论与怎么换】")
    if not usable:
        print("  ❌ 没有「能看到画面」的序号。")
        if dark:
            print("     以下序号能打开但画面全黑：%s" % "、".join(str(r["index"]) for r in dark))
            print("     常见原因与处理：")
            print("       · 笔记本内置摄像头的**物理隐私挡板/滑块**没打开（最常见，直接推开）")
            print("       · Windows 设置 → 隐私和安全性 → 摄像头 → 允许桌面应用访问摄像头")
            print("       · 该序号其实是**红外摄像头**（Windows Hello 用），它不给普通彩色画面")
            print("       · 镜头被贴住/朝向墙壁，或环境全黑（遮住镜头再看 mean 亮度）")
        else:
            print("     常见原因：")
            print("       · 摄像头正被别的程序占用（先关掉巡考界面、相机 App、其它预览工具）")
            print("       · 笔记本的物理摄像头开关/隐私开关关掉了")
            print("       · 试试 --max-index 8 看有没有更靠后的序号")
        return 1
    print("  能出画面的序号：%s" % "、".join(str(r["index"]) for r in usable))
    print("  分辨率/帧率  ：%s"
          % "，".join("%d号 %dx%d@%.0ffps" % (r["index"], r["w"], r["h"],
                                             r.get("fps_measured", 0.0)) for r in usable))
    if dark:
        print("  ⚠ 能打开但全黑的序号：%s（别用这些）"
              % "、".join(str(r["index"]) for r in dark))
    print()
    if len(usable) > 1:
        print("  要**换摄像头**：把「取流地址」从现在的序号改成另一个序号，例如：")
        for r in usable:
            print("     %d 号 -> 取流地址填 「%d」" % (r["index"], r["index"]))
    else:
        print("  只有一个序号能看到画面（%d 号）→ 取流地址就填 「%d」。"
              % (usable[0]["index"], usable[0]["index"]))
    print()
    print("  在巡考系统里填在哪：")
    print("    · 02 摄像头核查：设备台账 → 双击该机位编辑 → 取流地址改成上面的序号，"
          "再回实时监控点「刷新设备」")
    print("    · 11 摄像头AI识别：① 摄像头管理 → 地址下拉里选（或点「扫描」自动找）")
    print("    · 03 智能终端：监控页添加摄像头时地址填序号")
    print("  确认画面对不对：python 摄像头检测.py --preview <序号>（按 0-9 切换）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
