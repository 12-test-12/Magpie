"""只读查询的唯一实现：镜头、具名查询，以及"谁在问同一件事"的收敛点（§3.5）。

为什么这个模块存在
------------------
`magpie/cli/read.py` 的文件头从 2.2 起就记着这笔债：`ls` / `show` 的查询语句与
`api/routes.py` 里的那几条是**同一份语义的两份实现**。两份实现不会同时出错，也
不会以同样的方式出错 —— 而"同一份数据、两种答案"之所以是这里最怕的一类缺陷，
是因为它没有任何症状：两边都跑得通，只是答得不一样，直到有人按错的那一半行动。

所以本模块是这条路的**唯一实现**（计划书 §3.5 的"GUI 与 CLI 共用同一实现"）。
`cli/read.py` 与 `api/routes.py` 各自不再保留一条 SQL —— 这件事不靠自觉：
`check.py` 的 `SQL-FREE` 红线静态扫这两个文件，出现 SQL 字面量即整轮变红。

四条纪律
--------

1. **core 只用标准库**（R6）。`sqlite3` / `json` / `ipaddress` 都在标准库里，
   所以"把 SQL 收进 core"不会引入任何依赖。core 里碰 `ipaddress` 的只有两处，
   两处都是**判断**而不是归一化：这里的网段包含（`assets_in_network`）与
   `scope.py` 的 scope 求值。解析器不许碰它（PARSE-NORM 守的是"归一化只有
   一处"，而大小写折叠/地址规范化的唯一实现是 `normalize.py`）。

2. **身份规则不在这里重写。** 产品键走 `httpx.product_key` + `normalize.product_name`，
   接口键走 `normalize.endpoint_key`，网段走 `normalize.try_cidr` —— 它们各自是
   `kind_registry.identity_rule` 里那一条规则的**唯一实现**（004 的迁移注释把这条
   说死了：两份实现迟早在别名表/大小写上分叉，而分叉之后没有答案的是"哪个才对"）。
   本模块只**调用**它们，于是"用户在命令行里写的 nginx"与"解析器写进库里的 nginx"
   算出同一个键。

3. **错误是 `QueryError`，不是 `CliError` / `ApiError`。** 那两者各自带着一个入口
   的退出码与 HTTP 状态（`cli/envelope.py` / `api/errors.py`），core 依赖任何一个
   都会把读这条路绑死在那个入口上。这里只给**稳定的 code + 证据**，由调用方把它
   映射成自己的那一套 —— 映射表两边各写一次，而 code 只有一份。

4. **过滤条件不许被静默忽略。** 传了不支持的过滤条件要**响亮地拒绝**
   （`E_UNSUPPORTED_FILTER`）—— 一个被忽略的过滤条件会让调用方以为筛过了，那比
   报错坏得多。声明处只有 `ENTITY_FILTERS` 一处，报错信息从它来。

具名查询（§3.5 的五问）
-----------------------
"具名查询"在 CLI 与 API 上的形态不是一套新语法（那是 §8 的事），而是 **`ls` /
`GET /entities` 上几个具名的过滤条件**。`dispatch()` 是唯一的分发点，它把这组
过滤条件映射成下面某一个具名函数 —— 两个入口因此不可能选出不同的查询：

    §3.5 的问题            filters                    → 函数
    ────────────────────────────────────────────────────────────────
    谁还在用 Nginx 1.24    product=nginx@1.24.0       → services_running
    按 severity/state 筛   kind=vuln severity=high    → vulns
    某个接口归谁           endpoint=<URL 或 key>      → endpoint_owners
    哪些资产落在网段里     in_cidr=10.0.0.0/24        → assets_in_cidr
    镜头里的 kind 计数     （GET /lenses/{lens}）     → lens_view
    通用列表               kind= / q= / tag= …        → entities_page
    体检页的五类           各一组 inbox kind           → health_report

后四个都是 `entities_page` 的**定型调用**，不是另写一份 SQL。分开成具名函数有
两个实际理由：`query` 这个名字要能写进输出（调用方据此知道刚才问的是哪一问），
以及"产品实体不存在"这类**诚实的空结果**需要一个地方附加 `notes`。
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from . import normalize
from . import scope as scope_module
from .parsers.httpx import product_key

# ---------------------------------------------------------------------------
# 契约常量 —— **唯一的声明处**
# ---------------------------------------------------------------------------

#: 003 冻结的六个镜头 id 的全部（`kind_registry.lens` 的 CHECK 就是这六个）。
#: 校验本身走 `kind_registry` 而不是这张表：镜头由**登记的 kind** 决定，库里
#: 没有 kind 的镜头（比如还没有解析器产出的 `traffic`）不是一个可问的镜头。
LENSES = ("graph", "assets", "services", "api", "traffic", "vuln")

#: `ls` / `GET /entities` 支持的过滤条件。**唯一声明处** —— 报错信息也从它来
#: （2.2 与 2.7 各有一份同名常量，本步收敛成这一份）。
#:
#: 后四个是具名查询的入口（见模块文档的表）：`product` / `endpoint` 走关系
#: （`runs` / `served_by`），`severity` / `state` 走观测，`in_cidr` 走地址包含。
#: `review_status` / `in_scope` **仍然不在里面** —— 它们需要"人工状态"与 scope
#: 求值，传了要响亮地拒绝（见 `reject_unsupported`）。
ENTITY_FILTERS = ("kind", "lens", "q", "tag", "lifecycle", "product", "endpoint",
                  "severity", "state", "in_cidr", "cursor", "limit")

#: 分页：与 2.7 的 `MAX_PAGE` / `DEFAULT_PAGE` 同值。上限只有一处定义，
#: 两侧各写一个就是两个真相。
MAX_PAGE = 500
DEFAULT_PAGE = 50

#: 作业历史默认返回多少条。与实体列表的 50 不同：作业是"最近发生了什么"，
#: 一次导入就会产生 N 条（每个文件一条），翻页在这里比多给几条更烦人。
DEFAULT_JOB_LIMIT = 100

#: 列表类的体检项一次最多摆多少条（未测实体 / Inbox / 活动）。
DEFAULT_HEALTH_LIMIT = 100

#: `depth` 的上限。每一层都是一次 `IN (...)`，层数比宽度更快地把查询变成全表
#: 扫描。需要的层数超过 4 时，该问的是"你要那张网，还是要那张图的局部"。
MAX_DEPTH = 4


class QueryError(ValueError):
    """具名查询的**可消费**失败：稳定的 `code` + 给人和 agent 看的证据。

    刻意不是 `CliError` / `ApiError`（见模块文档第 3 条）。`details` 里的东西
    原样进错误信封的 JSON —— `supported` / `rejected` / `known_lenses` 都是
    调用方与 agent 据以修复的字段，不是装饰。
    """

    def __init__(self, code: str, message: str, *, hint: str | None = None,
                 **details: Any) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.hint = hint
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.details)
        return out


# ---------------------------------------------------------------------------
# 公共零件
# ---------------------------------------------------------------------------


def json_or_none(raw: Any) -> Any:
    """JSON 列 → 对象；读不懂就**原样返回**。

    读不懂的 JSON 是一份坏数据（体检会单独报），但"某一行坏"不该让整份详情
    崩掉 —— 崩掉之后连"这一行是坏的"都看不到了，而原样返回至少把它摆出来了。
    """
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


def page_size(limit: int) -> int:
    """`limit` → 真正的页大小。上限在 core，见 `MAX_PAGE`。"""
    if limit is None:
        return DEFAULT_PAGE
    if int(limit) < 1:
        raise QueryError("E_BAD_LIMIT", f"limit 必须 >= 1，收到 {limit}")
    return min(int(limit), MAX_PAGE)


def parse_cursor(cursor: str | None) -> int | None:
    """键集游标：上一页末尾的**实体 id**。与 2.7 的 `?cursor=` 同一语义。"""
    if cursor in (None, "", "null"):
        return None
    if not str(cursor).isdigit():
        raise QueryError("E_BAD_CURSOR",
                         f"cursor 必须是上一页末尾的实体 id，收到 {cursor!r}")
    return int(cursor)


def check_page_filters(*, limit: int = DEFAULT_PAGE,
                       cursor: str | None = None) -> None:
    """分页参数**先于**项目解析校验。调用方在 `reject_unsupported` 之后、
    `resolve_project` 之前调它。

    顺序不是风格：一个写错的 `limit`/`cursor` 是**用法错**（退出码 1 ↔ 400），
    而"这个库里有几个项目"是**数据错**（2 ↔ 404）。把数据错排在用法错前面，
    `ls --limit 0` 在一个空库上就会报出与用户输入毫无关系的项目错 —— 那是最
    难查的一类报错。两个入口（CLI / API）在这一步上的顺序也必须是同一个，
    否则"同一份数据两种答案"会先在**报错**上出现。

    `entities_page` 内部仍然会再校验一次（它可能被直接调用）。重复是廉价的，
    而顺序是**这里**说的话。
    """
    page_size(limit)
    parse_cursor(cursor)


def reject_unsupported(filters: dict[str, Any]) -> None:
    """沉默地忽略一个过滤条件 = 给 agent 一个假答案。**必须响亮地拒绝。**"""
    unknown = sorted(k for k, v in filters.items() if v not in (None, ""))
    if unknown:
        raise QueryError(
            "E_UNSUPPORTED_FILTER",
            f"本步不支持这些过滤条件：{', '.join(unknown)}",
            hint=f"支持的过滤条件：{', '.join(ENTITY_FILTERS)}。"
                 f"被静默忽略的过滤条件会让调用方以为筛过了 —— 那比报错坏得多。",
            supported=list(ENTITY_FILTERS), rejected=unknown)


# ---------------------------------------------------------------------------
# 行业零件（keys / tags / manual / entity row）
# ---------------------------------------------------------------------------


def keys_of(conn: sqlite3.Connection, project_id: int,
            entity_ids: list[int]) -> dict[int, str]:
    """entity_id → 规范 key。**一次查完，不做 N+1** —— 详情页每多一个邻居就是
    一次往返，而 N+1 在本地回环上也不痛不痒，直到图变大。"""
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


def project_tags(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """项目里的标签词表 + 计数（**只算活标签**：`deleted_at IS NULL`）。

    界面拿它渲染"这个词表里已经有哪些词" —— 与 3.2 的镜头徽标同一条纪律：
    **显示的是库里的数**，不是"界面上见过的那些"。少了它，一个标签一旦从所有
    实体上摘掉，它下一次就只能靠人记得重新敲一遍。

    计数用 `COUNT(DISTINCT entity_id)` 而不是数行：软删的历史行仍然躺在
    `entity_tags` 里（那是审计要的），把它们算进来会让徽标上的数大于
    `GET /entities?tag=X` 返回的实体数 —— 两个数说的是同一件事，就该相等。
    """
    rows = conn.execute(
        "SELECT tag, COUNT(DISTINCT entity_id) AS n FROM entity_tags"
        " WHERE project_id=? AND deleted_at IS NULL GROUP BY tag"
        " ORDER BY n DESC, tag", (project_id,)).fetchall()
    return [{"tag": str(r["tag"]), "count": int(r["n"])} for r in rows]


def layout_rows(conn: sqlite3.Connection, project_id: int,
                entity_ids: list[int]) -> dict[int, dict]:
    """{entity_id: {x, y, collapsed}} —— 只含**已经有行**的那些。

    读侧（`graph_payload`）自带一段同样的查询，但它只取当前这一页节点；
    这里要回答的是另一个问题："这条写入会不会**新建**一行"。`PATCH /graph/layout`
    用它把"对一条不存在的行只写 collapsed"**在排队之前**拒掉 —— 那行会带着
    (0,0) 出生，读侧把它当成"人把这个节点拖到了原点"。
    """
    if not entity_ids:
        return {}
    marks = ",".join("?" * len(entity_ids))
    return {int(r["entity_id"]): {"x": r["x"], "y": r["y"],
                                  "collapsed": bool(r["collapsed"])}
            for r in conn.execute(
                f"SELECT entity_id, x, y, collapsed FROM entity_layout"
                f" WHERE project_id=? AND entity_id IN ({marks})",
                (project_id, *entity_ids))}


def merge_events_of(conn: sqlite3.Connection, project_id: int,
                    entity_id: int) -> list[dict]:
    """这个实体参与过的合并事件（**只列仍然生效的** `state='merged'`）。

    它是 `unmerge` 的入口：撤销一次合并要的是 `event_id`，而 GUI 上人看到的
    只有"这个卡片是合并出来的"。把两端的映射一次查完（含两端各自的 key），
    界面就不必为了一行再发一次请求去看"另一个是谁"。

    已撤销（`state='unmerged'`）的事件不列 —— 它们不是可操作的，而"列出来但
    点了没反应"比不列更坏。
    """
    rows = conn.execute(
        "SELECT ev.id AS event_id, ev.survivor_id, ev.state, ev.reason,"
        "       ev.created_at, ev.created_by_job_id,"
        "       m.entity_id AS member_id, m.revoked_by_job_id"
        "  FROM entity_merge_events ev"
        "  JOIN entity_merge_members m ON m.merge_event_id = ev.id"
        " WHERE ev.project_id=? AND ev.state='merged'"
        "   AND ev.id IN (SELECT merge_event_id FROM entity_merge_members"
        "                  WHERE entity_id=? AND revoked_by_job_id IS NULL)"
        " ORDER BY ev.id DESC, m.entity_id", (project_id, entity_id)).fetchall()
    grouped: dict[int, dict] = {}
    for row in rows:
        event = grouped.setdefault(int(row["event_id"]), {
            "event_id": int(row["event_id"]), "survivor_id": int(row["survivor_id"]),
            "state": str(row["state"]), "reason": str(row["reason"]),
            "created_at": str(row["created_at"]),
            "job_id": None if row["created_by_job_id"] is None
                      else int(row["created_by_job_id"]),
            "member_ids": []})
        event["member_ids"].append(int(row["member_id"]))
    events = sorted(grouped.values(), key=lambda e: e["event_id"], reverse=True)
    ids = sorted({e["survivor_id"] for e in events}
                 | {m for e in events for m in e["member_ids"]})
    # **不能用 `keys_of`**：它只认 `canonical=1` 的 key，而**成员实体的 key 正是
    # 被合并之后丢掉 canonical 标记的那一个** —— 用它去取成员名会得到一串
    # `None`，界面上"这次合并吃掉了谁"就变成一句空话。这里取的是"这个实体叫
    # 什么名字"（展示名），与 `store.display_key` 同一条口径。
    keys: dict[int, str] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for row in conn.execute(
                f"SELECT entity_id, entity_key FROM entity_keys"
                f" WHERE project_id=? AND entity_id IN ({marks})"
                f" ORDER BY canonical DESC, created_at, entity_key",
                (project_id, *ids)):
            keys.setdefault(int(row["entity_id"]), str(row["entity_key"]))
    for event in events:
        event["survivor_key"] = keys.get(event["survivor_id"])
        event["member_keys"] = [keys.get(m) for m in event["member_ids"]
                                if m != event["survivor_id"]]
        # 没有 `can_unmerge` 这一栏：`ops.unmerge` 只认 `event_id`，幸存者还是
        # 成员来撤销都一样。多一个恒为真的布尔位，读的人会以为它有时是假的。
    return events


def manual_keys(conn: sqlite3.Connection, project_id: int, entity_id: int) -> list[str]:
    """人工保护的字段名。`manual_fields` 是**工作区**不是事实区 —— 它由
    `ManualWriter` 写，读它不需要经过单写队列。"""
    row = conn.execute(
        "SELECT keys_json FROM manual_fields WHERE project_id=? AND subject_kind='entity'"
        " AND subject_id=?", (project_id, entity_id)).fetchone()
    keys = json_or_none(None if row is None else row["keys_json"])
    return [str(k) for k in keys] if isinstance(keys, list) else []


def entity_row(conn: sqlite3.Connection, project_id: int, entity_id: int) -> dict:
    row = conn.execute(
        "SELECT id, kind, lifecycle, is_current, merged_into, first_seen, last_seen"
        " FROM entities WHERE id=? AND project_id=?",
        (entity_id, project_id)).fetchone()
    if row is None:
        raise QueryError("E_ENTITY_NOT_FOUND",
                         f"实体 {entity_id} 不在项目 {project_id} 里")
    entity = dict(row)
    entity["entity_key"] = keys_of(conn, project_id, [entity_id]).get(entity_id)
    return entity


def entity_exists(conn: sqlite3.Connection, project_id: int, entity_id: int) -> bool:
    return conn.execute("SELECT 1 FROM entities WHERE id=? AND project_id=?",
                        (entity_id, project_id)).fetchone() is not None


def target_entity_id(conn: sqlite3.Connection, project_id: int,
                     text: str) -> int | None:
    """一个字面量 → entity_id：先查**生效 alias**，再退回 key 表。

    `entity_alias` 上有一条部分唯一索引（`001_init.sql`：
    `(project_id, alias_key) WHERE revoked_by_job_id IS NULL`），所以这里的
    `fetchone()` 是**真的**唯一，不是碰巧唯一 —— 唯一性由数据库保证。

    兜底查 key 表：`key_without_alias` 是体检项会报的一种状态，但在它被报出来
    之前，`show <key>` 不该比数据库更早地失败。
    """
    row = conn.execute(
        "SELECT entity_id FROM entity_alias WHERE project_id=? AND alias_key=?"
        " AND revoked_by_job_id IS NULL", (project_id, text)).fetchone()
    if row is not None:
        return int(row["entity_id"])
    row = conn.execute(
        "SELECT entity_id FROM entity_keys WHERE project_id=? AND entity_key=?",
        (project_id, text)).fetchone()
    return None if row is None else int(row["entity_id"])


def entity_id_for_key(conn: sqlite3.Connection, project_id: int,
                      entity_key: str) -> int | None:
    """规范 key → entity_id。**只走 key 表**（`target_entity_id` 是给用户输入的
    那条路，它多一次 alias 查询）。"""
    row = conn.execute(
        "SELECT entity_id FROM entity_keys WHERE project_id=? AND entity_key=?"
        " AND canonical=1", (project_id, entity_key)).fetchone()
    return None if row is None else int(row["entity_id"])


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


def projects(conn: sqlite3.Connection) -> list[dict]:
    """id + 名字，按 id 排。**"库里有哪些项目"的唯一次查询处。**"""
    return [{"id": int(r["id"]), "name": str(r["name"])}
            for r in conn.execute("SELECT id, name FROM projects ORDER BY id")]


def project_named(conn: sqlite3.Connection, name: str) -> int | None:
    """项目名 → id。**只按名字查** —— 建项目时的重名判定要的是"这个名字被占了
    没有"，而不是"这个字面量能不能解析成一个项目"（那是 `project_id_for`）。"""
    row = conn.execute("SELECT id FROM projects WHERE name=?", (str(name),)).fetchone()
    return None if row is None else int(row["id"])


