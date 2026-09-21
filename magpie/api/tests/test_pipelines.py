"""编排模式的 API 面（3.7b · LARI-30）。

十条路由（3.7a §4.5 冻结的形状）从 501 变成真端点，这组用例是它们**会变红**的
那一半。四组断言，各守一件不同的事：

  1. **写定义只走单写队列**（R1）。一次 run 之后
     `writer.stats.concurrent_max == 1` 且执行过任务的线程只有一个 —— 与
     `test_writer.py` 同一套证据。写入口唯一的全部价值就是"任何时刻只有一个
     写者"，而它一旦被某条"顺手 UPDATE 一下"的路由破坏，没有别的测试会发现。
  2. **跑一次 = 既有的 ingest**。`SELECT kind FROM jobs WHERE pipeline_run_id` 全是
     `'ingest'`。批量跑不新开旁路 —— 它只是"按一条定义依次调同一个入口"。
  3. **保存-重开-复跑**（§4.5 的交付标准）：一条定义存下来 → 关掉库 → 重开 →
     仍在 → 再跑一次，**结果与第一次逐项相同**。
  4. **批量跑之后 undo 干净**：不留幻影行、不留待决 inbox 项。

另外两组边界：干跑**不写 `pipeline_runs` 行**（冻结 F10）、乐观锁不匹配 →
409（冻结 F2 的 `revision`）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app

ROOT = Path(__file__).resolve().parents[3]
E2E = ROOT / "bench" / "e2e"

TOKEN = "pipelines-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

#: 一条能真跑的 2 步定义（两个文件、两种选解析器的路径：显式指定 / 嗅探仲裁）。
DEFINITION = {
    "format_version": 1,
    "name": "常规一轮（域名 + 指纹）",
    "project": "demo",
    "base_dir": str(E2E),
    "scope": {"include": ["*.example.com"], "exclude": []},
    "steps": [
        {"id": "dns", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "source_name": "subfinder", "parser": "domainlist"},
        {"id": "probe", "source": {"kind": "glob", "pattern": "recon/*httpx*.jsonl"},
         "after": ["dns"]},
    ],
}


@pytest.fixture()
def client(tmp_path):
    """一个建好项目、还没有任何定义的库。"""
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as c:
        response = c.post("/api/v1/projects", headers=HEADERS,
                          json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        assert response.status_code == 201, response.text
        yield c


def _create(client, definition: dict) -> int:
    response = client.post("/api/v1/pipelines", headers=HEADERS, json=definition)
    assert response.status_code == 201, response.text
    return int(response.json()["data"]["id"])


def _wait_run(client, run_id: int, timeout: float = 120) -> dict:
    """等一次真跑落地。**轮询的是 run 的 state**，不是"睡一会儿"。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
        assert body["ok"] is True, body
        if body["data"]["state"] != "running":
            return body["data"]
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} 在 {timeout}s 内没有结束")


def _run(client, pipeline_id: int, **body) -> dict:
    response = client.post(f"/api/v1/pipelines/{pipeline_id}/run", headers=HEADERS,
                           json=body)
    assert response.status_code == 202, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# 定义：建 / 列表 / 详情 / 保存 / 归档
# ---------------------------------------------------------------------------


def test_definition_crud_and_optimistic_lock(client):
    """建 → 列表 → 详情 → 保存（revision+1）→ 拿旧 revision 再存 → 409。"""
    pipeline_id = _create(client, DEFINITION)

    listed = client.get("/api/v1/pipelines", headers=HEADERS).json()["data"]
    assert [item["id"] for item in listed] == [pipeline_id]
    assert listed[0]["step_count"] == 2, "列表要报步骤数（它只存在于定义 JSON 里）"
    assert listed[0]["last_run"] is None

    detail = client.get(f"/api/v1/pipelines/{pipeline_id}",
                        headers=HEADERS).json()["data"]
    assert detail["revision"] == 1
    assert detail["definition"]["steps"][0]["source"] == {
        "kind": "glob", "pattern": "recon/subfinder.txt"}
    assert detail["definition"]["steps"][0]["source_name"] == "subfinder"

    body = dict(detail["definition"], revision=detail["revision"], name="改过名字")
    saved = client.put(f"/api/v1/pipelines/{pipeline_id}", headers=HEADERS,
                       json=body).json()["data"]
    assert saved["revision"] == 2 and saved["name"] == "改过名字"

    stale = client.put(f"/api/v1/pipelines/{pipeline_id}", headers=HEADERS, json=body)
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "E_PIPELINE_CONFLICT"


