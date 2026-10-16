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
from urllib.parse import unquote, urlsplit

from . import normalize, query

CATEGORIES = {
    "assets": ("domain", "ip", "host", "cidr"),
    "services": ("service", "product"),
    "api": ("endpoint", "apischema"),
    "js": ("endpoint",),
}
BASE = (" FROM entities e JOIN entity_keys k ON k.entity_id=e.id"
        " AND k.project_id=e.project_id AND k.canonical=1")
ACTIVE = "e.project_id=? AND e.is_current=1 AND e.merged_into IS NULL"
OWNER = "((c.subject_kind='entity' AND c.subject_id=e.id) OR c.owner_entity_id=e.id)"


def _host(kind, key):
    return key if kind == "ip" else (query._key_host(kind, key) or "")


def _path(key):
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
    return {
        "kinds": kinds,
        "assets": sum(kinds.get(k, 0) for k in CATEGORIES["assets"]),
        "services": sum(kinds.get(k, 0) for k in CATEGORIES["services"]),
        "api": sum(kinds.get(k, 0) for k in CATEGORIES["api"]),
        "findings": kinds.get("vuln", 0),
        "candidates": kinds.get("secret", 0),
        "evidence": scalar("SELECT COUNT(*) FROM evidence WHERE project_id=?"),
        "pending": scalar("SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'"),
        "failed_jobs": scalar("SELECT COUNT(*) FROM jobs WHERE project_id=? AND state='failed'"),
        "recent_jobs": query.list_jobs(conn, project_id=project_id, limit=6),
        "count_basis": "当前未合并实体（含退役）；证据含历史记录；工具命中未等同确认漏洞",
    }


def browse(conn, project_id, *, category="assets", mode="host", q="", host=None,
           prefix=None, method="", source="", cursor=None, limit=50):
    if category not in (*CATEGORIES, "evidence", "notes") or mode not in ("host", "path", "list"):
        raise query.QueryError("E_UNSUPPORTED_FILTER", "不支持的工作台分类或分组")
    if len(q) > 300 or len(source) > 200 or len(method) > 30:
        raise query.QueryError("E_UNSUPPORTED_FILTER", "筛选文本过长")
    signature = hashlib.sha256(json.dumps(
        [project_id, category, mode, q, host, prefix, method, source]).encode()).hexdigest()[:20]
    if category in ("evidence", "notes"):
        if mode != "list" or host is not None or prefix is not None or method or source:
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
                   " o.entity_id, o.observed_at AS created_at FROM observations o"
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
        return {**result, "mode": "list", "category": category}

    _register(conn)
    params = [project_id, *CATEGORIES[category]]
    where = [ACTIVE, "e.kind IN (" + ",".join("?" for _ in CATEGORIES[category]) + ")"]
    if category == "js":
        where.append("(ws_js(k.entity_key)=1 OR EXISTS (SELECT 1 FROM observations jo WHERE jo.project_id=e.project_id AND jo.entity_id=e.id AND jo.is_current=1 AND jo.semantic_field IN ('js_content_version','js_content_status')))")
    if q:
        where.append("k.entity_key LIKE ? ESCAPE '\\'")
        params.append("%" + _literal(q) + "%")
    if host is not None:
        where.append("ws_host(e.kind,k.entity_key)=?")
        params.append(host)
    if prefix is not None:
        where.append("ws_prefix(k.entity_key)=?")
        params.append(prefix)
    if method:
        where.append("EXISTS (SELECT 1 FROM observations o WHERE o.entity_id=e.id"
                     " AND o.project_id=e.project_id AND o.is_current=1"
                     " AND o.semantic_field IN ('method','http.method') AND UPPER(o.value)=?)")
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
            row["observed"] = [dict(r) for r in conn.execute(
                "SELECT semantic_field,value FROM observations WHERE project_id=? AND entity_id=?"
                " AND is_current=1 AND semantic_field IN ('method','http.method','status_code','status',"
                " 'http.status','http.status_code','title','js_content_version','js_content_status') ORDER BY id LIMIT 11", (project_id, row["id"]))]
            row['observed_truncated'] = len(row['observed']) > 8
            row['observed'] = row['observed'][:8]
            if category == "js":
                version = conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='js_content_version' ORDER BY id DESC LIMIT 1", (project_id, row["id"])).fetchone()
                status = conn.execute("SELECT value FROM observations WHERE project_id=? AND entity_id=? AND is_current=1 AND semantic_field='js_content_status' ORDER BY id DESC LIMIT 1", (project_id, row["id"])).fetchone()
                try:
                    row["js_content"] = json.loads(version[0]) if version else {"status": status[0] if status else "content_not_imported"}
                except (ValueError, TypeError):
                    row["js_content"] = {"status": "invalid_metadata"}
    return {**result, "mode": mode, "category": category}


