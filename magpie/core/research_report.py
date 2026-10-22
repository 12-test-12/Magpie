"""Offline research report assembly and Markdown rendering."""
from __future__ import annotations

from . import query
from .finding_queries import finding_detail, public_value


def generate_report(conn, project_id, format="markdown"):
    proj = conn.execute("SELECT id, name, scope, status FROM projects WHERE id=?", (project_id,)).fetchone()
    if proj is None:
        raise query.QueryError("E_PROJECT_NOT_FOUND", "项目不存在")
    proj_name = proj["name"]
    from . import store
    t_now = store.now()

    kinds = query.kind_counts(conn, project_id)
    findings_total = kinds.get("vuln", 0)

    finding_rows = conn.execute(
        "SELECT id FROM entities WHERE project_id=? AND kind='vuln'"
        " AND is_current=1 AND merged_into IS NULL ORDER BY id", (project_id,))
    enriched_findings = []
    for row in finding_rows:
        finding = finding_detail(conn, project_id, row["id"], retest_limit=None)
        enriched_findings.append({
            "id": finding["id"], "key": finding["entity_key"],
            "status": finding["status"], "severity": finding["severity"],
            "title": finding["title"], "description": finding["description"],
            "disposition_reason": finding["triage"]["disposition_reason"],
            "decision_basis_missing": finding["decision_basis_missing"],
            "duplicate_of_id": finding["triage"]["duplicate_of_id"],
            "target": finding["target_asset"],
            "initial_evidence": finding["initial_evidence"],
            "retests": finding["retests"],
        })

    from . import notes as note_store
    notes = []
    cursor = None
    while True:
        page = note_store.browse(conn, project_id, cursor=cursor, limit=100)
        notes.extend(page['rows'])
        cursor = page['next_cursor']
        if not cursor:
            break

    notes = public_value(notes)
    proj_name = public_value(proj_name)

    confirmed_list = [f for f in enriched_findings if f["status"] == "confirmed"]
    candidate_list = [f for f in enriched_findings if f["status"] == "candidate"]
    resolved_list = [f for f in enriched_findings if f["status"] == "resolved"]
    fp_list = [f for f in enriched_findings if f["status"] == "false_positive"]
    dup_list = [f for f in enriched_findings if f["status"] == "duplicate"]
    inc_list = [f for f in enriched_findings if f["status"] == "inconclusive"]

    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in confirmed_list:
        s = f["severity"]
        sev_counts[s] = sev_counts.get(s, 0) + 1

    summary_metrics = {
        "findings_total": findings_total,
        "confirmed": len(confirmed_list),
        "candidates": len(candidate_list),
        "resolved": len(resolved_list),
        "false_positive": len(fp_list),
        "duplicate": len(dup_list),
        "inconclusive": len(inc_list),
        "severity_breakdown": sev_counts,
        "notes_count": len(notes),
    }

    if format == "json":
        return {
            "project": {"id": proj["id"], "name": proj_name, "scope": public_value(proj["scope"])},
            "generated_at": t_now,
            "summary": summary_metrics,
            "confirmed_findings": confirmed_list,
            "resolved_findings": resolved_list,
            "candidates": candidate_list,
            "false_positives": fp_list,
            "duplicates": dup_list,
            "inconclusive": inc_list,
            "research_notes": notes,
        }

    # Markdown format
    lines = [
        f"# Magpie 安全研究与漏洞发现报告",
        f"",
        f"> **项目名称**：{proj_name}  ",
        f"> **生成时刻**：{t_now}  ",
        f"> **数据边界**：本地离线数据 · 零主动网络扫描 · 事实与判定分离",
        f"",
        f"## 1. 执行摘要与发现总览",
        f"",
        f"| 状态分类 | 数量 | 说明 |",
        f"|---|---:|---|",
        f"| **已确认漏洞（Confirmed）** | **{len(confirmed_list)}** | 人工登记为已确认；依据及缺口见明细 |",
        f"| **待核实发现（Candidate）** | {len(candidate_list)} | 扫描器工具命中或静态候选，未完成人工判定 |",
        f"| **已修复漏洞（Resolved）** | {len(resolved_list)} | 人工登记为已修复；不代表系统验证了修复真实性 |",
        f"| **误报处置（False Positive）** | {len(fp_list)} | 人工登记为误报；依据及缺口见明细 |",
        f"| **重复发现（Duplicate）** | {len(dup_list)} | 关联至主漏洞记录 |",
        f"| **存疑未决（Inconclusive）** | {len(inc_list)} | 证据不足或环境限制无法确认 |",
        f"",
        f"### 已确认漏洞严重级别分布",
        f"",
        f"- **Critical（严重）**：{sev_counts.get('critical', 0)}",
        f"- **High（高危）**：{sev_counts.get('high', 0)}",
        f"- **Medium（中危）**：{sev_counts.get('medium', 0)}",
        f"- **Low（低危）**：{sev_counts.get('low', 0)}",
        f"- **Info（提示）**：{sev_counts.get('info', 0)}",
        f"",
        f"---",
        f"",
        f"## 2. 已确认漏洞列表（Confirmed Findings）",
        f"",
    ]

    if not confirmed_list:
        lines.append("*当前暂无已确认漏洞记录（工具命中未自动升级为确认漏洞）。*\n")
    for idx, finding in enumerate(confirmed_list, 1):
        lines.extend(_finding_markdown(finding, f"2.{idx}"))

    lines.extend([
        f"---",
        f"",
        f"## 3. 处置决策记录（误报、修复与未决）",
        f"",
    ])
    disposed = resolved_list + fp_list + dup_list + inc_list + candidate_list
    if not disposed:
        lines.append("*暂无其他处置决策记录。*\n")
    for idx, finding in enumerate(disposed, 1):
        lines.extend(_finding_markdown(finding, f"3.{idx}"))

    lines.extend([
        f"---",
        f"",
        f"## 4. 研究线索与备忘笔记（Research Notes）",
        f"",
    ])
    if not notes:
        lines.append("*暂无关联研究笔记。*\n")
    else:
        for n in notes:
            lines.append(f"- `[{n['created_at']}]`（对象：`{n['entity_key']}`）")
            lines.append(f"  > {str(n['text'])}")
            lines.append(f"  - 人工修订版本：{n['version']}；原始笔记证据：#{n['evidence_id']}")
            for ref in n['evidence_links']:
                lines.append(f"  - 关联研究证据：#{ref['id']} · `{ref['entity_key']}` · `{ref['locator']}` · SHA-256 `{ref['sha256']}`")
            lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 5. 方法学、数据源与局限性声明",
        f"",
        f"1. **零主动网络扫描**：本报告全部数据、接口识别与漏洞发现均基于用户显式导入的文件（如 HAR、Nuclei JSONL、OpenAPI、JS 清单等）或人工记录，系统不向任何外部网络地址发起主动探测。",
        f"2. **工具命中与确认分立**：自动化工具输出的命中仅记为观察事实与候选，不直接等同于已被利用或真实漏洞；必须经过人工审查与证据链路校验。",
        f"3. **人工复测边界**：网络超时、凭据过期或目标不可达不能推出漏洞已修复。未关联复测证据时仅有人工说明；证据引用也不代表系统验证了证据充分性。",
        f"4. **来源与缺口**：本报告列出已有的来源定位、片段哈希及证据引用。原文可能未归档或不可用；历史缺少判定理由的记录明确提示，不补造依据。",
        f"",
    ])

    return "\n".join(lines)


