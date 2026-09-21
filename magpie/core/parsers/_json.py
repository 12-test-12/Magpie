"""整份-JSON 格式共用的两个小零件（3.4）。

为什么需要它们
--------------
3.4 的三个来源（openapi / postman / har）**整份文件是一段 JSON**，第四类
（curl）不是。与 ffuf 那次的处境不同，这里的问题不是"怎么读进来"，而是
**"怎么在读进来之前就认出它是谁"**：

    仲裁的口径是"最高分 ≥ 0.8 且与第二名差距 ≥ 0.15"（§5.4），而
    HAR 与 Postman **都是 JSON**。判据只能落在**顶层键**上 ——
    `log` / `info`+`item` / `openapi`+`swagger`。

为什么不能用「试着 `json.loads`，哪个不报错就算哪个」
----------------------------------------------------
两个理由，第二个才是致命的：

1. **嗅探器只看样本**（`ingest.SAMPLE_BYTES` = 64 KiB），而一份真实的 HAR /
   OpenAPI 文档轻松超过它 —— 样本**必然被截断**，`json.loads` 必然失败。
   于是所有整份-JSON 的嗅探器在真实文件上都返回 0，整批文件掉进人工裁决。
   （ffuf 的 `sniff` 就是这个形状：它的注释承认了这条，只是 ffuf 的报告通常
   够小才没被咬到。）
2. 坏文件上它会给出**随机的归属**：一份语法坏掉的 Har，`json.loads` 失败，
   于是"谁都不要它"—— 或者更糟，某个判据更宽的解析器要了它。而 3.4 的验收
   标准要的是**靠键**区分：读不出顶层键就明说"读不出"，不要猜。

所以这里给一个**截断容忍**的顶层键扫描器：它不解析整份文档，只沿着字符走，
在 depth == 1 上收"后面紧跟 `:` 的字符串"。样本断在哪里，它就收到哪里。

它**不是** JSON 解析器，也刻意不是：真正的解析由各解析器的 `parse()` 负责，
那里拿到的是整份文件、有明确的大小上限与可读的坏行报告。

`_` 开头的模块，发现机制不会把它当成解析器（同 `_urls.py` / `_apis.py`），
但 PARSE-RO（不得 import 写入口）照常管它。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from ._urls import MAX_VALUE_CHARS
from .base import ParseContext

__all__ = ["MAX_DOC_CHARS", "collect_json", "find_key", "load_json",
           "report_unknown", "top_level_keys", "walk"]

#: 整份文档的字符数上限（32 MiB，与 ffuf 的 `MAX_REPORT_CHARS` 同一口径）。
#: 一份接口描述文档不该有这么大；超过就报一条可读的坏行并放弃，而不是把畸形
#: 文件吃进内存。
MAX_DOC_CHARS = 32 * 1024 * 1024

#: 顶层键最多收多少个（防的是一份几千个键的畸形文档把嗅探变成一次长扫描）。
MAX_KEYS = 64


def collect_json(lines: Iterable[str], ctx: ParseContext,
                 what: str) -> str | None:
    """把流式行收成整份文本（**唯一一种这样做的解析器形状**，同 ffuf）。

    返回 `None` 表示"已经报过一条坏行，别再往下走"。**不物化 `lines` 本身** —
    它按行追加进自己的列表（`list(lines)` 是 `Parser.parse` 的硬线，
    `test_parsers_never_list_the_whole_file` 会给每个解析器喂一个 `__len__`
    会炸的迭代器）。
    """
    parts: list[str] = []
    size = 0
    for line in lines:
        if ctx.cancelled():
            return None
        size += len(line)
        if size > MAX_DOC_CHARS:
            ctx.on_bad_line(
                1, f"{what} 超过 {MAX_DOC_CHARS} 字符的上限 —— 一份接口描述"
                   f"文档不该有这么大，拒绝把它整份读进内存", "")
            return None
        parts.append(line)
    return "\n".join(parts)


def load_json(text: str, ctx: ParseContext, what: str) -> Any | None:
    """`text` → JSON 文档；坏 JSON 时记一条**可读**的坏行并返回 `None`。

    错误信息里带 `what`：一份坏掉的 HAR 与一份坏掉的 OpenAPI 都报
    "JSON 解析失败"是不够的 —— 报告里要说清是哪种文件的哪一处。
    """
    try:
        return json.loads(text.replace("\x00", ""))
    except ValueError as exc:
        ctx.on_bad_line(1, f"JSON 解析失败（{what} 是一整个 JSON 文档）：{exc}",
                        text[:MAX_VALUE_CHARS])
        return None


def _read_string(sample: str, start: int) -> tuple[str, int] | None:
    """从 `sample[start]`（必须是 `"`）读一个 JSON 字符串，返回 (内容, 下一个位置)。

    被截断时返回 `None`。**不做转义解码** —— 这里比的是 `openapi` / `log` /
    `info` 这类键名，它们在任何真实文档里都不含转义；为一个不存在的形状承担
    一份转义表的维护成本不划算（`json.loads` 那边的解析是完整的）。
    """
    out: list[str] = []
    index = start + 1
    length = len(sample)
    while index < length:
        ch = sample[index]
        if ch == "\\":
            if index + 1 >= length:
                return None                  # 转义被截断
            out.append(sample[index + 1])    # 原样收，不解码
            index += 2
            continue
        if ch == '"':
            return "".join(out), index + 1
        out.append(ch)
        index += 1
    return None                              # 字符串没闭合 → 样本断在这里


def top_level_keys(sample: str) -> list[str]:
    """**截断容忍**的顶层键扫描器：样本 → 顶层对象的键（按出现顺序）。

    判据是"depth == 1 上、后面紧跟 `:` 的那个字符串" —— 它是**结构**判据，
    不是关键词匹配：`{"a": "log"}` 里的 `log` 是**值**，不会被当成键。

    读不出（不是以 `{` 开头 / 样本在第一个键之前就断了）返回 `[]` —— 空列表
    就是"这不是一份能认出来的 JSON 对象"，调用方据此给 0 分，而不是给一个
    似是而非的中间分。
    """
    index = 0
    length = len(sample)
    while index < length and (sample[index].isspace() or sample[index] == "﻿"):
        index += 1
    if index >= length or sample[index] != "{":
        return []
    index += 1

    keys: list[str] = []
    depth = 1
    while index < length:
        ch = sample[index]
        if ch == '"':
            read = _read_string(sample, index)
            if read is None:
                return keys                  # 样本断在这条字符串里
            text, after = read
            probe = after
            while probe < length and sample[probe].isspace():
                probe += 1
            if depth == 1 and probe < length and sample[probe] == ":":
                keys.append(text)
                if len(keys) >= MAX_KEYS:
                    return keys
            index = after
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth <= 0:
                return keys
        index += 1
    return keys


def find_key(keys: list[str], *names: str) -> bool:
    """`names` 里有任意一个出现在 `keys` 里（顶层键的集合判据）。"""
    for name in names:
        if name in keys:
            return True
    return False


def as_list(value: Any) -> list:
    """`None` / 单值 / 列表 → 列表。三个来源里到处是"一个或一串"的字段。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def report_unknown(node: Any, known: frozenset[str], ctx: ParseContext,
                   prefix: str = "") -> None:
    """未知字段 → `ctx.on_unknown_field`（§5.6 的格式漂移信号）。

    三条口径，与 3.3 / 3.2 一致：

    * **按名字报一次**，不是按条目 —— 一份几千条 entry 的 HAR 逐条报同一个新
      字段，会把那句"检测到新字段 x（N 行）"里的 N 变成条目数，噪声盖过信号
      （ffuf 对 `results[]` 就是这么做的）；
    * `_` 开头的键**不报**：HAR 与 Postman 都留了 `_resourceType` / `_postman_id`
      这类工具私有键，它们是规范允许的自留地，不是漂移；
    * 只对**对象**生效（数组没什么"字段"可言）。
    """
    if not isinstance(node, dict):
        return
    for key in node:
        name = str(key)
        if name.startswith("_") or name in known:
            continue
        ctx.on_unknown_field(prefix + name)


def walk(iterable: Iterable, what: str, ctx: ParseContext) -> Iterator[Any]:
    """逐个产出，**非字典项只报一次坏行**（一份文档里几千条时不该刷屏）。"""
    reported = False
    for item in iterable:
        if isinstance(item, dict):
            yield item
            continue
        if not reported:
            reported = True
            ctx.on_bad_line(1, f"{what} 里有一项不是对象（收到 "
                               f"{type(item).__name__}），这一项被跳过", "")
