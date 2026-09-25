"""CLI 契约测试（2.2）：**退出码 + JSON 信封字段**，每条命令至少一条。

为什么这批测试断言的是"信封"而不是"输出长什么样"：
`magpie` 的主要消费者是脚本与 agent。它们不能读终端，只能按 `ok` 分叉、按
`error.code` 决定下一步、按退出码决定重试与否。这三样东西一旦漂了，症状不是
"命令报错"，而是**调用方基于一个假事实继续往下走** —— 那比报错难查得多。

每条命令的用例都覆盖两件事：**成功**（`ok/data/meta` 齐、退出码 0）与
**失败**（`ok=false`、`error.code` 具体、退出码对得上）。只测成功路径的契约测试，
在命令坏掉时恰好是最沉默的那一种。

退出码表 0/1/2/3/4/5 ↔ HTTP 2xx/400/404/409/423/426 的**一一对应**由
`test_exit_code_table_is_bijective` 钉住：它是 CLI 与 2.7 之间唯一的耦合点。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from conftest import ENV                                             # noqa: E402
from magpie.cli.envelope import EXIT_TO_HTTP, EXIT_USAGE             # noqa: E402
from magpie.core import ops, store                                   # noqa: E402

# schema/ 下已应用的迁移数（`migrate.MIGRATIONS` 的长度）。
# 3.2 加了 `005_kind_vuln.sql`（登记 `vuln` kind 与 `found_on` 动词，并把
# `nuclei@1` 追加进既有 kind 的 `produced_by`）；3.3 加了
# `006_kind_endpoint.sql`（登记 `endpoint` kind 与 `served_by` 动词，并把四个
# "路径列表"来源的指纹追加进既有 kind 的 `produced_by`）；3.4 加了
# `007_kind_apischema.sql`（登记 `apischema` kind 与 `declared_in` 动词，并把四个
# "接口描述"来源的指纹追加进既有 kind 的 `produced_by`）；3.7b 加了
# `008_pipeline_definitions.sql`（管线定义 / 管线运行两张表 + `jobs.pipeline_run_id`
# + 跨项目触发器）；3.8 加了 `009_work_journal.sql`（工作层写入的撤销日志 ——
# `entities.lifecycle` 不是事实层，没有贡献可撤，撤销它需要"改之前的值"）；
# LARI-35 加了 `010_multivalue_fields.sql`（多值字段的**声明表** + 重建
# `v_field_conflicts` —— 视图是纯派生、不承载数据，所以"不许动 001"（R4）并不
# 禁止改判据；002 换 `v_integrity_violations` 是先例）；4.1 加了
# `011_flow_external.sql`（登记 `flow` kind 与 `involves` 动词，外加
# `raw_files.custody` 这一列与 `inbox_items.kind` 的第八个取值 —— 后者是重建
# 整张表，详见该文件头）；4.2 加了 `012_kind_secret.sql`（登记 `secret` kind）；
# 4.3 加了 `013_kind_fingerprint.sql`（product / service 的产出者追加
# `fingerprint@1` + `fingerprint` 观测字段的多值声明，详见该文件头）。
# 新增迁移必须同步改这一行，否则 `schema --json` 的契约数字会与真实库对不上，
# 而那条断言正是为"迁移与描述分叉"准备的。
MIGRATIONS = 13

#: 子进程的公共环境（定义在 `conftest.py`，两个测试文件共用一份）。
ENV = ENV


def cli(*args: str, env: dict | None = None) -> tuple[int, str, str]:
    """跑一次真子进程。**契约测试必须走真进程**。

    进程内 import `main()` 会跳过最容易坏的那几段：argv 解析、退出码、
    stdout 的编码。`verify_v32r5.py` 的文件头为同一件事写过同样的理由。
    """
    proc = subprocess.run([sys.executable, "-m", "magpie.cli", *args],
                          cwd=str(ROOT), capture_output=True, env=env or ENV)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def envelope(*args: str) -> tuple[int, dict]:
    code, out, err = cli(*args)
    assert out.strip(), f"stdout 为空（退出码 {code}）；stderr={err[:300]}"
    return code, json.loads(out)


def assert_ok(payload: dict, *, meta_db: Path | None = None) -> dict:
    """成功信封：三个字段一个都不能少。"""
    assert payload["ok"] is True, payload
    assert "data" in payload
    meta = payload["meta"]
    assert isinstance(meta["took_ms"], int) and meta["took_ms"] >= 0
    assert isinstance(meta["schema_version"], int)
    assert meta["db"]
    if meta_db is not None:
        assert Path(meta["db"]) == meta_db.resolve()
    return payload["data"]


def assert_err(payload: dict, code: str) -> dict:
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == code, payload
    assert payload["error"]["message"], payload
    # 错误信封**不带 meta** —— 与 /api/v1 逐字同形。多一个字段就是契约漂移。
    assert "meta" not in payload, payload
    assert "data" not in payload, payload
    return payload["error"]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _fresh_db(path: Path) -> Path:
    conn = store.connect(path)
    store.init_db(conn)
    conn.close()
    return path


# ---------------------------------------------------------------------------
# 退出码表 —— CLI 与 2.7 之间唯一的耦合点
# ---------------------------------------------------------------------------


def test_exit_code_table_is_bijective():
    """0/1/2/3/4/5 ↔ 2xx/400/404/409/423/426，**双向都是一一对应**。

    它被钉在这里的理由：这张表是 CLI 与 2.7 唯一的耦合点。CLI 侧改一档而
    API 侧没改，症状是"同一次失败在两条路上给出两个不同的判断"——
    而两条路都自洽，所以没有任何单侧的测试会发现。
    """
    assert sorted(EXIT_TO_HTTP) == [0, 1, 2, 3, 4, 5]
    assert len(set(EXIT_TO_HTTP.values())) == 6, "两个退出码映射到了同一个状态码"
    assert EXIT_TO_HTTP == {0: "2xx", 1: "400", 2: "404", 3: "409",
                            4: "423", 5: "426"}


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_schema_only(tmp_path):
    db = tmp_path / "sub" / "magpie.db"          # 目录还不存在
    code, payload = envelope("init", "--db", str(db), "--json")
    data = assert_ok(payload, meta_db=db)
    assert code == 0
    assert data["created"] is True
    assert data["schema_version"] == MIGRATIONS
    assert [m["version"] for m in data["applied"]] == list(range(1, MIGRATIONS + 1))
    assert data["projects"] == []                # **不建项目**，只建 schema


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "magpie.db"
    assert envelope("init", "--db", str(db), "--json")[0] == 0
    code, payload = envelope("init", "--db", str(db), "--json")
    data = assert_ok(payload)
    assert code == 0 and data["created"] is False and data["applied"] == []


def test_init_dry_run_creates_nothing(tmp_path):
    db = tmp_path / "sub" / "magpie.db"
    code, payload = envelope("init", "--db", str(db), "--json", "--dry-run")
    data = assert_ok(payload)
    assert code == 0 and data["dry_run"] is True
    assert [m["version"] for m in data["pending"]] == list(range(1, MIGRATIONS + 1))
    # 干跑**连文件都不建**：一个 0 字节的 magpie.db 会让下一次 init 报告
    # "库已存在"，那是真的副作用。
    assert not db.exists()
    assert not db.parent.exists()


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def test_ls_success_envelope(loaded_db):
    code, payload = envelope("ls", "--db", str(loaded_db), "--json", "--limit", "3")
    data = assert_ok(payload, meta_db=loaded_db)
    assert code == 0
    assert data["count"] == 3
    assert len(data["entities"]) == 3
    row = data["entities"][0]
    for field in ("id", "kind", "lifecycle", "entity_key", "observation_count",
                  "out_degree", "first_seen", "last_seen"):
        assert field in row, f"ls 的行少了 {field}"


def test_ls_paging_cursor_is_usable(loaded_db):
    _, first = envelope("ls", "--db", str(loaded_db), "--json", "--limit", "2")
    cursor = json.loads(json.dumps(first))["data"]["entities"][-1]["id"]
    code, payload = envelope("ls", "--db", str(loaded_db), "--json",
                             "--limit", "2", "--cursor", str(cursor))
    data = assert_ok(payload)
    assert code == 0
    assert all(row["id"] > cursor for row in data["entities"])


def test_ls_rejects_unsupported_filters_loudly(empty_db):
    """传了不支持的过滤条件要**报错**，不能静默忽略。

    静默忽略等于给 agent 一个"筛过了"的假答案 —— 而它没有。这条与 2.7 的
    `test_routes.py` 是同一个断言，两侧任何一个放松都会让它们分叉。
    """
    code, payload = envelope("ls", "--db", str(empty_db), "--json",
                             "--review-status", "open")
    error = assert_err(payload, "E_UNSUPPORTED_FILTER")
    assert code == 1
    assert error["rejected"] == ["review_status"]
    assert "kind" in error["supported"]


def test_ls_requires_project_when_ambiguous(loaded_db):
    """库里恰好一个项目 → 可以省略；0 个或多个 → **必须显式给**。"""
    code, payload = envelope("ls", "--db", str(loaded_db), "--json")
    assert_ok(payload)                       # 夹具只有一个项目

    conn = store.connect(loaded_db)
    try:
        ops.create_project(conn, "第二个", {"include": [], "exclude": []})
    finally:
        conn.close()
    code, payload = envelope("ls", "--db", str(loaded_db), "--json")
    error = assert_err(payload, "E_PROJECT_REQUIRED")
    assert code == 1                          # ↔ HTTP 400
    assert len(error["projects"]) == 2


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_by_id_and_by_key_agree(loaded_db):
    _, listing = envelope("ls", "--db", str(loaded_db), "--json", "--limit", "1")
    row = listing["data"]["entities"][0]
    code_id, by_id = envelope("show", str(row["id"]), "--db", str(loaded_db), "--json")
    code_key, by_key = envelope("show", row["entity_key"], "--db", str(loaded_db),
                                "--json")
    assert code_id == 0 and code_key == 0
    data_id = assert_ok(by_id)
    data_key = assert_ok(by_key)
    assert data_id["entity"]["id"] == data_key["entity"]["id"] == row["id"]
    for field in ("observations", "relations", "tags", "manual", "open_inbox",
                  "timeline"):
        assert field in data_id


def test_show_reports_ambiguity_instead_of_guessing(tmp_path):
    """`show 2` 有两种读法、且指向不同实体时 → 退出码 3 + candidates。

    **这是退出码 3（需人工裁决）唯一的真实触发点**，所以它值得一条用例：
    一个永远触发不了的退出码，和没有这个退出码的区别，只在于它让人以为
    处理过了。构造方式是真的用数据层写出来的两条事实，不是 mock。
    """
    db = _fresh_db(tmp_path / "ambiguous.db")
    conn = store.connect(db)
    try:
        project_id = ops.create_project(conn, "demo", {"include": [], "exclude": []})
        source_id = store.register_source(conn, name="s", authority_level="verified",
                                          tool="t")
        raw = ops.add_raw_file(conn, project_id=project_id,
                               locator_uri="file:///x", content="x\n")
        ev = ops.add_evidence(conn, project_id=project_id, raw_file_id=raw,
                              locator={"line": 1}, snippet="x")
        with store.transaction(conn):
            job = ops.start_job(conn, project_id=project_id, kind="ingest",
                                coverage="full", coverage_scope={},
                                parser_fingerprint=None)
            # key="2" 拿到实体 1；另一个 key 拿到实体 2 —— 于是字面量 "2"
            # 既是一个 id（实体 2），也是一个 key（实体 1）。
            for key in ("2", "other.example.com"):
                ops.add_observation(conn, project_id=project_id, job_id=job,
                                    evidence_id=ev, source_id=source_id,
                                    entity_key=key, kind="host",
                                    semantic_field="title", value="x")
            ops.finish_job(conn, job, state="done")
            store.recompute(conn)
    finally:
        conn.close()

    code, payload = envelope("show", "2", "--db", str(db), "--json")
    error = assert_err(payload, "E_AMBIGUOUS")
    assert code == 3                          # ↔ HTTP 409
    readings = {c["reading"]: c["entity_id"] for c in error["candidates"]}
    assert set(readings) == {"id", "key"} and len(set(readings.values())) == 2
    # 两个候选都要有展示名 —— 人在这里要判的正是"哪个是哪个"。
    assert all(c["entity_key"] for c in error["candidates"])


def test_show_not_found(loaded_db):
    code, payload = envelope("show", "没有这个.example.com", "--db", str(loaded_db),
                             "--json")
    assert_err(payload, "E_ENTITY_NOT_FOUND")
    assert code == 2                          # ↔ HTTP 404


# ---------------------------------------------------------------------------
# search —— 与 2.7 的 /search 一样响亮地 501
# ---------------------------------------------------------------------------


def test_search_is_loudly_not_implemented(empty_db):
    """**不能返回空数组。** 空数组对 agent 是一个确定的断言（"这里就是没有"），
    而事实是"这条命令还不存在"。两者混起来，agent 会基于一个假事实继续走。"""
    code, payload = envelope("search", "nginx", "--db", str(empty_db), "--json")
    error = assert_err(payload, "E_NOT_IMPLEMENTED")
    assert code == EXIT_USAGE
    assert "data" not in payload and "meta" not in payload
    assert "§8" in error["plan"]


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_reports_the_import(loaded_db):
    code, payload = envelope("diff", "--db", str(loaded_db), "--json")
    data = assert_ok(payload, meta_db=loaded_db)
    assert code == 0
    assert data["since"]["spec"] == "last" and data["since"]["job_id"] is not None
    assert data["counts"]["new"] > 0 and data["counts"]["facts"] > 0
    assert data["counts"]["entities_truncated"] == 0
    # 真实夹具的 453 条贡献精度**全是 unknown** —— 它必须出现在输出里（§8.1：
    # 不得静默把 unknown 当 exact）。
    assert data["time_accuracy"]["unknown"] == data["counts"]["facts"]
    assert any("unknown" in note for note in data["notes"])


def test_diff_shrinks_to_nothing_after_undo(loaded_db):
    """`undo` 之后 diff 必须变空。

    这不是在测 CLI —— 是在测它**读的事实**：一个作业被撤销后它的贡献不再是
    活跃贡献，那么"自它以来多出来的东西"就应当归零。2.6 的端到端流程正是
    靠这一条独立佐证"撤销是干净的"。
    """
    conn = store.connect(loaded_db)
    try:
        job_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM jobs WHERE project_id=1 ORDER BY id")]
        for job_id in job_ids:
            ops.undo(conn, job_id, actor="test")
    finally:
        conn.close()
    code, payload = envelope("diff", "--db", str(loaded_db), "--json")
    data = assert_ok(payload)
    assert code == 0
    assert data["counts"] == {"new": 0, "changed": 0, "facts": 0,
                              "entities_truncated": 0}


def test_diff_rejects_a_bad_since(loaded_db):
    code, payload = envelope("diff", "--since", "昨天", "--db", str(loaded_db),
                             "--json")
    error = assert_err(payload, "E_BAD_SINCE")
    assert code == 1
    assert error["accepted"] == ["<job_id>", "<ISO8601>", "last"]


def test_diff_unknown_job(loaded_db):
    code, payload = envelope("diff", "--since", "99999", "--db", str(loaded_db),
                             "--json")
    assert_err(payload, "E_JOB_NOT_FOUND")
    assert code == 2


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_healthy_and_reuses_core_verdicts(loaded_db):
    code, payload = envelope("doctor", "--db", str(loaded_db), "--json")
    data = assert_ok(payload, meta_db=loaded_db)
    assert code == 0
    assert data["healthy"] is True
    assert data["sqlite_integrity"] == "ok"
    assert data["violations"] == [] and data["orphan_subjects"] == []
    for field in ("purge_eligible", "foreign_key_check", "violations_by_kind",
                  "jobs", "counts"):
        assert field in data
    jobs = data["jobs"]
    assert jobs and all("scope" in job for job in jobs)
    assert jobs[0]["scope"]["interpret_with"] in ("current_scope", "historical_scope")


def test_doctor_zero_exit_when_data_is_unhealthy(loaded_db):
    """数据不健康**仍然是退出码 0**，健康与否在 `data.healthy` 里。

    理由不是"宽容"，是那两张表：`/api/v1/doctor` 在数据不健康时也返回 200，
    而退出码表要求与 HTTP 状态码一一对应。把"不健康"写成退出码 2（数据错）
    就同时破坏了两件事 —— 对应关系，以及"2 表示库读不出来"这个区分。
    脚本要的布尔值在 `data.healthy`。
    """
    conn = sqlite3.connect(str(loaded_db), isolation_level=None)
    try:
        # 这个库是**真导入**出来的，所以它自己的未决项数不是 0（3.6 起导入会开
        # 越界 / 未知字段的待办）。断言因此是**相对**的：加了一条，就多一条。
        # 写成 `== 1` 会让这条用例偷偷依赖"夹具库里一条待办都没有"这个与它
        # 要验的东西（退出码 0）毫无关系的前提。
        before = int(conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE state='open'").fetchone()[0])
        conn.execute("INSERT INTO inbox_items(project_id, kind, subject_kind,"
                     " subject_id, fingerprint, severity, state, payload_json,"
                     " created_at, last_seen_at)"
                     " VALUES (1,'manual_required','entity',1,'fp-1','high','open',"
                     "'{}','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    finally:
        conn.close()
    code, payload = envelope("doctor", "--db", str(loaded_db), "--json")
    data = assert_ok(payload)
    assert code == 0
    assert data["counts"]["open_inbox"] == before + 1
    assert data["healthy"] is True          # 未决 Inbox 不是"不健康"，它是待办


# ---------------------------------------------------------------------------
# timeline / export（4.4）
# ---------------------------------------------------------------------------


def test_timeline_success_and_carries_8_1_semantics(loaded_db):
    """`magpie timeline`：信封三件套齐、退出码 0、事件带 §8.1 时间四元组。"""
    code, payload = envelope("timeline", "--db", str(loaded_db), "--json")
    data = assert_ok(payload)
    assert code == 0
    for key in ("events", "lower_bounds", "counts", "semantics"):
        assert key in data
    assert "§8.1" in data["semantics"], "timeline 没带 §8.1 语义（时间语义没沿用）"
    for event in data["events"] + data["lower_bounds"]:
        for col in ("observed_at", "observed_at_accuracy",
                    "observed_at_source", "observed_at_precision"):
            assert col in event, f"事件缺 §8.1 字段 {col}"
    assert data["counts"]["ranked"] == len(data["events"])
    assert data["counts"]["lower_bounds"] == len(data["lower_bounds"])


def test_export_writes_self_contained_package_and_leaves_db_untouched(
        loaded_db, tmp_path):
    """`magpie export`：写出自洽包、退出码 0、**库一个字节都不动**（只读连接）。"""
    from magpie.cli.tests.test_readonly import _fingerprint
    before = _fingerprint(loaded_db)
    out = tmp_path / "pkg"
    code, payload = envelope("export", "--db", str(loaded_db), "--json",
                             "-o", str(out))
    data = assert_ok(payload)
    assert code == 0
    assert data["redaction"] == "default" and data["full"] is False
    assert data["warning"] is None          # 默认不告警
    # 包是自洽的：manifest + README + 实体/时间线/原文索引都在
    for name in ("manifest.json", "README.md", "entities.json",
                 "timeline.json", "raw_files.json", "secrets.json"):
        assert (out / name).is_file(), f"包里缺 {name}"
    # 只读：导出只读库、只写包目录，库逐表指纹不变
    assert _fingerprint(loaded_db) == before, "export 动了库 —— 它该是只读的"


def test_export_full_sets_warning_field_and_includes_raw(loaded_db, tmp_path):
    """`--full`：`data.warning` 非空、包里多 `raw/` 正文（告警本身见 4.4 核心用例）。"""
    out = tmp_path / "fullpkg"
    code, payload = envelope("export", "--db", str(loaded_db), "--json",
                             "--full", "-o", str(out))
    data = assert_ok(payload)
    assert code == 0
    assert data["full"] is True and data["redaction"] == "full"
    assert data["warning"], "--full 没有把 warning 放进信封"
    assert any(name.startswith("raw/") for name in data["files"]), \
        "--full 的文件清单里没有 raw/ 正文"


def test_doctor_flags_a_real_integrity_violation(loaded_db):
    """反例：制造一条真的违例，`healthy` 必须变红。

    一条永远绿的体检比没有体检更糟 —— 它让人更安心。所以这里造一个
    `v_integrity_violations` 真的看得见的状态（幽灵缓存行：一条没有任何
    贡献解释的 observation），并断言报告变红。
    """
    conn = sqlite3.connect(str(loaded_db), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO observations(project_id, entity_id, semantic_field,"
                     " value, is_current, created_at)"
                     " VALUES (1, 1, 'ghost', 'x', 0, '2026-01-01T00:00:00Z')")
    finally:
        conn.close()
    code, payload = envelope("doctor", "--db", str(loaded_db), "--json")
    data = assert_ok(payload)
    assert code == 0
    assert data["healthy"] is False
    assert data["violations_by_kind"].get("phantom_cache_row", 0) >= 1


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_schema_describes_types_verbs_fields_and_syntax(loaded_db):
    code, payload = envelope("schema", "--db", str(loaded_db), "--json")
    data = assert_ok(payload)
    assert code == 0
    assert data["schema_version"] == MIGRATIONS
    assert {"domain", "ip", "host", "service", "product"} <= {
        k["kind"] for k in data["kinds"]}
    for kind in data["kinds"]:
        # 三件套缺一不可（003_kind_registry.sql 的登记条件）
        assert kind["identity_rule"] and kind["lens"] and kind["fixture_path"]
    assert {"member_of", "resolves_to", "runs"} <= {
        v["verb"] for v in data["relation_verbs"]}
    assert {f["name"] for f in data["fields"]["entity"]} >= {
        "id", "entity_key", "kind", "lifecycle", "tag"}
    assert data["fields"]["observation"], "真库上应当列出实际出现过的观测字段"
    syntax = data["query_syntax"]
    assert set(syntax["commands"]) >= set(RUNNER_COMMANDS)
    assert syntax["exit_codes"]["3"]["http"] == "409"
    assert "hint" in syntax["envelope"]["failure"]["error"]


def test_schema_lists_every_extra_with_install_command_and_purpose(empty_db):
    """4.5 收口：有哪些 extras、装没装、跑哪条命令装、各自干嘛 —— `schema` 是
    查得到的，不用再读源码（"agent 无需读源码"本来就是这条命令立着的理由）。
    """
    _, payload = envelope("schema", "--db", str(empty_db), "--json")
    extras = assert_ok(payload)["extras"]
    assert set(extras) == {"api", "api-yaml", "traffic", "dev"}, sorted(extras)
    for name, info in extras.items():
        assert info["install"] == f'pip install -e ".[{name}]"', (name, info)
        assert isinstance(info["installed"], bool), name
        assert info["purpose"], f"{name} 的用途说明是空的"
        assert isinstance(info["packages"], list), name
    # 4.1 的裁定：traffic 刻意为空（零第三方走标准库 json；tshark 是外部可执行，
    # pip 装不出它）—— 空 extras 的 installed 必须是 True（就绪，不是"缺"）。
    assert extras["traffic"]["packages"] == []
    assert extras["traffic"]["installed"] is True
    # 探针按**模块**名问（pyyaml 提供的模块叫 yaml），包清单仍按 pyproject 的写法。
    assert extras["api-yaml"]["packages"] == ["pyyaml"]


def test_extras_registry_matches_pyproject_exactly():
    """`pyproject.toml` 的 `[project.optional-dependencies]` 是唯一事实来源 ——
    这里把它钉成断言：改了 pyproject 不同步 `EXTRAS_REGISTRY`，本条当场红。

    顺带把 4.5 点名的两条边界也钉在这里（都是解析出**值**，不是搜字符串 ——
    traffic 的注释里正大光明地写着 "tshark 不进这里"，字符串判据会把它误伤）：
    `traffic == []` 与 `api-yaml` 的包名。
    """
    import tomllib

    from magpie.cli.read import EXTRAS_REGISTRY

    table = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = table["project"]["optional-dependencies"]
    assert set(EXTRAS_REGISTRY) == set(extras), (
        f"pyproject: {sorted(extras)} ≠ registry: {sorted(EXTRAS_REGISTRY)}")
    for name, packages in extras.items():
        assert EXTRAS_REGISTRY[name]["packages"] == list(packages), name
    assert extras["traffic"] == [], "4.5 裁定：traffic 保持为空（零第三方）"
    assert extras["api-yaml"] == ["pyyaml"]


def test_schema_query_syntax_matches_the_real_parser(empty_db):
    """语法是从**真的那个 parser** 生成的 —— 手写的文档会过期。

    断言的是"没有第二份语法"：把 `schema` 描述出来的 flag 集合、位置参数顺序、
    以及默认值，与真解析器逐项对齐。写死一份 JSON 文档的实现在第一次改命令时红，
    而它红的方式恰好是**最不容易被发现的那种**：文档看起来仍然合理。
    """
    from magpie.cli import build_parser

    parser = build_parser()
    _, payload = envelope("schema", "--db", str(empty_db), "--json")
    described = assert_ok(payload)["query_syntax"]["commands"]
    real_commands = _subparsers(parser)
    assert set(described) == set(real_commands), "命令集合分叉"

    import argparse as _argparse

    def is_subparsers(action) -> bool:
        # 子命令表在 argparse 里也是一个"位置参数"（dest=`project_command` 之类），
        # 但它描述的是**命令名本身**，不是参数 —— `_query_syntax` 两边都跳过它，
        # 这里不跳的话，`project` 这一条会凭空多出一个名叫 project_command 的位置
        # 参数，而那是一条错误的用法说明。
        return isinstance(action, _argparse._SubParsersAction)

    for name, sub in real_commands.items():
        real_flags = {f for a in sub._actions if a.option_strings
                      for f in a.option_strings}
        got_flags = {f for o in described[name]["options"]
                     for f in o["flag"].split("/")}
        assert real_flags == got_flags, (name, real_flags ^ got_flags)

        real_pos = [a.dest for a in sub._actions
                    if not a.option_strings and not is_subparsers(a)]
        assert real_pos == [o["name"] for o in described[name]["positionals"]], name

    # 默认值也要跟着来 —— `--since last` 与 `--limit 50` 是 agent 拼参数时
    # 唯一能据以省略某个 flag 的依据。
    for option in described["diff"]["options"]:
        if option["flag"] == "--since":
            assert option["default"] == "last"
    for option in described["ls"]["options"]:
        if option["flag"] == "--limit":
            assert option["default"] == 50


def test_schema_command_access_is_tied_to_dispatch(empty_db):
    """读 / 写分类与**分发层的事实**逐项一致（5.2）。

    `magpie.cli.WRITABLE` 就是 `main` 决定开只读连接还是可写连接用的那一份 ——
    本条把 `commands` 输出钉在它身上：两边任何一侧改了而另一侧没跟，先红的
    是这里，而不是"某天 agent 按 read 调了一条会改库的命令"。
    """
    from magpie.cli import WRITABLE
    from magpie.cli.read import RUNNERS

    _, payload = envelope("schema", "--db", str(empty_db), "--json")
    data = assert_ok(payload)
    commands = data["commands"]

    # 完整性：真 parser 的每个键都要有分类（少一个就是"没分类的命令"）
    assert set(commands) == set(data["query_syntax"]["commands"])
    assert set(RUNNERS) <= set(commands)
    for key, entry in commands.items():
        assert entry["access"] in ("read", "write", "service", "group"), key
        assert isinstance(entry["mutates_db"], bool), key

    # 会改库的那些：顶层命令名与 WRITABLE **集合相等**（双向，不只是子集）
    mutates = {key for key, entry in commands.items() if entry["mutates_db"]}
    assert {key.split()[0] for key in mutates} == set(WRITABLE)

    # 各就各位的四个关键键 —— 每一个都曾是"按文件位置手抄会抄错"的陷阱：
    # jobs 住在 write.py 却是读；init 住在 read.py 却是写；
    # search 是读（未实现但不改库）；app / project 不是普通可执行命令。
    assert commands["jobs"]["access"] == "read"
    assert commands["init"]["access"] == "write"
    assert commands["init"]["mutates_db"] is True
    assert commands["search"]["access"] == "read"
    assert commands["app"]["access"] == "service"
    assert commands["project"]["access"] == "group"
    # 反例自检：service / group 的例外**恰好**只有这两个 —— 哪天 parser 多出
    # 一个不在 RUNNERS 里的键，先红的是这条（而不是它被静默编成 service）。
    assert {key for key, entry in commands.items()
            if entry["access"] in ("service", "group")} == {"app", "project"}
    # RUNNERS 里没有第三态：每条可分发命令不是读就是写
    for key in RUNNERS:
        assert commands[key]["access"] in ("read", "write"), key


def test_schema_enums_come_from_the_live_db_checks(empty_db):
    """枚举每一条都对得上**真库的 CHECK**（5.2）—— 003 冻结的六个镜头、
    011 重建表后的第八个 inbox kind，一个不多一个不少。

    手抄一张静态表在下一次迁移那天不会红 —— 这条与 `cmd_schema` 的
    `check_enum` 是一对：那边负责"从真库算"，这边负责"算错了要红"。
    """
    from magpie.cli import build_parser
    from magpie.core import ops, query as core_query, scope as scope_module

    _, payload = envelope("schema", "--db", str(empty_db), "--json")
    data = assert_ok(payload)
    enums = data["enums"]

    # 镜头：003 的 CHECK 冻结**只有六个**，且与代码常量一致
    assert enums["lens"] == ["graph", "assets", "services", "api", "traffic", "vuln"]
    assert enums["lens"] == list(core_query.LENSES)
    # kind → 镜头的归属由 kinds[].lens 给出，必须落在镜头全集里
    assert {k["lens"] for k in data["kinds"]} <= set(enums["lens"])

    # 生命周期：真库 CHECK == ops.LIFECYCLES == argparse 的 choices（三方一致）
    assert enums["lifecycle"] == list(ops.LIFECYCLES) == ["active", "retired"]
    ls_parser = _subparsers(build_parser())["ls"]
    lifecycle_action = next(a for a in ls_parser._actions
                            if "--lifecycle" in a.option_strings)
    assert list(lifecycle_action.choices) == enums["lifecycle"]

    # Inbox：001 的七个 kind + 011 重建表加的第八个；severity / state 一并钉住
    assert enums["inbox_kind"] == [
        "value_conflict", "orphan_subject", "identity_ambiguous",
        "merge_conflict", "evidence_missing", "scope_drift",
        "manual_required", "unregistered_asset"]
    assert enums["inbox_severity"] == ["low", "medium", "high"]
    assert enums["inbox_state"] == ["open", "resolved", "dismissed", "closed"]

    # 原文持有（011 的 custody + 001 的 storage_state）与来源可信度
    assert enums["storage_state"] == ["present", "archived", "purged", "missing"]
    assert enums["custody"] == ["stored", "external"]
    assert enums["authority_level"] == ["verified", "semiautomatic",
                                        "manual", "unverified"]

    # scope 三态：描述钉一遍，**真求值行为**再钉一遍（描述与实现分叉必须红）
    scope = enums["scope"]
    assert scope["evaluated_kinds"] == list(scope_module.SCOPE_KINDS) \
        == ["domain", "ip", "cidr"]
    assert scope["verdicts"] == {"in_scope": True, "out_of_scope": False,
                                 "not_applicable": None}
    cfg = {"include": ["*.example.com"], "exclude": []}
    assert scope_module.in_scope(cfg, "domain", "api.example.com") is True
    assert scope_module.in_scope(cfg, "domain", "evil.test") is False
    assert scope_module.in_scope(cfg, "product", "nginx@1.24.0") is None


def test_check_enum_reads_the_live_schema_and_fails_loudly(empty_db):
    """`query.check_enum` 的正反两面：真表读得出；读不懂**响亮失败**，不回空表。

    "解析失败 → 空列表"与"这个词表真的是空的"在输出里长得一模一样，而前者
    会让 agent 按"没有任何合法取值"行动 —— 所以三种读不懂的形态都必须抛
    `E_ENUM_UNREADABLE`，这一条就是那三个反例。
    """
    from magpie.core import query as core_query

    conn = store.connect(empty_db)
    try:
        # 正面：从**这个库真的建表语句**里读（不是读任何一张 Python 常量表）
        assert core_query.check_enum(conn, "kind_registry", "lens") == [
            "graph", "assets", "services", "api", "traffic", "vuln"]
        assert core_query.check_enum(conn, "inbox_items", "kind")[-1] \
            == "unregistered_asset"          # 011 重建表加的第八个真的在

        # 反例①：表不在本库里
        with pytest.raises(core_query.QueryError) as exc:
            core_query.check_enum(conn, "no_such_table", "x")
        assert exc.value.code == "E_ENUM_UNREADABLE"
        assert exc.value.details["table"] == "no_such_table"

        # 反例②：列没有 CHECK 词表 —— 不许回空列表冒充"词表是空的"
        conn.execute("CREATE TABLE plain (z TEXT NOT NULL)")
        with pytest.raises(core_query.QueryError) as exc:
            core_query.check_enum(conn, "plain", "z")
        assert exc.value.code == "E_ENUM_UNREADABLE"
        assert exc.value.details["column"] == "z"

        # 反例③：有 CHECK 但不是 IN 清单（表达式形态，本步的已知边界）
        conn.execute("CREATE TABLE guarded (w TEXT CHECK (length(w) > 0))")
        with pytest.raises(core_query.QueryError):
            core_query.check_enum(conn, "guarded", "w")

        # 对照：同一张新表上的 IN 清单照常读得出（反例②不是"新表都失败"）
        conn.execute("CREATE TABLE synthetic (x TEXT CHECK (x IN ('a', 'b')))")
        assert core_query.check_enum(conn, "synthetic", "x") == ["a", "b"]
    finally:
        conn.close()


def test_schema_tool_version_and_parser_catalog_are_live(empty_db):
    """`tool_version` / `parsers`（5.2）：版本对得上包，清单对得上**真的注册表**。

    `import --parser` 的帮助文本写着"取值见 `magpie schema`"—— 这条断言里
    对 help 的那两行就是在钉这个承诺：指引指向的地方必须真的有货。
    """
    from magpie import __version__
    from magpie.cli import build_parser
    from magpie.core import ingest

    _, payload = envelope("schema", "--db", str(empty_db), "--json")
    data = assert_ok(payload)

    assert data["tool_version"] == __version__

    tools, source = ingest.parser_catalog()
    assert data["parsers"]["tools"] == tools
    assert data["parsers"]["source"] == source
    names = [t["tool"] for t in tools]
    assert len(names) == len(set(names)), "解析器 tool 名重复"
    # 空清单是另一种故障：它与"真的没有解析器"在输出里一模一样地绿
    assert {"domainlist", "httpx", "nuclei", "har"} <= set(names)
    # kinds 声称的产出者必须真的是注册表里的解析器（tool@version 的前半段）
    assert {p.split("@")[0] for k in data["kinds"]
            for p in k["produced_by"]} <= set(names)

    # --parser 的帮助承诺"取值见 magpie schema"—— 承诺本身也钉在这里
    import_parser = _subparsers(build_parser())["import"]
    parser_action = next(a for a in import_parser._actions
                         if "--parser" in a.option_strings)
    assert "magpie schema" in (parser_action.help or "")


def _subparsers(parser) -> dict:
    """parser → {命令名: 子 parser}，**嵌套的展平成带空格的键**。

    与 `read._query_syntax` 的 `walk` 同一口径：`project create` 是两层 argparse
    一层键。两边各展平一次而不是共用，是因为这里要的是**真 parser 对象**（拿去
    比对 `_actions`），而那边要的是序列化后的字典。展平规则本身只有一条，
    它由 `set(described) == set(real_commands)` 这条断言钉住 —— 两边一旦
    展平得不一样，先红的就是它。
    """
    import argparse

    out: dict = {}

    def walk(node, prefix: str) -> None:
        for action in node._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in action.choices.items():
                key = f"{prefix} {name}".strip()
                out[key] = sub
                walk(sub, key)

    walk(parser, "")
    return out


#: 2.2 交付的只读命令。`schema` 的描述里这几条必须在（它们是最老的一批消费者
#: 依赖的东西）。刻意不是"全集相等"：那条断言在 `test_schema_..._real_parser`
#: 里已经逐项做过了，这里再钉一次是为了让"加命令时顺手删掉一条老的"也能红。
RUNNER_COMMANDS = {"init", "ls", "lens", "show", "search", "diff", "doctor", "schema",
                   "project create", "import", "undo", "redo", "jobs"}


# ---------------------------------------------------------------------------
# 用法错与未实现命令
# ---------------------------------------------------------------------------


def test_unknown_command_is_a_usage_error_in_the_envelope(empty_db):
    """argparse 的用法错**也要走信封**，而且退出码是 1 不是 2。

    argparse 默认 `sys.exit(2)`，而 2 在这张冻结的表里是**数据错**：同一个
    数字在两个地方指两件事，是 agent 最难查的一类歧义。
    """
    code, payload = envelope("没有这个命令", "--db", str(empty_db), "--json")
    assert_err(payload, "E_USAGE")
    assert code == 1


def test_every_registered_command_is_dispatchable(empty_db):
    """**登记在 `RUNNERS` 里的命令必须真的能被分发**（2.6 起写命令已落地）。

    2.2 时这条写的是"`magpie undo 1` 应当说'属于 2.6'，而不是 argparse 的
    'unrecognized arguments: 1'"。写路径落地之后，那句"尚未实现"就不再成立，
    而**测试没有跟着改**的话，它就会变成一条要求命令保持未实现的断言 ——
    一个把缺陷固化成规格的经典形态。所以这里换成正向的：每条命令都得跑起来。

    判据刻意不看退出码：`import 1` 在空库上会因为"没有项目"失败，那是**业务**
    失败，与"这条命令没被分发"是两回事。真正要排除的是 `E_NOT_IMPLEMENTED`
    与 argparse 的用法错 —— 它们才是"这条路根本没通"的症状。
    """
    from magpie.cli.read import RUNNERS

    #: 每条命令一个最小可运行调用（位置参数给对，`--db` 指到空库）。
    invocations = {
        "init": ("init",),
        "ls": ("ls",),
        "lens": ("lens",),
        "show": ("show", "1"),
        "search": ("search", "q"),
        "diff": ("diff", "--since", "last"),
        "doctor": ("doctor",),
        "schema": ("schema",),
        # 4.4：两条都在 resolve_project 处先要项目，空库上以 E_PROJECT_REQUIRED
        # 收场（不是 E_NOT_IMPLEMENTED / 用法错）—— "被分发了"这件事已成立。
        "timeline": ("timeline",),
        "export": ("export", "-o", "unused-pkg"),
        "project create": ("project", "create", "p"),
        "import": ("import", "x.txt"),
        "undo": ("undo", "1"),
        "redo": ("redo", "1"),
        "jobs": ("jobs",),
    }
    assert set(invocations) == set(RUNNERS), "有命令没被这条测试覆盖到"

    #: 登记了但**故意**还说自己没做的：搜索 DSL 属于计划书 §8。它由下一条
    #: 用例单独钉住 —— 混在这一条里会让"哪几条该通"变成一句含糊的话。
    not_yet = {"search"}

    for key, args in invocations.items():
        _, payload = envelope(*args, "--db", str(empty_db), "--json")
        code = payload.get("error", {}).get("code")
        if key in not_yet:
            assert code == "E_NOT_IMPLEMENTED", f"{key} 应当响亮地未实现：{payload}"
            continue
        assert payload["ok"] is True or code not in ("E_NOT_IMPLEMENTED", "E_USAGE"), \
            f"{key} 没有被分发：{payload}"


def test_search_is_still_loudly_not_implemented(empty_db):
    """`search` **仍然**是响亮的 501（§8 没落地）—— 它不该被写路径顺手带上。

    这条与上一条是一对：上一条证明"该通的通了"，这一条证明"没做的那条仍然
    说自己没做"。少了它，`search` 返回一个空列表也会全绿。
    """
    code, payload = envelope("search", "x", "--db", str(empty_db), "--json")
    error = assert_err(payload, "E_NOT_IMPLEMENTED")
    assert code == EXIT_USAGE
    assert "§8" in error["plan"]


def test_bad_limit_says_what_is_wrong(empty_db):
    code, payload = envelope("ls", "--db", str(empty_db), "--json", "--limit", "0")
    assert_err(payload, "E_BAD_LIMIT")
    assert code == 1
