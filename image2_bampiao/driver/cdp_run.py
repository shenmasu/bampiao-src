"""CDP 版跑图器：连到已启动的 Edge/Chrome（--remote-debugging-port），
每一步都可截图诊断，避免盲跑。

用法：
  python cdp_run.py --cat 棋子 --ids ... --out ... --port 9444
"""
import asyncio, os, sys, json, re, time, base64, argparse, glob, subprocess

from .. import paths
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# 本模块的输出总是被 driver/session.py 重定向到日志文件，再按 UTF-8 读回来。
# 冻结版会忽略 PYTHONIOENCODING，所以这里自己定死：非终端一律写 UTF-8。
# 真终端（手工跑）不动，保持和系统码页一致。
for _s in (sys.stdout, sys.stderr):
    try:
        if _s is not None and not _s.isatty():
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.async_api import async_playwright
from PIL import Image

from .. import media as media_mod
from .quality import dhash, is_good, parse_aspect

# 浏览器路径不写死：Windows 在 Program Files，macOS 在 .app 包里，
# 统一交给 browsers 模块按各系统标准位置探测。
def _find_browser(engine: str) -> str:
    try:
        from .. import browsers
        p = browsers.find(engine)
        return str(p) if p else ""
    except Exception:
        return ""


# 半瓢：这些目录从环境变量来，默认走 paths 里的数据根，绝不写死用户机器路径。
_DATA = os.environ.get("BAMPIAO_DATA") or str(paths.data_dir())


def launch_browser(profile, port, exe, url="about:blank"):
    """自己启动浏览器并持有句柄。必须带上 --disable-extensions 等参数，
    否则 profile 里的扩展会把页面劫持到推广站（踩过：pullywood.com）。"""
    env = dict(os.environ)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-default-apps",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        url,
    ]
    proc = subprocess.Popen(args, env=env)
    for i in range(40):
        time.sleep(1)
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            log(f"浏览器已就绪 port={port} pid={proc.pid}")
            return proc
        except Exception:
            continue
    log("!! 浏览器启动超时")
    return proc

DEBUG = os.path.join(_DATA, "logs", "driver")
QUOTA_HIT = {"v": False, "reset_at": ""}   # 撞上限时顺带记下"什么时候恢复"，上层要拿它设冷却
PASTE_FAIL = {"n": 0}      # 提示词没能完整放进输入框的次数（不是出图失败）
LOST = {"v": False}        # 等图时浏览器/页面断了（CDP 掉线），整批只能收手
NO_REF = {"n": 0}          # 参考图路径不存在（派单方给的路径没东西）
REF_FAIL = {"n": 0}        # 参考图没挂上（上传失败）

# 登录态探针：只看会话接口 + 输入框，不看界面文案（文案会改、会翻译）。
_PROBE_JS = """async () => {
  let user = null, expires = null;
  try {
    const r = await fetch('/api/auth/session', {credentials: 'include'});
    if (r.ok) { const j = await r.json(); user = (j && j.user) || null; expires = (j && j.expires) || null; }
  } catch (e) {}
  const composer = !!document.querySelector('div.ProseMirror[contenteditable="true"]');
  return {url: location.href, title: document.title, composer,
          logged_in: !!user,
          user_id: user ? String(user.id).slice(0, 12) : null,
          expires: expires};
}"""
STATE = os.path.join(_DATA, "state", "driver_state.json")
ITEMS = os.path.join(_DATA, "state", "items.json")

# 干净安装时这些目录都不存在。原来第一次出图会在"开场白已经发出去"之后
# 因为写不了 DEBUG 文件而 rc=1 退出——额度花了、图没拿到。必须在模块加载时就建好。
for _d in (DEBUG, os.path.dirname(STATE), os.path.dirname(ITEMS)):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass
COMPOSER = 'div.ProseMirror[contenteditable="true"]'
# 2026-10 改版：发送按钮已无 data-testid，改用 aria-label="发送"；保留旧选择器兼容
SEND_SEL = ('button[data-testid="send-button"], '
            'button[aria-label="发送"], '
            'button[aria-label="Send"], '
            'button[type="submit"]')

OPENING = (
    "接下来我会在这个对话里，为中国风像素游戏《仙骰棋局》制作技能、专长、棋子、神器、道具图标。"
    "画风：阶梯像素硬边、只用 3 档硬边明暗、8—12 色限色板、整体饱和度压低；"
    "整体走 Q 版可爱游戏风，造型圆润饱满、表情生动，纯器物也要圆厚有玩具感。"
    "每次只出一张 1024×1024 PNG，主体清楚、四周留 12% 安全边距，纯洋红 #FF00FF 背景。"
    "每次只给我出一张图，一个版本，不拼四宫格。"
    "文字、数值由后期另加。先确认，等我发任务。"
)


OPENING_CHAR = (
    "接下来我会在这个对话里，为中国风像素游戏《仙骰棋局》制作角色的正面全身定妆照。"
    "画风：2.5—3 头身的像素人物，可爱搞怪但有神格压场感；阶梯状像素轮廓、大块发束、"
    "少量色块和折线衣褶，每种材质 3—4 档明暗，不用连续渐变和柔光。"
    "配色以墨黑、宣纸白、朱砂、石青、藤黄、赭石为主。"
    "每次只出一张 1024×1536 PNG（2:3 竖版），完整全身、头顶和脚不裁切，纯洋红 #FF00FF 背景。"
    "每次只给我出一张图，一个版本，不拼四宫格。图中不要可读文字和水印。先确认，等我发任务。"
)


OPENING_PORTRAIT = (
    "接下来我会在这个对话里，为中国风像素游戏《仙骰棋局》制作角色的头肩头像和三种表情差分。"
    "每次我会附上一张该角色的正面全身定妆照作为参考，请严格沿用那张脸、发型、服饰和配色，不要重新设计。"
    "画风：阶梯状像素轮廓、大块发束、每种材质 3—4 档硬边色块，不要渐变和柔光。"
    "普通头像是 1024×1024 正方形；三种表情差分是 3072×1024 横版、三个等大格并排。"
    "纯洋红 #FF00FF 背景，图中不要文字。每次只出一张图，不拼四宫格。先确认，等我发任务。"
)


OPENING_WIDE = (
    "接下来我会在这个对话里，为中国风像素游戏《仙骰棋局》制作 16:9 横版画面。"
    "画风：阶梯状像素硬边、大块色块、每种材质 3—4 档明暗，不要渐变、柔光、发光和写实厚涂。"
    "画面里不要出现任何可读文字、数字、签名或伪汉字。每次只出一张图，不要拼四宫格。先确认，等我发任务。"
)


def log(m):
    _sw.touch()
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


async def shot(pg, name):
    try:
        await pg.screenshot(path=os.path.join(DEBUG, f"cdp_{name}.png"))
    except Exception:
        pass


JS_NOT_BUSY = """() => {
    // 生成中会出现 stop 按钮。两个选择器都要看：
    // data-testid 变过好几次，aria-label 才是稳定的那个
    //（2026-11-03 事故：只认 data-testid，页面明明在生成却判"空闲"，
    //  紧接着粘贴 0/210、整条判失败）。
    if (document.querySelector('button[data-testid="stop-button"]')) return false;
    if (document.querySelector('button[aria-label="停止"], button[aria-label="Stop"]')) return false;
    const el = document.querySelector('div.ProseMirror[contenteditable="true"]');
    if (el && el.getAttribute('aria-disabled') === 'true') return false;
    const ts = [...document.querySelectorAll('[data-talvt-turn-state]')];
    return !ts.some(t => t.getAttribute('data-talvt-turn-state') !== 'complete');
}"""

JS_HAS_COMPOSER = """() => !!document.querySelector('div.ProseMirror[contenteditable="true"]')"""

JS_SEND_READY = """() => {
    // 注意：空输入框时发送按钮本来就是 disabled，所以这个只能在「已填入内容」后调用
    const b = document.querySelector('button[data-testid="send-button"]')
        || document.querySelector('button[aria-label="发送"]')
        || document.querySelector('button[aria-label="Send"]')
        || [...document.querySelectorAll('button[type="submit"]')].find(x => !x.disabled);
    return !!(b && !b.disabled);
}"""

JS_CLICK_SEND = JS_SEND_READY.replace(
    "return !!(b && !b.disabled);",
    "if (b && !b.disabled) { b.click(); return true; } return false;")

JS_CLICK_NEWCHAT = """() => {
    const b = document.querySelector('button[data-testid="create-new-chat-button"]')
        || [...document.querySelectorAll('button,a')].find(x => (x.getAttribute('aria-label')||'') === '新聊天');
    if (b) { b.click(); return true; }
    return false;
}"""


