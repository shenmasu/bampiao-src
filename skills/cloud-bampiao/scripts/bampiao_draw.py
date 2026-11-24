#!/usr/bin/env python3
"""Zero-dependency client for the Bampiao asynchronous image task API."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://hezhuzi.com"
SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "9:16": "1024x1792",
    "16:9": "1792x1024",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
}


def _key_path() -> Path:
    return Path.home() / ".bampiao" / "key"


def _read_key(explicit: str = "") -> str:
    value = str(explicit or os.environ.get("BAMPIAO_KEY") or "").strip()
    if value:
        return value
    path = _key_path()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("未找到半瓢密钥，请先运行安装脚本或设置 BAMPIAO_KEY")


def _save_key(value: str) -> None:
    key = str(value or "").strip()
    if not key:
        raise SystemExit("密钥不能为空")
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print("密钥已保存到 %s" % path)


def _request_json(base: str, key: str, path: str, *, method: str = "GET", payload=None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"error": {"message": raw.decode("utf-8", errors="replace")[:300]}}
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error or body)
        raise RuntimeError("HTTP %d：%s" % (exc.code, message)) from exc


def _reference_uri(path_text: str) -> str:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise SystemExit("参考图不存在：%s" % path)
    size = path.stat().st_size
    if size > 10 * 1024 * 1024:
        raise SystemExit("参考图超过 10MB：%s" % path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise SystemExit("参考图格式不支持：%s" % path)
    return "data:%s;base64,%s" % (
        mime,
        base64.b64encode(path.read_bytes()).decode("ascii"),
    )


def _wait_for_task(base: str, key: str, task_id: str, timeout: float) -> dict:
    started = time.monotonic()
    last_line = ""
    while True:
        task = _request_json(base, key, "/v1/tasks/" + urllib.parse.quote(task_id))
        status = str(task.get("status") or "unknown")
        completed = int(task.get("completed_count") or 0)
        total = int(task.get("n") or 1)
        elapsed = int(time.monotonic() - started)
        stage = str(task.get("stage") or "")
        attempts = int(task.get("attempt_count") or 0)
        if status == "queued":
            queue = int(task.get("queue_position") or 1)
            line = "[半瓢] 排队第 %d 位，%ds" % (queue, elapsed)
        elif status == "running":
            line = "[半瓢] 正在生成 %d/%d" % (completed, total)
            if stage:
                line += " · " + stage
            if attempts:
                line += " · 第 %d 次尝试" % attempts
            line += " · %ds" % elapsed
        else:
            line = "[半瓢] %s" % status
        if line != last_line:
            print(line, flush=True)
            last_line = line
        if status in ("done", "failed", "cancelled"):
            return task
        if elapsed >= timeout:
            raise TimeoutError(
                "等待超时；任务可能仍在上游生成，请运行 --resume %s" % task_id
            )
        time.sleep(2.5)


def _download(base: str, key: str, url: str, target: Path) -> Path:
    if url.startswith("/"):
        url = base.rstrip("/") + url
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + key, "Accept": "image/*"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    suffix = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".png"
    if target.suffix.lower() not in (".gif", ".png"):
        target = target.with_suffix(suffix)
    elif suffix == ".gif" and target.suffix.lower() != ".gif":
        target = target.with_suffix(".gif")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.resolve()


def _download_results(base: str, key: str, task: dict, out: str) -> list[Path]:
    urls = list(task.get("image_urls") or [])
    if not urls:
        raise RuntimeError("任务已完成但没有可下载图片")
    target = Path(out or "bampiao-output").expanduser()
    saved = []
    if len(urls) == 1:
        saved.append(_download(base, key, urls[0], target))
    else:
        stem = target.with_suffix("") if target.suffix else target
        for idx, url in enumerate(urls, 1):
            saved.append(_download(base, key, url, stem.with_name(stem.name + "_%d" % idx)))
    return saved


def _check(base: str, key: str) -> int:
    request = urllib.request.Request(
        base.rstrip("/") + "/bp/me",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            me = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("自检失败：HTTP %d" % exc.code) from exc
    limit = "不限" if me.get("unlimited") else me.get("limit")
    concurrency = "不限" if me.get("concurrency_unlimited") else me.get("concurrency")
    print("半瓢连接正常｜%s｜今日 %s / %s｜并发上限 %s" % (
        me.get("name") or "已登录", me.get("used", 0), limit, concurrency,
    ))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="半瓢云端生图")
    parser.add_argument("prompt", nargs="?", default="")
    parser.add_argument("--ratio", default="1:1", choices=sorted(SIZE_BY_RATIO))
    parser.add_argument("--n", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--ref", action="append", default=[])
    parser.add_argument("--gif", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--resume", default="")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--url", default=os.environ.get("BAMPIAO_URL") or DEFAULT_URL)
    parser.add_argument("--key", default="")
    parser.add_argument("--save-key", default="")
    args = parser.parse_args(argv)
    if args.save_key:
        _save_key(args.save_key)
        return 0
    key = _read_key(args.key)
    base = args.url.rstrip("/")
    if args.check:
        return _check(base, key)
    try:
        if args.resume:
            task_id = args.resume
            print("继续任务 %s" % task_id, flush=True)
        else:
            prompt = args.prompt.strip()
            if not prompt:
                raise SystemExit("缺少提示词")
            count = 1 if args.gif else args.n
            payload = {
                "prompt": prompt,
                "n": count,
                "size": SIZE_BY_RATIO[args.ratio],
                "timeout": max(30, min(600, int(args.timeout))),
            }
            refs = [_reference_uri(path) for path in args.ref]
            if refs:
                payload["ref_images"] = refs
            if args.gif:
                payload.update({"gif": True, "anim": True, "size": "1024x1024"})
            submitted = _request_json(base, key, "/v1/tasks", method="POST", payload=payload)
            task_id = str(submitted.get("task_id") or "")
            if not task_id:
                raise RuntimeError("服务没有返回 task_id")
            print("任务已提交：%s" % task_id, flush=True)
        task = _wait_for_task(base, key, task_id, max(5.0, float(args.timeout)))
        if task.get("status") != "done":
            raise RuntimeError(str(task.get("reason") or "生成失败"))
        saved = _download_results(base, key, task, args.out)
        if task.get("partial"):
            print("部分完成：%s" % (task.get("reason") or ""), file=sys.stderr)
        print(json.dumps({
            "task_id": task_id,
            "status": task.get("status"),
            "partial": bool(task.get("partial")),
            "files": [str(path) for path in saved],
        }, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        if "task_id" in locals():
            print("已中断；稍后可用 --resume %s 继续" % task_id, file=sys.stderr)
        return 130
    except Exception as exc:
        print("半瓢出图失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
