"""导入管线 —— 从文件到事实（计划书 §5.1 / 开发实施计划 2.5）。

管线阶段（严格按 §5.1 的顺序，函数名与阶段一一对应）:

    发现文件 → sha256 → 归档（archived / external）→ 编码与格式探测 → 嗅探仲裁
      → 选解析器 → 流式解析 → normalize → 经 ops.add_* 写入
      → 冲突检测 → 进 Inbox → 汇总报告 → 作业收尾

设计要点（每一条都对应一个验收标准，不是风格偏好）:

* **写入只走 `ops.add_entity_field` / `add_observation` / `add_relation`**。
  本模块不直接写 contributions / observations / relations 的任何缓存列。
  唯一直接 INSERT 的表是 `raw_files`（见 `_add_raw_file` 的说明）。

* **流式**：解析按行迭代（`io.TextIOWrapper` + 生成器），探针只读 64 KiB，
  sha256 / 复制按 1 MiB 分块 —— 内存不随文件大小线性增长。

* **幂等**：sha256 + 同一 parser fingerprint 已导入过 → 整文件 `skipped`，
  一行都不写（硬性标准：第二次导入观测数不变）。

* **可撤销**：每个文件一个 job；`ops.undo(job)` 即可撤销。写入按批提交
  （checkpoint），因此"中断后已提交部分仍可 undo"，不留下不一致状态。

* **报告同源**：6 个计数来自运行时，但 `db` 块由 SQL 从数据层**独立**反查。
  两者对不上就是 bug —— 报告不允许自己说自己。

与 2.3 / 2.4 的边界
--------------------
本模块**只调用**解析器与归一化，不实现它们。解析器通过 `load_parsers()` 发现：
优先 `magpie.core.parsers`（2.4 的正式实现），只有在它**一个解析器都没有**时才
退回 `magpie.core.parsers_bridge`（2.5 自带的过渡实现；2.4 落地后自动退役，
可整文件删除）。解析器按 §5.3 的接口**鸭子类型**识别，因此 2.4 换掉 base.py
不影响本模块。
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import io
import os
import pkgutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import ops, store

# ---------------------------------------------------------------------------
# 常量（嗅探阈值来自计划书 §5.4，不是可调参数）
# ---------------------------------------------------------------------------

#: 最高分低于它 → 不采用（进 Inbox 待人工确认）
SNIFF_ADOPT_SCORE = 0.8
#: 与第二名的差距小于它 → 不采用（两个解析器都说得通时不允许猜）
SNIFF_ADOPT_MARGIN = 0.15

#: 探测 / 嗅探用的样本大小（只读文件头）
SAMPLE_BYTES = 64 * 1024
#: sha256 / 复制的分块大小
CHUNK_BYTES = 1 << 20
#: 每多少条记录提交一次 checkpoint。取消时已提交的部分仍可 undo。
DEFAULT_BATCH = 2000

#: 单条进度事件里最多带多少条**新**坏行。坏行明细是给人看的信号，不是全量搬运
#: —— 全量在 `<data-dir>/reports/<job_id>.badlines.txt`，报告里给的是那个路径。
PROGRESS_BAD_LINE_LIMIT = 50

#: 解析器发现顺序 —— 前者是 2.4 的正式实现，后者是 2.5 的过渡实现
PARSER_PACKAGES = ("magpie.core.parsers", "magpie.core.parsers_bridge")

#: 二进制魔数（探测阶段拦下，走"可读报告"而不是抛堆栈）
_BINARY_MAGIC = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip(empty)"),
    (b"\x1f\x8b", "gzip"),
    (b"\x7fELF", "elf"),
    (b"MZ", "pe"),
    (b"%PDF", "pdf"),
    (b"\x89PNG", "png"),
)

#: 编码探测顺序（严格解码成功才认；全失败 → utf-8 + replace，并记一条 note）
_ENCODING_CANDIDATES = ("utf-8", "gb18030")

_INPUT_SUFFIXES = (".txt", ".jsonl", ".ndjson", ".json", ".log", ".csv", ".tsv", ".out")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """编码 / 格式探测结果（**只读文件头**得出）。"""

    encoding: str
    bom: str | None            # 'utf-8' | 'utf-16-le' | 'utf-16-be' | None
    newline: str               # 'lf' | 'crlf' | 'cr' | 'mixed' | 'none'
    nul_count: int
    binary: str | None         # 命中魔数 / NUL 密度过高时的可读标签，否则 None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"encoding": self.encoding, "bom": self.bom, "newline": self.newline,
                "nul_bytes": self.nul_count, "binary": self.binary,
                "notes": list(self.notes)}


@dataclass
class IngestOptions:
    """一次导入的选项。所有"人可能想改"的东西都在这里，不留模块级可变状态。"""

    project_id: int
    data_dir: Path
    parser: str | None = None          # 强制指定解析器（跳过仲裁）
    source_name: str | None = None     # 默认取 parser.tool
    authority_level: str = "unverified"
    archive: str = "archived"          # archived（复制进 data_dir/raw）| external
    dry_run: bool = False              # 同一代码路径，只有 COMMIT→ROLLBACK
    batch_size: int = DEFAULT_BATCH
    actor: str = "import"
    verify: bool = True                # 收尾时跑不变量检查
    cancel_check: Callable[[], bool] | None = None   # 返回 True 即中断
    #: 进度观察者（可选）。**纯观测**：不传它时，本模块的行为与从前逐字节一致。
    #: 传了它，管线会在文件边界、每 batch 条记录、作业收尾各汇报一次（见
    #: `_emit_progress` 的事件表）。它是 2.7 的 SSE 唯一的数据来源 ——
    #: 进度**从运行时来**，而不是 API 层另起一套计数去猜。
    on_progress: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        if self.archive not in ("archived", "external"):
            raise ValueError(f"archive 只支持 archived / external，收到 {self.archive!r}")
        if self.batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，收到 {self.batch_size!r}")
        # 绝对化：归档 locator 要写成 file:// URI，相对路径表达不了（也不该表达）
        self.data_dir = Path(self.data_dir).resolve()


class Cancelled(Exception):
    """管线被中断（不是错误）。已提交的部分保留，job 记 partial。"""


@dataclass
class ParseContext:
    """§5.3 的 ParseContext：解析器只看这个接口，不看 ingest 的内部结构。

    它同时是"解析器向管线汇报"的唯一通道：
        on_bad_line        坏行 → 落 `<data-dir>/reports/<job_id>.badlines.txt`
        on_unknown_field   未知字段 → 格式漂移信号（§5.6）
        cancelled()        中断检查点
        batch()            批次边界（解析器可用来做内存管理）

    `current_line` / `current_line_text` 由管线在喂行时维护，供管线给事实
    挂 evidence 用 —— 解析器**不需要**（也不应该）自己回传行号。
    """

    project_id: int
    source_name: str
    job_id: int | None = None
    source_override: str | None = None
    bad_lines: list[dict[str, Any]] = field(default_factory=list)
    unknown_fields: dict[str, int] = field(default_factory=dict)
    #: 管线级说明（中断、解析器崩了……）。**不算坏行** —— 坏行是"数据有问题"，
    #: 这里是"这一轮没跑完"，两者混在一起会让 bad_lines 这个质量信号失真。
    notes: list[str] = field(default_factory=list)
    current_line: int = 0
    current_line_text: str = ""
    _cancel: Callable[[], bool] | None = field(default=None, repr=False)
    _records: int = field(default=0, repr=False)

    # ---- 解析器汇报通道 ----

    def on_bad_line(self, lineno: int, reason: str, raw: str = "") -> None:
        # 明细全收（要落盘）；内存里只留前 5000 条，避免坏行把内存吃掉。
        if len(self.bad_lines) < 5000:
            self.bad_lines.append({"line": int(lineno), "reason": str(reason),
                                   "raw": str(raw)[:400]})

    def on_unknown_field(self, name: str, lineno: int | None = None) -> None:
        key = str(name)
        self.unknown_fields[key] = self.unknown_fields.get(key, 0) + 1

    def cancelled(self) -> bool:
        return False if self._cancel is None else bool(self._cancel())

    def batch(self) -> None:
        """批次边界。管线在每个 checkpoint 自己也会调用，解析器无需依赖它。"""

    def count(self) -> int:
        return self._records

    def tick(self) -> None:
        self._records += 1


@dataclass
class Counters:
    """6 个计数。恒等式：added + updated + skipped == records。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    bad_lines: int = 0
    unknown_fields: int = 0
    records: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"added": self.added, "updated": self.updated, "skipped": self.skipped,
                "conflicts": self.conflicts, "bad_lines": self.bad_lines,
                "unknown_fields": self.unknown_fields, "records": self.records}


