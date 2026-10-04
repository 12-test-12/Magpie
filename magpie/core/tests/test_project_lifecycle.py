"""0.20.0 项目管理（Yakit 式）的数据层：归档 / 恢复 / 硬删。

三条边界各钉一条可执行的判据：

  * **归档是隐藏不是锁定**：`query.projects()` / `projects_detail()` 默认不列
    归档，但显式按名字（或 id）仍解析得开 —— 锁数据是删除的语义，归档只管
    清单；混起来会让"收起来"变成"丢了"。
  * **硬删按项目隔离**：A 名下的实体 / 观测 / 关系 / 作业 / 导入副本连根拔掉，
    B 一行不动、全局词表（sources / kind_registry）一行不动，收尾
    `PRAGMA foreign_key_check` 必须干净 —— "删得干净"要有证据。
  * **失败整树回滚**：故意喂一个错序（`_delete_order` 被 monkeypatch），让
    `DELETE projects` 炸在外键上 —— 事务必须把删了一半的行放回去。

表序本身**不在测试里复述**：`_delete_order` 从现场 `PRAGMA foreign_key_list`
算反拓扑，把清单抄进测试等于养一份会与实现双双过期的影子。
"""

from __future__ import annotations

import sqlite3

import pytest

from magpie.core import ops, query, store
from magpie.core.store import InvariantError, connect, init_db

T0 = "2026-01-01T00:00:00Z"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection, name: str) -> tuple[int, int, int]:
    """一个带真实行的项目（项目 + source + raw_file + evidence + job + 实体 +
    观测）。硬删要删的表种类够多，隔离断言才不是在验一张空表。"""
    project_id = ops.create_project(conn, name, {"include": [], "exclude": []})
    # source 是全局表：两次 seed 会重登同一个 id —— register_source 幂等
    # （Env 同款用法在跨项目用例里天天跑）。
    store.register_source(conn, source_id=1, name="nmap", authority_level="verified")
    raw_file_id = ops.add_raw_file(
        conn, project_id=project_id, locator_uri=f"file:///raw/{name}.txt",
        content="x\n", storage_state="present")
    evidence_id = ops.add_evidence(
        conn, project_id=project_id, raw_file_id=raw_file_id,
        locator={"line": 1}, snippet="x", quality_class="high")
    job_id = ops.start_job(
        conn, project_id=project_id, kind="scan", coverage="full",
        coverage_scope={"include": [], "exclude": []},
        parser_fingerprint="p-1", actor="cli")
    ops.add_observation(
        conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
        source_id=1, entity_key=f"host:{name}", kind="host",
        semantic_field="tech", value="nginx", observed_at=T0)
    ops.finish_job(conn, job_id, state="done")
    return project_id, job_id, evidence_id


def _count(conn: sqlite3.Connection, table: str, project_id: int) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project_id,)
    ).fetchone()[0])


def test_archive_hides_from_lists_but_stays_addressable(db):
    a, _, _ = _seed(db, "aaa")
    b, _, _ = _seed(db, "bbb")

    result = ops.archive_project(db, a)
    assert (result["status"], result["changed"]) == ("archived", True)
    assert ops.archive_project(db, a)["changed"] is False       # 幂等：不是错误

    # 清单默认不含归档；include_archived 看得见全部
    assert [row["name"] for row in query.projects(db)] == ["bbb"]
    assert [row["name"] for row in query.projects(db, include_archived=True)] \
        == ["aaa", "bbb"]
    assert [row["name"] for row in query.projects_detail(db)] == ["bbb"]
    assert [row["name"] for row in query.projects_detail(db, include_archived=True)] \
        == ["aaa", "bbb"]

    # 归档不是锁定：显式按名字 / id 照样解析得开
    assert query.project_id_for(db, "aaa") == a
    assert query.project_id_for(db, str(a)) == a

    assert ops.restore_project(db, a)["changed"] is True
    assert ops.restore_project(db, a)["changed"] is False        # 逆操作同样幂等
    assert [row["name"] for row in query.projects(db)] == ["aaa", "bbb"]

    with pytest.raises(InvariantError, match="not found"):
        ops.archive_project(db, 9999)
    with pytest.raises(InvariantError, match="not found"):
        ops.restore_project(db, 9999)


