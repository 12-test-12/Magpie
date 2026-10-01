"""MCP（Model Context Protocol）适配器 —— stdio 传输、只读工具面、零第三方依赖。

它让 agent 经 MCP **只读**地使用这份数据（5.1 / LARI-43）。四条设计裁定：

1. **传输只有 stdio**（本地子进程、换行分帧的 JSON-RPC 2.0）。HTTP / SSE 传输
   是出站的邻居 —— R7 对谁都不豁免，这里连"以后也许要"的口都不开。
   起服务：``py -3.13 -m magpie.adapters.mcp --db <库>``（README 有接线示例）。

2. **协议面手写最小集**（``initialize`` / ``notifications/initialized`` /
   ``ping`` / ``tools/list`` / ``tools/call``），零第三方依赖 —— 本机网络时通时
   断，官方 ``mcp`` SDK 装不上就满足不了 4.5 立下的"缺失与已装两条路径都要验"
   的纪律。协议夹具（``tests/test_protocol.py`` 的黄金请求 / 响应）钉住行为；
   若实现中发现协议面要扩到订阅 / roots 等，**停下来在交付评论报告**，不静默
   引第三方进 core（R6 的精神）。

3. **工具面从 5.2 的 schema 契约派生，不手抄**。清单来自 ``magpie schema
   --json`` 的 ``commands``（只取 ``access == "read"`` 的一半，剔除
   ``EXCLUDED_READ_COMMANDS``），描述来自同一份契约的 ``query_syntax``，参数枚举
   来自 ``enums``，服务版本来自 ``tool_version``；参数的形状（type / nargs /
   choices）从**真的那个 argparse parser** 算 —— 契约的 ``query_syntax`` 与它逐项
   相等，由 5.2 的契约测试钉住。手抄一份工具清单正是 5.2 修掉的头号失败模式：
   下次新增命令 / 改分类，``tests/test_readonly.py`` 的双向清单断言与
   ``McpServer`` 启动时的漂移检查当场红。

4. **读侧复用唯一实现**。命令类工具把参数还原成 argv，走 ``cli/read.py`` 的
   ``RUNNERS`` —— 与 ``magpie`` 命令同一段代码、同一个
   ``Database(read_only=True)`` 连接（``PRAGMA query_only`` 是数据库自己执行的
   只读，不是自觉）。``agent_context`` 没有 CLI 命令，按 ``api/routes.py`` 的
   同款组合调 ``core/query.py``（收敛断言在 ``tests/test_protocol.py``）。
   **本文件不碰 SQL，也不 import 写入口** —— ``tests/test_static.py`` 的 AST
   断言覆盖 ``magpie/adapters/**`` 全部源码。

错误与信封与 CLI / ``/api/v1`` 逐字同形（GUI / CLI / **MCP** 是 ``envelope.py``
点名的三个消费者）：成功带 ``meta``，失败不带；分叉按 ``error.code``。
``tools/call`` 的**工具级**失败进 ``result.isError``，**协议级**失败（未知方法 /
未知工具 / 坏 JSON）走 JSON-RPC ``error`` —— 两层不许混。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..cli import WRITABLE, Context, _runner_key, build_parser
from ..cli.db import Database, map_db_error, resolve_db, resolve_project
from ..cli.envelope import EXIT_USAGE, CliError, dump_json
from ..cli.read import RUNNERS, _command_nodes, as_cli_error, cmd_schema
from ..core import query

# ---------------------------------------------------------------------------
# 协议常量（JSON-RPC 2.0 + MCP 的 initialize / tools 面）
# ---------------------------------------------------------------------------

#: 我们实现的协议版本。客户端请求 ``SUPPORTED_PROTOCOL_VERSIONS`` 之一时照它的
#: 版本回（客户端与服务端按同一份规格说话），否则回我们自己的 —— 这是 MCP
#: initialize 的协商语义：**服务端总是给出一个它真的支持的版本**。
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")

SERVER_NAME = "magpie"

#: initialize 的 ``instructions``。信封怎么读、按什么分叉 —— agent 建立会话的
#: 第一眼就该看到，而不是等第一次报错才发现有 error.code 这回事。
SERVER_INSTRUCTIONS = (
    "Magpie 只读 MCP 适配器。工具面全部只读（导入 / undo / 合并 / 裁决 / 导出等"
    "写操作不在本阶段）。tools/call 的结果是与 CLI --json 与 /api/v1 逐字同形的"
    " JSON 信封：成功 {\"ok\": true, \"data\": …, \"meta\": {db, took_ms,"
    " schema_version}}，失败 {\"ok\": false, \"error\": {code, message, hint?}}"
    "（失败没有 meta）。分叉按 error.code，不要按 message 文本。")

#: JSON-RPC 标准错误码（-32700 … -32603）。
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# 工具面的声明（双向清单校验的"声明"这一半）
# ---------------------------------------------------------------------------

#: **声明的只读工具清单**。工具注册表从 5.2 契约派生（不是从这里生成），两边
#: 必须逐项相等 —— ``tests/test_readonly.py`` 的双向断言（实际 ⊆ 声明 且
#: 声明 ⊆ 实际）与 ``McpServer`` 启动时的漂移检查共同守它：
#:
#:   * 契约里多出一条 read 命令 → 派生清单变大 → **服务起不来、测试红** ——
#:     "工具面悄悄长出一个操作"必须是显式决定，不是顺手；
#:   * 声明里多出一个名字 → 同样红 —— "声明了一堆没实现的"也不许。
#:
#: 写工具（G5 之后的议题）不进这份清单，也不该有人往里加。
READONLY_TOOLS = frozenset({
    "ls",          # 实体列表 / 具名查询（§3.5 的搜索式读取）
    "lens",        # 镜头
    "show",        # 实体详情
    "diff",        # 增量 diff（§8.1 的 --since 语义）
    "doctor",      # 数据体检
    "schema",      # schema 契约（类型 / 关系 / 字段 / 查询语法 / 5.2 四键）
    "timeline",    # 项目时间线（§8.1）
    "jobs",        # 作业历史（分发层事实：读，住在 write.py 不改这个事实）
    "risk",        # 风险评分 + 攻击面概览（5.5 纯读侧派生 ↔ GET /api/v1/risk）
    "agent_context",  # 上下文包（↔ GET /api/v1/agent/context）
})

#: 契约里 ``access == "read"`` 但**明确不进工具面**的命令 → 不进面的理由。
#: 判据是任务书明文：导出落盘不进工具面；search 在 §8 之前永远
#: ``E_NOT_IMPLEMENTED``，一个恒红的工具对 agent 是噪声（响亮失败属于 CLI 契约，
#: 那边有退出码表要守）；ask 是**出站**命令（5.4，R7 唯一白名单出站点）——
#: MCP 只读，出站一律不进工具面。清单被剔除这件事本身也受双向断言保护 ——
#: 少剔一个、多剔一个都会红。
EXCLUDED_READ_COMMANDS: dict[str, str] = {
    "export": "导出落盘（写文件系统）—— 任务书明文不进工具面",
    "search": "计划书 §8 尚未实现（E_NOT_IMPLEMENTED 恒红对 agent 是噪声）；"
              "搜索式读取由 ls 的 --q 与具名查询承担",
    "ask": "出站命令（5.4：唯一出站点，需 MAGPIE_ASK_ENABLE 显式开启）—— "
           "MCP 只读，出站一律不进工具面（5.4 任务书明文）",
}

#: 参数名 → ``enums`` 契约键。argparse 只给 ``--lifecycle`` 设了 choices，``lens``
#: 是动态的（来自 kind_registry）—— 两处都从契约的 ``enums``（真库 CHECK）取，
#: 新 kind 落库后工具描述自动跟着变，不用改这里。
ARG_ENUM_KEYS = {"lens": "lens", "lifecycle": "lifecycle"}

#: 服务级参数：``--db`` / ``--data-dir`` 在起服务时就定了，``--json`` / ``--human``
#: （dest ``fmt``）在这里恒为 JSON —— 都不进工具的 inputSchema。
SERVER_LEVEL_DESTS = frozenset({"db", "data_dir", "fmt"})

#: ``agent_context`` 的名字（它没有 CLI 命令，是唯一一个手声明的工具）。
AGENT_CONTEXT_TOOL = "agent_context"


class _InvalidParams(Exception):
    """协议级参数错（JSON-RPC -32602）—— 与工具级失败（信封 + isError）分层。"""


# ---------------------------------------------------------------------------
# 注册表：从契约 + 真 parser 派生
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Param:
    """一个工具参数怎么还原成 argv（命令类工具的调用路径）。"""

    kind: str            # "option" | "positional"
    prop: str            # inputSchema 里的属性名（= argparse 的 dest）
    flag: str | None     # option 时的长 flag（如 "--limit"）
    is_bool: bool        # store_true 类：只在 true 时给 flag


@dataclass(frozen=True)
class Tool:
    """一个 MCP 工具：对外的三件套 + 对内的调用方式（argv 或 handler）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    command: str | None                          # None → handler 路径
    params: tuple[_Param, ...] = field(default_factory=tuple)
    handler: Callable[[Database, dict[str, Any]], dict[str, Any]] | None = None

    def to_mcp(self) -> dict[str, Any]:
        """``tools/list`` 里的一个条目（MCP 规定的三件套）。"""
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


