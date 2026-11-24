"""Background HTTP account probes and recovery for the cloud gateway."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from ..httpgen import integration as hg
from ..httpgen import pool
from . import runtime_pool, tasks

_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _db_path(cfg):
    return tasks.database_path(cfg)


def _select_account_ids(cfg, only: str, account_ids=None) -> list[str]:
    v = hg.get_vault(cfg)
    rows = v.load()
    runtime_pool.sync_from_vault(cfg, rows)
    live = runtime_pool.get_all(_db_path(cfg))
    requested = None if account_ids is None else {str(value) for value in account_ids}
    selected = []
    now = time.time()
    for account in rows:
        account_id = str(account.get("id") or "")
        if not account_id or (requested is not None and account_id not in requested):
            continue
        state = str((live.get(account_id) or {}).get("state") or "stale")
        mode = str(only or "all")
        if mode in ("stale_failed", "unprobed_failed"):
            row = live.get(account_id) or {}
            if state not in ("stale", "cooling") and not row.get("last_error"):
                continue
        elif mode == "wake_cooling" and state != "cooling":
            continue
        elif mode == "wake_cooling" and float((live.get(account_id) or {}).get("cooldown_until") or 0) > now:
            continue
        elif mode == "suspect_dead" and state != "suspect_dead":
            continue
        if state == "disabled":
            continue
        selected.append(account_id)
    return selected


def run_probe_batch(
    cfg,
    mode: str = "manual",
    *,
    account_ids=None,
    only: str = "all",
    workers: int = 5,
    stop_event: threading.Event | None = None,
) -> dict:
    """Probe accounts concurrently and persist every progress step."""
    v = hg.get_vault(cfg)
    all_rows = {str(row.get("id")): row for row in v.load() if row.get("id")}
    runtime_pool.sync_from_vault(cfg, all_rows.values())
    selected = _select_account_ids(cfg, only, account_ids)
    run_id = runtime_pool.create_probe_run(_db_path(cfg), mode, len(selected))
    results: list[dict] = []

    def probe_one(account_id: str) -> dict:
        started = time.monotonic()
        account = all_rows.get(account_id) or {}
        if stop_event is not None and stop_event.is_set():
            return {"account_id": account_id, "stage": "stopped", "ok": False,
                    "error_class": "stopped", "error": "扫描已停止"}
        runtime_pool.update_probe_run(_db_path(cfg), run_id, current_account=account_id)
        token_result = pool.ensure_token(v, account_id, force=not bool(account.get("access_token")))
        if not token_result.get("ok"):
            message = str(token_result.get("error") or "账号登录状态失效")
            cls = "network" if "network" in message.lower() or "timeout" in message.lower() else "auth"
            runtime_pool.record_probe_result(
                _db_path(cfg), account_id, ok=False, err_class=cls,
                err_msg=message, has_totp=bool(account.get("totp")),
            )
            result = {
                "account_id": account_id, "stage": "auth" if cls == "auth" else "network",
                "ok": False, "error_class": cls, "error_code": cls,
                "error": "账号需要重新登录" if cls == "auth" else "网络连接异常",
                "quota_zero": False, "elapsed_s": round(time.monotonic() - started, 3),
                "ts": time.time(),
            }
            runtime_pool.update_probe_run(_db_path(cfg), run_id, account_result=result)
            return result

        quota_result = pool.worker.run_quota(
            str(token_result.get("access_token") or ""),
            proxy=str(account.get("proxy") or ""),
            timeout=20,
        )
        if quota_result.get("ok"):
            quota = quota_result.get("remaining", quota_result.get("quota"))
            try:
                quota_int = None if quota is None else max(0, int(quota))
            except (TypeError, ValueError):
                quota_int = None
            restore_at = _restore_timestamp(quota_result.get("restore_at"))
            if quota_int is not None:
                try:
                    v.update(account_id, quota=quota_int, quota_at=time.time())
                except Exception:
                    pass
            plan_type = str(quota_result.get("plan_type") or "").strip().lower()
            if plan_type:
                try:
                    v.update(account_id, plan=plan_type)
                except Exception:
                    pass
            runtime_pool.record_probe_result(
                _db_path(cfg), account_id, ok=True, quota=quota_int,
                restore_at_ts=restore_at, has_totp=bool(account.get("totp")),
            )
            result = {
                "account_id": account_id, "stage": "done", "ok": True,
                "error_class": "", "error_code": "", "error": "",
                "quota_zero": quota_int == 0,
                "quota": quota_int,
                "elapsed_s": round(time.monotonic() - started, 3), "ts": time.time(),
            }
        else:
            message = str(quota_result.get("error") or "额度检查失败")
            cls = _probe_error_class(quota_result)
            runtime_pool.record_probe_result(
                _db_path(cfg), account_id, ok=False, err_class=cls,
                err_msg=message, has_totp=bool(account.get("totp")),
                restore_at_ts=_restore_timestamp(quota_result.get("retry_after")),
            )
            if cls in ("auth", "auth_401"):
                try:
                    v.update(account_id, access_token="", refresh_token="")
                except Exception:
                    pass
            result = {
                "account_id": account_id, "stage": cls, "ok": False,
                "error_class": cls, "error_code": str(quota_result.get("error_code") or cls),
                "error": _probe_error_text(cls), "quota_zero": cls == "quota",
                "elapsed_s": round(time.monotonic() - started, 3), "ts": time.time(),
            }
        runtime_pool.update_probe_run(_db_path(cfg), run_id, account_result=result)
        return result

    status = "done"
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(8, int(workers))),
            thread_name_prefix="bampiao-pool-probe",
        ) as executor:
            future_map = {executor.submit(probe_one, account_id): account_id for account_id in selected}
            for future in concurrent.futures.as_completed(future_map):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    account_id = future_map[future]
                    result = {
                        "account_id": account_id, "stage": "failed", "ok": False,
                        "error_class": "other", "error_code": "probe_failed",
                        "error": "扫描暂时失败", "quota_zero": False,
                        "elapsed_s": 0.0, "ts": time.time(),
                    }
                    runtime_pool.update_probe_run(_db_path(cfg), run_id, account_result=result)
                    results.append(result)
                if stop_event is not None and stop_event.is_set():
                    status = "stopped"
    except Exception:
        status = "failed"
        raise
    finally:
        runtime_pool.finish_probe_run(_db_path(cfg), run_id, status=status)
    latest = runtime_pool.latest_probe_run(_db_path(cfg)) or {}
    latest["results"] = results
    return latest


def _probe_error_class(result: dict) -> str:
    code = str(result.get("error_code") or "").lower()
    text = str(result.get("error") or "").lower()
    if result.get("http_status") == 401 or code == "unauthorized" or "401" in text:
        return "auth"
    if code == "rate_limited" or "额度" in text or "usage limit" in text:
        return "quota"
    if any(word in text for word in ("ssl", "timeout", "curl", "connection", "网络")):
        return "network"
    return "other"


def _probe_error_text(cls: str) -> str:
    return {
        "auth": "账号需要重新登录",
        "auth_401": "账号需要重新登录",
        "quota": "账号生成额度为 0",
        "network": "网络连接异常",
    }.get(cls, "扫描暂时失败")


def _restore_timestamp(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if number > time.time() else time.time() + max(0.0, number)
    return 0.0


def _process_recovery(cfg) -> bool:
    v = hg.get_vault(cfg)
    rows = {str(row.get("id")): row for row in v.load() if row.get("id")}
    runtime_pool.sync_from_vault(cfg, rows.values())
    live = runtime_pool.get_all(_db_path(cfg))
    candidates = []
    for account_id, row in live.items():
        account = rows.get(account_id) or {}
        if (
            row.get("state") == "recovering"
            and account.get("totp")
            and int(row.get("relogin_fail_streak") or 0) < 3
        ):
            candidates.append((float(row.get("updated_at") or 0), account_id, account))
    if not candidates:
        return False
    _updated, account_id, account = min(candidates, key=lambda item: (item[0], item[1]))
    result = pool.ensure_token(v, account_id, force=True)
    if not result.get("ok"):
        runtime_pool.fail_relogin(_db_path(cfg), account_id)
        return True
    quota_result = pool.worker.run_quota(
        str(result.get("access_token") or ""),
        proxy=str(account.get("proxy") or ""),
        timeout=20,
    )
    runtime_pool.reset_relogin_streak(_db_path(cfg), account_id)
    if quota_result.get("ok"):
        runtime_pool.record_probe_result(
            _db_path(cfg),
            account_id,
            ok=True,
            quota=quota_result.get("remaining", quota_result.get("quota")),
            restore_at_ts=_restore_timestamp(quota_result.get("restore_at")),
            has_totp=True,
        )
    else:
        runtime_pool.record_probe_result(
            _db_path(cfg),
            account_id,
            ok=False,
            err_class=_probe_error_class(quota_result),
            err_msg=str(quota_result.get("error") or "额度检查失败"),
            has_totp=True,
        )
    return True


def monitor_loop(cfg, stop_event: threading.Event) -> None:
    next_wake = 0.0
    next_recover = 0.0
    last_daily = ""
    while not stop_event.wait(1.0):
        now = time.monotonic()
        if now >= next_wake:
            try:
                run_probe_batch(
                    cfg,
                    "wake_cooling",
                    only="wake_cooling",
                    workers=4,
                    stop_event=stop_event,
                )
            except Exception:
                pass
            next_wake = time.monotonic() + 300.0
        if now >= next_recover:
            try:
                _process_recovery(cfg)
            except Exception:
                pass
            next_recover = time.monotonic() + 30.0
        shanghai = datetime.now(tz=_SHANGHAI_TZ)
        day = shanghai.strftime("%Y-%m-%d")
        if shanghai.hour >= 4 and last_daily != day:
            try:
                all_rows = hg.get_vault(cfg).load()
                runtime_pool.sync_from_vault(cfg, all_rows)
                live = runtime_pool.get_all(_db_path(cfg))
                cutoff = time.time() - 86400
                stale_ids = [
                    account_id
                    for account_id, row in live.items()
                    if row.get("state") in ("healthy", "stale")
                    and max(float(row.get("last_ok_at") or 0), float(row.get("last_probed_at") or 0)) < cutoff
                ]
                run_probe_batch(
                    cfg,
                    "daily_auto",
                    account_ids=stale_ids,
                    workers=5,
                    stop_event=stop_event,
                )
                last_daily = day
            except Exception:
                pass


def start_pool_monitor(cfg) -> threading.Thread | None:
    if os.environ.get("BAMPIAO_DISABLE_POOL_MONITOR") == "1":
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=monitor_loop,
        args=(cfg, stop_event),
        daemon=True,
        name="bampiao-pool-monitor",
    )
    thread.start()
    return thread
