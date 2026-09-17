"""`domainlist` —— 每行一个域名的清单（计划书 §4.1）。

覆盖来源：subfinder / assetfinder / findomain / crt.sh —— 它们**输出形状相同**
（一行一个主机名），差别只在覆盖面。所以这是一个解析器而不是四个：
格式版本由"行的形状"决定，不由工具名决定；真出现形状不同的那一天再拆。

产出
----

* `domain` 实体（身份规则见 `base.KIND_REGISTRY`）；
* `member_of` 关系，**由后缀推导**（`dev-api.example.com --member_of--> example.com`），
  端点是 `domain`。
* 上推在**顶级两段**停住：`example.com` 不再 `member_of` `com` —— 否则每一份
  清单都会造出一个叫 `com` 的实体，资产镜头立刻被噪声淹没。

为什么需要一个"深度"的上推链而不是只连父域：只连直接父域时
`a.b.c.example.com` 会造出 `b.c.example.com`、`c.example.com` 两个**清单里
根本没有的**实体。这不是错（它们确实是推导出的事实），但要知道这是链式上推
的必然代价 —— 每一层都是从子域真实推导出来的，不是凭空造的。
"""

from __future__ import annotations

from typing import Iterable, Iterator

from .. import normalize
from .base import OfflineContext, Parser, ParseContext, RawEntity, RawRelation, Record

__all__ = ["DomainListParser"]


def _parent_domain(domain: str) -> str | None:
    """父域（`a.b.example.com` → `b.example.com`）。顶级两段不再上推。"""
    parts = domain.split(".")
    if len(parts) <= 2:
        return None
    return ".".join(parts[1:])


def _maybe_ip(text: str) -> bool:
    """便宜的预筛：只有**可能**是 IP 的串才值得进 `ipaddress`（它会抛异常）。

    没有它的话，10 万行域名清单会给 `ipaddress` 送 10 万次必失败的解析 ——
    异常构造是这条热路径上最贵的一件事。
    """
    return ":" in text or text.replace(".", "").isdigit()


def accepts(text: str) -> bool:
    """这一行是不是本解析器认的域名 —— **身份规则里"额外边界"的唯一定义处**。

    为什么单独一个函数：`sniff` 与 `parse` 必须用**同一个**判据。分成两套写法
    时，一个纯 IP 的文件会被嗅探判成 0.9（IP 的四个标签都满足域名字符集）
    却在解析时行行报错 —— "我很确定这是域名清单，然后一行都读不出来"。
    """
    if _maybe_ip(text) and normalize.try_ip(text) is not None:
        return False
    return normalize.try_domain(text) is not None


class DomainListParser(Parser):
    tool = "domainlist"
    format_version = "1"
    #: 清单格式没有字段可言 —— 一行就是全部。所以未知字段探测在这里天然为空，
    #: 格式漂移的信号是"合法域名的比例掉下来"（见 `sniff`）。
    known_fields: frozenset[str] = frozenset()
    max_confidence = 0.9
    exclusive_group = "host-list"

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """按"样本里有多少行是合法域名"打分。

        刻意**不**看文件名：`subfinder.txt` 这种名字对仲裁没有证明力，
        而按名字给分会让一个改了扩展名的 httpx JSONL 拿到高分。
        """
        lines = [ln.strip() for ln in sample.replace("\x00", "").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if not lines:
            return 0.0
        sample_lines = lines[:200]
        good = sum(1 for ln in sample_lines if accepts(ln))
        ratio = good / len(sample_lines)
        # 阈值与计划书 §5.4 的仲裁口径对齐：≥0.8 才有资格被采用（≥0.8 分），
        # 0.5–0.8 给 0.6 —— 它够不上采用线，但能在仲裁报告里表示"我看着像"。
        if ratio >= 0.8:
            return 0.9
        if ratio >= 0.5:
            return 0.6
        return 0.0

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        seen: set[str] = set()
        for lineno, line in enumerate(lines, start=1):
            if ctx.cancelled():
                return
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            # IP 字面量必须**显式拒绝**，不能顺着域名规则放过去：
            # `203.0.113.10` 的四个标签都满足域名的字符集，它会被静默建成一个
            # 叫 "203.0.113.10" 的 **domain** 实体 —— 那是错得最安静的一种错。
            # 同时它是一个真实的格式漂移信号（域名清单里开始出现 IP）。
            if _maybe_ip(text) and normalize.try_ip(text) is not None:
                ctx.on_bad_line(lineno, "这一行是 IP，不是域名（domainlist 只收域名）",
                                text)
                continue
            domain = normalize.try_domain(text)
            if domain is None:
                ctx.on_bad_line(lineno, "不是合法域名（或不是完整域名）", text)
                continue
            if domain in seen:
                continue
            seen.add(domain)
            yield RawEntity(kind="domain", key=domain, display=domain)
            parent = _parent_domain(domain)
            if parent is not None:
                # 父域先成实体、再连边 —— 顺序反了的话，`ingest` 会拿**关系的
                # 端点 kind**（也是 domain）兜底建它，这里恰好同 kind 所以看不出
                # 差别；但换一个 src/dst kind 不同的动词（如 resolves_to）就会
                # 悄悄建错。把顺序固定成"先实体后关系"是共通的纪律。
                yield RawEntity(kind="domain", key=parent, display=parent)
                yield RawRelation(src=("domain", domain), dst=("domain", parent),
                                  rel="member_of")


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口：把整段文本解析成记录列表（夹具与单测用）。

    **不是**给管线用的 —— 管线走 `parse()` 的流式迭代器，不会把文件读成字符串。
    """
    parser = DomainListParser()
    return list(parser.parse(text.splitlines(), ctx or OfflineContext()))
