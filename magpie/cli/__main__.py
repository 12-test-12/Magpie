"""`py -3.13 -m magpie.cli …` 的入口。

它必须存在，而且必须**只有这一行**：`bench/test_perf_app.py` 与
`bench/test_perf_web.py` 都用 `-m magpie.cli app …` 起真子进程量 RSS 与冷启动，
而那两条契约问的是"用户双击之后会得到什么" —— 它们不该因为我们把 `cli.py`
改成了包就失效。

`-m` 与 console_scripts 走的是同一条 `main()`，所以这里不需要（也不应该）
有任何自己的逻辑：两条入口一有分叉，"测试跑的是 A，用户拿到的是 B"就开始了。
"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
