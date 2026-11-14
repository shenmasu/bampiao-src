"""账号备份：把"登录态"从一台机器搬到另一台。

## 为什么不能复制 profile

Chrome 的 cookie 库在 Windows 上用 **App-Bound Encryption / DPAPI** 加密，密钥绑定
当前用户甚至当前路径。把 ``profiles/acc1`` 整个拷到另一台机器，Chrome 打不开那些
cookie —— 看起来"文件都在"，实际上等于没登录。所以备份格式**只能是明文 cookie
+ 口令加密**。

## 流程（已按设计定死）

导出：连上已登录的浏览器 → 校验身份（会话接口）→ 取全部 cookie → 口令加密落盘
导入：解密 → 起一个新 profile → 注入 cookie → 重新加载 → **校验身份是否同一个人**
     （只看 HTTP 200 不够：没登录也 200）

## 文件长什么样

```json
{"v":1, "kind":"bampiao-account", "algo":"AESGCM-SCRYPT",
 "kdf":{"n":16384,"r":8,"p":1,"salt":"base64"},
 "nonce":"base64", "ct":"base64",
 "meta":{"account":"acc1","origin":"https://chatgpt.com","user":"<短 id>",
         "exported_at":1793250000,"cookies":37}}
```

``meta`` 是明文的——它不含凭据，只用来在导入前告诉人"这是谁的备份、什么时候导的"。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

from . import config as cfg_mod
from .core.store import Store
from .driver import browser

FILE_KIND = "bampiao-account"
ALGO = "AESGCM-SCRYPT"
# scrypt 参数：交互式场景下 ~0.1 秒，够挡住离线爆破的常用档位
SCRYPT = {"n": 16384, "r": 8, "p": 1}
ORIGIN = "https://chatgpt.com"

_JS_SESSION = """async () => {
  try {
    const r = await fetch('/api/auth/session', {credentials: 'include'});
    if (!r.ok) return {ok: false, status: r.status};
    const j = await r.json();
    const u = (j && j.user) || null;
    const a = (j && j.account) || null;
    return {ok: !!u, id: u ? String(u.id).slice(0, 12) : null,
            plan: (a && a.planType) || (u && u.planType) || null};
  } catch (e) { return {ok: false, err: String(e).slice(0, 80)}; }
}"""


class BackupError(RuntimeError):
    pass


# ------------------------------------------------------------------ 加密

def _derive(password: str, salt: bytes) -> bytes:
    import hashlib

    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"], dklen=32)


def seal(payload: dict, password: str, meta: dict) -> str:
    """把 payload 加密成一段可读的 JSON 文本。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not password:
        raise BackupError("备份必须设口令：没有口令的文件等于明文保存登录态")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ct = AESGCM(_derive(password, salt)).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")   # noqa: E731
    return json.dumps({
        "v": 1, "kind": FILE_KIND, "algo": ALGO,
        "kdf": {**SCRYPT, "salt": b64(salt)},
        "nonce": b64(nonce), "ct": b64(ct),
        "meta": meta,
    }, ensure_ascii=False, indent=1)


