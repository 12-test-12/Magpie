"""v3.2 不变量测试（第一批代码，不含 GUI / API / CLI）。

pytest 是唯一依赖；装了 hypothesis 会自动启用随机序列属性测试。
运行:  python -m pytest magpie/core/tests -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from magpie.core import manual as manual_mod                        # noqa: E402
from magpie.core import ops, store                                   # noqa: E402
from magpie.core.store import (                                      # noqa: E402
    InvariantError, active_contribution_ids, assert_integrity, audit_strictly_grew,
    connect, days_ago, domain_equivalent, effective_equivalent, history_monotone,
    hours_since, init_db, integrity_check, is_within, parse_time, phantom_cache_rows,
    reset_clock, scope_hash, set_clock, snapshot_audit, snapshot_cache, snapshot_effective,
    snapshot_history,
)

try:                                                          # pragma: no cover
    from hypothesis import HealthCheck, given, settings, strategies as st
    HAS_HYPOTHESIS = True
except Exception:                                             # pragma: no cover
    HAS_HYPOTHESIS = False


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"

_ENV_SEQ = [0]


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        init_db(conn)
    except sqlite3.OperationalError as exc:                   # pragma: no cover
        pytest.skip(f"SQLite too old for schema: {exc}")
    yield conn
    conn.close()


class Env:
    """一个 project + 预建 raw_file/evidence + 按需建 job 的小环境。"""

    def __init__(self, conn: sqlite3.Connection, scope=None, name=None,
                 sources=True):
        self.conn = conn
        # project.name 有 UNIQUE 约束：同一 connection 上建多个 Env 必须给不同名字，
        # 否则跨项目用例在 create_project 就炸掉（旧版 P0-C 用例因此从未真正跑过）。
        _ENV_SEQ[0] += 1
        self.name = name or f"p{_ENV_SEQ[0]}"
        self.scope = scope or {"include": ["*.example.com"], "exclude": []}
        self.project_id = ops.create_project(conn, self.name, self.scope)
        # v3.2-r3: sources 目录表必须真实存在（§8.2 authority_term 的数据源），
        # 且 contributions.source_id 有外键。这里登记 1..4 供测试使用。
        if sources:
            store.register_source(conn, source_id=1, name="nmap", authority_level="verified")
            store.register_source(conn, source_id=2, name="httpx",
                                  authority_level="unverified")
            store.register_source(conn, source_id=3, name="whatweb",
                                  authority_level="unverified")
            store.register_source(conn, source_id=4, name="wappalyzer",
                                  authority_level="unverified")
        self.raw_file_id = ops.add_raw_file(
            conn, project_id=self.project_id, locator_uri=f"file:///raw/{self.name}.txt",
            content="nginx/1.24.0\n", storage_state="present")
        self.evidence_id = ops.add_evidence(
            conn, project_id=self.project_id, raw_file_id=self.raw_file_id,
            locator={"line": 1}, snippet="nginx/1.24.0", quality_class="high")

    def job(self, kind="scan", coverage="full", coverage_scope=None,
            parser_fingerprint="p-1", actor="cli") -> int:
        return ops.start_job(
            self.conn, project_id=self.project_id, kind=kind, coverage=coverage,
            coverage_scope=coverage_scope if coverage_scope is not None else self.scope,
            parser_fingerprint=parser_fingerprint, actor=actor)

    def finish(self, job_id: int, state="done") -> None:
        ops.finish_job(self.conn, job_id, state=state)

    def obs(self, job_id: int, key="host:a", field="tech", value="nginx",
            source_id=1, authorship="tool", **kw) -> int:
        return ops.add_observation(
            self.conn, project_id=self.project_id, job_id=job_id,
            evidence_id=self.evidence_id, source_id=source_id, entity_key=key,
            kind="host", semantic_field=field, value=value, authorship=authorship,
            observed_at=kw.pop("observed_at", T0), **kw)

    def rel(self, job_id: int, src="host:a", dst="host:b", rel="resolves_to",
            source_id=1) -> int:
        return ops.add_relation(
            self.conn, project_id=self.project_id, job_id=job_id,
            evidence_id=self.evidence_id, source_id=source_id, src_key=src,
            dst_key=dst, rel=rel, observed_at=T0)

    def entity_id(self, key="host:a") -> int:
        return store.entity_for_key(self.conn, self.project_id, key)


def q1(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def qv(conn, sql, *args):
    row = q1(conn, sql, *args)
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# schema 层
# ---------------------------------------------------------------------------

def test_schema_builds_and_is_clean(db):
    assert integrity_check(db)["ok"] is True
    assert qv(db, "PRAGMA foreign_keys") == 1


def test_v31_columns_are_gone(db):
    """审阅点 1 / 2: entities 没有 origin；observations/relations 没有 superseded_at。"""
    ent_cols = {r["name"] for r in db.execute("PRAGMA table_info(entities)")}
    assert "origin" not in ent_cols
    assert "is_current" in ent_cols
    assert "is_live" not in ent_cols
    for table in ("observations", "relations"):
        cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        assert "superseded_at" not in cols
        assert "superseded_by_job_id" not in cols


def test_contributions_is_append_only(db):
    """INV-01: 任何 UPDATE contributions 都必须 ABORT。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        db.execute("UPDATE contributions SET field_value='apache'")
    assert "INV-01" in str(exc.value)


# ---------------------------------------------------------------------------
# P0-C 项目隔离（审阅点 5 / 6）
# ---------------------------------------------------------------------------

def test_cross_project_contribution_rejected(db):
    """INV-05/06/07/16: 跨项目写入必须被拒绝，而不是"事后 doctor 发现"。"""
    a = Env(db)
    b = Env(db, scope={"include": ["*.other.net"], "exclude": []})

    ja = a.job()
    # 1) evidence 跨项目 -> INV-07
    with pytest.raises(InvariantError, match="INV-07"):
        ops.add_observation(db, project_id=a.project_id, job_id=ja,
                            evidence_id=b.evidence_id, source_id=1, entity_key="host:a",
                            kind="host", semantic_field="tech", value="nginx")
    # 2) contribution 跨项目 -> INV-05（用 B 的 job + A 的 project）
    with pytest.raises(InvariantError, match="INV-05"):
        store.add_contribution(db, store.NewContribution(
            project_id=a.project_id, job_id=b.job(), source_id=1,
            evidence_id=a.evidence_id,
            subject_kind="entity", subject_id=1, contribution_kind="entity"))
    env_ok = a.obs(ja)
    # 3) subject 跨项目 -> INV-06
    with pytest.raises(InvariantError, match="INV-06"):
        store.add_contribution(db, store.NewContribution(
            project_id=b.project_id, job_id=b.job(), source_id=1, evidence_id=b.evidence_id,
            subject_kind="observation", subject_id=env_ok, owner_entity_id=env_ok,
            contribution_kind="field",
            field_namespace="observation_field", semantic_field="tech",
            field_value="nginx", value_kind="string"))
    # T1: 缓存重算后再做完整性检查（缓存一致性 INV 需要 recompute 之后才成立）
    store.recompute(db)
    assert integrity_check(db)["ok"] is True


def test_cross_project_supersession_is_rejected(db):
    """INV-09 的 supersession 半边（旧版没有落点，可被直接插入跨项目行）。"""
    a, b = Env(db), Env(db, scope={"include": ["*.other.net"], "exclude": []})
    ja = a.job()
    a.obs(ja)
    cid = qv(db, "SELECT id FROM contributions")
    jb = b.job()
    with pytest.raises(InvariantError, match="INV-09"):
        store.supersede(db, cid, by_job_id=jb, reason="retract")


def test_evidence_raw_file_project_is_db_level(db):
    """INV-08 由复合外键在数据库层拒绝（不依赖 store 断言）。"""
    a = Env(db)
    b = Env(db, scope={"include": [], "exclude": []})
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO evidence(project_id, raw_file_id, locator, sha256,"
            " quality_class, recovery_state, created_at) VALUES (?,?,?,?,?,?,?)",
            (b.project_id, a.raw_file_id, "{}", "deadbeef", "low", "full", T0))


# ---------------------------------------------------------------------------
# P0-A / P0-A' 单一事实源 + 真正 append-only（审阅点 1 / 2 / 3）
# ---------------------------------------------------------------------------

def test_is_current_derives_only_from_active_contributions(db):
    env = Env(db)
    j = env.job()
    obs = env.obs(j)
    store.recompute(db)
    assert qv(db, "SELECT is_current FROM observations WHERE id=?", obs) == 1
    assert qv(db, "SELECT is_current FROM entities WHERE id=?", env.entity_id()) == 1

    store.supersede(db, qv(db, "SELECT id FROM contributions WHERE subject_kind='observation'"),
                    by_job_id=j, reason="retract")
    store.recompute(db)
    assert qv(db, "SELECT is_current FROM observations WHERE id=?", obs) == 0
    # entity 仍有自己的 entity 级贡献 -> 依然 current（实体与事实是两个概念）
    assert qv(db, "SELECT is_current FROM entities WHERE id=?", env.entity_id()) == 1


def test_reparse_lifts_supersession_without_touching_contributions(db):
    """审阅点 3: v3.1 的 UPDATE contributions SET superseded_at=NULL 在 v3.2 不存在。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j)
    cid = qv(db, "SELECT id FROM contributions WHERE subject_kind='observation'")
    sid = store.supersede(db, cid, by_job_id=j, reason="reparse")
    assert qv(db, "SELECT is_current FROM observations WHERE id=?", obs) == 0
    store.lift_supersession(db, sid, actor="reparse", job_id=j)
    store.recompute(db)
    assert qv(db, "SELECT is_current FROM observations WHERE id=?", obs) == 1
    # 贡献本身从未被修改（INV-01 也禁止修改）
    assert qv(db, "SELECT COUNT(*) FROM contributions WHERE id=?", cid) == 1


# ---------------------------------------------------------------------------
# P0（v3.2-r2）: observation/relation 的归属事实 + 缓存可重建
# ---------------------------------------------------------------------------

def test_observation_owner_is_recorded_as_a_fact(db):
    """INV-16: contribution → observation → entity 链必须显式落在事实层。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="host:own", field="tech", value="nginx")
    eid = env.entity_id("host:own")
    row = q1(db, "SELECT subject_kind, subject_id, owner_entity_id FROM contributions"
                 " WHERE subject_kind='observation'")
    assert row["subject_kind"] == "observation"
    assert row["subject_id"] == obs
    assert row["owner_entity_id"] == eid
    # 观察缓存行的 entity 与事实一致
    assert qv(db, "SELECT entity_id FROM observations WHERE id=?", obs) == eid
    assert qv(db, "SELECT COUNT(*) FROM v_integrity_violations"
                  " WHERE violation='observation_owner_mismatch'") == 0


