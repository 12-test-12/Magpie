"""判据 1：只读自动化断言（有牙的）+ 双向清单校验 —— LARI-43 的核心网（0.21.0 加宽）。

四组断言，各答一个问题：

  * **"每个只读工具跑完库里一个字节没变"** —— 逐表指纹（行数 + 整表内容
    sha256）在"全部**只读**注册工具 × 空实参 / 最小实参"两轮调用前后相等。
    复用 CLI 那份 `_fingerprint`（跨套件 import，与 `test_contract.py` 同款）——
    两份口径分叉的话，"只读"就只对其中一份口径成立。末尾有**反例**：经产品
    自己的写入口真建一个项目，指纹必须变 —— 没有牙齿的只读断言比没有更糟。
  * **"写工具只动它自己的副本"** —— 写工具（恰一个 `add_finding`）拿读写连接，
    所以上一条的排除不能藏东西：对库的**副本**走协议调它 → 副本指纹变、原
    夹具指纹不变。既证明它真能写（不是空转绿），也证明它没越界。
  * **"实际注册的工具集 ≡ 声明（READONLY_TOOLS ∪ WRITE_TOOLS）"** —— 双向
    （实际 ⊆ 声明 且 声明 ⊆ 实际）+ 两份声明互斥：防白名单提前扩张，也防
    声明了一堆没实现的。
  * **"工具面 = 契约 read 那一半 ∪ 声明的 write 那一个"** —— 读半边、写半边
    **分开断言**（各指名一种失败：写命令混进读面 / 读命令伪装成写面）+ 派生
    自洽 + 分类不许说谎 + **派生真的跟着契约走**（喂一份改过分类的契约，
    注册表必须变 —— 手抄一份工具清单在那条断言下不会变，当场红）。
"""

from __future__ import annotations

import copy
import json
import shutil
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


