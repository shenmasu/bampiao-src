"""本地面板服务：只监听 127.0.0.1，不对外。

设计上的三条：
1. **只读为主**：绝大多数接口是查；写只有四个动作（暂停 / 恢复 / 取消 / 重投）。
2. **不重复实现逻辑**：所有读写都走 `core.store`，面板不自己扫目录、不自己算统计。
3. **不阻塞调度**：面板和 `bampiao run` 是两个进程，共享同一份磁盘状态，
   靠 store 里的锁保证安全。
"""

from __future__ import annotations

import json
import mimetypes
import os
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


SHOW_LEASE = 10 * 60      # 「打开浏览器」之后，静默巡逻在这段时间内不许藏它


def _today() -> str:
    """本地日期 YYYY-MM-DD。统计口径统一用它，别各写各的。"""
    return time.strftime("%Y-%m-%d")


from .. import __version__, config as cfg_mod, login_flow
from ..core.store import Store


class _LocalHTTPServer(ThreadingHTTPServer):
    """只监听回环地址的服务端，跳过反向 DNS。

    标准库 HTTPServer.server_bind() 会调 socket.getfqdn() 做一次反向解析。
    macOS 上这一步走 mDNS，网络环境稍复杂（VPN / Clash / 无 DNS）就会**卡住几十秒
    甚至一直不返回**——实测主线程停在 kevent 里，窗口和面板都出不来。
    我们只绑 127.0.0.1，本来也不需要 FQDN。
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 18799


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# 会话列表缓存：账号 id → (时间戳, 结果)。删完会清掉。
_CHATS_CACHE: dict = {}
CHATS_TTL = 60


def run_alive(cfg, account_id: str):
    """这个号此刻真的有进程在出图吗（看账号级的出图锁）。

    返回 True=有进程、False=没有、None=判断不了。
    job.json 写着 running 只代表"被领走了"，不代表真在画——2026-11-03 朋友看到
    "图都出来了，还显示在跑"，就是这两件事被混成了一件。
    """
    if not account_id:
        return None
    try:
        from .. import locks
        from ..driver import browser as br

        return not locks.try_acquire(br.gen_lockfile(cfg, account_id))
    except Exception:
        return None


class Panel:
    def __init__(self, cfg: cfg_mod.Config):
        self.cfg = cfg
        self.store = Store(cfg)
        self._cfg_mtime: float | None = None
        # 更新相关的运行态：查到没、下没下、等没等、预下载到哪了
        self._UPD: dict = {"check": None, "dl": None, "busy": False, "waiting": False}

    def _cfg(self) -> cfg_mod.Config:
        """返回最新的配置。

        面板启动时读一次就缓存的话，**CLI 加的账号、改的绑定永远不会出现在面板上**
        （2026-10-30 实测：CLI 加回 acc1，accounts list 有、面板只有一张卡）。
        配置文件很小，按 mtime 判断变没变即可，不用每次全量解析。
        """
        try:
            mtime = self.cfg.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._cfg_mtime:
            try:
                self.cfg = cfg_mod.load(self.cfg.path)
                self._cfg_mtime = mtime
            except Exception:
                # 配置正被另一个进程写（写一半）就用旧的，别把整个面板搞崩
                pass
        return self.cfg

    # ------------------------------------------------------------ 读
    def overview(self) -> dict:
        accs = []
        for acc in self._cfg().accounts:
            st = self.store.account(acc.id)
            hit = st.quota_hits[-1] if st.quota_hits else None
            phase = login_flow.read_phase(self.cfg, acc.id)
            accs.append({
                "id": acc.id, "name": acc.name, "browser": acc.browser,
                # today 必须按"今天"算：stat_today 只在下次出图时才跨日归零，
                # 这天没出图就会一直挂着上一个出图日的数字（2026-11-02 用户反馈）
                "login": st.login, "today": st.images_today(_today()),
                "cooling": st.cooling(),
                # 卡片上"上次检查"用；没有就显示"还没查过"
                "checked_at": st.login_checked_at,
                "cooldown_left_min": (max(0, round((st.cooldown_until - __import__("time").time()) / 60))
                                      if st.cooling() else 0),
                "quota_hits": len(st.quota_hits),
                "quota_today": st.quota_hits_on(_today()),
                # 进程在但窗口没了 = 假死，面板上要能看出来（否则只会觉得"点了没反应"）
                "window_ok": self._window_ok(acc.id),
                # 被 Cloudflare 挂起的号：面板要说清"为什么这单不动"
                "paused_min": self._paused_min(acc.id),
                "last_hit_after_n": hit.get("after_n") if hit else None,
                # 邮箱：新登录写进 AccountState；老账号从登录 phase 兜底，
                # 后台补扫线程会陆续把老账号的邮箱补上（见 _scan_emails）。
                "email": st.login_email or str(phase.get("email") or ""),
                "browser_running": self._browser_running(acc.id),
                "login_phase": phase,
            })
        # 在跑的单要单独给：前端原来从"当前页 + 当前筛选"里取，
        # 翻到第二页或筛成"完成"时，顶栏写着在跑 1 而卡片区是空的。
        try:
            runs = self.store.query(limit=5, status="running").get("rows") or []
            # "显示在跑"和"真的在跑"是两件事：job.json 写着 running 只说明它被领走了。
            # 用账号的出图锁判断此刻有没有进程在画——锁拿着=真在跑；锁空着=残留，
            # 由后台 reaper 在 90 秒内标成"不确定"（2026-11-03 朋友看到"完成了还在跑"）。
            for _r in runs:
                _r["alive"] = run_alive(self.cfg, str(_r.get("account_id") or ""))
        except Exception:
            runs = []
        from .. import browsers as _brs
        avail = [b for b in ("chrome", "edge") if _brs.find(b)]
        return {"ok": True, "version": __version__, "runs": runs, "browsers": avail,
                "crashes": self._crash_count(),
                "update": self.update_state(),
                "data_dir": str(self.cfg.data_dir),
                "summary": self.store.summary(), "accounts": accs,
                "idle_recycle_min": self._cfg().idle_recycle_min,
                "attention": self._attention_cached(),
                # 第一次收起窗口时桌面端留的记号：下次打开面板时说一句"它还在后台跑"。
                # 不用系统通知——2026-11-01 用户明确说过通知很打扰。
                "close_hint": (Path(self.cfg.data_dir) / "state" / "close_hint.txt").exists(),
                "paused": self.store.paused(), "pause": self.store.pause_info(),
                # 引导做完了没有，存在磁盘上而不是浏览器 localStorage：
                # 窗口是 WebView2，默认无痕，localStorage 关一次就清——于是每次都从头引导
                "onboarded": self._ui_flag("onboarded"),
                # 设置页要显示"现在选的是哪种策略"，得一起给出当前值
                "account_strategy": getattr(self._cfg(), "account_strategy", "balance"),
                "account_fixed_id": getattr(self._cfg(), "account_fixed_id", ""),
                "facets": self.store.facets()}

    def jobs(self, q: dict) -> dict:
        def one(k, d):
            try:
                return q.get(k, [d])[0]
            except (IndexError, AttributeError):
                return d

        limit = max(1, min(200, int(one("limit", 24))))
        offset = max(0, int(one("offset", 0)))
        d = self.store.query(limit=limit, offset=offset,
                             status=one("status", "all"),
                             account=one("account", "all"),
                             tag=one("tag", "all"),
                             q=one("q", ""), kind=one("kind", "all"))
        return {"ok": True, **d}

    def job_detail(self, job_id: int) -> dict:
        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d 这一单" % job_id}
        d = job.as_dict()
        d["dir"] = str(self.store.dir_of(job_id))
        d["display_brief"] = job.display_brief
        # 面板的已处理卡认这个布尔值；as_dict 里只有 handled_at
        d["handled"] = bool(job.handled_at)
        from .. import why as _why

        # 文本单：回复才是结果。全文从 S01.txt 读（老的成功单没有文件就退回 note）。
        if (job.kind or "image") == "ask":
            reply = ""
            for it in job.items:
                try:
                    reply = (self.store.dir_of(job_id) / ("%s.txt" % it.id)) \
                        .read_text(encoding="utf-8")
                    break
                except OSError:
                    continue
            if not reply and job.status == "done":
                # 0.2.50 之前成功的 ask 单只把回复存在 note 里；失败单的 note
                # 可能写着错误码，绝不能当回复返回（2026-11-06 评审）。
                reply = job.note or ""
            d["reply"] = reply[:20000]
            d["reply_truncated"] = len(reply) > 20000
            d["reply_partial"] = bool(getattr(job, "partial", False))

        for it in d["items"]:
            it["urls"] = ["/f/%d/%s" % (job_id, f) for f in it.get("files") or []]
            it["thumb"] = ("/f/%d/%s?w=160" % (job_id, it["files"][0])) if it.get("files") else ""
            # 交付物类型：GIF 要显示"动图 12 帧"，不能只当一个普通文件
            if it.get("files"):
                try:
                    it["media"] = self.media_info(self.store.dir_of(job_id) / it["files"][0])
                except Exception:
                    it["media"] = {}
            else:
                it["media"] = {}
            # 只有"已经有结论的失败"才给问题卡。
            # 以前写的是 status != "ok" —— 正在跑的单（queued/running）也会被算出
            # 一句"这张没画出来"，用户看着任务在跑却同时被告知失败（2026-11-03 反馈）。
            if it.get("status") in ("failed", "skipped", "cancelled"):
                info = _why.classify(it.get("note") or "", it.get("error_code") or "",
                                     bool(job.uncertain), kind=(job.kind or "image"))
                it["failure"] = {
                    "cls": info.cls, "title": info.title, "short": info.short,
                    "detail": info.detail,
                    "primary": list(info.primary),
                    "secondary": list(info.secondary) if info.secondary else None,
                    "extra": [list(x) for x in info.extra],
                    "steps": list(info.steps),
                }
        return {"ok": True, "job": d}

    def media_info(self, path: Path) -> dict:
        """这个文件的真身（GIF/PNG、几帧）。带缓存：轮询时不要反复解文件头。"""
        try:
            st = path.stat()
            key = (str(path), st.st_mtime, st.st_size)
            cache = getattr(self, "_media_cache", None)
            if cache is None:
                cache = self._media_cache = {}
            if key in cache:
                return cache[key]
            from .. import media as _media

            info = _media.probe(path)
            if len(cache) > 500:
                cache.clear()
            cache[key] = info
            return info
        except Exception:
            return {}

    def thumbnail(self, job_id: int, name: str, width: int = 80) -> Path | None:
        """列表用的 80px 缩略图。落盘缓存，按源文件 mtime 失效。

        为什么必须有：`/f/` 直接返原图，24 行 × 1024² PNG，每 2 秒重建一次，
        鼠标压在列表上就会卡（2026-11-03 评审的性能红线）。
        """
        src = self.file_path(job_id, name)
        if not src:
            return None
        try:
            from PIL import Image

            cache_dir = self.cfg.data_dir / "cache" / "thumbs"
            cache_dir.mkdir(parents=True, exist_ok=True)
            key = "%d-%s-%d-%d.jpg" % (job_id, src.stem, int(src.stat().st_mtime), width)
            out = cache_dir / key
            if out.exists() and out.stat().st_size > 0:
                return out
            im = Image.open(src)
            im.thumbnail((width, width), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, "JPEG", quality=82, optimize=True)
            return out
        except Exception:
            return None

    def file_path(self, job_id: int, name: str) -> Path | None:
        base = self.store.dir_of(job_id).resolve()
        p = (base / name).resolve()
        # 比较时带上分隔符：否则 R1 的目录能冒充 R11（"…/jobs/0001" 是 "…/jobs/00011" 的前缀）
        if not str(p).startswith(str(base) + os.sep) or p == base or not p.is_file():
            return None
        return p

    def _open_data_dir(self, sub: str) -> dict:
        """打开数据目录（或它的 output 子目录）。

        `sub` 只认白名单：前端能传任意字符串，不能让它变成"打开 C:\\Windows"。
        """
        import subprocess
        import sys as _sys

        allowed = {"": self.cfg.data_dir, "output": Path(self.cfg.data_dir) / "output",
                   "logs": Path(self.cfg.data_dir) / "logs"}
        target = allowed.get(sub)
        if target is None:
            return {"ok": False, "why": "不能打开这个位置"}
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(target))
            elif _sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False, timeout=10)
            else:
                subprocess.run(["xdg-open", str(target)], check=False, timeout=10)
            return {"ok": True, "path": str(target)}
        except Exception as e:
            return {"ok": False, "why": "打不开：%s" % e}

    def _copy_image(self, body: dict) -> dict:
        """把一张成图放进系统剪贴板（右键「复制图片」或 Ctrl+C 走这里）。

        为什么不只用 navigator.clipboard：面板跑在 WebView2 里，pywebview 没有接
        PermissionRequested，剪贴板授权默认被拒，前端那条路在真窗口里必失败。
        """
        from .. import clipboard as clip

        # 前端传进来的是 img.src，可能是完整地址（http://127.0.0.1:端口/f/5/S01.png）。
        # 只认路径部分，别因为多了个 origin 就报"这不是半瓢出的图"（实测踩过）。
        raw = str(body.get("url") or "")
        url = urlparse(raw).path if "://" in raw else raw.split("?")[0]
        url = unquote(url)                    # 文件名带中文时前端会转义成 %XX
        parts = url.split("/")
        if len(parts) < 4 or not url.startswith("/f/"):
            return {"ok": False, "why": "这不是半瓢出的图"}
        try:
            jid = int(parts[2])
        except ValueError:
            return {"ok": False, "why": "图号不对"}
        f = self.file_path(jid, parts[3])
        if not f:
            return {"ok": False, "why": "找不到这张图"}
        ok, why = clip.copy_image(f)
        return {"ok": True, "why": "已复制到剪贴板"} if ok else {"ok": False, "why": "复制不了：%s" % why}

    def _save_image(self, body: dict) -> dict:
        """「另存为」：复制到半瓢的 output 文件夹，并在资源管理器 / 访达里选中它。

        不用前端 <a download>：pywebview 默认不放行窗口里的下载，点了什么都不会发生，
        前端却提示"已存到下载"——又是一个假成功。
        """
        import shutil

        raw = str(body.get("url") or "")
        url = urlparse(raw).path if "://" in raw else raw.split("?")[0]
        url = unquote(url)                    # 文件名带中文时前端会转义成 %XX
        parts = url.split("/")
        if len(parts) < 4 or not url.startswith("/f/"):
            return {"ok": False, "why": "这不是半瓢出的图"}
        try:
            jid = int(parts[2])
        except ValueError:
            return {"ok": False, "why": "图号不对"}
        src = self.file_path(jid, parts[3])
        if not src:
            return {"ok": False, "why": "找不到这张图"}
        dl = Path(self.cfg.data_dir) / "output"
        dl.mkdir(parents=True, exist_ok=True)
        dst = dl / ("半瓢-R%d-%s" % (jid, src.name))
        n = 2
        while dst.exists():
            dst = dl / ("半瓢-R%d-%s-%d%s" % (jid, src.stem, n, src.suffix))
            n += 1
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            return {"ok": False, "why": "存不了：%s" % e}
        # 不再自动弹资源管理器：存一张就跳一次窗口，是在打断用户。
        # 想找文件用抽屉顶上的「打开文件夹」（2026-11-03 体验审计）。
        return {"ok": True, "why": "已存到「半瓢\\data\\output」：%s" % dst.name, "path": str(dst)}

    # ------------------------------------------------------------ 写
    def _ui_file(self) -> Path:
        return Path(self.cfg.data_dir) / "state" / "ui.json"

    def _ui_flag(self, key: str) -> bool:
        try:
            d = json.loads(self._ui_file().read_text(encoding="utf-8"))
            return bool(isinstance(d, dict) and d.get(key))
        except Exception:
            return False

    def _set_ui_flag(self, key: str, value: bool = True) -> None:
        try:
            f = self._ui_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
            d[key] = bool(value)
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def action(self, body: dict) -> dict:
        do = str(body.get("do") or "")
        if do == "onboarded":
            self._set_ui_flag("onboarded", True)
            return {"ok": True}
        if do == "pause":
            self.store.set_paused(True, by="ui", reason=str(body.get("reason") or ""))
            return {"ok": True, "paused": True}
        if do == "resume":
            self.store.set_paused(False)
            return {"ok": True, "paused": False}

        if do == "create":
            return self._create(body)

        if do == "login":
            return self._login(body)

        if do == "diag":
            return self._diag()

        if do == "feedback":
            return self._feedback(body)

        if do == "browser_down":
            return self._browser_down(body)

        if do == "account_add":
            return self._account_add(body)

        if do == "account_new":
            return self._account_new(body)

        if do == "login_cancel":
            return self._login_cancel(body)

        if do == "update_check":
            return self._update_check(force=True)

        if do == "update_install":
            return self._update_install()

        if do == "autostart_get":
            return self._autostart_get()

        if do == "autostart_set":
            return self._autostart_set(bool(body.get("on")))

        if do == "config_set":
            from .. import config as cfg_mod
            key = str(body.get("key") or "")
            # 白名单：只让面板改这几个，别把这里变成"任意改配置文件"的口子
            if key not in ("account_strategy", "account_fixed_id"):
                return {"ok": False, "why": "不认识的配置项：%s" % key}
            val = str(body.get("value") if body.get("value") is not None else "")
            if key == "account_strategy" and val not in ("balance", "roundrobin", "fixed"):
                return {"ok": False, "why": "取值不对"}
            try:
                cfg_mod.set_scalar(self._cfg().path, key, val)
            except Exception as e:
                return {"ok": False, "why": str(e)}
            return {"ok": True, "key": key, "value": val}

        if do == "global_install":
            from ..cli import cli_selfcheck, install_global, install_global_auto
            t = str(body.get("target") or "").strip()
            remove = bool(body.get("remove"))
            # 不给 target 就自动挑：用户装了什么 AI 就写什么，别让他做选择题
            r = (install_global_auto(remove=remove) if not t
                 else install_global(t, remove=remove))
            # 写进 md 只是"知道"，还得能"执行"——一起回报，别让用户以为通了
            if isinstance(r, dict):
                r["cli"] = cli_selfcheck()
                # 用户要求：失败必须能自证。这里带上能不能自动上报的信息，
                # 由前端决定是直接报还是给人一个按钮
                r["can_report"] = bool(r.get("failed")) and not remove
            return r

        if do == "browser_show":
            return self._browser_show(body, True)

        if do == "browser_hide":
            return self._browser_show(body, False)

        if do == "account_rm":
            return self._account_rm(body)

        if do == "account_rename":
            return self._account_rename(body)

        if do == "quit":
            return self._quit_app()

        if do == "chats_rm":
            return self._chats_rm(body)

        job_id = int(body.get("job") or 0)
        if do == "cancel":
            # 排队中：直接取消；在跑：杀驱动进程再取消（0.2.25 起支持）
            return self._cancel(job_id)

        if do == "retry":
            # allow_uncertain 只能由"我看过了"那个按钮带上来
            return self._retry(job_id, bool(body.get("allow_uncertain")))

        if do == "retry_other":
            return self._retry(job_id, bool(body.get("allow_uncertain")),
                               account_id=str(body.get("account") or ""))

        if do == "close_hint_ack":
            try:
                (Path(self.cfg.data_dir) / "state" / "close_hint.txt").unlink()
            except OSError:
                pass
            return {"ok": True}

        if do == "retry_all":
            return self._retry_all(kind=str(body.get("kind") or "all"))

        if do == "attention_dismiss":
            jid = int(body.get("job") or 0) or None
            return self._attention_dismiss(jid, str(body.get("kind") or "all"))

        if do == "undo_handle":
            return self._undo_handle(job_id)

        if do == "mark_found":
            return self._mark_found(job_id, list(body.get("items") or []))

        if do == "restart_browser":
            return self._restart_browser(job_id)

        if do == "net_check":
            return self._net_check(job_id)

        if do == "save_images":
            return self._save_images(job_id, list(body.get("files") or []))

        if do == "open_data":
            # 数据目录在 %LOCALAPPDATA%\Bampiao\data，是隐藏路径，用户自己找不到。
            # 设置页给一个「打开」，省得他们拼路径。
            return self._open_data_dir(str(body.get("sub") or ""))

        if do == "copy_image":
            return self._copy_image(body)

        if do == "save_image":
            return self._save_image(body)

        if do == "open":
            # os.startfile 只有 Windows 有；mac 上要用 open 命令
            try:
                target = str(self.store.dir_of(job_id))
                if hasattr(os, "startfile"):
                    os.startfile(target)
                elif sys.platform == "darwin":
                    subprocess.run(["open", target], check=False, timeout=10)
                else:
                    subprocess.run(["xdg-open", target], check=False, timeout=10)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "why": "打不开文件夹：%s" % e}

        return {"ok": False, "why": "不认识的动作：%s" % do}

    def _cli_cmd(self, *args: str) -> list[str]:
        """怎么起一个 bampiao 命令行子进程（面板要替用户跑 login）。"""
        import sys

        if getattr(sys, "frozen", False):
            exe = Path(sys.executable)
            # 打好的 bundle 里 GUI 和 CLI 是两个可执行文件，CLI 叫 bampiao：
            # Windows 是 bampiao-desktop.exe 同目录的 bampiao.exe，
            # macOS 是 半瓢.app/Contents/MacOS/ 下的 bampiao。
            for name in ("bampiao", "bampiao.exe"):
                alt = exe.with_name(name)
                if alt.exists() and alt != exe:
                    return [str(alt)] + list(args)
            return [str(exe)] + list(args)
        return [sys.executable, "-m", "image2_bampiao"] + list(args)

    def _acc_of(self, q: dict):
        """按 id 或名字找账号；**完全没指定**才用第一个。

        以前这里是"找不到就退回第一个"，于是前端传错 id 时会去动另一个账号：
        实测 `{do:"browser_hide", account:"nope"}` 回的是"已收起"——藏掉的是别人。
        单账号时看不出来，多账号时会关错号。现在找不到就返回 None，
        各动作自己回"没有这个账号"。
        """
        want = (q.get("account") or [""])[0]
        accs = self._cfg().accounts
        if not want:
            return accs[0] if accs else None
        for a in accs:
            if want in (a.id, a.name):
                return a
        return None

    def skill(self, which: str = "paste") -> dict:
        """把"贴给你 Agent 的那段"原文交出来。

        单一真源是 skills/ 目录里那几个 md（命令行 `bampiao skill` 读的也是它们），
        面板只负责展示和复制，绝不在前端硬编码一份副本。
        """
        from ..cli import (_SKILL_FILES, global_installed, global_targets,
                           skill_targets, skill_text)

        if which not in _SKILL_FILES:
            which = "paste"
        try:
            text = skill_text(which)
        except FileNotFoundError as e:
            return {"ok": False, "why": "安装包里少了 %s，重装一次试试。" % e}
        # 六家都要报，别只认 claude/codex —— 面板靠这个判断"第二步做完了没"
        every = list(global_targets()) + list(skill_targets())
        return {"ok": True, "which": which, "text": text,
                "global": {t: global_installed(t) for t in every}}

    def chats(self, q: dict) -> dict:
        """列会话。**带 60 秒缓存**：每次都要起一次驱动子进程（实测 10 秒），
        切一次页签等 10 秒谁都会以为卡死了。"""
        from ..driver import session as sess

        acc = self._acc_of(q)
        if acc is None:
            return {"ok": False, "why": "还没有账号"}
        try:
            limit = int((q.get("limit") or ["60"])[0])
        except ValueError:
            limit = 60
        hit = _CHATS_CACHE.get(acc.id)
        if hit and time.time() - hit[0] < CHATS_TTL:
            return hit[1]
        got = sess.chats(self.cfg, acc, "list", limit=limit)
        if got.get("ok"):
            _CHATS_CACHE[acc.id] = (time.time(), got)
        return got

    def _chats_rm(self, body: dict) -> dict:
        """删除会话。

        **服务端要再核一遍名单**：只删半瓢自己开过的，前端传什么进来都按这个筛。
        删会话不可撤销，这条校验是最后一道闸。
        """
        from ..driver import session as sess

        acc = self._acc_of({"account": [str(body.get("account") or "")]})
        if acc is None:
            return {"ok": False, "why": "还没有账号"}
        ids = body.get("ids") or []
        if isinstance(ids, str):
            ids = [x for x in ids.split(",") if x]
        info = sess.chats(self.cfg, acc, "list", limit=80)
        if not info.get("ok"):
            return {"ok": False, "why": info.get("why") or "列不出会话"}
        allowed = {x["id"] for x in (info.get("items") or []) if x.get("ours")}
        picked = [i for i in ids if i in allowed]
        if not picked:
            return {"ok": False, "why": "选中的都不在半瓢的名单里，没有删任何东西"}
        r = sess.chats(self.cfg, acc, "rm", ids=picked)
        _CHATS_CACHE.pop(acc.id, None)
        return {"ok": bool(r.get("ok")) and not r.get("failed"),
                "deleted": r.get("deleted") or [],
                "failed": r.get("failed") or [],
                "skipped": len(ids) - len(picked)}

    def _feedback(self, body: dict | None = None) -> dict:
        """打反馈包：诊断 + 日志 + 用户写的字 + 他贴的图。

        **日志一律默认带上**（脱敏后），不用用户勾——他勾不勾是他的事，
        但我要能复现就必须有日志。用户填的只是"哪里不对"，帮我对上号。
        """
        import base64

        body = body or {}
        note = str(body.get("note") or "")
        imgs = body.get("images") or []

        files: list[tuple[str, bytes]] = []
        for i, it in enumerate(imgs[:6]):          # 最多 6 张，别让包爆掉
            try:
                raw = base64.b64decode(str(it.get("b64") or ""), validate=False)
            except Exception:
                continue
            if not raw or len(raw) > 8 * 1024 * 1024:
                continue
            files.append((str(it.get("name") or ("图%d.png" % (i + 1))), raw))

        try:
            from .. import feedback as fb
            out = fb.collect(self.cfg, None, note=note, files=files)
        except Exception as e:
            return {"ok": False, "why": "打包失败：%s" % e}

        uploaded, why = False, ""
        try:
            uploaded, why = fb.upload(out, self.cfg)
        except Exception as e:
            why = str(e)

        # ok 必须等于"真的传上去了"：以前传失败也回 ok，面板就提示「已发出」，用户白等回音
        return {"ok": bool(uploaded), "path": str(out), "uploaded": bool(uploaded),
                "attachments": len(files),
                "why": ("反馈已发出，谢谢" if uploaded
                        else "没传上去，可能是网络不通（%s）。你写的内容还在，等网络好了再点一次「发送」。"
                        % (why or "未知原因")[:60])}

    def _browser_down(self, body: dict) -> dict:
        """关掉某个账号的浏览器。

        常驻浏览器是复用登录态用的，但用户得能自己收掉它——
        不然任务栏里躺着一个不认识的黑窗口，还占几百 MB。
        """
        import subprocess
        import sys

        acc = self._acc_of({"account": [body.get("account") or ""]})
        if acc is None:
            return {"ok": False, "why": "没有这个账号"}
        if run_alive(self._cfg(), acc.id) is True:
            return {"ok": False, "why": "这个号正在出图，等它跑完再关浏览器"}
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            r = subprocess.run(self._cli_cmd("browser", "down", acc.id, "--json"),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=40, creationflags=flags)
        except Exception as e:
            return {"ok": False, "why": "关不掉：%s" % e}
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        try:
            d = json.loads(out.splitlines()[-1]) if out else {}
        except ValueError:
            d = {}
        if not d.get("ok"):
            return {"ok": False, "why": d.get("why") or "没关掉，看日志"}
        return {"ok": True, "why": d.get("why") or "已关掉"}

    def _account_add(self, body: dict) -> dict:
        """在面板里加一个账号。

        以前只有命令行能加，面板空状态却写着「点击添加账号」——点进去什么都没有，
        0 个账号的用户在界面上是死路（实测卡死）。
        """
        from .. import browsers
        from .. import config as cfg_mod

        name = (str(body.get("name") or "").strip() or "新账号")[:20]
        browser = browsers.auto()
        if not browser:
            return {"ok": False, "why": "这台机器上没找到 Chrome 或 Edge，装一个再来"}
        try:
            acc = cfg_mod.append_account(self.cfg.path, name, browser)
            # 立刻重载配置：否则面板要等下一次重启才认得这个号
            self.cfg = cfg_mod.load(self.cfg.path)
        except Exception as e:
            return {"ok": False, "why": "加不上：%s" % e}
        return {"ok": True, "account": {"id": acc.id, "name": acc.name,
                                        "browser": acc.browser}}

    def _account_rm(self, body: dict) -> dict:
        """删账号：先关浏览器，再删配置。最后一个号不许删。"""
        from .. import config as cfg_mod
        from ..driver import browser as br

        acc = self._acc_of({"account": [body.get("account") or ""]})
        if acc is None:
            return {"ok": False, "why": "没有这个账号"}
        if len(self._cfg().accounts) <= 1:
            return {"ok": False,
                    "why": "只剩这一个号了，删了就出不了图。要删先再加一个。"}
        try:
            br.stop(self.cfg, acc)
            cfg_mod.remove_account(self.cfg.path, acc.id)
            self.cfg = cfg_mod.load(self.cfg.path)
        except Exception as e:
            return {"ok": False, "why": "删不掉：%s" % e}
        return {"ok": True, "why": "已删除「%s」" % acc.name}

    def _account_rename(self, body: dict) -> dict:
        """改显示名，不动 id / profile / 登录态。"""
        from .. import config as cfg_mod

        acc = self._acc_of({"account": [body.get("account") or ""]})
        if acc is None:
            return {"ok": False, "why": "没有这个账号"}
        name = (str(body.get("name") or "").strip())[:20]
        if not name:
            return {"ok": False, "why": "名字不能为空"}
        try:
            cfg_mod.rename_account(self.cfg.path, acc.id, name)
            self.cfg = cfg_mod.load(self.cfg.path)
        except Exception as e:
            return {"ok": False, "why": "改不了：%s" % e}
        return {"ok": True, "why": "已改名为「%s」" % name}

    def _quit_app(self) -> dict:
        """退出整个半瓢（面板「设置」页那个按钮用）。

        真正的退出逻辑在桌面壳手上（收浏览器、摘托盘、关窗口），
        面板只负责转发一句本机指令——这样两个入口不会有两套退出代码。
        """
        from .. import desktop

        ok, msg = desktop.send_command("quit", wait=0)
        return {"ok": ok, "why": msg}

    def _diag(self) -> dict:
        """生成诊断文件到桌面。

        让用户点一个按钮，比让他去找终端敲 `bampiao diag` 靠谱得多。
        """
        import subprocess
        import sys

        out = Path.home() / "Desktop" / "bampiao-diag.txt"
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            r = subprocess.run(self._cli_cmd("diag", "--out", str(out)),
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=flags, timeout=60)
        except Exception as e:
            return {"ok": False, "why": "生成失败：%s" % e}
        if r.returncode != 0 or not out.exists():
            return {"ok": False, "why": "生成失败，看看 ~/.bampiao/logs 里有没有线索"}
        return {"ok": True, "note": "已写到桌面 bampiao-diag.txt", "path": str(out)}

    def _login(self, body: dict) -> dict:
        """替用户打开登录窗口。

        登录会弹一个独立的浏览器窗口并等到登好为止（最长 5 分钟），
        所以这里**不阻塞**面板：起个后台进程就返回，状态由面板轮询刷新。
        """
        import subprocess
        import sys

        acc_id = str(body.get("account") or "")
        if acc_id not in [a.id for a in self._cfg().accounts]:
            return {"ok": False, "why": "没有这个账号：%s" % acc_id}
        ph = login_flow.read_phase(self.cfg, acc_id)
        if ph.get("phase") == "waiting":
            return {"ok": True, "note": "登录窗口已经开着了，在那个窗口里登就行"}
        extra = ["--discard-on-fail"] if body.get("new") else []
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000      # CREATE_NO_WINDOW：别再弹一个黑框
        try:
            # 先占住"等待中"：子进程起来要一两秒，这期间面板轮询不能显示成没登录
            login_flow.write_phase(self.cfg, acc_id, "waiting", started=time.time(),
                                   new=bool(body.get("new")), pid=0)
            # 必须带 --config：不带的话登录子进程去读默认配置，开的是另一个号的浏览器
            subprocess.Popen(self._cli_cmd("--config", str(self.cfg.path), "login", acc_id,
                                           "--timeout", "600", *extra),
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=flags)
        except Exception as e:
            login_flow.write_phase(self.cfg, acc_id, "failed", note=str(e)[:120])
            return {"ok": False, "why": "启动登录失败：%s" % e}
        return {"ok": True, "note": "登录窗口正在打开，登好会自动藏回后台"}

    def _account_new(self, body: dict) -> dict:
        """「＋」：建号并立刻弹出登录窗口，一步到位。

        以前是两步：先加一个空号占位，再在空号上点「登录」——用户的心智是
        "点加号 → 去窗口里登 → 回来就有了"。没登上（取消/关窗/超时）这个号自动撤掉。
        """
        from .. import browsers
        from .. import config as cfg_mod

        want = str(body.get("browser") or "")
        if want:
            if want not in ("chrome", "edge"):
                return {"ok": False, "why": "目前只支持 Chrome 和 Edge"}
            if not browsers.find(want):
                return {"ok": False, "why": "这台电脑上没装 %s" % ("Chrome" if want == "chrome" else "Edge")}
            browser = want
        else:
            browser = browsers.auto()
        if not browser:
            return {"ok": False, "why": "这台机器上没找到 Chrome 或 Edge，装一个再来"}
        names = {a.name for a in self._cfg().accounts}
        i = len(names) + 1
        while ("账号 %d" % i) in names:
            i += 1
        try:
            acc = cfg_mod.append_account(self.cfg.path, "账号 %d" % i, browser)
            self.cfg = cfg_mod.load(self.cfg.path)
        except Exception as e:
            return {"ok": False, "why": "加不上：%s" % e}
        r = self._login({"account": acc.id, "new": True})
        if not r.get("ok"):
            try:
                cfg_mod.remove_account(self.cfg.path, acc.id)
                self.cfg = cfg_mod.load(self.cfg.path)
            except Exception:
                pass
            return r
        return {"ok": True, "account": {"id": acc.id, "name": acc.name}}

    # ------------------------------------------------------------ 在线更新
    # 更新状态放在实例上（以前是类属性，所有 Panel 共用一份：
    # 测试之间会互相污染，将来万一多开一个 Panel 也会串状态）
    _upd_lock = threading.Lock()          # waiting 的"检查+置位"必须原子，否则会起两个等待线程

    def _update_check(self, force: bool = False) -> dict:
        from .. import update

        c = self._UPD["check"]
        if not force and c and time.time() - c.get("checked_at", 0) < 1800:
            return c
        self._UPD["check"] = c = update.check()
        return c

    def update_state(self) -> dict:
        """给概览用：没查过就在后台查一次，不让面板等网络。"""
        if self._UPD["check"] is None and not self._UPD["busy"]:
            self._UPD["busy"] = True

            def bg():
                try:
                    self._update_check()
                finally:
                    self._UPD["busy"] = False
            threading.Thread(target=bg, daemon=True, name="update-check").start()
        # waiting = 有图在跑，正在等这一轮结束再装。界面要显示出来，
        # 否则用户点了更新看到「没动静」，会以为按钮坏了。
        from .. import update as _u

        # 查到新版本就先在后台下：服务器只有 5Mbps，57MB 要 96 秒，
        # 这 96 秒不该算在用户头上（2026-10-31 用户反馈"每次更新下载很久"）。
        _c = self._UPD.get("check")
        if _c and _c.get("ok") and _c.get("newer") and _u.platform_key() == "win":
            self._prefetch(_c)

        return {"check": self._UPD["check"], "dl": self._UPD["dl"],
                "prefetch": self._UPD.get("prefetch"),
                "waiting": bool(self._UPD.get("waiting")),
                # Mac 版没有安装器，按钮文案得跟着变，否则写着"下载并安装"却什么都没发生
                "can_install": _u.platform_key() == "win"}

    def _unpause_if_ours(self) -> None:
        """把「等升级」时自己下的暂停还回去。

        **必须再确认一次 pause.json 里还是 by=update**：等待期间用户可能自己
        按了暂停（by=ui / tray），那是他的意图，不能因为我们是先手就替他放开
        （独立验证 2026-10-31 抓到过一次）。
        """
        if not self._UPD.pop("paused_by_us", False):
            return
        try:
            if self.store.pause_info().get("by") == "update":
                self.store.set_paused(False, by="update")
        except Exception:
            pass

    def _wait_then_update(self, c: dict) -> None:
        """有单在跑就等着，跑完了再下载安装。**空档到了也不解除暂停**。

        为什么不能解（这条我上一轮修反过）：一旦放开，下载 + 启动安装程序这几十秒
        里队列会立刻领走新单，安装器的 upgrade-guard 就会返回 3 而中止升级——
        用户反复看到「装不上，可能被安全软件拦了」，而真相是我们自己把门开了。
        暂停一直保持到装完；重启时 desktop 的 clear_update_pause 负责收尾。
        只有"等不到空档"和"装失败"这两条路径才还暂停。

        **最多等 2 小时**：一直有单在跑说明用户在批量干活，旧版本一天不亏。
        """
        import time as _t

        from .. import update

        deadline = _t.time() + 2 * 3600
        while _t.time() < deadline:
            if not update.busy_reason(self.cfg, getattr(self, "store", None)):
                self._UPD["waiting"] = False
                self._UPD["check"] = c          # 让它以为已经是最新（避免界面反复提示）
                self._start_download(c)         # 暂停**保持**，装完重启再清
                return
            _t.sleep(20)
        # 等不到空档：放弃这次升级，把暂停还回去（否则队列一直停着）
        self._UPD["waiting"] = False
        self._unpause_if_ours()

    def _prefetch(self, c: dict) -> None:
        """后台静默下载新版本。下载完只是"准备好了"，装不装由用户决定。

        为什么不直接装：装之前要 quit 掉正在出图的进程。下载可以随时做，
        安装必须等空档——两件事分开，用户就不用盯着 96 秒的进度条。
        """
        pf = self._UPD.get("prefetch")
        if not isinstance(pf, dict):
            pf = {}
            self._UPD["prefetch"] = pf
        if pf.get("state") in ("downloading", "ready"):
            return
        if (self._UPD.get("dl") or {}).get("phase") in ("downloading", "installing"):
            return                      # 用户已经手动触发了，别下两份
        pf.clear()
        pf.update({"state": "downloading", "got": 0, "total": c.get("size") or 0})

        def run():
            try:
                from .. import update

                def prog(got, total):
                    pf["got"], pf["total"] = got, total

                p = update.download(c, self.cfg.state_dir / "update", progress=prog)
                pf["path"] = str(p)
                pf["state"] = "ready"
            except Exception as e:      # noqa: BLE001
                # 静默失败：用户没点什么东西，不该弹窗烦他；点更新时会重下
                pf["state"] = "failed"
                pf["why"] = str(e)[:120]

        threading.Thread(target=run, daemon=True, name="update-prefetch").start()

    def _install_from(self, path, st: dict) -> None:
        """启动安装程序并让出进程。现下的包和后台下好的包走同一条路。"""
        from .. import platform as plat
        from .. import update

        try:
            ipid = update.launch_installer(path)
            time.sleep(1.5)
            if ipid and not plat.pid_alive(ipid):
                st["phase"] = "failed"
                st["why"] = "安装程序启动后立刻退出了（可能被安全软件拦了），请手动装"
                self._unpause_if_ours()     # 没装成，队列得恢复
                return
            st["phase"] = "installing"
            self._UPD["dl"] = st
            # 让出文件：安装程序要覆盖正在运行的 exe。放在最后一步，
            # 前面任何一步失败都还留着半瓢活着，能跟用户解释。
            self._quit_app()
        except Exception as e:          # noqa: BLE001
            st["phase"] = "failed"
            st["why"] = str(e)[:160]
            self._unpause_if_ours()

    def _start_download(self, c: dict) -> None:
        """下载 + 安装。如果后台已经下好了，直接进安装，省掉那 96 秒。"""
        import os as _os

        pre = self._UPD.get("prefetch") or {}
        if pre.get("state") == "ready" and pre.get("path") and _os.path.exists(pre["path"]):
            st = {"phase": "installing", "got": c.get("size") or 0,
                  "total": c.get("size") or 0}
            self._UPD["dl"] = st
            threading.Thread(target=self._install_from, args=(pre["path"], st),
                             daemon=True, name="update-inst").start()
            return

        st = {"phase": "downloading", "got": 0, "total": c.get("size") or 0}
        self._UPD["dl"] = st

        def run():
            try:
                def prog(got, total):
                    st["got"], st["total"] = got, total

                from .. import update

                path = update.download(c, self.cfg.state_dir / "update", progress=prog)
                st["phase"] = "installing"
                self._install_from(path, st)
            except Exception as e:      # noqa: BLE001
                st["phase"] = "failed"
                st["why"] = str(e)[:160]
                self._unpause_if_ours()     # 下载/启动失败，别让队列一直停着

        threading.Thread(target=run, daemon=True, name="update-dl").start()

    def _autostart_get(self) -> dict:
        """开机自启现在是开是关。

        为什么要有这个接口：2026-11-06 用户反馈「半瓢好像会一直自己启动，
        我经常关掉它，然后不知道啥时候他又自己打开了，有些困扰」。
        翻下来是安装时默认开了自启，而**面板里根本没有能关掉它的地方**——
        用户只能去注册表里找，那等于没给选择。
        """
        from .. import autostart

        try:
            st = autostart.status()
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "enabled": False, "why": str(e)}
        return {"ok": True, "enabled": bool(st.get("enabled")),
                "target": st.get("target") or "", "exists": bool(st.get("exists"))}

    def _autostart_set(self, on: bool) -> dict:
        from .. import autostart

        try:
            if on:
                autostart.enable()
            else:
                autostart.disable()
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "enabled": not on, "why": str(e)}
        return self._autostart_get()

    def _update_install(self) -> dict:
        """下载 → 校验 → 静默安装 → 退出本程序（安装程序装完会重新打开半瓢）。"""
        from .. import update

        c = self._update_check()
        if not c.get("ok"):
            return {"ok": False, "why": c.get("why") or "查不到更新"}
        if not c.get("newer"):
            return {"ok": False, "why": "已经是最新版了"}
        if (self._UPD["dl"] or {}).get("phase") in ("downloading", "installing"):
            return {"ok": True, "why": "正在更新"}
        _pf = self._UPD.get("prefetch") or {}
        if _pf.get("state") == "downloading":
            # 后台已经在下了，别再下一份（服务器 5Mbps，下两份就是双倍时间）
            return {"ok": True, "why": "新版本正在后台下载，下完会提示你重启升级"}
        if update.platform_key() != "win":
            return {"ok": False, "why": "Mac 版请手动下载安装：%s" % c.get("url")}

        # 有单在跑就先别装：安装器会 quit 掉正在出图的那个进程，图直接没了
        # （2026-10-30 实测把用户的 R101 掐了）。这里改成"等空档再装"，对用户无感。
        why_busy = update.busy_reason(self.cfg, getattr(self, "store", None))
        if why_busy:
            with self._upd_lock:                # 连点两次会起两个等待线程，抢着装
                already = bool(self._UPD.get("waiting"))
                self._UPD["waiting"] = True
            if not already:
                try:
                    # 暂停派新单，否则"空档"可能永远不来。
                    # **只在用户没暂停时才接手**：他要是自己停着队列，我们不该越权。
                    if not self.store.paused():
                        self.store.set_paused(True, by="update", reason="等这轮跑完再升级")
                        self._UPD["paused_by_us"] = True
                except Exception:
                    pass
                threading.Thread(target=self._wait_then_update, args=(c,),
                                 daemon=True, name="update-wait").start()
            return {"ok": True,
                    "why": "%s，跑完会自动升级（不会打断）" % why_busy}
        self._start_download(c)
        return {"ok": True, "why": "开始下载新版本"}

    def _attention_cached(self) -> dict:
        """待处理要扫全部 job.json，overview 每 2 秒一次，必须缓存。

        2 秒 TTL：面板 2-6 秒轮询一次，托盘 5 秒一次，两边共用同一份结果。
        """
        now = time.time()
        hit = getattr(self, "_attn_cache", None)
        if hit and now - hit[0] < 2:
            return hit[1]
        try:
            # 取全量再在内存里按 kind 切：面板要同时显示"出图几张 / 文本几条"，
            # 只缓存前 3 条会让横幅的数字和"看这 N 单"对不上（2026-11-06）。
            data = self.store.attention(limit=1000000)
        except Exception:
            data = {"count": 0, "by_class": {}, "jobs": [], "total_jobs": 0,
                    "by_kind": {}}
        self._attn_cache = (now, data)
        return data

    def _paused_min(self, acc_id: str) -> int:
        """还要挂起多少分钟。0 = 没挂起。"""
        from ..driver import session_health as sh

        rec = sh.read_pause(str(self.cfg.data_dir), acc_id)
        if not rec:
            return 0
        return max(1, int(round((float(rec.get("until") or 0) - time.time()) / 60)))

    def _crash_count(self) -> int:
        """崩溃记录条数。带 30 秒缓存：overview 是轮询的，别每次都扫目录。"""
        now = time.time()
        hit = getattr(self, "_crash_cache", None)
        if hit and now - hit[0] < 30:
            return hit[1]
        from .. import crashlog

        n = len(crashlog.list_crashes(self.cfg.data_dir))
        self._crash_cache = (now, n)
        return n

    def _browser_running(self, acc_id: str) -> bool:
        """这个号的浏览器进程还在不在（窗口收起来也算在）。"""
        try:
            from .. import platform as plat
            from ..driver import browser as br

            h = br.read_handle(self._cfg(), acc_id)
            return bool(h and plat.pid_alive(h.pid))
        except Exception:
            return False

    def start_background(self) -> None:
        """面板启动后的两个后台杂活：补邮箱、按 idle_recycle_min 回收闲置浏览器。"""
        threading.Thread(target=self._scan_emails, daemon=True,
                         name="email-scan").start()
        threading.Thread(target=self._recycle_idle_browsers, daemon=True,
                         name="idle-recycler").start()

    def _recycle_idle_browsers(self) -> None:
        """按时关掉闲置的浏览器进程。

        `idle_recycle_min` 以前只显示在 doctor 里，代码根本没执行——用户反馈
        「浏览器都收了还占不占内存」时，13 个号实际挂了 117 个进程、约 2.5 GB。
        这里只关：窗口不可见、没在等人登录/看页面、没有出图锁、闲置超过配置时间的。
        """
        time.sleep(15.0)
        while True:
            try:
                cfg = self._cfg()
                from ..driver import browser as br

                names = {a.id: a.name for a in cfg.accounts}
                closed = br.recycle_idle(
                    cfg, self.store,
                    busy=lambda aid: run_alive(cfg, aid) is True)
                for aid in closed:
                    print("闲置回收：%s 的浏览器已关（释放内存，下次出图会自动重开）"
                          % names.get(aid, aid))
            except Exception:
                pass
            time.sleep(60.0)

    def _scan_emails(self) -> None:
        """给没有邮箱的账号读一次 /api/auth/session。

        新号登录时本来就写 email；老账号（升级前加的、批量导入的）没有。
        这里只连已经开着的 CDP，**绝不启动浏览器**，也不碰正在出图的号。
        每 2 分钟扫一次缺邮箱的号，全部补齐后就不再有额外动作。
        """
        time.sleep(3.0)
        while True:
            try:
                cfg = self._cfg()
                for acc in list(cfg.accounts):
                    st = self.store.account(acc.id)
                    if st.login_email:
                        continue
                    if not self._browser_running(acc.id):
                        continue
                    if run_alive(cfg, acc.id) is True:
                        continue            # 正在出图，别去碰同一个页面
                    info = login_flow.read_state(acc.port)
                    email = str((info or {}).get("email") or "").strip()
                    if not email:
                        continue
                    st.login_email = email
                    st.login = "ok"
                    st.login_checked_at = time.time()
                    self.store.save_account(st)
            except Exception:
                pass
            time.sleep(120.0)

    def _window_ok(self, acc_id: str):
        """这个号的浏览器有没有窗口。None = 没开（不算故障）。

        带 5 秒缓存：overview 是轮询的，不能每 2 秒把三个账号的窗口枚举一遍。
        """
        now = time.time()
        cache = getattr(self, "_win_cache", None)
        if cache is None:
            cache = self._win_cache = {}
        hit = cache.get(acc_id)
        if hit and now - hit[0] < 5:
            return hit[1]
        from ..driver import browser as br
        from .. import platform as plat

        val = None
        try:
            h = br.read_handle(self._cfg(), acc_id)
            if h and plat.pid_alive(h.pid):
                val = bool(any((w.get("title") or "").strip()
                               for w in plat.list_windows(h.pid)))
        except Exception:
            val = None
        cache[acc_id] = (now, val)
        return val

    def _browser_show(self, body: dict, show: bool) -> dict:
        """账号卡上的「打开浏览器 / 收起」：想看看这个号的 ChatGPT 页面时用。

        打开期间巡逻不藏它；收起（或用户直接关掉窗口）之后恢复静默。
        """
        from .. import platform as plat
        from ..driver import browser as br

        acc = self._acc_of({"account": [body.get("account") or ""]})
        if acc is None:
            return {"ok": False, "why": "没有这个账号"}
        if login_flow.read_phase(self.cfg, acc.id).get("phase") == "waiting":
            return {"ok": False, "why": "正在登录，先在登录窗口里登完"}
        if not show:
            login_flow.write_phase(self.cfg, acc.id, "idle")
            # 看过窗口=处理过验证，解除挂起，让队列继续跑
            try:
                from ..driver import session_health as sh
                sh.clear_paused(str(self.cfg.data_dir), acc.id)
            except Exception:
                pass
            h = br.read_handle(self.cfg, acc.id)
            # 之前这里是 `if h and 活着: hide` 然后无条件返回"已收起"：
            # handle 丢了（换版本、手动清过 state）或 pid 过期时其实什么都没做，
            # 却告诉用户收好了——人看着任务栏那个窗口，只会以为功能坏了（2026-10-30 反馈）。
            pids = []
            if h and plat.pid_alive(h.pid):
                pids.append(h.pid)
            if not pids or plat.window_visible(pids[0]):
                # 兜底：按 profile 路径反查同一个人格的所有 chrome 主进程，
                # 不做这一步的话，Chrome 自己重启过（pid 变了）就再也收不掉。
                try:
                    pids = sorted(set(pids + plat.find_pids_with(str(acc.profile))))
                except Exception:
                    pass
            if not pids:
                if plat.cdp_alive(acc.port):
                    return {"ok": False, "why": "它的浏览器还开着，但半瓢认不出是哪个进程；点「关掉它的浏览器」能彻底收掉"}
                return {"ok": True, "why": "它已经不在了"}
            for _ in range(3):
                for pid in pids:
                    if plat.pid_alive(pid):
                        plat.hide_window(pid)
                if not any(plat.window_visible(p) for p in pids):
                    break
                time.sleep(0.2)
            still = [p for p in pids if plat.window_visible(p)]
            if still:
                return {"ok": False, "why": "没藏住（窗口还在），点「关掉它的浏览器」彻底收掉"}
            return {"ok": True, "why": "已收起"}
        # 先记「用户要看」再叫窗口：反过来的话，刚启动 8 秒内的隐藏线程和巡逻
        # 会在两步之间把刚叫出来的窗口又藏回去（实测）。
        h = br.read_handle(self.cfg, acc.id)
        login_flow.write_phase(self.cfg, acc.id, "shown", browser_pid=h.pid if h else 0)
        time.sleep(0.15)
        try:
            br.ensure(self.cfg, acc, visibility="show")
        except Exception as e:
            login_flow.write_phase(self.cfg, acc.id, "idle")
            return {"ok": False, "why": "打不开：%s" % str(e)[:120]}
        h = br.read_handle(self.cfg, acc.id)
        # 租约：这段时间内静默巡逻不许把它藏回去。没有租约的话，用户点了「打开浏览器」
        # 也可能被 0.8 秒一轮的巡逻当场收走，看起来就是"点了没反应"（2026-11-02 反馈）。
        login_flow.write_phase(self.cfg, acc.id, "shown",
                               browser_pid=h.pid if h else 0,
                               until=time.time() + SHOW_LEASE)
        if h:
            plat.show_window(h.pid, activate=True)

        # 别急着说"已打开"：Chrome 冷启动时窗口要几秒才出来，其间 show_window 是空打。
        # 逐轮重试，真看到可见窗口才算成功；看不到就如实报错，别骗人。
        deadline = time.time() + 12
        while time.time() < deadline:
            if h and plat.window_visible(h.pid):
                return {"ok": True, "why": "浏览器已打开"}
            h2 = br.read_handle(self.cfg, acc.id)
            if h2:
                h = h2
                plat.show_window(h.pid, activate=True)
                login_flow.write_phase(self.cfg, acc.id, "shown",
                                       browser_pid=h.pid, until=time.time() + SHOW_LEASE)
            time.sleep(0.5)
        return {"ok": False,
                "why": "窗口没出来（可能在启动或被静默巡逻藏了），再点一次试试"}

    def _login_cancel(self, body: dict) -> dict:
        acc_id = str(body.get("account") or "")
        ph = login_flow.read_phase(self.cfg, acc_id)
        if ph.get("phase") != "waiting":
            return {"ok": True, "why": "没有在等登录"}
        login_flow.cancel_path(self.cfg, acc_id).touch()
        return {"ok": True, "why": "已取消"}

    def _create(self, body: dict) -> dict:
        """面板里"出一张"。投一单进队列，由常驻的调度器去跑。

        面板只负责建单，不直接调驱动 —— 这样和 CLI 共用同一条排队/冷却/失败处理。
        """
        from ..core.models import Item

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "why": "先写一句想看什么画面"}
        if len(prompt) > 4000:
            return {"ok": False, "why": "提示词太长了（最多 4000 字）"}

        acc_id = str(body.get("account") or "").strip()
        if acc_id in ("", "auto"):
            # 自动挑：**今天出图最少**的那个（都在休息/掉登录才报错）。
            # 以前是"能用的第一个"，于是所有单都压在 acc1 上：撞额度那天
            # acc1 出了 120 张、acc2 只出了 14 张，acc2 全程闲着（2026-10-31）。
            # 先滤掉"现在不能用的"，再按用户选的策略挑（以前这里硬编码"今天最少的"，
            # 用户改了策略面板却不认，两边行为对不上）
            from ..config import choose_by_strategy
            usable = []
            for a in self._cfg().accounts:
                st = self.store.account(a.id)
                if st.login == "required" or st.cooling():
                    continue
                usable.append(a)
            load = self.store.active_load()
            picked = choose_by_strategy(
                self._cfg(), usable,
                lambda a: self.store.account(a.id).stat_today,
                lambda a: load.get(a.id, 0))
            acc_id = picked.id if picked else ""
            if not acc_id:
                return {"ok": False,
                        "why": "没有能用的号：都在休息或没登录。去「账号」页能看到各自还剩多久。"}
        elif acc_id not in [a.id for a in self._cfg().accounts]:
            return {"ok": False, "why": "没有这个账号：%s" % acc_id}

        try:
            n = max(1, min(8, int(body.get("n") or 1)))
        except (TypeError, ValueError):
            n = 1
        aspect = str(body.get("aspect") or "1:1")
        alpha = bool(body.get("alpha"))
        items = [
            Item(id="S%02d" % (i + 1),
                 prompt=(prompt if n == 1 else
                         "%s（第 %d/%d 张，构图要明显不同）" % (prompt, i + 1, n)),
                 aspect=aspect, alpha=alpha)
            for i in range(n)
        ]
        job, created = self.store.new_job(
            account_id=acc_id, items=items,
            brief=str(body.get("brief") or prompt)[:60].replace("\n", " "),
            tag=str(body.get("tag") or ""),
            submitter="面板",
        )
        return {"ok": True, "job": job.id, "created": created, "account": acc_id}

    def _retry(self, job_id: int, allow_uncertain: bool = False,
               account_id: str = "") -> dict:
        """重投。源单会被标成"已处理"，否则红灯永远灭不掉（0.2.25 的核心修复）。"""
        src = self.store.load(job_id)
        if not src:
            return {"ok": False, "why": "没有 R%d" % job_id}
        from ..core.models import ITEM_OK

        # TIMEOUT_UNCERTAIN / ASK_TIMEOUT 要排除：那是发出去之后断了/等超时，
        # 远端可能已经出图（或已经答完）。自动重投会重复出图、重复烧额度。
        # resolved_by=found 更要排除：用户已确认"网页上有图"，再画一次就是白烧额度。
        todo = [it for it in src.items
                if it.status != ITEM_OK
                and it.resolved_by != "found"
                and (allow_uncertain or it.error_code not in ("TIMEOUT_UNCERTAIN", "ASK_TIMEOUT"))]
        if not todo:
            return {"ok": False,
                    "why": "这一单没有可重投的条目（要么都成了，要么都标着「结果不确定」）。"}

        if self.store.paused():
            return {"ok": False, "need": "resume",
                    "why": "队列暂停中，重投了也不会跑。先恢复队列。"}

        target = src.account_id
        if account_id:
            acc0 = next((a for a in self.cfg.accounts if a.id == account_id), None)
            if not acc0:
                return {"ok": False, "why": "没有这个账号：%s" % account_id}
            target = acc0.id

        st = self.store.account(target)
        if st is not None and st.login == "required":
            return {"ok": False, "need": "login",
                    "why": "这个号现在需要登录，重投也只会排在队列里不动。先去「账号」页登录。"}
        if st is not None and st.cooling():
            left = max(1, int((st.cooldown_until - __import__("time").time()) / 60))
            return {"ok": False, "need": "retry_other",
                    "why": "这个号还在休息（还要 %d 分钟）。换一个号更快。" % left}

        from ..core.models import Item

        new_items = [Item(id=it.id, prompt=it.prompt, aspect=it.aspect,
                          alpha=it.alpha, refs=list(it.refs)) for it in todo]
        job, created = self.store.new_job(
            account_id=target, items=new_items,
            brief="补 R%d（%s）" % (job_id, ",".join(it.id for it in todo)),
            tag=src.tag, submitter="ui",
            # 幂等键带上目标账号：换号重投不会撞旧键，也不会因为双击建两单
            request_id="retry-%d-%s-%s" % (
                job_id, ",".join(sorted(it.id for it in todo)), target),
            retry_of=job_id,
            # ★ 类型必须跟着源单走。以前这里没传，默认 image ——
            # 点一次「重投没画成的 N 张」，文本单会被当成出图重投，
            # 拿"只回四个字"去画图，白烧额度（2026-11-06）。
            kind=(src.kind or "image"))

        # 源单标已处理——这一步是"红灯会灭"的唯一依据
        src2 = self.store.load(job_id) or src
        src2.handled_at = __import__("time").time()
        src2.handled_by = "retry"
        self.store.save(src2)
        return {"ok": True, "job": job.id, "created": created,
                "items": len(new_items), "account": target}

    def _retry_all(self, kind: str = "all") -> dict:
        """「重投没画成的 N 张」：一次把所有待处理单都重投掉。

        不能在面板里循环调用 retry —— 横幅上的 a.jobs 是**前 3 条**，
        点“重投 22 张”实际只重投了 3 单，剩下的还红着（2026-11-03 实测）。

        ``kind``：只重投当前 tab 的类型。文本单不能混进出图的重投里。
        """
        att = self.store.attention(limit=1000000, kind=kind)
        done, skipped, uncertain = 0, 0, 0
        new_ids = []
        for row in att.get("jobs") or []:
            if row.get("uncertain_only"):
                skipped += 1        # 结果不确定的不许自动重投（可能已经出图）
                uncertain += 1
                continue
            r = self._retry(int(row["id"]))
            if r.get("ok"):
                done += 1
                new_ids.append(r["job"])
            else:
                skipped += 1
                # 「不确定」不是"没投成"：ASK_TIMEOUT/TIMEOUT_UNCERTAIN 会被 _retry
                # 挡下来，提示语里带这句。分开计数，toast 才不会乱归因。
                if "结果不确定" in str(r.get("why") or ""):
                    uncertain += 1
        return {"ok": True, "done": done, "skipped": skipped,
                "skipped_uncertain": uncertain, "jobs": new_ids,
                "items": att.get("count") or 0}

    def _attention_dismiss(self, job_id: int | None, kind: str = "all") -> dict:
        """标记已处理。返回可撤销的 id 列表。

        ``kind``：只处理当前 tab 的类型。"我都知道了"点一次不该把另一个 tab
        的红点也一起灭掉（2026-11-06）。
        """
        import time as _t

        ids: list[int] = []
        if job_id:
            job = self.store.load(job_id)
            if job:
                job.handled_at = _t.time()
                job.handled_by = "dismiss"
                self.store.save(job)
                ids.append(job.id)
        else:
            now = _t.time()
            targets = {r["id"] for r in
                       self.store.attention(limit=1000000, kind=kind).get("jobs", [])}
            for job in self.store._all_jobs():
                if job.handled_at or job.id not in targets:
                    continue
                job.handled_at = now
                job.handled_by = "dismiss"
                self.store.save(job)
                ids.append(job.id)
        return {"ok": True, "ids": ids, "count": len(ids)}

    def _undo_handle(self, job_id: int) -> dict:
        """单条撤销：清 handled_* 和本单所有 resolved_by，重新计入待处理。"""
        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}
        job.handled_at = None
        job.handled_by = ""
        n = 0
        for it in job.items:
            if it.resolved_by:
                it.resolved_by = ""
                n += 1
        self.store.save(job)
        return {"ok": True, "restored": n}

    def _mark_found(self, job_id: int, item_ids: list[str] | None = None) -> dict:
        """「网页上有图，我收下」：这一张标 found，不计入没画成、也不再重投。

        **只收下「不确定」的那几张**。以前是"所有没成功的都标 found"——那会把
        额度用完、参考图丢了这些一眼就知道没出图的条目一起抹掉，
        用户点一下「收下」，一批没画的图就人间蒸发了（评审抓到的坑）。
        """
        import time as _t

        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}
        want = set(item_ids or [])
        n = 0
        for it in job.items:
            if it.status == "ok" or it.resolved_by:
                continue
            unsure = (it.error_code or "").upper() == "TIMEOUT_UNCERTAIN" or (it.failure_class or "") == "unsure"
            if want and it.id not in want:
                continue
            if not want and not unsure:
                continue
            it.resolved_by = "found"
            n += 1
        if n == 0:
            return {"ok": False, "why": "这一单没有等你确认的图了。"}
        left = any(it.status != "ok" and it.resolved_by != "found" for it in job.items)
        if not left:
            job.handled_at = _t.time()
            job.handled_by = "found"
        self.store.save(job)
        return {"ok": True, "marked": n, "all_done": not left}

    def _cancel(self, job_id: int) -> dict:
        """停下这一张：杀掉该号的驱动进程，条目标成取消（已出的图保留）。"""
        import time as _t

        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}
        acc = next((a for a in self.cfg.accounts if a.id == job.account_id), None)
        killed = 0
        if acc is not None:
            try:
                from ..driver import session as _sess

                pid = _sess.read_driver_pid(self.cfg, acc)
                if pid:
                    from .. import platform as _plat

                    _plat.kill_tree(pid)
                    killed = pid
            except Exception:
                pass
        from ..core.models import ITEM_CANCELLED, ITEM_QUEUED, JOB_CANCELLED  # noqa: F401

        for it in job.items:
            # running 的也要标：驱动已经被杀了，让它一直挂着"在跑"是骗人
            if it.status in (ITEM_QUEUED, "running"):
                it.status = ITEM_CANCELLED
                it.error_code = "CANCELLED"
                it.reason = "你手动停下了" if killed else "你手动停下了（没有找到在跑的进程）"
        job.status = JOB_CANCELLED
        job.finished_at = _t.time()
        job.handled_at = _t.time()
        job.handled_by = "dismiss"
        job.error_code = "CANCELLED"
        job.reason = "你手动停下了"
        self.store.save(job)
        return {"ok": True, "killed": killed}

    def _restart_browser(self, job_id: int) -> dict:
        """重启这个号的浏览器（页面卡住的主按钮）。"""
        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}
        acc = next((a for a in self.cfg.accounts if a.id == job.account_id), None)
        if acc is None:
            return {"ok": False, "why": "这一单的账号不在了"}
        try:
            from ..driver import browser as _br

            try:
                _br.stop(self.cfg, acc)
            except Exception:
                pass
            _br.ensure(self.cfg, acc)
        except Exception as e:
            return {"ok": False, "why": "浏览器没重启成功：%s" % str(e)[:120]}
        job.last_action = {"do": "restart_browser", "ok": True,
                           "at": __import__("time").time(), "note": ""}
        self.store.save(job)
        return {"ok": True, "account": acc.id}

    def _net_check(self, job_id: int) -> dict:
        """三行结论：系统代理 / 能不能连 ChatGPT / 建议。"""
        import json as _json
        import time as _t
        import urllib.request

        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}
        from .. import platform as _plat

        proxy = ""
        proxy_ok = None
        try:
            proxy = _plat.system_proxy()
            if proxy:
                proxy_ok = _plat.proxy_works(proxy)
        except Exception:
            pass

        reachable = False
        ms = 0
        try:
            t0 = _t.time()
            req = urllib.request.Request("https://chatgpt.com/", method="HEAD")
            with urllib.request.urlopen(req, timeout=6) as r:
                reachable = r.status < 500
            ms = int((_t.time() - t0) * 1000)
        except Exception:
            reachable = False

        advice = ("能连上，可以直接再投一次。" if reachable else
                  "先把代理软件退出再试；还不行就点「反馈给作者」。")
        data = {"ok": True, "proxy": proxy, "proxy_ok": proxy_ok,
                "reachable": reachable, "ms": ms, "advice": advice,
                "at": _t.time()}
        try:
            p = self.cfg.data_dir / "state" / "net_check.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
            job.last_action = {"do": "net_check", "ok": True,
                               "at": data["at"], "note": ""}
            self.store.save(job)
        except Exception:
            pass
        return data

    def _save_images(self, job_id: int, files: list[str]) -> dict:
        """批量存到「半瓢\\data\\output」：不弹资源管理器，只提示一次。

        2026-11-04 改：原来是 `~/Downloads`。用户的诉求是"属于半瓢的东西
        都待在半瓢文件夹里"，卸载才收得干净；而且 Downloads 里混着一堆
        半瓢-R12-xxx.png，找图反而更难。
        """
        import re
        import shutil
        from pathlib import Path as _P

        base = _P(self.cfg.data_dir) / "output"
        base.mkdir(parents=True, exist_ok=True)
        job = self.store.load(job_id)
        if not job:
            return {"ok": False, "why": "没有 R%d" % job_id}

        brief = re.sub(r'[\\/:*?"<>|]', "", job.display_brief or "").strip()[:24]
        ok, failed = 0, []
        for f in files or []:
            src = self.file_path(job_id, f)
            if not src:
                failed.append(f)
                continue
            stem = "半瓢-R%d%s" % (job_id, ("-" + brief) if brief else "")
            dst = base / ("%s-%s" % (stem, f))
            n = 2
            while dst.exists():
                dst = base / ("%s-%s-%d%s" % (stem, _P(f).stem, n, _P(f).suffix))
                n += 1
            try:
                shutil.copy2(src, dst)
                ok += 1
            except OSError as e:
                failed.append("%s（%s）" % (f, e.strerror or e))
        return {"ok": True, "saved": ok, "failed": failed, "dir": str(base)}


def make_handler(panel: Panel):
    class Handler(BaseHTTPRequestHandler):
        server_version = "bampiao/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # 安静：不要把每条请求打到控制台
            pass

        # ---- 工具
        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
                  extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, obj):
            self._send(code, _json_bytes(obj))

        def _err(self, code: int, why: str):
            self._json(code, {"ok": False, "why": why})

        def do_GET(self):
            u = urlparse(self.path)
            p = unquote(u.path)
            q = parse_qs(u.query)

            if p in ("/", "/index.html"):
                f = STATIC / "index.html"
                if not f.exists():
                    return self._send(200, "<h1>半瓢</h1><p>面板还没就位。</p>".encode("utf-8"),
                                      "text/html; charset=utf-8")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

            if p.startswith("/static/"):
                f = (STATIC / p[len("/static/"):]).resolve()
                if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
                    return self._err(404, "没有这个文件")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript",):
                    ctype += "; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)

            if p == "/api/overview":
                return self._json(200, panel.overview())

            if p == "/api/skill":
                # 面板首屏要显示"贴给你 Agent 的那段"，内容必须和命令行一致：
                # 都从同一个 skills/ 目录读，绝不在前端再抄一份（抄了必然对不上）。
                which = (q.get("which") or ["paste"])[0]
                try:
                    return self._json(200, panel.skill(which))
                except Exception as e:
                    return self._json(200, {"ok": False, "why": str(e)})

            if p == "/api/browser_icon":
                # 加账号时选浏览器用的图标：直接拿本机浏览器安装目录里自带的 Logo.png，
                # 不自己画、不打包别人的商标图。找不到（如 macOS）就 404，前端退回文字。
                from .. import browsers as _brs
                exe = _brs.find((q.get("b") or [""])[0])
                hit = sorted(exe.parent.glob("*/VisualElements/Logo.png")) if exe else []
                if not hit:
                    return self._json(404, {"ok": False})
                data = hit[-1].read_bytes()
                try:
                    # 自带的 Logo.png 四周留白很大，裁到图形本身，不然放在按钮里只有指甲盖大
                    import io

                    from PIL import Image
                    im = Image.open(io.BytesIO(data)).convert("RGBA")
                    box = im.getchannel("A").getbbox()
                    if box:
                        buf = io.BytesIO()
                        im.crop(box).save(buf, "PNG")
                        data = buf.getvalue()
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return

            if p == "/api/stats":
                try:
                    days = int((q.get("days") or ["14"])[0])
                except ValueError:
                    days = 14
                kind = (q.get("kind") or ["all"])[0]
                return self._json(200, {"ok": True, "kind": kind,
                                        **panel.store.stats(days, kind=kind)})

            if p == "/api/chats":
                try:
                    return self._json(200, panel.chats(q))
                except Exception as e:      # noqa: BLE001 - 面板不能因此 500
                    return self._json(200, {"ok": False, "why": str(e)[:200]})

            if p == "/api/jobs":
                try:
                    return self._json(200, panel.jobs(q))
                except ValueError:
                    return self._err(400, "limit / offset 不是数字")

            if p == "/api/attention":
                try:
                    limit = int((q.get("limit") or ["3"])[0])
                except ValueError:
                    limit = 3
                kind = (q.get("kind") or ["all"])[0]
                data = dict(panel._attention_cached())
                rows = list(data.get("jobs", []))
                if kind not in ("", "all"):
                    rows = [r for r in rows if (r.get("kind") or "image") == kind]
                    data["count"] = (data.get("by_kind") or {}).get(kind, 0)
                    # by_class 也要按类型重算，否则别的客户端拿到的分类数是混的
                    bc: dict[str, int] = {}
                    for r in rows:
                        bc[r.get("cls") or "other"] = \
                            bc.get(r.get("cls") or "other", 0) + (r.get("failed_count") or 1)
                    data["by_class"] = bc
                data["jobs"] = rows[:max(1, min(10, limit))]
                data["total_jobs"] = len(rows)
                return self._json(200, data)

            if p.startswith("/api/job/"):
                try:
                    jid = int(p.rsplit("/", 1)[1])
                except ValueError:
                    return self._err(400, "轮次号不是数字")
                d = panel.job_detail(jid)
                return self._json(200 if d.get("ok") else 404, d)

            if p.startswith("/f/"):
                parts = p.split("/", 3)
                if len(parts) < 4:
                    return self._err(400, "路径不对")
                try:
                    jid = int(parts[2])
                except ValueError:
                    return self._err(400, "轮次号不是数字")
                try:
                    want_w = int((q.get("w") or ["0"])[0])
                except ValueError:
                    want_w = 0
                if want_w:
                    thumb = panel.thumbnail(jid, parts[3], max(24, min(320, want_w)))
                    if thumb:
                        return self._send(200, thumb.read_bytes(), "image/jpeg",
                                          extra={"Cache-Control": "public, max-age=604800, immutable"})
                f = panel.file_path(jid, parts[3])
                if not f:
                    return self._err(404, "没有这张图")
                ctype = mimetypes.guess_type(f.name)[0] or "image/png"
                # 0.2.51 及以前把 GIF 存成 .png：按扩展名发 image/png 会骗到
                # 下载器和上传平台，这里按真身纠正（2026-11-06 评审）。
                if ctype in ("image/png", "image/jpeg"):
                    _info = panel.media_info(f)
                    if (_info.get("format") or "").upper() == "GIF":
                        ctype = "image/gif"
                # SVG 是 XML + 活跃内容：直接在浏览器打开时可能带 <script>/onload。
                # 给它一个 sandbox CSP，动效（SMIL/CSS）照跑，脚本一律不执行。
                # <img> 标签里本来就不执行脚本，这条是给"新标签打开"兜底。
                _extra = {}
                if f.suffix.lower() == ".svg":
                    ctype = "image/svg+xml; charset=utf-8"
                    _extra = {"Content-Security-Policy":
                              "sandbox; default-src 'none'; style-src 'unsafe-inline'",
                              "X-Content-Type-Options": "nosniff"}
                return self._send(200, f.read_bytes(), ctype, extra=_extra or None)

            return self._err(404, "没有这个地址")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/action":
                return self._err(404, "没有这个地址")
            # 面板只服务本机页面，但浏览器对"简单请求"不发预检：任意网页都能
            # POST 到 127.0.0.1 触发 装更新 / 删账号 / 退出。必须自己验来源。
            # （0.2.0 新增了 update_install，这个口子从"恶作剧"升级成"能装东西"）
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._err(415, "只接受 application/json")
            origin = (self.headers.get("Origin") or "").strip()
            if origin and not (origin.startswith("http://127.0.0.1:")
                               or origin.startswith("http://localhost:")):
                return self._err(403, "跨站请求被拒绝")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, OSError):
                return self._err(400, "请求体不是 JSON")
            try:
                d = panel.action(body)
            except Exception as e:
                return self._err(500, "%s: %s" % (type(e).__name__, e))
            return self._json(200 if d.get("ok") else 409, d)

    return Handler


def serve(cfg: cfg_mod.Config, port: int | None = None, *,
          block: bool = True):
    """起面板。block=False 时返回 (httpd, 实际端口)，由调用方负责 shutdown。"""
    if port is None:
        port = int(cfg.ui_port or DEFAULT_PORT)
    port = int(port)
    panel = Panel(cfg)
    panel.start_background()
    httpd = _LocalHTTPServer(("127.0.0.1", port), make_handler(panel))
    actual = httpd.server_address[1]
    # 记下真实端口：端口可能是 0（让系统挑），不写下来外面就找不到面板在哪。
    try:
        st = Path(cfg.data_dir) / "state"
        st.mkdir(parents=True, exist_ok=True)
        (st / "runtime.json").write_text(json.dumps({
            "panel": {"port": actual, "url": "http://127.0.0.1:%d/" % actual,
                      "pid": os.getpid(), "started_at": __import__("time").time(),
                      # 桌面壳会填这个：第二次双击时用它把已有窗口叫到前面，
                      # 不再另开一个浏览器标签页（那也是"网页版"存在的唯一理由）。
                      "show_port": 0},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="panel").start()
    return httpd, actual
