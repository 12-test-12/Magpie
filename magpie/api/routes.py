"""`/api/v1` 的路由（计划书 §6.2 / 2.7 任务 1）。

**这个模块里没有一条 SQL —— 读的没有，写的更没有。** 所有写都
`ctx.writer.submit(...)` 排进单写队列（2.7 任务 2），读走每请求一条自己的连接
（WAL 下读写不互相阻塞）并委托给 `magpie/core/query.py`。

两条纪律都不是洁癖:

  * **写**：R1"写入口唯一"的全部价值，就在于**任何时刻只有一个写者**，而它一旦
    被某条"顺手 UPDATE 一下"的路由破坏，就没有任何测试能替你发现。
  * **读**：3.5 还掉的那笔债（`cli/read.py` 与这里的查询是同一份语义的两份实现）。
    两份实现不会同时出错，也不会以同样的方式出错 —— 而"同一份数据、两种答案"
    没有任何症状，直到有人按错的那一半行动。`check.py` 的 `SQL-FREE` 红线静态
    扫本文件与 `cli/read.py`，出现 SQL 字面量即整轮变红。

查询层的失败是 `query.QueryError`（core 不认识 HTTP 状态码），本模块用
`@_translating` 把它翻成 `ApiError` —— 映射表在下面一处，`code` 仍然只有 core
一份。

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
import functools
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core import ingest, ops, query, store
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

#: 页大小上限/默认值的声明处是 `core/query.py`（CLI 与 API 各写一个就是两个
#: 真相）。这里只做转发，好让老的 `routes.MAX_PAGE` 引用照旧可用。
MAX_PAGE = query.MAX_PAGE
DEFAULT_PAGE = query.DEFAULT_PAGE

#: 列表端点支持的过滤条件。**唯一的声明处**也在 core —— 报错信息从它来。
_ENTITY_FILTERS = query.ENTITY_FILTERS

#: `query.QueryError.code` → HTTP 状态码。与 `cli/read.py` 的 `QUERY_EXIT` 是
#: **同一条映射的两侧**（`envelope.EXIT_TO_HTTP` 要求它们一一对应）：
#: 退出码 1 ↔ 400、2 ↔ 404。没登记的 code 按 404 处理，与"没登记的 code 按
#: 数据错（2）处理"是同一个保守选择。
_QUERY_STATUS: dict[str, int] = {
    "E_UNSUPPORTED_FILTER": 400,
    "E_BAD_LIMIT": 400,
    "E_BAD_CURSOR": 400,
    "E_BAD_PRODUCT": 400,
    "E_BAD_ENDPOINT": 400,
    "E_BAD_CIDR": 400,
    "E_BAD_STATE": 400,
    "E_BAD_DEPTH": 400,
    "E_LENS_NOT_FOUND": 404,
    "E_ENTITY_NOT_FOUND": 404,
    "E_JOB_NOT_FOUND": 404,
    "E_PROJECT_NOT_FOUND": 404,
}

#: 路由上的读查询把 `QueryError` 翻成 `ApiError` 的**唯一装饰器**。
#:
#: 为什么是装饰器而不是逐条 `try/except`：14 条路由各写一次的后果不是"多几行"，
#: 而是**总会漏一条** —— 漏掉的那条会把一个 `QueryError` 变成 500，而它的
#: 语义本来是 404/400。`functools.wraps` 保住了签名，FastAPI 的参数注入照旧
#: （它读 `inspect.signature`，而那个函数跟随 `__wrapped__`）。
def _translating(route: Callable) -> Callable:
    @functools.wraps(route)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return route(*args, **kwargs)
        except query.QueryError as exc:
            raise _api_error(exc) from exc
    return wrapper


def _api_error(exc: query.QueryError) -> ApiError:
    return ApiError(_QUERY_STATUS.get(exc.code, 404), exc.code, exc.message,
                    hint=exc.hint, **exc.details)


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

    "怎么找"是 `query.project_id_for`（与 CLI 的 `--project` 同一段实现）；
    "找不到时回什么"留在这一侧 —— HTTP 404 ↔ 退出码 2，两边的证据形状也不同。
    """
    if project is None:
        rows = query.projects(conn)
        if len(rows) == 1:
            return int(rows[0]["id"])
        raise ApiError(400, "E_PROJECT_REQUIRED",
                       "这个库里不止一个项目（或一个都没有），必须显式给 project",
                       hint="加上 ?project=<id 或名字>；用 GET /api/v1/projects 查",
                       projects=[str(r["name"]) for r in rows])
    spec = str(project)
    found = query.project_id_for(conn, spec)
    if found is None:
        raise ApiError(404, "E_PROJECT_NOT_FOUND", f"项目 {spec!r} 不存在")
    return int(found)