def _finding_markdown(finding, number):
    f = finding
    lines = [f"### {number} [{f['status'].upper()} / {f['severity'].upper()}] {f['title']}"]
    lines.append(f"- **漏洞标识**：`{f['key']}`")
    if f.get("target"):
        lines.append(f"- **目标资产**：`{f['target']['kind']}` · `{f['target']['display_key']}`")
    if f.get("duplicate_of_id"):
        lines.append(f"- **主漏洞**：#{f['duplicate_of_id']}")
    if f.get("disposition_reason"):
        lines.append(f"- **判定理由/分析说明**：{f['disposition_reason']}")
    if f.get("decision_basis_missing"):
        lines.append("- **依据缺口**：历史记录未填写判定理由，不能据此视为已完成证据核实。")
    if f.get("description"):
        lines.append(f"- **详细描述**：\n  > {f['description']}")
    ev = f.get("initial_evidence")
    if ev:
        lines.append(f"- **首次发现证据**：#{ev['id']} · `{ev.get('locator_uri')}`")
        lines.append(f"  - 片段定位：`{ev.get('locator')}` · SHA-256: `{ev.get('sha256')}`")
    if f['status'] == 'resolved' and not any(rt['status'] == 'resolved' for rt in f.get('retests', [])):
        lines.append("- **复测缺口**：当前为人工已修复判定，尚无已修复复测记录。")
    for rt in f.get("retests", []):
        lines.append(f"- **人工复测** [{rt['created_at']}] {rt['actor']} · **{rt['status']}**")
        lines.append(f"  - 说明：{rt['notes']}")
        if rt.get("sha256"):
            lines.append(f"  - 关联复测证据：#{rt['evidence_id']} · `{rt.get('locator_uri')}` · `{rt.get('locator')}` · SHA-256 `{rt['sha256']}`")
        else:
            lines.append("  - 未关联复测证据，仅有人工说明；仍需检查证据缺口。")
    lines.append("")
    return lines
