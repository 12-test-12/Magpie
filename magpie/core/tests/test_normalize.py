"""`magpie/core/normalize.py` 的回归网（开发实施计划 2.3 的交付标准）。

    | 纯函数     | 无 IO、无 DB、无全局状态；可在 :memory: 之外独立单测（本文件就没有库）|
    | 幂等       | 每个函数有 f(f(x)) == f(x) 用例                                  |
    | 正反例     | 每组至少 3 正例 + 3 反例                                          |
    | 版本语义   | 有 1.9 < 1.10 用例（**禁止字符串比较版本**）                       |
    | 边界       | 超长域名 / 空串 / 只有点 / 全角字符 / NUL → 抛可读错误，不静默放行   |
    | 职责边界   | **不做 kind 决策、不碰数据库** —— 只回答"怎么写才唯一"              |

反例是重点：归一化静默放行的代价是"一个畸形 key 进了库，从此每次导入都多一个
幽灵实体，而且没人知道它从哪来"。所以每一条反例都断言**抛错**而不是返回原值。
"""

from __future__ import annotations

import pytest

from magpie.core import normalize
from magpie.core.normalize import NormalizeError


# ---------------------------------------------------------------------------
# 域名
# ---------------------------------------------------------------------------

DOMAIN_OK = [
    ("Example.COM.", "example.com"),
    ("Example.com:8443", "example.com"),
    ("  api.example.com  ", "api.example.com"),
    ("例子.中国", "xn--fsqu00a.xn--fiqs8s"),
    ("_dmarc.example.com", "_dmarc.example.com"),
]

DOMAIN_BAD = [
    ("", "空串"),
    ("...", "只有点"),
    ("localhost", "单标签"),
    ("ｅｘａｍｐｌｅ.com", "全角字符"),
    ("a\x00b.example.com", "NUL 字节"),
    ("a" * 300 + ".com", "超长域名"),
    ("exa mple.com", "含空白"),
    ("https://a.example.com/", "URL 不是域名"),
    ("-bad.example.com", "标签以连字符开头"),
]


@pytest.mark.parametrize("raw,expected", DOMAIN_OK)
def test_normalize_domain_ok(raw, expected):
    assert normalize.normalize_domain(raw) == expected


@pytest.mark.parametrize("raw,why", DOMAIN_BAD)
def test_normalize_domain_rejects_with_readable_error(raw, why):
    with pytest.raises(NormalizeError) as exc:
        normalize.normalize_domain(raw)
    message = str(exc.value)
    assert message and "Traceback" not in message, why


def test_normalize_domain_is_idempotent():
    for raw, expected in DOMAIN_OK:
        assert normalize.normalize_domain(expected) == expected


def test_normalize_domain_require_dot_is_a_switch_not_a_default():
    """`localhost` 默认不成实体；要收它必须**显式**关掉 —— 改默认值就是改身份规则。"""
    assert normalize.try_domain("localhost") is None
    assert normalize.normalize_domain("localhost", require_dot=False) == "localhost"


# ---------------------------------------------------------------------------
# IP / 网段
# ---------------------------------------------------------------------------

IP_OK = [
    ("203.0.113.10", "203.0.113.10"),
    ("::ffff:203.0.113.13", "203.0.113.13"),
    ("2001:DB8::1", "2001:db8::1"),
]

IP_BAD = ["203.0.113.0/24", "999.1.1.1", "", "1.2.3", "abc"]

CIDR_OK = [
    ("10.0.0.7/24", "10.0.0.0/24"),
    ("2001:db8::1/64", "2001:db8::/64"),
    ("192.168.1.1/32", "192.168.1.1/32"),
]


@pytest.mark.parametrize("raw,expected", IP_OK)
def test_normalize_ip_ok(raw, expected):
    assert normalize.normalize_ip(raw) == expected


@pytest.mark.parametrize("raw", IP_BAD)
def test_normalize_ip_rejects(raw):
    with pytest.raises(NormalizeError):
        normalize.normalize_ip(raw)


