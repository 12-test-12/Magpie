"""解析器框架（开发实施计划 2.4 · 计划书 §5.3）。

这个文件立的是**解析器与数据层之间的契约**。三件事在这里一次性说清：

1. **解析器产出什么** —— `RawEntity` / `RawObservation` / `RawRelation`，
   字段沿用计划书 §5.3。
2. **解析器能看到什么** —— `ParseContext`（§5.3 的汇报通道）。
3. **解析器不许做什么** —— 硬线，写在下面的"三条硬线"里，由结构 + lint + 测试三处守着。

三条硬线
--------

**硬线 1 · 只产出 `(kind, key)` 引用，绝不产出数据库自增 id。**
`RawObservation.entity_ref` 与 `RawRelation.src` / `dst` 都是 `tuple[str, str]`，
`RawEntity.key` 是 `str`。这不是约定，是**结构**：三个 dataclass 的
`__post_init__` 会在构造时就拒绝非字符串引用（见 `_assert_ref`）。
理由很直接：解析器重跑时自增 id 会变，而 `(kind, key)` 不会 —— 于是
"这份文件说的事实"在 undo/redo 之后仍然说得通。

**硬线 2 · 解析器只读文件，写入是 ingest 的职责。**
`magpie/core/parsers/**` 不得 import `magpie.core.ops` / `magpie.core.store`
（写入口）。由 `check.py` 的 `PARSE-RO` 规则静态守着 —— 解析器一旦能写库，
"一切入口走管线"就没了，而这是 R1 的整条价值。

**硬线 3 · 解析器只调用 `normalize.py`，不得内联自己的归一化。**
`Nginx` / `nginx` / `NGINX/1.24` 三种写法同时入库，正是计划书 §3.6 要防的混乱。
由 `check.py` 的 `PARSE-NORM` 规则静态守着。

词表（kind / 关系动词）
-----------------------

`KIND_REGISTRY` / `RELATION_VOCABULARY` 是**数据库里那份登记表的 Python 镜像**。
唯一事实来源是 `schema/003_kind_registry.sql` 建的 `kind_registry` /
`relation_vocabulary` 两张表 + 它们的拒绝触发器；这里这份镜像的用途只有两个：

* 解析器在**产出**记录时给出可读的早期报错（而不是等到写库才被触发器拒绝）；
* `magpie/core/tests/test_parsers.py` 里有一条测试断言两份**逐行一致** ——
  镜像一旦与 schema 分叉，测试立刻红。

所以别在这儿"顺手加一个 kind"。要加 kind：先写迁移，再改这里，测试会替你确认两边一致。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Iterator, Protocol, runtime_checkable

__all__ = [
    "KIND_REGISTRY",
    "LENSES",
    "RELATION_VOCABULARY",
    "OfflineContext",
    "ParseContext",
    "Parser",
    "RawEntity",
    "RawObservation",
    "RawRelation",
    "Record",
    "VocabularyError",
    "require_kind",
    "require_relation",
]


class VocabularyError(ValueError):
    """未登记的 kind / 关系动词。**可读** —— 它会被写进报告给人看。"""


# ---------------------------------------------------------------------------
# §5.3 的记录类型
# ---------------------------------------------------------------------------


def _assert_ref(ref: Any, where: str) -> None:
    """`(kind, key)` 引用的形状校验（硬线 1）。

    在这里拒绝 int 而不是靠 code review：一个 `entity_ref=(1, "x")` 一旦流进
    管线，写进库的就是别的项目/别的作业的行 —— 而且是**静默**的。
    """
    if not isinstance(ref, tuple) or len(ref) != 2:
        raise TypeError(f"{where} 必须是 (kind, key) 二元组，收到 {ref!r}")
    kind, key = ref
    if not isinstance(kind, str) or not isinstance(key, str):
        raise TypeError(
            f"{where} 必须是 (str, str)；收到 ({type(kind).__name__}, "
            f"{type(key).__name__})。解析器只产出 (kind, key) 引用，"
            f"绝不产出数据库自增 id（计划书 §5.3 硬线）")
    if not kind or not key:
        raise TypeError(f"{where} 的 kind / key 不得为空：{ref!r}")


@dataclass(frozen=True)
class RawEntity:
    """一个实体的声明。`key` 是**身份标签**（写进 `entity_keys`），不是展示名。"""

    kind: str
    key: str
    display: str | None = None
    subtitle: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise TypeError(f"RawEntity.kind 必须是非空字符串，收到 {self.kind!r}")
        if not isinstance(self.key, str) or not self.key:
            raise TypeError(
                f"RawEntity.key 必须是非空字符串，收到 {self.key!r}"
                f"（解析器只产出 (kind, key) 引用，绝不产出数据库自增 id）")


@dataclass(frozen=True)
class RawObservation:
    """一条挂在某个实体上的观测（`type` 即 `semantic_field`）。"""

    entity_ref: tuple[str, str]        # (kind, key) —— 绝不是 DB id
    type: str
    value: Any
    severity: str | None = None
    state: str | None = None
    confidence: float | None = None
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _assert_ref(self.entity_ref, "RawObservation.entity_ref")
        if not isinstance(self.type, str) or not self.type:
            raise TypeError(f"RawObservation.type 必须是非空字符串，收到 {self.type!r}")


@dataclass(frozen=True)
class RawRelation:
    """一条关系。`src` / `dst` 同样是 `(kind, key)` 引用。"""

    src: tuple[str, str]
    dst: tuple[str, str]
    rel: str
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _assert_ref(self.src, "RawRelation.src")
        _assert_ref(self.dst, "RawRelation.dst")
        if not isinstance(self.rel, str) or not self.rel:
            raise TypeError(f"RawRelation.rel 必须是非空字符串，收到 {self.rel!r}")


#: 解析器 `parse()` 的产出类型。3.11 起 `X | Y` 可在运行时求值（下限即 3.11）。
Record = RawEntity | RawObservation | RawRelation


def is_relation(rec: Any) -> bool:
    return isinstance(rec, RawRelation)


def is_observation(rec: Any) -> bool:
    return isinstance(rec, RawObservation)


def is_entity(rec: Any) -> bool:
    return isinstance(rec, RawEntity)


# ---------------------------------------------------------------------------
# §5.3 的汇报通道
# ---------------------------------------------------------------------------


@runtime_checkable
class ParseContext(Protocol):
    """解析器**唯一**能看到的外部状态（计划书 §5.3）。

    为什么是 Protocol 而不是具体类：`ingest.ParseContext` 是**管线侧的实现**
    （它还要记行号、握中断回调、攒批次），这里要的是**契约**。
    定义成 Protocol 之后，两边就不是"两份实现"而是"一份契约 + 一个实现"——
    而且 `test_parsers.py` 有一条断言证明了那个实现确实满足这份契约。

    解析器只应使用下面这 6 个成员。`ingest.ParseContext` 还有 `job_id` /
    `current_line` / `notes` 等，那些是**管线自己**用的，解析器不该依赖。
    """

    project_id: int
    source_override: str | None

    def on_bad_line(self, lineno: int, reason: str, raw: str = "") -> None:
        """坏行 → 落 `<data-dir>/reports/<job_id>.badlines.txt`（§5.6）。

        `reason` 必须是**人话**（"不是合法域名"），不是异常文本。
        """

    def on_unknown_field(self, name: str, lineno: int | None = None) -> None:
        """未知字段 → 格式漂移信号（§5.6）。累计行数，由报告回显。"""

    def cancelled(self) -> bool:
        """中断检查点。解析器应在**行边界**检查它，并尽快返回。"""

    def batch(self) -> None:
        """批次边界（解析器可用来做内存管理）。管线在自己的 checkpoint 也会调。"""


@dataclass
class OfflineContext:
    """不接数据库的最小 `ParseContext` 实现 —— 夹具、bench、单测用。

    它存在的意义是让"解析一个文件"这件事**不需要一个库**就能跑：
    黄金夹具的逐字段比对、吞吐 bench、以及"喂一条带新字段的行看报告"，
    都不该为了拿一个 ctx 先建一个 SQLite。
    """

    project_id: int = 0
    source_override: str | None = None
    bad_lines: list[dict[str, Any]] = field(default_factory=list)
    unknown_fields: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    current_line: int = 0

    def on_bad_line(self, lineno: int, reason: str, raw: str = "") -> None:
        self.bad_lines.append({"line": int(lineno), "reason": str(reason),
                               "raw": str(raw)[:400]})

    def on_unknown_field(self, name: str, lineno: int | None = None) -> None:
        key = str(name)
        self.unknown_fields[key] = self.unknown_fields.get(key, 0) + 1

    def cancelled(self) -> bool:
        return False

    def batch(self) -> None:
        """离线路径没有批次边界要维护。"""

    # ---- 离线便利方法（不是 §5.3 契约的一部分）----

    def bad_line_report(self) -> str:
        """人读的坏行报告 —— 与 `ingest` 落盘的那份同一口径。"""
        if not self.bad_lines:
            return ""
        lines = [f"# 坏行明细（共 {len(self.bad_lines)} 条）"]
        lines += [f"{b['line']}\t{b['reason']}\t{b['raw']}" for b in self.bad_lines]
        return "\n".join(lines) + "\n"

    def unknown_field_report(self) -> list[str]:
        """§5.6 要求的那句话：`检测到新字段 x（N 行）`。"""
        return [f"检测到新字段 `{name}`（{count} 行），该工具可能已更新输出格式"
                for name, count in sorted(self.unknown_fields.items())]


# ---------------------------------------------------------------------------
# §5.3 的解析器接口
# ---------------------------------------------------------------------------


class Parser(ABC):
    """一个格式一个类。

    解析器是**无状态的单次使用者**：管线 `parser_cls().parse(lines, ctx)`，
    跑完即弃。所以这里没有任何实例状态，四个类属性就是全部自我描述。
    """

    #: 工具名，也是 source 名与 parser fingerprint 的前半段。
    tool: ClassVar[str] = ""
    #: 格式版本。**同一工具的输出格式变了就必须 +1** —— fingerprint 靠它区分。
    format_version: ClassVar[str] = "1"
    #: 本解析器**认识**的字段。出现别的 key → `on_unknown_field`（§5.6）。
    known_fields: ClassVar[frozenset[str]] = frozenset()
    #: 该来源的事实可信度上限（§8.2 的 authority_term 输入之一）。
    max_confidence: ClassVar[float] = 1.0
    #: 同类解析器互斥组（§5.4 的 `exclusive_group`）。空 = 不参与互斥。
    exclusive_group: ClassVar[str] = ""

    @property
    def fingerprint(self) -> str:
        """`tool@format_version` —— 每个 job 冻结的就是它。"""
        return f"{self.tool}@{self.format_version}"

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """给样本打分（0.0–1.0）。**默认 0.0 = 不认领任何文件。**

        纪律：嗅探器只看**样本**，不做 IO、不抛异常。它判错的代价是整批文件
        被推给人工裁决（§5.4 的仲裁），所以宁可给低分也不要猜。
        """
        return 0.0

    @abstractmethod
    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """`lines` 是**流式**迭代器（一行一次，已剥换行、已剥 NUL）。

        必须逐行产出、不许 `list(lines)`：10 万行的文件不该把内存吃光。
        坏行 → `ctx.on_bad_line(...)` 后继续，**不要抛**：一行坏掉不等于整份文件坏掉。
        """


# ---------------------------------------------------------------------------
# 词表的 Python 镜像（唯一事实来源是 schema/003_kind_registry.sql）
# ---------------------------------------------------------------------------

#: 计划书 §3.5 的五个镜头。kind 的"镜头归属"必须是其中之一。
LENSES: frozenset[str] = frozenset(
    {"graph", "assets", "services", "api", "traffic", "vuln"})


@dataclass(frozen=True)
class KindSpec:
    """一个已登记 kind 在**解析器这一侧**需要的形状。

    三件套（身份规则 / 样例夹具 / 镜头归属）的正文不在这里 —— 它们住在
    `schema/003_kind_registry.sql` 的登记表与 `fixtures/kinds/<kind>.json` 里，
    而那两处**互为对方的证据**（测试断言两边逐字一致）。

    这里只留解析器真正要用的东西：一个 kind 名、它属于哪个镜头、它的夹具在哪、
    以及谁在产出它。**刻意不在这里再抄一份身份规则的正文** —— 三份同样的长文本
    迟早会分叉成三份不同的真相，而"哪份对"将没有答案。
    """

    kind: str
    fixture: str
    lens: str
    #: 产出它的解析器（用于反查"这个 kind 谁在用"）。空 = 目前只有数据层在用。
    produced_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationSpec:
    """一个已登记的关系动词。`inverse` 空 = 自反或无可逆表达。"""

    verb: str
    description: str
    inverse: str = ""


#: **真的落地**的 kind（2.4 的五个 + 3.1 的 `cidr` + 3.2 的 `vuln`）。刻意先窄
#: 后宽：每个登记的 kind 都要带三件套，而 `endpoint` / `apischema` 之流的三件套
#: 只有在阶段 3 各自的解析器里才有真实形状。提前登记它们等于凭空设计一套必然
#: 要返工的约束（计划书 §3.3 的教训）。
#:
#: `cidr` 是这条纪律的正面例子：`ip` 的身份规则里 2.4 就写着"网段是另一种
#: kind"，但**那时没有产出者**，所以它一直没有被登记；3.1 的 nmap `<target>`
#: 是第一个真实产出者，三件套同期到位（迁移 `schema/004_kind_cidr.sql`）。
#: 3.2 的 `vuln` 走的是同一条路（迁移 `schema/005_kind_vuln.sql`）。
KIND_REGISTRY: tuple[KindSpec, ...] = (
    KindSpec(kind="domain", fixture="fixtures/kinds/domain.json", lens="assets",
             produced_by=("domainlist@1", "httpx@1", "nmap@1", "nuclei@1")),
    KindSpec(kind="ip", fixture="fixtures/kinds/ip.json", lens="assets",
             produced_by=("httpx@1", "nmap@1", "nuclei@1")),
    #: 网段（3.1 起有产出者：nmap 的 `<target specification="10.0.0.0/24">`）。
    #: **与 `ip` 是两个 kind** —— 把 1.2.3.4 与 1.2.3.0/24 认成同一个实体，
    #: 会让「哪台机器」的答案变成「哪一段」（`ip` 的身份规则里写死了这一条）。
    KindSpec(kind="cidr", fixture="fixtures/kinds/cidr.json", lens="assets",
             produced_by=("nmap@1",)),
    KindSpec(kind="host", fixture="fixtures/kinds/host.json", lens="assets"),
    KindSpec(kind="service", fixture="fixtures/kinds/service.json", lens="services",
             produced_by=("httpx@1", "nmap@1", "nuclei@1")),
    KindSpec(kind="product", fixture="fixtures/kinds/product.json", lens="services",
             produced_by=("httpx@1", "nmap@1")),
    #: 漏洞（3.2 起有产出者：nuclei 的 `-jsonl`）。**镜头归属是 003 里已冻结的
    #: `vuln`** —— 003 的 `CHECK` 是冻结的，而 R4 不许改 003，所以新增 lens id
    #: 事实上不可能；`vuln` 恰好在六个镜头之内。
    #:
    #: 它的身份是 `template-id@规范化目标`（`normalize.vuln_key`），**不是**
    #: template-id 一个人：只用 template-id 就答不出"这个模板命中了几个地方"，
    #: 而唯一的补救是给实体挂一个计数器 —— 那会绕开 contributions 模型，
    #: `undo` 之后留下幻影行（迁移 005 的文件头把这条说透了）。
    KindSpec(kind="vuln", fixture="fixtures/kinds/vuln.json", lens="vuln",
             produced_by=("nuclei@1",)),
)

#: 本步**真的被解析器产出**的关系动词。同样先窄后宽。
#: `relations.rel` 有拒绝触发器，所以这里的每一项都必须有真实产出者。
RELATION_VOCABULARY: tuple[RelationSpec, ...] = (
    RelationSpec(
        verb="member_of",
        description="子域隶属（a.b.example.com --member_of--> b.example.com）。由域名后缀推导，不来自文件里的显式字段；上推在顶级两段停住。",
        inverse="has_member",
    ),
    RelationSpec(
        verb="resolves_to",
        description="域名解析到某个 IP（example.com --resolves_to--> 203.0.113.10）。端点 kind 分别是 domain 与 ip。",
        inverse="resolved_by",
    ),
    RelationSpec(
        verb="runs",
        description="服务上跑着某个产品（example.com:443 --runs--> nginx@1.24.0）。计划书 §4.2 的核心目标：中间件版本的主关系。",
        inverse="runs_on",
    ),
    RelationSpec(
        verb="found_on",
        description="漏洞落在某个资产上（CVE-2021-44228@https://app.example.com:443/ --found_on--> app.example.com:443）。端点 kind 是 vuln 与 service（目标里没有 scheme 也没有端口时退化为 domain / ip）。",
        inverse="has_finding",
    ),
)

KIND_BY_NAME: dict[str, KindSpec] = {spec.kind: spec for spec in KIND_REGISTRY}
RELATION_BY_NAME: dict[str, RelationSpec] = {spec.verb: spec for spec in RELATION_VOCABULARY}


def require_kind(kind: str) -> KindSpec:
    """取已登记的 kind，未登记即抛**可读**错误。

    解析器在产出记录时调它 —— 早期报错比"写库时被触发器拒绝"好得多：
    前者能指出是哪个解析器的哪一行，后者只有一条 ABORT。
    """
    spec = KIND_BY_NAME.get(kind)
    if spec is None:
        raise VocabularyError(
            f"未登记的 kind {kind!r}；已登记：{sorted(KIND_BY_NAME)}。"
            f"新增 kind 必须同时提供身份规则 + 样例夹具 + 镜头归属，"
            f"并走 schema 迁移（见 schema/003_kind_registry.sql）")
    return spec


def require_relation(verb: str) -> RelationSpec:
    spec = RELATION_BY_NAME.get(verb)
    if spec is None:
        raise VocabularyError(
            f"未登记的关系动词 {verb!r}；已登记：{sorted(RELATION_BY_NAME)}。"
            f"新增动词同样要走 schema 迁移（见 schema/003_kind_registry.sql）")
    return spec
