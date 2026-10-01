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

from .. import __version__
from ..core import query as core_query
from .db import DEFAULT_DATA_DIR, Database, resolve_data_dir, resolve_db
from .envelope import (
    EXIT_OK,
    EXIT_USAGE,
    CliError,
    emit_error,
    emit_ok,
    usage_error,
)
from .read import RUNNERS, as_cli_error
from .write import WRITE_COMMANDS
from . import write

__all__ = ["build_parser", "main"]

#: 未实现命令的退出码。见 `envelope` 里关于 `E_NOT_IMPLEMENTED` 的裁定。
EXIT_NOT_IMPLEMENTED = EXIT_USAGE

#: 需要**可写连接**的命令。`init` 在 `read.py` 里（它只写 schema），其余在
#: `write.py` 里各自登记（`WRITE_COMMANDS`）。这里合成一个是**分发用**的：
#: `main` 据它决定 `Database(read_only=…)`。
WRITABLE = ("init",) + WRITE_COMMANDS


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
        """本命令该用的连接。**唯一入口**：读命令只读，写命令可写。

        `read_only` 在 `main` 里按**命令名**定（`WRITABLE`），不在这里按命令
        自己的意愿定 —— 一条命令能不能写，是分发层的事实，不该由命令自己声明
        而没有任何人复核。写连接自己也会做版本体检（`Database.connect_write`）。
        """
        return self.db.connect() if self.db.read_only else self.db.connect_write()


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
                    "工具本身不主动发起任何网络请求 —— 唯一例外是默认关闭的 "
                    "`magpie ask`（显式开启才出站，见 magpie ask --help）。",
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
        help="实体列表（键集游标分页）+ §3.5 的具名查询",
        description="列出项目里的实体。分页用 --cursor（上一页末尾的 id）。"
                    "后几个过滤条件是 §3.5 的**具名查询** —— 与 "
                    "`GET /entities?…` 的同名参数一字不差，两边走同一段实现"
                    "（`magpie.core.query`）。")
    ls.add_argument("--kind", help="按 kind 过滤（取值见 `magpie schema`）")
    ls.add_argument("--lens", help="按镜头过滤（取值见 `magpie lens`）")
    ls.add_argument("--q", help="按规范 key 的**子串**过滤")
    ls.add_argument("--tag", help="按标签过滤")
    ls.add_argument("--lifecycle", choices=["active", "retired"], help="按生命周期过滤")
    # -- 具名查询 ------------------------------------------------------------
    ls.add_argument("--product", metavar="<产品[@版本]>",
                    help="谁在跑这个产品（↔ GET /entities?product=）。"
                         "带版本与不带版本是两个实体：`nginx` 与 `nginx@1.24.0` 问的是两件事")
    ls.add_argument("--version", metavar="<版本>",
                    help="产品版本（与 --product 一起用；也可以直接写成 nginx@1.24.0）")
    ls.add_argument("--endpoint", metavar="<URL 或规范 key>",
                    help="这个接口的归属服务（↔ GET /entities?endpoint=）。"
                         "身份含主机，所以裸路径不收 —— 给 URL 或折叠好的 key")
    ls.add_argument("--severity", help="按 severity 观测筛（vuln 的 severity）")
    ls.add_argument("--state", help="按 state 观测筛（vuln 的 state）")
    ls.add_argument("--in-cidr", dest="in_cidr", metavar="<网段>",
                    help="落在网段里的资产（ip 的地址包含 / cidr 的子网包含；"
                         "**算出来的**判断，不是一条存着的边）")
    ls.add_argument("--cursor", help="上一页末尾的实体 id")
    ls.add_argument("--limit", type=int, default=core_query.DEFAULT_PAGE,
                    help=f"每页条数（默认 {core_query.DEFAULT_PAGE}，"
                         f"上限 {core_query.MAX_PAGE}）")
    # 这两个在 2.7 里就是"传了就 400"：它们要人工状态与 scope 求值，本步没有。
    # 登记它们**不是为了接受**，是为了让"传了却被忽略"不可能发生。
    ls.add_argument("--review-status", dest="review_status", help=argparse.SUPPRESS)
    ls.add_argument("--in-scope", dest="in_scope", help=argparse.SUPPRESS)

    lens = sub.add_parser(
        "lens", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="镜头：这个镜头下每个 kind 的计数（↔ GET /lenses/{lens}）",
        description="GUI 的类型徽标与 CLI 共用同一段实现（§3.5）。不给镜头时"
                    "列出**库里现在能问的**全部镜头 —— 清单来自 "
                    "kind_registry.lens，不是一张写死的表。")
    lens.add_argument("lens", nargs="?", metavar="<镜头>",
                      help="镜头 id（assets / services / api / vuln / …；"
                           "取值见不给参数时的输出）")

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

    risk = sub.add_parser(
        "risk", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="风险评分 + 攻击面概览（纯读侧派生：总分 + 逐因子为什么）",
        description="把库里已有的事实读成「分数 + 为什么」：每个因子带贡献值与"
                    "依据 id，sum(因子) == 总分；攻击面按 host/service/endpoint"
                    " 聚合，与评分共用同一份因子数据。**零出站**（不查 CVE 库 / "
                    "威胁情报 —— 分数保守是如实，不是缺陷）、不加表不落盘。"
                    "输出带 rule_version 与 rules_digest（规则表指纹）。")

    # -- 5.4：出站问答（默认关闭 —— R7 的唯一白名单出站点） --------------------
    ask = sub.add_parser(
        "ask", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="把问题发给配置的 LLM 端点（默认关闭 —— 显式开启才出站）",
        description="本工具**唯一被允许的出站点**（check.py 的 R7 红线把全仓出站"
                    "收敛到这一个文件：magpie/cli/ask.py）。**默认关闭**：未开启"
                    "时不发起任何网络调用，以可读错误（E_ASK_DISABLED，退出码 1）"
                    "收场，说清这是什么、为什么默认关、如何开启。"
                    "开启方式（两个环境变量，缺一不可）：MAGPIE_ASK_ENABLE=1"
                    "（只认 1/true/yes/on，认不出的值按未开启）且 "
                    "MAGPIE_ASK_URL=<http(s)://…>（出站目标）。开启后每次调用"
                    "先在 stderr 打可见横幅（目标 host + 将发送的内容类别 + "
                    "「唯一出站点」），stdout 信封不受污染。载荷出站前一律过 "
                    "detect_secrets 值级掩码（与 export 默认模式同一道防线）——"
                    "问题文本始终发送并掩码；上下文包（5.3 Markdown）只有 "
                    "--entity 显式给出才附带。超时（--timeout，默认 30 秒）/"
                    "非 2xx / DNS 失败 → 可读错误，退出码按冻结的表落 2，"
                    "不给裸 traceback。不进 MCP 工具面（MCP 只读）。")
    ask.add_argument("question", metavar="<问题>",
                     help="要发送的问题文本（出站前必过值级掩码）")
    ask.add_argument("--entity", type=int, metavar="<id>",
                     help="附带该实体的上下文包（5.3 的 Markdown）—— 默认不带，"
                          "只有显式给出才进载荷")
    ask.add_argument("--timeout", type=float, default=30.0, metavar="<秒>",
                     help="请求超时秒数（默认 30；超时 → 退出码 2）")

    schema = sub.add_parser(
        "schema", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="一条命令吐出类型 / 关系动词 / 字段 / 查询语法",
        description="agent 的说明书。查询语法**从真的那个 parser 生成** —— "
                    "手写的语法文档会在第三次改命令时过期。")

    # -- 4.4：时间线 / 导出（打包） --------------------------------------------
    timeline = sub.add_parser(
        "timeline", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="项目时间线（§8.1 时间语义：exact/approximate 排序，unknown 只作下界）",
        description="时间语义**逐字沿用 §8.1**（observed_at 四元组），不新造一套。"
                    "accuracy ∈ {exact, approximate} 的事件按 observed_at 排进"
                    " events；accuracy=unknown 的只作下界，不参与排序。")
    timeline.add_argument("--limit", type=int, default=core_query.DEFAULT_PAGE,
                          help=f"时间线最多返回多少条（默认 {core_query.DEFAULT_PAGE}，"
                               f"上限 {core_query.MAX_PAGE}）")

    exportp = sub.add_parser(
        "export", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="导出一个自洽的包（默认脱敏；--full 才带 stored 归档原文）",
        description="拼一份拿谁都能看懂的包：manifest + README + 实体/时间线/原文索引。"
                    "**默认不含原文正文**（headers/cookie/凭据结构上进不来）；"
                    "`--full` 才额外带 stored 归档正文并在 stderr 给一条显眼告警；"
                    "external 正文两种模式都不带（4.1 红线）。")
    exportp.add_argument("--full", action="store_true",
                         help="包含 stored 归档原文正文（默认剔除；命令行上有可见告警）")
    exportp.add_argument("-o", "--output", default="magpie-export",
                         metavar="<目录>", help="包的输出目录（默认 magpie-export）")

    # -- 2.6：写路径 ----------------------------------------------------------
    project = sub.add_parser(
        "project", parents=[_LOCATION, _FORMAT],
        help="项目（空库的第一件事：没有项目就没有地方导入）",
        description="项目命令。目前只有 create —— 项目的**创建**是写路径的事"
                    "（`init` 刻意不建项目，它只建 schema）。")
    psub = project.add_subparsers(dest="project_command", metavar="<子命令>")
    pcreate = psub.add_parser(
        "create", parents=[_LOCATION, _FORMAT],
        help="建项目（↔ POST /projects；重名返回退出码 3）",
        description="与 `/api/v1/projects` 同义。scope 决定「什么算在范围里」，"
                    "它随每次改动版本化（§5.1）—— 老作业用**当时的** scope 解释。")
    pcreate.add_argument("name", metavar="<名字>", help="项目名（唯一）")
    _scope = pcreate.add_mutually_exclusive_group()
    _scope.add_argument("--include", action="append", metavar="<glob>",
                        help="范围内的 glob，可重复（如 '*.example.com'）")
    _scope.add_argument("--scope", metavar="<JSON>",
                        help='完整 scope，如 \'{"include":["*.x"],"exclude":[]}\'')
    pcreate.add_argument("--exclude", action="append", metavar="<glob>",
                         help="排除的 glob，可重复")

    imp = sub.add_parser(
        "import", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="导入文件 / 目录（↔ POST /imports）",
        description="把真实工具的输出导进库里。每个文件一个作业 —— 于是 undo "
                    "的粒度是文件。解析器由嗅探仲裁选；仲裁不出结果的文件**不猜**，"
                    "而是进 Inbox 待人工确认（退出码 3）。")
    imp.add_argument("paths", nargs="+", metavar="<路径…>",
                     help="文件或目录（目录按扩展名浅过滤，不递归挑 .db / .png）")
    imp.add_argument("--parser", help="强制指定解析器，跳过仲裁（取值见 `magpie schema`）")
    imp.add_argument("--source", help="来源名（默认取解析器的工具名）")
    imp.add_argument("--authority", help="来源可信度上限（默认 unverified）")
    imp.add_argument("--archive", choices=["archived", "external"],
                     help="archived = 复制进数据区（默认）；external = 只记路径 + sha256")
    imp.add_argument("--dry-run", action="store_true",
                     help="干跑：事实一行不写，事务 ROLLBACK（报告与真跑同构）")

    undo = sub.add_parser(
        "undo", parents=[_LOCATION, _FORMAT],
        help="撤销一个作业（↔ POST /jobs/{id}/undo）",
        description="事实层追加 retract 抑制，不删贡献、不改贡献（INV-01 无例外）。"
                    "与 redo 严格互逆。")
    undo.add_argument("job", metavar="<job>", help="作业 id（见 `magpie jobs`）")
    undo.add_argument("--dry-run", action="store_true",
                      help="干跑：输出与真跑同构，只有 COMMIT→ROLLBACK 之差")

    redo = sub.add_parser(
        "redo", parents=[_LOCATION, _FORMAT],
        help="复原一个被撤销的作业（↔ POST /jobs/{id}/redo）",
        description="清掉指向该作业的 retract 抑制与撤销边，`applied` 回到 1。"
                    "没有任何东西可复原时是幂等的（`noop`），不是错误。")
    redo.add_argument("job", metavar="<job>", help="作业 id（见 `magpie jobs`）")
    redo.add_argument("--dry-run", action="store_true",
                      help="干跑：输出与真跑同构，只有 COMMIT→ROLLBACK 之差")

    jobs = sub.add_parser(
        "jobs", parents=[_LOCATION, _FORMAT, _PROJECT],
        help="作业历史（含 explain_job_scope）",
        description="不给 <job> 时列最近的一批（每条都带 explain_job_scope —— "
                    "老作业必须用**当时的** scope 解释）；给了就是单条详情。")
    jobs.add_argument("job", nargs="?", metavar="<job>", help="作业 id（可省略）")
    jobs.add_argument("--state", help=f"按状态过滤（{' / '.join(write.JOB_STATES)}）")
    jobs.add_argument("--applied", metavar="<true|false>",
                      help="只看生效中（true）或已撤销（false）的作业")
    jobs.add_argument("--limit", type=int, default=write.DEFAULT_JOB_LIMIT,
                      help=f"列表最多返回多少条（默认 {write.DEFAULT_JOB_LIMIT}，"
                           f"上限 {write.MAX_JOB_LIMIT}）")

    parser.epilog = (
        "只读：init、ls、lens、show、diff、doctor、schema、timeline、export、risk。"
        "写：project create、import、undo、redo、jobs（jobs 是读的，与作业历史"
        "放在一起便于对照）。本地服务：app。"
        "出站：ask —— 本工具唯一的出站点，**默认关闭**，"
        "MAGPIE_ASK_ENABLE=1 + MAGPIE_ASK_URL=<http(s)://…> 显式开启才发请求。"
        "export 默认脱敏（不带原文正文），--full 才带 stored 归档原文并给可见告警。"
        "search 尚未实现 —— 它以 E_NOT_IMPLEMENTED 响亮失败，而不是返回空结果。"
        "契约与退出码表见 `magpie schema --json`；具名查询的清单也在那里。")
    return parser


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """取一个 parser 的子命令表。找不到就抛 —— 没有子命令的 parser 是程序缺陷。"""
    for action in parser._actions:                                  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):          # noqa: SLF001
            return action
    raise AssertionError(f"{parser.prog} 没有子命令表")               # pragma: no cover


