"""半瓢共享网关：把本机 HTTP 号池按 OpenAI 兼容接口借给局域网 / Tailscale 上的人。

设计（MVP）：
- 只借 HTTP 号池（vault 里的导入号），不碰浏览器 Plus 号。
- 只暴露 /v1/*（models、images/generations）；管理面板仍只绑 127.0.0.1，绝不对外。
- 调用方用 Authorization: Bearer <key>；每个 key 有「每日张数上限 + 并发上限」。
- 后端复用：httpgen.pool.ensure_token 取 token、httpgen.worker.run_image 出图、
  core.store 记录本地额度扣减；失败按号池顺序自动换下一个号。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod, locks, why
from .cloud import api as cloud_api_mod, pool_monitor, runtime_pool, tasks as task_store
from .core import store as store_mod
from .httpgen import integration as hg
from .httpgen import pool, vault, worker

MODEL = "bampiao/image"
# GIF 动图走 gpt-image-2.6 系列（与 httpgen.integration.GIF_MODEL 同一模型）；PNG 仍用 gpt-image-2。
GIF_MODEL = "gpt-image-2.6"
GATEWAY_SKILL_URL = "https://hezhuzi.com/bp/skill/bampiao-skill.zip"
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


class PolicyViolationError(RuntimeError):
    """The prompt was refused upstream; retrying with another account is wasteful."""


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


def _acquire_account(
    cands,
    skip: set,
    cfg=None,
    cancelled=None,
    wait_timeout: float | None = None,
) -> str | None:
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
    deadline = time.monotonic() + max(
        0.0,
        float(QUEUE_WAIT if wait_timeout is None else wait_timeout),
    )
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
        int(getattr(cfg, "gateway_global_concurrency", 16)),
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


def add_key(cfg, name: str, daily: int = 100, concurrency: int = 4, key: str | None = None,
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


def upgrade_enabled_key_concurrency(cfg, *, target: int = 4) -> int:
    """Raise legacy enabled-key limits to the v3 default; unlimited stays unlimited."""
    wanted = max(1, int(target))
    changed = 0
    with _mutate_keys(cfg):
        data = _load(cfg)
        for keyrec in data.get("keys", []):
            if not keyrec.get("enabled", True):
                continue
            try:
                current = int(keyrec.get("concurrency", 0))
            except (TypeError, ValueError):
                current = 0
            if 0 < current < wanted:
                keyrec["concurrency"] = wanted
                changed += 1
        if changed:
            _save(cfg, data)
    return changed


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

    key_limit = _limit_value(current, "concurrency", 4)
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
    db_path = task_store.database_path(cfg)
    try:
        runtime_pool.sync_from_vault(cfg, rows)
    except Exception:
        pass
    runtime = runtime_pool.get_all(db_path)
    store = store_mod.Store(cfg)
    allowed_groups = set(
        _normalize_groups(keyrec.get("groups", ["free"])) if keyrec is not None else VALID_GROUPS
    )
    account_allowlist = set(_normalize_accounts(keyrec.get("accounts"))) if keyrec is not None else set()
    now = time.time()
    tiers: dict[int, list[tuple]] = {0: [], 1: [], 2: []}
    for a in rows:
        if not a.get("enabled", True):
            continue
        aid = a.get("id")
        group = vault.account_group(a)
        if group not in allowed_groups:
            continue
        if group in ("plus", "pro") and account_allowlist and aid not in account_allowlist:
            continue
        live = runtime.get(str(aid))
        if live is None:
            continue
        if live.get("state") in ("suspect_dead", "recovering", "disabled"):
            continue
        if float(live.get("cooldown_until") or 0) > now:
            continue
        if not a.get("access_token"):
            continue
        try:
            st = store.account(aid)
        except Exception:
            continue
        if not getattr(st, "enabled", True):
            continue
        cd = getattr(st, "cooldown_until", 0) or 0
        if cd and cd > now:
            continue
        # Runtime quota is authoritative after successful renders; static vault/store
        # values are only fallbacks for rows created before schema v3.
        vq = live.get("quota")
        if vq is None:
            vq = a.get("quota")
        sq = getattr(st, "image_quota", None)
        eff = vq if vq is not None else sq
        try:
            if eff is not None and int(eff) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        health = float(live.get("health_score") or 70.0)
        fail_streak = int(live.get("fail_streak") or 0)
        state = str(live.get("state") or "stale")
        if health >= 55 and fail_streak == 0:
            tier = 0
        elif state in ("healthy", "stale", "cooling"):
            tier = 1
        else:
            tier = 2
        last_used = float(live.get("last_used_at") or 0.0)
        # A tiny jitter prevents concurrent requests from all choosing the same
        # handful of long-idle accounts while preserving least-recently-used order.
        spread = last_used + random.uniform(-10.0, 10.0)
        tiers[tier].append((GROUP_ORDER[group], spread, -health, aid, a))

    out = []
    for tier in (0, 1, 2):
        tiers[tier].sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        out.extend(tiers[tier])
    return [(aid, a) for _, _, _, aid, a in out]


def _classify_worker_failure(result: dict) -> dict:
    code = str(result.get("error_code") or "").strip().lower()
    status = int(result.get("http_status") or 0)
    text = str(result.get("error") or "")
    low = text.lower()
    try:
        why_cls = why.classify(text, code).cls
    except Exception:
        why_cls = "other"
    if code == "content_policy_violation" or why_cls == "policy":
        return {
            "error_class": "policy", "error_code": code or "content_policy_violation",
            "error": "提示词触发了内容政策，请调整描述后重试",
            "fast": False, "cooldown": 0.0,
        }
    if status == 401 or code in ("unauthorized", "token_invalidated", "token_revoked") or any(
        key in low for key in ("http 401", "unauthorized", "token_invalidated", "token_revoked")
    ):
        return {
            "error_class": "auth_401", "error_code": code or "unauthorized",
            "error": "账号登录状态失效", "fast": True, "cooldown": 0.0,
        }
    if code == "rate_limited" or why_cls == "quota":
        return {
            "error_class": "quota", "error_code": code or "rate_limited",
            "error": "这个账号的生成额度已用完", "fast": True,
            "cooldown": _cooldown_from_result(result, 6 * 60 * 60),
        }
    if code in ("handshake_timeout", "network_error") or why_cls == "network" or any(
        key in low for key in (
            "boringssl", "ssl_error", "curl: (35)", "curl: (56)",
            "connection closed", "connection reset", "proxy",
        )
    ):
        return {
            "error_class": "network", "error_code": code or "network_error",
            "error": "网络连接中断，已自动切换通道", "fast": True,
            "cooldown": 30.0,
        }
    if code in ("timeout", "stream_timeout"):
        return {
            "error_class": "timeout", "error_code": code,
            "error": "本次生成超时，已自动切换通道", "fast": False,
            "cooldown": 300.0,
        }
    return {
        "error_class": "other", "error_code": code or "upstream_error",
        "error": why.explain(text, "") if text else "这次出图没有完成",
        "fast": False, "cooldown": 180.0,
    }


def _cooldown_from_result(result: dict, default: float) -> float:
    raw = result.get("retry_after") or result.get("restore_at") or result.get("reset_after")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return min(24 * 60 * 60, max(60.0, float(raw)))
    text = str(raw or "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(h|m|s|小时|分钟|秒)", text, re.I)
    if not match:
        return float(default)
    value = float(match.group(1))
    unit = match.group(2).lower()
    factor = 3600.0 if unit in ("h", "小时") else 60.0 if unit in ("m", "分钟") else 1.0
    return min(24 * 60 * 60, max(60.0, value * factor))


def _record_account_failure(cfg, store, aid: str, account: dict, failure: dict) -> None:
    cls = str(failure.get("error_class") or "other")
    message = str(failure.get("error") or "出图失败")
    cooldown = float(failure.get("cooldown") or 0.0)
    db_path = task_store.database_path(cfg)
    if cls == "policy":
        try:
            runtime_pool.record_policy_neutral(db_path, aid)
        except Exception:
            pass
        return
    try:
        v = hg.get_vault(cfg)
        if cls == "auth_401":
            v.update(
                aid,
                access_token="",
                refresh_token="",
                last_error=message[:200],
            )
        elif cls == "quota":
            v.update(
                aid,
                quota=0,
                cooldown_until=time.time() + cooldown,
                last_error=message[:200],
            )
        else:
            v.update(
                aid,
                cooldown_until=time.time() + cooldown,
                last_error=message[:200],
            )
    except Exception:
        pass
    try:
        runtime_pool.record_failure(
            db_path,
            aid,
            err_class=cls,
            err_msg=message,
            cooldown_secs=cooldown,
            has_totp=bool(account.get("totp")),
        )
    except Exception:
        pass
    try:
        state = store.account(aid)
        state.cooldown_until = time.time() + max(1.0, cooldown or 30.0)
        if cls == "quota" and hasattr(state, "image_quota"):
            state.image_quota = 0
        store.save_account(state)
    except Exception:
        pass


def _emit_attempt(callback, attempt: int, aid: str, started_at: float, result: dict) -> None:
    if callback is None:
        return
    ok = bool(result.get("ok"))
    record = {
        "attempt": int(attempt),
        "account_id": str(aid),
        "stage": "generating" if ok else "retrying",
        "ok": ok,
        "error_class": str(result.get("error_class") or ""),
        "error_code": str(result.get("error_code") or ""),
        "error": "" if ok else str(result.get("error") or ""),
        "elapsed_s": round(max(0.0, time.monotonic() - started_at), 3),
        "ts": time.time(),
    }
    try:
        callback(record)
    except Exception:
        pass


def render_once(
    cfg,
    prompt: str,
    timeout: float = 180.0,
    *,
    size: str = "",
    quality: str = "",
    images: list | None = None,
    keyrec: dict | None = None,
    cancel_requested=None,
    anim: bool = False,
    total_budget: float | None = None,
    handshake_timeout: float = 18.0,
    render_timeout: float = 115.0,
    on_attempt=None,
):
    """从 HTTP 号池挑号出图，返回 (b64, account_id)。失败抛异常。
    size: 如 "1024x1024"/"1024x1536"/"1536x1024"；images: 参考图列表（每项为 bytes 或 data URI）。
    anim=True 出 GIF 动图（走 gpt-image-2.6 系列，输出 format=gif）；否则出 PNG。"""
    store = store_mod.Store(cfg)
    cands = _candidates(cfg, keyrec)
    if not cands:
        raise RuntimeError("没有可用的导入号（都在冷却 / 停用 / 0 额度）")
    last = ""
    budget = float(total_budget if total_budget is not None else timeout or 300.0)
    budget = min(600.0, max(30.0, budget))
    deadline = time.monotonic() + budget
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
    # Fast failures rotate immediately; slow generation failures get a smaller
    # budget so a dead route cannot consume the whole user request.
    tried = set()
    attempts = 0
    fast_failures = 0
    slow_failures = 0
    while attempts < 10:
        if cancel_requested is not None and cancel_requested():
            break
        remaining = deadline - time.monotonic()
        if remaining < 20.0 and attempts > 0:
            break
        attempts += 1
        aid = _acquire_account(
            cands,
            tried,
            cfg=cfg,
            cancelled=cancel_requested,
            wait_timeout=max(1.0, remaining - 2.0),
        )
        if aid is None:
            if cancel_requested is not None and cancel_requested():
                break
            if len(tried) >= len(cands):
                break
            if deadline - time.monotonic() < 20.0:
                break
            time.sleep(0.2)
            continue
        tried.add(aid)
        try:
            runtime_pool.mark_used(task_store.database_path(cfg), str(aid))
        except Exception:
            pass
        a = next((x[1] for x in cands if x[0] == aid), {})
        started_at = time.monotonic()
        try:
            remaining = deadline - time.monotonic()
            poll_timeout = min(
                float(render_timeout),
                max(20.0, remaining - 5.0),
            )
            hs_timeout = min(
                float(handshake_timeout),
                max(8.0, remaining - 5.0),
            )
            tok = pool.ensure_token(hg.get_vault(cfg), aid, allow_login=False)
            if not tok.get("ok"):
                last = "登录失败：%s" % (tok.get("error") or "")
                failure = {"error_class": "auth_401", "error": "账号登录状态失效"}
                _record_account_failure(cfg, store, str(aid), a, failure)
                fast_failures += 1
                _emit_attempt(on_attempt, attempts, aid, started_at, failure)
                if fast_failures >= 8:
                    break
                continue
            _model = GIF_MODEL if anim else "gpt-image-2"
            _fmt = "gif" if anim else "png"
            r = worker.run_image(tok["access_token"], prompt, model=_model,
                                 size=size or "", quality=quality or "",
                                 images=_imgs, output_format=_fmt,
                                 proxy=a.get("proxy") or "", timeout=poll_timeout,
                                 handshake_timeout=hs_timeout)
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
                try:
                    runtime_pool.record_success(
                        task_store.database_path(cfg),
                        str(aid),
                        latency_s=time.monotonic() - started_at,
                    )
                except Exception:
                    pass
                _emit_attempt(on_attempt, attempts, aid, started_at, {"ok": True})
                return b64, str(aid)
            failure = _classify_worker_failure(r)
            last = failure["error"]
            _record_account_failure(cfg, store, str(aid), a, failure)
            _emit_attempt(on_attempt, attempts, aid, started_at, failure)
            if failure["error_class"] == "policy":
                raise PolicyViolationError("提示词触发了内容政策，换账号也不会改变结果，请调整描述")
            if failure["fast"]:
                fast_failures += 1
            else:
                slow_failures += 1
            if fast_failures >= 8 or slow_failures >= 3:
                break
        except PolicyViolationError:
            raise
        except Exception as exc:
            failure = _classify_worker_failure({"error": str(exc)})
            last = failure["error"]
            _record_account_failure(cfg, store, str(aid), a, failure)
            _emit_attempt(on_attempt, attempts, aid, started_at, failure)
            if failure["fast"]:
                fast_failures += 1
            else:
                slow_failures += 1
            if fast_failures >= 8 or slow_failures >= 3:
                break
        finally:
            _release_account(aid)
    if last:
        raise RuntimeError(last)
    raise RuntimeError("没有可用的导入号（都在冷却 / 停用 / 0 额度）")


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
        task_id = task_store.create_task(
            task_store.database_path(cfg),
            key_id=str(keyrec.get("id") or ""),
            key_name=str(keyrec.get("name") or ""),
            prompt=prompt,
            n=n,
            size=size,
            status="running",
            source="sync",
        )
        task_store.update_task_progress(
            task_store.database_path(cfg),
            task_id,
            stage="connecting",
        )
        return task_id
    except Exception as exc:  # noqa: BLE001
        print("gateway cloud task start failed: %r" % (exc,), flush=True)
        return None


def _job_finish(
    cfg,
    task_id: str | None,
    ok: bool,
    aid: str,
    b64s: list,
    err: str,
    *,
    partial: bool = False,
    error_code: str = "GATEWAY_FAILED",
):
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
            error_code="" if ok else error_code,
            reason=str(err) if (not ok or partial) else "",
            image_count=len(b64s) if ok else 0,
            partial=partial,
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
        try:
            cnt = cloud_api_mod._parse_n(body.get("n", 1))
            ref_images = cloud_api_mod._parse_ref_images(body.get("ref_images"))
            request_timeout = cloud_api_mod._parse_timeout(body.get("timeout", 240))
        except ValueError as exc:
            return self._send(400, {"error": {"message": str(exc), "code": "bad_request"}})
        want_gif = bool(body.get("gif") or body.get("anim"))
        requested_size = str(body.get("size") or "")
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
                size=requested_size,
            )
            last_aid = ""
            successes = []
            errors = []
            key_conc = _limit_value(keyrec, "concurrency", 4)
            batch_workers = min(cnt, key_conc if key_conc > 0 else 4, 4)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, int(batch_workers)),
                thread_name_prefix="bampiao-sync-image",
            ) as pool_executor:
                def render_index(idx: int):
                    def on_attempt(record):
                        record = dict(record)
                        record["img_idx"] = idx
                        if task_id:
                            task_store.update_task_progress(
                                task_store.database_path(self.cfg),
                                task_id,
                                stage=str(record.get("stage") or "retrying"),
                                attempt_record=record,
                            )

                    return render_once(
                        self.cfg,
                        prompt,
                        timeout=request_timeout,
                        keyrec=keyrec,
                        size=requested_size,
                        images=ref_images or None,
                        anim=want_gif,
                        total_budget=min(240.0, request_timeout),
                        on_attempt=on_attempt,
                    )

                futures = {
                    pool_executor.submit(render_index, idx): idx
                    for idx in range(cnt)
                }
                for future in concurrent.futures.as_completed(futures):
                    idx = futures[future]
                    try:
                        b64, account_id = future.result()
                        successes.append((idx, b64, account_id))
                        last_aid = account_id
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc) or "单张出图失败")
            successes.sort(key=lambda row: row[0])
            b64s = [row[1] for row in successes]
            data = [
                {"b64_json": b64, "revised_prompt": prompt}
                for b64 in b64s
            ]
            if not data:
                raise RuntimeError(errors[0] if errors else "出图失败")
            _commit(self.cfg, keyrec, len(data))
            partial = len(data) < cnt
            partial_reason = ""
            if partial:
                partial_reason = "已生成 %d/%d 张，其余未完成：%s" % (
                    len(data), cnt, errors[0] if errors else "上游未返回",
                )
            _job_finish(
                self.cfg,
                task_id,
                True,
                last_aid,
                b64s,
                partial_reason,
                partial=partial,
            )
            _resp = {"created": int(time.time()), "data": data, "model": MODEL}
            if partial:
                _resp.update({
                    "partial": True,
                    "requested": cnt,
                    "completed": len(data),
                    "reason": partial_reason,
                })
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
            error_code, reason = cloud_api_mod._failure_fields(e)
            _job_finish(
                self.cfg,
                task_id,
                False,
                "",
                [],
                reason,
                error_code=error_code,
            )
            return self._send(502, {
                "error": {"message": reason, "code": error_code}
            })
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
    pool_monitor.start_pool_monitor(cfg)
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
        "AI 技能包：%s" % download_url,
        "",
        "把下面这段直接发给对方的 AI 即可：",
        "————————————————",
        "请帮我永久安装「半瓢云端生图」技能：下载 %s，安装到全局 skills/bampiao；" % download_url,
        "把密钥 %s 保存到 ~/.bampiao/key；运行 scripts/bampiao_draw.py --check。" % key,
        "装好后以后我说“用半瓢生图”就直接调用。开发者兼容地址：%s/v1" % base,
        "————————————————",
    ])


def invite_file(cfg, text: str, name: str) -> Path:
    d = Path(cfg.data_dir) / "exports"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (name or "friend") if c.isalnum()) or "friend"
    p = d / ("gateway-%s.md" % safe)
    p.write_text(text, encoding="utf-8")
    return p
