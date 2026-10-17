"""Tests for Finding lifecycle, triage states, retests with evidence, and reporting (Work Package F)."""
from __future__ import annotations

import json
import sqlite3
import pytest

from magpie.core import ops, query, store, workspace


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    store.init_db(conn)
    yield conn
    conn.close()


def seed_host(conn: sqlite3.Connection, project_id: int, key: str = "app.example.test") -> int:
    store.register_source(conn, source_id=1, name="test-source", authority_level="verified")
    raw = ops.add_raw_file(conn, project_id=project_id, locator_uri=f"file:///{key}.txt", content="seed\n")
    evidence = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw, locator={"line": 1}, snippet="seed")
    job = ops.start_job(conn, project_id=project_id, kind="scan", coverage="full", parser_fingerprint="p-1", actor="cli")
    ops.add_observation(conn, project_id=project_id, job_id=job, evidence_id=evidence, source_id=1, entity_key=key,
                        kind="host", semantic_field="hostname", value=key)
    ops.finish_job(conn, job, "done")
    store.recompute(conn)
    return store.entity_for_key(conn, project_id, key)


def test_migration_017_schema_and_tables(db):
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finding_triage'").fetchone()
    assert row is not None
    row2 = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finding_retests'").fetchone()
    assert row2 is not None


def test_manual_finding_lifecycle_and_undo_redo(db):
    project_id = ops.create_project(db, "test-proj", {"include": ["*.example.test"]})
    host_id = seed_host(db, project_id=project_id, key="app.example.test")

    # 1. Create manual finding
    res = ops.create_manual_finding(
        db, project_id=project_id, title="SQL 注入漏洞", severity="high",
        target_entity_id=host_id, description="登录接口参数未做参数化查询",
        status="confirmed", reason="代码审查与人工复核确认", actor="analyst")

    finding_id = res["finding_id"]
    job_id = res["job_id"]
    assert finding_id is not None
    assert job_id is not None

    # Check detail
    detail = workspace.finding_detail(db, project_id, finding_id)
    assert detail["title"] == "SQL 注入漏洞"
    assert detail["triage"]["status"] == "confirmed"
    assert detail["triage"]["severity"] == "high"
    assert detail["target_asset"]["id"] == host_id
    assert detail["target_asset"]["kind"] == "host"
    assert detail["initial_evidence"] is not None

    # Browse findings
    browse_res = workspace.browse(db, project_id, category="findings", mode="list")
    assert browse_res["total"] == 1
    assert browse_res["rows"][0]["status"] == "confirmed"
    assert browse_res["rows"][0]["severity"] == "high"

    # Filter by status
    assert workspace.browse(db, project_id, category="findings", mode="list", status="confirmed")["total"] == 1
    assert workspace.browse(db, project_id, category="findings", mode="list", status="candidate")["total"] == 0

    # Summary reflects confirmed count
    summary = workspace.summary(db, project_id)
    assert summary["findings"] == 1
    assert summary["findings_confirmed"] == 1
    assert summary["findings_candidate"] == 0

    # Undo removes manual finding from active views
    ops.undo(db, job_id)
    assert workspace.browse(db, project_id, category="findings", mode="list")["total"] == 0

    # Redo restores manual finding
    ops.redo(db, job_id)
    assert workspace.browse(db, project_id, category="findings", mode="list")["total"] == 1