def test_contribution_with_wrong_owner_is_rejected(db):
    """INV-16 反向用例: 归属错误/缺失都必须被拒绝（store 层 + 触发器）。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="host:own2", field="tech", value="nginx")
    other = ops.upsert_entity(db, project_id=env.project_id, job_id=j,
                              evidence_id=env.evidence_id, source_id=1,
                              entity_key="host:other", kind="host", observed_at=T0)
    with pytest.raises(InvariantError, match="INV-16"):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=1,
            evidence_id=env.evidence_id, subject_kind="observation", subject_id=obs,
            owner_entity_id=other, contribution_kind="field",
            field_namespace="observation_field", semantic_field="tech",
            field_value="nginx", value_kind="string", observed_at=T0))
    with pytest.raises(InvariantError, match="INV-16"):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=1,
            evidence_id=env.evidence_id, subject_kind="observation", subject_id=obs,
            contribution_kind="field", field_namespace="observation_field",
            semantic_field="tech", field_value="nginx", value_kind="string",
            observed_at=T0))
    # 触发器兜底：绕过 store 直接插也必须 ABORT
    with pytest.raises(sqlite3.IntegrityError, match="INV-16"):
        db.execute(
            "INSERT INTO contributions(project_id, subject_kind, subject_id,"
            " owner_entity_id, source_id, authorship, contribution_kind, field_namespace,"
            " semantic_field, field_value, value_kind, observed_at, observed_at_min,"
            " observed_at_source, evidence_id, job_id, created_at)"
            " VALUES (?,?,?,?,?, 'tool','field','observation_field','tech','nginx',"
            " 'string',?,?,'tool',?,?,?)",
            (env.project_id, "observation", obs, other, 1, T0, T0,
             env.evidence_id, j, T0))


def test_missing_observation_cache_row_is_rebuilt_on_the_right_entity(db):
    """P0（v3.2-r2）: 缓存行可被**正确**重建。

    旧实现把 `c.subject_id` 当 entity_id，实测会 (a) FK 崩塌，或
    (b) 在 id 恰好撞上别的 entity 时把他人的字段静默投影到错误实体上。
    现在归属来自事实列，重建必须落在正确的 entity 上。
    """
    env = Env(db)
    j = env.job()
    obs_a = env.obs(j, key="host:a", field="tech", value="nginx")
    # 让 entity2 存在且带自己的贡献（防止被真空回收）
    eid_b = ops.upsert_entity(db, project_id=env.project_id, job_id=j,
                              evidence_id=env.evidence_id, source_id=1,
                              entity_key="host:b", kind="host", observed_at=T0)
    obs_b = env.obs(j, key="host:a", field="title", value="corp")   # 第二个观察，属于 a
    store.recompute(db)
    assert obs_b != obs_a
    eid_a = env.entity_id("host:a")

    # 模拟缓存丢失：删掉 A 的观察缓存行（事实不动）
    db.execute("DELETE FROM observations WHERE id=?", (obs_b,))
    store.recompute(db)

    restored = q1(db, "SELECT entity_id, semantic_field, value FROM observations WHERE id=?",
                  obs_b)
    assert restored is not None, "缓存行必须能从事实层重建"
    assert restored["entity_id"] == eid_a          # 关键: 不是 eid_b，也不是 obs_b 自己
    assert (restored["semantic_field"], restored["value"]) == ("title", "corp")
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE entity_id=?", eid_b) == 0
    assert integrity_check(db)["ok"] is True


def test_relation_is_rebuildable_from_facts(db):
    """INV-16 对 relation 同样成立: owner=src, dst 在 field_value 里。"""
    env = Env(db)
    j = env.job()
    rel_id = env.rel(j, src="host:a", dst="host:b", rel="resolves_to")
    store.recompute(db)
    before = q1(db, "SELECT src_id, dst_id, rel FROM relations WHERE id=?", rel_id)
    db.execute("DELETE FROM relations WHERE id=?", (rel_id,))
    store.recompute(db)
    after = q1(db, "SELECT src_id, dst_id, rel FROM relations WHERE id=?", rel_id)
    assert after is not None, "relation 缓存行必须能从事实层重建"
    assert tuple(after) == tuple(before)
    assert integrity_check(db)["ok"] is True


def test_observation_interval_is_cached_from_active_contributions(db):
    """observed_at / observed_at_min 是活贡献区间的上/下界（缓存口径唯一）。"""
    env = Env(db)
    j1, j2 = env.job(), env.job()
    o1 = env.obs(j1, key="host:i", field="tech", value="nginx",
                 observed_at="2020-01-01T00:00:00Z")
    o2 = env.obs(j2, key="host:i", field="tech", value="nginx",
                 observed_at="2020-03-01T00:00:00Z")
    assert o1 == o2
    store.recompute(db)
    assert qv(db, "SELECT observed_at FROM observations WHERE id=?", o1) \
        == "2020-03-01T00:00:00Z"
    assert qv(db, "SELECT observed_at_min FROM observations WHERE id=?", o1) \
        == "2020-01-01T00:00:00Z"
    assert qv(db, "SELECT COUNT(*) FROM v_integrity_violations"
                  " WHERE violation='observed_at_interval_mismatch'") == 0


# ---------------------------------------------------------------------------
# P0（v3.2-r2）: parser fingerprint 是已登记的事实，并真正参与 coverage 判定
# ---------------------------------------------------------------------------

def test_parser_fingerprint_is_a_registered_fact(db):
    env = Env(db)
    j = env.job(parser_fingerprint="nmap-svc@1")
    assert qv(db, "SELECT COUNT(*) FROM parser_profiles WHERE fingerprint=?",
              "nmap-svc@1") == 1
    assert qv(db, "SELECT parser_fingerprint FROM jobs WHERE id=?", j) == "nmap-svc@1"
    assert qv(db, "SELECT COUNT(*) FROM activity_log WHERE action='parser.register'") == 1


def test_coverage_uses_project_parser_fingerprint_not_scope_hash(db):
    """旧实现把 job.parser_fingerprint 与 projects.scope_hash 比较（永远 parser_drift）。"""
    env = Env(db, scope={"include": ["*.example.com"], "exclude": [],
                         "parser_fingerprint": "nmap-svc@1"})
    j1 = env.job(parser_fingerprint="nmap-svc@1")
    obs = env.obs(j1, key="host:p", field="tech", value="nginx")
    env.finish(j1)
    store.recompute(db)
    # j2 没有产生任何贡献 → 允许判定 inactive（前提: 同 scope + 同 parser）
    j2 = env.job(parser_fingerprint="nmap-svc@1")
    env.finish(j2)
    assert ops.candidate_inactive(db, observation_id=obs, job_id=j2) == \
        (True, "candidate_inactive")

    # 切换 parser: 必须 bump scope_version + 写历史，旧 job 只能用历史 scope 解释
    info = ops.set_parser(db, env.project_id, "nmap-svc@2")
    assert info["scope_version"] == 2
    assert qv(db, "SELECT parser_fingerprint FROM projects WHERE id=?",
              env.project_id) == "nmap-svc@2"
    assert qv(db, "SELECT COUNT(*) FROM parser_profiles") == 2
    explain = ops.explain_job_scope(db, j2)
    assert explain["interpret_with"] == "historical_scope"
    assert explain["parser_drift"] is True
    assert explain["coverage_usable_for_inactive"] is False
    assert ops.candidate_inactive(db, observation_id=obs, job_id=j2) == \
        (False, "scope_version_drift")


def test_canonical_key_duplication_is_project_scoped(db):
    """duplicate_canonical_key 只在 project 内成立（旧实现全库分组 → 多项目误报）。"""
    a = Env(db)
    b = Env(db, scope={"include": ["*.other.net"], "exclude": []})
    for env in (a, b):
        j = env.job()
        env.obs(j, key="host:same", field="tech", value="nginx")
        env.finish(j)
    store.recompute(db)
    report = integrity_check(db)
    assert report["ok"], report["violations"]
    assert qv(db, "SELECT COUNT(*) FROM entity_keys WHERE entity_key='host:same'") == 2


# ---------------------------------------------------------------------------
# P0-B 依赖闭包 + GC（审阅点 4 / v3.2-r2 收紧）—— 本版最关键的反例测试
# ---------------------------------------------------------------------------

def test_undo_does_not_delete_entity_still_referenced(db):
    """审阅点 4 的反例:

        Job A 创建 Entity E
        Job B 创建 Observation O -> E
        undo A  ->  E 没有自己的 active contribution，
                    但 O 仍 live 且以真 FK 引用 E
        正确行为: E 被保留（v3.1 会 DELETE entity 并级联删掉 O）
    """
    env = Env(db)
    ja = env.job()
    env.obs(ja, key="host:only-a", field="tech", value="nginx")
    env.finish(ja)
    eid = env.entity_id("host:only-a")
    # Job B 在 E 上再加一条字段贡献
    jb = env.job()
    env.obs(jb, key="host:only-a", field="title", value="corp")
    env.finish(jb)
    assert env.entity_id("host:only-a") == eid

    store.recompute(db)
    report = ops.undo(db, ja)
    # E 不再是 purge 候选（Job B 的贡献让它保持 current），二级数据完好
    assert f"entity:{eid}" not in report["impact"]["purge_eligible"]
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE entity_id=? AND is_current=1",
              eid) == 1
    assert integrity_check(db)["ok"] is True


def test_undo_retains_entity_blocked_by_live_observation(db):
    """同一反例的强版本: E 已不是 current（GC 候选），但 O 仍有活贡献引用 E。

    这里直接构造"E is_current=0 且 O is_current=1"的状态来攻击依赖闭包 ——
    早期实现把"blocker 是否存在"与"blocker 是否在候选集合里"混为一谈，
    会把这类 E 删掉（然后借真 FK 级联删掉 O）。"""
    env = Env(db)
    ja = env.job()
    eid = ops.upsert_entity(db, project_id=env.project_id, job_id=ja,
                            evidence_id=env.evidence_id, source_id=1,
                            entity_key="host:anchor", kind="host", observed_at=T0)
    env.obs(ja, key="host:anchor", field="tech", value="nginx")
    env.finish(ja)
    store.recompute(db)
    assert qv(db, "SELECT is_current FROM entities WHERE id=?", eid) == 1

    # 模拟"E 自己的贡献全部被抑制，但 O 的贡献仍活"的中间状态
    db.execute("UPDATE entities SET is_current = 0 WHERE id = ?", (eid,))
    plan = store.purge_eligible(db)
    assert f"entity:{eid}" not in plan["purge"]
    blockers = {r["subject"]: r["blockers"] for r in plan["retained"]}
    assert f"entity:{eid}" in blockers, plan
    assert any(b.startswith("observation:") for b in blockers[f"entity:{eid}"])
    # 依赖仍在，二级数据一行未动
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE entity_id=?", eid) == 1
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1


def test_gc_releases_subject_from_effective_state_without_destroying_history(db):
    """迭代到不动点 → v3.2-r2 语义: "释放" = 退出 effective state，而不是物理删除。

    墓碑行（被历史贡献解释的行）必须保留：它就是 INV-03 的落点。
    物理删除只对"从未有过任何贡献"的残件开放，且默认走 inbox 人工确认（§2.3）。
    """
    env = Env(db)
    ja = env.job()
    env.obs(ja, key="host:solo", field="tech", value="nginx")
    env.finish(ja)
    eid = env.entity_id("host:solo")
    store.recompute(db)
    report = ops.undo(db, ja)
    purge = report["impact"]["purge_eligible"]
    # 默认不物理删除任何缓存行
    assert purge == [], purge
    retained = {r["subject"]: r["blockers"] for r in report["impact"]["purge_retained"]}
    assert f"entity:{eid}" in retained, retained
    assert any(b.startswith("tombstone:observation:") for b in retained[f"entity:{eid}"])
    # 有效状态里已经没有它
    assert qv(db, "SELECT is_current FROM entities WHERE id=?", eid) == 0
    assert qv(db, "SELECT is_current FROM observations WHERE entity_id=?", eid) == 0
    # 但事实层与缓存层都一行未删（历史可解释）
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1
    assert qv(db, "SELECT COUNT(*) FROM contributions") > 0
    assert integrity_check(db)["ok"] is True


def test_gc_orphan_purge_is_opt_in_only(db):
    """"从未有过贡献"的残件默认不删（§2.3 孤儿反转规则）；显式人工 GC 才允许。"""
    env = Env(db)
    eid = store.new_entity(db, project_id=env.project_id, kind="host",
                          entity_key="host:leftover")
    store.recompute(db)
    plan = store.purge_eligible(db)
    assert plan["purge"] == []
    assert f"entity:{eid}" in plan["orphan_subjects"]
    forced = store.purge_eligible(db, include_orphans=True)
    assert f"entity:{eid}" in forced["purge"]


def test_manual_owned_entity_never_purged(db):
    """INV-02: 人工对象永不被物理删除；且 manual ownership 由 provenance 推导。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    db.execute("INSERT INTO entity_tags(project_id, entity_id, tag, set_by, set_at)"
               " VALUES (?,?,?,?,?)", (env.project_id, eid, "crown-jewel", "analyst", T0))
    env.finish(j)
    store.recompute(db)
    report = ops.undo(db, j)
    assert f"entity:{eid}" in [r["subject"] for r in report["impact"]["purge_retained"]]
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1


