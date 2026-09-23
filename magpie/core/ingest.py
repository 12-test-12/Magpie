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

import contextlib
import hashlib
import importlib
import inspect
import io
import json
import os
import pkgutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import normalize, ops, scan, store

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

#: 内存里保留多少条坏行**明细**（计数不受它限制，见 `ParseContext.bad_line_total`）。
BAD_LINE_DETAIL_LIMIT = 5000

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

#: 目录扫描认哪些扩展名。**公开**（不带下划线）：CLI 的报错要把它念给用户听
#: （"目录按扩展名过滤，`.db`/`.png` 不算输入"），而念一份手抄的副本迟早会
#: 与这里分叉 —— 报名单和用名单的必须是同一份。
INPUT_SUFFIXES = (".txt", ".jsonl", ".ndjson", ".json", ".log", ".csv", ".tsv", ".out")

#: 两个字段命名空间。**取值与 schema 的 CHECK 逐字一致**
#: （`contributions.field_namespace IN ('entity_field','observation_field')`），
#: 也与会写它们的 `ops.add_entity_field` / `ops.add_observation` 一致。
#: 之所以在 ingest 里再写一份：added / updated 的判定是拿**解析器产出的记录**
#: 去和**库里读回来的行**比对，两边的键必须逐字段同形，而这条知识在文件里
#: 只以字面量的形式存在于写入侧。
ENTITY_NAMESPACE = "entity_field"
OBSERVATION_NAMESPACE = "observation_field"


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
    #: 机器可读报告落盘目录（2.6）。**追加参数，默认 None = 不落盘** —— 不传它时
    #: 本模块的行为与从前逐字节一致。传了它，每导入一个文件就在
    #: `<report_dir>/<job_id>.json`（没有 job 时用 sha256 前缀）留一份 `ingest_file`
    #: 的原样报告。理由见 `_write_report`。
    report_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.archive not in ("archived", "external"):
            raise ValueError(f"archive 只支持 archived / external，收到 {self.archive!r}")
        if self.batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，收到 {self.batch_size!r}")
        # 绝对化：归档 locator 要写成 file:// URI，相对路径表达不了（也不该表达）
        self.data_dir = Path(self.data_dir).resolve()
        if self.report_dir is not None:
            self.report_dir = Path(self.report_dir).resolve()


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
    #: 坏行**明细**（最多 `BAD_LINE_DETAIL_LIMIT` 条，内存上限）。
    bad_lines: list[dict[str, Any]] = field(default_factory=list)
    #: 坏行**总数**。明细有上限，计数没有 —— 报告里的 `counts.bad_lines` 读的是它。
    #: 审核 LARI-21: 旧实现把"内存里留了多少条"当成计数，于是 10001 条坏行的文件
    #: 报告 `bad_lines=5000`，落盘报告的头也写"共 5000 条"—— 质量信号在撒谎，
    #: 而且**没有任何信号**说它被截断了。
    bad_line_total: int = 0
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
        # 计数**永远**加一；只有明细受内存上限约束（5000 条足够看清"坏在哪"）。
        self.bad_line_total += 1
        if len(self.bad_lines) < BAD_LINE_DETAIL_LIMIT:
            #  raw 是**行原文的副本**，而坏行明细是判据 ① 点名的落盘物
            #  （`reports/<job>.badlines.txt`）—— 先掩码再截断：反过来的话一个
            #  跨在 400 字符边界上的凭据会被截成半截明文。
            self.bad_lines.append(
                {"line": int(lineno),
                 #  reason 也会过一道：坏行理由偶有输入插值（`…不是合法域名：
                 #  'x'`），掩码对正常文案是恒等变换，对插进去的凭据是救命的。
                 "reason": str(normalize.redact_secrets(str(reason))),
                 "raw": str(normalize.redact_secrets(str(raw)))[:400]})

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
    """7 个计数。恒等式：added + updated + skipped == records。

    `secrets`（4.2）与那六个不是同一类东西，所以单独说明：前六个数的是**这份
    文件写出了什么事实**，而 `secrets` 数的是**扫描器在原文里认出了几个凭据**
    —— 它随后会被写成 `secret` 实体（进 `records`），但那个数回答的是另一个
    问题（"这份文件里有没有凭据、有几个"）。两个都留着是因为它们都要被看见：
    `records` 里的那几条与别的记录混在一起，单看它答不出"这次导入有没有扫到
    凭据"。
    """

    added: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    bad_lines: int = 0
    unknown_fields: int = 0
    records: int = 0
    secrets: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"added": self.added, "updated": self.updated, "skipped": self.skipped,
                "conflicts": self.conflicts, "bad_lines": self.bad_lines,
                "unknown_fields": self.unknown_fields, "records": self.records,
                "secrets": self.secrets}


# ---------------------------------------------------------------------------
# 阶段 1 · 发现文件
# ---------------------------------------------------------------------------


