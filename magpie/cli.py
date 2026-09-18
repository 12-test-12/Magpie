"""Magpie 命令行入口 —— 2.1 只立调用点，命令实现在 2.2 / 2.5 / 2.6 / 2.7。

这里刻意**几乎什么都不做**。2.1 需要的是一个稳定的 `console_scripts` 调用点
（`pyproject.toml` 的 `magpie = magpie.cli:main`），而 2.5 之前没有任何命令是
完整可用的。所以骨架只负责三件事：

  * `magpie --help` 能用 —— 打包成不成功，看它就行；
  * 八个命令名先在这里登记，免得 2.2 各写各的；
  * 未实现的命令**明确失败**（非零退出码 + 可读提示），绝不静默成功。
    静默成功是命令行工具最难查的一类 bug：脚本里 `magpie ls` 返回 0 却没有
    输出，调用方会以为"这里就是空的"。

**2.7 加进来的只有 `app` 一条**（`magpie app` 起本地服务）。它不碰 2.2 / 2.6 的
地盘：只读骨架与写路径那两个命令仍然走下面"尚未实现"的分支，等各自的步数落地。
先加 `app` 是有代价的 —— CLI 因此比原计划早一步知道 HTTP 的存在；但它换来的
是 2.7 的性能契约（`magpie app` 的 RSS 与冷启动）有一个**真的可跑的入口**，
而不是只能在测试里拼出来。

用 `argparse`（标准库）而不是 click / typer：core 零依赖是硬约束，CLI 也受益；
`--json` 信封与退出码表本来就要手写，框架省下的正是最少的那部分。

⚠️ `magpie app` 会 import fastapi（extras.api）。它**故意放在分支里**而不是
模块顶部：`magpie --help` 在没有装 extras 的环境里也必须是好的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 与 pyproject.toml 的 version 保持一致。
from . import __version__

# 计划书里的八个核心命令，先登记名字。实现在 2.2（只读骨架）与 2.5 / 2.6（写路径）。
COMMANDS = ("init", "import", "ls", "show", "search", "diff", "undo", "doctor")

# 2.7 起可用的命令（实现不在本文件里，见 magpie/api/）。
IMPLEMENTED = ("app",)

# 未实现的退出码。**临时值** —— 正式的退出码表在 2.2 落地，届时以那张表为准。
EXIT_NOT_IMPLEMENTED = 1
EXIT_USAGE = 2

#: `magpie app` 的缺省数据区 —— 与 `core.pipeline` 的 `data_dir` 缺省值一致。
DEFAULT_DATA_DIR = ".magpie"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="magpie",
        description="自用的资源统筹工具。仅用于自有资产与已获授权的目标；"
                    "工具本身不主动发起任何网络请求。",
    )
    parser.add_argument("--version", action="version", version=f"magpie {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    app = subparsers.add_parser("app", help="起本地服务（GUI + HTTP API 同一份数据）")
    app.add_argument("--db", help=f"数据库路径（默认 <data-dir>/magpie.db）")
    app.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                     help=f"数据区（归档 / 报告 / 库；默认 {DEFAULT_DATA_DIR}）")
    app.add_argument("--host", default="127.0.0.1",
                     help="只接受回环地址（默认 127.0.0.1；绑 0.0.0.0 会被拒绝）")
    app.add_argument("--port", type=int, default=0,
                     help="端口，0 = 随机（默认；随机端口是安全设计的一部分）")
    app.add_argument("--token", help="固定 token（默认启动时随机生成一次性 token）")
    app.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    app.add_argument("--log-level", default="warning",
                     choices=["critical", "error", "warning", "info", "debug", "trace"],
                     help="uvicorn 日志级别（默认 warning）")

    for name in COMMANDS:
        subparsers.add_parser(name, help="尚未实现（2.2 / 2.6 起逐个落地）")

    # 帮助尾巴上写清"哪些真的能用"。一个只有 `--help` 能用的 CLI，最容易被
    # 误当成"命令坏了"；把已实现的那一条明写出来，省掉一次这种误会。
    parser.epilog = ("已实现：" + "、".join(IMPLEMENTED)
                     + "。其余命令见 Magpie-开发实施计划.md 的 2.2 / 2.6。")
    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "app":
        from .api import available
        if not available():
            print(
                "magpie app 需要 extras.api（FastAPI / uvicorn），当前解释器里没有。\n"
                "  py -3.13 -m pip install -e \".[api]\"\n"
                "core 不受影响：数据层只用标准库（红线 R6）。",
                file=sys.stderr,
            )
            return EXIT_NOT_IMPLEMENTED
        from .api.app import serve

        data_dir = Path(args.data_dir)
        db_path = Path(args.db) if args.db else data_dir / "magpie.db"
        return serve(db_path=db_path, data_dir=data_dir, host=args.host,
                     port=args.port, token=args.token,
                     open_browser=not args.no_browser, log_level=args.log_level)

    print(
        f"magpie {args.command}: 尚未实现。"
        f"命令实现见 Magpie-开发实施计划.md 的 2.2（只读骨架）与 2.6（写路径）。",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