def _tool_name(command: str) -> str:
    """契约命令键 → MCP 工具名（嵌套命令的空格折成下划线；写命令永远到不了这）。"""
    return command.replace(" ", "_")


def _action_json_type(action: argparse.Action) -> str:
    """argparse 的 ``type=`` → JSON Schema 类型。没给就是 string。"""
    return {"int": "integer", "float": "number"}.get(
        getattr(action.type, "__name__", ""), "string")


def _input_schema(node: argparse.ArgumentParser,
                  enums: dict[str, Any]) -> tuple[dict[str, Any], tuple[_Param, ...]]:
    """子 parser → 工具的 inputSchema + argv 还原表。

    **从真 parser 算**（不抄契约的 ``query_syntax`` 也抄不了：它没有 type 与
    nargs）。契约那边由 ``test_schema_query_syntax_matches_the_real_parser`` 保证
    与 parser 逐项相等，所以两条派生路线殊途同归 —— 而各属性只认一个来源，
    才不会出现"一半从契约来、一半从代码来"的分叉。
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    params: list[_Param] = []
    for action in node._actions:                                   # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):         # noqa: SLF001
            continue
        if isinstance(action, argparse._HelpAction):               # noqa: SLF001
            # `-h/--help` 在终端里对，在协议面里是一条逃逸：argparse 把帮助文本
            # **print 到 stdout（协议流）**然后抛 SystemExit —— 而 SystemExit 是
            # BaseException，`except Exception` 接不住，会逃出 serve() 杀会话
            # （M1 审查实测；`{"help": true}` 是与位置参数 `-h` 同族的第二条路）。
            # 不进 inputSchema，于是它是未知参数 → -32602，连生成 argv 的机会都没有。
            continue
        if action.help == argparse.SUPPRESS:
            # CLI 里 help=SUPPRESS 的参数是"登记着但传了就 400"的（2.7 的
            # --review-status / --in-scope）—— 不给 agent 一个能看见却不该用的口。
            continue
        if action.option_strings:
            if action.dest in SERVER_LEVEL_DESTS:
                continue
            is_bool = action.nargs == 0
            spec: dict[str, Any] = {"type": "boolean" if is_bool
                                    else _action_json_type(action)}
            description = (action.help or "").strip()
            if description:
                spec["description"] = description
            enum_key = ARG_ENUM_KEYS.get(action.dest)
            if enum_key is not None:
                spec["enum"] = list(enums[enum_key])                # ← 契约 enums 键
            elif action.choices:
                spec["enum"] = list(action.choices)
            if not is_bool and action.default not in (None, argparse.SUPPRESS):
                spec["default"] = action.default
            properties[action.dest] = spec
            flag = next((f for f in action.option_strings if f.startswith("--")),
                        action.option_strings[0])
            params.append(_Param("option", action.dest, flag, is_bool))
        else:
            spec = {"type": "string"}
            description = (action.help or "").strip()
            if description:
                spec["description"] = description
            enum_key = ARG_ENUM_KEYS.get(action.dest)
            if enum_key is not None:
                # 位置参数同样吃契约 enums —— `lens` 在 argparse 里没有 choices
                # （它是 kind_registry 里的动态值），只可能从这来。
                spec["enum"] = list(enums[enum_key])
            properties[action.dest] = spec
            if action.nargs is None:            # 位置参数必填；nargs="?" 的可省
                required.append(action.dest)
            params.append(_Param("positional", action.dest, None, False))
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = False
    return schema, tuple(params)


def build_registry(schema_data: dict[str, Any]) -> dict[str, Tool]:
    """契约 ``data`` → 工具注册表。**纯函数**：清单、描述、枚举全从参数算。

    依赖注入 ``schema_data`` 而不是自己开库，是为了让"派生是否真的跟契约走"
    能被测试直接钉住（喂一份改过分类的契约，注册表必须跟着变 —— 手抄的清单
    在那条断言下不会变）。
    """
    nodes = _command_nodes(build_parser())          # 与契约共用同一条展平规则
    syntax = schema_data["query_syntax"]["commands"]
    enums = schema_data["enums"]
    registry: dict[str, Tool] = {}
    for key in sorted(schema_data["commands"]):
        entry = schema_data["commands"][key]
        if entry.get("access") != "read":
            continue                                 # 只取 read 那一半
        if key in EXCLUDED_READ_COMMANDS:
            continue                                 # 明文剔除（理由见常量注释）
        name = _tool_name(key)
        schema, params = _input_schema(nodes[key], enums)
        description = (syntax.get(key) or {}).get("help") \
            or f"magpie {key}（只读）"
        registry[name] = Tool(name=name, description=description,
                              input_schema=schema, command=key, params=params)
    registry[AGENT_CONTEXT_TOOL] = _agent_context_tool()
    return registry


# ---------------------------------------------------------------------------
# agent_context —— 唯一没有 CLI 命令的工具
# ---------------------------------------------------------------------------


def _agent_context_tool() -> Tool:
    """上下文包。**手声明的唯一一个工具**：CLI 没有对应命令，契约里没有它的键。

    参数与 ``GET /api/v1/agent/context`` 逐字同义（entity_id / depth / project）。
    它的存在受双向清单断言约束：不声明就实现不了、声明了就必须实现。
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "entity_id": {"type": "integer",
                          "description": "实体 id（与 GET /api/v1/agent/context 的"
                                         " entity_id 同义）"},
            "depth": {"type": "integer", "default": 1,
                      "description": "向外扩几层邻居（默认 1）"},
            "project": {"type": "string",
                        "description": "项目（库里只有一个时可省略）"},
        },
        "required": ["entity_id"],
        "additionalProperties": False,
    }
    return Tool(name=AGENT_CONTEXT_TOOL,
                description="★ 给 agent 的上下文包：实体详情 + 邻居 + 未决 Inbox + "
                            "这个项目里没测过的实体，JSON 与 Markdown 双形态 —— "
                            "与 GET /api/v1/agent/context 同一份数据",
                input_schema=schema, command=None, handler=_call_agent_context)


