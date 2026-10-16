"""The JS vocabulary upgrade must preserve existing evidence and reversibility."""
from magpie.core import ingest, migrate, ops, store


def test_v14_upgrade_preserves_native_import_and_undo(tmp_path, monkeypatch):
    conn = store.connect(':memory:')
    migrations = migrate.MIGRATIONS
    with monkeypatch.context() as previous:
        previous.setattr(migrate, 'MIGRATIONS', migrations[:-1])
        store.init_db(conn)
        assert migrate.current_version(conn) == 14
        project = ops.create_project(conn, 'pre-js-upgrade', {})
        source = tmp_path / 'domains.txt'
        source.write_text('upgrade.example.test\n', encoding='utf-8')
        report = ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=project, data_dir=tmp_path), source)
        assert report['state'] == 'done'

    def facts():
        return {
            table: [tuple(row) for row in conn.execute(
                f'SELECT * FROM {table} ORDER BY ' + ('entity_key' if table == 'entity_keys' else 'id'))]
            for table in ('entities', 'entity_keys', 'observations', 'evidence', 'raw_files', 'contributions')
        }

    original = facts()
    store.init_db(conn)
    assert migrate.current_version(conn) == 15
    assert facts() == original
    assert conn.execute("SELECT COUNT(*) FROM multivalue_fields WHERE semantic_field='js_candidate'").fetchone()[0] == 1
    ops.undo(conn, report['job_id'])
    assert conn.execute('SELECT COUNT(*) FROM v_active_contributions WHERE job_id=?',
                        (report['job_id'],)).fetchone()[0] == 0
    ops.redo(conn, report['job_id'])
    assert conn.execute('SELECT COUNT(*) FROM v_active_contributions WHERE job_id=?',
                        (report['job_id'],)).fetchone()[0] > 0
    store.assert_integrity(conn)
    conn.close()
