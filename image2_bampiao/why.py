"""把驱动的日志尾巴翻译成一句人话。

为什么要有这个文件：台账里原来只有 ``error_code``（``RENDER_FAILED`` 这种），
用户看到的就是"失败"两个字，完全不知道是提示词的问题、网络的问题，还是软件坏了。
驱动日志里其实写清了原因，但 ``gen`` 这条路只把 ``error_code`` 写进 job.json，
日志尾巴被丢掉了（2026-11-03 用户问"为什么不标失败原因"）。

这里做两件事：
1. 从日志尾巴里挑出最像原因的那一行（优先带 ``!!`` 的）；
2. 按关键词归成一句人话（换节点 / 重启浏览器 / 改提示词 / 重新登录）。

规则写在代码里而不是让模型猜：这些原因必须稳定、可测试、不花钱。
"""
from __future__ import annotations

import re

_TIME_PREFIX = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\]\s*")


def pick_line(tail: str) -> str:
    """从日志尾巴里挑一行最能说明问题的。"""
    lines = [ln.strip() for ln in (tail or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    bad = [ln for ln in lines if "!!" in ln]
    line = bad[-1] if bad else lines[-1]
    line = _TIME_PREFIX.sub("", line)
    line = line.replace("!!", "").strip()
    return line


# 关键词 → 人话。顺序有意义：先判最具体、最容易被误判成"渲染失败"的那几种。
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cloudflare", "人机验证", "cf_chl"),
     # 2026-11-05：原来写"换一个网络节点" —— 用户不知道"节点"是什么，
     # 也不知道该在哪点。改成看得懂的三步。
     "网页要确认一下是不是真人在操作。打开这个号的浏览器窗口，点一下那个验证框；"
     "如果一直过不去，换个网络再试一次"),
    (("本地草稿", "local-chatgpt"),
     # 2026-11-05：原文"会话是本地草稿"是内部实现，用户看不懂。
     "这条没发出去（浏览器里停在草稿状态）：把这个号的浏览器重启一下，再重投一次就行"),
    (("一直在生成中", "没加载完", "卡在加载", "一直是空白"),
     "页面卡住了、一直没加载出来：不是提示词的问题，刷新或重启浏览器"),
    (("提示词没进输入框",),
     "提示词没进输入框，这条没发出去（宁可失败也不发半句）"),
    (("参考图路径不存在", "参考图文件不存在", "no such file"),
     "参考图文件不存在：路径写错，或者文件还没生成好（改文件，不用改提示词）"),
    (("参考图", "附件", "上传"),
     "参考图没传上去：先确认文件还在、格式没变，再重投"),
    (("按钮未就绪", "找不到发送"),
     "页面上找不到发送按钮：多半是网页改版了，先反馈"),
    (("掉登录", "需要重新登录", "未登录"),
     "账号掉登录了：去面板「账号」页重新登录"),
    # 注意：不能只写"额度"——失败日志里常有"不占额度、不再空转"这种句子，
    # 会被误判成撞上限（2026-11-03 用真实日志验证时踩到）。
    (("生成上限", "撞上限", "达到上限", "quota_hit", "quota exceeded", "额度用完了"),
     "这个号的生成额度用完了：等冷却，或换一个号"),
)

_KIND_TEXT = {
    "quota": "这个号的生成额度用完了：等它冷却，或换一个号",
    "auth": "账号掉登录了：去面板「账号」页重新登录",
    "timeout": "发出去之后连接断了，不确定网页那边有没有出图：先去网页看一眼",
    "no_lane": "没有能用的账号（都在冷却或掉登录）",
    "browser-lost": "等图的时候浏览器被关掉了",
    "busy": "这个号正忙着跑别的单",
    "no-ref": "参考图文件不存在",
}


def _match(text: str) -> str:
    low = (text or "").lower()
    for keys, said in _RULES:
        for k in keys:
            if k in low or k in (text or ""):
                return said
    return ""


def explain(tail: str = "", kind: str = "") -> str:
    """一句人话。永远返回非空字符串。

    先看"最后一条报错行"——它最接近真实原因；整段尾巴只作兜底，
    否则一条日志里前面出现过的 Cloudflare 会把后面的"页面卡住"盖掉。
    """
    tail = tail or ""
    line = pick_line(tail)

    said = _match(line)
    if said:
        return said

    k = (kind or "").strip().lower()
    if k in _KIND_TEXT:
        return _KIND_TEXT[k]

    said = _match(tail)
    if said:
        return said
    if line:
        return line[:160]
    return "原因不明：日志里没有可识别的报错，把这条反馈给作者"


# ---------------------------------------------------------------- 失败分类（0.2.25）
from dataclasses import dataclass, field  # noqa: E402

CLS_WHITELIST = ("quota", "auth", "verify", "network", "page", "file", "unsure",
                 "policy", "empty", "other")


