#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
智能考场 AI 巡考系统 · pip 依赖「离线」一键安装器
=================================================

与 环境必要\install_pip环境.bat 的区别：
    · install_pip环境.bat  —— 联网安装（走清华源）
    · 本脚本               —— **完全离线**，只用本目录 wheelhouse\ 里的 wheel

覆盖范围（与项目各子方案一致）：
    01 人脸识别与身份核对（桌面端 + 网页操作面板后端）
    02 摄像头核查与视频质量诊断（含 HOGDescriptor / YOLO11n ONNX 检测链）
    03 智能终端操作 · 05 考生数据保护 · 06 智能安检 · 08 保密室监控
    09 无障碍保障 · 10 普通/特殊考场 · 12 告警演示 · 13 AI 火场识别
    11 摄像头 AI 识别 · 00 集成联动

用法：
    python install_offline.py              # 无参数双击 → 交互菜单
    python install_offline.py -c           # 只检查，不安装
    python install_offline.py -v           # 只做依赖校验
    python install_offline.py -o           # 额外安装"YOLO 模型导出"可选链
    python install_offline.py -f           # 强制重装 wheelhouse 内全部包
    python install_offline.py -p D:\Python312\python.exe   # 指定解释器

也可直接双击：一键离线安装.bat
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import sys
import time

# ---------------------------------------------------------------- 常量

HERE = os.path.dirname(os.path.abspath(__file__))          # 离线安装包必要环境\
PROJECT_ROOT = os.path.dirname(HERE)                       # 智能考场AI巡考系统\
WHEELHOUSE = os.path.join(HERE, "wheelhouse")
WHEELHOUSE_OPT = os.path.join(HERE, "wheelhouse_可选_模型导出")
BOOTSTRAP = os.path.join(HERE, "bootstrap")
LOCK = os.path.join(HERE, "requirements-lock.txt")
LOCK_OPT = os.path.join(HERE, "requirements-lock-可选-模型导出.txt")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "install_offline.log")

TARGET_PY = (3, 12)          # wheelhouse 按 CPython 3.12 / win_amd64 构建

# 打包时随项目附带的 Python 官方安装包（供目标机缺 Python 时使用）
PY_INSTALLER_CANDIDATES = [
    os.path.join(PROJECT_ROOT, "项目树", "All", "01_人脸识别与身份核对",
                 "01_人脸识别与身份核对", "资源", "python-3.12.4-amd64.exe"),
]
# 人脸模型（01 需要，非 pip 包，仅提示）
FACE_MODEL_CANDIDATES = [
    os.path.join(PROJECT_ROOT, "项目树", "All", "01_人脸识别与身份核对",
                 "01_人脸识别与身份核对", "资源", "buffalo_l.zip"),
]

# ---------------------------------------------------------------- 日志

_LOG_FH = None


