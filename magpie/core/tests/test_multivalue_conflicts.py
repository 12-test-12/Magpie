"""LARI-35 · 多值字段的假冲突 + 冲突 Inbox 项的生命周期（3.3 / 3.4 的两个缺陷）。

两个缺陷一个语义、一个生命周期，**都有各自的反证**，缺任何一条都只是"看起来
修好了"：

    缺陷 A（语义）  多值字段被一律当成冲突 —— `path` / `cname` / `method` /
                    `status` 这类字段的多个值不是"两个来源在打架"，而是这个
                    实体自己的证据。后果是每导入一个被折过的 endpoint 就开一条
                    `value_conflict`，量大了 Inbox 被填满，3.6 的"清空即干净"
                    够不着。
    缺陷 B（生命周期）冲突项 `undo` 关不掉 —— 生产者没传 `job_id`，
                    `created_by_job_id` 为 NULL，而 `undo` 是按 job 关待办的。

修法（两条都写在这里，因为它们是这一整个文件在验的东西）：

    A 用**声明**，不用特例。多值性是 `(field_namespace, semantic_field)` 的属性，
      落成 `multivalue_fields` 表（迁移 010），视图里用 `NOT EXISTS` 排掉。
      写死一个 `path` 是特例，下一个 kind 还会再撞一次。
    B 冲突项是**派生状态**，生命周期跟着**视图**走。锚 job（`job_id=`）与
      对账（`ops.reconcile_conflicts`）**两件都要**，理由是两条要求都要成立:

        * 库里有冲突   → 必须看得见（锚 job 的项会被 undo 的既有边关掉、
                        对账会把"视图里有而 Inbox 里没有"的开出来）；
        * 库里没冲突了 → 不许留着一条 open 项（对账按视图关掉）。

      只锚 job: 一条**更早的作业**开的冲突项在它的贡献被撤掉之后会留在那里
      （`test_undo_of_an_earlier_job_also_clears_the_conflict` 就是这条反证）。
      只按 job 关: 会在冲突**仍然存在**时错误地报干净。

判据 5 的反证在这个文件里有两处，不是一句注释:
`test_real_conflict_on_a_single_valued_field_is_still_detected`（真冲突必须开、
且只开一条）与 `test_the_declaration_is_narrow`（声明表必须窄到区分得开
"来源枚举出来的项"与"对同一个问题的两个回答"）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from magpie.core import ingest, migrate, ops, query, store


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project(db) -> int:
    return ops.create_project(db, "multivalue-test", {"include": [], "exclude": []})


def options(project_id: int, data_dir: Path) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def httpx_row(host: str, *, title: str = "T", **extra) -> str:
    """一份 httpx JSONL 行。

    ⚠️ 真分歧的夹具用 `title`（落在 **domain** 上的单值字段），不用
    `webserver`: httpx 会把它拆成 `webserver` **与** `webserver_version` 两个
    字段（`nginx/1.24.0` → `nginx` + `1.24.0`），于是一次"两个来源"的分歧会开出
    **两条**真冲突 —— 那是两个字段各自的真分歧，不是重复。用它当夹具会让
    "只开一条"这条断言测到别的东西上。
    """
    row = {"timestamp": "2026-01-01T00:00:00Z", "port": "443",
           "url": f"https://{host}", "input": host, "title": title, "scheme": "https",
           "webserver": "nginx/1.24.0", "content_type": "text/html",
           "status_code": 200, "tech": ["nginx"]}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def open_conflicts(db, project_id: int) -> list[dict]:
    return [{"field": json.loads(str(r["payload_json"]))["field"],
             "values": json.loads(str(r["payload_json"]))["values"],
             "job": r["created_by_job_id"],
             "closed_by_job_id": r["closed_by_job_id"]}
            for r in db.execute(
                "SELECT payload_json, created_by_job_id, closed_by_job_id"
                "  FROM inbox_items WHERE project_id=? AND kind='value_conflict'"
                "   AND state='open' ORDER BY id", (project_id,))]


def all_conflict_rows(db, project_id: int) -> list[tuple[str, str, str | None]]:
    return [(str(r["kind"]), str(r["state"]), r["close_reason"]) for r in db.execute(
        "SELECT kind, state, close_reason FROM inbox_items WHERE project_id=?"
        " ORDER BY id", (project_id,))]


# ---------------------------------------------------------------------------
# 判据 1 · 被折叠的多值字段不开冲突（A 的正向）
# ---------------------------------------------------------------------------

def test_folded_endpoint_multi_path_opens_no_conflict(db, project, tmp_path):
    """一个 endpoint 见过三条真实路径 → **不开** `value_conflict`。

    这是缺陷 A 最原始的形状（3.3 报的）。三条 `path` 观测必须都还在 ——
    "不开冲突"与"不落证据"是两件事，只做到后者的实现也能让这条变绿，
    所以两条断言必须同时在场。
    """
    report = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "gau.txt",
        "https://app.example.com/user/123\n"
        "https://app.example.com/user/456\n"
        "https://app.example.com/user/789\n"))
    assert report["counts"]["conflicts"] == 0, report["counts"]
    assert open_conflicts(db, project) == []

    # 证据一条不少：三条观测都落在同一个被折过的 endpoint 上。
    values = sorted(str(r[0]) for r in db.execute(
        "SELECT o.value FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        "  JOIN entity_keys ek ON ek.entity_id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind='endpoint'"
        "   AND ek.entity_key='app.example.com/user/{id}'"
        "   AND o.semantic_field='path'", (project,)))
    assert values == ["/user/123", "/user/456", "/user/789"]
    assert query.health_report(db, project)["open_inbox"] == 0


def test_multi_cname_opens_no_conflict(db, project, tmp_path):
    """一个域名两条 CNAME → **不开** `value_conflict`（httpx 的同一形状）。

    这条在缺陷 A 里与 `path` 是**同源**的：`_urls.py` 的文件头写着"CNAME 早就
    是同一个行为"，所以"只修 path"的实现会在这里红。
    """
    report = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "httpx.jsonl", httpx_row(
            "b.example.com", cname=["x.cdn.net", "y.cdn.net"]) + "\n"))
    assert report["counts"]["conflicts"] == 0, report["counts"]
    assert open_conflicts(db, project) == []
    values = sorted(str(r[0]) for r in db.execute(
        "SELECT o.value FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        "  JOIN entity_keys ek ON ek.entity_id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind='domain'"
        "   AND ek.entity_key='b.example.com' AND o.semantic_field='cname'",
        (project,)))
    assert values == ["x.cdn.net", "y.cdn.net"]


def test_multi_method_and_multi_status_open_no_conflict(db, project, tmp_path):
    """同一个 endpoint 两个方法 / 两个状态码 → **不开** `value_conflict`。

    `method` 与 `status` 是 3.3 / 3.4 的四个流量来源都会写的字段，也是缺陷 A
    在真数据上最吵的两个（一次 ffuf / har 导入能刷出好几条）。
    """
    report = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "har.json", json.dumps({"log": {"version": "1.2", "entries": [
            {"request": {"method": "GET", "url": "https://api.example.com/p",
                         "queryString": []},
             "response": {"status": 200}},
            {"request": {"method": "POST", "url": "https://api.example.com/p",
                         "queryString": []},
             "response": {"status": 404}},
        ]}}, ensure_ascii=False)))
    assert report["parser"] == "har@1", report
    assert report["counts"]["conflicts"] == 0, report["counts"]
    assert open_conflicts(db, project) == []

    # 两条观测都还在（`method` 与 `status` 各两条）。
    rows = sorted(str(r[0]) for r in db.execute(
        "SELECT o.semantic_field FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        "  JOIN entity_keys ek ON ek.entity_id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind='endpoint'"
        "   AND ek.entity_key='api.example.com/p'"
        "   AND o.semantic_field IN ('method','status')", (project,)))
    assert rows == ["method", "method", "status", "status"]


# ---------------------------------------------------------------------------
# 判据 2 + 5 · 真冲突必须仍然检出（"不许把多值实现成关掉冲突检测"）
# ---------------------------------------------------------------------------

def test_real_conflict_on_a_single_valued_field_is_still_detected(db, project,
                                                                  tmp_path):
    """两个来源对**同一个单值字段**给出不同值 → 必须开，**且只开一条**。

    这是判据 5 的反证：多值字段退出冲突域，不能把冲突检测一起带走。
    `title` 不在 `multivalue_fields` 里 —— 它是"对同一个问题的两个回答"
    （这个域名的标题到底叫什么），不是"来源各自枚举出来的项"。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    assert open_conflicts(db, project) == [], "一个值的时候不该有冲突"

    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))

    items = open_conflicts(db, project)
    assert len(items) == 1, f"两个来源一个冲突，开出来的必须只有一条：{items}"
    assert items[0]["field"] == "title", items
    assert sorted(items[0]["values"]) == ["Dashboard", "Login Portal"], items


