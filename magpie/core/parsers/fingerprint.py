"""`fingerprint` —— 离线指纹引擎的解析器皮（4.3，计划书 §4.2 的那一行）。

它认什么格式
------------
**已落盘的单次 HTTP 交换**（请求段 + 响应段，段间一个空行）—— 探针、代理
导出、`curl -v` 清洗版落在盘上的都是这个形状。计划书把引擎的输入写死为
"**已落盘的响应**"，而 `runs` 这条边要求先知道响应属于哪个服务：请求行 /
Host / 绝对 URL 是格式的一部分，读它们正是"解析器"这个角色的活。

    GET https://app.example.com/admin/login HTTP/1.1
    Host: app.example.com

    HTTP/1.1 200 OK
    Server: nginx/1.24.0

    <body…>

拆解、匹配、落记录都在零件模块 `_fingerprints.py` 里（文件头写了为什么：
PARSE-NORM 只对定义了 `class X(Parser)` 的模块生效，而匹配离不开 `re` 与
`.lower()`）。本模块只剩三件事：嗅探、收行、按顺序调用零件。

三条纪律（各自的理由在 `_fingerprints.py` 与本文件的注释里）
------------------------------------------------------------
* **零出站（R7）**：只读已经躺在盘上的文本，一个包都不发 —— 这是它与"探测器"
  的分界，也是计划书 §4"Magpie 不主动取数据"的落点；
* **YAML 走 extras（R6）**：`fingerprints/*.yaml` 由零件模块 lazy import 解析，
  缺 `api-yaml` 时抛 `MissingExtrasError`（人话，不是裸 `ImportError`），嗅探
  **完全不碰 yaml** —— 环境问题不得伪装成格式问题（3.4 点名过的头号陷阱）；
* **规则集 digest 进 definition_hash**：`definition_extra()` 把
  `_fingerprints.rules_digest()` 混进 `ingest._definition_hash()` —— 改任何
  一条规则，`fingerprint@1` 的 definition_hash 必变，回归夹具先红一次。这就是
  阶段交付标准「规则改动必须让夹具失败一次」的机械形态。

产出（判据 2 的形状）
--------------------
| 记录 | 内容 |
|---|---|
| `service` 实体 | key = `httpx.service_key`（与 httpx / nmap / 路径列表来源落同一个实体） |
| `product` 实体 | key = `httpx.product_key`（带版本与不带版本是两个实体） |
| 关系 | `service --runs--> product` |
| 观测 | `fingerprint` = `{product, rule, where, evidence}`，**`source` = 规则名** |

观测的 `source` 走 `RawObservation.source` → `ingest` 按它 `register_source`，
于是贡献的 `sources.name` 就是规则名 —— "哪条规则、依据什么"在数据层直接可答
（§4.3 的可解释要求），不是展示时拼出来的。
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .. import normalize
from . import _fingerprints
from .base import OfflineContext, ParseContext, Parser, Record

__all__ = ["FingerprintParser", "parse_text"]

#: 收整份交换的字符上限（与 `_json.MAX_DOC_CHARS` 同一口径：超过就报可读坏行
#: 并放弃，而不是把畸形文件吃进内存）。响应体可以是几 MB 的 HTML，32 MiB
#: 对单次交换足够宽。
MAX_DOC_CHARS = 32 * 1024 * 1024


class FingerprintParser(Parser):
    tool = "fingerprint"
    format_version = "1"
    #: 文本线格式没有"字段名"这回事，不认识的 key 无从谈起 —— 与 domainlist /
    #: curl 同一取舍（`known_fields` 空集，永不调 `on_unknown_field`）。
    known_fields: frozenset[str] = frozenset()
    #: 响应头里的 banner 是强证据，但不是签名（头可以被改写）—— 比 httpx 的
    #: 探测低半档，比猜测高得多。
    max_confidence = 0.9
    #: 不参与互斥：线格式与现有 13 个来源的判据都不相交（`test_sniffing_
    #: separates_the_formats_cleanly` 的 fingerprint 条目守着这一点）。
    exclusive_group = ""

    # ------------------------------------------------------------------
    # 规则集 digest —— "改规则必变 definition_hash"的入口
    # ------------------------------------------------------------------

    @classmethod
    def definition_extra(cls) -> str:
        """混进 `ingest._definition_hash()` 的额外材料：规则集的内容指纹。

        只对**这一个**解析器存在（`_definition_hash` 对没有该属性的类原样跳过，
        其余 13 个解析器的 hash 一个字节都不变）。没有它的话，改一条 YAML 规则
        之后同一个 `fingerprint@1` 会带着旧登记表继续"幂等跳过"—— 规则变了、
        结论没变，而且没有任何信号。
        """
        return "rules:" + _fingerprints.rules_digest()

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """首个非空行是请求行或状态行 → 0.95。

        判据与 `parse()` 共用 `_fingerprints.request_line_head` /
        `status_line_head`（`curl.sniff` 文件头写过反例：判据分两份的下场是
        "嗅探很确定、解析却一行读不出来"）。

        刻意**不看 yaml 能不能 import**（规则文件是环境问题，格式是格式问题）
        与**文件名**（对仲裁没有证明力 —— gau 的同款裁定）。
        """
        del filename
        text = sample.replace("\x00", "").lstrip("\ufeff")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            head = _fingerprints.request_line_head(stripped)
            if head is not None:
                try:
                    normalize.http_method(head[0])    # 归一化只认真方法名
                except normalize.NormalizeError:
                    return 0.0
                return 0.95
            if _fingerprints.status_line_head(stripped) is not None:
                return 0.95
            return 0.0                     # 第一个有内容的行不是请求行/状态行
        return 0.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        text = self._collect(lines, ctx)
        if text is None:
            return
        if not text.strip():
            return                          # 空文件不是坏行（与 ffuf 同口径）

        exchange, error = _fingerprints.parse_exchange(text.split("\n"))
        if error is not None:
            lineno, reason = error
            ctx.on_bad_line(lineno, reason, reason[:200])
            return
        assert exchange is not None         # error 为 None 时 parse_exchange 的合同

        # 规则在**认领之后**才加载：缺 extras / 规则坏了 → 抛给 ingest 折成
        # 可读的 parse_error（openapi 的 YAML 同一条路径）。空命中不是错误。
        rules = _fingerprints.load_rules()
        hits = _fingerprints.match_rules(exchange, rules)
        if ctx.cancelled():
            return
        yield from _fingerprints.build_records(exchange, hits)

    def _collect(self, lines: Iterable[str], ctx: ParseContext) -> str | None:
        """流式收行成整份文本。**不物化 `lines` 本身**（`test_parsers_never_
        list_the_whole_file` 对每个解析器都喂一个 `__len__` 会炸的迭代器）。"""
        parts: list[str] = []
        size = 0
        for line in lines:
            if ctx.cancelled():
                return None
            size += len(line)
            if size > MAX_DOC_CHARS:
                ctx.on_bad_line(
                    1, f"这份响应超过 {MAX_DOC_CHARS} 字符的上限 —— 单次交换"
                       f"不该有这么大，拒绝把它整份读进内存", "")
                return None
            parts.append(line)
        return "\n".join(parts)


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(FingerprintParser().parse(
        text.split("\n"), ctx or OfflineContext()))
