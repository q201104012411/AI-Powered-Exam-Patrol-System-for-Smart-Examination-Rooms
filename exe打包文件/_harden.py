# -*- coding: utf-8 -*-
"""安装/首次解压后的源码硬化：编译为 .pyc，并把 .py 换成极小的加载器。

目标：app 目录里不再保留可直接阅读的业务源码；逻辑在 .pyc 中（仍可被专业
工具反编译，但不再是明文）。runtime/ 的 Python 标准库不做处理。

只由安装器/启动器在解压后调用一次；业务代码不要 import 本文件。
"""
from __future__ import annotations

import os
import py_compile
import sys

MARK = "# _zk_pyc_loader"


def loader_stub(pyc_name: str, is_package: bool) -> str:
    kw = ', submodule_search_locations=[_os.path.dirname(_p)]' if is_package else ""
    return (
        "# -*- coding: utf-8 -*-\n"
        + MARK + "\n"
        "import importlib.util as _iu, os as _os, sys as _sys\n"
        "_p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), %r)\n"
        "_s = _iu.spec_from_file_location(__name__, _p%s)\n"
        "_m = _iu.module_from_spec(_s)\n"
        "_sys.modules[__name__] = _m\n"
        "_s.loader.exec_module(_m)\n"
    ) % (pyc_name, kw)


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
                py_compile.compile(src, cfile=pyc, doraise=True, optimize=0)
                stub = loader_stub(os.path.basename(pyc), fn == "__init__.py")
                with open(src, "w", encoding="utf-8", newline="\n") as f:
                    f.write(stub)
                done += 1
            except Exception as exc:                          # noqa: BLE001
                failed.append("%s: %s" % (src, exc))
    marker = os.path.join(root, ".hardened")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("pyc\n")
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