def unseal(text: str, password: str) -> tuple[dict, dict]:
    """解开备份。返回 (payload, meta)。口令错会抛 BackupError（不是 InvalidTag 那种原始异常）。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        doc = json.loads(text)
    except ValueError:
        raise BackupError("这不是半瓢的备份文件（不是 JSON）") from None
    if doc.get("kind") != FILE_KIND:
        raise BackupError("这不是半瓢的账号备份")
    if doc.get("algo") != ALGO:
        raise BackupError("不认识的加密方式：%s" % doc.get("algo"))
    try:
        salt = base64.b64decode(doc["kdf"]["salt"])
        nonce = base64.b64decode(doc["nonce"])
        ct = base64.b64decode(doc["ct"])
    except (KeyError, ValueError):
        raise BackupError("备份文件损坏（缺字段或 base64 解不开）") from None
    try:
        raw = AESGCM(_derive(password, salt)).decrypt(nonce, ct, None)
    except InvalidTag:
        # 口令错和文件被改过在密文层无法区分，统一按口令错提示
        raise BackupError("口令不对，或者文件被改过") from None
    try:
        return json.loads(raw.decode("utf-8")), (doc.get("meta") or {})
    except ValueError:
        raise BackupError("备份内容损坏") from None


# ------------------------------------------------------------------ 浏览器侧

async def _session_of(page) -> dict:
    try:
        return await page.evaluate(_JS_SESSION)
    except Exception as e:
        return {"ok": False, "err": str(e)[:100]}


async def _export_async(port: int) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        ctx = b.contexts[0] if b.contexts else None
        if ctx is None:
            raise BackupError("浏览器里没有可用的上下文")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if not page.url.startswith("http"):
            await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=45000)
        sess = await _session_of(page)
        if not sess.get("ok"):
            raise BackupError("这个账号现在是未登录状态，没有可备份的登录态")
        cookies = await ctx.cookies()
        keep = [c for c in cookies
                if "chatgpt.com" in (c.get("domain") or "") or
                "openai.com" in (c.get("domain") or "")]
        return {"cookies": keep, "user": sess.get("id"), "plan": sess.get("plan"),
                "origin": ORIGIN, "all": len(cookies)}


async def _import_async(port: int, cookies: list[dict], want_user: str | None) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        ctx = b.contexts[0] if b.contexts else None
        if ctx is None:
            raise BackupError("浏览器里没有可用的上下文")
        # 先清掉同名域下的旧 cookie：否则会同时存在两条 sessionKey，
        # 结果是"有时候是 A 有时候是 B"，排查起来要命。
        try:
            await ctx.clear_cookies()
        except Exception:
            pass
        await ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=60000)
        sess = {}
        for _ in range(10):
            sess = await _session_of(page)
            if sess.get("ok"):
                break
            await page.wait_for_timeout(1000)
        return {"session": sess, "want": want_user}


# ------------------------------------------------------------------ 对外

def export(cfg: cfg_mod.Config, acc: cfg_mod.Account, out: Path, password: str) -> dict:
    """导出这个账号的登录态。浏览器会自动起（隐藏），导出完不关。"""
    browser.ensure(cfg, acc, visibility="hide")
    try:
        r = asyncio.run(_export_async(acc.port))
    except BackupError:
        raise
    except Exception as e:
        raise BackupError("连不上浏览器：%s" % str(e)[:160]) from None

    meta = {"account": acc.id, "name": acc.name, "origin": r["origin"],
            "user": r.get("user"), "plan": r.get("plan"),
            "exported_at": round(time.time()), "cookies": len(r["cookies"])}
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(seal({"cookies": r["cookies"]}, password, meta), encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass

    # 记一笔"这个账号有登录态"，面板上就能显示已登录
    st = Store(cfg).account(acc.id)
    st.login = "ok"
    st.login_checked_at = time.time()
    st.login_user = str(r.get("user") or "")
    Store(cfg).save_account(st)
    return {"path": str(out.resolve()), "meta": meta}


def restore(cfg: cfg_mod.Config, acc: cfg_mod.Account, src: Path, password: str,
            *, force: bool = False) -> dict:
    """把备份注入这个账号的 profile。**身份不一致时拒绝覆盖**。"""
    text = Path(src).expanduser().read_text(encoding="utf-8")
    payload, meta = unseal(text, password)
    cookies = payload.get("cookies") or []
    if not cookies:
        raise BackupError("备份里没有 cookie")

    # 导入前先看现在是谁：已经登录着别人，就不能默默顶掉
    browser.ensure(cfg, acc, visibility="hide")
    cur = asyncio.run(_probe_session(acc.port))
    if cur.get("ok") and not force:
        want = str(meta.get("user") or "")
        if want and cur.get("id") and cur["id"] != want:
            raise BackupError(
                "这个账号现在登录的是另一个人（%s），备份是 %s。确认要覆盖就加 --force"
                % (cur["id"], want))

    try:
        r = asyncio.run(_import_async(acc.port, cookies, meta.get("user")))
    except Exception as e:
        raise BackupError("注入 cookie 失败：%s" % str(e)[:160]) from None

    sess = r.get("session") or {}
    if not sess.get("ok"):
        raise BackupError("cookie 注进去了，但会话仍然无效（备份过期了？）")
    want = str(meta.get("user") or "")
    got = str(sess.get("id") or "")
    if want and got and want != got:
        raise BackupError("恢复出来的是另一个账号（期望 %s，实际 %s）" % (want, got))

    st = Store(cfg).account(acc.id)
    st.login = "ok"
    st.login_checked_at = time.time()
    st.login_user = got
    Store(cfg).save_account(st)
    return {"account": acc.id, "user": got, "plan": sess.get("plan"),
            "from": str(Path(src).resolve())}


async def _probe_session(port: int) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        ctx = b.contexts[0] if b.contexts else None
        if ctx is None:
            return {}
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await _session_of(page)
