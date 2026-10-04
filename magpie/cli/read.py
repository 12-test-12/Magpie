"""只读命令的实现：`init` / `ls` / `lens` / `show` / `search` / `diff` / `doctor` / `schema` / `risk`。

**这个模块里没有一条 SQL —— 一条都没有。** 这不是风格问题：2.2 起这里记着一笔
明写的债（`ls` / `show` 的查询语句与 `api/routes.py` 里的那几条是**同一份语义的
两份实现**），3.5 把债还掉了：查询全部搬到 `magpie/core/query.py`，这个文件与
`api/routes.py` 都只**调用**它。约束不靠自觉 —— `check.py` 的 `SQL-FREE` 红线
静态扫这两个文件，出现 SQL 字面量即整轮变红。

本模块仍然只做三件事：

  * 命令行参数 → `query` 的调用（**过滤条件不许被静默忽略**，见
    `query.reject_unsupported`）；
  * `query` 的结果 → 人读表格（`--human`）或原样进信封（`--json`）；
  * `QueryError` → `CliError`（`as_cli_error`）。core 不认识退出码，CLI 不认识
    `E_BAD_CIDR` 该是几号退出码 —— 映射表两边各写一份，`code` 只有 core 一份。

与 2.7 的关系（LARI-8《排序变更记录②》的裁定）
----------------------------------------------
**契约以 2.7 的 `openapi.json` 为准，本模块适配它，不重新定义它。** 具体到三点:

  * 信封逐字同形（见 `envelope.py`）；
  * 过滤条件、分页游标、404/409 的分档与 `/api/v1` 一致 —— 包括"不支持的
    过滤条件必须**响亮拒绝**而不是静默忽略"这条；
  * `search` **故意不实现**。2.7 的 `/search` 返回 501（搜索 DSL 属于 §8），
    若 CLI 自己发明一套查询语义，就出现了"两条路、一份数据、两种答案"。
    所以这里也 501（`E_NOT_IMPLEMENTED`），把 agent 引向 core，而不是给它
    一个假答案。**具名查询不是 DSL**：`ls --product nginx@1.24.0` 与
    `GET /entities?product=nginx@1.24.0` 是同一个 `query.dispatch` 的同一次调用，
    这正是 §3.5 要的"GUI 与 CLI 共用同一实现"。

`init` 仍然是本模块唯一的写，而且只写 schema
--------------------------------------------
`ls` 不能顺手建库 —— 那样"库在哪、谁建的"就没有答案了。建库必须是一个**显式的、
可以被记一笔的**动作。它连一个 project 都不建：项目的创建是写路径（2.6 / GUI）的
事，所以 `init` 之后 `magpie ls` 会诚实地告诉你"一个项目都没有"，而不是替你造一个。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

from ..core import export, ingest, ops, query, risk, store
from ..core import scope as scope_module
from ..core.parsers.base import extras_install_hint
from . import write
from .ask import cmd_ask
from .db import Database, resolve_project
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

#: `ls` 支持的过滤条件 —— **实现那一侧的唯一声明处**是 `query.ENTITY_FILTERS`
#: （报错信息也从它来）。这里再导出一次是为了让老的 `from .read import
#: ENTITY_FILTERS` 照旧可用；本步之后它不该再被新增的代码引用。
ENTITY_FILTERS = query.ENTITY_FILTERS

#: `diff` 一次最多列多少个实体。**到顶要写进输出**（`truncated`），
#: 静默截断看起来和"就这么些"一模一样。
DIFF_ENTITY_LIMIT = 200

#: 页大小与项目解析住在 core / db 里，这里只做转发（见模块文档）。
MAX_PAGE = query.MAX_PAGE
DEFAULT_PAGE = query.DEFAULT_PAGE

#: `QueryError.code` → 退出码。**只有这里映射**：core 给的是稳定的 code，
#: 退出码是 CLI 这一侧的契约（`envelope.EXIT_TO_HTTP` 要求它与 HTTP 状态一一
#: 对应，所以 `E_LENS_NOT_FOUND` 必须落在 2 ↔ 404，而不是 1 ↔ 400）。
#: 没登记的 code 一律按**数据错**处理：宁可给一个偏保守的退出码，也不要让它
#: 变成 0（成功）—— 一个"查不动却退出 0"的命令会被脚本当成"查过了，没有"。
QUERY_EXIT: dict[str, int] = {
    "E_UNSUPPORTED_FILTER": EXIT_USAGE,
    "E_BAD_LIMIT": EXIT_USAGE,
    "E_BAD_CURSOR": EXIT_USAGE,
    "E_BAD_PRODUCT": EXIT_USAGE,
    "E_BAD_ENDPOINT": EXIT_USAGE,
    "E_BAD_CIDR": EXIT_USAGE,
    "E_BAD_STATE": EXIT_USAGE,
    "E_BAD_DEPTH": EXIT_USAGE,
    # 0.19.0：under_domain 的目标不是域名 —— 问法错（1 ↔ 400），改参数就能好。
    "E_NOT_A_DOMAIN": EXIT_USAGE,
    "E_LENS_NOT_FOUND": EXIT_DATA,
}


def as_cli_error(exc: query.QueryError) -> CliError:
    """`QueryError` → `CliError`。**唯一 的映射点**（见 `QUERY_EXIT`）。"""
    return CliError(exc.code, exc.message, hint=exc.hint,
                    exit_code=QUERY_EXIT.get(exc.code, EXIT_DATA), **exc.details)


# ---------------------------------------------------------------------------
# 公共零件（渲染用；取数一律走 `magpie.core.query`）
# ---------------------------------------------------------------------------


def _tbl(header: list[str], rows: list[list[Any]], empty: str = "（无）") -> list[str]:
    """空表时只回一句 `（无）`。

    `table()` 在空 rows 上只回表头 —— 那在终端里长得像"有个表，只是没打印出来"，
    而"这里是空的"与"这里没打印"是两件事。
    """
    return table(header, rows) if rows else [f"  {empty}"]


# `resolve_project` 住在 `db.py`（"库在哪、项目是哪个"是同一件事的两半），
# 在这里**再导出**一次只是为了让老的 `from .read import resolve_project` 照旧
# 可用。它服务**读命令与写命令两侧**（`write.py` 也用它），所以它没有跟着
# 查询一起搬进 core —— 那是 `query` 的本步边界，写在 `core/query.py` 的模块
# 文档里：core 提供"怎么找"（`query.project_id_for`），"找不到时说什么"由各
# 入口自己决定（退出码 2 ↔ HTTP 404）。


# ---------------------------------------------------------------------------
# init —— 本模块唯一的写，而且只写 schema
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
        try:
            version = query.probe_schema_version(db.path)
        except query.QueryError as exc:
            raise as_cli_error(exc) from exc
        pending = [m for m in migrate.MIGRATIONS if m.version > version]
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

    projects = query.projects(conn)
    data = {
        "db": db.display,
        "created": not existed,
        "applied": applied,
        "schema_version": db.schema_version,
        "projects": projects,
    }
    human = [
        f"{'建库' if not existed else '升级'}：{db.display}",
        f"schema 版本：{db.schema_version}"
        + (f"（本次应用 {len(applied)} 个迁移："
           f"{', '.join('v%d %s' % (m['version'], m['name']) for m in applied)}）"
           if applied else "（已是最新，未做任何改动）"),
        f"项目数：{len(projects)}"
        + ("（空库里一个项目都没有 —— 建项目是 2.6 写路径的事）"
           if not projects else ""),
    ]
    return data, human


# ---------------------------------------------------------------------------
# ls —— 列表，也是**所有具名查询的入口**（见 core/query.py 的模块文档）
# ---------------------------------------------------------------------------


#: `ls` 的表头。**具名查询共用它** —— 无论问的是"谁在跑 nginx"还是"高危漏洞
#: 有哪些"，回的都是同一张 entity 列表（`query.entities_page` 的那个形状）。
_LS_COLUMNS = ["ID", "KIND", "KEY", "LIFECYCLE", "OBS", "OUT", "FIRST_SEEN"]


def _ls_rows(items: list[dict]) -> list[list[Any]]:
    return [[i["id"], i["kind"], i["entity_key"], i["lifecycle"],
             i["observation_count"], i["out_degree"], i["first_seen"]]
            for i in items]


def cmd_ls(args, ctx) -> tuple[dict, list[str]]:
    """实体列表。键集游标分页 —— 与 `/api/v1/entities` 同一套语义、同一段实现。

    `--product` / `--endpoint` / `--severity` / `--state` / `--in-cidr` 是 §3.5
    的**具名查询**，不是一套新语法：它们与 `GET /entities?…` 的同名参数一字不差，
    两边都从 `query.dispatch` 进（那一个函数决定"这组条件在问哪一问"）。
    """
    query.reject_unsupported({"review_status": args.review_status,
                              "in_scope": args.in_scope})
    conn = ctx.conn()
    try:
        # 用法错**先于**数据错报出来（见 `query.check_page_filters`）：在空库上
        # 把 `--limit 0` 报成"必须显式给 --project"会把用户引向错误的方向。
        query.check_page_filters(limit=args.limit, cursor=args.cursor)
        project_id = resolve_project(conn, args.project)
        result = query.dispatch(
            conn, project_id, kind=args.kind, lens=args.lens, q=args.q, tag=args.tag,
            lifecycle=args.lifecycle, product=args.product, version=args.version,
            endpoint=args.endpoint, severity=args.severity, state=args.state,
            in_cidr=args.in_cidr, cursor=args.cursor, limit=args.limit)
    except query.QueryError as exc:
        raise as_cli_error(exc) from exc

    items = result["entities"]
    # `notes` 与 `query` 一起进输出：一个空列表必须带着"为什么是空的"（产品
    # 实体根本没有？网段里的候选是算出来的？）—— 少了它，空结果会被读成
    # "筛过了，就是没有"。
    data = {"query": result["query"], "entities": items, "count": len(items),
            "filters": result["filters"], "notes": result["notes"]}
    human = _tbl(_LS_COLUMNS, _ls_rows(items),
                 empty="（本项目当前没有符合条件的实体 —— 这是结果，不是错误）")
    if result["next_cursor"]:
        human.append(f"… 还有下一页：--cursor {result['next_cursor']}")
    for note in result["notes"]:
        human.append(f"  ⚠ {note}")
    return data, human


# ---------------------------------------------------------------------------
# lens —— 镜头页的类型徽标与计数（↔ GET /lenses/{lens}）
# ---------------------------------------------------------------------------


def cmd_lens(args, ctx) -> tuple[dict, list[str]]:
    """镜头：这个镜头下每个 kind 的类型徽标与计数。

    GUI 的镜头页用它渲染左侧的类型面板与数量徽标；CLI 与它共用 `query.lens_view`
    那一段实现（§3.5）。不给 `<镜头>` 时列出**库里现在能问的**全部镜头 ——
    清单来自 `kind_registry.lens`，不来自一张写死的表：一个还没有 kind 的镜头
    （比如还没有解析器产出的 `traffic`）不是一个能问出东西的镜头，它不该在
    清单里假装存在。
    """
    conn = ctx.conn()
    project_id = resolve_project(conn, args.project)
    try:
        if args.lens:
            view = query.lens_view(conn, project_id, args.lens)
            human = [f"镜头 {view['lens']}（{sum(k['count'] for k in view['kinds'])} 个实体）："]
            human += _tbl(["KIND", "计数", "PRODUCED_BY"],
                          [[k["kind"], k["count"], ",".join(k["produced_by"]) or "—"]
                           for k in view["kinds"]],
                          empty="（这个镜头下没有任何 kind —— 这是结果，不是错误）")
            return view, human
        lenses = query.lens_ids(conn)
        overview = [query.lens_view(conn, project_id, lens) for lens in lenses]
    except query.QueryError as exc:
        raise as_cli_error(exc) from exc

    data = {"query": "lens", "lenses": overview}
    human = ["镜头（由 kind_registry.lens 决定）："]
    human += _tbl(["LENS", "KIND", "计数"],
                  [[view["lens"], k["kind"], k["count"]]
                   for view in overview for k in view["kinds"]],
                  empty="（库里一个 kind 都没有 —— 检查迁移是否应用过）")
    return data, human


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _not_found(text: str) -> CliError:
    return CliError(
        "E_ENTITY_NOT_FOUND", f"没有 id 为 {text} 的实体，也没有 key 为 {text!r} 的实体",
        exit_code=EXIT_DATA,
        hint="key 是**规范写法**（normalize 之后）：域名小写去尾点、"
             "IDN 转 punycode、服务是 host:port。`magpie ls --q <片段>` 可以模糊找。")


def _resolve_ref(conn, project_id: int, ref: str) -> int:
    """`<id|entity_key>` → entity_id。

    **歧义必须交给人，不能替人挑一个**（退出码 3 存在的意义）。

    歧义从哪里来 —— 这里要写准，因为一开始我以为它来自 alias 表：**不是**，
    生效 alias 的 key 在库内唯一（见 `query.target_entity_id`）。真正的歧义是
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
    by_key = query.target_entity_id(conn, project_id, text)

    if not text.isdigit():
        if by_key is None:
            raise _not_found(text)
        return by_key

    by_id = int(text)
    exists_id = query.entity_exists(conn, project_id, by_id)

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


