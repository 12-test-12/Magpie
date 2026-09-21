"""`dirsearch` —— 目录爆破工具 dirsearch 的**文本报告**（计划书 §4.3 的 urls 行之一）。

dirsearch 主动试一批字典里的路径，把**有响应的**那些打出来。所以它给的是
"这些路径现在真的在"，而且带着状态码 —— 证据强度与 gau 的"历史存档里有"不是
一回事（这正是它们该是两个来源的理由）。

为什么要专门解析它的文本报告
----------------------------
dirsearch 的报告线里有两样别处没有的东西：**状态码**（200 / 301 / 403 —— 403 是
"存在但不让看"，是极有价值的一类）与**重定向目标**（`-> /images/`）。
而且它的默认输出给的是**相对路径**，主机在被称作 `Target:` 的那一行上 ——
所以这个解析器有一个别人没有的状态：**当前 base**。

报告形状（本解析器支持的形态，逐字）
-------------------------------------

    Extensions: php, aspx, jsp | HTTP method: GET | Threads: 30
    Target: http://app.example.com:8080/

    [12:00:00] Starting:
    [12:00:01] 200 -  123B  - /admin
    [12:00:02] 301 -    0B  - /images  ->  /images/
    [12:00:03] 403 -  456B  - /.git/

* `Target: <绝对 URL>` —— 设置当前 base。多目标时会再出现一次，**覆盖**前一个
  （dirsearch 本身就是一段一段打的，前一段的路径与后一段的主机无关）；
* `[HH:MM:SS] <状态码> - <大小> - <路径>[  ->  <重定向>]` —— 一条结果；
* 报告自己的门面行（`Extensions:` / `Starting:` / `Task Completed` 之类）
  **跳过**，不报坏行 —— 它们不是坏数据，是报告的边框。把边框报成坏行会让
  "坏行数"这个质量信号被自己的噪声淹掉；
* 其余非空行一律是坏行。

⚠️  相对路径**必须**有 base：还没见过 `Target:` 就来一条 `/admin`，那是坏行而不是
"猜一个主机"。猜错的代价是一整批接口挂到错误的主机上，而且不会有任何信号
（`normalize._split_base` 的注释写透了这一条）。

`-> /images/` 的归属
--------------------
重定向目标**不**单独成一条 endpoint：它是**这一条路径**的观测（`redirect`），
因为"`/images` 跳到 `/images/`"说的是这两个地址的关系，而不是"`/images/` 这个
接口被发现了"。把重定向目标也建成 endpoint 会让目录爆破的结果凭空虚增一倍。

`method` 观测
-------------
报告的抬头里有 `HTTP method: GET`，但那是**这一次运行**用的方法，不是每一条
路径的方法 —— 拿它当每条 endpoint 的方法等于把一句话抄 N 遍。本解析器不写
`method` 观测（`_urls.py` 的文件头写透了"缺失"与"UNKNOWN"的区别）。
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .. import normalize
from ._urls import MAX_VALUE_CHARS, EndpointSink, sample_lines
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["DirsearchParser", "is_result_line", "parse_text"]

#: 报告自己的门面行 —— **跳过**，不是坏行（理由见文件头）。
#: 这是"不以 `[` 开头"的那一半；带时间戳的那一半（`[12:00:00] Starting:`）由
#: `is_furniture` 的第二个条件兜住。
_FURNITURE = ("Extensions:", "Target:", "Output File:", "Task Completed",
              "Wordlist:", "Threads:", "HTTP method:",
              "Error Log:", "Report File:")

#: 状态码永远是三位数（`200` / `301` / `403` / `500`）。
_STATUS_LEN = 3


def _after_bracket(line: str) -> str | None:
    """`[12:00:01] 200 -  123B  - /admin` → `200 -  123B  - /admin`。

    不是"以 `[` 开头且有一个 `]`"的行 → `None`。刻意不看方括号里的内容是不是
    合法时间：`[12:00:01]` 与 `[2026-09-17 12:00:01]` 都是 dirsearch 打过的
    形状，认死一种会让另一种整份变成坏行。
    """
    if not line.startswith("["):
        return None
    end = line.find("]")
    if end < 0:
        return None
    return line[end + 1:].strip()


def is_result_line(line: str) -> bool:
    """这一行是不是一条结果。**`sniff` 与 `parse` 共用它**（同一判据两处用法
    会让"我很确定这是 dirsearch 的报告，然后一行都读不出来"重现）。
    """
    rest = _after_bracket(line)
    if not rest:
        return False
    head = rest.split(" ", 1)[0]
    return len(head) == _STATUS_LEN and head.isascii() and head.isdigit()


def is_furniture(line: str) -> bool:
    """这一行是报告自己的边框（而不是一条结果、也不是坏数据）。

    两类：**不带时间戳**的抬头（`Extensions:` / `Task Completed` …），与
    **带时间戳但第三段不是状态码**的进度行（`[12:00:00] Starting: `）。
    两类都跳过 —— 报告的边框不是坏数据，把它报成坏行会让"坏行数"这个质量信号
    被自己的噪声淹掉，而那是这个信号唯一的用途。
    """
    return line.startswith(_FURNITURE) or _after_bracket(line) is not None


def _split_result(line: str) -> tuple[str, str, str]:
    """一条结果 → `(status, path, redirect)`。只在 `is_result_line` 为真时调用。

    中间那个**大小**（`123B` / `1KB`）被读出来又丢掉：它是"这一次响应的体积"，
    随压缩、随页面改动而变，作为一条长期观测只会在重导时制造噪声 —— 而
    `status` 与 `redirect` 是稳定的结构与事实。丢弃是**有意的**，不是漏了。
    """
    rest = _after_bracket(line) or ""
    fields = rest.split(" - ", 2)
    while len(fields) < 3:
        fields.append("")
    status, _size, tail = (f.strip() for f in fields)
    # `  ->  /images/` 是重定向目标，不是路径的一部分；`->` 两边有无空格都认。
    path, _, redirect = tail.partition("->")
    return status, path.strip(), redirect.strip()


class DirsearchParser(Parser):
    tool = "dirsearch"
    format_version = "1"
    #: 行格式没有字段可言（见 gau 的同一条）。漂移信号是"结果行的比例掉下来"。
    known_fields: frozenset[str] = frozenset()
    #: 比 gau 高一档：这是**刚刚探到**的响应，还带状态码。但仍低于 httpx 的
    #: 0.95 —— 一次 403 说明"路径存在"，不说明"这个接口在做接口的事"。
    max_confidence = 0.9
    exclusive_group = "url-list"

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是报告的一部分（门面 / Target / 结果）"打分，
        并且**至少要有一条结果行** —— 只有 `Target:` 抬头的文件不是一份报告。

        ⚠️ 不能按"行里有没有 `://`"打分：那会把 gau 的整份文件认领走（gau 的
        每一行都是带 `://` 的 URL）。这里要求行**以 `[` 开头且第三段是三位
        状态码**，gau 的行不满足、katana 与 ffuf 的行以 `{` 开头也不满足。
        """
        del filename
        lines = sample_lines(sample)
        if not lines:
            return 0.0
        results = sum(1 for ln in lines if is_result_line(ln))
        if results == 0:
            return 0.0
        recognised = sum(1 for ln in lines if is_result_line(ln) or is_furniture(ln))
        ratio = recognised / len(lines)
        if ratio >= 0.8:
            return 0.9
        if ratio >= 0.5:
            return 0.6
        return 0.0

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐行产出。`base` 是**解析器实例之外**的状态，但它跟着一段 Target 段
        走，且在文件内是确定的 —— 所以它不破坏"同一份文件算出同一批记录"。
        """
        sink = EndpointSink()
        base: str | None = None
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text:
                continue
            if text.startswith("Target:"):
                target = text.split(":", 1)[1].strip()
                try:
                    # **在读到这一行时就校验**，而不是等后面某条结果行才报错 ——
                    # 否则错误会指向一条其实没问题的 `/admin`，而真正写坏的是
                    # 上面那行 Target。
                    base = normalize.endpoint_base(target)
                except normalize.NormalizeError as exc:
                    ctx.on_bad_line(lineno, f"`Target:` 不能当 base 用：{exc}",
                                    text[:MAX_VALUE_CHARS])
                    continue
                # 多目标时覆盖 —— dirsearch 本身就是一段一段打的，前一段的路径
                # 与后一段的主机无关。
                continue
            if not is_result_line(text):
                if is_furniture(text):
                    continue
                ctx.on_bad_line(
                    lineno,
                    "不是 dirsearch 的一条结果（`[时间] 状态码 - 大小 - 路径`）"
                    "，也不是报告的门面行", text[:MAX_VALUE_CHARS])
                continue
            status, path, redirect = _split_result(text)
            if not path:
                ctx.on_bad_line(lineno, "这条结果里没有路径", text[:MAX_VALUE_CHARS])
                continue
            yield from sink.emit(path, ctx, lineno, base=base,
                                 extra=(("status", status),
                                        ("redirect", redirect)))


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(DirsearchParser().parse(text.splitlines(), ctx or OfflineContext()))
