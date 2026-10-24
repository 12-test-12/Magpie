"""Seed research UI regressions inside the CI launcher's new synthetic workspace."""
from pathlib import Path

from magpie.core import notes, ops, store


def seed(data: Path):
    conn = store.connect(data / 'magpie.db')
    try:
        name = '研究资料回归 · 合成验收'
        if conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise RuntimeError('Research fixture already exists; refusing to replace it')
        pid = ops.create_project(conn, name, {'include': ['*.example.test']})
        store.register_source(conn, source_id=91, name='synthetic-research', authority_level='verified')
        raw = ops.add_raw_file(conn, project_id=pid, locator_uri='file:///synthetic-research-seed.txt', content='synthetic host')
        eid = ops.add_evidence(conn, project_id=pid, raw_file_id=raw, locator={'line':1}, snippet='synthetic host')
        job = ops.start_job(conn, project_id=pid, kind='scan', coverage='full', parser_fingerprint='synthetic-1', actor='ci-fixture')
        ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=eid, source_id=91,
                            entity_key='merge.example.test', kind='host', semantic_field='hostname', value='merge.example.test')
        ops.finish_job(conn, job, 'done'); store.recompute(conn)
        host = store.entity_for_key(conn, pid, 'merge.example.test')
        note = notes.create(conn, project_id=pid, entity_id=host, text='持续回归原始线索')
        note_evidence = notes.detail(conn, pid, note['observation_id'])['evidence_id']
        conn.execute("UPDATE jobs SET started_at='2020-01-01T00:00:00Z' WHERE project_id=?", (pid,))
        store.assert_integrity(conn)
        return {'project':name,'project_id':pid,'host':host,'note':note['observation_id'],
                'new_evidence':eid,'note_evidence':note_evidence,'evidence_query':'synthetic-research-seed'}
    finally:
        conn.close()
