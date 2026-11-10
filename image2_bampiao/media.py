"""交付文件的类型：静图还是动图。

2026-11-06 实测：ChatGPT 网页版 Images 2.6 直接返回 GIF（blob type=image/gif，
GIF89a、12 帧）。半瓢原来的保存逻辑一律写 ``.png``——字节是 GIF、名字是 PNG，
浏览器能靠内容嗅探放出来，但上传平台、素材库、剪辑软件按扩展名判断就废了。

所以：**扩展名必须跟内容走**。这里只做这一件事，顺带提供少量探测函数，
别把驱动和面板各自再写一套。

2026-11-07 新增 SVG：网页版还能交付 ``.svg`` 矢量文件（image/svg+xml，XML 文本，
可能带 SMIL/CSS 动画）。它不是位图，Pillow 打不开，所以单独一条探测/质检路径；
``IMAGE_EXTS`` 刻意**不加** ``.svg``，保证原来「扫目录找位图」的行为一字不变。
"""
from __future__ import annotations

import re
from pathlib import Path

IMAGE_EXTS = (".png", ".gif", ".webp", ".jpg", ".jpeg", ".avif")
SVG_EXTS = (".svg",)
# 只有明确出 SVG 的单子才扫 .svg；默认扫描仍然是纯位图集合。
ALL_EXTS = IMAGE_EXTS + SVG_EXTS

# MIME → 扩展名（ChatGPT 的 blob 一般给 image/png、image/gif、image/webp）
_EXT_BY_MIME = {
    "image/gif": ".gif",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}

# 头几个字节兜底：MIME 缺失或说错时也能认出来
_MAGIC = (
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"RIFF", ".webp"),       # 严格说还要看 8-12 字节是 WEBP；这里够用
    (b"\xff\xd8\xff", ".jpg"),
)


def ext_for(mime: str = "", data: bytes = b"") -> str:
    """按 MIME 优先、文件头兜底，给出扩展名；认不出来按 png。"""
    # 先看字节：MIME 可能被代理/扩展改错（0.2.51 的病根就是"名字说 png"）。
    head = bytes(data[:12])
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if is_svg_data(data):
        return ".svg"
    m = (mime or "").split(";")[0].strip().lower()
    if m in _EXT_BY_MIME:
        return _EXT_BY_MIME[m]
    return ".png"


def is_svg_data(data: bytes) -> bool:
    """这几个字节像不像一个 SVG/XML 文件。只认明确的头，别把位图误判了。"""
    if not data:
        return False
    head = bytes(data[:256]).lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith((b"<?xml", b"<svg")) and b"<svg" in bytes(data[:4096]).lower()


def is_anim_ext(name_or_ext: str) -> bool:
    """可能带多帧的格式。传 "S01.gif" 或 ".gif" 都行。

    以前只认 ".gif"，调用方传的是完整文件名 → 永远 False（2026-11-06 复评）。
    """
    s = str(name_or_ext or "").lower()
    ext = Path(s).suffix.lower() if Path(s).suffix else s
    return ext in (".gif", ".webp")


def outputs(out_dir: Path, include_svg: bool = False) -> list[Path]:
    """驱动目录里真正要交付的图：S01_xxx.png / .gif …

    排除 ``_flags.json``、``_c.png`` 这类中间文件（前缀下划线）。

    ``include_svg`` 默认 False：**只有明确出 SVG 的单子才传 True**。
    老调用方一个字都不用改，扫到的东西跟以前完全一致。
    """
    exts = ALL_EXTS if include_svg else IMAGE_EXTS
    try:
        rows = [p for p in Path(out_dir).iterdir()
                if p.is_file() and p.suffix.lower() in exts
                and not p.name.startswith("_")]
    except OSError:
        return []
    return sorted(rows)


def find_output(out_dir: Path, key: str, task: str,
                include_svg: bool = False) -> Path | None:
    """续跑时找这一项已有的交付图，不管它是 png 还是 gif。"""
    want = "%s_%s" % (key, task)
    for p in outputs(out_dir, include_svg=include_svg):
        if p.stem == want:
            return p
    return None


def is_svg_path(path: str | Path) -> bool:
    return str(path or "").lower().endswith(SVG_EXTS)


_SVG_ANIM_RE = re.compile(
    r"<(?:animate|animateTransform|animateMotion|set)\b|@keyframes\b|"
    r"<style[^>]*>[^<]*animation\s*:",
    re.I,
)


def svg_animated(text: str) -> bool:
    """SVG 里有没有真的会动的东西（SMIL 标签 / CSS 关键帧 / style 里的 animation）。

    ``<animate>`` 这类标签在 ``<img src=...svg>`` 里也会跑，浏览器和 Figma
    都能看到动效；光有 ``<style>`` 不算数。
    """
    return bool(_SVG_ANIM_RE.search(text or ""))


def probe_svg(path: str | Path) -> dict:
    """读一个 SVG 文件的类型/尺寸/是否带动效。绝不抛，读不了给空 dict。

    ``animated`` 恒为 False：那是给 GIF 多帧用的，SVG 一动它就会被
    面板贴上 "GIF" 角标。SVG 的动效单独放 ``svg_animated``。
    ``frames`` 恒为 1，别让面板显示"动图 1 帧"。
    """
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8", "replace")
        low = text.lower()
        if "<svg" not in low[:4096]:
            return {}
        size: list[int] = []
        m = re.search(r"\bviewBox\s*=\s*[\"']\s*[-\d.]+\s+[-\d.]+\s+([-\d.]+)\s+([-\d.]+)",
                      text, re.I)
        if not m:
            m = re.search(r"\bwidth\s*=\s*[\"']\s*(\d+(?:\.\d+)?)[\"'][^>]*"
                          r"\bheight\s*=\s*[\"']\s*(\d+(?:\.\d+)?)", text, re.I)
        if m:
            try:
                size = [int(float(m.group(1))), int(float(m.group(2)))]
            except (TypeError, ValueError):
                size = []
        return {
            "format": "SVG",
            "frames": 1,
            "size": size,
            "animated": False,
            "svg_animated": svg_animated(text),
            "svg": True,
            "bytes": len(raw),
        }
    except Exception:
        return {}


def probe(path: str | Path) -> dict:
    """读一下这文件的真实类型/帧数。读不了就返回空 dict，绝不抛。

    SVG 是 XML 文本，Pillow 一定打不开；先分流，别让它落进位图路径报"读取失败"。
    """
    if is_svg_path(path):
        return probe_svg(path)
    try:
        from PIL import Image

        with Image.open(path) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            return {
                "format": (im.format or "").upper(),
                "frames": frames,
                "size": list(im.size),
                "animated": frames > 1,
            }
    except Exception:
        return {}