def _require_int(value: Any, name: str, *, default: int | None = None) -> int:
    """整数参数校验。用法错走 CLI 的信封（与 argparse 的 type= 同一档）。

    纯数字字符串放行（`"1"` → 1）—— argv 与 HTTP 查询串里整数本来就是字符串，
    两个既有入口都收；bool 是 int 的子类，一并拒掉。
    """
    if value is None and default is not None:
        return default
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CliError("E_USAGE", f"{name} 必须是整数", exit_code=EXIT_USAGE,
                       hint=f'给数字，如 {{"{name}": 1}} —— 不要 {value!r}。')
    return value


def _call_agent_context(db: Database, arguments: dict[str, Any]) -> dict[str, Any]:
    """上下文包的取数。**与 ``api/routes.py:agent_context`` 同一组 query 调用**。

    这里刻意不"顺手改"组合方式：两份组合必须给出同一个 payload，收敛断言在
    ``tests/test_protocol.py::test_agent_context_converges_with_the_api_route``
    （那边改了 / 这边改了，先红的都是它）。读侧唯一实现仍然是 ``core/query.py``
    —— 本函数只做组合，不碰 SQL。
    """
    entity_id = _require_int(arguments.get("entity_id"), "entity_id")
    depth = _require_int(arguments.get("depth", 1), "depth", default=1)
    project = arguments.get("project")
    if project is not None and not isinstance(project, str):
        raise CliError("E_USAGE", "project 必须是字符串", exit_code=EXIT_USAGE,
                       hint="给项目 id 或完整名字；库里只有一个项目时可省略。")
    conn = db.connect()
    project_id = resolve_project(conn, project)
    data = query.entity_detail(conn, project_id, entity_id)
    neighbour_ids = query.neighbour_ids(conn, project_id, entity_id, depth)
    neighbours = query.graph_payload(conn, project_id, lens=None, kind=None, q=None,
                                     limit=max(len(neighbour_ids), 1),
                                     seeds=neighbour_ids)
    open_conflicts = query.open_inbox_count(conn, project_id)
    untested = query.untested_entities(conn, project_id, limit=20)
    payload = {"entity": data["entity"], "observations": data["observations"],
               "relations": data["relations"], "tags": data["tags"],
               "manual": data["manual"], "open_inbox": data["open_inbox"],
               "neighbours": neighbours["nodes"], "edges": neighbours["edges"],
               "project": {"id": project_id, "open_inbox": open_conflicts,
                           "untested_entities": untested}}
    return {"json": payload, "markdown": _context_markdown(payload)}