def discover(paths: Sequence[str | Path], *, recursive: bool = True
             ) -> tuple[list[Path], list[str]]:
    """把"用户给的一串路径"展开成文件列表。返回 (files, problems)。

    目录按扩展名过滤（`INPUT_SUFFIXES`），避免把 .db / .png 之类拖进来。
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
    return path.suffix.lower() in INPUT_SUFFIXES


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
                  sha256: str, byte_count: int, storage_state: str,
                  custody: str = "stored") -> int:
    """写 `raw_files`。

    **为什么不用 `ops.add_raw_file`**：它的签名是 `content: str`，要求调用方先把
    整个文件读成字符串 —— 与"大文件不 read() 全量"直接冲突。这里写同一张表、
    同一组列，语义等价。`ops` / `store` 的任何既有签名保持原样不动。

    `custody`（4.1 起，迁移 011）：原文由谁持有。它与 `storage_state` **正交**
    —— 后者说"它还在不在"，这一列说"它从来就不在我手里"。`--archive external`
    的两次落点（真跑与干跑）都必须带上它，否则外部文件会被当成归档文件，
    而 `ops.evidence_capability` 那条链会给出 `stored` 的结论（"可恢复"）——
    4.1 点名不许这么干。
    """
    cur = conn.execute(
        "INSERT INTO raw_files(project_id, locator_uri, sha256, byte_count,"
        " storage_state, custody, created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, locator_uri, sha256, byte_count, storage_state, custody,
         store.now()),
    )
    return int(cur.lastrowid)


def archive_file(conn: sqlite3.Connection, opts: IngestOptions, path: Path,
                 sha256: str, size: int) -> tuple[int, str, str, str]:
    """归档：返回 (raw_file_id, locator_uri, storage_state, custody)。

    archived —— 复制进 `<data-dir>/raw/<前两位>/<sha256>`（按内容寻址，天然去重），
    源文件此后可删；external —— **只记原路径（+ sha256 + 大小），正文一个字节
    都不入库**，文件原地不动。

    ⚠ **干跑不复制**（2.6）。这一条以前是漏的：`_JobTxn` 的 ROLLBACK 只回滚
    **数据库**，而复制发生在事务之外 —— 于是 `--dry-run` 会在磁盘上留下一份
    原文，一份被明确要求"先别动"的导入照样能把几个 GB 写进去。报告里那句
    `storage_state="archived"` 是对真跑结果的**预测**（干跑的整份报告都是），
    所以这里返回的还是那个预测值，只是不再真的落盘。

    与 `magpie init --dry-run` 不建 0 字节的 magpie.db 是同一条纪律：干跑的
    全部价值在于"什么都没发生"这句话可以被无条件相信。
    """
    if opts.archive == "external":
        # 「正文不入库」在这里是**结构上**成立的，不是一句约定：本函数只往
        # `raw_files` 写四件东西（路径 / sha256 / 大小 / 状态），文件内容从头到
        # 尾没有被读进来过（上面算 sha256 时是流式的，见 `ingest_file`）。
        locator_uri = path.resolve().as_uri()
        raw_id = _add_raw_file(conn, project_id=opts.project_id, locator_uri=locator_uri,
                               sha256=sha256, byte_count=size, storage_state="present",
                               custody="external")
        return raw_id, locator_uri, "present", "external"

    dest_dir = opts.data_dir / "raw" / sha256[:2]
    dest = dest_dir / sha256
    if opts.dry_run:
        return (_add_raw_file(conn, project_id=opts.project_id,
                              locator_uri=dest.as_uri(), sha256=sha256,
                              byte_count=size, storage_state="archived"),
                dest.as_uri(), "archived", "stored")
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
    return raw_id, dest.as_uri(), "archived", "stored"


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
    """把一条 Raw* 记录折成 (类别, 键)。键只用 **(kind, key) 引用**，不用 DB id。

    键必须与 `_existing_facts` 的键**逐字段同形** —— 后者是从库里读回来的
    `(field_namespace, alias_key, semantic_field, field_value)` 四元组。两个
    地方的差别就是 added / updated 的差别，而那个差别没有任何测试会替你发现。

    两处刻意的口径（审核 LARI-21 遗留项②）:

      * **namespace 进键**。`entity_field`（`RawEntity` 的 display_name）与
        `observation_field`（`RawObservation`）是**两个字段命名空间**
        （schema 的 CHECK 就是这两个值）。旧键只有 (key, field, value)，于是
        同一实体的 `display_name` 与一条同名字段的观测会撞成同一个键 ——
        一条**新观测**有机会被计成 `updated`。库里不受影响（两者写进不同的
        行），`records` 也守恒，所以它只错在报告上；而"报告说更新了、其实
        新增了一条"正是最难查的那类错。
      * **value 过 `_value_text`**。`_write_record` 落库前也走它：dict / list
        会被折成 canonical JSON。键里留原始 Python 对象的话，结构化值与它
        在库里的文本永远不相等 —— 那一条会**永远**被算成 added。
    """
    if _is_relation(rec):
        return "relation", (str(rec.src[1]), str(rec.rel), str(rec.dst[1]))
    if _is_observation(rec):
        return "field", (OBSERVATION_NAMESPACE, str(rec.entity_ref[1]),
                         str(rec.type), _value_text(rec.value))
    return "field", (ENTITY_NAMESPACE, str(rec.key), "display_name",
                     _value_text(rec.display or rec.key))


def _is_observation(rec: Any) -> bool:
    return hasattr(rec, "entity_ref") and hasattr(rec, "value") and hasattr(rec, "type")


def _is_relation(rec: Any) -> bool:
    return hasattr(rec, "src") and hasattr(rec, "dst") and hasattr(rec, "rel")


def _existing_facts(conn: sqlite3.Connection, project_id: int) -> tuple[set, set]:
    """导入前数据层里已存在的事实键（用于判定 added / updated）。

    键用 **alias_key / entity_key** 而不是自增 id —— 解析器只产出 (kind, key)
    引用，管线这一侧也必须只用 key，撤销之后结论才仍然说得通。

    键的形状必须与 `_fact_key` **逐字段同形**，否则比对必然落空（每一项都被
    算成新增）或误中（新增被算成更新）。`field_namespace` 是其中一员：它是
    schema 里真实存在的列，也真的区分两种字段 —— 读少了它，`entity_field`
    的 display_name 与同名 `observation_field` 就会撞成一个键。

    **两半都必须只算"还活着"的事实**（这就是 `v_field_owner` 与
    `v_active_contributions` 在这里出现的原因）。字段那一半走 `v_field_owner`，
    于是被 undo 抑制掉的贡献自动出局；关系那一半一度只查了 `relations` 表与
    别名，**没有任何活跃性判据** —— `relations` 行在 undo 里是**不删**的
    （撤销写的是抑制边，事实层只追加），于是 undo 之后那几条关系仍然"看得见"，
    重导时被算成 `updated`，而库里一条活跃事实都没有。判据与字段那一半对齐：
    关系要有**活跃的关系贡献**才算数 —— 用贡献流而不是 `relations.is_current`
    缓存列，因为缓存是投影、贡献才是事实（本模块反复重申的那条纪律）。
    """
    fields: set[tuple[str, str, str, str]] = set()
    for row in conn.execute(
        "SELECT c.field_namespace AS ns, a.alias_key AS k,"
        "       c.semantic_field AS f, c.field_value AS v"
        "  FROM v_field_owner c"
        "  JOIN entity_alias a ON a.entity_id = c.owner_id AND a.project_id = c.project_id"
        " WHERE c.project_id = ? AND a.revoked_by_job_id IS NULL", (project_id,)
    ):
        fields.add((str(row["ns"]), str(row["k"]), str(row["f"]), str(row["v"])))
    rels: set[tuple[str, str, str]] = set()
    for row in conn.execute(
        "SELECT sa.alias_key AS s, r.rel AS rel, da.alias_key AS d"
        "  FROM relations r"
        "  JOIN v_active_contributions c"
        "    ON c.contribution_kind = 'relation' AND c.subject_kind = 'relation'"
        "   AND c.subject_id = r.id"
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

    **所有值在这一层过 `normalize.redact_secrets`**（4.2 判据 ①，LARI-37 向量 A）：
    观测值与实体展示值是凭据最容易"以别的身份"落库的地方 —— 最典型的是 URL
    查询串：`path` 观测按"原始路径保留证据"的裁定写库，于是 `?api_key=xxx`
    的原文跟着观测值进了 `observations.value` / `contributions.field_value`。
    解析器只读文件（PARSE-RO），掩码是写入口的职责。**身份键（entity_key）不
    掩** —— 它是事实的主键，动它等于改身份；而现有所有 key 函数（endpoint 的
    `path_template` 丢查询串、flow 的 5 元组、secret 的 hash）本来就不含明文。
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
                         value=str(normalize.redact_secrets(
                             str(rec.display or rec.key))),
                         authorship="tool")


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
    """值一律以文本入库（observations.value 是 TEXT）。结构化值走 canonical JSON。

    **文本化之后过一遍 `redact_secrets`**（4.2 判据 ① 的 chokepoint 之一）：
    这是所有观测值落库的唯一出口 —— `path` 观测的查询串、`client` 观测的
    URL、conflict payload 引用的值，全从这里过。掩码与 `secret` 实体用同一个
    `mask_secret`，同一个凭据在两处长得一样，人对得上号。
    """
    if isinstance(value, (dict, list, tuple)):
        text = store.canonical_json(value)
    else:
        text = str(value)
    return str(normalize.redact_secrets(text))


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

    **`shared=True` 时本对象不自持事务**（干跑，边界在上一级）
    ----------------------------------------------------------
    上面那句"**全程**一个事务"以前是句空话：`_JobTxn` 是**按文件**实例化的，
    每个文件结束时各自 ROLLBACK，于是第 N 个文件看不见前 N-1 个文件刚写下的
    事实。真跑里每个文件收尾会 COMMIT，第 2 个文件**看得见**第 1 个文件 ——
    干跑却看不见，同一条命令行两边的 `added` / `updated` 于是对不上（实测
    `import subfinder.txt httpx.jsonl`：真跑 added=193/updated=12，干跑
    added=205/updated=0）。干跑的全部价值是"预测真跑"，它自己没有资格另立一套
    可见性。

    所以干跑的每个文件一律标 `shared=True`：不 BEGIN、不 COMMIT、不 ROLLBACK
    ——本文件的写入因此对**同一事务里的后续文件与后续 step** 可见。边界由谁开？
    见 `dry_run_scope`：**一条命令行**。`magpie import` 一条命令就是一轮
    `ingest_paths`，边界落在 `ingest_paths`；`pipeline run` 是一条命令、多个
    step，边界落在 `run_pipeline`（每个 step 各是一次 `ingest_paths` 调用，
    各自开边界的话第 2 个 step 又看不见第 1 个了）。真跑永远 `shared=False`
    （事务边界就是文件边界，取消语义依赖它）。
    """

    def __init__(self, conn: sqlite3.Connection, *, dry_run: bool, batch: int,
                 shared: bool = False) -> None:
        self.conn = conn
        self.dry_run = dry_run
        self.batch = batch
        #: 事务由调用方（`ingest_paths`）开与关，本文件不碰边界
        self.shared = shared
        self._since_commit = 0
        self.commits = 0

    def __enter__(self) -> "_JobTxn":
        if not self.shared:
            self.conn.execute("BEGIN IMMEDIATE")
        return self

    def checkpoint(self) -> None:
        self._since_commit += 1
        if self.dry_run or self.shared or self._since_commit < self.batch:
            return
        self.conn.execute("COMMIT")
        self.conn.execute("BEGIN IMMEDIATE")
        self._since_commit = 0
        self.commits += 1

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.shared:
            return False               # 边界归 `ingest_paths`，见上
        self.conn.execute("ROLLBACK" if (self.dry_run or exc_type is not None) else "COMMIT")
        return False


