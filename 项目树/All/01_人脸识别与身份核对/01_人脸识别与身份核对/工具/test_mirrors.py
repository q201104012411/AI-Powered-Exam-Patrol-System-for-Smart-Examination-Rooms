# -*- coding: utf-8 -*-
"""测试各镜像源下载速度（各取 2MB 样本，返回字节数/耗时）"""
import time
import urllib.request

GH = "https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_l.zip"
CANDIDATES = [
    ("GitHub 直连", GH),
    ("ghproxy.net", "https://ghproxy.net/" + GH),
    ("gh-proxy.com", "https://gh-proxy.com/" + GH),
    ("ghfast.top", "https://ghfast.top/" + GH),
    ("moeyy.xyz", "https://github.moeyy.xyz/" + GH),
    ("hk.gh-proxy.com", "https://hk.gh-proxy.com/" + GH),
    ("hf-mirror 镜像", "https://hf-mirror.com/DIAMONIK7777/antelopev2/resolve/main/antelopev2.zip"),
]

SAMPLE = 2 * 1024 * 1024

for name, url in CANDIDATES:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                  "Range": "bytes=0-%d" % (SAMPLE - 1)})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(SAMPLE)
        dt = time.time() - t0
        speed = len(data) / dt / 1048576.0 if dt > 0 else 0
        code = getattr(r, "status", "?")
        print("%-18s %s  %6.2f MB/s  样本 %sB  用时 %.2fs" % (name, code, speed, len(data), dt), flush=True)
    except Exception as e:
        print("%-18s 失败: %s" % (name, str(e)[:90]), flush=True)
