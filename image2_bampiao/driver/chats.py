"""会话管理：列出 / 删除 ChatGPT 侧栏里的会话。

**为什么不点 UI**：侧栏是虚拟列表，滚动加载、行号会变，点错就是删错。
页面里有现成接口，用 `fetch` 走 Bearer token 一步到位：

    GET    /backend-api/conversations?offset=0&limit=28&order=updated
    DELETE /backend-api/conversation/<id>
    PATCH  /backend-api/conversation/<id>   （出图时改标题用的同一个）

token 从 `/api/auth/session` 取——只带 cookie 会 401（改标题时踩过）。

这个模块只被 CLI/面板调用，**不碰出图流程**：删会话失败不该影响任何一单。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .. import paths


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _page(port: int):
    """连上已经开着的浏览器，找（或开）一个 chatgpt 页面。"""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    for pg in ctx.pages:
        if "chatgpt.com" in (pg.url or ""):
            return pw, pg
    pg = await ctx.new_page()
    await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
    return pw, pg


_LIST_JS = """async ([offset, limit]) => {
  let tok = '';
  try { tok = (await (await fetch('/api/auth/session')).json()).accessToken || ''; } catch (e) {}
  const h = {};
  if (tok) h['Authorization'] = 'Bearer ' + tok;
  const r = await fetch('/backend-api/conversations?offset=' + offset +
                        '&limit=' + limit + '&order=updated', {headers: h});
  if (!r.ok) return {ok: false, status: r.status};
  const d = await r.json();
  return {ok: true, total: d.total, items: (d.items || []).map(x => ({
    id: x.id, title: x.title, create_time: x.create_time, update_time: x.update_time
  }))};
}"""

_DEL_JS = """async (id) => {
  let tok = '';
  try { tok = (await (await fetch('/api/auth/session')).json()).accessToken || ''; } catch (e) {}
  const h = {'Content-Type': 'application/json'};
  if (tok) h['Authorization'] = 'Bearer ' + tok;
  // 注意：这个接口**不接受 DELETE**（实测 405）。侧栏里点"删除"发的是
  // PATCH {is_visible:false}，效果是从列表里移掉——语义等于用户看到的"删除"。
  const r = await fetch('/backend-api/conversation/' + id, {
    method: 'PATCH', headers: h, body: JSON.stringify({is_visible: false})
  });
  return {status: r.status, ok: r.ok};
}"""


async def list_chats(port: int, limit: int = 50) -> dict:
    pw = None
    try:
        pw, pg = await _page(port)
        out, total = [], None
        # 接口一次最多给 28 条，要多就翻页；翻页期间列表可能变，出错就用手上这些
        while len(out) < limit:
            got = await pg.evaluate(_LIST_JS, [len(out), min(28, limit - len(out))])
            if not got.get("ok"):
                if not out:
                    return {"ok": False, "why": "接口返回 %s（可能是登录态失效）"
                            % got.get("status")}
                break
            total = got.get("total")
            items = got.get("items") or []
            if not items:
                break
            out.extend(items)
        reg = load_registry()
        for x in out:
            x["ours"] = x.get("id") in reg
        return {"ok": True, "total": total if total is not None else len(out),
                "items": out[:limit], "tracked": len(reg)}
    except Exception as e:
        return {"ok": False, "why": "%s: %s" % (type(e).__name__, e)}
    finally:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


async def delete_chats(port: int, ids: list[str]) -> dict:
    pw = None
    done, failed = [], []
    try:
        pw, pg = await _page(port)
        for cid in ids:
            try:
                r = await pg.evaluate(_DEL_JS, cid)
                if r.get("ok"):
                    done.append(cid)
                    _log("  已删 %s" % cid)
                else:
                    failed.append({"id": cid, "status": r.get("status")})
                    _log("  失败 %s（HTTP %s）" % (cid, r.get("status")))
            except Exception as e:
                failed.append({"id": cid, "why": str(e)[:120]})
        return {"ok": True, "deleted": done, "failed": failed}
    except Exception as e:
        return {"ok": False, "why": "%s: %s" % (type(e).__name__, e),
                "deleted": done, "failed": failed}
    finally:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


def _norm(ts) -> float:
    """把接口的时间统一成 unix 秒。

    实测这个接口给的是 **ISO 字符串**（`2026-10-29T06:33:16.697926Z`），
    不是数字；老版本给过秒/毫秒。三种都认。
    """
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        v = float(ts)
        return v / 1000.0 if v > 1e12 else v
    s = str(ts).strip()
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        pass
    try:
        return float(s)
    except ValueError:
        return 0.0


def _when(item: dict) -> float:
    """更新时间；接口有时不给，退回创建时间（否则排序/筛选全废）。"""
    return _norm(item.get("update_time")) or _norm(item.get("create_time"))


def _registry_path() -> str:
    base = os.environ.get("BAMPIAO_DATA") or str(paths.data_dir())
    return os.path.join(base, "state", "conversations.jsonl")


def load_registry() -> dict:
    """半瓢自己开过的会话：id → 记录。

    为什么要有这份名单：这些会话的标题是模型自动起的（「出图要求」「生成单张图」），
    跟用户自己聊的「分析文章论文选题」长得一模一样，**靠标题猜必然误删**。
    所以出图时就把 id 记下来，删的时候只认名单。
    """
    out = {}
    try:
        with open(_registry_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                cid = d.get("id")
                if cid:
                    out[cid] = d
    except OSError:
        pass
    return out


def is_ours(item: dict) -> bool:
    """这条会话是不是半瓢起的 —— 只看生成时记下的名单。

    早期版本试过"标题以【开头"来认，实测不成立：出图时改标题那步经常没生效，
    这些会话的标题其实是「出图要求」「生成单张图」，和用户自己聊的主题无法区分。
    靠标题猜会误删用户几个月的对话，所以改成出图时记账。
    """
    if "ours" in item:
        return bool(item.get("ours"))
    return False


def filter_items(items: list[dict], keep: int = 0, older_days: float = 0,
                 ids: list[str] | None = None, only_ours: bool = True) -> list[dict]:
    """挑要删的。

    - 明确给了 ids 就只删这些（仍然要求是半瓢的，除非 only_ours=False）
    - 否则先只留半瓢自己起的，再按 keep（保留最近 N 条）/ older_days（只删 N 天前）
    - 两条规则都不给时**什么都不删**——宁可不删，也不能误删
    """
    import time

    pool = [x for x in items if (is_ours(x) or not only_ours)]

    if ids:
        want = set(ids)
        return [x for x in pool if x.get("id") in want]

    if not keep and not older_days:
        return []

    picked = list(pool)
    if keep and keep > 0:
        picked = picked[keep:]
    if older_days and older_days > 0:
        cutoff = time.time() - older_days * 86400
        picked = [x for x in picked if _when(x) < cutoff]
    return picked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="半瓢会话管理")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--op", choices=["list", "rm"], required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--json-out", help="结果写到这个文件（GUI 没有 stdout）")
    a = ap.parse_args(argv)

    if a.op == "list":
        res = asyncio.run(list_chats(a.port, a.limit))
    else:
        res = asyncio.run(delete_chats(a.port, a.ids))

    text = json.dumps(res, ensure_ascii=False)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
