"""驱动写回给上层的结构化结局（`_result.json`）。

单独一个模块是为了能测：`driver/cdp_run.py` 是"导入即执行"的，
里面的函数没法单独 import（一 import 就跑起来要参数了）。

这个文件的形状是 session._classify 的契约：上层只认它，不 grep 日志全文。
2026-10-30 的教训是**开场白失败时这个文件根本没写**，于是"提示词没进去"
被归成 RENDER_FAILED，用户和 AI 都以为模型不肯画，全在猜额度/登录/措辞。
"""

from __future__ import annotations

import json
import os


def write(out_dir, *, ok: int = 0, fail: int = 0, quota: bool = False,
          paste_failed: int = 0, flags=None, states=None, extra=None) -> None:
    """写 _result.json。extra 里的字段会盖掉同名键（比如 reason=opening）。"""
    payload = {"ok": ok, "fail": fail, "quota": bool(quota),
               "paste_failed": int(paste_failed),
               "flags": flags or {}, "states": states or {}}
    if extra:
        payload.update(extra)
    try:
        os.makedirs(str(out_dir), exist_ok=True)      # 目录不存在时以前一个字节都写不出
        with open(os.path.join(str(out_dir), "_result.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception:
        # 写不了就别让驱动因此崩掉：图可能已经出来了，那才是用户关心的
        pass
