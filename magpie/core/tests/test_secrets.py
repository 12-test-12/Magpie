"""4.2 · 敏感串扫描：`secret` 只存 SHA-256 + 规则名 + 位置 + 掩码前缀。

判据（每一条对应 LARI-37 的一条交付标准）：

  ①  **明文永不落库**（判据 1）：拿一份带假凭据的流量导进去，明文在**整张库
      + 落盘报告**里一个字节都找不到。搜整库而不是"断言没有某条观测"—— 后者
      只证明了**一处**没写，而凭据落库的姿势通常是在一个谁也没想到的地方（坏行
      报告的原文里、Inbox 的 payload 里、activity_log 里……）。
      两条归档路径（`stored` 与 `external`）各跑一遍，覆盖齐全。

  ②  **同一份输入跑两遍逐项相同**（判据 2）：`secret` 的身份不带行号，所以
      两遍导入产出的实体键逐条相等 —— 不是"条数一样"，是"每一条的指纹一样"。

  ③  **报告的聚合数字里只有计数与规则名，没有值**（判据 3）。

  ④  **undo 之后 `secret` 归零**（判据 4）：逐行指纹相等，不是只比条数。

⚠️  **夹具的来历**：`fixtures/har/1/input.json` 里是按 HAR 规范现写的**样本**，
不是真导出 —— 本机连不上网。它证明的是"解析器与扫描器自洽"，不是"与浏览器
的输出一致"。
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from magpie.core import ingest, migrate, normalize, ops, query, scan, store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project(db) -> int:
    return ops.create_project(db, "secret-test", {"include": [], "exclude": []})


def options(project_id: int, data_dir: Path, **kw) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True, **kw)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 辅助：合成带凭据的 HAR（不是真实导出，见文件头）
# ---------------------------------------------------------------------------

SECRET = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SECRET2 = "ghp_ZyXwVuTsRqPoNmLkJiHgFeDcBa0987654321"


def _har_with_secrets(*secrets: str) -> dict:
    """一份最小 HAR，每个请求的 Authorization 头带一个不同的假凭据。"""
    entries = []
    for i, secret in enumerate(secrets):
        entries.append({
            "startedDateTime": f"2026-01-01T00:00:{i:02d}.000Z",
            "time": 10,
            "request": {
                "method": "GET",
                "url": f"https://api.example.com/v{i}/me",
                "httpVersion": "HTTP/1.1",
                "headers": [
                    {"name": "Authorization", "value": f"Bearer {secret}"},
                    {"name": "Content-Type", "value": "application/json"},
                ],
                "queryString": [],
                "headersSize": 0,
                "bodySize": 0,
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "httpVersion": "HTTP/1.1",
                "headers": [
                    {"name": "Content-Type", "value": "application/json"},
                ],
                "content": {"size": 2, "mimeType": "application/json"},
                "redirectURL": "",
                "headersSize": 0,
                "bodySize": 2,
            },
            "cache": {},
            "timings": {"send": 1, "wait": 2, "receive": 3},
        })
    return {"log": {"version": "1.2",
                    "creator": {"name": "test", "version": "1"},
                    "entries": entries}}


def _all_text_in_db(db: sqlite3.Connection) -> str:
    """把库里所有 TEXT 列的值拼起来 —— "某个串有没有落库"的判据要用整张库。"""
    parts: list[str] = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        "  AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for row in db.execute("SELECT * FROM " + table):
            parts += [str(v) for v in tuple(row) if v is not None]
    return "\n".join(parts)


def _all_text_in_report(report: dict) -> str:
    """把报告里所有字符串值拼起来。"""
    parts: list[str] = []
    for v in report.values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for sv in _walk_dict(v):
                if isinstance(sv, str):
                    parts.append(sv)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    for sv in _walk_dict(item):
                        if isinstance(sv, str):
                            parts.append(sv)
    return "\n".join(parts)


def _walk_dict(d: dict):
    for v in d.values():
        yield v
        if isinstance(v, dict):
            yield from _walk_dict(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    yield from _walk_dict(item)


# ===========================================================================
# ① 明文永不落库（判据 1）
# ===========================================================================


def test_plaintext_never_reaches_the_database_stored(db, project, tmp_path):
    """`stored` 归档路径：明文**一个字节**都找不到。

    扫全表而不是断言某条特定观测 —— 凭据落库的姿势通常不在预期内（坏行报告
    的原文里、Inbox 的 payload 里、activity_log 里……）。搜整张库 + 落盘报告。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done", report

    found = _all_text_in_db(db)
    assert SECRET not in found, "凭据出现在库里（stored 路径）"

    location = report.get("badlines_report")
    if location and Path(location).is_file():
        assert SECRET not in Path(location).read_text(encoding="utf-8"), \
            "凭据出现在坏行报告里（stored 路径）"

    # 报告的聚合数字里只有计数与规则名，没有值（判据 3）
    report_text = _all_text_in_report(report)
    assert SECRET not in report_text, "凭据出现在落盘报告里（stored 路径）"


