"""解析器包（2.4）。

**一个格式一个文件**，`base.py` 是契约，其余是各自的格式。

发现机制由 `ingest.load_parsers()` 负责：它 `pkgutil.iter_modules` 扫这个包，
按**鸭子类型**（`tool` / `format_version` / `sniff` / `parse`）认出解析器类。
所以新增一个解析器 = 新增一个文件，**不需要在别处登记**；但也正因为如此，
`__init__.py` 里 import 什么不影响发现结果 —— 这里不 import 具体解析器，
只做包的说明与 `base` 契约的再导出。

本包的两条硬线（由 `check.py lint` 静态守着）：

* `PARSE-RO`   —— 不得 import 写入口（`ops` / `store`）与 `sqlite3`。
  解析器只读文件；写入是 `ingest` 的职责，这是 R1"写入口唯一"成立的前提。
* `PARSE-NORM` —— 不得内联归一化（`.lower()` / `idna` / `ipaddress` / `re`），
  一律调用 `magpie.core.normalize`。

这两条都是 `magpie/core/parsers_bridge.py`（2.5 的过渡实现）**没有**满足的 ——
它是一份为了先跑通管线而生的临时实现，本包落地后它自动退役。
"""

from __future__ import annotations

from . import base                      # noqa: F401  （契约：Raw* / Parser / ParseContext）

__all__ = ["base"]