def test_the_declaration_is_narrow(db, project, tmp_path):
    """声明表必须**窄** —— 逐字钉住它的内容，加一行就得改这条用例。

    这是判据 5 的第二处反证，而且它挡的是一个特定的坏实现：把声明写成
    "凡是观测型字段都算多值"（那会让 `webserver` 的分歧静默消失，冲突检测
    等于关掉）。所以这里比的是**集合相等**，不是"包含"。

    往表里加行时唯一可用的尺子（`010_multivalue_fields.sql` 文件头写透了）:

        这个字段的多个值，是**来源各自枚举出来的枚举项**（一个 endpoint 见过
        哪些真实路径 / 一个端口上有哪几条 CPE），还是**对同一个问题的两个回答**
        （这个 service 的 webserver 是 nginx 还是 apache）？

    特别是 `declared_status` **不在**表里: 它是两份契约各自声明的响应码集合
    （`["200","201","404","default"]` vs `["200","404"]`），属于后者。
    """
    declared = {(str(r[0]), str(r[1])) for r in db.execute(
        "SELECT field_namespace, semantic_field FROM multivalue_fields")}
    assert declared == {
        ("observation_field", "path"),
        ("observation_field", "method"),
        ("observation_field", "status"),
        ("observation_field", "redirect"),
        ("observation_field", "length"),
        ("observation_field", "duration"),
        ("observation_field", "fuzz_input"),
        ("observation_field", "source"),
        ("observation_field", "cname"),
        ("observation_field", "cpe"),
        ("observation_field", "cpe_vendor"),
        ("observation_field", "script"),
        # httpx 的逐次遭遇字段 —— 与上面同一种事实、只是名字不同。
        ("observation_field", "status_code"),
        ("observation_field", "content_length"),
        ("observation_field", "final_url"),
    }, sorted(declared)
    # 声明表里每一行都必须带理由 —— 一条说不出理由的声明不该进得来。
    assert not [r for r in db.execute(
        "SELECT 1 FROM multivalue_fields WHERE why IS NULL OR why = ''")]


