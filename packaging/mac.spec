# -*- mode: python ; coding: utf-8 -*-
"""半瓢 macOS 打包配置：一个目录、两个可执行文件。

  bampiao-gui  桌面壳（窗口 + 托盘）
  bampiao      命令行

macOS 上没有 .exe 后缀，两个二进制直接并排放在 bundle 的 Contents/MacOS/ 下；
`_cli_cmd()` / `_driver_exe()` 就是按这个名字（bampiao）去找同目录的 CLI 的。

**用 PyInstaller 自己的 BUNDLE 出 .app**（2026-10-30 起）。以前手工拼 .app，把 _internal 整个摊进
Contents/Frameworks——里面混着 png / .md / *.dist-info，macOS 15 的 codesign 不允许 Frameworks 下有
非 Mach-O 文件，整包签不上（封印是坏的，用户看到「已损坏」）。BUNDLE 会把数据文件放进
Contents/Resources 并用符号链接接回，两个可执行文件也都在 Contents/MacOS 下，整包能正常签名。
（旧注释说「BUNDLE 只认一个主可执行文件」不成立：主程序由 CFBundleExecutable 指定，另一个照样在 MacOS/。）
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
PKG = ROOT / "image2_bampiao"

datas = [(str(PKG / "ui" / "static"), "image2_bampiao/ui/static"),
         # 和 bampiao.spec 保持一致：给 Agent 的那几段说明必须随包走，
         # 否则 mac 用户 bampiao skill 会报"安装包里少了 PASTE.md"。
         (str(ROOT / "skills"), "image2_bampiao/skills")]

# macOS 的素材：.icns 由 build_mac.sh 用 iconutil 生成后放进来
icns = ROOT / "packaging" / "bampiao.icns"
if icns.exists():
    datas.append((str(icns), "."))

# playwright 没有官方 hook，必须手动收（driver/node 是出图链路的地基）
pw_datas, pw_bins, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries = pw_bins

hiddenimports = list(pw_hidden) + [
    # 和 Windows 那份保持一致：这两个模块在函数体内按需导入，
    # 静态分析可能跟不到，漏了就是启动即崩。
    "image2_bampiao.paths",
    "image2_bampiao.maintenance",
    # pywebview 的 macOS 后端是动态导入的
    "webview.platforms.cocoa",
    "pystray._darwin",
    "AppKit",
    "Foundation",
    "WebKit",
    "objc",
    "PyObjCTools",
    "Quartz",
]

excludes = ["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest",
            "_pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi",
            "PIL.AvifImagePlugin", "playwright.sync_api"]


def analysis(entry: str) -> Analysis:
    return Analysis(
        [str(ROOT / "packaging" / entry)],
        pathex=[str(ROOT)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
    )


a_cli = analysis("_entry_cli.py")
a_gui = analysis("_entry_gui.py")

pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data)

exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    exclude_binaries=True,
    name="bampiao",
    console=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_gui = EXE(
    pyz_gui, a_gui.scripts, [],
    exclude_binaries=True,
    name="bampiao-gui",
    console=False,          # 双击不弹终端
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_cli, a_cli.binaries, a_cli.datas,
    exe_gui, a_gui.binaries, a_gui.datas,
    strip=False,
    upx=False,
    name="bampiao",
)


import re as _re
_VERSION = _re.search(r'__version__ = "([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8")).group(1)

app = BUNDLE(
    coll,
    name="半瓢.app",
    icon=str(icns) if icns.exists() else None,
    bundle_identifier="com.hezhuzi.bampiao",
    version=_VERSION,
    info_plist={
        # 双击打开的是桌面壳，不是命令行
        "CFBundleExecutable": "bampiao-gui",
        "CFBundleName": "半瓢",
        "CFBundleDisplayName": "半瓢",
        "CFBundleShortVersionString": _VERSION,
        "CFBundleVersion": _VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.graphics-design",
        "NSHumanReadableCopyright": "MIT License",
        # 隐藏浏览器窗口用的是 AppleScript，没有这句话系统会直接把进程杀掉
        "NSAppleEventsUsageDescription": "半瓢需要通过「系统事件」打开或隐藏它自己的浏览器窗口来完成出图。"
                                         "它只会操作自己启动的那个 Chrome 实例。",
    },
)
