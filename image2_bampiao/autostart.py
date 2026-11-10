"""开机自启：写 HKCU 的 Run 项，不用计划任务。

为什么不用计划任务（这是老系统踩过的）：
- 计划任务的 Action 如果用 .cmd，每次触发都会闪一下黑框；
- 计划任务默认在 Session 0 / 隐藏桌面跑，GUI 起不来；
- 卸载时用户很难自己找到它。

HKCU\\...\\Run 的语义正好：登录后、当前用户、普通权限、随用户卸载。

Windows 写 HKCU 的 Run 项；macOS 写 ~/Library/LaunchAgents 里的 LaunchAgent。
两边都只影响「当前用户登录时自动启动」，卸载程序时删掉对应项。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Bampiao"

# macOS：LaunchAgent 的 label 和路径
MAC_LABEL = "com.hezhuzi.bampiao"
MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / (MAC_LABEL + ".plist")


class AutostartError(RuntimeError):
    pass


def _require_windows() -> None:
    if os.name != "nt":
        raise NotImplementedError("开机自启目前只支持 Windows（v1 范围）")


def desktop_exe() -> Path | None:
    """找一个能直接启动的 GUI 入口（不弹控制台）。

    三种安装形态：
    - PyInstaller 打包：sys.executable 本身就是 bampiao-desktop.exe
    - pipx / venv：sys.executable 是 python.exe，同目录下有 bampiao-desktop.exe
    - 源码直跑：找不到 exe，返回 None（调用方要提示用户）
    """
    exe = Path(sys.executable)
    if exe.name.lower().startswith(("bampiao-desktop", "bampiao-gui")):
        return exe
    for name in ("bampiao-desktop.exe", "bampiao-desktop", "bampiao-gui"):
        cand = exe.with_name(name)
        if cand.exists():
            return cand
    return None


def _app_bundle(exe: Path) -> Path | None:
    """从 .../Foo.app/Contents/MacOS/foo 找回 Foo.app。"""
    for parent in Path(exe).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def _mac_plist(args: list[str]) -> str:
    body = chr(10).join("    <string>%s</string>" % a for a in args)
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>
%s
  </array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
""" % (MAC_LABEL, body)


def _mac_enable(exe: Path | None, extra: str) -> str:
    target = Path(exe) if exe else desktop_exe()
    if not target or not Path(target).exists():
        raise AutostartError("找不到半瓢的可执行文件，无法设置开机自启")
    app = _app_bundle(target)
    # 在 .app 里就用 open -a：走 LaunchServices，Dock 和菜单栏图标都正常；
    # 直接执行 Contents/MacOS 下的二进制会缺 bundle 身份，表现会怪。
    args = ["/usr/bin/open", "-a", str(app)] if app else [str(target)]
    try:
        MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
        MAC_PLIST.write_text(_mac_plist(args), encoding="utf-8")
    except OSError as e:
        raise AutostartError("写 %s 失败：%s" % (MAC_PLIST, e))

    # 已经加载过就先卸掉，否则 bootstrap 会报 "already bootstrapped"
    _launchctl(["bootout", "gui/%d/%s" % (os.getuid(), MAC_LABEL)])
    if not _launchctl(["bootstrap", "gui/%d" % os.getuid(), str(MAC_PLIST)]):
        # 写文件成功但加载失败，用户会以为"已经开了"却永远等不到它起来——
        # 这种情况必须报错，不能返回成功。
        raise AutostartError(
            "plist 写好了，但 launchctl 加载失败。可以手动执行："
            "launchctl bootstrap gui/$(id -u) %s" % MAC_PLIST)
    if extra:
        raise AutostartError("macOS 的自启不支持 --extra，请改用桌面壳自身的参数")
    return " ".join(args)


def _launchctl(args: list[str]) -> bool:
    try:
        r = subprocess.run(["launchctl"] + args, capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _mac_disable() -> bool:
    existed = MAC_PLIST.exists()
    _launchctl(["bootout", "gui/%d/%s" % (os.getuid(), MAC_LABEL)])
    try:
        MAC_PLIST.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return existed


def _mac_status() -> dict:
    if not MAC_PLIST.exists():
        return {"enabled": False, "command": "", "exists": False}
    try:
        text = MAC_PLIST.read_text(encoding="utf-8")
    except OSError:
        return {"enabled": False, "command": "", "exists": False}
    # 取 ProgramArguments 里的路径：open -a /path/App.app 或直接可执行文件
    target = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("<string>") and line.endswith("</string>"):
            v = line[len("<string>"):-len("</string>")]
            if v.startswith("/"):
                target = v
    loaded = _launchctl(["print", "gui/%d/%s" % (os.getuid(), MAC_LABEL)])
    return {"enabled": True, "command": target or MAC_PLIST.name,
            "target": target, "exists": Path(target).exists() if target else False,
            "loaded": loaded}


def _command(exe: Path, extra: str = "") -> str:
    # 路径带空格必须加引号，否则注册表里会被当成"第一个空格前的可执行文件"
    cmd = '"%s"' % exe
    if extra:
        cmd += " " + extra
    return cmd


def enable(exe: Path | None = None, *, extra: str = "", run_key: str = RUN_KEY,
           value: str = VALUE_NAME) -> str:
    """打开自启。返回写进去的命令（Windows 是注册表值，mac 是 LaunchAgent）。"""
    if sys.platform == "darwin":
        return _mac_enable(exe, extra)
    _require_windows()
    import winreg

    target = Path(exe) if exe else desktop_exe()
    if not target or not Path(target).exists():
        raise AutostartError(
            "找不到 bampiao-desktop 可执行文件。用 pipx 装的话它应该在同一个 Scripts 目录里；"
            "源码直跑请先 pip install -e .，或手动指定 --exe")
    cmd = _command(Path(target), extra)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, value, 0, winreg.REG_SZ, cmd)
    return cmd


def disable(*, run_key: str = RUN_KEY, value: str = VALUE_NAME) -> bool:
    if sys.platform == "darwin":
        return _mac_disable()
    """关掉自启。返回"本来有没有"。"""
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def status(*, run_key: str = RUN_KEY, value: str = VALUE_NAME) -> dict:
    """现在的状态：开了没、指向哪个文件、那个文件还在不在。"""
    if sys.platform == "darwin":
        return _mac_status()
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as k:
            raw, _typ = winreg.QueryValueEx(k, value)
    except FileNotFoundError:
        return {"enabled": False, "command": "", "exists": False}
    except OSError:
        return {"enabled": False, "command": "", "exists": False}

    # 命令行可能带参数：带引号的取引号内，不带的取第一个空格前
    if str(raw).startswith('"'):
        target = str(raw)[1:].split('"', 1)[0]
    else:
        target = str(raw).split(" ", 1)[0]
    return {"enabled": True, "command": str(raw),
            "target": target, "exists": Path(target).exists()}
