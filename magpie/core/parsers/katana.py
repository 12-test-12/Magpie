"""`katana` —— ProjectDiscovery katana 的 `-jsonl` 输出（计划书 §4.3 的 urls 行之一）。

katana 是主动爬虫：它**真的走了一遍**页面里的链接、表单和 JS 里的路径。所以它
给的证据比 gau 那种历史存档强一档 —— "我刚刚在这台服务器上请求到了它"。

为什么是 `-jsonl` 而不是默认的纯文本输出
----------------------------------------
katana 的默认输出是**一行一个 URL**，与 gau 一模一样。形状相同就该是同一个
解析器（domainlist 的注释里那条"格式版本由行的形状决定，不由工具名决定"）——
可那样一来这个来源独有的两样东西就全丢了：

* **方法**（`request.method`）—— 爬虫知道它是 GET 还是 POST；
* **来源**（`request.source` / `request.attribute`）—— 这个 URL 是从哪个页面、
  哪个属性（`href` / `form` / `script`）里发现的。

`-jsonl` 是 katana 自己提供的输出形状，两样都在里面。所以这里收 `-jsonl`。

产出
----

| 记录 | 内容 |
|---|---|
| `endpoint` 实体 | key = `主机[:非默认端口] + 路径模板` |
| `domain` / `ip` / `service` 实体 + `served_by` | 同其余三个"路径列表"来源（`_urls.py`） |
| 观测 | `path`（原样）/ `path_template` / `scheme` / `port` / `method` / `status` / `source` |

与 httpx 那份 JSONL **不会互相抢**
----------------------------------
katana 的每一行也是 JSON 对象、也带 URL。判据必须是 katana 自己的签名结构
（`request` 对象里的 `endpoint`），不能是"一行是不是 JSON"：

* httpx 的判据要顶层有 `host` / `input` / `status_code` / `scheme` —— katana 的
  顶层只有 `timestamp` / `request` / `response`，两边互不认领；
* nuclei 的判据要 `template-id`，katana 没有。

`test_sniffing_separates_the_formats_cleanly` 用真实的 8 份样本跑仲裁，
分不开就会红 —— 而分不开的代价是整批文件掉进人工裁决（§5.4），新来源永远宽不起来。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from ._urls import MAX_VALUE_CHARS, EndpointSink
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["KNOWN_FIELDS", "KatanaParser", "parse_text"]

#: 本解析器**认识**的顶层字段。出现别的 key 就报未知字段（§5.6 的格式漂移信号）。
KNOWN_FIELDS: frozenset[str] = frozenset({
    "timestamp", "request", "response", "error", "duration", "file",
})

#: `request` 子对象里认识的字段。
REQUEST_FIELDS: frozenset[str] = frozenset({
    "method", "endpoint", "tag", "attribute", "source", "raw", "body", "headers",
    "host", "url", "path", "query",
})

#: `response` 子对象里认识的字段（只用到 status_code，其余登记=不报漂移）。
RESPONSE_FIELDS: frozenset[str] = frozenset({
    "status_code", "content_length", "content_type", "headers", "body",
    "technologies", "raw", "words", "lines", "title", "tls",
})

#: 单行上限。超过它的行不做 JSON 解析 —— katana 的行里唯一会长成这样的是
#: `response.body`（整页正文），而它本来就不入库。先拦下来，坏行报告给的才是
#: "这行太大"这条**准确**的原因，而不是 JSON 的括号错误。
MAX_LINE_CHARS = 1024 * 1024


class KatanaParser(Parser):
    tool = "katana"
    format_version = "2"
    known_fields = KNOWN_FIELDS
    #: 与 gau 同档：爬虫确实请求到了它，但"路径存在"与"这个接口在做接口的事"
    #: 是两回事（一个静态资源的 URL 也会落成 endpoint）。
    max_confidence = 0.85
    exclusive_group = "url-list"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是带 `request.endpoint` 的 JSON 对象"打分。

        ⚠️ 判据必须是 `request` **子对象**里的 `endpoint`，不能是"一行是不是
        JSON 对象"（httpx / nuclei 的行也都是），也不能是顶层的 `endpoint`
        （那是另一类工具的形状）。理由与 `httpx._NUCLEI_MARKERS` 那段同源：
        两个 JSONL 来源的判据一旦重叠，仲裁就拉不开差距。
        """
        del filename
        lines = [ln.strip() for ln in sample.replace("\x00", "").splitlines()]
        lines = [ln for ln in lines if ln][:50]
        if not lines:
            return 0.0
        hits = 0
        for line in lines:
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and _katana_endpoint(obj):
                hits += 1
        if hits == 0:
            return 0.0
        return 0.95 if hits / len(lines) >= 0.5 else 0.6

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐行产出，**不物化列表**。"""
        sink = EndpointSink()
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text:
                continue
            if len(text) > MAX_LINE_CHARS:
                ctx.on_bad_line(
                    lineno,
                    f"单行超过 {MAX_LINE_CHARS} 字节（{len(text)}）—— katana 里"
                    f"只有 response.body 会长成这样，而它不入库",
                    text[:MAX_VALUE_CHARS])
                continue
            try:
                obj = json.loads(text)
            except ValueError as exc:
                ctx.on_bad_line(lineno, f"JSON 解析失败：{exc}",
                                text[:MAX_VALUE_CHARS])
                continue
            if not isinstance(obj, dict):
                ctx.on_bad_line(
                    lineno, "不是 JSON 对象（katana -jsonl 每行必须是一个对象）",
                    text[:MAX_VALUE_CHARS])
                continue
            for key in obj:
                if key not in self.known_fields:
                    ctx.on_unknown_field(str(key), lineno)
            yield from self._records(obj, ctx, lineno, sink, text)

    def _records(self, obj: dict[str, Any], ctx: ParseContext, lineno: int,
                 sink: EndpointSink, text: str) -> Iterator[Record]:
        request = obj.get("request")
        if not isinstance(request, dict):
            ctx.on_bad_line(
                lineno,
                f"缺 request 对象（或它不是对象）：{type(request).__name__} ——"
                f"katana 的 URL 与方法都在里面，没有它这条记录无处可挂",
                text[:MAX_VALUE_CHARS])
            return
        for key in request:
            if key not in REQUEST_FIELDS:
                ctx.on_unknown_field(f"request.{key}", lineno)
        response = obj.get("response")
        if isinstance(response, dict):
            for key in response:
                if key not in RESPONSE_FIELDS:
                    ctx.on_unknown_field(f"response.{key}", lineno)
        else:
            response = {}

        endpoint = request.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            # 刻意**不**退化到 `request.source` / `response.url`：那是"从哪发现的"，
            # 不是"爬到了什么"。半条身份比一条坏行危险得多（nuclei 对 matched-at
            # 的处理是同一条）。
            ctx.on_bad_line(
                lineno,
                f"缺 request.endpoint（或它不是字符串）：{endpoint!r} ——"
                f"没有它这条记录算不出接口的身份", text[:MAX_VALUE_CHARS])
            return

        method = ""
        if request.get("method"):
            try:
                method = normalize.http_method(request.get("method"))
            except normalize.NormalizeError as exc:
                ctx.on_bad_line(lineno, f"request.method 不可识别：{exc}",
                                text[:MAX_VALUE_CHARS])
        # `source` 是"这个 URL 是从哪儿发现的"。它是证据（爬虫的来路），
        # 但**不是**身份的一部分 —— 同一个 URL 从十个页面链接过来是同一件事。
        yield from sink.emit(endpoint, ctx, lineno, method=method,
                             extra=(("status", response.get("status_code")),
                                    ("source", _text(request.get("source")))))


def _katana_endpoint(obj: dict[str, Any]) -> str:
    """这一行是不是 katana 的一条记录；是的话返回它的 URL（否则空串）。

    `sniff` 与 `parse` **共用这一个判据** —— 分成两套时会出现"我很确定这是
    katana 的输出，然后一行都读不出来"（domainlist 的 `accepts` 那段注释）。
    """
    request = obj.get("request")
    if not isinstance(request, dict):
        return ""
    endpoint = request.get("endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint.strip() else ""


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(KatanaParser().parse(text.splitlines(), ctx or OfflineContext()))
