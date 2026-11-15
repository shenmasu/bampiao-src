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


def set_enabled(cfg, ident: str, on: bool) -> int:
    """启用/停用一个分享 key。"""
    with _LOCK:
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
            continue        # 号池里被停用 → 不派活
        aid = a.get("id")
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
        out.append((weight, aid, a))
    out.sort(key=lambda x: -x[0])
    return [(aid, a) for _, aid, a in out]


def render_once(cfg, prompt: str, timeout: float = 180.0, *,
                size: str = "", quality: str = "", images: list | None = None):
    """从 HTTP 号池挑号出图，返回 (b64, account_id)。失败抛异常。
    size: 如 "1024x1024"/"1024x1536"/"1536x1024"；images: 参考图列表（每项为 bytes 或 data URI）。"""
    v = hg.get_vault(cfg)
    store = store_mod.Store(cfg)
    cands = _candidates(cfg)
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
    for aid, a in cands[:4]:
        tok = pool.ensure_token(v, aid)
        if not tok.get("ok"):
            last = "登录失败：%s" % (tok.get("error") or "")
            try:
                store.set_cooldown(aid, 300) if hasattr(store, "set_cooldown") else None
            except Exception:
                pass
            continue
        r = worker.run_image(tok["access_token"], prompt, model="gpt-image-2",
                             size=size or "", quality=quality or "",
                             images=_imgs, output_format="png",
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
            return b64, aid
        last = str(r.get("error") or r.get("error_code") or "出图失败")
        # 出图失败/超时 → 该号冷却 5 分钟，本单/后续单都先跳过它，避免反复卡同一个慢号
        _cool_down(cfg, store, v, aid, a, last)
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
            # 出图前先记下预估要用的号，便于卡住/出图中时也能看到落在哪个号
            try:
                cands = _candidates(self.cfg)
                if cands and job:
                    job.ran_on = cands[0][0] or ""
                    store.save(job)
            except Exception:
                pass
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
