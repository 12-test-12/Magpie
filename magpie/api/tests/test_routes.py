"""路由契约：信封、鉴权、过滤、501 边界（2.7）。

三类断言，各自的理由不同:

  * **信封**：`{"ok":true,"data":...}` / `{"ok":false,"error":{...}}` 是 `/api/v1`
    的对外形状。三个消费者（GUI / CLI / MCP）都靠它分叉，破一次要动三处。
  * **501 而不是空数组**：没交付的端点必须响亮。空数组对 agent 是一个**确定的
    断言**（"这里就是没有"），而事实是"这条路由还不存在"—— 两者混起来，
    agent 会基于一个假事实继续往下走。
  * **拒绝而不是静默忽略**：传了不支持的过滤条件要 400。静默忽略等于给
    agent 一个"筛过了"的假答案。
"""

from __future__ import annotations

import json
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
    import time
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = client.app.state.ctx.imports.get(import_id)
        if record is not None and record.terminal:
            break
        time.sleep(0.02)
    assert record is not None and record.state == "done", f"夹具导入失败：{record}"
    return client


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_token_required(client):
    assert client.get("/api/v1/meta").status_code == 401
    assert client.get("/api/v1/meta?token=wrong").status_code == 401
    assert client.get("/api/v1/meta?token=" + TOKEN).status_code == 200
    assert client.get("/api/v1/meta", headers=HEADERS).status_code == 200
    # 头名大小写不敏感（ASGI 实现不保证）
    assert client.get("/api/v1/meta",
                      headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_error_envelope_shape(client):
    response = client.get("/api/v1/meta")
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_UNAUTHORIZED"
    assert body["error"]["message"]
    assert "hint" in body["error"]


def test_openapi_is_public_on_purpose(client):
    """/openapi.json 不带 token —— 这是**有意的**（见 app.py 的模块说明）。

    它描述形状不描述数据；给它加 token 只会催生"把 token 塞进 URL"的做法。
    这条断言把那个取舍钉住：改它要改这里，也就必须重新想一遍。
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"].startswith("Magpie")
    # 但数据仍然要 token
    assert client.get("/api/v1/projects").status_code == 401


# ---------------------------------------------------------------------------
# 元信息 / 项目
# ---------------------------------------------------------------------------


def test_meta_lists_kinds_verbs_parsers(client):
    data = client.get("/api/v1/meta", headers=HEADERS).json()["data"]
    assert data["api_prefix"] == "/api/v1"
    assert data["schema_version"] >= 1
    assert {k["kind"] for k in data["kinds"]}, "类型登记表是空的"
    assert {v["verb"] for v in data["relation_verbs"]}, "关系词表是空的"
    assert {p["tool"] for p in data["parsers"]} >= {"domainlist", "httpx"}
    assert set(data["counts"]) >= {"projects", "entities", "observations"}


def test_project_must_be_explicit_when_ambiguous(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "a"})
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "b"})
    response = client.get("/api/v1/entities", headers=HEADERS)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "E_PROJECT_REQUIRED"
    assert set(error["projects"]) == {"a", "b"}

    # 只有一个项目时允许省略 —— 但是**因为只有一个**，不是因为"默认第一个"
    assert client.get("/api/v1/projects", headers=HEADERS).json()["data"]


def test_single_project_can_be_implicit(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "only"})
    assert client.get("/api/v1/entities", headers=HEADERS).status_code == 200


def test_duplicate_project_rejected(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "dup"})
    response = client.post("/api/v1/projects", headers=HEADERS, json={"name": "dup"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "E_PROJECT_EXISTS"


# ---------------------------------------------------------------------------
# 实体 / 图
# ---------------------------------------------------------------------------


def test_entities_pagination_is_stable(loaded):
    first = loaded.get("/api/v1/entities?limit=2", headers=HEADERS).json()
    assert len(first["data"]) == 2
    assert first["meta"]["next_cursor"]
    second = loaded.get(f"/api/v1/entities?limit=2&cursor={first['meta']['next_cursor']}",
                        headers=HEADERS).json()
    ids_first = {e["id"] for e in first["data"]}
    ids_second = {e["id"] for e in second["data"]}
    assert not (ids_first & ids_second), "翻页返回了重复的实体"


def _with_observations(client) -> dict:
    """夹具里**真的有观测**的那个实体。写死 kind 会随解析器扩张而失效 ——
    "第一个有观测的实体"才是这条测试真正需要的前提。"""
    listing = client.get("/api/v1/entities?limit=500", headers=HEADERS).json()["data"]
    assert listing, "导入后没有任何实体"
    found = next((e for e in listing if e["observation_count"] > 0), None)
    assert found is not None, "导入后没有一个实体带观测，测不出详情/上下文包"
    return found


def test_entity_detail_has_observations_and_relations(loaded):
    entity_id = _with_observations(loaded)["id"]
    data = loaded.get(f"/api/v1/entities/{entity_id}", headers=HEADERS).json()["data"]
    assert data["entity"]["id"] == entity_id
    assert data["observations"], "详情里没有观测"
    assert data["relations"], "详情里没有关系"
    assert {"entity", "observations", "relations", "tags", "manual",
            "open_inbox", "timeline"} <= set(data)


def test_unsupported_filter_is_rejected_not_ignored(loaded):
    """静默忽略一个过滤条件 = 给 agent 一个"筛过了"的假答案。"""
    response = loaded.get("/api/v1/entities?review_status=confirmed", headers=HEADERS)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "E_UNSUPPORTED_FILTER"
    assert "review_status" in error["rejected"]
    assert "kind" in error["supported"]


def test_graph_nodes_edges_layout(loaded):
    graph = loaded.get("/api/v1/graph?limit=200", headers=HEADERS).json()
    data = graph["data"]
    assert data["nodes"] and data["edges"]
    node_ids = {n["id"] for n in data["nodes"]}
    for edge in data["edges"]:
        assert edge["src"] in node_ids and edge["dst"] in node_ids, \
            "边的两端必须都在本次返回的节点里（否则前端会画出悬空的线）"


def test_neighbors_depth(loaded):
    entity_id = loaded.get("/api/v1/entities?limit=1",
                           headers=HEADERS).json()["data"][0]["id"]
    one = loaded.get(f"/api/v1/graph/{entity_id}/neighbors?depth=1",
                     headers=HEADERS).json()["data"]
    two = loaded.get(f"/api/v1/graph/{entity_id}/neighbors?depth=2",
                     headers=HEADERS).json()["data"]
    assert one["root_id"] == entity_id
    assert len(two["nodes"]) >= len(one["nodes"])
    assert loaded.get(f"/api/v1/graph/{entity_id}/neighbors?depth=0",
                      headers=HEADERS).status_code == 400


def test_layout_persists_and_reads_back(loaded):
    entity_id = loaded.get("/api/v1/entities?limit=1",
                           headers=HEADERS).json()["data"][0]["id"]
    response = loaded.patch("/api/v1/graph/layout", headers=HEADERS,
                            json={"items": [{"entity_id": entity_id,
                                             "x": 12.5, "y": -3.0,
                                             "collapsed": True}]})
    assert response.status_code == 200, response.text
    graph = loaded.get("/api/v1/graph?limit=200", headers=HEADERS).json()["data"]
    assert graph["layout"][str(entity_id)] == {"x": 12.5, "y": -3.0,
                                               "collapsed": True}


def test_lenses_only_accepts_registered_lenses(loaded):
    lens = loaded.get("/api/v1/lenses/assets", headers=HEADERS)
    assert lens.status_code == 200
    assert lens.json()["data"]["kinds"], "assets 镜头下没有 kind"
    missing = loaded.get("/api/v1/lenses/nope", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "E_LENS_NOT_FOUND"


# ---------------------------------------------------------------------------
# 域名视图（0.19.0 · under_domain / with_domain）
# ---------------------------------------------------------------------------


def test_graph_under_domain_scopes_to_the_family(loaded):
    """/graph?under_domain= —— 画布只剩名下节点，推导边界随 `notes` 回去。

    夹具前提：subfinder 有 app/api 两个域名实体（没有裸 example.com，
    所以这里锚定 app.example.com —— 它自己就是 kind=domain）。
    """
    full = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]
    full_keys = {n["entity_key"] for n in full["nodes"]}
    assert {"app.example.com", "api.example.com"} <= full_keys, "夹具前提"
    scoped = loaded.get(
        "/api/v1/graph?limit=500&under_domain=app.example.com",
        headers=HEADERS).json()["data"]
    keys = {n["entity_key"] for n in scoped["nodes"]}
    assert "app.example.com" in keys, "域名实体自己是画布焦点节点"
    assert "api.example.com" not in keys, "别的域名混进来了"
    assert {n["id"] for n in scoped["nodes"]} <= {n["id"] for n in full["nodes"]}, \
        "scoped 必须是 full 的子集（推在 SQL 里，不是前端再筛的）"
    assert any("推导出来的" in note for note in scoped["notes"]), \
        "推导的边界必须跟着图负载一起回去"


def test_graph_notes_always_present(loaded):
    """0.19.0 起 `/graph` 负载恒有 `notes`：没话要说 = 空数组，但键必须在。"""
    data = loaded.get("/api/v1/graph?limit=50", headers=HEADERS).json()["data"]
    assert data["notes"] == []


def test_graph_kinds_filters_in_sql_and_carries_total(loaded):
    """0.20.0：kind 筛选推在 SQL 里，负载恒带 `total`（同筛选、不带 LIMIT）。

    截图 bug 的根因：稀有 kind（secret，库里 13 个）多半不在前 500 里，本地筛
    等于"0 个匹配"；空状态还拿载入数（=limit）冒充库总数。`total` 与
    `nodes.length` 是**两个口径**，界面上不许再混。
    """
    full = loaded.get("/api/v1/graph?limit=500", headers=HEADERS).json()["data"]
    assert isinstance(full["total"], int) and full["total"] >= len(full["nodes"])

    single = loaded.get("/api/v1/graph?limit=500&kind=domain",
                        headers=HEADERS).json()["data"]
    assert single["nodes"] and {n["kind"] for n in single["nodes"]} == {"domain"}

    multi = loaded.get("/api/v1/graph?limit=500&kinds=domain,ip",
                       headers=HEADERS).json()["data"]
    assert multi["nodes"], "夹具里 domain 与 ip 都该有"
    assert {n["kind"] for n in multi["nodes"]} <= {"domain", "ip"}

    # 库里没有的 kind：节点 0 个，total 也是 0（不是"加载数冒充库总数"）
    none = loaded.get("/api/v1/graph?limit=500&kinds=nonexistent",
                      headers=HEADERS).json()["data"]
    assert none["nodes"] == [] and none["total"] == 0


def test_entities_under_domain_missing_says_missing(loaded):
    """格式合法但实体不在库里 → 空 + **为什么空**（product 先例），200 不是 404。"""
    response = loaded.get("/api/v1/entities?under_domain=ghost.example.com",
                          headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == []
    meta = body["meta"]
    assert meta["query"] == "assets_under_domain"
    assert meta["filters"]["under_domain"] == "ghost.example.com"
    assert any("还没被任何来源写进来" in note for note in meta["notes"])


def test_under_domain_bad_usage_is_400(loaded):
    """问法错要响亮 —— 静默回空列表会让人以为"查过了，名下没有"。"""
    for path in ("/api/v1/graph?under_domain=nonsense",
                 "/api/v1/entities?under_domain=nonsense"):
        response = loaded.get(path, headers=HEADERS)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "E_NOT_A_DOMAIN"


def test_entities_with_domain_annotates_without_filtering(loaded):
    """`with_domain=1` 是呈现参数：补 `domain` 字段，一行不多一行不少。

    注解与 `under_domain=` 筛选共用同一组原语 —— 这里在 API 层再钉一遍
    "注解归到 app.example.com 的行，必须也在 under_domain 的集合里"。
    """
    plain = loaded.get("/api/v1/entities?limit=500", headers=HEADERS).json()
    annotated = loaded.get("/api/v1/entities?limit=500&with_domain=1",
                           headers=HEADERS).json()
    assert len(annotated["data"]) == len(plain["data"]), "呈现参数不许筛掉行"
    assert all("domain" in row for row in annotated["data"])
    assert all("domain" not in row for row in plain["data"])
    by_key = {row["entity_key"]: row["domain"] for row in annotated["data"]}
    assert by_key["app.example.com"] == "app.example.com", "域名行归自己"

    scoped_ids = {row["id"] for row in loaded.get(
        "/api/v1/entities?limit=500&under_domain=app.example.com",
        headers=HEADERS).json()["data"]}
    for row in annotated["data"]:
        if row["domain"] == "app.example.com":
            assert row["id"] in scoped_ids, f"{row['entity_key']} 注解与筛选分叉"


def test_domains_summary_counts_match_with_domain_grouping(loaded):
    """0.20.0 `/domains/summary`：**单真相** —— 与 `with_domain=1` 分组逐域名相等。

    下拉里的 `key (N)`、镜头页分段行、详情面板的名下计数必须是同一个数 ——
    所以这里拿注解结果当基准做相等断言，而不是只看形状（形状对、数分叉，
    正是截图里三个口径互相打架的那种病）。
    """
    body = loaded.get("/api/v1/domains/summary", headers=HEADERS).json()
    rows = body["data"]
    assert rows, "夹具里有域名与名下资产"
    assert body["meta"]["count"] == len(rows)
    totals = [row["total"] for row in rows]
    assert totals == sorted(totals, reverse=True), "按 total 降序（同数按名字典序）"
    for row in rows:
        assert row["domain"] and row["total"] >= 1
        assert sum(row["kinds"].values()) == row["total"], "kinds 加和必须等于 total"
        assert list(row["kinds"].values()) == sorted(
            row["kinds"].values(), reverse=True), "kinds 计数降序"

    annotated = loaded.get("/api/v1/entities?limit=500&with_domain=1",
                           headers=HEADERS).json()["data"]
    grouped: dict[str, int] = {}
    for row in annotated:
        if row.get("domain"):
            grouped[row["domain"]] = grouped.get(row["domain"], 0) + 1
    assert grouped, "夹具里有可归域的实体"
    assert {r["domain"]: r["total"] for r in rows} == grouped, \
        "summary 与 with_domain 分组必须同一套推导"


def test_domains_summary_self_attribution_and_domain_filter(loaded):
    """域名行归自己（host==key 首选自己）；`domain=` 只回一行且口径不变。"""
    rows = loaded.get("/api/v1/domains/summary",
                      headers=HEADERS).json()["data"]
    by_key = {row["domain"]: row for row in rows}
    assert "app.example.com" in by_key, "域名实体自己必在行里"
    own = by_key["app.example.com"]
    assert own["kinds"].get("domain", 0) >= 1, "自己至少给自己算一个"

    one = loaded.get("/api/v1/domains/summary?domain=app.example.com",
                     headers=HEADERS).json()
    assert [r["domain"] for r in one["data"]] == ["app.example.com"]
    assert one["data"][0] == own, "过滤不改口径：同一行、同一个数"


def test_domains_summary_empty_and_bad_usage(loaded):
    """两种空分开 + 问法错响亮（与 `under_domain=` 同一条纪律）。"""
    # 域名实体还没写进库 → 200 空 + 为什么空
    ghost = loaded.get("/api/v1/domains/summary?domain=ghost.example.com",
                       headers=HEADERS)
    assert ghost.status_code == 200
    body = ghost.json()
    assert body["data"] == []
    assert any("还没被任何来源写进来" in note for note in body["meta"]["notes"])

    # key 被非 domain 实体占用（夹具里 httpx 的 a 记录产出 ip）→ 400
    ips = [row["entity_key"] for row in loaded.get(
        "/api/v1/entities?limit=500&kind=ip", headers=HEADERS).json()["data"]]
    assert ips, "夹具前提：httpx 的 a 记录该产出 ip 实体"
    wrong = loaded.get(f"/api/v1/domains/summary?domain={ips[0]}",
                       headers=HEADERS)
    assert wrong.status_code == 400
    error = wrong.json()["error"]
    assert error["code"] == "E_NOT_A_DOMAIN"
    assert error.get("actual_kind") == "ip"

    # 根本不是域名 → 400
    bad = loaded.get("/api/v1/domains/summary?domain=nonsense", headers=HEADERS)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "E_NOT_A_DOMAIN"


# ---------------------------------------------------------------------------
# 写入路径
# ---------------------------------------------------------------------------


def test_patch_entity_tags_and_protection(loaded):
    listing = loaded.get("/api/v1/entities?limit=1", headers=HEADERS).json()["data"]
    entity_id = listing[0]["id"]
    response = loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                            json={"add_tags": ["重要"], "protect_fields": ["tech"]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["tags"] == ["重要"]
    assert data["protected_fields"] == ["tech"]

    detail = loaded.get(f"/api/v1/entities/{entity_id}",
                        headers=HEADERS).json()["data"]
    assert detail["tags"] == ["重要"]
    assert detail["manual"]["protected_fields"] == ["tech"]

    removed = loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                           json={"remove_tags": ["重要"], "unprotect_fields": ["tech"]})
    assert removed.json()["data"]["tags"] == []
    assert removed.json()["data"]["protected_fields"] == []


def test_patch_refuses_to_fake_a_value_write(loaded):
    """人工写**值**没有走 HTTP 的路径 —— 它必须 501，而不是静默只改了个标记。

    静默地"收下 review_status 但什么都不做"是最坏的一种：调用方以为改成功了。
    """
    entity_id = loaded.get("/api/v1/entities?limit=1",
                           headers=HEADERS).json()["data"][0]["id"]
    response = loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS,
                            json={"review_status": "confirmed"})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "E_NOT_IMPLEMENTED"


def test_empty_patch_rejected(loaded):
    entity_id = loaded.get("/api/v1/entities?limit=1",
                           headers=HEADERS).json()["data"][0]["id"]
    response = loaded.patch(f"/api/v1/entities/{entity_id}", headers=HEADERS, json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_EMPTY_PATCH"


def test_import_missing_path_fails_fast(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "demo"})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": ["no/such/file.txt"], "project": "demo"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_PATH_NOT_FOUND"


def test_import_no_input_rejected(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "demo"})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"project": "demo"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_NO_INPUT"


def test_import_bad_archive_rejected(client):
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "demo"})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": [str(SUBFINDER)], "project": "demo",
                                 "archive": "delete"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_BAD_ARCHIVE"


# ---------------------------------------------------------------------------
# 体检 / Inbox / 活动 / 导出 / 上下文
# ---------------------------------------------------------------------------


def test_doctor_reports_integrity(loaded):
    data = loaded.get("/api/v1/doctor", headers=HEADERS).json()["data"]
    assert data["ok"] is True
    assert data["violations"] == []
    assert data["jobs_by_state"], "体检里没有任何 job 状态"
    assert "untested_entities" in data


def test_inbox_and_resolve(loaded):
    inbox = loaded.get("/api/v1/inbox", headers=HEADERS).json()
    assert inbox["ok"] is True
    data = inbox["data"]
    if data:
        item_id = data[0]["id"]
        response = loaded.post(f"/api/v1/inbox/{item_id}/resolve", headers=HEADERS,
                               json={"state": "dismissed", "resolution": {"by": "test"}})
        assert response.status_code == 200, response.text
        after = loaded.get("/api/v1/inbox", headers=HEADERS).json()["data"]
        assert item_id not in {item["id"] for item in after}
    else:
        # 没有 open 项也要能走通查询（返回空列表，而不是报错）
        assert data == []
    bad = loaded.post("/api/v1/inbox/1/resolve", headers=HEADERS,
                      json={"state": "nope"})
    assert bad.status_code == 400


def test_activity_records_writes(loaded):
    items = loaded.get("/api/v1/activity?limit=50", headers=HEADERS).json()["data"]
    assert any(item["action"] == "job.start" or item["action"].startswith("job")
               for item in items), f"活动日志里没有 job 相关记录：{items[:3]}"


def test_export_formats(loaded):
    listing = loaded.get("/api/v1/export?format=json", headers=HEADERS).json()
    assert listing["data"], "导出为空"
    assert any(entity["fields"] for entity in listing["data"])

    csv_text = loaded.get("/api/v1/export?format=csv", headers=HEADERS).text
    assert csv_text.splitlines()[0] == "entity_id,entity_key,kind,field,value,confidence"

    domains = loaded.get("/api/v1/export?format=domains", headers=HEADERS).text
    assert "example.com" in domains

    markdown = loaded.get("/api/v1/export?format=markdown", headers=HEADERS).text
    assert markdown.startswith("# 导出")

    bad = loaded.get("/api/v1/export?format=xlsx", headers=HEADERS)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "E_BAD_FORMAT"


def test_agent_context_is_markdown_plus_json(loaded):
    entity_id = _with_observations(loaded)["id"]
    body = loaded.get(f"/api/v1/agent/context?entity_id={entity_id}&depth=2",
                      headers=HEADERS).json()["data"]
    assert body["json"]["entity"]["id"] == entity_id
    markdown = body["markdown"]
    assert markdown.startswith("# ")
    assert "## 观测" in markdown and "## 这个项目里还有什么没测" in markdown
    # 上下文包必须能独立读懂 —— 观测与关系都要在 markdown 里出现
    assert body["json"]["observations"], "示例实体没有观测，测不出上下文包"


# ---------------------------------------------------------------------------
# 尚未实现的端点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/search?q=x"),
    ("GET", "/api/v1/diff?since=last"),
    ("POST", "/api/v1/paste"),
    # 三条 `/pipelines` 从 3.7b 起是**真端点**（编排模式落地），这里的 501 断言
    # 相应地删掉 —— 留下它们就等于在断言"这一块还没做"，而那句话已经不成立。
    # 它们的行为由 `test_pipelines.py` 接管。
])
def test_unimplemented_endpoints_are_loud(client, method, path):
    """501 + 结构化错误，**不是**空数组 —— 空数组会被读成"这里就是没有"。"""
    response = client.request(method, path, headers=HEADERS,
                              json={} if method == "POST" else None)
    assert response.status_code == 501, f"{path} 返回了 {response.status_code}"
    error = response.json()["error"]
    assert error["code"] == "E_NOT_IMPLEMENTED"
    assert error["plan"], "必须说清它属于哪一步，否则没人知道该等什么"


def test_openapi_documents_every_route(client):
    """openapi 是 agent 的说明书 —— 说明书里必须真的有那些路由。"""
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert "/api/v1/imports/{import_id}/events" in paths
    assert "/api/v1/meta" in paths
    assert "/api/v1/search" in paths          # 501 的路由也要在说明书里
    import_ids = schema["components"]["schemas"]
    assert "EntityPatch" in import_ids        # 请求体的形状也是契约
    assert json.dumps(schema, ensure_ascii=False).count("0.0.0.0") == 0
