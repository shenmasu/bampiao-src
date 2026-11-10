"""浏览器实例管理：谁启动、谁隐藏、谁回收。

规矩（都是踩出来的）：
- **一个账号一个专用 profile**，绝不共用、绝不碰用户日常浏览器。
- 启动参数固定（禁扩展等），否则 profile 里的扩展会把页面劫持走。
- 窗口**按 PID 隐藏**，且要抢在绘制出来之前：启动后立刻并行轮询隐藏，
  否则任务栏会闪几下（用户真投诉过）。
- 结束进程只结束**我们自己记录过、且启动时间对得上**的那一个，
  不能按进程名杀。
- **没有记录就一个进程都不碰**；**没杀掉就不清记录**（清了就永久失控）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import browsers, config as cfg_mod, locks
from .. import platform as plat

CHAT_URL = "https://chatgpt.com/"

# 从"进程起来"到"CDP 可用"通常 3–10 秒；给到 90 秒是为了照顾冷启动 + 慢磁盘。
# 超过这个窗口还没 CDP，才算真的卡死（可以结束重开）。
STARTUP_GRACE = 90.0

# 这套参数是生产验证过的，别随手改
_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-blink-features=AutomationControlled",
    # 代理普遍不支持 UDP：Chrome 走 QUIC(HTTP/3) 时会卡在连接建立，表现就是
    # "页面一片空白、title 为空、等 30 秒也不出字"（2026-11-03 实测）。
    "--disable-quic",
    # 下面几条是「漏窗口」的止血（2026-10-31 用户截图：任务栏冒出【半瓢·R117·dice1…】）：
    # · session-crashed-bubble / hide-crash-restore-bubble：关掉「要恢复页面吗？」那类弹窗。
    #   它由 Chrome 自己创建，创建那一刻是可见的，而巡逻最快 0.8 秒一轮，中间就会露头。
    # · window-position 挪到屏幕外：就算窗口真被 ShowWindow 了，也不挡视线；
    #   任务栏按钮仍靠 hide_window 的 TOOLWINDOW 摘掉。两道一起才不留缝。
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # 缓存必须有上限。2026-11-04 实测：三个号跑了一个月，Cache + Code Cache
    # 长到 1.5G（acc1 单号 592M）。Chrome 默认按磁盘比例给，不封顶就一直涨。
    # 半瓢只访问 chatgpt.com 一个站，50M 磁盘缓存 + 20M 媒体缓存完全够用。
    "--disk-cache-size=52428800",
    "--media-cache-size=20971520",
    # 不需要后台组件（优化指南模型、翻译、CRX 预装）——
    # acc3 单号的 component_crx_cache 涨到了 202M，就是它们下的。
    "--disable-component-update",
    "--disable-background-networking",
    "--window-position=-32000,-32000",
    "--window-size=1400,900",
]


@dataclass
class Handle:
    account_id: str
    pid: int
    port: int
    profile: str
    started_at: float

    def as_dict(self) -> dict:
        return {"account_id": self.account_id, "pid": self.pid, "port": self.port,
                "profile": self.profile, "started_at": self.started_at}


def _run_dir(cfg: cfg_mod.Config) -> Path:
    d = cfg.state_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pidfile(cfg: cfg_mod.Config, account_id: str) -> Path:
    return _run_dir(cfg) / ("%s.json" % account_id)


def lockfile(cfg: cfg_mod.Config, account_id: str) -> Path:
    return _run_dir(cfg) / ("%s.browser.lock" % account_id)


def gen_lockfile(cfg: cfg_mod.Config, account_id: str) -> Path:
    return _run_dir(cfg) / ("%s.gen.lock" % account_id)


def read_handle(cfg: cfg_mod.Config, account_id: str) -> Handle | None:
    p = pidfile(cfg, account_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return Handle(d["account_id"], int(d["pid"]), int(d["port"]),
                      d.get("profile") or "", float(d.get("started_at") or 0))
    except Exception:
        return None


def _write_handle(cfg: cfg_mod.Config, h: Handle) -> None:
    p = pidfile(cfg, h.account_id)
    # 临时名带上 pid：两个写者用同一个 .tmp 会互相截断，写出来的 JSON 是半截。
    tmp = p.with_name("%s.%d.tmp" % (p.name, os.getpid()))
    tmp.write_text(json.dumps(h.as_dict(), ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _clear_handle(cfg: cfg_mod.Config, account_id: str) -> None:
    try:
        pidfile(cfg, account_id).unlink(missing_ok=True)
    except OSError:
        pass


def _same_proc(h: Handle) -> bool:
    """这条记录还指向当初那个进程吗（PID 复用检查）。"""
    if not plat.pid_alive(h.pid):
        return False
    st = plat.start_time(h.pid)
    return bool(h.started_at and st and abs(st - h.started_at) <= 2)


def alive(cfg: cfg_mod.Config, acc: cfg_mod.Account) -> dict:
    """这个账号的浏览器现在是死是活，是谁的。

    返回 {"state": "down"|"starting"|"ours"|"stale"|"reused"|"foreign", "pid": .., "note": ..}

    starting / ours / stale / reused 四种的区别很关键：
    - starting：我们的进程在、还没到 CDP 就绪 —— **不许杀**，等着
    - ours：我们的进程在、CDP 通 —— 正常
    - stale：我们的进程在、过了启动窗口 CDP 仍不通 —— 可以结束重开
    - reused：这个 PID 已经是别人的程序了 —— **绝对不许碰**
    """
    h = read_handle(cfg, acc.id)
    if h:
        if not plat.pid_alive(h.pid):
            return {"state": "down", "pid": h.pid, "note": "记录的进程已退出"}
        # PID 复用防护：启动时间对不上，说明这个 PID 已经被别的进程占了
        st = plat.start_time(h.pid)
        if h.started_at and st and abs(st - h.started_at) > 2:
            # 这不是"我们的浏览器病了"，而是"这个 PID 现在是别人的程序"。
            # 绝不能杀——混成 stale 会让 ensure() 顺手 taskkill 掉无辜进程（评审实测过）。
            return {"state": "reused", "pid": h.pid,
                    "note": "这个 PID 已经是别的程序了（系统复用），半瓢不碰它"}
        if plat.cdp_alive(acc.port):
            return {"state": "ours", "pid": h.pid, "note": "在跑"}
        if h.started_at and (time.time() - h.started_at) < STARTUP_GRACE:
            return {"state": "starting", "pid": h.pid, "note": "正在启动（CDP 还没就绪）"}
        return {"state": "stale", "pid": h.pid, "note": "进程在但 CDP 不通"}
    if plat.port_open(acc.port):
        return {"state": "foreign", "pid": 0,
                "note": "端口 %d 被别的程序占着，不是半瓢的实例" % acc.port}
    return {"state": "down", "pid": 0, "note": "未启动"}


def _next_free_port(cfg: cfg_mod.Config, acc: cfg_mod.Account, limit: int = 10) -> int:
    """这个账号能换到哪个空端口。找不到返回 0。

    **必须同时避开其他账号的端口**：只判"有没有人监听"是不够的，
    另一个账号的浏览器没起在跑的时候，它的端口看起来也是空的，
    换过去等于把冲突从 A 挪到 B，下次开机就轮到 B 起不来。
    """
    used = {a.port for a in getattr(cfg, "accounts", []) if a.id != acc.id}
    for port in range(acc.port + 1, acc.port + 1 + limit):
        if port in used:
            continue
        if not plat.port_open(port):
            return port
    return 0


# Windows 藏一次就是一个 user32 调用，可以高频；macOS 每次都要起一个 osascript 进程，
# 50ms 一次等于每秒 20 个进程。而且 mac 是按「整个 App 隐藏」，隐藏后新开的窗口也不会冒出来，
# 用不着高频盯。
_HIDE_POLL = 0.5 if sys.platform == "darwin" else 0.05
_KEEP_POLL = 2.0 if sys.platform == "darwin" else 0.8
KEEP_OPEN_TTL = 2 * 3600      # `--keep-open` 的豁免最多管 2 小时


def log(msg: str) -> None:
    """巡逻/浏览器层的日志：写 stderr，桌面壳会把它并进 desktop.log。

    单独实现而不是 import 驱动的 log：browser 是底层模块，不该依赖 cdp_run。
    """
    try:
        sys.stderr.write("[browser] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass
   # mac 每次要 fork osascript，别太密


def _hide_soon(pid: int, seconds: float = 8.0, cfg=None, account_id: str = "") -> None:
    """并行盯着窗口，一出现就隐藏。

    50ms 一次：窗口建出来到画出内容之间只有一两百毫秒，0.25 秒一次会让人看到任务栏闪一下。
    用户这时点了「打开浏览器」/ 开始登录，就立刻收手——否则刚叫出来又被它藏回去（实测）。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not plat.pid_alive(pid):
            return
        if cfg is not None and _wants_visible(cfg, account_id):
            return
        try:
            plat.hide_window(pid)
        except Exception:
            pass
        time.sleep(_HIDE_POLL)


