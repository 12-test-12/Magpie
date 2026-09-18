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

MIGRATIONS = 3          # schema/ 下已应用的迁移数（`migrate.MIGRATIONS` 的长度）

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
    assert data["counts"]["open_inbox"] == 1
    assert data["healthy"] is True          # 未决 Inbox 不是"不健康"，它是待办


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

    for name, sub in real_commands.items():
        real_flags = {f for a in sub._actions if a.option_strings
                      for f in a.option_strings}
        got_flags = {f for o in described[name]["options"]
                     for f in o["flag"].split("/")}
        assert real_flags == got_flags, (name, real_flags ^ got_flags)

        real_pos = [a.dest for a in sub._actions if not a.option_strings]
        assert real_pos == [o["name"] for o in described[name]["positionals"]], name

    # 默认值也要跟着来 —— `--since last` 与 `--limit 50` 是 agent 拼参数时
    # 唯一能据以省略某个 flag 的依据。
    for option in described["diff"]["options"]:
        if option["flag"] == "--since":
            assert option["default"] == "last"
    for option in described["ls"]["options"]:
        if option["flag"] == "--limit":
            assert option["default"] == 50


def _subparsers(parser):
    import argparse
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


RUNNER_COMMANDS = {"init", "ls", "show", "search", "diff", "doctor", "schema"}


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


def test_pending_write_commands_say_why(empty_db):
    """`magpie undo 1` 应当说"属于 2.6"，而不是 argparse 的
    "unrecognized arguments: 1" —— 后者会让人以为是自己参数写错了。"""
    for name in ("import", "undo", "redo", "jobs"):
        code, payload = envelope(name, "1", "--db", str(empty_db), "--json")
        error = assert_err(payload, "E_NOT_IMPLEMENTED")
        assert code == EXIT_USAGE, name
        assert "2.6" in error["plan"], name


def test_bad_limit_says_what_is_wrong(empty_db):
    code, payload = envelope("ls", "--db", str(empty_db), "--json", "--limit", "0")
    assert_err(payload, "E_BAD_LIMIT")
    assert code == 1
