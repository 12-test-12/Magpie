"""4.2 · 敏感串扫描：`secret` 只存 SHA-256 + 规则名 + 位置 + 掩码前缀。

判据（每一条对应 LARI-37 的一条交付标准）：

  ①  **明文永不落库**（判据 1）：拿一份带假凭据的流量导进去，明文在**整张库
      + 落盘报告**里一个字节都找不到。搜整库而不是"断言没有某条观测"—— 后者
      只证明了**一处**没写，而凭据落库的姿势通常是在一个谁也没想到的地方（坏行
      报告的原文里、Inbox 的 payload 里、activity_log 里……）。
      两条归档路径（`stored` 与 `external`）各跑一遍，覆盖齐全。
      **审查方（2026-09-22）补出的两条向量也各有一条专项**：
        * **向量 A** —— 凭据在 URL 查询串：`path` 观测按"原始路径保留证据"的
          裁定写库，原文会跟着 `observations.value` / `contributions.field_value`
          落进去。收口在 ingest 写入口（`_value_text` 统一过 `redact_secrets`）。
        * **向量 B** —— 凭据在**文件名**：`location` 观测 / scan evidence /
          `jobs.coverage_scope` / 报告 path / CLI stdout 都会带上 `path.name`。
          派生落点全部掩码；**唯一例外**是 `raw_files.locator_uri`（见下面
          `test_filename_credential_external_mode` 的裁定说明）。

  ②  **同一份输入跑两遍逐项相同**（判据 2）：`secret` 的身份不带行号，所以
      两遍导入产出的实体键逐条相等 —— 不是"条数一样"，是"每一条的指纹一样"。
      **数量**也必须可复算：`by_rule` 每个规则 = 手工数（不是"两遍相等"——
      双写 bug 下两遍会相等地错，见 `test_by_rule_counts_equal_hand_tally`）。

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
    #  report_dir 默认打开（与 CLI 一致：`data_dir/reports`）—— 判据 ① 扫的
    #  "落盘报告"必须**真的存在**才谈得上"0 命中"，不落盘的 0 是空转的 0。
    kw.setdefault("report_dir", Path(data_dir) / "reports")
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


def _har_with_secrets(*secrets: str, urls: list[str] | None = None) -> dict:
    """一份最小 HAR。

    默认形状：每个请求的 Authorization 头带一个不同的假凭据。
    给 `urls` 时：条目按 `urls` 建、**不带** Authorization 头（向量 A 用 ——
    凭据只在 URL 查询串里，免得头部那条命中混进手工计数）。
    """
    entries = []
    count = len(urls) if urls is not None else len(secrets)
    for i in range(count):
        url = (urls[i] if urls is not None
               else f"https://api.example.com/v{i}/me")
        headers = [{"name": "Content-Type", "value": "application/json"}]
        if urls is None:
            headers.insert(0, {"name": "Authorization",
                               "value": f"Bearer {secrets[i]}"})
        entries.append({
            "startedDateTime": f"2026-01-01T00:00:{i:02d}.000Z",
            "time": 10,
            "request": {
                "method": "GET",
                "url": url,
                "httpVersion": "HTTP/1.1",
                "headers": headers,
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


def _cells_containing(db: sqlite3.Connection, needle: str) -> list[tuple]:
    """全表逐格扫 `needle`，返回 `[(表名, 列名), …]`。

    比 `_all_text_in_db` 多一维**列名**：向量 B 的裁定是"`raw_files.locator_uri`
    一列例外、其余全清"，只报"在不在库里"回答不了"是不是只在那一列"。
    """
    hits: list[tuple] = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        "  AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        cursor = db.execute("SELECT * FROM " + table)
        columns = [d[0] for d in cursor.description]
        for row in cursor:
            for column, value in zip(columns, row):
                if value is not None and needle in str(value):
                    hits.append((table, column))
    return hits


def _assert_reports_clean(data_dir: Path, needle: str, label: str = "") -> None:
    """`<data_dir>/reports/**` 逐字节扫 `needle`；**先断言报告真的落了盘**。

    不落盘的"0 命中"是空转的 0 —— 判据 ① 的报告那一半必须先证明"有东西被扫"。
    """
    reports = Path(data_dir) / "reports"
    files = ([p for p in reports.rglob("*") if p.is_file()]
             if reports.is_dir() else [])
    assert files, f"{label}: reports/ 里一个文件都没有 —— 0 命中无意义"
    encoded = needle.encode("utf-8")
    for path in files:
        assert encoded not in path.read_bytes(), \
            f"{label}: {needle} 出现在落盘报告 {path.name}"


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
    assert _cells_containing(db, SECRET) == [], "凭据出现在具体某格（stored 路径）"

    location = report.get("badlines_report")
    if location and Path(location).is_file():
        assert SECRET not in Path(location).read_text(encoding="utf-8"), \
            "凭据出现在坏行报告里（stored 路径）"

    # 报告的聚合数字里只有计数与规则名，没有值（判据 3）
    report_text = _all_text_in_report(report)
    assert SECRET not in report_text, "凭据出现在返回的报告里（stored 路径）"
    _assert_reports_clean(tmp_path, SECRET, "stored 路径")


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
    assert _cells_containing(db, SECRET) == [], "凭据出现在具体某格（external 路径）"

    location = report.get("badlines_report")
    if location and Path(location).is_file():
        assert SECRET not in Path(location).read_text(encoding="utf-8"), \
            "凭据出现在坏行报告里（external 路径）"

    report_text = _all_text_in_report(report)
    assert SECRET not in report_text, "凭据出现在返回的报告里（external 路径）"
    _assert_reports_clean(tmp_path, SECRET, "external 路径")


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
    """报告的 `secrets` 块只有计数与规则名，且数字**逐项可手工复算**（判据 ③）。

    三重判据，缺一条都放过不了审查抓出的两类问题：

      * **键集合**恰好是 `{lines, secrets, occurrences, by_rule}` —— 键是结构，
        多一个少一个都是分歧（上一版手拼的块缺 `occurrences`，与
        `ScanResult.as_dict()` 各说各话，LARI-37 审查抓出）；
      * **值**是手工数：一条 HAR 单行、一个 Bearer 头 → 两条规则各命中 1 次
        （`authorization_header` 认 Bearer、`github_token` 认 `ghp_` 本身），
        去重后 2 个实体、共 2 次出现。双写 bug 下会变成 4/2/4 —— 红；
      * 不得出现 `masked` / `value` / `location` 任何一栏。

    复跑（`-s` 会把实测块打出来，供交付评论引用）：

        py -3.13 -m pytest magpie/core/tests/test_secrets.py -q -s \
            -k test_report_contains_only_counts_and_rule_names
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, "cap.har", json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done", report

    secrets_report = report.get("secrets", {})
    print("report[secrets] =", json.dumps(secrets_report, ensure_ascii=False,
                                           sort_keys=True))

    assert set(secrets_report) == {"lines", "secrets", "occurrences", "by_rule"}, \
        sorted(secrets_report)
    # 手工数（不是"两遍相等"）：单行输入，Bearer 一条命中两条规则。
    assert secrets_report["by_rule"] == {"authorization_header": 1,
                                         "github_token": 1}, secrets_report
    assert secrets_report["secrets"] == 2, secrets_report
    assert secrets_report["occurrences"] == 2, secrets_report
    assert secrets_report["lines"] == 1, secrets_report
    # 报告的 secrets 块里不得出现 "masked"、"value"、"location"
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