# ---------------------------------------------------------------------------
# 阶段 1 · 发现文件
# ---------------------------------------------------------------------------


def discover(paths: Sequence[str | Path], *, recursive: bool = True
             ) -> tuple[list[Path], list[str]]:
    """把"用户给的一串路径"展开成文件列表。返回 (files, problems)。

    目录按扩展名过滤（`_INPUT_SUFFIXES`），避免把 .db / .png 之类拖进来。
    problems 是**可读**的跳过原因，不抛异常。
    """
    files: list[Path] = []
    problems: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            for child in sorted(walker):
                if child.is_file() and _looks_like_input(child):
                    files.append(child)
        elif p.is_file():
            files.append(p)
        else:
            problems.append(f"{p}: 不存在或不是文件/目录")
    seen: set[str] = set()
    unique: list[Path] = []
    for f in files:                       # 去重但保持顺序（两条 glob 命中同一文件是常态）
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique, problems


def _looks_like_input(path: Path) -> bool:
    return path.suffix.lower() in _INPUT_SUFFIXES


# ---------------------------------------------------------------------------
# 阶段 2 · sha256（分块，不读全量）
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> tuple[str, int]:
    """返回 (hexdigest, 字节数)。分块读取，内存恒定。"""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            h.update(block)
    return h.hexdigest(), size


# ---------------------------------------------------------------------------
# 阶段 3 · 归档（archived / external）
# ---------------------------------------------------------------------------


def _add_raw_file(conn: sqlite3.Connection, *, project_id: int, locator_uri: str,
                  sha256: str, byte_count: int, storage_state: str) -> int:
    """写 `raw_files`。

    **为什么不用 `ops.add_raw_file`**：它的签名是 `content: str`，要求调用方先把
    整个文件读成字符串 —— 与"大文件不 read() 全量"直接冲突。这里写同一张表、
    同一组列，语义等价。`ops` / `store` 的任何既有签名保持原样不动。
    """
    cur = conn.execute(
        "INSERT INTO raw_files(project_id, locator_uri, sha256, byte_count,"
        " storage_state, created_at) VALUES (?,?,?,?,?,?)",
        (project_id, locator_uri, sha256, byte_count, storage_state, store.now()),
    )
    return int(cur.lastrowid)


def archive_file(conn: sqlite3.Connection, opts: IngestOptions, path: Path,
                 sha256: str, size: int) -> tuple[int, str, str]:
    """归档：返回 (raw_file_id, locator_uri, storage_state)。

    archived —— 复制进 `<data-dir>/raw/<前两位>/<sha256>`（按内容寻址，天然去重），
    源文件此后可删；external —— 只记原路径，不动文件。
    """
    if opts.archive == "external":
        locator_uri = path.resolve().as_uri()
        raw_id = _add_raw_file(conn, project_id=opts.project_id, locator_uri=locator_uri,
                               sha256=sha256, byte_count=size, storage_state="present")
        return raw_id, locator_uri, "present"

    dest_dir = opts.data_dir / "raw" / sha256[:2]
    dest = dest_dir / sha256
    if not dest.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with open(path, "rb") as src, open(tmp, "wb") as out:
            while True:
                block = src.read(CHUNK_BYTES)
                if not block:
                    break
                out.write(block)
        os.replace(tmp, dest)             # 原子落位，避免半截文件被当成已归档
    raw_id = _add_raw_file(conn, project_id=opts.project_id, locator_uri=dest.as_uri(),
                           sha256=sha256, byte_count=size, storage_state="archived")
    return raw_id, dest.as_uri(), "archived"


# ---------------------------------------------------------------------------
# 阶段 4 · 编码与格式探测（只读文件头）
# ---------------------------------------------------------------------------