def _unsupported(filters: dict[str, Any]) -> None:
    """沉默地忽略一个过滤条件 = 给 agent 一个假答案。**必须响亮地拒绝。**

    判定、报错文本与 `supported` 都由 `query.reject_unsupported` 给 —— 这里只
    把 `QueryError` 翻成 `ApiError`，让"支持哪些过滤条件"只有一个答案。
    """
    try:
        query.reject_unsupported(filters)
    except query.QueryError as exc:
        raise _api_error(exc) from exc


def _json_or_none(raw: Any) -> Any:
    return query.json_or_none(raw)


# ---------------------------------------------------------------------------
# 元信息 / 项目
# ---------------------------------------------------------------------------


@router.get("/meta", summary="版本 / schema / 类型登记表 / 可用解析器")
def get_meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    from .. import __version__

    # `/meta` 的 kinds 不带 fixture_path（它是**登记时**的证据，不是运行时要用的
    # 东西）；`magpie schema` 带。两者取的是同一份 `query.kinds`，各自丢字段。
    kinds = [{k: v for k, v in row.items() if k != "fixture_path"}
             for row in query.kinds(conn)]
    try:
        parsers, source_pkg = ingest.load_parsers()
        parser_list = [{"tool": str(c.tool), "format_version": str(c.format_version)}
                       for c in parsers]
    except Exception as exc:                       # 解析器坏了不该让 /meta 也坏
        source_pkg, parser_list = f"<{type(exc).__name__}: {exc}>", []
    return ok({
        "magpie_version": __version__,
        "schema_version": query.schema_version(conn),
        "api_prefix": API_PREFIX,
        "kinds": kinds,
        "relation_verbs": query.relation_verbs(conn),
        "parsers": parser_list,
        "parser_source": source_pkg,
        "counts": query.metric_counts(conn),
    })


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
    if query.project_named(conn, body.name) is not None:
        raise ApiError(409, "E_PROJECT_EXISTS", f"项目 {body.name!r} 已存在")
    ctx = _ctx(request)
    name, scope = body.name, dict(body.scope)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        project_id = ops.create_project(conn_, name, scope)
        return {"id": project_id, "name": name, "scope": scope}

    return ok(ctx.writer.submit(f"create project {name}", task).result(timeout=30))


