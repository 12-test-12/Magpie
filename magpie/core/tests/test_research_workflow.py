from __future__ import annotations

import json

import pytest

from magpie.core import migrate, normalize, ops, query, research, store, workspace
from magpie.core.tests.test_findings_lifecycle import seed_host


@pytest.fixture
def db():
    conn = store.connect(':memory:'); store.init_db(conn)
    pid = ops.create_project(conn, 'research', {})
    host = seed_host(conn, pid)
    yield conn, pid, host
    conn.close()


def test_tasks_states_links_pagination_and_stale_writes(db):
    conn, pid, host = db
    finding = ops.create_manual_finding(conn, project_id=pid, title='人工发现', severity='info', target_entity_id=host)
    first = research.save_task(conn, project_id=pid, title='待申请账号', entity_ids=[host, finding['finding_id']])
    for i in range(4):
        research.save_task(conn, project_id=pid, title=f'任务 {i}', status=research.STATUSES[i])
    page = research.tasks(conn, pid, limit=2)
    ids = []
    while True:
        ids.extend(r['id'] for r in page['rows'])
        if not page['next_cursor']: break
        page = research.tasks(conn, pid, limit=2, cursor=page['next_cursor'])
    assert len(ids) == len(set(ids)) == 5
    assert research.tasks(conn, pid, entity_id=host)['total'] == 1
    for status in research.STATUSES:
        first = research.save_task(conn, project_id=pid, task_id=first['id'], version=first['version'],
                                   title=first['title'], status=status, entity_ids=[host])
        assert research.tasks(conn, pid, status=status)['rows'][0]['status'] == status
    with pytest.raises(query.QueryError, match='已被修改'):
        research.save_task(conn, project_id=pid, task_id=first['id'], version=1, title='旧窗口')
    assert research.task_detail(conn, pid, first['id'])['title'] == '待申请账号'
    assert len(list(conn.execute("SELECT * FROM activity_log WHERE action='research.task.update'"))) == 4
    store.assert_integrity(conn)


def test_tasks_project_guards_atomicity_and_literal_search(db):
    conn, pid, host = db
    other = ops.create_project(conn, 'other', {})
    task = research.save_task(conn, project_id=pid, title='100%_manual', entity_ids=[host])
    cursor = research.tasks(conn, pid, limit=1)['next_cursor']
    assert cursor is None
    research.save_task(conn, project_id=pid, title='second')
    cursor = research.tasks(conn, pid, limit=1)['next_cursor']
    with pytest.raises(query.QueryError): research.tasks(conn, other, cursor=cursor)
    with pytest.raises(query.QueryError): research.tasks(conn, pid, cursor=cursor, status='done')
    assert research.tasks(conn, pid, q='%_')['total'] == 1
    with pytest.raises(query.QueryError): research.save_task(conn, project_id=other, title='bad link', entity_ids=[host])
    with pytest.raises(query.QueryError): research.task_detail(conn, other, task['id'])
    with pytest.raises(query.QueryError): research.save_task(conn, project_id=pid, title=' ', entity_ids=[])
    with pytest.raises(query.QueryError): research.save_task(conn, project_id=pid, title='bad', status='running')
    conn.execute('BEGIN')
    research.save_task(conn, project_id=pid, title='outer rollback')
    conn.rollback()
    assert research.tasks(conn, pid)['total'] == 2
    assert conn.execute('SELECT COUNT(*) FROM research_task_links').fetchone()[0] == 1
    ops.delete_project(conn, pid)
    store.assert_integrity(conn)
    assert conn.execute('SELECT COUNT(*) FROM research_tasks').fetchone()[0] == 0


def test_metadata_reuses_tags_group_count_parity_and_clear(db):
    conn, pid, host = db
    saved = research.save_metadata(conn, project_id=pid, entity_id=host, add_tags=['core-system', 'payment'], criticality=1)
    assert saved['tags'] == query.tags_of(conn, pid, host) == ['core-system', 'payment']
    assert workspace.summary(conn, pid)['tier1_assets'] == 1
    groups = workspace.browse(conn, pid, tag='payment', criticality=1)
    rows = workspace.browse(conn, pid, mode='list', tag='payment', criticality=1)
    assert groups['entity_total'] == rows['total'] == 1
    assert rows['rows'][0]['tags'] == saved['tags']
    assert rows['rows'][0]['criticality'] == 1
    with pytest.raises(query.QueryError): research.save_metadata(conn, project_id=pid, entity_id=host, version=0)
    conn.execute('BEGIN')
    research.save_metadata(conn, project_id=pid, entity_id=host, version=saved['version'], remove_tags=['payment'], criticality=3)
    conn.rollback()
    assert research.metadata(conn, pid, host) == saved
    research.save_metadata(conn, project_id=pid, entity_id=host, version=saved['version'], remove_tags=saved['tags'])
    assert not query.tags_of(conn, pid, host)
    assert workspace.browse(conn, pid, tag='payment')['total'] == 0
    assert workspace.summary(conn, pid)['tier1_assets'] == 0
    with pytest.raises(query.QueryError): research.save_metadata(conn, project_id=pid, entity_id=host, criticality=4)
    with pytest.raises(query.QueryError): research.save_metadata(conn, project_id=pid, entity_id=host, add_tags=[' '])


