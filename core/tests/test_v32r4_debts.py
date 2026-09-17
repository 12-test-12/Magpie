"""v3.2-r4 债务收口回归网（2026-09-17）。

覆盖 `Magpie-搭建思路.md` 第三节里"还在"的六笔债:

    #3 compact 裸 SQL 直写 contributions   → 收编回唯一写入口 + lint 测试
    #4 rekey 缺 Online Backup 快照        → store.online_backup + rekey 接线
    #5 canonical key 展示优先级未定       → store.display_key 定死口径
    #6 parser 定义版本化 / 回滚未定义     → parser_history + rollback_parser
    #7 无迁移机制                        → core/migrate.py + schema_migrations
    #8 没有 git                          → 不在测试范围内（工程前提，见 check.py）

纪律: 每一笔先写反例（确认它在修之前是红的）再修到绿。这些用例是那一步的存档。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import ops, store
from core.store import (
    InvariantError, connect, effective_equivalent, init_db, integrity_check,
    snapshot_effective,
)
from test_invariants import Env, q1, qv

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 债 #3 —— 写入口唯一性
# ---------------------------------------------------------------------------

def test_no_module_bypasses_the_contribution_write_entry():
    """`INSERT INTO contributions` 只允许出现在 store.py。

    compact 曾是唯一违规者（`core/ops.py` 里一段裸 INSERT 直写合成行）。
    这条 lint 测试是"写入口唯一性"唯一可自动验证的形式 ——
    没有它，下一个图省事的写点会再次悄悄出现。
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO contributions" in text and path.name != "store.py":
            for i, line in enumerate(text.splitlines(), 1):
                if "INSERT INTO contributions" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")
    assert offenders == [], f"绕过唯一写入口的裸 SQL: {offenders}"


def test_compacted_row_goes_through_the_write_entry_and_keeps_shape(db):
    """合成行必须与入口产出的行同形：同一组校验、同一份审计。"""
    env = Env(db)
    for ts in ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", "2020-01-03T00:00:00Z"):
        j = env.job()
        env.obs(j, key="host:c3", field="tech", value="nginx", observed_at=ts)
        env.finish(j)
    store.recompute(db)
    ops.compact(db, project_id=env.project_id, older_than="2025-01-01T00:00:00Z")

    row = q1(db, "SELECT * FROM contributions WHERE synthetic=1")
    assert row is not None
    # 入口会为每一行贡献写审计 —— 裸 SQL 时代这件事是漏的
    assert qv(db, "SELECT COUNT(*) FROM activity_log WHERE action='contribution.add'"
                  " AND target_id=?", int(row["id"])) == 1
    assert row["observed_at"] >= row["observed_at_min"]
    assert integrity_check(db)["ok"] is True


def test_write_entry_rejects_bogus_compaction_metadata(db):
    """入口对压实字段自身的约束（裸 SQL 时代完全没有）。"""
    env = Env(db)
    j = env.job()
    eid = env.obs(j)
    obs_row = q1(db, "SELECT * FROM observations WHERE id=?", eid)
    base: dict = dict(project_id=env.project_id, job_id=j, source_id=1,
                      evidence_id=env.evidence_id, subject_kind="observation",
                      subject_id=eid, contribution_kind="field",
                      owner_entity_id=int(obs_row["entity_id"]),
                      field_namespace="observation_field", semantic_field="tech",
                      field_value="nginx")
    with pytest.raises(InvariantError):
        store.add_contribution(db, store.NewContribution(**base, synthetic=2))
    with pytest.raises(InvariantError):
        store.add_contribution(db, store.NewContribution(**base, synthetic=1, job_count=0))
    with pytest.raises(InvariantError):
        store.add_contribution(db, store.NewContribution(**base, compacted_job_count=1))
    with pytest.raises(InvariantError):
        store.add_contribution(db, store.NewContribution(**base, represents_json="{}"))


# ---------------------------------------------------------------------------
# 债 #4 —— rekey 快照备份
# ---------------------------------------------------------------------------

def test_rekey_writes_a_usable_online_backup_snapshot(db, tmp_path):
    """§4.5 第 1 步的快照必须是一个**可打开、内容是改之前状态**的库。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="/report/2024", field="status", value="ok")
    ops.finish_job(db, j)
    eid = env.entity_id("/report/2024")

    j_rekey = env.job()
    rep = ops.rekey(db, project_id=env.project_id,
                    mapping={"/report/2024": "/report/{id}"}, job_id=j_rekey,
                    backup_dir=tmp_path)
    snap = Path(rep["backup"])
    assert snap.exists() and snap.parent == tmp_path

    copy = connect(str(snap))
    try:
        assert copy.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] > 0
        assert store.entity_for_key(copy, env.project_id, "/report/2024") == eid
        assert store.resolve_entity(copy, env.project_id, "/report/{id}") is None
    finally:
        copy.close()


def test_rekey_dry_run_also_takes_the_snapshot(db, tmp_path):
    """dry-run 与真实执行走同一代码路径 —— 快照那一步的失败要在预演时暴露。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="host:bk", field="tech", value="nginx")
    ops.finish_job(db, j)
    before = snapshot_effective(db)
    rep = ops.rekey(db, project_id=env.project_id, mapping={"host:bk": "host:bk2"},
                    dry_run=True, backup_dir=tmp_path)
    assert Path(rep["backup"]).exists()
    ok, diff = effective_equivalent(db, before)
    assert ok, diff


