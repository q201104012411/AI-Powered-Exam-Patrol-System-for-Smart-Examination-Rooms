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
    """安装 01/02/03/05/08/09/10/12/13 都需要的通用包（opencv-python 固定 4.x，5.x 已移除 HOGDescriptor）

    onnxruntime：YOLO11n ONNX 检测器（02/03/11 的主力检测链）的推理依赖，
    缺它检测链会静默回落到 HOG+SVM —— 装了才能真用上 models\\yolo11n.onnx。
    scipy：匈牙利匹配跟踪（可选，缺失时自动回退贪婪最近邻）。
    cryptography：03/06/08/09 的字段级加密（Fernet）；缺它这些模块会**如实降级为明文**
                  并在界面/报告里写明未加密（不假装加密），所以能装就装。
    flask：02 网页核查面板、08 网页监控面板的服务端；缺它只能用桌面界面。
    requests：部分联动/上报脚本的可选客户端（项目主链路用标准库 urllib，不强制）。
    """
    log("安装/升级通用依赖：opencv-python<5、numpy、Pillow、cryptography、"
        "onnxruntime、scipy、flask")
    run([py, "-m", "pip", "install", "--upgrade",
         "opencv-python<5", "numpy", "Pillow", "cryptography",
         "onnxruntime", "scipy", "flask"])


def install_face(py):
    """安装 01 人脸识别依赖"""
    req = os.path.join(ROOT, "01_人脸识别与身份核对", "01_人脸识别与身份核对", "requirements-face.txt")
    if os.path.exists(req):
        log("安装 01 人脸识别依赖：%s" % req)
        run([py, "-m", "pip", "install", "-r", req])
    else:
        log("未找到 01 requirements-face.txt，跳过")


def install_05(py):
    """安装 05 考生数据保护依赖（有 requirements 就装）"""
    req = os.path.join(ROOT, "05_考生数据保护", "requirements.txt")
    if os.path.exists(req):
        log("安装 05 考生数据保护依赖：%s" % req)
        run([py, "-m", "pip", "install", "-r", req])
    else:
        log("05 无 requirements.txt，跳过（其依赖已在通用包里）")


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
        ("onnxruntime", "onnxruntime", "YOLO11n ONNX 推理（缺则检测链回落 HOG）"),
        ("cryptography", "cryptography", "字段级加密（03/06/08/09；缺则如实降级明文）"),
        ("flask", "flask", "网页面板（02 核查面板 / 08 监控面板；缺则只用桌面界面）"),
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
    log("-" * 50)
    log("模块自检（能跑就跑，个别模块缺依赖时如实跳过）")
    log("-" * 50)
    module_checks = [
        ("08 火检模块（13 融入）", "08_试卷流转与保密室智能监控",
         ["-c", "import sys; sys.path.insert(0, r'08_试卷流转与保密室智能监控'); "
                "import fire_detect; d=fire_detect.load_settings(); "
                "assert fire_detect.FireWatch and fire_detect.build_detector; "
                "print('后端:', fire_detect.describe_backend("
                "fire_detect.build_detector(d)[0]))"]),
        ("03 驾驶舱/主题", "03_智能终端操作",
         ["-c", "import sys, os; sys.path.insert(0, os.getcwd()); "
                "import theme, dashboard; "
                "page = os.path.join('web', 'cockpit.html'); "
                "assert os.path.isfile(page), '缺 web/cockpit.html'; "
                "print('主题色', theme.ACCENT, '· Py 驾驶舱 + 网页驾驶舱 OK（%d 字节页面）' "
                "% os.path.getsize(page))"]),
        ("03 数据维护（清空能力）", "03_智能终端操作",
         ["-c", "import sys, tempfile, os; "
                "os.environ['EXAM_TERMINAL_HOME']=tempfile.mkdtemp(prefix='envchk_'); "
                "sys.path.insert(0, r'03_智能终端操作'); "
                "import storage; s=storage.Store(); "
                "assert hasattr(s,'purge_data') and hasattr(s,'purge_rows'); "
                "print('清空能力 OK（范围=%d）' % len(s.PURGE_SCOPES))"]),
        ("03 跨机账号管理", "03_智能终端操作",
         ["-c", "import sys, tempfile, os; "
                "os.environ['EXAM_TERMINAL_HOME']=tempfile.mkdtemp(prefix='envchk2_'); "
                "sys.path.insert(0, r'03_智能终端操作'); "
                "import account_bridge as ab; b=ab.AccountBridge(); "
                "print('可管理模块:', '、'.join(ab.TARGETS))"]),
        ("03 跨模块态势聚合", "03_智能终端操作",
         ["-c", "import sys, tempfile, os; "
                "os.environ['EXAM_TERMINAL_HOME']=tempfile.mkdtemp(prefix='envchk3_'); "
                "sys.path.insert(0, r'03_智能终端操作'); "
                "import remote_status as rs; print('聚合模块数:', len(rs.MODULES))"]),
        ("00 设备端命令执行器", "00_集成联动",
         ["-c", "import sys; sys.path.insert(0, r'00_集成联动'); "
                "import command_runner as cr; print('支持命令:', '、'.join(cr.SUPPORTED))"]),
    ]
    for name, cwd, args in module_checks:
        d = os.path.join(ROOT, cwd)
        if not os.path.isdir(d):
            log("[SKIP] %s —— 目录不存在" % name)
            continue
        p = subprocess.run([py] + args, capture_output=True, text=True, timeout=60, cwd=d)
        if p.returncode == 0:
            log("[OK] %s —— %s" % (name, (p.stdout or "").strip().splitlines()[-1:][0]
                                   if (p.stdout or "").strip() else "通过"))
        else:
            log("[FAIL] %s —— %s" % (name, (p.stderr or "").strip().splitlines()[-1:][0]
                                     if (p.stderr or "").strip() else "失败"))
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
    install_05(py)

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
