"""半瓢共享网关：把本机 HTTP 号池按 OpenAI 兼容接口借给局域网 / Tailscale 上的人。

设计（MVP）：
- 只借 HTTP 号池（vault 里的导入号），不碰浏览器 Plus 号。
- 只暴露 /v1/*（models、images/generations）；管理面板仍只绑 127.0.0.1，绝不对外。
- 调用方用 Authorization: Bearer <key>；每个 key 有「每日张数上限 + 并发上限」。
- 后端复用：httpgen.pool.ensure_token 取 token、httpgen.worker.run_image 出图、
  core.store 记录本地额度扣减；失败按号池顺序自动换下一个号。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod, locks
from .cloud import api as cloud_api_mod, tasks as task_store
from .core import store as store_mod
from .httpgen import integration as hg
from .httpgen import pool, vault, worker

MODEL = "bampiao/image"
# GIF 动图走 gpt-image-2.6 系列（与 httpgen.integration.GIF_MODEL 同一模型）；PNG 仍用 gpt-image-2。
GIF_MODEL = "gpt-image-2.6"
GATEWAY_SKILL_URL = "https://hezhuzi.com/bampiao/dl/skill/bampiao-gateway.zip"
VALID_GROUPS = ("free", "plus", "pro")
GROUP_ORDER = {name: i for i, name in enumerate(VALID_GROUPS)}
_LOCK = threading.RLock()
# 超并发时请求排队等空位的最长时间（秒）。超过才返回 429。
# 一次出图 30-90 秒，排队给 15 分钟，足够消化一段突发（如一次发 40+ 条）。
QUEUE_WAIT = 900
# 活请求每 10 秒心跳续期一次；进程若崩溃，最后一份租约最多 60 秒后回收。
LEASE_TTL = 60.0
LEASE_HEARTBEAT_INTERVAL = 10.0
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# cfg=None 的本地兼容路径仍用进程内集合；生产调用全部走 SQLite 共享租约。
_LOCAL_INUSE: set = set()
_INUSE_LOCK = threading.Lock()
_LIMIT_CACHE: dict[str, tuple[int, int]] = {}


class _Lease:
    def __init__(self, cfg, table: str, token: str):
        self.cfg = cfg
        self.table = table
        self.token = token
        self._released = False
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="bampiao-gateway-lease",
            daemon=True,
        )
        self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(float(LEASE_HEARTBEAT_INTERVAL)):
            _renew_lease(self.cfg, self.table, self.token)

    def release(self) -> None:
        if self._released:
            return
        self._stop.set()
        if self._heartbeat is not threading.current_thread():
            self._heartbeat.join(timeout=1.0)
        self._released = _remove_lease(self.cfg, self.table, self.token)


class _AccountHold(str):
    """str 子类：保留账号 id 的旧调用方式，同时携带可释放的共享租约。"""

    def __new__(cls, account_id: str, lease: _Lease | None):
        obj = str.__new__(cls, account_id)
        obj.lease = lease
        return obj


@contextmanager
def _mutate_keys(cfg):
    """同一台机器上的 gateway/web/admin 进程串行执行密钥文件读改写。"""
    with _LOCK:
        with locks.exclusive(
            _keys_path(cfg).with_suffix(".lock"),
            timeout=15,
            what="共享网关密钥文件",
        ):
            yield


def _limits_path(cfg) -> Path:
    return Path(cfg.state_dir) / "gateway_limits.sqlite3"


def _limits_conn(cfg):
    p = _limits_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=0.25, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=250")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS request_leases ("
        "token TEXT PRIMARY KEY, key_id TEXT NOT NULL, "
        "acquired_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS request_leases_key "
        "ON request_leases(key_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account_leases ("
        "token TEXT PRIMARY KEY, account_id TEXT NOT NULL, "
        "acquired_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS account_leases_account "
        "ON account_leases(account_id)"
    )
    return conn


def _remove_lease(cfg, table: str, token: str) -> bool:
    if table not in ("request_leases", "account_leases"):
        raise ValueError("unknown lease table")
    deadline = time.monotonic() + 2.0
    while True:
        conn = None
        try:
            conn = _limits_conn(cfg)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM %s WHERE token = ?" % table, (token,))
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        finally:
            if conn is not None:
                conn.close()


def _renew_lease(cfg, table: str, token: str) -> bool:
    """把仍在使用中的租约续到当前时间之后；失败留到下个心跳重试。"""
    if table not in ("request_leases", "account_leases"):
        raise ValueError("unknown lease table")
    conn = None
    try:
        conn = _limits_conn(cfg)
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE %s SET expires_at = ? WHERE token = ?" % table,
            (time.time() + float(LEASE_TTL), token),
        )
        conn.execute("COMMIT")
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False
    finally:
        if conn is not None:
            conn.close()


def _try_request_lease(cfg, key_id: str, key_limit: int, global_limit: int):
    """原子检查全局 + 单密钥并发并插入租约；数据库忙时交给调用方重试。"""
    conn = None
    try:
        conn = _limits_conn(cfg)
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM request_leases WHERE expires_at <= ?", (now,))
        total = conn.execute("SELECT COUNT(*) FROM request_leases").fetchone()[0]
        key_count = conn.execute(
            "SELECT COUNT(*) FROM request_leases WHERE key_id = ?", (key_id,)
        ).fetchone()[0]
        if total >= global_limit or (key_limit > 0 and key_count >= key_limit):
            conn.execute("COMMIT")
            return None
        token = "req-" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO request_leases(token, key_id, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, key_id, now, now + LEASE_TTL),
        )
        conn.execute("COMMIT")
        return _Lease(cfg, "request_leases", token)
    except sqlite3.OperationalError:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return None
    finally:
        if conn is not None:
            conn.close()


def _wait_request_lease(
    cfg,
    key_id: str,
    key_limit: int,
    global_limit: int,
    cancelled=None,
):
    deadline = time.monotonic() + max(0.0, float(QUEUE_WAIT))
    while True:
        if cancelled is not None and cancelled():
            return None
        lease = _try_request_lease(cfg, key_id, key_limit, global_limit)
        if lease is not None:
            return lease
        if cancelled is not None and cancelled():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def _try_account_hold(cfg, cands, skip: set, per_account_limit: int):
    conn = None
    try:
        conn = _limits_conn(cfg)
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM account_leases WHERE expires_at <= ?", (now,))
        for aid, _acc in cands:
            if aid in skip:
                continue
            count = conn.execute(
                "SELECT COUNT(*) FROM account_leases WHERE account_id = ?", (aid,)
            ).fetchone()[0]
            if count >= per_account_limit:
                continue
            token = "acc-" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO account_leases(token, account_id, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, aid, now, now + LEASE_TTL),
            )
            conn.execute("COMMIT")
            return _AccountHold(aid, _Lease(cfg, "account_leases", token))
        conn.execute("COMMIT")
        return None
    except sqlite3.OperationalError:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return None
    finally:
        if conn is not None:
            conn.close()


def _acquire_account(cands, skip: set, cfg=None, cancelled=None) -> str | None:
    """挑一个未占用账号。生产路径使用跨进程租约；cfg=None 保留进程内兼容。"""
    if cfg is None:
        if cancelled is not None and cancelled():
            return None
        with _INUSE_LOCK:
            for aid, _acc in cands:
                if aid in _LOCAL_INUSE or aid in skip:
                    continue
                _LOCAL_INUSE.add(aid)
                return _AccountHold(aid, None)
        return None

    per_account_limit = _gateway_limits(cfg)[1]
    deadline = time.monotonic() + max(0.0, float(QUEUE_WAIT))
    while True:
        if cancelled is not None and cancelled():
            return None
        hold = _try_account_hold(cfg, cands, skip, per_account_limit)
        if hold is not None:
            return hold
        if cancelled is not None and cancelled():
            return None
        if all(aid in skip for aid, _acc in cands):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def _release_account(hold) -> None:
    lease = getattr(hold, "lease", None)
    if lease is not None:
        lease.release()
        return
    with _INUSE_LOCK:
        _LOCAL_INUSE.discard(str(hold))


# ---------------------------------------------------------------- keys 管理
def _keys_path(cfg) -> Path:
    return Path(cfg.state_dir) / "gateway_keys.json"


def _normalize_groups(groups) -> list[str]:
    """密钥分组去重并按免费优先的固定顺序保存。"""
    values = [groups] if isinstance(groups, str) else list(groups or [])
    seen = set()
    for value in values:
        group = str(value or "").strip().lower()
        if group not in VALID_GROUPS:
            raise ValueError("groups 只能包含 free、plus、pro")
        seen.add(group)
    return [group for group in VALID_GROUPS if group in seen]


def _normalize_accounts(accounts) -> list[str]:
    values = [accounts] if isinstance(accounts, str) else list(accounts or [])
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _limit_value(keyrec: dict, name: str, default: int) -> int:
    """读取密钥上限；0 或负数原样保留，由调用方解释为不限。"""
    try:
        return int(keyrec.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _gateway_limits(cfg) -> tuple[int, int]:
    """从 config.toml 读取全局/单账号保护值；每次调用重读，热改无等待窗口。"""
    path = getattr(cfg, "path", None)
    if path:
        try:
            fresh = config_mod.load(path)
            values = (
                int(fresh.gateway_global_concurrency),
                int(fresh.gateway_account_concurrency),
            )
            with _LOCK:
                _LIMIT_CACHE[str(path)] = values
            return values
        except Exception:
            # 编辑中的半截 TOML 不打断正在跑的请求；继续用最后一份有效值。
            with _LOCK:
                cached = _LIMIT_CACHE.get(str(path))
            if cached is not None:
                return cached
    return (
        int(getattr(cfg, "gateway_global_concurrency", 8)),
        int(getattr(cfg, "gateway_account_concurrency", 1)),
    )


def _load(cfg) -> dict:
    p = _keys_path(cfg)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError
        d.setdefault("keys", [])
        d.setdefault("usage", {})
        for keyrec in d["keys"]:
            if not isinstance(keyrec, dict):
                continue
            if "groups" in keyrec:
                try:
                    keyrec["groups"] = _normalize_groups(keyrec.get("groups"))
                except (TypeError, ValueError):
                    keyrec["groups"] = []
            else:
                keyrec["groups"] = ["free"]
            keyrec["accounts"] = _normalize_accounts(keyrec.get("accounts"))
        return d
    except Exception:
        return {"keys": [], "usage": {}}


def _save(cfg, d: dict) -> None:
    p = _keys_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def add_key(cfg, name: str, daily: int = 100, concurrency: int = 3, key: str | None = None,
            groups=None, accounts=None) -> dict:
    with _mutate_keys(cfg):
        d = _load(cfg)
        rec = {
            "id": uuid.uuid4().hex[:12],
            "name": (name or "friend").strip()[:30],
            "key": key or ("bp-" + secrets.token_hex(16)),
            "enabled": True,
            "daily_limit": int(daily),
            "concurrency": int(concurrency),
            "groups": _normalize_groups(["free"] if groups is None else groups),
            "accounts": _normalize_accounts(accounts),
        }
        d["keys"].append(rec)
        _save(cfg, d)
        return rec


def list_keys(cfg) -> list:
    with _LOCK:
        return list(_load(cfg)["keys"])


def rm_key(cfg, ident: str) -> int:
    with _mutate_keys(cfg):
        d = _load(cfg)
        before = len(d["keys"])
        d["keys"] = [k for k in d["keys"]
                     if ident not in (k.get("id"), k.get("name"), k.get("key"))]
        _save(cfg, d)
        return before - len(d["keys"])


def set_limit(cfg, ident: str, daily=None, concurrency=None) -> int:
    with _mutate_keys(cfg):
        d = _load(cfg)
        n = 0
        for k in d["keys"]:
            if ident in (k.get("id"), k.get("name")):
                if daily is not None:
                    k["daily_limit"] = int(daily)
                if concurrency is not None:
                    k["concurrency"] = int(concurrency)
                n += 1
        _save(cfg, d)
        return n


def set_groups(cfg, ident: str, groups) -> int:
    """设置密钥允许的账号组；老密钥缺失时按 free 读取。"""
    normalized = _normalize_groups(groups)
    with _mutate_keys(cfg):
        d = _load(cfg)
        n = 0
        for keyrec in d["keys"]:
            if ident in (keyrec.get("id"), keyrec.get("name")):
                keyrec["groups"] = normalized
                n += 1
        _save(cfg, d)
        return n


def set_accounts(cfg, ident: str, accounts) -> int:
    """设置 plus/pro 账号白名单；free 组永远忽略这个列表。"""
    normalized = _normalize_accounts(accounts)
    with _mutate_keys(cfg):
        d = _load(cfg)
        n = 0
        for keyrec in d["keys"]:
            if ident in (keyrec.get("id"), keyrec.get("name")):
                keyrec["accounts"] = normalized
                n += 1
        _save(cfg, d)
        return n


def set_enabled(cfg, ident: str, on: bool) -> int:
    """启用/停用一个分享 key。"""
    with _mutate_keys(cfg):
        d = _load(cfg)
        n = 0
        for k in d["keys"]:
            if ident in (k.get("id"), k.get("name")):
                k["enabled"] = bool(on)
                n += 1
        _save(cfg, d)
        return n


def _find_key(d: dict, token: str):
    for k in d.get("keys", []):
        if k.get("enabled", True) and secrets.compare_digest(str(k.get("key", "")), token):
            return k
    return None


def _now_shanghai() -> datetime:
    return datetime.now(_SHANGHAI_TZ)


def _today(now: datetime | None = None) -> str:
    value = now or _now_shanghai()
    if value.tzinfo is None:
        value = value.replace(tzinfo=_SHANGHAI_TZ)
    return value.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d")


def _reserve(cfg, keyrec: dict, cancelled=None):
    """每日额度 + 共享并发。返回 (ok, reason, lease_or_None)。

    2026-11-15：超并发不再立即 429，改成**排队等空位**（最多 QUEUE_WAIT 秒）。
    专业生图产品是"排队"体验：请求进来先等，有并发空位就出图，等太久才拒。
    2026-11-21：0/负数=不限；全局与单密钥并发使用 SQLite 共享租约，跨 gateway/web
    进程生效。每次 reserve 都重读密钥文件与 config.toml，修改无需重启。
    """
    kid = keyrec["id"]
    d = _load(cfg)
    current = next(
        (k for k in d.get("keys", []) if k.get("id") == kid and k.get("enabled", True)),
        None,
    )
    if current is None:
        return False, "invalid_key", None

    daily_limit = _limit_value(current, "daily_limit", 100)
    usage = d.setdefault("usage", {}).get(kid) or {}
    if (
        daily_limit > 0
        and usage.get("date") == _today()
        and int(usage.get("count", 0)) >= daily_limit
    ):
        return False, "daily_limit", None

    key_limit = _limit_value(current, "concurrency", 3)
    global_limit, _account_limit = _gateway_limits(cfg)
    lease = _wait_request_lease(
        cfg,
        kid,
        key_limit,
        global_limit,
        cancelled=cancelled,
    )
    if lease is None:
        if cancelled is not None and cancelled():
            return False, "cancelled", None
        return False, "busy", None

    # 排队期间配置可能已热改；拿到租约后再用最新文件复核。
    if cancelled is not None and cancelled():
        lease.release()
        return False, "cancelled", None
    d2 = _load(cfg)
    current2 = next(
        (k for k in d2.get("keys", []) if k.get("id") == kid and k.get("enabled", True)),
        None,
    )
    if current2 is None:
        lease.release()
        return False, "invalid_key", None
    daily_limit = _limit_value(current2, "daily_limit", 100)
    usage = d2.setdefault("usage", {}).get(kid) or {}
    if (
        daily_limit > 0
        and usage.get("date") == _today()
        and int(usage.get("count", 0)) >= daily_limit
    ):
        lease.release()
        return False, "daily_limit", None
    return True, "", lease


def _commit(cfg, keyrec: dict, n: int) -> None:
    kid = keyrec["id"]
    with _mutate_keys(cfg):
        d = _load(cfg)
        u = d.setdefault("usage", {}).get(kid) or {"date": _today(), "count": 0}
        if u.get("date") != _today():
            u = {"date": _today(), "count": 0}
        u["count"] = int(u.get("count", 0)) + int(n)
        d.setdefault("usage", {})[kid] = u
        _save(cfg, d)


# ---------------------------------------------------------------- 出图（复用 httpgen）
def _candidates(cfg, keyrec: dict | None = None):
    v = hg.get_vault(cfg)
    try:
        rows = v.load()
    except Exception:
        rows = []
    store = store_mod.Store(cfg)
    allowed_groups = set(
        _normalize_groups(keyrec.get("groups", ["free"])) if keyrec is not None else VALID_GROUPS
    )
    account_allowlist = set(_normalize_accounts(keyrec.get("accounts"))) if keyrec is not None else set()
    out = []
    for a in rows:
        if not a.get("enabled", True):
            continue        # 号池里被停用 → 不派活
        aid = a.get("id")
        group = vault.account_group(a)
        if group not in allowed_groups:
            continue
        if group in ("plus", "pro") and account_allowlist and aid not in account_allowlist:
            continue
        try:
            st = store.account(aid)
        except Exception:
            continue
        if not getattr(st, "enabled", True):
            continue        # store 里停用 → 不派活
        cd = getattr(st, "cooldown_until", 0) or 0
        if cd and cd > time.time():
            continue
        # 额度：vault.quota（管理台探测写入）优先，其次 store.image_quota；任一明确 <=0 就踢出
        vq = a.get("quota")
        sq = getattr(st, "image_quota", None)
        eff = vq if vq is not None else sq
        if eff is not None and int(eff) <= 0:
            continue        # 额度=0 → 不派活
        # 探测/登录失败的坏号（无 token 且 last_error 有值）→ 不进候选，避免每单都卡在它身上登录超时
        if not a.get("access_token") and (a.get("last_error") or ""):
            continue
        # 排序权重：有 token 且有额度最高；无 token 待登录次之；从未探测(quota=None)排最后试
        if a.get("access_token") and eff is not None:
            weight = 2000 + int(eff)
        elif a.get("access_token"):
            weight = 1500
        else:
            weight = 500        # 无 token 需要登录的，优先级最低（登录慢，别让它挡路）
        out.append((GROUP_ORDER[group], weight, aid, a))
    out.sort(key=lambda item: (item[0], -item[1]))
    return [(aid, a) for _, _, aid, a in out]


def render_once(cfg, prompt: str, timeout: float = 180.0, *,
                size: str = "", quality: str = "", images: list | None = None,
                keyrec: dict | None = None, cancel_requested=None,
                anim: bool = False):
    """从 HTTP 号池挑号出图，返回 (b64, account_id)。失败抛异常。
    size: 如 "1024x1024"/"1024x1536"/"1536x1024"；images: 参考图列表（每项为 bytes 或 data URI）。
    anim=True 出 GIF 动图（走 gpt-image-2.6 系列，输出 format=gif）；否则出 PNG。"""
    v = hg.get_vault(cfg)
    store = store_mod.Store(cfg)
    cands = _candidates(cfg, keyrec)
    if not cands:
        raise RuntimeError("没有可用的导入号（都在冷却 / 停用 / 0 额度）")
    last = ""
    # 单号出图限时：正常 22-60s，给 60s 上限（慢于 60s 基本是卡号）；卡号冷却后换下一个
    per_timeout = min(float(timeout), 60.0)
    # 参考图规范化成 image_worker 要的 dict 列表 [{b64,filename,mime_type}]
    _imgs = None
    if images:
        import base64 as _b64
        _imgs = []
        for i, im in enumerate(images, 1):
            raw = None
            mime = "image/png"
            if isinstance(im, (bytes, bytearray)):
                raw = bytes(im)
            elif isinstance(im, str) and im.startswith("data:"):
                try:
                    head, b64part = im.split(",", 1)
                    raw = _b64.b64decode(b64part)
                    mime = head.split(";")[0].replace("data:", "") or "image/png"
                except Exception:
                    raw = None
            if raw:
                _imgs.append({"b64": _b64.b64encode(raw).decode("ascii"),
                              "filename": "ref%d.png" % i, "mime_type": mime})
        if not _imgs:
            _imgs = None
    # 分散选号：每次从"当前没被别请求占用"的号里挑一个，按云端规格最多试 4 个号。
    # 高并发(如30)时不会都挤在同一个号上（同号并发会串图/失败）。
    tried = set()
    attempts = 0
    max_attempts = min(len(cands), 4)
    while attempts < max_attempts:
        if cancel_requested is not None and cancel_requested():
            break
        attempts += 1
        aid = _acquire_account(
            cands,
            tried,
            cfg=cfg,
            cancelled=cancel_requested,
        )
        if aid is None:
            if cancel_requested is not None and cancel_requested():
                break
            # 所有候选都被占用或在 tried 里 → 短暂等待后重试（并发让号池自然错开）
            if len(tried) >= len(cands):
                break
            time.sleep(0.4)
            continue
        tried.add(aid)
        a = next((x[1] for x in cands if x[0] == aid), {})
        try:
            tok = pool.ensure_token(v, aid)
            if not tok.get("ok"):
                last = "登录失败：%s" % (tok.get("error") or "")
                try:
                    store.set_cooldown(aid, 300) if hasattr(store, "set_cooldown") else None
                except Exception:
                    pass
                continue
            _model = GIF_MODEL if anim else "gpt-image-2"
            _fmt = "gif" if anim else "png"
            r = worker.run_image(tok["access_token"], prompt, model=_model,
                                 size=size or "", quality=quality or "",
                                 images=_imgs, output_format=_fmt,
                                 proxy=a.get("proxy") or "", timeout=per_timeout)
            if r.get("ok") and r.get("data"):
                b64 = r["data"][0].get("b64_json") or ""
                try:
                    st = store.account(aid)
                    q = getattr(st, "image_quota", None)
                    if q is not None:
                        st.image_quota = max(0, int(q) - 1)
                        store.save_account(st)
                except Exception:
                    pass
                return b64, str(aid)
            last = str(r.get("error") or r.get("error_code") or "出图失败")
            # 出图失败/超时 → 该号冷却 5 分钟，本单/后续单都先跳过它，避免反复卡同一个慢号
            _cool_down(cfg, store, v, aid, a, last)
        finally:
            _release_account(aid)
    raise RuntimeError(last or "出图失败")


def _cool_down(cfg, store, vault, aid, acc, reason):
    """出图失败/超时：普通失败冷却 5 分钟；若是撞额度墙（Free 套餐用完），则该号标 quota=0、
    冷却到次日（额度恢复），工作台才会显示「额度用完」而不是「可用」。
    （2026-11-15 B2：原来一律 5 分钟冷却，额度墙号 5 分钟后又被派活，反复撞墙，状态还一直显可用。）"""
    is_quota = False
    try:
        from . import why as _why
        info = _why.classify(str(reason or ""), "")
        is_quota = (getattr(info, "cls", "") == "quota")
    except Exception:
        is_quota = False
    if is_quota:
        try:
            until = time.time() + 86400   # 到明天再试（额度按天恢复）
            vault.update(aid, cooldown_until=until, quota=0,
                         last_error=("额度用完：" + str(reason))[:200])
        except Exception:
            pass
        try:
            st = store.account(aid)
            st.cooldown_until = until
            st.image_quota = 0
            store.save_account(st)
        except Exception:
            pass
        return
    try:
        until = time.time() + 300
        vault.update(aid, cooldown_until=until, last_error=("出图失败/超时：" + str(reason))[:200])
    except Exception:
        pass
    try:
        st = store.account(aid)
        st.cooldown_until = until
        store.save_account(st)
    except Exception:
        pass


# ---------------------------------------------------------------- 云端任务记账
def _job_start(
    cfg,
    keyrec,
    prompt: str,
    *,
    n: int = 1,
    size: str = "",
):
    """Create a synchronous cloud task and return its public id."""
    try:
        return task_store.create_task(
            task_store.database_path(cfg),
            key_id=str(keyrec.get("id") or ""),
            key_name=str(keyrec.get("name") or ""),
            prompt=prompt,
            n=n,
            size=size,
            status="running",
            source="sync",
        )
    except Exception as exc:  # noqa: BLE001
        print("gateway cloud task start failed: %r" % (exc,), flush=True)
        return None


def _job_finish(cfg, task_id: str | None, ok: bool, aid: str, b64s: list, err: str):
    if not task_id:
        return
    saved = 0
    if ok:
        for idx, b64_json in enumerate(b64s):
            try:
                image = cloud_api_mod.save_b64_image(cfg, task_id, idx, b64_json)
                task_store.add_task_images(
                    task_store.database_path(cfg),
                    task_id,
                    [image],
                )
                saved += 1
            except Exception as exc:  # noqa: BLE001
                print(
                    "gateway cloud task %r image %d save failed: %r"
                    % (task_id, idx, exc),
                    flush=True,
                )
    try:
        task_store.finish_task(
            task_store.database_path(cfg),
            task_id,
            ok=ok,
            account_id=str(aid or ""),
            error_code="" if ok else "GATEWAY_FAILED",
            reason="" if ok else str(err),
            image_count=len(b64s) if ok else 0,
        )
    except Exception as exc:
        print("gateway cloud task %r finish failed: %r" % (task_id, exc), flush=True)
    else:
        print(
            "gateway cloud task %r finished ok=%s account=%s images=%d/%d"
            % (task_id, ok, aid, saved, len(b64s)),
            flush=True,
        )


# ---------------------------------------------------------------- HTTP 服务
class _Handler(BaseHTTPRequestHandler):
    server_version = "BampiaoGateway"
    cfg = None
    cloud_api = None

    def log_message(self, *a):  # 安静
        pass

    def _send(self, code: int, obj: dict):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def _send_api(self, response):
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(response.body)
        except Exception:
            pass

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("bad content-length") from exc
        if length < 0:
            raise ValueError("bad content-length")
        if length > cloud_api_mod.MAX_BODY_BYTES:
            raise OverflowError("body_too_large")
        return self.rfile.read(length)

    def _auth(self):
        h = self.headers.get("Authorization", "") or ""
        if not h.lower().startswith("bearer "):
            return None
        return _find_key(_load(self.cfg), h[7:].strip())

    def do_GET(self):
        if self.cloud_api is not None:
            response = self.cloud_api.dispatch(
                "GET",
                self.path,
                self.headers,
            )
            if response is not None:
                return self._send_api(response)
        p = self.path.split("?")[0]
        if p == "/v1/models":
            return self._send(200, {"object": "list",
                                    "data": [{"id": MODEL, "object": "model", "owned_by": "bampiao"}]})
        if p in ("/", "/health"):
            return self._send(200, {"ok": True, "model": MODEL})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/v1/tasks" or p.startswith("/v1/tasks/"):
            try:
                body = self._read_body()
            except OverflowError:
                return self._send(
                    413,
                    {"error": {"message": "请求体不能超过 32MB", "code": "body_too_large"}},
                )
            except ValueError:
                return self._send(
                    400,
                    {"error": {"message": "bad content-length", "code": "bad_request"}},
                )
            response = self.cloud_api.dispatch(
                "POST",
                self.path,
                self.headers,
                body,
            )
            if response is None:
                return self._send(404, {"error": {"message": "not found"}})
            return self._send_api(response)
        if p != "/v1/images/generations":
            return self._send(404, {"error": {"message": "not found"}})
        keyrec = self._auth()
        if not keyrec:
            return self._send(401, {"error": {"message": "invalid api key"}})
        try:
            body = json.loads(self._read_body() or b"{}")
        except OverflowError:
            return self._send(
                413,
                {"error": {"message": "请求体不能超过 32MB", "code": "body_too_large"}},
            )
        except Exception:
            return self._send(400, {"error": {"message": "bad json"}})
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._send(400, {"error": {"message": "prompt required"}})
        cnt = max(1, min(4, int(body.get("n") or 1)))
        want_gif = bool(body.get("gif") or body.get("anim"))
        ok, reason, ctx = _reserve(self.cfg, keyrec)
        if not ok:
            msg = {"daily_limit": "这个 key 今天的出图额度用完了",
                   "busy": "排队超过了 15 分钟还没轮到（并发一直满），请稍后再试"}.get(reason, reason)
            return self._send(429, {"error": {"message": msg, "code": reason}})
        task_id = None
        try:
            task_id = _job_start(
                self.cfg,
                keyrec,
                prompt,
                n=cnt,
                size=str(body.get("size") or ""),
            )
            data = []
            b64s = []
            last_aid = ""
            for _ in range(cnt):
                b64, last_aid = render_once(
                    self.cfg, prompt, timeout=body.get("timeout") or 180, keyrec=keyrec,
                    anim=want_gif,
                )
                b64s.append(b64)
                data.append({"b64_json": b64, "revised_prompt": prompt})
            _commit(self.cfg, keyrec, len(data))
            _job_finish(self.cfg, task_id, True, last_aid, b64s, "")
            _resp = {"created": int(time.time()), "data": data, "model": MODEL}
            if want_gif:
                # 以实际返回数据的魔数为准：上游可能对 gif 请求仍返回 PNG。
                _first = b64s[0] if b64s else ""
                try:
                    import base64 as _b64
                    _head = _b64.b64decode(_first[:16] or "")[:6]
                except Exception:
                    _head = b""
                _resp["format"] = "gif" if _head in (b"GIF87a", b"GIF89a") else "png"
            return self._send(200, _resp)
        except Exception as e:  # noqa: BLE001
            _job_finish(self.cfg, task_id, False, "", [], str(e))
            return self._send(502, {"error": {"message": str(e)}})
        finally:
            if ctx is not None:
                try:
                    ctx.release()
                except Exception:
                    pass


def serve(cfg, host: str = "0.0.0.0", port: int = 8788, block: bool = True):
    _Handler.cfg = cfg
    try:
        recovered = task_store.recover_interrupted_tasks(
            task_store.database_path(cfg)
        )
        if recovered:
            print("云端任务库恢复：%d 条未完成任务已标记失败" % recovered, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("云端任务库恢复失败：%r" % (exc,), flush=True)
    _Handler.cloud_api = cloud_api_mod.CloudTaskAPI(
        cfg,
        authenticate=lambda token: _find_key(_load(cfg), token),
        reserve=_reserve,
        commit=_commit,
        render=render_once,
    )
    httpd = ThreadingHTTPServer((host, int(port)), _Handler)
    httpd.cloud_api = _Handler.cloud_api
    print("共享网关已启动：http://%s:%d/v1   模型=%s" % (host, int(port), MODEL), flush=True)
    if block:
        httpd.serve_forever()
    else:
        # 非阻塞：起后台线程真正开始服务，否则调用方拿到 httpd 却没人 serve
        import threading
        th = threading.Thread(target=httpd.serve_forever, daemon=True, name="bampiao-share-gw")
        th.start()
        httpd._serve_thread = th
    return httpd


# ---------------------------------------------------------------- 邀请 / 一段话
def best_host() -> str:
    """优先 Tailscale IPv4，其次本机对外网卡 IP。"""
    import socket
    import subprocess
    for exe in ("tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe",
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        try:
            r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0 and (r.stdout or "").strip():
                ip = r.stdout.strip().splitlines()[0].strip()
                if ip:
                    return ip
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def invite_paragraph(keyrec: dict, host: str, port: int = 8788,
                     download_url: str = GATEWAY_SKILL_URL) -> str:
    base = "http://%s:%d" % (host, int(port))
    key = keyrec["key"]
    name = keyrec.get("name", "")
    return "\n".join([
        "【半瓢共享网关 · 给 %s】" % name,
        "地址：%s" % base,
        "密钥：%s" % key,
        "模型：bampiao/image",
        "技能包下载（可选）：%s" % download_url,
        "",
        "把下面这段直接发给对方的 AI 即可：",
        "————————————————",
        "我要用「半瓢共享网关」出图。网关地址 %s ，密钥 %s ，模型固定 bampiao/image。" % (base, key),
        "请把要画的内容作为 prompt，POST 到 %s/v1/images/generations" % base,
        "（Header：Authorization: Bearer %s；JSON：{\"model\":\"bampiao/image\",\"prompt\":\"...\",\"n\":1,\"response_format\":\"b64_json\"}），" % key,
        "把返回的 data[0].b64_json 解码保存成 png 再给我看；出图慢给 300 秒超时，失败会返回中文原因、别反复重试。",
        "————————————————",
    ])


def invite_file(cfg, text: str, name: str) -> Path:
    d = Path(cfg.data_dir) / "exports"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (name or "friend") if c.isalnum()) or "friend"
    p = d / ("gateway-%s.md" % safe)
    p.write_text(text, encoding="utf-8")
    return p
