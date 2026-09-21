"""3.8 的 API 面：批量改状态 / 标签词表 / 合并与拆开 / 布局的部分写入。

这组用例与 `test_work_layer.py`（core 那一份）的分工：

  * core 那一份管**语义**：一个 job、幂等、全成或全不成、逐行指纹、重导不拆开。
  * 这一份管**契约**：状态码、错误码、响应形状、以及三件只有到了 HTTP 这一层
    才成立的事 —— 写仍走单写队列、"快速失败在排队之前"、老客户端的请求形状
    仍然被接受。

第三条要单独说：`PATCH /graph/layout` 这次把三个字段都改成可选的（部分写入），
**响应形状逐字不变**（`{"stored": N}`）。所以这里既验新形状（只送 collapsed），
也验老形状（三个都送、照旧生效）—— 一条"只验新写法"的用例会在下一次有人
删掉老分支时全绿。
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

TOKEN = "work-layer-token"
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


def _entities(client, **params) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get(f"/api/v1/entities?limit=200{('&' + query) if query else ''}",
                          headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# 批量改状态
# ---------------------------------------------------------------------------


def test_batch_lifecycle_changes_a_whole_batch_in_one_job(loaded):
    """响应里那个 `job_id` 是**一个** job —— 批量的原子性就落在这个形状上。"""
    ids = [int(e["id"]) for e in _entities(loaded)[:3]]
    assert len(ids) == 3

    response = loaded.post("/api/v1/entities/lifecycle", headers=HEADERS,
                           json={"entity_ids": ids, "lifecycle": "retired",
                                 "reason": "季度清理"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["counts"] == {"requested": 3, "changed": 3, "unchanged": 0}
    assert data["job_id"]

    for entity_id in ids:
        detail = loaded.get(f"/api/v1/entities/{entity_id}", headers=HEADERS).json()
        assert detail["data"]["entity"]["lifecycle"] == "retired"


def test_batch_lifecycle_is_undone_by_the_same_job_endpoint(loaded):
    """撤销走**作业历史那个已有的按钮**（`POST /jobs/{id}/undo`）—— 没有第二条路。"""
    ids = [int(e["id"]) for e in _entities(loaded)[:2]]
    before = {i: loaded.get(f"/api/v1/entities/{i}",
                            headers=HEADERS).json()["data"]["entity"]["lifecycle"]
              for i in ids}

    posted = loaded.post("/api/v1/entities/lifecycle", headers=HEADERS,
                         json={"entity_ids": ids, "lifecycle": "retired"}).json()
    job_id = posted["data"]["job_id"]

    undone = loaded.post(f"/api/v1/jobs/{job_id}/undo", headers=HEADERS, json={})
    assert undone.status_code == 200, undone.text
    assert undone.json()["data"]["report"]["impact"]["work_reverted"] == 2, (
        "撤销报告必须说出**它真的放回去了几个**，否则界面上这次撤销看起来像个 no-op")
    for entity_id in ids:
        detail = loaded.get(f"/api/v1/entities/{entity_id}", headers=HEADERS).json()
        assert detail["data"]["entity"]["lifecycle"] == before[entity_id]


def test_batch_lifecycle_rejects_unknown_entity_before_queueing(loaded):
    """快速失败：注定失败的批量**不占一次写者**，也在写任何东西之前就报。"""
    ids = [int(e["id"]) for e in _entities(loaded)[:1]]
    response = loaded.post("/api/v1/entities/lifecycle", headers=HEADERS,
                           json={"entity_ids": ids + [999999], "lifecycle": "retired"})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_ENTITY_NOT_FOUND"
    # 好的那个也一个字没改
    assert loaded.get(f"/api/v1/entities/{ids[0]}",
                      headers=HEADERS).json()["data"]["entity"]["lifecycle"] == "active"


@pytest.mark.parametrize("body,code", [
    ({"entity_ids": [1], "lifecycle": "已退休"}, "E_BAD_LIFECYCLE"),
    ({"entity_ids": [], "lifecycle": "retired"}, "E_EMPTY_PATCH"),
])
def test_batch_lifecycle_rejects_a_bad_body(loaded, body, code):
    response = loaded.post("/api/v1/entities/lifecycle", headers=HEADERS, json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code


def test_batch_lifecycle_dry_run_leaves_nothing(loaded):
    ids = [int(e["id"]) for e in _entities(loaded)[:2]]
    before = {i: loaded.get(f"/api/v1/entities/{i}",
                            headers=HEADERS).json()["data"]["entity"]["lifecycle"]
              for i in ids}
    jobs_before = loaded.get("/api/v1/jobs?limit=200",
                             headers=HEADERS).json()["meta"]["count"]

    response = loaded.post("/api/v1/entities/lifecycle", headers=HEADERS,
                           json={"entity_ids": ids, "lifecycle": "retired",
                                 "dry_run": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["counts"]["changed"] == len(ids)
    for entity_id in ids:
        assert loaded.get(f"/api/v1/entities/{entity_id}",
                          headers=HEADERS).json()["data"]["entity"]["lifecycle"] \
            == before[entity_id]
    assert loaded.get("/api/v1/jobs?limit=200",
                      headers=HEADERS).json()["meta"]["count"] == jobs_before


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


def test_tags_vocabulary_counts_live_tags_only(loaded):
    entity_id = _entities(loaded)[0]["id"]
    for tag in ("外部资产", "重点"):
        assert loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                            json={"add_tags": [tag]}).status_code == 200

    vocab = {row["tag"]: row["count"]
             for row in loaded.get("/api/v1/tags", headers=HEADERS).json()["data"]}
    assert vocab["外部资产"] == 1 and vocab["重点"] == 1

    loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                 json={"remove_tags": ["重点"], "reason": "看错了"})
    vocab = {row["tag"]: row["count"]
             for row in loaded.get("/api/v1/tags", headers=HEADERS).json()["data"]}
    assert "重点" not in vocab, "摘掉的词不该继续挂在词表上"
    assert vocab["外部资产"] == 1


def test_tags_vocabulary_matches_the_entity_filter(loaded):
    """词表上的数 == `?tag=` 过滤出来的数 —— 两个数说的是同一件事。"""
    for entity in _entities(loaded)[:2]:
        loaded.patch(f"/api/v1/entities/{entity['id']}", headers=HEADERS,
                     json={"add_tags": ["外部资产"]})

    vocab = {row["tag"]: row["count"]
             for row in loaded.get("/api/v1/tags", headers=HEADERS).json()["data"]}
    page = loaded.get("/api/v1/entities?tag=外部资产&limit=200", headers=HEADERS).json()
    assert vocab["外部资产"] == page["meta"]["count"] == 2


# ---------------------------------------------------------------------------
# 合并 / 拆开
# ---------------------------------------------------------------------------


def _two_domains(client) -> tuple[dict, dict]:
    rows = _entities(client, kind="domain")
    assert len(rows) >= 2, f"夹具里域名不足两个：{[r['entity_key'] for r in rows]}"
    return rows[0], rows[1]


def test_merge_then_unmerge_is_visible_and_reversible(loaded):
    survivor, member = _two_domains(loaded)
    merged = loaded.post(f"/api/v1/entities/{survivor['id']}/merge", headers=HEADERS,
                         json={"survivor_key": survivor["entity_key"],
                               "member_keys": [member["entity_key"]]})
    assert merged.status_code == 200, merged.text
    event_id = merged.json()["data"]["event_id"]

    # 成员从图上消失，幸存者的详情里**说得出来**它合并过谁（unmerge 的入口）
    keys = {n["entity_key"] for n in
            loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["nodes"]}
    assert member["entity_key"] not in keys

    events = loaded.get(f"/api/v1/entities/{survivor['id']}/merges",
                        headers=HEADERS).json()["data"]
    assert [e["event_id"] for e in events] == [event_id]
    assert member["entity_key"] in events[0]["member_keys"]

    split = loaded.post(f"/api/v1/entities/{survivor['id']}/unmerge", headers=HEADERS,
                        json={"event_id": event_id})
    assert split.status_code == 200, split.text
    keys = {n["entity_key"] for n in
            loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["nodes"]}
    assert member["entity_key"] in keys
    # 拆开之后那条事件不再列出来 —— "列出来但点了没反应"比不列更坏
    assert loaded.get(f"/api/v1/entities/{survivor['id']}/merges",
                      headers=HEADERS).json()["data"] == []


def test_unmerge_refuses_an_event_the_entity_did_not_take_part_in(loaded):
    """路径上的实体只是**入口**：它必须真的参与过这个事件。

    不做这个核对的话，`POST /entities/1/unmerge` 就成了"撤销任意一次合并"的
    后门，而它读起来像是在拆实体 1。
    """
    survivor, member = _two_domains(loaded)
    third = _entities(loaded, kind="domain")[2]
    merged = loaded.post(f"/api/v1/entities/{survivor['id']}/merge", headers=HEADERS,
                         json={"survivor_key": survivor["entity_key"],
                               "member_keys": [member["entity_key"]]}).json()
    event_id = merged["data"]["event_id"]

    response = loaded.post(f"/api/v1/entities/{third['id']}/unmerge", headers=HEADERS,
                           json={"event_id": event_id})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_MERGE_EVENT_NOT_FOUND"


def test_merge_survivor_key_must_match_the_path_entity(loaded):
    survivor, member = _two_domains(loaded)
    response = loaded.post(f"/api/v1/entities/{survivor['id']}/merge", headers=HEADERS,
                           json={"survivor_key": member["entity_key"],
                                 "member_keys": [survivor["entity_key"]]})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "E_MERGE_SURVIVOR_MISMATCH"


# ---------------------------------------------------------------------------
# 布局：部分写入
# ---------------------------------------------------------------------------


def test_layout_touch_one_column_does_not_reset_the_other(loaded):
    """**这一条就是"两列互不串位"的契约形式。**

    3.8 之前这里是整行覆盖写：只送 `x` / `y`（拖拽的做法）会把 `collapsed`
    一起重置成 false，于是"拖一下已经折叠的节点"悄悄把它展开了。
    """
    entity_id = int(_entities(loaded)[0]["id"])
    created = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                           json={"items": [{"entity_id": entity_id, "x": 10, "y": 20,
                                            "collapsed": True}]})
    assert created.status_code == 200 and created.json()["data"] == {"stored": 1}

    # 拖位置：只送坐标
    dragged = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                           json={"items": [{"entity_id": entity_id, "x": 99, "y": 88}]})
    assert dragged.status_code == 200 and dragged.json()["data"] == {"stored": 1}
    layout = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["layout"]
    assert layout[str(entity_id)] == {"x": 99, "y": 88, "collapsed": True}, (
        "拖位置把折叠状态一起改掉了 —— 两列串位")

    # 切折叠：只送 collapsed
    folded = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                          json={"items": [{"entity_id": entity_id, "collapsed": False}]})
    assert folded.status_code == 200
    layout = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["layout"]
    assert layout[str(entity_id)] == {"x": 99, "y": 88, "collapsed": False}, (
        "切折叠把坐标一起改掉了 —— 两列串位")


def test_layout_still_accepts_the_old_full_row_shape(loaded):
    """老客户端三个字段都送，行为**逐字不变** —— 这是那次改动没有破坏契约的证据。"""
    entity_id = int(_entities(loaded)[0]["id"])
    response = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                            json={"items": [{"entity_id": entity_id, "x": 1, "y": 2,
                                             "collapsed": False}]})
    assert response.status_code == 200
    layout = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["layout"]
    assert layout[str(entity_id)] == {"x": 1, "y": 2, "collapsed": False}


def test_layout_rejects_unknown_entities_before_queueing(loaded):
    response = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                            json={"items": [{"entity_id": 999999, "x": 1, "y": 2}]})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_ENTITY_NOT_FOUND"


def test_layout_rejects_an_item_that_changes_nothing(loaded):
    entity_id = int(_entities(loaded)[0]["id"])
    response = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                            json={"items": [{"entity_id": entity_id}]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_EMPTY_PATCH"


def test_layout_refuses_a_collapsed_only_row_for_a_new_entity(loaded):
    """没有坐标就建行 → 那一行会带着 (0,0) 出生，节点下次打开跳到左上角。

    它在**排队之前**就被拒（400），而不是走到写者里抛一个 500 —— 一个说得清
    原因的错误不该被降级成"服务器错误"。
    """
    entity_id = int(_entities(loaded)[0]["id"])
    response = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                            json={"items": [{"entity_id": entity_id, "collapsed": True}]})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "E_LAYOUT_NEEDS_POSITION"
    layout = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]["layout"]
    assert str(entity_id) not in layout, "拒绝之后不许留下半条记录"

    # 拖过一次之后，只切折叠就合法了 —— 这条边界不是"永远不许"，是"新行必须
    # 有坐标"。
    loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                 json={"items": [{"entity_id": entity_id, "x": 5, "y": 6}]})
    again = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                         json={"items": [{"entity_id": entity_id, "collapsed": True}]})
    assert again.status_code == 200, again.text


def test_all_new_writes_go_through_the_single_write_queue(loaded):
    """R1：写入口唯一。新加的这条批量路由与其它写走的是**同一个**写者。"""
    stats = loaded.app.state.ctx.writer.stats
    entity_id = int(_entities(loaded)[0]["id"])
    loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                 json={"add_tags": ["排队断言"]})
    loaded.post("/api/v1/entities/lifecycle", headers=HEADERS,
                json={"entity_ids": [entity_id], "lifecycle": "retired"})
    assert stats.concurrent_max == 1, (
        "出现了两个同时执行的写任务 —— 单写队列被绕开了")
