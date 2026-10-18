"""Human task and priority work records, referencing the existing entity store.

No parser facts are written here. Records survive import Undo/Redo; linked
targets honestly report when their effective facts have been withdrawn.
"""
from __future__ import annotations

import hashlib
import json

from . import normalize, query, store
from .manual import ManualWriter

STATUSES = ('pending', 'in_progress', 'done', 'shelved')
TARGET_KINDS = ('domain', 'ip', 'host', 'endpoint', 'vuln')
METADATA_KINDS = ('domain', 'ip', 'host', 'endpoint')


def _bad(message):
    raise query.QueryError('E_BAD_RESEARCH', message)


def _target(conn, project_id, entity_id, kinds=TARGET_KINDS, *, require_current=True):
    row = conn.execute(
        'SELECT e.id,e.kind,e.is_current,e.merged_into,'
        ' (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id AND k.project_id=e.project_id'
        ' ORDER BY k.canonical DESC,k.created_at,k.entity_key LIMIT 1) AS entity_key FROM entities e'
        ' WHERE e.project_id=? AND e.id=?', (project_id, entity_id)).fetchone()
    if row is None or not row['entity_key'] or row['kind'] not in kinds:
        _bad('关联对象必须是当前项目的主机/域名/IP、Endpoint 或 Finding')
    available = bool(row['is_current'] and row['merged_into'] is None)
    if require_current and not available:
        _bad('关联对象已撤销或已合并，请重新选择有效对象')
    display = normalize.endpoint_display(row['entity_key'])
    if row['kind'] == 'vuln':
        title = conn.execute(
            "SELECT COALESCE((SELECT title FROM finding_triage WHERE project_id=? AND finding_id=?),"
            " (SELECT value FROM observations WHERE project_id=? AND entity_id=? AND semantic_field='title' ORDER BY is_current DESC,id DESC LIMIT 1))",
            (project_id, entity_id, project_id, entity_id)).fetchone()[0]
        display = title or display
    return {**dict(row), 'available': available,
            'display_key': str(normalize.redact_secrets(display))}


def task_detail(conn, project_id, task_id):
    row = conn.execute('SELECT * FROM research_tasks WHERE project_id=? AND id=?',
                       (project_id, task_id)).fetchone()
    if row is None:
        raise query.QueryError('E_TASK_NOT_FOUND', '研究任务不存在于当前项目')
    result = dict(row)
    result['links'] = [_target(conn, project_id, r[0], require_current=False) for r in conn.execute(
        'SELECT entity_id FROM research_task_links WHERE project_id=? AND task_id=? ORDER BY entity_id',
        (project_id, task_id))]
    return result


def tasks(conn, project_id, *, q='', status='', cursor=None, limit=50, entity_id=None):
    from .workspace import _literal, _page
    if len(q) > 300 or (status and status not in (*STATUSES, 'open')):
        _bad('不支持的任务筛选')
    where = ['project_id=?', "(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"]
    args = [project_id, '%' + _literal(q) + '%', '%' + _literal(q) + '%']
    if status == 'open':
        where.append("status IN ('pending','in_progress')")
    elif status:
        where.append('status=?'); args.append(status)
    if entity_id is not None:
        where.append('id IN (SELECT task_id FROM research_task_links WHERE project_id=? AND entity_id=?)')
        args.extend([project_id, entity_id])
    signature = hashlib.sha256(json.dumps([project_id, 'tasks', q, status, entity_id]).encode()).hexdigest()[:20]
    page = _page(conn, 'SELECT id FROM research_tasks WHERE ' + ' AND '.join(where), args,
                 cursor=cursor, signature=signature, limit=limit)
    page['rows'] = [task_detail(conn, project_id, r['id']) for r in page['rows']]
    return page


