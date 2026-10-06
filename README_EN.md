# Auto Clicker (AutoClicker)

[简体中文](README.md) | [English](README_EN.md)

A **single-file** Windows utility that automates mouse clicks.
Set an interval and a hotkey starts clicking; you can also script **multi-step macros**
and click **any application window** — including windows running in the background.

- Single-file exe, no installation, double-click to run
- License: **MIT**
- Uses only official APIs (`SendInput` / `PostMessage` / `GetAsyncKeyState`); no process
  injection, no game-memory reading or writing
- Fully supports high DPI (150% / 200% scaling)

| Click control | Parameters | Macro |
| --- | --- | --- |
| ![Main](docs/screenshot-main.png) | ![Params](docs/screenshot-params.png) | ![Flow](docs/screenshot-flow.png) |

---

## Related tools

| Project | What it does |
| --- | --- |
| [GPU Switcher](https://github.com/xavier111222/GpuSwitcher) | One-click dGPU / integrated switching + per-app GPU assignment |
| [Screen Recorder](https://github.com/xavier111222/ScreenRecorder) | Four capture modes: full screen / picked region / app window / auto-detected |

All three share the same high-DPI UI skeleton (`ui_kit.py`), so they look and behave consistently.

---

## Quick start

1. Double-click **`极速连点器.exe`**
2. Move the mouse to the target position
3. Press **F6** (configurable) to start clicking; press again to stop. The "start countdown"
   gives you time to get your hand in place

Download: <https://github.com/xavier111222/AutoClicker/releases/latest>

---

## Features

| Feature | Description |
| --- | --- |
| Click interval | Milliseconds / seconds / minutes, scheduled by accumulating on `perf_counter` so long runs don't drift |
| Button | Left / right / middle, optionally double-click |
| Click count | Stop after N clicks, or `0` = keep going until you stop it |
| Position mode | Follow the current mouse position, or lock to fixed coordinates (one click records the current position) |
| Random jitter | ±N px around the target point, so coordinates are never perfectly fixed |
| Global hotkey | Any F1–F12, in **toggle** mode (press once to start/stop) or **hold** mode (click while held) |
| Countdown | 0–10 seconds, then clicking starts automatically |
| Live stats | Clicks so far, current rate (clicks/sec), elapsed time |

### Macro scripting: turn "clicking" into a *flow*

Beyond "keep clicking the same spot", you can script a sequence of steps, and **each step
targets its own destination**:

| Step type | Description |
| --- | --- |
| Click / Double-click / Move | Mouse actions, with left/right/middle button |
| Wait | Pause for N milliseconds, giving the UI time to react |
| Type text | Send a string to the target |
| Keypress | Enter / Ctrl / F5 and other common keys |
| Loop | Repeat the whole flow N times (set "repeat count" at the top) |

**Three target modes** — this is what makes cross-app work possible:

| Target | Mechanism | Best for |
| --- | --- | --- |
| Follow mouse | `SendInput` synthesizes real input | Games, web pages and other **custom-drawn UIs** |
| Fixed coordinates | `SendInput` + `SetCursorPos` | A fixed spot on screen |
| **Target window** | `PostMessage` posts window messages | **Any native window, works in the background** |

> **"Target window" is the core of cross-app clicking**: clicks are delivered straight to the
> target window's client area as window messages — **the mouse never moves and focus is never
> stolen**, so the window can stay behind others or even be minimized and still respond.
> Verified working against Notepad, File Explorer, and other native Win32 apps.
> Note that custom-drawn UIs (browser pages, Unity, Electron) ignore window messages; use
> "Follow mouse" or "Fixed coordinates" for those.

How to use: open the "Macro" tab → "Refresh windows" to pick a target → add and edit steps →
"Pick point" records the current mouse coordinates with one click → press **F7** or click
"Run flow" to start.

### Choosing parameters

| Scenario | Recommendation |
| --- | --- |
| Regular clicking | 100–200 ms, stable and drop-free |
| Extreme clicking | 30–50 ms; below 10 ms the system event queue drops events and you don't actually get faster |
| Long-running | 1 second or more plus a click count, to avoid risk-control systems |
| Fixed position | Use "Fixed coordinates" + pick position, then the mouse can move elsewhere |

---

## Command line

```bat
极速连点器.exe --version     :: show version
极速连点器.exe --selftest    :: self-test (resolution / SendInput / DPI awareness)
极速连点器.exe --pos         :: print current mouse coordinates
```

> Frozen exes are much more prone to "works from source, breaks when packaged" than you'd
> think, so always verify the exe itself with `--selftest`.

---

## How does it work?

1. **Clicking**: builds an `INPUT/MOUSEINPUT` struct and calls `SendInput` to synthesize
   `MOUSEEVENTF_*DOWN/UP` — the same system path a real mouse takes, so every app accepts it.
2. **Hotkey**: polls `GetAsyncKeyState` every 30 ms (instead of registering a global hotkey),
   so it **writes no registry, occupies no hotkey, releases on exit**, and needs no admin rights.
3. **Background clicking**: `PostMessage` sends `WM_LBUTTONDOWN/UP` directly to the target
   window, packing client-area coordinates into `lParam`. Choosing this means the mouse is
   never moved and focus is never stolen.
4. **Timing**: instead of accumulating `sleep(interval)`, it does
   `next += interval; wait(next - now)`, so error never accumulates into "slower and slower".
   If it falls more than 1 second behind (e.g. after sleep), it re-synchronizes automatically.
5. **UI**: the main thread only draws; all clicking happens on a worker thread. Thread
   messages go back through a queue to the main thread rather than calling `root.after`
   directly (Tk is not thread-safe and that crashes intermittently).

---

## ⚠ Notes

1. **Respect the terms of service of the software/games you use.** Automated clicking may
   violate some games' rules; bans and similar consequences are your own responsibility.
2. To stop: press the hotkey again, or click "Stop". Closing the window also stops it
   (it cleans up before exiting).
3. Very short intervals (<10 ms) aren't faster, they just drop events.
4. If SmartScreen blocks the first run, click "More info → Run anyway" (common for unsigned
   open-source builds).
5. Full high-DPI support: the app declares Per-Monitor V2 awareness; fonts and layouts scale
   with the real DPI.

---

## Run from source and build

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

python auto_clicker.py            :: run from source
.venv\Scripts\python build.py     :: generate icon → PyInstaller → copy to desktop
```

### Project layout

```
AutoClicker/
├── auto_clicker.py     # main program: click engine + GUI + CLI
├── macro.py            # macro engine: step model + sequential execution
├── win_input.py        # Win32 input layer: window enumeration / foreground SendInput / background PostMessage
├── ui_kit.py           # Apple-style UI skeleton (high-DPI-safe component library)
├── make_icon.py        # generates assets/icon.ico
├── build.py            # one-click build and copy to desktop
├── assets/
│   ├── icon.ico
│   └── app.manifest    # Per-Monitor V2 DPI awareness declaration
├── docs/               # README screenshots
├── requirements.txt
├── LICENSE
└── README.md
```

---

## FAQ

**Q: The hotkey does nothing?**
A: ① The hotkey still works when another app has focus; if it doesn't, something else (such
as a game's hotkey handler) has grabbed it — try a different F key. ② "Hold" mode requires
you to keep the key down. ③ Programs running as administrator can't be simulated by a
normal-privilege process; use "Get administrator rights" to elevate and try again.

**Q: Click speed won't go up?**
A: Below 10 ms the system drops events; 30 ms+ is recommended. The target app also has its
own processing ceiling.

**Q: Why does background clicking fail on some apps?**
A: Window messages only work for controls that respond to clicks via Win32 messages
(Notepad, File Explorer, Office, some older programs). Custom-drawn UIs — browser pages,
Unity/Electron games — ignore `WM_LBUTTONDOWN`; you must use "Follow mouse" or
"Fixed coordinates" (foreground clicking) instead.

**Q: Does switching to another program interrupt background clicking?**
A: No — that's the whole point of "Target window". You can let the app click away in the
background while you use the mouse for other things. One caveat: if the target app pops up a
modal dialog that covers a control, clicks will land on the dialog.

**Q: Does it record my mouse activity?**
A: No. The app does not read screen contents, upload anything, or write any file except its
log, and the log is only shown at the bottom of the window.

---

## License

MIT — see [LICENSE](LICENSE). Use at your own risk; the author is not responsible for any
consequences of using this tool.
