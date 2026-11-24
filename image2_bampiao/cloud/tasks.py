"""SQLite task ledger shared by the cloud gateway, web, and admin processes."""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
TASK_SOURCES = ("api", "sync", "web")
TERMINAL_STATUSES = ("done", "failed", "cancelled")
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def database_path(cfg) -> Path:
    """Return the cloud task database path for a Config-like object."""
    return Path(cfg.state_dir) / "cloud.db"


def usage_date(timestamp: float | None = None) -> str:
    """Return the Asia/Shanghai calendar date for a Unix timestamp."""
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(
        _SHANGHAI_TZ
    ).strftime("%Y-%m-%d")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the task database with WAL and migrate it to the current schema."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(
        str(db_path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
        return conn
    except Exception:
        conn.close()
        raise


def initialize(path: str | Path) -> int:
    """Create or migrate the database and return the resulting schema version."""
    conn = connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "cloud.db schema version %d is newer than supported version %d"
            % (version, SCHEMA_VERSION)
        )
    while version < SCHEMA_VERSION:
        with _transaction(conn):
            # Another process may have migrated while we waited for the write
            # lock, so version is deliberately re-read under BEGIN IMMEDIATE.
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                continue
            next_version = version + 1
            if next_version == 1:
                _migration_1(conn)
            elif next_version == 2:
                _migration_2(conn)
            conn.execute("PRAGMA user_version=%d" % next_version)
            version = next_version


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            key_name TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            n INTEGER NOT NULL CHECK (n BETWEEN 1 AND 4),
            size TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL
                CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
            reason TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            source TEXT NOT NULL
                CHECK (source IN ('api', 'sync', 'web'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS tasks_key_created ON tasks(key_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS tasks_status_created ON tasks(status, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_images (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            path TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            bytes INTEGER,
            expires_at REAL,
            is_cleaned INTEGER NOT NULL DEFAULT 0
                CHECK (is_cleaned IN (0, 1)),
            PRIMARY KEY (task_id, idx)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS task_images_expiry ON task_images(is_cleaned, expires_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_daily (
            key_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ok_count INTEGER NOT NULL DEFAULT 0 CHECK (ok_count >= 0),
            fail_count INTEGER NOT NULL DEFAULT 0 CHECK (fail_count >= 0),
            img_count INTEGER NOT NULL DEFAULT 0 CHECK (img_count >= 0),
            PRIMARY KEY (key_id, date)
        )
        """
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    """Add permanent feedback records without coupling them to image cleanup."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            key_name TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '',
            context_json TEXT NOT NULL DEFAULT '{}',
            image_count INTEGER NOT NULL DEFAULT 0
                CHECK (image_count BETWEEN 0 AND 9),
            status TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'seen', 'fixed', 'ignored')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS feedback_key_created
        ON feedback(key_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS feedback_status_created
        ON feedback(status, created_at DESC)
        """
    )


def create_task(
    path: str | Path,
    *,
    key_id: str,
    key_name: str = "",
    prompt: str,
    n: int = 1,
    size: str = "",
    status: str = "queued",
    reason: str = "",
    error_code: str = "",
    account_id: str = "",
    source: str = "api",
    created_at: float | None = None,
) -> str:
    """Create one task and return its short random public id."""
    key_id = str(key_id or "").strip()
    if not key_id:
        raise ValueError("key_id is required")
    if status not in TASK_STATUSES:
        raise ValueError("invalid task status: %s" % status)
    if source not in TASK_SOURCES:
        raise ValueError("invalid task source: %s" % source)
    image_count = int(n)
    if image_count < 1 or image_count > 4:
        raise ValueError("n must be between 1 and 4")

    created = time.time() if created_at is None else float(created_at)
    started = created if status == "running" else None
    task_id = secrets.token_urlsafe(12)
    conn = connect(path)
    try:
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO tasks (
                    id, key_id, key_name, prompt, n, size, status, reason,
                    error_code, account_id, created_at, started_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    key_id,
                    str(key_name or ""),
                    str(prompt or ""),
                    image_count,
                    str(size or ""),
                    status,
                    _short_text(reason),
                    _short_text(error_code),
                    str(account_id or ""),
                    created,
                    started,
                    source,
                ),
            )
        return task_id
    finally:
        conn.close()


def mark_running(
    path: str | Path,
    task_id: str,
    *,
    account_id: str = "",
    started_at: float | None = None,
) -> bool:
    """Move a queued task to running without overwriting an earlier start time."""
    started = time.time() if started_at is None else float(started_at)
    conn = connect(path)
    try:
        with _transaction(conn):
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    started_at = COALESCE(started_at, ?),
                    account_id = CASE WHEN ? = '' THEN account_id ELSE ? END
                WHERE id = ? AND status = 'queued'
                """,
                (started, str(account_id or ""), str(account_id or ""), task_id),
            )
            return cursor.rowcount > 0
    finally:
        conn.close()


def cancel_task(
    path: str | Path,
    task_id: str,
    *,
    key_id: str,
    finished_at: float | None = None,
) -> dict | None:
    """Request cancellation without allowing another key to see the task."""
    finished = time.time() if finished_at is None else float(finished_at)
    conn = connect(path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT key_id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None or row["key_id"] != str(key_id or ""):
                return None
            if row["status"] in TERMINAL_STATUSES:
                return {
                    "status": row["status"],
                    "cancel_accepted": False,
                    "previous_status": row["status"],
                }
            previous_status = row["status"]
            message = (
                "正在停止任务；已经发出的单张出图会尽快结束"
                if previous_status == "running"
                else "任务已取消"
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', finished_at = ?,
                    error_code = 'CANCELLED', reason = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (finished, message, task_id),
            )
            return {
                "status": "cancelled",
                "cancel_accepted": True,
                "previous_status": previous_status,
            }
    finally:
        conn.close()


def finish_task(
    path: str | Path,
    task_id: str,
    *,
    ok: bool,
    account_id: str = "",
    error_code: str = "",
    reason: str = "",
    image_count: int = 0,
    date: str | None = None,
    finished_at: float | None = None,
) -> bool:
    """Finish a task and update its daily usage in the same transaction.

    Repeating a finish is idempotent and will not count the task twice.
    """
    finished = time.time() if finished_at is None else float(finished_at)
    usage_day = _validated_date(date) if date else usage_date(finished)
    count = max(0, int(image_count))
    conn = connect(path)
    try:
        with _transaction(conn):
            row = conn.execute(
                "SELECT key_id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] in TERMINAL_STATUSES:
                return False
            status = "done" if ok else "failed"
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, account_id = ?,
                    error_code = ?, reason = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (
                    status,
                    finished,
                    str(account_id or ""),
                    "" if ok else _short_text(error_code or "TASK_FAILED"),
                    "" if ok else _short_text(reason),
                    task_id,
                ),
            )
            if cursor.rowcount == 0:
                return False
            if ok:
                _record_usage(
                    conn,
                    key_id=row["key_id"],
                    date=usage_day,
                    ok_delta=1,
                    img_delta=count,
                )
            else:
                _record_usage(
                    conn,
                    key_id=row["key_id"],
                    date=usage_day,
                    fail_delta=1,
                )
        return True
    finally:
        conn.close()


def get_task(path: str | Path, task_id: str) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def list_tasks(
    path: str | Path,
    *,
    key_id: str,
    limit: int = 50,
    before: float | None = None,
) -> list[dict]:
    """Return one key's latest tasks, optionally before a created-at cursor."""
    page_size = max(1, min(200, int(limit)))
    conn = connect(path)
    try:
        if before is None:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE key_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (key_id, page_size),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE key_id = ? AND created_at < ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (key_id, float(before), page_size),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def add_task_images(
    path: str | Path,
    task_id: str,
    images: Iterable[Mapping],
) -> int:
    """Insert or replace image metadata for a task."""
    rows = list(images)
    conn = connect(path)
    try:
        with _transaction(conn):
            for image in rows:
                relative_path = Path(str(image["path"]))
                if (
                    relative_path.is_absolute()
                    or not relative_path.parts
                    or ".." in relative_path.parts
                ):
                    raise ValueError("image path must be relative and stay in storage")
                conn.execute(
                    """
                    INSERT INTO task_images (
                        task_id, idx, path, width, height, bytes,
                        expires_at, is_cleaned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, idx) DO UPDATE SET
                        path = excluded.path,
                        width = excluded.width,
                        height = excluded.height,
                        bytes = excluded.bytes,
                        expires_at = excluded.expires_at,
                        is_cleaned = excluded.is_cleaned
                    """,
                    (
                        task_id,
                        int(image["idx"]),
                        relative_path.as_posix(),
                        image.get("width"),
                        image.get("height"),
                        image.get("bytes"),
                        image.get("expires_at"),
                        int(bool(image.get("is_cleaned", False))),
                    ),
                )
        return len(rows)
    finally:
        conn.close()


def get_task_images(path: str | Path, task_id: str) -> list[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM task_images WHERE task_id = ? ORDER BY idx",
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_expired_images(path: str | Path, *, before: float) -> list[dict]:
    """Return not-yet-cleaned image rows whose TTL ended before *before*."""
    conn = connect(path)
    try:
        rows = conn.execute(
            """
            SELECT task_id, idx, path, bytes, expires_at
            FROM task_images
            WHERE is_cleaned = 0
              AND expires_at IS NOT NULL
              AND expires_at < ?
            ORDER BY expires_at, task_id, idx
            """,
            (float(before),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_images_cleaned(
    path: str | Path,
    images: Iterable[Mapping],
) -> int:
    """Mark scanned expired image rows cleaned without deleting task rows."""
    rows = list(images)
    conn = connect(path)
    try:
        with _transaction(conn):
            changed = 0
            for image in rows:
                cursor = conn.execute(
                    """
                    UPDATE task_images
                    SET is_cleaned = 1
                    WHERE task_id = ?
                      AND idx = ?
                      AND path = ?
                      AND is_cleaned = 0
                    """,
                    (
                        str(image["task_id"]),
                        int(image["idx"]),
                        str(image["path"]),
                    ),
                )
                changed += cursor.rowcount
        return changed
    finally:
        conn.close()


def record_usage(
    path: str | Path,
    *,
    key_id: str,
    date: str | None = None,
    ok_delta: int = 0,
    fail_delta: int = 0,
    img_delta: int = 0,
) -> None:
    """Atomically add counters to one key's daily usage row."""
    usage_day = _validated_date(date) if date else usage_date()
    conn = connect(path)
    try:
        with _transaction(conn):
            _record_usage(
                conn,
                key_id=str(key_id or "").strip(),
                date=usage_day,
                ok_delta=ok_delta,
                fail_delta=fail_delta,
                img_delta=img_delta,
            )
    finally:
        conn.close()


def get_usage(path: str | Path, *, key_id: str, date: str | None = None) -> dict:
    usage_day = _validated_date(date) if date else usage_date()
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT key_id, date, ok_count, fail_count, img_count "
            "FROM usage_daily WHERE key_id = ? AND date = ?",
            (key_id, usage_day),
        ).fetchone()
        if row is not None:
            return dict(row)
        return {
            "key_id": key_id,
            "date": usage_day,
            "ok_count": 0,
            "fail_count": 0,
            "img_count": 0,
        }
    finally:
        conn.close()


def recover_interrupted_tasks(
    path: str | Path,
    *,
    reason: str = "服务重启，请重新提交",
    finished_at: float | None = None,
) -> int:
    """Fail queued/running tasks left by a previous service process."""
    finished = time.time() if finished_at is None else float(finished_at)
    usage_day = usage_date(finished)
    conn = connect(path)
    try:
        with _transaction(conn):
            rows = conn.execute(
                "SELECT id, key_id FROM tasks "
                "WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', finished_at = ?,
                    error_code = 'SERVICE_RESTART', reason = ?
                WHERE status IN ('queued', 'running')
                """,
                (finished, _short_text(reason)),
            )
            by_key: dict[str, int] = {}
            for row in rows:
                by_key[row["key_id"]] = by_key.get(row["key_id"], 0) + 1
            for key_id, count in by_key.items():
                _record_usage(
                    conn,
                    key_id=key_id,
                    date=usage_day,
                    fail_delta=count,
                )
            return len(rows)
    finally:
        conn.close()


def _record_usage(
    conn: sqlite3.Connection,
    *,
    key_id: str,
    date: str,
    ok_delta: int = 0,
    fail_delta: int = 0,
    img_delta: int = 0,
) -> None:
    key_id = str(key_id or "").strip()
    if not key_id:
        raise ValueError("key_id is required")
    ok_count = max(0, int(ok_delta))
    fail_count = max(0, int(fail_delta))
    img_count = max(0, int(img_delta))
    conn.execute(
        """
        INSERT INTO usage_daily (
            key_id, date, ok_count, fail_count, img_count
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key_id, date) DO UPDATE SET
            ok_count = usage_daily.ok_count + excluded.ok_count,
            fail_count = usage_daily.fail_count + excluded.fail_count,
            img_count = usage_daily.img_count + excluded.img_count
        """,
        (key_id, date, ok_count, fail_count, img_count),
    )


def _validated_date(value: str) -> str:
    try:
        return date_type.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _short_text(value) -> str:
    return str(value or "")[:1000]
