# -*- coding: utf-8 -*-
"""
win_input.py —— 跨应用 / 后台点击与窗口枚举
================================================
被 auto_clicker.py 引用。单独成模块是为了让「窗口枚举」「后台点击」
这类 Win32 逻辑可被两个程序共用，也方便单独自测。

两种点击方式
------------
1) 前台点击 `send_click()`：SendInput 合成真实输入，能作用于所有程序
   （游戏、浏览器、远程桌面等），但要求目标窗口在前台、且会移动鼠标。

2) 后台点击 `post_click()`：直接向目标窗口投递 WM_LBUTTONDOWN/UP 消息，
   **不移动鼠标、不抢焦点**，目标窗口在后台也能收到。适合：
   - 边录屏/边做别的事时继续点
   - 固定窗口内的重复操作（按钮、列表项）
   限制：只对「用 Win32 窗口消息响应点击」的控件有效（原生 Win32 / Qt /
   部分游戏）；浏览器网页、Electron、Unity 等自绘控件通常无效，
   这类程序请用前台点击。

许可证: MIT
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  必须显式导入，打包后 ctypes.wintypes 属性才可用
import os

user32 = ctypes.windll.user32

# ---------------------------------------------------------------- 窗口消息

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEMOVE = 0x0200
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SETFOCUS = 0x0007

_BTN_MSG = {
    0: (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    1: (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
    2: (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
}

WINDOW = ctypes.wintypes.HWND


# ---------------------------------------------------------------- 前台输入

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def screen_size():
    """返回 (宽, 高)，单位是物理像素（需 DPI 感知进程）。"""
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen_rect():
    """整个虚拟桌面（含多显示器）的物理矩形 (left, top, right, bottom)。"""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN) + user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _abs_xy(x, y):
    """把虚拟屏幕物理坐标换算成 SendInput 需要的 0..65535 绝对坐标。"""
    l, t, r, b = virtual_screen_rect()
    w = max(1, r - l)
    h = max(1, b - t)
    ax = int((x - l) * 65535 / w)
    ay = int((y - t) * 65535 / h)
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def move_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def send_click(button: int = 0, double: bool = False, at=None):
    """在光标处（at=(x,y) 时先移动过去）发送一次前台点击。"""
    import time as _t
    if at is not None:
        ax, ay = _abs_xy(at[0], at[1])
        for flags in (_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,):
            inp = _INPUT(type=_INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(ax, ay, 0, flags, 0, 0)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        _t.sleep(0.01)
    down, up = ((_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
                (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
                (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP))[button]
    for _ in range(2 if double else 1):
        for flags in (down, up):
            inp = _INPUT(type=_INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if double:
            _t.sleep(0.04)


def send_text(text):
    """向前台窗口输入一段 Unicode 文本（用于流程宏的「输入文本」步骤）。"""
    for ch in text:
        inp = _INPUT(type=_INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(0, ord(ch), _KEYEVENTF_UNICODE, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        inp2 = _INPUT(type=_INPUT_KEYBOARD)
        inp2.ki = _KEYBDINPUT(0, ord(ch), _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0)
        user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(_INPUT))


def send_vk(vk: int, up=True):
    """向前台发送一次按键。"""
    for flags in ((0, _KEYEVENTF_KEYUP) if up else (0,)):
        inp = _INPUT(type=_INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def key_down(vk: int) -> bool:
    """按键是否处于按下状态（用于热键轮询）。"""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------- 后台点击

def post_click(hwnd, x, y, button: int = 0, double: bool = False):
    """向窗口 hwnd 的客户区坐标 (x, y) 投递一次点击消息，不抢焦点。

    x/y 必须是**客户区坐标**（相对窗口左上角，不含标题栏边框）。
    返回是否投递成功。
    """
    import time as _t
    hwnd = WINDOW(int(hwnd))
    if not user32.IsWindow(hwnd):
        return False
    down, up, mk = _BTN_MSG[button]
    lparam = (int(y) << 16) | (int(x) & 0xFFFF)
    for _ in range(2 if double else 1):
        user32.SendMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
        user32.SendMessageW(hwnd, down, mk, lparam)
        user32.SendMessageW(hwnd, up, 0, lparam)
        if double:
            _t.sleep(0.04)
    return True


def post_text(hwnd, text):
    """向后台窗口投递 WM_CHAR 文本输入。"""
    hwnd = WINDOW(int(hwnd))
    if not user32.IsWindow(hwnd):
        return False
    for ch in text:
        user32.SendMessageW(hwnd, WM_CHAR, ord(ch), 1)
    return True


# ---------------------------------------------------------------- 窗口枚举

class WindowInfo(ctypes.Structure):
    """一条窗口记录（hwnd + 标题 + 进程名 + 位置 + 是否最小化）"""
    __slots__ = ("hwnd", "title", "exe", "pid", "left", "top", "right", "bottom", "minimized")

    def __init__(self, hwnd, title, exe, pid, rect, minimized):
        self.hwnd = hwnd
        self.title = title
        self.exe = exe
        self.pid = pid
        self.left, self.top, self.right, self.bottom = rect
        self.minimized = minimized

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    @property
    def valid(self):
        return not self.minimized and self.width > 80 and self.height > 40

    def __repr__(self):
        return "<WindowInfo 0x%X '%s' %s %dx%d>" % (
            self.hwnd, (self.title or "")[:30], self.exe, self.width, self.height)


_EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def _process_name(pid):
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            size = ctypes.c_ulong(260)
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


import os  # noqa: E402  放在后面避免与 ctypes 初始化的顺序混淆


def window_rect(hwnd):
    """取窗口在屏幕上的物理矩形（含标题栏）。最小化时返回 (0,0,0,0)。"""
    r = ctypes.wintypes.RECT()
    # 9 = DWMWA_EXTENDED_FRAME_BOUNDS，比 GetWindowRect 准（去掉阴影留白）
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            WINDOW(int(hwnd)), 9, ctypes.byref(r), ctypes.sizeof(r))
    except Exception:  # noqa: BLE001
        user32.GetWindowRect(WINDOW(int(hwnd)), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def client_rect(hwnd):
    """取客户区矩形（相对窗口左上角）。"""
    r = ctypes.wintypes.RECT()
    user32.GetClientRect(WINDOW(int(hwnd)), ctypes.byref(r))
    return 0, 0, r.right, r.bottom


def client_to_screen(hwnd, cx, cy):
    """客户区坐标 → 屏幕物理坐标。"""
    p = ctypes.wintypes.POINT(int(cx), int(cy))
    user32.ClientToScreen(WINDOW(int(hwnd)), ctypes.byref(p))
    return p.x, p.y


def is_window(hwnd):
    return bool(user32.IsWindow(WINDOW(int(hwnd))))


def is_minimized(hwnd):
    return bool(user32.IsIconic(WINDOW(int(hwnd))))


def list_windows(only_visible=True, min_w=200, min_h=140, exclude_self=True):
    """枚举可用于「后台点击 / 录制」的顶层窗口。

    返回 WindowInfo 列表，按面积从大到小排序。
    exclude_self: 排除本程序自己的窗口与桌面/任务栏等系统窗口。
    """
    import os as _os  # noqa: F401
    results = []
    self_pids = {os.getpid()}

    def _cb(hwnd, _lparam):
        try:
            if only_visible and not user32.IsWindowVisible(hwnd):
                return True
            title_len = user32.GetWindowTextLengthW(hwnd)
            if title_len <= 0:
                return True
            buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buf, title_len + 1)
            title = buf.value.strip()
            if not title:
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if exclude_self and pid.value in self_pids:
                return True
            # 过滤桌面、任务栏等系统壳窗口
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"):
                return True
            rect = window_rect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            mini = bool(user32.IsIconic(hwnd))
            if not mini and (w < min_w or h < min_h):
                return True
            if mini and (w < 160 or h < 120):
                return True
            exe = _process_name(pid.value)
            results.append(WindowInfo(int(hwnd), title, exe, pid.value, rect, mini))
        except Exception:  # noqa: BLE001
            pass
        return True

    user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    # 面积大的排前面（一般就是主窗口），最小化的排最后
    results.sort(key=lambda w: (w.minimized, -(w.width * w.height)))
    return results


def find_window_by_title(keyword):
    """按标题关键字模糊查找窗口（取第一个匹配）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    for w in list_windows():
        if kw in w.title.lower() or kw in (w.exe or "").lower():
            return w
    return None


def activate(hwnd):
    """把窗口切到前台（失败时返回 False）。"""
    hwnd = WINDOW(int(hwnd))
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # AttachThreadInput 技巧：把目标窗口的线程与当前线程挂接后才能可靠置前
    fg = user32.GetForegroundWindow()
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached = False
    if fg and tgt_tid and tgt_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
    return user32.GetForegroundWindow() == hwnd


def selftest():
    """自检：打印当前 DPI / 屏幕 / 可见窗口数。返回 0 表示通过。"""
    print("[win_input] 屏幕(物理):", screen_size())
    print("[win_input] 虚拟桌面:", virtual_screen_rect())
    wins = list_windows()
    print("[win_input] 可用窗口数: %d" % len(wins))
    for w in wins[:10]:
        print("   ", w)
    assert len(wins) >= 0
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
