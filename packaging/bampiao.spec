# -*- mode: python ; coding: utf-8 -*-
"""半瓢 Windows 打包配置：一个目录、两个 exe，共用同一份 _internal/。

  bampiao.exe          命令行（console 子系统，输出看得见）
  bampiao-desktop.exe  桌面壳（windowed 子系统，双击不弹黑框）

为什么 onedir 不用 onefile：onefile 每次启动要把 200MB 解压到临时目录，
首屏要等好几秒，杀软也更容易误报。onedir + 安装包是更稳的组合。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent          # SPECPATH = packaging/
PKG = ROOT / "image2_bampiao"

# 面板的静态资源必须跟着走，否则冻结后是白窗（claude-hub 踩过）。
datas = [(str(PKG / "ui" / "static"), "image2_bampiao/ui/static"),
         # 给 Agent 的那两段说明必须随包走：用户装的是 exe，没有仓库可翻。
         (str(ROOT / "skills"), "image2_bampiao/skills")]

# playwright 在 PyInstaller 里没有官方 hook，必须手动收：它运行时靠
# 包目录里的 driver/node.exe 起 CDP 会话，漏了就"能启动、出图必崩"。
pw_datas, pw_bins, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries = pw_bins

hiddenimports = list(pw_hidden) + [
    # 这两个是新模块，在函数体内按需导入（`from . import maintenance`），
    # PyInstaller 的静态分析不一定跟得到，漏了就是启动即崩。显式写死。
    "image2_bampiao.paths",
    "image2_bampiao.maintenance",
    # pywebview 的后端是按字符串动态导入的
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "pystray._win32",
    "clr",
    "clr_loader",
    "pythonnet",
    "bottle",
    "proxy_tools",
    "typing_extensions",
]

# 只排掉确定用不到的大件；排多了反而会在运行时 ImportError。
# PIL.AvifImagePlugin 连带 _avif.pyd 有 7.5MB —— 我们只开 PNG/JPEG，
# 参考图是直接交给浏览器上传的，不经过 PIL 解码（2026-10-30 量过）。
excludes = ["tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest", "_pytest",
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
        # 2026-11-05：试过 runtime hook 强改 stdout 编码，结果 exe 直接崩：
        # ValueError: I/O operation on closed file（PyInstaller 退出时会关 stdio，
        # 和新 wrapper 抢 buffer）。所以这里保持空——中文输出去走 --out 写文件。
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
    )


a_cli = analysis("_entry_cli.py")
a_gui = analysis("_entry_gui.py")

pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="bampiao",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "packaging" / "bampiao.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="bampiao-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：双击不弹黑框
    icon=str(ROOT / "packaging" / "bampiao.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    strip=False,
    upx=False,
    name="bampiao",
)
