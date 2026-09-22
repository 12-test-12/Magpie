"""离线指纹引擎的零件（4.3）—— 规则加载、响应拆解、命中匹配、落记录。

为什么这个文件不在 `scan.py` 那一边（管线阶段），而在 `parsers/` 里
------------------------------------------------------------------
4.2 的敏感串扫描是**管线阶段**：任何格式的任何一行都可能有凭据，它与解析器
正交，所以它的 `produced_by` 结构上是空的。指纹引擎走的是另一条路，而且是
判据指定的那条：命中要写 `service --runs--> product`，**必须**先知道这段响应
属于哪个服务 —— 那需要请求行 / Host / URL，而这些东西只有"认领这份文件"的
**解析器**才有资格从格式里读出来。于是它是一个真正的解析器
（`fingerprint.py` 的 `FingerprintParser`），`kind_registry.produced_by` 里
`product` / `service` 各多一个 `fingerprint@1`（迁移 013 用 `json_insert` 追加）。

为什么匹配逻辑单独住在 `_fingerprints.py` 而不是解析器类所在的模块
------------------------------------------------------------------
`check.py` 的 PARSE-NORM 只对**定义了 `class X(Parser)` 的模块**生效：那些模块
不许 `import re`、不许 `.lower()` —— 归一化只能走 `normalize.py`。而规则匹配
离不开 `re`，响应头名的大小写折叠也离不开 `.lower()`（`Server` 与 `server` 是
同一个头，这不是"实体归一化"，是 HTTP 语法本身）。把匹配放进 `_` 前缀的零件
模块（没有 Parser 类，发现机制与 PARSE-NORM 都不碰它），解析器类所在的模块
保持"只认格式、只调 normalize"—— 与 `_urls.py` / `_json.py` 同一条先例：
零件不是解析器，但 PARSE-RO（不得 import 写入口）照常管它。

YAML 走 extras，报错必须是人话（照 3.4 OpenAPI 的模板）
------------------------------------------------------------------
规则是 `fingerprints/*.yaml` 这样的**数据**，解析 YAML 不在标准库里。红线 R6
要求 `magpie/core/**` 的**顶层** import ⊆ 标准库，所以 `import yaml` 只能写在
`_load_yaml()` 的**函数体**里。于是缺 extras 时的行为被死钉在三句话上：

* 抛 `MissingExtrasError`，消息里说清缺哪个 extras、跑哪条命令装；
* **绝不**是裸 `ImportError`（那把人引去看 `sys.path`）；
* **绝不**静默降级成"没有命中"（那会把环境问题伪装成"这台主机没有指纹"）。

嗅探（`FingerprintParser.sniff`）刻意**不碰 yaml**：环境问题不得伪装成格式问题
—— 这是 3.4 点名过的头号陷阱。

规则集 digest —— "改规则必变"的机械形态
------------------------------------------------------------------
`rules_digest()` 把每个规则文件的**文件名 + 原始字节**依次喂进 sha256。它经
`FingerprintParser.definition_extra()` 混入 `ingest._definition_hash()`：

    改任何规则 → digest 变 → definition_hash 变
               → 回归夹具里钉着的 digest 先红（test_fingerprints.py）
               → 重导时 `definition_drift` 报"同一 fingerprint 背后的实现变过"

这就是阶段交付标准「规则改动必须让夹具失败一次，再加新用例修到绿」的可执行
形态：夹具红是**机械必然**，不靠自觉。digest 只吃原始字节、不解析 YAML ——
所以没装 pyyaml 的环境里它照样算得出同一个值（definition_hash 不因环境而分叉）。

零出站（R7）
------------------------------------------------------------------
本模块读的只有规则文件与已经**在盘上**的响应文本，不 import 任何出站客户端、
不碰 socket。指纹引擎与扫描器的区别是"它认格式、能挂 service"，与探测器的
区别是**它一个包都不发**（计划书 §4：Magpie 不主动取数据）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .. import normalize
from .base import MissingExtrasError, RawEntity, RawObservation, RawRelation, \
    extras_install_hint
from .httpx import product_key, service_key

__all__ = ["MAX_EVIDENCE_CHARS", "RULES_DIR", "Exchange", "Hit", "Rule",
           "build_records", "load_rules", "match_rules", "parse_exchange",
           "request_line_head", "rules_digest", "status_line_head"]

#: 规则目录 = 仓库根的 `fingerprints/`（与 `schema/` 同一条先例：运行时要读的
#: 数据住在仓库根，不在包内 —— 见 `migrate.SCHEMA_DIR` 的注释）。
#: `__file__` = `magpie/core/parsers/_fingerprints.py` → parents[3] = 仓库根。
RULES_DIR = Path(__file__).resolve().parents[3] / "fingerprints"

#: YAML 那一个 extras 的名字与模块名（与 `openapi.py` 的两个常量同名同义）。
_YAML_EXTRAS = "api-yaml"
_YAML_MODULE = "yaml"

#: 命中证据的字符上限。响应头一行通常不到 100 字符；封顶防的是一条把整页
#: HTML 当证据的规则把观测值撑成一份文档（同 `_urls.MAX_VALUE_CHARS` 的动机）。
MAX_EVIDENCE_CHARS = 160

#: 规则名的形状：小写字母开头，之后只许 `[a-z0-9]` 与 `-` / `_` 分段。
#: 它同时是 `sources.name`（观测的 source），所以不许空白、不许大写 ——
#: `nginx-server` 与 `Nginx-Server` 会变成两个 source，而那是假分歧。
_RULE_NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")

#: `match.target` 的两个合法取值。写成元组而不是字符串判断：多一个 target
#: （比如 `cookie`）时改这一行就是一次要过 review 的 diff。
_TARGETS = ("header", "body")


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """一条编译好的指纹规则。**构造只能走 `load_rules()`** —— 校验都在那里。"""

    #: 规则名（全局唯一；同时是观测的 `source`）。
    name: str
    #: 规范化后的产品家族名（已过 `normalize.product_name` 别名表）。
    product: str
    #: "header" | "body"。
    target: str
    #: target=header 时匹配的头名（**原样保存**，匹配时才做大小写折叠）。
    header: str
    #: 原始 pattern 文本（进错误消息与夹具，方便"哪条规则坏了"一眼可见）。
    pattern: str
    #: 编译好的正则（`load_rules` 保证编译成功）。
    compiled: "re.Pattern[str]"
    #: 版本取第几个捕获组（1 起）；0 = 这条规则不带版本。
    version_from: int


def _load_yaml(text: str, origin: str) -> object:
    """YAML 文本 → Python 对象。**extras 缺失时抛可读错误**（文件头讲透了）。

    `import` 在函数体里，不在模块顶层 —— 那是 R6 与 extras 两条约束唯一的交点
    （`check.py` 的 R6 只看 `tree.body`）。形状照抄 `openapi._load_yaml`：
    两处各写一份是刻意的 —— 报错文案说的是**各自**的文件（"指纹规则" vs
    "OpenAPI 文档"），抽成一个带参数的公共函数只会让下一个人改文案时
    两处都要想一遍。
    """
    try:
        import yaml                                    # noqa: PLC0415 —— 见上
    except ImportError as exc:                         # pragma: no cover - 分开测
        raise MissingExtrasError(
            f"{origin} 是 YAML 格式的指纹规则，而 YAML 解析器不在标准库里。"
            f"缺少 extras `{_YAML_EXTRAS}`（pyyaml）—— 装上它："
            f"`{extras_install_hint(_YAML_EXTRAS)}`。"
            f"（core 本身零第三方依赖，所以这个 import 只在真的读规则时发生。）"
        ) from exc
    try:
        return yaml.safe_load(text)
    except Exception as exc:                           # noqa: BLE001 —— yaml 的异常谱系很宽
        raise ValueError(f"{origin} 的 YAML 解析失败：{exc}") from exc


def iter_rule_files(rules_dir: Path | None = None) -> list[Path]:
    """规则目录 → 排序后的 `*.yaml` 清单。**排序进 digest**：换个文件系统遍历
    顺序不该改变 definition_hash。"""
    directory = Path(rules_dir) if rules_dir else RULES_DIR
    return sorted(directory.glob("*.yaml"))


def load_rules(rules_dir: Path | None = None) -> list[Rule]:
    """读全部规则 → 编译好的 `Rule` 列表（声明顺序 = 文件名排序 + 文件内顺序）。

    校验在这一层**一次做完**，坏规则抛 `ValueError`（消息带文件名与规则名）：
    管线会把它折成可读的 `parse_error`，而不是让一条坏 pattern 在匹配到第
    一万行时才炸、或者更糟 —— 静默不命中（那会把"规则写错了"伪装成"这台
    主机没有指纹"）。
    """
    files = iter_rule_files(rules_dir)
    if not files:
        directory = Path(rules_dir) if rules_dir else RULES_DIR
        raise ValueError(
            f"规则目录 {directory} 下没有任何 *.yaml —— 指纹引擎没有规则就没有"
            f"可执行的判据；这是仓库内容缺失，不是输入文件的问题")
    rules: list[Rule] = []
    seen: dict[str, str] = {}
    for path in files:
        doc = _load_yaml(path.read_text(encoding="utf-8"), origin=path.name)
        if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
            raise ValueError(f"{path.name}: 顶层必须是带 `rules:` 列表的对象")
        if not doc["rules"]:
            # 空文件是错的，不是"暂无规则"：一个 `rules: []` 的文件唯一的可能
            # 是写错了，而静默跳过它会让"规则去哪了"这个问题没有答案。
            raise ValueError(f"{path.name}: `rules:` 是空的 —— 一条规则都没有")
        for index, raw in enumerate(doc["rules"]):
            rule = _compile_rule(path.name, index, raw)
            if rule.name in seen:
                raise ValueError(
                    f"{path.name} 的规则 {rule.name!r} 与 {seen[rule.name]} 重名 —— "
                    f"规则名是命中身份（观测的 source），必须全局唯一")
            seen[rule.name] = path.name
            rules.append(rule)
    if not rules:
        raise ValueError("所有规则文件都是空的（一条规则都没有）—— "
                         "指纹引擎没有规则就没有可执行的判据")
    return rules


def _compile_rule(origin: str, index: int, raw: object) -> Rule:
    """一条 YAML 里的规则声明 → 校验、规范化、编译。坏的抛 `ValueError`。"""
    where = f"{origin} 第 {index + 1} 条规则"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是对象（收到 {type(raw).__name__}）")
    name = raw.get("name")
    if not isinstance(name, str) or not _RULE_NAME_RE.match(name):
        raise ValueError(
            f"{where} 的 name 不合法：{name!r} —— 规则名同时是观测的 source，"
            f"只许小写字母开头的 [a-z0-9_-] 分段")
    where = f"{origin} 规则 {name!r}"

    product = raw.get("product")
    if not isinstance(product, str) or not product.strip():
        raise ValueError(f"{where} 缺 `product`（产品名）")
    try:
        family = normalize.product_name(product)
    except normalize.NormalizeError as exc:
        raise ValueError(f"{where} 的 product 不合法：{exc}") from exc

    match = raw.get("match")
    if not isinstance(match, dict):
        raise ValueError(f"{where} 缺 `match` 对象")
    target = match.get("target")
    if target not in _TARGETS:
        raise ValueError(f"{where} 的 match.target 必须是 {_TARGETS} 之一"
                         f"（收到 {target!r}）")
    pattern = match.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{where} 缺 match.pattern（正则）")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{where} 的 match.pattern 不是合法正则：{exc}") from exc

    header = match.get("name") if target == "header" else None
    if target == "header" and (not isinstance(header, str) or not header.strip()):
        raise ValueError(f"{where} 的 target=header 必须带 match.name"
                         f"（匹配哪个响应头）")

    version_from = raw.get("version_from", 0)
    if isinstance(version_from, bool) or not isinstance(version_from, int) \
            or version_from < 0:
        raise ValueError(f"{where} 的 version_from 必须是捕获组序号（1 起的"
                         f"整数，0/缺省 = 不带版本），收到 {version_from!r}")
    if version_from > compiled.groups:
        raise ValueError(
            f"{where} 的 version_from={version_from}，但 pattern 只有 "
            f"{compiled.groups} 个捕获组 —— 版本将永远取不到")

    return Rule(name=name, product=family, target=str(target),
                header=str(header or ""), pattern=pattern, compiled=compiled,
                version_from=int(version_from))


def rules_digest(rules_dir: Path | None = None) -> str:
    """规则集的内容指纹：`sha256:<hex>` —— 改任何规则必变，缺文件时**稳定地**
    退化成一个占位值。

    两件刻意的事：

    * **不解析 YAML** —— 没装 pyyaml 的环境里它照样算得出与装了时相同的值
      （definition_hash 不因环境而分叉）；
    * **换行归一**（`read_text` 的通用换行把 CRLF 折成 LF 再喂 sha256）——
      本机 `core.autocrlf=true`，同一份规则在不同检出里字节不同（LF vs
      CRLF），而 YAML 的语义不随换行变。不归一的话，"钉在夹具里的 digest"
      会**在每个新克隆里必然变红** —— 一条因为换行而红的断言训练人忽略红色。
      归一之后红只来自**内容**改动，那正是这条钉要抓的东西。

    拿不到文件（目录不存在 / 读失败 / 非 UTF-8）时返回 `unavailable(...)`
    而不是抛 —— `definition_extra` 的调用点（每次导入都要算 definition_hash）
    不该因为"这台机器没有规则"而炸掉一次本来能给出可读报告的导入。
    """
    try:
        files = iter_rule_files(rules_dir)
        if not files:
            return "unavailable(no-rule-files)"
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            # 通用换行读入（CRLF → LF），再回编码成 UTF-8 —— 见文件头的第二条。
            digest.update(path.read_text(encoding="utf-8").encode("utf-8"))
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()
    except (OSError, UnicodeDecodeError) as exc:
        return f"unavailable({type(exc).__name__})"


# ---------------------------------------------------------------------------
# 响应拆解
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exchange:
    """一份已落盘的请求-响应交换。**只装匹配需要的部件**，不是完整 HTTP 实现。"""

    #: 归一化后的方法（`normalize.http_method` 的输出，如 `GET`）。
    method: str
    #: 归一化后的目标 URL（绝对形式，或 scheme 合成后的）。
    url: str
    #: `service` 的身份键（`httpx.service_key` 的输出 —— 与 httpx / nmap 落同一个）。
    svc: str
    #: http | https（裁定见 `parse_exchange` 的文件头注释）。
    scheme: str
    #: 状态码（`"200"`）。
    status: str
    #: 响应头，`(原样的头名, 值)` 按文件顺序。
    headers: tuple[tuple[str, str], ...]
    #: 响应体（第二个空行之后的全部行，`\n` 连接；原始换行已不可考，匹配足够）。
    body: str


def request_line_head(line: str) -> tuple[str, str] | None:
    """首行是形如 `GET /path HTTP/1.1` 的请求行 → `(原样方法, 目标)`；否则 None。

    **嗅探与解析共用这一个函数**（`curl.sniff` 的文件头写过反例：判据分两份的
    下场是"嗅探很确定这是我的格式，解析却一行都读不出来"）。方法的合法性这里
    只查形状（三段、末段 `HTTP/` 开头），真正的归一化在 `parse_exchange` 里走
    `normalize.http_method` —— 单点归一化是 PARSE-NORM 的口径。
    """
    tokens = line.split()
    if len(tokens) != 3 or not tokens[2].startswith("HTTP/"):
        return None
    if not tokens[0] or not tokens[1]:
        return None
    return tokens[0], tokens[1]


def status_line_head(line: str) -> str | None:
    """状态行 `HTTP/1.1 200 OK` → 状态码 `"200"`；否则 None。同样两处共用。"""
    tokens = line.split()
    if len(tokens) >= 2 and tokens[0].startswith("HTTP/") \
            and tokens[1].isdigit() and len(tokens[1]) == 3:
        return tokens[1]
    return None


def _implicit_scheme(host_header: str) -> str:
    """origin-form 请求行下的 scheme 裁定：**Host 显式带 443 才判 https**。

    请求行是 origin-form（`GET / HTTP/1.1`）时，dump 里没有任何一处写着"这条
    连接走了 TLS" —— 而 scheme 决定无端口 Host 落成 `host:80` 还是 `host:443`，
    猜错就是一个**从未存在过的 service**。所以：

      * Host 带显式端口 → 端口进身份，scheme 不影响 `service_key` 的键
        （`effective` 取显式端口），这里取 443→https 只影响展示；
      * Host **不带**端口 → 这是真正的歧义，由 `parse_exchange` 报可读坏行
        拒绝（与 `flow` 的"两端都未知直接拒绝"、openapi 的"相对起源拒绝"
        同一条纪律：猜错的方向有两边，而错的那边没有任何信号）。
    """
    if host_header.rsplit(":", 1)[-1].strip() == "443":
        return "https"
    return "http"


def _host_has_port(host: str) -> bool:
    """Host 头里有没有**显式端口**（认得 `host:8080` 与 `[::1]:8443` 两种形状）。

    只做形状判断：`example.com` 无端口；`[::1]` 也没有（`]` 后面没跟 `:端口`）。
    """
    text = host.rsplit("@", 1)[-1].strip()
    if text.startswith("["):
        return "]:" in text
    return ":" in text


def _header(lines: Sequence[str], start: int, stop: int, what: str,
            ) -> tuple[tuple[int, str, str], ...]:
    """一段头 → `(行号, 头名, 值)`；缺冒号的行抛 `ValueError`（带行号）。"""
    out: list[tuple[int, str, str]] = []
    for offset in range(start, stop):
        line = lines[offset]
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise ValueError(offset + 1, f"{what}行缺少冒号：{stripped[:80]!r}")
        name, _, value = stripped.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(offset + 1, f"{what}行的头名为空：{stripped[:80]!r}")
        out.append((offset + 1, name, value.strip()))
    return tuple(out)


def parse_exchange(lines: Sequence[str]) -> tuple[Exchange | None,
                                                  tuple[int, str] | None]:
    """行列表 → `(交换, None)` 或 `(None, (行号, 可读理由))`。

    格式：**一个请求段 + 一个响应段**，段之间一个空行（HTTP/1.1 线格式的
    单次交换 —— 探针、代理导出、`curl -v` 清洗版落盘的都是这个形状）：

        GET https://app.example.com/admin HTTP/1.1
        Host: app.example.com

        HTTP/1.1 200 OK
        Server: nginx/1.24.0

        <body…>

    三条**拒绝**（都报可读坏行，不猜）：

      1. 以状态行开头（纯响应、没有请求段）→ 指不到服务，与 `flow` 的
         "两端都未知直接拒绝"同一条；
      2. origin-form 请求行 + Host 不带端口 → 80 还是 443 无从知道（见
         `_implicit_scheme`）；
      3. 请求行 / 状态行 / 头的形状不对 → 格式错误如实报。

    绝对形式的请求行（`GET https://… HTTP/1.1`，代理与不少导出工具的写法）
    优先于 Host 合成 —— URL 里什么都带，不需要任何裁定。
    """
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return None, None                       # 空文件：调用方按"没东西"处理

    first = lines[index].strip()
    first_lineno = index + 1

    status = status_line_head(first)
    if status is not None:
        return None, (first_lineno,
                      "这份文件以状态行开头，没有请求段 —— 没有请求行/Host 就"
                      "不知道这段响应属于哪个服务，而指错服务会把指纹挂到错误"
                      "的资产上。连请求一起保存（单次交换的线格式），或改用带"
                      "URL 的导出（HAR）")

    head = request_line_head(first)
    if head is None:
        return None, (first_lineno,
                      "第一行既不是请求行（`METHOD 目标 HTTP/1.x`）也不是状态行"
                      "（`HTTP/1.x 码`）—— 这不是一份已落盘的 HTTP 响应")
    raw_method, target = head

    # --- 请求段：请求行之后到第一个空行 ---
    request_end = index + 1
    while request_end < len(lines) and lines[request_end].strip():
        request_end += 1
    if request_end >= len(lines):
        return None, (first_lineno,
                      "请求段之后没有空行分隔 —— 响应段缺失（这是一份只保存了"
                      "请求的 dump，没有可匹配的响应头/体）")
    try:
        request_headers = _header(lines, index + 1, request_end, "请求头")
    except ValueError as exc:
        lineno, reason = exc.args[0], exc.args[1]
        return None, (int(lineno), str(reason))

    # --- 响应段：空行之后到下一个空行 ---
    status_index = request_end + 1
    if status_index >= len(lines):
        return None, (request_end + 1, "请求段之后就没有内容了 —— 响应段缺失")
    status = status_line_head(lines[status_index].strip())
    if status is None:
        return None, (status_index + 1,
                      "空行之后必须是状态行（`HTTP/1.x 码 文本`），收到 "
                      f"{lines[status_index].strip()[:80]!r}")
    response_end = status_index + 1
    while response_end < len(lines) and lines[response_end].strip():
        response_end += 1
    try:
        response_headers = _header(lines, status_index + 1, response_end,
                                   "响应头")
    except ValueError as exc:
        lineno, reason = exc.args[0], exc.args[1]
        return None, (int(lineno), str(reason))
    body = "\n".join(lines[response_end + 1:]) if response_end < len(lines) else ""

    # --- URL：绝对形式优先；否则 Host 合成 ---
    host = ""
    for _, name, value in request_headers:
        if name.lower() == "host":               # HTTP 头名大小写不敏感（语法本身）
            host = value
            break
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    elif host:
        if not _host_has_port(host):
            # origin-form + Host 无端口（连 `host:443` 都没有）→ 歧义，拒绝。
            # 判据写在 `_implicit_scheme` 的注释里：猜错会造出一个不存在的
            # service，而那种错没有任何信号。
            return None, (first_lineno,
                          "请求行是 origin-form（`GET /path`）而 Host 不带端口"
                          "（也没有绝对 URL）—— 无从判断这是 80 还是 443，指错"
                          "端口会造出一个从未存在过的 service。请保存成绝对形式"
                          "（`GET https://host/path HTTP/1.1`）或让 Host 带端口")
        scheme = _implicit_scheme(host)
        url = f"{scheme}://{host}" + (target if target.startswith("/")
                                      else "/" + target)
    else:
        return None, (first_lineno,
                      "请求行是 origin-form 而请求段里没有 Host 头 —— 无从确定"
                      "这段响应属于哪台主机")

    try:
        identity = normalize.endpoint_identity(url)
    except normalize.NormalizeError as exc:
        return None, (first_lineno, f"算不出 endpoint 身份：{exc}")

    method = normalize.http_method(raw_method)    # 归一化的唯一入口（PARSE-NORM）
    scheme = str(identity["scheme"])
    svc = service_key(str(identity["host"]), identity["port"], scheme)
    return Exchange(method=method, url=url, svc=svc, scheme=scheme, status=status,
                    headers=tuple((n, v) for _, n, v in response_headers),
                    body=body), None


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """一处命中。**可解释性全在这三个字段里**：哪条规则、在哪儿、依据什么。"""

    rule: Rule
    #: `"header:Server"` 或 `"body"` —— 命中发生在哪。
    where: str
    #: 依据（响应头是整行 `名: 值`，响应体是命中的那一段），封顶
    #: `MAX_EVIDENCE_CHARS`。
    evidence: str
    #: 版本（`""` = 证据里没有可解析的版本，product 不带版本）。
    version: str


def match_rules(exchange: Exchange, rules: Sequence[Rule]) -> list[Hit]:
    """一份交换 × 全部规则 → 命中列表（**规则声明顺序**，确定性）。

    每条规则**至多命中一次**（同一头名取首个匹配、响应体只搜一次）：同一份
    响应里 nginx 出现两次说的是同一件事，记两次只会让证据列表变长。
    """
    hits: list[Hit] = []
    for rule in rules:
        if rule.target == "header":
            hit = _match_header(exchange, rule)
        else:
            hit = _match_body(exchange, rule)
        if hit is not None:
            hits.append(hit)
    return hits


def _version_from(match: "re.Match[str]", rule: Rule) -> str:
    """捕获组 → 版本字符串。**取不到可解析的版本就不带版本**（不编一个）。"""
    if not rule.version_from:
        return ""
    try:
        raw = match.group(rule.version_from)
    except IndexError:                             # pragma: no cover —— 加载时已验
        return ""
    if not raw:
        return ""
    try:
        normalize.version_tuple(raw)
    except normalize.NormalizeError:
        return ""                  # 证据在，版本不在 —— 假版本比没有版本更糟
    return raw


def _match_header(exchange: Exchange, rule: Rule) -> Hit | None:
    wanted = rule.header.lower()                   # HTTP 头名大小写不敏感
    for name, value in exchange.headers:
        if name.lower() != wanted:
            continue
        found = rule.compiled.search(value)
        if found is None:
            continue
        evidence = f"{name}: {value}"[:MAX_EVIDENCE_CHARS]
        return Hit(rule=rule, where=f"header:{rule.header}", evidence=evidence,
                   version=_version_from(found, rule))
    return None


def _match_body(exchange: Exchange, rule: Rule) -> Hit | None:
    if not exchange.body:
        return None
    found = rule.compiled.search(exchange.body)
    if found is None:
        return None
    return Hit(rule=rule, where="body",
               evidence=found.group(0)[:MAX_EVIDENCE_CHARS],
               version=_version_from(found, rule))


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


def build_records(exchange: Exchange, hits: Sequence[Hit]) -> list:
    """命中 → 记录（`service` 实体 → 每条命中的 `product` + `runs` + 观测）。

    顺序是硬合同：**先实体后关系**（domainlist 文件头写透了为什么 —— 关系先落
    的话端点会被兜底 kind 建出来）。观测的 `source` = 规则名：`ingest` 的
    逐记录 source 接线（`RawObservation.source`）会按它 `register_source`，
    于是贡献的 `sources.name` 就是"哪条规则"—— 判据 2 的 `source='规则名'`
    是数据层的事实，不是展示层的拼接。

    零命中 → 空列表（一份没有产品痕迹的响应是**干净的**，不是坏行）。
    """
    if not hits:
        return []

    records: list = [RawEntity(kind="service", key=exchange.svc,
                               display=exchange.svc, subtitle=exchange.scheme)]
    seen_products: set[str] = set()
    for hit in hits:
        key = product_key(hit.rule.product, hit.version)
        if key not in seen_products:
            seen_products.add(key)
            records.append(RawEntity(
                kind="product", key=key, display=hit.rule.product,
                subtitle=hit.version or None,
                attrs={"family": hit.rule.product, "version": hit.version or None}))
            records.append(RawRelation(src=("service", exchange.svc),
                                       dst=("product", key), rel="runs"))
        records.append(RawObservation(
            entity_ref=("service", exchange.svc), type="fingerprint",
            value={"product": key, "rule": hit.rule.name, "where": hit.where,
                   "evidence": hit.evidence},
            source=hit.rule.name))
    return records