def cmd_show(args, ctx) -> tuple[dict, list[str]]:
    """实体详情（观测 / 关系 / 标签 / Inbox / 时间线）。形状与 `/entities/{id}` 同义。

    两边调的是**同一个** `query.entity_detail` —— 这正是 §3.5 要的那件事：此前
    这一段在 CLI 与 API 里各写了一遍，两份的字段恰好一致纯属当时的运气。
    """
    conn = ctx.conn()
    project_id = resolve_project(conn, args.project)
    entity_id = _resolve_ref(conn, project_id, args.ref)
    try:
        data = query.entity_detail(conn, project_id, entity_id)
    except query.QueryError as exc:
        raise as_cli_error(exc) from exc

    entity = data["entity"]
    observations, relations = data["observations"], data["relations"]
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
              f"未决 Inbox：{len(data['open_inbox'])} 条"]
    return data, human


# ---------------------------------------------------------------------------
# search —— 与 2.7 的 /search 一样，响亮地 501
# ---------------------------------------------------------------------------


def cmd_search(args, ctx) -> tuple[dict, list[str]]:
    raise not_implemented(f"搜索（q={args.query!r}）", plan="计划书 §8（查询与搜索）")


# ---------------------------------------------------------------------------
# diff —— 自某个作业起，库里多了什么
# ---------------------------------------------------------------------------


