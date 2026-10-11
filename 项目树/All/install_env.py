#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
智能考场 AI 巡考系统 · 一键环境安装

自动完成 01/02/03 运行所需依赖的安装与校验：
- 挑选一个已安装 Python（优先 3.12/3.11/3.10/3.13，支持 py/python/python3）
- 安装/升级通用依赖：opencv-python（完整版，非 headless）、numpy、Pillow
- 安装 01 人脸识别专用依赖（requirements-face.txt）
- 检测 OpenCV 是否包含 HOGDescriptor（02 AI 越界检测的必要条件）
- 若检测到 headless 导致 HOG 缺失，自动卸载 headless 并强制重装完整版 opencv-python
- 校验 tkinter / sqlite3 等系统自带模块

用法：
    python install_env.py
    # Windows 下可直接双击「项目树\\All\\安装环境.bat」
"""
import os
import platform
import subprocess
import sys

# 让中文日志在命令行和日志文件里都正确显示
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print("[install] %s" % msg)
    sys.stdout.flush()


def run(cmd, check=False, timeout=300):
    """运行命令并打印"""
    log("> %s" % " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 and p.stderr:
        log(p.stderr.strip()[:500])
    if p.stdout:
        for line in p.stdout.strip().splitlines()[-20:]:
            log("  %s" % line)
    if check and p.returncode != 0:
        raise RuntimeError("命令失败：%s" % " ".join(str(c) for c in cmd))
    return p


def find_python():
    """找一个可用的 Python 解释器，优先 py launcher"""
    candidates = [
        ["py", "-3.12"], ["py", "-3.11"], ["py", "-3.13"], ["py", "-3.10"],
        ["py", "-3"], ["python3"], ["python"],
    ]
    for c in candidates:
        try:
            p = subprocess.run(c + ["-c", "import sys; print(sys.executable)"],
                               capture_output=True, text=True, timeout=15)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
    # 兜底：当前解释器
    return sys.executable


def has_module(py, mod):
    p = subprocess.run([py, "-c", "import %s" % mod],
                       capture_output=True, text=True, timeout=30)
    return p.returncode == 0


def has_hog(py):
    """02 AI 越界检测必要条件：HOGDescriptor"""
    code = (
        "import cv2; "
        "assert hasattr(cv2, 'HOGDescriptor'), '缺少 HOGDescriptor'; "
        "assert hasattr(cv2, 'HOGDescriptor_getDefaultPeopleDetector'), '缺少默认行人检测器'"
    )
    p = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=30)
    return p.returncode == 0


def install_common(py):
    """安装 02/03/01 都需要的通用包（opencv-python 固定 4.x，5.x 已移除 HOGDescriptor）"""
    log("安装/升级通用依赖：opencv-python<5、numpy、Pillow")
    run([py, "-m", "pip", "install", "--upgrade", "opencv-python<5", "numpy", "Pillow"])


def install_face(py):
    """安装 01 人脸识别依赖"""
    req = os.path.join(ROOT, "01_人脸识别与身份核对", "01_人脸识别与身份核对", "requirements-face.txt")
    if os.path.exists(req):
        log("安装 01 人脸识别依赖：%s" % req)
        run([py, "-m", "pip", "install", "-r", req])
    else:
        log("未找到 01 requirements-face.txt，跳过")


def fix_opencv_hog(py):
    """如果当前 cv2 没有 HOG，尝试修复 headless 冲突或版本过高"""
    if has_hog(py):
        return True
    log("当前 OpenCV 缺少 HOGDescriptor，尝试修复...")
    # 常见原因 1：装的是 opencv-python-headless
    # 常见原因 2：装的是 opencv-python 5.x（已移除 HOGDescriptor）
    run([py, "-m", "pip", "uninstall", "-y",
         "opencv-python-headless", "opencv-python"])
    run([py, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall",
         "opencv-python<5"])
    return has_hog(py)


def check_all(py):
    """最终校验"""
    checks = [
        ("tkinter", "tkinter", "GUI 库（Python 官方安装包自带）"),
        ("sqlite3", "sqlite3", "数据库支持（Python 官方安装包自带）"),
        ("cv2", "opencv-python", "OpenCV 图像/视频处理"),
        ("numpy", "numpy", "NumPy 数值计算"),
        ("PIL", "Pillow", "Pillow 图像显示"),
    ]
    ok = True
    log("=" * 50)
    log("依赖校验")
    log("=" * 50)
    for mod, pkg, desc in checks:
        if has_module(py, mod):
            log("[OK] %s (%s)" % (pkg, desc))
        else:
            log("[FAIL] %s (%s) 缺失" % (pkg, desc))
            ok = False
    if has_hog(py):
        log("[OK] HOGDescriptor（02 AI 越界检测必要条件）")
    else:
        log("[FAIL] HOGDescriptor 缺失，02 AI 越界检测将不可用")
        ok = False
    log("=" * 50)
    return ok


def main():
    log("平台：%s" % platform.platform())
    py = find_python()
    log("选用解释器：%s" % py)

    if not has_module(py, "tkinter"):
        log("ERROR: 该 Python 未包含 tkinter，请重新安装 Python 并勾选 tcl/tk")
        return 1

    install_common(py)
    install_face(py)

    if not fix_opencv_hog(py):
        log("WARNING: HOGDescriptor 修复失败，02 的 AI 越界检测功能不可用")

    ok = check_all(py)

    if ok:
        log("环境安装完成，可关闭本窗口后运行各子系统启动脚本。")
        return 0
    else:
        log("环境未完全就绪，请根据上方提示安装缺失项。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