def test_orphan_subject_is_reported_not_deleted(db):
    """v3.1 漏洞: "没有活贡献" 不等于 "可以删除"。从未有过贡献的对象只进 inbox。"""
    env = Env(db)
    eid = store.new_entity(db, project_id=env.project_id, kind="host",
                          entity_key="host:hand-made")
    store.recompute(db)
    plan = store.purge_eligible(db)
    assert f"entity:{eid}" not in plan["purge"]
    assert f"entity:{eid}" in plan["orphan_subjects"]
    # 行本身必须还在（否则 rowid 复用会让残留人工状态静默易主）
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1
    assert store.entity_for_key(db, env.project_id, "host:hand-made") == eid


def test_inv13_effective_snapshot_excludes_tombstones_but_keeps_working_state(db):
    """effective state 的定义：只有 is_current=1 的缓存行 + 人工状态 + open inbox。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:eff", field="tech", value="nginx")
    eid = env.entity_id("host:eff")
    db.execute("INSERT INTO entity_tags(project_id, entity_id, tag, set_by, set_at)"
               " VALUES (?,?,?,?,?)", (env.project_id, eid, "crown-jewel", "analyst", T0))
    env.finish(j)
    store.recompute(db)
    snap = snapshot_effective(db)
    assert any(row[0] == eid for row in snap["entities"])
    assert len(snap["entity_tags"]) == 1
    ops.undo(db, j)
    snap_after = snapshot_effective(db)
    assert snap_after["entities"] == []
    assert snap_after["observations"] == []
    # 人工状态不受 undo 影响
    assert len(snap_after["entity_tags"]) == 1


# ---------------------------------------------------------------------------
# P0-E audit != domain state（审阅点 19）
# ---------------------------------------------------------------------------

def test_undo_all_restores_effective_state_but_history_and_audit_grow(db):
    """INV-13（v3.2-r2 重写）+ INV-14:

        (a) effective state 必须逐列回到初始值；
        (c) 事实历史只允许**增长**（G1: contributions 不可删，因此不可能回到初始行集）；
        (d) 活跃贡献集合回到初始值；
        (b) 无幻影行 / 无悬空；
        且 audit state 必须严格增长（undo 本身是历史事实）。
    """
    env = Env(db)
    baseline_effective = snapshot_effective(db)
    baseline_history = snapshot_history(db)
    baseline_active = active_contribution_ids(db)
    baseline_audit = snapshot_audit(db)

    jobs = [env.job() for _ in range(4)]
    for i, j in enumerate(jobs):
        env.obs(j, key=f"host:h{i}", field="tech", value="nginx", source_id=i + 1)
    for j in jobs:
        env.finish(j)
    store.recompute(db)
    assert snapshot_effective(db) != baseline_effective

    for j in reversed(jobs):
        ops.undo(db, j)

    ok, diff = effective_equivalent(db, baseline_effective)
    assert ok, diff
    ok_hist, hist = history_monotone(db, baseline_history)
    assert ok_hist, hist
    assert snapshot_history(db)["contributions"] > baseline_history["contributions"]
    assert active_contribution_ids(db) == baseline_active
    assert phantom_cache_rows(db) == []
    grew, detail = audit_strictly_grew(db, baseline_audit)
    assert grew, detail
    assert integrity_check(db)["ok"] is True


def test_inv13_scope_is_job_derived_operations_only(db):
    """INV-13 的适用范围（v3.2-r3 写死）:

    只对**可回滚的 job-derived 操作序列**断言；独立 manual mutation 不属于该序列，
    undo 不得改变它。因此基线的定义是"该序列开始之前的 effective state"，
    而不是"数据库最初的样子"。
    """
    env = Env(db)
    # ① 序列之外的 job：不在 Σ 里
    j0 = env.job()
    env.obs(j0, key="host:a", field="tech", value="nginx")
    ops.finish_job(db, j0)
    store.recompute(db)
    eid = env.entity_id("host:a")
    # ② 独立 manual mutation（不在 Σ 里）
    w = manual_mod.ManualWriter(db, actor="analyst")
    w.set_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
               add=["display_name"])
    w.set_tag(project_id=env.project_id, entity_id=eid, tag="crown-jewel")

    baseline = snapshot_effective(db)          # Σ 开始前的 effective state
    # ③ Σ = 两个 job（可回滚）
    j1, j2 = env.job(), env.job()
    env.obs(j1, key="host:b", field="tech", value="apache")
    env.obs(j2, key="host:c", field="tech", value="nginx")
    ops.finish_job(db, j1)
    ops.finish_job(db, j2)
    store.recompute(db)
    for j in (j2, j1):
        ops.undo(db, j)

    ok, diff = effective_equivalent(db, baseline)
    assert ok, diff
    # manual mutation 未被 undo 触碰，且仍在有效状态里
    assert w.read_keys(project_id=env.project_id, subject_kind="entity",
                       subject_id=eid) == ["display_name"]
    assert snapshot_effective(db)["entity_tags"] == baseline["entity_tags"]
    assert snapshot_effective(db)["manual_fields"] == baseline["manual_fields"]
    assert integrity_check(db)["ok"] is True


def test_undo_keeps_tombstones_but_they_are_not_effective_state(db):
    """INV-13(a)/(b) 的分界: 墓碑行**存在**（历史贡献仍指向它），但**不算有效状态**。

    这正是旧 INV-13（"整个 domain state 等于初始"）与 G1 冲突的地方：
    旧表述永远不可能成立 —— 因为 contributions / supersessions 只增不减。
    """
    env = Env(db)
    baseline_effective = snapshot_effective(db)
    j = env.job()
    env.obs(j, key="host:tomb", field="tech", value="nginx")
    env.finish(j)
    store.recompute(db)
    eid = env.entity_id("host:tomb")
    assert snapshot_effective(db) != baseline_effective
    ops.undo(db, j)

    # 有效状态回到初始（空）
    ok, diff = effective_equivalent(db, baseline_effective)
    assert ok, diff
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE is_current=1") == 0
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE is_current=1") == 0
    # 墓碑行仍在，并被贡献解释（不是幻影）
    assert qv(db, "SELECT COUNT(*) FROM entities WHERE id=?", eid) == 1
    assert qv(db, "SELECT COUNT(*) FROM contributions WHERE subject_kind='entity'"
                  " AND subject_id=?", eid) > 0
    assert phantom_cache_rows(db) == []
    assert integrity_check(db)["ok"] is True


def test_undo_dry_run_changes_nothing(db):
    """dry-run 必须走同一代码路径，且真的什么都没改。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    env.finish(j)
    store.recompute(db)
    before = (snapshot_cache(db), snapshot_history(db), snapshot_audit(db))
    report = ops.undo(db, j, dry_run=True)
    assert report["dry_run"] is True
    assert (snapshot_cache(db), snapshot_history(db), snapshot_audit(db)) == before


# ---------------------------------------------------------------------------
# INV-04 recompute 等价（§9.3）
# ---------------------------------------------------------------------------

def test_full_and_delta_recompute_are_column_equal(db):
    """INV-04: 同一序列下，完整 impact_set 的增量重算必须与全量重算逐列相等。

    注意契约: INV-04 要求的是"delta(impact_set)"，impact_set 必须覆盖本次操作
    触及的全部 subject（§4.2）。这里手工给出完整集合；操作层由
    `_recompute_subjects_of_job` / `_closure` 负责算全。
    """
    env = Env(db)
    j1, j2 = env.job(), env.job()
    env.obs(j1, key="host:a", field="tech", value="nginx", source_id=1)
    env.obs(j2, key="host:a", field="tech", value="apache", source_id=2)
    env.obs(j2, key="host:a", field="title", value="corp", source_id=2)
    rel_id = env.rel(j2)
    eid = env.entity_id("host:a")
    obs_ids = [int(r["id"]) for r in db.execute("SELECT id FROM observations ORDER BY id")]
    store.recompute(db)                       # 先让缓存处于一致状态
    store.recompute(db, delta=[("observation", o) for o in obs_ids]
                    + [("relation", rel_id), ("entity", eid)])
    delta_state = snapshot_cache(db)
    store.recompute(db)
    assert snapshot_cache(db) == delta_state


# ---------------------------------------------------------------------------
# 审阅点 17: confidence 不是多数票
# ---------------------------------------------------------------------------

