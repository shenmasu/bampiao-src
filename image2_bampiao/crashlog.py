"""崩溃/异常自动留档。

2026-11-03 用户要求：不能只在用户主动点「反馈」时才带日志，**出问题时就得拿到**。

做法：
- 装 `sys.excepthook` / `threading.excepthook`，任何没被接住的异常都落一个文件；
- 文件里带版本、命令行、系统、traceback、以及最近若干行业务日志（context 才是关键）；
- 只留最近 10 个，别把磁盘堆满；
- **绝不自动外发**：只落本地，面板/`bampiao diag` 能看到，发不发由人决定。
"""
from __future__ import annotations

import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

MAX_KEEP = 10
TAIL_LINES = 60


def _logs_dir(data_dir) -> Path:
    return Path(str(data_dir)) / "logs"


def _tail_of(path: Path, n: int = TAIL_LINES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def record(data_dir, kind: str, exc_text: str, extra: str = "") -> Path:
    """写一条崩溃记录，返回文件路径。任何异常都不往外抛。"""
    logs = _logs_dir(data_dir)
    try:
        logs.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = logs / ("crash-%s-%s-%d.log" % (ts, kind, os.getpid()))
        try:
            from . import __version__ as ver      # crashlog 在包根，不能写 ".."
        except Exception:
            ver = "?"
        head = [
            "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "版本: %s" % ver,
            "类型: %s" % kind,
            "进程: %d" % os.getpid(),
            "系统: %s %s" % (platform.system(), platform.release()),
            "Python: %s" % sys.version.split()[0],
            "命令行: %s" % " ".join(sys.argv[:6]),
        ]
        if extra:
            head.append("附注: %s" % extra)
        body = "\n".join(head) + "\n\n---- traceback ----\n" + (exc_text or "(无)")
        tail = _tail_of(logs / "desktop.log")
        if tail:
            body += "\n\n---- desktop.log 尾部 ----\n" + tail
        p.write_text(body, encoding="utf-8")
        _prune(logs)
        return p
    except Exception:
        return Path("")


def _prune(logs: Path) -> None:
    try:
        items = sorted(logs.glob("crash-*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in items[MAX_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def list_crashes(data_dir) -> list[dict]:
    out = []
    try:
        for p in sorted(_logs_dir(data_dir).glob("crash-*.log"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            out.append({"file": str(p), "at": p.stat().st_mtime,
                        "size": p.stat().st_size})
    except Exception:
        return []
    return out


def install(data_dir) -> None:
    """挂上全局钩子。桌面端和驱动都该在启动最早期调用。"""
    def _hook(exc_type, exc, tb):
        try:
            record(data_dir, "uncaught",
                   "".join(traceback.format_exception(exc_type, exc, tb)))
        finally:
            sys.__excepthook__(exc_type, exc, tb)

    def _thook(args):
        try:
            record(data_dir, "thread",
                   "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                      args.exc_traceback)),
                   extra="线程 %s" % getattr(args.thread, "name", "?"))
        except Exception:
            pass

    sys.excepthook = _hook
    try:
        threading.excepthook = _thook
    except Exception:
        pass
