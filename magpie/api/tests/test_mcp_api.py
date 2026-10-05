"""0.21.0 · `GET /api/v1/mcp` —— 接入页后端的契约。

这条端点存在的理由写在 `routes.get_mcp` 的注释里：配置要暴露 db 绝对路径与
完整命令表（`/meta` 刻意不给），工具清单必须与适配器 `tools/list` 逐字节同源
（现开一个真 `McpServer` 算，不存第二份）。这一组断言钉的就是那两件事 ——
红了意味着接入页会给出接不上的配置，或者工具清单与适配器漂移。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.adapters import mcp
from magpie.api.app import create_app

TOKEN = "mcp-page-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as c:
        yield c


def test_mcp_page_carries_a_usable_config(client):
    """配置卡的 `--db` 必须是字面的绝对路径 —— 客户端按自己的 cwd 解析，相对路径接不上。"""
    response = client.get("/api/v1/mcp", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    server_cfg = data["config"]["mcpServers"]["magpie"]
    assert server_cfg["command"] == "py"
    args = server_cfg["args"]
    assert args[:3] == ["-3.13", "-m", "magpie.adapters.mcp"], args
    assert args[3] == "--db" and Path(args[4]).is_absolute(), args
    assert data["db_path"] == args[4]

    assert data["server"]["name"] == mcp.SERVER_NAME
    assert data["server"]["protocol_version"] == mcp.PROTOCOL_VERSION
    assert data["server"]["instructions"].strip(), "instructions 不许是空的"
    assert data["server"]["version"], "版本从契约 tool_version 流过来，不许空"


def test_mcp_page_tool_manifest_converges_with_the_adapter(client):
    """工具清单 ≡ 适配器 `tools/list` 的名字集合，只读/写划分与声明逐项相等。"""
    response = client.get("/api/v1/mcp", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    names = [tool["name"] for tool in data["tools"]]
    assert names == sorted(mcp.READONLY_TOOLS | mcp.WRITE_TOOLS), names
    assert data["readonly_tools"] == sorted(mcp.READONLY_TOOLS)
    assert data["write_tools"] == sorted(mcp.WRITE_TOOLS)
    assert set(data["readonly_tools"]).isdisjoint(data["write_tools"])

    # 逐条目形状与适配器的 to_mcp 一致（三件套，不多不少）。
    for tool in data["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}, tool["name"]
        assert tool["description"], tool["name"]

    # 划分正确：写面里的名字确实标在 write_tools，读面互不相交。
    assert set(names) == set(data["readonly_tools"]) | set(data["write_tools"])


def test_mcp_page_rejects_without_token(client):
    """没带令牌就拿不到配置 —— 它暴露 db 绝对路径，与其它端点同一条门。"""
    response = client.get("/api/v1/mcp")
    assert response.status_code in (401, 403), response.text