def test_single_high_quality_source_beats_many_weak_ones(db):
    """P-17a / P-17b（v3.2-r3: authority 来自真实的 sources 目录表）。"""
    from magpie.core.confidence import discretize, score_field

    env = Env(db)
    j = env.job()
    # 单源 + 高证据质量 + verified
    obs_a = env.obs(j, key="host:a", field="tech", value="nginx", source_id=1)
    store.recompute(db)
    eid_a = env.entity_id("host:a")
    strong, _ = score_field(db, "entity", eid_a, "observation_field", "tech", "nginx")
    assert strong == "high", strong

    # 三源弱证据（httpx + WhatWeb 之类）不允许跳到 high
    env.raw_file_id = ops.add_raw_file(db, project_id=env.project_id,
                                      locator_uri="file:///raw/weak.txt", content="weak")
    weak_ev = ops.add_evidence(db, project_id=env.project_id, raw_file_id=env.raw_file_id,
                               locator={"line": 1}, snippet="weak", quality_class="low")
    obs_b = env.obs(j, key="host:b", field="server", value="nginx", source_id=2)
    eid_b = env.entity_id("host:b")
    for sid in (3, 4):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=sid, evidence_id=weak_ev,
            subject_kind="observation", subject_id=obs_b, owner_entity_id=eid_b,
            contribution_kind="field",
            field_namespace="observation_field", semantic_field="server",
            field_value="nginx", value_kind="string", observed_at=T0,
            observed_at_source="tool", observed_at_accuracy="unknown"))
    store.recompute(db)
    weak, _ = score_field(db, "entity", eid_b, "observation_field", "server", "nginx")
    assert weak in ("low", "medium"), weak
    assert weak != "high"
    assert discretize(9.9, has_conflict=False, manual=False) == "high"


def test_confidence_uses_per_field_agreement_not_global_vote(db):
    """agreement 落在 (entity, namespace, field, value) 上 —— 不是 subject 全局投票。

    v3.2-r2: 主体必须是 entity。若以 observation 行（身份含 value）为主体，
    同一字段的多个取值永远落在不同主体上，冲突档位不可达。
    """
    env = Env(db)
    j1, j2 = env.job(), env.job()
    env.obs(j1, key="host:a", field="tech", value="nginx", source_id=1)
    env.obs(j2, key="host:a", field="tech", value="apache", source_id=2)
    env.obs(j2, key="host:a", field="title", value="corp", source_id=2)
    store.recompute(db)
    conf = qv(db, "SELECT confidence FROM observations WHERE semantic_field='tech'"
                  " AND value='nginx'")
    assert conf == "conflict", conf
    # 另一个字段没有冲突 → 不因为"这个实体有多个来源"而全局投票
    assert qv(db, "SELECT confidence FROM observations WHERE semantic_field='title'") != "conflict"
    # 不同字段的冲突不会折叠成同一条 open item（fingerprint 含 conflict_field）
    eid = env.entity_id("host:a")
    ops._open_inbox(db, project_id=env.project_id, kind="value_conflict",
                    subject_kind="entity", subject_id=eid,
                    values=["a", "b"], sources=[1], conflict_field="tech")
    ops._open_inbox(db, project_id=env.project_id, kind="value_conflict",
                    subject_kind="entity", subject_id=eid,
                    values=["a", "b"], sources=[1], conflict_field="title")
    assert qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"
                  " AND kind='value_conflict'") == 2


# ---------------------------------------------------------------------------
# 审阅点 16: inbox 唯一性
# ---------------------------------------------------------------------------

def test_repeated_conflict_does_not_duplicate_open_items(db):
    env = Env(db)
    for i in range(20):
        j = env.job()
        env.obs(j, key="host:a", field="tech",
                value="nginx" if i % 2 == 0 else "apache", source_id=1 + (i % 2))
    store.recompute(db)
    ops.refresh_observation_conflicts(db, env.project_id)
    opened = qv(db, "SELECT COUNT(*) FROM inbox_items WHERE kind='value_conflict'"
                    " AND state='open'")
    assert opened == 1, opened
    total = qv(db, "SELECT occurrence_count FROM inbox_items WHERE state='open'")
    assert total >= 1


def test_partial_unique_index_blocks_duplicate_open_items(db):
    env = Env(db)
    cols = ("project_id, kind, subject_kind, subject_id, fingerprint, severity, state,"
            " occurrence_count, auto_generated, created_at")
    vals = (env.project_id, "value_conflict", "entity", 1, "fp-1", "high", "open", 1, 1, T0)
    db.execute(f"INSERT INTO inbox_items({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"INSERT INTO inbox_items({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
    # 关闭后允许重开（同一个冲突再次出现）
    db.execute("UPDATE inbox_items SET state='resolved'")
    db.execute(f"INSERT INTO inbox_items({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", vals)


# ---------------------------------------------------------------------------
# 审阅点 11 / 12: rekey 与 merge
# ---------------------------------------------------------------------------

def test_rekey_is_identity_only(db):
    """rekey 零外键迁移: 引用仍指向同一 entity_id，old key 作为别名继续解析。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="/report/2024", field="status", value="ok")
    eid = env.entity_id("/report/2024")
    store.recompute(db)
    env.finish(j)

    report = ops.rekey(db, project_id=env.project_id,
                       mapping={"/report/2024": "/report/{id}"}, job_id=env.job())
    assert report["identity_rewritten"] == 1
    # 引用未迁移，但解析相同
    assert qv(db, "SELECT entity_id FROM observations WHERE id=?", obs) == eid
    assert store.entity_for_key(db, env.project_id, "/report/2024") == eid
    assert store.entity_for_key(db, env.project_id, "/report/{id}") == eid
    assert qv(db, "SELECT canonical_key FROM entity_identity WHERE entity_id=?", eid) \
        == "/report/2024"          # 原始 key 冻结，规范 key 走 alias 解析
    assert integrity_check(db)["ok"] is True


def test_rekey_dry_run_matches_real_run(db):
    env = Env(db)
    j = env.job()
    env.obs(j, key="/report/2024", field="status", value="ok")
    env.finish(j)
    before = (snapshot_cache(db), snapshot_history(db), snapshot_audit(db))
    ops.rekey(db, project_id=env.project_id, mapping={"/report/2024": "/report/{id}"},
              job_id=j, dry_run=True)
    assert (snapshot_cache(db), snapshot_history(db), snapshot_audit(db)) == before
    after_report = ops.rekey(db, project_id=env.project_id,
                             mapping={"/report/2024": "/report/{id}"}, job_id=j)
    assert after_report["pairs"][0]["new"] == "/report/{id}"


def test_rekey_collision_becomes_merge_not_error(db):
    env = Env(db)
    j = env.job()
    env.obs(j, key="/report/2024", field="status", value="ok")
    env.obs(j, key="/report/{id}", field="status", value="ok")
    env.finish(j)
    report = ops.rekey(db, project_id=env.project_id,
                       mapping={"/report/2024": "/report/{id}"}, job_id=j)
    assert len(report["merges"]) == 1
    survivor = report["merges"][0]["survivor_id"]
    member = report["merges"][0]["member_id"]
    assert store.canonical_of(db, member) == survivor
    assert integrity_check(db)["ok"] is True


def test_merge_does_not_move_facts_and_unmerge_is_exact(db):
    """INV-10: merge / unmerge 前后事实层逐行不变（行数 + 行内容）。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx")
    env.obs(j, key="host:b", field="tech", value="nginx")
    env.finish(j)
    store.recompute(db)

    facts_before = {t: store.snapshot_table(db, t, normalize_volatile=False)
                    for t in store.FACT_TABLES}
    report = ops.merge(db, project_id=env.project_id, survivor_key="host:a",
                       member_keys=["host:b"], job_id=j)
    assert report["facts"]["before"] == report["facts"]["after"]
    assert {t: store.snapshot_table(db, t, normalize_volatile=False)
            for t in store.FACT_TABLES} == facts_before

    a = env.entity_id("host:a")
    b = env.entity_id("host:b")
    assert store.canonical_of(db, b) == a
    # 观察行仍归属各自 entity
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE entity_id=?", b) == 1

    ops.unmerge(db, event_id=report["event_id"])
    assert store.canonical_of(db, b) == b
    assert {t: store.snapshot_table(db, t, normalize_volatile=False)
            for t in store.FACT_TABLES} == facts_before
    assert integrity_check(db)["ok"] is True


def test_merge_chain_resolves_transitively(db):
    env = Env(db)
    j = env.job()
    for k in ("host:a", "host:b", "host:c"):
        env.obs(j, key=k, field="tech", value="nginx")
    env.finish(j)
    store.recompute(db)
    ops.merge(db, project_id=env.project_id, survivor_key="host:a",
              member_keys=["host:b"], job_id=j)
    ops.merge(db, project_id=env.project_id, survivor_key="host:b",
              member_keys=["host:c"], job_id=j)
    assert store.canonical_of(db, env.entity_id("host:c")) == env.entity_id("host:a")
    assert integrity_check(db)["ok"] is True


def test_merge_conflict_never_last_write_wins(db):
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx")
    env.obs(j, key="host:b", field="tech", value="apache")
    env.finish(j)
    report = ops.merge(db, project_id=env.project_id, survivor_key="host:a",
                       member_keys=["host:b"], job_id=j)
    assert len(report["conflicts"]) == 1
    values = report["conflicts"][0]["values"]
    assert set(values) == {"apache", "nginx"}
    # 冲突仍在缓存里，没有被自动裁决
    assert qv(db, "SELECT COUNT(*) FROM observations WHERE semantic_field='tech'"
                  " AND is_current=1") == 2


# ---------------------------------------------------------------------------
# 审阅点 7 / 8 / 9 / 10
# ---------------------------------------------------------------------------

def test_scope_hash_is_order_independent_and_guards_coverage(db):
    assert scope_hash({"include": ["b", "a"], "exclude": ["z"], "depth": 3}) == \
           scope_hash({"include": ["a", "b"], "exclude": ["z"], "depth": 3})

    env = Env(db)
    j = env.job(coverage="full")
    obs = env.obs(j)
    store.recompute(db)
    env.finish(j)
    ok, why = ops.candidate_inactive(db, observation_id=obs, job_id=j)
    assert (ok, why) == (False, "job_has_active_contribution")

    # 换个"没有贡献"的 job：此刻仅剩 parser 未知这一项不满足
    j2 = env.job(coverage="full")
    env.finish(j2)
    ok, why = ops.candidate_inactive(db, observation_id=obs, job_id=j2)
    assert (ok, why) == (False, "project_parser_unset")

    # scope 升级后，旧 job 的 coverage 不能再被当前 scope 解释
    ops.update_scope(db, env.project_id, {"include": ["*.example.com", "*.example.net"],
                                         "exclude": []})
    info = ops.explain_job_scope(db, j2)
    assert info["interpret_with"] == "historical_scope"
    assert info["coverage_usable_for_inactive"] is False
    ok, why = ops.candidate_inactive(db, observation_id=obs, job_id=j2)
    assert ok is False and why == "scope_version_drift"


def test_observed_at_accuracy_defaults_are_conservative(db):
    """审阅点 8: tool 默认不可信 —— accuracy 必须显式升级。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j)
    src = qv(db, "SELECT observed_at_source FROM contributions WHERE subject_id=?", obs)
    acc = qv(db, "SELECT observed_at_accuracy FROM contributions WHERE subject_id=?", obs)
    assert src == "tool" and acc == "unknown"


# ---------------------------------------------------------------------------
# v3.2-r3: 时间源必须是真实时钟（否则 retention / 24h / GC 全部失真）
# ---------------------------------------------------------------------------

def test_now_is_a_real_clock_not_a_constant(db):
    import datetime as _dt

    real = _dt.datetime.now(_dt.timezone.utc)
    stamp = store.now()
    parsed = parse_time(stamp)
    assert parsed is not None, stamp
    assert abs((parsed - real).total_seconds()) < 120, (stamp, real)
    assert parsed.year >= 2026                       # 不再是硬编码的常量
    # 单调不减，且秒精度
    assert parse_time(store.now()) >= parsed
    assert len(stamp) == 20 and stamp.endswith("Z")

    # retention / 24h 语义现在真的可用
    assert is_within(store.now(), hours=24.0) is True
    assert is_within("2000-01-01T00:00:00Z", hours=24.0) is False
    old = days_ago(180)
    assert hours_since(old) >= 179 * 24


def test_clock_is_injectable_for_determinism(db):
    try:
        set_clock(lambda: "2030-05-05T05:05:05Z")
        assert store.now() == "2030-05-05T05:05:05Z"
        assert days_ago(1) == "2030-05-04T05:05:05Z"
        assert is_within("2030-05-05T00:00:00Z", hours=24.0) is True
        env = Env(db)
        j = env.job()
        env.obs(j, key="host:clock")
        assert qv(db, "SELECT at FROM activity_log ORDER BY id DESC LIMIT 1") \
            == "2030-05-05T05:05:05Z"
        assert qv(db, "SELECT started_at FROM jobs WHERE id=?", j) == "2030-05-05T05:05:05Z"
    finally:
        reset_clock()
    assert store.now() != "2030-05-05T05:05:05Z"


def test_sources_catalog_feeds_authority_term(db):
    """v3.2-r3: §8.2 的 authority_term 必须有数据源（sources 目录表 + FK）。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="host:src", field="tech", value="nginx", source_id=1)
    store.recompute(db)
    eid = env.entity_id("host:src")
    from magpie.core.confidence import score_field
    verified, _ = score_field(db, "entity", eid, "observation_field", "tech", "nginx")
    assert verified in ("high", "medium"), verified      # verified=1.5 起作用了
    assert store.source_authority(db, 1) == "verified"
    # 未登记的 source 会被外键拒绝（不能悄悄用一个"幽灵 source"）
    with pytest.raises(sqlite3.IntegrityError):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=999,
            evidence_id=env.evidence_id, subject_kind="entity", subject_id=eid,
            contribution_kind="entity", observed_at=T0))
    # 幂等 + 可更新 authority
    assert store.register_source(db, name="nmap", authority_level="semiautomatic") == 1
    assert store.source_authority(db, 1) == "semiautomatic"


