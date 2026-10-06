# -*- coding: utf-8 -*-
"""
macro.py —— 连点器流程宏引擎
================================================
把「连点」扩展成「流程」：一串带顺序的步骤（点击 / 移动 / 等待 / 输入文本 /
按键 / 循环），每步可以选择作用目标：

  * 跟随鼠标：前台点击，光标在哪点哪（游戏、网页等）
  * 固定坐标：前台点击屏幕绝对坐标
  * 指定窗口：把点击投递到某个应用窗口的**客户区坐标**，不抢焦点、
    不移动鼠标，目标窗口在后台也能收到（跨应用连点的关键）

步骤数据结构（可直接 json 序列化，便于存盘 / 分享）
    {"id": 1, "type": "click", "target": "window", "hwnd": 12345,
     "title": "记事本", "x": 100, "y": 200, "button": 0, "double": false,
     "jitter": 0, "text": "", "vk": 0, "ms": 500, "times": 1}

许可证: MIT
"""

from __future__ import annotations

import json
import threading
import time
import traceback

import win_input as wi

# ---------------------------------------------------------------- 步骤定义

STEP_TYPES = [
    ("click", "点击"),
    ("double", "双击"),
    ("move", "移动"),
    ("wait", "等待"),
    ("text", "输入文本"),
    ("key", "按键"),
    ("loop", "循环"),
]

TYPE_LABEL = dict(STEP_TYPES)
TARGET_LABEL = {
    "follow": "跟随鼠标",
    "fixed": "固定坐标",
    "window": "指定窗口",
}

BUTTON_LABEL = {0: "左键", 1: "右键", 2: "中键"}

# 常用虚拟键码（流程里「按键」步骤用）
VK_NAMES = {
    "Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B, "Space": 0x20,
    "Backspace": 0x08, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "Ctrl": 0x11, "Alt": 0x12, "Shift": 0x10, "Win": 0x5B,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
}


def new_step(step_id=1, stype="click"):
    """新建一个步骤的默认字典。"""
    return {
        "id": step_id,
        "type": stype,
        "target": "follow",
        "hwnd": 0,
        "title": "",
        "x": 0,
        "y": 0,
        "button": 0,
        "double": False,
        "jitter": 0,
        "text": "",
        "vk": 0,
        "vk_name": "",
        "ms": 500,
        "times": 1,
    }


def describe(step):
    """给 UI 用的一行摘要。"""
    t = step.get("type", "click")
    tgt = step.get("target", "follow")
    if t in ("click", "double", "move"):
        btn = BUTTON_LABEL.get(step.get("button", 0), "左键")
        verb = TYPE_LABEL.get(t, t)
        where = {
            "follow": "跟随鼠标",
            "fixed": "(%d, %d)" % (step.get("x", 0), step.get("y", 0)),
            "window": "%s(%d,%d)" % (step.get("title") or "窗口", step.get("x", 0), step.get("y", 0)),
        }.get(tgt, tgt)
        return "%s %s @%s" % (verb, btn, where)
    if t == "wait":
        return "等待 %d ms" % step.get("ms", 500)
    if t == "text":
        s = (step.get("text") or "")[:12]
        return "输入文本 “%s%s”" % (s, "…" if len(step.get("text") or "") > 12 else "")
    if t == "key":
        return "按键 %s" % (step.get("vk_name") or "VK_%d" % step.get("vk", 0))
    if t == "loop":
        return "循环 %d 次" % step.get("times", 1)
    return t


# ---------------------------------------------------------------- 宏引擎

