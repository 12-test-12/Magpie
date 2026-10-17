"""Finding lifecycle writes, using the existing ops contribution writers.

Public callers continue to use ops; this module owns only Finding policy.
"""
from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

from . import normalize, ops, store
from .store import InvariantError, atomic, log_activity, now, sha256_hex

FINDING_STATUSES = ops.FINDING_STATUSES
FINDING_SEVERITIES = ops.FINDING_SEVERITIES
RETEST_STATUSES = ops.RETEST_STATUSES


def _require_finding(conn, project_id, finding_id):
    row = conn.execute(
        "SELECT id FROM entities WHERE id=? AND project_id=? AND kind='vuln'"
        " AND is_current=1 AND merged_into IS NULL", (finding_id, project_id)).fetchone()
    if row is None:
        raise InvariantError(f"漏洞实体 {finding_id} 不在项目 {project_id} 当前有效发现里")


def create_manual_finding(conn: sqlite3.Connection, *, project_id: int,
                          title: str, severity: str, target_entity_id: int,
                          template_id: str | None = None,
                          description: str | None = None,
                          evidence_id: int | None = None,
                          status: str = "confirmed",
                          reason: str | None = None,
                          actor: str = "analyst",
                          dry_run: bool = False,
                          _shared_txn: bool = False) -> dict[str, Any]:
    """人工创建漏洞发现（工作包 F）。

    使用 vuln 实体身份与 found_on 关系，接入既有 contribution / undo 体系。
    """
    store.require_row_factory(conn, caller="ops.create_manual_finding")
    name = (title or "").strip()
    if not name:
        raise InvariantError("漏洞标题不能为空")
    if len(name) > 300:
        raise InvariantError("漏洞标题过长（最多 300 字符）")
    sev = str(severity).lower()
    if sev not in FINDING_SEVERITIES:
        raise InvariantError(f"严重级别只能是 {list(FINDING_SEVERITIES)} 之一，收到 {severity!r}")
    st = str(status).lower()
    if st not in FINDING_STATUSES:
        raise InvariantError(f"处置状态只能是 {list(FINDING_STATUSES)} 之一，收到 {status!r}")
    if st == "duplicate":
        raise InvariantError("请先创建发现，再通过处置研判指定主漏洞")

    with ops.batch_txn(conn, dry_run, _shared_txn):
        row = conn.execute(
            "SELECT kind FROM entities WHERE id=? AND project_id=?"
            " AND is_current=1 AND merged_into IS NULL",
            (target_entity_id, project_id)).fetchone()
        if row is None:
            raise InvariantError(f"目标实体 {target_entity_id} 不在项目 {project_id} 里")
        target_kind = str(row["kind"])
        target_key = store.display_key(conn, target_entity_id)
        if not target_key:
            raise InvariantError(f"目标实体 {target_entity_id} 没有可用的 entity_key")

        if evidence_id is not None:
            ev_row = conn.execute(
                "SELECT id FROM evidence WHERE id=? AND project_id=?",
                (evidence_id, project_id)).fetchone()
            if ev_row is None:
                raise InvariantError(f"证据 {evidence_id} 不在项目 {project_id} 里")

        # A title is display text, not identity. Distinct manual records must
        # never merge because of transliteration, punctuation or title length.
        tmpl = (template_id or f"manual:{uuid4().hex}").strip()
        # vuln target normalization is intentionally coarser than Endpoint v2.
        # Qualify explicit manual templates with the exact target identity.
        if template_id:
            tmpl += ":" + sha256_hex(store.canonical_json([target_kind, target_key]))[:24]
        vuln_target = target_key
        if target_kind == "endpoint":
            if vuln_target.startswith("ep2:"):
                d = normalize.decode_endpoint_key(vuln_target)
                vuln_target = normalize.endpoint_display(d).split(" ", 1)[1]
            elif " " in vuln_target:
                vuln_target = vuln_target.split()[-1]
        key = normalize.vuln_key(tmpl, vuln_target)

        job_id = ops._identity_job(conn, project_id, "manual_finding", None, actor)

        if evidence_id is None:
            content = f"# {name}\n\n{description or ''}\n"
            digest = sha256_hex(content)
            raw_id = ops.add_raw_file(
                conn, project_id=project_id,
                locator_uri=f"manual-finding://{job_id}/{digest[:12]}.md",
                content=content)
            evidence_id = ops.add_evidence(
                conn, project_id=project_id, raw_file_id=raw_id,
                locator={"kind": "manual_finding", "job_id": job_id, "title": name},
                snippet=content[:1000], quality_class="medium", recovery_state="full")

        source_id = store.register_source(conn, name="manual:finding",
                                          authority_level="manual",
                                          tool="manual_finding", actor=actor)

        ops.add_observation(
            conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
            source_id=source_id, entity_key=key, kind="vuln",
            semantic_field="title", value=name, value_kind="string",
            authorship="manual", observed_at_source="manual")
        ops.add_observation(
            conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
            source_id=source_id, entity_key=key, kind="vuln",
            semantic_field="severity", value=sev, value_kind="string",
            authorship="manual", observed_at_source="manual")
        if description:
            ops.add_observation(
                conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
                source_id=source_id, entity_key=key, kind="vuln",
                semantic_field="description", value=description.strip(), value_kind="string",
                authorship="manual", observed_at_source="manual")
        ops.add_observation(
            conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
            source_id=source_id, entity_key=key, kind="vuln",
            semantic_field="state", value="open", value_kind="string",
            authorship="manual", observed_at_source="manual")

        ops.add_relation(
            conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
            source_id=source_id, src_key=key, src_kind="vuln",
            rel="found_on", dst_key=target_key, dst_kind=target_kind,
            authorship="manual", observed_at_source="manual")

        touched = ops._recompute_subjects_of_job(conn, job_id)
        store.recompute(conn, delta=touched)

        finding_id = store.resolve_entity(conn, project_id, key)
        if not dry_run:
            conn.execute(
                "INSERT INTO finding_triage"
                " (project_id, finding_id, status, severity, title, disposition_reason, updated_at, updated_by)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id, finding_id) DO NOTHING",
                (project_id, finding_id, st, sev, name, (reason or "人工录入发现").strip(), now(), actor))

        ops.finish_job(conn, job_id, "done", actor=actor)
        return {
            "op": "manual_finding.create", "dry_run": bool(dry_run),
            "job_id": None if dry_run else job_id,
            "finding_id": finding_id, "finding_key": key,
            "status": st, "severity": sev, "target_id": target_entity_id,
            "title": name, "evidence_id": evidence_id,
        }


