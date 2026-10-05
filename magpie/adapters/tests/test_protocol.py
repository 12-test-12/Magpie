"""判据 3：协议往返黄金夹（进程内，不起网络）+ 错误分层 + 上下文包收敛。

黄金夹的口径是**钉行为、不抄实现**：initialize 的应答整份比对（版本取活值）、
tools/list 的结构与名字集合比对、tools/call 的信封逐层拆开比对（外层 MCP
result、内层 CLI 信封、meta 的键集合）。任何一处形状漂移，这里的整份 ==
先红。

错误分两层，各有各的应答通道，测试也分两组钉：

  * **协议级**（坏 JSON / 未知方法 / 未知工具 / 未知参数 / 形状错）→
    JSON-RPC `error` 码（-32700 / -32601 / -32602 / -32600）；
  * **工具级**（查不到、需裁决、用法错…）→ `result.isError: true` +
    内层信封（`{"ok": false, "error": {code, …}}`，**没有 meta** ——
    `envelope.py` 点名的三个消费者里 MCP 是第三个，错误不带 meta 这条
    谁都不许破）。
"""

from __future__ import annotations

import json
import shutil
import sys
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from magpie.adapters import mcp                                          # noqa: E402


def _request(message_id, method, params=None) -> dict:
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _envelope_of(result: dict) -> dict:
    assert set(result) == {"content", "isError"}, result
    assert len(result["content"]) == 1
    content = result["content"][0]
    assert set(content) == {"type", "text"} and content["type"] == "text"
    return json.loads(content["text"])


def _call_tool(server: mcp.McpServer, name: str, arguments: dict) -> dict:
    """tools/call 一次 → 外层 result（协议级错误在这里炸）。"""
    response = server.handle(_request(0, "tools/call",
                                      {"name": name, "arguments": arguments}))
    assert response is not None and "result" in response, \
        f"{name} 掉到了协议级错误：{response}"
    return response["result"]


# ---------------------------------------------------------------------------
# 黄金链：initialize → notifications/initialized → ping → tools/list → tools/call
# ---------------------------------------------------------------------------


