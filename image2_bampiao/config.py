"""配置：读取、校验、写默认值。

约定（来自设计稿 §4）：
- 加载顺序：显式 --config → BAMPIAO_CONFIG → ~/.bampiao/config.toml
- data_dir 相对配置文件的目录解析，**不随 cwd 变**
- 账号 = 稳定 id + 浏览器品牌 + 专用 profile + CDP 端口
- 校验失败一律抛 ConfigError，不静默降级
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import locks, paths

SCHEMA_VERSION = 1
ENV_CONFIG = "BAMPIAO_CONFIG"
APP_DIR_NAME = ".bampiao"

BROWSERS = ("chrome", "edge")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")

# 真实浏览器的默认数据目录（含这些片段的一律拒绝）
_DAILY_PROFILE_HINTS = (
    "google/chrome/user data",
    "microsoft/edge/user data",
    "google\\chrome\\user data",
    "microsoft\\edge\\user data",
)


class ConfigError(Exception):
    """配置有问题，且必须由人改配置才能继续。"""


class NeedsUser(ConfigError):
    """环境还没准备好，需要用户动手（加账号 / 去登录 / 装浏览器）。

    和 ConfigError 的区别只在退出码：这个映射到 3（需要用户动作），
    而不是 2（命令写错了）。agent 拿到 2 会去改参数，方向就错了。
    """


@dataclass
class Account:
    id: str
    name: str
    browser: str
    profile: Path | None = None
    port: int = 0
    # "browser" = 真浏览器 CDP 号（默认，行为不变）；"http" = 号池里的 HTTP 号
    # （没有 profile/port，出图走 httpgen，不进浏览器主链路）
    kind: str = "browser"

    def as_row(self) -> dict:
        return {"id": self.id, "name": self.name, "browser": self.browser,
                "profile": str(self.profile) if self.profile else "", "port": self.port,
                "kind": self.kind}


@dataclass
class Config:
    path: Path
    data_dir: Path
    concurrency: int = 2
    idle_recycle_min: int = 10
    # 多账号时怎么挑：balance 用得少的优先 / roundrobin 轮流 / fixed 固定最近用过的
    account_strategy: str = "balance"
    # fixed 模式下固定用哪个账号。**存 id 不存名字**——改名不影响引用。
    # 删号时 remove_account 会把这个引用一起清掉（见下面）。
    account_fixed_id: str = ""
    ui_port: int = 0
    accounts: list[Account] = field(default_factory=list)
    notify_desktop: bool = True
    notify_webhook: bool = False
    webhook_url: str = ""
    # HTTP 号池旁路总开关（[http_pool] enabled）。默认关：关着时 HTTP 号不参与任何流程。
    http_pool_enabled: bool = False

    # ---------------------------------------------------------------- 派生路径
    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def rounds_dir(self) -> Path:
        return self.data_dir / "rounds"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def dirs(self) -> list[Path]:
        return [self.data_dir, self.profiles_dir, self.rounds_dir,
                self.state_dir, self.backups_dir, self.logs_dir]


# -------------------------------------------------------------------- 定位

def default_config_path() -> Path:
    return paths.config_path()


def find_config(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser().resolve()
    return default_config_path()


# -------------------------------------------------------------------- 读取

def load(path: str | Path | None = None, *, missing_ok: bool = False) -> Config | None:
    p = find_config(path)
    if not p.exists():
        if missing_ok:
            return None
        raise ConfigError(
            "找不到配置文件：%s\n先跑 `bampiao init` 生成一份。" % p)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("配置文件不是合法 TOML：%s\n  %s" % (p, e)) from e
    return parse(raw, p)


def parse(raw: dict, path: Path) -> Config:
    schema = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        raise ConfigError("配置 schema_version=%r 太新，请升级 半瓢。" % (schema,))

    base = path.parent
    data_dir = _resolve_dir(raw.get("data_dir", "."), base, "data_dir")

    idle = _int_in(raw.get("idle_recycle_min", 10), 1, 1440, "idle_recycle_min")

    strategy = str(raw.get("account_strategy", "balance") or "balance").strip().lower()
    if strategy not in ("balance", "roundrobin", "fixed", "tier"):
        raise ConfigError(
            "account_strategy 只能是 balance / roundrobin / fixed / tier，收到 %r。" % strategy)
    fixed_id = str(raw.get("account_fixed_id", "") or "").strip()

    ui = raw.get("ui") or {}
    if not isinstance(ui, dict):
        raise ConfigError("ui 必须是表（[ui]），收到 %r。" % (ui,))
    ui_port = _int_in(ui.get("port", 0), 0, 65535, "ui.port")

    notify = raw.get("notify") or {}
    if not isinstance(notify, dict):
        raise ConfigError("notify 必须是表（[notify]），收到 %r。" % (notify,))
    webhook_url = str(notify.get("webhook_url") or "")
    webhook_on = bool(notify.get("webhook_enabled", False))
    if webhook_on and not webhook_url.startswith(("http://", "https://")):
        raise ConfigError("notify.webhook_enabled=true 但 webhook_url 不是 http(s) 地址。")

    http_pool = raw.get("http_pool") or {}
    if not isinstance(http_pool, dict):
        raise ConfigError("http_pool 必须是表（[http_pool]），收到 %r。" % (http_pool,))
    http_pool_enabled = bool(http_pool.get("enabled", False))

    accounts_raw = raw.get("accounts") or []
    if not isinstance(accounts_raw, list):
        raise ConfigError("accounts 必须是数组（[[accounts]]）。")

    accounts: list[Account] = []
    seen_id, seen_port, seen_profile = set(), set(), set()
    for i, a in enumerate(accounts_raw, 1):
        if not isinstance(a, dict):
            raise ConfigError("第 %d 个 [[accounts]] 不是表。" % i)
        aid = str(a.get("id") or "").strip()
        if not ID_RE.match(aid):
            raise ConfigError("账号 id %r 不合法：只能小写字母/数字/下划线，≤32 字符。" % aid)
        if aid in seen_id:
            raise ConfigError("账号 id %r 重复。" % aid)
        seen_id.add(aid)

        name = str(a.get("name") or "").strip()
        if not name:
            raise ConfigError("账号 %s 缺 name。" % aid)

        browser = str(a.get("browser") or "").strip().lower()
        if browser not in BROWSERS:
            raise ConfigError("账号 %s 的 browser=%r 不支持（可选：%s）。"
                              % (aid, browser, "/".join(BROWSERS)))

        prof = _resolve_dir(a.get("profile") or ("profiles/%s" % aid), base, "账号 %s 的 profile" % aid)
        # 先判"是不是日常浏览器目录"，再判越界：日常目录本来就在 data_dir 外，
        # 若先报越界，用户看到的是"路径不对"而不是"别碰你的浏览器"，误导。
        _reject_daily_profile(prof, aid)
        if not _is_inside(prof, data_dir):
            raise ConfigError("账号 %s 的 profile 必须放在 data_dir 里：%s" % (aid, prof))
        if prof in seen_profile:
            raise ConfigError("账号 %s 的 profile 与另一个账号重复：%s" % (aid, prof))
        seen_profile.add(prof)

        port = _int_in(a.get("port", 0), 1024, 65535, "账号 %s 的 port" % aid)
        if port in seen_port:
            raise ConfigError("端口 %d 被两个账号同时占用。" % port)
        seen_port.add(port)

        accounts.append(Account(aid, name, browser, prof, port))

    if len(accounts) > 1 and len({a.port for a in accounts}) != len(accounts):
        raise ConfigError("账号端口必须互不相同。")

    # 默认并发 = 账号数：有几个号就开几个 worker。
    # 以前写死 2 —— 三个号时第三个号永远闲着，用户看到的是"只用一个号在跑"
    # （2026-11-03 被问到）。显式写在配置里的 concurrency 仍然优先。
    # 2026-11-11：上限曾写死 8，13 个号时后 5 个永远闲着；改成跟着账号数涨。
    # 但不能硬等于账号数：init 生成的默认配置是「0 个账号 + concurrency = 2」，
    # 硬等于会让 C.load(DEFAULT_TOML) 直接报错。所以下限保留 8。
    cap = max(8, len(accounts))
    default_conc = max(1, len(accounts))   # 有几个号就开几个 worker
    concurrency = _int_in(raw.get("concurrency", default_conc), 1, cap, "concurrency")

    return Config(path=path, data_dir=data_dir, concurrency=concurrency,
                  idle_recycle_min=idle, account_strategy=strategy,
                  account_fixed_id=fixed_id,
                  ui_port=ui_port, accounts=accounts,
                  notify_desktop=bool(notify.get("desktop", True)),
                  notify_webhook=webhook_on, webhook_url=webhook_url,
                  http_pool_enabled=http_pool_enabled)


# -------------------------------------------------------------------- 写默认

DEFAULT_TOML = """\
schema_version = 1
data_dir = "."
concurrency = 2
idle_recycle_min = 10
# 多账号时用哪个：
# balance（用得少的优先）/ roundrobin（轮流）/ fixed（固定最近用过的）
# tier（按订阅档位：Pro > Plus > unknown > free；连续失败的号一律先靠边）
account_strategy = "balance"
# 选了 fixed 才会用到：固定用哪个账号（写 id，不是名字；改名不影响）
account_fixed_id = ""