@pytest.mark.parametrize("raw,expected", CIDR_OK)
def test_normalize_cidr_zeroes_host_bits(raw, expected):
    assert normalize.normalize_cidr(raw) == expected


def test_normalize_cidr_strict_refuses_to_silently_fix():
    """`strict=True` 时主机位不补齐而是报错 —— 写错了要立刻知道。"""
    assert normalize.normalize_cidr("10.0.0.7/24") == "10.0.0.0/24"
    with pytest.raises(NormalizeError):
        normalize.normalize_cidr("10.0.0.7/24", strict=True)


def test_ip_helpers_are_idempotent():
    for raw, expected in IP_OK:
        assert normalize.normalize_ip(expected) == expected


# ---------------------------------------------------------------------------
# 路径模板
# ---------------------------------------------------------------------------

TEMPLATE_OK = [
    ("/user/123/profile", "/user/{id}/profile"),
    ("/user/123", "/user/{id}"),
    ("/orders/550e8400-e29b-41d4-a716-446655440000", "/orders/{uuid}"),
    ("/blobs/deadbeefdeadbeef01", "/blobs/{hash}"),
    ("/blog/2026-01-01/post", "/blog/{date}/post"),
    ("/blog/20260101/post", "/blog/{date}/post"),
    ("/static/app.js", "/static/app.js"),
]


@pytest.mark.parametrize("raw,expected", TEMPLATE_OK)
def test_path_template_ok(raw, expected):
    assert normalize.path_template(raw) == expected


def test_path_template_never_over_folds():
    """**绝不误折叠**：`/user/123` 与 `/user/abc` 不得折叠成同一模板。

    `/user/abc` 往往是一条真实路由，把它折成 `/user/{id}` 会让两条不同的接口
    在"接口"镜头里变成一条 —— 那是丢信息，不是归一化。
    """
    assert normalize.path_template("/user/123") == "/user/{id}"
    assert normalize.path_template("/user/abc") == "/user/abc"
    assert normalize.path_template("/user/123") != normalize.path_template("/user/abc")


def test_path_template_drops_query_and_fragment_by_default():
    assert normalize.path_template("/a/1?token=xyz#frag") == "/a/{id}"


@pytest.mark.parametrize("raw", ["", "no-leading-slash", "/a\x00b"])
def test_path_template_rejects(raw):
    with pytest.raises(NormalizeError):
        normalize.path_template(raw)


def test_path_template_is_idempotent():
    for raw, expected in TEMPLATE_OK:
        assert normalize.path_template(expected) == expected


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------


def test_version_ordering_is_numeric_not_lexicographic():
    """`1.9 < 1.10` —— 字符串比较会给相反答案（"1.9" > "1.10"）。"""
    assert normalize.compare_version("1.9", "1.10") == -1
    assert normalize.compare_version("1.10", "1.9") == 1
    assert "1.9" > "1.10"                      # 反例：字符串比较确实是错的
    assert normalize.compare_version("2.0", "10.0") == -1


def test_version_prerelease_is_a_separate_dimension():
    assert normalize.compare_version("1.0.0-rc1", "1.0.0") == -1
    assert normalize.compare_version("1.0.0-alpha", "1.0.0-beta") == -1
    assert normalize.compare_version("1.0.0-rc2", "1.0.0-rc1") == 1
    # 折进 release 元组就分不开了：前者是同一版本的前置，后者是下一个版本。
    assert normalize.compare_version("1.0.0-rc1", "1.0.0.1") == -1


def test_version_padding_makes_1_0_equal_to_1_0_0():
    assert normalize.compare_version("1.0", "1.0.0") == 0


def test_version_tuple_and_prerelease_tuple():
    assert normalize.version_tuple("1.24") == (1, 24, 0)
    # 前导 `v` 剥掉；**四段不截断**（这条断言原先写的是 (2, 1, 3)，
    # 它把"截断"这个 bug 当成期望值固定了下来 —— 见下面的反例网）。
    assert normalize.version_tuple("v2.1.3.4") == (2, 1, 3, 4)
    assert normalize.prerelease_tuple("1.0.0")[0] > normalize.prerelease_tuple("1.0.0-rc1")[0]