def test_delete_project_removes_only_its_rows(db):
    a, job_a, ev_a = _seed(db, "doomed")
    b, _, _ = _seed(db, "keeper")
    # 关系再给 A 添一个实体（host:other），让"实体连根拔"覆盖 >1 行
    ops.add_relation(db, project_id=a, job_id=job_a, evidence_id=ev_a, source_id=1,
                     src_key="host:doomed", dst_key="host:other", rel="resolves_to",
                     observed_at=T0)
    doomed_entities = [int(r["id"]) for r in db.execute(
        "SELECT id FROM entities WHERE project_id=?", (a,))]
    doomed_jobs = [int(r["id"]) for r in db.execute(
        "SELECT id FROM jobs WHERE project_id=?", (a,))]
    kind_rows = int(db.execute("SELECT COUNT(*) FROM kind_registry").fetchone()[0])
    source_rows = int(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0])

    result = ops.delete_project(db, a)

    # A 名下：项目自己、实体、观测、关系、作业、证据链 —— 逐表为零
    for table in ("entities", "observations", "relations", "jobs", "raw_files",
                  "evidence", "contributions", "entity_keys", "project_scope_history"):
        assert _count(db, table, a) == 0, f"{table} 还剩 A 的行"
    assert db.execute("SELECT COUNT(*) FROM projects WHERE id=?", (a,)).fetchone()[0] == 0
    # 不带 project_id 的两张：经外键挂上来，跟着走
    marks = ",".join("?" * len(doomed_entities))
    assert int(db.execute(
        f"SELECT COUNT(*) FROM entity_identity WHERE entity_id IN ({marks})",
        doomed_entities).fetchone()[0]) == 0
    marks = ",".join("?" * len(doomed_jobs))
    assert int(db.execute(
        f"SELECT COUNT(*) FROM activity_log WHERE job_id IN ({marks})",
        doomed_jobs).fetchone()[0]) == 0
    # 全局词表一行不动
    assert int(db.execute("SELECT COUNT(*) FROM kind_registry").fetchone()[0]) == kind_rows
    assert int(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]) == source_rows

    # B 一行不动
    assert _count(db, "entities", b) == 1
    assert _count(db, "observations", b) == 1
    assert _count(db, "jobs", b) == 1

    # 回执能对账 + 库不欠任何外键的债
    assert result["removed"]["projects"] == 1
    assert result["removed"]["entities"] == len(doomed_entities)
    assert list(db.execute("PRAGMA foreign_key_check")) == []
    assert [row["name"] for row in query.projects(db)] == ["keeper"]
    with pytest.raises(InvariantError, match="not found"):
        ops.delete_project(db, a)


def test_failed_delete_rolls_back_every_row(db, monkeypatch):
    a, _, _ = _seed(db, "doomed")
    b, _, _ = _seed(db, "keeper")

    # 故意喂错序：只删 observations，根（projects）随后就要面对还挂着
    # entities / jobs 的 NO ACTION 外键 —— 必炸在根上，而此时观测**已经删了**。
    # 这正是要验的：炸点必须发生在"有行已经没了"之后，回滚才有对象。
    monkeypatch.setattr(ops, "_delete_order", lambda conn: ["observations"])
    with pytest.raises(sqlite3.IntegrityError):
        ops.delete_project(db, a)

    assert _count(db, "observations", a) == 1          # 删掉的那一行回来了
    assert _count(db, "entities", a) == 1
    assert db.execute("SELECT COUNT(*) FROM projects WHERE id=?",
                      (a,)).fetchone()[0] == 1
    assert list(db.execute("PRAGMA foreign_key_check")) == []
    assert [row["name"] for row in query.projects(db)] == ["doomed", "keeper"]
