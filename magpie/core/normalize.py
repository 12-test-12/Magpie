"""归一化唯一入口（开发实施计划 2.3）。

**这个模块存在的理由是一条顺序纪律**：解析器落地**之前**先立好"同一件事只有一种
写法"的唯一入口。如果让每个解析器自己归一化，等第二个来源进来时就会出现
`Nginx` / `nginx` / `NGINX/1.24` 三种写法同时入库 —— 那正是计划书 §3.6 要防的混乱。

所以 2.4 的每一个解析器**只调用**这里，不得内联自己的归一化（由 `check.py` 的
`PARSE-NORM` 规则静态守着，见 `check.py` 文件头）。

职责边界（同样是被断言守着的）
--------------------------------
归一化只回答"**怎么写才唯一**"，**不回答"这是什么"**：

* 不做 kind 决策 —— "这行是域名还是 IP"是解析器的嗅探与 identity rule 的事；
* 不碰数据库、不 import 任何写入口、无全局状态 —— 全是纯函数；
* 只 import 标准库（红线 R6）。

错误语义（刻意选"响"而不是"静"）
--------------------------------
超长域名 / 空串 / 只有点 / 全角字符 / NUL 字节一律抛 `NormalizeError`，
**绝不静默放行**。静默放行的代价是：一个畸形的 key 进了库，从此每次导入都
多一个幽灵实体，而且没人知道它从哪来。

解析器要给"这行是不是合法域名"这种**谓词**问题时用 `try_domain` / `try_ip`
这类 `try_*` 变体 —— 它们把异常折成 `None`，因为"不符合"在解析器里是常态，
不是错误。
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata

__all__ = [
    "NormalizeError",
    "SEGMENT_TEMPLATES",
    "WEB_DEFAULT_PORTS",
    "apischema_identity",
    "apischema_key",
    "compare_version",
    "endpoint_base",
    "endpoint_identity",
    "endpoint_key",
    "http_method",
    "is_absolute_url",
    "param_name",
    "normalize_cidr",
    "normalize_domain",
    "normalize_ip",
    "normalize_template_id",
    "normalize_vuln_target",
    "path_template",
    "prerelease_tuple",
    "product_name",
    "try_cidr",
    "try_domain",
    "try_ip",
    "version_tuple",
    "vuln_identity",
    "vuln_key",
    "vuln_target_parts",
]


class NormalizeError(ValueError):
    """归一化失败。**必须是可读的** —— 它会被写进坏行报告给人看，而不是堆栈。"""


def _require_no_homoglyphs(text: str, what: str) -> None:
    """全角/兼容字符（同形字）一律拒绝 —— 域名 / 路径 / 产品名共用这一道守卫。

    判定用 NFKC ≠ NFC：兼容字符（全角 `ｅｘａｍｐｌｅ`、连字 `ﬁ`）在这里现形，
    而真正的 CJK / IDN 字符不受影响（`例子.中国` 该走 punycode 照走）。

    为什么必须拒绝而不是顺手 NFKC 折叠：折叠会让两个**视觉相同**的输入变成
    同一个 key。同形字是钓鱼与数据投毒的经典载体，在这个库里表现为"某个攻击者
    登记的资产和真实资产合并成了一个实体"。宁可让人显式写成 ASCII / punycode。

    ⚠️ 这道守卫看不见 NFKC 稳定的非 ASCII 数字（阿拉伯-印度数字 `٣`），
    所以数字**正则也必须是 ASCII 的**（`[0-9]` 而不是 `\\d`）—— 两者缺一不可。
    """
    if unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text):
        raise NormalizeError(f"{what}含全角/兼容字符（疑似同形字）：{text!r}")


# ---------------------------------------------------------------------------
# 域名
# ---------------------------------------------------------------------------

MAX_DOMAIN_LENGTH = 253
MAX_LABEL_LENGTH = 63

#: 标签：字母数字下划线开头结尾，中间允许连字符。故意**不收**下划线开头的写法
#: 之外的宽松形态；`_dmarc.example.com` 这类下划线前缀是真实存在的，所以保留 `_`。
_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")

#: 出现在整行域名里的这些字符说明"这不是一个主机名"，而是一段文本/URL/JSON。
#: 解析器**绝不去文本里提取域名** —— 一旦允许提取，`{"url":"https://a/"}`
#: 也会被判成域名，domainlist 与 httpx 就会同时高分（嗅探仲裁因此失效）。
_HOSTILE_CHARS = "/\\?#@\"'{}[],<>=&%*|;"


def normalize_domain(raw: object, *, require_dot: bool = True) -> str:
    """域名归一化：小写、去尾点、去端口、IDN → punycode、长度与标签规则校验。

    `require_dot=True`（默认）拒绝单标签（`localhost`、`intranet`）。这是一条
    **身份规则**而不是语法规则：单标签在资产清单里几乎总是噪声或误报，
    而它们一旦成实体就会污染"资产"镜头。需要收录单标签时显式传
    `require_dot=False`，别改默认值。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NormalizeError(f"域名不是合法 UTF-8：{exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise NormalizeError(f"域名必须是字符串，收到 {type(raw).__name__}")

    if "\x00" in text:
        raise NormalizeError("域名含 NUL 字节")
    text = text.strip()
    if not text:
        raise NormalizeError("空域名")
    if any(ch.isspace() for ch in text):
        raise NormalizeError(f"域名含空白字符：{text!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise NormalizeError("域名含控制字符")

    _require_no_homoglyphs(text, "域名")

    bad = next((ch for ch in text if ch in _HOSTILE_CHARS), None)
    if bad is not None:
        raise NormalizeError(f"域名含不允许的字符 {bad!r}（不是纯主机名）：{text!r}")
    if text.startswith("["):
        raise NormalizeError("IPv6 字面量不是域名")

    text = _strip_port(text)
    text = text.rstrip(".")          # 尾点在 DNS 里表示绝对名，身份上无意义
    if not text:
        raise NormalizeError("域名只有点")
    if text.startswith(".") or ".." in text:
        raise NormalizeError(f"域名含空标签：{text!r}")

    source = text                    # punycode 之前的形态，只用于错误信息
    text = _to_ascii(text).lower()
    # 非 ASCII 输入报错时补上原形：`example.com:٣٤` 的标签会变成 `xn--com:-ofgg`，
    # 只给 punycode 的话，读坏行报告的人没法拿它去 grep 原始文件。
    origin = f"（原始输入 {source!r}）" if not source.isascii() else ""

    if len(text) > MAX_DOMAIN_LENGTH:
        raise NormalizeError(f"域名超过 {MAX_DOMAIN_LENGTH} 字节：{len(text)}{origin}")
    if require_dot and "." not in text:
        raise NormalizeError(f"单标签不是完整域名（require_dot=True）：{text!r}{origin}")
    for label in text.split("."):
        if not label:
            raise NormalizeError(f"域名含空标签：{text!r}{origin}")
        if len(label) > MAX_LABEL_LENGTH:
            raise NormalizeError(f"标签超过 {MAX_LABEL_LENGTH} 字节：{label!r}{origin}")
        if not _LABEL_RE.match(label):
            raise NormalizeError(f"标签不合法（连字符位置 / 字符集）：{label!r}{origin}")
    return text


def _strip_port(text: str) -> str:
    """去掉 `host:port`。只认"冒号右边全是 **ASCII** 数字"这一种，避免误伤 IPv6。

    `isascii()` 不是多余的：`str.isdigit()` 是 Unicode 感知的，`int("٣") == 3`，
    于是 `example.com:٣٤` 会被当成"带端口的域名"**静默**剥掉一段谁也不认识的
    东西。剥不掉时它留在标签里，由字符集校验报一条可读的错误。
    """
    head, sep, tail = text.rpartition(":")
    if sep and head and tail.isascii() and tail.isdigit() and ":" not in head:
        return head
    return text


def _to_ascii(text: str) -> str:
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        try:
            return text.encode("idna").decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise NormalizeError(f"IDN 转 punycode 失败：{text!r}") from exc
    return text


def try_domain(raw: object, **kw: object) -> str | None:
    """`normalize_domain` 的谓词变体：不符合返回 `None`，不抛。"""
    try:
        return normalize_domain(raw, **kw)          # type: ignore[arg-type]
    except NormalizeError:
        return None


# ---------------------------------------------------------------------------
# IP / 网段
# ---------------------------------------------------------------------------


def normalize_ip(raw: object) -> str:
    """IP 归一化。IPv4-mapped IPv6（`::ffff:1.2.3.4`）折成 IPv4 —— 它们指的是同一台机器。

    带掩码的（`1.2.3.4/24`）走 `normalize_cidr`，这里拒绝：一个 IP 和一个网段
    是两种身份，混在一起会让"哪台机器"这个问题的答案悄悄变成"哪一段"。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"IP 必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("空 IP")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise NormalizeError(f"不是合法 IP：{text!r}（{exc}）") from exc
    return _unmap(addr)


def _unmap(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def normalize_cidr(raw: object, *, strict: bool = False) -> str:
    """网段归一化：主机位补齐（`10.0.0.7/24` → `10.0.0.0/24`）。

    `strict=True` 时主机位不补齐而是报错 —— 用于"写错了要立刻知道"的场合。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"网段必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("空网段")
    if "/" not in text:
        raise NormalizeError(f"网段必须带掩码（`1.2.3.0/24`）：{text!r}")
    try:
        net = ipaddress.ip_network(text, strict=strict)
    except ValueError as exc:
        raise NormalizeError(f"不是合法网段：{text!r}（{exc}）") from exc
    return f"{_unmap(net.network_address)}/{net.prefixlen}"


def try_ip(raw: object) -> str | None:
    try:
        return normalize_ip(raw)
    except NormalizeError:
        return None


def try_cidr(raw: object, **kw: object) -> str | None:
    try:
        return normalize_cidr(raw, **kw)            # type: ignore[arg-type]
    except NormalizeError:
        return None


# ---------------------------------------------------------------------------
# 路径模板
# ---------------------------------------------------------------------------

#: 会**变**的路径段。顺序即优先级：先判最能确定的（UUID 比纯数字更具体）。
#: 数字类一律写成 `[0-9]` 而**不是** `\d` —— `\d` 在 str 上是 Unicode 感知的，
#: 全角 `１２３` 与阿拉伯-印度数字 `٣٤٥` 都算 `\d`，会被静默折成 `{id}`，
#: 于是两条不同的路径变成一条。ASCII 之外的东西**保持字面量**（它可能是真实
#: 路由的一部分），全角那部分则由 `_require_no_homoglyphs` 提前拦掉。
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_DATE_COMPACT_RE = re.compile(r"^[0-9]{8}$")
_ISODATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}(-[0-9]{2})?$")
_NUMERIC_RE = re.compile(r"^[0-9]{1,20}$")

#: `/user/123` 与 `/user/abc` **不得**折叠成同一模板 —— 前者是 id，后者是字面量
#: 路径（往往是真实路由）。这条规则就是那件事的落点：只有**匹配到的**段被替换。


#: **段折叠的常量表 —— 这一张表就是折叠规则的全部内容。**
#:
#: 表是**有序的**，顺序即优先级，四条纪律都写在这里：
#:
#: 1. **UUID / 日期排在纯数字前面**。`20260101` 同时匹配 `[0-9]{8}`（日期）与
#:    `[0-9]{1,20}`（id）；顺序反了它会被折成 `{id}`，"这是个日期"这件事就没了。
#: 2. **纯数字排在 hex 前面**。`1234567890123456` 是"一个很长的 id"，不是指纹；
#:    反过来把 id 折成 `{hash}` 会让指纹镜头把每条记录都当成新指纹。
#: 3. **没匹配到的段一律原样保留**。`v1` / `abc` / `logo.png` / `2024-13-99`
#:    都是字面量 —— 折叠少了只是行数多，**折叠多了是静默丢信息**（接口镜头里
#:    两条不同的接口并成一条，而没有任何信号）。
#: 4. 正则里的数字一律写 `[0-9]` 而**不是** `\d`（`\d` 是 Unicode 感知的，
#:    全角 `１２３` 与阿拉伯-印度数字 `٣٤٥` 都会被折成 `{id}`）。
#:
#: ⚠️ 这张表**故意**不含"文件名段"（`logo.png`）、"查询串"、"多段路径"这几类：
#: 它们要么无法与真实路由区分（`.json` 结尾的路径真实存在），要么本来就该留在
#: 证据层（查询串，见 `endpoint_identity` 的裁定）。
SEGMENT_TEMPLATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("{uuid}", _UUID_RE),
    ("{date}", _DATE_COMPACT_RE),
    ("{date}", _ISODATE_RE),
    ("{id}", _NUMERIC_RE),
    ("{hash}", _HEX_RE),
)


