"""doctor：把"能不能跑"拆成一条条可判定的检查。

输出三类：ok / warn（能跑但不理想）/ fail（跑不了）。
`--json` 时只输出一个 JSON 对象，诊断信息走 stderr。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__, browsers, config as cfg_mod, platform as plat

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Row:
    level: str
    name: str
    detail: str = ""


def _dep(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def collect(explicit_config: str | None = None) -> list[Row]:
    rows: list[Row] = []

    rows.append(Row(OK if sys.version_info >= (3, 12) else FAIL, "Python",
                    "%d.%d.%d" % sys.version_info[:3]))
    rows.append(Row(OK, "平台", plat.current))

    # 两个平台各有一个"装不装得对"的隐形依赖，缺了的表现都是"双击没反应"，
    # 所以必须在 doctor 里说清楚，别让用户以为程序坏了。
    if plat.current == "windows":
        wv = plat.webview2_version()
        rows.append(Row(
            OK if wv else WARN, "WebView2 运行时",
            wv if wv else "没装 —— 窗口开不出来。装一下：https://go.microsoft.com/fwlink/p/?LinkId=2124703"))
    elif plat.current == "macos":
        ok = plat.automation_ok()
        rows.append(Row(
            OK if ok else WARN, "自动化权限",
            "已允许，浏览器窗口能自动隐藏" if ok else
            "没授权 —— 出图时浏览器窗口会留在屏幕上；"
            "去「系统设置 → 隐私与安全性 → 自动化」里勾上半瓢"))
        # 巡逻用哪条路探测窗口：Quartz 便宜且不用权限；退回 osascript 会吃掉几个点 CPU。
        # 用户反馈"Mac 上任务栏还有残留"时，这一行能立刻区分是权限问题还是探测方式问题。
        cheap = plat.window_probe_cheap()
        rows.append(Row(
            OK if cheap else WARN, "窗口探测方式",
            "Quartz（快，不吃 CPU）" if cheap else
            "osascript（慢，巡逻间隔已放宽到 12 秒；打好的安装包不会走到这里）"))

    # 冻结版（安装包）里这些模块是打进 exe 的，没有 pip 可装；
    # 缺了只可能是安装包残了，所以提示要换一句。
    frozen = bool(getattr(sys, "frozen", False))
    for mod, label in (("webview", "pywebview"), ("pystray", "pystray"),
                       ("PIL", "pillow")):
        ok = _dep(mod)
        if ok:
            detail = "已打包" if frozen else "已安装"
        else:
            detail = ("安装包不完整，建议重新安装" if frozen
                      else "缺依赖：pip install %s" % mod)
        rows.append(Row(OK if ok else FAIL, label, detail))

    path = cfg_mod.find_config(explicit_config)
    try:
        cfg = cfg_mod.load(path, missing_ok=True)
    except cfg_mod.ConfigError as e:
        # 配置写错时 doctor 不能自己先崩：它就是用来告诉人"哪里不对"的
        rows.append(Row(FAIL, "配置", str(e)))
        return rows
    if cfg is None:
        # 提示里要写真实 exe 路径：装完 Scripts 目录通常不在 PATH 里，
        # 写裸的 `bampiao init` 会让人 command not found（真踩过）。
        try:
            from .cli import _self as _self_cmd

            me = _self_cmd()
        except Exception:
            me = "bampiao"
        rows.append(Row(WARN, "配置", "还没初始化：%s\n    跑 `%s init`" % (path, me)))
        return rows
    rows.append(Row(OK, "配置", str(cfg.path)))

    # 数据目录可写
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        rows.append(Row(OK, "数据目录", str(cfg.data_dir)))
    except OSError as e:
        rows.append(Row(FAIL, "数据目录", "不可写：%s（%s）" % (cfg.data_dir, e)))

    rows.append(Row(OK, "并发", "%d ｜ 闲置回收 %d 分钟"
                    % (cfg.concurrency, cfg.idle_recycle_min)))

    if not cfg.accounts:
        rows.append(Row(WARN, "账号", "还没有账号：bampiao accounts add --name 我的号（浏览器自动挑）"))
        return rows

    for a in cfg.accounts:
        exe = browsers.find(a.browser)
        rows.append(Row(OK if exe else FAIL, "浏览器 · %s" % a.browser,
                        str(exe) if exe else browsers.missing_reason(a.browser)))

        # 登录态：这是"能不能真的出图"的唯一硬条件。
        # 以前 doctor 完全不看它，于是没登录也报"能跑"——Agent 拿它当前置检查，
        # 过了，然后 gen 直接 AUTH_REQUIRED（真踩过）。
        login = "unknown"
        try:
            from .core.store import Store

            login = Store(cfg).account(a.id).login
        except Exception:
            pass
        if login == "ok":
            rows.append(Row(OK, "账号 %s · 登录" % a.name, "已登录"))
        else:
            rows.append(Row(WARN, "账号 %s · 登录" % a.name,
                            "还没登录：bampiao login（会弹浏览器，登一次就好）"))

        prof_ok = a.profile.is_dir()
        rows.append(Row(OK if prof_ok else WARN, "账号 %s · profile" % a.name,
                        str(a.profile) + ("" if prof_ok else "（还没建，首次登录时创建）")))

        if plat.cdp_alive(a.port):
            rows.append(Row(OK, "账号 %s · 端口 %d" % (a.name, a.port), "CDP 在跑"))
        elif plat.port_open(a.port):
            rows.append(Row(WARN, "账号 %s · 端口 %d" % (a.name, a.port),
                            "端口被别的程序占着，不是半瓢的实例"))
        else:
            rows.append(Row(OK, "账号 %s · 端口 %d" % (a.name, a.port), "空闲（未启动）"))

    # 浏览器隐私提醒：profile 必须在 data_dir 内（config 已校验，这里复核一次）
    for a in cfg.accounts:
        try:
            a.profile.relative_to(cfg.data_dir)
        except ValueError:
            rows.append(Row(FAIL, "账号 %s · profile 越界" % a.name, str(a.profile)))

    return rows


def _width_safe(text: str) -> bool:
    """当前 stdout 能不能表示这个字符（cp437 之类就表示不了）。"""
    enc = (getattr(__import__("sys").stdout, "encoding", "") or "utf-8")
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render(rows: list[Row]) -> str:
    # 只用 cp936 里也有的符号：√ × ｜ 都能编码，管道里不会变成问号。
    # （✓ U+2713 和 ✗ U+2717 不在 GBK 里，中文 Windows 的管道上必成 "?"。）
    if _width_safe("√×｜"):
        icon = {OK: "√", WARN: "!", FAIL: "×"}
        bar = "｜"
    else:
        icon = {OK: "[OK]", WARN: "[!]", FAIL: "[X]"}
        bar = "|"
    out = ["半瓢 %s %s doctor" % (__version__, bar), ""]
    for r in rows:
        out.append(" %s %-22s %s" % (icon[r.level], r.name, r.detail))
    bad = sum(1 for r in rows if r.level == FAIL)
    warn = sum(1 for r in rows if r.level == WARN)
    out.append("")
    if bad:
        out.append("结论：%d 项必须先解决。" % bad)
    elif warn:
        out.append("结论：能跑，但有 %d 项提醒。" % warn)
    else:
        out.append("结论：可以出图。")
    return "\n".join(out)


def main(explicit_config: str | None = None, as_json: bool = False,
         allow_empty: bool = False) -> int:
    """0 = 能跑；3 = 还差几步要人动手（没初始化 / 没账号 / 没浏览器）。

    以前"没配置、没账号"也只返回 0，agent 拿它当前置检查会被骗：
    以为环境好了，跑 gen 才炸。现在把"没准备好"如实报出来。
    allow_empty 给"我只想确认装得对不对"的场景用。
    """
    rows = collect(explicit_config)
    hard = any(r.level == FAIL for r in rows)
    # "登录" 也算：没登录时 gen 一定失败，这属于"还差人动手"，不是"能跑"
    soft = any(r.level == WARN and (r.name in ("配置", "账号") or r.name.endswith("· 登录"))
               for r in rows)
    if allow_empty:
        soft = False
    bad = hard or soft
    if as_json:
        # ensure_ascii=True：纯 ASCII，任何码页解码后 json 都还能解析。
        # 中文原样输出会在中文 Windows 的管道里被当 GBK 解码，
        # 变成乱码不说，JSON 还会因此解析失败（安装脚本就栽在这）。
        print(json.dumps({
            "schema_version": 1, "ok": not bad,
            "needs_action": bad,
            "rows": [{"level": r.level, "name": r.name, "detail": r.detail} for r in rows],
        }, ensure_ascii=True))
    else:
        print(render(rows))
    # 3 = "需要用户动作"（改配置 / 装浏览器 / 去登录）。
    # 原来返回 5（内部错误），agent 会当成"工具自己挂了"而放弃重试。
    return 3 if bad else 0
