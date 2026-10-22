# -*- coding: utf-8 -*-
"""安装/首次解压后的源码硬化：编译为 .pyc，加密字节码，并把 .py 换成加载器。

目标：app 目录里不再保留可直接阅读的业务源码；.pyc 也不是标准格式，
常见反编译工具不能直接打开（加载器运行时才在内存中还原）。

说明：这是提高逆向门槛的工程措施，不是不可破解的 DRM。密钥文件与加载器
都在本机，拥有管理员权限的人仍可还原；真正的边界是安装密码、AES-GCM
载荷加密和启动器哈希绑定。

只由安装器/启动器在解压后调用一次；业务代码不要 import 本文件。
"""
from __future__ import annotations

import os
import py_compile
import sys

MARK = "# _zk_pyc_loader"
KEY_NAME = ".zk_key"


def _key_for(dirpath: str) -> bytes:
    path = os.path.join(dirpath, KEY_NAME)
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    with open(path, "wb") as f:
        f.write(key)
    try:  # Windows：把密钥文件设为隐藏+系统，降低被随手看到的机会
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x2 | 0x4)
    except Exception:                                        # noqa: BLE001
        pass
    return key


def _xor(data: bytes, key: bytes) -> bytes:
    buf = bytearray(data)
    n = len(key)
    for i in range(len(buf)):
        buf[i] ^= key[i % n]
    return bytes(buf)


def loader_stub(pyc_name: str, is_package: bool) -> str:
    pkg = "True" if is_package else "False"
    head = (
        "# -*- coding: utf-8 -*-\n"
        + MARK + "\n"
        "import marshal as _marshal, os as _os, sys as _sys, types as _t\n"
        "import importlib.machinery as _im\n"
        "_d = _os.path.dirname(_os.path.abspath(__file__))\n"
        "_p = _os.path.join(_d, %r)\n"
        "_k = open(_os.path.join(_d, %r), 'rb').read()\n"
        "_b = open(_p, 'rb').read()\n"
        "_x = bytes(c ^ _k[i %% len(_k)] for i, c in enumerate(_b))\n"
        "_mod = _t.ModuleType(__name__)\n"
        "_mod.__file__ = _p\n"
        "_mod.__loader__ = None\n"
        "_mod.__package__ = (__name__ if %s else __name__.rpartition('.')[0])\n"
        "_mod.__spec__ = _im.ModuleSpec(__name__, None, origin=_p, is_package=%s)\n"
        % (pyc_name, KEY_NAME, pkg, pkg)
    )
    path = ""
    if is_package:
        path = ("_mod.__path__ = [_d]\n"
                "_mod.__spec__.submodule_search_locations = [_d]\n")
    return head + path + (
        "_sys.modules[__name__] = _mod\n"
        "exec(_marshal.loads(_x[16:]), _mod.__dict__)\n"
    )


def main() -> int:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    skip_dirs = {"runtime", "__pycache__", ".git", "_build", "data", "logs",
                 "reports", "attachments", "captures"}
    done = 0
    failed = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith(".py") or fn == "_harden.py":
                continue
            src = os.path.join(dirpath, fn)
            try:
                with open(src, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(200)
                if MARK in head:
                    continue
                pyc = src + "c"
                # optimize=2：去掉 docstring / assert，进一步减少可读信息。
                py_compile.compile(src, cfile=pyc, doraise=True, optimize=2)
                key = _key_for(dirpath)
                with open(pyc, "rb") as f:
                    raw = f.read()
                with open(pyc, "wb") as f:
                    f.write(_xor(raw, key))
                stub = loader_stub(os.path.basename(pyc), fn == "__init__.py")
                with open(src, "w", encoding="utf-8", newline="\n") as f:
                    f.write(stub)
                done += 1
            except Exception as exc:                          # noqa: BLE001
                failed.append("%s: %s" % (src, exc))
    marker = os.path.join(root, ".hardened")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("pyc-xor\n")
    except Exception:                                        # noqa: BLE001
        pass
    print("hardened %d files" % done)
    if failed:
        print("FAILED %d:" % len(failed))
        for item in failed[:10]:
            print("  " + item)
        return 1
    # 硬化脚本自身不留在 app 目录
    try:
        os.remove(os.path.join(root, "_harden.py"))
        pyc_self = os.path.join(root, "_harden.pyc")
        if os.path.exists(pyc_self):
            os.remove(pyc_self)
    except Exception:                                        # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