def test_compact_conserves_counts_and_never_raises_confidence(db):
    """审阅点 9: job_count 语义不跳变（INV-11）。"""
    env = Env(db)
    jobs = [env.job() for _ in range(4)]
    for j in jobs:
        env.obs(j, key="host:a", field="tech", value="nginx", source_id=1,
                observed_at="2020-01-01T00:00:00Z")
    for j in jobs:
        env.finish(j)
    store.recompute(db)
    before = ops._conservation(db, env.project_id)
    conf_before = qv(db, "SELECT confidence FROM observations WHERE semantic_field='tech'")

    report = ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z",
                         job_id=env.job())
    # 编组键含 contribution_kind：entity 存在性贡献与 field 事实各自成组
    kinds = sorted(g["contribution_kind"] for g in report["detail"])
    assert kinds == ["entity", "field"], report["detail"]
    after = ops._conservation(db, env.project_id)
    assert before["logical_jobs"] == after["logical_jobs"]
    assert before["sources"] == after["sources"]
    assert before["sum_source_count"] == after["sum_source_count"]
    assert before["sum_logical_job_count"] == after["sum_logical_job_count"]
    assert before["obs_current"] == after["obs_current"]

    compacted = qv(db, "SELECT compacted_job_count FROM contributions"
                       " WHERE synthetic=1 AND contribution_kind='field'")
    # 4 条同类贡献：最近一条被保护（§5.2），其余 3 条压成 1 条 → compacted = 3-1 = 2
    assert compacted == 2
    assert len(report["protected_last_active"]) >= 1
    assert qv(db, "SELECT logical_job_count FROM observations WHERE semantic_field='tech'") == 4
    conf_after = qv(db, "SELECT confidence FROM observations WHERE semantic_field='tech'")
    order = {"none": 0, "low": 1, "medium": 2, "high": 3, "conflict": 4, "manual": 5}
    assert order[conf_after] <= order[conf_before]
    assert integrity_check(db)["ok"] is True


def test_undo_redo_compact_round_trip_restores_what_undo_destroyed(db):
    """redo 的动机（v3.2-r4）: `undo(compact job)` 曾让数据在 effective 层消失。

    compact 压制了被吞并的原始贡献；undo(C) 只 retract 了 C 的合成行，
    于是"合成行不活跃 + 原始贡献仍被 compact 压制" → 该 subject 一条活贡献都不剩。
    redo(C) 只解除 **retract** 行（undo 的产物），不动 C 自己的 compact 行，
    因此精确回到 compact 之后的状态。
    """
    env = Env(db)
    stamps = ["2020-01-0%dT00:00:00Z" % i for i in (1, 2, 3, 4)]
    jobs = [env.job() for _ in range(4)]
    for j, ts in zip(jobs, stamps):
        env.obs(j, key="host:rt", field="tech", value="nginx", source_id=1, observed_at=ts)
        ops.finish_job(db, j)
    store.recompute(db)
    before = ops._conservation(db, env.project_id)

    cjob = env.job()
    ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z",
                job_id=cjob)
    after_compact = ops._conservation(db, env.project_id)
    assert after_compact["logical_jobs"] == before["logical_jobs"]

    undone = ops.undo(db, cjob)
    assert undone["impact"]["superseded"] > 0
    after_undo = ops._conservation(db, env.project_id)
    # ← 这就是"数据消失": 被吞并的历史（logical job 数）在活的图里消失了。
    #   （注意：v3.2-r3 起"最后一条 active"被保护，所以 observation 仍然 current ——
    #     丢的是被压实的那些 job，不是全部。）
    assert after_undo["logical_jobs"] < after_compact["logical_jobs"], after_undo
    assert after_undo["sum_logical_job_count"] < after_compact["sum_logical_job_count"]
    assert qv(db, "SELECT logical_job_count FROM observations WHERE semantic_field='tech'") \
        < 4

    redone = ops.redo(db, cjob)
    assert redone["noop"] is False
    assert redone["impact"]["supersessions_lifted"] > 0
    assert redone["applied"] == 1
    after_redo = ops._conservation(db, env.project_id)
    assert after_redo == after_compact                     # 精确回到 compact 之后
    assert qv(db, "SELECT is_current FROM observations WHERE semantic_field='tech'") == 1
    assert qv(db, "SELECT logical_job_count FROM observations WHERE semantic_field='tech'") == 4
    assert qv(db, "SELECT observed_at FROM observations WHERE semantic_field='tech'") \
        == stamps[-1]
    assert integrity_check(db)["ok"] is True


def test_redo_restores_identity_facts_that_undo_can_only_revoke(db):
    """alias / merge 是**身份事实**：undo 只能写撤销边，行必须留着，redo 才能复原。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="/report/2024", field="status", value="ok")
    ops.finish_job(db, j)
    eid = env.entity_id("/report/2024")

    j_rekey = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"/report/2024": "/report/{id}"},
              job_id=j_rekey)
    assert store.resolve_entity(db, env.project_id, "/report/{id}") == eid
    baseline = snapshot_effective(db)
    baseline_alias_rows = qv(db, "SELECT COUNT(*) FROM entity_alias")

    ops.undo(db, j_rekey)
    # 解析不到 —— 但**行内容没被删掉**（这正是 redo 可逆的前提）
    assert store.resolve_entity(db, env.project_id, "/report/{id}") is None
    assert qv(db, "SELECT COUNT(*) FROM entity_alias") == baseline_alias_rows
    assert qv(db, "SELECT revoked_by_job_id FROM entity_alias WHERE alias_key=?",
              "/report/{id}") == j_rekey
    assert qv(db, "SELECT applied FROM jobs WHERE id=?", j_rekey) == 0

    ops.redo(db, j_rekey)
    assert store.resolve_entity(db, env.project_id, "/report/{id}") == eid
    assert qv(db, "SELECT revoked_by_job_id FROM entity_alias WHERE alias_key=?",
              "/report/{id}") is None
    assert qv(db, "SELECT applied FROM jobs WHERE id=?", j_rekey) == 1
    ok, diff = effective_equivalent(db, baseline)
    assert ok, diff
    assert integrity_check(db)["ok"] is True


def test_undo_redo_merge_event_and_inbox(db):
    """merge job 的 undo/redo: 事件、成员、以及由它打开的 inbox 项都要对称。"""
    env = Env(db)
    j_ing = env.job()
    env.obs(j_ing, key="host:a", field="tech", value="nginx")
    env.obs(j_ing, key="host:a", field="title", value="corp")
    env.obs(j_ing, key="host:b", field="tech", value="nginx")
    env.obs(j_ing, key="host:b", field="title", value="other")     # 与 a 冲突
    ops.finish_job(db, j_ing)
    store.recompute(db)
    eid_a = env.entity_id("host:a")
    eid_b = env.entity_id("host:b")

    j_merge = env.job()
    rep = ops.merge(db, project_id=env.project_id, survivor_key="host:a",
                    member_keys=["host:b"], job_id=j_merge)
    event_id = rep["event_id"]
    assert store.canonical_of(db, eid_b) == eid_a
    open_items = qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"
                        " AND kind='merge_conflict'")
    assert open_items == 1
    baseline = snapshot_effective(db)

    ops.undo(db, j_merge)
    assert qv(db, "SELECT state FROM entity_merge_events WHERE id=?", event_id) == "unmerged"
    assert store.canonical_of(db, eid_b) == eid_b
    assert qv(db, "SELECT COUNT(*) FROM entity_merge_members WHERE merge_event_id=?"
                  " AND revoked_by_job_id IS NULL", event_id) == 0
    assert qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"
                  " AND kind='merge_conflict'") == 0

    ops.redo(db, j_merge)
    assert qv(db, "SELECT state FROM entity_merge_events WHERE id=?", event_id) == "merged"
    assert store.canonical_of(db, eid_b) == eid_a
    # merge_members 记录的是 {survivor} ∪ members = 2 个（survivor 自己也在里面）
    assert qv(db, "SELECT COUNT(*) FROM entity_merge_members WHERE merge_event_id=?"
                  " AND revoked_by_job_id IS NULL", event_id) == 2
    assert qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"
                  " AND kind='merge_conflict'") == open_items
    ok, diff = effective_equivalent(db, baseline)
    assert ok, diff
    assert integrity_check(db)["ok"] is True


def test_unmerge_closes_its_merge_conflict_items(db):
    """v3.2-r4 修 bug: unmerge 靠 payload 里的 event_id 关闭项，而旧 payload 里没有该字段。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:a", field="tech", value="nginx")
    env.obs(j, key="host:b", field="tech", value="apache")
    ops.finish_job(db, j)
    rep = ops.merge(db, project_id=env.project_id, survivor_key="host:a",
                    member_keys=["host:b"], job_id=j)
    assert qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'") == 1
    report = ops.unmerge(db, event_id=rep["event_id"])
    assert report["inbox_closed"] == 1
    assert qv(db, "SELECT COUNT(*) FROM inbox_items WHERE state='open'") == 0
    assert integrity_check(db)["ok"] is True


