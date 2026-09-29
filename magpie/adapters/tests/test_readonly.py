"""判据 1：只读自动化断言（有牙的）+ 双向清单校验 —— LARI-43 的核心网。

三组断言，各答一个问题：

  * **"每个工具跑完库里一个字节没变"** —— 逐表指纹（行数 + 整表内容 sha256）
    在"全部注册工具 × 空实参 / 最小实参"两轮调用前后相等。复用 CLI 那份
    `_fingerprint`（跨套件 import，与 `test_contract.py` 同款）—— 两份口径
    分叉的话，"只读"就只对其中一份口径成立。末尾有**反例**：经产品自己的写
    入口真建一个项目，指纹必须变 —— 没有牙齿的只读断言比没有更糟。
  * **"实际注册的工具集 ≡ 声明的只读清单"** —— 双向（实际 ⊆ 声明 且
    声明 ⊆ 实际）：防白名单提前扩张，也防声明了一堆没实现的。
  * **"工具面真的是契约的 read 那一半"** —— 派生自洽 + 分类不许说谎 +
    **派生真的跟着契约走**（喂一份改过分类的契约，注册表必须变 —— 手抄一份
    工具清单在那条断言下不会变，当场红）。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from magpie.adapters import mcp                                          # noqa: E402
from magpie.cli import WRITABLE                                          # noqa: E402
from magpie.cli.tests.conftest import ENV                                # noqa: E402
from magpie.cli.tests.test_readonly import _fingerprint                  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]

#: 每个工具的最小实参。没有必填参数的工具给空对象 —— 空实参那一轮走的是
#: 用法错路径，它同样不许碰库（`show` 缺 ref、`agent_context` 缺 entity_id
#: 都会失败，但"失败"和"写库"是两回事）。
MINIMAL_ARGS = {
    "show": {"ref": "1"},
    "jobs": {"job": "1"},
    "agent_context": {"entity_id": 1},
}


def _call(server: mcp.McpServer, name: str, arguments: dict) -> dict:
    """走**完整协议**调一个工具，回 tools/call 的 result（协议级错误在这里炸）。"""
    response = server.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    assert response is not None and "result" in response, \
        f"{name} 掉到了协议级错误：{response}"
    return response["result"]


# ---------------------------------------------------------------------------
# 判据 1a：库快照不变
# ---------------------------------------------------------------------------


def test_every_registered_tool_leaves_the_db_untouched(loaded_db):
    """枚举全部注册工具，逐个调用（两轮），**前后逐表指纹不变**。"""
    before = _fingerprint(loaded_db)
    assert before["entities"]["rows"] > 0, "空夹具 —— 这条断言什么都没验到"

    server = mcp.McpServer(loaded_db)
    envelopes = []
    for name in sorted(server.registry):
        for arguments in ({}, dict(MINIMAL_ARGS.get(name, {}))):
            result = _call(server, name, arguments)
            envelopes.append(json.loads(result["content"][0]["text"]))

    assert _fingerprint(loaded_db) == before, \
        "某个 MCP 工具动了库 —— 工具面必须只读"
    # 非空转：至少 ls 那一轮真的读到了数据（全员失败的"只读"是空转绿）。
    assert any(env.get("ok") for env in envelopes), \
        f"没有任何一次 tools/call 成功，快照断言什么都没验到：{envelopes[:3]}"


def test_the_db_fingerprint_has_teeth(loaded_db):
    """反例：经**产品自己的写入口**真建一个项目，指纹必须变。

    走真子进程的 `magpie project create`（不是一句裸 SQL）：要证明的是这套
    测量对"任何经入口发生的写"有反应，而入口之外的写根本不存在（R1）。
    """
    before = _fingerprint(loaded_db)
    proc = subprocess.run(
        [sys.executable, "-m", "magpie.cli", "project", "create",
         "mcp-fingerprint-probe", "--db", str(loaded_db), "--json"],
        cwd=str(ROOT), capture_output=True, env=ENV, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads(proc.stdout.decode("utf-8"))["ok"] is True
    assert _fingerprint(loaded_db) != before, \
        "指纹没检出一次真的写 —— 上面那条只读断言是假的"


# ---------------------------------------------------------------------------
# 判据 1b：双向清单校验
# ---------------------------------------------------------------------------


def test_registered_tools_exactly_match_the_declared_readonly_manifest(empty_db):
    """实际注册 ≡ 声明（READONLY_TOOLS）——**两个方向各断言一次**。

    只做 `==` 也成立，但分开写是有意的：两条红的**含义**不同（一个是悄悄混进
    新工具、一个是声明了没实现），修的人要看懂是哪种才该动哪一半。
    """
    actual = frozenset(mcp.McpServer(empty_db).registry)
    assert actual <= mcp.READONLY_TOOLS, \
        f"声明外的工具混进了面（扩张必须显式改 READONLY_TOOLS）：{sorted(actual - mcp.READONLY_TOOLS)}"
    assert mcp.READONLY_TOOLS <= actual, \
        f"声明了却没实现（不许有死声明）：{sorted(mcp.READONLY_TOOLS - actual)}"


# ---------------------------------------------------------------------------
# 判据 1c：派生真的来自契约
# ---------------------------------------------------------------------------


def test_every_tool_is_the_read_half_of_the_contract(empty_db):
    """工具面 ≡ 契约 read 半边（剔除明文排除项），且逐个工具的分类不说谎。"""
    server = mcp.McpServer(empty_db)
    commands = server.schema["commands"]
    read_half = {key for key, entry in commands.items() if entry["access"] == "read"}
    derived = {tool.command for tool in server.registry.values() if tool.command}
    assert derived == read_half - set(mcp.EXCLUDED_READ_COMMANDS), \
        "工具清单与契约的 read 半边脱钩 —— 手抄的清单在这里会红"
    for tool in server.registry.values():
        if tool.command is None:
            continue                               # agent_context 走手声明分支
        entry = commands[tool.command]
        assert entry["access"] == "read" and entry["mutates_db"] is False, tool.name
        # 分发层事实（WRITABLE）再核一遍 —— 契约分类本就该由 5.2 钉在它身上，
        # 两道都过才算"工具面只许取 read 那一半"真的成立。
        assert tool.command.split()[0] not in WRITABLE, \
            f"{tool.command} 是写命令，不许进 MCP 工具面"
    # 每个排除项都得真的是 read 半边的活命令 —— 否则它就是一条死条目
    # （哪天 export 改分类成 write，这条先红，提醒把死条目摘掉）。
    for key in mcp.EXCLUDED_READ_COMMANDS:
        assert key in read_half, f"排除项 {key!r} 不在契约 read 半边 —— 已是死条目"


def test_derivation_follows_the_live_contract(empty_db):
    """喂一份**改过分类**的契约，注册表必须跟着变 —— 手抄的清单不会变。

    两个方向：把 `import` 标成 read（假想的新命令进面）→ 派生出 `import`
    （于是双向清单断言必红 —— 这正是"新增命令必须显式进声明"的牙齿）；
    把 `ls` 标成 write → 派生清单当场丢掉 `ls`（同理必红）。
    """
    schema = mcp.McpServer(empty_db).schema

    doctored = copy.deepcopy(schema)
    doctored["commands"]["import"]["access"] = "read"
    doctored["commands"]["import"]["mutates_db"] = False
    assert "import" in mcp.build_registry(doctored), \
        "契约把 import 划成 read 了，派生清单却没跟着变 —— 这说明清单是手抄的"

    doctored2 = copy.deepcopy(schema)
    doctored2["commands"]["ls"]["access"] = "write"
    assert "ls" not in mcp.build_registry(doctored2), \
        "契约把 ls 划成 write 了，派生清单却还在出 ls —— 这说明清单是手抄的"


def test_descriptions_enums_and_version_come_from_the_contract(empty_db):
    """描述 ← 契约 query_syntax、参数枚举 ← 契约 enums、版本 ← 契约 tool_version。"""
    server = mcp.McpServer(empty_db)
    syntax = server.schema["query_syntax"]["commands"]
    for tool in server.registry.values():
        if tool.command is not None:
            assert tool.description == syntax[tool.command]["help"], tool.name

    # 参数枚举从 enums 键（真库 CHECK）来，不是抄 argparse 的 choices。
    ls_props = server.registry["ls"].input_schema["properties"]
    assert ls_props["lifecycle"]["enum"] == server.schema["enums"]["lifecycle"]
    lens_prop = server.registry["lens"].input_schema["properties"]["lens"]
    assert lens_prop["enum"] == server.schema["enums"]["lens"], \
        "lens 的取值在 argparse 里没有 choices —— 它只能来自契约 enums 键"

    # tool_version 流到 initialize 的 serverInfo（MCP 的版本就是契约的版本）。
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}})
    assert response["result"]["serverInfo"]["version"] == server.schema["tool_version"]
    assert server.tool_version == server.schema["tool_version"]
