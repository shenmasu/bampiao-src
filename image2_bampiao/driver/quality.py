"""图片质检：比例/尺寸/动图/重复度。

单独一个模块，只有一个原因：cdp_run 是"导入即跑"的驱动脚本，
测试没法 import 它。把纯函数放这里，驱动和测试都能用（2026-11-06）。

SVG 单独一个 ``is_good_svg``：它是 XML 文本，Pillow 打不开，也不能按
静图的透明底 / 720px 门槛 / 洋红背景去挑刺（2026-11-07）。
``is_good`` 本体一行没动。
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image


def dhash(p, size=8):
    im = Image.open(p).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = im.load()
    b = 0
    for y in range(size):
        for x in range(size):
            b = (b << 1) | (1 if px[x, y] > px[x + 1, y] else 0)
    return b


def parse_aspect(s):
    """把 "2:3" / "16:9" / "1.5" 解析成宽高比；解析不了返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[:：x×]\s*(\d+(?:\.\d+)?)$", s)
    try:
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            return (a / b) if b else None
        v = float(s)
        return v if v > 0 else None
    except Exception:
        return None


def is_good_svg(path, anim=False):
    """SVG 的质检。返回 ``(ok, note)``，和 ``is_good`` 同形状。

    硬拒只有三条：读不出来、不是合法 XML、根元素不是 <svg>。
    其余（尺寸小、没写 viewBox、要动效却是静的）只标注不判死——
    SVG 是文本文件，跟位图不是一套质量标准，额度已经花了就交给主模型判断。
    """
    import xml.etree.ElementTree as ET

    try:
        raw = Path(path).read_bytes()
    except Exception as e:
        return False, "读取失败 %s" % e
    if not raw or len(raw) < 40:
        return False, "SVG 文件为空或过小"
    text = raw.decode("utf-8", "replace")
    try:
        root = ET.fromstring(text)
    except Exception as e:
        # 有些 SVG 带 DOCTYPE / 未声明实体，ET 会拒绝；只要看得出是 svg 就放行
        if "<svg" not in text.lower()[:4096]:
            return False, "不是合法 SVG（XML 解析失败：%s）" % str(e)[:60]
    else:
        if root.tag.split("}")[-1].lower() != "svg":
            return False, "根元素不是 <svg>（%s）" % root.tag[:50]
    from .. import media as _media

    info = _media.probe_svg(path)
    if not info:
        return False, "读不出 SVG 头"
    flags = []
    if anim and not info.get("svg_animated"):
        flags.append("要的是会动的 SVG，但拿到的是静态矢量图")
    elif info.get("svg_animated"):
        flags.append("SVG 内含动效（SMIL/CSS）")
    if not info.get("size"):
        flags.append("没读到 viewBox/尺寸（不影响浏览器渲染）")
    # 外链脚本在 <img> 里不执行，但交付给别人时该提醒一句
    if "<script" in text.lower() or "onload=" in text.lower():
        flags.append("含脚本/事件属性，嵌入网页前请检查")
    return True, "；".join(flags)


def is_good(path, known, want=None, need_alpha=False, anim=False):
    """质检。``anim=True`` 表示这一条要的是动图（Images 2.6 的 GIF）。

    动图不是"多帧的图"那么简单：GIF 的调色板天然带透明键，不能按 PNG 的
    alpha 逻辑去挑刺；尺寸门槛也该放宽（GIF 通常比静图小）。
    """
    try:
        raw = Image.open(path)
        frames = int(getattr(raw, "n_frames", 1) or 1)
        is_anim = frames > 1
        fmt = (raw.format or "").upper()
        # GIF 的 P 模式常带 transparency，但那是动画调色板的一部分，不是"没抠干净"
        has_alpha = (raw.mode in ("RGBA", "LA")
                     or (raw.mode == "P" and "transparency" in raw.info)) and fmt != "GIF"
        im = raw.convert("RGB")
        w, h = im.size
        ratio = w / max(h, 1)
        if min(w, h) < 256:
            return False, "尺寸异常 %dx%d" % (w, h)      # 缩略图/坏图，硬拒

        flag = ""
        if is_anim:
            flag = "动图 %d 帧" % frames
        elif anim:
            flag = "要的是动图，但拿到的是静态图"
        # 尺寸/比例一律只做「标注」，不判死：图已经花了额度，交不交由主模型决定
        want_r = parse_aspect(want)
        if want_r and abs(ratio - want_r) / want_r > 0.06:
            _f = "比例不符（要求 %s≈%.2f，实际 %.2f %dx%d）" % (want, want_r, ratio, w, h)
            flag = (flag + "；" if flag else "") + _f
        elif not is_anim and min(w, h) < 720:
            _f = "尺寸偏小 %dx%d" % (w, h)
            flag = (flag + "；" if flag else "") + _f

        px = im.load()
        cs = [px[3, 3], px[w - 4, 3], px[3, h - 4], px[w - 4, h - 4]]
        mg = sum(1 for c in cs if c[0] > 170 and c[1] < 110 and c[2] > 170)
        if has_alpha:
            # 透明底 PNG 是好事（游戏直接用），不算不合格
            a = raw.convert("RGBA").getchannel("A")
            import numpy as _np
            arr = _np.array(a)
            transparent_ratio = float((arr < 16).mean())
            if transparent_ratio < 0.05:
                flag = (flag + "；" if flag else "") + "文件带 alpha 通道但无透明区（与任务 alpha 开关无关）"
        elif mg < 3 and need_alpha:
            flag = (flag + "；" if flag else "") + "底非洋红（可能抠不了图）"
        hh = dhash(path)
        for kh in known:
            if bin(hh ^ kh).count("1") <= 6:
                # 相似不等于错：同一套棋子/表情本来就长得很像。
                # 交给主模型判断，这里只标注（此前硬拒会误杀正版图）。
                flag = (flag + "；" if flag else "") + "疑似与历史重复（可能是抓错图，请核对）"
        return True, flag
    except Exception as e:
        return False, f"读取失败 {e}"