def test_redo_is_idempotent_and_dry_run_matches_real_run(db):
    env = Env(db)
    j = env.job()
    env.obs(j)
    ops.finish_job(db, j)
    store.recompute(db)

    # 没有被 undo 过 → redo 是 noop（幂等，不报错）
    first = ops.redo(db, j)
    assert first["noop"] is True
    assert first["impact"]["supersessions_lifted"] == 0

    ops.undo(db, j)
    before = (snapshot_cache(db), snapshot_history(db), snapshot_audit(db))
    dry = ops.redo(db, j, dry_run=True)
    assert dry["dry_run"] is True and dry["noop"] is False
    assert (snapshot_cache(db), snapshot_history(db), snapshot_audit(db)) == before

    real = ops.redo(db, j)
    # env.obs() 会写两条贡献（entity 存在性 + field 事实）→ undo 产生 2 条 retract
    assert real["impact"]["supersessions_lifted"] == 2
    again = ops.redo(db, j)
    assert again["noop"] is True
    # undo 也是幂等的
    ops.undo(db, j)
    ops.undo(db, j)
    assert qv(db, "SELECT applied FROM jobs WHERE id=?", j) == 0
    assert qv(db, "SELECT COUNT(*) FROM contribution_supersessions"
                  " WHERE superseded_by_job_id=? AND reason='retract'", j) == 2


def test_redo_reports_blocked_alias_instead_of_overwriting_a_newer_decision(db):
    """redo 撞上更新的身份决定时：保留撤销边并汇报，绝不静默覆盖。"""
    env = Env(db)
    j0 = env.job()
    env.obs(j0, key="k1", field="tech", value="nginx")
    env.obs(j0, key="k2", field="tech", value="nginx")
    ops.finish_job(db, j0)
    store.recompute(db)
    eid1 = env.entity_id("k1")
    eid2 = env.entity_id("k2")

    j_old = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"k1": "shared"}, job_id=j_old)
    ops.undo(db, j_old)                       # "shared" 的 alias 被撤销
    # 更新的决定：把 "shared" 给另一个 entity
    j_new = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"k2": "shared"}, job_id=j_new)
    assert store.resolve_entity(db, env.project_id, "shared") == eid2

    report = ops.redo(db, j_old)
    assert report["impact"]["alias_restored"] == 0
    assert report["impact"]["blocked"], report
    assert store.resolve_entity(db, env.project_id, "shared") == eid2   # 未被覆盖
    assert qv(db, "SELECT revoked_by_job_id FROM entity_alias WHERE alias_key='shared'"
                  " AND entity_id=?", eid1) == j_old                    # 边保留
    assert integrity_check(db)["ok"] is True


def test_undo_redo_full_round_trip_restores_effective_state(db):
    """INV-18（新）: undo 与 redo 严格互逆 —— redo(正序) ∘ undo(反序) = 恒等。

    注意这比 INV-13 更强：INV-13 只要求"撤销后回到序列前的有效状态"，
    INV-18 还要求"撤销再重做之后，有效状态与撤销前**逐列相等**"（可逆性）。
    """
    env = Env(db)
    jobs = []
    for i in range(3):
        j = env.job()
        env.obs(j, key=f"host:r{i}", field="tech", value="nginx", source_id=1 + (i % 3))
        ops.finish_job(db, j)
        jobs.append(j)
    j_id = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"host:r0": "host:r0-renamed"},
              job_id=j_id)
    store.recompute(db)
    baseline = snapshot_effective(db)
    baseline_active = active_contribution_ids(db)

    for j in reversed(jobs + [j_id]):
        ops.undo(db, j)
    assert snapshot_effective(db) != baseline

    for j in jobs + [j_id]:
        ops.redo(db, j)
    ok, diff = effective_equivalent(db, baseline)
    assert ok, diff
    assert active_contribution_ids(db) == baseline_active
    assert store.resolve_entity(db, env.project_id, "host:r0-renamed") == \
        env.entity_id("host:r0")
    assert integrity_check(db)["ok"] is True


def test_compact_preserves_time_interval_and_never_rewinds_timeline(db):
    """v3.2-r2: 压实必须保持时间区间语义（observed_at 上界不许回退）。

    旧实现 observed_at = MIN(成员) 会把时间线往回拉 —— 压实操作改坏了历史。
    """
    env = Env(db)
    stamps = ["2020-01-0%dT00:00:00Z" % i for i in (1, 2, 3, 4)]
    jobs = [env.job() for _ in range(4)]
    for j, ts in zip(jobs, stamps):
        env.obs(j, key="host:iv", field="tech", value="nginx", source_id=1, observed_at=ts)
    for j in jobs:
        env.finish(j)
    store.recompute(db)
    obs_id = qv(db, "SELECT id FROM observations WHERE semantic_field='tech'")
    before = (qv(db, "SELECT observed_at FROM observations WHERE id=?", obs_id),
              qv(db, "SELECT observed_at_min FROM observations WHERE id=?", obs_id))
    assert before == (stamps[-1], stamps[0])

    ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z",
                job_id=env.job())
    after = (qv(db, "SELECT observed_at FROM observations WHERE id=?", obs_id),
             qv(db, "SELECT observed_at_min FROM observations WHERE id=?", obs_id))
    assert after == before, (before, after)
    row = q1(db, "SELECT observed_at, observed_at_min, observed_at_accuracy"
                 " FROM contributions WHERE synthetic=1 AND contribution_kind='field'")
    # 最近一条被保护，因此合成行的上界是"倒数第二新"那一批的最大值
    assert row["observed_at"] == stamps[-2]        # 上界 = MAX(被压实成员)，不许回退
    assert row["observed_at_min"] == stamps[0]     # 下界 = MIN(成员)
    assert row["observed_at_accuracy"] == "approximate"
    # 被保护的最近一条仍是原始贡献
    assert qv(db, "SELECT observed_at FROM contributions WHERE synthetic=0"
                  " AND contribution_kind='field' ORDER BY observed_at DESC LIMIT 1") \
        == stamps[-1]
    assert integrity_check(db)["ok"] is True


def test_compact_protects_the_last_active_per_source_field(db):
    """§5.2（v3.2-r3）: "每组必须保留最后一条 active per (source, field)"。

    业务语义: "最近一次观测"必须单独存活，不能被合成行代表 ——
    否则 earliest/latest 的区分、以及"最近是否还看到过"的判断都会失去依据。
    """
    env = Env(db)
    stamps = ["2020-01-0%dT00:00:00Z" % i for i in (1, 2, 3)]
    jobs = [env.job() for _ in range(3)]
    for j, ts in zip(jobs, stamps):
        env.obs(j, key="host:last", field="title", value="corp", source_id=1,
                observed_at=ts)
        ops.finish_job(db, j)
    store.recompute(db)
    latest_id = qv(db, "SELECT id FROM contributions WHERE subject_kind='observation'"
                       " ORDER BY observed_at DESC, id DESC LIMIT 1")
    before = ops._conservation(db, env.project_id)
    report = ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z",
                         job_id=env.job())

    assert latest_id in report["protected_last_active"], report
    # 最近一条仍是**原始**贡献（未被合成行取代）
    row = q1(db, "SELECT synthetic, job_count, compacted_job_count FROM contributions"
                 " WHERE id=?", latest_id)
    assert int(row["synthetic"]) == 0
    assert int(row["compacted_job_count"]) == 0
    # 更早的两条被压实成 1 条 synthetic，且守恒
    assert qv(db, "SELECT COUNT(*) FROM contributions WHERE synthetic=1"
                  " AND contribution_kind='field'") == 1
    after = ops._conservation(db, env.project_id)
    assert before["logical_jobs"] == after["logical_jobs"]
    assert before["sum_logical_job_count"] == after["sum_logical_job_count"]
    # 缓存里的 observed_at 仍是最新那次观测
    assert qv(db, "SELECT observed_at FROM observations") == stamps[-1]
    assert integrity_check(db)["ok"] is True


def test_contribution_structure_mismatch_is_rejected(db):
    """INV-16 的**结构一致**半边（v3.2-r3）:

    同一 observation 的所有贡献必须给出同一 (owner, namespace, field, value)。
    于是 materialize() 里的 MIN(id) 只是在等价类里取确定性代表，
    **不是**"哪条贡献负责创建结构行"的领域语义。
    """
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="host:struct", field="tech", value="nginx")
    eid = env.entity_id("host:struct")
    with pytest.raises(InvariantError, match="INV-16"):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=2, evidence_id=env.evidence_id,
            subject_kind="observation", subject_id=obs, owner_entity_id=eid,
            contribution_kind="field", field_namespace="observation_field",
            semantic_field="tech", field_value="apache",     # ← 同一行声称了另一个值
            value_kind="string", observed_at=T0))
    with pytest.raises(sqlite3.IntegrityError, match="INV-16"):
        db.execute(
            "INSERT INTO contributions(project_id, subject_kind, subject_id, owner_entity_id,"
            " source_id, authorship, contribution_kind, field_namespace, semantic_field,"
            " field_value, value_kind, observed_at, observed_at_min, observed_at_source,"
            " evidence_id, job_id, created_at) VALUES (?,?,?,?,?, 'tool','field',"
            " 'observation_field','tech','apache','string',?,?,'tool',?,?,?)",
            (env.project_id, "observation", obs, eid, 2, T0, T0, env.evidence_id, j, T0))
    # 结构一致的重复贡献仍然允许（多来源支持同一事实）
    store.add_contribution(db, store.NewContribution(
        project_id=env.project_id, job_id=j, source_id=2, evidence_id=env.evidence_id,
        subject_kind="observation", subject_id=obs, owner_entity_id=eid,
        contribution_kind="field", field_namespace="observation_field",
        semantic_field="tech", field_value="nginx", value_kind="string", observed_at=T0))
    store.recompute(db)
    assert qv(db, "SELECT COUNT(*) FROM v_integrity_violations"
                  " WHERE violation LIKE '%owner_mismatch%'") == 0
    assert integrity_check(db)["ok"] is True


