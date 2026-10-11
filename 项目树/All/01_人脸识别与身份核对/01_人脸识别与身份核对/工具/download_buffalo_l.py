# -*- coding: utf-8 -*-
"""
下载 insightface buffalo_l 模型包（带断点续传与 ZIP 完整性校验）。

说明：本机已有一份完整模型包放在 ..\\资源\\buffalo_l.zip，正常无需再跑本脚本；
     只有换机器/重新拉取时才用得上。GitHub 直连在国内可能很慢（实测约 0.02MB/s），
      可先跑 test_mirrors.py 找一个快的镜像，再改 URL。
用法：python download_buffalo_l.py
"""
import os
import sys
import time
import zipfile
import urllib.request

URL = "https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_l.zip"
# 默认下到同级的 ..\资源\ 目录（相对本脚本定位，换目录也不会失效）
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "资源"))
OUT = os.path.join(OUT_DIR, "buffalo_l.zip")

os.makedirs(OUT_DIR, exist_ok=True)

# 期望大小（来自 HEAD 请求）
EXPECTED = 288621354


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def download():
    for attempt in range(1, 6):
        have = os.path.getsize(OUT) if os.path.exists(OUT) else 0
        if have >= EXPECTED:
            print("已下载完整文件：%s" % human(have))
            return True
        headers = {"User-Agent": "Mozilla/5.0"}
        if have:
            headers["Range"] = "bytes=%d-" % have
            print("第 %d 次尝试：从 %s 处续传" % (attempt, human(have)))
        else:
            print("第 %d 次尝试：开始下载" % attempt)
        try:
            req = urllib.request.Request(URL, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp, open(OUT, "ab" if have else "wb") as f:
                total = EXPECTED
                got = have
                t0 = time.time()
                last = t0
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if now - last >= 5:
                        speed = (got - have) / max(now - t0, 0.001)
                        pct = got * 100.0 / total if total else 0
                        eta = (total - got) / speed if speed > 0 else 0
                        print("  %5.1f%%  %s / %s  %.2f MB/s  剩余约 %d 秒"
                              % (pct, human(got), human(total), speed / 1048576.0, eta), flush=True)
                        last = now
            size = os.path.getsize(OUT)
            print("本次结束：文件大小 %s" % human(size))
            if size >= EXPECTED:
                return True
        except Exception as e:
            print("  出错：%s" % e, flush=True)
            time.sleep(3)
    return os.path.exists(OUT) and os.path.getsize(OUT) >= EXPECTED


ok = download()
if not ok:
    print("FAILED: 下载未完成")
    sys.exit(1)

# 完整性校验
print("--- 校验 ---")
size = os.path.getsize(OUT)
print("文件：%s" % OUT)
print("大小：%s (%d 字节)" % (human(size), size))
with zipfile.ZipFile(OUT) as z:
    bad = z.testzip()
    if bad:
        print("FAILED: ZIP 内文件损坏 -> %s" % bad)
        sys.exit(1)
    names = z.namelist()
print("ZIP 校验通过，含 %d 个文件：" % len(names))
for n in names:
    info = None
    with zipfile.ZipFile(OUT) as z:
        info = z.getinfo(n)
    print("  %-28s %10s" % (n, human(info.file_size)))
print("OK")
