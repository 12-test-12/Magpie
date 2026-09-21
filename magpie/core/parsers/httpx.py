"""`httpx` —— ProjectDiscovery httpx 的 `-json` 输出（JSONL，计划书 §4.2）。

**这是计划书 §4.2 的核心目标那一步**：`tech[]` 不只是"一条观测"，它要变成
`product` 实体 + `service --runs--> product` 关系。这样"哪几个目标还在用
Laravel 8 / Nginx 1.18"才是一个**图上的查询**，而不是对着一列文本做 LIKE。

产出
----

| 记录 | 内容 |
|---|---|
| `domain` 实体 | 每行一个 host |
| `ip` 实体 + `resolves_to` | 来自 `a` / `aaaa` |
| `service` 实体 | key = `host:port`（无端口退化为 `host:scheme`） |
| `product` 实体 + `runs` | 来自 `tech[]` / `technologies[]` |
| 观测 | 服务级：port / scheme / status_code / content_length / webserver / content_type / final_url / tls_version / tls_cipher / favicon / body_hash / failed；主机级：title / cname |

**为什么不把 `title` / `cname` 挂在 service 上**（或反过来）：分工要**不重叠**。
同一条值在两处各记一次会让"观测数"这个指标虚高，也让镜头页出现重复行。
计划书 §4.1 把标题与 CNAME 归在**域名**上（它们是"这台主机"的属性），
端口/状态码/中间件/内容类型/最终 URL 归在**服务**上（它们是"这个端口上跑的
那个东西"的属性）。

`body` / `header` / `response` 这类正文**一律不入库**：它们体积大、
里面全是会话信息，而且 httpx 的归档响应是阶段 4 的离线指纹引擎的输入，
不是这里的事。它们留在 `known_fields` 里（认识 = 不报格式漂移）但被忽略。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from .base import OfflineContext, Parser, ParseContext, RawEntity, RawObservation, \
    RawRelation, Record

__all__ = ["HttpxParser", "product_key", "service_key", "split_tech"]

#: 本解析器**认识**的字段。出现别的 key 就报未知字段（§5.6 的格式漂移信号）。
#: 同时收 `tech`（现行 httpx）与 `technologies`（旧版），两者都认识但只取其一。
KNOWN_FIELDS: frozenset[str] = frozenset({
    "timestamp", "port", "url", "input", "title", "scheme", "webserver",
    "content_type", "method", "host", "path", "status_code", "content_length",
    "failed", "technologies", "tech", "cname", "cnames", "tls", "a", "aaaa",
    "cdn_name", "cdn_type", "favicon", "hash", "header", "body", "jarm_hash",
    "chain_status_codes", "response_time", "final_url", "words", "lines",
    "location", "error", "ip", "resolvers", "sni", "tls_grab", "raw_header",
    "request", "response", "duration", "knowledgebase", "csp", "websocket",
    "pipeline", "stored_response_path", "cdn", "asn",
})

#: 挂在 **domain** 上的字段；其余（端口/状态码/中间件/内容类型/最终 URL）挂 service。
_DOMAIN_OBS_FIELDS = ("title",)

#: 一条 host 最多收几个 IP。防的是一个畸形/恶意行塞 10 万个地址把库撑爆。
_MAX_ADDRESSES = 16
#: 一条 host 最多收几个 CNAME。
_MAX_CNAMES = 16

#: nuclei 的 `-jsonl` 也是每行一个 JSON 对象，而且**带 `host`**。上面那条
#: "有 host / input / status_code / scheme 就算 httpx"的判据会把它整份认领
#: （0.95），于是同一份文件两个解析器都拿满分、仲裁因为拉不开差距而把它推给
#: 人工裁决（§5.4）—— 3.2 加了 nuclei 之后这条是**必然**会撞上的。
#:
#: 判据是"排除 nuclei 自己的签名字段"，**不是**"排除带 info 的对象"：
#: 后者是一句关于另一个工具的猜测，而这个列表是它真实写在每一行里的东西。
#: httpx 自己的输出里没有这两个 key，所以这条排除不会误伤它（黄金夹具守着）。
_NUCLEI_MARKERS = ("template-id", "matched-at")


class HttpxParser(Parser):
    tool = "httpx"
    format_version = "1"
    known_fields = KNOWN_FIELDS
    max_confidence = 0.95
    exclusive_group = "http-probe"

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是带 host 的 JSON 对象"打分。

        阈值刻意放在 **0.5** 而不是 1.0：真实 JSONL 里断行/截断是常态
        （我们自己的样例里就有一条），拿"全部行都合法"当采用条件，会让一条
        坏行把整份文件推给人工裁决。

        nuclei 的行（带 `template-id`）**不**计入 —— 它同样是带 `host` 的 JSON
        对象，理由见 `_NUCLEI_MARKERS`。
        """
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
            if not isinstance(obj, dict) or _is_nuclei_finding(obj):
                continue
            if "host" in obj or "input" in obj or "status_code" in obj or "scheme" in obj:
                hits += 1
        if hits == 0:
            return 0.0
        return 0.95 if hits / len(lines) >= 0.5 else 0.6

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        seen_host: set[str] = set()
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text:
                continue
            # 一行一条记录，但**不做整体截断**：`body` 再大也是这一行的事，
            # 截断会让 JSON 变成不可解析 → 每条正常记录都被判成坏行。
            try:
                obj = json.loads(text)
            except ValueError as exc:
                ctx.on_bad_line(lineno, f"JSON 解析失败：{exc}", text[:400])
                continue
            if not isinstance(obj, dict):
                ctx.on_bad_line(lineno, "不是 JSON 对象（JSONL 的每一行必须是对象）",
                                text[:400])
                continue
            for key in obj:
                if key not in self.known_fields:
                    ctx.on_unknown_field(str(key), lineno)
            yield from self._records(obj, ctx, lineno, seen_host)

    # ---- 单个 JSON 对象 → 记录 ----

    def _records(self, obj: dict[str, Any], ctx: ParseContext, lineno: int,
                 seen_host: set[str]) -> Iterator[Record]:
        host = normalize.try_domain(obj.get("host") or obj.get("input") or "")
        if host is None:
            ctx.on_bad_line(
                lineno,
                f"host / input 不是合法域名：{obj.get('host') or obj.get('input')!r}",
                "")
            return
        if host not in seen_host:
            seen_host.add(host)
            yield RawEntity(kind="domain", key=host, display=host)

        port = obj.get("port")
        scheme = str(obj.get("scheme") or "http")
        svc = service_key(host, port, scheme)

        # --- IP：a / aaaa → ip 实体 + resolves_to ---
        for addr in _as_list(obj.get("a"))[:_MAX_ADDRESSES]:
            ip = normalize.try_ip(addr)
            if ip is None:
                ctx.on_bad_line(lineno, f"a[] 不是合法 IP：{addr!r}", "")
                continue
            yield RawEntity(kind="ip", key=ip, display=ip)
            yield RawRelation(src=("domain", host), dst=("ip", ip), rel="resolves_to")
        for addr in _as_list(obj.get("aaaa"))[:_MAX_ADDRESSES]:
            ip = normalize.try_ip(addr)
            if ip is None:
                ctx.on_bad_line(lineno, f"aaaa[] 不是合法 IP：{addr!r}", "")
                continue
            yield RawEntity(kind="ip", key=ip, display=ip)
            yield RawRelation(src=("domain", host), dst=("ip", ip), rel="resolves_to")

        # --- service ---
        yield RawEntity(kind="service", key=svc, display=svc,
                        subtitle=f"{scheme}://{host}" + (f":{port}" if port is not None
                                                         else ""))
        for name, value in (
            ("port", port),
            ("scheme", scheme),
            ("status_code", obj.get("status_code")),
            ("content_length", obj.get("content_length")),
            ("content_type", obj.get("content_type")),
            ("final_url", obj.get("final_url")),
            ("favicon", obj.get("favicon")),
            ("body_hash", obj.get("hash")),
            ("cdn", obj.get("cdn_name") or obj.get("cdn_type")),
        ):
            if value is None or value == "":
                continue
            yield RawObservation(entity_ref=("service", svc), type=name,
                                 value=value)

        # `webserver` 也要过归一化 —— 观测的身份是 (entity, field, value)，
        # 所以 `webserver=Apache` 与 `webserver=apache` 会变成**两条**观测，
        # 在服务镜头里表现为一个假的冲突。走 `split_tech` 之后它与 `tech[]`
        # 用的是同一套产品词表（`Apache` → `httpd`），两个字段不再各说各话。
        webserver = obj.get("webserver")
        if webserver:
            ws_name, ws_version = split_tech(str(webserver))
            if ws_name:
                yield RawObservation(entity_ref=("service", svc),
                                     type="webserver", value=ws_name)
            if ws_version:
                yield RawObservation(entity_ref=("service", svc),
                                     type="webserver_version", value=ws_version)

        # `failed` 只在**真失败**时记：给每个成功的主机写一条 "failed=False"
        # 是零信息的噪声 —— 它会实打实地抬高观测数与存储。
        if obj.get("failed"):
            yield RawObservation(entity_ref=("service", svc), type="failed",
                                 value="true")

        tls = obj.get("tls")
        if isinstance(tls, dict):
            for name, value in (("tls_version", tls.get("version")),
                                ("tls_cipher", tls.get("cipher"))):
                if value:
                    yield RawObservation(entity_ref=("service", svc),
                                         type=name, value=str(value))

        # --- 主机级观测（§4.1：title / cname 归在域名上）---
        for name in _DOMAIN_OBS_FIELDS:
            value = obj.get(name)
            if value in (None, "", []):
                continue
            yield RawObservation(entity_ref=("domain", host), type=name, value=value)

        # CNAME 归一化后再入库：`app.cdn.example.net.` 与 `APP.CDN.example.net`
        # 必须落成同一条观测，否则同一份文件重导会"多出一条新事实"。
        seen_cname: set[str] = set()
        for raw_cname in (_as_list(obj.get("cname")) + _as_list(obj.get("cnames")))[:_MAX_CNAMES]:
            cname = normalize.try_domain(raw_cname)
            if cname is None or cname == host or cname in seen_cname:
                continue
            seen_cname.add(cname)
            yield RawObservation(entity_ref=("domain", host), type="cname", value=cname)

        # --- 产品与版本（§4.2：service --runs--> product）---
        seen_product: set[str] = set()
        techs = _as_list(obj.get("tech")) or _as_list(obj.get("technologies"))
        for tech in techs:
            name, version = split_tech(str(tech))
            if not name:
                continue
            key = product_key(name, version)
            if key in seen_product:
                continue
            seen_product.add(key)
            yield RawEntity(kind="product", key=key, display=name,
                            subtitle=version or None,
                            attrs={"family": name, "version": version or None})
            yield RawRelation(src=("service", svc),
                              dst=("product", key), rel="runs")