def test_two_contracts_disagreeing_about_statuses_is_still_a_conflict(db, project,
                                                                      tmp_path):
    """`declared_status` 是**真分歧**，必须仍然开一条 —— 声明表窄的边界在这里。

    两份契约各自声明了不同的响应码集合。它们是"对同一个问题的两个回答"
    （这个接口返回哪些状态码），不是"来源各自枚举出来的项"，所以**不**声明。
    这一条与 `test_the_declaration_is_narrow` 是同一件事的正反两面。
    """
    endpoint = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"},
                "servers": [{"url": "https://api.example.com"}],
                "paths": {"/pets": {"get": {"responses": {"200": {}, "404": {}}}}}}
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "one.json", json.dumps(endpoint)))
    assert open_conflicts(db, project) == [], "一份契约声明一组状态码，不是冲突"

    endpoint["paths"]["/pets"]["get"]["responses"] = {"200": {}, "201": {}}
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "two.json", json.dumps(endpoint)))

    items = open_conflicts(db, project)
    assert len(items) == 1, items
    assert items[0]["field"] == "declared_status", items


# ---------------------------------------------------------------------------
# 判据 3 · undo 之后不留残骸、redo 之后要回来（B）
# ---------------------------------------------------------------------------

def test_undo_and_redo_of_the_conflicting_job_clears_and_restores(db, project,
                                                                 tmp_path):
    """undo 造出冲突的那个作业 → 冲突消失、待办消失；redo → 都回来。

    这是缺陷 B 最直接的形状，走的是**锚 job** 那条路（待办正是这个作业开的）。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    second = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))
    assert len(open_conflicts(db, project)) == 1

    ops.undo(db, job_id=second["job_id"], actor="test")
    assert open_conflicts(db, project) == [], "撤销之后冲突不再存在，待办也不该在"
    assert query.health_report(db, project)["open_inbox"] == 0

    ops.redo(db, job_id=second["job_id"], actor="test")
    items = open_conflicts(db, project)
    assert len(items) == 1, f"redo 之后冲突回来了，待办也必须回来：{items}"
    assert items[0]["field"] == "title", items


def test_undo_of_an_earlier_job_also_clears_the_conflict(db, project, tmp_path):
    """撤销**先写**的那个作业也要收干净 —— 这条只有对账做得到。

    两个作业各写一个值，冲突项是**第二个作业**开的（`created_by_job_id=J2`）；
    现在撤销的是**第一个**作业 J1。`undo` 那条"按 job 关待办"的边挂的是 J1，
    够不到 J2 开的那条 —— 而库里此刻只剩一个值，冲突**已经不存在了**。

    这是"只锚 job 不够"的反证：不跑对账的实现会在这里留下一条残骸。
    """
    first = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))
    items = open_conflicts(db, project)
    assert len(items) == 1 and items[0]["job"] != first["job_id"], \
        "夹具的前提是：冲突项由**后一个**作业开出来"

    ops.undo(db, job_id=first["job_id"], actor="test")
    assert open_conflicts(db, project) == [], \
        "撤销先写的那个作业之后库里只剩一个值，冲突项必须被对账收掉"
    assert query.health_report(db, project)["open_inbox"] == 0

    ops.redo(db, job_id=first["job_id"], actor="test")
    items = open_conflicts(db, project)
    assert len(items) == 1, f"redo 之后冲突回来，待办也必须回来：{items}"


def test_repeated_undo_redo_does_not_grow_the_inbox_table(db, project, tmp_path):
    """反复 undo / redo **不新开行** —— 待办是被重开的，不是每轮插一条。

    只做"关一条、开一条"的实现能让上面两条都绿，而 `inbox_items` 会随撤销
    次数单调增长。残骸不会红（读的是 open 项），它只会变大 —— 所以单独钉一条。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    second = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))
    total_after_import = db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=?", (project,)).fetchone()[0]

    for _ in range(3):
        ops.undo(db, job_id=second["job_id"], actor="test")
        ops.redo(db, job_id=second["job_id"], actor="test")

    total = db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=?", (project,)).fetchone()[0]
    assert total == total_after_import, (
        f"三轮 undo/redo 之后 Inbox 的行数长了：{total_after_import} → {total}")
    assert len(open_conflicts(db, project)) == 1


