#!/usr/bin/env bash
# =============================================================================
#  智能考场 AI 巡考系统 · 一键打包
# -----------------------------------------------------------------------------
#  把当前目录（exe打包文件）打包成单文件 Windows exe：
#      启动器 launcher.exe + payload.zip + 24 字节尾部索引
#
#  exe 图标使用 icon/ 目录里的 PNG（默认 icon/icon.png，512x512）：
#      1. Pillow 把 PNG 转成多尺寸 icon.ico；
#      2. rsrc 把 ico 编进启动器的 .rsrc 资源段；
#      3. 再追加 payload，成品 exe 在资源管理器里即显示该图标。
#
#  用法（Git Bash）：
#      bash 一键打包.sh                  # 完整打包
#      bash 一键打包.sh --check          # 只检查环境 + 试转图标，不打包
#      bash 一键打包.sh --out D:/x.exe   # 指定输出文件
#      bash 一键打包.sh --icon icon/a.png
#      bash 一键打包.sh --help
#
#  依赖：
#      Git Bash、Python 3.8+（Pillow）、Go 1.20+（windows/amd64）
#      rsrc.exe 随包放在 _build/tools/rsrc.exe；缺失时脚本会尝试
#      `go install github.com/akavel/rsrc@v0.10.2` 自动获取（需联网）。
#
#  产物：智能考场AI巡考系统.exe
# =============================================================================

set -Eeuo pipefail

# 让 Python 在 Git Bash / 重定向时都按 UTF-8 输出，避免中文乱码。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

APP_NAME="智能考场AI巡考系统"
ROOT="$SCRIPT_DIR"
BUILD="$ROOT/_build"
LAUNCHER_DIR="$BUILD/launcher"
LAUNCHER_EXE="$BUILD/launcher.exe"
PAYLOAD="$BUILD/payload.zip"
ICON_DIR="$ROOT/icon"
OUT="$ROOT/$APP_NAME.exe"
OUT_POSIX=""

ICON_PNG=""
ICON_OPT=""
OUT_OPT=""
CHECK_ONLY=0
STEP=0
STEP_TOTAL=6
START_TS=$(date +%s 2>/dev/null || printf '0')

PY=()
RSRC=""
GO_OS=""
GO_ARCH=""
WORK=""

# 颜色（仅在终端里启用）
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_BLUE=$'\033[36m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi

# ---------------------------------------------------------------------------
# 基础输出
# ---------------------------------------------------------------------------
info() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '  %s⚠%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '\n  %s✘ %s%s\n\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }
step() {
    STEP=$((STEP + 1))
    printf '\n%s[%d/%d] %s%s\n' "$C_BLUE" "$STEP" "$STEP_TOTAL" "$*" "$C_RESET"
}

cleanup() {
    local code=$?
    if [ -n "${WORK:-}" ] && [ -d "$WORK" ]; then
        if [ "$code" -eq 0 ] || [ "${KEEP_WORK:-0}" != "1" ]; then
            rm -rf -- "$WORK" 2>/dev/null || true
        else
            printf '\n中间文件保留在：%s\n' "$WORK" >&2
        fi
    fi
}
trap cleanup EXIT

