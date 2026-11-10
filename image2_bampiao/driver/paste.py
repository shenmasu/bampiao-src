"""把提示词放进 ChatGPT 输入框，并确认它真的进去了。

单独一个模块，不为什么架构美感——`driver/cdp_run.py` 是"导入即执行"的
（冻结版里 `from .driver import cdp_run` 就是启动驱动的方式），
逻辑写在那里就没法单独测，只能端到端赌。

两次实测（同一天，用户在 Mac Intel 上提交的反馈包）：
  15:28:11 发完开场白 → 15:30:11 开场没确认（URL 还是 chatgpt.com/）
  → 15:30:15 起连着三次「置入 0/108」，一个字都进不去。
  当时验收阈值是 40%，而且**没人检查输入框能不能写**：
  页面还在生成时编辑器是只读的，往里插就是 0，然后被当成"图没画出来"报给用户。

所以这里多了两件事：先等编辑器"可写"，再谈粘不粘得进去；三种选择器兜住 UI 改版。
"""

from __future__ import annotations

from typing import Callable

_log: Callable[[str], None] = lambda _msg: None


def set_logger(fn: Callable[[str], None]) -> None:
    """让 cdp_run 把自己的 log 挂进来（这个模块不自己写日志）。"""
    global _log
    _log = fn


class PasteFailed(Exception):
    """提示词没完整进输入框。

    单独一类：这**不是出图失败**，重试或把提示词缩短就能救，
    不能让上层报成"渲染失败"（那会让人去改提示词内容，白折腾）。
    """


def _norm_len(s: str) -> int:
    """去掉所有空白再数长度：ProseMirror 里的换行/空格和原文不完全一致，不能直接比。"""
    return len("".join((s or "").split()))


# ChatGPT 换过一次编辑器结构，只认一种选择器，就是等着某天全线 0 置入
COMPOSER_JS = """() => {
  const sels = ['div.ProseMirror[contenteditable="true"]',
                'div[contenteditable="true"]',
                'textarea#prompt-textarea',
                'textarea'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (!el) continue;
    const ce = el.getAttribute('contenteditable');
    return {sel: s, tag: (el.tagName||'').toLowerCase(),
            editable: ce === 'true' || el.tagName === 'TEXTAREA',
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
            readonly: !!el.readOnly};
  }
  return null;
}"""

_BUSY_JS = """() => {
  const stop = document.querySelector('button[aria-label="停止"]')
            || document.querySelector('[data-testid="stop-button"]');
  return !!stop;
}"""


def describe_now(c: dict | None) -> str:
    if not c:
        return "找不到输入框"
    return "editable=%s disabled=%s readonly=%s" % (
        c.get("editable"), c.get("disabled"), c.get("readonly"))


async def find_composer(pg) -> dict | None:
    try:
        return await pg.evaluate(COMPOSER_JS)
    except Exception:
        return None


async def describe(pg) -> str:
    """给失败日志用的一行现场：编辑器在不在、能不能写、页面忙不忙、停在哪一页。"""
    try:
        c = await find_composer(pg)
        busy = bool(await pg.evaluate(_BUSY_JS))
        url = getattr(pg, "url", "") or ""
        if not c:
            return "找不到输入框（页面可能停在错误页/没加载完）url=%s" % url
        return ("%s editable=%s disabled=%s readonly=%s 生成中=%s url=%s"
                % (c.get("sel"), c.get("editable"), c.get("disabled"),
                   c.get("readonly"), busy, url))
    except Exception as e:  # noqa: BLE001
        return "现场信息取不到（%s）" % str(e)[:60]


async def wait_editable(pg, timeout: float = 30.0) -> dict:
    """等编辑器变成"能写"的状态。

    页面还在生成时编辑器是只读的，这时候往里插就是 0 字符——2026-10-30 那次
    连试三次全 0 就是这个原因（开场白发了但没确认，页面还在生成，
    我们就急着去粘任务提示词，插进去 0 个字，还报成"渲染失败"）。
    """
    import asyncio

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last = None
    while True:
        last = await find_composer(pg)
        if last and last.get("editable") and not last.get("disabled") and not last.get("readonly"):
            return last
        if loop.time() >= deadline:
            break
        await pg.wait_for_timeout(500)
    raise PasteFailed("输入框不可写（%s）" % describe_now(last))