def _resolve_since(conn, project_id: int, spec: str) -> dict:
    """`<job_id|ISO|last>` → 一个时间点 + 它是怎么来的。

    **口径**：`--since` 是"从那一刻起"。`last` = 本项目的最后一个作业，
    判定用的是 `jobs.started_at`（见 `query.last_job`：一次导入的多个 job 的
    `started_at` 几乎相同，所以得到的是**这一批导入**带来的全部变化）。

    刻意**不**用 `entities.first_seen / last_seen` 做增量判据 —— 那两列只在
    实体创建时写一次，之后没有任何地方更新它们。作业归属（`contributions.job_id`）
    才是模型里**真的存在**的事实。
    """
    text = str(spec)
    try:
        if text.isdigit():
            at = query.job_started_at(conn, project_id, int(text))
            return {"spec": text, "at": at, "job_id": int(text)}
        if text == "last":
            found = query.last_job(conn, project_id)
            if found is None:
                return {"spec": "last", "at": None, "job_id": None}
            job_id, at = found
            return {"spec": "last", "at": at, "job_id": job_id}
    except query.QueryError as exc:
        raise as_cli_error(exc) from exc
    if store.parse_time(text) is None:
        raise CliError(
            "E_BAD_SINCE",
            f"--since 只收作业 id / ISO 时间 / 'last'，收到 {text!r}",
            exit_code=EXIT_USAGE,
            hint="例：`--since last`、`--since 3`、`--since 2026-09-18T00:00:00Z`。",
            accepted=["<job_id>", "<ISO8601>", "last"])
    return {"spec": text, "at": text, "job_id": None}


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
    conn = ctx.conn()
    project_id = resolve_project(conn, args.project)
    since = _resolve_since(conn, project_id, args.since)

    empty = {"since": since, "entities": {"new": [], "changed": []},
             "counts": {"new": 0, "changed": 0, "facts": 0, "entities_truncated": 0},
             "time_accuracy": {"exact": 0, "approximate": 0, "unknown": 0},
             "notes": []}
    if since["at"] is None:
        empty["notes"].append("本项目还没有任何作业 —— 没有可比的基准。")
        return empty, ["（本项目还没有任何作业，无从 diff）"]

    rows = query.changes_since(conn, project_id, since["at"])

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
    keys = query.keys_of(conn, project_id, ids)
    kinds = query.kinds_by_ids(conn, project_id, ids)
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
      * `ingest.parser_drift_report` —— 已登记的解析器指纹 vs 当前实现（2.6）；
      * `ops.explain_job_scope` —— 每个作业**按它当时的 scope** 解释。

    **2.6 增补的三项导入体检**（`data.imports`）:

      * `unknown_fields` / `bad_lines` —— §5.6 的两个格式漂移信号。它们**只在
        解析当时可见**，所以判据是 `<data-dir>/reports/` 下留下的东西：坏行明细
        （`.badlines.txt`，导入路径无条件写的）与每文件的机器可读报告
        （`<job_id>.json`，导入路径在 2.6 起写）。**没有留下报告的作业就统计不到**
        —— 这一条写在 `imports.notes` 里，而不是让空数组冒充"没有漂移"。
      * `parser_drift` —— 见 `ingest.parser_drift_report`。

    一个刻意的取舍：这三项都不进 `healthy`。`healthy` 是**数据坏没坏**的判断
    （不变量 + 孤儿 + GC），而格式漂移是"某个上游工具可能改了输出"—— 那是一个
    **要你去看一眼**的信号，不是一条数据损坏。把两者混进同一个布尔，会让
    "healthy=false"同时意味着"库坏了"和"某天 httpx 加了个字段"，于是没人
    再认真对待它。

    在 CLI 里重写这些判断的代价不是"多写几行"，而是**两个真相**：体检报告与
    数据层自己的断言会分叉，而分叉之后"哪个对"没有答案。

    **退出码与 `data.healthy` 的分工**（这里刻意不把"数据不健康"变成退出码 2）：
    `/api/v1/doctor` 在数据不健康时仍然返回 **200**，而退出码表要求与 HTTP 状态
    码一一对应 —— 所以 CLI 同样退出 0，健康与否写在 `data.healthy` 里。
    脚本要的是 `magpie doctor --json` 里的那个布尔值，不是一个粗粒度的数字。
    """
    conn = ctx.conn()
    report = store.integrity_check(conn)
    violations = report["violations"]
    by_kind: dict[str, int] = {}
    for item in violations:
        name = str(item.get("violation"))
        by_kind[name] = by_kind.get(name, 0) + 1

    orphans = store.orphan_subjects(conn)
    purge = store.purge_eligible(conn)

    jobs = query.all_jobs(conn)
    for job in jobs:
        try:
            job["scope"] = ops.explain_job_scope(conn, int(job["id"]))
        except Exception as exc:                       # noqa: BLE001
            # 一个 job 的解释坏了不该让整份体检报告坏掉 —— 但**不能静默**，
            # 它自己就是一条体检结论。
            job["scope"] = {"error": f"{type(exc).__name__}: {exc}"}

    counts = query.metric_counts(conn)
    imports = _import_health(ctx)

    # 体检页的六类（§7.4）。**与 `/doctor` 逐字同义的选择**：项目解析不了时
    # 不报错，而是**少算这一项**，且那一项在输出里不出现（不是 0）——
    # "这个库有 2 个项目而你一个都没指定"与"这个项目一类问题都没有"是两件事，
    # 让后者去冒充前者，正是体检页最不该犯的错。
    health = None
    health_skipped = None
    try:
        health = query.health_report(conn, resolve_project(conn, getattr(args, "project", None)))
    except CliError as exc:
        health_skipped = f"{exc.code}：{exc.message}"

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
        "imports": imports,
    }
    if health is not None:
        data["health"] = health
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
    human += [""]
    if health is not None:
        # 六类问题（§7.4 的体检页）。数字与 GUI **同一份实现**：两处都只是
        # `query.health_report` 的一次调用，"两边给出不同的数"因此不可能。
        human.append(f"六类问题（项目 {health['project_id']}，未决 " +
                     f"{health['open_inbox']} 条）：")
        for category in health["categories"]:
            human.append(f"  {category['label']}：{category['count']} 条"
                         f"（{' / '.join(category['kinds'])}）")
        for note in health["notes"]:
            human.append(f"  ⚠ {note}")
    else:
        human.append(f"六类问题：未计算（{health_skipped}）")
    human += ["", "导入体检（2.6）：",
              f"  解析器漂移：{len(imports['parser_drift'])} 项"
              + (f"  {[d['drift'] for d in imports['parser_drift']]}" if imports["parser_drift"] else ""),
              f"  坏行：{len(imports['bad_lines'])} 个作业有明细，共 {imports['bad_lines_total']} 条",
              f"  未知字段：{len(imports['unknown_fields'])} 个名字"
              + (f"  {imports['unknown_fields']}" if imports["unknown_fields"] else ""),
              f"  报告目录：{imports['reports_dir']}（{len(imports['reports'])} 份报告）"]
    for note in imports["notes"]:
        human.append(f"  ⚠ {note}")
    return data, human


def _import_health(ctx) -> dict:
    """2.6 增补的三项导入体检。见 `cmd_doctor` 的 docstring。

    全部是**只读**：一个 SQL 常量在 `parser_drift_report` 里（core 的判断），
    其余是读 `<data-dir>/reports/` 下的文件。体检不该有副作用 —— 一条顺手写了
    点什么的 doctor 会让"体检前后逐字节不变"那条断言失效，而那条断言正是
    `test_read_commands_do_not_touch_a_byte` 在守的东西。
    """
    from .db import resolve_data_dir

    conn = ctx.conn()
    reports_dir = resolve_data_dir(getattr(ctx.args, "data_dir", None)) / "reports"
    drift = ingest.parser_drift_report(conn)

    bad_lines: list[dict] = []
    reports: list[dict] = []
    unknown: dict[str, int] = {}
    notes: list[str] = []

    if reports_dir.is_dir():
        for path in sorted(reports_dir.glob("*.badlines.txt")):
            total, kept = _badlines_header(path)
            bad_lines.append({"job_id": _int_or_none(path.stem), "bad_lines": total,
                              "kept": kept, "report": str(path)})
        for path in sorted(reports_dir.glob("*.json")):
            item = _read_report(path)
            if item is not None:
                reports.append(item)
                for name, count in (item.get("unknown_fields_detail") or {}).items():
                    unknown[str(name)] = unknown.get(str(name), 0) + int(count)
    else:
        notes.append(f"数据区里没有解析报告目录（{reports_dir}）—— 还没有导入过，"
                     f"或者导入走的是不落报告的入口。")

    if reports_dir.is_dir() and not reports:
        notes.append(
            "报告目录里没有机器可读的报告（`<job_id>.json`）—— **未知字段只在解析"
            "当时可见**，没有报告就没有它的痕迹。这一项因此为空，"
            "而不是'没有漂移'。")
    covered = {int(r["job_id"]) for r in reports if r.get("job_id") is not None}
    jobs_with_fingerprint = query.jobs_with_parser_fingerprint(conn)
    return {
        "reports_dir": str(reports_dir),
        "reports": reports,
        "bad_lines": bad_lines,
        "bad_lines_total": sum(int(b["bad_lines"]) for b in bad_lines),
        "unknown_fields": dict(sorted(unknown.items())),
        "parser_drift": drift,
        # 「有报告的作业」对「用过解析器的作业」——两个数差得远就说明大部分
        # 导入没留下报告（走的不是会落报告的入口），而未知字段那一项正是
        # 靠报告才算得出来的。一个空数组不该冒充"没有漂移"。
        "reported_jobs": sorted(covered),
        "jobs_with_parser_fingerprint": jobs_with_fingerprint,
        "notes": notes,
    }


def _int_or_none(text: str) -> int | None:
    return int(text) if str(text).isdigit() else None


def _badlines_header(path) -> tuple[int, int | None]:
    """从坏行明细的头读 (真实条数, 保留条数)。

    头是 `_write_badlines` 写的：`# job N 坏行明细（共 M 条[，此处只保留前 K 条明细]）`。
    取不到就返回 (0, None) —— 一个读不懂的头不该让整份体检崩掉，但它确实是一份
    **读不懂**的明细，所以 0 是"没读到"，`kept=None` 如实表达这一点。
    """
    try:
        first = path.read_text(encoding="utf-8").splitlines()[:1]
    except OSError:                                             # pragma: no cover
        return 0, None
    if not first:
        return 0, None
    text = first[0]
    total = re.search(r"共\s*(\d+)\s*条", text)
    kept = re.search(r"只保留前\s*(\d+)\s*条", text)
    return (int(total.group(1)) if total else 0,
            int(kept.group(1)) if kept else (int(total.group(1)) if total else None))


def _read_report(path) -> dict | None:
    """读一份导入报告副本。**只取体检要用的字段**，不是把整个报告塞进体检输出。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):                                # pragma: no cover
        return None
    if not isinstance(payload, dict):
        return None
    counts = payload.get("counts") or {}
    return {
        "path": str(path),
        "job_id": payload.get("job_id"),
        "source_path": payload.get("path"),
        "state": payload.get("state"),
        "parser": payload.get("parser"),
        "definition_drift": payload.get("definition_drift"),
        "observation_at_accuracy": None,
        "counts": {"records": counts.get("records"), "added": counts.get("added"),
                   "updated": counts.get("updated"), "skipped": counts.get("skipped"),
                   "bad_lines": counts.get("bad_lines"),
                   "unknown_fields": counts.get("unknown_fields")},
        "unknown_fields_detail": dict(payload.get("unknown_fields_detail") or {}),
        "notes": list(payload.get("notes") or []),
    }


