"""半瓢的路径：所有可写文件收在一个文件夹里。

2026-11-04 改。以前是散的：

| 东西 | 原来在哪 |
|---|---|
| 账号、队列、日志、浏览器 profile | `~/.bampiao`（隐藏目录，卸载**故意不删**） |
| 存下来的图 | `~/Downloads` |
| `bampiao doctor` 的诊断文件 | 桌面 |
| 每个版本下载的安装包 | `~/.bampiao/state/update/`，装完从不删 |

用户在真机上撞到：三个月长到 3.4G，其中 2.1G 是缓存和旧安装包，
而卸载器按设计不碰 `~/.bampiao` —— 等于永远留一堆看不见的东西。

现在：

- **安装版**：`%LOCALAPPDATA%\\Bampiao\\data`，跟程序同一个文件夹，卸载一起收走
- **macOS**：`~/Library/Application Support/Bampiao`（.app 在 /Applications 里不可写，只能这样）
- **源码运行**：还是 `~/.bampiao`，不动 —— 开发机保持原样，不会污染仓库

`BAMPIAO_DATA` 环境变量优先级最高，测试和高级用户靠它隔离。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Bampiao"

# 迁移失败时指向老目录，让 data_dir() 别撒谎。见 migrate_legacy()。
_FALLBACK: Path | None = None


def is_frozen() -> bool:
    """是不是打包后的可执行文件（安装版）。"""
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """程序自己待的目录。

    **这个值不跟 data_dir 配置走。** 单实例锁拿它当锚点：只要锚点固定，
    改配置里的数据目录也起不了第二个实例（2026-11-01 那个「任务栏一排」的 bug）。
    """
    env = os.environ.get("BAMPIAO_HOME")
    if env:
        return Path(env).expanduser()
    if not is_frozen():
        return Path(__file__).resolve().parents[1]
    if sys.platform == "darwin":
        # .app 里的 Contents/MacOS 是只读的，数据只能放 Application Support
        return Path.home() / "Library" / "Application Support" / APP_NAME
    # 跟着 exe 走，不写死 %LOCALAPPDATA%：用户把半瓢装到 D 盘时，
    # 数据也得在 D 盘那个文件夹里，卸载才删得到（2026-11-04 审计 P1）。
    return Path(sys.executable).resolve().parent


def legacy_dir() -> Path:
    """老位置。2026-11-04 之前的版本都写在这里。"""
    return Path(os.path.expanduser("~")) / ".bampiao"


def data_dir() -> Path:
    """数据根：配置、账号 profile、任务、日志、缓存全在它下面。"""
    if _FALLBACK is not None:
        # 迁移没做成（文件被占、跨盘复制失败）：这次启动继续用老目录，
        # 不能一边说"还在老地方"一边把新路径给出去 —— 那就成两份数据了。
        return _FALLBACK
    env = os.environ.get("BAMPIAO_DATA")
    if env:
        return Path(env).expanduser()
    if is_frozen():
        return program_dir() / "data"
    # 源码运行：维持老位置。开发机不该因为这次改动多出一套数据。
    return legacy_dir()


def config_path() -> Path:
    env = os.environ.get("BAMPIAO_CONFIG")
    if env:
        return Path(env).expanduser()
    return data_dir() / "config.toml"


def logs_dir() -> Path:
    return data_dir() / "logs"


def driver_logs_dir() -> Path:
    """驱动层日志 + 调试截图。出图失败时会把最后 3 张截图发给用户。"""
    return logs_dir() / "driver"


def state_dir() -> Path:
    return data_dir() / "state"


def update_dir() -> Path:
    """下载下来的安装包。装完就该清，见 update.sweep_old()."""
    return state_dir() / "update"


def jobs_dir() -> Path:
    return data_dir() / "jobs"


def output_dir() -> Path:
    """用户点「保存图片」的去处。以前是 ~/Downloads，现在收回来。"""
    return data_dir() / "output"


def ensure(p: Path) -> Path:
    """建目录并返回，省得每处都写两行。"""
    p.mkdir(parents=True, exist_ok=True)
    return p


def describe() -> str:
    """给 doctor / 关于页用的一句话。"""
    kind = "安装版" if is_frozen() else "源码运行"
    return "%s · %s" % (data_dir(), kind)


# --------------------------------------------------------------------- 迁移


def _payload(root: Path) -> tuple[int, int]:
    """这个数据目录里有多少真东西：(账号 profile 数, 任务数)。"""

    def count(name: str) -> int:
        try:
            return sum(1 for x in (root / name).iterdir() if x.is_dir())
        except OSError:
            return 0

    return count("profiles"), count("jobs")


def _count_accounts(root: Path) -> int:
    try:
        text = (root / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return text.count("[[accounts]]")


def _worth_rescuing(old: Path, new: Path) -> bool:
    """老位置的东西明显比新位置多，才把老的抢回来。

    走到这里的前提是"老目录还在" —— 迁移成功的话老目录已经被搬走了。
    所以这等价于"这台机器上从来没迁成功过"，新位置只是安装包夹带出来的空壳
    （2026-11-04 的 0.2.30 就是这么让用户以为账号全丢了）。

    判据故意用**数量差**而不是"有没有"：安装包夹带的那份假配置里也有一条
    `[[accounts]]`，只看有没有会判错。新位置已经有任务 = 用户在真用，不抢。
    """
    if _payload(new)[1]:
        return False                      # 新位置跑过任务，别动人家
    if _payload(old)[1]:
        return True                       # 老位置有任务史，新位置一片空白
    return _count_accounts(old) > _count_accounts(new)


def _restore_from_old(old: Path, new: Path, log=None) -> str:
    """新位置是空壳、老位置有真数据 —— 把老数据整锅换上来。

    2026-11-04 真机事故的补丁：安装包夹带了一份刚装出来的 `data\\config.toml`，
    用户升级后 `{app}\\data` 已经有配置，迁移判定"已迁过"直接跳过，面板里账号和
    任务全空（其实一个都没丢，都在 `~/.bampiao`）。

    做法：空壳改名成 `data.preinstall` 挪开 → 老目录整锅 replace 上来。
    **任何一步失败都把空壳放回原处、继续用老目录**，绝不留"新目录里一半、老目录里
    一半"的状态 —— 那种下次启动就再也拼不回来了。
    """
    global _FALLBACK
    import errno
    import shutil

    bak = new.parent / (new.name + ".preinstall")
    try:
        shutil.rmtree(bak, ignore_errors=True)
        os.replace(new, bak)
    except OSError as e:
        _FALLBACK = old
        msg = "旧数据恢复没做成（%s），这次仍用 %s" % (e, old)
        if log:
            log(msg)
        return msg

    try:
        os.replace(old, new)
    except OSError as e:
        if getattr(e, "errno", None) == errno.EXDEV:
            # 跨盘（装到 D 盘那种）：退回复制路线。new 已经挪走，_copy_migrate 会自己收拾。
            return _copy_migrate(old, new, log)
        try:
            os.replace(bak, new)      # 空壳放回去，别让程序启动时找不到配置
        except OSError:
            pass
        _FALLBACK = old
        msg = "旧数据恢复没做成（%s），这次仍用 %s" % (e, old)
        if log:
            log(msg)
        return msg

    _rewrite_config_paths(new, old)
    # 空壳**不删**：万一它里面真有用户刚加的账号，留着还能手工捞回来。
    # 位置也写进消息里，出问题时有处可找。
    msg = "已从旧位置恢复账号和任务（原来在 %s；安装包带来的那份留底在 %s）" % (old, bak)
    if log:
        log(msg)
    return msg


def migrate_legacy(log=None) -> str:
    """把老的 `~/.bampiao` 整个挪进新的 data 目录。返回给人看的一句话。

    **要么整锅搬完，要么原地一点不动。** 中间态是最坏的：一半数据在新位置、
    一半在老位置，下次启动看到"新目录非空"就不搬了，用户以为登录态丢了。
    所以：

    - 同盘：`os.replace` 整目录改名，内核保证原子；
    - 跨盘：先复制到 `data.migrating`，成功后原子改名，失败整个删掉；
    - 被占用 / 权限不足：这次不搬，`_FALLBACK` 指回老目录，下次启动再试。

    只在安装版跑：源码运行本来就还在老位置。
    失败不抛 —— 磁盘上的东西比路径整齐重要，宁可继续用老目录也要能启动。
    """
    global _FALLBACK
    if not is_frozen() or os.environ.get("BAMPIAO_DATA"):
        return ""
    new = program_dir() / "data"      # 故意不用 data_dir()，它受 _FALLBACK 影响
    old = legacy_dir()
    if not old.is_dir():
        return ""

    # 判据是 config.toml，不是"目录非空"：迁移途中留下的空壳不该拦住下一次尝试。
    if (new / "config.toml").exists():
        # ★ 2026-11-04 真机事故：0.2.30 的安装包里夹带了一份刚装出来的 data\config.toml，
        #   用户升级后新位置"已经有配置"，迁移直接 return ""，账号和任务全看不见。
        #   现在反过来判：老位置的东西比新位置多 → 以老位置为准。
        if _worth_rescuing(old, new):
            return _restore_from_old(old, new, log)
        return ""                     # 已经迁过（或另一个进程刚搬完）
    try:
        if new.is_dir() and not any(new.iterdir()):
            new.rmdir()
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)
        _rewrite_config_paths(new, old)
        msg = "数据已迁到 %s（原来在 %s）" % (new, old)
        if log:
            log(msg)
        return msg
    except OSError as e:
        if (new / "config.toml").exists():
            return ""                 # 并发：别人搬完了，别回滚人家
        import errno
        if getattr(e, "errno", None) == errno.EXDEV:
            return _copy_migrate(old, new, log)
        # 被占用（多半是浏览器没退干净）或权限问题：这次先不动。
        # 关键：data_dir() 必须跟着回老目录，否则程序会去空的新目录找配置。
        _FALLBACK = old
        msg = "数据目录这次没搬成（%s），仍用 %s；下次启动再试" % (e, old)
        if log:
            log(msg)
        return msg


def _copy_migrate(old: Path, new: Path, log=None) -> str:
    """跨盘迁移：复制到 staging → 原子改名 → 删老目录。失败不留半个新目录。"""
    global _FALLBACK
    import shutil

    staging = new.parent / (new.name + ".migrating")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(old, staging)
        if new.exists():
            shutil.rmtree(new, ignore_errors=True)
        os.replace(staging, new)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        _FALLBACK = old
        msg = "数据复制到新位置失败（%s），仍用 %s" % (e, old)
        if log:
            log(msg)
        return msg
    _rewrite_config_paths(new, old)
    shutil.rmtree(old, ignore_errors=True)
    msg = "数据已复制到 %s（原来在 %s）" % (new, old)
    if log:
        log(msg)
    return msg


def _rewrite_config_paths(new: Path, old: Path) -> None:
    """配置里如果写死了老目录的绝对路径，跟着改过来。

    默认配置是 `data_dir = "."`（相对配置文件），不用管。但高级用户可能填了
    绝对路径，搬完之后那些路径还指着已经不存在的 `~/.bampiao`，表现就是
    "账号都没了"。改失败也不抛 —— 宁可留个坏配置让人看见，也不要搬一半。
    """
    p = new / "config.toml"
    try:
        s = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    olds = str(old)
    news = str(new)
    if olds not in s and olds.replace("\\", "/") not in s:
        return
    s = s.replace(olds, news).replace(olds.replace("\\", "/"), news.replace("\\", "/"))
    try:
        p.write_text(s, encoding="utf-8")
    except OSError:
        pass