# ---------------------------------------------------------------------------
# 债 #5 —— 展示名口径
# ---------------------------------------------------------------------------

def test_display_key_contract_rekey_never_renames_the_entity(db):
    """展示名 = canonical entity 的创建期 key。rekey / merge 都只加解析入口，不改名。"""
    env = Env(db)
    j = env.job()
    env.obs(j, key="/report/2024", field="status", value="ok")
    ops.finish_job(db, j)
    eid = env.entity_id("/report/2024")
    assert store.display_key(db, eid) == "/report/2024"

    j_rekey = env.job()
    ops.rekey(db, project_id=env.project_id, mapping={"/report/2024": "/report/{id}"},
              job_id=j_rekey)
    assert store.resolve_entity(db, env.project_id, "/report/{id}") == eid
    assert store.display_key(db, eid) == "/report/2024"          # ← 展示名不变

    j2 = env.job()
    env.obs(j2, key="/report/2025", field="status", value="ok")
    ops.finish_job(db, j2)
    eid2 = env.entity_id("/report/2025")
    j_merge = env.job()
    ops.merge(db, project_id=env.project_id, survivor_key="/report/2024",
              member_keys=["/report/2025"], job_id=j_merge)
    assert store.canonical_of(db, eid2) == eid
    # survivor 的名称没有被被合并成员的名字顶掉（"最新 alias 胜出"会犯的错）
    assert store.display_key(db, eid2) == "/report/2024"


# ---------------------------------------------------------------------------
# 债 #7 —— 迁移机制
# ---------------------------------------------------------------------------

def test_schema_migrations_records_version_and_is_idempotent(db):
    from core import migrate as migrate_mod
    assert migrate_mod.current_version(db) == 1
    row = q1(db, "SELECT version, name, checksum FROM schema_migrations WHERE version=1")
    assert row["name"] == "init"
    assert row["checksum"] == migrate_mod.checksum(migrate_mod.MIGRATIONS[0].path)
    assert migrate_mod.migrate(db) == []                          # 幂等
    assert migrate_mod.pending(db) == []


def test_migrations_adopts_a_legacy_database_without_rerunning_sql():
    """旧 init_db 建出来的库（没有 registry）必须被采纳为 v1，不重跑 001。"""
    from core import migrate as migrate_mod
    conn = connect(":memory:")
    try:
        conn.executescript(
            (REPO_ROOT / "schema" / "001_init.sql").read_text(encoding="utf-8"))
        assert migrate_mod.applied(conn) == {}
        store.log_activity(conn, actor="x", action="job.start")
        assert migrate_mod.migrate(conn) == []                    # ← 不重跑 001
        assert migrate_mod.current_version(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrations_refuse_a_modified_applied_migration(db, tmp_path, monkeypatch):
    """已应用的迁移文件被改动 → 报错，而不是让人以为新老库结构一样。"""
    from core import migrate as migrate_mod
    forged = tmp_path / "001_init.sql"
    forged.write_text("-- 有人偷偷改了已应用的迁移\n", encoding="utf-8")
    monkeypatch.setattr(migrate_mod, "MIGRATIONS",
                        (migrate_mod.Migration(1, "init", forged),))
    with pytest.raises(migrate_mod.MigrationError):
        migrate_mod.migrate(db)


# ---------------------------------------------------------------------------
# 债 #6 —— parser 版本化 / 回滚
# ---------------------------------------------------------------------------

def test_parser_version_history_and_rollback_are_pointer_moves(db):
    """回滚 = 改指针：不删历史、不动旧 job 的指纹快照、可再回滚。"""
    env = Env(db)
    j1 = env.job(parser_fingerprint="p-a")
    env.obs(j1)
    ops.finish_job(db, j1)

    ops.set_parser(db, env.project_id, "p-b", profile_id="httpx", version="2")
    j2 = env.job(parser_fingerprint="p-b")
    env.obs(j2, key="host:b")
    ops.finish_job(db, j2)

    assert [h["fingerprint"] for h in ops.parser_history(db, profile_id="httpx")] == ["p-b"]
    assert {h["fingerprint"] for h in ops.parser_history(db, project_id=env.project_id)} \
        == {"p-a", "p-b"}

    rep = ops.rollback_parser(db, project_id=env.project_id, to_fingerprint="p-a")
    assert rep["parser_fingerprint"] == "p-a"
    assert {h["fingerprint"] for h in ops.parser_history(db)} == {"p-a", "p-b"}
    assert q1(db, "SELECT parser_fingerprint FROM jobs WHERE id=?", j2)["parser_fingerprint"] \
        == "p-b"
    assert ops.explain_job_scope(db, j1)["parser_drift"] is False
    assert ops.explain_job_scope(db, j2)["parser_drift"] is True
    # j1 的 scope_version 已经落后（set_parser 会 bump），所以它对"当前 coverage"
    # 天然不可用 —— 这正是历史快照存在的理由；但回滚之后**新** job 不再漂移。
    assert ops.explain_job_scope(db, j1)["interpret_with"] == "historical_scope"
    j3 = env.job(parser_fingerprint="p-a")
    assert ops.explain_job_scope(db, j3)["parser_drift"] is False
    assert ops.explain_job_scope(db, j3)["coverage_usable_for_inactive"] is True

    with pytest.raises(InvariantError):                # 未登记的指纹不能当回滚目标
        ops.rollback_parser(db, project_id=env.project_id, to_fingerprint="p-nope")
