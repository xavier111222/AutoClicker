# -*- coding: utf-8 -*-
"""
生成程序图标 assets/icon.ico —— macOS 风格：
蓝紫渐变圆角底 + 白色鼠标指针 + 点击涟漪圈，小尺寸自动简化细节。
（仅构建时需要 Pillow）
"""
import os

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "icon.ico")

TOP = (94, 132, 255)
BOT = (36, 24, 190)


def _vgrad(w, h, top, bot):
    col = Image.new("L", (1, h))
    px = col.load()
    for y in range(h):
        px[0, y] = int(y / max(1, h - 1) * 255)
    col = col.resize((w, h), Image.NEAREST)
    return Image.composite(Image.new("RGB", (w, h), bot),
                           Image.new("RGB", (w, h), top), col)


def _squircle_mask(size):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                       radius=size * 0.225, fill=255)
    return m


def _draw(size, ripple=True, pointer=True):
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 投影
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).rounded_rectangle(
        [S * 0.075, S * 0.105, S * 0.935, S * 0.965], radius=S * 0.225, fill=90)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    shadow.putalpha(sh)
    img = Image.alpha_composite(img, shadow)

    # 渐变主体 + 顶部高光
    mask = _squircle_mask(S)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(_vgrad(S, S, TOP, BOT).convert("RGBA"), (0, 0), mask)
    img = Image.alpha_composite(img, base)

    hg = Image.new("L", (1, S))
    hp = hg.load()
    for y in range(S):
        hp[0, y] = int(max(0.0, 1.0 - y / (S * 0.52)) * 52)
    hg = ImageChops.darker(hg.resize((S, S), Image.NEAREST), mask)
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    hl.putalpha(hg)
    img = Image.alpha_composite(img, hl)

    d = ImageDraw.Draw(img)
    cx, cy = S * 0.46, S * 0.47

    # 点击涟漪
    if ripple:
        for i, (r, a, wdt) in enumerate(((0.20, 90, 0.030),
                                         (0.30, 55, 0.026),
                                         (0.395, 30, 0.022))):
            rr = S * r
            d.ellipse([cx + S * 0.075 - rr, cy + S * 0.085 - rr,
                       cx + S * 0.075 + rr, cy + S * 0.085 + rr],
                      outline=(255, 255, 255, a), width=max(1, int(S * wdt)))

    # 鼠标指针（经典箭头）
    if pointer:
        px0, py0 = S * 0.335, S * 0.245
        pts = [(px0, py0), (px0, py0 + S * 0.44), (px0 + S * 0.105, py0 + S * 0.345),
               (px0 + S * 0.175, py0 + S * 0.325), (px0 + S * 0.255, py0 + S * 0.525),
               (px0 + S * 0.325, py0 + S * 0.485), (px0 + S * 0.245, py0 + S * 0.305),
               (px0 + S * 0.355, py0 + S * 0.285)]
        d.polygon(pts, fill=(255, 255, 255, 255))
        # 描边提升对比
        d.line(pts + [pts[0]], fill=(255, 255, 255, 255),
               width=max(1, int(S * 0.012)), joint="curve")
    return img


def build(size, detail):
    src = _draw(size * 4, ripple=detail >= 1, pointer=True)
    return src.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = (256, 128, 64, 48, 40, 32, 24, 20, 16)
    frames = []
    for s in sizes:
        detail = 2 if s >= 48 else (1 if s >= 24 else 0)
        img = build(s, detail)
        img.save(os.path.join(HERE, "_icon_%d.png" % s))
        frames.append(img)
    frames[0].save(OUT, format="ICO",
                   sizes=[(s, s) for s in sizes], append_images=frames[1:])
    print("icon ->", OUT)