def probe_file(path: Path, *, sample_bytes: int = SAMPLE_BYTES) -> Probe:
    """探测编码 / BOM / 换行 / NUL / 二进制魔数。**不抛异常**，一切进 notes。"""
    with open(path, "rb") as fh:
        sample = fh.read(sample_bytes)
    if not sample:
        return Probe("utf-8", None, "none", 0, None, ("空文件",))

    for magic, label in _BINARY_MAGIC:
        if sample.startswith(magic):
            return Probe("binary", None, "none", sample.count(b"\x00"), label,
                         (f"命中二进制魔数 {label}：归档但不解析",))

    notes: list[str] = []
    bom: str | None = None
    encoding = "utf-8"
    body = sample
    if sample.startswith(b"\xef\xbb\xbf"):
        bom, encoding, body = "utf-8", "utf-8-sig", sample[3:]
    elif sample.startswith(b"\xff\xfe"):
        bom, encoding = "utf-16-le", "utf-16"
    elif sample.startswith(b"\xfe\xff"):
        bom, encoding = "utf-16-be", "utf-16"

    nul_count = body.count(b"\x00")
    if bom is None and nul_count / len(body) > 0.30:
        return Probe("binary", None, "none", nul_count, "nul-heavy",
                     ("NUL 字节占比超过 30%：归档但不解析（疑似无 BOM 的 UTF-16）",))
    if nul_count:
        notes.append(f"含 {nul_count} 个 NUL 字节：解析前剥离，行号不变")

    if bom is None:
        for candidate in _ENCODING_CANDIDATES:
            try:
                body.decode(candidate)
            except UnicodeDecodeError:
                continue
            encoding = candidate
            break
        else:
            encoding = "utf-8"
            notes.append("编码探测失败：按 utf-8 + errors=replace 读，坏字节显示为 \\ufffd")
        if encoding == "gb18030":
            notes.append("按 gb18030 解码成功（GBK 的超集）")
    else:
        notes.append(f"检测到 BOM：{bom}")

    newline = _detect_newline(body)
    if newline == "mixed":
        notes.append("换行不统一（CRLF / CR / LF 混用）：按逻辑行切分")
    return Probe(encoding, bom, newline, nul_count, None, tuple(notes))


def _detect_newline(sample: bytes) -> str:
    crlf = sample.count(b"\r\n")
    cr = sample.count(b"\r") - crlf
    lf = sample.count(b"\n") - crlf
    kinds = [k for k, n in (("crlf", crlf), ("cr", cr), ("lf", lf)) if n]
    if not kinds:
        return "none"
    return "mixed" if len(kinds) > 1 else kinds[0]


# ---------------------------------------------------------------------------
# 阶段 5 · 流式解析（生成器；从不 read() 全量）
# ---------------------------------------------------------------------------


def iter_lines(path: Path, probe: Probe) -> Iterator[tuple[int, str]]:
    """按行流式产出 (行号, 内容)。行号从 1 起，与原始文件一致。

    统一换行交给 `newline=None`（CRLF / 孤立 CR / LF 都变成逻辑行边界），因此
    "孤立 CR"不需要额外分支。NUL 在解析前剥离，行号不受影响。
    """
    if probe.binary:
        return
    with open(path, "rb") as raw:
        if probe.bom == "utf-8":
            raw.read(3)
        text = io.TextIOWrapper(raw, encoding=probe.encoding, errors="replace", newline=None)
        try:
            for lineno, line in enumerate(text, start=1):
                if "\x00" in line:
                    line = line.replace("\x00", "")
                yield lineno, line.rstrip("\n")
        finally:
            text.detach()                  # 不重复关闭底层句柄


def _tracked_lines(path: Path, probe: Probe, ctx: ParseContext) -> Iterator[str]:
    """喂给解析器的行迭代器：维护 `ctx.current_line`，并在中断时抛 Cancelled。

    中断检查放在**行**这一层而不是记录层，是因为解析器可能一行产出多条记录，
    也可能多行产出零条 —— 只有行边界是双方都同意的检查点。
    """
    for lineno, line in iter_lines(path, probe):
        if ctx.cancelled():
            raise Cancelled(f"收到中断，停在第 {lineno} 行")
        ctx.current_line = lineno
        ctx.current_line_text = line
        yield line


# ---------------------------------------------------------------------------
# 阶段 6 · 嗅探仲裁（计划书 §5.4）
# ---------------------------------------------------------------------------


def load_parsers() -> tuple[list[Any], str]:
    """发现可用解析器。返回 (解析器类列表, 来源包名)。

    顺序即优先级：`magpie.core.parsers`（2.4）只要提供**一个**解析器，
    `magpie.core.parsers_bridge`（2.5 过渡实现）就不再参与 —— 自动退役。
    """
    for pkg_name in PARSER_PACKAGES:
        found = _parsers_in_package(pkg_name)
        if found:
            return found, pkg_name
    return [], "（无）"


def _parsers_in_package(pkg_name: str) -> list[Any]:
    try:
        pkg = importlib.import_module(pkg_name)
    except ImportError:
        return []
    modules = [pkg]
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is not None:
        for info in pkgutil.iter_modules(pkg_path):
            if info.name.startswith("_"):
                continue
            try:
                modules.append(importlib.import_module(f"{pkg_name}.{info.name}"))
            except ImportError:
                continue
    out: list[Any] = []
    for module in modules:
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue                    # 只认本模块定义的（不重复收集 import 进来的基类）
            if getattr(obj, "__abstractmethods__", None):
                continue
            if not (getattr(obj, "tool", None) and getattr(obj, "format_version", None)):
                continue
            if not (callable(getattr(obj, "sniff", None))
                    and callable(getattr(obj, "parse", None))):
                continue
            out.append(obj)
    uniq: dict[str, Any] = {}
    for cls in out:
        uniq.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    return sorted(uniq.values(), key=lambda c: (str(c.tool), str(c.format_version)))


def sniff_rank(sample: str, filename: str, parsers: Sequence[Any]) -> list[dict[str, Any]]:
    """给每个解析器打分，返回按分数降序的排名（并列时按 tool 名稳定）。"""
    ranked: list[dict[str, Any]] = []
    for cls in parsers:
        item = {"tool": str(cls.tool), "format_version": str(cls.format_version)}
        try:
            item["score"] = max(0.0, min(1.0, float(cls.sniff(sample, filename))))
        except Exception as exc:            # 嗅探器自己坏了，不能拖垮整条管线
            item["score"] = 0.0
            item["error"] = f"{type(exc).__name__}: {exc}"
        ranked.append(item)
    ranked.sort(key=lambda r: (-r["score"], r["tool"]))
    return ranked