def _wait_cdp(port: int, pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if plat.cdp_alive(port):
            return True
        if pid and not plat.pid_alive(pid):
            return False
        time.sleep(0.4)
    return False


def ensure(cfg: cfg_mod.Config, acc: cfg_mod.Account, *,
           visibility: str = "hide", timeout: float = 45.0) -> tuple[bool, str]:
    """确保这个账号的浏览器可用。返回 (是否新启动, 说明)。

    visibility（窗口怎么处理）：
    - ``hide``  一出现就藏（出图流程用，默认）
    - ``keep``  复用已有实例时不动它的可见性（probe 用：绝不把用户正在输密码的窗口藏掉）；
                **但如果必须新起一个，还是要藏**——用户没要求看窗口，弹出来是惊吓
    - ``show``  叫到最前（登录用）
    """
    if visibility not in ("hide", "keep", "show"):
        raise ValueError("visibility 只能是 hide / keep / show")
    try:
        with locks.exclusive(lockfile(cfg, acc.id), timeout=timeout,
                             what="账号 %s 的浏览器" % acc.id):
            return _ensure_locked(cfg, acc, visibility, timeout)
    except locks.Busy as e:
        raise RuntimeError(str(e)) from None


def _needs_restart(cfg: cfg_mod.Config, acc_id: str) -> bool:
    from . import session_health as sh

    try:
        return sh.is_broken(str(cfg.data_dir), acc_id)
    except Exception:
        return False


def _clear_needs_restart(cfg: cfg_mod.Config, acc_id: str) -> None:
    from . import session_health as sh

    try:
        sh.clear_broken(str(cfg.data_dir), acc_id)
    except Exception:
        pass


def _proxy_args() -> list[str]:
    """决定这个浏览器怎么走网络。**每次启动只探测一次**。

    2026-11-03 事故：res-downloader 把系统代理写成 127.0.0.1:8899，退出后没还原。
    它会对 CONNECT 回 200，但隧道里送不出任何东西 → 页面一片空白、
    Cloudflare 永远停在"请稍候"。而 curl 走环境变量 7890 一切正常，
    所以从命令行怎么看都不像网络问题。

    现在的策略（按顺序）：
    1. 系统代理真的能通 → 就用它（不动用户的设置）
    2. 不能通 → 在本机常见代理端口里找一个能通的，只给**这个浏览器**加
       `--proxy-server`（不改注册表，不影响用户的浏览器）
    3. 都不通 → `--no-proxy-server`，并打一行日志，让人知道该修什么
    """
    try:
        impl = plat.impl
        sp = impl.system_proxy() if hasattr(impl, "system_proxy") else ""
        works = getattr(impl, "proxy_works", None)
        if not sp:
            return []
        if works is None or works(sp):
            return []
        print("!! 系统代理 %s 不能转发 HTTPS（多半是下载工具劫持的）" % sp)
        # 常见的本地代理端口：Clash / Clash Verge / v2rayN / Shadowsocks
        for cand in ("127.0.0.1:7890", "127.0.0.1:7897", "127.0.0.1:10809",
                     "127.0.0.1:1080", "127.0.0.1:2080"):
            try:
                if works(cand):
                    print("   已改用 %s（只对这个浏览器生效，不动你的系统设置）" % cand)
                    return ["--proxy-server=http://%s" % cand]
            except Exception:
                continue
        print("   本机没找到可用的代理，改为直连")
        return ["--no-proxy-server"]
    except Exception:
        return []


def _has_window(pid: int) -> bool:
    """这个进程有没有真正的顶层窗口。

    2026-11-02 实测：Chrome 进程活着、窗口没了（假死），面板点「打开浏览器」毫无反应，
    也没有验证框。这种状态只能重启浏览器，等是等不回来的。
    查不到就当有窗口（宁可不动，也别误杀用户的浏览器）。
    """
    try:
        return any((w.get("title") or "").strip() for w in plat.list_windows(pid))
    except Exception:
        return True


def _ensure_locked(cfg: cfg_mod.Config, acc: cfg_mod.Account,
                   visibility: str, timeout: float) -> tuple[bool, str]:
    st = alive(cfg, acc)
    pid = st["pid"]

    # 进程在、但一个窗口都没有 = 假死。当成 stale 走"结束重开"那条路，
    # 否则后面所有"叫窗口出来"的操作都是对着空气做。
    if st["state"] == "ours" and pid and not _has_window(pid):
        st = {"state": "stale", "pid": pid, "note": "浏览器进程在但窗口没了（假死）"}

    # 上一轮被判成"本地草稿会话"：那种状态 reload 救不回来，只有重启浏览器能好。
    # 标记由驱动进程写（它杀不了浏览器），这里看到就重启，然后清标记。
    if st["state"] in ("ours", "starting") and _needs_restart(cfg, acc.id):
        st = {"state": "stale", "pid": pid, "note": "上一轮会话坏了（本地草稿），重启浏览器"}
        _clear_needs_restart(cfg, acc.id)

    if st["state"] in ("ours", "starting"):
        if st["state"] == "starting":
            # 等多久要按**已经等了多久**算，不能每次都给 60 秒：
            # 否则一个卡死的实例会被 consecutive 调用反复"续命"，永远等不到超时。
            h = read_handle(cfg, acc.id)
            elapsed = time.time() - (h.started_at if h and h.started_at else 0)
            wait = max(5.0, min(timeout, STARTUP_GRACE - elapsed + 5.0))
            if not _wait_cdp(acc.port, pid, wait):
                st = {"state": "stale", "pid": pid, "note": "启动超时（CDP 一直没就绪）"}
        if st["state"] in ("ours", "starting"):
            # 用户正在登录 / 明说别藏时，即使调用方要 hide 也不许动
            # （并发跑一次 gen 会把登录窗口藏掉一次，独立验证真机复现）
            if visibility == "hide" and not _wants_visible(cfg, acc.id):
                plat.hide_window(pid)
            elif visibility == "show":
                plat.show_window(pid, activate=True)
            note = ("复用已在运行的实例（pid %d）" % pid if st["state"] == "ours"
                    else "复用正在启动的实例（pid %d）" % pid)
            return False, note

    if st["state"] == "foreign":
        # 端口被别的程序占着：以前这里直接抛错，用户点「打开浏览器」看着就是"没反应"，
        # 登录时则报「不是半瓢的实例」——他根本不知道该去关哪个程序（2026-11-06 反馈）。
        # 现在自动往后挪一个空端口、写回配置，接着起：用户什么都不用管。
        port = _next_free_port(cfg, acc)
        if not port:
            raise RuntimeError(
                "端口 %d 被别的程序占着，往后 10 个端口也都满了。"
                "先关掉那个程序（多半是你自己开着的 Chrome 或 Edge）再点一次" % acc.port)
        old = acc.port
        acc.port = port
        try:
            cfg_mod.set_account_port(cfg.path, acc.id, port)
        except Exception as e:                  # 写不回配置也要能起，下次再撞就再换
            log("端口 %d 被占，这次改用 %d（没写回配置：%s）" % (old, port, e))
        else:
            log("端口 %d 被别的程序占着，自动改用 %d" % (old, port))
        _clear_handle(cfg, acc.id)
        st = {"state": "down", "pid": 0, "note": "换到端口 %d" % port}
    if st["state"] == "reused":
        _clear_handle(cfg, acc.id)          # 只丢记录，不杀任何进程

    prev = read_handle(cfg, acc.id) if st["state"] == "stale" else None
    if st["state"] == "stale":
        # 进程在、确实是我们起的、CDP 过了启动窗口还是不通：结束重开
        if not plat.kill_tree(pid):
            raise RuntimeError("旧的浏览器（pid %d）结束不掉，先手动关掉它再试" % pid)
        _clear_handle(cfg, acc.id)

    exe = browsers.find(acc.browser)
    if not exe:
        raise RuntimeError(browsers.missing_reason(acc.browser))

    # 新起实例一律先藏：只有"登录"才需要人看见窗口。
    # 以前 probe 传 keep 就直接把新窗口摊在桌面上，用户一看就顺手关掉，
    # 于是"探一下状态"把浏览器探没了（真踩过）。
    spawn_visibility = "hide" if visibility == "keep" else visibility

    acc.profile.mkdir(parents=True, exist_ok=True)
    args = [str(exe), "--remote-debugging-port=%d" % acc.port,
            "--user-data-dir=%s" % acc.profile] + _ARGS + _proxy_args()
    if spawn_visibility == "hide":
        args += list(getattr(plat.impl, "OFFSCREEN_ARGS", []))
    args += [CHAT_URL]
    new_pid = plat.spawn_detached(args, background=(spawn_visibility == "hide"))
    started = plat.start_time(new_pid) or time.time()
    _write_handle(cfg, Handle(acc.id, new_pid, acc.port, str(acc.profile), started))

    if spawn_visibility == "hide":
        # 8 秒太短：Chrome 冷启动到 CDP 可用就要 3–10 秒，「要恢复页面吗？」那类
        # 弹窗往往更晚才冒出来，之后只剩 0.8 秒一轮的巡逻，用户就会看到任务栏闪一下
        # （2026-10-31 截图）。盯满 60 秒，之后交给巡逻。
        threading.Thread(target=_hide_soon, args=(new_pid, 60.0, cfg, acc.id), daemon=True).start()

    ok = _wait_cdp(acc.port, new_pid, timeout)
    if ok:
        # 新起实例这条分支以前无条件藏：正在登录的用户会看到窗口刚出来就没了
        # （独立验证真机复现）。所有 hide 都必须先问一句"用户此刻要看吗"。
        if spawn_visibility == "hide" and not _wants_visible(cfg, acc.id):
            plat.hide_window(new_pid)
        elif spawn_visibility == "show":
            plat.show_window(new_pid, activate=True)
        return True, "已启动（pid %d，端口 %d）" % (new_pid, acc.port)

    if not plat.pid_alive(new_pid):
        # 典型场景：同一个 profile 已有实例在跑，新进程把 URL 转交给它之后自己退出了。
        # 这时**绝不能清记录**——老实例还活着，记录一清它就成孤儿，永远停不掉。
        if prev and prev.pid != new_pid and _same_proc(prev):
            _write_handle(cfg, prev)
            raise RuntimeError(
                "这个 profile 已经被另一个实例占着（pid %d，多半是上次没关干净）。"
                "先跑 bampiao browser down %s 再试" % (prev.pid, acc.id))
        _clear_handle(cfg, acc.id)
        raise RuntimeError("浏览器启动后立刻退出了（pid %d）" % new_pid)

    # 超时：只有确实杀掉了才清记录
    if plat.kill_tree(new_pid):
        _clear_handle(cfg, acc.id)
    raise RuntimeError("浏览器 %.0f 秒内没有就绪（端口 %d）" % (timeout, acc.port))


def stop(cfg: cfg_mod.Config, acc: cfg_mod.Account) -> tuple[bool, str]:
    """结束**我们自己**的实例。

    铁律：**没有 pid 记录就一个进程都不碰**。判断"端口通不通"是会飘的
    （探活本身会占用对端 backlog），拿它当杀进程的依据迟早出事。
    """
    h = read_handle(cfg, acc.id)
    if not h:
        if plat.port_open(acc.port):
            return False, "端口 %d 被别的程序占着，不是半瓢的实例，我不动它" % acc.port
        return True, "本来就没在跑"

    if not plat.pid_alive(h.pid):
        _clear_handle(cfg, acc.id)
        return True, "记录的进程已经退出了（pid %d）" % h.pid

    st = plat.start_time(h.pid)
    if h.started_at and st and abs(st - h.started_at) > 2:
        _clear_handle(cfg, acc.id)
        return True, "旧记录已失效（pid %d 现在是别的程序），没有进程需要结束" % h.pid

    ok = plat.kill_tree(h.pid)
    if ok:
        _clear_handle(cfg, acc.id)
    # 没杀掉就不能清记录：清了之后 alive() 只会说"端口被占"，用户再也停不掉它
    return ok, ("已结束 pid %d" % h.pid) if ok else ("pid %d 没能结束，记录保留，可重试" % h.pid)


def _wants_visible(cfg: cfg_mod.Config, account_id: str) -> bool:
    """这个号的窗口此刻是不是该让人看见。只有登录相关的几种情况。"""
    from .. import login_flow

    ph = login_flow.read_phase(cfg, account_id)
    phase = ph.get("phase")
    if phase == "waiting":
        return True                         # 正在等人登录
    if phase == "ok" and ph.get("keep_open"):
        # **必须有时效**：否则一次 `login --keep-open` 之后，这个账号此后任何时候
        # 露出来的窗口都不会被藏（旧 phase 文件永久生效，独立验证真机复现）。
        # 2 小时足够覆盖"登完顺手用一下"，过期后自动回到正常巡逻。
        return time.time() - float(ph.get("at") or 0) < KEEP_OPEN_TTL
    if phase == "shown":
        # 账号卡上点了「打开浏览器」。只认当时那个进程：用户把窗口关了、
        # 下次出图新起的浏览器就该照常静默，不能一直沿用"别藏"。
        # 另外必须有租约（until）：否则用户早就看完关掉了，这个号此后每次启动
        # 都会顶着"别藏"，静默就废了。
        if time.time() > float(ph.get("until") or 0):
            return False
        h = read_handle(cfg, account_id)
        return bool(h and h.pid == ph.get("browser_pid"))
    if phase == "failed" and not ph.get("discarded") and time.time() - ph.get("at", 0) < 600:
        return True                         # 命令行登录超时：窗口留给人继续登
    return False


def keep_hidden(get_cfg, stop: threading.Event, interval: float = _KEEP_POLL) -> None:
    """常驻巡逻：账号浏览器的窗口只要冒出来就藏回去。

    以前只在"刚启动的 8 秒"和"登录成功那一刻"各藏一次。之后 Chrome 自己再弹窗口
    （恢复页面、新开标签、读会话列表时开的新页），就一直挂在桌面和任务栏上——
    这就是用户说的"不断有残留"。现在改成持续盯着，登录期间除外。
    """
    interval = max(0.2, float(interval))     # interval=0 会把一核打满（独立验证实测）
    if not plat.window_probe_cheap():
        # 没有 Quartz 的 mac 只能 fork osascript 探测（每次约 120ms CPU）。
        # 实测：2 秒一轮 6.5%、6 秒一轮 1.55%，都超 1% 红线；12 秒约 0.78%。
        # 打好的 app 里 pyobjc 一定在，走的是 Quartz，根本不会到这。
        interval = max(interval, 12.0)
    warned_perm = warned_hide = warned_err = False
    perm_ok, perm_at = True, 0.0
    while not stop.wait(interval):
        try:
            # 没有「自动化」权限时，window_visible 一律 False，巡逻会一直空转、
            # 一个日志都不打。这种"静默不工作"最难查，所以这里先喊一声。
            # 权限不会每 2 秒变一次：一分钟问一次就够（mac 上每次问都是一次 fork）
            if time.time() - perm_at > 60:
                perm_ok, perm_at = plat.automation_ok(), time.time()
            if not perm_ok:
                if not warned_perm:
                    warned_perm = True
                    log("!! 没有「自动化」权限，藏不了浏览器窗口"
                        "（系统设置 → 隐私与安全性 → 自动化，把半瓢 / System Events 勾上）")
                continue
            cfg = get_cfg()
            # 副本：巡逻途中账号被删/被加，不能因为迭代中集合变了就整轮抛异常
            for acc in list(cfg.accounts):
                h = read_handle(cfg, acc.id)
                if not h or not plat.pid_alive(h.pid) or not _same_proc(h):
                    continue
                if _wants_visible(cfg, acc.id):
                    continue
                if plat.window_visible(h.pid):
                    if not plat.hide_window(h.pid) and not warned_hide:
                        # 藏失败不能静默：以前这里是个空操作 if not ...: pass
                        warned_hide = True
                        log("!! 藏窗口没成功（%s pid %s），可能是权限或窗口已被接管"
                            % (acc.id, h.pid))
                else:
                    # 窗口已经看不见了，任务栏按钮却可能还在（Chrome 藏窗后不撤按钮，
                    # 而按钮是窗口创建时登记的，改样式刷不掉缓存）。
                    # 以前只有"可见"才进 hide_window，于是按钮就永远钉在那儿。
                    plat.drop_taskbar_buttons(h.pid)
        except Exception as e:              # noqa: BLE001
            # 巡逻失败不能带走桌面壳，但**也不能一声不响**：
            # 以前这里是裸 pass，任何平台函数没接好都会变成"偶尔有窗口冒出来"，
            # 而且日志里一个字都没有。每轮最多喊一次。
            if not warned_err:
                warned_err = True
                log("!! 藏窗口巡逻出错（%s: %s）——窗口可能藏不住"
                    % (type(e).__name__, str(e)[:80]))


def recycle_idle(cfg: cfg_mod.Config, store, *, busy=None, now: float | None = None) -> list[str]:
    """关掉闲置超过 ``cfg.idle_recycle_min`` 的浏览器进程，返回关掉的账号 id。

    用户 2026-11-10 反馈「浏览器都收了，还占不占太多内存？」：收起只是隐藏窗口，
    Chrome/Edge 的浏览器进程、渲染进程、GPU 进程都还在。实测 13 个号 117 个进程
    约 2.5 GB。这个函数只碰**窗口不可见、没在等人登录/看页面、没有出图锁、
    闲置超过配置时间**的实例；下次出图 ensure() 会重新拉起，登录态不丢。
    """
    now = time.time() if now is None else now
    idle_sec = max(1, int(cfg.idle_recycle_min)) * 60
    out: list[str] = []
    for acc in list(cfg.accounts):
        h = read_handle(cfg, acc.id)
        if not h or not plat.pid_alive(h.pid):
            continue
        if _wants_visible(cfg, acc.id):
            continue
        try:
            if plat.window_visible(h.pid):
                continue            # 用户正开着这个窗口看
        except Exception:
            pass
        if busy is not None:
            try:
                if busy(acc.id):
                    continue        # 正在出图，不能动
            except Exception:
                continue            # 判断不了就当在忙，宁可留着
        try:
            st = store.account(acc.id)
            last = max(float(getattr(st, "last_used_at", 0) or 0),
                       float(h.started_at or 0))
        except Exception:
            continue
        if now - last < idle_sec:
            continue
        ok, _note = stop(cfg, acc)
        if ok:
            out.append(acc.id)
    return out


def stop_all(cfg: cfg_mod.Config) -> list[str]:
    out = []
    for acc in cfg.accounts:
        ok, note = stop(cfg, acc)
        out.append("%s：%s" % (acc.name, note))
    return out
