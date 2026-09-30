"""零出站专项（G5 复验面之一）：`GET /agent/context` 在出站能力被钉死时照常出货。

**为什么静态红线还不够**：`check.py` 的 R7 扫的是源码里的出站**写法**
（`import urllib.request` / `socket.connect(...)` 调用点）。它保证"没人写出站
代码"，但保证不了"端点跑起来真的没伸手出去" —— 运行时的行为要靠行为断言：
把 `socket` 的两个出口当场钉死（一连就炸），再真调一次端点，双形状照旧产生
才算数。两层防的不是同一件事，少了行为这层，"零出站"只是 lint 的一个绿灯。

**为什么单独一个文件**：这是 G5 要复验的面。单独成档让复验命令短到不用猜：

    py -3.13 -m pytest magpie/api/tests/test_agent_context_offline.py -q

**反例纪律**：这条断言必须证明"有牙"—— 2026-09-30 的反例实测是在
`routes.py` 的 handler 里临时塞一行 `urllib.request.urlopen(...)`，本文件红，
还原后复绿（实测输出附在 LARI-44 交付评论里）。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app

ROOT = Path(__file__).resolve().parents[3]
SUBFINDER = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"
HTTPX = ROOT / "bench" / "e2e" / "recon" / "httpx.jsonl"

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def loaded(client):
    """一个有真实数据的库：一次真导入，然后等到它跑完。"""
    client.post("/api/v1/projects", headers=HEADERS,
                json={"name": "demo", "scope": {"include": ["*.example.com"]}})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": [str(SUBFINDER), str(HTTPX)],
                                 "project": "demo"})
    import_id = response.json()["data"]["import_id"]
    deadline = time.monotonic() + 60
    record = None
    while time.monotonic() < deadline:
        record = client.app.state.ctx.imports.get(import_id)
        if record is not None and record.terminal:
            break
        time.sleep(0.02)
    assert record is not None and record.state == "done", f"夹具导入失败：{record}"
    return client


def _with_observations(client) -> int:
    """夹具里**真的有观测**的那个实体 id（与 test_routes 同一条选取理由：
    写死 kind 会随解析器扩张而失效）。"""
    listing = client.get("/api/v1/entities?limit=500", headers=HEADERS).json()["data"]
    assert listing, "导入后没有任何实体"
    found = next((e for e in listing if e["observation_count"] > 0), None)
    assert found is not None, "导入后没有一个实体带观测，测不出上下文包"
    return found["id"]


@pytest.fixture()
def outbound_blocked(monkeypatch):
    """把出站能力钉死：**任何一条真连接都当场炸**，并把每次尝试记下来。

    钉的是行为不是写法 —— `socket.socket.connect` 是所有出站的必经一步
    （`urllib`/`http.client` 最终也走它），`socket.create_connection` 是标准库
    客户端的入口。两处都在，拆开写（先建套接字再连）也绕不过去。
    """
    attempts: list[tuple] = []

    def _blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("零出站被破：运行期间试图建立出站连接")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    return attempts


def test_agent_context_zero_outbound(loaded, outbound_blocked):
    """真调端点：出站被钉死的情况下 200 + `{json, markdown}` 双形状照旧产生。

    查询串与 GUI 按钮发出去的**一字不差**（`entity_id=&depth=1`）——
    断言的必须是用户真的会打到的那条请求，不是一条更温和的变体。
    """
    entity_id = _with_observations(loaded)

    response = loaded.get(
        f"/api/v1/agent/context?entity_id={entity_id}&depth=1", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    # 双形状：JSON payload 与 Markdown 各自独立成键，缺一个都不是这个端点
    assert set(body) == {"json", "markdown"}
    assert body["json"]["entity"]["id"] == entity_id
    assert body["json"]["observations"], "上下文包里没有观测，形状对了但内容是空的"
    assert body["markdown"].startswith("# ")
    assert "## 观测" in body["markdown"]
    # 一次出站都没发生（有就会在 _blocked 里当场炸，这里再钉一道）
    assert outbound_blocked == [], f"端点运行期间试图出站：{outbound_blocked}"


def test_agent_context_zero_outbound_depth_default(loaded, outbound_blocked):
    """同一个端点、GUI 不传 depth 时的默认路径 —— 两半都不许伸手出去。"""
    entity_id = _with_observations(loaded)

    response = loaded.get(f"/api/v1/agent/context?entity_id={entity_id}",
                          headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert set(body) == {"json", "markdown"}
    assert outbound_blocked == []
