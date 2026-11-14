"""Cloudflare 挑战：先自己试，试不动再叫人。

2026-11-02 的事故里，页面只留一个 `cloudflare_challenge` 红条。实测发现：
- 有时候**等几秒它自己就没了**（托管质询自己过）；
- 有时候**刷新一次**就没了；
- 实在不行，**重启这个号的浏览器**一定有效（换了个干净会话）。

所以顺序是：等 → 刷新 → 点一下（如果真给了可点的框）→ 还不行才叫人。
人只在最后一步被打扰。
"""
from __future__ import annotations

import time

WAIT_SELF = 8.0        # 先等它自己过（托管质询常见）
WAIT_RELOAD = 6.0      # 刷新后再给的时间


async def _still(pg) -> bool:
    try:
        return bool(await pg.evaluate(
            "() => (document.body.innerText||'').toLowerCase().includes('cloudflare_challenge')"))
    except Exception:
        return True


async def _click_turnstile(pg) -> bool:
    """如果页面里嵌了 Turnstile 的复选框，点它一下。点不到就算了。

    只点"验证你是人"的框，不做任何绕指纹/绕脚本的动作——那是另一回事，也不该做。
    """
    try:
        frames = [f for f in pg.frames if "challenges.cloudflare.com" in (f.url or "")]
    except Exception:
        return False
    for f in frames:
        try:
            box = f.locator("input[type=checkbox], .cb-lb, label")
            if await box.count():
                await box.first.click(timeout=3000)
                return True
        except Exception:
            continue
    # 有些版本把 checkbox 放在主文档里
    try:
        box = pg.locator("#challenge-stage input[type=checkbox]")
        if await box.count():
            await box.first.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


async def try_auto_pass(pg, log=None, allow_reload: bool = True,
                        on_show=None, on_hide=None,
                        wait_visible: float = 40.0) -> tuple[bool, str]:
    """依次尝试"自己过"，返回 (是否过了, 说明)。"""

    def say(m: str) -> None:
        if log:
            try:
                log(m)
            except Exception:
                pass

    # ① 先把窗口亮出来，再等它自己过。
    # 2026-11-03 实测：Chrome 一直在隐藏/屏幕外窗口里跑，Cloudflare 的托管质询**永远不过**
    # （页面卡在"请稍候…"，用户说"没有让我点的地方"）。把窗口显示出来的那一刻，5 秒就过了。
    # 所以"藏窗口"这件事和"过验证"是冲突的：遇到验证必须先亮窗口。
    shown = False
    if on_show is not None:
        try:
            on_show()
            shown = True
            say("    .. 已把浏览器窗口亮出来（Cloudflare 需要可见窗口才能过）")
        except Exception:
            shown = False

    deadline = time.time() + (wait_visible if shown else 0)
    while time.time() < deadline:
        await pg.wait_for_timeout(1500)
        if not await _still(pg):
            say("    .. 窗口可见后，质询自己过了")
            if on_hide is not None:
                try:
                    on_hide()
                except Exception:
                    pass
            return True, "visible"

    await pg.wait_for_timeout(int(WAIT_SELF * 1000))
    if not await _still(pg):
        say("    .. Cloudflare 质询自己过了")
        return True, "self"

    # ② 点一下（如果给了可点的框）
    if await _click_turnstile(pg):
        await pg.wait_for_timeout(5000)
        if not await _still(pg):
            say("    .. 点了验证框，过了")
            return True, "click"

    # ③ 刷新一次再等。开场已经发出去时不能刷新——会把对话冲掉（实测 no-turns 白跑）
    if not allow_reload:
        return False, "give_up"
    try:
        await pg.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    await pg.wait_for_timeout(int(WAIT_RELOAD * 1000))
    if not await _still(pg):
        say("    .. 刷新后挑战没了")
        return True, "reload"

    return False, "give_up"
