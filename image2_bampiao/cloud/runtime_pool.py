"""Persistent scheduling state and probe history for HTTP pool accounts."""

from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from . import tasks

RUNTIME_STATES = (
    "healthy",
    "cooling",
    "recovering",
    "suspect_dead",
    "stale",
    "disabled",
)

STATE_LABELS = {
    "healthy": "健康可用",
    "cooling": "冷却恢复",
    "recovering": "待自救",
    "suspect_dead": "疑似作废",
    "stale": "待体检",
    "disabled": "已停用",
}


@contextmanager
def _transaction(conn) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _db_path(cfg) -> Path:
    return tasks.database_path(cfg)


def _initial_state(account: dict, now: float) -> tuple[str, float]:
    if not bool(account.get("enabled", True)):
        return "disabled", 0.0
    cooldown = float(account.get("cooldown_until") or 0.0)
    try:
        quota = None if account.get("quota") is None else int(account.get("quota"))
    except (TypeError, ValueError):
        quota = None
    if cooldown > now:
        return "cooling", cooldown
    if quota is not None and quota <= 0:
        return "cooling", now + 6 * 60 * 60
    if account.get("access_token") and account.get("last_ok_at"):
        return "healthy", 0.0
    return "stale", cooldown


def sync_from_vault(cfg, vault_rows: Iterable[dict], store=None) -> int:
    """Create missing runtime rows and remove rows for deleted vault accounts."""
    del store  # Kept for call compatibility with the deployment contract.
    rows = [dict(row) for row in vault_rows if str(row.get("id") or "")]
    wanted = {str(row["id"]) for row in rows}
    now = time.time()
    conn = tasks.connect(_db_path(cfg))
    changed = 0
    try:
        with _transaction(conn):
            existing = {
                str(row["account_id"]): dict(row)
                for row in conn.execute("SELECT * FROM account_runtime").fetchall()
            }
            for account in rows:
                account_id = str(account["id"])
                current = existing.get(account_id)
                state, cooldown = _initial_state(account, now)
                quota = account.get("quota")
                if current is None:
                    conn.execute(
                        """
                        INSERT INTO account_runtime (
                            account_id, state, quota, quota_at, cooldown_until,
                            last_error, last_ok_at, last_probed_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            account_id,
                            state,
                            quota,
                            float(account.get("quota_at") or 0.0),
                            cooldown,
                            str(account.get("last_error") or "")[:300],
                            float(account.get("last_ok_at") or 0.0),
                            now,
                        ),
                    )
                    changed += 1
                    continue
                updates = []
                values = []
                if not bool(account.get("enabled", True)) and current["state"] != "disabled":
                    updates.append("state = 'disabled'")
                elif bool(account.get("enabled", True)) and current["state"] == "disabled":
                    updates.append("state = 'stale'")
                if current.get("quota") is None and quota is not None:
                    updates.extend(("quota = ?", "quota_at = ?"))
                    values.extend((quota, float(account.get("quota_at") or now)))
                if not current.get("last_error") and account.get("last_error"):
                    updates.append("last_error = ?")
                    values.append(str(account.get("last_error"))[:300])
                if updates:
                    values.append(now)
                    conn.execute(
                        "UPDATE account_runtime SET %s, updated_at = ? WHERE account_id = ?"
                        % ", ".join(updates),
                        (*values, account_id),
                    )
                    changed += 1
            stale_ids = sorted(set(existing) - wanted)
            if stale_ids:
                conn.executemany(
                    "DELETE FROM account_runtime WHERE account_id = ?",
                    ((account_id,) for account_id in stale_ids),
                )
                changed += len(stale_ids)
        return changed
    finally:
        conn.close()


def get_all(db_path: str | Path) -> dict[str, dict]:
    conn = tasks.connect(db_path)
    try:
        return {
            str(row["account_id"]): dict(row)
            for row in conn.execute("SELECT * FROM account_runtime").fetchall()
        }
    finally:
        conn.close()


def get_one(db_path: str | Path, account_id: str) -> dict | None:
    conn = tasks.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM account_runtime WHERE account_id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_for_states(db_path: str | Path, states: Iterable[str]) -> list[dict]:
    wanted = [state for state in states if state in RUNTIME_STATES]
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    conn = tasks.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM account_runtime WHERE state IN (%s) "
            "ORDER BY health_score DESC, last_used_at ASC" % marks,
            tuple(wanted),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_used(db_path: str | Path, account_id: str, *, at: float | None = None) -> None:
    now = time.time() if at is None else float(at)
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_runtime SET last_used_at = ?, updated_at = ? "
            "WHERE account_id = ?",
            (now, now, account_id),
        )
    finally:
        conn.close()


def set_state(
    db_path: str | Path,
    account_id: str,
    state: str,
    *,
    cooldown_until: float | None = None,
) -> None:
    if state not in RUNTIME_STATES:
        raise ValueError("invalid runtime state")
    now = time.time()
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_runtime SET state = ?, cooldown_until = COALESCE(?, cooldown_until), "
            "updated_at = ? WHERE account_id = ?",
            (state, cooldown_until, now, account_id),
        )
    finally:
        conn.close()


def record_success(
    db_path: str | Path,
    account_id: str,
    *,
    latency_s: float,
    at: float | None = None,
) -> None:
    now = time.time() if at is None else float(at)
    latency = max(0.0, float(latency_s))
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT * FROM account_runtime WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                return
            quota = row["quota"]
            quota = None if quota is None else max(0, int(quota) - 1)
            avg = float(row["avg_latency"] or 0.0)
            avg = latency if avg <= 0 else 0.7 * avg + 0.3 * latency
            health = min(100.0, float(row["health_score"] or 70.0) + (5.0 if latency <= 70 else 2.0))
            state = "cooling" if quota == 0 else "healthy"
            cooldown = now + 6 * 60 * 60 if quota == 0 else 0.0
            conn.execute(
                """
                UPDATE account_runtime
                SET quota = ?, quota_at = ?, state = ?, cooldown_until = ?,
                    fail_streak = 0, relogin_fail_streak = 0,
                    ok_total = ok_total + 1, avg_latency = ?,
                    health_score = ?, last_used_at = ?, last_ok_at = ?,
                    last_probed_at = ?, last_error_class = '', last_error = '',
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    quota, now, state, cooldown, avg, health,
                    now, now, now, now, account_id,
                ),
            )
    finally:
        conn.close()