[ui]
# 0 = 每次启动用动态端口，并把实际端口写进 state/runtime.json 供 CLI 使用
port = 0

[notify]
desktop = true
webhook_enabled = false
"""


def init(path: str | Path | None = None, *, force: bool = False) -> tuple[Path, bool]:
    """初始化配置与目录。返回 (配置路径, 是否新建)。已存在且没 force 就不覆盖。"""
    p = find_config(path)
    created = False
    if p.exists() and not force:
        cfg = load(p)
        for d in cfg.dirs():
            d.mkdir(parents=True, exist_ok=True)
        return p, created
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_TOML, encoding="utf-8")
    created = True
    cfg = load(p)
    for d in cfg.dirs():
        d.mkdir(parents=True, exist_ok=True)
    return p, created


# -------------------------------------------------------------------- 小工具

def _resolve_dir(value, base: Path, label: str) -> Path:
    s = str(value or "").strip() or "."
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if not p.is_absolute():
        p = base / p
    try:
        return p.resolve()
    except OSError as e:
        raise ConfigError("%s 路径解析失败：%s（%s）" % (label, p, e)) from e


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_daily_profile(prof: Path, aid: str) -> None:
    low = str(prof).replace("\\", "/").lower()
    for hint in _DAILY_PROFILE_HINTS:
        if hint.replace("\\", "/") in low:
            raise ConfigError(
                "账号 %s 的 profile 指向了你日常浏览器的数据目录（%s）。\n"
                "半瓢 只用专用目录，避免碰到你平时的登录态。" % (aid, prof))


def _int_in(value, low: int, high: int, label: str) -> int:
    # 不能用 int() 硬转：19445.7 会被静默截成 19445，concurrency=true 会变 1，
    # 用户改配置改错了却看不到任何报错，是最难查的一类问题。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s 必须是整数（不带引号、不带小数），收到 %r。"
                          % (label, value))
    n = value
    if not (low <= n <= high):
        raise ConfigError("%s=%d 超出范围（%d–%d）。" % (label, n, low, high))
    return n


# -------------------------------------------------------------------- 追加账号

def next_account_id(cfg: "Config") -> str:
    used = {a.id for a in cfg.accounts}
    i = 1
    while ("acc%d" % i) in used:
        i += 1
    return "acc%d" % i


def pick_port(cfg: "Config") -> int:
    """挑一个没被账号占用、且当前没人监听的端口。"""
    from . import platform as plat

    used = {a.port for a in cfg.accounts}
    port = 19445
    for _ in range(200):
        if port not in used and not plat.port_open(port):
            return port
        port += 1
    raise ConfigError("连续 200 个端口都不可用，请手动指定 --port。")


def append_account(path: Path, name: str, browser: str, port: int | None = None) -> Account:
    """往 config.toml 追加一个账号块，然后重新加载校验。

    整段"读配置 → 算下一个 id/端口 → 写回"必须在**同一把锁**里：
    只锁写盘那一小段是不够的——两个进程会算出同一个 id 和同一个端口，
    一个写成功、另一个校验失败回滚，用户看到"加了两个只剩一个"。
    """
    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        cfg = load(path)
        aid = next_account_id(cfg)
        if any(a.name == name for a in cfg.accounts):
            raise ConfigError("已经有一个叫「%s」的账号了，换个名字（重名会让命令分不清操作谁）。" % name)
        port = port or pick_port(cfg)
        block = (
            "\n[[accounts]]\n"
            'id = "%s"\n'
            'name = "%s"\n'
            'browser = "%s"\n'
            'profile = "profiles/%s"\n'
            "port = %d\n"
        ) % (aid, name.replace('"', "'"), browser, aid, port)

        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(text + block, encoding="utf-8")
        try:
            cfg2 = load(tmp)          # 先在临时文件上校验
        except ConfigError:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)         # 校验过了才替换，不会留半截配置
    return next(a for a in cfg2.accounts if a.id == aid)


# -------------------------------------------------------------------- 删 / 改账号

def _account_blocks(text: str) -> list[tuple[int, int, str]]:
    """把 config.toml 按 [[accounts]] 切块，返回 (起点, 终点, 块文本)。

    配置是手写的 TOML，没有可靠的"删一个表"的 API，所以按块处理文本。
    每块从 `[[accounts]]` 那一行起，到下一个 `[[accounts]]` 或文件尾。
    """
    import re

    marks = [m.start() for m in re.finditer(r"(?m)^\[\[accounts\]\][ \t]*\r?$", text)]
    out = []
    for i, st in enumerate(marks):
        en = marks[i + 1] if i + 1 < len(marks) else len(text)
        out.append((st, en, text[st:en]))
    return out


def _write_accounts(path: Path, blocks: list[str], head: str) -> None:
    """把"头部 + 账号块"写回配置，先在临时文件上校验，再替换。"""
    new = head + "".join(blocks)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(new, encoding="utf-8")
    try:
        load(tmp)                     # 校验：剩下的配置必须仍然合法
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)             # 不会留半截配置


def _profile_mtime(acc) -> float:
    try:
        return Path(acc.profile).stat().st_mtime
    except OSError:
        return 0.0


def choose_by_strategy(cfg, candidates, today_of=None, pending_of=None,
                       plan_of=None, fail_of=None):
    """在候选账号里按 account_strategy 挑一个。

    调用方负责先把"现在不能用"的（冷却中 / 掉登录）过滤掉——策略只管"在能用的里面挑谁"。

    today_of(acc) -> 该账号今天出图数；不给就退回按 profile 时间挑。
    pending_of(acc) -> 该账号还有几单没跑完（队列 + 在跑）。
    plan_of(acc) -> 订阅档位（free / Plus / Pro / 其它），给 tier 策略用。
    fail_of(acc) -> 连续失败次数，给"坏号先靠边"用。
    后两个都是可选回调，**不传时行为与旧版完全一致**（老调用方 / 既有单测不受影响）。
    **排队数必须排在第一优先**：连续投四单、每单都只看"今天出了几张"的话，
    四单会全挑中同一个号（今天都还没结算），面板上看起来像"只用一个号"；
    实际调度时才换号，用户看到的是"根本不并行"（2026-11-05 实机复现）。
    返回 None 表示没得挑。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    strat = (getattr(cfg, "account_strategy", "") or "balance").strip().lower()
    want = (getattr(cfg, "account_fixed_id", "") or "").strip()

    if strat == "fixed" and want:
        for a in candidates:
            if want in (a.id, a.name):
                return a
        # ★ 固定那个不在候选里了：可能被删、被改名、也可能它正休息。
        #   绝不能因此不让出图，往下走按用得少的挑。
    if strat == "roundrobin":
        return min(candidates, key=_profile_mtime)

    def _streak(a):
        try:
            return int(fail_of(a) or 0) if fail_of is not None else 0
        except Exception:
            return 0

    def _tier(a):
        """档位排序：Pro > Plus > unknown/其它 > free。

        unknown（没探到）排在中间：不能把"不知道"当成免费号压到最后。
        用宽松包含匹配，planType 具体字符串可能带前缀/大小写。
        """
        try:
            raw = plan_of(a) if plan_of is not None else ""
        except Exception:
            raw = ""
        p = str(raw or "").lower()
        if "pro" in p:
            return 0
        if "plus" in p:
            return 1
        if "free" in p:
            return 3
        return 2

    def _base(a):
        load = int(pending_of(a) or 0) if pending_of is not None else 0
        today = int(today_of(a) or 0) if today_of is not None else 0
        return (load, today, a.id)

    if strat == "tier":
        try:
            # 连续失败排在档位之前：再高级的号一直坏，也该先让给别人
            return min(candidates, key=lambda a: (_streak(a), _tier(a)) + _base(a))
        except Exception:
            pass
        return max(candidates, key=_profile_mtime)

    if today_of is not None or pending_of is not None or fail_of is not None:
        try:
            # 连续失败降权对所有策略生效：坏号先靠边，别的号轮着上
            return min(candidates, key=lambda a: (_streak(a),) + _base(a))
        except Exception:
            pass
    return max(candidates, key=_profile_mtime)