def path_template(path: object, *, keep_query: bool = False) -> str:
    """`/user/123/profile` → `/user/{id}/profile`。

    识别：纯数字 → `{id}`；UUID → `{uuid}`；≥16 位 hex → `{hash}`；
    `20260101` / `2026-01-01` → `{date}`；纯字母段**原样保留**。

    查询串与片段默认丢弃（它们通常带 token / 分页参数，折叠不了也不该折叠）。
    """
    if not isinstance(path, str):
        raise NormalizeError(f"路径必须是字符串，收到 {type(path).__name__}")
    text = path.strip()
    if not text:
        raise NormalizeError("空路径")
    if "\x00" in text:
        raise NormalizeError("路径含 NUL 字节")
    if not text.startswith("/"):
        raise NormalizeError(f"路径必须以 / 开头：{text!r}")
    _require_no_homoglyphs(text, "路径")

    if not keep_query:
        text = text.split("#", 1)[0].split("?", 1)[0]

    leading = text.startswith("/")
    parts = [p for p in text.split("/")]
    out: list[str] = []
    for index, segment in enumerate(parts):
        if index == 0 and leading:
            continue
        out.append(_fold_segment(segment))
    return "/" + "/".join(out)


def _fold_segment(segment: str) -> str:
    """按 `SEGMENT_TEMPLATES` 逐条试；**没匹配到就原样返回**（表里有理由）。"""
    if not segment:
        return segment
    for replacement, pattern in SEGMENT_TEMPLATES:
        if pattern.match(segment):
            return replacement
    return segment


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

