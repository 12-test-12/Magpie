"""3.6 的出口断言：体检页的五类，**GUI 与 CLI 必须逐项相同**。

§3.5 把"同一份数据、两种答案"定成了这个项目最怕的一类缺陷，理由是它**没有症状**：
两边都跑得通，只是答得不一样，直到有人按错的那一半行动。体检页是这条风险最高的
一页 —— 它说的不是"某个实体存不存在"（那种分歧会被人一眼看穿），而是**"这个库
现在有多少件事等着你"**：一个数字错了，人不会去核对，他会据此判断"这里没事"。

所以这一页的验收不是"两边都不为空"，是**逐项相等**：五个 key、五个 count、
每一类下的 item id 列表，全部逐个比。比"两边都调了 `core/query.py`"强的地方在于
前者验的是**结果** —— 实现相同的两个入口照样会在传参上分叉（少带一个 project、
把 limit 拼错、拿 `state='open'` 去比 `state='resolved'`）。

第三条腿是**数据层自己**（`query.health_report` 的原始结果）：CLI 与 API 各自都
可能"一致地错"，三个数字相等才算这一页收敛了。

这个文件住在 `magpie/api/tests/` 而不是 core：core 的 R6 只给测试目录开了 pytest
的口子，import fastapi 会当场把那条红线弄红（与 `test_query_convergence.py` 同）。
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

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def httpx_row(host: str, *, webserver: str = "nginx/1.24.0", **extra) -> str:
    row = {"timestamp": "2026-01-01T00:00:00Z", "port": "443",
           "url": f"https://{host}", "input": host, "title": "T", "scheme": "https",
           "webserver": webserver, "content_type": "text/html",
           "status_code": 200, "tech": ["nginx"]}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


@pytest.fixture()
def surfaces(tmp_path):
    """**一个库，两个入口**：GUI 走 TestClient，CLI 走真子进程的 `magpie`。

    夹具刻意让四类同时非零（未知字段 / 越界资产 / 低置信度 / 冲突）—— 一个
    "五类都是 0"的库上，这条断言什么都不验（两边一致地什么都没算出来）。
    """
    files = {
        # 未知字段（brand_new_field）+ 越界资产（evil.other.net）
        "a.jsonl": "\n".join([httpx_row("app.example.com", brand_new_field="42"),
                              httpx_row("evil.other.net")]) + "\n",
        # 冲突：同一实体两个来源报了两个不同的值
        "b.jsonl": httpx_row("app.example.com", webserver="nginx/1.24.0") + "\n",
        "c.jsonl": httpx_row("app.example.com", webserver="apache/2.4.58") + "\n",
        # 低置信度：嗅探仲裁不采用任何解析器
        "dirty.txt": ("app.example.com\nnot a domain\n- bad\nlocalhost\n"
                      "also bad\napi.example.com\n"),
    }
    paths = []
    for name, text in files.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))

    db_path = tmp_path / "magpie.db"
    app = create_app(db_path=db_path, data_dir=tmp_path / "data", token=TOKEN)
    with TestClient(app) as client:
        client.post("/api/v1/projects", headers=HEADERS,
                    json={"name": "demo", "scope": {"include": ["*.example.com"],
                                                    "exclude": []}})
        response = client.post("/api/v1/imports", headers=HEADERS,
                               json={"paths": paths, "project": "demo"})
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
    proc = subprocess.run(
        [sys.executable, "-m", "magpie.cli", *args, "--db", str(db_path), "--json"],
        cwd=str(ROOT), capture_output=True, env=ENV, timeout=120)
    assert proc.returncode == 0, (proc.returncode,
                                  proc.stderr.decode("utf-8", "replace"))
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["ok"] is True, payload
    return payload["data"]


def shape(health: dict) -> list:
    """体检页的**可比形状**：五类各自的 key / count / kinds / item id。

    比之前先压成形状，是为了让失败信息说得出"哪一类、差多少"，而不是把两个
    几百行的 JSON 摆出来让人自己找不同。
    """
    return [(c["key"], c["label"], c["count"], sorted(c["kinds"]),
             [int(i["id"]) for i in c["items"]])
            for c in health["categories"]]


def test_cli_and_gui_report_the_same_five_numbers(surfaces):
    client, db_path = surfaces
    gui = client.get("/api/v1/doctor", headers=HEADERS,
                     params={"project": "demo"}).json()["data"]["health"]
    command = cli(db_path, "doctor", "--project", "demo")["health"]

    assert shape(gui) == shape(command), "GUI 与 CLI 的五类不一致"
    assert gui["open_inbox"] == command["open_inbox"]
    assert gui["claimed"] == command["claimed"]
    assert gui["unclaimed"] == command["unclaimed"]


def test_the_fixture_actually_exercises_more_than_one_category(surfaces):
    """先证明这条断言不是空转：夹具上至少三类非零，且总数不为 0。

    没有这一条，"两边都是 0"同样能让上面那条用例变绿 —— 那个绿什么都没验。
    """
    client, _ = surfaces
    health = client.get("/api/v1/doctor", headers=HEADERS,
                        params={"project": "demo"}).json()["data"]["health"]
    non_zero = {c["key"]: c["count"] for c in health["categories"] if c["count"]}
    assert len(non_zero) >= 3, f"夹具太干净，验不到东西：{non_zero}"
    assert health["claimed"] == health["open_inbox"]
    assert health["unclaimed"] == []


def test_the_data_layer_agrees_with_both_entrances(surfaces):
    """第三条腿：数据层自己。两个入口都可能"一致地错"。"""
    client, db_path = surfaces
    conn = store.connect(db_path)
    try:
        project_id = query.project_id_for(conn, "demo")
        assert project_id is not None, "夹具里的项目没了"
        direct = query.health_report(conn, project_id)
    finally:
        conn.close()

    gui = client.get("/api/v1/doctor", headers=HEADERS,
                     params={"project": "demo"}).json()["data"]["health"]
    command = cli(db_path, "doctor", "--project", "demo")["health"]
    assert shape(direct) == shape(gui) == shape(command)


def test_clearing_the_inbox_zeroes_all_five_on_both_entrances(surfaces):
    """**本步的验收判据，跨入口版本**：裁决掉全部待办，两边都必须归零。

    §7.4 的一句话是"清空 Inbox 即「干净」"。它在界面上的样子是：Inbox 页空了，
    体检页五类也全归零。这条用例走的正是那条路 —— 裁决走 `POST /inbox/{id}/resolve`
    （界面按的那个按钮），然后两个入口各查一遍。
    """
    client, db_path = surfaces
    items = client.get("/api/v1/inbox", headers=HEADERS,
                       params={"project": "demo", "state": "open"}).json()["data"]
    assert items, "夹具的 Inbox 是空的，这条断言什么都没验"
    for item in items:
        response = client.post(f"/api/v1/inbox/{item['id']}/resolve", headers=HEADERS,
                               json={"state": "resolved"})
        assert response.status_code == 200, response.text

    for health in (client.get("/api/v1/doctor", headers=HEADERS,
                              params={"project": "demo"}).json()["data"]["health"],
                   cli(db_path, "doctor", "--project", "demo")["health"]):
        assert {c["key"]: c["count"] for c in health["categories"]} == {
            c.key: 0 for c in query.HEALTH_CATEGORIES}
        assert health["open_inbox"] == 0
