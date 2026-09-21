"""数据层反例审核（LARI-21 · `6406b99` 的 store / ops / confidence / ingest / pipeline）。

判据沿用 2.3 那一轮：**找"跑得起来但答案是错的"** —— 畸形输入不报错，而是被
静默改写成另一个合法值、或者被静默丢掉。每个用例在修之前都真的红过，红在哪
写在各自的 docstring 里。

    #1 ops.undo / ops.redo 对连接的前提从没写下来   → 入口守卫 + 可读错误
    #2 store._owners_of 用写死的 _BATCH 分块         → 跟着连接自己的上限走
    #3 pipeline 重复 id 判不出来 → 静默丢一步        → 按归一化 id 判重
    #4 ingest 坏行计数被明细上限截断                 → 计数与明细分开
    #5 解析器按契约提前返回 = 报 done（截断无信号）  → 回读 cancelled() 标志

另外三条**等价性**守护（不是缺陷，是"改了分块绑定之后必须仍然成立"）:

    A 全量重算在任意分块大小下逐列相同
    B 增量重算（delta）在任意分块大小下与全量一致（INV-04 的小块版）
    C FieldScoreBatch 与参考实现 score_field 逐值相同

"任意分块大小"用**真实手段**制造: SQLite 的 `SQLITE_LIMIT_VARIABLE_NUMBER` 是
连接级、可查询也可下调的（`bind_batch_size` 读的就是它）。压低下限之前先换一条
**全新的连接**——语句缓存是空的，于是每条语句都真的按新上限准备；在旧连接上
下调上限会被缓存里那条"高上限时准备过的语句"绕过，测出来的是假绿。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from magpie.core import ingest, ops, pipeline, store
from magpie.core.confidence import FieldScoreBatch, score_field
from magpie.core.parsers.base import RawEntity, RawObservation
from magpie.core.store import InvariantError, bind_batch_size, connect, init_db, \
    snapshot_cache
from test_invariants import Env, qv

T0 = "2026-01-01T00:00:00Z"

#: 行级 UPDATE（11 个 SET + 1 个 WHERE）要 12 个变量 —— 下限必须高于它，
#: 否则崩的是别的语句，测的就不是 `_owners_of` 了。
LOW_LIMIT = 16


def _fresh_conn_with_low_limit(db_path: Path, limit: int = LOW_LIMIT) -> sqlite3.Connection:
    """新开一条连接并把变量上限压到 `limit`（新连接的语句缓存是空的）。"""
    conn = connect(db_path)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
    assert bind_batch_size(conn) == limit, "上限没落到连接上，用例前提不成立"
    return conn


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection, env: Env, n_obs: int) -> list[int]:
    """n_obs 条观测，每条挂在不同 entity 上（owner 数 = 观测数，分块一定被切开）。"""
    j = env.job()
    for i in range(n_obs):
        env.obs(j, key=f"host:{i}", field="tech", value="nginx")
    env.finish(j)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM observations")]


# ---------------------------------------------------------------------------
# #1 —— undo / redo 的连接前提
# ---------------------------------------------------------------------------


def test_undo_and_redo_require_a_store_connection(tmp_path):
    """裸 `sqlite3.connect` 的连接调 undo / redo 必须是**可读的前提错误**。

    红在哪: 两者都用 `row["contribution_kind"]` 这类按列名取值，而列名来自
    `conn.row_factory = sqlite3.Row` —— 全仓只有 `store.connect()` 设它。
    自己开连接的调用方（2.7 的 FastAPI 层就是现实的例子）拿到的是

        TypeError: tuple indices must be integers or slices, not str

    一个指向函数内部、完全不提"你少了个前提"的错误。
    """
    db_path = tmp_path / "m.db"
    setup = connect(db_path)
    init_db(setup)
    env = Env(setup)
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx")
    env.finish(j)
    setup.close()

    bare = sqlite3.connect(str(db_path), isolation_level=None)
    bare.execute("PRAGMA foreign_keys = ON")
    try:
        assert bare.row_factory is None
        for call in (lambda: ops.undo(bare, j), lambda: ops.redo(bare, j)):
            with pytest.raises(InvariantError) as exc:
                call()
            message = str(exc.value)
            # 必须说清"要什么"和"怎么改"，而不是把内部症状原样抛出来
            assert "store.connect" in message and "row_factory" in message, message
            assert "tuple indices" not in message, message

        # 反例自检（用例的牙齿）: 守卫真的挡住了那次崩溃 —— 绕过守卫直调内部函数，
        # 旧错误照旧出现。若哪天它不再出现，说明这条用例测的不是它以为的东西。
        with pytest.raises(TypeError, match="tuple indices"):
            ops._recompute_subjects_of_job(bare, j)          # noqa: SLF001

        # 另一个方向: 守卫不得误报 —— 错误文案推荐的替代做法必须真的能用
        bare.row_factory = sqlite3.Row
        assert ops.redo(bare, j)["op"] == "redo"
        assert ops.undo(bare, j)["op"] == "undo"     # 幂等：再撤一次仍然合法
    finally:
        bare.close()

    # `store.connect()` 的连接当然不得误报
    good = connect(db_path)
    try:
        report = ops.undo(good, j)
        assert report["op"] == "undo" and report["job_id"] == j
        assert good.execute("SELECT applied FROM jobs WHERE id=?", (j,)).fetchone()[0] == 0
    finally:
        good.close()


# ---------------------------------------------------------------------------
# #2 —— 分块绑定必须跟着连接走（不能只改一半）
# ---------------------------------------------------------------------------


def test_owners_of_respects_the_connection_variable_limit(tmp_path):
    """`_owners_of` 的 `IN (...)` 必须按**连接自己的**上限分块。

    红在哪: `6406b99` 把 `recompute_subset` / `FieldScoreBatch` 都改成了
    `bind_batch_size(conn)`，唯独 `_owners_of` 还写着 `_chunks(ids, _BATCH)`
    （写死 20000）。而 `bind_batch_size` 的 docstring 明说 `SQLITE_MAX_VARIABLE_NUMBER`
    是**编译期**的，可以是 999 —— 那条编译参数下，一次 delta 覆盖 1000 个观测
    就是 `too many SQL variables`。同一批改动里的漏网之鱼。
    """
    db_path = tmp_path / "m.db"
    setup = connect(db_path)
    init_db(setup)
    env = Env(setup)
    ids = _seed(setup, env, n_obs=LOW_LIMIT + 1)     # 17 个 subject > 上限 16
    setup.close()

    conn = _fresh_conn_with_low_limit(db_path)
    try:
        store.recompute(conn, delta=[("observation", i) for i in ids])   # 旧代码在这里崩
        assert store.integrity_check(conn)["ok"] is True
        # 缓存确实被重算过（不是"没崩"而已）
        assert qv(conn, "SELECT COUNT(*) FROM observations WHERE is_current=1") == len(ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A / B / C —— 分块等价性
# ---------------------------------------------------------------------------


def _mixed_env(conn: sqlite3.Connection, env: Env) -> list[int]:
    """冲突 / 一致 / 人工 / 关系 / entity 级字段都齐的一小组事实。

    source 用 `Env` 已经登记好的 1（nmap/verified）、2（httpx/unverified）、
    3（whatweb/unverified）—— authority_term 的三种取值都要走到。
    """
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx", source_id=1)
    env.obs(j, key="host:a", field="tech", value="apache", source_id=2)   # 冲突 → 0.4
    env.obs(j, key="host:a", field="server", value="nginx", source_id=1)
    env.obs(j, key="host:a", field="server", value="nginx", source_id=3)  # 两源一致
    env.obs(j, key="host:m", field="tech", value="iis", authorship="manual")
    for i in range(LOW_LIMIT + 4):
        env.obs(j, key=f"host:s{i}", field="server", value="nginx")
    env.rel(j, src="host:a", dst="host:b")
    ops.add_entity_field(conn, project_id=env.project_id, job_id=j,
                         evidence_id=env.evidence_id, source_id=1, entity_key="host:a",
                         kind="host", semantic_field="tech", value="nginx")
    env.finish(j)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM observations")]


def test_full_recompute_is_invariant_to_chunk_size(tmp_path):
    """全量重算在"上限 16 的连接"上与默认连接**逐列相同**。

    这是对 `6406b99` 那句"累积器共用、逐列语义与单次扫描完全一致"的直接检验：
    块边界上少算一次 `COUNT(DISTINCT)`、漏并一个 owner、或者把 max 做成了
    "最后一块的 max"，都会让两张快照分叉。
    """
    baseline_db, small_db = tmp_path / "a.db", tmp_path / "b.db"
    base = connect(baseline_db)
    init_db(base)
    env = Env(base)
    _mixed_env(base, env)
    store.recompute(base)
    expected = snapshot_cache(base)
    base.close()

    env_db = connect(small_db)
    init_db(env_db)
    env2 = Env(env_db)
    _mixed_env(env_db, env2)
    env_db.close()

    conn = _fresh_conn_with_low_limit(small_db)
    try:
        assert bind_batch_size(conn) < qv(
            conn, "SELECT COUNT(*) FROM observations"), "分块必须真的被切开"
        store.recompute(conn)
        got = snapshot_cache(conn)
        assert got == expected, _first_diff(expected, got)
        assert store.integrity_check(conn)["ok"] is True

        # C: 批量评分与参考实现逐值一致（小块批量也要一致）
        batch = FieldScoreBatch(conn, owners=None)
        checked = 0
        for row in conn.execute("SELECT * FROM observations"):
            ref = score_field(conn, "entity", int(row["entity_id"]),
                              str(row["field_namespace"]), str(row["semantic_field"]),
                              str(row["value"]))
            got_conf = batch.score(int(row["entity_id"]), str(row["field_namespace"]),
                                   str(row["semantic_field"]), str(row["value"]))
            assert got_conf == ref, f"observation {row['id']}: 批量={got_conf} 参考={ref}"
            checked += 1
        assert checked > LOW_LIMIT, "一条都没比对上，用例本身失效了"
    finally:
        conn.close()


def test_delta_recompute_matches_full_recompute_under_small_chunks(tmp_path):
    """INV-04 的小块版: delta 重算（上限 16 的连接）结果 == 全量重算结果。

    旧实现把全部 subject/owner 一次绑进 `IN (...)`，10 万观测的导入直接撞上限；
    改成按块绑定之后，块边界是否与单次扫描等价，只能这样比出来。
    """
    dbfile = tmp_path / "m.db"
    setup = connect(dbfile)
    init_db(setup)
    env = Env(setup)
    ids = _mixed_env(setup, env)
    j = env.job()
    for i in range(3):
        env.obs(j, key=f"host:t{i}", field="title", value=f"t{i}")
    env.finish(j)
    store.recompute(setup)
    expected = snapshot_cache(setup)
    setup.close()

    conn = _fresh_conn_with_low_limit(dbfile)
    try:
        subjects = [("observation", i) for i in ids]
        store.recompute(conn, delta=subjects)
        # 增量结果先与"同一连接上的全量"比，再与基准连接比
        store.recompute(conn)
        assert snapshot_cache(conn) == expected, _first_diff(
            expected, snapshot_cache(conn))
    finally:
        conn.close()


def _first_diff(expected: dict[str, list[Any]],
                got: dict[str, list[Any]]) -> str:
    for table in expected:
        if expected[table] != got[table]:
            before, after = expected[table], got.get(table, [])
            missing = [r for r in before if r not in after][:3]
            extra = [r for r in after if r not in before][:3]
            return (f"{table}: 少了 {len(before)}-{len(after)} 行；"
                    f"missing={missing} extra={extra}")
    return "无差异"


# ---------------------------------------------------------------------------
# #3 —— pipeline 的重复 id
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_pipeline_rejects_duplicate_step_ids_of_any_yaml_scalar_type(tmp_path):
    """重复 id 必须报错 —— 无论它在 YAML 里是字符串还是数字。

    红在哪: 判重拿 YAML 的**原始值**去和 `ids`（装着 `str`）比。`id: 1` 与 `id: 1`
    因为 `1 in ["1"]` 为假而判不出来，于是 `order_steps` 的 `by_id = {str(id): step}`
    让后者覆盖前者 —— 两步的管线**静默跑成一步**，报告里也只剩一步。
    """
    dup = _write_yaml(tmp_path / "dup.yaml", """
