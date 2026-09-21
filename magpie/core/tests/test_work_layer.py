"""3.8 工作层：批量改状态 / 合并与拆开 / 布局的两列互不串位。

这一批断言与前面几步的关系，写在最前面（不然读者会问"这些为什么不测在
`test_invariants.py` 里"）：

  * **逐行指纹相等**是 2.6 立下的判据（B 组），本文件直接复用
    `store.effective_equivalent` —— 不是另发明一条。它覆盖 `entity_layout` /
    `entity_tags`（`snapshot_effective` 里就有），所以"撤销之后布局与标签原样"
    是**同一条判据**顺带管住的东西，不需要第三条断言。
  * **合并走 `ops.merge` / `ops.unmerge`**：本文件钉的两件事，一件是"拆开之后
    逐行回到合并前"，另一件是**那条分水岭用例** —— 合并之后再导入同一份数据，
    融合的实体不许被重新拆成两个。后者才是"可合并"这个功能能不能用的判据：
    合成完第二天导一次就散开的话，这个功能等于没做。
  * **批量改状态**是**一个 job**（不是 N 个）。为什么，写在 `ops.set_lifecycle`
    的段落注释里；这里只验它的后果：一个事务、幂等、撤销即干净。

前置条件同 `test_invariants.py`：`conn` 必须是 `store.connect()` 建出来的。

运行: python -m pytest magpie/core/tests -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from magpie.core import ingest, ops, query, store                  # noqa: E402
from magpie.core.store import (                                     # noqa: E402
    InvariantError, assert_integrity, connect, effective_equivalent, init_db,
    integrity_check,
)


T0 = "2026-01-01T00:00:00Z"

_SEQ = [0]


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def make_project(conn: sqlite3.Connection, name: str | None = None) -> int:
    _SEQ[0] += 1
    return ops.create_project(conn, name or f"work-{_SEQ[0]}",
                              {"include": ["*.example.com"], "exclude": []})


def seed(conn: sqlite3.Connection, project_id: int, keys: list[str]) -> dict:
    """一个 job + 每个 key 一条观测。返回 {key: entity_id}。

    刻意**不走 ingest 的文件路径**：这一批要验的是"工作层 + 身份层"，
    而 ingest 那一侧的回归由 `test_ingest.py` 与 `verify_v32r5.py` 管 ——
    在同一个用例里同时压两条路，红了之后第一件事是分不清哪条坏了。
    唯一例外是下面那条"合并后再导入"的分水岭用例，它**必须**走真 ingest。
    """
    store.register_source(conn, source_id=1, name="nmap", authority_level="verified")
    raw = ops.add_raw_file(conn, project_id=project_id, locator_uri="file:///seed.txt",
                           content="seed\n", storage_state="present")
    evidence = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw,
                                locator={"line": 1}, snippet="seed",
                                quality_class="high")
    job = ops.start_job(conn, project_id=project_id, kind="scan", coverage="full",
                        coverage_scope={"include": ["*.example.com"], "exclude": []},
                        parser_fingerprint="p-1", actor="cli")
    out = {}
    for key in keys:
        ops.add_observation(conn, project_id=project_id, job_id=job, evidence_id=evidence,
                            source_id=1, entity_key=key, kind="host",
                            semantic_field="tech", value="nginx", observed_at=T0)
        out[key] = store.entity_for_key(conn, project_id, key)
    ops.finish_job(conn, job, "done")
    store.recompute(conn)
    assert_integrity(conn)
    return out


def lifecycles(conn: sqlite3.Connection) -> list[str]:
    return [str(r["lifecycle"]) for r in
            conn.execute("SELECT lifecycle FROM entities ORDER BY id")]


def counts(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ---------------------------------------------------------------------------
# 批量改状态
# ---------------------------------------------------------------------------


def test_batch_lifecycle_is_one_job_and_one_transaction(db):
    """**一次点击 = 一个 job**。这是"要么全成要么全不成"在形状上的落点。"""
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b", "host:c"]).values())

    jobs_before = counts(db, "jobs")
    report = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                               lifecycle="retired", reason="季度清理", actor="cli")

    assert counts(db, "jobs") == jobs_before + 1
    assert report["counts"] == {"requested": 3, "changed": 3, "unchanged": 0}
    assert lifecycles(db) == ["retired"] * 3
    assert report["dry_run"] is False
    # 工作日志是**可撤销**的那一半：一个 job 三条日志，一条不多一条不少。
    assert counts(db, "work_journal") == 3
    assert_integrity(db)


def test_batch_lifecycle_is_idempotent_and_reports_what_changed(db):
    """已经是目标状态的实体不进 `changed`，也不写日志。

    理由不只是省行数：`undo` 报告的"放回去了几条"读的正是这张日志表，
    多写一条就多一条"撤销时摸一下但没改什么"的假动作。
    """
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b"]).values())

    first = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                              lifecycle="retired", actor="cli")
    second = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                               lifecycle="retired", actor="cli")

    assert first["counts"]["changed"] == 2
    assert second["counts"] == {"requested": 2, "changed": 0, "unchanged": 2}
    assert counts(db, "work_journal") == 2, "第二次没有改任何东西，不该再写日志"


def test_batch_lifecycle_all_or_nothing(db):
    """整批一起拒：**一个字都不写**。错误里要列全有问题的 id。

    只报第一个 id 的坏处很具体：调用方改一个跑一次，改一轮跑一轮。
    """
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b"]).values())
    before = store.snapshot_effective(db)
    jobs_before = counts(db, "jobs")

    with pytest.raises(InvariantError) as excinfo:
        ops.set_lifecycle(db, project_id=project_id, entity_ids=[ids[0], 99998, 99999],
                          lifecycle="retired", actor="cli")

    assert "99998" in str(excinfo.value) and "99999" in str(excinfo.value)
    assert store.snapshot_effective(db) == before, "失败之后库必须逐行没变"
    assert counts(db, "jobs") == jobs_before, "注定失败的批量不该留下一个 job"
    assert lifecycles(db) == ["active", "active"]


def test_batch_lifecycle_rejects_a_foreign_project(db):
    """跨项目 = 与不存在同样的响亮。静默跳过会让"我改了 30 个"变成一个没人核对的数。"""
    project_id = make_project(db)
    other = make_project(db)
    ids = list(seed(db, project_id, ["host:a"]).values())
    other_ids = list(seed(db, other, ["host:a"]).values())

    with pytest.raises(InvariantError):
        ops.set_lifecycle(db, project_id=project_id, entity_ids=[*ids, *other_ids],
                          lifecycle="retired", actor="cli")
    assert lifecycles(db) == ["active", "active"]


def test_batch_lifecycle_dry_run_writes_nothing_but_says_the_same_thing(db):
    """干跑与真跑走同一段代码（只有 COMMIT→ROLLBACK），所以**报告同形**、
    且跑完一行不留。LARI-34 的结论在这里逐字复用：一个会说假话的干跑比没有
    干跑更糟，人会照着它的数做判断。"""
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b"]).values())
    jobs_before = counts(db, "jobs")

    dry = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                            lifecycle="retired", actor="cli", dry_run=True)
    assert lifecycles(db) == ["active", "active"], "干跑不许改库"
    assert counts(db, "jobs") == jobs_before, "干跑连 job 都不该留下"
    assert counts(db, "work_journal") == 0
    # 唯一允许不同的两处：`dry_run` 这个旗标，与"这一次真跑会建出哪个 job_id"。
    assert {k: v for k, v in dry.items() if k not in ("dry_run", "job_id")} \
        == {k: v for k, v in ops.set_lifecycle(
            db, project_id=project_id, entity_ids=ids, lifecycle="retired",
            actor="cli").items() if k not in ("dry_run", "job_id")}


def test_batch_lifecycle_undo_redo_restores_row_by_row(db):
    """B 组口径：**逐行指纹相等**，不是只比条数。"""
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b", "host:c"]).values())
    before = store.snapshot_effective(db)

    report = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                               lifecycle="retired", actor="cli")
    job_id = report["job_id"]
    assert lifecycles(db) == ["retired"] * 3

    undo = ops.undo(db, job_id, actor="cli")
    assert undo["impact"]["work_reverted"] == 3
    assert lifecycles(db) == ["active"] * 3
    ok, diff = effective_equivalent(db, before)
    assert ok, f"undo 之后没有逐行回到之前：{diff}"

    redo = ops.redo(db, job_id, actor="cli")
    assert redo["impact"]["work_replayed"] == 3
    assert lifecycles(db) == ["retired"] * 3
    assert_integrity(db)


def test_undo_of_a_batch_is_reentrant(db):
    """再撤一次是**幂等**的：第二次没有东西可放回去，报告如实说 0。"""
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a"]).values())
    job_id = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                               lifecycle="retired", actor="cli")["job_id"]

    ops.undo(db, job_id, actor="cli")
    again = ops.undo(db, job_id, actor="cli")
    assert again["impact"]["work_reverted"] == 0
    assert lifecycles(db) == ["active"]


def test_undo_run_covers_a_batch_too(db):
    """批量改状态走的是**同一份** `ops.undo` —— `undo_jobs` 只是它的信封。

    这条断言是这个模块对"不许另造第二套批量机制"唯一的可执行形式：把 job id
    交给 `undo_jobs`，结果与直接调 `undo` 逐字一样。
    """
    project_id = make_project(db)
    ids = list(seed(db, project_id, ["host:a", "host:b"]).values())
    job_id = ops.set_lifecycle(db, project_id=project_id, entity_ids=ids,
                               lifecycle="retired", actor="cli")["job_id"]

    batch = ops.undo_jobs(db, [job_id], actor="cli")
    assert batch["undone"] == [job_id]
    assert batch["stopped_at"] is None and batch["error"] is None
    assert lifecycles(db) == ["active", "active"]


# ---------------------------------------------------------------------------
# 合并 / 拆开
# ---------------------------------------------------------------------------


def test_unmerge_restores_the_fact_layer_row_by_row(db):
    """**验收（照 verify_v32r5.py 的 B 组形状）**：合并两个实体 → unmerge →
    逐行指纹回到合并前（不是只比条数）。"""
    project_id = make_project(db)
    ids = seed(db, project_id, ["host:a", "host:b"])
    before = store.snapshot_effective(db)

    merged = ops.merge(db, project_id=project_id, survivor_key="host:a",
                       member_keys=["host:b"], actor="cli")
    assert merged["facts"]["before"] == merged["facts"]["after"], "INV-10: 事实层零变更"
    assert db.execute("SELECT merged_into FROM entities WHERE id=?",
                      (ids["host:b"],)).fetchone()["merged_into"] == ids["host:a"]

    ops.unmerge(db, event_id=merged["event_id"], actor="cli")
    ok, diff = effective_equivalent(db, before)
    assert ok, f"unmerge 之后没有逐行回到合并前：{diff}"
    assert_integrity(db)


def test_merge_registers_conflicts_and_unmerge_closes_them(db):
    """合并后同字段多值 → 冲突进 Inbox（**禁止 last-write-wins**）；
    拆开时那些项要被关掉 —— 否则一次已经拆掉的合并在 Inbox 上留着永久的待办。"""
    project_id = make_project(db)
    seed(db, project_id, ["host:a", "host:b"])
    evidence = int(db.execute("SELECT id FROM evidence LIMIT 1").fetchone()["id"])
    job = ops.start_job(db, project_id=project_id, kind="scan", coverage="full",
                        coverage_scope={"include": ["*.example.com"], "exclude": []},
                        parser_fingerprint="p-1", actor="cli")
    # 同一个字段、两个**不同**的值 → 合并之后必须变成一条冲突，而不是"后写的赢"
    for key, title in (("host:a", "控制台"), ("host:b", "邮件网关")):
        ops.add_observation(db, project_id=project_id, job_id=job, evidence_id=evidence,
                            source_id=1, entity_key=key, kind="host",
                            semantic_field="title", value=title, observed_at=T0)
    ops.finish_job(db, job, "done")
    store.recompute(db)

    merged = ops.merge(db, project_id=project_id, survivor_key="host:a",
                       member_keys=["host:b"], actor="cli")
    assert merged["conflicts"], "同字段两个值必须产出冲突登记"
    open_before = db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE kind='merge_conflict' AND state='open'"
    ).fetchone()[0]
    assert open_before == len(merged["conflicts"])

    report = ops.unmerge(db, event_id=merged["event_id"], actor="cli")
    assert report["inbox_closed"] == open_before
    left = db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE kind='merge_conflict' AND state='open'"
    ).fetchone()[0]
    assert left == 0


def test_undo_of_a_merge_brings_the_identity_back(db):
    """撤销**建出这次合并的那个 job**，身份层要一起回去。

    这条与 `unmerge` 不是同一件事，两者都要在：`unmerge` 是"我改主意了，
    拆开"（不进 undo/redo 的生命周期边模型）；`undo` 是"把那次操作撤了"。
    合并这个动作**是一个 job**，所以它必须也能被普通地撤销。
    """
    project_id = make_project(db)
    seed(db, project_id, ["host:a", "host:b"])
    before = store.snapshot_effective(db)

    merged = ops.merge(db, project_id=project_id, survivor_key="host:a",
                       member_keys=["host:b"], actor="cli")
    # `ops.merge` 的报告里没有 job_id —— 撤销它要的是**创建它的那个 job**，
    # 而那条边必须找得到（`entity_merge_events.created_by_job_id`）。
    job_id = int(db.execute(
        "SELECT created_by_job_id FROM entity_merge_events WHERE id=?",
        (merged["event_id"],)).fetchone()["created_by_job_id"])

    report = ops.undo(db, job_id, actor="cli")
    assert report["impact"]["merge_events_reverted"] == 1
    assert db.execute("SELECT state FROM entity_merge_events WHERE id=?",
                      (merged["event_id"],)).fetchone()["state"] == "unmerged"
    ok, diff = effective_equivalent(db, before)
    assert ok, f"撤销合并之后没有逐行回到合并前：{diff}"

    # 撤销之后 still 能 redo 回来（B 组的另一半）
    ops.redo(db, job_id, actor="cli")
    assert db.execute("SELECT state FROM entity_merge_events WHERE id=?",
                      (merged["event_id"],)).fetchone()["state"] == "merged"
    assert_integrity(db)


# ---------------------------------------------------------------------------
# 分水岭：合并之后再导入同一份数据
# ---------------------------------------------------------------------------


def _write_domains(path: Path, hosts: list[str]) -> Path:
    """`domainlist`（subfinder 形状）—— **一个 host 一个实体、没有别的东西**。

    刻意不用 httpx：它会顺带建出 `service` / `product`，于是"图上有几个节点"
    这条判据要先去理解解析器的拓扑。这条用例要看的不是解析器，是**导入那一路
    对身份覆盖的尊重** —— 输入越笨越好。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(hosts) + "\n", encoding="utf-8")
    return path


