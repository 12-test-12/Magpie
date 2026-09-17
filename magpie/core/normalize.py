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
    "compare_version",
    "normalize_cidr",
    "normalize_domain",
    "normalize_ip",
    "path_template",
    "prerelease_tuple",
    "product_name",
    "try_cidr",
    "try_domain",
    "try_ip",
    "version_tuple",
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
    if not segment:
        return segment
    if _UUID_RE.match(segment):
        return "{uuid}"
    if _DATE_COMPACT_RE.match(segment):
        return "{date}"
    if _ISODATE_RE.match(segment):
        return "{date}"
    # 纯数字先于 hex：`1234567890123456` 是"一个很长的 id"，不是指纹。
    # 反过来把 id 折成 {hash} 会让指纹镜头把每条记录都当成新指纹。
    if _NUMERIC_RE.match(segment):
        return "{id}"
    if _HEX_RE.match(segment):
        return "{hash}"
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