name: 两步同 id
project: 某SRC
scope: {include: ["*.example.com"], exclude: []}
steps:
  - id: 1
    parser: domainlist
    source_glob: "recon/*subfinder*.txt"
  - id: 1
    parser: httpx
    source_glob: "recon/*httpx*.jsonl"
""")
    with pytest.raises(ValueError, match="重复"):
        pipeline.load_definition(dup)

    # 归一化到同一个 id 的两种写法（`1` 与 `"1"`）也必须撞上
    mixed = _write_yaml(tmp_path / "mixed.yaml", """
project: 某SRC
steps:
  - id: 1
    source_glob: "a/*.txt"
  - id: "1"
    source_glob: "b/*.txt"
""")
    with pytest.raises(ValueError, match="重复"):
        pipeline.load_definition(mixed)

    # 直接调 order_steps（load_definition 之外的调用方）时，"丢步"就发生在这里
    with pytest.raises(ValueError, match="重复的 id"):
        pipeline.order_steps([{"id": 1, "source_glob": "a"},
                              {"id": 1, "source_glob": "b"}])

    # 反例自检: 合法的两步管线必须**两步都在**（判重不能把正常定义也毙掉）
    ok = _write_yaml(tmp_path / "ok.yaml", """
project: 某SRC
steps:
  - id: 1
    source_glob: "a/*.txt"
  - id: 2
    source_glob: "b/*.jsonl"
    after: [1]
