"""Asynchronous task API for the shared cloud gateway."""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .. import why
from . import tasks

MAX_BODY_BYTES = 32 * 1024 * 1024
IMAGE_TTL_SECONDS = 7 * 24 * 60 * 60
_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_TASK_ITEM_RE = re.compile(r"^/v1/tasks/([^/]+)$")
_TASK_CANCEL_RE = re.compile(r"^/v1/tasks/([^/]+)/cancel$")
_TASK_IMAGE_RE = re.compile(r"^/v1/tasks/([^/]+)/images/(\d+)$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")

# GIF 动图占位：https://... 需 gpt-image-2.6 系列；写进 size 字段不会影响 PNG 任务（原来只存尺寸串）。
GIF_SIZE_MARKER = "gif"


def _is_png(raw: bytes) -> bool:
    return len(raw) >= 24 and raw.startswith(_PNG_SIGNATURE) and raw[12:16] == b"IHDR"


def _png_size(raw: bytes) -> tuple[int, int]:
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _gif_size(raw: bytes) -> tuple[int, int]:
    # GIF 逻辑屏幕宽高：偏移 6-7 宽、8-9 高（小端，像素）。
    return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()


def json_response(status: int, payload: Mapping) -> ApiResponse:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return ApiResponse(status, body, "application/json; charset=utf-8")


def error_response(status: int, message: str, code: str = "") -> ApiResponse:
    error = {"message": message}
    if code:
        error["code"] = code
    return json_response(status, {"error": error})


