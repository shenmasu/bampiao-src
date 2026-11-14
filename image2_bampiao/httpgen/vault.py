"""HTTP 号池的加密凭证库。

- 存放：<data_dir>/state/http_accounts.json（整份 JSON 用 Windows DPAPI 加密后 base64）
- DPAPI 绑定当前 Windows 用户，换机器/换用户解不开（符合"凭证不出本机"）
- 只存 HTTP 号（邮箱+密码+2FA+token），与浏览器号的 profile/登录态完全分开
- 非 Windows 平台直接抛 NotImplementedError（不做降级空操作）

对外只暴露"脱敏列表"给面板；密码/2FA/token 只在出图/登录内部取用，绝不进日志。
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from pathlib import Path

DESC = "bampiao-http-accounts"


# ------------------------------------------------------------------ DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise NotImplementedError("HTTP 号池凭证库仅支持 Windows（DPAPI）")
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _mk(buf: bytes) -> DATA_BLOB:
        b = ctypes.create_string_buffer(buf, len(buf))
        return DATA_BLOB(len(buf), ctypes.cast(b, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _mk(data)
    blob_out = DATA_BLOB()
    desc = ctypes.c_wchar_p(DESC)
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), desc, None, None, None,
                                     0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                        0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI %s 失败（错误码 %d）" % ("加密" if protect else "解密",
                                                    kernel32.GetLastError()))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _enc(data: bytes) -> str:
    return base64.b64encode(_dpapi(data, True)).decode("ascii")


def _dec(text: str) -> bytes:
    return _dpapi(base64.b64decode(text), False)


# ------------------------------------------------------------------ helpers
def account_id_for(email: str) -> str:
    norm = (email or "").strip().lower()
    return "http:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _mask_email(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return e
    name, dom = e.split("@", 1)
    if len(name) <= 2:
        return "*@" + dom
    return name[:2] + "***@" + dom


# ------------------------------------------------------------------ Vault
class Vault:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---- 存储
    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            blob = raw.get("blob") if isinstance(raw, dict) else None
            if not blob:
                return []
            data = json.loads(_dec(blob).decode("utf-8"))
            accs = data.get("accounts") if isinstance(data, dict) else None
            return [a for a in (accs or []) if isinstance(a, dict)]
        except Exception:
            # 解不开就当空（可能是换了机器/用户）；绝不猜测合并
            return []

    def _write_all(self, accounts: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # 某些机器上 state 可能是已存在的 junction/特殊目录，exist_ok 仍抛 183；目录在就行
            pass
        payload = json.dumps({"version": 1, "accounts": accounts},
                             ensure_ascii=False).encode("utf-8")
        doc = {"version": 1, "blob": _enc(payload)}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(self.path)

    # ---- 账号
    def load(self) -> list[dict]:
        return self._read_all()

    def get(self, account_id: str) -> dict | None:
        for a in self._read_all():
            if a.get("id") == account_id:
                return a
        return None

    def find_by_email(self, email: str) -> dict | None:
        norm = (email or "").strip().lower()
        for a in self._read_all():
            if (a.get("email") or "").lower() == norm:
                return a
        return None

    def upsert(self, *, email: str, password: str, totp: str = "", proxy: str = "",
               name: str = "") -> tuple[dict, bool]:
        """新增或更新一个号。返回 (账号dict, 是否新建)。"""
        accs = self._read_all()
        aid = account_id_for(email)
        now = time.time()
        for a in accs:
            if a.get("id") == aid:
                a["email"] = email
                a["password"] = password
                if totp:
                    a["totp"] = totp
                if proxy:
                    a["proxy"] = proxy
                if name:
                    a["name"] = name
                a["updated_at"] = now
                self._write_all(accs)
                return a, False
        acc = {
            "id": aid, "email": email, "name": name or _mask_email(email),
            "password": password, "totp": totp, "proxy": proxy,
            "access_token": "", "refresh_token": "", "token_exp": 0,
            "plan": "", "created_at": now, "updated_at": now,
            "last_ok_at": 0, "last_error": "", "enabled": True,
        }
        accs.append(acc)
        self._write_all(accs)
        return acc, True

    def update(self, account_id: str, **fields) -> None:
        accs = self._read_all()
        for a in accs:
            if a.get("id") == account_id:
                a.update(fields)
                a["updated_at"] = time.time()
                break
        else:
            return
        self._write_all(accs)

    def delete(self, account_id: str) -> bool:
        accs = self._read_all()
        kept = [a for a in accs if a.get("id") != account_id]
        if len(kept) == len(accs):
            return False
        self._write_all(kept)
        return True

    def set_token(self, account_id: str, access_token: str, refresh_token: str = "",
                  expires_in=None) -> None:
        exp = 0
        try:
            if expires_in:
                exp = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            exp = 0
        self.update(account_id, access_token=access_token or "",
                    refresh_token=refresh_token or "", token_exp=exp,
                    last_ok_at=time.time(), last_error="")

    # ---- 面板用：脱敏
    def public_list(self) -> list[dict]:
        out = []
        for a in self._read_all():
            out.append({
                "id": a.get("id"),
                "email": a.get("email") or "",
                "name": a.get("name") or _mask_email(a.get("email") or ""),
                "plan": a.get("plan") or "",
                "proxy": a.get("proxy") or "",
                "enabled": bool(a.get("enabled", True)),
                "has_token": bool(a.get("access_token")),
                "last_ok_at": a.get("last_ok_at") or 0,
                "last_error": a.get("last_error") or "",
                "created_at": a.get("created_at") or 0,
            })
        return out