# ---------------------------------------------------------------------------
# risk —— 风险评分 + 攻击面概览（5.5，纯读侧派生）
# ---------------------------------------------------------------------------


#: `--human` 里攻击面最多铺几台主机。**JSON 不截断**（截断是给人读的取舍，
#: 不许改变机器拿到的事实）；这里 20 行之后折成一句"还有 N 台"，理由与
#: `inbox_items` 的 limit 相同：一屏铺不下的列表等于没有列表。
_RISK_HOST_LIMIT = 20


def cmd_risk(args, ctx) -> tuple[dict, list[str]]:
    """风险评分 + 攻击面概览。

    **这条命令自己不判任何东西**：分数、因子、聚合全部来自 `core/risk.overview`
    —— CLI / `GET /api/v1/risk` / GUI 三个消费者调的是同一个入口，"CLI 说 258、
    界面说 273"在结构上不可能出现。这里只做命令行该做的三件事：解析项目、
    调 core、把结果摆成人读的版式（`--human`）。

    评分的输入全是库里已有的事实（零出站，R7），不加表、不加迁移、不落盘 ——
    与 `doctor` 同款：跑前跑后库里每一个字节都不许变（`test_readonly` 钉住）。
    """
    conn = ctx.conn()
    try:
        project_id = resolve_project(conn, args.project)
        data = risk.overview(conn, project_id)
    except query.QueryError as exc:
        raise as_cli_error(exc) from exc
    return data, _risk_human(data)