def test_tool_hit_default_candidate_and_triage_flow(db):
    project_id = ops.create_project(db, "test-proj", {"include": ["*.example.test"]})
    host_id = seed_host(db, project_id=project_id, key="target.example.test")

    # Simulate tool hit (e.g. nuclei import)
    store.register_source(db, source_id=10, name="nuclei@1", authority_level="verified")
    raw = ops.add_raw_file(db, project_id=project_id, locator_uri="file:///scan.jsonl", content="line")
    ev = ops.add_evidence(db, project_id=project_id, raw_file_id=raw, locator={"line": 1}, snippet="match")
    job = ops.start_job(db, project_id=project_id, kind="scan", coverage="partial", actor="nuclei")

    from magpie.core import normalize
    vuln_key = normalize.vuln_key("cve-2023-0001", "target.example.test")
    ops.add_observation(db, project_id=project_id, job_id=job, evidence_id=ev, source_id=10,
                        entity_key=vuln_key, kind="vuln", semantic_field="title", value="CVE-2023-0001 远程代码执行")
    ops.add_observation(db, project_id=project_id, job_id=job, evidence_id=ev, source_id=10,
                        entity_key=vuln_key, kind="vuln", semantic_field="severity", value="critical")
    ops.add_observation(db, project_id=project_id, job_id=job, evidence_id=ev, source_id=10,
                        entity_key=vuln_key, kind="vuln", semantic_field="state", value="open")
    ops.add_relation(db, project_id=project_id, job_id=job, evidence_id=ev, source_id=10,
                     src_key=vuln_key, src_kind="vuln", rel="found_on", dst_key="target.example.test", dst_kind="host")
    ops.finish_job(db, job, "done")
    store.recompute(db)

    vuln_id = store.resolve_entity(db, project_id, vuln_key)

    # Tool hit must default to 'candidate' (NOT automatically confirmed)
    finding = workspace.finding_detail(db, project_id, vuln_id)
    assert finding["triage"]["status"] == "candidate"
    assert finding["triage"]["severity"] == "critical"

    summary = workspace.summary(db, project_id)
    assert summary["findings"] == 1
    assert summary["findings_confirmed"] == 0
    assert summary["findings_candidate"] == 1

    # Triage: Mark as False Positive
    ops.triage_finding(db, project_id=project_id, finding_id=vuln_id,
                       status="false_positive", reason="测试探针误报，响应体包含错误关键字但未实际执行",
                       actor="analyst")
    finding = workspace.finding_detail(db, project_id, vuln_id)
    assert finding["triage"]["status"] == "false_positive"
    assert "测试探针误报" in finding["triage"]["disposition_reason"]

    # Triage: Confirm finding
    ops.triage_finding(db, project_id=project_id, finding_id=vuln_id,
                       status="confirmed", reason="重现成功，确认存在", actor="lead")
    finding = workspace.finding_detail(db, project_id, vuln_id)
    assert finding["triage"]["status"] == "confirmed"


def test_retest_records_and_evidence_linkage(db):
    project_id = ops.create_project(db, "test-proj", {"include": ["*.example.test"]})
    host_id = seed_host(db, project_id=project_id, key="svc.example.test")

    res = ops.create_manual_finding(
        db, project_id=project_id, title="越权访问漏洞", severity="medium",
        target_entity_id=host_id, status="confirmed", reason="发现越权接口", actor="analyst")
    finding_id = res["finding_id"]

    # 1. First retest: still vulnerable
    ops.add_retest(db, project_id=project_id, finding_id=finding_id,
                   status="still_vulnerable", notes="第一轮复测：依然可以访问他人资源", actor="tester1")
    detail = workspace.finding_detail(db, project_id, finding_id)
    assert len(detail["retests"]) == 1
    assert detail["retests"][0]["status"] == "still_vulnerable"
    assert detail["triage"]["status"] == "confirmed"

    # 2. Second retest: resolved with evidence
    retest_raw = ops.add_raw_file(db, project_id=project_id, locator_uri="file:///retest.http", content="HTTP/1.1 403 Forbidden\n")
    retest_ev = ops.add_evidence(db, project_id=project_id, raw_file_id=retest_raw,
                                 locator={"step": "retest_verify"}, snippet="HTTP/1.1 403 Forbidden")
    ops.add_retest(db, project_id=project_id, finding_id=finding_id,
                   status="resolved", notes="第二轮复测：已返回 403 拒绝访问，修复生效",
                   evidence_id=retest_ev, actor="tester2")

    detail = workspace.finding_detail(db, project_id, finding_id)
    assert len(detail["retests"]) == 2
    assert detail["retests"][0]["status"] == "resolved"
    assert detail["triage"]["status"] == "resolved"
    assert "403" in detail["retests"][0]["notes"]