def set_scalar(path: Path, key: str, value, *, as_int: bool = False) -> None:
    """改配置里的**一个顶层标量**（只动那一行，别的一字不改）。

    写之前先在临时文件上 load 一遍：改坏了就不落盘。
    面板改策略走这里，不用它整份重写配置。
    as_int=True 用于 concurrency 这类整数项：写成裸数字、别加引号，
    否则 load() 的 _int_in 会把 "3" 当字符串直接拒掉。
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", key or ""):
        raise ConfigError("配置项名字不合法：%r" % key)

    if as_int:
        try:
            line = "%s = %d" % (key, int(value))
        except (TypeError, ValueError) as e:
            raise ConfigError("%s 必须是整数，收到 %r。" % (key, value)) from e
    else:
        line = '%s = "%s"' % (key, str(value).replace('"', ""))

    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        text = path.read_text(encoding="utf-8")
        # 先把已有的同名行**全部摘掉**：老版本把它插进过 [notify] 表里，
        # 那种位置改了也不生效（等于 notify.account_strategy）。摘干净再重插。
        body = "".join(
            ln for ln in text.splitlines(keepends=True)
            if not re.match(r"[ \t]*%s[ \t]*=" % re.escape(key), ln)
        )
        # 插在**第一个表头之前**。TOML 里表头之后的裸键属于那张表，
        # 以前插在 [[accounts]] 前面，结果写出来是 notify.account_strategy，
        # 面板里选了策略，重启又变回 balance（2026-11-04 在真机配置里看到这个坑）。
        m = re.search(r"(?m)^\[", body)
        i = m.start() if m else len(body)
        text = body[:i] + line + "\n\n" + body[i:]

        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            load(tmp)
        except ConfigError:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)


def set_http_pool_enabled(path: Path, on: bool) -> None:
    """开关 [http_pool] enabled。导入功能必须打开它才生效（面板设置里那个选项）。"""
    val = "true" if on else "false"
    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        out, i, found = [], 0, False
        while i < len(lines):
            ln = lines[i]
            if re.match(r"^\[http_pool\][ \t]*\r?\n?$", ln):
                found = True
                out.append(ln)
                j, replaced = i + 1, False
                while j < len(lines) and not re.match(r"^\[", lines[j]):
                    if re.match(r"^[ \t]*enabled[ \t]*=", lines[j]):
                        out.append("enabled = %s\n" % val)
                        replaced = True
                        j += 1
                        continue
                    out.append(lines[j]); j += 1
                if not replaced:
                    out.append("enabled = %s\n" % val)
                i = j
                continue
            out.append(ln); i += 1
        if not found:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append("\n[http_pool]\nenabled = %s\n" % val)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        try:
            load(tmp)
        except ConfigError:
            tmp.unlink(missing_ok=True); raise
        os.replace(tmp, path)


def set_account_port(path: Path, aid: str, port: int) -> None:
    """改某个账号的调试端口（只动那一行，别的一字不改）。

    为的是什么：端口被别的程序占着时（用户自己开的 Chrome 也加了
    --remote-debugging-port，或者上次没退干净的浏览器还挂着），
    2026-11-06 之前只报错「不是半瓢的实例」，用户点「打开浏览器」毫无反应，
    也不知道该去关哪个程序。现在驱动层自动换一个空端口，顺手写回配置，
    下次启动用的就是新端口，不会再撞同一堵墙。
    """
    import re

    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        text = path.read_text(encoding="utf-8")
        blocks = _account_blocks(text)
        head = text[: blocks[0][0]] if blocks else text
        hit = False
        out: list[str] = []
        for _st, _en, blk in blocks:
            if re.search(r'(?m)^[ \t]*id[ \t]*=[ \t]*"%s"' % re.escape(aid), blk):
                new, n = re.subn(r"(?m)^([ \t]*port[ \t]*=[ \t]*)\d+",
                                 lambda m: "%s%d" % (m.group(1), int(port)), blk, count=1)
                if n == 0:
                    new = blk.rstrip("\n") + "\nport = %d\n" % int(port)
                blk, hit = new, True
            out.append(blk)
        if not hit:
            raise ConfigError("配置里没有账号 %s" % aid)
        _write_accounts(path, out, head)


def remove_account(path: Path, aid: str) -> None:
    """从配置里删掉一个账号。**不删 profile 目录**——里面是登录态和出过的图，
    删配置只是"不再用它跑"，后悔了我们还能加回来。
    """
    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        cfg = load(path)
        if not any(a.id == aid for a in cfg.accounts):
            raise ConfigError("没有这个账号：%s" % aid)
        text = path.read_text(encoding="utf-8")
        blocks = _account_blocks(text)
        head = text[:blocks[0][0]] if blocks else text
        kept, removed = [], False
        for st, en, blk in blocks:
            if ('id = "%s"' % aid) in blk:
                removed = True
                continue
            kept.append(blk)
        if not removed:
            raise ConfigError("配置里找不到账号 %s" % aid)
        # 删掉的正好是「固定用」的那个 → 把这个引用一起清掉，策略退回 balance。
        # 否则配置里会留一个指向不存在账号的 id：出图虽然会降级不崩，
        # 但用户回头改配置会看到一条指向空气的设置，说不清是怎么来的。
        if (cfg.account_fixed_id or "").strip() == aid:
            head = re.sub(r"(?m)^[ 	]*account_fixed_id[ 	]*=.*$",
                          'account_fixed_id = ""', head)
            head = re.sub(r"(?m)^[ 	]*account_strategy[ 	]*=.*$",
                          'account_strategy = "balance"', head)
        _write_accounts(path, kept, head)


def rename_account(path: Path, aid: str, new_name: str) -> None:
    """改账号的显示名（不改 id、不动 profile、不动登录态）。"""
    import re

    new_name = (new_name or "").strip()
    if not new_name:
        raise ConfigError("名字不能为空")
    with locks.exclusive(path.with_suffix(".lock"), timeout=15,
                         what="配置文件（另一个半瓢正在改它）"):
        cfg = load(path)
        if not any(a.id == aid for a in cfg.accounts):
            raise ConfigError("没有这个账号：%s" % aid)
        if any(a.name == new_name and a.id != aid for a in cfg.accounts):
            raise ConfigError("已经有一个叫「%s」的账号了，换个名字。" % new_name)

        text = path.read_text(encoding="utf-8")
        blocks = _account_blocks(text)
        head = text[:blocks[0][0]] if blocks else text
        out, hit = [], False
        for st, en, blk in blocks:
            if ('id = "%s"' % aid) in blk:
                blk = re.sub(r'(?m)^name[ \t]*=[ \t]*"[^"]*"',
                             'name = "%s"' % new_name.replace('"', "'"), blk, count=1)
                hit = True
            out.append(blk)
        if not hit:
            raise ConfigError("配置里找不到账号 %s" % aid)
        _write_accounts(path, out, head)
