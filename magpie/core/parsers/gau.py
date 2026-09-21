"""`gau` —— getallurls 的 URL 清单（计划书 §4.3 的 urls 行之一）。

gau 从 Wayback / Common Crawl / OTX / URLScan 的**历史存档**里捞 URL，输出是
一行一个绝对 URL。它给的是"这台主机**曾经**被访问过哪些路径"—— 与 dirsearch /
ffuf 那种"刚刚主动探测过"的证据是**两种证据**，所以它值得是一个独立来源
（计划书的"多镜头"要的是能分清这两件事，而不是把它们糊成一份清单）。

产出
----

| 记录 | 内容 |
|---|---|
| `endpoint` 实体 | key = `主机[:非默认端口] + 路径模板`（身份规则在 `normalize.endpoint_key`） |
| `domain` / `ip` 实体 | URL 里的主机 |
| `service` 实体 | key = `host:port`（**复用 `httpx.service_key`**，与 httpx / nmap / nuclei 落同一个服务） |
| 关系 | `endpoint --served_by--> service` |
| 观测 | `path`（**原样**，含查询串与片段）/ `path_template` / `scheme` / `port` |

三处刻意不做的事
----------------

* **不写 `method` 观测**。gau 的输出里没有方法，而"这一条来自一个不产出方法的
  工具"用**缺观测**就能表达 —— 补一条 `method=UNKNOWN` 是零信息噪声（与 httpx
  对 `failed=False` 的处理同一条口径，`_urls.py` 的文件头把这条裁定写透了）。
  接口页面渲染时把"没有 method 观测"显示成 `UNKNOWN` 即可，那是 GUI 的默认值。
* **不把查询串并进身份**。`/a?id=1` 与 `/a?id=2` 是同一个接口的两次调用；把查询串
  当 key 的一部分会让接口列表每抓一次就膨胀一轮。但**原始串留在 `path` 观测里**
  —— 折叠的是身份，不是事实（`normalize.endpoint_identity` 的裁定一）。
* **不解析 `#fragment`**。它只对浏览器有意义，服务器收不到它；它照旧留在
  `path` 观测的原文里。
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .. import normalize
from ._urls import MAX_VALUE_CHARS, EndpointSink, sample_lines
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["GauParser", "parse_text"]


class GauParser(Parser):
    tool = "gau"
    format_version = "1"
    #: 行格式没有字段可言 —— 一行就是全部。所以未知字段探测在这里天然为空，
    #: 格式漂移的信号是"合法 URL 的比例掉下来"（见 `sniff`）。
    known_fields: frozenset[str] = frozenset()
    #: 比 httpx 低一档：gau 说的是"历史存档里出现过"，不是"我刚刚探到了它"。
    #: 一条存档 URL 完全可能是三年前的一次 404。
    max_confidence = 0.85
    #: 与另外三个"路径列表"来源互斥：同一份文件只该被其中一个认领。四个的
    #: sniff 判据是刻意**互不相交**的（文本 vs JSONL vs 整份 JSON），
    #: `test_sniffing_separates_the_formats_cleanly` 守着这一点。
    exclusive_group = "url-list"

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是一条绝对 URL"打分。

        判据是**整行**像不像 URL，而不是"行里有没有 `://`"：后者会把 dirsearch
        的结果行（`[12:00:01] 200 -  123B  - /admin` 这一类，正文里可能带完整
        URL）也算成自己的。判据与 `parse()` 用的是**同一个函数**
        （`normalize.is_absolute_url`），否则会出现"我很确定这是 gau 的输出，
        然后一行都读不出来"—— domainlist 的 `accepts` 那段注释写的就是这件事。

        刻意**不**看文件名：`gau.txt` 对仲裁没有证明力。
        """
        del filename
        lines = sample_lines(sample)
        if not lines:
            return 0.0
        good = sum(1 for ln in lines if normalize.is_absolute_url(ln))
        ratio = good / len(lines)
        # 阈值与 §5.4 的仲裁口径对齐：≥0.8 才有资格被采用，0.5–0.8 只表示
        # "我看着像"（够不上采用线，但能在仲裁报告里说清楚）。
        if ratio >= 0.8:
            return 0.9
        if ratio >= 0.5:
            return 0.6
        return 0.0

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐行产出（**不物化列表**：一份 gau 导出动辄几十万行）。"""
        sink = EndpointSink()
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if not normalize.is_absolute_url(text):
                ctx.on_bad_line(lineno, "不是一条绝对 URL（gau 每行一个 URL）",
                                text[:MAX_VALUE_CHARS])
                continue
            yield from sink.emit(text, ctx, lineno)


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(GauParser().parse(text.splitlines(), ctx or OfflineContext()))