def _risk_human(data: dict[str, Any]) -> list[str]:
    score = data["score"]
    surface = data["attack_surface"]
    lines = [f"风险评分 {score['total']}"
             f"（规则 v{data['rule_version']} · {data['rules_digest'][:23]}…）",
             "因子（只有非零的列出来；合计 = 总分）："]
    hit = [f for f in score["factors"] if f["count"]]
    if hit:
        lines += _tbl(["因子", "贡献", "计数", "权重", "依据（首条）"],
                      [[f["id"], f["contribution"], f["count"], f["weight"],
                        (f["evidence"][0]["label"] if f["evidence"] else "—")]
                       for f in hit])
    else:
        lines.append("  （全部因子 0 命中 —— 这是结果，不是错误：库里还没有"
                     "可计分的事实）")

    totals = surface["totals"]
    lines += ["", f"攻击面（按主机，共 {totals['hosts']} 台 / "
                  f"服务 {totals['services']} / 接口 {totals['endpoints']}）："]
    shown = surface["hosts"][:_RISK_HOST_LIMIT]
    if shown:
        lines += _tbl(["HOST", "服务", "接口", "未处置漏洞"],
                      [[row["host"], row["service_count"], row["endpoint_count"],
                        row["open_vuln_total"]] for row in shown])
        rest = len(surface["hosts"]) - len(shown)
        if rest > 0:
            lines.append(f"  … 还有 {rest} 台没铺开（--json 里是全量）")
    else:
        lines.append("  （没有暴露的 service / endpoint / 被打的主机）")
    if totals["unmapped_open_vulns"]:
        lines.append(f"  ⚠ {len(totals['unmapped_open_vulns'])} 条未处置漏洞"
                     f"没有 found_on 落点，没进任何主机行（--json 的 "
                     f"totals.unmapped_open_vulns 里）")

    facts = data["facts"]
    lines += ["", "摘要："
              f"漏洞 {facts['vuln_total']}"
              f"（severity {dict((k, v) for k, v in facts['vuln_by_severity'].items() if v)}"
              f"；state {facts['vuln_by_state']}）、"
              f"未决 Inbox {facts['inbox_open']}、未测 {facts['untested']}、"
              f"越界 {facts['out_of_scope']}、secret {facts['secrets']}"]
    return lines


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


def _command_nodes(parser) -> dict[str, Any]:
    """命令键 → 子 parser（**唯一的展平走查**，`_query_syntax` 与
    `_commands_report` 共用）。

    **嵌套的命令展平成一个带空格的键**（`project create`），与 `RUNNERS` 的键
    逐字一致。展平规则本身由 `test_schema_query_syntax_matches_the_real_parser`
    的 `set(described) == set(real_commands)` 钉住 —— 那条断言拿的是测试自己
    走一遍真 parser 的结果，两边任何一处展平得不一样，先红的就是它。
    """
    nodes: dict[str, Any] = {}

    def walk(node, prefix: str) -> None:
        for action in getattr(node, "_actions", []):
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict):                  # 只看 subparsers
                continue
            for name, sub in choices.items():
                key = f"{prefix} {name}".strip()
                nodes[key] = sub
                walk(sub, key)                                 # 再下去一层

    walk(parser, "")
    return nodes


