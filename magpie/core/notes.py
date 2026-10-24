"""Human revisions over existing note observations; never rewrite parser facts."""
from __future__ import annotations

import hashlib
import json

from . import normalize, ops, query, store
from .finding_queries import public_value


def _evidence_value(row):
    return public_value({**dict(row), 'kind': 'evidence', 'available': True,
                         'display_key': f"证据 #{row['id']} · {row['entity_key']}"})


def evidence_ref(conn, project_id, evidence_id):
    row = conn.execute(
        'SELECT v.id,v.sha256,v.locator,r.locator_uri AS entity_key,r.storage_state'
        ' FROM evidence v JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id'
        ' WHERE v.project_id=? AND v.id=?', (project_id, evidence_id)).fetchone()
    if row is None:
        raise query.QueryError('E_BAD_RESEARCH', '证据不存在于当前项目')
    return _evidence_value(row)


def validate_evidence(conn, project_id, ids):
    if len(ids) > 20 or any(type(i) is not int or i <= 0 for i in ids):
        raise query.QueryError('E_BAD_RESEARCH', '每项记录最多关联 20 个项目内证据')
    return [evidence_ref(conn, project_id, eid) for eid in sorted(set(ids))]


def _details(conn, project_id, note_ids):
    """Read only the bounded page, with the same shape as an individual note."""
    if not note_ids:
        return []
    placeholders = ','.join('?' for _ in note_ids)
    rows = conn.execute(
        "SELECT o.id,o.value AS original_text,o.entity_id,o.observed_at AS created_at,"
        " o.is_current,e.is_current AS entity_current,e.merged_into,e.kind AS target_kind,k.entity_key,"
        " COALESCE(n.version,0) AS version,COALESCE(n.text,o.value) AS text,COALESCE(n.archived,0) AS archived,"
        " (SELECT evidence_id FROM v_active_contributions c WHERE c.project_id=o.project_id"
        " AND c.subject_kind='observation' AND c.subject_id=o.id ORDER BY c.id LIMIT 1) AS evidence_id"
        " FROM observations o JOIN entities e ON e.id=o.entity_id AND e.project_id=o.project_id"
        " JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1"
        " LEFT JOIN note_revisions n ON n.project_id=o.project_id AND n.observation_id=o.id"
        " AND n.version=(SELECT MAX(n2.version) FROM note_revisions n2"
        " WHERE n2.project_id=o.project_id AND n2.observation_id=o.id)"
        " WHERE o.project_id=? AND o.id IN (" + placeholders + ") AND o.semantic_field LIKE 'ai_note:%'",
        (project_id, *note_ids)).fetchall()
    links = {}
    for ref in conn.execute(
            'SELECT l.observation_id,v.id,v.sha256,v.locator,r.locator_uri AS entity_key,r.storage_state'
            ' FROM note_evidence_links l JOIN evidence v ON v.id=l.evidence_id AND v.project_id=l.project_id'
            ' JOIN raw_files r ON r.id=v.raw_file_id AND r.project_id=v.project_id'
            ' WHERE l.project_id=? AND l.observation_id IN (' + placeholders + ') ORDER BY l.evidence_id',
            (project_id, *note_ids)):
        value = dict(ref)
        note_id = value.pop('observation_id')
        links.setdefault(note_id, []).append(_evidence_value(value))
    result = {}
    for row in rows:
        refs = links.get(row['id'], [])
        if len(refs) > 20:
            raise query.QueryError('E_BAD_RESEARCH', '每项记录最多关联 20 个项目内证据')
        result[row['id']] = public_value({**dict(row), 'kind': 'note', 'archived': bool(row['archived']),
            'available': bool(row['is_current'] and row['entity_current'] and row['merged_into'] is None),
            'target_display': normalize.endpoint_display(row['entity_key']),
            'display_key': normalize.endpoint_display(row['entity_key']), 'evidence_links': refs})
    return [result[note_id] for note_id in note_ids if note_id in result]


def detail(conn, project_id, note_id):
    rows = _details(conn, project_id, [note_id])
    if not rows:
        raise query.QueryError('E_NOTE_NOT_FOUND', '笔记不存在于当前项目')
    return rows[0]


