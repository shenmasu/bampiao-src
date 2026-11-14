"""发版对账：所有产物必须和源码同一个版本号。

用户的原话是「我希望能对上」。人工对账必然出错（0.2.3 的 exe 里
FileVersion 还写着 0.1.0.0 就是这么来的），所以钉成脚本：

    python packaging/check_release.py            # 在仓库根目录跑

检查项：
  1. 源码 __version__ / pyproject / 安装器 / exe 资源，四处必须一致
  2. 发版目录里每个产物（Windows 安装包、mac dmg）文件名里的版本 = 源码版本
  3. Windows exe 内部的 FileVersion = 源码版本（这就是"右键属性显示旧版本"的来源）
  4. latest.json（如果给了 URL）的 version / size / sha256 是否对得上本地文件

退出码：0 = 全对上；1 = 有对不上的（逐条打印差在哪）。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import sys
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def source_version() -> str:
    import image2_bampiao

    return image2_bampiao.__version__


def exe_file_version(path: Path) -> str | None:
    """读 Windows PE 的 FileVersion（右键 属性 → 详细信息 里那个）。"""
    if sys.platform != "win32":
        return None
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf):
        return None
    r = ctypes.c_void_p()
    ln = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(r), ctypes.byref(ln)):
        return None
    ffi = ctypes.cast(r, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
    ms, ls = ffi[2], ffi[3]
    return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-dir", default=str(Path(ROOT).parent / "image2-bampiao-发版"))
    ap.add_argument("--latest-json", default="https://hezhuzi.com/bampiao/dl/latest.json",
                    help="线上清单地址；传空字符串跳过")
    args = ap.parse_args()

    ver = source_version()
    rel = Path(args.release_dir)
    bad: list[str] = []
    print("源码版本：%s" % ver)
    print("发版目录：%s\n" % rel)

    # ---- 1. 四处版本号 ----
    pyproj = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    iss = re.search(r'#define MyAppVersion "([^"]+)"', (ROOT / "packaging" / "bampiao.iss").read_text(encoding="utf-8"))
    vi = re.search(r"filevers=\((\d+), (\d+), (\d+), (\d+)\)",
                   (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8"))
    for label, got in (("pyproject.toml", pyproj and pyproj.group(1)),
                       ("bampiao.iss", iss and iss.group(1)),
                       ("version_info.txt", vi and ".".join(vi.groups()[:3]))):
        mark = "✅" if got == ver else "❌"
        print("%s %-18s %s" % (mark, label, got))
        if got != ver:
            bad.append("%s 是 %s，源码是 %s" % (label, got, ver))

    # ---- 2. 产物文件名 ----
    pats = ["半瓢-*-安装程序.exe", "mac/*.dmg", "*.dmg", "*/*.dmg"]
    seen: set[Path] = set()
    for pat in pats:
        for p in sorted(rel.glob(pat)):
            if p in seen:
                continue
            seen.add(p)
            m = re.search(r"半瓢-(\d+\.\d+\.\d+)", p.name)
            got = m.group(1) if m else "?"
            mark = "✅" if got == ver else "⏭"
            note = "" if got == ver else "（旧版本，允许留着）"
            print("%s %-46s %s %s" % (mark, p.name, got, note))

    # ---- 3. exe 内部版本 ----
    # 查的是"被装进用户机器里的那个 exe"，不是安装包外壳：
    # Inno 的 setup.exe 自己是 0.0.0.0（它的版本在 uninstall 注册表里），
    # 真正会显示在"右键 → 属性"里的是 {app}ampiao.exe。
    # 只看 dist / dist-024 这种本次构建目录；dist-prev-* 是历史备份，别拿来报错
    payloads = [q for q in sorted(ROOT.glob("dist*/bampiao/bampiao.exe"))
                if q.parent.parent.name == "dist" or re.fullmatch(r"dist-\d+", q.parent.parent.name)]
    if not payloads:
        print("⚠️  没找到 dist*/bampiao/bampiao.exe，跳过 exe 版本检查（先跑一次 PyInstaller）")
    # 只看最新那次构建：旧 dist 里的 exe 版本天然是旧的，拿它报错等于自己吵自己
    payloads.sort(key=lambda q: q.stat().st_mtime, reverse=True)
    for p in payloads[:1]:
        fv = exe_file_version(p)
        ok = fv is not None and fv.rsplit(".", 1)[0] == ver
        print("%s 最新 payload %-16s FileVersion %s" % ("✅" if ok else "❌", p.parent.parent.name + "/", fv))
        if not ok and fv is not None:
            bad.append("%s 的 FileVersion=%s，应为 %s.0（右键属性会显示这个）" % (p, fv, ver))
    for p in payloads[1:]:
        print("⏭ 历史 payload %-16s 跳过" % (p.parent.parent.name + "/"))

    # ---- 4. 线上清单 ----
    if args.latest_json:
        try:
            with urllib.request.urlopen(args.latest_json, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("⚠️  读不到线上清单：%s" % e)
            d = None
        if d:
            print("\n线上清单 version：%s  %s" % (d.get("version"), "✅" if d.get("version") == ver else "❌"))
            if d.get("version") != ver:
                bad.append("latest.json 是 %s，源码是 %s（没发出去就会这样）" % (d.get("version"), ver))
            for key in ("win", "mac"):
                item = d.get(key)
                if not item:
                    print("⚠️  清单里没有 %s 条目：%s 用户查更新会看到空链接"
                          % (key, "Mac" if key == "mac" else "Windows"))
                    continue
                # 线上文件名是英文（bampiao-0.2.4-setup.exe），本地是中文名，
                # 所以按版本号去本地找，别按 url 里的文件名找。
                v = d.get("version") or ""
                cands = []
                if key == "win":
                    cands = sorted(rel.glob("半瓢-%s-安装程序.exe" % v))
                else:
                    cands = sorted(rel.glob("**/半瓢-%s*.dmg" % v))
                local = cands[0] if cands else None
                if local is None:
                    print("   %s：本地没有 %s 版产物，跳过校验" % (key, v))
                    continue
                same = sha256(local) == item.get("sha256")
                size_ok = local.stat().st_size == item.get("size")
                print("%s 清单 %s 条目：sha256 %s ｜ size %s"
                      % ("✅" if same and size_ok else "❌", key,
                         "对" if same else "对不上", "对" if size_ok else "对不上"))
                if not (same and size_ok):
                    bad.append("清单 %s 条目与本地 %s 不一致（重传安装包后忘了更新清单？）" % (key, local.name))

    print()
    if bad:
        print("❌ 有 %d 处对不上：" % len(bad))
        for b in bad:
            print("   - %s" % b)
        return 1
    print("✅ 全部对上：源码、安装器、exe 资源、产物文件名、线上清单都是 %s" % ver)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