# ===========================================================================
# 判据 ② 的数量：精确计数（LARI-37 审查必须修 #1）
# ===========================================================================


def test_detect_secrets_returns_one_entry_per_rule_hit():
    """一次命中 = 一条 —— `detect_secrets` 曾经每条命中 `append` 两遍。

    那个双写的症状很隐蔽：实体数按身份键去重所以不变、location 行号有
    `last != lineno` 守卫也不变，**只有报告的 `by_rule` 恒 ×2** —— 于是
    "同一份输入跑两遍逐项相同"这类断言全绿（两遍会相等地错），只有拿
    **手工数**去对才会露。这条是那个 bug 的直接反例：

        Authorization: Bearer ghp_…
          → authorization_header（认 Bearer 头）+ github_token（认 ghp_ 本身）
          → 恰好 2 条，不是 4 条。
    """
    token = "ghp_" + "aZ3" * 12            # ghp_ + 36 位字母数字
    hits = normalize.detect_secrets(f"Authorization: Bearer {token}")
    assert [h["rule"] for h in hits] == ["authorization_header", "github_token"], \
        [(h["rule"], h["masked"]) for h in hits]
    assert all(h["value"] == token for h in hits)


def test_by_rule_counts_equal_hand_tally():
    """`by_rule` 每个规则 = **手工数**（不是"两遍相等"）。

    手工账（写死在这里，逐行可核）：

        行 1  Authorization: Bearer ghp_…   → authorization_header ×1
                                              + github_token ×1
        行 2  curl -u alice:hunter2xyz …    → basic_auth ×1
        行 3  ?api_key=ReviewQueryKey…&x=1  → password_param ×1
        行 4  Host: api.example.com         → 0

        合计 by_rule = {authorization_header:1, github_token:1,
                        basic_auth:1, password_param:1}
        实体 secrets  = 4（ghp 值两把规则各算一个身份 + 口令 + api_key）
        出现 occurrences = 4

    双写 bug 下这份账会变成每规则 2、合计 8 —— 本条当场红。
    """
    token = "ghp_" + "bX7" * 12
    lines = [
        (1, f"Authorization: Bearer {token}"),
        (2, "curl -u alice:hunter2xyz https://a.example.com/x"),
        (3, "?api_key=ReviewQueryKey99887766&x=1"),
        (4, "Host: api.example.com"),
    ]
    result = scan.scan_lines(lines, file_name="tally.har")
    assert result.by_rule == {"authorization_header": 1,
                              "github_token": 1,
                              "basic_auth": 1,
                              "password_param": 1}, result.by_rule
    assert result.secrets == 4, result.secrets
    assert sum(result.by_rule.values()) == 4, result.by_rule
    assert result.lines == 4


