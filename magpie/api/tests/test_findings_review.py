"""HTTP regressions for the reviewed Finding write/report paths."""
from magpie.api.tests.test_routes import HEADERS, client, loaded  # noqa: F401


def test_finding_http_lifecycle_report_and_scope(loaded):
    rows = loaded.get('/api/v1/workspace/browse', headers=HEADERS,
                      params={'project': 'demo', 'category': 'assets', 'mode': 'list'}).json()['data']['rows']
    target = rows[0]['id']
    result = loaded.post('/api/v1/workspace/findings?project=demo', headers=HEADERS, json={
        'title': 'HTTP 人工发现', 'severity': 'high', 'target_id': target,
        'reason': '人工复核依据', 'description': 'password=private-review-value',
    })
    assert result.status_code == 200, result.text
    fid = result.json()['data']['finding_id']
    base = f'/api/v1/workspace/findings/{fid}'
    version = result.json()['data']['version']
    invalid = loaded.post(base + '/triage?project=demo', headers=HEADERS, json={'status': 'duplicate', 'expected_version': version, 'reason': '重复测试'})
    assert invalid.status_code == 400
    retest = loaded.post(base + '/retest?project=demo', headers=HEADERS,
                         json={'status': 'resolved', 'notes': '人工修复验证 password=private-retest-value', 'expected_version': version})
    assert retest.status_code == 200, retest.text
    detail = loaded.get(base, headers=HEADERS, params={'project': 'demo'}).json()['data']
    assert detail['status'] == 'resolved'
    assert len(detail['retests']) == 1
    assert 'private-retest-value' not in str(detail)
    report = loaded.get('/api/v1/workspace/report', headers=HEADERS,
                         params={'project': 'demo', 'format': 'markdown'})
    assert report.status_code == 200 and 'text/markdown' in report.headers['content-type']
    assert 'HTTP 人工发现' in report.text and '人工修复验证' in report.text
    assert 'private-review-value' not in report.text
    loaded.post('/api/v1/projects', headers=HEADERS, json={'name': 'other', 'scope': {}})
    assert loaded.get(base, headers=HEADERS, params={'project': 'other'}).status_code == 404
    assert loaded.post(base + '/retest?project=other', headers=HEADERS,
                        json={'status': 'resolved', 'notes': '跨项目写入', 'expected_version': version}).status_code == 400
    assert loaded.post('/api/v1/workspace/findings?project=other', headers=HEADERS,
                        json={'title': '跨项目', 'severity': 'high', 'target_id': target}).status_code == 400


def test_version_required_conflict_and_reason_guards(loaded):
    rows = loaded.get('/api/v1/workspace/browse', headers=HEADERS,
                      params={'project':'demo','category':'assets','mode':'list'}).json()['data']['rows']
    payload = {'title':'版本检查', 'severity':'high', 'target_id':rows[0]['id']}
    invalid = loaded.post('/api/v1/workspace/findings?project=demo', headers=HEADERS, json={**payload, 'status':'confirmed'})
    assert invalid.status_code == 400
    created = loaded.post('/api/v1/workspace/findings?project=demo', headers=HEADERS, json=payload).json()['data']
    base = f"/api/v1/workspace/findings/{created['finding_id']}"
    assert created['status'] == 'candidate' and created['version'] == 1
    assert loaded.post(base + '/triage?project=demo', headers=HEADERS, json={'status':'candidate'}).status_code == 422
    for bad in [None, -1, True, '1']:
        assert loaded.post(base + '/retest?project=demo', headers=HEADERS,
                           json={'expected_version':bad,'status':'resolved','notes':'说明'}).status_code == 422
    updated = loaded.post(base + '/triage?project=demo', headers=HEADERS,
                          json={'expected_version':1,'status':'confirmed','reason':'人工核实'}).json()['data']
    assert updated['version'] == 2
    for suffix, body in [('triage', {'status':'candidate'}), ('retest', {'status':'resolved','notes':'旧窗口'})]:
        result = loaded.post(base + '/' + suffix + '?project=demo', headers=HEADERS, json={**body, 'expected_version':1})
        assert result.status_code == 409 and result.json()['error']['code'] == 'E_STALE_FINDING'
    detail = loaded.get(base, headers=HEADERS, params={'project':'demo'}).json()['data']
    assert detail['status'] == 'confirmed' and detail['triage']['version'] == 2 and detail['retests'] == []