on_error() {
    local code=$?
    printf '\n  %s✘ 第 %s 行执行失败（退出码 %s）：%s%s\n\n' \
        "$C_RED" "$1" "$code" "$2" "$C_RESET" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
    cat <<'EOF'
智能考场 AI 巡考系统 · 一键打包

用法：
  bash 一键打包.sh                   完整打包（图标取 icon/icon.png）
  bash 一键打包.sh --check           只检查环境和图标，不生成大文件
  bash 一键打包.sh --out D:/x.exe    指定输出 exe
  bash 一键打包.sh --icon icon/a.png 指定图标 PNG
  bash 一键打包.sh --help            显示本帮助

流程：
  1. 检查 Git Bash / Python+Pillow / Go / rsrc
  2. 把 icon/*.png 转成多尺寸 ico
  3. 编译 launcher，并把 ico 嵌进 exe 资源段
  4. 压缩 payload.zip（排除 _build、缓存、日志、附件）
  5. 组装 启动器 + payload + 尾部索引
  6. 校验图标资源、尾部索引、SHA256

环境变量：
  KEEP_WORK=1   打包失败时保留 _build/.work.* 方便排查
EOF
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
to_win_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

normalize_path() {
    # 交给 Python 统一处理 POSIX / Windows 两种写法
    "${PY[@]}" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

detect_python() {
    local cand
    for cand in python python3 py; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
            >/dev/null 2>&1; then
            PY=("$cand")
            return 0
        fi
    done
    if [ -f "$ROOT/runtime/python.exe" ] && \
       "$ROOT/runtime/python.exe" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
           >/dev/null 2>&1; then
        PY=("$ROOT/runtime/python.exe")
        return 0
    fi
    return 1
}

ensure_pillow() {
    if "${PY[@]}" -c 'import PIL' >/dev/null 2>&1; then
        return 0
    fi
    # 系统 Python 没装 Pillow 时，退回项目自带的 runtime
    if [ -f "$ROOT/runtime/python.exe" ] && \
       "$ROOT/runtime/python.exe" -c 'import PIL' >/dev/null 2>&1; then
        PY=("$ROOT/runtime/python.exe")
        return 0
    fi
    die "当前 Python 缺少 Pillow：${PY[*]}
    请安装：${PY[*]} -m pip install pillow
    或改用自带运行时：bash 一键打包.sh --check（脚本会自动回退到 runtime\\python.exe）"
}

detect_go() {
    command -v go >/dev/null 2>&1 || return 1
    GO_OS="$(go env GOOS 2>/dev/null || true)"
    GO_ARCH="$(go env GOARCH 2>/dev/null || true)"
    [ "$GO_OS" = "windows" ] || return 1
    case "$GO_ARCH" in
        amd64|386|arm|arm64) ;;
        *) return 1 ;;
    esac
    return 0
}

detect_rsrc() {
    local p
    for p in "$BUILD/tools/rsrc.exe" "$BUILD/rsrc.exe"; do
        if [ -f "$p" ]; then
            RSRC="$p"
            return 0
        fi
    done
    if command -v rsrc >/dev/null 2>&1; then
        RSRC="$(command -v rsrc)"
        return 0
    fi
    info "未找到 rsrc.exe，尝试通过 Go 自动获取（需要联网）..."
    mkdir -p "$BUILD/tools"
    if GOBIN="$(to_win_path "$BUILD/tools")" \
        go install github.com/akavel/rsrc@v0.10.2 >/dev/null 2>&1; then
        RSRC="$BUILD/tools/rsrc.exe"
        [ -f "$RSRC" ] && return 0
    fi
    return 1
}

find_icon() {
    if [ -n "$ICON_OPT" ]; then
        if [ ! -f "$ICON_OPT" ]; then
            die "指定的图标不存在：$ICON_OPT"
        fi
        ICON_PNG="$ICON_OPT"
        return 0
    fi
    if [ -f "$ICON_DIR/icon.png" ]; then
        ICON_PNG="$ICON_DIR/icon.png"
        return 0
    fi
    local f
    for f in "$ICON_DIR"/*.png; do
        [ -f "$f" ] || continue
        ICON_PNG="$f"
        return 0
    done
    return 1
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            CHECK_ONLY=1
            ;;
        --out)
            shift
            [ $# -gt 0 ] || die "--out 需要一个路径"
            OUT_OPT="$1"
            ;;
        --icon)
            shift
            [ $# -gt 0 ] || die "--icon 需要一个 PNG 路径"
            ICON_OPT="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf '未知参数：%s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$CHECK_ONLY" -eq 1 ]; then
    STEP_TOTAL=2
fi

# 工作目录：放在 _build 下，避免污染源码目录
WORK="$BUILD/.work.$$"
mkdir -p "$WORK/src"

# 纯标准库校验器：检查 PE 图标资源 + 单文件尾部索引
cat > "$WORK/verify.py" <<'PY'
# -*- coding: utf-8 -*-
"""校验 PE 图标资源与单文件尾部索引（仅用标准库，不依赖 pefile）。"""
from __future__ import annotations

import os
import struct
import sys


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _rva_to_off(sections, rva):
    for va, span, rawsize, rawptr in sections:
        if va <= rva < va + span:
            delta = rva - va
            if delta >= rawsize:
                return None
            return rawptr + delta
    return None


def _resource_types(path):
    with open(path, "rb") as f:
        head = f.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            raise ValueError("缺少 MZ 头，不是 Windows exe")
        pe = _u32(head, 0x3C)
        f.seek(pe)
        peh = f.read(24)
        if len(peh) < 24 or peh[:4] != b"PE\0\0":
            raise ValueError("缺少 PE 签名")
        nsec = _u16(peh, 6)
        optsz = _u16(peh, 20)
        f.seek(pe + 24)
        opt = f.read(optsz)
        if len(opt) < optsz:
            raise ValueError("PE 可选头不完整")
        magic = _u16(opt, 0)
        dd = 112 if magic == 0x20B else 96
        if len(opt) < dd + 24:
            raise ValueError("PE 数据目录不完整")
        res_rva = _u32(opt, dd + 16)
        res_size = _u32(opt, dd + 20)
        f.seek(pe + 24 + optsz)
        secdata = f.read(nsec * 40)
        if len(secdata) < nsec * 40:
            raise ValueError("PE 节表不完整")
        sections = []
        for i in range(nsec):
            o = i * 40
            vsize = _u32(secdata, o + 8)
            va = _u32(secdata, o + 12)
            rawsize = _u32(secdata, o + 16)
            rawptr = _u32(secdata, o + 20)
            sections.append((va, max(vsize, rawsize), rawsize, rawptr))
        if not res_rva or not res_size:
            return {}
        off = _rva_to_off(sections, res_rva)
        if off is None:
            return {}
        f.seek(off)
        res = f.read(min(res_size, 4 * 1024 * 1024))

    types = {}
    if len(res) < 16:
        return types
    count = _u16(res, 12) + _u16(res, 14)
    for i in range(count):
        e = 16 + i * 8
        if e + 8 > len(res):
            break
        name = _u32(res, e)
        ptr = _u32(res, e + 4)
        # 高位为 1 表示字符串名称；图标类型都是整数 ID
        if name & 0x80000000:
            continue
        sub = ptr & 0x7FFFFFFF
        n = 0
        if (ptr & 0x80000000) and sub + 16 <= len(res):
            n = _u16(res, sub + 12) + _u16(res, sub + 14)
        types[name] = n
    return types


def icon_info(path):
    types = _resource_types(path)
    return types.get(3, 0), types.get(14, 0)  # RT_ICON / RT_GROUP_ICON


def check_payload(path):
    size = os.path.getsize(path)
    if size < 32:
        raise ValueError("文件太小，不是单文件包")
    with open(path, "rb") as f:
        f.seek(-24, 2)
        trailer = f.read(24)
        magic, off, length = struct.unpack("<8sQQ", trailer)
        if magic != b"ZKSFX001":
            raise ValueError("尾部索引魔术字不是 ZKSFX001")
        if off <= 0 or length <= 0:
            raise ValueError("尾部索引数值无效")
        if off + length + 24 != size:
            raise ValueError(
                "尾部索引与文件长度不一致（offset=%d size=%d file=%d）"
                % (off, length, size))
        f.seek(off)
        if f.read(4) != b"PK\x03\x04":
            raise ValueError("payload 起点不是 ZIP 本地头")
    return off, length


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("--icon", "--full"):
        print("用法: verify.py --icon|--full <exe>", file=sys.stderr)
        return 2
    mode, path = sys.argv[1], sys.argv[2]
    if not os.path.isfile(path):
        print("     错误：文件不存在：%s" % path, file=sys.stderr)
        return 1
    try:
        icons, groups = icon_info(path)
        print("     RT_ICON       : %d" % icons)
        print("     RT_GROUP_ICON : %d" % groups)
        if icons <= 0 or groups <= 0:
            print("     错误：exe 没有嵌入图标资源", file=sys.stderr)
            return 1
        if mode == "--full":
            off, length = check_payload(path)
            print("     尾部索引      : payload @ %d, %d bytes" % (off, length))
    except ValueError as exc:
        print("     错误：%s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

printf '\n%s============================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s  智能考场 AI 巡考系统 · 一键打包%s\n' "$C_BOLD" "$C_RESET"
printf '%s============================================================%s\n' "$C_BOLD" "$C_RESET"
info "工作目录：$ROOT"
info "输出文件：${OUT_OPT:-$OUT}"
info "图标来源：${ICON_OPT:-$ICON_DIR/icon.png}"

# ---------------------------------------------------------------------------
# 1. 环境检查
# ---------------------------------------------------------------------------
step "检查打包环境"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        ok "运行环境：$(uname -s)（Windows / Git Bash）"
        ;;
    *)
        die "请在 Windows 的 Git Bash 中运行本脚本（当前：$(uname -s)）。"
        ;;
esac

[ -f "$ROOT/总控台.py" ] || die "缺少总控台.py：$ROOT/总控台.py"
[ -f "$ROOT/模块清单.json" ] || die "缺少模块清单.json"
[ -f "$ROOT/runtime/pythonw.exe" ] || die "缺少 runtime/pythonw.exe（自带 Python 运行时）"
[ -f "$LAUNCHER_DIR/main.go" ] || die "缺少启动器源码：$LAUNCHER_DIR/main.go"
[ -f "$BUILD/pack.py" ] || die "缺少打包脚本：$BUILD/pack.py"
ok "项目文件完整"

if ! detect_python; then
    die "未找到 Python 3.8+。请安装 Python，或确认 runtime\\python.exe 存在。"
fi
ok "Python：$("${PY[@]}" --version 2>&1)"

ensure_pillow
ok "Pillow：$("${PY[@]}" -c 'import PIL; print(PIL.__version__)')"

if ! detect_go; then
    die "未找到可用的 Go（需要 windows/amd64）。
    嵌入 exe 图标必须重新编译启动器，请先安装 Go：https://go.dev/dl/"
fi
ok "Go：$(go version | awk '{print $3" "$4}')（$GO_OS/$GO_ARCH）"

if ! detect_rsrc; then
    die "未找到 rsrc.exe，且自动获取失败。
    请联网后重试，或手动把 rsrc.exe 放到：$BUILD/tools/rsrc.exe"
fi
ok "rsrc：$RSRC"

if ! find_icon; then
    die "icon/ 目录下没有 PNG 图标。请把图片放到：$ICON_DIR/icon.png"
fi
ok "图标源：$ICON_PNG"

# 统一输出路径（支持相对路径 / Windows 路径）
if [ -n "$OUT_OPT" ]; then
    OUT="$OUT_OPT"
fi
OUT="$(normalize_path "$OUT")"
if command -v cygpath >/dev/null 2>&1; then
    OUT_POSIX="$(cygpath -u "$OUT")"
else
    OUT_POSIX="$OUT"
fi
OUT_DIR="$(dirname -- "$OUT_POSIX")"
mkdir -p -- "$OUT_DIR"
[ -w "$OUT_DIR" ] || die "输出目录不可写：$OUT_DIR"
ok "输出路径：$OUT"

if [ -n "$ICON_OPT" ]; then
    ICON_PNG="$(normalize_path "$ICON_PNG")"
fi

# ---------------------------------------------------------------------------
# 2. 生成图标
# ---------------------------------------------------------------------------
step "生成多尺寸 exe 图标"

"${PY[@]}" - "$ICON_PNG" "$WORK/app.ico" <<'PY'
import os
import sys

from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
im = Image.open(src)
im.load()
if im.format and im.format.upper() != "PNG":
    print("     注意：源文件不是 PNG（%s），仍按图片处理" % im.format, file=sys.stderr)
if im.mode != "RGBA":
    im = im.convert("RGBA")

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
im.save(dst, format="ICO", sizes=sizes)

with Image.open(dst) as ico:
    got = ", ".join("%dx%d" % (w, h) for w, h in sorted(ico.ico.sizes()))

print("     源图：%dx%d" % (im.width, im.height))
print("     ico ：%.1f KB [%s]" % (os.path.getsize(dst) / 1024.0, got))
PY

[ -s "$WORK/app.ico" ] || die "图标转换失败，未生成 ico"
ok "已生成：$WORK/app.ico"

if [ "$CHECK_ONLY" -eq 1 ]; then
    printf '\n%s环境检查通过，可以开始打包。%s\n' "$C_GREEN" "$C_RESET"
    printf '  去掉 --check 即可完整打包：\n'
    printf '      bash 一键打包.sh\n\n'
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. 编译启动器并嵌入图标
# ---------------------------------------------------------------------------
step "编译启动器并嵌入图标"

# 在独立临时目录编译，避免并发/残留的 rsrc.syso 影响结果。
cp -f "$LAUNCHER_DIR/main.go" "$WORK/src/main.go"
printf 'module examlauncher\n\ngo 1.20\n' > "$WORK/src/go.mod"

info "rsrc 写入资源段 ..."
"$RSRC" -ico "$WORK/app.ico" -o "$WORK/src/rsrc.syso" -arch "$GO_ARCH"
[ -s "$WORK/src/rsrc.syso" ] || die "rsrc 生成 .syso 失败"

info "go build（首次编译约 10-60 秒）..."
(
    cd "$WORK/src"
    CGO_ENABLED=0 GO111MODULE=on \
        go build -trimpath -buildvcs=false -ldflags="-s -w" \
        -o "$WORK/launcher.exe" .
)
[ -s "$WORK/launcher.exe" ] || die "启动器编译失败"

# 先校验临时启动器确实带图标，再留一份到 _build/launcher.exe 供手工打包使用。
# 组装阶段始终使用 $WORK 里的副本，避免并发清理把 _build/launcher.exe 删掉后打包失败。
"${PY[@]}" "$WORK/verify.py" --icon "$WORK/launcher.exe"
cp -f -- "$WORK/launcher.exe" "$LAUNCHER_EXE"
ok "已生成带图标的启动器：$LAUNCHER_EXE"

# ---------------------------------------------------------------------------
# 4. 压缩 payload
# ---------------------------------------------------------------------------
step "压缩 payload.zip"

info "使用：${PY[*]} _build/pack.py"
info "首次打包约需 3-10 分钟（约 1 GB 源码/运行时，压缩后约 500 MB）..."
"${PY[@]}" "$BUILD/pack.py"

[ -s "$PAYLOAD" ] || die "payload.zip 生成失败"
PAYLOAD_MB="$("${PY[@]}" -c 'import os,sys; print("%.1f" % (os.path.getsize(sys.argv[1])/1048576.0))' "$PAYLOAD")"
ok "payload.zip：${PAYLOAD_MB} MB"

# ---------------------------------------------------------------------------
# 5. 组装单文件 exe
# ---------------------------------------------------------------------------
step "组装单文件 exe"

info "启动器：$WORK/launcher.exe"
info "payload：$PAYLOAD"
info "输出　：$OUT"

"${PY[@]}" - "$WORK/launcher.exe" "$PAYLOAD" "$OUT" <<'PY'
import os
import shutil
import struct
import sys

launcher, payload, out = sys.argv[1], sys.argv[2], sys.argv[3]
tmp = out + ".building"

try:
    with open(tmp, "wb") as dst, open(launcher, "rb") as src:
        shutil.copyfileobj(src, dst, 1024 * 1024)

    off = os.path.getsize(tmp)
    plen = os.path.getsize(payload)

    with open(tmp, "ab") as dst, open(payload, "rb") as src:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.write(struct.pack("<8sQQ", b"ZKSFX001", off, plen))

    os.replace(tmp, out)
except PermissionError as exc:
    try:
        os.remove(tmp)
    except OSError:
        pass
    print("无法写入输出文件（可能正在运行）：%s" % out, file=sys.stderr)
    print("请先关闭正在运行的 %s.exe 再重试。" % "智能考场AI巡考系统", file=sys.stderr)
    sys.exit(2)
except Exception as exc:
    try:
        os.remove(tmp)
    except OSError:
        pass
    print("组装失败：%s" % exc, file=sys.stderr)
    sys.exit(1)

print("     启动器：%d bytes" % os.path.getsize(launcher))
print("     payload：%d bytes" % plen)
print("     索引　：offset=%d size=%d" % (off, plen))
PY

[ -s "$OUT_POSIX" ] || die "未生成输出文件：$OUT"
ok "已生成：$OUT"

# ---------------------------------------------------------------------------
# 6. 校验成品
# ---------------------------------------------------------------------------
step "校验成品"

"${PY[@]}" "$WORK/verify.py" --full "$OUT"

OUT_MB="$("${PY[@]}" -c 'import os,sys; print("%.1f" % (os.path.getsize(sys.argv[1])/1048576.0))' "$OUT")"
SHA256="$(sha256sum "$OUT_POSIX" | awk '{print $1}')"
ELAPSED=$(($(date +%s 2>/dev/null || printf '0') - START_TS))

printf '\n%s============================================================%s\n' "$C_GREEN" "$C_RESET"
printf '%s  ✔ 打包完成%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
printf '%s============================================================%s\n' "$C_GREEN" "$C_RESET"
printf '  输出文件：%s\n' "$OUT"
printf '  文件大小：%s MB\n' "$OUT_MB"
printf '  SHA256  ：%s\n' "$SHA256"
printf '  总耗时  ：%d 分 %d 秒\n' $((ELAPSED / 60)) $((ELAPSED % 60))
printf '\n  双击 exe 即可运行；图标来自：%s\n\n' "$ICON_PNG"
