import json

import pytest

from magpie.core import changes, migrate, notes, ops, query, research, store, workspace
from magpie.core.manual import ManualWriter
from magpie.core.tests.test_findings_lifecycle import seed_host
from magpie.core.js_intelligence import extract, indicators


@pytest.fixture
def db():
    conn = store.connect(':memory:'); store.init_db(conn)
    pid = ops.create_project(conn,'materials',{})
    host = seed_host(conn,pid)
    yield conn,pid,host
    conn.close()


def test_note_revisions_preserve_original_evidence_undo_redo_and_report(db):
    conn,pid,host = db
    created = notes.create(conn,project_id=pid,entity_id=host,text='原始线索')
    note_id = created['observation_id']
    before = notes.detail(conn,pid,note_id)
    evidence = before['evidence_id']
    facts = [tuple(r) for r in conn.execute('SELECT * FROM contributions')]
    edited = notes.revise(conn,project_id=pid,note_id=note_id,text='补充研究 %_ 中文',
                          expected_version=0,reason='补充依据',evidence_ids=[evidence])
    assert edited['version'] == 1 and edited['original_text'] == '原始线索'
    assert notes.browse(conn,pid,q='%_')['total'] == 1
    assert [tuple(r) for r in conn.execute('SELECT * FROM contributions')] == facts
    with pytest.raises(query.QueryError,match='已被修改'):
        notes.revise(conn,project_id=pid,note_id=note_id,text='旧窗口',expected_version=0,reason='旧')
    report = workspace.generate_report(conn,pid,'json')
    assert report['research_notes'][0]['text'] == edited['text']
    assert report['research_notes'][0]['evidence_links'][0]['id'] == evidence
    ops.undo(conn,created['job_id'])
    assert notes.browse(conn,pid)['total'] == 0
    assert notes.detail(conn,pid,note_id)['available'] is False
    ops.redo(conn,created['job_id'])
    assert notes.detail(conn,pid,note_id)['text'] == edited['text']
    notes.revise(conn,project_id=pid,note_id=note_id,text=edited['text'],archived=True,
                 expected_version=1,reason='归档',evidence_ids=[evidence])
    assert notes.browse(conn,pid)['total'] == 0
    assert workspace.summary(conn,pid)['notes'] == 0
    assert notes.browse(conn,pid,archived='archived')['total'] == 1
    assert workspace.generate_report(conn,pid,'json')['research_notes'] == []
    history = notes.history(conn,pid,note_id,limit=1)
    assert history['total'] == 2 and history['next_cursor']
    assert notes.history(conn,pid,note_id,limit=1,cursor=history['next_cursor'])['rows'][0]['version'] == 2
    store.assert_integrity(conn)


def test_notes_task_evidence_guards_and_atomicity(db):
    conn,pid,host = db
    created = notes.create(conn,project_id=pid,entity_id=host,text='线索')
    nid = created['observation_id']; eid = notes.detail(conn,pid,nid)['evidence_id']
    other = ops.create_project(conn,'other',{})
    with pytest.raises(query.QueryError): notes.detail(conn,other,nid)
    with pytest.raises(query.QueryError): research.save_task(conn,project_id=other,title='bad',evidence_ids=[eid])
    with pytest.raises(query.QueryError): notes.create(conn,project_id=pid,entity_id=host,text='bad',evidence_ids=[999999])
    conn.execute('BEGIN')
    notes.revise(conn,project_id=pid,note_id=nid,text='rollback',expected_version=0,reason='事务',evidence_ids=[eid])
    research.save_task(conn,project_id=pid,title='rollback',entity_ids=[host],evidence_ids=[eid])
    conn.rollback()
    assert notes.detail(conn,pid,nid)['version'] == 0
    assert research.tasks(conn,pid)['total'] == 0
    task = research.save_task(conn,project_id=pid,title='查证据',entity_ids=[host],evidence_ids=[eid])
    assert task['evidence_links'][0]['kind'] == 'evidence'
    assert research.tasks(conn,pid,evidence_id=eid)['total'] == 1
    task = research.save_task(conn,project_id=pid,task_id=task['id'],version=1,title='保留引用')
    assert task['evidence_links'][0]['id'] == eid  # old clients do not erase new references
    ops.delete_project(conn,pid)
    store.assert_integrity(conn)


