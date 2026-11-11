"""在线更新：查新版本、下载安装包、校验、静默安装。

更新源放在作者自己的服务器上（和反馈同一台），不依赖 GitHub：
    https://hezhuzi.com/bampiao/dl/latest.json
    {
      "version": "0.2.1",
      "notes": "这一版改了什么（给用户看的，一两句）",
      "win": {"url": "https://hezhuzi.com/bampiao/dl/bampiao-0.2.1-setup.exe",
              "sha256": "…", "size": 62855678},
      "mac": {"url": "https://hezhuzi.com/bampiao/dl/bampiao-0.2.1-macos.dmg", "sha256": "…"}
    }

安全：
- 只认 https://hezhuzi.com/ 下的安装包地址——清单被人改了也不会去别处下东西；
- 下载完必须对上 sha256 才会运行；
- 下载到 ~/.bampiao/state/update/，不放桌面。

发版用 tools/publish_update.py 生成清单并上传。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from . import __version__

MANIFEST_URL = "https://hezhuzi.com/bampiao/dl/latest.json"
ALLOWED_PREFIX = "https://hezhuzi.com/"
UA = "bampiao/%s" % __version__


def _ver(v: str) -> tuple:
    out = []
    for part in str(v or "").strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def platform_key() -> str:
    """清单里用哪个键取安装包。

    **必须和 tools/publish_update.py 写进去的键一致**。以前这里返回 "mac"，
    而清单里写的是 "mac_arm64" —— 于是 Mac 用户的 `pkg` 永远是个空字典：
    查得到"有新版本"，点下载却拿到空 URL（点「复制下载链接」复制出来是一串空格）。
    Mac 的自动更新从上线起就没成功过一次，只是没人报（2026-11-06 排查发现）。

    Intel Mac 对应的是 **源码包**（mac_intel_src），不是 dmg：那台机器上没法
    交叉编译 arm64，只能让用户自己 build（见发版记录 0.2.53）。
    """
    if sys.platform != "darwin":
        return "win"
    try:
        import platform as _p

        if _p.machine().lower() in ("x86_64", "i386", "i686"):
            return "mac_intel_src"
    except Exception:
        pass
    return "mac_arm64"


def check(url: str = MANIFEST_URL, timeout: float = 8.0) -> dict:
    """查一次。失败不抛异常，返回 ok=False + 人话原因。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 404 以前被当成"已是最新"，于是更新服务根本没上线时，面板也理直气壮说
        # "已经是最新版"——用户永远等不到新版本，还以为自己是最新的（2026-10-30 实测 404）。
        if e.code == 404:
            return {"ok": False, "current": __version__,
                    "why": "更新服务还没上线（服务器上没有版本清单）"}
        return {"ok": False, "current": __version__, "why": "更新服务器出错了（%d），过会儿再查" % e.code}
    except Exception as e:
        return {"ok": False, "current": __version__,
                "why": "连不上更新服务器（%s），检查一下网络" % type(e).__name__}
    if not isinstance(d, dict):
        return {"ok": False, "current": __version__, "why": "版本清单格式不对（不是对象）"}
    latest = str(d.get("version") or "")
    pkg = d.get(platform_key()) or {}
    # **按平台条目自己的版本算**。清单是"一个版本一次发版"，但 Mac 和 Windows
    # 常常不是同一时刻打的包：这次 0.2.54 只动了 Windows，如果还看顶层 version，
    # Mac 用户会一直被告知"有新版本 0.2.54"，点下载却拿到 0.2.53 的 dmg，
    # 装完还是 0.2.53，于是永远在提示更新——比不提示还糟。
    mine = str(pkg.get("version") or latest)
    return {"ok": True, "current": __version__, "latest": mine,
            "newer": bool(mine) and _ver(mine) > _ver(__version__),
            "notes": str(d.get("notes") or ""), "url": str(pkg.get("url") or ""),
            "sha256": str(pkg.get("sha256") or "").lower(), "size": int(pkg.get("size") or 0),
            "checked_at": time.time()}


