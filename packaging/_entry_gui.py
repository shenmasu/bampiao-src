"""PyInstaller 用的 GUI 启动器（无控制台窗口）。

打包成 windowed 子系统，双击不会弹黑框；`--selftest` 仍然可用（写 report 文件）。
"""

import sys

from image2_bampiao.desktop import main

if __name__ == "__main__":
    sys.exit(main())
