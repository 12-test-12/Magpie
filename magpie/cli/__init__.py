"""Magpie 命令行入口（2.2 只读骨架 + 2.7 的 `app`）。

这个包负责三件事，且**只负责这三件**：把参数解析出来、把命令跑起来、
把结果套进信封发出去。每个命令的具体行为在 `read.py`；信封与退出码表在
`envelope.py`；"库能不能读"在 `db.py`。分开的理由很实际：契约（`envelope`）
比命令活得久，把它和某个命令的实现混在一个文件里，改命令时特别容易顺手改到它。

三条纪律
--------

1. **`argparse`（标准库），不引 `click` / `typer`**（拍板项 3，与文档 §3 · 2.2
   一致）。零依赖是 core 的硬约束，CLI 也受益；而 `--json` 信封与退出码表本来
   就要手写 —— 框架省下的正是最少的那部分。

2. **非交互**：任何路径都不读 stdin、不弹提示。需要人工裁决时返回**结构化错误
   + 退出码 3**（`E_AMBIGUOUS` 的 `candidates`），而不是问一句"你要哪个"。
   `argparse` 本身不读 stdin，所以这条纪律的实际工作量落在"把每一个需要人决定的
   岔路都变成错误码"上 —— 见 `read._resolve_ref`。

3. **`--json` 是默认**，`--human` 才切表格。理由是消费者：这条命令的主要用户是
   脚本与 agent，人读输出是**例外**。反过来设计（默认表格、`--json` 才结构化）
   会让每一个调用方都必须记得加那一个 flag，而漏掉的那次不会报错 —— 它只会
   返回一段人话，然后在 `json.load` 里以一种很难查的方式炸掉。

`app` 命令（2.7）留在同一个入口里：GUI 与 CLI 是**同一份数据的两个入口**，
不是两套工具。它 import fastapi（extras.api），**故意放在分支里**而不是模块顶部 ——
`magpie --help` 在没有装 extras 的环境里也必须是好的。

⚠️ **刻意不兜底未建模的异常**。一个没被 `CliError` / `sqlite3.Error` 建模的异常
是**程序缺陷**，不是一个可消费的答案：把它包装成信封，调用方就会按一个假的
`error.code` 继续往下走。这类异常照旧抛出去（连同 traceback）—— 响亮地坏掉，
比安静地给出一个错的答案好。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from .db import DEFAULT_DATA_DIR, Database, resolve_db
from .envelope import (
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    emit_error,
    emit_ok,
    usage_error,
)
from .read import RUNNERS

__all__ = ["build_parser", "main"]

#: 2.6 才会落地的写命令。**先登记名字**：`magpie undo` 报"尚未实现、属于 2.6"
#: 比 argparse 报"invalid choice: 'undo'"有用得多 —— 后者会让人以为这个命令
#: 不存在，而事实是它还没做。
PENDING_WRITE_COMMANDS = ("import", "undo", "redo", "jobs")

#: 未实现命令的退出码。见 `envelope` 里关于 `E_NOT_IMPLEMENTED` 的裁定。
EXIT_NOT_IMPLEMENTED = EXIT_USAGE


class _Parse(argparse.ArgumentParser):
    """把 argparse 的"打印 + 退出"改成"抛一个模塑过的错误"。

    默认行为的两个问题，都发生在**契约**上而不是体验上:

      * `error()` 直接 `sys.exit(2)` —— 于是"用法错"的退出码是 2，而 2 在这张
        冻结的表里是**数据错**。同一个数字在两个地方指两件事，是 agent 最难查的
        一类歧义。
      * 错误走的是 `print(..., file=sys.stderr)`，与 `--json` 的消费者无关；
        调用方拿到的是一段英文用法提示，而不是 `{"ok":false,"error":{...}}`。

    改成抛 `CliError` 之后，用法错与数据错在**同一个信封**里，退出码表也不再被
    argparse 的默认值偷偷改写。`--help` / `--version` 仍然是 argparse 自己的
    `exit(0)`，照原样放行（它们不是错误）。
    """

    def error(self, message: str):                            # noqa: D102
        raise usage_error(message, hint=self.format_usage().strip())

    def exit(self, status: int = 0, message: str | None = None):   # noqa: D102
        if message:
            stream = sys.stdout if status == 0 else sys.stderr
            stream.write(message)
        raise SystemExit(status)


@dataclass
class Context:
    """一次命令要用到的东西。不叫 `App` —— 它没有生命周期，只有一次调用。"""

    args: argparse.Namespace
    db: Database
    parser: argparse.ArgumentParser

    def conn(self) -> sqlite3.Connection:
        return self.db.connect()


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

#: `--db` / `--data-dir`：全局，**且命令前后都能写**（`magpie --db X ls` 与
#: `magpie ls --db X` 都成立）。用 `SUPPRESS` 当缺省值是这里唯一的关键点：
#: argparse 的子解析器会把**自己的缺省值**写回 namespace，若缺省是 `None`，
#: 写在命令**前面**的值就会被后解析的子解析器用 `None` 覆盖掉 —— 一个只在
#: "把全局参数写在前面"时才出现的静默失效。
_LOCATION = argparse.ArgumentParser(add_help=False)
_LOCATION.add_argument("--db", default=argparse.SUPPRESS,
                       metavar="<文件>", help="数据库文件（默认 <data-dir>/magpie.db）")
_LOCATION.add_argument("--data-dir", default=argparse.SUPPRESS,
                       metavar="<目录>", help=f"数据区（默认 {DEFAULT_DATA_DIR}）")

#: 输出格式。默认 json，见模块文档第 3 条。
_FORMAT = argparse.ArgumentParser(add_help=False)
_FORMAT_GROUP = _FORMAT.add_mutually_exclusive_group()
_FORMAT_GROUP.add_argument("--json", dest="fmt", action="store_const", const="json",
                           default=argparse.SUPPRESS, help="JSON 信封（默认）")
_FORMAT_GROUP.add_argument("--human", dest="fmt", action="store_const", const="human",
                           default=argparse.SUPPRESS, help="人读表格")

#: 项目选择。**不给时只有"库里恰好一个项目"才允许省略** —— 见 `read.resolve_project`。
_PROJECT = argparse.ArgumentParser(add_help=False)
_PROJECT.add_argument("--project", default=None, metavar="<id|名字>",
                      help="项目（库里只有一个时可以省略）")


def _fmt_of(args_or_argv) -> str:
    """从已解析的 namespace（或原始 argv）取输出格式。"""
    if isinstance(args_or_argv, argparse.Namespace):
        return getattr(args_or_argv, "fmt", "json")
    return "human" if "--human" in (args_or_argv or []) else "json"


def build_parser() -> argparse.ArgumentParser:
    parser = _Parse(
        prog="magpie",
        description="自用的资源统筹工具。仅用于自有资产与已获授权的目标；"
                    "工具本身不主动发起任何网络请求。",
        parents=[_LOCATION, _FORMAT],
    )
    parser.add_argument("--version", action="version", version=f"magpie {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # -- 2.7：起本地服务（GUI + HTTP API 共用同一份数据） ---------------------
    app = sub.add_parser("app", parents=[_LOCATION],
                         help="起本地服务（GUI + HTTP API 同一份数据）")
    app.add_argument("--host", default="127.0.0.1",
                     help="只接受回环地址（默认 127.0.0.1；绑 0.0.0.0 会被拒绝）")
    app.add_argument("--port", type=int, default=0,
                     help="端口，0 = 随机（默认；随机端口是安全设计的一部分）")
    app.add_argument("--token", help="固定 token（默认启动时随机生成一次性 token）")
    app.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    app.add_argument("--log-level", default="warning",
                     choices=["critical", "error", "warning", "info", "debug", "trace"],
                     help="uvicorn 日志级别（默认 warning）")

    # -- 2.2：只读命令 --------------------------------------------------------
    init = sub.add_parser(
        "init", parents=[_LOCATION, _FORMAT],
        help="建库 / 升级 schema（只建 schema，不建项目）",
        description="把数据区与 schema 备好。**本步唯一的写命令，而且只写 schema。**")
    init.add_argument("--dry-run", action="store_true",
                      help="只报告会应用哪些迁移，不做任何改动")

    ls = sub.add_parser(
        "ls", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="实体列表（键集游标分页）",
        description="列出项目里的实体。分页用 --cursor（上一页末尾的 id）。")
    ls.add_argument("--kind", help="按 kind 过滤（取值见 `magpie schema`）")
    ls.add_argument("--q", help="按规范 key 的**子串**过滤")
    ls.add_argument("--tag", help="按标签过滤")
    ls.add_argument("--lifecycle", choices=["active", "retired"], help="按生命周期过滤")
    ls.add_argument("--cursor", help="上一页末尾的实体 id")
    ls.add_argument("--limit", type=int, default=50, help="每页条数（默认 50，上限 500）")
    # 这两个在 2.7 里就是"传了就 400"：它们要人工状态与 scope 求值，本步没有。
    # 登记它们**不是为了接受**，是为了让"传了却被忽略"不可能发生。
    ls.add_argument("--review-status", dest="review_status", help=argparse.SUPPRESS)
    ls.add_argument("--in-scope", dest="in_scope", help=argparse.SUPPRESS)

    show = sub.add_parser(
        "show", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="实体详情（观测 / 关系 / 标签 / Inbox / 时间线）",
        description="按实体 id 或**规范 key** 查详情。key 有两个候选时返回退出码 3。")
    show.add_argument("ref", metavar="<id|key>", help="实体 id 或规范 key")

    search = sub.add_parser(
        "search", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="搜索（§8，尚未实现 —— 与 /api/v1/search 一样响亮地拒绝）",
        description="**尚未实现**。搜索 DSL 属于计划书 §8；本步不发明第二套查询语义。")
    search.add_argument("query", metavar="<DSL>", help="查询串")

    diff = sub.add_parser(
        "diff", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="增量 diff：自某个作业起，库里多了什么",
        description="自 --since 起**仍然生效**的变化（被 undo 的作业不算）。")
    diff.add_argument("--since", default="last", metavar="<job_id|ISO|last>",
                      help="基准点：作业 id / ISO 时间 / last（默认）")

    doctor = sub.add_parser(
        "doctor", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="数据体检（复用数据层的判断，不在这里重写）",
        description="integrity_check / orphan_subjects / purge_eligible / "
                    "v_integrity_violations / explain_job_scope。"
                    "数据不健康时**仍然退出 0** —— 健康与否在 data.healthy 里。")

    schema = sub.add_parser(
        "schema", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="一条命令吐出类型 / 关系动词 / 字段 / 查询语法",
        description="agent 的说明书。查询语法**从真的那个 parser 生成** —— "
                    "手写的语法文档会在第三次改命令时过期。")

    # -- 2.6 的写命令：先登记名字，未实现时响亮失败 ---------------------------
    for name in PENDING_WRITE_COMMANDS:
        pending = sub.add_parser(name, parents=[_LOCATION, _FORMAT],
                                 help="尚未实现（属于 2.6 的写路径）",
                                 description="**尚未实现**。它属于 2.6 的写路径；"
                                             "本步（2.2）只交付只读命令。")
        # 收下任意位置参数，只为把错误信息**说对**：`magpie undo 1` 应当得到
        # "undo 尚未实现，属于 2.6"，而不是 argparse 的
        # "unrecognized arguments: 1" —— 后者会让人以为是自己参数写错了。
        pending.add_argument("rest", nargs="*", help=argparse.SUPPRESS)

    parser.epilog = (
        "已实现：init、ls、show、diff、doctor、schema（只读）与 app（本地服务）。"
        "search 与 " + "、".join(PENDING_WRITE_COMMANDS) + " 尚未实现 —— 它们会"
        "以 E_NOT_IMPLEMENTED 响亮失败，而不是返回空结果。"
        "契约与退出码表见 `magpie schema --json`。")
    return parser


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------


def _run_app(args) -> int:
    """`magpie app` —— 起本地服务（2.7）。**逻辑仍在 `magpie.api.app.serve`。**

    这里只做入口该做的事：确认 extras 装了、把 `--db` / `--data-dir` 解析成
    同一条路径（与只读命令用同一个 `resolve_db`，见 `db` 模块文档第 1 条），
    然后把控制权交出去。extras 缺失时**明确失败**而不是抛 ImportError ——
    一个没有栈的提示比一个 20 行的 traceback 有用。
    """
    from ..api import available
    if not available():
        print(
            "magpie app 需要 extras.api（FastAPI / uvicorn），当前解释器里没有。\n"
            "  py -3.13 -m pip install -e \".[api]\"\n"
            "core 不受影响：数据层只用标准库（红线 R6）。",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED

    from ..api.app import serve

    data_dir = Path(getattr(args, "data_dir", None) or DEFAULT_DATA_DIR)
    return serve(db_path=resolve_db(getattr(args, "db", None),
                                    getattr(args, "data_dir", None)),
                 data_dir=data_dir, host=args.host, port=args.port,
                 token=args.token, open_browser=not args.no_browser,
                 log_level=args.log_level)


def main(argv: list | None = None) -> int:
    from .envelope import _force_utf8
    _force_utf8()

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliError as exc:                       # argparse 的用法错也走信封
        emit_error(exc, _fmt_of(argv))
        return exc.exit_code

    fmt = _fmt_of(args)

    if args.command is None:
        parser.print_help()
        return EXIT_OK
    if args.command == "app":
        return _run_app(args)
    if args.command in PENDING_WRITE_COMMANDS:
        from .envelope import not_implemented
        err = not_implemented(f"命令 {args.command}",
                              plan="2.6（CLI 写路径）")
        emit_error(err, fmt)
        return err.exit_code

    runner = RUNNERS[args.command]
    started = time.perf_counter()
    db = Database(resolve_db(getattr(args, "db", None),
                             getattr(args, "data_dir", None)),
                  read_only=args.command != "init")
    ctx = Context(args=args, db=db, parser=parser)
    try:
        data, human = runner(args, ctx)
    except CliError as exc:
        emit_error(exc, fmt)
        return exc.exit_code
    except sqlite3.Error as exc:
        from .db import _map_sqlite
        err = _map_sqlite(exc, db.path)
        emit_error(err, fmt)
        return err.exit_code
    finally:
        db.close()

    meta = {
        "db": db.display,
        "took_ms": int((time.perf_counter() - started) * 1000),
        "schema_version": db.schema_version,
    }
    emit_ok(data, meta, human, fmt)
    return EXIT_OK


def _main() -> int:                                              # pragma: no cover
    return main()


if __name__ == "__main__":                                       # pragma: no cover
    sys.exit(main())
