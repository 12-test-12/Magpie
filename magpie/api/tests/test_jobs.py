"""作业历史 + undo / redo（2.8 的三个用户入口）。

**这组测试是 G2 出口 Gate 第 ② 条的自动化形式**：计划书要求"导入 → undo → redo
精确复原"能手工验收。手工验收的问题是它只发生一次；这里的 `test_redo_restores_*`
把同一件事变成每次 `check.py` 都会跑一遍的断言。

三条断言，各自守一件不同的事:

  * **干跑与真跑同构但不落库** —— 界面上的"预览"按钮就是这个端点。干跑若落了库，
    "预览"就成了一次静默的真跑；若输出形状与真跑不同，界面就得维护两套渲染。
  * **undo 真的撤销了** —— 用**读端点**（`/export`、`/entities`）验证，而不是
    读 `jobs.applied`。读 applied 只是把写入的值再读回来；读派生物才证明效果
    真的到达了缓存层。
  * **redo 精确复原** —— 与 undo 之前的 `/export` 逐字节比较。这就是
    "精确互逆"里"精确"二字的全部含义。
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

TOKEN = "jobs-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def loaded(tmp_path):
    """一个有真实数据、且导入已经跑完的库。"""
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as client:
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
        client.import_id = import_id                     # 供用例取用
        yield client


def _job_ids(client) -> list[int]:
    body = client.get("/api/v1/jobs", headers=HEADERS).json()
    assert body["ok"] is True
    ids = [item["id"] for item in body["data"]]
    assert ids, "导入之后一条作业都没有 —— 作业历史是空的"
    return ids


def _export(client) -> list:
    """整个项目的**派生物**快照。undo / redo 的"精确"就是与它比。"""
    return client.get("/api/v1/export?format=json", headers=HEADERS).json()["data"]


def _post(client, path: str, **body):
    response = client.post(f"/api/v1{path}", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------


def test_jobs_list_is_newest_first_with_counts(loaded):
    body = loaded.get("/api/v1/jobs", headers=HEADERS).json()
    ids = [item["id"] for item in body["data"]]
    assert ids == sorted(ids, reverse=True), "作业历史不是最近的在前"
    for item in body["data"]:
        # 界面按钮的可用性必须由**数据**决定，不由它自己猜状态
        assert item["can_undo"] is not item["can_redo"]
        assert set(item["counts"]) == {"contributions", "retracted"}
        assert item["counts"]["contributions"] > 0
        assert item["counts"]["retracted"] == 0


def test_job_detail_carries_explain_job_scope(loaded):
    """旧 job 必须用**当时的** scope 解释 —— 界面不自己拿当前 scope 去算。"""
    job_id = _job_ids(loaded)[0]
    data = loaded.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()["data"]
    scope = data["scope"]
    assert scope["job_id"] == job_id
    assert scope["interpret_with"] in ("historical_scope", "current_scope")
    assert set(scope) >= {"job_scope_version", "current_scope_version",
                          "scope_versions_behind", "parser_drift",
                          "coverage_usable_for_inactive"}
    assert data["job"]["id"] == job_id
    assert data["activity"], "作业详情里没有任何活动记录"


def test_unknown_job_is_404(loaded):
    response = loaded.get("/api/v1/jobs/999999", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E_JOB_NOT_FOUND"


def test_bad_job_filter_is_rejected(loaded):
    response = loaded.get("/api/v1/jobs?state=nonsense", headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_BAD_STATE"


# ---------------------------------------------------------------------------
# 干跑
# ---------------------------------------------------------------------------


def test_dry_run_undo_reports_but_changes_nothing(loaded):
    """干跑与真跑**输出同构**，只有 COMMIT → ROLLBACK 之差。"""
    job_id = _job_ids(loaded)[0]
    before = _export(loaded)

    preview = _post(loaded, f"/jobs/{job_id}/undo", dry_run=True)
    assert preview["dry_run"] is True
    assert preview["report"]["impact"], "干跑的报告里没有 impact —— 那界面预览什么？"
    assert _export(loaded) == before, "干跑改了数据 —— 那不是干跑"
    assert loaded.get(f"/api/v1/jobs/{job_id}",
                      headers=HEADERS).json()["data"]["job"]["applied"] is True

    real = _post(loaded, f"/jobs/{job_id}/undo", dry_run=False)
    assert real["dry_run"] is False
    # 同一批 key —— 界面可以用同一段代码渲染两者
    assert set(real["report"]) == set(preview["report"])
    assert _export(loaded) != before, "真跑没有撤销任何东西"


def test_dry_run_redo_reports_but_changes_nothing(loaded):
    job_id = _job_ids(loaded)[0]
    _post(loaded, f"/jobs/{job_id}/undo")
    undone = _export(loaded)

    preview = _post(loaded, f"/jobs/{job_id}/redo", dry_run=True)
    assert preview["report"]["impact"]
    assert _export(loaded) == undone, "干跑改了数据 —— 那不是干跑"

    real = _post(loaded, f"/jobs/{job_id}/redo")
    assert set(real["report"]) == set(preview["report"])
    assert _export(loaded) != undone


# ---------------------------------------------------------------------------
# 闭环：导入 → undo → redo 精确复原（G2 出口 Gate 第 ② 条）
# ---------------------------------------------------------------------------


def test_undo_then_redo_restores_every_job_exactly(loaded):
    original = _export(loaded)
    assert original, "导入之后没有任何实体，测不出复原"

    for job_id in _job_ids(loaded):
        undone = _post(loaded, f"/jobs/{job_id}/undo")
        assert undone["report"]["impact"], f"job {job_id} 的 undo 报告是空的"

    after_undo = _export(loaded)
    assert all(not entity["fields"] for entity in after_undo), \
        "undo 之后还有观测 —— 撤销没有真正到达派生物"

    for job_id in _job_ids(loaded):
        redone = _post(loaded, f"/jobs/{job_id}/redo")
        assert redone["report"]["impact"], f"job {job_id} 的 redo 报告是空的"

    # **精确**复原：不只是"实体数一样"，而是逐字节一致
    assert _export(loaded) == original


def test_import_undo_endpoint_undoes_the_whole_batch(loaded):
    """2.7 就有的批量 undo 也要仍然可用 —— 2.8 的界面同时用这两条路。"""
    job_ids = _job_ids(loaded)
    original = _export(loaded)
    data = _post(loaded, f"/imports/{loaded.import_id}/undo")
    assert data["undone"] == sorted(job_ids, reverse=True)
    assert _export(loaded) != original

    for job_id in job_ids:
        _post(loaded, f"/jobs/{job_id}/redo")
    assert _export(loaded) == original