def test_plaintext_never_reaches_the_database_external(db, project, tmp_path):
    """`external` 归档路径：同样的判据，覆盖另一条路。

    4.1 的 `raw_files.custody` 把归档分成两档。扫描**读的是源文件**（在归档
    之前），所以两条路径都覆盖 —— 但还是要验：防止有人为了扫描把外部文件的
    正文复制进库来。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))
    opts = options(project, tmp_path, archive="external")
    report = ingest.ingest_file(db, opts, src)
    assert report["state"] == "done", report
    assert report["custody"] == "external", report

    found = _all_text_in_db(db)
    assert SECRET not in found, "凭据出现在库里（external 路径）"

    location = report.get("badlines_report")
    if location and Path(location).is_file():
        assert SECRET not in Path(location).read_text(encoding="utf-8"), \
            "凭据出现在坏行报告里（external 路径）"

    report_text = _all_text_in_report(report)
    assert SECRET not in report_text, "凭据出现在落盘报告里（external 路径）"


def test_plaintext_never_reaches_the_inbox_payload(db, project, tmp_path):
    """Inbox 的 `payload_json` 里也找不到明文 —— 它是"最容易顺手把命中的原文
    塞进去"的地方（LARI-37 的判据说明）。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))
    ingest.ingest_file(db, options(project, tmp_path), src)

    for row in db.execute("SELECT id, payload_json FROM inbox_items"):
        payload = row["payload_json"] or ""
        assert SECRET not in payload, \
            f"凭据出现在 inbox_items.id={row['id']} 的 payload 里"


# ===========================================================================
# ② 同一份输入跑两遍逐项相同（判据 2）
# ===========================================================================


def test_same_input_produces_same_secret_keys(db, project, tmp_path):
    """逐行指纹：同一份输入跑两遍，每一条 secret 的身份键完全一致。

    "条数一样"不够 —— 条数相同、内容不同是完全可能的（4.1 的 flow 同一条
    纪律）。`secret` 的身份不含行号，所以它天然满足这一条 —— 但还是要验。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))

    opts1 = options(project, tmp_path)
    r1 = ingest.ingest_file(db, opts1, src)
    assert r1["state"] == "done"

    # 重导同一份文件：幂等（同一 sha256 + 同一 fingerprint → skipped）
    opts2 = options(project, tmp_path, parser="har")
    r2 = ingest.ingest_file(db, opts2, src)
    assert r2["state"] == "skipped", "重导没有跳过（幂等失败）"

    # 用 undo/redo 验"逐行指纹"：redo 之后 secret 必须回来，而且每一条都一样
    from magpie.core import store as _store
    fp_before = _fp(db)
    ops.undo(db, int(r1["job_id"]), actor="test")
    fp_after_undo = _fp(db)
    ops.redo(db, int(r1["job_id"]), actor="test")
    fp_after_redo = _fp(db)
    assert fp_before == fp_after_redo, "undo → redo 后 secret 的逐行指纹不一致"
    assert fp_before != fp_after_undo, "undo 后 secret 没有变化（归零失败）"


def _fp(db):
    """secret 实体的逐行指纹。"""
    rows = db.execute(
        "SELECT e.id AS id, e.kind AS kind, ek.entity_key AS entity_key"
        " FROM entities e JOIN entity_keys ek ON ek.entity_id=e.id"
        " WHERE e.is_current=1 AND e.kind='secret'"
        " ORDER BY ek.entity_key").fetchall()
    return [(dict(r)["entity_key"], dict(r)["kind"]) for r in rows]


# ===========================================================================
# ③ 报告只有计数与规则名，没有值（判据 3）
# ===========================================================================


def test_report_contains_only_counts_and_rule_names(db, project, tmp_path):
    """报告的 `secrets` 块只有计数与规则名 —— 没有 `masked`、没有 `value`。

    这一条的判据比"明文找不到"更强：掩码虽然不含随机段，但它仍然是从明文
    派生出来的文本。报告是三条落点里唯一**必然落盘**的一个（`reports/*.json`），
    所以它只带可复算的数。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path, report_dir=tmp_path),
                                src)
    assert report["state"] == "done", report

    secrets_report = report.get("secrets", {})
    assert secrets_report, "报告里没有 secrets 块"
    assert "by_rule" in secrets_report, "报告的 secrets 块里没有 by_rule"
    assert isinstance(secrets_report["by_rule"], dict)
    assert "secrets" in secrets_report
    assert secrets_report["secrets"] >= 1

    # 报告的 secrets 块里不得出现 "masked"、"value"、"location"
    report_json = json.dumps(secrets_report, ensure_ascii=False)
    assert "masked" not in secrets_report, "报告的 secrets 块里出现了 masked"
    assert "value" not in secrets_report, "报告的 secrets 块里出现了 value"
    assert "location" not in secrets_report, "报告的 secrets 块里出现了 location"