def test_generate_report_markdown_and_json(db):
    project_id = ops.create_project(db, "demo-audit", {"include": ["*.example.test"]})
    host_id = seed_host(db, project_id=project_id, key="api.example.test")

    # Add confirmed finding
    ops.create_manual_finding(
        db, project_id=project_id, title="未授权管理后台", severity="critical",
        target_entity_id=host_id, description="管理面板无需认证即可访问",
        status="confirmed", reason="人工验证成功", actor="analyst")

    # Add a research note
    ops.add_finding(db, project_id=project_id, entity_id=host_id, text="初步发现端口 8443 开放管理端点", actor="analyst")

    # Generate Markdown report
    md = workspace.generate_report(db, project_id, format="markdown")
    assert "# Magpie 安全研究与漏洞发现报告" in md
    assert "未授权管理后台" in md
    assert "CRITICAL" in md
    assert "零主动网络扫描" in md
    assert "初步发现端口 8443 开放管理端点" in md

    # Generate JSON report
    data = workspace.generate_report(db, project_id, format="json")
    assert data["project"]["name"] == "demo-audit"
    assert data["summary"]["confirmed"] == 1
    assert len(data["confirmed_findings"]) == 1
    assert len(data["research_notes"]) >= 1


def test_duplicate_finding_validation(db):
    project_id = ops.create_project(db, "test-proj", {"include": ["*.example.test"]})
    host_id = seed_host(db, project_id=project_id, key="dup.example.test")

    f1 = ops.create_manual_finding(db, project_id=project_id, title="漏洞A", severity="low",
                                  target_entity_id=host_id, status="confirmed")["finding_id"]
    f2 = ops.create_manual_finding(db, project_id=project_id, title="漏洞B", severity="low",
                                  target_entity_id=host_id, status="candidate")["finding_id"]

    # Cannot duplicate self
    with pytest.raises(store.InvariantError, match="不能标记为自身"):
        ops.triage_finding(db, project_id=project_id, finding_id=f1, status="duplicate", duplicate_of_id=f1)

    # Valid duplicate
    ops.triage_finding(db, project_id=project_id, finding_id=f2, status="duplicate",
                       duplicate_of_id=f1, reason="重复上报同一接口问题")
    detail = workspace.finding_detail(db, project_id, f2)
    assert detail["triage"]["status"] == "duplicate"
    assert detail["triage"]["duplicate_of_id"] == f1


def test_manual_finding_on_endpoint_target(db):
    from magpie.core import normalize
    project_id = ops.create_project(db, "test-proj", {"include": ["*.example.test"]})
    store.register_source(db, source_id=1, name="test-source", authority_level="verified")
    raw = ops.add_raw_file(db, project_id=project_id, locator_uri="file:///ep.txt", content="seed\n")
    evidence = ops.add_evidence(db, project_id=project_id, raw_file_id=raw, locator={"line": 1}, snippet="seed")
    job = ops.start_job(db, project_id=project_id, kind="scan", coverage="full", parser_fingerprint="p-1", actor="cli")
    ep_ident = normalize.endpoint_identity("https://app.example.test/api/v1/users", method="POST")
    ops.add_observation(db, project_id=project_id, job_id=job, evidence_id=evidence, source_id=1,
                        entity_key=ep_ident["key"], kind="endpoint", semantic_field="url",
                        value="https://app.example.test/api/v1/users")
    ops.finish_job(db, job, "done")
    store.recompute(db)

    ep_id = store.resolve_entity(db, project_id, ep_ident["key"])
    assert ep_id is not None

    res = ops.create_manual_finding(
        db, project_id=project_id, title="接口越权访问", severity="high",
        target_entity_id=ep_id, description="普通用户可遍历他人信息",
        status="confirmed", reason="重现确认", actor="analyst")

    finding_id = res["finding_id"]
    detail = workspace.finding_detail(db, project_id, finding_id)
    assert detail["target_asset"]["id"] == ep_id
    assert detail["target_asset"]["kind"] == "endpoint"
    assert "/api/v1/users" in detail["target_asset"]["display_key"]
    assert "app.example.test" in detail["target_asset"]["display_key"]
