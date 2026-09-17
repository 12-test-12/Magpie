"""2.5 的**过渡解析器** —— 2.4 落地后本文件自动退役，可整文件删除。

为什么存在
----------
2.5 的验收标准里有一条硬性要求：**用真实 subfinder + httpx 文件跑一次最小入口，
产出汇总报告**。而 2.4（解析器框架 + domainlist/httpx）尚未落地时，这条标准
没有任何解析器可用 —— 管子接好了却没有水。

于是这里放一份**最小可用**的实现，它的唯一职责是让 `ingest` 能被端到端地跑通、
被验收、被回归。它不是 2.4 的替代品：

* 没有 kind 登记表 / 关系词表的 schema 迁移（那是 2.4 的同期必交项 A）；
* 没有黄金夹具与负向夹具（同期必交项 C）；
* 没有 `definition_hash` 强制填写（同期必交项 B —— 那一项在 `store.register_parser` 上）；
* 归一化只做域名一层，且**优先调用** `magpie.core.normalize`（2.3 的产物）。
  2.3 落地后这里自动走它的实现。

退役机制
--------
`ingest.load_parsers()` 按序发现 `magpie.core.parsers` → `magpie.core.parsers_bridge`。
只要 2.4 的包里出现**一个**解析器，本文件就不再被加载 —— 不需要谁记得来删它。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# §5.3 的记录类型（frozen dataclass，字段名与计划书一致）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawEntity:
    kind: str
    key: str
    display: str | None = None
    subtitle: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawObservation:
    entity_ref: tuple[str, str]        # (kind, key) —— 绝不是 DB id
    type: str
    value: Any
    severity: str | None = None
    state: str | None = None
    confidence: float | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class RawRelation:
    src: tuple[str, str]
    dst: tuple[str, str]
    rel: str
    extra: dict[str, Any] | None = None


class Parser(ABC):
    """§5.3 的解析器接口。ingest 按鸭子类型识别，不 import 本类。"""

    tool: str = ""
    format_version: str = "1"
    known_fields: frozenset[str] = frozenset()
    max_confidence: float = 1.0

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        return 0.0

    @abstractmethod
    def parse(self, lines: Iterable[str], ctx: Any) -> Iterator[Any]:
        """`lines` 是**流式**迭代器（一行一次），返回 Raw* 记录的迭代器。"""


# ---------------------------------------------------------------------------
# 归一化 —— 优先用 2.3 的 normalize.py，没有才退回本地最小实现
# ---------------------------------------------------------------------------

try:                                        # pragma: no cover - 取决于 2.3 是否已落地
    from .normalize import normalize_domain as _normalize_domain_23
except ImportError:                         # pragma: no cover
    _normalize_domain_23 = None             # type: ignore[assignment]

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
    r"(?:\.(?!-)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*\.?$")


def normalize_domain(raw: Any) -> str | None:
    """域名归一化：小写、去尾点、去端口、IDN → punycode。不合法返回 None。

    2.3 落地后走 `normalize.normalize_domain`；否则本地最小实现（只为让 2.5
    能被端到端验收，**不承担** 2.3 的全部规则：长度与标签规则、保留字符、
    可读错误的类型等都以 2.3 为准）。
    """
    if _normalize_domain_23 is not None:
        try:
            value = _normalize_domain_23(raw)
        except Exception:
            return None
        return None if value is None else str(value)

    # 严格：只接受**整行就是一个主机名**。绝不去文本里"提取"域名 ——
    # 一旦允许提取，`{"url": "https://a.example.com/"}` 这种 JSON 行
    # 也会被判成域名，domainlist 与 httpx 就会同时高分（实测 0.90 vs 0.95，
    # 差距 0.05 < 0.15 → 整批文件被推给人工）。嗅探的准确率靠这里。
    text = str(raw).strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if any(ch in text for ch in '/\\?#@"\'{}[],<>=&%'):
        return None
    if text.startswith("["):                       # IPv6 字面量：不是域名
        return None
    if ":" in text:
        text = text.rsplit(":", 1)[0]
    text = text.rstrip(".").lower()
    if not text or len(text) > 253 or ".." in text:
        return None
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        try:
            text = text.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
    if not _DOMAIN_RE.match(text):
        return None
    if "." not in text:                            # 单标签（localhost 之类）不入库
        return None
    return text


def _looks_like_ip(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ---------------------------------------------------------------------------
# domainlist —— 每行一个域名（subfinder / assetfinder / findomain / crt.sh）
# ---------------------------------------------------------------------------


class DomainListParser(Parser):
    """域名清单：一行一个域名 → `domain` 实体 + `member_of` 关系（由后缀推导）。"""

    tool = "domainlist"
    format_version = "1"
    known_fields: frozenset[str] = frozenset()
    max_confidence = 0.9

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        # 探针说过"NUL 会在解析前剥离"，嗅探必须按同一口径看样本，
        # 否则一个 NUL 就能让整份清单的得分掉到"分不开"。
        lines = [ln.strip() for ln in sample.replace("\x00", "").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if not lines:
            return 0.0
        sample_lines = lines[:200]
        good = sum(1 for ln in sample_lines if _line_is_domain(ln))
        ratio = good / len(sample_lines)
        if ratio >= 0.8:
            return 0.9
        if ratio >= 0.5:
            return 0.6
        return 0.0

    def parse(self, lines: Iterable[str], ctx: Any) -> Iterator[Any]:
        seen: set[str] = set()
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            domain = normalize_domain(text)
            if domain is None:
                ctx.on_bad_line(ctx.current_line, "不是合法域名", text)
                continue
            if domain in seen:
                continue
            seen.add(domain)
            yield RawEntity(kind="domain", key=domain, display=domain)
            parent = _parent_domain(domain)
            if parent is not None:
                yield RawEntity(kind="domain", key=parent, display=parent)
                yield RawRelation(src=("domain", domain), dst=("domain", parent),
                                  rel="member_of")


def _line_is_domain(line: str) -> bool:
    return normalize_domain(line) is not None


def _parent_domain(domain: str) -> str | None:
    """推导父域（`a.b.example.com` → `b.example.com`）。顶级两段不再上推。"""
    parts = domain.split(".")
    if len(parts) <= 2:
        return None
    return ".".join(parts[1:])


# ---------------------------------------------------------------------------
# httpx —— ProjectDiscovery httpx `-json` 的 JSONL 输出
# ---------------------------------------------------------------------------

#: 本解析器**认识**的字段。出现别的 key 就报未知字段（§5.6 的格式漂移信号）。
_HTTPX_KNOWN = frozenset({
    "timestamp", "port", "url", "input", "title", "scheme", "webserver",
    "content_type", "method", "host", "path", "status_code", "content_length",
    "failed", "technologies", "tech", "cname", "cnames", "tls", "a", "aaaa",
    "cdn_name", "cdn_type", "favicon", "hash", "header", "body", "jarm_hash",
    "chain_status_codes", "response_time", "final_url", "words", "lines",
    "location", "error", "ip", "resolvers", "sni", "tls_grab", "raw_header",
    "request", "response", "duration", "knowledgebase",
})

#: 挂在 **domain** 上的字段；其余（端口/状态码/中间件/内容类型/最终 URL）挂 service。
_DOMAIN_OBS_FIELDS = ("title", "cname")


class HttpxParser(Parser):
    """httpx JSONL → domain / ip / service / product + 观测 + 关系。

    关系只用计划书里已登记的两个动词：`resolves_to`（domain → ip）与
    `runs`（service → product，§4.2 的核心目标）。
    """

    tool = "httpx"
    format_version = "1"
    known_fields = _HTTPX_KNOWN
    max_confidence = 0.95

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        lines = [ln.strip() for ln in sample.replace("\x00", "").splitlines()]
        lines = [ln for ln in lines if ln][:50]
        if not lines:
            return 0.0
        hits = 0
        for ln in lines:
            try:
                obj = json.loads(ln)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and ("host" in obj or "input" in obj
                                          or "status_code" in obj or "scheme" in obj):
                hits += 1
        if hits == 0:
            return 0.0
        # 阈值刻意放在 0.5：真实 JSONL 里断行/截断是常态（我们自己的样例就有 1 条），
        # 拿"全部行都合法"当采用条件会让一条坏行把整份文件推给人工。
        return 0.95 if hits / len(lines) >= 0.5 else 0.6

    def parse(self, lines: Iterable[str], ctx: Any) -> Iterator[Any]:
        seen_host: set[str] = set()
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except ValueError as exc:
                ctx.on_bad_line(ctx.current_line, f"JSON 解析失败：{exc}", text)
                continue
            if not isinstance(obj, dict):
                ctx.on_bad_line(ctx.current_line, "不是 JSON 对象", text)
                continue
            for key in obj:
                if key not in self.known_fields:
                    ctx.on_unknown_field(str(key), ctx.current_line)
            yield from self._records(obj, ctx, seen_host)

    # ---- 单个 JSON 对象 → 记录 ----

    def _records(self, obj: dict[str, Any], ctx: Any,
                 seen_host: set[str]) -> Iterator[Any]:
        host = normalize_domain(obj.get("host") or obj.get("input") or "")
        if host is None:
            ctx.on_bad_line(ctx.current_line,
                            f"host 字段不是合法域名：{obj.get('host')!r}", "")
            return
        if host not in seen_host:
            seen_host.add(host)
            yield RawEntity(kind="domain", key=host, display=host)

        port = obj.get("port")
        scheme = str(obj.get("scheme") or "http")
        service_key = f"{host}:{port}" if port is not None else f"{host}:{scheme}"

        # --- IP：a / aaaa 记录 → ip 实体 + resolves_to ---
        for addr in _as_list(obj.get("a"))[:16]:
            ip = str(addr).strip()
            if not _looks_like_ip(ip):
                continue
            yield RawEntity(kind="ip", key=ip, display=ip)
            yield RawRelation(src=("domain", host), dst=("ip", ip), rel="resolves_to")

        # --- service ---
        yield RawEntity(kind="service", key=service_key, display=service_key,
                        subtitle=f"{scheme}://{host}:{port}" if port is not None else None)
        for name, value in (("port", port), ("scheme", scheme),
                            ("status_code", obj.get("status_code")),
                            ("content_length", obj.get("content_length")),
                            ("webserver", obj.get("webserver")),
                            ("content_type", obj.get("content_type")),
                            ("final_url", obj.get("final_url"))):
            if value is None or value == "":
                continue
            yield RawObservation(entity_ref=("service", service_key), type=name,
                                 value=value)
        # `failed` 只在**真失败**时记：给每个成功的主机写一条 "failed=false" 是
        # 零信息的噪声 —— 它会实打实地抬高观测数与存储（10 万行导入里约 1 万条）。
        if obj.get("failed"):
            yield RawObservation(entity_ref=("service", service_key), type="failed",
                                 value="true")

        # --- host 级观测（§4.1 把 title / cname 归在域名上）---
        # 分工刻意不重叠：服务级的东西（端口/状态码/中间件/内容类型/最终 URL）
        # 挂在 service 上，主机级的东西（标题/CNAME）挂在 domain 上。同一条值
        # 在两处各记一次会让"观测数"这个指标虚高，也让镜头页出现重复行。
        for name in _DOMAIN_OBS_FIELDS:
            value = obj.get(name)
            if value in (None, "", []):
                continue
            yield RawObservation(entity_ref=("domain", host), type=name, value=value)

        tls = obj.get("tls")
        if isinstance(tls, dict) and tls.get("version"):
            yield RawObservation(entity_ref=("service", service_key), type="tls_version",
                                 value=str(tls.get("version")))

        # --- 产品与版本（§4.2：service --runs--> product）---
        for tech in _as_list(obj.get("tech")) or _as_list(obj.get("technologies")):
            name, version = _split_tech(str(tech))
            if not name:
                continue
            product_key = f"{name}@{version}" if version else name
            yield RawEntity(kind="product", key=product_key, display=name,
                            subtitle=version, attrs={"family": name,
                                                     "version": version or None})
            yield RawRelation(src=("service", service_key),
                              dst=("product", product_key), rel="runs")


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _split_tech(text: str) -> tuple[str, str]:
    """`nginx/1.24.0` / `Laravel 8` / `jQuery` → (产品名, 版本)。"""
    text = text.strip()
    if not text:
        return "", ""
    for sep in ("/", ":"):
        if sep in text:
            name, _, version = text.partition(sep)
            if re.fullmatch(r"[0-9][0-9A-Za-z.\-+]*", version.strip()):
                return name.strip().lower(), version.strip()
    match = re.match(r"^(.*?)\s+v?([0-9][0-9A-Za-z.\-+]*)$", text)
    if match:
        return match.group(1).strip().lower(), match.group(2)
    return text.lower(), ""


__all__ = [
    "DomainListParser", "HttpxParser", "Parser", "RawEntity", "RawObservation",
    "RawRelation", "normalize_domain",
]
