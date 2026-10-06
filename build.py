# -*- coding: utf-8 -*-
"""
构建单文件 exe：
    python build.py

依赖：.venv 中已安装 pyinstaller / pillow
产物：dist/<NAME>.exe  →  自动复制到桌面
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY_NAME = "auto_clicker.py"
ICON = os.path.join(HERE, "assets", "icon.ico")
MANIFEST = os.path.join(HERE, "assets", "app.manifest")
NAME = "极速连点器"
DIST = os.path.join(HERE, "dist")


def desktop_dir() -> str:
    p = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(p):
        return p
    p = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
    return p if os.path.isdir(p) else os.path.expanduser("~")


def main() -> int:
    if sys.platform != "win32":
        print("本工具仅支持 Windows。")
        return 1

    if not os.path.isfile(ICON):
        print("[1/3] 生成图标 ...")
        subprocess.run([sys.executable, os.path.join(HERE, "make_icon.py")], check=False)
    else:
        print("[1/3] 图标已存在，跳过。")

    print("[2/3] 正在打包（约 1-3 分钟）...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
        "--hidden-import", "ui_kit",
        "--hidden-import", "win_input",
        "--hidden-import", "macro",
    ]
    if os.path.isfile(ICON):
        cmd += ["--icon", ICON]
    if os.path.isfile(MANIFEST):
        # 声明 Per-Monitor V2 DPI 感知：高分屏不模糊、布局按 DPI 缩放
        cmd += ["--manifest", MANIFEST]
    cmd += [os.path.join(HERE, ENTRY_NAME)]

    if subprocess.run(cmd, cwd=HERE).returncode != 0:
        print("打包失败。")
        return 1

    exe = os.path.join(DIST, NAME + ".exe")
    if not os.path.isfile(exe):
        print("未找到产物:", exe)
        return 2

    print("[3/3] 复制到桌面 ...")
    target = os.path.join(desktop_dir(), NAME + ".exe")
    try:
        shutil.copy2(exe, target)
        print("完成:", target, "(%.1f MB)" % (os.path.getsize(target) / 1048576))
    except Exception as e:  # noqa: BLE001
        print("复制失败:", e, "| 产物仍在:", exe)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
