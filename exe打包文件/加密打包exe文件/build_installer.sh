#!/usr/bin/env bash
# 构建“加密 + 可选安装路径”的安装包：
#   stub(installer.exe) + payload.enc(AES-256-GCM) + 40 字节尾部索引
#
# 用法（Git Bash）：
#   cd "exe打包文件/加密打包exe文件"
#   bash build_installer.sh
#
# 产物：
#   智能考场AI巡考系统_安装包.exe
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HERE/.." && pwd)"
PAYLOAD="$BASE/_build/payload.zip"
ICON="$BASE/_build/app.ico"
RSRC="$BASE/_build/tools/rsrc.exe"
OUT="$HERE/智能考场AI巡考系统_安装包.exe"
WORK="$HERE/.work"
PY="${PYTHON:-python}"

[ -f "$PAYLOAD" ] || { echo "缺少 payload：$PAYLOAD（先运行一键打包.sh）" >&2; exit 1; }
[ -f "$ICON" ] || { echo "缺少图标：$ICON" >&2; exit 1; }
[ -f "$RSRC" ] || { echo "缺少 rsrc：$RSRC" >&2; exit 1; }
[ -f "$HERE/installer.go" ] || { echo "缺少 installer.go" >&2; exit 1; }

rm -rf "$WORK"
mkdir -p "$WORK/src"
cp "$HERE/installer.go" "$WORK/src/main.go"
printf 'module examinstaller\n\ngo 1.20\n' > "$WORK/src/go.mod"

echo "[1/4] 嵌入图标并编译安装器 stub ..."
"$RSRC" -ico "$ICON" -o "$WORK/src/rsrc.syso" -arch amd64
(
    cd "$WORK/src"
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 \
        go build -trimpath -buildvcs=false -ldflags="-s -w" -o "$WORK/stub.base.exe" .
)
# 密钥材料：随机 32 字节；只以“异或 0x5A 后的 hex”注入 stub，构建脚本不落盘明文。
SECRET="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
KEYBLOB="$("$PY" -c 'import sys; s=bytes.fromhex(sys.argv[1]); print(bytes(b ^ 0x5A for b in s).hex())' "$SECRET")"
(
    cd "$WORK/src"
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 \
        go build -trimpath -buildvcs=false \
        -ldflags="-s -w -X main.keyBlob=$KEYBLOB" -o "$WORK/stub.exe" .
)
[ -s "$WORK/stub.exe" ] || { echo "stub 编译失败" >&2; exit 1; }

echo "[2/4] AES-256-GCM 分块加密 payload ..."
"$PY" "$HERE/encrypt_payload.py" \
    --payload "$PAYLOAD" \
    --stub "$WORK/stub.exe" \
    --out "$WORK/payload.enc" \
    --secret "$SECRET" \
    --meta "$HERE/payload.meta.json"

echo "[3/4] 追加密文与尾部索引 ..."
"$PY" - "$WORK/stub.exe" "$WORK/payload.enc" "$OUT" "$HERE/payload.meta.json" <<'PY'
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

stub, enc, out, meta_path = map(Path, sys.argv[1:5])
meta = json.loads(meta_path.read_text(encoding="utf-8"))
salt = bytes.fromhex(meta["salt"])
offset = stub.stat().st_size
enc_size = enc.stat().st_size
with open(out, "wb") as w:
    with open(stub, "rb") as r:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            w.write(b)
    with open(enc, "rb") as r:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            w.write(b)
    w.write(struct.pack("<8sQQ16s", b"ZKINS002", offset, enc_size, salt))
print("installer:", out, out.stat().st_size, "bytes")
print("offset:", offset, "enc:", enc_size)
h = hashlib.sha256()
with open(out, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print("SHA256:", h.hexdigest().upper())
PY

echo "[4/4] 完成"
ls -lh "$OUT"
rm -rf "$WORK"
