# -*- coding: utf-8 -*-
r"""
试卷流转与保密室智能监控 · 环境安装

本专题的运行时依赖与 **02 摄像头核查 / 03 智能终端 / 11 摄像头AI识别 完全一致**，
所以这里装的是同一套东西，保证多套系统共用同一个解释器时不会互相拆台：

  · opencv-python（**4.x，完整版非 headless**）—— 取流、抓帧留证、合成源
  · numpy                                        —— 帧运算
  · Pillow                                       —— 桌面端画面显示（缺失时降级）
  · flask / flask-cors                           —— 网页面板（端口 5020，含 MJPEG 实时流）
  · cryptography                                 —— 与 02/03 的既有依赖保持一致

为什么必须强调 "4.x 完整版"：
  · opencv-python-headless **不含 HOGDescriptor**；
  · opencv-python **5.x 已移除 HOGDescriptor / CascadeClassifier**。
  两者都会让"人形检测"不可用，也让 02/11 的 AI 检测挂掉 —— 本仓库统一锁 4.x。

用法：
    python 安装环境.py
    # Windows 下可直接双击「安装环境.bat」
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print("[install] %s" % msg, flush=True)


def run(cmd):
    log("$ %s" % " ".join(cmd))
    try:
        return subprocess.call(cmd) == 0
    except Exception as e:                                   # noqa: BLE001
        log("执行失败：%s" % e)
        return False


def has_hog(py):
    code = ("import cv2; "
            "assert hasattr(cv2, 'HOGDescriptor'), '缺少 HOGDescriptor'; "
            "assert hasattr(cv2, 'HOGDescriptor_getDefaultPeopleDetector')")
    return subprocess.call([py, "-c", code],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def has_module(py, mod):
    return subprocess.call([py, "-c", "import %s" % mod],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def verify(py):
    """装完必须实际验一遍，不能只看 pip 返回码。"""
    code = ("import cv2, numpy; "
            "print('opencv', cv2.__version__); "
            "print('numpy', numpy.__version__); "
            "print('HOGDescriptor', hasattr(cv2, 'HOGDescriptor')); "
            "print('peopleDetector', "
            "hasattr(cv2, 'HOGDescriptor_getDefaultPeopleDetector'))")
    log("验证核心依赖…")
    try:
        subprocess.call([py, "-c", code])
    except Exception as e:                                   # noqa: BLE001
        log("验证失败：%s" % e)
        return False
    return True


def main():
    py = sys.executable
    log("解释器：%s" % py)

    # ---- 1. 通用依赖（与 02/03/11 同一套） ----
    log("安装/升级：opencv-python<5、numpy、Pillow、cryptography")
    run([py, "-m", "pip", "install", "--upgrade",
         "opencv-python<5", "numpy", "Pillow", "cryptography"])

    # ---- 2. 网页面板依赖（本专题独有：MJPEG 实时流走 Flask） ----
    log("安装/升级：flask、flask-cors（网页面板 + MJPEG 实时画面）")
    run([py, "-m", "pip", "install", "--upgrade", "flask", "flask-cors"])

    # ---- 3. HOG 修复（headless 或 5.x 都会缺） ----
    log("检查 HOGDescriptor（人形检测必要条件，与 02/11 共用）…")
    if not has_hog(py):
        log("HOGDescriptor 缺失（多半装成了 headless 或 5.x），强制重装完整版 4.x")
        run([py, "-m", "pip", "uninstall", "-y",
             "opencv-python-headless", "opencv-python"])
        run([py, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall",
             "opencv-python<5"])

    verify(py)

    # ---- 4. 系统自带模块校验 ----
    log("=" * 50)
    log("依赖校验")
    log("=" * 50)
    ok = True
    for mod, desc in (("tkinter", "桌面界面（Python 官方安装包自带，需勾 tcl/tk）"),
                      ("sqlite3", "数据库（Python 官方安装包自带）"),
                      ("cv2", "OpenCV 取流与抓帧"),
                      ("numpy", "NumPy 数值计算"),
                      ("PIL", "Pillow 画面显示"),
                      ("flask", "网页面板"),
                      ("flask_cors", "网页跨域")):
        if has_module(py, mod):
            log("[OK] %s（%s）" % (mod, desc))
        else:
            log("[FAIL] %s 缺失（%s）" % (mod, desc))
            ok = False
    if has_hog(py):
        log("[OK] HOGDescriptor（人形检测必要条件）")
    else:
        log("[FAIL] HOGDescriptor 缺失，人形检测与 02/11 的 AI 检测将不可用")
        ok = False
    log("=" * 50)

    if ok:
        log("环境安装完成。下一步：")
        log("  1) 双击「运行自检与测试.bat」验证"
            "（122 单元 + 114 自检 + 86 网页接口 + 30 联动）")
        log("  2) 双击「启动保密室监控界面.bat」用 owner / EDY_POWERQ 登录桌面端")
        log("  3) 或双击「启动网页版.bat」在浏览器打开 http://127.0.0.1:5020/")
        log("  4) 需要与 00_集成联动 联调时，双击「联动自检.bat」")
        return 0
    log("环境未完全就绪，请根据上方提示安装缺失项。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
