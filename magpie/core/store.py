"""v3.2 core — schema + recompute + invariant checks.

分层铁律（禁止跨层写）:
    A 事实层  raw_files / evidence / contributions / contribution_supersessions  -> append-only
    B 缓存层  entities / observations / relations / entity_identity               -> recompute 覆盖
    C 身份层  entity_alias / entity_merge_events / entity_merge_members           -> overlay
    D 工作层  entity_layout / entity_tags / manual_fields / inbox_items            -> GC 锚点
    E 审计层  jobs / activity_log / tag_events                                     -> 永不回滚

唯一公理: 数据行是缓存，贡献流是事实。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

# schema/ 在仓库根，不在包内（随 2.1 包布局迁移加深了一层）。
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "001_init.sql"

FIELD_NAMESPACES = ("entity_field", "observation_field")
SUBJECT_KINDS = ("entity", "observation", "relation")
SOURCE_AUTHORITY_LEVELS = ("verified", "semiautomatic", "manual", "unverified")

# 可注入时钟（测试确定性）。None = 真实 UTC 时钟。
_CLOCK: list[Callable[[], str] | None] = [None]

# 事实层: 永不被 GC 触碰，只增不减
FACT_TABLES = (
    "raw_files",
    "evidence",
    "contributions",
    "contribution_supersessions",
)
# 审计层: 永不回滚
AUDIT_TABLES = ("jobs", "activity_log", "tag_events")
# 缓存层（B 层）: 由事实投影而来；墓碑行（贡献全被抑制）不再是"有效状态"
CACHE_TABLES = (
    "entities",
    "entity_keys",
    "observations",
    "relations",
    "entity_identity",
    "entity_alias",
    "entity_merge_events",
    "entity_merge_members",
)
# 工作层（D 层）: 人工状态，不可由事实重建
WORK_TABLES = ("entity_layout", "entity_tags", "manual_fields", "inbox_items",
               "research_tasks", "research_task_links", "entity_research")
# 历史口径（INV-13(c)）: 只允许单调增长
HISTORY_TABLES = FACT_TABLES

# 快照比较时需要归一化的列（时间戳/自增空洞）
VOLATILE_COLUMNS = frozenset(
    {"created_at", "updated_at", "set_at", "last_seen_at", "last_seen", "first_seen",
     "changed_at", "purged_at", "deleted_at"}
)


class InvariantError(RuntimeError):
    """INV-01..INV-15 任一被违反。调用方必须 ROLLBACK。"""


class ManualConflict(RuntimeError):
    """INV-12: 乐观版本号冲突，调用方必须重读重试。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """key 排序 / 数组排序不在此处做（由调用方保证），仅做稳定序列化。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scope_hash(scope: dict[str, Any]) -> str:
    """scope 的 canonical hash（审阅点 7 + v3.2-r2）。

    规则: key 排序、数组排序、无多余空白、UTF-8、无尾换行。
    因此同一 scope 的两种书写顺序必须得到同一 hash。

    v3.2-r2: parser fingerprint 进入 hash。它决定"覆盖"的含义，
    所以 scope 快照与 parser 必须绑在同一个版本号上（§5.1）。
    """
    normalized = {
        "include": sorted(scope.get("include", [])),
        "exclude": sorted(scope.get("exclude", [])),
        "parser_profile": scope.get("parser_profile"),
        "parser_fingerprint": scope.get("parser_fingerprint"),
        "depth": scope.get("depth"),
    }
    return sha256_hex(canonical_json(normalized))


def fingerprint(kind: str, subject_kind: str, subject_id: int,
                semantic_field: str | None,
                values: Iterable[str], sources: Iterable[str | int]) -> str:
    """inbox 唯一性指纹（审阅点 16）。禁止 NULL。"""
    return sha256_hex(canonical_json({
        "kind": kind,
        "subject": f"{subject_kind}:{subject_id}",
        "conflict_field": semantic_field,
        "values": sorted({str(v) for v in values}),
        "sources": sorted({str(s) for s in sources}),
    }))


# ---------------------------------------------------------------------------
# 连接 / 事务
# ---------------------------------------------------------------------------

def connect(db_path: str | Path = ":memory:", *,
            check_same_thread: bool = True) -> sqlite3.Connection:
    """连接。

    `check_same_thread=False` 只应由 ManualWriter 这类"单写队列"使用
    （所有写入被串行化，连接因此不会被并发使用）。
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def require_row_factory(conn: sqlite3.Connection, *, caller: str) -> None:
    """断言 `conn` 是 `store.connect()` 建出来的连接 —— 否则**立刻**报可读错误。

    为什么需要这条守卫（审核 LARI-21 · `6406b99` 的已知遗留）:
        本模块与 `ops` 的多数函数按**列名**取值（`row["contribution_kind"]`、
        `row["entity_id"]`……），这要求 `conn.row_factory = sqlite3.Row`。
        而这条前提全仓**只在 `connect()` 里设置过一次**，别处没有任何守卫。
        于是"自己 `sqlite3.connect()` 的调用方"（2.7 的 FastAPI 层就是现实的
        例子）走到 `ops.undo` / `ops.redo` 时，拿到的是

            TypeError: tuple indices must be integers or slices, not str

        —— 一个指向函数内部、且完全不提"你少了个前提"的错误。坏状态要大声报出来，
        而不是坏在别处（与 `check.py` 的解释器守卫同一纪律）。

    为什么只认 `sqlite3.Row`（而不是"factory 不为 None 就行"）:
        本模块依赖的是"按列名取值"这一条**具体**行为；别的 factory 是否等价
        不能靠猜。`connect()` 钉的就是 `sqlite3.Row`，这里就认它。
    """
    if conn.row_factory is sqlite3.Row:
        return
    actual = "None（裸 sqlite3.connect）" if conn.row_factory is None \
        else repr(conn.row_factory)
    raise InvariantError(
        f"{caller} 要求 store.connect() 建出来的连接（conn.row_factory = sqlite3.Row），"
        f"当前连接的 row_factory 是 {actual}。\n"
        f"  改法: conn = store.connect(db_path)（或先 conn.row_factory = sqlite3.Row）。\n"
        f"  原因: 数据层按列名取值，裸连接的行是 tuple，会以 TypeError 坏在函数内部。")


