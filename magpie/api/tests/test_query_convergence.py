"""3.5 的出口断言：**CLI 一条命令与 GUI 给出的必须是同一个答案、同一份数据**。

§3.5 的验收标准就一句话：

    「哪些还在用 Nginx 1.18」—— CLI 一条命令与 GUI 服务镜头页给出的**必须是
    同一个答案、同一份数据**。

这句话在测试上的形状只能是**跨入口比对**：同一份库，一边走 HTTP（GUI 读的那
条路），一边走真子进程的 `magpie` 命令，然后逐条比 id。比"两边都调了
`core/query.py`"强的地方在于：前者验的是**结果**，后者验的是实现 —— 而实现
相同的两个入口在传参上分叉（拼错一个过滤条件、少带一个 limit）照样给出不同的
答案，且没有任何症状。

为什么断言里还要带上 `query.service_running(...)` 的**原始结果**：
CLI 与 API 各自都可能"一致地错"（比如两边都按 id 而不是 key 比）。所以第三条
腿上放数据层自己 —— 三个数字必须相等：GUI = CLI = core。

这个文件住在 `magpie/api/tests/` 而不是 core：core 的 R6 只给测试目录开了
pytest 的口子，import fastapi 会当场把那条红线弄红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app
from magpie.core import query, store

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "magpie" / "core" / "tests" / "fixtures"
SUBFINDER = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"
HTTPX = ROOT / "bench" / "e2e" / "recon" / "httpx.jsonl"
NMAP = FIXTURES / "nmap" / "1" / "input.xml"
NUCLEI = FIXTURES / "nuclei" / "1" / "input.jsonl"
DIRSEARCH = FIXTURES / "dirsearch" / "1" / "input.txt"

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

#: 子进程的公共环境。`PYTHONIOENCODING` 是必须的（Windows 上 `sys.executable`
#: 的 stdout 默认是 GBK）—— 与 `magpie/cli/tests/conftest.py` 同一条理由。
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


@pytest.fixture()
def surfaces(tmp_path):
    """**一个库，两个入口**：GUI 走 TestClient，CLI 走真子进程的 `magpie`。

    真的开子进程而不是进程内调 `magpie.cli.main`：CLI 这一侧的验收标准是
    "一条命令可以答"，而进程内调用恰好跳过了最容易坏的那几段（argv 解析、
    `--db` 的解析、退出码、信封）。这里要验的是**那条命令**给不给同一个答案。
    """
    db_path = tmp_path / "magpie.db"
    app = create_app(db_path=db_path, data_dir=tmp_path / "data", token=TOKEN)
    with TestClient(app) as client:
        client.post("/api/v1/projects", headers=HEADERS,
                    json={"name": "demo", "scope": {"include": ["*.example.com"],
                                                    "exclude": []}})
        response = client.post("/api/v1/imports", headers=HEADERS, json={
            "paths": [str(SUBFINDER), str(HTTPX), str(NMAP), str(NUCLEI),
                      str(DIRSEARCH)],
            "project": "demo"})
        import_id = response.json()["data"]["import_id"]
        deadline = time.monotonic() + 120
        record = None
        while time.monotonic() < deadline:
            record = client.app.state.ctx.imports.get(import_id)
            if record is not None and record.terminal:
                break
            time.sleep(0.02)
        assert record is not None and record.state == "done", f"夹具导入失败：{record}"
        yield client, db_path


def cli(db_path: Path, *args: str) -> dict:
    """跑**真的** `magpie` 命令，回它的 `data` 段。"""
    proc = subprocess.run(
        [sys.executable, "-m", "magpie.cli", *args, "--db", str(db_path), "--json"],
        cwd=str(ROOT), capture_output=True, env=ENV, timeout=120)
    assert proc.returncode == 0, (proc.returncode,
                                  proc.stderr.decode("utf-8", "replace"))
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["ok"] is True, payload
    return payload["data"]


def _ids(items) -> list:
    return sorted(e["id"] for e in items)


# ---------------------------------------------------------------------------
# 一条命令 vs 一个镜头页 —— 五个问题各问一遍
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question,http_params,cli_args", [
    # 「哪些还在用 Nginx 1.24.0」（§3.5 验收标准那一句）
    ("services", {"product": "nginx@1.24.0"}, ["ls", "--product", "nginx@1.24.0"]),
    ("services by version", {"product": "nginx", "version": "1.24.0"},
     ["ls", "--product", "nginx", "--version", "1.24.0"]),
    # 接口的归属服务
    ("endpoint", {"endpoint": "http://app.example.com:8080/user/123"},
     ["ls", "--endpoint", "http://app.example.com:8080/user/123"]),
    # 落在网段里的资产
    ("assets in cidr", {"in_cidr": "203.0.113.7/24"},
     ["ls", "--in-cidr", "203.0.113.7/24"]),
    # 高危漏洞
    ("critical vulns", {"kind": "vuln", "severity": "critical"},
     ["ls", "--kind", "vuln", "--severity", "critical"]),
    # 通用列表（老的那一条路也一起验，它同样只能有一份实现）
    ("all services", {"kind": "service"}, ["ls", "--kind", "service"]),
])
def test_cli_and_gui_answer_the_same_question(surfaces, question, http_params,
                                              cli_args):
    """同一组过滤条件：HTTP 与 CLI 回的**必须是同一批实体**。

    判据是 id 集合相等，而且**至少有一条** —— 一个双方都返回空列表的断言
    永远绿，它验的是"两边都没崩"，不是"两边答得一样"。
    """
    client, db_path = surfaces
    response = client.get("/api/v1/entities", headers=HEADERS, params=http_params)
    assert response.status_code == 200, response.text
    gui = _ids(response.json()["data"])
    assert gui, f"{question}：夹具上没有匹配的实体，这条断言什么都没验到"

    command = _ids(cli(db_path, *cli_args)["entities"])
    assert command == gui, f"{question}：CLI 与 GUI 的答案不一致"


def test_the_data_layer_agrees_with_both_entrances(surfaces):
    """第三条腿：数据层自己。

    CLI 与 API **各自**都可能"一致地错"（比如两边都用同一个错的过滤语义）。
    所以把 `query` 的原始结果也摆进来 —— 三个数字相等才算这一问收敛了。
    """
    client, db_path = surfaces
    conn = store.connect(db_path)
    try:
        project_id = query.project_id_for(conn, "demo")
        assert project_id is not None, "夹具里的项目没了"
        direct = query.services_running(conn, project_id, "Nginx", version="1.24.0")
    finally:
        conn.close()
    direct_ids = _ids(direct["entities"])
    assert direct_ids, "数据层自己都查不到 nginx@1.24.0 —— 夹具或身份规则出了问题"

    gui = _ids(client.get("/api/v1/entities", headers=HEADERS,
                          params={"product": "nginx@1.24.0"}).json()["data"])
    command = _ids(cli(db_path, "ls", "--product", "nginx@1.24.0")["entities"])
    assert direct_ids == gui == command


def test_lens_counts_are_the_same_number_on_both_sides(surfaces):
    """镜头页的类型徽标：GUI 的 `/lenses/{lens}` 与 `magpie lens <镜头>`。

    徽标上的数字与 CLI 数出来的不一样，是一个**没有人会立刻发现**的缺陷 ——
    直到有人照着徽标行动。
    """
    client, db_path = surfaces
    gui = client.get("/api/v1/lenses/services", headers=HEADERS).json()
    lens = cli(db_path, "lens", "services")
    assert {k["kind"]: k["count"] for k in gui["data"]["kinds"]} == \
        {k["kind"]: k["count"] for k in lens["kinds"]}
    assert gui["meta"]["total"] == sum(k["count"] for k in lens["kinds"])


def test_the_services_lens_page_shows_the_answer(surfaces):
    """**服务与中间件镜头的页面上真的能看到那个答案。**

    GUI 的镜头页（`LensView.vue`）就是这么取数的：按 `kind_registry.lens` 逐个
    kind 拉 `GET /entities?kind=…`，再从关系里看归属。所以这里把那条路原样走
    一遍，断言"在跑 nginx@1.24.0 的那几个服务"确实落在页面会渲染的那些行里 ——
    否则"两侧答案相同"只是在两个接口之间成立，而页面上看不到。

    ⚠️ 页面上的呈现仍是**逐行 + 点开看关系**（前端源码不在 3.5 的改动边界内），
    这一条验的是数据：页面拿到的行里包含答案的每一行。
    """
    client, db_path = surfaces
    kinds = client.get("/api/v1/lenses/services", headers=HEADERS).json()
    rows = []
    for entry in kinds["data"]["kinds"]:
        rows.extend(client.get("/api/v1/entities", headers=HEADERS,
                               params={"kind": entry["kind"], "limit": 500})
                    .json()["data"])
    page_ids = {row["id"] for row in rows}

    answer = client.get("/api/v1/entities", headers=HEADERS,
                        params={"product": "nginx@1.24.0"}).json()["data"]
    assert answer, "夹具上没有在跑 nginx@1.24.0 的服务"
    assert {e["id"] for e in answer} <= page_ids, \
        "答案里的实体不在服务镜头页会渲染的那些行里"

    # 页面上的行与 CLI 完全一致（同一批 id）
    command = cli(db_path, "ls", "--kind", "service", "--limit", "500")["entities"]
    assert _ids([r for r in rows if r["kind"] == "service"]) == _ids(command)
