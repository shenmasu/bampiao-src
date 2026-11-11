"""登录流程：把窗口叫出来，等用户登好，再藏回去。

为什么要单独一个模块：
- 登录是唯一**必须让人看见窗口**的动作，不能走后端的隐藏流程；
- 判断"登好了"不能只看 HTTP 200，要看页面上真的有输入框 / 头像；
- 用户可能中途放弃，必须有超时和"保持窗口打开"的选项。

检测逻辑与 driver/cdp_run.py 的 --probe 保持一致，避免两处判据不同。
"""

from __future__ import annotations

import asyncio
import time

from . import config as cfg_mod
from . import platform as plat
from .driver import browser

# 判据：有输入框（能直接对话）+ 有头像/账号菜单（确实登录了）
_JS_STATE = """async () => {
  let user = null, expires = null;
  try {
    const r = await fetch('/api/auth/session', {credentials: 'include'});
    if (r.ok) { const j = await r.json(); user = (j && j.user) || null; expires = (j && j.expires) || null; }
  } catch (e) {}
  const composer = !!document.querySelector('div.ProseMirror[contenteditable="true"]');
  return {url: location.href, title: document.title, composer,
          logged_in: !!user,
          user_id: user ? String(user.id).slice(0, 12) : null,
          email: user ? (user.email || null) : null,
          plan: (user && user.planType) || null,
          expires: expires};
}"""


async def _wait_login(port: int, deadline: float, poll: float = 2.0,
                      stop=None) -> tuple[bool, dict]:
    """连上 CDP 反复看页面，直到登录或超时。返回 (是否登录, 最后一次状态)。

    stop() 返回原因字符串时立刻收手：用户点了「取消」，或者直接把窗口关了。
    以前只认超时，用户关掉窗口后后台还傻等 5 分钟，面板一直写着"等你登录"。
    """
    from playwright.async_api import async_playwright

    last: dict = {}
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        ctx = b.contexts[0] if b.contexts else await b.new_context()
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        while time.time() < deadline:
            why = stop() if stop else None
            if why:
                return False, {"stopped": why}
            try:
                if not pg.url.startswith("http"):
                    await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                                  timeout=45000)
                last = await pg.evaluate(_JS_STATE)
                if last.get("logged_in") and last.get("composer"):
                    return True, last
            except Exception as e:  # 页面在跳转/刷新时会短暂失败，不该中断整个流程
                last = {"error": str(e)[:120]}
            await asyncio.sleep(poll)
    return False, last