@router.get("/projects", summary="项目列表")
def list_projects(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    items = query.projects_detail(conn)
    return ok(items, count=len(items))


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


@router.get("/entities", summary="实体列表（键集游标分页）+ §3.5 的具名查询")
@_translating
def list_entities(project: str | None = None,
                  kind: str | None = None,
                  lens: str | None = None,
                  q: str | None = None,
                  tag: str | None = None,
                  lifecycle: str | None = None,
                  product: str | None = None,
                  version: str | None = None,
                  endpoint: str | None = None,
                  severity: str | None = None,
                  state: str | None = None,
                  in_cidr: str | None = None,
                  cursor: str | None = None,
                  limit: int = DEFAULT_PAGE,
                  review_status: str | None = None,
                  in_scope: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """实体列表。`product` / `endpoint` / `severity` / `state` / `in_cidr` 是
    §3.5 的**具名查询**（`query.dispatch` 决定这组条件在问哪一问）—— 与
    `magpie ls` 的同名 flag 一字不差，两边走同一段实现。

    `meta.notes` 是**诚实的边界**：产品实体根本不存在、网段里的候选是算出来的
    而不是连出来的…… 一个空列表必须带着"为什么是空的"，否则它会被读成
    "筛过了，就是没有"（这正是 `review_status` 那条纪律的同一个理由）。
    """
    _unsupported({"review_status": review_status, "in_scope": in_scope})
    # 用法错先于数据错 —— 与 `magpie ls` 同一步、同一顺序（见
    # `query.check_page_filters`）。
    try:
        query.check_page_filters(limit=limit, cursor=cursor)
    except query.QueryError as exc:
        raise _api_error(exc) from exc
    project_id = _resolve_project(conn, project)
    result = query.dispatch(
        conn, project_id, kind=kind, lens=lens, q=q, tag=tag, lifecycle=lifecycle,
        product=product, version=version, endpoint=endpoint, severity=severity,
        state=state, in_cidr=in_cidr, cursor=cursor, limit=limit)
    return ok(result["entities"], count=result["count"],
              next_cursor=result["next_cursor"], project_id=project_id,
              query=result["query"], filters={"kind": kind, "q": q, "tag": tag,
                                              "lifecycle": lifecycle},
              notes=result["notes"])


@router.get("/entities/{entity_id}", summary="实体详情（观测 / 关系 / 标签 / Inbox）")
@_translating
def get_entity(entity_id: int, project: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    return ok(query.entity_detail(conn, project_id, entity_id),
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
    # 不存在 → 404（**在排队之前**：一个注定失败的作业不该占一次写者）
    try:
        query.entity_row(conn, project_id, entity_id)
    except query.QueryError as exc:
        raise _api_error(exc) from exc
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
        return {"entity_id": entity_id_,
                "tags": query.tags_of(conn_, project_id, entity_id_),
                "protected_fields": query.manual_keys(conn_, project_id, entity_id_)}

    return ok(ctx.writer.submit(f"patch entity {entity_id}", task).result(timeout=30),
              project_id=project_id)


class MergeBody(BaseModel):
    survivor_key: str
    member_keys: list[str] = Field(default_factory=list)
    dry_run: bool = False


@router.post("/entities/{entity_id}/merge", summary="合并（identity overlay）")
@_translating
def merge_entity(entity_id: int, body: MergeBody, request: Request,
                 project: str | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    target = query.entity_row(conn, project_id, entity_id)
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


@router.get("/graph", summary="图谱（节点 + 边 + 布局）")
@_translating
def get_graph(project: str | None = None, lens: str | None = None,
              kind: str | None = None, q: str | None = None,
              limit: int = 500,
              depth: int | None = None,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """注意 `_unsupported({"depth": depth})`：`depth` 在 `/graph` 上**仍然**是
    "传了就 400"（它只在 `/graph/{id}/neighbors` 上有意义）—— 收下它再静默
    忽略，调用方就会以为按深度筛过了。"""
    _unsupported({"depth": depth})
    project_id = _resolve_project(conn, project)
    payload = query.graph_payload(conn, project_id, lens=lens, kind=kind, q=q,
                                  limit=query.page_size(limit))
    payload["project_id"] = project_id
    return ok(payload, counts={"nodes": len(payload["nodes"]),
                               "edges": len(payload["edges"])})


@router.get("/graph/{entity_id}/neighbors", summary="邻居（BFS，depth 层）")
@_translating
def get_neighbors(entity_id: int, depth: int = 1, project: str | None = None,
                  limit: int = 300,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    query.entity_row(conn, project_id, entity_id)
    ids = query.neighbour_ids(conn, project_id, entity_id, depth, limit=limit)
    payload = query.graph_payload(conn, project_id, lens=None, kind=None, q=None,
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


@router.get("/lenses/{lens}", summary="镜头页（assets|services|api|vuln|traffic|graph）")
@_translating
def get_lens(lens: str, project: str | None = None,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """镜头页的类型徽标与计数。

    与 `magpie lens` 调的是**同一个** `query.lens_view` —— GUI 的镜头页与 CLI
    的类型计数因此不可能给出不同的数（§3.5）。`total` 是这个镜头的实体总数，
    界面直接用它，不必自己把 kinds 加起来（加错了一次就少一个数）。
    """
    project_id = _resolve_project(conn, project)
    view = query.lens_view(conn, project_id, lens)
    return ok({"lens": view["lens"], "kinds": view["kinds"]}, project_id=project_id,
              total=sum(k["count"] for k in view["kinds"]))


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
DEFAULT_JOB_LIMIT = query.DEFAULT_JOB_LIMIT


def _decorate_job(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    """给一行作业补上"现在能不能 undo/redo"与计数。

    `applied` 是 SQLite 里的 0/1，出信封前折成布尔 —— JSON 的消费者不该
    自己记得"1 是真"。
    """
    item["applied"] = bool(item["applied"])
    item["counts"] = query.job_counts(conn, [int(item["id"])])[int(item["id"])]
    item["can_undo"] = bool(item["applied"])
    item["can_redo"] = not bool(item["applied"])
    return item


@router.get("/jobs", summary="作业历史（最近的在前）")
@_translating
def list_jobs(project: str | None = None, state: str | None = None,
              applied: int | None = None, limit: int = DEFAULT_JOB_LIMIT,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project) if project is not None else None
    if applied is not None:
        # 布尔从查询串进来只可能是 "0"/"1"/"true"/"false"，而 FastAPI 对
        # int 的解析会把 "true" 变成 422（带一页 pydantic 报错）。收下来自己
        # 判定，是为了让错误信息说人话。
        applied = 1 if applied else 0
    rows = query.list_jobs(conn, project_id=project_id, state=state,
                           applied=applied, limit=limit)
    items = [_decorate_job(conn, row) for row in rows]
    return ok(items, count=len(items))


@router.get("/jobs/{job_id}", summary="作业详情（含 explain_job_scope）")
@_translating
def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = _decorate_job(conn, query.job_row(conn, job_id))
    # scope 的解释**必须**来自数据层：`interpret_with` 是 historical_scope 时，
    # 界面要显示的是"当时的 scope"，不是现在这份。
    scope = ops.explain_job_scope(conn, job_id)
    return ok({"job": job, "scope": scope,
               "activity": query.job_activity(conn, job_id),
               "can_undo": job["can_undo"], "can_redo": job["can_redo"]},
              project_id=int(job["project_id"]))


class JobAction(BaseModel):
    """undo / redo 的体。

    `dry_run=True` 时输出与真跑**同构**（只有 COMMIT→ROLLBACK 之差），
    界面因此可以用同一段代码渲染预览与结果 —— 见本节模块注释第 2 条。
    """

    dry_run: bool = False


@router.post("/jobs/{job_id}/undo", summary="撤销一个作业（dry_run = 干跑预览）")
@_translating
def undo_job(job_id: int, body: JobAction, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = query.job_row(conn, job_id)
    ctx, dry = _ctx(request), bool(body.dry_run)

    def task(conn_: sqlite3.Connection) -> dict[str, Any]:
        return {"op": "undo", "job_id": job_id, "dry_run": dry,
                "report": ops.undo(conn_, job_id, actor="api", dry_run=dry)}

    label = f"{'dry-run ' if dry else ''}undo job {job_id}"
    return ok(ctx.writer.submit(label, task).result(timeout=120),
              project_id=int(job["project_id"]))


@router.post("/jobs/{job_id}/redo", summary="复原一个被撤销的作业（dry_run = 干跑预览）")
@_translating
def redo_job(job_id: int, body: JobAction, request: Request,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    job = query.job_row(conn, job_id)
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
@_translating
def doctor(project: str | None = None,
           conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """体检。判断全部来自数据层（`store.integrity_check` 等），取数全部走
    `query` —— 这个路由只负责把两块拼起来。

    `?project=` 找不到时**故意不报错**：体检是"这个库怎么样"，它必须在那一个
    项目都用不了的时候也能跑完（`data.untested_entities` 才需要项目）。
    这与"静默忽略一个过滤条件"不是一回事：这里没有筛任何东西，只是少算一项，
    而少算的那一项**在输出里没有出现**（不是 0）。
    """
    report = store.integrity_check(conn)
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
        "jobs_by_state": query.jobs_by_state(conn),
        "open_inbox": query.metric_counts(conn)["open_inbox"],
        "raw_files": query.raw_file_states(conn),
    }
    if project_id is not None:
        data["project_id"] = project_id
        data["untested_entities"] = query.untested_count(conn, project_id)
        # 体检页的五类（§7.4）。**与 `magpie doctor` 同一份实现** —— 两处都只是
        # `query.health_report` 的一次调用（`core/query.py` 是唯一实现），
        # 所以"GUI 说 3、CLI 说 2"这种分歧在结构上不可能出现。
        data["health"] = query.health_report(conn, project_id)
    return ok(data)


@router.get("/inbox", summary="Inbox（默认只看 open）")
@_translating
def list_inbox(project: str | None = None, state: str = "open", limit: int = 100,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project_id = _resolve_project(conn, project)
    items = query.inbox_items(conn, project_id, state=state, limit=limit)
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
@_translating
def list_activity(limit: int = 100, action: str | None = None, job_id: int | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    items = query.activity_log(conn, limit=limit, action=action, job_id=job_id)
    return ok(items, count=len(items))


# ---------------------------------------------------------------------------
# 导出 / agent 上下文
# ---------------------------------------------------------------------------


@router.get("/export", summary="导出（json|csv|domains|markdown）")
@_translating
def export(project: str | None = None, format: str = "json",
           conn: sqlite3.Connection = Depends(get_conn)):
    """导出。**取数只有一份**（`query.export_entities`），四种格式是四种**渲染** ——
    一个按格式各查一次的导出迟早会把 `domains` 的那种排序悄悄换成另一种。"""
    project_id = _resolve_project(conn, project)
    fmt = str(format).lower()
    if fmt not in ("json", "csv", "domains", "markdown"):
        raise ApiError(400, "E_BAD_FORMAT", f"不支持的导出格式 {format!r}",
                       hint="支持 json / csv / domains / markdown",
                       supported=["json", "csv", "domains", "markdown"])
    items = query.export_entities(conn, project_id)
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
@_translating
def agent_context(entity_id: int, depth: int = 1, project: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """它**复用详情那一条路**（`query.entity_detail`），不另取一份 —— 上下文包
    里说"这个实体有 3 条观测"而详情页说 4 条，是最难查的一类不一致。"""
    project_id = _resolve_project(conn, project)
    data = query.entity_detail(conn, project_id, entity_id)
    neighbour_ids = query.neighbour_ids(conn, project_id, entity_id, depth)
    neighbours = query.graph_payload(conn, project_id, lens=None, kind=None, q=None,
                                     limit=max(len(neighbour_ids), 1),
                                     seeds=neighbour_ids)
    open_conflicts = query.open_inbox_count(conn, project_id)
    untested = query.untested_entities(conn, project_id, limit=20)
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