def _context_markdown(payload: dict[str, Any]) -> str:
    """payload → Markdown。与 ``api/routes.py:_context_markdown`` **逐行同款**。

    两份渲染由收敛断言钉成一件事：那边改了而这边没跟（或反过来），MCP 与
    GUI 给 agent 的"复制给 AI"包就会一个说法两个样 —— 那正是这条断言要防的。
    """
    entity = payload["entity"]
    lines = [f"# {entity.get('entity_key')}（{entity.get('kind')}）", ""]
    lines.append(f"- 实体 id：{entity.get('id')}；生命周期：{entity.get('lifecycle')}")
    if payload["tags"]:
        lines.append(f"- 标签：{', '.join(payload['tags'])}")
    if payload["manual"]["protected_fields"]:
        lines.append(f"- 人工保护字段：{', '.join(payload['manual']['protected_fields'])}")
    lines += ["", "## 观测"]
    if payload["observations"]:
        for obs in payload["observations"]:
            lines.append(f"- {obs['semantic_field']}: {obs['value']}"
                         f"（{obs['confidence']}，{obs['source_count']} 个来源）")
    else:
        lines.append("- （无）—— 这是一个**没测过**的实体")
    lines += ["", "## 关系"]
    if payload["relations"]:
        for rel in payload["relations"]:
            arrow = "→" if rel["direction"] == "out" else "←"
            lines.append(f"- {arrow} {rel['rel']} {rel['other_key']}"
                         f"（{rel['confidence']}）")
    else:
        lines.append("- （无）")
    lines += ["", "## 未决"]
    if payload["open_inbox"]:
        for item in payload["open_inbox"]:
            lines.append(f"- [{item['severity']}] {item['kind']}：{item['payload']}")
    else:
        lines.append("- （无）")
    lines += ["", "## 这个项目里还有什么没测",
              f"- 未决 Inbox 项共 {payload['project']['open_inbox']} 条",
              f"- 一条观测都没有的实体（前 20 个）："]
    for row in payload["project"]["untested_entities"]:
        lines.append(f"  - {row['entity_key']}（{row['kind']}）")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 调用：参数 → argv → 与 CLI 同一段 runner
