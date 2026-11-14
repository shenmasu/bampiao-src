"""号池文本解析 + 批量导入 + token 保养。

文本格式（每行一条，分隔符 3 个及以上连续的 `-`，最多切 3 刀）：
    邮箱----密码----2FA密钥
    邮箱----密码                    （无 2FA 也行，但上游要求验证时会失败）
    邮箱----密码----2FA----邮箱链接/Token   （第 4 段兼容，不强制用）

与喜鹊插件 lib/credentials.mjs 的解析口径保持一致，避免同样的号两家判定不同。
"""
from __future__ import annotations

import re
import time

from . import worker
from .vault import Vault, account_id_for, _mask_email

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")
_SEP = re.compile(r"-{3,}")


def _split(line: str) -> list[str]:
    fields, start = [], 0
    for m in _SEP.finditer(line):
        if len(fields) >= 3:
            break
        fields.append(line[start:m.start()].strip())
        start = m.end()
    fields.append(line[start:].strip())
    return fields


def parse_text(text: str) -> tuple[list[dict], list[dict]]:
    """返回 (有效账号, 错误)。错误项 {line, reason}。"""
    ok, errs = [], []
    seen = set()
    for i, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        cols = _split(line)
        if len(cols) < 2:
            errs.append({"line": i, "reason": "至少要有 邮箱----密码"})
            continue
        email = cols[0].strip().lower()
        password = cols[1]
        totp = (cols[2] if len(cols) >= 3 else "").replace(" ", "").replace("-", "").upper()
        if not EMAIL_RE.match(email):
            errs.append({"line": i, "reason": "邮箱格式不对"})
            continue
        if not password or len(password) > 512:
            errs.append({"line": i, "reason": "密码为空或太长"})
            continue
        if totp and (not BASE32_RE.match(totp) or len(totp.rstrip("=")) < 16):
            errs.append({"line": i, "reason": "2FA 不是合法 Base32"})
            continue
        if email in seen:
            continue
        seen.add(email)
        ok.append({"email": email, "password": password, "totp": totp, "line": i})
    return ok, errs


def import_text(vault: Vault, text: str, *, proxy: str = "",
                name_prefix: str = "") -> dict:
    """把文本批量写进凭证库（只存，不登录）。返回汇总。"""
    ok, errs = parse_text(text)
    added = updated = 0
    for a in ok:
        existing = vault.find_by_email(a["email"])
        _acc, created = vault.upsert(
            email=a["email"], password=a["password"], totp=a["totp"],
            proxy=proxy, name=(name_prefix + _mask_email(a["email"])) if name_prefix else "",
        )
        if created:
            added += 1
        else:
            updated += 1
    return {"ok": True, "added": added, "updated": updated,
            "invalid": errs, "total_valid": len(ok),
            "total_lines": added + updated + len(errs)}


def ensure_token(vault: Vault, account_id: str, *, force: bool = False,
                 skew: int = 120) -> dict:
    """确保这个号有可用 access_token；必要时用存着的密码+2FA 重新登录。

    返回 {"ok":bool, "access_token":str, "error":str}
    """
    acc = vault.get(account_id)
    if not acc:
        return {"ok": False, "error": "没有这个号"}
    tok = acc.get("access_token") or ""
    exp = int(acc.get("token_exp") or 0)
    expired = exp and exp <= int(time.time()) + skew
    if tok and not expired and not force:
        return {"ok": True, "access_token": tok}
    r = worker.run_auth(acc.get("email") or "", acc.get("password") or "",
                        acc.get("totp") or "", proxy=acc.get("proxy") or "")
    if not r.get("ok"):
        vault.update(account_id, last_error="登录失败：%s" % (r.get("error") or "unknown"))
        return {"ok": False, "error": r.get("error") or "auth_failed"}
    vault.set_token(account_id, r.get("access_token") or "",
                    r.get("refresh_token") or "", r.get("expires_in"))
    return {"ok": True, "access_token": r.get("access_token") or ""}


def refresh_quota(vault: Vault, account_id: str) -> dict:
    """查剩余生图额度并写回 vault。"""
    e = ensure_token(vault, account_id)
    if not e.get("ok"):
        return e
    acc = vault.get(account_id) or {}
    r = worker.run_quota(e["access_token"], proxy=acc.get("proxy") or "")
    if r.get("ok"):
        vault.update(account_id, quota=r.get("remaining", r.get("quota")), quota_at=time.time())
    return r
