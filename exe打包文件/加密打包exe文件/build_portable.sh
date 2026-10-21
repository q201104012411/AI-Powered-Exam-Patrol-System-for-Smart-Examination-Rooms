#!/usr/bin/env bash
# 构建“加密免安装版”单文件 exe：launcher + payload.enc + 40 字节尾部索引。
# 7-Zip 打开只会看到密文数据，不会列出目录/文件；首次运行解密到本地缓存，
# 之后复用缓存，运行速度与明文版一致。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$HERE/.." && pwd)"
LAUNCHER_SRC="$BASE/_build/launcher/main.go"
PAYLOAD="$BASE/_build/payload.zip"
ICON="$BASE/_build/app.ico"
RSRC="$BASE/_build/tools/rsrc.exe"
OUT="$BASE/智能考场AI巡考系统.exe"
WORK="$BASE/_build/portable_build"
PY="${PYTHON:-python}"

[ -f "$LAUNCHER_SRC" ] || { echo "缺少 launcher：$LAUNCHER_SRC" >&2; exit 1; }
[ -f "$PAYLOAD" ] || { echo "缺少 payload：$PAYLOAD" >&2; exit 1; }
[ -f "$ICON" ] || { echo "缺少图标：$ICON" >&2; exit 1; }
[ -f "$RSRC" ] || { echo "缺少 rsrc：$RSRC" >&2; exit 1; }

rm -rf "$WORK"
mkdir -p "$WORK/src"
cp "$LAUNCHER_SRC" "$WORK/src/main.go"
printf 'module examlauncher\n\ngo 1.20\n' > "$WORK/src/go.mod"
# goversioninfo 生成的 .syso 已包含图标与版本资源；再叠加 rsrc.syso 会报 too many .rsrc sections
cp -f "$BASE/_build/version_launcher.syso" "$WORK/src/version.syso"

SECRET="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
KEYBLOB="$("$PY" -c 'import sys; s=bytes.fromhex(sys.argv[1]); print(bytes(b ^ 0x5A for b in s).hex())' "$SECRET")"

echo "[1/3] 编译加密启动器 ..."
(
    cd "$WORK/src"
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 \
        go build -trimpath -buildvcs=false \
        -ldflags="-s -w -X main.keyBlob=$KEYBLOB" -o "$WORK/stub.exe" .
)
sleep 1
sync 2>/dev/null || true
[ -s "$WORK/stub.exe" ] || { echo "launcher 编译失败" >&2; exit 1; }

echo "[2/3] 加密 payload ..."
"$PY" "$HERE/encrypt_payload.py" \
    --payload "$PAYLOAD" \
    --stub "$WORK/stub.exe" \
    --out "$WORK/payload.enc" \
    --secret "$SECRET" \
    --meta "$HERE/portable.meta.json"

echo "[3/3] 追加密文与尾部索引 ..."
"$PY" - "$WORK/stub.exe" "$WORK/payload.enc" "$OUT" "$HERE/portable.meta.json" <<'PY'
import hashlib, json, struct, sys
from pathlib import Path
stub, enc, out, meta_path = map(Path, sys.argv[1:5])
meta = json.loads(meta_path.read_text(encoding="utf-8"))
salt = bytes.fromhex(meta["salt"])
offset = stub.stat().st_size
enc_size = enc.stat().st_size
with open(out, "wb") as w:
    for p in (stub, enc):
        with open(p, "rb") as r:
            for b in iter(lambda: r.read(1 << 20), b""):
                w.write(b)
    w.write(struct.pack("<8sQQ16s", b"ZKINS002", offset, enc_size, salt))
print("portable:", out, out.stat().st_size, "bytes")
h = hashlib.sha256()
with open(out, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print("SHA256:", h.hexdigest().upper())
PY

ls -lh "$OUT"
rm -rf "$WORK"