@dataclass(frozen=True)
class FailureInfo:
    """一类失败的完整说法：标题、短标签、解释、动作。面板/托盘/CLI 都从这里取。"""
    cls: str
    title: str
    short: str
    detail: str
    primary: tuple[str, str]                 # (action_id, 按钮文案)
    secondary: tuple[str, str] | None = None
    extra: tuple[tuple[str, str], ...] = ()  # 额外的常驻动作（unsure 用）
    steps: tuple[str, ...] = field(default_factory=tuple)


_INFO: dict[str, FailureInfo] = {
    "quota": FailureInfo(
        "quota", "这个号暂时画不了", "额度用完",
        "今天的额度用完了，通常几小时后恢复（我按 5 小时估的，不一定准）",
        ("retry_other", "换个号重投"), ("wait", "看还要等多久"),
        steps=("点「换个号重投」——会用另一个号重画这一张",
               "或者等这个号冷却，队列会自动接着跑")),
    "auth": FailureInfo(
        "auth", "这个号掉登录了", "要重新登录",
        "网页登录过期了，重新登一次就好",
        ("login", "去登录"), ("retry_other", "换个号重投"),
        steps=("点「去登录」，在弹出的窗口里登一次",
               "登好它会自己收起来，回来点「再投一次」")),
    "verify": FailureInfo(
        "verify", "被网页的验证挡住了", "验证没通过",
        "先在浏览器里点一下；不行就把 VPN / 加速器关掉再试",
        ("open_browser", "打开浏览器，我来点"), ("retry_other", "换个号重投"),
        steps=("点「打开浏览器」，在那个窗口里完成验证",
               "验证过了会自己消失；没反应就点「我点完了，再投一次」")),
    "network": FailureInfo(
        "network", "连不上 ChatGPT", "网络问题",
        "有别的软件改了网络设置，这很常见",
        ("net_check", "检查网络"), ("feedback", "反馈给作者"),
        steps=("点「检查网络」，先看三行结论",
               "还连不上就点「反馈给作者」，日志会一起带上")),
    "page": FailureInfo(
        "page", "网页卡住了", "页面卡住",
        "页面没加载出来，跟提示词没关系",
        ("restart_browser", "重启这个号的浏览器"), ("retry", "再投一次"),
        steps=("点「重启这个号的浏览器」，大约 20 秒",
               "重启好了会自动接着重画，不用你再点")),
    "file": FailureInfo(
        "file", "参考图丢了", "文件找不到",
        "派单时给的图片路径不存在，重投也没用",
        ("feedback", "让作者看看这一单"), ("retry_file_fixed", "文件补好了，重投"),
        steps=("点「让作者看看这一单」——会把路径和日志一起打包",
               "或者把文件放回原处，点「文件补好了，重投」")),
    "unsure": FailureInfo(
        "unsure", "不确定有没有出图", "结果不确定",
        "发送后连接断了，网页那边可能已经画完",
        ("look_web", "去网页看一眼"),
        extra=(("found", "网页上有图，我收下"), ("retry_confirm", "确实没有，重投")),
        steps=("先去网页看一眼：有图就点「网页上有图，我收下」",
               "确实没有，再点「确实没有，重投」")),
    "policy": FailureInfo(
        "policy", "被内容政策挡住了", "内容政策",
        "ChatGPT 判定这张图违规，改了才会画；跟网络、账号、软件都没关系",
        ("retry", "我改好提示词了，再投"), None,
        steps=("别直接照搬手机 App / 网站界面、品牌标志、真人照片",
               "把「原型图、界面还原」改成描述画面内容，或者换一张参考图")),
    "empty": FailureInfo(
        "empty", "这次没拿到回复", "没有回复",
        "网页里没读到这次的回复：可能还在生成，也可能页面没刷出来",
        # 主按钮必须是"先去看"：重问会再花一份额度，步骤和按钮不能反着来
        ("look_web", "先打开浏览器看看"), ("retry", "确实没有，重问一次"),
        steps=("点「打开浏览器看看」——那边可能已经有答案了，只是我们没读到",
               "确实没有，再点「确实没有，重问一次」（会再花一份额度）")),
    "other": FailureInfo(
        "other", "这张没画出来", "没画出来",
        "没记下具体原因，再投一次多半能好",
        ("retry", "再投一次"), ("feedback", "反馈给作者"),
        steps=("点「再投一次」",
               "还是不行就点「反馈给作者」，日志会一起带上")),
}

