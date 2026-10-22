"""Explicit human labels; source values stay in the existing fact store."""
from __future__ import annotations

from . import query, store
from .finding_queries import public_value


def read(conn, project_id, entity_id, *, redacted=True):
    query.entity_row(conn,project_id,entity_id)
    row = conn.execute('SELECT display_name,review_status,version,reason,updated_at,updated_by'
                       ' FROM entity_manual_values WHERE project_id=? AND entity_id=?', (project_id,entity_id)).fetchone()
    result = dict(row) if row else {'display_name':None,'review_status':None,'version':0,'reason':''}
    return public_value(result) if redacted else result


def save(conn, *, project_id, entity_id, expected_version, display_name, review_status, reason, actor):
    if (type(expected_version) is not int or expected_version < 0
            or display_name is not None and (not isinstance(display_name,str) or not 1 <= len(display_name.strip()) <= 300)
            or review_status not in (None,'unreviewed','reviewed','needs_review')
            or not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 2000):
        raise query.QueryError('E_BAD_RESEARCH','需要有效版本、1–300 字符名称和 1–2000 字符修改理由；审核状态不合法')
    with store.atomic(conn):
        entity = query.entity_row(conn,project_id,entity_id)
        if not entity['is_current'] or entity['merged_into'] is not None:
            raise query.QueryError('E_BAD_RESEARCH','不能修改已撤销或合并对象的人工字段')
        before = read(conn,project_id,entity_id)
        if before['version'] != expected_version:
            raise query.QueryError('E_STALE_RESEARCH','人工字段已被修改，请重新载入')
        conn.execute('INSERT INTO entity_manual_values VALUES(?,?,?,?,?,?,?,?)'
                     ' ON CONFLICT(project_id,entity_id) DO UPDATE SET display_name=excluded.display_name,'
                     'review_status=excluded.review_status,version=excluded.version,reason=excluded.reason,'
                     'updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                     (project_id,entity_id,display_name.strip() if display_name else None,review_status,
                      expected_version+1,reason.strip(),store.now(),actor))
        result = read(conn,project_id,entity_id)
        store.log_activity(conn,actor=actor,action='manual.set',target_kind='entity',target_id=entity_id,
                           detail={'project_id':project_id,'before':before,'after':result,'field_namespace':'human_labels'})
        return result
