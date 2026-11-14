"""httpgen 与半瓢主体之间的集成层（最小侵入）。

设计原则：
- **不改 cfg.accounts 的语义**：浏览器号仍在 cfg.accounts；HTTP 号单独在 vault 里，
  通过 http_accounts(cfg) 按需合成成 Account(kind='http')，只喂给"选号/出图/展示"这三处。
- 现有 CDP 主链路一行不变；只有 acc.kind == 'http' 时才走 HTTP 出图。
- 总开关 cfg.http_pool_enabled（config [http_pool] enabled）关着时 http_accounts 返回空，
  行为与未安装本功能完全一致。
"""
from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path

from . import worker
from .pool import ensure_token
from .vault import Vault

DEFAULT_MODEL = "gpt-image-2"
GIF_MODEL = "gpt-image-2.6"


def vault_path(cfg) -> Path:
    return Path(cfg.data_dir) / "state" / "http_accounts.json"


def get_vault(cfg) -> Vault:
    return Vault(vault_path(cfg))


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "http_pool_enabled", False))


def http_accounts(cfg) -> list:
    """把 vault 里的号合成成 Account(kind='http')，供选号/展示。开关关着返回 []。"""
    if not enabled(cfg):
        return []
    try:
        from ..config import Account
    except Exception:
        return []
    out = []
    try:
        rows = get_vault(cfg).load()
    except Exception:
        return []
    for a in rows:
        if not a.get("enabled", True):
            continue
        out.append(Account(id=str(a.get("id") or ""),
                           name=str(a.get("name") or a.get("email") or "HTTP号"),
                           browser="http", profile=None, port=0, kind="http"))
    return out


def find_account(cfg, account_id: str):
    for a in http_accounts(cfg):
        if a.id == account_id:
            return a
    return None


def _refs_to_images(refs):
    out = []
    for r in refs or []:
        p = Path(str(r)).expanduser()
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        out.append({"b64": base64.b64encode(raw).decode("ascii"),
                    "filename": p.name, "mime_type": mime})
    return out


def _save_output(data_item: dict, out: Path) -> int:
    b64 = data_item.get("b64_json") or ""
    raw = base64.b64decode(b64) if b64 else b""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return len(raw)


_KIND_BY_CODE = {
    "unauthorized": "auth",
    "rate_limited": "quota",
    "timeout": "timeout",
}


def generate(cfg, acc, prompt: str, out: Path, *, refs=None, aspect: str = "",
             alpha: bool = False, timeout: float | None = None, title: str = "",
             anim: bool = False, svg: bool = False, svg_anim: bool = False,
             item=None) -> dict:
    """HTTP 出图，返回与 driver.session.generate 兼容的 dict。"""
    if svg or svg_anim:
        return {"ok": False, "kind": "render", "rc": 1, "log": "",
                "tail": "HTTP 号池不支持 SVG，请用浏览器号或去掉 --svg",
                "reason": "HTTP 号池不支持 SVG 矢量输出"}

    # 快速前置检查（用已探测的 AccountState）：停用 / 冷却中 / 剩 0 额度 →
    # 立刻返回，别连上游白等（否则上游不快速回 429 会一直 pending 到超时，误判 TIMEOUT_UNCERTAIN）。
    try:
        from ..core import store as _store_mod
        _st = _store_mod.Store(cfg).account(acc.id)
        if getattr(_st, "enabled", True) is False:
            return {"ok": False, "kind": "disabled", "rc": 1, "log": "", "error_code": "DISABLED",
                    "tail": "这个导入号已停用，不参与出图", "reason": "导入号已停用"}
        _cd = float(getattr(_st, "cooldown_until", 0) or 0)
        if _cd > time.time():
            _m = max(1, int((_cd - time.time()) // 60))
            return {"ok": False, "kind": "quota", "rc": 1, "log": "", "error_code": "QUOTA_COOLDOWN",
                    "tail": "额度冷却中，约 %d 分钟后恢复" % _m, "reason": "额度冷却中"}
        _q = getattr(_st, "image_quota", None)
        if _q is not None and int(_q) <= 0:
            return {"ok": False, "kind": "quota", "rc": 1, "log": "", "error_code": "QUOTA_COOLDOWN",
                    "tail": "这个号剩余额度为 0，先换号或等恢复", "reason": "剩余额度为 0"}
    except Exception:
        pass

    v = get_vault(cfg)
    tok = ensure_token(v, acc.id)
    if not tok.get("ok"):
        return {"ok": False, "kind": "auth", "rc": 1, "log": "",
                "tail": "登录失败：%s" % (tok.get("error") or ""),
                "reason": "HTTP 号登录失败，需要重新登录或换号"}

    model = GIF_MODEL if anim else DEFAULT_MODEL
    fmt = "gif" if anim else ("webp" if str(out).lower().endswith(".webp")
                              else ("jpeg" if str(out).lower().endswith((".jpg", ".jpeg"))
                                    else "png"))
    imgs = _refs_to_images(refs) or None
    tmo = float(timeout or worker.DEFAULT_IMAGE_TIMEOUT)
    r = worker.run_image(tok["access_token"], prompt, model=model,
                         output_format=fmt, images=imgs,
                         proxy=(v.get(acc.id) or {}).get("proxy") or "",
                         timeout=tmo)
    if not r.get("ok"):
        code = str(r.get("error_code") or "")
        kind = _KIND_BY_CODE.get(code, "render")
        msg = str(r.get("error") or code or "出图失败")
        # 额度类错误交给上层冷却
        return {"ok": False, "kind": kind, "rc": 1, "log": "", "tail": msg,
                "reason": msg, "error_code": code}
    data = r.get("data") or []
    if not data:
        return {"ok": False, "kind": "render", "rc": 1, "log": "",
                "tail": "上游没返回图片", "reason": "上游没返回图片"}
    nbytes = _save_output(data[0], out)
    size = ""
    frames = 1
    animated = False
    try:
        from .. import media as _media
        p = _media.probe(str(out))
        size = p.get("size") or ""
        frames = int(p.get("frames") or 1)
        animated = bool(p.get("animated"))
    except Exception:
        size = "%d KB" % max(1, nbytes // 1024)
    return {"ok": True, "kind": "ok", "rc": 0, "path": str(out), "size": size,
            "frames": frames, "animated": animated, "svg": False,
            "note": "", "tail": "", "log": ""}