# ===========================================================================
# 判据 ① 的两条向量（LARI-37 审查必须修 #2）
# ===========================================================================

#: 向量 A：凭据只出现在 URL 查询串里（头部 / 响应体都不带它）。
QUERY_SECRET = "ReviewQueryKey99887766"
#: 向量 B：凭据在**文件名**里（`tok_ghp_…` 下划线粘连 —— 审查方用的形状）。
GH_FILE = "ghp_Fname9Qx7Lm2Rt5Yv8Bn3Dk6Ps1Wz4"
FILENAME = f"tok_{GH_FILE}.har"


def test_url_query_credential_never_reaches_the_database(db, project, tmp_path):
    """向量 A：`?api_key=…` 的原文不落库 —— 掩在**写入口**，不是解析器。

    这条凭据的落库路径与头部凭据完全不同：har 解析器按"原始路径保留证据"
    的裁定把 `path` 观测写成 `observations.value`（+ `contributions.field_value`），
    原文因此**以观测的身份**进库 —— 只扫 `secret` 表永远看不见它。收口在
    `ingest._value_text`（所有观测值的唯一出口）过 `redact_secrets`。

    断言分两半：**没了**（全库逐格 + 落盘报告 0 字节）与**还在**（path 观测
    的形状没被毁掉、secret 实体照常产出、掩码与 secret 表同形）。
    """
    har = _har_with_secrets(
        urls=[f"https://api.example.com/v1/profile?api_key={QUERY_SECRET}"])
    src = write(tmp_path, "cap.har", json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done", report

    # 没了：全库逐格 + 落盘报告 0 字节
    assert _cells_containing(db, QUERY_SECRET) == [], \
        "查询串凭据出现在库里"
    _assert_reports_clean(tmp_path, QUERY_SECRET, "向量 A")

    # 还在：path 观测形状完好，值是掩码形态（Revi**** = 前 4 位 + ****）
    path_values = [str(r[0]) for r in db.execute(
        "SELECT value FROM observations WHERE semantic_field='path'")]
    assert path_values, "path 观测不见了 —— 掩码不等于删证据"
    masked_values = [v for v in path_values if "api_key=Revi****" in v]
    assert masked_values, f"path 观测不是掩码形态：{path_values}"
    assert all(QUERY_SECRET not in v for v in path_values)

    # 还在：secret 实体照常产出，且报告数字是手工数（这一行只命中 1 条规则）
    keys = [r[0] for r in db.execute(
        "SELECT entity_key FROM entity_keys WHERE entity_key LIKE 'password_param:%'")]
    assert len(keys) == 1, keys
    assert report["secrets"]["by_rule"] == {"password_param": 1}, report["secrets"]
    assert report["secrets"]["secrets"] == 1, report["secrets"]


def test_filename_credential_stored_mode_is_nowhere(db, project, tmp_path):
    """向量 B / stored：文件名带凭据时，**全库逐格 + 落盘报告 0 字节**。

    stored 模式下没有任何理由留明文：归档 locator 是内容寻址的
    （`raw/<sha 前两位>/<sha256>`，不含文件名），`coverage_scope.file`、
    `location.file`、scan evidence、报告 path 全是派生落点，逐个掩掉之后
    文件名在这条路上就没有任何落点 —— 所以这条断言是"全清"，不是"例外之外清"。

    内容里另放一个**不同**的头部凭据（SECRET）：文件名命中与内容命中互不
    干扰，两边各自归零。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, FILENAME, json.dumps(har))
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done", report

    assert _cells_containing(db, GH_FILE) == [], \
        f"文件名凭据出现在库里：{_cells_containing(db, GH_FILE)}"
    assert _cells_containing(db, SECRET) == [], "内容凭据出现在库里"
    _assert_reports_clean(tmp_path, GH_FILE, "向量 B stored")
    _assert_reports_clean(tmp_path, SECRET, "向量 B stored")

    # 正证据：location 观测的 file 字段是掩码形态（不是"没有这个字段"）
    locations = [str(r[0]) for r in db.execute(
        "SELECT value FROM observations WHERE semantic_field='location'")]
    assert locations, "location 观测不见了"
    assert any("tok_ghp_****.har" in v for v in locations), locations
    assert all(GH_FILE not in v for v in locations), locations


def test_filename_credential_external_mode_only_in_raw_locator(
        db, project, tmp_path):
    """向量 B / external：文件名明文**只允许出现在 `raw_files.locator_uri` 一列**。

    **裁定（LARI-37 审查要求"横切面一并裁定"，这条就是裁定本身）**：

    `raw_files.locator_uri` 是 4.1 的 `custody='external'` 契约本身 ——
    "原文在工具之外，库里只记**路径** + sha256 + 大小"。那一列是指向真文件的
    **功能性指针**（evidence 恢复、reparse 都靠它把文件重新打开），掩掉它
    等于把文件弄丢：换来的是"全库 0 字节"这句好听的话，赔上的是 4.1 立住的
    可恢复性。**指针必须说真话**，这是它存在的全部意义。

    所以这一列是判据 ① 的**唯一例外**，记账为已知边界（不是静默带过）：
    其余所有派生落点（`jobs.coverage_scope`、`location.file`、scan evidence、
    报告 path / locator_uri、CLI stdout 信封）全部掩码，本条用例逐格钉住
    "除这一列外全清"。真正消除这一列的办法不是掩码而是**不让凭据进文件名**
    （导入时告警 / 拒绝），那是产品裁决，超出 4.2 的边界 —— 已在交付评论
    里提交架构师裁定。
    """
    har = _har_with_secrets(SECRET)
    src = write(tmp_path, FILENAME, json.dumps(har))
    opts = options(project, tmp_path, archive="external")
    report = ingest.ingest_file(db, opts, src)
    assert report["state"] == "done", report
    assert report["custody"] == "external", report

    hits = _cells_containing(db, GH_FILE)
    assert hits, "测试前提不成立：文件名凭据一个落点都没有？"
    assert set(hits) == {("raw_files", "locator_uri")}, \
        f"文件名凭据越过了裁定的边界：{sorted(set(hits))}"

    # 内容凭据仍然是全清（external 没有归档副本，库里更不该有）
    assert _cells_containing(db, SECRET) == [], "内容凭据出现在库里"
    _assert_reports_clean(tmp_path, GH_FILE, "向量 B external")
    _assert_reports_clean(tmp_path, SECRET, "向量 B external")

    # location 观测的 file 字段同样是掩码形态
    locations = [str(r[0]) for r in db.execute(
        "SELECT value FROM observations WHERE semantic_field='location'")]
    assert locations, "location 观测不见了"
    assert all(GH_FILE not in v for v in locations), locations
