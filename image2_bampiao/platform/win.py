"""Windows 平台层：窗口、进程、端口。

全部来自生产代码的实战结论，改动前请先读这三条：
1. **ctypes 调用必须写 argtypes**。不写时 `HWND_TOPMOST(-1)` 会被截成 32 位，函数静默失败。
2. **窗口只能按 PID 操作**，绝不能按进程名——否则会把用户日常浏览器一起藏掉或杀掉。
3. **结束进程树用 taskkill /T**。只杀父进程的话，孙进程（ssh、node）会继续占着管道。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import threading
import time
from ctypes import wintypes

PLATFORM_NAME = "windows"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
                             wintypes.LPARAM]
_u32.EnumWindows.restype = wintypes.BOOL
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD
_u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u32.GetWindowTextLengthW.restype = ctypes.c_int
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowTextW.restype = ctypes.c_int
_u32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
_u32.GetWindow.restype = wintypes.HWND
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.restype = ctypes.c_int
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.ShowWindow.restype = wintypes.BOOL
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.IsWindowVisible.restype = wintypes.BOOL
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_u32.SetWindowLongPtrW.restype = ctypes.c_void_p
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowRect.restype = wintypes.BOOL
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.SetWindowPos.restype = wintypes.BOOL

# 不用 --window-position=-32000,-32000 把后台浏览器放到屏幕外：实测这样起的 Chrome
# 之后 ShowWindow 怎么都叫不出来（登录、「打开浏览器」全失灵）。防闪烁靠 _hide_soon 高频盯。
OFFSCREEN_ARGS: list[str] = []
_u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongPtrW.restype = ctypes.c_void_p

GWL_EXSTYLE = -20
GW_OWNER = 4

# 置前要用到的常量与 argtypes。这个文件开头就写了：不写 argtypes 时
# HWND_TOPMOST(-1) 会被截成 32 位，函数静默失败——所以下面一个都不能省。
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.SetForegroundWindow.restype = wintypes.BOOL
_u32.BringWindowToTop.argtypes = [wintypes.HWND]
_u32.BringWindowToTop.restype = wintypes.BOOL
_u32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_u32.AllowSetForegroundWindow.restype = wintypes.BOOL
_u32.GetForegroundWindow.argtypes = []
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD
_u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_u32.AttachThreadInput.restype = wintypes.BOOL
_u32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
_u32.SwitchToThisWindow.restype = None
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCurrentThreadId.argtypes = []
_k32.GetCurrentThreadId.restype = wintypes.DWORD
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                 ctypes.POINTER(wintypes.FILETIME),
                                 ctypes.POINTER(wintypes.FILETIME),
                                 ctypes.POINTER(wintypes.FILETIME)]
_k32.GetProcessTimes.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


# ------------------------------------------------------------------ 进程

def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetExitCodeProcess.restype = wintypes.BOOL


def exe_path(pid: int) -> str:
    """进程可执行文件全路径；拿不到返回空串。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(h)


