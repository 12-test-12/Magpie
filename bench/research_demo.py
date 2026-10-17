"""Isolated work package G fixture; refuses to overwrite an existing database.

python -m bench.research_demo
python dev.py --workspace research-workflow
"""
from pathlib import Path

from bench import findings_demo
from magpie.core import ops, research, store

DATA = Path(__file__).resolve().parents[1] / '.magpie-dev' / 'research-workflow'


def build():
    findings_demo.DATA = DATA
    findings_demo.build()
    conn = store.connect(DATA / 'magpie.db')
    try:
        pid = conn.execute('SELECT id FROM projects').fetchone()[0]
        host = conn.execute("SELECT id FROM entities WHERE kind='host' AND is_current=1").fetchone()[0]
        endpoint = conn.execute("SELECT id FROM entities WHERE kind='endpoint' AND is_current=1").fetchone()[0]
        finding = conn.execute("SELECT id FROM entities WHERE kind='vuln' AND is_current=1 ORDER BY id DESC").fetchone()[0]
        research.save_metadata(conn, project_id=pid, entity_id=host, criticality=1, add_tags=['core-system'])
        research.save_metadata(conn, project_id=pid, entity_id=endpoint, criticality=2, add_tags=['payment'])
        research.save_task(conn, project_id=pid, title='待验证 8443 控制台登录策略', entity_ids=[host, finding])
        research.save_task(conn, project_id=pid, title='待申请测试账号', description='由研究员手工记录客户依赖；无自动网络执行。', status='shelved')
        research.save_task(conn, project_id=pid, title='待核查 POST /api/login', status='in_progress', entity_ids=[endpoint])
        for i in range(51):
            research.save_task(conn, project_id=pid, title=f'分页合成待办 {i+1:02d}', status='done')
        ops.create_project(conn, 'G 空项目', {})
        store.assert_integrity(conn)
        print('Research workspace ready:', DATA)
    finally:
        conn.close()


if __name__ == '__main__':
    build()