def _query_syntax(parser) -> dict:
    """从**真的那个 parser** 生成命令语法。

    一份手写的语法文档会在第三次改命令时过期，而一个过期的语法文档比没有更坏
    （agent 会照着它拼参数）。直接从 argparse 的注册表读，两边就不可能分叉。
    """
    commands: dict[str, Any] = {}
    for key, sub in _command_nodes(parser).items():
        options, positionals = [], []
        for sub_action in getattr(sub, "_actions", []):
            if isinstance(sub_action, argparse._SubParsersAction):
                continue
            entry = {"help": (sub_action.help or "").strip() or None}
            if not sub_action.option_strings:                   # 位置参数
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
        commands[key] = {"help": (sub.description or "").strip() or None,
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


# ---------------------------------------------------------------------------
# extras —— 有哪些、装没装、怎么装（4.5 收口点）
# ---------------------------------------------------------------------------

#: 每个 extras 的**用途人话**与**探针模块名**（`packages` 逐字来自
#: `pyproject.toml`）。**唯一事实来源是 `pyproject.toml` 的
#: `[project.optional-dependencies]`** —— 名字与包的逐项一致由
#: `test_extras_registry_matches_pyproject` 钉住：改了 pyproject 不改这里，
#: 测试当场红。这里只多带 pyproject 装不下的两样：一句"这 extras 是干嘛的"
#: （要知道它干嘛只能读源码，正是 4.5 要收掉的口）和一个"装没装"的探针 ——
#: 探针按**模块**问而不是按包名（`pyyaml` 提供的模块叫 `yaml`）。
EXTRAS_REGISTRY: dict[str, dict[str, Any]] = {
    "api": {
        "packages": ["fastapi", "uvicorn", "python-multipart"],
        "modules": ("fastapi", "uvicorn", "python_multipart"),
        "purpose": "本地 API 层（FastAPI / uvicorn / multipart 表单）—— "
                   "`magpie app` 起服务用；缺它时 app 入口报可读错误，"
                   "core / 只读命令不受影响",
    },
    "api-yaml": {
        "packages": ["pyyaml"],
        "modules": ("yaml",),
        "purpose": "YAML 解析：OpenAPI YAML、指纹规则（fingerprints/*.yaml）、"
                   "管线定义 —— 只在函数体内 lazy import（R6），缺时抛 "
                   "MissingExtrasError（消息含安装命令）",
    },
    "traffic": {
        "packages": [],
        "modules": (),
        "purpose": "流量解析（HAR / tshark JSON，4.1 起）—— 当前零第三方、"
                   "刻意为空（走标准库 json）；tshark 是外部可执行（Wireshark），"
                   "pip 装不出它，不进任何 extras（缺它见 parsers/tshark.py "
                   "的 ensure_available）",
    },
    "dev": {
        "packages": ["pytest", "httpx2"],
        "modules": ("pytest", "httpx2"),
        "purpose": "开发用（pytest + httpx2）—— 跑 check.py / 单元测试；httpx2 是"
                   " starlette TestClient（api 测试）的台架依赖。两者都非运行时"
                   "依赖，缺了测试起不来，不属于运行时报错的那类",
    },
}


def _extras_report() -> dict[str, dict[str, Any]]:
    """每个 extras → 包清单 / 装没装 / 安装命令 / 用途。

    `installed` 探的是模块名，且**空模块表恒为 True**（`traffic` 零第三方 ——
    标准库环境它就绪，"没装东西"不是它的缺失状态）。探测失败（模块在
    `sys.modules` 里被置 None 之类的半残状态）一律按"没装"报 —— 与 `import`
    当场抛 `ImportError` 的判断一致，不比它更乐观。
    """
    report: dict[str, dict[str, Any]] = {}
    for name in sorted(EXTRAS_REGISTRY):
        spec = EXTRAS_REGISTRY[name]
        installed = True
        for module in spec["modules"]:
            try:
                if importlib.util.find_spec(module) is None:
                    installed = False
                    break
            except (ImportError, ValueError, AttributeError):
                installed = False
                break
        report[name] = {
            "packages": list(spec["packages"]),
            "installed": installed,
            "install": extras_install_hint(name),
            "purpose": spec["purpose"],
        }
    return report


def _commands_report(parser) -> dict:
    """每条命令 → 读 / 写分类（5.2）。**分类从代码算，不另抄一张表。**

    判据是**分发层的事实**：`magpie.cli.WRITABLE`（= `("init",) + write.
    WRITE_COMMANDS`）就是 `main` 决定开只读连接还是可写连接用的那一份，这里
    直接问它 —— 写命令在 `write.py` 登记、`init` 是 `read.py` 里唯一的写，
    `jobs` 虽然实现在 `write.py` 但不在 `WRITABLE` 里（它是读），全部按事实算。

    两个非 RUNNERS 的键单独认：

      * **父节点**（`project` 单独出现）——有子命令表的键，执行它只打印用法；
      * **服务**（`app`）—— `main` 在分发到 `RUNNERS` **之前**把它接走。
        目前恰好各一个，`test_schema_command_access_is_tied_to_dispatch` 把
        "只有这些例外"钉住：将来多出一个没登记的键，先红的是那条测试，而不是
        这里静默给它编一个分类。

    延迟导入 `WRITABLE`：包根（`__init__`）在 `read` 之后才完成初始化，
    模块顶层 import 它会撞进循环 —— 而 `cmd_schema` 运行时一切都已就位。
    """
    from . import WRITABLE

    report: dict[str, dict] = {}
    for key, sub in _command_nodes(parser).items():
        if any(isinstance(a, argparse._SubParsersAction) for a in sub._actions):
            report[key] = {
                "access": "group", "mutates_db": False,
                "note": "命令族的父节点（如 `magpie project` 单独出现）——"
                        "只打印该族的用法并退出 1，本身不执行任何操作"}
        elif key not in RUNNERS:
            report[key] = {
                "access": "service", "mutates_db": False,
                "note": "本地服务（GUI + /api/v1 与 CLI 同一份数据）——"
                        "本进程不执行一次查询，读写经 HTTP；缺 extras.api 时"
                        "响亮失败"}
        elif key.split()[0] in WRITABLE:
            report[key] = {
                "access": "write", "mutates_db": True,
                "note": "只写 schema（建库 / 应用迁移）—— 不建项目、不写事实"
                        if key == "init" else
                        "写路径：会改库（事实 / 作业 / 项目）——"
                        "干跑类开关（--dry-run）见 query_syntax 里该命令的 options"}
        else:
            report[key] = {"access": "read", "mutates_db": False}
    return report


def _enums_report(conn) -> dict:
    """枚举取值（5.2）。**每一项都从真库的 CHECK / 真代码算出，一张表不手抄。**

    词表的权威是 schema 里的 `CHECK (… IN (…))` —— 003 冻结镜头只有六个、
    011 重建 Inbox 表加了第八个 kind，这些事实由迁移本身守着；`check_enum`
    把它们从**这个库当前真的建表语句**里读出来。手抄一张静态表在下一次迁移
    那天不会红 —— 那等于把分叉埋进输出里。

    `scope` 没有 CHECK（它是求值语义，不是存储约束），所以它的三态从
    `core/scope.py` 的真代码取，并由契约测试拿 `in_scope()` 的真实行为钉住。
    """
    return {
        "lens": query.check_enum(conn, "kind_registry", "lens"),
        "lifecycle": query.check_enum(conn, "entities", "lifecycle"),
        "inbox_kind": query.check_enum(conn, "inbox_items", "kind"),
        "inbox_severity": query.check_enum(conn, "inbox_items", "severity"),
        "inbox_state": query.check_enum(conn, "inbox_items", "state"),
        "authority_level": query.check_enum(conn, "sources", "authority_level"),
        "storage_state": query.check_enum(conn, "raw_files", "storage_state"),
        "custody": query.check_enum(conn, "raw_files", "custody"),
        "scope": {
            "evaluated_kinds": list(scope_module.SCOPE_KINDS),
            "verdicts": {"in_scope": True, "out_of_scope": False,
                         "not_applicable": None},
            "note": (
                "scope 求值是三态：true=在范围内、false=越界、null=不问 —— "
                "只有资产类 kind（evaluated_kinds）且项目配了 scope 才问得出"
                "答案。null 不是越界：体检的越界计数与 Inbox 的 scope_drift "
                "都只收 false。include 是白名单（空=无约束）、exclude 优先；"
                "模式支持 '*.example.com'（含 apex 自己）与 '10.0.0.0/24'"
                "（网段包含）。"),
        },
    }


def _parsers_report() -> dict:
    """可用解析器（5.2）。清单与 `/api/v1/meta` **共用** `ingest.parser_catalog()`。

    这一条是审计里"不读源码就不可能知道"的典型：`import --parser` 的帮助文本
    写着"取值见 `magpie schema`"，而 schema 此前根本没有解析器清单 —— 一个
    指向空处的指引，比没有指引更费时间。
    """
    tools, source = ingest.parser_catalog()
    return {
        "source": source,
        "tools": tools,
        "note": "`magpie import --parser <tool>` 按 tool 匹配；不给时嗅探仲裁"
                "自动选，仲裁不出结果的文件进 Inbox 待人工裁决（退出码 3），"
                "不猜。指纹按 tool@format_version 记。",
    }


def cmd_schema(args, ctx) -> tuple[dict, list[str]]:
    """一条命令吐出类型、关系动词、字段与查询语法（§6.3）。**agent 无需读源码。**

    还有几样东西也在这里（每一样存在的理由相同：此前要知道它只能读源码）：

      * **具名查询的清单**（`query.NAMED_QUERIES`）—— agent 据此知道"哪些问题
        可以直接问"，而不必自己拼过滤条件 —— 拼接就是发明语义，而 2.2 已经
        拒绝过一次"CLI 自己发明查询语义"；
      * **可选 extras 的清单**（4.5）—— 有哪些 extras、装没装、跑哪条命令装、
        各自是干嘛的；
      * **命令的读 / 写分类**（5.2，`_commands_report`）—— 哪些能随便调、
        哪些会改库；分类与 `WRITABLE`（分发层真正用来开连接的事实）绑定；
      * **枚举取值**（5.2，`_enums_report`）—— 镜头 / 生命周期 / Inbox /
        存储与持有 / scope 三态，从真库 CHECK 与 `core/scope.py` 算出；
      * **解析器清单**（5.2，`_parsers_report`）—— `import --parser` 的取值。

    扩这里，不新造一条命令 —— 这条规矩从 4.5 起就是本命令的 docstring 明文。
    """
    conn = ctx.conn()
    kinds = query.kinds(conn)
    verbs = query.relation_verbs(conn)

    notes: list[str] = []
    observed: list[dict] = []
    try:
        project_id = resolve_project(conn, args.project)
        observed = query.observed_fields(conn, project_id)
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
        "tool_version": _tool_version(),
        "kinds": kinds,
        "relation_verbs": verbs,
        "named_queries": {name: {"help": help_text,
                                 "filters": [f for f in query.ENTITY_FILTERS
                                             if f not in ("cursor", "limit")]}
                          for name, help_text in sorted(query.NAMED_QUERIES.items())},
        "fields": {"entity": list(ENTITY_FIELDS),
                   "observation": observed,
                   "observation_note": OBSERVATION_FIELDS_NOTE},
        "query_syntax": _query_syntax(ctx.parser),
        "commands": _commands_report(ctx.parser),
        "enums": _enums_report(conn),
        "parsers": _parsers_report(),
        "extras": _extras_report(),
        "notes": notes,
    }
    human = [f"schema 版本 {data['schema_version']}"
             f"（magpie {data['tool_version']}）",
             "", "资源类型（kind_registry）："]
    human += _tbl(["KIND", "LENS", "PRODUCED_BY"],
                  [[k["kind"], k["lens"], ",".join(k["produced_by"])] for k in kinds])
    human += ["", "关系动词（relation_vocabulary）："]
    human += _tbl(["VERB", "INVERSE"], [[v["verb"], v["inverse"]] for v in verbs])
    human += ["", "具名查询（§3.5）："]
    human += _tbl(["名字", "说明"], [[name, help_text]
                                     for name, help_text in sorted(query.NAMED_QUERIES.items())])
    human += ["", "实体字段："]
    human += _tbl(["NAME", "TYPE", "可筛", "说明"],
                  [[f["name"], f["type"], f.get("filter") or "—",
                    (f.get("note") or "")[:40]] for f in ENTITY_FIELDS])
    human += ["", "观测字段（本库实际出现）："]
    human += _tbl(["KIND", "FIELD", "VALUE_KIND", "N"],
                  [[k["kind"], f["field"], f["value_kind"], f["count"]]
                   for k in observed for f in k["fields"]])
    human += ["", "命令读写（access=write 的会改库；read 的随便调）："]
    human += _tbl(["COMMAND", "ACCESS", "说明"],
                  [[key, entry["access"], entry.get("note") or "—"]
                   for key, entry in data["commands"].items()])
    human += ["", "枚举取值（从真库 CHECK / scope 求值算出，不手抄）："]
    human += _tbl(["词表", "取值"],
                  [[name, ", ".join(str(v) for v in values)]
                   for name, values in data["enums"].items() if name != "scope"])
    human.append(f"  scope：三态 true / false / null，只判 "
                 f"{'、'.join(data['enums']['scope']['evaluated_kinds'])}"
                 f"（null=不问，不当越界 —— 详见 --json 的 enums.scope.note）")
    human += ["", "解析器（`import --parser <tool>` 的取值）："]
    human += _tbl(["TOOL", "FMT"], [[tool["tool"], tool["format_version"]]
                                    for tool in data["parsers"]["tools"]],
                  empty="（无 —— 任何文件都仲裁不出解析器）")
    human.append(f"  来源：{data['parsers']['source']}")
    human += ["", "可选 extras（4.5 收口：装没装一并报出；缺了按 install 装）："]
    human += _tbl(["EXTRAS", "已装", "包", "用途"],
                  [[name, "✓" if info["installed"] else "✗",
                    ", ".join(info["packages"]) or "（无 —— 零第三方，见用途）",
                    info["purpose"]]
                   for name, info in data["extras"].items()])
    for note in notes:
        human.append(f"  ⚠ {note}")
    return data, human


