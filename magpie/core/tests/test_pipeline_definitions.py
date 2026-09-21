"""管线定义与批量跑（3.7b · LARI-30）。

3.7a 的冻结文件（`Magpie-编排模式设计冻结.md`）把这一节的判据逐条写死了，每个
用例的 docstring 指出它对应哪一条。**冻结项不在这里解释，只在这里被执行** ——
需要改的时候，理由要回 LARI-17 说，不是改一个断言。

    A6  同一份定义从 YAML 与从库载入 → normalize 输出逐字节相等
    A7  双向：保存定义不建 job；跑一次不改定义
    A4  批量跑的 job 全部是 kind='ingest'，且集合等于报告里的 job_ids
    A5  干跑 == 真跑（totals 逐字段相等），且干跑前后行数一行不变
    A8  连续两次干跑 undo_run 报告逐字段相同，且 applied=0 / retract 行数不变
    A9  真跑 → 快照 → undo_run → redo_run → 逐行指纹相等（复用 INV-13(a)）

另外三组不来自冻结编号、但同样是"会变红"的机制：

    * `source.kind` 三种取值在三处（匹配 / 找不到 / 递归）行为各不相同 ——
      `file` 找不到必须是**硬错误**，不是"一次空导入"；
    * 旧形态（`source_glob` + 字符串 `source`）读得懂，只有 `source: "名字"`
      这一种**必须报错**（它有两种读法，而"猜"在这里没有依据）；
    * `undo_run` 的部分失败必须**停下并说清楚**，不假装原子。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from magpie.core import ops, pipeline, store
from magpie.core.store import InvariantError, connect, init_db

SCOPE = {"include": ["*.example.com"], "exclude": []}


# ---------------------------------------------------------------------------
# 脚手架：一个临时库 + 两个真实夹具文件
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    """临时库 + 两个可导入的输入文件（域名列表与 httpx 指纹）。"""
    (tmp_path / "recon").mkdir()
    (tmp_path / "recon" / "subfinder.txt").write_text(
        "".join(f"h{i}.example.com\n" for i in range(12)), encoding="utf-8")
    (tmp_path / "recon" / "httpx.jsonl").write_text(
        "".join(json.dumps({"url": f"http://h{i}.example.com/", "status_code": 200,
                            "tech": ["nginx"]}) + "\n" for i in range(6)),
        encoding="utf-8")
    conn = connect(tmp_path / "magpie.db")
    init_db(conn)
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def _yaml(tmp_path: Path, text: str, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _definition(tmp_path: Path, steps, **extra) -> dict:
    """一份**已经归一化**的定义（库入口那一侧的形状）。"""
    raw = {"format_version": 1, "name": "常规一轮", "project": "演示项目",
           "base_dir": str(tmp_path), "scope": SCOPE, "steps": steps}
    raw.update(extra)
    return pipeline.normalize_definition(raw)


def _run(conn, definition, **kwargs):
    return pipeline.run_pipeline(conn, definition, **kwargs)


def _project_id(conn) -> int:
    return int(conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"])


def _batch_run(conn, steps, **kwargs) -> dict:
    """建项目 → 存定义 → 跑一次（真跑），返回报告。"""
    definition = _definition(conn_anchor(conn), steps, **kwargs.pop("definition_extra", {}))
    project = pipeline.resolve_project(conn, definition)
    pipeline_id = int(ops.save_pipeline(conn, project_id=int(project["id"]),
                                        definition=definition, actor="test")["id"])
    run_id = ops.start_pipeline_run(
        conn, project_id=int(project["id"]), pipeline_id=pipeline_id,
        definition_hash=pipeline.definition_hash(definition),
        format_version=1, step_count=len(definition["steps"]), actor="test")
    report = _run(conn, dict(definition, _project_id=int(project["id"]),
                             _pipeline_id=pipeline_id), **kwargs)
    ops.finish_pipeline_run(conn, run_id, state="done", report=report,
                            job_ids=report["job_ids"], actor="test")
    return {"run_id": run_id, "pipeline_id": pipeline_id,
            "project_id": int(project["id"]), "report": report}


def conn_anchor(conn) -> Path:
    """定义里 base_dir 的基准 —— 用库文件所在目录（测试里就是 tmp_path）。"""
    return store.db_file(conn).parent


# ---------------------------------------------------------------------------
# A6 —— 两个载入器逐字节相等
# ---------------------------------------------------------------------------


def test_a6_yaml_and_library_loaders_produce_byte_identical_definitions(env):
    """**A6**：同一份定义分别从 YAML 与从库载入，`normalize_definition` 的输出
    `json.dumps(..., sort_keys=True, ensure_ascii=False)` **逐字节相等**。

    这条防的是"同一个定义、两种读法"：两边都跑得通，只是答得不一样，而那没有
    任何症状。3.5 为同一类缺陷立了 `SQL-FREE` 红线，这里配这条测试。
    """
    conn, tmp_path = env
    yaml_path = _yaml(tmp_path, """
