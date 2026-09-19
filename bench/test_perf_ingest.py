r"""§9.2 契约项 · 导入吞吐：**10 万观测 < 60s**（2.5 兑现的那一条）。

计划书 §9.2 里唯一长期是 ⬜ 的契约项就是它。所以它必须能被**单独**质问
"现在到底达不达标"——因此与 `bench/test_perf.py` 同一套纪律：

    py -3.13 -m pytest bench/test_perf_ingest.py -q            # 只报告实测值
    MAGPIE_PERF_STRICT=1 py -3.13 -m pytest bench/test_perf_ingest.py -q   # 当契约

为什么观测数要**从数据层反查**而不是读报告里的计数：报告是管线自己写的，
用它自己的数字来证明它自己达标，等于没证。`db.observations` 是 recompute
之后 `is_current=1` 的真实行数 —— 到达这一步才说明"写进去了并且看得见"。

一次测量为什么会不准，以及这里怎么对待它（LARI-22 实测之后补的）
--------------------------------------------------------------

同一份代码、同一台空载机器，实测过 **53.25s ~ 85.9s** 的跨度（54%）。拆段计时
把差异定到了"整条导入等比变慢"（各段占比几乎不变），而不是某一段特别胖 ——
这是**机器速度**的签名，不是代码的签名：整条导入是单线程纯 Python + SQLite，
墙钟与当前有效主频近似成正比，而 85.5 / 85.9 两次又快得彼此只差 0.5%。

2026-09-18 在 16 核空载机器上实测过三组（同一份工作量）：

| 条件 | min | 中位 | max |
|---|---|---|---|
| `6406b99`（本 bench 诞生的那一版）· 空载 | 53.25s | **54.90s** | 55.45s |
| 当前工作区 · 空载 | 52.95s | **55.76s** | 57.20s |
| 当前工作区 · **16 路 CPU 争用** | — | **104.09s** | — |

第三行是关键证据：**一行代码没改**，把机器压满就把 55s 推到了 104s，而且各段
占比原样平移。所以**契约在什么条件下才成立**要说清楚：

> §9.2 的 60s 是**空载**下的承诺。同一份代码在机器被压满或降频时会超过它 ——
> 那既不是回归也不是谎言，是"这条承诺有一个没有被写进契约的前提"。
> 判断某次超标属于哪种，看 `[speed]` 那一行的 CPU 速度尺：它也慢了 → 机器状态；
> 它没变 → 那才是代码变慢了。**这一步不能靠猜，要靠这把尺子。**

另外一个次要的风险面：单次导入峰值工作集约 **540 MB**（10 万观测全程内存库）。
几路同时跑时除了抢 CPU，还会抢内存 —— 那是这个 54% 摆幅的第二条来源。

所以取三次**中位数**当结论，而不是一次单点：

* 单点测量对"测量期间机器降速"零抵抗，中位数对一次受污染的样本有抵抗；
* 三次都打印出来（min / 中位 / max）——**不做成只给一个数**。
  一个不给分布的性能报告，读者无法判断它到底是稳定还是侥幸。

`_cpu_probe` 是配套的**速度尺**（只报不改判）：一段固定的纯整数循环。它不参与
任何断言 —— 它的用途是让下一次"85s"能被一眼归类：探针也跟着变慢 → 机器状态，
不是回归；探针没变 → 那才是代码变慢了。没有这把尺子，这两个结论长得一模一样。

每 1 万观测的上限（`PER_10K_BUDGET_SECONDS`）是从 60s 契约**推导**出来的，
不是第二个可调参数（见该常量的说明）。它和 60s 契约在观测数恰好 10 万时同真
同假 —— 加它不是为了"多拦一次"，而是因为：观测数偏移时它仍然有意义、它把
"余量"摆到数字旁边、以及它是外推到 100 万时唯一还站得住的那个数。
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from magpie.core import ingest, ops, store  # noqa: E402

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

#: 契约：10 万观测 < 60s（§9.2，**不是本文件的可调参数**）
BUDGET_SECONDS = 60.0
TARGET_OBSERVATIONS = 100_000

#: 每 1 万观测的上限。**推导值** —— 写死一个 6.0 就会和它漂开。
#: 观测数恰好 10 万时它与 60s 契约同真同假；观测数偏离时（更少或更多），
#: 只有它还说得通："按这个速率，再来一万条要多久"。
PER_10K_BUDGET_SECONDS = BUDGET_SECONDS * 10_000 / TARGET_OBSERVATIONS

#: 取几次中位数。三次是"能抵抗一次受污染样本"的最小值，代价是这条 bench 从
#: 约 55s 变成约 3 分钟 —— 它本来就在 bench（不在 lint/unit）里，这个代价是
#: 明码标价的。要单跑一次看数：`MAGPIE_PERF_SAMPLES=1`。
DEFAULT_SAMPLES = 3

#: 一条 httpx 记录 ≈ 11 条观测（port/scheme/status_code/… + title/cname）。
#: 每个 host 只出现一次，观测值因此互不相同 —— 否则同 (entity, field, value)
#: 会复用同一缓存行，"10 万观测"就名不副实（行数没到）。
N_LINES = 10_000
N_HOSTS = N_LINES

#: CPU 速度尺：固定的纯整数循环轮数。取 300 万轮 ≈ 0.2s —— 够长到不被时钟
#: 粒度淹没，够短到不把这条 bench 再加一秒。**纯整数运算、无分配**：浮点与
#: GC 会引入自己的抖动，那样量到的就不是主频了。
_CPU_PROBE_ROUNDS = 3_000_000

#: 前后两次探针相差超过这个比例 → 本次测量期间机器速度变过，数字不可比。
_CPU_DRIFT_ALERT = 0.20


def _generate(path: Path, n_lines: int, n_hosts: int) -> None:
    """造一份形状与 `httpx -json` 一致的 JSONL（不联网，全本地生成）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(n_lines):
            host = f"h{i % n_hosts}.perf.example.com"
            row = {
                "timestamp": "2026-09-17T10:00:00.000Z",
                "port": str(443 if i % 3 else 80),
                "url": f"https://{host}/",
                "input": host,
                "title": f"host {i % n_hosts}",
                "scheme": "https",
                "webserver": "nginx",
                "content_type": "text/html",
                "host": host,
                "path": "/",
                "status_code": 200 + (i % 5),
                "content_length": 1000 + i,
                "failed": False,
                "tech": ["Nginx:1.24.0", "PHP:8.1.2"],
                "cname": [f"edge{i % 7}.perf.example.net"],
                "tls": {"version": "tls13"},
                "a": [f"10.{i % 250}.{(i // 250) % 250}.7"],
                "final_url": f"https://{host}/",
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _cpu_probe(rounds: int = _CPU_PROBE_ROUNDS) -> float:
    """一段固定工作量的纯整数循环耗时（秒）—— 当前机器速度的**相对**标尺。

    只报不判：它不参与任何断言。理由见模块 docstring —— 它的价值在于把
    "85s 是机器慢还是代码慢"变成一个可以当场回答的问题。
    """
    started = time.perf_counter()
    acc = 0
    for i in range(rounds):
        acc = (acc * 31 + i) & 0xFFFFFFFF
    elapsed = time.perf_counter() - started
    return elapsed if acc >= 0 else elapsed          # acc 只是防止循环被优化掉


def _sample_count() -> int:
    raw = os.environ.get("MAGPIE_PERF_SAMPLES", "")
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SAMPLES


class Sample:
    """一次完整导入的实测值。"""

    def __init__(self, report: dict, elapsed: float, observations: int) -> None:
        self.report = report
        self.elapsed = elapsed
        self.observations = observations
        # 观测数为 0 时不能说"每 1 万观测要 0 秒" —— 那会让断言静默地通过。
        # 用 inf 让它**大声**失败（这条同时兜住"样本量不足"那个断言）。
        self.per_10k = (elapsed / observations * 10_000) if observations else math.inf


def _measure(source: Path, scratch: Path, index: int) -> Sample:
    """跑一次完整导入。每次都用**全新**的内存库（样本之间不互相污染）。"""
    conn = store.connect(":memory:")
    store.init_db(conn)
    project_id = ops.create_project(
        conn, f"perf-ingest-{index}", {"include": ["*.perf.example.com"], "exclude": []})
    # verify 保持默认（True）—— 契约量的是"用户真跑一次导入"的墙钟，
    # 事后不变量校验是这次导入的一部分，不能从计时里摘出去。
    options = ingest.IngestOptions(project_id=project_id, data_dir=scratch / "data",
                                   archive="external")
    started = time.perf_counter()
    report = ingest.ingest_file(conn, options, source)
    elapsed = time.perf_counter() - started

    observations = int(conn.execute(
        "SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1",
        (project_id,)).fetchone()[0])
    report["db"] = {"observations": observations,
                    "relations": int(conn.execute(
                        "SELECT COUNT(*) FROM relations WHERE project_id=? AND is_current=1",
                        (project_id,)).fetchone()[0])}
    conn.close()
    return Sample(report, elapsed, observations)


def _margin(label: str, value: float, budget: float, unit: str = "s") -> str:
    """`55.70s / 60.00s，余量 4.30s（7.2%）` —— 余量永远摆在数字旁边。

    超预算时**不写负的余量**：`余量 -25.9s` 会被读成"还有余量"。超出就写超出，
    符号靠词而不是靠减号表达（数字旁边没有余量，读的人就得自己算，而人不会算；
    算出一个负数还读反，比不算更糟）。
    """
    margin = budget - value
    percent = margin / budget * 100 if budget else 0.0
    if margin >= 0:
        return f"{label} {value:.2f}{unit} / {budget:.2f}{unit}，余量 {margin:.2f}{unit}（{percent:.1f}%）"
    return (f"{label} {value:.2f}{unit} / {budget:.2f}{unit}，"
            f"**超出 {-margin:.2f}{unit}（{-percent:.1f}%）**")


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> list[Sample]:
    """跑 N 次完整导入，返回样本列表。模块级缓存：这一跑很贵，只跑一次。

    文件**只生成一次**再重复导入 —— 每次重新生成会把"造数据"的时间混进来，
    而契约量的是导入，不是造数据。
    """
    tmp = tmp_path_factory.mktemp("perf-ingest")
    source = tmp / "httpx.jsonl"
    _generate(source, N_LINES, N_HOSTS)

    probe_before = _cpu_probe()
    out = [_measure(source, tmp, index) for index in range(_sample_count())]
    probe_after = _cpu_probe()
    drift = abs(probe_after - probe_before) / probe_before if probe_before else 0.0

    print(f"\n[speed] CPU 速度尺（{_CPU_PROBE_ROUNDS:,} 轮纯整数循环）: "
          f"跑前 {probe_before:.3f}s / 跑后 {probe_after:.3f}s；"
          f"本次导入中位数 {statistics.median(s.elapsed for s in out):.2f}s "
          f"= {statistics.median(s.elapsed for s in out) / probe_before:.0f} 个基准单位"
          f"（这个比值才是与机器无关的那个数）")
    if drift > _CPU_DRIFT_ALERT:
        # 不失败，只把话说清楚：速度变过，这组数字之间不可比。
        print(f"[speed] ⚠️ 本次测量期间机器速度变了 {drift * 100:.0f}%"
              f"（>{_CPU_DRIFT_ALERT * 100:.0f}%）—— 这组数字不宜当作结论")
    return out


def _report_distribution(samples: list[Sample]) -> None:
    """把分布摆出来。**只给一个数**的性能报告没法判断它是稳定还是侥幸。"""
    elapsed = [s.elapsed for s in samples]
    per_10k = [s.per_10k for s in samples]
    median_total = statistics.median(elapsed)
    median_rate = statistics.median(per_10k)
    spread = (max(elapsed) - min(elapsed)) / min(elapsed) * 100 if min(elapsed) else 0.0
    print(f"\n导入 {samples[0].report['counts']['records']} 条记录 → "
          f"{samples[0].observations} 条有效观测（{len(samples)} 次样本）")
    print(f"  {_margin('耗时（中位）', median_total, BUDGET_SECONDS)}")
    print(f"  {_margin('每 1 万观测（中位）', median_rate, PER_10K_BUDGET_SECONDS)}")
    print(f"  样本 min {min(elapsed):.2f}s / 中位 {median_total:.2f}s / "
          f"max {max(elapsed):.2f}s（摆幅 {spread:.1f}%，n={len(samples)}）")
    print(f"  外推：100 万观测 ≈ {median_rate * 100:.0f}s（契约只要求 10 万 < 60s）")
    report = samples[0].report
    print(f"  其中不变量校验 {report['integrity']['ok']}（layer="
          f"{report['integrity'].get('level')}）；"
          f"事实写入 {report['counts']['added'] + report['counts']['updated']} 条贡献")


def _echo_margin_line(samples: list[Sample]) -> str:
    """给 pytest 的 warnings summary 用的一行 —— 见 `_echo` 的说明。"""
    elapsed = statistics.median(s.elapsed for s in samples)
    return _margin("§9.2 导入 10 万观测", elapsed, BUDGET_SECONDS)


def _echo(samples: list[Sample]) -> None:
    """把结论**回显到不带 `-s` 也能看见的地方**。

    `check.py bench` 跑的是 `pytest bench/… -q`（没有 `-s`），此时通过的用例
    的 stdout 会被 pytest 吞掉 —— 也就是说：**G2 的验收入口上看不见任何数字**。
    一个"记得加 -s 才存在"的余量回显，和没有回显的区别只是让人更安心，所以这里
    另发一条 warning：它不是失败，但会出现在 summary 里，`check.py bench` 的
    输出里因此永远有余量那一行。
    """
    warnings.warn(f"[perf] {_echo_margin_line(samples)}", stacklevel=1)


def test_imports_100k_observations_within_60s(samples):
    _report_distribution(samples)
    _echo(samples)

    observations = samples[0].observations
    median_total = statistics.median(s.elapsed for s in samples)
    median_rate = statistics.median(s.per_10k for s in samples)
    msg = (f"导入 {samples[0].report['counts']['records']} 条记录 → {observations} 条有效观测，"
           f"耗时中位 {median_total:.1f}s（契约 {BUDGET_SECONDS:.0f}s；"
           f"每 1 万观测 {median_rate:.2f}s，上限 {PER_10K_BUDGET_SECONDS:.2f}s）")

    assert observations >= TARGET_OBSERVATIONS, f"样本量不足，测不出契约：{msg}"
    if STRICT:
        # 两条断言：整条契约（绝对值）与它的速率形式。观测数正好 10 万时它们
        # 同真同假 —— 速率那条不是"第二道闸"，是同一个承诺的另一种写法，它让
        # "余量"可以被打印、可以在观测数偏移时继续说人话。两条都**可能红**。
        assert median_rate <= PER_10K_BUDGET_SECONDS, f"§9.2 契约未达标（速率）：{msg}"
        assert median_total < BUDGET_SECONDS, f"§9.2 契约未达标：{msg}"


def test_import_is_linear_enough_to_extrapolate(samples):
    """记录每 1 万观测的成本，供"到 100 万会怎样"外推（不设断言，只报告）。"""
    median_rate = statistics.median(s.per_10k for s in samples)
    print(f"外推：100 万观测 ≈ {median_rate * 100:.0f}s（契约只要求 10 万 < 60s）")
    assert samples[0].report["counts"]["records"] > 0
