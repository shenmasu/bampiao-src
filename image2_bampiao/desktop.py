"""桌面壳：pywebview 窗口 + pystray 托盘。

线程约束（实测与官方文档一致）：
- pywebview 的 GUI 循环必须占**主线程**；
- pystray 在 Windows 可以跑在别的线程，所以托盘放后台线程；
- 关窗口 = 隐藏（不是销毁），只有托盘「退出」才真正结束。

自检：`bampiao-desktop --selftest 3 --report out.json`
  只用来验证"能起窗、托盘活着、能退干净"，不接任何出图逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import paths
from . import platform as plat

PKG = Path(__file__).resolve().parent
INDEX = PKG / "ui" / "static" / "index.html"


def _log_path() -> Path:
    base = os.environ.get("BAMPIAO_DATA") or str(paths.data_dir())
    p = Path(base) / "logs" / "desktop.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _redirect_std() -> None:
    """GUI 子系统里 sys.stdout/stderr 是 None，任何 print 都会炸。

    更糟的是崩了之后用户什么都看不到（没有控制台）。所以：
    - 是 None 就整个换成日志文件；
    - 不是 None（终端里跑、或被 open 起了管道）也**追加写一份到日志**，
      否则打包后排查起来就是睁眼瞎。
    """
    try:
        f = open(_log_path(), "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = f
    else:
        sys.stdout = _Tee(sys.stdout, f)
    if sys.stderr is None:
        sys.stderr = f
    else:
        sys.stderr = _Tee(sys.stderr, f)


class _Tee:
    """同时往原流和日志文件写；任何一边坏掉都不影响另一边。"""

    def __init__(self, a, b):
        self._a, self._b = a, b

    def write(self, s):
        for f in (self._a, self._b):
            try:
                f.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for f in (self._a, self._b):
            try:
                f.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._a, name)


def _rotate_logs(max_bytes: int = 2 * 1024 * 1024, keep_days: int = 7) -> None:
    """日志会一直长：每单一张调试截图（几百 KB）+ gen_*.log + desktop.log 追加。

    不清理的话，跑一百单就是几十 MB，用户既不知道也不会去删。
    启动时做一次：主日志过大只留末尾，超过 7 天的逐单文件删掉。
    """
    base = _log_path().parent
    try:
        p = _log_path()
        if p.exists() and p.stat().st_size > max_bytes:
            tail = p.read_bytes()[-300 * 1024:]
            p.write_bytes(b"...(older log trimmed at startup)...\n" + tail)
    except OSError:
        pass

    cutoff = time.time() - keep_days * 86400
    try:
        for f in base.rglob("*"):
            if not f.is_file() or f.name.startswith("desktop"):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _watch_flag() -> Path:
    """「用户自己退出的，别自动叫回来」标记。

    没有它就等着挨骂：用户点了托盘「退出」，五分钟后又自己冒出来一个窗口。
    标记保留 12 小时——用户真忘了，半天后恢复看护，总比永远不看好。
    """
    return _runtime_path().parent / "do_not_watch"


def _mark_user_quit() -> None:
    try:
        f = _watch_flag()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    except Exception:
        pass


def _clear_user_quit() -> None:
    try:
        _watch_flag().unlink(missing_ok=True)
    except Exception:
        pass


def _user_quit_recently() -> bool:
    try:
        d = json.loads(_watch_flag().read_text(encoding="utf-8"))
        return time.time() - float(d.get("at") or 0) < 12 * 3600
    except Exception:
        return False


def watch_once() -> int:
    """看护一轮：半瓢不在就拉起来。供计划任务每 5 分钟调一次（`--watch`）。

    为什么要这个：用户根本不会去点开软件。进程半夜挂了，他第二天只想"怎么不能生图了"，
    压根不知道是程序没了。内部的自愈（调度线程守护）只在进程活着时有用，
    进程整个没了必须有外面的东西来拉。
    """
    if _user_quit_recently():
        return 0                    # 用户主动退的，别烦他
    try:
        rt = _runtime_path()
        panel = json.loads(rt.read_text(encoding="utf-8")).get("panel") or {}
        pid = int(panel.get("pid") or 0)
        port = int(panel.get("show_port") or 0)
    except Exception:
        pid, port = 0, 0
    if pid and plat.pid_alive(pid):
        return 0                    # 活着（线程级问题由进程内的 supervisor 管）
    if port:
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            return 0                # 端口还通，说明只是 pid 记错了
        except OSError:
            pass
    # 真的不在：拉起自己（不带参数 = 正常启动）
    try:
        exe = Path(sys.executable)
        args = [str(exe)]
        if exe.suffix.lower() != ".exe":        # 源码跑的情况
            args = [str(exe), "-m", "image2_bampiao.desktop"]
        subprocess.Popen(args, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return 0
    except Exception:
        return 1


def ensure_watchdog_task() -> str:
    """确保「每 5 分钟看一眼」的计划任务在。返回一句话说明结果。

    用计划任务而不是常驻守护进程：守护进程自己也会死，而系统调度不会。
    用桌面壳的 exe（GUI 子系统）跑 --watch，不会闪黑框。
    创建用户级任务不需要管理员权限。
    """
    if sys.platform != "win32":
        return "非 Windows，跳过"
    try:
        exe = Path(sys.executable)
        if exe.suffix.lower() != ".exe":
            return "源码运行，跳过"
        cmd = '"%s" --watch' % exe
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", "BampiaoWatchdog", "/SC", "MINUTE",
             "/MO", "5", "/TR", cmd, "/F", "/RL", "LIMITED"],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return "ok"
        # schtasks 在中文 Windows 上输出 GBK；以前写死 text=True（UTF-8）会直接
        # UnicodeDecodeError 把启动线程炸掉，源码运行时窗口根本起不来。
        raw = (r.stdout or b"") + (r.stderr or b"")
        try:
            txt = raw.decode("mbcs", "replace")
        except Exception:
            txt = raw.decode("utf-8", "replace")
        return txt.strip()[:120] or "创建失败"
    except Exception as e:
        return str(e)[:120]


def _runtime_path() -> Path:
    """runtime.json 的位置——必须和面板服务写的是**同一个文件**。

    以前这里固定用 `_log_path().parent.parent`（永远是 ~/.bampiao）。
    一旦用户在 config.toml 里改了 data_dir，面板把端口写在新目录、
    桌面壳却把 show_port 写到旧目录：第二次双击叫不醒窗口，
    `bampiao quit` 也找不到它（2026-10-29 实测到的分叉）。
    """
    try:
        from . import config as cfg_mod

        c = cfg_mod.load(None, missing_ok=True)
        if c is not None:
            return Path(c.data_dir) / "state" / "runtime.json"
    except Exception:
        pass
    return _log_path().parent.parent / "state" / "runtime.json"


def _instance_dir() -> Path:
    """全局单实例的锚点目录：**不随 BAMPIAO_DATA / data_dir 变**。

    2026-11-01 的教训：单实例锁原来放在数据目录里（_log_path()），于是
    **换个数据目录就能再起一个进程**——每个实例各带一个窗口和一个托盘图标，
    任务栏瞬间一排，用户只能挨个关。单实例的含义是"这台机器只许一个"，
    所以锚点固定落在**程序目录**，和配置无关。

    2026-11-04 又挪了一次：从 `~/.bampiao` 挪到程序目录。
    `%LOCALAPPDATA%\\Bampiao` 是安装器写死的位置，卸载时整个目录一起删，
    不会像以前那样留下一个永远清不掉的隐藏文件夹。
    """
    d = paths.program_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _instance_lock_path() -> Path:
    return _instance_dir() / "instance.lock"


def _pointer_path() -> Path:
    """记录"正在跑的那个"的 pid 与叫醒端口，供第二个进程把它叫到前面。"""
    return _instance_dir() / "instance.json"


def _bring_to_front(pid: int) -> bool:
    """把该进程那个叫「半瓢」的窗口拉到最前面。只对 Windows 有意义。

    为什么要单独做：pywebview 的 show()+restore() 只保证"可见"，
    窗口如果被别的窗口压着，用户会觉得"点了没反应"。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from .platform import win as winplat

        u = ctypes.windll.user32
        for w in winplat.list_windows(pid):
            if (w.get("title") or "") != "半瓢":
                continue
            hwnd = w["hwnd"]
            u.ShowWindow(hwnd, 9)            # SW_RESTORE
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def _allow_foreground() -> None:
    """告诉系统"待会儿那个进程可以抢前台"。

    自己这一方是用户刚点起来的（有前台权限），把权限让给正在跑的那个，
    否则它的 SetForegroundWindow 会被 Windows 静默拒绝，窗口只在后面闪一下。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_ulong(0xFFFFFFFF))
    except Exception:
        pass


def _force_show_by_pid() -> bool:
    """socket 叫不醒时，直接按窗口句柄把那个窗口拉出来。"""
    try:
        d = json.loads(_pointer_path().read_text(encoding="utf-8"))
        pid = int(d.get("pid") or 0)
    except Exception:
        return False
    if not pid or not plat.pid_alive(pid):
        return False
    _allow_foreground()
    return _bring_to_front(pid)


def _poke_running(tries: int = 8, delay: float = 0.4) -> bool:
    """通过全局指针让正在跑的那个把窗口 show 出来。成功返回 True。

    要重试：两个实例同时双击时，先拿到锁的那个还没把 show_port 写进指针，
    后一个此刻读到的是空——直接放弃就会弹"已经在运行"的框，明明再等半秒就行。
    """
    import socket

    _allow_foreground()          # 让正在跑的那个有权限抢前台
    for _ in range(max(1, tries)):
        try:
            d = json.loads(_pointer_path().read_text(encoding="utf-8"))
            port = int(d.get("show_port") or 0)
            pid = int(d.get("pid") or 0)
        except Exception:
            port, pid = 0, 0
        if port and (not pid or plat.pid_alive(pid)):
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=3)
                c.sendall(b"show")
                c.close()
                return True
            except Exception:
                pass
        time.sleep(delay)
    return False


def send_command(cmd: str, wait: float = 0.0) -> tuple[bool, str]:
    """给正在跑的那个半瓢发一句本地指令：show（把窗口叫到前面）/ quit（真退出）。

    为什么要这条通道：托盘图标在 Windows 上默认被收进 ^ 里，用户找不到；
    菜单偶尔点不动时也没有别的办法——只能开任务管理器杀进程。
    这属于"退出都不给"的硬伤，必须有一条命令行的逃生门。
    """
    import socket

    # 先看数据目录里的 runtime.json；找不到再退回全局指针。
    # 为什么要退：用户换过 data_dir（或带 BAMPIAO_DATA 跑）时，第二份配置
    # 算出来的 runtime.json 和正在跑的那个不是同一个文件，命令会报"没在运行"。
    port, pid = 0, 0
    try:
        panel = json.loads(_runtime_path().read_text(encoding="utf-8"))["panel"]
        port = int(panel.get("show_port") or 0)
        pid = int(panel.get("pid") or 0)
    except Exception:
        pass
    if not port or (pid and not plat.pid_alive(pid)):
        try:
            ptr = json.loads(_pointer_path().read_text(encoding="utf-8"))
            port, pid = int(ptr.get("show_port") or 0), int(ptr.get("pid") or 0)
        except Exception:
            pass
    if not port:
        return False, "半瓢没在运行"
    if pid and not plat.pid_alive(pid):
        return False, "半瓢没在运行（上次留下的记录已经过期）"
    if not port:
        return False, "那个半瓢起得比较早，没有这条通道；重启一次半瓢之后就能用了"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
            c.sendall((cmd + "\n").encode("ascii", "ignore"))
    except OSError as e:
        return False, "连不上正在运行的半瓢（%s）" % e

    if wait and pid:
        t0 = time.time()
        while time.time() - t0 < wait:
            if not plat.pid_alive(pid):
                return True, "已退出"
            time.sleep(0.3)
        return True, "已经让它退了，但 %d 秒还没退干净（可能卡在关浏览器）" % int(wait)
    return True, "指令已发出"


def _open_existing_panel() -> bool:
    """叫醒已经在跑的那个窗口（第二次双击时用）。

    以前这里是 `webbrowser.open(url)`——于是用户会看到托盘里一个窗口、
    浏览器里又一个页面，同一个东西两套壳，还以为是两个程序。
    现在改成通过本地端口喊一声"show"，由那个进程把窗口翻到前面。
    """
    ok, _ = send_command("show")
    return ok


def _start_show_server(on_show, on_quit=None) -> int:
    """开一个只认本机的小端口，收 "show" 叫窗口、"quit" 真退出。

    只绑 127.0.0.1，只认一行文本，没有别的能力——比对外暴露 HTTP 安全得多。
    quit 这条是必需的逃生门：托盘图标被 Windows 收进 ^ 里、或者菜单点不动时，
    用户还能 `bampiao-desktop.exe --quit` 收掉它，而不是只能开任务管理器。
    """
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                line = conn.recv(64).strip().lower()
                if line.startswith(b"quit"):
                    if on_quit is not None:
                        on_quit()
                elif line.startswith(b"show"):
                    on_show()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=loop, daemon=True).start()
    return port


def _say(text: str, title: str) -> None:
    """弹一句人话（不带"崩溃"含义，比如"已经在运行了"）。"""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
        elif sys.platform == "darwin":
            safe = text.replace("\\", "\\\\").replace('"', '\\"')
            safe = safe.replace("\n", '" & return & "')
            subprocess.run(
                ["osascript", "-e",
                 'display dialog "%s" with title "%s" buttons {"好"} default button 1'
                 % (safe, title)],
                capture_output=True, timeout=30)
        else:
            print("%s: %s" % (title, text))
    except Exception:
        pass


def _die_screen(err: str) -> None:
    """启动失败时弹一句人话。

    **不要把 traceback 糊到用户脸上**：他看不懂，也不知道该干什么。
    技术细节已经写进 desktop-crash.log，让他把那个文件发过来就行。
    """
    msg = ("半瓢没能启动。\n\n"
           "把这个文件发给作者就能定位：\n%s\n\n"
           "（技术摘要：%s）"
           % (_log_path().with_name("desktop-crash.log"),
              (err or "").strip().splitlines()[-1][:120] if (err or "").strip() else "无"))
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "半瓢 · 启动失败", 0x10)
        else:
            _say(msg, "半瓢 · 启动失败")
    except Exception:
        pass

_state: dict = {"window": None, "tray": None, "started_at": None,
                 "events": [], "quitting": False}



def _close_hint_once(cfg) -> bool:
    """点 X 收起窗口时，告诉用户「它还在后台跑」。

    为什么要说：用户以为关窗口=退出，结果队列还在跑、托盘里还占着；
    或者反过来，以为还在跑却真退出了（0.2.19 之前的行为就是这个）。

    以前是"这辈子只提示一次"（写盘 close_hint_seen.txt）。2026-11-06 收到反馈：
    「半瓢好像会一直自己启动，我经常关掉它，然后不知道啥时候他又自己打开了，有些困扰」——
    翻了日志才知道，他第一天关窗时那条提示就没看见（系统通知被静音，日志里是
    `[notify-suppressed]`），之后再也不提示，于是每次开机自启他都以为是程序自己蹦出来。
    现在改成**每次启动提示一次**：标记只放内存，重启程序才会再提示。
    既不会反复骚扰，也不至于让人彻底失明。
    """
    if _state.get("close_hint_shown"):
        return False
    _state["close_hint_shown"] = True
    try:
        base = Path(getattr(cfg, "data_dir", "") or paths.data_dir())
    except Exception:
        base = paths.data_dir()
    # close_hint.txt 是留给面板的记号：用户下次打开面板时能看到「刚才只是收起，
    # 不是退出」，点「知道了」就删掉。它和上面的"提示过没有"是两件事，别混。
    pending = base / "state" / "close_hint.txt"
    try:
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    except OSError:
        pass
    return True


def _confirm_quit(snap: dict) -> bool:
    """托盘「退出」前问一句。只有真有活没干完时才问。

    不问的代价：用户在出图中途点了退出，回来发现一单卡在 running，
    图只出了一半，还得自己去「不确定」里收拾。
    """
    running = int(snap.get("running") or 0)
    queued = int(snap.get("queued") or 0)
    if not running and not queued:
        return True
    if not sys.platform.startswith("win"):
        return True                       # mac 上退出不杀队列，不打扰
    try:
        import ctypes

        lines = []
        if running:
            lines.append("还有 %d 单正在画，现在退出会打断它。" % running)
        if queued:
            lines.append("还有 %d 单在排队，会留到下次打开时接着跑。" % queued)
        text = "\n".join(lines) + "\n\n要退出吗？"
        MB_YESNO = 0x4
        MB_ICONWARNING = 0x30
        MB_DEFBUTTON2 = 0x100             # 默认停在「否」，手滑回车不会退
        MB_TOPMOST = 0x40000
        MB_SETFOREGROUND = 0x10000
        r = ctypes.windll.user32.MessageBoxW(
            None, text, "退出半瓢",
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND)
        return r == 6                     # IDYES
    except Exception:
        return True


def _close_behavior(quitting: bool) -> str:
    """点右上角 X 时该做什么：quit / hide / minimize。

    · Windows → hide（收进托盘，任务栏不占位）。托盘图标还在，点一下或
      `bampiao show` 都能 show+restore 回来。
    · macOS  → minimize。**不能改成 hide**：点 Dock 图标发的是 reopen 事件，
      pywebview 不处理；只有最小化的窗口，点 Dock 才是系统原生恢复。
      （2026-10-31 有用户因此卡在"点了图标没反应、退也退不掉"。）
    """
    if quitting:
        return "quit"
    return "hide" if sys.platform.startswith("win") else "minimize"

_INSTANCE_LOCK = None      # 全局单实例锁的句柄，留着别关
_LEGACY_LOCK = None        # 旧版（<=0.2.18）那把数据目录锁，只为兼容
_TRAY_SIG = {"v": None}    # 上一次画进托盘的数字，没变就不重建菜单


def _note(ev: str) -> None:
    _state["events"].append({"t": round(time.time(), 3), "ev": ev})


def _safe(fn, *a) -> None:
    try:
        fn(*a)
    except Exception:
        pass


def _single_click_icon(on_open):
    """返回一个"单击托盘图标就打开半瓢"的 pystray.Icon 子类。

    pystray 在 Windows 上把 WM_LBUTTONUP 直接变成 `self()`，而基类 `self()` 会去跑
    菜单里的 default 项。2026-10-29 那个「Edge 里开了 727 个标签页」就是 default 项
    加上每 5 秒重建菜单联手造成的（当时 on_open 还会 webbrowser.open）。
    所以这里**不设 default**，而是把 `self()` 接管成"打开窗口"：
    单击就开，重建菜单也不会顺带触发。用户原话："单击打不开，跟死了似的。"
    """
    import pystray

    class _SingleClickIcon(pystray.Icon):
        def __call__(self):
            try:
                on_open()
            except Exception:
                pass

    return _SingleClickIcon


def _make_tray(on_open, on_quit, on_pause):
    import pystray
    from PIL import Image, ImageDraw

    # 优先用随包的正式图标（弱水三千那只瓢），拿不到才退回几何图形。
    img = None
    try:
        art = INDEX.parent / "icon.png"
        if art.exists():
            img = Image.open(art).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        img = None
    if img is None:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(27, 58, 47, 255))
        d.ellipse([20, 20, 44, 44], fill=(244, 241, 234, 255))

    try:
        _state["tray_base"] = img
    except Exception:
        pass
    menu = pystray.Menu(
        pystray.MenuItem("打开半瓢", lambda: on_open()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("暂停 / 恢复", lambda: on_pause()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda: on_quit()),
    )
    if sys.platform == "darwin":
        # pystray 的 darwin 后端会抢 NSApplication 事件循环，把窗口顶掉；
        # 换成我们自己的 NSStatusItem（接口一样，事件由 pywebview 的循环分发）。
        from .tray_mac import MacTray
        return MacTray("bampiao", img, "半瓢", menu)
    return _single_click_icon(on_open)("bampiao", img, "半瓢", menu)


# --------------------------------------------------------------- 托盘状态
_LOGIN_TXT = {"ok": "已登录", "required": "需登录", "unknown": "未探测"}


_ATTN_CACHE = {"at": 0.0, "v": None}


def _attention_of(store) -> dict:
    """托盘用的待处理快照。3 秒缓存：面板和托盘都在问，别各自扫一遍磁盘。"""
    now = time.time()
    if _ATTN_CACHE["v"] is not None and now - _ATTN_CACHE["at"] < 3:
        return _ATTN_CACHE["v"]
    try:
        v = store.attention(limit=2)
    except Exception:
        v = {"count": 0, "by_class": {}, "jobs": [], "total_jobs": 0}
    _ATTN_CACHE["at"] = now
    _ATTN_CACHE["v"] = v
    return v


def _snapshot(cfg, store) -> dict:
    """托盘要显示的全部内容。**任何异常都返回空壳**，托盘不能因为读状态而消失。"""
    try:
        accs = []
        for a in cfg.accounts:
            st = store.account(a.id)
            accs.append({
                "id": a.id, "name": a.name,
                "login": st.login, "today": st.stat_today,
                "cooling": st.cooling(),
                "cool_min": (max(0, round((st.cooldown_until - time.time()) / 60))
                             if st.cooling() else 0),
            })
        s = store.summary()
        return {"accounts": accs, "queued": s["queued"], "running": s["running"],
                "today": s["today_images"], "paused": store.paused(),
                "attention": _attention_of(store)}
    except Exception:
        return {"accounts": [], "queued": 0, "running": 0, "today": 0, "paused": False,
                "attention": {"count": 0, "by_class": {}, "jobs": [], "total_jobs": 0}}


def _acc_line(a: dict) -> str:
    if a["cooling"]:
        tail = "冷却 %d 分钟" % a["cool_min"]
    else:
        tail = _LOGIN_TXT.get(a["login"], a["login"])
    return "%s · %s · 今天 %d 张" % (a["name"], tail, a["today"])


def _notify(tray, text: str, title: str = "半瓢") -> None:
    """**不发系统通知**（2026-11-01 用户明确要求：通知很打扰）。

    保留这个函数只是为了让调用点不用改；里面只剩日志。
    想看消息的入口是托盘菜单和面板，不弹任何东西。
    注意：启动失败那种 MessageBox 不算通知，那个只在根本起不来时才出现。
    """
    try:
        print("[notify-suppressed] %s: %s" % (title, text))
    except Exception:
        pass


def _dotted(base):
    """给托盘图标右下角加一个红点（不画数字：64px 缩到 16px 会糊）。"""
    try:
        from PIL import ImageDraw

        im = base.copy().convert("RGBA")
        w = im.size[0]
        r = max(6, int(w * 0.30))
        x0, y0 = w - r - 1, w - r - 1
        d = ImageDraw.Draw(im)
        d.ellipse([x0 - 1, y0 - 1, x0 + r + 1, y0 + r + 1], fill=(255, 255, 255, 255))
        d.ellipse([x0, y0, x0 + r, y0 + r], fill=(211, 58, 44, 255))
        return im
    except Exception:
        return base


def _apply_tray(icon, snap: dict, on_open, on_quit, on_pause) -> None:
    """按快照重建菜单。pystray 的菜单不支持"原地改文字"，只能整体换。

    但**数字没变就别重建**：菜单正开着的时候整体换掉，光标下面的项会跳，
    点「退出」可能落到「暂停」上。这是实打实会误操作的。

    0.2.25：签名只保留"会改变菜单文字"的字段（失败数/前两单/暂停/各号登录状态），
    把 running/today 剔除——出图过程中每张图都会变，以前每个 5 秒周期都可能重建。
    """
    import pystray

    att = snap.get("attention") or {}
    cnt = int(att.get("count") or 0)
    jobs = (att.get("jobs") or [])[:2]
    total = int(att.get("total_jobs") or len(jobs) or 0)

    sig = (cnt, tuple(j.get("id") for j in jobs), bool(snap.get("paused")),
           tuple((a.get("name"), a.get("login"), bool(a.get("cooling")))
                 for a in snap.get("accounts") or []))
    if sig == _TRAY_SIG["v"]:
        return
    _TRAY_SIG["v"] = sig

    items = []
    if cnt:
        items.append(pystray.MenuItem(
            "%d 张没画成（%d 单）" % (cnt, total),
            lambda: on_open("attention")))
        for j in jobs:
            label = "%s · %s%s" % (
                (j.get("brief") or "…")[:12],
                time.strftime("%H:%M", time.localtime(float(j.get("at") or 0))),
                (" · %d 张" % j["failed_count"]) if int(j.get("failed_count") or 0) > 1 else "")
            items.append(pystray.MenuItem(
                label, (lambda jid=j.get("id"): on_open("job", jid))))
        if len(jobs) < 2:
            items.append(pystray.MenuItem("今天 %d 张" % snap["today"], None, enabled=False))
    else:
        # 3 行恒定，且都有真实内容——别摆"（暂无更多）"糊脸
        items.append(pystray.MenuItem("没有要处理的任务", None, enabled=False))
        items.append(pystray.MenuItem("今天 %d 张" % snap["today"], None, enabled=False))
        items.append(pystray.MenuItem(
            "待跑 %d · 在跑 %d" % (snap["queued"], snap["running"]), None, enabled=False))

    items += [pystray.Menu.SEPARATOR,
              pystray.MenuItem("打开半瓢", lambda: on_open())]
    if snap["accounts"]:
        for a in snap["accounts"]:
            items.append(pystray.MenuItem(_acc_line(a), lambda: None, enabled=False))
    else:
        items.append(pystray.MenuItem("还没有账号", lambda: None, enabled=False))
    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("待跑 %d · 在跑 %d · 今天 %d 张"
                         % (snap["queued"], snap["running"], snap["today"]),
                         lambda: None, enabled=False),
        pystray.MenuItem("恢复队列" if snap["paused"] else "暂停队列",
                         lambda: on_pause()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda: on_quit()),
    ]
    try:
        icon.menu = pystray.Menu(*items)
        icon.title = ("半瓢 · %d 张没画成 · 待跑 %d · 在跑 %d"
                      % (cnt, snap["queued"], snap["running"])) if cnt else (
                     "半瓢 · 待跑 %d · 在跑 %d · 今天 %d 张"
                     % (snap["queued"], snap["running"], snap["today"]))
        base = _state.get("tray_base")
        if base is not None:
            icon.icon = _dotted(base) if cnt else base
        icon.update_menu()
    except Exception:
        pass


def _start_worker(cfg) -> None:
    """在桌面壳里跑调度器：装完就能在面板里排队出图，不用另开命令行。

    失败绝不能带走界面——面板还能看历史，只是不会自己跑新单。
    """
    try:
        from .core.coordinator import Coordinator
        from .core.store import Store

        stop = threading.Event()
        _state["worker_stop"] = stop
        store = Store(cfg)

        # 残留清理独立成一条线程：托盘的 _poll 线程万一挂了，这里还在。
        # "显示在跑"和"真的在跑"是两件事——job.json 写着 running 只说明被领走了；
        # 账号的出图锁空着 = 没有进程在画，90 秒后标成"不确定"，别让用户对着幽灵等。
        def _reap_stale() -> None:
            from .cli import gen_in_progress

            while not stop.wait(10):
                try:
                    alive = gen_in_progress(cfg)
                    store.requeue_unstarted(alive=alive, min_age=90)
                    store.recover_running(alive=alive, min_age=90)
                except Exception:
                    pass

        threading.Thread(target=_reap_stale, daemon=True, name="stale-reaper").start()

        # 升级流程为了等空档会临时暂停队列（pause.json 里 by=update）。
        # 如果装的过程中被打断（关机 / 装失败 / 用户取消），这份暂停会留下来，
        # 重启后半瓢就静默不干活——而且界面上看不出是谁停的。
        # 开机时只清「升级留下的」这一种，用户自己按的暂停一律不动。
        try:
            if store.clear_update_pause():
                print("上次升级留下的暂停已解除（队列恢复）")
        except Exception:
            pass
        # 上次被强杀/断电时正在跑的单，磁盘上还挂着 running。
        # 不清的话：顶栏"在跑"永远不归零、卡片上的耗时一直涨，用户以为卡死了。
        try:
            from .cli import gen_in_progress
            stuck = store.recover_running(alive=gen_in_progress(cfg))
            if stuck:
                print("上次退出时没跑完的单已标成「不确定」（不会自动重跑）：%s"
                      % ", ".join("R%d" % i for i in stuck))
        except Exception as e:
            print("!! 清理残留的单失败：%s" % e)
        co = Coordinator(cfg, store)
        threading.Thread(target=co.run_forever,
                         kwargs={"poll": 5.0, "stop": stop},
                         daemon=True, name="coord").start()

        def _supervise() -> None:
            """看门狗：调度线程全没了就重启。

            worker 内部已经兜住异常，这里是第二道保险——真遇到进程级问题
            （线程被 _thread.interrupt、异常从 C 层冒出来）也不至于让队列静默停摆。
            宁可短暂重复一个调度器，也不能让用户对着"都是等待"干等。
            """
            while not stop.wait(15):
                if any(t.is_alive() for t in threading.enumerate()
                       if t.name.startswith("coord-")):
                    continue
                _note("scheduler-dead-restarting")
                try:
                    Coordinator(cfg, store).run_forever(poll=5.0, stop=stop)
                except Exception as e:      # noqa: BLE001
                    _note("scheduler-restart-failed:%s" % str(e)[:40])

        threading.Thread(target=_supervise, daemon=True, name="coord-supervisor").start()
        _start_window_keeper(cfg, stop)
        _note("worker-started")
    except Exception:
        _note("worker-failed")


def _start_window_keeper(cfg, stop) -> None:
    """账号浏览器的窗口一冒头就藏回去（登录期间除外），见 browser.keep_hidden。"""
    from . import config as cfg_mod
    from .driver import browser as br

    cache = {"cfg": cfg, "mtime": 0.0}

    def get_cfg():
        # 面板里加/删号会改 config.toml：按修改时间重载，别每 1.5 秒都解析一遍
        try:
            m = cfg.path.stat().st_mtime
            if m != cache["mtime"]:
                cache["cfg"] = cfg_mod.load(cfg.path)
                cache["mtime"] = m
        except Exception:
            pass
        return cache["cfg"]

    threading.Thread(target=br.keep_hidden, args=(get_cfg, stop),
                     daemon=True, name="window-keeper").start()


def _ensure_config():
    """保证有一份可用配置：首次运行自动初始化，并先建一个账号。

    否则全新机器上 load() 返回 None，窗口只能退回静态页——用户装完打开一片空白，
    会以为装坏了。
    """
    from . import browsers, config as cfg_mod

    cfg = cfg_mod.load(None, missing_ok=True)
    if cfg is not None and cfg.accounts:
        return cfg

    path = cfg.path if cfg is not None else cfg_mod.init(None)[0]
    cfg = cfg_mod.load(path)
    if not cfg.accounts:
        # 挑一个本机真装了的浏览器，别写死 chrome
        browser = "chrome" if browsers.find("chrome") else (
            "edge" if browsers.find("edge") else "chrome")
        cfg_mod.append_account(path, "我的号", browser)
        cfg = cfg_mod.load(path)
    return cfg


def _start_panel():
    """尽量起本地面板，返回 (窗口该打开的地址, httpd 或 None)。

    起不来就退回静态页：窗口还在、界面还能看，只是没有数据。
    这比"窗口打不开"或者"什么都显示不出来"都好。
    """
    try:
        from . import config as cfg_mod
        from .ui import server as server_mod

        cfg = _ensure_config()
        if cfg is None:
            return INDEX.as_uri(), None

        # 面板放到线程里起，最多等 3 秒：绑端口这步在某些网络环境下会卡住
        # （macOS 的 mDNS 反向解析就卡过一次），绝不能让它拖死窗口。
        box: dict = {}

        def _serve():
            try:
                box["r"] = server_mod.serve(cfg, 0, block=False)
            except Exception as e:      # noqa: BLE001 - 交给下面统一兜
                box["e"] = e

        th = threading.Thread(target=_serve, daemon=True, name="panel-start")
        th.start()
        th.join(3.0)
        if "r" not in box:
            raise RuntimeError("面板 3 秒内没起来（%s）" % box.get("e", "超时"))
        httpd, port = box["r"]
        _start_worker(cfg)
        return "http://127.0.0.1:%d/" % port, httpd
    except Exception:
        try:
            print("!! 面板起不来，退回静态页", file=sys.stderr)
        except Exception:
            pass
        return INDEX.as_uri(), None


def _install_crashlog() -> None:
    """崩溃要能自己留档。用户不点「反馈」时也得有东西可查。"""
    try:
        from . import config as _cfg_mod, crashlog

        _cfg = _cfg_mod.load(None, missing_ok=True)
        crashlog.install(_cfg.data_dir)
    except Exception:
        pass


def run(selftest_seconds: float = 0.0, report: str | None = None) -> int:
    import webview

    _note("import-ok")
    _install_crashlog()
    # 优先连本地面板（有真实数据）；面板起不来才退回静态页。
    url, httpd = _start_panel()
    _note("panel" if httpd else "static")
    # Windows 上缺 WebView2 时 pywebview 开不出窗口，表现是"双击没反应/白屏"。
    # 与其让人以为坏了，不如用系统浏览器打开面板，程序留在托盘里。
    no_webview = sys.platform == "win32" and not plat.webview2_version()
    if no_webview:
        # 不再"降级打开网页版"：窗口起不来就说清楚，托盘和命令行照常能用。
        # （网页版这套壳已经删了，用户也不该被迫在浏览器里看面板。）
        _note("no-webview2")
        _say("这台机器缺 WebView2（Edge 的内核），窗口显示不出来。\n\n"
             "命令行照常能用；装一下 WebView2 之后窗口就有了：\n"
             "https://go.microsoft.com/fwlink/p/?LinkId=2124703",
             "半瓢：窗口起不来")
        window = None
    else:
        window = webview.create_window("半瓢", width=1100, height=760,
                                       background_color="#FAF9F6", url=url)
    _state["window"] = window
    _state["started_at"] = time.time()

    if window is not None:
        def _on_closing() -> bool:
            """点 X 只收起窗口，**不销毁**。Windows 进托盘，macOS 最小化。

            为什么要分平台，见 `_close_behavior` 的说明：macOS 的 Dock reopen
            只有最小化才救得回来；Windows 用户明确要"点 X 不占任务栏"。
            """
            action = _close_behavior(bool(_state.get("quitting")))
            if action == "quit":
                return True      # 正在退出：别再拦着，让窗口正常销毁

            if action == "hide":
                try:
                    window.hide()
                except Exception:
                    try:
                        window.minimize()
                    except Exception:
                        pass
                _note("window-hidden-tray")
                # 第一次收起时说清楚"它还在跑"，之后不再打扰
                try:
                    from . import config as _cfgm

                    if _close_hint_once(_cfgm.load(None, missing_ok=True)):
                        _notify(_state.get("tray"),
                                "半瓢还在后台跑，任务不会停。"
                                "点右下角托盘图标（可能藏在 ^ 里）能叫回来；"
                                "要彻底退出：托盘右键 → 退出。",
                                "窗口已收起")
                except Exception:
                    pass
                return False

            try:
                window.minimize()
            except Exception:
                try:
                    window.hide()
                except Exception:
                    pass
            _note("window-minimized")
            return False

        try:
            window.events.closing += _on_closing
        except Exception:
            pass

    def on_open(kind=None, jid=None):
        # 节流：2 秒内只认真一次。以前这里在 window 为 None 时会
        # `webbrowser.open(url)`，被反复调用就在 Edge 里开了 727 个标签页
        # （20:04–21:07，每 5 秒一个，历史库里实测）。
        # 现在就算被循环调用，也只是把窗口叫到前面，不再产生任何外部动作。
        now = time.time()
        if now - _state.get("last_open", 0) < 2:
            return
        _state["last_open"] = now
        if window is None:
            _note("no-window")
            return
        try:
            window.show()
            window.restore()
        except Exception:
            pass
        # show() 只是"可见"，不会把窗口拉到最前面（独立验收提的：窗口在别的窗口下面，
        # 用户以为没反应）。Windows 上补一次 SetForegroundWindow。
        _bring_to_front(os.getpid())
        # 从托盘点"某一单"时，直接把那一单的详情打开
        try:
            if window is not None and kind == "job" and jid:
                window.evaluate_js("openJob(%d)" % int(jid))
            elif window is not None and kind == "attention":
                window.evaluate_js(
                    "S.status='attention';var s=document.querySelector('#fSt');"
                    "if(s)s.value='attention';loadJobs()")
        except Exception:
            pass
        _note("tray-open")

    # 第二次双击靠这条本地端口把窗口叫出来（不再开浏览器标签页）
    # on_quit 在下面才定义，所以这里用 lambda 延迟取——端口收到 quit 时它早就在了
    try:
        show_port = _start_show_server(on_open, lambda: on_quit("quit-command"))
        rt = _runtime_path()
        data = json.loads(rt.read_text(encoding="utf-8"))
        data.setdefault("panel", {})["show_port"] = show_port
        rt.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        # 全局指针：第二个进程靠它找到"在跑的那个"并把它叫到前面。
        # 它必须和锁放在一起（~/.bampiao），否则换了数据目录就找不到。
        try:
            _pointer_path().write_text(json.dumps({
                "pid": os.getpid(), "show_port": show_port,
                "panel_url": data.get("panel", {}).get("url") or "",
                # 这里没有 cfg（它在别的函数里），用 runtime.json 的位置反推数据目录
                "data_dir": str(_runtime_path().parent.parent),
                "started_at": time.time(),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            print("!! 全局指针没写成：%s" % e)
    except Exception as e:
        print("!! 叫醒端口没起来：%s" % e)

    def _shutdown(reason: str = "tray-quit"):
        """真退出：立标志 → **先挂兜底** → 一件件收拾 → 直接结束进程。

        顺序是这次事故改的。以前 `window.destroy()` 排在兜底之前，而 pywebview
        的 destroy 走 `Control.Invoke`（同步等 UI 线程），UI 线程一旦不泵消息就
        永远阻塞 —— 兜底线程根本没机会创建。用户看到的就是"点了退出，什么也没发生"，
        最后只能开任务管理器。

        现在兜底先挂（它自己 sleep 计时），最后无条件 os._exit。
        退出是用户明确要求的动作：宁可干脆杀掉，也不能"按了没反应"。
        """
        if _state.get("quitting"):
            return
        _state["quitting"] = True
        _note(reason)
        # 撤掉全局指针（别让下一次启动以为还有个活的）
        try:
            ptr = _pointer_path()
            if ptr.exists() and json.loads(ptr.read_text(encoding="utf-8")).get("pid") == os.getpid():
                ptr.unlink()
        except Exception:
            pass
        try:
            print("== 正在退出（%s）" % reason)
        except Exception:
            pass

        # selftest 要写报告（报告在 run() 返回之后写），那种情况不能硬退
        hard = report is None
        grace = 12.0

        def _watchdog():
            time.sleep(grace)
            try:
                print("!! 退出超时 %.0f 秒，强制结束进程" % grace, file=sys.stderr)
            except Exception:
                pass
            os._exit(0)

        if hard:
            threading.Thread(target=_watchdog, daemon=True,
                             name="quit-watchdog").start()

        # 1) 让调度器停手：它可能正在等图，硬退会留下半张图和白烧的额度。
        #    只 set 事件、不 join——worker 是 daemon，进程退了它自然没。
        stop = _state.get("worker_stop")
        if stop is not None:
            try:
                stop.set()
            except Exception:
                pass

        # 2) 收掉账号浏览器。**这就是"退出之后任务栏还挂着 Chrome 窗口"的来源**：
        #    浏览器是独立进程，父进程退了它照样活着，而且里面带着登录态。
        #    以前只有空闲 30 分钟才会收，正常退出反而不收。
        #    给它 6 秒预算：某个浏览器卡住时不能让"退出"跟着一起卡。
        def _close_browsers():
            try:
                if cfg is None:
                    return
                if report is not None:
                    # 自检只验桌面壳能起能退，浏览器不是它开的就不该收：
                    # 2026-10-30 打包冒烟时把命令行正在出图的浏览器收掉了，那张图直接失败。
                    print("  自检模式：不动账号浏览器")
                    return
                from .driver import browser as br
                for acc in cfg.accounts:
                    try:
                        ok, note = br.stop(cfg, acc)
                        if ok:
                            print("  已收掉「%s」的浏览器（%s）" % (acc.name, note))
                    except Exception as e:
                        print("  !! 收「%s」的浏览器失败：%s" % (acc.name, e))
            except Exception as e:
                print("  !! 关浏览器失败：%s" % e)

        th = threading.Thread(target=_close_browsers, daemon=True, name="close-browsers")
        th.start()
        th.join(6.0)

        # 3) 托盘图标（两个平台都先摘，mac 上必须先于 destroy）
        t = _state.get("tray")
        if t is not None:
            _safe(t.stop)

        # 4) 窗口（可能卡住，所以排在兜底之后）
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

        # 5) 清掉 runtime.json：留着它，下一次双击会去连一个已经死掉的端口
        try:
            rt = _runtime_path()
            if rt.exists():
                rt.unlink()
        except Exception:
            pass

        _state["exiting"] = True
        if hard:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)

    def on_quit(reason: str = "tray-quit"):
        # 真退出前问一句：在跑的单会被打断（只问有活的时候）
        if reason == "tray-quit":
            try:
                if cfg is not None and store is not None and not _confirm_quit(_snapshot(cfg, store)):
                    _note("quit-cancelled")
                    _apply_tray(_state["tray"], _snapshot(cfg, store), on_open, on_quit, on_pause)
                    return
            except Exception:
                pass
        # 用户主动退出要留个记号，否则看门狗五分钟后又把它拉回来
        if reason != "update":
            _mark_user_quit()
        # 用线程跑：托盘回调 / 本地端口线程都不该被退出流程堵住
        threading.Thread(target=_shutdown, kwargs={"reason": reason},
                         daemon=True, name="quit").start()

    # 托盘要显示账号状态，就得有个东西定期读一下。读失败也不能让托盘消失。
    cfg, store = None, None
    try:
        from . import config as cfg_mod
        from .core.store import Store

        cfg = cfg_mod.load(None, missing_ok=True)
        if cfg is not None:
            store = Store(cfg)
    except Exception:
        pass

    def on_pause():
        if store is None:
            return
        try:
            store.set_paused(not store.paused(), by="tray")
            _note("tray-pause-toggle")
        except Exception:
            pass

    tray = _make_tray(on_open, on_quit, on_pause)
    _state["tray"] = tray
    if sys.platform == "darwin":
        # NSStatusItem 在主线程建好就行，run() 是空操作，不需要线程
        _note("tray-macos-statusbar")
    else:
        threading.Thread(target=tray.run, daemon=True, name="tray").start()
        _note("tray-thread-started")


    if tray is not None and cfg is not None and store is not None:
        seen = {"today": None, "idle_since": time.time(), "closed": False}

        def _poll():
            while not _state.get("quitting"):
                # 自愈：状态栏图标可能第一次没建成（窗口服务器还没就绪），
                # 再试一次就好；不重试的话用户会遇到"菜单栏什么都没有"且无提示。
                if sys.platform == "darwin" and not getattr(tray, "visible", False):
                    try:
                        from .tray_mac import _on_main
                        _on_main(tray.build)   # AppKit 只能在主线程碰
                    except Exception:
                        pass
                # 假在跑自愈：单子标着 running，但没有任何进程在出图（驱动被杀、崩了）。
                # 不清的话队列永远堵着——2026-10-30 实测：R105 21:21 起跑到 23:31 驱动没了，
                # 任务还挂着 running，后面 4 张一张都没动，用户只看到"好慢"。
                # 只在启动时清一次是不够的：这次就是运行中死的。
                try:
                    from .cli import gen_in_progress, _note as _n  # noqa: F401
                    alive = gen_in_progress(cfg)
                    # 一项都没开始的：直接回队列（没有图，也没烧额度）
                    back = store.requeue_unstarted(alive=alive, min_age=90)
                    if back:
                        print("有单被领走但一项都没开始，已放回队列：%s"
                              % ", ".join("R%d" % i for i in back))
                    stuck = store.recover_running(alive=alive, min_age=90)
                    if stuck:
                        print("发现「假在跑」的单，已标成不确定（不会自动重跑）：%s"
                              % ", ".join("R%d" % i for i in stuck))
                except Exception:
                    pass
                snap = _snapshot(cfg, store)
                _apply_tray(tray, snap, on_open, on_quit, on_pause)

                # 空闲自动收掉浏览器：常驻实例的唯一用途是复用登录态。
                # 30 分钟没活就关掉——省内存，也不让任务栏躺着一堆看不懂的窗口。
                # 有活在跑/在排队时重新计时，一开新单就不关了。
                busy_now = bool(snap.get("running") or snap.get("queued"))
                if not busy_now:
                    try:
                        from .cli import gen_in_progress
                        busy_now = bool(gen_in_progress(cfg))
                    except Exception:
                        busy_now = True     # 判断不了就当忙，宁可多留一会儿
                if busy_now:
                    seen["idle_since"] = time.time()
                    seen["closed"] = False
                elif (not seen["closed"]
                      and time.time() - seen["idle_since"] > 30 * 60):
                    seen["closed"] = True
                    try:
                        from .driver import browser as br
                        for acc in cfg.accounts:
                            ok, note = br.stop(cfg, acc)
                            if ok:
                                print("空闲了半小时，收掉 %s 的浏览器（%s）" % (acc.name, note))
                    except Exception as e:
                        print("!! 空闲关浏览器失败：%s" % e)

                # 不发系统通知（2026-10-30 用户：通知吓一跳，Win/Mac 都不要）。
                # 出图结果只在面板和托盘菜单的数字里体现。
                time.sleep(5)

        threading.Thread(target=_poll, daemon=True, name="tray-poll").start()
        _note("tray-poll-started")

    if selftest_seconds:
        def _auto():
            time.sleep(selftest_seconds)
            _note("selftest-timeout")
            on_quit()
        threading.Thread(target=_auto, daemon=True).start()

    try:
        # 关掉无痕模式：WebView2 在 private_mode=True 时用临时数据目录，
        # localStorage 一关就清——于是「上手引导做完了没有」永远记不住，
        # 每次打开都从头引导（2026-10-31 用户反馈）。
        # 后端另外也记了一份（state/ui.json），这里只是把它修对。
        _wv_kw = {}
        try:
            _sp = Path(cfg.data_dir) / "state" / "webview"
            _sp.mkdir(parents=True, exist_ok=True)
            _wv_kw = {"private_mode": False, "storage_path": str(_sp)}
        except Exception:
            _wv_kw = {}

        def _wv_start(func=None):
            try:
                if func is None:
                    webview.start(**_wv_kw)
                else:
                    webview.start(func, **_wv_kw)
            except TypeError:
                # 老版本 pywebview 不认这两个参数，退回默认（宁可丢 localStorage 也要能开窗）
                if func is None:
                    webview.start()
                else:
                    webview.start(func)

        if window is None:
            # 没有内嵌窗口：靠托盘活着，用户从托盘「退出」才结束
            while not _state.get("quitting"):
                time.sleep(0.4)
        elif sys.platform == "darwin" and tray is not None:
            # 菜单栏图标必须等 NSApplication 跑起来之后再建：
            # 提前碰 NSStatusBar 会在没有窗口服务器时把进程 abort 掉（踩过）。
            try:
                from PyObjCTools import AppHelper

                _wv_start(lambda: AppHelper.callAfter(tray.build))
            except Exception:
                _wv_start()
        else:
            _wv_start()          # 主线程阻塞在这里
    finally:
        _state["exiting"] = True      # 标记正常退出，供强制退出兜底判断
        _note("webview-stopped")
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
        # 托盘停止：Windows 上放线程里，免得它拖住退出；
        # macOS 上本来就没有托盘，直接跳过。
        if tray is not None:
            if sys.platform == "darwin":
                _safe(tray.stop)
            else:
                th = threading.Thread(target=lambda: _safe(tray.stop), daemon=True)
                th.start()
                th.join(0.5)   # 托盘是守护线程，进程退出时自然会收

    if report:
        Path(report).write_text(json.dumps({
            "ok": True, "events": _state["events"],
            "tray_alive_before_stop": getattr(_state["tray"], "visible", None),
            "elapsed": round(time.time() - (_state["started_at"] or time.time()), 2),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 兜底：万一有人用桌面壳这个 exe 去跑驱动（--driver），也得能分派过去，
    # 而不是掉进它自己的 argparse 报 unrecognized arguments（真踩过）。
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "--driver":
        from .cli import main as cli_main
        return cli_main()

    ap = argparse.ArgumentParser(prog="bampiao-desktop")
    ap.add_argument("--selftest", type=float, default=0.0,
                    help="自动跑 N 秒后退出（验证用）")
    ap.add_argument("--report", help="自检结果写到这里（GUI 程序没有 stdout）")
    ap.add_argument("--quit", action="store_true",
                    help="让正在运行的那个半瓢退出（托盘找不到时用这个）")
    ap.add_argument("--watch", action="store_true",
                    help="看护一轮：半瓢不在就拉起来（给计划任务用，不显示窗口）")
    a = ap.parse_args(argv)

    if a.quit:
        ok, msg = send_command("quit", wait=15)
        print(msg)
        return 0 if ok else 2

    if a.watch:
        return watch_once()

    # 数据搬家必须在日志重定向**之前**：Windows 上日志文件开着，
    # 整个目录就改不了名。这里只拿返回值，等 stdout 有了再打印。
    try:
        _moved = paths.migrate_legacy()
    except Exception:
        _moved = ""

    _redirect_std()
    _rotate_logs()
    if _moved:
        print(_moved)

    # 单实例：双击两次会出现两个窗口 + 两个调度器抢同一个队列，
    # 后果是同一张图可能被跑两遍、白烧额度。锁跟着进程走，强杀也会自动释放。
    #
    # 2026-11-01 改成**全局锁**（~/.bampiao/instance.lock）：以前锁在数据目录里，
    # 换个数据目录（改 config / 带 BAMPIAO_DATA 跑）就能再起一个进程，
    # 每个都带窗口和托盘图标，任务栏一排，用户只能挨个关。
    from . import locks
    global _INSTANCE_LOCK
    try:
        _INSTANCE_LOCK = locks.hold(_instance_lock_path())
    except locks.LockUnavailable as e:
        # 目录不可写之类：这不是"已经在跑"，别骗人，直接说清楚
        print("!! 拿不到单实例锁：%s" % e)
        _die_screen("数据目录不可写，半瓢没法启动：\n%s" % e)
        return 1
    if _INSTANCE_LOCK is None:
        print("已经有另一个半瓢在跑了（全局单实例）")
        # 第二次双击多半是"没看到第一个"：叫到前面就行。
        # 顺序：socket → 按窗口句柄强拉 → 面板 URL。
        # 以前最后一步是弹 MessageBox，那会让叫不醒的第二进程**挂在模态框上不退**，
        # 结果又变回两个进程（2026-11-01 独立验收实测到）。现在一律不弹，叫不醒就干净退出。
        if _poke_running():
            print("已把正在运行的那个叫到前面")
            return 0
        if _force_show_by_pid():
            print("已直接把它叫到前面")
            return 0
        if _open_existing_panel():
            print("已打开正在运行的那个面板")
            return 0
        print("那个半瓢没有响应窗口（可能在启动或卡住），本次启动放弃")
        return 0

    # 兼容旧版：0.2.18 及更早的锁在数据目录里，它们不认识全局锁。
    # 我们也把那把锁拿一下，拿不到就说明有个旧版还在跑 —— 两个窗口的根源就在这。
    try:
        _LEGACY_LOCK = locks.hold(_log_path().with_name("desktop.lock"))
    except locks.LockUnavailable:
        _LEGACY_LOCK = None
    if _LEGACY_LOCK is None:
        # 两个原因都会走到这里：真有旧版在跑，或者上一个实例还在退出（最长 12 秒宽限）。
        # 2026-11-02 16:24 实测：退出期间重叠启动，系统里一度出现两个 bampiao-desktop。
        print("检测到另一个半瓢还占着这个数据目录（可能在退出中），本次启动放弃")
        try:
            _INSTANCE_LOCK.close()      # 让出全局锁，别把新版也堵死
        except Exception:
            pass
        send_command("show")
        return 0

    # 启动清理：删半瓢自己攒的垃圾（旧安装包、超期调试截图）。
    # 放在拿到锁之后——两个实例同时删同一批文件会互相报错。
    try:
        from . import maintenance
        maintenance.startup_pass(paths.data_dir(), log=print)
    except Exception:
        pass

    # 拿到锁之后再写看护任务：否则第二个实例会把任务重指到它自己的 exe 上
    # （独立验收发现：便携版/测试实例跑起来会把计划任务改到别处）。
    try:
        _note("watchdog:%s" % ensure_watchdog_task())
    except Exception:
        pass
    # 手动启动了，说明用户想要它跑：撤掉「别自动叫回来」的标记
    _clear_user_quit()

    # 诊断：把同时活着的 bampiao-desktop 都记一笔。锁正常时应该只有自己；
    # 出现两个说明退出期间被重叠启动过（2026-11-02 16:24 遇到过一次）。
    try:
        import subprocess as _sp
        _out = _sp.run(["tasklist", "/FI", "IMAGENAME eq bampiao-desktop.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, timeout=8,
                       creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0)).stdout
        _pids = [x.split(b'","')[1].decode("ascii", "ignore")
                 for x in _out.splitlines() if b"bampiao-desktop.exe" in x]
        if len(_pids) > 1:
            _note("!! 同时存在多个半瓢进程：%s（自己是 %d）" % (",".join(_pids), os.getpid()))
    except Exception:
        pass

    # 启动横幅：GUI 出问题时，这行往往是唯一能对上的线索
    try:
        print("=== 启动 %s | argv=%r | cwd=%s | frozen=%s | py=%s"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), sys.argv, os.getcwd(),
                 bool(getattr(sys, "frozen", False)),
                 sys.version.split()[0]))
    except Exception:
        pass
    try:
        return run(a.selftest, a.report)
    except Exception:
        tb = traceback.format_exc()
        try:
            print(tb[-3000:], file=sys.stderr)
        except Exception:
            pass
        # GUI 程序没有终端，异常必须自己落盘，否则现场只剩一个崩溃报告。
        try:
            crash = _log_path().with_name("desktop-crash.log")
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text("时间：%s\n\n%s\n\n事件：%s\n"
                             % (time.strftime("%Y-%m-%d %H:%M:%S"), tb,
                                json.dumps(_state["events"], ensure_ascii=False)),
                             encoding="utf-8")
        except Exception:
            pass
        if a.report:
            Path(a.report).write_text(json.dumps({
                "ok": False, "error": tb[-1500:],
                "events": _state["events"]}, ensure_ascii=False, indent=1), encoding="utf-8")
        _die_screen(tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