def triage_finding(conn: sqlite3.Connection, *, project_id: int, finding_id: int,
                   status: str, severity: str | None = None,
                   reason: str | None = None, duplicate_of_id: int | None = None,
                   actor: str = "analyst") -> dict[str, Any]:
    """更新漏洞发现的人工处置状态与判定（工作包 F）。"""
    store.require_row_factory(conn, caller="ops.triage_finding")
    _require_finding(conn, project_id, finding_id)
    st = str(status).lower()
    if st not in FINDING_STATUSES:
        raise InvariantError(f"处置状态只能是 {list(FINDING_STATUSES)} 之一，收到 {status!r}")
    sev = str(severity).lower() if severity else None
    if sev and sev not in FINDING_SEVERITIES:
        raise InvariantError(f"严重级别只能是 {list(FINDING_SEVERITIES)} 之一，收到 {severity!r}")
    if st == "duplicate" and duplicate_of_id is None:
        raise InvariantError("重复发现必须指定主漏洞")
    if st != "duplicate":
        duplicate_of_id = None
    if duplicate_of_id is not None:
        if duplicate_of_id == finding_id:
            raise InvariantError("漏洞不能标记为自身的重复项")
        _require_finding(conn, project_id, duplicate_of_id)
        seen = {finding_id}
        current = duplicate_of_id
        while current is not None:
            if current in seen:
                raise InvariantError("重复发现不能形成循环引用")
            seen.add(current)
            parent = conn.execute(
                "SELECT duplicate_of_id FROM finding_triage WHERE project_id=?"
                " AND finding_id=? AND status='duplicate'", (project_id, current)).fetchone()
            current = parent[0] if parent else None

    t_now = now()
    with atomic(conn):
        conn.execute(
            "INSERT INTO finding_triage (project_id, finding_id, status, severity, disposition_reason, duplicate_of_id, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, finding_id) DO UPDATE SET"
            "   status=excluded.status,"
            "   severity=COALESCE(excluded.severity, finding_triage.severity),"
            "   disposition_reason=excluded.disposition_reason,"
            "   duplicate_of_id=excluded.duplicate_of_id,"
            "   updated_at=excluded.updated_at,"
            "   updated_by=excluded.updated_by",
            (project_id, finding_id, st, sev, (reason or "").strip(), duplicate_of_id, t_now, actor))
        log_activity(conn, actor=actor, action="finding.triage", target_kind="entity",
                     target_id=finding_id, detail={"status": st, "severity": sev, "reason": reason, "duplicate_of_id": duplicate_of_id})
    return {"finding_id": finding_id, "status": st, "severity": sev,
            "disposition_reason": (reason or "").strip(), "duplicate_of_id": duplicate_of_id,
            "updated_at": t_now, "updated_by": actor}


