"""0.19.0 · `under_domain` —— 按域名归拢资产（视图层隔离的服务端那半）。

归属是**推导的**（后缀 + resolves_to 反查 + found_on 并入），不是一条存着的
边 —— 所以这组用例的主线有三条：

  * **全集与子集**：example.com 名下四路成员各就各位，无关域名（含
    `badexample.com` 这种"没有点的后缀"陷阱）一个都不许混进来；问
    a.example.com 只有它名下的。
  * **问法错要响亮**：不是域名 / key 被非 domain 实体占用 → `E_NOT_A_DOMAIN`
    （静默回空列表会让人以为"查过了，名下没有"）；格式对但实体不在库里 →
    空 + **为什么空**（product 先例）。
  * **单真相**：`with_domain` 注解与 `under_domain` 筛选共用同一组原语 ——
    注解归到 D 的行，under_domain=D 的筛选必须也含它。两处各写一遍后缀判断，
    是"分组看到的与筛出来的不一致"的标准来路。

夹具**手搭**而不是用真实导入（nmap/nuclei/dirsearch）：那个形状里没有
"三级域名 + resolves_to + found_on + 无关域名"同框的场景。
"""

from __future__ import annotations

import sqlite3

import pytest

from magpie.core import ops, query, store


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def domain_shaped(db: sqlite3.Connection) -> int:
    """手搭的域名归属形状（成员表即断言表，见文件头）。"""
    project_id = ops.create_project(db, "under-domain",
                                    {"include": ["*"], "exclude": []})
    job = ops.start_job(db, project_id=project_id, kind="ingest", coverage="full")
    raw = ops.add_raw_file(db, project_id=project_id,
                           locator_uri="file:///seed.txt", content="x")
    evidence = ops.add_evidence(db, project_id=project_id, raw_file_id=raw,
                                locator={"line": 1}, snippet="x")
    source = store.register_source(db, name="seed", actor="test")

    def ent(key: str, kind: str) -> int:
        return ops.upsert_entity(db, project_id=project_id, job_id=job,
                                 evidence_id=evidence, source_id=source,
                                 entity_key=key, kind=kind)

    def rel(src: str, dst: str, verb: str) -> int:
        return ops.add_relation(db, project_id=project_id, job_id=job,
                                evidence_id=evidence, source_id=source,
                                src_key=src, dst_key=dst, rel=verb)

    for key, kind in [
        ("example.com", "domain"), ("a.example.com", "domain"),
        ("x.a.example.com", "domain"), ("badexample.com", "domain"),
        ("other.com", "domain"),
        ("shop.example.com", "host"), ("legacy.example.com", "host"),
        ("www.a.example.com", "host"),   # 多候选：归 a.example.com 而非 example.com
        ("web.other.com", "host"),
        ("api.example.com:443", "service"),
        ("app.example.com:8080/user/{id}", "endpoint"),
        ("203.0.113.10", "ip"), ("198.51.100.7", "ip"),
        ("203.0.113.99", "ip"),
        ("CVE-2021-44228@https://api.example.com:443/", "vuln"),
        ("CVE-2020-0000@https://web.other.com/", "vuln"),
    ]:
        ent(key, kind)
    # 实体先建好：add_relation 的 kind 只是**兜底**，已存在的实体不会被改写。
    rel("example.com", "203.0.113.10", "resolves_to")
    rel("other.com", "198.51.100.7", "resolves_to")
    rel("CVE-2021-44228@https://api.example.com:443/",
        "api.example.com:443", "found_on")
    rel("CVE-2020-0000@https://web.other.com/", "web.other.com", "found_on")
    # 手搭走的 upsert_entity 只写 `canonical=0/is_current=0` 半成品 —— 真实
    # 导入由 ingest 收尾调 recompute 翻牌（`_rebuild_identity`）。夹具补同一口
    # 气，否则所有 canonical=1 的 JOIN 都空转（test_flows 同款先例）。
    store.recompute(db)
    return project_id


