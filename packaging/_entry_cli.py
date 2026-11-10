"""PyInstaller 用的 CLI 启动器。

不能直接把 image2_bampiao/cli.py 当脚本打包：它用的是包内相对导入
（`from . import ...`），脱离包上下文会 ImportError。
"""

import sys

from image2_bampiao.cli import main

if __name__ == "__main__":
    sys.exit(main())