@pytest.mark.parametrize("raw", ["", "CloudFront", "1.2.x", "not-a-version"])
def test_version_rejects(raw):
    with pytest.raises(NormalizeError):
        normalize.version_tuple(raw)


def test_version_is_idempotent():
    """规范化写法再进一次，结果不变（`1.24` → `1.24.0` → `1.24.0`）。"""
    for raw in ("1.24.0", "2", "1.24.0-rc1"):
        canonical = ".".join(str(p) for p in normalize.version_tuple(raw))
        assert normalize.version_tuple(canonical) == normalize.version_tuple(raw)
        assert normalize.compare_version(raw, raw) == 0


# ---------------------------------------------------------------------------
# 产品名
# ---------------------------------------------------------------------------

PRODUCT_OK = [
    ("Apache httpd", "httpd"),
    ("APACHE", "httpd"),
    ("apache2", "httpd"),
    ("Microsoft  IIS", "iis"),
    ("NGINX", "nginx"),
    ("Vue.js", "vue"),
]


@pytest.mark.parametrize("raw,expected", PRODUCT_OK)
def test_product_name_ok(raw, expected):
    assert normalize.product_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\x00"])
def test_product_name_rejects(raw):
    with pytest.raises(NormalizeError):
        normalize.product_name(raw)


def test_product_name_is_idempotent():
    for raw, expected in PRODUCT_OK:
        assert normalize.product_name(expected) == expected


def test_product_name_keeps_unknown_names_verbatim_but_lowercased():
    """别名表只折叠**真的会撞上**的那些 —— 不认识的厂商名原样小写保留，不猜。"""
    assert normalize.product_name("Some Obscure Appliance") == "some obscure appliance"


# ---------------------------------------------------------------------------
# 职责边界 / 零依赖
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 反例网 · 一轮审核补的五处「静默」
# ---------------------------------------------------------------------------
# 共同点是**同一类错误**：一个畸形的 key 不进错误报告，而是被悄悄改写成**另一个
# 合法 key**。前者会被人看见并修掉，后者只会让两个不同的东西在库里合并成一个，
# 而且没有任何信号。所以下面每一条都先是红的，再修的。
#
# 根因有两处，都是 Python 把「数字」做得比 ASCII 宽：
#   * `re` 的 `\d` 在 str 上是 **Unicode 感知**的 —— 全角 `１`、阿拉伯-印度数字
#     `٣` 全都算 `\d`；
#   * `int()` / `str.isdigit()` 同样接受它们（`int("１") == 1`）。
# 于是「全角数字」这一路绕过了域名的同形字守卫（那道守卫靠 NFKC≠NFC，而
# `٣` 的 NFKC 等于自身，压根不触发）。


def test_version_tuple_never_truncates_long_versions():
    """**绝不静默丢弃**：`1.2.3.4` 是nginx 的常见第四段，不能被截成 `1.2.3`。

    截断的代价不是"少显示一段"，而是 `1.2.3.4` 与 `1.2.3` 得到**同一个元组**
    —— 两个不同的版本从此是同一个实体。这与 `compare_version` 直接打架：
    后者说它们不同（-1），前者说它们相同。
    """
    assert normalize.version_tuple("1.2.3.4") == (1, 2, 3, 4)
    assert normalize.version_tuple("1.24.0.1") == (1, 24, 0, 1)
    assert normalize.version_tuple("1.2.3.4.5") == (1, 2, 3, 4, 5)
    assert normalize.version_tuple("1.2.3.4") != normalize.version_tuple("1.2.3")
    # 与比较器保持一致：元组不同 ⇔ 比较结果非 0
    assert normalize.compare_version("1.2.3", "1.2.3.4") == -1
    assert normalize.compare_version("1.2.3.4", "1.2.3.4") == 0


def test_version_tuple_still_pads_short_versions():
    """补齐三位是**加法**：短的补零，长的原样 —— 不是"切成三位"。"""
    assert normalize.version_tuple("1.24") == (1, 24, 0)
    assert normalize.version_tuple("2") == (2, 0, 0)