def test_import_after_a_resolved_conflict_opens_a_new_item(db, project, tmp_path):
    """裁决掉一条冲突之后，值集**变了**就是一个新问题 → 必须重新开一条。

    对账的重开只认**同指纹**（同 subject + 同值集 + 同来源）。值集变了说明这是
    一条不同的冲突 —— 复用旧行会让"我上次已经看过了"。

    ⚠️ 与 3.6 的"清空即干净"不冲突：那条承诺说的是**当下** Inbox 里没有东西，
    不是"以后也不会再有"。新证据产生新待办是它的前提，不是它的反例。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))
    item_id = db.execute(
        "SELECT id FROM inbox_items WHERE project_id=? AND state='open'",
        (project,)).fetchone()[0]
    db.execute("UPDATE inbox_items SET state='resolved', resolved_by='human'"
               " WHERE id=?", (item_id,))
    assert open_conflicts(db, project) == []

    third = ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n3.jsonl", httpx_row("c.example.com", title="Status Page") + "\n"))
    assert third["counts"]["conflicts"] == 1, third["counts"]
    items = open_conflicts(db, project)
    assert len(items) == 1 and "Status Page" in items[0]["values"], items


# ---------------------------------------------------------------------------
# 判据 4 · 3.6 的"清空即干净"在多值导入之后仍然成立
# ---------------------------------------------------------------------------

def test_multivalue_import_leaves_the_health_report_at_zero(db, project, tmp_path):
    """一次多值字段很密的导入之后，体检页五类**全为零**（判据 4 的加强版）。

    3.6 的 `test_clearing_the_inbox_zeroes_all_five` 证的是"**裁决完之后**归零"，
    它管不到"导入一开始就不该有"。这一步买的正是后者：多值字段不再造冲突，
    所以清空 Inbox 这件事在导入那一刻就成立了，而不是靠人一条条裁决。

    夹具三份文件各顶一种多值字段（`path` / `method` + `status` / `cname`），
    全都是夹具自己写的行 —— 这样"五类为 0"里没有任何一类是被真夹具里**别的**
    信号顶起来的（真 ffuf 夹具就带一个未知字段，见下一条用例）。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "gau.txt",
        "https://app.example.com/user/123\n"
        "https://app.example.com/user/456\n"))
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "har.json", json.dumps({"log": {"version": "1.2", "entries": [
            {"request": {"method": "GET", "url": "https://api.example.com/p",
                         "queryString": []},
             "response": {"status": 200}},
            {"request": {"method": "POST", "url": "https://api.example.com/p",
                         "queryString": []},
             "response": {"status": 404}},
        ]}}, ensure_ascii=False)))
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "httpx.jsonl", httpx_row(
            "b.example.com", cname=["x.cdn.net", "y.cdn.net"]) + "\n"))

    after = query.health_report(db, project)
    assert {c["key"]: c["count"] for c in after["categories"]} == {
        c.key: 0 for c in query.HEALTH_CATEGORIES}, after
    assert after["open_inbox"] == 0, after
    assert after["unclaimed"] == [], after["unclaimed"]

    # 而且这三份文件确实各自落了多条观测 —— 否则"五类为 0"可能只是"什么都没导进去"。
    multi = db.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT o.entity_id, o.semantic_field FROM observations o"
        "   WHERE o.project_id=? AND o.is_current=1"
        "     AND o.semantic_field IN ('path','method','status','cname')"
        "   GROUP BY o.entity_id, o.semantic_field HAVING COUNT(DISTINCT o.value) > 1)",
        (project,)).fetchone()[0]
    assert multi >= 4, f"夹具没有把多值字段顶起来，只有 {multi} 组"


