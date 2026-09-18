"""CLI 的对外契约：JSON 信封与退出码表（2.2 冻结）。

信封与 `/api/v1` **逐字同形** —— 2.7 已先把 HTTP 那一半冻结了，2.2 的这一半
是它的镜像，不是另立一套::

    {"ok": true,  "data": ..., "meta": {"db": "...", "took_ms": 12, "schema_version": 3}}
    {"ok": false, "error": {"code": "E_AMBIGUOUS", "message": "...", "hint": "...",
                            "candidates": []}}

退出码表 **0/1/2/3/4/5 与 2.7 的 HTTP 状态码一一对应**（LARI-8《排序变更记录②》
的裁定原文）。它不是"各定各的"，而是同一件事在两条传输上的两种写法：

    0 成功        ↔ 2xx
    1 用法错      ↔ 400
    2 数据错      ↔ 404
    3 需人工裁决  ↔ 409
    4 锁冲突      ↔ 423
    5 版本不匹配  ↔ 426

**`E_NOT_IMPLEMENTED` 落哪一档**（本步唯一需要裁定的映射）：表里没有"还没做"
这一档，而 2.7 的 501 是真实存在的一个状态（`search` 走的就是它）。选择映射到
**1（用法错）**，理由有二：

  * 对**调用方**而言，"你请求了一个本工具还没有的语义"就是一次用法错误 ——
    修法在他那边（换个命令 / 等 §8 落地），不在数据里；
  * 真正的信号在 `error.code`（`E_NOT_IMPLEMENTED`）与 `hint`（指向计划书哪一节）
    里，不在这个粗粒度的数字上。agent 应当按 code 分叉。

不新增第 6 档：表是冻结的，冻结的表在落地当天被自己扩一档，后面就没人守它了。

⚠️ **错误信封不带 meta**。这不是省事，是与 `/api/v1` 的错误形状保持逐字一致：
三处消费者（GUI / CLI / MCP）里但凡有一处按"错误里也有 meta"写，另一处就会崩。
`db` 路径这类上下文放进 `hint`，不进信封。
"""

from __future__ import annotations

import json
import sys
import unicodedata
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DATA = 2
EXIT_HUMAN = 3
EXIT_LOCKED = 4
EXIT_VERSION = 5

#: 退出码 → HTTP 状态码。**这张表是"一一对应"的唯一声明处**，CLI 与 API 的任何
#: 一侧改了这一列，另一侧必须同时改 —— 两处对不上就等于两个真相。
EXIT_TO_HTTP: dict[int, str] = {
    EXIT_OK: "2xx", EXIT_USAGE: "400", EXIT_DATA: "404", EXIT_HUMAN: "409",
    EXIT_LOCKED: "423", EXIT_VERSION: "426",
}

#: 退出码 → 人读含义（`--human` 的报错尾巴与文档都用它）。
EXIT_MEANING: dict[int, str] = {
    EXIT_OK: "成功", EXIT_USAGE: "用法错", EXIT_DATA: "数据错",
    EXIT_HUMAN: "需人工裁决", EXIT_LOCKED: "锁冲突", EXIT_VERSION: "版本不匹配",
}


class CliError(Exception):
    """一次可读的 CLI 失败。命令 raise 它，`main` 统一转成信封 + 退出码。"""

    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_DATA,
                 hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.exit_code = int(exit_code)
        self.hint = hint
        #: 结构化补充 —— §6.3 信封里的 `"candidates": [...]` 就落在这里。
        self.extra = extra

    def envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        error.update(self.extra)
        return {"ok": False, "error": error}

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"CliError({self.exit_code}, {self.code!r}, {self.message!r})"


def usage_error(message: str, *, hint: str | None = None, **extra: Any) -> CliError:
    """用法错（退出码 1 ↔ HTTP 400）。"""
    return CliError("E_USAGE", message, exit_code=EXIT_USAGE, hint=hint, **extra)


