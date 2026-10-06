# -*- coding: utf-8 -*-
"""
ui_kit —— Windows 桌面小工具的 Apple 风格 Tkinter UI 骨架（高 DPI 安全）
=====================================================================
从 GPU 切换助手（GpuSwitcher）实战中沉淀出来的可复用组件库。

用法：
    from ui_kit import (AppBase, THEME, px, F, RoundedFrame, AppleButton,
                        SegmentedControl, ToggleSwitch, Pill, Sidebar,
                        ScrollFrame, setup_dpi, init_ui_scale, apply_tk_scaling)

    class MyApp(AppBase):
        PAGES = [("main", "主页"), ("more", "更多")]
        def _build_pages(self):
            p = self.pages["main"].body
            box = self._card(p, "标题")
            AppleButton(box, "点我", command=..., style="primary").pack()

    if __name__ == "__main__":
        MyApp.run(title="我的工具", size=(980, 720))

硬性规则（踩过坑，别改）：
  * 所有尺寸必须包 px()，字体必须用 F()（负像素字号）
  * 打包必须带 --manifest（PerMonitorV2），否则高分屏模糊
  * 后台线程禁止直接 root.after，一律 self.ui(fn, *args)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  必须显式导入：打包后才会被打进 exe
import math
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk

UI_SCALE = 1.0


def get_system_dpi(hwnd=None):
    """取真实 DPI。三级取数，任一可用即可，越靠前越准。

    注意：GetDeviceCaps(LOGPIXELSX) 的返回值依赖进程的感知状态——进程未声明
    DPI 感知时它返回「虚拟化的 96」，即使屏幕真实是 192。所以它只能当兜底。
    """
    # ① 窗口所在显示器的 DPI：多屏混合缩放时最准（需 DPI 感知已生效）
    if hwnd:
        try:
            f = ctypes.windll.user32.GetDpiForWindow
            f.argtypes = [ctypes.wintypes.HWND]
            f.restype = ctypes.c_uint
            d = f(ctypes.wintypes.HWND(hwnd))
            if d:
                return int(d)
        except Exception:  # noqa: BLE001  Win8.1 以下没有该 API
            pass
    # ② 系统 DPI：Win10 1607+，不受进程感知状态影响
    try:
        f = ctypes.windll.user32.GetDpiForSystem
        f.restype = ctypes.c_uint
        d = f()
        if d:
            return int(d)
    except Exception:  # noqa: BLE001
        pass
    # ③ 兜底：屏幕 DC 的 LOGPIXELSX（依赖进程感知状态，未感知时会返回 96）
    try:
        h = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(h, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, h)
        return int(dpi) or 96
    except Exception:  # noqa: BLE001
        return 96


def _current_awareness() -> str:
    """回读**实际生效**的 DPI 感知级别（不修改任何状态）。

    打包时 manifest 已声明 PerMonitorV2 → 进程启动就已感知，
    此时再调 SetProcessDpiAwarenessContext 会失败（已设置），
    setup_dpi() 的兜底分支会误报成 SystemAware。用这个函数纠正标签。
    """
    # DPI_AWARENESS: 0=unaware 1=system 2=permonitor
    try:
        f = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext
        f.argtypes = [ctypes.c_void_p]
        f.restype = ctypes.c_int
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        a = f(ctypes.c_void_p(-4))
        if a == 2:
            return "PerMonitorV2"
        if a == 1:
            return "PerMonitor"
        if a == 0:
            return "Unaware"
    except Exception:  # noqa: BLE001  Win10 1703 以前没有该 API
        pass
    try:
        f = ctypes.windll.user32.GetProcessDpiAwareness
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        f.restype = ctypes.c_long
        awareness = ctypes.c_int(-1)
        if f(None, ctypes.byref(awareness)) == 0:
            return {2: "PerMonitor", 1: "SystemAware",
                    0: "Unaware"}.get(awareness.value, "Unaware")
    except Exception:  # noqa: BLE001
        pass
    return "Unknown"


def setup_dpi() -> str:
    """创建窗口前调用，尽量声明 Per-Monitor V2 感知。返回状态串。

    坑 1：SetProcessDpiAwarenessContext 在 **user32.dll**，不在 shcore.dll。
    写错库会让第一优先级静默失败（AttributeError 被 except 吞掉），
    程序降级到 SystemAware，200% 缩放下布局全错却毫无报错。

    坑 2：带 manifest 打包时进程**启动即**已是 PerMonitorV2，
    此时三个 Set* 调用全部失败，会走到最后误报 "SystemAware"。
    所以最后统一用 _current_awareness() 回读真实值。
    """
    try:
        f = ctypes.windll.user32.SetProcessDpiAwarenessContext
        f.argtypes = [ctypes.c_void_p]
        f.restype = ctypes.c_bool
        if f(ctypes.c_void_p(-4)):          # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "PerMonitorV2"
        if f(ctypes.c_void_p(-2)):          # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE (v1)
            return "PerMonitor"
    except Exception:  # noqa: BLE001  Win8.1 以下没有该 API
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "PerMonitor"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass
    # 三个 Set* 都可能失败（manifest 已声明 / 权限不足），回读真实状态
    actual = _current_awareness()
    return actual if actual != "Unknown" else "LegacyAware"


def init_ui_scale(root=None):
    """必须在建 Tk() 之后、建任何控件之前调用。

    传 root 可拿到该窗口所在显示器的真实 DPI（多屏场景更准）。
    """
    global UI_SCALE
    hwnd = None
    if root is not None:
        try:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        except Exception:  # noqa: BLE001
            hwnd = None
    UI_SCALE = get_system_dpi(hwnd) / 96.0
    return UI_SCALE


def dpi_report():
    """调试用：返回当前 DPI 诊断串。排版异常时先看它。"""
    try:
        h = ctypes.windll.user32.GetDC(0)
        caps = ctypes.windll.gdi32.GetDeviceCaps(h, 88)
        ctypes.windll.user32.ReleaseDC(0, h)
    except Exception:  # noqa: BLE001
        caps = -1
    return "UI_SCALE=%.3f  GetDpiForSystem=%s  GetDeviceCaps=%s" % (
        UI_SCALE,
        getattr(ctypes.windll.user32, "GetDpiForSystem", lambda: "n/a")(),
        caps,
    )


def apply_tk_scaling(root):
    """把 tk scaling 固定为 1.0：让 px()/F() 成为唯一缩放来源"""
    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:  # noqa: BLE001
        pass




# ---------------------------------------------------------------- 主题

THEME = {
    "window": "#F5F5F7",
    "sidebar": "#EFEFF2",
    "card": "#FFFFFF",
    "soft": "#FAFAFC",
    "text": "#1D1D1F",
    "text2": "#6E6E73",
    "text3": "#86868B",
    "sep": "#E8E8ED",
    "border": "#D2D2D7",
    "blue": "#007AFF",
    "blue_h": "#0A84FF",
    "blue_p": "#0060DF",
    "green": "#34C759",
    "orange": "#FF9500",
    "red": "#FF3B30",
    "track": "#E9E9EE",
    "shadow": "#DEDEE3",
    "disabled": "#C7C7CC",
}

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

# 整体字号微调系数。1.0 = 严格按 96DPI 的 pt→px 换算。
# 高 DPI 下 UI_FONT 的实际字面偏大（微软雅黑在 200% 下笔画较满），
# 调到 0.92 观感更接近 macOS 原生控件密度。
FONT_SCALE = 0.92


def F(size=13, weight="normal", mono=False):
    # 负字号 = 像素。96 DPI 下 1pt≈1.333px，与旧版 tk scaling=1.333 视觉一致
    return ((MONO_FONT if mono else UI_FONT),
            -px(size * 96.0 / 72.0 * FONT_SCALE), weight)


def apply_base_fonts(root=None):
    """把 Tk 的**命名默认字体**改成负像素字号。

    这是高 DPI 下最容易漏的一环：ttk.Combobox 展开后的下拉列表、
    tk.Listbox、菜单、Tooltip、Treeview 标题这些控件**不继承** style
    里的 font，而是回退到命名默认字体 TkDefaultFont（9pt）。
    由于 apply_tk_scaling() 把 tk scaling 钉死在 1.0，这 9pt 不会再被
    系统按 DPI 放大 —— 结果就是 200% 缩放下拉列表文字小到 9 物理像素，
    而正文是 27px，反差极其明显。样式里配 font=F(10) 对它无效。

    必须在建任何控件之前调用。
    """
    import tkinter.font as tkfont
    # 名称 -> 基准 pt 字号（对齐 F() 的观感层级）
    named = {
        "TkDefaultFont": 10,
        "TkTextFont": 10,
        "TkMenuFont": 10,
        "TkHeadingFont": 10,
        "TkCaptionFont": 10,
        "TkSmallCaptionFont": 9,
        "TkIconFont": 10,
        "TkTooltipFont": 9,
        "TkFixedFont": 10,
    }
    for name, pt in named.items():
        try:
            f = tkfont.nametofont(name)
            f.configure(family=UI_FONT,
                        size=-px(pt * 96.0 / 72.0 * FONT_SCALE))
        except Exception:  # noqa: BLE001  某些命名字体在个别平台不存在
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=MONO_FONT)
    except Exception:  # noqa: BLE001
        pass
    return True


# ---------------------------------------------------------------- 圆角绘制

def rrect_points(x1, y1, x2, y2, r, steps=14):
    r = max(0.0, min(float(r), abs(x2 - x1) / 2.0, abs(y2 - y1) / 2.0))
    pts = [x1 + r, y1]

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))

    arc(x2 - r, y1 + r, -90, 0)
    pts += [x2, y2 - r]
    arc(x2 - r, y2 - r, 0, 90)
    pts += [x1 + r, y2]
    arc(x1 + r, y2 - r, 90, 180)
    pts += [x1, y1 + r]
    arc(x1 + r, y1 + r, 180, 270)
    return pts


def rrect(canvas, x1, y1, x2, y2, r, **kw):
    kw.setdefault("outline", "")
    return canvas.create_polygon(rrect_points(x1, y1, x2, y2, r), **kw)


SHADOW_PAD = 5

# UI 缩放因子：运行时按系统 DPI 设置（96 DPI 时为 1.0）。
# 所有固定像素尺寸都应经过 px() 缩放，字体在 F() 内部处理。
UI_SCALE = 1.0


def px(n):
    return int(round(n * UI_SCALE))


def style_combobox(s=None, name="Apple.Combo"):
    """把 ttk.Combobox 改成 Apple 风格（白底圆角感 + 细边 + 主题色焦点）。

    clam 主题的 Combobox 默认是灰色方块，跟整体 UI 不搭；
    element_create 只能改颜色，改不了圆角，但观感已经统一很多。
    """
    s = s or ttk.Style()
    if "clam" in s.theme_names():
        s.theme_use("clam")
    # 坑：ttk 里 configure 一个全新的样式名不会自动创建 layout，
    # 必须先把基础 layout 克隆过来，否则控件创建时报
    # 「Layout Apple.Combo not found」。
    try:
        base = s.layout("TCombobox")
        if base:
            s.layout(name, base)
    except Exception:  # noqa: BLE001
        pass
    s.configure(name,
                fieldbackground=THEME["card"],
                background=THEME["card"],
                foreground=THEME["text"],
                bordercolor=THEME["border"],
                lightcolor=THEME["border"],
                darkcolor=THEME["border"],
                arrowcolor=THEME["text3"],
                arrowsize=px(12),
                borderwidth=1,
                relief="solid",
                padding=(px(8), px(5)),
                font=F(10))
    # clam 的 Combobox 行高由 element 的 height 决定，configure 管不到，
    # 必须 element_create 一个同尺寸的 field element 覆盖，否则高 DPI 下被压扁。
    try:
        s.element_create("%s.field" % name, "from", "clam", "field",
                         width=px(16), height=px(20))
        s.element_create("%s.padding" % name, "from", "clam", "padding",
                         left=px(8), right=px(8), top=px(2), bottom=px(2))
        s.element_create("%s.arrow" % name, "from", "clam", "arrow",
                         width=px(18), height=px(18))
        s.element_create("%s.arrow.image" % name, "from", "clam", "arrow.image")
        s.element_create("%s.downarrow" % name, "from", "clam", "downarrow",
                         width=px(11), height=px(11))
    except Exception:  # noqa: BLE001
        pass
    s.map(name,
          fieldbackground=[("readonly", THEME["card"]), ("disabled", THEME["soft"])],
          foreground=[("disabled", THEME["text3"])],
          bordercolor=[("focus", THEME["blue"])],
          lightcolor=[("focus", THEME["blue"])],
          darkcolor=[("focus", THEME["blue"])],
          arrowcolor=[("focus", THEME["blue"]), ("active", THEME["text"])])
    # 下拉列表
    s.configure("%s.popdown" % name,
                background=THEME["card"],
                foreground=THEME["text"],
                borderwidth=1,
                relief="solid",
                arrowcolor=THEME["text3"],
                arrowsize=px(12),
                font=F(10))
    s.map("%s.popdown" % name,
          background=[("selected", THEME["blue"])],
          foreground=[("selected", "#FFFFFF")])
    # element_create 之后再把 layout 指向新 element（顺序不能反）
    try:
        lay = s.layout(name)
        new = []
        for elm, opts in lay:
            e = elm.replace(name, "%s" % name, 1)
            new.append((e, opts))
        s.layout(name, new)
    except Exception:  # noqa: BLE001
        pass
    return name



class RoundedFrame(tk.Canvas):
    """圆角白色卡片，内部 .body 可自由摆放控件

    坑：默认参数里不能写 px(16)。默认参数在 import 时求值，那时 UI_SCALE
    还是 1.0，值会被永久冻结，200% 缩放下圆角/阴影全部失真。
    一律用 None 哨兵，在 __init__ 内部（即 init_ui_scale 之后）再 px()。
    """

    def __init__(self, master, radius=None, fill=None, pad=16, bg=None, shadow=True):
        bg = bg or THEME["window"]
        fill = fill or THEME["card"]
        radius = px(16 if radius is None else radius)
        pad = px(pad)
        tk.Canvas.__init__(self, master, bg=bg, highlightthickness=0, bd=0)
        self.radius = radius
        self.fill = fill
        self.pad = pad
        self.shadow = shadow
        self._h = pad * 2 + SHADOW_PAD
        self._last_w = -1
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._on_size)
        self.body.bind("<Configure>", self._on_inner)
        self.configure(height=self._h)

    def _on_size(self, event):
        w = max(24, self.winfo_width())
        if w != self._last_w:
            self._last_w = w
            self.itemconfigure(self._win, width=max(12, w - 2 * self.pad))
        self._draw(w)

    def _on_inner(self, event=None):
        h = self.body.winfo_reqheight() + 2 * self.pad + SHADOW_PAD
        if abs(h - self._h) > 0.5:
            self._h = h
            self.configure(height=h)
            self._draw(self.winfo_width())

    def _draw(self, w):
        self.delete("card")
        h = self._h
        if w < 6 or h < 6:
            return
        if self.shadow:
            rrect(self, 3, 4, w - 3, h - 1, self.radius + 1,
                  fill=THEME["shadow"], tags="card")
        rrect(self, 0, 0, w - 1, h - SHADOW_PAD - 1, self.radius,
              fill=self.fill, outline=THEME["sep"], width=px(1), tags="card")


# ---------------------------------------------------------------- 控件

class AppleButton(tk.Canvas):
    """圆角按钮：primary / secondary / danger / success / plain"""

    STYLES = {
        "primary": (THEME["blue"], THEME["blue_h"], THEME["blue_p"], "#FFFFFF"),
        "secondary": ("#FFFFFF", "#F5F5F7", "#E5E5EA", THEME["text"]),
        "danger": (THEME["red"], "#FF453A", "#D70015", "#FFFFFF"),
        "success": (THEME["green"], "#30D158", "#248A3D", "#FFFFFF"),
        "plain": (None, None, None, THEME["blue"]),
    }

    def __init__(self, master, text="", command=None, style="primary",
                 width=None, height=None, radius=None, bg=None, font=None,
                 state="normal", fill=False):
        # 坑：默认参数里不能写 px(150)——import 时 UI_SCALE 还是 1.0，
        # 求值结果被永久冻结，高 DPI 下按钮尺寸全是 96-DPI 的。
        width = px(150 if width is None else width)
        height = px(40 if height is None else height)
        radius = px(11 if radius is None else radius)
        self.bg = bg if bg is not None else THEME["card"]
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.text = text
        self.command = command
        self.style = style
        self.width_ = width
        self.height_ = height
        self.radius = radius
        self.font = font or F(11, "bold")
        self.state = state
        # fill=True：按钮宽度跟随容器（pack(fill="x", expand=True) 之后必须开这个，
        # 否则 Canvas 内部仍按构造时的固定 width 绘制，右边会留白或被裁）。
        self.fill = fill
        self._hover = False
        self._press = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        if fill:
            self.bind("<Configure>", self._on_fill)
        self.draw()

    def _on_fill(self, event):
        w = event.width
        if w > 1 and w != self.width_:
            self.width_ = w
            self.draw()

    # -- 状态
    def set_enabled(self, on: bool):
        self.state = "normal" if on else "disabled"
        self.configure(cursor="hand2" if on else "")
        self.draw()

    def set_text(self, t):
        self.text = t
        self.draw()

    def _enter(self, _):
        self._hover = True
        self.configure(cursor="hand2" if self.state == "normal" else "")
        self.draw()

    def _leave(self, _):
        self._hover = False
        self._press = False
        self.draw()

    def _down(self, _):
        if self.state != "normal":
            return
        self._press = True
        self.draw()

    def _up(self, _):
        if self.state != "normal":
            return
        was = self._press
        self._press = False
        self.draw()
        if was and self.command:
            self.command()

    def draw(self):
        self.delete("all")
        w, h, r = self.width_, self.height_, self.radius
        base, hover, press, fg = self.STYLES.get(self.style, self.STYLES["primary"])
        dis = self.state != "normal"

        if dis:
            fill, fg = THEME["disabled"], "#FFFFFF" if self.style != "secondary" else "#AEAEB2"
            if self.style == "plain":
                fill, fg = None, THEME["disabled"]
        else:
            fill = base
            if self._press:
                fill = press
            elif self._hover:
                fill = hover

        if fill:
            outline = THEME["border"] if self.style == "secondary" else ""
            rrect(self, 1, 1, w - 2, h - 2, r, fill=fill, outline=outline, width=px(1))
        elif self.style == "plain" and self._hover and not dis:
            rrect(self, 1, 1, w - 2, h - 2, r, fill="#EFEFF2", outline="")

        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class SegmentedControl(tk.Canvas):
    """iOS 风格分段控件（带滑动 thumb）

    坑：默认参数不能写 px(340)——import 时求值会冻结在 UI_SCALE=1.0。
    """

    def __init__(self, master, options, command=None, width=None, height=None,
                 bg=None, radius=None, fill=False):
        self.bg = bg or THEME["card"]
        width = px(340 if width is None else width)
        height = px(38 if height is None else height)
        radius = px(10 if radius is None else radius)
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.options = list(options)
        self.command = command
        self.width_ = width
        self.height_ = height
        self.radius = radius
        self.index = 0
        self._shown = 0.0
        self._anim = None
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        # 坑：Canvas 类控件 pack(fill="x", expand=True) 只会分配空间，
        # 内部仍按构造时的 self.width_ 绘制，选项多时右侧会被裁掉。
        # fill=True 用 <Configure> 跟随容器实际宽度重绘。
        if fill:
            self.bind("<Configure>", self._on_fill)
        self.draw()

    def _on_fill(self, event):
        w = event.width
        if w > 1 and w != self.width_:
            self.width_ = w
            self.draw()

    def select(self, i, fire=False):
        i = max(0, min(i, len(self.options) - 1))
        if i == self.index and self._shown == float(i):
            return
        self.index = i
        self._animate_to(float(i))
        if fire and self.command:
            self.command(i, self.options[i])

    def _click(self, event):
        n = len(self.options)
        seg = self.width_ / float(n)
        i = int(event.x // seg)
        if 0 <= i < n and i != self.index:
            self.select(i)
            if self.command:
                self.command(i, self.options[i])

    def _animate_to(self, target):
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:  # noqa: BLE001
                pass
        start = self._shown
        steps = 8
        self._step = 0

        def tick():
            self._step += 1
            t = self._step / float(steps)
            self._shown = start + (target - start) * t
            self.draw()
            if self._step < steps:
                self._anim = self.after(16, tick)
            else:
                self._shown = target
                self._anim = None
                self.draw()

        tick()

    def draw(self):
        self.delete("all")
        w, h, n = self.width_, self.height_, len(self.options)
        rrect(self, 0, 0, w - 1, h - 1, self.radius, fill=THEME["track"])
        seg = w / float(n)
        x = 3 + self._shown * seg
        tw = seg - 6
        rrect(self, x + 1, 5, x + tw + 1, h - 4, self.radius - 2,
              fill=THEME["shadow"], outline="")
        rrect(self, x, 3, x + tw, h - 6, self.radius - 2,
              fill="#FFFFFF", outline=THEME["sep"], width=px(1))
        for i, label in enumerate(self.options):
            cx = seg * (i + 0.5)
            self.create_text(cx, h / 2, text=label,
                             fill=THEME["text"] if i == self.index else THEME["text2"],
                             font=F(11, "bold" if i == self.index else "normal"))


class ToggleSwitch(tk.Canvas):
    """iOS 风格开关"""

    def __init__(self, master, variable=None, command=None, bg=None,
                 width=None, height=None):
        width = px(48 if width is None else width)
        height = px(29 if height is None else height)
        self.bg = bg or THEME["card"]
        tk.Canvas.__init__(self, master, width=width, height=height,
                           bg=self.bg, highlightthickness=0, bd=0)
        self.var = variable
        self.command = command
        self.width_, self.height_ = width, height
        self._pos = 1.0 if (variable and variable.get()) else 0.0
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        self.draw()

    def _click(self, _):
        if self.var is None:
            return
        self.var.set(not self.var.get())
        self._animate()
        if self.command:
            self.command()

    def _animate(self):
        target = 1.0 if self.var.get() else 0.0
        start, steps = self._pos, 8
        self._i = 0

        def tick():
            self._i += 1
            self._pos = start + (target - start) * (self._i / float(steps))
            self.draw()
            if self._i < steps:
                self.after(16, tick)
            else:
                self._pos = target

        tick()

    def sync(self):
        self._pos = 1.0 if (self.var and self.var.get()) else 0.0
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.width_, self.height_
        on = self._pos
        r = h / 2.0
        col = THEME["green"] if on > 0.5 else THEME["track"]
        rrect(self, 0, 0, w - 1, h - 1, r, fill=col, outline="")
        kx = 3 + on * (w - h + 3 - 3)
        kd = h - 6
        self.create_oval(kx + 1, 4, kx + kd + 1, 4 + kd,
                         fill=THEME["shadow"], outline="")
        self.create_oval(kx, 3, kx + kd, 3 + kd, fill="#FFFFFF", outline="")


class Pill(tk.Canvas):
    """小胶囊标签（状态徽章）"""

    def __init__(self, master, text="", color=None, bg=None, size=11, pad=9, height=None):
        # 坑：height 默认值不能写 px(24)（import 时被冻结），
        # 且高度必须随字号走，否则 200% 下文字撑破胶囊。
        height = px(max(24, size * 96.0 / 72.0 + 8) if height is None else height)
        self.bg = bg or THEME["window"]
        self.color = color or THEME["blue"]
        tk.Canvas.__init__(self, master, height=height, bg=self.bg,
                           highlightthickness=0, bd=0)
        self.text = text
        self.size = size
        self.pad = pad
        self.height_ = height
        self.redraw()

    def set(self, text, color=None):
        self.text = text
        if color:
            self.color = color
        self.redraw()

    def redraw(self):
        self.delete("all")
        font = F(self.size, "bold")          # 负像素字号，高 DPI 下不变形
        f = tkfont.Font(font=font)
        pad = px(self.pad)                   # 内边距也必须缩放，否则文字溢出胶囊
        tw = f.measure(self.text)
        th = f.metrics("linespace")
        # 胶囊高度取「构造时给定值」与「文字实际高度 + 上下留白」的较大者，
        # 保证任何字号 / 任何 DPI 下文字都不会被裁切。
        need = th + px(8)
        if need > self.height_:
            self.height_ = need
            self.configure(height=self.height_)
        w = tw + pad * 2
        self.configure(width=w)
        rrect(self, 0, 0, w - 1, self.height_ - 1, self.height_ / 2.0, fill=self.color)
        self.create_text(w / 2, self.height_ / 2 + 0.5, text=self.text,
                         fill="#FFFFFF", font=font)


# ---------------------------------------------------------------- 侧边栏

def draw_icon(c, kind, cx, cy, color, s=None):
    k = (px(15) if s is None else px(s)) / 15.0

    def v(x):
        return x * k

    if kind == "switch":
        c.create_line(cx - v(7.5), cy - v(3.5), cx + v(4.5), cy - v(3.5), fill=color,
                      width=v(1.7), arrow=tk.LAST, arrowshape=(v(5), v(6), v(3)))
        c.create_line(cx + v(7.5), cy + v(3.5), cx - v(4.5), cy + v(3.5), fill=color,
                      width=v(1.7), arrow=tk.LAST, arrowshape=(v(5), v(6), v(3)))
    elif kind == "monitor":
        base = cy + v(6)
        for dx, hh in ((-6, 7), (-1.5, 12), (3, 5)):
            c.create_rectangle(cx + v(dx), base - v(hh), cx + v(dx + 3), base,
                               fill=color, outline="")
    elif kind == "apps":
        for i in range(3):
            c.create_rectangle(cx - v(7), cy - v(6) + i * v(5.5), cx + v(7), cy - v(3) + i * v(5.5),
                               fill=color, outline="")
    elif kind == "tools":
        c.create_oval(cx - v(3.6), cy - v(3.6), cx + v(3.6), cy + v(3.6),
                      outline=color, width=v(1.5))
        for a in range(0, 360, 45):
            ar = math.radians(a)
            c.create_line(cx + v(5) * math.cos(ar), cy + v(5) * math.sin(ar),
                          cx + v(7.5) * math.cos(ar), cy + v(7.5) * math.sin(ar),
                          fill=color, width=v(1.5))
    elif kind == "mouse":          # 连点器：鼠标
        c.create_oval(cx - v(5), cy - v(7), cx + v(5), cy + v(7),
                      outline=color, width=v(1.6))
        c.create_line(cx, cy - v(7), cx, cy - v(1), fill=color, width=v(1.4))
        c.create_oval(cx - v(2.2), cy - v(6.5), cx + v(2.2), cy - v(1.5),
                      outline=color, width=v(1.2))
    elif kind == "record":         # 录屏：录制圆点
        c.create_oval(cx - v(7), cy - v(7), cx + v(7), cy + v(7),
                      outline=color, width=v(1.6))
        c.create_oval(cx - v(3.6), cy - v(3.6), cx + v(3.6), cy + v(3.6),
                      fill=color, outline="")
    elif kind == "video":          # 录屏：影片
        c.create_rectangle(cx - v(7.5), cy - v(5.5), cx + v(7.5), cy + v(5.5),
                           outline=color, width=v(1.5))
        c.create_polygon(cx + v(7.5), cy - v(1.5), cx + v(7.5), cy + v(1.5),
                         cx + v(11), cy + v(4), cx + v(11), cy - v(4),
                         fill=color, outline="")
    elif kind == "clock":          # 计时 / 间隔
        c.create_oval(cx - v(7), cy - v(7), cx + v(7), cy + v(7),
                      outline=color, width=v(1.6))
        c.create_line(cx, cy - v(3.5), cx, cy, cx + v(3.5), cy + v(1.5),
                      fill=color, width=v(1.6))
    elif kind == "gear":           # 设置
        c.create_oval(cx - v(4), cy - v(4), cx + v(4), cy + v(4),
                      outline=color, width=v(1.6))
        for a in range(0, 360, 60):
            ar = math.radians(a)
            c.create_line(cx + v(4.5) * math.cos(ar), cy + v(4.5) * math.sin(ar),
                          cx + v(7) * math.cos(ar), cy + v(7) * math.sin(ar),
                          fill=color, width=v(1.6))
    elif kind == "list":           # 列表 / 记录
        for i in range(3):
            c.create_rectangle(cx - v(7.5), cy - v(6) + i * v(5.5),
                               cx + v(7.5), cy - v(3.4) + i * v(5.5),
                               fill=color, outline="")


class Sidebar(tk.Canvas):
    ROW_H = 40
    TITLE = "工具"
    SUB = ""
    VER = "1.0.0"

    def __init__(self, master, items, on_select, width=None):
        width = width or px(196)
        tk.Canvas.__init__(self, master, width=width, bg=THEME["sidebar"],
                           highlightthickness=0, bd=0)
        self.items = items
        self.on_select = on_select
        self.index = 0
        self.hover = None
        self.width_ = width
        self.row_h = px(40)
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)

    def _row_at(self, y):
        i = int((y - self.top) // self.row_h)
        return i if 0 <= i < len(self.items) else None

    def _motion(self, e):
        i = self._row_at(e.y)
        if i != self.hover:
            self.hover = i
            self.draw()
            self.configure(cursor="hand2" if i is not None else "")

    def _leave(self, _):
        if self.hover is not None:
            self.hover = None
            self.draw()

    def _click(self, e):
        i = self._row_at(e.y)
        if i is not None and i != self.index:
            self.index = i
            self.draw()
            self.on_select(i)

    def draw(self):
        self.delete("all")
        w = self.width_
        top = self.top = px(96)
        # 应用标题
        self.create_text(px(20), px(34), anchor="w", text=Sidebar.TITLE,
                         fill=THEME["text"], font=F(15, "bold"))
        if Sidebar.SUB:
            self.create_text(px(20), px(58), anchor="w", text=Sidebar.SUB,
                             fill=THEME["text3"], font=F(9))
        self.create_line(px(14), px(80), w - px(14), px(80), fill=THEME["border"])

        pad = px(12)
        hh = px(34)
        for i, (icon, label) in enumerate(self.items):
            y = top + i * self.row_h
            x1, x2 = pad, w - pad
            if i == self.index:
                rrect(self, x1, y, x2, y + hh, px(9), fill=THEME["blue"])
                color = "#FFFFFF"
            elif i == self.hover:
                rrect(self, x1, y, x2, y + hh, px(9), fill="#E3E3E8")
                color = THEME["text"]
            else:
                color = THEME["text"]
            draw_icon(self, icon, x1 + px(18), y + hh // 2, color)
            self.create_text(x1 + px(38), y + hh // 2, anchor="w", text=label,
                             fill=color, font=F(12, "bold" if i == self.index else "normal"))

        # 底部版本号
        h = self.winfo_height()
        self.create_text(px(20), h - px(26) if h > px(200) else px(640),
                         anchor="w", text="v" + Sidebar.VER,
                         fill=THEME["text3"], font=F(9))


class ScrollFrame(tk.Frame):
    def __init__(self, master, bg):
        tk.Frame.__init__(self, master, bg=bg)
        self.bg = bg
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.body)

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", self._wheel)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    def bind_wheel_all(self):
        for child in self.body.winfo_children():
            self._recurse(child)

    def _recurse(self, w):
        try:
            w.bind("<MouseWheel>", self._wheel)
        except Exception:  # noqa: BLE001
            pass
        for c in w.winfo_children():
            self._recurse(c)

# ============================================================ 应用基类


class AppBase:
    """Apple 风格窗口基类：侧边栏 + 多页 + 底部日志 + 线程安全 UI 队列"""

    PAGES = [("main", "主页")]
    APP_NAME = "工具"
    APP_VERSION = "1.0.0"
    APP_SUB = ""

    def __init__(self, root: tk.Tk, size=(980, 720), minsize=(900, 640)):
        self.root = root
        self.busy = False
        Sidebar.TITLE = self.APP_NAME
        Sidebar.SUB = self.APP_SUB
        Sidebar.VER = self.APP_VERSION
        root.title("%s v%s" % (self.APP_NAME, self.APP_VERSION))
        # 坑：这里必须用 GetSystemMetrics 取物理像素，不能用 winfo_screenwidth()。
        # PerMonitorV2 下 winfo_screenwidth 返回「逻辑像素」，而 px() 返回物理像素，
        # 两者单位不一致会算出负的/过大的窗口尺寸。
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        sw = sw or root.winfo_screenwidth()
        sh = sh or root.winfo_screenheight()
        gw, gh = min(px(size[0]), max(px(640), sw - px(24))), min(px(size[1]), max(px(480), sh - px(24)))
        root.geometry("%dx%d" % (gw, gh))
        root.minsize(min(px(minsize[0]), sw - px(40)), min(px(minsize[1]), sh - px(40)))
        root.configure(bg=THEME["window"])

        self.ui_q = queue.Queue()
        self.root.after(60, self._drain_ui)

        self._style()
        self._build()
        self._build_pages()
        for sc in self.pages.values():
            sc.bind_wheel_all()
        self.show_page(0, silent=True)
        self._build_log(self._right)
        self.on_ready()

    # -- 生命周期回调
    def on_ready(self):
        """子类可覆盖：初始化完成后调用（在这里启动后台任务）"""

    # -- 线程安全 UI
    def ui(self, fn, *args):
        self.ui_q.put((fn, args))

    def _drain_ui(self):
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    self._log_direct("界面更新出错: " + traceback.format_exc(limit=3),
                                     "error")
        except queue.Empty:
            pass
        self.root.after(60, self._drain_ui)

    def run_async(self, fn):
        """在后台线程跑 fn，异常自动记日志，结束后自动 self.ui(self.on_idle)"""
        def work():
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.log("内部错误: " + traceback.format_exc(limit=3), "error")
        threading.Thread(target=work, daemon=True).start()

    def on_idle(self):
        """后台任务结束后的默认收尾，子类可覆盖"""

    # -- 日志
    def log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.ui(self._log_direct, "[%s] %s\n" % (ts, msg), level)

    def _log_direct(self, text: str, level: str = "info"):
        try:
            self.logbox.configure(state="normal")
            self.logbox.insert("end", text, level)
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # -- 样式
    def _style(self):
        s = ttk.Style()
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Apple.Treeview", background=THEME["card"],
                    fieldbackground=THEME["card"], foreground=THEME["text"],
                    borderwidth=0, relief="flat", rowheight=px(32), font=F(10),
                    bordercolor=THEME["card"], lightcolor=THEME["card"],
                    darkcolor=THEME["card"])
        s.configure("Apple.Treeview.Heading", background=THEME["card"],
                    foreground=THEME["text3"], borderwidth=0, relief="flat",
                    font=F(9, "bold"))
        s.map("Apple.Treeview",
              background=[("selected", THEME["blue"])],
              foreground=[("selected", "#FFFFFF")])
        style_combobox(s)

    # -- 布局
    def _build(self):
        main = tk.Frame(self.root, bg=THEME["window"])
        main.pack(fill="both", expand=True)
        if len(self.PAGES) > 1:
            self.sidebar = Sidebar(main, self.PAGES, on_select=self.show_page)
            self.sidebar.pack(side="left", fill="y")
        self._right = tk.Frame(main, bg=THEME["window"])
        self._right.pack(side="left", fill="both", expand=True)

        head = tk.Frame(self._right, bg=THEME["window"])
        head.pack(fill="x", padx=px(30), pady=(px(22), px(10)))
        self.page_title = tk.Label(head, text=self.PAGES[0][1],
                                   bg=THEME["window"], fg=THEME["text"],
                                   font=F(24, "bold"))
        self.page_title.pack(side="left")
        if len(self.PAGES) == 1:
            head.pack_forget()

        self.pages_box = tk.Frame(self._right, bg=THEME["window"])
        self.pages_box.pack(fill="both", expand=True)
        self.pages = {}
        for key, _lbl in self.PAGES:
            sc = ScrollFrame(self.pages_box, THEME["window"])
            sc.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = sc
        self.pages_box.grid_rowconfigure(0, weight=1)
        self.pages_box.grid_columnconfigure(0, weight=1)

    def _build_pages(self):
        """子类实现：往 self.pages[key].body 里塞内容"""

    def _card(self, parent, title=None, pad=18, radius=None):
        radius = px(16) if radius is None else radius
        c = RoundedFrame(parent, radius=radius, fill=THEME["card"], pad=pad)
        c.pack(fill="x", padx=px(28), pady=(px(0), px(12)))
        if title:
            tk.Label(c.body, text=title, bg=THEME["card"], fg=THEME["text"],
                     font=F(13, "bold")).pack(anchor="w", pady=(px(0), px(12)))
        return c.body

    def show_page(self, index, silent=False):
        key = self.PAGES[index][0]
        for i, (k, _l) in enumerate(self.PAGES):
            sc = self.pages[k]
            if i == index:
                sc.tkraise()
            else:
                sc.lower()
        if hasattr(self, "page_title"):
            self.page_title.configure(text=self.PAGES[index][1])
        if hasattr(self, "sidebar"):
            self.sidebar.index = index
            self.sidebar.draw()
        if not silent:
            self.on_page_shown(key)

    def on_page_shown(self, key):
        """子类可覆盖"""

    def _build_log(self, parent):
        box = tk.Frame(parent, bg=THEME["window"])
        box.pack(fill="x", padx=px(28), pady=(px(4), px(16)))
        self.log_box = box          # 供截图脚本临时隐藏
        card = RoundedFrame(box, radius=px(14), fill=THEME["card"], pad=12)
        card.pack(fill="x")
        top = tk.Frame(card.body, bg=THEME["card"])
        top.pack(fill="x")
        tk.Label(top, text="操作日志", bg=THEME["card"], fg=THEME["text"],
                 font=F(11, "bold")).pack(side="left")
        AppleButton(top, "清空", command=self.clear_log, style="plain",
                    width=px(54), height=px(24), radius=px(7), bg=THEME["card"],
                    font=F(9)).pack(side="right")
        self.logbox = tk.Text(card.body, height=3, font=F(9, mono=True),
                              bg=THEME["soft"], fg=THEME["text2"], relief="flat",
                              bd=0, highlightthickness=0, wrap="word")
        self.logbox.pack(fill="x", pady=(px(6), px(0)))
        self.logbox.tag_configure("ok", foreground=THEME["green"])
        self.logbox.tag_configure("error", foreground=THEME["red"])
        self.logbox.tag_configure("warn", foreground=THEME["orange"])
        self.logbox.configure(state="disabled")

    @classmethod
    def run(cls, argv=None):
        setup_dpi()                     # 必须在建 Tk() 之前
        root = tk.Tk()
        init_ui_scale(root)             # 传 root 才能拿到该显示器真实 DPI
        apply_tk_scaling(root)
        apply_base_fonts(root)          # 命名默认字体也要缩放，否则下拉列表字小
        app = cls(root)
        root.mainloop()
        return app


def open_uri(uri: str):
    try:
        os.startfile(uri)  # noqa: S606
        return True
    except Exception:  # noqa: BLE001
        return False
