"""半瓢共享网关：把本机 HTTP 号池按 OpenAI 兼容接口借给局域网 / Tailscale 上的人。

设计（MVP）：
- 只借 HTTP 号池（vault 里的导入号），不碰浏览器 Plus 号。
- 只暴露 /v1/*（models、images/generations）；管理面板仍只绑 127.0.0.1，绝不对外。
- 调用方用 Authorization: Bearer <key>；每个 key 有「每日张数上限 + 并发上限」。
- 后端复用：httpgen.pool.ensure_token 取 token、httpgen.worker.run_image 出图、
  core.store 记录本地额度扣减；失败按号池顺序自动换下一个号。
"""
from __future__ import annotations

import base64  # noqa: F401  (保留给 future b64 处理)
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import store as store_mod
from .httpgen import integration as hg
from .httpgen import pool, worker

MODEL = "bampiao/image"
GATEWAY_SKILL_URL = "https://hezhuzi.com/bampiao/dl/skill/bampiao-gateway.zip"
_LOCK = threading.RLock()
_SEMS: dict = {}


# ---------------------------------------------------------------- keys 管理
def _keys_path(cfg) -> Path:
    return Path(cfg.state_dir) / "gateway_keys.json"


def _load(cfg) -> dict:
    p = _keys_path(cfg)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError
        d.setdefault("keys", [])
        d.setdefault("usage", {})
        return d
    except Exception:
        return {"keys": [], "usage": {}}


def _save(cfg, d: dict) -> None:
    p = _keys_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def add_key(cfg, name: str, daily: int = 100, concurrency: int = 3, key: str | None = None) -> dict:
    with _LOCK:
        d = _load(cfg)
        rec = {
            "id": uuid.uuid4().hex[:12],
            "name": (name or "friend").strip()[:30],
            "key": key or ("bp-" + secrets.token_hex(16)),
            "enabled": True,
            "daily_limit": int(daily),
            "concurrency": int(concurrency),
        }
        d["keys"].append(rec)
        _save(cfg, d)
        return rec


def list_keys(cfg) -> list:
    with _LOCK:
        return list(_load(cfg)["keys"])


def rm_key(cfg, ident: str) -> int:
    with _LOCK:
        d = _load(cfg)
        before = len(d["keys"])
        d["keys"] = [k for k in d["keys"]
                     if ident not in (k.get("id"), k.get("name"), k.get("key"))]
        _save(cfg, d)
        return before - len(d["keys"])


def set_limit(cfg, ident: str, daily=None, concurrency=None) -> int:
    with _LOCK:
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


def _find_key(d: dict, token: str):
    for k in d.get("keys", []):
        if k.get("enabled", True) and secrets.compare_digest(str(k.get("key", "")), token):
            return k
    return None


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _reserve(cfg, keyrec: dict):
    """每日额度 + 并发。返回 (ok, reason, semaphore_or_None)。"""
    kid = keyrec["id"]
    sem = _SEMS.get(kid)
    if sem is None:
        sem = threading.BoundedSemaphore(max(1, int(keyrec.get("concurrency") or 3)))
        _SEMS[kid] = sem
    if not sem.acquire(blocking=False):
        return False, "busy", None
    with _LOCK:
        d = _load(cfg)
        u = d.setdefault("usage", {}).get(kid) or {}
        if u.get("date") != _today():
            u = {"date": _today(), "count": 0}
        if int(u.get("count", 0)) >= int(keyrec.get("daily_limit") or 100):
            sem.release()
            return False, "daily_limit", None
        d.setdefault("usage", {})[kid] = u
        _save(cfg, d)
    return True, "", sem


def _commit(cfg, keyrec: dict, n: int) -> None:
    kid = keyrec["id"]
    with _LOCK:
        d = _load(cfg)
        u = d.setdefault("usage", {}).get(kid) or {"date": _today(), "count": 0}
        if u.get("date") != _today():
            u = {"date": _today(), "count": 0}
        u["count"] = int(u.get("count", 0)) + int(n)
        d.setdefault("usage", {})[kid] = u
        _save(cfg, d)


# ---------------------------------------------------------------- 出图（复用 httpgen）
def _candidates(cfg):
    v = hg.get_vault(cfg)
    try:
        rows = v.load()
    except Exception:
        rows = []
    store = store_mod.Store(cfg)
    out = []
    for a in rows:
        if not a.get("enabled", True):
            continue
        aid = a.get("id")
        try:
            st = store.account(aid)
        except Exception:
            continue
        if not getattr(st, "enabled", True):
            continue
        cd = getattr(st, "cooldown_until", 0) or 0
        if cd and cd > time.time():
            continue
        q = getattr(st, "image_quota", None)
        if q is not None and int(q) <= 0:
            continue
        out.append((q if q is not None else 9999, aid, a))
    out.sort(key=lambda x: -x[0])
    return [(aid, a) for _, aid, a in out]