def not_implemented(what: str, *, plan: str) -> CliError:
    """没交付的命令必须**响亮** —— 与 2.7 的 501 同一个理由。

    返回一个空列表对 agent 是一个**确定的断言**（"这里就是没有"），而事实是
    "这条命令还不存在"。两者混起来，agent 会基于一个假事实继续往下走。
    """
    return CliError(
        "E_NOT_IMPLEMENTED", f"{what} 尚未实现", exit_code=EXIT_USAGE,
        hint=f"它属于 {plan}；本步（2.2）只交付只读命令。"
             f"在它落地之前，别把这个结果当成'查过了，没有'。",
        plan=plan)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _force_utf8() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    Windows 控制台默认 GBK：不改的话，被管道/重定向接住时中文会乱码或直接
    `UnicodeEncodeError`。`magpie app` 打印端口与 token 时踩过同一个坑
    （见 `magpie/api/app.py` 的 `serve`），这里沿用同一条处置。

    **这不是唯一一道保险**：`--json` 另外用 `ensure_ascii=True`（见 `dump_json`），
    于是一个没设 `PYTHONIOENCODING` 的消费者也能安全读走。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):                # pragma: no cover
            pass


def dump_json(obj: Any) -> str:
    """信封的序列化 —— **纯 ASCII 输出**。

    `ensure_ascii=True` 不是保守，是为了让可消费性断言真的成立：契约要求
    `magpie ls --json | python -c "import json,sys;json.load(sys.stdin)"` 成功，
    而这个消费者在 Windows 上默认按 **GBK** 解 stdin。只要信封里有一个中文
    （项目名、identity_rule 的正文都是中文），UTF-8 字节被按 GBK 解就会当场
    `UnicodeDecodeError`。

    代价是 `\\u4e2d\\u6587` 这种转义在终端里不可读 —— 但 `--json` 本来就
    **不是给人读的**，给人读的是 `--human`。两者各管一头，比一个两头都不牢的
    折中好。
    """
    return json.dumps(obj, ensure_ascii=True, sort_keys=False, default=str)


def emit_ok(data: Any, meta: dict[str, Any], human: list[str], fmt: str) -> None:
    if fmt == "human":
        for line in human:
            print(line)
        return
    print(dump_json({"ok": True, "data": data, "meta": meta}))


def emit_error(err: CliError, fmt: str) -> None:
    if fmt == "human":
        print(f"错误 [{err.code}] {err.message}", file=sys.stderr)
        if err.hint:
            print(f"  {err.hint}", file=sys.stderr)
        print(f"  退出码 {err.exit_code} = {EXIT_MEANING.get(err.exit_code, '?')}"
              f"（↔ HTTP {EXIT_TO_HTTP.get(err.exit_code, '?')}）", file=sys.stderr)
        return
    print(dump_json(err.envelope()))


# ---------------------------------------------------------------------------
# `--human` 的表格
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """终端里的显示宽度。

    中日韩字符占**两格**，按 `len()` 补空格会让整张表在中英混排下错位 ——
    而这张表恰恰是中英混排的（`example.com` 挨着 `域名`）。
    `unicodedata.east_asian_width` 的 'W'/'F' 是两格，其余一格。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def table(header: list[str], rows: list[list[Any]]) -> list[str]:
    """等宽表。空 rows 时**只回表头**，由调用方决定要不要写"(空)"。

    不写"没有数据"这类句子：一个空的只读命令在有数据的库上是**异常**，
    在没有数据的库上是**正常**，两者要能分辨 —— 那件事交给 `data` 里的
    `count` 与信封的 `meta`，不交给一句人读的话。
    """
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [display_width(h) for h in header]
    for row in cells:
        for i, cell in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], display_width(cell))
    out = ["  ".join(h + " " * (widths[i] - display_width(h))
                     for i, h in enumerate(header)).rstrip()]
    out.append("  ".join("-" * w for w in widths).rstrip())
    for row in cells:
        out.append("  ".join(cell + " " * (widths[i] - display_width(cell))
                             for i, cell in enumerate(row[:len(widths)])).rstrip())
    return out
