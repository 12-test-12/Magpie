import time

from fastapi.testclient import TestClient

from magpie.api.app import create_app


def test_preview_mapping_import_profile_and_undo(tmp_path):
    app = create_app(db_path=tmp_path / 'test.db', data_dir=tmp_path / 'data', token='test')
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        client.post('/api/v1/projects', headers=headers, json={"name": "demo", "scope": {}})
        file = tmp_path / 'custom.csv'
        file.write_text('destination,code\nhttps://a.example.com/api,200\n', encoding='utf-8')
        body = {"path": str(file), "project": "demo", "mapping": {"$.destination": "url", "$.code": "status"}}
        assert client.post('/api/v1/import-preview', json=body).status_code == 401
        preview = client.post('/api/v1/import-preview', headers=headers, json=body).json()['data']
        assert preview['mapped_complete'] == 1
        assert client.get('/api/v1/entities?project=demo', headers=headers).json()['data'] == []
        saved = client.post('/api/v1/import-profiles', headers=headers, json={
            "project": "demo", "name": "custom", "structure_signature": preview['structure_signature'],
            "mapping": body['mapping']})
        assert saved.status_code == 200
        assert len(client.get('/api/v1/import-profiles?project=demo', headers=headers).json()['data']) == 1
        response = client.post('/api/v1/imports', headers=headers, json={"project": "demo",
            "paths": [str(file)], "parser": "generic", "mapping": body['mapping'],
            "expected_sha256": preview['sha256']})
        assert response.status_code == 202
        import_id = response.json()['data']['import_id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            report = client.get(f'/api/v1/imports/{import_id}', headers=headers).json()['data']
            if report['state'] in ('done', 'failed', 'cancelled'):
                break
            time.sleep(.02)
        assert report['state'] == 'done', report
        assert report['report']['files'][0]['import_coverage']['mapped_complete'] == 1
        job_id = report['job_ids'][0]
        persisted = client.get(f'/api/v1/jobs/{job_id}/import-report', headers=headers)
        assert persisted.status_code == 200
        assert persisted.json()['data']['import_coverage']['fields_ignored'] == 0
        assert client.post(f'/api/v1/imports/{import_id}/undo', headers=headers).status_code == 200
        file.write_text('changed', encoding='utf-8')
        changed = client.post('/api/v1/imports', headers=headers, json={"project": "demo",
            "paths": [str(file)], "parser": "generic", "expected_sha256": preview['sha256']})
        assert changed.status_code == 409

    restarted = create_app(db_path=tmp_path / 'test.db', data_dir=tmp_path / 'data', token='test')
    with TestClient(restarted) as client:
        assert client.get(f'/api/v1/jobs/{job_id}/import-report', headers=headers).status_code == 200
        assert len(client.get('/api/v1/import-profiles?project=demo', headers=headers).json()['data']) == 1