def start_time(pid: int) -> float:
    """进程启动时间（epoch 秒）。用来防 PID 复用：端口/记录里存了它，就能确认还是同一实例。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return 0.0
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(k), ctypes.byref(u)):
            return 0.0
        ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return ft / 1e7 - 11644473600  # FILETIME(1601) → Unix epoch
    except Exception:
        return 0.0
    finally:
        _k32.CloseHandle(h)


def kill_tree(pid: int, timeout: float = 8.0) -> bool:
    """结束整棵进程树。返回是否已确认退出。"""
    if not pid_alive(pid):
        return True
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except Exception:
        return pid_alive(pid) is False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def spawn_detached(args: list[str], cwd: str | None = None, background: bool = False) -> int:
    """起一个脱离父进程的进程（无控制台）。返回 pid。

    background=True：让它的第一个窗口「最小化、不激活」（SW_SHOWMINNOACTIVE）。
    不这样的话 Chrome 一启动就拿走前台焦点——窗口虽然马上被藏起来，键盘焦点却一直在它身上，
    用户正在别处打的字会打进一个看不见的浏览器（2026-10-30 实测：命令退出后 20 秒焦点都没还回来）。
    """
    si = None
    if background:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                  # SW_SHOWMINNOACTIVE
    p = subprocess.Popen(args, cwd=cwd, close_fds=True, startupinfo=si,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    return p.pid


# ------------------------------------------------------------------ 窗口

def find_pids_by_exe(prefix: str) -> list[int]:
    """exe 路径落在 prefix 之下的进程（升级/卸载前把它收掉用）。

    为什么不用 taskkill /IM bampiao.exe：同名 exe 可能在别处（另一个安装、另一个测试版），
    按名字杀会误伤。这里按"完整路径以安装目录开头"认人，并且排除自己。
    """
    prefix = str(prefix).rstrip("\\/").replace("/", "\\").lower()
    me = os.getpid()
    ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process -Filter \"name='bampiao.exe' or name='bampiao-desktop.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[int] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_s, path = line.split("|", 1)
        if not pid_s.strip().isdigit():
            continue
        pid = int(pid_s)
        if pid == me or not path:
            continue
        if path.replace("/", "\\").lower().startswith(prefix):
            out.append(pid)
    return out


def find_pids_with(marker: str) -> list[int]:
    """命令行里带 marker 的 chrome 主进程（只看主进程，不要渲染子进程）。

    为什么要它：账号浏览器是独立 Chrome 进程，pid 会变（用户手动关掉、
    Chrome 自己重启、换版本清了 state）。只按记录下来的 pid 找窗口时，
    "收起"就会收了个寂寞。按 profile 路径反查，才算认得出"还是这个人格"。
    """
    if not marker:
        return []
    # 不用 wmic：Windows 11 24H2 起默认不装，朋友电脑上这个兜底会悄悄失效。
    # 也不能只查 chrome.exe：Edge 账号的进程叫 msedge.exe，以前永远找不到。
    ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process -Filter \"name='chrome.exe' or name='msedge.exe'\" | "
          "ForEach-Object { \"$($_.CommandLine) $($_.ProcessId)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[int] = []
    # 分隔符要和命令行统一：配置里写的是 C:/Users/.../profiles/acc1（正斜杠），
    # 命令行里是反斜杠。不归一化就永远匹配不上，兜底等于没有（实测踩到）。
    needle = marker.replace("\\", "/").lower()
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or not line[-1].isdigit():
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        cl, pid = parts[0], int(parts[1])
        if " --type=" in cl:                 # 渲染/GPU 子进程没有自己的窗口
            continue
        if needle in cl.replace("\\", "/").lower():
            out.append(pid)
    return out


def _top_level_windows(pid: int) -> list[int]:
    found: list[int] = []

    def cb(hwnd, _):
        pidv = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pidv))
        if pidv.value == pid and _u32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    _u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return found


def window_probe_cheap() -> bool:
    """Windows 上枚举窗口就是几个 Win32 调用，很便宜。"""
    return True


def list_windows(pid: int) -> list[dict]:
    """该进程的**所有**顶层窗口（含空标题的），够判断"它会不会出现在任务栏"。

    为什么要这条：用户截图里任务栏冒出 `MSCTFIME UI` / `Default IME` 两个按钮，
    那是 Chrome 的输入法 UI 窗口——它们没有标题，按标题筛的逻辑根本看不见。
    出问题时用 `bampiao windows` 把这张表打出来，比猜快得多。
    """
    out: list[dict] = []

    def cb(hwnd, _):
        pidv = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pidv))
        if pidv.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(128)
        _u32.GetClassNameW(hwnd, cls, 128)
        ttl = ctypes.create_unicode_buffer(512)
        _u32.GetWindowTextW(hwnd, ttl, 512)
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
        owner = _u32.GetWindow(hwnd, GW_OWNER)
        owner_visible = bool(owner) and bool(_u32.IsWindowVisible(owner))
        out.append({
            "hwnd": int(hwnd), "class": cls.value, "title": ttl.value,
            "visible": bool(_u32.IsWindowVisible(hwnd)),
            "toolwindow": bool(ex & WS_EX_TOOLWINDOW),
            "appwindow": bool(ex & WS_EX_APPWINDOW),
            "owner": int(owner or 0), "owner_visible": owner_visible,
            # 任务栏判定：可见 + 不是工具窗 + （没有 owner 或 owner 不可见）
            "taskbar": bool(_u32.IsWindowVisible(hwnd))
                        and not (ex & WS_EX_TOOLWINDOW)
                        and (not owner or not owner_visible),
        })
        return True

    _u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return out


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_ole32 = ctypes.OleDLL("ole32")
_ole32.CoInitialize.argtypes = [ctypes.c_void_p]


def _guid(s: str) -> _GUID:
    g = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


_CLSID_TASKBAR = _guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")
_IID_TASKBAR = _guid("{56FDF342-FD6D-11d0-958A-006097C9A090}")
_taskbar_tls = threading.local()


def _taskbar():
    """当前线程的 ITaskbarList。COM 对象不能跨线程共用，所以按线程缓存。"""
    cached = getattr(_taskbar_tls, "obj", None)
    if cached is not None:
        return cached or None
    try:
        _ole32.CoInitialize(None)          # 幂等；非主线程第一次必须要
        p = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(ctypes.byref(_CLSID_TASKBAR), None, 1,
                                     ctypes.byref(_IID_TASKBAR), ctypes.byref(p))
        if hr != 0 or not p:
            _taskbar_tls.obj = False
            return None
        vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        # IUnknown 占 0..2，ITaskbarList: HrInit=3 AddTab=4 DeleteTab=5 ActivateTab=6
        init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vt[3])
        delete = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)(vt[5])
        init(p)
        _taskbar_tls.obj = (p, delete)
        return _taskbar_tls.obj
    except Exception:
        _taskbar_tls.obj = False
        return None


def _drop_taskbar_button(hwnd) -> bool:
    """把窗口从任务栏删掉。

    **光 ShowWindow(SW_HIDE) 不够**：任务栏按钮是窗口创建那一刻登记的，
    事后改 WS_EX_TOOLWINDOW 刷不掉那份缓存。2026-10-31 用户截图里两个
    【半瓢·R123…】按钮就是这么来的——窗口 vis=False、tool=True，
    可按钮还挂在任务栏上；DeleteTab 之后立刻消失（实测返回 S_OK）。
    """
    tb = _taskbar()
    if not tb:
        return False
    p, delete = tb
    try:
        return delete(p, hwnd) == 0
    except Exception:
        return False


def automation_ok() -> bool:
    """Windows 没有 macOS 那种「自动化」授权，永远算可用。

    说明：`platform/__init__.py` 里本来就有 `getattr(impl, "automation_ok", lambda: True)`
    兜底，所以 Windows 缺它也不会炸（我一度误以为这是巡逻失效的根因，查证后不是）。
    这里显式写出来，是为了让两个平台的实现对齐、契约测试能一眼看出差异。
    """
    return True


def drop_taskbar_buttons(pid: int) -> int:
    """把这个进程所有窗口从任务栏删掉（不改可见性、不改样式）。

    巡逻每轮都要调它。原因是：窗口可能已经 SW_HIDE 了，任务栏按钮却还在——
    这时 `window_visible(pid)` 是 False，巡逻原先根本不会进 `hide_window`，
    于是那个按钮就永远留在任务栏上（2026-10-31 用户升级到 0.2.12 后仍然看到）。
    """
    n = 0
    try:
        for hwnd in _top_level_windows(pid):
            if _drop_taskbar_button(hwnd):
                n += 1
    except Exception:
        pass
    return n


def hide_window(pid: int) -> int:
    """把该进程的顶层窗口从任务栏拿掉并隐藏。返回处理了几个窗口。

    先设 WS_EX_TOOLWINDOW 再去掉 APPWINDOW：这一步是任务栏按钮消失的关键。
    注意：**不要**在这里再 ShowWindow(SHOWNOACTIVATE) 去"刷新"，那会让窗口在屏幕上闪一下。

    还要顺手收掉输入法的 UI 窗口（`MSCTFIME UI` / `IME`）：它们**没有标题**，
    上面那圈按标题筛的逻辑根本看不到它们。用户在登录页里打字时它们会变可见，
    我们把浏览器主窗口一藏，它们就成了"所有者不可见的可见窗口"——Windows 会把它们
    提升成任务栏按钮，用户就看见两个"MSCTFIME UI / Default IME"挂在那儿
    （2026-10-30 用户截图实锤）。
    """
    n = 0
    for hwnd in _top_level_windows(pid):
        # 收起时一并取消置顶，否则下次它还会压在所有窗口上面
        try:
            _u32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_NOTOPMOST), 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE)
        except Exception:
            pass
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
        if ex & WS_EX_TOOLWINDOW:
            _u32.ShowWindow(hwnd, SW_HIDE)
            _drop_taskbar_button(hwnd)      # 样式已对，但任务栏缓存还在
            n += 1
            continue
        _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        _u32.ShowWindow(hwnd, SW_HIDE)
        _drop_taskbar_button(hwnd)
        n += 1
    n += _hide_ime_windows(pid)
    return n


_IME_CLASSES = ("MSCTFIME UI", "IME")


def _ime_windows(pid: int) -> list[int]:
    """该进程的输入法 UI 窗口（空标题，所以不在 _top_level_windows 里）。"""
    found: list[int] = []

    def cb(hwnd, _):
        pidv = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pidv))
        if pidv.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, buf, 64)
        if buf.value in _IME_CLASSES:
            found.append(hwnd)
        return True

    _u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return found


def _hide_ime_windows(pid: int) -> int:
    """把还可见的输入法 UI 窗口藏掉，并标成工具窗口（这样它再显示也不会进任务栏）。"""
    n = 0
    for hwnd in _ime_windows(pid):
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
        if not (ex & WS_EX_TOOLWINDOW):
            _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        if _u32.IsWindowVisible(hwnd):
            _u32.ShowWindow(hwnd, SW_HIDE)
            n += 1
    return n


def _bring_onscreen(hwnd) -> None:
    """窗口在屏幕外（后台启动时放过去的，或 Chrome 记住了上次的位置）就挪回来。"""
    r = wintypes.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return
    if r.left > -10000 and r.top > -10000:
        return
    w = max(900, min(1280, r.right - r.left))
    h = max(700, min(900, r.bottom - r.top))
    _u32.SetWindowPos(hwnd, None, 120, 60, w, h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE


def _force_foreground(hwnd) -> bool:
    """把窗口真的拽到最前。

    光 SetForegroundWindow 不够：调用方不是前台进程时，Windows 会**静默拒绝**。
    标准绕法是 AttachThreadInput 把自己的输入队列挂到当前前台线程上，
    抢完再解挂；SwitchToThisWindow 作为兜底。
    """
    me = _k32.GetCurrentThreadId()
    fg = _u32.GetForegroundWindow()
    tid = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if tid and tid != me:
            attached = bool(_u32.AttachThreadInput(tid, me, True))
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
        _u32.SwitchToThisWindow(hwnd, True)
    finally:
        if attached:
            _u32.AttachThreadInput(tid, me, False)
    return _u32.GetForegroundWindow() == hwnd


def show_window(pid: int, activate: bool = True) -> int:
    """把窗口叫回桌面（不重启进程，保住登录态）。"""
    n = 0
    for hwnd in _top_level_windows(pid):
        _bring_onscreen(hwnd)
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
        _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
        _u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if not activate else 9)  # 9 = SW_RESTORE
        if activate:
            # 置顶：Windows 只让"有前台权限"的进程抢前台，我们这种后台服务
            # 调 SetForegroundWindow 会被静默拒绝（实测 AttachThreadInput 也不行）。
            # 置顶不需要权限，用户一定看得见；收起时再取消（见 hide_window）。
            try:
                _u32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                  SWP_NOMOVE | SWP_NOSIZE)
            except Exception:
                pass
            # 只 ShowWindow 是不够的：窗口"可见"但还在别的窗口后面，用户看到的是
            # "点了没反应"（2026-11-02 实测：visible=True、iconic=False、但它不在最前）。
            # 先抢前台权限，再置顶一拍再放下来，保证真的跳到人眼前。
            try:
                _u32.AllowSetForegroundWindow(0xFFFFFFFF)     # ASFW_ANY
            except Exception:
                pass
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, 9)
            try:
                _force_foreground(hwnd)
            except Exception:
                pass
        n += 1
    return n


def window_visible(pid: int) -> bool:
    return any(_u32.IsWindowVisible(h) for h in _top_level_windows(pid))


# ------------------------------------------------------------------ 端口

def port_open(port: int, timeout: float = 0.35) -> bool:
    """本机端口是否可连。比 HTTP 探测快，用于快速否定。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def cdp_alive(port: int, timeout: float = 0.6) -> bool:
    """CDP 是否真的可用（只有 Chromium 系会响应 /json/version）。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % int(port),
                                    timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return bool(d.get("webSocketDebuggerUrl"))
    except Exception:
        return False


# ------------------------------------------------------------------ 其它

def is_windows() -> bool:
    return os.name == "nt"

# WebView2 运行时（pywebview 在 Windows 上就靠它）。缺了会开不出窗口，
# 表现是"双击没反应"或一片空白——所以必须能查出来并说人话。
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_version() -> str:
    """返回已装的 WebView2 版本号；没装返回空串。

    两处都要看：机器级安装和用户级安装（Edge 自带的那个在用户目录）。
    """
    import winreg

    # 注意是**双反斜杠**：这是给 winreg 的路径，不是 raw string
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE,
         "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
        (winreg.HKEY_LOCAL_MACHINE,
         "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
        (winreg.HKEY_CURRENT_USER,
         "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID),
    ):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v:
                    return str(v)
        except OSError:
            continue

    import glob
    import os
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                        "EdgeWebView", "Application")
    for exe in glob.glob(os.path.join(base, "*", "msedgewebview2.exe")):
        return os.path.basename(os.path.dirname(exe))
    return ""


# ------------------------------------------------------------------ 系统代理
def system_proxy() -> str:
    """读 WinINET 的系统代理（形如 127.0.0.1:8899）；没开返回空串。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
            if not int(enable or 0):
                return ""
            return str(winreg.QueryValueEx(k, "ProxyServer")[0] or "")
    except Exception:
        return ""


