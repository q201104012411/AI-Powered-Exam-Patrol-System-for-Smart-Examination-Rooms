# -*- coding: utf-8 -*-
r"""
AI 火场识别与自动报警 · 环境安装 / 依赖检查

需要什么：
    必需  opencv-python（取流 + 图像处理）  numpy  Pillow（界面显示画面）  tkinter（随 Python 自带）
    可选  onnxruntime（只有要用 models\fire*.onnx 深度学习模型时才需要）
          本程序默认用多信号传统视觉，**不装 onnxruntime 也能正常报警**。

用法：
    python 安装环境.py            # 检查 + 缺什么装什么（联网）
    python 安装环境.py --check    # 只检查，不安装
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

REQUIRED = [
    ("cv2", "opencv-python<5", "取流与图像处理"),
    ("numpy", "numpy", "数值计算"),
    ("PIL", "Pillow", "界面显示画面"),
]
OPTIONAL = [
    ("onnxruntime", "onnxruntime", "ONNX 火焰/烟雾模型（可选，默认不用）"),
]


def find(m):
    try:
        return importlib.util.find_spec(m) is not None
    except (ImportError, ValueError):
        return False


def check_tkinter():
    try:
        import tkinter  # noqa: F401
        return True, ""
    except ImportError as exc:
        return False, ("缺少 tkinter。请用官方安装包重装 Python 并在安装时勾选 "
                       "tcl/tk（Windows 的 Microsoft Store 版常缺）。（%s）" % exc)


def report():
    print("=" * 74)
    print("AI 火场识别与自动报警 · 依赖检查")
    print("=" * 74)
    print("Python：%s" % sys.version.split()[0])
    print("解释器：%s" % sys.executable)
    print("-" * 74)
    missing = []
    for m, pkg, desc in REQUIRED:
        ok = find(m)
        print("  [%s] %-12s %-22s %s" % ("OK" if ok else "缺", m, pkg, desc))
        if not ok:
            missing.append(pkg)
    for m, pkg, desc in OPTIONAL:
        ok = find(m)
        print("  [%s] %-12s %-22s %s" % ("OK" if ok else "可选", m, pkg, desc))
    tk_ok, tk_why = check_tkinter()
    print("  [%s] %-12s %-22s %s" % ("OK" if tk_ok else "缺", "tkinter",
                                     "Python 自带", "图形界面"))
    if not tk_ok:
        print("      %s" % tk_why)
    print("-" * 74)
    if missing:
        print("缺必需依赖：%s" % " ".join(missing))
    else:
        print("必需依赖齐全。")
    print("=" * 74)
    return missing, tk_ok


def pip_install(pkgs):
    if not pkgs:
        return True
    cmd = [sys.executable, "-m", "pip", "install"] + pkgs
    print("执行：%s" % " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
    except OSError as exc:
        print("无法启动 pip：%s" % exc)
        return False
    return rc == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不安装")
    ap.add_argument("--with-onnx", action="store_true",
                    help="顺便安装 onnxruntime（要用 .onnx 模型时才需要）")
    ns = ap.parse_args()

    missing, tk_ok = report()
    if ns.check:
        return 0 if not missing else 1
    if not tk_ok:
        print("\n[注意] 缺 tkinter 时 pip 装不上（它是 Python 的一部分）。")
        print("       仍可用 `python app.py --headless` 无界面常驻报警。")

    todo = list(missing)
    if ns.with_onnx and not find("onnxruntime"):
        todo.append("onnxruntime")
    if not todo:
        print("\n无需安装。")
        return 0
    if pip_install(todo):
        print("\n安装完成，重新运行本脚本可复核。")
        return 0
    print("\n安装失败。可手动执行：")
    print("    %s -m pip install %s" % (sys.executable, " ".join(todo)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
