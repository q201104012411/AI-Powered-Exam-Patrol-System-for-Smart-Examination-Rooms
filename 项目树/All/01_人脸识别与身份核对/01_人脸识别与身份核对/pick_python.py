# -*- coding: utf-8 -*-
"""
挑选"能真正干活"的 Python 解释器。

背景：一台机器上可能装了多个 Python（本机就有 3.12 与 3.15），
默认解释器往往没装 numpy/cv2/insightface。批处理里做这种探测非常脆弱
（曾因 `%~1` 只取第一个参数，把 `py -3.12` 变成 `py`，结果永远选中 3.15）。

用法：
    python pick_python.py face     # 找能跑人脸核验的解释器（numpy/cv2/insightface/onnxruntime）
    python pick_python.py web      # 找能跑网页后端的解释器（flask/jwt/bcrypt/flask_cors/flask_limiter）
    python pick_python.py base     # 只要求是可用 Python
    python pick_python.py face --all          # 列出全部候选及其检查结果
    python pick_python.py face --out 文件路径  # 结果同时写入文件（供批处理 set /p 读取）

输出：stdout 打印选中的解释器绝对路径（单行）
退出码：0 找到满足依赖的解释器；3 没有解释器满足依赖（输出"最合适的"那个）；4 完全没有 Python

为什么提供 --out：批处理里 `for /f ... in ('"路径带空格的exe" 参数')` 会被
cmd 的引号规则吃掉，捕获不到输出；写文件再 `set /p` 读是唯一稳妥的做法。
"""
import glob
import os
import subprocess
import sys
import tempfile

MODULE_SETS = {
    "face": ["numpy", "cv2", "insightface", "onnxruntime"],
    "web": ["flask", "jwt", "bcrypt", "flask_cors", "flask_limiter"],
    "base": [],
}

# 版本优先级：insightface / onnxruntime 在 3.10~3.12 上最稳，故优先这些
PREFERRED = ["3.12", "3.11", "3.10", "3.13"]


def _run(cmd, timeout=25):
    """用临时文件接收输出，避免管道在受限环境下的兼容问题"""
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        with open(tmp, "wb") as out:
            rc = subprocess.call(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                 timeout=timeout)
        with open(tmp, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        return rc, text
    except Exception as e:
        return 1, str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _decode_console(raw):
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.encode("latin1", "ignore").decode(enc, "replace")
        except Exception:
            continue
    return raw


def candidates():
    """收集候选 (显示名, 可执行命令列表)"""
    found, seen = [], set()

    def add(label, cmd):
        key = " ".join(cmd).lower()
        if key in seen:
            return
        seen.add(key)
        found.append((label, cmd))

    # 1) py 启动器登记的解释器（最可靠）
    rc, out = _run(["py", "-0p"])
    if rc == 0:
        for line in out.replace("\r", "").split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            exe = parts[-1]
            ver = ""
            for p in parts:
                if p.startswith("-V:"):
                    ver = p[3:]
            if os.path.exists(exe):
                add("py-%s" % (ver or "?"), [exe])

    # 2) 常见安装目录
    patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python3*", "python.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Python", "pythoncore-*", "python.exe"),
        r"C:\Python3*\python.exe",
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Python3*", "python.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Python3*", "python.exe"),
    ]
    for pat in patterns:
        for exe in sorted(glob.glob(pat), reverse=True):
            add(os.path.basename(os.path.dirname(exe)), [exe])

    # 3) PATH 里的 python / python3 / py
    for name in ("python", "python3", "py"):
        add(name, [name])

    # 4) 版本优先级排序
    def rank(item):
        label = item[0]
        for i, v in enumerate(PREFERRED):
            if v in label:
                return i
        if label in ("python", "python3"):
            return len(PREFERRED) + 1
        return len(PREFERRED) + 2

    found.sort(key=rank)
    return found


def check(cmd, modules):
    if not modules:
        probe = "import sys"
    else:
        probe = "import " + ", ".join(modules)
    rc, _ = _run(list(cmd) + ["-c", probe], timeout=40)
    return rc == 0


def exe_path(cmd):
    rc, out = _run(list(cmd) + ["-c", "import sys;print(sys.executable)"])
    if rc != 0:
        return None
    for line in out.replace("\r", "").split("\n"):
        line = line.strip()
        if line.lower().endswith(".exe"):
            return line
    return None


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "base"
    show_all = "--all" in sys.argv
    out_file = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out_file = sys.argv[i + 1]
    modules = MODULE_SETS.get(kind, [])

    def emit(path, code):
        print(path)
        if out_file:
            try:
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(path + "\n")
            except OSError:
                pass
        return code

    best_any, results = None, []
    for label, cmd in candidates():
        ok = check(cmd, modules)
        exe = exe_path(cmd) if ok else None
        results.append((label, " ".join(cmd), ok, exe))
        if ok and exe and best_any is None:
            best_any = exe
        if ok and not show_all:
            return emit(exe, 0)

    if show_all:
        for label, cmd, ok, exe in results:
            print("%-14s %-46s %s" % (label, cmd, "OK  " + (exe or "") if ok else "缺少依赖"))
        if best_any:
            return emit(best_any, 0)

    # 没有一个满足依赖：给出基础解释器，让调用方决定是否提示安装
    for label, cmd, ok, exe in results:
        if exe:
            return emit(exe, 3)
    return emit("", 4)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
