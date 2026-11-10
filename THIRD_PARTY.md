# 第三方许可

半瓢本身是 MIT（见 [LICENSE](LICENSE)）。它依赖下面这些开源项目，全部是宽松许可，
商用和再分发都没问题。发行时请保留本文件。

## 运行时依赖

| 项目 | 用途 | 许可 |
|---|---|---|
| [pywebview](https://pywebview.flowrl.com/) | 桌面壳的窗口（Windows 上走 WebView2） | BSD-3-Clause |
| [pythonnet](https://pythonnet.github.io/) | pywebview 在 Windows 上的 .NET 桥 | MIT |
| [pystray](https://github.com/moses-palmer/pystray) | 系统托盘 | LGPL-3.0 |
| [Pillow](https://python-pillow.org/) | 生成托盘图标、读图尺寸 | MIT-CMU |
| [websockets](https://github.com/python-websockets/websockets) | CDP 通信 | BSD-3-Clause |
| [Playwright (Python)](https://playwright.dev/python/) | 驱动浏览器、读写 cookie | Apache-2.0 |
| [greenlet](https://greenlet.readthedocs.io/) / [pyee](https://github.com/jfhbrook/pyee) | Playwright 的依赖 | MIT |
| [NumPy](https://numpy.org/) | 图片相似度 / alpha 检测 | BSD-3-Clause |
| [cryptography](https://cryptography.io/) | 备份文件加密（AES-GCM + scrypt） | Apache-2.0 / BSD-3-Clause |

## 关于 pystray 的 LGPL

pystray 是 LGPL-3.0。半瓢**只是 import 它**、没有修改它，也没有把它静态链接进自己的代码，
属于 LGPL 允许的动态使用方式。如果你要分发**修改过的 pystray 本身**，那部分必须同样以 LGPL 提供。

## 关于 WebView2

Windows 上的窗口由系统的 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
渲染。它不是本项目的依赖，也不随本项目分发——Win10 1903+ 和 Win11 通常已经预装。
`bampiao doctor` 会提示它缺失的情况。

## 关于浏览器

半瓢**不打包、不分发** Chrome 或 Edge，只按标准路径调用用户自己安装的浏览器。
浏览器本身受各自的许可约束。

## 开发依赖（不随发行版分发）

pytest（MIT）。
