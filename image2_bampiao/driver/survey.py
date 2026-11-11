"""关掉 ChatGPT 的「你更喜欢哪一张图像？」偏好问卷。

2026-10-25 首次出现：ChatGPT 网页版会随机弹一个二选一偏好收集（免费版账号尤其常见），
两个候选是**空灰块**，DOM 里没有 <img>，取图逻辑的 blob_srcs 永远扫不到。
当时的真机结论是「不挡出图、点跳过也关不掉」，于是撤掉了防护，只留下
cdp_run.dismiss_survey() 这个调用点和「实现在 survey.py」的注释——
**但本模块从未落地**，ImportError 被静默吞掉，功能假存在了 17 天。
2026-11-11 复现（R444 H4-xiaobao）：问卷确实让这一项白等 273 秒、
整单被标 TIMEOUT_UNCERTAIN、后面 3 张连带跳过。

检测依据用「投票按钮是否存在」，**不看问句文字**：问卷关掉后问句会留在
会话记录里，只看文字会一直误判（10-25 已踩过）。
点击是尽力而为（10-25 实测「跳过」点不动），不把成功当作前提；
真正的止血靠 cdp_run 里的早收手 + 明确的 SURVEY_BLOCKED 错误码。
"""

from __future__ import annotations

import asyncio

# 投票按钮文案（中英文兜底；DOM 结构会变，所以用文本匹配而不是选择器）
_VOTE_WORDS = (
    "第 1 张图像更好", "第 2 张图像更好",
    "第1张图像更好", "第2张图像更好",
    "第一张图像更好", "第二张图像更好",
    "I prefer the first", "I prefer the second",
    "first image is better", "second image is better",
)
_SKIP_WORDS = ("跳过", "略过", "Skip", "skip", "Skip for now")

_JS_DETECT = """() => {
  const voteWords = %s;
  const skipWords = %s;
  const nodes = [...document.querySelectorAll('button, [role="button"]')];
  const textOf = (e) => (e.innerText || e.textContent || '').trim();
  const votes = nodes.filter(e => voteWords.some(w => textOf(e).includes(w)));
  const skip = nodes.find(e => skipWords.some(w => textOf(e) === w));
  const tx = document.body ? (document.body.innerText || '') : '';
  return {
    present: votes.length >= 2,
    voteCount: votes.length,
    hasSkip: !!skip,
    questionSeen: tx.includes('你更喜欢哪一张图像') ||
                  tx.includes('你更喜欢哪张') ||
                  /which image do you prefer/i.test(tx),
    // 只回结构信息，不回账号、图片或会话内容
  };
}""" % (
    __import__("json").dumps(_VOTE_WORDS, ensure_ascii=False),
    __import__("json").dumps(_SKIP_WORDS, ensure_ascii=False),
)

_JS_CLICK = """(mode) => {
  const voteWords = %s;
  const skipWords = %s;
  const nodes = [...document.querySelectorAll('button, [role="button"]')];
  const textOf = (e) => (e.innerText || e.textContent || '').trim();
  let target = null;
  if (mode === 'skip') {
    target = nodes.find(e => skipWords.some(w => textOf(e) === w));
  } else if (mode === 'vote') {
    target = nodes.find(e => voteWords.some(w => textOf(e).includes(w)));
  }
  if (!target) return {clicked: false, why: 'not-found'};
  try {
    target.click();
    return {clicked: true, label: textOf(target).slice(0, 40)};
  } catch (e) {
    return {clicked: false, why: String(e).slice(0, 80)};
  }
}""" % (
    __import__("json").dumps(_VOTE_WORDS, ensure_ascii=False),
    __import__("json").dumps(_SKIP_WORDS, ensure_ascii=False),
)


async def detect(pg) -> dict:
    """问卷此刻在不在。只回结构信息，失败一律当作「不在」。"""
    try:
        r = await pg.evaluate(_JS_DETECT)
        return r if isinstance(r, dict) else {"present": False}
    except Exception:
        return {"present": False}


async def dismiss(pg, tag: str = "", log=None) -> dict:
    """尽力点掉问卷：先「跳过」，不行再点第一个投票按钮。

    不保证成功（10-25 实测跳过按钮点不动），返回值供调用方记日志。
    绝不抛异常打断出图主链路。
    """
    info = await detect(pg)
    if not info.get("present"):
        return {"present": False, "clicked": False}

    out = {"present": True, "voteCount": info.get("voteCount", 0),
           "hasSkip": info.get("hasSkip", False), "clicked": False}
    for mode in ("skip", "vote"):
        try:
            r = await pg.evaluate(_JS_CLICK, mode)
        except Exception as e:  # noqa: BLE001
            r = {"clicked": False, "why": str(e)[:80]}
        if log:
            log("    .. %s 偏好问卷点「%s」：%s" % (
                tag, mode, (r or {}).get("label") or (r or {}).get("why") or "无反应"))
        if (r or {}).get("clicked"):
            out["clicked"] = True
            out["how"] = mode
            out["label"] = (r or {}).get("label", "")
            # 给页面一点时间把后续图渲染出来，不在这里死等
            await asyncio.sleep(1.0)
            break
    return out
