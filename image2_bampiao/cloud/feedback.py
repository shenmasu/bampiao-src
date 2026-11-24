"""Permanent user feedback storage for the shared cloud workspace."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Mapping

from .. import __version__
from . import tasks

MAX_IMAGES = 9
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 24 * 1024 * 1024
MAX_MESSAGE_CHARS = 2000
MAX_CONTACT_CHARS = 200
MAX_CONTEXT_BYTES = 16 * 1024
MINUTE_LIMIT = 3
DAY_LIMIT = 30
FEEDBACK_STATUSES = ("new", "seen", "fixed", "ignored")
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_FEEDBACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_IMAGE_NAME_RE = re.compile(r"^(\d{2})\.(webp|jpg|png|gif)$")


class FeedbackError(Exception):
    """An expected upload validation or rate-limit error."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.retry_after = retry_after


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[bytes]]:
    """Parse a multipart form without trusting file names or client image types."""
    if not str(content_type or "").lower().startswith("multipart/form-data;"):
        raise FeedbackError(400, "bad_content_type", "反馈必须使用 multipart/form-data 提交")
    if len(body) > MAX_REQUEST_BYTES:
        raise FeedbackError(413, "request_too_large", "反馈请求不能超过 24MB")
    prefix = (
        "Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % content_type
    ).encode("ascii", "ignore")
    try:
        message = BytesParser(policy=policy.default).parsebytes(prefix + body)
    except Exception as exc:
        raise FeedbackError(400, "bad_multipart", "上传内容无法解析，请刷新页面后重试") from exc
    if not message.is_multipart():
        raise FeedbackError(400, "bad_multipart", "上传内容不完整，请重新选择图片")

    fields: dict[str, str] = {}
    images: list[bytes] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = str(part.get_param("name", header="content-disposition") or "")
        payload = part.get_payload(decode=True) or b""
        if name == "images":
            images.append(payload)
            continue
        if name not in {"message", "contact", "context", "image_meta"}:
            continue
        try:
            fields[name] = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FeedbackError(400, "bad_form", "反馈文字必须使用 UTF-8") from exc
    return fields, images