def _keys_for(db: sqlite3.Connection, project_id: int, ids) -> set[str]:
    return set(query.keys_of(db, project_id, list(ids)).values())


def test_under_domain_gathers_the_whole_family_and_only_the_family(db,
                                                                   domain_shaped):
    """全集：四路成员各就各位，无关域名一个都不许进来。"""
    canonical, ids, notes = query.under_domain_ids(db, domain_shaped,
                                                   "example.com")
    assert canonical == "example.com"
    keys = _keys_for(db, domain_shaped, ids)
    assert keys == {
        "example.com",                       # 目标自己（画布焦点）
        "a.example.com", "x.a.example.com",  # 后缀：子域与孙域
        "legacy.example.com",                # host kind，key 恰是域名串
        "shop.example.com",                  # host kind
        "www.a.example.com",                 # 挂在孙域名下的 host
        "api.example.com:443",               # service：先取 host 再比
        "app.example.com:8080/user/{id}",    # endpoint：同上
        "203.0.113.10",                      # resolves_to 反查
        "CVE-2021-44228@https://api.example.com:443/",   # found_on 第二遍
    }
    assert not keys & {
        "badexample.com", "other.com", "web.other.com", "198.51.100.7",
        "203.0.113.99", "CVE-2020-0000@https://web.other.com/",
    }, "无关域名（或后缀陷阱）混进了名下"
    # 推导的边界必须随结果一起回去 —— 否则空列表会被读成"存着的边没有"。
    assert any("推导出来的" in note for note in notes)
    assert any(f"候选 {len(ids)}" in note for note in notes)


def test_under_domain_subset_is_the_most_specific_scope(db, domain_shaped):
    """子集：问 a.example.com 只有它名下的 —— api.example.com 不是它的。"""
    _, ids, _ = query.under_domain_ids(db, domain_shaped, "a.example.com")
    assert _keys_for(db, domain_shaped, ids) == {"a.example.com",
                                                 "x.a.example.com",
                                                 "www.a.example.com"}


def test_under_domain_rejects_non_domains_loudly(db, domain_shaped):
    """问法错要响亮 —— 静默回空列表会让人以为"查过了，名下没有"。"""
    # ① 输入根本不是域名（单标签，格式关就拦下）
    with pytest.raises(query.QueryError) as excinfo:
        query.under_domain_ids(db, domain_shaped, "nonsense")
    assert excinfo.value.code == "E_NOT_A_DOMAIN"
    # ② 合法域名格式、但 key 被非 domain 实体占用（IP 走这支）
    with pytest.raises(query.QueryError) as excinfo:
        query.under_domain_ids(db, domain_shaped, "203.0.113.10")
    assert excinfo.value.code == "E_NOT_A_DOMAIN"
    assert excinfo.value.details["actual_kind"] == "ip"
    with pytest.raises(query.QueryError) as excinfo:
        query.under_domain_ids(db, domain_shaped, "legacy.example.com")
    assert excinfo.value.code == "E_NOT_A_DOMAIN"
    assert excinfo.value.details["actual_kind"] == "host"


def test_under_domain_missing_says_missing_not_empty(db, domain_shaped):
    """域名格式合法但实体不在库里 → 空 + **为什么空**（product 先例）。"""
    result = query.assets_under_domain(db, domain_shaped, "ghost.example.com")
    assert result["query"] == "assets_under_domain"
    assert result["count"] == 0 and result["entities"] == []
    assert result["filters"]["under_domain"] == "ghost.example.com"
    assert any("还没被任何来源写进来" in note for note in result["notes"])


def test_dispatch_routes_under_domain_to_its_named_query(db, domain_shaped):
    """分发点认得出这一问（`query` 名字跟着结果走）。"""
    result = query.dispatch(db, domain_shaped, under_domain="example.com")
    assert result["query"] == "assets_under_domain"
    assert result["count"] >= 9


