# -*- coding: utf-8 -*-
"""
YOLO11n ONNX 模型导出工具

作用：把 Ultralytics YOLO11n 官方权重导出为 ONNX（models/yolo11n.onnx），
      供 person_detect.py 检测链使用（YOLO11n 优先，HOG 兜底）。

一次性依赖（导出用，日常运行只需要 onnxruntime）：
    pip install ultralytics onnxslim

用法：
    python 工具_YOLO模型导出.py

说明：
    · 首次运行会联网下载 yolo11n.pt（约 5.4MB，Ultralytics 官方 release）；
    · 导出的 models/yolo11n.onnx 约 10MB，随项目拷贝分发即可，无需重复导出；
    · 输出模型：输入 1×3×640×640（RGB、/255），输出 [1, 84, 8400]
      （4 bbox + 80 COCO 类，person 为第 0 类），与 person_detect 的
      _yolo_postprocess 解码约定一致；
    · 如需换模型（如 yolo11s）：改 MODEL_NAME 后重跑本脚本即可。
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "models")
MODEL_NAME = "yolo11n"                      # 可改为 yolo11s / yolo11m 等
TARGET = os.path.join(MODELS_DIR, MODEL_NAME + ".onnx")


def main():
    try:
        from ultralytics import YOLO
    except ImportError:
        print("[缺少依赖] 导出需要 ultralytics：")
        print("    pip install ultralytics onnxslim")
        return 2

    os.makedirs(MODELS_DIR, exist_ok=True)
    print("开始导出 %s -> ONNX（首次会自动下载 %s.pt）..." % (MODEL_NAME, MODEL_NAME))
    model = YOLO(MODEL_NAME + ".pt")
    out = model.export(format="onnx", imgsz=640, opset=12, simplify=True)
    if not out or not os.path.exists(str(out)):
        print("导出失败：未找到输出文件 %s" % out)
        return 1
    if os.path.abspath(str(out)) != os.path.abspath(TARGET):
        shutil.move(str(out), TARGET)
    print("导出完成：%s（%.1f MB）" % (TARGET, os.path.getsize(TARGET) / 1e6))
    print("现在运行 run_desktop.py / run_web.py，检测链会自动启用 YOLO11n。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
