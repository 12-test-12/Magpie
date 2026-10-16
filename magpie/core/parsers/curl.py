"""`curl` —— 粘贴进来的一条命令。

四条来源里唯一一个**不是文件格式**的来源：它的输入是一行命令。它存在的理由很
具体 —— 排查问题时从浏览器 devtools「Copy as cURL」、从同事的聊天记录、从一份
issue 里拿到的通常就是这一行，而这一行里恰好有接口的形状（目标、方法、查询串、
请求体的字段名）。

⚠️  解析边界：单条命令，**不做 shell 展开**
---------------------------------------------
计划书把这条写死了，而这个项目里有一条一模一样的既有取舍 —— `check.py` 的 C2
写过"只认字面量，不认变量"。口径是：

    **宁可明确地拒绝，也不要假装支持。**

具体到本解析器：

* 命令头 + 参数 + 目标，就这三样；
* **变量（`$VAR` / `${VAR}`）、命令替换（`` `…` `` / `$(…)`）、管道（`|`）、
  重定向（`>` / `<`）、分号、`&&` 一律不支持** —— 遇到就**报一条可读的坏行**
  （在**引号外**才判：`"…&…"` 里的 `&` 是 URL 的一部分，不是 shell 语法）；
* 一个命令里出现**两个目标**同样报坏行（"只取第一个"是猜，而猜错的代价是
  一整条接口挂到别的目标上）。

为什么不做展开：展开要一个 shell（或者一份环境），而"这条命令**本来会**请求
什么"与"这条命令**字面**请求什么"是两个不同的答案。把 `$HOST/api/users` 清洗
成 `HOST/api/users` 会造出一个不存在的接口，而且是**静默**的。

⚠️  凭据：一个字都不落
------------------------
`-u user:pass`、`-H "Authorization: Bearer …"`、`-b "session=…"`、
`--oauth2-bearer`、`--aws-sigv4` 都是**明文凭据**（G4 要求永不落库）。本步的线
一句话能说清，三个来源**共用同一条**：

    **header 不进参数树；`-u` / `-b` / `--oauth2-bearer` 既不落库也不落观测。**

不落"这个请求带了凭据"这件事，是因为那句话在 OpenAPI 里是 `security` +
`securitySchemes` 的事，不是 curl 一行能说清的 —— 半句事实比没有更糟。
用例 `test_curl_credentials_never_reach_the_database` 拿口令在整张库里搜一遍。

产出
----

| 记录 | 内容 |
|---|---|
| `apischema` 实体 | key = 目标 URL 的起源 |
| `endpoint` 实体 + `served_by` | 目标 URL |
| 关系 | `endpoint --declared_in--> apischema` |
| 观测 | `path` / `path_template` / `scheme` / `port` / `method`（观测型，单值） |
| 参数树 | `param.<名字>`：查询串的键 + `-d` 的**键名**（值一个字不读） |

方法与"多个目标"的细节
----------------------
* `-X/--request` 说了算；没写时 `-d` / `-F` / `--json` 的存在意味着 POST
  （curl 自己的默认值），`-I/--head` 意味着 HEAD，其余默认 GET；
* 输出文件（`-o` / `-O`）与它的值**不影响接口形状**，只是被跳过 —— 但**不认识
  的选项一律报坏行**：静静忽略一个选项等于假装支持它，而 `--retry 3` 这类"看着
  无害"的选项里，`--data` 的变体有好几个。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from ._apis import ApiSink
from ._urls import MAX_VALUE_CHARS
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["BOOLEAN_FLAGS", "VALUE_FLAGS", "CurlParser", "parse_text"]

#: **引号外**出现就拒绝的字符 —— 它们是 shell 语法，不是命令的字面内容。
#: 刻意**不含**单独的 `&`：`?a=1&b=2` 在引号里是 URL 的一部分，而引号外的 `&`
#: 由 `&&` 与"后台执行"两条更窄的判据覆盖（见 `_shell_metachar`）。
_SHELL_MARKS = frozenset("$`|><;()")

#: 带值的选项（长名）。`--json` 是 `-d` 的 JSON 变体，算请求体。
VALUE_FLAGS: frozenset[str] = frozenset({
    "--request", "--header", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "--data-ascii", "--json", "--form", "--form-string",
    "--url", "--user", "--cookie", "--cookie-jar", "--user-agent", "--referer",
    "--output", "--output-dir", "--connect-timeout", "--max-time", "--retry",
    "--retry-delay", "--proxy", "--cacert", "--cert", "--key", "--interface",
    "--resolve", "--write-out", "--oauth2-bearer", "--limit-rate",
    "--upload-file", "--max-redirs", "--trace", "--trace-ascii",
    "--dump-header", "--aws-sigv4", "--range", "--local-port", "--socks5",
    "--socks4", "--unix-socket", "--proto", "--pass", "--pubkey", "--hostpubmd5",
    # 与接口形状无关但**必须被认出来**，否则它们的值会被当成"第二个目标"
    "--next", "--config", "--header-file",
})

#: 不带值的选项（长名）。
BOOLEAN_FLAGS: frozenset[str] = frozenset({
    "--insecure", "--silent", "--show-error", "--location", "--verbose",
    "--include", "--compressed", "--fail", "--globoff", "--netrc",
    "--no-buffer", "--head", "--basic", "--digest", "--get", "--raw",
    "--path-as-is", "--remote-name", "--http1.0", "--http1.1", "--http2",
    "--tlsv1.2", "--tlsv1.3", "--disable", "--no-progress-meter",
    "--ssl-no-revoke", "--fail-with-body", "--no-keepalive", "--tcp-nodelay",
})

#: 带值的**短**选项（`-X POST` 与 `-XPOST` 两种写法都要认）。
_SHORT_VALUE: frozenset[str] = frozenset("XHdubAeomxwFTcD")

#: 不带值的**短**选项，可以连写（`-sSL` 是常见写法）。
_SHORT_BOOLEAN: frozenset[str] = frozenset("ksSLvifgnNqIGO046")

#: 短选项 → 长选项（一张表，两处判据共用一份事实）。
_SHORT_ALIAS: dict[str, str] = {
    "X": "--request", "H": "--header", "d": "--data", "u": "--user",
    "b": "--cookie", "A": "--user-agent", "e": "--referer", "o": "--output",
    "m": "--max-time", "x": "--proxy", "w": "--write-out", "F": "--form",
    "T": "--upload-file", "c": "--cookie-jar", "D": "--dump-header",
}

#: 请求体类选项（决定"没写 `-X` 时的方法"与参数树的来源）。
_DATA_FLAGS: frozenset[str] = frozenset({
    "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii",
    "--json", "--form", "--form-string",
})

#: **绝不落库**的选项：值是明文凭据（文件头）。
_CREDENTIAL_FLAGS: frozenset[str] = frozenset({
    "--user", "--cookie", "--oauth2-bearer", "--aws-sigv4", "--pass",
})

#: 命令行里出现它就跳过、**不影响形状**的选项（值也被吞掉）。
_IGNORED_FLAGS: frozenset[str] = frozenset({
    "--output", "--output-dir", "--user-agent", "--referer", "--connect-timeout",
    "--max-time", "--retry", "--retry-delay", "--proxy", "--cacert", "--cert",
    "--key", "--interface", "--resolve", "--write-out", "--limit-rate",
    "--upload-file", "--max-redirs", "--trace", "--trace-ascii",
    "--dump-header", "--range", "--local-port", "--socks5", "--socks4",
    "--unix-socket", "--proto", "--pubkey", "--hostpubmd5", "--cookie-jar",
    "--next", "--config", "--header-file",
    # ⚠️  `--header` 在**这里**，不在参数树里 —— 文件头那条凭据线的全部内容：
    # header 里真正有信息量的几个（`Authorization` / `Cookie` / `X-Api-Key`）
    # 恰恰是凭据，而其余是客户端特征（`User-Agent` / `Accept-Encoding`）。
    # 与其维护一张"哪些 header 安全"的白名单（它每天都要改，而漏掉一个是
    # **静默**的凭据落库），不如一条能一句话说清的线：header 一概不进参数树。
    "--header",
})


class CurlError(ValueError):
    """一条 curl 命令的**可读**拒绝理由 —— 会被写进坏行报告给人看。"""


class CurlParser(Parser):
    tool = "curl"
    format_version = "2"
    #: 一行命令没有"字段"可言（同 gau / domainlist）：格式漂移的信号是"能解析
    #: 的命令比例掉下来"，由 `sniff` 承担。
    known_fields: frozenset[str] = frozenset()
    #: 粘贴来的一条命令是**人写的**，可能改过、可能不完整。同 postman 一档。
    max_confidence = 0.9
    exclusive_group = "api-schema"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """样本里**过半的行**以 `curl` 开头 → 认领。

        判据与 `parse()` 用的是**同一个函数**（`_logical_lines` + `_command_head`）：
        否则会出现"我很确定这是 curl 的输出，然后一行都读不出来"
        （domainlist 的 `accepts` 那段注释写的就是这件事）。

        刻意**不**看文件名，也刻意**不**看 `-` / `--` 之类的特征 —— 前者对仲裁
        没有证明力，后者会把一份恰好带连字符的清单抢过来。
        """
        del filename
        lines = _logical_lines(sample.replace("\x00", ""))
        considered = 0
        hits = 0
        for line in lines[:40]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            considered += 1
            if _command_head(stripped):
                hits += 1
        if not considered:
            return 0.0
        ratio = hits / considered
        if ratio >= 0.8:
            return 0.9
        if ratio >= 0.5:
            return 0.6
        return 0.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐行产出。`lines` 是**流式**迭代器，这里按行读、不物化整份文件。

        ⚠️  反斜杠续行需要把**两行**拼起来才是一条命令，所以这里有**一个**槽位
        记住"上一行以 `\\` 结尾"—— 它不是把这批行物化成列表（
        `test_parsers_never_list_the_whole_file` 的哨兵迭代器会当场抗议）。
        """
        sink = ApiSink()
        pending = ""
        start_line = 0
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.rstrip()
            if not pending:
                start_line = lineno
            pending = f"{pending} {text[:-1]}" if text.endswith("\\") else f"{pending} {text}"
            if text.endswith("\\"):
                continue                     # 续行：还没读完一条命令
            command, pending = pending.strip(), ""
            if not command or command.startswith("#"):
                continue
            if not _command_head(command):
                ctx.on_bad_line(start_line, "不是一条 curl 命令（本解析器只认以 "
                                            "curl 开头的行；一行一条命令）",
                                command[:MAX_VALUE_CHARS])
                continue
            yield from self._command(command, sink, ctx, start_line)
        if pending.strip():
            ctx.on_bad_line(start_line, "最后一行以 `\\` 结尾，命令被截断了",
                            pending.strip()[:MAX_VALUE_CHARS])
        yield from sink.declared()

    def _command(self, command: str, sink: ApiSink, ctx: ParseContext,
                 lineno: int) -> Iterator[Record]:
        try:
            tokens = _tokenize(command)
            target, method, data = _scan(tokens)
        except CurlError as exc:
            ctx.on_bad_line(lineno, f"这条 curl 命令读不了：{exc}",
                            command[:MAX_VALUE_CHARS])
            return
        yield from sink.emit_operation(
            target, ctx, lineno, method=method,
            params=_params(target, data))