def sweep(dest_dir: Path, keep: Path | None = None) -> tuple[int, int]:
    """清掉攒下来的旧安装包，返回（删了几个，省了多少字节）。

    2026-11-04 用户机器实测：`~/.bampiao/state/update/` 里躺着 9 个版本
    （0.2.7 到 0.2.18），每个 58MB，总共 517MB —— 装完从来没删过。
    每更新一版就多一份，这是纯增量垃圾。

    安装器已经启动、exe 被系统打开时删不掉，那是正常情况：跳过，下次再来。

    **`.part` 一定要留着**：那是没下完的半截文件，续传全靠它。
    2026-11-06 的反馈就是这么翻车的——慢网用户每次点"再试一次"，
    这里先把 30MB 的 .part 删掉，然后再从头下 60MB，永远下不完。
    """
    removed = freed = 0
    try:
        if not dest_dir.is_dir():
            return 0, 0
        for p in dest_dir.iterdir():
            if not p.is_file() or (keep is not None and p == keep):
                continue
            if p.suffix.lower() not in (".exe", ".dmg", ".pkg", ".msi"):
                continue
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue          # 正被安装器占着
            removed += 1
            freed += size
    except OSError:
        pass
    return removed, freed


def _sha256_file(path: Path) -> str:
    """算整个文件的指纹。

    以前是边下边 h.update，省一次读盘。但续传接不上——半截文件的哈希状态
    没法保存。60MB 读一遍也就几十毫秒，换成"下完再算"值得。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(e: BaseException) -> str:
    """把 urllib 那套英文异常翻成人话。

    2026-11-06 用户反馈的原文是「更新没成功：The read operation timed out」——
    一句英文 socket 错误，用户看完只会觉得这软件坏了。
    """
    text = str(e).lower()
    if isinstance(e, TimeoutError) or "timed out" in text:
        return "网络卡住了（下载超时）"
    if isinstance(e, urllib.error.URLError):
        return "连不上更新服务器（%s）" % (getattr(e, "reason", None) or e)
    if isinstance(e, ConnectionError) or "connection" in text:
        return "网络断了"
    return "%s: %s" % (type(e).__name__, e)


def download(info: dict, dest_dir: Path, progress=None, timeout: float = 45.0,
             retries: int = 3) -> Path:
    """下载并校验。progress(已下字节, 总字节) 给面板显示进度。

    三个韧性设计，都是被真实反馈打出来的：

    1. **断点续传**：先看 `.part` 有多大，带 `Range` 头接着下。
       慢网用户下到 40MB 断了，再点一次是从 40MB 继续，不是回零。
    2. **自动重试**：网络抖一下就重来，重试之间退避 1.5s / 3s。
       用户不需要理解"再试一次"到底会不会白费——它会接着下。
    3. **说人话**：超时/断网翻译成中文，并且告诉他"已经下了多少、点再试一次会接着下"。

    2026-11-06 的完整因果：60MB 安装包 + 单次 30s 超时 + 失败删 `.part`，
    于是那位用户点了三次、卡了三天，一直停在 0.2.5 升不上来。
    """
    # 下新的之前先扫一遍：反正旧版已经装过了，留着只占地方（`.part` 会被保留）
    sweep(dest_dir)
    url, want = info.get("url") or "", (info.get("sha256") or "").lower()
    if not url.startswith(ALLOWED_PREFIX):
        raise RuntimeError("安装包地址不对，没下载（只认 hezhuzi.com）")
    if len(want) != 64:
        raise RuntimeError("更新清单里没有校验值，没下载")
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 只取最后一段，并且只保留安全字符：远端 url 里塞 ".." 段时不能写到目录外
    name = PurePosixPath(url.split("?")[0]).name
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-") or "bampiao-setup"
    if not PurePosixPath(name).suffix:
        name += ".exe" if platform_key() == "win" else ".dmg"
    dst = dest_dir / name
    tmp = dst.with_name(dst.name + ".part")
    total = int(info.get("size") or 0)

    last = ""
    for attempt in range(1, max(1, retries) + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        if total and have > total:           # 上次下多了/文件坏了，重来
            tmp.unlink(missing_ok=True)
            have = 0
        if total and have >= total:
            break                            # 上次其实已经下完了

        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # 只校验初始 URL 不够：urllib 默认跟随 302，最终可能落到任意 host。
                # 这里再确认一次"真正在下载的那个地址"。
                final = r.geturl() or url
                if not final.startswith(ALLOWED_PREFIX):
                    raise RuntimeError("安装包被重定向到别处（%s），没下载" % final[:80])
                if have and r.status != 206:
                    have = 0                 # 服务器不认 Range，只能从头来
                clen = int(r.headers.get("Content-Length") or 0)
                if clen:
                    total = have + clen
                got = have
                if progress:
                    progress(got, total)
                with open(tmp, "ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total)
            # http.client 的 read(amt) 遇到**提前 EOF 是静默返回 b""**（它自己的注释里
            # 写着"理想情况该抛 IncompleteRead，但为了兼容性没抛"）。所以断流必须自己
            # 按字节数认出来：不认就当成下完了，接着 sha 校验失败、.part 被删，
            # 用户又得从零开始——这正是 2026-11-06 那位卡在 57% 的用户的死循环。
            if total and got < total:
                raise ConnectionError("只收到 %d/%d 字节" % (got, total))
            break                            # 下完了
        except urllib.error.HTTPError as e:
            # 4xx 重试没用（地址错了/被删了），直接说清楚，别让用户干等
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise RuntimeError("下载地址打不开（HTTP %d），可能服务器上还没放这个包" % e.code) from None
            last = "服务器返回 %d" % e.code
        except RuntimeError:
            raise                            # 重定向/参数这类错误不是网络抖动，不重试
        except Exception as e:
            last = _human(e)

        if attempt < retries:
            time.sleep(1.5 * attempt)

    if not tmp.exists():
        raise RuntimeError("没下下来（%s），过会儿再试" % (last or "原因不明"))
    if total and tmp.stat().st_size < total:
        pct = int(tmp.stat().st_size * 100 / total)
        # 别在这儿解释"再点一次会续传"——面板在按钮旁边已经写了，重复反而啰嗦
        raise RuntimeError("下到 %d%% 断了（%s）" % (pct, last or "网络不稳"))
    if _sha256_file(tmp) != want:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载的文件校验不对（可能没下完整），没安装，再试一次")
    tmp.replace(dst)
    return dst


def busy_reason(cfg, store=None) -> str:
    """现在升级会不会打断正在跑的东西。返回空串表示"可以升"。

    为什么要有这道闸：2026-10-30 我在用户机器上装 0.2.6 时，安装器先 `bampiao quit`，
    把当时正在跑的 R101 直接掐了（标成 TIMEOUT_UNCERTAIN，图没了）。
    用户的要求是「升级对正在进行的无感」，所以：有单在跑就不升，等它跑完。
    """
    try:
        from . import locks
        from .driver import browser

        for acc in getattr(cfg, "accounts", []):
            try:
                if not locks.try_acquire(browser.gen_lockfile(cfg, acc.id)):
                    return "「%s」正在出图" % (acc.name or acc.id)
            except Exception:
                continue
    except Exception:
        pass
    # 锁是"正在出图"的快照，两次采样之间可能刚好空一拍；running 状态更粗但更稳。
    # 给 60 秒宽限：刚领到手的单可能还没写 started_at，别误判成"没活的"。
    try:
        import time as _t

        if store is not None:
            for j in store.running():
                if _t.time() - float(getattr(j, "started_at", 0) or 0) > 60:
                    return "R%d 还在跑" % j.id
    except Exception:
        pass
    # **排队的单不算阻塞**：队列是持久化的，装完重启会自动继续跑，
    # 那不算"打断"。只有"正在出图"才是真的会把图弄丢。
    return ""


def launch_installer(path: Path) -> int:
    """Windows：静默安装。返回安装器 pid，供调用方确认"它真的起来了"。

    安装程序会先关掉正在运行的半瓢，装完自己再打开（见 bampiao.iss）。

    2026-11-11：此前用 DETACHED_PROCESS 拉起安装器，它脱离父进程、
    不继承任何隐藏窗口设置，于是 setup.exe 会正常弹窗/闪任务栏图标——用户看得见。
    现在改用 CREATE_NO_WINDOW + STARTUPINFO(SW_HIDE)，并升到 /VERYSILENT
    （连进度条/背景都不画）+ /SP-（开头「正在准备安装」小窗也省掉），
    安装器进程照常存在，但不再有任何窗口。
    """
    if sys.platform != "win32":
        raise RuntimeError("这个平台要手动打开下载好的安装包")
    si = None
    flags = 0
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE          # 0
        flags |= 0x08000000                          # CREATE_NO_WINDOW：不给它建控制台窗口
    p = subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/SP-", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS"],
                         creationflags=flags, startupinfo=si, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return int(p.pid)
