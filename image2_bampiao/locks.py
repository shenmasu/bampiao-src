"""跨进程的独占锁。

为什么不用"O_EXCL 建文件 + 里面写 pid"那种土办法：
进程如果在临界区里被强杀（任务管理器、蓝屏、断电），锁文件会永远留着，
用户只能自己去删一个他根本不知道存在的文件。msvcrt 的字节锁由内核持有，
**进程一死系统就释放**，不会留下僵尸锁。

用法：

    with exclusive(path, timeout=0, what="账号 acc1 的出图"):
        ...
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class Busy(RuntimeError):
    """锁被别的进程占着，等到超时也没拿到。"""


class LockUnavailable(OSError):
    """锁文件本身建不出来（目录不可写、被杀软挡住）。

    和"被别人占着"必须分开：前者是环境坏了，后者是正常的"已经有一个在跑"。
    混成一种的话，用户会遇到"怎么点都提示已经在运行"却根本起不来。
    """


def hold(path, what: str = "程序"):
    """一直持有独占锁，返回文件句柄；已被别的进程占着就返回 None。

    和 exclusive() 的区别：这个锁要跨整个进程生命周期，不能用 with。
    句柄留着别关，进程退出（含被强杀）时内核会自动释放，不会留僵尸锁。
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "a+b")
    except OSError as e:
        # 建不出锁文件 = 环境问题，不是"已经有一个在跑"
        raise LockUnavailable("建不出锁文件 %s：%s" % (p, e))
    try:
        _lock_fd(fh.fileno())
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        return None
    return fh


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - v1 只发 Windows，这里是为了 mac.py 以后能接
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive(path: Path | str, *, timeout: float = 0.0, poll: float = 0.12,
              what: str = "这个资源"):
    """拿不到锁时抛 Busy；timeout=0 表示不等，立刻抛。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    deadline = time.time() + max(0.0, timeout)
    try:
        while True:
            try:
                _lock_fd(fd)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise Busy("%s 正被另一个进程使用" % what) from None
                time.sleep(poll)
        yield
    finally:
        if got:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            # 关不掉也不能盖住 with 块里的原始异常
            pass


def try_acquire(path: Path | str) -> bool:
    """只问一句"现在能不能拿到"，能拿到就立刻放开。给 doctor / UI 判断忙闲用。"""
    try:
        with exclusive(path, timeout=0):
            return True
    except Busy:
        return False
