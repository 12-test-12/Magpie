r"""解析吞吐契约（开发实施计划 2.4 的验收项之一）：

    **单文件解析吞吐写进 `bench/`（10 万行 < 10s 量级），不拖慢 `check.py`。**

它单独一个文件、只在 `check.py bench` / `check.py all` 里跑：解析是纯 CPU 的
逐行循环，10 万行要跑满一秒级；混进单元测试段会把每次 `pytest` 都拖慢，
而"每次改一行都要跑一遍"的测试才有人跑。

为什么这个契约值得存在
----------------------
解析器是整个管线里唯一**按行**计费的环节：其余每一步（写库、重算）都能靠
批处理摊薄，只有它必须一行一行地读完整个文件。10 万行是这个项目真实见到过的
量级（一次 subfinder + httpx 巡检），所以"读得动"是阶段 3"资源扩张"的前置条件：
来源从 2 个扩到十几个时，这个数字只会被乘以更多倍。

量级而不是精确值：不同机器的绝对值能差一倍，钉死一个秒数会变成"换台机器就红"。
契约是 10 万行 < 10s（§9.2 的"10 万观测 < 60s"里，解析这一段留 1/6 的预算），
外加"每行成本随规模不增长"——后者才是真正防退化的那条。

实测基线（2026-09-17，本机 py -3.13）
------------------------------------
    domainlist   10 万行 / 30.0 万条记录 / 0.80s （ 8.0 us/行）
    httpx        10 万行 / 170.5 万条记录 / 4.78s （47.8 us/行）

httpx 贵在"一行拆出 17 条记录"，拆解开来是：`json.loads` 0.26s、
构造 170 万条 `Raw*`（含硬线校验）1.26s、其余是字段分派与归一化调用。
构造那一份不是浪费 —— 它是"绝不产出数据库自增 id"这条硬线**在热路径上**的
代价，而把校验改成只在测试里跑，等于让这条硬线在生产里只剩一句注释。
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

from magpie.core.parsers import domainlist, httpx  # noqa: E402
from magpie.core.parsers.base import OfflineContext  # noqa: E402

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

#: 10 万行的解析预算（秒）。§9.2 的"10 万观测 < 60s"里解析占 1/6。
BUDGET_SECONDS = 10.0
LINES = 100_000


def _domain_lines(count: int):
    """域名的重复度刻意做高：真实清单里撞父域是常态，去重路径要被压到。"""
    for i in range(count):
        yield f"host{i % 5000:04d}.sub{i % 37:02d}.example.com"


def _httpx_lines(count: int):
    for i in range(count):
        yield json.dumps({
            "timestamp": "2026-09-17T10:00:01.100Z", "port": "443",
            "url": f"https://host{i % 5000}.example.com",
            "input": f"host{i % 5000}.example.com", "title": f"Host {i}",
            "scheme": "https", "webserver": "nginx/1.24.0",
            "content_type": "text/html", "host": f"host{i % 5000}.example.com",
            "status_code": 200, "content_length": 5213, "failed": False,
            "tech": ["Vue.js", "Nginx:1.24.0"], "tls": {"version": "tls13"},
            "a": [f"203.0.113.{i % 250}"], "final_url": f"https://host{i % 5000}.example.com/",
        }, ensure_ascii=False)


def _timed(cls, lines) -> tuple[float, int]:
    ctx = OfflineContext()
    t0 = time.perf_counter()
    count = sum(1 for _ in cls().parse(lines, ctx))
    return time.perf_counter() - t0, count


@pytest.mark.parametrize("name,cls,make", [
    ("domainlist", domainlist.DomainListParser, _domain_lines),
    ("httpx", httpx.HttpxParser, _httpx_lines),
])
def test_parse_throughput_100k_lines(name, cls, make):
    elapsed, records = _timed(cls, make(LINES))
    per_line = elapsed / LINES * 1e6
    message = (f"{name}: {LINES} 行 / {records} 条记录 用时 {elapsed:.2f}s "
               f"（{per_line:.1f} us/行）；预算 {BUDGET_SECONDS:.0f}s")
    print(message)
    if STRICT:
        assert elapsed < BUDGET_SECONDS, message


@pytest.mark.parametrize("name,cls,make", [
    ("domainlist", domainlist.DomainListParser, _domain_lines),
    ("httpx", httpx.HttpxParser, _httpx_lines),
])
def test_parse_cost_per_line_does_not_grow(name, cls, make):
    """规模翻 4 倍时每行成本不该涨 —— 涨了说明某个环节是 O(N²) 的。

    这条比绝对秒数更能防退化：换台机器绝对值会变，但"每行成本是否随规模上升"
    不会。阈值取 2.5x 是给噪声留的余量（缓存、GC、预热），不是精度。
    """
    small, _ = _timed(cls, make(LINES // 4))
    large, _ = _timed(cls, make(LINES))
    ratio = (large / LINES) / (small / (LINES // 4))
    message = (f"{name}: 每行成本 小规模 {small / (LINES // 4) * 1e6:.1f} us "
               f"→ 大规模 {large / LINES * 1e6:.1f} us，比值 {ratio:.2f}x（线性=1.00x）")
    print(message)
    if STRICT:
        assert ratio < 2.5, message


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