def init_db(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """建库 / 升级到当前 schema 版本。

    v3.2-r4（债 #7）: 不再直接 `executescript(001_init.sql)`，改走 `migrate`，
    由 `schema_migrations` 记录已应用版本。`schema_path` 参数保留只为兼容
    自定义 schema 的测试调用；默认路径始终走迁移 runner。
    """
    if schema_path != SCHEMA_PATH:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys = ON")
        return
    from . import migrate as _migrate                       # 延迟导入，避免环
    _migrate.migrate(conn)


def db_file(conn: sqlite3.Connection) -> Path | None:
    """当前连接的 main 数据库文件；`:memory:` 或临时库返回 None。"""
    for row in conn.execute("PRAGMA database_list"):
        if str(row[1]) == "main":
            name = str(row[2] or "")
            return Path(name) if name else None
    return None


def online_backup(conn: sqlite3.Connection, *, dest_dir: str | Path | None = None,
                  tag: str = "snapshot") -> Path:
    """SQLite Online Backup API 快照（§4.5 第 1 步）。

    为什么不能用复制文件: WAL 模式下 `.db` 不是完整状态（`-wal` 里还有未 checkpoint
    的数据），直接复制会得到一个**看起来正常但缺数据**的备份。

    目标目录解析顺序: `dest_dir` → `$MAGPIE_SNAPSHOT_DIR` → 主库同目录 →
    系统临时目录（`:memory:` 库没有主库文件；测试与临时环境走这条）。
    """
    if dest_dir is None:
        env = os.environ.get("MAGPIE_SNAPSHOT_DIR")
        if env:
            dest_dir = Path(env)
        else:
            main = db_file(conn)
            dest_dir = (main.parent if main is not None
                        else Path(tempfile.gettempdir()) / "magpie-snapshots")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = "magpie"
    main = db_file(conn)
    if main is not None:
        stem = main.stem or stem
    stamp = now().replace(":", "").replace("-", "")
    dest = dest_dir / f"{stem}.{tag}.{stamp}.db"
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


@contextmanager
def transaction(conn: sqlite3.Connection, dry_run: bool = False) -> Iterator[sqlite3.Connection]:
    """统一事务模板 T1（P0-D）。

    dry_run 走同一代码路径，仅把 COMMIT 换成 ROLLBACK —— 否则 dry-run 会说谎。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


_SAVEPOINT_SEQ = [0]


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """保存点级原子性（可安全嵌套在 transaction() 内，也可独立使用）。

    为什么需要: 高阶写入口（add_observation / add_relation / upsert_entity）会先建
    缓存行再写贡献。若贡献被约束拒绝（跨项目 / INV-16 / INV-03），调用方若没有
    外层事务，就会留下"没有任何贡献解释的缓存行" —— 正是 INV-13(b) 禁止的幻影行。
    """
    _SAVEPOINT_SEQ[0] += 1
    name = f"dsh_sp_{_SAVEPOINT_SEQ[0]}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def now() -> str:
    """当前时间（ISO8601, UTC, 秒精度）。

    v3.2-r3: 不再是常量。系统重度依赖时间语义（observed_at / retention /
    compact / coverage / parser / temporal_term / activity_log / GC / evidence），
    硬编码常量会让所有"最近 24h""older_than 180d"在生产里失真。

    测试通过 `set_clock(...)` 注入确定性时钟；`reset_clock()` 恢复真实时钟。
    """
    if _CLOCK[0] is not None:
        return str(_CLOCK[0]())
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_clock(fn: Callable[[], str]) -> None:
    """注入时钟（测试专用）。fn 必须返回 `YYYY-MM-DDTHH:MM:SSZ`。"""
    _CLOCK[0] = fn


def reset_clock() -> None:
    _CLOCK[0] = None


def parse_time(text: str | None) -> datetime | None:
    """解析系统内的时间戳（宽容 NaN/None）。"""
    if not text:
        return None
    raw = str(text).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(text: str | None, *, reference: str | None = None) -> float | None:
    """`text` 距参考时刻（默认 now()）的小时数；无法解析时返回 None。"""
    then, ref = parse_time(text), parse_time(reference or now())
    if then is None or ref is None:
        return None
    return (ref - then).total_seconds() / 3600.0


def is_within(text: str | None, *, hours: float, reference: str | None = None) -> bool:
    delta = hours_since(text, reference=reference)
    return delta is not None and -3600.0 <= delta <= hours


def days_ago(days: float, *, reference: str | None = None) -> str:
    """参考时刻往前推 N 天的 ISO8601 字符串（retention 表达式用）。"""
    ref = parse_time(reference or now()) or datetime.now(timezone.utc)
    return (ref - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

ACTION_NAMESPACE = frozenset({
    "job.start", "job.finish",
    "contribution.add",
    "supersession.add", "supersession.lift",
    "recompute.full", "recompute.delta",
    "undo", "reparse", "rekey", "merge", "unmerge", "compact", "redo",
    "gc.purge", "gc.retained", "gc.raw_purge",
    "inbox.open", "inbox.close", "inbox.merge",
    "tag.add", "tag.remove",
    "manual.set", "manual.clear",
    "parser.register", "parser.set", "parser.rollback",
    "source.register", "evidence.degrade",
    "schema.migrate",
    # 管线编排（3.7b）。**定义不是事实**（3.7a 冻结 F4）：保存 / 归档一个定义
    # 只写审计与乐观锁，不建 job、不产生贡献。`pipeline.run` 记的是"跑了一次"，
    # 它挂着的 N 个 job 各自仍写自己的 `job.start` / `job.finish`。
    "pipeline.save", "pipeline.archive", "pipeline.run",
    "pipeline.undo_run", "pipeline.redo_run",
    # 工作层的可撤销写入（3.8）。`lifecycle.set` 是"批量改状态"这一次操作；
    # `work.revert` / `work.replay` 是 `ops.undo` / `ops.redo` 把 `work_journal`
    # 放回去时留下的两行 —— **与 `undo` / `redo` 分开**，因为一次 undo 里
    # "撤掉了几条贡献"与"把几个实体的状态放回去了"是两件事，混在一个 action 里
    # 就没法只查其中一件。
    "lifecycle.set", "work.revert", "work.replay",
    "finding.triage", "finding.retest",
    "research.task.create", "research.task.update", "research.metadata.set",
})


def log_activity(conn: sqlite3.Connection, *, actor: str, action: str,
                 target_kind: str | None = None, target_id: int | None = None,
                 job_id: int | None = None, detail: Any = None) -> None:
    if action not in ACTION_NAMESPACE:
        raise InvariantError(f"unknown action outside frozen namespace: {action}")
    conn.execute(
        "INSERT INTO activity_log(at, actor, action, target_kind, target_id, job_id, detail)"
        " VALUES (?,?,?,?,?,?,?)",
        (now(), actor, action, target_kind, target_id, job_id,
         None if detail is None else canonical_json(detail)),
    )


# ---------------------------------------------------------------------------
# 实体身份解析（C 层）
# ---------------------------------------------------------------------------

def new_entity(conn: sqlite3.Connection, *, project_id: int, kind: str,
               entity_key: str) -> int:
    cur = conn.execute(
        "INSERT INTO entities(project_id, kind, lifecycle, is_current, merged_into,"
        " first_seen, last_seen) VALUES (?,?,'active',0,NULL,?,?)",
        (project_id, kind, now(), now()),
    )
    entity_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO entity_keys(project_id, entity_id, entity_key, canonical, created_at)"
        " VALUES (?,?,?,0,?)",
        (project_id, entity_id, entity_key, now()),
    )
    conn.execute(
        "INSERT INTO entity_alias(project_id, alias_key, entity_id, reason, created_at)"
        " VALUES (?,?,?,'import',?)",
        (project_id, entity_key, entity_id, now()),
    )
    return entity_id


def resolve_entity(conn: sqlite3.Connection, project_id: int, entity_key: str) -> int | None:
    """key → entity（只认**生效** alias；被 undo 撤销的 alias 不参与解析，v3.2-r4）。"""
    row = conn.execute(
        "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
        " AND revoked_by_job_id IS NULL",
        (project_id, entity_key),
    ).fetchone()
    return None if row is None else int(row["entity_id"])


def entity_for_key(conn: sqlite3.Connection, project_id: int, entity_key: str) -> int:
    entity_id = resolve_entity(conn, project_id, entity_key)
    if entity_id is None:
        raise InvariantError(f"unknown entity_key {entity_key!r} in project {project_id}")
    return entity_id


# ---------------------------------------------------------------------------
# 事实层写入口（唯一入口；INV-01）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewContribution:
    project_id: int
    job_id: int
    source_id: int
    evidence_id: int
    subject_kind: str
    subject_id: int
    contribution_kind: str                       # entity | field | relation | layout
    # INV-16: observation 的归属 entity / relation 的源 entity（事实，非缓存）
    owner_entity_id: int | None = None
    authorship: str = "tool"
    field_namespace: str | None = None
    semantic_field: str | None = None
    field_value: str | None = None
    value_kind: str | None = None
    relation_kind: str | None = None
    # None = 用插入时刻（真实时钟）作为 observed_at
    observed_at: str | None = None
    # 事实时间区间的下界；None = 与 observed_at 相同的单点事实
    observed_at_min: str | None = None
    observed_at_source: str = "tool"
    observed_at_accuracy: str = "unknown"
    observed_at_precision: str = "unknown"
    # v3.2-r4（债 #3）: 压实产物专用字段。只有 compact 会传非默认值 ——
    # 它过去用裸 SQL 直写 contributions 绕过本入口，是唯一违反"写入口唯一性"的地方。
    # 把这三个字段收进本入口后，INV-01 的"唯一入口"才是真的（有 lint 测试守着）。
    job_count: int = 1
    compacted_job_count: int = 0
    synthetic: int = 0
    represents_json: str | None = None


def add_contribution(conn: sqlite3.Connection, c: NewContribution) -> int:
    """唯一贡献写入口。

    前置断言只是为了让错误可读；真正的防线是 schema 里的复合 FK + 触发器。
    """
    if c.synthetic not in (0, 1):
        raise InvariantError(f"synthetic must be 0 or 1, got {c.synthetic!r}")
    if c.job_count < 1:
        raise InvariantError(f"job_count >= 1 required, got {c.job_count!r}")
    if c.compacted_job_count < 0:
        raise InvariantError(
            f"compacted_job_count >= 0 required, got {c.compacted_job_count!r}")
    if c.synthetic == 0 and (c.job_count != 1 or c.compacted_job_count != 0
                             or c.represents_json is not None):
        raise InvariantError(
            "只有 synthetic 贡献可以携带 job_count/compacted_job_count/represents_json")
    if c.subject_kind not in SUBJECT_KINDS:
        raise InvariantError(f"bad subject_kind {c.subject_kind!r}")
    job = conn.execute("SELECT project_id FROM jobs WHERE id=?", (c.job_id,)).fetchone()
    if job is None:
        raise InvariantError(f"job {c.job_id} not found")
    if int(job["project_id"]) != c.project_id:
        raise InvariantError("INV-05: job.project_id != contribution.project_id")
    ev = conn.execute(
        "SELECT project_id FROM evidence WHERE id=?", (c.evidence_id,)
    ).fetchone()
    if ev is None:
        raise InvariantError(f"evidence {c.evidence_id} not found")
    if int(ev["project_id"]) != c.project_id:
        raise InvariantError("INV-07: evidence.project_id != contribution.project_id")
    table = {"entity": "entities", "observation": "observations",
             "relation": "relations"}[c.subject_kind]
    row = conn.execute(
        f"SELECT project_id FROM {table} WHERE id=?", (c.subject_id,)
    ).fetchone()
    if row is None:
        raise InvariantError(f"INV-03: subject {c.subject_kind}:{c.subject_id} not found")
    if int(row["project_id"]) != c.project_id:
        raise InvariantError("INV-06: subject.project_id != contribution.project_id")

    # INV-16: 归属事实必须存在且形状正确（observation 归属 / relation 源）
    needs_owner = (c.subject_kind == "observation" or c.contribution_kind == "relation")
    if needs_owner and c.owner_entity_id is None:
        raise InvariantError(
            f"INV-16: {c.subject_kind}/{c.contribution_kind} 贡献必须携带 owner_entity_id")
    if not needs_owner and c.owner_entity_id is not None:
        raise InvariantError(
            f"INV-16: {c.subject_kind}/{c.contribution_kind} 贡献不得携带 owner_entity_id")
    if c.owner_entity_id is not None:
        owner = conn.execute(
            "SELECT project_id FROM entities WHERE id=?", (c.owner_entity_id,)
        ).fetchone()
        if owner is None or int(owner["project_id"]) != c.project_id:
            raise InvariantError("INV-16: owner entity missing or cross-project")
    # INV-16: 归属与结构必须与缓存行一致（触发器兜底；这里给可读错误）
    if c.subject_kind == "observation":
        obs = conn.execute(
            "SELECT entity_id, field_namespace, semantic_field, value FROM observations"
            " WHERE id=?", (c.subject_id,)
        ).fetchone()
        if obs is not None:
            actual = (int(obs["entity_id"]), obs["field_namespace"], obs["semantic_field"],
                      obs["value"])
            declared = (int(c.owner_entity_id or -1), c.field_namespace,
                        c.semantic_field, c.field_value)
            if actual != declared:
                raise InvariantError(
                    f"INV-16: observation {c.subject_id} 结构 {actual} != 贡献声明的 {declared}")
    elif c.contribution_kind == "relation":
        rel = conn.execute(
            "SELECT src_id, dst_id, rel FROM relations WHERE id=?", (c.subject_id,)
        ).fetchone()
        if rel is not None:
            dst = int(json.loads(str(c.field_value))["dst_id"])
            actual = (int(rel["src_id"]), int(rel["dst_id"]), str(rel["rel"]))
            declared = (int(c.owner_entity_id or -1), dst, str(c.relation_kind))
            if actual != declared:
                raise InvariantError(
                    f"INV-16: relation {c.subject_id} 结构 {actual} != 贡献声明的 {declared}")

    observed_at = c.observed_at if c.observed_at is not None else now()
    observed_at_min = observed_at if c.observed_at_min is None else c.observed_at_min
    if observed_at_min > observed_at:
        raise InvariantError(
            f"INV-16: observed_at_min {observed_at_min} > observed_at {observed_at}")

    cur = conn.execute(
        "INSERT INTO contributions("
        " project_id, subject_kind, subject_id, owner_entity_id, source_id, authorship,"
        " contribution_kind, field_namespace, semantic_field, field_value, value_kind,"
        " relation_kind, observed_at, observed_at_min, observed_at_source,"
        " observed_at_accuracy, observed_at_precision, evidence_id, job_id,"
        " job_count, compacted_job_count, synthetic, represents_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c.project_id, c.subject_kind, c.subject_id, c.owner_entity_id, c.source_id,
         c.authorship, c.contribution_kind, c.field_namespace, c.semantic_field,
         c.field_value, c.value_kind, c.relation_kind, observed_at, observed_at_min,
         c.observed_at_source, c.observed_at_accuracy, c.observed_at_precision,
         c.evidence_id, c.job_id, c.job_count, c.compacted_job_count, c.synthetic,
         c.represents_json, now()),
    )
    cid = int(cur.lastrowid)
    log_activity(conn, actor="store", action="contribution.add", target_kind="contribution",
                 target_id=cid, job_id=c.job_id,
                 detail={"subject": f"{c.subject_kind}:{c.subject_id}",
                         "owner": (None if c.owner_entity_id is None
                                   else f"entity:{c.owner_entity_id}")})
    return cid


def supersede(conn: sqlite3.Connection, contribution_id: int, *, by_job_id: int,
              reason: str, actor: str = "cli") -> int:
    """把"某贡献不再活跃"登记为一条事实（审阅点 2/3 方案 B）。"""
    row = conn.execute(
        "SELECT project_id FROM contributions WHERE id=?", (contribution_id,)
    ).fetchone()
    if row is None:
        raise InvariantError(f"INV-15: contribution {contribution_id} not found")
    project_id = int(row["project_id"])
    job = conn.execute("SELECT project_id FROM jobs WHERE id=?", (by_job_id,)).fetchone()
    if job is None or int(job["project_id"]) != project_id:
        raise InvariantError("INV-09: supersession job cross-project")
    cur = conn.execute(
        "INSERT INTO contribution_supersessions("
        " project_id, contribution_id, superseded_by_job_id, reason, created_at, actor)"
        " VALUES (?,?,?,?,?,?)",
        (project_id, contribution_id, by_job_id, reason, now(), actor),
    )
    sid = int(cur.lastrowid)
    log_activity(conn, actor=actor, action="supersession.add",
                 target_kind="contribution", target_id=contribution_id, job_id=by_job_id,
                 detail={"reason": reason})
    return sid


def lift_supersession(conn: sqlite3.Connection, supersession_id: int, *,
                      actor: str = "cli", job_id: int | None = None) -> None:
    """唯一允许 DELETE supersession 的路径（undo/reparse/rekey/unmerge/compact）。"""
    conn.execute("DELETE FROM contribution_supersessions WHERE id=?", (supersession_id,))
    log_activity(conn, actor=actor, action="supersession.lift",
                 target_kind="supersession", target_id=supersession_id, job_id=job_id)


def active_contributions(conn: sqlite3.Connection, subject_kind: str, subject_id: int):
    return conn.execute(
        "SELECT * FROM v_active_contributions WHERE subject_kind=? AND subject_id=?",
        (subject_kind, subject_id),
    ).fetchall()


# ---------------------------------------------------------------------------
# 物化（把事实投影成缓存行；只 INSERT 缺失行，永不 UPDATE 结构列）
# ---------------------------------------------------------------------------

def materialize(conn: sqlite3.Connection) -> None:
    """从 contributions 补建缺失的缓存行（结构列 = 投影，一次性冻结）。

    v3.2-r2 修正（P0）: 主体归属必须来自**事实列 `owner_entity_id`**。
    旧实现把 `c.subject_id` 当成 entity_id（observation_field 贡献的 subject_id 是
    observation 的 id），后果是 FK 崩塌、或在 id 恰好撞上别的 entity 时
    **把他人的字段静默投影到错误的 entity 上**。

    重建规则（幂等）:
      observation 行 ← subject_id 对应的事实里最早的一条（owner / namespace / field / value）
      relation   行 ← subject_id 对应的事实里最早的一条（owner=src / dst_id / rel）
      entity 行由 new_entity 建立（key 只在创建时出现，故不由 materialize 生成）

    性能（2026-09-17 修正）: 「subject_id 对应事实里最早的一条」原本写成**相关子查询**
    `c.id = (SELECT MIN(c2.id) … WHERE c2.subject_id = c.subject_id)`。
    `contributions` 上没有 `(contribution_kind, subject_id)` 索引，于是该子查询退化成
    每行一次**全表扫描** —— 整体 O(N²)。观测那半条恰好能借 `ix_contrib_subject`
    （subject_kind, subject_id）走索引，relation 这半条不能：实测 20 万贡献时
    `materialize` 单独耗时 **102s**（同一时刻写路径只要 21s），10 万观测的导入
    直接击穿 §9.2 的 60s 契约。
    改成先按 subject_id 一次性 GROUP BY 求 MIN、再等值连接 —— 同样的 MIN、同样
    的行集，但只有一次扫描 + 一次分组，不再是 N 次扫描。
    """
    # 1) observation 行: 以事实里的 observation id 为身份（不再用 (entity, field, value) 猜）
    conn.execute(
        """
        INSERT INTO observations(id, project_id, entity_id, field_namespace, semantic_field,
                                 value, value_kind, is_current, confidence,
                                 source_count, job_count, logical_job_count, origin,
                                 observed_at, observed_at_min, observed_at_source,
                                 observed_at_accuracy, observed_at_precision, created_at)
        SELECT c.subject_id, c.project_id, c.owner_entity_id, c.field_namespace,
               c.semantic_field,
               c.field_value, COALESCE(c.value_kind,'string'), 0, 'none',
               0, 0, 0, c.authorship,
               c.observed_at, c.observed_at_min, c.observed_at_source,
               c.observed_at_accuracy, c.observed_at_precision, c.created_at
          FROM contributions c
          JOIN (SELECT subject_id, MIN(id) AS first_id
                  FROM contributions
                 WHERE subject_kind = 'observation'
                 GROUP BY subject_id) m
            ON m.first_id = c.id
         WHERE c.subject_kind = 'observation'
           AND c.contribution_kind = 'field'
           AND c.owner_entity_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.id = c.subject_id)
        """
    )
    # 2) relation 行: 身份 = 事实里的 relation id；结构列 = (owner=src, dst_id, rel)
    conn.execute(
        """
        INSERT INTO relations(id, project_id, src_id, dst_id, rel, is_current, confidence,
                              source_count, job_count, created_at)
        SELECT c.subject_id, c.project_id, c.owner_entity_id,
               CAST(json_extract(c.field_value, '$.dst_id') AS INTEGER),
               c.relation_kind, 0, 'none', 0, 0, c.created_at
          FROM contributions c
          JOIN (SELECT subject_id, MIN(id) AS first_id
                  FROM contributions
                 WHERE contribution_kind = 'relation'
                 GROUP BY subject_id) m
            ON m.first_id = c.id
         WHERE c.contribution_kind = 'relation'
           AND c.owner_entity_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM relations r WHERE r.id = c.subject_id)
        """
    )
    # 3) entity_field 行: 直接在 contributions 上成立，无独立缓存表（entity 行由 new_entity 建立）


# ---------------------------------------------------------------------------
# recompute（缓存重算；full 与 delta 必须逐列等价 —— INV-04）
# ---------------------------------------------------------------------------

def _recompute_subjects(conn: sqlite3.Connection, subjects: Sequence[tuple[str, int]],
                        *, owners: "set[int] | None" = None) -> None:
    """重算给定 subject 集合的缓存列（**批量实现**，见 recompute_subset）。

    历史: v3.2-r2 的实现是逐 subject 调用一组 `_recompute_observation` /
    `_recompute_relation` / `_recompute_entity`（v3.2-r4 已删除，见 `recompute_subset`），
    每个 observation 触发约 10 条 SQL，其中 `confidence.score_field` 每次都对
    `v_field_owner` / `v_field_conflicts` 全表聚合视图求值 —— 整体 O(N²)。
    实测 N=8000 全量重算 39.8s，直接击穿 §9.2 的 "100 万行 ≤ 60s"（外推约 78 分钟）。
    现改为一次扫描 + 内存索引 + 按块批量 UPDATE。
    """
    recompute_subset(conn, subjects, owners=owners)


def _owners_of(conn: sqlite3.Connection,
               subjects: Sequence[tuple[str, int]]) -> set[int]:
    """算出这批 subject 涉及的 owner entity（observation→entity、relation→src）。

    用于给增量 recompute 限定评分索引的扫描范围（见 FieldScoreBatch(owners=...)）。
    entity subject 自身也是 owner。

    分块大小同样由**连接**决定（`bind_batch_size`），不用写死的 `_BATCH`:
    一次 delta 可以覆盖上万个 observation（10 万观测的导入就是这样），
    而这些 id 是按 `IN (...)` 绑定的。审核 LARI-21 修的就是这里 ——
    同一批改动里 `recompute_subset` / `FieldScoreBatch` 都改用了连接上限，
    只有本函数漏了，于是在 999 上限的 SQLite 编译参数下（docstring 明说它存在）
    delta 覆盖 1000 个观测即 `too many SQL variables`。
    """
    owners: set[int] = set()
    obs_ids: list[int] = []
    rel_ids: list[int] = []
    for kind, sid in subjects:
        if kind == "entity":
            owners.add(int(sid))
        elif kind == "observation":
            obs_ids.append(int(sid))
        elif kind == "relation":
            rel_ids.append(int(sid))
    step = bind_batch_size(conn)
    for chunk in _chunks(obs_ids, step):
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(
                f"SELECT entity_id FROM observations WHERE id IN ({marks})", chunk):
            owners.add(int(r["entity_id"]))
    for chunk in _chunks(rel_ids, step):
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(
                f"SELECT src_id FROM relations WHERE id IN ({marks})", chunk):
            owners.add(int(r["src_id"]))
    return owners


# SQLite 默认变量上限 32766（旧版 999）。按块绑定，避免 "too many SQL variables"。
_BATCH = 20000

#: 绑定变量上限的兜底值：读不到连接上限时用它（见 `bind_batch_size`）。
_BIND_FALLBACK = 900


def bind_batch_size(conn: sqlite3.Connection, *, reserve: int = 0) -> int:
    """本次 **连接** 真正允许的分块大小（按绑定变量数算，留 `reserve` 个余量）。

    为什么不能只写死 `_BATCH`：SQLite 的 `SQLITE_MAX_VARIABLE_NUMBER` 是
    **编译期**决定的，可以是 999（老版本）也可以是 32766。写死一个数只对其中
    一种编译参数成立，另一种会在某个规模上突然 `too many SQL variables`。
    而它又是可查的 —— `SQLITE_LIMIT_VARIABLE_NUMBER` 就是答案。

    调用方（`recompute_subset` / `FieldScoreBatch`）用它决定 `IN (...)` 的
    分块大小，于是"多大算太大"这件事只有一个事实来源：连接自己。
    """
    try:
        limit = int(conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, sqlite3.NotSupportedError):     # pragma: no cover
        return _BIND_FALLBACK
    if limit <= 0:
        return _BIND_FALLBACK
    return max(1, min(_BATCH, limit - max(0, int(reserve))))


def recompute_subset(conn: sqlite3.Connection, subjects: Sequence[tuple[str, int]],
                     *, owners: "set[int] | None" = None) -> None:
    """批量重算一组 subject 的缓存列（O(N)，与逐行语义逐列等价）。

    三段式:
      1. 默认值重置: 把本次涉及的 subject 全部写成"无活跃贡献"的样子
         （is_current=0 / confidence='none' / count=0 / 时间列 NULL）。
         逐行实现里这一步是"读不到活贡献就原样保留旧值"的隐含行为，
         批量版必须显式做，否则撤销后缓存会残留旧值。
      2. 一次聚合活跃贡献（observation / relation / entity 各一条 GROUP BY），
         对落在本次集合内的行覆写真实值。
      3. confidence 通过 `FieldScoreBatch` 一次性预计算，避免逐行查视图。

    等价性由 INV-04（full vs delta 交错属性测试，比较缓存层全表含墓碑行）守护。
    """
    if not subjects:
        return
    want_obs: set[int] = set()
    want_rel: set[int] = set()
    want_ent: set[int] = set()
    for kind, sid in subjects:
        if kind == "observation":
            want_obs.add(int(sid))
        elif kind == "relation":
            want_rel.add(int(sid))
        elif kind == "entity":
            want_ent.add(int(sid))
    if not want_obs and not want_rel and not want_ent:
        return

    from .confidence import FieldScoreBatch

    if owners is None:
        # 全量路径: 全库索引。
        # 增量路径传 owners 时，且此处**不能**退回全库扫描 —— 否则每个 delta 都是 O(N)。
        batch = FieldScoreBatch(conn, owners=None)
    else:
        # 增量路径: 只扫被触及的 owner（delta 改 K 个 subject → owner 至多 2K 个）。
        # 额外并入本次 subject 自身的 entity_id，覆盖 "直接改 entity 字段" 的情形。
        touched = set(int(o) for o in owners)
        if want_ent:
            touched |= want_ent
        batch = FieldScoreBatch(conn, owners=touched)

    step = bind_batch_size(conn)

    # ---- observation ----
    if want_obs:
        # 聚合查询必须与重置 UPDATE **同样分块**：两者都按 subject 绑变量，而
        # SQLite 的变量上限（32766；旧版 999）与集合大小无关。旧实现只在重置时
        # 分块、聚合时一次性绑定全部 subject —— 于是「一次 delta 覆盖 > 3 万个
        # subject」会抛 `too many SQL variables`（2.5 的首个大文件导入正好踩到：
        # 10 万观测 → 崩溃）。改成同一循环内分块，SQL 与逐行语义完全不变。
        for chunk in _chunks(sorted(want_obs), step):
            marks = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE observations"
                f"   SET is_current=0, confidence='none', source_count=0, job_count=0,"
                f"       logical_job_count=0, observed_at=NULL, observed_at_min=NULL,"
                f"       observed_at_source=NULL, observed_at_accuracy=NULL,"
                f"       observed_at_precision=NULL"
                f" WHERE id IN ({marks})",
                chunk,
            )
            agg_sql = (
                "SELECT o.id                          AS sid,"
                "       o.entity_id                   AS entity_id,"
                "       o.field_namespace             AS ns,"
                "       o.semantic_field              AS field,"
                "       o.value                       AS val,"
                "       o.origin                      AS origin,"
                "       COUNT(*)                      AS n,"
                "       COUNT(DISTINCT c.source_id)   AS sources,"
                "       COUNT(DISTINCT c.job_id)      AS jobs,"
                "       COALESCE(SUM(c.job_count + c.compacted_job_count), 0) AS logical_jobs,"
                "       MAX(CASE c.authorship WHEN 'manual' THEN 1 ELSE 0 END) AS manual,"
                "       MAX(c.observed_at)            AS observed_at,"
                "       MIN(c.observed_at_min)        AS observed_at_min,"
                "       (SELECT c2.observed_at_source FROM v_active_contributions c2"
                "         WHERE c2.subject_kind='observation' AND c2.subject_id=o.id"
                "         ORDER BY c2.observed_at DESC, c2.id DESC LIMIT 1) AS t_source,"
                "       (SELECT c2.observed_at_accuracy FROM v_active_contributions c2"
                "         WHERE c2.subject_kind='observation' AND c2.subject_id=o.id"
                "         ORDER BY c2.observed_at DESC, c2.id DESC LIMIT 1) AS t_accuracy,"
                "       (SELECT c2.observed_at_precision FROM v_active_contributions c2"
                "         WHERE c2.subject_kind='observation' AND c2.subject_id=o.id"
                "         ORDER BY c2.observed_at DESC, c2.id DESC LIMIT 1) AS t_precision"
                "  FROM observations o"
                "  JOIN v_active_contributions c"
                "    ON c.subject_kind='observation' AND c.subject_id=o.id"
                f" WHERE o.id IN ({marks})"
                " GROUP BY o.id"
            )
            for row in conn.execute(agg_sql, chunk):
                n = int(row["n"])
                conf, _ = batch.score(int(row["entity_id"]), str(row["ns"]),
                                      str(row["field"]), str(row["val"]))
                origin = "manual" if int(row["manual"] or 0) else str(row["origin"])
                conn.execute(
                    "UPDATE observations SET is_current=?, confidence=?, source_count=?,"
                    " job_count=?, logical_job_count=?, origin=?, observed_at=?,"
                    " observed_at_min=?, observed_at_source=?, observed_at_accuracy=?,"
                    " observed_at_precision=? WHERE id=?",
                    (1 if n > 0 else 0, conf, int(row["sources"] or 0),
                     int(row["jobs"] or 0),
                     int(row["logical_jobs"] or 0), origin,
                     row["observed_at"], row["observed_at_min"], row["t_source"],
                     row["t_accuracy"], row["t_precision"], int(row["sid"])),
                )

    # ---- relation ----
    if want_rel:
        for chunk in _chunks(sorted(want_rel), step):
            marks = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE relations SET is_current=0, confidence='none',"
                f" source_count=0, job_count=0 WHERE id IN ({marks})",
                chunk,
            )
            rel_sql = (
                "SELECT r.id AS sid, COUNT(*) AS n,"
                "       COUNT(DISTINCT c.source_id) AS sources,"
                "       COUNT(DISTINCT c.job_id)    AS jobs"
                "  FROM relations r"
                "  JOIN v_active_contributions c"
                "    ON c.subject_kind='relation' AND c.subject_id=r.id"
                f" WHERE r.id IN ({marks})"
                " GROUP BY r.id"
            )
            for row in conn.execute(rel_sql, chunk):
                sid = int(row["sid"])
                n = int(row["n"])
                conf = _relation_confidence(conn, sid) if n > 0 else "none"
                conn.execute(
                    "UPDATE relations SET is_current=?, confidence=?, source_count=?,"
                    " job_count=? WHERE id=?",
                    (1 if n > 0 else 0, conf, int(row["sources"] or 0),
                     int(row["jobs"] or 0), sid),
                )

    # ---- entity ----
    if want_ent:
        for chunk in _chunks(sorted(want_ent), step):
            marks = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE entities SET is_current=0 WHERE id IN ({marks})", chunk)
            ent_sql = (
                "SELECT e.id AS sid, COUNT(*) AS n"
                "  FROM entities e"
                "  JOIN v_active_contributions c"
                "    ON c.subject_kind='entity' AND c.subject_id=e.id"
                f" WHERE e.id IN ({marks})"
                " GROUP BY e.id"
            )
            for row in conn.execute(ent_sql, chunk):
                conn.execute("UPDATE entities SET is_current=1 WHERE id=?",
                             (int(row["sid"]),))


def _chunks(items: Sequence[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def _relation_confidence(conn: sqlite3.Connection, rel_id: int) -> str:
    from .confidence import score_relation
    return score_relation(conn, rel_id)


def recompute(conn: sqlite3.Connection, delta: Sequence[tuple[str, int]] | None = None) -> dict[str, int]:
    """缓存重算。

    delta=None  -> 全量（正确性基准）
    delta=[...] -> 增量（性能路径）；必须与全量逐列等价（INV-04）

    无论哪条路径都执行三个幂等步骤:
        1. vacuum_orphan_cache()  —— 清掉"没有任何贡献"的缓存行（投影幻影）
        2. materialize()          —— 补建缺失的缓存行
        3. 重算被覆盖的缓存列 + 重建身份层

    这样 full 与 delta 的差异只可能出现在"被重算的列"上；而任何缓存行都
    严格等价于"至少有一条贡献指向它"，INV-13 的逐列等价因此可以精确成立。
    """
    vacuum_orphan_cache(conn)
    materialize(conn)
    if delta is None:
        subjects = _all_subjects(conn)
        owners = None                      # 全量: 全库评分索引
        log_activity(conn, actor="recompute", action="recompute.full",
                     detail={"subjects": len(subjects)})
    else:
        # 增量：闭包内的 subject + 其结构依赖（entity 的 is_current 会被 field 贡献影响）
        subjects = _closure(conn, delta)
        # 只在这些 subject 对应的 owner 上建评分索引 —— 否则 delta 要付 O(N) 全库扫描，
        # "增量"就退化成全量（这是批量重构后必须守住的性能契约）。
        owners = _owners_of(conn, subjects)
        log_activity(conn, actor="recompute", action="recompute.delta",
                     detail={"subjects": len(subjects), "owners": len(owners)})
    _recompute_subjects(conn, subjects, owners=owners)
    _rebuild_identity(conn)
    return {"subjects": len(subjects)}


def repair_declared_conflicts(conn: sqlite3.Connection) -> dict[str, int]:
    """存量脏置信度的一次性修复（0.20.0 C）：声明过的字段还标着
    `confidence='conflict'` 的观测，重算干净。

    来龙去脉: 批量评分 `FieldScoreBatch`（recompute 走的那条路）过去不读
    `multivalue_fields`，单值 `score_field` 走 `v_field_conflicts` 视图却读 ——
    两条路分叉: 010 收了 Inbox，没收观测行，真库里攒下 1442 条假 conflict。
    批量评分修好之后**新写入**都对了；存量要等各自的实体下次被触及才重算，
    所以在 API 启动时（写者队列上）收一次。

    自限与幂等: 查到 0 条直接返回（零写入）—— 修完计数归零，之后每次启动都
    是一次只读查询，常驻成本趋零。修复走 `recompute` 的 **delta 路径**（与
    导入时同一条语义），不是 UPDATE 硬改缓存列: 缓存列只能被重算覆写，手改的
    下一次重算就会被覆盖 —— 那正是"缓存与贡献层分叉"这类查不出来的问题的
    来源。
    """
    count_sql = ("SELECT COUNT(*) FROM observations o"
                 " JOIN multivalue_fields m"
                 "   ON m.field_namespace = o.field_namespace"
                 "  AND m.semantic_field  = o.semantic_field"
                 " WHERE o.confidence = 'conflict' AND o.is_current = 1")
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT DISTINCT o.id FROM observations o"
        " JOIN multivalue_fields m"
        "   ON m.field_namespace = o.field_namespace"
        "  AND m.semantic_field  = o.semantic_field"
        " WHERE o.confidence = 'conflict' AND o.is_current = 1")]
    if not ids:
        return {"repaired": 0, "remaining": 0}
    recompute(conn, delta=[("observation", i) for i in ids])
    left = int(conn.execute(count_sql).fetchone()[0])
    return {"repaired": len(ids), "remaining": left}


def vacuum_orphan_cache(conn: sqlite3.Connection) -> dict[str, int]:
    """回收"没有任何贡献、也没有人工作业锚点"的**二级**缓存行。

    为什么必须每轮 recompute 都做:
      否则会留下无法被任何贡献解释的幻影行（INV-13(b) 直接失败）。

    为什么只删"零贡献"的行:
      带有 superseded 贡献的行必须保留 —— 那条贡献的 subject_id 仍指向它，
      删掉就会造成 INV-03 悬空（undo 之后又要 redo 的话尤其致命）。

    v3.2-r2 修正（P0）:
      **entity 不再由此函数删除**。entity 行携带 entity_keys / entity_alias 等
      创建期身份事实，一旦删除就无法从事实层重建（key 不在事实里），
      并且 rowid 复用会让残留的 manual_fields / inbox_items 静默易主。
      §2.3 的孤儿反转规则要求这类对象"不得自动清理"，而是进 inbox 交人工。
    """
    obs = conn.execute(
        "DELETE FROM observations WHERE NOT EXISTS ("
        "  SELECT 1 FROM contributions c"
        "   WHERE c.subject_kind='observation' AND c.subject_id=observations.id)"
        " AND NOT EXISTS (SELECT 1 FROM manual_fields m"
        "                  WHERE m.subject_kind='observation' AND m.subject_id=observations.id)"
        " AND NOT EXISTS (SELECT 1 FROM inbox_items i"
        "                  WHERE i.subject_kind='observation' AND i.subject_id=observations.id"
        "                    AND i.state='open')"
    ).rowcount
    rel = conn.execute(
        "DELETE FROM relations WHERE NOT EXISTS ("
        "  SELECT 1 FROM contributions c"
        "   WHERE c.subject_kind='relation' AND c.subject_id=relations.id)"
        " AND NOT EXISTS (SELECT 1 FROM manual_fields m"
        "                  WHERE m.subject_kind='relation' AND m.subject_id=relations.id)"
        " AND NOT EXISTS (SELECT 1 FROM inbox_items i"
        "                  WHERE i.subject_kind='relation' AND i.subject_id=relations.id"
        "                    AND i.state='open')"
    ).rowcount
    return {"observations": obs, "relations": rel, "entities": 0}


def _all_subjects(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for kind, table in (("entity", "entities"), ("observation", "observations"),
                        ("relation", "relations")):
        for row in conn.execute(f"SELECT id FROM {table}"):
            out.append((kind, int(row["id"])))
    return out


def _closure(conn: sqlite3.Connection, seed: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    """impact_set 的一跳依赖闭包（P0-D §4.2）。

    observation -> entity （entity.is_current 受 field 贡献影响）
    relation    -> src/dst entity
    """
    seen: set[tuple[str, int]] = set(seed)
    stack = list(seed)
    while stack:
        kind, sid = stack.pop()
        if kind == "observation":
            row = conn.execute("SELECT entity_id FROM observations WHERE id=?", (sid,)).fetchone()
            if row:
                nxt = ("entity", int(row["entity_id"]))
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        elif kind == "relation":
            row = conn.execute("SELECT src_id, dst_id FROM relations WHERE id=?", (sid,)).fetchone()
            if row:
                for col in ("src_id", "dst_id"):
                    nxt = ("entity", int(row[col]))
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
    return sorted(seen)


# ---------------------------------------------------------------------------
# 身份层重建（C 层；纯 overlay，可全量重建）
# ---------------------------------------------------------------------------

def _rebuild_identity(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM entity_identity")
    # merged_into: 以 merge event 为准，且必须**递归折叠**（survivor 自身也可能后被合并）
    #
    # 解释: chain 从每个非 survivor 的 member 出发，反复"跳过"当前 merged 的 survivor，
    #       直到 survivor == current（终局）或 guard 用尽。终局行由
    #       "guard 取最大值"唯一确定（若它还能继续走，就存在更大的 guard）。
    conn.execute("UPDATE entities SET merged_into = NULL")
    conn.execute(
        """
        WITH RECURSIVE chain(entity_id, current_id, guard) AS (
            SELECT m.entity_id, ev.survivor_id, 1
              FROM entity_merge_members m
              JOIN entity_merge_events ev ON ev.id = m.merge_event_id
             WHERE ev.state = 'merged' AND ev.survivor_id <> m.entity_id
               AND m.revoked_by_job_id IS NULL
            UNION ALL
            SELECT ch.entity_id, ev2.survivor_id, ch.guard + 1
              FROM chain ch
              JOIN entity_merge_members m2 ON m2.entity_id = ch.current_id
              JOIN entity_merge_events ev2 ON ev2.id = m2.merge_event_id
             WHERE ev2.state = 'merged'
               AND m2.revoked_by_job_id IS NULL
               AND ev2.survivor_id <> ch.current_id
               AND ch.guard < 32
        )
        UPDATE entities SET merged_into = (
            SELECT ch.current_id FROM chain ch
             WHERE ch.entity_id = entities.id
             ORDER BY ch.guard DESC LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM chain ch WHERE ch.entity_id = entities.id)
        """
    )
    # canonical: 沿 merged_into 折叠（survivor 的 merged_into 为 NULL → 指向自身）
    conn.execute(
        """
        INSERT INTO entity_identity(entity_id, canonical_entity_id, canonical_key)
        SELECT e.id,
               COALESCE(e.merged_into, e.id),
               (SELECT ek.entity_key FROM entity_keys ek
                 WHERE ek.entity_id = COALESCE(e.merged_into, e.id)
                 ORDER BY ek.created_at, ek.entity_key LIMIT 1)
          FROM entities e
        """
    )
    # 原始 key 的 canonical 标记（只用于展示；唯一性以 entity_identity.canonical_key 为准）
    #   v3.2-r2: 必须按 project 收敛 —— key 只在 project 内可比（原实现按全局 key 分组，
    #   多项目同名 key 会互相污染标记，并让 duplicate_canonical_key 全库误报）
    conn.execute("UPDATE entity_keys SET canonical = 0")
    conn.execute(
        """
        UPDATE entity_keys SET canonical = 1
         WHERE EXISTS (
               SELECT 1 FROM entity_identity ii
                WHERE ii.entity_id = entity_keys.entity_id
                  AND ii.canonical_key = entity_keys.entity_key)
        """
    )


def canonical_of(conn: sqlite3.Connection, entity_id: int) -> int:
    row = conn.execute(
        "SELECT canonical_entity_id FROM entity_identity WHERE entity_id=?", (entity_id,)
    ).fetchone()
    return entity_id if row is None else int(row["canonical_entity_id"])


# §4.5 第 2 条契约的落点（v3.2-r4 收口，债 #5）。
#
# 口径**已定死为"创建期 key 胜出"**，不再留作未闭合项:
#     展示名 = canonical entity 的创建期 key（`entity_keys` 里最早的一条）
# 于是 `canonical_key` 就是展示名，rekey / merge **都不改变展示名** ——
# 这正是 identity overlay 的本意: 身份折叠改变的是"哪些 key 指向同一个实体"，
# 不是"这个实体叫什么"。新 key 只作为**解析入口**生效。
#
# 为什么不是"最新 alias 胜出": merge 会给 survivor 写入 member 的 alias 行
# （`alias_key = member_key → survivor`）。若按"最新 alias 胜出"，survivor 的展示名
# 会被**被合并掉的那个成员**的名字顶掉 —— 展示名会随合并顺序漂移。
# 将来若产品确实要"新 key 成为展示名"，必须新增显式的 `preferred` 标志
# （entity_alias.preferred），不得改变本条默认值。
def display_key(conn: sqlite3.Connection, entity_id: int) -> str | None:
    """实体的展示名（GUI / MCP / 搜索层必须使用本函数，不得自行取 alias）。"""
    canonical = canonical_of(conn, entity_id)
    row = conn.execute(
        "SELECT canonical_key FROM entity_identity WHERE entity_id=?", (canonical,)
    ).fetchone()
    if row is not None and row["canonical_key"] is not None:
        return str(row["canonical_key"])
    fallback = conn.execute(
        "SELECT entity_key FROM entity_keys WHERE entity_id=?"
        " ORDER BY created_at, entity_key LIMIT 1", (canonical,)).fetchone()
    return None if fallback is None else str(fallback["entity_key"])


# ---------------------------------------------------------------------------
# GC / purge eligibility（P0-B，审阅点 4）
# ---------------------------------------------------------------------------

def _has_manual_keys(raw: Any) -> bool:
    """`manual_fields.keys_json` 是否是一个**非空**数组。

    不导入 `core.manual._load_keys`（那会造成 manual ← store 的循环导入），
    也不因为它解析失败就判 False —— 宁可保守地保留对象，也不误删。
    """
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return True
    return isinstance(data, list) and len(data) > 0


def manual_owned(conn: sqlite3.Connection, kind: str, sid: int) -> bool:
    """manual ownership **只由活事实 + 人工作业层推导**。

    v3.2-r3 修正: 不再读缓存列 `observations.origin`。该列在贡献被抑制之后不会回退，
    于是"人工贡献被 undo 之后实体被永久锚定、永不回收" —— 这是缓存冒充事实。

    v3.2-r4 修正: `manual_fields` 必须是**非空**的字段集合才算锚点。空行不是人工状态
    （人没有保护任何字段），把它当锚点会让"清空最后一次保护"变成"永久保护"。
    """
    for row in conn.execute(
        "SELECT keys_json FROM manual_fields WHERE subject_kind=? AND subject_id=?",
        (kind, sid),
    ):
        if _has_manual_keys(row["keys_json"]):
            return True
    if kind == "entity":
        if conn.execute("SELECT 1 FROM entity_layout WHERE entity_id=?", (sid,)).fetchone():
            return True
        if conn.execute(
            "SELECT 1 FROM entity_tags WHERE entity_id=? AND deleted_at IS NULL", (sid,)
        ).fetchone():
            return True
        if conn.execute(
            "SELECT 1 FROM entity_merge_events WHERE survivor_id=? AND state='merged'", (sid,)
        ).fetchone():
            return True
    # 活的人工贡献（observation 的归属也算：人工观察使其 owner entity 成为人工对象）
    if conn.execute(
        "SELECT 1 FROM v_active_contributions"
        " WHERE subject_kind=? AND subject_id=? AND authorship='manual' LIMIT 1",
        (kind, sid),
    ).fetchone():
        return True
    if kind == "entity" and conn.execute(
        "SELECT 1 FROM v_active_contributions"
        " WHERE subject_kind='observation' AND owner_entity_id=? AND authorship='manual'"
        " LIMIT 1",
        (sid,),
    ).fetchone():
        return True
    return False


def _entity_blockers(conn: sqlite3.Connection, sid: int) -> set[str]:
    """哪些"仍然存在的事实/人工状态"引用着 entity sid？

    这里的语义是"blocker 是否存在"，不是"blocker 是否在候选集合里"——
    早期版本把两者混为一谈，导致活引用被当成已解锁，会把仍被引用的实体删掉。

    v3.2-r2 补充**墓碑 blocker**：缓存行只要还被任何贡献（活或已抑制）解释，
    它就是必须保留的事实投影；它的 FK 指向的 entity 因此也不得物理删除。
    否则 DELETE entity 会借 CASCADE 毁掉这些行，使 INV-03 立刻悬空。
    """
    blockers: set[str] = set()
    # Historical migration verification runs Undo on pre-v18 schemas too.
    research_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('research_task_links','entity_research')")}
    if 'research_task_links' in research_tables:
        for row in conn.execute("SELECT task_id FROM research_task_links WHERE entity_id=?", (sid,)):
            blockers.add(f"research_task:{row['task_id']}")
    if 'entity_research' in research_tables and conn.execute(
            "SELECT 1 FROM entity_research WHERE entity_id=?", (sid,)).fetchone():
        blockers.add(f"research_metadata:{sid}")
    for row in conn.execute(
        "SELECT id, is_current FROM observations WHERE entity_id=?", (sid,)
    ):
        prefix = "observation" if int(row["is_current"]) == 1 else "tombstone:observation"
        blockers.add(f"{prefix}:{int(row['id'])}")
    for row in conn.execute(
        "SELECT id, is_current FROM relations WHERE src_id=? OR dst_id=?", (sid, sid)
    ):
        prefix = "relation" if int(row["is_current"]) == 1 else "tombstone:relation"
        blockers.add(f"{prefix}:{int(row['id'])}")
    for row in conn.execute(
        "SELECT id FROM inbox_items WHERE subject_kind='entity' AND subject_id=?"
        " AND state='open'", (sid,)
    ):
        blockers.add(f"inbox:{int(row['id'])}")
    # 自身的存在性事实（entity 级贡献）——即使全部被抑制，历史仍指向它
    if conn.execute(
        "SELECT 1 FROM contributions WHERE subject_kind='entity' AND subject_id=? LIMIT 1",
        (sid,),
    ).fetchone():
        blockers.add(f"contribution:entity:{sid}")
    # merge overlay 是身份层，不允许被 GC 顺手拆掉
    if conn.execute(
        "SELECT 1 FROM entity_merge_events WHERE survivor_id=? AND state='merged' LIMIT 1",
        (sid,),
    ).fetchone():
        blockers.add(f"merge_survivor:entity:{sid}")
    if conn.execute(
        "SELECT 1 FROM entity_merge_members m JOIN entity_merge_events ev"
        " ON ev.id = m.merge_event_id WHERE m.entity_id=? AND ev.state='merged' LIMIT 1",
        (sid,),
    ).fetchone():
        blockers.add(f"merge_member:entity:{sid}")
    return blockers


def purge_eligible(conn: sqlite3.Connection, *,
                   include_orphans: bool = False) -> dict[str, Any]:
    """GC 判定（替代 v3.1 的单次 `origin='tool'` 判定）。

    返回 {"purge": [...], "retained": [{"subject":..., "blockers":[...]}],
          "orphan_subjects": [...]}

    规则（候选只有 entity）:
      reason 优先级: manual_owned → 依赖 blocker（活引用 / 墓碑引用 / 自身贡献 / merge）
                     → 无任何贡献且无引用 = orphan_subject

    v3.2-r2 语义收紧（INV-13 改写后的必然结果）:
      缓存层里"被事实解释过"的行是**事实的投影**，不是垃圾。因此
        - 有贡献（活或已抑制）的 entity → 保留（墓碑），只是不再计入有效状态；
        - 从未有过任何贡献的 entity → §2.3 孤儿规则：**不得自动清理**，进 inbox 交人工。
      两者都不进 `purge`，所以默认 `purge == []`：
      v3.2-r2 的 GC **不再自动物理删除任何缓存行**。
      `include_orphans=True` 保留给显式人工 GC（例如确认后清理从未有贡献的残件）。

    范围限定（重要）:
      observation / relation 属于"二级数据"，GC **绝不删除它们**。
      零贡献缓存行由 recompute → vacuum_orphan_cache() 回收。
    """
    candidates: set[tuple[str, int]] = {
        ("entity", int(r["id"]))
        for r in conn.execute("SELECT id FROM entities WHERE is_current = 0")
    }
    retained_reasons: dict[tuple[str, int], set[str]] = {}
    orphans: list[str] = []
    purge: list[str] = []
    for subject in sorted(candidates):
        reasons: set[str] = set()
        if manual_owned(conn, *subject):
            reasons.add("manual_owned")
        reasons |= _entity_blockers(conn, subject[1])
        if reasons:
            retained_reasons[subject] = reasons
            continue
        orphans.append(f"{subject[0]}:{subject[1]}")
        retained_reasons[subject] = {"orphan_subject"}
        if include_orphans:
            purge.append(f"{subject[0]}:{subject[1]}")
    retained = [
        {"subject": f"{k}:{v}", "blockers": sorted(reasons)}
        for (k, v), reasons in sorted(retained_reasons.items())
        if f"{k}:{v}" not in purge
    ]
    return {"purge": sorted(purge), "retained": retained, "orphan_subjects": orphans}


def vacuum_cache(conn: sqlite3.Connection, *, actor: str = "gc",
                 job_id: int | None = None) -> dict[str, int]:
    """显式回收（recompute 已自动调用 vacuum_orphan_cache；此函数只多写一条审计）。"""
    stats = vacuum_orphan_cache(conn)
    log_activity(conn, actor=actor, action="gc.purge", target_kind="cache_vacuum",
                 target_id=None, job_id=job_id, detail=stats)
    return stats


def orphan_subjects(conn: sqlite3.Connection) -> list[str]:
    """is_current=0 且**从未有过任何 contribution** 的 subject。

    v3.1 漏洞: 这类对象"没有活贡献"，旧规则可能顺手删掉。
    v3.2: 一律不清删，转为人工 inbox 项。
    """
    out: list[str] = []
    for kind, table in (("entity", "entities"), ("observation", "observations"),
                        ("relation", "relations")):
        for row in conn.execute(
            f"SELECT t.id AS id FROM {table} t"
            " WHERE t.is_current = 0"
            "   AND NOT EXISTS (SELECT 1 FROM contributions c"
            "                    WHERE c.subject_kind=? AND c.subject_id=t.id)",
            (kind,),
        ):
            out.append(f"{kind}:{int(row['id'])}")
    return out


def apply_purge(conn: sqlite3.Connection, purge: Sequence[str], *,
                actor: str = "gc", job_id: int | None = None) -> int:
    """只删除缓存层/身份层/工作层对象。事实层与审计层永不被 GC 触碰。"""
    removed = 0
    for token in purge:
        kind, raw = token.split(":", 1)
        sid = int(raw)
        if kind == "entity":
            conn.execute("DELETE FROM entity_identity WHERE entity_id=?", (sid,))
            conn.execute("DELETE FROM entity_alias WHERE entity_id=?", (sid,))
            conn.execute("DELETE FROM entity_keys WHERE entity_id=?", (sid,))
            conn.execute("DELETE FROM entities WHERE id=?", (sid,))
        elif kind == "observation":
            conn.execute("DELETE FROM observations WHERE id=?", (sid,))
        elif kind == "relation":
            conn.execute("DELETE FROM relations WHERE id=?", (sid,))
        else:
            raise InvariantError(f"unknown purge target {token!r}")
        removed += 1
        log_activity(conn, actor=actor, action="gc.purge", target_kind=kind,
                     target_id=sid, job_id=job_id)
    return removed


# ---------------------------------------------------------------------------
# 完整性检查
# ---------------------------------------------------------------------------

def integrity_check(conn: sqlite3.Connection) -> dict[str, Any]:
    """INV-03 / INV-07 / INV-08 / INV-15 + 缓存一致性。CI 必须为空。"""
    violations = [
        dict(row) for row in conn.execute("SELECT * FROM v_integrity_violations")
    ]
    fk = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
    sqlite_ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "violations": violations,
        "foreign_key_check": fk,
        "sqlite_integrity": sqlite_ok,
        "ok": not violations and not fk and sqlite_ok == "ok",
    }


def assert_integrity(conn: sqlite3.Connection) -> None:
    report = integrity_check(conn)
    if not report["ok"]:
        raise InvariantError(f"integrity violated: {canonical_json(report)[:2000]}")


# ---------------------------------------------------------------------------
# 状态等价（INV-13 重写 / INV-14，审阅点 19 + v3.2-r2）
#
# INV-13 旧表述"整个 domain state 逐列 == 初始状态"与 G1（contributions 不可变、
# 只增不减）不可同时成立 —— 实测 undo 全部之后 contributions / supersessions
# 必然比初始多。v3.2-r2 改写为两部分:
#
#   (a) effective state  ==  初始 effective state          （逐列相等）
#   (b) 每条历史事实仍可定位（无悬空、无幻影行）            （INV-03 / INV-15）
#   (c) 事实历史单调不减（只允许增长）                      （G1 的直接结果）
#   (d) 活跃贡献集合回到初始值
#
# "effective state" = 事实流的当前投影：is_current=1 的缓存行、与之相关的身份层行、
# 人工工作层状态、以及 state='open' 的 inbox 项。
# 墓碑行（贡献全被抑制）**存在但不算有效状态** —— 它们是历史的投影，不是垃圾。
# ---------------------------------------------------------------------------

def _normalize(value: Any, column: str) -> Any:
    if column in VOLATILE_COLUMNS:
        return "<ts>"
    return value


def _snapshot(conn: sqlite3.Connection, table: str, where: str = "",
              normalize_volatile: bool = True) -> list[tuple]:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    order = "id" if "id" in cols else ", ".join(cols)
    sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {order}"
    return [
        tuple(_normalize(row[c], c) if normalize_volatile else row[c] for c in cols)
        for row in conn.execute(sql)
    ]


def snapshot_table(conn: sqlite3.Connection, table: str,
                   normalize_volatile: bool = True) -> list[tuple]:
    return _snapshot(conn, table, normalize_volatile=normalize_volatile)


def snapshot_cache(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """缓存层 + 身份层的**完整**快照（含墓碑行）。

    用途: INV-04（full / delta recompute 逐列等价）—— 墓碑行也必须一致。
    """
    return {t: snapshot_table(conn, t) for t in CACHE_TABLES}


def snapshot_effective(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """有效状态: 事实流当前的投影（INV-13(a) 的比较对象）。"""
    active = "(SELECT id FROM entities WHERE is_current=1)"
    return {
        "entities": _snapshot(conn, "entities", "is_current=1"),
        "observations": _snapshot(conn, "observations", "is_current=1"),
        "relations": _snapshot(conn, "relations", "is_current=1"),
        "entity_keys": _snapshot(conn, "entity_keys", f"entity_id IN {active}"),
        "entity_alias": _snapshot(conn, "entity_alias", f"entity_id IN {active}"),
        "entity_identity": _snapshot(conn, "entity_identity", f"entity_id IN {active}"),
        "entity_merge_events": _snapshot(
            conn, "entity_merge_events", f"state='merged' AND survivor_id IN {active}"),
        "entity_merge_members": _snapshot(
            conn, "entity_merge_members",
            "merge_event_id IN (SELECT id FROM entity_merge_events"
            f" WHERE state='merged' AND survivor_id IN {active})"),
        # 工作层: 人工状态必须逐列一致（undo 不允许动人工数据）
        "entity_layout": _snapshot(conn, "entity_layout"),
        "entity_tags": _snapshot(conn, "entity_tags"),
        "manual_fields": _snapshot(conn, "manual_fields"),
        # inbox: 只有"未决项"属于有效状态；已关闭项是历史
        "inbox_items": _snapshot(conn, "inbox_items", "state='open'"),
    }


def snapshot_history(conn: sqlite3.Connection) -> dict[str, int]:
    """事实历史计数（INV-13(c): 只允许单调不减）。"""
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in HISTORY_TABLES
    }


def snapshot_audit(conn: sqlite3.Connection) -> dict[str, int]:
    """audit state: undo 后**不应**回到初始状态（undo 本身是历史事实）。"""
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in AUDIT_TABLES
    }


def active_contribution_ids(conn: sqlite3.Connection) -> list[int]:
    """活跃贡献集合（INV-13(d)）。"""
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM v_active_contributions ORDER BY id")]


def phantom_cache_rows(conn: sqlite3.Connection) -> list[str]:
    """INV-13(b): 无法被任何贡献解释的缓存行（必须为空）。"""
    return [str(r["ref"]) for r in conn.execute(
        "SELECT ref FROM v_integrity_violations WHERE violation='phantom_cache_row'"
        " ORDER BY ref")]


def effective_equivalent(conn: sqlite3.Connection, baseline: dict[str, list[tuple]]
                         ) -> tuple[bool, dict[str, Any]]:
    """INV-13(a): 有效状态逐列相等。"""
    current = snapshot_effective(conn)
    diff: dict[str, Any] = {}
    for table, before in baseline.items():
        after = current.get(table, [])
        if before == after:
            continue
        diff[table] = {
            "rows_before": len(before),
            "rows_after": len(after),
            "missing_after": [r for r in before if r not in after][:5],
            "unexpected_after": [r for r in after if r not in before][:5],
        }
    for table, after in current.items():
        if table not in baseline and after:
            diff[table] = {"rows_before": 0, "rows_after": len(after),
                           "missing_after": [], "unexpected_after": after[:5]}
    return (not diff), diff


def history_monotone(conn: sqlite3.Connection,
                     baseline: dict[str, int]) -> tuple[bool, dict[str, Any]]:
    """INV-13(c): 事实历史只允许增长（G1 的直接结果）。"""
    now_counts = snapshot_history(conn)
    detail = {t: {"before": baseline.get(t, 0), "after": n}
              for t, n in now_counts.items()}
    ok = all(n >= baseline.get(t, 0) for t, n in now_counts.items())
    return ok, detail


def domain_equivalent(conn: sqlite3.Connection,
                      baseline: dict[str, list[tuple]]) -> tuple[bool, dict[str, Any]]:
    """INV-13 综合判定（兼容旧调用点）。

    入参 baseline 是**初始 effective 快照**；判定:
        (a) effective state 逐列相等
        (b) 无幻影行 / 无悬空（v_integrity_violations 的 orphan/phantom 类为空）
    """
    ok_effective, diff = effective_equivalent(conn, baseline)
    phantoms = phantom_cache_rows(conn)
    if phantoms:
        diff["phantom_cache_row"] = phantoms[:5]
    orphans = [dict(r) for r in conn.execute(
        "SELECT * FROM v_integrity_violations WHERE violation LIKE 'orphan%' LIMIT 5")]
    if orphans:
        diff["orphan"] = orphans
    return (ok_effective and not phantoms and not orphans), diff


def audit_strictly_grew(conn: sqlite3.Connection,
                        baseline: dict[str, int]) -> tuple[bool, dict[str, Any]]:
    now_counts = snapshot_audit(conn)
    detail = {
        t: {"before": baseline.get(t, 0), "after": now_counts[t]} for t in AUDIT_TABLES
    }
    grew = now_counts["activity_log"] > baseline.get("activity_log", 0)
    no_shrink = all(now_counts[t] >= baseline.get(t, 0) for t in AUDIT_TABLES)
    return (grew and no_shrink), detail


# ---------------------------------------------------------------------------
# parser 事实（v3.2-r2: coverage 依赖它，因此它必须是已登记的数据事实）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# source 目录（v3.2-r3: §8.2 的 authority_term 必须真的有数据源）
# ---------------------------------------------------------------------------

def register_source(conn: sqlite3.Connection, *, name: str,
                    authority_level: str = "unverified", tool: str | None = None,
                    source_id: int | None = None, actor: str = "cli") -> int:
    """登记一个 catalog source（幂等：按 name 复用）。

    authority_level 是 §8.2 authority_term 的唯一数据源；
    未登记就写贡献会被 `contributions.source_id` 的外键拒绝。
    """
    if authority_level not in SOURCE_AUTHORITY_LEVELS:
        raise InvariantError(
            f"bad authority_level {authority_level!r}; "
            f"expected one of {SOURCE_AUTHORITY_LEVELS}")
    row = conn.execute("SELECT id, authority_level FROM sources WHERE name=?",
                       (name,)).fetchone()
    if row is not None:
        if str(row["authority_level"]) != authority_level:
            conn.execute("UPDATE sources SET authority_level=? WHERE id=?",
                         (authority_level, int(row["id"])))
        return int(row["id"])
    if source_id is None:
        cur = conn.execute(
            "INSERT INTO sources(name, authority_level, tool, created_at) VALUES (?,?,?,?)",
            (name, authority_level, tool, now()))
        sid = int(cur.lastrowid)
    else:
        conn.execute(
            "INSERT INTO sources(id, name, authority_level, tool, created_at)"
            " VALUES (?,?,?,?,?)", (source_id, name, authority_level, tool, now()))
        sid = int(source_id)
    log_activity(conn, actor=actor, action="source.register", target_kind="source",
                 target_id=sid, detail={"name": name, "authority_level": authority_level})
    return sid


def source_authority(conn: sqlite3.Connection, source_id: int) -> str | None:
    row = conn.execute("SELECT authority_level FROM sources WHERE id=?",
                       (source_id,)).fetchone()
    return None if row is None else str(row["authority_level"])


#: 「这次登记只有名字，没有实现可 hash」的**显式**标记（2.4 同期必交项 B）。
#:
#: 它存在是因为两类登记**真的不是一回事**，把它们混成一个默认值正是 v3.2 §16.4
#: 第 2 条那笔债的来源：
#:
#:   * **实现级登记**（解析器自己，`ingest` 走这条）—— 调用方手里有解析器类，
#:     `inspect.getsource` 就能算出实现 hash。这条路上 `definition_hash` 是真值。
#:   * **名称级登记**（项目 scope 上钉的 `parser_fingerprint`、job 的快照）——
#:     调用方只有一个名字 `nmap@1`，**没有任何实现可 hash**。硬要一个 hash，
#:     得到的只会是"把名字再 hash 一遍"这种自欺。
#:
#: 所以这里不给默认值，而是让名称级的调用方**显式写出这个常量**：它可 grep、
#: 可统计，读代码的人一眼能看出"这一行登记没有实现 hash"。
UNHASHED_PARSER = "unhashed:name-only"


def register_parser(conn: sqlite3.Connection, fingerprint: str, *,
                    definition_hash: str | None = None,
                    profile_id: str | None = None, version: str | None = None,
                    actor: str = "cli") -> None:
    """登记一个 parser 指纹（幂等）。

    parser 指纹是**事实**：coverage 判定（§5.1）拿它和项目当前指纹比较，
    因此它必须可被引用（jobs / projects 都有指向它的 FK），也必须可审计。

    `definition_hash` 回答的是另一个问题：**同一 fingerprint 背后的实现有没有
    变过**。所以它是必填的（v3.2 §16.4 第 2 条）：

    * **新登记**缺 `definition_hash` → `InvariantError`，文案说明缺了会答不出什么；
    * **已登记**的行是幂等的空操作；若老行的 hash 还是 NULL / 只有名称级标记，
      而这次带来了真实现 hash → **回填**（那不是漂移，是第一次知道实现长什么样）；
    * 已登记且**已有真 hash**，这次带来的不一样 → **不覆盖**，由调用方自己报漂移
      （`ingest._definition_drift`）。覆盖它会让"实现变过"这件事永远查不出来。

    DB 层还有一道 `trg_parser_requires_definition_hash` 兜底（迁移 003），
    所以绕过应用层的写入同样进不来。
    """
    if not fingerprint:
        raise InvariantError("parser fingerprint must be non-empty")
    provided = (definition_hash or "").strip() or None
    row = conn.execute("SELECT definition_hash FROM parser_profiles WHERE fingerprint=?",
                       (fingerprint,)).fetchone()
    if row is not None:
        stored = str(row["definition_hash"] or "").strip() or None
        if provided is not None and stored in (None, UNHASHED_PARSER):
            # 老库回填 / 名称级升级为实现级：这不是漂移，是第一次知道实现。
            conn.execute("UPDATE parser_profiles SET definition_hash=? WHERE fingerprint=?",
                         (provided, fingerprint))
            log_activity(conn, actor=actor, action="parser.register",
                         target_kind="parser",
                         detail={"fingerprint": fingerprint, "backfilled": True,
                                 "definition_hash": provided})
        return
    if provided is None:
        raise InvariantError(
            "register_parser 必须提供 definition_hash —— 缺了它，"
            f"「同一 fingerprint（{fingerprint}）背后的实现有没有变过」就无法回答"
            "（v3.2 §16.4 第 2 条）。名称级登记（拿不到实现）请显式传 "
            "store.UNHASHED_PARSER。")
    conn.execute(
        "INSERT INTO parser_profiles(fingerprint, profile_id, version,"
        " definition_hash, registered_at) VALUES (?,?,?,?,?)",
        (fingerprint, profile_id, version, provided, now()),
    )
    log_activity(conn, actor=actor, action="parser.register",
                 target_kind="parser", detail={"fingerprint": fingerprint,
                                               "profile_id": profile_id,
                                               "version": version,
                                               "definition_hash": provided})