def test_materialize_picks_a_deterministic_representative(db):
    """同一 observation 的多条等价贡献 → 重建结果与"用哪一条"无关。"""
    env = Env(db)
    j = env.job()
    obs = env.obs(j, key="host:repr", field="tech", value="nginx", source_id=1)
    eid = env.entity_id("host:repr")
    for sid in (2, 3):
        store.add_contribution(db, store.NewContribution(
            project_id=env.project_id, job_id=j, source_id=sid,
            evidence_id=env.evidence_id, subject_kind="observation", subject_id=obs,
            owner_entity_id=eid, contribution_kind="field",
            field_namespace="observation_field", semantic_field="tech",
            field_value="nginx", value_kind="string", observed_at=T0))
    store.recompute(db)
    before = q1(db, "SELECT entity_id, field_namespace, semantic_field, value, value_kind"
                    " FROM observations WHERE id=?", obs)
    for _ in range(3):
        db.execute("DELETE FROM observations WHERE id=?", (obs,))
        store.recompute(db)
        assert tuple(q1(db, "SELECT entity_id, field_namespace, semantic_field, value,"
                           " value_kind FROM observations WHERE id=?", obs)) == tuple(before)


def test_compact_never_compacts_manual_contributions(db):
    env = Env(db)
    jobs = [env.job() for _ in range(3)]
    for j in jobs:
        env.obs(j, key="host:a", field="tech", value="nginx", authorship="manual",
                observed_at="2020-01-01T00:00:00Z")
    store.recompute(db)
    report = ops.compact(db, project_id=env.project_id,
                         older_than="2025-01-01T00:00:00Z", job_id=env.job())
    assert report["groups"] == 0
    assert qv(db, "SELECT COUNT(*) FROM contribution_supersessions WHERE reason='compact'") == 0


def test_compact_without_job_still_satisfies_supersession_fk(db):
    """compact 可以不带 job（后台压实）；此时必须自己开一个并复用同一个 id，
    否则 superseded_by_job_id 会拿到 NULL 而违反 NOT NULL。"""
    env = Env(db)
    jobs = [env.job() for _ in range(3)]
    for j in jobs:
        env.obs(j, key="host:a", field="tech", value="nginx", source_id=1,
                observed_at="2020-01-01T00:00:00Z")
    store.recompute(db)
    report = ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z")
    assert report["groups"] >= 1
    rows = db.execute(
        "SELECT DISTINCT superseded_by_job_id FROM contribution_supersessions"
        " WHERE reason='compact'").fetchall()
    assert len(rows) == 1 and rows[0][0] is not None
    assert qv(db, "SELECT COUNT(*) FROM contributions WHERE synthetic=1"
                  " AND job_id=?", rows[0][0]) == report["groups"]
    assert integrity_check(db)["ok"] is True


def test_evidence_capability_matrix_is_complete_and_conservative(db):
    """§5.3（v3.2-r3 闭环）: 3×4 = 12 种组合全部写死，且语义自洽。

    三级 = 这一行还留下什么；能力 = "原文是否仍可取得" × "片段是否仍可定位"。
    """
    storage = ("present", "archived", "purged", "missing")
    recovery = ("full", "locator", "hash_only")
    matrix = {(s, r): ops.evidence_capability(s, r) for s in storage for r in recovery}
    assert len(matrix) == 12
    # 原文仍在 + 有 locator → 可恢复片段
    for s in ("present", "archived"):
        assert matrix[(s, "full")] == "recoverable"
        assert matrix[(s, "locator")] == "recoverable"
        # 原文仍在，但 locator 已丢弃 → 定位不到片段，只能校验
        assert matrix[(s, "hash_only")] == "verifiable_only"
    # 原文已清除 → 一律只能校验
    for r in recovery:
        assert matrix[("purged", r)] == "verifiable_only"
    # 无原文、无归档 → 只剩 hash
    for r in recovery:
        assert matrix[("missing", r)] == "hash_only"
    # 未知组合按最保守处理
    assert ops.evidence_capability("weird", "weird") == "hash_only"


def test_evidence_downgrade_is_monotone_and_hash_only_drops_locator(db):
    env = Env(db)
    # full → locator：保留 locator
    r1 = ops.degrade_evidence(db, raw_file_id=env.raw_file_id, to_state="locator")
    assert r1["changed"][0]["to"] == "locator"
    assert qv(db, "SELECT locator FROM evidence WHERE id=?", env.evidence_id) is not None
    # locator → hash_only：locator 必须被丢弃（DB CHECK 强制）
    r2 = ops.degrade_evidence(db, raw_file_id=env.raw_file_id, to_state="hash_only")
    assert qv(db, "SELECT recovery_state FROM evidence WHERE id=?", env.evidence_id) \
        == "hash_only"
    assert qv(db, "SELECT locator FROM evidence WHERE id=?", env.evidence_id) is None
    assert r2["capability"] == "verifiable_only"    # 原文仍在，但片段不可定位
    assert r2["locatable"] is False
    assert r2["original_available"] is True
    # 不允许升级（store 层 + 触发器兜底）
    with pytest.raises(InvariantError, match="INV-17"):
        ops.degrade_evidence(db, raw_file_id=env.raw_file_id, to_state="full")
    with pytest.raises(sqlite3.IntegrityError, match="INV-17"):
        db.execute("UPDATE evidence SET recovery_state='full' WHERE id=?", (env.evidence_id,))
    assert integrity_check(db)["ok"] is True


def test_raw_file_purge_is_transaction_safe_and_downgrades_evidence(db):
    """旧实现 `purge_raw_file` 嵌套事务必然崩，且 dry_run 未下传。"""
    env = Env(db)
    before = (snapshot_cache(db), snapshot_history(db), snapshot_audit(db))
    dry = ops.purge_raw_file(db, raw_file_id=env.raw_file_id, dry_run=True)
    assert dry["dry_run"] is True
    assert (snapshot_cache(db), snapshot_history(db), snapshot_audit(db)) == before

    report = ops.purge_raw_file(db, raw_file_id=env.raw_file_id, job_id=env.job())
    assert report["capability"] == "verifiable_only"
    assert qv(db, "SELECT storage_state FROM raw_files WHERE id=?", env.raw_file_id) \
        == "purged"
    assert qv(db, "SELECT recovery_state FROM evidence WHERE id=?", env.evidence_id) \
        == "hash_only"
    assert qv(db, "SELECT COUNT(*) FROM activity_log WHERE action='evidence.degrade'") == 1
    # storage_state 只允许降级
    with pytest.raises(sqlite3.IntegrityError, match="INV-17"):
        db.execute("UPDATE raw_files SET storage_state='present' WHERE id=?",
                   (env.raw_file_id,))
    assert integrity_check(db)["ok"] is True


# ---------------------------------------------------------------------------
# 审阅点 13 / 14 / 15: manual 单写队列 / tag 审计 / activity.job_id
# ---------------------------------------------------------------------------

def test_manual_fields_optimistic_version_prevents_lost_update(db):
    """审阅点 13 的下界: 过期版本号的裸 UPDATE 不得覆盖已提交值。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    db.execute("INSERT INTO manual_fields(project_id, subject_kind, subject_id, keys_json,"
               " version, updated_at, updated_by) VALUES (?, 'entity', ?, '[]', 1, ?, 'a')",
               (env.project_id, eid, T0))
    # writer A 与 writer B 都读到 version=1
    a = dict(q1(db, "SELECT * FROM manual_fields WHERE subject_id=?", eid))
    b = dict(a)
    # A 先提交
    cur = db.execute("UPDATE manual_fields SET keys_json=?, version=version+1"
                     " WHERE subject_id=? AND version=?", ('["display_name"]', eid, a["version"]))
    assert cur.rowcount == 1
    # B 用过期的 version 提交 -> 必须失败（而不是静默覆盖 A）
    cur = db.execute("UPDATE manual_fields SET keys_json=?, version=version+1"
                     " WHERE subject_id=? AND version=?", ('["review_status"]', eid, b["version"]))
    assert cur.rowcount == 0
    assert qv(db, "SELECT keys_json FROM manual_fields WHERE subject_id=?", eid) \
        == '["display_name"]'


def test_manual_writer_merges_retries_and_never_loses_keys(db):
    """INV-12 的真正要求（v3.2-r3）: 乐观锁只能发现问题，
    唯一 writer 必须**重读-合并-重试**，让最终 keys == 所有写入的并集，且无丢失。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    w = manual_mod.ManualWriter(db, actor="analyst")

    # 模拟"两个 writer 都读到 version=1"，第二个必须合并而不是覆盖
    db.execute("INSERT INTO manual_fields(project_id, subject_kind, subject_id, keys_json,"
               " version, updated_at, updated_by) VALUES (?, 'entity', ?, '[]', 1, ?, 'a')",
               (env.project_id, eid, T0))
    w.set_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
               add=["display_name"])
    stale_version = 1          # 另一个 writer 手里仍握着旧版本号
    row = q1(db, "SELECT version FROM manual_fields WHERE subject_id=?", eid)
    assert int(row["version"]) == 2
    cur = db.execute("UPDATE manual_fields SET keys_json='[\"review_status\"]',"
                     " version=version+1 WHERE subject_id=? AND version=?",
                     (eid, stale_version))
    assert cur.rowcount == 0   # 过期写入被拒
    # writer 内部重试后写进去的 key 不会丢
    w.set_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
               add=["review_status"])
    assert w.read_keys(project_id=env.project_id, subject_kind="entity",
                       subject_id=eid) == ["display_name", "review_status"]

    # 100 次不同 key 的写入（含重复/撤销）→ 最终必须是并集，且无丢失
    expected: set[str] = {"display_name", "review_status"}
    for i in range(100):
        key = f"field_{i}"
        expected.add(key)
        w.set_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
                   add=[key])
    assert set(w.read_keys(project_id=env.project_id, subject_kind="entity",
                           subject_id=eid)) == expected
    assert len(expected) == 102

    # clear 是增量语义，不能把其它 key 一起抹掉
    w.clear_keys(project_id=env.project_id, subject_kind="entity", subject_id=eid,
                 keys=["field_0", "field_1"])
    remaining = set(w.read_keys(project_id=env.project_id, subject_kind="entity",
                                subject_id=eid))
    assert "field_0" not in remaining and "field_1" not in remaining
    assert "field_99" in remaining
    # 每个写操作都有审计（manual.set / manual.clear）
    assert qv(db, "SELECT COUNT(*) FROM activity_log WHERE action IN"
                  " ('manual.set','manual.clear')") >= 102
    store.recompute(db)
    assert integrity_check(db)["ok"] is True


