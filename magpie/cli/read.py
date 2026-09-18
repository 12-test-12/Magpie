"""只读命令的实现：`init` / `ls` / `show` / `search` / `diff` / `doctor` / `schema`。

**这个模块里没有一条 SQL 在写事实。** 唯一的写是 `init` 建 schema —— 而它连
一个 project 都不建。原因不是洁癖：2.2 的价值在于**把"读"这条路的风险压到最低**，
先冻结契约；一条顺手 `INSERT` 的读命令会把这份价值立刻抹掉，而且不会有任何
测试替你发现（测试只断言它读得对）。

与 2.7 的关系（LARI-8《排序变更记录②》的裁定）
----------------------------------------------
**契约以 2.7 的 `openapi.json` 为准，本模块适配它，不重新定义它。** 具体到三点:

  * 信封逐字同形（见 `envelope.py`）；
  * 过滤条件、分页游标、404/409 的分档与 `/api/v1` 一致 —— 包括"不支持的
    过滤条件必须**响亮拒绝**而不是静默忽略"这条；
  * `search` **故意不实现**。2.7 的 `/search` 返回 501（搜索 DSL 属于 §8），
    若 CLI 自己发明一套查询语义，就出现了"两条路、一份数据、两种答案"——
    而"GUI 与 CLI 共用同一实现"是 §3.5 已经写下的原则。所以这里也 501
    （`E_NOT_IMPLEMENTED`），把 agent 引向 core，而不是给它一个假答案。

**已知技术债（明写，不藏着）**：`ls` / `show` 的查询语句与 `api/routes.py` 里
的那几条是**同一份语义的两份实现**。计划书 §3.5 的 `core/query.py`（"具名查询；
GUI 与 CLI 共用同一实现"）落地后应当收敛到一处。现在不收敛的理由是本步的兼容性
约束写着"不触碰 `magpie/core/**`" —— 把它记在这里，别让它变成没人知道的重复。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from ..core import ops, store
from .db import Database, current_version
from .envelope import (
    EXIT_DATA,
    EXIT_HUMAN,
    EXIT_MEANING,
    EXIT_TO_HTTP,
    EXIT_USAGE,
    EXIT_VERSION,
    CliError,
    not_implemented,
    table,
)

#: `ls` 支持的过滤条件。**唯一声明处** —— 报错信息也从它来（与 2.7 的
#: `routes._ENTITY_FILTERS` 对齐）。`review_status` / `in_scope` 在 2.7 里
#: 就是"传了就 400"，它们需要"人工状态"与 scope 求值，本步没有。
ENTITY_FILTERS = ("kind", "q", "tag", "lifecycle", "cursor", "limit")

#: `diff` 一次最多列多少个实体。**到顶要写进输出**（`truncated`），
#: 静默截断看起来和"就这么些"一模一样。
DIFF_ENTITY_LIMIT = 200

#: `ls` 每页上限，与 2.7 的 `MAX_PAGE` 一致。
MAX_PAGE = 500
DEFAULT_PAGE = 50


# ---------------------------------------------------------------------------
# 公共零件（与 `api/routes.py` 的同名零件保持同义）
# ---------------------------------------------------------------------------


def _tbl(header: list[str], rows: list[list[Any]], empty: str = "（无）") -> list[str]:
    """空表时只回一句 `（无）`。

    `table()` 在空 rows 上只回表头 —— 那在终端里长得像"有个表，只是没打印出来"，
    而"这里是空的"与"这里没打印"是两件事。
    """
    return table(header, rows) if rows else [f"  {empty}"]


def _json_or_none(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


def resolve_project(conn: sqlite3.Connection, spec: str | None) -> int:
    """`--project` 收 id 或名字；不给时，**只有一个项目**才允许省略。

    刻意不做"默认选第一个"：多项目下"第一个"是行序，不是意图。让调用方显式写，
    比让他某天发现数据进了另一个项目要好。（这条与 2.7 的 `_resolve_project`
    逐字同义，退出码 1 ↔ HTTP 400。）
    """
    rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    if spec is None:
        if len(rows) == 1:
            return int(rows[0]["id"])
        raise CliError(
            "E_PROJECT_REQUIRED",
            f"这个库里有 {len(rows)} 个项目，必须显式给 --project",
            exit_code=EXIT_USAGE,
            hint="加上 `--project <id 或名字>`；项目列表见 `magpie schema --json`"
                 " 的 projects（或 `magpie ls --project` 的报错 candidates）。",
            projects=[{"id": int(r["id"]), "name": str(r["name"])} for r in rows])
    text = str(spec)
    row = None
    if text.isdigit():
        row = conn.execute("SELECT id FROM projects WHERE id=?", (int(text),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM projects WHERE name=?", (text,)).fetchone()
    if row is None:
        raise CliError(
            "E_PROJECT_NOT_FOUND", f"项目 {text!r} 不存在", exit_code=EXIT_DATA,
            hint="用 id 或**完整的**项目名；名字是大小写敏感的。",
            candidates=[{"id": int(r["id"]), "name": str(r["name"])} for r in rows])
    return int(row["id"])


def keys_of(conn: sqlite3.Connection, project_id: int,
            entity_ids: list[int]) -> dict[int, str]:
    """entity_id → 规范 key。一次查完，不做 N+1。"""
    if not entity_ids:
        return {}
    marks = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT entity_id, entity_key FROM entity_keys"
        f" WHERE project_id=? AND canonical=1 AND entity_id IN ({marks})",
        (project_id, *entity_ids)).fetchall()
    return {int(r["entity_id"]): str(r["entity_key"]) for r in rows}


def tags_of(conn: sqlite3.Connection, project_id: int, entity_id: int) -> list[str]:
    return [str(r["tag"]) for r in conn.execute(
        "SELECT tag FROM entity_tags WHERE project_id=? AND entity_id=?"
        " AND deleted_at IS NULL ORDER BY tag", (project_id, entity_id))]


def manual_keys(conn: sqlite3.Connection, project_id: int, entity_id: int) -> list[str]:
    row = conn.execute(
        "SELECT keys_json FROM manual_fields WHERE project_id=? AND subject_kind='entity'"
        " AND subject_id=?", (project_id, entity_id)).fetchone()
    keys = _json_or_none(None if row is None else row["keys_json"])
    return [str(k) for k in keys] if isinstance(keys, list) else []


def entity_row(conn: sqlite3.Connection, project_id: int, entity_id: int) -> dict:
    row = conn.execute(
        "SELECT id, kind, lifecycle, is_current, merged_into, first_seen, last_seen"
        " FROM entities WHERE id=? AND project_id=?",
        (entity_id, project_id)).fetchone()
    if row is None:
        raise CliError("E_ENTITY_NOT_FOUND",
                       f"实体 {entity_id} 不在项目 {project_id} 里", exit_code=EXIT_DATA)
    entity = dict(row)
    entity["entity_key"] = keys_of(conn, project_id, [entity_id]).get(entity_id)
    return entity


def _unsupported(filters: dict[str, Any], supported: tuple[str, ...]) -> None:
    """沉默地忽略一个过滤条件 = 给 agent 一个假答案。**必须响亮地拒绝。**"""
    unknown = sorted(k for k, v in filters.items() if v not in (None, ""))
    if unknown:
        raise CliError(
            "E_UNSUPPORTED_FILTER",
            f"本步不支持这些过滤条件：{', '.join(unknown)}",
            exit_code=EXIT_USAGE,
            hint=f"支持的过滤条件：{', '.join(supported)}。"
                 f"被静默忽略的过滤条件会让调用方以为筛过了 —— 那比报错坏得多。",
            supported=list(supported), rejected=unknown)


def _page_size(limit: int) -> int:
    if limit < 1:
        raise CliError("E_BAD_LIMIT", f"limit 必须 >= 1，收到 {limit}",
                       exit_code=EXIT_USAGE)
    return min(int(limit), MAX_PAGE)


def _probe(db_path) -> tuple[int, list]:
    """`init --dry-run` 的探针：**不建文件、不改任何东西**。

    刻意不走 `Database.connect()`：那条路要求"库必须已经建好且版本自洽"，
    而干跑最常问的恰恰是"我还什么都没建，它会做什么"。所以这里自己开一条
    最朴素的连接，只读账本。
    """
    from ..core import migrate

    if not db_path.exists():
        return 0, list(migrate.MIGRATIONS)
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:                                        # pragma: no cover
        return 0, list(migrate.MIGRATIONS)
    try:
        conn.execute("PRAGMA query_only = ON")
        version = current_version(conn)
    except sqlite3.DatabaseError as exc:
        raise CliError("E_DB_NOT_A_DB", f"{db_path} 不是一个可读的 SQLite 库：{exc}",
                       exit_code=EXIT_DATA) from exc
    finally:
        conn.close()
    return version, [m for m in migrate.MIGRATIONS if m.version > version]


# ---------------------------------------------------------------------------
# init —— 本步唯一的写，而且只写 schema
# ---------------------------------------------------------------------------


def cmd_init(args, ctx) -> tuple[dict, list[str]]:
    """建库 / 升级 schema。**建 schema，不建数据。**

    为什么它在"只读骨架"里：`ls` 不能顺手建库 —— 那样"库在哪、谁建的"
    就没有答案了。建库必须是一个**显式的、可以被记一笔的**动作。

    它不建 project：项目的创建是写路径（2.6 / GUI）的事。所以 `init` 之后
    `magpie ls` 会诚实地告诉你"一个项目都没有"，而不是替你造一个。
    """
    from ..core import migrate

    db: Database = ctx.db
    existed = db.path.exists()

    if args.dry_run:
        # §6.3 的"`--dry-run` 到处可用"。对 init 来说它的意思是"只说要做什么"，
        # 所以**连文件都不该建** —— 一个 0 字节的 magpie.db 会让下一次 init
        # 报告"库已存在"，那是真的副作用，不是"什么都没做"。
        version, pending = _probe(db.path)
        data = {"db": db.display, "created": not existed, "applied": [],
                "pending": [{"version": m.version, "name": m.name} for m in pending],
                "schema_version": version, "dry_run": True}
        human = [f"（干跑）{db.display}："
                 + (f"会应用 {len(pending)} 个迁移 —— "
                    + "、".join(f"v{m.version} {m.name}" for m in pending)
                    if pending else "已是最新，不会做任何改动")]
        return data, human

    conn = db.writable()
    try:
        # 快照只为**升级已有库**而拍。给一个刚建出来的空库拍快照只会往数据区
        # 里扔一个没人会要的文件，而真正需要退路的那一刻（改已有 schema）
        # 恰恰不是它。
        todo = migrate.pending(conn)
        applied = migrate.migrate(conn, snapshot=bool(todo) and existed)
        db.schema_version = migrate.current_version(conn)
    except migrate.MigrationError as exc:
        raise CliError(
            "E_MIGRATION_FAILED", str(exc), exit_code=EXIT_VERSION,
            hint="迁移**只增不改**：已应用的迁移文件被改动会让新库与老库看起来"
                 "一样、实际不一样。新增一个迁移文件，不要改旧的。") from exc

    data = {
        "db": db.display,
        "created": not existed,
        "applied": applied,
        "schema_version": db.schema_version,
        "projects": [dict(r) for r in conn.execute(
            "SELECT id, name FROM projects ORDER BY id")],
    }
    human = [
        f"{'建库' if not existed else '升级'}：{db.display}",
        f"schema 版本：{db.schema_version}"
        + (f"（本次应用 {len(applied)} 个迁移："
           f"{', '.join('v%d %s' % (m['version'], m['name']) for m in applied)}）"
           if applied else "（已是最新，未做任何改动）"),
        f"项目数：{len(data['projects'])}"
        + ("（空库里一个项目都没有 —— 建项目是 2.6 写路径的事）"
           if not data["projects"] else ""),
    ]
    return data, human


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def cmd_ls(args, ctx) -> tuple[dict, list[str]]:
    """实体列表。键集游标分页 —— 与 `/api/v1/entities` 同一套语义。"""
    _unsupported({"review_status": args.review_status, "in_scope": args.in_scope},
                 ENTITY_FILTERS)
    size = _page_size(args.limit)
    conn: sqlite3.Connection = ctx.conn()
    project_id = resolve_project(conn, args.project)

    where = ["e.project_id=?", "e.is_current=1", "e.merged_into IS NULL"]
    params: list[Any] = [project_id]
    if args.kind:
        where.append("e.kind=?")
        params.append(str(args.kind))
    if args.lifecycle:
        where.append("e.lifecycle=?")
        params.append(str(args.lifecycle))
    if args.q:
        where.append("EXISTS (SELECT 1 FROM entity_keys k WHERE k.entity_id=e.id"
                     " AND k.project_id=e.project_id AND k.entity_key LIKE ?)")
        params.append(f"%{args.q}%")
    if args.tag:
        where.append("EXISTS (SELECT 1 FROM entity_tags t WHERE t.entity_id=e.id"
                     " AND t.project_id=e.project_id AND t.tag=? AND t.deleted_at IS NULL)")
        params.append(str(args.tag))
    if args.cursor not in (None, "", "null"):
        if not str(args.cursor).isdigit():
            raise CliError("E_BAD_CURSOR",
                           f"cursor 必须是上一页末尾的实体 id，收到 {args.cursor!r}",
                           exit_code=EXIT_USAGE)
        where.append("e.id > ?")
        params.append(int(args.cursor))

    rows = conn.execute(
        "SELECT e.id, e.kind, e.lifecycle, e.first_seen, e.last_seen,"
        " (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id"
        "   AND k.project_id=e.project_id AND k.canonical=1"
        "   ORDER BY k.entity_key LIMIT 1) AS entity_key,"
        " (SELECT COUNT(*) FROM observations o WHERE o.entity_id=e.id AND o.is_current=1)"
        "   AS observation_count,"
        " (SELECT COUNT(*) FROM relations r WHERE r.src_id=e.id AND r.is_current=1)"
        "   AS out_degree"
        f" FROM entities e WHERE {' AND '.join(where)}"
        " ORDER BY e.id LIMIT ?", (*params, size)).fetchall()
    items = [dict(r) for r in rows]
    next_cursor = str(items[-1]["id"]) if len(items) == size and items else None

    data = {"entities": items, "count": len(items)}
    human = _tbl(["ID", "KIND", "KEY", "LIFECYCLE", "OBS", "OUT", "FIRST_SEEN"],
                 [[i["id"], i["kind"], i["entity_key"], i["lifecycle"],
                   i["observation_count"], i["out_degree"], i["first_seen"]]
                  for i in items],
                 empty="（本项目当前没有实体 —— 这是结果，不是错误）")
    if next_cursor:
        human.append(f"… 还有下一页：--cursor {next_cursor}")
    return data, human


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _entity_for_alias(conn: sqlite3.Connection, project_id: int,
                      text: str) -> int | None:
    """生效 alias → entity_id。

    `entity_alias` 上有一条**部分唯一索引**（`001_init.sql`:
    `(project_id, alias_key) WHERE revoked_by_job_id IS NULL`），所以这里
    `fetchone()` 是**真的**唯一，不是碰巧唯一 —— 唯一性由数据库保证，不由
    调用方的写法保证。
    """
    row = conn.execute(
        "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
        " AND revoked_by_job_id IS NULL", (project_id, text)).fetchone()
    if row is not None:
        return int(row["entity_id"])
    # 兜底：key 表里有、alias 表里没有。体检项 `key_without_alias` 会报这种状态，
    # 但在它被报出来之前，`show <key>` 不该比数据库更早地失败。
    row = conn.execute(
        "SELECT entity_id FROM entity_keys WHERE project_id=? AND entity_key=?",
        (project_id, text)).fetchone()
    return None if row is None else int(row["entity_id"])


def _resolve_ref(conn: sqlite3.Connection, project_id: int, ref: str) -> int:
    """`<id|entity_key>` → entity_id。

    **歧义必须交给人，不能替人挑一个**（退出码 3 存在的意义）。

    歧义从哪里来 —— 这里要写准，因为一开始我以为它来自 alias 表：**不是**，
    生效 alias 的 key 在库内唯一（见 `_entity_for_alias`）。真正的歧义是
    **同一个字面量有两种读法**：`show 42` 既可能是"实体 id 42"，也可能是
    "key 恰好是 `42` 的那个实体"。两者都存在且指向不同实体时，命令**不能猜**：
    猜错的那一半不会有任何症状，直到某天有人按一个错误的结论行动。

    两种读法各写清楚，剩下的组合都是确定的:

      * 数字、且 key 表里没有同名字面量 → 按 **id** 读；
      * 数字、且 key 表里有 → **两个都查**：指向同一实体则无歧义，指向不同
        实体则 `E_AMBIGUOUS` + `candidates`；
      * 数字、但库里**没有**这个 id → 按 **key** 读（"42" 是一个合法的 key）。
    """
    text = str(ref)
    by_key = _entity_for_alias(conn, project_id, text)

    if not text.isdigit():
        if by_key is None:
            raise _not_found(text)
        return by_key

    by_id = int(text)
    exists_id = conn.execute(
        "SELECT 1 FROM entities WHERE id=? AND project_id=?",
        (by_id, project_id)).fetchone() is not None

    if by_key is not None and exists_id and by_key != by_id:
        # 候选里用 `store.display_key` 而不是自己拼一个 —— §4.5 写明
        # "GUI / MCP / 搜索层**必须**调用它，不得自行取 alias"。人在这里要判的
        # 正是"哪个是哪个"，而展示名的口径已经有一个权威实现了。
        raise CliError(
            "E_AMBIGUOUS",
            f"{text!r} 既是一个实体 id，也是另一个实体的 key，需要人工裁决",
            exit_code=EXIT_HUMAN,
            hint=f"两个读法指向不同的实体。用 `magpie show {by_id}` 查 id 那个，"
                 f"或先 `magpie ls --q {text}` 看清是哪一条。**本命令不替你挑。**",
            candidates=[
                {"reading": "id", "entity_id": by_id,
                 "entity_key": store.display_key(conn, by_id)},
                {"reading": "key", "entity_id": by_key,
                 "entity_key": store.display_key(conn, by_key)},
            ])
    if exists_id:
        return by_id
    if by_key is not None:
        return by_key
    raise _not_found(text)


def _not_found(text: str) -> CliError:
    return CliError(
        "E_ENTITY_NOT_FOUND", f"没有 id 为 {text} 的实体，也没有 key 为 {text!r} 的实体",
        exit_code=EXIT_DATA,
        hint="key 是**规范写法**（normalize 之后）：域名小写去尾点、"
             "IDN 转 punycode、服务是 host:port。`magpie ls --q <片段>` 可以模糊找。")


def cmd_show(args, ctx) -> tuple[dict, list[str]]:
    """实体详情（观测 / 关系 / 标签 / Inbox / 时间线）。形状与 `/entities/{id}` 同义。"""
    conn: sqlite3.Connection = ctx.conn()
    project_id = resolve_project(conn, args.project)
    entity_id = _resolve_ref(conn, project_id, args.ref)
    entity = entity_row(conn, project_id, entity_id)

    observations = [dict(r) for r in conn.execute(
        "SELECT id, semantic_field, value, value_kind, confidence, source_count,"
        " job_count, origin, observed_at, observed_at_accuracy"
        " FROM observations WHERE entity_id=? AND project_id=? AND is_current=1"
        " ORDER BY semantic_field, id", (entity_id, project_id))]

    rows = conn.execute(
        "SELECT id, rel, src_id, dst_id, confidence, source_count, job_count"
        " FROM relations WHERE project_id=? AND is_current=1"
        " AND (src_id=? OR dst_id=?) ORDER BY rel, id",
        (project_id, entity_id, entity_id)).fetchall()
    other_ids = sorted({int(r["dst_id"]) if int(r["src_id"]) == entity_id
                        else int(r["src_id"]) for r in rows})
    keys = keys_of(conn, project_id, other_ids)
    relations = []
    for row in rows:
        item = dict(row)
        other = int(row["dst_id"]) if int(row["src_id"]) == entity_id else int(row["src_id"])
        item["direction"] = "out" if int(row["src_id"]) == entity_id else "in"
        item["other_id"] = other
        item["other_key"] = keys.get(other)
        relations.append(item)

    inbox = [dict(r) for r in conn.execute(
        "SELECT id, kind, severity, state, payload_json, created_at FROM inbox_items"
        " WHERE project_id=? AND subject_kind='entity' AND subject_id=? AND state='open'"
        " ORDER BY id", (project_id, entity_id))]
    for item in inbox:
        item["payload"] = _json_or_none(item.pop("payload_json"))

    timeline = [dict(r) for r in conn.execute(
        "SELECT id, actor, action, at, job_id FROM activity_log"
        " WHERE target_kind='entity' AND target_id=? ORDER BY id DESC LIMIT 20",
        (entity_id,))]

    data = {"entity": entity, "observations": observations, "relations": relations,
            "tags": tags_of(conn, project_id, entity_id),
            "manual": {"protected_fields": manual_keys(conn, project_id, entity_id)},
            "open_inbox": inbox, "timeline": timeline}

    human = [f"#{entity['id']}  {entity['entity_key']}  "
             f"({entity['kind']} / {entity['lifecycle']})",
             f"  first_seen={entity['first_seen']}  last_seen={entity['last_seen']}",
             ""]
    human += ["观测："] + _tbl(
        ["FIELD", "VALUE", "CONF", "SRC", "JOBS", "AT", "ACC"],
        [[o["semantic_field"], o["value"], o["confidence"], o["source_count"],
          o["job_count"], o["observed_at"], o["observed_at_accuracy"]]
         for o in observations])
    human += ["", "关系："] + _tbl(
        ["DIR", "VERB", "OTHER", "KEY", "CONF"],
        [[r["direction"], r["rel"], r["other_id"], r["other_key"], r["confidence"]]
         for r in relations])
    human += ["", f"标签：{', '.join(data['tags']) or '（无）'}",
              f"人工保护字段：{', '.join(data['manual']['protected_fields']) or '（无）'}",
              f"未决 Inbox：{len(inbox)} 条"]
    return data, human


# ---------------------------------------------------------------------------
# search —— 与 2.7 的 /search 一样，响亮地 501
# ---------------------------------------------------------------------------


def cmd_search(args, ctx) -> tuple[dict, list[str]]:
    raise not_implemented(f"搜索（q={args.query!r}）", plan="计划书 §8（查询与搜索）")


# ---------------------------------------------------------------------------
# diff —— 自某个作业起，库里多了什么
# ---------------------------------------------------------------------------


def _resolve_since(conn: sqlite3.Connection, project_id: int, spec: str) -> dict:
    """`<job_id|ISO|last>` → 一个时间点 + 它是怎么来的。

    **口径**：`--since` 是"从那一刻起"。`last` = 本项目的最后一个作业，
    判定用的是 `jobs.started_at` —— 一次导入的多个 job 的 `started_at` 几乎
    相同，所以 `diff --since last` 得到的是**这一批导入**带来的全部变化，
    而不是只剩最后那个文件的那一点。

    刻意**不**用 `entities.first_seen / last_seen` 做增量判据：那两列只在
    实体创建时写一次（`store.new_entity`），之后没有任何地方更新它们 ——
    拿 `last_seen` 当"最后活跃时间"会得到一个永远停在建库那天的答案。
    作业归属（`contributions.job_id`）才是模型里**真的存在**的事实。
    """
    text = str(spec)
    if text.isdigit():
        row = conn.execute("SELECT id, started_at FROM jobs WHERE id=? AND project_id=?",
                           (int(text), project_id)).fetchone()
        if row is None:
            raise CliError("E_JOB_NOT_FOUND",
                           f"项目 {project_id} 里没有作业 {text}", exit_code=EXIT_DATA,
                           hint="`magpie doctor --json` 的 jobs 段列出全部作业。")
        return {"spec": text, "at": str(row["started_at"]), "job_id": int(row["id"])}
    if text == "last":
        row = conn.execute("SELECT id, started_at FROM jobs WHERE project_id=?"
                           " ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        if row is None:
            return {"spec": "last", "at": None, "job_id": None}
        return {"spec": "last", "at": str(row["started_at"]), "job_id": int(row["id"])}
    stamp = store.parse_time(text)
    if stamp is None:
        raise CliError(
            "E_BAD_SINCE",
            f"--since 只收作业 id / ISO 时间 / 'last'，收到 {text!r}",
            exit_code=EXIT_USAGE,
            hint="例：`--since last`、`--since 3`、`--since 2026-09-18T00:00:00Z`。",
            accepted=["<job_id>", "<ISO8601>", "last"])
    return {"spec": text, "at": text, "job_id": None}


_SINCE_SQL = """
WITH win AS (
    SELECT c.id, c.job_id, c.contribution_kind, c.semantic_field, c.field_value,
           c.observed_at, c.observed_at_accuracy,
           CASE WHEN c.subject_kind='entity' THEN c.subject_id
                ELSE c.owner_entity_id END AS entity_id
      FROM v_active_contributions c
      JOIN jobs j ON j.id = c.job_id
     WHERE c.project_id = ? AND j.started_at >= ?
),
before AS (
    SELECT DISTINCT CASE WHEN c.subject_kind='entity' THEN c.subject_id
                         ELSE c.owner_entity_id END AS entity_id
      FROM v_active_contributions c
      JOIN jobs j ON j.id = c.job_id
     WHERE c.project_id = ? AND j.started_at < ?
)
SELECT w.*, (b.entity_id IS NULL) AS is_new
  FROM win w LEFT JOIN before b ON b.entity_id = w.entity_id
 WHERE w.entity_id IS NOT NULL
 ORDER BY w.entity_id, w.id