def arbitrate(ranked: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """§5.4 的仲裁：最高分 ≥ 0.8 **且**与第二名差 ≥ 0.15 才采用。"""
    if not ranked:
        return {"adopted": None, "margin": None, "runner_up": None, "ranking": [],
                "reason": "没有任何可用解析器（2.4 的 parsers 包未落地？）"}
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = top["score"] - (second["score"] if second else 0.0)
    reasons: list[str] = []
    if top["score"] < SNIFF_ADOPT_SCORE:
        reasons.append(f"最高分 {top['score']:.2f} < {SNIFF_ADOPT_SCORE}")
    if margin < SNIFF_ADOPT_MARGIN:
        who = f"第二名 {second['tool']} {second['score']:.2f}" if second else "无第二名"
        reasons.append(f"与{who}的差距 {margin:.2f} < {SNIFF_ADOPT_MARGIN}")
    adopted = not reasons
    reason = (f"采用 {top['tool']}@{top['format_version']}：最高分 {top['score']:.2f} "
              f"≥ {SNIFF_ADOPT_SCORE}，与第二名差距 {margin:.2f} ≥ {SNIFF_ADOPT_MARGIN}"
              if adopted else
              f"不采用任何解析器（{'; '.join(reasons)}）→ 进 Inbox 待人工确认")
    return {"adopted": top if adopted else None, "margin": round(margin, 4),
            "runner_up": second, "ranking": list(ranked), "reason": reason}


# ---------------------------------------------------------------------------
# 事实键（added / updated 的判定依据）
# ---------------------------------------------------------------------------


def _fact_key(rec: Any) -> tuple[str, tuple[str, ...]]:
    """把一条 Raw* 记录折成 (类别, 键)。键只用 **(kind, key) 引用**，不用 DB id。"""
    if _is_relation(rec):
        return "relation", (str(rec.src[1]), str(rec.rel), str(rec.dst[1]))
    if _is_observation(rec):
        return "field", (str(rec.entity_ref[1]), str(rec.type), str(rec.value))
    return "field", (str(rec.key), "display_name", str(rec.display or rec.key))


def _is_observation(rec: Any) -> bool:
    return hasattr(rec, "entity_ref") and hasattr(rec, "value") and hasattr(rec, "type")


def _is_relation(rec: Any) -> bool:
    return hasattr(rec, "src") and hasattr(rec, "dst") and hasattr(rec, "rel")


def _existing_facts(conn: sqlite3.Connection, project_id: int) -> tuple[set, set]:
    """导入前数据层里已存在的事实键（用于判定 added / updated）。

    键用 **alias_key / entity_key** 而不是自增 id —— 解析器只产出 (kind, key)
    引用，管线这一侧也必须只用 key，撤销之后结论才仍然说得通。
    """
    fields: set[tuple[str, str, str]] = set()
    for row in conn.execute(
        "SELECT a.alias_key AS k, c.semantic_field AS f, c.field_value AS v"
        "  FROM v_field_owner c"
        "  JOIN entity_alias a ON a.entity_id = c.owner_id AND a.project_id = c.project_id"
        " WHERE c.project_id = ? AND a.revoked_by_job_id IS NULL", (project_id,)
    ):
        fields.add((str(row["k"]), str(row["f"]), str(row["v"])))
    rels: set[tuple[str, str, str]] = set()
    for row in conn.execute(
        "SELECT sa.alias_key AS s, r.rel AS rel, da.alias_key AS d"
        "  FROM relations r"
        "  JOIN entity_alias sa ON sa.entity_id = r.src_id AND sa.project_id = r.project_id"
        "  JOIN entity_alias da ON da.entity_id = r.dst_id AND da.project_id = r.project_id"
        " WHERE r.project_id = ? AND sa.revoked_by_job_id IS NULL"
        "   AND da.revoked_by_job_id IS NULL", (project_id,)
    ):
        rels.add((str(row["s"]), str(row["rel"]), str(row["d"])))
    return fields, rels


def _write_record(conn: sqlite3.Connection, opts: IngestOptions, job_id: int,
                  source_id: int, evidence_id: int, rec: Any, endpoint_kind: str) -> None:
    """把一条 Raw* 记录经受控写入口落库。

    解析器给的是 (kind, key) 引用；这里负责把它翻成数据层的调用参数。
    关系的端点 kind 只影响**实体首次创建**时的 kind —— 解析器应当先产出
    RawEntity 再产出 RawRelation，否则端点会被兜底 kind 建出来。
    """
    if _is_relation(rec):
        ops.add_relation(conn, project_id=opts.project_id, job_id=job_id,
                         evidence_id=evidence_id, source_id=source_id,
                         src_key=str(rec.src[1]), dst_key=str(rec.dst[1]),
                         rel=str(rec.rel), kind=endpoint_kind, authorship="tool")
        return
    if _is_observation(rec):
        kind, key = rec.entity_ref
        ops.add_observation(conn, project_id=opts.project_id, job_id=job_id,
                            evidence_id=evidence_id, source_id=source_id,
                            entity_key=str(key), kind=str(kind),
                            semantic_field=str(rec.type), value=_value_text(rec.value),
                            value_kind=_value_kind(rec.value), authorship="tool")
        return
    ops.add_entity_field(conn, project_id=opts.project_id, job_id=job_id,
                         evidence_id=evidence_id, source_id=source_id,
                         entity_key=str(rec.key), kind=str(rec.kind),
                         semantic_field="display_name",
                         value=str(rec.display or rec.key), authorship="tool")


#: 数据层允许的 value_kind（schema 的 CHECK 就是这份清单）
_VALUE_KINDS = frozenset({"string", "enum", "number", "ip", "cidr", "url", "json", "bool"})


def _value_kind(value: Any) -> str:
    """值类型判定。**必须落在 schema 的 CHECK 清单内**（否则整条记录被拒）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (dict, list, tuple)):
        return "json"
    return "string"


def _value_text(value: Any) -> str:
    """值一律以文本入库（observations.value 是 TEXT）。结构化值走 canonical JSON。"""
    if isinstance(value, (dict, list, tuple)):
        return store.canonical_json(value)
    return str(value)


def _endpoint_kind(rec: Any) -> str:
    """关系端点实体的兜底 kind（解析器给了 `src[0]` 就用它）。

    只在关系记录上求值；端点实体若已在 `RawRelation` 之前被 `RawEntity` 建过，
    这里的 kind 根本不会被用到。
    """
    if not _is_relation(rec):
        return "host"
    try:
        return str(rec.src[0]) or "host"
    except (IndexError, TypeError):
        return "host"


# ---------------------------------------------------------------------------
# 作业级事务（分批 checkpoint；dry-run 走同一代码路径，只把 COMMIT 换成 ROLLBACK）
# ---------------------------------------------------------------------------


class _JobTxn:
    """导入作业的事务外壳。

    为什么不用 `store.transaction`：取消语义要求"已提交部分仍可 undo"，即作业
    中途要有**真实提交**（checkpoint），单个事务表达不了这件事。dry-run 恰好相反
    —— 必须一行都不留，所以它**不**做 checkpoint，全程一个事务、末尾 ROLLBACK。
    两条路径共用同一份写入代码，差异只在 COMMIT / ROLLBACK 这一处（T1 模板的
    纪律：dry-run 说真话的前提是它与真跑同一条路径）。
    """

    def __init__(self, conn: sqlite3.Connection, *, dry_run: bool, batch: int) -> None:
        self.conn = conn
        self.dry_run = dry_run
        self.batch = batch
        self._since_commit = 0
        self.commits = 0

    def __enter__(self) -> "_JobTxn":
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def checkpoint(self) -> None:
        self._since_commit += 1
        if self.dry_run or self._since_commit < self.batch:
            return
        self.conn.execute("COMMIT")
        self.conn.execute("BEGIN IMMEDIATE")
        self._since_commit = 0
        self.commits += 1

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.conn.execute("ROLLBACK" if (self.dry_run or exc_type is not None) else "COMMIT")
        return False


# ---------------------------------------------------------------------------
# 冲突检测 / 坏行落盘 / 数据层反查
# ---------------------------------------------------------------------------


def _open_value_conflicts(conn: sqlite3.Connection, project_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND kind='value_conflict'"
        " AND state='open'", (project_id,)).fetchone()[0])


def _write_badlines(opts: IngestOptions, job_id: int, ctx: ParseContext) -> str | None:
    """坏行明细 → `<data-dir>/reports/<job_id>.badlines.txt`（§5.6）。"""
    if not ctx.bad_lines:
        return None
    reports = opts.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{job_id}.badlines.txt"
    lines = [f"# job {job_id} 坏行明细（共 {len(ctx.bad_lines)} 条，内存上限 5000 条）"]
    for item in ctx.bad_lines:
        lines.append(f"{item['line']}\t{item['reason']}\t{item['raw'].replace(chr(9), ' ')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _db_facts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """从数据层**独立反查**的事实计数（报告"同源"的那一半）。"""
    def q(sql: str) -> int:
        return int(conn.execute(sql, (project_id,)).fetchone()[0])

    return {
        "entities": q("SELECT COUNT(*) FROM entities WHERE project_id=? AND is_current=1"),
        "observations": q("SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1"),
        "relations": q("SELECT COUNT(*) FROM relations WHERE project_id=? AND is_current=1"),
        "observation_rows": q("SELECT COUNT(*) FROM observations WHERE project_id=?"),
        "relation_rows": q("SELECT COUNT(*) FROM relations WHERE project_id=?"),
        "open_conflicts": q("SELECT COUNT(*) FROM inbox_items WHERE project_id=?"
                            " AND kind='value_conflict' AND state='open'"),
    }


def _job_facts(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    """作业自身的足迹（同样全部由 SQL 反查）。"""
    def q(sql: str) -> int:
        return int(conn.execute(sql, (job_id,)).fetchone()[0])

    return {
        "contributions": q("SELECT COUNT(*) FROM contributions WHERE job_id=?"),
        "active_contributions": q("SELECT COUNT(*) FROM v_active_contributions WHERE job_id=?"),
        "evidence": q("SELECT COUNT(*) FROM evidence WHERE id IN"
                      " (SELECT evidence_id FROM contributions WHERE job_id=?)"),
        "open_inbox": q("SELECT COUNT(*) FROM inbox_items WHERE created_by_job_id=?"
                        " AND state='open'"),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _emit_progress(opts: IngestOptions, ctx: ParseContext | None,
                   payload: dict[str, Any]) -> None:
    """把一条进度事件交给观察者（`opts.on_progress`）。

    **观察者的异常不拖垮被观察的作业**：进度是旁路，不是管线的一环。一个断掉的
    SSE 订阅者不该让导入失败，但也不该让这件事**静默** —— 异常会被记进
    `ctx.notes`，随作业报告一起露出来。

    事件表（`event` 字段）:
        file_start  文件即将开始（index / total）
        job_start   作业已开（job_id / parser）
        progress    每 batch 条记录一次（records / line / 6 个计数 / 新坏行）
        job_done    作业收尾（state / counts）
    """
    callback = opts.on_progress
    if callback is None:
        return
    try:
        callback(payload)
    except Exception as exc:                      # noqa: BLE001 —— 见上：旁路不通不该毁主路
        if ctx is not None:
            note = f"进度观察者抛了 {type(exc).__name__}: {exc}（已忽略，不影响本次导入）"
            if note not in ctx.notes:
                ctx.notes.append(note)


def ingest_paths(conn: sqlite3.Connection, opts: IngestOptions,
                 paths: Sequence[str | Path]) -> dict[str, Any]:
    """导入一批文件（每个文件一个 job）。返回聚合报告。"""
    files, problems = discover(paths)
    parsers, source_pkg = load_parsers()
    reports: list[dict[str, Any]] = []
    for index, path in enumerate(files):
        if opts.cancel_check is not None and opts.cancel_check():
            reports.append({"path": str(path), "state": "cancelled", "job_id": None,
                            "reason": "中断：该文件尚未开始", "counts": Counters().as_dict()})
            continue
        _emit_progress(opts, None, {"event": "file_start", "file": path.name,
                                    "path": str(path), "index": index,
                                    "total": len(files)})
        try:
            report = ingest_file(conn, opts, path, parsers=parsers)
        except Exception as exc:            # 单个文件失败不该毁掉整轮导入
            report = _problem_report(path, f"{type(exc).__name__}: {exc}")
        reports.append(report)
        _emit_progress(opts, None, {"event": "file_done", "file": path.name,
                                    "path": str(path), "index": index,
                                    "total": len(files), "job_id": report.get("job_id"),
                                    "state": report.get("state"),
                                    "counts": report.get("counts")})
    totals: dict[str, int] = {}
    for rep in reports:
        for name, value in rep.get("counts", {}).items():
            totals[name] = totals.get(name, 0) + int(value)
    return {
        "kind": "import",
        "dry_run": opts.dry_run,
        "parser_source": source_pkg,
        "available_parsers": [f"{c.tool}@{c.format_version}" for c in parsers],
        "data_dir": str(opts.data_dir),
        "problems": problems,
        "files": reports,
        "totals": totals,
        "job_ids": [rep["job_id"] for rep in reports if rep.get("job_id")],
        "db": _db_facts(conn, opts.project_id),
    }


def ingest_file(conn: sqlite3.Connection, opts: IngestOptions, path: Path, *,
                parsers: Sequence[Any] | None = None) -> dict[str, Any]:
    """一个文件的完整管线（§5.1 的 12 个阶段）。失败也返回可读报告，不抛堆栈。"""
    path = Path(path)
    if parsers is None:
        parsers, _ = load_parsers()

    # --- 阶段 2 / 3 / 4：sha256 → 归档 → 编码与格式探测 ---
    try:
        digest, size = sha256_file(path)
    except OSError as exc:
        return _problem_report(path, f"读不到文件：{exc}")
    probe = probe_file(path)

    # --- 阶段 4.5：二进制短路 ---
    # 二进制内容不能喂给嗅探器：样本解码本身就没有意义（"binary" 不是编码）。
    # 走"归档/跳过的可读报告"，绝不抛堆栈。
    if probe.binary is not None:
        return _binary_report(path, digest, size, probe,
                              {"adopted": None, "margin": None, "runner_up": None,
                               "ranking": [], "reason": f"二进制输入（{probe.binary}）"
                                                        f"，未做嗅探仲裁"})

    # --- 阶段 6：嗅探（只读样本）→ 仲裁 ---
    ranked = sniff_rank(_sample_text(path, probe), path.name, parsers)
    if opts.parser:
        picked = next((c for c in parsers if str(c.tool) == opts.parser), None)
        if picked is None:
            return _problem_report(
                path, f"指定的解析器 {opts.parser!r} 未找到；可用："
                      f"{sorted({str(c.tool) for c in parsers}) or '（无）'}")
        arbitration = {"adopted": {"tool": str(picked.tool),
                                   "format_version": str(picked.format_version),
                                   "score": _score_of(ranked, picked)},
                       "reason": f"人工指定 parser={opts.parser}，跳过仲裁",
                       "margin": None, "runner_up": None, "ranking": ranked,
                       "forced": True}
        parser_cls = picked
    else:
        arbitration = arbitrate(ranked)
        parser_cls = (None if arbitration["adopted"] is None
                      else _resolve_parser(parsers, arbitration["adopted"]))

    fingerprint = (None if parser_cls is None
                   else f"{parser_cls.tool}@{parser_cls.format_version}")

    # --- 阶段 4.5：幂等短路（同一 sha256 + 同一 fingerprint 已导入过） ---
    if fingerprint is not None:
        already = _already_imported(conn, opts.project_id, digest, fingerprint)
        if already is not None:
            return {
                "path": str(path), "sha256": digest, "byte_count": size,
                "state": "skipped", "job_id": already["job_id"],
                "skip_reason": (f"同一文件（sha256 {digest[:12]}…）已由 job "
                                f"{already['job_id']} 用 {fingerprint} 导入过 —— "
                                f"幂等跳过，一行未写"),
                "encoding": probe.as_dict(), "parser": fingerprint,
                "arbitration": arbitration, "counts": Counters().as_dict(),
            }

    if parser_cls is None:
        return _no_parser_report(conn, opts, path, digest, size, probe, arbitration)
    if probe.binary is not None:
        return _binary_report(path, digest, size, probe, fingerprint, arbitration)

    source_name = opts.source_name or str(parser_cls.tool)
    return _run_job(conn, opts, path, digest, size, probe, parser_cls, fingerprint,
                    _definition_hash(parser_cls), source_name, arbitration)


def _run_job(conn: sqlite3.Connection, opts: IngestOptions, path: Path, digest: str,
             size: int, probe: Probe, parser_cls: Any, fingerprint: str,
             definition_hash: str, source_name: str,
             arbitration: dict[str, Any]) -> dict[str, Any]:
    """解析 → 写入 → 冲突 → 报告 → 收尾。**唯一的写入路径**。"""
    ctx = ParseContext(project_id=opts.project_id, source_name=source_name,
                       source_override=opts.source_name, _cancel=opts.cancel_check)
    counters = Counters()
    conflicts_before = _open_value_conflicts(conn, opts.project_id)
    cancelled = False
    parse_error: str | None = None
    integrity: dict[str, Any] = {"ok": None, "reason": "未执行（dry-run）"}
    definition_drift: str | None = None
    badlines_path: str | None = None
    commits = 0

    with _JobTxn(conn, dry_run=opts.dry_run, batch=opts.batch_size) as txn:
        raw_id, locator_uri, storage_state = archive_file(conn, opts, path, digest, size)
        source_id = store.register_source(conn, name=source_name,
                                          authority_level=opts.authority_level,
                                          tool=str(parser_cls.tool), actor=opts.actor)
        # 先把 parser profile 登记**完整**（含 definition_hash）再开作业：
        # `ops.start_job` 内部的 register_parser 是 INSERT OR IGNORE，顺序反了
        # definition_hash 就永久留空（v3.2 §16.4 第 2 条）。
        store.register_parser(conn, fingerprint, profile_id=str(parser_cls.tool),
                              version=str(parser_cls.format_version),
                              definition_hash=definition_hash, actor=opts.actor)
        definition_drift = _definition_drift(conn, fingerprint, definition_hash)
        job_id = ops.start_job(conn, project_id=opts.project_id, kind="ingest",
                               coverage="partial",
                               coverage_scope={"file": path.name, "sha256": digest,
                                               "parser": fingerprint},
                               parser_fingerprint=fingerprint, actor=opts.actor)
        ctx.job_id = job_id
        _emit_progress(opts, ctx, {"event": "job_start", "job_id": job_id,
                                   "file": path.name, "parser": fingerprint,
                                   "source": source_name})
        existing_fields, existing_rels = _existing_facts(conn, opts.project_id)
        seen: set[tuple[str, tuple[str, ...]]] = set()
        evidence_cache: dict[int, int] = {}
        sent_bad_lines = 0

        try:
            for rec in parser_cls().parse(_tracked_lines(path, probe, ctx), ctx):
                counters.records += 1
                ctx.tick()
                key = _fact_key(rec)
                if key in seen:                       # 同一 job 内重复 → 一条贡献都不写
                    counters.skipped += 1
                    continue
                seen.add(key)
                evidence_id = _evidence_for_line(conn, opts, raw_id, ctx, evidence_cache)
                _write_record(conn, opts, job_id, source_id, evidence_id, rec,
                              _endpoint_kind(rec))
                if key in (existing_rels if key[0] == "relation" else existing_fields):
                    counters.updated += 1
                else:
                    counters.added += 1
                txn.checkpoint()
                if counters.records % opts.batch_size == 0:
                    # 与 checkpoint **同一个节奏**：进度汇报的粒度就是"已提交多少"，
                    # 不是"读了多少行" —— 后者会让进度条跑到 undo 追不上的位置。
                    fresh = ctx.bad_lines[sent_bad_lines:]
                    _emit_progress(opts, ctx, {
                        "event": "progress", "job_id": job_id, "file": path.name,
                        "records": counters.records, "line": ctx.current_line,
                        "counts": counters.as_dict(), "commits": txn.commits,
                        "bad_lines": fresh[:PROGRESS_BAD_LINE_LIMIT],
                        "bad_lines_total": len(ctx.bad_lines),
                    })
                    sent_bad_lines += len(fresh)
        except Cancelled as exc:
            cancelled = True
            ctx.notes.append(f"作业中断：{exc}（已提交部分保留，可 undo）")
        except Exception as exc:                      # 解析器崩了 → 可读报告，不是堆栈
            parse_error = f"{type(exc).__name__}: {exc}"
            ctx.notes.append(f"解析在第 {ctx.current_line} 行中止：{parse_error}")

        badlines_path = _write_badlines(opts, job_id, ctx)
        counters.bad_lines = len(ctx.bad_lines)
        counters.unknown_fields = sum(ctx.unknown_fields.values())

        # --- 阶段 8 收口：缓存重算（增量）---
        # 贡献流是事实，缓存行是投影 —— 不重算，事实就"写进去了但看不见"，
        # 而且 v_integrity_violations 会当场判 cache_understated。撤销同样是
        # 靠这条路径把缓存投影回事实，所以它与 ops.undo 走的是同一个函数。
        # 中断时也必须走到这里：已提交的部分要处于**一致**状态才谈得上可 undo。
        store.recompute(conn, delta=ops._recompute_subjects_of_job(  # noqa: SLF001
            conn, job_id))

        # --- 阶段 9/10：冲突检测 → 进 Inbox ---
        ops.refresh_observation_conflicts(conn, opts.project_id)
        counters.conflicts = max(0, _open_value_conflicts(conn, opts.project_id)
                                 - conflicts_before)

        state = "done"
        if cancelled:
            state = "partial"
        elif parse_error or counters.bad_lines:
            state = "partial"
        if opts.verify:
            integrity = _verify(conn)
            if not integrity["ok"]:
                state = "failed"
        ops.finish_job(conn, job_id, state=state, actor=opts.actor)
        commits = txn.commits

    # 收尾事件放在 `with` **之外**：此刻 COMMIT 已经落地，"进度说完了"与
    # "数据真的可读了"才是同一件事。放在里面会让订阅者在提交前就去查库。
    _emit_progress(opts, ctx, {"event": "job_done", "job_id": job_id,
                               "file": path.name,
                               "state": "cancelled" if cancelled else state,
                               "parse_error": parse_error,
                               "counts": counters.as_dict(),
                               "badlines_report": badlines_path,
                               "notes": list(ctx.notes)})

    report: dict[str, Any] = {
        "path": str(path), "sha256": digest, "byte_count": size,
        "state": "cancelled" if cancelled else state,
        "job_id": job_id, "raw_file_id": raw_id, "locator_uri": locator_uri,
        "storage_state": storage_state,
        "encoding": probe.as_dict(),
        "parser": fingerprint, "definition_hash": definition_hash, "source": source_name,
        "arbitration": arbitration,
        "counts": counters.as_dict(),
        "badlines_report": badlines_path,
        "notes": list(ctx.notes),
        "unknown_fields_detail": dict(sorted(ctx.unknown_fields.items())),
        "reconcile": {
            "records": counters.records,
            "counted": counters.added + counters.updated + counters.skipped,
            "ok": counters.records == counters.added + counters.updated + counters.skipped,
        },
        "integrity": integrity,
        "dry_run": opts.dry_run,
    }
    if parse_error:
        report["parse_error"] = parse_error
    if definition_drift:
        report["definition_drift"] = definition_drift
    if not opts.dry_run:
        report["db"] = _job_facts(conn, job_id)
    return report


def _evidence_for_line(conn: sqlite3.Connection, opts: IngestOptions, raw_id: int,
                       ctx: ParseContext, cache: dict[int, int]) -> int:
    """一行一条 evidence（locator 记行号，§5.3）。懒创建：只给真正产出事实的行建。"""
    lineno = int(ctx.current_line)
    if lineno not in cache:
        cache[lineno] = ops.add_evidence(
            conn, project_id=opts.project_id, raw_file_id=raw_id,
            locator={"line": lineno, "file": Path(ctx.source_name).name},
            snippet=ctx.current_line_text[:512], quality_class="high")
    return cache[lineno]


def _sample_text(path: Path, probe: Probe, limit: int = SAMPLE_BYTES) -> str:
    """嗅探样本：只读头部，按探测出的编码解。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(limit)
    except OSError:
        return ""
    if probe.encoding == "utf-16":
        return head.decode("utf-16", errors="replace")
    if probe.bom == "utf-8":
        head = head[3:]
    return head.decode(probe.encoding, errors="replace")


def _score_of(ranked: Sequence[dict[str, Any]], cls: Any) -> float | None:
    for item in ranked:
        if item["tool"] == str(cls.tool) and item["format_version"] == str(cls.format_version):
            return item["score"]
    return None


def _resolve_parser(parsers: Sequence[Any], adopted: dict[str, Any]) -> Any:
    for cls in parsers:
        if (str(cls.tool) == adopted["tool"]
                and str(cls.format_version) == adopted["format_version"]):
            return cls
    raise store.InvariantError(f"仲裁选中的解析器不在候选列表里：{adopted}")


def _definition_hash(cls: Any) -> str:
    """解析器定义的 hash —— 回答"同一 fingerprint 背后的实现有没有变过"。

    取**类源码**而不是模块文件：一个模块里可能有多个解析器。取不到源码时
    退化为类名，让报告里一眼看得出"这次没算成"。
    """
    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):
        return "unavailable:" + cls.__qualname__
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def _definition_drift(conn: sqlite3.Connection, fingerprint: str,
                      expected: str) -> str | None:
    row = conn.execute("SELECT definition_hash FROM parser_profiles WHERE fingerprint=?",
                       (fingerprint,)).fetchone()
    if row is None or row["definition_hash"] is None:
        return None if row is not None else f"parser_profiles 里找不到 {fingerprint}"
    actual = str(row["definition_hash"])
    if actual != expected:
        return (f"parser_profiles.{fingerprint} 已登记的 definition_hash {actual[:12]}… "
                f"与本次实现 {expected[:12]}… 不一致 —— 同一 fingerprint 背后的实现变过")
    return None