# ---------------------------------------------------------------------------
# 词法与语法
# ---------------------------------------------------------------------------


def _command_head(line: str) -> bool:
    """这一行的命令头是不是 `curl`（**词边界**：`curlie` 不算）。

    认路径写法（`/usr/bin/curl` / `curl.exe`）：粘贴进来的命令常常带着它，而
    把它当成"不是 curl"会让最常见的一种输入掉进坏行报告。
    """
    text = line.strip()
    if not text:
        return False
    head = text.split(None, 1)[0]
    name = head.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "curl"


def _logical_lines(sample: str) -> list[str]:
    """样本 → 合并了 `\\` 续行的逻辑行（嗅探用；`parse()` 走的是流式版本）。"""
    out: list[str] = []
    pending = ""
    for raw in sample.splitlines():
        text = raw.rstrip()
        pending = f"{pending} {text[:-1]}" if text.endswith("\\") else f"{pending} {text}"
        if not text.endswith("\\"):
            out.append(pending.strip())
            pending = ""
    if pending.strip():
        out.append(pending.strip())
    return out


def _shell_metachar(text: str, index: int) -> str:
    """`text[index]` 是不是**引号外**的 shell 语法；是就返回它（用于报错）。"""
    ch = text[index]
    if ch in _SHELL_MARKS:
        return ch
    if ch == "&":
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if nxt == "&":
            return "&&"
        if index > 0 and text[index - 1] == "&":
            return "&&"
    return ""


