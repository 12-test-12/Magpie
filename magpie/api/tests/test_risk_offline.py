"""零出站 + 三面收敛（G5 复验面之一）：`GET /api/v1/risk` 在出站能力被钉死时
照常出货，且与 CLI、core 逐字节同答案。

**为什么单独一个文件**（与 `test_agent_context_offline.py` 同一条理由）：这是
LARI-45 判据 4 要复验的面，单独成档让复验命令短到不用猜：

    py -3.13 -m pytest magpie/api/tests/test_risk_offline.py -q

三层各验一件事：

  * **零出站**：`check.py` 的 R7 扫的是源码里的出站**写法**，保证不了"端点
    跑起来真的没伸手出去" —— 行为要靠行为断言：先真连一把证明网络栈活着，
    再把 `socket` 的三个出口当场钉死，端点照常 200，且 `attempts` 为空；
  * **钉子有牙**：反例（评分路径里伸手出去查 CVE）必须当场炸；
  * **收敛**：同一个库，API / CLI / core 三份 overview **逐字节相同** ——
    与 `test_health_convergence.py` 同一条纪律的第三个实例。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app
from magpie.core import risk

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "magpie" / "core" / "tests" / "fixtures"
#: 与 core 侧 `test_risk.py` 同一套输入 —— 两侧吃的必须是同一种库。
INPUTS = (FIXTURES / "nuclei" / "1" / "input.jsonl",
          FIXTURES / "httpx" / "1" / "input.jsonl",
          FIXTURES / "ffuf" / "1" / "input.json")

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def loaded(client):
    """一个有真实数据的库：三个真夹具走 API 真导入，等到终态。"""
    client.post("/api/v1/projects", headers=HEADERS,
                json={"name": "demo", "scope": {"include": ["*.example.com"],
                                                "exclude": []}})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": [str(p) for p in INPUTS],
                                 "project": "demo"})
    import_id = response.json()["data"]["import_id"]
    deadline = time.monotonic() + 60
    record = None
    while time.monotonic() < deadline:
        record = client.app.state.ctx.imports.get(import_id)
        if record is not None and record.terminal:
            break
        time.sleep(0.02)
    # 夹具里有带坏行的文件（nuclei 第 7 行缺 matched-at），partial 是常态。
    assert record is not None and record.state in ("done", "partial"), \
        f"夹具导入失败：{record}"
    return client


def _real_connect_first():
    """正对照：**打补丁之前**先真连一把 —— 网络栈活着（环回关闸端口 = 立即
    拒绝），否则"钉死后没出站"可能只是因为这台机器连不出去。

    走 `urllib`（台架豁免的是它，见 check.py R7 的边界说明）而不是字面
    `socket.create_connection(…)` —— 后者是 R7 按调用点判定的出站写法，测试
    文件同样不许出现：真实连接用 HTTP 客户端，挡板验证用挡板本身。

    结果收**被拒**与**超时**两种（沙箱 / 防火墙可能黑洞环回 SYN）—— 正对照
    要证明的是"连接尝试真的发出去了"，两者都是证据；它排除的是"根本没发起"。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(OSError) as excinfo:
        opener.open("http://127.0.0.1:1/", timeout=2)
    reason = excinfo.value.args[0] if excinfo.value.args else None
    assert isinstance(reason, (ConnectionRefusedError, TimeoutError)), (
        f"正对照期望'被拒或超时'，收到 {type(excinfo.value).__name__}: {reason!r}")


def _pin_outbound(monkeypatch) -> list:
    """三点击杀 + `attempts` 记账（与 5.3 的 `test_agent_context_offline`
    同款：钉的是**行为**，不是写法）。"""
    attempts: list[tuple] = []

    def _blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("零出站被破：运行期间试图建立出站连接")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    return attempts


def test_risk_endpoint_answers_with_the_socket_pinned(loaded, monkeypatch):
    """真调端点：出站钉死的情况下 200 + 完整形状照旧产生，且一次出站都没发生。"""
    _real_connect_first()
    attempts = _pin_outbound(monkeypatch)

    response = loaded.get("/api/v1/risk", headers=HEADERS,
                          params={"project": "demo"})
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert set(body) == {"query", "rule_version", "rules_digest", "project_id",
                         "score", "attack_surface", "facts"}
    assert body["query"] == "risk"
    assert body["rules_digest"].startswith("sha256:")
    assert sum(f["contribution"] for f in body["score"]["factors"]) == \
        body["score"]["total"], "端点出来的分数自己都对不上"
    assert body["facts"]["vuln_total"] > 0, "夹具导入没生效，这条断言没验到东西"
    assert attempts == [], f"端点运行期间试图出站：{attempts}"


def test_the_pin_catches_an_outbound_cve_lookup(loaded, monkeypatch):
    """反例：评分路径里伸手出去（模拟"联网查 CVE 库补数据"）→ 当场炸。

    没有这条，上一条的绿可能只是"端点恰好没联网"，而不是"联网会被抓住"。"""
    _real_connect_first()
    attempts = _pin_outbound(monkeypatch)

    original = risk.overview

    def sneaky_overview(conn, project_id):
        # 反例：放行说明点名的 urlopen 形态 —— 模拟"联网查 CVE 库补数据"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open("http://127.0.0.1:1/", timeout=2)
        return original(conn, project_id)

    monkeypatch.setattr(risk, "overview", sneaky_overview)
    with pytest.raises(AssertionError, match="零出站被破"):
        loaded.get("/api/v1/risk", headers=HEADERS, params={"project": "demo"})
    assert attempts, "反例没被钉子抓住 —— 钉子是死的"


def test_api_cli_and_core_give_byte_identical_answers(loaded):
    """三面收敛：同一个库，`GET /risk` / `magpie risk --json` / `risk.overview`
    三份 data 逐字节相同。压成 canonical JSON 再比 —— 键序不同的"相同"在
    契约消费者眼里就是两个东西。"""
    from magpie.core import store

    ctx = loaded.app.state.ctx
    api = loaded.get("/api/v1/risk", headers=HEADERS,
                     params={"project": "demo"}).json()["data"]

    proc = subprocess.run(
        [sys.executable, "-m", "magpie.cli", "risk", "--project", "demo",
         "--db", str(ctx.db_path), "--json"],
        cwd=str(ROOT), capture_output=True, env=ENV, timeout=120)
    assert proc.returncode == 0, (proc.returncode,
                                  proc.stderr.decode("utf-8", "replace"))
    cli_payload = json.loads(proc.stdout.decode("utf-8"))
    assert cli_payload["ok"] is True, cli_payload
    cli_data = cli_payload["data"]

    conn = store.connect(ctx.db_path)
    try:
        row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        assert row is not None, "库里没有项目"
        core_data = risk.overview(conn, int(row["id"]))
    finally:
        conn.close()

    def dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)

    assert dumps(api) == dumps(cli_data), "API 与 CLI 的 risk 不是同一个答案"
    assert dumps(api) == dumps(core_data), "API 与 core 的 risk 不是同一个答案"