def _already_imported(conn: sqlite3.Connection, project_id: int, digest: str,
                      fingerprint: str) -> dict[str, Any] | None:
    """该 sha256 是否已被**同一 fingerprint** 的作业导入过（幂等判定的唯一依据）。

    只认"跑完且未被撤销"的作业（`applied=1`）：被 undo 过的文件重新导入是正当的
    redo / reparse 行为，不能算重复。
    """
    row = conn.execute(
        "SELECT j.id AS job_id FROM raw_files rf"
        "  JOIN evidence e ON e.raw_file_id = rf.id"
        "  JOIN contributions c ON c.evidence_id = e.id"
        "  JOIN jobs j ON j.id = c.job_id"
        " WHERE rf.project_id=? AND rf.sha256=? AND j.applied=1 AND j.parser_fingerprint=?"
        " LIMIT 1", (project_id, digest, fingerprint)).fetchone()
    if row is None:
        return None
    return {"job_id": int(row["job_id"])}


def _verify(conn: sqlite3.Connection) -> dict[str, Any]:
    """导入收尾的**不变量**校验（不是文件校验）。

    只跑不变量层：`v_integrity_violations` + `PRAGMA foreign_key_check` —— 它们回答
    "这次导入有没有把库写坏"。`store.integrity_check` 里还有一条
    `PRAGMA integrity_check`，它回答的是另一个问题（库**文件**有没有损坏），
    代价是整个文件的全量扫描（实测 11 万观测时 3.2s，占该函数 4.1s 的 78%），
    而且与某一次导入无关 —— 它属于 `pipeline check <db>` / 2.6 的 `magpie doctor`。
    """
    try:
        violations = [dict(r) for r in conn.execute("SELECT * FROM v_integrity_violations")]
        fk = [dict(r) for r in conn.execute("PRAGMA foreign_key_check")]
    except Exception as exc:                            # 检查器自己坏了也要可读
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": not violations and not fk,
            "reason": "无违规" if not violations and not fk else "存在违规",
            "level": "invariants",
            "violations": len(violations), "foreign_key_violations": len(fk),
            "sample": violations[:5]}


