"""Permanent regression coverage for Endpoint v2 identity and migration behavior."""
from __future__ import annotations

import json

import pytest

from magpie.core import ingest, migrate, normalize, ops, store, workspace
from magpie.core.parsers import base, har


def test_endpoint_v2_identity_keeps_exact_components_and_unknown_method_distinct():
    get = normalize.endpoint_identity("HTTPS://API.Example.Test:443/items/One?x=1#part",
                                     method="get")
    same_get = normalize.endpoint_identity("https://api.example.test/items/One?x=2",
                                          method="GET")
    unknown = normalize.endpoint_identity("https://api.example.test/items/One")
    whitespace_method = normalize.endpoint_identity(
        "https://api.example.test/items/One", method="   ")

    assert get["key"] == same_get["key"]
    assert get["method"] == "GET"
    assert get["port"] == 443
    assert get["path"] == "/items/One"
    assert get["key"] != unknown["key"]
    assert whitespace_method["key"] == unknown["key"]
    assert get["key"] != normalize.endpoint_key(
        "https://api.example.test/items/one", method="GET")
    assert get["key"] != normalize.endpoint_key(
        "https://api.example.test/items/Two", method="GET")
    assert get["key"] != normalize.endpoint_key(
        "http://api.example.test/items/One", method="GET")
    assert normalize.decode_endpoint_key(get["key"])["path"] == "/items/One"
    assert normalize.endpoint_identity("https://api.example.test?x=1")["path"] == "/"


def _open_v15(monkeypatch):
    conn = store.connect(":memory:")
    original = migrate.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(migrate, "MIGRATIONS", tuple(m for m in original if m.version <= 15))
        store.init_db(conn)
    assert migrate.current_version(conn) == 15
    return conn


def _legacy_snapshot(conn):
    tables = ("entities", "entity_keys", "observations", "relations", "evidence",
              "raw_files", "contributions", "jobs", "entity_alias",
              "entity_merge_events", "entity_merge_members", "contribution_supersessions")
    return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            for table in tables}


def test_v15_migration_preserves_folded_facts_merge_and_inactive_job(monkeypatch):
    conn = _open_v15(monkeypatch)
    try:
        project = ops.create_project(conn, "legacy-endpoints", {})
        job = ops.start_job(conn, project_id=project, kind="ingest", coverage="partial")
        raw = ops.add_raw_file(conn, project_id=project, locator_uri="synthetic://legacy.har",
                               content="synthetic legacy evidence")
        evidence = ops.add_evidence(conn, project_id=project, raw_file_id=raw,
                                    locator={"record": 1}, snippet="synthetic")
        source = store.register_source(conn, name="legacy synthetic", actor="test")
        left = "api.example.test/users/{id}"
        right = "api.example.test/orders/{id}"
        for key in (left, right):
            for field, value in (("path", "/users/123"), ("path", "/orders/456"),
                                 ("method", "GET"), ("method", "POST"),
                                 ("scheme", "http"), ("scheme", "https"),
                                 ("port", "80"), ("port", "443")):
                ops.add_observation(conn, project_id=project, job_id=job,
                                    evidence_id=evidence, source_id=source,
                                    entity_key=key, kind="endpoint",
                                    semantic_field=field, value=value)
        store.recompute(conn)
        ops.merge(conn, project_id=project, survivor_key=left, member_keys=[right], actor="test")
        ops.undo(conn, job_id=job, actor="test")
        assert conn.execute("SELECT applied FROM jobs WHERE id=?", (job,)).fetchone()[0] == 0
        before = _legacy_snapshot(conn)

        applied = migrate.migrate(conn)

        assert 16 in [item["version"] for item in applied]
        assert _legacy_snapshot(conn) == before
        assert migrate.current_version(conn) == len(migrate.MIGRATIONS)
        assert conn.execute(
            "SELECT COUNT(*) FROM multivalue_fields WHERE semantic_field='http_request_sample'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM entity_merge_events").fetchone()[0] == 1
        store.assert_integrity(conn)
    finally:
        conn.close()


def test_v16_registry_failure_rolls_back_schema_and_retries(monkeypatch):
    conn = _open_v15(monkeypatch)
    try:
        before_rule = conn.execute(
            "SELECT identity_rule FROM kind_registry WHERE kind='endpoint'").fetchone()[0]
        conn.execute("CREATE TRIGGER reject_v16 BEFORE INSERT ON schema_migrations "
                     "WHEN NEW.version=16 BEGIN SELECT RAISE(ABORT,'synthetic ledger failure'); END")
        with pytest.raises(migrate.MigrationError, match="synthetic ledger failure"):
            migrate.migrate(conn)
        assert not conn.in_transaction
        assert migrate.current_version(conn) == 15
        assert conn.execute(
            "SELECT identity_rule FROM kind_registry WHERE kind='endpoint'").fetchone()[0] == before_rule
        assert conn.execute(
            "SELECT COUNT(*) FROM multivalue_fields WHERE semantic_field='http_request_sample'"
        ).fetchone()[0] == 0

        conn.execute("DROP TRIGGER reject_v16")
        assert 16 in [item["version"] for item in migrate.migrate(conn)]
        assert migrate.current_version(conn) == len(migrate.MIGRATIONS)
        assert migrate.migrate(conn) == []
    finally:
        conn.close()


