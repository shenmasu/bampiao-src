"""页面动作的超时闸。

2026-11-03 实测：`bampiao probe` 连上 CDP 后就没了下文，卡满 120 秒；
出图中途也出现过"页面还在、日志不动"。根因是 Playwright 的 `page.evaluate`
**不受 set_default_timeout 管**——渲染进程一卡，这个 await 永不返回，
只能靠外层 120/540 秒的总超时兜底，那时用户已经认定"软件死了"。

这里在 page 对象上装一道闸：单次 evaluate 超过 N 秒就抛 PageWedged，
调用方能立刻收手、留重启标记、把浏览器换掉。
"""
from __future__ import annotations

import asyncio
import time

EVAL_TIMEOUT = 35.0


class PageWedged(RuntimeError):
    """页面无响应。"""


def guard_page(pg, timeout: float = EVAL_TIMEOUT, log=None):
    """给 page.evaluate 装超时。返回同一个 page，方便链式调用。"""
    orig = pg.evaluate

    async def _guarded(expression, arg=None, **kw):
        try:
            return await asyncio.wait_for(orig(expression, arg, **kw), timeout)
        except asyncio.TimeoutError:
            if log:
                try:
                    log("[%s] !! 页面 %d 秒没有响应（evaluate 超时）"
                        % (time.strftime("%H:%M:%S"), int(timeout)))
                except Exception:
                    pass
            raise PageWedged("页面无响应") from None

    pg.evaluate = _guarded
    return pg
