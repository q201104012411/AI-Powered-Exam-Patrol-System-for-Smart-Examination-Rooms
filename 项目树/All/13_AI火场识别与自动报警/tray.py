# -*- coding: utf-8 -*-
r"""
系统托盘图标（纯 ctypes 调 Win32，**不需要 pystray**）

为什么要自己写：
    本机没装 pystray，而"平时只在后台运行"必须有托盘 —— 否则用户点了最小化就
    再也找不到程序了（无窗口 + 无托盘 = 幽灵进程，只能去任务管理器杀）。
    托盘只需 Shell_NotifyIcon 一个 API，用 ctypes 调一次比装依赖更可控。

实现要点：
    · 托盘消息必须由一个**有消息循环的窗口**接收，所以这里建一个隐藏窗口，
      并在**独立线程**里跑自己的 GetMessage 循环 —— 不干扰 tkinter 的主循环。
    · 菜单命令通过 `on_command(action)` 回调抛回 tkinter 线程（调用方负责
      `root.after(0, ...)` 切回 UI 线程，tkinter 不是线程安全的）。
    · 任何一步失败都返回 False，调用方降级为"普通最小化"，绝不让程序起不来。
"""
from __future__ import annotations

import ctypes
import itertools
import os
import sys
import threading
import time
from ctypes import wintypes

# ---------------- Win32 常量 ----------------
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IDI_APPLICATION, IDI_WARNING = 32512, 32515
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

#: 托盘菜单项：action 名 -> 显示文字（顺序即菜单顺序，None 表示分隔线）
MENU_ITEMS = (
    ("show", "显示监控窗口"),
    ("toggle", "开始 / 停止监控"),
    ("sep1", None),
    ("test", "模拟一次火警（演练用）"),
    ("sep2", None),
    ("quit", "退出程序"),
)


class _NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        #: 存放窗口过程地址（int，见 _run 里的说明）
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


#: 进程内递增，保证窗口类名唯一。
#: 早期用时间戳做后缀，同一秒内重复创建会**撞名** —— RegisterClassW 对已存在的类
#: 返回 0 且 GetLastError=0，随后 CreateWindowExW 用的还是上一次注册的旧窗口过程
#: （其函数地址可能已失效），表现为"建窗失败且查不到原因"。
_CLS_SEQ = itertools.count(1)


