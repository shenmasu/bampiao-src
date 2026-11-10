"""卡死看门狗：进程活着，但一行日志都不再增长。

2026-11-02 实测：`bampiao.exe gen` 进程还在，日志停在 487 秒处不动，还占着账号的
出图锁 —— 面板永远显示"在跑"，自动回收看到锁被占也跳过它，用户只能开任务管理器。
进程活着 ≠ 在干活，所以要有这一条。

放在单独模块里是为了能单测：`cdp_run.py` 是"导入即执行"的，import 它会跑 argparse。
"""
from __future__ import annotations

import os
import sys
import threading
import time

ALIVE = {"t": time.time()}
STUCK_AFTER = 420          # 秒。7 分钟一行日志都没有 = 卡死（正常每 30 秒必有一行）


def touch() -> None:
    """有进展就调一下。"""
    ALIVE["t"] = time.time()


def idle_seconds() -> float:
    return time.time() - ALIVE["t"]


def is_stuck(idle: float, limit: float = STUCK_AFTER) -> bool:
    return idle > limit


def start(limit: float = STUCK_AFTER, interval: float = 30.0) -> None:
    """起一个后台线程，超时就写一行日志然后硬退出（锁会随进程释放）。"""

    def _watch() -> None:
        while True:
            time.sleep(interval)
            idle = idle_seconds()
            if is_stuck(idle, limit):
                try:
                    print("[%s] !! 卡死：%d 秒没有任何进展，强制结束本进程（锁会随进程释放）"
                          % (time.strftime("%H:%M:%S"), idle), flush=True)
                except Exception:
                    pass
                os._exit(9)

    threading.Thread(target=_watch, daemon=True, name="stuck-watchdog").start()