def browse(conn, project_id, *, q='', archived='active', entity_id=None, evidence_id=None, cursor=None, limit=50):
    from .workspace import _literal, _page
    if len(q) > 300 or archived not in ('active', 'archived', 'all'):
        raise query.QueryError('E_BAD_RESEARCH', '不支持的笔记筛选')
    where = ["o.project_id=?", "o.semantic_field LIKE 'ai_note:%'", 'o.is_current=1',
             'e.is_current=1', 'e.merged_into IS NULL',
             "(COALESCE(n.text,o.value) LIKE ? ESCAPE '\\' OR k.entity_key LIKE ? ESCAPE '\\')"]
    args = [project_id, '%' + _literal(q) + '%', '%' + _literal(q) + '%']
    if archived != 'all':
        where.append('COALESCE(n.archived,0)=?'); args.append(int(archived == 'archived'))
    if entity_id is not None:
        where.append('o.entity_id=?'); args.append(entity_id)
    if evidence_id is not None:
        where.append('EXISTS(SELECT 1 FROM note_evidence_links l WHERE l.project_id=o.project_id'
                     ' AND l.observation_id=o.id AND l.evidence_id=?)'); args.append(evidence_id)
    sql = ('SELECT o.id FROM observations o JOIN entities e ON e.id=o.entity_id AND e.project_id=o.project_id'
           ' JOIN entity_keys k ON k.entity_id=e.id AND k.project_id=e.project_id AND k.canonical=1'
           ' LEFT JOIN note_revisions n ON n.project_id=o.project_id AND n.observation_id=o.id'
           ' AND n.version=(SELECT MAX(n2.version) FROM note_revisions n2 WHERE n2.project_id=o.project_id'
           ' AND n2.observation_id=o.id) WHERE ' + ' AND '.join(where))
    signature = hashlib.sha256(json.dumps([project_id,'notes',q,archived,entity_id,evidence_id]).encode()).hexdigest()[:20]
    page = _page(conn, sql, args, cursor=cursor, signature=signature, limit=limit)
    page['rows'] = _details(conn, project_id, [row['id'] for row in page['rows']])
    return {**page, 'category': 'notes', 'mode': 'list'}


def revise(conn, *, project_id, note_id, text, archived=False, expected_version, reason, evidence_ids=(), actor='analyst'):
    if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 10000
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000
            or type(archived) is not bool or type(expected_version) is not int or expected_version < 0):
        raise query.QueryError('E_BAD_RESEARCH', '笔记正文须为 1–10000 字符，修改理由须为 1–2000 字符，并携带有效版本')
    with store.atomic(conn):
        before = detail(conn, project_id, note_id)
        if not before['available']:
            raise query.QueryError('E_BAD_RESEARCH', '笔记或目标已撤销/合并，不能修改')
        if before['version'] != expected_version:
            raise query.QueryError('E_STALE_RESEARCH', '笔记已被修改，请重新载入后检查草稿')
        refs = validate_evidence(conn, project_id, evidence_ids)
        version = expected_version + 1
        conn.execute('INSERT INTO note_revisions(project_id,observation_id,version,text,archived,reason,'
                     'evidence_ids,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)',
                     (project_id,note_id,version,text.strip(),int(archived),reason.strip(),
                      json.dumps([r['id'] for r in refs]),store.now(),actor))
        conn.execute('DELETE FROM note_evidence_links WHERE project_id=? AND observation_id=?', (project_id,note_id))
        conn.executemany('INSERT INTO note_evidence_links VALUES(?,?,?)', [(project_id,note_id,r['id']) for r in refs])
        result = detail(conn, project_id, note_id)
        store.log_activity(conn, actor=actor, action='research.note.revise', target_kind='observation', target_id=note_id,
                           detail={'project_id':project_id,'before_version':expected_version,'version':version,'reason':reason.strip()})
        return result


def history(conn, project_id, note_id, *, cursor=None, limit=20):
    from .workspace import _page
    detail(conn, project_id, note_id)
    signature = hashlib.sha256(f'{project_id}:note-history:{note_id}'.encode()).hexdigest()[:20]
    result = _page(conn, 'SELECT version AS id,version,text,archived,reason,evidence_ids,created_at,created_by'
                   ' FROM note_revisions WHERE project_id=? AND observation_id=?', [project_id,note_id],
                   cursor=cursor, signature=signature, limit=limit)
    return public_value(result)


def create(conn, *, project_id, entity_id, text, evidence_ids=(), actor='analyst'):
    with store.atomic(conn):
        validate_evidence(conn, project_id, evidence_ids)
        entity = query.entity_row(conn, project_id, entity_id)
        if not entity['is_current'] or entity['merged_into'] is not None or not 1 <= len(text.strip()) <= 10000:
            raise query.QueryError('E_BAD_RESEARCH', '需要有效目标和 1–10000 字符的笔记正文')
        result = ops.add_finding(conn, project_id=project_id, entity_id=entity_id, text=text, actor=actor, _shared_txn=True)
        if evidence_ids:
            current = detail(conn,project_id,result['observation_id'])
            if current['version']:
                raise query.QueryError('E_STALE_RESEARCH','同内容笔记已有人工修订，请打开已有笔记并携带版本关联证据')
            revise(conn, project_id=project_id, note_id=result['observation_id'], text=text,
                   expected_version=0,
                   reason='关联研究证据', evidence_ids=evidence_ids, actor=actor)
        return result