def save_task(conn, *, project_id, title, description='', status='pending',
              entity_ids=(), task_id=None, version=None, actor='analyst'):
    title = title.strip()
    if not title or len(title) > 300 or len(description) > 10000 or status not in STATUSES:
        _bad('任务标题须为 1–300 字符，描述最多 10000 字符，状态须为 pending/in_progress/done/shelved')
    if len(entity_ids) > 20 or any(type(eid) is not int or eid <= 0 for eid in entity_ids):
        _bad('每项任务最多关联 20 个有效对象')
    with store.atomic(conn):
        before = task_detail(conn, project_id, task_id) if task_id is not None else None
        if before and version != before['version']:
            raise query.QueryError('E_STALE_RESEARCH', '任务已被修改，请重新载入后再保存')
        # Existing withdrawn links may be retained during a status change. New
        # links must point to effective, unmerged targets in the same project.
        previous_ids = {r['id'] for r in before['links']} if before else set()
        for eid in set(entity_ids):
            _target(conn, project_id, eid, require_current=eid not in previous_ids)
        stamp = store.now()
        if before:
            conn.execute('UPDATE research_tasks SET title=?,description=?,status=?,version=version+1,'
                         'updated_at=?,updated_by=? WHERE project_id=? AND id=?',
                         (title, description, status, stamp, actor, project_id, task_id))
            conn.execute('DELETE FROM research_task_links WHERE project_id=? AND task_id=?', (project_id, task_id))
        else:
            task_id = conn.execute(
                'INSERT INTO research_tasks(project_id,title,description,status,created_at,created_by,updated_at,updated_by)'
                ' VALUES(?,?,?,?,?,?,?,?)', (project_id, title, description, status, stamp, actor, stamp, actor)).lastrowid
        conn.executemany('INSERT INTO research_task_links(project_id,task_id,entity_id) VALUES(?,?,?)',
                         [(project_id, task_id, eid) for eid in sorted(set(entity_ids))])
        result = task_detail(conn, project_id, task_id)
        store.log_activity(conn, actor=actor,
                           action='research.task.update' if before else 'research.task.create',
                           target_kind='research_task', target_id=task_id,
                           detail={'project_id': project_id, 'before': before, 'after': result})
        return result


def metadata(conn, project_id, entity_id):
    _target(conn, project_id, entity_id, METADATA_KINDS, require_current=False)
    row = conn.execute('SELECT * FROM entity_research WHERE project_id=? AND entity_id=?',
                       (project_id, entity_id)).fetchone()
    return {**(dict(row) if row else {'criticality': None, 'version': 0}),
            'tags': query.tags_of(conn, project_id, entity_id)}


def save_metadata(conn, *, project_id, entity_id, criticality=None, version=0,
                  add_tags=(), remove_tags=(), actor='analyst'):
    if criticality is not None and (type(criticality) is not int or criticality not in (1, 2, 3)):
        _bad('重要度仅支持 Tier 1、2、3 或未分级')
    if len(add_tags) + len(remove_tags) > 100 or any(
            not isinstance(t, str) or not t.strip() or len(t) > 60 or any(ord(c) < 32 for c in t)
            for t in [*add_tags, *remove_tags]):
        _bad('标签须为 1–60 字符，每次最多修改 100 个')
    with store.atomic(conn):
        _target(conn, project_id, entity_id, METADATA_KINDS)
        before = metadata(conn, project_id, entity_id)
        if version != before['version']:
            raise query.QueryError('E_STALE_RESEARCH', '重要度或标签已被修改，请重新载入后再保存')
        writer = ManualWriter(conn, actor=actor)
        for tag in sorted(set(t.strip() for t in remove_tags)):
            writer.remove_tag(project_id=project_id, entity_id=entity_id, tag=tag)
        for tag in sorted(set(t.strip() for t in add_tags)):
            writer.set_tag(project_id=project_id, entity_id=entity_id, tag=tag)
        conn.execute(
            'INSERT INTO entity_research(project_id,entity_id,criticality,version,updated_at,updated_by)'
            ' VALUES(?,?,?,1,?,?) ON CONFLICT(project_id,entity_id) DO UPDATE SET'
            ' criticality=excluded.criticality,version=entity_research.version+1,'
            ' updated_at=excluded.updated_at,updated_by=excluded.updated_by',
            (project_id, entity_id, criticality, store.now(), actor))
        result = metadata(conn, project_id, entity_id)
        store.log_activity(conn, actor=actor, action='research.metadata.set', target_kind='entity',
                           target_id=entity_id, detail={'project_id': project_id, 'before': before, 'after': result})
        return result


def quick_search(conn, project_id, *, q='', limit=5):
    from .workspace import browse
    from .finding_queries import public_value
    q = q.strip()
    if not q or len(q) > 300 or type(limit) is not int or not 1 <= limit <= 10:
        _bad('请输入 1–300 字符的关键词；每组最多 10 条')
    groups = []
    for category in ('assets', 'api', 'findings', 'evidence', 'notes'):
        page = browse(conn, project_id, category=category, mode='list', q=q, limit=limit)
        groups.append(public_value({'category': category, **page}))
    return {'q': q, 'groups': groups, 'scope': '当前项目；有效资产/接口/发现/笔记与历史证据索引。字面子串检索，不搜索原文正文。'}
