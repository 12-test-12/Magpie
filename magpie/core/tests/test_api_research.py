"""Research conditions refer to evidence from the same request, never invented joins."""
import json
from pathlib import Path

import pytest

from magpie.core import api_research, ingest, normalize, ops, query, store, workspace


@pytest.fixture
def research_db(tmp_path):
    conn = store.connect(':memory:')
    store.init_db(conn)
    pid = ops.create_project(conn, 'api-research', {})
    jobs = []
    fixture = Path(__file__).resolve().parents[3] / 'bench' / 'e2e' / 'api-research'
    for path in (fixture / name for name in ('requests.har', 'app.manifest.json', 'api.openapi.json')):
        result = ingest.ingest_file(conn, ingest.IngestOptions(project_id=pid, data_dir=tmp_path, verify=True), path)
        assert result['state'] in ('done', 'partial'), result
        jobs.append(result['job_id'])
    yield conn, pid, jobs
    conn.close()


def page(db, **filters):
    conn, pid, _ = db
    return workspace.browse(conn, pid, category='api', mode='list', **filters)


def paths(result):
    return {normalize.decode_endpoint_key(r['entity_key'])['path'] for r in result['rows'] if r['kind'] == 'endpoint'}


def test_same_request_conditions_and_exact_parameter_names(research_db):
    assert paths(page(research_db, q='/api/research', response_status=200, credential='cookie',
                      parameter_name='redirect', parameter_location='query')) == {'/api/research'}
    assert page(research_db, q='/api/research', response_status=200, credential='bearer')['total'] == 0
    assert page(research_db, q='/api/research', response_status=401, parameter_name='redirect')['total'] == 0
    assert page(research_db, q='/api/research', response_status=401, credential='bearer')['total'] == 1
    assert page(research_db, q='/api/research', response_mime='APPLICATION/JSON', response_status=200)['total'] == 1
    assert page(research_db, parameter_name='key_%')['total'] == 1
    assert page(research_db, parameter_name='key_')['total'] == 0
    assert page(research_db, parameter_name='Redirect')['total'] == 0
    assert paths(page(research_db, parameter_name='X-Trace', parameter_location='header')) == {'/api/declared'}
    assert page(research_db, parameter_name='X-Trace', response_status=200)['total'] == 0


def test_origins_groups_cursors_and_project_isolation(research_db):
    conn, pid, _ = research_db
    assert paths(page(research_db, api_origin='js_only')) == {'/api/js-only'}
    assert paths(page(research_db, api_origin='declaration_only')) == {'/api/declared'}
    assert page(research_db, api_origin='declaration_only')['total'] == 2  # Endpoint + existing apischema
    assert '/api/mixed' in paths(page(research_db, api_origin='http_sample'))
    assert '/api/mixed' not in paths(page(research_db, api_origin='js_only'))
    filters = dict(parameter_name='redirect', parameter_location='query')
    group = workspace.browse(conn, pid, category='api', **filters)
    listing = page(research_db, **filters)
    assert group['entity_total'] == listing['total'] == sum(r['count'] for r in group['rows'])
    seen, cursor = [], None
    while True:
        result = page(research_db, limit=1, cursor=cursor, **filters)
        seen.extend(r['id'] for r in result['rows'])
        cursor = result['next_cursor']
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == listing['total']
    first = page(research_db, limit=1, **filters)
    for key, value in dict(parameter_name='other', parameter_location='body', response_status=200,
                           response_mime='text/plain', credential='bearer', api_origin='http_sample').items():
        with pytest.raises(query.QueryError, match='游标'):
            page(research_db, limit=1, cursor=first['next_cursor'], **{**filters, key: value})
    empty = ops.create_project(conn, 'empty', {})
    assert workspace.browse(conn, empty, category='api', mode='list', **filters)['total'] == 0


def test_read_only_effective_facts_undo_redo(research_db):
    conn, _, jobs = research_db
    before = list(conn.iterdump())
    page(research_db, response_status=200, api_origin='http_sample')
    assert list(conn.iterdump()) == before
    ops.undo(conn, jobs[0])
    assert page(research_db, api_origin='http_sample')['total'] == 0
    assert '/api/mixed' in paths(page(research_db, api_origin='js_only'))
    ops.redo(conn, jobs[0])
    assert '/api/mixed' in paths(page(research_db, api_origin='http_sample'))