def entity_detail(conn, project_id, entity_id, *, section="observations", cursor=None, limit=20):
    entity = query.entity_row(conn, project_id, entity_id)
    if section == "observations":
        sql = ("SELECT id,semantic_field,value,confidence,source_count,observed_at FROM observations"
               " WHERE project_id=? AND entity_id=? AND is_current=1")
        params = [project_id, entity_id]
    elif section == "js":
        sql = ("SELECT id,semantic_field,value,confidence,source_count,observed_at FROM observations"
               " WHERE project_id=? AND entity_id=? AND is_current=1"
               " AND semantic_field IN ('js_content_version','js_content_status','js_candidate')"
               " ORDER BY CASE semantic_field WHEN 'js_content_version' THEN 0 WHEN 'js_content_status' THEN 1 ELSE 2 END,id DESC")
        params = [project_id, entity_id]
    elif section == "relations":
        sql = ("SELECT r.id,r.rel,r.src_id,r.dst_id,k.entity_key AS other_key,"
               " CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END AS other_id"
               " FROM relations r LEFT JOIN entity_keys k ON k.entity_id="
               " CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END AND k.project_id=r.project_id AND k.canonical=1"
               " WHERE r.project_id=? AND r.is_current=1 AND (r.src_id=? OR r.dst_id=?)")
        params = [entity_id, entity_id, project_id, entity_id, entity_id]
    elif section == "evidence":
        sql = ("SELECT DISTINCT v.id,v.locator,v.recovery_state,r.locator_uri AS entity_key,r.storage_state"
               " FROM v_active_contributions c JOIN evidence v ON v.id=c.evidence_id AND v.project_id=c.project_id"
               " JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id"
               " WHERE c.project_id=? AND ((c.subject_kind='entity' AND c.subject_id=?) OR c.owner_entity_id=?)")
        params = [project_id, entity_id, entity_id]
    else:
        raise query.QueryError("E_UNSUPPORTED_FILTER", "详情分区必须是 observations / relations / evidence / js")
    result = _page(conn, sql, params, cursor=cursor,
                   signature=f"entity:{project_id}:{entity_id}:{section}", limit=limit)
    for row in result["rows"]:
        if "value" in row:
            value = str(normalize.redact_secrets(row["value"]))
            row["value"] = value[:8000]
            row["value_truncated"] = len(value) > 8000
            if row.get("semantic_field") == "js_candidate":
                try:
                    candidate = json.loads(row["value"])
                    if candidate.get("absolute"):
                        identity = normalize.endpoint_identity(candidate.get("url", ""))
                        found = conn.execute("SELECT e.id FROM entities e JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1 WHERE e.project_id=? AND e.kind='endpoint' AND k.entity_key=? AND e.is_current=1 AND e.merged_into IS NULL LIMIT 1", (project_id, identity["key"])).fetchone()
                        candidate["target_id"] = int(found[0]) if found else None
                    row["value"] = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                except (ValueError, TypeError, KeyError, normalize.NormalizeError):
                    row["value"] = "{}"
            elif row.get("semantic_field") == "js_source_ref":
                try:
                    source = json.loads(row["value"])
                    found = conn.execute("SELECT e.id FROM entities e JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1 WHERE e.project_id=? AND e.kind='endpoint' AND k.entity_key=? AND e.is_current=1 AND e.merged_into IS NULL LIMIT 1", (project_id, source.get("resource_key", ""))).fetchone()
                    source["resource_id"] = int(found[0]) if found else None
                    row["value"] = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
                except (ValueError, TypeError, KeyError, normalize.NormalizeError):
                    row["value"] = str(normalize.redact_secrets(row["value"]))[:8000]
    return {**result, "entity": entity, "section": section}


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
