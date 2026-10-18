"""Refuse stale or unsupported analyst writes without changing research facts."""
from __future__ import annotations

import pytest

from magpie.core import migrate, ops, query, research, store, workspace
from magpie.core.tests.test_findings_lifecycle import seed_host


@pytest.fixture
def data():
    conn = store.connect(':memory:')
    store.init_db(conn)
    pid = ops.create_project(conn, 'synthetic write safety', {})
    host = seed_host(conn, pid)
    finding = ops.create_manual_finding(conn, project_id=pid, title='人工线索', severity='medium', target_entity_id=host)
    yield conn, pid, host, finding
    conn.close()


def snapshot(conn):
    tables = ['finding_triage', 'finding_retests', 'activity_log', 'jobs', *store.FACT_TABLES, *store.CACHE_TABLES]
    return {t: [tuple(r) for r in conn.execute('SELECT * FROM ' + t)] for t in tables}


@pytest.mark.parametrize('first', ['triage', 'retest', 'inconclusive'])
@pytest.mark.parametrize('second', ['triage', 'retest'])
def test_stale_interleaved_writes_preserve_first_decision_and_all_facts(data, first, second):
    conn, pid, _, created = data
    fid, version = created['finding_id'], created['version']
    if first == 'triage':
        result = ops.triage_finding(conn, project_id=pid, finding_id=fid, expected_version=version,
                                    status='confirmed', severity='high', reason='人工核实依据')
    else:
        result = ops.add_retest(conn, project_id=pid, finding_id=fid, expected_version=version,
                               status='inconclusive' if first == 'inconclusive' else 'resolved', notes='人工操作说明，证据未导入')
    assert result['version'] == version + 1
    before = snapshot(conn)
    with pytest.raises(query.QueryError, match='已被修改') as error:
        if second == 'triage':
            ops.triage_finding(conn, project_id=pid, finding_id=fid, expected_version=version,
                               status='candidate', severity='low', reason='旧窗口')
        else:
            ops.add_retest(conn, project_id=pid, finding_id=fid, expected_version=version,
                           status='still_vulnerable', notes='旧窗口')
    assert error.value.code == 'E_STALE_FINDING'
    assert snapshot(conn) == before
    detail = workspace.finding_detail(conn, pid, fid)
    assert detail['triage']['version'] == version + 1
    assert detail['status'] == {'triage': 'confirmed', 'retest': 'resolved', 'inconclusive': 'candidate'}[first]
    store.assert_integrity(conn)


@pytest.mark.parametrize('status', ['confirmed', 'resolved', 'false_positive', 'duplicate', 'inconclusive'])
def test_key_decisions_require_explicit_reason_and_do_not_write_on_failure(data, status):
    conn, pid, host, created = data
    before = snapshot(conn)
    with pytest.raises(store.InvariantError, match='填写理由'):
        ops.triage_finding(conn, project_id=pid, finding_id=created['finding_id'], expected_version=1,
                           status=status, reason='  ', duplicate_of_id=created['finding_id'] if status == 'duplicate' else None)
    assert snapshot(conn) == before
    if status != 'duplicate':
        with pytest.raises(store.InvariantError, match='填写理由'):
            ops.create_manual_finding(conn, project_id=pid, title='缺少依据', severity='high', target_entity_id=host, status=status)
        assert snapshot(conn) == before


@pytest.mark.parametrize('value', [None, -1, True, '1'])
def test_core_refuses_invalid_revision(data, value):
    conn, pid, _, created = data
    before = snapshot(conn)
    with pytest.raises(store.InvariantError, match='expected_version'):
        ops.add_retest(conn, project_id=pid, finding_id=created['finding_id'], expected_version=value,
                       status='inconclusive', notes='本次无写入')
    assert snapshot(conn) == before


@pytest.mark.parametrize('operation', ['triage', 'retest'])
def test_revision_and_audit_roll_back_with_caller(data, operation):
    conn, pid, _, created = data
    before = snapshot(conn)
    conn.execute('BEGIN IMMEDIATE')
    if operation == 'triage':
        ops.triage_finding(conn, project_id=pid, finding_id=created['finding_id'], expected_version=1,
                           status='confirmed', reason='回滚测试')
    else:
        ops.add_retest(conn, project_id=pid, finding_id=created['finding_id'], expected_version=1,
                       status='inconclusive', notes='回滚测试')
    assert workspace.finding_detail(conn, pid, created['finding_id'])['triage']['version'] == 2
    conn.rollback()
    assert snapshot(conn) == before


def test_project_and_evidence_guards_do_not_leave_partial_writes(data):
    conn, pid, _, created = data
    other = ops.create_project(conn, 'other', {})
    raw = ops.add_raw_file(conn, project_id=other, locator_uri='file:///synthetic', content='fixture')
    ev = ops.add_evidence(conn, project_id=other, raw_file_id=raw, locator={}, snippet='fixture')
    for project, evidence in [(other, None), (pid, ev)]:
        before = snapshot(conn)
        with pytest.raises(store.InvariantError):
            ops.add_retest(conn, project_id=project, finding_id=created['finding_id'], expected_version=1,
                           status='resolved', notes='错误项目', evidence_id=evidence)
        assert snapshot(conn) == before