def test_httpx_per_encounter_fields_under_many_probes(db, project, tmp_path):
    """同一个 service 被探测 40 次（状态码 / 长度 / 终点各不相同）→ **零**冲突。

    这条是"按字段名字面匹配"的补丁: `status` / `length` / `redirect` 声明之后，
    httpx 用**另外三个名字**写的同一种事实（`status_code` / `content_length` /
    `final_url`）必须一起声明 —— 否则声明表退化成"哪些名字恰好被夹具撞到过"。

    夹具刻意让这三个字段逐次变化（真实 httpx 输出的形状），并且用
    `content_type` 当**对照**: 它没被声明，所以两两不同时**仍然**会开冲突 ——
    一条用例同时证明"该静的静了"与"该响的还响"。
    """
    for i in range(40):
        ingest.ingest_file(db, options(project, tmp_path), write(
            tmp_path, f"p{i}.jsonl", httpx_row(
                "load.example.com", title="Load Test",
                status_code=200 + (i % 4),
                content_length=1000 + i,
                final_url=f"https://load.example.com/after/{i}",
                content_type="text/html" if i % 2 else "application/json") + "\n"))

    items = open_conflicts(db, project)
    # 40 次探测里三个逐次字段各变了 40 种值 —— 一条冲突都不该开。
    assert not [i for i in items if i["field"] in (
        "status_code", "content_length", "final_url")], items
    # 对照：`content_type` 没进声明表，两个值 → 一条真分歧，必须看得见。
    assert [i["field"] for i in items] == ["content_type"], items

    # 逐次字段确实逐次落成了观测，不是被静默丢掉了。
    for field in ("status_code", "content_length", "final_url"):
        values = {str(r[0]) for r in db.execute(
            "SELECT o.value FROM observations o"
            "  JOIN entities e ON e.id = o.entity_id"
            " WHERE o.project_id=? AND o.is_current=1"
            "   AND o.semantic_field=?", (project, field))}
        assert len(values) >= 2, (field, sorted(values))