def projects_detail(conn: sqlite3.Connection) -> list[dict]:
    """项目列表（含 scope 与三项计数）。`GET /projects` 的形状在这里定死：
    每个项目的计数**一次查完**，而不是"先列项目再逐个问它有多大"。"""
    counts: dict[int, dict[str, int]] = {}
    for row in conn.execute(
            "SELECT project_id,"
            " SUM(CASE WHEN is_current=1 THEN 1 ELSE 0 END) AS entities"
            " FROM entities WHERE project_id IS NOT NULL GROUP BY project_id"):
        counts[int(row["project_id"])] = {"entities": int(row["entities"] or 0),
                                          "observations": 0, "relations": 0}
    for table, key in (("observations", "observations"), ("relations", "relations")):
        for row in conn.execute(
                f"SELECT project_id, COUNT(*) AS n FROM {table}"
                f" WHERE is_current=1 GROUP BY project_id"):
            counts.setdefault(int(row["project_id"]),
                              {"entities": 0, "observations": 0, "relations": 0})
            counts[int(row["project_id"])][key] = int(row["n"])

    items = []
    for row in conn.execute(
            "SELECT id, name, status, scope_version, scope_hash, parser_fingerprint,"
            " scope FROM projects ORDER BY id"):
        item = dict(row)
        item["scope"] = json_or_none(item["scope"])
        item["counts"] = counts.get(int(row["id"]),
                                    {"entities": 0, "observations": 0, "relations": 0})
        items.append(item)
    return items


