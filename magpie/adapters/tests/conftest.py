"""LARI-43 的测试夹具 —— 与 CLI 契约测试**共用同一份**，不另搭一份。

理由与 `magpie/cli/tests/conftest.py` 的文件头相同：两套测试各搭一个夹具库，
它们哪天搭得不一样了，"MCP 工具面只读"就只对其中一个夹具成立。这里直接
re-export CLI 那份（`loaded_db` 走 2.5 的最小入口真导入一次）。

`ENV` 也一并再导出 —— 本套件会 import `magpie.cli.tests.test_readonly`（复用
它的 `_fingerprint`），那个模块顶层是 `from conftest import ENV`：跨套件解析
落在本套件的 conftest 上，没有这行再导出它就会 ImportError。
"""

from __future__ import annotations

from magpie.cli.tests.conftest import ENV, empty_db, loaded_db  # noqa: F401
