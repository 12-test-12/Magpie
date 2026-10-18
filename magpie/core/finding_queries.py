"""Finding read models. No writes or parallel fact store."""
from __future__ import annotations

from urllib.parse import urlsplit

from . import normalize, query


def public_value(value):
    """Redact copies returned to the UI/report; preserve stored evidence."""
    if isinstance(value, str):
        return normalize.redact_secrets(value)
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def finding_detail(conn, project_id, finding_id, *, retest_limit=50):
    row = conn.execute(
        "SELECT e.id, e.kind, k.entity_key, e.lifecycle, e.first_seen, e.last_seen"
        " FROM entities e JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1"
        " WHERE e.project_id=? AND e.id=? AND e.is_current=1 AND e.merged_into IS NULL",
        (project_id, finding_id)).fetchone()
    if row is None or row["kind"] != "vuln":
        raise query.QueryError("E_ENTITY_NOT_FOUND", "当前项目中未找到该漏洞发现")

    res = dict(row)
    key = row["entity_key"]
    res["template_id"] = key.split("@", 1)[0] if "@" in key else key
    res["target"] = key.split("@", 1)[1] if "@" in key else ""
    target_key = res["target"]
    try:
        res["host"] = urlsplit(target_key if "://" in target_key else "//" + target_key).hostname or ""
    except ValueError:
        res["host"] = ""

    triage = conn.execute(
        "SELECT status, severity, title, disposition_reason, duplicate_of_id, updated_at, updated_by, version"
        " FROM finding_triage WHERE project_id=? AND finding_id=?",
        (project_id, finding_id)).fetchone()
    obs_sev = (conn.execute(
        "SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='severity' ORDER BY id DESC LIMIT 1",
        (project_id, finding_id)).fetchone() or ["unknown"])[0]
    obs_title = (conn.execute(
        "SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='title' ORDER BY id DESC LIMIT 1",
        (project_id, finding_id)).fetchone() or [key])[0]

    res["triage"] = {
        "version": triage["version"] if triage else 0,
        "status": triage["status"] if triage else "candidate",
        "severity": (triage["severity"] if triage and triage["severity"] else obs_sev).lower(),
        "title": triage["title"] if triage and triage["title"] else obs_title,
        "disposition_reason": triage["disposition_reason"] if triage else "",
        "duplicate_of_id": triage["duplicate_of_id"] if triage else None,
        "updated_at": triage["updated_at"] if triage else None,
        "updated_by": triage["updated_by"] if triage else None,
    }
    res["title"] = res["triage"]["title"]
    res["severity"] = res["triage"]["severity"]
    res["status"] = res["triage"]["status"]
    res["decision_basis_missing"] = res["status"] != "candidate" and not (res["triage"]["disposition_reason"] or "").strip()
    obs_desc = (conn.execute(
        "SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='description' ORDER BY id DESC LIMIT 1",
        (project_id, finding_id)).fetchone() or [""])[0]
    res["description"] = obs_desc

    if res["triage"]["duplicate_of_id"]:
        dup_key = conn.execute(
            "SELECT entity_key FROM entity_keys WHERE entity_id=? AND project_id=? AND canonical=1",
            (res["triage"]["duplicate_of_id"], project_id)).fetchone()
        res["triage"]["duplicate_of_display"] = dup_key[0] if dup_key else None
    else:
        res["triage"]["duplicate_of_display"] = None

    target = conn.execute(
        "SELECT r.dst_id AS id, e2.kind,"
        " (SELECT k2.entity_key FROM entity_keys k2 WHERE k2.entity_id=r.dst_id AND k2.project_id=r.project_id AND k2.canonical=1 LIMIT 1) AS display_key"
        " FROM relations r JOIN entities e2 ON e2.id=r.dst_id AND e2.project_id=r.project_id"
        " WHERE r.project_id=? AND r.src_id=? AND r.rel='found_on' AND r.is_current=1 LIMIT 1",
        (project_id, finding_id)).fetchone()
    if target:
        t_key = target["display_key"] or ""
        t_kind = str(target["kind"])
        if t_kind == "endpoint":
            ep_id = normalize.decode_endpoint_key(t_key)
            disp = normalize.endpoint_display(ep_id) if ep_id else t_key
        else:
            disp = t_key
        res["target_asset"] = {"id": target["id"], "kind": t_kind, "display_key": str(normalize.redact_secrets(disp))}
    else:
        res["target_asset"] = None

    ev_row = conn.execute(
        "SELECT v.id, v.locator, v.sha256, v.recovery_state, r.locator_uri, r.storage_state"
        " FROM v_active_contributions c"
        " JOIN evidence v ON v.id=c.evidence_id AND v.project_id=c.project_id"
        " JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
        " WHERE c.project_id=? AND ((c.subject_kind='entity' AND c.subject_id=?) OR c.owner_entity_id=?)"
        " ORDER BY c.id LIMIT 1", (project_id, finding_id, finding_id)).fetchone()
    if ev_row:
        res["initial_evidence"] = dict(ev_row)
    else:
        res["initial_evidence"] = None

    count = conn.execute(
        "SELECT COUNT(*) FROM finding_retests WHERE project_id=? AND finding_id=?",
        (project_id, finding_id)).fetchone()[0]
    retests = [dict(r) for r in conn.execute(
        "SELECT fr.id, fr.status, fr.notes, fr.evidence_id, fr.actor, fr.created_at,"
        " ev.locator, ev.sha256, rf.locator_uri"
        " FROM finding_retests fr"
        " LEFT JOIN evidence ev ON ev.id=fr.evidence_id AND ev.project_id=fr.project_id"
        " LEFT JOIN raw_files rf ON rf.id=ev.raw_file_id AND rf.project_id=ev.project_id"
        " WHERE fr.project_id=? AND fr.finding_id=? ORDER BY fr.id DESC LIMIT ?",
        (project_id, finding_id, retest_limit if retest_limit is not None else -1)).fetchall()]
    res["retests"] = retests
    for retest in retests:
        retest["evidence_basis"] = "linked_evidence" if retest["evidence_id"] is not None else "manual_statement_only"
    res["retests_count"] = count
    res["retests_truncated"] = count > len(retests)
    return public_value(res)


