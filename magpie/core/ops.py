"""事务型状态迁移（P0-D · 审阅点 11 / 12 / 9 / 10）。

统一模板 T1:
    operation -> impact_set -> provenance -> recompute -> integrity -> commit

四个操作（undo / rekey / merge / compact）全部走同一模板，且全部支持 --dry-run，
dry-run 必须走**同一代码路径**（只有 COMMIT 变成 ROLLBACK），否则 dry-run 会说谎。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import store
from .store import (
    UNHASHED_PARSER,
    InvariantError,
    NewContribution,
    add_contribution,
    apply_purge,
    assert_integrity,
    atomic,
    canonical_json,
    connect,
    entity_for_key,
    fingerprint,
    log_activity,
    now,
    orphan_subjects,
    purge_eligible,
    recompute,
    register_parser,
    scope_hash,
    sha256_hex,
    transaction,
)


# ---------------------------------------------------------------------------
# 项目 / 作业 / 证据（测试与生产共用的最小写入 API）
# ---------------------------------------------------------------------------

def _canonical_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """scope 的规范形式：数组排序（否则同一 scope 会存成两种文本，审计不可比）。"""
    out = dict(scope)
    for key in ("include", "exclude"):
        if isinstance(out.get(key), list):
            out[key] = sorted(out[key])
    return out


def create_project(conn: sqlite3.Connection, name: str, scope: dict[str, Any]) -> int:
    scope = _canonical_scope(scope)
    h = scope_hash(scope)
    fingerprint_value = scope.get("parser_fingerprint")
    if fingerprint_value is not None:
        # 名称级登记：scope 上的 fingerprint 是**一个名字**，这里没有任何实现
        # 可 hash，所以显式标注 UNHASHED（同期必交项 B 的语义分界）。
        register_parser(conn, str(fingerprint_value), definition_hash=UNHASHED_PARSER)
    cur = conn.execute(
        "INSERT INTO projects(name, scope, scope_version, scope_hash, parser_fingerprint)"
        " VALUES (?,?,1,?,?)",
        (name, canonical_json(scope), h, fingerprint_value),
    )
    pid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO project_scope_history(project_id, version, scope, scope_hash,"
        " changed_at, changed_by, reason) VALUES (?,1,?,?,?,'system','init')",
        (pid, canonical_json(scope), h, now()),
    )
    return pid


def update_scope(conn: sqlite3.Connection, project_id: int, scope: dict[str, Any],
                 actor: str = "cli", reason: str = "manual") -> tuple[int, str]:
    """改 scope 必须 bump version（审阅点 7），否则 coverage 判定会撒谎。

    v3.2-r2: parser 指纹随 scope 一起版本化（§5.1）—— 它决定"覆盖"的含义。
    """
    row = conn.execute(
        "SELECT scope_version FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if row is None:
        raise InvariantError(f"project {project_id} not found")
    scope = _canonical_scope(scope)
    version = int(row["scope_version"]) + 1
    h = scope_hash(scope)
    fingerprint_value = scope.get("parser_fingerprint")
    if fingerprint_value is not None:
        register_parser(conn, str(fingerprint_value), definition_hash=UNHASHED_PARSER)
    conn.execute(
        "UPDATE projects SET scope=?, scope_version=?, scope_hash=?, parser_fingerprint=?"
        " WHERE id=?",
        (canonical_json(scope), version, h, fingerprint_value, project_id),
    )
    conn.execute(
        "INSERT INTO project_scope_history(project_id, version, scope, scope_hash,"
        " changed_at, changed_by, reason) VALUES (?,?,?,?,?,?,?)",
        (project_id, version, canonical_json(scope), h, now(), actor, reason),
    )
    log_activity(conn, actor=actor, action="reparse", target_kind="project",
                 target_id=project_id, detail={"scope_version": version, "reason": reason})
    return version, h


def set_parser(conn: sqlite3.Connection, project_id: int, fingerprint: str, *,
               profile_id: str | None = None, version: str | None = None,
               definition_hash: str = UNHASHED_PARSER,
               actor: str = "cli", reason: str = "parser_change") -> dict[str, Any]:
    """切换项目当前 parser 指纹（**事实操作**，v3.2-r2 落库）。

    必须同时做三件事，否则旧 job 会被当前 scope/parser 误解释:
      1. 登记 fingerprint 到 parser_profiles（事实 + 审计）
      2. bump scope_version 并重算 scope_hash（hash 含 fingerprint）
      3. 写 project_scope_history，使 explain_job_scope 能用当时的快照解释旧 job
    """
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise InvariantError(f"project {project_id} not found")
    # 默认是 UNHASHED（调用方通常只有一个名字）；手里真有实现时传进来，
    # 它会**回填**到一个原本只登记了名字的行上（见 store.register_parser）。
    register_parser(conn, fingerprint, profile_id=profile_id, version=version,
                    definition_hash=definition_hash, actor=actor)
    scope = json.loads(str(row["scope"]))
    scope["parser_fingerprint"] = fingerprint
    if profile_id is not None:
        scope["parser_profile"] = profile_id
    new_version, new_hash = update_scope(conn, project_id, scope, actor=actor, reason=reason)
    log_activity(conn, actor=actor, action="parser.set", target_kind="project",
                 target_id=project_id,
                 detail={"fingerprint": fingerprint, "scope_version": new_version})
    return {"op": "set_parser", "project_id": project_id,
            "parser_fingerprint": fingerprint, "scope_version": new_version,
            "scope_hash": new_hash}


def parser_history(conn: sqlite3.Connection, *, profile_id: str | None = None,
                   project_id: int | None = None) -> list[dict[str, Any]]:
    """已登记的 parser 指纹（v3.2-r4 · 债 #6 的"版本化"半边）。

    为什么这是"版本化"而不是一张字典表: 每个 job 冻结了自己跑时的
    `parser_fingerprint`（外键），所以"这个结论是哪个 parser 版本产出的"
    永远可回答。`definition_hash` 是"同一个 fingerprint 背后的实现有没有变过"
    的判据 —— **第一个真解析器落地时必须填**（阶段 2 第 2 步的验收项）。
    """
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM jobs j"
           "                 WHERE j.parser_fingerprint = p.fingerprint) AS job_count"
           "  FROM parser_profiles p")
    args: list[Any] = []
    where: list[str] = []
    if profile_id is not None:
        where.append("p.profile_id = ?")
        args.append(profile_id)
    if project_id is not None:
        where.append("EXISTS (SELECT 1 FROM jobs j2 WHERE j2.parser_fingerprint = p.fingerprint"
                     " AND j2.project_id = ?)")
        args.append(project_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.registered_at, p.fingerprint"
    return [{"fingerprint": str(r["fingerprint"]), "profile_id": r["profile_id"],
             "version": r["version"], "definition_hash": r["definition_hash"],
             "registered_at": str(r["registered_at"]), "job_count": int(r["job_count"])}
            for r in conn.execute(sql, args)]


def rollback_parser(conn: sqlite3.Connection, *, project_id: int,
                    to_fingerprint: str, actor: str = "cli",
                    reason: str = "parser_rollback") -> dict[str, Any]:
    """回滚项目的 parser 指纹（v3.2-r4 · 债 #6 的"回滚"半边）。

    **回滚不是撤销**: 不删任何 parser_profiles 行、不改任何历史 job 的指纹快照，
    只是把"当前指针"改回一个**已登记**的旧指纹。这与 §4.5 的 rekey 同一哲学 ——
    历史是事实，当前状态是覆盖在上面的指针。

    因此回滚天然安全且可再回滚，`explain_job_scope` 对回滚前后的 job 都成立。
    拒绝回滚到未登记的指纹: 那样 coverage 判定会引用一个不存在的版本。
    """
    row = conn.execute(
        "SELECT fingerprint FROM parser_profiles WHERE fingerprint=?", (to_fingerprint,)
    ).fetchone()
    if row is None:
        raise InvariantError(
            f"parser rollback target {to_fingerprint!r} is not registered "
            f"(未登记的指纹无法作为回滚目标)")
    prof = conn.execute(
        "SELECT profile_id, version FROM parser_profiles WHERE fingerprint=?",
        (to_fingerprint,)).fetchone()
    report = set_parser(conn, project_id, to_fingerprint,
                        profile_id=None if prof is None else prof["profile_id"],
                        version=None if prof is None else prof["version"],
                        actor=actor, reason=reason)
    report["op"] = "rollback_parser"
    return report


def start_job(conn: sqlite3.Connection, *, project_id: int, kind: str,
              coverage: str, coverage_scope: dict[str, Any] | None = None,
              parser_fingerprint: str | None = None, actor: str = "cli") -> int:
    """job 必须冻结 coverage + scope + parser 快照（审阅点 7 / v3.2-r2 §5.1）。

    parser 指纹是事实：这里先登记（幂等），再让 jobs 通过 FK 引用它。
    """
    proj = conn.execute(
        "SELECT scope_version, scope_hash FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if proj is None:
        raise InvariantError(f"project {project_id} not found")
    if parser_fingerprint is not None:
        # job 只是**快照**一个名字，实现 hash 该由真正跑解析的那条路先登记好
        # （`ingest` 就是在 start_job 之前登记完整的 —— 顺序反了 definition_hash
        # 会永久留空，那正是 v3.2 §16.4 第 2 条记录的病）。
        # 这里显式标 UNHASHED：轮到这里才第一次出现的 fingerprint 意味着没有
        # 实现参与，而不是"实现没有 hash"。
        register_parser(conn, parser_fingerprint, definition_hash=UNHASHED_PARSER,
                        actor=actor)
    cur = conn.execute(
        "INSERT INTO jobs(project_id, kind, state, coverage, coverage_scope,"
        " scope_version, scope_hash, parser_fingerprint, actor, started_at)"
        " VALUES (?,?,'running',?,?,?,?,?,?,?)",
        (project_id, kind, coverage,
         None if coverage_scope is None else canonical_json(coverage_scope),
         int(proj["scope_version"]), str(proj["scope_hash"]), parser_fingerprint,
         actor, now()),
    )
    job_id = int(cur.lastrowid)
    log_activity(conn, actor=actor, action="job.start", target_kind="job",
                 target_id=job_id, job_id=job_id,
                 detail={"coverage": coverage, "scope_version": int(proj["scope_version"]),
                         "parser_fingerprint": parser_fingerprint})
    return job_id


def finish_job(conn: sqlite3.Connection, job_id: int, state: str = "done",
               actor: str = "cli") -> None:
    if state not in ("done", "failed", "partial", "aborted"):
        raise InvariantError(f"bad job state {state!r}")
    conn.execute("UPDATE jobs SET state=?, finished_at=? WHERE id=?",
                 (state, now(), job_id))
    log_activity(conn, actor=actor, action="job.finish", target_kind="job",
                 target_id=job_id, job_id=job_id, detail={"state": state})


def add_raw_file(conn: sqlite3.Connection, *, project_id: int, locator_uri: str,
                 content: str, storage_state: str = "present") -> int:
    cur = conn.execute(
        "INSERT INTO raw_files(project_id, locator_uri, sha256, byte_count,"
        " storage_state, created_at) VALUES (?,?,?,?,?,?)",
        (project_id, locator_uri, sha256_hex(content), len(content.encode("utf-8")),
         storage_state, now()),
    )
    return int(cur.lastrowid)


def add_evidence(conn: sqlite3.Connection, *, project_id: int, raw_file_id: int,
                 locator: dict[str, Any], snippet: str,
                 quality_class: str = "medium",
                 recovery_state: str = "full") -> int:
    cur = conn.execute(
        "INSERT INTO evidence(project_id, raw_file_id, locator, sha256, quality_class,"
        " recovery_state, created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, raw_file_id, canonical_json(locator), sha256_hex(snippet),
         quality_class, recovery_state, now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 高阶写入：一次调用写 "实体/字段/关系" 的完整贡献闭包
# ---------------------------------------------------------------------------

def upsert_entity(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                  evidence_id: int, source_id: int, entity_key: str, kind: str,
                  authorship: str = "tool", **time_kw: Any) -> int:
    from .store import resolve_entity, new_entity

    with atomic(conn):
        entity_id = resolve_entity(conn, project_id, entity_key)
        if entity_id is None:
            entity_id = new_entity(conn, project_id=project_id, kind=kind,
                                  entity_key=entity_key)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="entity", subject_id=entity_id,
            contribution_kind="entity", authorship=authorship, **time_kw))
    return entity_id


def ensure_observation_row(conn: sqlite3.Connection, *, project_id: int, entity_id: int,
                           semantic_field: str, value: str,
                           value_kind: str = "string", **time_kw: Any) -> int:
    """取出/创建 observation 缓存行（不写贡献；结构列一次性冻结）。"""
    row = conn.execute(
        "SELECT id FROM observations WHERE project_id=? AND entity_id=?"
        " AND field_namespace='observation_field' AND semantic_field=? AND value=?",
        (project_id, entity_id, semantic_field, value),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO observations(project_id, entity_id, field_namespace, semantic_field,"
        " value, value_kind, is_current, confidence, source_count, job_count,"
        " logical_job_count, origin, observed_at, observed_at_source,"
        " observed_at_accuracy, observed_at_precision, created_at)"
        " VALUES (?,?,'observation_field',?,?,?,0,'none',0,0,0,'tool',NULL,NULL,NULL,NULL,?)",
        (project_id, entity_id, semantic_field, value, value_kind, now()),
    )
    return int(cur.lastrowid)


def ensure_relation_row(conn: sqlite3.Connection, *, project_id: int, src_id: int,
                        dst_id: int, rel: str) -> int:
    row = conn.execute(
        "SELECT id FROM relations WHERE project_id=? AND src_id=? AND dst_id=? AND rel=?",
        (project_id, src_id, dst_id, rel),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO relations(project_id, src_id, dst_id, rel, is_current, confidence,"
        " source_count, job_count, created_at) VALUES (?,?,?,?,0,'none',0,0,?)",
        (project_id, src_id, dst_id, rel, now()),
    )
    return int(cur.lastrowid)


def add_entity_field(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                     evidence_id: int, source_id: int, entity_key: str, kind: str,
                     semantic_field: str, value: str, value_kind: str = "string",
                     authorship: str = "tool", **time_kw: Any) -> int:
    """entity 级字段（display_name / cpe / tech 等），不建独立缓存表。"""
    with atomic(conn):
        entity_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                                 evidence_id=evidence_id, source_id=source_id,
                                 entity_key=entity_key, kind=kind, authorship=authorship,
                                 **time_kw)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="entity", subject_id=entity_id,
            contribution_kind="field", authorship=authorship,
            field_namespace="entity_field", semantic_field=semantic_field,
            field_value=value, value_kind=value_kind, **time_kw))
    return entity_id


def add_observation(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                    evidence_id: int, source_id: int, entity_key: str, kind: str,
                    semantic_field: str, value: str, value_kind: str = "string",
                    authorship: str = "tool", **time_kw: Any) -> int:
    """observation 是 field 贡献的规范锚点: 先确保缓存行存在，再写贡献。

    INV-16: 贡献**显式记录** owner_entity_id（observation → entity 的事实链）。
    注意这里**没有**任何 DELETE contributions —— INV-01 无例外（undo 靠 supersession）。
    """
    entity_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                             evidence_id=evidence_id, source_id=source_id,
                             entity_key=entity_key, kind=kind, authorship=authorship,
                             **time_kw)
    with atomic(conn):
        obs_id = ensure_observation_row(conn, project_id=project_id, entity_id=entity_id,
                                        semantic_field=semantic_field, value=value,
                                        value_kind=value_kind, **time_kw)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="observation", subject_id=obs_id,
            owner_entity_id=entity_id, contribution_kind="field", authorship=authorship,
            field_namespace="observation_field", semantic_field=semantic_field,
            field_value=value, value_kind=value_kind, **time_kw))
    return obs_id


def add_relation(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                 evidence_id: int, source_id: int, src_key: str, dst_key: str,
                 rel: str, kind: str = "host", authorship: str = "tool",
                 **time_kw: Any) -> int:
    """relation 事实：subject 是 relations 缓存行，INV-16 的 owner 记 **src entity**，
    dst 记在 field_value.dst_id —— 于是 relations 行也能从事实层完整重建。"""
    src_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                          evidence_id=evidence_id, source_id=source_id,
                          entity_key=src_key, kind=kind, authorship=authorship, **time_kw)
    dst_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                          evidence_id=evidence_id, source_id=source_id,
                          entity_key=dst_key, kind=kind, authorship=authorship, **time_kw)
    with atomic(conn):
        rel_id = ensure_relation_row(conn, project_id=project_id, src_id=src_id,
                                     dst_id=dst_id, rel=rel)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="relation", subject_id=rel_id,
            owner_entity_id=src_id, contribution_kind="relation", authorship=authorship,
            relation_kind=rel,
            field_value=canonical_json({"dst_id": dst_id, "dst_key": dst_key}),
            **time_kw))
    return rel_id


# ---------------------------------------------------------------------------
# undo（T2）—— 审阅点 4 的正面回答
# ---------------------------------------------------------------------------

def _recompute_subjects_of_job(conn: sqlite3.Connection, job_id: int) -> list[tuple[str, int]]:
    """job 触及的**缓存行**集合（不是贡献的原始 subject）。

    为什么需要映射（v3.2-r3 修正）:
      `contribution_kind='relation'` 的贡献 subject 是 **relations 行本身**，
      结构行跟着变的还有 src（owner_entity_id）与 dst（field_value.dst_id）两个 entity。
      旧实现把 subject_id 当成 src_id 去反查 relations，于是 relation 行从来没被
      重算过（INV-04 会当场抓到 cache_overstated_relation）。
    """
    out: set[tuple[str, int]] = set()
    for row in conn.execute(
        """
        SELECT c.subject_kind, c.subject_id, c.owner_entity_id, c.contribution_kind,
               c.relation_kind, c.field_value
          FROM contributions c WHERE c.job_id = ?
        UNION ALL
        SELECT c.subject_kind, c.subject_id, c.owner_entity_id, c.contribution_kind,
               c.relation_kind, c.field_value
          FROM contribution_supersessions s JOIN contributions c ON c.id = s.contribution_id
         WHERE s.superseded_by_job_id = ?
        """,
        (job_id, job_id),
    ):
        if str(row["contribution_kind"]) == "relation":
            out.add(("relation", int(row["subject_id"])))
            if row["owner_entity_id"] is not None:
                out.add(("entity", int(row["owner_entity_id"])))
            try:
                out.add(("entity", int(_dst_of(row["field_value"]))))
            except (TypeError, ValueError, KeyError):
                pass
        else:
            out.add((str(row["subject_kind"]), int(row["subject_id"])))
            if row["owner_entity_id"] is not None:
                out.add(("entity", int(row["owner_entity_id"])))
    return sorted(out)


def _dst_of(field_value: Any) -> int:
    """从 relation 贡献的 field_value 里取 dst_id（关系的另一端点）。"""
    return int(json.loads(str(field_value))["dst_id"])


def undo(conn: sqlite3.Connection, job_id: int, *, actor: str = "cli",
         dry_run: bool = False, purge: bool = True) -> dict[str, Any]:
    """撤销一个 job（T2）。v3.2-r4: **完全可逆** —— 与 `redo` 对称。

    事实层: 追加 retract 抑制事实（不删贡献、不改贡献，INV-01 无例外）。
    身份层: 不再 DELETE alias / merge members，而是写**生命周期边**
            （`revoked_by_job_id` / `unmerged_by_job_id`）——
            否则 alias 的行内容一删，redo 就再也无法重建（key 映射不在事实层里）。
    工作层: 关闭 J 打开的自动 inbox 项（`closed_by_job_id`）；
            undo 自己打开的孤儿项打上 `opened_by_undo_job_id`，供 redo 关闭。
    执行状态: 只改 `jobs.applied = 0`，**不再**把 state 改成 'aborted'
            （"跑完了但被撤销" 与 "执行失败" 是两件事）。

    幂等: 已撤销的 job 再次 undo 不会重复写边（都带 `IS NULL` 守卫）。
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")

    with transaction(conn, dry_run=dry_run):
        # ---- 1. impact_set ----
        subjects = _recompute_subjects_of_job(conn, job_id)
        impacted_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM contributions WHERE job_id=?", (job_id,))]

        # ---- 2. provenance: 追加 retract 抑制事实 ----
        report_superseded = 0
        for cid in impacted_ids:
            exists = conn.execute(
                "SELECT 1 FROM contribution_supersessions WHERE contribution_id=?", (cid,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO contribution_supersessions(project_id, contribution_id,"
                " superseded_by_job_id, reason, created_at, actor) VALUES (?,?,?,?,?,?)",
                (int(job["project_id"]), cid, job_id, "retract", now(), actor),
            )
            report_superseded += 1
        log_activity(conn, actor=actor, action="supersession.add", target_kind="job",
                     target_id=job_id, job_id=job_id,
                     detail={"retract": report_superseded})

        # ---- 3. 身份层: 撤销 J 建立的 alias / merge 事件（写边，不删行）----
        alias_revoked = conn.execute(
            "UPDATE entity_alias SET revoked_by_job_id=?, revoked_at=?"
            " WHERE created_by_job_id=? AND revoked_by_job_id IS NULL",
            (job_id, now(), job_id),
        ).rowcount
        merge_events = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM entity_merge_events WHERE created_by_job_id=? AND state='merged'",
            (job_id,))]
        members_revoked = 0
        for ev in merge_events:
            members_revoked += conn.execute(
                "UPDATE entity_merge_members SET revoked_by_job_id=?, revoked_at=?"
                " WHERE merge_event_id=? AND revoked_by_job_id IS NULL",
                (job_id, now(), ev)).rowcount
            conn.execute(
                "UPDATE entity_merge_events SET state='unmerged', unmerged_at=?,"
                " unmerged_by_job_id=? WHERE id=? AND state='merged'",
                (now(), job_id, ev))

        # ---- 4. 工作层: J 自动生成的 inbox 项 ----
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='undo',"
            " closed_by_job_id=? WHERE created_by_job_id=? AND state='open'"
            " AND auto_generated=1 AND closed_by_job_id IS NULL",
            (now(), job_id, job_id),
        ).rowcount

        # ---- 5. 缓存重算（delta） ----
        recompute(conn, delta=subjects)

        # ---- 6. GC 评估（v3.2-r2 起默认不物理删除）----
        purge_report: dict[str, Any] = {"purge": [], "retained": [], "orphan_subjects": []}
        if purge:
            purge_report = purge_eligible(conn)
            for orphan in purge_report["orphan_subjects"]:
                kind, raw = orphan.split(":", 1)
                # `created` 有意弃用: 无论这条 open 项是新建的还是**复用**已存在的，
                # 它都是"undo 打开的"，redo 都必须关掉它（否则 redo 之后残留一个
                # 语义已经不成立的孤儿项）。旧写法 `if created or True` 是恒真的
                # 死条件，读起来像有分支、实际没有。
                item_id, _ = _open_inbox(
                    conn, project_id=int(job["project_id"]), kind="orphan_subject",
                    subject_kind=kind, subject_id=int(raw),
                    values=[], sources=[], job_id=job_id,
                    payload={"reason": "no_contribution_ever"})
                # 标记"这条 open 项是 undo 打开的"，redo 会关闭它
                conn.execute(
                    "UPDATE inbox_items SET opened_by_undo_job_id=?"
                    " WHERE id=? AND opened_by_undo_job_id IS NULL", (job_id, item_id))
            apply_purge(conn, purge_report["purge"], actor=actor, job_id=job_id)
            for item in purge_report["retained"]:
                log_activity(conn, actor=actor, action="gc.retained",
                             target_kind=item["subject"].split(":")[0],
                             target_id=int(item["subject"].split(":")[1]),
                             job_id=job_id, detail={"blockers": item["blockers"]})

        # ---- 7. 应用状态 + integrity ----
        conn.execute("UPDATE jobs SET applied=0 WHERE id=?", (job_id,))
        assert_integrity(conn)

        # ---- 8. 审计 ----
        report = {
            "op": "undo",
            "job_id": job_id,
            "dry_run": dry_run,
            "impact": {
                "contributions": len(impacted_ids),
                "superseded": report_superseded,
                "alias_revoked": alias_revoked,
                "merge_members_revoked": members_revoked,
                "merge_events_reverted": len(merge_events),
                "inbox_closed": inbox_closed,
                "purge_eligible": purge_report["purge"],
                "purge_retained": purge_report["retained"],
                "inbox_opened": len(purge_report["orphan_subjects"]),
            },
            "integrity": {"ok": True},
        }
        log_activity(conn, actor=actor, action="undo", target_kind="job",
                     target_id=job_id, job_id=job_id, detail=report["impact"])
        return report


