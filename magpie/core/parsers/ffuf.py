"""`ffuf` —— ffuf 的 `-of json` 报告（计划书 §4.3 的 urls 行之一）。

ffuf 是"带 FUZZ 关键字的爆破器"：它把 URL 里的 `FUZZ` 换成字典里的每一个词，
把**有响应的**那些连状态码、长度、耗时一起写进一份 JSON 报告。所以它比
dirsearch 多两样东西：**这次的 fuzz 载荷是什么**（`input.FUZZ`）与**响应有多快**
（`duration`）—— "哪个接口慢"是一个真实的问题。

⚠️  唯一一个**整份文件是一段 JSON** 的解析器
--------------------------------------------
另外三个来源都是"一行一条"（两种文本、一种 JSONL），所以 `parse()` 能逐行产出。
ffuf 的报告是一整个 JSON 对象（而且是**缩进过的多行**），标准库没有流式 JSON
解析器 —— 所以这个解析器必须先把整个文件读进内存再 `json.loads`。

这是**受控的例外**，不是把纪律放开了：

* 它**没有** `list(lines)`、**没有**调 `len(lines)`（那是 `Parser.parse` 的硬线，
  `test_parsers_never_list_the_whole_file` 会给每个解析器喂一个 `__len__` 会炸的
  迭代器）；它只是把行追加进一个自己的列表；
* 它有**显式上限** `MAX_REPORT_CHARS`（32 MiB）。超过就报一条可读的坏行并放弃，
  而不是把一个畸形文件吃进内存 —— 这正是那份上限存在的理由；
* 一份 ffuf 报告是"一次爆破的结果列表"，量级是几 MB，与 gau 那种几十万行的
  存档导出不是一回事。

产出
----

| 记录 | 内容 |
|---|---|
| `endpoint` 实体 | key = `主机[:非默认端口] + 路径模板` |
| `domain` / `ip` / `service` 实体 + `served_by` | 同其余三个来源（`_urls.py`） |
| 观测 | `path`（原样）/ `path_template` / `scheme` / `port` / `method` / `status` / `length` / `duration` / `fuzz_input` |

`fuzz_input` 是**载荷**（`admin` / `.git` / `backup.zip`），不是路径的一部分 ——
它正是"路径模板折叠"要抹平的那一维，所以它进证据层（"这条接口是靠猜哪个词
找到的"），不进身份。

`method` 从 `config.method` 取
------------------------------
ffuf 的每一条结果里没有方法字段：一次运行只用一个方法（`-X`），写在 `config`
里。把它发给每一条 endpoint 是**准确**的（这次爆破确实用的是它），而不是把一句
话抄 N 遍 —— 抄的那件事说的是"每条路径各自的方法"，与事实不符。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from ._urls import MAX_VALUE_CHARS, EndpointSink
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["KNOWN_FIELDS", "RESULT_FIELDS", "FfufParser", "parse_text"]

#: 本解析器认识的**顶层**字段。出现别的 key 就报未知字段（§5.6 的漂移信号）。
KNOWN_FIELDS: frozenset[str] = frozenset({
    "config", "results", "time", "version", "commandline", "input",
})

#: `results[]` 每一项里认识的字段。
RESULT_FIELDS: frozenset[str] = frozenset({
    "input", "position", "status", "length", "words", "lines", "content-type",
    "redirectlocation", "duration", "url", "host", "resultfile", "scenario",
    "content_length", "fuzzresult", "timestamp", "method",
})

#: 整份报告的字符数上限（见文件头）。
MAX_REPORT_CHARS = 32 * 1024 * 1024


class FfufParser(Parser):
    tool = "ffuf"
    format_version = "2"
    known_fields = KNOWN_FIELDS
    #: 与 dirsearch 同档：这是刚刚探到的响应，还带状态码与长度。
    max_confidence = 0.9
    exclusive_group = "url-list"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"整份样本是不是一份 ffuf 报告"打分：**0.95 或 0**。

        ⚠️ 判据是 ffuf 自己的签名结构 —— 顶层有 `results` 数组**且**有 `config`
        对象，且结果项带 `url`：

        * `dirsearch` 的报告也是"整份 JSON + `results` 数组"，但它的顶层是
          `target`（字符串），**没有 `config`** —— 这一条就是两个 JSON 来源
          分得开的地方（分不开 → 整批进人工裁决，§5.4）；
        * `katana` 是 JSONL：整份样本 `json.loads` 直接失败。即便只有一行，
          那一行也没有 `results` / `config`。

        不给"部分像"的中间分：整份 JSON 只有"是"与"不是"，给 0.6 那种分只会
        在仲裁表里制造一个说不清来源的第二名。
        """
        del filename
        try:
            doc = json.loads(sample.replace("\x00", ""))
        except (ValueError, TypeError):
            return 0.0
        if not isinstance(doc, dict):
            return 0.0
        results = doc.get("results")
        if not isinstance(results, list) or not isinstance(doc.get("config"), dict):
            return 0.0
        # 空数组是合法的一次"一无所获"；非空则至少得有一项带 url，否则
        # 这是一份长得像、但不是 ffuf 的东西。
        if results and not any(isinstance(item, dict)
                               and isinstance(item.get("url"), str)
                               for item in results):
            return 0.0
        return 0.95

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """先收全文（**唯一一个这样做的解析器**，理由见文件头），再逐条产出。"""
        parts: list[str] = []
        size = 0
        for line in lines:
            if ctx.cancelled():
                return
            size += len(line)
            if size > MAX_REPORT_CHARS:
                ctx.on_bad_line(
                    1, f"报告超过 {MAX_REPORT_CHARS} 字符的上限 —— ffuf 的一次"
                       f"爆破报告不该有这么大，拒绝把它整份读进内存", "")
                return
            parts.append(line)
        if not parts:
            return                       # 空文件不是坏行：没有任何结果也是合法的一份报告

        try:
            doc = json.loads("\n".join(parts))
        except ValueError as exc:
            ctx.on_bad_line(1, f"JSON 解析失败（ffuf 的 -of json 报告是一整个"
                               f"JSON 对象）：{exc}",
                            "".join(parts)[:MAX_VALUE_CHARS])
            return
        if not isinstance(doc, dict):
            ctx.on_bad_line(1, "不是 JSON 对象（ffuf 的报告顶层必须是一个对象）", "")
            return
        for key in doc:
            if key not in self.known_fields:
                ctx.on_unknown_field(str(key))
        results = doc.get("results")
        if results is None:
            ctx.on_bad_line(1, "报告里没有 `results` —— 这不是 ffuf 的 JSON 报告", "")
            return
        if not isinstance(results, list):
            ctx.on_bad_line(1, f"`results` 不是数组（收到 {type(results).__name__}）", "")
            return

        config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
        method = ""
        if config.get("method"):
            try:
                method = normalize.http_method(config.get("method"))
            except normalize.NormalizeError:
                # 认不出的方法**如实报成漂移**，不是折成空串抹平（与 nuclei 处理
                # 第七种 severity 同一口径）。
                ctx.on_unknown_field(f"config.method={config.get('method')!r}")
        sink = EndpointSink()
        reported: set[str] = set()
        for index, item in enumerate(results, start=1):
            if ctx.cancelled():
                return
            if not isinstance(item, dict):
                ctx.on_bad_line(
                    1, f"results[{index}] 不是对象（收到 {type(item).__name__}）", "")
                continue
            for key in item:
                # 只报一次：一份报告里几千条结果，逐条报同一个新字段会把
                # "检测到新字段 x（N 行）"里的 N 变成结果条数，噪声盖过信号。
                name = f"results[].{key}"
                if key not in RESULT_FIELDS and name not in reported:
                    reported.add(name)
                    ctx.on_unknown_field(name)
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                ctx.on_bad_line(
                    1, f"results[{index}] 缺 url（或它不是字符串）：{url!r}", "")
                continue
            yield from sink.emit(url, ctx, 1, method=method,
                                 extra=(("status", item.get("status")),
                                        ("length", item.get("length")),
                                        ("duration", item.get("duration")),
                                        ("fuzz_input", _fuzz_input(item))))


def _fuzz_input(item: dict[str, Any]) -> str:
    """`input.FUZZ` → 这一次用的载荷。

    `input` 是一个字典（键是 URL 里被替换掉的那个关键字，默认 `FUZZ`）。
    多个关键字时按 `k=v` 拼起来 —— 顺序由排序固定，否则同一份报告两次解析会
    得到两种写法，而观测的身份是 (实体, 字段, 值)。
    """
    payload = item.get("input")
    if isinstance(payload, dict):
        pairs = [f"{k}={payload[k]}" for k in sorted(payload)]
        return ", ".join(pairs)
    if isinstance(payload, str):
        return payload
    return ""


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(FfufParser().parse(text.splitlines(), ctx or OfflineContext()))