@contextlib.contextmanager
def dry_run_scope(conn: sqlite3.Connection, enabled: bool) -> Iterator[bool]:
    """干跑的**命令级**事务边界：整段跑在一个事务里，末尾一律 ROLLBACK。

    yield 出去的布尔值是"本对象是否真的持着事务" —— 调用方（`run_pipeline`）
    必须把它原样传给 `ingest_paths(..., _shared_txn=...)`，别在两处各判一次。

    为什么需要"命令级"
    ------------------
    `ingest_paths` 自己的边界是**一次调用**。一次调用就是一条命令行时（`magpie
    import`、GUI 导入、API 导入）那是对的，但 `pipeline run` 是**一条命令、多个
    step**（`magpie/core/pipeline.py` 每个 step 调一次 `ingest_paths`），于是：

      * 真跑：每个 step 各自收尾 COMMIT，第 2 个 step **看得见**第 1 个 step
        刚落下的事实；
      * 干跑（修这条之前）：每次调用各自 BEGIN/ROLLBACK，第 2 个 step **看不见**
        —— 同一条命令行，两端的库状态在干跑/真跑下就已经不同了。

    后果不是"数字难看"，而是 `pipeline run --dry-run` 在**多 step 的 flow 上低报
    `updated`**：它把"重述第 1 个 step 已有事实"的那些记录全算成新增。干跑唯一的
    价值就是预测真跑，一个会说假话的干跑比没有干跑更糟 —— 人会照着它的数做判断。

    所以命令级入口用本对象把**整条命令**包起来。修完之后，`pipeline` 入口的
    "干跑 totals == 真跑 totals"是一条真正的端到端断言，不需要任何特判。

    真跑时它是**空的**（`enabled=False`，不开事务、不碰边界）—— 真跑的事务边界
    就是每个文件的边界，取消语义依赖它（见 `_JobTxn`）。
    """
    if not enabled:
        yield False
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield True
    finally:
        # 与 `ingest_paths` 的收尾同一条理由：不能误回滚别人的事务。BEGIN 失败
        # 时异常会直接抛出，这里判一次 `in_transaction` 就不会顺手把外面的工作
        # 也撤掉。
        if conn.in_transaction:
            conn.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# 冲突检测 / 坏行落盘 / 数据层反查
