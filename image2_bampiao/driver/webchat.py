"""网页对话驱动：在 ChatGPT 网页里发一句话，把回复的文本取回来。

为什么单独一个文件、不 import cdp_run：
`cdp_run` 在模块级就 `asyncio.run` 了（它是给 --driver 子进程用的），
import 它等于把整个出图流程再跑一遍。所以这里自己实现三件事：
发消息、等生成完、读回复。

2026-11-05 实测的坑（都写了注释，别删）：
1. 这个版本的 ChatGPT **类名全是哈希**（`DilRenderer-tB76Jj` / `Reveal-k2IWvM`），
   `data-message-author-role`、`[data-testid^="conversation-turn"]`、`.markdown`
   **一个都不存在**。所以读回复只能靠 `main.innerText` + 文案切分。
2. 页面上会混进输入框占位、"ChatGPT 可能会出错"、"最新一条回复"、"思考强度" 这些噪音，
   必须在切分时剔掉。
3. 判断"生成完了"仍然是 `button[data-testid="stop-button"]`（或 aria-label=停止），
   这个和多号出图用的是同一个信号。
"""
from __future__ import annotations

import asyncio
import re

# 回复正文前后的分隔文案（中文 / 英文都认，尽量别依赖单一语言）
_HEAD_MARKS = ("ChatGPT 说：", "ChatGPT 说:", "ChatGPT said:", "ChatGPT said：")
# 正文之后出现的页脚噪音，从这里开始全部丢掉
_TAIL_MARKS = (
    "ChatGPT 可能会出错",
    "ChatGPT can make mistakes",
    "最新一条回复",
    "思考强度",
    "Reasoning",
    "询问 ChatGPT",
    "Ask anything",
    # 回复末尾的反馈条（实测会跟着正文一起被 innerText 带出来）
    "到目前为止，这段对话对你有帮助吗",
    "Was this conversation helpful",
    "这段对话对你有帮助吗",
)

_STOP_SEL = ('button[data-testid="stop-button"], button[aria-label="停止"], '
             'button[aria-label="Stop answering"]')
_SEND_SEL = ('button[data-testid="send-button"], button[aria-label="发送"], '
             'button[aria-label="Send prompt"]')
BOX_SEL = 'div.ProseMirror'


def _head_count(s: str) -> int:
    """数一共出现了几个"ChatGPT 说："分隔符。

    用来判断"这一轮是不是新写了一条回复"。只数中文那一种的话，英文界面/回复里
    带引号就会算错（2026-11-06 评审）。几种写法都数，各算各的不会重复。
    """
    return sum((s or "").count(m) for m in _HEAD_MARKS)


async def _main_text(pg) -> str:
    try:
        return await pg.evaluate(
            "() => (document.querySelector('main') || document.body).innerText || ''")
    except Exception:
        return ""


async def _busy(pg) -> bool:
    try:
        return bool(await pg.evaluate("() => !!document.querySelector(%r)" % _STOP_SEL))
    except Exception:
        return False


async def _composer_text(pg) -> str:
    try:
        return await pg.evaluate(
            "() => {const e=document.querySelector('div.ProseMirror');"
            " return e ? (e.innerText||'').trim() : '';}")
    except Exception:
        return ""


async def _type_and_send(pg, text: str, log=None) -> bool:
    """点输入框 → 清空 → 输入 → 发送。返回是否确认发出去了。

    清空这步不能省：上一句如果是草稿（或者重问时残留），再 insertText 会拼成
    "旧话+新话"一起发出去（2026-11-06 评审指出）。
    """
    # React 会重渲染，缓存的 ElementHandle 随时失效（实测报 Element is not attached）。
    # 所以每次都用选择器现查现点，不保存句柄。
    await pg.click(BOX_SEL, timeout=10000)
    await asyncio.sleep(0.3)
    try:
        await pg.keyboard.press("Control+A")
        await pg.keyboard.press("Backspace")
    except Exception:
        pass
    # 长文本用 insertText 一次性写进去，比逐字敲快且不会被 IME 打断
    try:
        await pg.evaluate(
            """(t) => {
                const el = document.querySelector('div.ProseMirror');
                el.focus();
                document.execCommand('insertText', false, t);
            }""", text)
    except Exception:
        await pg.keyboard.type(text)
    await asyncio.sleep(0.5)

    btn = await pg.query_selector(_SEND_SEL)
    if btn:
        try:
            await btn.click()
        except Exception:
            await pg.keyboard.press("Enter")
    else:
        await pg.keyboard.press("Enter")

    # 确认真的发出去了：输入框应该在几秒内清空。没清空 = 没发出去，
    # 别在这儿干等 300 秒（用户会以为软件卡了）。
    for _ in range(8):
        await asyncio.sleep(0.5)
        if not await _composer_text(pg):
            if log:
                log("已发出（%d 字）" % len(text))
            return True
    if log:
        log("!! 输入框里还有字：消息没发出去")
    return False