@pytest.mark.parametrize('value', ['broken', '[]', '{}', '{"version":true}', '{"version":1}', '{"version":2}',
                                  'x' * 8001, '[' * 1500 + '0' + ']' * 1500])
def test_unknown_samples_do_not_become_request_facts(value):
    assert api_research.sample_value(value) is None
    assert api_research._sample_matches(value, '', '', None, '', 'unknown') == 0


def test_missing_and_malformed_fields_do_not_match():
    base = {'version':1,'url':'https://api.example.test/api','method':'POST','record_path':'$.log.entries[0]'}
    assert api_research._sample_matches(json.dumps(base), '', '', None, '', 'unknown') == 0
    assert api_research._sample_matches(json.dumps({**base, 'status': True}), '', '', 1, '', '') == 0
    assert api_research._sample_matches(json.dumps({**base, 'credentials': 'bearer'}), '', '', None, '', 'bearer') == 0
    assert api_research._parameter_matches('param.query.redirect.type', 'string', 'redirect', '') == 0


@pytest.mark.parametrize('filters', [dict(response_status=99), dict(response_status=True),
    dict(response_status=600), dict(response_mime='json'), dict(credential='no_auth'),
    dict(api_origin='unknown'), dict(parameter_location='file'), dict(parameter_name='x'*121)])
def test_invalid_filters_rejected(research_db, filters):
    with pytest.raises(query.QueryError):
        page(research_db, **filters)


def test_api_filters_rejected_for_other_categories(research_db):
    conn, pid, _ = research_db
    for category in ('assets', 'js', 'notes', 'evidence', 'findings'):
        with pytest.raises(query.QueryError):
            workspace.browse(conn, pid, category=category, mode='list', response_status=200)


def test_unknown_http_sample_is_not_misreported_as_js_only(research_db):
    conn, pid, _ = research_db
    raw = ops.add_raw_file(conn, project_id=pid, locator_uri='synthetic://unknown-sample', content='unknown sample format')
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=raw, locator={'record':1}, snippet='unknown sample format')
    source = store.register_source(conn, name='unknown sample format')
    job = ops.start_job(conn, project_id=pid, kind='scan', coverage='partial')
    key = normalize.endpoint_key('https://api.example.test/api/js-only', method='POST')
    ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev, source_id=source,
                        kind='endpoint', entity_key=key, semantic_field='http_request_sample', value='{"version":999}')
    ops.finish_job(conn, job, 'done'); store.recompute(conn)
    assert page(research_db, q='/api/js-only')['total'] == 1
    assert page(research_db, q='/api/js-only', api_origin='http_sample')['total'] == 0
    assert page(research_db, q='/api/js-only', api_origin='js_only')['total'] == 0
    assert conn.execute("SELECT value FROM observations WHERE semantic_field='http_request_sample' AND value LIKE '%999%'").fetchone()


def test_sample_origin_and_method_cannot_borrow_parameters_or_status(research_db):
    conn, pid, _ = research_db
    raw = ops.add_raw_file(conn, project_id=pid, locator_uri='synthetic://legacy-requests', content='synthetic legacy')
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=raw, locator={'record':1}, snippet='synthetic legacy')
    source = store.register_source(conn, name='synthetic legacy')
    job = ops.start_job(conn, project_id=pid, kind='scan', coverage='partial')
    def add(key, field, value):
        ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev, source_id=source,
                            kind='endpoint', entity_key=key, semantic_field=field, value=value)
    legacy = 'api.example.test/legacy/{id}'
    for method, status in [('GET',200),('POST',401)]:
        add(legacy, 'method', method)
        add(legacy, 'http_request_sample', json.dumps({'version':1,'url':'https://api.example.test/legacy/1',
            'method':method,'status':status,'record_path':'$.entries[0]','query_parameter_names':['redirect']}))
    add(normalize.endpoint_key('https://api.example.test/api/mixed', method='POST'),
        'js_static_param', 'js_only_param')
    ops.finish_job(conn, job, 'done'); store.recompute(conn)
    assert page(research_db, q='legacy', method='POST', response_status=200)['total'] == 0
    assert page(research_db, q='legacy', method='GET', response_status=200)['total'] == 1
    assert page(research_db, parameter_name='js_only_param')['total'] == 1
    assert page(research_db, parameter_name='js_only_param', api_origin='http_sample')['total'] == 0
