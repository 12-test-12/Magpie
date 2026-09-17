"""Magpie · 喜鹊 —— 自用的资源统筹工具（SRC / 渗透测试场景的资产与情报整合工作台）。

仅用于自有资产与已获授权的目标。**工具本身不主动发起任何网络请求**（红线 R7）。

包边界（见 `Magpie-开发实施计划.md` §2）:
    magpie.core   数据层。**只用标准库**（红线 R6），不含任何第三方依赖。
    magpie.cli    命令行入口（`console_scripts: magpie = magpie.cli:main`）。
    magpie.api    可选的本地 API（依赖走 extras.api / extras.api-yaml）。

这里刻意**不** eager import `core`：`import magpie` 应当廉价且无副作用，
以便 `magpie --help` 之类的入口不受数据层导入成本拖累。
"""

from __future__ import annotations

__version__ = "0.0.0"

__all__ = ["__version__"]