# ---------------------------------------------------------------------------


def _argv_for(tool: Tool, arguments: dict[str, Any]) -> list[str]:
    """工具实参 → CLI argv（命令类工具的调用路径）。

    未知属性在这一层挡掉（inputSchema 的 ``additionalProperties: false`` 不是
    摆设），标量 / 布尔的形状也在这里验 —— 过了这层之后，argparse 是第二道
    用法错防线（缺必填、坏 --limit 之类走 CLI 的信封，退出码语义原样保留）。
    """
    if tool.command is None:                       # pragma: no cover - handler 路径不走这
        raise _InvalidParams(f"{tool.name} 没有对应的 CLI 命令")
    known = set(tool.input_schema.get("properties", {}))
    unknown = sorted(set(arguments) - known)
    if unknown:
        raise _InvalidParams(
            f"{tool.name}: 未知参数 {', '.join(unknown)} —— inputSchema 的 "
            f"additionalProperties 是 false，清单见 tools/list")
    argv = tool.command.split()
    for param in tool.params:
        if param.prop not in arguments:
            continue
        value = arguments[param.prop]
        if value is None:
            continue                               # JSON null = 没给
        if param.kind == "positional":
            if isinstance(value, (dict, list)):
                raise _InvalidParams(
                    f"{param.prop} 要标量，给了 {type(value).__name__}")
            text = str(value)
            if text.startswith("-"):
                # M1 审查必修：`-` 开头的**位置参数**值会被 argparse 当成 flag，
                # `-h` / `--help` 于是打印帮助文本进协议流并 SystemExit 杀会话。
                # 位置参数是实体 id/key、镜头 id、job id —— 合法值不以 `-` 开头，
                # 在这里挡成协议级参数错（-32602），help 从此没有机会被触发。
                # （option 值以 `-` 开头走另一条路：argparse 报
                # "expected one argument" → CliError → isError 信封，已实测安全。）
                raise _InvalidParams(
                    f"{param.prop} 的值不许以 '-' 开头：{text!r} —— 位置参数是"
                    f"实体 id/key、镜头 id、job id，'-' 开头会被 argparse 当成 "
                    f"flag（-h/--help 会把帮助文本写进协议流）")
            argv.append(text)
            continue
        if param.is_bool:
            if not isinstance(value, bool):
                raise _InvalidParams(f"{param.prop} 要布尔值")
            if value:
                argv.append(param.flag)
            continue
        if isinstance(value, (dict, list)):
            raise _InvalidParams(
                f"{param.prop} 要标量，给了 {type(value).__name__}")
        argv += [param.flag, str(value)]
    return argv


