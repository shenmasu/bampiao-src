"""PyInstaller runtime hook：把 stdout / stderr 强制成 UTF-8。

为什么要单独一个 hook：
源码跑 `python -m image2_bampiao` 时中文输出是正常的，但 PyInstaller 打出来的
exe 用的是 cp936 —— 在 cli.py 里 `sys.stdout.reconfigure()`、换 TextIOWrapper
**都不管用**（0.2.39 到 0.2.44 连试六版才发现）。frozen 环境里 Python 的 stdio
在脚本跑起来之前就按控制台代码页建好了，runtime hook 是唯一能在
"它建好之后、脚本执行之前"介入的地方。

★ 第一版把 exe 直接搞崩了：`ValueError: I/O operation on closed file`。
原因是重包之后旧 wrapper 没人引用，GC 回收它时顺手把底层 buffer 关掉了，
新 wrapper 拿到的是一个已关闭的 buffer。所以必须把旧对象存起来（_HOLD）。
"""
import io
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 关键：压住旧 wrapper，别让 GC 回收时把 buffer 关掉
_HOLD: list = []


def _wrap(name: str) -> None:
    s = getattr(sys, name, None)
    buf = getattr(s, "buffer", None)
    if buf is None:
        return
    try:
        _HOLD.append(s)
        setattr(sys, name, io.TextIOWrapper(
            buf, encoding="utf-8", errors="replace", line_buffering=True))
    except Exception:
        pass


_wrap("stdout")
_wrap("stderr")