_STUCK = {"v": False}     # 页面卡在加载中过：失败时据此让下次重启浏览器


async def _page_state(pg) -> dict:
    """一次拿到页面状态，省得三个 evaluate 各挂一次。"""
    try:
        return await pg.evaluate("""() => ({
            ready: (document.readyState || '?'),
            composer: !!document.querySelector('div.ProseMirror'),
            stop: !!document.querySelector('button[aria-label="停止"]'),
            text: document.body ? document.body.innerText.trim().length : 0
        })""") or {}
    except Exception:
        return {}


async def wait_not_busy(pg, timeout=180, blank_grace=20, reload_stuck=True):
    """等页面能输入（有输入框、没有 stop 按钮）。

    2026-11-03 加了「卡在加载中」的判定与自救：页面能显示侧栏、但主区域空白、
    ``document.readyState`` 一直停在 ``loading``、输入框永远不出现。
    用户看到的是"跑图失败"，日志写的是"页面一直在生成中"——两句话都不对。
    现在识别出来就刷新一次，还不行就记一笔，下次让浏览器重启。
    """
    t0 = time.time()
    blanks = 0
    stuck_since = None
    reloaded = False
    last: dict = {}
    while time.time() - t0 < timeout:
        try:
            if await pg.evaluate(JS_NOT_BUSY) and await pg.evaluate(JS_HAS_COMPOSER):
                return True
        except Exception:
            pass

        st = await _page_state(pg)
        if st:
            last = st
            loading = (not st.get("composer")) and (not st.get("stop")) and st.get("ready") != "complete"
            if loading:
                stuck_since = stuck_since or time.time()
                if reload_stuck and not reloaded and time.time() - stuck_since > 25:
                    log("    .. 页面卡在加载中（readyState=%s），刷新一次" % st.get("ready"))
                    reloaded = True
                    stuck_since = None
                    try:
                        await pg.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await pg.wait_for_timeout(2500)
                    continue
            else:
                stuck_since = None

            if int(st.get("text") or 0) < 10:
                blanks += 1
                if blanks >= blank_grace:        # 约 24 秒
                    log("!! 页面一直是空白的：连不上 chatgpt.com")
                    try:
                        from .. import platform as _plat

                        _sp = getattr(_plat.impl, "system_proxy", lambda: "")()
                        if _sp:
                            log("   当前系统代理是 %s。如果这不是你正在用的代理软件，"
                                "把它改回去——下载工具（res-downloader 之类）经常改完不还原" % _sp)
                    except Exception:
                        pass
                    log("   也可能是节点太慢：换一个节点再试")
                    return False
            else:
                blanks = 0

        await pg.wait_for_timeout(1200)

    if last and not last.get("composer") and not last.get("stop")             and last.get("ready") != "complete":
        _STUCK["v"] = True
        log("!! 页面一直没加载完（readyState=%s）：不是提示词的问题，会重启这个号的浏览器"
            % last.get("ready"))
    return False


async def dismiss_survey(pg, tag=""):
    """关掉「你更喜欢哪一张图像？」偏好问卷（实现在 survey.py，可单独测）。"""
    try:
        import survey
        return await survey.dismiss(pg, tag)
    except Exception:
        return False


def _write_result(out_dir, ok=0, fail=0, flags=None, states=None, extra=None) -> None:
    """薄封装：把本进程的全局计数塞进 driver/result 的写函数（那边才可单测）。"""
    _extra = dict(extra or {})
    if QUOTA_HIT["v"] and QUOTA_HIT.get("reset_at"):
        _extra["reset_at"] = QUOTA_HIT["reset_at"]
    result.write(out_dir, ok=ok, fail=fail, quota=bool(QUOTA_HIT["v"]),
                 paste_failed=int(PASTE_FAIL["n"]), flags=flags, states=states, extra=_extra)


async def _open_conversation(pg, text, tag="开场", tries=2) -> bool:
    """把开场白发出去。失败先重载页面再来一次，两次都不行才放弃。

    为什么值得重试：开场白失败几乎都是"页面还没就绪/编辑器还只读"，
    重载一次、等编辑器可写，通常就过了；直接退出等于把整批图都扔了。
    """
    for i in range(tries):
        try:
            if await send(pg, text, tag):
                return True
            why = "发送后没进入生成状态"
        except PasteFailed as e:
            why = "提示词没进输入框（%s）" % e
        log(f"    !! {tag} 第 {i + 1}/{tries} 次失败：{why}")
        if i + 1 >= tries:
            return False
        await _reload_page(pg, "%s 失败，重载再来" % tag)
        try:
            await _paste_mod.wait_editable(pg, 20)
        except Exception:
            pass
    return False


from . import paste as _paste_mod  # noqa: E402 —— 放在这里是为了不打乱上面的工具函数
from . import result  # noqa: E402
from . import stuckwatch as _sw  # noqa: E402
from . import session_health  # noqa: E402
from . import cf_pass  # noqa: E402
from . import pageguard as _pg_guard  # noqa: E402
from .paste import PasteFailed, paste as _paste  # noqa: E402
_paste_mod.set_logger(log)


async def _page_busy(pg) -> bool:
    """页面是不是还在生成（输入框被占着）。"""
    try:
        return not bool(await pg.evaluate(JS_NOT_BUSY))
    except Exception:
        return False


async def _paste_when_free(pg, text, tag, budget: float = 150.0) -> None:
    """粘贴提示词。碰到"页面还在生成、输入框禁用"时，等它空下来再试。

    2026-11-03 事故：开场白刚发出去、模型还在回，三种粘贴方式都只进 0/210，
    整条直接判失败——其实再等 20 秒就能发出去。用户看到的是"跑很久然后失败"。
    """
    t0 = time.time()
    for attempt in (1, 2):
        try:
            await _paste(pg, text, tag)
            return
        except PasteFailed:
            left = budget - (time.time() - t0)
            if attempt == 2 or left < 30 or not await _page_busy(pg):
                raise
            log("    .. %s 输入框被占着（页面还在生成），等它空下来再试" % tag)
            if not await wait_not_busy(pg, min(120.0, left)):
                raise


async def send(pg, text, tag=""):
    for att in range(1, 4):
        if att > 1:
            # 上一次点击可能其实已经发出去了，只是「是否进入生成态」没判定到。
            # 不查就重发会把同一个任务投两遍，白烧一张额度还会串图。
            try:
                if find_turn(await get_turns(pg), tag):
                    log(f"    .. {tag} 回合已出现，判定上一次点击其实成功")
                    return True
            except Exception:
                pass
        await dismiss_survey(pg, tag)      # 有偏好问卷就轻试一次关掉（不影响发送，失败也继续）
        # 60 秒足够区分"正在生成"和"页面根本没加载"。原来给 180 秒，
        # 冷启动遇上空白页/没网时要白等三分钟才报一句超时。
        if not await wait_not_busy(pg, 60):
            log(f"    !! {tag} 页面没准备好 (第{att}次，原因见上一行)")
            await shot(pg, f"busy_{att}")
            continue
        try:
            # 全部走 JS，避免 locator 在 CDP 下挂起
            try:
                await _paste_when_free(pg, text, tag)
            except PasteFailed as e:
                # 不许发半句话：宁可这条明确失败，也不能让 ChatGPT 收到残句
                # （残句不触发生图，调用方还以为是提示词的问题，来回改措辞白折腾）
                log(f"    !! {tag} 提示词没进输入框 {e}，这条不发（避免发半句话）")
                raise

            clicked = False
            has_attach = (await _composer_previews(pg)) > 0
            for _ in range(40):          # 长提示词时按钮 enable 很慢，给到 32 秒
                if await pg.evaluate(JS_SEND_READY):
                    if has_attach:
                        # 有附件时 JS click 常被吞掉，必须用真实鼠标点击
                        try:
                            await pg.locator('button[aria-label="发送"]').first.click(timeout=4000)
                            clicked = True
                        except Exception:
                            clicked = await pg.evaluate(JS_CLICK_SEND)
                    else:
                        clicked = await pg.evaluate(JS_CLICK_SEND)
                    break
                await pg.wait_for_timeout(800)
            if not clicked:
                log(f"    !! {tag} 按钮未就绪，改用回车")
                await pg.evaluate("""() => {
                    const el=document.querySelector('div.ProseMirror[contenteditable="true"]');
                    if (el) { el.focus(); const r=document.createRange();
                        r.selectNodeContents(el); r.collapse(false);
                        const s=getSelection(); s.removeAllRanges(); s.addRange(r); }
                }""")
                await pg.wait_for_timeout(300)
                await pg.keyboard.press("Enter")
            await pg.wait_for_timeout(2000)

            left = await pg.evaluate("""() => {
                const el=document.querySelector('div.ProseMirror[contenteditable="true"]');
                return el ? (el.innerText||'').trim().length : 0;
            }""")
            # 成功判据：出现「停止」按钮（说明已开始生成）或输入框已清空。
            # 长提示词 + 附件时，输入框要好几秒才清空，不能拿它做即时判据。
            sent_ok = False
            for _ in range(30):
                await pg.wait_for_timeout(1000)
                st2 = await pg.evaluate("""() => {
                    const el = document.querySelector('div.ProseMirror');
                    const stop = document.querySelector('button[aria-label="停止"]');
                    const send = document.querySelector('button[aria-label="发送"]');
                    return {len: el ? (el.innerText||'').trim().length : 0,
                            stop: !!stop, noSend: !send};
                }""")
                if st2["stop"] or st2["noSend"] or st2["len"] < 10:
                    sent_ok = True
                    break
            if not sent_ok:
                log(f"    !! {tag} 点击后未进入生成状态，重试")
                continue

            log(f"    >> 发送成功 {tag}")
            return True
        except PasteFailed:
            raise
        except Exception as e:
            log(f"    !! {tag} 异常: {str(e)[:70]}")
            await pg.wait_for_timeout(1200)
    return False


