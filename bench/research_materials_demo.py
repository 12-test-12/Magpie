"""Synthetic material workflow fixture; never overwrites an existing workspace."""
from pathlib import Path

from bench import research_demo
from magpie.core import notes, research, store

DATA = Path(__file__).resolve().parents[1] / '.magpie-dev' / 'research-materials-20261021-final'


def build():
    research_demo.DATA = DATA
    research_demo.build()
    conn = store.connect(DATA / 'magpie.db')
    try:
        pid = conn.execute('SELECT id FROM projects ORDER BY id').fetchone()[0]
        host = conn.execute("SELECT id FROM entities WHERE project_id=? AND kind='host' AND is_current=1",(pid,)).fetchone()[0]
        evidence = conn.execute('SELECT id FROM evidence WHERE project_id=? ORDER BY id LIMIT 1',(pid,)).fetchone()[0]
        created = notes.create(conn,project_id=pid,entity_id=host,text='资料验收：保留原始笔记与来源',evidence_ids=[evidence])
        for version in range(1,23):
            notes.revise(conn,project_id=pid,note_id=created['observation_id'],text=f'资料验收：第 {version} 次人工补充',
                         expected_version=version,reason=f'合成历史 {version}',evidence_ids=[evidence])
        research.save_task(conn,project_id=pid,title='资料验收：核查来源证据',entity_ids=[host],evidence_ids=[evidence])
        # Controlled job times distinguish seeded history from subsequent browser imports.
        conn.execute("UPDATE jobs SET started_at='2020-01-01T00:00:00Z'")
        store.assert_integrity(conn)
        print('Material workflow ready:',DATA)
    finally:
        conn.close()


if __name__ == '__main__':
    build()
