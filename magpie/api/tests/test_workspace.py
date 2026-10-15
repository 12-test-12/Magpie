"""Workspace read models: filtering, scoping, effective facts and bounded previews."""
from __future__ import annotations

import hashlib

import pytest

from magpie.api.tests.test_routes import HEADERS, client, loaded  # noqa: F401
from magpie.core import ops, query, store, workspace


def get(client, path, **params):
    return client.get('/api/v1/workspace/' + path, headers=HEADERS,
                      params={'project': 'demo', **params})


def test_summary_and_cursor_filter_parity(loaded):
    summary = get(loaded, 'summary').json()['data']
    assert summary['assets'] > 0 and summary['evidence'] > 0
    groups = get(loaded, 'browse', limit=1).json()['data']
    assert len(groups['rows']) == 1 and groups['next_cursor']
    seen = [groups['rows'][0]['group_key']]
    cursor = groups['next_cursor']
    while cursor:
        page = get(loaded, 'browse', limit=1, cursor=cursor).json()['data']
        seen.extend(row['group_key'] for row in page['rows'])
        cursor = page['next_cursor']
    assert len(seen) == len(set(seen)) == groups['total']
    host = groups['rows'][0]['label']
    rows = get(loaded, 'browse', host=host, mode='list').json()['data']
    assert rows['total'] == groups['rows'][0]['count']
    assert get(loaded, 'browse', cursor=groups['next_cursor'], q='different').status_code == 400
    assert get(loaded, 'browse', cursor='bad').status_code == 400
    assert get(loaded, 'browse', cursor='e30=').status_code == 400
    assert get(loaded, 'browse', limit=101).status_code == 400


def test_project_isolation_and_auth(loaded):
    loaded.post('/api/v1/projects', headers=HEADERS, json={'name': 'empty', 'scope': {}})
    assert get(loaded, 'summary', project='empty').json()['data']['assets'] == 0
    rows = get(loaded, 'browse', mode='list').json()['data']['rows']
    assert get(loaded, f"entities/{rows[0]['id']}", project='empty').status_code == 404
    evidence = get(loaded, 'browse', category='evidence', mode='list').json()['data']['rows'][0]
    assert get(loaded, f"evidence/{evidence['id']}", project='empty').status_code == 404
    assert loaded.get('/api/v1/workspace/summary').status_code == 401


@pytest.mark.parametrize('params', [dict(category='unknown'), dict(mode='unknown'),
    dict(category='notes', mode='host'), dict(category='evidence', mode='list', method='GET')])
def test_reject_unsupported(loaded, params):
    assert get(loaded, 'browse', **params).status_code == 400


def test_details_and_archived_preview(loaded):
    rows = get(loaded, 'browse', mode='list').json()['data']['rows']
    entity = rows[0]['id']
    detail = get(loaded, f'entities/{entity}', limit=1).json()['data']
    assert len(detail['rows']) <= 1
    evidence = get(loaded, f'entities/{entity}', section='evidence').json()['data']
    assert evidence['rows']
    preview = get(loaded, f"evidence/{evidence['rows'][0]['id']}").json()['data']
    assert preview['sha256'] and preview['contributions']
    assert preview['preview_state'] == 'available', preview
    assert len(preview['preview']) <= 8000


def test_core_effective_methods_notes_undo_and_unsafe_preview(tmp_path):
    conn = store.connect(':memory:'); store.init_db(conn)
    project = ops.create_project(conn, 'demo', {})
    store.register_source(conn, source_id=1, name='sample', authority_level='verified')
    raw = ops.add_raw_file(conn, project_id=project, locator_uri=(tmp_path / 'private.txt').as_uri(), content='private')
    ev = ops.add_evidence(conn, project_id=project, raw_file_id=raw, locator={'line': 1}, snippet='private')
    job = ops.start_job(conn, project_id=project, kind='scan', coverage='partial', actor='test')
    for key, method in [('a.example.com/api/v1/one', 'GET'), ('a.example.com/api/v1/two', 'POST'), ('b.example.com/static/app.js', 'GET')]:
        ops.add_observation(conn, project_id=project, job_id=job, evidence_id=ev, source_id=1,
                            entity_key=key, kind='endpoint', semantic_field='method', value=method,
                            observed_at='2026-10-15T00:00:00Z')
    ops.finish_job(conn, job, 'done'); store.recompute(conn)
    assert workspace.browse(conn, project, category='api', mode='list', method='GET', source='sample')['total'] == 2
    assert workspace.browse(conn, project, category='api', mode='list', q='%')['total'] == 0
    groups = workspace.browse(conn, project, category='api', mode='path', host='a.example.com')
    assert groups['rows'][0]['label'] == '/api/v1'
    assert groups['rows'][0]['count'] == 2
    assert workspace.browse(conn, project, category='js', mode='list')['total'] == 1
    assert workspace.evidence_detail(conn, project, ev, tmp_path)['preview'] is None
    entity = store.entity_for_key(conn, project, 'a.example.com/api/v1/one')
    ops.add_finding(conn, project_id=project, entity_id=entity, text='Follow the original evidence', actor='test')
    assert workspace.browse(conn, project, category='notes', mode='list')['total'] == 1
    ops.undo(conn, job)
    assert workspace.browse(conn, project, category='api', mode='list', method='POST')['total'] == 0
    ops.redo(conn, job)
    assert workspace.browse(conn, project, category='api', mode='list', method='POST')['total'] == 1
    conn.close()


def test_archive_hash_mismatch_and_redaction(tmp_path):
    conn = store.connect(':memory:'); store.init_db(conn)
    project = ops.create_project(conn, 'demo', {})
    content = b'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n'
    sha = hashlib.sha256(content).hexdigest()
    path = tmp_path / 'raw' / sha[:2] / sha
    path.parent.mkdir(parents=True); path.write_bytes(content)
    raw = ops.add_raw_file(conn, project_id=project, locator_uri=path.as_uri(), content=content.decode())
    ev = ops.add_evidence(conn, project_id=project, raw_file_id=raw, locator={'line': 1}, snippet=content.decode())
    result = workspace.evidence_detail(conn, project, ev, tmp_path)
    assert result['preview_state'] == 'available'
    assert 'abcdefghijklmnopqrstuvwxyz123456' not in result['preview']
    path.write_text('changed', encoding='utf-8')
    assert workspace.evidence_detail(conn, project, ev, tmp_path)['preview'] is None
    conn.close()