def test_golden_chain_initialize_list_call(loaded_db):
    server = mcp.McpServer(loaded_db)

    # 1) initialize —— 整份应答比对（版本取活值：它就是契约的 tool_version 键）
    init = server.handle(_request(
        1, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "golden", "version": "1.0"}}))
    assert init == {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "magpie", "version": server.tool_version},
        "instructions": mcp.SERVER_INSTRUCTIONS}}
    assert "只读" in init["result"]["instructions"]

    # 2) 初始化通知 —— 无应答（JSON-RPC 通知永不回）
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized",
                          "params": {}}) is None

    # 3) ping —— 空 result
    assert server.handle(_request(2, "ping")) == \
        {"jsonrpc": "2.0", "id": 2, "result": {}}

    # 4) tools/list —— 结构、名字集合、派生出的参数形状
    listing = server.handle(_request(3, "tools/list", {}))
    tools = listing["result"]["tools"]
    # 0.21.0：工具面 = 只读 ∪ 唯一写通道（写面恰一个，双向断言在 test_readonly）
    assert [t["name"] for t in tools] == sorted(mcp.READONLY_TOOLS | mcp.WRITE_TOOLS)
    for tool in tools:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["description"], tool["name"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False

    by_name = {t["name"]: t for t in tools}
    ls_props = by_name["ls"]["inputSchema"]["properties"]
    # option → 属性名是 argparse 的 dest（--in-cidr → in_cidr），类型从真 parser 算
    assert set(ls_props) >= {"kind", "lens", "q", "tag", "lifecycle", "product",
                             "version", "endpoint", "severity", "state", "in_cidr",
                             "cursor", "limit", "project"}
    assert ls_props["limit"]["type"] == "integer"
    assert ls_props["limit"]["default"] == 50
    assert set(ls_props["limit"]) == {"type", "description", "default"}, \
        "ls --limit 的 inputSchema 形状漂了（type / description / default 之外多了东西）"
    assert by_name["show"]["inputSchema"]["required"] == ["ref"]
    assert by_name["lens"]["inputSchema"].get("required") is None, \
        "lens 位置参数是 nargs='?' —— 不给时列出全部镜头，不该标必填"

    # 5) tools/call（option 路径：--limit 3）—— 外层 result + 内层信封逐层比对
    call = server.handle(_request(4, "tools/call",
                                  {"name": "ls", "arguments": {"limit": 3}}))
    result = call["result"]
    assert result["isError"] is False
    envelope = _envelope_of(result)
    assert envelope["ok"] is True
    assert envelope["data"]["count"] == 3
    assert len(envelope["data"]["entities"]) == 3
    assert set(envelope["meta"]) == {"db", "took_ms", "schema_version"}
    # 与 --json 同一条可消费性纪律：纯 ASCII，GBK 控制台的消费者也读得走
    result["content"][0]["text"].encode("ascii")

    # 6) tools/call（位置参数路径：ref=1）
    call2 = server.handle(_request(5, "tools/call",
                                   {"name": "show", "arguments": {"ref": "1"}}))
    envelope2 = _envelope_of(call2["result"])
    assert call2["result"]["isError"] is False
    assert envelope2["ok"] is True
    assert envelope2["data"]["entity"]["id"] == 1


def test_tool_failure_is_an_error_envelope_without_meta(loaded_db):
    """工具级失败：`isError: true` + 信封，**失败没有 meta**（三个消费者同形）。"""
    server = mcp.McpServer(loaded_db)
    response = server.handle(_request(1, "tools/call",
                                      {"name": "show", "arguments": {"ref": "99999"}}))
    result = response["result"]
    assert result["isError"] is True
    envelope = _envelope_of(result)
    assert envelope["ok"] is False
    assert "meta" not in envelope, "错误信封带了 meta —— 与 CLI /api/v1 分叉"
    assert envelope["error"]["code"] == "E_ENTITY_NOT_FOUND"
    assert envelope["error"]["hint"], "报错必须带 hint（信封契约）"


# ---------------------------------------------------------------------------
# 协议级错误
# ---------------------------------------------------------------------------


def test_protocol_errors_use_jsonrpc_codes(empty_db):
    server = mcp.McpServer(empty_db)

    # 未知方法 → -32601；但未知**通知**不回应（JSON-RPC：通知永不回）
    assert server.handle(_request(1, "resources/list"))["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {}}) is None

    # 未知工具 / 未知参数 / arguments 形状错 → -32602
    for params in ({"name": "import", "arguments": {}},
                   {"name": "ls", "arguments": {"drop_table": True}},
                   {"name": "ls", "arguments": ["limit"]}):
        response = server.handle(_request(2, "tools/call", params))
        assert response["error"]["code"] == -32602, params

    # 形状错 → -32600；坏 JSON → -32700（id 为 null）
    assert server.handle(["not", "a", "dict"])["error"]["code"] == -32600
    parse_error = json.loads(server.handle_line("{oops"))
    assert parse_error["error"]["code"] == -32700
    assert parse_error["id"] is None and parse_error["jsonrpc"] == "2.0"

    # 控制组：上面一串错误之后，正常请求照常应答（服务没被"毒"到）
    assert "tools" in server.handle(_request(3, "tools/list"))["result"]


def test_protocol_version_negotiation(empty_db):
    """客户端请求支持的版本照它回；不认识的版本回服务端自己的（协商语义）。"""
    server = mcp.McpServer(empty_db)
    for requested, expected in (("2024-11-05", "2024-11-05"),
                                ("1999-01-01", mcp.PROTOCOL_VERSION),
                                (None, mcp.PROTOCOL_VERSION)):
        params = {} if requested is None else {"protocolVersion": requested}
        response = server.handle(_request(1, "initialize", params))
        assert response["result"]["protocolVersion"] == expected, requested


# ---------------------------------------------------------------------------
# M1 回归：help 走私不许杀死会话、不许污染协议流
# ---------------------------------------------------------------------------
#
# 审查实测的逃逸链：位置参数值 `-h` → argparse help 动作 → 帮助文本 print 到
# stdout（协议流）+ `SystemExit` → `except Exception` 接不住（BaseException）
# → serve() 循环死亡、进程以 0 伪装卸载、后续请求无人应答。三层修复各有断言：
# 入口挡（-32602）、help 属性不进面（-32602）、兜底（isError 信封）。


def test_help_smuggling_cannot_kill_the_session(loaded_db):
    """四条同形攻击路径全部落进协议层应答（-32602），会话随后照常 ping。"""
    server = mcp.McpServer(loaded_db)
    attacks = (
        {"name": "show", "arguments": {"ref": "-h"}},
        {"name": "show", "arguments": {"ref": "--help"}},
        {"name": "lens", "arguments": {"lens": "-h"}},
        {"name": "jobs", "arguments": {"job": "-h"}},
    )
    for attack in attacks:
        response = server.handle(_request(1, "tools/call", attack))
        assert response is not None, f"攻击 {attack} 没有应答 —— 会话被杀了"
        assert "error" in response and "result" not in response, \
            f"攻击 {attack} 没落进协议层错误：{response}"
        assert response["error"]["code"] == -32602, attack

    # 同族第二条路：`help` 属性根本不该在工具面里（它一进 argv 就是 --help）
    listing = server.handle(_request(2, "tools/list"))["result"]["tools"]
    for tool in listing:
        assert "help" not in tool["inputSchema"]["properties"], \
            f"{tool['name']} 的 inputSchema 泄漏了 help 属性"
    smuggled = server.handle(_request(
        3, "tools/call", {"name": "show", "arguments": {"help": True}}))
    assert smuggled["error"]["code"] == -32602, smuggled

    # 会话还活着
    assert server.handle(_request(4, "ping"))["result"] == {}


def test_serve_stream_stays_json_only_under_help_attack(loaded_db):
    """真 serve() 循环：攻击之后 ping 仍有应答，**stdout 每一行都是可解析 JSON**。"""
    stdin = StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2025-06-18"}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"show","arguments":{"ref":"-h"}}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
    stdout = StringIO()
    assert mcp.serve(loaded_db, stdin=stdin, stdout=stdout) == 0

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 3, \
        f"应答行数不对（help 文本混进协议流了？）：{len(lines)} 行"
    parsed = [json.loads(line) for line in lines]      # 每行都是可解析 JSON
    assert "result" in parsed[0]                       # initialize 应答
    assert parsed[1]["error"]["code"] == -32602        # 攻击 → 协议层错误
    assert parsed[2]["result"] == {}                   # ping 仍被应答


def test_systemexit_backstop_converts_to_error_envelope(monkeypatch, loaded_db):
    """② 兜底保命：万一 SystemExit 仍从 parse_args 逃出 → 工具级 isError 信封。"""
    server = mcp.McpServer(loaded_db)

    class _ExplodingParser:
        """与 `build_parser()` 同接口；parse_args 抛 SystemExit(0) —— argparse
        help 动作在 `_Parse.exit` 里的真实形状（M1 的根因）。"""

        def parse_args(self, argv):
            raise SystemExit(0)

    monkeypatch.setattr(mcp, "build_parser", lambda: _ExplodingParser())
    response = server.handle(_request(1, "tools/call",
                                      {"name": "ls", "arguments": {}}))
    assert response is not None and "result" in response, \
        f"SystemExit 逃出了协议层：{response}"
    assert response["result"]["isError"] is True
    envelope = _envelope_of(response["result"])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_USAGE"
    assert "meta" not in envelope

    # 会话继续（ping 不经过 build_parser，monkeypatch 自动复原也无妨）
    assert server.handle(_request(2, "ping"))["result"] == {}


# ---------------------------------------------------------------------------
# 换行分帧（serve 循环，进程内流）
# ---------------------------------------------------------------------------


def test_serve_frames_one_response_per_line(loaded_db):
    """stdin 三行（请求 / 空行 / 通知 / 请求）→ stdout 恰好两行可解析 JSON。"""
    stdin = StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        "\n"
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
    stdout = StringIO()
    assert mcp.serve(loaded_db, stdin=stdin, stdout=stdout) == 0

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2, f"空行与通知不许产生应答行：{lines}"
    first, second = (json.loads(line) for line in lines)
    assert first["result"] == {}
    assert [t["name"] for t in second["result"]["tools"]] == \
        sorted(mcp.READONLY_TOOLS | mcp.WRITE_TOOLS)


# ---------------------------------------------------------------------------
# 上下文包：与 API 路由收敛（读侧组合不许各写各的）
# ---------------------------------------------------------------------------


def test_agent_context_converges_with_the_api_route(loaded_db):
    """MCP 的 `agent_context` 与 `GET /api/v1/agent/context` 必须给同一份 payload。

    两边的组合各写了一份（CLI 时代起的既有形状：每个传输自己组合 query 调用），
    收敛只能靠断言钉：**JSON 逐键相等 + Markdown 逐字节相等**。API 那边改了
    组合、或 MCP 这边改了，先红的都是这条。

    extras.api 缺失时 fastapi import 不了 —— 那是 4.5 的"缺失路径"，跳过；
    fastapi 在而路由模块坏了则必须红（不是跳过）。
    """
    pytest.importorskip("fastapi")
    from magpie.api import routes
    from magpie.cli.db import Database

    server = mcp.McpServer(loaded_db)
    result = server.handle(_request(1, "tools/call",
                                    {"name": "agent_context",
                                     "arguments": {"entity_id": 1, "depth": 1}}))
    assert result["result"]["isError"] is False, result
    mine = _envelope_of(result["result"])
    assert mine["ok"] is True

    db = Database(loaded_db, read_only=True)
    try:
        conn = db.connect()
        theirs = routes.agent_context(entity_id=1, depth=1, project=None, conn=conn)
    finally:
        db.close()

    assert theirs["data"]["json"] == mine["data"]["json"], \
        "上下文包 payload 与 API 分叉"
    assert theirs["data"]["markdown"] == mine["data"]["markdown"], \
        "上下文包 Markdown 与 API 分叉 —— 两份渲染没跟上"


# ---------------------------------------------------------------------------
# add_finding：与 POST /api/v1/notes 收敛（写侧组合不许各写各的）
# ---------------------------------------------------------------------------


def test_add_finding_converges_with_the_notes_route(loaded_db, tmp_path):
    """MCP `add_finding` 与 `POST /api/v1/notes` 必须落出同一形状的结果。

    两个人机入口一份实现（都落 `ops.add_finding`）—— 收敛只能靠断言钉：
    **两份同源库副本**，一边走 MCP 协议、一边走 TestClient，落完之后比三件事：
    报告的键集合与内容寻址字段、作业的 kind/state、观测的 origin。
    哪边改了组合或改了落库语义，先红的都是这条（与 agent_context 那条同款）。

    extras.api 缺失时 fastapi import 不了 —— 那是 4.5 的"缺失路径"，跳过；
    fastapi 在而路由坏了则必须红（不是跳过）。
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from magpie.api.app import create_app

    mcp_db = tmp_path / "via-mcp.db"
    http_db = tmp_path / "via-http.db"
    shutil.copy2(loaded_db, mcp_db)
    shutil.copy2(loaded_db, http_db)
    headers = {"Authorization": "Bearer convergence-token"}
    text = "同一条发现：/api/debug 未鉴权"

    # -- HTTP 一半 ------------------------------------------------------------
    app = create_app(db_path=http_db, data_dir=tmp_path / "data",
                     token="convergence-token")
    with TestClient(app) as client:
        entities = client.get("/api/v1/entities?limit=5",
                              headers=headers).json()["data"]
        assert entities, "夹具库里没有实体"
        entity_id = int(entities[0]["id"])

        posted = client.post("/api/v1/notes", headers=headers,
                             json={"entity_id": entity_id, "text": text})
        assert posted.status_code == 200, posted.text
        http_report = posted.json()["data"]
        http_job = client.get(f"/api/v1/jobs/{http_report['job_id']}",
                              headers=headers).json()["data"]["job"]
        http_obs = client.get(f"/api/v1/entities/{entity_id}",
                              headers=headers).json()["data"]["observations"]
    http_origin = [o["origin"] for o in http_obs
                   if int(o["id"]) == int(http_report["observation_id"])]

    # -- MCP 一半 -------------------------------------------------------------
    server = mcp.McpServer(mcp_db)
    result = server.handle(_request(1, "tools/call",
                                    {"name": "add_finding",
                                     "arguments": {"ref": str(entity_id),
                                                   "text": text}}))
    assert result is not None and "result" in result, result
    mcp_result = result["result"]
    assert mcp_result["isError"] is False, mcp_result
    mcp_report = _envelope_of(mcp_result)["data"]

    mcp_job = _envelope_of(_call_tool(server, "jobs",
                                      {"job": str(mcp_report["job_id"])}))["data"]["job"]
    mcp_obs = _envelope_of(_call_tool(server, "show",
                                      {"ref": str(entity_id)}))["data"]["observations"]
    mcp_origin = [o["origin"] for o in mcp_obs
                  if int(o["id"]) == int(mcp_report["observation_id"])]

    # -- 收敛断言 -------------------------------------------------------------
    assert set(mcp_report) == set(http_report), \
        f"两个入口的报告形状分叉：MCP {sorted(mcp_report)} vs HTTP {sorted(http_report)}"
    assert mcp_report["op"] == http_report["op"] == "ai_note.add"
    assert mcp_report["semantic_field"] == http_report["semantic_field"], \
        "同一段文本在两个入口落成了不同字段 —— 内容寻址不收敛"
    assert mcp_report["source"] == http_report["source"] == "ai:add_finding"
    assert mcp_report["text_length"] == http_report["text_length"]
    assert (mcp_job["kind"], mcp_job["state"]) == \
           (http_job["kind"], http_job["state"]) == ("ai_note", "done")
    assert http_origin == mcp_origin == ["manual"], \
        f"origin 不收敛：MCP {mcp_origin} vs HTTP {http_origin}"