def save_b64_image(
    cfg,
    task_id: str,
    idx: int,
    b64_json: str,
    *,
    anim: bool = False,
) -> dict:
    """Decode one generated image (PNG or GIF) and return its database metadata."""
    task = tasks.get_task(tasks.database_path(cfg), task_id)
    if task is None:
        raise KeyError(task_id)
    try:
        raw = base64.b64decode(str(b64_json or ""), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("出图结果不是合法 base64") from exc

    if raw[:6] in _GIF_SIGNATURES:
        is_gif = True
        ext = "gif"
        width, height = _gif_size(raw)
    elif _is_png(raw):
        is_gif = False
        ext = "png"
        width, height = _png_size(raw)
    else:
        raise ValueError("出图结果不是有效 PNG 或 GIF")

    created = datetime.fromtimestamp(
        float(task["created_at"]), tz=timezone.utc
    ).astimezone(_SHANGHAI_TZ)
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", str(task["key_id"]))[:64] or "key"
    safe_task = re.sub(r"[^A-Za-z0-9._-]", "_", str(task_id))[:96] or "task"
    relative = (
        Path("cloud_images")
        / safe_key
        / created.strftime("%Y%m%d")
        / ("%s_%d.%s" % (safe_task, int(idx), ext))
    )
    base = Path(cfg.data_dir).resolve()
    target = (base / relative).resolve()
    if base != target and base not in target.parents:
        raise ValueError("图片路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(raw)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)
    return {
        "idx": int(idx),
        "path": relative.as_posix(),
        "width": width,
        "height": height,
        "bytes": len(raw),
        "expires_at": float(task["created_at"]) + IMAGE_TTL_SECONDS,
    }


class CloudTaskAPI:
    """Route the asynchronous task endpoints and run work off the HTTP threads."""

    def __init__(
        self,
        cfg,
        *,
        authenticate: Callable[[str], dict | None],
        reserve: Callable,
        commit: Callable,
        render: Callable,
        workers: int | None = None,
    ):
        self.cfg = cfg
        self._db = tasks.database_path(cfg)
        tasks.initialize(self._db)
        self._authenticate = authenticate
        self._reserve = reserve
        self._commit = commit
        self._render = render
        worker_count = workers or max(
            4, int(getattr(cfg, "gateway_global_concurrency", 8))
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(worker_count)),
            thread_name_prefix="bampiao-cloud-task",
        )
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._futures: dict[str, concurrent.futures.Future] = {}

    def dispatch(
        self,
        method: str,
        target: str,
        headers,
        body: bytes = b"",
    ) -> ApiResponse | None:
        parsed = urlsplit(target)
        path = parsed.path
        if not (path == "/v1/tasks" or path.startswith("/v1/tasks/")):
            return None
        if len(body) > MAX_BODY_BYTES:
            return error_response(413, "请求体不能超过 32MB", "body_too_large")

        keyrec = self._auth(headers)
        if keyrec is None:
            return error_response(401, "invalid api key", "invalid_key")

        if method == "POST" and path == "/v1/tasks":
            return self._submit(keyrec, body)
        if method == "GET" and path == "/v1/tasks":
            return self._list(keyrec, parsed.query)

        match = _TASK_ITEM_RE.match(path)
        if match and method == "GET":
            return self._detail(keyrec, match.group(1))
        match = _TASK_CANCEL_RE.match(path)
        if match and method == "POST":
            return self._cancel(keyrec, match.group(1))
        match = _TASK_IMAGE_RE.match(path)
        if match and method == "GET":
            return self._image(keyrec, match.group(1), int(match.group(2)))
        return error_response(404, "not found")

    def close(self) -> None:
        with self._lock:
            self._events.clear()
            self._futures.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _auth(self, headers) -> dict | None:
        value = str(headers.get("Authorization", "") or "")
        if not value.lower().startswith("bearer "):
            return None
        token = value[7:].strip()
        if not token:
            return None
        try:
            return self._authenticate(token)
        except Exception:
            return None

    def _submit(self, keyrec: dict, body: bytes) -> ApiResponse:
        try:
            payload = json.loads(body.decode("utf-8") if body else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_response(400, "bad json", "bad_request")
        if not isinstance(payload, dict):
            return error_response(400, "JSON 请求体必须是对象", "bad_request")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return error_response(400, "prompt required", "bad_request")
        try:
            n = _parse_n(payload.get("n", 1))
            ref_images = _parse_ref_images(payload.get("ref_images"))
            timeout = _parse_timeout(payload.get("timeout", 300))
        except ValueError as exc:
            return error_response(400, str(exc), "bad_request")
        want_gif = bool(payload.get("gif") or payload.get("anim"))

        task_id = tasks.create_task(
            self._db,
            key_id=str(keyrec["id"]),
            key_name=str(keyrec.get("name") or ""),
            prompt=prompt,
            n=n,
            size=str(payload.get("size") or "") or (GIF_SIZE_MARKER if want_gif else ""),
            source="api",
        )
        event = threading.Event()
        with self._lock:
            self._events[task_id] = event
        try:
            future = self._executor.submit(
                self._run,
                task_id,
                dict(keyrec),
                prompt,
                n,
                str(payload.get("size") or ""),
                ref_images,
                timeout,
                event,
                want_gif,
            )
        except Exception as exc:
            tasks.finish_task(
                self._db,
                task_id,
                ok=False,
                error_code="executor_unavailable",
                reason=str(exc),
            )
            with self._lock:
                self._events.pop(task_id, None)
            return error_response(503, "任务执行器暂时不可用", "executor_unavailable")
        with self._lock:
            self._futures[task_id] = future
        future.add_done_callback(
            lambda _future, ident=task_id: self._forget(ident)
        )
        return json_response(
            202,
            {
                "object": "task",
                "task_id": task_id,
                "status": "queued",
            },
        )

    def _run(
        self,
        task_id: str,
        keyrec: dict,
        prompt: str,
        n: int,
        size: str,
        ref_images: list[str],
        timeout: float,
        event: threading.Event,
        anim: bool = False,
    ) -> None:
        lease = None
        try:
            ok, reserve_reason, lease = self._reserve(
                self.cfg,
                keyrec,
                cancelled=event.is_set,
            )
            if not ok:
                if reserve_reason == "cancelled" or event.is_set():
                    return
                self._fail_reservation(task_id, reserve_reason)
                return
            if event.is_set() or not tasks.mark_running(self._db, task_id):
                return

            try:
                key_conc = int(keyrec.get("concurrency", 4))
            except (TypeError, ValueError):
                key_conc = 4
            batch_workers = min(n, key_conc if key_conc > 0 else 4, 4)
            successes: list[tuple[int, str, str]] = []
            errors: list[tuple[str, str]] = []

            def render_one(idx: int):
                def on_attempt(record):
                    record = dict(record)
                    record["img_idx"] = idx
                    tasks.update_task_progress(
                        self._db,
                        task_id,
                        stage=str(record.get("stage") or "retrying"),
                        attempt_record=record,
                    )

                return self._render(
                    self.cfg,
                    prompt,
                    timeout=timeout,
                    size=size,
                    images=ref_images or None,
                    keyrec=keyrec,
                    cancel_requested=event.is_set,
                    anim=anim,
                    total_budget=timeout,
                    on_attempt=on_attempt,
                )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, int(batch_workers)),
                thread_name_prefix="bampiao-task-image",
            ) as image_pool:
                future_map = {
                    image_pool.submit(render_one, idx): idx
                    for idx in range(n)
                }
                for future in concurrent.futures.as_completed(future_map):
                    idx = future_map[future]
                    if event.is_set():
                        continue
                    try:
                        b64_json, account_id = future.result()
                        image = save_b64_image(
                            self.cfg,
                            task_id,
                            idx,
                            b64_json,
                            anim=anim,
                        )
                        tasks.add_task_images(self._db, task_id, [image])
                        successes.append((idx, account_id, b64_json))
                        tasks.update_task_progress(
                            self._db,
                            task_id,
                            stage="generating",
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(_failure_fields(exc))

            if event.is_set():
                return
            saved = len(successes)
            if not saved:
                code, reason = errors[0] if errors else ("other", "出图失败")
                tasks.finish_task(
                    self._db,
                    task_id,
                    ok=False,
                    error_code=code,
                    reason=reason,
                )
                return
            successes.sort(key=lambda row: row[0])
            last_aid = successes[-1][1]
            self._commit(self.cfg, keyrec, saved)
            partial = saved < n
            reason = ""
            if partial:
                _code, first_reason = errors[0] if errors else ("other", "上游未返回")
                reason = "已生成 %d/%d 张，其余 %d 张未完成：%s" % (
                    saved,
                    n,
                    n - saved,
                    first_reason,
                )
            tasks.finish_task(
                self._db,
                task_id,
                ok=True,
                account_id=str(last_aid or ""),
                reason=reason,
                image_count=saved,
                partial=partial,
            )
        except Exception as exc:
            if not event.is_set():
                error_code, reason = _failure_fields(exc)
                tasks.finish_task(
                    self._db,
                    task_id,
                    ok=False,
                    error_code=error_code,
                    reason=reason,
                )
        finally:
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    pass

    def _fail_reservation(self, task_id: str, reason: str) -> None:
        code, message = {
            "daily_limit": ("daily_limit", "这个 key 今天的出图额度用完了"),
            "busy": (
                "busy",
                "排队超过了 15 分钟还没轮到（并发一直满），请稍后再试",
            ),
            "invalid_key": ("invalid_key", "密钥已停用或不存在"),
        }.get(reason, (reason or "reserve_failed", reason or "任务无法开始"))
        tasks.finish_task(
            self._db,
            task_id,
            ok=False,
            error_code=code,
            reason=message,
        )

    def _list(self, keyrec: dict, query: str) -> ApiResponse:
        try:
            values = parse_qs(query, keep_blank_values=True)
            limit = int((values.get("limit") or ["50"])[0])
            if limit < 1 or limit > 200:
                raise ValueError
            before_value = (values.get("before") or [None])[0]
            before = None if before_value in (None, "") else float(before_value)
        except (TypeError, ValueError):
            return error_response(
                400,
                "limit 必须是 1-200，before 必须是时间戳",
                "bad_request",
            )
        rows = tasks.list_tasks(
            self._db,
            key_id=str(keyrec["id"]),
            limit=limit,
            before=before,
        )
        return json_response(
            200,
            {
                "object": "list",
                "data": [self._task_view(row) for row in rows],
                "next_before": rows[-1]["created_at"] if len(rows) == limit else None,
            },
        )

    def _detail(self, keyrec: dict, task_id: str) -> ApiResponse:
        task = self._own_task(keyrec, task_id)
        if task is None:
            return error_response(404, "task not found", "not_found")
        return json_response(200, self._task_view(task))

    def _cancel(self, keyrec: dict, task_id: str) -> ApiResponse:
        result = tasks.cancel_task(
            self._db,
            task_id,
            key_id=str(keyrec["id"]),
        )
        if result is None:
            return error_response(404, "task not found", "not_found")
        with self._lock:
            event = self._events.get(task_id)
            future = self._futures.get(task_id)
        if event is not None:
            event.set()
        if future is not None:
            future.cancel()
        if not result["cancel_accepted"]:
            return error_response(
                409,
                "任务已经结束，不能再取消",
                "not_cancellable",
            )
        return json_response(
            200,
            {
                "object": "task",
                "task_id": task_id,
                "status": "cancelled",
                "cancel_accepted": True,
                "message": (
                    "正在尽力停止；已经发出的单张出图可能仍会完成"
                    if result.get("previous_status") == "running"
                    else "任务已取消"
                ),
            },
        )

    def _image(self, keyrec: dict, task_id: str, idx: int) -> ApiResponse:
        task = self._own_task(keyrec, task_id)
        if task is None:
            return error_response(404, "task not found", "not_found")
        image = next(
            (
                row
                for row in tasks.get_task_images(self._db, task_id)
                if int(row["idx"]) == int(idx)
            ),
            None,
        )
        if image is None:
            return error_response(404, "image not found", "not_found")
        now = time.time()
        if image.get("is_cleaned") or (
            image.get("expires_at") is not None
            and float(image["expires_at"]) <= now
        ):
            return error_response(410, "图片已过期（保留 7 天）", "image_expired")

        base = Path(self.cfg.data_dir).resolve()
        source = (base / str(image["path"])).resolve()
        if (
            (base != source and base not in source.parents)
            or not source.is_file()
        ):
            return error_response(404, "image not found", "not_found")
        try:
            data = source.read_bytes()
        except OSError:
            return error_response(404, "image not found", "not_found")
        # 按真实文件类型返回：GIF 与 PNG 都要正确（served 文件名/Content-Type 跟存储后缀走）。
        _suffix = source.suffix.lower()
        if _suffix == ".gif" or data[:6] in _GIF_SIGNATURES:
            _ext, _mime = "gif", "image/gif"
        else:
            _ext, _mime = "png", "image/png"
        filename = "%s_%d.%s" % (task_id, int(idx), _ext)
        max_age = max(0, int(float(image["expires_at"] or now) - now))
        return ApiResponse(
            200,
            data,
            _mime,
            (
                ("Content-Disposition", 'inline; filename="%s"' % filename),
                ("Cache-Control", "private, max-age=%d" % max_age),
                ("X-Content-Type-Options", "nosniff"),
            ),
        )

    def _own_task(self, keyrec: dict, task_id: str) -> dict | None:
        task = tasks.get_task(self._db, task_id)
        if task is None or task["key_id"] != str(keyrec["id"]):
            return None
        return task

    def _task_view(self, task: dict) -> dict:
        images = tasks.get_task_images(self._db, str(task["id"]))
        attempts = tasks.get_task_attempts(self._db, str(task["id"]))
        now = time.time()
        started = task.get("started_at")
        finished = task.get("finished_at")
        if started is None:
            elapsed = 0.0
        else:
            elapsed = max(0.0, float(finished or time.time()) - float(started))
        image_items = [
            {
                "idx": int(image["idx"]),
                "url": "/v1/tasks/%s/images/%d"
                % (task["id"], int(image["idx"])),
                "width": image.get("width"),
                "height": image.get("height"),
                "bytes": image.get("bytes"),
                "expires_at": image.get("expires_at"),
                "is_cleaned": bool(image.get("is_cleaned")),
                "kind": ("gif" if str(image.get("path") or "").lower().endswith(".gif") else "png"),
                "expired": bool(image.get("is_cleaned"))
                or (
                    image.get("expires_at") is not None
                    and float(image["expires_at"]) <= now
                ),
            }
            for image in images
        ]
        stage = str(task.get("stage") or "")
        if not stage:
            stage = "done" if task["status"] == "done" else (
                "failed" if task["status"] == "failed" else (
                    "cancelled" if task["status"] == "cancelled" else task["status"]
                )
            )
        queue_position = (
            tasks.count_queued_ahead(self._db, str(task["id"])) + 1
            if task["status"] == "queued"
            else 0
        )
        return {
            "object": "task",
            "task_id": task["id"],
            "prompt": task["prompt"],
            "n": int(task["n"]),
            "size": task["size"],
            # format 以实际落盘文件类型为准（上游对 GIF 请求可能仍返回 PNG）；
            # 没有图时才退回请求标记。
            "format": ("gif" if image_items and any(i["kind"] == "gif" for i in image_items)
                       else ("png" if image_items else
                             ("gif" if str(task["size"]) == GIF_SIZE_MARKER else "png"))),
            "status": task["status"],
            "stage": stage,
            "attempt_count": int(task.get("attempt_count") or 0),
            "attempts": attempts,
            "partial": bool(task.get("partial")),
            "queue_position": queue_position,
            "completed_count": len(images),
            "elapsed_seconds": round(elapsed, 3),
            "reason": task["reason"],
            "error_code": task["error_code"],
            "source": task["source"],
            "created_at": task["created_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "image_urls": [image["url"] for image in image_items],
            "images": image_items,
        }

    def _forget(self, task_id: str) -> None:
        with self._lock:
            self._futures.pop(task_id, None)
            self._events.pop(task_id, None)


def _parse_n(value) -> int:
    if isinstance(value, bool):
        raise ValueError("n 必须是 1-4 的整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("n 必须是 1-4 的整数") from exc
    if number < 1 or number > 4:
        raise ValueError("n 必须是 1-4 的整数")
    return number


def _parse_ref_images(value) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("ref_images 必须是 data URI 字符串数组")
    return list(value)


def _parse_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout 必须是 1-600 秒") from exc
    if timeout < 1 or timeout > 600:
        raise ValueError("timeout 必须是 1-600 秒")
    return timeout


def _failure_fields(exc: Exception) -> tuple[str, str]:
    text = str(exc or "").strip()
    info = why.classify(text, "")
    reason = info.detail
    if info.cls == "other" and text:
        reason = why.explain(text)
    return info.cls, reason
