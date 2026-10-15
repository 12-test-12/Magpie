"""Build a separate, synthetic browser acceptance database (no running writer).

Run from repo root: .venv-dev/Scripts/python.exe -m bench.workspace_demo
Then: .venv-dev/Scripts/python.exe dev.py --workspace mainline-two
Refuses to overwrite an existing database. All facts use existing ops/store.
This measures workspace reading, not parser throughput or API identity changes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from magpie.core import normalize, ops, store, workspace

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.magpie-dev' / 'mainline-two'


def build():
    DATA.mkdir(parents=True, exist_ok=True)
    db = DATA / 'magpie.db'
    if db.exists():
        raise SystemExit('Fixture database already exists; refusing to overwrite: ' + str(db))
    started = time.perf_counter()
    conn = store.connect(db); store.init_db(conn)
    ops.create_project(conn, '空项目 · 主线二验收', {})
    store.register_source(conn, source_id=1, name='workspace-synthetic', authority_level='unverified')
    # A genuine archived manifest, not invented scanner evidence.
    content = 'Synthetic workspace acceptance fixture. No live targets scanned.\n'
    import hashlib
    sha = hashlib.sha256(content.encode()).hexdigest()
    path = DATA / 'raw' / sha[:2] / sha
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content, encoding='utf-8')
    for title, count in [('普通项目 · 主线二验收', 120), ('大数据 · 十万接口验收', 100000)]:
        pid = ops.create_project(conn, title, {'include': ['*.example.test']})
        raw = ops.add_raw_file(conn, project_id=pid, locator_uri=path.as_uri(), content=content, storage_state='archived')
        ev = ops.add_evidence(conn, project_id=pid, raw_file_id=raw, locator={'line': 1}, snippet=content)
        job = ops.start_job(conn, project_id=pid, kind='ingest', coverage='partial', actor='synthetic-fixture')
        with store.atomic(conn):
            for i in range(count):
                # Alphabetic suffix keeps distinct paths under existing normalization.
                suffix = ''.join(chr(97 + (i // (26 ** power)) % 26) for power in range(4))
                url = f'https://h{i % 100:02}.example.test/api/v{i % 8}/resource{suffix}'
                key = normalize.endpoint_key(url)
                ops.add_observation(conn, project_id=pid, job_id=job, source_id=1, evidence_id=ev,
                    entity_key=key, kind='endpoint', semantic_field='method', value='GET' if i % 3 else 'POST')
                if i < 4:
                    ops.add_observation(conn, project_id=pid, job_id=job, source_id=1, evidence_id=ev,
                        entity_key=key, kind='endpoint', semantic_field='status', value='200')
                if i and i % 10000 == 0:
                    print(f'{title}: {i} / {count}', flush=True)
            if count == 120:
                for host in ['app.example.test', 'api.example.test', 'cdn.example.test']:
                    ops.upsert_entity(conn, project_id=pid, job_id=job, source_id=1, evidence_id=ev,
                                      entity_key=host, kind='domain')
                ops.upsert_entity(conn, project_id=pid, job_id=job, source_id=1, evidence_id=ev,
                    entity_key=normalize.endpoint_key('https://cdn.example.test/static/app.js'), kind='endpoint')
        ops.finish_job(conn, job, 'done')
        store.recompute(conn)
        store.assert_integrity(conn)
        samples = {}
        for mode, host in [('host', None), ('path', 'h00.example.test'), ('list', 'h00.example.test')]:
            before = time.perf_counter()
            result = workspace.browse(conn, pid, category='api', mode=mode, host=host)
            samples[mode] = {'seconds': round(time.perf_counter() - before, 4), 'total': result['total'],
                             'rows': len(result['rows']), 'has_next': bool(result['next_cursor'])}
        print(json.dumps({'project': title, 'counts': query_counts(conn, pid), 'queries': samples}, ensure_ascii=False), flush=True)
    conn.close()
    print(f'Fixture ready in {time.perf_counter() - started:.2f}s: {DATA}', flush=True)


def query_counts(conn, pid):
    return workspace.summary(conn, pid)['kinds']


if __name__ == '__main__':
    build()