# 文本任务（ask）的同一套分类，但**话术完全不同**：
# 不能跟用户说"这张没画出来""网页上有图，我收下"——文本单没有图（2026-11-06 评审）。
_INFO_ASK: dict[str, FailureInfo] = {
    "empty": FailureInfo(
        "empty", "这次没拿到回复", "没有回复",
        "网页里没读到这次的回复：可能还在生成，也可能页面没刷出来",
        # 主按钮必须是"先去看"：重问会再花一份额度，步骤和按钮不能反着来
        ("look_web", "先打开浏览器看看"), ("retry", "确实没有，重问一次"),
        steps=("点「打开浏览器看看」——那边可能已经有答案了，只是我们没读到",
               "确实没有，再点「确实没有，重问一次」（会再花一份额度）")),
    "unsure": FailureInfo(
        "unsure", "不确定有没有回复", "结果不确定",
        "中间断了或等到超时，网页那边可能已经把话答完了",
        ("look_web", "打开浏览器看看"),
        extra=(("retry_confirm", "确实没有，重问"),),
        steps=("先去网页看一眼：有回复就不用重问（重问会再花一份额度）",
               "确实没有，再点「确实没有，重问」")),
    "auth": FailureInfo(
        "auth", "这个号现在问不了", "要重新登录",
        "可能掉登录、页面没加载完，或者被网页的验证挡住了",
        ("login", "去登录"), ("look_web", "打开浏览器看看"),
        steps=("点「去登录」，在弹出的窗口里登一次",
               "或者点「打开浏览器看看」，看看是不是停在验证页")),
    "network": FailureInfo(
        "network", "连不上 ChatGPT", "网络问题",
        "有别的软件改了网络设置，这很常见",
        ("net_check", "检查网络"), ("feedback", "反馈给作者"),
        steps=("点「检查网络」，先看三行结论",
               "还连不上就点「反馈给作者」，日志会一起带上")),
    "page": FailureInfo(
        "page", "网页卡住了", "页面卡住",
        "页面没加载出来，跟你要问的内容没关系",
        ("restart_browser", "重启这个号的浏览器"), ("retry", "重问一次"),
        steps=("点「重启这个号的浏览器」，大约 20 秒",
               "重启好了再点「重问一次」")),
    "other": FailureInfo(
        "other", "这条没答出来", "没答出来",
        "没记下具体原因，再问一次多半能好",
        ("retry", "重问一次"), ("feedback", "反馈给作者"),
        steps=("点「重问一次」",
               "还是不行就点「反馈给作者」，日志会一起带上")),
}

_CODE_TO_CLS = {
    "QUOTA_COOLDOWN": "quota",
    "AUTH_REQUIRED": "auth",
    "NO_REF_FILE": "file",
    "TIMEOUT_UNCERTAIN": "unsure",
    "CONTENT_POLICY": "policy",
    "BROWSER_LOST": "page",
    "NO_LANE": "network",
    # 文本任务（ask）
    "ASK_EMPTY": "empty",
    "ASK_TIMEOUT": "unsure",
    "ASK_SEND": "page",
    "ASK_FAILED": "other",
}


def _cls_from_text(text: str) -> str:
    t = text or ""
    low = t.lower()
    if "empty_reply" in low or "没读到这次的回复" in t:
        return "empty"
    if "no_composer" in low:
        return "auth"            # 没登录 / 没加载完 / 被验证挡住，先按"去登录看看"处理
    if "send_failed" in low or "消息没发出去" in t:
        return "page"            # 发不出去＝页面没准备好，刷新/重启浏览器
    if "cloudflare" in low or "人机验证" in t or "cf_chl" in low:
        return "verify"
    if "本地草稿" in t or "local-chatgpt" in low:
        return "verify"          # 会话坏了，处理方式同样是"去浏览器里弄一下"
    if "一直在生成中" in t or "没加载完" in t or "卡在加载" in t:
        return "page"
    if "一直是空白" in t or "goto" in low or "连不上" in t:
        return "network"
    if "被拒答" in t or "内容政策" in t or "content policy" in low:
        return "policy"
    if "参考图" in t or "附件" in t:
        return "file"
    return ""


def classify(tail: str = "", error_code: str = "", uncertain: bool = False,
             kind: str = "") -> FailureInfo:
    """把一次失败归到八类里的一类，并给全套说法。

    优先级：uncertain → error_code → 日志特征 → other。
    cls 永远在白名单里；白名单外的历史值一律回落 other（绝不把原始 code 显示给用户）。

    ``kind="ask"`` 时用文本任务的词典：同一类失败，出图说"没画出来/我收下这张"，
    文本要说"没答出来/去看看有没有回复"。
    """
    table = _INFO_ASK if (kind or "").strip().lower() == "ask" else _INFO
    if uncertain and (error_code or "").upper() in ("", "TIMEOUT_UNCERTAIN"):
        return table["unsure"]

    code = (error_code or "").upper()
    cls = _CODE_TO_CLS.get(code, "")
    if not cls and code == "RENDER_FAILED":
        cls = _cls_from_text(pick_line(tail)) or _cls_from_text(tail)
    if not cls:
        cls = _cls_from_text(pick_line(tail)) or _cls_from_text(tail)
    if cls not in table:
        cls = "other"
    return table[cls]


def info_of(cls: str) -> FailureInfo:
    """按 cls 取说法；未知 cls 回落 other。"""
    return _INFO.get((cls or "").strip().lower(), _INFO["other"])
