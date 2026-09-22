"""`har` —— HTTP Archive（浏览器 / 代理抓下来的请求记录）。

HAR 与另外三个来源**不是同一种证据**，这一点必须先说清楚：OpenAPI 与 Postman
说的是"**契约声称**这个接口长这样"，curl 是"**这一条命令**要发什么"，而 HAR 是
"**当时真的发生了**"—— 它有状态码、有真实的查询串、有别人访问过的路径。所以它
写的是**观测型**的 `method` / `status`（单值），不是 `declared_*`（一组）。

⚠️  本步的范围边界：**只取接口形状**（路径 / 方法 / 参数 / 状态码）
---------------------------------------------------------------------
计划书写死了这一条，而 HAR 是唯一一个"顺手就能多存一百倍数据"的来源：
`entry.response.content.text` 里是整个响应体，`request.postData.text` 里是整个
请求体。**本解析器一个字都不读它们**：

* 响应体是阶段 4 的流量题（4.1 / 4.2 / 4.3），提前存进来会让本步的体积失控，
  而且"响应体该不该脱敏、怎么索引"这些问题要等那一阶段才看得清；
* 请求体里的字段**只取键名、值一个字都不落** —— 那既符合"只取形状"，也是 G4
  的同一个方向（`application/x-www-form-urlencoded` 的 body 里放密码是最常见
  的一种凭据泄漏姿势）。

所以 `parse()` 里没有出现 `content` / `text` 这两个键，`response` 只读 `status`。

HAR 与 Postman 都是 JSON，靠**顶层键**区分
-------------------------------------------
`log`（HAR）vs `info` + `item`（Postman）。这条判据是本步点名要求的一条：不许
用"试着解析一下看哪个不报错" —— 那种写法在遇到坏文件时会给出随机的归属
（`_json.py` 的文件头把这条讲透了）。

产出
----

| 记录 | 内容 |
|---|---|
| `apischema` 实体 | key = 起源（**每一跳各自算** —— 一次浏览跨好几个主机，这是对的） |
| `endpoint` 实体 + `served_by` | 每一跳的 URL（路径模板折叠由 3.3 的 `EndpointSink` 负责） |
| 关系 | `endpoint --declared_in--> apischema` |
| 观测 | `path` / `path_template` / `scheme` / `port`（3.3 的）+ `method` / `status` |
| 参数树 | `param.<名字>`：`queryString[].name` + `postData.params[].name` |
| `flow` 实体 + `involves` | 4.1 起：同一主机的若干跳折成**一段会话**（`_flows.FlowSink`） |

4.1 的那一半：HAR 也是**流量**的一条来源
------------------------------------------
一份 HAR 记的是"当时真的发生了"（文件头第一段），所以它同样能回答"这段会话有
多大、什么时候、打到哪台主机"。会话的形状与切分规则由 `_flows.py` 定死（与
`tshark` 共用同一份实现），这里只负责把每一跳翻译成一条观测。

三处**只能这么定**的裁定，写在这里免得下游各自发现：

* **`proto` 落的是 scheme（`http` / `https`），不是 `tcp`。** HAR 里没有传输层
  —— 浏览器只告诉你"这是一次 HTTPS 事务"。写 `tcp` 是编，而编出来的协议会跟着
  会话的身份走一辈子。代价写在明处：同一段会话在 HAR 与抓包两种来源下**不会**
  折成同一个 flow（`https|…` vs `tcp|…`），它们是两个视角的两次观测，本步不合并
  —— 合并需要 IP 与端口的对应关系，而 HAR 只在 `serverIPAddress` 偶尔给出的
  时候才有它（下面那条）。
* **`serverIPAddress` 既不进身份、也不落观测。** 它**经常缺失**（Chrome 就不写），
  一个"有时有有时没有"的字段落成观测，会让同一段会话在两次抓包里长得不一样；
  而进了身份则会给同一台主机造出**第二个**实体（域名一个、IP 一个）和一条幻影
  `involves` 边。主机的身份取 URL 里的那个 —— 与 3.4 的 `endpoint` / `service`
  落的是同一个实体。
* **`packet_count` 这个名字在 HAR 里换成 `request_count`。** 一次 HTTP 事务不是
  一个包，把两者记成同一个字段名，等于在库里写下一个**说不通**的数（`_flows`
  的文件头把"计数口径"列为两个来源唯一的实质差别）。

计数与字节数的口径见 `_flows.FlowSink`：`byte_count` 是**正文**字节（
`request.bodySize + response.bodySize`，规范的 -1 按 0 记），不含头。

`log.entries` 为空**是一条可读的坏行**，不是"零事实的成功"
----------------------------------------------------------
一份没有请求的抓包取不到任何接口形状，而它在报告里长得与"解析成功但什么都没
找到"一模一样 —— 后者会让人以为这份 HAR 里的接口都被认出来了。报告里说一句
"这份 HAR 里没有任何请求"，使用者立刻知道该去查抓包本身。
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from .. import normalize
from . import _json
from ._apis import ApiSink
from ._flows import FlowSink
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["ENTRY_FIELDS", "KNOWN_FIELDS", "REQUEST_FIELDS", "RESPONSE_FIELDS",
           "HarParser", "parse_text"]

#: 顶层：HAR 只有一个 `log`（其余是工具自留的 `_` 前缀键）。
KNOWN_FIELDS: frozenset[str] = frozenset({"log"})

#: `log` 里认识的字段。
LOG_FIELDS: frozenset[str] = frozenset({
    "version", "creator", "browser", "pages", "entries", "comment",
})

#: `log.entries[]` 里认识的字段（真实 HAR 里工具会加 `_` 前缀的私有键，
#: 按 §5.6 的惯例不报 —— 与顶层那条同口径）。
ENTRY_FIELDS: frozenset[str] = frozenset({
    "pageref", "startedDateTime", "time", "request", "response", "cache",
    "timings", "serverIPAddress", "connection", "comment",
})

#: `entry.request` / `entry.response` 里认识的字段。
REQUEST_FIELDS: frozenset[str] = frozenset({
    "method", "url", "httpVersion", "cookies", "headers", "queryString",
    "postData", "headersSize", "bodySize", "comment",
})
RESPONSE_FIELDS: frozenset[str] = frozenset({
    "status", "statusText", "httpVersion", "cookies", "headers", "content",
    "redirectURL", "headersSize", "bodySize", "comment",
})


class HarParser(Parser):
    tool = "har"
    format_version = "1"
    known_fields = KNOWN_FIELDS
    #: 比契约低一档：抓包说的是"当时发生了什么"，一次 404 不代表这个接口不存在
    #: （反过来也一样 —— 一条三年前的记录不代表它现在还能用）。同 gau 的口径。
    max_confidence = 0.8
    exclusive_group = "api-schema"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """靠**顶层键** `log` 认 —— 与 Postman 的 `info` + `item` 是两条不相交的线。

        用截断容忍的顶层键扫描而不是 `json.loads`：一次真实浏览的 HAR 远超过
        64 KiB 的样本上限，`json.loads` 在它上面**必然**失败（`_json.py` 头）。
        """
        del filename
        text = sample.replace("\x00", "").lstrip()
        if not text.startswith("{"):
            return 0.0
        keys = _json.top_level_keys(text)
        # `log` 单独一个键就够：ffuf 要 `results`+`config`，Postman 要 `info`+`item`，
        # 三者的键集刻意不相交。
        return 0.95 if "log" in keys else 0.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        text = _json.collect_json(lines, ctx, "这份 HAR")
        if text is None:
            return
        if not text.strip():
            return                          # 空文件不是坏行（与 ffuf 同口径）
        doc = _json.load_json(text, ctx, "HAR")
        if doc is None:
            return
        if not isinstance(doc, dict):
            ctx.on_bad_line(1, f"HAR 的顶层必须是一个对象（收到 "
                               f"{type(doc).__name__}）", "")
            return

        _json.report_unknown(doc, KNOWN_FIELDS, ctx, "")
        log = doc.get("log")
        if not isinstance(log, dict):
            ctx.on_bad_line(1, "顶层没有 `log` 对象 —— 这不是一份 HAR"
                               "（HAR 的形状是 `{log: {entries: […]}}`）", "")
            return
        _json.report_unknown(log, LOG_FIELDS, ctx, "log.")

        entries = log.get("entries")
        if entries is None:
            ctx.on_bad_line(1, "`log` 里没有 `entries` —— 这不是一份 HAR", "")
            return
        if not isinstance(entries, list):
            ctx.on_bad_line(1, f"`log.entries` 必须是数组（收到 "
                               f"{type(entries).__name__}）", "")
            return
        if not entries:
            # 空抓包**是一条要说的坏行**，不是"零事实的成功"（文件头）。
            ctx.on_bad_line(1, "`log.entries` 是空的 —— 这份 HAR 里没有任何请求，"
                               "取不到接口形状", "")
            return

        sink = ApiSink()
        #: 4.1 的会话摘要在**同一次遍历**里累积，文件末尾一次落记录（`_flows`
        #: 的文件头讲了为什么切分规则只能有一份实现）。
        flows = FlowSink(count_field=REQUEST_COUNT_FIELD)
        for index, entry in enumerate(_json.walk(entries, "log.entries", ctx), start=1):
            if ctx.cancelled():
                return
            yield from self._entry(entry, index, sink, flows, ctx)
        yield from sink.declared()
        yield from flows.finish(ctx)

    def _entry(self, entry: dict, index: int, sink: ApiSink, flows: FlowSink,
               ctx: ParseContext) -> Iterator[Record]:
        _json.report_unknown(entry, ENTRY_FIELDS, ctx, "log.entries[].")
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, dict):
            ctx.on_bad_line(1, f"log.entries[{index}] 没有 `request` 对象 ——"
                               f"没有请求就没有接口形状", "")
            return
        _json.report_unknown(request, REQUEST_FIELDS, ctx, "log.entries[].request.")
        if isinstance(response, dict):
            _json.report_unknown(response, RESPONSE_FIELDS, ctx,
                                 "log.entries[].response.")

        url = request.get("url")
        if not isinstance(url, str) or not url.strip():
            ctx.on_bad_line(1, f"log.entries[{index}].request 缺 url（或它不是"
                               f"字符串）：{url!r}", "")
            return

        method = ""
        if request.get("method"):
            try:
                method = normalize.http_method(request.get("method"))
            except normalize.NormalizeError:
                # 认不出的方法如实报成漂移，不是折成空串抹平（与 ffuf / nuclei
                # 处理第七种 severity 同一条口径）。
                ctx.on_unknown_field(f"request.method={request.get('method')!r}")

        yield from self._flow(entry, index, url.strip(), request, response, flows, ctx)
        yield from sink.emit_operation(
            url.strip(), ctx, 1, method=method,
            status=response.get("status") if isinstance(response, dict) else None,
            params=[
                *[(item.get("name"), {"in": "query"})
                  for item in _json.as_list(request.get("queryString"))
                  if isinstance(item, dict) and isinstance(item.get("name"), str)],
                # 位置是**粗档**（`body`），不带 MIME —— 理由见 `_apis.py` 的文件头：
                # `application/json` 与 `x-www-form-urlencoded` 是**这一次请求**的
                # 属性，塞进参数值会让同一个参数换个编码就变成两个值（一条假冲突）。
                *[(item.get("name"), {"in": "body"})
                  for item in _form_params(request.get("postData"))],
            ])

    def _flow(self, entry: dict, index: int, url: str, request: dict,
              response: Any, flows: FlowSink, ctx: ParseContext) -> Iterator[Record]:
        """一跳 → 一条会话观测（4.1）。

        与 `emit_operation` 分开两次调用 `endpoint_identity` 是**刻意的**：
        这里的身份是**会话**（5 元组 + 时间窗），那里的身份是**接口**（主机 +
        路径模板）。两者共用的是同一份 URL 归一化结果，但把它们塞进一次
        `emit_operation` 会让"这一段代码在算哪种身份"变得不可读 —— 而接口那
        一半有 3.4 的四个来源在共用（`_apis.py`）。

        它**不产出记录**：会话是文件末尾一次落的（`FlowSink.finish`），所以
        这里返回的永远是一个空列表，`yield from` 它只是为了让调用点与上面那次
        `emit_operation` 长得一样。
        """
        try:
            identity = normalize.endpoint_identity(url)
        except normalize.NormalizeError:
            return                      # 坏 URL 已经在 `emit_operation` 里报过一次
        scheme = str(identity["scheme"])
        if not scheme:
            # 相对 URL（`base` 只有 dirsearch 有）。HAR 的规范要求绝对 URL，
            # 而"半个 URL"算不出会话身份 —— 静默跳过，`emit_operation` 那边
            # 会给出可读的那条报错。
            return
        size = 0
        if isinstance(response, dict):
            size += _body_size(response.get("bodySize"))
        size += _body_size(request.get("bodySize"))
        yield from flows.observe(
            proto=scheme,
            src_ip="", src_port=None,          # HAR 里没有源地址（文件头）
            dst_ip=str(identity["host"]), dst_port=identity["port"],
            at=entry.get("startedDateTime"),
            size=size, ctx=ctx, lineno=index)


#: 4.1 的计数口径：HAR 的一"条"是一次 **HTTP 事务**，不是一个包。
#: 见文件头"三处只能这么定的裁定"第三条 —— 把两者记成同一个字段名，等于在库里
#: 写下一个说不通的数。
REQUEST_COUNT_FIELD = "request_count"


def _body_size(raw: Any) -> int:
    """HAR 的 `bodySize` → 非负的整数。**-1 按 0 记**（`_flows._size` 同一条口径：
    规范用它表示"响应体没有回传"，而把负数字节累加进会话摘要会得到一个说不通的
    负数）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return int(raw) if raw > 0 else 0


def _form_params(post_data: Any) -> list[dict]:
    """`request.postData.params[]` —— **只取 `name`，`value` 一个字都不读**。

    请求体里的值可能是密码 / token（文件头讲了为什么），而"这个接口收哪些字段"
    只需要键名。
    """
    if not isinstance(post_data, dict):
        return []
    params = post_data.get("params")
    if not isinstance(params, list):
        return []
    return [item for item in params
            if isinstance(item, dict) and isinstance(item.get("name"), str)]


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(HarParser().parse(text.splitlines(), ctx or OfflineContext()))
