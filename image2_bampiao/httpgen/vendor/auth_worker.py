"""Standalone DSH-style Platform HTTP auth worker for Magpie GPTFree.

Adapted from yang0/chatgpt2api-dsh/scripts/auth-worker.py (user-requested).
Uses the original HTTP authorization/password/TOTP/token flow, not Playwright
or Codex CLI. The login protocol is OpenAI Platform OAuth, not Codex OAuth.

The bundled utils/sentinel.py and utils/pkce.py are MIT-licensed code from
basketikun/chatgpt2api; source and copyright are recorded in THIRD_PARTY.md.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# Resolve only the helpers bundled with GPTFree. No DSH installation,
# service directory, or environment is consulted.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from curl_cffi import requests
except ImportError:
    requests = None

try:
    from utils.pkce import generate_pkce
except ImportError:
    generate_pkce = None

try:
    from utils.sentinel import build_sentinel_token
except ImportError:
    build_sentinel_token = None




def parse_jwt_payload(t: str) -> dict:
    """Safely decode JWT payload JSON without signature verification."""
    try:
        parts = str(t or "").split(".")
        if len(parts) != 3:
            return {}
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return {}


def get_totp(secret: str) -> str:
    """Calculate current 6-digit TOTP code from Base32 secret."""
    secret = secret.replace(" ", "").upper()
    secret += "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret)
    counter = int(time.time() // 30)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def resolve_proxy(custom_proxy: str = "") -> str:
    """Resolve proxy like the DSH worker: explicit value then environment/system."""
    if custom_proxy and str(custom_proxy).strip():
        return str(custom_proxy).strip()
    for env_key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    # Check if common local proxy ports are listening
    import socket
    for port in (7890, 7897, 10809, 10808):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return ""


def login_with_password_and_totp(email: str, password: str, totp_secret: str = "", proxy: str = "") -> dict:
    """
    Log in to OpenAI Platform OAuth via email, password, and optional TOTP 2FA.
    Returns token dict on success or error dict on failure.
    """
    if not requests or not generate_pkce or not build_sentinel_token:
        return {"ok": False, "error": "dependencies_missing", "detail": "curl_cffi or utils modules not found"}

    auth_base = "https://auth.openai.com"
    platform_oauth_audience = "https://api.openai.com/v1"
    platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
    platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
    platform_oauth_redirect_uri = "https://platform.openai.com/auth/callback"
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

    session_kwargs = {"impersonate": "chrome110", "verify": False}
    proxy_url = resolve_proxy(proxy)
    if proxy_url:
        session_kwargs["proxy"] = proxy_url

    session = requests.Session(**session_kwargs)
    device_id = str(uuid.uuid4())
    code_verifier, code_challenge = generate_pkce()

    session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
    session.cookies.set("oai-did", device_id, domain="auth.openai.com")

    # 1. Authorize
    params = {
        "issuer": auth_base,
        "client_id": platform_oauth_client_id,
        "audience": platform_oauth_audience,
        "redirect_uri": platform_oauth_redirect_uri,
        "device_id": device_id,
        "screen_hint": "login_or_signup",
        "max_age": "0",
        "login_hint": email,
        "scope": "openid profile email offline_access",
        "response_type": "code",
        "response_mode": "query",
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": platform_auth0_client,
    }
    authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
    try:
        resp = session.get(authorize_url, headers={"user-agent": user_agent}, allow_redirects=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"authorize_network_error: {str(e)}"}

    if resp.status_code not in (200, 302):
        return {"ok": False, "error": f"authorize_failed_{resp.status_code}"}

    # 2. Password verify
    login_headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json",
        "origin": auth_base,
        "priority": "u=1, i",
        "user-agent": user_agent,
        "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "referer": f"{auth_base}/email-verification",
        "oai-device-id": device_id,
    }

    try:
        sentinel_val, oai_sc_val = build_sentinel_token(session, device_id, "password_verify")
        login_headers["openai-sentinel-token"] = sentinel_val
        if oai_sc_val:
            session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
    except Exception:
        pass

    try:
        login_resp = session.post(
            f"{auth_base}/api/accounts/password/verify",
            headers=login_headers,
            json={"password": password},
            timeout=30,
        )
    except Exception as e:
        return {"ok": False, "error": f"password_verify_network_error: {str(e)}"}

    if login_resp.status_code != 200:
        return {"ok": False, "error": f"password_verify_failed_{login_resp.status_code}", "detail": login_resp.text}

    login_data = login_resp.json() if login_resp.text else {}
    continue_url = str(login_data.get("continue_url") or "").strip()
    auth_code = ""

    if continue_url and "code=" in continue_url:
        parsed_params = parse_qs(urlparse(continue_url).query)
        auth_code = str((parsed_params.get("code") or [""])[0]).strip()

    # 3. 2FA MFA challenge if needed
    if not auth_code:
        page_info = login_data.get("page", {})
        page_type = str(page_info.get("type") or "")
        if page_type == "mfa_challenge":
            factor_id = page_info.get("payload", {}).get("factor_id", "")
            if not factor_id:
                factors = page_info.get("payload", {}).get("factors", [])
                if factors:
                    factor_id = factors[0].get("id", "")

            if not factor_id:
                return {"ok": False, "error": "mfa_challenge_no_factor_id", "detail": login_data}

            if not totp_secret:
                return {"ok": False, "error": "mfa_challenge_requires_totp_secret", "detail": login_data}

            try:
                totp_code = get_totp(totp_secret)
            except binascii.Error:
                return {"ok": False, "error": "invalid_totp_secret_base32", "detail": "TOTP secret is not valid Base32"}
            except Exception as e:
                return {"ok": False, "error": f"totp_generation_failed: {str(e)}"}

            mfa_headers = dict(login_headers)
            try:
                mfa_sentinel, mfa_sc = build_sentinel_token(session, device_id, "verify_mfa")
                mfa_headers["openai-sentinel-token"] = mfa_sentinel
                if mfa_sc:
                    session.cookies.set("oai-sc", mfa_sc, domain=".openai.com")
            except Exception:
                pass

            mfa_headers["referer"] = f"{auth_base}/mfa-challenge/{factor_id}"

            try:
                mfa_resp = session.post(
                    f"{auth_base}/api/accounts/mfa/verify",
                    headers=mfa_headers,
                    json={
                        "id": factor_id,
                        "type": "totp",
                        "code": totp_code,
                    },
                    timeout=30,
                )
            except Exception as e:
                return {"ok": False, "error": f"mfa_verify_network_error: {str(e)}"}

            if mfa_resp.status_code != 200:
                return {"ok": False, "error": f"mfa_verify_failed_{mfa_resp.status_code}", "detail": mfa_resp.text}

            mfa_data = mfa_resp.json() if mfa_resp.text else {}
            mfa_continue_url = str(mfa_data.get("continue_url") or "").strip()
            if mfa_continue_url and "code=" in mfa_continue_url:
                parsed_params = parse_qs(urlparse(mfa_continue_url).query)
                auth_code = str((parsed_params.get("code") or [""])[0]).strip()

    if not auth_code:
        return {"ok": False, "error": "no_auth_code_obtained", "detail": login_data}

    # 4. Exchange Token
    platform_base = "https://platform.openai.com"
    token_headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }
    token_payload = {
        "client_id": platform_oauth_client_id,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": platform_oauth_redirect_uri,
    }

    try:
        token_resp = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers=token_headers,
            json=token_payload,
            timeout=60,
        )
    except Exception as e:
        return {"ok": False, "error": f"token_exchange_network_error: {str(e)}"}

    if token_resp.status_code != 200:
        return {"ok": False, "error": f"token_exchange_failed_{token_resp.status_code}", "detail": token_resp.text}

    token_data = token_resp.json() if token_resp.text else {}
    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "error": "empty_access_token", "detail": token_data}

    return {
        "ok": True,
        "email": email,
        "password": password,
        "totp_secret": totp_secret,
        "access_token": access_token,
        "refresh_token": str(token_data.get("refresh_token") or "").strip(),
        "id_token": str(token_data.get("id_token") or "").strip(),
        "expires_in": token_data.get("expires_in"),
    }


def main() -> None:
    """One account per process, stdin JSON / stdout JSON, no stdout tracing."""
    try:
        raw = sys.stdin.read(64 * 1024)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("action") != "authenticate":
            print(json.dumps({"ok": False, "error": "invalid_input"}))
            return
        email = str(data.get("email") or "").strip()
        password = str(data.get("password") or "")
        secret = str(data.get("totp_secret") or "").strip()
        proxy = str(data.get("proxy") or "").strip()
        if not email or not password or len(email) > 254 or len(password) > 512:
            print(json.dumps({"ok": False, "error": "missing_login"}))
            return
        value = login_with_password_and_totp(email, password, secret, proxy=proxy)
        # Output is consumed privately by the Magpie plugin host; never dump
        # upstream HTML or diagnostic detail that may contain session data.
        if not value.get("ok"):
            print(json.dumps({"ok": False, "error": str(value.get("error") or "auth_failed")[:120]}))
            return
        print(json.dumps({
            "ok": True, "access_token": value.get("access_token") or "",
            "refresh_token": value.get("refresh_token") or "",
            "id_token": value.get("id_token") or "",
            "expires_in": value.get("expires_in")
        }))
    except Exception:
        print(json.dumps({"ok": False, "error": "auth_worker_error"}))

if __name__ == "__main__":
    main()
