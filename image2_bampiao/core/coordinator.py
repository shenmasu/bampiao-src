"""调度器：唯一决定"现在跑哪一单、用哪个账号、失败怎么办"的地方。

为什么只允许一个写者：
- 两个调度器同时跑同一个账号，会共用同一个 CDP 页面，互相 goto、互相发消息，
  结果是串图 + 重复烧额度。账号级锁只能兜底，正确的做法是根本不产生这种竞争。

几条刻在旧系统教训里的规则：
1. **撞生成上限就整批收手**，剩下的直接标 skipped——继续硬试只是白烧时间和次数。
2. **掉登录立刻停用这个账号**，不要把剩下的条目也试一遍（每条都会失败并留痕迹）。
3. **超时不算失败**，算"不确定"（``uncertain``）：远端可能已经出图了，
   自动重投会重复出图，必须让人决定。
4. **冷却中的账号不参与调度**，其余账号照常——一个号撞墙不能拖住整个队列。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import config as cfg_mod
from . import models as M
from .store import Store

# 撞一次上限默认歇 5 小时（网页版的重置窗口通常是几小时，宁可保守）
DEFAULT_QUOTA_COOLDOWN = 5 * 3600.0


@dataclass
class Outcome:
    """一条任务的结果。runner 只要返回这个形状，coordinator 不关心它内部怎么实现。"""
    ok: bool
    kind: str = ""            # ok / auth / quota / render / busy
    files: list[str] | None = None
    note: str = ""            # 技术尾巴（日志），排查用
    reason: str = ""          # 人话原因，直接给用户看
    log: str = ""             # 完整驱动日志的路径


Runner = Callable[[cfg_mod.Config, cfg_mod.Account, M.Item, Path], Outcome]


def _http_runner(cfg, acc, item, dest) -> Outcome:
    """HTTP 号池出图（旁路）。只在 acc.kind == 'http' 时被调用。"""
    from ..httpgen import integration as hg

    r = hg.generate(cfg, acc, item.prompt, dest,
                    refs=item.refs, aspect=item.aspect, alpha=item.alpha,
                    anim=bool(item.anim),
                    svg=bool(getattr(item, "svg", False)),
                    svg_anim=bool(getattr(item, "svg_anim", False)))
    if r.get("ok"):
        name = Path(str(r.get("path") or dest)).name
        return Outcome(True, "ok", files=[name],
                       note=str(r.get("note") or ""), reason=str(r.get("note") or ""))
    return Outcome(False, str(r.get("kind") or "render"),
                   note=str(r.get("tail") or "")[-400:],
                   reason=str(r.get("reason") or r.get("tail") or "出图失败"),
                   log=str(r.get("log") or ""))


def _default_runner(cfg, acc, item, dest) -> Outcome:
    if getattr(acc, "kind", "browser") == "http":
        return _http_runner(cfg, acc, item, dest)
    from ..driver import session as sess

    # 图存在 jobs/<单号>/ 下：单号从路径取，好让会话标题写成【半瓢·R12·S01·时间】
    jid = dest.parent.name.lstrip("0")
    title = ("R%s·%s" % (jid, item.id)) if jid.isdigit() else item.id
    r = sess.generate(cfg, acc, item.prompt, dest,
                      refs=item.refs, aspect=item.aspect, alpha=item.alpha, title=title,
                      anim=bool(item.anim),
                      # SVG 旁路：默认 False，老单子走的路径一字不变
                      svg=bool(getattr(item, "svg", False)),
                      svg_anim=bool(getattr(item, "svg_anim", False)))
    if r.get("ok"):
        # 交付文件可能不是 .png（GIF）：以实际落盘的名字为准。
        # 以前固定 dest.name，批量出 GIF 时记录会指向不存在的 S01.png（2026-11-06 评审）。
        name = Path(str(r.get("path") or dest)).name
        return Outcome(True, "ok", files=[name],
                       note=str(r.get("note") or ""), reason=str(r.get("note") or ""))
    # 取**尾部**：tail 本身是日志最后 1200 字，再截前 400 字的话，
    # 台账里看到的永远是「连接 CDP…打开首页…」这种开头，真正的报错在末尾被切掉。
    # 2026-10-30 R96/S07 就是这样：note 停在「[21:06:22]」，看着像日志断了，
    # 其实后半截才是 Playwright 的崩溃栈。
    _tail = str(r.get("tail") or "")
    from .. import why as _why

    return Outcome(False, str(r.get("kind") or "render"), note=_tail[-400:],
                   reason=_why.explain(_tail, r.get("kind") or ""),
                   log=str(r.get("log") or ""))


class Coordinator:
    def __init__(self, cfg: cfg_mod.Config, store: Store | None = None, *,
                 runner: Runner | None = None,
                 concurrency: int | None = None,
                 quota_cooldown: float = DEFAULT_QUOTA_COOLDOWN,
                 open_login: bool = True):
        self.cfg = cfg
        self.store = store or Store(cfg)
        self.runner = runner or _default_runner
        self.concurrency = max(1, int(concurrency or cfg.concurrency))
        self.quota_cooldown = quota_cooldown
        self.open_login = open_login       # 掉登录时要不要弹真浏览器（测试关掉）
        self.fail_backoff = 2.0            # 调度一轮失败后等多久再试（测试会调小）
        self._busy: set[int] = set()          # 本进程正在跑的任务 id
        # 任务 id → 账号 id。以前只记"哪些单在跑"，不记"跑在哪个号上"，
        # 于是两个 worker 可能同时挑到同一个号：真机上第二个会拿到 gen 锁的 busy，
        # 白跑一轮再回队列（2026-11-03 实测：两单都钉 acc1 时 peak=2 全压在 acc1）。
        self._busy_acc: dict[int, str] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------ 账号选择
    def _strategy_callbacks(self):
        """给 config.choose_by_strategy 的四个只读回调，统一从 store 取运行态。"""
        try:
            load = self.store.active_load()
        except Exception:
            load = {}
        return (
            lambda a: self.store.account(a.id).today_count(),
            lambda a: load.get(a.id, 0),
            lambda a: self.store.account(a.id).plan,
            lambda a: self.store.account(a.id).fail_streak_now(),
        )

    def _all_accounts(self) -> list[cfg_mod.Account]:
        """浏览器号 + HTTP 号（开关关着时后者为空）。HTTP 号只在选号/出图这一层参与。"""
        accs = list(self.cfg.accounts)
        try:
            from ..httpgen import integration as hg
            accs.extend(hg.http_accounts(self.cfg))
        except Exception:
            pass
        return accs

    def usable_accounts(self) -> list[cfg_mod.Account]:
        """现在能用的账号：没在冷却、没被判掉登录、没被"需要人过验证"挂起。

        最后一条是 2026-11-02 加的：ChatGPT 弹 Cloudflare 挑战时，那个号的会话
        根本发不出消息，后面每一单都去撞一次只会让人以为"软件坏了"。
        """
        from ..driver import session_health as sh

        out = []
        for acc in self._all_accounts():
            st = self.store.account(acc.id)
            if not getattr(st, "enabled", True):
                continue
            if st.cooling():
                continue
            if st.login == "required":
                continue
            if sh.read_pause(str(self.cfg.data_dir), acc.id):
                continue
            out.append(acc)
        return out

    def _pick(self, job: M.Job) -> cfg_mod.Account | None:
        usable = self.usable_accounts()
        if not usable:
            return None

        # 先去掉"本进程里正被别的单占着"的号。注意这一步必须在"优先用原定账号"
        # 之前——否则两单钉同一个号时，后一单还是会拿到那个忙号。
        with self._guard:
            taken = {aid for jid, aid in self._busy_acc.items() if jid != job.id}
        free = [a for a in usable if a.id not in taken]
        if not free:
            return None          # 号都在忙：等下一轮，别硬塞

        for acc in free:
            if acc.id == job.account_id:
                return acc

        # 指定的号不可用（掉登录 / 撞额度在休息）→ 换一个能用的，别让单干等着。
        # 以前这里 `return None`，理由是"换号会打乱账号分工"。可现实是：
        # 一个号撞了额度、另一个闲着，单就一直卡在 queued 不动，
        # 用户看到的是"软件坏了"（2026-10-31 R121 卡了半小时，acc2 全程空转）。
        # 空转比换号更伤，所以改成换号。job.account_id 保持不动——
        # "这单本来想给谁"的语义还在，等原账号恢复后重试仍优先它。

        # 但换号不能换到"另一个 worker 正在用的号"上：那样两个 worker 会共用一个
        # CDP 页面，后到的那个只能拿到 busy 再弹回队列，等于白转一圈。
        # 上面已经把这些号排掉了，这里只需说明为什么换了。
        # 不能写死 free[0]：那等于永远挑配置里的第一个号，一个号反复失败时
        # 后面每一单（含换号）还是会压回它身上（2026-11-11 用户反馈）。
        # 统一走 config.choose_by_strategy：连续失败降权 + 用户选的策略都生效。
        from .. import config as cfg_mod
        t, p, plan, fail = self._strategy_callbacks()
        alt = cfg_mod.choose_by_strategy(self.cfg, free, t, p, plan, fail) or free[0]
        if job.account_id and alt.id != job.account_id:
            why = "正在跑别的单" if job.account_id in taken else "正在休息"
            try:
                print("R%d 原定账号「%s」%s，改由「%s」执行"
                      % (job.id, job.account_id, why, alt.id))
            except Exception:
                pass
        return alt

    def _free_slot(self, job_id: int) -> None:
        """释放名额 + 账号占位。两件事必须一起做——只清一个，号会永远"被占着"，
        后面所有单都排不上（测试里手动 discard 就踩过）。"""
        with self._guard:
            self._busy.discard(job_id)
            self._busy_acc.pop(job_id, None)

    # ------------------------------------------------------------ 领取
    def _next_claim(self, skip: set[int] | None = None) -> tuple[M.Job, cfg_mod.Account] | None:
        """挑一单能跑的领掉。没有可跑的返回 None。

        名额满了、**队列被暂停**、账号在冷却/掉登录、或者这一单的账号不可用，都跳过。
        """
        # 每次领单前顺手看一眼：有没有"因为掉登录而失败、现在账号又好了"的单要补投。
        self._auto_requeue_after_login()

        # 暂停是硬闸门：面板上那个按钮必须真的管用（以前它只写文件、没人读）。
        if self.store.paused():
            return None
        with self._guard:
            if len(self._busy) >= self.concurrency:
                return None
        skip = skip or set()
        for job in self.store.pending(limit=20):
            if job.id in skip:
                continue
            with self._guard:
                if job.id in self._busy:
                    continue
            acc = self._pick(job)
            if acc is None:
                continue
            # 先占位再 claim：两个 worker 同时 _pick 到同一个号时，
            # 后到的那个在这里被挡下，改去挑下一单（不然就是白撞 busy）。
            with self._guard:
                if acc.id in self._busy_acc.values():
                    continue
                self._busy_acc[job.id] = acc.id
            claimed = self.store.claim(job.id, create_dir=False, ran_on=acc.id)
            if claimed is None:
                with self._guard:
                    self._busy_acc.pop(job.id, None)
                continue
            with self._guard:
                self._busy.add(claimed.id)
            return claimed, acc
        return None

    def _release(self, job: M.Job, res: dict) -> dict:
        """收尾统计：busy 放回队列的不算成功也不算失败。"""
        if res.get("status") == M.JOB_QUEUED:
            return {"counted": False, "ok": False}
        ok = res.get("status") in (M.JOB_DONE, M.JOB_PARTIAL)
        return {"counted": True, "ok": ok}

    # ------------------------------------------------------------ 同步跑一轮
    def tick(self, *, limit: int = 20) -> dict:
        """把当前能跑的任务按**串行**跑掉（CLI 的 ``run --once`` 用它）。

        真并发在 ``run_forever`` 里：它开 ``concurrency`` 个 worker，
        每个 worker 各自领单。这里串行的原因是——命令行一次性跑完更好读日志，
        而且账号级锁本来就让同账号不可能并行。
        """
        done = failed = skipped = 0
        touched: list[int] = []
        # 因为"账号忙"被放回队列的单，这一轮不许再领——否则会立刻重领，
        # 把 busy 变成死循环（跑完一轮才发现任务从 queued 变 done）。
        deferred: set[int] = set()
        for _ in range(max(1, limit)):
            got = self._next_claim(skip=deferred)
            if not got:
                break
            job, acc = got
            try:
                res = self._run_job(job, acc)
            finally:
                self._free_slot(job.id)
            verdict = self._release(job, res)
            if not verdict["counted"]:
                deferred.add(job.id)
                continue
            touched.append(job.id)
            if verdict["ok"]:
                done += 1
            else:
                failed += 1

        # skipped 的定义：队列里还有活儿，但它的账号现在不可用（冷却 / 掉登录）。
        # 不把"账号忙被放回队列"算进来——那是临时状态，不是卡住。
        leftover = self.store.pending(limit=limit)
        skipped = sum(1 for j in leftover if self._pick(j) is None)
        return {"done": done, "failed": failed, "skipped": skipped,
                "jobs": touched, "busy": len(self._busy)}

    # ------------------------------------------------------------ 跑一单
    def _save_progress(self, job: M.Job) -> None:
        """每跑完一张就把进度写盘。**面板读的是磁盘，不是这块内存。**

        2026-11-07 用户反馈：跑 9 张时进度一直停在 0/9，要么等到全跑完直接跳 9/9，
        要么中途断了显示"4/9 5 失败"，从来见不到 1/9。
        根子就在这里——以前只在"换号/忙/没通道"这几个分支 save，正常出图成功
        和普通失败都是 continue 走了，磁盘上的 manifest 一直是开局那份。
        一条一写（几 KB 的 JSON，毫秒级），换来的是进度真的在动。
        """
        try:
            self.store.save(job)
        except Exception as e:            # 写盘失败绝不能把这一单带崩
            print("R%d 进度没写下去（不影响出图）：%r" % (job.id, e))

    def _run_job(self, job: M.Job, acc: cfg_mod.Account) -> dict:
        job_dir = self.store.dir_of(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_reason: str | None = None

        if not job.client_version:
            try:
                from .. import __version__ as _cv

                job.client_version = _cv
            except Exception:
                pass

        # 文本单（ask）走另一条路：它不是"画图"，是"问一句、把回复取回来"。
        # 不分支的话，`kind=ask` 的单会被当出图跑——把"只回四个字"画成一张图，
        # 白烧额度还看不出错（2026-11-06 排查时确认）。
        if (job.kind or "image") == "ask":
            return self._run_ask_job(job, acc, job_dir)

        _auto_retried: set[str] = set()   # 每条只自动重来一次，避免死循环
        for item in job.items:
            if item.status != M.ITEM_QUEUED:
                continue
            # 参考图不存在就别开浏览器。以前是在驱动里、连接 CDP + 发开场 +
            # 建会话 + 改标题之后才校验——R111 因此白建了 4 个 ChatGPT 会话。
            # 这里提前判，整批几毫秒就标完，一个会话都不建。
            # 先把 "" 之类噪声去掉，再转绝对路径（相对路径取决于当时的工作目录，
            # 校验和在驱动里打开时可能不是同一个目录）。
            # 还必须是**文件**：目录能通过 exists()，但后面上传一定失败。
            refs = [os.path.abspath(str(r)) for r in (getattr(item, "refs", None) or [])
                    if str(r).strip()]
            missing = [r for r in refs if not os.path.isfile(r)]
            if missing:
                item.status = M.ITEM_FAILED
                item.error_code = M.NO_REF_FILE
                item.failure_class = "file"
                item.note = ("参考图不存在：%s" % missing[0])[:300]
                item.reason = ("参考图文件不存在：%s（改文件路径，改提示词没用）"
                               % missing[0])[:200]
                job.reason = item.reason
                self._save_progress(job)
                continue
            if refs:
                item.refs = refs          # 统一成绝对路径往下传，驱动不用再猜 CWD
            dest = job_dir / ("%s.png" % item.id)
            # 2026-11-10 用户反馈：一次跑 9 张时，详情页只能看到数字，看不出
            # "现在正在画哪一张、哪些已经好了"。先把当前这张标成 running 落盘，
            # 面板每 2 秒读一次磁盘，就能显示"第 1 张完成预览、第 2 张出图中、其余排队"。
            item.status = M.ITEM_RUNNING
            self._save_progress(job)
            try:
                out = self.runner(self.cfg, acc, item, dest)
            except Exception as e:      # runner 自己崩了也要落一条，不能让整单悬空
                out = Outcome(False, "render", note="runner 异常：%s" % str(e)[:200])

            # 环境抽风（被 Cloudflare 挡 / 页面卡住）自动再来一次：
            # 重启这个号的浏览器，多半就好了。让用户自己点「重投」是最差的选择
            #（2026-11-03：用户连跑几单，被 CF 挡一次就得手动重来，体验像坏了一样）。
            if (not out.ok) and out.kind in ("verify", "page") and item.id not in _auto_retried:
                _auto_retried.add(item.id)
                try:
                    from ..driver import session_health as _sh

                    _sh.mark_broken(str(self.cfg.data_dir), acc.id, "auto-retry")
                except Exception:
                    pass
                print("R%d %s 遇到%s：重启这个号的浏览器后自动再来一次（不用你重投）"
                      % (job.id, item.id, "验证" if out.kind == "verify" else "页面卡住"))
                time.sleep(5)
                try:
                    out = self.runner(self.cfg, acc, item, dest)
                except Exception as e:
                    out = Outcome(False, "render", note="runner 异常：%s" % str(e)[:200])

            # 2026-11-14：「停下这张」。用户在面板点了只停当前一张 → server 把 item id
            # 记进 job.cancelled_items 并杀了驱动，这里 runner 会异常/失败返回。回读磁盘上的
            # job，若这张确实被用户主动停了，就标 cancelled、continue 下一张——
            # 不能当失败（否则会进 fail_streak / 触发自动换号重跑，正是用户看到的"换号又继续"）。
            try:
                _disk = self.store.load(job.id)
                if _disk is not None and item.id in (getattr(_disk, "cancelled_items", None) or []):
                    # 把磁盘上的取消标记同步回内存 job，避免随后 _save_progress 把它冲掉
                    job.cancelled_items = list(_disk.cancelled_items or [])
                    item.status = M.ITEM_CANCELLED
                    item.error_code = "CANCELLED"
                    item.reason = "你手动停下了这一张（其余继续）"
                    self._save_progress(job)
                    continue
            except Exception:
                pass

            if out.ok:
                item.status = M.ITEM_OK
                item.files = out.files or ([dest.name] if dest.exists() else [])
                item.error_code = None
                # 成功也可能有标注（要动图却给静图 / 比例不符），别让它只留在日志里。
                # 用独立字段：note 可能还留着上一次失败的原因，混在一起会误导。
                item.reason = str(out.reason or "")[:300]
                job.quality_note = str(out.reason or "")[:400]
                self._save_progress(job)
                continue

            # 环境类失败（Cloudflare 验证 / 页面卡住 / 发不出去）**自动换号再来**。
            # 用户有三个号，不该因为某一个号被挡就整单红着等他想起来点重投
            # （2026-11-04：用户直接问"不会自动转单啊？"）。
            # 只在"换号有意义"的失败上做；内容政策、参考图丢了、额度用完换号也没用。
            if (not out.ok) and self._worth_hop(out) and job.auto_hops < self._max_hops():
                alt = self._other_account(acc)
                if alt is not None:
                    job.auto_hops += 1
                    job.account_id = alt.id
                    item.status = M.ITEM_QUEUED
                    job.status = M.JOB_QUEUED
                    job.reason = ""
                    job.note = "在「%s」上没成（%s），自动换到「%s」重试（第 %d 次）" % (
                        acc.name, out.kind, alt.name, job.auto_hops)
                    self.store.save(job)
                    # 这一支在 finish() 之前就 return 了，必须自己记一次失败，
                    # 否则「风控验证没点」这类失败永远不进 fail_streak，
                    # 下一单（尤其外部重投）还会挑回这个号（2026-11-11 用户反馈）。
                    self._note_failure(acc)
                    print("R%d %s 在「%s」失败（%s）→ 自动换到「%s」重试"
                          % (job.id, item.id, acc.name, out.kind, alt.name))
                    return {"status": M.JOB_QUEUED}

            if out.kind == "quota":
                # 2026-11-14：撞额度不再整批放弃。多账号号池里，这个号满了别的号往往还有额度
                # ——把这张+剩余标回 queued，换到下一个有额度的号接着画，用户无感。
                # 只有"试遍了所有号 / 别的号也都不可用"才真停下。
                self._mark_quota(acc, job, item)     # 这个号进冷却，usable_accounts 会排除它
                job.auto_hops += 1
                other = [a for a in self.usable_accounts() if a.id != acc.id]
                if job.auto_hops <= self._max_hops() and other:
                    # 还有别的能用的号 → 这张和剩余都回队列，下一轮换号继续
                    item.status = M.ITEM_QUEUED
                    item.error_code = None
                    item.failure_class = ""
                    item.reason = ""
                    item.note = ""
                    job.status = M.JOB_QUEUED
                    job.reason = "「%s」额度满了，换到别的号继续画剩下的" % acc.name
                    self.store.save(job)
                    try:
                        print("R%d 「%s」撞额度，改由别的号继续（还剩 %d 张没画）"
                              % (job.id, acc.name, job.expected - job.rendered))
                    except Exception:
                        pass
                    return {"status": M.JOB_QUEUED}
                # 没别的号可用 或 换号次数用尽 → 真停，明确告诉还差几张
                item.status = M.ITEM_FAILED
                item.error_code = M.QUOTA_COOLDOWN
                item.failure_class = "quota"
                item.reason = "所有账号的生成额度都满了：等冷却，或加账号"
                job.reason = item.reason
                for rest in job.items:
                    if rest.status == M.ITEM_QUEUED:
                        rest.status = M.ITEM_SKIPPED
                        rest.error_code = M.QUOTA_COOLDOWN
                        rest.failure_class = "quota"
                        rest.note = "全部账号额度已满，剩下的没再试"
                        rest.reason = "所有账号都撞上限了，剩下的等加号/冷却后再跑"
                stop_reason = M.QUOTA_COOLDOWN
                break

            if out.kind == "auth":
                item.status = M.ITEM_FAILED
                item.error_code = M.AUTH_REQUIRED
                item.failure_class = "auth"
                item.reason = "账号掉登录了：去面板「账号」页重新登录"
                job.reason = item.reason
                self._mark_auth(acc)
                for rest in job.items:
                    if rest.status == M.ITEM_QUEUED:
                        rest.status = M.ITEM_FAILED
                        rest.error_code = M.AUTH_REQUIRED
                        rest.failure_class = "auth"
                        rest.note = "账号需要重新登录"
                        rest.reason = "账号掉登录了，剩下的没再试"
                stop_reason = M.AUTH_REQUIRED
                break

            if out.kind == "busy":
                # 账号正被别的进程用：把这条放回队列，等下一轮
                item.status = M.ITEM_QUEUED
                job.status = M.JOB_QUEUED
                self.store.save(job)
                return {"status": M.JOB_QUEUED}

            if out.kind == "no_lane":
                # 环境问题（没装浏览器 / profile 不对 / 端口被占）：条目不能算失败，
                # 否则用户装好浏览器之后还得手工把这几条改回来。
                item.status = M.ITEM_QUEUED
                job.status = M.JOB_QUEUED
                job.note = out.note[:200]
                self.store.save(job)
                return {"status": M.JOB_QUEUED}

            if out.kind in ("timeout", "empty"):
                # 超时 ≠ 失败：远端可能已经把图出完了。自动重投会重复出图、重复烧额度，
                # 所以整单标 uncertain 并立刻收手，等人决定。
                # empty = 回合结束了但一直没读到图（网慢时图会晚出来）：同理，
                # 判"没画出来"会误导用户去改提示词（2026-11-06 用户实测）。
                item.status = M.ITEM_FAILED
                item.error_code = M.TIMEOUT_UNCERTAIN
                item.failure_class = "unsure"
                item.note = out.note or (
                    "回合结束了但没读到图" if out.kind == "empty" else "超时，远端可能已经出图")
                item.reason = out.reason or (
                    "回合结束了却没读到图：可能还在生成。去网页看一眼，有图就收下。"
                    if out.kind == "empty" else
                    "发出去之后连接断了，不确定网页那边有没有出图")
                job.uncertain = True
                for rest in job.items:
                    if rest.status == M.ITEM_QUEUED:
                        rest.status = M.ITEM_SKIPPED
                        rest.error_code = M.TIMEOUT_UNCERTAIN
                        rest.failure_class = "unsure"
                        rest.note = "整单已标不确定，等人工确认后再重投"
                        rest.reason = "前一张没读到结果，整单标不确定：先去网页确认有没有出图"
                stop_reason = M.TIMEOUT_UNCERTAIN
                break

            item.status = M.ITEM_FAILED
            item.error_code = M.to_code(out.kind)
            item.note = out.note
            item.log_path = out.log or ""
            from .. import why as _why2

            item.reason = out.reason or _why2.explain(out.note, out.kind)
            item.failure_class = _why2.classify(out.note, item.error_code or "").cls
            if item.failure_class == "policy":
                # 别让"它不肯画"混进"没画出来"：两者给用户的建议完全相反
                item.error_code = "CONTENT_POLICY"
            job.reason = item.reason
            self._save_progress(job)

        finished = self.store.finish(job, error_code=stop_reason)
        if finished.rendered:
            self.store.bump_account(acc.id, finished.rendered)
        st = self.store.account(acc.id)
        st.login = "ok" if st.login != "required" else st.login
        st.last_used_at = time.time()
        # 连续失败计数：出到图就清零，失败就累加。分配时用它降权，
        # 这样外部 Agent 重投时不会再挑回这个一直撞风控/验证的号（2026-11-11）。
        # 人手动「停下这张」不算账号故障，别误伤。
        if finished.status == M.JOB_CANCELLED:
            pass
        elif finished.rendered and finished.status in (M.JOB_DONE, M.JOB_PARTIAL):
            st.fail_streak = 0
        elif (finished.status in (M.JOB_FAILED, M.JOB_PARTIAL)
              and stop_reason not in (M.QUOTA_COOLDOWN, M.AUTH_REQUIRED)):
            # quota / auth 已有专门的冷却与停用状态，再累加就是双重惩罚
            st.fail_streak = int(getattr(st, "fail_streak", 0) or 0) + 1
            st.last_fail_at = time.time()
        self.store.save_account(st)
        return {"status": finished.status, "rendered": finished.rendered}

    # ------------------------------------------------------------ 跑文本单
    def _run_ask_job(self, job: M.Job, acc: cfg_mod.Account, job_dir) -> dict:
        """问一句、把回复落盘。失败也留人话，和出图一张的收尾保持一致。"""
        from ..driver import ask_job
        from .. import why as _why

        item = next((it for it in job.items if it.status == M.ITEM_QUEUED), None)
        if item is None:
            finished = self.store.finish(job)
            return {"status": finished.status, "rendered": finished.rendered}

        r = ask_job.run_text(self.cfg, acc, item.prompt, timeout=300.0)
        item.log_path = str(r.get("log") or "")
        if r.get("ok"):
            text = str(r.get("text") or "")
            dest = job_dir / ("%s.txt" % item.id)
            try:
                dest.write_text(text, encoding="utf-8")
            except OSError:
                pass
            item.status = M.ITEM_OK
            item.error_code = None
            item.note = text[:300]
            item.reason = ""
            job.note = text[:400]
            job.partial = bool(r.get("partial"))
        else:
            err = str(r.get("error") or "")
            if err.startswith("EMPTY_REPLY"):
                code, cls = M.ASK_EMPTY, "empty"
            elif err.startswith("TIMEOUT"):
                code, cls = M.ASK_TIMEOUT, "unsure"
            elif err.startswith("NO_LANE"):
                code, cls = "NO_LANE", "network"
            elif err.startswith("NO_COMPOSER"):
                code, cls = M.ASK_FAILED, "auth"
            elif err.startswith("SEND_FAILED"):
                code, cls = M.ASK_SEND, "page"
            else:
                code, cls = M.ASK_FAILED, "other"
            item.status = M.ITEM_FAILED
            item.error_code = code
            item.failure_class = cls
            item.note = err[:300]
            item.reason = _why.classify(err, code, kind="ask").detail
            job.reason = item.reason
            # 等超时的不确定：网页那边可能已经答完，自动重问会重复花额度
            if code == M.ASK_TIMEOUT:
                job.uncertain = True
            # 失败时 note 绝不能写错误码：列表第一行和详情"回复"都会读它，
            # 用户会看到"EMPTY_REPLY"被当成回复，还带个复制按钮（2026-11-06 评审）。
            job.note = ""

        # claim() 已经把 started_at 写好了；这里只需要 finish 落 finished_at。
        # 面板的「耗时」、文本图表的中位耗时都靠这两个时间
        #（0.2.50 之前 ask 只建单不写时间，统计里根本看不到文本）。
        now = time.time()
        finished = self.store.finish(job)
        st = self.store.account(acc.id)
        st.last_used_at = now
        if st.login == "required":
            st.login = "ok"
        # 和出图单同一套连续失败计数，避免文本单把坏号的计数洗掉或漏记
        if finished.status == M.JOB_DONE:
            st.fail_streak = 0
        elif finished.status == M.JOB_FAILED:
            st.fail_streak = int(getattr(st, "fail_streak", 0) or 0) + 1
            st.last_fail_at = now
        self.store.save_account(st)
        return {"status": finished.status, "rendered": finished.rendered}

    # ------------------------------------------------------------ 换号
    def _worth_hop(self, out) -> bool:
        """这个失败换一个号重来有没有意义。

        - verify / page / busy：环境问题，换个号多半能过
        - render：只有在"消息没发出去 / 消息流报错"时才值得换（模型本身拒答换号也没用）
        - quota / auth / timeout / policy / file：换号要么没用，要么会重复烧额度
        """
        kind = (out.kind or "").strip().lower()
        if kind in ("verify", "page", "busy"):
            return True
        if kind == "render":
            note = (out.note or "") + (out.reason or "")
            if "被拒答" in note or "内容政策" in note:
                return False
            return any(w in note for w in ("没发出去", "空回复", "消息流", "消息没能", "本地草稿"))
        return False

    def _max_hops(self) -> int:
        """最多换几个号。三个号时最多换两次（每个号都试一遍就够）。"""
        try:
            return max(0, min(3, len(self.cfg.accounts) - 1))
        except Exception:
            return 0

    def _other_account(self, used: cfg_mod.Account):
        """挑一个和 used 不同、现在能用的号；优先今天出图少的。"""
        try:
            cands = [a for a in self.usable_accounts() if a.id != used.id]
        except Exception:
            return None
        if not cands:
            return None
        # 换号也要按同一套策略挑：别一换就换回那个连续失败/低档位的号。
        from .. import config as cfg_mod
        t, p, plan, fail = self._strategy_callbacks()
        try:
            return cfg_mod.choose_by_strategy(self.cfg, cands, t, p, plan, fail) or cands[0]
        except Exception:
            return cands[0]

    # ------------------------------------------------------------ 账号状态
    def _note_failure(self, acc: cfg_mod.Account) -> None:
        """记一次账号级连续失败，给分配降权用。

        只在「早退出、走不到 _run_job 收尾」的失败分支调用（自动换号那类），
        正常走到 finish() 的失败由收尾统一处理，两边不会重复计数。
        """
        try:
            st = self.store.account(acc.id)
            st.fail_streak = int(getattr(st, "fail_streak", 0) or 0) + 1
            st.last_fail_at = time.time()
            self.store.save_account(st)
        except Exception:
            pass

    def _mark_quota(self, acc: cfg_mod.Account, job: M.Job, item: M.Item) -> None:
        st = self.store.account(acc.id)
        now = time.time()
        st.quota_hits.append({
            "at": now,
            "job": job.id,
            "after_n": job.rendered,          # 这一批里已经出了几张才撞上
            "reset_at": now + self.quota_cooldown,
        })
        # 只留最近 20 次，文件别无限长
        st.quota_hits = st.quota_hits[-20:]
        st.cooldown_until = now + self.quota_cooldown
        self.store.save_account(st)

    def _mark_auth(self, acc: cfg_mod.Account) -> None:
        st = self.store.account(acc.id)
        st.login = "required"
        st.login_checked_at = time.time()
        self.store.save_account(st)
        self._pop_login_window(acc)

    def _auto_requeue_after_login(self) -> None:
        """登好了就把"因掉登录而失败"的那批自动补投一次。

        用户点完登录回来，不该还要自己翻历史找哪单没跑完。
        每个源单只自动补一次（记在 state/auth_requeued.json）：补了又失败就不再补，
        免得死循环白烧额度。
        """
        now = time.time()
        if now - getattr(self, "_auth_check_at", 0.0) < 10:
            return
        self._auth_check_at = now

        ok_accounts = {a.id for a in self.cfg.accounts
                       if self.store.account(a.id).login == "ok"}
        if not ok_accounts:
            return

        mark = self.store.state / "auth_requeued.json"
        try:
            done = set(json.loads(mark.read_text(encoding="utf-8")))
        except Exception:
            done = set()

        changed = False
        for job in self.store.query(limit=40).get("rows") or []:
            src_id = job["id"] if isinstance(job, dict) else job.id
            if src_id in done:
                continue
            src = self.store.load(src_id)
            if not src or src.account_id not in ok_accounts:
                continue
            todo = [it for it in src.items if it.error_code == M.AUTH_REQUIRED]
            if not todo:
                continue
            new_items = [M.Item(id=it.id, prompt=it.prompt, aspect=it.aspect,
                                alpha=it.alpha, refs=list(it.refs)) for it in todo]
            try:
                made, created = self.store.new_job(
                    account_id=src.account_id, items=new_items,
                    brief="补 R%d（登录好了，自动接着跑）" % src_id,
                    tag=src.tag, submitter="auto",
                    request_id="auth-%d" % src_id)
                done.add(src_id)
                changed = True
                if created:
                    print("登录已恢复：自动补投 R%d（%d 条）" % (made.id, len(new_items)))
            except Exception as e:
                print("!! 自动补投失败：%s" % e)

        if changed:
            try:
                mark.write_text(json.dumps(sorted(done)), encoding="utf-8")
            except Exception:
                pass

    def _pop_login_window(self, acc: cfg_mod.Account) -> None:
        """把登录窗口直接送到人面前。

        为什么不是"面板上提示一下让他去点"：掉登录的时候他正在等图，
        多一步就多一次"这软件是不是坏了"的怀疑。也不能替他登——密码和验证码
        只有人能给。能做到的极限就是：窗口自己弹出来，人只要输密码。
        """
        # 这两道闸是 2026-10-29 补的：test_coordinator 的契约是"不联网、不起浏览器"，
        # 加了自动弹窗之后 pytest 真的拉起了一个 Chrome，跑完还留在任务栏里
        # （用户看到"半瓢关了还有两个 chrome 窗口"）。测试里绝不允许碰图形界面。
        if not getattr(self, "open_login", True):
            return
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return

        import threading

        def work():
            try:
                from ..driver import browser as br
                br.ensure(self.cfg, acc, visibility="keep", timeout=60)
                print("账号 %s 掉登录了：登录窗口已经弹出来，登完会自动继续。"
                      % acc.name)
            except Exception as e:
                print("!! 自动弹登录窗口失败：%s（去面板「账号」页点「登录这个号」）" % e)

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 常驻
    def run_forever(self, *, poll: float = 3.0, stop: threading.Event | None = None) -> None:
        """常驻跑：开 ``concurrency`` 个 worker，各自领单。

        为什么不是"一个循环里开线程跑任务"：worker 各自独立领单，
        任何一单卡住（比如等图 10 分钟）都不会挡住别的账号继续干活。
        """
        stop = stop or threading.Event()

        def worker(idx: int) -> None:
            while not stop.is_set():
                # **整轮都要护住**，不只是跑单那一段。以前 _next_claim 在 try 外面，
                # 一次 WinError 5（写 job.json 时被杀软/索引器占用）就把这条线程打死，
                # 队列从此静默停摆——R105 卡 2 小时、R115 卡 24 分钟都是这一条。
                try:
                    got = self._next_claim()
                    if not got:
                        stop.wait(poll)
                        continue
                    job, acc = got
                    try:
                        self._run_job(job, acc)
                    finally:
                        self._free_slot(job.id)
                except Exception as e:      # noqa: BLE001
                    print("!! 调度出错（%s），稍后重试：%s" % (str(e)[:100], str(e)[:40]))
                    stop.wait(self.fail_backoff)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True,
                                    name="coord-%d" % i)
                   for i in range(self.concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
