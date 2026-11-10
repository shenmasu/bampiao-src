"""会话健康判定：这个号此刻到底能不能干活。

2026-11-02 的事故：ChatGPT 给浏览器会话弹了 Cloudflare 挑战，消息根本没发出去，
页面上只留一个红条 `cloudflare_challenge` + 「无法加载历史记录」。半瓢当时不认识，
当成"消息流错误"重试三次，然后空等 540 秒 —— 用户看到的是"卡了 9 分钟"。

这里只管**判定**，不含任何 IO，方便单测。
"""
from __future__ import annotations

HEALTHY = "healthy"
CHALLENGED = "challenged"        # Cloudflare 挑战：消息发不出去，必须人来点
LOCAL_DRAFT = "local_draft"      # 本地草稿会话：消息没发到服务器
NO_WINDOW = "no_window"          # 进程在、窗口没了（假死）

# 页面文本里的特征串。用 include 而不是正则：ChatGPT 改版时这些词比 DOM 稳。
CHALLENGE_MARKS = ("cloudflare_challenge", "cloudflare challenge", "无法加载历史记录")
LOCAL_DRAFT_MARKS = ("local-chatgpt",)


def classify(url: str = "", text: str = "") -> str:
    """按页面 URL + 可见文本判状态。挑战优先于本地草稿：先解决没过验证的问题。"""
    low = (text or "").lower()
    if any(m.lower() in low for m in CHALLENGE_MARKS):
        return CHALLENGED
    if any(m in (url or "") for m in LOCAL_DRAFT_MARKS):
        return LOCAL_DRAFT
    return HEALTHY


def is_challenge(text: str = "") -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in CHALLENGE_MARKS)


def human_hint(state: str) -> str:
    """给人看的一句话。面板和日志共用，别各写各的。"""
    if state == CHALLENGED:
        return "需要你在浏览器里点一下：出现了 Cloudflare 验证，点「重试」或完成验证后再跑"
    if state == LOCAL_DRAFT:
        return "页面在本地草稿状态（消息没发出去），重启这个号的浏览器再试"
    if state == NO_WINDOW:
        return "浏览器进程还在但窗口没了，正在自动重启"
    return ""


# ---------------------------------------------------------------- 暂停闸门
# 一个号被挑战挡住之后，不该让后面每一单都去撞一次门：既慢又难看。
# 这里用一个小文件记"这个号暂时别用"，到点自动过期。

def pause_path(data_dir: str, acc_id: str) -> str:
    import os

    return os.path.join(str(data_dir), "state", "paused_%s.json" % acc_id)


def mark_paused(data_dir: str, acc_id: str, reason: str, minutes: float = 10.0) -> None:
    import json
    import os
    import time

    p = pause_path(data_dir, acc_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = {"reason": reason, "since": time.time(), "until": time.time() + minutes * 60}
    tmp = p + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def read_pause(data_dir: str, acc_id: str):
    """还没过期就返回记录，否则 None。"""
    import json
    import os
    import time

    p = pause_path(data_dir, acc_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if float(d.get("until") or 0) <= time.time():
            return None
        return d
    except Exception:
        return None


def clear_paused(data_dir: str, acc_id: str) -> None:
    import os

    try:
        os.remove(pause_path(data_dir, acc_id))
    except OSError:
        pass


# ------------------------------------------------------------ 需要重启的信号
# 会话坏了（本地草稿态）时，光靠 reload 救不回来，实测只有"重启这个号的浏览器"有效。
# 但驱动进程杀不了浏览器（它没有 cfg / 锁），所以只留一个标记，
# 下一次 ensure() 看到就自己重启。这样用户什么都不用做。

def broken_path(data_dir: str, acc_id: str) -> str:
    import os

    return os.path.join(str(data_dir), "state", "restart_%s.flag" % acc_id)


def mark_broken(data_dir: str, acc_id: str, reason: str = "") -> None:
    import os
    import time

    p = broken_path(data_dir, acc_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("%s %.0f\n" % (reason or "local_draft", time.time()))


def is_broken(data_dir: str, acc_id: str) -> bool:
    import os

    return os.path.exists(broken_path(data_dir, acc_id))


def clear_broken(data_dir: str, acc_id: str) -> None:
    import os

    try:
        os.remove(broken_path(data_dir, acc_id))
    except OSError:
        pass