def project_id_for(conn: sqlite3.Connection, spec: str) -> int | None:
    """id 或**完整**名字 → project_id；找不到回 None（由调用方决定报什么错）。

    报错的位置刻意留给调用方：CLI 的 `--project` 与 API 的 `?project=` 在
    "找不到"上给的是不同的东西（退出码 2 ↔ HTTP 404、`candidates` 的形状也
    不同），而**怎么找**只有这一处。
    """
    text = str(spec)
    row = None
    if text.isdigit():
        row = conn.execute("SELECT id FROM projects WHERE id=?", (int(text),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM projects WHERE name=?", (text,)).fetchone()
    return None if row is None else int(row["id"])


# ---------------------------------------------------------------------------
# 镜头
# ---------------------------------------------------------------------------


def kinds_for_lens(conn: sqlite3.Connection, lens: str) -> list[str]:
    """镜头 → 它名下的 kind 列表。空列表 = 这个镜头不存在（或还没有产出者）。"""
    return [str(r["kind"]) for r in conn.execute(
        "SELECT kind FROM kind_registry WHERE lens=? ORDER BY kind", (str(lens),))]


def lens_ids(conn: sqlite3.Connection) -> list[str]:
    """库里真实存在的镜头 id。**不返回 `LENSES` 那张常量表** —— 常量表是
    003 的 CHECK 约束，而"这个镜头现在能问出东西吗"是数据说的事（没有 kind
    的镜头要 404，不返回空列表）。"""
    return [str(r["lens"]) for r in conn.execute(
        "SELECT DISTINCT lens FROM kind_registry ORDER BY lens")]


def lens_view(conn: sqlite3.Connection, project_id: int, lens: str) -> dict:
    """镜头页（§7.4）：这个镜头下每个 kind 的类型徽标与计数。

    计数是**这个项目里当前生效**的实体数（`is_current=1 AND merged_into IS NULL`
    —— 与 `ls` 同一口径），不是 kind 登记表里的声明。
    """
    lenses = lens_ids(conn)
    if str(lens) not in lenses:
        raise QueryError(
            "E_LENS_NOT_FOUND", f"镜头 {lens!r} 没有归属任何 kind",
            hint="镜头由 kind_registry.lens 决定；库里有 kind 的镜头见 known_lenses。",
            known_lenses=lenses)
    kinds = []
    for row in conn.execute(
            "SELECT kind, identity_rule, produced_by FROM kind_registry"
            " WHERE lens=? ORDER BY kind", (str(lens),)):
        kinds.append({"kind": str(row["kind"]),
                      "identity_rule": str(row["identity_rule"]),
                      "produced_by": json_or_none(row["produced_by"]) or [],
                      "count": kind_count(conn, project_id, str(row["kind"]))})
    return {"query": "lens", "lens": str(lens), "kinds": kinds}


def kind_count(conn: sqlite3.Connection, project_id: int, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE project_id=? AND kind=? AND is_current=1"
        " AND merged_into IS NULL", (project_id, str(kind))).fetchone()
    return int(row[0])


def kind_counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """kind → 计数（一次查完）。左栏的类型徽标要的是全部 kind 的计数，
    逐个 kind 问一遍就是 N 次往返。"""
    return {str(r["kind"]): int(r["n"]) for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM entities WHERE project_id=? AND is_current=1"
        " AND merged_into IS NULL GROUP BY kind", (project_id,))}


# ---------------------------------------------------------------------------
# 身份规则：用户输入 → 库里的规范键
# ---------------------------------------------------------------------------


def product_spec(spec: str, version: str | None = None) -> str:
    """用户写的产品 → `product` 的规范 key（`产品名[@版本]`）。

    三段都**委托**给各自的唯一实现：是不是版本号问 `normalize.version_tuple`，
    别名折叠走 `normalize.product_name`（`Apache httpd` / `apache2` → `httpd`），
    拼接走 `httpx.product_key`。这里只负责"把用户可能写的形状切开"：

        nginx            → nginx
        nginx@1.24.0     → nginx@1.24.0
        Nginx 1.24.0     → nginx@1.24.0        （空格当分隔符）
        --product nginx --version 1.24.0       → nginx@1.24.0

    **带版本与不带版本是两个实体**（003 的 `product.identity_rule`）：问
    "谁还在用 nginx"与"谁还在用 nginx 1.24.0"是两件事，这里不替人折叠。
    """
    text = str(spec).strip()
    name, separator, tail = text.rpartition("@")
    if separator:
        name, ver = name.strip(), tail.strip()
    else:
        name, ver = text, str(version or "").strip()
    if version is not None and str(version).strip():
        ver = str(version).strip()
    if not name:
        raise QueryError("E_BAD_PRODUCT", f"产品名不能为空（收到 {spec!r}）")
    try:
        name = normalize.product_name(name)
    except normalize.NormalizeError as exc:
        raise QueryError("E_BAD_PRODUCT", f"产品名无法归一化：{exc}") from exc
    if ver:
        try:
            normalize.version_tuple(ver)
        except normalize.NormalizeError as exc:
            raise QueryError(
                "E_BAD_PRODUCT", f"版本 {ver!r} 不是一个可解析的版本号：{exc}",
                hint="版本按 normalize.version_tuple 校验（`1.24.0` 可以，"
                     "`cloudfront` 不行）—— 不是版本就当它不存在，"
                     "而不要把它当成一个字符串去比。") from exc
    return product_key(name, ver)


def endpoint_spec(ref: str) -> str:
    """用户写的接口 → `endpoint` 的规范 key（`主机[:非默认端口] + 路径模板`）。

    两种写法，**判据是形状不是猜**：

      * 含 `://` 的绝对 URL —— 走 `normalize.endpoint_key`（身份规则的唯一实现，
        它顺带完成路径模板折叠：`/user/8f3a…` → `/user/{uuid}`）；
      * 不含 `://` 且不以 `/` 开头 —— 当作**规范 key 字面量**（与 `show <key>`
        同一口径：key 是 normalize 之后的写法）；
      * 以 `/` 开头的裸路径 —— **报错**。endpoint 的身份含主机，替人挑一个 base
        就是在编事实，而编错的那一半不会有任何症状。
    """
    text = str(ref).strip()
    if not text:
        raise QueryError("E_BAD_ENDPOINT", "endpoint 不能为空")
    if text.startswith("/"):
        raise QueryError(
            "E_BAD_ENDPOINT",
            f"只给了路径 {text!r} —— endpoint 的身份是 `主机[:非默认端口] + 路径模板`，"
            f"没有主机就算不出它是哪一个接口",
            hint="给完整的 URL（https://app.example.com/user/42），"
                 "或已经折叠好的规范 key（app.example.com/user/{id}）。")
    if "://" not in text:
        return text                      # 规范 key 字面量，原样去查
    try:
        return normalize.endpoint_key(text)
    except normalize.NormalizeError as exc:
        raise QueryError("E_BAD_ENDPOINT", f"{text!r} 不是一个可解析的 URL：{exc}",
                         hint="接口的身份规则见 `magpie schema --json` 的 "
                              "kinds[endpoint].identity_rule。") from exc


def cidr_network(raw: str) -> tuple[str, Any]:
    """用户写的网段 → (规范写法, 网络对象)。**规范化走 `normalize.try_cidr`**
    （主机位补齐：`10.0.0.7/24` → `10.0.0.0/24` —— 同一网段的两种写法是同一个）。"""
    text = str(raw).strip()
    canonical = normalize.try_cidr(text)
    if canonical is None:
        raise QueryError(
            "E_BAD_CIDR", f"{text!r} 不是一个网段",
            hint="要的是带前缀长度的网段（10.0.0.0/24）。单个 IP 是另一个 kind —— "
                 "它不是「落在网段里的资产」的判据。")
    return canonical, ipaddress.ip_network(canonical, strict=False)


# ---------------------------------------------------------------------------
# 实体列表 —— 通用引擎
# ---------------------------------------------------------------------------


def _observation_exists(field: str, value: str) -> tuple[str, list[Any]]:
    """某条观测等于某个值。severity / state 是 `vuln` 实体上的**观测**而不是
    新列（005 的迁移注释解释了为什么），所以筛它们只能走观测表。"""
    return ("EXISTS (SELECT 1 FROM observations o WHERE o.entity_id=e.id"
            " AND o.project_id=e.project_id AND o.is_current=1"
            " AND o.semantic_field=? AND o.value=?)", [field, value])


def entities_page(conn: sqlite3.Connection, project_id: int, *,
                  kind: str | None = None, lens: str | None = None,
                  q: str | None = None, tag: str | None = None,
                  lifecycle: str | None = None,
                  product: str | None = None, version: str | None = None,
                  endpoint: str | None = None,
                  severity: str | None = None, state: str | None = None,
                  in_cidr: str | None = None,
                  cursor: str | None = None, limit: int = DEFAULT_PAGE) -> dict:
    """实体列表。**键集游标分页**（`ORDER BY e.id` + `e.id > cursor`）。

    返回 `{"entities", "count", "next_cursor", "notes", "filters", "query"}`。
    每一页的 `next_cursor` 是**本页最后一条的 id**，与 2.7 的 `/entities` 逐字
    同义 —— 分页语义在这里只有一份实现，两侧都不可能改出第二套。

    `notes` 是**诚实的边界**：产品实体根本不存在、网段里的候选是被算出来的
    而不是连出来的…… 这些话必须跟着结果一起走，否则一个空列表会被读成
    "筛过了，就是没有"。
    """
    size = page_size(limit)
    after = parse_cursor(cursor)
    notes: list[str] = []
    echo: dict[str, Any] = {}

    where = ["e.project_id=?", "e.is_current=1", "e.merged_into IS NULL"]
    params: list[Any] = [project_id]

    if kind:
        where.append("e.kind=?")
        params.append(str(kind))
        echo["kind"] = str(kind)
    if lens:
        kinds = kinds_for_lens(conn, str(lens))
        if not kinds:
            raise QueryError(
                "E_LENS_NOT_FOUND", f"镜头 {lens!r} 没有归属任何 kind",
                hint="镜头由 kind_registry.lens 决定；库里有 kind 的镜头见 known_lenses。",
                known_lenses=lens_ids(conn))
        where.append(f"e.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
        echo["lens"] = str(lens)
    if q:
        where.append("EXISTS (SELECT 1 FROM entity_keys k WHERE k.entity_id=e.id"
                     " AND k.project_id=e.project_id AND k.entity_key LIKE ?)")
        params.append(f"%{q}%")
        echo["q"] = str(q)
    if tag:
        where.append("EXISTS (SELECT 1 FROM entity_tags t WHERE t.entity_id=e.id"
                     " AND t.project_id=e.project_id AND t.tag=? AND t.deleted_at IS NULL)")
        params.append(str(tag))
        echo["tag"] = str(tag)
    if lifecycle:
        where.append("e.lifecycle=?")
        params.append(str(lifecycle))
        echo["lifecycle"] = str(lifecycle)
    if severity:
        clause, extra = _observation_exists("severity", str(severity))
        where.append(clause)
        params.extend(extra)
        echo["severity"] = str(severity)
    if state:
        clause, extra = _observation_exists("state", str(state))
        where.append(clause)
        params.extend(extra)
        echo["state"] = str(state)

    # 关系过滤（`runs` / `served_by`）：把"跑着这个产品的实体""暴露这个接口的
    # 实体"翻译成一次 EXISTS。目标实体不存在时**不能**当成"没有匹配"就完事 ——
    # 那是两件不同的事（"库里没有 nginx 这个产品"vs"有 nginx，但没人跑它"）。
    if product is not None or version is not None:
        key = product_spec(product if product is not None else "", version)
        target = entity_id_for_key(conn, project_id, key)
        echo["product"] = key
        if target is None:
            where.append("1=0")
            notes.append(f"库里没有 {key!r} 这个产品实体 —— 空结果的意思是"
                         f"**这个产品还没被任何来源写进来**，不是「没人跑它」。")
        else:
            where.append("EXISTS (SELECT 1 FROM relations r WHERE r.src_id=e.id"
                         " AND r.project_id=e.project_id AND r.is_current=1"
                         " AND r.rel='runs' AND r.dst_id=?)")
            params.append(target)
    if endpoint is not None:
        key = endpoint_spec(endpoint)
        target = entity_id_for_key(conn, project_id, key)
        echo["endpoint"] = key
        if target is None:
            where.append("1=0")
            notes.append(f"库里没有 {key!r} 这个接口实体 —— 空结果的意思是"
                         f"**没人观测到过它**，不是「没有服务拥有它」。")
        else:
            where.append("EXISTS (SELECT 1 FROM relations r WHERE r.dst_id=e.id"
                         " AND r.project_id=e.project_id AND r.is_current=1"
                         " AND r.rel='served_by' AND r.src_id=?)")
            params.append(target)
    if in_cidr is not None:
        canonical, network, ids = assets_in_network(conn, project_id, in_cidr)
        echo["in_cidr"] = canonical
        if not ids:
            where.append("1=0")
            notes.append(f"没有资产落在 {canonical} 里 —— 这是**算出来的**包含判断"
                         f"（nmap 的 `<target>` 不与主机连边），不是一条存着的边。")
        else:
            where.append(f"e.id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
            notes.append(f"`in_cidr` 是**算出来的**包含判断：拿本项目里的 ip / cidr "
                         f"实体逐个与 {canonical} 比对（nmap 的 `<target>` 不与主机"
                         f"连边，那条边是 3.8 的 `member_of` 上推的事）。"
                         f"候选 {len(ids)} 个。")
    if after is not None:
        where.append("e.id > ?")
        params.append(after)
        echo["cursor"] = after

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
    return {"query": "entities", "entities": items, "count": len(items),
            "next_cursor": next_cursor, "notes": notes, "filters": echo}


# ---------------------------------------------------------------------------
# 实体详情
# ---------------------------------------------------------------------------


def entity_detail(conn: sqlite3.Connection, project_id: int, entity_id: int) -> dict:
    """实体详情：观测 / 关系 / 标签 / 人工保护 / 未决 Inbox / 时间线。

    `ls` 与 `GET /entities/{id}` 用的是**同一个**函数 —— 这正是 §3.5 要的那件事：
    此前这一段在 `read.py` 与 `routes.py` 里各写了一遍，两份的形状恰好一致
    纯属当时的运气，而运气不会在第三次改动时还在。
    """
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
        item["payload"] = json_or_none(item.pop("payload_json"))

    timeline = [dict(r) for r in conn.execute(
        "SELECT id, actor, action, at, job_id FROM activity_log"
        " WHERE target_kind='entity' AND target_id=? ORDER BY id DESC LIMIT 20",
        (entity_id,))]

    return {"entity": entity, "observations": observations, "relations": relations,
            "tags": tags_of(conn, project_id, entity_id),
            "manual": {"protected_fields": manual_keys(conn, project_id, entity_id)},
            "open_inbox": inbox, "timeline": timeline}


# ---------------------------------------------------------------------------
# 具名查询（§3.5 的五问）—— 每一个都是 `entities_page` 的定型调用
# ---------------------------------------------------------------------------


def services_running(conn: sqlite3.Connection, project_id: int,
                     product: str | None = None, *,
                     version: str | None = None, **page: Any) -> dict:
    """**谁还在跑 <产品[@版本]>**：`service --runs--> product` 的入边。

    "还在用 Nginx 1.18"这句话问的是**带版本的那个实体**（003 的
    `product.identity_rule` 把这条写死了）：把它折成"只要有 nginx 就算命中"，
    那个问题就再也问不出来了。库里没有这个产品实体时，`notes` 会说出**它没被
    写进来**，而不是让空列表冒充"没人跑它"。
    """
    if product is None:
        raise QueryError(
            "E_BAD_PRODUCT", "只给了版本没给产品 —— 版本是产品的一部分",
            hint="写成 `--product nginx@1.24.0`，或者 `--product nginx "
                 "--version 1.24.0`。`nginx` 与 `nginx@1.24.0` 是两个实体，"
                 "所以产品名不能省。")
    out = entities_page(conn, project_id, product=product, version=version, **page)
    out["query"] = "services"
    out["product"] = {"key": out["filters"].get("product"),
                      "requested": str(product)}
    return out


def vulns(conn: sqlite3.Connection, project_id: int, *, severity: str | None = None,
          state: str | None = None, kind: str | None = None,
          **page: Any) -> dict:
    """按 `severity` / `state` 筛漏洞。两者都是 `vuln` 实体上的**观测**
    （005 的迁移注释：它们是事实，由谁在哪次作业里说的），所以这里是观测筛选，
    不是新的列。

    不给 `kind` 时落在 `vuln` —— 问"高危里有哪些还没确认"问的是漏洞，而
    `severity` 这个词在别的 kind 上还没有定义。给了就以给的为准（解析器扩张
    之后可能有第二个带 severity 的 kind，那时这一行不用改）。
    """
    out = entities_page(conn, project_id, kind=kind or "vuln", severity=severity,
                        state=state, **page)
    out["query"] = "vulns"
    return out


def endpoint_owners(conn: sqlite3.Connection, project_id: int, endpoint: str,
                    **page: Any) -> dict:
    """**某个接口的归属服务**：`endpoint --served_by--> service` 的出边。

    返回的是**服务**（和 `services_running` 一样，这一问要的是"归谁"），
    接口本身用 `show <key>` 看详情。"""
    out = entities_page(conn, project_id, endpoint=endpoint, **page)
    out["query"] = "endpoint"
    out["endpoint"] = {"key": out["filters"].get("endpoint"),
                       "requested": str(endpoint)}
    return out


def assets_in_cidr(conn: sqlite3.Connection, project_id: int, in_cidr: str,
                   **page: Any) -> dict:
    """**落在网段里的资产**。包含判断是**算出来的**（见 `assets_in_network`）。

    参数名与过滤条件名（`in_cidr`）刻意一致：`dispatch` 把它整组关键字原样转交，
    名字对不上就会在分发那一行炸掉 —— 而"哪个名字是对的"只有一个答案。
    """
    out = entities_page(conn, project_id, in_cidr=in_cidr, **page)
    out["query"] = "assets_in_cidr"
    return out


def assets_in_network(conn: sqlite3.Connection, project_id: int,
                      cidr: str) -> tuple[str, Any, list[int]]:
    """网段 → (规范写法, 网络对象, 落在里面的实体 id)。

    为什么是"算"而不是"查一条边"：nmap 的 `<target specification="10.0.0.0/24">`
    只产出 `cidr` 实体与一条状态观测，**不与 `<host>` 连边** —— 那个文件里没有
    "我在哪个扫描目标里"这个字段，连边就只能拿地址去和每个网段做包含判断，那是
    在**猜** nmap 没有说的事（3.1 的注释把这条写死了）。本函数把这件事放到查询
    里明说：结果是包含判断的产物，不是一条存着的边。

    候选是**本项目里的 `ip` / `cidr` 实体**（这两类是唯一有地址语义的 kind）。
    两类的判据不同，各自写清楚：

      * `ip`   —— 地址落在网段里；
      * `cidr` —— 子网**被**该网段包含（`10.0.0.0/25` 落在 `10.0.0.0/24` 里）。

    网段**自己**不算落在自己里面（问"这段里有什么"时把自己列出来是一条废话）。
    """
    canonical, network = cidr_network(cidr)
    rows = conn.execute(
        "SELECT e.id, e.kind, k.entity_key FROM entities e"
        " JOIN entity_keys k ON k.entity_id = e.id AND k.project_id = e.project_id"
        " AND k.canonical = 1"
        " WHERE e.project_id=? AND e.is_current=1 AND e.merged_into IS NULL"
        " AND e.kind IN ('ip', 'cidr') ORDER BY e.id", (project_id,)).fetchall()
    ids: list[int] = []
    for row in rows:
        key = str(row["entity_key"])
        if str(row["kind"]) == "ip":
            address = normalize.try_ip(key)
            if address is None:                      # pragma: no cover - 坏数据
                continue
            inside = ipaddress.ip_address(address) in network
        else:
            subnet = normalize.try_cidr(key)
            if subnet is None:                       # pragma: no cover - 坏数据
                continue
            # 网段**自己**不算落在自己里面（问"这段里有什么"时把自己列出来
            # 是一条废话），所以判据是"严格更小的子网"。
            block = ipaddress.ip_network(subnet, strict=False)
            inside = block != network and block.subnet_of(network)
        if inside:
            ids.append(int(row["id"]))
    return canonical, network, ids


def dispatch(conn: sqlite3.Connection, project_id: int, **filters: Any) -> dict:
    """**唯一的分发点**：这组过滤条件在问哪一个具名查询。

    `cli/read.py` 与 `api/routes.py` 都只调它（外加 `reject_unsupported` 的
    响亮拒绝）—— 于是"两个入口会不会选出不同的查询"这个问题不存在：映射只有
    这一处。没有命中任何具名查询时落到通用的 `entities_page`。

    `query` 这个名字跟着结果一起返回，调用方据此知道刚才是哪一问（输出里带上
    它，人读的时候一眼能看出"这次问的是 services 还是 entities"）。
    """
    if filters.get("product") is not None or filters.get("version") is not None:
        return services_running(conn, project_id, **filters)
    if filters.get("endpoint") is not None:
        return endpoint_owners(conn, project_id, **filters)
    if filters.get("in_cidr") is not None:
        return assets_in_cidr(conn, project_id, **filters)
    if filters.get("severity") is not None or filters.get("state") is not None:
        return vulns(conn, project_id, **filters)
    return entities_page(conn, project_id, **filters)


#: 具名查询的名字 → 说明。**`magpie schema --json` 会把这张表打出来** ——
#: agent 据此知道有哪些问题可以直接问，而不必自己拼过滤条件（拼接就是发明
#: 语义，2.2 拒绝过一次）。
NAMED_QUERIES = {
    "entities": "通用实体列表（按 kind / q / tag / lifecycle 筛）",
    "services": "谁还在跑某个产品[@版本]（service --runs--> product）",
    "vulns": "按 severity / state 筛漏洞（两者都是观测）",
    "endpoint": "某个接口的归属服务（endpoint --served_by--> service）",
    "assets_in_cidr": "落在网段里的资产（包含判断是算出来的）",
    "lens": "镜头页：这个镜头下每个 kind 的类型徽标与计数",
    # 五类问题（§7.4 的体检页）。它们不是 `ls` 上的过滤条件，而是**体检页**
    # 会问的五问 —— 一并列在这里，是因为 agent 同样该知道"这个库里现在有哪几类
    # 待办"，而 `magpie schema --json` 是它的说明书。
    "health": "体检页：五类问题各自的计数与未决项",
    "health.orphan": "孤儿：从未有过任何贡献的实体",
    "health.conflict": "冲突：同字段多值 / 合并后多值 / 身份归属不唯一",
    "health.low_confidence": "低置信度：嗅探仲裁拿不准这份输入该由谁解析",
    "health.unknown_field": "未知字段：输入里出现了解析器不认识的字段名",
    "health.out_of_scope": "越界资产：落在项目 scope 的 include / exclude 之外",
}


# ---------------------------------------------------------------------------
# 图
# ---------------------------------------------------------------------------


def graph_payload(conn: sqlite3.Connection, project_id: int, *, lens: str | None,
                  kind: str | None, q: str | None, limit: int,
                  seeds: list[int] | None = None) -> dict:
    """节点 + 边 + 布局。`GET /graph` 与 `GET /graph/{id}/neighbors` 共用它。

    `q` 是**内存里**的过滤（在已取回的那一批节点上做子串匹配），不推进 SQL ——
    它的位置在 `limit` 之后，所以"筛出来比 limit 少"是正常的：这一条写在
    `_graph_payload` 的老注释里，改动它要连着前端的告示一起改。
    """
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
    layout: dict[str, Any] = {}
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


def neighbour_ids(conn: sqlite3.Connection, project_id: int, entity_id: int,
                  depth: int, *, limit: int = 300) -> list[int]:
    """从 `entity_id` 出发走 `depth` 层关系（BFS），返回**含自身**的 id 集合。"""
    if depth is None or int(depth) < 1:
        raise QueryError("E_BAD_DEPTH", f"depth 必须 >= 1，收到 {depth}")
    depth = min(int(depth), MAX_DEPTH)
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


# ---------------------------------------------------------------------------
# 作业历史
# ---------------------------------------------------------------------------


def job_row(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise QueryError("E_JOB_NOT_FOUND", f"作业 {job_id} 不存在",
                         hint="作业 id 由导入产生；作业列表见 `magpie jobs`。")
    return dict(row)


def job_counts(conn: sqlite3.Connection,
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


#: 作业列表的列。与 `SELECT *` 的区别是**顺序稳定**：列表的字段顺序会原样进
#: 输出，而 `*` 的顺序跟着 schema 走 —— 加一列就会让每个消费者的字段顺序变一次。
JOB_COLUMNS = ("id, project_id, kind, state, applied, coverage, scope_version,"
               " scope_hash, parser_fingerprint, actor, started_at, finished_at")

#: 作业的状态词表（002 与 `ops.finish_job` 定义的那五个）。
JOB_STATES = ("running", "done", "failed", "partial", "aborted")


def list_jobs(conn: sqlite3.Connection, *, project_id: int | None = None,
              state: str | None = None, applied: bool | int | None = None,
              limit: int = DEFAULT_JOB_LIMIT) -> list[dict]:
    """作业历史（最近的在前）。`applied` 收布尔 —— 调用方负责把它从查询串
    或 flag 解析出来（API 那侧刻意不交给 FastAPI，理由见 `routes.list_jobs`）。"""
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if project_id is not None:
        where.append("project_id=?")
        params.append(int(project_id))
    if state is not None:
        if state not in JOB_STATES:
            raise QueryError("E_BAD_STATE",
                             f"state 只能是 {' / '.join(JOB_STATES)}，收到 {state!r}",
                             supported=list(JOB_STATES))
        where.append("state=?")
        params.append(str(state))
    if applied is not None:
        where.append("applied=?")
        params.append(1 if applied else 0)
    return [dict(r) for r in conn.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE {' AND '.join(where)}"
        f" ORDER BY id DESC LIMIT ?", (*params, page_size(limit)))]


def all_jobs(conn: sqlite3.Connection) -> list[dict]:
    """体检要的作业全表（按 id 升序 —— 体检是"从头看一遍"，不是"最近发生了什么"）。"""
    return [dict(r) for r in conn.execute(
        "SELECT id, project_id, kind, state, applied, started_at, finished_at"
        " FROM jobs ORDER BY id")]


def job_activity(conn: sqlite3.Connection, job_id: int, limit: int = 50) -> list[dict]:
    items = [dict(r) for r in conn.execute(
        "SELECT id, at, actor, action, target_kind, target_id, detail"
        " FROM activity_log WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))]
    for item in items:
        item["detail"] = json_or_none(item["detail"])
    return items


def jobs_with_parser_fingerprint(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE parser_fingerprint IS NOT NULL").fetchone()[0])


def jobs_by_state(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state ORDER BY state")]


def raw_file_states(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT storage_state, COUNT(*) AS n FROM raw_files GROUP BY storage_state")]


# ---------------------------------------------------------------------------
# 管线定义与运行（3.7b）
# ---------------------------------------------------------------------------
#
# 这一节的存在理由与 3.5 把两条 SQL 收敛进 `core/query.py` 逐字相同：`/api/v1`
# 的读侧不许在路由里写 SQL（红线 `SQL-FREE` 扫 `api/routes.py` 与
# `cli/read.py`），而"同一份数据、两种答案"没有任何症状，直到有人按错的那一半
# 行动。查询写在这里，路由只负责 HTTP 形状。


#: 管线定义 / 运行的列表列。与 `JOB_COLUMNS` 同一条纪律：显式列出、顺序稳定
#: （`SELECT *` 的顺序跟着 schema 走，加一列就让每个消费者的字段顺序变一次）。
#:
#: `definition` 在列里，因为列表也要报 `step_count`（它只存在于 JSON 里）；
#: 列表端点算完之后把它**弹掉**再出信封 —— 定义本身走 `GET /pipelines/{id}`，
#: 列表不需要把每一份定义都背过去。`report` 不在列里：它只属于 run 详情。
_PIPELINE_COLUMNS = ("id, project_id, name, format_version, definition_hash,"
                     " revision, definition, created_at, created_by, updated_at,"
                     " updated_by, archived_at")
_RUN_COLUMNS = ("id, project_id, pipeline_id, definition_hash, format_version,"
                " applied, state, step_count, job_count, actor, started_at,"
                " finished_at")


def _decorate_run(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["applied"] = bool(item["applied"])
    # `can_undo` / `can_redo` 与 `_decorate_job` 同构 —— 界面据此决定按钮，
    # 而不是自己记一本账（两本账不会同时错，只会各错一半）。
    item["can_undo"] = bool(item["applied"])
    item["can_redo"] = not bool(item["applied"])
    return item


def pipelines(conn: sqlite3.Connection, project_id: int, *,
              include_archived: bool = False,
              limit: int = DEFAULT_PAGE) -> list[dict]:
    """定义列表（含各自最近一次 run 的摘要）。

    `step_count` 由**解析定义 JSON** 得出：定义是**输入**，它没有一份"事实的"
    步骤数可以冗余存储（`pipeline_runs.step_count` 是另一样东西 —— 那一次跑
    真的跑了几个步骤）。定义的数量级是个位数，"一次列表解析几个 JSON"远小于
    "为了省它再开一张会和定义对不上的表"。
    """
    where = ["d.project_id=?"]
    params: list[Any] = [int(project_id)]
    if not include_archived:
        where.append("d.archived_at IS NULL")
    rows = []
    for raw in conn.execute(
            f"SELECT {_PIPELINE_COLUMNS} FROM pipeline_definitions d"
            f" WHERE {' AND '.join(where)} ORDER BY d.name LIMIT ?",
            (*params, page_size(limit))):
        item = _decorate_definition(raw)
        item.pop("definition", None)          # 见 `_PIPELINE_COLUMNS` 的说明
        rows.append(item)

    # 最近一次 run：一条子查询取 id，再一次 `IN (...)` 取明细 —— 不做 N+1。
    last_ids = [int(r["id"]) for r in conn.execute(
        "SELECT MAX(id) AS id FROM pipeline_runs WHERE project_id=?"
        " GROUP BY pipeline_id HAVING MAX(id) IS NOT NULL", (int(project_id),))]
    if last_ids:
        marks = ",".join("?" * len(last_ids))
        latest = {int(r["pipeline_id"]): _decorate_run(r) for r in conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM pipeline_runs WHERE id IN ({marks})"
            f" ORDER BY id DESC", tuple(last_ids))}
        for item in rows:
            item["last_run"] = latest.get(int(item["id"]))
    else:                                               # pragma: no cover —— 空库
        for item in rows:
            item["last_run"] = None
    return rows


def _decorate_definition(row: Any) -> dict[str, Any]:
    item = dict(row)
    definition = json_or_none(item.get("definition"))
    item["step_count"] = len((definition or {}).get("steps") or [])
    return item


def pipeline_detail(conn: sqlite3.Connection, project_id: int,
                    pipeline_id: int) -> dict[str, Any]:
    """一条定义 + `definition_hash` + `revision` + 解析好的定义本身。"""
    row = conn.execute(
        f"SELECT {_PIPELINE_COLUMNS} FROM pipeline_definitions"
        f" WHERE id=? AND project_id=?",
        (int(pipeline_id), int(project_id))).fetchone()
    if row is None:
        raise QueryError("E_PIPELINE_NOT_FOUND",
                         f"管线定义 {pipeline_id} 不存在（项目 {project_id}）",
                         hint="GET /api/v1/pipelines 看这个项目里有哪些定义")
    item = _decorate_definition(row)
    item["definition"] = json_or_none(item.get("definition"))
    return item


def pipeline_runs(conn: sqlite3.Connection, project_id: int, pipeline_id: int, *,
                  limit: int = DEFAULT_JOB_LIMIT) -> list[dict]:
    """一条定义的运行历史（最近的在前）。**不带报告** —— 报告在详情里。"""
    return [_decorate_run(r) for r in conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM pipeline_runs"
        f" WHERE project_id=? AND pipeline_id=? ORDER BY id DESC LIMIT ?",
        (int(project_id), int(pipeline_id), page_size(limit)))]


def pipeline_run_detail(conn: sqlite3.Connection, project_id: int,
                        run_id: int) -> dict[str, Any]:
    """单个 run（含完整报告）。步骤 → job 的映射就活在 `report.steps[].job_ids`。"""
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS}, report FROM pipeline_runs"
        f" WHERE id=? AND project_id=?",
        (int(run_id), int(project_id))).fetchone()
    if row is None:
        raise QueryError("E_PIPELINE_RUN_NOT_FOUND",
                         f"管线运行 {run_id} 不存在（项目 {project_id}）",
                         hint="GET /api/v1/pipelines/{id}/runs 看一条定义的运行历史")
    item = _decorate_run(row)
    item["report"] = json_or_none(item.get("report"))
    return item


def pipeline_run_jobs(conn: sqlite3.Connection, run_id: int) -> list[int]:
    """这次 run 挂着的 job id（正序）。"""
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM jobs WHERE pipeline_run_id=? ORDER BY id", (int(run_id),))]


# ---------------------------------------------------------------------------
# 增量 diff（`magpie diff` / `GET /diff` 的数据侧）
# ---------------------------------------------------------------------------

#: 自某个时刻起**仍然生效**的贡献，并标出实体是不是这一批里新出现的。
#:
#: 两个判据都在这里：
#:   * `v_active_contributions` —— 被 undo 的作业不算（撤销之后 diff 必须跟着
#:     变空，否则"撤销成功"这条结论就少了一个独立佐证）；
#:   * `before` 子查询 —— 这个实体在那之前**有没有过**任何贡献。用"有没有过"
#:     而不是 `entities.first_seen`：那两列只在建实体时写一次，之后没人更新
#:     （`store.new_entity` 之外没有任何地方动它），拿它当"新不新"会得到一个
#:     永远停在建库那天的答案。
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


def changes_since(conn: sqlite3.Connection, project_id: int,
                  since_at: str) -> list[dict]:
    """`since_at` 起仍然生效的每一条贡献（含 `is_new` 标记）。"""
    return [dict(r) for r in conn.execute(
        _SINCE_SQL, (project_id, since_at, project_id, since_at))]


def job_started_at(conn: sqlite3.Connection, project_id: int,
                   job_id: int) -> str | None:
    row = conn.execute("SELECT id, started_at FROM jobs WHERE id=? AND project_id=?",
                       (int(job_id), project_id)).fetchone()
    if row is None:
        raise QueryError("E_JOB_NOT_FOUND", f"项目 {project_id} 里没有作业 {job_id}",
                         hint="`magpie doctor --json` 的 jobs 段列出全部作业。")
    return str(row["started_at"])


def last_job(conn: sqlite3.Connection, project_id: int) -> tuple[int, str] | None:
    """本项目最后一个作业的 (id, started_at)。

    判据是 `jobs.started_at` 而不是 `id`：一次导入的多个 job 的 `started_at`
    几乎相同，所以"最后一次"得到的是**这一批导入**，而不是只剩最后一个文件的
    那一点。
    """
    row = conn.execute("SELECT id, started_at FROM jobs WHERE project_id=?"
                       " ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    return None if row is None else (int(row["id"]), str(row["started_at"]))


def kinds_by_ids(conn: sqlite3.Connection, project_id: int,
                 ids: list[int]) -> dict[int, str]:
    """一次查出若干实体的 kind（diff 的每一行都要它，逐个问就是 N+1）。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {int(r["id"]): str(r["kind"]) for r in conn.execute(
        f"SELECT id, kind FROM entities WHERE project_id=? AND id IN ({marks})",
        (project_id, *ids))}


# ---------------------------------------------------------------------------
# 元信息 / 体检 / Inbox / 活动 / 导出
# ---------------------------------------------------------------------------


def metric_counts(conn: sqlite3.Connection,
                  project_id: int | None = None) -> dict[str, int]:
    """`/meta` 与 `magpie doctor` 用的那组计数。**一次定义，两处消费。**

    `project_id` 不给时是**全库**计数（`/meta` 看的是"这个库有多大"）；
    给了就带上该项目的 `untested`（一条观测都没有的实体）。
    """
    def one(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    counts = {
        "projects": one("SELECT COUNT(*) FROM projects"),
        "entities": one("SELECT COUNT(*) FROM entities WHERE is_current=1"),
        "observations": one("SELECT COUNT(*) FROM observations WHERE is_current=1"),
        "relations": one("SELECT COUNT(*) FROM relations WHERE is_current=1"),
        "jobs": one("SELECT COUNT(*) FROM jobs"),
        "open_inbox": one("SELECT COUNT(*) FROM inbox_items WHERE state='open'"),
    }
    if project_id is not None:
        counts["untested"] = untested_count(conn, int(project_id))
    return counts


def open_inbox_count(conn: sqlite3.Connection, project_id: int | None = None) -> int:
    """未决 Inbox 项数。`project_id` 不给是**全库** —— 两处消费者要的正是两个
    不同的口径（`/doctor` 看整库，上下文包看这一个项目），所以这里把话说清楚，
    而不是让调用方各自去猜 `metric_counts` 里那个数字是哪个范围。"""
    if project_id is None:
        return int(conn.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE state='open'").fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'",
        (int(project_id),)).fetchone()[0])


def untested_count(conn: sqlite3.Connection, project_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE e.project_id=? AND e.is_current=1"
        " AND e.merged_into IS NULL AND NOT EXISTS (SELECT 1 FROM observations o"
        "   WHERE o.entity_id=e.id AND o.is_current=1)", (project_id,)).fetchone()[0])


def untested_entities(conn: sqlite3.Connection, project_id: int,
                      limit: int = 20) -> list[dict]:
    """一条观测都没有的实体。**不自动清理**（§2.3）—— 它们是"没测过"这件事本身，
    而"没测过"与"没有问题"是两件事。"""
    return [dict(r) for r in conn.execute(
        "SELECT e.id, (SELECT k.entity_key FROM entity_keys k WHERE k.entity_id=e.id"
        " AND k.project_id=e.project_id AND k.canonical=1 LIMIT 1) AS entity_key,"
        " e.kind FROM entities e WHERE e.project_id=? AND e.is_current=1"
        " AND e.merged_into IS NULL AND NOT EXISTS (SELECT 1 FROM observations o"
        "   WHERE o.entity_id=e.id AND o.is_current=1) ORDER BY e.id LIMIT ?",
        (project_id, int(limit)))]


def inbox_items(conn: sqlite3.Connection, project_id: int, state: str = "open",
                limit: int = DEFAULT_HEALTH_LIMIT,
                kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Inbox 项。默认只看 `open`，并按 severity 排（high 在前）—— 一个需要人
    裁决的列表，把 high 埋在 50 条 low 后面等于没有排序。

    `kinds` 是体检页要的过滤（五类各是一组 kind，见 `HEALTH_CATEGORIES`）。
    过滤放在**这一处**而不是让调用方拿到全表再自己挑：那样"排序"与"截断"就会
    在两侧各发生一次，而 `limit` 截断过的列表再过滤，得到的计数是错的。
    """
    params: list[Any] = [project_id, str(state)]
    where = "project_id=? AND state=?"
    if kinds is not None:
        marks = ",".join("?" * len(kinds))
        where += f" AND kind IN ({marks})"
        params.extend(str(k) for k in kinds)
    rows = conn.execute(
        "SELECT id, kind, subject_kind, subject_id, severity, state, payload_json,"
        " occurrence_count, created_at, last_seen_at, created_by_job_id"
        f" FROM inbox_items WHERE {where} ORDER BY"
        " CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
        " LIMIT ?", (*params, page_size(limit))).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json_or_none(item.pop("payload_json"))
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# 五类问题（§7.4 的体检页）—— 每类一条具名查询
# ---------------------------------------------------------------------------
#
# 为什么五类**是** Inbox 项的分组，而不是五条独立的检测语句
# ------------------------------------------------------------
# 本步的验收判据是一句话：「清空 Inbox 后体检页五类全部归零」。
#
# 这句话只有在五类**就是** Inbox 的归组时才是一条**结构上成立**的定理；换成
# 五条独立的检测语句，它立刻变成一句多半为假的口号 —— 裁决掉一条待办并不会
# 让"这个字段有两个值"这件事从库里消失，下一次导入还会把它重新报出来。于是
# 「清空即干净」就成了一句没人能兑现的承诺，而体检页上的数字会长期挂在非零，
# 慢慢地没人再看它。
#
# 所以这里的分工是：**Inbox 是唯一的待办队列，体检页是它的按类视图。** 谁发现
# 了问题，谁就在写事实的同一条路径上开一条 Inbox 项（`ops._open_inbox` 是唯一
# 的写入口）；体检页只负责把已经开出来的项分成五类数一遍。这条分工让两个页面
# 不可能给出不同的答案 —— 因为它们看的是同一张表。
#
# ⚠ 「越界资产」与「未知字段」这两类的生产者是 3.6 才接上的
# ------------------------------------------------------------
# 3.6 之前，schema 冻结的七个 kind 里只有四个有生产者（`orphan_subject` /
# `value_conflict` / `merge_conflict` / `manual_required`）。另外三个
# （`identity_ambiguous` / `evidence_missing` / `scope_drift`）在 001 的 CHECK
# 里躺着，但**没有任何一处写它们** —— 于是「未知字段」与「越界资产」两类的计数
# 会永远恒等于 0。一个恒为 0 的数字比不显示更糟：它会被读成"查过了，没问题"
# （`DoctorView.vue` 从 2.8 起就是用这句话把它们挡在「尚未提供」里的）。
#
# 所以 3.6 补上了两个生产者：
#   * `ops.refresh_unknown_fields` —— 解析器报出未知字段名时开 `evidence_missing`；
#   * `ops.refresh_scope_drift`    —— 资产落在 scope 之外时开 `scope_drift`。
# 两者都只在**真的有东西要报**时才写，干净的夹具上一行不写（这也让它们对既有
# 的黄金夹具完全惰性）。


class HealthCategory(NamedTuple):
    """体检页的一行。`kinds` 是这一类的全部 kind —— **分类表是唯一声明处**。"""

    key: str
    label: str
    kinds: tuple[str, ...]
    why: str


#: 五类的**唯一分类表**。schema 的七个 inbox kind 在这里被**划分**（partition）：
#: 每个 kind 恰好属于一类，没有漏、没有重。这两条都有用例钉着：
#:   * 漏 → `unclaimed_inbox_kinds()` 会把它报出来（"开了一条谁都不认领的待办"）；
#:   * 重 → 五类之和会大于 `open_inbox`，一个项目看起来比它自己还多待办。
#:
#: 分类的**依据是"这件事要怎么裁决"**，不是 kind 的字面名字：
#:   * `manual_required` 归「低置信度」而不是「未知字段」—— `_no_parser_report`
#:     开它的时候说的是"嗅探仲裁拿不准这个文件属于谁"（§5.4），要人做的裁决是
#:     **挑一个解析器**，这是置信度问题；
#:   * `evidence_missing` 归「未知字段」—— 解析器声明了 `known_fields` 而输入里
#:     出现了别的名字（§5.6），要人做的是**判断那个字段是什么**，这是"我们少了
#:     一份对它的认识"。
HEALTH_CATEGORIES: tuple[HealthCategory, ...] = (
    HealthCategory("orphan", "孤儿", ("orphan_subject",),
                   "从未有过任何贡献的实体（§2.3：不自动清理，交人工）"),
    HealthCategory("conflict", "冲突",
                   ("value_conflict", "merge_conflict", "identity_ambiguous"),
                   "同字段多值 / 合并后多值 / 身份归属不唯一"),
    HealthCategory("low_confidence", "低置信度", ("manual_required",),
                   "嗅探仲裁拿不准这份输入该由谁解析（§5.4）"),
    HealthCategory("unknown_field", "未知字段", ("evidence_missing",),
                   "输入里出现了解析器不认识的字段名（§5.6 的格式漂移信号）"),
    HealthCategory("out_of_scope", "越界资产", ("scope_drift",),
                   "资产落在项目 scope 的 include / exclude 之外"),
)

#: kind → 类别的反查表。从 `HEALTH_CATEGORIES` 生成，**不是**第二份声明。
HEALTH_KIND_TO_CATEGORY: dict[str, str] = {
    kind: category.key
    for category in HEALTH_CATEGORIES for kind in category.kinds
}


def health_category_for(kind: str) -> str | None:
    """这个 kind 属于哪一类；不在分类表里回 None（由守卫报出来）。"""
    return HEALTH_KIND_TO_CATEGORY.get(str(kind))


def category_spec(key: str) -> HealthCategory:
    for category in HEALTH_CATEGORIES:
        if category.key == str(key):
            return category
    raise QueryError(
        "E_UNKNOWN_CATEGORY", f"没有 {key!r} 这一类",
        hint=f"体检页的五类是：{', '.join(c.key for c in HEALTH_CATEGORIES)}",
        categories=[c.key for c in HEALTH_CATEGORIES])


def health_category(conn: sqlite3.Connection, project_id: int, key: str,
                    limit: int = DEFAULT_HEALTH_LIMIT) -> dict:
    """**一类问题**：这一类下未决的 Inbox 项。

    返回 `{"query", "key", "label", "why", "kinds", "count", "items",
    "truncated"}`。

    `count` 是**这一类未决项的全部**，`items` 是被 `limit` 截过的那一截。
    两个数分开是因为它们回答两个问题（"有几件"与"哪几件"）—— 让 `count` 等于
    `len(items)` 会让一个超过 limit 的类别看起来"就这么多"，而漏掉的那些
    没有任何提示。`truncated` 就是这个提示。
    """
    spec = category_spec(key)
    row = conn.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'"
        f" AND kind IN ({','.join('?' * len(spec.kinds))})",
        (project_id, *spec.kinds)).fetchone()
    total = int(row[0])
    items = inbox_items(conn, project_id, state="open", limit=limit, kinds=spec.kinds)
    return {"query": f"health.{spec.key}", "key": spec.key, "label": spec.label,
            "why": spec.why, "kinds": list(spec.kinds), "count": total,
            "items": items, "truncated": total > len(items)}


def health_report(conn: sqlite3.Connection, project_id: int,
                  limit: int = DEFAULT_HEALTH_LIMIT) -> dict:
    """**体检页的全部**：五类 + 三个对账用的数。

    `open_inbox` / `claimed` / `unclaimed` 不是装饰，它们是"分类表还成立吗"的
    守卫：`claimed + unclaimed == open_inbox` 必须成立，而 `unclaimed` 非空就
    意味着**有人开了一条五类里没有的待办**，那条待办在体检页上永远不会被看见。
    一个不被任何一页显示的待办和没有这条待办，对人来说是同一件事。

    口径：全部限定在这一个项目里（`metric_counts` 的 `open_inbox` 是**全库**的，
    两者刻意不同 —— 体检页看的是这个项目，顶部徽标看的是这个库）。
    """
    categories = [health_category(conn, project_id, c.key, limit=limit)
                  for c in HEALTH_CATEGORIES]
    claimed = sum(int(c["count"]) for c in categories)
    open_inbox = open_inbox_count(conn, project_id)
    unclaimed = unclaimed_inbox_kinds(conn, project_id)
    return {"query": "health", "project_id": int(project_id),
            "categories": categories, "claimed": claimed,
            "open_inbox": open_inbox,
            "unclaimed": unclaimed,
            "notes": _health_notes(categories, unclaimed, claimed, open_inbox)}


def _health_notes(categories: list[dict], unclaimed: list[dict],
                  claimed: int, open_inbox: int) -> list[str]:
    """把"这个数字现在意味着什么"跟着数字一起送出去。

    空计数器是最容易被读错的东西 —— "低置信度 0"既可能是"查过了，没有"，
    也可能是"这一类从来没有生产者"。前者是一个结论，后者是一个缺口，而它们在
    界面上长得一模一样。所以这里的规则是：**某类为零时，如果这一类在本库里从来
    没有出现过任何一条（含已裁决的），就明说它的归零是因为没有东西可归**。
    """
    notes: list[str] = []
    if unclaimed:
        notes.append(
            "有 " + str(sum(int(u["n"]) for u in unclaimed)) + " 条未决项不属于五类中的"
            "任何一类（kind：" + ", ".join(str(u["kind"]) for u in unclaimed) +
            "）—— 它们在体检页上不会被看见。分类表见 query.HEALTH_CATEGORIES。")
    if claimed != open_inbox:
        notes.append(
            f"五类之和（{claimed}）与未决总数（{open_inbox}）不一致 —— 分类表出现"
            f"了漏算，体检页上少掉的正是那 {open_inbox - claimed} 条。")
    return notes


def unclaimed_inbox_kinds(conn: sqlite3.Connection,
                          project_id: int) -> list[dict]:
    """未决项里不属于五类中任何一类的 kind（含数目）。**空列表才是对的。**

    只看 `open`：已裁决的项不属于任何一页要展示的东西，把它们算进来会让
    `claimed + unclaimed == open_inbox` 这条对账式**在历史一多时必然不成立**
    —— 一条永远为假的守卫和没有守卫是一样的。
    """
    counts: dict[str, int] = {}
    for row in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM inbox_items WHERE project_id=? AND state='open'"
            " GROUP BY kind ORDER BY kind", (int(project_id),)):
        if health_category_for(str(row["kind"])) is None:
            counts[str(row["kind"])] = int(row["n"])
    return [{"kind": kind, "n": n} for kind, n in counts.items()]


def project_scope(conn: sqlite3.Connection, project_id: int) -> dict:
    """项目的 scope 对象。**"这个项目的 scope 是什么"的唯一次查询处。**"""
    row = conn.execute("SELECT scope FROM projects WHERE id=?",
                       (int(project_id),)).fetchone()
    if row is None:
        raise QueryError("E_PROJECT_NOT_FOUND", f"项目 {project_id} 不存在")
    value = json_or_none(row["scope"])
    return value if isinstance(value, dict) else {}


def out_of_scope_entities(conn: sqlite3.Connection, project_id: int,
                          entity_ids: list[int] | None = None) -> list[dict]:
    """**落在项目 scope 之外的资产**。`domain` / `ip` / `cidr` 三类，判据见 `scope.py`。

    `entity_ids` 给了就只查这几条（写路径用：一个作业只为自己刚写进去的资产负责），
    不给就查本项目当前全部资产（读路径用：体检页问的是"这个项目里有哪些越界"）。

    三处写法都是**被实测逼出来的**，不是风格：

      * `IN (?,?,…)` 分块（`_CHUNK`）：一个 10 万观测的导入会产生同样量级的
        entity id，拼成一个 `IN (…)` 会当场撞上 SQLite 的变量上限 —— 于是
        "体检"在小库上正常、在大库上抛一个与体检毫无关系的错；
      * 取 key **分两步**，而不是一条带相关子查询的语句
        （`(SELECT k.entity_key … WHERE k.entity_id=e.id)`）：后者读起来更直观，
        但 SQLite 会选 `sqlite_autoindex_entity_keys_1` 并且**只绑定
        project_id** —— 每一行都把整个项目的 key 扫一遍，复杂度 O(实体数²)。
        实测 1 万实体、磁盘库：相关子查询 3.06s，分两步 0.032s（约 100 倍）。
        这条路径在**每次导入的末尾**都要走一遍，`bench` 的 10 万观测导入契约
        因此会红 35s —— 而单元测试的库都太小，看不见它。
      * 取 key 的那一步带 `INDEXED BY ix_entity_keys_entity`：这是**故意把执行
        计划钉住**。同一个 `WHERE` 不带 hint 时查询规划器会选自动索引
        （`SEARCH … (project_id=?)`，又是线性变平方米）。索引在 001 里就有，
        名字是契约的一部分；真要改它，这句会**响亮地**报错，而不是悄悄换成
        一个慢一百倍的等价计划。
    """
    scope_obj = project_scope(conn, project_id)
    kinds = scope_module.SCOPE_KINDS
    pid = int(project_id)
    marks = ",".join("?" * len(kinds))

    # 第一步：(id, kind) —— 只在这一步用得到 entities 表。
    rows: list[Any] = []
    if entity_ids is None:
        rows = conn.execute(
            "SELECT id, kind FROM entities WHERE project_id=? AND is_current=1"
            f" AND merged_into IS NULL AND kind IN ({marks}) ORDER BY id",
            (pid, *kinds)).fetchall()
    else:
        wanted = sorted({int(i) for i in entity_ids})
        sql = ("SELECT id, kind FROM entities WHERE project_id=? AND is_current=1"
               f" AND merged_into IS NULL AND kind IN ({marks}) AND id IN (%s)")
        for start in range(0, len(wanted), _CHUNK):
            chunk = wanted[start:start + _CHUNK]
            rows.extend(conn.execute(
                sql % ",".join("?" * len(chunk)), (pid, *kinds, *chunk)).fetchall())

    # 第二步：这些实体的规范 key（分块，见上）。
    keys: dict[int, str] = {}
    ids = [int(r["id"]) for r in rows]
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        for row in conn.execute(
                "SELECT entity_id, entity_key FROM entity_keys"
                " INDEXED BY ix_entity_keys_entity"
                " WHERE entity_id IN (%s) AND project_id=? AND canonical=1"
                % ",".join("?" * len(chunk)), (*chunk, pid)):
            keys[int(row["entity_id"])] = str(row["entity_key"])

    out: list[dict] = []
    for row in rows:
        key = keys.get(int(row["id"]))
        if key is None:                          # pragma: no cover - 坏数据
            continue
        verdict = scope_module.in_scope(scope_obj, str(row["kind"]), key)
        if verdict is False:                     # None = 不问，不是越界
            out.append({"id": int(row["id"]), "kind": str(row["kind"]),
                        "entity_key": key})
    return out


#: `IN (…)` 的分块大小。SQLite 老版本的变量上限是 999，取 500 留一倍余量。
_CHUNK = 500


def activity_log(conn: sqlite3.Connection, *, limit: int = DEFAULT_HEALTH_LIMIT,
                 action: str | None = None, job_id: int | None = None) -> list[dict]:
    """活动日志：agent 干了什么，这里看得到。"""
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
        (*params, page_size(limit))).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["detail"] = json_or_none(item["detail"])
        items.append(item)
    return items


def export_entities(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """导出用的实体 + 字段：每个实体一行，字段是 `(field, value, confidence,
    source_count)` 的列表。四种导出格式（json / csv / domains / markdown）都
    建在它上面 —— 格式是**渲染**，取数只有这一处。"""
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
    return list(entities.values())


# ---------------------------------------------------------------------------
# schema（登记表 / 词表 / 观测字段）
# ---------------------------------------------------------------------------


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])


def probe_schema_version(db_path: Path) -> int:
    """`init --dry-run` 的探针：**不建文件、不改任何东西**。

    刻意不走 `Database.connect()`（那条路要求"库必须已经建好且版本自洽"，而
    干跑最常问的恰恰是"我还什么都没建，它会做什么"），所以这里自己开一条最朴素
    的连接，只读账本。文件不存在就是 0（= 一个迁移都还没应用）。

    `PRAGMA query_only` 让"这条连接只读"变成**数据库自己**保证的事，而不是
    "我记得别写"。
    """
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:                                        # pragma: no cover
        return 0
    try:
        conn.execute("PRAGMA query_only = ON")
        return schema_version(conn)
    except sqlite3.DatabaseError as exc:
        raise QueryError("E_DB_NOT_A_DB", f"{db_path} 不是一个可读的 SQLite 库：{exc}") from exc
    finally:
        conn.close()


def kinds(conn: sqlite3.Connection) -> list[dict]:
    """kind 登记表（`003` 起冻结的那张词表）。`produced_by` 解码成数组再返回 ——
    "这一列是 JSON"是实现细节，不该让每个消费者各解一次。"""
    return [{"kind": str(r["kind"]), "lens": str(r["lens"]),
             "identity_rule": str(r["identity_rule"]),
             "fixture_path": str(r["fixture_path"]),
             "produced_by": json_or_none(r["produced_by"]) or []}
            for r in conn.execute(
                "SELECT kind, lens, identity_rule, fixture_path, produced_by"
                " FROM kind_registry ORDER BY kind")]


def relation_verbs(conn: sqlite3.Connection) -> list[dict]:
    return [{"verb": str(r["verb"]), "inverse": r["inverse"],
             "description": str(r["description"])}
            for r in conn.execute(
                "SELECT verb, inverse, description FROM relation_vocabulary"
                " ORDER BY verb")]


def observed_fields(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """本库**实际出现过**的观测字段（按 kind 分组）。

    `semantic_field` 是自由文本（由解析器决定），没有一份静态词表 —— 这份输出
    是事实，不是声明。要加字段就加一个解析器。
    """
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
            "SELECT e.kind AS kind, o.semantic_field AS field,"
            " o.value_kind AS value_kind, COUNT(*) AS n"
            " FROM observations o JOIN entities e ON e.id = o.entity_id"
            " WHERE o.project_id=? AND o.is_current=1"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2", (project_id,)):
        grouped.setdefault(str(row["kind"]), []).append(
            {"field": str(row["field"]), "value_kind": str(row["value_kind"]),
             "count": int(row["n"])})
    return [{"kind": kind, "fields": fields} for kind, fields in sorted(grouped.items())]