def test_saving_a_definition_creates_no_job(client):
    """**定义不是事实**（冻结 F4）：存一条定义不建 job、不产生贡献。

    这一条在 API 层再验一次，是因为"顺手在保存时建一个 job 记录一下"是那种
    看起来无害、事后极难查的改动 —— 它会让"撤到哪"同时有两个答案。
    """
    ctx = client.app.state.ctx
    _create(client, DEFINITION)
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM activity_log ORDER BY id")]
        assert actions.count("pipeline.save") == 1, actions
    finally:
        conn.close()


def test_archived_definitions_leave_the_list_but_keep_their_history(client):
    """归档是**软删**：它离开列表，但 `GET /pipelines/{id}` 仍然答得出来 ——
    已经跑过的 run 靠 `pipeline_id` 指向它，"这次跑的是哪一版"必须一直有答案。
    """
    pipeline_id = _create(client, DEFINITION)
    run = _run(client, pipeline_id)
    _wait_run(client, run["run_id"])

    response = client.post(f"/api/v1/pipelines/{pipeline_id}/archive",
                           headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["archived_at"] is not None

    assert client.get("/api/v1/pipelines", headers=HEADERS).json()["data"] == []
    still_there = client.get(f"/api/v1/pipelines/{pipeline_id}", headers=HEADERS)
    assert still_there.status_code == 200, still_there.text
    runs = client.get(f"/api/v1/pipelines/{pipeline_id}/runs",
                      headers=HEADERS).json()["data"]
    assert [item["id"] for item in runs] == [run["run_id"]]


def test_pipeline_errors_are_named_not_empty(client):
    """不存在的定义 / run 必须**响亮**：404 + 具名 code，而不是空数组。"""
    missing = client.get("/api/v1/pipelines/999", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "E_PIPELINE_NOT_FOUND"
    assert client.get("/api/v1/runs/999", headers=HEADERS).json()["error"]["code"] \
        == "E_PIPELINE_RUN_NOT_FOUND"
    nameless = client.post("/api/v1/pipelines", headers=HEADERS,
                           json={**DEFINITION, "name": ""})
    assert nameless.status_code == 400
    assert nameless.json()["error"]["code"] == "E_PIPELINE_BAD_DEFINITION"


# ---------------------------------------------------------------------------
# 跑：真跑 / 干跑 / 单写队列
# ---------------------------------------------------------------------------


def test_a4_a_run_only_creates_ingest_jobs_and_they_go_through_the_single_write_queue(
        client):
    """**A4** + **R1**：批量跑挂着的 job 全部是 `'ingest'`，且整次 run 只经过
    一个写线程（`concurrent_max == 1`、执行过任务的线程 id 只有一个）。

    证据取自 `WriteQueue.stats`（与 `test_writer.py` 同一套），而不是"看起来
    没问题"。这两条一起说明"批量跑**复用**既有 ingest，没有新开旁路"。
    """
    ctx = client.app.state.ctx
    pipeline_id = _create(client, DEFINITION)
    run = _run(client, pipeline_id)
    data = _wait_run(client, run["run_id"])

    conn = sqlite3.connect(str(ctx.db_path))
    try:
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM jobs WHERE pipeline_run_id=?", (run["run_id"],))]
        assert kinds and set(kinds) == {"ingest"}, kinds
        assert sorted(int(j) for j in data["job_ids"]) == sorted(
            int(r[0]) for r in conn.execute(
                "SELECT id FROM jobs WHERE pipeline_run_id=?", (run["run_id"],)))
    finally:
        conn.close()

    stats = ctx.writer.stats.as_dict()
    assert stats["concurrent_max"] == 1, stats
    assert len(stats["thread_ids"]) == 1, stats


def test_dry_run_is_synchronous_and_leaves_no_run_row(client):
    """冻结 F10：干跑**不写 `pipeline_runs` 行**，报告同步返回。

    它是"预览"，而一份"预演发生过"的持久记录不是事实。这也是那一列上没有
    `dry_run` 的原因 —— 一列永远等于 0 的数字会被读成"查过了，没有干跑"。
    """
    ctx = client.app.state.ctx
    pipeline_id = _create(client, DEFINITION)
    response = client.post(f"/api/v1/pipelines/{pipeline_id}/run", headers=HEADERS,
                           json={"dry_run": True})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["run_id"] is None
    assert data["state"] == "done"
    assert data["report"]["totals"]["added"] > 0

    conn = sqlite3.connect(str(ctx.db_path))
    try:
        for table in ("pipeline_runs", "jobs", "contributions"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"干跑之后 {table} 有 {count} 行 —— 那不是干跑"
    finally:
        conn.close()


def test_dry_run_and_real_run_agree_field_by_field(client):
    """**A5**：干跑与真跑的 `totals` 逐字段相等（同一个库状态、同一份定义）。

    一个会说假话的干跑比没有干跑更糟 —— 人会照着它的数做判断。
    """
    pipeline_id = _create(client, DEFINITION)
    dry = client.post(f"/api/v1/pipelines/{pipeline_id}/run", headers=HEADERS,
                      json={"dry_run": True}).json()["data"]["report"]["totals"]
    run = _run(client, pipeline_id)
    real = _wait_run(client, run["run_id"])["report"]["totals"]
    assert dry == real, (dry, real)


def test_run_progress_is_published_on_the_broker(client):
    """进度走 SSE，且**复用 `EventBroker`**（流 id 是 `run-{id}`）。

    断言的是总线上的事实，不是"订阅之后看到了什么"：`subscribe` 会**回放历史**，
    所以即使这里订阅得晚，前面的事件也必须在。带前缀是为了让人一眼看出它**不是**
    job_id（与 `imp-1` 同一条纪律）。
    """
    ctx = client.app.state.ctx
    pipeline_id = _create(client, DEFINITION)
    run = _run(client, pipeline_id)
    _wait_run(client, run["run_id"])

    events = ctx.broker.replay(f"run-{run['run_id']}")
    assert events, "总线上一件事都没有 —— 那界面上的进度条是从哪来的？"
    types = [event.type for event in events]
    assert types[0] == "queued" and types[-1] == "done", types
    assert "job_done" in types, types
    assert ctx.broker.is_terminal(f"run-{run['run_id']}") is True


# ---------------------------------------------------------------------------
# 保存-重开-复跑 / 批量跑之后 undo 干净
# ---------------------------------------------------------------------------


def test_save_reopen_rerun_gives_the_same_result(client, tmp_path):
    """**§4.5 的交付标准**：存下来 → 关掉 → 重开 → 仍在 → 再跑一次，
    **结果与第一次逐项相同**。

    "关掉重开"在这里是真的：另起一个 app 实例指向同一个 db 文件。在同一个
    app 里再调一次 `GET` 测不到"持久化" —— 那测的是内存里那份还在不在。

    第二次跑的数字里 `skipped` 会涨：幂等来自 2.5 已有的那条（同一 sha256 +
    同一 parser_fingerprint → 跳过），定义层**不实现**缓存。所以判据是
    "第二次的 `job_ids` 数量与第一次相同、且没有一行 failed/needs_confirmation"，
    不是"totals 逐字段相同"—— 后者会给出一条错误的期望。
    """
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as first:
        first.post("/api/v1/projects", headers=HEADERS,
                   json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        pipeline_id = _create(first, DEFINITION)
        run = _run(first, pipeline_id)
        before = _wait_run(first, run["run_id"])

    assert before["state"] == "done"
    assert before["report"]["totals"]["added"] > 0

    # 「关掉重开」——同一个库文件，全新的服务实例。
    app2 = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                      token=TOKEN)
    with TestClient(app2) as second:
        listed = second.get("/api/v1/pipelines", headers=HEADERS).json()["data"]
        assert [item["id"] for item in listed] == [pipeline_id], "定义没活过重启"
        assert listed[0]["definition_hash"] == \
            first.get(f"/api/v1/pipelines/{pipeline_id}",
                      headers=HEADERS).json()["data"]["definition_hash"]

        again = _run(second, pipeline_id)
        after = _wait_run(second, again["run_id"])
        assert after["state"] == "done", after
        assert len(after["job_ids"]) == len(before["job_ids"]), \
            "同一份定义、同一批文件，第二次的 job 数必须一样"
        states = [f["state"] for step in after["report"]["steps"]
                  for f in step["files"]]
        assert set(states) == {"skipped"}, states
        assert after["report"]["totals"]["added"] == 0, "第二次不该再新增事实"
        # 幂等跳过 ≠ 什么都没发生：它是**同一个 sha256 + 同一个指纹**的答案，
        # 而那个答案只有在库里真有那批事实时才成立。
        assert second.get("/api/v1/pipelines", headers=HEADERS
                          ).json()["data"][0]["last_run"]["id"] == again["run_id"]


def test_undo_run_leaves_no_phantom_rows_and_no_pending_inbox(client):
    """**批量跑之后 undo 干净**（验收标准 · 口径照 2.6 的 B 组：逐行指纹相等）。

    三个判据一起看，缺一个都不算"干净"：

      * 库里没有任何**活跃贡献**（事实层真的撤了，不是只把 `applied` 翻了个面）；
      * `v_integrity_violations` 里没有 `phantom_cache_row`（不留幻影行）；
      * 没有**待决的 inbox 项**（不留待人工裁决的东西）。
    """
    ctx = client.app.state.ctx
    pipeline_id = _create(client, DEFINITION)
    run = _run(client, pipeline_id)
    _wait_run(client, run["run_id"])

    conn = sqlite3.connect(str(ctx.db_path))
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM v_active_contributions").fetchone()[0]
        assert before > 0, "前提不成立：这次跑没落任何活跃贡献"

        dry = client.post(f"/api/v1/runs/{run['run_id']}/undo", headers=HEADERS,
                          json={"dry_run": True}).json()["data"]
        assert conn.execute("SELECT COUNT(*) FROM v_active_contributions"
                            ).fetchone()[0] == before, \
            "干跑撤销改了数据 —— 那不是干跑"
        assert dry["undone"], dry

        done = client.post(f"/api/v1/runs/{run['run_id']}/undo", headers=HEADERS,
                           json={}).json()["data"]
        assert done["undone"] == dry["undone"], "干跑与真跑撤的不是同一批"
        assert done["applied"] == 0

        assert conn.execute("SELECT COUNT(*) FROM v_active_contributions"
                            ).fetchone()[0] == 0
        phantoms = [r[0] for r in conn.execute(
            "SELECT ref FROM v_integrity_violations WHERE violation='phantom_cache_row'")]
        assert phantoms == [], phantoms
        open_inbox = [dict(r) for r in conn.execute(
            "SELECT kind, subject_kind FROM inbox_items WHERE state='open'")]
        assert open_inbox == [], f"批量跑撤销之后还留着待决的 inbox 项：{open_inbox}"
    finally:
        conn.close()


def test_redo_run_restores_exactly_what_undo_took_away(client):
    """`redo_run` 把整批放回去，且与撤销前**逐行相同**。

    判据是 `/export` 的逐字节比较（与 `test_jobs.py` 的 B 组同一套）——
    条数相同、内容不同是完全可能的。
    """
    pipeline_id = _create(client, DEFINITION)
    run = _run(client, pipeline_id)
    _wait_run(client, run["run_id"])

    def export() -> str:
        return json.dumps(client.get("/api/v1/export?format=json",
                                     headers=HEADERS).json()["data"],
                          sort_keys=True, ensure_ascii=False)

    before = export()
    client.post(f"/api/v1/runs/{run['run_id']}/undo", headers=HEADERS, json={})
    assert export() != before, "撤销之后派生数据必须变了，否则撤销什么都没干"
    redone = client.post(f"/api/v1/runs/{run['run_id']}/redo", headers=HEADERS,
                         json={}).json()["data"]
    assert redone["applied"] == 1
    assert export() == before, "redo 之后没有逐字节复原"
