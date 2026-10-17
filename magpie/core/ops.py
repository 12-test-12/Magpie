"""事务型状态迁移（P0-D · 审阅点 11 / 12 / 9 / 10）。

统一模板 T1:
    operation -> impact_set -> provenance -> recompute -> integrity -> commit

四个操作（undo / rekey / merge / compact）全部走同一模板，且全部支持 --dry-run，
dry-run 必须走**同一代码路径**（只有 COMMIT 变成 ROLLBACK），否则 dry-run 会说谎。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import normalize, scope as scope_module
from . import store
from . import query
from .store import (
    UNHASHED_PARSER,
    InvariantError,
    NewContribution,
    add_contribution,
    apply_purge,
    assert_integrity,
    atomic,
    canonical_json,
    connect,
    entity_for_key,
    fingerprint,
    log_activity,
    now,
    orphan_subjects,
    purge_eligible,
    recompute,
    register_parser,
    scope_hash,
    sha256_hex,
    transaction,
)


# ---------------------------------------------------------------------------
# 批量操作的事务外壳 + 管线定义 / 运行（3.7b）
# ---------------------------------------------------------------------------


class PipelineConflict(InvariantError):
    """乐观锁不匹配 / 名字已被占用。API 层翻成 409 `E_PIPELINE_CONFLICT`。"""


class PipelineNotFound(InvariantError):
    """定义 / 运行不存在。API 层翻成 404 `E_PIPELINE_NOT_FOUND`。"""


@contextlib.contextmanager
def batch_txn(conn: sqlite3.Connection, dry_run: bool,
              shared: bool) -> Iterator[None]:
    """批量操作（N 个 job 的 undo / redo）的**命令级**事务外壳。

    `shared=True` 时**不碰边界** —— 边界在上一级（`ingest.dry_run_scope` 那条
    命令级的）。这条判断只写一次：`undo` / `redo` 各自 `if shared` 一遍，迟早
    会出现"一个判了、一个没判"，而症状是干跑到第二个 job 才炸。

    与 `_JobTxn` 的 `shared` 是同一条纪律："预演看得见前一个的效果"是干跑
    说真话的前提。第 2 个 job 的预演必须看得见第 1 个 job 的撤销效果 ——
    否则"预演说撤 12 条、真跑撤 15 条"就是一份会说假话的预览，而 LARI-34 的
    结论很直白：**一个会说假话的干跑比没有干跑更糟。**
    """
    if shared:
        yield
        return
    with transaction(conn, dry_run=dry_run):
        yield


def _resolve_pipeline(conn: sqlite3.Connection, pipeline_id: int,
                      project_id: int | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM pipeline_definitions WHERE id=?",
                       (int(pipeline_id),)).fetchone()
    if row is None:
        raise PipelineNotFound(f"管线定义 {pipeline_id} 不存在")
    if project_id is not None and int(row["project_id"]) != int(project_id):
        # 跨项目访问必须响亮 —— 静默返回"没找到"会让调用方去建一个同名的，
        # 于是同一个 id 在两个项目里各有一份谁也不知道的定义。
        raise PipelineNotFound(
            f"管线定义 {pipeline_id} 不属于项目 {project_id}"
            f"（它属于项目 {int(row['project_id'])}）")
    return row


def _pipeline_row(row: sqlite3.Row, *, created: bool = False) -> dict[str, Any]:
    return {"id": int(row["id"]), "project_id": int(row["project_id"]),
            "name": str(row["name"]), "format_version": int(row["format_version"]),
            "definition_hash": str(row["definition_hash"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]), "created_by": str(row["created_by"]),
            "updated_at": str(row["updated_at"]), "updated_by": str(row["updated_by"]),
            "archived_at": None if row["archived_at"] is None else str(row["archived_at"]),
            "created": bool(created)}


def save_pipeline(conn: sqlite3.Connection, *, project_id: int,
                  definition: dict[str, Any], actor: str = "cli",
                  pipeline_id: int | None = None,
                  expected_revision: int | None = None) -> dict[str, Any]:
    """建（`pipeline_id is None`）或保存（带 `expected_revision`）一条定义。

    【冻结 F4 —— 定义不是事实】**保存一个定义不建 job、不产生 contribution、
    不参与 undo / redo。** 它的审计走 `activity_log`（`pipeline.save`）与
    `revision` 乐观锁 —— 那两个机制已经够回答"谁在什么时候改了什么"，而把定义
    塞进 job 会让"撤到哪"这个问题同时有两个答案。

    `definition` 会先过一遍 `pipeline.canonical_definition`（去掉 `_path` 一类
    运行时键），再算 `definition_hash`。**这是唯一算 hash 的地方** —— 存进去的
    文本与 hash 由同一段代码产出，所以"hash 与它声称的那份定义对不上"这件事
    在结构上不可能发生。
    """
    from . import pipeline as pipeline_module               # 延迟导入，避免环

    if hasattr(definition, "keys") and "format_version" not in definition:
        raise InvariantError("定义缺少 format_version（库里的定义一律带版本）")
    canonical = pipeline_module.canonical_definition(dict(definition))
    version = int(canonical.get("format_version") or 0)
    if version < 1:
        raise InvariantError("定义缺少 format_version（库里的定义一律带版本）")
    digest = pipeline_module.definition_hash(canonical)
    name = str(canonical.get("name") or "")
    if not name:
        raise InvariantError("管线定义必须带 name")
    text = canonical_json(canonical)
    stamp = now()

    if pipeline_id is None:
        try:
            cur = conn.execute(
                "INSERT INTO pipeline_definitions(project_id, name, format_version,"
                " definition, definition_hash, revision, created_at, created_by,"
                " updated_at, updated_by) VALUES (?,?,?,?,?,1,?,?,?,?)",
                (int(project_id), name, version, text, digest, stamp, actor,
                 stamp, actor))
        except sqlite3.IntegrityError as exc:
            if "pipeline_definitions.project_id, pipeline_definitions.name" in str(exc):
                raise PipelineConflict(
                    f"项目 {project_id} 里已经有一条叫 {name!r} 的管线定义。",
                    ) from exc
            raise
        pipeline_id = int(cur.lastrowid)
        log_activity(conn, actor=actor, action="pipeline.save",
                     target_kind="pipeline", target_id=pipeline_id,
                     detail={"created": True, "name": name, "revision": 1,
                             "definition_hash": digest,
                             "format_version": version})
        return _pipeline_row(_resolve_pipeline(conn, pipeline_id, project_id),
                             created=True)

    current = _resolve_pipeline(conn, pipeline_id, project_id)
    if expected_revision is None:
        raise InvariantError(
            "保存一条既有定义必须带 expected_revision（乐观锁）—— "
            "不带它就是「最后写的人赢」，而两个编辑器互相覆盖不会有任何症状。")
    if int(current["revision"]) != int(expected_revision):
        raise PipelineConflict(
            f"管线定义 {pipeline_id} 的版本已经变了：你手上是 revision="
            f"{expected_revision}，库里是 {int(current['revision'])}。",
            )
    try:
        conn.execute(
            "UPDATE pipeline_definitions SET name=?, format_version=?, definition=?,"
            " definition_hash=?, revision=revision+1, updated_at=?, updated_by=?"
            " WHERE id=? AND project_id=? AND archived_at IS NULL",
            (name, version, text, digest, stamp, actor, int(pipeline_id),
             int(project_id)))
    except sqlite3.IntegrityError as exc:
        if "pipeline_definitions.project_id, pipeline_definitions.name" in str(exc):
            raise PipelineConflict(
                f"项目 {project_id} 里已经有一条叫 {name!r} 的管线定义。") from exc
        raise
    log_activity(conn, actor=actor, action="pipeline.save",
                 target_kind="pipeline", target_id=int(pipeline_id),
                 detail={"created": False, "name": name,
                         "revision": int(current["revision"]) + 1,
                         "definition_hash": digest, "format_version": version})
    return _pipeline_row(_resolve_pipeline(conn, pipeline_id, project_id))


def archive_pipeline(conn: sqlite3.Connection, pipeline_id: int, *,
                     project_id: int | None = None, actor: str = "cli") -> dict[str, Any]:
    """归档（软删）。**不提供 DELETE** —— 已经跑过的 run 靠 `pipeline_id` 指向它，
    物理删除会让历史 run 指向一个不存在的定义。

    幂等：已经归档的定义再归档一次不写第二行审计、不改 `archived_at`。
    """
    row = _resolve_pipeline(conn, pipeline_id, project_id)
    if row["archived_at"] is None:
        conn.execute("UPDATE pipeline_definitions SET archived_at=?, updated_at=?,"
                     " updated_by=? WHERE id=?",
                     (now(), now(), actor, int(pipeline_id)))
        log_activity(conn, actor=actor, action="pipeline.archive",
                     target_kind="pipeline", target_id=int(pipeline_id),
                     detail={"name": str(row["name"])})
    return _pipeline_row(_resolve_pipeline(conn, pipeline_id, project_id))


def start_pipeline_run(conn: sqlite3.Connection, *, project_id: int,
                       pipeline_id: int, definition_hash: str,
                       format_version: int, step_count: int = 0,
                       actor: str = "cli") -> int:
    """建一行 `pipeline_runs`（state='running'），返回 run_id。

    一次"批量跑" = **1 行 run + N 行 jobs**（冻结 F9）。job 的粒度不变 ——
    永远是一个文件一个 job：`raw_files` 的 UNIQUE(project_id, sha256)、
    evidence 的 per-file 归属、`job_counts`、`explain_job_scope`、GUI 的
    `can_undo` / `can_redo` 全部建立在它上面。run 是**分组**，不是新的一种 job。
    """
    row = _resolve_pipeline(conn, pipeline_id, project_id)
    cur = conn.execute(
        "INSERT INTO pipeline_runs(project_id, pipeline_id, definition_hash,"
        " format_version, applied, state, step_count, job_count, actor, started_at)"
        " VALUES (?,?,?,?,1,'running',?,0,?,?)",
        (int(project_id), int(pipeline_id), str(definition_hash),
         int(format_version), int(step_count), str(actor), now()))
    return int(cur.lastrowid)


def finish_pipeline_run(conn: sqlite3.Connection, run_id: int, *,
                        state: str, report: dict[str, Any] | None = None,
                        job_ids: Sequence[int] = (), actor: str = "cli") -> dict[str, Any]:
    """给一次 run 收尾：写报告、把 N 个 job 挂到它上面、记一条 `pipeline.run`。

    **job 的挂载走 UPDATE**，因此 `schema/008` 的 `trg_job_run_same_project`
    守着它（跨项目的 job 挂不上来）。挂载放在收尾而不是开头，是因为 job 的 id
    要等 `run_pipeline` 跑完才知道 —— 中间崩溃留下的是一条 state='running' 的
    run 与若干没有归属的 job，那是**如实**的：那一次确实没跑完。
    """
    if state not in ("running", "done", "failed", "partial", "aborted"):
        raise InvariantError(f"bad pipeline run state {state!r}")
    run = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise PipelineNotFound(f"管线运行 {run_id} 不存在")
    ids = [int(j) for j in job_ids]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE jobs SET pipeline_run_id=? WHERE id IN ({marks})",
                     (int(run_id), *ids))
    conn.execute(
        "UPDATE pipeline_runs SET state=?, report=?, job_count=?, finished_at=?"
        " WHERE id=?",
        (str(state), None if report is None else canonical_json(report), len(ids),
         now(), int(run_id)))
    log_activity(conn, actor=actor, action="pipeline.run", target_kind="pipeline_run",
                 target_id=int(run_id),
                 detail={"pipeline_id": int(run["pipeline_id"]), "state": state,
                         "job_count": len(ids), "applied": int(run["applied"])})
    return pipeline_run_row(conn, int(run_id))


def pipeline_run_row(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None:
        raise PipelineNotFound(f"管线运行 {run_id} 不存在")
    out = dict(row)
    out["report"] = query.json_or_none(out.get("report"))
    out["applied"] = bool(out["applied"])
    return out


# ---------------------------------------------------------------------------
# 项目 / 作业 / 证据（测试与生产共用的最小写入 API）
# ---------------------------------------------------------------------------

def _canonical_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """scope 的规范形式：数组排序（否则同一 scope 会存成两种文本，审计不可比）。"""
    out = dict(scope)
    for key in ("include", "exclude"):
        if isinstance(out.get(key), list):
            out[key] = sorted(out[key])
    return out


def create_project(conn: sqlite3.Connection, name: str, scope: dict[str, Any]) -> int:
    scope = _canonical_scope(scope)
    h = scope_hash(scope)
    fingerprint_value = scope.get("parser_fingerprint")
    if fingerprint_value is not None:
        # 名称级登记：scope 上的 fingerprint 是**一个名字**，这里没有任何实现
        # 可 hash，所以显式标注 UNHASHED（同期必交项 B 的语义分界）。
        register_parser(conn, str(fingerprint_value), definition_hash=UNHASHED_PARSER)
    cur = conn.execute(
        "INSERT INTO projects(name, scope, scope_version, scope_hash, parser_fingerprint)"
        " VALUES (?,?,1,?,?)",
        (name, canonical_json(scope), h, fingerprint_value),
    )
    pid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO project_scope_history(project_id, version, scope, scope_hash,"
        " changed_at, changed_by, reason) VALUES (?,1,?,?,?,'system','init')",
        (pid, canonical_json(scope), h, now()),
    )
    return pid


def _project_row(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, name, status FROM projects WHERE id=?",
                       (int(project_id),)).fetchone()
    if row is None:
        raise InvariantError(f"project {project_id} not found")
    return row


def archive_project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """归档 = **收起来，不是删掉**（`projects.status` 的 CHECK 里 'archived'
    一直躺着，0.20.0 第一次用它）。

    归档项目从默认列表里消失（`GET /projects`、CLI 的隐式解析都只看 active），
    但按名字显式引用仍完全可读 —— 归档是**列表层面的隐藏**，数据一行没动；
    删除才是终结，所以删除要输项目名确认。幂等：已在归档态 → 不写、`changed` 回
    False（重复点归档不是错误，是把状态说了一遍）。
    """
    row = _project_row(conn, project_id)
    changed = str(row["status"]) != "archived"
    if changed:
        conn.execute("UPDATE projects SET status='archived' WHERE id=?",
                     (int(project_id),))
    return {"id": int(project_id), "name": str(row["name"]),
            "status": "archived", "changed": changed}


def restore_project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """归档的逆操作（`archive_project` 的镜像，语义与幂等规则逐字同义）。"""
    row = _project_row(conn, project_id)
    changed = str(row["status"]) != "active"
    if changed:
        conn.execute("UPDATE projects SET status='active' WHERE id=?",
                     (int(project_id),))
    return {"id": int(project_id), "name": str(row["name"]),
            "status": "active", "changed": changed}


def _delete_order(conn: sqlite3.Connection) -> list[str]:
    """硬删的表序 —— 从**现场**的 `PRAGMA foreign_key_list` 算反拓扑，不手写。

    手写清单的症状是"新迁移加了一张表，第一次删除时才炸在 RESTRICT 上" ——
    而那正是这份代码最不能出错的时候（0.20.0 交付时清单已经 24 张带 project_id
    的表，人肉维护不起）。`projects` 是根，排在最后由调用方单独删；环（不该
    存在 —— 本库除自引用外无环）不猜顺序，响亮地失败。
    """
    tables = [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    referrers: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            parent = str(fk[2])
            if parent != table and parent in referrers:
                # 子引用父 → 删的时候子先行。自引用（inbox_items.reopened_from）
                # 不进图：同一条 DELETE 里 SQLite 在语句收尾时才查它（实测通过）。
                referrers[parent].add(table)
    order: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if not (referrers[t] & remaining))
        if not ready:
            raise InvariantError(
                "foreign_key_list 里有环，算不出删除顺序："
                + ", ".join(sorted(remaining)))
        order.extend(ready)
        remaining -= set(ready)
    return order


def delete_project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """**硬删**一个项目与它名下的一切 —— 不可恢复（要可恢复的那条路是归档）。

    行的范围按表分两类，没有第三类：
      * 带 `project_id` 列的表按它删（项目自己的行，24 张）；
      * 不带的只删**经外键挂在本项目行上**的那几行（`entity_identity` ← entities、
        `activity_log` ← jobs）；纯全局词表（kind_registry / relation_vocabulary /
        multivalue_fields / parser_profiles / sources / schema_migrations）一行不碰。

    顺序由 `_delete_order` 现场算（子先行），整个过程包在 `store.transaction`
    里 —— 任何一步失败整棵树回滚成原样，写者队列把异常原样带回路由；
    「删了一半的库」在这套设计里没有立足点。收尾断言 `PRAGMA foreign_key_check`
    为空：顺序算错、哪张表的约束没看见，在**这里**被抓，而不是在下一次读时。
    返回逐表行数（removed）—— 删除的回执要能对账，"应该删了"不算回执。
    """
    project_id = int(project_id)
    row = _project_row(conn, project_id)
    name = str(row["name"])
    removed: dict[str, int] = {}
    with store.transaction(conn):
        tables = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}
        columns = {table: {str(c[1]) for c in conn.execute(f"PRAGMA table_info({table})")}
                   for table in tables}
        for table in _delete_order(conn):
            if table == "projects":
                continue                        # 根，最后单独删
            if "project_id" in columns[table]:
                sql, params = f"DELETE FROM {table} WHERE project_id=?", (project_id,)
            else:
                # 全局表：按"哪张父表带 project_id"逐个外键组拼 IN 子查询。
                groups: dict[int, dict[str, Any]] = {}
                for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
                    entry = groups.setdefault(int(fk[0]),
                                              {"parent": str(fk[2]), "pairs": []})
                    entry["pairs"].append((str(fk[3]), str(fk[4])))
                clauses: list[str] = []
                parts: list[Any] = []
                for entry in groups.values():
                    parent = entry["parent"]
                    if parent == table or "project_id" not in columns.get(parent, set()):
                        continue                # 指向全局表 / 自引用 → 不是本项目的
                    pairs = entry["pairs"]
                    clauses.append("(" + " AND ".join(
                        f"{col} IN (SELECT {pt} FROM {parent} WHERE project_id=?)"
                        for col, pt in pairs) + ")")
                    parts.extend([project_id] * len(pairs))
                if not clauses:
                    continue                    # 纯全局行，不碰
                sql = f"DELETE FROM {table} WHERE " + " OR ".join(clauses)
                params = tuple(parts)
            count = int(conn.execute(sql, params).rowcount)
            if count:
                removed[table] = count
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        removed["projects"] = 1
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise InvariantError(
                f"删除项目 {name!r} 后 foreign_key_check 不干净（前 5 条）："
                f"{violations[:5]}")
    return {"id": project_id, "name": name, "removed": removed}


def update_scope(conn: sqlite3.Connection, project_id: int, scope: dict[str, Any],
                 actor: str = "cli", reason: str = "manual") -> tuple[int, str]:
    """改 scope 必须 bump version（审阅点 7），否则 coverage 判定会撒谎。

    v3.2-r2: parser 指纹随 scope 一起版本化（§5.1）—— 它决定"覆盖"的含义。
    """
    row = conn.execute(
        "SELECT scope_version FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if row is None:
        raise InvariantError(f"project {project_id} not found")
    scope = _canonical_scope(scope)
    version = int(row["scope_version"]) + 1
    h = scope_hash(scope)
    fingerprint_value = scope.get("parser_fingerprint")
    if fingerprint_value is not None:
        register_parser(conn, str(fingerprint_value), definition_hash=UNHASHED_PARSER)
    conn.execute(
        "UPDATE projects SET scope=?, scope_version=?, scope_hash=?, parser_fingerprint=?"
        " WHERE id=?",
        (canonical_json(scope), version, h, fingerprint_value, project_id),
    )
    conn.execute(
        "INSERT INTO project_scope_history(project_id, version, scope, scope_hash,"
        " changed_at, changed_by, reason) VALUES (?,?,?,?,?,?,?)",
        (project_id, version, canonical_json(scope), h, now(), actor, reason),
    )
    log_activity(conn, actor=actor, action="reparse", target_kind="project",
                 target_id=project_id, detail={"scope_version": version, "reason": reason})
    return version, h


def set_parser(conn: sqlite3.Connection, project_id: int, fingerprint: str, *,
               profile_id: str | None = None, version: str | None = None,
               definition_hash: str = UNHASHED_PARSER,
               actor: str = "cli", reason: str = "parser_change") -> dict[str, Any]:
    """切换项目当前 parser 指纹（**事实操作**，v3.2-r2 落库）。

    必须同时做三件事，否则旧 job 会被当前 scope/parser 误解释:
      1. 登记 fingerprint 到 parser_profiles（事实 + 审计）
      2. bump scope_version 并重算 scope_hash（hash 含 fingerprint）
      3. 写 project_scope_history，使 explain_job_scope 能用当时的快照解释旧 job
    """
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise InvariantError(f"project {project_id} not found")
    # 默认是 UNHASHED（调用方通常只有一个名字）；手里真有实现时传进来，
    # 它会**回填**到一个原本只登记了名字的行上（见 store.register_parser）。
    register_parser(conn, fingerprint, profile_id=profile_id, version=version,
                    definition_hash=definition_hash, actor=actor)
    scope = json.loads(str(row["scope"]))
    scope["parser_fingerprint"] = fingerprint
    if profile_id is not None:
        scope["parser_profile"] = profile_id
    new_version, new_hash = update_scope(conn, project_id, scope, actor=actor, reason=reason)
    log_activity(conn, actor=actor, action="parser.set", target_kind="project",
                 target_id=project_id,
                 detail={"fingerprint": fingerprint, "scope_version": new_version})
    return {"op": "set_parser", "project_id": project_id,
            "parser_fingerprint": fingerprint, "scope_version": new_version,
            "scope_hash": new_hash}


def parser_history(conn: sqlite3.Connection, *, profile_id: str | None = None,
                   project_id: int | None = None) -> list[dict[str, Any]]:
    """已登记的 parser 指纹（v3.2-r4 · 债 #6 的"版本化"半边）。

    为什么这是"版本化"而不是一张字典表: 每个 job 冻结了自己跑时的
    `parser_fingerprint`（外键），所以"这个结论是哪个 parser 版本产出的"
    永远可回答。`definition_hash` 是"同一个 fingerprint 背后的实现有没有变过"
    的判据 —— **第一个真解析器落地时必须填**（阶段 2 第 2 步的验收项）。
    """
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM jobs j"
           "                 WHERE j.parser_fingerprint = p.fingerprint) AS job_count"
           "  FROM parser_profiles p")
    args: list[Any] = []
    where: list[str] = []
    if profile_id is not None:
        where.append("p.profile_id = ?")
        args.append(profile_id)
    if project_id is not None:
        where.append("EXISTS (SELECT 1 FROM jobs j2 WHERE j2.parser_fingerprint = p.fingerprint"
                     " AND j2.project_id = ?)")
        args.append(project_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.registered_at, p.fingerprint"
    return [{"fingerprint": str(r["fingerprint"]), "profile_id": r["profile_id"],
             "version": r["version"], "definition_hash": r["definition_hash"],
             "registered_at": str(r["registered_at"]), "job_count": int(r["job_count"])}
            for r in conn.execute(sql, args)]


def rollback_parser(conn: sqlite3.Connection, *, project_id: int,
                    to_fingerprint: str, actor: str = "cli",
                    reason: str = "parser_rollback") -> dict[str, Any]:
    """回滚项目的 parser 指纹（v3.2-r4 · 债 #6 的"回滚"半边）。

    **回滚不是撤销**: 不删任何 parser_profiles 行、不改任何历史 job 的指纹快照，
    只是把"当前指针"改回一个**已登记**的旧指纹。这与 §4.5 的 rekey 同一哲学 ——
    历史是事实，当前状态是覆盖在上面的指针。

    因此回滚天然安全且可再回滚，`explain_job_scope` 对回滚前后的 job 都成立。
    拒绝回滚到未登记的指纹: 那样 coverage 判定会引用一个不存在的版本。
    """
    row = conn.execute(
        "SELECT fingerprint FROM parser_profiles WHERE fingerprint=?", (to_fingerprint,)
    ).fetchone()
    if row is None:
        raise InvariantError(
            f"parser rollback target {to_fingerprint!r} is not registered "
            f"(未登记的指纹无法作为回滚目标)")
    prof = conn.execute(
        "SELECT profile_id, version FROM parser_profiles WHERE fingerprint=?",
        (to_fingerprint,)).fetchone()
    report = set_parser(conn, project_id, to_fingerprint,
                        profile_id=None if prof is None else prof["profile_id"],
                        version=None if prof is None else prof["version"],
                        actor=actor, reason=reason)
    report["op"] = "rollback_parser"
    return report


def start_job(conn: sqlite3.Connection, *, project_id: int, kind: str,
              coverage: str, coverage_scope: dict[str, Any] | None = None,
              parser_fingerprint: str | None = None, actor: str = "cli") -> int:
    """job 必须冻结 coverage + scope + parser 快照（审阅点 7 / v3.2-r2 §5.1）。

    parser 指纹是事实：这里先登记（幂等），再让 jobs 通过 FK 引用它。
    """
    proj = conn.execute(
        "SELECT scope_version, scope_hash FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if proj is None:
        raise InvariantError(f"project {project_id} not found")
    if parser_fingerprint is not None:
        # job 只是**快照**一个名字，实现 hash 该由真正跑解析的那条路先登记好
        # （`ingest` 就是在 start_job 之前登记完整的 —— 顺序反了 definition_hash
        # 会永久留空，那正是 v3.2 §16.4 第 2 条记录的病）。
        # 这里显式标 UNHASHED：轮到这里才第一次出现的 fingerprint 意味着没有
        # 实现参与，而不是"实现没有 hash"。
        register_parser(conn, parser_fingerprint, definition_hash=UNHASHED_PARSER,
                        actor=actor)
    cur = conn.execute(
        "INSERT INTO jobs(project_id, kind, state, coverage, coverage_scope,"
        " scope_version, scope_hash, parser_fingerprint, actor, started_at)"
        " VALUES (?,?,'running',?,?,?,?,?,?,?)",
        (project_id, kind, coverage,
         None if coverage_scope is None else canonical_json(coverage_scope),
         int(proj["scope_version"]), str(proj["scope_hash"]), parser_fingerprint,
         actor, now()),
    )
    job_id = int(cur.lastrowid)
    log_activity(conn, actor=actor, action="job.start", target_kind="job",
                 target_id=job_id, job_id=job_id,
                 detail={"coverage": coverage, "scope_version": int(proj["scope_version"]),
                         "parser_fingerprint": parser_fingerprint})
    return job_id


def finish_job(conn: sqlite3.Connection, job_id: int, state: str = "done",
               actor: str = "cli") -> None:
    if state not in ("done", "failed", "partial", "aborted"):
        raise InvariantError(f"bad job state {state!r}")
    conn.execute("UPDATE jobs SET state=?, finished_at=? WHERE id=?",
                 (state, now(), job_id))
    log_activity(conn, actor=actor, action="job.finish", target_kind="job",
                 target_id=job_id, job_id=job_id, detail={"state": state})


def add_raw_file(conn: sqlite3.Connection, *, project_id: int, locator_uri: str,
                 content: str, storage_state: str = "present") -> int:
    cur = conn.execute(
        "INSERT INTO raw_files(project_id, locator_uri, sha256, byte_count,"
        " storage_state, created_at) VALUES (?,?,?,?,?,?)",
        (project_id, locator_uri, sha256_hex(content), len(content.encode("utf-8")),
         storage_state, now()),
    )
    return int(cur.lastrowid)


def add_evidence(conn: sqlite3.Connection, *, project_id: int, raw_file_id: int,
                 locator: dict[str, Any], snippet: str,
                 quality_class: str = "medium",
                 recovery_state: str = "full") -> int:
    cur = conn.execute(
        "INSERT INTO evidence(project_id, raw_file_id, locator, sha256, quality_class,"
        " recovery_state, created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, raw_file_id, canonical_json(locator), sha256_hex(snippet),
         quality_class, recovery_state, now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 高阶写入：一次调用写 "实体/字段/关系" 的完整贡献闭包
# ---------------------------------------------------------------------------

def upsert_entity(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                  evidence_id: int, source_id: int, entity_key: str, kind: str,
                  authorship: str = "tool", **time_kw: Any) -> int:
    from .store import resolve_entity, new_entity

    with atomic(conn):
        entity_id = resolve_entity(conn, project_id, entity_key)
        if entity_id is None:
            entity_id = new_entity(conn, project_id=project_id, kind=kind,
                                  entity_key=entity_key)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="entity", subject_id=entity_id,
            contribution_kind="entity", authorship=authorship, **time_kw))
    return entity_id


def ensure_observation_row(conn: sqlite3.Connection, *, project_id: int, entity_id: int,
                           semantic_field: str, value: str,
                           value_kind: str = "string", **time_kw: Any) -> int:
    """取出/创建 observation 缓存行（不写贡献；结构列一次性冻结）。"""
    row = conn.execute(
        "SELECT id FROM observations WHERE project_id=? AND entity_id=?"
        " AND field_namespace='observation_field' AND semantic_field=? AND value=?",
        (project_id, entity_id, semantic_field, value),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO observations(project_id, entity_id, field_namespace, semantic_field,"
        " value, value_kind, is_current, confidence, source_count, job_count,"
        " logical_job_count, origin, observed_at, observed_at_source,"
        " observed_at_accuracy, observed_at_precision, created_at)"
        " VALUES (?,?,'observation_field',?,?,?,0,'none',0,0,0,'tool',NULL,NULL,NULL,NULL,?)",
        (project_id, entity_id, semantic_field, value, value_kind, now()),
    )
    return int(cur.lastrowid)


def ensure_relation_row(conn: sqlite3.Connection, *, project_id: int, src_id: int,
                        dst_id: int, rel: str) -> int:
    row = conn.execute(
        "SELECT id FROM relations WHERE project_id=? AND src_id=? AND dst_id=? AND rel=?",
        (project_id, src_id, dst_id, rel),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO relations(project_id, src_id, dst_id, rel, is_current, confidence,"
        " source_count, job_count, created_at) VALUES (?,?,?,?,0,'none',0,0,?)",
        (project_id, src_id, dst_id, rel, now()),
    )
    return int(cur.lastrowid)


def add_entity_field(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                     evidence_id: int, source_id: int, entity_key: str, kind: str,
                     semantic_field: str, value: str, value_kind: str = "string",
                     authorship: str = "tool", **time_kw: Any) -> int:
    """entity 级字段（display_name / cpe / tech 等），不建独立缓存表。"""
    with atomic(conn):
        entity_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                                 evidence_id=evidence_id, source_id=source_id,
                                 entity_key=entity_key, kind=kind, authorship=authorship,
                                 **time_kw)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="entity", subject_id=entity_id,
            contribution_kind="field", authorship=authorship,
            field_namespace="entity_field", semantic_field=semantic_field,
            field_value=value, value_kind=value_kind, **time_kw))
    return entity_id


def add_observation(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                    evidence_id: int, source_id: int, entity_key: str, kind: str,
                    semantic_field: str, value: str, value_kind: str = "string",
                    authorship: str = "tool", **time_kw: Any) -> int:
    """observation 是 field 贡献的规范锚点: 先确保缓存行存在，再写贡献。

    INV-16: 贡献**显式记录** owner_entity_id（observation → entity 的事实链）。
    注意这里**没有**任何 DELETE contributions —— INV-01 无例外（undo 靠 supersession）。
    """
    entity_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                             evidence_id=evidence_id, source_id=source_id,
                             entity_key=entity_key, kind=kind, authorship=authorship,
                             **time_kw)
    with atomic(conn):
        obs_id = ensure_observation_row(conn, project_id=project_id, entity_id=entity_id,
                                        semantic_field=semantic_field, value=value,
                                        value_kind=value_kind, **time_kw)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="observation", subject_id=obs_id,
            owner_entity_id=entity_id, contribution_kind="field", authorship=authorship,
            field_namespace="observation_field", semantic_field=semantic_field,
            field_value=value, value_kind=value_kind, **time_kw))
    return obs_id


def add_relation(conn: sqlite3.Connection, *, project_id: int, job_id: int,
                 evidence_id: int, source_id: int, src_key: str, dst_key: str,
                 rel: str, kind: str = "host",
                 src_kind: str | None = None, dst_kind: str | None = None,
                 authorship: str = "tool",
                 **time_kw: Any) -> int:
    """relation 事实：subject 是 relations 缓存行，INV-16 的 owner 记 **src entity**，
    dst 记在 field_value.dst_id —— 于是 relations 行也能从事实层完整重建。"""
    s_kind = src_kind or kind
    d_kind = dst_kind or kind
    src_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                          evidence_id=evidence_id, source_id=source_id,
                          entity_key=src_key, kind=s_kind, authorship=authorship, **time_kw)
    dst_id = upsert_entity(conn, project_id=project_id, job_id=job_id,
                          evidence_id=evidence_id, source_id=source_id,
                          entity_key=dst_key, kind=d_kind, authorship=authorship, **time_kw)
    with atomic(conn):
        rel_id = ensure_relation_row(conn, project_id=project_id, src_id=src_id,
                                     dst_id=dst_id, rel=rel)
        add_contribution(conn, NewContribution(
            project_id=project_id, job_id=job_id, source_id=source_id,
            evidence_id=evidence_id, subject_kind="relation", subject_id=rel_id,
            owner_entity_id=src_id, contribution_kind="relation", authorship=authorship,
            relation_kind=rel,
            field_value=canonical_json({"dst_id": dst_id, "dst_key": dst_key}),
            **time_kw))
    return rel_id


# ---------------------------------------------------------------------------
# undo（T2）—— 审阅点 4 的正面回答
# ---------------------------------------------------------------------------

def _recompute_subjects_of_job(conn: sqlite3.Connection, job_id: int) -> list[tuple[str, int]]:
    """job 触及的**缓存行**集合（不是贡献的原始 subject）。

    为什么需要映射（v3.2-r3 修正）:
      `contribution_kind='relation'` 的贡献 subject 是 **relations 行本身**，
      结构行跟着变的还有 src（owner_entity_id）与 dst（field_value.dst_id）两个 entity。
      旧实现把 subject_id 当成 src_id 去反查 relations，于是 relation 行从来没被
      重算过（INV-04 会当场抓到 cache_overstated_relation）。
    """
    out: set[tuple[str, int]] = set()
    for row in conn.execute(
        """
        SELECT c.subject_kind, c.subject_id, c.owner_entity_id, c.contribution_kind,
               c.relation_kind, c.field_value
          FROM contributions c WHERE c.job_id = ?
        UNION ALL
        SELECT c.subject_kind, c.subject_id, c.owner_entity_id, c.contribution_kind,
               c.relation_kind, c.field_value
          FROM contribution_supersessions s JOIN contributions c ON c.id = s.contribution_id
         WHERE s.superseded_by_job_id = ?
        """,
        (job_id, job_id),
    ):
        if str(row["contribution_kind"]) == "relation":
            out.add(("relation", int(row["subject_id"])))
            if row["owner_entity_id"] is not None:
                out.add(("entity", int(row["owner_entity_id"])))
            try:
                out.add(("entity", int(_dst_of(row["field_value"]))))
            except (TypeError, ValueError, KeyError):
                pass
        else:
            out.add((str(row["subject_kind"]), int(row["subject_id"])))
            if row["owner_entity_id"] is not None:
                out.add(("entity", int(row["owner_entity_id"])))
    return sorted(out)


def _dst_of(field_value: Any) -> int:
    """从 relation 贡献的 field_value 里取 dst_id（关系的另一端点）。"""
    return int(json.loads(str(field_value))["dst_id"])


def undo(conn: sqlite3.Connection, job_id: int, *, actor: str = "cli",
         dry_run: bool = False, purge: bool = True,
         _shared_txn: bool = False) -> dict[str, Any]:
    """撤销一个 job（T2）。v3.2-r4: **完全可逆** —— 与 `redo` 对称。

    事实层: 追加 retract 抑制事实（不删贡献、不改贡献，INV-01 无例外）。
    身份层: 不再 DELETE alias / merge members，而是写**生命周期边**
            （`revoked_by_job_id` / `unmerged_by_job_id`）——
            否则 alias 的行内容一删，redo 就再也无法重建（key 映射不在事实层里）。
    工作层: 关闭 J 打开的自动 inbox 项（`closed_by_job_id`）；
            undo 自己打开的孤儿项打上 `opened_by_undo_job_id`，供 redo 关闭；
            **把 J 写下的 `work_journal` 逐行放回 `before`**（3.8：批量改状态 —
            `entities.lifecycle` 不是事实层，没有贡献可撤，能撤的只有"改之前的
            那个值"，见迁移 009 的文件头）。
    执行状态: 只改 `jobs.applied = 0`，**不再**把 state 改成 'aborted'
            （"跑完了但被撤销" 与 "执行失败" 是两件事）。

    幂等: 已撤销的 job 再次 undo 不会重复写边（都带 `IS NULL` 守卫）。

    前置条件: `conn` 必须是 `store.connect()` 建出来的连接。本函数按列名取值
    （`row["contribution_kind"]` 等），裸 `sqlite3.connect()` 的行是 tuple，
    会以 `TypeError` 坏在内部 —— 因此入口先守卫（审核 LARI-21）。

    `_shared_txn` 是**内部**参数（3.7b）：为真时本函数不自持事务边界，由调用方
    （`undo_jobs` / `undo_run`）持着 —— 批量撤销时每个 job 各开一次事务的话，
    干跑的"预演"就看不见前一个 job 已经撤掉的东西，于是"预演说撤 12 条、真跑
    撤 15 条"成了一句假话。见 `ingest._JobTxn` 的同一套 `shared` 纪律。
    """
    store.require_row_factory(conn, caller="ops.undo")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")

    with batch_txn(conn, dry_run, _shared_txn):
        # ---- 1. impact_set ----
        subjects = _recompute_subjects_of_job(conn, job_id)
        impacted_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM contributions WHERE job_id=?", (job_id,))]

        # ---- 2. provenance: 追加 retract 抑制事实 ----
        report_superseded = 0
        for cid in impacted_ids:
            exists = conn.execute(
                "SELECT 1 FROM contribution_supersessions WHERE contribution_id=?", (cid,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO contribution_supersessions(project_id, contribution_id,"
                " superseded_by_job_id, reason, created_at, actor) VALUES (?,?,?,?,?,?)",
                (int(job["project_id"]), cid, job_id, "retract", now(), actor),
            )
            report_superseded += 1
        log_activity(conn, actor=actor, action="supersession.add", target_kind="job",
                     target_id=job_id, job_id=job_id,
                     detail={"retract": report_superseded})

        # ---- 3. 身份层: 撤销 J 建立的 alias / merge 事件（写边，不删行）----
        alias_revoked = conn.execute(
            "UPDATE entity_alias SET revoked_by_job_id=?, revoked_at=?"
            " WHERE created_by_job_id=? AND revoked_by_job_id IS NULL",
            (job_id, now(), job_id),
        ).rowcount
        merge_events = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM entity_merge_events WHERE created_by_job_id=? AND state='merged'",
            (job_id,))]
        members_revoked = 0
        for ev in merge_events:
            members_revoked += conn.execute(
                "UPDATE entity_merge_members SET revoked_by_job_id=?, revoked_at=?"
                " WHERE merge_event_id=? AND revoked_by_job_id IS NULL",
                (job_id, now(), ev)).rowcount
            conn.execute(
                "UPDATE entity_merge_events SET state='unmerged', unmerged_at=?,"
                " unmerged_by_job_id=? WHERE id=? AND state='merged'",
                (now(), job_id, ev))

        # ---- 4. 工作层: J 自动生成的 inbox 项 ----
        # 这一条是**按作业**的便宜路径: 撤销一个作业，先把它自己开出来的待办
        # 关掉。它对 `value_conflict` 之外的每一类都是完备的（那些项的主体就是
        # 那个作业），对冲突项只是**近似** —— 一条冲突完全可能是更早的作业开的，
        # 而这次撤销把它弄没了。精确的收口在下一步的对账。
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='undo',"
            " closed_by_job_id=? WHERE created_by_job_id=? AND state='open'"
            " AND auto_generated=1 AND closed_by_job_id IS NULL",
            (now(), job_id, job_id),
        ).rowcount

        # ---- 4b. 工作层: 冲突项跟**视图**对齐（不是跟 job）----
        # 放在这里而不是 recompute 之后: 视图读的是 contributions（事实层），
        # 不读缓存投影，而第 2 步的 retract 边已经写完了 —— 所以此刻视图就是
        # "撤销之后"的视图。放在这一组里也让工作层的三步（4 关、4b 对账、
        # 5 回放 work_journal）读起来是同一条流水线。
        conflict_report = reconcile_conflicts(conn, project_id=int(job["project_id"]),
                                              job_id=job_id)
        inbox_closed += conflict_report["closed"]

        # ---- 5. 工作层: J 写下的 work_journal 逐行放回 before（3.8）----
        # 放在 recompute **之前**：lifecycle 不参与缓存投影，两者互不影响，
        # 但"先在库里恢复事实、再一次性重算"是这一整个函数的形状，别在这里破例。
        work_reverted = _replay_work_journal(conn, job_id, direction="undo", actor=actor)

        # ---- 6. 缓存重算（delta） ----
        recompute(conn, delta=subjects)

        # ---- 7. GC 评估（v3.2-r2 起默认不物理删除）----
        purge_report: dict[str, Any] = {"purge": [], "retained": [], "orphan_subjects": []}
        if purge:
            purge_report = purge_eligible(conn)
            for orphan in purge_report["orphan_subjects"]:
                kind, raw = orphan.split(":", 1)
                # `created` 有意弃用: 无论这条 open 项是新建的还是**复用**已存在的，
                # 它都是"undo 打开的"，redo 都必须关掉它（否则 redo 之后残留一个
                # 语义已经不成立的孤儿项）。旧写法 `if created or True` 是恒真的
                # 死条件，读起来像有分支、实际没有。
                item_id, _ = _open_inbox(
                    conn, project_id=int(job["project_id"]), kind="orphan_subject",
                    subject_kind=kind, subject_id=int(raw),
                    values=[], sources=[], job_id=job_id,
                    payload={"reason": "no_contribution_ever"})
                # 标记"这条 open 项是 undo 打开的"，redo 会关闭它
                conn.execute(
                    "UPDATE inbox_items SET opened_by_undo_job_id=?"
                    " WHERE id=? AND opened_by_undo_job_id IS NULL", (job_id, item_id))
            apply_purge(conn, purge_report["purge"], actor=actor, job_id=job_id)
            for item in purge_report["retained"]:
                log_activity(conn, actor=actor, action="gc.retained",
                             target_kind=item["subject"].split(":")[0],
                             target_id=int(item["subject"].split(":")[1]),
                             job_id=job_id, detail={"blockers": item["blockers"]})

        # ---- 7. 应用状态 + integrity ----
        conn.execute("UPDATE jobs SET applied=0 WHERE id=?", (job_id,))
        assert_integrity(conn)

        # ---- 8. 审计 ----
        report = {
            "op": "undo",
            "job_id": job_id,
            "dry_run": dry_run,
            "impact": {
                "contributions": len(impacted_ids),
                "superseded": report_superseded,
                "alias_revoked": alias_revoked,
                "merge_members_revoked": members_revoked,
                "merge_events_reverted": len(merge_events),
                # `inbox_closed` **含**第 4b 步对账关掉的那些: 它们同样是"这次
                # 撤销关掉的待办"，刨掉它们会让这个数停在真话与假话之间。
                "inbox_closed": inbox_closed,
                # 一个数，不是一列 id：`impact` 的每一个键都是计数，而**这条响应
                # 是既有形状**（加键要按 CHANGELOG 的规矩升 minor 登记）。逐行的
                # 明细在 `activity_log` 的 `work.undo` 里 —— 审计要的是明细，
                # 契约要的是稳定。
                "work_reverted": len(work_reverted),
                "purge_eligible": purge_report["purge"],
                "purge_retained": purge_report["retained"],
                "inbox_opened": len(purge_report["orphan_subjects"])
                                + conflict_report["opened"],
            },
            "integrity": {"ok": True},
        }
        log_activity(conn, actor=actor, action="undo", target_kind="job",
                     target_id=job_id, job_id=job_id, detail=report["impact"])
        return report


def redo(conn: sqlite3.Connection, job_id: int, *, actor: str = "cli",
         dry_run: bool = False, _shared_txn: bool = False) -> dict[str, Any]:
    """重新应用一个被 undo 的 job（T2′）。`redo` 与 `undo` 严格互逆。

    动作 = **清掉所有指向 J 的生命周期边** + `jobs.applied = 1`：
      事实层: DELETE `contribution_supersessions WHERE superseded_by_job_id=J
              AND reason='retract'`（每次 DELETE 都写 `supersession.lift`）。
              **只解除 retract 行** —— 那是 undo 的产物；
              `reason='compact'` 等行是 J 自己的效果，redo 不得触碰，
              否则被吞并的原始贡献与合成行会同时活跃、守恒被破坏。
      身份层: 解除 `revoked_by_job_id=J`（alias / merge members）；
              重新 unmerge 那些"由 J 反向合并回去"的事件（幂等对称）。
      工作层: 重开 `closed_by_job_id=J` 的项（若同 fingerprint 已有 open 项，
              则并入它的 occurrence_count 而不破唯一索引）；关闭
              `opened_by_undo_job_id=J` 的项；**把 J 的 `work_journal` 按 `after`
              再写一遍**（3.8，与 `undo` 的 `before` 严格对称）。
      应用状态: `applied=1`。

    幂等: 已经没有 retract 行且 `applied=1` 时，返回 `{"noop": True}` 报告。

    前置条件: 同 `undo` —— `conn` 必须是 `store.connect()` 建出来的连接
    （入口守卫，审核 LARI-21）。`_shared_txn` 的语义同 `undo`。
    """
    store.require_row_factory(conn, caller="ops.redo")
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")
    project_id = int(job["project_id"])

    with batch_txn(conn, dry_run, _shared_txn):
        retract_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM contribution_supersessions WHERE superseded_by_job_id=?"
            " AND reason='retract'", (job_id,))]
        alias_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM entity_alias WHERE revoked_by_job_id=?", (job_id,))]
        member_rows = [(int(r["merge_event_id"]), int(r["entity_id"])) for r in conn.execute(
            "SELECT merge_event_id, entity_id FROM entity_merge_members"
            " WHERE revoked_by_job_id=?", (job_id,))]
        closed_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM inbox_items WHERE closed_by_job_id=?", (job_id,))]
        opened_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM inbox_items WHERE opened_by_undo_job_id=?", (job_id,))]

        if (not retract_ids and int(job["applied"]) == 1 and not alias_ids
                and not member_rows and not closed_ids and not opened_ids):
            return {"op": "redo", "job_id": job_id, "dry_run": dry_run, "noop": True,
                    "impact": {"supersessions_lifted": 0, "alias_restored": 0,
                               "merge_members_restored": 0, "inbox_reopened": 0,
                               "inbox_closed": 0, "blocked": []},
                    "integrity": {"ok": True}}

        # ---- 1. 事实层: 解除 undo 造成的抑制（只动 reason='retract'）----
        for sid in retract_ids:
            conn.execute("DELETE FROM contribution_supersessions WHERE id=?", (sid,))
            log_activity(conn, actor=actor, action="supersession.lift",
                         target_kind="supersession", target_id=sid, job_id=job_id,
                         detail={"reason": "retract"})

        # ---- 2. 身份层: 解除撤销边 ----
        blocked: list[dict[str, Any]] = []
        alias_restored = 0
        for aid in alias_ids:
            row = conn.execute(
                "SELECT project_id, alias_key FROM entity_alias WHERE id=?", (aid,)
            ).fetchone()
            conflict = conn.execute(
                "SELECT id, entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL", (int(row["project_id"]),
                                                  str(row["alias_key"]))).fetchone()
            if conflict is not None:
                # 更新的身份决定已经占用了这个 key → 保留撤销边，汇报而不是静默覆盖
                blocked.append({"alias_id": aid, "alias_key": str(row["alias_key"]),
                                "conflicting_alias_id": int(conflict["id"])})
                continue
            conn.execute(
                "UPDATE entity_alias SET revoked_by_job_id=NULL, revoked_at=NULL WHERE id=?",
                (aid,))
            alias_restored += 1
        members_restored = 0
        for event_id, entity_id in member_rows:
            members_restored += conn.execute(
                "UPDATE entity_merge_members SET revoked_by_job_id=NULL, revoked_at=NULL"
                " WHERE merge_event_id=? AND entity_id=?", (event_id, entity_id)).rowcount
        # 重新合并"由 undo(J) 拆开"的事件（undo 把 state 改成 unmerged 并写下边）
        merge_events_restored = conn.execute(
            "UPDATE entity_merge_events SET state='merged', unmerged_at=NULL,"
            " unmerged_by_job_id=NULL WHERE unmerged_by_job_id=?", (job_id,)).rowcount

        # ---- 3. 工作层: 重开 / 关闭 inbox ----
        inbox_reopened = 0
        for iid in closed_ids:
            row = conn.execute(
                "SELECT project_id, kind, subject_kind, subject_id, fingerprint"
                " FROM inbox_items WHERE id=?", (iid,)).fetchone()
            conflict = conn.execute(
                "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
                " AND subject_id=? AND fingerprint=? AND state='open'",
                (int(row["project_id"]), str(row["kind"]), str(row["subject_kind"]),
                 int(row["subject_id"]), str(row["fingerprint"]))).fetchone()
            if conflict is not None:
                conn.execute(
                    "UPDATE inbox_items SET occurrence_count=occurrence_count+1,"
                    " last_seen_at=? WHERE id=?", (now(), int(conflict["id"])))
                conn.execute(
                    "UPDATE inbox_items SET closed_by_job_id=NULL WHERE id=?", (iid,))
                continue
            conn.execute(
                "UPDATE inbox_items SET state='open', resolved_at=NULL, resolved_by=NULL,"
                " close_reason=NULL, closed_by_job_id=NULL, last_seen_at=? WHERE id=?",
                (now(), iid))
            inbox_reopened += 1
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='redo',"
            " opened_by_undo_job_id=NULL WHERE opened_by_undo_job_id=?",
            (now(), job_id)).rowcount

        # ---- 3c. 工作层: 冲突项跟**视图**对齐（undo 的 4b 的镜像）----
        # 上面那一步重开的是 `closed_by_job_id=J` 的项 —— 它覆盖了"J 的 undo 关掉
        # 的"那一批，而 J 的 redo 恰恰会让那些冲突回来。这里再对一次账，是为了
        # 覆盖两种它够不到的情况: 撤销期间别的路径（例如先 undo、再单独 undo 了
        # 另一个作业）在 J 的边上留下的空隙，以及"重开之后值又变了"（指纹不同，
        # 上面那条按原件重开的项已经不是当前的值集）。对账以视图为准，所以它
        # 无论走哪条路径进来，最终状态都一致。
        conflict_report = reconcile_conflicts(conn, project_id=project_id, job_id=job_id)
        inbox_reopened += conflict_report["opened"]

        # ---- 3b. 工作层: J 的 work_journal 按 after 再写一遍（3.8）----
        work_replayed = _replay_work_journal(conn, job_id, direction="redo", actor=actor)

        # ---- 4. 缓存重算 + 应用状态 + integrity ----
        recompute(conn, delta=_recompute_subjects_of_job(conn, job_id))
        # 部分成功时保持 applied=0（还有撤销边未恢复），保证
        # "边存在 ⇒ 该 job 未完全应用" 这条不变式成立；人工处置后再 redo。
        conn.execute("UPDATE jobs SET applied=? WHERE id=?",
                     (0 if blocked else 1, job_id))
        assert_integrity(conn)

        report = {
            "op": "redo",
            "job_id": job_id,
            "dry_run": dry_run,
            "noop": False,
            "applied": 0 if blocked else 1,
            "impact": {
                "supersessions_lifted": len(retract_ids),
                "alias_restored": alias_restored,
                "merge_members_restored": members_restored,
                "merge_events_restored": merge_events_restored,
                "inbox_reopened": inbox_reopened,
                "inbox_closed": inbox_closed,
                "work_replayed": len(work_replayed),
                "blocked": blocked,
            },
            "integrity": {"ok": True},
        }
        log_activity(conn, actor=actor, action="redo", target_kind="job",
                     target_id=job_id, job_id=job_id, detail=report["impact"])
        return report


# ---------------------------------------------------------------------------
# 批量跑的撤销与重放（3.7b）—— 唯一可逆原语仍然是 **job**
# ---------------------------------------------------------------------------
#
# `undo` 的单位永远是 job（冻结 F11）。一次"批量跑"的撤销 = 把这一批的 N 个
# job **逐个 undo**，不是发明一个"撤销一个 run"的新原语。这不是偷懒，是唯一
# 说得通的选法：2.6 冻结的 undo / redo 精确互逆，其证据是**逐行指纹相等**
# （B 组用例），而逐行指纹是每个 job 的贡献算出来的 —— 任何自己实现一套状态
# 迁移的"批量撤销"，都必须独立地再证一遍它与 redo 互逆，而那份证明会与 job
# 的那份分叉。
#
# **顺序是冻结的**：撤销**逆序**（job id 从大到小 —— 后写的先撤，从栈顶往下
# 拆）；重放**正序**（从小到大 —— 按原来的顺序）。撤销是拆栈，重放是重放，
# 两个方向各有各的对，都写死。


def _run_job_ids(conn: sqlite3.Connection, run_id: int, *,
                 order: str = "DESC") -> list[int]:
    direction = "DESC" if order.upper() == "DESC" else "ASC"
    return [int(r["id"]) for r in conn.execute(
        f"SELECT id FROM jobs WHERE pipeline_run_id=? ORDER BY id {direction}",
        (int(run_id),))]


def _applied_ids(conn: sqlite3.Connection, job_ids: Sequence[int],
                 applied: int) -> list[int]:
    ids = [int(j) for j in job_ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return [int(r["id"]) for r in conn.execute(
        f"SELECT id FROM jobs WHERE id IN ({marks}) AND applied=? ORDER BY id",
        (*ids, applied))]


def undo_jobs(conn: sqlite3.Connection, job_ids: Sequence[int], *,
              actor: str = "cli", dry_run: bool = False,
              _shared_txn: bool = False) -> dict[str, Any]:
    """**撤销一批 job 的唯一实现**（逆序：后写的先撤，从栈顶往下拆）。

    `imports.ImportRegistry.undo`（`/imports/{id}/undo`）与 `undo_run`
    （`/runs/{id}/undo`）**都调它**。理由与 3.5 把两条 SQL 收敛进
    `core/query.py` 逐字相同：两份实现不会同时出错，也不会以同样的方式出错，
    而"同一批作业、两种撤销结果"没有任何症状。

    **部分失败**：第 k 个抛了 → **停下**，不继续。**不假装原子** —— 数据层从不
    假装（"取消：已提交部分保留，仍可 undo"是同一条哲学），它做的是把状态
    说清楚：哪几个撤了、卡在哪一个、为什么。`stopped_at` + `error` 就是那句话说
    出来的地方，可重入：再调一次会从剩下的接着撤。
    """
    ordered = sorted({int(j) for j in job_ids}, reverse=True)
    reports: list[dict[str, Any]] = []
    undone: list[int] = []
    stopped_at: int | None = None
    error: str | None = None
    with batch_txn(conn, dry_run, _shared_txn):
        for jid in ordered:
            try:
                reports.append(undo(conn, jid, actor=actor, dry_run=dry_run,
                                    _shared_txn=True))
            except Exception as exc:                       # noqa: BLE001 —— 见 docstring
                stopped_at, error = jid, f"{type(exc).__name__}: {exc}"
                break
            undone.append(jid)
    return {"op": "undo.jobs", "dry_run": dry_run, "undone": undone,
            "reports": reports, "stopped_at": stopped_at, "error": error}


def redo_jobs(conn: sqlite3.Connection, job_ids: Sequence[int], *,
              actor: str = "cli", dry_run: bool = False,
              _shared_txn: bool = False) -> dict[str, Any]:
    """`undo_jobs` 的镜像：**正序**重放（按原来的顺序），部分失败同样停下。"""
    ordered = sorted({int(j) for j in job_ids})
    reports: list[dict[str, Any]] = []
    redone: list[int] = []
    stopped_at: int | None = None
    error: str | None = None
    with batch_txn(conn, dry_run, _shared_txn):
        for jid in ordered:
            try:
                reports.append(redo(conn, jid, actor=actor, dry_run=dry_run,
                                    _shared_txn=True))
            except Exception as exc:                       # noqa: BLE001
                stopped_at, error = jid, f"{type(exc).__name__}: {exc}"
                break
            redone.append(jid)
    return {"op": "redo.jobs", "dry_run": dry_run, "redone": redone,
            "reports": reports, "stopped_at": stopped_at, "error": error}


def undo_run(conn: sqlite3.Connection, run_id: int, *, actor: str = "cli",
             dry_run: bool = False, _shared_txn: bool = False) -> dict[str, Any]:
    """撤销一次"批量跑" = **把这一批的 N 个 job 逐个 undo**（冻结 F11）。

    刻意**不发明一个"撤销一个 run"的新原语**：2.6 冻结的 `undo` / `redo` 精确
    互逆，其证据是**逐行指纹相等**（B 组用例），而逐行指纹是每个 job 的贡献算
    出来的。任何"批量撤销"如果自己实现一套状态迁移，就必须独立地再证一遍它与
    redo 互逆 —— 而那份证明会与 job 的那份分叉。

    **已经单独撤过的 job**：只撤 `applied = 1` 的那些，已撤的列进
    `already_undone` 并在报告里如实列出。这样"撤一次撤到哪"的答案是稳定的
    —— **撤到这次跑之前**，即使中间有人单独撤过几个。

    **部分失败**：`run.applied` 保持 1（这一批还没整体失效），下一个
    `undo_run` 会从剩下的接着撤 —— 幂等且可重入。整批撤完才翻 0。

    `run.applied` 只动这一列，**不把 `state` 改成 aborted**（冻结 F12）：
    "跑完了但后来被撤销" 与 "执行失败" 是两件事。
    """
    store.require_row_factory(conn, caller="ops.undo_run")
    run = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise PipelineNotFound(f"管线运行 {run_id} 不存在")
    order = _run_job_ids(conn, run_id, order="DESC")           # 逆序：后写的先撤
    targets = _applied_ids(conn, order, 1)
    already = [j for j in order if j not in set(targets)]

    batch = undo_jobs(conn, targets, actor=actor, dry_run=dry_run,
                      _shared_txn=_shared_txn)
    applied = 1
    with batch_txn(conn, dry_run, _shared_txn):
        remaining = _applied_ids(conn, order, 1)
        if batch["stopped_at"] is None and not remaining:
            conn.execute("UPDATE pipeline_runs SET applied=0 WHERE id=?", (int(run_id),))
            applied = 0
        log_activity(conn, actor=actor, action="pipeline.undo_run",
                     target_kind="pipeline_run", target_id=int(run_id),
                     detail={"undone": batch["undone"], "already_undone": already,
                             "stopped_at": batch["stopped_at"], "applied": applied})
    return {"op": "undo.run", "run_id": int(run_id), "dry_run": bool(dry_run),
            "pipeline_id": int(run["pipeline_id"]),
            "undone": batch["undone"], "already_undone": already,
            "stopped_at": batch["stopped_at"], "error": batch["error"],
            "reports": batch["reports"], "applied": applied}


def redo_run(conn: sqlite3.Connection, run_id: int, *, actor: str = "cli",
             dry_run: bool = False, _shared_txn: bool = False) -> dict[str, Any]:
    """重新应用一次被撤销的批量跑。**正序**（从小到大 —— 按原来的顺序重放）。"""
    store.require_row_factory(conn, caller="ops.redo_run")
    run = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (int(run_id),)).fetchone()
    if run is None:
        raise PipelineNotFound(f"管线运行 {run_id} 不存在")
    order = _run_job_ids(conn, run_id, order="ASC")
    targets = _applied_ids(conn, order, 0)
    already = [j for j in order if j not in set(targets)]

    batch = redo_jobs(conn, targets, actor=actor, dry_run=dry_run,
                      _shared_txn=_shared_txn)
    applied = 0
    with batch_txn(conn, dry_run, _shared_txn):
        remaining = _applied_ids(conn, order, 0)
        if batch["stopped_at"] is None and not remaining:
            conn.execute("UPDATE pipeline_runs SET applied=1 WHERE id=?", (int(run_id),))
            applied = 1
        log_activity(conn, actor=actor, action="pipeline.redo_run",
                     target_kind="pipeline_run", target_id=int(run_id),
                     detail={"redone": batch["redone"], "already_applied": already,
                             "stopped_at": batch["stopped_at"], "applied": applied})
    return {"op": "redo.run", "run_id": int(run_id), "dry_run": bool(dry_run),
            "pipeline_id": int(run["pipeline_id"]),
            "redone": batch["redone"], "already_applied": already,
            "stopped_at": batch["stopped_at"], "error": batch["error"],
            "reports": batch["reports"], "applied": applied}


# ---------------------------------------------------------------------------
# 批量改状态（3.8）—— 工作层写入，走同一套 undo
# ---------------------------------------------------------------------------
#
# `entities.lifecycle` 是 D 工作层的**人工状态**（schema/001 的列注释:
# "业务生命周期（人工/流程）"），不是事实层的投影 —— 导入不能把人工退休的资产
# 复活。所以它没有贡献可撤，`ops.undo` 那套"追加 retract 抑制边"在它身上无事可做。
#
# 撤销它需要的是一件这里从来没有过的东西：**改之前的那个值**。存它的地方是
# `work_journal`（迁移 009，理由见那个文件头）。于是可逆性的形状与其它 op 一致：
#
#     undo(J)  = 把 J 在 work_journal 里写下的每一行按 `before` 放回去
#     redo(J)  = 按 `after` 再写一遍
#
# **为什么"批量"是一个 job，而不是 N 个 job。**
#
# 3.7b 留下的批量通路（`undo_jobs` / `undo_run`）是**N 个 job + 逆序逐个撤销**，
# 它对 ingest 是对的：一个文件一个 job 的粒度是数据层的纪律（F9），撤到一半停下
# 并说清停在哪，在那里的语义是"如实"。但 3.8 的硬线是另一条：
#
#     **批量操作要么全成要么全不成（事务），不许半批成功。**
#
# 这一条管的是**正向**这一次批量：N 个 job 的形状让"撤销"天然可以半批完成
# （`stopped_at`），而批量改状态要的恰好是它的反面 —— 一次点击改 30 个实体的
# 状态，不能出现"撤回了 17 个、还剩 13 个退休着"的中间态。要让"原子"在撤销侧
# 也成立，唯一说得通的形状就是 **1 个 job**（一个事务、一条日志、一次撤销）。
#
# 这不是"另造第二套批量机制"：批量改状态调用的仍然是 `ops.undo` / `ops.undo_jobs`
# 这**一份**实现（1 个 job 的批量走 `undo_jobs([jid])`，与走 `undo(jid)` 逐字同一条
# 代码路径）。真正的"第二套"是把 N 个 job 的 id 记在某处、再写一个"撤销这一批"
# 的新原语 —— 那需要再造一张分组表（`pipeline_runs` 是管线定义的下游，改实体
# 挂不上去），而那张表就是第二套批量机制的物理形态。
#
# 顺带: job 粒度纪律（F9「一个文件一个 job」）约束的是 **ingest**，不是工作层
# 写入 —— 它的理由（`raw_files` 的 UNIQUE(project_id, sha256)、evidence 的
# per-file 归属、`job_counts`、`explain_job_scope`）一条都不适用于"把 30 个实体
# 标成 retired"。

#: `entities.lifecycle` 的合法取值（唯一声明处 = schema/001 的 CHECK）。
#: 两处都写不是重复：这一份让错误在**排队之前**就能报成一句人话，
#: 而不是一次 SQLite CHECK 失败。
LIFECYCLES = ("active", "retired")

#: 工作层里可以被 job 撤销的列（`work_journal.field` 的取值域）。
#: 今天只有 lifecycle。加一个取值时要同时想清楚 undo / redo 怎么放回去。
WORK_FIELDS = ("lifecycle",)


def set_lifecycle(conn: sqlite3.Connection, *, project_id: int,
                  entity_ids: Sequence[int], lifecycle: str,
                  job_id: int | None = None, reason: str | None = None,
                  actor: str = "cli", dry_run: bool = False,
                  _shared_txn: bool = False) -> dict[str, Any]:
    """**批量**改一组实体的生命周期（active / retired）。一次调用 = 一个 job。

    * **要么全成要么全不成。** 整批在一个事务里；任何一个实体不存在 / 不属于
      本项目 → **在写第一行之前**就抛，一个字都不落库（错误信息把有问题的 id
      全列出来，而不是只报第一个 —— 只报第一个会让人改一轮再跑一轮）。
    * **幂等**：已经是目标状态的实体不写日志、不进 `changed`，如实列进
      `unchanged`。于是 undo 报告的 `reverted` 数的是"真的被改回去的行数"。
    * **可撤销**：撤销走 `ops.undo(job_id)` / `ops.undo_jobs([job_id])`，
      判据与 2.6 的 B 组同一条 —— `store.effective_equivalent` 逐行指纹相等。
    """
    store.require_row_factory(conn, caller="ops.set_lifecycle")
    if lifecycle not in LIFECYCLES:
        raise InvariantError(
            f"lifecycle 只能是 {list(LIFECYCLES)} 之一，收到 {lifecycle!r}")
    ids = sorted({int(e) for e in entity_ids})
    if not ids:
        raise InvariantError("批量改状态至少要给一个实体")

    with batch_txn(conn, dry_run, _shared_txn):
        # ---- 1. 前置校验（整批，在写之前）----
        found: dict[int, str] = {}
        for eid in ids:
            row = conn.execute(
                "SELECT lifecycle FROM entities WHERE id=? AND project_id=?",
                (eid, project_id)).fetchone()
            if row is not None:
                found[eid] = str(row["lifecycle"])
        missing = [eid for eid in ids if eid not in found]
        if missing:
            # 跨项目访问与不存在在这里是同一句话的两种原因：都必须是**响亮**的
            # 失败。静默跳过会让"我改了 30 个"变成一个没人核对的数字。
            raise InvariantError(
                f"这些实体不在项目 {project_id} 里（不存在或属于别的项目）：{missing}")

        job_id = _identity_job(conn, project_id, "lifecycle", job_id, actor)
        changed: list[int] = []
        unchanged: list[int] = []
        for eid in ids:
            before = found[eid]
            if before == lifecycle:
                unchanged.append(eid)
                continue
            conn.execute("UPDATE entities SET lifecycle=? WHERE id=? AND project_id=?",
                         (lifecycle, eid, project_id))
            conn.execute(
                "INSERT INTO work_journal(project_id, job_id, entity_id, field,"
                " before_json, after_json, created_at) VALUES (?,?,?,'lifecycle',?,?,?)",
                (project_id, job_id, eid, canonical_json(before),
                 canonical_json(lifecycle), now()))
            changed.append(eid)

        finish_job(conn, job_id, "done", actor=actor)
        report = {"op": "lifecycle.set", "dry_run": bool(dry_run),
                  # 干跑**不给** job_id：那个 id 会随末尾的 ROLLBACK 一起消失，
                  # 而把它交出去等于给调用方一个可以拿去 /jobs/{id}/undo 的把手
                  # —— 换回来的是一个 500。报告里那句 `dry_run: true` 已经说清
                  # 这一次什么都没有留下。
                  "job_id": None if dry_run else job_id,
                  "lifecycle": lifecycle, "reason": reason,
                  "changed": changed, "unchanged": unchanged,
                  "counts": {"requested": len(ids), "changed": len(changed),
                             "unchanged": len(unchanged)}}
        log_activity(conn, actor=actor, action="lifecycle.set", target_kind="job",
                     target_id=job_id, job_id=job_id,
                     detail={"lifecycle": lifecycle, "changed": changed,
                             "unchanged": unchanged, "reason": reason})
        return report


def add_finding(conn: sqlite3.Connection, *, project_id: int, entity_id: int,
                text: str, actor: str = "cli", dry_run: bool = False,
                _shared_txn: bool = False) -> dict[str, Any]:
    """AI 发现通道：把一段散文提交为实体上的观测（0.21.0 · `jobs.kind="ai_note"`）。

    设计要点（缺一不可，理由写在这里免得后人"简化"）：

    * **append-only + 可撤销免费**：事实以 contribution 落库（INV-01，唯一写入口
      `store.add_contribution`），撤销走 `ops.undo(job_id)` 的 supersession ——
      undo/redo 对 job kind 是通用的，这里一行撤销代码都不需要。
    * **`authorship="manual"`**：`store.recompute` 看到 manual 贡献就把
      `observations.origin` 翻成 `manual` → DetailPanel 的「人工写入」分组
      自然展示，且 `manual_owned` 把实体锚成 GC 保护对象。
    * **source 是 `unverified`（0.6）不是 `manual`（2.0）**：AI 的话不该拿
      人工录入的最高权重 —— `confidence.AUTHORITY_TERM` 是唯一数据源。
    * **`semantic_field` 内容寻址（`ai_note:<sha256前12>`）**：字段名随文本走。
      固定字段名会让同一实体的第二条发现撞 `v_field_conflicts`（>1 distinct
      value），在下一次 import 的 `reconcile_conflicts` 里变成假 Inbox 项；
      声明 multivalue 又要一次迁移（禁止）。每段文本一个字段 → 结构上不可能
      冲突；同一段文本重复提交 → 同字段同值仍是 1 个 distinct value，无冲突。
    * **刻意不调** `reconcile_conflicts` / `refresh_unknown_fields` / scope 系列
      （ingest 专属）与 `log_activity`（ACTION_NAMESPACE 冻结；`job.start` /
      `job.finish` / `contribution.add` 已覆盖审计）。也不写 `work_journal` ——
      追加型事实的撤销走 supersession，不走工作层回放。
    """
    store.require_row_factory(conn, caller="ops.add_finding")
    note = (text or "").strip()
    if not note:
        raise InvariantError("发现文本不能为空")

    with batch_txn(conn, dry_run, _shared_txn):
        # ---- 1. 前置校验（写第一行之前）----
        row = conn.execute(
            "SELECT kind FROM entities WHERE id=? AND project_id=?",
            (entity_id, project_id)).fetchone()
        if row is None:
            # 跨项目访问与不存在是同一句话的两种原因：都必须响亮失败
            # （与 set_lifecycle 同一条纪律，消息形状保持一致）。
            raise InvariantError(
                f"实体 {entity_id} 不在项目 {project_id} 里（不存在或属于别的项目）")
        kind = str(row["kind"])
        # 展示 key 走 store.display_key —— GUI / MCP / 搜索层的强制入口，
        # 自己拼 alias 会绕过 canonical / merge 的物化结果。
        entity_key = store.display_key(conn, entity_id)
        if not entity_key:
            raise InvariantError(f"实体 {entity_id} 没有可用的 entity_key")

        job_id = _identity_job(conn, project_id, "ai_note", None, actor)

        # ---- 2. 证据链：raw_file → evidence（两列 NOT NULL 的代价）----
        digest = sha256_hex(note)
        raw_id = add_raw_file(conn, project_id=project_id,
                              locator_uri=f"ai-note://{job_id}/note-{digest[:12]}.txt",
                              content=note)
        evidence_id = add_evidence(
            conn, project_id=project_id, raw_file_id=raw_id,
            locator={"kind": "ai_note", "job_id": job_id,
                     "entity_id": entity_id, "offset": 0, "length": len(note)},
            snippet=note, quality_class="medium", recovery_state="full")
        source_id = store.register_source(conn, name="ai:add_finding",
                                          authority_level="unverified",
                                          tool="add_finding", actor=actor)

        # ---- 3. 身份一致性守卫：key 解析必须回到同一个 id ----
        # add_observation 内部按 key upsert；若 alias 层把 key 解到别的实体，
        # 这里先炸，而不是悄悄把发现写到别人身上。
        resolved = store.resolve_entity(conn, project_id, entity_key)
        if resolved != entity_id:
            raise InvariantError(
                f"INV-05 变体: 实体 {entity_id} 的 key {entity_key!r} "
                f"解析到 {resolved}，不一致")

        # ---- 4. 事实：observation 贡献（authorship=manual）----
        semantic_field = f"ai_note:{digest[:12]}"
        obs_id = add_observation(
            conn, project_id=project_id, job_id=job_id, evidence_id=evidence_id,
            source_id=source_id, entity_key=entity_key, kind=kind,
            semantic_field=semantic_field, value=note, value_kind="string",
            authorship="manual", observed_at_source="manual")

        # ---- 5. 缓存层重建（origin → manual 就在这一步发生）----
        touched = _recompute_subjects_of_job(conn, job_id)
        store.recompute(conn, delta=touched)

        finish_job(conn, job_id, "done", actor=actor)
        return {"op": "ai_note.add", "dry_run": bool(dry_run),
                # 干跑不给 job_id（同 set_lifecycle：它会随 ROLLBACK 消失，
                # 交出去等于给一个换回 500 的把手）。
                "job_id": None if dry_run else job_id,
                "entity_id": entity_id, "observation_id": obs_id,
                "semantic_field": semantic_field, "source": "ai:add_finding",
                "text_length": len(note)}


FINDING_STATUSES = ("candidate", "confirmed", "false_positive", "duplicate", "inconclusive", "resolved")
FINDING_SEVERITIES = ("critical", "high", "medium", "low", "info")
RETEST_STATUSES = ("still_vulnerable", "resolved", "inconclusive")


def create_manual_finding(conn: sqlite3.Connection, *, project_id: int,
                          title: str, severity: str, target_entity_id: int,
                          template_id: str | None = None,
                          description: str | None = None,
                          evidence_id: int | None = None,
                          status: str = "confirmed",
                          reason: str | None = None,
                          actor: str = "analyst",
                          dry_run: bool = False,
                          _shared_txn: bool = False) -> dict[str, Any]:
    """Compatibility facade for finding_ops.create_manual_finding."""
    from .finding_ops import create_manual_finding as operation
    return operation(
        conn, project_id=project_id, title=title, severity=severity,
        target_entity_id=target_entity_id, template_id=template_id,
        description=description, evidence_id=evidence_id, status=status,
        reason=reason, actor=actor, dry_run=dry_run, _shared_txn=_shared_txn)


def triage_finding(conn: sqlite3.Connection, *, project_id: int, finding_id: int,
                   status: str, severity: str | None = None,
                   reason: str | None = None, duplicate_of_id: int | None = None,
                   actor: str = "analyst") -> dict[str, Any]:
    """Compatibility facade for finding_ops.triage_finding."""
    from .finding_ops import triage_finding as operation
    return operation(
        conn, project_id=project_id, finding_id=finding_id, status=status,
        severity=severity, reason=reason, duplicate_of_id=duplicate_of_id, actor=actor)


def add_retest(conn: sqlite3.Connection, *, project_id: int, finding_id: int,
               status: str, notes: str, evidence_id: int | None = None,
               actor: str = "analyst") -> dict[str, Any]:
    """Compatibility facade for finding_ops.add_retest."""
    from .finding_ops import add_retest as operation
    return operation(
        conn, project_id=project_id, finding_id=finding_id, status=status,
        notes=notes, evidence_id=evidence_id, actor=actor)


def _replay_work_journal(conn: sqlite3.Connection, job_id: int, *, direction: str,
                         actor: str) -> list[dict[str, Any]]:
    """把 J 写下的工作层日志按 `direction` 放回去（undo→before / redo→after）。

    每条日志一个 (entity_id, field) 对，`UNIQUE(job_id, entity_id, field)` 保证
    它们互不重叠 —— 所以"按什么顺序回放"不影响结果，这里仍然**倒序**：与
    `undo_jobs` 拆栈的方向一致，读日志时不必解释为什么这里例外。

    只写**真的变了**的那些（`UPDATE` 的 rowcount），于是报告里的 `reverted`
    数是"这个 job 造成过的净变化"，不是"摸过的行数"。
    """
    column = "before_json" if direction == "undo" else "after_json"
    rows = conn.execute(
        f"SELECT id, entity_id, field, before_json, after_json FROM work_journal"
        f" WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        field = str(row["field"])
        if field not in WORK_FIELDS:                       # pragma: no cover - 兜底
            raise InvariantError(
                f"work_journal.field={field!r} 没有对应的回放规则（WORK_FIELDS 是"
                f"这份清单的唯一声明处）")
        raw = row[column]
        value = None if raw is None else json.loads(str(raw))
        cur = conn.execute(
            f"UPDATE entities SET {field}=? WHERE id=? AND {field} IS NOT ?",
            (value, int(row["entity_id"]), value))
        if cur.rowcount:
            out.append({"entity_id": int(row["entity_id"]), "field": field,
                        "value": value, "journal_id": int(row["id"])})
    if out:
        log_activity(conn, actor=actor,
                     action="work.revert" if direction == "undo" else "work.replay",
                     target_kind="job", target_id=job_id, job_id=job_id,
                     detail={"entries": out})
    return out


# ---------------------------------------------------------------------------
# Inbox（审阅点 16：partial unique index + occurrence_count）
# ---------------------------------------------------------------------------

def _open_inbox(conn: sqlite3.Connection, *, project_id: int, kind: str,
                subject_kind: str, subject_id: int, values: Iterable[str],
                sources: Iterable[Any], job_id: int | None = None,
                payload: dict[str, Any] | None = None,
                severity: str = "medium",
                conflict_field: str | None = None) -> tuple[int, bool]:
    """返回 (item_id, created)。重复冲突只累加 occurrence_count，不新增行。

    v3.2-r2: fingerprint 必须包含 conflict_field（§6.3）——否则同一主体上
    两个**不同字段**的冲突会折叠成同一条 open item，后出现的冲突静默丢失。
    重复插入由 partial unique index 兜底（并发下 SELECT-then-INSERT 会撞索引）。
    """
    fp = fingerprint(kind, subject_kind, subject_id, conflict_field, values, sources)
    row = conn.execute(
        "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
        " AND subject_id=? AND fingerprint=? AND state='open'",
        (project_id, kind, subject_kind, subject_id, fp),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE inbox_items SET occurrence_count = occurrence_count + 1,"
            " last_seen_at=? WHERE id=?", (now(), int(row["id"])),
        )
        return int(row["id"]), False
    try:
        cur = conn.execute(
            "INSERT INTO inbox_items(project_id, kind, subject_kind, subject_id, fingerprint,"
            " severity, state, payload_json, occurrence_count, created_by_job_id,"
            " auto_generated, created_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,'open',?,1,?,1,?,?)",
            (project_id, kind, subject_kind, subject_id, fp, severity,
             None if payload is None else canonical_json(payload), job_id, now(), now()),
        )
    except sqlite3.IntegrityError:
        # 并发下另一个 writer 已插入同一 fingerprint → 退化为累加
        row = conn.execute(
            "SELECT id FROM inbox_items WHERE project_id=? AND kind=? AND subject_kind=?"
            " AND subject_id=? AND fingerprint=? AND state='open'",
            (project_id, kind, subject_kind, subject_id, fp),
        ).fetchone()
        if row is None:
            raise
        conn.execute(
            "UPDATE inbox_items SET occurrence_count = occurrence_count + 1,"
            " last_seen_at=? WHERE id=?", (now(), int(row["id"])),
        )
        return int(row["id"]), False
    item_id = int(cur.lastrowid)
    log_activity(conn, actor="store", action="inbox.open", target_kind="inbox_item",
                 target_id=item_id, job_id=job_id,
                 detail={"kind": kind, "field": conflict_field})
    return item_id, True


def _live_conflicts(conn: sqlite3.Connection,
                    project_id: int) -> list[dict[str, Any]]:
    """`v_field_conflicts` 的当前内容 + **与 `_open_inbox` 逐字一致的指纹**。

    为什么指纹要在这里算，而不是在 reconcile 里另写一份判据
    ------------------------------------------------------
    "这条待办还成不成立"必须和"当初为什么开它"用**同一个**判据。分开写两份的
    结局是它们会漂移，而漂移的症状是"视图里有、Inbox 里没有"（漏报）或反过来
    （残骸）—— 两种都不会有任何东西变红。所以生产（`_open_inbox` 里的
    `fingerprint(...)`）与对账（本函数）共用 `store.fingerprint` 这一个入口，
    参数也按同一个顺序、同一份清洗规则（`_open_inbox` 内部对 values / sources
    排序去重）给。
    """
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT subject_kind, subject_id, field_namespace, semantic_field, values_csv"
        "  FROM v_field_conflicts WHERE project_id=?", (project_id,)
    ):
        values = str(row["values_csv"]).split(",")
        sources = [r["source_id"] for r in conn.execute(
            "SELECT DISTINCT source_id FROM v_active_contributions"
            " WHERE subject_kind=? AND subject_id=? AND semantic_field=?",
            (str(row["subject_kind"]), int(row["subject_id"]), str(row["semantic_field"])),
        )]
        out.append({
            "subject_kind": str(row["subject_kind"]),
            "subject_id": int(row["subject_id"]),
            "field_namespace": str(row["field_namespace"]),
            "semantic_field": str(row["semantic_field"]),
            "values": values,
            "sources": sources,
            "fingerprint": fingerprint(
                "value_conflict", str(row["subject_kind"]), int(row["subject_id"]),
                str(row["semantic_field"]), values, sources),
        })
    return out


def refresh_observation_conflicts(conn: sqlite3.Connection, project_id: int, *,
                                  job_id: int | None = None) -> int:
    """把"同 entity 同字段多值"登记为 inbox 冲突（主体是 entity，见 §9 视图注释）。

    `job_id` 是**生命周期锚点**（缺陷 B 的前一半）: 传了它，开出来的项就带
    `created_by_job_id`，于是 `undo` 那条"按 job 关待办"的边能够到它。
    **但它不是全部** —— 锚点只解决"这个 job 自己开的那条"，而一条冲突完全可能
    是**更早的作业**开的、被**这次撤销**弄没的（见 `reconcile_conflicts`）。

    哪些字段算"多值"由 `multivalue_fields` 声明表说了算（迁移 010），不在
    这里判断 —— 视图已经把它们排掉了，所以本函数的输入天然只剩真冲突。

    它**只开不关**。生命周期那一半在 `reconcile_conflicts` 里 —— 生产代码走
    那一个（导入 / 撤销 / 重做都走它）。本函数留着是因为它是"打开一条冲突项"
    这件事的**唯一写法**，对账复用它的下半段（`_open_one_conflict`）；单测也会
    直接调它来构造"视图里有、Inbox 里没有"的前提。
    """
    opened = 0
    for live in _live_conflicts(conn, project_id):
        _, created = _open_one_conflict(conn, project_id=project_id, live=live,
                                        job_id=job_id)
        opened += 1 if created else 0
    return opened


def _open_one_conflict(conn: sqlite3.Connection, *, project_id: int,
                       live: dict[str, Any], job_id: int | None) -> tuple[int, bool]:
    """开一条冲突项。**这是"冲突项长什么样"的唯一落点**。

    生产（`refresh_observation_conflicts`）与对账（`reconcile_conflicts`）都
    必须从这里走: 两处各写一份 `_open_inbox(...)` 的话，它们在 fingerprint
    参数、payload 形状、job_id 锚点上的任何一处改动都可能只落在一半上 ——
    而分叉的症状（重开出来的项与对账算的指纹对不上）不会让任何东西变红。
    """
    return _open_inbox(
        conn, project_id=project_id, kind="value_conflict",
        subject_kind=live["subject_kind"], subject_id=live["subject_id"],
        values=live["values"], sources=live["sources"],
        conflict_field=live["semantic_field"], job_id=job_id,
        payload={"field": live["semantic_field"], "values": live["values"]},
    )


def reconcile_conflicts(conn: sqlite3.Connection, *, project_id: int,
                        job_id: int | None = None) -> dict[str, int]:
    """把 `value_conflict` 待办与**视图当前的内容**对齐（缺陷 B 的后一半）。

    冲突项是**派生状态**: 它的真值永远是"`v_field_conflicts` 现在有没有这一条"，
    而不是"哪个作业开过它"。所以生命周期必须跟着视图走，两条都要成立:

    * 库里有冲突 → **必须看得见**。若某条冲突在视图里而 Inbox 里没有 open 项
      （撤销之后又被 redo 带回来、或先前的对账漏掉），这里**开/重开**它；
    * 库里没有冲突了 → **不许**留着一条 open 项。视图里没有的 open 项，这里
      按 `close_reason='reconcile'` 关掉。

    为什么"锚 job"与"对账"两件都要做（而不是只做一件）
    --------------------------------------------------
    只锚 job: 能关掉"这个作业自己开的"那条，但一条**更早的作业**开的冲突项
    （`created_by_job_id` 是别人）在它的贡献被撤掉之后会**留在那里** ——
    库里已经没有冲突了，Inbox 说还有。反向的"只按 job 关掉"则更糟: 它会在
    冲突**仍然存在**时把项关掉，于是"报告干净"变成假话。

    只做对账（不锚 job）: 语义上够，但每一次对账都要重算整个视图，而撤销是
    按 job 逐个走的 —— N 个作业的批量撤销就是 N 次全表聚合。锚 job 之后，
    `undo` 那条既有边（`created_by_job_id=J AND state='open'`）先把最典型的那
    一批便宜地关掉，对账只处理剩下的。两者是"便宜的近似 + 精确的收口"。

    为什么在这里关的项仍然带 `closed_by_job_id=job_id`
    --------------------------------------------------
    这是给 `redo` 留的边: `redo` 重开的是 `closed_by_job_id=J` 的项。一条因为
    J 的撤销而消失的冲突，正是"J 的 redo 会让它回来"的那一条 —— 由 redo 自己
    重开比依赖对账再开更省一次全表聚合。**并且这条边不会说谎**: 库里的状态确实
    是"J 撤销之前有、之后没有"。对账在 `redo` 末尾仍然会跑一次，所以这条边即便
    因为别的原因失效（例如 redo 之后另一个作业又把值改回去了），对账也会把最终
    状态校正回来 —— 两半各自都不足以单独保证正确，合起来才同时满足上面两条。

    幂等: 反复调用不改变结果集（已关的项不再是 open，不会被再关一次；已 open
    的项指纹相同，`_open_inbox` 退化为累加 `occurrence_count`）。
    """
    live = _live_conflicts(conn, project_id)
    live_fingerprints = {c["fingerprint"] for c in live}

    closed = 0
    for row in conn.execute(
        "SELECT id, fingerprint FROM inbox_items"
        " WHERE project_id=? AND kind='value_conflict' AND state='open'"
        "   AND auto_generated=1", (project_id,)
    ):
        if str(row["fingerprint"]) in live_fingerprints:
            continue
        conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?,"
            " close_reason='reconcile', closed_by_job_id=?"
            " WHERE id=? AND state='open'",
            (now(), job_id, int(row["id"])),
        )
        closed += 1

    opened = 0
    for item in live:
        # 复用一条**同指纹的已关项**（undo → redo → undo → … 会反复开关同一条
        # 冲突）: 不这么做的话每一轮都插一行新的，`inbox_items` 会随撤销次数
        # 单调增长，而"清空即干净"读的是 open 项 —— 残骸不会红，只会变大。
        revived = conn.execute(
            "SELECT id FROM inbox_items"
            " WHERE project_id=? AND kind='value_conflict' AND subject_kind=?"
            "   AND subject_id=? AND fingerprint=? AND state<>'open'"
            " ORDER BY id DESC LIMIT 1",
            (project_id, item["subject_kind"], item["subject_id"],
             item["fingerprint"]),
        ).fetchone()
        if revived is not None:
            conn.execute(
                "UPDATE inbox_items SET state='open', resolved_at=NULL,"
                " resolved_by=NULL, close_reason=NULL, closed_by_job_id=NULL,"
                " opened_by_undo_job_id=NULL, occurrence_count=occurrence_count+1,"
                " last_seen_at=? WHERE id=?", (now(), int(revived["id"])),
            )
            opened += 1
            continue
        _, created = _open_one_conflict(conn, project_id=project_id, live=item,
                                        job_id=job_id)
        opened += 1 if created else 0
    return {"opened": opened, "closed": closed}


def refresh_unknown_fields(conn: sqlite3.Connection, project_id: int, *,
                           fields: dict[str, int] | None = None,
                           file_name: str | None = None,
                           fingerprint: str | None = None,
                           job_id: int | None = None) -> int:
    """把"解析器不认识的字段名"登记为 inbox 项（§5.6 的格式漂移信号）。

    为什么这一条需要人看一眼（而不是静静地记在报告里）
    ------------------------------------------------
    `ctx.on_unknown_field` 从 2.4 起就在数这些名字，`Counters.unknown_fields` 也
    一直落在导入报告的 `counts` 里 —— 但**报告是导入那一刻的文件**，事后无迹可查
    （`_import_health` 的注释把这条写死了：没有留下报告的作业就统计不到）。于是
    "上游某天加了一个字段"这件事，只有在导入那一次被看见，第二天就没了。

    它要人做的裁决很具体：那个字段是什么、要不要为它补一条归一化 / 一条观测。
    所以它是一条**待办**，不是一条日志 —— 待办的表就是 Inbox。

    几个刻意的选择：

      * **按字段名各开一条**（不是每个文件一条）：同一个字段在两个文件里出现，
        要做的裁决是同一件 —— 合并成一条 `occurrence_count` 累加，
        正是 `_open_inbox` 的 fingerprint 机制在做的事；
      * `sources` 放**解析器指纹**而不是文件名：指纹在一次导入里是常量，所以
        同一份文件的两次导入、同一解析器的两个文件，都折进同一条；
      * `severity='low'`：格式漂移是"要你去看一眼"的信号，不是数据损坏 ——
        `cmd_doctor` 早就把这条分工写死了（漂移不进 `healthy`）。把它报成 high
        会让真正的高危项淹在里面；
      * 主体用 `entity:0`（项目级占位）：解析器报的是**字段名**，那一刻还没有
        任何实体可指 —— 与 `_no_parser_report` 同一个占位约定；
      * `fields` 为空时**一行不写**：干净的夹具因此完全不受影响（生产者必须是
        惰性的，否则每个既有导入都会凭空多出一条待办）。

    返回新开的条数（复用已存在的 open 项不算）。
    """
    counts = fields or {}
    opened = 0
    for name in sorted(counts):
        count = int(counts[name])
        _, created = _open_inbox(
            conn, project_id=project_id, kind="evidence_missing",
            subject_kind="entity", subject_id=0,
            values=[str(name)], sources=[str(fingerprint or "")],
            job_id=job_id, severity="low",
            payload={"reason": "unknown_field", "field": str(name), "count": count,
                     "file": file_name, "parser": fingerprint})
        opened += 1 if created else 0
    return opened


def refresh_scope_drift(conn: sqlite3.Connection, project_id: int, *,
                        entity_ids: list[int] | None = None,
                        job_id: int | None = None) -> int:
    """把"落在项目 scope 之外"的资产登记为 inbox 项（`scope_drift`）。

    **判据不在这里** —— 哪条资产越界由 `query.out_of_scope_entities` 说了算，
    它底下是 `scope.py` 的唯一一份匹配语义（子域通配 / 网段包含 / 大小写折叠）。
    读写两侧调同一个函数，所以"体检页说 3、Inbox 说 2"这种分歧不可能出现。

    `entity_ids` 给了就只判这几条 —— 导入路径传的是**这个作业刚写进去的实体**：
    一个作业为自己写的行负责，而不是替整个库重新判一遍（那会让每次导入付一次
    全库扫描，而 100 万观测的库上这一步比导入本身还贵）。

    没配 scope 的项目（include / exclude 都空）**一行不写**：`scope.in_scope`
    对这种项目回的是 `None`（不问），不是 `False`（越界）。把"没配"读成"全越界"
    会让每个项目在第一次导入时刷出一屏假待办 —— 那是把体检页变成噪音。

    返回新开的条数。重复导入同一份文件不会新增（fingerprint 含实体键）。
    """
    scope_obj = query.project_scope(conn, project_id)
    opened = 0
    for entity in query.out_of_scope_entities(conn, project_id,
                                              entity_ids=entity_ids):
        _, created = _open_inbox(
            conn, project_id=project_id, kind="scope_drift",
            subject_kind="entity", subject_id=int(entity["id"]),
            values=[str(entity["entity_key"])], sources=[],
            job_id=job_id, severity="medium",
            payload={"reason": "out_of_scope", "entity_key": str(entity["entity_key"]),
                     "kind": str(entity["kind"]),
                     "scope": scope_module.describe(scope_obj)})
        opened += 1 if created else 0
    return opened


def refresh_unregistered_assets(conn: sqlite3.Connection, project_id: int, *,
                                job_id: int) -> int:
    """把"会话里出现的、此前从未登记过的主机"登记为 inbox 项（4.1）。

    **这是「发现未登记资产」那条待办的唯一生产者**，而它用的是一条既有的通道：
    `_open_inbox` + `inbox_items`。计划书 4.1 点名了这条纪律 —— 不新造第二套
    待办（第二套的表现是"体检页说干净、而还有一屏事没人看"）。

    **判据不在这里** —— 哪些主机算"新发现"由 `query.unregistered_assets` 说了
    算（四条判据写在那个函数的 docstring 里）。读写两侧调同一个函数，所以
    "待办里有、查询里没有"这种分歧不可能出现。

    `subject` 是**主机实体本身**（不是 flow）：要人裁决的是"这台主机纳不纳入
    我的资产"，而不是"这段会话是不是真的"。

    `severity='low'`：它是"发现"，不是"问题"。一份真实抓包会带出十几台新的
    对端主机（CDN / 分析域名 / 时间同步……），把它们报成 high 会让真正的高危项
    淹在里面 —— 与 `refresh_unknown_fields` 同一条口径。

    `values` 放**实体键**、`sources` 放 `flow@…` 的指纹那一路不需要：指纹的
    输入里已经有 `subject_id`，而同一台主机在两次导入里被"第一次发现"只可能
    发生一次（第二次它已经有贡献了，判据 3 直接把它挡掉）。

    返回新开的条数。**干净的夹具上一行不写** —— 没有 flow 就没有候选
    （`query.unregistered_assets` 里那句早退是这条惰性的实现，它同时是导入路径
    的常态：除 4.1 的两个来源之外没有任何来源产出 `involves`）。
    """
    opened = 0
    for entity in query.unregistered_assets(conn, project_id=project_id, job_id=job_id):
        _, created = _open_inbox(
            conn, project_id=project_id, kind="unregistered_asset",
            subject_kind="entity", subject_id=int(entity["id"]),
            values=[str(entity["entity_key"])], sources=[],
            job_id=job_id, severity="low",
            payload={"reason": "unregistered_asset",
                     "entity_key": str(entity["entity_key"]),
                     # 地址的形状（ip / domain）而不是 `entities.kind`：
                     # 后者可能是**先写它的那一类记录**留下的 kind（一份 HAR 里
                     # `api.example.com` 的 kind 是 `apischema`），拿它当"这是一台
                     # 什么资产"的答案会误导人（见 `query.unregistered_assets`）。
                     "host_kind": str(entity["host_kind"]),
                     "entity_kind": str(entity["kind"]),
                     "job_id": int(job_id)})
        opened += 1 if created else 0
    return opened


# ---------------------------------------------------------------------------
# rekey（T3）—— 审阅点 11：身份迁移事务，零外键迁移
# ---------------------------------------------------------------------------

def _identity_job(conn: sqlite3.Connection, project_id: int, kind: str,
                  job_id: int | None, actor: str) -> int:
    """身份类操作必须有一个 job 作为生命周期边的锚点（v3.2-r4）。

    `revoked_by_job_id` / `unmerged_by_job_id` 都是外键，且 undo/redo 靠它们定位
    要撤销的东西。若调用方不传 job_id，这里自动开一个（与 compact 的处理一致）。
    """
    if job_id is not None:
        return int(job_id)
    return start_job(conn, project_id=project_id, kind=kind, coverage="unknown",
                     actor=actor)


def rekey(conn: sqlite3.Connection, *, project_id: int,
          mapping: dict[str, str], job_id: int | None = None,
          actor: str = "cli", dry_run: bool = False,
          backup_dir: str | Path | None = None) -> dict[str, Any]:
    """把 entity_key 升级为更规范的 key（如 /report/2024 -> /report/{id}）。

    关键: `entities.entity_key` 永不被 UPDATE；所有引用都指向 entities.id（稳定），
    因此 rekey **不需要迁移任何外键**，不可能出现"改了一半、引用断链"的窗口。

    v3.2-r4（债 #4）: 补上 §4.5 第 1 步的 **SQLite Online Backup 快照**。
    快照在事务**之前**做（事务内备份会撞锁），dry-run 也做 —— 因为 dry-run 与
    真实执行走同一代码路径，快照那一步的失败（磁盘满 / 目录不可写）必须在预演时
    就暴露，而不是等到真跑。

    前置条件: 同 `undo` / `redo` —— `conn` 必须是 `store.connect()` 建出来的
    连接（入口守卫，审核 LARI-21 的遗留项①）。
    """
    store.require_row_factory(conn, caller="ops.rekey")
    snapshot = store.online_backup(conn, dest_dir=backup_dir, tag="rekey")
    with transaction(conn, dry_run=dry_run):
        job_id = _identity_job(conn, project_id, "rekey", job_id, actor)
        pairs: list[dict[str, Any]] = []
        merges: list[dict[str, Any]] = []
        for old_key, new_key in sorted(mapping.items()):
            entity_id = entity_for_key(conn, project_id, old_key)
            kind_row = conn.execute("SELECT kind FROM entities WHERE project_id=? AND id=?",
                                     (project_id, entity_id)).fetchone()
            if kind_row is not None and kind_row["kind"] == "endpoint":
                try:
                    decoded = normalize.decode_endpoint_key(new_key)
                except normalize.NormalizeError as exc:
                    raise InvariantError(f"endpoint rekey target is malformed: {exc}") from exc
                if decoded is None:
                    raise InvariantError("endpoint rekey target must be a canonical Endpoint v2 key")
            existing = conn.execute(
                "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL",
                (project_id, new_key),
            ).fetchone()
            if existing is not None and int(existing["entity_id"]) != entity_id:
                other_kind = conn.execute("SELECT kind FROM entities WHERE project_id=? AND id=?",
                                          (project_id, int(existing["entity_id"]))).fetchone()
                if kind_row is not None and other_kind is not None and kind_row["kind"] != other_kind["kind"]:
                    raise InvariantError("rekey collision cannot merge different entity kinds")
                merges.append({"survivor_key": new_key,
                               "survivor_id": int(existing["entity_id"]),
                               "member_key": old_key, "member_id": entity_id})
                continue
            if existing is None:
                conn.execute(
                    "INSERT INTO entity_alias(project_id, alias_key, entity_id, reason,"
                    " created_at, created_by_job_id) VALUES (?,?,?,'rekey',?,?)",
                    (project_id, new_key, entity_id, now(), job_id),
                )
            pairs.append({"old": old_key, "new": new_key, "entity_id": entity_id})

        for m in merges:
            _merge_inside(conn, project_id=project_id, survivor_id=m["survivor_id"],
                          member_ids=[m["member_id"]], job_id=job_id,
                          reason="rekey_collision", actor=actor)

        recompute(conn)
        assert_integrity(conn)
        report = {"op": "rekey", "dry_run": dry_run, "pairs": pairs, "merges": merges,
                  "identity_rewritten": len(pairs), "backup": str(snapshot)}
        log_activity(conn, actor=actor, action="rekey", target_kind="project",
                     target_id=project_id, job_id=job_id, detail=report)
        return report


# ---------------------------------------------------------------------------
# merge / unmerge（T4 / T5）—— 审阅点 12：identity overlay，不移动任何事实
# ---------------------------------------------------------------------------

def _merge_inside(conn: sqlite3.Connection, *, project_id: int, survivor_id: int,
                  member_ids: Sequence[int], job_id: int | None, reason: str,
                  actor: str) -> int:
    all_ids = sorted({survivor_id, *member_ids})
    cur = conn.execute(
        "INSERT INTO entity_merge_events(project_id, survivor_id, state, reason,"
        " created_at, created_by_job_id) VALUES (?,?,'merged',?,?,?)",
        (project_id, survivor_id, reason, now(), job_id),
    )
    event_id = int(cur.lastrowid)
    for eid in all_ids:
        conn.execute(
            "INSERT INTO entity_merge_members(merge_event_id, entity_id, project_id)"
            " VALUES (?,?,?)", (event_id, eid, project_id),
        )
    log_activity(conn, actor=actor, action="merge", target_kind="entity",
                 target_id=survivor_id, job_id=job_id,
                 detail={"members": all_ids, "reason": reason})
    return event_id


def merge(conn: sqlite3.Connection, *, project_id: int, survivor_key: str,
          member_keys: Sequence[str], job_id: int | None = None,
          actor: str = "cli", dry_run: bool = False) -> dict[str, Any]:
    """merge 是 identity overlay:
      事实层零变更（INV-10），只写 merge event + alias，然后 recompute。

    前置条件: 同 `undo` / `redo` —— `conn` 必须是 `store.connect()` 建出来的
    连接（入口守卫，审核 LARI-21 的遗留项①）。
    """
    store.require_row_factory(conn, caller="ops.merge")
    with transaction(conn, dry_run=dry_run):
        job_id = _identity_job(conn, project_id, "merge", job_id, actor)
        survivor_id = entity_for_key(conn, project_id, survivor_key)
        # key → entity 一次性配对（v3.2-r4 修正）。
        # 旧实现在 member_ids 前面**插入** survivor，再用 `zip(member_keys, member_ids)`
        # 取配对 —— 两个列表长度不同、错位一格，member_key 会指到**另一个** member 的
        # entity 上。（今天不可达: 下面的 `existing is None` 对数上来讲永远为假，
        # 见下方注释。）配对必须由同一次解析产生，不能靠两个列表的下标对齐。
        member_pairs = [(k, entity_for_key(conn, project_id, k)) for k in member_keys]
        member_ids = sorted({survivor_id, *(eid for _, eid in member_pairs)})
        kinds = {str(row[0]) for row in conn.execute(
            f"SELECT DISTINCT kind FROM entities WHERE project_id=? AND id IN ({','.join('?' for _ in member_ids)})",
            (project_id, *member_ids))}
        if len(kinds) != 1:
            raise InvariantError("merge members must have the same entity kind")

        before_facts = _fact_layer_digest(conn)
        event_id = _merge_inside(conn, project_id=project_id, survivor_id=survivor_id,
                                member_ids=member_ids, job_id=job_id,
                                reason="manual_merge", actor=actor)
        # 这里对 `existing` 的判断实际上不会命中: 上面 `entity_for_key(k)` 用的正是
        # 同一条"生效 alias"查询，能解析出 entity 就说明 alias 已存在。保留它是为了让
        # "alias 已经指向别处" 这种情况显式地不覆盖（而不是留一个静默的写入）。
        for k, eid in member_pairs:
            if eid == survivor_id:
                continue
            existing = conn.execute(
                "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
                " AND revoked_by_job_id IS NULL",
                (project_id, k)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO entity_alias(project_id, alias_key, entity_id, reason,"
                    " created_at, created_by_job_id)"
                    " VALUES (?,?,?,'merge_member',?,?)",
                    (project_id, k, eid, now(), job_id))

        recompute(conn)
        conflicts = _merge_conflicts(conn, project_id, all_ids=sorted(set(member_ids)),
                                     job_id=job_id, event_id=event_id)
        after_facts = _fact_layer_digest(conn)
        if before_facts != after_facts:   # pragma: no cover - INV-10 兜底
            raise InvariantError("INV-10 violated: merge touched the fact layer")
        assert_integrity(conn)
        report = {"op": "merge", "dry_run": dry_run, "event_id": event_id,
                  "survivor": f"entity:{survivor_id}",
                  "members": [f"entity:{i}" for i in sorted(set(member_ids))],
                  "conflicts": conflicts,
                  "facts": {"before": before_facts, "after": after_facts}}
        log_activity(conn, actor=actor, action="merge", target_kind="entity",
                     target_id=survivor_id, job_id=job_id, detail=report)
        return report


def unmerge(conn: sqlite3.Connection, *, event_id: int, actor: str = "cli",
            dry_run: bool = False) -> dict[str, Any]:
    """unmerge 天然精确可逆 —— 因为事实从未移动过。

    v3.2-r4 说明: unmerge 是 **merge 的显式逆操作**，本身**不**进入 undo/redo 的
    生命周期边模型（它的逆操作就是再 merge 一次）。因此它不开 job、不写
    `unmerged_by_job_id` —— 所有 `*_by_job_id` 生命周期边都严格只由 **undo** 产生，
    于是一定满足"边存在 ⇒ 该 job 的 applied=0"。

    修复: 旧实现用 `payload_json LIKE '%"event_id":N%'` 关闭 merge_conflict 项，
    但当时 `_merge_conflicts` 的 payload 里**没有** event_id（死代码）。
    现在 payload 真的带 event_id，这个关闭动作才真正生效。

    前置条件: 同 `merge` —— `conn` 必须是 `store.connect()` 建出来的连接
    （入口守卫）。LARI-21 只点名了 `merge`，但两者是同一对逆操作、同一种调用
    形状，守一半会让"有守卫 ⇒ 这条路上都安全"变成一句不成立的话。
    """
    store.require_row_factory(conn, caller="ops.unmerge")
    with transaction(conn, dry_run=dry_run):
        ev = conn.execute("SELECT * FROM entity_merge_events WHERE id=?", (event_id,)).fetchone()
        if ev is None:
            raise InvariantError(f"merge event {event_id} not found")
        members = [int(r["entity_id"]) for r in conn.execute(
            "SELECT entity_id FROM entity_merge_members WHERE merge_event_id=?", (event_id,))]
        before_facts = _fact_layer_digest(conn)
        conn.execute("UPDATE entity_merge_events SET state='unmerged', unmerged_at=?"
                     " WHERE id=?", (now(), event_id))
        inbox_closed = conn.execute(
            "UPDATE inbox_items SET state='closed', resolved_at=?, close_reason='unmerge'"
            " WHERE kind='merge_conflict' AND state='open' AND payload_json LIKE ?",
            (now(), f'%"event_id":{event_id}%'),
        ).rowcount
        recompute(conn)
        after_facts = _fact_layer_digest(conn)
        if before_facts != after_facts:   # pragma: no cover
            raise InvariantError("INV-10 violated: unmerge touched the fact layer")
        assert_integrity(conn)
        report = {"op": "unmerge", "dry_run": dry_run, "event_id": event_id,
                  "inbox_closed": inbox_closed,
                  "members": [f"entity:{m}" for m in members]}
        log_activity(conn, actor=actor, action="unmerge", target_kind="entity",
                     target_id=int(ev["survivor_id"]), detail=report)
        return report


def _merge_conflicts(conn: sqlite3.Connection, project_id: int,
                     all_ids: Sequence[int], job_id: int | None,
                     event_id: int | None = None) -> list[dict[str, Any]]:
    """合并后同字段多值 → 冲突 → inbox。**禁止 last-write-wins 自动选值。**

    payload 必须带 `event_id`（v3.2-r4）：否则 `unmerge` 无法定位并关闭这些项
    （旧代码就是靠 LIKE 匹配一个从不存在的字段 —— 死代码）。
    """
    out: list[dict[str, Any]] = []
    marks = ",".join("?" for _ in all_ids)
    rows = conn.execute(
        f"SELECT semantic_field, GROUP_CONCAT(DISTINCT value) AS vals,"
        f" COUNT(DISTINCT value) AS n"
        f"  FROM observations WHERE entity_id IN ({marks}) AND is_current=1"
        f" GROUP BY semantic_field HAVING COUNT(DISTINCT value) > 1",
        list(all_ids),
    ).fetchall()
    for row in rows:
        values = sorted(set(str(row["vals"]).split(",")))
        sources = [r["source_id"] for r in conn.execute(
            f"SELECT DISTINCT c.source_id FROM v_active_contributions c"
            f" JOIN observations o ON o.id = c.subject_id"
            f" WHERE c.subject_kind='observation' AND o.entity_id IN ({marks})"
            f"   AND o.semantic_field=?", [*all_ids, str(row["semantic_field"])])]
        item_id, _ = _open_inbox(
            conn, project_id=project_id, kind="merge_conflict",
            subject_kind="entity", subject_id=int(all_ids[0]),
            values=values, sources=sources, job_id=job_id,
            payload={"field": str(row["semantic_field"]), "values": values,
                     "event_id": event_id,
                     "members": [f"entity:{i}" for i in all_ids]},
            severity="high",
        )
        out.append({"field": str(row["semantic_field"]), "values": values,
                    "inbox_item": item_id})
    return out


def _fact_layer_digest(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("raw_files", "evidence", "contributions",
                      "contribution_supersessions")}


# ---------------------------------------------------------------------------
# compact（T6）—— 审阅点 9：job_count 语义不再跳变
# ---------------------------------------------------------------------------

def compact(conn: sqlite3.Connection, *, project_id: int, job_id: int | None = None,
            older_than: str = "1970-01-01T00:00:00Z", actor: str = "cli",
            dry_run: bool = False) -> dict[str, Any]:
    """压实非活跃历史（新行 append + 旧行 suppress，仍不 UPDATE/DELETE）。

    守恒（INV-11）:
        Σ logical_job_count 不变
        Σ source_count      不变
        is_current 不变
        confidence 只可不变或降低

    v3.2-r2 时间语义（区间）:
        压实行的 `observed_at` = **MAX(成员)**（区间上界/规范时刻**不许回退**），
        `observed_at_min`       = MIN(成员.observed_at_min)。
        旧实现取 MIN(observed_at) 会把时间线往回拉，等于让压实改坏了历史。

    v3.2-r2 编组键补 `contribution_kind`（不同语义的贡献永不合并）。

    前置条件: 同 `undo` / `redo` —— `conn` 必须是 `store.connect()` 建出来的
    连接（入口守卫，审核 LARI-21 的遗留项①）。
    """
    store.require_row_factory(conn, caller="ops.compact")
    with transaction(conn, dry_run=dry_run):
        groups = conn.execute(
            """
            SELECT c.project_id, c.subject_kind, c.subject_id, c.owner_entity_id,
                   c.contribution_kind, c.field_namespace,
                   c.semantic_field, c.field_value, c.value_kind, c.relation_kind,
                   c.source_id, c.authorship, COUNT(*) AS n,
                   MAX(c.observed_at)     AS max_at,
                   MIN(c.observed_at_min) AS min_at,
                   SUM(c.job_count + c.compacted_job_count) AS logical_jobs,
                   MIN(c.evidence_id) AS any_evidence,
                   GROUP_CONCAT(c.id) AS ids
              FROM contributions c
             WHERE c.project_id = ?
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id = c.id)
               AND c.authorship <> 'manual'
               AND c.observed_at < ?
             GROUP BY c.project_id, c.subject_kind, c.subject_id, c.owner_entity_id,
                      c.contribution_kind, c.field_namespace,
                      c.semantic_field, c.field_value, c.value_kind, c.relation_kind,
                      c.source_id, c.authorship
            HAVING COUNT(*) > 1
            """,
            (project_id, older_than),
        ).fetchall()

        before = _conservation(conn, project_id)
        conf_before = _observation_confidence_map(conn, project_id)
        effective_job = job_id if job_id is not None else _compact_job(
            conn, project_id, actor)

        # v3.2-r3: 每个 (subject, source, field) 的**最后一条**活贡献不参与压实。
        # 业务语义: "最近一次观测"必须单独存活，不能被合成行代表 ——
        # 否则 earliest/latest 的区分、以及"最近是否还看到过"的判断都会失去依据。
        protected = _protected_last_active(conn, project_id, older_than)

        groups_done: list[dict[str, Any]] = []
        for g in groups:
            ids = [int(x) for x in str(g["ids"]).split(",") if int(x) not in protected]
            if len(ids) < 2:
                continue
            marks = ",".join("?" for _ in ids)
            # 只统计**真正被压实**的成员（保护行不参与，否则会重复计数）
            agg = conn.execute(
                f"SELECT MAX(observed_at) AS max_at, MIN(observed_at_min) AS min_at,"
                f" SUM(job_count + compacted_job_count) AS logical_jobs"
                f"  FROM contributions WHERE id IN ({marks})", ids).fetchone()
            # 保守证据选择: 取 quality 最高的那条
            best = conn.execute(
                f"SELECT c.evidence_id FROM contributions c JOIN evidence e ON e.id=c.evidence_id"
                f" WHERE c.id IN ({marks})"
                f" ORDER BY CASE e.quality_class WHEN 'high' THEN 3"
                f" WHEN 'medium' THEN 2 ELSE 1 END DESC, c.id ASC LIMIT 1",
                ids,
            ).fetchone()
            evidence_id = int(best["evidence_id"])
            representative = ids[0]
            rep = conn.execute("SELECT * FROM contributions WHERE id=?",
                               (representative,)).fetchone()

            members = [
                {"contribution_id": int(r["id"]), "job_id": int(r["job_id"]),
                 "source_id": int(r["source_id"]), "observed_at": str(r["observed_at"]),
                 "observed_at_min": str(r["observed_at_min"]),
                 "evidence_id": int(r["evidence_id"])}
                for r in conn.execute(
                    f"SELECT * FROM contributions WHERE id IN ({marks})", ids)
            ]
            # 1) 抑制被合并的旧贡献（append 抑制事实，不 DELETE）
            for cid in ids:
                conn.execute(
                    "INSERT INTO contribution_supersessions(project_id, contribution_id,"
                    " superseded_by_job_id, reason, created_at, actor)"
                    " VALUES (?,?,?, 'compact', ?, ?)",
                    (project_id, cid, effective_job, now(), actor),
                )
            # 2) 追加一条 compaction 代表贡献（区间语义: 上界不变，下界细化）
            #    v3.2-r4（债 #3）: 这里原来是裸 SQL 直写 contributions，是全仓唯一
            #    绕过 add_contribution 的写点。合成行也是贡献，必须走同一入口 ——
            #    否则 INV-16 的结构校验对它形同虚设，schema 加了新列它也不跟。
            #    入口本身零行为差异: job_count/compacted_job_count/synthetic/
            #    represents_json 就是为压实产物加的参数。
            add_contribution(conn, NewContribution(
                project_id=project_id,
                job_id=effective_job,
                source_id=int(rep["source_id"]),
                evidence_id=evidence_id,
                subject_kind=str(rep["subject_kind"]),
                subject_id=int(rep["subject_id"]),
                contribution_kind=str(rep["contribution_kind"]),
                owner_entity_id=(None if rep["owner_entity_id"] is None
                                 else int(rep["owner_entity_id"])),
                authorship=str(rep["authorship"]),
                field_namespace=rep["field_namespace"],
                semantic_field=rep["semantic_field"],
                field_value=rep["field_value"],
                value_kind=rep["value_kind"],
                relation_kind=rep["relation_kind"],
                observed_at=str(agg["max_at"]),
                observed_at_min=str(agg["min_at"]),
                observed_at_source=str(rep["observed_at_source"]),
                observed_at_accuracy="approximate",
                observed_at_precision="unknown",
                job_count=1,
                compacted_job_count=int(agg["logical_jobs"]) - 1,
                synthetic=1,
                represents_json=canonical_json({"members": members}),
            ))
            groups_done.append({
                "subject": f"{g['subject_kind']}:{int(g['subject_id'])}",
                "contribution_kind": str(g["contribution_kind"]),
                "field": g["semantic_field"], "members": len(ids),
                "observed_at": str(agg["max_at"]), "observed_at_min": str(agg["min_at"]),
                "compacted_job_count": int(agg["logical_jobs"]) - 1,
            })

        subjects = sorted({(str(g["subject_kind"]), int(g["subject_id"])) for g in groups})
        recompute(conn, delta=subjects)
        after = _conservation(conn, project_id)
        conf_after = _observation_confidence_map(conn, project_id)
        _assert_conservation(before, after, conf_before=conf_before, conf_after=conf_after)
        assert_integrity(conn)
        report = {"op": "compact", "dry_run": dry_run, "groups": len(groups_done),
                  "protected_last_active": sorted(protected),
                  "detail": groups_done,
                  "conservation": {"before": before, "after": after}}
        log_activity(conn, actor=actor, action="compact", target_kind="project",
                     target_id=project_id, job_id=job_id, detail=report)
        return report


def _protected_last_active(conn: sqlite3.Connection, project_id: int,
                           older_than: str) -> set[int]:
    """每个 (subject, source, field) 的**最后一条**可压实贡献 → 不参与压实。

    排序口径: (observed_at, id) 最大者。这正是"最近一次观测"的业务语义。
    """
    rows = conn.execute(
        """
        SELECT c.id, c.observed_at, c.subject_kind, c.subject_id, c.owner_entity_id,
               c.field_namespace, c.semantic_field, c.source_id, c.contribution_kind
          FROM contributions c
         WHERE c.project_id = ?
           AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                            WHERE s.contribution_id = c.id)
           AND c.authorship <> 'manual'
           AND c.observed_at < ?
         ORDER BY c.subject_kind, c.subject_id, c.owner_entity_id, c.field_namespace,
                  c.semantic_field, c.source_id, c.contribution_kind,
                  c.observed_at DESC, c.id DESC
        """,
        (project_id, older_than),
    ).fetchall()
    seen: set[tuple] = set()
    protected: set[int] = set()
    for r in rows:
        group = (r["subject_kind"], int(r["subject_id"]), r["owner_entity_id"],
                 r["field_namespace"], r["semantic_field"], int(r["source_id"]),
                 r["contribution_kind"])
        if group in seen:
            continue
        seen.add(group)
        protected.add(int(r["id"]))
    return protected


def _compact_job(conn: sqlite3.Connection, project_id: int, actor: str) -> int:
    return start_job(conn, project_id=project_id, kind="compact", coverage="unknown",
                     actor=actor)


def _conservation(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """INV-11 的守恒量（v3.2-r2: 真正按 §5.2 的 Σ 口径，不是 project 去重）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(c.job_count + c.compacted_job_count), 0) AS logical_jobs,
               (SELECT COUNT(DISTINCT source_id) FROM v_active_contributions
                 WHERE project_id = ?)                                AS sources,
               (SELECT COALESCE(SUM(source_count), 0) FROM observations
                 WHERE project_id = ?)                                AS sum_source_count,
               (SELECT COALESCE(SUM(logical_job_count), 0) FROM observations
                 WHERE project_id = ?)                                AS sum_logical_job_count,
               (SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1) AS obs_current
          FROM v_active_contributions c WHERE c.project_id = ?
        """,
        (project_id, project_id, project_id, project_id, project_id),
    ).fetchone()
    return {"logical_jobs": int(row["logical_jobs"]), "sources": int(row["sources"]),
            "sum_source_count": int(row["sum_source_count"]),
            "sum_logical_job_count": int(row["sum_logical_job_count"]),
            "obs_current": int(row["obs_current"])}


CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "conflict": 4,
                    "manual": 5}


def _observation_confidence_map(conn: sqlite3.Connection,
                                project_id: int) -> dict[int, str]:
    return {int(r["id"]): str(r["confidence"])
            for r in conn.execute("SELECT id, confidence FROM observations"
                                  " WHERE project_id=?", (project_id,))}


def _assert_conservation(before: dict[str, int], after: dict[str, int], *,
                         conf_before: dict[int, str] | None = None,
                         conf_after: dict[int, str] | None = None) -> None:
    if before["logical_jobs"] != after["logical_jobs"]:
        raise InvariantError(
            f"INV-11 violated: logical_job_count not conserved "
            f"{before['logical_jobs']} -> {after['logical_jobs']}")
    if before["sources"] != after["sources"]:
        raise InvariantError("INV-11 violated: source_count not conserved")
    if before["sum_source_count"] != after["sum_source_count"]:
        raise InvariantError(
            f"INV-11 violated: Σ source_count not conserved "
            f"{before['sum_source_count']} -> {after['sum_source_count']}")
    if before["sum_logical_job_count"] != after["sum_logical_job_count"]:
        raise InvariantError(
            f"INV-11 violated: Σ logical_job_count not conserved "
            f"{before['sum_logical_job_count']} -> {after['sum_logical_job_count']}")
    if before["obs_current"] != after["obs_current"]:
        raise InvariantError("INV-11 violated: is_current changed by compact")
    if conf_before and conf_after:
        for oid, before_conf in conf_before.items():
            after_conf = conf_after.get(oid)
            if after_conf is None:
                continue
            if (CONFIDENCE_ORDER.get(after_conf, 99)
                    > CONFIDENCE_ORDER.get(before_conf, 99)):
                raise InvariantError(
                    f"INV-11 violated: compact raised confidence of observation "
                    f"{oid}: {before_conf} -> {after_conf}")


# ---------------------------------------------------------------------------
# evidence 降级（审阅点 10 + v3.2-r3 闭环）
#
#   三级 = **这一行还留下什么**（§5.3）:
#     full      片段内容 + locator + hash
#     locator   无片段内容；locator + hash
#     hash_only 只剩 hash（locator 已丢弃；DB 层 CHECK 强制 locator IS NULL）
#
#   能力 = 两级因素交叉:
#     原文是否仍可取得   ⟸ raw_files.storage_state ∈ {present, archived}
#     片段是否仍可定位   ⟸ evidence.locator IS NOT NULL（即 recovery_state ∈ {full, locator}）
# ---------------------------------------------------------------------------

RECOVERY_LEVELS = ("full", "locator", "hash_only")
STORAGE_STATES = ("present", "archived", "purged", "missing")

#: **原文由谁持有**（4.1 起，迁移 011 加的列）。它与 `storage_state` **正交**：
#: 后者说"它还在不在"，这一列说"它从来就不在我手里"。v3.1 的
#: `storage='external'` 想说的正是这件事，而 v3.2 把它塞进 `storage_state` 的
#: 单调降级链里是塞不进去的 —— 它不在那条链的任何一级上（迁移 011 的文件头
#: 第一节把这件事写透了）。
CUSTODY_KINDS = ("stored", "external")

RECOVERY_CAPABILITY = {
    ("present", "full"): "recoverable",
    ("present", "locator"): "recoverable",
    ("present", "hash_only"): "verifiable_only",
    ("archived", "full"): "recoverable",
    ("archived", "locator"): "recoverable",
    ("archived", "hash_only"): "verifiable_only",
    ("purged", "full"): "verifiable_only",
    ("purged", "locator"): "verifiable_only",
    ("purged", "hash_only"): "verifiable_only",
    ("missing", "full"): "hash_only",
    ("missing", "locator"): "hash_only",
    ("missing", "hash_only"): "hash_only",
}


def evidence_capability(raw_storage_state: str, recovery_state: str,
                        custody: str = "stored") -> str:
    """能力判定（4.1 起是**三个**因素的函数：谁持有 × 还在不在 × 还留下什么）。

        custody='stored'（本工具持有原文）—— 3×4 = 12 种组合，判据不变：
            present/archived + full/locator → recoverable
            present/archived + hash_only    → verifiable_only
                （原文还在，但 evidence 侧已丢弃 locator → 定位不到片段）
            purged                          → verifiable_only
            missing                         → hash_only
        custody='external'（原文在**工具之外**，我们只有一个路径指着它）:
            full/locator → locator_only
            hash_only    → hash_only

    `external` 为什么**不能**沿用 `stored` 的结论（这是 4.1 点名的一条）
    -----------------------------------------------------------------
    "可恢复"在 `stored` 下的含义是"本工具手里有副本，凭 locator + hash 取回
    片段"。外部文件没有这件事：副本从来不在本工具手里，能不能取到取决于**那个
    路径今天还在不在** —— 而那是本工具之外的世界的状态。沿用 `recoverable`
    等于把一句"我保证"写在一条给不了保证的行上；4.1 的原话是"可恢复等级要
    **如实**标注，不许沿用 `stored` 的结论"。

    所以外部文件的能力是**独立的一档** `locator_only`：靠 locator 去取，取不
    取得到不由我们说了算。而它的 `hash_only` 与 `stored` 那一档**同义**——
    locator 都丢了，两条路都只剩 hash。

    `recovery_state` 对外部文件是**唯一**的定位依据（`original_available` 见
    `evidence_capability_report`）：外部文件的"原文可取得"本身就是由 locator
    定义的 —— 没有 locator，"它还在"这句话我们连说都说不出来。所以
    `storage_state` 在这条分支上只在 `missing`（有人显式声明它不见了）时才
    参与判定，且只会把结论**往保守的方向**推。
    """
    if custody == "external":
        if recovery_state == "hash_only" or raw_storage_state == "missing":
            return "hash_only"
        return "locator_only"
    return RECOVERY_CAPABILITY.get((raw_storage_state, recovery_state), "hash_only")


def evidence_capability_report(raw_storage_state: str, recovery_state: str,
                               custody: str = "stored") -> dict[str, Any]:
    """能力 + 三个决定因素（GUI 文案直接可用，避免再次误述）。

    §5.3 的纪律没变：**GUI 必须用这里的字段，不得自行措辞**。4.1 之后多了一个
    必须跟着走的字段 `custody` —— 一张只写"原文持有"的界面对外部文件会一直
    说错话（那个文件不在工具的 data 目录里，工具也删不掉它）。
    """
    capability = evidence_capability(raw_storage_state, recovery_state, custody)
    external = custody == "external"
    wording = {
        "recoverable": "可校验完整性，并可凭 locator + hash 恢复原文片段",
        "verifiable_only": "仅可校验完整性（hash 比对已有副本），不可恢复原文片段",
        "hash_only": "仅保留 hash：无原文、无归档，不可恢复也不可重定位",
        "locator_only": "可凭 locator 指向的**外部**路径读取原文并比对 hash；"
                        "副本不在本工具手里 —— 那个路径还在不在，由工具之外的世界决定",
    }[capability]
    # 两个 `original_available` 不是同一个问题，所以它们必须给出不同的答案：
    #   stored   → 原文在不在（= storage_state 那一级）
    #   external → 我们**还知不知道它在哪**（= locator 还在不在）——
    #              本工具从来就没有副本，把 storage_state 的 present 读成
    #              "原文可取得"是在替别人的文件担保
    original_available = (recovery_state in ("full", "locator") if external
                          else raw_storage_state in ("present", "archived"))
    return {
        "capability": capability,
        "custody": custody,
        "original_available": original_available,
        "locatable": recovery_state in ("full", "locator"),
        "fragment_recoverable": capability == "recoverable",
        "verifiable": capability in ("recoverable", "verifiable_only", "locator_only"),
        "wording": wording,
        # 外部文件删不掉 —— 这句话只在有外部文件要谈的时候出现，免得它变成
        # 一条每张证据上都有的套话（套话不会被读）。
        "external_not_deletable": external,
    }


def _downgrade_evidence_in_txn(conn: sqlite3.Connection, *, raw_file_id: int,
                               to_state: str, actor: str, job_id: int | None,
                               dry_run: bool) -> dict[str, Any]:
    """事务内实现（调用方负责事务；本函数不 BEGIN，避免嵌套事务崩溃）。"""
    if to_state not in RECOVERY_LEVELS:
        raise InvariantError(f"bad recovery_state {to_state!r}")
    order = {"full": 2, "locator": 1, "hash_only": 0}
    rows = conn.execute(
        "SELECT id, recovery_state, locator FROM evidence WHERE raw_file_id=?",
        (raw_file_id,),
    ).fetchall()
    changed: list[dict[str, Any]] = []
    for r in rows:
        current = str(r["recovery_state"])
        if order[to_state] > order[current]:
            raise InvariantError(
                f"INV-17: evidence {int(r['id'])} recovery_state 不允许升级 "
                f"({current} -> {to_state})")
        if current == to_state:
            continue
        dropped_locator = r["locator"] if to_state == "hash_only" else None
        conn.execute(
            "UPDATE evidence SET recovery_state=?, locator=? WHERE id=?",
            (to_state, None if to_state == "hash_only" else r["locator"], int(r["id"])),
        )
        changed.append({"evidence_id": int(r["id"]), "from": current, "to": to_state,
                        "dropped_locator": dropped_locator})
    rf = conn.execute("SELECT storage_state, custody FROM raw_files WHERE id=?",
                      (raw_file_id,)).fetchone()
    storage_state = str(rf["storage_state"]) if rf else "missing"
    custody = str(rf["custody"]) if rf else "stored"
    report = {"op": "evidence.degrade", "dry_run": dry_run, "raw_file_id": raw_file_id,
              "recovery_state": to_state,
              **evidence_capability_report(storage_state, to_state, custody),
              "changed": changed}
    if changed:
        log_activity(conn, actor=actor, action="evidence.degrade", target_kind="raw_file",
                     target_id=raw_file_id, job_id=job_id, detail=report)
    return report


def degrade_evidence(conn: sqlite3.Connection, *, raw_file_id: int,
                     to_state: str, actor: str = "compact",
                     job_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """把某个 raw_file 下的 evidence 降级（只降不升）。

    前置条件: 同 `undo` / `redo` —— `conn` 必须是 `store.connect()` 建出来的
    连接（入口守卫，审核 LARI-21 的遗留项①）。
    """
    store.require_row_factory(conn, caller="ops.degrade_evidence")
    with transaction(conn, dry_run=dry_run):
        return _downgrade_evidence_in_txn(conn, raw_file_id=raw_file_id, to_state=to_state,
                                          actor=actor, job_id=job_id, dry_run=dry_run)


def purge_raw_file(conn: sqlite3.Connection, *, raw_file_id: int, actor: str = "gc",
                   job_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """原文清除（事实层 storage_state 降级 → evidence 随之降为 hash_only）。

    v3.2-r3 修正: 不再嵌套 `transaction()`（旧实现必然抛
    "cannot start a transaction within a transaction"），且 dry_run 正确下传。

    ⚠️ 4.1：`custody='external'` 的行**拒绝**清除（`InvariantError`）
    ------------------------------------------------------------------
    一条外部 `raw_file` 指的是**工具之外**的一个文件：本工具只有一条路径指着它。
    对这样的行说"已 purged"是一句假话 —— 文件还在原处，而这句话会让人以为
    "retention 到期之后那份原文已经不可恢复了"（§5.3 要求 GUI 给出的告警是
    "将永久失去原文"）。**"清除"这个词对外部文件没有对应的动作**，所以这里
    拒绝，而不是把"放弃指针"伪装成"清除"。

    拒绝的代价与它换来的东西：`gc --raw-older-than N` 之类的调用方**必须**先
    按 `custody` 过滤（这一步没有任何生产调用方，它今天只从测试与 CLI 的将来
    入口进来；拒绝会比静默地做错事早一步被看见）。真的要"停止跟踪这个外部
    文件"要用的是 `degrade_evidence(..., to_state='hash_only')` —— 那一步**有**
    对应动作（丢掉 locator），而它的报告会如实写出能力降到了哪一档。
    """
    row = conn.execute("SELECT custody, locator_uri FROM raw_files WHERE id=?",
                       (raw_file_id,)).fetchone()
    if row is not None and str(row["custody"]) == "external":
        raise InvariantError(
            f"raw_file {raw_file_id} 的原文在工具之外（{row['locator_uri']}）——"
            f"本工具删不掉它，所以「purge」在这里没有对应动作。"
            f"要停止跟踪它请用 `ops.degrade_evidence(raw_file_id={raw_file_id},"
            f" to_state='hash_only')`：那一步丢掉的是**本工具的指针**，"
            f"而它会如实报告能力降到了哪一档。")
    with transaction(conn, dry_run=dry_run):
        conn.execute("UPDATE raw_files SET storage_state='purged', purged_at=? WHERE id=?",
                     (now(), raw_file_id))
        report = _downgrade_evidence_in_txn(conn, raw_file_id=raw_file_id,
                                            to_state="hash_only", actor=actor,
                                            job_id=job_id, dry_run=dry_run)
        report["op"] = "raw_file.purge"
        report["note"] = ("raw_file 已 purged: evidence 仅具备完整性校验能力，"
                          "不再具备原文恢复能力")
        return report


# ---------------------------------------------------------------------------
# coverage 判定（审阅点 7）
# ---------------------------------------------------------------------------

def candidate_inactive(conn: sqlite3.Connection, *, observation_id: int,
                       job_id: int) -> tuple[bool, str]:
    """coverage 判定（v3.2-r2 修正）。

    判定顺序（先给出最可读的原因）:
      1. coverage='full'              否则 unknown
      2. 该 job 对被观察对象没有活贡献  否则 unknown（本 job 不能否定自己）
      3. scope_version / scope_hash 与项目当前一致
      4. **parser fingerprint 与项目当前一致**（`projects.parser_fingerprint`）
         —— parser 决定"覆盖"的含义；项目未登记当前 parser → unknown
      5. 观察对象与其归属 entity 都不是 manual_owned

    任何一项不满足 → unknown（**不**判 inactive）。
    旧实现的 bug: 把 job.parser_fingerprint 与 projects.scope_hash 比较（永远 drift）。
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    obs = conn.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
    if job is None or obs is None:
        return False, "missing"
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (int(obs["project_id"]),)).fetchone()
    if str(job["coverage"]) != "full":
        return False, f"coverage={job['coverage']}"
    active_from_job = conn.execute(
        "SELECT COUNT(*) FROM v_active_contributions"
        " WHERE subject_kind='observation' AND subject_id=? AND job_id=?",
        (observation_id, job_id),
    ).fetchone()[0]
    if int(active_from_job) != 0:
        return False, "job_has_active_contribution"
    if int(job["project_id"]) != int(obs["project_id"]):
        return False, "job_project_mismatch"
    if int(job["scope_version"]) != int(proj["scope_version"]):
        return False, "scope_version_drift"
    if str(job["scope_hash"]) != str(proj["scope_hash"]):
        return False, "scope_hash_drift"
    if job["parser_fingerprint"] is None:
        return False, "parser_unknown"
    if proj["parser_fingerprint"] is None:
        return False, "project_parser_unset"
    if str(job["parser_fingerprint"]) != str(proj["parser_fingerprint"]):
        return False, "parser_drift"
    # 人工对象永不被 coverage 否定（§5.1: ¬manual_owned(o) ∧ ¬manual_owned(o.entity)）
    from .store import manual_owned as _manual_owned
    if _manual_owned(conn, "observation", observation_id):
        return False, "manual_owned"
    if _manual_owned(conn, "entity", int(obs["entity_id"])):
        return False, "manual_owned_entity"
    return True, "candidate_inactive"