class TrayIcon(object):
    """托盘图标。start() 成功返回 True；失败返回 False（调用方降级）。"""

    def __init__(self, tooltip="AI 火场识别", on_command=None,
                 icon_path=None, level="idle"):
        self.tooltip = tooltip[:120]
        self.on_command = on_command            # callable(action:str)
        self.icon_path = icon_path
        self.level = level
        self._thread = None
        self._hwnd = None
        self._nid = None
        self._hicon = None
        self._hicon_own = False
        self._menu = None
        self._atom = 0
        self._subclassed = False
        self._prev_wndproc = 0
        self._cls_name = "FireTrayWnd_%d_%d" % (os.getpid(), next(_CLS_SEQ))
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._wndproc_ref = None                # 必须持有引用，否则回调被 GC
        self._err = ""                          # 失败原因（供上层打印/记日志）
        self._ok = False

    # ---------------- 对外 ----------------
    @property
    def last_error(self):
        return self._err

    def start(self):
        if os.name != "nt":
            self._err = "非 Windows 平台"
            return False
        self._thread = threading.Thread(target=self._run, name="fire-tray",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=4.0)
        if not self._ok and self._err:
            self._log(self._err)
        return bool(self._ok and self._hwnd)

    @staticmethod
    def _log(message):
        try:
            import config
            config.log("托盘：%s" % message)
        except Exception:                                   # noqa: BLE001
            pass

    def update(self, tooltip=None, level=None):
        """更新提示文字/告警级别（级别只影响图标，失败无声）。"""
        if tooltip:
            self.tooltip = tooltip[:120]
        if level:
            self.level = level
        if not self._hwnd:
            return
        try:
            self._set_icon(_icon_for_level(self.level))
            nid = self._nid
            nid.szTip = self.tooltip
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:                                   # noqa: BLE001
            pass

    def stop(self):
        self._stop.set()
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:                               # noqa: BLE001
                pass

    # ---------------- 内部 ----------------
    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc)

            # ---- 建隐藏窗口：托盘消息的接收者 ----
            # 优先用**系统预定义类 STATIC + 子类化**，而不是自建窗口类。
            # 原因：自建类需要 RegisterClassW + 自定义窗口过程，在受限环境/部分
            # 远程会话里会"注册成功但建窗返回 NULL 且 GetLastError=0"，极难排查
            # （本机沙箱实测就是这种）。系统类无需注册，兼容性最好。
            self._hwnd = self._create_window(user32, kernel32, hinst)
            if not self._hwnd:
                if not self._err:
                    self._err = "无法创建托盘消息窗口"
                return

            self._hicon, self._hicon_own = _load_icon(self.icon_path, self.level)
            nid = _NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip
            self._nid = nid
            if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._err = ("Shell_NotifyIconW 失败（err=0x%08X）—— 可能是无桌面会话"
                             "（服务/计划任务里运行），此时托盘不可用属正常"
                             % (kernel32.GetLastError() & 0xFFFFFFFF))
                return
            self._build_menu()
            self._ok = True
            self._ready.set()

            msg = wintypes.MSG()
            while not self._stop.is_set():
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:                            # noqa: BLE001
            # 绝不让托盘把主程序拖下水，但也不能一声不吭（否则"托盘没了"无从排查）
            self._err = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            self._ready.set()
        finally:
            self._cleanup()

    def _create_window(self, user32, kernel32, hinst):
        """
        建隐藏窗口，两条路依次试：
            A) 系统类 STATIC + SetWindowLongPtrW 子类化（兼容性最好，首选）
            B) 自建窗口类（少数环境里系统类不可用时兜底）
        返回 hwnd 或 0；失败原因写进 self._err。
        """
        # ---- A) STATIC + 子类化 ----
        hwnd = user32.CreateWindowExW(0, "STATIC", "fire-tray", 0,
                                      0, 0, 0, 0, None, None, hinst, None)
        if hwnd:
            GWLP_WNDPROC = -4
            fn = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
            try:
                set_long = getattr(user32, "SetWindowLongPtrW", None) or \
                    getattr(user32, "SetWindowLongW")
                prev = set_long(wintypes.HWND(hwnd), GWLP_WNDPROC,
                                wintypes.LPARAM(fn))
                self._prev_wndproc = prev or 0
                self._subclassed = True
            except Exception as exc:                        # noqa: BLE001
                # 子类化失败也不致命：托盘仍能加图标，只是收不到菜单事件
                self._err = "子类化失败（%s），托盘仅显示图标" % str(exc)[:60]
            return hwnd

        # ---- B) 自建窗口类（注意 lpfnWndProc 必须填函数地址，不能填 ctypes 可调用对象） ----
        wc = _WNDCLASS()
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
        wc.hInstance = hinst
        wc.lpszClassName = self._cls_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            self._err = "RegisterClassW 失败（err=0x%08X）" % (
                kernel32.GetLastError() & 0xFFFFFFFF)
            return 0
        self._atom = int(atom)
        hwnd = user32.CreateWindowExW(0, atom, "fire-tray", 0,
                                      0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            self._err = "CreateWindowExW 失败（err=0x%08X）" % (
                kernel32.GetLastError() & 0xFFFFFFFF)
        return hwnd

    def _cleanup(self):
        try:
            if self._nid is not None:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE,
                                                        ctypes.byref(self._nid))
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self._menu:
                ctypes.windll.user32.DestroyMenu(self._menu)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self._hicon and self._hicon_own:
                ctypes.windll.user32.DestroyIcon(self._hicon)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self._hwnd and self._subclassed and self._prev_wndproc:
                # 恢复原子类过程：不还原的话，句柄复用后可能调到已析构的 Python 回调
                set_long = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None) or \
                    getattr(ctypes.windll.user32, "SetWindowLongW")
                set_long(wintypes.HWND(self._hwnd), -4,
                         wintypes.LPARAM(self._prev_wndproc))
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self._hwnd:
                ctypes.windll.user32.DestroyWindow(self._hwnd)
        except Exception:                                   # noqa: BLE001
            pass
        # 注销窗口类：类名带进程内序号，不注销会在同一进程里累积注册项
        try:
            if self._atom:
                ctypes.windll.user32.UnregisterClassW(
                    wintypes.LPCWSTR(self._cls_name),
                    ctypes.windll.kernel32.GetModuleHandleW(None))
        except Exception:                                   # noqa: BLE001
            pass
        self._hwnd = None

    def _build_menu(self):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        for i, (action, text) in enumerate(MENU_ITEMS):
            if text is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, i, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, i, text)
        self._menu = menu

    def _wndproc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        try:
            if msg == WM_TRAYICON:
                low = _as_int(lparam) & 0xFFFF
                if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._emit("show")
                elif low == WM_RBUTTONUP:
                    self._popup_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                self._emit_by_index(_as_int(wparam) & 0xFFFF)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:                                   # noqa: BLE001
            pass
        # 交回原来的窗口过程（子类化时必须调上一个过程，不能直接叫 DefWindowProc）。
        # 参数要规整成明确类型：部分系统消息的 lparam 会超出 c_int32，直接透传会
        # OverflowError（实测在真实机器上刷屏），所以夹到 32 位并兜住异常。
        cur = getattr(self, "_prev_wndproc", 0)
        try:
            if cur:
                return user32.CallWindowProcW(wintypes.WPARAM(cur),
                                               wintypes.HWND(hwnd),
                                               wintypes.UINT(msg),
                                               wintypes.WPARAM(_as_int(wparam) & 0xFFFFFFFF),
                                               wintypes.LPARAM(_as_signed32(lparam)))
            return user32.DefWindowProcW(wintypes.HWND(hwnd), wintypes.UINT(msg),
                                         wintypes.WPARAM(_as_int(wparam) & 0xFFFFFFFF),
                                         wintypes.LPARAM(_as_signed32(lparam)))
        except Exception:                                   # noqa: BLE001
            return 0

    def _popup_menu(self, hwnd):
        if not self._menu:
            return
        user32 = ctypes.windll.user32
        pt = _POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 关键：弹菜单前必须 SetForegroundWindow，否则点空白处菜单不消失
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(self._menu,
                                    TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)
        if cmd:
            self._emit_by_index(int(cmd))

    def _emit_by_index(self, idx):
        if 0 <= idx < len(MENU_ITEMS):
            action, text = MENU_ITEMS[idx]
            if text is not None:
                self._emit(action)

    def _emit(self, action):
        if self.on_command:
            try:
                self.on_command(action)
            except Exception:                               # noqa: BLE001
                pass

    def _set_icon(self, want):
        if self._hicon_own and self._hicon:
            try:
                ctypes.windll.user32.DestroyIcon(self._hicon)
            except Exception:                               # noqa: BLE001
                pass
            self._hicon_own = False
        self._hicon, self._hicon_own = _load_icon(self.icon_path, want)
        self._nid.hIcon = self._hicon


