"""洋红底 → 真透明。

`--alpha` 让模型出「纯洋红 #FF00FF 背景」的图，但模型给的是一张**带洋红底的位图**，
并不是透明 PNG。webimg 和半瓢以前都只**检查**"是不是洋红底"，没有真的抠。
这个模块补上最后一步：把洋红背景换成 alpha=0。

为什么用 flood fill 而不是"全图阈值"：
    主体里常常也有粉色/洋红（花、衣服、腮红）。全图设阈值会把主体一起抠穿。
    正确的做法是**只抠连通到画布边缘的那一片背景**——主体内部的粉色不是背景，
     flood fill 从四角和四边中点出发，走不到它。

用法：
    from . import matte
    matte.magenta_to_alpha(path)      # 就地改写，返回 {"ok":bool, "ratio":float, "why":str}
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# flood fill 时用来标记背景的"哨兵色"（必须是一个正常图里不会出现的颜色）
_MARK = (1, 2, 3)


def magenta_to_alpha(path: str | Path, *, thresh: int = 70,
                     feather: float = 0.6, min_ratio: float = 0.02) -> dict:
    """把洋红背景就地转成透明，返回 {"ok", "ratio", "why"}。

    thresh    颜色容差（0–441）。模型出的洋红很少是纯 #FF00FF，通常带抖动，
              70 能盖住这种抖动，又不会把主体的暗部当背景。
    feather   边缘羽化半径（像素）。0 表示不羽化。
    min_ratio 抠掉的像素占比低于这个值就认为"这张图不是洋红底"，不动它。
    """
    p = Path(path)
    im = Image.open(p).convert("RGBA")
    w, h = im.size
    rgb = im.convert("RGB")

    # 从四角 + 四边中点 flood fill。主体在画面中央时，这些种子点都落在背景上。
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
             (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for xy in seeds:
        # 先确认这个种子点本身就是洋红：flood fill 的容差是**相对种子点**的，
        # 如果种子点是蓝色/白色，它会把那一整片非洋红区域也填成背景色，
        # 于是一张纯蓝底的图会被整个抠穿（测试抓到的边界 bug）。
        px = rgb.getpixel(xy)
        dist = ((px[0] - 255) ** 2 + px[1] ** 2 + (px[2] - 255) ** 2) ** 0.5
        if dist > thresh:
            continue
        try:
            ImageDraw.floodfill(rgb, xy, _MARK, thresh=thresh)
        except Exception:
            # 种子点落在主体上（比如主体顶到边角）就跳过，不影响其它种子
            continue

    arr = np.array(rgb)
    bg = np.all(arr == _MARK, axis=-1)
    ratio = float(bg.mean())
    if ratio < min_ratio:
        return {"ok": False, "ratio": ratio,
                "why": "几乎没找到洋红背景（这张图可能不是洋红底），保留原图"}

    alpha = np.array(im.getchannel("A"))
    alpha[bg] = 0
    a = Image.fromarray(alpha, mode="L")
    if feather:
        # 只对 alpha 羽化：背景内部仍是 0、主体内部仍是 255，
        # 只有交界那一圈变成半透明，边缘就不会是一圈硬锯齿。
        a = a.filter(ImageFilter.GaussianBlur(feather))
    im.putalpha(a)
    im.save(p)
    return {"ok": True, "ratio": ratio,
            "why": "已把 %.0f%% 的背景转成透明" % (ratio * 100)}
