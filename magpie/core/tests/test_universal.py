"""Unknown formats must remain explainable, reversible and source-locatable."""
import json
from pathlib import Path

import pytest

from magpie.core import ingest, ops, store, universal


def write(tmp_path, text, name="unknown.result"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_mixed_jsonl_reports_every_record_and_does_not_promote_id(tmp_path):
    path = write(tmp_path, '{"target":"https://a.example.com/api","extra":"x"}\n'
                 '{"id":"127.0.0.1"}\nnot-json\n')
    result = universal.preview(path)
    assert result["records_total"] == 3
    assert result["mapped_partial"] == result["unrecognized"] == result["record_error"] == 1
    assert result["complete"]
    assert result["details"][1]["fields"][0]["kind"] == "unknown"
    assert sum(result[k] for k in ("mapped_complete", "mapped_partial", "unrecognized",
                                   "ignored", "record_error")) == 3


def test_csv_mapping_changes_meaning_only_after_explicit_choice(tmp_path):
    path = write(tmp_path, 'destination,code,other\nhttps://a.example.com/api,200,retain\n', 'custom.csv')
    before = universal.preview(path)
    assert before["unrecognized"] == 1
    after = universal.preview(path, {"$.destination": "url", "$.code": "status", "$.other": "ignore"})
    assert after["mapped_complete"] == 1
    assert before["structure_signature"] == after["structure_signature"]
    assert after["details"][0]["fields"][-1]["kind"] == "ignore"


@pytest.mark.parametrize("text,fmt,total", [
    ('[{"domain":"a.example.com"},{"domain":"b.example.com"}]', "json", 2),
    ('domain\tip\na.example.com\t192.0.2.1\n', "tsv", 1),
    ('<rows><row><domain>a.example.com</domain></row><row><domain>b.example.com</domain></row></rows>', "xml", 2),
    ('a.example.com\nb.example.com\n', "text", 2),
])
def test_formats_are_content_based(tmp_path, text, fmt, total):
    result = universal.preview(write(tmp_path, text))
    assert result["format"] == fmt
    assert result["records_total"] == total
    assert result["complete"]


def test_xml_entities_and_broken_whole_document_are_not_success(tmp_path):
    result = universal.preview(write(tmp_path, '<!DOCTYPE x [<!ENTITY y SYSTEM "file:///x">]><x>&y;</x>'))
    assert not result["complete"] and result["error"]
    result = universal.preview(write(tmp_path, '<rows><bad>'))
    assert not result["complete"] and result["error"]


def test_native_priority_generic_fallback_and_undo(tmp_path):
    conn = store.connect(":memory:")
    store.init_db(conn)
    project = ops.create_project(conn, "universal", {"include": ["*.example.com"]})
    options = ingest.IngestOptions(project, tmp_path / "data")
    native = ingest.ingest_file(conn, options, write(tmp_path, 'a.example.com\n', 'domains.txt'))
    assert native["parser"].startswith("domainlist@")
    path = write(tmp_path, '[{"domain":"b.example.com"},{"id":"127.0.0.1"}]')
    result = ingest.ingest_file(conn, options, path)
    assert result["parser"].startswith("generic@")
    assert result["import_coverage"]["unrecognized"] == 1
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE kind='ip'").fetchone()[0] == 0
    job_id = result["job_id"]
    evidence = conn.execute("SELECT locator FROM evidence").fetchall()
    assert any('record_path' in row[0] for row in evidence)
    assert ingest.ingest_file(conn, options, path)["state"] == "skipped"
    assert not [r for r in ingest.parser_drift_report(conn) if r['fingerprint'].startswith('generic@')]
    ops.undo(conn, job_id)
    assert store.integrity_check(conn)["ok"]
    ops.redo(conn, job_id)
    assert store.integrity_check(conn)["ok"]
    conn.close()


def test_mapping_fingerprints_and_changed_file_protection(tmp_path):
    assert universal.parser_for({}).format_version != universal.parser_for({"$.x": "url"}).format_version
    conn = store.connect(":memory:")
    store.init_db(conn)
    project = ops.create_project(conn, "test", {})
    path = write(tmp_path, '{"target":"https://a.example.com"}')
    digest = universal.preview(path)["sha256"]
    path.write_text('{"target":"https://b.example.com"}', encoding="utf-8")
    report = ingest.ingest_file(conn, ingest.IngestOptions(project, tmp_path,
        parser="generic", expected_sha256=digest), path)
    assert report["state"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0
    conn.close()


def test_profiles_persist_no_samples_and_isolate_projects(tmp_path):
    universal.save_profile(tmp_path, 1, "custom", "0123456789abcdef", {"$.destination": "url"})
    assert universal.load_profiles(tmp_path, 1)[0]["mapping"] == {"$.destination": "url"}
    assert universal.load_profiles(tmp_path, 2) == []
    with pytest.raises(ValueError):
        universal.save_profile(tmp_path, 1, "bad", "0123456789abcdef", {"$.x": "execute"})


def test_directory_discovery_reports_skipped_binary(tmp_path):
    write(tmp_path, '{"domain":"a.example.com"}', 'scanner.weird')
    (tmp_path / 'picture.png').write_bytes(b'\x89PNG\x00\x00')
    skipped = []
    files, problems = ingest.discover([str(tmp_path)], skipped=skipped)
    assert [p.name for p in files] == ['scanner.weird']
    assert not problems
    assert len(skipped) == 1 and 'picture.png' in skipped[0]['path']


def test_detail_limit_preserves_counts(tmp_path):
    path = write(tmp_path, '\n'.join('{"id":"x"}' for _ in range(205)))
    result = universal.preview(path)
    assert result["records_total"] == result["unrecognized"] == 205
    assert len(result["details"]) == 200 and result["details_truncated"]


def test_field_paths_do_not_alias_literal_dots():
    assert set(k for k, _ in universal.leaves({"a.b": 1, "a": {"b": 2}})) == {'$["a.b"]', '$.a.b'}
