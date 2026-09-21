"""`postman` —— Postman **v2** collection 的导出（计划书 §4 的接口来源行之一）。

Postman 的导出是团队里最常见的一种"接口清单"：它既不是规范（没有 schema，
没有 `required`），也不是流量（没有状态码）—— 它是**一组被命名、被分组的请求**。
所以它写**声明型**的 `declared_method`（`item` 上写的方法 = "这个集合认为这个
接口是这么调的"），不写 `status`（集合里没有观测到的状态码）。

⚠️  只认 v2，v1 必须报**可读**的错
------------------------------------
两代的形状完全不同，而导出的默认值在旧版本 Postman 里是 v1：

    v1   {"id": …, "name": …, "requests": […], "folders": […]}      ← 没有 info
    v2   {"info": {…, "schema": "…/collection/v2.1.0/collection.json"}, "item": […]}

判据是 `info.schema` 里的版本号（拿不到时退回"有 `requests` / `folders`、
没有 `info` / `item`"）。**报错而不是猜着解析**：v1 的 `requests[]` 里
`url` 是纯字符串、`headers` 是 `"K: V"` 字符串数组，猜着读会读出一个**看起来
成功、实际丢字段**的结果，而"宁可明确地拒绝，也不要假装支持"是这个项目一贯的
取舍（`check.py` 的 C2 写过同一句话）。

⚠️  变量：**不展开**
----------------------
`{{base_url}}/pets` 在 Postman 里极常见，但把它展开需要**环境文件**（另一份
文件，可能带凭据），而解析器只被交给一个文件。所以带 `{{…}}` 的 URL **记一条
坏行**，不猜、不清洗（把它清洗成 `base_url/pets` 会造出一个不存在的接口，而
那是**静默**的）。

产出
----

| 记录 | 内容 |
|---|---|
| `apischema` 实体 | key = 起源（每个 item 的 URL 各自算 —— 一个集合可以跨主机） |
| `endpoint` 实体 + `served_by` | 每个 item 的 URL |
| 关系 | `endpoint --declared_in--> apischema` |
| 声明型观测 | `declared_method`（一组，排序）；`declared_status` 取 `response[].code` |
| 文档级观测 | `api_title`（`info.name`）/ `api_version`（`info.version`，v2.1 才有） |
| 参数树 | `param.<名字>`：`url.query[].key` + body 的键名 |

请求体**只取键名**
-------------------
`body.raw` 里的 JSON、`body.urlencoded[]` 的 value 都可能带密码（Postman 的
"另存为示例"经常把真实 token 一起存进去）。本步只取**形状**，所以：

* `mode=raw` 时**只**尝试 `json.loads` 出顶层键名，解析不出来就一个参数都不落
  （一份 XML / GraphQL body 的键不是"参数"）；
* `mode=urlencoded` / `formdata` 只取 `key`，**`value` 一个字都不读**；
* `mode` **本身不进参数值** —— 位置一律是粗档 `body`，理由见 `_apis.py` 的文件头
  （同一个参数换个编码就变成两个值，是拿请求的维度污染参数的维度）。

这与 HAR 那边是同一条线（`har.py` 的文件头）。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from . import _json
from ._apis import ApiSink
from ._urls import MAX_VALUE_CHARS
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["KNOWN_FIELDS", "ITEM_FIELDS", "REQUEST_FIELDS", "PostmanParser",
           "parse_text"]

#: 顶层：v2 collection 是 `info` + `item`。
KNOWN_FIELDS: frozenset[str] = frozenset({
    "info", "item", "event", "variable", "auth", "protocolProfileBehavior",
})

#: `info` 里认识的字段。
INFO_FIELDS: frozenset[str] = frozenset({
    "name", "_postman_id", "description", "schema", "version", "_exporter_id",
    "_collection_link",
})

#: `item[]` 里认识的字段 —— 一个 item 要么是**请求**（有 `request`），
#: 要么是**文件夹**（有 `item`），v2 用同一个数组表达这两件事。
ITEM_FIELDS: frozenset[str] = frozenset({
    "name", "item", "request", "response", "event", "id", "description",
    "variable", "auth", "protocolProfileBehavior",
})

#: `item.request` 里认识的字段。
REQUEST_FIELDS: frozenset[str] = frozenset({
    "method", "header", "body", "url", "auth", "description", "proxy",
    "certificate", "proxyProfileBehavior",
})

#: `item.request.url`（对象形态）里认识的字段。
URL_FIELDS: frozenset[str] = frozenset({
    "raw", "protocol", "host", "path", "port", "query", "hash", "variable",
})

#: `item.request.body` 里认识的字段。
BODY_FIELDS: frozenset[str] = frozenset({
    "mode", "raw", "urlencoded", "formdata", "file", "graphql", "options",
    "disabled", "description",
})

#: URL 里出现它 = 用了 Postman 变量（不展开，见文件头）。
_VARIABLE_MARK = "{{"


class PostmanParser(Parser):
    tool = "postman"
    format_version = "1"
    known_fields = KNOWN_FIELDS
    #: 比契约低一档：集合是**人维护**的清单，会过期、会有手工改过的临时值
    #: （OpenAPI 的 `max_confidence` 是 1.0，因为它是提供方自己签名的）。
    max_confidence = 0.9
    exclusive_group = "api-schema"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """靠**顶层键**认：`info` **且** `item` 同时出现。

        两个键都要 —— 一份 Swagger 2 文档也有 `info`（`title` / `version`），
        只要 `info` 就会把它从 openapi 那边抢过来，而抢错的那个方向正是 §5.4
        说的"整批进人工裁决"。HAR 的 `log` 与这里的两个键同样不相交。
        """
        del filename
        text = sample.replace("\x00", "").lstrip()
        if not text.startswith("{"):
            return 0.0
        keys = _json.top_level_keys(text)
        return 0.95 if ("info" in keys and "item" in keys) else 0.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        text = _json.collect_json(lines, ctx, "这份 Postman 集合")
        if text is None:
            return
        if not text.strip():
            return
        doc = _json.load_json(text, ctx, "Postman 集合")
        if doc is None:
            return
        if not isinstance(doc, dict):
            ctx.on_bad_line(1, f"集合的顶层必须是一个对象（收到 "
                               f"{type(doc).__name__}）", "")
            return

        reason = _not_v2(doc)
        if reason:
            ctx.on_bad_line(1, reason, "")
            return

        _json.report_unknown(doc, KNOWN_FIELDS, ctx, "")
        info = doc.get("info")
        _json.report_unknown(info, INFO_FIELDS, ctx, "info.")

        sink = ApiSink()
        title = info.get("name") if isinstance(info, dict) else None
        version = info.get("version") if isinstance(info, dict) else None
        # 文档级观测要等知道起源才能落 —— 一个集合里的 item 可以跨主机，所以
        # 起源是**逐个 item** 算出来的，标题先攒着，遇到第一个起源时补上。
        pending = [(str(title), str(version)) if title or version else None]

        for item in _json.walk(_json.as_list(doc.get("item")), "item", ctx):
            yield from self._item(item, sink, ctx, pending)

        yield from sink.declared()

    def _item(self, item: dict, sink: ApiSink, ctx: ParseContext,
              pending: list) -> Iterator[Record]:
        """一个 item：既可能是请求，也可能是文件夹（v2 用同一个数组表达两者）。"""
        _json.report_unknown(item, ITEM_FIELDS, ctx, "item[].")
        for record in self._request(item, sink, ctx, pending):
            yield record
        for child in _json.walk(_json.as_list(item.get("item")), "item[].item", ctx):
            if ctx.cancelled():
                return
            yield from self._item(child, sink, ctx, pending)

    def _request(self, item: dict, sink: ApiSink, ctx: ParseContext,
                 pending: list) -> Iterator[Record]:
        request = item.get("request")
        if request is None:
            return                              # 文件夹：没有 request，正常
        if not isinstance(request, dict):
            ctx.on_bad_line(1, f"item[{item.get('name')!r}] 的 `request` 必须是"
                               f"对象（收到 {type(request).__name__}）", "")
            return
        _json.report_unknown(request, REQUEST_FIELDS, ctx, "item[].request.")

        url = _absolute_url(request.get("url"))
        if url is None:
            ctx.on_bad_line(1, f"item[{item.get('name')!r}] 的 `request.url` 认不出"
                               f"（既不是字符串，也不是含 host 的对象）", "")
            return
        if _VARIABLE_MARK in url:
            ctx.on_bad_line(
                1, f"URL 里有 Postman 变量（`{{{{…}}}}`）：{url[:120]!r} —— "
                   f"本解析器**不做变量展开**（展开需要另一份环境文件，而它可能"
                   f"带凭据）。把集合导出成不含变量的形式再来", "")
            return
        _json.report_unknown(request.get("url"), URL_FIELDS, ctx,
                             "item[].request.url.")
        _json.report_unknown(request.get("body"), BODY_FIELDS, ctx,
                             "item[].request.body.")

        method = ""
        if request.get("method"):
            try:
                method = normalize.http_method(request.get("method"))
            except normalize.NormalizeError:
                ctx.on_unknown_field(f"request.method={request.get('method')!r}")

        # 文档级观测在**第一个能算出起源的 item** 上落一次（`ApiSink` 自己按
        # (起源, 字段, 值) 去重，所以一个集合跨主机时每个起源各得一份标题）。
        # 先显式声明实体再落观测：一个集合的 `item` 可以跨主机，而"标题属于
        # **哪个**起源"要等第一个能算出起源的 item 才知道 —— 顺序因此是
        # 实体 → 观测 → 这个 item 的全部记录（与 `emit_operation` 自己的顺序一致，
        # 那里实体已经声明过，`_first` 会把它去重掉）。
        if pending and pending[0]:
            yield from sink.apischema(url, ctx, 1)
            title, version = pending[0]
            pending[0] = None
            yield from sink.apischema_fact(url, "api_title", title, ctx)
            yield from sink.apischema_fact(url, "api_version", version, ctx)

        yield from sink.emit_operation(
            url, ctx, 1, declared_method=method,
            declared_status=_declared_status(item), params=_params(request))


def _not_v2(doc: dict) -> str:
    """不是 Postman v2 时返回一句**人话**（是 v2 时返回空串）。"""
    info = doc.get("info")
    schema = info.get("schema") if isinstance(info, dict) else None
    if isinstance(schema, str) and "v1" in schema:
        return ("这是 Postman **v1** 的集合（`info.schema` 指向 v1）—— 本解析器"
                "只认 v2。在 Postman 里用「导出 → Collection v2.1」重导一份再来")
    if not isinstance(info, dict) or "item" not in doc:
        if "requests" in doc or "folders" in doc:
            return ("这是 Postman **v1** 的集合（顶层是 `requests` / `folders`，"
                    "没有 `info` + `item`）—— 本解析器只认 v2。在 Postman 里用"
                    "「导出 → Collection v2.1」重导一份再来")
        return ("顶层没有 `info` + `item` —— 这不是一份 Postman v2 collection"
                "（v2 的形状是 `{info: …, item: […]}`）")
    return ""


def _absolute_url(url: Any) -> str | None:
    """`request.url` → 一个**绝对** URL 字符串；认不出时返回 `None`。

    Postman 的 `url` 有两种形态：字符串（`raw` 那一种）或对象
    （`{protocol, host[], path[], query[]}`）。对象形态缺 `protocol` 时按 https
    补 —— 这是 Postman 自己的默认值，不是这里的猜测。
    """
    if isinstance(url, str):
        return url.strip() or None
    if not isinstance(url, dict):
        return None
    raw = url.get("raw")
    if isinstance(raw, str) and "://" in raw:
        return raw.strip()
    host = ".".join(str(part) for part in _json.as_list(url.get("host")) if part)
    if not host:
        return raw.strip() if isinstance(raw, str) and raw.strip() else None
    protocol = url.get("protocol")
    scheme = protocol if isinstance(protocol, str) and protocol else "https"
    path = "/".join(str(part) for part in _json.as_list(url.get("path")) if part != "")
    text = f"{scheme}://{host}"
    if url.get("port"):
        text += f":{url['port']}"
    text += "/" + path
    query = _query_string(url.get("query"))
    if query:
        text += "?" + query
    fragment = url.get("hash")
    if isinstance(fragment, str) and fragment:
        text += "#" + fragment
    return text


def _query_string(query: Any) -> str:
    """`url.query[]` → `k=v&k=v`（**值原样保留** —— 它是路径证据的一部分，
    与 3.3 对 `path` 观测的口径一致）。"""
    pairs: list[str] = []
    for item in _json.as_list(query):
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        value = item.get("value")
        pairs.append(f"{key}={value}" if isinstance(value, (str, int, float)) else key)
    return "&".join(pairs)


def _params(request: dict) -> list[tuple[str, dict]]:
    """query + body 的参数（**body 只取键名**，见文件头）。"""
    out: list[tuple[str, dict]] = []
    url = request.get("url")
    if isinstance(url, dict):
        for item in _json.as_list(url.get("query")):
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                out.append((item["key"], {"in": "query"}))
    body = request.get("body")
    if not isinstance(body, dict):
        return out
    # 位置一律是**粗档** `body`，不按 `mode` 分 —— 理由见 `_apis.py` 的文件头。
    mode = body.get("mode")
    if mode in ("urlencoded", "formdata"):
        for item in _json.as_list(body.get(mode)):
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                out.append((item["key"], {"in": "body"}))
    elif mode == "raw":
        for name in _json_object_keys(body.get("raw")):
            out.append((name, {"in": "body"}))
    return out


def _json_object_keys(text: Any) -> list[str]:
    """`body.raw` → 顶层键名（**不是** JSON 对象时返回空 —— 一份 XML/GraphQL
    body 的"键"不是参数，硬解会造出不存在的参数）。"""
    if not isinstance(text, str) or not text.strip().startswith("{"):
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    return [str(key) for key in parsed]


def _declared_status(item: dict) -> list[str]:
    """`item.response[].code` → 声明型的状态码。

    Postman 把"保存下来的示例响应"放在 item 上（不是 request 上），它是这个集合
    唯一有的状态码来源。没有就返回空 —— 空**不落观测**（缺观测表达的正是
    "这个来源不产出状态码"）。
    """
    out: list[str] = []
    for response in _json.as_list(item.get("response")):
        if isinstance(response, dict) and response.get("code") is not None:
            out.append(str(response["code"]))
    return out


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(PostmanParser().parse(text.splitlines(), ctx or OfflineContext()))
