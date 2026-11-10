"""把生成好的图标原图变成可用的 .ico。

原图是模型出的 RGB 方图（四周留白、圆角是画出来的），直接用会在深色任务栏上
露出白角。所以这里做三件事：

  1. 找出图形真正的边界（非白像素的包围盒），裁掉四周留白；
  2. 按 iOS 圆角比例（22%）重新生成一张带抗锯齿的 alpha 蒙版，
     不依赖原图圆角画得准不准；
  3. 输出多尺寸 ico（Windows 会按 DPI 挑），外加 512 的 png 给文档用。

原图在 packaging/icon_art.png；没有原图时退回几何图形（深绿圆角块 + 米白圆点），
保证打包流程不会因为缺素材而断。
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ART = HERE / "icon_art.png"

GREEN = (27, 58, 47, 255)
CREAM = (244, 241, 234, 255)
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
RADIUS_RATIO = 0.22


def _fallback(size: int) -> Image.Image:
    """没有原图时的兜底图案（以前那版：深绿圆角块 + 米白圆点）。"""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(s * 0.03)
    d.rounded_rectangle([pad, pad, s - pad, s - pad],
                        radius=int(s * RADIUS_RATIO), fill=GREEN)
    r = int(s * 0.19)
    cx = cy = s // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CREAM)
    return img.resize((size, size), Image.LANCZOS)


def _content_box(img: Image.Image) -> tuple[int, int, int, int]:
    """非白像素的包围盒——原图四周的白边留在外面，不能被当成图标本体。"""
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    left, top, right, bottom = w, h, 0, 0
    step = max(1, min(w, h) // 400)          # 大图抽样扫描，够快又不漏
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            if r > 244 and g > 244 and b > 244:
                continue                      # 白底
            if x < left:
                left = x
            if x > right:
                right = x
            if y < top:
                top = y
            if y > bottom:
                bottom = y
    if right <= left or bottom <= top:
        return (0, 0, w, h)
    return (left, top, right + 1, bottom + 1)


def _master(art: Path, side: int = 1024) -> Image.Image:
    """裁到图形边界 + 套圆角蒙版，返回 side×side 的 RGBA 主图。"""
    src = Image.open(art).convert("RGB")
    art_img = src.crop(_content_box(src))
    w, h = art_img.size
    if w != h:                                # 别把圆角压扁
        s = min(w, h)
        art_img = art_img.crop(((w - s) // 2, (h - s) // 2,
                                (w - s) // 2 + s, (h - s) // 2 + s))
    art_img = art_img.resize((side, side), Image.LANCZOS)

    # 4 倍超采样画圆角，边缘才不毛糙
    ss = 4
    mask = Image.new("L", (side * ss, side * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, side * ss - 1, side * ss - 1],
        radius=int(side * ss * RADIUS_RATIO), fill=255)
    mask = mask.resize((side, side), Image.LANCZOS)

    out = art_img.convert("RGBA")
    out.putalpha(mask)
    return out


def main() -> None:
    if ART.exists():
        master = _master(ART)
        source = "icon_art.png"
    else:
        master = _fallback(1024)
        source = "内置几何图形（没找到 icon_art.png）"

    ico = HERE / "bampiao.ico"
    master.resize((256, 256), Image.LANCZOS).save(
        ico, format="ICO", sizes=[(n, n) for n in SIZES])
    master.resize((512, 512), Image.LANCZOS).save(HERE / "bampiao.png")
    # macOS 的 .icns 要 1024 起（512@2x），单独存一张
    master.resize((1024, 1024), Image.LANCZOS).save(HERE / "bampiao_1024.png")
    # 小尺寸预览：肉眼确认 16/32 还认得出
    for n in (16, 32, 48):
        master.resize((n, n), Image.LANCZOS).save(HERE / ("preview_%d.png" % n))

    print("图标已生成：%s（来源 %s，%d 个尺寸）" % (ico, source, len(SIZES)))


if __name__ == "__main__":
    main()
