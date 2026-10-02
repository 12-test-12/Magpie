"""PyInstaller 的入口脚本（LARI-48）。

打包态与 `magpie` console script 走**同一条路**：argv / 退出码原样交给
`magpie.cli.main`，不在这里加任何逻辑 —— 加了就意味着"打包态的 CLI 行为"
和源码态分叉，而分叉只有等用户拿到包才会被发现。
"""

import sys

from magpie.cli import main

if __name__ == "__main__":
    sys.exit(main())