def proxy_works(server: str, host: str = "chatgpt.com", port: int = 443,
                timeout: float = 8.0) -> bool:
    """这个代理能不能真的把 HTTPS 送到对端。

    2026-11-03 事故：res-downloader 把系统代理写成它自己的 127.0.0.1:8899，
    退出后没还原。它**会回 CONNECT 200**，但隧道里什么都送不出去——
    只测 CONNECT 会被骗过（当天就误判成"代理正常"）。
    所以这里真握手、真发一个 GET，拿到以 HTTP/ 开头的一行才算通。
    """
    if not server:
        return False
    hp = server.split(";")[0].strip()
    if hp.lower().startswith(("http://", "https://")):
        hp = hp.split("://", 1)[1]
    if ":" not in hp:
        hp = hp + ":8080"
    host_part, _, port_part = hp.partition(":")
    try:
        sock = socket.create_connection((host_part, int(port_part)), timeout=min(4.0, timeout))
    except OSError:
        return False
    try:
        sock.settimeout(timeout)
        req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
               % (host, port, host, port)).encode()
        sock.sendall(req)
        head = sock.recv(200)
        if not (head.startswith(b"HTTP/") and b"200" in head[:20]):
            return False
        try:
            import ssl

            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except Exception:
            return False
        try:
            tls.settimeout(timeout)
            tls.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                         % host).encode())
            data = tls.recv(16)
            # 只要对端回了 HTTP 状态行就说明隧道是通的（403 也算通：那是 Cloudflare 在回话）
            return data.startswith(b"HTTP/")
        except Exception:
            return False
        finally:
            try:
                tls.close()
            except OSError:
                pass
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


