"""生成应用图标（占位用，也可当作最终图标）。

企业微信要求：750×750、1MB 以内的 jpg/png。
这里画一个深色底 + 绿色脉冲线的心电图样式图标，寓意"监控/告警"。

    python tools/make_logo.py --out assets/ops-alert-logo.png

想换成自己的图（比如某张头像）不用改这个脚本，直接用 --from 走裁剪路径：

    python tools/make_logo.py --from "C:/path/to/avatar.jpg" --out assets/ops-alert-logo.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 750
TOP = (11, 18, 32)        # 深海军蓝
BOTTOM = (22, 35, 58)
ACCENT = (34, 197, 94)    # 绿色脉冲
ACCENT_DIM = (21, 128, 61)


def background() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), TOP)
    draw = ImageDraw.Draw(img)
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        draw.line(
            [(0, y), (SIZE, y)],
            fill=(
                round(TOP[0] + (BOTTOM[0] - TOP[0]) * ratio),
                round(TOP[1] + (BOTTOM[1] - TOP[1]) * ratio),
                round(TOP[2] + (BOTTOM[2] - TOP[2]) * ratio),
            ),
        )

    # 中间一团柔和的高光，让脉冲线有"发光"的落点
    glow = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [SIZE * 0.18, SIZE * 0.34, SIZE * 0.82, SIZE * 0.66], fill=(16, 60, 40)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(SIZE * 0.12))
    return Image.blend(img, Image.blend(img, glow, 0.55), 0.9)


def pulse_points() -> list[tuple[float, float]]:
    mid = SIZE / 2
    return [
        (SIZE * 0.10, mid),
        (SIZE * 0.30, mid),
        (SIZE * 0.38, mid - SIZE * 0.16),
        (SIZE * 0.46, mid + SIZE * 0.20),
        (SIZE * 0.55, mid - SIZE * 0.28),
        (SIZE * 0.63, mid + SIZE * 0.06),
        (SIZE * 0.72, mid),
        (SIZE * 0.90, mid),
    ]


def draw_pulse(base: Image.Image) -> Image.Image:
    points = pulse_points()

    glow_layer = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    ImageDraw.Draw(glow_layer).line(
        points, fill=ACCENT_DIM, width=int(SIZE * 0.05), joint="curve"
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(SIZE * 0.022))
    canvas = Image.blend(base, Image.blend(base, glow_layer, 0.8), 0.85)

    draw = ImageDraw.Draw(canvas)
    draw.line(points, fill=ACCENT, width=int(SIZE * 0.024), joint="curve")
    # 端点圆头，线条看起来不那么"断"
    radius = SIZE * 0.016
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=ACCENT)
    return canvas


def make_generated() -> Image.Image:
    return draw_pulse(background())


def _edge_is_blank(img: Image.Image, index: int, axis: str, threshold: float, spread: float) -> bool:
    """判断某一行/列是不是"空白边"：既要暗，还要**均匀**。

    只看"暗"会把夜空、深色头发这类真实内容也裁掉；
    加上"均匀"这个条件，纯黑边框才会被识别出来。
    """
    width, height = img.size
    if axis == "y":
        pixels = [img.getpixel((x, index)) for x in range(width)]
    else:
        pixels = [img.getpixel((index, y)) for y in range(height)]
    values = [sum(pixel[:3]) / 3 for pixel in pixels]
    mean = sum(values) / len(values)
    return mean < threshold and (max(values) - min(values)) < spread


def trim_borders(
    img: Image.Image, *, threshold: float = 40.0, spread: float = 25.0, max_ratio: float = 0.12
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """裁掉四边均匀的纯黑（或纯色）边框。

    很多图（截图、带边框的下载图、二次压缩过的头像）四周会留一圈黑边，
    直接当图标会显得脏。这里最多裁掉各边 12%，避免把内容也吃掉。
    """
    width, height = img.size
    limit_y = int(height * max_ratio)
    limit_x = int(width * max_ratio)
    top = 0
    while top < limit_y and _edge_is_blank(img, top, "y", threshold, spread):
        top += 1
    bottom = 0
    while bottom < limit_y and _edge_is_blank(img, height - 1 - bottom, "y", threshold, spread):
        bottom += 1
    left = 0
    while left < limit_x and _edge_is_blank(img, left, "x", threshold, spread):
        left += 1
    right = 0
    while right < limit_x and _edge_is_blank(img, width - 1 - right, "x", threshold, spread):
        right += 1
    if top or bottom or left or right:
        img = img.crop((left, top, width - right, height - bottom))
    return img, (left, top, right, bottom)


def make_from_source(
    source: Path, *, zoom: float = 1.0, offset_x: float = 0.0, offset_y: float = 0.0,
    trim: bool = True,
) -> tuple[Image.Image, dict]:
    """把一张任意图片裁成正方形并缩放到 750×750。

    放大时会补一次轻微锐化：源图比 750 小时，LANCZOS 放大会偏软，
    而图标最终显示得很小，软了就看不清主体。
    """
    img = Image.open(source).convert("RGB")
    info: dict = {"source_size": img.size}
    if trim:
        img, trimmed = trim_borders(img)
        info["trimmed"] = trimmed
    width, height = img.size
    info["working_size"] = img.size

    side = min(width, height) / max(zoom, 0.05)
    left = (width - side) / 2 + offset_x * side
    top = (height - side) / 2 + offset_y * side
    left = max(0.0, min(left, width - side))
    top = max(0.0, min(top, height - side))
    img = img.crop((round(left), round(top), round(left + side), round(top + side)))

    upscaling = side < SIZE
    img = img.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    if upscaling:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=3))
    info["upscaled"] = upscaling
    return img, info


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/处理企业微信应用图标（750×750）")
    parser.add_argument("--out", default="assets/ops-alert-logo.png")
    parser.add_argument("--from", dest="source", default=None, help="用自己的图片作为素材（居中裁剪）")
    parser.add_argument("--zoom", type=float, default=1.0,
                        help=">1 放大裁切范围（等于放大主体），<1 收紧")
    parser.add_argument("--offset-x", type=float, default=0.0,
                        help="横向偏移，-0.3~0.3；往右为正")
    parser.add_argument("--offset-y", type=float, default=0.0, help="纵向偏移，-0.3~0.3 之间调整取景")
    parser.add_argument("--no-trim", action="store_true", help="不要自动裁掉四周的纯黑边框")
    args = parser.parse_args()

    info: dict = {}
    if args.source:
        path = Path(args.source)
        if not path.is_file():
            raise SystemExit(f"找不到素材文件：{path}")
        image, info = make_from_source(
            path, zoom=args.zoom, offset_x=args.offset_x, offset_y=args.offset_y,
            trim=not args.no_trim,
        )
    else:
        image = make_generated()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    size_kb = out.stat().st_size / 1024
    print(f"已生成 {out.resolve()}  {image.size[0]}×{image.size[1]}  {size_kb:.0f} KB")
    if info:
        if info.get("source_size") != info.get("working_size"):
            print(f"  原图 {info['source_size']} → 去掉边框后 {info['working_size']}"
                  f"（各边裁掉 {info.get('trimmed')}：左/上/右/下）")
        else:
            print(f"  原图 {info['source_size']}（四周没有可裁的纯色边框）")
        if info.get("upscaled"):
            print("  源图小于 750，已放大并补了轻微锐化")
    if size_kb > 1024:
        print("⚠️ 超过 1MB，企业微信可能不接受；试试 --from 换分辨率更低的素材")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