def record_failure(
    db_path: str | Path,
    account_id: str,
    *,
    err_class: str,
    err_msg: str,
    cooldown_secs: float,
    has_totp: bool,
    at: float | None = None,
) -> None:
    now = time.time() if at is None else float(at)
    cls = str(err_class or "other").strip().lower()
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT * FROM account_runtime WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                return
            fail_streak = int(row["fail_streak"] or 0)
            health = float(row["health_score"] or 70.0)
            state = str(row["state"] or "stale")
            cooldown = float(cooldown_secs or 0.0)
            if cls == "auth_401":
                state = "recovering" if has_totp else "suspect_dead"
                health = max(0.0, health - 25.0)
                fail_streak += 1
            elif cls == "quota":
                state = "cooling"
                cooldown = min(24 * 60 * 60, max(300.0, cooldown or 6 * 60 * 60))
                health = max(0.0, health - 5.0)
            elif cls in ("network", "handshake_timeout"):
                state = "cooling"
                cooldown = min(300.0, max(20.0, cooldown or 30.0))
                health = max(0.0, health - 3.0)
            elif cls == "timeout":
                state = "cooling"
                fail_streak += 1
                cooldown = min(3600.0, max(300.0, cooldown or 300.0 * (2 ** max(0, fail_streak - 1))))
                health = max(0.0, health - 18.0)
            else:
                state = "cooling"
                fail_streak += 1
                cooldown = min(1800.0, max(60.0, cooldown or 180.0))
                health = max(0.0, health - 10.0)
            conn.execute(
                """
                UPDATE account_runtime
                SET state = ?, cooldown_until = ?, fail_streak = ?,
                    fail_total = fail_total + 1, health_score = ?,
                    last_used_at = ?, last_error_class = ?, last_error = ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    state, now + cooldown, fail_streak, health, now,
                    cls, str(err_msg or "")[:300], now, account_id,
                ),
            )
    finally:
        conn.close()


def record_policy_neutral(db_path: str | Path, account_id: str) -> None:
    """Keep account health unchanged for a content-policy refusal."""
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_runtime SET last_used_at = ?, updated_at = ? "
            "WHERE account_id = ?",
            (time.time(), time.time(), account_id),
        )
    finally:
        conn.close()


def clear_token_state(db_path: str | Path, account_id: str, *, has_totp: bool) -> None:
    state = "recovering" if has_totp else "suspect_dead"
    now = time.time()
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_runtime SET state = ?, updated_at = ? WHERE account_id = ?",
            (state, now, account_id),
        )
    finally:
        conn.close()


def record_probe_result(
    db_path: str | Path,
    account_id: str,
    *,
    ok: bool,
    quota=None,
    err_class: str = "",
    err_msg: str = "",
    has_totp: bool = False,
    restore_at_ts: float = 0.0,
    at: float | None = None,
) -> None:
    now = time.time() if at is None else float(at)
    cls = str(err_class or "").strip().lower()
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            if ok:
                try:
                    quota_value = None if quota is None else max(0, int(quota))
                except (TypeError, ValueError):
                    quota_value = None
                if quota_value == 0:
                    state = "cooling"
                    cooldown = float(restore_at_ts or (now + 4 * 60 * 60))
                else:
                    state = "healthy"
                    cooldown = 0.0
                conn.execute(
                    """
                    UPDATE account_runtime
                    SET state = ?, quota = ?, quota_at = ?, cooldown_until = ?,
                        fail_streak = 0, relogin_fail_streak = 0,
                        last_probed_at = ?, last_error_class = '', last_error = '',
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (state, quota_value, now, cooldown, now, now, account_id),
                )
                return
            if cls in ("auth", "auth_401"):
                state = "recovering" if has_totp else "suspect_dead"
                cooldown = 0.0
            elif cls == "network":
                state = "cooling"
                cooldown = now + 10 * 60
            elif cls == "quota":
                state = "cooling"
                cooldown = float(restore_at_ts or (now + 4 * 60 * 60))
            else:
                state = "stale"
                cooldown = 0.0
            conn.execute(
                """
                UPDATE account_runtime
                SET state = ?, cooldown_until = ?, quota = CASE WHEN ? = 'quota' THEN 0 ELSE quota END,
                    fail_streak = fail_streak + 1, fail_total = fail_total + 1,
                    last_probed_at = ?, last_error_class = ?, last_error = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (state, cooldown, cls, now, cls, str(err_msg or "")[:300], now, account_id),
            )
    finally:
        conn.close()


def reset_relogin_streak(db_path: str | Path, account_id: str) -> None:
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_runtime SET relogin_fail_streak = 0, updated_at = ? "
            "WHERE account_id = ?",
            (time.time(), account_id),
        )
    finally:
        conn.close()


def fail_relogin(db_path: str | Path, account_id: str) -> int:
    now = time.time()
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT relogin_fail_streak FROM account_runtime WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                return 0
            count = int(row["relogin_fail_streak"] or 0) + 1
            state = "suspect_dead" if count >= 3 else "recovering"
            conn.execute(
                "UPDATE account_runtime SET relogin_fail_streak = ?, state = ?, updated_at = ? "
                "WHERE account_id = ?",
                (count, state, now, account_id),
            )
            return count
    finally:
        conn.close()


def delete_many(db_path: str | Path, account_ids: Iterable[str]) -> int:
    wanted = [str(value) for value in account_ids if str(value)]
    if not wanted:
        return 0
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            before = conn.total_changes
            conn.executemany(
                "DELETE FROM account_runtime WHERE account_id = ?",
                ((account_id,) for account_id in wanted),
            )
            return conn.total_changes - before
    finally:
        conn.close()


def create_probe_run(db_path: str | Path, mode: str, total: int) -> str:
    run_id = secrets.token_urlsafe(10)
    now = time.time()
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO probe_runs (
                id, mode, status, total, started_at, log_json
            ) VALUES (?, ?, 'running', ?, ?, '[]')
            """,
            (run_id, str(mode or "manual")[:40], max(0, int(total)), now),
        )
        return run_id
    finally:
        conn.close()