# ---------------------------------------------------------------------------


def _open_value_conflicts(conn: sqlite3.Connection, project_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND kind='value_conflict'"
        " AND state='open'", (project_id,)).fetchone()[0])


def _write_badlines(opts: IngestOptions, job_id: int, ctx: ParseContext) -> str | None:
    """坏行明细 → `<data-dir>/reports/<job_id>.badlines.txt`（§5.6）。

    头部写的是**真实条数** `bad_line_total`，并如实交代明细被截到多少条 ——
    "共 5000 条"在只保留了 5000 条明细、而实际有 10001 条时是一句假话。
    """
    if not ctx.bad_lines:
        return None
    reports = opts.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{job_id}.badlines.txt"
    kept = len(ctx.bad_lines)
    truncated = "" if kept >= ctx.bad_line_total else f"，此处只保留前 {kept} 条明细"
    lines = [f"# job {job_id} 坏行明细（共 {ctx.bad_line_total} 条{truncated}）"]
    if kept < ctx.bad_line_total:
        ctx.notes.append(
            f"坏行明细只保留了前 {kept} 条（实际 {ctx.bad_line_total} 条）——"
            f"计数不受影响，counts.bad_lines 是真实条数")
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
                 paths: Sequence[str | Path], *,
                 _shared_txn: bool = False) -> dict[str, Any]:
    """导入一批文件（每个文件一个 job）。返回聚合报告。

    **干跑时整轮共用一个事务**（`shared=True`，见 `_JobTxn`）：真跑里第 N 个
    文件看得见前 N-1 个文件已提交的事实，干跑必须看得见同样多，否则同一条
    命令行的 totals 会与真跑对不上 —— 而"干跑预测真跑"正是它唯一的价值。
    事务边界在这里开、也在这里关：无论中途哪个文件失败或中断，末尾一律
    ROLLBACK（干跑的硬性标准：跑完一行都不留）。

    `_shared_txn` 是**内部**参数，为真时表示"边界不归我，调用方持着它" ——
    `pipeline run` 一条命令多个 step，边界必须落在整条命令上（见 `dry_run_scope`），
    否则第 2 个 step 看不见第 1 个 step 写下的事实。默认 `False`：一次调用一条
    命令的入口（CLI / GUI / API 的导入）行为与以前逐字节相同。
    """
    files, problems = discover(paths)
    parsers, source_pkg = load_parsers()
    reports: list[dict[str, Any]] = []
    #: 干跑才谈"谁来持边界"：真跑的事务边界就是文件边界，取消语义依赖它。
    owns_txn = opts.dry_run and not _shared_txn
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for index, path in enumerate(files):
            if opts.cancel_check is not None and opts.cancel_check():
                reports.append({"path": str(path), "state": "cancelled", "job_id": None,
                                "reason": "中断：该文件尚未开始",
                                "counts": Counters().as_dict()})
                continue
            _emit_progress(opts, None, {"event": "file_start", "file": path.name,
                                        "path": str(path), "index": index,
                                        "total": len(files)})
            try:
                report = ingest_file(conn, opts, path, parsers=parsers,
                                     _shared_txn=opts.dry_run)
            except Exception as exc:        # 单个文件失败不该毁掉整轮导入
                report = _problem_report(path, f"{type(exc).__name__}: {exc}")
            reports.append(report)
            _emit_progress(opts, None, {"event": "file_done", "file": path.name,
                                        "path": str(path), "index": index,
                                        "total": len(files), "job_id": report.get("job_id"),
                                        "state": report.get("state"),
                                        "counts": report.get("counts")})
    finally:
        # 不能在 BEGIN 失败时误回滚别人的事务，所以判 `in_transaction`。
        if owns_txn and conn.in_transaction:
            conn.execute("ROLLBACK")
    totals: dict[str, int] = {}
    for rep in reports:
        for name, value in rep.get("counts", {}).items():
            totals[name] = totals.get(name, 0) + int(value)
    #  再过一道 `_redact_report`（4.2 判据 ①）：单文件报告在 `ingest_file` 里
    #  已经掩过，但这一层还有两个自己的面 —— `problems`（`discover` 的提示语里
    #  嵌着原样路径）与任何聚合时拼进去的字符串。掩码幂等（掩完不再命中），
    #  所以二次清洗无害。
    return _redact_report({
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
    })