def test_real_ffuf_export_opens_no_conflict(db, project, tmp_path):
    """**真** ffuf 导出不再刷冲突 —— 3.3 / 3.4 那句"别灌真实导出"的告警可以撤。

    这条的意义与上一条不同：上一条用的是自己写的行，这一条用的是仓库里那份
    真夹具（它刻意带一个未知字段，所以体检页不是全零 —— 那是**另一类**真信号）。
    它要证的就一件事: 多值字段曾经是"大批真实 ffuf 导出不能灌"的**唯一**原因，
    而那一条已经不成立了。告警可以撤，是因为噪声源没了，不是因为噪声被忽略。
    """
    fixture = (Path(__file__).resolve().parent / "fixtures" / "ffuf" / "1"
               / "input.json")
    report = ingest.ingest_file(db, options(project, tmp_path), fixture)
    assert report["parser"] == "ffuf@1", report
    assert report["counts"]["conflicts"] == 0, report["counts"]
    assert [r for r in db.execute(
        "SELECT 1 FROM inbox_items WHERE project_id=? AND kind='value_conflict'",
        (project,))] == []

    # 这个夹具本身的形状：多值字段一次占满，而它们一条冲突都不该开。
    multi = sorted(str(r[0]) for r in db.execute(
        "SELECT DISTINCT o.semantic_field FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind='endpoint'"
        "   AND o.semantic_field IN ('path','method','status','length','duration',"
        "                             'fuzz_input')", (project,)))
    assert multi == ["duration", "fuzz_input", "length", "method", "path", "status"], multi


# ---------------------------------------------------------------------------
# 判据 6 · 替换的是**视图**: 不丢数据、不动 001–009
# ---------------------------------------------------------------------------

def _database_at_version_9(tmp_path) -> sqlite3.Connection:
    """建一个**只应用到 009** 的库（升级路径本身要被测到，不能只测新库）。"""
    conn = store.connect(tmp_path / "old.db")
    conn.execute("PRAGMA foreign_keys = ON")
    original = migrate.MIGRATIONS
    try:
        migrate.MIGRATIONS = tuple(m for m in original if m.version <= 9)
        store.init_db(conn)
    finally:
        migrate.MIGRATIONS = original
    assert migrate.current_version(conn) == 9
    return conn


