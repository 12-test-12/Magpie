"""core 测试的 pytest 选项。目前只有一条：重钉风险评分的回归夹具。

为什么走 pytest 选项而不是一个独立脚本：重钉 golden 必须发生在**测试自己的
断言语境**里（先跑同一条断言、看它红、确认方向、再写），独立脚本会把"重钉"
变成一件与"验证"无关的事 —— 而 4.3 的纪律恰恰要求这两件事长在同一条路上。
"""

from __future__ import annotations


def pytest_addoption(parser):
    parser.addoption(
        "--update-risk-golden", action="store_true", default=False,
        help="重钉 magpie/core/tests/fixtures/risk/expected.json（有意变更后用；"
             "先看红、确认方向、再重钉）")