def _import(conn: sqlite3.Connection, project_id: int, path: Path, tmp_path: Path) -> dict:
    return ingest.ingest_paths(conn, ingest.IngestOptions(
        project_id=project_id, data_dir=tmp_path / "data", parser="domainlist",
        archive="external"), [path])


def test_a_merge_survives_a_reimport_of_the_same_data(db, tmp_path):
    """**分水岭用例**：合并之后再导入同一份数据，融合的实体不许被拆成两个。

    合并本质上是**对身份的覆盖**（identity overlay）。下一次导入同一份数据
    必须尊重这个覆盖 —— 否则"合并"这件事只在当下的界面里成立，第二天导一次
    就散开，而这个功能等于没做。

    这条**必须走真 `ingest`**（其余用例刻意不走）：它是唯一能证明"导入那一路
    真的尊重 overlay"的方式 —— 在 `ops.add_observation` 上验等于在替 ingest
    做假设，而假设正是这条用例要排除的东西。

    判据取**用户看得见的那一层**（`query.graph_payload` 的节点数）：导入前后
    图上的节点数不变，且新事实的 owner 折叠到同一个 canonical entity。
    """
    survivor, member = "a.example.com", "b.example.com"
    project_id = make_project(db, "reimport")
    source = _write_domains(tmp_path / "one.txt", [survivor, member])
    first = _import(db, project_id, source, tmp_path)
    assert [f["state"] for f in first["files"]] == ["done"], first["files"]

    def nodes() -> set[str]:
        return {str(n["entity_key"]) for n in query.graph_payload(
            db, project_id, lens=None, kind=None, q=None, limit=500)["nodes"]}

    def canon_of(key: str) -> int:
        row = db.execute(
            "SELECT i.canonical_entity_id AS c FROM entity_keys k"
            " JOIN entity_identity i ON i.entity_id = k.entity_id"
            " WHERE k.entity_key=?", (key,)).fetchone()
        return int(row["c"])

    entities_before = counts(db, "entities")
    assert {survivor, member} <= nodes(), f"第一次导入没把两个 host 都建出来：{nodes()}"

    merged = ops.merge(db, project_id=project_id, survivor_key=survivor,
                       member_keys=[member], actor="cli")
    after_merge = nodes()
    assert member not in after_merge, "合并之后成员节点不该还在图上"
    assert survivor in after_merge

    # ---- 第二天又导了一次（同一批 host，外加一个新发现的）----
    # 刻意**不是**同一个文件：同一个 sha256 会被 2.5 的幂等规则直接 `skipped`
    # 掉，那样这条用例根本走不到"重新写入这两个 key"那一步 —— 一条被跳过的
    # 导入当然不会拆开任何东西，拿它当证据等于什么都没证明。
    again = _write_domains(tmp_path / "two.txt",
                           [survivor, member, "c.example.com"])
    second = _import(db, project_id, again, tmp_path)
    assert [f["state"] for f in second["files"]] == ["done"], second["files"]

    assert nodes() == after_merge | {"c.example.com"}, (
        "合并被下一次导入拆开了 —— 图上的节点变了："
        f"{sorted(nodes() ^ (after_merge | {'c.example.com'}))}")
    assert counts(db, "entities") == entities_before + 1, (
        "重导只该多出那一个新 host，不该因为已合并的两个 key 再建实体")
    # 更硬的一条：**这两个 key 各自指向的实体，折叠到同一个 canonical**。
    # 只挑这两个（不是"库里的全部"）—— 重导顺带建出的 `c.example.com` 本该
    # 是另一个实体，把它算进来只会让这条断言变成"图里只能有一个节点"。
    assert canon_of(survivor) == canon_of(member), (
        f"重导之后这两个 key 又指回了不同的实体："
        f"{survivor}→{canon_of(survivor)}, {member}→{canon_of(member)}")
    assert db.execute(
        "SELECT state FROM entity_merge_events WHERE id=?",
        (merged["event_id"],)).fetchone()["state"] == "merged", (
        "重导不得悄悄把合并事件标成 unmerged")
    assert_integrity(db)