def _init_log():
    global _LOG_FH
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _LOG_FH = open(LOG_FILE, "a", encoding="utf-8")
        _LOG_FH.write("\n" + "=" * 70 + "\n")
        _LOG_FH.write("离线依赖安装 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        _LOG_FH.write("=" * 70 + "\n")
    except Exception:
        _LOG_FH = None


def log(msg=""):
    line = str(msg)
    print(line)
    sys.stdout.flush()
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


def rule(char="-", width=70):
    log(char * width)


# ---------------------------------------------------------------- 工具

def run(cmd, timeout=1800, quiet=False, capture=False):
    """执行命令；默认把输出实时透传到本进程（保证用户看得见进度）。"""
    log("  > %s" % " ".join(str(c) for c in cmd))
    try:
        if capture:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            if p.stdout and not quiet:
                for ln in p.stdout.strip().splitlines()[-40:]:
                    log("    %s" % ln)
            if p.stderr and not quiet:
                for ln in p.stderr.strip().splitlines()[-20:]:
                    log("    %s" % ln)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        p = subprocess.run(cmd, timeout=timeout)
        return p.returncode, ""
    except subprocess.TimeoutExpired:
        log("  [超时] 命令执行超过 %d 秒" % timeout)
        return 124, ""
    except OSError as exc:
        log("  [错误] 无法执行：%s" % exc)
        return 127, ""


def py_exe():
    """当前运行本脚本的解释器（.bat 会先用 py -3.12 拉起本脚本）。"""
    return sys.executable


def has_module(py, mod):
    rc, _ = run([py, "-c", "import %s" % mod], timeout=60, quiet=True, capture=True)
    return rc == 0


# ---------------------------------------------------------------- 1. 环境体检

def check_interpreter():
    log("【1/6】检查 Python 解释器")
    rule()
    ok = True
    log("  解释器：%s" % py_exe())
    log("  版本  ：%s" % sys.version.replace("\n", " "))

    if sys.version_info[:2] != TARGET_PY:
        log("  [失败] 本离线包按 CPython %d.%d / win_amd64 构建，当前为 %d.%d"
            % (TARGET_PY[0], TARGET_PY[1], sys.version_info[0], sys.version_info[1]))
        log("         wheelhouse 内的 numpy / scipy / opencv / onnxruntime 等")
        log("         是 cp312 专用轮子，装到其它版本会失败。")
        ok = False
    if sys.version_info[:2] == TARGET_PY:
        log("  [OK] 版本匹配（%d.%d）" % TARGET_PY)

    if platform.architecture()[0] != "64bit" or sys.maxsize <= 2 ** 32:
        log("  [失败] 需要 64 位 Python（当前 %s）" % platform.architecture()[0])
        ok = False
    else:
        log("  [OK] 64 位（win_amd64 轮子匹配）")

    if platform.system() != "Windows":
        log("  [警告] 当前系统不是 Windows，wheelhouse 为 win_amd64 轮子")

    # tkinter / sqlite3 是 Python 自带，缺了 pip 也补不上
    if has_module(py_exe(), "tkinter"):
        log("  [OK] tkinter（各子方案桌面 GUI 必备）")
    else:
        log("  [失败] 该 Python 缺少 tkinter —— 请用官方安装包重装并勾选 tcl/tk")
        ok = False
    if has_module(py_exe(), "sqlite3"):
        log("  [OK] sqlite3（本地库存储）")
    else:
        log("  [失败] 该 Python 缺少 sqlite3")
        ok = False
    return ok


def report_missing_python():
    rule("=")
    log("  未找到可用的 Python 3.12。请先安装 Python（离线安装包已随项目附带）：")
    for p in PY_INSTALLER_CANDIDATES:
        if os.path.exists(p):
            log("    %s" % p)
        else:
            log("    （未找到）%s" % p)
    log("  官网：https://www.python.org/downloads/release/python-3124/")
    log("  安装时务必勾选：")
    log("    [x] Add python.exe to PATH")
    log("    [x] tcl/tk and IDLE")
    rule("=")


# ---------------------------------------------------------------- 2. 资源自检

def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_hashes(manifest):
    """按 清单_SHA256.txt 核对 wheel 的 sha256。

    返回 (缺失列表, 损坏列表, 已核对数)。
    对「可选链 / bootstrap 未随包拷贝」的情况只算缺失，不算损坏。
    """
    missing, corrupt, checked = [], [], 0
    try:
        with open(manifest, encoding="utf-8") as f:
            rows = [l.rstrip("\n") for l in f
                    if l.strip() and not l.startswith("#")]
    except Exception as exc:
        log("  [警告] 无法读取完整性清单：%s" % exc)
        return [], [], 0

    for row in rows:
        parts = row.split("  ")
        if len(parts) != 3:
            continue
        digest, _size, rel = parts
        p = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            missing.append(rel)
            continue
        checked += 1
        if _sha256(p) != digest:
            corrupt.append(rel)
    return missing, corrupt, checked


def check_payload():
    log("")
    log("【2/6】检查离线资源完整性")
    rule()
    ok = True

    if not os.path.isdir(WHEELHOUSE):
        log("  [失败] 缺少 wheelhouse 目录：%s" % WHEELHOUSE)
        return False
    wheels = [f for f in os.listdir(WHEELHOUSE) if f.endswith(".whl")]
    size = sum(os.path.getsize(os.path.join(WHEELHOUSE, f)) for f in wheels)
    log("  [OK] wheelhouse：%d 个 wheel，%.1f MB" % (len(wheels), size / 1048576.0))
    if len(wheels) < 20:
        log("  [失败] wheel 数量异常偏少，离线包可能不完整")
        ok = False

    if os.path.exists(LOCK):
        with open(LOCK, encoding="utf-8") as f:
            need = [l.strip() for l in f
                    if l.strip() and not l.strip().startswith("#")]
        log("  [OK] requirements-lock.txt：%d 条锁定依赖" % len(need))
    else:
        log("  [失败] 缺少 requirements-lock.txt")
        ok = False

    boot = [f for f in os.listdir(BOOTSTRAP) if f.endswith(".whl")] \
        if os.path.isdir(BOOTSTRAP) else []
    log("  [OK] bootstrap：%d 个 wheel（离线修复 pip 用）" % len(boot))

    if os.path.isdir(WHEELHOUSE_OPT):
        optn = len([f for f in os.listdir(WHEELHOUSE_OPT) if f.endswith(".whl")])
        log("  [OK] 可选链 wheelhouse_可选_模型导出：%d 个 wheel" % optn)

    # 逐字节完整性核对（拷贝过程中被截断 / 改坏能查出来）
    manifest = os.path.join(HERE, "清单_SHA256.txt")
    if os.path.exists(manifest):
        missing, corrupt, checked = verify_hashes(manifest)
        core_missing = [m for m in missing if m.startswith("wheelhouse/")]
        if corrupt:
            log("  [失败] %d 个文件 SHA256 与清单不符（拷贝损坏 / 被篡改）："
                % len(corrupt))
            for m in corrupt[:10]:
                log("         %s" % m)
            ok = False
        if core_missing:
            log("  [失败] wheelhouse 内 %d 个文件缺失：%s"
                % (len(core_missing), "、".join(core_missing[:6])))
            ok = False
        if not corrupt and not core_missing:
            log("  [OK] SHA256 完整性核对通过（%d 个文件%s）"
                % (checked, "，可选链/bootstap 有缺失但不影响核心安装"
                   if missing else ""))
    else:
        log("  [提示] 未找到 清单_SHA256.txt，跳过完整性核对")
    return ok


# ---------------------------------------------------------------- 3. pip

def ensure_pip(py):
    log("")
    log("【3/6】检查 / 离线修复 pip")
    rule()
    rc, out = run([py, "-m", "pip", "--version"], capture=True, quiet=True)
    if rc == 0:
        log("  [OK] %s" % out.strip().splitlines()[0])
        rc2, _ = run([py, "-m", "pip", "install", "--no-index",
                      "--find-links", BOOTSTRAP, "--upgrade",
                      "pip", "setuptools", "wheel"],
                     capture=True, quiet=True)
        if rc2 != 0:
            log("  [提示] bootstrap 升级 pip 未成功（不影响后续安装）")
        return True

    log("  [修复] pip 缺失，使用本地 bootstrap 离线补装 ...")
    if os.path.isdir(BOOTSTRAP) and any(f.endswith(".whl") for f in os.listdir(BOOTSTRAP)):
        run([py, "-m", "pip", "install", "--no-index", "--find-links", BOOTSTRAP,
             "pip", "setuptools", "wheel"], capture=True)
    if run([py, "-m", "pip", "--version"], capture=True, quiet=True)[0] != 0:
        log("  [修复] 改用 ensurepip ...")
        run([py, "-m", "ensurepip", "--upgrade"], capture=True)
    if run([py, "-m", "pip", "--version"], capture=True, quiet=True)[0] == 0:
        log("  [OK] pip 已可用")
        return True
    log("  [失败] pip 无法使用，请用官方安装包重装 Python 并勾选 pip")
    return False


# ---------------------------------------------------------------- 4. 安装

def install_core(py, force=False):
    log("")
    log("【4/6】离线安装核心依赖（--no-index，全程不联网）")
    rule()

    # opencv-python-headless 不含 HOGDescriptor，是 02/03/11 AI 框选识别失效的头号原因
    log("  预清理：卸载 opencv-python-headless / opencv-contrib（避免 HOGDescriptor 缺失）")
    run([py, "-m", "pip", "uninstall", "-y",
         "opencv-python-headless", "opencv-contrib-python-headless",
         "opencv-contrib-python"], capture=True, quiet=True)

    cmd = [py, "-m", "pip", "install",
           "--no-index",
           "--find-links", WHEELHOUSE,
           "--upgrade",
           "--no-warn-script-location",
           "-r", LOCK]
    if force:
        cmd.append("--force-reinstall")
    rc, _ = run(cmd, timeout=3600)
    if rc != 0:
        log("  [警告] 批量安装返回码 %d，请向上翻看具体报错" % rc)
    return rc == 0


def install_optional(py):
    log("")
    log("【附加】离线安装可选链：YOLO11n → ONNX 模型导出（ultralytics + torch）")
    rule()
    if not os.path.exists(LOCK_OPT):
        log("  [跳过] 未找到 requirements-lock-可选-模型导出.txt")
        return True
    log("  说明：仅在需要用 ultralytics 重新导出 models\\yolo11n.onnx 时才需要；")
    log("        项目已自带 yolo11n.onnx，日常运行**不需要**这一可选链。")
    rc, _ = run([py, "-m", "pip", "install", "--no-index",
                 "--find-links", WHEELHOUSE_OPT, "--upgrade",
                 "--no-warn-script-location", "-r", LOCK_OPT], timeout=3600)
    return rc == 0


# ---------------------------------------------------------------- 5. 校验

CHECKS = [
    ("numpy",        "numpy",                      "数值计算（01/02/03/…）"),
    ("cv2",          "opencv-python 4.x",          "取流 + 图像处理"),
    ("PIL",          "Pillow",                     "画面显示 / 图片转换"),
    ("scipy",        "scipy",                      "匈牙利匹配跟踪"),
    ("onnx",         "onnx",                       "onnx 图解析（insightface 依赖）"),
    ("onnxruntime",  "onnxruntime",                "ONNX 推理（人脸 + YOLO11n 检测链）"),
    ("insightface",  "insightface",                "人脸检测/对齐/识别"),
    ("flask",        "flask",                      "网页操作面板 / 网页核查面板"),
    ("flask_cors",   "flask-cors",                 "网页跨域"),
    ("flask_limiter", "flask-limiter",             "接口限流"),
    ("jwt",          "PyJWT",                      "JWT 令牌"),
    ("bcrypt",       "bcrypt",                     "口令哈希"),
    ("cryptography", "cryptography",               "字段级加密（缺则如实降级明文）"),
    ("openpyxl",     "openpyxl",                   "Excel 名册 / 模板"),
    ("docx",         "python-docx",                "Word 项目书 / 报告"),
    ("gmssl",        "gmssl",                      "国密 SM2/SM3/SM4（05）"),
]

OPTIONAL_CHECKS = [
    ("serial", "pyserial", "06 安检门串口接入（未装则界面如实显示不可用）"),
]


def verify(py):
    log("")
    log("【5/6】依赖校验")
    rule()
    fail = 0
    for mod, pkg, desc in CHECKS:
        if has_module(py, mod):
            log("  [OK]   %-18s %s" % (pkg, desc))
        else:
            log("  [失败] %-18s %s  —— 缺失" % (pkg, desc))
            fail += 1
    for mod, pkg, desc in OPTIONAL_CHECKS:
        if has_module(py, mod):
            log("  [OK]   %-18s %s" % (pkg, desc))
        else:
            log("  [可选] %-18s %s" % (pkg, desc))

    # 02/03/11 AI 框选识别必要条件
    code = ("import cv2, sys;"
            "ok = hasattr(cv2,'HOGDescriptor') and "
            "hasattr(cv2,'HOGDescriptor_getDefaultPeopleDetector');"
            "print('cv2', cv2.__version__); sys.exit(0 if ok else 1)")
    rc, out = run([py, "-c", code], capture=True, quiet=True)
    if rc == 0:
        log("  [OK]   %-18s %s" % ("HOGDescriptor", out.strip().splitlines()[0]))
    else:
        log("  [失败] %-18s 02/03/11 的 AI 框选识别将不可用（%s）" % ("HOGDescriptor", out.strip()))
        fail += 1

    # 关键版本快照
    log("")
    log("  已安装版本快照：")
    rc, out = run([py, "-m", "pip", "list", "--format=freeze"],
                  capture=True, quiet=True, timeout=120)
    if rc == 0:
        want = ("numpy", "opencv", "pillow", "insightface", "onnxruntime", "scipy",
                "flask", "pyjwt", "bcrypt", "cryptography", "openpyxl", "python-docx",
                "gmssl", "pyserial", "onnx", "torch", "ultralytics")
        for line in sorted(out.splitlines()):
            if line.lower().startswith(want):
                log("    %s" % line)
    return fail


# ---------------------------------------------------------------- 6. 收尾提示

def next_steps(py):
    log("")
    log("【6/6】后续步骤")
    rule()
    face_pkg = None
    for p in FACE_MODEL_CANDIDATES:
        if os.path.exists(p):
            face_pkg = p
            break
    log("  ① 人脸模型（非 pip 包，需手工解压一次）：")
    if face_pkg:
        log("     把 %s" % face_pkg)
    else:
        log("     把 buffalo_l.zip")
    log("     解压到 %s\\.insightface\\models\\buffalo_l"
        % os.path.expanduser("~"))
    log("     （insightface 会自动到该目录查找，无需改代码）")
    log("")
    all_dir = os.path.join(PROJECT_ROOT, "项目树", "All")
    for tip in ("02 启动前自检：   %s\\02_摄像头核查与视频质量诊断\\启动前自检.bat" % all_dir,
                "02 桌面核查端：   %s\\02_摄像头核查与视频质量诊断\\启动核查界面.bat" % all_dir,
                "03 智能终端：     %s\\03_智能终端操作\\启动终端界面.bat" % all_dir,
                "01 网页后端：     %s\\01_人脸识别与身份核对\\01_人脸识别与身份核对\\网页操作面板\\打开网页.bat" % all_dir,
                "11 摄像头AI识别： %s\\11_摄像头AI识别\\启动.bat" % all_dir,
                "13 AI火场识别：   %s\\13_AI火场识别与自动报警\\启动.bat" % all_dir):
        log("      %s" % tip)
    log("")
    log("  ② 其它非 pip 环境（已随项目附带，无需联网）：")
    log("     · Ollama     大模型本地部署\\OllmaSetup.exe")
    log("     · go2rtc     摄像头\\小米摄像头go2rtc\\bin\\go2rtc.exe")
    log("     · YOLO11n    项目树\\All\\02_摄像头核查与视频质量诊断\\models\\yolo11n.onnx（已自带）")
    log("")
    log("  ③ 日志：%s" % LOG_FILE)


# ---------------------------------------------------------------- 交互菜单

MENU = """
------------------------------------------------------------
 请选择操作：

   [1] 一键离线安装全部依赖（推荐，含自动校验）
   [2] 只检查环境与离线包完整性（不安装）
   [3] 只做依赖校验（装完后复检）
   [4] 一键离线安装 +「YOLO 模型导出」可选链
       （ultralytics + torch，约 264 MB；日常运行不需要）
   [5] 强制重装 wheelhouse 内全部包
   [0] 退出
------------------------------------------------------------"""


def interactive_menu():
    """双击 一键离线安装.bat 时进入；返回要传给 argparse 的参数列表，退出返回 None。

    若第一次询问就读到 EOF（非交互环境 / 输入被重定向），退化为"一键全装"，
    避免脚本静默什么都不做。
    """
    first = True
    while True:
        print(MENU)
        try:
            ch = input("请输入序号后回车：").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            if first:
                print("[提示] 未检测到交互输入，默认执行「一键离线安装全部依赖」。")
                return []
            return None
        first = False
        if ch == "1":
            return []
        if ch == "2":
            return ["--check"]
        if ch == "3":
            return ["--verify"]
        if ch == "4":
            return ["--optional"]
        if ch == "5":
            return ["--force"]
        if ch in ("0", "q", "Q"):
            return None
        print("\n[提示] 输入无效，请重新选择。\n")


# ---------------------------------------------------------------- main

def main():
    if len(sys.argv) == 1:
        try:
            tty = sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            tty = False
        if tty:
            picked = interactive_menu()
            if picked is None:
                return 0
            sys.argv = [sys.argv[0]] + picked
            print("")

    ap = argparse.ArgumentParser(
        description="智能考场 AI 巡考系统 · pip 依赖离线安装器")
    ap.add_argument("-c", "--check", action="store_true",
                    help="只检查环境与离线包，不安装")
    ap.add_argument("-v", "--verify", action="store_true", help="只做依赖校验")
    ap.add_argument("-o", "--optional", action="store_true",
                    help="额外安装 YOLO 模型导出可选链（ultralytics/torch）")
    ap.add_argument("-f", "--force", action="store_true",
                    help="强制重装 wheelhouse 内全部包")
    ap.add_argument("-p", "--python", default=None, help="指定解释器路径（默认当前解释器）")
    ns = ap.parse_args()

    _init_log()
    log("=" * 70)
    log(" 智能考场 AI 巡考系统 · pip 依赖离线安装器")
    log(" 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log(" 离线包：%s" % HERE)
    log(" 项目根：%s" % PROJECT_ROOT)
    log(" 解释器：%s" % py_exe())
    log("=" * 70)

    if ns.python:
        # 允许用户显式指定解释器：用子进程重跑本脚本
        alt = os.path.abspath(ns.python)
        if not os.path.exists(alt):
            log("[错误] 指定的解释器不存在：%s" % alt)
            return 1
        if os.path.normcase(alt) != os.path.normcase(py_exe()):
            # 剥掉 -p/--python <值>（两种写法），其余参数原样透传给子进程
            rest, skip = [], False
            for a in sys.argv[1:]:
                if skip:
                    skip = False
                    continue
                if a in ("--python", "-p"):
                    skip = True
                    continue
                if a.startswith("--python=") or a.startswith("-p="):
                    continue
                rest.append(a)
            argv = [alt, os.path.abspath(__file__)] + rest
            log("[切换] 使用指定解释器重新执行：%s" % alt)
            return subprocess.call(argv)
        return 1

    if ns.verify:
        fail = verify(py_exe())
        log("")
        log("校验结束：%s" % ("全部通过" if fail == 0 else "有 %d 项失败" % fail))
        return 0 if fail == 0 else 1

    interp_ok = check_interpreter()
    payload_ok = check_payload()

    if not interp_ok:
        log("")
        report_missing_python()
        return 1
    if not payload_ok:
        log("")
        log("[中止] 离线资源不完整，请重新获取完整的「离线安装包必要环境」目录。")
        return 1
    if ns.check:
        log("")
        log("[--check] 检查完成，未做任何改动。")
        return 0

    if not ensure_pip(py_exe()):
        return 1

    core_ok = install_core(py_exe(), force=ns.force)
    if ns.optional:
        install_optional(py_exe())

    fail = verify(py_exe())

    log("")
    log("=" * 70)
    if core_ok and fail == 0:
        log(" 离线环境安装完成，全部校验通过！")
    else:
        log(" 离线环境安装结束，但存在未通过项（见上方 [失败]），日志：")
        log("   %s" % LOG_FILE)
    log("=" * 70)

    next_steps(py_exe())
    log("")
    return 0 if (core_ok and fail == 0) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.exit(main())
    finally:
        if _LOG_FH is not None:
            try:
                _LOG_FH.close()
            except Exception:
                pass
