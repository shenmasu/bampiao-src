"""0.2.0 图标：几何瓢（墨黑半瓢 + 一弧朱红水），原图由半瓢自己出。

原图 packaging/icon_art_v2.png 是米白底整幅图。这里：
  1. 按「不是米白」的像素找出瓢的包围盒，裁掉留白；
  2. 重新铺一张纯米白方底（直角，不做圆角——去 AI 味，2026-10-30 用户定的方向）；
  3. 大尺寸瓢占 76%，16/20/24 这些小尺寸放大到 90%，免得在任务栏里只剩一个黑点；
  4. 输出多尺寸 .ico、面板/托盘用的 icon.png、Mac 用的 1024 原图和小尺寸预览。
"""

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ART = HERE / "icon_art_v2.png"
CREAM = (243, 242, 238, 255)
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _subject() -> Image.Image:
    im = Image.open(ART).convert("RGB")
    bg = im.getpixel((8, 8))
    mask = Image.new("L", im.size, 0)
    px, mp = im.load(), mask.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = px[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 60:
                mp[x, y] = 255
    box = mask.getbbox()
    sub = im.crop(box).convert("RGBA")
    # 把米白底变透明，好贴到新的方底上
    alpha = mask.crop(box)
    sub.putalpha(alpha)
    return sub


def render(size: int, sub: Image.Image) -> Image.Image:
    s = size * 4
    tile = Image.new("RGBA", (s, s), CREAM)
    frac = 0.90 if size <= 24 else 0.76
    k = frac * s / max(sub.size)
    w, h = int(sub.width * k), int(sub.height * k)
    tile.alpha_composite(sub.resize((w, h), Image.LANCZOS), ((s - w) // 2, (s - h) // 2 + int(s * 0.02)))
    return tile.resize((size, size), Image.LANCZOS)


def main() -> None:
    sub = _subject()
    imgs = [render(n, sub) for n in SIZES]
    imgs[-1].save(HERE / "bampiao.ico", sizes=[(n, n) for n in SIZES], append_images=imgs[:-1])
    render(1024, sub).save(HERE / "bampiao_1024.png")
    render(512, sub).save(HERE / "bampiao.png")
    render(256, sub).save(ROOT / "image2_bampiao" / "ui" / "static" / "icon.png")
    for n in (16, 32, 48):
        render(n, sub).save(HERE / ("preview_%d.png" % n))
    print("ok", sub.size)


if __name__ == "__main__":
    main()