def test_quick_search_groups_original_filename_title_note_and_undo(db):
    conn, pid, host = db
    finding = ops.create_manual_finding(conn, project_id=pid, title='portal 中文发现', severity='info', target_entity_id=host)
    ops.add_finding(conn, project_id=pid, entity_id=host, text='portal 研究笔记')
    job = ops.start_job(conn, project_id=pid, kind='scan', coverage='partial', actor='test')
    conn.execute('UPDATE jobs SET coverage_scope=? WHERE id=?', (json.dumps({'file':'portal-original.har'}), job))
    raw = ops.add_raw_file(conn, project_id=pid, locator_uri='file:///raw/abcdef', content='portal synthetic')
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=raw, locator={'line':1}, snippet='portal')
    key = normalize.endpoint_identity('https://portal.example.test/API/Upload', method='POST')['key']
    ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev, source_id=1,
                        kind='endpoint', entity_key=key, semantic_field='method', value='POST')
    ops.finish_job(conn, job, 'done'); store.recompute(conn)
    endpoint = store.entity_for_key(conn, pid, key)
    task = research.save_task(conn, project_id=pid, title='验证 portal', entity_ids=[endpoint])
    result = {r['category']: r for r in research.quick_search(conn, pid, q='portal')['groups']}
    assert set(result) == {'assets','api','findings','evidence','notes'}
    assert result['api']['rows'][0]['display_key'] == 'POST https://portal.example.test:443/API/Upload'
    assert result['findings']['rows'][0]['title'] == 'portal 中文发现'
    assert result['notes']['rows'][0]['entity_id'] == host
    assert result['evidence']['rows'][0]['source_names'] == ['portal-original.har']
    assert all(g['total'] == 0 for g in research.quick_search(conn, pid, q='%')['groups'])
    ops.undo(conn, job)
    assert not research.task_detail(conn, pid, task['id'])['links'][0]['available']
    assert workspace.browse(conn, pid, category='api', mode='list')['total'] == 0
    # A task may still be completed after its original source is withdrawn.
    research.save_task(conn, project_id=pid, task_id=task['id'], version=task['version'], title=task['title'], status='shelved', entity_ids=[endpoint])
    assert research.quick_search(conn, pid, q='portal-original')['groups'][3]['total'] == 1
    ops.redo(conn, job)
    assert research.task_detail(conn, pid, task['id'])['links'][0]['available']
    store.assert_integrity(conn)


def test_v17_migration_preserves_facts_and_is_idempotent(monkeypatch):
    conn = store.connect(':memory:')
    all_migrations = migrate.MIGRATIONS
    monkeypatch.setattr(migrate, 'MIGRATIONS', tuple(m for m in all_migrations if m.version <= 17))
    store.init_db(conn)
    pid = ops.create_project(conn, 'legacy', {})
    seed_host(conn, pid)
    tables = [*store.FACT_TABLES, *store.CACHE_TABLES, 'jobs', 'entity_tags']
    before = {t:[tuple(r) for r in conn.execute('SELECT * FROM ' + t)] for t in tables}
    monkeypatch.setattr(migrate, 'MIGRATIONS', all_migrations)
    assert migrate.migrate(conn)[0]['version'] == 18
    assert migrate.migrate(conn) == []
    assert before == {t:[tuple(r) for r in conn.execute('SELECT * FROM ' + t)] for t in tables}
    store.assert_integrity(conn)
    conn.close()


def test_task_keeps_the_original_target_after_identity_merge(db):
    conn, pid, host = db
    other = seed_host(conn, pid, key='other.example.test')
    task = research.save_task(conn, project_id=pid, title='保留原始关联', entity_ids=[other])
    merged = ops.merge(conn, project_id=pid, survivor_key='app.example.test', member_keys=['other.example.test'])
    linked = research.task_detail(conn, pid, task['id'])['links'][0]
    assert linked['id'] == other and not linked['available']
    assert linked['display_key'] == 'other.example.test'
    research.save_task(conn, project_id=pid, task_id=task['id'], version=task['version'], title=task['title'], status='shelved', entity_ids=[other])
    ops.unmerge(conn, event_id=merged['event_id'])
    assert research.task_detail(conn, pid, task['id'])['links'][0]['available']
    store.assert_integrity(conn)
