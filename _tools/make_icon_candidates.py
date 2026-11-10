"""生成 5 个候选应用图标供挑选（全部为明亮配色）。

软件特性：多账号浏览器环境隔离 / 指纹伪装 / 代理出口 / 防关联。
5 个方向刻意拉开差异，且都针对「上一版太暗」做了提亮：
  1 晴空指纹   —— 浅冰蓝底 + 青蓝渐变写实指纹弧
  2 三环境窗口 —— 白底 + 三个并排的彩色独立浏览器窗口
  3 守护盾纹   —— 亮紫蓝渐变底 + 白色盾牌内嵌指纹
  4 极简浏览器 —— 亮青绿渐变底 + 白色窗口 + 指纹弧
  5 多色身份   —— 白底 + 五种亮色指纹脊线（一号一身份）

产物：_tools/icons/candidate_N.png / .ico，以及对比图 _tools/icon_candidates.png
用法： runtime/python.exe _tools/make_icon_candidates.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "_tools" / "icons"
PREVIEW = ROOT / "_tools" / "icon_candidates.png"

S = 1024
R = 224
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

NAMES = [
    ("晴空指纹", "浅冰蓝底 · 青蓝指纹弧"),
    ("三环境窗口", "白底 · 三个彩色独立环境"),
    ("守护盾纹", "亮紫蓝渐变 · 盾+指纹"),
    ("极简浏览器", "亮青绿底 · 白色窗口"),
    ("多色身份", "白底 · 一号一身份"),
]


def mix(a, b, t):
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def canvas(top, bottom, radius=R):
    g = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(g)
    for y in range(S):
        d.line([(0, y), (S, y)], fill=mix(top, bottom, y / (S - 1)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(g, (0, 0), mask)
    return out


def border(img, rgba=(255, 255, 255, 90), width=8):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, S - 7, S - 7), radius=R - 6, outline=rgba, width=width)
    return img


def fingerprint(d, cx, cy, radii, colors, width=48, start=180, end=360, gap=8):
    """同心 U 形指纹脊线。缺口必须很小，否则左右两段会散成「翅膀」而不是指纹。"""
    for i, r in enumerate(radii):
        col = colors[i]
        bbox = (cx - r, cy - r, cx + r, cy + r)
        g = gap + i * 2
        for a, b in ((start, 270 - g), (270 + g, end)):
            d.arc(bbox, a, b, fill=col, width=width)
            for ang in (a, b):
                x = cx + r * math.cos(math.radians(ang))
                y = cy + r * math.sin(math.radians(ang))
                d.ellipse((x - width / 2, y - width / 2,
                           x + width / 2, y + width / 2), fill=col)


def dot(d, cx, cy, r, fill, outline=None, ow=8):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=outline, width=ow)


def mini_window(d, box, fill, bar=(255, 255, 255, 70), dots=(255, 255, 255, 230),
                bars=(255, 255, 255, 120), radius=40):
    x1, y1, x2, y2 = box
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=(255, 255, 255, 110), width=7)
    d.rounded_rectangle((x1, y1, x2, y1 + 96), radius=radius, fill=bar)
    d.line([(x1, y1 + 96), (x2, y1 + 96)], fill=(255, 255, 255, 130), width=6)
    for i in range(3):
        dot(d, x1 + 52 + i * 46, y1 + 48, 13, dots)
    w = x2 - x1
    for yy, ww in ((y1 + 150, w - 90), (y1 + 208, w - 170), (y1 + 266, w - 250)):
        if yy < y2 - 30:
            d.rounded_rectangle((x1 + 45, yy, x1 + 45 + ww, yy + 30), radius=15, fill=bars)


# ---------------------------------------------------------------- 5 个方案

def c1():
    img = canvas((232, 247, 255), (196, 229, 250))
    d = ImageDraw.Draw(img)
    cols = [mix((13, 148, 116), (30, 84, 200), i / 4) + (255,) for i in range(5)]
    fingerprint(d, 512, 650, [130, 202, 274, 346, 418], cols, width=50, gap=7)
    # 指纹核心：一小段下方弧，避免变成空心靶心
    d.arc((452, 590, 572, 710), 20, 160, fill=cols[0], width=38)
    dot(d, 838, 190, 46, (22, 163, 74, 255), (255, 255, 255, 255), 12)
    return border(img, (255, 255, 255, 190))


def c2():
    img = canvas((255, 255, 255), (236, 242, 250))
    d = ImageDraw.Draw(img)
    fills = [(52, 211, 153, 255), (59, 130, 246, 255), (167, 139, 250, 255)]
    for i, x in enumerate((92, 382, 672)):
        box = (x, 286, x + 260, 738)
        mini_window(d, box, fills[i], bar=(255, 255, 255, 90),
                    dots=(255, 255, 255, 245), bars=(255, 255, 255, 150), radius=38)
        dot(d, x + 130, 800, 22, fills[i])
    return border(img, (206, 218, 235, 255))


def c3():
    img = canvas((146, 112, 246), (32, 172, 240))
    # 白色盾牌（圆角上身 + 收尖下摆，用 mask 合成保证边缘连贯）
    m = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(m)
    md.rounded_rectangle((212, 176, 812, 672), radius=96, fill=255)
    md.polygon([(212, 540), (812, 540), (512, 892)], fill=255)
    white = Image.new("RGBA", (S, S), (255, 255, 255, 246))
    img.paste(white, (0, 0), m)
    d = ImageDraw.Draw(img)
    purple = mix((110, 74, 230), (30, 84, 200), .35) + (255,)
    fingerprint(d, 512, 660, [110, 178, 246, 314], [purple] * 4, width=44, gap=7)
    return border(img, (255, 255, 255, 120))


def c4():
    img = canvas((28, 214, 168), (12, 160, 232))
    d = ImageDraw.Draw(img)
    box = (190, 268, 834, 756)
    d.rounded_rectangle(box, radius=58, fill=(255, 255, 255, 255))
    d.rounded_rectangle((190, 268, 834, 372), radius=58, fill=(232, 245, 252, 255))
    d.rectangle((190, 340, 834, 372), fill=(232, 245, 252, 255))
    d.line([(190, 372), (834, 372)], fill=(190, 216, 234, 255), width=6)
    for i in range(3):
        dot(d, 262 + i * 52, 320, 14, (28, 160, 200, 255))
    # 窗口内的指纹弧
    fingerprint(d, 512, 760, [100, 168, 236, 304],
                [mix((13, 148, 140), (30, 84, 200), i / 3) + (255,) for i in range(4)],
                width=38, gap=6)
    return border(img, (255, 255, 255, 110))


def c5():
    img = canvas((255, 255, 255), (234, 241, 250))
    d = ImageDraw.Draw(img)
    cols = [(13, 148, 116, 255), (37, 99, 235, 255), (124, 58, 237, 255),
            (219, 39, 119, 255), (217, 119, 6, 255)]
    fingerprint(d, 512, 650, [130, 202, 274, 346, 418], cols, width=50, gap=7)
    dot(d, 512, 650, 32, (30, 41, 59, 255))
    dot(d, 838, 190, 46, (22, 163, 74, 255), (255, 255, 255, 255), 12)
    return border(img, (206, 218, 235, 255))


CANDIDATES = [c1, c2, c3, c4, c5]


def font(size: int):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
              r"C:\Windows\Fonts\segoeui.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def preview(bigs: list[Image.Image]) -> None:
    W, H = 5 * 236 + 40, 430
    sheet = Image.new("RGB", (W, H), (243, 245, 249))
    d = ImageDraw.Draw(sheet)
    f_num, f_name, f_desc, f_small = font(30), font(23), font(16), font(15)
    for i, big in enumerate(bigs):
        x = 20 + i * 236
        d.rounded_rectangle((x, 18, x + 216, H - 18), radius=18, fill=(255, 255, 255))
        d.text((x + 16, 28), f"0{i+1}", font=f_num, fill=(37, 99, 235))
        # 必须带 alpha 合成：直接 convert("RGB") 会把圆角外的透明区变成黑三角
        rgba = big.resize((180, 180), Image.LANCZOS)
        sheet.paste(rgba, (x + 18, 66), rgba)
        name, desc = NAMES[i]
        d.text((x + 16, 258), name, font=f_name, fill=(27, 29, 33))
        d.text((x + 16, 292), desc, font=f_desc, fill=(107, 114, 128))
        small = big.resize((32, 32), Image.LANCZOS)
        sheet.paste(small, (x + 16, 340), small)
        d.text((x + 58, 346), "32px 任务栏效果", font=f_small, fill=(107, 114, 128))
    sheet.save(PREVIEW)
    print("对比图", PREVIEW)


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    bigs = []
    for i, fn in enumerate(CANDIDATES, 1):
        big = fn().resize((256, 256), Image.LANCZOS)
        big.save(OUTDIR / f"candidate_{i}.png")
        big.save(OUTDIR / f"candidate_{i}.ico", format="ICO", sizes=SIZES)
        bigs.append(big)
        print(f"候选 {i}: {NAMES[i-1][0]} -> candidate_{i}.png / .ico")
    preview(bigs)


if __name__ == "__main__":
    main()
