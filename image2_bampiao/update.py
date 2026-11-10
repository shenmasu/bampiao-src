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
    return "mac" if sys.platform == "darwin" else "win"


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
    return {"ok": True, "current": __version__, "latest": latest,
            "newer": bool(latest) and _ver(latest) > _ver(__version__),
            "notes": str(d.get("notes") or ""), "url": str(pkg.get("url") or ""),
            "sha256": str(pkg.get("sha256") or "").lower(), "size": int(pkg.get("size") or 0),
            "checked_at": time.time()}


def sweep(dest_dir: Path, keep: Path | None = None) -> tuple[int, int]:
    """清掉攒下来的旧安装包，返回（删了几个，省了多少字节）。

    2026-11-04 用户机器实测：`~/.bampiao/state/update/` 里躺着 9 个版本
    （0.2.7 到 0.2.18），每个 58MB，总共 517MB —— 装完从来没删过。
    每更新一版就多一份，这是纯增量垃圾。

    安装器已经启动、exe 被系统打开时删不掉，那是正常情况：跳过，下次再来。
    """
    removed = freed = 0
    try:
        if not dest_dir.is_dir():
            return 0, 0
        for p in dest_dir.iterdir():
            if not p.is_file() or (keep is not None and p == keep):
                continue
            if p.suffix.lower() not in (".exe", ".part", ".dmg", ".pkg", ".msi"):
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


def download(info: dict, dest_dir: Path, progress=None, timeout: float = 30.0) -> Path:
    """下载并校验。progress(已下字节, 总字节) 给面板显示进度。"""
    # 下新的之前先扫一遍：反正旧版已经装过了，留着只占地方
    sweep(dest_dir)
    url, want = info.get("url") or "", (info.get("sha256") or "").lower()
    if not url.startswith(ALLOWED_PREFIX):
        raise RuntimeError("安装包地址不对，没下载（只认 hezhuzi.com）")
    if len(want) != 64:
        raise RuntimeError("更新清单里没有校验值，没下载")
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 只取最后一段，并且只保留安全字符：远端 url 里塞 ".." 段时不能写到目录外
    name = PurePosixPath(url.split("?")[0]).name
    name = "".join(ch for ch in name if ch.isalnum() or ch in "._-") or "bampiao-setup.exe"
    if not name.lower().endswith(".exe"):
        name += ".exe"
    dst = dest_dir / name
    tmp = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    total = int(info.get("size") or 0)
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        # 只校验初始 URL 不够：urllib 默认跟随 302，最终可能落到任意 host。
        # 这里再确认一次"真正在下载的那个地址"。
        final = r.geturl() or url
        if not final.startswith(ALLOWED_PREFIX):
            raise RuntimeError("安装包被重定向到别处（%s），没下载" % final[:80])
        total = total or int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    if h.hexdigest() != want:
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
    """
    if sys.platform != "win32":
        raise RuntimeError("这个平台要手动打开下载好的安装包")
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS"],
                         creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return int(p.pid)
