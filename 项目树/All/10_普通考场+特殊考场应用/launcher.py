# -*- coding: utf-8 -*-
r"""
Start the GUI of topic 10 (plain-ASCII filename on purpose).

Why a separate launcher instead of running gui.py directly from the .bat:

1) **No console window.** The .bat runs this file with `pythonw.exe`, so the
   black console window never appears. Running `python.exe` flashes a console
   first, which is exactly what makes people think "it did not start".
2) **Failures stay visible.** `pythonw.exe` swallows tracebacks. `gui.run()`
   writes the full traceback to `data\logs\launch.log` and shows a dialog box,
   so a failed start is never silent.
3) **Chinese paths are safe.** Batch files are parsed byte-wise by cmd.exe
   before `chcp 65001` takes effect, so a Chinese file name passed from a
   .bat can be mangled. This file is ASCII-named and does path handling in
   Python, where Unicode is reliable.

Usage:  pythonw.exe launcher.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


def main():
    try:
        import gui
    except Exception as e:                                # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            os.makedirs(os.path.join(HERE, "data", "logs"), exist_ok=True)
            with open(os.path.join(HERE, "data", "logs", "launch.log"),
                      "a", encoding="utf-8") as f:
                f.write("launcher: import gui failed\n%s\n" % tb)
        except OSError:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Failed to import the GUI module:\n\n%s: %s\n\n"
                "See data\\logs\\launch.log" % (type(e).__name__, e),
                "Exam Room App - cannot start", 0x10)
        except Exception:                                 # noqa: BLE001
            pass
        return 1
    return gui.run()


if __name__ == "__main__":
    sys.exit(main())
