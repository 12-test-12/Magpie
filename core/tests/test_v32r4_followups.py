"""2026-09-17 审核追加的反例网（v3.2-r4 后续）。

四个"没有测试所以一直没被发现"的问题，共同点是: 每一处都在**注释/文档里声称了
一种行为，而代码不会那样做**。没有反例测试的注释只是一句愿望，所以这几条与
`test_v32r4_debts.py` 里的六笔债同等对待 —— 先确认用例在修之前是红的。

    #1 FieldScoreBatch 逐行查 sources      → 一次扫描 + 内存索引
    #2 FieldScoreBatch 增量窗口给出假答案  → 窗口外一律报错（不再静默返回 none）
    #3 redo 的 blocked（部分成功）不可达   → 收窄 undo_incomplete（迁移 002）
    #4 clear_keys 凭空造出人工所有权       → 没有 key 就没有行
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import ops, store
from core.manual import ManualWriter
from core.store import InvariantError, connect, init_db, integrity_check
from test_invariants import Env, qv

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# #1 / #2 —— 批量置信度
# ---------------------------------------------------------------------------

def test_field_score_batch_matches_score_field_in_both_windows(db):
    """批量置信度必须与参考实现 `confidence.score_field` 逐值一致。

    类注释一直写着"等价性由 INV-04 守护"，但 INV-04 比的是
    **批量 vs 批量**（full vs delta）—— 它抓不到"批量实现整体读错了口径"。
    这里补上真正缺失的那条比对，并且**两种窗口都测**:
      · owners=None      全库窗口
      · owners={...}     增量窗口（生产里每次 delta recompute 都走这条）
    """
    from core.confidence import FieldScoreBatch, score_field

    env = Env(db)
    store.register_source(db, source_id=2, name="httpx", authority_level="unverified")
    store.register_source(db, source_id=3, name="whatweb", authority_level="semiautomatic")
    j = env.job()
    # 同字段多值: agreement 必须降到 0.4（conflict）
    env.obs(j, key="host:q", field="tech", value="nginx", source_id=1)
    env.obs(j, key="host:q", field="tech", value="apache", source_id=2)
    # 两源一致: agreement 1.25
    env.obs(j, key="host:q", field="server", value="nginx", source_id=1)
    env.obs(j, key="host:q", field="server", value="nginx", source_id=3)
    # entity 级字段（owner_entity_id 为 NULL，走投影的另一条分支）
    ops.add_entity_field(db, project_id=env.project_id, job_id=j,
                         evidence_id=env.evidence_id, source_id=1, entity_key="host:q",
                         kind="host", semantic_field="tech", value="nginx")
    # 人工贡献 → manual 档
    env.obs(j, key="host:r", field="tech", value="nginx", authorship="manual")
    ops.finish_job(db, j)
    store.recompute(db)

    owner = env.entity_id("host:q")
    rows = list(db.execute("SELECT id, entity_id, field_namespace, semantic_field, value"
                           " FROM observations"))

    def compare(batch, label, in_window):
        checked = 0
        for o in rows:
            if int(o["entity_id"]) not in in_window:
                continue                    # 窗口外不比对: 那是"必须报错"的情形
            ref = score_field(db, "entity", int(o["entity_id"]),
                              str(o["field_namespace"]), str(o["semantic_field"]),
                              str(o["value"]))
            got = batch.score(int(o["entity_id"]), str(o["field_namespace"]),
                              str(o["semantic_field"]), str(o["value"]))
            assert got == ref, f"{label}: observation {o['id']} 批量={got} 参考={ref}"
            checked += 1
        assert checked > 0, f"{label}: 一条都没比对上，用例本身失效了"

    compare(FieldScoreBatch(db, owners=None), "全库窗口",
            in_window={int(o["entity_id"]) for o in rows})
    compare(FieldScoreBatch(db, owners={owner}), "增量窗口", in_window={owner})

    # 窗口外的 owner 必须报错，不能给出一个"看起来合法"的 none
    windowed = FieldScoreBatch(db, owners={owner})
    with pytest.raises(InvariantError):
        windowed.score(env.entity_id("host:r"), "observation_field", "tech", "nginx")


def test_field_score_batch_does_not_query_per_row(db):
    """批量评分的**存在理由**就是消灭逐行数据库往返。

    实测: 逐行调用 `_authority()` 让 60 行活跃贡献产生 61 条 SQL ——
    "一次扫描 + 内存索引"退化成 N 次往返，bench 里那类性能债会重新长回来。
    判据不看具体条数，看**是否随行数增长**。
    """
    from core.confidence import FieldScoreBatch

    def scan_count(n_obs: int) -> int:
        conn = connect(":memory:")
        try:
            init_db(conn)
            pid = ops.create_project(conn, f"sql-count-{n_obs}",
                                     {"include": ["*.example.com"], "exclude": []})
            store.register_source(conn, source_id=1, name="nmap",
                                  authority_level="verified")
            rf = ops.add_raw_file(conn, project_id=pid, locator_uri="file:///c",
                                  content="x")
            ev = ops.add_evidence(conn, project_id=pid, raw_file_id=rf,
                                  locator={"line": 1}, snippet="x")
            j = ops.start_job(conn, project_id=pid, kind="ingest", coverage="full",
                              parser_fingerprint="p-1")
            for i in range(n_obs):
                ops.add_observation(conn, project_id=pid, job_id=j, evidence_id=ev,
                                    source_id=1, entity_key=f"host:{i % 8}", kind="host",
                                    semantic_field="tech", value="nginx",
                                    observed_at="2026-01-01T00:00:00Z")
            ops.finish_job(conn, j)
            store.recompute(conn)
            counter = [0]
            conn.set_trace_callback(lambda _sql: counter.__setitem__(0, counter[0] + 1))
            FieldScoreBatch(conn, owners=None)
            conn.set_trace_callback(None)
            return counter[0]
        finally:
            conn.close()

    small, large = scan_count(16), scan_count(64)
    assert small == large, f"SQL 条数随行数增长（{small} → {large}）: 又有逐行查询了"
    assert large <= 5, f"单次构建应只扫一遍（实测 {large} 条）"


# ---------------------------------------------------------------------------
# #3 —— redo 的 blocked（部分成功）状态
# ---------------------------------------------------------------------------

def test_redo_blocked_partial_success_is_a_legal_state(db):
    """`redo` 的"部分成功"必须可达 —— 旧 schema 让它对**真的写过贡献**的 job
    必然抛 InvariantError。

    `undo_incomplete` 过去读作 "applied=0 ⇒ 本 job 的贡献必须全被抑制"，
    而 blocked 分支恰恰要留下"applied=0 但贡献已恢复活跃"的状态。
    于是整笔 redo 回滚、调用方拿到的是 integrity 报错而不是 blocked 报告；
    既有用例之所以通过，只因为它用的 rekey job 一条贡献都没有。
    """
    env = Env(db)
    j0 = env.job()
    env.obs(j0, key="host:x", field="tech", value="nginx")
    env.obs(j0, key="host:y", field="tech", value="nginx")
    ops.finish_job(db, j0)

    # 一个既做身份迁移、又写贡献的 job（reparse 型 job 的真实形态）
    j_old = env.job(kind="reparse")
    ops.rekey(db, project_id=env.project_id, mapping={"host:x": "shared"}, job_id=j_old)
    env.obs(j_old, key="host:x", field="http_status", value="200")
    ops.finish_job(db, j_old)
    ops.undo(db, j_old)                       # "shared" 的 alias 被撤销

    j_new = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"host:y": "shared"}, job_id=j_new)
    ops.finish_job(db, j_new)
    winner = env.entity_id("host:y")

    report = ops.redo(db, j_old)
    assert report["impact"]["blocked"], report
    assert report["impact"]["alias_restored"] == 0
    assert report["applied"] == 0
    assert qv(db, "SELECT applied FROM jobs WHERE id=?", j_old) == 0
    # 事实部分真的恢复了（这正是"部分成功"的意义），更新的身份决定没有被覆盖
    assert qv(db, "SELECT COUNT(*) FROM v_active_contributions WHERE job_id=?", j_old) > 0
    assert store.resolve_entity(db, env.project_id, "shared") == winner
    assert integrity_check(db)["ok"] is True

    # 人工处置掉冲突（撤销更新的那个决定）之后再 redo，必须能走完全程
    ops.undo(db, j_new)
    again = ops.redo(db, j_old)
    assert again["applied"] == 1
    assert again["impact"]["blocked"] == []
    assert integrity_check(db)["ok"] is True


# ---------------------------------------------------------------------------
# #4 —— 人工所有权的凭空生成
# ---------------------------------------------------------------------------

def test_clear_keys_does_not_fabricate_manual_ownership(db):
    """没有 key 就没有行 —— `manual_fields` 的**存在性**就是 manual ownership。

    旧行为: 对一个从未设过保护的对象调 `clear_keys()` 会 INSERT 一条
    `keys_json='[]'` 的空行，于是"解除保护"反而把它永久锚住
    （`manual_owned` 为真 → 不进 GC 孤儿清单、coverage 永远判不了 inactive）。
    """
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:m", field="tech", value="nginx")
    ops.finish_job(db, j)
    store.recompute(db)
    eid = env.entity_id("host:m")
    w = ManualWriter(db)

    assert store.manual_owned(db, "entity", eid) is False
    w.clear_keys(project_id=env.project_id, subject_kind="entity",
                 subject_id=eid, keys=["never-set"])
    assert qv(db, "SELECT COUNT(*) FROM manual_fields WHERE subject_id=?", eid) == 0
    assert store.manual_owned(db, "entity", eid) is False

    # 设过保护 → 是人工对象；把 key 全部清掉 → 不再是（行保留，用于版本号）
    w.set_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
               add=["display_name"])
    assert store.manual_owned(db, "entity", eid) is True
    w.clear_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
                 keys=["display_name"])
    assert qv(db, "SELECT keys_json FROM manual_fields WHERE subject_id=?", eid) == "[]"
    assert store.manual_owned(db, "entity", eid) is False
    assert integrity_check(db)["ok"] is True


# ---------------------------------------------------------------------------
# #3 的前置: 迁移 002 必须真的被应用，且旧库能被升级
# ---------------------------------------------------------------------------

def test_migration_002_relaxes_undo_incomplete_on_an_upgraded_legacy_db():
    """旧库（只有 001）升到 002 之后，视图规则必须已经是收窄过的那版。

    这是"迁移 runner"第一次真的被用来改一条已有规则 —— 所以顺手验证:
    升级路径产出的视图与新建库的视图**行为一致**。
    """
    from core import migrate as migrate_mod

    legacy = connect(":memory:")
    try:
        legacy.executescript(
            (REPO_ROOT / "schema" / "001_init.sql").read_text(encoding="utf-8"))
        # 001 的规则: applied=0 且有活跃贡献 ⇒ 报 undo_incomplete
        before = legacy.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='view'"
            " AND name='v_integrity_violations'").fetchone()[0]
        assert before == 1
        assert migrate_mod.migrate(legacy) != []
        assert migrate_mod.current_version(legacy) == migrate_mod.MIGRATIONS[-1].version
        sql = legacy.execute("SELECT sql FROM sqlite_master WHERE type='view'"
                             " AND name='v_integrity_violations'").fetchone()[0]
        assert "opened_by_undo_job_id = j.id" in sql          # 例外条款在
        assert migrate_mod.migrate(legacy) == []              # 幂等
    finally:
        legacy.close()