# ---------------------------------------------------------------------------
# timeline（4.4）—— §8.1 时间语义的读侧出口
# ---------------------------------------------------------------------------


def cmd_timeline(args, ctx) -> tuple[dict, list[str]]:
    """项目时间线。时间语义**逐字沿用 §8.1**（`query.timeline` 是唯一实现）。

    输出两桶：`events`（accuracy ∈ {exact, approximate}，按 observed_at 升序）
    与 `lower_bounds`（accuracy=unknown，只作下界）。分桶而不是给每行打标记 ——
    一个混排的列表，**行序本身**就已经把 unknown 当成了排序依据（§8.1 要防的
    正是这个），消费者看的是行序，不是标记。
    """
    conn = ctx.conn()
    pid = resolve_project(conn, getattr(args, "project", None))
    limit = getattr(args, "limit", query.DEFAULT_PAGE)
    data = query.timeline(conn, pid, limit=limit)

    human = [f"时间线（项目 {pid}）："
             f"有序 {data['counts']['ranked']} 条（exact/approximate）、"
             f"下界 {data['counts']['lower_bounds']} 条（unknown）"]
    if data["truncated"]:
        human.append(f"  ⚠ 已截断到 {limit} 条（--limit 调大可看全）")
    human.append("")
    human.append("有序事件 events（§8.1：exact/approximate 才能当排序依据）：")
    human += _tbl(
        ["OBSERVED_AT", "ACCURACY", "KIND", "ENTITY", "FIELD", "VALUE"],
        [[e["observed_at"], e["observed_at_accuracy"], e["kind"],
          e["entity_key"] or "—", e["field"],
          str(e["value"])[:40]] for e in data["events"]],
        empty="（无 —— 本库尚无 exact/approximate 观测）")
    human.append("")
    human.append("下界 lower_bounds（§8.1：unknown 只作「不早于」下界，不参与排序）：")
    human += _tbl(
        ["NOT_BEFORE", "KIND", "ENTITY", "FIELD", "VALUE"],
        [[e["lower_bound"], e["kind"], e["entity_key"] or "—", e["field"],
          str(e["value"])[:40]] for e in data["lower_bounds"]],
        empty="（无）")
    human.append(f"  语义：{data['semantics']}")
    return data, human