name: 常规一轮
project: 演示项目
scope: {include: ["*.example.com"], exclude: []}
steps:
  - id: dns
    parser: domainlist
    source_glob: "recon/subfinder.txt"
    source: subfinder
  - id: probe
    parser: httpx
    source_glob: "recon/*httpx*.jsonl"
    after: [dns]
""")
    from_file = pipeline.load_definition(yaml_path)

    project_id = ops.create_project(conn, "演示项目", SCOPE)
    pipeline_id = int(ops.save_pipeline(conn, project_id=project_id,
                                        definition=from_file, actor="test")["id"])
    stored = _row_definition(conn, pipeline_id)
    from_library = pipeline.normalize_definition(stored, project="演示项目")

    assert _dump(from_library) == _dump(from_file)


def _row_definition(conn, pipeline_id: int) -> dict:
    raw = conn.execute("SELECT definition FROM pipeline_definitions WHERE id=?",
                       (int(pipeline_id),)).fetchone()["definition"]
    return json.loads(raw)


def _dump(definition: dict) -> str:
    """A6 的判据**逐字**来自冻结文件里的那行 json.dumps。

    比较的是 `canonical_definition`（去掉运行时键）而不是整个归一化产物：两个
    入口的 `_path` 本来就不同，而那正是"它不从定义的内容里来"的证明 —— 拿它去
    比会把一条**设计如此**的差异读成不等。
    """
    return json.dumps(pipeline.canonical_definition(definition),
                      sort_keys=True, ensure_ascii=False)


def test_a6_normalization_drops_runtime_keys_from_the_stored_document(env):
    """`_path` 是入口的信息，不是定义的内容 —— 它不进库，也不进 hash。

    不这么做的话，同一个定义从 YAML 与从库载入会得到**两个 `definition_hash`**，
    而"这次跑的是哪一版"就再也答不上来。
    """
    conn, tmp_path = env
    yaml_path = _yaml(tmp_path, """
name: x
project: 演示项目
scope: {include: ["a.example.com"], exclude: []}
steps:
  - id: a
    source_glob: "recon/subfinder.txt"