def add_retest(conn: sqlite3.Connection, *, project_id: int, finding_id: int,
               status: str, notes: str, evidence_id: int | None = None,
               actor: str = "analyst") -> dict[str, Any]:
    """添加人工复测记录并按结论联动状态（工作包 F）。

    注意：网络超时、目标不可达或登录凭据失效不得判定为 resolved。
    """
    store.require_row_factory(conn, caller="ops.add_retest")
    _require_finding(conn, project_id, finding_id)
    st = str(status).lower()
    if st not in RETEST_STATUSES:
        raise InvariantError(f"复测结论只能是 {list(RETEST_STATUSES)} 之一，收到 {status!r}")
    note_text = (notes or "").strip()
    if not note_text:
        raise InvariantError("复测记录说明不能为空")
    if evidence_id is not None:
        ev_row = conn.execute(
            "SELECT id FROM evidence WHERE id=? AND project_id=?",
            (evidence_id, project_id)).fetchone()
        if ev_row is None:
            raise InvariantError(f"复测证据 {evidence_id} 不在项目 {project_id} 里")

    t_now = now()
    with atomic(conn):
        cursor = conn.execute(
            "INSERT INTO finding_retests (project_id, finding_id, status, notes, evidence_id, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, finding_id, st, note_text, evidence_id, actor, t_now))
        retest_id = cursor.lastrowid
        new_triage_status = "resolved" if st == "resolved" else "confirmed" if st == "still_vulnerable" else None
        if new_triage_status:
            conn.execute(
                "INSERT INTO finding_triage (project_id, finding_id, status, disposition_reason, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(project_id, finding_id) DO UPDATE SET"
                "   status=excluded.status,"
                "   duplicate_of_id=NULL,"
                "   disposition_reason=excluded.disposition_reason,"
                "   updated_at=excluded.updated_at,"
                "   updated_by=excluded.updated_by",
                (project_id, finding_id, new_triage_status,
                 f"人工复测联动（结论：{st}）: {note_text[:100]}", t_now, actor))
        log_activity(conn, actor=actor, action="finding.retest", target_kind="entity",
                     target_id=finding_id, detail={"retest_id": retest_id, "status": st, "notes": note_text, "evidence_id": evidence_id})
    return {"retest_id": retest_id, "finding_id": finding_id, "status": st,
            "notes": note_text, "evidence_id": evidence_id, "created_at": t_now, "actor": actor}