def _redact_report(obj: Any) -> Any:
    """递归把报告里的**所有字符串**过一遍 `redact_secrets`（4.2 判据 ①）。

    报告是判据 ① 点名的落盘物（`reports/*.json`），也是 CLI stdout 的数据源
    （`cmd_import` 把它整个塞进 JSON 信封）—— 在这一处掩码，落盘与 stdout
    两个面一次收口。为什么递归扫**全部**字符串而不是挑字段：报告的形状随
    解析器与阶段演进（`skip_reason` 的措辞、`arbitration.ranking` 的内容……），
    挑字段的清单必然过期，而"某个新字段把文件名带出去了"没有任何症状。
    掩码对不含凭据的字符串是恒等变换，所以 sha256、指纹、计数一个都不会变。

    只掩**值**不掩**键**：键是报告的 schema（`path` / `sha256` 这些名字），
    掩键会让下游按名取值的代码静默取空。
    """
    if isinstance(obj, str):
        return normalize.redact_secrets(obj)
    if isinstance(obj, dict):
        return {key: _redact_report(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_redact_report(item) for item in obj]
    return obj


def ingest_file(conn: sqlite3.Connection, opts: IngestOptions, path: Path, *,
                parsers: Sequence[Any] | None = None,
                _shared_txn: bool = False) -> dict[str, Any]:
    """一个文件的完整管线（§5.1 的 12 个阶段）。失败也返回可读报告，不抛堆栈。

    顺手按 `opts.report_dir` 落一份机器可读的副本（默认 None = 不落，见
    `_write_report`）。落盘失败**不影响**这条报告的返回：一份写不下去的诊断
    副本，不该把一次成功的导入变成失败。

    `_shared_txn` 是**内部**参数：为真时本文件不持事务边界，由 `ingest_paths`
    开的整轮干跑事务兜着（见 `_JobTxn`）。单文件调用（含 CLI 的 `--dry-run`）
    保持默认，自持事务、末尾 ROLLBACK。
    """
    report = _redact_report(
        _ingest_file(conn, opts, path, parsers=parsers, _shared_txn=_shared_txn))
    _write_report(opts, report)
    return report


def _write_report(opts: IngestOptions, report: dict[str, Any]) -> str | None:
    """把这份报告写进 `<report_dir>/<job_id>.json`（无 job 时用 sha256 前缀）。

    为什么要有它（2.6）: 报告是导入**唯一**的交付通道，但一次 `magpie import`
    结束、终端翻页之后它就没了。而 §5.6 的两个质量信号里，坏行至少还有
    `.badlines.txt` 明细，**未知字段却没有任何持久化的痕迹** —— 它只活在那一次
    调用的内存里。`magpie doctor` 要回答"哪个来源的格式漂了"，就必须有人在
    导入当时把它记下来（`unknown_fields_detail`），事后无迹可查。

    落在 `reports/` 里而不是新开一个目录：坏行明细已经在那里，同一个目录放
    同一件事的两种粒度（人读的 `.txt` / 机器读的 `.json`），比散在两处好找。

    刻意**不为 dry-run 落盘**：干跑没有 job，留下来的文件会声称一次并未发生的
    导入发生过 —— 那是真的副作用，不是"什么都没做"（与 `init --dry-run` 不建
    文件的理由同一条）。
    """
    if opts.report_dir is None or opts.dry_run:
        return None
    job_id = report.get("job_id")
    if job_id is not None:
        name = f"{int(job_id)}.json"
    else:
        name = f"sha-{str(report.get('sha256') or 'unknown')[:16]}.json"
    path = opts.report_dir / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    except OSError:                                              # pragma: no cover
        return None
    return str(path)


def _ingest_file(conn: sqlite3.Connection, opts: IngestOptions, path: Path, *,
                 parsers: Sequence[Any] | None = None,
                 _shared_txn: bool = False) -> dict[str, Any]:
    """上一条的实现体（`ingest_file` 负责落盘副本，这里只算报告）。"""
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
                    _definition_hash(parser_cls), source_name, arbitration,
                    _shared_txn=_shared_txn)


