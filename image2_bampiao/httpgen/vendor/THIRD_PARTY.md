# Third-party authentication & image generation sources

The user asked that GPTFree use the same HTTP password/TOTP authentication
approach as their [chatgpt2api-dsh](https://github.com/yang0/chatgpt2api-dsh),
and free-account ChatGPT Web `picture_v2` image generation flow adapted from
`chatgpt2api-native`.

- `auth_worker.py`: adapted from
  `yang0/chatgpt2api-dsh/scripts/auth-worker.py`.
- `utils/pkce.py` and `utils/sentinel.py`: copied from
  `basketikun/chatgpt2api` (MIT license; notice in THIRD_PARTY_LICENSE.txt).
- `utils/web_pow.py` and `utils/turnstile.py`: adapted from
  `chatgpt2api-native` (`utils/pow.py`, `utils/turnstile.py`) using Python
  standard library only (`base64`, `hashlib.sha3_512`, `uuid`).
- `image_worker.py`: standalone ChatGPT Web `picture_v2` worker adapted from
  `chatgpt2api-native` (`services/openai_backend_api.py`,
  `services/protocol/conversation.py`) with pure-stdlib image dimension parsing
  and PNG alpha mask compositing (no Pillow, pybase64, or FastAPI required).
- `curl_cffi` is installed separately from PyPI into a local Python venv.

The imported auth worker requests **Platform OAuth** (`platform.openai.com`);
tokens are not guaranteed to authenticate the separate Codex backend or
ChatGPT Web endpoints. GPTFree verifies Codex access before admitting a token
to its pool, and handles `401` reauthentication and `429`/`5xx` failover cleanly.
