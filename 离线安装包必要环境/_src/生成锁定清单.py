# -*- coding: utf-8 -*-
"""从 wheelhouse 生成：
   1) requirements-lock.txt / requirements-lock-可选-模型导出.txt（全量锁定）
   2) 清单_SHA256.txt（完整性校验清单，供 install_offline.py 核对）
"""
import hashlib
import os
import sys

try:
    from packaging.utils import parse_wheel_filename
except Exception:                                    # packaging 缺失时的兜底解析
    parse_wheel_filename = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HEADER = """# ============================================================
#  智能考场 AI 巡考系统 · 离线依赖全量锁定清单（自动生成，勿手改）
#  平台：Windows x86_64 / CPython 3.12
#  内容：直接依赖 + 全部传递依赖，共 {n} 个发行包
#  安装：pip install --no-index --find-links=wheelhouse -r requirements-lock.txt
#  重新生成：python _src\\生成锁定清单.py
# ============================================================
"""


def parse(path):
    """返回 (规范化名, 版本)，兼容带连字符的发行名。"""
    name = os.path.basename(path)
    if parse_wheel_filename is not None:
        n, v, _b, _t = parse_wheel_filename(name)
        return str(n), str(v)
    stem = name[:-4] if name.endswith(".whl") else name
    parts = stem.split("-")
    return parts[0].replace("_", "-"), parts[1]


def collect(folder):
    out = {}
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder)):
        if fn.endswith(".whl"):
            n, v = parse(os.path.join(folder, fn))
            out[n.lower()] = (n, v, fn)
    return out


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_hash_manifest():
    """写 清单_SHA256.txt：相对路径 + 大小 + sha256，供离线包完整性核对。"""
    lines = ["# 智能考场 AI 巡考系统 · 离线安装包完整性清单（自动生成，勿手改）",
             "# 格式：<sha256>  <字节数>  <相对路径>",
             "# 核对：python install_offline.py -c   （会自动比对本清单）",
             ""]
    n = 0
    total = 0
    for folder in ("wheelhouse", "wheelhouse_可选_模型导出", "bootstrap"):
        d = os.path.join(ROOT, folder)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            lines.append("%s  %d  %s/%s" % (sha256_of(p), size, folder, fn))
            n += 1
            total += size
    lines.append("")
    with open(os.path.join(ROOT, "清单_SHA256.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("清单_SHA256.txt：%d 个文件，%.1f MB" % (n, total / 1048576.0))


def main():
    pkgs = collect(os.path.join(ROOT, "wheelhouse"))
    opt_dir = os.path.join(ROOT, "wheelhouse_可选_模型导出")
    opt = collect(opt_dir) if os.path.isdir(opt_dir) else {}

    lines = [HEADER.format(n=len(pkgs))]
    for key in sorted(pkgs):
        n, v, _fn = pkgs[key]
        lines.append("%s==%s" % (n, v))
    lines.append("")

    with open(os.path.join(ROOT, "requirements-lock.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    print("requirements-lock.txt：%d 个包" % len(pkgs))

    if opt:
        olines = ["# 可选：YOLO11n -> ONNX 导出工具链（仅重新导出模型时需要）",
                  "# 安装：pip install --no-index --find-links=wheelhouse_可选_模型导出 -r 本文件", ""]
        for key in sorted(opt):
            n, v, _fn = opt[key]
            olines.append("%s==%s" % (n, v))
        olines.append("")
        with open(os.path.join(ROOT, "requirements-lock-可选-模型导出.txt"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(olines))
        print("requirements-lock-可选-模型导出.txt：%d 个包" % len(opt))

    write_hash_manifest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