def _envelope(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# 判据 1a：只读工具的库快照不变
# ---------------------------------------------------------------------------


def test_every_readonly_tool_leaves_the_db_untouched(loaded_db):
    """枚举全部**只读**注册工具，逐个调用（两轮），**前后逐表指纹不变**。

    0.21.0 起循环排除 `WRITE_TOOLS` —— 排除的正当性由下面
    `test_the_write_tool_is_confined_to_its_own_copy` 证明（它证明排除没藏东西：
    写工具真的写，但只写自己的副本）。
    """
    before = _fingerprint(loaded_db)
    assert before["entities"]["rows"] > 0, "空夹具 —— 这条断言什么都没验到"

    server = mcp.McpServer(loaded_db)
    readonly = [name for name in sorted(server.registry)
                if name not in mcp.WRITE_TOOLS]
    assert readonly, "只读工具一个不剩 —— 这条断言退化成空转"
    envelopes = []
    for name in readonly:
        for arguments in ({}, dict(MINIMAL_ARGS.get(name, {}))):
            result = _call(server, name, arguments)
            envelopes.append(_envelope(result))

    assert _fingerprint(loaded_db) == before, \
        "某个 MCP 只读工具动了库 —— 只读半边必须一个字节不写"
    # 非空转：至少 ls 那一轮真的读到了数据（全员失败的"只读"是空转绿）。
    assert any(env.get("ok") for env in envelopes), \
        f"没有任何一次 tools/call 成功，快照断言什么都没验到：{envelopes[:3]}"


def test_the_write_tool_is_confined_to_its_own_copy(loaded_db, tmp_path):
    """写工具的边界证明：协议调 `add_finding` → **副本变、原夹具不变**。

    上一条把 `WRITE_TOOLS` 排除出了只读循环 —— 如果这个排除在藏东西（写工具
    越界动了别人的库），这条当场红。反过来，如果写工具其实写不进去（空转），
    "副本指纹变"那半也红：两半合起来才是完整的"它只动它该动的"。
    """
    original_before = _fingerprint(loaded_db)
    copy_db = tmp_path / "mcp-write-copy.db"
    shutil.copy2(loaded_db, copy_db)
    copy_before = _fingerprint(copy_db)

    server = mcp.McpServer(copy_db)
    # 拿一个真实存在的实体 key（判据取自 `ls` 的输出 —— 与同文件其它用例同一条纪律）
    listed = _envelope(_call(server, "ls", {}))
    assert listed["ok"] and listed["data"]["entities"], listed
    ref = listed["data"]["entities"][0]["entity_key"]

    result = _call(server, "add_finding", {"ref": ref, "text": "副本上的 AI 发现"})
    envelope = _envelope(result)
    assert result["isError"] is False and envelope["ok"] is True, envelope
    assert envelope["data"]["job_id"] and envelope["data"]["op"] == "ai_note.add"

    assert _fingerprint(copy_db) != copy_before, \
        "写工具声称成功却没写进副本 —— 这条用例是空转绿"
    assert _fingerprint(loaded_db) == original_before, \
        "写工具动了原夹具 —— 越界：它只许写自己拿到的那个库"


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
# 判据 1b：双向清单校验（声明 = 读 ∪ 写，且两份互斥）
# ---------------------------------------------------------------------------


def test_registered_tools_exactly_match_the_declared_manifest(empty_db):
    """实际注册 ≡ 声明（READONLY_TOOLS ∪ WRITE_TOOLS）——**两个方向各断言一次**。

    只做 `==` 也成立，但分开写是有意的：两条红的**含义**不同（一个是悄悄混进
    新工具、一个是声明了没实现），修的人要看懂是哪种才该动哪一半。外加两道
    0.21.0 的新牙：两份声明**必须互斥**（同一个名字既读又写 = 边界糊了），
    写声明必须逐个真的进了面（WRITE_TOOLS 不许是死声明）。
    """
    actual = frozenset(mcp.McpServer(empty_db).registry)
    declared = mcp.READONLY_TOOLS | mcp.WRITE_TOOLS
    assert mcp.READONLY_TOOLS.isdisjoint(mcp.WRITE_TOOLS), \
        f"一个工具同时声明成只读和写：{sorted(mcp.READONLY_TOOLS & mcp.WRITE_TOOLS)}"
    assert actual <= declared, \
        f"声明外的工具混进了面（扩张必须显式改声明清单）：{sorted(actual - declared)}"
    assert declared <= actual, \
        f"声明了却没实现（不许有死声明）：{sorted(declared - actual)}"
    assert mcp.WRITE_TOOLS <= actual, \
        f"写声明是死的（AI 发现通道没进面）：{sorted(mcp.WRITE_TOOLS - actual)}"


# ---------------------------------------------------------------------------
# 判据 1c：派生真的来自契约（读半边 / 写半边分开断言）
# ---------------------------------------------------------------------------


def test_the_read_half_is_the_read_half_of_the_contract(empty_db):
    """读半边 ≡ 契约 read（剔除明文排除项与写工具名），且逐个分类不说谎。"""
    server = mcp.McpServer(empty_db)
    commands = server.schema["commands"]
    read_half = {key for key, entry in commands.items() if entry["access"] == "read"}
    expected = {key for key in read_half
                if key not in mcp.EXCLUDED_READ_COMMANDS
                and mcp._tool_name(key) not in mcp.WRITE_TOOLS}
    derived = {tool.command for tool in server.registry.values()
               if tool.command and tool.name not in mcp.WRITE_TOOLS}
    assert derived == expected, \
        f"读半边与契约脱钩 —— 手抄的清单在这里会红：多了 {sorted(derived - expected)}、" \
        f"少了 {sorted(expected - derived)}"
    for name in sorted(derived):
        tool = server.registry[name]
        entry = commands[tool.command]
        assert entry["access"] == "read" and entry["mutates_db"] is False, tool.name
        # 分发层事实（WRITABLE）再核一遍 —— 契约分类本就该由 5.2 钉在它身上，
        # 两道都过才算"读半边只许取 read 那一半"真的成立。
        assert tool.command.split()[0] not in WRITABLE, \
            f"{tool.command} 是写命令，不许混进读半边"
    # 每个排除项都得真的是 read 半边的活命令 —— 否则它就是一条死条目
    # （哪天 export 改分类成 write，这条先红，提醒把死条目摘掉）。
    for key in mcp.EXCLUDED_READ_COMMANDS:
        assert key in read_half, f"排除项 {key!r} 不在契约 read 半边 —— 已是死条目"


def test_the_write_half_is_exactly_the_declared_write_tool(empty_db):
    """写半边 ≡ 声明的 WRITE_TOOLS，且逐个分类与分发层事实不说谎。"""
    server = mcp.McpServer(empty_db)
    commands = server.schema["commands"]
    write_half = {key for key, entry in commands.items() if entry["access"] == "write"}
    derived = {tool.name for tool in server.registry.values()
               if tool.command and tool.command.split()[0] in WRITABLE}
    expected = {key for key in write_half if mcp._tool_name(key) in mcp.WRITE_TOOLS}
    assert {mcp._tool_name(key) for key in expected} == derived == mcp.WRITE_TOOLS, \
        ("写半边与声明脱钩：进面的写工具必须逐项等于 WRITE_TOOLS，"
         f"derived={sorted(derived)} write_half={sorted(write_half)}")
    for name in sorted(derived):
        tool = server.registry[name]
        entry = commands[tool.command]
        assert entry["access"] == "write" and entry["mutates_db"] is True, \
            f"{tool.command} 声明成写工具，契约却说 access={entry['access']!r}"
        assert tool.command.split()[0] in WRITABLE, \
            f"{tool.command} 不在分发层 WRITABLE 里 —— 写命令必须骑在 WRITABLE 上"
        assert tool.command.split()[0] not in {
            key for key, e in commands.items() if e["access"] == "read"}, \
            f"{tool.command} 同时是 read 和 write —— 分类说谎了"


def test_derivation_follows_the_live_contract(empty_db):
    """喂一份**改过分类**的契约，注册表必须跟着变 —— 手抄的清单不会变。

    三个方向（0.21.0 加了第三个）：把 `import` 标成 read（假想的新命令进面）
    → 派生出 `import`（于是双向清单断言必红 —— 这正是"新增命令必须显式进声明"
    的牙齿）；把 `ls` 标成 write → 派生清单当场丢掉 `ls`（同理必红）；
    **把 `add_finding` 的 access 伪造成 read** → 写工具名只从写分支进面，
    读分支必须丢弃它（伪装不会让写命令拿到读工具的待遇）。
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

    doctored3 = copy.deepcopy(schema)
    doctored3["commands"]["add_finding"]["access"] = "read"
    doctored3["commands"]["add_finding"]["mutates_db"] = False
    assert "add_finding" not in mcp.build_registry(doctored3), \
        "把 add_finding 的 access 伪造成 read，它就不该再进面 —— 否则分类" \
        "可以被一张改过的契约收买"


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
