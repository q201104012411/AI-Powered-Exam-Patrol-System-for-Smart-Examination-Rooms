# -*- coding: utf-8 -*-
"""把 payload.zip 加密成安装包用的 payload.enc（AES-256-GCM 分块）。

密钥 = SHA256(secret + salt + SHA256(stub.exe))。
stub 由 Go installer 构建；加密后把 payload.enc 追加到 stub 末尾，再加 40 字节尾部索引。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import struct
import sys
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ZKENC003"
CHUNK = 1 << 20


def sha256_file(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.digest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--stub", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--secret", required=True, help="32 字节密钥材料（hex）")
    ap.add_argument("--meta", default="")
    args = ap.parse_args()

    secret = bytes.fromhex(args.secret)
    if len(secret) != 32:
        raise SystemExit("secret 必须是 32 字节")
    salt = secrets.token_bytes(16)
    nonce_prefix = secrets.token_bytes(8)
    stub_hash = sha256_file(args.stub)
    key = hashlib.sha256(secret + salt + stub_hash).digest()
    gcm = AESGCM(key)

    total = os.path.getsize(args.payload)
    print("payload %.1f MB, stub %d bytes" % (total / 1048576.0, os.path.getsize(args.stub)))
    started = time.time()
    done = 0
    with open(args.payload, "rb") as src, open(args.out, "wb") as dst:
        dst.write(struct.pack("<8sI8s", MAGIC, CHUNK, nonce_prefix))
        i = 0
        while True:
            plain = src.read(CHUNK)
            if not plain:
                break
            nonce = nonce_prefix + struct.pack(">I", i)
            ct = gcm.encrypt(nonce, plain, None)
            dst.write(struct.pack("<I", len(ct)))
            dst.write(ct)
            done += len(plain)
            i += 1
            if i % 32 == 0 or done == total:
                pct = done * 100.0 / max(total, 1)
                print("\r加密 %5.1f%%  %.1f/%.1f MB" % (pct, done / 1048576.0, total / 1048576.0),
                      end="", flush=True)
    print()
    meta = {
        "salt": salt.hex(),
        "nonce_prefix": nonce_prefix.hex(),
        "stub_sha256": stub_hash.hex(),
        "chunk": CHUNK,
        "enc_size": os.path.getsize(args.out),
        "payload_size": total,
        "seconds": round(time.time() - started, 1),
    }
    if args.meta:
        with open(args.meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    print("encrypted:", args.out, os.path.getsize(args.out), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