""")
    definition = pipeline.load_definition(ok)
    assert [str(s["id"]) for s in pipeline.order_steps(definition["steps"])] == ["1", "2"]

    # after 里的引用同样按归一化 id 解析（旧写法把 `after: [1]` 判成"不存在"）
    numeric = pipeline.load_definition(_write_yaml(tmp_path / "num.yaml", """
project: 某SRC
steps:
  - id: 10
    source_glob: "a/*.txt"
  - id: 20
    source_glob: "b/*.jsonl"
    after: [10]
"""))
    assert len(pipeline.order_steps(numeric["steps"])) == 2

    # after 的另外两种形态也要有明确行为（此前全仓没有一个 pipeline 用例）:
    #   环  → ValueError（指出环上的步骤），不是静默少跑几步
    #   空  → 正常执行（"没有依赖"不是"依赖没产物"）
    with pytest.raises(ValueError, match="成环"):
        pipeline.order_steps([{"id": "a", "source_glob": "x", "after": ["b"]},
                              {"id": "b", "source_glob": "y", "after": ["a"]}])
    assert [s["id"] for s in pipeline.order_steps([{"id": "solo", "source_glob": "x"}])] \
        == ["solo"]


# ---------------------------------------------------------------------------
# #4 —— 坏行计数
# ---------------------------------------------------------------------------


def test_bad_line_count_is_not_capped_by_the_detail_buffer(db, tmp_path, monkeypatch):
    """`counts.bad_lines` 必须是**真实条数**，不能等于"内存里还留着几条"。

    红在哪: `on_bad_line` 只在自己还装得下时才记账，`counters.bad_lines` 又读
    明细列表的长度 → 一个 10001 条坏行的文件报告 `bad_lines=5000`，落盘报告的头
    也写"共 5000 条"。质量信号在撒谎，而且**没有任何信号**说它被截断了。

    明细上限压到 2（生产值 5000）只是为了跑得快 —— 被测的是"计数与明细解耦"
    这条关系，不是那个数字本身。好行要占够样本的 80%（否则嗅探判不采用，
    文件根本走不到解析那一步）。
    """
    monkeypatch.setattr(ingest, "BAD_LINE_DETAIL_LIMIT", 2)
    project_id = ops.create_project(db, "badlines", {"include": [], "exclude": []})
    path = tmp_path / "mixed.txt"
    good, bad = 40, 5
    path.write_text("".join(f"h{i}.example.com\n" for i in range(good))
                    + "".join(f"!!坏行 {i}\n" for i in range(bad)), encoding="utf-8")

    report = ingest.ingest_file(
        db, ingest.IngestOptions(project_id=project_id, data_dir=tmp_path / "data",
                                 archive="external"), path)

    assert report["state"] == "partial"
    assert report["counts"]["bad_lines"] == bad, report["counts"]
    detail = Path(report["badlines_report"]).read_text(encoding="utf-8").splitlines()
    assert f"共 {bad} 条" in detail[0] and "只保留前 2 条" in detail[0], detail[0]
    assert len(detail) == 1 + 2                       # 头 + 保留的明细
    assert any("只保留了前 2 条" in note for note in report["notes"]), report["notes"]


# ---------------------------------------------------------------------------
# #5 —— 取消：解析器按契约提前返回
# ---------------------------------------------------------------------------


def test_parser_early_return_on_cancel_is_not_reported_as_done(db, tmp_path):
    """中断必须报 cancelled —— 哪怕解析器是**正常返回**的。

    红在哪: `_tracked_lines` 在行边界检查 `cancelled()`，解析器自己也按 §5.3 的
    契约在行边界检查并 `return`。标志恰好在两者之间翻转时（真实世界里由另一个
    线程/请求置位），解析器是 StopIteration 而不是抛 `Cancelled` —— 管线只 catch
    异常，于是这次导入被报成 `state='done'`: 数据少了一半，报告说完成。

    这里用"第 6 次检查起要求中断"把那个窗口**确定性**地复现出来（第 5 次是喂行、
    第 6 次是解析器自己那一次）。
    """
    project_id = ops.create_project(db, "cancel", {"include": [], "exclude": []})
    path = tmp_path / "d.txt"
    path.write_text("".join(f"h{i}.example.com\n" for i in range(20)), encoding="utf-8")

    calls = [0]

    def cancel_check() -> bool:
        calls[0] += 1
        return calls[0] >= 6

    report = ingest.ingest_file(
        db, ingest.IngestOptions(project_id=project_id, data_dir=tmp_path / "data",
                                 archive="external", batch_size=1000,
                                 cancel_check=cancel_check), path)

    assert report["state"] == "cancelled", report
    assert report["counts"]["records"] < 20                 # 确实只导入了一部分
    assert any("提前返回" in note for note in report["notes"]), report["notes"]
    assert report["integrity"]["ok"], report["integrity"]
    # 已导入的部分仍然是一致状态、仍然可 undo（取消语义不变）。
    # 注意数的是 `is_current=1`：entity 行是墓碑保留的，undo 不删行（v3.2-r2）。
    live = "SELECT COUNT(*) FROM entities WHERE project_id=? AND is_current=1"
    assert qv(db, live, project_id) > 0
    ops.undo(db, report["job_id"])
    assert qv(db, live, project_id) == 0
    assert store.integrity_check(db)["ok"] is True


# ---------------------------------------------------------------------------
# #6 —— 连接前提：同一族写入口的其余四个（LARI-21 遗留项①）
# ---------------------------------------------------------------------------


def test_every_identity_writing_entry_point_requires_a_store_connection(tmp_path):
    """`compact` / `merge` / `rekey` / `degrade_evidence`（+ `unmerge`）同一守卫。

    红在哪: LARI-21 当轮只守了 `undo` / `redo`（题面点名的两个），而**同一族**
    的另外几个写入口按列名取值的方式一字不差 —— 自己 `sqlite3.connect()` 的
    调用方走到它们身上，拿到的仍旧是

        TypeError: tuple indices must be integers or slices, not str

    一个指向函数内部、完全不提"你少了个前提"的错误。守一半比一个都不守更糟：
    "有守卫 ⇒ 这条路上都安全"会变成一句不成立的话。

    反例自检（用例的牙齿）: 守卫**真的**挡住了那次崩溃 —— 绕过守卫直调内部
    实现，那个 TypeError 照旧出现。它给的是 LARI-21 那句结论的可复现证据，
    而不只是一句"我加了守卫"。
    """
    db_path = tmp_path / "m.db"
    setup = connect(db_path)
    init_db(setup)
    env = Env(setup)
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx")
    env.finish(j)
    setup.close()

    bare = sqlite3.connect(str(db_path), isolation_level=None)
    bare.execute("PRAGMA foreign_keys = ON")
    try:
        assert bare.row_factory is None
        calls = {
            "ops.compact": lambda: ops.compact(bare, project_id=env.project_id),
            "ops.merge": lambda: ops.merge(bare, project_id=env.project_id,
                                           survivor_key="host:a",
                                           member_keys=["host:b"]),
            "ops.rekey": lambda: ops.rekey(bare, project_id=env.project_id, mapping={}),
            "ops.degrade_evidence": lambda: ops.degrade_evidence(
                bare, raw_file_id=env.raw_file_id, to_state="locator"),
            "ops.unmerge": lambda: ops.unmerge(bare, event_id=1),
        }
        for caller, call in calls.items():
            with pytest.raises(InvariantError) as exc:
                call()
            message = str(exc.value)
            assert caller in message, message
            assert "store.connect" in message and "row_factory" in message, message
            assert "tuple indices" not in message, message

        # 反例自检: 守卫挡住的确实是那次崩溃（不是"顺手加了个 raise"）。
        # 绕过守卫直调**内部实现** —— compact / merge 的体型在这一行，
        # degrade_evidence 的在下一行。
        with pytest.raises(TypeError, match="tuple indices"):
            ops._recompute_subjects_of_job(bare, j)                  # noqa: SLF001
        with pytest.raises(TypeError, match="tuple indices"):
            ops._downgrade_evidence_in_txn(                          # noqa: SLF001
                bare, raw_file_id=env.raw_file_id, to_state="locator",
                actor="selftest", job_id=None, dry_run=True)

        # 另一个方向: 守卫不得误报 —— 错误文案推荐的替代做法必须真的能用。
        # `Env` 建出来的库要先 recompute 一次才算自洽（缓存行是投影，不是事实），
        # 而 `compact` 收尾会 assert_integrity —— 这一步与守卫无关，只是为了
        # 让"没误报"这个结论落在一条**真的跑得通**的调用上。
        bare.row_factory = sqlite3.Row
        store.recompute(bare)
        assert ops.compact(bare, project_id=env.project_id)["op"] == "compact"
        assert ops.degrade_evidence(bare, raw_file_id=env.raw_file_id,
                                    to_state="locator")["op"] == "evidence.degrade"
    finally:
        bare.close()


# ---------------------------------------------------------------------------
# #7 —— added / updated 的键（LARI-21 遗留项②）
# ---------------------------------------------------------------------------


def test_fact_key_carries_the_namespace_and_the_stored_text(db, tmp_path):
    """新增 / 更新的判定键必须与 `_existing_facts` **逐字段同形**。

    红在哪: 键当时只有 `(entity_key, semantic_field, value)`，而 schema 里
    `field_namespace` 是一个真实存在的列（`entity_field` / `observation_field`）。
    于是"同一实体的 display_name"与"一条 type=display_name 的同值观测"撞成
    同一个键：一条**新观测**会被计成 `updated`。库里不受影响（两者写进不同的
    行）、`records` 也守恒 —— 它只错在报告上，而"报告说更新了、其实新增了一条"
    正是最难查的那类错。

    另一半是值：`_write_record` 落库前过 `_value_text`（dict/list 折成 canonical
    JSON），键里若留原始 Python 对象，结构化值就永远对不上、永远被算成 added。
    这里用 `_fact_key` 与 `_existing_facts` 两个**真的会被比对**的东西对撞，
    而不是各自断言一遍自己的形状 —— 同形与否正是这两者之间的关系。
    """
    env = Env(db)
    job = env.job()
    # 同一个实体、同一个 semantic_field、同一个值，两个命名空间各写一条
    ops.add_entity_field(db, project_id=env.project_id, job_id=job,
                         evidence_id=env.evidence_id, source_id=1, entity_key="host:a",
                         kind="host", semantic_field="display_name", value="nginx",
                         observed_at=T0)
    ops.add_observation(db, project_id=env.project_id, job_id=job,
                        evidence_id=env.evidence_id, source_id=1, entity_key="host:a",
                        kind="host", semantic_field="display_name", value="nginx",
                        observed_at=T0)
    env.finish(job)
    store.recompute(db, delta=None)

    fields, _rels = ingest._existing_facts(db, env.project_id)          # noqa: SLF001
    entity_rec = RawEntity(kind="host", key="host:a", display="nginx")
    obs_rec = RawObservation(entity_ref=("host", "host:a"),
                             type="display_name", value="nginx")
    key_e = ingest._fact_key(entity_rec)[1]                              # noqa: SLF001
    key_o = ingest._fact_key(obs_rec)[1]                                # noqa: SLF001

    assert key_e != key_o, "两个命名空间撞成了同一个键"
    assert key_e in fields, f"entity_field 的键对不上库里的读出：{key_e} not in {fields}"
    assert key_o in fields, f"observation_field 的键对不上库里的读出：{key_o} not in {fields}"

    # 结构化值也要能对上：库里存的是 canonical JSON 文本，键里必须也是它
    struct = RawObservation(entity_ref=("host", "host:a"), type="tech",
                            value={"name": "nginx", "v": 2})
    ops.add_observation(db, project_id=env.project_id, job_id=job,
                        evidence_id=env.evidence_id, source_id=1, entity_key="host:a",
                        kind="host", semantic_field="tech",
                        value=store.canonical_json({"name": "nginx", "v": 2}),
                        value_kind="json", observed_at=T0)
    fields, _ = ingest._existing_facts(db, env.project_id)              # noqa: SLF001
    assert ingest._fact_key(struct)[1] in fields, "结构化值的键与落库文本不同形"  # noqa: SLF001

    # 反例自检（用例的牙齿）: 把 namespace 从键里拿掉，两条必须重新撞上 ——
    # 否则上面那条 `!=` 可能只是因为别的字段不同，而不是因为 namespace 真的进了键。
    assert key_e[0] == ingest.ENTITY_NAMESPACE, key_e            # noqa: SLF001
    assert key_o[0] == ingest.OBSERVATION_NAMESPACE, key_o       # noqa: SLF001
    assert key_e[1:] == key_o[1:], \
        "两条记录除了 namespace 之外还有别的差别，这个用例没在测它以为的东西"


# ---------------------------------------------------------------------------
# #8 —— dependency_empty 的判据（LARI-21 遗留项③）
# ---------------------------------------------------------------------------


def test_dependency_empty_counts_production_not_job_creation(tmp_path):
    """`dependency_empty` 问的是"上游有没有产出"，不是"上游有没有建作业"。

    红在哪: 判据当时是 `f.get("job_id")`。而嗅探仲裁失败的文件走
    `_no_parser_report` —— 它为"待人工裁决"**建了一个 job**（state='partial'，
    进 Inbox），却一条事实都没落。于是它的下游拿到的是一个空的上游，报告却说
    依赖是满的。判错这个提示位不会毁掉数据，但会让"下游为什么是空的"失去唯一
    的线索。

    另一半同样重要: `skipped`（幂等跳过）**必须算有产出** —— 跳过恰恰因为同一
    sha256 已被同一 fingerprint 导入过，上游的产物真实存在于库里。
    """
    data_dir = tmp_path / "data"
    empty = tmp_path / "empty.txt"                 # 仲裁不采用任何解析器 → 待人工裁决
    empty.write_bytes(b"")
    good = tmp_path / "subfinder.txt"
    good.write_text("".join(f"h{i}.example.com\n" for i in range(20)), encoding="utf-8")

    definition = {
        "_path": str(tmp_path / "pipeline.yaml"),
        "name": "dependency_empty 用例",
        "project": "dep",
        "scope": {"include": ["*.example.com"], "exclude": []},
        "steps": [
            {"id": "a", "source_glob": "empty.txt"},
            {"id": "b", "source_glob": "subfinder.txt", "after": ["a"]},
        ],
    }
    conn = connect(tmp_path / "m.db")
    init_db(conn)
    try:
        first = pipeline.run_pipeline(conn, definition, base_dir=tmp_path,
                                      data_dir=data_dir)
        steps = {s["id"]: s for s in first["steps"]}
        a_files = steps["a"]["files"]
        assert [f["state"] for f in a_files] == ["needs_confirmation"], a_files
        assert a_files[0]["job_id"] is not None, \
            "前提不成立：待人工裁决的文件本来就没建作业，那就测不到这个缺陷"
        assert steps["b"]["dependency_empty"] is True, \
            "上游一条事实都没落，下游却被告知依赖是满的"

        # 幂等跳过的那一侧：第二次导入，上游的文件已被导入过 → skipped → 算有产出
        second = pipeline.run_pipeline(conn, definition, base_dir=tmp_path,
                                       data_dir=data_dir)
        steps2 = {s["id"]: s for s in second["steps"]}
        assert [f["state"] for f in steps2["b"]["files"]] == ["skipped"], \
            steps2["b"]["files"]
        third = pipeline.run_pipeline(conn, definition, base_dir=tmp_path,
                                      data_dir=data_dir)
        assert all(f["state"] == "skipped"
                   for f in {s["id"]: s for s in third["steps"]}["b"]["files"])
    finally:
        conn.close()