def test_a_merge_survives_an_undo_redo_cycle_of_the_reimport(db, tmp_path):
    """重导之后撤销**那一次重导**，合并仍然在。

    这条防的是"重导时顺手把 merge 重做了一遍"这种实现 —— 那样撤销重导就会
    把合并一起撤掉，而撤销一次导入**从来不该影响身份覆盖**：它们是两层。
    """
    survivor, member = "a.example.com", "b.example.com"
    project_id = make_project(db, "reimport-undo")
    source = _write_domains(tmp_path / "one.txt", [survivor, member])
    _import(db, project_id, source, tmp_path)
    ops.merge(db, project_id=project_id, survivor_key=survivor,
              member_keys=[member], actor="cli")

    # 换一个 sha256（多一个 host），否则 2.5 的幂等规则会让这次导入 `skipped`
    # —— 那样"撤销这次导入"撤的是一个什么都没写的 job，验不到东西。
    again = _write_domains(tmp_path / "two.txt",
                           [survivor, member, "c.example.com"])
    second = _import(db, project_id, again, tmp_path)
    job_ids = [int(r["job_id"]) for r in second["files"] if r.get("job_id")]
    assert job_ids, second

    ops.undo_jobs(db, job_ids, actor="cli")
    assert db.execute(
        "SELECT COUNT(*) FROM entity_merge_events WHERE state='merged'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM entity_identity WHERE entity_id<>canonical_entity_id"
    ).fetchone()[0] == 1
    assert_integrity(db)