FILE_INPUT_SEL = 'input[type=file][accept="image/*"][multiple]'


async def _pick_input(pg):
    """页面上有 4 个 file input，只有『image/* 且 multiple』那个接的是正常的附件上传流程。
    用 .first 会拿到只收单图的隐藏框，setInputFiles 直接超时——这就是长期以来上传失败的原因。"""
    for sel in (FILE_INPUT_SEL,
                'input[type=file][accept*="image"]',
                'input[type=file]'):
        loc = pg.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        if n:
            return loc.first, sel
    return None, ""


def _cdp_up(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


async def _composer_counts(pg):
    """数输入框里的附件，返回三种口径。

    只用 blob: 一种口径太脆：上传完成后缩略图的 src 可能被换成别的协议，
    计数就会从 3 掉到 0/1，于是「明明挂上了却判数量不足」（R158 就是这么失败的）。
    这里同时数 blob 图、附件块、以及 form 内所有图，任一口径达标即可。
    """
    try:
        return await pg.evaluate("""() => {
            const form = document.querySelector('form');
            if (!form) return {blob: 0, chips: 0, all: 0};
            const imgs = [...form.querySelectorAll('img')];
            const blob = imgs.filter(x => (x.src || '').startsWith('blob:')).length;
            const chips = form.querySelectorAll(
                '[data-testid*="attach"], [class*="attach"], [aria-label*="移除"], [aria-label*="Remove"]'
            ).length;
            return {blob: blob, chips: Math.min(chips, imgs.length || chips), all: imgs.length};
        }""")
    except Exception:
        return {"blob": 0, "chips": 0, "all": 0}


async def _composer_previews(pg):
    """兼容旧调用：返回最宽松的可用数。"""
    try:
        c = await _composer_counts(pg)
        return max(c.get("blob", 0), c.get("chips", 0), c.get("all", 0))
    except Exception:
        return 0


async def _reload_page(pg, why=""):
    """刷新一次页面并等输入框回来。

    用于清掉「上一条发送没生效」留下的半卡死状态：那种状态下文件输入框会消失，
    紧接着的下一条必然报「找不到可用的文件输入框」，白白废掉一张。
    """
    try:
        await pg.reload(wait_until="domcontentloaded")
    except Exception:
        try:
            await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("    !! 刷新失败: %s" % str(e)[:50])
            return False
    for _ in range(20):
        await pg.wait_for_timeout(1000)
        try:
            if await pg.evaluate("()=>!!document.querySelector('div.ProseMirror')"):
                break
        except Exception:
            pass
    await pg.wait_for_timeout(1200)
    log("    已刷新页面" + (("（" + why + "）") if why else ""))
    return True


async def upload_refs(pg, paths, wait_ready=True):
    """上传参考图：每次重取输入框、每次校验预览是否真的出现。
    返回 False 时调用方必须放弃本条，绝不降级成纯文字。"""
    for one in paths:
        ok = False
        reloaded = False
        for attempt in range(3):
            loc, sel = await _pick_input(pg)
            if loc is None:
                # 页面可能处于半卡死状态（上一条发送未生效留下的）：刷一次再找，
                # 否则这一张必然报废，还会把下一张一起带死。
                if not reloaded:
                    reloaded = True
                    log("    !! 找不到可用的文件输入框，刷新后重试")
                    await _reload_page(pg, "输入框丢失")
                    continue
                log("    !! 找不到可用的文件输入框")
                break
            try:
                await loc.set_input_files(one)
            except Exception as e:
                log(f"    .. 第{attempt+1}次 setInputFiles 失败: {str(e)[:46]}")
                await pg.wait_for_timeout(2000)
                continue
            # 等预览出现（最多 12 秒），出现即算这张成功
            for _ in range(12):
                await pg.wait_for_timeout(1000)
                if await _composer_previews(pg) >= 1:
                    ok = True
                    break
            if ok:
                log(f"    参考图已附上（{os.path.basename(one)}）")
                break
            log(f"    .. 第{attempt+1}次上传后没出现预览，重试")
            await pg.wait_for_timeout(1500)
        if not ok:
            log("    !! 参考图始终没附上")
            return False

    if not wait_ready:
        return True
    # 多张时确认数量够（双口径：blob / 附件块 / form 内所有图）
    need = len(paths)
    last = {}
    for i in range(60):
        await pg.wait_for_timeout(1000)
        c = await _composer_counts(pg)
        last = c
        if c["blob"] >= need or c["chips"] >= need or (c["blob"] >= 1 and c["all"] >= need):
            how = "blob" if c["blob"] >= need else ("chips" if c["chips"] >= need else "all")
            log(f"    参考图就绪（{i+1}s，{need} 张，口径 {how}：blob={c['blob']} chips={c['chips']} all={c['all']}）")
            return True
    log(f"    !! 参考图数量不足（要 {need} 张；blob={last.get('blob')} "
        f"chips={last.get('chips')} all={last.get('all')}）")
    return False


async def rename_conversation(pg, title):
    """把会话标题改成可辨识的名字，侧栏里一眼能认出是哪一轮。

    为什么必须走接口：侧栏标题是模型从首轮问答自己生成的，我们同一套开场白
    会让每轮都叫「确认出图规则/要求」，塞标签、让它复述都没用（实测无效）。
    这个接口只认 Bearer token，光靠 cookie 会 401；token 从 /api/auth/session 取。
    失败只记一行，绝不影响出图。
    """
    js = """async ([cid, title]) => {
      let tok = '';
      try { tok = (await (await fetch('/api/auth/session')).json()).accessToken || ''; } catch (e) {}
      const h = {'Content-Type': 'application/json'};
      if (tok) h['Authorization'] = 'Bearer ' + tok;
      const r = await fetch('/backend-api/conversation/' + cid, {
        method: 'PATCH', headers: h, body: JSON.stringify({title})
      });
      return {status: r.status, ok: r.ok};
    }"""
    try:
        cid = (pg.url or "").rstrip("/").split("/")[-1]
        if len(cid) < 20:
            return False
        r = await pg.evaluate(js, [cid, str(title)[:60]])
        if r.get("ok"):
            log(f"    会话标题 → {str(title)[:44]}")
            return True
        log(f"    .. 改标题失败（HTTP {r.get('status')}），不影响出图")
    except Exception as e:
        log(f"    .. 改标题异常：{str(e)[:50]}")
    return False


def _registry_path() -> str:
    return os.path.join(_DATA, "state", "conversations.jsonl")


def record_conversation(cid: str, label: str = "") -> None:
    """把这次开的会话记进名单。

    这是删会话时**唯一**可信的依据：标题是模型自动起的（「出图要求」「生成单张图」），
    和用户自己聊的主题分不开，靠标题认必然误删。记一行文本，失败了也绝不影响出图。
    """
    if not cid or len(cid) < 20:
        return
    try:
        p = _registry_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        line = json.dumps({"id": cid, "ts": time.time(), "job": str(label)[:60]},
                          ensure_ascii=False)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _title_from_opening(txt):
    """开场白的第一行就是标题（【项目·R编号·简报·时间】）。"""
    first = (txt or "").strip().splitlines()[0].strip() if (txt or "").strip() else ""
    return first if first.startswith("【") else ""


async def blob_srcs(pg):
    try:
        return await pg.evaluate("""() => [...document.querySelectorAll('img')]
            .filter(i=>(i.src||'').startsWith('blob:') && i.naturalWidth>300).map(i=>i.src)""")
    except Exception:
        return []


async def save_img(pg, src, fn):
    """把 blob 原样写到磁盘。返回 (实际路径, mime)。

    扩展名跟内容走：Images 2.6 的 GIF 以前被存成 .png，
    文件能用、但扩展名在骗人（2026-11-06 实测 12 帧 GIF）。
    """
    payload = await pg.evaluate("""async (u)=>{const r=await fetch(u);const b=await r.blob();
        const by=new Uint8Array(await b.arrayBuffer());let s='';
        for(let i=0;i<by.length;i++)s+=String.fromCharCode(by[i]);
        return {t:b.type||'', b64:btoa(s)};}""", src)
    data = base64.b64decode(payload.get("b64") or "")
    mime = str(payload.get("t") or "")
    base = str(fn)
    if base.lower().endswith(media_mod.IMAGE_EXTS):
        path = base                     # 中间文件（_c.png）保持原名
    else:
        path = base + media_mod.ext_for(mime, data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path, mime


JS_TURNS = """() => [...document.querySelectorAll('[data-talvt-turn-state]')].map(t => ({
    s: t.getAttribute('data-talvt-turn-state'),
    u: (t.querySelector('[data-content-search-unit-key$=":user"]')?.innerText || '').slice(0, 200),
    a: (t.querySelector('[data-content-search-unit-key$=":assistant"]')?.innerText || '').slice(0, 80),
    imgs: [...t.querySelectorAll('img')]
        .filter(i => (i.src||'').startsWith('blob:') && i.naturalWidth > 300)
        .map(i => i.src),
    uimgs: [...(t.querySelector('[data-content-search-unit-key$=":user"]')||t).querySelectorAll('img')]
        .filter(i => (i.src||'').startsWith('blob:') && i.naturalWidth > 300)
        .map(i => i.src),
    aimgs: [...(t.querySelector('[data-content-search-unit-key$=":assistant"]')||t).querySelectorAll('img')]
        .filter(i => (i.src||'').startsWith('blob:') && i.naturalWidth > 300)
        .map(i => i.src)
}))"""


async def get_turns(pg):
    try:
        return await pg.evaluate(JS_TURNS)
    except Exception:
        return []


def find_turn(turns, task):
    """按任务名定位回合。提示词首行是『…·任务名」。』
    任务名可能是「角色-子任务」，而提示词里是「角色·子任务」，两种都试。"""
    cands = {task, task.replace("-", "·"), task.replace("-", ""), task.split("-")[0]}
    keys = []
    for c in cands:
        if not c:
            continue
        # 末尾直接跟换行的独立标记行也算命中（run_round 会在 prompt 首行打 ·任务名）
        keys += [f"·{c}\n", f"·{c}", f"·{c}”", f"·{c}」", f"·{c}“", f"{c}·"]
    for t in reversed(turns):
        if any(k in t["u"] for k in keys):
            return t
    return None



def parse_reset_time(text, now=None):
    """从网页文字里解析「什么时候恢复」。

    网页通常写成：
      「你已达到上限，将在 17:30 后重置」/「3 小时后可再次生成」/「约 45 分钟后再试」
    也见过英文：You've reached the limit. Try again at 5:30 PM / in 3 hours.
    解析不了就返回 None，由调用方退回保守估计。
    """
    import datetime
    if not text:
        return None
    t = now or datetime.datetime.now()
    s = str(text)

    # 1) 相对时间：N 小时 / N 分钟
    m = re.search(r"(\d+)\s*(?:小时|个小时|hours?|hrs?)", s, re.I)
    if m:
        try:
            return (t + datetime.timedelta(hours=int(m.group(1)))).replace(microsecond=0).isoformat()
        except Exception:
            pass
    m = re.search(r"(\d+)\s*(?:分钟|分|minutes?|mins?)", s, re.I)
    if m:
        try:
            return (t + datetime.timedelta(minutes=int(m.group(1)))).replace(microsecond=0).isoformat()
        except Exception:
            pass

    # 2) 绝对时间：17:30 / 下午 5:30 / 5:30 PM
    m = re.search(r"(上午|下午|晚上|凌晨)?\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*(AM|PM|am|pm)?", s)
    if m:
        ampm = (m.group(1) or "") + (m.group(4) or "")
        hh, mm = int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            return None
        if ("下午" in ampm or "晚上" in ampm or ampm.upper() == "PM") and hh < 12:
            hh += 12
        if ("凌晨" in ampm or "上午" in ampm or ampm.upper() == "AM") and hh == 12:
            hh = 0
        try:
            dt = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if dt <= t:                      # 已经过了这个点 → 算明天，但多半是解析错，宁可不用
                return None
            return dt.isoformat()
        except Exception:
            return None
    return None


RESET_HINT_JS = """() => {
  const sep = String.fromCharCode(10);
  const t = (document.body && document.body.innerText) || '';
  const lines = t.split(sep).filter(l => /上限|limit|重置|reset|再试|try again|恢复|可用/i.test(l));
  return lines.slice(0, 6).join(sep);
}"""


async def reset_hint(pg, text):
    """先看助手正文，再看页面上带「上限/重置」字样的行。"""
    hint = parse_reset_time(text)
    if hint:
        return hint, "message"
    try:
        lines = await pg.evaluate(RESET_HINT_JS)
    except Exception:
        lines = ""
    hint = parse_reset_time(lines)
    if hint:
        return hint, "page"
    return None, ""


def _same_picture(path, refs, tol=0.6):
    """判断 path 与任一参考图是否为「同一张画面的复制」。

    用来挡住「把上传的参考图当成品交出去」这类事故：复制品与原件缩放后平均差 ≈ 0.00，
    而真生成哪怕只多一个对白泡，缩放后平均差也有几个单位，所以 0.6 不会误杀。
    """
    try:
        import numpy as _np
        im = Image.open(path).convert("RGB")
        if im.width > 320:
            im = im.resize((320, max(1, int(320 * im.height / max(im.width, 1)))))
        a = _np.asarray(im, dtype=_np.int16)
    except Exception:
        return False
    for r in (refs or []):
        try:
            b = r.convert("RGB").resize(im.size)
            if float(_np.abs(a - _np.asarray(b, dtype=_np.int16)).mean()) < tol:
                return True
        except Exception:
            continue
    return False


async def wait_img(pg, before, task, known, timeout=600, want=None, need_alpha=False,
                   refs=None):
    """优先：本任务回合内的图（最准）；兜底：发送后新出现的图（需过查重）。
    车辆/重复误判都源于只靠「新图」，所以回合优先是必须的。"""
    t0 = time.time()
    prev = set(before or [])
    last = 0
    dup_streak = 0
    retry_n = 0
    reject_log = {}          # 记录已经播报过的拒绝原因，避免每 2 秒刷一行
    ref_imgs = []
    for r in (refs or []):
        try:
            ref_imgs.append(Image.open(r))
        except Exception:
            pass
    while time.time() - t0 < timeout:
        await pg.wait_for_timeout(2000)
        turns = await get_turns(pg)
        t = find_turn(turns, task)

        # 方式一：回合内取图（只认 assistant 那一半的图）
        # 用户上传的参考图也在同一个 turn 元素里。以前把整个 turn 的 img 都当候选，
        # 结果会把「上一条上传的参考图」当成成品抓走——R188 S04 就这么交了一张
        # 与 173/S03 像素级相同的复制品，日志还显示 9 秒 OK（其实零生成）。
        _uimgs = set((t or {}).get("uimgs") or [])
        _aimgs = []
        if t:
            _aimgs = t.get("aimgs") or [s for s in t["imgs"] if s not in _uimgs]
        if _aimgs:
            for src in reversed(_aimgs):
                tmp = os.path.join(DEBUG, "_c.png")
                try:
                    await save_img(pg, src, tmp)
                except Exception as e:
                    if reject_log.get("save") != str(e)[:40]:
                        reject_log["save"] = str(e)[:40]
                        log(f"    .. {task} 存图失败：{str(e)[:50]}")
                    continue
                # 与本次上传的参考图逐像素相同 → 这是参考图被当成成品的复制品，不是新生成
                if _same_picture(tmp, ref_imgs):
                    if reject_log.get("refcopy") != src:
                        reject_log["refcopy"] = src
                        log(f"    .. {task} 抓到的是上传的参考图副本（并未生成），继续等")
                    if (t or {}).get("s") == "complete" and time.time() - t0 > 20:
                        log(f"    .. {task} 回合已结束且只有参考图副本，判定本条未生成")
                        return None
                    continue
                ok, why = is_good(tmp, set(), want, need_alpha)   # ok=可交付；why 是标注，不判死
                if ok:
                    return src
                # 图在，但校验不过：一定要说出理由，否则只能看到「无图」，白猜
                if reject_log.get("why") != why:
                    reject_log["why"] = why
                    try:
                        from PIL import Image as _I
                        _w, _h = _I.open(tmp).size
                        detail = f"{why} {_w}x{_h}"
                    except Exception:
                        detail = why
                    try:
                        import shutil as _sh
                        rejdir = os.path.join(DEBUG, "reject")
                        os.makedirs(rejdir, exist_ok=True)
                        _sh.copy2(tmp, os.path.join(rejdir, f"{task}_{int(t0)}.png"))
                        log(f"    .. {task} 图被拒（{detail}），样本已存 debug/reject/")
                    except Exception:
                        log(f"    .. {task} 图被拒（{detail}）")
                # 回合已经结束、且一直只有这一张不合格的图 → 再等也不会变，省下两三分钟
                if t["s"] == "complete":
                    if not reject_log.get("since"):
                        reject_log["since"] = time.time()
                    elif time.time() - reject_log["since"] > 45:
                        log(f"    .. {task} 抓到的图不合格（{reject_log.get('why') or '原因未知'}）且回合已结束，提前放弃")
                        return None
                else:
                    reject_log["since"] = 0

        # 方式二：新出现的图（仅在「完全找不到本任务回合」时才允许兜底）
        # —— 一旦定位到回合，就算它还没有图也必须继续等，否则会抓成上一条的图（串图事故的根源）
        cur = await blob_srcs(pg)
        cand = [s for s in cur if s not in prev]
        # 受控兜底：回合已存在但没有图，且本轮恰好只多出一张图 → 认为就是它
        # （严格限制「恰好 1 张」+「回合已 complete」，避免抓到上一条的图）
        if (cand and len(cand) == 1 and t is not None and t["s"] == "complete"
                and time.time() - t0 > 40):
            src = cand[0]
            tmp = os.path.join(DEBUG, "_c2.png")
            try:
                await save_img(pg, src, tmp)
                ok, why = is_good(tmp, set(), want, need_alpha)
                if ok:
                    log(f"    用兜底抓到本轮图（回合内无图，全局新增 1 张）")
                    return src
            except Exception:
                pass

        if cand and t is None and time.time() - t0 > 60:
            for src in reversed(cand):
                tmp = os.path.join(DEBUG, "_c.png")
                try:
                    await save_img(pg, src, tmp)
                except Exception:
                    continue
                ok, why = is_good(tmp, known, want, need_alpha)
                if ok:
                    return src
                if why == "与历史重复":
                    dup_streak += 1

        if cand and dup_streak >= 30 and t and t["s"] == "complete" and time.time() - t0 > 90:
            return None

        # 消息流报错（Edge 上常见「消息流中的错误」）：自动点重试并重置计时，最多 3 次
        if retry_n < 3:
            try:
                hit = await pg.evaluate("""() => {
                    const tx = document.body.innerText || '';
                    return {
                        err: tx.includes('消息流中的错误'),
                        btn: !!([...document.querySelectorAll('button')].find(b => (b.innerText||'').trim() === '重试')),
                        cf: tx.includes('cloudflare_challenge') || tx.includes('无法加载历史记录'),
                    };
                }""")
            except Exception:
                hit = {"err": False, "btn": False, "cf": False}
            if hit.get("cf"):
                # 2026-11-02 事故：ChatGPT 给这个浏览器会话弹 Cloudflare 挑战，消息发不出去，
                # 页面只留一个带「重试」的红条。机器点不动，得人来点。
                # 处理顺序：① 自动 reload 一次（实测能清掉红条）② 还不行就叫人 + 立刻失败，
                # 绝不再熬 540 秒。
                # 只有在开场还没确认时才敢刷新：确认之后 reload 会把这轮对话冲掉，
                # 后面的提示词就发进一个空会话（2026-11-02 实测：刷新后 no-turns，白跑）。
                if not _NEED_HUMAN["tried"]:
                    # 先自己过：等它自动过 → 点验证框 → 刷新。都失败才叫人。
                    _NEED_HUMAN["tried"] = True
                    log(f"    .. {task} 检测到 Cloudflare 挑战，先自己试（等/点/刷新）")
                    try:
                        ok, how = await cf_pass.try_auto_pass(
                            pg, log, allow_reload=not _OPENED["v"],
                            on_show=lambda: _show_my_window(getattr(args, "state_key", "")),
                            on_hide=lambda: _hide_my_window(getattr(args, "state_key", "")))
                    except Exception:
                        ok, how = False, "error"
                    if ok:
                        log(f"    .. {task} 挑战已解除（{how}），继续")
                        t0 = time.time()
                        continue
                if _OPENED["v"]:
                    # 开场已经发出去了：这时候再刷新会把对话冲掉，只能叫人。
                    if not _NEED_HUMAN["why"]:
                        _NEED_HUMAN["why"] = "cloudflare"
                        log(f"    !! {task} 自己过不去：{session_health.human_hint('challenged')}")
                        log("    !! 这单立刻判失败（不占额度、不再空转）；下次这个号会自动重启浏览器")
                    return None
                if not _NEED_HUMAN["why"]:
                    _NEED_HUMAN["why"] = "cloudflare"
                    # 把这个号挂起 10 分钟：后面的单别再去撞同一堵墙
                    try:
                        session_health.mark_paused(
                            _DATA, getattr(args, "state_key", ""), "cloudflare", 10)
                        log("    .. 已把这号挂起 10 分钟（后面的单不会再来撞）")
                    except Exception:
                        pass
                    log(f"    !! {task} 需要你点一下：{session_health.human_hint('challenged')}")
                    log("    !! 这单立刻判失败（不占额度、不再空转），处理完重投即可")
                return None
            if hit["err"] or hit["btn"]:
                try:
                    await pg.get_by_text("重试", exact=True).last.click(timeout=5000)
                    retry_n += 1
                    log(f"    .. {task} 消息流报错，已点重试（第{retry_n}次）")
                    t0 = time.time()
                    continue
                except Exception:
                    pass

        # 拒答/异常快速失败：助手回了「抱歉、违反、无法」之类且没有图 → 别干等
        if t and t["s"] == "complete" and not t["imgs"]:
            a = t["a"] or ""
            bare = a.replace("ChatGPT 说：", "").strip()
            # 先判额度/限流：这种失败不是提示词问题，硬重试只会一直烧时间
            if any(w in a for w in ("达到上限", "上限", "稍后", "限流", "limit", "Limit", "用量已达")):
                log(f"    !! QUOTA {task} 疑似达到生成上限: {a.replace(chr(10), " ")[:80]}")
                QUOTA_HIT["v"] = True
                try:
                    _hint, _src = await reset_hint(pg, a)
                except Exception:
                    _hint, _src = None, ""
                if _hint:
                    # 记下来传给上层：只写日志的话，面板永远显示"撞上限 0 次"，
                    # 用户会以为账号没事，反复重投反复失败（2026-11-04 反馈包实锤）
                    QUOTA_HIT["reset_at"] = _hint
                    log(f"    !! RESET_AT={_hint}  # 来源：{_src}")
                else:
                    log("    .. 页面上没读到重置时间，按保守估计冷却")
                return None
            # 必须有实打实的文字才判拒答；只有前缀「ChatGPT 说：」的空壳不算
            if len(bare) >= 4 and any(w in bare for w in ("抱歉", "违反", "无法", "不能", "sorry", "Sorry", "can't")):
                # 打一个能被上层识别的记号：这不是"模型没画"，是"它不肯画"。
                log(f"    !! {task} 被拒答(内容政策): {bare[:50]}")
                return None
            # 服务端生图失败（"由于我这边发生了错误，我未能生成图片" / "图片生成失败"）：
            # 这是 OpenAI 侧抖动，不是提示词或参考图的问题。原地重发一次，
            # 比让主模型整轮重投便宜得多（2026-10-27 晚大面积出现）。
            if len(bare) >= 4 and any(w in bare for w in ("未能生成图片", "图片生成失败", "发生了错误")):
                log(f"    !! {task} 服务端生图失败: {bare[:40]}")
                return "__RETRY__"
            # 空回复（只有「ChatGPT 说：」）也一样，别干等 600 秒
            # 图片可能比文字晚几秒才挂到 DOM，别在 50 秒就判空；等到接近超时才放弃
            # 2026-11-06 用户反馈：网慢时图会晚几十秒才慢慢显出来，我们 180 秒就判"没成"，
            # 判完图又出来了。这里放宽到 300 秒，并且把"回合结束但没图"单独记账，
            # 由上层判成「结果不确定」而不是「没画出来」。
            if _LOCAL_CHAT["url"] or _NEED_HUMAN["why"]:
                _empty_after = 60
            else:
                _empty_after = 300
            if len(bare) < 6 and time.time() - t0 > _empty_after:
                log(f"    !! {task} 空回复，跳过（回合已结束但一直没读到图）")
                _EMPTY_TURN["n"] += 1
                return None

        el = time.time() - t0
        if el - last >= 30:
            last = el
            st = f"{t['s']}|{t['a'][:30]}" if t else ("none" if turns else "no-turns")
            log(f"    .. {task} {int(el)}s 回合{len(turns)} 本项图{len(t['imgs']) if t else 0} "
                f"新增{len(cand)} 重复{dup_streak} 末轮={st}")
        try:
            await pg.mouse.wheel(0, 700)
        except Exception:
            pass
    return None


_LOCAL_CHAT = {"url": ""}     # 本次运行是否建成了"本地草稿"会话（消息根本没发出去）
_NEED_HUMAN = {"why": "", "tried": False}   # 需要人点一下（Cloudflare 等）；tried=已自动尝试过
_OPENED = {"v": False}     # 开场是否已确认。确认之后再 reload 会把对话冲掉，绝对不能做
# 回合已经结束、却一直没读到图（网慢时图会比文字晚几十秒才挂到 DOM）。
# 单独计数：这种不能判"没画出来"，要判"不确定，去网页看一眼"（2026-11-06 用户实测）。
_EMPTY_TURN = {"n": 0}


def _conn_lost(e) -> bool:
    """这个异常是不是「浏览器/页面没了」。

    2026-10-30 实测（R96/S07、R99/X10）：`wait_for_timeout` 抛 ChannelClosed，
    异常一路冒到 `asyncio.run()` 外面，驱动直接死，`_result.json` 一个字节都没写，
    上层只好记成 RENDER_FAILED。用户看到「渲染失败」就去改提示词——方向完全错了。
    """
    text = (str(e) or "").lower()
    return any(k in text for k in (
        "target closed", "target page, context or browser has been closed",
        "connection closed", "channel was closed", "browser has been closed",
        "has been closed", "browser has disconnected", "websocket"))


async def wait_img_or_lost(pg, before, task, known, **kw):
    """等图；浏览器断了就返回 `__LOST__`，让主循环把这一条标死并收手。

    不就地重连：页面都没了，重连也是从头来一遍；交给上层重开浏览器更干净。
    """
    try:
        return await wait_img(pg, before, task, known, **kw)
    except Exception as e:  # noqa: BLE001
        if _conn_lost(e):
            return "__LOST__"
        raise


def _show_my_window(state_key: str, seconds: float = 180.0) -> None:
    """把本账号的浏览器亮出来，并给它一段"别藏"租约。

    Cloudflare 的托管质询要求窗口可见；半瓢平时一直藏着跑，所以必须先亮。
    亮的时候顺手写 phase=shown 租约，否则 0.8 秒一轮的静默巡逻会立刻把它藏回去。
    """
    from .. import config as _c, login_flow
    from .. import platform as _plat
    from . import browser as _br

    cfg = _c.load(None, missing_ok=True)
    h = _br.read_handle(cfg, state_key)
    if not h:
        return
    login_flow.write_phase(cfg, state_key, "shown", browser_pid=h.pid,
                           until=time.time() + seconds)
    _plat.show_window(h.pid, activate=True)


def _hide_my_window(state_key: str) -> None:
    from .. import config as _c, login_flow

    cfg = _c.load(None, missing_ok=True)
    login_flow.write_phase(cfg, state_key, "idle")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True)
    ap.add_argument("--ids", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--state-key", required=True)
    ap.add_argument("--port", type=int, default=9444)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--rotate", type=int, default=8)
    ap.add_argument("--conv", default="")
    ap.add_argument("--launch", action="store_true", help="自己启动浏览器")
    ap.add_argument("--profile", default="", help="账号专用 profile 目录（必填）")
    ap.add_argument("--engine", default="edge", choices=["edge", "chrome"])
    ap.add_argument("--exe", default="", help="浏览器可执行文件；不给就按 engine 探测")
    ap.add_argument("--items", default=ITEMS, help="items 清单文件")
    ap.add_argument("--ref", default="", help="开场随附的画风参考图，多个用 ; 分隔")
    ap.add_argument("--opening", default="icon", choices=["icon", "char", "portrait", "wide"])
    ap.add_argument("--opening-text", default="", help="直接给开场文本，覆盖内置开场（风格中立）")
    ap.add_argument("--probe", action="store_true",
                    help="只连上去看看这一页是什么（登录/未登录），不出图")
    args = ap.parse_args()
    main.args = args          # 崩掉时兜底写结果要用（见文件末尾）
    # 从这一刻开始盯：进程活着但日志不动 = 卡死，7 分钟后自己退（锁随进程释放）
    _sw.start()

    proc = None
    if args.launch and not _cdp_up(args.port):
        exe = args.exe or _find_browser(args.engine)
        if not exe:
            log(f"!! 找不到 {args.engine}，请用 --exe 指定可执行文件")
        proc = launch_browser(args.profile, args.port, exe)
    elif args.launch:
        log(f"端口 {args.port} 已有实例，直接复用，不再启动/关闭浏览器")

    items = [x for x in json.load(open(args.items, encoding="utf-8")) if x["cat"] == args.cat]
    if args.ids:
        only = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [x for x in items if x["id"] in only]
    st_all = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    st = st_all.setdefault(args.state_key, {})
    os.makedirs(args.out, exist_ok=True)

    known = set()
    for f in glob.glob(os.path.join(args.out, "*.png")):
        try:
            known.add(dhash(f))
        except Exception:
            pass
    try:
        from .. import __version__ as _cv
    except Exception:
        _cv = "?"
    # 一行版本，排查时能直接看出这批单是哪个客户端跑的。
    # 2026-10-31 之前日志里没有版本，只能靠 git 反查 cdp_run.py 行号，靠不住。
    log(f"半瓢 {_cv} · Python {sys.version.split()[0]} · {sys.platform}")
    log(f"历史图 {len(known)} 张参与查重")

    async with async_playwright() as p:
        log("连接 CDP…")
        b = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        log("CDP 已连接")
        ctx = b.contexts[0]
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        _pg_guard.guard_page(pg, log=log)
        if not pg.url.startswith("http"):
            log("当前页是空白页，导航到 chatgpt.com")
            try:
                await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
            except Exception as e:
                log(f"导航失败：{e}")
        log(f"当前页 {pg.url[:80]}")

        if args.probe:
            # 判登录不能看界面文案（data-testid 会变、语言会变），看会话接口最稳。
            info = {}
            # 要轮询：页面刚加载时 composer 还没渲染出来，
            # 一次就读会得到"已登录但没输入框"这种自相矛盾的结论（真发生过）。
            for _i in range(12):
                info = await pg.evaluate(_PROBE_JS)
                if info.get("composer") or not info.get("logged_in"):
                    break
                await pg.wait_for_timeout(1000)
            print(json.dumps({"ok": True, "probe": info}, ensure_ascii=False))
            return

        conv = args.conv
        if conv and os.path.exists(conv):
            conv = open(conv, encoding="utf-8").read().strip()
        if conv:
            log(f"续用对话 {conv}")
            await pg.goto(conv, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(5000)
        else:
            log("打开首页…")
            try:
                await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"goto 异常(继续): {str(e)[:60]}")
            await pg.wait_for_timeout(5000)
            log("首页已加载")
            await shot(pg, "home")
            try:
                body = await pg.evaluate("document.body?document.body.innerText.slice(0,120):''")
            except Exception:
                body = ""
            log(f"页面文本: {body[:60]!r}")
            # 有输入框就用当前会话，但**必须是空的**。
            # 2026-11-03 事故：chatgpt.com 会把上一单的对话恢复出来，程序接着用，
            # 于是 (a) 开场白发进旧对话、URL 不变，「开场确认」白等 120 秒；
            # (b) 上一单还在收尾时输入框是禁用态，提示词 0/210 全失败。
            has_box = await pg.evaluate(
                "() => !!document.querySelector('div.ProseMirror[contenteditable=\"true\"]')")
            turns = 0
            if has_box:
                try:
                    turns = int(await pg.evaluate(
                        "() => document.querySelectorAll('article[data-testid^=\"conversation-turn\"], main article').length"))
                except Exception:
                    turns = 0
            if has_box and not turns:
                log("当前会话是空的，直接使用")
            elif has_box and turns:
                log("当前页是上一单的对话（%d 轮），不接着用" % turns)
                if await pg.evaluate(JS_CLICK_NEWCHAT):
                    await pg.wait_for_timeout(3500)
                    log("已点新对话")
                else:
                    await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
                    await pg.wait_for_timeout(4000)
                    log("已重新打开首页（没找到新对话按钮）")
            elif await pg.evaluate(JS_CLICK_NEWCHAT):
                log("已点新对话")
                await pg.wait_for_timeout(3500)
            # 登录态检查：不管新建还是续用会话都要查一遍。
            # 掉登录时每条都会静默失败（无图），必须单独报出来，否则看起来像"模型不出图"。
            # 判据必须同时满足「没有输入框」+「出现登录字样」，且要复检几次。
            # 光看文本会误判：刚登录/刚打开时页面外壳还在渲染，页头侧栏会短暂出现「登录」，
            # 2026-10-28 就是这样把一条好通道判成掉登录，整轮少出 3 张。
            LOGIN_WORDS = ("登录", "Log in", "Sign in", "Continue with", "使用 Google")

            async def looks_logged_out():
                try:
                    if await pg.evaluate(JS_HAS_COMPOSER):
                        return False
                    body = await pg.evaluate("()=>(document.body&&document.body.innerText)||''")
                except Exception:
                    return False          # 读不到就别判死，宁可试一次
                return any(k in body for k in LOGIN_WORDS)

            still_out = False
            for i in range(4):
                still_out = await looks_logged_out()
                if not still_out:
                    break
                log(f"   .. 第 {i + 1} 次判定疑似掉登录，等 2.5 秒复检")
                await pg.wait_for_timeout(2500)
            if still_out:
                log("!! AUTH 多次复检都没有输入框且出现登录字样，判定为未登录")
                raise SystemExit(9)

            if args.ref:
                refs = [x for x in args.ref.split(";") if x.strip() and os.path.exists(x.strip())]
                if refs:
                    await upload_refs(pg, refs)
            open_txt = args.opening_text or {"icon": OPENING, "char": OPENING_CHAR, "portrait": OPENING_PORTRAIT, "wide": OPENING_WIDE}[args.opening]
            # 无论用默认还是主模型自带的开场白，都补上这条硬要求：
            # 模型偶尔一次给两张并问「你更喜欢哪张」，会多烧一份额度、也让取图变成碰运气。
            if "不要问「你更喜欢哪张」" not in open_txt:
                open_txt = open_txt.rstrip() + "\n每次只出一张图，不要给两个候选版本，不要问「你更喜欢哪张」，也不要给选择按钮。"
            if not await _open_conversation(pg, open_txt):
                await shot(pg, "opening_fail")
                # 这里原来直接 return，**连 _result.json 都不写** —— 上层只能归成
                # RENDER_FAILED（看起来像"模型没画出来"），于是用户和 AI 都在猜
                # 额度/登录/提示词。实测就吃过这个亏（2026-10-30 两位用户）。
                scene = await _paste_mod.describe(pg)
                log(f"!! 开场白没能发出去（不是额度问题），这批不跑；现场：{scene}")
                _write_result(args.out, ok=0, fail=1,
                              extra={"paste_failed": 1, "reason": "opening", "scene": scene})
                return
            t0 = time.time()
            while time.time() - t0 < 60:
                await pg.wait_for_timeout(2000)
                cur = pg.url or ""
                # 会话一建出来、页面空了就继续，不必等模型把开场白答完。
                # 2026-11-03 实测：等完整回合要 121 秒，用户以为卡死了。
                if "/c/" in cur and "local-chatgpt" not in cur and not await _page_busy(pg):
                    break
                try:
                    ts = await pg.evaluate("""()=>[...document.querySelectorAll('[data-talvt-turn-state]')].map(t=>({
                        s:t.getAttribute('data-talvt-turn-state'),
                        a:(t.querySelector('[data-content-search-unit-key$=":assistant"]')?.innerText||'').slice(0,20)}))""")
                except Exception:
                    ts = []
                if ts and ts[-1]["s"] == "complete" and ts[-1]["a"]:
                    break
            open(os.path.join(DEBUG, f"conv_{args.state_key}.txt"), "w", encoding="utf-8").write(pg.url)
            log("开场确认 %s（等了 %.0f 秒）" % (pg.url, time.time() - t0))
            _OPENED["v"] = True
            if "local-chatgpt" in (pg.url or ""):
                # 2026-11-02 实测：页面在"项目加载失败"时会建出一个本地草稿会话
                # （URL 形如 /c/local-chatgpt%3A<uuid>），消息**根本没发到服务器**，
                # 之后只会看到一个空回合。以前要白等 9 分钟才判空回复。
                _LOCAL_CHAT["url"] = pg.url
                # 让下一次 ensure() 重启这个号的浏览器：实测只有重启能救回草稿态
                try:
                    session_health.mark_broken(
                        _DATA, getattr(args, "state_key", ""), "local_draft")
                except Exception:
                    pass
                log("!! 会话是本地草稿（local-chatgpt）：消息没真正发出去，本轮会很快失败")
                # 立刻收手：这种会话里发什么都不算数，再等 2 分钟只是让用户干等。
                # 上层看到 note 里的"本地草稿"会自动换一个号重试（2026-11-04）。
                scene = await _paste_mod.describe(pg)
                _write_result(args.out, ok=0, fail=1,
                              extra={"paste_failed": 1, "reason": "local_draft", "scene": scene})
                return
            _cid = (pg.url or "").rstrip("/").split("/")[-1]
            record_conversation(_cid, getattr(args, "state_key", ""))
            _t = _title_from_opening(open_txt)
            if _t:
                await rename_conversation(pg, _t)

        ok = fail = 0
        sent_count = 0
        flags = {}          # 可交付但需要主模型留意的点（比例/尺寸/底色）
        sessions = {}       # {mid: 会话URL} —— 隔离是否真的实现，看这个
        for idx, it in enumerate(items):
            key = it["id"]
            task = it["task"]
            if st.get(key) == "ok" and media_mod.find_output(args.out, key, task):
                continue
            # 不带后缀：save_img 按 blob 的 MIME 决定 .png / .gif
            fn = os.path.join(args.out, f"{key}_{task}")

            # --alpha 的洋红要求必须落到**每一条提示词**上。
            # 以前它只出现在 opening（开场白）里，而 opening 只在会话开始发一次、
            # 且常被复用会话跳过——于是模型根本不知道要洋红底，给了白底，
            # 后面的抠图自然无从谈起（2026-10-30 实测：--alpha 出了张白底苹果）。
            prompt = it["prompt"]
            if it.get("alpha") and "洋红" not in prompt and "ff00ff" not in prompt.lower():
                prompt = prompt.rstrip() + (
                    "\n（背景用纯洋红 #FF00FF，主体与背景要有明显区分、不要同色系，"
                    "方便后期把背景抠成透明。）")
            # 重跑同一项时，先把这一项自己的旧图 hash 从查重集合里去掉，
            # 否则新图会因为和旧图相似被判「重复」而永远过不了。
            _old = media_mod.find_output(args.out, key, task)
            if _old:
                try:
                    known.discard(dhash(_old))
                except Exception:
                    pass
            if args.rotate and sent_count and sent_count % args.rotate == 0:
                log("  -- 换新对话 --")
                await pg.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
                await pg.wait_for_timeout(4000)
                if await pg.evaluate(JS_CLICK_NEWCHAT):
                    await pg.wait_for_timeout(2500)
                try:
                    await send(pg, OPENING, "开场2")
                except PasteFailed as e:
                    log(f"!! 开场2 提示词没进输入框 {e}，继续（后面每条都会自己再校验）")
                # 等开场回复结束，否则紧接着发任务会发不出去
                t0 = time.time()
                while time.time() - t0 < 150:
                    await pg.wait_for_timeout(3000)
                    try:
                        if await pg.evaluate(JS_NOT_BUSY):
                            ts = await pg.evaluate(
                                "()=>document.querySelectorAll('[data-talvt-turn-state]').length")
                            if ts:
                                break
                    except Exception:
                        pass
                open(os.path.join(DEBUG, f"conv_{args.state_key}.txt"), "w", encoding="utf-8").write(pg.url)

            # 等页面稳定（上一张图渲染完），再拍快照，否则会把旧图当新图
            await pg.wait_for_timeout(2500)
            # 输入框里可能残留上一轮的附件，刷新一次保证干净
            try:
                if await _composer_previews(pg) > 0:
                    await pg.reload(wait_until="domcontentloaded")
                    for _ in range(20):
                        await pg.wait_for_timeout(1000)
                        if await pg.evaluate("()=>!!document.querySelector('div.ProseMirror')"):
                            break
                    await pg.wait_for_timeout(1500)
                    log("    已刷新页面，清掉残留附件")
            except Exception:
                pass
            if it.get("ref"):
                refs = [x for x in str(it["ref"]).split(";") if x.strip() and os.path.exists(x.strip())]
                missing = [x for x in str(it["ref"]).split(";") if x.strip() and not os.path.exists(x.strip())]
                if missing:
                    log(f"!! {task} 参考图路径不存在: {missing}")
                    NO_REF["n"] += 1
                    st[key] = "no-ref-file"; fail += 1
                    json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    continue
                if refs:
                    ok_ref = await upload_refs(pg, refs)
                    if not ok_ref:
                        # 关键：参考图没挂上就绝不出图，避免“悄悄降级成纯文字”导致风格漂移
                        log(f"!! {task} 参考图未就绪，本条直接放弃（不降级）")
                        REF_FAIL["n"] += 1
                        st[key] = "ref-fail"; fail += 1
                        json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                        continue
                    await pg.wait_for_timeout(1200)
            sessions[key] = pg.url        # 这条图落在哪个会话里，供主模型验收隔离
            before = await blob_srcs(pg)
            try:
                sent = await send(pg, prompt, task)
            except PasteFailed as e:
                # 一条提示词没进去就整单放弃，太脆了（2026-11-03：三种粘贴方式都 0/210，
                # 整条直接死，用户看到的就是"跑很久然后失败"）。刷新一次再来一遍。
                log(f"    .. {task} 提示词没进输入框（{e}），刷新再试一次")
                await _reload_page(pg, "提示词没进输入框")
                try:
                    await _paste_mod.wait_editable(pg, 25)
                except Exception:
                    pass
                try:
                    sent = await send(pg, prompt, task)
                except PasteFailed:
                    PASTE_FAIL["n"] += 1
                    st[key] = "pastefail"; fail += 1
                    json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    await _reload_page(pg, "提示词没进输入框（重试后仍失败）")
                    continue
            if not sent:
                st[key] = "sendfail"; fail += 1
                json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                # 发送没生效会把输入框留在一团乱的状态里（R188 就是这样连废两张：
                # S02 点三次不进生成态，S03 紧接着找不到文件输入框）。刷一次再下一条。
                await _reload_page(pg, "发送未进入生成状态")
                continue
            sent_count += 1
            t0 = time.time()
            # 带参考图的任务：确认这条消息真的把图发出去了，否则放弃（防止悄悄变成纯文字）
            if it.get("ref"):
                carried = False
                for _ in range(40):
                    # 发送后附件渲染成 data:image，不是 blob:，不能只数 blob
                    carried = await pg.evaluate("""(k) => {
                        const ts = [...document.querySelectorAll('[data-talvt-turn-state]')];
                        for (const t of ts) {
                            const u = t.querySelector('[data-content-search-unit-key$=":user"]');
                            if (!u || !(u.innerText||'').includes(k)) continue;
                            const n = [...t.querySelectorAll('img')].filter(i => (i.naturalWidth||0) > 200).length;
                            if (n > 0) return true;
                        }
                        return false;
                    }""", task.replace("-", "·"))
                    if carried:
                        break
                    await pg.wait_for_timeout(1000)
                if not carried:
                    # 发送前已经确认附件在输入框里，这里超时多半只是消息渲染慢，不再直接丢弃
                    log(f"    .. {task} 回合里暂未渲染出参考图，继续等待出图（发送前已确认附件存在）")

            src = await wait_img_or_lost(pg, before, task, known, timeout=args.timeout,
                         want=it.get("aspect"), need_alpha=bool(it.get("alpha")),
                         refs=[x for x in str(it.get("ref") or "").split(";") if x.strip()])
            if src == "__LOST__":
                LOST["v"] = True
                st[key] = "browser-lost"; fail += 1
                log(f"  !! {task} 浏览器连接断了（页面/浏览器被关），这条判失败，本批先不跑了")
                json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                break
            # 服务端抖动导致本条没出图 → 原地重发一次（只一次，避免死循环烧额度）
            if src == "__RETRY__":
                log(f"    .. {task} 服务端失败，原地重发一次")
                before = await blob_srcs(pg)
                try:
                    resent = await send(pg, prompt, task)
                except PasteFailed as e:
                    # 这里以前没接：重试分支一抛，整轮直接结束，连 _result.json 都写不出，
                    # 上层只能看到 RENDER_FAILED（2026-10-30 独立验证发现）
                    PASTE_FAIL["n"] += 1
                    log(f"    !! {task} 重发时提示词没进输入框 {e}，本条判失败")
                    resent = False
                if resent:
                    src = await wait_img_or_lost(pg, before, task, known, timeout=args.timeout,
                                 want=it.get("aspect"), need_alpha=bool(it.get("alpha")),
                                 refs=[x for x in str(it.get("ref") or "").split(";") if x.strip()])
                else:
                    src = None
                if src == "__LOST__":
                    LOST["v"] = True
                    st[key] = "browser-lost"; fail += 1
                    log(f"  !! {task} 浏览器连接断了（页面/浏览器被关），这条判失败，本批先不跑了")
                    json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    break
                if src == "__RETRY__":
                    src = None
            if not src:
                st[key] = "fail"; fail += 1
                log(f"  [{idx+1}/{len(items)}] {task} 无图")
                await shot(pg, f"nofig_{key}")
            else:
                fn, mime = await save_img(pg, src, fn)
                good, why = is_good(fn, known, it.get("aspect"), bool(it.get("alpha")),
                                    anim=bool(it.get("anim")))
                if good:
                    known.add(dhash(fn))
                    st[key] = "ok"; ok += 1
                    if why:
                        flags[key] = why
                        log(f"  [{idx+1}/{len(items)}] {task} OK {int(time.time()-t0)}s（标注：{why}）")
                    else:
                        log(f"  [{idx+1}/{len(items)}] {task} OK {int(time.time()-t0)}s")
                else:
                    st[key] = "quality"; fail += 1
                    log(f"  [{idx+1}/{len(items)}] {task} 质检 {why}")
            json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            # 撞到生成上限 → 整批立刻收手，剩下的不再逐条空转
            if QUOTA_HIT["v"]:
                left = len(items) - idx - 1
                if left > 0:
                    log(f"  !! 已撞生成上限，本批剩余 {left} 张不再尝试（省约 {left * 60} 秒空转）")
                    for _it in items[idx + 1:]:
                        st_all[_it["id"]] = "quota-skip"
                    fail += left
                    json.dump(st_all, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                break

        try:
            json.dump(flags, open(os.path.join(args.out, "_flags.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass
        try:
            json.dump(sessions, open(os.path.join(args.out, "_sessions.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass
        # 结构化结局：上层靠它分类（撞上限 / 质检不过 / 正常），不要让它去 grep 日志全文。
        # 日志里出现 "QUOTA" 三个字母不代表撞了上限（提示词、页面文案都可能带）。
        _write_result(args.out, ok=ok, fail=fail, flags=flags, states=st_all,
                      extra={"browser_lost": int(bool(LOST["v"])),
                             "no_ref_file": int(NO_REF["n"]),
                             "ref_failed": int(REF_FAIL["n"]),
                             "empty_turn": int(_EMPTY_TURN["n"] > 0),
                             "client_version": _cv})
        if fail and _STUCK["v"]:
            try:
                session_health.mark_broken(_DATA, getattr(args, "state_key", ""), "page_stuck")
            except Exception:
                pass
        log(f"DONE ok={ok} fail={fail}" + (f" flagged={len(flags)}" if flags else ""))
        await shot(pg, f"done_{args.state_key}")


try:
    asyncio.run(main())
except SystemExit:
    raise
except BaseException as _e:
    # 驱动无论怎么崩，都要留下结构化结局，否则上层只能猜成"渲染失败"
    _a = getattr(main, "args", None)
    if _a is not None:
        extra = {"crashed": 1, "reason": "driver-exception"}
        if isinstance(_e, _pg_guard.PageWedged):
            # 页面卡死：留个重启标记，下次 ensure() 会把这个号的浏览器换掉
            extra["reason"] = "page-wedged"
            try:
                session_health.mark_broken(
                    _DATA, getattr(_a, "state_key", ""), "page_wedged")
            except Exception:
                pass
        # 不只认 LOST 标记：开场 / 上传参考图 / 发送这几步抛的掉线异常
        # 根本走不到 wait_img，标记一直是 False，会被错记成"渲染失败"
        if LOST["v"] or _conn_lost(_e):
            extra["browser_lost"] = 1
        if NO_REF["n"]:
            extra["no_ref_file"] = NO_REF["n"]
        try:
            from .. import __version__ as _cv2

            extra["client_version"] = _cv2
        except Exception:
            pass
        _write_result(_a.out, ok=0, fail=1, extra=extra)
        try:
            from .. import crashlog

            crashlog.record(_DATA, "driver",
                            "".join(__import__("traceback").format_exception(
                                type(_e), _e, _e.__traceback__)),
                            extra=str(extra))
        except Exception:
            pass
    raise