def _run_job(conn: sqlite3.Connection, opts: IngestOptions, path: Path, digest: str,
             size: int, probe: Probe, parser_cls: Any, fingerprint: str,
             definition_hash: str, source_name: str,
             arbitration: dict[str, Any], *, _shared_txn: bool = False) -> dict[str, Any]:
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

    with _JobTxn(conn, dry_run=opts.dry_run, batch=opts.batch_size,
                 shared=_shared_txn) as txn:
        raw_id, locator_uri, storage_state, custody = archive_file(
            conn, opts, path, digest, size)
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
                               #  file 过掩码（4.2 判据 ①，向量 B）：coverage_scope
                               #  是落库的诊断信息，不是路径指针 —— 幂等判定走
                               #  `_already_imported` 的 sha256 + fingerprint，与
                               #  这一格无关，掩掉文件名不损失任何功能。
                               coverage_scope={"file": str(normalize.redact_secrets(
                                                   path.name)),
                                               "sha256": digest,
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
            # 逐记录的 source 覆盖（4.3 的指纹引擎：观测的 source = 规则名）。
            # 没有覆盖的记录照旧用作业 source —— 其余 13 个解析器一条都不会
            # 设置 `rec.source`，这段对它们是零成本的直通。
            extra_sources: dict[str, int] = {}
            for rec in parser_cls().parse(_tracked_lines(path, probe, ctx), ctx):
                counters.records += 1
                ctx.tick()
                key = _fact_key(rec)
                if key in seen:                       # 同一 job 内重复 → 一条贡献都不写
                    counters.skipped += 1
                    continue
                seen.add(key)
                rec_source_id = source_id
                rec_source = getattr(rec, "source", None)
                if rec_source:
                    name = str(rec_source)
                    if name not in extra_sources:
                        extra_sources[name] = store.register_source(
                            conn, name=name,
                            authority_level=opts.authority_level,
                            tool=str(parser_cls.tool), actor=opts.actor)
                    rec_source_id = extra_sources[name]
                evidence_id = _evidence_for_line(conn, opts, raw_id, ctx, evidence_cache)
                _write_record(conn, opts, job_id, rec_source_id, evidence_id, rec,
                              _endpoint_kind(rec))
                # **取 `key[1]`，不是 `key`**：`_fact_key` 返回的是 `(类别, 键)`
                # 这样一个二元组，而 `_existing_facts` 给的两半分别是不带标签的
                # 三元组（关系）与四元组（字段）。拿整个二元组去比对，两边**永远**
                # 对不上 —— `updated` 成了走不到的分支，每一次重导都被算成 `added`。
                # 判据的形状写在 `_fact_key` / `_existing_facts` 的 docstring 里，
                # `test_datalayer_audit` 的 #7 用例也正是拿 `[1]` 去对撞的。
                if key[1] in (existing_rels if key[0] == "relation" else existing_fields):
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
                        "bad_lines_total": ctx.bad_line_total,
                    })
                    sent_bad_lines += len(fresh)
        except Cancelled as exc:
            cancelled = True
            ctx.notes.append(f"作业中断：{exc}（已提交部分保留，可 undo）")
        except Exception as exc:                      # 解析器崩了 → 可读报告，不是堆栈
            parse_error = f"{type(exc).__name__}: {exc}"
            ctx.notes.append(f"解析在第 {ctx.current_line} 行中止：{parse_error}")

        # §5.3 的契约要求解析器"在行边界检查 cancelled() 并尽快返回" —— 于是
        # 中断时解析器可能是**正常结束**（StopIteration），而不是抛 Cancelled。
        # 只看异常的话，那次导入会被报成 state='done'：数据少了一半，报告说完成。
        # 这里回读同一个标志，让"解析器自己停的"与"管线拦下的"落进同一个结论。
        if not cancelled and ctx.cancelled():
            cancelled = True
            ctx.notes.append(
                f"作业中断：解析器在第 {ctx.current_line} 行按 cancelled() 提前返回"
                f"（已提交部分保留，可 undo）")

        badlines_path = _write_badlines(opts, job_id, ctx)
        counters.bad_lines = ctx.bad_line_total      # 计数不随明细上限截断
        counters.unknown_fields = sum(ctx.unknown_fields.values())

        # --- 阶段 7b：敏感串扫描（4.2）---
        # 读的是**源文件**（不是解析器的输出），所以 `stored` 与 `external` 两条
        # 归档路径覆盖一致 —— 不存在"外部文件扫不到"这回事。中断或解析器崩了时
        # 跳过：那意味着文件可能没读完，而"扫了一半"不是一个有意义的报告。
        # 扫描器不选解析器 —— 同一份 HAR 无论被 har 还是被 curl 认领，扫出来
        # 的结果必须一样，所以它挂在"解析之后"而不是"解析器 X 的路径里"。
        scan_result = None
        if not cancelled and not parse_error:
            scan_result = _scan_and_write(conn, opts, job_id, raw_id, path,
                                          probe, counters)

        # --- 阶段 8 收口：缓存重算（增量）---
        # 贡献流是事实，缓存行是投影 —— 不重算，事实就"写进去了但看不见"，
        # 而且 v_integrity_violations 会当场判 cache_understated。撤销同样是
        # 靠这条路径把缓存投影回事实，所以它与 ops.undo 走的是同一个函数。
        # 中断时也必须走到这里：已提交的部分要处于**一致**状态才谈得上可 undo。
        touched = ops._recompute_subjects_of_job(conn, job_id)    # noqa: SLF001
        store.recompute(conn, delta=touched)

        # --- 阶段 9/10：冲突 / 格式漂移 / 越界 → 进 Inbox ---
        # 三件事都走 `ops` 的 `refresh_*`，全部只**开待办**、不改事实：
        # 体检页的六类正是这张表的归组（见 query.HEALTH_CATEGORIES），所以
        # "谁发现了问题"与"体检页显示什么"是同一件事的两个说法。
        # 冲突这一条是**对账**、不是"再开一遍"：冲突项是派生状态，视图是它的
        # 真值 —— 新出现的要开，已经不作数的（值集变了、上游被撤了）要关。
        # 只开不关的结果是每导入一次就多一条残骸，而 3.6 的"清空即干净"正是
        # 靠这个函数守住的。`job_id` 是生命周期锚点：这次导入开的项能被这个
        # 作业的 undo 关掉（见 `ops.reconcile_conflicts` 的 docstring）。
        ops.reconcile_conflicts(conn, project_id=opts.project_id, job_id=job_id)
        # 未知字段（§5.6）：`ctx.unknown_fields` 在这次解析里已经数好了，
        # 不在这里落成待办，它就只活在报告文件里（事后无迹可查）。
        ops.refresh_unknown_fields(conn, opts.project_id, job_id=job_id,
                                   fields=ctx.unknown_fields, file_name=path.name,
                                   fingerprint=fingerprint)
        # 越界资产：只判**这个作业刚写进去的实体** —— 作业为自己写的行负责，
        # 而不是替整个库重新判一遍（那会让每次导入付一次全库扫描）。
        ops.refresh_scope_drift(
            conn, opts.project_id, job_id=job_id,
            entity_ids=[sid for kind, sid in touched if kind == "entity"])
        # 「发现未登记资产」（4.1）：**这个作业写下的一段会话里**出现了此前从未
        # 登记过的主机 → 开一条待办。与上面两条同一条纪律（只开待办、不改事实），
        # 也同一条惰性要求 —— 没有 flow 的导入（也就是除 4.1 之外的所有来源）
        # 一行都不写。
        #
        # 它**不收** `entity_ids`（上面那一条收）：判据 3（"除了本作业之外没有
        # 任何贡献指向它"）已经蕴含了"这个作业把它带进来的"，而一次 10 万观测的
        # 导入手上那份 id 清单是同一量级的长度 —— 为它拼一条几万个占位符的 SQL，
        # 换来的是一个已经成立的结论。`query.unregistered_assets` 开头还有一句
        # 早退（这个作业一条 `involves` 都没有就直接返回）。
        ops.refresh_unregistered_assets(conn, opts.project_id, job_id=job_id)
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
        # 原文由谁持有（4.1）。它与 storage_state 正交：`--archive external`
        # 的导入在这一行说 `external`，而 `storage_state` 仍然是 `present`
        # （原文可取得 —— 只是不在本工具手里）。
        "custody": custody,
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
    if scan_result is not None and scan_result.secrets:
        #  **单一出处**（LARI-37 审查）：这一块就是 `ScanResult.as_dict()`，
        #  不再手拼 —— 手拼的那版缺 `occurrences` 键，与 `as_dict` 各说各话，
        #  而"报告数字能不能复算"正是判据 ②/③ 要回答的问题。
        report["secrets"] = scan_result.as_dict()
    return report


