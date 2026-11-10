"""在后台把「一句话」交给某个账号的网页去答（供队列/面板的文本单用）。

和 `cli.ask` 的区别只有一个：这里不碰命令行参数和打印，给调度器/面板调用。
流程和 `cli._ask_over_cdp` 一致：起浏览器 → 连 CDP → webchat.ask。

2026-11-06：文本单要能"重问"，就必须能进队列（有 running 状态、有 started_at）。
"""
from __future__ import annotations

import asyncio
import time


async def _ask_over_port(port: int, text: str, url: str, timeout: float, log=None) -> dict:
    from playwright.async_api import async_playwright

    from . import webchat

    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        pg = None
        for ctx in b.contexts:
            for q in ctx.pages:
                if "chatgpt.com" in (q.url or ""):
                    pg = q
                    break
            if pg:
                break
        if pg is None:
            ctx = b.contexts[0] if b.contexts else await b.new_context()
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await webchat.ask(pg, text, url or None, timeout=timeout, log=log)


def run_text(cfg, acc, text: str, *, timeout: float = 300.0, url: str = "") -> dict:
    """返回 webchat.ask 的形状：{ok, text, error, seconds, url}。

    起不来浏览器不算"回答失败"：给 NO_LANE，让上层决定是重排还是报环境问题。
    """
    from . import browser

    # 每次问话都留一份日志：EMPTY_REPLY 那种，没有日志根本看不出是没发出去、
    # 还是回复晚到了（2026-11-06 实际踩过）。
    logp = cfg.logs_dir / ("ask_%s_%d.log" % (acc.id, int(time.time())))
    logp.parent.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        try:
            with open(logp, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except OSError:
            pass

    _log("开始问话（%d 字，超时 %d 秒）" % (len(text), timeout))
    try:
        browser.ensure(cfg, acc)
    except Exception as e:          # noqa: BLE001
        _log("!! 浏览器起不来：%s" % e)
        return {"ok": False, "text": "", "error": "NO_LANE: %s" % e, "log": str(logp)}

    h = browser.read_handle(cfg, acc.id)
    port = int(getattr(h, "port", 0) or 0) if h else 0
    if not port:
        _log("!! 浏览器没起来（没有端口）")
        return {"ok": False, "text": "", "error": "NO_LANE: 浏览器没起来",
                "log": str(logp)}

    try:
        r = asyncio.run(_ask_over_port(port, text, url, timeout, log=_log))
    except Exception as e:          # noqa: BLE001
        _log("!! 异常：%s: %s" % (type(e).__name__, e))
        return {"ok": False, "text": "", "error": "%s: %s" % (type(e).__name__, e),
                "log": str(logp)}
    r["log"] = str(logp)
    _log("结束：ok=%s 用时 %ss %s" % (r.get("ok"), r.get("seconds"), r.get("error") or ""))
    return r
