"""macOS 的菜单栏图标（NSStatusItem），接口模仿 pystray.Icon。

**为什么不用 pystray**：它的 darwin 后端会自己 `AppHelper.runEventLoop()`，
和 pywebview 抢同一个 NSApplication——实测托盘先起，`webview.start()` 立刻返回、
窗口一闪都没有。

**为什么不能"构造即创建"**：`NSStatusBar.systemStatusBar()` 要求进程已经连上窗口服务器
（WindowServer）。在没有图形会话时（ssh、launchd 后台任务）它会直接
`Assertion failed: CGSConnectionByID` **abort 掉进程**——这不是 Python 异常，catch 不住。
所以这里分成两步：

    tray = MacTray(...)        # 只记参数，不碰 AppKit
    ...webview 起来之后...
    AppHelper.callAfter(tray.build)   # 回到主线程再建

`build()` 里还会确认 `AppHelper.running()`，事件循环没跑就什么都不做。

菜单会被后台轮询线程每 5 秒重刷一次，所以 `menu` 的 setter 必须把 AppKit 操作
丢回主线程（`_on_main`），否则就是在后台线程动 NSMenu。
"""

from __future__ import annotations

import os

_SEP_TEXT = "- - -"


def _on_main(fn) -> None:
    """把它丢到主线程执行；已经在主线程就直接跑。

    **拿不到主线程就放弃**，绝不退回"就地执行"——那等于在后台线程改 NSMenu，
    随机崩。轮询线程每 5 秒会来一次，漏刷一次菜单无所谓，崩一次很要命。
    """
    try:
        from Foundation import NSThread
        if NSThread.isMainThread():
            fn()
            return
    except Exception:
        return
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn)
    except Exception:
        return


class MacTray:
    """菜单栏图标。构造不创建任何 AppKit 对象，`build()` 才真正出现。"""

    def __init__(self, name: str, image=None, title: str = "", menu=None):
        self._name = name
        self._title = title
        self._menu_obj = menu
        self._item = None
        self._target = None
        self.visible = False
        self._icon_path = getattr(image, "filename", None) if image is not None else None

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        """在主线程、且事件循环已经在跑的时候调用。"""
        if self._item is not None:
            return
        # 注意：**不能用 AppHelper.running()**——pyobjc 12 里这个方法已经没了，
        # 调用会直接 AttributeError（实测把整只托盘吞掉了）。用 NSApp.isRunning()。
        try:
            from AppKit import NSApplication
            app = NSApplication.sharedApplication()
            if app is None or not app.isRunning():
                return
        except Exception:
            return
        try:
            from AppKit import (NSImage, NSStatusBar, NSVariableStatusItemLength)
            from Foundation import NSObject

            class _Target(NSObject):
                """菜单项的目标对象。动作都在主线程回调。"""

                def open_(self, sender):
                    cb = getattr(self, "_h_open", None)
                    if cb:
                        cb()

                def pause_(self, sender):
                    cb = getattr(self, "_h_pause", None)
                    if cb:
                        cb()

                def quit_(self, sender):
                    cb = getattr(self, "_h_quit", None)
                    if cb:
                        cb()

            self._target = _Target.alloc().init()
            bar = NSStatusBar.systemStatusBar()
            item = bar.statusItemWithLength_(NSVariableStatusItemLength)
            if item is None:
                return
            self._item = item

            img = None
            for cand in (self._icon_path, _default_icon()):
                if cand and os.path.exists(cand):
                    img = NSImage.alloc().initWithContentsOfFile_(cand)
                    if img is not None:
                        img.setSize_((18.0, 18.0))
                        break
            if img is not None:
                item.button().setImage_(img)
            else:
                item.button().setTitle_("半瓢")
            item.setToolTip_(self._title or "半瓢")
            self._rebuild_menu()
            self.visible = True
        except Exception:
            self._item = None
            self.visible = False

    def _rebuild_menu(self) -> None:
        # 取成局部变量：退出瞬间 stop() 会把 self._item 置 None，
        # 而这段可能还在主线程队列里排着，末尾那两行就会撞 None。
        item = self._item
        if item is None:
            return
        try:
            from AppKit import NSMenu, NSMenuItem
        except Exception:
            return

        items = getattr(self._menu_obj, "items", None) or []
        menu = NSMenu.alloc().init()
        n = 0
        for it in items:
            text = getattr(it, "text", "") or ""
            if text == _SEP_TEXT:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            action = getattr(it, "action", None)
            enabled = bool(getattr(it, "enabled", True))

            sel = None
            if enabled and action is not None:
                if "打开" in text:
                    sel, slot = "open:", "_h_open"
                elif "暂停" in text or "恢复" in text:
                    sel, slot = "pause:", "_h_pause"
                elif "退出" in text:
                    sel, slot = "quit:", "_h_quit"
            if sel is None:
                # 纯展示行（账号状态、队列数字）
                mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
                mi.setEnabled_(False)
            else:
                mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, sel, "")
                mi.setTarget_(self._target)
                setattr(self._target, slot, action)
            menu.addItem_(mi)
            n += 1
        if n == 0:
            menu.addItem_(NSMenuItem.separatorItem())
        item.setMenu_(menu)

    # ---------------------------------------------------------- pystray 兼容
    @property
    def menu(self):
        return self._menu_obj

    @menu.setter
    def menu(self, value):
        self._menu_obj = value
        if self._item is not None:
            _on_main(self._rebuild_menu)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value or ""

        def apply():
            try:
                if self._item is not None:
                    self._item.setToolTip_(self._title)
            except Exception:
                pass

        _on_main(apply)

    def update_menu(self):
        """pystray 的接口。它只要求"把菜单刷一遍"，我们设 menu 时已经刷过了。"""
        if self._item is not None:
            _on_main(self._rebuild_menu)

    def run(self):
        """空操作：图标由 build() 建，事件循环由 pywebview 跑。"""
        return None

    def stop(self):
        item, self._item = self._item, None
        if item is None:
            self.visible = False
            return

        def apply():
            try:
                from AppKit import NSStatusBar
                NSStatusBar.systemStatusBar().removeStatusItem_(item)
            except Exception:
                pass

        _on_main(apply)
        self.visible = False


def _default_icon() -> str | None:
    """随包的那张图（冻结后 _MEIPASS 下也是这个相对路径）。"""
    try:
        from .desktop import INDEX
        return str(INDEX.parent / "icon.png")
    except Exception:
        return None
