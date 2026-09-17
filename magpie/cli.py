"""Magpie 命令行入口 —— 2.1 只立调用点，命令实现在 2.2 / 2.5。

这里刻意**什么都不做**。2.1 需要的是一个稳定的 `console_scripts` 调用点
（`pyproject.toml` 的 `magpie = magpie.cli:main`），而 2.5 之前没有任何命令是
完整可用的。所以骨架只负责三件事：

  * `magpie --help` 能用 —— 打包成不成功，看它就行；
  * 八个命令名先在这里登记，免得 2.2 各写各的；
  * 未实现的命令**明确失败**（非零退出码 + 可读提示），绝不静默成功。
    静默成功是命令行工具最难查的一类 bug：脚本里 `magpie ls` 返回 0 却没有
    输出，调用方会以为"这里就是空的"。

用 `argparse`（标准库）而不是 click / typer：core 零依赖是硬约束，CLI 也受益；
`--json` 信封与退出码表本来就要手写，框架省下的正是最少的那部分。详见决策表。
"""

from __future__ import annotations

import argparse
import sys

# 与 pyproject.toml 的 version 保持一致。
from . import __version__

# 计划书里的八个核心命令，先登记名字。实现在 2.2（只读骨架）与 2.5（最小触发入口）。
COMMANDS = ("init", "import", "ls", "show", "search", "diff", "undo", "doctor")

# 未实现的退出码。**临时值** —— 正式的退出码表在 2.2 落地，届时以那张表为准。
EXIT_NOT_IMPLEMENTED = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="magpie",
        description="自用的资源统筹工具。仅用于自有资产与已获授权的目标；"
                    "工具本身不主动发起任何网络请求。",
    )
    parser.add_argument("--version", action="version", version=f"magpie {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")
    for name in COMMANDS:
        subparsers.add_parser(name, help="尚未实现（2.2 起逐个落地）")
    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    print(
        f"magpie {args.command}: 尚未实现。"
        f"2.1 只立调用点，命令实现见 Magpie-开发实施计划.md 的 2.2 / 2.5。",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
