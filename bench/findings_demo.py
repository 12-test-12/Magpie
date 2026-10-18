"""Create an isolated synthetic workspace for Findings lifecycle, triage, retests and reporting.

Run: .venv-dev/Scripts/python.exe -m bench.findings_demo
Then: .venv-dev/Scripts/python.exe dev.py --workspace findings-workspace
The generator refuses to overwrite an existing workspace.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from magpie.core import normalize, ops, store, workspace

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".magpie-dev" / "findings-workspace"


def build() -> None:
    db_path = DATA / "magpie.db"
    if db_path.exists():
        raise SystemExit(f"Fixture database already exists; refusing to overwrite: {db_path}")

    DATA.mkdir(parents=True, exist_ok=True)
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        project_id = ops.create_project(conn, "Finding 闭环与处置复测 · 合成验收",
                                        {"include": ["*.example.test"]})

        # 1. Seed assets: host and endpoint
        store.register_source(conn, source_id=1, name="synthetic-recon", authority_level="verified")
        raw_recon = ops.add_raw_file(conn, project_id=project_id, locator_uri="file:///recon.json",
                                     content='{"host":"portal.example.test","endpoint":"/api/login"}\n')
        ev_host = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw_recon,
                                  locator={"line": 1, "path": "host"}, snippet="portal.example.test")
        ev_ep = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw_recon,
                                 locator={"line": 1, "path": "endpoint"}, snippet="/api/login")

        job_recon = ops.start_job(conn, project_id=project_id, kind="scan", coverage="full",
                                  parser_fingerprint="p-recon", actor="recon-engine")
        host_key = "portal.example.test"
        ops.add_observation(conn, project_id=project_id, job_id=job_recon, evidence_id=ev_host,
                            source_id=1, entity_key=host_key, kind="host", semantic_field="hostname",
                            value=host_key)

        ep_ident = normalize.endpoint_identity("https://portal.example.test/api/login", method="POST")
        ep_key = ep_ident["key"]
        ops.add_observation(conn, project_id=project_id, job_id=job_recon, evidence_id=ev_ep,
                            source_id=1, entity_key=ep_key, kind="endpoint", semantic_field="url",
                            value="https://portal.example.test/api/login")
        ops.add_observation(conn, project_id=project_id, job_id=job_recon, evidence_id=ev_ep,
                            source_id=1, entity_key=ep_key, kind="endpoint", semantic_field="method",
                            value="POST")
        ops.finish_job(conn, job_recon, "done")
        store.recompute(conn)

        host_id = store.resolve_entity(conn, project_id, host_key)
        ep_id = store.resolve_entity(conn, project_id, ep_key)

        # 2. Tool findings (Nuclei tool candidate)
        store.register_source(conn, source_id=2, name="nuclei@1", authority_level="verified")
        raw_tool = ops.add_raw_file(conn, project_id=project_id, locator_uri="file:///nuclei_out.jsonl",
                                    content='{"template":"cve-2023-99999","matched":"https://portal.example.test/debug"}\n')
        ev_tool = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw_tool,
                                   locator={"line": 1}, snippet="CVE-2023-99999 debug endpoint")
        job_tool = ops.start_job(conn, project_id=project_id, kind="scan", coverage="partial",
                                 parser_fingerprint="p-nuclei", actor="nuclei")
        vuln1_key = normalize.vuln_key("cve-2023-99999", "portal.example.test")
        ops.add_observation(conn, project_id=project_id, job_id=job_tool, evidence_id=ev_tool,
                            source_id=2, entity_key=vuln1_key, kind="vuln", semantic_field="title",
                            value="CVE-2023-99999 敏感调试端点暴露")
        ops.add_observation(conn, project_id=project_id, job_id=job_tool, evidence_id=ev_tool,
                            source_id=2, entity_key=vuln1_key, kind="vuln", semantic_field="severity",
                            value="medium")
        ops.add_observation(conn, project_id=project_id, job_id=job_tool, evidence_id=ev_tool,
                            source_id=2, entity_key=vuln1_key, kind="vuln", semantic_field="state",
                            value="open")
        ops.add_relation(conn, project_id=project_id, job_id=job_tool, evidence_id=ev_tool,
                         source_id=2, src_key=vuln1_key, src_kind="vuln", rel="found_on",
                         dst_key=host_key, dst_kind="host")
        ops.finish_job(conn, job_tool, "done")
        store.recompute(conn)

        tool_finding_id = store.resolve_entity(conn, project_id, vuln1_key)

        # 3. Tool candidate triaged as false positive
        ops.triage_finding(conn, project_id=project_id, finding_id=tool_finding_id,
                           status="false_positive",
                           reason="测试探针误报：调试端点已通过前置反向代理拦截，仅内部白名单可访问，外部返回 404",
                           actor="security-analyst", expected_version=workspace.finding_detail(conn, project_id, tool_finding_id)['triage']['version'])

        # 4. Manual finding: SQL injection on login endpoint
        res = ops.create_manual_finding(
            conn, project_id=project_id,
            title="登录接口 SQL 注入漏洞",
            severity="critical",
            target_entity_id=ep_id,
            description="POST /api/login 的 username 参数未进行转义或参数化处理，导致布尔盲注与报错注入。",
            status="confirmed",
            reason="人工审计请求包并成功重现，利用报错注入提取数据库版本号",
            actor="researcher")
        manual_finding_id = res["finding_id"]

        # 4b. Another manual finding: Unauthenticated admin endpoint (kept confirmed)
        res2 = ops.create_manual_finding(
            conn, project_id=project_id,
            title="管理控制台默认凭据与未授权访问",
            severity="high",
            target_entity_id=host_id,
            description="8443 端口管理后台存在默认管理员口令，且部分监控指标接口无需鉴权。",
            status="confirmed",
            reason="直接使用 admin:admin 成功登录控制台并访问敏感配置",
            actor="researcher")
        confirmed_finding_id = res2["finding_id"]

        # 5. Add research notes on host and endpoint
        ops.add_finding(conn, project_id=project_id, entity_id=host_id,
                        text="主机暴露 443 与 8443，8443 疑似管理控制台",
                        actor="researcher")
        ops.add_finding(conn, project_id=project_id, entity_id=ep_id,
                        text="登录接口未开启验证码与频率限制，存在暴力破解可能",
                        actor="researcher")

        # 6. Retests on manual finding
        # Round 1: Still vulnerable
        ops.add_retest(conn, project_id=project_id, finding_id=manual_finding_id,
                       status="still_vulnerable",
                       notes="第一轮人工复测：提交单引号 payload 依然返回 SQL 语法错误，尚未修复。",
                       actor="qa-tester", expected_version=workspace.finding_detail(conn, project_id, manual_finding_id)['triage']['version'])

        # Round 2: Resolved with evidence
        raw_retest = ops.add_raw_file(conn, project_id=project_id, locator_uri="file:///retest_login.http",
                                      content="POST /api/login HTTP/1.1\nHost: portal.example.test\n\nHTTP/1.1 400 Bad Request\n{\"error\":\"Invalid characters\"}\n")
        ev_retest = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw_retest,
                                     locator={"step": "retest_verification"}, snippet="HTTP/1.1 400 Bad Request")
        ops.add_retest(conn, project_id=project_id, finding_id=manual_finding_id,
                       status="resolved",
                       notes="第二轮人工复测：后端已替换为 PreparedStatement 参数化查询，输入特殊字符返回校验错误，漏洞已解决。",
                       evidence_id=ev_retest,
                       actor="security-analyst", expected_version=workspace.finding_detail(conn, project_id, manual_finding_id)['triage']['version'])

        store.recompute(conn)
        store.assert_integrity(conn)

        print(f"Synthetic findings workspace ready: {DATA}")
        print("Project: Finding 闭环与处置复测 · 合成验收")
        print(f"- Tool Finding ID: {tool_finding_id} (triaged as false_positive)")
        print(f"- Manual Finding ID: {manual_finding_id} (confirmed -> resolved after 2 retests)")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
