"""Bounded note reads must not issue a detail/evidence query for every row."""
import pytest

from magpie.core import notes, ops, query, store
from magpie.core.tests.test_findings_lifecycle import seed_host


def test_batch_matches_details_and_query_budget_through_pagination_and_undo():
    conn = store.connect(':memory:')
    store.init_db(conn)
    pid = ops.create_project(conn, 'batch-synthetic', {})
    other = ops.create_project(conn, 'other', {})
    host = seed_host(conn, pid)
    created = [notes.create(conn, project_id=pid, entity_id=host, text=f'原始笔记 {i}') for i in range(51)]
    evidence = [notes.detail(conn, pid, n['observation_id'])['evidence_id'] for n in created[:3]]
    for index, n in enumerate(created[:3]):
        notes.revise(conn, project_id=pid, note_id=n['observation_id'], text=f'修订 {index}',
                     expected_version=0, reason='合成修订', evidence_ids=evidence, archived=index == 2)
    before = list(conn.iterdump())
    queries = []
    conn.set_trace_callback(queries.append)
    page = notes.browse(conn, pid, archived='all', limit=50)
    conn.set_trace_callback(None)
    assert len(page['rows']) == 50 and page['total'] == 51
    assert sum(sql.lstrip().upper().startswith('SELECT') for sql in queries) == 4
    assert page['rows'] == [notes.detail(conn, pid, n['observation_id']) for n in created[:50]]
    assert list(conn.iterdump()) == before
    last = notes.browse(conn, pid, archived='all', limit=50, cursor=page['next_cursor'])
    assert [r['id'] for r in last['rows']] == [created[-1]['observation_id']]
    assert not last['next_cursor']
    assert notes.browse(conn, other, archived='all')['total'] == 0
    with pytest.raises(query.QueryError):
        notes.detail(conn, other, created[0]['observation_id'])
    assert notes.browse(conn, pid, archived='archived')['rows'][0]['version'] == 1
    assert notes.browse(conn, pid, evidence_id=evidence[0])['total'] == 2
    assert notes.browse(conn, pid, q='修订 0')['total'] == 1
    ops.undo(conn, created[0]['job_id'])
    assert not notes.detail(conn, pid, created[0]['observation_id'])['available']
    assert notes.browse(conn, pid, q='修订 0')['rows'] == []
    ops.redo(conn, created[0]['job_id'])
    assert notes.browse(conn, pid, q='修订 0')['rows'][0] == notes.detail(conn, pid, created[0]['observation_id'])
    store.assert_integrity(conn)
    conn.close()