def test_manual_values_version_atomic_and_no_fact_rewrite(db):
    conn,pid,host = db
    writer = ManualWriter(conn,actor='test')
    facts = [tuple(r) for r in conn.execute('SELECT * FROM contributions')]
    value = writer.set_field_value(project_id=pid,entity_id=host,expected_version=0,
        display_name='支付中心',review_status='needs_review',reason='人工标识')
    assert value['version'] == 1
    with pytest.raises(query.QueryError):
        writer.set_field_value(project_id=pid,entity_id=host,expected_version=0,
            display_name='覆盖',review_status=None,reason='旧版本')
    conn.execute('BEGIN')
    writer.set_field_value(project_id=pid,entity_id=host,expected_version=1,
        display_name=None,review_status=None,reason='清除')
    conn.rollback()
    from magpie.core import manual_values
    assert manual_values.read(conn,pid,host) == value
    assert facts == [tuple(r) for r in conn.execute('SELECT * FROM contributions')]


def test_change_pagination_scoping_and_effective_contributions(db):
    conn,pid,host = db
    conn.execute("UPDATE jobs SET started_at='2020-01-01T00:00:00Z'")
    note = notes.create(conn,project_id=pid,entity_id=host,text='change')
    page = changes.browse(conn,pid,since=str(note['job_id']),limit=1)
    assert page['total'] >= 1 and len(page['rows']) == 1
    assert page['counts']['facts'] > 0
    with pytest.raises(query.QueryError): changes.browse(conn,pid,since='invalid')
    other = ops.create_project(conn,'empty',{})
    assert changes.browse(conn,other)['total'] == 0
    with pytest.raises(query.QueryError): changes.browse(conn,other,since=str(note['job_id']))
    ops.undo(conn,note['job_id'])
    assert changes.browse(conn,pid,since=str(note['job_id']))['total'] == 0
    ops.redo(conn,note['job_id'])
    assert changes.browse(conn,pid,since=str(note['job_id']))['counts'] == page['counts']


def test_js_static_concat_indicators_and_secret_values_never_exposed():
    secret = 'ghp_' + 'a'*36
    text = "fetch('https://api.example.test/' + 'orders?id=private');\nconst token='" + secret + "';\nfetch(prefix + '/x');"
    result = extract(text)
    assert result['candidates'][0]['url'] == 'https://api.example.test/orders?id=private'
    assert result['status'] == 'partial'
    assert text[result['candidates'][0]['start']:result['candidates'][0]['end']].startswith("'https://")
    signals = indicators(text)
    assert any(r['kind'] == 'secret_candidate' for r in signals['rows'])
    assert secret not in json.dumps(signals)
    assert all(r['start'] < r['end'] for r in signals['rows'])


@pytest.mark.parametrize('fail',[False,True])
def test_v19_material_upgrade_preserves_facts_and_registry_failure_rolls_back(monkeypatch,fail):
    conn = store.connect(':memory:')
    migrations = migrate.MIGRATIONS
    monkeypatch.setattr(migrate,'MIGRATIONS',tuple(m for m in migrations if m.version <= 19))
    store.init_db(conn)
    pid = ops.create_project(conn,'legacy-materials',{})
    host = seed_host(conn,pid)
    created = ops.add_finding(conn,project_id=pid,entity_id=host,text='历史笔记')
    tables = ('entities','entity_keys','observations','raw_files','evidence','contributions','jobs','manual_fields')
    before = {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables}
    monkeypatch.setattr(migrate,'MIGRATIONS',migrations)
    if fail:
        conn.execute("CREATE TRIGGER reject_materials BEFORE INSERT ON schema_migrations WHEN NEW.version=20 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(migrate.MigrationError): migrate.migrate(conn)
        assert migrate.current_version(conn) == 19
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='note_revisions'").fetchone() is None
    else:
        assert [m['version'] for m in migrate.migrate(conn)] == [20]
        assert notes.detail(conn,pid,created['observation_id'])['version'] == 0
    assert {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables} == before
    if not fail:
        ops.undo(conn,created['job_id']); ops.redo(conn,created['job_id'])
        assert notes.detail(conn,pid,created['observation_id'])['text'] == '历史笔记'
        store.assert_integrity(conn)
    conn.close()