def test_annotate_domains_shares_the_derivation_with_the_filter(db,
                                                               domain_shaped):
    """`with_domain` 注解：与筛选**同一组原语** —— 单真相回归。

    多归属分工在这里兑现：**筛选取全集**（逐域名问）、**分组取确定性首选**
    （最具体优先，域名行归自己）。于是 www.a.example.com 的注解是
    a.example.com，而问 example.com 时它照样在集合里。
    """
    rows = [dict(row) for row in db.execute(
        "SELECT e.id, e.kind, k.entity_key FROM entities e"
        " JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id"
        "  AND k.canonical=1"
        " WHERE e.project_id=? AND e.is_current=1 AND e.merged_into IS NULL"
        " ORDER BY e.id", (domain_shaped,))]
    query.annotate_domains(db, domain_shaped, rows)
    by_key = {row["entity_key"]: row["domain"] for row in rows}

    # 最具体优先 = 含自身的最长命中：域名行归**自己**（分组表里
    # "a.example.com" 组必须含 a.example.com —— 归了父域，组名就和组员对不上）
    assert by_key["a.example.com"] == "a.example.com"
    assert by_key["x.a.example.com"] == "x.a.example.com"
    # 非域名行才看得见"多候选取最具体"：www.a 同时命中 a.example.com 与
    # example.com，取前者 —— 一行只进一个分组的确定性靠这条钉住
    assert by_key["www.a.example.com"] == "a.example.com"
    assert by_key["example.com"] == "example.com"
    # 内存三件套（host 原样 / service / endpoint 取 host）
    assert by_key["shop.example.com"] == "example.com"
    assert by_key["legacy.example.com"] == "example.com"
    assert by_key["api.example.com:443"] == "example.com"
    assert by_key["app.example.com:8080/user/{id}"] == "example.com"
    # resolves_to 反查 + found_on 第二遍（深度与 under_domain_ids 对齐）
    assert by_key["203.0.113.10"] == "example.com"
    assert by_key["CVE-2021-44228@https://api.example.com:443/"] == "example.com"
    # 别的域照旧归自己；后缀陷阱与裸 ip 没有归属
    assert by_key["other.com"] == "other.com"
    assert by_key["198.51.100.7"] == "other.com"
    assert by_key["CVE-2020-0000@https://web.other.com/"] == "other.com"
    # 后缀陷阱：badexample.com 自己有域名行（归自己），但**没有**被
    # example.com 认领；没解析过的裸 ip 才是真·无归属
    assert by_key["badexample.com"] == "badexample.com"
    assert by_key["203.0.113.99"] is None

    # 单真相：凡注解归到 D 的行，under_domain=D 的筛选**必须**也含它。
    # 两处共用 `_key_host` + `_suffix_domains`，这条断言是"没有分叉"的回归钉。
    for name in ("example.com", "a.example.com", "other.com"):
        _, filtered, _ = query.under_domain_ids(db, domain_shaped, name)
        member = set(filtered)
        for row in rows:
            if row["domain"] == name:
                assert row["id"] in member, (row["entity_key"], name)


def test_graph_payload_always_carries_notes(db, domain_shaped):
    """0.19.0 起 `/graph` 负载恒有 `notes`（空数组 = 没话要说），带
    under_domain 时推导边界跟着回去，节点集与筛选同集。"""
    plain = query.graph_payload(db, domain_shaped, lens=None, kind=None,
                                q=None, limit=100)
    assert plain["notes"] == []
    scoped = query.graph_payload(db, domain_shaped, lens=None, kind=None,
                                 q=None, limit=100, under_domain="example.com")
    assert scoped["notes"], "推导的边界必须随图负载一起回去"
    expect = query.under_domain_ids(db, domain_shaped, "example.com")[1]
    assert {n["id"] for n in scoped["nodes"]} == set(expect)