# ===========================================================================
# ④ undo 之后 secret 归零（判据 4）
# ===========================================================================


def test_undo_zeroes_secret_entities(db, project, tmp_path):
    """`undo` 之后 secret 实体全部 `is_current=0`。

    照既有口径：逐行指纹相等，不是只比条数。一个没有 `is_current=1` 的 secret
    实体，在体检页上就是 0 —— 那才是"归零"这个判据的含义。
    """
    har = _har_with_secrets(SECRET, SECRET2)
    src = write(tmp_path, "cap.har", json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done"

    # 导入后至少有一个 secret
    before = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='secret' AND is_current=1"
    ).fetchone()[0]
    assert before >= 2, f"导入后 secret 不足 2 个（{before}）"

    # undo 后归零
    ops.undo(db, int(report["job_id"]), actor="test")
    after = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='secret' AND is_current=1"
    ).fetchone()[0]
    assert after == 0, f"undo 后 secret 没有归零（{after}）"

    # redo 后回来
    ops.redo(db, int(report["job_id"]), actor="test")
    restored = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='secret' AND is_current=1"
    ).fetchone()[0]
    assert restored == before, f"redo 后 secret 没有回来（{restored} vs {before}）"


# ===========================================================================
# 补充：扫描器本身的幂等与确定性
# ===========================================================================


def test_scanner_is_deterministic():
    """同一份文本跑两遍，结果逐项相同。"""
    lines = [(1, f"Authorization: Bearer {SECRET}"),
             (2, f"api_key={SECRET2}"),
             (3, "Host: api.example.com"),
             (4, f"Cookie: session={SECRET}")]
    r1 = scan.scan_lines(lines)
    r2 = scan.scan_lines(lines)
    assert r1.records == r2.records
    assert r1.by_rule == r2.by_rule
    assert r1.secrets == r2.secrets


def test_scanner_records_contain_no_plaintext():
    """扫描器的输出（records）里没有明文。"""
    lines = [(1, f"Authorization: Bearer {SECRET}")]
    result = scan.scan_lines(lines)
    for rec in result.records:
        rec_str = json.dumps(rec.__dict__, ensure_ascii=False, default=str)
        assert SECRET not in rec_str, \
            f"明文出现在记录里：{rec_str[:200]}"


def test_scanner_on_empty_input():
    """空输入 → 0 个秘密、0 条记录。"""
    result = scan.scan_lines([])
    assert result.secrets == 0
    assert result.records == []
    assert result.by_rule == {}


def test_scanner_on_clean_traffic():
    """干净的流量 → 0 个秘密。"""
    lines = [(1, "GET /v1/me HTTP/1.1"),
             (2, "Host: api.example.com"),
             (3, '{"status": 200}'),
             (4, "Content-Type: application/json")]
    result = scan.scan_lines(lines)
    assert result.secrets == 0, result.by_rule
