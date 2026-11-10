"""启动时的小扫除。

只做一件事：把半瓢自己产生的、攒着没用的东西删掉。
每条都必须**幂等**且**删失败了不影响启动**——磁盘整齐是次要的，能用是主要的。

2026-11-04 的起因：用户问「账号为什么有 3.4G」。

| 谁在涨 | 涨到多少 | 为什么会涨 |
|---|---|---|
| `state/update/*.exe` | 517M（9 个版本） | 装完不删 |
| `logs/driver/*.png` | 141M（798 张） | 每跑一单存一张整页截图 |
| `profiles/*/Default/Cache` | 1.5G | Chrome 缓存没有上限 |

第三项在 browser.py 里用启动参数封顶了，前两项在这里扫。
"""

from __future__ import annotations

import time
from pathlib import Path

# 调试截图只在出图失败时用得上，feedback 最多取最近 3 张。
# 留 3 天 / 200 张，既够回溯，也不会再涨到上百兆。
SHOT_KEEP_DAYS = 3
SHOT_KEEP_MAX = 200


def trim_driver_shots(driver_dir: Path,
                      keep_days: int = SHOT_KEEP_DAYS,
                      keep_max: int = SHOT_KEEP_MAX) -> tuple[int, int]:
    """裁掉超期的调试截图，返回（删了几个，省了多少字节）。

    双阈值：先按时间砍（3 天前的），剩下的如果还多于 keep_max，再从最旧的砍。
    """
    removed = freed = 0
    try:
        if not driver_dir.is_dir():
            return 0, 0
        cutoff = time.time() - keep_days * 86400
        shots = []
        for p in driver_dir.iterdir():
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            shots.append((st.st_mtime, st.st_size, p))
        shots.sort()                       # 旧的在前面

        for mtime, size, p in shots:
            if mtime < cutoff:
                try:
                    p.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
            else:
                break                      # 已经排好序，后面都更新

        remaining = [(m, s, p) for m, s, p in shots if p.exists()]
        if len(remaining) > keep_max:
            for mtime, size, p in remaining[:len(remaining) - keep_max]:
                try:
                    p.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
    except OSError:
        pass
    return removed, freed


def trim_browser_cache(profiles_dir: Path, limit_mb: int = 200) -> tuple[int, int]:
    """把单个账号超过 limit_mb 的浏览器缓存删掉，返回（删了几个目录，省了多少字节）。

    为什么光加 `--disk-cache-size` 不够：老用户升级上来时缓存已经是 1.5G 了，
    Chrome 不会因为新参数就主动裁到 50M —— 它会一直用到过期。所以升级后
    第一次启动得手动清一遍，之后新参数才管得住。

    只删纯缓存目录（Cache / Code Cache / GPU*Cache）。Cookies、Local Storage、
    IndexedDB 一律不碰 —— 那是登录态。
    """
    removed = freed = 0
    names = ("Cache", "Code Cache", "GPUCache", "DawnWebGPUCache",
             "DawnGraphiteCache", "GrShaderCache", "GPUPersistentCache")
    try:
        if not profiles_dir.is_dir():
            return 0, 0
        for prof in profiles_dir.iterdir():
            if not prof.is_dir():
                continue
            for sub in ("Default", ""):
                base = prof / sub if sub else prof
                for name in names:
                    d = base / name
                    try:
                        if not d.is_dir():
                            continue
                        size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
                        if size < limit_mb * 1024 * 1024:
                            continue
                        import shutil
                        shutil.rmtree(d, ignore_errors=True)
                    except OSError:
                        continue
                    removed += 1
                    freed += size
    except OSError:
        pass
    return removed, freed


def trim_webview_cache(state_dir: Path, limit_mb: int = 120) -> tuple[int, int]:
    """面板自己的 WebView2 缓存。只涨不降，本机实测 74MB。

    只清纯缓存目录，**绝不碰 Default**（那里有面板的 Local Storage，删了等于重置界面偏好）。
    超过 limit_mb 才动手，免得每次启动都让 WebView 重新下载组件、白白变慢。
    """
    root = Path(state_dir) / "webview" / "EBWebView"
    if not root.is_dir():
        return (0, 0)
    try:
        total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    except OSError:
        return (0, 0)
    if total < limit_mb * 1048576:
        return (0, 0)
    import shutil
    freed = 0
    for name in ("component_crx_cache", "GrShaderCache", "ShaderCache",
                 "GPUCache", "DawnCache", "Code Cache", "Subresource Filter"):
        sub = root / name
        if not sub.is_dir():
            continue
        try:
            freed += sum(f.stat().st_size for f in sub.rglob("*") if f.is_file())
        except OSError:
            pass
        shutil.rmtree(sub, ignore_errors=True)
    return (freed, 0)


def startup_pass(data_dir: Path, log=None) -> dict:
    """启动时跑一遍。返回一份小结，给日志用。"""
    out = {"shots": 0, "updates": 0, "cache": 0, "webview": 0, "freed": 0}
    try:
        n, b = trim_driver_shots(Path(data_dir) / "logs" / "driver")
        out["shots"], out["freed"] = n, out["freed"] + b
    except Exception:
        pass
    try:
        from . import update
        n, b = update.sweep(Path(data_dir) / "state" / "update")
        out["updates"], out["freed"] = n, out["freed"] + b
    except Exception:
        pass
    try:
        n, b = trim_browser_cache(Path(data_dir) / "profiles")
        out["cache"], out["freed"] = n, out["freed"] + b
    except Exception:
        pass
    try:
        n, b = trim_webview_cache(Path(data_dir) / "state")
        out["webview"], out["freed"] = n, out["freed"] + b
    except Exception:
        pass
    if log and any(out[k] for k in ("shots", "updates", "cache", "webview")):
        log("启动清理：截图 %d、旧安装包 %d、浏览器缓存 %d 处、面板缓存 %d 处，释放 %.1fMB"
            % (out["shots"], out["updates"], out["cache"], out["webview"],
               out["freed"] / 1048576))
    return out
