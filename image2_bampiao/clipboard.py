"""把一张图放进系统剪贴板。

为什么要走后端：面板跑在 pywebview / WebView2 里，`navigator.clipboard.write`
需要浏览器给 ClipboardItem 授权，而 pywebview 没有接 PermissionRequested，
WebView2 默认拒——前端那条路在真窗口里会失败。前端只在后端也失败时才退回下载。

命令行用系统自带的东西（Windows 用 PowerShell + System.Windows.Forms，
macOS 用 osascript），不额外依赖 win32clipboard / pyperclip，
打包体积不动。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(args: list[str], timeout: float = 15.0) -> tuple[bool, str]:
    flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=flags,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
        return False, msg[-300:] or "退出码 %d" % r.returncode
    return True, ""


def copy_image(path: Path) -> tuple[bool, str]:
    """把 path 指向的图放进剪贴板。返回 (成功?, 说明)。"""
    path = Path(path).resolve()
    if not path.is_file():
        return False, "文件不在了"

    if sys.platform == "win32":
        # 单引号里的路径要把 ' 写成 ''（PowerShell 规则）；文件名里带单引号时不会炸
        p = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$i=[System.Drawing.Image]::FromFile('%s');"
            "try{[System.Windows.Forms.Clipboard]::SetImage($i)}finally{$i.Dispose()}" % p
        )
        ok, why = _run(["powershell", "-NoProfile", "-NonInteractive", "-STA",
                        "-ExecutionPolicy", "Bypass", "-Command", script])
        return (True, "已复制") if ok else (False, why)

    if sys.platform == "darwin":
        p = str(path).replace('"', '\\"')
        ok, why = _run(["osascript", "-e",
                        'set the clipboard to (read (POSIX file "%s") as «class PNGf»)' % p])
        return (True, "已复制") if ok else (False, why)

    return False, "这个系统还不支持直接复制，请用「另存为」"
