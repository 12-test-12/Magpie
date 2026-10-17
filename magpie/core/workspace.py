"""Bounded read models for the research workspace; no second fact store.

Groups are exact canonical hosts and literal path prefixes, not inferred ownership
or API templates. Entity identity and effective facts remain in query/store.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from . import normalize, query

CATEGORIES = {
    "assets": ("domain", "ip", "host", "cidr"),
    "services": ("service", "product"),
    "api": ("endpoint", "apischema"),
    "js": ("endpoint",),
    "findings": ("vuln",),
    "vuln": ("vuln",),
}
BASE = (" FROM entities e JOIN entity_keys k ON k.entity_id=e.id"
        " AND k.project_id=e.project_id AND k.canonical=1")
ACTIVE = "e.project_id=? AND e.is_current=1 AND e.merged_into IS NULL"
OWNER = "((c.subject_kind='entity' AND c.subject_id=e.id) OR c.owner_entity_id=e.id)"


def _host(kind, key):
    if kind == "ip":
        return key
    if kind == "vuln":
        target = key.split("@", 1)[1] if "@" in key else key
        try:
            return urlsplit(target if "://" in target else "//" + target).hostname or ""
        except ValueError:
            return ""
    return query._key_host(kind, key) or ""


def _path(key):
    identity = normalize.decode_endpoint_key(key)
    if identity is not None:
        return str(identity["path"])
    try:
        return urlsplit(key if "://" in key else "//" + key).path or "/"
    except ValueError:
        return ""


def _prefix(key):
    # Keep concrete segments; never turn numeric IDs into invented templates.
    path = _path(key)
    return "/".join(path.split("/")[:3]) or "/"


def _register(conn):
    conn.create_function("ws_host", 2, _host, deterministic=True)
    conn.create_function("ws_prefix", 1, _prefix, deterministic=True)
    conn.create_function("ws_js", 1, lambda key: int(_path(key).lower().endswith((".js", ".mjs"))),
                         deterministic=True)
    conn.create_function("ws_method", 1,
                         lambda key: ((normalize.decode_endpoint_key(key) or {}).get("method", "")
                                      if str(key).startswith("ep2:") else ""),
                         deterministic=True)
    conn.create_function("ws_display", 1,
                         lambda key: normalize.endpoint_display(key), deterministic=True)


def _literal(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cursor(value, signature):
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if (not isinstance(data, list) or len(data) != 2 or data[0] != signature
                or type(data[1]) not in (int, str)):
            raise ValueError()
        return data[1]
    except (ValueError, TypeError, IndexError, UnicodeError):
        raise query.QueryError("E_BAD_CURSOR", "游标不属于当前项目或筛选，请从第一页重新查询") from None


def _encode(value, signature):
    return base64.urlsafe_b64encode(json.dumps([signature, value]).encode()).decode()


def _page(conn, sql, params, *, cursor, signature, limit, key="id", text=False):
    size = query.page_size(limit)
    if size > 100:
        raise query.QueryError("E_BAD_LIMIT", "工作台每页最多 100 条")
    after = _cursor(cursor, signature)
    if after is not None and (isinstance(after, str) != text):
        raise query.QueryError("E_BAD_CURSOR", "游标类型不匹配")
    # Grouping invokes canonical-host parsing. Compute group and entity counts
    # in that same scan instead of repeating the aggregation for every page.
    if text:
        sql = ("SELECT *,COUNT(*) OVER() AS __total,SUM(count) OVER() AS __entities"
               " FROM (" + sql + ")")
        total = 0
    else:
        total = conn.execute("SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM (" + sql + ") WHERE " + key + ">? ORDER BY " + key + " LIMIT ?",
        (*params, after if after is not None else ("" if text else 0), size + 1))]
    more = len(rows) > size
    rows = rows[:size]
    entities = 0
    if text:
        if rows:
            total, entities = rows[0]['__total'], rows[0]['__entities']
        else:
            first = conn.execute(sql + " LIMIT 1", params).fetchone()
            if first:
                total, entities = first['__total'], first['__entities']
        for row in rows:
            row.pop('__total'); row.pop('__entities')
    result = {"rows": rows, "total": total, "limit": size,
              "next_cursor": _encode(rows[-1][key], signature) if more else None}
    if text:
        result['entity_total'] = entities
    return result


def summary(conn, project_id):
    kinds = query.kind_counts(conn, project_id)
    scalar = lambda sql: conn.execute(sql, (project_id,)).fetchone()[0]
    confirmed_findings = scalar(
        "SELECT COUNT(*) FROM entities e JOIN finding_triage ft ON ft.finding_id=e.id AND ft.project_id=e.project_id"
        " WHERE e.project_id=? AND e.kind='vuln' AND e.is_current=1 AND e.merged_into IS NULL AND ft.status='confirmed'")
    candidate_findings = scalar(
        "SELECT COUNT(*) FROM entities e LEFT JOIN finding_triage ft ON ft.finding_id=e.id AND ft.project_id=e.project_id"
        " WHERE e.project_id=? AND e.kind='vuln' AND e.is_current=1 AND e.merged_into IS NULL AND COALESCE(ft.status, 'candidate')='candidate'")
    resolved_findings = scalar(
        "SELECT COUNT(*) FROM entities e JOIN finding_triage ft ON ft.finding_id=e.id AND ft.project_id=e.project_id"
        " WHERE e.project_id=? AND e.kind='vuln' AND e.is_current=1 AND e.merged_into IS NULL AND ft.status='resolved'")
    notes_count = scalar(
        "SELECT COUNT(*) FROM observations o WHERE o.project_id=? AND o.is_current=1 AND o.semantic_field LIKE 'ai_note:%'")
    return {
        "kinds": kinds,
        "assets": sum(kinds.get(k, 0) for k in CATEGORIES["assets"]),
        "services": sum(kinds.get(k, 0) for k in CATEGORIES["services"]),
        "api": sum(kinds.get(k, 0) for k in CATEGORIES["api"]),
        "findings": kinds.get("vuln", 0),
        "findings_confirmed": confirmed_findings,
        "findings_candidate": candidate_findings,
        "findings_resolved": resolved_findings,
        "candidates": kinds.get("secret", 0),
        "notes": notes_count,
        "evidence": scalar("SELECT COUNT(*) FROM evidence WHERE project_id=?"),
        "pending": scalar("SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'"),
        "failed_jobs": scalar("SELECT COUNT(*) FROM jobs WHERE project_id=? AND state='failed'"),
        "recent_jobs": query.list_jobs(conn, project_id=project_id, limit=6),
        "count_basis": "当前未合并实体（含退役）；证据含历史记录；工具命中未等同确认漏洞",
    }


def browse(conn, project_id, *, category="assets", mode="host", q="", host=None,
           prefix=None, method="", source="", status="", severity="", cursor=None, limit=50):
    if category not in (*CATEGORIES, "evidence", "notes") or mode not in ("host", "path", "list"):
        raise query.QueryError("E_UNSUPPORTED_FILTER", "不支持的工作台分类或分组")
    if len(q) > 300 or len(source) > 200 or len(method) > 30:
        raise query.QueryError("E_UNSUPPORTED_FILTER", "筛选文本过长")
    signature = hashlib.sha256(json.dumps(
        [project_id, category, mode, q, host, prefix, method, source, status, severity]).encode()).hexdigest()[:20]
    if category in ("evidence", "notes"):
        if mode != "list" or host is not None or prefix is not None or method or source or status or severity:
            raise query.QueryError("E_UNSUPPORTED_FILTER", "证据和笔记仅支持文本搜索与列表分页")
        if category == "evidence":
            sql = ("SELECT v.id, 'evidence' AS kind, r.locator_uri AS entity_key,"
                   " v.recovery_state AS status, r.storage_state, v.locator, v.sha256, v.created_at"
                   " FROM evidence v JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
                   " WHERE v.project_id=? AND (r.locator_uri LIKE ? ESCAPE '\\'"
                   " OR v.sha256 LIKE ? ESCAPE '\\')")
            params = [project_id, "%" + _literal(q) + "%", "%" + _literal(q) + "%"]
        else:
            sql = ("SELECT o.id, 'note' AS kind, k.entity_key, o.value AS text,"
                   " o.entity_id, e.kind AS target_kind,"
                   " ws_display(k.entity_key) AS target_display,"
                   " (SELECT c.evidence_id FROM v_active_contributions c WHERE c.project_id=o.project_id AND c.subject_kind='observation' AND c.subject_id=o.id ORDER BY c.id LIMIT 1) AS evidence_id,"
                   " o.observed_at AS created_at FROM observations o"
                   " JOIN entities e ON e.id=o.entity_id AND e.project_id=o.project_id"
                   " JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1"
                   " WHERE o.project_id=? AND o.is_current=1 AND e.is_current=1 AND e.merged_into IS NULL"
                   " AND o.semantic_field LIKE 'ai_note:%' AND"
                   " (o.value LIKE ? ESCAPE '\\' OR k.entity_key LIKE ? ESCAPE '\\')")
            params = [project_id, "%" + _literal(q) + "%", "%" + _literal(q) + "%"]
        result = _page(conn, sql, params, cursor=cursor, signature=signature, limit=limit)
        for row in result["rows"]:
            if "text" in row:
                row["text"] = str(normalize.redact_secrets(row["text"]))[:2000]
            if "target_display" in row:
                row["target_display"] = str(normalize.redact_secrets(row["target_display"] or row.get("entity_key", "")))
        return {**result, "mode": "list", "category": category}

    _register(conn)
    params = [project_id, *CATEGORIES[category]]
    where = [ACTIVE, "e.kind IN (" + ",".join("?" for _ in CATEGORIES[category]) + ")"]
    if category == "js":
        where.append("(ws_js(k.entity_key)=1 OR EXISTS (SELECT 1 FROM observations jo WHERE jo.project_id=e.project_id AND jo.entity_id=e.id AND jo.is_current=1 AND jo.semantic_field IN ('js_content_version','js_content_status')))")
    if category in ("findings", "vuln"):
        if status:
            where.append("COALESCE((SELECT ft.status FROM finding_triage ft WHERE ft.project_id=e.project_id AND ft.finding_id=e.id), 'candidate')=?")
            params.append(status.lower())
        if severity:
            where.append("LOWER(COALESCE((SELECT ft.severity FROM finding_triage ft WHERE ft.project_id=e.project_id AND ft.finding_id=e.id), (SELECT o.value FROM observations o WHERE o.project_id=e.project_id AND o.entity_id=e.id AND o.is_current=1 AND o.semantic_field='severity' ORDER BY o.id DESC LIMIT 1), 'unknown'))=?")
            params.append(severity.lower())
    if q:
        where.append("(k.entity_key LIKE ? ESCAPE '\\' OR ws_display(k.entity_key) LIKE ? ESCAPE '\\')")
        params.append("%" + _literal(q) + "%")
        params.append("%" + _literal(q) + "%")
    if host is not None:
        where.append("ws_host(e.kind,k.entity_key)=?")
        params.append(host)
    if prefix is not None:
        where.append("ws_prefix(k.entity_key)=?")
        params.append(prefix)
    if method:
        if normalize.lowercase_text(method) == "unknown":
            where.append("ws_method(k.entity_key)='unknown'")
        else:
            where.append("(ws_method(k.entity_key)=? OR EXISTS (SELECT 1 FROM observations o WHERE o.entity_id=e.id"
                         " AND o.project_id=e.project_id AND o.is_current=1"
                         " AND o.semantic_field IN ('method','http.method') AND UPPER(o.value)=?))")
            params.append(method.upper())
            params.append(method.upper())
    if source:
        where.append("EXISTS (SELECT 1 FROM v_active_contributions c JOIN sources s ON s.id=c.source_id"
                     " WHERE c.project_id=e.project_id AND " + OWNER + " AND s.name=?)")
        params.append(source)
    filtered = BASE + " WHERE " + " AND ".join(where)
    if mode != "list":
        expr = "ws_host(e.kind,k.entity_key)" if mode == "host" else "ws_prefix(k.entity_key)"
        # Prefix '@' permits the empty (unassociated) group in a strict keyset cursor.
        sql = "SELECT '@'||" + expr + " AS group_key," + expr + " AS label,COUNT(*) AS count" + filtered + " GROUP BY " + expr
        result = _page(conn, sql, params, cursor=cursor, signature=signature,
                       limit=limit, key="group_key", text=True)
    else:
        sql = ("SELECT e.id,e.kind,k.entity_key,e.lifecycle,e.last_seen,"
               " ws_host(e.kind,k.entity_key) AS host" + filtered)
        result = _page(conn, sql, params, cursor=cursor, signature=signature, limit=limit)
        # Bounded enrichment; never pull the project's observation table into the browser.
        for row in result["rows"]:
            if row.get("kind") == "endpoint":
                decoded = normalize.decode_endpoint_key(row["entity_key"])
                row["identity_status"] = "exact_v2" if decoded else "legacy_folded"
                row["endpoint_identity"] = decoded
                row["display_key"] = (str(normalize.redact_secrets(
                    normalize.endpoint_display(decoded))) if decoded else row["entity_key"])
            row["observed"] = [dict(r) for r in conn.execute(
                "SELECT semantic_field,value FROM observations WHERE project_id=? AND entity_id=?"
                " AND is_current=1 AND semantic_field IN ('method','http.method','declared_method','status_code','status',"
                " 'http.status','http.status_code','title','severity','description','js_content_version','js_content_status',"
                " 'js_source_ref','js_static_method') ORDER BY id LIMIT 13", (project_id, row["id"]))]
            row['observed_truncated'] = len(row['observed']) > 10
            row['observed'] = row['observed'][:10]
            if category in ("findings", "vuln"):
                triage = conn.execute(
                    "SELECT status, severity, title, disposition_reason, duplicate_of_id, updated_at, updated_by FROM finding_triage WHERE project_id=? AND finding_id=?",
                    (project_id, row["id"])).fetchone()
                row["status"] = triage["status"] if triage else "candidate"
                obs_sev = next((o["value"] for o in row["observed"] if o["semantic_field"] == "severity"), "unknown")
                row["severity"] = (triage["severity"] if triage and triage["severity"] else obs_sev).lower()
                obs_title = next((o["value"] for o in row["observed"] if o["semantic_field"] == "title"), row["entity_key"])
                row["title"] = triage["title"] if triage and triage["title"] else obs_title
                row["disposition_reason"] = triage["disposition_reason"] if triage else ""
                row["duplicate_of_id"] = triage["duplicate_of_id"] if triage else None
                row["updated_at"] = triage["updated_at"] if triage else None
                row["updated_by"] = triage["updated_by"] if triage else None
                row["retests_count"] = conn.execute(
                    "SELECT COUNT(*) FROM finding_retests WHERE project_id=? AND finding_id=?",
                    (project_id, row["id"])).fetchone()[0]
                target = conn.execute(
                    "SELECT r.dst_id AS id, e2.kind,"
                    " (SELECT k2.entity_key FROM entity_keys k2 WHERE k2.entity_id=r.dst_id AND k2.project_id=r.project_id AND k2.canonical=1 LIMIT 1) AS display_key"
                    " FROM relations r JOIN entities e2 ON e2.id=r.dst_id AND e2.project_id=r.project_id"
                    " WHERE r.project_id=? AND r.src_id=? AND r.rel='found_on' AND r.is_current=1 LIMIT 1",
                    (project_id, row["id"])).fetchone()
                if target:
                    t_key = target["display_key"] or ""
                    t_kind = str(target["kind"])
                    if t_kind == "endpoint":
                        ep_id = normalize.decode_endpoint_key(t_key)
                        disp = normalize.endpoint_display(ep_id) if ep_id else t_key
                    else:
                        disp = t_key
                    row["target_asset"] = {"id": target["id"], "kind": t_kind, "display_key": str(normalize.redact_secrets(disp))}
                else:
                    row["target_asset"] = None
                ev = conn.execute(
                    "SELECT c.evidence_id FROM v_active_contributions c WHERE c.project_id=? AND ((c.subject_kind='entity' AND c.subject_id=?) OR c.owner_entity_id=?) ORDER BY c.id LIMIT 1",
                    (project_id, row["id"], row["id"])).fetchone()
                row["initial_evidence_id"] = ev[0] if ev else None
            elif category == "js":
                version = conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='js_content_version' ORDER BY id DESC LIMIT 1", (project_id, row["id"])).fetchone()
                status_obs = conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='js_content_status' ORDER BY id DESC LIMIT 1", (project_id, row["id"])).fetchone()
                try:
                    row["js_content"] = json.loads(version[0]) if version else {"status": status_obs[0] if status_obs else "content_not_imported"}
                except (ValueError, TypeError):
                    row["js_content"] = {"status": "invalid_metadata"}
    return {**result, "mode": mode, "category": category}


def entity_detail(conn, project_id, entity_id, *, section="observations", cursor=None, limit=20):
    entity = query.entity_row(conn, project_id, entity_id)
    if entity.get("kind") == "endpoint":
        decoded = normalize.decode_endpoint_key(entity.get("entity_key", ""))
        entity["identity_status"] = "exact_v2" if decoded else "legacy_folded"
        entity["endpoint_identity"] = decoded
        entity["display_key"] = (str(normalize.redact_secrets(
            normalize.endpoint_display(decoded))) if decoded else entity["entity_key"])
        if decoded is None:
            entity["identity_notice"] = (
                "历史身份按路径模板折叠；确切路径、方法及每次请求组合无法从现存事实完整恢复。")
        events = conn.execute(
            "SELECT ev.id,ev.survivor_id,ev.reason FROM entity_merge_events ev"
            " JOIN entity_merge_members self ON self.merge_event_id=ev.id"
            " WHERE ev.project_id=? AND ev.state='merged' AND self.entity_id=?"
            " AND self.revoked_by_job_id IS NULL ORDER BY ev.id DESC LIMIT 21",
            (project_id, entity_id)).fetchall()
        entity["merge_groups_truncated"] = len(events) > 20
        entity["merge_groups"] = []
        legacy_member_seen = False
        for event in events[:20]:
            members = conn.execute(
                "SELECT m.entity_id,e.kind,(SELECT k.entity_key FROM entity_keys k"
                " WHERE k.entity_id=m.entity_id AND k.project_id=m.project_id"
                " ORDER BY k.canonical DESC,k.created_at,k.entity_key LIMIT 1) AS entity_key"
                " FROM entity_merge_members m"
                " JOIN entities e ON e.id=m.entity_id AND e.project_id=m.project_id"
                " WHERE m.project_id=? AND m.merge_event_id=? AND m.revoked_by_job_id IS NULL"
                " ORDER BY m.entity_id LIMIT 51",
                (project_id, int(event["id"]))).fetchall()
            unique = {}
            for member in members:
                mid = int(member["entity_id"])
                if mid in unique:
                    continue
                key = str(member["entity_key"] or "")
                identity = normalize.decode_endpoint_key(key) if member["kind"] == "endpoint" else None
                legacy = member["kind"] == "endpoint" and identity is None
                legacy_member_seen = legacy_member_seen or legacy
                unique[mid] = {"entity_id": mid, "kind": str(member["kind"]),
                               "display_key": (str(normalize.redact_secrets(normalize.endpoint_display(identity)))
                                               if identity else str(normalize.redact_secrets(key))),
                               "identity_status": "legacy_folded" if legacy else
                                                 "exact_v2" if identity else "not_applicable"}
            entity["merge_groups"].append({"event_id": int(event["id"]),
                                            "reason": str(event["reason"]),
                                            "members": list(unique.values()),
                                            "members_truncated": len(members) > 50})
        if legacy_member_seen:
            entity["identity_notice"] = (
                "此合并组含历史折叠身份；组内历史路径和方法无法完整恢复，精确成员仅代表可解码的 v2 身份。")
    if entity.get("kind") == "vuln":
        triage = conn.execute(
            "SELECT status, severity, title, disposition_reason, duplicate_of_id, updated_at, updated_by"
            " FROM finding_triage WHERE project_id=? AND finding_id=?",
            (project_id, entity_id)).fetchone()
        retests = [dict(r) for r in conn.execute(
            "SELECT fr.id, fr.status, fr.notes, fr.evidence_id, fr.actor, fr.created_at,"
            " ev.locator, ev.sha256, rf.locator_uri"
            " FROM finding_retests fr"
            " LEFT JOIN evidence ev ON ev.id=fr.evidence_id AND ev.project_id=fr.project_id"
            " LEFT JOIN raw_files rf ON rf.id=ev.raw_file_id AND rf.project_id=ev.project_id"
            " WHERE fr.project_id=? AND fr.finding_id=? ORDER BY fr.id DESC LIMIT 50",
            (project_id, entity_id)).fetchall()]
        target = conn.execute(
            "SELECT r.dst_id AS id, e2.kind,"
            " (SELECT k2.entity_key FROM entity_keys k2 WHERE k2.entity_id=r.dst_id AND k2.project_id=r.project_id AND k2.canonical=1 LIMIT 1) AS display_key"
            " FROM relations r JOIN entities e2 ON e2.id=r.dst_id AND e2.project_id=r.project_id"
            " WHERE r.project_id=? AND r.src_id=? AND r.rel='found_on' AND r.is_current=1 LIMIT 1",
            (project_id, entity_id)).fetchone()
        if target:
            t_key = target["display_key"] or ""
            t_kind = str(target["kind"])
            if t_kind == "endpoint":
                ep_id = normalize.decode_endpoint_key(t_key)
                disp = normalize.endpoint_display(ep_id) if ep_id else t_key
            else:
                disp = t_key
            target_asset = {"id": target["id"], "kind": t_kind, "display_key": str(normalize.redact_secrets(disp))}
        else:
            target_asset = None
        obs_sev = (conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='severity' ORDER BY id DESC LIMIT 1", (project_id, entity_id)).fetchone() or ["unknown"])[0]
        obs_title = (conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='title' ORDER BY id DESC LIMIT 1", (project_id, entity_id)).fetchone() or [entity.get("entity_key")])[0]
        entity["finding_info"] = {
            "status": triage["status"] if triage else "candidate",
            "severity": (triage["severity"] if triage and triage["severity"] else obs_sev).lower(),
            "title": triage["title"] if triage and triage["title"] else obs_title,
            "disposition_reason": triage["disposition_reason"] if triage else "",
            "duplicate_of_id": triage["duplicate_of_id"] if triage else None,
            "updated_at": triage["updated_at"] if triage else None,
            "updated_by": triage["updated_by"] if triage else None,
            "target_asset": target_asset,
            "retests": retests,
        }
    if section == "observations":
        sql = ("SELECT id,semantic_field,value,confidence,source_count,observed_at,"
               " (SELECT c.evidence_id FROM v_active_contributions c WHERE c.project_id=o.project_id"
               " AND c.subject_kind='observation' AND c.subject_id=o.id ORDER BY c.id LIMIT 1) AS evidence_id,"
               " (SELECT s.name FROM v_active_contributions c JOIN sources s ON s.id=c.source_id"
               " WHERE c.project_id=o.project_id AND c.subject_kind='observation' AND c.subject_id=o.id"
               " ORDER BY c.id LIMIT 1) AS source"
               " FROM observations o"
               " WHERE project_id=? AND entity_id=? AND is_current=1")
        params = [project_id, entity_id]
    elif section == "js":
        sql = ("SELECT id,semantic_field,value,confidence,source_count,observed_at,"
               " (SELECT c.evidence_id FROM v_active_contributions c WHERE c.project_id=o.project_id"
               " AND c.subject_kind='observation' AND c.subject_id=o.id ORDER BY c.id LIMIT 1) AS evidence_id,"
               " (SELECT s.name FROM v_active_contributions c JOIN sources s ON s.id=c.source_id"
               " WHERE c.project_id=o.project_id AND c.subject_kind='observation' AND c.subject_id=o.id"
               " ORDER BY c.id LIMIT 1) AS source"
               " FROM observations o"
               " WHERE project_id=? AND entity_id=? AND is_current=1"
               " AND semantic_field IN ('js_content_version','js_content_status','js_candidate')"
               " ORDER BY CASE semantic_field WHEN 'js_content_version' THEN 0 WHEN 'js_content_status' THEN 1 ELSE 2 END,id DESC")
        params = [project_id, entity_id]
    elif section in ("samples", "parameters", "js_cross"):
        if section == "samples":
            predicate = "semantic_field='http_request_sample'"
            order = "id DESC"
        elif section == "parameters":
            predicate = "(semantic_field LIKE 'param.%' OR semantic_field='js_static_param')"
            order = "semantic_field,id"
        else:
            predicate = "semantic_field IN ('js_candidate','js_source_ref','js_static_method','js_content_version','js_content_status')"
            order = "CASE semantic_field WHEN 'js_content_version' THEN 0 WHEN 'js_source_ref' THEN 1 WHEN 'js_static_method' THEN 2 WHEN 'js_candidate' THEN 3 ELSE 4 END,id DESC"
        sql = ("SELECT id,semantic_field,value,confidence,source_count,observed_at,"
               " (SELECT c.evidence_id FROM v_active_contributions c WHERE c.project_id=o.project_id"
               " AND c.subject_kind='observation' AND c.subject_id=o.id ORDER BY c.id LIMIT 1) AS evidence_id,"
               " (SELECT s.name FROM v_active_contributions c JOIN sources s ON s.id=c.source_id"
               " WHERE c.project_id=o.project_id AND c.subject_kind='observation' AND c.subject_id=o.id"
               " ORDER BY c.id LIMIT 1) AS source"
               " FROM observations o WHERE project_id=? AND entity_id=? AND is_current=1 AND "
               + predicate + " ORDER BY " + order)
        params = [project_id, entity_id]
    elif section == "relations":
        sql = ("SELECT r.id,r.rel,r.src_id,r.dst_id,k.entity_key AS other_key,e.kind AS other_kind,"
               " CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END AS other_id"
               " FROM relations r LEFT JOIN entity_keys k ON k.entity_id="
               " CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END AND k.project_id=r.project_id AND k.canonical=1"
               " LEFT JOIN entities e ON e.id=CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END"
               " AND e.project_id=r.project_id"
               " WHERE r.project_id=? AND r.is_current=1 AND (r.src_id=? OR r.dst_id=?)")
        params = [entity_id, entity_id, entity_id, project_id, entity_id, entity_id]
    elif section == "evidence":
        sql = ("SELECT DISTINCT v.id,v.locator,v.recovery_state,r.locator_uri AS entity_key,r.storage_state"
               " FROM v_active_contributions c JOIN evidence v ON v.id=c.evidence_id AND v.project_id=c.project_id"
               " JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
               " WHERE c.project_id=? AND ((c.subject_kind='entity' AND c.subject_id=?) OR c.owner_entity_id=?)")
        params = [project_id, entity_id, entity_id]
    elif section == "retests":
        sql = ("SELECT fr.id, fr.status, fr.notes, fr.evidence_id, fr.actor, fr.created_at,"
               " ev.locator, ev.sha256, rf.locator_uri"
               " FROM finding_retests fr"
               " LEFT JOIN evidence ev ON ev.id=fr.evidence_id AND ev.project_id=fr.project_id"
               " LEFT JOIN raw_files rf ON rf.id=ev.raw_file_id AND rf.project_id=ev.project_id"
               " WHERE fr.project_id=? AND fr.finding_id=? ORDER BY fr.id DESC")
        params = [project_id, entity_id]
    else:
        raise query.QueryError("E_UNSUPPORTED_FILTER", "详情分区必须是 observations / relations / evidence / js / samples / parameters / js_cross / retests")
    result = _page(conn, sql, params, cursor=cursor,
                   signature=f"entity:{project_id}:{entity_id}:{section}", limit=limit)
    for row in result["rows"]:
        if row.get("other_kind") == "endpoint":
            other_identity = normalize.decode_endpoint_key(row.get("other_key", ""))
            row["other_display"] = (normalize.endpoint_display(other_identity)
                                    if other_identity else row.get("other_key"))
        if "value" in row:
            if row.get("semantic_field", "").startswith("param."):
                field = str(row["semantic_field"])
                value_text = str(row["value"])
                is_location = value_text.lower() in {"query", "path", "header", "cookie", "body", "unknown"}
                is_type = field.endswith(".type") and len(value_text) <= 100
                is_required = field.endswith(".required") and value_text.lower() in {"true", "false", "0", "1"}
                if not (is_location or is_type or is_required):
                    row["value"] = "[未在参数结构页展示]"
                    row["value_hidden"] = True
                else:
                    row["value"] = value_text[:100]
            value = str(normalize.redact_secrets(row["value"]))
            if row.get("semantic_field") == "path":
                value = _mask_query_values(value)
            row["value"] = value[:8000]
            row["value_truncated"] = len(value) > 8000
            if row.get("semantic_field") == "js_candidate":
                try:
                    candidate = json.loads(row["value"])
                    if candidate.get("absolute"):
                        identity = normalize.endpoint_identity(
                            candidate.get("url", ""), method=candidate.get("method"))
                        found = conn.execute("SELECT e.id FROM entities e JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1 WHERE e.project_id=? AND e.kind='endpoint' AND k.entity_key=? AND e.is_current=1 AND e.merged_into IS NULL LIMIT 1", (project_id, identity["key"])).fetchone()
                        candidate["target_id"] = int(found[0]) if found else None
                        candidate["http_observed"] = bool(found and conn.execute(
                            "SELECT 1 FROM observations WHERE project_id=? AND entity_id=? AND is_current=1"
                            " AND semantic_field IN ('method','http_request_sample') LIMIT 1",
                            (project_id, int(found[0]))).fetchone())
                    row["value"] = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                except (ValueError, TypeError, KeyError, normalize.NormalizeError):
                    row["value"] = "{}"
            elif row.get("semantic_field") == "js_source_ref":
                try:
                    source = json.loads(row["value"])
                    resource_identity = normalize.decode_endpoint_key(
                        source.get("resource_key", ""))
                    source["resource_display"] = (
                        normalize.endpoint_display(resource_identity)
                        if resource_identity else source.get("resource_key", "JS 资源"))
                    found = conn.execute("SELECT e.id FROM entities e JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1 WHERE e.project_id=? AND e.kind='endpoint' AND k.entity_key=? AND e.is_current=1 AND e.merged_into IS NULL LIMIT 1", (project_id, source.get("resource_key", ""))).fetchone()
                    source["resource_id"] = int(found[0]) if found else None
                    row["value"] = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
                except (ValueError, TypeError, KeyError, normalize.NormalizeError):
                    row["value"] = str(normalize.redact_secrets(row["value"]))[:8000]
    return {**result, "entity": entity, "section": section}


def _mask_query_values(value: str) -> str:
    """Business detail shows exact path shape, with every query value hidden."""
    try:
        parts = urlsplit(value)
        if not parts.query:
            return value
        query = urlencode([(name, "[redacted]") for name, _ in
                           parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return value.split("?", 1)[0] + "?[redacted]"


def evidence_detail(conn, project_id, evidence_id, data_dir):
    row = conn.execute(
        "SELECT v.*,r.locator_uri,r.storage_state,r.sha256 AS file_sha256,r.byte_count"
        " FROM evidence v JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
        " WHERE v.project_id=? AND v.id=?", (project_id, evidence_id)).fetchone()
    if row is None:
        raise query.QueryError("E_ENTITY_NOT_FOUND", "当前项目中没有该证据")
    result = dict(row)
    result["contributions"] = [dict(r) for r in conn.execute(
        "SELECT c.id,c.subject_kind,c.subject_id,c.owner_entity_id,c.job_id,s.name AS source"
        " FROM v_active_contributions c JOIN sources s ON s.id=c.source_id"
        " WHERE c.project_id=? AND c.evidence_id=? ORDER BY c.id LIMIT 51", (project_id, evidence_id))]
    result["contributions_truncated"] = len(result["contributions"]) > 50
    result["contributions"] = result["contributions"][:50]
    result.update(preview=None, preview_state="unavailable", preview_reason="原文未归档或已不可用")
    # Only managed, content-addressed archives are readable. Never resolve arbitrary
    # file:// pointers from imported metadata into a local file disclosure endpoint.
    root = Path(data_dir).resolve() / "raw"
    uri = urlsplit(row["locator_uri"])
    if uri.scheme != "file" or uri.netloc or row["storage_state"] not in ("present", "archived"):
        return result
    path = Path(unquote(uri.path.lstrip("/") if len(uri.path) > 2 and uri.path[2] == ":" else uri.path)).resolve()
    if root not in path.parents or path.name != row["file_sha256"]:
        return result
    try:
        with path.open("rb") as stream:
            content = stream.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            result["preview_reason"] = "原文超过 16 MiB 预览上限"
            return result
        if hashlib.sha256(content).hexdigest() != row["file_sha256"]:
            result["preview_reason"] = "归档内容校验失败"
            return result
        if b"\x00" in content[:8192]:
            result["preview_reason"] = "二进制原文仅提供定位与哈希"
            return result
        locator = query.json_or_none(row["locator"]) or {}
        if not isinstance(locator, dict):
            locator = {}
        text = content.decode("utf-8", errors="replace")
        line = locator.get("line")
        if isinstance(line, int) and 1 <= line <= 1000000:
            # Cap input by bytes above; splitlines cannot traverse an unbounded file.
            lines = text.splitlines()
            text = "\n".join(lines[max(0, line - 2):line + 2])
            result["preview_reason"] = f"第 {line} 行附近的脱敏上下文（并非已验证的精确片段）"
        else:
            result["preview_reason"] = "文件开头的脱敏预览；非行定位请对照原始 locator"
        result.update(preview=str(normalize.redact_secrets(text))[:8000], preview_state="available",
                      preview_truncated=len(text) > 8000)
    except (OSError, ValueError):
        result["preview_reason"] = "归档文件无法读取"
    return result


def finding_detail(conn, project_id, finding_id):
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
    res["host"] = _host("vuln", key)

    triage = conn.execute(
        "SELECT status, severity, title, disposition_reason, duplicate_of_id, updated_at, updated_by"
        " FROM finding_triage WHERE project_id=? AND finding_id=?",
        (project_id, finding_id)).fetchone()
    obs_sev = (conn.execute(
        "SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='severity' ORDER BY id DESC LIMIT 1",
        (project_id, finding_id)).fetchone() or ["unknown"])[0]
    obs_title = (conn.execute(
        "SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='title' ORDER BY id DESC LIMIT 1",
        (project_id, finding_id)).fetchone() or [key])[0]

    res["triage"] = {
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

    retests = [dict(r) for r in conn.execute(
        "SELECT fr.id, fr.status, fr.notes, fr.evidence_id, fr.actor, fr.created_at,"
        " ev.locator, ev.sha256, rf.locator_uri"
        " FROM finding_retests fr"
        " LEFT JOIN evidence ev ON ev.id=fr.evidence_id AND ev.project_id=fr.project_id"
        " LEFT JOIN raw_files rf ON rf.id=ev.raw_file_id AND rf.project_id=ev.project_id"
        " WHERE fr.project_id=? AND fr.finding_id=? ORDER BY fr.id DESC",
        (project_id, finding_id)).fetchall()]
    res["retests"] = retests
    return res


def generate_report(conn, project_id, format="markdown"):
    proj = conn.execute("SELECT id, name, scope, status FROM projects WHERE id=?", (project_id,)).fetchone()
    if proj is None:
        raise query.QueryError("E_PROJECT_NOT_FOUND", "项目不存在")
    proj_name = proj["name"]
    from . import store
    t_now = store.now()

    kinds = query.kind_counts(conn, project_id)
    findings_total = kinds.get("vuln", 0)

    finding_rows = conn.execute(
        "SELECT e.id, k.entity_key, ft.status, ft.severity, ft.title, ft.disposition_reason, ft.duplicate_of_id"
        " FROM entities e"
        " JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1"
        " LEFT JOIN finding_triage ft ON ft.project_id=e.project_id AND ft.finding_id=e.id"
        " WHERE e.project_id=? AND e.kind='vuln' AND e.is_current=1 AND e.merged_into IS NULL"
        " ORDER BY CASE COALESCE(ft.status, 'candidate') WHEN 'confirmed' THEN 1 WHEN 'candidate' THEN 2 WHEN 'resolved' THEN 3 ELSE 4 END, e.id",
        (project_id,)).fetchall()

    enriched_findings = []
    for r in finding_rows:
        fid = int(r["id"])
        key = str(r["entity_key"])
        obs_sev = (conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='severity' ORDER BY id DESC LIMIT 1", (project_id, fid)).fetchone() or ["unknown"])[0]
        obs_title = (conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='title' ORDER BY id DESC LIMIT 1", (project_id, fid)).fetchone() or [key])[0]
        obs_desc = (conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='description' ORDER BY id DESC LIMIT 1", (project_id, fid)).fetchone() or [""])[0]

        target = conn.execute(
            "SELECT r.dst_id AS id, e2.kind,"
            " (SELECT k2.entity_key FROM entity_keys k2 WHERE k2.entity_id=r.dst_id AND k2.project_id=r.project_id AND k2.canonical=1 LIMIT 1) AS display_key"
            " FROM relations r JOIN entities e2 ON e2.id=r.dst_id AND e2.project_id=r.project_id"
            " WHERE r.project_id=? AND r.src_id=? AND r.rel='found_on' AND r.is_current=1 LIMIT 1",
            (project_id, fid)).fetchone()
        t_key = target["display_key"] if target else ""
        if target and target["kind"] == "endpoint":
            ep_id = normalize.decode_endpoint_key(t_key)
            t_disp = normalize.endpoint_display(ep_id) if ep_id else t_key
        else:
            t_disp = t_key

        ev_row = conn.execute(
            "SELECT v.id, v.locator, v.sha256, r.locator_uri"
            " FROM v_active_contributions c"
            " JOIN evidence v ON v.id=c.evidence_id AND v.project_id=c.project_id"
            " JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
            " WHERE c.project_id=? AND ((c.subject_kind='entity' AND c.subject_id=?) OR c.owner_entity_id=?)"
            " ORDER BY c.id LIMIT 1", (project_id, fid, fid)).fetchone()

        retests = [dict(rt) for rt in conn.execute(
            "SELECT fr.id, fr.status, fr.notes, fr.evidence_id, fr.actor, fr.created_at,"
            " ev.locator, ev.sha256, rf.locator_uri"
            " FROM finding_retests fr"
            " LEFT JOIN evidence ev ON ev.id=fr.evidence_id AND ev.project_id=fr.project_id"
            " LEFT JOIN raw_files rf ON rf.id=ev.raw_file_id AND rf.project_id=ev.project_id"
            " WHERE fr.project_id=? AND fr.finding_id=? ORDER BY fr.id DESC",
            (project_id, fid)).fetchall()]

        st = r["status"] or "candidate"
        sev = (r["severity"] or obs_sev).lower()
        title = r["title"] or obs_title

        enriched_findings.append({
            "id": fid, "key": key, "status": st, "severity": sev, "title": title,
            "description": obs_desc,
            "disposition_reason": r["disposition_reason"] or "",
            "target": {"kind": str(target["kind"]) if target else "", "display_key": str(t_disp)} if target else None,
            "initial_evidence": dict(ev_row) if ev_row else None,
            "retests": retests,
        })

    notes = [dict(n) for n in conn.execute(
        "SELECT o.id, o.value AS text, o.observed_at AS created_at, k.entity_key"
        " FROM observations o"
        " JOIN entities e ON e.id=o.entity_id AND e.project_id=o.project_id"
        " JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1"
        " WHERE o.project_id=? AND o.is_current=1 AND e.is_current=1 AND e.merged_into IS NULL"
        " AND o.semantic_field LIKE 'ai_note:%' ORDER BY o.id DESC LIMIT 50", (project_id,)).fetchall()]

    confirmed_list = [f for f in enriched_findings if f["status"] == "confirmed"]
    candidate_list = [f for f in enriched_findings if f["status"] == "candidate"]
    resolved_list = [f for f in enriched_findings if f["status"] == "resolved"]
    fp_list = [f for f in enriched_findings if f["status"] == "false_positive"]
    dup_list = [f for f in enriched_findings if f["status"] == "duplicate"]
    inc_list = [f for f in enriched_findings if f["status"] == "inconclusive"]

    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in confirmed_list:
        s = f["severity"]
        sev_counts[s] = sev_counts.get(s, 0) + 1

    summary_metrics = {
        "findings_total": findings_total,
        "confirmed": len(confirmed_list),
        "candidates": len(candidate_list),
        "resolved": len(resolved_list),
        "false_positive": len(fp_list),
        "duplicate": len(dup_list),
        "inconclusive": len(inc_list),
        "severity_breakdown": sev_counts,
        "notes_count": len(notes),
    }

    if format == "json":
        return {
            "project": {"id": proj["id"], "name": proj_name, "scope": proj["scope"]},
            "generated_at": t_now,
            "summary": summary_metrics,
            "confirmed_findings": confirmed_list,
            "resolved_findings": resolved_list,
            "candidates": candidate_list,
            "false_positives": fp_list,
            "duplicates": dup_list,
            "inconclusive": inc_list,
            "research_notes": notes,
        }

    # Markdown format
    lines = [
        f"# Magpie 安全研究与漏洞发现报告",
        f"",
        f"> **项目名称**：{proj_name}  ",
        f"> **生成时刻**：{t_now}  ",
        f"> **数据边界**：本地离线数据 · 零主动网络扫描 · 事实与判定分离",
        f"",
        f"## 1. 执行摘要与发现总览",
        f"",
        f"| 状态分类 | 数量 | 说明 |",
        f"|---|---:|---|",
        f"| **已确认漏洞（Confirmed）** | **{len(confirmed_list)}** | 经人工复核或有效复测证据验证 |",
        f"| **待核实发现（Candidate）** | {len(candidate_list)} | 扫描器工具命中或静态候选，未完成人工判定 |",
        f"| **已修复漏洞（Resolved）** | {len(resolved_list)} | 经后续导入的复测证据验证修复 |",
        f"| **误报处置（False Positive）** | {len(fp_list)} | 经人工核实非漏洞，已记录排除理由 |",
        f"| **重复发现（Duplicate）** | {len(dup_list)} | 关联至主漏洞记录 |",
        f"| **存疑未决（Inconclusive）** | {len(inc_list)} | 证据不足或环境限制无法确认 |",
        f"",
        f"### 已确认漏洞严重级别分布",
        f"",
        f"- **Critical（严重）**：{sev_counts.get('critical', 0)}",
        f"- **High（高危）**：{sev_counts.get('high', 0)}",
        f"- **Medium（中危）**：{sev_counts.get('medium', 0)}",
        f"- **Low（低危）**：{sev_counts.get('low', 0)}",
        f"- **Info（提示）**：{sev_counts.get('info', 0)}",
        f"",
        f"---",
        f"",
        f"## 2. 已确认漏洞列表（Confirmed Findings）",
        f"",
    ]

    if not confirmed_list:
        lines.append("*当前暂无已确认漏洞记录（工具命中未自动升级为确认漏洞）。*\n")
    else:
        for idx, f in enumerate(confirmed_list, 1):
            lines.append(f"### 2.{idx} [{f['severity'].upper()}] {f['title']}")
            lines.append(f"- **漏洞标识**：`{f['key']}`")
            if f.get("target"):
                lines.append(f"- **目标资产**：`{f['target']['kind']}` · `{f['target']['display_key']}`")
            if f.get("disposition_reason"):
                lines.append(f"- **确认理由/分析说明**：{f['disposition_reason']}")
            if f.get("description"):
                lines.append(f"- **详细描述**：\n  > {f['description']}")
            if f.get("initial_evidence"):
                ev = f["initial_evidence"]
                lines.append(f"- **首次发现证据**：")
                lines.append(f"  - 来源定位：`{ev.get('locator_uri')}`")
                lines.append(f"  - 片段定位：`{ev.get('locator')}` · SHA-256: `{ev.get('sha256')}`")
            if f.get("retests"):
                lines.append(f"- **人工复测记录（共 {len(f['retests'])} 次）**：")
                for rt in f["retests"]:
                    lines.append(f"  - [{rt['created_at']}] 复测人: {rt['actor']} · 结论: **{rt['status']}**")
                    lines.append(f"    - 说明: {rt['notes']}")
                    if rt.get("sha256"):
                        lines.append(f"    - 关联复测证据: SHA-256 `{rt['sha256']}` (`{rt.get('locator_uri')}`)")
            lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 3. 处置决策记录（误报、修复与未决）",
        f"",
    ])
    disposed = resolved_list + fp_list + dup_list + inc_list
    if not disposed:
        lines.append("*暂无其他处置决策记录。*\n")
    else:
        for d in disposed:
            lines.append(f"- **[{d['status'].upper()}]** {d['title']} (`{d['key']}`)")
            if d.get("disposition_reason"):
                lines.append(f"  - 判定理由：{d['disposition_reason']}")
            lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 4. 研究线索与备忘笔记（Research Notes）",
        f"",
    ])
    if not notes:
        lines.append("*暂无关联研究笔记。*\n")
    else:
        for n in notes:
            lines.append(f"- `[{n['created_at']}]`（对象：`{n['entity_key']}`）")
            lines.append(f"  > {str(normalize.redact_secrets(n['text']))[:500]}")
            lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 5. 方法学、数据源与局限性声明",
        f"",
        f"1. **零主动网络扫描**：本报告全部数据、接口识别与漏洞发现均基于用户显式导入的文件（如 HAR、Nuclei JSONL、OpenAPI、JS 清单等）或人工记录，系统不向任何外部网络地址发起主动探测。",
        f"2. **工具命中与确认分立**：自动化工具输出的命中仅记为观察事实与候选，不直接等同于已被利用或真实漏洞；必须经过人工审查与证据链路校验。",
        f"3. **复测真实性保证**：网络超时、凭据过期或目标不可达不能作为“漏洞已修复”的推论。所有修复判定均需提供人工复测记录与实际证据。",
        f"4. **证据可追溯**：每个发现均可回查原始导入定位、片段哈希与受管归档，防止虚构或不可复现的报告产出。",
        f"",
    ])

    return "\n".join(lines)