@pytest.mark.parametrize('operation', ['triage', 'resolved', 'inconclusive'])
def test_tool_candidate_starts_at_zero_and_creates_first_revision(data, operation):
    conn, pid, _, created = data
    fid = created['finding_id']
    # An imported candidate has facts but no human triage row.
    conn.execute('DELETE FROM finding_triage WHERE project_id=? AND finding_id=?', (pid, fid))
    assert workspace.finding_detail(conn, pid, fid)['triage']['version'] == 0
    if operation == 'triage':
        result = ops.triage_finding(conn, project_id=pid, finding_id=fid, expected_version=0,
                                    status='confirmed', reason='首次人工核实')
    else:
        result = ops.add_retest(conn, project_id=pid, finding_id=fid, expected_version=0,
                               status=operation, notes='首次人工复测说明')
    assert result['version'] == workspace.finding_detail(conn, pid, fid)['triage']['version'] == 1
    store.assert_integrity(conn)


def test_report_preserves_evidence_gaps_and_does_not_rewrite_history(data):
    conn, pid, _, created = data
    fid = created['finding_id']
    assert workspace.finding_detail(conn, pid, fid)['status'] == 'candidate'
    ops.add_retest(conn, project_id=pid, finding_id=fid, expected_version=1, status='resolved', notes='仅人工说明')
    # Reproduce a historical missing-reason record, not a permitted production write.
    conn.execute('UPDATE finding_triage SET disposition_reason=NULL WHERE finding_id=?', (fid,))
    before = snapshot(conn)
    report = workspace.generate_report(conn, pid, 'json')['resolved_findings'][0]
    md = workspace.generate_report(conn, pid)
    assert report['decision_basis_missing']
    assert report['retests'][0]['evidence_basis'] == 'manual_statement_only'
    assert '未关联复测证据，仅有人工说明' in md and '历史记录未填写判定理由' in md
    assert '经后续导入的复测证据验证修复' not in md
    assert snapshot(conn) == before


def test_open_task_filter_has_same_count_as_summary_and_bound_cursor(data):
    conn, pid, _, _ = data
    for status in research.STATUSES:
        research.save_task(conn, project_id=pid, title=status, status=status)
    page = research.tasks(conn, pid, status='open', limit=1)
    assert page['total'] == workspace.summary(conn, pid)['tasks_open'] == 2
    assert page['rows'][0]['status'] == 'pending'
    last = research.tasks(conn, pid, status='open', limit=1, cursor=page['next_cursor'])
    assert last['rows'][0]['status'] == 'in_progress' and last['next_cursor'] is None
    with pytest.raises(query.QueryError):
        research.tasks(conn, pid, cursor=page['next_cursor'])
    with pytest.raises(query.QueryError):
        research.save_task(conn, project_id=pid, title='不可写入组合状态', status='open')


@pytest.mark.parametrize('fail', [False, True])
def test_v18_upgrade_only_adds_versions_and_is_atomic(monkeypatch, fail):
    conn = store.connect(':memory:')
    all_migrations = migrate.MIGRATIONS
    monkeypatch.setattr(migrate, 'MIGRATIONS', tuple(m for m in all_migrations if m.version <= 18))
    store.init_db(conn)
    pid = ops.create_project(conn, 'legacy synthetic', {})
    host = seed_host(conn, pid)
    # Seed old parser facts and a work-layer row without calling new versioned code.
    ev = conn.execute('SELECT id FROM evidence WHERE project_id=? LIMIT 1', (pid,)).fetchone()[0]
    job = ops.start_job(conn, project_id=pid, kind='scan', coverage='partial')
    key = 'legacy-check@app.example.test'
    ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev, source_id=1,
                        entity_key=key, kind='vuln', semantic_field='title', value='历史判定')
    ops.finish_job(conn, job, 'done'); store.recompute(conn)
    fid = store.resolve_entity(conn, pid, key)
    conn.execute("INSERT INTO finding_triage(project_id,finding_id,status,disposition_reason,updated_at) VALUES(?,?,'resolved',NULL,'2026-10-17')", (pid, fid))
    before = snapshot(conn)
    old_columns = [r[1] for r in conn.execute('PRAGMA table_info(finding_triage)')]
    monkeypatch.setattr(migrate, 'MIGRATIONS', all_migrations)
    if fail:
        conn.execute("CREATE TRIGGER fail_version_registry BEFORE INSERT ON schema_migrations WHEN NEW.version=19 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(migrate.MigrationError):
            migrate.migrate(conn)
        assert [r[1] for r in conn.execute('PRAGMA table_info(finding_triage)')] == old_columns
        assert snapshot(conn) == before
        assert migrate.current_version(conn) == 18
    else:
        assert [m['version'] for m in migrate.migrate(conn)] == [19]
        for table, rows in before.items():
            if table == 'finding_triage':
                assert [tuple(r) for r in conn.execute('SELECT ' + ','.join(old_columns) + ' FROM finding_triage')] == rows
            elif table == 'activity_log':
                assert [tuple(r) for r in conn.execute('SELECT * FROM activity_log')][:len(rows)] == rows
            else:
                assert [tuple(r) for r in conn.execute('SELECT * FROM ' + table)] == rows
        assert conn.execute('SELECT version FROM finding_triage').fetchone()[0] == 1
        assert migrate.migrate(conn) == []
    conn.close()