def _tool_result(envelope: dict[str, Any], is_error: bool) -> dict[str, Any]:
    """信封 → MCP 的 tool result。文本是 **纯 ASCII JSON**（ensure_ascii，与
    ``--json`` 同一条可消费性纪律：GBK 控制台上的消费者也读得走）。"""
    return {"content": [{"type": "text", "text": dump_json(envelope)}],
            "isError": is_error}


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id,
            "error": {"code": code, "message": message}}


class McpServer:
    """一次会话的服务端状态：契约快照 + 派生出的工具注册表。

    构造时读一次 ``schema``（这正是 5.2 说的"消费契约输出"），随后注册表在会话
    内不变 —— ``tools/list`` 可以随时问，答案不会中途漂移。库没了 / 版本不匹配
    直接抛 ``CliError``（与 CLI 同一张错误表、同一个退出码语义），服务起不来
    比"起来了但答错"好。
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.schema = self._load_schema()
        self.tool_version = self.schema["tool_version"]      # ← 契约 tool_version 键
        registry = build_registry(self.schema)
        missing = READONLY_TOOLS - set(registry)
        extra = set(registry) - READONLY_TOOLS
        if missing or extra:
            raise RuntimeError(
                f"MCP 工具清单与声明的只读清单（READONLY_TOOLS）漂移："
                f"缺 {sorted(missing)}、多 {sorted(extra)}。新增工具进面必须先改"
                f" READONLY_TOOLS 并过双向清单断言；写工具不在本步射程。")
        self.registry = registry

    def _load_schema(self) -> dict[str, Any]:
        """跑一次 ``cmd_schema`` 拿契约 ``data``（只读连接，用完即关）。"""
        db = Database(self.db_path, read_only=True)
        try:
            args = argparse.Namespace(project=None)
            ctx = Context(args=args, db=db, parser=build_parser())
            data, _human = cmd_schema(args, ctx)
            return data
        finally:
            db.close()

    # -- 请求分发 -------------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """一条已解析的 JSON-RPC 消息 → 应答；**通知返回 ``None``（永不回）**。

        分层：请求形状错 → -32600；方法不存在 → -32601；参数协议错 → -32602；
        实现内部缺陷 → -32603（traceback 上 stderr，不吞）。工具的业务失败不走
        这里 —— 它在 ``tools/call`` 里变成 ``result.isError`` + 信封。
        """
        message_id = message.get("id") if isinstance(message, dict) else None
        has_id = isinstance(message, dict) and "id" in message
        if (not isinstance(message, dict)
                or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)):
            return _error(message_id, INVALID_REQUEST,
                          "Invalid Request —— 需要 jsonrpc:\"2.0\" 与 method 字符串")
        method = message["method"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return None if not has_id else _error(
                message_id, INVALID_REQUEST, "params 必须是对象")
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = self._tools_call(params)
            else:
                # 通知（无 id）与 notifications/* 永不回应 —— JSON-RPC 的铁律；
                # 未知的**请求**方法响亮 -32601。
                if not has_id or method.startswith("notifications/"):
                    return None
                return _error(message_id, METHOD_NOT_FOUND,
                              f"Method not found: {method}")
        except _InvalidParams as exc:
            return None if not has_id else _error(message_id, INVALID_PARAMS, str(exc))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return None if not has_id else _error(
                message_id, INTERNAL_ERROR, "Internal error —— 详情见 stderr")
        return None if not has_id else {"jsonrpc": "2.0", "id": message_id,
                                        "result": result}

    def handle_line(self, line: str) -> str | None:
        """一行文本 → 一行应答文本（``None`` = 不产生行）。换行分帧的落点。"""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return dump_json(_error(None, PARSE_ERROR, f"Parse error: {exc.msg}"))
        response = self.handle(message)
        return dump_json(response) if response is not None else None

    # -- 五个方法 -------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                   else PROTOCOL_VERSION)
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": self.tool_version},
                "instructions": SERVER_INSTRUCTIONS}

    def _tools_list(self) -> dict[str, Any]:
        return {"tools": [self.registry[name].to_mcp()
                          for name in sorted(self.registry)]}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.registry:
            raise _InvalidParams(f"未知工具：{name!r} —— 清单见 tools/list（只读）")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _InvalidParams("arguments 必须是对象")
        tool = self.registry[name]
        started = time.perf_counter()
        db = Database(self.db_path, read_only=True)
        try:
            try:
                data = self._invoke(tool, arguments, db)
            except CliError as exc:
                # 建模过的失败（用法错 / 查不到 / 需裁决…）：与 CLI 同一个信封。
                return _tool_result(exc.envelope(), True)
            except query.QueryError as exc:
                return _tool_result(as_cli_error(exc).envelope(), True)
            except Exception as exc:
                # 查询中途的 sqlite 失败要与 CLI 同一张错误表（E_LOCKED /
                # E_DB_NOT_A_DB…），而 sqlite3 不许 import 进本包 —— 判定住在
                # cli/db.map_db_error。**不是** sqlite 的异常一律不兜（程序缺陷
                # → -32603 + stderr，响亮）。
                mapped = map_db_error(exc, db.path)
                if mapped is None:
                    raise
                return _tool_result(mapped.envelope(), True)
            envelope = {"ok": True, "data": data,
                        "meta": {"db": db.display,
                                 "took_ms": int((time.perf_counter() - started) * 1000),
                                 "schema_version": db.schema_version}}
            return _tool_result(envelope, False)
        finally:
            db.close()

    def _invoke(self, tool: Tool, arguments: dict[str, Any],
                db: Database) -> dict[str, Any]:
        """一个工具怎么真正跑起来：handler 路径，或"还原 argv → CLI runner"。"""
        if tool.command is None:
            if tool.handler is None:               # pragma: no cover - 构造保证
                raise RuntimeError(f"工具 {tool.name} 既无 command 也无 handler")
            return tool.handler(db, arguments)
        argv = _argv_for(tool, arguments)
        parser = build_parser()
        try:
            args = parser.parse_args(argv)         # 用法错 → CliError（同一张信封）
        except SystemExit as exc:
            # 兜底保命（M1）：argparse 的 help/version 动作走 `_Parse.exit` →
            # `SystemExit` —— 它是 **BaseException**，上面 `_tools_call` 与
            # `handle` 的 `except Exception` 都接不住，逃出去就是 serve() 循环
            # 死亡 + 进程伪装卸载。正常情况下到不了这：help 不在 inputSchema、
            # `-` 开头的位置参数值在 `_argv_for` 已挡成 -32602 —— 走到这说明
            # 有新的走私路径，转成工具级用法错（isError 信封），会话继续。
            # （此时 help 文本可能已写向 stdout —— 所以这层只是保命，不能替代
            # 入口的那两道挡。）
            raise CliError(
                "E_USAGE",
                f"magpie {tool.command} 的参数解析被 argparse 以退出码 "
                f"{exc.code} 终止（help/version 类动作不属于工具面）",
                exit_code=EXIT_USAGE,
                hint="位置参数不许以 '-' 开头；要看帮助请用 CLI 的 "
                     "`magpie <命令> --help`，协议面不打印帮助文本。") from exc
        if str(args.command) in WRITABLE:
            # 双保险：注册表只从 read 一半派生，理论上到不了这 —— 到了就是缺陷，
            # 响亮失败而不是"反正只读连接也写不进去"。
            raise RuntimeError(
                f"写命令 {args.command!r} 进了 MCP 工具面（分发层 WRITABLE 挡下）")
        runner = RUNNERS[_runner_key(args)]
        ctx = Context(args=args, db=db, parser=parser)
        data, _human = runner(args, ctx)
        return data


# ---------------------------------------------------------------------------
# stdio 服务循环与入口
# ---------------------------------------------------------------------------


def serve(db_path: Path | str, *, stdin: Any = None,
          stdout: Any = None) -> int:
    """换行分帧的 JSON-RPC 2.0 over stdio。**每行一个消息，应答一行一 flush。**

    流先钉成 UTF-8：Windows 上文本 stdin 默认按 GBK 解，客户端发来的 UTF-8 中文
    会在 decode 那一步坏成乱码甚至 ``UnicodeDecodeError`` —— 与 ``envelope`` 的
    ``_force_utf8`` 同一个理由（StringIO 之类的测试流没有 ``reconfigure``，
    按需跳过）。stdout 上**只有** JSON-RPC 应答；诊断一律去 stderr。
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for stream in (stdin, stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):       # pragma: no cover - 流已关等极端态
            pass
    server = McpServer(db_path)
    for line in stdin:
        if not line.strip():
            continue                               # 空行不是消息
        out = server.handle_line(line)
        if out is not None:
            stdout.write(out + "\n")
            stdout.flush()
    return 0                                        # EOF = 客户端正常断开