def _is_nuclei_finding(obj: dict[str, Any]) -> bool:
    """这一行是不是 nuclei 的一条命中（见 `_NUCLEI_MARKERS`）。"""
    return any(marker in obj for marker in _NUCLEI_MARKERS)


def service_key(host: str, port: Any, scheme: str) -> str:
    """**`service` 的身份规则本身**（登记在 `kind_registry.identity_rule` 里）。

    `host:port`；端口缺失时退化为 `host:scheme`。它是公开函数而不是内联表达式，
    因为 `fixtures/kinds/service.json` 要能**执行**它来证明身份规则不是一句空话 ——
    "身份规则 + 样例夹具"这条准入要求只有在夹具真能跑通它时才成立。
    """
    if port is None or port == "":
        return f"{host}:{scheme}"
    return f"{host}:{port}"


def product_key(name: str, version: str = "") -> str:
    """**`product` 的身份规则本身**：`产品名[@版本]`。

    带版本与不带版本是**两个实体** ——「哪些还在用 Nginx 1.18」问的是后者，
    而把它折成"只要有 nginx 就算命中"会让那个问题问不出来。
    """
    return f"{name}@{version}" if version else name


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def split_tech(text: str) -> tuple[str, str]:
    """`nginx/1.24.0` / `Laravel 8.12.0` / `Nginx:1.24.0` / `Vue.js` → (产品名, 版本)。

    **归一化的活全部外包给 `normalize`**：产品名走别名表折叠，版本走
    `normalize.version_tuple` 校验。这一层只负责"把名字和版本切开"——
    也就是"这是什么形状"，不是"怎么写才唯一"。两者混在一起正是
    `Nginx` / `nginx` / `NGINX/1.24` 三写法同时入库的来源。
    """
    text = text.strip()
    if not text:
        return "", ""
    name, version = text, ""
    for sep in ("/", ":"):
        if sep in text:
            head, _, tail = text.partition(sep)
            if head.strip() and _is_version(tail):
                name, version = head.strip(), tail.strip()
                break
    else:
        head, _, tail = text.rpartition(" ")
        if head.strip() and _is_version(tail):
            name, version = head.strip(), tail.strip()

    try:
        name = normalize.product_name(name)
    except normalize.NormalizeError:
        return "", ""
    # 版本**原样保留**（`1.24.0` 不该被写成 `1.24.0.0.0`），只借 version_tuple
    # 校验它确实是可解析的版本号 —— `HTTP/3` 的 3 是版本，
    # `CloudFront:cloudfront` 的 cloudfront 不是。
    return name, version


def _is_version(text: str) -> bool:
    try:
        normalize.version_tuple(text.strip())
    except normalize.NormalizeError:
        return False
    return True


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(HttpxParser().parse(text.splitlines(), ctx or OfflineContext()))
