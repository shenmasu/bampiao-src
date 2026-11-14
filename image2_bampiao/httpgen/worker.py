"""对 vendor 里的 auth_worker / image_worker 做封装。

- 开发（源码）模式：起子进程跑 worker 脚本（隔离好、可超时强杀）。
- 打包（frozen）模式：PyInstaller exe 里 sys.executable 是半瓢自身，跑不了脚本，
  所以在**当前进程内**直接调用 vendor 的函数（image_worker.handle_payload /
  auth_worker.login_with_password_and_totp）。

约定（沿用插件协议）：
- auth:    {"action":"authenticate","email","password","totp_secret","proxy"}
           -> {"ok":true,"access_token","refresh_token","expires_in"}
- image:   {"action":"generate_image"|"check_image_quota","access_token",...}
           -> {"ok":true,"created","data":[{"b64_json","media_type"}]} 或 {"ok":false,"error_code",...}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
AUTH_WORKER = VENDOR / "auth_worker.py"
IMAGE_WORKER = VENDOR / "image_worker.py"

DEFAULT_IMAGE_TIMEOUT = 300.0
DEFAULT_AUTH_TIMEOUT = 120.0

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_FROZEN = bool(getattr(sys, "frozen", False))
_vendor_ready = False


class WorkerError(RuntimeError):
    pass


def _ensure_vendor_on_path() -> None:
    """冻结模式下把 vendor 目录加进 sys.path，好 import 里面的模块和 utils 子包。"""
    global _vendor_ready
    if _vendor_ready:
        return
    if VENDOR.is_dir() and str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    _vendor_ready = True


def python_exe() -> str:
    return os.environ.get("BAMPIAO_HTTP_PY") or sys.executable


def _kill_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


def _run_worker(script: Path, payload: dict, timeout: float) -> dict:
    """（源码模式）跑一个 worker 脚本，返回 stdout 的 JSON。"""
    if not script.exists():
        return {"ok": False, "error_code": "worker_missing", "error": "worker 不存在：%s" % script}
    py = python_exe()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="bampiao-http-") as td:
        out_f = open(os.path.join(td, "out.json"), "wb")
        err_f = open(os.path.join(td, "err.txt"), "wb")
        try:
            proc = subprocess.Popen([py, str(script)], stdin=subprocess.PIPE,
                                    stdout=out_f, stderr=err_f, cwd=str(VENDOR),
                                    creationflags=_NO_WINDOW)
        except FileNotFoundError as e:
            out_f.close(); err_f.close()
            return {"ok": False, "error_code": "python_missing", "error": "找不到解释器 %s：%s" % (py, e)}
        timed_out = False
        try:
            proc.stdin.write(data); proc.stdin.close(); proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True; _kill_tree(proc.pid)
            try: proc.wait(timeout=10)
            except Exception: pass
        except Exception as e:  # noqa: BLE001
            _kill_tree(proc.pid); out_f.close(); err_f.close()
            return {"ok": False, "error_code": "worker_spawn_error", "error": str(e)}
        finally:
            try: out_f.close()
            except Exception: pass
            try: err_f.close()
            except Exception: pass
        stdout = Path(td, "out.json").read_text(encoding="utf-8", errors="replace").strip()
        stderr = Path(td, "err.txt").read_text(encoding="utf-8", errors="replace").strip()
    if timed_out:
        return {"ok": False, "error_code": "timeout", "error": "worker 超时（%.0fs）" % timeout, "stderr_tail": stderr[-400:]}
    if not stdout:
        return {"ok": False, "error_code": "worker_no_output", "error": "worker 没有输出", "stderr_tail": stderr[-400:]}
    last = stdout.splitlines()[-1] if stdout.splitlines() else stdout
    try:
        obj = json.loads(last)
    except ValueError:
        return {"ok": False, "error_code": "worker_bad_json", "error": "worker 输出不是 JSON",
                "stdout_tail": stdout[-400:], "stderr_tail": stderr[-400:]}
    return obj if isinstance(obj, dict) else {"ok": False, "error_code": "worker_bad_json"}


# ------------------------------------------------------------------ auth
def run_auth(email: str, password: str, totp_secret: str = "",
             proxy: str = "", timeout: float = DEFAULT_AUTH_TIMEOUT) -> dict:
    if _FROZEN:
        try:
            _ensure_vendor_on_path()
            import auth_worker  # type: ignore
            v = auth_worker.login_with_password_and_totp(email, password, totp_secret, proxy=proxy)
            if not v.get("ok"):
                return {"ok": False, "error": str(v.get("error") or "auth_failed")[:200]}
            return {"ok": True, "access_token": v.get("access_token") or "",
                    "refresh_token": v.get("refresh_token") or "",
                    "expires_in": v.get("expires_in")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error_code": "auth_worker_error", "error": str(e)}
    return _run_worker(AUTH_WORKER, {
        "action": "authenticate", "email": email, "password": password,
        "totp_secret": totp_secret, "proxy": proxy}, timeout)


# ------------------------------------------------------------------ image
def _image_payload(access_token, prompt, model, size, quality, output_format,
                   background, frame_count, frame_duration_ms, images, masks,
                   proxy, upstream_model, thinking_effort) -> dict:
    payload = {
        "action": "generate_image", "access_token": access_token, "prompt": prompt,
        "model": model, "size": size or "", "quality": quality or "auto",
        "output_format": output_format or "", "background": background or "", "proxy": proxy or "",
    }
    if frame_count is not None: payload["frame_count"] = frame_count
    if frame_duration_ms is not None: payload["frame_duration_ms"] = frame_duration_ms
    if images: payload["images"] = images
    if masks: payload["masks"] = masks
    if upstream_model: payload["upstream_model"] = upstream_model
    if thinking_effort: payload["thinking_effort"] = thinking_effort
    return payload


def run_image(access_token: str, prompt: str, *, model: str = "gpt-image-2",
              size: str = "", quality: str = "", output_format: str = "",
              background: str = "", frame_count=None, frame_duration_ms=None,
              images: list | None = None, masks: list | None = None, proxy: str = "",
              upstream_model: str = "", thinking_effort: str = "",
              timeout: float = DEFAULT_IMAGE_TIMEOUT) -> dict:
    payload = _image_payload(access_token, prompt, model, size, quality, output_format,
                             background, frame_count, frame_duration_ms, images, masks,
                             proxy, upstream_model, thinking_effort)
    try:
        payload["poll_timeout_secs"] = float(min(300.0, max(1.0, timeout)))
    except Exception:
        pass
    if _FROZEN:
        try:
            _ensure_vendor_on_path()
            import image_worker  # type: ignore
            r = image_worker.handle_payload(payload)
            return r if isinstance(r, dict) else {"ok": False, "error_code": "image_worker_error", "error": "bad result"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error_code": "image_worker_error", "error": str(e)}
    return _run_worker(IMAGE_WORKER, payload, timeout)


def run_quota(access_token: str, *, proxy: str = "",
              timezone_offset_min: int = -480, timeout: float = DEFAULT_AUTH_TIMEOUT) -> dict:
    payload = {"action": "check_image_quota", "access_token": access_token,
               "proxy": proxy or "", "timezone_offset_min": timezone_offset_min}
    if _FROZEN:
        try:
            _ensure_vendor_on_path()
            import image_worker  # type: ignore
            r = image_worker.handle_payload(payload)
            return r if isinstance(r, dict) else {"ok": False, "error_code": "image_worker_error"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error_code": "image_worker_error", "error": str(e)}
    return _run_worker(IMAGE_WORKER, payload, timeout)


def health() -> dict:
    py = python_exe()
    got = ""
    ok = False
    try:
        r = subprocess.run([py, "-c", "import curl_cffi,sys;print(curl_cffi.__version__, sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        ok = r.returncode == 0
        got = (r.stdout or r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        got = str(e)
    if _FROZEN:
        try:
            import curl_cffi  # type: ignore
            ok = True; got = getattr(curl_cffi, "__version__", "?")
        except Exception as e:  # noqa: BLE001
            ok = False; got = str(e)
    return {"ok": bool(ok and (VENDOR.exists() or _FROZEN)), "python": py,
            "curl_cffi": got if ok else "", "auth_worker": AUTH_WORKER.exists(),
            "image_worker": IMAGE_WORKER.exists(), "frozen": _FROZEN}