def _tokenize(command: str) -> list[str]:
    """一条命令 → token 列表。**只解引号与反斜杠转义，不做任何展开。**

    引号外的 shell 语法在这里就被拒（`CurlError`）—— 放在这里而不是 `_scan`
    里，是因为"这条命令根本不是单条命令"比"这个选项不认识"更靠前：报错顺序
    决定了使用者先看到哪一句。
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    length = len(command)
    while index < length:
        ch = command[index]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and index + 1 < length:
                buf.append(command[index + 1])
                index += 2
                continue
            else:
                buf.append(ch)
            index += 1
            continue
        mark = _shell_metachar(command, index)
        if mark:
            raise CurlError(
                f"命令里有 shell 语法 `{mark}` —— 本解析器**不做 shell 展开**"
                f"（变量、管道、重定向、命令替换都不支持）。把命令里的变量换成"
                f"字面值再来")
        if ch in "'\"":
            quote = ch
            index += 1
            continue
        if ch == "\\" and index + 1 < length:
            buf.append(command[index + 1])
            index += 2
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            index += 1
            continue
        buf.append(ch)
        index += 1
    if quote:
        raise CurlError(f"引号没有闭合（少了收尾的 {quote}）")
    if buf:
        tokens.append("".join(buf))
    return tokens


def _scan(tokens: list[str]) -> tuple[str, str, list[str]]:
    """token 列表 → `(目标 URL, 方法, 请求体片段列表)`。

    不认识的选项、缺值的选项、两个目标一律抛 `CurlError`（可读）。
    """
    if not tokens or not _command_head(tokens[0]):
        raise CurlError("第一段不是 curl")
    target = ""
    method = ""
    data: list[str] = []
    head = False
    index = 1
    length = len(tokens)
    while index < length:
        token = tokens[index]
        if token == "--":
            index += 1
            while index < length:
                target = _take_target(target, tokens[index])
                index += 1
            break
        if token.startswith("--"):
            name, eq, attached = token.partition("=")
            if name in BOOLEAN_FLAGS:
                if eq:
                    raise CurlError(f"`{name}` 不带值，却写了 `={attached}`")
                head = head or name == "--head"
                index += 1
                continue
            if name not in VALUE_FLAGS:
                raise CurlError(
                    f"不认识的 curl 选项 `{name}` —— 本解析器只认一份写死的选项表"
                    f"（宁可明确地拒绝，也不要假装支持：被忽略的选项可能正是"
                    f"决定请求形状的那一个）")
            if eq:
                value = attached
                index += 1
            else:
                if index + 1 >= length:
                    raise CurlError(f"`{name}` 后面缺一个值")
                value = tokens[index + 1]
                index += 2
            if name == "--url":
                target = _take_target(target, value)
                continue
            if name in _DATA_FLAGS:
                data.append(value)
                continue
            if name in _CREDENTIAL_FLAGS:
                continue                          # 凭据：不落库（文件头）
            if name in _IGNORED_FLAGS:
                continue
            if name == "--request":
                method = _method(value)
                continue
            raise CurlError(f"`{name}` 还没有被本解析器接纳")
        if token.startswith("-") and len(token) > 1:
            flag = token[1]
            rest = token[2:]
            if flag in _SHORT_VALUE:
                if rest:
                    value = rest
                    index += 1
                else:
                    if index + 1 >= length:
                        raise CurlError(f"`-{flag}` 后面缺一个值")
                    value = tokens[index + 1]
                    index += 2
                long_name = _SHORT_ALIAS.get(flag, "")
                if long_name == "--url":
                    target = _take_target(target, value)
                elif long_name in _DATA_FLAGS:
                    data.append(value)
                elif long_name in _CREDENTIAL_FLAGS:
                    continue                      # 凭据：不落库
                elif long_name in _IGNORED_FLAGS:
                    continue
                elif long_name == "--request":
                    method = _method(value)
                else:
                    raise CurlError(f"`-{flag}` 还没有被本解析器接纳")
                continue
            for ch in token[1:]:
                if ch not in _SHORT_BOOLEAN:
                    raise CurlError(
                        f"不认识的 curl 选项 `-{ch}` —— 短选项可以连写"
                        f"（`-sSL`），但每一个都必须在写死的那张表里")
                head = head or ch == "I"
            index += 1
            continue
        target = _take_target(target, token)
        index += 1

    if not target:
        raise CurlError("命令里没有目标 URL —— curl 的一条命令至少要有一样东西"
                        "可以请求")
    if not method:
        # curl 自己的默认值：有请求体就是 POST，`-I` 是 HEAD，其余 GET。
        method = "POST" if data else ("HEAD" if head else "GET")
    return target, method, data


def _take_target(current: str, value: str) -> str:
    if current:
        raise CurlError(f"命令里有第二个目标 {value!r} —— 单条命令只取一个目标，"
                        f"多出来的那个不猜（猜错的代价是整条接口挂到别的目标上）")
    return value


def _method(value: str) -> str:
    try:
        return normalize.http_method(value)
    except normalize.NormalizeError as exc:
        raise CurlError(f"`-X` 的方法认不出：{exc}") from exc


def _params(target: str, data: list[str]) -> list[tuple[str, dict]]:
    """查询串的键 + 请求体的**键名**（值一个字都不读，见文件头）。"""
    out: list[tuple[str, dict]] = []
    for name in _query_names(target):
        out.append((name, {"in": "query"}))
    for blob in data:
        for name, where in _data_names(blob):
            out.append((name, {"in": where}))
    return out


def _query_names(target: str) -> list[str]:
    """URL 的查询串 → 键名（**值不取**：它可能是 token）。"""
    path = target.split("#", 1)[0]
    _, sep, query = path.partition("?")
    if not sep or not query:
        return []
    names: list[str] = []
    for pair in query.split("&"):
        name = pair.partition("=")[0]
        if name:
            names.append(name)
    return names


def _data_names(blob: str) -> list[tuple[str, str]]:
    """一段请求体 → `(键名, 位置)`。位置一律是粗档 `body`（`_apis.py` 的文件头）。

    两种形状认，其余一律**不猜**：

    * `k=v&k2=v2`（含 `@file` / `@-` 的 `-d` 语义：`k=@file` 只取键名 `k`）；
    * 一个 JSON 对象的顶层键（`--json` 与 `-d '{"…":…}'`）。

    认不出就返回空 —— 一份 XML / GraphQL body 的"键"不是参数，硬解会造出
    不存在的参数。
    """
    text = blob.strip()
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return [(str(key), "body") for key in parsed]
    if "=" not in text:
        return []
    out: list[tuple[str, str]] = []
    for pair in text.split("&"):
        name = pair.partition("=")[0].strip()
        if name:
            out.append((name, "body"))
    return out


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(CurlParser().parse(text.splitlines(), ctx or OfflineContext()))