async def _peek_state(port: int) -> dict:
    """只连已经开着的 CDP 读一次登录态；连不上/不是 ChatGPT 页就返回空。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        if not b.contexts or not b.contexts[0].pages:
            return {}
        pg = b.contexts[0].pages[0]
        if not pg.url.startswith("https://chatgpt.com"):
            return {}
        return await pg.evaluate(_JS_STATE)


def read_state(port: int, timeout: float = 4.0) -> dict:
    """只读探一次登录态，给面板补邮箱用。

    **绝不调用 browser.ensure()**：账号页刷新不能顺手把 13 个浏览器都拉起来。
    浏览器没开 / 页面还没到 chatgpt.com / CDP 连不上，一律安静返回 {}。
    """
    try:
        return asyncio.run(asyncio.wait_for(_peek_state(port), timeout))
    except Exception:
        return {}


def phase_path(cfg: cfg_mod.Config, account_id: str):
    d = cfg.state_dir / "login"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%s.json" % account_id)


def write_phase(cfg: cfg_mod.Config, account_id: str, phase: str, **extra) -> None:
    """登录进行到哪一步，写给面板看（面板和登录进程不是同一个进程）。

    phase: waiting（窗口开着等人登）/ ok / failed / cancelled
    """
    import json
    import os

    data = {"phase": phase, "at": time.time(), "pid": os.getpid(), **extra}
    p = phase_path(cfg, account_id)
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_phase(cfg: cfg_mod.Config, account_id: str) -> dict:
    """读登录进度。登录进程已经死了却还写着 waiting，就当它失败了。"""
    import json

    p = phase_path(cfg, account_id)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if d.get("phase") == "waiting" and d.get("pid") and not plat.pid_alive(int(d["pid"])):
        d["phase"] = "failed"
        d["note"] = "登录进程意外退出了"
    elif d.get("phase") == "waiting" and not d.get("pid"):
        # 面板先写一条 pid=0 的占位（让界面立刻显示"正在打开"），登录子进程起来后
        # 才把自己的 pid 写进去。如果子进程根本没起来（导入失败 / 被关掉 / 机器睡眠），
        # 这个 pid=0 会永远停在 waiting：卡片一直写"窗口已经弹出来了"、再点登录被拒、
        # 「＋」按钮永远 busy。给 25 秒宽限，超了就判失败（子进程启动是秒级的）。
        started = float(d.get("started") or 0)
        if started and time.time() - started > 25:
            d["phase"] = "failed"
            d["note"] = "登录进程没起来（可能是环境问题，再试一次）"
    return d


def cancel_path(cfg: cfg_mod.Config, account_id: str):
    return phase_path(cfg, account_id).with_suffix(".cancel")


def login(cfg: cfg_mod.Config, acc: cfg_mod.Account, *,
          timeout: float = 300.0, keep_open: bool = False,
          poll: float = 2.0, discard_on_fail: bool = False) -> dict:
    """打开窗口让用户登录。成功后（默认）把窗口藏回去。

    discard_on_fail：这个号是面板里点「＋」刚建的。没登上（取消 / 关窗 / 超时）
    就把浏览器关掉、把号删掉——用户心里"还没加上"，不能留一个空壳号在列表里。

    返回 {"ok": bool, "already": bool, "state": {...}, "note": str}
    """
    cancel_path(cfg, acc.id).unlink(missing_ok=True)
    write_phase(cfg, acc.id, "waiting", started=time.time(), timeout=timeout,
                new=discard_on_fail)
    # 必须 visibility="show"：一是启动时的隐藏线程会在 8 秒内反复把窗口藏回去，
    # 用户刚看到登录页就消失；二是复用实例时也要主动叫到最前，否则用户在找窗口。
    try:
        created, note = browser.ensure(cfg, acc, visibility="show")
    except Exception as e:
        return _fail(cfg, acc, discard_on_fail, "浏览器没起来：%s" % str(e)[:160])
    st = browser.alive(cfg, acc)
    if st["state"] != "ours":
        return _fail(cfg, acc, discard_on_fail, "浏览器没起来：%s" % st.get("note"), st)

    pid = st["pid"]

    def stop():
        if cancel_path(cfg, acc.id).exists():
            return "cancelled"
        if not plat.pid_alive(pid):
            return "closed"
        return None

    deadline = time.time() + timeout
    try:
        ok, state = asyncio.run(_wait_login(acc.port, deadline, poll, stop))
    except Exception as e:
        # 用户关窗口时 CDP 连接会直接断，这也算"关了"，不是程序出错
        why = stop()
        if why:
            ok, state = False, {"stopped": why}
        else:
            return _fail(cfg, acc, discard_on_fail, "等待登录时出错：%s" % str(e)[:160])

    if ok:
        if not keep_open:
            plat.hide_window(pid)          # 登好了就藏回去
        # 必须把账号状态改回可用：coordinator 因为 AUTH_REQUIRED 停用了这个账号，
        # 如果登录成功不解开，用户会看到"明明登上了却一直不跑"。
        _mark_login_ok(cfg, acc, state)
        write_phase(cfg, acc.id, "ok", email=(state or {}).get("email"),
                    keep_open=keep_open)
        return {"ok": True, "already": bool(created is False), "state": state,
                "note": "已登录"}

    stopped = (state or {}).get("stopped")
    if stopped == "cancelled":
        note = "已取消"
    elif stopped == "closed":
        note = "登录窗口被关掉了"
    else:
        note = "还没登录（%d 秒内没检测到输入框和头像）" % int(timeout)
    return _fail(cfg, acc, discard_on_fail, note, state,
                 phase="cancelled" if stopped else "failed")


def _drop_profile(profile) -> None:
    """新号没登上：它的浏览器资料目录是空壳（Chrome 一启动就写约 60MB），删掉。

    只在 discard 路径调：这个号从没登录过，目录里没有任何用户的东西。
    浏览器刚被结束，文件句柄可能还没放干净，所以重试几次。
    """
    import shutil

    for _ in range(10):
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return
        time.sleep(0.5)


def _fail(cfg, acc, discard: bool, note: str, state=None, phase: str = "failed") -> dict:
    if discard:
        # 新号没登上：浏览器关掉，号删掉。profile 目录留着（空的，不碍事）。
        try:
            browser.stop(cfg, acc)
        except Exception:
            pass
        try:
            cfg_mod.remove_account(cfg.path, acc.id)
        except Exception:
            pass
        _drop_profile(acc.profile)
    write_phase(cfg, acc.id, phase, note=note, discarded=discard)
    cancel_path(cfg, acc.id).unlink(missing_ok=True)
    return {"ok": False, "already": False, "state": state or {}, "note": note,
            "discarded": discard}


def _mark_login_ok(cfg: cfg_mod.Config, acc: cfg_mod.Account, state: dict) -> None:
    try:
        from .core.store import Store

        store = Store(cfg)
        st = store.account(acc.id)
        st.login = "ok"
        st.login_checked_at = time.time()
        st.login_user = str((state or {}).get("user_id") or "")
        email = str((state or {}).get("email") or "").strip()
        if email:
            st.login_email = email
        # 订阅档位：登录探针已经顺手带了 planType，落盘后账号卡直接显示。
        # 没探到就留空（面板显示 unknown），不猜。
        plan = str((state or {}).get("plan") or "").strip()
        if plan:
            st.plan = plan
            st.plan_source = "session"
            st.plan_checked_at = time.time()
        store.save_account(st)
    except Exception:
        # 状态记录失败不能影响"登录成功"这个事实本身
        pass
