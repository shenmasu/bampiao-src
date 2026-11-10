"""任务与账号的数据模型。

两条硬规矩，都是从旧系统踩出来的：

1. **错误码是枚举，不是自由文本**。上层（CLI / UI / agent）靠它决定"去登录"还是
   "等冷却"还是"重投"，自由文本没法判断。
2. **一次任务的状态只有少数几个值**。状态机每多一个状态，分支就多一倍；
   "不确定"用 ``uncertain`` 布尔表示，不另开状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

# ---- 错误码（写进 JSON 合约，改名等于破坏兼容）
AUTH_REQUIRED = "AUTH_REQUIRED"        # 掉登录，要人
QUOTA_COOLDOWN = "QUOTA_COOLDOWN"      # 撞生成上限，等重置
RENDER_FAILED = "RENDER_FAILED"        # 出图失败（网页改版 / 质检不过 / 超时）
NO_LANE = "NO_LANE"                    # 没有可用账号（全在冷却 / 全掉登录）
TIMEOUT_UNCERTAIN = "TIMEOUT_UNCERTAIN"  # 发出去之后断了，远端状态未知
CANCELLED = "CANCELLED"                # 人为取消
BUSY = "BUSY"                          # 同一账号正忙
NO_REF_FILE = "NO_REF_FILE"            # 参考图路径不存在（派单方给了不存在的文件）
BROWSER_LOST = "BROWSER_LOST"          # 等图时浏览器/页面断了
# 文本任务（ask）
ASK_EMPTY = "ASK_EMPTY"                # 网页里没读到这次的回复
ASK_TIMEOUT = "ASK_TIMEOUT"            # 等到超时，回复可能没写完
ASK_SEND = "ASK_SEND"                  # 消息没发出去
ASK_FAILED = "ASK_FAILED"              # 其它文本失败

# 为什么要把这两个从 RENDER_FAILED 拆出来（2026-10-31 查昨晚失败得出）：
# 8 个失败项里有 7 个都记成 RENDER_FAILED，但真实原因是两种完全不同的东西：
#   · R111 的 4 项：参考图文件 04:45:57 才落盘，我们在 04:43 就引用了（派单方时序）
#   · R96/S07、R99/X10：wait_for_timeout 抛 ChannelClosed，浏览器在等图时被关了
# 混成"渲染失败"会让用户和 AI 都去改提示词、查额度，白折腾。
ERROR_CODES = (AUTH_REQUIRED, QUOTA_COOLDOWN, RENDER_FAILED, NO_LANE,
               TIMEOUT_UNCERTAIN, CANCELLED, BUSY, NO_REF_FILE, BROWSER_LOST,
               ASK_EMPTY, ASK_TIMEOUT, ASK_SEND, ASK_FAILED)

# 驱动的 kind → 对外错误码
_KIND_TO_CODE = {
    "auth": AUTH_REQUIRED,
    "quota": QUOTA_COOLDOWN,
    "render": RENDER_FAILED,
    "busy": BUSY,
    "timeout": TIMEOUT_UNCERTAIN,
    "no_lane": NO_LANE,
    "no-ref": NO_REF_FILE,
    "browser-lost": BROWSER_LOST,
}

# ---- 任务状态
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_PARTIAL = "partial"     # 有产物也有失败项
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_STATES = (JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_PARTIAL, JOB_FAILED, JOB_CANCELLED)

# ---- 单条任务状态
ITEM_QUEUED = "queued"
ITEM_OK = "ok"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"    # 撞上限后本批剩余不再尝试
ITEM_CANCELLED = "cancelled"  # 用户手动「停下这张」


def to_code(kind: str | None) -> str:
    """驱动的失败类别 → 对外错误码。未知一律当出图失败。"""
    return _KIND_TO_CODE.get((kind or "").strip().lower(), RENDER_FAILED)


@dataclass
class Item:
    id: str
    prompt: str
    aspect: str = "1:1"
    alpha: bool = False
    anim: bool = False       # 这一条要动图（GIF）；只影响提示词和质检，交付类型看文件本身
    refs: list[str] = field(default_factory=list)
    status: str = ITEM_QUEUED
    files: list[str] = field(default_factory=list)   # 相对 job 目录的文件名
    error_code: str | None = None
    note: str = ""          # 技术尾巴（日志最后几百字），排查用
    reason: str = ""        # 人话原因（一句话），面板和 agent 看这个
    log_path: str = ""      # 这一条对应的完整驱动日志（排查时直接打开）
    failure_class: str = "" # quota/auth/verify/network/page/file/unsure/other（失败当场落盘）
    resolved_by: str = ""   # "found" = 用户确认"网页上有图"，不计入没画成、也不许重投

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(
            id=str(d.get("id") or ""),
            prompt=str(d.get("prompt") or ""),
            aspect=str(d.get("aspect") or "1:1"),
            alpha=bool(d.get("alpha")),
            anim=bool(d.get("anim")),
            refs=[str(x) for x in (d.get("refs") or [])],
            status=str(d.get("status") or ITEM_QUEUED),
            files=[str(x) for x in (d.get("files") or [])],
            error_code=d.get("error_code"),
            note=str(d.get("note") or ""),
            reason=str(d.get("reason") or ""),
            log_path=str(d.get("log_path") or ""),
            failure_class=str(d.get("failure_class") or ""),
            resolved_by=str(d.get("resolved_by") or ""),
        )


@dataclass
class Job:
    id: int
    request_id: str                  # 幂等键：同一个 request_id 只会有一个任务
    account_id: str
    # 实际执行的账号。account_id 是"这单本来想给谁"（调度器换号时不改它，
    # 原账号恢复后重试仍优先它）；ran_on 是"最后真的在谁那儿跑的"。
    # 没有它，三个号并行跑的四单在面板上全显示 acc1——用户以为软件没并行
    # （2026-11-05 实机复现：R258/259/260 分别跑在 acc1/acc2/acc3，台账全写 acc1）。
    ran_on: str = ""
    brief: str = ""
    tag: str = ""                    # 可选分类，取代旧系统的"项目"
    submitter: str = "cli"           # 谁投的：cli / ui / agent:<name>
    status: str = JOB_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    items: list[Item] = field(default_factory=list)
    uncertain: bool = False          # 发出去之后断了，远端可能已经出图
    attempts: int = 0
    error_code: str | None = None
    note: str = ""
    reason: str = ""        # 整单失败的人话原因
    # ---- 0.2.25：失败处理状态机
    # handled_at 是唯一依据：有值 = 这单已经处理过（重投/标记/反馈/收下），不再计入待处理。
    # 单个 dict 存一次性动作结果（重启浏览器/登录/打开浏览器），渲染时 30 分钟过期。
    handled_at: float | None = None
    handled_by: str = ""            # retry / dismiss / feedback / found / legacy
    retry_of: int | None = None     # 重投出来的新单指回源单
    auto_hops: int = 0              # 自动换号重试了几次（2026-11-04：一个号被 CF 挡住就换下一个）
    last_action: dict | None = None # {do, ok, at, note}
    opening: str = ""                # 驱动用的开场风格（空 = 产品默认）
    client_version: str = ""         # 跑这单的客户端版本（排查用：一眼看出是哪版出的问题）
    # 2026-11-05：半瓢不再只出图，`ask` 也会建单。面板要能分开看，所以记一下类型。
    # image / ask —— 老数据没有这个键，读出来当 image。
    kind: str = "image"
    # 文本单等到超时才读到回复：内容可能没写完。必须是独立字段，
    # 不能塞进 note —— note 会被截断，也会被当成回复正文（2026-11-06 复评）。
    partial: bool = False
    # 质检标注（要动图却给静图 / 比例不符）。和 note 分开：
    # 失败重投后成功的单，note 里还留着上一次的失败原因，不能再当"标注"展示。
    quality_note: str = ""

    # ---- 派生量
    @property
    def display_brief(self) -> str:
        """给人看的说明：重投单的 "补 R211（…）" 前缀要剥掉，否则托盘里又冒出 R 号。"""
        import re

        b = re.sub(r"^补 R\d+（.*?）\s*", "", self.brief or "")
        return b.strip()

    @property
    def expected(self) -> int:
        return len(self.items)

    @property
    def rendered(self) -> int:
        return sum(1 for it in self.items if it.status == ITEM_OK)

    @property
    def failed_items(self) -> list[Item]:
        return [it for it in self.items if it.status == ITEM_FAILED]

    def derive_status(self) -> str:
        """按当前 items 推导任务状态。

        注意：**不能拿"提交了几条"当成功数**（旧系统把 len(batch) 累加进成功数，
        统计一直偏高）。这里只数真正落了文件的条目。
        """
        if self.status == JOB_CANCELLED:
            return JOB_CANCELLED
        todo = [it for it in self.items if it.status == ITEM_QUEUED]
        if todo:
            return JOB_RUNNING if self.started_at else JOB_QUEUED
        ok = self.rendered
        bad = len(self.failed_items)
        if ok and bad:
            return JOB_PARTIAL
        if ok:
            return JOB_DONE
        return JOB_FAILED

    def as_dict(self) -> dict:
        d = asdict(self)
        d["rendered"] = self.rendered
        d["expected"] = self.expected
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            id=int(d["id"]),
            request_id=str(d.get("request_id") or ""),
            account_id=str(d.get("account_id") or ""),
            ran_on=str(d.get("ran_on") or ""),
            brief=str(d.get("brief") or ""),
            tag=str(d.get("tag") or ""),
            submitter=str(d.get("submitter") or "cli"),
            status=str(d.get("status") or JOB_QUEUED),
            created_at=float(d.get("created_at") or 0),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            items=[Item.from_dict(x) for x in (d.get("items") or [])],
            uncertain=bool(d.get("uncertain")),
            attempts=int(d.get("attempts") or 0),
            error_code=d.get("error_code"),
            note=str(d.get("note") or ""),
            reason=str(d.get("reason") or ""),
            handled_at=d.get("handled_at"),
            handled_by=str(d.get("handled_by") or ""),
            retry_of=d.get("retry_of"),
            auto_hops=int(d.get("auto_hops") or 0),
            last_action=d.get("last_action") or None,
            opening=str(d.get("opening") or ""),
            client_version=str(d.get("client_version") or ""),
            kind=str(d.get("kind") or "image"),
            partial=bool(d.get("partial")),
            quality_note=str(d.get("quality_note") or ""),
        )


@dataclass
class AccountState:
    """账号的运行态。**不是配置**——配置在 config.toml，这里只放"跑起来才知道的事"。"""

    account_id: str
    login: str = "unknown"           # ok / required / unknown
    login_checked_at: float | None = None
    login_user: str = ""             # 会话里拿到的短 id，用于发现"换了号"
    cooldown_until: float | None = None
    # 撞上限的记录：每次 {at, after_n, reset_at}。样本够了就能估"今天还能出几张"
    quota_hits: list[dict] = field(default_factory=list)
    last_used_at: float | None = None
    stat_date: str = ""              # YYYY-MM-DD
    stat_today: int = 0

    def cooling(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.cooldown_until and self.cooldown_until > now)

    def bump_today(self, day: str, n: int = 1) -> None:
        """跨日重置；旧系统按累计算，UI 上"今天出了多少"一直不准。"""
        if self.stat_date != day:
            self.stat_date = day
            self.stat_today = 0
        self.stat_today += n

    def images_today(self, day: str) -> int:
        """今天出了几张。**过期就返回 0**。

        2026-11-02 用户反馈：账号页的「今天出图」过了几天还挂着旧数字。
        原因是 stat_today 只在"下次出图"时才跨日归零——这天没出图，界面上就一直
        显示上一个出图日的累计值。读的时候也判一次日期，显示才对。
        """
        return self.stat_today if self.stat_date == day else 0

    def quota_hits_on(self, day: str) -> int:
        """某一天撞了几次上限。quota_hits 里存的是时间戳，按本地日期数。"""
        import time as _t

        n_hits = 0
        for h in self.quota_hits or []:
            try:
                if _t.strftime("%Y-%m-%d", _t.localtime(float(h.get("at") or 0))) == day:
                    n_hits += 1
            except Exception:
                continue
        return n_hits

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        return cls(
            account_id=str(d.get("account_id") or ""),
            login=str(d.get("login") or "unknown"),
            login_checked_at=d.get("login_checked_at"),
            login_user=str(d.get("login_user") or ""),
            cooldown_until=d.get("cooldown_until"),
            quota_hits=[x for x in (d.get("quota_hits") or []) if isinstance(x, dict)],
            last_used_at=d.get("last_used_at"),
            stat_date=str(d.get("stat_date") or ""),
            stat_today=int(d.get("stat_today") or 0),
        )
