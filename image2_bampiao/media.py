"""交付文件的类型：静图还是动图。

2026-11-06 实测：ChatGPT 网页版 Images 2.6 直接返回 GIF（blob type=image/gif，
GIF89a、12 帧）。半瓢原来的保存逻辑一律写 ``.png``——字节是 GIF、名字是 PNG，
浏览器能靠内容嗅探放出来，但上传平台、素材库、剪辑软件按扩展名判断就废了。

所以：**扩展名必须跟内容走**。这里只做这一件事，顺带提供少量探测函数，
别把驱动和面板各自再写一套。
"""
from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = (".png", ".gif", ".webp", ".jpg", ".jpeg", ".avif")

# MIME → 扩展名（ChatGPT 的 blob 一般给 image/png、image/gif、image/webp）
_EXT_BY_MIME = {
    "image/gif": ".gif",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/avif": ".avif",
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
    m = (mime or "").split(";")[0].strip().lower()
    if m in _EXT_BY_MIME:
        return _EXT_BY_MIME[m]
    return ".png"


def is_anim_ext(name_or_ext: str) -> bool:
    """可能带多帧的格式。传 "S01.gif" 或 ".gif" 都行。

    以前只认 ".gif"，调用方传的是完整文件名 → 永远 False（2026-11-06 复评）。
    """
    s = str(name_or_ext or "").lower()
    ext = Path(s).suffix.lower() if Path(s).suffix else s
    return ext in (".gif", ".webp")


def outputs(out_dir: Path) -> list[Path]:
    """驱动目录里真正要交付的图：S01_xxx.png / .gif …

    排除 ``_flags.json``、``_c.png`` 这类中间文件（前缀下划线）。
    """
    try:
        rows = [p for p in Path(out_dir).iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                and not p.name.startswith("_")]
    except OSError:
        return []
    return sorted(rows)


def find_output(out_dir: Path, key: str, task: str) -> Path | None:
    """续跑时找这一项已有的交付图，不管它是 png 还是 gif。"""
    want = "%s_%s" % (key, task)
    for p in outputs(out_dir):
        if p.stem == want:
            return p
    return None


def probe(path: str | Path) -> dict:
    """读一下这文件的真实类型/帧数。读不了就返回空 dict，绝不抛。"""
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
