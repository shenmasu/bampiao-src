"""浏览器可执行文件探测。

不写死本机路径：按系统标准安装位置找，允许配置覆盖（后续步骤加载）。
"""

from __future__ import annotations

import os
from pathlib import Path

_CANDIDATES = {
    "chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        # macOS：.app 里的可执行文件；~/Applications 是免管理员安装的常见位置
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "edge": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
}


def find(browser: str, override: str | None = None) -> Path | None:
    if override:
        p = Path(os.path.expandvars(os.path.expanduser(override)))
        return p if p.is_file() else None
    for raw in _CANDIDATES.get(browser, []):
        p = Path(os.path.expandvars(raw))
        if p.is_file():
            return p
    return None


def auto() -> str:
    """挑一个本机真装了的浏览器。给 `--browser auto` / 首次初始化用。

    顺序是 chrome → edge：Chrome 的 CDP 更稳；没有就退 Edge（Win10/11 自带）。
    都没有就返回空串，由调用方给出"装上 Chrome 或 Edge"的提示。
    """
    for name in ("chrome", "edge"):
        if find(name):
            return name
    return ""


def missing_reason(browser: str) -> str:
    if browser not in _CANDIDATES:
        return "不支持的浏览器：%s" % browser
    paths = _CANDIDATES[browser]
    return "%s 没找到。装一个，或在配置里用 exe 覆盖。看过的位置：\n    %s" % (
        browser, "\n    ".join(os.path.expandvars(p) for p in paths))
