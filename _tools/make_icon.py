"""生成应用图标 app/assets/icon.ico。

设计：深蓝圆角底 + 三层错位叠放的浏览器窗口 = 「一号一环境、彼此隔离」。
最前窗口青绿→蓝渐变并带标签栏三点，呼应界面里的状态圆点。
4 倍超采样绘制后 LANCZOS 缩小，保证 16/32px 小尺寸不糊。

用法： runtime/python.exe _tools/make_icon.py
产物： app/assets/icon.ico（exe 图标）、app/assets/icon.png（托盘图标）、
      _tools/icon_new_preview.png、_tools/icon_sizes.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "assets" / "icon.ico"
PREVIEW = ROOT / "_tools" / "icon_new_preview.png"
SIZES_PNG = ROOT / "_tools" / "icon_sizes.png"

S = 1024  # 4x 超采样
R = 224   # 背景圆角


def mix(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def rounded_gradient(size: int, radius: int,
                     top: tuple[int, int, int],
                     bottom: tuple[int, int, int]) -> Image.Image:
    """竖向渐变 + 圆角遮罩。"""
    g = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(g)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=mix(top, bottom, y / (size - 1)) + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(g, (0, 0), mask)
    return out


def main() -> None:
    # ---- 背景 ----
    img = rounded_gradient(S, R, (26, 40, 64), (10, 17, 30))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, S - 7, S - 7), radius=R - 6,
                        outline=(255, 255, 255, 30), width=8)

    # ---- 三层窗口（从后往前，向左下展开）----
    def win_box(dx: int, dy: int) -> tuple[int, int, int, int]:
        return (184 + dx, 320 + dy, 744 + dx, 760 + dy)

    # 投影，让最前窗口浮起来
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(win_box(0, 0), radius=44, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    img.alpha_composite(shadow, (0, 14))

    layers = [
        (win_box(108, -124), (42, 72, 112, 235), (120, 165, 220, 95)),
        (win_box(54, -62), (34, 98, 142, 240), (110, 200, 225, 120)),
    ]
    for box, fill, outline in layers:
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(box, radius=44, fill=fill, outline=outline, width=6)
        # 露出的一角加一道标签栏暗示
        d.line([(box[0] + 22, box[1] + 82), (box[2] - 22, box[1] + 82)],
               fill=outline, width=7)

    # ---- 最前窗口：青绿→蓝渐变 ----
    front = win_box(0, 0)
    fw, fh = front[2] - front[0], front[3] - front[1]
    fg = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)
    for y in range(fh):
        fd.line([(0, y), (fw, y)], fill=mix((45, 214, 162), (59, 130, 246), y / fh) + (255,))
    fmask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(fmask).rounded_rectangle((0, 0, fw - 1, fh - 1), radius=44, fill=255)
    fg.putalpha(fmask)
    # 标签栏 + 分隔线 + 三个点
    fd = ImageDraw.Draw(fg)
    fd.rectangle((0, 0, fw, 92), fill=(255, 255, 255, 34))
    fd.line([(0, 92), (fw, 92)], fill=(255, 255, 255, 105), width=6)
    for i in range(3):
        cx = 44 + i * 44
        fd.ellipse((cx - 13, 46 - 13, cx + 13, 46 + 13), fill=(255, 255, 255, 220))
    # 窗口内容区放两道极简「内容条」，小尺寸下仍像浏览器
    fd.rounded_rectangle((42, 140, fw - 42, 176), radius=18, fill=(255, 255, 255, 55))
    fd.rounded_rectangle((42, 204, fw - 150, 226), radius=11, fill=(255, 255, 255, 40))
    fd.rounded_rectangle((42, 246, fw - 230, 268), radius=11, fill=(255, 255, 255, 32))
    img.alpha_composite(fg, (front[0], front[1]))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(front, radius=44, outline=(255, 255, 255, 70), width=6)

    # ---- 导出 ----
    big = img.resize((256, 256), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    big.save(OUT, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(PREVIEW)
    # 托盘图标走 common.ASSETS / icon.png，必须和 exe 图标同一张，否则两个样子
    big.save(ROOT / "app" / "assets" / "icon.png")

    # 小尺寸拼图，方便肉眼检查 16/32/64 是否还认得出
    strip = Image.new("RGBA", (16 + 32 + 64 + 128 + 3 * 24 + 40, 168), (245, 246, 248, 255))
    x = 20
    for s in (16, 32, 64, 128):
        small = img.resize((s, s), Image.LANCZOS)
        strip.alpha_composite(small, (x, (168 - s) // 2))
        x += s + 24
    strip.convert("RGB").save(SIZES_PNG)

    print("已生成", OUT, OUT.stat().st_size, "B")
    print("预览", PREVIEW, "/", SIZES_PNG)


if __name__ == "__main__":
    main()