def redo(conn: sqlite3.Connection, job_id: int, *, actor: str = "cli",
         dry_run: bool = False) -> dict[str, Any]:
    """重新应用一个被 undo 的 job（T2′）。`redo` 与 `undo` 严格互逆。

    动作 = **清掉所有指向 J 的生命周期边** + `jobs.applied = 1`：
      事实层: DELETE `contribution_supersessions WHERE superseded_by_job_id=J
              AND reason='retract'`（每次 DELETE 都写 `supersession.lift`）。
              **只解除 retract 行** —— 那是 undo 的产物；
              `reason='compact'` 等行是 J 自己的效果，redo 不得触碰，
              否则被吞并的原始贡献与合成行会同时活跃、守恒被破坏。
      身份层: 解除 `revoked_by_job_id=J`（alias / merge members）；
              重新 unmerge 那些"由 J 反向合并回去"的事件（幂等对称）。
      工作层: 重开 `closed_by_job_id=J` 的项（若同 fingerprint 已有 open 项，
              则并入它的 occurrence_count 而不破唯一索引）；关闭
              `opened_by_undo_job_id=J` 的项。
      应用状态: `applied=1`。

    幂等: 已经没有 retract 行且 `applied=1` 时，返回 `{"noop": True}` 报告。
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")
    project_id = int(job["project_id"])

    with transaction(conn, dry_run=dry_run):
        retract_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM contribution_supersessions WHERE superseded_by_job_id=?"
            " AND reason='retract'", (job_id,))]
        alias_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM entity_alias WHERE revoked_by_job_id=?", (job_id,))]
        member_rows = [(int(r["merge_event_id"]), int(r["entity_id"])) for r in conn.execute(
            "SELECT merge_event_id, entity_id FROM entity_merge_members"
            " WHERE revoked_by_job_id=?", (job_id,))]
        closed_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM inbox_items WHERE closed_by_job_id=?", (job_id,))]
        opened_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM inbox_items WHERE opened_by_undo_job_id=?", (job_id,))]

        if (not retract_ids and int(job["applied"]) == 1 and not alias_ids
                and not member_rows and not closed_ids and not opened_ids):
            return {"op": "redo", "job_id": job_id, "dry_run": dry_run, "noop": True,
                    "impact": {"supersessions_lifted": 0, "alias_restored": 0,
                               "merge_members_restored": 0, "inbox_reopened": 0,
                               "inbox_closed": 0, "blocked": []},
                    "integrity": {"ok": True}}

        # ---- 1. 事实层: 解除 undo 造成的抑制（只动 reason='retract'）----
        for sid in retract_ids:
            conn.execute("DELETE FROM contribution_supersessions WHERE id=?", (sid,))
            log_activity(conn, actor=actor, action="supersession.lift",
                         target_kind="supersession", target_id=sid, job_id=job_id,
                         detail={"reason": "retract"})

        # ---- 2. 身份层: 解除撤销边 ----
        blocked: list[dict[str, Any]] = []
        alias_restored = 0
        for aid in alias_ids:
            row = conn.execute(
                "SELECT project_id, alias_key FROM entity_alias WHERE id=?", (aid,)
            ).fetchone()
            conflict = conn.execute(
                "SELECT id, entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL", (int(row["project_id"]),
                                                  str(row["alias_key"]))).fetchone()
            if conflict is not None:
                # 更新的身份决定已经占用了这个 key → 保留撤销边，汇报而不是静默覆盖
                blocked.append({"alias_id": aid, "alias_key": str(row["alias_key"]),
                                "conflicting_alias_id": int(conflict["id"])})
                continue
            conn.execute(
                "UPDATE entity_alias SET revoked_by_job_id=NULL, revoked_at=NULL WHERE id=?",
                (aid,))
            alias_restored += 1
        members_restored = 0
        for event_id, entity_id in member_rows:
            members_restored += conn.execute(
                "UPDATE entity_merge_members SET revoked_by_job_id=NULL, revoked_at=NULL"
                " WHERE merge_event_id=? AND entity_id=?", (event_id, entity_id)).rowcount
        # 重新合并"由 undo(J) 拆开"的事件（undo 把 state 改成 unmerged 并写下边）
        merge_events_restored = conn.execute(
            "UPDATE entity_merge_events SET state='merged', unmerged_at=NULL,"
            " unmerged_by_job_id=NULL WHERE unmerged_by_job_id=?", (job_id,)).rowcount

        # ---- 3. 工作层: 重开 / 关闭 inbox ----
        inbox_reopened = 0
        for iid in closed_ids:
            row = conn.execute(
                "SELECT project_id, kind, subject_kind, subject_id, fingerprint"
                " FROM inbox_items WHERE id=?", (iid,)).fetchone()
            conflict = conn.execute(
                "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
                " AND subject_id=? AND fingerprint=? AND state='open'",
                (int(row["project_id"]), str(row["kind"]), str(row["subject_kind"]),
                 int(row["subject_id"]), str(row["fingerprint"]))).fetchone()
            if conflict is not None:
                conn.execute(
                    "UPDATE inbox_items SET occurrence_count=occurrence_count+1,"
                    " last_seen_at=? WHERE id=?", (now(), int(conflict["id"])))
                conn.execute(
                    "UPDATE inbox_items SET closed_by_job_id=NULL WHERE id=?", (iid,))
                continue
            conn.execute(
                "UPDATE inbox_items SET state='open', resolved_at=NULL, resolved_by=NULL,"
                " close_reason=NULL, closed_by_job_id=NULL, last_seen_at=? WHERE id=?",
                (now(), iid))
            inbox_reopened += 1
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='redo',"
            " opened_by_undo_job_id=NULL WHERE opened_by_undo_job_id=?",
            (now(), job_id)).rowcount

        # ---- 4. 缓存重算 + 应用状态 + integrity ----
        recompute(conn, delta=_recompute_subjects_of_job(conn, job_id))
        # 部分成功时保持 applied=0（还有撤销边未恢复），保证
        # "边存在 ⇒ 该 job 未完全应用" 这条不变式成立；人工处置后再 redo。
        conn.execute("UPDATE jobs SET applied=? WHERE id=?",
                     (0 if blocked else 1, job_id))
        assert_integrity(conn)

        report = {
            "op": "redo",
            "job_id": job_id,
            "dry_run": dry_run,
            "noop": False,
            "applied": 0 if blocked else 1,
            "impact": {
                "supersessions_lifted": len(retract_ids),
                "alias_restored": alias_restored,
                "merge_members_restored": members_restored,
                "merge_events_restored": merge_events_restored,
                "inbox_reopened": inbox_reopened,
                "inbox_closed": inbox_closed,
                "blocked": blocked,
            },
            "integrity": {"ok": True},
        }
        log_activity(conn, actor=actor, action="redo", target_kind="job",
                     target_id=job_id, job_id=job_id, detail=report["impact"])
        return report


# ---------------------------------------------------------------------------
# Inbox（审阅点 16：partial unique index + occurrence_count）
# ---------------------------------------------------------------------------

def _open_inbox(conn: sqlite3.Connection, *, project_id: int, kind: str,
                subject_kind: str, subject_id: int, values: Iterable[str],
                sources: Iterable[Any], job_id: int | None = None,
                payload: dict[str, Any] | None = None,
                severity: str = "medium",
                conflict_field: str | None = None) -> tuple[int, bool]:
    """返回 (item_id, created)。重复冲突只累加 occurrence_count，不新增行。

    v3.2-r2: fingerprint 必须包含 conflict_field（§6.3）——否则同一主体上
    两个**不同字段**的冲突会折叠成同一条 open item，后出现的冲突静默丢失。
    重复插入由 partial unique index 兜底（并发下 SELECT-then-INSERT 会撞索引）。
    """
    fp = fingerprint(kind, subject_kind, subject_id, conflict_field, values, sources)
    row = conn.execute(
        "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
        " AND subject_id=? AND fingerprint=? AND state='open'",
        (project_id, kind, subject_kind, subject_id, fp),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE inbox_items SET occurrence_count = occurrence_count + 1,"
            " last_seen_at=? WHERE id=?", (now(), int(row["id"])),
        )
        return int(row["id"]), False
    try:
        cur = conn.execute(
            "INSERT INTO inbox_items(project_id, kind, subject_kind, subject_id, fingerprint,"
            " severity, state, payload_json, occurrence_count, created_by_job_id,"
            " auto_generated, created_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,'open',?,1,?,1,?,?)",
            (project_id, kind, subject_kind, subject_id, fp, severity,
             None if payload is None else canonical_json(payload), job_id, now(), now()),
        )
    except sqlite3.IntegrityError:
        # 并发下另一个 writer 已插入同一 fingerprint → 退化为累加
        row = conn.execute(
            "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
            " AND subject_id=? AND fingerprint=? AND state='open'",
            (project_id, kind, subject_kind, subject_id, fp),
        ).fetchone()
        if row is None:
            raise
        conn.execute(
            "UPDATE inbox_items SET occurrence_count = occurrence_count + 1,"
            " last_seen_at=? WHERE id=?", (now(), int(row["id"])),
        )
        return int(row["id"]), False
    item_id = int(cur.lastrowid)
    log_activity(conn, actor="store", action="inbox.open", target_kind="inbox_item",
                 target_id=item_id, job_id=job_id,
                 detail={"kind": kind, "field": conflict_field})
    return item_id, True


def refresh_observation_conflicts(conn: sqlite3.Connection, project_id: int) -> int:
    """把"同 entity 同字段多值"登记为 inbox 冲突（主体是 entity，见 §9 视图注释）。"""
    opened = 0
    for row in conn.execute(
        "SELECT subject_kind, subject_id, field_namespace, semantic_field, values_csv"
        "  FROM v_field_conflicts WHERE project_id=?", (project_id,)
    ):
        values = str(row["values_csv"]).split(",")
        sources = [r["source_id"] for r in conn.execute(
            "SELECT DISTINCT source_id FROM v_active_contributions"
            " WHERE subject_kind=? AND subject_id=? AND semantic_field=?",
            (str(row["subject_kind"]), int(row["subject_id"]), str(row["semantic_field"])),
        )]
        _, created = _open_inbox(
            conn, project_id=project_id, kind="value_conflict",
            subject_kind=str(row["subject_kind"]), subject_id=int(row["subject_id"]),
            values=values, sources=sources,
            conflict_field=str(row["semantic_field"]),
            payload={"field": str(row["semantic_field"]), "values": values},
        )
        opened += 1 if created else 0
    return opened


# ---------------------------------------------------------------------------
# rekey（T3）—— 审阅点 11：身份迁移事务，零外键迁移
# ---------------------------------------------------------------------------

def _identity_job(conn: sqlite3.Connection, project_id: int, kind: str,
                  job_id: int | None, actor: str) -> int:
    """身份类操作必须有一个 job 作为生命周期边的锚点（v3.2-r4）。

    `revoked_by_job_id` / `unmerged_by_job_id` 都是外键，且 undo/redo 靠它们定位
    要撤销的东西。若调用方不传 job_id，这里自动开一个（与 compact 的处理一致）。
    """
    if job_id is not None:
        return int(job_id)
    return start_job(conn, project_id=project_id, kind=kind, coverage="unknown",
                     actor=actor)


def rekey(conn: sqlite3.Connection, *, project_id: int,
          mapping: dict[str, str], job_id: int | None = None,
          actor: str = "cli", dry_run: bool = False,
          backup_dir: str | Path | None = None) -> dict[str, Any]:
    """把 entity_key 升级为更规范的 key（如 /report/2024 -> /report/{id}）。

    关键: `entities.entity_key` 永不被 UPDATE；所有引用都指向 entities.id（稳定），
    因此 rekey **不需要迁移任何外键**，不可能出现"改了一半、引用断链"的窗口。

    v3.2-r4（债 #4）: 补上 §4.5 第 1 步的 **SQLite Online Backup 快照**。
    快照在事务**之前**做（事务内备份会撞锁），dry-run 也做 —— 因为 dry-run 与
    真实执行走同一代码路径，快照那一步的失败（磁盘满 / 目录不可写）必须在预演时
    就暴露，而不是等到真跑。
    """
    snapshot = store.online_backup(conn, dest_dir=backup_dir, tag="rekey")
    with transaction(conn, dry_run=dry_run):
        job_id = _identity_job(conn, project_id, "rekey", job_id, actor)
        pairs: list[dict[str, Any]] = []
        merges: list[dict[str, Any]] = []
        for old_key, new_key in sorted(mapping.items()):
            entity_id = entity_for_key(conn, project_id, old_key)
            existing = conn.execute(
                "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL",
                (project_id, new_key),
            ).fetchone()
            if existing is not None and int(existing["entity_id"]) != entity_id:
                merges.append({"survivor_key": new_key,
                               "survivor_id": int(existing["entity_id"]),
                               "member_key": old_key, "member_id": entity_id})
                continue
            if existing is None:
                conn.execute(
                    "INSERT INTO entity_alias(project_id, alias_key, entity_id, reason,"
                    " created_at, created_by_job_id) VALUES (?,?,?,'rekey',?,?)",
                    (project_id, new_key, entity_id, now(), job_id),
                )
            pairs.append({"old": old_key, "new": new_key, "entity_id": entity_id})

        for m in merges:
            _merge_inside(conn, project_id=project_id, survivor_id=m["survivor_id"],
                          member_ids=[m["member_id"]], job_id=job_id,
                          reason="rekey_collision", actor=actor)

        recompute(conn)
        assert_integrity(conn)
        report = {"op": "rekey", "dry_run": dry_run, "pairs": pairs, "merges": merges,
                  "identity_rewritten": len(pairs), "backup": str(snapshot)}
        log_activity(conn, actor=actor, action="rekey", target_kind="project",
                     target_id=project_id, job_id=job_id, detail=report)
        return report


# ---------------------------------------------------------------------------
# merge / unmerge（T4 / T5）—— 审阅点 12：identity overlay，不移动任何事实
# ---------------------------------------------------------------------------

def _merge_inside(conn: sqlite3.Connection, *, project_id: int, survivor_id: int,
                  member_ids: Sequence[int], job_id: int | None, reason: str,
                  actor: str) -> int:
    all_ids = sorted({survivor_id, *member_ids})
    cur = conn.execute(
        "INSERT INTO entity_merge_events(project_id, survivor_id, state, reason,"
        " created_at, created_by_job_id) VALUES (?,?,'merged',?,?,?)",
        (project_id, survivor_id, reason, now(), job_id),
    )
    event_id = int(cur.lastrowid)
    for eid in all_ids:
        conn.execute(
            "INSERT INTO entity_merge_members(merge_event_id, entity_id, project_id)"
            " VALUES (?,?,?)", (event_id, eid, project_id),
        )
    log_activity(conn, actor=actor, action="merge", target_kind="entity",
                 target_id=survivor_id, job_id=job_id,
                 detail={"members": all_ids, "reason": reason})
    return event_id


def merge(conn: sqlite3.Connection, *, project_id: int, survivor_key: str,
          member_keys: Sequence[str], job_id: int | None = None,
          actor: str = "cli", dry_run: bool = False) -> dict[str, Any]:
    """merge 是 identity overlay:
      事实层零变更（INV-10），只写 merge event + alias，然后 recompute。
    """
    with transaction(conn, dry_run=dry_run):
        job_id = _identity_job(conn, project_id, "merge", job_id, actor)
        survivor_id = entity_for_key(conn, project_id, survivor_key)
        # key → entity 一次性配对（v3.2-r4 修正）。
        # 旧实现在 member_ids 前面**插入** survivor，再用 `zip(member_keys, member_ids)`
        # 取配对 —— 两个列表长度不同、错位一格，member_key 会指到**另一个** member 的
        # entity 上。（今天不可达: 下面的 `existing is None` 对数上来讲永远为假，
        # 见下方注释。）配对必须由同一次解析产生，不能靠两个列表的下标对齐。
        member_pairs = [(k, entity_for_key(conn, project_id, k)) for k in member_keys]
        member_ids = sorted({survivor_id, *(eid for _, eid in member_pairs)})

        before_facts = _fact_layer_digest(conn)
        event_id = _merge_inside(conn, project_id=project_id, survivor_id=survivor_id,
                                member_ids=member_ids, job_id=job_id,
                                reason="manual_merge", actor=actor)
        # 这里对 `existing` 的判断实际上不会命中: 上面 `entity_for_key(k)` 用的正是
        # 同一条"生效 alias"查询，能解析出 entity 就说明 alias 已存在。保留它是为了让
        # "alias 已经指向别处" 这种情况显式地不覆盖（而不是留一个静默的写入）。
        for k, eid in member_pairs:
            if eid == survivor_id:
                continue
            existing = conn.execute(
                "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL",
                (project_id, k)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO entity_alias(project_id, alias_key, entity_id, reason,"
                    " created_at, created_by_job_id)"
                    " VALUES (?,?,?,'merge_member',?,?)",
                    (project_id, k, eid, now(), job_id))

        recompute(conn)
        conflicts = _merge_conflicts(conn, project_id, all_ids=sorted(set(member_ids)),
                                     job_id=job_id, event_id=event_id)
        after_facts = _fact_layer_digest(conn)
        if before_facts != after_facts:   # pragma: no cover - INV-10 兜底
            raise InvariantError("INV-10 violated: merge touched the fact layer")
        assert_integrity(conn)
        report = {"op": "merge", "dry_run": dry_run, "event_id": event_id,
                  "survivor": f"entity:{survivor_id}",
                  "members": [f"entity:{i}" for i in sorted(set(member_ids))],
                  "conflicts": conflicts,
                  "facts": {"before": before_facts, "after": after_facts}}
        log_activity(conn, actor=actor, action="merge", target_kind="entity",
                     target_id=survivor_id, job_id=job_id, detail=report)
        return report


def unmerge(conn: sqlite3.Connection, *, event_id: int, actor: str = "cli",
            dry_run: bool = False) -> dict[str, Any]:
    """unmerge 天然精确可逆 —— 因为事实从未移动过。

    v3.2-r4 说明: unmerge 是 **merge 的显式逆操作**，本身**不**进入 undo/redo 的
    生命周期边模型（它的逆操作就是再 merge 一次）。因此它不开 job、不写
    `unmerged_by_job_id` —— 所有 `*_by_job_id` 生命周期边都严格只由 **undo** 产生，
    于是一定满足"边存在 ⇒ 该 job 的 applied=0"。

    修复: 旧实现用 `payload_json LIKE '%"event_id":N%'` 关闭 merge_conflict 项，
    但当时 `_merge_conflicts` 的 payload 里**没有** event_id（死代码）。
    现在 payload 真的带 event_id，这个关闭动作才真正生效。
    """
    with transaction(conn, dry_run=dry_run):
        ev = conn.execute("SELECT * FROM entity_merge_events WHERE id=?", (event_id,)).fetchone()
        if ev is None:
            raise InvariantError(f"merge event {event_id} not found")
        members = [int(r["entity_id"]) for r in conn.execute(
            "SELECT entity_id FROM entity_merge_members WHERE merge_event_id=?", (event_id,))]
        before_facts = _fact_layer_digest(conn)
        conn.execute("UPDATE entity_merge_events SET state='unmerged', unmerged_at=?"
                     " WHERE id=?", (now(), event_id))
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='unmerge'"
            " WHERE kind='merge_conflict' AND state='open' AND payload_json LIKE ?",
            (now(), f'%"event_id":{event_id}%'),
        ).rowcount
        recompute(conn)
        after_facts = _fact_layer_digest(conn)
        if before_facts != after_facts:   # pragma: no cover
            raise InvariantError("INV-10 violated: unmerge touched the fact layer")
        assert_integrity(conn)
        report = {"op": "unmerge", "dry_run": dry_run, "event_id": event_id,
                  "inbox_closed": inbox_closed,
                  "members": [f"entity:{m}" for m in members]}
        log_activity(conn, actor=actor, action="unmerge", target_kind="entity",
                     target_id=int(ev["survivor_id"]), detail=report)
        return report


def _merge_conflicts(conn: sqlite3.Connection, project_id: int,
                     all_ids: Sequence[int], job_id: int | None,
                     event_id: int | None = None) -> list[dict[str, Any]]:
    """合并后同字段多值 → 冲突 → inbox。**禁止 last-write-wins 自动选值。**

    payload 必须带 `event_id`（v3.2-r4）：否则 `unmerge` 无法定位并关闭这些项
    （旧代码就是靠 LIKE 匹配一个从不存在的字段 —— 死代码）。
    """
    out: list[dict[str, Any]] = []
    marks = ",".join("?" for _ in all_ids)
    rows = conn.execute(
        f"SELECT semantic_field, GROUP_CONCAT(DISTINCT value) AS vals,"
        f" COUNT(DISTINCT value) AS n"
        f"  FROM observations WHERE entity_id IN ({marks}) AND is_current=1"
        f" GROUP BY semantic_field HAVING COUNT(DISTINCT value) > 1",
        list(all_ids),
    ).fetchall()
    for row in rows:
        values = sorted(set(str(row["vals"]).split(",")))
        sources = [r["source_id"] for r in conn.execute(
            f"SELECT DISTINCT c.source_id FROM v_active_contributions c"
            f" JOIN observations o ON o.id = c.subject_id"
            f" WHERE c.subject_kind='observation' AND o.entity_id IN ({marks})"
            f"   AND o.semantic_field=?", [*all_ids, str(row["semantic_field"])])]
        item_id, _ = _open_inbox(
            conn, project_id=project_id, kind="merge_conflict",
            subject_kind="entity", subject_id=int(all_ids[0]),
            values=values, sources=sources, job_id=job_id,
            payload={"field": str(row["semantic_field"]), "values": values,
                     "event_id": event_id,
                     "members": [f"entity:{i}" for i in all_ids]},
            severity="high",
        )
        out.append({"field": str(row["semantic_field"]), "values": values,
                    "inbox_item": item_id})
    return out


def _fact_layer_digest(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("raw_files", "evidence", "contributions",
                      "contribution_supersessions")}


# ---------------------------------------------------------------------------
# compact（T6）—— 审阅点 9：job_count 语义不再跳变
# ---------------------------------------------------------------------------

def compact(conn: sqlite3.Connection, *, project_id: int, job_id: int | None = None,
            older_than: str = "1970-01-01T00:00:00Z", actor: str = "cli",
            dry_run: bool = False) -> dict[str, Any]:
    """压实非活跃历史（新行 append + 旧行 suppress，仍不 UPDATE/DELETE）。

    守恒（INV-11）:
        Σ logical_job_count 不变
        Σ source_count      不变
        is_current 不变
        confidence 只可不变或降低

    v3.2-r2 时间语义（区间）:
        压实行的 `observed_at` = **MAX(成员)**（区间上界/规范时刻**不许回退**），
        `observed_at_min`       = MIN(成员.observed_at_min)。
        旧实现取 MIN(observed_at) 会把时间线往回拉，等于让压实改坏了历史。

    v3.2-r2 编组键补 `contribution_kind`（不同语义的贡献永不合并）。
    """
    with transaction(conn, dry_run=dry_run):
        groups = conn.execute(
            """
            SELECT c.project_id, c.subject_kind, c.subject_id, c.owner_entity_id,
                   c.contribution_kind, c.field_namespace,
                   c.semantic_field, c.field_value, c.value_kind, c.relation_kind,
                   c.source_id, c.authorship, COUNT(*) AS n,
                   MAX(c.observed_at)     AS max_at,
                   MIN(c.observed_at_min) AS min_at,
                   SUM(c.job_count + c.compacted_job_count) AS logical_jobs,
                   MIN(c.evidence_id) AS any_evidence,
                   GROUP_CONCAT(c.id) AS ids
              FROM contributions c
             WHERE c.project_id = ?
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id = c.id)
               AND c.authorship <> 'manual'
               AND c.observed_at < ?
             GROUP BY c.project_id, c.subject_kind, c.subject_id, c.owner_entity_id,
                      c.contribution_kind, c.field_namespace,
                      c.semantic_field, c.field_value, c.value_kind, c.relation_kind,
                      c.source_id, c.authorship
            HAVING COUNT(*) > 1
            """,
            (project_id, older_than),
        ).fetchall()

        before = _conservation(conn, project_id)
        conf_before = _observation_confidence_map(conn, project_id)
        effective_job = job_id if job_id is not None else _compact_job(
            conn, project_id, actor)

        # v3.2-r3: 每个 (subject, source, field) 的**最后一条**活贡献不参与压实。
        # 业务语义: "最近一次观测"必须单独存活，不能被合成行代表 ——
        # 否则 earliest/latest 的区分、以及"最近是否还看到过"的判断都会失去依据。
        protected = _protected_last_active(conn, project_id, older_than)

        groups_done: list[dict[str, Any]] = []
        for g in groups:
            ids = [int(x) for x in str(g["ids"]).split(",") if int(x) not in protected]
            if len(ids) < 2:
                continue
            marks = ",".join("?" for _ in ids)
            # 只统计**真正被压实**的成员（保护行不参与，否则会重复计数）
            agg = conn.execute(
                f"SELECT MAX(observed_at) AS max_at, MIN(observed_at_min) AS min_at,"
                f" SUM(job_count + compacted_job_count) AS logical_jobs"
                f"  FROM contributions WHERE id IN ({marks})", ids).fetchone()
            # 保守证据选择: 取 quality 最高的那条
            best = conn.execute(
                f"SELECT c.evidence_id FROM contributions c JOIN evidence e ON e.id=c.evidence_id"
                f" WHERE c.id IN ({marks})"
                f" ORDER BY CASE e.quality_class WHEN 'high' THEN 3"
                f" WHEN 'medium' THEN 2 ELSE 1 END DESC, c.id ASC LIMIT 1",
                ids,
            ).fetchone()
            evidence_id = int(best["evidence_id"])
            representative = ids[0]
            rep = conn.execute("SELECT * FROM contributions WHERE id=?",
                               (representative,)).fetchone()

            members = [
                {"contribution_id": int(r["id"]), "job_id": int(r["job_id"]),
                 "source_id": int(r["source_id"]), "observed_at": str(r["observed_at"]),
                 "observed_at_min": str(r["observed_at_min"]),
                 "evidence_id": int(r["evidence_id"])}
                for r in conn.execute(
                    f"SELECT * FROM contributions WHERE id IN ({marks})", ids)
            ]
            # 1) 抑制被合并的旧贡献（append 抑制事实，不 DELETE）
            for cid in ids:
                conn.execute(
                    "INSERT INTO contribution_supersessions(project_id, contribution_id,"
                    " superseded_by_job_id, reason, created_at, actor)"
                    " VALUES (?,?,?, 'compact', ?, ?)",
                    (project_id, cid, effective_job, now(), actor),
                )
            # 2) 追加一条 compaction 代表贡献（区间语义: 上界不变，下界细化）
            #    v3.2-r4（债 #3）: 这里原来是裸 SQL 直写 contributions，是全仓唯一
            #    绕过 add_contribution 的写点。合成行也是贡献，必须走同一入口 ——
            #    否则 INV-16 的结构校验对它形同虚设，schema 加了新列它也不跟。
            #    入口本身零行为差异: job_count/compacted_job_count/synthetic/
            #    represents_json 就是为压实产物加的参数。
            add_contribution(conn, NewContribution(
                project_id=project_id,
                job_id=effective_job,
                source_id=int(rep["source_id"]),
                evidence_id=evidence_id,
                subject_kind=str(rep["subject_kind"]),
                subject_id=int(rep["subject_id"]),
                contribution_kind=str(rep["contribution_kind"]),
                owner_entity_id=(None if rep["owner_entity_id"] is None
                                 else int(rep["owner_entity_id"])),
                authorship=str(rep["authorship"]),
                field_namespace=rep["field_namespace"],
                semantic_field=rep["semantic_field"],
                field_value=rep["field_value"],
                value_kind=rep["value_kind"],
                relation_kind=rep["relation_kind"],
                observed_at=str(agg["max_at"]),
                observed_at_min=str(agg["min_at"]),
                observed_at_source=str(rep["observed_at_source"]),
                observed_at_accuracy="approximate",
                observed_at_precision="unknown",
                job_count=1,
                compacted_job_count=int(agg["logical_jobs"]) - 1,
                synthetic=1,
                represents_json=canonical_json({"members": members}),
            ))
            groups_done.append({
                "subject": f"{g['subject_kind']}:{int(g['subject_id'])}",
                "contribution_kind": str(g["contribution_kind"]),
                "field": g["semantic_field"], "members": len(ids),
                "observed_at": str(agg["max_at"]), "observed_at_min": str(agg["min_at"]),
                "compacted_job_count": int(agg["logical_jobs"]) - 1,
            })

        subjects = sorted({(str(g["subject_kind"]), int(g["subject_id"])) for g in groups})
        recompute(conn, delta=subjects)
        after = _conservation(conn, project_id)
        conf_after = _observation_confidence_map(conn, project_id)
        _assert_conservation(before, after, conf_before=conf_before, conf_after=conf_after)
        assert_integrity(conn)
        report = {"op": "compact", "dry_run": dry_run, "groups": len(groups_done),
                  "protected_last_active": sorted(protected),
                  "detail": groups_done,
                  "conservation": {"before": before, "after": after}}
        log_activity(conn, actor=actor, action="compact", target_kind="project",
                     target_id=project_id, job_id=job_id, detail=report)
        return report


def _protected_last_active(conn: sqlite3.Connection, project_id: int,
                           older_than: str) -> set[int]:
    """每个 (subject, source, field) 的**最后一条**可压实贡献 → 不参与压实。

    排序口径: (observed_at, id) 最大者。这正是"最近一次观测"的业务语义。
    """
    rows = conn.execute(
        """
        SELECT c.id, c.observed_at, c.subject_kind, c.subject_id, c.owner_entity_id,
               c.field_namespace, c.semantic_field, c.source_id, c.contribution_kind
          FROM contributions c
         WHERE c.project_id = ?
           AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                            WHERE s.contribution_id = c.id)
           AND c.authorship <> 'manual'
           AND c.observed_at < ?
         ORDER BY c.subject_kind, c.subject_id, c.owner_entity_id, c.field_namespace,
                  c.semantic_field, c.source_id, c.contribution_kind,
                  c.observed_at DESC, c.id DESC
        """,
        (project_id, older_than),
    ).fetchall()
    seen: set[tuple] = set()
    protected: set[int] = set()
    for r in rows:
        group = (r["subject_kind"], int(r["subject_id"]), r["owner_entity_id"],
                 r["field_namespace"], r["semantic_field"], int(r["source_id"]),
                 r["contribution_kind"])
        if group in seen:
            continue
        seen.add(group)
        protected.add(int(r["id"]))
    return protected


def _compact_job(conn: sqlite3.Connection, project_id: int, actor: str) -> int:
    return start_job(conn, project_id=project_id, kind="compact", coverage="unknown",
                     actor=actor)


def _conservation(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """INV-11 的守恒量（v3.2-r2: 真正按 §5.2 的 Σ 口径，不是 project 去重）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(c.job_count + c.compacted_job_count), 0) AS logical_jobs,
               (SELECT COUNT(DISTINCT source_id) FROM v_active_contributions
                 WHERE project_id = ?)                                AS sources,
               (SELECT COALESCE(SUM(source_count), 0) FROM observations
                 WHERE project_id = ?)                                AS sum_source_count,
               (SELECT COALESCE(SUM(logical_job_count), 0) FROM observations
                 WHERE project_id = ?)                                AS sum_logical_job_count,
               (SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1) AS obs_current
          FROM v_active_contributions c WHERE c.project_id = ?
        """,
        (project_id, project_id, project_id, project_id, project_id),
    ).fetchone()
    return {"logical_jobs": int(row["logical_jobs"]), "sources": int(row["sources"]),
            "sum_source_count": int(row["sum_source_count"]),
            "sum_logical_job_count": int(row["sum_logical_job_count"]),
            "obs_current": int(row["obs_current"])}


CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "conflict": 4,
                    "manual": 5}


def _observation_confidence_map(conn: sqlite3.Connection,
                                project_id: int) -> dict[int, str]:
    return {int(r["id"]): str(r["confidence"])
            for r in conn.execute("SELECT id, confidence FROM observations"
                                  " WHERE project_id=?", (project_id,))}


def _assert_conservation(before: dict[str, int], after: dict[str, int], *,
                         conf_before: dict[int, str] | None = None,
                         conf_after: dict[int, str] | None = None) -> None:
    if before["logical_jobs"] != after["logical_jobs"]:
        raise InvariantError(
            f"INV-11 violated: logical_job_count not conserved "
            f"{before['logical_jobs']} -> {after['logical_jobs']}")
    if before["sources"] != after["sources"]:
        raise InvariantError("INV-11 violated: source_count not conserved")
    if before["sum_source_count"] != after["sum_source_count"]:
        raise InvariantError(
            f"INV-11 violated: Σ source_count not conserved "
            f"{before['sum_source_count']} -> {after['sum_source_count']}")
    if before["sum_logical_job_count"] != after["sum_logical_job_count"]:
        raise InvariantError(
            f"INV-11 violated: Σ logical_job_count not conserved "
            f"{before['sum_logical_job_count']} -> {after['sum_logical_job_count']}")
    if before["obs_current"] != after["obs_current"]:
        raise InvariantError("INV-11 violated: is_current changed by compact")
    if conf_before and conf_after:
        for oid, before_conf in conf_before.items():
            after_conf = conf_after.get(oid)
            if after_conf is None:
                continue
            if (CONFIDENCE_ORDER.get(after_conf, 99)
                    > CONFIDENCE_ORDER.get(before_conf, 99)):
                raise InvariantError(
                    f"INV-11 violated: compact raised confidence of observation "
                    f"{oid}: {before_conf} -> {after_conf}")


# ---------------------------------------------------------------------------
# evidence 降级（审阅点 10 + v3.2-r3 闭环）
#
#   三级 = **这一行还留下什么**（§5.3）:
#     full      片段内容 + locator + hash
#     locator   无片段内容；locator + hash
#     hash_only 只剩 hash（locator 已丢弃；DB 层 CHECK 强制 locator IS NULL）
#
#   能力 = 两级因素交叉:
#     原文是否仍可取得   ⟸ raw_files.storage_state ∈ {present, archived}
#     片段是否仍可定位   ⟸ evidence.locator IS NOT NULL（即 recovery_state ∈ {full, locator}）
# ---------------------------------------------------------------------------

RECOVERY_LEVELS = ("full", "locator", "hash_only")
STORAGE_STATES = ("present", "archived", "purged", "missing")

RECOVERY_CAPABILITY = {
    ("present", "full"): "recoverable",
    ("present", "locator"): "recoverable",
    ("present", "hash_only"): "verifiable_only",
    ("archived", "full"): "recoverable",
    ("archived", "locator"): "recoverable",
    ("archived", "hash_only"): "verifiable_only",
    ("purged", "full"): "verifiable_only",
    ("purged", "locator"): "verifiable_only",
    ("purged", "hash_only"): "verifiable_only",
    ("missing", "full"): "hash_only",
    ("missing", "locator"): "hash_only",
    ("missing", "hash_only"): "hash_only",
}


def evidence_capability(raw_storage_state: str, recovery_state: str) -> str:
    """能力判定（3×4 = 12 种组合；未知组合按最保守处理）。

        raw_file present/archived + evidence full/locator → recoverable
        raw_file present/archived + evidence hash_only    → verifiable_only
            （原文还在，但 evidence 侧已丢弃 locator → 定位不到片段）
        raw_file purged                                    → verifiable_only
        raw_file missing                                   → hash_only
    """
    return RECOVERY_CAPABILITY.get((raw_storage_state, recovery_state), "hash_only")


def evidence_capability_report(raw_storage_state: str, recovery_state: str) -> dict[str, Any]:
    """能力 + 两个决定因素（GUI 文案直接可用，避免再次误述）。"""
    capability = evidence_capability(raw_storage_state, recovery_state)
    wording = {
        "recoverable": "可校验完整性，并可凭 locator + hash 恢复原文片段",
        "verifiable_only": "仅可校验完整性（hash 比对已有副本），不可恢复原文片段",
        "hash_only": "仅保留 hash：无原文、无归档，不可恢复也不可重定位",
    }[capability]
    return {
        "capability": capability,
        "original_available": raw_storage_state in ("present", "archived"),
        "locatable": recovery_state in ("full", "locator"),
        "fragment_recoverable": capability == "recoverable",
        "verifiable": capability in ("recoverable", "verifiable_only"),
        "wording": wording,
    }


def _downgrade_evidence_in_txn(conn: sqlite3.Connection, *, raw_file_id: int,
                               to_state: str, actor: str, job_id: int | None,
                               dry_run: bool) -> dict[str, Any]:
    """事务内实现（调用方负责事务；本函数不 BEGIN，避免嵌套事务崩溃）。"""
    if to_state not in RECOVERY_LEVELS:
        raise InvariantError(f"bad recovery_state {to_state!r}")
    order = {"full": 2, "locator": 1, "hash_only": 0}
    rows = conn.execute(
        "SELECT id, recovery_state, locator FROM evidence WHERE raw_file_id=?",
        (raw_file_id,),
    ).fetchall()
    changed: list[dict[str, Any]] = []
    for r in rows:
        current = str(r["recovery_state"])
        if order[to_state] > order[current]:
            raise InvariantError(
                f"INV-17: evidence {int(r['id'])} recovery_state 不允许升级 "
                f"({current} -> {to_state})")
        if current == to_state:
            continue
        dropped_locator = r["locator"] if to_state == "hash_only" else None
        conn.execute(
            "UPDATE evidence SET recovery_state=?, locator=? WHERE id=?",
            (to_state, None if to_state == "hash_only" else r["locator"], int(r["id"])),
        )
        changed.append({"evidence_id": int(r["id"]), "from": current, "to": to_state,
                        "dropped_locator": dropped_locator})
    rf = conn.execute("SELECT storage_state FROM raw_files WHERE id=?",
                      (raw_file_id,)).fetchone()
    storage_state = str(rf["storage_state"]) if rf else "missing"
    report = {"op": "evidence.degrade", "dry_run": dry_run, "raw_file_id": raw_file_id,
              "recovery_state": to_state, **evidence_capability_report(storage_state, to_state),
              "changed": changed}
    if changed:
        log_activity(conn, actor=actor, action="evidence.degrade", target_kind="raw_file",
                     target_id=raw_file_id, job_id=job_id, detail=report)
    return report


def degrade_evidence(conn: sqlite3.Connection, *, raw_file_id: int,
                     to_state: str, actor: str = "compact",
                     job_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """把某个 raw_file 下的 evidence 降级（只降不升）。"""
    with transaction(conn, dry_run=dry_run):
        return _downgrade_evidence_in_txn(conn, raw_file_id=raw_file_id, to_state=to_state,
                                          actor=actor, job_id=job_id, dry_run=dry_run)


def purge_raw_file(conn: sqlite3.Connection, *, raw_file_id: int, actor: str = "gc",
                   job_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """原文清除（事实层 storage_state 降级 → evidence 随之降为 hash_only）。

    v3.2-r3 修正: 不再嵌套 `transaction()`（旧实现必然抛
    "cannot start a transaction within a transaction"），且 dry_run 正确下传。
    """
    with transaction(conn, dry_run=dry_run):
        conn.execute("UPDATE raw_files SET storage_state='purged', purged_at=? WHERE id=?",
                     (now(), raw_file_id))
        report = _downgrade_evidence_in_txn(conn, raw_file_id=raw_file_id,
                                            to_state="hash_only", actor=actor,
                                            job_id=job_id, dry_run=dry_run)
        report["op"] = "raw_file.purge"
        report["note"] = ("raw_file 已 purged: evidence 仅具备完整性校验能力，"
                          "不再具备原文恢复能力")
        return report


# ---------------------------------------------------------------------------
# coverage 判定（审阅点 7）
# ---------------------------------------------------------------------------

def candidate_inactive(conn: sqlite3.Connection, *, observation_id: int,
                       job_id: int) -> tuple[bool, str]:
    """coverage 判定（v3.2-r2 修正）。

    判定顺序（先给出最可读的原因）:
      1. coverage='full'              否则 unknown
      2. 该 job 对被观察对象没有活贡献  否则 unknown（本 job 不能否定自己）
      3. scope_version / scope_hash 与项目当前一致
      4. **parser fingerprint 与项目当前一致**（`projects.parser_fingerprint`）
         —— parser 决定"覆盖"的含义；项目未登记当前 parser → unknown
      5. 观察对象与其归属 entity 都不是 manual_owned

    任何一项不满足 → unknown（**不**判 inactive）。
    旧实现的 bug: 把 job.parser_fingerprint 与 projects.scope_hash 比较（永远 drift）。
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
    if job is None or obs is None:
        return False, "missing"
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (int(obs["project_id"]),)).fetchone()
    if str(job["coverage"]) != "full":
        return False, f"coverage={job['coverage']}"
    active_from_job = conn.execute(
        "SELECT COUNT(*) FROM v_active_contributions"
        " WHERE subject_kind='observation' AND subject_id=? AND job_id=?",
        (observation_id, job_id),
    ).fetchone()[0]
    if int(active_from_job) != 0:
        return False, "job_has_active_contribution"
    if int(job["project_id"]) != int(obs["project_id"]):
        return False, "job_project_mismatch"
    if int(job["scope_version"]) != int(proj["scope_version"]):
        return False, "scope_version_drift"
    if str(job["scope_hash"]) != str(proj["scope_hash"]):
        return False, "scope_hash_drift"
    if job["parser_fingerprint"] is None:
        return False, "parser_unknown"
    if proj["parser_fingerprint"] is None:
        return False, "project_parser_unset"
    if str(job["parser_fingerprint"]) != str(proj["parser_fingerprint"]):
        return False, "parser_drift"
    # 人工对象永不被 coverage 否定（§5.1: ¬manual_owned(o) ∧ ¬manual_owned(o.entity)）
    from .store import manual_owned as _manual_owned
    if _manual_owned(conn, "observation", observation_id):
        return False, "manual_owned"
    if _manual_owned(conn, "entity", int(obs["entity_id"])):
        return False, "manual_owned_entity"
    return True, "candidate_inactive"


def explain_job_scope(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    """旧 job 必须用**当时的** scope 解释，不能用当前 scope。"""
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")
    hist = conn.execute(
        "SELECT * FROM project_scope_history WHERE project_id=? AND version=?",
        (int(job["project_id"]), int(job["scope_version"])),
    ).fetchone()
    proj = conn.execute("SELECT * FROM projects WHERE id=?",
                        (int(job["project_id"]),)).fetchone()
    drift = int(proj["scope_version"]) - int(job["scope_version"])
    parser_drift = (job["parser_fingerprint"] is not None
                    and str(job["parser_fingerprint"]) != str(proj["parser_fingerprint"]))
    return {
        "job_id": job_id,
        "job_scope_version": int(job["scope_version"]),
        "job_scope_hash": str(job["scope_hash"]),
        "job_parser_fingerprint": job["parser_fingerprint"],
        "current_scope_version": int(proj["scope_version"]),
        "current_parser_fingerprint": proj["parser_fingerprint"],
        "scope_versions_behind": drift,
        "parser_drift": parser_drift,
        "job_scope": None if hist is None else hist["scope"],
        "interpret_with": "historical_scope" if drift > 0 else "current_scope",
        "coverage_usable_for_inactive": drift == 0 and not parser_drift,
    }
