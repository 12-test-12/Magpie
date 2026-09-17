r"""§9.2 性能护栏：把"文档承诺的性能目标"变成可执行的回归测试。

背景（实测，2026-09 基线）：
  `store.recompute` 走 `_recompute_subjects` 的**逐 subject 循环**，每个
  observation 调 `confidence.score_field`，而后者每次都对
  `v_field_owner` / `v_field_conflicts` 两个**全表聚合视图**做查询。
  结果是每行约 10 条 SQL、整体近似 O(N^2)：

      N=1000  full=  0.87s
      N=2000  full=  2.71s   (3.13x)
      N=4000  full=  9.05s   (3.34x)
      N=8000  full= 39.76s   (4.40x)   <- 平方增长

  §9.2 承诺 "100 万 observations 全量 recompute <= 60s" —— 按此曲线外推
  约 4700 秒（78 分钟），与承诺差约 78 倍。

本文件不"修复"性能，只把当前曲线钉成契约：
  - `test_recompute_growth_is_not_quadratic`：增长比必须接近线性。
    当前实现**应当失败**（这正是要暴露的事实）。
  - 用 `MAGPIE_PERF_STRICT=1` 控制是否把超线性增长当失败，
    避免在修复前阻塞正常的回归测试。

运行：
  python -m pytest bench/test_perf.py -q              # 只报告
  MAGPIE_PERF_STRICT=1 python -m pytest bench/test_perf.py -q   # 当契约
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import ops, store  # noqa: E402

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

# 单行成本上限（秒）。当前实测约 1.1e-3 s/row @N=8000；理想应 <= 1e-4。
MAX_SECONDS_PER_ROW = 1e-3


def _build(n_obs: int, n_entities: int = 20):
    conn = store.connect(":memory:")
    store.init_db(conn)
    pid = ops.create_project(conn, "perf", {"include": ["*.example.com"], "exclude": []})
    store.register_source(conn, source_id=1, name="nmap", authority_level="verified")
    store.register_parser(conn, "nmap@1", profile_id="nmap", version="1")
    ops.set_parser(conn, pid, "nmap@1")
    rf = ops.add_raw_file(conn, project_id=pid, locator_uri="file:///raw/p.txt",
                          content="x\n", storage_state="present")
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=rf, locator={"line": 1},
                          snippet="x", quality_class="high")
    job = ops.start_job(conn, project_id=pid, kind="ingest", coverage="full",
                        coverage_scope={"include": ["*.example.com"], "exclude": []},
                        parser_fingerprint="nmap@1")
    for i in range(n_obs):
        ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev,
                            source_id=1, entity_key=f"h{i % n_entities}.example.com",
                            kind="host", semantic_field="port",
                            value=str(1000 + (i % 60000)))
    ops.finish_job(conn, job, state="done")
    return conn, pid, job


def _time_full(conn) -> float:
    t0 = time.perf_counter()
    store.recompute(conn, None)
    return time.perf_counter() - t0


def test_full_recompute_per_row_cost_has_an_upper_bound():
    """把"每行成本"钉住：超过 MAX_SECONDS_PER_ROW 说明退化。

    这是防"悄悄变慢"的护栏，阈值取当前实测的同一量级，
    修复后应显著低于它（目标 1e-4 量级）。
    """
    conn, _, _ = _build(2000)
    try:
        elapsed = _time_full(conn)
    finally:
        conn.close()
    per_row = elapsed / 2000
    msg = (f"full recompute: {elapsed:.3f}s for 2000 rows = {per_row*1e6:.0f} us/row; "
           f"§9.2 承诺 1M 行 <= 60s，即 <= 60 us/row")
    if STRICT:
        assert per_row <= MAX_SECONDS_PER_ROW, msg
    else:
        print(msg)


def test_recompute_growth_is_not_quadratic():
    """N 翻倍时耗时应约 2x；实测约 3.3~4.4x -> 平方增长。

    修复前该断言只在 STRICT 模式下失败（避免阻塞回归），但始终打印实测比值。
    """
    sizes = (1000, 2000, 4000)
    times = []
    for n in sizes:
        conn, _, _ = _build(n)
        try:
            times.append(_time_full(conn))
        finally:
            conn.close()

    ratios = [times[i + 1] / times[i] for i in range(len(times) - 1)]
    report = "  ".join(f"N={n}:{t:.3f}s" for n, t in zip(sizes, times))
    report += "  比值=" + ", ".join(f"{r:.2f}x" for r in ratios) + " (线性=2.00x)"
    print(report)
    if STRICT:
        assert all(r < 2.6 for r in ratios), f"recompute 增长超线性: {report}"


def test_delta_recompute_cost_scales_with_subject_count():
    """增量路径：文档承诺"单 job undo(<=5000 影响对象) <= 1.5s"。

    实测 N=4000 时 K=500 已需约 1.2s；且成本随 N 一起涨
    （因为逐 row 的 confidence 在全表视图上求值）。
    """
    conn, _, _ = _build(4000)
    try:
        t1 = time.perf_counter()
        store.recompute(conn, [("observation", i) for i in range(1, 101)])
        k100 = time.perf_counter() - t1
        t2 = time.perf_counter()
        store.recompute(conn, [("observation", i) for i in range(1, 501)])
        k500 = time.perf_counter() - t2
    finally:
        conn.close()
    msg = (f"delta(K=100)={k100:.3f}s  delta(K=500)={k500:.3f}s "
           f"@N=4000；§9.2 承诺 K<=5000 <= 1.5s")
    if STRICT:
        assert k500 <= 1.5, msg
    else:
        print(msg)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