def _icon_for_level(level):
    """不同级别用不同系统图标（有自定义 ico 时以文件为准）。"""
    return IDI_WARNING if level == "alarm" else IDI_APPLICATION


def _as_int(v):
    """把 ctypes 回调参数安全地转成 python int（可能是 LPARAM/PARAM 包装类型）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(getattr(v, "value", 0) or 0)


def _as_signed32(v):
    """夹到 32 位有符号范围：DefWindowProcW 的 LPARAM 传超范围值会 OverflowError。"""
    n = _as_int(v) & 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _load_icon(path, level):
    """
    返回 (hicon, own)。优先用自定义 .ico，否则退回系统图标。

    坑：wintypes.LPCWSTR 不接受整数。系统图标要用 MAKEINTRESOURCE 的写法 ——
    把 ID **当整数传给 LoadIconW 的第二个参数**（ctypes 允许 int 传给指针参数），
    早期版本写成 wintypes.LPCWSTR(32512) 会抛 TypeError 并被外层 except 吞掉，
    表现为"托盘创建失败"却没有任何提示。
    """
    user32 = ctypes.windll.user32
    if path and os.path.isfile(path):
        try:
            h = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h, True
        except Exception:                                   # noqa: BLE001
            pass
    # 系统图标（共享资源，不能 Destroy）。失败时返回 0，调用方仍可继续（只是没图标）
    try:
        return user32.LoadIconW(None, _icon_for_level(level)), False
    except Exception:                                       # noqa: BLE001
        return 0, False


def available():
    """托盘功能是否可用（非 Windows 一律不可用）。"""
    if os.name != "nt":
        return False, "非 Windows 平台，托盘不可用（不影响弹窗报警）"
    try:
        ctypes.windll.shell32
        ctypes.windll.user32
        return True, "可用"
    except Exception as exc:                                # noqa: BLE001
        return False, "Win32 不可用：%s" % str(exc)[:60]
