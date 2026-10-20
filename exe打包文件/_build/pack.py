# -*- coding: utf-8 -*-
"""把 exe打包文件 压成单文件 exe 的 payload.zip（排除构建目录/缓存/日志）。"""
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_build", "payload.zip")

INCLUDES = [
    "总控台.py", "launcher_auth.py", "模块清单.json", "icon",
    "sso.py", "ui_theme.py",
    "runtime",
    "00_集成联动", "01_人脸识别与身份核对", "02_摄像头核查与视频质量诊断",
    "03_智能终端操作", "04_巡考机器人", "05_考生数据保护",
    "06_智能安检与手机作弊器材检测", "07_无线信号监测与屏蔽管控",
    "08_试卷流转与保密室智能监控", "09_无障碍与特殊考生保障",
    "10_普通考场+特殊考场应用", "11_告警上传与终端处置演示",
    "README.md", "install_env.py", "安装环境.bat",
]

EXCLUDE_DIR_NAMES = {"__pycache__", "_build", ".git", ".idea",
                     "data_backup_20261015_175027"}
# 相对包根的排除片段（用 / 分隔）
EXCLUDE_SUBSTR = (
    "/data/logs/", "/data/reports/", "/data/attachments/",
    "/data/captures/", "/runtime/Scripts/", "/runtime/Include/",
    # 05 的密钥环用 Windows DPAPI 加密，换机器/换 Windows 账户就解不开；
    # 不随包分发，首次运行时由模块自己生成当前用户可用的密钥。
    "/05_考生数据保护/data/",
)
EXCLUDE_FILES = {"总控台账户.json", "总控台审计.jsonl", "sso.key",
                 "_融入火检到02.py"}
EXCLUDE_EXT = (".pyc", ".pyo", ".log", ".tmp", ".pdb")


def skip(rel, is_dir):
    rel_u = rel.replace("\\", "/")
    parts = rel_u.split("/")
    if any(p in EXCLUDE_DIR_NAMES for p in parts):
        return True
    # 任何模块自建的数据备份目录都不入包（含密钥/历史数据）
    if any(p.startswith("data_backup_") for p in parts):
        return True
    if is_dir:
        return False
    if os.path.basename(rel_u) in EXCLUDE_FILES:
        return True
    if rel_u.lower().endswith(EXCLUDE_EXT):
        return True
    for s in EXCLUDE_SUBSTR:
        if s in "/" + rel_u + "/":
            return True
    return False


def main():
    files = []
    for inc in INCLUDES:
        p = os.path.join(ROOT, inc)
        if not os.path.exists(p):
            print("[WARN] missing include:", inc)
            continue
        if os.path.isfile(p):
            rel = inc
            if not skip(rel, False):
                files.append(rel)
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = sorted(d for d in dirnames
                                 if not skip(os.path.relpath(
                                     os.path.join(dirpath, d), ROOT), True))
            for fn in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
                if not skip(rel, False):
                    files.append(rel)
    files = sorted(set(files))
    raw = sum(os.path.getsize(os.path.join(ROOT, f)) for f in files)
    print("files=%d raw=%.1f MB -> %s" % (len(files), raw / 1024.0 / 1024.0, OUT))

    t0 = time.time()
    written = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9, allowZip64=True) as z:
        for i, rel in enumerate(files, 1):
            z.write(os.path.join(ROOT, rel), rel.replace("\\", "/"))
            written += 1
            if i % 500 == 0:
                print("  %d/%d  %.0f MB  %.0fs" % (
                    i, len(files),
                    os.path.getsize(OUT) / 1024.0 / 1024.0,
                    time.time() - t0), flush=True)
    print("done: %d files, %.1f MB, %.1fs" % (
        written, os.path.getsize(OUT) / 1024.0 / 1024.0, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
