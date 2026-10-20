"""Synthetic API filters and cross-page sample comparison; never overwrites a DB."""
from __future__ import annotations

import json
from pathlib import Path

from magpie.core import ingest, ops, store

ROOT = Path(__file__).resolve().parents[1]


def inputs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(24):
        query = 'redirect' if index == 0 else 'user_id'
        entries.append({
            'startedDateTime': f'2026-10-20T01:00:{index:02d}Z', 'time': 1,
            'request': {'url': f'https://api.example.test/api/research?{query}=synthetic',
                        'method': 'POST', 'headers': [{'name': 'Cookie' if index == 0 else 'Authorization',
                            'value': 'session=synthetic' if index == 0 else 'Bearer synthetic-token'}],
                        'queryString': [{'name': query, 'value': 'synthetic'}],
                        'postData': {'mimeType': 'application/json', 'text': '{"item_id":1}'}},
            'response': {'status': 200 if index == 0 else 401 if index == 1 else 204,
                         'headers': [], 'content': {'mimeType': 'application/json; charset=utf-8' if index == 0 else 'text/plain', 'size': 12 + index}},
        })
    for path, name in [('mixed', 'redirect'), ('literal', 'key_%')]:
        entries.append({'startedDateTime': '2026-10-20T01:01:00Z', 'time': 1,
            'request': {'url': f'https://api.example.test/api/{path}', 'method': 'POST',
                        'headers': [], 'queryString': [{'name': name, 'value': 'synthetic'}],
                        'postData': {'mimeType': 'application/json', 'text': '{}'}},
            'response': {'status': 200, 'headers': [], 'content': {'mimeType': 'application/json', 'size': 2}}})
    docs = {
        'requests.har': {'log': {'version': '1.2', 'entries': entries}},
        'app.manifest.json': {'magpie_js': 1, 'url': 'https://static.example.test/app.js',
            'content': "axios.post('https://api.example.test/api/js-only?redirect=x');axios.post('https://api.example.test/api/mixed');"},
        'api.openapi.json': {'openapi': '3.0.0', 'info': {'title': 'Synthetic API', 'version': '1'},
            'servers': [{'url': 'https://api.example.test'}],
            'paths': {'/api/declared': {'post': {'parameters': [
                {'name': 'redirect', 'in': 'query', 'schema': {'type': 'string'}},
                {'name': 'X-Trace', 'in': 'header', 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'declaration only'}}}}}},
    }
    paths = []
    for name, doc in docs.items():
        path = directory / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        paths.append(path)
    return paths


def build(workspace='api-research-20261020'):
    data = ROOT / '.magpie-dev' / workspace
    if (data / 'magpie.db').exists():
        raise SystemExit('Synthetic database already exists; refusing to overwrite')
    data.mkdir(parents=True, exist_ok=True)
    conn = store.connect(data / 'magpie.db')
    try:
        store.init_db(conn)
        project = ops.create_project(conn, 'API 研究筛选 · 合成验收', {'include': ['*.example.test']})
        ops.create_project(conn, 'API 筛选空项目', {})
        for path in inputs(data / 'samples'):
            result = ingest.ingest_file(conn, ingest.IngestOptions(
                project_id=project, data_dir=data, actor='synthetic-api-research', verify=True), path)
            if result.get('state') not in ('done', 'partial'):
                raise RuntimeError(result)
        store.recompute(conn)
        store.assert_integrity(conn)
        print(f'Synthetic workspace ready: {data}')
    finally:
        conn.close()


if __name__ == '__main__':
    build()