def test_manual_writer_is_thread_safe_and_lossless(tmp_path):
    """真并发: 8 线程 × 各自的 key，全部经唯一 writer → 并集、无丢失、无异常。"""
    import threading

    db_path = tmp_path / "manual.db"
    conn = connect(str(db_path), check_same_thread=False)
    init_db(conn)
    env = Env(conn)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    writer = manual_mod.ManualWriter(conn, actor="threads")
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(25):
                writer.set_keys(project_id=env.project_id, subject_kind="entity",
                                subject_id=eid, add=[f"t{tid}_k{i}"])
        except BaseException as exc:           # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    keys = set(writer.read_keys(project_id=env.project_id, subject_kind="entity",
                                subject_id=eid))
    assert keys == {f"t{t}_k{i}" for t in range(8) for i in range(25)}
    assert len(keys) == 200
    store.recompute(conn)
    assert integrity_check(conn)["ok"] is True
    conn.close()


def test_manual_writer_audits_tags_and_layout(db):
    """§6.1 + INV-12: tag / layout 也必须经唯一 writer，且 add/remove 都有审计。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    w = manual_mod.ManualWriter(db, actor="analyst")
    w.set_tag(project_id=env.project_id, entity_id=eid, tag="crown-jewel", reason="triage")
    w.set_tag(project_id=env.project_id, entity_id=eid, tag="crown-jewel")   # 幂等
    assert qv(db, "SELECT COUNT(*) FROM entity_tags WHERE deleted_at IS NULL") == 1
    assert qv(db, "SELECT COUNT(*) FROM tag_events WHERE action='add'") == 1
    assert store.manual_owned(db, "entity", eid) is True
    w.set_layout(project_id=env.project_id, entity_id=eid, x=1.5, y=2.5)
    assert qv(db, "SELECT x FROM entity_layout WHERE entity_id=?", eid) == 1.5

    w.remove_tag(project_id=env.project_id, entity_id=eid, tag="crown-jewel",
                 reason="mistake")
    w.remove_tag(project_id=env.project_id, entity_id=eid, tag="crown-jewel")  # 幂等
    assert qv(db, "SELECT COUNT(*) FROM entity_tags WHERE deleted_at IS NULL") == 0
    assert qv(db, "SELECT COUNT(*) FROM tag_events WHERE action='remove'") == 1
    # 软删保留 set_by 历史
    assert qv(db, "SELECT set_by FROM entity_tags WHERE entity_id=?", eid) == "analyst"
    # layout 仍是人工锚点
    assert store.manual_owned(db, "entity", eid) is True
    db.execute("DELETE FROM entity_layout WHERE entity_id=?", (eid,))
    # 撤销全部人工状态之后 manual ownership 必须随之消失
    # （v3.2-r3: 不再读缓存列 observations.origin）
    assert store.manual_owned(db, "entity", eid) is False
    store.recompute(db)
    assert integrity_check(db)["ok"] is True


def test_manual_owned_follows_active_facts_not_cached_origin(db):
    """v3.2-r3 修正: 人工贡献被 undo 之后，实体不再被永久锚定。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:man", authorship="manual")
    ops.finish_job(db, j)
    store.recompute(db)
    eid = env.entity_id("host:man")
    assert store.manual_owned(db, "entity", eid) is True
    ops.undo(db, j)
    # 缓存列 observations.origin 仍是 'manual'，但事实已不活跃 → 不再是 manual 对象
    assert qv(db, "SELECT origin FROM observations WHERE entity_id=?", eid) == "manual"
    assert qv(db, "SELECT COUNT(*) FROM v_active_contributions WHERE authorship='manual'"
                  ) == 0
    assert store.manual_owned(db, "entity", eid) is False


def test_tag_add_and_remove_are_both_audited(db):
    """审阅点 14: 删除 tag 也必须有审计对象。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    eid = env.entity_id()
    db.execute("INSERT INTO entity_tags(project_id, entity_id, tag, set_by, set_at)"
               " VALUES (?,?,?,?,?)", (env.project_id, eid, "crown-jewel", "analyst", T0))
    db.execute("INSERT INTO tag_events(project_id, entity_id, tag, action, actor, at)"
               " VALUES (?,?,?,'add','analyst',?)", (env.project_id, eid, "crown-jewel", T0))
    db.execute("UPDATE entity_tags SET deleted_at=?, deleted_by=?, delete_reason=?"
               " WHERE entity_id=? AND tag=?", (T1, "analyst", "mistake", eid, "crown-jewel"))
    db.execute("INSERT INTO tag_events(project_id, entity_id, tag, action, actor, at, reason)"
               " VALUES (?,?,?,'remove','analyst',?,?)",
               (env.project_id, eid, "crown-jewel", T1, "mistake"))
    assert qv(db, "SELECT COUNT(*) FROM tag_events WHERE action='remove'") == 1
    # 软删保留 set_by 历史
    assert qv(db, "SELECT set_by FROM entity_tags WHERE entity_id=?", eid) == "analyst"
    assert qv(db, "SELECT COUNT(*) FROM entity_tags WHERE deleted_at IS NULL") == 0


def test_activity_log_links_to_job_and_action_namespace_is_frozen(db):
    """审阅点 15: activity -> job -> contributions -> evidence 链条完整。"""
    env = Env(db)
    j = env.job()
    env.obs(j)
    env.finish(j)
    row = q1(db, "SELECT job_id, action FROM activity_log WHERE action='undo' OR action='job.start'"
                 " ORDER BY id LIMIT 1")
    assert row["job_id"] == j
    with pytest.raises(InvariantError):
        store.log_activity(db, actor="x", action="not.in.namespace")
    chain = q1(db, """
        SELECT a.action, j.id AS job_id, c.id AS contribution_id, e.id AS evidence_id
          FROM activity_log a
          JOIN jobs j ON j.id = a.job_id
          JOIN contributions c ON c.job_id = j.id
          JOIN evidence e ON e.id = c.evidence_id
         LIMIT 1""")
    assert chain["contribution_id"] and chain["evidence_id"]


# ---------------------------------------------------------------------------
# 属性测试（INV-09 / INV-13 / INV-15）
# ---------------------------------------------------------------------------

def _replay(db, plan):
    env = Env(db)
    baseline_domain, baseline_audit = snapshot_effective(db), snapshot_audit(db)
    baseline_history = snapshot_history(db)
    jobs: list[int] = []
    for op in plan:
        kind = op["kind"]
        if kind == "job":
            j = env.job(coverage=op.get("coverage", "full"))
            jobs.append(j)
        elif kind == "obs":
            if not jobs:
                continue
            env.obs(jobs[-1], key=op["key"], field=op["field"], value=op["value"],
                    source_id=op["source"])
        elif kind == "rel":
            if not jobs:
                continue
            env.rel(jobs[-1], src=op["src"], dst=op["dst"])
        elif kind == "recompute":
            store.recompute(db)
    for j in jobs:
        env.finish(j)
    store.recompute(db)
    return env, jobs, baseline_domain, baseline_audit, baseline_history


def _check_every_step(db):
    """INV-09 / INV-15: 每个操作后都不允许 orphan / cross-project / 悬空。"""
    report = integrity_check(db)
    assert report["ok"], report


def test_property_undo_all_restores_effective_state_random_sequences():
    """INV-09 / INV-13 / INV-14 / INV-15 随机序列属性测试。

    v3.2-r3: 不再依赖 hypothesis（离线环境装不上）。用固定种子的 PRNG 跑
    **1000 条**随机序列，每条都:
        每个操作后跑完整性检查（INV-09 / INV-15）
        交错一次 merge + unmerge（INV-10）
        逆序 undo 全部 job（INV-13 / INV-14）
    """
    import random

    rng = random.Random(20260911)
    keys = ["host:a", "host:b", "host:c"]
    fields = ["tech", "title", "status"]
    values = ["nginx", "apache", "ok"]
    plans = []
    for _ in range(1000):
        plan = []
        for _ in range(rng.randint(1, 12)):
            roll = rng.random()
            if roll < 0.3:
                plan.append({"kind": "job"})
            elif roll < 0.8:
                plan.append({"kind": "obs", "key": rng.choice(keys),
                             "field": rng.choice(fields), "value": rng.choice(values),
                             "source": rng.randint(1, 3)})
            else:
                plan.append({"kind": "rel", "src": rng.choice(keys[:2]),
                             "dst": rng.choice(keys[1:])})
        plans.append(plan)

    for plan in plans:
        db = connect(":memory:")
        try:
            init_db(db)
            env, jobs, baseline_domain, baseline_audit, baseline_history = _replay(db, plan)
            _check_every_step(db)
            keys_found = [r["entity_key"] for r in db.execute(
                "SELECT entity_key FROM entity_keys ORDER BY entity_key")]
            if len(keys_found) >= 2 and jobs:
                ev_id = ops.merge(db, project_id=env.project_id, survivor_key=keys_found[0],
                                  member_keys=[keys_found[1]], job_id=jobs[-1])["event_id"]
                _check_every_step(db)
                ops.unmerge(db, event_id=ev_id)
                _check_every_step(db)
            for j in reversed(jobs):
                ops.undo(db, j)
                _check_every_step(db)
            ok, diff = domain_equivalent(db, baseline_domain)
            assert ok, (plan, diff)
            ok_hist, hist = history_monotone(db, baseline_history)
            assert ok_hist, (plan, hist)
            grew, detail = audit_strictly_grew(db, baseline_audit)
            assert grew, (plan, detail)
        finally:
            db.close()


def test_property_recompute_equivalence_random_operations():
    """INV-04: 随机操作序列 + 完整 impact_set 的 delta 必须与 full 逐列相等。

    v3.2-r3: 同样改为固定种子的确定性随机（500 例），不依赖 hypothesis。
    """
    import random

    rng = random.Random(4242)
    cases = []
    for _ in range(500):
        rows = [(rng.choice(["host:a", "host:b"]), rng.choice(["tech", "title"]),
                 rng.choice(["nginx", "apache"]), rng.randint(1, 2))
                for _ in range(rng.randint(1, 10))]
        cases.append(rows)

    for rows in cases:
        db = connect(":memory:")
        try:
            init_db(db)
            env = Env(db)
            js = [env.job(), env.job()]
            for i, (k, f, v, s) in enumerate(rows):
                env.obs(js[i % 2], key=k, field=f, value=v, source_id=s)
            for j in js:
                env.finish(j)
            store.recompute(db)
            obs_ids = [int(r["id"]) for r in db.execute(
                "SELECT id FROM observations ORDER BY id")]
            entities = [int(r["id"]) for r in db.execute(
                "SELECT id FROM entities ORDER BY id")]
            relations = [int(r["id"]) for r in db.execute(
                "SELECT id FROM relations ORDER BY id")]
            delta = ([("observation", o) for o in obs_ids]
                     + [("entity", e) for e in entities]
                     + [("relation", r) for r in relations])
            store.recompute(db, delta=delta)
            delta_state = snapshot_cache(db)
            store.recompute(db)
            assert snapshot_cache(db) == delta_state, rows
            _check_every_step(db)
        finally:
            db.close()
