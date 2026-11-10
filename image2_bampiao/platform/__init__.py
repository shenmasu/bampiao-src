"""平台层入口：按操作系统选实现。

**不在这里做"降级空操作"**：mac/其他平台缺什么就直接报错，绝不假装成功。
设计稿要求：mac.py 只声明未实现的能力，不能静默用空操作伪装可用。
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from . import win as impl
elif sys.platform == "darwin":
    from . import mac as impl
else:  # pragma: no cover - 目前只支持 win/darwin
    from . import mac as impl  # mac.py 会逐项抛 NotImplementedError

current = impl.PLATFORM_NAME

pid_alive = impl.pid_alive
exe_path = impl.exe_path
start_time = impl.start_time
kill_tree = impl.kill_tree
spawn_detached = impl.spawn_detached
hide_window = impl.hide_window
drop_taskbar_buttons = impl.drop_taskbar_buttons
show_window = impl.show_window
window_visible = impl.window_visible
find_pids_with = impl.find_pids_with
def _no_find_pids(prefix):
    """同上：不实现就报错，不静默返回空列表。"""
    raise NotImplementedError("这个平台没有实现 find_pids_by_exe（%s）" % sys.platform)


find_pids_by_exe = getattr(impl, "find_pids_by_exe", _no_find_pids)
def _no_list_windows(pid):
    """某平台没实现就**大声报错**，不许回退成空列表。

    2026-10-30 踩过：mac.py 没有 list_windows，回退成 lambda: []，
    于是"窗口守护"在 Mac 上把每个账号都判成"没有窗口"，一辈子不动手，还不报错。
    静默降级比崩掉更难查。
    """
    raise NotImplementedError("这个平台没有实现 list_windows（%s）" % sys.platform)


list_windows = getattr(impl, "list_windows", _no_list_windows)
# 探测窗口可见性是否便宜（mac 有 Quartz 才便宜；影响巡逻间隔）
window_probe_cheap = getattr(impl, "window_probe_cheap", lambda: True)
port_open = impl.port_open
cdp_alive = impl.cdp_alive

# 可选能力：没有实现的平台给一个"没问题"的默认，别让调用方到处判空
automation_ok = getattr(impl, "automation_ok", lambda: True)
webview2_version = getattr(impl, "webview2_version", lambda: "")

__all__ = ["current", "pid_alive", "exe_path", "start_time", "kill_tree",
           "spawn_detached", "hide_window", "show_window", "window_visible",
           "port_open", "cdp_alive", "find_pids_with", "find_pids_by_exe", "list_windows",
           "window_probe_cheap", "drop_taskbar_buttons",
           "automation_ok", "webview2_version"]
