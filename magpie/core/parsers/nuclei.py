"""`nuclei` —— ProjectDiscovery nuclei 的 `-jsonl` 输出（计划书 §4.5 的 vuln 行）。

nuclei 是这份工具里**第一个产风险项的来源**：httpx / nmap 说的是"手上有
什么"，nuclei 说的是"其中的哪一些有问题"。所以这一条链路的目的不是"多一个
来源"，而是让「哪些资产上有高危漏洞」变成一个**图上的查询**，而不是对着一列
文本做 grep。

产出
----

| 记录 | 内容 |
|---|---|
| `vuln` 实体 | key = `template-id@规范化目标`（身份规则在 `normalize.vuln_key`） |
| `domain` / `ip` 实体 | 命中的主机（`matched-at` 里的主机名，IP 走 `ip`、域名走 `domain`） |
| `service` 实体 | key = `host:port`（**复用 `httpx.service_key`**，与 httpx / nmap 落同一个服务） |
| 关系 | `vuln --found_on--> service`（或 `domain` / `ip`） |
| 观测 | `severity` / `state` / `template_id` / `matched_at` / `vuln_type` / `template_name` / `matcher_name` / `extracted_results` / `tags` / `reference` / `ip` |

⚠️ 本步最容易做错的一处：给"重复命中"加计数
------------------------------------------

「同模板重复命中不重复计数」**不是**一个新机制，它是身份规则的**推论**：

* 身份 = `(template-id, 规范化目标)` —— 不是 `template-id` 一个人。同一个模板
  命中两个不同的资产是**两件事**，它们该是两个实体；
* 同一份文件里同一条命中出现两次 → 两次算出同一个 `(kind, key)`，第二条的
  facts 完全重复 → 管线把它记成 `skipped`，**一条贡献都不写**；
* 同一个文件再导一遍 → 每一条 fact 都已存在 → `added=0`，逐行指纹不变。

所以这里**没有** `hit_count`、没有 `count += 1`、没有单独一张计数表。那三种写法
都会绕开 contributions 模型：`undo` 之后计数不会跟着回退，留下一行谁也不认识
的幻影数据，而且**现有测试只断言它读得对，不会替你发现**。

`severity` / `state` 为什么是**观测**而不是新列
----------------------------------------------

计划书 §4.5 要求 `vuln` 带 `severity` + `state`。数据库里没有这两列，本步的迁移
（`schema/005_kind_vuln.sql`）按红线 R4 只能 INSERT、不能改表结构 —— 于是它们
落成 `vuln` 实体上的**两条观测**（`semantic_field` 就是 `severity` / `state`）。

这不是退而求其次：severity 与 state 是**事实**（谁说的、哪次作业说的），而事实
的家就是 contributions / observations。镜头页按它们筛选时走的是同一个事实层，
`undo` 之后它们和别的观测一起精确归零。

`state` 在 nuclei 的文件里**没有**对应字段 —— 那份文件只说得出一件事："这个
模板在这次扫描里命中了"。所以导入的每一条都落 `open`（还没人处置过它）。这既
不是占位符也不是噪声：它是漏洞镜头页按 state 筛选的**唯一来源**，而且它是一条
真事实。3.8 的批量改状态开始动它之后，这条筛选才真的有分层。

两处刻意不做的事
----------------

* **`request` / `response` / `curl-command` 一律不入库**。它们是整段 HTTP 往返，
  带着 Cookie、Authorization 与一次性 token；`curl-command` 天生含凭据。它们
  留在 `known_fields` 里（认识 = 不报格式漂移）但被忽略 —— 与 httpx 对
  `body` / `header` 的处理是同一条口径（计划书 §4.5 的硬规则：明文凭据永不落库）。
* **`matched_at` 观测存的是规范化之后的目标，不是原文**。原文里可能带
  `?token=…`，存原文会让同一次扫描的同一件事在重导时变成一条**新观测**
  （`added` 不再是 0）—— 而"重导不留痕"正是本步的核心交付标准。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from .base import OfflineContext, Parser, ParseContext, RawEntity, RawObservation, \
    RawRelation, Record
from .httpx import service_key

__all__ = ["DEFAULT_STATE", "MAX_VALUE_CHARS", "NucleiParser", "SEVERITIES",
           "parse_text"]

#: 本解析器**认识**的顶层字段。出现别的 key 就报未知字段（§5.6 的格式漂移信号）。
#:
#: 名单收得比实际用到的宽：`request` / `response` / `curl-command` 之类只登记、
#: 不落库（见文件头）。宽名单在这里是**对的** —— 它的作用是"认识 = 不报漂移"，
#: 而不是"用到 = 才登记"。
KNOWN_FIELDS: frozenset[str] = frozenset({
    "template-id", "template-path", "template-url", "template-encoded",
    "info", "type", "host", "port", "scheme", "url", "matched-at", "matched",
    "extracted-results", "request", "response", "ip", "timestamp",
    "matcher-name", "matcher-status", "curl-command", "interactsh-protocol",
    "metadata", "path", "file", "duration", "error", "lines", "words",
    "rendered-request", "rendered-response", "fingerprint", "operator",
    "raw", "engine-id", "csp", "websocket", "tls", "domain", "fqdn", "ptr",
    "knowledgebase", "failed", "proxy", "sni", "user-agent", "template-verifier",
})

#: nuclei 的 `info.severity` 取值（模板规范里的那六个）。出现第七个就是漂移信号。
SEVERITIES: frozenset[str] = frozenset(
    {"info", "low", "medium", "high", "critical", "unknown"})

#: 导入的漏洞默认落在哪个 triage 状态（见文件头的"为什么是观测"）。
DEFAULT_STATE = "open"

#: 观测值的上限（与 `ParseContext.on_bad_line` 的 `raw[:400]` 同一口径）。
#: `extracted-results` / `reference` 这类字段动辄几百字节，截断而不是丢弃：
#: 截断之后它仍然是"这个规则命中了什么"的证据。
MAX_VALUE_CHARS = 400
_MAX_VALUE_CHARS = MAX_VALUE_CHARS

#: 单行的字节上限。超过它的行**不做 JSON 解析**：nuclei 的行里唯一可能长成这样
#: 的是 `request` / `response` 正文（几十 KB 到几 MB），而那种行本来就不该入库。
#: 先拦下来，坏行报告给的才是"这行太大"这条**准确**的原因，而不是 JSON 的
#: "Expecting ',' delimiter" —— 后者会让人以为是格式坏了。
MAX_LINE_CHARS = 1024 * 1024


class NucleiParser(Parser):
    tool = "nuclei"
    format_version = "1"
    known_fields = KNOWN_FIELDS
    #: 比 httpx 低一档：nuclei 的命中是**规则匹配**的结果，模板自己可能误报；
    #: 计划书 §8.2 把"权威源"定义成 CPE / 离线指纹 / 人工确认，规则命中不在其中。
    #: 但它高于"猜"，所以镜头页把 severity 与证据一起摆出来由人裁决。
    max_confidence = 0.9
    #: 与 httpx **不互斥**：同一台机器上 httpx 探服务、nuclei 找漏洞，一份目录里
    #: 两个都有是常态（互斥组会把它们推向人工裁决）。
    exclusive_group = ""

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是带 `template-id` 的 JSON 对象"打分。

        ⚠️ 判据必须是 **nuclei 自己的签名字段**，不能是"一行是不是 JSON 对象"、
        更不能是"有没有 `host`"：nuclei 的行**带 `host`**，按那两条打分会让
        `httpx` 解析器把整份 nuclei 文件认领走（反过来 httpx 的行没有
        `template-id`，所以这条判据不会误伤它）。两边分不开的代价是仲裁拉不开
        差距 → 整批文件进人工裁决（§5.4），也就是"新来源永远宽不起来"。

        阈值与 httpx 一样放在 **0.5**：真实 JSONL 里断行/截断是常态，拿"全部行
        都合法"当采用条件，会让一条坏行把整份文件推给人工裁决。
        """
        del filename          # 文件名没有证明力，理由同 domainlist.sniff
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
            if isinstance(obj, dict) and "template-id" in obj:
                hits += 1
        if hits == 0:
            return 0.0
        return 0.95 if hits / len(lines) >= 0.5 else 0.6

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐行产出，**不物化列表** —— 10 万行的一份 `-jsonl` 不该把内存吃光。"""
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text:
                continue
            if len(text) > MAX_LINE_CHARS:
                ctx.on_bad_line(
                    lineno,
                    f"单行超过 {MAX_LINE_CHARS} 字节（{len(text)}）—— nuclei 里"
                    f"只有 request / response 正文会长成这样，而它们不入库",
                    text[:_MAX_VALUE_CHARS])
                continue
            try:
                obj = json.loads(text)
            except ValueError as exc:
                ctx.on_bad_line(lineno, f"JSON 解析失败：{exc}", text[:_MAX_VALUE_CHARS])
                continue
            if not isinstance(obj, dict):
                ctx.on_bad_line(
                    lineno, "不是 JSON 对象（nuclei 的 -jsonl 每行必须是一个对象）",
                    text[:_MAX_VALUE_CHARS])
                continue
            for key in obj:
                if key not in self.known_fields:
                    ctx.on_unknown_field(str(key), lineno)
            yield from self._records(obj, ctx, lineno, text)

    # ---- 单个 JSON 对象 → 记录 ----

    def _records(self, obj: dict[str, Any], ctx: ParseContext, lineno: int,
                 text: str) -> Iterator[Record]:
        template_id = obj.get("template-id")
        if not isinstance(template_id, str) or not template_id.strip():
            ctx.on_bad_line(
                lineno,
                f"缺 template-id（或它不是字符串）：{template_id!r} —— 模板是漏洞"
                f"身份的一半，没有它这条记录无处可挂", text[:_MAX_VALUE_CHARS])
            return

        matched_at = obj.get("matched-at")
        if not isinstance(matched_at, str) or not matched_at.strip():
            # 刻意**不**退化到 `host`：那是替 nuclei 决定"它命中的是这台主机的
            # 根"，而文件里没这么说。半条身份比一条坏行危险得多。
            ctx.on_bad_line(
                lineno,
                f"缺 matched-at（或它不是字符串）：{matched_at!r} —— 命中目标是"
                f"漏洞身份的另一半，缺了它这条记录无法落库", text[:_MAX_VALUE_CHARS])
            return

        try:
            # 身份与目标**一次算出来**：解析器两样都要（key 挂实体、parts 挂资产），
            # 分两次调会把同一个目标规范化两遍 —— 10 万行就是 20 万次域名归一化。
            identity = normalize.vuln_identity(template_id, matched_at)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"matched-at 不是可识别的目标：{exc}",
                            text[:_MAX_VALUE_CHARS])
            return
        key = str(identity["key"])
        parts = identity["parts"]

        info = obj.get("info")
        if not isinstance(info, dict):
            info = {}
        severity = _severity(info.get("severity"), ctx, lineno)
        name = _text(info.get("name")) or template_id

        yield RawEntity(kind="vuln", key=key, display=name,
                        subtitle=f"{severity} · {parts['target']}",
                        attrs={"template_id": template_id, "severity": severity,
                               "state": DEFAULT_STATE, "target": parts["target"]})

        for field, value in (
            ("severity", severity),
            ("state", DEFAULT_STATE),
            ("template_id", template_id),
            # 规范化之后的目标（不是原文）：原文里的 `?token=…` 会让重导变成
            # 一条新观测，而"重导不留痕"是本步的核心交付标准。
            ("matched_at", parts["target"]),
            ("vuln_type", _text(obj.get("type"))),
            ("template_name", name),
            ("matcher_name", _text(obj.get("matcher-name"))),
            ("ip", _ip_text(obj.get("ip"))),
            ("extracted_results", _joined(obj.get("extracted-results"))),
            ("tags", _joined(info.get("tags"))),
            ("reference", _joined(info.get("reference"))),
        ):
            if value:
                yield RawObservation(entity_ref=("vuln", key), type=field, value=value)

        host, host_kind = parts["host"], parts["host_kind"]
        yield RawEntity(kind=host_kind, key=host, display=host)

        # 目标里有 scheme 或端口 → 这是一个**服务**；两者都没有（dns / 裸主机名的
        # 模板）→ 只挂到主机上。给裸主机名编一个 `:80` 是替 nuclei 说它没说的话。
        if parts["scheme"] or parts["port"] is not None:
            svc = service_key(host, parts["port"], parts["scheme"])
            yield RawEntity(kind="service", key=svc, display=svc,
                            subtitle=parts["target"])
            yield RawRelation(src=("vuln", key), dst=("service", svc),
                              rel="found_on")
        else:
            yield RawRelation(src=("vuln", key), dst=(host_kind, host),
                              rel="found_on")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _severity(raw: Any, ctx: ParseContext, lineno: int) -> str:
    """`info.severity` → 词表里的那一个。缺失即 `unknown`。

    不在词表里的值**如实保留**并报一条未知字段（与 nmap 处理 `status.state`
    同一口径）：把它折成 `unknown` 会把一个真实的格式漂移抹平成正常值，而
    保留它只是让镜头页多出一个筛选项 —— 后者是可以看见并修掉的。
    """
    text = _text(raw)
    if not text:
        return "unknown"
    if text not in SEVERITIES:
        ctx.on_unknown_field(f"info.severity={text}", lineno)
    return text


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return text[:_MAX_VALUE_CHARS]


def _ip_text(value: Any) -> str:
    """`ip` 字段只在它真的是 IP 时才算证据（nuclei 偶尔给 `::1` 之类的形态）。"""
    text = _text(value)
    if not text:
        return ""
    return text if normalize.try_ip(text) is not None else ""


def _joined(value: Any) -> str:
    """`["a", "b"]` / `"a"` → `"a, b"`（去重、保序、截断）。

    列表字段一律折成一个字符串观测：观测的身份是 (实体, 字段, 值)，拆成多条
    会让"这个规则命中了什么"变成一个需要 join 才能读的问题，而它们的**顺序**
    本来也没有含义。
    """
    if value is None:
        return ""
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return ", ".join(out)[:_MAX_VALUE_CHARS]


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(NucleiParser().parse(text.splitlines(), ctx or OfflineContext()))
