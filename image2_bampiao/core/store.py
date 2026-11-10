"""磁盘上的任务库：一个任务一个目录 + 一份 job.json。

为什么不用 SQLite（v1 明确不做）：
- 只有一个写者（coordinator），没有读写竞态；
- 个人用户一年几千单，目录 + JSON 完全够；
- 用户发来问题时能直接 `cat job.json`，不用求他装个 sqlite 客户端。

三条必须守住的写规则：
1. **原子写**：先写 `.tmp` 再 `os.replace`，断电也不会留半截 JSON。
2. **投单幂等**：同一个 ``request_id`` 只会产生一个任务（重复点两次、agent 重试都不会翻倍）。
3. **领取互斥**：``queued → running`` 靠文件锁 + 重读确认，两个进程不会抢到同一单。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import config as cfg_mod, locks
from .. import media as media_mod

# 列表每 6 秒刷一次：probe 只做一次，之后按 (路径, mtime, 大小) 命中
_MEDIA_CACHE: dict[tuple, dict] = {}
from .models import (AccountState, Item, Job, JOB_DONE, JOB_FAILED, JOB_PARTIAL,
                     JOB_QUEUED, JOB_RUNNING, JOB_STATES)


class StoreError(RuntimeError):
    pass


def _now_stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t or time.time()))


class Store:
    def __init__(self, cfg: cfg_mod.Config):
        self.cfg = cfg
        self.root = Path(cfg.data_dir) / "jobs"
        self.state = Path(cfg.data_dir) / "state"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self._facets_cache: dict = {}     # {"sig": ..., "v": ...}，见 facets()

    # ------------------------------------------------------------ 路径
    def dir_of(self, job_id: int) -> Path:
        # 四位补零：目录按名字排序就是时间顺序，翻页/找最早的任务都方便
        return self.root / ("%04d" % int(job_id))

    def manifest(self, job_id: int) -> Path:
        return self.dir_of(job_id) / "job.json"

    def _seq_file(self) -> Path:
        return self.state / "seq.json"

    def _index_file(self) -> Path:
        return self.state / "requests.json"

    def _lock(self) -> Path:
        return self.state / "store.lock"

    # ------------------------------------------------------------ 原子读写
    @staticmethod
    def _write_json(path: Path, obj) -> None:
        """原子写。**必须能扛住瞬时占用**，否则一次撞车就够毁掉整条队列。

        2026-10-31 实测（desktop.log）：
            PermissionError: [WinError 5] 拒绝访问。
                '...jobs\\0112\\job.json.37740.tmp' -> '...jobs\\0112\\job.json'
        这一行发生在领单途中（`_next_claim` → `claim` → `save`），当时裸调 os.replace，
        异常冒到 worker 线程最外层，coord-1 直接死掉——队列从此静默停摆，
        R105 卡了两个多小时、R115 卡了 24 分钟，用户只看到"好慢""都是等待"。
        Windows 上目标文件被杀软/索引器/另一个进程短暂占用就会这样，重试基本都能过。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(obj, ensure_ascii=False, indent=1)
        tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
        tmp.write_text(data, encoding="utf-8")
        for delay in (0.0, 0.05, 0.15, 0.4):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                continue
            except OSError:
                break
        # 兜底：宁可这一次少个原子性，也不能让调度线程因为写文件而死
        try:
            path.write_text(data, encoding="utf-8")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------ 任务
    def load(self, job_id: int) -> Job | None:
        d = self._read_json(self.manifest(job_id))
        if not isinstance(d, dict):
            return None
        try:
            j = Job.from_dict(d)
        except (KeyError, TypeError, ValueError):
            return None
        # 旧版本（0.2.50 早几轮）写过"状态 done、条目还在排队"的僵尸单：
        # 列表会显示"已回复"却 0/1。这里只在**读的时候**纠正成 failed，
        # 绝不能算回 queued——否则调度器会把它当新单再跑一遍（白花额度）。
        if j.status == JOB_DONE and j.items and \
                not any(it.status == "ok" for it in j.items):
            j.status = JOB_FAILED
            verb = "重问" if (j.kind or "image") == "ask" else "重投"
            for it in j.items:
                if it.status == "queued":
                    it.status = "failed"
                    it.error_code = it.error_code or (
                        "ASK_FAILED" if (j.kind or "image") == "ask" else "RENDER_FAILED")
                    it.failure_class = it.failure_class or "other"
                    it.reason = it.reason or (
                        "旧版本遗留：这单没真正跑完（当时没记下原因）。先点「其实还没处理好」再%s" % verb)
                    it.note = it.note or "zombie"
            j.reason = j.reason or (j.items[0].reason if j.items else "")
        # 0.2.50 早期版本把失败原因只写在 job.reason（EMPTY_REPLY），条目的
        # error_code/note 都是空的：于是横幅算"原因不明"、列表却翻成"没读到回复"、
        # 详情又说"没记下原因"——三处对不上（2026-11-06 三评）。
        # 读的时候归一，三处自动一致。
        if (j.kind or "image") == "ask" \
                and (j.reason or "").strip().upper() == "EMPTY_REPLY":
            for it in j.items:
                if it.status == "failed" and not it.error_code:
                    it.error_code = "ASK_EMPTY"
                    it.failure_class = "empty"
                    it.reason = it.reason or (
                        "网页里没读到这次的回复：可能还在生成，也可能页面没刷出来")
        return j

    def save(self, job: Job) -> None:
        self._write_json(self.manifest(job.id), job.as_dict())

    def new_job(self, *, account_id: str, items: list[Item], brief: str = "",
                tag: str = "", submitter: str = "cli", request_id: str = "",
                opening: str = "", status: str = JOB_QUEUED,
                retry_of: int | None = None, kind: str = "image") -> tuple[Job, bool]:
        """建一单。返回 (任务, 是否新建)。同一个 request_id 会返回已有那一单。"""
        if not items:
            raise StoreError("一单至少要有一张图")
        request_id = request_id or ("r%d" % int(time.time() * 1000))

        with locks.exclusive(self._lock(), timeout=30, what="任务库"):
            idx = self._read_json(self._index_file()) or {}
            if not idx and self._max_id() > 0:
                # 索引文件丢了但任务还在：重建，否则幂等键失效，
                # agent 重试一次就会多出一模一样的一单。
                idx = self._rebuild_index()
            if request_id in idx:
                jid = int(idx[request_id])
                old = self.load(jid)
                if old:
                    return old, False          # 幂等：重复投单不再生成新任务

            seq = int((self._read_json(self._seq_file()) or {}).get("next") or 1)
            # 极端情况：seq 丢了但目录还在，从目录最大值续上，避免覆盖旧任务
            if seq <= 1:
                seq = max([self._max_id() + 1, 1])

            job = Job(id=seq, request_id=request_id, account_id=account_id,
                      brief=brief, tag=tag, submitter=submitter,
                      status=status, items=items, opening=opening,
                      retry_of=retry_of, kind=kind)
            if status == JOB_RUNNING:
                job.started_at = time.time()   # gen 直接跑：一出生就是在跑，调度器不会去领
            self.save(job)
            self._write_json(self._seq_file(), {"next": seq + 1})
            idx[request_id] = seq
            self._write_json(self._index_file(), idx)
            return job, True

    def _max_id(self) -> int:
        best = 0
        for p in self.root.iterdir():
            if p.is_dir() and p.name.isdigit():
                best = max(best, int(p.name))
        return best

    def _rebuild_index(self) -> dict:
        idx = {}
        for p in self.root.iterdir():
            if not p.is_dir() or not p.name.isdigit():
                continue
            d = self._read_json(p / "job.json")
            if isinstance(d, dict) and d.get("request_id"):
                idx[str(d["request_id"])] = int(p.name)
        self._write_json(self._index_file(), idx)
        return idx

    def claim(self, job_id: int, *, create_dir: bool = True,
              ran_on: str = "") -> Job | None:
        """把 queued 原子地翻成 running。抢不到（已经被别人领了）返回 None。

        ``create_dir=False`` 用于"扫描队列"的场景：扫到一半遇到已删除的任务时，
        不该在磁盘上凭空建出一个空目录。
        ``ran_on`` 记下**实际**接手的账号（可能和 job.account_id 不同：原定的号在忙
        或在冷却，调度器换了号）。没有它，面板上四单并行全显示同一个号。
        """
        d = self.dir_of(job_id)
        if not d.exists():
            if not create_dir:
                return None
            d.mkdir(parents=True, exist_ok=True)
        try:
            with locks.exclusive(d / ".claim", timeout=0, what="R%d" % job_id):
                job = self.load(job_id)
                if not job or job.status != JOB_QUEUED:
                    return None
                job.status = JOB_RUNNING
                job.started_at = time.time()
                job.attempts += 1
                if ran_on:
                    job.ran_on = ran_on
                self.save(job)
                return job
        except locks.Busy:
            return None

    def pending(self, limit: int = 50) -> list[Job]:
        out = []
        for p in sorted(self.root.iterdir()):
            if not p.is_dir() or not p.name.isdigit():
                continue
            j = self.load(int(p.name))
            if j and j.status == JOB_QUEUED:
                out.append(j)
                if len(out) >= limit:
                    break
        return out

    def running(self) -> list[Job]:
        out = []
        for p in sorted(self.root.iterdir()):
            if not p.is_dir() or not p.name.isdigit():
                continue
            j = self.load(int(p.name))
            if j and j.status == JOB_RUNNING:
                out.append(j)
        return out

    def active_load(self) -> dict[str, int]:
        """每个账号手上还有几单没跑完（排队 + 在跑）。

        投单时挑账号要先看这个，再看"今天出了几张"：连续投四单的那一刻，
        四单都还没结算，只看今天计数会全部挑中同一个号——面板上看着像
        "三个号只用一个"，实际调度才换号（2026-11-05 实机复现）。
        """
        out: dict[str, int] = {}
        for j in self.running() + self.pending(limit=1000):
            aid = j.ran_on or j.account_id
            if aid:
                out[aid] = out.get(aid, 0) + 1
        return out

    def finish(self, job: Job, *, error_code: str | None = None,
               uncertain: bool = False, note: str = "") -> Job:
        job.status = job.derive_status()
        job.finished_at = time.time()
        job.uncertain = bool(uncertain) or job.uncertain
        job.error_code = error_code or job.error_code
        if note:
            job.note = note
        self.save(job)
        return job

    # ------------------------------------------------------------ 查询（给面板）
    def query(self, *, limit: int = 24, offset: int = 0, status: str = "all",
              account: str = "all", tag: str = "all", q: str = "",
              kind: str = "all") -> dict:
        """列表页。只解析本页的 manifest，不为显示 24 条去读 1000 个文件。

        筛选只用目录序（id 就是时间序）+ 本页 manifest；status/q 需要对每条解析，
        所以先按 id 倒序切片再筛会漏，这里的折中是：先扫目录名（便宜），
        命中页窗口内再解析。
        """
        ids = [int(p.name) for p in self.root.iterdir()
               if p.is_dir() and p.name.isdigit()]
        ids.sort(reverse=True)

        rows = []
        only_attention = (status == "attention")
        attn_ids: set[int] = set()
        if only_attention:
            # kind 要一起带进去：只看"出图"的待处理时，文本单不该混进来
            attn_ids = {j["id"] for j in
                        self.attention(limit=1000000, kind=kind)["jobs"]}
            status = "all"
        # ★ only_attention 必须排除在快路径之外：这里把 status 改成 "all" 之后，
        # 条件全成立，快路径会直接把整页返回——筛选等于没做。
        # 2026-11-06 实测：status=attention 和 status=all 都返回 total 334。
        plain = (status in ("", "all") and account in ("", "all")
                 and tag in ("", "all") and not q and kind in ("", "all")
                 and not only_attention)
        if plain:
            total = len(ids)
            page = ids[offset:offset + limit]
            rows = [j for j in (self.load(i) for i in page) if j]
        else:
            for i in ids:
                j = self.load(i)
                if not j:
                    continue
                if only_attention and j.id not in attn_ids:
                    continue
                if status not in ("", "all") and j.status != status:
                    continue
                if account not in ("", "all") and j.account_id != account \
                        and (j.ran_on or "") != account:
                    continue
                if tag not in ("", "all") and (j.tag or "") != tag:
                    continue
                # 2026-11-05：任务列表要能只看出图 / 只看文本（ask）
                if kind not in ("", "all") and (j.kind or "image") != kind:
                    continue
                if q:
                    qq = q.lower()
                    # 文本单要能搜回复内容：用户记的是答案，不是当时怎么问的
                    if qq not in str(j.id) and qq not in (j.brief or "").lower() \
                            and qq not in (j.tag or "").lower() \
                            and qq not in (j.note or "").lower():
                        continue
                rows.append(j)
            total = len(rows)
            rows = rows[offset:offset + limit]
        return {"total": total, "offset": offset, "limit": limit,
                "rows": [self._row_view(j) for j in rows]}

    def _row_view(self, job) -> dict:
        """列表行要用的派生字段：缩略图、说明回退、失败短标签、是否已处理。

        这里算而不是前端算：前端拿不到分类词典，也不该重复一套文案。
        """
        from .models import ITEM_FAILED, ITEM_OK, ITEM_SKIPPED

        d = job.as_dict()
        d["display_brief"] = job.display_brief
        d["handled"] = bool(job.handled_at)
        d["retried"] = bool(job.retry_of)
        d["thumb"] = ""
        d["animated"] = False
        # 2026-11-07：SVG 单独立标记，绝不塞进 ``animated``——那个字段在面板上
        # 画的是 "GIF" 角标，把矢量图说成 GIF 会误导。
        d["svg"] = False
        d["svg_animated"] = False
        _all_files = []
        for it in d.get("items") or []:
            _fs = it.get("files") or []
            _all_files.extend(_fs)
            if not d["thumb"] and _fs:
                d["thumb"] = "/f/%d/%s?w=80" % (job.id, _fs[0])
            if any(media_mod.is_anim_ext(_f) for _f in _fs):
                d["animated"] = True
            if any(media_mod.is_svg_path(_f) for _f in _fs):
                d["svg"] = True
        # 旧单可能把 GIF 存成了 .png（0.2.51 及以前）：后缀看不出来，就按内容认一次。
        # 只 probe 第一条，带缓存；24 行 × 每 6 秒也不会真的反复读盘。
        if not d["animated"]:
            for _f in _all_files:
                try:
                    _p = self.dir_of(job.id) / _f
                    _st = _p.stat()
                    _key = (str(_p), _st.st_mtime, _st.st_size)
                    _info = _MEDIA_CACHE.get(_key)
                    if _info is None:
                        _info = media_mod.probe(_p)
                        if len(_MEDIA_CACHE) > 2000:
                            _MEDIA_CACHE.clear()
                        _MEDIA_CACHE[_key] = _info
                    if _info.get("animated"):
                        d["animated"] = True
                        break
                except Exception:
                    continue
        # SVG 单独探测，不插进上面那段老逻辑：只对 .svg 文件跑一次，
        # 没有 SVG 的单子连这半行开销都不会多。
        if d["svg"]:
            for _f in _all_files:
                if not media_mod.is_svg_path(_f):
                    continue
                try:
                    _info = media_mod.probe_svg(self.dir_of(job.id) / _f)
                    if _info.get("svg_animated"):
                        d["svg_animated"] = True
                        break
                except Exception:
                    continue
        d["failure_short"] = ""
        for it in job.items:
            if it.status in (ITEM_FAILED, ITEM_SKIPPED) and it.resolved_by != "found":
                from .. import why as _why
                info = _why.classify(it.note or "", it.error_code or "", bool(job.uncertain),
                                     kind=(job.kind or "image"))
                d["failure_short"] = info.short
                d["failure_title"] = info.title
                d["failure_class"] = info.cls
                break
        return d

    def stats(self, days: int = 14, kind: str = "all") -> dict:
        """按天统计产量。**按每张图的完成时刻归日**，不是按投单时刻。

        旧系统拿"这一批提交了几张"累加，跨天的一批会整个算到前一天，用户对不上账。

        ``kind``：image / ask / all。文本单和出图单必须分开算——混在一起
        "每张中位耗时""成功率"都没有意义（文本一条几十秒、图一张一两分钟）。
        老 ask 单没有 finished_at（0.2.50 之前只建单不写时间），用 created_at 兜底。
        """
        import datetime as _dt

        days = max(1, min(90, int(days)))
        today = _dt.date.today()
        span = [(today - _dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        bucket = {d: 0 for d in span}
        by_tag: dict[str, int] = {}
        # 管理页要的不只是"出了几张"：哪个号出的、失败栽在哪、一张图多久
        by_acc: dict[str, dict[str, int]] = {}
        outcomes: dict[str, int] = {"ok": 0}
        secs: list[float] = []
        start_day = span[0]

        for p in self.root.iterdir():
            if not p.is_dir() or not p.name.isdigit():
                continue
            j = self.load(int(p.name))
            if not j:
                continue
            job_kind = (j.kind or "image")
            if kind not in ("", "all") and job_kind != kind:
                continue
            finished = j.finished_at or (j.created_at if job_kind == "ask" else None)
            if not finished:
                continue
            day = _now_stamp(finished)
            # ★ 产量和成败是两件事：
            # 以前 `if not j.rendered: continue` 把"一张没成"的单整个跳过，
            # 于是 12 成功 + 5 失败显示成 100%，414 成功 + 74 失败显示成 95%。
            # 现在产量只加 rendered，成败按每一条的结局单独数（2026-11-06 评审）。
            if day in bucket:
                bucket[day] += j.rendered
                acc_b = by_acc.setdefault((j.ran_on or j.account_id) or "?", {})
                acc_b[day] = acc_b.get(day, 0) + j.rendered
            if j.tag:
                by_tag[j.tag] = by_tag.get(j.tag, 0) + j.rendered
            if day >= start_day:
                for it in j.items:
                    if it.status == "ok":
                        outcomes["ok"] += 1
                    elif it.status in ("failed", "skipped", "cancelled"):
                        code = it.error_code or "UNKNOWN"
                        outcomes[code] = outcomes.get(code, 0) + 1
                if j.started_at and j.rendered:
                    secs.append(max(0.0, (finished - j.started_at) / j.rendered))

        series = [{"date": d, "n": bucket[d]} for d in span]
        acc_series = {a: [b.get(d, 0) for d in span] for a, b in by_acc.items()}
        secs.sort()
        median = round(secs[len(secs) // 2]) if secs else None
        total = sum(bucket.values())
        d7 = sum(x["n"] for x in series[-7:])
        return {"series": series, "total": total, "today": bucket.get(today.isoformat(), 0),
                "d7": d7, "avg": round(total / float(days), 1) if days else 0.0,
                "by_tag": by_tag, "by_account": acc_series, "outcomes": outcomes,
                "median_sec_per_image": median}

    def facets(self) -> dict:
        """筛选项：有哪些账号、有哪些 tag。给面板的下拉框用。

        面板每 5 秒轮询一次，这个函数要扫所有单的 manifest——600 单时约 140ms，
        等于每轮都白白烧一次。用"单数 + 目录 mtime"当签名，没变就直接返回上次的。
        """
        try:
            dirs = [p for p in self.root.iterdir()
                    if p.is_dir() and p.name.isdigit()]
            sig = (len(dirs), max((p.stat().st_mtime for p in dirs), default=0.0))
        except OSError:
            sig = None
        if sig is not None and self._facets_cache.get("sig") == sig:
            return self._facets_cache["v"]

        accounts, tags = {}, {}
        for p in (dirs if sig is not None else self.root.iterdir()):
            if not p.name.isdigit():
                continue
            j = self.load(int(p.name))
            if not j:
                continue
            accounts[j.account_id] = accounts.get(j.account_id, 0) + 1
            t = j.tag or ""
            if t:
                tags[t] = tags.get(t, 0) + 1
        out = {"accounts": accounts, "tags": tags}
        if sig is not None:
            self._facets_cache = {"sig": sig, "v": out}
        return out

    def summary(self) -> dict:
        """给顶栏用的几个数：待跑 / 在跑 / 今天完成张数。带 3 秒缓存。

        面板每 5 秒拉一次；不带缓存的话每次都要把上千个 manifest 全解析一遍。
        """
        now = time.time()
        cached = getattr(self, "_summary_cache", None)
        if cached and now - cached[0] < 6:      # 面板 5 秒一轮，短于它等于没有缓存
            return cached[1]

        queued = running = 0
        today = _now_stamp()
        today_n = 0
        today_ask = 0
        total = 0
        ever = 0            # 一共出成过几张：上手引导第三步「试一张」用
        for p in self.root.iterdir():
            if not p.is_dir() or not p.name.isdigit():
                continue
            total += 1
            j = self.load(int(p.name))
            if not j:
                continue
            if j.status == JOB_QUEUED:
                queued += 1
            elif j.status == JOB_RUNNING:
                running += 1
            elif j.status in (JOB_DONE, JOB_PARTIAL, JOB_FAILED):
                # today_images / images_ever 只算图：文本单混进来会让
                # "今天完成 12 张"变成一句假话（2026-11-06）。
                if (j.kind or "image") == "ask":
                    fin = j.finished_at or j.created_at
                    if fin and _now_stamp(fin) == today:
                        today_ask += j.rendered
                    continue
                ever += j.rendered
                if j.finished_at and _now_stamp(j.finished_at) == today:
                    today_n += j.rendered
        out = {"jobs": total, "queued": queued, "running": running,
               "today_images": today_n, "today_ask": today_ask, "images_ever": ever}
        self._summary_cache = (now, out)
        return out

    def requeue_unstarted(self, alive=None, min_age: float = 90.0) -> list[int]:
        """把「被领走、但一项都没开始」的单放回队列。

        为什么单独拎出来：`recover_running` 一律标 `TIMEOUT_UNCERTAIN`（怕图已经画出来了），
        可子项**全是 queued** 的单根本没发出去过——没有图，也没有额度被烧。
        标成不确定只会让人以为要人工核对一堆不存在的图（2026-10-31 R115 就是这样：
        12 项全 queued 卡了 24 分钟，用户看着"全是等待"，还以为软件坏了）。
        这种直接重排，用户无感。

        保守起见仍然要过两道闸：跑够 min_age（默认 90 秒，别抢正在启动的单）、
        且这个号此刻没有真在出图（出图锁空闲）。
        """
        from .models import ITEM_QUEUED

        out = []
        for job in self.running():
            if min_age and time.time() - float(getattr(job, "started_at", 0) or 0) < min_age:
                continue
            if not all(it.status == ITEM_QUEUED for it in job.items):
                continue                    # 有任意一项动过，就交给 recover_running 判
            if alive is not None:
                try:
                    if alive(job.account_id):
                        continue            # 真在出图，不能碰
                except Exception:
                    continue                # 判断不了就当在跑，宁可等
            job.status = JOB_QUEUED
            job.started_at = None
            job.error_code = None
            job.note = "进程中途没了、一项都没开始，已自动放回队列"
            self.save(job)
            out.append(job.id)
        return out

    def recover_running(self, alive=None, min_age: float = 0.0) -> list[int]:
        """把"上个进程退出时还在跑"的单标成不确定。

        为什么不自动重跑：图可能已经出完了（额度已经花了），再跑一遍就是重复烧。
        这条和"超时不重投"是同一个道理——不确定的事情交给人。

        alive(account_id) 返回 True 表示这个号**此刻真有进程在出图**（出图锁被占着），
        这种单不是残留，不能碰：2026-10-30 实测，桌面壳启动时把命令行正在跑的单标成了失败。
        """
        from .models import ITEM_QUEUED, ITEM_RUNNING, ITEM_SKIPPED, TIMEOUT_UNCERTAIN

        out = []
        for job in self.running():
            if min_age and time.time() - float(getattr(job, "started_at", 0) or 0) < min_age:
                continue            # 刚领到手的单给它一点时间，别误杀
            if alive is not None:
                try:
                    if alive(job.account_id):
                        continue
                except Exception:
                    pass
            job.status = JOB_FAILED
            job.finished_at = time.time()
            job.uncertain = True
            job.error_code = TIMEOUT_UNCERTAIN
            job.note = "上次退出（或崩溃）时正在跑，远端状态未知；确认后再重投"
            for it in job.items:
                # running 也要收尾：进程都没了，还挂"出图中"就是骗人
                if it.status in (ITEM_QUEUED, ITEM_RUNNING):
                    it.status = ITEM_SKIPPED
                    it.error_code = TIMEOUT_UNCERTAIN
                    it.note = "整单已标不确定"
            self.save(job)
            out.append(job.id)
        return out

    # ------------------------------------------------------------ 账号运行态
    def account(self, account_id: str) -> AccountState:
        d = self._read_json(self.state / "accounts" / ("%s.json" % account_id))
        if isinstance(d, dict):
            try:
                return AccountState.from_dict(d)
            except (TypeError, ValueError):
                pass
        return AccountState(account_id=account_id)

    def save_account(self, st: AccountState) -> None:
        self._write_json(self.state / "accounts" / ("%s.json" % st.account_id), st.as_dict())

    def bump_account(self, account_id: str, rendered: int) -> AccountState:
        st = self.account(account_id)
        st.bump_today(_now_stamp(), rendered)
        st.last_used_at = time.time()
        self.save_account(st)
        return st

    # ------------------------------------------------------------ 暂停
    def _pause_file(self) -> Path:
        return self.state / "pause.json"

    def paused(self) -> bool:
        d = self._read_json(self._pause_file())
        return bool(isinstance(d, dict) and d.get("paused"))

    def pause_info(self) -> dict:
        d = self._read_json(self._pause_file())
        return d if isinstance(d, dict) else {}

    def clear_update_pause(self) -> bool:
        """清掉「升级流程留下的」暂停；用户自己按的暂停一律不动。

        为什么需要：更新为了等空档会 set_paused(by="update")，如果装的过程中
        被打断（关机 / 装失败 / 用户取消），这份暂停会留在磁盘上，重启后半瓢
        静默不干活，界面上还看不出是谁停的（独立验证 2026-10-31 抓到）。
        返回 True 表示这次真的清了一份。
        """
        try:
            f = self._pause_file()
            d = self._read_json(f)
            if not (isinstance(d, dict) and d.get("by") == "update"):
                return False
            f.unlink()                 # 只有真的删掉才返回 True
            return True
        except FileNotFoundError:
            return False
        except OSError:
            # 只读 / 被占：删不掉就如实说"No"，别谎报"已解除"——那会让队列静默停着
            return False

    def set_paused(self, paused: bool, by: str = "", reason: str = "") -> None:
        """暂停只影响**调度**：已经在跑的那一单不打断（打断会留下半张图和白烧的额度）。"""
        if paused:
            self._write_json(self._pause_file(),
                             {"paused": True, "by": by, "reason": reason,
                              "at": time.time()})
        else:
            try:
                self._pause_file().unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------ 待处理（0.2.25）
    def _migrate_attention(self) -> None:
        """第一次跑 0.2.25：把 24 小时前的旧失败标成 legacy，别让老账一次性点亮红灯。"""
        from .models import ITEM_FAILED, ITEM_SKIPPED, JOB_FAILED, JOB_PARTIAL

        p = self.state / "attention_init.json"
        if p.exists():
            return
        now = time.time()
        cut = now - 24 * 3600
        for job in self._all_jobs():
            try:
                if job.handled_at:
                    continue
                if job.status not in (JOB_FAILED, JOB_PARTIAL):
                    continue
                if float(job.finished_at or job.created_at or 0) >= cut:
                    continue
                job.handled_at = now
                job.handled_by = "legacy"
                self.save(job)
            except Exception:
                continue
        self._write_json(p, {"at": now})

    def _all_jobs(self):
        for d in sorted(self.root.iterdir(), key=lambda x: x.name):
            if not d.is_dir() or not d.name.isdigit():
                continue
            j = self.load(int(d.name))
            if j:
                yield j

    def attention(self, now: float | None = None, limit: int = 3,
                  kind: str = "all") -> dict:
        """待处理：没画成、还没被处理、且没超过 72 小时。

        计数按"张"（用户按张想事），by_class 只读落盘的 failure_class，绝不回头读日志。
        count==0 就不亮灯——哪怕还有 uncertain 单。

        ``kind`` 只影响**返回的清单和 count**；``by_kind`` 永远两种都给。
        面板的任务页能切"出图/文本"，横幅上的数字和"看这 N 单"必须跟着当前 tab，
        否则会出现"写着 4 张没画成、点进去一个都没有"（2026-11-06）。
        """
        from .models import ITEM_FAILED, ITEM_OK, ITEM_SKIPPED, JOB_FAILED, JOB_PARTIAL

        self._migrate_attention()
        now = now or time.time()
        cut = now - 72 * 3600
        rows: list[dict] = []
        by_class: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        total = 0

        for job in self._all_jobs():
            if job.handled_at:
                continue
            if job.status not in (JOB_FAILED, JOB_PARTIAL) and not job.uncertain:
                continue
            finished = float(job.finished_at or job.created_at or 0)
            if finished < cut:
                continue

            # 只数"已经有结论的失败/跳过"；还在 queued 的不算（那是没开始）。
            failed = [it for it in job.items
                      if it.status in (ITEM_FAILED, ITEM_SKIPPED)
                      and it.resolved_by != "found"]
            has_ok = any(it.status == ITEM_OK for it in job.items)
            # 一张没成、也没出图，但整单被标了不确定（进程中断）：按 1 张不确定算
            uncertain_only = (not failed) and (not has_ok) and bool(job.uncertain)
            n = len(failed) if failed else (1 if uncertain_only else 0)
            if n == 0:
                continue

            job_kind = (job.kind or "image")
            by_kind[job_kind] = by_kind.get(job_kind, 0) + n
            if kind not in ("", "all") and job_kind != kind:
                continue

            total += n
            for it in failed:
                cls = (it.failure_class or "").strip().lower()
                if cls not in ("quota", "auth", "verify", "network", "page", "file", "unsure", "other"):
                    from .. import why as _why
                    cls = _why.classify(it.note or "", it.error_code or "",
                                        kind=(job.kind or "image")).cls
                by_class[cls] = by_class.get(cls, 0) + 1
            if uncertain_only:
                by_class["unsure"] = by_class.get("unsure", 0) + 1

            first_cls = "unsure" if uncertain_only else (
                (failed[0].failure_class or "other") if failed else "other")
            rows.append({
                "id": job.id,
                "kind": job_kind,
                "cls": first_cls,
                "brief": job.display_brief[:24],
                "at": finished,
                "failed_count": n,
                "uncertain_only": uncertain_only,
                "account_id": job.account_id,
            })

        rows.sort(key=lambda r: r["at"], reverse=True)
        return {"count": total, "by_class": by_class, "jobs": rows[:limit],
                "total_jobs": len(rows), "by_kind": by_kind}