async def _focus_editor(pg) -> None:
    """真点一下编辑区。ProseMirror 有时不认 el.focus()，但认真实点击。"""
    try:
        await pg.locator('div.ProseMirror[contenteditable="true"], textarea').first.click(timeout=3000)
    except Exception:
        try:
            await pg.evaluate("""() => {
                const el=document.querySelector('div.ProseMirror[contenteditable="true"]')
                      || document.querySelector('textarea');
                if (el) el.focus();
            }""")
        except Exception:
            pass


async def _clear_composer(pg, comp: dict | None = None) -> None:
    await _focus_editor(pg)
    try:
        if comp and comp.get("tag") == "textarea":
            await pg.evaluate("""() => {
                const el=document.querySelector('textarea');
                if (!el) return;
                el.value='';
                el.dispatchEvent(new Event('input', {bubbles:true}));
            }""")
        else:
            await pg.evaluate("""() => { document.execCommand('selectAll'); document.execCommand('delete'); }""")
    except Exception:
        pass
    await pg.wait_for_timeout(150)


async def composer_text(pg) -> str:
    try:
        t = await pg.evaluate("""() => {
            const el=document.querySelector('div.ProseMirror[contenteditable="true"]')
                  || document.querySelector('textarea');
            if (!el) return '';
            return el.tagName === 'TEXTAREA' ? (el.value||'') : (el.innerText||'');
        }""")
    except Exception:
        t = ""
    return t or ""


async def _composer_norm_len(pg) -> int:
    return _norm_len(await composer_text(pg))


async def _insert_js(pg, text: str, comp: dict) -> None:
    if comp.get("tag") == "textarea":
        await pg.evaluate("""(t) => {
            const el=document.querySelector('textarea');
            if (!el) return;
            el.focus();
            el.value = t;
            el.dispatchEvent(new Event('input', {bubbles:true}));
        }""", text)
    else:
        await pg.evaluate("""(t) => {
            const el=document.querySelector('div.ProseMirror[contenteditable="true"]')
                  || document.querySelector('div[contenteditable="true"]');
            if (!el) return;
            el.focus();
            document.execCommand('insertText', false, t);
        }""", text)


async def paste(pg, text: str, tag: str = "", wait_s: float = 30.0) -> int:
    """把提示词放进输入框，成功返回置入的字符数（归一化后），放不进去抛 PasteFailed。

    三级兜底：JS 直接写 → CDP Input.insertText → 真键盘分段敲。
    验收是"归一化（去空白）后 ≥95%"——以前只要 40% 就算放好了，
    结果发出去的是半句话，ChatGPT 不画图，还报成"渲染失败"。
    """
    want = _norm_len(text)
    if not want:
        return 0

    comp = await wait_editable(pg, wait_s)  # 先确认输入框能写，再谈怎么写
    last = 0
    for how in ("js", "insert_text", "typing"):
        await _clear_composer(pg, comp)
        try:
            if how == "js":
                await _insert_js(pg, text, comp)
            elif how == "insert_text":
                await _focus_editor(pg)
                await pg.keyboard.insert_text(text)
            else:
                # 真键盘分段敲：慢，但最贴"人打的"，ProseMirror 不会吞
                await _focus_editor(pg)
                for i in range(0, len(text), 200):
                    await pg.keyboard.type(text[i:i + 200])
                    await pg.wait_for_timeout(60)
        except Exception as e:
            _log("    .. %s 粘贴方式 %s 出错：%s" % (tag, how, str(e)[:60]))
        await pg.wait_for_timeout(900)
        got = await _composer_norm_len(pg)
        if got >= want * 0.95:
            if how != "js":
                _log("    .. %s 提示词用 %s 才放进去（%d/%d）" % (tag, how, got, want))
            return got
        last = got
        _log("    .. %s 粘贴方式 %s 只进去 %d/%d" % (tag, how, got, want))
    raise PasteFailed("%d/%d（%s）" % (last, want, await describe(pg)))