def clean_reply(raw: str, prompt: str = "") -> str:
    """从 main.innerText 里切出「最后一条回复」。

    顺序：先砍掉最后一个 "ChatGPT 说：" 之前的所有内容（那是提问和更早的对话），
    再从第一个页脚标记起全部丢掉。都不命中就原样返回 —— 宁可多给，不要给空。
    """
    s = (raw or "").strip()
    if not s:
        return ""

    pos = -1
    for mark in _HEAD_MARKS:
        i = s.rfind(mark)
        if i > pos:
            pos = i + len(mark)
    if pos > 0:
        s = s[pos:].lstrip("\n ")

    cut = len(s)
    for mark in _TAIL_MARKS:
        i = s.find(mark)
        if 0 <= i < cut:
            cut = i
    s = s[:cut].strip()

    # 有些页面会把刚才的提问也回显一遍，开头重复就去掉
    if prompt:
        p = prompt.strip()
        if p and s.startswith(p):
            s = s[len(p):].lstrip("\n ")
    return s.strip()


async def ask(pg, text: str, url: str | None = None, timeout: float = 300.0,
              log=None) -> dict:
    """在一个页面上完成「发一句 → 等完 → 取回复」。

    返回 {ok, text, url, seconds, error}。失败不抛异常，让上层统一处理。
    """
    t0 = asyncio.get_event_loop().time()
    try:
        # 2026-11-05 踩的坑：这里原来写的是 `want not in pg.url` —— 子串匹配，
        # 而 "https://chatgpt.com" 恰好是 "https://chatgpt.com/c/xxx" 的前缀，
        # 于是**永远不跳转**，一直在上一轮出图的会话里发消息：读回来的是整段历史，
        # 还顺手把出图的上下文搅了。现在改成精确判断。
        cur = (pg.url or "").rstrip("/")
        if url:
            if cur != url.rstrip("/"):
                await pg.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(2.5)
        elif "/c/" in cur or cur != "https://chatgpt.com":
            # 没指定会话 = 要一个新会话。当前若停在某个会话里，必须回首页。
            await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2.5)

        # 页面可能还在渲染（实测：URL 已经对了，但 ProseMirror 还没挂上，
        # 0.2 秒就去点就报"找不到输入框"）。等它出现，最多 15 秒。
        for _ in range(15):
            if await pg.query_selector(BOX_SEL):
                break
            await asyncio.sleep(1)
        else:
            return {"ok": False, "url": pg.url, "text": "", "seconds":
                    round(asyncio.get_event_loop().time() - t0, 1),
                    "error": "NO_COMPOSER：页面上没有输入框（没登录 / 没加载完 / 被验证挡住）"}

        # 本地草稿会话（URL 里带 local-chatgpt）是异常态：消息其实没进服务器，
        # 出图流程也踩过同一个坑。这里先刷新回正常页面再发。
        if "local-chatgpt" in (pg.url or ""):
            if log:
                log("页面停在本地草稿会话上，先刷新")
            await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # 等页面空闲（上一轮可能还在生成）
        for _ in range(30):
            if not await _busy(pg):
                break
            await asyncio.sleep(2)

        # ★ 发送前先记一份快照。
        # 没有它就无法判断"读到的这段话是不是本轮新写的"：
        # `--chat` 续聊时，本轮还没渲染出来，clean_reply 会取到**上一轮**的回复，
        # 然后 ok=true 地返回给你——静默错答案，比报错更坏（2026-11-06）。
        before_raw = await _main_text(pg)
        before_heads = _head_count(before_raw)
        before_out = clean_reply(before_raw, text)

        if not await _type_and_send(pg, text, log=log):
            return {"ok": False, "text": "", "url": pg.url,
                    "seconds": round(asyncio.get_event_loop().time() - t0, 1),
                    "error": "SEND_FAILED: 消息没发出去（输入框里还留着字）"}

        # 一直读到超时，见到"本轮新回复"就继续等它稳定；不再靠 stop 按钮判断开始。
        # 2026-11-06 实测：stop 一直不出现时空转 30 秒，然后只给 12 秒读正文，
        # 回复 50 秒才出来 → 假失败（页面里明明有答案）。
        deadline = t0 + timeout
        last_out, last_len, stable, saw_any = "", 0, 0, False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.5)
            busy = await _busy(pg)
            raw = await _main_text(pg)
            out = clean_reply(raw, text)
            heads = _head_count(raw)
            fresh = (heads > before_heads) or bool(out and out != before_out)
            if not fresh:
                continue
            saw_any = True
            if not out:
                continue            # 刚出现「ChatGPT 说：」，正文还没渲染，接着等
            # 稳定判据用"正文长度 + 文本都没变"：只看文本相等会把"同样的字又追加了一段"
            # 误判成结束（流式长回答被截断，2026-11-06 评审）。
            if out == last_out and len(raw) == last_len:
                stable += 1
            else:
                stable, last_out, last_len = 0, out, len(raw)
            # 连续两次读到同一段（长度也不变）、且不在生成中 = 写完了
            if stable >= 1 and not busy:
                return {"ok": True, "text": out, "url": pg.url,
                        "seconds": round(asyncio.get_event_loop().time() - t0, 1),
                        "error": ""}

        # 超时：有本轮的新正文就带回去（标注可能没写完），总比丢掉强
        out = last_out or clean_reply(await _main_text(pg), text)
        if saw_any and out and out != before_out:
            return {"ok": True, "text": out, "url": pg.url, "partial": True,
                    "seconds": round(asyncio.get_event_loop().time() - t0, 1),
                    "error": ""}
        return {"ok": False, "text": "", "url": pg.url,
                "seconds": round(asyncio.get_event_loop().time() - t0, 1),
                "error": "TIMEOUT" if saw_any else "EMPTY_REPLY"}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "url": getattr(pg, "url", ""),
                "seconds": round(asyncio.get_event_loop().time() - t0, 1), "text": ""}