def parse_client_context(value: str) -> dict:
    """Parse and cap the untrusted context hint sent by the browser."""
    if len(value.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise FeedbackError(400, "context_too_large", "页面上下文过大，请刷新页面后重试")
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise FeedbackError(400, "bad_context", "页面上下文格式不正确") from exc
    if not isinstance(parsed, dict):
        raise FeedbackError(400, "bad_context", "页面上下文格式不正确")
    viewport = parsed.get("viewport") if isinstance(parsed.get("viewport"), dict) else {}
    result = {
        "task_id": _plain_text(parsed.get("task_id"), 64),
        "page_version": _plain_text(parsed.get("page_version"), 80),
        "timezone": _plain_text(parsed.get("timezone"), 80),
        "client_time": _plain_text(parsed.get("client_time"), 80),
        "source": _plain_text(parsed.get("source"), 32),
        "viewport": {
            "width": _bounded_int(viewport.get("width"), 0, 10000),
            "height": _bounded_int(viewport.get("height"), 0, 10000),
        },
    }
    return result


def parse_image_meta(value: str, expected_count: int) -> list[dict]:
    """Read optional browser compression metadata while keeping only safe fields."""
    if not value:
        return [{} for _ in range(expected_count)]
    try:
        rows = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FeedbackError(400, "bad_image_meta", "图片压缩信息格式不正确") from exc
    if not isinstance(rows, list):
        raise FeedbackError(400, "bad_image_meta", "图片压缩信息格式不正确")
    result = []
    for row in rows[:expected_count]:
        row = row if isinstance(row, dict) else {}
        result.append(
            {
                "original_bytes": _bounded_int(
                    row.get("original_bytes"), 0, 100 * 1024 * 1024 * 1024
                ),
                "compression": _plain_text(row.get("compression"), 24),
            }
        )
    while len(result) < expected_count:
        result.append({})
    return result


def detect_image(data: bytes) -> dict:
    """Validate image magic and return a safe extension plus dimensions."""
    if not data:
        raise FeedbackError(400, "empty_image", "有一张图片是空文件")
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        if data[12:16] != b"IHDR":
            raise FeedbackError(415, "bad_image", "PNG 图片内容不完整")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        ext, mime = "png", "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_size(data)
        ext, mime = "jpg", "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        ext, mime = "gif", "image/gif"
    elif (
        len(data) >= 30
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        width, height = _webp_size(data)
        ext, mime = "webp", "image/webp"
    else:
        raise FeedbackError(
            415,
            "unsupported_image",
            "只能上传 PNG、JPEG、WebP 或 GIF 图片",
        )
    if not width or not height:
        raise FeedbackError(415, "bad_image", "图片尺寸无法识别，请换一张图片重试")
    return {
        "ext": ext,
        "mime": mime,
        "width": int(width),
        "height": int(height),
        "bytes": len(data),
    }


def create_feedback(
    cfg,
    keyrec: Mapping,
    *,
    message: str,
    contact: str,
    client_context: dict,
    images: list[bytes],
    image_meta: list[dict] | None = None,
    user_agent: str = "",
    now: float | None = None,
) -> dict:
    """Persist one feedback record and its images permanently."""
    created = time.time() if now is None else float(now)
    text = str(message or "").strip()
    if not text:
        raise FeedbackError(400, "message_required", "请写下遇到的问题或建议")
    if len(text) > MAX_MESSAGE_CHARS:
        raise FeedbackError(
            400,
            "message_too_long",
            "留言不能超过 %d 字" % MAX_MESSAGE_CHARS,
        )
    contact = str(contact or "").strip()
    if len(contact) > MAX_CONTACT_CHARS:
        raise FeedbackError(400, "contact_too_long", "联系方式不能超过 200 字")
    if len(images) > MAX_IMAGES:
        raise FeedbackError(400, "too_many_images", "最多上传 9 张图片")
    if not isinstance(client_context, dict):
        raise FeedbackError(400, "bad_context", "页面上下文格式不正确")

    detected = [detect_image(data) for data in images]
    for item in detected:
        if item["bytes"] > MAX_IMAGE_BYTES:
            raise FeedbackError(
                413,
                "image_too_large",
                "单张图片压缩后仍超过 5MB，请换一张图片重试",
            )
    if sum(item["bytes"] for item in detected) + len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise FeedbackError(413, "request_too_large", "反馈请求不能超过 24MB")

    db = tasks.database_path(cfg)
    conn = tasks.connect(db)
    feedback_id = secrets.token_urlsafe(12)
    data_dir = Path(cfg.data_dir).resolve()
    root = (data_dir / "feedback").resolve()
    _inside(data_dir, root)
    final_dir = root / feedback_id
    staging_dir = root / (".tmp-" + feedback_id)
    inserted = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_rate_limit(conn, str(keyrec["id"]), created)
        task = _owned_task(db, keyrec, client_context.get("task_id"))
        context = _build_context(
            keyrec,
            client_context,
            task,
            detected,
            image_meta or [],
            user_agent,
            created,
        )
        root.mkdir(parents=True, exist_ok=True)
        _chmod(root, 0o700)
        staging_dir.mkdir(mode=0o700)
        for index, (data, info) in enumerate(zip(images, detected), start=1):
            name = "%02d.%s" % (index, info["ext"])
            target = staging_dir / name
            target.write_bytes(data)
            _chmod(target, 0o600)
            context["images"].append(
                {
                    "name": name,
                    "content_type": info["mime"],
                    "bytes": info["bytes"],
                    "width": info["width"],
                    "height": info["height"],
                    "original_bytes": _bounded_int(
                        (image_meta or [{}] * len(images))[index - 1].get("original_bytes")
                        if index - 1 < len(image_meta or [])
                        else 0,
                        0,
                        100 * 1024 * 1024 * 1024,
                    ),
                    "compression": _plain_text(
                        (image_meta or [{}] * len(images))[index - 1].get("compression")
                        if index - 1 < len(image_meta or [])
                        else "",
                        24,
                    ),
                }
            )
        context_path = staging_dir / "context.json"
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        _chmod(context_path, 0o600)
        os.replace(staging_dir, final_dir)
        conn.execute(
            """
            INSERT INTO feedback(
                id, key_id, key_name, message, contact, context_json,
                image_count, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                feedback_id,
                str(keyrec["id"]),
                str(keyrec.get("name") or ""),
                text,
                contact,
                json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                len(images),
                created,
                created,
            ),
        )
        conn.execute("COMMIT")
        inserted = True
    except FeedbackError:
        _rollback(conn)
        raise
    except (OSError, sqlite3.Error) as exc:
        _rollback(conn)
        raise FeedbackError(500, "feedback_store_failed", "反馈暂时保存失败，请稍后重试") from exc
    finally:
        conn.close()
        if not inserted:
            _remove_path(staging_dir)
            _remove_path(final_dir)
    return {"id": feedback_id, "created_at": created, "status": "new"}


def list_feedback(
    cfg,
    *,
    key: str = "",
    status: str = "",
    since: float | None = None,
    before: float | None = None,
    limit: int = 50,
) -> list[dict]:
    """List feedback newest first for the future admin tab."""
    limit = max(1, min(200, int(limit)))
    clauses = []
    values = []
    key = str(key or "").strip()
    if key:
        clauses.append("(key_id = ? OR key_name = ?)")
        values.extend([key, key])
    status = str(status or "").strip()
    if status:
        if status not in FEEDBACK_STATUSES:
            raise ValueError("invalid feedback status")
        clauses.append("status = ?")
        values.append(status)
    if since is not None:
        clauses.append("created_at >= ?")
        values.append(float(since))
    if before is not None:
        clauses.append("created_at < ?")
        values.append(float(before))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(limit)
    conn = tasks.connect(tasks.database_path(cfg))
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM feedback%s ORDER BY created_at DESC LIMIT ?" % where,
                values,
            )
        ]
    finally:
        conn.close()


def get_feedback(cfg, feedback_id: str) -> dict | None:
    feedback_id = str(feedback_id or "").strip()
    if not _FEEDBACK_ID_RE.fullmatch(feedback_id):
        return None
    conn = tasks.connect(tasks.database_path(cfg))
    try:
        row = conn.execute(
            "SELECT * FROM feedback WHERE id = ?",
            (feedback_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_feedback_status(
    cfg,
    feedback_id: str,
    status: str,
    *,
    now: float | None = None,
) -> dict | None:
    status = str(status or "").strip()
    if status not in FEEDBACK_STATUSES:
        raise ValueError("invalid feedback status")
    record = get_feedback(cfg, feedback_id)
    if record is None:
        return None
    updated = time.time() if now is None else float(now)
    conn = tasks.connect(tasks.database_path(cfg))
    try:
        conn.execute(
            "UPDATE feedback SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated, str(feedback_id)),
        )
    finally:
        conn.close()
    record["status"] = status
    record["updated_at"] = updated
    return record


def feedback_images(cfg, feedback_id: str) -> list[Path]:
    directory = _feedback_dir(cfg, feedback_id)
    if not directory.is_dir():
        return []
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and _IMAGE_NAME_RE.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    )


def read_feedback_image(cfg, feedback_id: str, index: int) -> tuple[bytes, str] | None:
    images = feedback_images(cfg, feedback_id)
    if index < 1 or index > len(images):
        return None
    path = images[index - 1]
    mime = {
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
    }[path.suffix.lower()]
    return path.read_bytes(), mime


def _check_rate_limit(conn: sqlite3.Connection, key_id: str, now: float) -> None:
    minute_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE key_id = ? AND created_at >= ?",
            (key_id, now - 60),
        ).fetchone()[0]
    )
    if minute_count >= MINUTE_LIMIT:
        raise FeedbackError(
            429,
            "feedback_rate_limited_minute",
            "反馈提交太频繁，请等一分钟后重试",
            retry_after=60,
        )
    local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(_SHANGHAI_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    day_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE key_id = ? AND created_at >= ? AND created_at < ?",
            (key_id, start.timestamp(), end.timestamp()),
        ).fetchone()[0]
    )
    if day_count >= DAY_LIMIT:
        raise FeedbackError(
            429,
            "feedback_rate_limited_day",
            "这个密钥今天提交的反馈已达 30 条，请明天再试",
            retry_after=max(1, int(end.timestamp() - now)),
        )


def _owned_task(db, keyrec: Mapping, task_id) -> dict | None:
    task_id = _plain_text(task_id, 64)
    if not task_id:
        return None
    task = tasks.get_task(db, task_id)
    if task is None or str(task.get("key_id") or "") != str(keyrec["id"]):
        return None
    return task


def _build_context(
    keyrec: Mapping,
    client: dict,
    task: dict | None,
    images: list[dict],
    image_meta: list[dict],
    user_agent: str,
    created: float,
) -> dict:
    viewport = client.get("viewport") if isinstance(client.get("viewport"), dict) else {}
    task_context = None
    if task:
        task_context = {
            "id": str(task["id"]),
            "prompt": _plain_text(task.get("prompt"), 8000),
            "status": _plain_text(task.get("status"), 32),
            "reason": _plain_text(task.get("reason"), 4000),
            "error_code": _plain_text(task.get("error_code"), 160),
            "account": _mask_account(task.get("account_id")),
            "source": _plain_text(task.get("source"), 32),
            "timeline": {
                "created_at": task.get("created_at"),
                "started_at": task.get("started_at"),
                "finished_at": task.get("finished_at"),
            },
        }
    return {
        "schema": 1,
        "key": {
            "id": _plain_text(keyrec.get("id"), 80),
            "name": _plain_text(keyrec.get("name"), 160),
        },
        "task": task_context,
        "client": {
            "task_id": _plain_text(client.get("task_id"), 64),
            "page_version": _plain_text(client.get("page_version"), 80),
            "timezone": _plain_text(client.get("timezone"), 80),
            "client_time": _plain_text(client.get("client_time"), 80),
            "source": _plain_text(client.get("source"), 32),
            "user_agent": _plain_text(user_agent, 1000),
            "viewport": {
                "width": _bounded_int(viewport.get("width"), 0, 10000),
                "height": _bounded_int(viewport.get("height"), 0, 10000),
            },
        },
        "server": {
            "version": _plain_text(__version__, 40),
            "time": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
            "timestamp": created,
        },
        "submitted_at": created,
        "images": [],
    }


def _mask_account(value) -> str:
    text = _plain_text(value, 160)
    if not text:
        return ""
    if len(text) <= 8:
        return text[:2] + "…"
    return text[:5] + "…" + text[-4:]


def _feedback_dir(cfg, feedback_id: str) -> Path:
    feedback_id = str(feedback_id or "").strip()
    if not _FEEDBACK_ID_RE.fullmatch(feedback_id):
        raise ValueError("invalid feedback id")
    base = Path(cfg.data_dir).resolve()
    target = (base / "feedback" / feedback_id).resolve()
    _inside(base, target)
    return target


def _inside(base: Path, target: Path) -> None:
    if target != base and base not in target.parents:
        raise ValueError("path escaped data directory")


def _plain_text(value, limit: int) -> str:
    return str(value or "")[:limit]


def _bounded_int(value, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length >= 7:
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return width, height
            break
        index += segment_length
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None, None