class MacroEngine:
    """按顺序执行步骤列表。可后台线程跑，通过 on_event 回调汇报。"""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.state = "idle"       # idle / running / finished / error
        self.step_index = 0
        self.total = 0
        self.started = 0.0
        self.on_event = lambda *a: None

    @property
    def running(self):
        return self.state == "running"

    def start(self, steps, repeat=1, on_log=None):
        if self.running:
            return False
        if not steps:
            return False
        self.steps = [dict(s) for s in steps]
        self.repeat = max(1, int(repeat or 1))
        self.stop_flag.clear()
        self.step_index = 0
        self.state = "running"
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       args=(on_log or (lambda *a: None),))
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

    def _run(self, on_log):
        try:
            for rep in range(self.repeat):
                for i, step in enumerate(self.steps):
                    if self.stop_flag.is_set():
                        self.state = "idle"
                        self._emit("stopped", self.step_index, rep + 1)
                        return
                    self.step_index = i
                    self._emit("step", i, step, describe(step))
                    self._exec(step)
                    on_log("步骤 %d/%d：%s" % (i + 1, len(self.steps), describe(step)))
                on_log("第 %d/%d 轮完成" % (rep + 1, self.repeat))
            self.state = "finished"
            self._emit("finished", time.perf_counter() - self.started)
        except Exception:  # noqa: BLE001
            self.state = "error"
            self._emit("error", traceback.format_exc(limit=3))

    def _target_hwnd(self, step):
        hwnd = int(step.get("hwnd") or 0)
        if hwnd and not wi.is_window(hwnd):
            return 0
        return hwnd

    def _exec(self, step):
        t = step.get("type", "click")
        if t == "wait":
            self.stop_flag.wait(max(0, int(step.get("ms", 500)) / 1000.0))
            return
        if t == "key":
            vk = int(step.get("vk") or 0)
            if vk:
                wi.send_vk(vk)
            return
        if t == "text":
            txt = step.get("text") or ""
            if not txt:
                return
            hwnd = self._target_hwnd(step) if step.get("target") == "window" else 0
            if hwnd:
                wi.post_text(hwnd, txt)
            else:
                wi.send_text(txt)
            return
        if t == "move":
            self._point(step, click=False)
            return
        # click / double
        self._point(step, click=True)

    def _point(self, step, click):
        tgt = step.get("target", "follow")
        button = int(step.get("button", 0))
        dbl = bool(step.get("double")) or step.get("type") == "double"
        jitter = int(step.get("jitter") or 0)
        sx, sy = int(step.get("x", 0)), int(step.get("y", 0))

        if tgt == "window":
            hwnd = self._target_hwnd(step)
            if not hwnd:
                self._emit("warn", "步骤 %d 的目标窗口已关闭，跳过" % step.get("id", "?"))
                return
            if jitter:
                # 确定性偏移（不用 hash：每进程随机会导致同一流程表现不一致）
                import random
                rnd = random.Random(int(step.get("id", 0)) * 7919)
                sx += rnd.randint(-jitter, jitter)
                sy += rnd.randint(-jitter, jitter)
            if click:
                wi.post_click(hwnd, sx, sy, button, dbl)
            else:
                # 后台窗口无法真正移动光标，这里只做客户区->屏幕的换算并
                # 把光标移过去（部分程序跟随光标），失败也不影响流程
                wx, wy = wi.client_to_screen(hwnd, sx, sy)
                try:
                    wi.move_cursor(wx, wy)
                except Exception:  # noqa: BLE001
                    pass
            return

        # 跟随鼠标 / 固定坐标 → 前台点击
        if tgt == "fixed":
            x, y = sx, sy
        else:
            x, y = wi.cursor_pos()
        if jitter:
            import random
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)
        wi.move_cursor(x, y)
        if not click:
            return
        time.sleep(0.002)
        wi.send_click(button, dbl)


# ---------------------------------------------------------------- 存盘

def steps_to_json(steps):
    return json.dumps(steps, ensure_ascii=False, indent=2)


def steps_from_json(text):
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [dict(s) for s in data]
    except Exception:  # noqa: BLE001
        pass
    return []


DEFAULT_STEPS = [
    new_step(1, "wait"),
    new_step(2, "click"),
    new_step(3, "wait"),
    new_step(4, "click"),
]
DEFAULT_STEPS[0]["ms"] = 1000
DEFAULT_STEPS[2]["ms"] = 1000