# ---------------------------------------------------------------------------
# 布局 / 标签：两列（行）互不串位
# ---------------------------------------------------------------------------


def test_layout_write_is_per_column(db):
    """**拖位置不改折叠，折叠不改位置。** 3.8 之前这里是真的破的：

    旧的 `PATCH /graph/layout` 是整行覆盖写，而前端拖拽时固定补一个
    `collapsed: false` —— 于是"拖一下已经折叠的节点"会把它悄悄展开，
    而位置与折叠是两个互不相干的人工决定。
    """
    from magpie.core.manual import ManualWriter

    project_id = make_project(db)
    ids = seed(db, project_id, ["host:a"])
    entity_id = ids["host:a"]
    writer = ManualWriter(db, actor="test")

    writer.set_layout(project_id=project_id, entity_id=entity_id, x=10, y=20,
                      collapsed=True)
    # 只拖位置 —— 折叠状态不许被动到
    writer.set_layout(project_id=project_id, entity_id=entity_id, x=99, y=88)
    row = db.execute("SELECT x, y, collapsed FROM entity_layout WHERE entity_id=?",
                     (entity_id,)).fetchone()
    assert (row["x"], row["y"], int(row["collapsed"])) == (99, 88, 1)

    # 只切折叠 —— 坐标不许被动到
    writer.set_layout(project_id=project_id, entity_id=entity_id, collapsed=False)
    row = db.execute("SELECT x, y, collapsed FROM entity_layout WHERE entity_id=?",
                     (entity_id,)).fetchone()
    assert (row["x"], row["y"], int(row["collapsed"])) == (99, 88, 0)


