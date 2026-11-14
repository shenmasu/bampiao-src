"""命令行入口：`bampiao ...`

退出码约定（写进 JSON 合约，agent 靠它决策）：
  0  成功
  2  参数 / 配置错误
  3  需要用户动作（去登录、去恢复备份）
  4  等待超时（任务还没结束，状态没变）
  5  内部错误
  6  已结束但存在失败项 / 部分成功
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config as cfg_mod, doctor as doc_mod, locks, paths

# 2026-11-05：这里原来在模块级把 sys.stdout 重包成 UTF-8，结果把 pytest 的
# 输出捕获也一起搞坏了（ValueError: I/O operation on closed file）。
# 现在改成"只在输出到管道时用字节写 UTF-8"（见 _w），不再动全局 stdio。

EXIT_OK, EXIT_USAGE, EXIT_ACTION, EXIT_TIMEOUT, EXIT_INTERNAL, EXIT_PARTIAL = 0, 2, 3, 4, 5, 6


def _self() -> str:
    """给"下一步"提示用的命令名：**永远写 `bampiao`**。

    以前这里返回绝对路径（形如 D:\\…\\.venv\\Scripts\\bampiao.exe）。
    用户照着抄到记事本、发给朋友、贴进 Agent 的提示词，换台机器就是废的。
    装好之后它在 PATH 里（安装器写用户环境变量），所以裸命令名才对。
    """
    return "bampiao"


# 一次进程只跑一条命令，全局记一下"最后说了什么错"，供 --json 兜底用。
_LAST = {"why": "", "code": "", "hint": "", "emitted": False}

# 退出码 → 默认错误码。命令自己知道更具体的（如 auth/quota），会显式传。
_CODE_BY_RC = {2: "BAD_USAGE", 3: "NEED_USER", 4: "TIMEOUT",
               5: "INTERNAL", 6: "PARTIAL"}


def _emit(obj: dict) -> None:
    # ensure_ascii=True：JSON 里不出现非 ASCII 字节。
    # 这样无论管道那头按 UTF-8 还是 GBK 解码，json.loads 都能拿到正确内容
    # （中文会变成 \uXXXX 转义，机器读完全等价）。
    _LAST["emitted"] = True
    print(json.dumps(obj, ensure_ascii=True))


def _err(msg: str, code: str = "", hint: str = "") -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    _LAST["why"] = msg.strip()
    if code:
        _LAST["code"] = code
    if hint:
        _LAST["hint"] = hint


def _finish(a, rc: int) -> int:
    """收尾：`--json` 时保证 stdout **永远有一行 JSON**，成功失败都一样。

    原来失败时 stdout 是 0 字节，而 SKILL.md 写着"可以直接 json.loads"——
    照文档写的 Agent 会当场 JSONDecodeError。人话仍旧留在 stderr，
    stdout 只给机器。
    """
    if rc != 0 and getattr(a, "json", False) and not _LAST["emitted"]:
        _emit({"schema_version": 1, "ok": False,
               "error_code": _LAST["code"] or _CODE_BY_RC.get(rc, "ERROR"),
               "rc": rc,
               "why": _LAST["why"] or "命令失败，细节看 stderr",
               "hint": _LAST["hint"]})
    return rc


# ------------------------------------------------------------------ 子命令

def cmd_init(a) -> int:
    try:
        path, created = cfg_mod.init(a.config, force=a.force)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    cfg = cfg_mod.load(path)

    # 顺手把第一个账号建好。
    # 以前 init 只写 config，账号要另外 `accounts add`；而桌面壳却自己建一个，
    # 两条路的"第一次体验"不一样——照着教程走的人会卡在"还没有账号"。
    new_acc = None
    if not cfg.accounts:
        from . import browsers
        picked = browsers.auto()
        if picked:
            try:
                new_acc = cfg_mod.append_account(path, "我的号", picked, None)
            except cfg_mod.ConfigError as e:
                _err("建账号失败：%s" % e)

    if a.json:
        _emit({"schema_version": 1, "ok": True, "config": str(path),
               "created": created, "data_dir": str(cfg.data_dir),
               "account": new_acc.as_row() if new_acc else None})
    else:
        print(("已创建 " if created else "已存在 ") + str(path))
        print("数据目录：%s" % cfg.data_dir)
        if new_acc:
            print("\n已建好账号「%s」（用 %s 跑）" % (new_acc.name, new_acc.browser))
            print("下一步：%s login    # 会弹出浏览器，登录你自己的 ChatGPT" % _self())
        elif not cfg.accounts:
            print("\n这台机器上没找到 Chrome 或 Edge。装一个之后跑："
                  "\n  bampiao accounts add --name 我的号")
    return EXIT_OK


def _skills_dir() -> Path:
    """随包的 skills 目录。

    两套布局都要认：冻结后是 `image2_bampiao/skills`（spec 里拷进去的），
    源码树里是仓库根的 `skills/`。只认一种就会「装了能跑、开发时找不到」。
    """
    here = Path(__file__).resolve().parent
    for cand in (here / "skills", here.parent / "skills"):
        if cand.is_dir():
            return cand
    return here / "skills"


_SKILL_FILES = {
    # paste = 直接粘到 Agent 对话框就能用，不用去改 CLAUDE.md / AGENTS.md
    "paste": ["PASTE.md"],
    "claude": ["CLAUDE.snippet.md"],
    "agents": ["AGENTS.snippet.md"],
    "all": ["CLAUDE.snippet.md", "AGENTS.snippet.md", "SKILL.md"],
    # 单独一份完整的 skill（带 frontmatter，装进 ~/.workbuddy/skills/ 用）
    "skill": ["SKILL.md"],
    # 单独一份完整的 skill（带 frontmatter，装进 ~/.workbuddy/skills/ 用）
    "skill": ["SKILL.md"],
}


def _exe_hint() -> str:
    """bampiao 的完整路径：AI 的终端如果是装半瓢之前开的，PATH 里还没有它。"""
    import sys

    exe = Path(sys.executable)
    for name in ("bampiao.exe", "bampiao"):
        alt = exe.with_name(name)
        if alt.exists():
            return str(alt)
    return str(exe) if getattr(sys, "frozen", False) else "bampiao"


_PATH_RULE_OLD = ("- 如果提示找不到 bampiao（Windows「不是内部或外部命令」/ macOS「command not found」）：\n"
                  "  说明你启动时还没读到新的 PATH，让我重启终端 / 重启你再试一次，不要自己去改 PATH。")


def skill_text(which: str = "paste") -> str:
    """面板和 `bampiao skill` 共用：读 skills/*.md，并把本机 bampiao 的完整路径填进去。

    2026-10-30 实测：用户把这段贴给一个早就开着的 agent，它的 PATH 里没有 bampiao，
    于是转头去用了旧工具。给出完整路径，它就不用等重启。
    """
    d = _skills_dir()
    chunks = []
    for name in _SKILL_FILES[which]:
        f = d / name
        if not f.exists():
            raise FileNotFoundError(name)
        chunks.append(f.read_text(encoding="utf-8").rstrip())
    text = "\n\n---\n\n".join(chunks) + "\n"
    exe = _exe_hint()
    if exe != "bampiao":
        new = ("- 如果提示 bampiao 找不到（你的终端是装半瓢之前开的），直接用完整路径，不用等重启：\n"
               "  \"%s\"\n  不要自己去改 PATH。" % exe)
        if _PATH_RULE_OLD in text:
            text = text.replace(_PATH_RULE_OLD, new)
        else:
            text = text.rstrip() + "\n" + new + "\n"
    return text


_GLOBAL_BEGIN = "<!-- bampiao:begin 由半瓢写入，可整段删除 -->"
_GLOBAL_END = "<!-- bampiao:end -->"


def _dsh_root() -> Path:
    """DeepSeek Harness 的家目录：非空 $DSH_HOME 优先，否则 ~/.dsh。

    和 dsh 自己的解析规则保持一致（内置插件 dsh-agent-instructions 就是这么找的），
    否则我们把说明写到一个它根本不读的地方，用户看到"装好了"却毫无效果。
    """
    v = os.environ.get("DSH_HOME", "").strip()
    return Path(v).expanduser() if v else Path.home() / ".dsh"


def global_targets() -> dict:
    """各家 AI 的**全局说明文件**（往里追加一段带标记的说明）。

    2026-10-31 用户拍的板：不要让人选"你用的是哪家 AI"——他根本不知道区别。
    扫到哪个装哪个，全给。
    """
    home = Path.home()
    return {
        "claude": home / ".claude" / "CLAUDE.md",
        "codex": home / ".codex" / "AGENTS.md",
        # opencode 全局规则；没有这个文件时它会回退读 ~/.claude/CLAUDE.md，
        # 但用户可以设 OPENCODE_DISABLE_CLAUDE_CODE=1 关掉那个兜底，所以显式写一份
        "opencode": home / ".config" / "opencode" / "AGENTS.md",
        "gemini": home / ".gemini" / "GEMINI.md",
        "dsh": _dsh_root() / "AGENTS.md",
        # 2026-11-14 扩展：只在对应 agent 目录存在时才真正写（install_global_auto 会检测）
        "qwen": home / ".qwen" / "QWEN.md",
        "windsurf": home / ".codeium" / "windsurf" / "memories" / "global_rules.md",
    }


_SKILL_TARGETS = {
    "workbuddy": Path(".workbuddy") / "skills" / "bampiao" / "SKILL.md",
    "hermes": Path(".hermes") / "skills" / "bampiao" / "SKILL.md",
    # 通用 agent 技能库（DSH 等自定义 agent 也读这里）
    "agents": Path(".agents") / "skills" / "bampiao" / "SKILL.md",
}


def skill_targets() -> dict:
    """要装成 **skill 目录**的（不是追加段落，是整份文件，靠 frontmatter 被识别）。

    WorkBuddy 只认 ~/.workbuddy/skills/<名字>/SKILL.md 这种结构，往 AGENTS.md
    追加它是不读的 —— 这正是那位 Mac 用户"只有 Claude 能出图"的原因。

    Hermes（Nous Research 那个 Agent）同理：它的技能就是 ~/.hermes/skills/<名字>/SKILL.md，
    开场只读 frontmatter 里的 name/description 做索引，真要用才加载全文（progressive
    disclosure）。2026-11-06 朋友的 Hermes Desktop 在 Mac 上只找到 /Applications/半瓢.app，
    「命令行里没找到 bampiao 命令」——因为我们压根没往她的技能目录写过东西。
    """
    return {k: Path.home() / rel for k, rel in _SKILL_TARGETS.items()}


def _ensure_cli_shim(dirs: list[str] | None = None) -> str:
    """macOS 上把 bampiao 放进 PATH，返回建好的软链路径（没建就返回空串）。

    2026-11-06 反馈：朋友的 Hermes 在 Mac 上能找到 `/Applications/半瓢.app`
    （版本、bundle id 都报对了），但说"命令行里没找到可直接用的 bampiao 或 半瓢 命令"。
    .app 里其实一直有 CLI（Contents/MacOS/bampiao），只是没人把它放进 PATH ——
    dmg 拖进 Applications 就完事了，没有安装器替我们做这件事。

    写进 skill 的说明里会带完整路径，但人和别的工具还是习惯直接敲，所以顺手补一个软链：
      /usr/local/bin/bampiao  （可写就优先，绝大多数 Mac 都已在 PATH 里）
      ~/.local/bin/bampiao    （只有它本来就在 PATH 里才建，否则建了也白建）
    已存在同名文件就认输，绝不覆盖别人的东西。

    `dirs` 只给测试用：这段代码会往系统目录写东西，跑单元测试不能真去碰 /usr/local/bin。
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return ""
    exe = _exe_hint()
    if not exe.startswith("/"):
        return ""
    if dirs is None:
        path_dirs = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        home_bin = str(Path.home() / ".local" / "bin")
        dirs = ["/usr/local/bin"] + ([home_bin] if home_bin in path_dirs else [])
    for d in dirs:
        link = Path(d) / "bampiao"
        try:
            if link.is_symlink():
                if os.path.realpath(link) == os.path.realpath(exe):
                    return str(link)                 # 已经是它了
                continue                             # 指向别处，不抢
            if link.exists():
                continue                             # 是个真文件，不动
            Path(d).mkdir(parents=True, exist_ok=True)
            os.symlink(exe, link)
            return str(link)
        except OSError:
            continue
    return ""


def _target_path(name: str) -> tuple[Path | None, str]:
    """名字 → (路径, 写入方式)。方式：append = 追加带标记的段，file = 整份覆写。"""
    g = globals()
    p = g["global_targets"]().get(name)
    if p is not None:
        return p, "append"
    p = g["skill_targets"]().get(name)
    if p is not None:
        return p, "file"
    return None, ""


def global_targets_installed() -> list[str]:
    """物理上装了哪几家（看目录在不在）。

    用户不该被问"你用的是哪家 AI"——他电脑上有什么，我们看得出来。
    skill 类（WorkBuddy）看的是 skills 目录的上一级，也就是 ~/.workbuddy。
    """
    out = [k for k, p in global_targets().items() if p.parent.is_dir()]
    for k, p in skill_targets().items():
        # ~/.workbuddy/skills/bampiao/SKILL.md → 往上三层才是 ~/.workbuddy
        if p.parent.parent.parent.is_dir():
            out.append(k)
    return out


def _os_why(e: OSError) -> str:
    """把写文件失败的 errno 翻成"用户接下来该干什么"。

    直接甩 `[Errno 13] Permission denied: 'C:\\...\\SKILL.md'` 给用户，
    他只能看到一串路径和英文，不知道是自己要动手还是软件坏了。
    """
    code = getattr(e, "errno", 0) or 0
    if code in (13, 1):
        return "没权限写这个文件：它可能被设成「只读」了（右键 → 属性 → 取消只读），或者正被别的程序锁着"
    if code == 2:
        return "上一级目录不存在，建不出来"
    if code == 28:
        return "磁盘满了"
    return str(e)


def install_global_auto(remove: bool = False) -> dict:
    """装进全局，不分 Claude Code / Codex：装了哪家写哪家。

    一家都没装时两处都写好（文件放着不占地方，将来装了它们就能直接用），
    并在返回里说清楚，别让人以为"装成功了"却什么都没发生。
    """
    picked = global_targets_installed()
    note = ""
    if not picked:
        picked = list(global_targets()) + list(skill_targets())
        note = ("没检测到任何 AI 工具，先把说明都放好了；"
                "等你装了它们就能直接用")
    done, failed, written = [], [], []
    for t in picked:
        try:
            r = install_global(t, remove)
        except OSError as e:
            why = _os_why(e)
            failed.append("%s（%s）" % (t, why))
            written.append({"name": t, "ok": False, "why": why})
            continue
        if not r.get("ok"):
            failed.append(t)
            written.append({"name": t, "ok": False, "why": r.get("why") or "写入失败"})
            continue
        done.append(t)
        if remove:
            written.append({"name": t, "ok": True, "path": r.get("path"), "why": ""})
            continue
        # **读回校验**：光"write_text 没抛异常"不算数，还要能读出来。
        # 用户要的是"点完这一步，第 3 步的 AI 一定认识半瓢"，
        # 那么"文件里真的有这段"就是唯一能自查的证据。
        back_ok = global_installed(t)
        written.append({
            "name": t, "ok": back_ok, "path": r.get("path"),
            "why": "" if back_ok else "写完了但读不回来（可能被别的程序覆盖了）",
        })
    if not done:
        return {"ok": False, "targets": [], "failed": failed, "written": written,
                "why": "一个都没写成：%s" % "、".join(failed or ["？"])}

    # macOS 上顺手补个 PATH 软链：朋友的 Hermes 就是因为"命令行里没有 bampiao"才没调起来。
    # 建不成不影响写入结果（skill 里本来就带完整路径）。
    shim = ""
    if not remove:
        try:
            shim = _ensure_cli_shim()
        except Exception:
            shim = ""
        if shim:
            note = (note + "；" if note else "") + "已把 bampiao 加进 PATH：%s" % shim

    # 字段名必须避开 "cli"：面板那边会把 cli_selfcheck() 的结果塞进 r["cli"]，
    # 同名会被覆盖成 dict，前端渲染出 "[object Object]"。2026-11-06 发 0.2.55 时踩到。
    return {"ok": True, "targets": done, "failed": failed, "written": written,
            "note": note, "shim": shim}


def cli_selfcheck() -> dict:
    """确认另一半：AI 不光要"知道"半瓢，还得能**执行**它。

    这一步是用户要求的"只要过了第 2 步，第 3 步就一定能认识"里的另一半。
    光把说明写进 md 是不够的——常见的坑是 AI 的终端 PATH 里没有 bampiao
    （那个终端是装半瓢之前就开着的），于是"知道有这东西但调不动"。
    所以这里真跑一次 --version，跑得出来才算通。

    返回 {"ok": bool, "exe": str, "version": str, "why": str}
    """
    import shutil
    import subprocess

    cands = []
    w = shutil.which("bampiao")
    if w:
        cands.append(w)
    if getattr(sys, "frozen", False):
        cands.append(sys.executable)          # 打包版：exe 自己就是 bampiao
    for exe in cands:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, timeout=20)
        except Exception:
            continue
        # Windows 上打包 exe 输出的是本地代码页（GBK），不能直接 text=True
        raw = r.stdout or b""
        for enc in ("utf-8", "gbk", "cp936"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                text = ""
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if r.returncode == 0 and lines:
            return {"ok": True, "exe": exe, "version": lines[0].strip(), "why": ""}
    return {"ok": False, "exe": cands[0] if cands else "",
            "version": "",
            "why": "命令行跑不起来（PATH 里可能没有 bampiao，或者被拦了）"}


def _write_text(path: Path, text: str) -> None:
    """写文件前先摘掉只读位。

    2026-11-06 反馈：`C:\\Users\\sun.yongchang\\.workbuddy\\skills\\bampiao\\SKILL.md`
    报 `Permission denied`。用户不会想到是文件带了"只读"属性（同步盘、解压出来的、
    某些编辑器保存时都会这么干），只会觉得半瓢坏了。这是半瓢自己的文件，
    只读位是我们有权改的，就顺手改掉再写。
    """
    try:
        if path.exists() and not os.access(path, os.W_OK):
            os.chmod(path, 0o644)
    except OSError:
        pass                     # 改不动就照写，让上层报出真实的错
    path.write_text(text, encoding="utf-8")


def install_global(target: str, remove: bool = False) -> dict:
    """把「出图用半瓢」写进（或移出）某个 AI 的全局说明。只动自己那一段，别的一个字不碰。"""
    import re

    path, mode = _target_path(target)
    if path is None:
        return {"ok": False, "why": "不认识 %s" % target}

    if mode == "file":
        # skill 类：整份覆写（它本来就是半瓢自己的文件，不碰别人的东西）
        if remove:
            if path.exists():
                path.unlink()
            return {"ok": True, "path": str(path), "removed": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, skill_text("skill").rstrip() + "\n")
        return {"ok": True, "path": str(path), "removed": False}

    old = path.read_text(encoding="utf-8") if path.exists() else ""
    pat = re.compile(re.escape(_GLOBAL_BEGIN) + r".*?" + re.escape(_GLOBAL_END) + r"\n?", re.S)
    body = pat.sub("", old).rstrip()
    if not remove:
        block = "%s\n## 出图用半瓢\n\n%s\n%s\n" % (_GLOBAL_BEGIN, skill_text("paste").rstrip(), _GLOBAL_END)
        body = (body + "\n\n" if body else "") + block
    elif body:
        body += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = path.with_name(path.name + ".bampiao-bak")
    if old and not bak.exists():
        _write_text(bak, old)                        # 第一次改之前留一份原样
    _write_text(path, body)
    return {"ok": True, "path": str(path), "removed": remove}


def global_installed(target: str) -> bool:
    p, mode = _target_path(target)
    try:
        if not (p and p.exists()):
            return False
        text = p.read_text(encoding="utf-8")
        return "bampiao" in text if mode == "file" else (_GLOBAL_BEGIN in text)
    except OSError:
        return False


def cmd_skill(a) -> int:
    """把"贴给你的 Agent"的原文交出来。

    这是整个产品的入口：用户装完 exe，他的 Claude Code / Codex 需要知道有半瓢。
    以前这些 md 只躺在仓库里，装了 exe 的人根本拿不到——核心功能等于不存在。
    """
    want = a.which or "paste"
    if want not in _SKILL_FILES:
        _err("不认识 %s，可选：%s" % (want, " / ".join(_SKILL_FILES)), code="BAD_USAGE")
        return EXIT_USAGE

    try:
        text = skill_text(want)
    except FileNotFoundError as e:
        _err("安装包里少了 %s，重装一次试试。" % e, code="MISSING_SKILL")
        return EXIT_ACTION

    if a.out:
        dst = Path(a.out).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        if a.json:
            _emit({"schema_version": 1, "ok": True, "which": want,
                   "path": str(dst.resolve()), "bytes": len(text.encode("utf-8"))})
        else:
            print("已写到 %s" % dst)
            print("把它粘进你 Agent 的项目说明（AGENTS.md / CLAUDE.md / 自定义指令）里。")
        return EXIT_OK

    if a.json:
        _emit({"schema_version": 1, "ok": True, "which": want, "text": text})
    else:
        print(text, end="")
    return EXIT_OK


def cmd_feedback(a) -> int:
    """打一个反馈包（诊断 + 日志 + 失败单），让用户一键发回来。

    和 diag 的区别：diag 是一页给人看的文本；反馈包是 zip，
    里面还带日志原文和每单的失败码，够复现问题。
    """
    from . import feedback as fb_mod

    try:
        cfg = cfg_mod.load(a.config, missing_ok=True)
        out = fb_mod.collect(cfg, Path(a.out) if a.out else None)
    except Exception as e:
        if a.json:
            _emit({"ok": False, "why": "%s: %s" % (type(e).__name__, e)})
        else:
            _err("打包失败：%s" % e)
        return EXIT_USAGE

    # 先落盘再上传：传不上去也留得住，用户手动发同一条路
    sent, why = (False, "已跳过")
    if not getattr(a, "no_upload", False):
        sent, why = fb_mod.upload(out, cfg)

    if a.json:
        _emit({"ok": True, "path": str(out), "bytes": out.stat().st_size,
               "uploaded": sent, "upload_note": why})
    else:
        print("反馈包已写到：%s" % out)
        print("（%d KB，里面有诊断、最近日志和失败记录；不含密码和 cookie）"
              % max(1, out.stat().st_size // 1024))
        if sent:
            print("已自动上传，作者那边会收到。")
        else:
            print("没传上去（%s）。等网络好了再跑一次 bampiao feedback。" % why)
    return EXIT_OK


def cmd_doctor(a) -> int:
    try:
        return doc_mod.main(a.config, as_json=a.json,
                            allow_empty=getattr(a, "allow_empty", False))
    except Exception as e:      # doctor 崩了还得告诉人哪里不对，不能吐 traceback
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": "%s: %s" % (type(e).__name__, e)})
        else:
            _err("doctor 自己出错了：%s: %s" % (type(e).__name__, e))
        return EXIT_INTERNAL


def cmd_accounts(a) -> int:
    try:
        cfg = cfg_mod.load(a.config)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    if a.action in (None, "list"):
        rows = [x.as_row() for x in cfg.accounts]
        if a.json:
            _emit({"schema_version": 1, "ok": True, "accounts": rows})
        else:
            if not rows:
                print("还没有账号。加一个：bampiao accounts add --name 日常 --browser chrome")
            for r in rows:
                print(" %-6s %-12s %-7s %5d  %s"
                      % (r["id"], r["name"], r["browser"], r["port"], r["profile"]))
        return EXIT_OK

    if a.action == "add":
        if not a.name:
            _err("用法：bampiao accounts add --name 显示名 [--browser chrome|edge] [--port N]")
            return EXIT_USAGE
        browser = a.browser
        if not browser or browser == "auto":
            from . import browsers
            browser = browsers.auto()
            if not browser:
                _err("这台机器上没找到 Chrome 或 Edge，半瓢需要一个来跑。",
                     code="NO_BROWSER",
                     hint="装一个 Chrome 或 Edge（Windows 自带 Edge）后重试。")
                return EXIT_ACTION
        try:
            acc = cfg_mod.append_account(cfg.path, a.name, browser, a.port)
        except cfg_mod.ConfigError as e:
            _err(str(e))
            return EXIT_USAGE
        if a.json:
            _emit({"schema_version": 1, "ok": True, "account": acc.as_row()})
        else:
            print("已添加账号 %s（%s / %s / 端口 %d）"
                  % (acc.name, acc.id, acc.browser, acc.port))
            print("下一步：%s login %s" % (_self(), acc.id))
        return EXIT_OK

    if a.action in ("rm", "rename"):
        # 多账号的另一半：能加就得能减、能改名，否则越攒越多只能手改 toml。
        aid = a.account
        if not aid:
            _err("用法：bampiao accounts %s <账号id或名字>%s"
                 % (a.action, " --name 新名字" if a.action == "rename" else ""),
                 code="BAD_USAGE")
            return EXIT_USAGE
        acc = _pick_account(cfg, aid)
        try:
            if a.action == "rm":
                # 和面板同一条规矩：最后一个号不许删。
                # 两套入口态度不一致，用户换个入口就会把自己搞到"没有账号"（实测过）。
                if len(cfg.accounts) <= 1:
                    _err("只剩这一个号了，删了就出不了图。要删先加一个。",
                         code="BAD_USAGE",
                         hint="bampiao accounts add --name 新号")
                    return EXIT_USAGE
                # 先关浏览器：不然配置删了、浏览器还在后台跑，又是一个关不掉的窗口
                from .driver import browser as br
                br.stop(cfg, acc)
                cfg_mod.remove_account(cfg.path, acc.id)
                msg = "已删除账号「%s」（浏览器也关了；profile 目录留着，登录态没删）" % acc.name
            else:
                # 新名字可以跟在后面，也可以走 --name（帮助里以前没写，照"改名 <账号>"敲必报错）
                new_name = (getattr(a, "new_name", None) or a.name or "").strip()
                if not new_name:
                    _err("改名要给新名字：bampiao accounts rename %s 新名字（或 --name 新名字）" % acc.id,
                         code="BAD_USAGE")
                    return EXIT_USAGE
                cfg_mod.rename_account(cfg.path, acc.id, new_name)
                msg = "已把「%s」改名为「%s」" % (acc.name, new_name)
        except cfg_mod.ConfigError as e:
            _err(str(e))
            return EXIT_USAGE
        if a.json:
            _emit({"schema_version": 1, "ok": True, "action": a.action, "why": msg})
        else:
            print(msg)
        return EXIT_OK

    _err("未知动作：%s" % a.action)
    return EXIT_USAGE


def _recent_profile_mtime(acc) -> float:
    """账号 profile 目录的最后修改时间，用来判断"最近实际用过的是哪个"。"""
    try:
        return Path(acc.profile).stat().st_mtime
    except OSError:
        return 0.0


def _pick_account(cfg, key: str | None):
    """按 id 或显示名找账号；**没指定时不许报错**。

    多账号的人（或他的脚本）少写一个 --account 就整批图全废——2026-10-30 实测：
    用户跑着 13 张的批量，中途加了个 acc2，之后每条 gen 都被
    "有 2 个账号，请指定是哪一个" 拒掉，12 张全失败。加账号不该把正在干活的人搞挂。
    所以这里按"最近真的用过的那个"自动挑（profile 目录 mtime 最新），静默继续。
    """
    accounts = list(cfg.accounts)
    try:
        from .httpgen import integration as _hg
        accounts.extend(_hg.http_accounts(cfg))
    except Exception:
        pass
    if not accounts:
        raise cfg_mod.NeedsUser(
            "还没有账号。先建一个（浏览器会自动挑本机装了的那个）：\n"
            "  bampiao accounts add --name 我的号\n"
            "建完再登录：bampiao login")
    if key:
        hits = [a for a in accounts if key in (a.id, a.name)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # 重名时静默取第一个，会让用户以为在操作 A 其实在操作 B
            raise cfg_mod.ConfigError("「%s」对应多个账号（%s），请用 id 指定"
                                      % (key, ", ".join(x.id for x in hits)))
        raise cfg_mod.ConfigError("没有这个账号：%s（现有：%s）"
                                  % (key, ", ".join(a.id for a in accounts)))
    if len(accounts) == 1:
        return accounts[0]

    # 多账号且没指定 → 按 account_strategy 挑。
    # 2026-10-31 用户反馈：两个号，出图永远只用第一个，第二个白养着。
    # 原因是老逻辑固定挑"profile mtime 最新"的那个——只要一直用 acc1，它永远最新。
    # cli 和面板共用 config.choose_by_strategy，免得两边规矩对不上。
    store_obj = None
    try:
        from .core.store import Store
        store_obj = Store(cfg)
    except Exception:
        pass
    today_of = (lambda a: store_obj.account(a.id).today_count()) if store_obj else None
    load = store_obj.active_load() if store_obj else {}
    pending_of = (lambda a: load.get(a.id, 0)) if store_obj else None
    # 2026-11-11：tier 策略要档位，连续失败降权要 fail_streak（带 2 小时衰减）；
    # 不传这两个回调时 choose_by_strategy 行为与旧版一致。
    plan_of = (lambda a: store_obj.account(a.id).plan) if store_obj else None
    fail_of = (lambda a: store_obj.account(a.id).fail_streak_now()) if store_obj else None

    # 先滤掉"现在不能用"的号：bampiao gen 直出不经过 Coordinator 的
    # usable_accounts()，不滤就会把冷却/掉登录/被验证挂起的号直接派去出图。
    # 全都不能用时退回全量，让下游按老规矩报错，别在这里悄悄换语义。
    candidates = accounts
    if store_obj:
        try:
            from .driver import session_health as sh

            usable = []
            for a in accounts:
                st = store_obj.account(a.id)
                if not getattr(st, "enabled", True):
                    continue
                if st.cooling() or st.login == "required":
                    continue
                if sh.read_pause(str(cfg.data_dir), a.id):
                    continue
                usable.append(a)
            if usable:
                candidates = usable
        except Exception:
            pass

    acc = cfg_mod.choose_by_strategy(cfg, candidates, today_of, pending_of,
                                     plan_of, fail_of)
    if acc is not None:
        want = (getattr(cfg, "account_fixed_id", "") or "").strip()
        fixed_gone = ((getattr(cfg, "account_strategy", "") or "").strip().lower() == "fixed"
                      and want and want not in (acc.id, acc.name))
        if fixed_gone:
            try:
                print("（固定用的账号「%s」已经不在了，这次按用得少的挑）" % want,
                      file=sys.stderr)
            except Exception:
                pass
        return acc
    return accounts[0]


def cmd_browser(a) -> int:
    from .driver import browser as br

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    if a.action == "status":
        st = br.alive(cfg, acc)
        if a.json:
            _emit({"schema_version": 1, "ok": True, "account": acc.id, **st})
        else:
            print(" %-10s %s（pid %s）" % (acc.name, st["state"], st["pid"] or "-"))
            print(" %s" % st["note"])
        return EXIT_OK

    if a.action == "up":
        try:
            created, note = br.ensure(cfg, acc)
        except Exception as e:
            # 端口被别人占 / 没装浏览器 / profile 被别的实例占着 —— 都要用户动手
            if a.json:
                _emit({"schema_version": 1, "ok": False, "why": str(e)})
            else:
                _err("起不来：%s" % e)
            return EXIT_ACTION
        if a.json:
            _emit({"schema_version": 1, "ok": True, "created": created,
                   "note": note, "account": acc.id})
        else:
            print(("新启动 " if created else "复用 ") + note)
        return EXIT_OK

    if a.action == "down":
        ok, note = br.stop(cfg, acc)
        if a.json:
            _emit({"schema_version": 1, "ok": bool(ok), "note": note,
                   "account": acc.id})
        else:
            print(note)
        return EXIT_OK if ok else EXIT_ACTION

    _err("未知动作：%s" % a.action)
    return EXIT_USAGE


def _chat_rows(items: list) -> list:
    import time as _t

    from .driver.chats import _when, is_ours

    out = []
    for x in items:
        ts = _when(x)
        when = _t.strftime("%m-%d %H:%M", _t.localtime(ts)) if ts else "-"
        title = (x.get("title") or "(无标题)").replace("\n", " ")[:44]
        mark = "*" if is_ours(x) else " "
        out.append(" %s %-26s %s  %s" % (mark, str(x.get("id"))[:26], when, title))
    return out


def cmd_chats(a) -> int:
    """列出 / 删除 ChatGPT 侧栏里的会话。

    为什么单独做：每出一轮图就会在网页里留一个会话，长期会堆成几百条，
    用户自己在侧栏里翻不动。删除是**不可撤销**的，所以规则是：
    - 不给选择条件就只列，不删；
    - 真要删必须 --yes 或交互确认；
    - 一次删多少条都先说清楚。
    """
    from .driver import chats as ch
    from .driver import session as sess

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    try:
        data = sess.chats(cfg, acc, "list", limit=max(a.limit, 50))
    except Exception as e:
        _err("连不上浏览器：%s" % e)
        return EXIT_ACTION
    if not data.get("ok"):
        _err("列不出来：%s" % data.get("why", ""))
        return EXIT_ACTION

    items = data.get("items") or []

    if a.action == "list":
        mine = [x for x in items if ch.is_ours(x)]
        if a.json:
            _emit({"schema_version": 1, "ok": True, "total": data.get("total"),
                   "items": items[: a.limit], "ours": len(mine)})
        else:
            print("会话共 %s 条，其中半瓢起的 %d 条（标 *）\n"
                  % (data.get("total"), len(mine)))
            for line in _chat_rows(items[: a.limit]):
                print(line)
            print("\n删除（默认只删半瓢自己起的那些）：")
            print("  bampiao chats rm --id <会话id> --yes")
            print("  bampiao chats rm --keep 10 --yes    （半瓢的留最近 10 条，其余删掉）")
            print("  bampiao chats rm --older 7 --yes    （删 7 天前的）")
        return EXIT_OK

    # ---- rm
    picked = ch.filter_items(items, keep=a.keep, older_days=a.older, ids=a.ids,
                             only_ours=not a.any)
    if not picked:
        if a.json:
            _emit({"schema_version": 1, "ok": True, "deleted": [], "why": "没有匹配的会话"})
        else:
            print("没有匹配的会话，什么都没删。")
            print("（不给 --id / --keep / --older 时，为安全起见不会删除任何东西）")
        return EXIT_OK

    if not a.yes:
        if a.json or not sys.stdin.isatty():
            _err("要删 %d 条，但没加 --yes。为防误删，这里不会替你决定。" % len(picked))
            return EXIT_USAGE
        print("将要删除 %d 条会话：" % len(picked))
        for line in _chat_rows(picked[:20]):
            print(line)
        if len(picked) > 20:
            print("  …还有 %d 条" % (len(picked) - 20))
        try:
            ans = input("\n确认删除？不可撤销 [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消，什么都没有删。")
            return EXIT_OK

    res = sess.chats(cfg, acc, "rm", ids=[x["id"] for x in picked])
    done = res.get("deleted") or []
    bad = res.get("failed") or []
    if a.json:
        _emit({"schema_version": 1, "ok": res.get("ok") and not bad,
               "deleted": done, "failed": bad, "why": res.get("why")})
    else:
        print("已删除 %d 条。" % len(done))
        if bad:
            print("有 %d 条没删掉（可能已经不存在或没权限）：" % len(bad))
            for b in bad[:10]:
                print("  %s → %s" % (b.get("id"), b.get("status") or b.get("why")))
    return EXIT_OK if res.get("ok") and not bad else EXIT_PARTIAL


def cmd_probe(a) -> int:
    from .driver import session as sess

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    try:
        r = sess.probe(cfg, acc)
    except Exception as e:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": str(e)})
        else:
            _err("连不上浏览器：%s" % e)
        return EXIT_ACTION
    if a.json:
        _emit({"schema_version": 1, **r})
        return EXIT_OK if r["ok"] else EXIT_ACTION
    if not r["ok"]:
        _err("探不到页面：%s\n%s" % (r.get("log"), r.get("tail", "")[-600:]))
        return EXIT_ACTION
    p = r["probe"]
    print(" 页面：%s" % p.get("url", "")[:90])
    print(" 标题：%s" % p.get("title", ""))
    print(" 已登录：%s ｜ 输入框：%s" % ("是" if p.get("logged_in") else "否",
                                        "有" if p.get("composer") else "没有"))
    # 探到已登录就把账号状态改回可用：否则一次掉登录会让这个账号永久停用
    try:
        from .core.store import Store

        store = Store(cfg)
        st = store.account(acc.id)
        if p.get("logged_in"):
            st.login = "ok"
            st.login_user = str(p.get("user_id") or "")
            email = str(p.get("email") or "").strip()
            if email:
                st.login_email = email
            plan = str(p.get("plan") or "").strip()
            if plan:
                st.plan = plan
                st.plan_source = "session"
                st.plan_checked_at = __import__("time").time()
        else:
            st.login = "required"
        st.login_checked_at = __import__("time").time()
        store.save_account(st)
    except Exception:
        pass

    if not p.get("logged_in"):
        print("\n  还没有登录。跑 `%s login %s`，窗口会弹到最前，登完自动藏回去。"
              % (_self(), acc.id))
    return EXIT_OK


# 驱动内部的 kind → 对外契约里的错误码 → 退出码。
# 三套词必须一次翻清楚：人看的是中文，agent 看的是 error_code + rc。
_KIND_CODE = {"auth": "AUTH_REQUIRED", "quota": "QUOTA_COOLDOWN",
              "timeout": "TIMEOUT_UNCERTAIN", "render": "RENDER_FAILED",
              "busy": "BUSY", "paste": "PASTE_FAILED",
              # 参考图不存在是"改文件"的事，跟提示词、网络都无关。
              # 以前漏了这一条，全归成 GENERATE_FAILED，agent 会去改提示词白折腾。
              "no-ref": "NO_REF_FILE",
              # 「你更喜欢哪一张图像？」偏好问卷：确定没出图，别归成不确定
              "survey": "SURVEY_BLOCKED"}

_KIND_RC = {"auth": EXIT_ACTION, "quota": EXIT_ACTION, "timeout": EXIT_TIMEOUT,
            "busy": EXIT_INTERNAL, "paste": EXIT_ACTION, "no-ref": EXIT_ACTION,
            "survey": EXIT_ACTION}


def _rc_for_kind(kind: str) -> int:
    """失败类型 → 退出码。人类分支和 --json 分支共用同一套，绝不能两套语义。"""
    return _KIND_RC.get(kind or "", EXIT_INTERNAL)


def _explain_fail(kind: str, acc_id: str) -> str:
    """把驱动的 kind 翻成**能照着做**的话。

    原来这里直接打 ``kind=auth rc=9``，朋友看到只会发懵（首轮体验审计的原话）。
    技术码留在括号里，方便出了问题对日志。
    """
    me = _self()
    if kind == "auth":
        return ("这个账号还没有登录，或者登录已经失效了。\n"
                "  跑 `%s login %s` 重新登录，然后再试一次。" % (me, acc_id))
    if kind == "quota":
        return ("这个账号今天的生成额度到上限了。\n"
                "  等几个小时（或换一个账号）再来；**现在反复重试没用**。")
    if kind == "timeout":
        return ("等太久没等到结果。**它可能已经把图画出来了**，只是我们没等到。\n"
                "  先去 ChatGPT 网页里看一眼，别急着重试——重试可能重复消耗额度。")
    if kind == "busy":
        return ("这个账号正被另一个半瓢占着（或者上一次还没跑完）。\n"
                "  等它跑完再来，或者换一个账号。")
    if kind == "paste":
        return ("提示词没能完整放进输入框（半瓢是往网页里粘的，偶尔会粘空），所以没发出去——"
                "**这不是额度问题，也不是提示词内容的问题**。\n"
                "  再跑一次通常会好；一直不行就把提示词拆短（先发主句，细节接着说）。")
    if kind == "no-ref":
        return ("参考图文件不存在（路径写错，或者文件还没生成好）。\n"
                "  这是派单方的问题：改文件路径，改提示词没用。")
    if kind == "no_lane":
        return ("浏览器没能起来，所以没法出图。\n"
                "  跑 `%s browser status %s` 看它卡在哪。" % (me, acc_id))
    return ("图没生成出来（页面报错，或者模型没给图）。\n"
            "  先改一句提示词再试；连着两次这样，在面板里点「反馈」写一句，日志会自动带上。")


def _w(line: str = "") -> None:
    """写一行给人/Agent 看的东西。

    2026-11-05：Windows 上 exe 的 stdout 是 cp936，Agent 按 UTF-8 读就全是乱码
    （实测害得一个第三方 Agent 白跑一次）。改 stdout 编码在 PyInstaller 里行不通
    （reconfigure 无效；重包 TextIOWrapper 会让 exe 崩在 I/O operation on closed file）。
    所以只在"输出被重定向到管道"时直接用字节写 UTF-8；人在终端里看还是走 print，
    控制台照常显示。
    """
    s_out = sys.stdout
    buf = getattr(s_out, "buffer", None)
    try:
        tty = s_out.isatty()
    except Exception:
        tty = True
    if buf is not None and not tty:
        try:
            buf.write((line + chr(10)).encode("utf-8"))
            buf.flush()
            return
        except Exception:
            pass
    print(line)


def _chats_file(cfg) -> Path:
    return Path(cfg.data_dir) / "state" / "chats.json"


def _read_chats(cfg) -> dict:
    try:
        return json.loads(_chats_file(cfg).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _remember_chat(cfg, name: str, account: str, url: str) -> None:
    """记住这个名字对应的会话，下次 --chat 同名就接着聊。

    会话是**绑账号**的：同一个对话框在 acc1 和 acc2 里是两份上下文，
    所以记录里带账号，续聊时号不对就明确报错，而不是悄悄开个新的。
    """
    if not name or "/c/" not in (url or ""):
        return
    d = _read_chats(cfg)
    d[name] = {"account": account, "url": url, "at": time.strftime("%Y-%m-%dT%H:%M")}
    p = _chats_file(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


async def _ask_over_cdp(port: int, text: str, url: str, timeout: float, log=None) -> dict:
    from playwright.async_api import async_playwright

    from .driver import webchat
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        pg = None
        for ctx in b.contexts:
            for q in ctx.pages:
                if "chatgpt.com" in (q.url or ""):
                    pg = q
                    break
            if pg:
                break
        if pg is None:
            ctx = b.contexts[0] if b.contexts else await b.new_context()
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await webchat.ask(pg, text, url or None, timeout=timeout, log=log)


def _ask_record(cfg, acc, text: str, r: dict, log_path: str = "") -> None:
    """ask 也记一笔，面板才看得见「额度花在哪」。

    记账失败绝不打断回答——用户要的是回复，不是账单。
    """
    try:
        import time as _t

        from .core.models import Item, JOB_DONE, JOB_FAILED
        from .core.store import Store
        from . import why as _why

        store = Store(cfg)
        ok = bool(r.get("ok"))
        err = str(r.get("error") or "")
        if ok:
            code, cls = None, ""
        elif err.startswith("EMPTY_REPLY"):
            code, cls = "ASK_EMPTY", "empty"
        elif err.startswith("TIMEOUT"):
            code, cls = "ASK_TIMEOUT", "unsure"
        elif err.startswith("NO_COMPOSER"):
            code, cls = "ASK_FAILED", "auth"
        elif err.startswith("SEND_FAILED"):
            code, cls = "ASK_SEND", "page"
        else:
            code, cls = "ASK_FAILED", "other"
        # rendered 是只读属性（从条目状态算出来的），不能直接赋值——
        # 之前就是在这里抛 AttributeError，整段记账被 except 吞掉，台账里一片空白。
        item = Item(id="S01", prompt=(text or "")[:2000], aspect="text",
                    status="ok" if ok else "failed",
                    error_code=code, failure_class=cls, log_path=log_path or "")
        if not ok:
            item.note = err[:300]
            item.reason = _why.classify(err, code or "", kind="ask").detail
        job, _ = store.new_job(
            account_id=acc.id, items=[item],
            brief=(text or "").strip().splitlines()[0][:60],
            submitter="cli", kind="ask",
            status=JOB_DONE if ok else JOB_FAILED)
        job.ran_on = acc.id
        job.client_version = __version__

        # 时间要落盘：文本图表的中位耗时、"耗时"这一栏都靠它。
        # 0.2.50 之前只建单不写时间，于是文本在统计里完全不存在（2026-11-06）。
        now = _t.time()
        seconds = float(r.get("seconds") or 0) or None
        job.finished_at = now
        job.started_at = (now - seconds) if seconds else now

        if ok:
            reply = r.get("text") or ""
            job.note = reply[:400]
            # 全文落盘：面板要能看、要能复制，400 字远远不够
            try:
                (store.dir_of(job.id) / "S01.txt").write_text(reply, encoding="utf-8")
            except OSError:
                pass
            job.partial = bool(r.get("partial"))
        else:
            job.reason = (r.get("error") or "没拿到回复")[:300]
            # note 留空：面板会把 note 当"回复"显示，写错误码等于把 EMPTY_REPLY
            # 当成答案给用户（2026-11-06 评审）。
            job.note = ""
        store.save(job)
    except Exception:
        pass


def cmd_ask(a) -> int:
    """把一句话交给「网页会员额度」去答（和出图同一套账号池、同一把锁）。

    2026-11-05 用户定的方向：**整个半瓢都走 ChatGPT 网页会员额度**，
    出图是这样，文本任务也是这样；多号轮着用，但要节制（一条一条来）。
    """
    import asyncio

    from .driver import browser as dbrowser

    text = (a.prompt or "").strip()
    if getattr(a, "file", None):
        try:
            text = Path(a.file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            _err("读不了 %s：%s" % (a.file, e), code="BAD_USAGE")
            return EXIT_USAGE
    if text == "-" or getattr(a, "stdin", False):
        text = sys.stdin.read()
    if not text.strip():
        _err("没说要问什么", code="BAD_USAGE")
        return EXIT_USAGE

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    _keep_windows_hidden(cfg)

    url = ""
    if getattr(a, "chat", ""):
        rec = _read_chats(cfg).get(a.chat) or {}
        url = str(rec.get("url") or "")
        if url and rec.get("account") and rec["account"] != acc.id:
            _err("「%s」这个会话是在账号 %s 上开的，换号会丢上下文；"
                 "加 --account %s 接着聊，或者换个会话名。"
                 % (a.chat, rec["account"], rec["account"]), code="BAD_USAGE")
            return EXIT_USAGE

    try:
        # timeout=0 太激进：面板刚好在跑一单时，人敲一次 ask 就直接报 BUSY，
        # 看着像坏了。等 3 秒——真在出图的那单通常几十秒，报错也还来得及。
        with locks.exclusive(dbrowser.gen_lockfile(cfg, acc.id), timeout=3,
                             what="账号 %s 的对话" % acc.id):
            dbrowser.ensure(cfg, acc)
            h = dbrowser.read_handle(cfg, acc.id)
            port = int(getattr(h, "port", 0) or 0) if h else 0
            if not port:
                _err("这个号的浏览器没起来，先跑：bampiao browser up %s" % acc.id,
                     code="NO_LANE")
                return EXIT_ACTION
            if not a.json:
                _w("用 %s 问（%d 字，约 10–60 秒）…" % (acc.name, len(text)))
            # 留一份日志：EMPTY_REPLY 时能看出是没发出去、还是回复晚到了
            logp = cfg.logs_dir / ("ask_%s_%d.log" % (acc.id, int(time.time())))
            logp.parent.mkdir(parents=True, exist_ok=True)

            def _log(msg: str, _p=logp) -> None:
                try:
                    with open(_p, "a", encoding="utf-8") as f:
                        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
                except OSError:
                    pass

            _log("开始问话（%d 字，超时 %d 秒）" % (len(text), a.timeout))
            r = asyncio.run(_ask_over_cdp(port, text, url, a.timeout, log=_log))
            _log("结束：ok=%s 用时 %ss %s"
                 % (r.get("ok"), r.get("seconds"), r.get("error") or ""))
            a._ask_log = str(logp)
    except locks.Busy:
        _err("这个号正在出图或对话（等了 3 秒仍被占），过一会儿再来；"
             "半瓢一条一条来，不并发", code="BUSY")
        return EXIT_ACTION

    if r.get("ok") and getattr(a, "chat", ""):
        _remember_chat(cfg, a.chat, acc.id, r.get("url") or "")

    _ask_record(cfg, acc, text, r, log_path=str(getattr(a, "_ask_log", "") or ""))

    if not r.get("ok"):
        msg = r.get("error") or "没拿到回复"
        if not a.json and getattr(a, "_ask_log", ""):
            msg += "（日志：%s）" % a._ask_log
        _err(msg, code="ASK_FAILED")
        return EXIT_ACTION

    out = r.get("text") or ""
    if getattr(a, "out", None):
        dst = Path(a.out).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(out, encoding="utf-8")
        if a.json:
            _emit({"schema_version": 1, "ok": True, "seconds": r.get("seconds"),
                   "path": str(dst.resolve()), "chars": len(out),
                   "account": acc.id, "url": r.get("url")})
        else:
            _w("已写到 %s（%.0f 秒，%d 字）" % (dst, r.get("seconds") or 0, len(out)))
        return EXIT_OK
    if a.json:
        _emit({"schema_version": 1, "ok": True, "seconds": r.get("seconds"),
               "text": out, "account": acc.id, "url": r.get("url")})
        return EXIT_OK
    _w(out)
    return EXIT_OK


def cmd_gen(a) -> int:
    from .driver import session as sess

    if getattr(a, "prompt_file", None):
        # 长提示词（几十行的分镜/人设）放命令行里会被 shell 转义搞坏，
        # 从文件读是 webimg 就有的能力，半瓢一直缺（审计发现的唯一实质差距）。
        try:
            a.prompt = Path(a.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            _err("读不到提示词文件：%s" % e, code="BAD_USAGE")
            return EXIT_USAGE

    # 先看命令自己写全了没有，再管环境。
    # 反过来的话，多账号的人少写一个提示词会收到"有 2 个账号，请指定是哪一个"，
    # 修了半天才发现是提示词没给（实测踩到）。
    if not a.prompt:
        _err("给个提示词：bampiao gen \"画面描述\" --out 图.png",
             code="BAD_USAGE", hint="把要画的东西写在第一个参数里。")
        return EXIT_USAGE

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    _keep_windows_hidden(cfg)

    if a.out:
        out = Path(a.out).expanduser().resolve()
    else:
        # 不给路径就自己起个名字：agent 少一个必填参数，人也可以直接 gen "画个猫"
        out = Path.cwd() / time.strftime("bampiao-%Y%m%d-%H%M%S.png")
    if not a.json:
        print("用 %s 出图 → %s（约 1–3 分钟）" % (acc.name, out.name))
    svg, svg_anim = _svg_flags(a)
    if svg and getattr(a, "gif", False):
        _err("--svg / --svg-anim 不能和 --gif 一起用：一个要矢量文件，一个要位图动图。",
             code="BAD_USAGE", hint="二选一；要会动的矢量图用 --svg-anim。")
        return EXIT_USAGE
    if svg and a.alpha:
        a.alpha = False
        print("（SVG 本身就是透明矢量，--alpha 已忽略）", file=sys.stderr)
    if getattr(a, "gif", False) and a.alpha:
        a.alpha = False
        print("（动图不支持透明底，--alpha 已忽略）", file=sys.stderr)
    # --gif：模型不会读心，明确说"要 GIF"比事后失望便宜
    if getattr(a, "gif", False):
        _p = a.prompt or ""
        if "GIF" not in _p.upper() and "动图" not in _p and "循环" not in _p:
            a.prompt = (_p.rstrip()
                        + "\n（输出为 2–3 秒无缝循环的 GIF 动图，不要静态图。）")
    if svg:
        a.prompt = _svg_prompt(a.prompt or "", svg_anim)
    rec = _gen_record_start(cfg, acc, a)
    import re
    gist = re.sub(r"^\s*\d{3,4}\s*[x×*]\s*\d{3,4}\s*", "", " ".join((a.prompt or "").split()))
    title = ("R%d·" % rec[1].id if rec else "") + gist[:12]     # 会话标题里不要「1024x1024」
    # 参考图先在本机查一遍，再去碰浏览器。以前是连上 CDP、发完开场、
    # 建好会话、改完标题之后才发现文件不在——白建一个 ChatGPT 会话等两分钟
    # （2026-11-03 实测：一个不存在的路径耗了 2 分 12 秒）。
    missing = [str(x) for x in (a.ref or [])
               if str(x).strip() and not Path(str(x)).expanduser().is_file()]
    if missing:
        r = {"ok": False, "kind": "no-ref", "rc": 0, "log": "",
             "reason": "参考图文件不存在：%s" % missing[0],
             "tail": "[%s] !! 参考图路径不存在: %s\n"
                     % (time.strftime("%H:%M:%S"), missing[0])}
        _gen_record_end(cfg, rec, r)
    else:
        if getattr(acc, "kind", "browser") == "http":
            from .httpgen import integration as _hg
            r = _hg.generate(cfg, acc, a.prompt, out, refs=a.ref or [], aspect=a.aspect,
                             alpha=a.alpha, timeout=a.timeout, title=title,
                             anim=bool(getattr(a, "gif", False)),
                             svg=svg, svg_anim=svg_anim)
        else:
            r = sess.generate(cfg, acc, a.prompt, out, refs=a.ref or [], aspect=a.aspect,
                              alpha=a.alpha, timeout=a.timeout, title=title,
                              anim=bool(getattr(a, "gif", False)),
                              svg=svg, svg_anim=svg_anim)
        _gen_record_end(cfg, rec, r)
    if a.json:
        # error_code 是给 agent 决策的（SKILL 里那张表用的就是这套词），
        # kind 只是内部归类。以前只给 kind，agent 得自己猜映射。
        payload = {"schema_version": 1, **r}
        payload["account"] = acc.id
        # 交付物是静图还是动图，Agent 需要一眼知道（不用自己开文件）
        if r.get("ok"):
            from . import media as _media
            _probe = _media.probe(r.get("path") or "")
            payload.setdefault("format", _probe.get("format", ""))
            payload.setdefault("frames", _probe.get("frames", 1))
            payload.setdefault("animated", bool(_probe.get("animated")))      # 多账号自动挑人时，调用方要能知道用的是谁
            if _probe.get("svg"):
                payload.setdefault("svg", True)
                payload.setdefault("svg_animated", bool(_probe.get("svg_animated")))
        if not r.get("ok"):
            from . import why as _why

            payload["error_code"] = _KIND_CODE.get(r.get("kind") or "", "GENERATE_FAILED")
            payload["reason"] = _why.explain(r.get("tail") or "", r.get("kind") or "")
            payload["hint"] = _explain_fail(r.get("kind") or "", acc.id)
        _emit(payload)
        if r.get("ok"):
            return EXIT_OK
        # 和人类可读分支用同一套退出码：json 不能是另一套语义
        return _rc_for_kind(r.get("kind") or "")

    if r.get("ok"):
        _extra = " · 动图 %d 帧" % r.get("frames", 1) if r.get("animated") else ""
        if r.get("svg"):
            _extra = " · SVG 矢量" + ("（含动效）" if r.get("svg_animated") else "（静态）")
        if r.get("note"):
            print("  注意：%s" % r["note"], file=sys.stderr)
        print("已保存 %s  %s%s" % (r["path"], r.get("size") or "", _extra))
        return EXIT_OK
    from . import why as _why

    _err("没出图：%s" % _why.explain(r.get("tail") or "", r.get("kind") or ""))
    _err("（技术信息：kind=%s rc=%s）\n日志：%s"
         % (r.get("kind"), r.get("rc"), r.get("log")))
    if r.get("tail") and r.get("kind") not in ("auth", "quota"):
        # auth/quota 的原因已经说清了，再甩 500 字日志只会干扰；
        # 其余情况日志末尾是唯一的线索。
        _err("\n日志末尾：\n%s" % r["tail"][-500:])
    # 超时必须单独一个退出码：agent 拿到 5 会当成"内部错误，重试一次"，
    # 而超时的单子可能已经在网页里出图了，重投＝重复扣额度。
    return _rc_for_kind(r.get("kind") or "")


def _gen_record_start(cfg, acc, a):
    """gen 也在面板里记一单。

    以前 gen 不走队列、不建单：AI 平时多半用 gen 出单张，用户打开面板一张都看不到，
    产量也不算（2026-10-30 用户：「我在半瓢里看不到你提的单子」）。
    记账失败不能影响出图，所以全程吞异常。
    """
    try:
        from .core.models import Item, JOB_RUNNING
        from .core.store import Store

        store = Store(cfg)
        item = Item(id="S01", prompt=a.prompt, aspect=a.aspect, alpha=bool(a.alpha),
                    refs=[str(x) for x in (a.ref or [])])
        job, _ = store.new_job(account_id=acc.id, items=[item],
                               brief=(a.prompt or "").strip().splitlines()[0][:60],
                               submitter="cli", status=JOB_RUNNING)
        # 2026-11-05：gen 是 AI 最常用的那条路，但它不走队列的 claim()，
        # 所以 ran_on 一直是空的 —— 台账"实际在谁那儿跑的"这列对 gen 出的图永远显示不出来。
        from . import __version__ as _ver
        job.ran_on = acc.id
        job.client_version = _ver        # 排查时一眼看出是哪版出的问题
        store.save(job)
        return store, job, acc
    except Exception:
        return None


def _parse_reset_at(text) -> float:
    """把远端给的"什么时候恢复"解析成时间戳。解析不出来返回 0（上层就不设冷却）。"""
    import time as _t
    s = str(text or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return _t.mktime(_t.strptime(s[:len(_t.strftime(fmt, _t.localtime()))], fmt))
        except Exception:
            continue
    return 0.0


def _gen_record_end(cfg, rec, r: dict) -> None:
    """单条 gen 收尾：成功记 +1，撞上限记冷却。

    ★ 撞上限必须回写账号状态。以前这里只处理成功分支，导致命令行出的图撞了上限，
    面板上仍然显示"撞上限 0 次、没在冷却"，用户会一直重投、一直失败
    （2026-11-04 反馈包：acc2 连续 4 单成功后第 5 单撞上限，账号页却显示 0 次）。
    """
    if not rec:
        return
    try:
        import shutil

        from .core.models import ITEM_FAILED, ITEM_OK, JOB_DONE, JOB_FAILED

        store, job, acc = rec
        job = store.load(job.id) or job
        it = job.items[0]
        if not r.get("ok") and r.get("kind") == "quota":
            # 撞上限：记一次 + 按远端给的时间设冷却，让面板和下一次派单都看得见
            try:
                st = store.account(acc.id)
                st.quota_hits = (st.quota_hits or [])[-19:] + [{
                    "at": __import__("time").time(),
                    "after_n": st.stat_today,
                    "job": job.id,
                }]
                until = _parse_reset_at(r.get("reset_at"))
                if until:
                    st.cooldown_until = until
                store.save_account(st)
            except Exception:
                pass

        if r.get("ok") and r.get("path"):
            src = Path(r["path"])
            name = "S01" + (src.suffix or ".png")   # GIF 要保住 .gif，面板才认得出
            dst = store.dir_of(job.id) / name
            shutil.copyfile(src, dst)      # 面板从单子目录里读图
            it.status, it.files = ITEM_OK, [name]
            it.reason = str(r.get("note") or "")[:300]
            job.quality_note = str(r.get("note") or "")[:400]
            job.status = JOB_DONE
            store.bump_account(acc.id, 1)
        else:
            it.status = ITEM_FAILED
            it.error_code = _KIND_CODE.get(r.get("kind") or "", "GENERATE_FAILED")
            job.status = JOB_FAILED
            job.error_code = it.error_code
            if it.error_code == "TIMEOUT_UNCERTAIN":
                job.uncertain = True
            # 失败必须留下原因。以前这里只写 error_code，日志尾巴直接丢了，
            # 面板上只剩一个"失败"——用户根本不知道是网络、验证还是提示词
            # （2026-11-03 用户问"为什么不标失败原因"）。
            _tail = str(r.get("tail") or "")
            from . import why as _why

            it.reason = str(r.get("reason") or "") or _why.explain(_tail, r.get("kind") or "")
            it.failure_class = _why.classify(_tail, it.error_code or "").cls
            it.note = _tail[-400:]
            it.log_path = str(r.get("log") or "")
            job.reason = it.reason
            job.note = it.note
        # 连续失败计数：gen 直出不经过 Coordinator，必须在这里自己维护。
        # 否则外部 Agent 反复重投 `bampiao gen` 时，坏号 stat_today 不涨、
        # 永远"今天出图最少"被优先选中（2026-11-11 用户反馈）。
        # quota / auth 已有专门的冷却与停用状态，不重复累加，避免双重惩罚。
        try:
            st = store.account(acc.id)
            if r.get("ok") and r.get("path"):
                st.fail_streak = 0
            elif r.get("kind") not in ("quota", "auth"):
                st.fail_streak = int(getattr(st, "fail_streak", 0) or 0) + 1
                st.last_fail_at = time.time()
            store.save_account(st)
        except Exception:
            pass
        job.finished_at = time.time()
        store.save(job)
    except Exception:
        pass


def _store_of(a):
    """加载配置并返回 (cfg, Store)。"""
    from .core.store import Store

    cfg = cfg_mod.load(a.config)
    return cfg, Store(cfg)


def _gif_prompt(p: str) -> str:
    """要动图时补一句。模型不会读心，明说比事后失望便宜。"""
    t = p or ""
    if "GIF" in t.upper() or "动图" in t or "循环" in t:
        return t
    return t.rstrip() + "\n（输出为 2–3 秒无缝循环的 GIF 动图，不要静态图。）"


def _svg_prompt(p: str, anim: bool = False) -> str:
    """要 SVG 文件时补一句。

    2026-11-07 网页版实测：只说"画个图标"会出位图；只说"SVG"它可能
    贴一整段代码块。明确要「.svg 文件附件」才会给文件卡片。要动效还得
    点名 SMIL / @keyframes，否则给的是静态矢量图。
    """
    t = p or ""
    need = ["请把结果作为单个 .svg 文件附件交付，不要只贴代码块、不要给文字描述、"
            "不要导出成 PNG 或 GIF"]
    if "svg" not in t.lower() and "矢量" not in t:
        need.insert(0, "输出为原生 SVG 矢量文件（SVG 1.1 XML）")
    if anim:
        need.append("文件内部用 SMIL <animate>/<animateTransform> 或 CSS @keyframes "
                    "实现无缝循环动效，透明背景，动画全部内嵌在这一个 .svg 里，不依赖外部资源")
    return t.rstrip() + "\n（" + "；".join(need) + "。）"


def _svg_flags(a) -> tuple[bool, bool]:
    """(要不要 svg, 要不要动效)。``--svg-anim`` 隐含 ``--svg``。"""
    anim = bool(getattr(a, "svg_anim", False))
    return (bool(getattr(a, "svg", False)) or anim), anim


def _items_from(a) -> list:
    from .core.models import Item

    svg, svg_anim = _svg_flags(a)
    if svg and getattr(a, "gif", False):
        raise cfg_mod.ConfigError(
            "--svg / --svg-anim 不能和 --gif 一起用：一个要矢量文件，一个要位图动图；"
            "要会动的矢量图用 --svg-anim。")
    alpha = bool(a.alpha) and not svg     # SVG 天然透明，抠图对它没意义

    def _prep(p, *, anim=False, is_svg=False, sa=False):
        if anim:
            p = _gif_prompt(p)
        if is_svg:
            p = _svg_prompt(p, sa)
        return p

    if a.shots:
        # `--shots 4`（张数）和 `--shots shots.json`（清单）都要认。
        # 2026-10-29 实测：PASTE.md 教用户/Agent 写的是 `--shots 4`，
        # 而实现只接受文件路径 → 报 "No such file or directory: '4'"，
        # 也就是"我们让 Agent 用的那条命令根本跑不通"。这条属于核心链路，必须两种都收。
        val = str(a.shots).strip()
        if val.isdigit():
            n = int(val)
            if n <= 0:
                raise cfg_mod.ConfigError("--shots 张数要大于 0")
            if not (a.prompt or "").strip():
                raise cfg_mod.ConfigError("--shots %d 需要配一句提示词：bampiao submit \"画面\" --shots %d" % (n, n))
            _p = _prep(a.prompt.strip(), anim=bool(getattr(a, "gif", False)),
                       is_svg=svg, sa=svg_anim)
            return [Item(id="S%02d" % i, prompt=_p,
                         aspect=a.aspect, alpha=alpha,
                         anim=bool(getattr(a, "gif", False)),
                         svg=svg, svg_anim=svg_anim)
                    for i in range(1, n + 1)]

        raw = json.loads(Path(val).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise cfg_mod.ConfigError("--shots 文件应该是一个数组")
        out = []
        for i, x in enumerate(raw, 1):
            if not isinstance(x, dict) or not str(x.get("prompt") or "").strip():
                raise cfg_mod.ConfigError("第 %d 条缺少 prompt" % i)
            it = Item.from_dict(x)
            if not it.id:
                it.id = "S%02d" % i
            if getattr(a, "gif", False):
                it.anim = True
                it.prompt = _gif_prompt(it.prompt)
            # 命令行 --svg 覆盖清单；清单里自己写了 svg 的也认
            it.svg = bool(svg or it.svg)
            it.svg_anim = bool(svg_anim or it.svg_anim)
            if it.svg_anim:
                it.svg = True
            if it.svg and getattr(a, "gif", False):
                raise cfg_mod.ConfigError("第 %d 条同时要 SVG 和 GIF，只能二选一" % i)
            if it.svg:
                it.prompt = _svg_prompt(it.prompt, it.svg_anim)
                it.alpha = False
            out.append(it)
        return out

    if not a.prompt:
        raise cfg_mod.ConfigError("给个提示词，或者用 --shots 投一批")
    _p = _prep(a.prompt, anim=bool(getattr(a, "gif", False)),
               is_svg=svg, sa=svg_anim)
    return [Item(id="S01", prompt=_p, aspect=a.aspect, alpha=alpha,
                 anim=bool(getattr(a, "gif", False)),
                 svg=svg, svg_anim=svg_anim)]


def desktop_exe() -> Path | None:
    """装好的桌面端可执行文件；没有就 None（源码环境跑测试时会没有）。"""
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        return None
    p = Path(base) / "Bampiao" / "bampiao-desktop.exe"
    return p if p.exists() else None


def desktop_running() -> bool:
    """桌面端在不在跑：以 instance.json 里的 pid 为准（进程在 + 端口有人听）。"""
    p = paths.program_dir() / "instance.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        pid = int(d.get("pid") or 0)
        # 注意：instance.json 里**没有 port 字段**，只有 panel_url / show_port。
        # 之前按 "port" 读，永远拿到 0 → 明明起来了却判"没在跑"（2026-11-03 实测）。
        port = int(d.get("port") or 0)
        if not port:
            url = str(d.get("panel_url") or "")
            if "://" in url:
                try:
                    port = int(url.rstrip("/").split("://", 1)[1].split(":")[1].split("/")[0])
                except Exception:
                    port = 0
        if not port:
            port = int(d.get("show_port") or 0)
    except Exception:
        return False
    if not pid or not port:
        return False
    # 不能用 os.kill(pid, 0) 判活：Windows 上它抛 OSError(WinError 87)，
    # 会被当成"进程已死"——2026-11-03 实测，明明在跑却一直提示手动启动。
    try:
        from .platform import pid_alive
    except Exception:
        pid_alive = None
    if pid_alive is not None and not pid_alive(pid):
        return False
    import socket

    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def ensure_desktop(timeout: float = 20.0) -> str:
    """队列要有人跑。桌面端没开就自己拉起来，别让用户/AI 手动去开。

    返回：running（本来就在）/ started（刚拉起）/ no-exe（没装）/ timeout。
    刻意不抛异常：投单已经成功了，拉不起桌面端只是"跑得慢"，不该让整条命令失败。
    """
    if desktop_running():
        return "running"
    exe = desktop_exe()
    if not exe:
        return "no-exe"
    try:
        subprocess.Popen([str(exe)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception:
        return "timeout"
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if desktop_running():
            return "started"
    return "timeout"


def _should_spread(a, items) -> bool:
    """这一单要不要拆成 N 单，好让多个号同时跑。

    只拆一种情况：``--shots 4``（同一句提示词的 4 个变体）。
    它们彼此没有先后依赖，拆开是纯赚。

    不拆的两种情况：
    - ``--shots shots.json`` 清单：可能是一组分镜，后一张要拿前一张当参考图，
      拆开会被别的 worker 提前跑掉（顺序保证比快几秒重要）。
    - ``--account acc1``：人都明确指定号了，就按他说的来。

    2026-11-03 用户问"两个号为什么只出一个"：一单里的多张以前是**串行**跑在
    同一个号上的，拆成多单后由调度器分给不同号，才真正并行。
    """
    if len(items) <= 1:
        return False
    if getattr(a, "serial", False) or getattr(a, "account", None):
        return False
    return str(getattr(a, "shots", "") or "").strip().isdigit()


def _spread_accounts(cfg, n: int) -> list:
    """把 n 张图分给账号：先按策略权重**排一次序**，再轮流发。

    排序用和 config.choose_by_strategy 同一套权重（连续失败 > 档位 > 今天用量 > id），
    但这里是「排一次序再轮流」，不是「每张都重挑最优」——否则 tier 策略下
    n 张图会全压到同一个 Pro 号上，同一个号并发会串图（AGENTS.md 明令禁止）。
    """
    try:
        from .core.store import Store

        st = Store(cfg)

        def weight(a):
            s = st.account(a.id)
            p = str(s.plan or "").lower()
            tier = (0 if "pro" in p else 1 if "plus" in p
                    else 3 if "free" in p else 2)
            return (s.fail_streak_now(), tier, s.today_count(), a.id)
    except Exception:
        def weight(a):
            return (0, 2, 0, a.id)
    order = sorted(cfg.accounts, key=weight)
    return [order[i % len(order)] for i in range(n)] if order else []


def cmd_submit(a) -> int:
    from .core.store import StoreError

    if getattr(a, "prompt_file", None):
        try:
            a.prompt = Path(a.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            _err("读不到提示词文件：%s" % e, code="BAD_USAGE")
            return EXIT_USAGE

    try:
        cfg, store = _store_of(a)
        acc = _pick_account(cfg, a.account)      # 顺带校验：没账号时在这里就报清楚
        items = _items_from(a)
        made = []
        if _should_spread(a, items):
            # 同一句提示词 × N：拆成 N 单，调度器才能分给多个号并行。
            base = str(a.request_id or "").strip() or ("r%d" % int(time.time() * 1000))
            for i, (it, ac) in enumerate(zip(items, _spread_accounts(cfg, len(items))), 1):
                j, created_i = store.new_job(account_id=ac.id, items=[it], brief=a.brief,
                                             tag=a.tag, submitter=a.submitter,
                                             request_id="%s#%d" % (base, i))
                made.append((j, created_i, ac))
            job, created = made[0][0], all(m[1] for m in made)
        else:
            job, created = store.new_job(account_id=acc.id, items=items, brief=a.brief,
                                         tag=a.tag, submitter=a.submitter,
                                         request_id=a.request_id)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    except (OSError, ValueError, StoreError) as e:
        _err("投单失败：%s" % e)
        return EXIT_INTERNAL

    if a.json:
        payload = {"schema_version": 1, "ok": True, "created": created,
                   "job": job.id, "account": acc.id, "items": len(items),
                   "request_id": job.request_id}
        if made:
            payload.update({"jobs": [m[0].id for m in made],
                            "accounts": [m[2].id for m in made],
                            "split": True, "concurrency": cfg.concurrency})
        _emit(payload)
    else:
        if made:
            ids = [m[0].id for m in made]
            names = "、".join(dict.fromkeys(m[2].name for m in made))
            span = ("R%d–R%d" % (ids[0], ids[-1])) if len(ids) > 1 else ("R%d" % ids[0])
            print("已投 %s（%d 张，拆成 %d 单，分给 %s）" % (span, len(ids), len(ids), names))
            print("当前并发 %d：最多同时用 %d 个号跑"
                  % (cfg.concurrency, min(cfg.concurrency, len(ids))))
        else:
            print(("已投 R%d（%d 张，账号 %s）" if created else "R%d 已存在，没有重复投（%d 张，账号 %s）")
                  % (job.id, job.expected, acc.name))
        # 投完就把队列跑起来：以前这里只说"下一步 run --once"，
        # 实际结果是单子躺在队列里等到天荒地老（用户以为软件坏了）。
        how = ensure_desktop()
        if how == "running":
            print("队列已在跑（托盘常驻），会自动接这单")
        elif how == "started":
            print("已经帮你把半瓢拉起来了，它会自动跑这单")
        elif how == "no-exe":
            print("下一步：%s run --once   （没找到桌面端，只能在当前终端跑）" % _self())
        else:
            print("下一步：%s run --once   （桌面端没起来，手动跑一次）" % _self())
    return EXIT_OK


def gen_in_progress(cfg):
    """给 recover_running 用：这个号的出图锁现在有没有人拿着。"""
    from . import locks
    from .driver import browser as br

    return lambda aid: not locks.try_acquire(br.gen_lockfile(cfg, aid))


def _keep_windows_hidden(cfg) -> None:
    """AI 直接调命令行出图时，桌面壳可能根本没开，没人替它藏窗口。

    出图期间在本进程里也挂一个巡逻（daemon 线程，命令结束它就没了）。
    """
    import threading

    from .driver import browser as br

    threading.Thread(target=br.keep_hidden, args=(lambda: cfg, threading.Event()),
                     daemon=True, name="window-keeper").start()


def _usable_accounts(cfg, store) -> list:
    """现在真正能派单的号（没冷却、没掉登录）。给 jobs --json 报"还有几个号能用"。"""
    out = []
    for acc in cfg.accounts:
        try:
            st = store.account(acc.id)
            if st.cooling() or st.login == "required":
                continue
            out.append(acc)
        except Exception:
            continue
    return out


def _skip_reasons(cfg, store) -> list[str]:
    """队列里有单却没跑：逐个账号说为什么，带上下一步命令。"""
    out = []
    for acc in cfg.accounts:
        st = store.account(acc.id)
        if st.cooling():
            left = max(0, round((st.cooldown_until - __import__("time").time()) / 60))
            out.append("%s（%s）额度用完在休息，还要约 %d 分钟" % (acc.name, acc.id, left))
        elif st.login != "ok":
            out.append("%s（%s）没登录：让用户跑 %s login %s，或在面板「账号」页点登录"
                       % (acc.name, acc.id, _self(), acc.id))
    return out


def cmd_run(a) -> int:
    from .core.coordinator import Coordinator

    try:
        cfg, store = _store_of(a)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    # 升级流程留下的暂停不该拦住用户手动的 bampiao run
    # （面板路径重启时有 desktop 收尾，纯 CLI 用户没人管，会看到 ok:true done:0 停摆）
    try:
        if store.clear_update_pause() and not a.json:
            print("上次升级留下的暂停已解除")
    except Exception:
        pass
    co = Coordinator(cfg, store)
    _keep_windows_hidden(cfg)
    # 上次进程被杀/断电时留在 running 的单，先标不确定——不自动重跑，避免重复烧额度
    _back = store.requeue_unstarted(alive=gen_in_progress(cfg), min_age=90)
    if _back and not a.json:
        print("有单被领走但一项都没开始，已放回队列：%s" % ", ".join("R%d" % i for i in _back))
    leftover = store.recover_running(alive=gen_in_progress(cfg))
    if leftover and not a.json:
        print("上一轮有 %d 单没收尾（R%s），已标成\"不确定\"，确认后再重投。"
              % (len(leftover), ", R".join(str(x) for x in leftover)))
    if a.once:
        try:
            r = co.tick()
        except Exception as e:      # 队列跑挂了要给人话，不能吐 traceback
            if a.json:
                _emit({"schema_version": 1, "ok": False, "why": "%s: %s" % (type(e).__name__, e)})
            else:
                _err("跑队列出错：%s: %s" % (type(e).__name__, e))
            return EXIT_INTERNAL
        why = _skip_reasons(cfg, store) if r["skipped"] else []
        if a.json:
            # 只给 skipped 个数，AI 会把 ok:true 当成"跑完了"。必须说清楚卡在哪、下一步干什么。
            _emit({"schema_version": 1, "ok": True, **r,
                   **({"why": why} if why else {})})
        else:
            print("完成 %d 单 ｜ 失败 %d 单 ｜ 因账号不可用跳过 %d 单"
                  % (r["done"], r["failed"], r["skipped"]))
            for w in why:
                print("  " + w)
        if r["done"] == 0 and r["failed"] == 0:
            return EXIT_OK if not r["skipped"] else EXIT_ACTION
        return EXIT_OK if r["failed"] == 0 else EXIT_PARTIAL

    if not a.json:
        print("半瓢开始常驻跑队列（Ctrl+C 停）。当前并发 %d。" % co.concurrency)
    try:
        co.run_forever(poll=a.poll)
    except KeyboardInterrupt:
        if not a.json:
            print("\n已停止。队列里的任务还留着，下次 run 会接着跑。")
        return EXIT_OK
    return EXIT_OK


def cmd_jobs(a) -> int:
    try:
        cfg, store = _store_of(a)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    lim = max(1, min(200, int(a.limit)))
    d = store.query(limit=lim, offset=max(0, int(a.offset)), status=a.status,
                    account=a.account, tag=a.tag, q=a.q)
    if a.json:
        # 2026-11-14：给 Agent 一次看全。每条补「还差几张 / 卡在哪」，
        # 让 Agent 一条 jobs --json 就能判断要不要等、要不要补派，别重复派单烧额度。
        rows = []
        for j in d.get("rows") or []:
            dd = j.as_dict() if hasattr(j, "as_dict") else dict(j)
            pend = [it for it in (dd.get("items") or [])
                    if it.get("status") in ("queued", "running")]
            dd["pending_items"] = len(pend)
            # 卡在哪：queued 且还有没跑的 → 看有没有号在冷却。其它状态也给默认值，
            # 免得 Agent 读缺键报错（2026-11-14：子代理验证建议所有行都带这俩键）。
            dd.setdefault("waiting_reason", "")
            dd.setdefault("accounts_left", None)
            if dd.get("status") == "queued" and pend:
                try:
                    usable = len(_usable_accounts(cfg, store))
                except Exception:
                    usable = 0
                dd["waiting_reason"] = "quota" if usable == 0 else "pending"
                dd["accounts_left"] = usable
            rows.append(dd)
        d["rows"] = rows
        _emit({"schema_version": 1, "ok": True, **d})
        return EXIT_OK

    if not d["rows"]:
        print("没有匹配的任务。")
        return EXIT_OK
    print("共 %d 单（第 %d–%d）" % (d["total"], d["offset"] + 1,
                                  d["offset"] + len(d["rows"])))
    for j in d["rows"]:
        # 符号只用 cp936 里也有的：√ × → · ｜ 都能编码，管道里不会变成问号。
        mark = {"done": "√", "partial": "!", "failed": "×",
                "running": "→", "queued": "·", "cancelled": "-"}.get(j["status"], "?")
        print(" %s R%-4d %-8s %s/%s  %-10s %s"
              % (mark, j["id"], j["status"], j["rendered"], j["expected"],
                 j.get("ran_on") or j["account_id"], (j["brief"] or "")[:34]))
    return EXIT_OK


def cmd_show(a) -> int:
    try:
        cfg, store = _store_of(a)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    job = store.load(int(a.job_id))
    if not job:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": "没有 R%d" % a.job_id})
        else:
            _err("没有 R%d 这一单" % a.job_id)
        return EXIT_USAGE

    # 文本单的结果是那段回复：从 S01.txt 读全文（老单没有文件就退回 note）
    reply = ""
    if (job.kind or "image") == "ask":
        for it in job.items:
            try:
                reply = (store.dir_of(job.id) / ("%s.txt" % it.id)) \
                    .read_text(encoding="utf-8")
                break
            except OSError:
                continue
        if not reply:
            reply = job.note or ""

    if a.json:
        payload = {"schema_version": 1, "ok": True, "job": job.as_dict()}
        if (job.kind or "image") == "ask":
            payload["text"] = reply
        _emit(payload)
        return EXIT_OK

    print("R%d  %s  %s/%s  账号 %s"
          % (job.id, job.status, job.rendered, job.expected,
             job.ran_on or job.account_id))
    if job.ran_on and job.ran_on != job.account_id:
        print("（原定 %s，那个号当时在忙，实际由 %s 跑）"
              % (job.account_id, job.ran_on))
    if job.brief:
        print("说明：%s" % job.brief)
    if job.error_code:
        print("错误：%s" % job.error_code)
    if job.uncertain:
        print("注意：这单有\"不确定\"的条目，远端可能已经出图，自动重投会重复出图。")
    if reply:
        print("回复：")
        print(reply)
    for it in job.items:
        print("  %-5s %-8s %-22s %s"
              % (it.id, it.status, (it.error_code or "-"),
                 (it.prompt or "").splitlines()[0][:30]))
    return EXIT_OK


def cmd_usage(a) -> int:
    try:
        cfg, store = _store_of(a)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    import time as _t

    rows = []
    for acc in cfg.accounts:
        st = store.account(acc.id)
        left = max(0.0, (st.cooldown_until or 0) - _t.time())
        rows.append({
            "account": acc.id, "name": acc.name, "browser": acc.browser,
            "login": st.login, "today": st.images_today(_t.strftime("%Y-%m-%d")),
            "cooling": st.cooling(),
            "cooldown_left_min": round(left / 60) if left else 0,
            "quota_hits": len(st.quota_hits),
            "quota_today": st.quota_hits_on(_t.strftime("%Y-%m-%d")),
            "last_hit_after_n": (st.quota_hits[-1]["after_n"] if st.quota_hits else None),
        })

    if a.json:
        _emit({"schema_version": 1, "ok": True, "accounts": rows})
        return EXIT_OK

    if not rows:
        print("还没有账号。")
        return EXIT_OK
    for r in rows:
        state = "冷却中（还剩 %d 分钟）" % r["cooldown_left_min"] if r["cooling"] else "可用"
        login = {"ok": "已登录", "required": "需要登录", "unknown": "未探测"}[r["login"]]
        print(" %-6s %-10s %-14s 今天 %d 张 ｜ %s ｜ 今天撞上限 %d 次（近 20 次里共 %d）"
              % (r["account"], r["name"], login, r["today"], state,
                 r.get("quota_today") or 0, r["quota_hits"]))
    return EXIT_OK


def _open_window() -> int:
    """打开半瓢窗口（和双击桌面图标等价的路径）。

    网页版那套壳已经删了：面板就是那个原生窗口，图片预览、抽屉、灯箱都在里面。
    要纯 HTTP（自动化/无图形环境）请用 `bampiao ui --serve`。
    """
    import subprocess
    import sys

    flags = 0x08000000 if sys.platform == "win32" else 0   # DETACHED_PROCESS（仅 Windows）

    # 候选：Windows=bampiao-desktop.exe；mac 打包版=bampiao-gui。
    # 用 autostart.desktop_exe() 统一口径（它已含 bampiao-gui），再补邻近目录扫描。
    cands: list[Path] = []
    try:
        from . import autostart as _auto
        _de = _auto.desktop_exe()
        if _de:
            cands.append(Path(_de))
    except Exception:
        pass
    exe = Path(sys.executable)
    names = ["bampiao-desktop.exe"] if sys.platform == "win32" else ["bampiao-gui", "bampiao-desktop"]
    for nm in names:
        cands.append(exe.with_name(nm))
        cands.append(exe.parent / nm)

    seen = set()
    for cand in cands:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if cand.exists():
            try:
                subprocess.Popen([str(cand)], close_fds=True, creationflags=flags,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                print("正在打开半瓢窗口…（已经在跑的话会直接叫到前面）")
                return EXIT_OK
            except Exception as e:
                _err("打不开窗口：%s" % e)
                return EXIT_INTERNAL

    # 源码运行（非冻结）才退到模块入口。冻结包里 sys.executable 是 CLI 本身，
    # 再 `-m image2_bampiao.desktop` 只会被 argparse 判成非法子命令（mac 实测坑）。
    if not getattr(sys, "frozen", False):
        try:
            subprocess.Popen([sys.executable, "-m", "image2_bampiao.desktop"],
                             close_fds=True, creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            print("正在打开半瓢窗口…")
            return EXIT_OK
        except Exception as e:
            _err("打不开窗口：%s。可以用 bampiao ui --serve 起网页服务。" % e)
            return EXIT_INTERNAL

    _err("找不到图形界面程序（bampiao-gui / bampiao-desktop），请确认安装完整；或先用 bampiao ui --serve。")
    return EXIT_INTERNAL


def _install_dir() -> Path | None:
    """冻结版（安装包）的安装目录；源码跑时返回 None。

    返回 None 是为了不误伤：源码跑的时候 sys.executable 是 python.exe，
    按它的目录去"收掉半瓢进程"会把自己的 python 也算进去。
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def quit_leftovers() -> int:
    """把安装目录里还活着的半瓢进程收掉，返回收掉了几个。

    为什么需要它：安装器覆盖升级要替换 {app}\\bampiao.exe。真实场景是用户在批量跑
    `bampiao gen`——这些进程没有窗口、不响应 Restart Manager，Inno 的 CloseApplications
    对它们无效，于是向导弹「尝试替换已存在的文件时出错：DeleteFile 失败；错误代码 5。
    拒绝访问。」（2026-10-30 在用户机器上实测）。
    所以升级/卸载前由半瓢自己把残留收干净，比指望安装器去关更可靠。
    """
    base = _install_dir()
    if base is None:
        return 0
    from . import platform as plat

    killed = 0
    for pid in plat.find_pids_by_exe(base):
        try:
            if plat.kill_tree(pid):
                killed += 1
        except Exception:  # noqa: BLE001 —— 收不掉也要继续，别把 quit 本身弄崩
            continue
    return killed


def cmd_windows(a) -> int:
    """列出账号浏览器进程的所有窗口，并标出哪些会出现在任务栏。

    为什么要有这条：用户截图里任务栏冒出 `MSCTFIME UI` / `Default IME` 两个按钮，
    那是 Chrome 的**输入法 UI 窗口**——没有标题，普通"列窗口"根本看不见它们。
    出问题时跑一次 `bampiao windows`，把这类幽灵窗口照出来，比猜快得多。
    """
    try:
        cfg = cfg_mod.load(a.config)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    from . import platform as plat

    want = getattr(a, "account", None)
    accs = [x for x in cfg.accounts if not want or want in (x.id, x.name)]
    if not accs:
        _err("没有这个账号：%s" % want)
        return EXIT_USAGE

    rows = []
    for acc in accs:
        for pid in plat.find_pids_with(str(acc.profile)):
            for w in plat.list_windows(pid):
                rows.append((acc.id, pid, w))

    if a.json:
        _emit({"schema_version": 1, "ok": True,
               "windows": [dict(account=acc, pid=pid, **w) for acc, pid, w in rows]})
        return EXIT_OK
    if not rows:
        print("没找到运行中的账号浏览器（先 bampiao browser up，或 open 面板点「打开浏览器」）")
        return EXIT_OK

    print("%-6s %-7s %-22s %-6s %-7s %-6s %s"
          % ("账号", "pid", "窗口类名", "可见", "工具窗", "任务栏", "标题"))
    ghost = []
    for acc, pid, w in rows:
        mark = "是" if w["taskbar"] else ""
        print("%-6s %-7s %-22s %-6s %-7s %-6s %s"
              % (acc, pid, w["class"][:22], "是" if w["visible"] else "否",
                 "是" if w["toolwindow"] else "否", mark, (w["title"] or "")[:34]))
        if w["taskbar"] and not w["title"]:
            ghost.append(w)
    if ghost:
        print("\n注意：上面有 %d 个**没有标题却占着任务栏**的窗口（多半是输入法 UI）。"
              % len(ghost))
        print("把它们报给我，我按类名收掉。")
    return EXIT_OK


def cmd_upgrade_guard(a) -> int:
    """给安装器用：现在能不能装（有没有单在跑）。

    退出码 0 = 可以装；3 = 有任务在跑，别装（安装器会把这句话当错误显示并中止）。
    安装器是 Inno Pascal，判断不了"半瓢是不是在出图"，所以让半瓢自己回答。
    """
    try:
        cfg = cfg_mod.load(a.config)
    except cfg_mod.ConfigError:
        return EXIT_OK          # 配置都没有，更不可能有单在跑
    store = None
    try:
        from .core.store import Store

        store = Store(cfg)
    except Exception:
        pass
    from . import update

    why = update.busy_reason(cfg, store)
    if a.json:
        _emit({"schema_version": 1, "ok": not why, "why": why})
    elif why:
        print(why)
    return EXIT_ACTION if why else EXIT_OK


def cmd_quit(a) -> int:
    """让正在跑的那个半瓢退出。

    托盘图标在 Windows 上默认被收进 ^ 里，用户经常找不到；菜单点不动、
    或者进程卡住时也需要一条出口。常驻程序"关不掉"是硬伤——
    用户最后只能开任务管理器，那会让他觉得这软件很脏。
    """
    from . import desktop

    ok, msg = desktop.send_command("quit", wait=20)
    if not ok:
        # socket 找不到人（比如队列 worker 起在别的数据目录、或者根本没起壳），
        # 但安装目录里确实还有进程活着 —— 收掉它们，别让用户卡在"没在运行"这句话上。
        n = quit_leftovers()
        if n:
            ok, msg = True, "已收掉 %d 个没响应的半瓢进程" % n
    if a.json:
        _emit({"schema_version": 1, "ok": ok, "why": msg})
        return EXIT_OK if ok else EXIT_ACTION
    print(msg)
    return EXIT_OK if ok else EXIT_ACTION


def cmd_ui(a) -> int:
    import time as _t

    from .ui import server as ui_server

    try:
        cfg = cfg_mod.load(a.config)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    if not getattr(a, "serve", False):
        return _open_window()

    try:
        httpd, port = ui_server.serve(cfg, a.port, block=False)
    except OSError as e:
        _err("端口起不来：%s。换一个：bampiao ui --port 18800" % e)
        return EXIT_ACTION

    url = "http://127.0.0.1:%d/" % port
    if a.json:
        _emit({"schema_version": 1, "ok": True, "url": url, "port": port})
    else:
        # flush：输出被重定向到文件/管道时是块缓冲的，不刷就看不到"面板在哪"，
        # 用户会以为卡住了（真踩过）。
        print("面板：%s" % url, flush=True)
        print("（Ctrl+C 关掉；关掉浏览器不会停服务）", flush=True)
    # --serve 是给自动化/无头环境用的：**不许**自己开浏览器。
    # 之前这里 webbrowser.open 过一次，被循环调用就成了 727 个标签页。

    try:
        while True:
            _t.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()
        if not a.json:
            print("\n面板已关闭。队列里的任务不受影响。")
    return EXIT_OK


def _read_password(a, confirm: bool) -> str:
    """取口令：环境变量 → 参数 → 交互输入。

    环境变量优先是为了让 agent / 脚本能用；明文 `--password` 会留在命令行历史里，
    所以只在没有环境变量时用它，并且打个提醒。
    """
    import getpass
    import os as _os

    pwd = _os.environ.get("BAMPIAO_BACKUP_PASSWORD") or ""
    if pwd:
        return pwd
    if getattr(a, "password", ""):
        _err("提醒：--password 会留在命令历史里，正式使用建议改用 BAMPIAO_BACKUP_PASSWORD")
        return a.password
    pwd = getpass.getpass("备份口令：")
    if confirm:
        again = getpass.getpass("再输一次：")
        if pwd != again:
            raise cfg_mod.ConfigError("两次输入不一致")
    if not pwd:
        raise cfg_mod.ConfigError("口令不能为空（空口令就等于明文保存登录态）")
    return pwd


def cmd_backup(a) -> int:
    from . import backup as bk

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account)
        pwd = _read_password(a, confirm=(a.action == "export"))
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    try:
        if a.action == "export":
            if not a.out:
                _err("导出要指定 --out 文件路径")
                return EXIT_USAGE
            r = bk.export(cfg, acc, Path(a.out), pwd)
            if a.json:
                _emit({"schema_version": 1, "ok": True, "action": "export", **r})
            else:
                m = r["meta"]
                print("已导出 %s" % r["path"])
                print("  账号 %s ｜ 用户 %s ｜ cookie %d 条 ｜ %s"
                      % (acc.name, m.get("user") or "?", m.get("cookies") or 0,
                         time.strftime("%m-%d %H:%M", time.localtime(m.get("exported_at") or 0))))
                print("  这个文件等于登录凭据，别传群里、别放仓库。")
            return EXIT_OK

        if not a.file:
            _err("导入要给备份文件路径")
            return EXIT_USAGE
        r = bk.restore(cfg, acc, Path(a.file), pwd, force=a.force)
        if a.json:
            _emit({"schema_version": 1, "ok": True, "action": "import", **r})
        else:
            print("已恢复 %s 的登录态（用户 %s，套餐 %s）"
                  % (acc.name, r["user"] or "?", r["plan"] or "?"))
            print("下一步：%s probe %s 复核一次" % (_self(), acc.id))
        return EXIT_OK
    except bk.BackupError as e:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": str(e)})
        else:
            _err(str(e))
        return EXIT_ACTION
    except Exception as e:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": "%s: %s" % (type(e).__name__, e)})
        else:
            _err("备份/恢复出错：%s: %s" % (type(e).__name__, e))
        return EXIT_INTERNAL


def cmd_autostart(a) -> int:
    from . import autostart as auto

    try:
        if a.action == "on":
            cmd = auto.enable(Path(a.exe) if a.exe else None, extra=a.extra)
            if a.json:
                _emit({"schema_version": 1, "ok": True, "enabled": True, "command": cmd})
            else:
                print("已设为开机自启：%s" % cmd)
                if sys.platform == "darwin":
                    print("（写的是 ~/Library/LaunchAgents 里的 LaunchAgent，"
                          "登录后自动起，图标在菜单栏）")
                else:
                    print("（写的是 HKCU 的 Run 项，登录后自动起托盘，不弹黑框）")
            return EXIT_OK

        if a.action == "off":
            had = auto.disable()
            if a.json:
                _emit({"schema_version": 1, "ok": True, "enabled": False, "was": had})
            else:
                print("已关闭开机自启" if had else "本来就没开")
            return EXIT_OK

        st = auto.status()
        if a.json:
            _emit({"schema_version": 1, "ok": True, **st})
        else:
            if not st["enabled"]:
                print("开机自启：未开启")
            else:
                print("开机自启：已开启")
                print("  命令：%s" % st["command"])
                if not st.get("exists"):
                    print("  注意：这个文件已经不在了，重新跑一次 autostart on")
        return EXIT_OK
    except NotImplementedError as e:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": str(e)})
        else:
            _err(str(e))
        return EXIT_ACTION
    except auto.AutostartError as e:
        if a.json:
            _emit({"schema_version": 1, "ok": False, "why": str(e)})
        else:
            _err(str(e))
        return EXIT_ACTION


def cmd_diag(a) -> int:
    """把所有排障信息写成一个文件，交给别人看。

    为什么要这个命令，而不是让用户 `doctor | Out-File`：
    Windows PowerShell 5.1 用管道接原生命令时按系统码页解码，UTF-8 的中文会变成乱码，
    而且 `doctor` 里的 ✓ 在 GBK 下会直接崩。**由程序自己写文件**就绕开了整条链路。

    内容**不含** cookie、token、口令、邮箱；账号只出 id/名字/浏览器/端口/状态。
    """
    import platform as _pf

    out = Path(a.out).expanduser() if a.out else (paths.data_dir() / "bampiao-diag.txt")
    lines: list[str] = []
    try:
        from . import crashlog as _cl

        _cr = _cl.list_crashes(cfg.data_dir)
        lines.append("崩溃记录: %d 条" % len(_cr))
        for it in _cr[:5]:
            lines.append("  %s  %d 字节" % (Path(it["file"]).name, it["size"]))
    except Exception:
        pass
    add = lines.append

    add("半瓢诊断报告")
    add("=" * 40)
    add("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("版本：%s" % __version__)
    add("系统：%s %s (%s)" % (_pf.system(), _pf.release(), _pf.machine()))
    add("Python：%s" % _pf.python_version())
    add("可执行：%s" % sys.executable)
    add("编码：stdout=%s stderr=%s" % (
        getattr(sys.stdout, "encoding", "?"), getattr(sys.stderr, "encoding", "?")))
    add("")

    add("一、doctor")
    add("-" * 40)
    try:
        for r in doc_mod.collect(a.config):
            add(" [%s] %-24s %s" % (r.level, r.name, r.detail))
    except Exception as e:
        add(" doctor 自己出错：%s: %s" % (type(e).__name__, e))
    add("")

    add("二、配置与账号")
    add("-" * 40)
    cfg = None
    try:
        cfg = cfg_mod.load(a.config, missing_ok=True)
    except cfg_mod.ConfigError as e:
        add(" 配置读取失败：%s" % e)
    if cfg is None:
        add(" 还没有初始化配置")
    else:
        add(" 配置文件：%s" % cfg.path)
        add(" 数据目录：%s" % cfg.data_dir)
        add(" 并发：%d ｜ 闲置回收：%d 分钟" % (cfg.concurrency, cfg.idle_recycle_min))
        for acc in cfg.accounts:
            add(" 账号 %-8s %-12s %-6s 端口 %-6d profile=%s"
                % (acc.id, acc.name, acc.browser, acc.port, acc.profile))
        add("")

        add("三、账号运行态")
        add("-" * 40)
        try:
            from .core.store import Store

            store = Store(cfg)
            for acc in cfg.accounts:
                st = store.account(acc.id)
                add(" %-8s 登录=%-8s 今天=%d 张 ｜ 撞上限 %d 次 ｜ 冷却到 %s"
                    % (acc.id, st.login, st.stat_today, len(st.quota_hits),
                       time.strftime("%m-%d %H:%M", time.localtime(st.cooldown_until))
                       if st.cooldown_until else "—"))
            add("")

            add("四、队列")
            add("-" * 40)
            s = store.summary()
            add(" 共 %d 单 ｜ 待跑 %d ｜ 在跑 %d ｜ 今天出图 %d 张"
                % (s["jobs"], s["queued"], s["running"], s["today_images"]))
            add(" 暂停：%s" % ("是" if store.paused() else "否"))
            for row in store.query(limit=8)["rows"]:
                add("  R%-4d %-8s %s/%s  %s  %s"
                    % (row["id"], row["status"], row["rendered"], row["expected"],
                       row["account_id"], (row["brief"] or "")[:30]))
        except Exception as e:
            add(" 读队列失败：%s: %s" % (type(e).__name__, e))
    add("")

    add("五、最近的日志（各取末尾 25 行）")
    add("-" * 40)
    try:
        base = Path(cfg.data_dir) / "logs" if cfg else paths.logs_dir()
        logs = sorted(base.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        if not logs:
            add(" 没有日志文件")
        for f in logs:
            add("")
            add(">>> %s" % f)
            try:
                tail = f.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                lines.extend("    " + x for x in tail)
            except OSError as e:
                add("    （读不了：%s）" % e)
    except Exception as e:
        add(" 找日志失败：%s" % e)

    add("")
    add("说明：这份文件不含 cookie / token / 口令 / 邮箱；账号只出现 id 与显示名。")

    # 脱敏：把主目录和用户名换成占位符。日志里常出现完整路径，
    # 朋友发给我之前不必先把用户名也发过来。
    home = str(Path.home())
    uname = Path.home().name
    redacted = []
    for line in lines:
        if home and home in line:
            line = line.replace(home, "<主目录>")
        if uname and len(uname) >= 2:
            line = line.replace(uname, "<用户>")
        redacted.append(line)
    lines = redacted

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig：带 BOM。朋友多半用记事本打开或直接发给我，
        # 不带 BOM 的 UTF-8 在中文 Windows 上会被当 GBK 读出乱码。
        out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    except OSError as e:
        _err("写不了 %s：%s" % (out, e))
        return EXIT_INTERNAL

    if a.json:
        _emit({"schema_version": 1, "ok": True, "path": str(out.resolve()),
               "lines": len(lines)})
    else:
        print("诊断已写到：%s" % out)
        print("（%d 行，可以直接发人；里面没有你的账号密码）" % len(lines))
    return EXIT_OK


def cmd_login(a) -> int:
    from . import login_flow

    try:
        cfg = cfg_mod.load(a.config)
        acc = _pick_account(cfg, a.account_id)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE

    if not a.json:
        print("正在打开 %s 的专用浏览器，窗口会自动弹到最前…" % acc.name)
    r = login_flow.login(cfg, acc, timeout=a.timeout, keep_open=a.keep_open,
                         discard_on_fail=a.discard_on_fail)
    if a.json:
        _emit({"schema_version": 1, **r})
        return EXIT_OK if r["ok"] else EXIT_ACTION

    st = r.get("state") or {}
    if r["ok"]:
        print("已登录：%s" % (st.get("url", "")[:70]))
        if a.keep_open:
            print("窗口保持打开（--keep-open）。关掉它或跑 %s browser down。" % _self())
        else:
            print("窗口已藏回后台。")
        print("下一步：%s probe %s 复核一次" % (_self(), acc.id))
        return EXIT_OK

    _err("没等到登录：%s" % r.get("note"))
    _err("窗口还开着，你可以继续在里面登录；再跑一次这个命令会重新检查。")
    return EXIT_ACTION


# ------------------------------------------------------------------ 解析

def cmd_gateway(a) -> int:
    """共享网关：把本机 HTTP 号池按 OpenAI 兼容接口借给局域网 / Tailscale 上的人。"""
    from . import gateway
    cfg = cfg_mod.load(getattr(a, "config", None))
    act = getattr(a, "gaction", None)
    if act == "serve":
        gateway.serve(cfg, host=(getattr(a, "host", "0.0.0.0") or "0.0.0.0"),
                      port=(getattr(a, "port", 8788) or 8788), block=True)
        return EXIT_OK
    if act == "invite":
        name = getattr(a, "name", None) or ident or "朋友"
        keys = gateway.list_keys(cfg)
        rec = next((k for k in keys if k.get("name") == name), None)
        if not rec:
            rec = gateway.add_key(cfg, name,
                                  daily=(a.daily if getattr(a, "daily", None) is not None else 100),
                                  concurrency=(a.concurrency if getattr(a, "concurrency", None) is not None else 3))
        host = getattr(a, "host", None) or gateway.best_host()
        port = getattr(a, "port", 8788) or 8788
        txt = gateway.invite_paragraph(rec, host, port=port)
        p = gateway.invite_file(cfg, txt, name)
        print(txt)
        print("\n（同一段话已存到：%s ，可直接把这个文件发给对方）" % p)
        return EXIT_OK
    if act == "key":
        ksub = getattr(a, "kaction", "ls") or "ls"
        ident = getattr(a, "kname", None) or getattr(a, "nameflag", None)
        if ksub == "add":
            daily = a.daily if getattr(a, "daily", None) is not None else 100
            conc = a.concurrency if getattr(a, "concurrency", None) is not None else 3
            rec = gateway.add_key(cfg, ident or "friend", daily=daily, concurrency=conc)
            print("已生成调用 key：%s" % rec["key"])
            print("  名称=%s  每日上限=%d 张  并发=%d" % (rec["name"], rec["daily_limit"], rec["concurrency"]))
            return EXIT_OK
        if ksub == "ls":
            rows = gateway.list_keys(cfg)
            if not rows:
                print("还没有 key。用：bampiao gateway key add --name 朋友")
                return EXIT_OK
            for k in rows:
                print("%-12s %-16s daily=%-5s 并发=%-3s %s" % (
                    k.get("id"), k.get("name"), k.get("daily_limit"), k.get("concurrency"),
                    "启用" if k.get("enabled", True) else "停用"))
            return EXIT_OK
        if ksub == "rm":
            if not ident:
                _err("要删哪个 key？bampiao gateway key rm <名字或id>", code="BAD_USAGE")
                return EXIT_USAGE
            print("已删除 %d 个 key" % gateway.rm_key(cfg, ident))
            return EXIT_OK
        if ksub == "limit":
            if not ident:
                _err("要改哪个 key？bampiao gateway key limit <名字> --daily N --concurrency M", code="BAD_USAGE")
                return EXIT_USAGE
            print("已更新 %d 个 key" % gateway.set_limit(
                cfg, ident, daily=getattr(a, "daily", None), concurrency=getattr(a, "concurrency", None)))
            return EXIT_OK
    print("用法：")
    print("  bampiao gateway serve --host 0.0.0.0 --port 8788")
    print("  bampiao gateway key add --name 朋友 [--daily 100 --concurrency 3]")
    print("  bampiao gateway key ls | rm <名字> | limit <名字> --daily N --concurrency M")
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="bampiao", description="半瓢 · 本地出图台")
    ap.add_argument("--version", action="version", version="半瓢 %s" % __version__)
    ap.add_argument("--config", help="配置文件路径（默认 ~/.bampiao/config.toml）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="初始化配置与目录（顺带建好第一个账号）")
    p.add_argument("--force", action="store_true", help="覆盖已有配置（危险）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("skill", help="打印要粘给你 Agent 的那段说明")
    p.add_argument("which", nargs="?", default="paste",
                   choices=list(_SKILL_FILES),
                   help="paste=直接粘到对话框（默认）/ claude / agents / all")
    p.add_argument("--out", help="写到文件，而不是打印到屏幕")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("doctor", help="检查环境能不能跑")
    p.add_argument("--allow-empty", action="store_true", dest="allow_empty",
                   help="只查装得对不对，不要求已经加好账号")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("accounts", help="账号：列表 / 添加 / 删除 / 改名")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "rm", "rename"])
    p.add_argument("account", nargs="?", help="rm / rename 时的账号 id 或显示名")
    p.add_argument("new_name", nargs="?", help="rename 的新名字（也可以 --name）")
    p.add_argument("--name")
    p.add_argument("--browser", choices=["chrome", "edge", "auto"],
                   help="不给就自动挑本机装了的（chrome 优先）")
    p.add_argument("--port", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("browser", help="浏览器实例：up / down / status")
    p.add_argument("action", nargs="?", default="status", choices=["up", "down", "status"])
    p.add_argument("account", nargs="?", help="账号 id 或显示名（只有一个账号时可省略）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("probe", help="看一眼这个账号的浏览器停在哪一页")
    p.add_argument("account", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("upgrade-guard", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_upgrade_guard)
    p = sub.add_parser("windows", help="列出账号浏览器的窗口（排查任务栏幽灵窗口）")
    p.add_argument("--account")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("chats", help="管理 ChatGPT 侧栏里的会话（列出来 / 删掉）")
    p.add_argument("action", nargs="?", default="list", choices=["list", "rm"])
    p.add_argument("--account", help="账号 id 或显示名")
    p.add_argument("-n", "--limit", type=int, default=30, help="列出多少条（默认 30）")
    p.add_argument("--id", action="append", default=[], dest="ids",
                   help="要删的会话 id，可重复")
    p.add_argument("--keep", type=int, default=0,
                   help="只保留最近 N 条，其余删掉（要配 --yes）")
    p.add_argument("--older", type=float, default=0,
                   help="只删 N 天以前的（可单独用，也可配合 --keep）")
    p.add_argument("--any", action="store_true",
                   help="连用户自己的会话也算进来（默认只动半瓢标题的，风险自负）")
    p.add_argument("--yes", action="store_true", help="不再确认，直接删")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("ask", help="问一句，走网页会员额度（和出图同一套账号池）")
    p.add_argument("prompt", nargs="?", help="要问什么；给 - 表示从 stdin 读")
    p.add_argument("--file", help="提示词从 UTF-8 文件读（长提示词用）")
    p.add_argument("--out", "-o", help="回复写到这个文件；不给就打到 stdout")
    p.add_argument("--account", help="用哪个号（默认挑今天最闲的）")
    p.add_argument("--chat", default="",
                   help="会话名：同名就接着聊（会话绑账号，换号会丢上下文）")
    p.add_argument("--timeout", type=int, default=300, help="最多等多久（秒）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("gen", help="出一条图")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--out", "-o", help="输出文件路径；不给就按时间自动命名")
    p.add_argument("--prompt-file", help="提示词从 UTF-8 文件读（长提示词用）")
    p.add_argument("--account", help="账号 id 或显示名（只有一个账号时可省略）")
    p.add_argument("--aspect", default="1:1", help="1:1 / 16:9 / 2:3 …")
    p.add_argument("--ref", action="append", help="参考图，可重复")
    p.add_argument("--alpha", action="store_true", help="要透明底（会追加洋红底要求）")
    p.add_argument("--gif", action="store_true",
                   help="要动图（Images 2.6 的循环 GIF）；会给提示词补一句并核对帧数")
    p.add_argument("--svg", action="store_true",
                   help="要 .svg 矢量文件（网页版以文件附件交付，不是图片 blob）")
    p.add_argument("--svg-anim", action="store_true",
                   help="要会动的 SVG（内含 SMIL/CSS 动效），自动按 --svg 处理")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("login", help="打开专用浏览器，等你登录完自动藏回后台")
    # 不给账号就用默认的那个（init 打印的"下一步：bampiao login"就是这种用法）。
    # 以前这里是必填，于是 init 教用户跑的第一条命令直接报参数错误（实测）。
    p.add_argument("account_id", nargs="?", help="账号 id 或显示名（只有一个账号时可省略）")
    p.add_argument("--timeout", type=float, default=300.0, help="最多等多久（秒，默认 300）")
    p.add_argument("--keep-open", action="store_true", help="登录后不自动藏窗口")
    # 面板「＋ 添加账号」用：新号没登上就连号一起撤掉，不留空壳
    p.add_argument("--discard-on-fail", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("submit", help="投一单（进队列，由 run 或托盘去跑）")
    p.add_argument("prompt", nargs="?", help="一句话提示词；要投多条请用 --shots")
    p.add_argument("--shots", help="张数（--shots 4）或 JSON 文件（--shots shots.json）")
    p.add_argument("--serial", action="store_true",
                   help="多张保持一单、串行跑在同一个号上（默认拆开分给多个号并行）")
    p.add_argument("--prompt-file", help="提示词从 UTF-8 文件读")
    p.add_argument("--account", help="账号 id 或显示名（只有一个账号时可省略）")
    p.add_argument("--brief", default="", help="这单是干什么的（给人看的）")
    p.add_argument("--tag", default="", help="可选分类，用来筛选和统计")
    p.add_argument("--aspect", default="1:1", help="单条提示词时的画幅")
    p.add_argument("--alpha", action="store_true", help="要透明底")
    p.add_argument("--gif", action="store_true",
                   help="要动图（GIF）；会给每条提示词补一句并核对帧数")
    p.add_argument("--svg", action="store_true",
                   help="要 .svg 矢量文件（网页版以文件附件交付，不是图片 blob）")
    p.add_argument("--svg-anim", action="store_true",
                   help="要会动的 SVG（内含 SMIL/CSS 动效），自动按 --svg 处理")
    p.add_argument("--request-id", default="", help="幂等键：同一个值不会重复投单")
    p.add_argument("--submitter", default="cli", help="谁投的（cli / ui / agent:xxx）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run", help="跑队列（--once 跑完就退，默认常驻）")
    p.add_argument("--once", action="store_true", help="把当前能跑的处理完就退出")
    p.add_argument("--poll", type=float, default=3.0, help="空闲时多久看一次队列")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("jobs", help="看队列 / 历史")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--status", default="all")
    p.add_argument("--account", default="all")
    p.add_argument("--tag", default="all")
    p.add_argument("--q", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("show", help="看某一单的详情")
    p.add_argument("job_id", type=_job_id, help="单号，写 7 或 R7 都行")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("usage", help="账号用量与冷却状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("backup", help="账号登录态的备份 / 恢复（cookie 加密）")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file", nargs="?", help="导入时的备份文件")
    p.add_argument("--account", help="账号 id 或显示名")
    p.add_argument("--out", help="导出到哪个文件")
    p.add_argument("--password", default="",
                   help="口令（更推荐环境变量 BAMPIAO_BACKUP_PASSWORD，避免留在历史里）")
    p.add_argument("--force", action="store_true", help="目标已登录别的账号时仍然覆盖")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("autostart", help="开机自启开关（HKCU Run，不碰计划任务）")
    p.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    p.add_argument("--exe", help="手动指定 bampiao-desktop 的路径")
    p.add_argument("--extra", default="", help="附加启动参数")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("diag", help="把排障信息写成一个文件（发给别人看）")
    p.add_argument("--out", help="写到哪（默认桌面 bampiao-diag.txt）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_diag)

    p = sub.add_parser("feedback", help="打包反馈（诊断+日志+失败单）发给作者")
    p.add_argument("--out", help="写到哪（默认桌面 半瓢反馈-时间.zip）")
    p.add_argument("--no-upload", action="store_true", help="只落盘，不上传")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("ui", help="打开半瓢窗口（等价双击图标）")
    p.add_argument("--serve", action="store_true",
                   help="只起 HTTP 服务并打印地址，不打开窗口（自动化 / 无图形环境用）")
    p.add_argument("--port", type=int, default=0, help="配合 --serve 指定端口")
    p.add_argument("--no-browser", action="store_true", help="（已废弃，保留兼容）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("gateway", help="共享网关：把本机 HTTP 号池按 OpenAI 接口借给局域网/Tailscale 上的人")
    gsub = p.add_subparsers(dest="gaction")
    ps = gsub.add_parser("serve", help="启动网关服务（默认 0.0.0.0:8788）")
    ps.add_argument("--host", default="0.0.0.0")
    ps.add_argument("--port", type=int, default=8788)
    pk = gsub.add_parser("key", help="管理调用钥匙（每个 key 有每日张数/并发上限）")
    pk.add_argument("kaction", nargs="?", default="ls", choices=["add", "ls", "rm", "limit"])
    pk.add_argument("kname", nargs="?", help="名字或 id")
    pk.add_argument("--name", dest="nameflag", help="名字（同 kname）")
    pk.add_argument("--daily", type=int)
    pk.add_argument("--concurrency", type=int)
    pi = gsub.add_parser("invite", help="生成给某人的完整接入说明（地址/密钥/一段话/下载链接）")
    pi.add_argument("--name")
    pi.add_argument("--host")
    pi.add_argument("--port", type=int, default=8788)
    pi.add_argument("--daily", type=int)
    pi.add_argument("--concurrency", type=int)
    p.set_defaults(func=cmd_gateway)

    p = sub.add_parser("quit", help="让正在运行的半瓢退出（托盘里找不到它时用）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quit)

    return ap


def _console_cp() -> int:
    """控制台输出码页（中文机器 936，chcp 65001 时 65001）。没有控制台返回 0。"""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:
        return 0


def _fix_std_encoding() -> None:
    """让输出按**控制台实际码页**写，别让用户看到乱码。

    为什么不能简单"跟随系统"：Python 3.14 起 UTF-8 模式可能被
    PYTHONUTF8=1（或将来的默认值）打开，那时 stdout 是 UTF-8 而
    PowerShell 仍按 cp936 解码 —— 中文就成乱码（实测过）。
    依赖"Python 的默认行为恰好和终端一致"是靠运气，不是设计。

    所以：不是真终端时，显式按 GetConsoleOutputCP() 写。PowerShell / cmd
    解码原生命令输出用的就是这个码页，两边天然对齐；`chcp 65001` 的用户
    也会自动变成 UTF-8。

    - 真控制台（isatty）不动：Python 走 WriteConsoleW，本来就正确
    - 显式设了 PYTHONIOENCODING 的调用方（agent、脚本）不动：尊重它
    - --json 输出是纯 ASCII，任何码页都不会坏
    """
    if os.name != "nt":
        return
    # 显式指定的编码优先。但冻结版（安装包）里 Python 根本不认这个变量，
    # 认了反而会让我们放弃纠正 —— 那里必须继续按控制台码页来。
    if os.environ.get("PYTHONIOENCODING") and not getattr(sys, "frozen", False):
        return
    cp = _console_cp()
    if not cp:
        return                      # 没有控制台（服务/计划任务），保持原样
    enc = "utf-8" if cp == 65001 else "cp%d" % cp
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty():
                continue
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass


def _job_id(s: str) -> int:
    """单号：`7` 和 `R7` 都认。

    界面卡片上写的是「R12」，Agent 看多了自然会照抄；只收纯数字时它要挨一次
    参数错误才知道改——这种小事不该让 Agent 去猜。
    """
    t = str(s).strip().lstrip("Rr")
    try:
        return int(t)
    except ValueError:
        raise argparse.ArgumentTypeError("单号写成 7 或 R7，收到的是 %r" % s)


class _Parser(argparse.ArgumentParser):
    """argparse 自己报错时也要守住 --json 契约。

    默认它直接往 stderr 打 usage 然后 sys.exit(2)，stdout 一个字节都没有：
    带 --json 的 agent 只会看到"输出为空"，看不出是自己参数写错了。
    子命令解析器会继承这个类（add_subparsers 默认用父解析器的类型）。
    """

    def error(self, message):
        if "--json" in sys.argv:
            _emit({"schema_version": 1, "ok": False, "error_code": "BAD_USAGE",
                   "rc": EXIT_USAGE, "why": message,
                   "hint": self.format_usage().strip()})
        super().error(message)


def main(argv: list[str] | None = None) -> int:
    # 2026-11-05：Windows 上 sys.stdout 默认是 cp936，Agent 按 UTF-8 读就全是乱码。
    # 实测代价很实在：一个第三方 Agent 拿 `bampiao ask` 的结果时中文全坏，只好重跑一次，
    # 白花了 300 秒和一份订阅额度。这里统一改成 UTF-8（不影响控制台显示，
    # 现代 Windows Terminal 本来就吃 UTF-8）。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    # 冻结版（PyInstaller 打的 exe）里没有 `python -m 模块` 这条路：
    # sys.executable 就是 bampiao.exe。所以驱动子进程用隐藏参数 --driver
    # 回到这里，导入即执行（cdp_run 在模块级就 asyncio.run 了）。
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "--chats":
        from .driver import chats
        return chats.main(sys.argv[2:])

    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "--driver":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .driver import cdp_run  # noqa: F401  导入即跑，不会返回
        return 0                      # 正常情况到不了这里

    # 编码调整本身绝不许把 CLI 弄挂（第一版就因为少 import os 直接 rc=1 + traceback）。
    try:
        _fix_std_encoding()
    except Exception:
        pass

    # 数据搬家：安装版从 ~/.bampiao 挪到程序目录下的 data。
    # 桌面端启动时也会做，但有人只用命令行（尤其 agent 直接调 `bampiao gen`），
    # 那就会拿着新路径去找配置、发现是空的。这里补一刀，幂等。
    #
    # ★ 只读查询不搬。2026-11-04 实测：Mac 上跑一次 `半瓢 --version` 就把
    # 3.4G 数据搬了家，装着的旧版 app 立刻看不到账号——用户只是问了句版本号。
    # 下面 maintenance 那段早就有守卫，这里当初漏了。
    _sub = sys.argv[1] if (argv is None and len(sys.argv) > 1) else ""
    if _sub not in ("--version", "-V", "--help", "-h", ""):
        try:
            _moved = paths.migrate_legacy()
        except Exception:
            _moved = ""
        if _moved:
            # 走 stderr：stdout 可能正被 agent 当 JSON 解析，不能掺一句话进去
            print(_moved, file=sys.stderr)

    # 纯命令行用法也要扫垃圾：桌面端启动时会做，但有人只用 `bampiao gen`。
    # 只在真干活的命令前做 —— --version / --help 不该去碰磁盘。
    if argv is None and len(sys.argv) > 1 and sys.argv[1] in ("gen", "submit", "run"):
        try:
            from . import maintenance
            maintenance.startup_pass(paths.data_dir())
        except Exception:
            pass

    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        ap.print_help()
        return EXIT_USAGE
    # 全局兜底：任何没预料到的异常都不许吐 traceback、更不许返回 1（不在合约里）。
    # GUI/agent 拿到 rc=1 只会当成"工具挂了"，连重试策略都没法定。
    try:
        rc = a.func(a)
    except cfg_mod.NeedsUser as e:
        # 环境没准备好（没账号 / 没登录 / 没浏览器）：这是"要人动手"，
        # 不是"命令写错了"。给 3，agent 才会去叫用户，而不是改参数。
        _err(str(e), code="NEED_USER", hint="按上面的提示处理后重跑这条命令。")
        return _finish(a, EXIT_ACTION)
    except cfg_mod.ConfigError as e:
        _err(str(e))
        return _finish(a, EXIT_USAGE)
    except locks.Busy as e:
        _err("另一个半瓢正在操作同一份数据，稍等再试：%s" % e, code="BUSY")
        return _finish(a, EXIT_ACTION)
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as e:
        if getattr(a, "json", False):
            _emit({"schema_version": 1, "ok": False,
                   "error_code": "INTERNAL",
                   "why": "%s: %s" % (type(e).__name__, e)})
        else:
            _err("出错了：%s: %s（日志在 %s）" % (type(e).__name__, e, _log_hint()))
        return EXIT_INTERNAL
    return _finish(a, rc)


def _log_hint() -> str:
    try:
        cfg = cfg_mod.load(None)
        return str(Path(cfg.data_dir) / "logs")
    except Exception:
        return str(paths.logs_dir())


if __name__ == "__main__":
    sys.exit(main())