"""


def cmd_diff(args, ctx) -> tuple[dict, list[str]]:
    """自 `--since` 起**仍然生效**的变化。

    两个细节值得写下来:

      * **只统计仍然生效的贡献**（`v_active_contributions`）。一个作业被
        `undo` 之后它的效果失效，diff 必须跟着变空 —— 否则"撤销成功"这条
        结论就少了一个独立佐证。2.6 的端到端流程正是靠这一点验 undo 的。
      * **时间精度必须露出来**（§8.1 / v3.1 的隐患）：本判据走的是作业归属，
        不依赖 `observed_at`，但事实的 `observed_at_accuracy` 仍然一并报出并
        计数。真实夹具里 453 条贡献的精度**全是 `unknown`** —— 如果某天有人
        把 diff 改成按 `observed_at` 排序，这个计数就是那个改动唯一会在
        输出里留下的痕迹。
    """
    conn: sqlite3.Connection = ctx.conn()
    project_id = resolve_project(conn, args.project)
    since = _resolve_since(conn, project_id, args.since)

    empty = {"since": since, "entities": {"new": [], "changed": []},
             "counts": {"new": 0, "changed": 0, "facts": 0, "entities_truncated": 0},
             "time_accuracy": {"exact": 0, "approximate": 0, "unknown": 0},
             "notes": []}
    if since["at"] is None:
        empty["notes"].append("本项目还没有任何作业 —— 没有可比的基准。")
        return empty, ["（本项目还没有任何作业，无从 diff）"]

    rows = conn.execute(_SINCE_SQL,
                        (project_id, since["at"], project_id, since["at"])).fetchall()

    grouped: dict[int, dict] = {}
    accuracy = {"exact": 0, "approximate": 0, "unknown": 0}
    for row in rows:
        eid = int(row["entity_id"])
        bucket = grouped.setdefault(eid, {
            "id": eid, "is_new": bool(row["is_new"]), "facts": [],
            "jobs": sorted({int(row["job_id"])}),
        })
        acc = row["observed_at_accuracy"] or "unknown"
        accuracy[acc] = accuracy.get(acc, 0) + 1
        bucket["facts"].append({
            "job_id": int(row["job_id"]),
            "kind": str(row["contribution_kind"]),
            "field": row["semantic_field"],
            "value": row["field_value"],
            "observed_at": row["observed_at"],
            "observed_at_accuracy": acc,
        })

    ids = sorted(grouped)
    keys = keys_of(conn, project_id, ids)
    kinds: dict[int, str] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        kinds = {int(r["id"]): str(r["kind"]) for r in conn.execute(
            f"SELECT id, kind FROM entities WHERE project_id=? AND id IN ({marks})",
            (project_id, *ids))}
    for eid, bucket in grouped.items():
        bucket["entity_key"] = keys.get(eid)
        bucket["kind"] = kinds.get(eid)

    new = sorted((b for b in grouped.values() if b["is_new"]), key=lambda b: b["id"])
    changed = sorted((b for b in grouped.values() if not b["is_new"]),
                     key=lambda b: b["id"])
    # 截断**必须写进输出**：一个被截过的列表和"就这么些"长得一模一样。
    shown_new = new[:DIFF_ENTITY_LIMIT]
    shown_changed = changed[:max(0, DIFF_ENTITY_LIMIT - len(shown_new))]
    truncated = len(new) + len(changed) - len(shown_new) - len(shown_changed)

    notes = []
    if accuracy.get("unknown"):
        notes.append(
            f"{accuracy['unknown']} 条事实的 observed_at 精度是 unknown（§8.1：它"
            f"不得被当作 exact 参与时间判断）。本命令按**作业归属**判定增量，"
            f"因此不受影响；但不要把这里的 observed_at 当成可排序的时刻。")
    if truncated:
        notes.append(f"实体数超过 {DIFF_ENTITY_LIMIT}，本次只列出前 "
                     f"{len(shown_new) + len(shown_changed)} 个（截断 {truncated} 个）。")

    data = {
        "since": since,
        "entities": {"new": shown_new, "changed": shown_changed},
        "counts": {"new": len(new), "changed": len(changed), "facts": len(rows),
                   "entities_truncated": truncated},
        "time_accuracy": accuracy,
        "notes": notes,
    }
    human = [f"自 {since['at']}（{since['spec']}"
             + (f" = job {since['job_id']}" if since["job_id"] else "") + "）以来：",
             f"  新增实体 {len(new)}，变化实体 {len(changed)}，贡献 {len(rows)} 条"]
    for label, bucket_list in (("新增", shown_new), ("变化", shown_changed)):
        for bucket in bucket_list:
            human.append(f"  [{label}] #{bucket['id']} {bucket['entity_key']}"
                         f"  ({len(bucket['facts'])} 条)")
    for note in notes:
        human.append(f"  ⚠ {note}")
    return data, human


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args, ctx) -> tuple[dict, list[str]]:
    """数据体检。**复用数据层的判断，一条都不在这里重写。**

    用到的五样，全部来自 `magpie/core`：

      * `store.integrity_check` —— INV-03/07/08/15 + 缓存一致性，它内部读的
        正是 `v_integrity_violations`（所以那一条不会被漏掉，也不会被抄第二遍）；
      * `store.orphan_subjects` —— 从未有过任何贡献的实体（§2.3：不得自动清理）；
      * `store.purge_eligible` —— GC 判定（默认 `purge == []`，这是 v3.2-r2 的
        有意语义，不是"还没实现"）；
      * `ops.explain_job_scope` —— 每个作业**按它当时的 scope** 解释。

    在 CLI 里重写这些判断的代价不是"多写几行"，而是**两个真相**：体检报告与
    数据层自己的断言会分叉，而分叉之后"哪个对"没有答案。

    **退出码与 `data.healthy` 的分工**（这里刻意不把"数据不健康"变成退出码 2）：
    `/api/v1/doctor` 在数据不健康时仍然返回 **200**，而退出码表要求与 HTTP 状态
    码一一对应 —— 所以 CLI 同样退出 0，健康与否写在 `data.healthy` 里。
    脚本要的是 `magpie doctor --json` 里的那个布尔值，不是一个粗粒度的数字。
    """
    conn: sqlite3.Connection = ctx.conn()
    report = store.integrity_check(conn)
    violations = report["violations"]
    by_kind: dict[str, int] = {}
    for item in violations:
        name = str(item.get("violation"))
        by_kind[name] = by_kind.get(name, 0) + 1

    orphans = store.orphan_subjects(conn)
    purge = store.purge_eligible(conn)

    jobs = [dict(r) for r in conn.execute(
        "SELECT id, project_id, kind, state, applied, started_at, finished_at"
        " FROM jobs ORDER BY id")]
    for job in jobs:
        try:
            job["scope"] = ops.explain_job_scope(conn, int(job["id"]))
        except Exception as exc:                       # noqa: BLE001
            # 一个 job 的解释坏了不该让整份体检报告坏掉 —— 但**不能静默**，
            # 它自己就是一条体检结论。
            job["scope"] = {"error": f"{type(exc).__name__}: {exc}"}

    counts = {
        "projects": _scalar(conn, "SELECT COUNT(*) FROM projects"),
        "entities": _scalar(conn, "SELECT COUNT(*) FROM entities WHERE is_current=1"),
        "observations": _scalar(conn,
                                "SELECT COUNT(*) FROM observations WHERE is_current=1"),
        "relations": _scalar(conn, "SELECT COUNT(*) FROM relations WHERE is_current=1"),
        "jobs": len(jobs),
        "open_inbox": _scalar(conn, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"),
    }
    healthy = bool(report["ok"]) and not orphans and not purge["purge"]
    data = {
        "healthy": healthy,
        "sqlite_integrity": report["sqlite_integrity"],
        "invariants_ok": bool(report["ok"]),
        "violations": violations,
        "violations_by_kind": by_kind,
        "foreign_key_check": report["foreign_key_check"],
        "orphan_subjects": orphans,
        "purge_eligible": purge,
        "jobs": jobs,
        "counts": counts,
    }
    human = [f"{'健康' if healthy else '**不健康**'}",
             f"  sqlite integrity_check：{report['sqlite_integrity']}",
             f"  不变量违例：{len(violations)} 条"
             + (f"  {by_kind}" if by_kind else ""),
             f"  外键检查：{len(report['foreign_key_check'])} 条",
             f"  孤儿实体：{len(orphans)} 个",
             f"  可清理（purge）：{len(purge['purge'])} 个 / 保留 {len(purge['retained'])} 个",
             f"  计数：{counts}",
             "作业："]
    human += _tbl(["ID", "KIND", "STATE", "APPLIED", "STARTED", "SCOPE"],
                  [[j["id"], j["kind"], j["state"], j["applied"], j["started_at"],
                    ("error" if "error" in j["scope"]
                     else j["scope"].get("interpret_with"))] for j in jobs])
    return data, human


def _scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

#: 实体层对外的字段（`ls` 可筛、`show` 可见的那些）。**静态声明**：
#: 它们由 schema 决定，不因库里有没有数据而变。
ENTITY_FIELDS = (
    {"name": "id", "type": "integer", "filterable": False,
     "note": "`show <id>` 直接收它"},
    {"name": "entity_key", "type": "string", "filterable": True,
     "filter": "ls --q <片段>（子串匹配）",
     "note": "规范 key；`show <key>` 直接收它。展示名一律走 store.display_key"},
    {"name": "kind", "type": "enum", "filterable": True, "filter": "ls --kind",
     "note": "取值见本输出的 kinds；未登记的 kind 写不进库（003 的拒绝触发器）"},
    {"name": "lifecycle", "type": "enum", "filterable": True, "filter": "ls --lifecycle",
     "values": ["active", "retired"]},
    {"name": "tag", "type": "string", "filterable": True, "filter": "ls --tag"},
    {"name": "first_seen", "type": "iso8601", "filterable": False,
     "note": "实体创建时刻；只在创建时写一次"},
    {"name": "last_seen", "type": "iso8601", "filterable": False,
     "note": "⚠ 同样只在创建时写一次 —— 不要拿它当'最后活跃时间'"},
    {"name": "observation_count", "type": "integer", "filterable": False},
    {"name": "out_degree", "type": "integer", "filterable": False},
)

#: 观测层的字段命名空间。`semantic_field` 是自由文本（由解析器决定），
#: 所以本输出会**按项目列出库里实际出现过**的取值，而不是凭空声明一份。
OBSERVATION_FIELDS_NOTE = (
    "semantic_field 由解析器产出，没有一份静态词表。下面列出的是**本库实际"
    "出现过**的取值 —— 它是事实，不是声明。要加字段就加一个解析器。"
)


def _query_syntax(parser) -> dict:
    """从**真的那个 parser** 生成命令语法。

    一份手写的语法文档会在第三次改命令时过期，而一个过期的语法文档比没有更坏
    （agent 会照着它拼参数）。直接从 argparse 的注册表读，两边就不可能分叉。
    """
    commands: dict[str, Any] = {}
    for action in getattr(parser, "_actions", []):
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):                      # 只看 subparsers
            continue
        for name, sub in choices.items():
            options, positionals = [], []
            for sub_action in getattr(sub, "_actions", []):
                if isinstance(sub_action, argparse._SubParsersAction):
                    continue                       # 嵌套的子命令不在本步的合同里
                entry = {"help": (sub_action.help or "").strip() or None}
                if not sub_action.option_strings:               # 位置参数
                    entry["name"] = sub_action.dest
                    positionals.append(entry)
                    continue
                entry["flag"] = "/".join(sub_action.option_strings)
                entry["metavar"] = sub_action.metavar
                if sub_action.choices:
                    entry["choices"] = list(sub_action.choices)
                if sub_action.default not in (None, False, argparse.SUPPRESS):
                    entry["default"] = sub_action.default
                options.append(entry)
            commands[name] = {"help": (sub.description or "").strip() or None,
                              "options": options, "positionals": positionals}
    return {
        "program": parser.prog,
        "commands": commands,
        "envelope": {
            "success": {"ok": True, "data": "…",
                        "meta": {"db": "<绝对路径>", "took_ms": "<整数>",
                                 "schema_version": "<整数>"}},
            "failure": {"ok": False,
                        "error": {"code": "E_…", "message": "…", "hint": "…"}},
            "note": "错误信封**没有 meta** —— 与 /api/v1 逐字同形。"
                    "分叉请按 error.code，不要按 message 文本。",
        },
        "exit_codes": {str(code): {"meaning": EXIT_MEANING[code],
                                   "http": EXIT_TO_HTTP[code]}
                       for code in sorted(EXIT_TO_HTTP)},
        "json_output": "--json 是**默认**；--human 才切成人读表格。"
                       "JSON 输出是纯 ASCII（ensure_ascii），"
                       "所以不设 PYTHONIOENCODING 的消费者也能安全读走。",
    }


def cmd_schema(args, ctx) -> tuple[dict, list[str]]:
    """一条命令吐出类型、关系动词、字段与查询语法（§6.3）。**agent 无需读源码。**"""
    conn: sqlite3.Connection = ctx.conn()
    kinds = [{"kind": str(r["kind"]), "lens": str(r["lens"]),
              "identity_rule": str(r["identity_rule"]),
              "fixture_path": str(r["fixture_path"]),
              "produced_by": _json_or_none(r["produced_by"]) or []}
             for r in conn.execute(
                 "SELECT kind, lens, identity_rule, fixture_path, produced_by"
                 " FROM kind_registry ORDER BY kind")]
    verbs = [{"verb": str(r["verb"]), "inverse": r["inverse"],
              "description": str(r["description"])}
             for r in conn.execute(
                 "SELECT verb, inverse, description FROM relation_vocabulary"
                 " ORDER BY verb")]

    notes: list[str] = []
    observed: dict[str, list[dict]] = {}
    project_spec = args.project
    try:
        project_id = resolve_project(conn, project_spec)
        for row in conn.execute(
                "SELECT e.kind AS kind, o.semantic_field AS field,"
                " o.value_kind AS value_kind, COUNT(*) AS n"
                " FROM observations o JOIN entities e ON e.id = o.entity_id"
                " WHERE o.project_id=? AND o.is_current=1"
                " GROUP BY 1, 2, 3 ORDER BY 1, 2", (project_id,)):
            observed.setdefault(str(row["kind"]), []).append(
                {"field": str(row["field"]), "value_kind": str(row["value_kind"]),
                 "count": int(row["n"])})
    except CliError as exc:
        if exc.code != "E_PROJECT_REQUIRED":
            raise
        notes.append("库里有 0 个或多个项目，未指定 --project —— 观测字段"
                     "（本库实际出现过的）本次为空。加上 --project 再跑一次。")
    if not observed and not notes:
        notes.append("本项目当前没有任何观测 —— 观测字段列表因此为空。"
                     "这是结果，不是错误。")

    data = {
        "schema_version": ctx.db.schema_version,
        "kinds": kinds,
        "relation_verbs": verbs,
        "fields": {"entity": list(ENTITY_FIELDS),
                   "observation": observed,
                   "observation_note": OBSERVATION_FIELDS_NOTE},
        "query_syntax": _query_syntax(ctx.parser),
        "notes": notes,
    }
    human = [f"schema 版本 {data['schema_version']}",
             "", "资源类型（kind_registry）："]
    human += _tbl(["KIND", "LENS", "PRODUCED_BY"],
                  [[k["kind"], k["lens"], ",".join(k["produced_by"])] for k in kinds])
    human += ["", "关系动词（relation_vocabulary）："]
    human += _tbl(["VERB", "INVERSE"], [[v["verb"], v["inverse"]] for v in verbs])
    human += ["", "实体字段："]
    human += _tbl(["NAME", "TYPE", "可筛", "说明"],
                  [[f["name"], f["type"], f.get("filter") or "—",
                    (f.get("note") or "")[:40]] for f in ENTITY_FIELDS])
    human += ["", "观测字段（本库实际出现）："]
    human += _tbl(["KIND", "FIELD", "VALUE_KIND", "N"],
                  [[k, f["field"], f["value_kind"], f["count"]]
                   for k, fields in sorted(observed.items()) for f in fields])
    for note in notes:
        human.append(f"  ⚠ {note}")
    return data, human


#: 命令名 → 实现。`__init__.py` 按它分发；**没有登记的命令不会被执行**。
RUNNERS = {
    "init": cmd_init,
    "ls": cmd_ls,
    "show": cmd_show,
    "search": cmd_search,
    "diff": cmd_diff,
    "doctor": cmd_doctor,
    "schema": cmd_schema,
}
