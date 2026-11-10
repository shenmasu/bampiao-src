"""macOS 平台层：窗口、进程、端口。

和 Windows 的差异，以及为什么这么写：

1. **隐藏窗口要靠 AppleScript**，没有 Win32 那种 ShowWindow。第一次调用时
   macOS 会弹「半瓢想要控制 System Events」——这是正常现象，允许一次就好。
   用户拒绝时我们**不能装作藏好了**：`hide_window` 返回 0，窗口就留在屏幕上，
   功能照常，只是不安静。

2. **结束进程树没有 taskkill /T**。我们用两个手段叠加：
   先按**进程组** killpg（`spawn_detached` 里开了 start_new_session，
   所以 pgid == pid），失败再按 ppid 递归收集后代。Chrome 会起一堆 helper，
   只杀父进程会留一地孤儿。

3. **启动时间用 `ps -o lstart=`**，必须带 `LC_ALL=C`：
   否则中文/其他语言的系统会给「2026年10月29日 星期四」这种没法解析的格式。

4. **绝不按应用名操作**。按名字隐藏/杀掉会把用户自己开着的 Chrome 一起处理掉
   （Windows 版踩过同样的坑）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

PLATFORM_NAME = "macos"

_PS_ENV = {"LC_ALL": "C", "LANG": "C"}


def _ps(*args: str) -> str:
    """跑一次 ps，返回 stdout（失败给空串，调用方自己判断）。"""
    env = dict(os.environ)
    env.update(_PS_ENV)
    try:
        r = subprocess.run(["ps", *args], capture_output=True, text=True,
                           timeout=6, env=env)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 僵尸（stat=Z）已经死了，只是父进程还没回收。以前这里判成"还活着"，
    # kill_tree 会硬等到超时（独立验证实测多等 11.3 秒）。
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=2)
        if (r.stdout or "").strip().upper().startswith("Z"):
            return False
    except Exception:
        pass
    except PermissionError:
        return True          # 活着，只是不归我们管
    except OSError:
        return False
    return True


def exe_path(pid: int) -> str:
    """进程的可执行文件路径。mac 上 `comm` 就是完整路径。"""
    return _ps("-p", str(pid), "-o", "comm=").strip()


def start_time(pid: int) -> float:
    """进程启动时刻（unix 秒）。

    用 lstart 而不是 etime：etime 是"已经跑了多久"，每次调用都在变，
    拿它当身份标记会让"同一个进程"看起来每次都不同。
    """
    raw = _ps("-p", str(pid), "-o", "lstart=").strip()
    if not raw:
        return 0.0
    try:
        return time.mktime(time.strptime(raw, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return 0.0


def _descendants(pid: int) -> list[int]:
    """按 ppid 收集所有后代（广度优先，带环保护）。"""
    out = _ps("-Ao", "pid=,ppid=")
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            c, p = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        kids.setdefault(p, []).append(c)
    seen: list[int] = []
    queue = [pid]
    guard = set()
    while queue:
        cur = queue.pop(0)
        for c in kids.get(cur, []):
            if c in guard or c == pid:
                continue
            guard.add(c)
            seen.append(c)
            queue.append(c)
    return seen


def kill_tree(pid: int, timeout: float = 8.0) -> bool:
    """结束整棵进程树。成功返回 True（不只是发了信号，而是确认都死了）。"""
    if not pid_alive(pid):
        return True

    targets = _descendants(pid) + [pid]

    # 先走进程组：spawn_detached 里 start_new_session=True，pgid 就是 pid，
    # 一次 killpg 能把 Chrome 的全部 helper 一起带走。
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for p in targets:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(pid_alive(p) for p in targets):
            return True
        time.sleep(0.1)

    # 还赖着就 SIGKILL（浏览器有时不理会 SIGTERM）
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for p in targets:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for _ in range(30):
        if not any(pid_alive(p) for p in targets):
            return True
        time.sleep(0.1)
    return not any(pid_alive(p) for p in targets)


def spawn_detached(args: list[str], cwd: str | None = None, background: bool = False) -> int:
    """起一个和当前进程脱钩的子进程（新会话 = 新进程组），返回 pid。"""
    p = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,      # 关键：pgid == pid，kill_tree 才能整组带走
        close_fds=True,
    )
    return p.pid


# --------------------------------------------------------------- 窗口

def _osascript(script: str, timeout: float = 6.0) -> bool:
    """跑一段 AppleScript，成功返回 True。

    用户没给「自动化」权限时 osascript 会以 -1743 失败；
    这时返回 False，让调用方知道**没藏成**，而不是假装成功。
    """
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _proc_ref(pid: int) -> str:
    return ('(first application process whose unix id is %d)' % int(pid))


def find_pids_with(marker: str) -> list[int]:
    """命令行里带 marker 的 chrome 主进程（macOS 版）。

    Windows 那边用 wmic；mac 用 pgrep -f。都只挑主进程：`--type=` 子进程没有窗口。
    """
    if not marker:
        return []
    try:
        r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[int] = []
    for line in (r.stdout or "").split():
        try:
            pid = int(line)
        except ValueError:
            continue
        try:
            ps = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            continue
        cmd = (ps.stdout or "").strip()
        if " --type=" in cmd:
            continue
        out.append(pid)
    return out


def drop_taskbar_buttons(pid: int) -> int:
    """macOS 上没有"任务栏按钮"这个概念，Dock 图标是按 App 算的。

    这个函数存在只是为了**跨平台调用不炸**：browser.keep_hidden 的巡逻是平台无关的，
    它会在"窗口不可见"那一支上无条件调一次（Windows 那边专门用于清掉残留的
    任务栏按钮 —— Chrome 藏窗之后按钮不撤，是个真问题）。
    macOS 上隐藏整个 App 之后 Dock 图标就没了，不需要额外动作，所以这里直接返回 0。
    """
    return 0


def hide_window(pid: int) -> int:
    script = ('tell application "System Events" to set visible of %s to false'
              % _proc_ref(pid))
    return 1 if _osascript(script) else 0


def show_window(pid: int, activate: bool = True) -> int:
    ref = _proc_ref(pid)
    ok = _osascript('tell application "System Events" to set visible of %s to true' % ref)
    if ok and activate:
        _osascript('tell application "System Events" to set frontmost of %s to true' % ref)
    return 1 if ok else 0


_ONSCREEN_WARNED = False      # Quartz 失败只喊一次，别刷日志


def window_probe_cheap() -> bool:
    """探测窗口可见性是否便宜（不需要 fork）。有 Quartz 就是便宜。"""
    return _quartz() is not None


def _quartz():
    """Quartz 在打好的 app 里一定在（pywebview 带进来的 pyobjc），源码环境可能没有。"""
    try:
        import Quartz  # noqa: F401

        return Quartz
    except Exception:
        return None


def _onscreen(pid: int) -> bool | None:
    """用 CGWindowList 判断这个进程有没有**正在屏幕上显示**的窗口。

    返回 None 表示这个环境没有 Quartz，调用方退回 AppleScript。
    为什么不一直用 AppleScript：每次调用要 fork 一个 osascript，
    实测常驻巡逻把 CPU 吃到 6.5%（独立验证量的），红线是 1%。
    CGWindowListCopyWindowInfo 是进程内调用，几毫秒，且不用权限。
    """
    q = _quartz()
    if q is None:
        return None
    try:
        opts = q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements
        for w in q.CGWindowListCopyWindowInfo(opts, q.kCGNullWindowID) or []:
            try:
                if int(w.get("kCGWindowOwnerPID", -1)) == int(pid)                         and int(w.get("kCGWindowLayer", 1)) == 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    except Exception as e:  # noqa: BLE001
        global _ONSCREEN_WARNED
        if not _ONSCREEN_WARNED:
            _ONSCREEN_WARNED = True
            try:
                sys.stderr.write("[mac] Quartz 探测失败，退回 AppleScript：%s\n" % str(e)[:80])
            except Exception:
                pass
        return None


def list_windows(pid: int) -> list[dict]:
    """macOS 没有"单个窗口是否占任务栏"这一说：Dock 里显示的是**应用是否可见**。

    所以把整个进程压成一条记录，字段名和 win.py 对齐——调用方（窗口守护）
    只认 `taskbar` 这个键，两边就能用同一套逻辑。
    """
    on = _onscreen(pid)
    vis = window_visible(pid) if on is None else on
    return [{"hwnd": 0, "class": "app", "title": "", "visible": vis,
             "toolwindow": False, "appwindow": True, "owner": 0,
             "owner_visible": False, "taskbar": vis}]


def window_visible(pid: int) -> bool:
    on = _onscreen(pid)          # 有 Quartz 就完全不碰 AppleScript（省 fork）
    if on is not None:
        return on
    script = ('tell application "System Events" to get visible of %s' % _proc_ref(pid))
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


# 这两个与平台无关，和 win.py 保持同一份实现
def port_open(port: int, timeout: float = 0.35) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def cdp_alive(port: int, timeout: float = 0.6) -> bool:
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % int(port),
                                    timeout=timeout) as r:
            return bool(json.loads(r.read().decode("utf-8", "replace")).get("webSocketDebuggerUrl"))
    except Exception:
        return False

def find_pids_by_exe(prefix: str) -> list[int]:
    """可执行路径落在 prefix 之下的半瓢进程（升级前收掉用）。排除自己。

    mac 上没有 "按 exe 名字杀" 这种便宜事，用 `ps -axo pid=,command=` 拿全量命令行再筛，
    顺便把 "…/Contents/MacOS/bampiao" 和 "…/Contents/MacOS/bampiao-gui" 都算进来。
    """
    import os as _os

    prefix = str(prefix).rstrip("/") + "/"
    me = _os.getpid()
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, text=True, timeout=10, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[int] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        if not pid_s.isdigit() or int(pid_s) == me:
            continue
        if cmd.startswith(prefix):
            out.append(int(pid_s))
    return out


def automation_ok() -> bool:
    """能不能用 AppleScript 操作别的 App（隐藏浏览器要靠它）。

    第一次会弹「半瓢想要控制 System Events」，允许一次就好；
    用户拒绝后系统记 -1743，这里返回 False——调用方要提示去系统设置里勾选，
    而不是闷头重试。
    """
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process'],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
