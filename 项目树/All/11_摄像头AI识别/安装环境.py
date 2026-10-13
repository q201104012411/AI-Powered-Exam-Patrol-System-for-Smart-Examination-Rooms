# -*- coding: utf-8 -*-
r"""
摄像头 AI 识别 · 环境安装

只装本子方案需要的东西：opencv-python（完整版，非 headless）、numpy、Pillow。

为什么要强调"非 headless"：opencv-python-headless **不含 HOGDescriptor**，
而 HOG 是没放模型时的兜底检测器，缺了它 AI 识别直接不可用。
另外 opencv-python 5.x 已移除 HOGDescriptor，所以固定装 4.x。

用法：python 安装环境.py
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
    except Exception as e:
        log("执行失败：%s" % e)
        return False


def verify(py):
    """装完必须实际验一遍，不能只看 pip 返回码。"""
    code = ("import cv2, numpy, PIL; "
            "print('opencv', cv2.__version__); "
            "print('numpy', numpy.__version__); "
            "print('HOGDescriptor', hasattr(cv2, 'HOGDescriptor')); "
            "print('peopleDetector', hasattr(cv2, 'HOGDescriptor_getDefaultPeopleDetector'))")
    log("验证依赖…")
    try:
        subprocess.call([py, "-c", code])
    except Exception as e:
        log("验证失败：%s" % e)
        return False
    return True


def main():
    py = sys.executable
    log("解释器：%s" % py)

    log("安装/升级：opencv-python<5、numpy、Pillow")
    run([py, "-m", "pip", "install", "--upgrade",
         "opencv-python<5", "numpy", "Pillow"])

    log("检查 HOGDescriptor（AI 框选的必要条件）…")
    ok = subprocess.call(
        [py, "-c", "import cv2; assert hasattr(cv2,'HOGDescriptor')"]) == 0
    if not ok:
        log("HOGDescriptor 缺失（多半装成了 headless 或 5.x），强制重装完整版 4.x")
        run([py, "-m", "pip", "uninstall", "-y",
             "opencv-python-headless", "opencv-python"])
        run([py, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall",
             "opencv-python<5"])

    verify(py)

    log("")
    log("完成。下一步：")
    log("  1) 双击「启动.bat」打开界面")
    log("  2) 在「① 摄像头管理」里加摄像头（本机摄像头填 0 即可先试）")
    log("  3) 想让识别更准：把 yolov8n-pose.onnx 放进本目录或本目录\\models\\")
    log("     （可选，不放则用 HOG+SVM 兜底；确认当前后端看界面顶部）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