def main(argv: list[str] | None = None) -> int:
    """``python -m magpie.adapters.mcp`` 的入口。接线示例在 ``--help`` 里。"""
    parser = argparse.ArgumentParser(
        prog="python -m magpie.adapters.mcp",
        description="MCP（Model Context Protocol）适配器 —— stdio 传输的只读工具面。"
                    " 不发起任何网络请求（红线 R7）；HTTP / SSE 传输不实现。",
        epilog=("接线示例（Claude Desktop 等 MCP 客户端）：\n"
                '  "mcpServers": {"magpie": {"command": "py",\n'
                '    "args": ["-3.13", "-m", "magpie.adapters.mcp",\n'
                '             "--db", "D:/path/.magpie/magpie.db"]}}\n\n'
                "工具面只读（READONLY_TOOLS），从 magpie schema --json 的 commands"
                " 键派生；写操作（import / undo / 合并 / 裁决 / 导出）不在本步"
                " 射程。--db 相对路径按客户端的 cwd 解析，接线建议用绝对路径。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", metavar="<文件>", default=None,
                        help="数据库文件（默认 <data-dir>/magpie.db）")
    parser.add_argument("--data-dir", metavar="<目录>", default=None,
                        help="数据区（默认 .magpie）")
    args = parser.parse_args(argv)
    try:
        return serve(resolve_db(args.db, args.data_dir))
    except CliError as exc:
        # 库不存在 / 版本不匹配：信封去 stderr（stdout 是协议面，不许污染），
        # 退出码沿用 CLI 那张表（2 数据错 / 5 版本不匹配…）。
        print(dump_json(exc.envelope()), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":                          # pragma: no cover
    sys.exit(main())