def _evidence_for_line(conn: sqlite3.Connection, opts: IngestOptions, raw_id: int,
                       ctx: ParseContext, cache: dict[int, int]) -> int:
    """一行一条 evidence（locator 记行号，§5.3）。懒创建：只给真正产出事实的行建。

    **snippet 先掩码再截断**（4.2 判据 ①）：它是行原文的副本，而一行 JSON 完全
    可能就是 `{"url": "…?api_key=…"}`。截断在掩码之后 —— 反过来的话一个跨在
    512 字符边界上的凭据会被截成半截明文。掩码后的片段仍然回答"这行长什么样"，
    真原文按 locator 回归档文件取（`recovery_state` 的语义不变）。
    """
    lineno = int(ctx.current_line)
    if lineno not in cache:
        snippet = str(normalize.redact_secrets(ctx.current_line_text))[:512]
        cache[lineno] = ops.add_evidence(
            conn, project_id=opts.project_id, raw_file_id=raw_id,
            locator={"line": lineno, "file": Path(ctx.source_name).name},
            snippet=snippet, quality_class="high")
    return cache[lineno]


def _scan_and_write(conn: sqlite3.Connection, opts: IngestOptions, job_id: int,
                    raw_id: int, path: Path, probe: Probe,
                    counters: Counters) -> scan.ScanResult:
    """敏感串扫描 → 写入。**独立函数是为了让 _run_job 里那一行是一句调用**。

    读的是**源文件**（经 `iter_lines`），不是解析器的输出 —— 所以它覆盖
    `stored` 与 `external` 两条归档路径，与选中的解析器正交。

    一个证据、一个来源：扫描器的 `location` 观测已经带了精确行号，把每一处
    命中各挂一条 evidence 会让同一个 raw_file 上多出几十条 locator 不同的证据
    —— 而那条信息已经在观测里了，再挂一遍是冗余。
    """
    result = scan.scan_lines(iter_lines(path, probe), file_name=path.name)
    counters.secrets = result.secrets
    if result.secrets == 0:
        return result
    scan_evidence = ops.add_evidence(
        conn, project_id=opts.project_id, raw_file_id=raw_id,
        #  file 过掩码（4.2 判据 ①，向量 B 的"4.2 自产"两处之一）：evidence
        #  locator 是派生记录，真文件由 raw_file_id 指着，掩掉文件名不丢线索
        #  （与 `scan._location` 的 file 字段同一个口径）。
        locator={"scan": "secrets",
                 "file": str(normalize.redact_secrets(path.name))},
        snippet="", quality_class="medium")
    scan_source = store.register_source(
        conn, name="scanner", authority_level=opts.authority_level,
        tool="scanner", actor=opts.actor)
    for rec in result.records:
        _write_record(conn, opts, job_id, scan_source, scan_evidence, rec,
                      _endpoint_kind(rec))
        counters.records += 1
        counters.added += 1
    return result


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

    可选的 `definition_extra()`（4.3 起）：类源码之外的**额外材料**，混进同一个
    sha256。它存在的唯一理由是 `fingerprint@1` —— 那个解析器的行为由
    `fingerprints/*.yaml` 这些**数据文件**决定，改一条规则不会动类源码一个字节，
    于是"改规则必变 definition_hash"这条判据（规则可版本化的落点）在纯源码
    hash 下走不通。没有该属性的 13 个解析器走原路径，hash 一个字节都不变。
    `extra()` 抛异常时折成 `definition_extra error: <类型>` —— 降级要**稳定**
    （同一种坏在两台机器上算出同一个 hash），绝不让一次规则读取失败把导入路径
    自己炸掉。
    """
    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):
        return "unavailable:" + cls.__qualname__
    extra = getattr(cls, "definition_extra", None)
    if callable(extra):
        try:
            material = extra()
        except Exception as exc:                      # noqa: BLE001 —— 降级要稳定
            material = f"definition_extra error: {type(exc).__name__}: {exc}"
        src = f"{src}\n# definition_extra\n{material}"
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


def parser_drift_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """已登记的解析器指纹 vs **当前实现**：哪些变过。

    判据与 `_definition_drift`（导入当时那一条）共用同一个哈希函数，只是这里
    事后批量算。两处各算一次会分叉，而分叉之后"哪个对"没有答案 —— 所以
    `_definition_hash` 是唯一实现，这个函数只是把它用在别处。

    三种漂移，都是**真的会影响答案**的那种：

      * `implementation` —— 同一 fingerprint 背后的实现变了。这意味着"同一份
        文件、同一个指纹"导出来的事实可能与当时不同，而 `_already_imported`
        的幂等短路**只认指纹**。这是三个里最需要人看一眼的。
      * `unavailable` —— 库里登记过一个指纹，而本程序当前的解析器集合里已经
        没有它了（2.4 落地后 `parsers_bridge` 退役就会产生一批）。重导那些
        文件会走仲裁，落到的解析器**不是**原来那个。
      * `unregistered` —— 反过来：本程序认识、库里也有它的作业，但
        `parser_profiles` 里没有登记（`definition_hash` 为空）。导入路径会先
        登记再开作业（v3.2 §16.4 第 2 条），所以它通常意味着数据是绕开管线
        写进来的，或者是很早的版本留下的。

    空定义哈希（`store.UNHASHED_PARSER`，名称级登记，没有实现可 hash）**不算
    漂移** —— 它本来就声明了"我不知道"，把它报成漂移等于报一个假警。
    """
    parsers, _source_pkg = load_parsers()
    current = {f"{c.tool}@{c.format_version}": c for c in parsers}
    out: list[dict[str, Any]] = []
    for row in conn.execute("SELECT fingerprint, profile_id, version, definition_hash"
                            " FROM parser_profiles ORDER BY fingerprint"):
        fingerprint = str(row["fingerprint"])
        registered = row["definition_hash"]
        cls = current.get(fingerprint)
        if cls is not None and registered not in (None, store.UNHASHED_PARSER):
            actual = _definition_hash(cls)
            if str(registered) != actual:
                out.append({
                    "fingerprint": fingerprint, "drift": "implementation",
                    "registered_definition_hash": str(registered),
                    "current_definition_hash": actual,
                    "detail": (f"{fingerprint} 已登记的 definition_hash "
                               f"{str(registered)[:12]}… 与当前实现 {actual[:12]}… "
                               f"不一致 —— 同一指纹背后的实现变过。"
                               f"那些文件的幂等短路只认指纹，重导**不会**重跑，"
                               f"要重跑得先换一个 format_version。")})
            continue
        if cls is None:
            out.append({
                "fingerprint": fingerprint, "drift": "unavailable",
                "registered_definition_hash": None if registered is None else str(registered),
                "detail": (f"{fingerprint} 在本程序当前的解析器集合里已经不存在了 —— "
                           f"涉及它的文件重导会走仲裁，落到的**不是**原来那个解析器。")})
            continue
        if registered is None and conn.execute(
                "SELECT 1 FROM jobs WHERE parser_fingerprint=? LIMIT 1",
                (fingerprint,)).fetchone() is not None:
            out.append({
                "fingerprint": fingerprint, "drift": "unregistered",
                "registered_definition_hash": None,
                "detail": (f"{fingerprint} 有作业用过，但 parser_profiles 里没有它的 "
                           f"definition_hash —— 这条路径没有走管线的'先登记、再开作业'。")})
    return out


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
            #  文件名过掩码（4.2 判据 ①，向量 B）：coverage_scope 是诊断信息；
            #  inbox 的 `values` 进指纹、`payload.file` 是给人看的 —— 两者都是
            #  **派生**落点，掩掉文件名不损失功能（payload 里 sha256 还在）。
            safe_name = str(normalize.redact_secrets(path.name))
            job_id = ops.start_job(conn, project_id=opts.project_id, kind="ingest",
                                   coverage="unknown",
                                   coverage_scope={"file": safe_name,
                                                   "sha256": digest},
                                   actor=opts.actor)
            item_id, _ = ops._open_inbox(          # noqa: SLF001 — 复用唯一 inbox 写入口
                conn, project_id=opts.project_id, kind="manual_required",
                subject_kind="entity", subject_id=0, values=[safe_name], sources=[],
                job_id=job_id, severity="medium",
                payload={"reason": arbitration["reason"], "file": safe_name,
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
    "arbitrate", "archive_file", "discover", "dry_run_scope", "ingest_file",
    "ingest_paths",
    "INPUT_SUFFIXES", "iter_lines", "load_parsers", "parser_drift_report",
    "probe_file",
    "sha256_file", "sniff_rank",
]
