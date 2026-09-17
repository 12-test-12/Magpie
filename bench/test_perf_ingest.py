r"""§9.2 契约项 · 导入吞吐：**10 万观测 < 60s**（2.5 兑现的那一条）。

计划书 §9.2 里唯一长期是 ⬜ 的契约项就是它。所以它必须能被**单独**质问
"现在到底达不达标"——因此与 `bench/test_perf.py` 同一套纪律：

    py -3.13 -m pytest bench/test_perf_ingest.py -q            # 只报告实测值
    MAGPIE_PERF_STRICT=1 py -3.13 -m pytest bench/test_perf_ingest.py -q   # 当契约

为什么观测数要**从数据层反查**而不是读报告里的计数：报告是管线自己写的，
用它自己的数字来证明它自己达标，等于没证。`db.observations` 是 recompute
之后 `is_current=1` 的真实行数 —— 到达这一步才说明"写进去了并且看得见"。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from magpie.core import ingest, ops, store  # noqa: E402

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

#: 契约：10 万观测 < 60s
BUDGET_SECONDS = 60.0
TARGET_OBSERVATIONS = 100_000

#: 一条 httpx 记录 ≈ 11 条观测（port/scheme/status_code/… + title/cname）。
#: 每个 host 只出现一次，观测值因此互不相同 —— 否则同 (entity, field, value)
#: 会复用同一缓存行，"10 万观测"就名不副实（行数没到）。
N_LINES = 10_000
N_HOSTS = N_LINES


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


@pytest.fixture(scope="module")
def report_and_elapsed(tmp_path_factory) -> tuple[dict, float]:
    """跑一次完整导入，返回 (报告, 墙钟秒数)。模块级缓存：这一跑很贵，只跑一次。"""
    tmp = tmp_path_factory.mktemp("perf-ingest")
    source = tmp / "httpx.jsonl"
    _generate(source, N_LINES, N_HOSTS)

    conn = store.connect(":memory:")
    store.init_db(conn)
    project_id = ops.create_project(conn, "perf-ingest",
                                    {"include": ["*.perf.example.com"], "exclude": []})
    # verify 保持默认（True）—— 契约量的是"用户真跑一次导入"的墙钟，
    # 事后不变量校验是这次导入的一部分，不能从计时里摘出去。
    options = ingest.IngestOptions(project_id=project_id, data_dir=tmp / "data",
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
    return report, elapsed


def test_imports_100k_observations_within_60s(report_and_elapsed):
    report, elapsed = report_and_elapsed
    observations = report["db"]["observations"]
    msg = (f"导入 {report['counts']['records']} 条记录 → {observations} 条有效观测，"
           f"耗时 {elapsed:.1f}s（契约 {BUDGET_SECONDS:.0f}s；"
           f"每 1 万观测 {elapsed / max(observations, 1) * 10_000:.2f}s）")
    print(msg)
    print(f"  其中不变量校验 {report['integrity']['ok']}（layer="
          f"{report['integrity'].get('level')}）；"
          f"事实写入 {report['counts']['added'] + report['counts']['updated']} 条贡献")
    assert observations >= TARGET_OBSERVATIONS, f"样本量不足，测不出契约：{msg}"
    if STRICT:
        assert elapsed < BUDGET_SECONDS, f"§9.2 契约未达标：{msg}"


def test_import_is_linear_enough_to_extrapolate(report_and_elapsed):
    """记录每 1 万观测的成本，供"到 100 万会怎样"外推（不设断言，只报告）。"""
    report, elapsed = report_and_elapsed
    observations = max(int(report["db"]["observations"]), 1)
    per_10k = elapsed / observations * 10_000
    print(f"外推：100 万观测 ≈ {per_10k * 100:.0f}s（契约只要求 10 万 < 60s）")
    assert report["counts"]["records"] > 0