def _har_entry(index: int, secret: str) -> dict:
    return {
        "startedDateTime": f"2026-01-01T00:00:0{index}Z",
        "request": {
            "method": "GET",
            "url": f"https://api.example.test/items?password={secret}&page={index}",
            "queryString": [],
            "headers": [
                {"name": "Cookie", "value": f"session={secret}"},
                {"name": "Authorization", "value": f"Bearer {secret}"},
            ],
            "postData": {"mimeType": "application/json",
                         "text": json.dumps({"token": secret, "display.name": "synthetic"})},
        },
        "response": {"status": 200 + index,
                     "content": {"mimeType": "application/json", "size": 40}},
    }


def test_har_samples_are_masked_paginated_provenanced_and_reversible(tmp_path):
    conn = store.connect(":memory:")
    try:
        store.init_db(conn)
        project = ops.create_project(conn, "synthetic-har", {})
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        path = data_dir / "requests.har"
        secrets = ["SYNTHETIC-SECRET-ONE", "SYNTHETIC-SECRET-TWO", "SYNTHETIC-SECRET-THREE"]
        path.write_text(json.dumps({"log": {"version": "1.2", "entries": [
            _har_entry(index, value) for index, value in enumerate(secrets, 1)]}}), encoding="utf-8")
        report = ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=project, data_dir=data_dir, actor="test", verify=True), path)
        unknown_path = data_dir / "unknown.txt"
        unknown_path.write_text("https://api.example.test/unknown\n", encoding="utf-8")
        ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=project, data_dir=data_dir, parser="gau", actor="test", verify=True),
            unknown_path)
        key = normalize.endpoint_key("https://api.example.test/items", method="GET")
        row = next(item for item in workspace.browse(
            conn, project, category="api", mode="list")["rows"]
                   if item["entity_key"] == key)

        first = workspace.entity_detail(conn, project, row["id"], section="samples", limit=2)
        second = workspace.entity_detail(conn, project, row["id"], section="samples",
                                          cursor=first["next_cursor"], limit=2)
        samples = first["rows"] + second["rows"]
        assert first["total"] == second["total"] == 3
        assert len(samples) == 3
        assert all(item["evidence_id"] and item["source"] for item in samples)
        rendered = json.dumps(samples, ensure_ascii=False)
        assert all(secret not in rendered for secret in secrets)
        sample = json.loads(samples[0]["value"])
        assert {"password", "page"} == set(sample["query_parameter_names"])
        assert {"cookie", "bearer", "unknown"} <= set(sample["credentials"])
        assert "MASKED" in sample["url"]
        parameters = workspace.entity_detail(conn, project, row["id"], section="parameters", limit=50)
        fields = {item["semantic_field"] for item in parameters["rows"]}
        assert "param.query.password" in fields
        assert "param.body.display.name" in fields
        assert all(secret not in json.dumps(parameters["rows"]) for secret in secrets)
        unknown_rows = workspace.browse(conn, project, category="api", mode="list",
                                        method="unknown")["rows"]
        assert [item["endpoint_identity"]["method"] for item in unknown_rows
                if item["kind"] == "endpoint"] == ["unknown"]

        ops.undo(conn, job_id=report["job_id"], actor="test")
        assert workspace.entity_detail(conn, project, row["id"], section="samples")["total"] == 0
        ops.redo(conn, job_id=report["job_id"], actor="test")
        store.recompute(conn)
        restored = workspace.entity_detail(conn, project, row["id"], section="samples", limit=10)
        assert restored["total"] == 3
        assert len(restored["rows"]) == 3
        store.assert_integrity(conn)
    finally:
        conn.close()


def test_har_unparsed_body_keeps_credentials_unknown():
    document = {"log": {"version": "1.2", "entries": [{
        "request": {"method": "POST", "url": "https://api.example.test/submit",
                    "headers": [], "postData": {"mimeType": "text/plain",
                                                   "text": "api_key=SYNTHETIC-KEY"}},
        "response": {"status": 204},
    }]}}
    records = har.parse_text(json.dumps(document))
    sample_record = next(record for record in records
                         if isinstance(record, base.RawObservation)
                         and record.type == "http_request_sample")
    sample = sample_record.value
    assert sample["body_parameters_state"] == "not_json"
    assert sample["credentials"] == ["unknown"]
    assert "SYNTHETIC-KEY" not in json.dumps(sample)