def _runner_key(args) -> str:
    """已解析的 namespace → `RUNNERS` 的键。目前只有 `project` 这一族是**嵌套**的。

    `project create` 在 argparse 里是两层，而 `RUNNERS` 是一层表。把两层折成
    一个键名（`"project create"`）而不是给 `RUNNERS` 加一层嵌套：调用方的
    `runner(args, ctx)` 形状不变，读命令那边一行都不用动。
    """
    if args.command == "project":
        return f"project {args.project_command}"
    return str(args.command)


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

    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
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
    if args.command == "project" and getattr(args, "project_command", None) is None:
        # `magpie project` 单独出现：它不是一个命令，是命令的**父节点**。
        # 打印那一段的用法并退出 1 —— `parser.print_help()` 会打出全局帮助，
        # 让人以为是自己命令写错了。
        _subparsers(parser).choices["project"].print_help()
        return EXIT_USAGE

    runner = RUNNERS[_runner_key(args)]
    started = time.perf_counter()
    db = Database(resolve_db(getattr(args, "db", None),
                             getattr(args, "data_dir", None)),
                  read_only=args.command not in WRITABLE)
    ctx = Context(args=args, db=db, parser=parser)
    try:
        data, human = runner(args, ctx)
    except CliError as exc:
        emit_error(exc, fmt)
        return exc.exit_code
    except core_query.QueryError as exc:
        # 查询层的失败（core 不认识退出码）在这里套进 CLI 的信封。
        # 命令自己捕获并 raise `as_cli_error(exc)` 是为了让 traceback 指向**哪一条
        # 命令**；这里兜住漏网的那些 —— 一条漏网的 QueryError 会变成一个
        # traceback，而"响亮地坏掉"不该等于"给调用方一段栈"。
        err = as_cli_error(exc)
        emit_error(err, fmt)
        return err.exit_code
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
