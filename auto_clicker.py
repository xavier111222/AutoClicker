# -*- coding: utf-8 -*-
"""
极速连点器 (AutoClicker) —— 独立单文件 Windows 小工具
=====================================================
可配置点击间隔、按键、次数与位置，支持全局热键（F1-F12）开始/停止、
倒计时启动、随机偏移与实时 CPS 统计。

许可证: MIT
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  必须显式导入，打包后 ctypes.wintypes 属性才可用
import json
import os
import random
import sys
import threading
import time
import traceback
from datetime import datetime

from ui_kit import (AppBase, AppleButton, F, Field, Pill, RoundedFrame, ScrollFrame,
                    SegmentedControl, THEME, ToggleSwitch, open_uri, px,
                    setup_dpi)

import macro
import win_input

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "极速连点器"
APP_VERSION = "1.0.2"

# ---------------------------------------------------------------- Win32 输入

_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def send_click(button: int = 0, double: bool = False):
    """在当前鼠标位置发送一次点击。button: 0=左 1=右 2=中"""
    down, up = ((_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
                (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
                (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP))[button]
    times = 2 if double else 1
    for _ in range(times):
        for flags in (down, up):
            inp = _INPUT(type=_INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(0, 0, 0, flags, 0, 0)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if double:
            time.sleep(0.04)


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move_cursor(x: int, y: int):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def screen_size():
    u = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    ctypes.windll.user32.GetDesktopWindow()
    return (ctypes.windll.user32.GetSystemMetrics(0),
            ctypes.windll.user32.GetSystemMetrics(1))


# 坑：VK_F1 = 0x70，正确的换算是 0x70 + i - 1。
# 之前写成 0x70 + i，整体偏移一位：F6 存成了 0x76（那是 F7），
# 于是「按 F6 没反应」，而流程宏热键硬编码的 0x76(F7) 又和它撞车。
HOTKEYS = {("F%d" % i): 0x70 + i - 1 for i in range(1, 13)}
BUTTONS = {0: "左键", 1: "右键", 2: "中键"}


# ============================================================ 后端：点击引擎

class ClickEngine:
    """按设定参数循环点击；节拍用 perf_counter 累加，避免 sleep 漂移"""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.state = "idle"          # idle / countdown / running
        self.clicks = 0
        self.started = 0.0
        # 真正开始点击的时刻。坑：倒计时期间 clicks 恒为 0，若用 self.started
        # 算速度，倒计时的 3~10 秒会被算进分母，显示的速度明显偏低；
        # 而第一个 tick 时 clicks=1、耗时≈0，速度会飙到几千次再回落 —— 看着像乱跳。
        self.running_since = 0.0
        self._samples = []            # (时刻, 累计点击数) 滚动窗口，算平滑速度
        self.on_event = lambda *a: None

    # -- 参数
    def config(self):
        return dict(interval=1.0, count=0, button=0, double=False,
                    mode="follow", x=0, y=0, jitter=0)

    @property
    def running(self):
        return self.state in ("countdown", "running")

    def start(self, cfg, countdown=3):
        if self.running:
            return False
        self.cfg = cfg
        self.stop_flag.clear()
        self.clicks = 0
        self.state = "countdown"
        self.started = time.perf_counter()
        self.running_since = 0.0
        self._samples = []
        self._cd_left = countdown
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if not self.running:
            return
        self.stop_flag.set()
        self.state = "idle"

    # -- 内部
    def _emit(self, kind, *args):
        try:
            self.on_event(kind, *args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _speed(self, now):
        """最近约 1.2 秒的滑动窗口速度（次/秒）。

        不用「总数 / 总时长」：那样刚开始时 clicks 少、分母也小，数字会剧烈跳动；
        滑动窗口既能反映瞬时速度，又足够平滑。
        """
        self._samples.append((now, self.clicks))
        cut = now - 1.2
        while len(self._samples) > 2 and self._samples[0][0] < cut:
            self._samples.pop(0)
        t0, c0 = self._samples[0]
        dt = now - t0
        if dt < 0.05:                 # 窗口太窄，先不给结论
            return 0.0
        return (self.clicks - c0) / dt

    def _do_click(self):
        c = self.cfg
        if c["mode"] == "fixed":
            x, y = c["x"], c["y"]
            if c["jitter"]:
                x += random.randint(-c["jitter"], c["jitter"])
                y += random.randint(-c["jitter"], c["jitter"])
            move_cursor(x, y)
            time.sleep(0.002)
        elif c["jitter"]:
            x, y = cursor_pos()
            move_cursor(x + random.randint(-c["jitter"], c["jitter"]),
                        y + random.randint(-c["jitter"], c["jitter"]))
        send_click(c["button"], c["double"])
        self.clicks += 1

    def _loop(self):
        c = self.cfg
        try:
            n = self._cd_left
            while n > 0 and not self.stop_flag.is_set():
                self._emit("countdown", n)
                time.sleep(1.0)
                n -= 1
            if self.stop_flag.is_set():
                self._emit("stopped", self.clicks)
                return
            self.state = "running"
            self.running_since = time.perf_counter()
            self._samples = [(self.running_since, 0)]
            self._emit("started", c)
            next_t = self.running_since
            while not self.stop_flag.is_set():
                self._do_click()
                limit = c["count"]
                if limit and self.clicks >= limit:
                    self.state = "idle"
                    self._emit("finished", self.clicks)
                    return
                next_t += c["interval"]
                now = time.perf_counter()
                delay = next_t - now
                if delay < -1.0:          # 落后太多（例如系统休眠后）重新对时
                    next_t = now
                    delay = 0
                # 计时只统计真正在点击的时间，不含倒计时
                self._emit("tick", self.clicks, now - self.running_since,
                           self._speed(now))
                self.stop_flag.wait(delay)
            self.state = "idle"
            self._emit("stopped", self.clicks)
        except Exception:  # noqa: BLE001
            self._emit("error", traceback.format_exc(limit=3))


# ============================================================ GUI

class ClickerApp(AppBase):
    APP_NAME = APP_NAME
    APP_VERSION = APP_VERSION
    APP_SUB = "热键一按，指针飞舞"
    PAGES = [("main", "连点控制"), ("params", "参数设置"),
             ("flow", "流程宏"), ("about", "关于")]

    def __init__(self, root):
        self.eng = ClickEngine()
        self.eng.on_event = self._on_engine
        # 变量
        self.interval_val = tk.StringVar(value="100")
        self.interval_unit = tk.StringVar(value="毫秒")
        self.count_val = tk.StringVar(value="0")
        self.button_val = tk.IntVar(value=0)
        self.double_var = tk.BooleanVar(value=False)
        self.mode_val = tk.StringVar(value="follow")
        self.pos_x = tk.StringVar(value="0")
        self.pos_y = tk.StringVar(value="0")
        self.jitter_val = tk.StringVar(value="0")
        self.hotkey_val = tk.StringVar(value="F6")
        self.hold_mode = tk.BooleanVar(value=False)
        self.countdown_val = tk.IntVar(value=3)
        self.status_var = tk.StringVar(value="就绪")
        self.stat_clicks = tk.StringVar(value="0")
        self.stat_cps = tk.StringVar(value="0.0")
        self.stat_time = tk.StringVar(value="0:00")
        #坑：这里曾用 `self._stat_win` 保存 Tk 根窗口，却又调`.is_alive()`
        # （那是threading.Thread 的方法）→ 停止连点时抛
        # AttributeError: '_tkinter.tkapp' object has no attribute 'is_alive'。
        # 统计区现在就在主窗口里，用布尔标记即可，别再假装有独立窗口。
        self._stat_shown = False
        self._hotkey_down = False
        # 流程宏
        self.macro_eng = macro.MacroEngine()
        self.macro_eng.on_event = self._on_macro
        self.flow_steps = [dict(s) for s in macro.DEFAULT_STEPS]
        self.flow_cur = 0
        self.flow_repeat_var = tk.IntVar(value=1)
        self._flow_hk_down = False
        super().__init__(root, size=(980, 900), minsize=(900, 700))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -------------------------------------------------- 页面
    def _build_pages(self):
        self._page_main()
        self._page_params()
        self._page_flow()
        self._page_about()

    def _page_main(self):
        p = self.pages["main"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()

        # 状态大卡
        box = self._card(p, pad=20)
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x")
        self.state_pill = Pill(row, "● 就绪", THEME["text3"], bg=THEME["card"], size=12)
        self.state_pill.pack(side="left")
        tk.Label(row, text="全局热键", bg=THEME["card"], fg=THEME["text3"],
                 font=F(10)).pack(side="right", padx=(px(8), px(6)))
        self.hk_pill = Pill(row, "F6", THEME["blue"], bg=THEME["card"], size=11)
        self.hk_pill.pack(side="right")

        # 统计
        stat = tk.Frame(box, bg=THEME["card"])
        stat.pack(fill="x", pady=(px(16), px(0)))
        for i, (name, var, unit) in enumerate((
                ("已点击", self.stat_clicks, "次"),
                ("当前速度", self.stat_cps, "次/秒"),
                ("运行时长", self.stat_time, ""))):
            card = RoundedFrame(stat, radius=px(12), fill=THEME["soft"], pad=14)
            card.pack(side="left", fill="both", expand=True,
                      padx=(px(0) if i == 0 else px(8), 0))
            c = card.body
            tk.Label(c, text=name, bg=THEME["soft"], fg=THEME["text3"],
                     font=F(9)).pack(anchor="w", pady=(px(0), px(2)))
            tk.Label(c, textvariable=var, bg=THEME["soft"], fg=THEME["text"],
                     font=F(22, "bold")).pack(anchor="w")
            tk.Label(c, text=unit, bg=THEME["soft"], fg=THEME["text3"],
                     font=F(9)).pack(anchor="w")

        # 主按钮
        acts = tk.Frame(p, bg=THEME["window"])
        acts.pack(fill="x", padx=px(28), pady=(px(0), px(12)))
        self.btn_start = AppleButton(acts, "▶  开始连点", command=self.toggle_start,
                                     style="success", width=px(300), height=px(58),
                                     radius=px(16), font=F(16), fill=True)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_stop = AppleButton(acts, "■  停止", command=self.stop,
                                    style="danger", width=px(300), height=px(58),
                                    radius=px(16), font=F(16), fill=True)
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(px(12), 0))

        # 提示
        tip = self._card(p, "使用提示")
        tk.Label(tip,
                 text="1) 鼠标移到目标位置后按 F6（或点「开始连点」）即可开始，再按一次停止。\n"
                      "2) 想连点某个固定坐标：到「参数设置」选「固定坐标」，"
                      "点「取当前鼠标位置」再填入。\n"
                      "3) 勾选「按住热键连点」后，按住热键持续点击、松开即停。\n"
                      "4) 间隔 < 10ms 时系统可能来不及响应，建议 30ms 以上更稳定。",
                 bg=THEME["card"], fg=THEME["text2"], font=F(10),
                 justify="left", wraplength=px(620)).pack(anchor="w")

    def _page_params(self):
        p = self.pages["params"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()

        box = self._card(p, "点击参数")
        r1 = tk.Frame(box, bg=THEME["card"])
        r1.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r1, text="点击间隔", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        e1 = Field(r1, textvariable=self.interval_val, width_chars=8, font=F(11))
        e1.pack(side="left", padx=px(10))
        self.unit_seg = SegmentedControl(
            r1, ["毫秒", "秒", "分钟"], command=self._on_unit, width=px(220),
            height=px(34), bg=THEME["card"])
        self.unit_seg.pack(side="left")
        self.unit_seg.select(0)

        r2 = tk.Frame(box, bg=THEME["card"])
        r2.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r2, text="点击次数", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        Field(r2, textvariable=self.count_val, width_chars=8, font=F(11)).pack(
            side="left", padx=px(10))
        tk.Label(r2, text="0 = 一直点，直到手动停止", bg=THEME["card"],
                 fg=THEME["text3"], font=F(9)).pack(side="left")

        r3 = tk.Frame(box, bg=THEME["card"])
        r3.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r3, text="按键", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(0), px(10)))
        self.btn_seg = SegmentedControl(r3, ["左键", "右键", "中键"],
                                        command=lambda i: self.button_val.set(i),
                                        width=px(220), height=px(34),
                                        bg=THEME["card"])
        self.btn_seg.pack(side="left")
        self.btn_seg.select(0)
        ToggleSwitch(r3, variable=self.double_var, bg=THEME["card"]).pack(side="left",
                                                                        padx=px(12))
        tk.Label(r3, text="双击", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(6), 0))

        r4 = tk.Frame(box, bg=THEME["card"])
        r4.pack(fill="x")
        tk.Label(r4, text="随机偏移(px)", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        Field(r4, textvariable=self.jitter_val, width_chars=8, font=F(11)).pack(
            side="left", padx=px(10))
        tk.Label(r4, text="每次点击在目标点附近随机抖动，可避免固定坐标被识别",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9)).pack(side="left")

        # 位置
        box2 = self._card(p, "点击位置")
        r5 = tk.Frame(box2, bg=THEME["card"])
        r5.pack(fill="x", pady=(px(0), px(12)))
        self.pos_seg = SegmentedControl(r5, ["跟随鼠标当前位置", "固定坐标"],
                                        command=self._on_pos_mode, width=px(320),
                                        height=px(34), bg=THEME["card"])
        self.pos_seg.pack(side="left")
        self.pos_seg.select(0)

        r6 = tk.Frame(box2, bg=THEME["card"])
        r6.pack(fill="x")
        tk.Label(r6, text="X", bg=THEME["card"], fg=THEME["text2"],
                 font=F(10)).pack(side="left")
        Field(r6, textvariable=self.pos_x, width_chars=9, font=F(11, mono=True)).pack(
            side="left", padx=px(6))
        tk.Label(r6, text="Y", bg=THEME["card"], fg=THEME["text2"],
                 font=F(10)).pack(side="left", padx=(px(12), px(6)))
        Field(r6, textvariable=self.pos_y, width_chars=9, font=F(11, mono=True)).pack(
            side="left")
        AppleButton(r6, "取当前鼠标位置", command=self.pick_pos, style="secondary",
                    width=px(140), height=px(32), radius=px(9),
                    font=F(10)).pack(side="left", padx=px(14))
        self.pos_label = tk.Label(r6, text="", bg=THEME["card"],
                                  fg=THEME["green"], font=F(10))
        self.pos_label.pack(side="left")

        # 热键
        box3 = self._card(p, "热键")
        r7 = tk.Frame(box3, bg=THEME["card"])
        r7.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r7, text="开始 / 停止热键", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        self.hk_combo = ttk.Combobox(r7, textvariable=self.hotkey_val,
                                     values=list(HOTKEYS.keys()), width=8,
                                     state="readonly", font=F(11))
        self.hk_combo.pack(side="left", padx=px(10), ipady=px(3))
        self.hk_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_hotkey())
        ToggleSwitch(r7, variable=self.hold_mode, command=self._on_hold, bg=THEME["card"]).pack(
            side="left", padx=px(16))
        tk.Label(r7, text="按住热键连点（松开停止）", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(6), 0))

        r8 = tk.Frame(box3, bg=THEME["card"])
        r8.pack(fill="x")
        tk.Label(r8, text="开始倒计时(秒)", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        Field(r8, textvariable=self.countdown_val, width_chars=6, font=F(11),
              spin=True, from_=0, to=10).pack(side="left", padx=px(10))
        tk.Label(r8, text="倒计时结束自动开始，方便你把手放到目标位置",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9)).pack(side="left")

    # -------------------------------------------------- 流程宏
    def _page_flow(self):
        p = self.pages["flow"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()

        # -- 顶部：状态 + 运行控制
        box = self._card(p, pad=18)
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x")
        self.flow_pill = Pill(row, "● 未运行", THEME["text3"], bg=THEME["card"], size=12)
        self.flow_pill.pack(side="left")
        tk.Label(row, text="重复轮数", bg=THEME["card"], fg=THEME["text3"],
                 font=F(10)).pack(side="right", padx=(px(8), px(6)))
        self.flow_repeat = Field(row, textvariable=self.flow_repeat_var,
                                 width_chars=6, font=F(11), spin=True,
                                 from_=1, to=9999)
        self.flow_repeat.pack(side="right")
        self.flow_repeat.widget.configure(state="readonly", readonlybackground=THEME["card"])
        self.flow_hk_pill = Pill(row, "F7", THEME["green"], bg=THEME["card"], size=11)
        self.flow_hk_pill.pack(side="right", padx=(px(10), px(4)))
        tk.Label(row, text="启动热键", bg=THEME["card"], fg=THEME["text3"],
                 font=F(10)).pack(side="right")

        # -- 按钮条
        acts = tk.Frame(p, bg=THEME["window"])
        acts.pack(fill="x", padx=px(28), pady=(px(0), px(12)))
        self.btn_flow_run = AppleButton(
            acts, "▶  运行流程", command=self.toggle_flow, style="success",
            width=px(300), height=px(52), radius=px(14), font=F(15), fill=True)
        self.btn_flow_run.pack(side="left", fill="x", expand=True)
        AppleButton(acts, "＋ 添加步骤", command=self.flow_add, style="secondary",
                    width=px(150), height=px(52), radius=px(14), font=F(13),
                    fill=True).pack(side="left", padx=(px(10), 0))

        # -- 步骤列表
        lb = self._card(p, "步骤列表（按顺序执行）")
        self.flow_list = tk.Listbox(
            lb, font=F(11), relief="flat", highlightthickness=0,
            bg=THEME["card"], activestyle="none", selectmode="browse",
            height=6, bd=0)
        self.flow_list.pack(fill="both", expand=True)
        self.flow_list.bind("<<ListboxSelect>>", self._flow_on_select)

        # 步骤编辑区
        ed = self._card(p, "步骤设置")
        g = tk.Frame(ed, bg=THEME["card"])
        g.pack(fill="x")
        for c in range(4):
            g.grid_columnconfigure(c, weight=1)

        def _lab(txt, r, c):
            tk.Label(g, text=txt, bg=THEME["card"], fg=THEME["text3"],
                     font=F(9)).grid(row=r * 2, column=c, sticky="w",
                                      padx=(0, px(14)))

        def _ent(parent, r, c, w=8):
            e = Field(parent, width_chars=w, font=F(10))
            e.grid(row=r * 2 + 1, column=c, sticky="ew", padx=(0, px(14)),
                   pady=(px(2), px(10)))
            return e

        _lab("类型", 0, 0)
        self.ed_type = ttk.Combobox(g, values=[lbl for _k, lbl in macro.STEP_TYPES],
                                    state="readonly", style="Apple.Combo", width=11)
        self.ed_type.grid(row=1, column=0, sticky="ew", padx=(0, px(14)),
                          pady=(px(2), px(10)))
        self.ed_type.bind("<<ComboboxSelected>>", lambda e: self._flow_sync_type())

        _lab("作用目标", 0, 1)
        self.ed_target = ttk.Combobox(g, values=list(macro.TARGET_LABEL.values()),
                                      state="readonly", style="Apple.Combo", width=11)
        self.ed_target.grid(row=1, column=1, sticky="ew", padx=(0, px(14)),
                            pady=(px(2), px(10)))
        self.ed_target.bind("<<ComboboxSelected>>", lambda e: self._flow_refresh_targets())

        _lab("鼠标按键", 0, 2)
        self.ed_button = ttk.Combobox(g, values=["左键", "右键", "中键"],
                                      state="readonly", style="Apple.Combo", width=6)
        self.ed_button.grid(row=1, column=2, sticky="ew", padx=(0, px(14)),
                            pady=(px(2), px(10)))

        _lab("取点（屏幕绝对坐标）", 0, 3)
        pick = tk.Frame(g, bg=THEME["card"])
        pick.grid(row=1, column=3, sticky="ew", padx=(0, px(14)),
                  pady=(px(2), px(10)))
        tk.Label(pick, text="X", bg=THEME["card"], fg=THEME["text3"],
                 font=F(9)).grid(row=0, column=0, sticky="w", padx=(0, px(14)))
        self.ed_x = Field(pick, width_chars=7, font=F(10))
        self.ed_x.grid(row=1, column=0, sticky="w")
        tk.Label(pick, text="Y", bg=THEME["card"], fg=THEME["text3"],
                 font=F(9)).grid(row=0, column=1, sticky="w", padx=(px(6), px(8)))
        self.ed_y = Field(pick, width_chars=7, font=F(10))
        self.ed_y.grid(row=1, column=1, sticky="w")
        AppleButton(pick, "取点", command=self.flow_pick_point, style="secondary",
                    width=px(54), height=px(26), radius=px(8), font=F(10),
                    bg=THEME["card"]).grid(row=1, column=2, sticky="w", padx=(px(8), 0))

        # 第二行：等待 / 偏移 / 按键
        _lab("等待毫秒（等待/循环步骤用）", 1, 0)
        self.ed_ms = _ent(g, 1, 0, 8)
        _lab("随机偏移 ±像素", 1, 1)
        self.ed_jit = _ent(g, 1, 1, 6)
        _lab("按键（按键步骤用）", 1, 2)
        self.ed_vk = ttk.Combobox(g, values=list(macro.VK_NAMES.keys()),
                                  state="readonly", style="Apple.Combo", width=11)
        self.ed_vk.grid(row=3, column=2, sticky="ew", padx=(0, px(14)),
                        pady=(px(2), px(10)))
        _lab("双击该步骤", 1, 3)
        self.ed_double = tk.BooleanVar(value=False)
        ToggleSwitch(g, variable=self.ed_double, bg=THEME["card"]).grid(
            row=3, column=3, sticky="w", padx=(0, px(14)), pady=(px(2), px(10)))

        # 目标窗口（跨应用后台连点）
        g3 = tk.Frame(ed, bg=THEME["card"])
        g3.pack(fill="x", pady=(px(2), px(10)))
        tk.Label(g3, text="目标窗口", bg=THEME["card"],
                 fg=THEME["text3"], font=F(9)).pack(side="left")
        self.ed_win = ttk.Combobox(g3, values=["（未选择）"], state="readonly",
                                   style="Apple.Combo", width=46)
        self.ed_win.pack(side="left", fill="x", expand=True,
                         padx=(px(8), px(8)))
        AppleButton(g3, "刷新窗口", command=self.flow_refresh_windows,
                    style="secondary", width=px(84), height=px(28),
                    radius=px(8), font=F(10), bg=THEME["card"]).pack(side="left")

        # 输入文本
        g2 = tk.Frame(ed, bg=THEME["card"])
        g2.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(g2, text="输入文本", bg=THEME["card"], fg=THEME["text3"],
                 font=F(9)).pack(side="left")
        self.ed_text = Field(g2, font=F(10))
        self.ed_text.pack(side="left", fill="x", expand=True,
                          padx=(px(8), 0))

        # 步骤操作
        ops = tk.Frame(ed, bg=THEME["card"])
        ops.pack(fill="x")
        for text, cmd, st in (("保存修改", self.flow_apply, "primary"),
                              ("删除", self.flow_del, "secondary"),
                              ("上移", lambda: self.flow_move(-1), "secondary"),
                              ("下移", lambda: self.flow_move(1), "secondary"),
                              ("清空", self.flow_clear, "secondary")):
            AppleButton(ops, text, command=cmd, style=st, width=px(84),
                        height=px(30), radius=px(9), font=F(10),
                        bg=THEME["card"]).pack(side="left", padx=(0, px(8)))

        self.flow_hint = tk.Label(
            ed, text="", bg=THEME["card"], fg=THEME["text3"], font=F(9),
            wraplength=px(640), justify="left")
        self.flow_hint.pack(anchor="w", pady=(px(12), 0))

        self._flow_windows = []
        self._flow_refresh_list()

    # -- 流程页逻辑
    def _flow_refresh_list(self):
        lb = self.flow_list
        lb.delete(0, "end")
        for i, s in enumerate(self.flow_steps):
            lb.insert("end", "  %d.  %s" % (i + 1, macro.describe(s)))
        if self.flow_steps:
            lb.selection_clear(0, "end")
            lb.selection_set(0)
            self._flow_load(0)

    def _flow_on_select(self, _evt=None):
        sel = self.flow_list.curselection()
        if sel:
            self._flow_load(sel[0])

    def _flow_load(self, idx):
        if not (0 <= idx < len(self.flow_steps)):
            return
        s = self.flow_steps[idx]
        self.flow_cur = idx
        # TYPE_LABEL / TARGET_LABEL 都是 {内部值: 中文标签}，
        # 存的是内部值，取的是中文标签，所以直接查、不反转。
        self.ed_type.set(macro.TYPE_LABEL.get(s.get("type", "click"), "点击"))
        self.ed_target.set(macro.TARGET_LABEL.get(s.get("target", "follow"), "跟随鼠标"))
        self.ed_button.set(macro.BUTTON_LABEL.get(s.get("button", 0), "左键"))
        self.ed_x.delete(0, "end")
        self.ed_x.insert(0, str(s.get("x", 0)))
        self.ed_y.delete(0, "end")
        self.ed_y.insert(0, str(s.get("y", 0)))
        self.ed_ms.delete(0, "end")
        self.ed_ms.insert(0, str(s.get("ms", 500)))
        self.ed_jit.delete(0, "end")
        self.ed_jit.insert(0, str(s.get("jitter", 0)))
        self.ed_text.delete(0, "end")
        self.ed_text.insert(0, s.get("text", "") or "")
        self.ed_vk.set(s.get("vk_name", "") or "")
        self.ed_double.set(bool(s.get("double")))
        self._flow_sync_target_widget(s)
        self._flow_sync_type()

    def _flow_sync_target_widget(self, s=None):
        """把步骤的 hwnd/title 回填到窗口下拉框。"""
        s = s or (self.flow_steps[self.flow_cur] if self.flow_cur is not None else {})
        hwnd = int(s.get("hwnd") or 0)
        if hwnd and self._flow_windows:
            for i, w in enumerate(self._flow_windows):
                if w.hwnd == hwnd:
                    self.ed_win.current(i + 1)   # 第 0 项是「（未选择）」占位
                    return
        self.ed_win.current(0)

    def _flow_sync_type(self):
        """按类型 + 作用目标给出针对性提示。"""
        t = self.ed_type.get()
        tgt = self.ed_target.get()
        base = {
            "点击": "点击：在目标位置发送一次鼠标点击。",
            "双击": "双击：连续发送两次点击（也可在下方打开「双击该步骤」开关）。",
            "移动": "移动：只把光标移到目标位置，不点击。",
            "等待": "等待：暂停「等待毫秒」后继续下一步，常用于给界面反应时间。",
            "输入文本": "输入文本：向目标输入「输入文本」框里的文字。",
            "按键": "按键：发送一次按键，如 Enter / Ctrl / F5。",
            "循环": "循环：在顶部「重复轮数」里设置整个流程跑几遍。",
        }.get(t, "")
        extra = ""
        if tgt == "指定窗口":
            extra = ("　当前为「指定窗口」：X/Y 填的是该窗口的**客户区坐标**"
                     "（窗口左上角为 0,0，不含标题栏），点击会作为窗口消息投递，"
                     "不移动鼠标、不抢焦点，窗口在后台也能响应。")
        elif tgt == "固定坐标":
            extra = "　当前为「固定坐标」：X/Y 填屏幕绝对坐标，可点「取点」自动读取当前鼠标位置。"
        else:
            extra = "　当前为「跟随鼠标」：在光标所在位置点击，适合游戏和网页这类自绘界面。"
        if hasattr(self, "flow_hint"):
            self.flow_hint.config(text=base + extra)

    def _flow_refresh_targets(self):
        tgt = self.ed_target.get()
        if tgt == "指定窗口" and not self._flow_windows:
            self.flow_refresh_windows()
        self._flow_sync_type()

    def flow_refresh_windows(self):
        self.run_async(self._flow_refresh_windows_bg)

    def _flow_refresh_windows_bg(self):
        wins = [w for w in win_input.list_windows(min_w=160, min_h=120)]
        self.ui(self._flow_set_windows, wins)

    def _flow_set_windows(self, wins):
        self._flow_windows = wins
        vals = ["（未选择）"] + ["%s  —  %s" % (w.title[:44], w.exe or "?") for w in wins]
        self.ed_win["values"] = vals
        self.ed_win.current(0)
        self._flow_sync_target_widget()
        self.log("已刷新窗口列表：%d 个可用" % len(wins), "ok")

    def flow_pick_point(self):
        """取当前鼠标坐标填入 X/Y。"""
        mx, my = win_input.cursor_pos()
        self.ed_x.delete(0, "end")
        self.ed_x.insert(0, str(mx))
        self.ed_y.delete(0, "end")
        self.ed_y.insert(0, str(my))
        self.log("已取点：(%d, %d)" % (mx, my), "ok")

    def flow_add(self):
        self._flow_flush()
        nid = max([s["id"] for s in self.flow_steps], default=0) + 1
        self.flow_steps.append(macro.new_step(nid, "click"))
        self._flow_refresh_list()
        self.flow_list.selection_clear(0, "end")
        self.flow_list.selection_set("end")
        self._flow_load(len(self.flow_steps) - 1)

    def flow_del(self):
        if self.flow_cur is None or not self.flow_steps:
            return
        self.flow_steps.pop(self.flow_cur)
        self.flow_cur = None
        self._flow_refresh_list()

    def flow_move(self, d):
        i = self.flow_cur
        if i is None:
            return
        j = i + d
        if not (0 <= j < len(self.flow_steps)):
            return
        self.flow_steps[i], self.flow_steps[j] = self.flow_steps[j], self.flow_steps[i]
        self._flow_refresh_list()
        self.flow_list.selection_clear(0, "end")
        self.flow_list.selection_set(j)
        self._flow_load(j)

    def flow_clear(self):
        if not self.flow_steps:
            return
        if not messagebox.askyesno("清空流程", "确定要删除全部 %d 个步骤吗？"
                                   % len(self.flow_steps), parent=self.root):
            return
        self.flow_steps = []
        self.flow_cur = None
        self._flow_refresh_list()

    def _flow_flush(self):
        """把编辑区内容写回当前步骤。"""
        i = self.flow_cur
        if i is None or not (0 <= i < len(self.flow_steps)):
            return
        s = self.flow_steps[i]
        # 中文标签 → 内部值（与 _flow_load 相反）
        inv_t = {v: k for k, v in macro.TYPE_LABEL.items()}
        inv_g = {v: k for k, v in macro.TARGET_LABEL.items()}
        s["type"] = inv_t.get(self.ed_type.get(), "click")
        s["target"] = inv_g.get(self.ed_target.get(), "follow")
        s["button"] = {"左键": 0, "右键": 1, "中键": 2}.get(self.ed_button.get(), 0)
        for key, wdg in (("x", self.ed_x), ("y", self.ed_y),
                         ("ms", self.ed_ms), ("jitter", self.ed_jit)):
            try:
                s[key] = int(wdg.get().strip() or 0)
            except ValueError:
                s[key] = 0
        s["text"] = self.ed_text.get()
        s["double"] = bool(self.ed_double.get())
        vk_name = self.ed_vk.get()
        s["vk_name"] = vk_name
        s["vk"] = macro.VK_NAMES.get(vk_name, 0)
        # 目标窗口
        try:
            idx = self.ed_win.current()
        except Exception:  # noqa: BLE001
            idx = -1
        if (self.ed_target.get() == "指定窗口" and idx and idx > 0
                and idx - 1 < len(self._flow_windows)):
            w = self._flow_windows[idx - 1]
            s["hwnd"] = w.hwnd
            s["title"] = w.title
        else:
            s["hwnd"] = 0
            s["title"] = ""

    def flow_apply(self):
        self._flow_flush()
        self._flow_refresh_list()
        self.log("步骤已保存", "ok")

    def toggle_flow(self):
        if self.macro_eng.running:
            self.macro_eng.stop()
            return
        self._flow_flush()
        if not self.flow_steps:
            messagebox.showinfo("流程为空", "请先添加至少一个步骤。", parent=self.root)
            return
        ok = self.macro_eng.start(self.flow_steps,
                                  repeat=self.flow_repeat_var.get(),
                                  on_log=lambda m: self.ui(self.log, m, "ok"))
        if ok:
            self.log("流程已启动：%d 个步骤 × %s 轮"
                     % (len(self.flow_steps), self.flow_repeat_var.get()), "ok")
        else:
            messagebox.showwarning("无法启动", "流程已经在运行中。", parent=self.root)

    def _on_macro(self, kind, *args):
        if kind == "step":
            idx, _s, desc = args
            self.ui(self._macro_step, idx, desc)
        elif kind == "finished":
            dur = args[0]
            self.ui(self._macro_done, dur)
        elif kind == "stopped":
            self.ui(self._macro_stop)
        elif kind == "warn":
            self.ui(self.log, args[0], "warn")
        elif kind == "error":
            self.ui(self.log, "流程出错：\n" + str(args[0]), "error")
            self.ui(self._macro_stop)

    def _macro_step(self, idx, desc):
        lb = self.flow_list
        lb.selection_clear(0, "end")
        if 0 <= idx < lb.size():
            lb.selection_set(idx)
            lb.see(idx)
        self.flow_pill.set("● 运行中 步骤 %d" % (idx + 1), THEME["green"])

    def _macro_done(self, dur):
        self.flow_pill.set("● 已完成", THEME["blue"])
        self.btn_flow_run.set_text("▶  运行流程")
        self.log("流程完成，用时 %.1f 秒" % dur, "ok")

    def _macro_stop(self):
        self.flow_pill.set("● 未运行", THEME["text3"])
        self.btn_flow_run.set_text("▶  运行流程")

    def _poll_flow_hotkey(self):
        vk = 0x76  # F7
        try:
            down = win_input.key_down(vk)
            if down and not self._flow_hk_down:
                self._flow_hk_down = True
                self.ui(self.toggle_flow)
            elif not down:
                self._flow_hk_down = False
        except Exception:  # noqa: BLE001
            pass
        self.root.after(30, self._poll_flow_hotkey)

    def _page_about(self):
        p = self.pages["about"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()
        box = self._card(p, "关于")
        tk.Label(box,
                 text="%s v%s · MIT 开源协议\n\n"
                      "原理：调用 Windows 官方 SendInput API 合成鼠标事件，"
                      "不注入任何进程、不读写游戏内存。\n"
                      "热键通过 GetAsyncKeyState 轮询实现，不注册全局热键，"
                      "退出即释放，不留后台残留。\n\n"
                      "⚠ 使用提示：请遵守所使用软件/游戏的规则，"
                      "自动化点击可能违反部分游戏的服务条款，"
                      "由此产生的封号等后果需自行承担。"
                      % (APP_NAME, APP_VERSION),
                 bg=THEME["card"], fg=THEME["text2"], font=F(10),
                 justify="left", wraplength=px(620)).pack(anchor="w")
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x", pady=(px(14), px(0)))
        AppleButton(row, "打开项目主页", command=lambda: open_uri(
            "https://github.com/xavier111222/AutoClicker"), style="secondary",
            width=px(150), height=px(34), font=F(10)).pack(side="left")
        AppleButton(row, "系统自检", command=self.selftest_gui, style="secondary",
                    width=px(120), height=px(34), font=F(10)).pack(side="left",
                                                                   padx=px(10))

    # -------------------------------------------------- 交互
    def _on_unit(self, i):
        self.unit_seg.select(i)

    def _on_pos_mode(self, i):
        self.pos_seg.select(i)
        self.mode_val.set("follow" if i == 0 else "fixed")

    def _on_hold(self):
        self.log("热键模式：%s" % ("按住热键连点" if self.hold_mode.get()
                                   else "按一次热键切换"))

    def _sync_hotkey(self):
        self.hk_pill.set(self.hotkey_val.get(), THEME["blue"])
        self.log("热键已设为 %s" % self.hotkey_val.get(), "ok")

    def pick_pos(self):
        x, y = cursor_pos()
        self.pos_x.set(str(x))
        self.pos_y.set(str(y))
        self.pos_seg.select(1)
        self.mode_val.set("fixed")
        self.pos_label.configure(text="已锁定 %d, %d" % (x, y))
        self.log("已记录固定坐标：%d, %d" % (x, y), "ok")

    def _config(self):
        def num(var, cast, default=0):
            try:
                return cast(str(var.get()).strip())
            except Exception:  # noqa: BLE001
                return default
        unit = {"毫秒": 0.001, "秒": 1.0, "分钟": 60.0}[self.interval_unit.get()]
        interval = max(0.001, num(self.interval_val, float, 0.1) * unit)
        count = max(0, num(self.count_val, int, 0))
        jitter = max(0, num(self.jitter_val, int, 0))
        mode = "fixed" if self.mode_val.get() == "fixed" else "follow"
        return dict(interval=interval, count=count, button=self.button_val.get(),
                    double=self.double_var.get(), mode=mode,
                    x=num(self.pos_x, int), y=num(self.pos_y, int), jitter=jitter)

    def toggle_start(self):
        if self.eng.running:
            self.stop()
        else:
            self.start()

    def start(self):
        cfg = self._config()
        if cfg["mode"] == "fixed":
            sw, sh = screen_size()
            if not (0 <= cfg["x"] < sw and 0 <= cfg["y"] < sh):
                messagebox.showerror("坐标无效", "固定坐标超出屏幕范围 (%dx%d)。"
                                     % (sw, sh))
                return
        cd = max(0, int(self.countdown_val.get() or 0))
        if not self.eng.start(cfg, cd):
            return
        self.btn_start.set_enabled(False)
        self.state_pill.set("● 准备中" if cd else "● 连点中", THEME["orange"])
        self.status_var.set("运行中")
        self.log("开始连点：间隔 %.3fs · %s%s%s · 位置%s" % (
            cfg["interval"], BUTTONS[cfg["button"]],
            " · 双击" if cfg["double"] else "",
            " · 次数 %d" % cfg["count"] if cfg["count"] else "",
            "固定(%d,%d)" % (cfg["x"], cfg["y"]) if cfg["mode"] == "fixed" else "跟随鼠标"),
            "ok")

    def stop(self):
        if not self.eng.running:
            return
        self.eng.stop()
        self.log("已停止，共点击 %d 次。" % self.eng.clicks, "warn")
        self._reset_ui()

    def _reset_ui(self):
        self.btn_start.set_enabled(True)
        self.state_pill.set("● 就绪", THEME["text3"])
        self.status_var.set("就绪")
        self.stat_cps.set("0.0")
        self._stat_shown = False

    def _on_engine(self, kind, *args):
        """引擎线程事件 → 主线程"""
        if kind == "countdown":
            self.ui(self.state_pill.set, "● %d 秒后开始" % args[0], THEME["orange"])
        elif kind == "started":
            self.ui(self.state_pill.set, "● 连点中", THEME["green"])
            self.ui(self._start_stat_window)
        elif kind == "tick":
            clicks, elapsed, cps = args
            self.ui(self.stat_clicks.set, str(clicks))
            self.ui(self.stat_cps.set, "%.1f" % cps)
            self.ui(self.stat_time.set, fmt_dur(elapsed))
        elif kind == "finished":
            self.ui(self.stat_clicks.set, str(args[0]))
            self.ui(self.log, "已完成 %d 次点击。" % args[0], "ok")
            self.ui(self._reset_ui)
        elif kind == "stopped":
            self.ui(self._reset_ui)
        elif kind == "error":
            self.ui(self.log, "点击线程出错: " + args[0], "error")
            self.ui(self._reset_ui)

    def _start_stat_window(self):
        if self._stat_shown:
            return
        self._stat_shown = True

    def on_ready(self):
        self.root.after(30, self._poll_hotkey)
        self.root.after(60, self._poll_flow_hotkey)
        self.log("%s v%s 已启动 · 屏幕 %dx%d"
                 % (APP_NAME, APP_VERSION, *screen_size()), "ok")

    def _poll_hotkey(self):
        """轮询全局热键（30ms）"""
        vk = HOTKEYS.get(self.hotkey_val.get(), 0x75)
        down = key_down(vk)
        try:
            if self.hold_mode.get():
                if down and not self.eng.running:
                    self.start()
                elif not down and self.eng.running:
                    self.stop()
            else:
                if down and not self._hotkey_down:
                    self.toggle_start()
                self._hotkey_down = down
        except Exception:  # noqa: BLE001
            self.log("热键处理异常: " + traceback.format_exc(limit=2), "error")
        self.root.after(30, self._poll_hotkey)

    def selftest_gui(self):
        info = selftest()
        messagebox.showinfo("系统自检", info)

    def on_close(self):
        self.eng.stop()
        self.root.destroy()

    def show_page(self, index, silent=False):
        super().show_page(index, silent=silent)
        if not silent and self.PAGES[index][0] == "main":
            self._refresh_hotkey_pill()

    def _refresh_hotkey_pill(self):
        self.hk_pill.set(self.hotkey_val.get(), THEME["blue"])


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec >= 3600:
        return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)
    return "%d:%02d" % (sec // 60, sec % 60)


# ============================================================ CLI

def selftest() -> str:
    sw, sh = screen_size()
    x, y = cursor_pos()
    lines = [
        "屏幕分辨率：%dx%d" % (sw, sh),
        "当前鼠标位置：%d, %d" % (x, y),
        "SendInput 可用：%s" % hasattr(ctypes.windll.user32, "SendInput"),
        "GetAsyncKeyState 可用：%s" % hasattr(ctypes.windll.user32, "GetAsyncKeyState"),
        "DPI 感知：%s / 系统 DPI %d" % (setup_dpi(), ui_scale_probe()),
    ]
    try:
        send_click(0)
        lines.append("测试点击：已发送（如果你看到鼠标左键点了一下，说明正常）")
    except Exception as e:  # noqa: BLE001
        lines.append("测试点击失败：%s" % e)

    # 跨应用能力自检：窗口枚举 + 后台投递 + 流程宏引擎
    try:
        import win_input
        wins = win_input.list_windows()
        lines.append("窗口枚举：%d 个可用窗口（跨应用点击/流程宏依赖此项）" % len(wins))
        for w in wins[:3]:
            lines.append("    - %s | %s" % (w.title[:40], w.exe))
        if wins:
            wi = wins[0]
            win_input.post_click(wi.hwnd, 10, 10)
            lines.append("后台点击投递：已发送到 0x%X（不抢焦点）" % wi.hwnd)
    except Exception as e:  # noqa: BLE001
        lines.append("窗口枚举失败：%s" % e)
    try:
        import macro
        eng = macro.MacroEngine()
        steps = macro.steps_from_json(macro.steps_to_json(macro.DEFAULT_STEPS))
        lines.append("流程宏引擎：%d 个默认步骤，序列化往返=%s"
                     % (len(macro.DEFAULT_STEPS), "OK" if steps else "空"))
        assert eng is not None
    except Exception as e:  # noqa: BLE001
        lines.append("流程宏引擎失败：%s" % e)
    return "\n".join(lines)


def ui_scale_probe() -> int:
    import ui_kit
    return ui_kit.get_system_dpi()


def cli_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog=APP_NAME, description="极速连点器")
    ap.add_argument("--version", action="store_true", help="显示版本")
    ap.add_argument("--selftest", action="store_true", help="系统自检")
    ap.add_argument("--pos", action="store_true", help="打印当前鼠标坐标")
    a = ap.parse_args(argv)
    if a.version:
        print("%s v%s" % (APP_NAME, APP_VERSION))
        return 0
    if a.pos:
        print("x=%d y=%d" % cursor_pos())
        return 0
    if a.selftest:
        _safe_print(selftest())
        return 0
    return None


def _safe_print(s):
    """打印可能被 GBK 控制台拒绝的文本。

    坑：窗口标题里可能含零宽字符（U+200B等），中文Windows 控制台
    默认编码 GBK，遇到这些字符 print 直接抛 UnicodeEncodeError，
    整个 --selftest 崩掉。降级成replace 后不可见字符变问号，不影响诊断。
    """
    try:
        print(s)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "gbk"
        print(s.encode(enc, "replace").decode(enc, "replace"))


def main():
    argv = sys.argv[1:]
    if argv and argv[0].startswith("-"):
        rc = cli_main(argv)
        if rc is not None:
            return rc
    setup_dpi()
    root = tk.Tk()
    import ui_kit
    ui_kit.init_ui_scale(root)      # 传 root 才能拿到该显示器真实 DPI
    ui_kit.apply_tk_scaling(root)
    ui_kit.apply_base_fonts(root)   # 命名默认字体也要缩放
    root.report_callback_exception = lambda exc, val, tb: (
        messagebox.showerror("程序错误", "".join(traceback.format_exception(exc, val, tb)))
        or traceback.print_exception(exc, val, tb))
    app = ClickerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
