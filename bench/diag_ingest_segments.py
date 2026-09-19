r"""导入契约的**拆段计时** —— 回答「85s 里哪一段变胖了」（LARI-22 诊断用）。

**这不是契约，是一把尺子。** `bench/test_perf_ingest.py` 给的是一个数字（好/坏），
本脚本给的是这个数字的**构成**：解析 / 归一化 / 写库 / 重算 / 不变量校验各占多少。
没有它，"55.7 → 85.9s"只能靠猜是哪一段；有了它，才能说清"是回归还是方差"。

用法::

    py -3.13 bench/diag_ingest_segments.py                 # 跑 1 次，出一张拆段表
    py -3.13 bench/diag_ingest_segments.py --repeat 5      # 跑 5 次，出分布 + 每段中位数
    py -3.13 bench/diag_ingest_segments.py --profile       # 另跑一次 cProfile，出热点函数

设计纪律（与 `test_perf_ingest.py` 同一套，否则两张表不可比）:

* **工作量逐字节相同** —— 直接 import `test_perf_ingest` 的 `_generate` / `N_LINES` /
  `N_HOSTS`，而不是复制一份。复制一份的下场是两边迟早分叉，而分叉之后
  "拆段表说 A 段最贵"与"契约说总共 85s"就不再是同一件事了。
* **不改被测代码** —— 全部靠运行时猴补丁插桩（`_patch`）。`magpie/**` 一行不动：
  诊断工具一旦要改被测对象，它测的就不再是"线上跑的那份代码"。
* **归因要能对账** —— 各段自身耗时之和与总墙钟的差单独列成「未归因」，不摊进
  任何一段。"归因不了的部分"是个数字；把它藏起来，表就会自己说自己解释了一切。
* **每跑一行是**这一跑**的账，不是累计** —— 累计表会把第 5 次画成第 1 次的 5 倍，
  而"第 5 次变慢了"和"累计值本来就大"是两件完全不同的事。`Clock.diff` 专门
  防这个：本脚本第一版就踩过，第 2 次起的表读起来像雪崩。

计时口径: 每段的「自身耗时」＝该段累计 − 已知子段之和（子段单独成行），
因此同一毫秒不会被算进两行。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 与契约 bench 共用同一份工作量定义（见模块 docstring 的「工作量逐字节相同」）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_perf_ingest as contract      # noqa: E402
from magpie.core import ingest, ops, store   # noqa: E402


# ---------------------------------------------------------------------------
# 计时器与猴补丁
# ---------------------------------------------------------------------------


class Clock:
    """累计每段的墙钟与调用次数。"""

    def __init__(self) -> None:
        self.acc: dict[str, float] = {}
        self.calls: dict[str, int] = {}

    def add(self, label: str, dt: float) -> None:
        self.acc[label] = self.acc.get(label, 0.0) + dt
        self.calls[label] = self.calls.get(label, 0) + 1

    def seconds(self, label: str) -> float:
        return self.acc.get(label, 0.0)

    def diff(self, before: "Clock") -> "Clock":
        """相对某个快照的增量 —— 一行 = 一次的账（见模块 docstring）。"""
        out = Clock()
        for label, value in self.acc.items():
            out.acc[label] = value - before.acc.get(label, 0.0)
            out.calls[label] = self.calls.get(label, 0) - before.calls.get(label, 0)
        return out


def _patch(owner, name: str, label: str, clock: Clock) -> None:
    """把 `owner.name` 换成计时包装。原位替换，不保留句柄。"""
    original = getattr(owner, name)

    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            clock.add(label, time.perf_counter() - started)

    wrapper.__name__ = name
    wrapper.__doc__ = original.__doc__
    wrapper.__wrapped__ = original
    setattr(owner, name, wrapper)


def _patch_parser(parser_cls, clock: Clock) -> None:
    """给解析器的 `parse` 生成器套一层计时。

    只量**生成器内部**的耗时：`next()` 返回到下一次 `next()` 之间的是调用方
    （写库）的时间，不算解析。裸计时整条 for 循环会把写库的账记到解析头上 ——
    那正是这张表最不该犯的错。
    """
    original = parser_cls.parse

    def parse(self, lines, ctx):
        iterator = iter(original(self, lines, ctx))
        while True:
            started = time.perf_counter()
            try:
                item = next(iterator)
            except StopIteration:
                return
            clock.add(PARSE_LABEL, time.perf_counter() - started)
            yield item

    parser_cls.parse = parse


PARSE_LABEL = "解析 parse（生成器内部）"

#: 段与插桩点。**每个函数只能出现一次**：包装是加法累计的，套两层会把同一
#: 毫秒记两遍（本脚本第一版就让 `_write_record` 既在表里又被单独补一次，
#: 于是它的数字整整翻倍 —— 拆段表一旦会自己说谎，它就比没有更糟）。
_PROBES = (
    (PARSE_LABEL, None, None),                     # 由 _patch_parser 装
    ("嗅探仲裁 sniff_rank", ingest, "sniff_rank"),
    ("写库 _write_record", ingest, "_write_record"),
    ("  └ ops.add_observation", ops, "add_observation"),
    ("  └ ops.add_relation", ops, "add_relation"),
    ("  └ ops.add_entity_field", ops, "add_entity_field"),
    ("evidence 查找", ingest, "_evidence_for_line"),
    ("  └ ops.add_evidence", ops, "add_evidence"),
    ("事实键 _fact_key", ingest, "_fact_key"),
    ("已存在事实反查", ingest, "_existing_facts"),
    ("recompute 重算缓存", store, "recompute"),
    ("  └ 取 delta 主体", ops, "_recompute_subjects_of_job"),
    ("冲突检测 refresh_observation_conflicts", ops, "refresh_observation_conflicts"),
    ("不变量校验 _verify", ingest, "_verify"),
    ("作业开场 start_job", ops, "start_job"),
    ("作业收尾 finish_job", ops, "finish_job"),
    ("坏行落盘 _write_badlines", ingest, "_write_badlines"),
    ("sha256 + 归档 + 探测", ingest, "sha256_file"),
    ("sha256 + 归档 + 探测", ingest, "archive_file"),
    ("sha256 + 归档 + 探测", ingest, "probe_file"),
)

#: 子段：父段的自身耗时 = 累计 − Σ子段。表里按上面的顺序逐行显示。
_CHILDREN = {
    "写库 _write_record": ("  └ ops.add_observation", "  └ ops.add_relation",
                           "  └ ops.add_entity_field"),
    "evidence 查找": ("  └ ops.add_evidence",),
    "recompute 重算缓存": ("  └ 取 delta 主体",),
}

#: 显示顺序（只在插过桩、真跑过的段才出现）。去重但保序 —— 有三条探针共用
#: 「sha256 + 归档 + 探测」这一个标签，不去重就会印三行同样的数字。
_DISPLAY = tuple(dict.fromkeys(label for label, _, _ in _PROBES))


def install(clock: Clock) -> None:
    seen: set[tuple[int, str]] = set()
    for label, owner, name in _PROBES:
        if owner is None or name is None:
            continue
        key = (id(owner), name)
        if key in seen:                            # 同一点被两条探针命中 → 只装一次
            continue
        seen.add(key)
        _patch(owner, name, label, clock)


# ---------------------------------------------------------------------------
# 峰值内存（Windows 私有接口；取不到就是 None —— 观测值不该让整张表挂掉）
# ---------------------------------------------------------------------------


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _memory(field: str) -> float | None:
    """读本进程的 `field`（字节）→ MB。任何一步失败都返回 None。"""
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetCurrentProcess()
        for dll_name, func_name in (("psapi", "GetProcessMemoryInfo"),
                                    ("kernel32", "K32GetProcessMemoryInfo")):
            try:
                func = getattr(ctypes.WinDLL(dll_name), func_name)
            except (OSError, AttributeError):
                continue
            func.argtypes = [ctypes.wintypes.HANDLE,
                             ctypes.POINTER(_ProcessMemoryCounters),
                             ctypes.wintypes.DWORD]
            func.restype = ctypes.wintypes.BOOL
            if func(handle, ctypes.byref(counters), counters.cb):
                return getattr(counters, field) / (1 << 20)
        return None
    except Exception:                                    # noqa: BLE001 — 观测是旁路
        return None


# ---------------------------------------------------------------------------
# 一次完整导入
# ---------------------------------------------------------------------------


def run_once(source: Path, scratch: Path, *, verify: bool = True) -> dict:
    """跑一次导入。**源文件在计时之外**生成（与契约 bench 同一口径）。"""
    conn = store.connect(":memory:")
    store.init_db(conn)
    project_id = ops.create_project(
        conn, "perf-ingest", {"include": ["*.perf.example.com"], "exclude": []})
    options = ingest.IngestOptions(project_id=project_id, data_dir=scratch / "data",
                                   archive="external", verify=verify)
    started = time.perf_counter()
    report = ingest.ingest_file(conn, options, source)
    elapsed = time.perf_counter() - started
    observations = int(conn.execute(
        "SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1",
        (project_id,)).fetchone()[0])
    conn.close()
    return {"elapsed": elapsed, "observations": observations, "report": report}


def table(run: Clock, elapsed: float) -> tuple[list[tuple[str, float, int]], float]:
    """(行, 未归因秒)。行的自身耗时互不重叠，因此可以直接相加对账。"""
    rows: list[tuple[str, float, int]] = []
    accounted = 0.0
    for label in _DISPLAY:
        if run.calls.get(label, 0) == 0 and run.seconds(label) == 0.0:
            continue
        own = run.seconds(label) - sum(run.seconds(c) for c in _CHILDREN.get(label, ()))
        rows.append((label, own, run.calls.get(label, 0)))
        accounted += own
    return rows, elapsed - accounted


def print_table(run: Clock, elapsed: float) -> None:
    rows, unaccounted = table(run, elapsed)
    width = max((len(row[0]) for row in rows), default=10)
    print(f"  总墙钟 {elapsed:8.2f}s   （当前工作集 {_memory('WorkingSetSize') or 0:.0f} MB / "
          f"峰值 {_memory('PeakWorkingSetSize') or 0:.0f} MB）")
    print(f"  {'段':<{width}}  {'自身':>8}  {'占比':>7}  {'调用':>8}")
    for label, own, calls in rows:
        share = own / elapsed * 100 if elapsed else 0.0
        print(f"  {label:<{width}}  {own:8.2f}s  {share:6.1f}%  {calls:8d}")
    print(f"  {'未归因（循环 / 去重 / 其它）':<{width}}  {unaccounted:8.2f}s  "
          f"{(unaccounted / elapsed * 100 if elapsed else 0):6.1f}%")


# ---------------------------------------------------------------------------
# cProfile：函数级热点（拆段表说"哪一段"，这个说"哪一行"）
# ---------------------------------------------------------------------------


def profile_once(source: Path, scratch: Path) -> None:
    import cProfile
    import pstats

    profiler = cProfile.Profile()
    profiler.enable()
    run_once(source, scratch)
    profiler.disable()
    print("\n=== cProfile · 自身耗时前 25 ===", flush=True)
    print("（cProfile 自身有开销：看的是**相对**排序，不是绝对秒数）", flush=True)
    pstats.Stats(profiler).sort_stats("tottime").print_stats(25)
    print("\n=== cProfile · 累计耗时前 20 ===", flush=True)
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(20)


# ---------------------------------------------------------------------------


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="导入契约拆段计时（LARI-22 诊断）")
    parser.add_argument("--repeat", type=int, default=1, help="重复次数（默认 1）")
    parser.add_argument("--profile", action="store_true", help="额外跑一次 cProfile")
    parser.add_argument("--no-verify", action="store_true",
                        help="关掉收尾不变量校验（单独量它的代价）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录，便于事后翻查")
    args = parser.parse_args(argv[1:])

    tmp = Path(tempfile.mkdtemp(prefix="magpie-diag-"))
    source = tmp / "httpx.jsonl"
    contract._generate(source, contract.N_LINES, contract.N_HOSTS)
    print(f"源文件 {source}")
    print(f"工作量 N_LINES={contract.N_LINES} N_HOSTS={contract.N_HOSTS}"
          f"（与 bench/test_perf_ingest.py 共用同一份定义）")
    print(f"解释器 {sys.executable} {sys.version.split()[0]}", flush=True)

    clock = Clock()
    install(clock)
    parsers, source_pkg = ingest.load_parsers()
    for cls in parsers:
        _patch_parser(cls, clock)
    print(f"解析器来源 {source_pkg}；已插桩 "
          f"{', '.join(f'{c.tool}@{c.format_version}' for c in parsers) or '（无）'}")
    print(f"git HEAD {_git_head()}", flush=True)

    elapsed_samples: list[float] = []
    per_label: dict[str, list[float]] = {}
    for index in range(args.repeat):
        before = Clock()
        before.acc = dict(clock.acc)
        before.calls = dict(clock.calls)
        result = run_once(source, tmp, verify=not args.no_verify)
        run = clock.diff(before)
        elapsed_samples.append(result["elapsed"])
        for label, value in run.acc.items():
            if value:
                per_label.setdefault(label, []).append(value)
        print(f"\n--- 第 {index + 1}/{args.repeat} 次（本跑增量）---")
        print_table(run, result["elapsed"])
        print(f"  观测数 {result['observations']}；"
              f"每 1 万观测 {result['elapsed'] / max(result['observations'], 1) * 10_000:.2f}s",
              flush=True)

    print("\n=== 总墙钟分布 ===")
    _print_stats("总墙钟", elapsed_samples)
    if args.repeat > 1:
        print("\n=== 各段分布（秒）===")
        for label, values in per_label.items():
            _print_stats(label, values)
        print("\n=== 各段中位数占比（自身耗时，互不重叠）===")
        median_total = statistics.median(elapsed_samples)
        for label, values in sorted(per_label.items(),
                                    key=lambda kv: -statistics.median(kv[1])):
            share = statistics.median(values) / median_total * 100 if median_total else 0
            print(f"  {label:<40} {statistics.median(values):8.2f}s  {share:6.1f}%")

    if args.profile:
        profile_once(source, tmp)

    if args.keep:
        print(f"\n临时目录保留在 {tmp}")
    return 0


def _git_head() -> str:
    """当前 checkout 的 commit（**同一份表必须能对上它验的代码**）。

    用 `git rev-parse` 而不是读 `.git/HEAD`：`git worktree` 建出来的目录里
    `.git` 是一个**文件**（指向主仓的 gitdir），读文件那条路在那里会静默失败，
    于是旧 rev 的表头上会写着"读不到"—— 一张说不清自己测了哪版代码的表，
    拿来做回归对照是没有意义的。
    """
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "（读不到）"
    return out.stdout.strip()[:12] if out.returncode == 0 else "（读不到）"


def _print_stats(label: str, values: list[float]) -> None:
    if not values:
        return
    print(f"  {label:<40} min {min(values):8.2f}  中位 {statistics.median(values):8.2f}  "
          f"max {max(values):8.2f}  n={len(values)}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
