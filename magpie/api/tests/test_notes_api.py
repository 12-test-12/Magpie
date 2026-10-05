"""0.21.0 · `POST /api/v1/notes` —— AI 发现通道的 HTTP 契约。

这一组用例与 `test_add_finding.py`（core 那一份）的分工：

  * core 管**语义**：零 work_journal、内容寻址防假冲突、undo/redo 逐行回去。
  * 这一份管**契约**：状态码（400/404）、错误码、响应形状、以及三件只有到了
    HTTP 这一层才成立的事 —— 写仍走单写队列、快速失败在排队之前、撤销走
    **作业历史里那条已有的 undo 路**（没有第二条路）。

为什么每条断言都指名一种误读：AI 自动渗透里这条通道是"AI 查到的东西落回页面"
的唯一入口，红了意味着页面会漏掉 AI 的发现，或者把失败的提交当成成功。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app

ROOT = Path(__file__).resolve().parents[3]
SUBFINDER = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"
HTTPX = ROOT / "bench" / "e2e" / "recon" / "httpx.jsonl"

TOKEN = "notes-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as c:
        c.post("/api/v1/projects", headers=HEADERS,
               json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        yield c


@pytest.fixture()
def loaded(client):
    """一个有真实数据的库：一次真导入，等到它跑完。"""
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


def _first_entity_id(client) -> int:
    response = client.get("/api/v1/entities?limit=5", headers=HEADERS)
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert rows, "夹具库里没有实体，后面都测不了"
    return int(rows[0]["id"])


def _observations(client, entity_id: int) -> list[dict]:
    detail = client.get(f"/api/v1/entities/{entity_id}", headers=HEADERS)
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]["observations"]


# ---------------------------------------------------------------------------
# 正路
# ---------------------------------------------------------------------------


def test_note_lands_and_is_visible_on_the_entity(loaded):
    """200 报告形状 + 页面链路：详情页必须看得到，origin=manual。"""
    entity_id = _first_entity_id(loaded)

    response = loaded.post("/api/v1/notes", headers=HEADERS,
                           json={"entity_id": entity_id,
                                 "text": "首页存在未鉴权的 /api/debug 端点"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["op"] == "ai_note.add"
    assert data["job_id"] and data["job_id"] > 0
    assert data["semantic_field"].startswith("ai_note:")
    assert data["source"] == "ai:add_finding"
    assert data["text_length"] > 0

    hit = [o for o in _observations(loaded, entity_id)
           if int(o["id"]) == int(data["observation_id"])]
    assert hit, "AI 发现没有出现在实体详情的观测列表里"
    assert hit[0]["origin"] == "manual", hit[0]


def test_note_job_is_undoable_through_the_existing_undo_route(loaded):
    """撤销走作业历史那条已有的 `POST /jobs/{id}/undo` —— 没有第二条路。"""
    entity_id = _first_entity_id(loaded)
    before = _observations(loaded, entity_id)

    posted = loaded.post("/api/v1/notes", headers=HEADERS,
                         json={"entity_id": entity_id, "text": "可撤销的发现"})
    assert posted.status_code == 200, posted.text
    job_id = posted.json()["data"]["job_id"]
    observation_id = int(posted.json()["data"]["observation_id"])
    assert len(_observations(loaded, entity_id)) == len(before) + 1

    job = loaded.get(f"/api/v1/jobs/{job_id}", headers=HEADERS)
    assert job.status_code == 200, job.text
    assert job.json()["data"]["job"]["kind"] == "ai_note"

    undone = loaded.post(f"/api/v1/jobs/{job_id}/undo", headers=HEADERS, json={})
    assert undone.status_code == 200, undone.text
    after = [o for o in _observations(loaded, entity_id)
             if int(o["id"]) == observation_id]
    assert not any(int(o.get("is_current", 1)) for o in after), (
        "撤销之后这条发现还在页面上 —— undo 对 ai_note job 失效了")


def test_note_dry_run_reports_but_writes_nothing(loaded):
    """干跑：报告照出、job_id=null、作业历史里没有多出任何 job。"""
    entity_id = _first_entity_id(loaded)
    before = _observations(loaded, entity_id)
    jobs_before = len(loaded.get("/api/v1/jobs?limit=200",
                                 headers=HEADERS).json()["data"])

    response = loaded.post("/api/v1/notes", headers=HEADERS,
                           json={"entity_id": entity_id, "text": "干跑的发现",
                                 "dry_run": True})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True and data["job_id"] is None
    assert data["op"] == "ai_note.add"

    assert _observations(loaded, entity_id) == before, "干跑往页面上写了东西"
    jobs_after = len(loaded.get("/api/v1/jobs?limit=200",
                                headers=HEADERS).json()["data"])
    assert jobs_after == jobs_before, "干跑在作业历史里留了一个 job"


# ---------------------------------------------------------------------------
# 反路（每个都指名一种会被误读的失败）
# ---------------------------------------------------------------------------


def test_note_empty_text_is_400_before_queueing(loaded):
    """空文本 = 用法错 400，且不占写者（快速失败在排队之前）。"""
    entity_id = _first_entity_id(loaded)
    before = _observations(loaded, entity_id)

    response = loaded.post("/api/v1/notes", headers=HEADERS,
                           json={"entity_id": entity_id, "text": "   \n  "})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "E_EMPTY_NOTE"
    assert _observations(loaded, entity_id) == before, "400 之后库被动了"


def test_note_unknown_entity_is_404(loaded):
    """实体不存在 = 数据错 404，一个字都不写（作业历史不多出任何 job）。"""
    jobs_before = len(loaded.get("/api/v1/jobs?limit=200",
                                 headers=HEADERS).json()["data"])

    response = loaded.post("/api/v1/notes", headers=HEADERS,
                           json={"entity_id": 999999, "text": "找不到的实体"})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_ENTITY_NOT_FOUND"
    jobs_after = len(loaded.get("/api/v1/jobs?limit=200",
                                headers=HEADERS).json()["data"])
    assert jobs_after == jobs_before, "404 的请求在作业历史里留了 job"
