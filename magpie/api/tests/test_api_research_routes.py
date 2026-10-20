"""HTTP contract for read-only API research conditions."""
from bench.api_research_demo import inputs
from magpie.api.tests.test_routes import HEADERS, client  # noqa: F401
from magpie.core import ingest, ops


def test_api_research_conditions_auth_validation_and_project_scope(client, tmp_path):
    files = inputs(tmp_path / 'samples')
    def seed(conn):
        pid = ops.create_project(conn, 'research', {})
        ops.create_project(conn, 'empty', {})
        for path in files:
            ingest.ingest_file(conn, ingest.IngestOptions(project_id=pid, data_dir=tmp_path), path)
    client.app.state.ctx.writer.submit('synthetic research fixture', seed).result(timeout=30)
    url = '/api/v1/workspace/browse'
    filters = dict(project='research',category='api',mode='list',parameter_name='redirect',
                   parameter_location='query',response_status=200,response_mime='application/json',credential='cookie')
    result = client.get(url, headers=HEADERS, params=filters)
    assert result.status_code == 200, result.text
    assert result.json()['data']['total'] == 1
    assert client.get(url, headers=HEADERS, params={**filters,'credential':'bearer'}).json()['data']['total'] == 0
    assert client.get(url, headers=HEADERS, params={**filters,'project':'empty'}).json()['data']['total'] == 0
    assert client.get(url, params=filters).status_code == 401
    assert client.get(url, headers=HEADERS, params={**filters,'response_status':'not-a-number'}).status_code == 422
    assert client.get(url, headers=HEADERS, params={**filters,'category':'assets'}).status_code == 400
    assert client.get(url, headers=HEADERS, params={**filters,'credential':'no_auth'}).status_code == 400