def update_probe_run(
    db_path: str | Path,
    run_id: str,
    *,
    current_account: str | None = None,
    account_result: dict | None = None,
    mode: str | None = None,
    total: int | None = None,
) -> None:
    now = time.time()
    conn = tasks.connect(db_path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT log_json FROM probe_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return
            log = _load_log(row["log_json"])
            if account_result:
                log.append(_sanitize_probe_log(account_result))
                log = log[-250:]
                cls = str(account_result.get("error_class") or "")
                ok = bool(account_result.get("ok"))
                quota_zero = bool(account_result.get("quota_zero"))
                conn.execute(
                    """
                    UPDATE probe_runs
                    SET done = done + 1, ok_count = ok_count + ?,
                        fail_count = fail_count + ?, auth_fail_count = auth_fail_count + ?,
                        quota_zero_count = quota_zero_count + ?,
                        network_fail_count = network_fail_count + ?,
                        current_account = ?, log_json = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        int(ok), int(not ok), int(cls in ("auth", "auth_401")),
                        int(quota_zero), int(cls == "network"),
                        str(current_account or "")[:80],
                        json.dumps(log, ensure_ascii=False, separators=(",", ":")),
                        run_id,
                    ),
                )
                return
            sets = ["current_account = ?"]
            values = [str(current_account or "")[:80]]
            if mode is not None:
                sets.append("mode = ?")
                values.append(str(mode)[:40])
            if total is not None:
                sets.append("total = ?")
                values.append(max(0, int(total)))
            conn.execute(
                "UPDATE probe_runs SET %s WHERE id = ? AND status = 'running'" % ", ".join(sets),
                (*values, run_id),
            )
    finally:
        conn.close()


def finish_probe_run(
    db_path: str | Path,
    run_id: str,
    *,
    status: str = "done",
) -> None:
    if status not in ("done", "stopped", "failed"):
        raise ValueError("invalid probe run status")
    conn = tasks.connect(db_path)
    try:
        conn.execute(
            "UPDATE probe_runs SET status = ?, finished_at = ?, current_account = '' "
            "WHERE id = ?",
            (status, time.time(), run_id),
        )
    finally:
        conn.close()


def latest_probe_run(
    db_path: str | Path,
    *,
    modes: Iterable[str] | None = None,
    exclude_modes: Iterable[str] | None = None,
) -> dict | None:
    where = []
    values: list[str] = []
    if modes is not None:
        wanted = [str(mode) for mode in modes]
        if not wanted:
            return None
        where.append("mode IN (%s)" % ",".join("?" for _ in wanted))
        values.extend(wanted)
    if exclude_modes is not None:
        excluded = [str(mode) for mode in exclude_modes]
        if excluded:
            where.append("mode NOT IN (%s)" % ",".join("?" for _ in excluded))
            values.extend(excluded)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = tasks.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM probe_runs%s ORDER BY started_at DESC LIMIT 1" % clause,
            tuple(values),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["log"] = _load_log(result.pop("log_json", "[]"))
        return result
    finally:
        conn.close()


def _load_log(value) -> list[dict]:
    try:
        rows = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)][-250:]


def _sanitize_probe_log(record: dict) -> dict:
    allowed = (
        "account_id", "stage", "ok", "error_class", "error_code",
        "error", "quota_zero", "elapsed_s", "ts",
    )
    out = {}
    for key in allowed:
        if key not in record:
            continue
        value = record[key]
        out[key] = str(value)[:300] if isinstance(value, str) else value
    return out