def test_layout_refuses_a_collapsed_only_row_for_a_new_entity(db):
    """只有 collapsed 的写入对一条**不存在**的行是拒绝的。

    放它过去的话，那一行会带着 (0,0) 出生 —— 读侧把它当成"人把这个节点拖到了
    原点"，于是它下次打开就跳到左上角。一个由"新建行时的默认值"造成的、
    看起来像随机漂移的 bug。
    """
    from magpie.core.manual import ManualWriter

    project_id = make_project(db)
    entity_id = seed(db, project_id, ["host:a"])["host:a"]
    writer = ManualWriter(db, actor="test")

    with pytest.raises(InvariantError):
        writer.set_layout(project_id=project_id, entity_id=entity_id, collapsed=True)
    assert counts(db, "entity_layout") == 0
    with pytest.raises(InvariantError):
        writer.set_layout(project_id=project_id, entity_id=entity_id)


def test_tag_removal_keeps_its_audit_trail(db):
    """`entity_tags` 的删除只置 `deleted_at`（软删），`tag_events` 上 add 与
    remove **各留一行**（v3.2 §6.1）。删除也留审计这条不是可选项。"""
    from magpie.core.manual import ManualWriter

    project_id = make_project(db)
    entity_id = seed(db, project_id, ["host:a"])["host:a"]
    writer = ManualWriter(db, actor="tester")
    writer.set_tag(project_id=project_id, entity_id=entity_id, tag="重点")
    writer.remove_tag(project_id=project_id, entity_id=entity_id, tag="重点",
                      reason="看错了")

    assert query.tags_of(db, project_id, entity_id) == []
    assert counts(db, "entity_tags") == 1, "软删：行还在（否则审计也一起没了）"
    events = [(str(r["action"]), str(r["reason"] or "")) for r in db.execute(
        "SELECT action, reason FROM tag_events ORDER BY id")]
    assert events == [("add", ""), ("remove", "看错了")]
    # 词表只算活标签：摘掉的词不该继续在界面里挂着
    assert query.project_tags(db, project_id) == []


def test_project_tags_counts_match_the_entity_filter(db):
    """词表上的计数必须等于 `GET /entities?tag=X` 的实体数 —— 两个数说的是
    同一件事，就该相等（软删的历史行不许混进来）。"""
    from magpie.core.manual import ManualWriter

    project_id = make_project(db)
    ids = seed(db, project_id, ["host:a", "host:b", "host:c"])
    writer = ManualWriter(db, actor="tester")
    for key in ("host:a", "host:b"):
        writer.set_tag(project_id=project_id, entity_id=ids[key], tag="外部资产")
    writer.set_tag(project_id=project_id, entity_id=ids["host:c"], tag="内部资产")
    writer.remove_tag(project_id=project_id, entity_id=ids["host:c"], tag="内部资产")

    vocab = {row["tag"]: row["count"] for row in query.project_tags(db, project_id)}
    assert vocab == {"外部资产": 2}
    # 词表上的数 == `?tag=` 过滤出来的实体数（同一件事，就该相等）
    page = query.dispatch(db, project_id, tag="外部资产", limit=50)
    assert page["count"] == vocab["外部资产"] == 2
    assert {str(e["entity_key"]) for e in page["entities"]} == {"host:a", "host:b"}