def explain_job_scope(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    """旧 job 必须用**当时的** scope 解释，不能用当前 scope。"""
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {job_id} not found")
    hist = conn.execute(
        "SELECT * FROM project_scope_history WHERE project_id=? AND version=?",
        (int(job["project_id"]), int(job["scope_version"])),
    ).fetchone()
    proj = conn.execute("SELECT * FROM projects WHERE id=?",
                        (int(job["project_id"]),)).fetchone()
    drift = int(proj["scope_version"]) - int(job["scope_version"])
    parser_drift = (job["parser_fingerprint"] is not None
                    and str(job["parser_fingerprint"]) != str(proj["parser_fingerprint"]))
    return {
        "job_id": job_id,
        "job_scope_version": int(job["scope_version"]),
        "job_scope_hash": str(job["scope_hash"]),
        "job_parser_fingerprint": job["parser_fingerprint"],
        "current_scope_version": int(proj["scope_version"]),
        "current_parser_fingerprint": proj["parser_fingerprint"],
        "scope_versions_behind": drift,
        "parser_drift": parser_drift,
        "job_scope": None if hist is None else hist["scope"],
        "interpret_with": "historical_scope" if drift > 0 else "current_scope",
        "coverage_usable_for_inactive": drift == 0 and not parser_drift,
    }
