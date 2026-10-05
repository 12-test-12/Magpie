"""0.21.0 · `ops.add_finding` —— AI 发现通道的不变量。

这一批断言钉的是 add_finding 的**设计理由**，每条注释说明"红了意味着什么"：

  * 干跑同构（事实一行不写）—— 与 import 的 --dry-run 同一条纪律。
  * `authorship="manual"` → `observations.origin="manual"` —— GUI「人工写入」
    分组的展示完全依赖这个翻转；红了 = AI 的发现不出现在页面上。
  * **零 `work_journal`** —— 追加型事实的撤销走 supersession，不走工作层回放。
    多一行 work_journal 意味着有人把这当成"可回放的字段修改"，语义就错了。
  * **零 inbox 冲突** —— `semantic_field` 内容寻址是防假冲突的关键；
    第二次提交不同文本不许产生任何 inbox 项。
  * undo / redo 逐行回到原状 —— 与 2.6 B 组同一条判据
    （`store.effective_equivalent`），不是另发明一条。

前置条件同 `test_work_layer.py`：`conn` 必须是 `store.connect()` 建出来的。

运行: py -3.13 -m pytest magpie/core/tests/test_add_finding.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from magpie.core import ops, query, store                  # noqa: E402
from magpie.core.store import (                            # noqa: E402
    InvariantError, assert_integrity, connect, effective_equivalent,
    init_db, integrity_check,
)


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def make_project(conn: sqlite3.Connection, name: str = "ai-note") -> int:
    return ops.create_project(conn, name,
                              {"include": ["*.example.com"], "exclude": []})


def seed_one(conn: sqlite3.Connection, project_id: int, key: str = "host:a") -> int:
    """一个实体，走与 test_work_layer 相同的最小证据链。"""
    store.register_source(conn, source_id=1, name="nmap",
                          authority_level="verified")
    raw = ops.add_raw_file(conn, project_id=project_id,
                           locator_uri="file:///seed.txt", content="seed\n")
    evidence = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw,
                                locator={"line": 1}, snippet="seed")
    job = ops.start_job(conn, project_id=project_id, kind="scan",
                        coverage="full", parser_fingerprint="p-1", actor="cli")
    ops.add_observation(conn, project_id=project_id, job_id=job,
                        evidence_id=evidence, source_id=1, entity_key=key,
                        kind="host", semantic_field="tech", value="nginx")
    ops.finish_job(conn, job, "done")
    store.recompute(conn)
    assert_integrity(conn)
    return store.entity_for_key(conn, project_id, key)


def counts(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def origin_of(conn: sqlite3.Connection, obs_id: int) -> str:
    row = conn.execute("SELECT origin FROM observations WHERE id=?",
                       (obs_id,)).fetchone()
    return str(row["origin"])


# ---------------------------------------------------------------------------
# 正路
# ---------------------------------------------------------------------------

def test_add_finding_lands_as_manual_observation_in_one_job(db):
    """一次提交 = 一个 ai_note job + origin 翻成 manual（页面展示的前提）。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    jobs_before = counts(db, "jobs")
    report = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                             text="首页存在未鉴权的 /api/debug 端点",
                             actor="api")

    assert counts(db, "jobs") == jobs_before + 1
    assert report["job_id"] is not None
    job = db.execute("SELECT kind, state, applied FROM jobs WHERE id=?",
                     (report["job_id"],)).fetchone()
    assert (job["kind"], job["state"], job["applied"]) == ("ai_note", "done", 1)
    assert origin_of(db, report["observation_id"]) == "manual"
    assert report["source"] == "ai:add_finding"
    # 内容寻址：字段名带文本指纹
    assert report["semantic_field"].startswith("ai_note:")
    assert_integrity(db)


def test_add_finding_is_pure_append_no_work_journal(db):
    """零 work_journal：追加型事实的撤销走 supersession，不走工作层回放。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    assert counts(db, "work_journal") == 0
    ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                    text="发现 A", actor="api")
    assert counts(db, "work_journal") == 0
    assert_integrity(db)


def test_add_finding_dry_run_writes_nothing(db):
    """干跑同构：报告照出，事实一行不写，job_id 为 None。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)
    before = store.snapshot_effective(db)

    report = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                             text="干跑的发现", actor="api", dry_run=True)

    assert report["dry_run"] is True
    assert report["job_id"] is None
    assert report["op"] == "ai_note.add"
    assert store.snapshot_effective(db) == before, "干跑之后库必须逐行没变"
    assert_integrity(db)


def test_add_finding_two_distinct_notes_do_not_open_inbox(db):
    """内容寻址字段防假冲突：两条不同发现 → 零 inbox 项。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                    text="第一条发现", actor="api")
    ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                    text="第二条发现", actor="api")

    assert counts(db, "inbox_items") == 0
    assert_integrity(db)


def test_add_finding_same_text_twice_is_stable(db):
    """同一段文本重复提交：同字段同值，仍 1 个 distinct value，无冲突。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    first = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                            text="重复的发现", actor="api")
    second = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                             text="重复的发现", actor="api")

    assert first["semantic_field"] == second["semantic_field"]
    assert counts(db, "inbox_items") == 0
    assert_integrity(db)


# ---------------------------------------------------------------------------
# 反路（每个都指名一种会被误读的失败）
# ---------------------------------------------------------------------------

def test_add_finding_rejects_unknown_entity(db):
    """实体不存在（或属于别的项目）→ 写第一行之前就炸，一个字不落。"""
    project_id = make_project(db)
    seed_one(db, project_id)
    before = store.snapshot_effective(db)

    with pytest.raises(InvariantError, match="不在项目"):
        ops.add_finding(db, project_id=project_id, entity_id=99999,
                        text="找不到实体", actor="api")
    assert store.snapshot_effective(db) == before, "失败之后库必须逐行没变"


def test_add_finding_rejects_empty_text(db):
    """空白文本没有信息量；落库会得到一个永远没人读的观测。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    with pytest.raises(InvariantError, match="不能为空"):
        ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                        text="   \n  ", actor="api")


# ---------------------------------------------------------------------------
# 撤销 / 复原（复用 2.6 B 组的判据，不另发明）
# ---------------------------------------------------------------------------

def test_add_finding_undo_and_redo_roundtrip(db):
    """undo 后观测消失、实体回到导入时的状态；redo 逐行还原。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)
    baseline = store.snapshot_effective(db)

    report = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                             text="可撤销的发现", actor="api")
    assert store.snapshot_effective(db) != baseline

    ops.undo(db, report["job_id"], actor="cli")
    ok, diff = effective_equivalent(db, baseline)
    assert ok, f"undo 之后必须逐行回到导入时的状态: {diff}"
    row = db.execute("SELECT is_current FROM observations WHERE id=?",
                     (report["observation_id"],)).fetchone()
    assert row is not None and int(row["is_current"]) == 0
    assert_integrity(db)

    ops.redo(db, report["job_id"], actor="cli")
    assert origin_of(db, report["observation_id"]) == "manual"
    assert_integrity(db)


def test_add_finding_shows_up_in_entity_detail(db):
    """页面链路的后端一半：entity_detail 必须把它列进 observations。"""
    project_id = make_project(db)
    entity_id = seed_one(db, project_id)

    report = ops.add_finding(db, project_id=project_id, entity_id=entity_id,
                             text="出现在详情里的发现", actor="api")
    detail = query.entity_detail(db, project_id, entity_id)
    matched = [o for o in detail["observations"]
               if int(o["id"]) == int(report["observation_id"])]
    assert matched, "AI 发现没有出现在 entity_detail 的观测列表里"
    assert matched[0]["origin"] == "manual"