# ---------------------------------------------------------------------------
# export（4.4）—— 默认脱敏的自洽包，--full 才带归档原文
# ---------------------------------------------------------------------------

#: `--full` 时打到 **stderr** 的告警。放 stderr 而不是塞进 `--json` 信封：
#: 信封在 stdout、是给机器读的，混一行警告进去会把 `json.load` 弄炸；而 stderr
#: 在终端上**就在眼前** —— 判据要的是"命令行上一眼能看见，不是日志里一行"。
_FULL_WARNING = (
    "⚠ " + "=" * 68 + "\n"
    "⚠  magpie export --full：包里含归档原文 —— 可能带 headers / cookie / 凭据明文！\n"
    "⚠  默认导出（不带 --full）已剔除原文正文；--full 是你的显式选择。\n"
    "⚠ " + "=" * 68
)


def cmd_export(args, ctx) -> tuple[dict, list[str]]:
    """导出一个自洽的包（`core.export.build_package` 拼装，这里只落盘 + 告警）。

    默认 `redaction=default`：不含原文正文（headers/cookie/凭据结构上进不来），
    观测值再过一道值级掩码。`--full`：额外带 `stored` 归档正文 + stderr 告警。
    `external` 正文两种模式都不带（4.1 红线）。
    """
    conn = ctx.conn()
    pid = resolve_project(conn, getattr(args, "project", None))
    full = bool(getattr(args, "full", False))
    out = Path(getattr(args, "output", None) or "magpie-export")

    if full:
        print(_FULL_WARNING, file=sys.stderr)      # 一眼可见，且不污染 stdout 信封

    files = export.build_package(
        conn, pid, full=full,
        tool_version=_tool_version(),
        project_name=query.project_name(conn, pid))
    root = export.write_package(files, out)

    manifest = json.loads(files[export.MANIFEST_NAME])
    warning = None
    if full:
        warning = ("--full：包含 stored 归档原文（可能含 headers/cookie/凭据明文）；"
                   "external 正文仍未包含。")
    data = {
        "output": str(root),
        "redaction": manifest["redaction"],
        "full": full,
        "files": sorted(files),
        "counts": manifest["counts"],
        "warning": warning,
    }
    human = [f"导出 → {root}（模式 {manifest['redaction']}）",
             f"  文件：{', '.join(sorted(files))}",
             f"  计数：{manifest['counts']}"]
    if full:
        human.append("  ⚠ " + warning)
    human.append("  （格式与说明随包走：README.md + manifest.json）")
    return data, human


def _tool_version() -> str:
    """取包版本号（core 不 import 包根，这里补上）。"""
    from .. import __version__
    return __version__


#: 命令名 → 实现。`__init__.py` 按它分发；**没有登记的命令不会被执行**。
#:
#: 写命令从 `write.py` 并进来（而不是各留一张表）：分发只该有一处，否则
#: "某条命令不被执行"的诊断要先去查它到底在哪张表里。`project create` 这种
#: 嵌套命令在这里折成一个带空格的键（见 `__init__._runner_key`）。
RUNNERS = {
    "init": cmd_init,
    "ls": cmd_ls,
    "lens": cmd_lens,
    "show": cmd_show,
    "search": cmd_search,
    "diff": cmd_diff,
    "doctor": cmd_doctor,
    "schema": cmd_schema,
    # -- 5.5：风险评分 + 攻击面概览（纯读侧派生）--------------------------------
    "risk": cmd_risk,
    # -- 5.4：出站问答（默认关闭 —— 分类上是读（不改库），行为上是唯一出站点）----
    "ask": cmd_ask,
    # -- 4.4：时间线 / 导出（打包）----------------------------------------------
    "timeline": cmd_timeline,
    "export": cmd_export,
    # -- 2.6 的写路径（实现在 `write.py`）--------------------------------------
    "project create": write.cmd_project_create,
    "import": write.cmd_import,
    "undo": write.cmd_undo,
    "redo": write.cmd_redo,
    "jobs": write.cmd_jobs,
}