def test_version_digits_are_ascii_only():
    """全角数字不是版本数字 —— `１.２` 与 `1.2` 视觉相同、key 必须不同。

    要么拒绝，要么保留差异；**绝不能**两者折叠成同一个元组。
    """
    with pytest.raises(NormalizeError):
        normalize.version_tuple("１.２")
    with pytest.raises(NormalizeError):
        normalize.version_tuple("1.2.3-rc１")
    assert normalize.version_tuple("1.2") == (1, 2, 0)


def test_path_template_rejects_fullwidth_digits():
    """路径里的全角数字不做静默折叠 —— 与域名的同形字守卫同一条规矩。"""
    with pytest.raises(NormalizeError):
        normalize.path_template("/user/１２３")
    with pytest.raises(NormalizeError):
        normalize.path_template("/blog/２０２６０１０１")


def test_path_template_does_not_fold_non_ascii_digits():
    """NFKC 稳定的非 ASCII 数字（阿拉伯-印度数字）绕过同形字守卫，但**不得被当成 id**。

    这类数字全角化的那道守卫看不见（NFKC 等于自身），只能靠正则里的 `[0-9]` 拦住。
    """
    assert normalize.path_template("/user/٣٤٥") == "/user/٣٤٥"
    assert normalize.path_template("/user/٣٤٥") != "/user/{id}"


def test_product_name_rejects_fullwidth():
    """`ＮＧＩＮＸ` 不得变成 `ｎｇｉｎｘ` —— 那是一个永远撞不上 `nginx` 的幽灵产品。"""
    with pytest.raises(NormalizeError):
        normalize.product_name("ＮＧＩＮＸ")
    with pytest.raises(NormalizeError):
        normalize.product_name("Ａｐａｃｈｅ httpd")
    assert normalize.product_name("NGINX") == "nginx"


def test_domain_port_must_be_ascii_digits():
    """端口只认 ASCII 数字：`example.com:٣٤` 不是"带端口的域名"，是畸形输入。"""
    assert normalize.normalize_domain("example.com:8443") == "example.com"
    with pytest.raises(NormalizeError):
        normalize.normalize_domain("example.com:٣٤")


def test_domain_error_message_quotes_the_original_input():
    """报错必须带上**原形**，不能只有 punycode。

    `example.com:٣٤` 的坏标签是 `xn--com:-ofgg` —— 拿这个去 grep 原始文件什么
    也搜不到，坏行报告就废了。「可读错误」是交付标准，可读的前提是能对上输入。
    """
    with pytest.raises(NormalizeError) as exc:
        normalize.normalize_domain("example.com:٣٤")
    assert "example.com:٣٤" in str(exc.value)


#: `normalize.py` 允许 import 的标准库。**白名单而不是黑名单**：这个模块是
#: "同一件事只有一种写法"的唯一入口，它能碰什么应当是一份看得完的清单。
#: `datetime` 是 4.1 加的 —— 会话（flow）的身份里有**时间窗**，而"时间戳怎么写
#: 才唯一"（UTC、秒精度、ISO8601）正是归一化的活，落到解析器里就会变成
#: `tshark` 与 `har` 各写一种时间格式（`normalize.flow_window` 的文件头）。
NORMALIZE_ALLOWED_IMPORTS = frozenset({
    "ipaddress", "re", "unicodedata", "datetime", "__future__",
})


def test_normalize_does_not_touch_the_database_or_the_write_entry():
    """归一化只回答"怎么写才唯一"，不回答"这是什么"—— 这条由 lint 静态守着，
    这里再断一次它的运行时副作用：模块里没有 sqlite3 / 写入口。"""
    import ast
    from pathlib import Path

    source = Path(normalize.__file__).read_text(encoding="utf-8")
    imported = {n.module.split(".")[0]
                for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name.split(".")[0]
                 for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.Import) for a in n.names}
    assert imported <= NORMALIZE_ALLOWED_IMPORTS, imported - NORMALIZE_ALLOWED_IMPORTS
