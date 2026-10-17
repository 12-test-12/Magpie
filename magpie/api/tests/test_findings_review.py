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
    invalid = loaded.post(base + '/triage?project=demo', headers=HEADERS, json={'status': 'duplicate'})
    assert invalid.status_code == 400
    retest = loaded.post(base + '/retest?project=demo', headers=HEADERS,
                         json={'status': 'resolved', 'notes': '人工修复验证 password=private-retest-value'})
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
                        json={'status': 'resolved', 'notes': '跨项目写入'}).status_code == 400
    assert loaded.post('/api/v1/workspace/findings?project=other', headers=HEADERS,
                        json={'title': '跨项目', 'severity': 'high', 'target_id': target}).status_code == 400