#: `1.24.0-rc1+build7` → release `1.24.0`、prerelease `rc1`、build `build7`
_VERSION_RE = re.compile(
    r"^[vV]?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[-_.]?(?P<pre>alpha|beta|rc|pre|preview|dev)[-_.]?([0-9]*))?"
    r"(?:\+(?P<build>[0-9A-Za-z.\-]+))?$")

#: 预发布的排序权重（SemVer §11）：数字段先于一切，`alpha < beta < rc < 正式版`。
#: 没有预发布标记的版本**大于**任何带标记的同 release 版本（1.0.0 > 1.0.0-rc1）。
_PRERELEASE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "pre": 3, "preview": 3, "rc": 4}
_NO_PRERELEASE_RANK = 99


def version_tuple(text: object) -> tuple[int, ...]:
    """`1.24` → `(1, 24, 0)`（**短的补齐到三位**）。**预发布不在这一维**。

    补齐是**加法，不是截断**：短的补零，长的**原样保留**。`1.2.3.4` 截成
    `1.2.3` 会让两个不同的版本得到同一个元组 —— 那不是"少显示一段"，是把
    它们合并成同一个实体，而且没有任何信号。nginx 的 `1.24.0.1` 就是四段。

    返回的是纯数字元组，可以直接 `sorted()`，但**长度不同的两个版本不能直接比**：
    `(1,) < (1, 0)` 在 Python 里为真，语义上却相等。要比大小请用
    `compare_version` —— 它按位补齐，并单独处理预发布维度。
    """
    release, _, _ = _split_version(text)
    if len(release) < 3:
        release = release + (0,) * (3 - len(release))
    return release


def prerelease_tuple(text: object) -> tuple[int, int]:
    """预发布**单独一维**：`(权重, 序号)`。没有预发布标记 → `(99, 0)`。

    为什么必须单独一维：把它折进 release 元组会让 `1.0.0-rc1` 与 `1.0.0.1`
    无法区分 —— 前者是同一版本的前置，后者是下一个版本。
    """
    _, pre, _ = _split_version(text)
    if pre is None:
        return (_NO_PRERELEASE_RANK, 0)
    name, number = pre
    return (_PRERELEASE_RANK.get(name, 5), number)


def compare_version(a: object, b: object) -> int:
    """版本比较：`-1` / `0` / `1`。**禁止字符串比较版本**（`"1.9" > "1.10"` 为真）。

    `1.9 < 1.10`、`1.0.0-rc1 < 1.0.0`、`1.0 < 1.0.0`（相等）都由它保证。
    """
    rel_a, _, _ = _split_version(a)
    rel_b, _, _ = _split_version(b)
    width = max(len(rel_a), len(rel_b))
    pad_a = list(rel_a) + [0] * (width - len(rel_a))
    pad_b = list(rel_b) + [0] * (width - len(rel_b))
    if pad_a != pad_b:
        return -1 if pad_a < pad_b else 1
    pre_a = prerelease_tuple(a)
    pre_b = prerelease_tuple(b)
    if pre_a == pre_b:
        return 0
    return -1 if pre_a < pre_b else 1


def _split_version(text: object) -> tuple[tuple[int, ...], tuple[str, int] | None, str]:
    if not isinstance(text, str):
        raise NormalizeError(f"版本必须是字符串，收到 {type(text).__name__}")
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise NormalizeError(f"不是可解析的版本号：{text!r}")
    release = tuple(int(p) for p in match.group("release").split("."))
    pre = match.group("pre")
    parsed_pre = None if pre is None else (pre.lower(), int(match.group(3) or 0))
    return release, parsed_pre, match.group("build") or ""


# ---------------------------------------------------------------------------
# 产品名
# ---------------------------------------------------------------------------

#: 厂商前缀 / 常见叫法的别名表。**只收真的会撞上的那些** —— 一份穷尽清单会被跳过，
#: 短清单才会被认真读完并继续往里加。
_PRODUCT_ALIASES = {
    "apache": "httpd",
    "apache httpd": "httpd",
    "apache http server": "httpd",
    "httpd": "httpd",
    "apache2": "httpd",
    "iis": "iis",
    "microsoft-iis": "iis",
    "microsoft iis": "iis",
    "internet information services": "iis",
    "open ssh": "openssh",
    "openssh": "openssh",
    "openresty": "openresty",
    "nginx": "nginx",
    "vue": "vue",
    "vue.js": "vue",
    "vuejs": "vue",
    "jquery": "jquery",
    "jquery ui": "jquery-ui",
    "laravel": "laravel",
    "php": "php",
    "tomcat": "tomcat",
    "apache tomcat": "tomcat",
    "jetty": "jetty",
    "wordpress": "wordpress",
    "spring boot": "spring-boot",
}

