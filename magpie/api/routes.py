"""`/api/v1` 的路由（计划书 §6.2 / 2.7 任务 1）。

**这个模块里没有一条 SQL 在写库。** 所有写都 `ctx.writer.submit(...)` 排进单写
队列（2.7 任务 2），读走每请求一条自己的连接（WAL 下读写不互相阻塞）。
这条纪律不是洁癖：R1"写入口唯一"的全部价值，就在于**任何时刻只有一个写者**，
而它一旦被某条"顺手 UPDATE 一下"的路由破坏，就没有任何测试能替你发现。

响应形状（对外契约，改它要升 minor）:

    {"ok": true, "data": ..., "meta": {...}}          成功
    {"ok": false, "error": {"code","message","hint"}} 失败

**没有交付的端点必须响亮**：§6.2 里还有 search / diff / paste / pipelines 四条
需要本步尚未建成的子系统（搜索 DSL 是 §8、diff 是 §8.5、粘贴导入与管线编辑是
2.6/3）。它们一律返回 **501 `E_NOT_IMPLEMENTED`**，而不是空数组 —— 空数组对
agent 是一个确定的断言（"这里就是没有"），而事实是"这条路由还不存在"。

已知边界（诚实交代，别把它读成完整实现）:

  * `PATCH /entities/{id}` **不写值**，只写标签与人工保护标记。
    "人工写入某个字段的**值**"需要 `ManualWriter` 有一条值写入路径（属于 2.6 的
    写路径），在 HTTP 层临时拼一个 job + 贡献就是**第二条写值的路**，而那正是
    R1 要防的东西。所以这里 501，并给出该走哪里。
  * `?review_status=` / `?in_scope=` 过滤需要"人工状态"与 scope 求值，本步没有 ——
    传了就 400 并列出**支持**的过滤条件（沉默地忽略一个过滤条件等于给 agent 假答案）。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core import ingest, ops, store
from . import API_PREFIX
from .errors import ApiError, not_implemented

router = APIRouter(prefix=API_PREFIX)

#: SSE 批量推送间隔（计划书 §6.2 的"每 100ms"）。客户端拿到的是**一批**事件，
#: 而不是一条一条 —— 一次导入的文件数 + 进度事件在小批量下震荡很明显。
SSE_BATCH_SECONDS = 0.1
#: 没事件时多久发一次心跳注释。比它短会白占带宽，比它长会被中间的代理掐掉。
SSE_HEARTBEAT_SECONDS = 15.0
#: 单帧最多带多少条事件（防止一个卡了很久的消费者在恢复时灌出一条巨帧）。
SSE_MAX_BATCH = 256

MAX_PAGE = 500
DEFAULT_PAGE = 50

#: 列表端点支持的过滤条件。**唯一的声明处** —— 报错信息也从它来。
_ENTITY_FILTERS = ("kind", "q", "tag", "lifecycle", "cursor", "limit")


# ---------------------------------------------------------------------------
# 公共零件
# ---------------------------------------------------------------------------


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "meta": meta}


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """每请求一条**只读用途**的连接。用完即关。

    为什么不共享一条：写线程手里的那条连接会停在**未提交**的事务里，共享它读
    就会读到未提交的行 —— "写者唯一"说的是只有一个写者，不是只有一个连接。
    """
    conn = request.app.state.ctx.connect_ro()
    try:
        yield conn
    finally:
        conn.close()


def _ctx(request: Request):
    return request.app.state.ctx


def _resolve_project(conn: sqlite3.Connection, project: str | int | None) -> int:
    """`?project=` 收 id 或名字；不给时，**只有一个项目**才允许省略。

    刻意不做"默认选第一个"：多项目下"第一个"是行序，不是意图。让调用方显式写，
    比让他某天发现数据进了另一个项目要好。
    """
    if project is None:
        rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
        if len(rows) == 1:
            return int(rows[0]["id"])
        raise ApiError(400, "E_PROJECT_REQUIRED",
                       "这个库里不止一个项目（或一个都没有），必须显式给 project",
                       hint="加上 ?project=<id 或名字>；用 GET /api/v1/projects 查",
                       projects=[str(r["name"]) for r in rows])
    spec = str(project)
    row = None
    if spec.isdigit():
        row = conn.execute("SELECT id FROM projects WHERE id=?", (int(spec),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM projects WHERE name=?", (spec,)).fetchone()
    if row is None:
        raise ApiError(404, "E_PROJECT_NOT_FOUND", f"项目 {spec!r} 不存在")
    return int(row["id"])


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ApiError(400, "E_BAD_LIMIT", f"limit 必须 >= 1，收到 {limit}")
    return min(int(limit), MAX_PAGE)


def _parse_cursor(cursor: str | None) -> int | None:
    if cursor in (None, "", "null"):
        return None
    if not str(cursor).isdigit():
        raise ApiError(400, "E_BAD_CURSOR",
                       f"cursor 必须是上一页末尾的实体 id，收到 {cursor!r}")
    return int(cursor)


def _unsupported(filters: dict[str, Any], supported: tuple[str, ...]) -> None:
    """沉默地忽略一个过滤条件 = 给 agent 一个假答案。**必须响亮地拒绝。**"""
    unknown = sorted(k for k, v in filters.items() if v not in (None, ""))
    if unknown:
        raise ApiError(400, "E_UNSUPPORTED_FILTER",
                       f"本步不支持这些过滤条件：{', '.join(unknown)}",
                       hint=f"支持的过滤条件：{', '.join(supported)}。"
                            f"被静默忽略的过滤条件会让调用方以为筛过了 —— 那比报错坏得多",
                       supported=list(supported), rejected=unknown)


def _json_or_none(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


def _keys_of(conn: sqlite3.Connection, project_id: int,
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


def _entity_row(conn: sqlite3.Connection, project_id: int, entity_id: int) -> dict:
    row = conn.execute(
        "SELECT id, kind, lifecycle, is_current, merged_into, first_seen, last_seen"
        " FROM entities WHERE id=? AND project_id=?",
        (entity_id, project_id)).fetchone()
    if row is None:
        raise ApiError(404, "E_ENTITY_NOT_FOUND",
                       f"实体 {entity_id} 不在项目 {project_id} 里")
    entity = dict(row)
    entity["entity_key"] = _keys_of(conn, project_id, [entity_id]).get(entity_id)
    return entity


def _tags_of(conn: sqlite3.Connection, project_id: int, entity_id: int) -> list[str]:
    return [str(r["tag"]) for r in conn.execute(
        "SELECT tag FROM entity_tags WHERE project_id=? AND entity_id=?"
        " AND deleted_at IS NULL ORDER BY tag", (project_id, entity_id))]


def _manual_keys(conn: sqlite3.Connection, project_id: int, entity_id: int) -> list[str]:
    row = conn.execute(
        "SELECT keys_json FROM manual_fields WHERE project_id=? AND subject_kind='entity'"
        " AND subject_id=?", (project_id, entity_id)).fetchone()
    keys = _json_or_none(None if row is None else row["keys_json"])
    return [str(k) for k in keys] if isinstance(keys, list) else []


# ---------------------------------------------------------------------------
# 元信息 / 项目
# ---------------------------------------------------------------------------


@router.get("/meta", summary="版本 / schema / 类型登记表 / 可用解析器")
def get_meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    from .. import __version__

    schema_version = conn.execute(
        "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    kinds = [{"kind": str(r["kind"]), "lens": str(r["lens"]),
              "identity_rule": str(r["identity_rule"]),
              "produced_by": _json_or_none(r["produced_by"]) or []}
             for r in conn.execute(
                 "SELECT kind, lens, identity_rule, produced_by FROM kind_registry"
                 " ORDER BY kind")]
    verbs = [{"verb": str(r["verb"]), "inverse": r["inverse"],
              "description": str(r["description"])}
             for r in conn.execute(
                 "SELECT verb, inverse, description FROM relation_vocabulary"
                 " ORDER BY verb")]
    try:
        parsers, source_pkg = ingest.load_parsers()
        parser_list = [{"tool": str(c.tool), "format_version": str(c.format_version)}
                       for c in parsers]
    except Exception as exc:                       # 解析器坏了不该让 /meta 也坏
        source_pkg, parser_list = f"<{type(exc).__name__}: {exc}>", []
    return ok({
        "magpie_version": __version__,
        "schema_version": schema_version,
        "api_prefix": API_PREFIX,
        "kinds": kinds,
        "relation_verbs": verbs,
        "parsers": parser_list,
        "parser_source": source_pkg,
        "counts": {
            "projects": _scalar(conn, "SELECT COUNT(*) FROM projects"),
            "entities": _scalar(conn, "SELECT COUNT(*) FROM entities WHERE is_current=1"),
            "observations": _scalar(conn,
                                    "SELECT COUNT(*) FROM observations WHERE is_current=1"),
            "relations": _scalar(conn,
                                 "SELECT COUNT(*) FROM relations WHERE is_current=1"),
            "jobs": _scalar(conn, "SELECT COUNT(*) FROM jobs"),
            "open_inbox": _scalar(conn,
                                  "SELECT COUNT(*) FROM inbox_items WHERE state='open'"),
        },
    })


def _scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


class ProjectBody(BaseModel):
    name: str
    scope: dict[str, Any] = Field(default_factory=dict)


@router.post("/projects", status_code=201, summary="建项目（空库的第一件事）")
def create_project(body: ProjectBody, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """**§6.2 的草案里没有这条**，但没有它，第一次打开 GUI 的人什么都导不进去
    （`POST /imports` 要求项目已存在，而空库里一个项目都没有）—— 一个"要先在
    别处做点什么"的空界面，是最容易被误读成故障的那种界面。

    真正建项目的入口仍然是 `ops.create_project`（写者唯一）。
    """
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (body.name,)).fetchone():
        raise ApiError(409, "E_PROJECT_EXISTS", f"项目 {body.name!r} 已存在")
    ctx = _ctx(request)
    name, scope = body.name, dict(body.scope)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        project_id = ops.create_project(conn_, name, scope)
        return {"id": project_id, "name": name, "scope": scope}

    return ok(ctx.writer.submit(f"create project {name}", task).result(timeout=30))


@router.get("/projects", summary="项目列表")
def list_projects(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, name, status, scope_version, scope_hash, parser_fingerprint"
        " FROM projects ORDER BY id").fetchall()
    items = []
    for row in rows:
        pid = int(row["id"])
        item = dict(row)
        item["scope"] = _json_or_none(conn.execute(
            "SELECT scope FROM projects WHERE id=?", (pid,)).fetchone()["scope"])
        item["counts"] = {
            "entities": _scalar(conn, "SELECT COUNT(*) FROM entities"
                                      " WHERE project_id=? AND is_current=1", pid),
            "observations": _scalar(conn, "SELECT COUNT(*) FROM observations"
                                          " WHERE project_id=? AND is_current=1", pid),
            "relations": _scalar(conn, "SELECT COUNT(*) FROM relations"
                                       " WHERE project_id=? AND is_current=1", pid),
        }
        items.append(item)
    return ok(items, count=len(items))


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


@router.get("/entities", summary="实体列表（键集游标分页）")
def list_entities(project: str | None = None,
                  kind: str | None = None,
                  q: str | None = None,
                  tag: str | None = None,
                  lifecycle: str | None = None,
                  cursor: str | None = None,
                  limit: int = DEFAULT_PAGE,
                  review_status: str | None = None,
                  in_scope: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    _unsupported({"review_status": review_status, "in_scope": in_scope},
                 _ENTITY_FILTERS)
    size = _page_size(limit)
    project_id = _resolve_project(conn, project)

    where = ["e.project_id=?", "e.is_current=1", "e.merged_into IS NULL"]
    params: list[Any] = [project_id]
    if kind:
        where.append("e.kind=?")
        params.append(str(kind))
    if lifecycle:
        where.append("e.lifecycle=?")
        params.append(str(lifecycle))
    if q:
        where.append("EXISTS (SELECT 1 FROM entity_keys k WHERE k.entity_id=e.id"
                     " AND k.project_id=e.project_id AND k.entity_key LIKE ?)")
        params.append(f"%{q}%")
    if tag:
        where.append("EXISTS (SELECT 1 FROM entity_tags t WHERE t.entity_id=e.id"
                     " AND t.project_id=e.project_id AND t.tag=? AND t.deleted_at IS NULL)")
        params.append(str(tag))
    after = _parse_cursor(cursor)
    if after is not None:
        where.append("e.id > ?")
        params.append(after)

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
    return ok(items, count=len(items), next_cursor=next_cursor,
              project_id=project_id, filters={"kind": kind, "q": q, "tag": tag,
                                              "lifecycle": lifecycle})


@router.get("/entities/{entity_id}", summary="实体详情（观测 / 关系 / 标签 / Inbox）")
def get_entity(entity_id: int, project: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    entity = _entity_row(conn, project_id, entity_id)

    observations = [dict(r) for r in conn.execute(
        "SELECT id, semantic_field, value, value_kind, confidence, source_count,"
        " job_count, origin, observed_at, observed_at_accuracy"
        " FROM observations WHERE entity_id=? AND project_id=? AND is_current=1"
        " ORDER BY semantic_field, id", (entity_id, project_id))]

    relations = []
    rows = conn.execute(
        "SELECT id, rel, src_id, dst_id, confidence, source_count, job_count"
        " FROM relations WHERE project_id=? AND is_current=1"
        " AND (src_id=? OR dst_id=?) ORDER BY rel, id",
        (project_id, entity_id, entity_id)).fetchall()
    other_ids = sorted({int(r["dst_id"]) if int(r["src_id"]) == entity_id
                        else int(r["src_id"]) for r in rows})
    keys = _keys_of(conn, project_id, other_ids)
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

    activity = [dict(r) for r in conn.execute(
        "SELECT id, actor, action, at, job_id FROM activity_log"
        " WHERE target_kind='entity' AND target_id=? ORDER BY id DESC LIMIT 20",
        (entity_id,))]

    return ok({"entity": entity, "observations": observations, "relations": relations,
               "tags": _tags_of(conn, project_id, entity_id),
               "manual": {"protected_fields": _manual_keys(conn, project_id, entity_id)},
               "open_inbox": inbox, "timeline": activity},
              project_id=project_id)


class EntityPatch(BaseModel):
    """`PATCH /entities/{id}` 的体。**只收能真的落地的字段**（见模块文档的边界）。"""

    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    protect_fields: list[str] = Field(default_factory=list)
    unprotect_fields: list[str] = Field(default_factory=list)
    reason: str | None = None
    #: 收到就 501 —— 收下它再静默丢掉，比不收更坏。
    review_status: str | None = None
    display_name: str | None = None


@router.patch("/entities/{entity_id}", summary="改标签 / 人工保护标记")
def patch_entity(entity_id: int, body: EntityPatch, request: Request,
                 project: str | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if body.review_status is not None or body.display_name is not None:
        raise not_implemented("人工写入字段的**值**（review_status / display_name）",
                              plan="2.6 的写路径（ManualWriter.set_field_value）")
    project_id = _resolve_project(conn, project)
    _entity_row(conn, project_id, entity_id)          # 不存在 → 404（在排队之前）
    if not (body.add_tags or body.remove_tags
            or body.protect_fields or body.unprotect_fields):
        raise ApiError(400, "E_EMPTY_PATCH", "这次 PATCH 什么都没改",
                       hint="至少要给 add_tags / remove_tags / protect_fields / "
                            "unprotect_fields 中的一项")

    ctx = _ctx(request)
    entity_id_, reason = entity_id, body.reason

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        from ..core.manual import ManualWriter
        writer = ManualWriter(conn_, actor="api")
        for tag in body.add_tags:
            writer.set_tag(project_id=project_id, entity_id=entity_id_, tag=tag,
                           reason=reason)
        for tag in body.remove_tags:
            writer.remove_tag(project_id=project_id, entity_id=entity_id_, tag=tag,
                              reason=reason)
        if body.protect_fields or body.unprotect_fields:
            writer.set_keys(project_id=project_id, subject_kind="entity",
                            subject_id=entity_id_,
                            add=body.protect_fields, remove=body.unprotect_fields)
        return {"entity_id": entity_id_, "tags": _tags_of(conn_, project_id, entity_id_),
                "protected_fields": _manual_keys(conn_, project_id, entity_id_)}

    return ok(ctx.writer.submit(f"patch entity {entity_id}", task).result(timeout=30),
              project_id=project_id)


class MergeBody(BaseModel):
    survivor_key: str
    member_keys: list[str] = Field(default_factory=list)
    dry_run: bool = False


@router.post("/entities/{entity_id}/merge", summary="合并（identity overlay）")
def merge_entity(entity_id: int, body: MergeBody, request: Request,
                 project: str | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    target = _entity_row(conn, project_id, entity_id)
    if target["entity_key"] != body.survivor_key:
        raise ApiError(400, "E_MERGE_SURVIVOR_MISMATCH",
                       f"路径上的实体 {entity_id} 的 key 是 {target['entity_key']!r}，"
                       f"与 survivor_key {body.survivor_key!r} 不一致",
                       hint="合并的幸存者只能由 key 指定；两者对不上时宁可拒绝，"
                            "也不要猜你想合谁")
    ctx = _ctx(request)
    members = list(body.member_keys)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        return ops.merge(conn_, project_id=project_id, survivor_key=body.survivor_key,
                         member_keys=members, actor="api", dry_run=body.dry_run)

    return ok(ctx.writer.submit(f"merge {body.survivor_key}", task).result(timeout=60),
              project_id=project_id)


# ---------------------------------------------------------------------------
# 图 / 镜头
# ---------------------------------------------------------------------------


def _graph_payload(conn: sqlite3.Connection, project_id: int, *, lens: str | None,
                   kind: str | None, q: str | None, limit: int,
                   seeds: list[int] | None = None) -> dict[str, Any]:
    where = ["e.project_id=?", "e.is_current=1", "e.merged_into IS NULL"]
    params: list[Any] = [project_id]
    if kind:
        where.append("e.kind=?")
        params.append(str(kind))
    if lens:
        where.append("e.kind IN (SELECT kind FROM kind_registry WHERE lens=?)")
        params.append(str(lens))
    if seeds is not None:
        where.append(f"e.id IN ({','.join('?' * len(seeds))})")
        params.extend(seeds)
    rows = conn.execute(
        "SELECT e.id, e.kind, e.lifecycle,"
        " (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id"
        "   AND k.project_id=e.project_id AND k.canonical=1"
        "   ORDER BY k.entity_key LIMIT 1) AS entity_key"
        f" FROM entities e WHERE {' AND '.join(where)} ORDER BY e.id LIMIT ?",
        (*params, limit)).fetchall()
    nodes = [dict(r) for r in rows]
    if q:
        nodes = [n for n in nodes if q in str(n["entity_key"] or "")]
    ids = [int(n["id"]) for n in nodes]
    layout = {}
    edges: list[dict[str, Any]] = []
    if ids:
        marks = ",".join("?" * len(ids))
        for row in conn.execute(
                "SELECT entity_id, x, y, collapsed FROM entity_layout"
                f" WHERE project_id=? AND entity_id IN ({marks})",
                (project_id, *ids)):
            layout[str(row["entity_id"])] = {"x": row["x"], "y": row["y"],
                                             "collapsed": bool(row["collapsed"])}
        keys = {int(n["id"]): n["entity_key"] for n in nodes}
        for row in conn.execute(
                "SELECT id, src_id, dst_id, rel, confidence FROM relations"
                f" WHERE project_id=? AND is_current=1 AND src_id IN ({marks})"
                f" AND dst_id IN ({marks}) ORDER BY id", (project_id, *ids, *ids)):
            edges.append({"id": int(row["id"]), "src": int(row["src_id"]),
                          "dst": int(row["dst_id"]), "rel": str(row["rel"]),
                          "confidence": row["confidence"],
                          "src_key": keys.get(int(row["src_id"])),
                          "dst_key": keys.get(int(row["dst_id"]))})
    return {"nodes": nodes, "edges": edges, "layout": layout}


@router.get("/graph", summary="图谱（节点 + 边 + 布局）")
def get_graph(project: str | None = None, lens: str | None = None,
              kind: str | None = None, q: str | None = None,
              limit: int = 500,
              depth: int | None = None,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    _unsupported({"depth": depth}, ("lens", "kind", "q", "limit"))
    project_id = _resolve_project(conn, project)
    payload = _graph_payload(conn, project_id, lens=lens, kind=kind, q=q,
                             limit=_page_size(limit))
    payload["project_id"] = project_id
    return ok(payload, counts={"nodes": len(payload["nodes"]),
                               "edges": len(payload["edges"])})


def _neighbour_ids(conn: sqlite3.Connection, project_id: int, entity_id: int,
                   depth: int, *, limit: int = 300) -> list[int]:
    """从 `entity_id` 出发走 `depth` 层关系（BFS），返回**含自身**的 id 集合。

    上限 4 层是故意的：每一层都是一次 `IN (...)`，层数比宽度更快地把查询变成
    全表扫描。需要的层数超过 4 时，该问的是"你要那张网，还是要那张图的局部"。
    """
    if depth < 1:
        raise ApiError(400, "E_BAD_DEPTH", f"depth 必须 >= 1，收到 {depth}")
    depth = min(int(depth), 4)
    seen = {entity_id}
    frontier = [entity_id]
    for _ in range(depth):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        rows = conn.execute(
            "SELECT src_id, dst_id FROM relations WHERE project_id=? AND is_current=1"
            f" AND (src_id IN ({marks}) OR dst_id IN ({marks}))",
            (project_id, *frontier, *frontier)).fetchall()
        nxt = []
        for row in rows:
            for candidate in (int(row["src_id"]), int(row["dst_id"])):
                if candidate not in seen:
                    seen.add(candidate)
                    nxt.append(candidate)
        frontier = nxt[:limit]
    return sorted(seen)


@router.get("/graph/{entity_id}/neighbors", summary="邻居（BFS，depth 层）")
def get_neighbors(entity_id: int, depth: int = 1, project: str | None = None,
                  limit: int = 300,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    _entity_row(conn, project_id, entity_id)
    ids = _neighbour_ids(conn, project_id, entity_id, depth, limit=limit)
    payload = _graph_payload(conn, project_id, lens=None, kind=None, q=None,
                             limit=max(len(ids), 1), seeds=ids)
    payload["project_id"] = project_id
    payload["root_id"] = entity_id
    payload["depth"] = depth
    return ok(payload, counts={"nodes": len(payload["nodes"]),
                               "edges": len(payload["edges"])})


class LayoutItem(BaseModel):
    entity_id: int
    x: float
    y: float
    collapsed: bool = False


class LayoutBody(BaseModel):
    items: list[LayoutItem] = Field(default_factory=list)


@router.patch("/graph/layout", summary="布局持久化（批量）")
def patch_layout(body: LayoutBody, request: Request, project: str | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    if not body.items:
        raise ApiError(400, "E_EMPTY_PATCH", "items 是空的")
    items = [(i.entity_id, i.x, i.y, i.collapsed) for i in body.items]
    ctx = _ctx(request)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        from ..core.manual import ManualWriter
        writer = ManualWriter(conn_, actor="api")
        for entity_id, x, y, collapsed in items:
            writer.set_layout(project_id=project_id, entity_id=entity_id,
                              x=x, y=y, collapsed=collapsed)
        return {"stored": len(items)}

    return ok(ctx.writer.submit("layout", task).result(timeout=60),
              project_id=project_id)


@router.get("/lenses/{lens}", summary="镜头页（assets|products|endpoints|flows|vulns）")
def get_lens(lens: str, project: str | None = None,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    rows = conn.execute(
        "SELECT k.kind, k.identity_rule, k.produced_by FROM kind_registry k"
        " WHERE k.lens=? ORDER BY k.kind", (str(lens),)).fetchall()
    kinds = []
    for row in rows:
        kinds.append({"kind": str(row["kind"]),
                      "identity_rule": str(row["identity_rule"]),
                      "produced_by": _json_or_none(row["produced_by"]) or [],
                      "count": _scalar(conn,
                                       "SELECT COUNT(*) FROM entities WHERE project_id=?"
                                       " AND kind=? AND is_current=1"
                                       " AND merged_into IS NULL",
                                       project_id, str(row["kind"]))})
    if not kinds:
        known = [str(r["lens"]) for r in conn.execute(
            "SELECT DISTINCT lens FROM kind_registry ORDER BY lens")]
        raise ApiError(404, "E_LENS_NOT_FOUND", f"镜头 {lens!r} 没有归属任何 kind",
                       hint="镜头由 kind_registry.lens 决定", known_lenses=known)
    return ok({"lens": lens, "kinds": kinds}, project_id=project_id,
              total=sum(k["count"] for k in kinds))


# ---------------------------------------------------------------------------
# 导入（含 SSE）
# ---------------------------------------------------------------------------


class ImportBody(BaseModel):
    paths: list[str] = Field(default_factory=list)
    dir: str | None = None
    parser: str | None = None
    source: str | None = None
    dry_run: bool = False
    archive: str = "archived"
    project: str | None = None


@router.post("/imports", status_code=202, summary="提交导入 → 返回导入请求 id")
def post_import(body: ImportBody, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    ctx = _ctx(request)
    if body.archive not in ("archived", "external"):
        raise ApiError(400, "E_BAD_ARCHIVE",
                       f"archive 只支持 archived / external，收到 {body.archive!r}")
    targets = list(body.paths)
    if body.dir:
        targets.append(body.dir)
    if not targets:
        raise ApiError(400, "E_NO_INPUT", "既没给 paths 也没给 dir",
                       hint="至少要给一个文件或目录")
    missing = [p for p in targets if not Path(p).exists()]
    if missing:
        # 快速失败：让"路径写错了"在**提交之前**就报出来，而不是变成一个
        # 通过了 HTTP、却在报告里写"没有匹配到任何文件"的 job。
        raise ApiError(400, "E_PATH_NOT_FOUND",
                       f"这些路径不存在：{missing}",
                       hint="路径按服务进程的工作目录解析；GUI 请用绝对路径")
    project_id = _resolve_project(conn, body.project)
    record = ctx.imports.create(project_id=project_id, paths=targets,
                                parser=body.parser, source=body.source,
                                dry_run=body.dry_run, archive=body.archive)
    data = record.as_dict()
    data["events"] = f"{API_PREFIX}/imports/{record.id}/events"
    return ok(data, project_id=project_id)


@router.get("/imports", summary="导入请求列表（最近优先）")
def list_imports(request: Request) -> dict[str, Any]:
    items = [r.as_dict() for r in _ctx(request).imports.list()]
    return ok(items, count=len(items))


@router.get("/imports/{import_id}", summary="单个导入请求的状态（含完整报告）")
def get_import(import_id: str, request: Request) -> dict[str, Any]:
    record = _ctx(request).imports.get(import_id)
    if record is None:
        raise ApiError(404, "E_IMPORT_NOT_FOUND", f"导入请求 {import_id!r} 不存在",
                       hint="导入请求的 id 形如 imp-1；GET /api/v1/imports 看全部")
    return ok(record.as_dict(include_report=True))


@router.get("/imports/{import_id}/events", summary="★ SSE：进度 / 阶段 / 日志 / 坏行")
async def import_events(import_id: str, request: Request) -> StreamingResponse:
    ctx = _ctx(request)
    if ctx.imports.get(import_id) is None:
        raise ApiError(404, "E_IMPORT_NOT_FOUND", f"导入请求 {import_id!r} 不存在")
    subscription = ctx.broker.subscribe(import_id)

    async def stream():
        idle = 0.0
        try:
            yield _sse_frame("open", {"import_id": import_id,
                                      "batch_ms": int(SSE_BATCH_SECONDS * 1000)})
            while True:
                if await request.is_disconnected():
                    break
                batch, dropped = subscription.drain(SSE_MAX_BATCH)
                if batch:
                    idle = 0.0
                    yield _sse_frame("batch", {
                        "import_id": import_id, "dropped": dropped,
                        "events": [e.as_dict() for e in batch],
                    })
                    if any(e.terminal for e in batch):
                        break
                else:
                    idle += SSE_BATCH_SECONDS
                    if idle >= SSE_HEARTBEAT_SECONDS:
                        idle = 0.0
                        # 注释行是 SSE 的心跳：它让中间的代理与浏览器都知道
                        # "连接还活着"。断流最容易被误读成"导入卡住了"。
                        yield ": keep-alive\n\n"
                await asyncio.sleep(SSE_BATCH_SECONDS)
        finally:
            ctx.broker.unsubscribe(subscription)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        # 让 nginx 之类的反向代理不要缓冲（本服务只在回环，但客户端可能挂了代理）
        "X-Accel-Buffering": "no",
    })


def _sse_frame(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/imports/{import_id}/cancel", summary="请求取消（下一个 checkpoint 生效）")
def cancel_import(import_id: str, request: Request) -> dict[str, Any]:
    record = _ctx(request).imports.cancel(import_id)
    if record is None:
        raise ApiError(404, "E_IMPORT_NOT_FOUND", f"导入请求 {import_id!r} 不存在")
    return ok(record.as_dict())


@router.post("/imports/{import_id}/undo", summary="撤销这一批导入的全部 job")
def undo_import(import_id: str, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    record = ctx.imports.get(import_id)
    if record is None:
        raise ApiError(404, "E_IMPORT_NOT_FOUND", f"导入请求 {import_id!r} 不存在")
    if not record.job_ids:
        raise ApiError(409, "E_NOTHING_TO_UNDO",
                       f"导入请求 {import_id} 还没有产生任何 job（state={record.state}）")
    ticket = ctx.imports.undo(import_id)
    return ok(ticket.result(timeout=120))


# ---------------------------------------------------------------------------
# 作业历史 + undo / redo（2.8 的三个用户入口之一）
# ---------------------------------------------------------------------------
#
# 为什么这三个端点属于 2.8 的交付，而不是"再开一条 CLI 的活"：原设计里
# 「导入 / undo / redo」由 `magpie import` / `magpie undo` / `magpie redo`
# （2.6 的 CLI 写路径）承担，而 2.6 已**后置**。CLI 后置后图形界面成了唯一的
# 用户界面 —— **没有这三个端点，G2 的「导入 → undo → redo 精确复原」在界面上
# 根本无法验收**。
#
# 三条纪律：
#
#   1. 路由里**一条写库的 SQL 都没有**（与导入一致）：undo / redo 一律提交给
#      单写队列（R1 写入口唯一）。**干跑也走队列** —— `ops.undo(dry_run=True)`
#      是在事务里真的 INSERT/UPDATE 之后再 ROLLBACK，而请求连接带着
#      `PRAGMA query_only = ON`（`app.ApiContext.connect_ro`），在它上面干跑会
#      当场报错。更根本的理由是：干跑要读的必须是**写者眼里的**那个库。
#   2. 干跑与真跑**输出同构**，只有 COMMIT → ROLLBACK 之差（`ops.undo` /
#      `ops.redo` 的 `dry_run` 本来就是这个语义），所以界面可以用同一段代码
#      渲染"预览"与"结果"，不必维护两套。
#   3. `explain_job_scope` 的输出**原样**带上。旧 job 必须用**当时的** scope
#      解释；界面若拿当前 scope 去解释历史作业，就会得出"这条覆盖已经失效"
#      的假结论 —— 那正是 `ops.explain_job_scope` 存在的理由。

#: 作业历史默认返回多少条。与实体列表的 50 不同：作业是"最近发生了什么"，
#: 一次导入就会产生 N 条（每个文件一条），翻页在这里比多给几条更烦人。
DEFAULT_JOB_LIMIT = 100


def _job_row(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ApiError(404, "E_JOB_NOT_FOUND", f"作业 {job_id} 不存在",
                       hint="作业 id 由导入产生；GET /api/v1/jobs 看全部")
    return dict(row)


def _job_counts(conn: sqlite3.Connection,
                job_ids: list[int]) -> dict[int, dict[str, int]]:
    """每个 job 的贡献数 / 已撤回数。**一次查完，不做 N+1。**

    `retracted` 就是 undo 的可逆性证据：undo 一次 = 这个数从 0 变成贡献数，
    redo 一次 = 变回 0。界面据此判断"这个作业现在处于哪一侧"，而不是自己记账。
    """
    out = {int(j): {"contributions": 0, "retracted": 0} for j in job_ids}
    if not job_ids:
        return out
    marks = ",".join("?" * len(job_ids))
    for row in conn.execute(
            "SELECT job_id, COUNT(*) AS n FROM contributions"
            f" WHERE job_id IN ({marks}) GROUP BY job_id", tuple(job_ids)):
        out[int(row["job_id"])]["contributions"] = int(row["n"])
    for row in conn.execute(
            "SELECT superseded_by_job_id AS jid, COUNT(*) AS n"
            " FROM contribution_supersessions"
            f" WHERE superseded_by_job_id IN ({marks}) AND reason='retract'"
            " GROUP BY superseded_by_job_id", tuple(job_ids)):
        out[int(row["jid"])]["retracted"] = int(row["n"])
    return out


def _decorate_job(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    item["applied"] = bool(item["applied"])
    item["counts"] = _job_counts(conn, [int(item["id"])])[int(item["id"])]
    item["can_undo"] = bool(item["applied"])
    item["can_redo"] = not bool(item["applied"])
    return item


_JOB_STATES = ("running", "done", "failed", "partial", "aborted")
_JOB_COLUMNS = ("id, project_id, kind, state, applied, coverage, scope_version,"
                " scope_hash, parser_fingerprint, actor, started_at, finished_at")


@router.get("/jobs", summary="作业历史（最近的在前）")
def list_jobs(project: str | None = None, state: str | None = None,
              applied: int | None = None, limit: int = DEFAULT_JOB_LIMIT,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if project is not None:
        where.append("project_id=?")
        params.append(_resolve_project(conn, project))
    if state is not None:
        if state not in _JOB_STATES:
            raise ApiError(400, "E_BAD_STATE",
                           f"state 只能是 {' / '.join(_JOB_STATES)}，收到 {state!r}",
                           supported=list(_JOB_STATES))
        where.append("state=?")
        params.append(str(state))
    if applied is not None:
        # 布尔从查询串进来只可能是 "0"/"1"/"true"/"false"，而 FastAPI 对
        # int 的解析会把 "true" 变成 422（带一页 pydantic 报错）。收下来自己
        # 判定，是为了让错误信息说人话。
        where.append("applied=?")
        params.append(1 if applied else 0)
    rows = conn.execute(f"SELECT {_JOB_COLUMNS} FROM jobs"
                        f" WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                        (*params, _page_size(limit))).fetchall()
    items = [_decorate_job(conn, dict(r)) for r in rows]
    return ok(items, count=len(items))


@router.get("/jobs/{job_id}", summary="作业详情（含 explain_job_scope）")
def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = _decorate_job(conn, _job_row(conn, job_id))
    # scope 的解释**必须**来自数据层：`interpret_with` 是 historical_scope 时，
    # 界面要显示的是"当时的 scope"，不是现在这份。
    scope = ops.explain_job_scope(conn, job_id)
    activity = [dict(r) for r in conn.execute(
        "SELECT id, at, actor, action, target_kind, target_id, detail"
        " FROM activity_log WHERE job_id=? ORDER BY id DESC LIMIT 50", (job_id,))]
    for item in activity:
        item["detail"] = _json_or_none(item["detail"])
    return ok({"job": job, "scope": scope, "activity": activity,
               "can_undo": job["can_undo"], "can_redo": job["can_redo"]},
              project_id=int(job["project_id"]))


class JobAction(BaseModel):
    """undo / redo 的体。

    `dry_run=True` 时输出与真跑**同构**（只有 COMMIT→ROLLBACK 之差），
    界面因此可以用同一段代码渲染预览与结果 —— 见本节模块注释第 2 条。
    """

    dry_run: bool = False


@router.post("/jobs/{job_id}/undo", summary="撤销一个作业（dry_run = 干跑预览）")
def undo_job(job_id: int, body: JobAction, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = _job_row(conn, job_id)
    ctx, dry = _ctx(request), bool(body.dry_run)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        return {"op": "undo", "job_id": job_id, "dry_run": dry,
                "report": ops.undo(conn_, job_id, actor="api", dry_run=dry)}

    label = f"{'dry-run ' if dry else ''}undo job {job_id}"
    return ok(ctx.writer.submit(label, task).result(timeout=120),
              project_id=int(job["project_id"]))


@router.post("/jobs/{job_id}/redo", summary="复原一个被撤销的作业（dry_run = 干跑预览）")
def redo_job(job_id: int, body: JobAction, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = _job_row(conn, job_id)
    ctx, dry = _ctx(request), bool(body.dry_run)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        return {"op": "redo", "job_id": job_id, "dry_run": dry,
                "report": ops.redo(conn_, job_id, actor="api", dry_run=dry)}

    label = f"{'dry-run ' if dry else ''}redo job {job_id}"
    return ok(ctx.writer.submit(label, task).result(timeout=120),
              project_id=int(job["project_id"]))


# ---------------------------------------------------------------------------
# 体检 / Inbox / 活动
# ---------------------------------------------------------------------------


@router.get("/doctor", summary="数据体检")
def doctor(project: str | None = None,
           conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    report = store.integrity_check(conn)
    jobs = [dict(r) for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state ORDER BY state")]
    project_id = None
    try:
        project_id = _resolve_project(conn, project)
    except ApiError:
        project_id = None
    data: dict[str, Any] = {
        "ok": bool(report.get("ok")),
        "violations": report.get("violations") or [],
        "foreign_key_check": report.get("foreign_key_check") or [],
        "sqlite_integrity": report.get("sqlite_integrity"),
        "jobs_by_state": jobs,
        "open_inbox": _scalar(conn, "SELECT COUNT(*) FROM inbox_items WHERE state='open'"),
        "raw_files": [dict(r) for r in conn.execute(
            "SELECT storage_state, COUNT(*) AS n FROM raw_files GROUP BY storage_state")],
    }
    if project_id is not None:
        data["project_id"] = project_id
        data["untested_entities"] = _scalar(
            conn,
            "SELECT COUNT(*) FROM entities e WHERE e.project_id=? AND e.is_current=1"
            " AND e.merged_into IS NULL AND NOT EXISTS (SELECT 1 FROM observations o"
            "   WHERE o.entity_id=e.id AND o.is_current=1)", project_id)
    return ok(data)


@router.get("/inbox", summary="Inbox（默认只看 open）")
def list_inbox(project: str | None = None, state: str = "open", limit: int = 100,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    rows = conn.execute(
        "SELECT id, kind, subject_kind, subject_id, severity, state, payload_json,"
        " occurrence_count, created_at, last_seen_at, created_by_job_id"
        " FROM inbox_items WHERE project_id=? AND state=? ORDER BY"
        " CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
        " LIMIT ?", (project_id, str(state), _page_size(limit))).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = _json_or_none(item.pop("payload_json"))
        items.append(item)
    return ok(items, count=len(items), project_id=project_id)


class ResolveBody(BaseModel):
    state: str = "resolved"
    resolution: dict[str, Any] | None = None


@router.post("/inbox/{item_id}/resolve", summary="裁决一个 Inbox 项")
def resolve_inbox(item_id: int, body: ResolveBody, request: Request) -> dict[str, Any]:
    if body.state not in ("resolved", "dismissed"):
        raise ApiError(400, "E_BAD_STATE",
                       f"state 只能是 resolved / dismissed，收到 {body.state!r}")
    ctx = _ctx(request)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        from ..core.manual import ManualWriter
        writer = ManualWriter(conn_, actor="api")
        writer.resolve_inbox(item_id=item_id, resolution=body.resolution,
                             state=body.state)
        return {"item_id": item_id, "state": body.state}

    return ok(ctx.writer.submit(f"resolve inbox {item_id}", task).result(timeout=30))


@router.get("/activity", summary="活动日志（agent 干了什么，这里看得到）")
def list_activity(limit: int = 100, action: str | None = None, job_id: int | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    where, params = ["1=1"], []
    if action:
        where.append("action=?")
        params.append(str(action))
    if job_id is not None:
        where.append("job_id=?")
        params.append(int(job_id))
    rows = conn.execute(
        "SELECT id, at, actor, action, target_kind, target_id, job_id, detail"
        f" FROM activity_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        (*params, _page_size(limit))).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["detail"] = _json_or_none(item["detail"])
        items.append(item)
    return ok(items, count=len(items))


# ---------------------------------------------------------------------------
# 导出 / agent 上下文
# ---------------------------------------------------------------------------


@router.get("/export", summary="导出（json|csv|domains|markdown）")
def export(project: str | None = None, format: str = "json",
           conn: sqlite3.Connection = Depends(get_conn)):
    project_id = _resolve_project(conn, project)
    fmt = str(format).lower()
    if fmt not in ("json", "csv", "domains", "markdown"):
        raise ApiError(400, "E_BAD_FORMAT", f"不支持的导出格式 {format!r}",
                       hint="支持 json / csv / domains / markdown",
                       supported=["json", "csv", "domains", "markdown"])
    rows = conn.execute(
        "SELECT e.id, e.kind, e.lifecycle,"
        " (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id"
        "   AND k.project_id=e.project_id AND k.canonical=1"
        "   ORDER BY k.entity_key LIMIT 1) AS entity_key,"
        " o.semantic_field, o.value, o.confidence, o.source_count"
        " FROM entities e LEFT JOIN observations o"
        "   ON o.entity_id=e.id AND o.is_current=1"
        " WHERE e.project_id=? AND e.is_current=1 AND e.merged_into IS NULL"
        " ORDER BY e.id, o.semantic_field", (project_id,)).fetchall()

    entities: dict[int, dict[str, Any]] = {}
    for row in rows:
        eid = int(row["id"])
        entity = entities.setdefault(eid, {
            "id": eid, "entity_key": row["entity_key"], "kind": str(row["kind"]),
            "lifecycle": str(row["lifecycle"]), "fields": []})
        if row["semantic_field"] is not None:
            entity["fields"].append({"field": str(row["semantic_field"]),
                                     "value": str(row["value"]),
                                     "confidence": row["confidence"],
                                     "source_count": int(row["source_count"])})
    items = list(entities.values())
    if fmt == "json":
        return ok(items, project_id=project_id, count=len(items))
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["entity_id", "entity_key", "kind", "field", "value",
                         "confidence"])
        for entity in items:
            for field in entity["fields"]:
                writer.writerow([entity["id"], entity["entity_key"], entity["kind"],
                                 field["field"], field["value"], field["confidence"]])
        return PlainTextResponse(buffer.getvalue(), media_type="text/csv; charset=utf-8")
    if fmt == "domains":
        hosts = sorted(str(e["entity_key"]) for e in items if e["entity_key"])
        return PlainTextResponse("\n".join(hosts) + ("\n" if hosts else ""),
                                 media_type="text/plain; charset=utf-8")
    lines = [f"# 导出（project {project_id}）", f"", f"共 {len(items)} 个实体。", ""]
    for entity in items:
        lines.append(f"## {entity['entity_key']}（{entity['kind']}）")
        for field in entity["fields"]:
            lines.append(f"- {field['field']}: {field['value']}"
                         f"（{field['confidence']}）")
        lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")


@router.get("/agent/context", summary="★ 给 agent 的上下文包（Markdown + JSON）")
def agent_context(entity_id: int, depth: int = 1, project: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    detail = get_entity(entity_id, project=project, conn=conn)
    project_id = int(detail["meta"]["project_id"])
    data = detail["data"]
    neighbour_ids = _neighbour_ids(conn, project_id, entity_id, depth)
    neighbours = _graph_payload(conn, project_id, lens=None, kind=None, q=None,
                                limit=max(len(neighbour_ids), 1),
                                seeds=neighbour_ids)
    open_conflicts = _scalar(conn, "SELECT COUNT(*) FROM inbox_items WHERE project_id=?"
                                  " AND state='open'", project_id)
    untested = [dict(r) for r in conn.execute(
        "SELECT e.id, (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id"
        " AND k.project_id=e.project_id AND k.canonical=1 LIMIT 1) AS entity_key,"
        " e.kind FROM entities e WHERE e.project_id=? AND e.is_current=1"
        " AND e.merged_into IS NULL AND NOT EXISTS (SELECT 1 FROM observations o"
        "   WHERE o.entity_id=e.id AND o.is_current=1) ORDER BY e.id LIMIT 20",
        (project_id,))]
    payload = {"entity": data["entity"], "observations": data["observations"],
               "relations": data["relations"], "tags": data["tags"],
               "manual": data["manual"], "open_inbox": data["open_inbox"],
               "neighbours": neighbours["nodes"], "edges": neighbours["edges"],
               "project": {"id": project_id, "open_inbox": open_conflicts,
                           "untested_entities": untested}}
    return ok({"json": payload, "markdown": _context_markdown(payload)},
              project_id=project_id, depth=depth)


def _context_markdown(payload: dict[str, Any]) -> str:
    entity = payload["entity"]
    lines = [f"# {entity.get('entity_key')}（{entity.get('kind')}）", ""]
    lines.append(f"- 实体 id：{entity.get('id')}；生命周期：{entity.get('lifecycle')}")
    if payload["tags"]:
        lines.append(f"- 标签：{', '.join(payload['tags'])}")
    if payload["manual"]["protected_fields"]:
        lines.append(f"- 人工保护字段：{', '.join(payload['manual']['protected_fields'])}")
    lines += ["", "## 观测"]
    if payload["observations"]:
        for obs in payload["observations"]:
            lines.append(f"- {obs['semantic_field']}: {obs['value']}"
                         f"（{obs['confidence']}，{obs['source_count']} 个来源）")
    else:
        lines.append("- （无）—— 这是一个**没测过**的实体")
    lines += ["", "## 关系"]
    if payload["relations"]:
        for rel in payload["relations"]:
            arrow = "→" if rel["direction"] == "out" else "←"
            lines.append(f"- {arrow} {rel['rel']} {rel['other_key']}"
                         f"（{rel['confidence']}）")
    else:
        lines.append("- （无）")
    lines += ["", "## 未决"]
    if payload["open_inbox"]:
        for item in payload["open_inbox"]:
            lines.append(f"- [{item['severity']}] {item['kind']}：{item['payload']}")
    else:
        lines.append("- （无）")
    lines += ["", "## 这个项目里还有什么没测",
              f"- 未决 Inbox 项共 {payload['project']['open_inbox']} 条",
              f"- 一条观测都没有的实体（前 20 个）："]
    for row in payload["project"]["untested_entities"]:
        lines.append(f"  - {row['entity_key']}（{row['kind']}）")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 尚未实现的端点 —— 响亮地 501（见模块文档）
# ---------------------------------------------------------------------------


@router.get("/search", summary="搜索（§8，尚未实现）")
def search(q: str = "") -> dict[str, Any]:
    raise not_implemented(f"搜索（q={q!r}）", plan="计划书 §8（查询与搜索）")


@router.get("/diff", summary="增量 diff（§8，尚未实现）")
def diff(since: str = "last") -> dict[str, Any]:
    raise not_implemented(f"增量 diff（since={since!r}）", plan="计划书 §8 的增量 diff")


@router.post("/paste", summary="粘贴导入（尚未实现）")
def paste() -> dict[str, Any]:
    raise not_implemented("粘贴导入", plan="2.6 的写路径 + 阶段 3 的解析器扩张")


@router.get("/pipelines", summary="管线列表（尚未实现）")
def list_pipelines() -> dict[str, Any]:
    raise not_implemented("管线列表", plan="2.6（管线定义目前只有 core.pipeline 的 YAML 入口）")


@router.post("/pipelines", summary="建管线（尚未实现）")
def create_pipeline() -> dict[str, Any]:
    raise not_implemented("建管线", plan="2.6")


@router.post("/pipelines/{pipeline_id}/run", summary="跑管线（尚未实现）")
def run_pipeline(pipeline_id: str) -> dict[str, Any]:
    raise not_implemented(f"跑管线 {pipeline_id!r}",
                          plan="2.6（现在请用 core.pipeline 的 YAML 入口）")