""")
    loaded = pipeline.load_definition(yaml_path)
    assert loaded["_path"] == str(yaml_path)                 # 运行期看得到
    assert "_path" not in pipeline.canonical_definition(loaded)   # 存库看不到


# ---------------------------------------------------------------------------
# `source` 的形态：三种 kind + 旧写法
# ---------------------------------------------------------------------------


def test_legacy_source_glob_plus_source_normalizes_to_a_locator(env):
    """旧形态：`source_glob` + 字符串 `source`。

    `source` 从 3.7b 起只表示「导入什么」，来源名是 `source_name`。旧写法读得懂
    —— 但读出来的是**新形态**，因为"一个只写了 `source: subfinder` 的 step
    会被当成 `glob("subfinder")` 去扫盘"正是这一步要修掉的歧义。
    """
    _, tmp_path = env
    definition = pipeline.normalize_definition({
        "name": "x", "project": "p", "base_dir": str(tmp_path),
        "steps": [{"id": "dns", "source_glob": "recon/*.txt", "source": "subfinder"}],
    })
    step = definition["steps"][0]
    assert step["source"] == {"kind": "glob", "pattern": "recon/*.txt"}
    assert step["source_name"] == "subfinder"


def test_source_without_glob_is_an_error_and_shows_both_ways_out(env):
    """只有 `source: "<字符串>"` → **报错**，且报错信息给出两条出路。

    它可能是来源名、也可能是一个路径，两种读法给出两个不同的结果，而"猜"在这
    里没有依据。一条会告诉你怎么写的错误，比一条猜一半的宽容有用得多。
    """
    _, tmp_path = env
    with pytest.raises(pipeline.DefinitionError) as exc:
        pipeline.normalize_definition({
            "name": "x", "project": "p", "base_dir": str(tmp_path),
            "steps": [{"id": "dns", "source": "subfinder"}],
        })
    message = str(exc.value)
    assert "source_name" in message and "pattern" in message


def test_source_kind_file_missing_is_a_hard_error_before_anything_runs(env):
    """`kind: "file"` 找不到 → 硬错误 `E_SOURCE_NOT_FOUND`，而且**在跑之前**。

    与 `POST /imports` 对不存在的路径在提交之前就报 `E_PATH_NOT_FOUND` 同一条
    纪律："路径写错了"不该变成一个通过了校验、却在报告里写"没有匹配到任何文件"
    的 job。这里连项目都不该被建出来。
    """
    conn, tmp_path = env
    definition = _definition(tmp_path, [
        {"id": "a", "source": {"kind": "file", "pattern": "recon/nope.txt"}},
    ])
    with pytest.raises(pipeline.DefinitionError) as exc:
        _run(conn, definition)
    assert exc.value.code == "E_SOURCE_NOT_FOUND"
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0, \
        "硬错误发生在建项目之前 —— 一个写错的路径不该留下半条管线"


def test_source_kind_file_rejects_globs(env):
    """`file` 的语义是"我指名道姓要这个文件"，带通配符就是写错了。"""
    _, tmp_path = env
    with pytest.raises(pipeline.DefinitionError) as exc:
        pipeline.normalize_definition({
            "name": "x", "project": "p", "base_dir": str(tmp_path),
            "steps": [{"id": "a", "source": {"kind": "file", "pattern": "recon/*.txt"}}],
        })
    assert "通配符" in str(exc.value)


def test_source_kind_glob_with_zero_matches_is_a_problem_not_an_error(env):
    """`glob` 匹配 0 个 → 报告里的 `problems` + `dependency_empty`（保持今天的行为）。

    一条"今天的上游还没产出"的管线不该跑不起来 —— 那是频率问题，不是配置错误。
    """
    conn, tmp_path = env
    result = _batch_run(conn, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/nothere-*.txt"}},
    ])
    step = result["report"]["steps"][0]
    assert step["matched"] == 0
    assert any("没有匹配到任何文件" in p for p in step["problems"])
    assert step["job_ids"] == []


def test_source_kind_dir_discovers_recursively(env):
    """`dir` → 交给 `ingest.discover` 递归发现（它按扩展名过滤）。"""
    conn, tmp_path = env
    (tmp_path / "recon" / "deep").mkdir()
    (tmp_path / "recon" / "deep" / "more.txt").write_text(
        "deep.example.com\n", encoding="utf-8")
    result = _batch_run(conn, [
        {"id": "a", "source": {"kind": "dir", "pattern": "recon"}},
    ])
    step = result["report"]["steps"][0]
    names = sorted(Path(f["path"]).name for f in step["files"])
    assert names == ["httpx.jsonl", "more.txt", "subfinder.txt"], names


# ---------------------------------------------------------------------------
# 格式版本
# ---------------------------------------------------------------------------


def test_library_definition_without_format_version_is_rejected(env):
    """冻结 F3：库里的定义**必须有** `format_version`，读不到就是错误。

    文件入口那一边的职责恰恰相反 —— 它给旧 YAML **盖上 1**，于是"没有版本字段
    的那一天"永远不会到来。
    """
    _, tmp_path = env
    with pytest.raises(pipeline.DefinitionError) as exc:
        pipeline.normalize_definition(
            {"name": "x", "project": "p", "steps": [
                {"id": "a", "source": {"kind": "glob", "pattern": "x"}}]},
            require_version=True)
    assert exc.value.code == "E_PIPELINE_BAD_FORMAT"


def test_a_newer_format_version_is_refused_loudly(env):
    """读到不认识的版本必须**响亮地拒绝**，而不是"尽力而为地读一半"。

    读了一半的定义跑出来的是一份看着成功、实际漏了一段的东西 —— 那比失败坏得多
    （照抄 `migrate.MigrationError` 那条：数据库里有未知迁移 → 拒绝继续）。
    """
    _, tmp_path = env
    raw = {"format_version": pipeline.FORMAT_VERSION + 1, "name": "x", "project": "p",
           "steps": [{"id": "a", "source": {"kind": "glob", "pattern": "x"}}]}
    for entry in (lambda: pipeline.normalize_definition(raw),
                  lambda: pipeline.upgrade_definition(raw)):
        with pytest.raises(pipeline.DefinitionError) as exc:
            entry()
        assert exc.value.code == "E_PIPELINE_FORMAT_TOO_NEW"


def test_every_format_level_has_an_upgrade_step():
    """**每一级都要有一个"旧形态样例 → 新形态"的用例**（冻结 F3）。

    今天只有 v1，所以这张表是空的 —— 这条断言钉的是"加一个版本"必须与"写一个
    它的升级步骤"是同一个动作：`_UPGRADES` 的键集必须恰好是 1..FORMAT_VERSION-1。
    没有它，加版本的人可以只改常量、不写升级，而那要到有人拿着旧定义来跑的那天
    才显形。
    """
    assert set(pipeline._UPGRADES) == set(range(1, pipeline.FORMAT_VERSION))


# ---------------------------------------------------------------------------
# A7 —— 定义不是事实（双向）
# ---------------------------------------------------------------------------


def test_a7_saving_a_definition_creates_no_job_and_runs_changed_nothing(env):
    """**A7 双向**：保存定义 → `jobs` 行数不变、`activity_log` 多一条
    `pipeline.save`；跑一次 → `pipeline_definitions` 逐行不变、`jobs` 多 N 行。

    定义不产生贡献，把它塞进 job 会让"撤到哪"这个问题同时有两个答案。
    """
    conn, tmp_path = env
    definition = _definition(tmp_path, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"},
    ])
    project_id = ops.create_project(conn, "演示项目", SCOPE)

    def jobs() -> int:
        return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def saves() -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='pipeline.save'"
        ).fetchone()[0])

    before_jobs, before_saves = jobs(), saves()
    saved = ops.save_pipeline(conn, project_id=project_id, definition=definition,
                              actor="test")
    assert jobs() == before_jobs, "保存一个定义不该建任何 job"
    assert saves() == before_saves + 1

    run_id = ops.start_pipeline_run(
        conn, project_id=project_id, pipeline_id=int(saved["id"]),
        definition_hash=saved["definition_hash"], format_version=1, step_count=1,
        actor="test")
    defn_row_before = _definition_rows(conn)
    report = _run(conn, dict(definition, _project_id=project_id,
                             _pipeline_id=int(saved["id"])))
    ops.finish_pipeline_run(conn, run_id, state="done", report=report,
                            job_ids=report["job_ids"], actor="test")

    assert _definition_rows(conn) == defn_row_before, \
        "跑一次不该改定义（行数 / revision / updated_at / definition_hash 都不变）"
    assert jobs() > before_jobs, "跑一次必须建 job —— 否则它没有写任何事实"
    assert saves() == before_saves + 1, "跑一次不该再写一条 pipeline.save"


def _definition_rows(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT id, name, format_version, definition, definition_hash, revision,"
        " created_at, created_by, updated_at, updated_by, archived_at"
        " FROM pipeline_definitions ORDER BY id")]


def test_a7_optimistic_lock_refuses_a_stale_revision(env):
    """乐观锁：`revision` 不匹配 → `PipelineConflict`。

    两个编辑器互相覆盖**不会有任何症状** —— 后保存的人看到的是自己刚写的，先
    保存的人以为自己写进去了，而库里只剩一份。
    """
    conn, tmp_path = env
    project_id = ops.create_project(conn, "演示项目", SCOPE)
    definition = _definition(tmp_path, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"}}])
    saved = ops.save_pipeline(conn, project_id=project_id, definition=definition,
                              actor="test")
    updated = dict(definition, name="改过名字")
    fresh = ops.save_pipeline(conn, project_id=project_id, definition=updated,
                              actor="test", pipeline_id=int(saved["id"]),
                              expected_revision=1)
    assert fresh["revision"] == 2
    with pytest.raises(ops.PipelineConflict):
        ops.save_pipeline(conn, project_id=project_id,
                          definition=dict(updated, name="再来一次"), actor="test",
                          pipeline_id=int(saved["id"]), expected_revision=1)


# ---------------------------------------------------------------------------
# A4 / A5 —— 批量跑 = 1 run + N job（全是 ingest），干跑说真话
# ---------------------------------------------------------------------------


def test_a4_every_job_of_a_run_is_an_ingest_job_and_matches_the_report(env):
    """**A4**：`SELECT kind FROM jobs WHERE pipeline_run_id = :run` 全部是
    `'ingest'`，且它的**集合**与报告里的 `job_ids` **相等**。

    防的是"顺手新开一个 `kind='pipeline_run'` 的 job" —— 那会让
    `explain_job_scope` / `job_counts` / GUI 的 undo 按钮在三处同时失去对一个
    job 的解释能力。
    """
    conn, tmp_path = env
    result = _batch_run(conn, [
        {"id": "dns", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"},
        {"id": "probe", "source": {"kind": "glob", "pattern": "recon/*httpx*.jsonl"},
         "after": ["dns"]},
    ])
    kinds = [str(r["kind"]) for r in conn.execute(
        "SELECT kind FROM jobs WHERE pipeline_run_id=?", (result["run_id"],))]
    assert kinds and set(kinds) == {"ingest"}, kinds
    assert sorted(int(j) for j in result["report"]["job_ids"]) == \
        sorted(int(r["id"]) for r in conn.execute(
            "SELECT id FROM jobs WHERE pipeline_run_id=?", (result["run_id"],)))


def test_a5_dry_run_totals_equal_real_run_and_leave_the_database_untouched(env):
    """**A5**：同一个库状态、同一份定义，`dry_run=1` 与 `dry_run=0` 的 `totals`
    逐字段相等；且干跑前后 `pipeline_runs` / `jobs` / `contributions` 行数不变。

    这是 LARI-34 在 A2e 上建立的那条断言的**同形复用**，不是新发明 ——
    一个会说假话的干跑比没有干跑更糟，人会照着它的数做判断。
    """
    conn, tmp_path = env
    definition = _definition(tmp_path, [
        {"id": "dns", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"},
        {"id": "probe", "source": {"kind": "glob", "pattern": "recon/*httpx*.jsonl"},
         "after": ["dns"]},
    ])

    def counts() -> dict[str, int]:
        return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                for t in ("pipeline_runs", "jobs", "contributions")}

    before = counts()
    dry = _run(conn, definition, dry_run=True)
    assert counts() == before, f"干跑留了东西：{before} → {counts()}"
    real = _batch_run(conn, definition["steps"])["report"]
    assert dry["totals"] == real["totals"], (dry["totals"], real["totals"])


def test_a5_dry_run_does_not_create_a_pipeline_run_row(env):
    """冻结 F10：**干跑不写 `pipeline_runs` 行**（这也是那一列上没有 `dry_run`
    的原因 —— 一列永远等于 0 的数字比没有这一列更糟，它会被读成"查过了"）。"""
    conn, tmp_path = env
    definition = _definition(tmp_path, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"}])
    _run(conn, definition, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# A8 / A9 —— 批量 undo / redo
# ---------------------------------------------------------------------------


def test_a8_two_consecutive_dry_undo_runs_report_identically(env):
    """**A8**：同一个库状态上连续预演两次，两份报告**逐字段相同**；预演之后
    `jobs WHERE applied=0` 与 `contribution_supersessions WHERE reason='retract'`
    的行数都不变。

    预演的命令级事务边界必须**包住整批**：每个 job 各自 BEGIN/ROLLBACK 的话，
    第 2 个 job 看不见第 1 个刚撤掉的东西，于是"预演说撤 12 条、真跑撤 15 条"
    成了一句假话。
    """
    conn, tmp_path = env
    result = _batch_run(conn, [
        {"id": "dns", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"},
        {"id": "probe", "source": {"kind": "glob", "pattern": "recon/*httpx*.jsonl"},
         "after": ["dns"]},
    ])
    run_id = result["run_id"]

    def retracts() -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM contribution_supersessions WHERE reason='retract'"
        ).fetchone()[0])

    def undone() -> int:
        return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE applied=0"
                                ).fetchone()[0])

    first = ops.undo_run(conn, run_id, actor="test", dry_run=True)
    assert (undone(), retracts()) == (0, 0), "预演改了数据 —— 那不是预演"
    second = ops.undo_run(conn, run_id, actor="test", dry_run=True)
    assert first["undone"] == second["undone"] and first["undone"], first
    for key in ("undone", "already_undone", "stopped_at", "error", "applied"):
        assert first[key] == second[key], key
    assert (undone(), retracts()) == (0, 0)


def test_a9_undo_run_then_redo_run_restores_row_for_row(env):
    """**A9**：真跑一次 3 个文件的批量跑 → 快照 → `undo_run` → `redo_run` →
    断言与快照**逐行指纹相等**（复用 `store.effective_equivalent`，与 2.6 的
    `undo` / `redo` 用例同一套判据）。

    判据是逐行指纹，**不是条数**：条数相同、内容不同是完全可能的。
    """
    conn, tmp_path = env
    (tmp_path / "recon" / "extra.txt").write_text("extra.example.com\n", encoding="utf-8")
    result = _batch_run(conn, [
        {"id": "all", "source": {"kind": "dir", "pattern": "recon"}},
    ])
    run_id, report = result["run_id"], result["report"]
    assert len(report["job_ids"]) == 3, report["job_ids"]

    baseline = store.snapshot_effective(conn)
    assert len(store.active_contribution_ids(conn)) > 0, "前提不成立：这次跑没落事实"

    undone = ops.undo_run(conn, run_id, actor="test")
    assert sorted(undone["undone"], reverse=True) == undone["undone"], "撤销必须逆序"
    assert undone["applied"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_run_id=?"
                        " AND applied=1", (run_id,)).fetchone()[0] == 0

    redone = ops.redo_run(conn, run_id, actor="test")
    assert redone["redone"] == sorted(redone["redone"]), "重放必须正序"
    assert redone["applied"] == 1

    ok, diff = store.effective_equivalent(conn, baseline)
    assert ok, f"undo_run + redo_run 之后逐行指纹不等：{json.dumps(diff, default=str)[:800]}"


def test_undo_run_skips_jobs_that_were_already_undone_individually(env):
    """已经单独撤过的 job：`undo_run` 只撤 `applied = 1` 的那些，已撤的列进
    `already_undone` —— 于是"撤一次撤到哪"的答案是**稳定的**（撤到这次跑之前），
    即使中间有人单独撤过几个。
    """
    conn, tmp_path = env
    result = _batch_run(conn, [
        {"id": "all", "source": {"kind": "dir", "pattern": "recon"}},
    ])
    job_ids = sorted(int(j) for j in result["report"]["job_ids"])
    ops.undo(conn, job_ids[0], actor="test")               # 单独撤掉最早那一个
    batch = ops.undo_run(conn, result["run_id"], actor="test")
    assert batch["already_undone"] == [job_ids[0]]
    assert sorted(batch["undone"]) == job_ids[1:]
    assert batch["stopped_at"] is None and batch["applied"] == 0


def test_undo_jobs_stops_at_the_first_failure_and_says_where(env):
    """部分失败：第 k 个抛了 → **停下**，不继续，并把"哪几个撤了、卡在哪一个、
    为什么"说清楚。**不假装原子** —— 数据层从不假装。

    反例：第 1 个是个不存在的 job（`ops.undo` 抛 `InvariantError`）。
    """
    conn, tmp_path = env
    result = _batch_run(conn, [
        {"id": "all", "source": {"kind": "dir", "pattern": "recon"}},
    ])
    job_ids = sorted(int(j) for j in result["report"]["job_ids"])
    ghost = max(job_ids) + 1000                              # 逆序第一个就是它 → 立刻停
    batch = ops.undo_jobs(conn, job_ids + [ghost], actor="test")
    assert batch["undone"] == [] and batch["stopped_at"] == ghost
    assert batch["error"] and "not found" in batch["error"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE applied=0").fetchone()[0] == 0, \
        "停下之前一个都没撤 —— 但这是因为它卡在第一个，不是因为它假装原子"


def test_undo_run_keeps_applied_when_it_stops_early(env, monkeypatch):
    """部分失败时 `run.applied` **保持 1**（这一批还没整体失效），下一个
    `undo_run` 会从剩下的接着撤 —— 幂等且可重入。

    `applied` 只说自己：它**不**因为一次失败就翻 0，否则"这一批还有效果吗"会
    答错；而"撤到哪"由每个 job 自己的 `applied` 回答。

    失败用**打桩**制造（把中间那个 job 的 `ops.undo` 换成会抛的）：部分失败是
    一条语义，不是某种坏数据才会走到的分支，用打桩测它才是真的在测那条语义。
    """
    conn, tmp_path = env
    (tmp_path / "recon" / "extra.txt").write_text(
        "extra.example.com\n", encoding="utf-8")             # 三个 job，才谈得上"中间"
    result = _batch_run(conn, [
        {"id": "all", "source": {"kind": "dir", "pattern": "recon"}},
    ])
    run_id = result["run_id"]
    order = sorted((int(j) for j in result["report"]["job_ids"]), reverse=True)
    assert len(order) == 3
    victim = order[1]                                        # 逆序走到第二个时炸
    real_undo = ops.undo

    def flaky(conn_, job_id, **kwargs):
        if int(job_id) == victim:
            raise InvariantError("模拟：这个 job 撤不动")
        return real_undo(conn_, job_id, **kwargs)

    monkeypatch.setattr(ops, "undo", flaky)
    batch = ops.undo_run(conn, run_id, actor="test")
    assert batch["undone"] == [order[0]], "它撤了第一个就该停下"
    assert batch["stopped_at"] == victim and "撤不动" in batch["error"]
    assert batch["applied"] == 1, "部分失败时 run.applied 不该翻 0"
    assert conn.execute("SELECT applied FROM pipeline_runs WHERE id=?",
                        (run_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_run_id=?"
                        " AND applied=0", (run_id,)).fetchone()[0] == 1

    # **可重入**：把打桩撤掉再撤一次，它会从剩下的接着撤，而不是从头再来。
    monkeypatch.setattr(ops, "undo", real_undo)
    again = ops.undo_run(conn, run_id, actor="test")
    assert again["already_undone"] == [order[0]]
    assert sorted(again["undone"]) == sorted([order[1], order[2]])
    assert again["applied"] == 0


def test_undo_run_refuses_a_missing_run(env):
    """不存在的 run → `PipelineNotFound`（不是静默返回一份空报告）。"""
    conn, _ = env
    with pytest.raises(ops.PipelineNotFound):
        ops.undo_run(conn, 999, actor="test")


def test_jobs_of_two_projects_cannot_share_a_run(env):
    """跨项目兜底：挂在 A 项目的 run 上的 job，必须也是 A 项目的。

    与 v3.2 §2.4 的 Project Isolation 同一条 —— 项目边界不能靠调用方记得。
    这条不走 `finish_pipeline_run`（它总是先解析出同一个项目），而是直接
    UPDATE，让 `schema/008` 的 `trg_job_run_same_project` 说话：一条**只在
    正常路径上成立**的约束不是约束。
    """
    conn, tmp_path = env
    definition = _definition(tmp_path, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"}])
    project_a = ops.create_project(conn, "A 项目", SCOPE)
    project_b = ops.create_project(conn, "B 项目", SCOPE)
    saved_b = ops.save_pipeline(conn, project_id=project_b, definition=definition,
                                actor="test")
    run_b = ops.start_pipeline_run(
        conn, project_id=project_b, pipeline_id=int(saved_b["id"]),
        definition_hash=str(saved_b["definition_hash"]), format_version=1,
        step_count=1, actor="test")
    job_a = ops.start_job(conn, project_id=project_a, kind="ingest", coverage="full")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute("UPDATE jobs SET pipeline_run_id=? WHERE id=?", (run_b, job_a))
    assert "必须同属一个项目" in str(exc.value)
    assert conn.execute("SELECT pipeline_run_id FROM jobs WHERE id=?",
                        (job_a,)).fetchone()[0] is None


# ---------------------------------------------------------------------------
# 报告里的一根线：进度回调
# ---------------------------------------------------------------------------


def test_run_pipeline_forwards_progress_to_ingest(env):
    """`run_pipeline` 必须把 `on_progress` 交给 `ingest`。

    自己数数的进度条迟早会说"80%"，而报告说"100%" —— 进度只有一个来源。
    """
    conn, tmp_path = env
    events: list[dict] = []
    _run(conn, _definition(tmp_path, [
        {"id": "a", "source": {"kind": "glob", "pattern": "recon/subfinder.txt"},
         "parser": "domainlist"}]),
        on_progress=events.append)
    kinds = [e.get("event") for e in events]
    assert "file_start" in kinds and "job_done" in kinds, kinds
    assert any(e.get("parser") for e in events), "job_start 事件必须带解析器"