def render_once(cfg, prompt: str, timeout: float = 180.0):
    """从 HTTP 号池挑号出图，返回 (b64, account_id)。失败抛异常。"""
    v = hg.get_vault(cfg)
    store = store_mod.Store(cfg)
    cands = _candidates(cfg)
    if not cands:
        raise RuntimeError("没有可用的导入号（都在冷却 / 停用 / 0 额度）")
    last = ""
    for aid, a in cands[:6]:
        tok = pool.ensure_token(v, aid)
        if not tok.get("ok"):
            last = "登录失败：%s" % (tok.get("error") or "")
            continue
        r = worker.run_image(tok["access_token"], prompt, model="gpt-image-2",
                             output_format="png", proxy=a.get("proxy") or "", timeout=float(timeout))
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
            return b64, aid
        last = str(r.get("error") or r.get("error_code") or "出图失败")
    raise RuntimeError(last or "出图失败")


# ---------------------------------------------------------------- 任务记账（让本机任务页能看到）
def _job_start(cfg, keyrec, prompt: str):
    try:
        from .core.models import Item, JOB_RUNNING
        store = store_mod.Store(cfg)
        item = Item(id="S01", prompt=prompt)
        job, _ = store.new_job(account_id="gw:" + str(keyrec.get("name") or keyrec.get("id")),
                               items=[item], brief=(prompt or "").strip().splitlines()[0][:60],
                               submitter="gateway", status=JOB_RUNNING, kind="image")
        from . import __version__ as _ver
        job.client_version = _ver
        store.save(job)
        return store, job
    except Exception:
        return None, None


def _job_finish(store, job, ok: bool, aid: str, b64s: list, err: str):
    if not store or not job:
        return
    # 先把状态/结果落库，保证任务页一定看得到（图片落地失败也不影响"成功/失败"）
    try:
        from .core.models import ITEM_FAILED, ITEM_OK, JOB_DONE, JOB_FAILED
        job.status = JOB_DONE if ok else JOB_FAILED
        job.finished_at = time.time()
        job.ran_on = aid or ""
        for it in job.items:
            if ok:
                it.status = ITEM_OK
            else:
                it.status = ITEM_FAILED
                it.error_code = "GATEWAY_FAILED"
                it.reason = str(err)[:200]
        if not ok:
            job.error_code = "GATEWAY_FAILED"
            job.reason = str(err)[:200]
        store.save(job)
    except Exception:
        pass
    if ok:
        try:
            d = Path(store.root) / ("%04d" % int(job.id))
            d.mkdir(parents=True, exist_ok=True)
            files = []
            for i, b64 in enumerate(b64s):
                name = "S%02d.png" % (i + 1)
                (d / name).write_bytes(base64.b64decode(b64))
                files.append(name)
            for it in job.items:
                it.files = files
            store.save(job)
        except Exception as e:  # noqa: BLE001
            try:
                job.note = ("图片落地失败：%s" % e)[:200]
                store.save(job)
            except Exception:
                pass


# ---------------------------------------------------------------- HTTP 服务
class _Handler(BaseHTTPRequestHandler):
    server_version = "BampiaoGateway"
    cfg = None

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

    def _auth(self):
        h = self.headers.get("Authorization", "") or ""
        if not h.lower().startswith("bearer "):
            return None
        return _find_key(_load(self.cfg), h[7:].strip())

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/v1/models":
            return self._send(200, {"object": "list",
                                    "data": [{"id": MODEL, "object": "model", "owned_by": "bampiao"}]})
        if p in ("/", "/health"):
            return self._send(200, {"ok": True, "model": MODEL})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/images/generations":
            return self._send(404, {"error": {"message": "not found"}})
        keyrec = self._auth()
        if not keyrec:
            return self._send(401, {"error": {"message": "invalid api key"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": {"message": "bad json"}})
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._send(400, {"error": {"message": "prompt required"}})
        cnt = max(1, min(4, int(body.get("n") or 1)))
        ok, reason, ctx = _reserve(self.cfg, keyrec)
        if not ok:
            msg = {"daily_limit": "这个 key 今天的出图额度用完了",
                   "busy": "这个 key 并发已满，稍后再试"}.get(reason, reason)
            return self._send(429, {"error": {"message": msg, "code": reason}})
        store = job = None
        try:
            store, job = _job_start(self.cfg, keyrec, prompt)
            data = []
            b64s = []
            last_aid = ""
            for _ in range(cnt):
                b64, last_aid = render_once(self.cfg, prompt, timeout=body.get("timeout") or 180)
                b64s.append(b64)
                data.append({"b64_json": b64, "revised_prompt": prompt})
            _commit(self.cfg, keyrec, len(data))
            _job_finish(store, job, True, last_aid, b64s, "")
            return self._send(200, {"created": int(time.time()), "data": data, "model": MODEL})
        except Exception as e:  # noqa: BLE001
            _job_finish(store, job, False, "", [], str(e))
            return self._send(502, {"error": {"message": str(e)}})
        finally:
            if ctx is not None:
                try:
                    ctx.release()
                except Exception:
                    pass


def serve(cfg, host: str = "0.0.0.0", port: int = 8788, block: bool = True):
    _Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, int(port)), _Handler)
    print("共享网关已启动：http://%s:%d/v1   模型=%s" % (host, int(port), MODEL), flush=True)
    if block:
        httpd.serve_forever()
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