# ---------------------------------------------------------------------------
# 「可读报告」的三条出口：无解析器 / 二进制 / 读不到文件
# ---------------------------------------------------------------------------


def _problem_report(path: Path, reason: str) -> dict[str, Any]:
    return {"path": str(path), "state": "failed", "job_id": None, "reason": reason,
            "counts": Counters().as_dict()}


def _binary_report(path: Path, digest: str, size: int, probe: Probe,
                   arbitration: dict[str, Any]) -> dict[str, Any]:
    """二进制输入：跳过并给出可读理由。

    **不归档**：raw_files 的语义是"可复算原文的锚点"，而这里根本没有事实行
    会指向它；为一个不解析的文件建作业与归档，只会制造没有下游的孤儿行。
    """
    return {
        "path": str(path), "sha256": digest, "byte_count": size,
        "state": "skipped", "job_id": None,
        "skip_reason": (f"二进制输入（{probe.binary}）：跳过，不解析也不归档"
                        f"（归档对象只针对能产出事实的原文）"),
        "encoding": probe.as_dict(), "parser": None,
        "arbitration": arbitration, "counts": Counters().as_dict(),
    }


def _no_parser_report(conn: sqlite3.Connection, opts: IngestOptions, path: Path,
                      digest: str, size: int, probe: Probe,
                      arbitration: dict[str, Any]) -> dict[str, Any]:
    """嗅探仲裁不采用任何解析器 → 进 Inbox 待人工确认（§5.4）。

    inbox 的 subject 是 NOT NULL 的多态引用（entity/observation/relation），而
    仲裁失败时**还没有任何实体可指**，因此用 `entity:0` 作为项目级占位，并把
    候选排名与理由放进 payload。这是"宁可留下一条待办，也不猜一个解析器"的代价。
    """
    job_id: int | None = None
    item_id: int | None = None
    if not opts.dry_run:
        with store.atomic(conn):
            job_id = ops.start_job(conn, project_id=opts.project_id, kind="ingest",
                                   coverage="unknown",
                                   coverage_scope={"file": path.name, "sha256": digest},
                                   actor=opts.actor)
            item_id, _ = ops._open_inbox(          # noqa: SLF001 — 复用唯一 inbox 写入口
                conn, project_id=opts.project_id, kind="manual_required",
                subject_kind="entity", subject_id=0, values=[path.name], sources=[],
                job_id=job_id, severity="medium",
                payload={"reason": arbitration["reason"], "file": path.name,
                         "sha256": digest, "ranking": arbitration["ranking"]})
            ops.finish_job(conn, job_id, state="partial", actor=opts.actor)
    return {
        "path": str(path), "sha256": digest, "byte_count": size,
        "state": "needs_confirmation", "job_id": job_id, "inbox_item_id": item_id,
        "skip_reason": arbitration["reason"],
        "encoding": probe.as_dict(), "arbitration": arbitration,
        "counts": Counters().as_dict(),
    }


__all__ = [
    "Cancelled", "Counters", "IngestOptions", "ParseContext", "Probe",
    "SNIFF_ADOPT_MARGIN", "SNIFF_ADOPT_SCORE",
    "arbitrate", "archive_file", "discover", "ingest_file", "ingest_paths",
    "iter_lines", "load_parsers", "probe_file", "sha256_file", "sniff_rank",
]
