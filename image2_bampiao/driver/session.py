"""一次出图的完整动作：确保浏览器 → 交给 CDP 驱动 → 收回产物。

为什么用子进程调驱动，而不是 import：
- 驱动内部有全局状态（STATE/DEBUG）和长生命周期连接，一旦卡住，子进程可以直接整棵树回收；
- 出图是"跑一次、结束"的动作，不需要常驻内存。
子进程的输出**写文件**而不是管道：孙进程占着管道时 capture_output 的超时是假的（V3 已验证）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config as cfg_mod, locks
from .. import media, platform as plat
from . import browser

CREATE_NO_WINDOW = 0x08000000
# POSIX 上 creationflags 只要非 0 就直接 ValueError，所以必须按平台给。
# （Windows 要这个 flag 是为了不给驱动弹黑框；mac 本来就没有控制台窗口。）
_SPAWN_FLAGS = CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _state_file(cfg: cfg_mod.Config, name: str) -> Path:
    d = cfg.state_dir / "driver"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _env(cfg: cfg_mod.Config) -> dict:
    env = dict(os.environ)
    env["BAMPIAO_DATA"] = str(cfg.data_dir)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _driver_exe() -> str:
    """冻结版里挑哪个 exe 去跑驱动。

    桌面壳自己不认识 --driver，用它会掉进它自己的参数解析。
    bundle 里 GUI 和 CLI 是同目录的两个可执行文件，优先找 CLI：
    Windows 叫 bampiao.exe，macOS 叫 bampiao。
    """
    exe = Path(sys.executable)
    for name in ("bampiao", "bampiao.exe"):
        alt = exe.with_name(name)
        if alt.exists() and alt != exe:
            return str(alt)
    return str(exe)


def _driver_cmd() -> list[str]:
    """怎么起驱动子进程。

    普通安装：``python -m image2_bampiao.driver.cdp_run``
    冻结版：exe 里没有 ``-m``，改用隐藏参数 ``--driver`` 回到入口分派。
    """
    if getattr(sys, "frozen", False):
        return [_driver_exe(), "--driver"]
    return [sys.executable, "-m", "image2_bampiao.driver.cdp_run"]


def _opening(title: str) -> str:
    """开场白。第一行【半瓢·…】会被驱动设成 ChatGPT 会话标题，侧栏里一眼认得出是半瓢开的。

    webimg 时代就是这么做的；移植时开场白换成了普通一句话，驱动认不出【开头，
    就不改标题了——侧栏全是「出图要求」，用户分不清哪些是半瓢的（2026-10-30 用户指出）。
    """
    head = "·".join(x for x in ("半瓢", " ".join(str(title or "").split())[:24],
                                time.strftime("%m-%d %H:%M")) if x)
    return ("【%s】\n我在给本地出图工具做准备。接下来每条消息都是一个画面需求，"
            "请只出图，不要额外解释，不要反问。\n"
            # 2026-11-03：原来没这句，模型会认真回一整段，光"开场"就白等 121 秒。
            "这一条只是打个招呼：不用回复，也不用画图。" % head)


def _base_args(cfg: cfg_mod.Config, acc: cfg_mod.Account, out_dir: Path,
               items: Path, timeout: int, title: str = "") -> list[str]:
    return _driver_cmd() + [
        "--cat", "job", "--out", str(out_dir),
        "--state-key", "job_%d" % int(time.time()),
        "--port", str(acc.port), "--engine", acc.browser,
        "--items", str(items), "--timeout", str(timeout),
        "--opening", "wide", "--opening-text", _opening(title),
    ]


def driver_pid_file(cfg: cfg_mod.Config, args: list[str]) -> Path:
    """驱动进程的 pid 放在哪。用端口区分账号（args 里带 --port）。"""
    port = ""
    try:
        i = args.index("--port")
        port = args[i + 1]
    except (ValueError, IndexError):
        port = "unknown"
    return Path(cfg.data_dir) / "state" / "driver" / ("pid_%s.txt" % port)


def read_driver_pid(cfg: cfg_mod.Config, acc: cfg_mod.Account) -> int:
    """这个号此刻的驱动进程 pid；没有返回 0。"""
    from .. import locks
    from . import browser as _br

    p = Path(cfg.data_dir) / "state" / "driver" / ("pid_%d.txt" % acc.port)
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    # 校验：锁还被占着才可信（进程死了锁就释放了）
    try:
        if locks.try_acquire(_br.gen_lockfile(cfg, acc.id)):
            return 0
    except Exception:
        pass
    return pid


def _run(cfg: cfg_mod.Config, args: list[str], log_path: Path,
         timeout: float) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_file = driver_pid_file(cfg, args)
    with open(log_path, "wb") as fh:
        p = subprocess.Popen(args, stdout=fh, stderr=fh,
                             env=_env(cfg), creationflags=_SPAWN_FLAGS)
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(p.pid), encoding="utf-8")
        except OSError:
            pass
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            plat.kill_tree(p.pid)
            # kill 之后必须回收句柄：不 wait 的话僵尸留到 GC，
            # 而且下面读日志时文件可能还在被这个进程写。
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            rc = -9          # 我们主动杀的，语义是"超时未完成"，不是驱动自己的失败
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(p.pid):
            pid_file.unlink()
    except OSError:
        pass
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
    except OSError:
        tail = ""
    return rc, tail


def _classify(rc: int, out_dir: Path, tail: str) -> str:
    """失败归因。优先用驱动写的结构化结果，其次看它打的强标记，都不猜全文。

    以前是 `"登录" in tail or "login" in tail.lower()`：日志里出现一次 URL、
    一个 JS 类名就能误判成"掉登录"，用户被叫去重新登录，其实只是出图失败。
    """
    if rc == 9:
        return "auth"                       # 驱动 raise SystemExit(9) 只在判定未登录时
    res = None
    try:
        res = json.loads((out_dir / "_result.json").read_text(encoding="utf-8"))
    except Exception:
        res = None
    if isinstance(res, dict) and res.get("quota"):
        return "quota"
    if rc == -9:
        # 我们主动杀的（超时）：图可能已经在远端生成，这比"粘贴失败"重要得多。
        # 顺序不能错——一次跑里既有粘贴失败又超时被杀时，要先报"不确定"，
        # 否则用户会以为可以放心重投，实际会重复出图、重复烧额度（独立验证发现）。
        return "timeout"
    if isinstance(res, dict) and res.get("browser_lost"):
        # 等图时浏览器/页面被关了（wait_for_timeout 抛 ChannelClosed）。
        # 以前这类异常直接让驱动崩掉、连 _result.json 都不写，上层只能记成
        # "渲染失败"——用户看到就去改提示词，其实跟提示词一点关系没有。
        return "browser-lost"
    if isinstance(res, dict) and res.get("no_ref_file"):
        # 参考图路径不存在：派单方给的路径没东西（文件还没落盘/写错）。
        # 重试一次通常就过，改提示词纯属白费。
        return "no-ref"
    if isinstance(res, dict) and res.get("paste_failed"):
        # 提示词没进输入框 != 出图失败：重试一次或把提示词缩短通常就能救，
        # 报成"渲染失败"会让调用方（尤其 AI）去改提示词内容，白折腾（2026-10-30 实测）。
        return "paste"
    if isinstance(res, dict) and res.get("empty_turn"):
        # 回合结束了、但一直没读到图：网慢时图会比文字晚几十秒出来。
        # 不能报"渲染失败"（用户会去改提示词），要报"不确定，去网页看一眼"。
        return "empty"
    if rc != 0 and "!! AUTH" in tail:
        return "auth"
    return "render"


def probe(cfg: cfg_mod.Config, acc: cfg_mod.Account, *, timeout: float = 120) -> dict:
    """看一眼这个账号的浏览器现在停在哪一页（主要用于登录前后自检）。"""
    # visibility="keep"：绝不改窗口可见性。
    # 用户在 bampiao login 里正输密码时，别处跑一次 probe 就把窗口藏没了，
    # 这种"帮忙变成捣乱"的 bug 最难查。
    browser.ensure(cfg, acc, visibility="keep")
    items = _state_file(cfg, "items.json")
    if not items.exists():
        items.write_text("[]", encoding="utf-8")
    args = _base_args(cfg, acc, cfg.logs_dir / "probe", items, 60) + ["--probe"]
    log = cfg.logs_dir / ("probe_%s.log" % acc.id)
    rc, tail = _run(cfg, args, log, timeout)
    info = None
    for line in reversed((log.read_text(encoding="utf-8", errors="replace") or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"probe"' in line:
            try:
                info = json.loads(line).get("probe")
                break
            except Exception:
                continue
    return {"ok": rc == 0 and bool(info), "probe": info, "rc": rc, "log": str(log),
            "tail": "" if info else tail}


def _hide_settled(cfg: cfg_mod.Config, acc: cfg_mod.Account,
                  rounds: int = 3, gap: float = 0.8) -> None:
    """把"我们起的那个浏览器"再藏一次，分几轮。

    为什么一次不够：Chrome 启动时我们会立刻藏窗口，但它在加载完页面后**又会把窗口显示出来**
    （尤其新 profile 第一次起、或恢复上次会话）。用户看到的就是"点了一下读取会话，
    任务栏里冒出个浏览器"。所以收尾再扫几轮，看到可见就藏。
    """
    from .. import media, platform as plat

    for i in range(rounds):
        try:
            st = browser.alive(cfg, acc)
            pid = (st or {}).get("pid")
            if not pid:
                return
            if plat.window_visible(pid):
                try:
                    wants = browser._wants_visible(cfg, acc.id)
                except Exception:
                    wants = True      # 判断不了就当用户要看，宁可不藏
                if not wants:
                    plat.hide_window(pid)
        except Exception:
            return
        if i + 1 < rounds:
            time.sleep(gap)


def chats(cfg: cfg_mod.Config, acc: cfg_mod.Account, op: str, *,
          ids: list[str] | None = None, limit: int = 50,
          timeout: float = 60) -> dict:
    """会话管理：op = list / rm。浏览器没起就起一个（隐藏）。

    收尾会把"本次新起的那个窗口"再藏几轮：只藏启动那一瞬间不够，
    Chrome 加载完页面会把窗口重新显示出来（2026-10-30 用户实测：点了"读取"，
    任务栏就冒出浏览器）。
    """
    started, _ = browser.ensure(cfg, acc, visibility="keep")
    try:
        return _chats_run(cfg, acc, op, ids=ids, limit=limit, timeout=timeout)
    finally:
        if started:
            _hide_settled(cfg, acc)


def _chats_run(cfg: cfg_mod.Config, acc: cfg_mod.Account, op: str, *,
               ids: list[str] | None = None, limit: int = 50,
               timeout: float = 60) -> dict:
    out = _state_file(cfg, "chats_result.json")
    try:
        out.unlink()
    except OSError:
        pass
    args = [_driver_exe(), "--chats", "--port", str(acc.port),
            "--op", op, "--limit", str(limit), "--json-out", str(out)]
    if ids:
        args += ["--ids"] + [str(i) for i in ids]
    log = cfg.logs_dir / ("chats_%s.log" % acc.id)
    rc, tail = _run(cfg, args, log, timeout)
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "why": (tail or "驱动没有任何输出")[-400:]}


def generate(cfg: cfg_mod.Config, acc: cfg_mod.Account, prompt: str, out: Path, *,
             refs: list[str] | None = None, aspect: str = "1:1", alpha: bool = False,
             opening: str = "wide", timeout: int = 600,
             lock_timeout: float = 0.0, title: str = "", anim: bool = False,
             svg: bool = False, svg_anim: bool = False) -> dict:
    """出一条图。成功返回 {"ok": True, "path": 绝对路径}。

    **一个账号同一时刻只允许一次出图**：两次调用连的是同一个 CDP 页面，
    会互相 goto / 发消息，结果是串图 + 重复烧额度（评审指出的 P1-5）。
    拿不到锁就返回 kind="busy"，让上层决定是排队还是报忙。
    """
    try:
        with locks.exclusive(browser.gen_lockfile(cfg, acc.id), timeout=lock_timeout,
                             what="账号 %s 的出图" % acc.id):
            return _generate(cfg, acc, prompt, out, refs=refs, aspect=aspect,
                             alpha=alpha, opening=opening, timeout=timeout, title=title,
                             anim=anim, svg=svg, svg_anim=svg_anim)
    except locks.Busy as e:
        return {"ok": False, "kind": "busy", "rc": 0, "log": "", "tail": str(e)}


def _generate(cfg: cfg_mod.Config, acc: cfg_mod.Account, prompt: str, out: Path, *,
              refs: list[str] | None = None, aspect: str = "1:1", alpha: bool = False,
              opening: str = "wide", timeout: int = 600, title: str = "",
              anim: bool = False, svg: bool = False, svg_anim: bool = False) -> dict:
    try:
        browser.ensure(cfg, acc)
    except Exception as e:
        # 起不来浏览器 ≠ 出图失败。分两类：
        # - 账号正被另一个进程用 → busy（放回队列，别烧额度）
        # - 没装浏览器 / profile 不对 / 端口被占 → no_lane（环境问题，要人修）
        msg = str(e)
        kind = "busy" if ("正被另一个进程使用" in msg or "另一个实例占着" in msg) else "no_lane"
        return {"ok": False, "kind": kind, "rc": 0, "log": "", "tail": msg}

    # 首行会被驱动拼进文件名（S01_<task>.png）。中文提示里 "1/2 侧光"、"为什么?" 很常见，
    # 这些字符在 Windows 文件名里非法 → 图已经出好、额度已经花掉，却在保存那一步崩。
    raw = (prompt.strip().splitlines() or ["出图"])[0]
    _BAD = "/:*?\"<>|" + chr(92) + "".join(chr(c) for c in (13, 10, 9))
    task = "".join("_" if ch in _BAD else ch for ch in raw).strip(" .")[:16] or "出图"
    items = _state_file(cfg, "items_%d.json" % int(time.time() * 1000))
    items.write_text(json.dumps([{
        "id": "S01", "cat": "job", "task": task,
        "prompt": "·" + task + "\n" + prompt.strip(),
        "ref": ";".join(refs or []),
        "aspect": aspect or "",
        "alpha": bool(alpha),
        "anim": bool(anim),
        # SVG 旁路：驱动看到这个才走文件附件通道。默认 False，
        # 老单子/老调用方写出来的 items.json 跟以前一字不差。
        "svg": bool(svg),
        "svg_anim": bool(svg_anim),
    }], ensure_ascii=False, indent=1), encoding="utf-8")

    out_dir = cfg.state_dir / "driver" / ("out_%d" % int(time.time() * 1000))
    out_dir.mkdir(parents=True, exist_ok=True)
    args = _base_args(cfg, acc, out_dir, items, timeout, title)
    log = cfg.logs_dir / ("gen_%s_%d.log" % (acc.id, int(time.time())))
    rc, tail = _run(cfg, args, log, timeout + 180)

    # 不能只认 *.png：Images 2.6 的 GIF 现在是 .gif（2026-11-06）。
    # .svg 只在明确出 SVG 的单子里才纳入扫描，其他单子的目录里就算混进
    # 一个 svg 也不会被误当成成图（2026-11-07）。
    outs = media.outputs(out_dir, include_svg=svg)
    if rc != 0 or not outs:
        kind = _classify(rc, out_dir, tail)
        r = {"ok": False, "kind": kind, "rc": rc, "log": str(log), "tail": tail}
        if kind == "quota":
            # 驱动把"什么时候恢复"写进了 _result.json，这里必须带上去，
            # 否则上层只能记成"撞了上限"却不知道冷却到几点，账号页永远显示没在冷却。
            try:
                res = json.loads((out_dir / "_result.json").read_text(encoding="utf-8"))
                if isinstance(res, dict) and res.get("reset_at"):
                    r["reset_at"] = res["reset_at"]
            except Exception:
                pass
        return r

    src = outs[0]
    dst = Path(out)
    # 用户给的 --out 可能是 xxx.png，但交付物其实是 GIF：以内容为准，
    # 把后缀换掉（文件名其余部分不动），并在 stderr 说一句（2026-11-06）。
    if dst.suffix.lower() != src.suffix.lower():
        dst = dst.with_suffix(src.suffix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

    info = media.probe(dst)
    if svg and alpha:
        # SVG 本身就是矢量 + 透明底，抠图（Pillow）对 XML 毫无意义，还会报错
        print("  （SVG 是矢量文本，--alpha 已忽略）", file=sys.stderr)
    elif alpha and info.get("animated"):
        # 抠图会把动图压成一张静图——宁可不抠，也不能把动画弄没
        print("  （这是动图，--alpha 跳过抠图；需要透明底请单独出一张静图）",
              file=sys.stderr)
    elif alpha:
        # --alpha 的真正含义是"要透明 PNG"。模型只能给洋红底，抠图是最后一步。
        # 以前只做"是不是洋红底"的检查，用户拿到的是一张洋红底的图，等于没实现。
        from . import matte
        try:
            r = matte.magenta_to_alpha(dst)
            if not r.get("ok"):
                print("  （透明底：%s）" % r.get("why"), file=sys.stderr)
        except Exception as e:
            print("  （抠图失败：%s；图已按原样保存，可手工抠）" % e, file=sys.stderr)

    size = None
    if svg:
        # Pillow 打不开 XML；probe_svg 从 viewBox / width height 读出尺寸
        size = info.get("size") or None
    else:
        try:
            from PIL import Image
            with Image.open(dst) as im:
                size = list(im.size)
        except Exception:
            pass
    # 驱动的"标注"（比例不符 / 要动图却给了静图 / 尺寸偏小）写在 _flags.json 里。
    # 以前直接 rmtree，标注跟着现场一起没了——用户只看到"已保存"，不知道拿错了
    # （2026-11-06 评审）。
    note = ""
    try:
        _fl = json.loads((out_dir / "_flags.json").read_text(encoding="utf-8"))
        if isinstance(_fl, dict):
            note = "；".join(str(v) for v in _fl.values() if v)[:300]
    except Exception:
        pass
    if svg and svg_anim and not info.get("svg_animated"):
        note = ("要的是会动的 SVG，但拿到的是静态矢量图"
                + ("；" + note if note else ""))[:300]
    elif anim and not info.get("animated"):
        note = ("要的是动图，但拿到的是静态图" + ("；" + note if note else ""))[:300]

    # 图已经复制到 jobs 目录了，驱动的工作目录（每单一张原图，约 1MB）留着只会占地。
    # 失败路径不清：那时候要留着现场排查。
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
    except Exception:
        pass
    _r = {"ok": True, "path": str(dst.resolve()), "size": size, "log": str(log),
          "format": info.get("format", ""), "frames": info.get("frames", 1),
          "animated": bool(info.get("animated")), "note": note}
    if svg:
        # 只给 SVG 单子加这两个键：老调用方拿到的数据结构保持原样
        _r["svg"] = bool(info.get("svg"))
        _r["svg_animated"] = bool(info.get("svg_animated"))
    return _r
