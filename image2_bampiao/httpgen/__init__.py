"""httpgen —— 半瓢的 HTTP 号池旁路（方案A）。

把用户已登录/可登录的 ChatGPT 账号，用纯 HTTP（不走浏览器）驱动网页版出图。
与现有 CDP（真浏览器）主链路并存、默认关闭，只做独立旁路，不动主链路。

组成：
- vendor/     从 yang0/magpie-gptfree（MIT）vendor 的 Python worker
- worker.py   子进程封装：auth_worker / image_worker 的一次性 JSON 调用
- vault.py    加密凭证库（邮箱/密码/2FA/token）
- pool.py     号池 CRUD + 批量文本导入

公开开关见 config [http_pool] enabled。
"""

from __future__ import annotations

__all__ = ["worker", "vault", "pool"]