def test_migration_010_replaces_the_view_without_losing_data(tmp_path):
    """010 换的是**视图**：事实一条不少，老的假冲突项被收掉（判据 6）。

    三件事一起断言，缺一条都只是"看起来升上去了"：

    * 升级前 `v_field_conflicts` 里有 `path`（老判据），升级后没有；
    * 升级**不动事实** —— 实体 / 观测 / 贡献的行数逐张相等（视图是纯派生，
      它换判据不该带走任何一行数据）；
    * 升级**顺手收口**：老库里已经开着的那条假冲突被关掉，而 `resolved_at`
      用的是与 `store.now()` 同形的 ISO8601，不是 `datetime('now')` 的空格格式。
    """
    conn = _database_at_version_9(tmp_path)
    try:
        project = ops.create_project(conn, "upgrade-test", {"include": [], "exclude": []})
        job = ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=project, data_dir=tmp_path, actor="test"), write(
            tmp_path, "gau.txt",
            "https://app.example.com/user/123\n"
            "https://app.example.com/user/456\n"))

        # 升级前：老视图把多值 `path` 记成一条冲突（这正是缺陷 A 的形状）。
        old = [dict(r) for r in conn.execute(
            "SELECT semantic_field FROM v_field_conflicts WHERE project_id=?",
            (project,))]
        assert [r["semantic_field"] for r in old] == ["path"], old
        before = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                  for t in ("entities", "observations", "contributions",
                            "entity_keys", "relations")}
        assert all(v > 0 for v in before.values()), before

        applied = migrate.migrate(conn)
        assert [a["version"] for a in applied] == [10], applied

        # 视图换了判据：`path` 不再算冲突。
        assert conn.execute(
            "SELECT COUNT(*) FROM v_field_conflicts WHERE project_id=?",
            (project,)).fetchone()[0] == 0

        # 事实一行没少。
        after = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                 for t in before}
        assert after == before, f"迁移动了事实层：{before} → {after}"

        # 老库里那条假冲突被收口，且时间戳与 `store.now()` 同形。
        rows = [dict(r) for r in conn.execute(
            "SELECT state, close_reason, resolved_at FROM inbox_items"
            "  WHERE project_id=? AND kind='value_conflict'", (project,))]
        assert len(rows) == 1, rows
        assert rows[0]["state"] == "closed", rows
        assert rows[0]["close_reason"] == "reconcile:010-multivalue", rows
        import re
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                            str(rows[0]["resolved_at"])), rows[0]["resolved_at"]
        assert job["job_id"] > 0
    finally:
        conn.close()


def test_migration_010_extends_the_registry_without_editing_earlier_files():
    """010 是**新增**的一个迁移，001–009 的文件一字未动（R4）。

    老库的校验和由 `migrate()` 在升级时逐条比对（改了文件就 `MigrationError`），
    这条用例把"清单本身"也钉住：版本连续、每条一个文件、`010` 在最后。
    """
    versions = [m.version for m in migrate.MIGRATIONS]
    assert versions == list(range(1, 11)), versions
    names = [m.name for m in migrate.MIGRATIONS]
    assert len(set(names)) == len(names), names
    assert names[-1] == "multivalue_fields", names
    last = migrate.MIGRATIONS[-1]
    assert last.path.name == "010_multivalue_fields.sql", last.path
    assert "DROP VIEW IF EXISTS v_field_conflicts" in last.path.read_text(
        encoding="utf-8")


def test_reconcile_is_idempotent(db, project, tmp_path):
    """对账跑 N 次 = 跑一次（`ops.reconcile_conflicts` 的幂等承诺）。

    它现在在**每一次**导入 / 撤销 / 重做之后都跑，所以"跑两次会不会多开一条"
    必须是一条被测过的性质，而不是一句注释。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n1.jsonl", httpx_row("c.example.com", title="Dashboard") + "\n"))
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "n2.jsonl", httpx_row("c.example.com", title="Login Portal") + "\n"))
    snapshot = all_conflict_rows(db, project)
    assert len(snapshot) == 1

    for _ in range(3):
        report = ops.reconcile_conflicts(db, project_id=project)
        assert report == {"opened": 0, "closed": 0}, report
    assert all_conflict_rows(db, project) == snapshot
