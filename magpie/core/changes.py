"""Bounded change summaries derived from the same effective facts as CLI diff."""
from __future__ import annotations

import hashlib
import json

from . import normalize, query, store


def resolve_since(conn, project_id, spec='last'):
    text = str(spec)
    if text.isdigit():
        return {'spec':text,'at':query.job_started_at(conn,project_id,int(text)),'job_id':int(text)}
    if text == 'last':
        last = query.last_job(conn,project_id)
        return {'spec':text,'at':last[1] if last else None,'job_id':last[0] if last else None}
    if store.parse_time(text) is None:
        raise query.QueryError('E_BAD_SINCE', '基准只支持作业 ID、ISO 时间或 last')
    return {'spec':text,'at':text,'job_id':None}


def browse(conn, project_id, *, since='last', cursor=None, limit=50):
    from .workspace import _page
    baseline = resolve_since(conn,project_id,since)
    signature = hashlib.sha256(json.dumps([project_id,'changes',baseline]).encode()).hexdigest()[:20]
    # Last's identity is part of the cursor: a newly imported job invalidates it.
    sql = ('SELECT entity_id AS id,MAX(is_new) AS is_new,COUNT(*) AS fact_count'
           ' FROM (' + query._SINCE_SQL + ') GROUP BY entity_id')
    args = [project_id,baseline['at'],project_id,baseline['at']]
    page = _page(conn,sql,args,cursor=cursor,signature=signature,limit=limit)
    counts = conn.execute('SELECT COALESCE(SUM(is_new),0),COUNT(*)-COALESCE(SUM(is_new),0),'
                          'COALESCE(SUM(fact_count),0) FROM (' + sql + ')',args).fetchone()
    for row in page['rows']:
        entity = query.entity_row(conn,project_id,row['id'])
        row.update(entity)
        row['display_key'] = str(normalize.redact_secrets(normalize.endpoint_display(entity['entity_key'])))
        row['is_new'] = bool(row['is_new'])
    return {**page,'since':baseline,'counts':{'new':counts[0],'changed':counts[1],'facts':counts[2]},
            'notes':['按作业归属统计当前有效贡献；不代表目标的真实发生时间，也不包括被撤销的事实。']}
