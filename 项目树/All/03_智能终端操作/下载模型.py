# -*- coding: utf-8 -*-
r"""
摄像头 AI 识别 · 模型下载

作用：从 ultralytics **官方 release** 下载预导出的 ONNX 权重并校验 SHA256，
      放到 models\ 后检测链会自动启用（无需改配置/界面）。

与「工具_YOLO模型导出.py」的分工：
  · 本脚本  ：下载官方**预导出**的 .onnx（不装 ultralytics 也能用，带哈希校验）；
  · 导出工具：本机用 ultralytics 从 .pt 重新导出（可换 yolo11s 等，需要联网下 .pt）。

如实说明：当前检测链（person_detect.py）只解码 COCO 的 **person** 类做
人形框选与行为判定，既不使用姿态（pose）模型，也不做手机类识别；
因此本脚本只提供检测（detect）模型。

用法：
  python 下载模型.py                  # 下载默认 yolo11n.onnx（推荐）
  python 下载模型.py --check          # 只检查本机已有模型的完整性
  python 下载模型.py --list           # 列出可下载的模型与哈希
  python 下载模型.py --force          # 即使已存在也重新下载
"""
import argparse
import hashlib
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "models")
BASE = "https://github.com/ultralytics/assets/releases/download/v8.4.0/"

# 官方 release 资产 + 官方发布的 SHA256（下载后逐字节校验，避免拿到被篡改/半截的文件）
MODELS = {
    "yolo11n.onnx": {
        "url": BASE + "yolo11n.onnx",
        "sha256": "634279b40c07c6391472c51ad45b81ebc48706a9a1fe72dd3396322acd0c053b",
        "size_mb": 10.9,
        "desc": "YOLO11n 检测模型（COCO 80 类，person=第 0 类）——主力检测器",
    },
    "yolo11s.onnx": {
        "url": BASE + "yolo11s.onnx",
        "sha256": "21d6650c5097610c92c76ce5e4b717976059169eaea4962035b90c6a92c07a8f",
        "size_mb": 36.3,
        "desc": "YOLO11s 检测模型（更准、更慢）",
    },
}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def report(msg):
    print("[模型] %s" % msg, flush=True)


def download(name, item, force=False):
    os.makedirs(MODEL_DIR, exist_ok=True)
    dst = os.path.join(MODEL_DIR, name)
    if os.path.exists(dst) and not force:
        got = sha256_of(dst)
        if item["sha256"] and got == item["sha256"]:
            report("%s 已存在且校验通过，跳过" % name)
            return True
        report("%s 已存在但校验不符，重新下载" % name)

    report("正在下载 %s（约 %.1f MB，网络慢时会等一会）…" % (name, item["size_mb"]))
    tmp = dst + ".part"
    t0 = time.time()
    try:
        with urllib.request.urlopen(item["url"], timeout=120) as resp, \
                open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100.0 * done / total
                    sys.stdout.write("\r  %.1f%%  (%.1f/%.1f MB)"
                                     % (pct, done / 1e6, total / 1e6))
                    sys.stdout.flush()
        sys.stdout.write("\n")
    except Exception as e:
        report("下载失败：%s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        report("可手动下载后放到：%s" % os.path.join(MODEL_DIR, name))
        report("  地址：%s" % item["url"])
        return False

    got = sha256_of(tmp)
    if item["sha256"] and got != item["sha256"]:
        report("❌ 校验失败，已丢弃。期望 %s，实际 %s"
               % (item["sha256"][:16], got[:16]))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    os.replace(tmp, dst)
    report("✅ %s 下载完成，用时 %.1f 秒%s"
           % (name, time.time() - t0,
              "，SHA256 校验通过" if item["sha256"] else ""))
    return True


def check():
    print("模型目录：%s" % MODEL_DIR)
    found = []
    if os.path.isdir(MODEL_DIR):
        for f in sorted(os.listdir(MODEL_DIR)):
            if f.lower().endswith(".onnx"):
                found.append(f)
    if not found:
        print("  目录下没有任何 .onnx 模型")
    for f in found:
        p = os.path.join(MODEL_DIR, f)
        got = sha256_of(p)
        meta = MODELS.get(f)
        ok = ""
        if meta and meta.get("sha256"):
            ok = "✅ 与官方一致" if got == meta["sha256"] else "⚠ 与官方 release 不一致"
        print("  %-24s %6.1f MB  %s  %s"
              % (f, os.path.getsize(p) / 1e6, got[:16], ok))
    print()
    print("检测链默认用 models\\yolo11n.onnx（COCO person 类，人形框选）。")
    print("  注：若该文件是用「工具_YOLO模型导出.py」本地导出的（simplify=True），")
    print("      哈希与官方 release 不一致属正常，能正常推理即可。")


def main():
    ap = argparse.ArgumentParser(description="下载 YOLO ONNX 模型（官方 release + SHA256 校验）")
    ap.add_argument("--check", action="store_true", help="只检查已有模型")
    ap.add_argument("--list", action="store_true", help="列出可下载模型")
    ap.add_argument("--force", action="store_true", help="即使已存在也重新下载")
    args = ap.parse_args()

    print("=" * 74)
    print("摄像头 AI 识别 · 模型下载")
    print("=" * 74)

    if args.list:
        for n, it in MODELS.items():
            print("  %-22s %6.1f MB  %s" % (n, it["size_mb"], it["desc"]))
            print("      %s" % it["url"])
        return 0
    if args.check:
        check()
        return 0

    names = ["yolo11n.onnx"]

    ok = True
    for n in names:
        ok = download(n, MODELS[n], force=args.force) and ok

    print()
    check()
    print()
    if ok:
        print("完成。把模型放在这里后程序会**自动启用**，无需改配置：")
        print("  %s" % MODEL_DIR)
        print("启动界面后，AI 页顶部会显示当前检测链（YOLO11n-ONNX / HOG+SVM）。")
    else:
        print("有模型未下载成功，可重跑本脚本，或按上面的地址手动下载。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