_PRODUCT_NOISE_RE = re.compile(r"\s+")


def product_name(raw: object) -> str:
    """产品名归一化：小写、折叠空白、去厂商前缀、别名表。

    `Apache httpd` / `APACHE` / `apache2` → `httpd`。

    **版本不在这里** —— `nginx/1.24.0` 的拆分是调用方的事（先按 `version_tuple`
    切出数字段，再把名字喂进来）。归一化只回答"这个名字的唯一写法"。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"产品名必须是字符串，收到 {type(raw).__name__}")
    text = _PRODUCT_NOISE_RE.sub(" ", raw.strip())
    if not text:
        raise NormalizeError("空产品名")
    if "\x00" in text:
        raise NormalizeError("产品名含 NUL 字节")
    # 守卫在 `.lower()` **之前**：`ＮＧＩＮＸ` 小写后是 `ｎｇｉｎｘ`，一个永远
    # 撞不上 `nginx` 的幽灵产品 —— 别名表救不了它，只有拒绝能。
    _require_no_homoglyphs(text, "产品名")
    text = text.lower()
    return _PRODUCT_ALIASES.get(text, text)


# ---------------------------------------------------------------------------
# 漏洞（vuln）· 3.2 nuclei
# ---------------------------------------------------------------------------
#
# 这一节是 `vuln` 这个 kind 的**身份规则本身**（登记在
# `kind_registry.identity_rule` 里），所以它必须住在 `normalize.py` ——
# 解析器只调用它，不得内联自己的一套（`check.py` 的 PARSE-NORM 守着这条）。
#
# 一个漏洞的身份由**两件事**共同决定：
#
#     (template-id, 规范化之后的目标)   →  "template-id@目标"
#
# 为什么**不能**只用 template-id：那样"这个模板命中了几个地方"就答不出来了，
# 而唯一的补救办法是给实体挂一个计数器 —— 那正是本步点名要防的写法
# （计数器绕开 contributions 模型，`undo` 之后会留下幻影行，而且没有测试会
# 发现）。同一个模板命中两个不同的资产是**两件事**，它们该是两个实体。
#
# 为什么**不需要**计数器：同一个模板命中同一个目标两次是**同一件事** ——
# 两次算出同一个 `(kind, key)`，落库时第二条记录的 facts 完全重复，管线的
# `seen` 集合把它记成 `skipped`（一条贡献都不写）。所以"同模板重复命中不重复
# 计数"是这条身份规则的**推论**，不是另造的一个机制。

#: scheme → 默认端口。只用于**补齐与折叠**：`https://a/` 与 `https://a:443/`
#: 说的是同一个目标，不统一就会裂成两个实体。表里没有的 scheme（`nuclei` 的
#: 模板类型不止 http）一律原样保留端口 —— 编一个默认端口出来比留空更糟。
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}

#: scheme 的合法字符集（RFC 3986）。刻意**不做**已知 scheme 的白名单：
#: 拒绝一份真实数据比放过一个陌生的 scheme 贵得多。
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789+-.")


def normalize_template_id(raw: object) -> str:
    """nuclei 的 `template-id` → 身份里用的那一份。**原样保留，只做形状校验**。

    刻意不做大小写折叠：`template-id` 是 nuclei 自己给的标识符（`CVE-2021-44228`、
    `tech-detect`），它是模板文件的身份，不是一段人类文本。折叠它等于替工具
    决定"这两个模板是同一个"，而那件事没有依据。

    `@` 被拒绝：它是身份串（`template-id@目标`）的分隔符，出现在模板名里会让
    两个不同的漏洞算出同一个 key —— 那是**静默**的实体合并（两个漏洞变成一个，
    谁也不会发现）。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"template-id 必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("空 template-id（没有它就没有漏洞的身份）")
    if "\x00" in text:
        raise NormalizeError("template-id 含 NUL 字节")
    if any(ch.isspace() for ch in text):
        raise NormalizeError(f"template-id 含空白字符：{text!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise NormalizeError("template-id 含控制字符")
    _require_no_homoglyphs(text, "template-id")
    if "@" in text:
        raise NormalizeError(
            f"template-id 含 `@`：它是身份串 `template-id@目标` 的分隔符，"
            f"出现在里面会让两个不同的漏洞算出同一个 key：{text!r}")
    return text


def vuln_target_parts(raw: object) -> dict[str, object]:
    """nuclei 的 `matched-at` / `url` / `host` → 漏洞目标的规范五件套。

    两种形状都要认，而且**规范化之后仍然是两种形状**：

        https://app.example.com/admin?x=1  →  https://app.example.com:443/admin
        203.0.113.10:3389                  →  203.0.113.10:3389
        app.example.com                    →  app.example.com

    为什么不统一补成一个 URL：`matched-at` 是模板自己写的，http 模板给完整 URL，
    network / dns 模板给 `host:port` 或裸主机名。给后两者补一个 `http://` 是
    **编造**（nuclei 没说那个端口上跑 HTTP），而编出来的 scheme 会跟着漏洞的
    身份走一辈子。所以有 scheme 就规范化它，没有就**不添**。

    丢弃的三样东西，每一样都有具体代价：

    * **查询串与片段** —— 它们常常带一次性 token。留着会让同一个漏洞每次扫描
      都算成一个新的（"同模板重复命中不重复计数"当场失效）；
    * **userinfo（`user:pass@`）** —— 它是凭据。计划书 §4.5 的硬规则是明文凭据
      永不落库，而身份串是要落库的；
    * **默认端口** —— `https://a:443/` 与 `https://a/` 是同一个目标。统一之后
      **总是写出显式端口**（`https://a:443/`），免得"省略"与"写出来"又变成
      两种写法（这正是 `Nginx` / `nginx` 那类混乱的同一形状）。

    大小写：scheme 与主机名折小写（DNS 不区分大小写）；**路径原样保留** ——
    路径区分大小写，`/Admin` 与 `/admin` 是两个真实路由。

    host 在返回值里**不带方括号**（`2001:db8::1`，不是 `[2001:db8::1]`）：
    调用方要拿它去算 `service` 的 key，而方括号是 URL 正文的书写形式，
    不是主机名的写法。只有拼回 `target` 时才加回去。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"漏洞目标必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("空漏洞目标（matched-at）")
    if "\x00" in text:
        raise NormalizeError("漏洞目标含 NUL 字节")
    if any(ch.isspace() for ch in text):
        raise NormalizeError(f"漏洞目标含空白字符：{text!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise NormalizeError("漏洞目标含控制字符")
    _require_no_homoglyphs(text, "漏洞目标")

    scheme = ""
    rest = text
    if "://" in text:
        scheme, _, rest = text.partition("://")
        scheme = scheme.lower()
        if not scheme or not all(ch in _SCHEME_CHARS for ch in scheme):
            raise NormalizeError(f"漏洞目标的 scheme 不合法：{text!r}")
        if scheme[0].isdigit():
            raise NormalizeError(f"漏洞目标的 scheme 不合法（以数字开头）：{text!r}")

    # 查询串与片段先丢：`#` 在路径里没有别的含义，而 `?` 之后全是参数。
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    authority, slash, tail = rest.partition("/")
    path = "/" + tail if slash else "/"
    if "@" in authority:                     # userinfo：凭据，不进身份
        authority = authority.rsplit("@", 1)[1]
    raw_host, raw_port = _split_authority(authority)
    host = _normalize_target_host(raw_host)

    port: int | None = None
    if raw_port:
        if not raw_port.isascii() or not raw_port.isdigit():
            raise NormalizeError(f"漏洞目标的端口不是数字：{raw_port!r}")
        port = int(raw_port)
        if not 0 < port <= 65535:
            raise NormalizeError(f"漏洞目标的端口越界：{port}")

    if scheme:
        default = _DEFAULT_PORTS.get(scheme)
        if port is None:
            port = default                    # 陌生 scheme 时仍是 None：不编端口
        target = f"{scheme}://{_bracket(host)}" + (f":{port}" if port else "") + path
    else:
        target = host + (f":{port}" if port else "")

    return {"target": target, "scheme": scheme, "host": host, "port": port,
            "path": path, "host_kind": "ip" if try_ip(host) is not None else "domain"}


def normalize_vuln_target(raw: object) -> str:
    """`vuln_target_parts` 的字符串面 —— 身份串与观测值用的都是它。"""
    return str(vuln_target_parts(raw)["target"])


def vuln_key(template_id: object, target: object) -> str:
    """**`vuln` 的身份规则本身**：`template-id@规范化目标`。

    公开函数而不是内联表达式，理由与 `httpx.service_key` 相同：
    `fixtures/kinds/vuln.json` 要能**执行**它来证明身份规则不是一句空话。

    目标传规范写法或原始 `matched-at` 都可以 —— `vuln_target_parts` 是幂等的
    （`https://a:443/` 再规范化一遍还是它自己）。
    """
    return str(vuln_identity(template_id, target)["key"])


def vuln_identity(template_id: object, target: object) -> dict[str, object]:
    """身份与目标一起算：`{"key": …, "parts": …}`。

    存在的理由只有一个但是很硬：解析器**既**要 key（挂实体）**又**要 parts
    （把资产挂到 service / domain / ip 上），而分两次调就会把同一个目标规范化
    **两遍**。那是纯浪费 —— 10 万行的一份 `-jsonl` 就是 20 万次域名归一化，
    而解析是整条管线里唯一按行计费的环节（§9.2 ⑤ 的预算）。

    拆成"key 函数 + parts 函数"再在调用点拼 key 是不行的：那会把身份串的
    拼法搬到解析器里，正是 PARSE-NORM 要防的"各写一套"。
    """
    parts = vuln_target_parts(target)
    key = f"{normalize_template_id(template_id)}@{parts['target']}"
    return {"key": key, "parts": parts}


def _bracket(host: str) -> str:
    """IPv6 拼回 URL 时加方括号；域名加了反而是错的。"""
    return f"[{host}]" if ":" in host else host


def _split_authority(authority: str) -> tuple[str, str]:
    """`host[:port]` → `(host, port)`。**只认 ASCII 数字的端口**（同 `_strip_port`）。

    IPv6 字面量必须写成 `[::1]:443`：不先切方括号的话，`rpartition(":")` 会把
    `::1` 的最后一个冒号当成端口分隔符，于是 `2001:db8::1` 变成一个主机
    `2001:db8:` 加一个端口 `1` —— 一个谁也不会发现它错了的目标。
    """
    if not authority:
        # 措辞对两个调用方都成立：vuln 的 `matched-at` 与 endpoint 的 URL 都会
        # 走到这里，"`https://` 后面什么都没有"是同一件事。
        raise NormalizeError("目标里没有主机名（`://` 后面是空的）")
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise NormalizeError(f"IPv6 字面量没有闭合的 `]`：{authority!r}")
        host, tail = authority[:end + 1], authority[end + 1:]
        if not tail:
            return host, ""
        if tail.startswith(":"):
            return host, tail[1:]
        raise NormalizeError(f"主机名后面有多余字符：{authority!r}")
    head, sep, last = authority.rpartition(":")
    if sep and last.isascii() and last.isdigit():
        return head, last
    return authority, ""


def _normalize_target_host(raw: str) -> str:
    """目标主机 → IP（走 `normalize_ip`）或域名（走 `normalize_domain`）。

    单标签主机（`nas`）默认被 `normalize_domain` 拒绝 —— 与 `domain` 这个 kind
    的身份规则是同一条（单标签在资产清单里几乎总是噪声或误报）。这里**不**
    放宽：nuclei 的目标要挂到资产上，而那个资产自己也得能成为实体。
    """
    text = raw.strip()
    if not text:
        raise NormalizeError("目标里没有主机名（host 是空的）")
    if text.startswith("[") and text.endswith("]"):
        return normalize_ip(text[1:-1])
    ip = try_ip(text)
    if ip is not None:
        return ip
    return normalize_domain(text)


# ---------------------------------------------------------------------------
# 接口（endpoint）· 3.3 dirsearch / katana / gau / ffuf
# ---------------------------------------------------------------------------
#
# 这一节是 `endpoint` 这个 kind 的**身份规则本身**（登记在
# `kind_registry.identity_rule` 里），所以它必须住在 `normalize.py` ——
# 解析器只调用它（`check.py` 的 PARSE-NORM 守着这条）。
#
#     endpoint 身份 = 主机[:非默认端口] + 路径模板
#
# ---------------------------------------------------------------------------
# 裁定一：查询串与片段**不进**身份，但原始串必须留证据
# ---------------------------------------------------------------------------
# `/user/123?tab=posts#top` 的 key 是 `/user/{id}`，`?tab=posts#top` 不算 key 的
# 一部分。**这是一个裁定，不是默认值**，理由两条：
#
#   * 查询串常带一次性 token / 分页 / 时间戳。进身份的话，同一个接口每抓一次就
#     多一个实体，接口镜头当场爆掉（这正是路径模板折叠要防的那件事的另一面）；
#   * 但"这个 endpoint 见过哪些真实路径"是**证据**，3.5 的镜头与 3.6 的体检都要
#     用它 —— 所以原始串（含查询与片段）由解析器原样写进 `path` 观测。
#
# 折叠掉的只是**身份**，不是**事实**。
#
# ---------------------------------------------------------------------------
# 裁定二：scheme 不进身份（数据模型修订稿 v3.1 §4.4 写死的）
# ---------------------------------------------------------------------------
# `http://x/a` 与 `https://x/a` 是同一个接口。身份里带上 scheme，同一个接口就会
# 因为换过一次协议而分裂成两个实体。
#
# ⚠️ 默认端口同理，而且这里有一个**必须写下来的坑**：不能按 scheme 取默认端口
# （`http→80` / `https→443`）。那样算的话 `http://a:80/x` 折成 `a/x` 而
# `https://a:80/x` 保留成 `a:80/x` —— scheme 又从后门进了身份。
# 所以折叠的是 `WEB_DEFAULT_PORTS` = {80, 443} 这**两个"默认 web 端口"**，
# 与 scheme 无关：省略、写 80、写 443 在身份上是同一件事。
# 8080 / 8443 / 3389 这些**保留** —— 它们确实区分服务。
#
# ---------------------------------------------------------------------------
# 裁定三：折叠是**保守**的，宁可少折
# ---------------------------------------------------------------------------
# 具体哪些段算模板段见 `SEGMENT_TEMPLATES` 的常量表。两条被点名守着的反例：
#
#   * `/user/abc` **不折** —— 不是所有段都是占位符，`abc` 往往是一条真实路由；
#   * `/api/v1/users` 与 `/api/v2/users` **不折** —— `v1` / `v2` 不是纯数字，
#     表里没有一条匹配它们。折掉版本号等于把"两代接口"这件事抹掉，而"哪些还在
#     用 v1"正是这份工具存在的理由之一。

#: 在 endpoint 身份里被折叠掉的端口。**刻意与 scheme 无关** —— 见上面裁定二。
#: 这两个是"默认 web 端口"：写成它、或省略，说的是同一个接口。
WEB_DEFAULT_PORTS: frozenset[int] = frozenset({80, 443})


def endpoint_identity(raw: object, *, base: object | None = None) -> dict[str, object]:
    """一条 URL 或一条相对路径 + base → endpoint 的规范身份与全部部件。

    返回 `{"key", "scheme", "host", "host_kind", "port", "path", "raw_path",
    "template"}`：

    * `key` —— **身份**：`主机[:非默认端口] + 路径模板`（如
      `app.example.com/user/{id}`）。观测与关系挂的都是它；
    * `host` —— 不含方括号的 IP / 域名（调用方要拿它去算 `service` 的 key）；
    * `port` —— **有效端口**（显式端口缺失时按 scheme 补齐，补齐表见
      `_DEFAULT_PORTS`）。它与 `key` 里那个端口**不是一回事**：key 用的是
      "非默认才写出来"的那一个，而 `port` 要给 `httpx.service_key` 用，必须与
      httpx / nmap / nuclei 算出同一个 service；
    * `path` —— 去掉查询串与片段之后的路径（身份这一侧）；
    * `raw_path` —— **原样**的路径 + 查询串 + 片段（证据这一侧，`?`/`#` 都还在）；
    * `template` —— `path_template(path)` 的结果，单独给出来是因为 3.5 的镜头页
      与 3.6 的体检都要按"模板"分组，让它们不必再算一遍。

    `base` 只给**相对路径**用（dirsearch 的 `Target:` 是唯一的真实场景）：传进来的
    相对路径必须以 `/` 开头，且 base 的路径必须是根 —— 否则报错而不是猜。
    "把 `/admin` 拼到 `http://a/sub/` 后面"这件事有多种合理答案，猜错的代价是
    一整个子树的 endpoint 全部落到错误的 key 上，而且不会有任何信号。
    """
    text = _clean_endpoint_text(raw)
    if "://" in text:
        scheme, rest = text.split("://", 1)
        scheme = scheme.lower()
        _require_scheme(scheme, text)
        authority, slash, tail = rest.partition("/")
        raw_path = "/" + tail if slash else "/"
    else:
        if not text.startswith("/"):
            raise NormalizeError(f"endpoint 不是 URL，也不是以 / 开头的路径：{text!r}")
        scheme, authority = _split_base(base)
        raw_path = text

    if "@" in authority:                     # userinfo：凭据，不进身份（§4.5）
        authority = authority.rsplit("@", 1)[1]
    raw_host, raw_port = _split_authority(authority)
    host = _normalize_target_host(raw_host)
    port = _port_or_none(raw_port)

    effective = port if port is not None else _DEFAULT_PORTS.get(scheme)
    identity_port = None if effective in (None,) or effective in WEB_DEFAULT_PORTS \
        else effective

    # 查询串与片段在这里被丢掉 —— 它们进的是 `raw_path`（证据），不是身份。
    # 顺序是 `#` 先还是 `?` 先无所谓：两者都会截断，先撞上哪个都一样。
    path = raw_path.split("#", 1)[0].split("?", 1)[0]
    template = path_template(path)
    key = _bracket(host) + (f":{identity_port}" if identity_port else "") + template

    return {"key": key, "scheme": scheme, "host": host,
            "host_kind": "ip" if try_ip(host) is not None else "domain",
            "port": effective, "path": path, "raw_path": raw_path,
            "template": template}


def endpoint_key(raw: object, *, base: object | None = None) -> str:
    """`endpoint_identity` 的字符串面 —— 身份规则本身，夹具执行的就是它。"""
    return str(endpoint_identity(raw, base=base)["key"])


#: HTTP 方法（RFC 9110 的 token）：全 ASCII 字母，允许内部的 `-`
#: （`M-SEARCH` 是真实存在的）。长度上限防的是一条畸形行塞进一段长文本。
_METHOD_RE = re.compile(r"^[A-Za-z]{2,16}(?:-[A-Za-z]{2,16})?$")


def http_method(raw: object) -> str:
    """HTTP 方法归一化：折成大写，`get` / `Get` / `GET` 是同一个方法。

    方法**不进 endpoint 的身份**（`GET /x` 与 `POST /x` 是同一个接口的两件事，
    而不是两个接口 —— 接口是"那个地址上能被调用的东西"）。它落成一条观测，
    所以这里归一化的是**观测值**：不折的话 `get` 与 `GET` 会变成两条观测，
    接口镜头里表现为一个假的冲突。

    工具没给方法时**调用方不要调它** —— 返回空串在这里等于"没得归一化"，
    而不是"方法未知"（把缺失写成 `UNKNOWN` 是解析器的裁定，不是归一化的）。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise NormalizeError(f"HTTP 方法必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        return ""
    _require_no_homoglyphs(text, "HTTP 方法")
    if not _METHOD_RE.match(text):
        raise NormalizeError(f"不是合法的 HTTP 方法：{text!r}")
    return text.upper()


# ---------------------------------------------------------------------------
# 接口契约（apischema）· 3.4 openapi / postman / har / curl
# ---------------------------------------------------------------------------
#
# 这一节是 `apischema` 这个 kind 的**身份规则本身**（登记在
# `kind_registry.identity_rule` 里），所以它必须住在 `normalize.py`。
#
#     apischema 身份 = 这份契约**所属的那个起源**：主机[:非默认端口]
#
# ---------------------------------------------------------------------------
# 裁定：身份锚在**主机**上，不锚在文档标题或版本上
# ---------------------------------------------------------------------------
# 这是本步最难的一处取舍，先把判据说清楚：验收标准要的是「**同一个 API 定义
# 从四个来源进来，要认出是同一个**」。四个来源能共同给出的东西**只有主机**：
#
#     OpenAPI  → `servers[].url`            （契约声明它服务在哪个起源上）
#     Postman  → `item[].request.url.host`  （集合里的每条请求）
#     HAR      → `entry.request.url`        （每一跳）
#     curl     → 命令末尾的那个 URL
#
# 标题 / 版本看起来更"像"一份 API 的名字，但它们**过不了这条验收标准**：
# HAR 与 curl 里根本没有标题，拿标题当身份等于让这两类来源永远落不到另外两类
# 身上 —— 那恰恰是要被认出来的那件事。用一个只有两个来源能算出来的字段当身份，
# 规则本身就是错的，不是"还不够完善"。
#
# 所以标题与版本**落成观测，不进身份**（与 `endpoint` 那条"折叠的是身份，
# 不是事实"同一条纪律）。代价写在下面，它是一条**真实**的代价：
#
#   * 一台主机上并存两份契约（`/v1/swagger.json` 与 `/v2/swagger.json`）时，
#     两者折成**同一个** apischema。这不是丢信息：两份文档的标题与版本各落成
#     一条观测，而 `/api/v1/*` 与 `/api/v2/*` 本来就是**两条不同的 endpoint**
#     （3.3 的折叠规则刻意不折版本号）—— "哪些还在用 v1"这个问题在 endpoint
#     那一层就有答案，不需要 apischema 再分一层；
#   * 反过来，把版本放进身份会让"同一个 API 出了一版新文档"变成**另一个实体**，
#     而它俩说的还是同一批接口。
#
# 与 `endpoint` 的身份是同一种形状（主机 + 非默认端口才写出来）：一份契约与
# 它描述的那些接口住在同一个起源上，两者的主机部分逐字相同。
#
# 端口折的是 `WEB_DEFAULT_PORTS`（与 endpoint 同一条裁定）：省略、写 80、写 443
# 是同一件事，8080 / 8443 保留。


def apischema_identity(raw: object) -> dict[str, object]:
    """一份 API 契约的**起源**（绝对 URL 或裸 `host[:port]`）→ 规范身份与部件。

    返回 `{"key", "host", "host_kind", "scheme", "port"}`：

    * `key` —— **身份**：`主机[:非默认端口]`（如 `api.example.com` /
      `api.example.com:8443`）。四个来源的 URL 里都能算出它；
    * `host` —— 不含方括号的 IP / 域名（调用方拿它去算 `service` 的 key）；
    * `port` —— **有效端口**（缺省时按 scheme 补齐，补齐表见 `_DEFAULT_PORTS`）；
      它与 `key` 里那个端口不是一回事，同 `endpoint_identity`；
    * `scheme` —— 空串表示来源只给了裸 `host[:port]`（Postman 的 `url.host`
      数组、Swagger 2 的 `host` + `schemes` 都可能走到这里）。

    ⚠️ 路径**不进**身份。`https://api.example.com/v2` 与
    `https://api.example.com/openapi.json` 是同一个起源 —— 前者是契约声明的
    base path，后者是这份契约**被取回来的地址**，而取回地址换个位置（CDN、
    仓库路径）不该让同一份 API 变成两个实体。
    """
    text = _clean_endpoint_text(raw).rstrip("/")
    if "://" in text:
        scheme, _, rest = text.partition("://")
        scheme = scheme.lower()
        _require_scheme(scheme, text)
        authority = rest.partition("/")[0]
    else:
        scheme = ""
        authority = text.partition("/")[0]
    if "@" in authority:                     # userinfo：凭据，不进身份（§4.5）
        authority = authority.rsplit("@", 1)[1]
    raw_host, raw_port = _split_authority(authority)
    host = _normalize_target_host(raw_host)
    port = _port_or_none(raw_port)

    effective = port if port is not None else _DEFAULT_PORTS.get(scheme)
    identity_port = None if effective in (None,) or effective in WEB_DEFAULT_PORTS \
        else effective
    key = _bracket(host) + (f":{identity_port}" if identity_port else "")
    return {"key": key, "host": host,
            "host_kind": "ip" if try_ip(host) is not None else "domain",
            "scheme": scheme, "port": effective}


def apischema_key(raw: object) -> str:
    """`apischema_identity` 的字符串面 —— 身份规则本身，夹具执行的就是它。"""
    return str(apischema_identity(raw)["key"])


#: 参数名的合法字符（RFC 9110 的 token 子集 + `[` `]` `.`）。
#: 方括号是 PHP 系的数组参数（`filter[status]`），点是 JSON 嵌套（`user.name`）
#: —— 两者都是真实现象。**刻意不含** `$` / 空白 / 引号：它们要么来自模板语言，
#: 要么根本不是一个参数名。
_PARAM_NAME_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.[]")


def param_name(raw: object) -> str:
    """参数名归一化 —— `param.<名字>` 这个观测字段名的后半段。

    **刻意不折大小写**：HTTP 查询参数与 JSON 键在标准里都是大小写敏感的，
    `?Page=1` 与 `?page=1` 是两个不同的输入。折了就是把两个参数并成一个，
    而那是**静默丢信息**（与 `SEGMENT_TEMPLATES` 那条"折叠多了没人发现"
    是同一个风险）。这一点与 `http_method` 相反 —— 方法名在 RFC 里明确
    不区分大小写，参数名没有这条。

    认不出的名字**抛错**而不是清洗：把 `{{token}}` 清洗成 `token` 会让一条
    模板变量伪装成一个真实参数（3.4 的"不做变量展开"就是这条）。
    """
    if not isinstance(raw, str):
        raise NormalizeError(f"参数名必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("参数名是空的")
    if len(text) > 64:
        raise NormalizeError(f"参数名超过 64 个字符：{text[:32]!r}…")
    if not all(ch in _PARAM_NAME_CHARS for ch in text):
        raise NormalizeError(
            f"参数名里有不认识的字符（只认字母数字与 _-.[]）：{text!r}")
    _require_no_homoglyphs(text, "参数名")
    return text


def _clean_endpoint_text(raw: object) -> str:
    """URL / 路径的公共前置校验。错误信息要能指出**是哪个原始串**不合法。"""
    if not isinstance(raw, str):
        raise NormalizeError(f"endpoint 必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise NormalizeError("空 endpoint（没有它就算不出接口的身份）")
    if "\x00" in text:
        raise NormalizeError("endpoint 含 NUL 字节")
    if any(ch.isspace() for ch in text):
        raise NormalizeError(f"endpoint 含空白字符：{text!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise NormalizeError("endpoint 含控制字符")
    _require_no_homoglyphs(text, "endpoint")
    return text


def _require_scheme(scheme: str, text: str) -> None:
    if not scheme or not all(ch in _SCHEME_CHARS for ch in scheme):
        raise NormalizeError(f"endpoint 的 scheme 不合法：{text!r}")
    if scheme[0].isdigit():
        raise NormalizeError(f"endpoint 的 scheme 不合法（以数字开头）：{text!r}")


def endpoint_base(raw: object) -> str:
    """`Target:` 那一行 → **规范的 base**（`scheme://host[:port]/`）。

    相对路径**必须**有一个 base 才知道主机是谁（dirsearch 的 `Target:` 是它唯一的
    来源）。这个函数存在的理由是**报错的位置**：有了它，写坏的 `Target:` 在读到那
    一行时就被指出，而不是等到后面某条结果行才报一条指向"`/admin` 有问题"的错误。

    base 自己必须是一个**绝对的、根路径的** URL：`http://a/` 可以，
    `http://a/sub/` 不行 —— 把 `/admin` 拼到 `http://a/sub/` 后面有多种合理答案，
    猜错的代价是一整棵子树的 endpoint 落到错误的 key 上，而且不会有任何信号。
    """
    text = _clean_endpoint_text(raw)
    if "://" not in text:
        raise NormalizeError(f"base 必须是绝对 URL（带 scheme）：{text!r}")
    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    _require_scheme(scheme, text)
    authority, slash, tail = rest.partition("/")
    if slash and tail.strip("/"):
        raise NormalizeError(
            f"base 的路径必须是根（`http://a/`），收到 {text!r} ——"
            f"把相对路径拼到一个子目录后面有多种合理答案，猜错的代价是一整棵子树"
            f"落到错误的 key 上")
    if "@" in authority:                     # userinfo 是凭据，不进 base（§4.5）
        authority = authority.rsplit("@", 1)[1]
    raw_host, raw_port = _split_authority(authority)
    host = _normalize_target_host(raw_host)
    port = _port_or_none(raw_port)
    return f"{scheme}://{_bracket(host)}" + (f":{port}" if port else "") + "/"


def is_absolute_url(raw: object) -> bool:
    """这一行是不是一条**绝对的** URL（`scheme://…`，且不含空白）。

    **它不是归一化** —— 归一化回答"怎么写才唯一"，这个函数回答"这是什么形状"
    （`normalize` 的文件头把这条边界写死了）。它住在这里的理由只有一条：scheme
    的字符集必须**只有一处定义**（`_SCHEME_CHARS`）。解析器各写一份"冒号前是
    字母就算 scheme"的判据时，两边会在遇到 `1http://a` 或 `ht_tp://a` 的那天分叉，
    而分叉的表现是"嗅探认领了整份文件、解析器一行都读不出来"。

    scheme 的判据刻意**不**是"http / https 白名单"：白名单会让 `ftp://…` 那样的
    一行掉进坏行，而它是真实存在的一种发现结果 —— 拒绝一份真实数据比放过一个
    陌生的 scheme 贵得多（与 `_SCHEME_CHARS` 的注释同一条理由）。
    """
    if not isinstance(raw, str):
        return False
    if "://" not in raw or any(ch.isspace() for ch in raw):
        return False
    head, _, rest = raw.partition("://")
    if not head or not rest:
        return False
    return all(ch in _SCHEME_CHARS for ch in head) and not head[0].isdigit()


def _split_base(base: object | None) -> tuple[str, str]:
    """相对路径的 base → `(scheme, authority)`。校验与规范写法见 `endpoint_base`。"""
    if base is None:
        raise NormalizeError(
            "相对路径需要 base（dirsearch 的 `Target:` 是它的唯一来源）——"
            "没有 base 就无法知道这是哪台主机上的接口")
    scheme, _, rest = endpoint_base(base).partition("://")
    authority = rest.rstrip("/")
    return scheme, authority


def _port_or_none(raw_port: str) -> int | None:
    """`host:` 后面的那段 → 端口。空 → `None`。

    只认 **ASCII** 数字：`str.isdigit()` 是 Unicode 感知的，`int("٣٤") == 34`，
    于是一个谁也不认识的端口会被静默当成 34（与 `_strip_port` 同一条理由）。
    """
    if not raw_port:
        return None
    if not raw_port.isascii() or not raw_port.isdigit():
        raise NormalizeError(f"endpoint 的端口不是数字：{raw_port!r}")
    port = int(raw_port)
    if not 0 < port <= 65535:
        raise NormalizeError(f"endpoint 的端口越界：{port}")
    return port
