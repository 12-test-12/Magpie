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

实测基线（2026-09-19，本机 py -3.13）
------------------------------------
    domainlist   10 万行 /  30.0 万条记录 / 0.80s （ 8.0 us/行）
    httpx        10 万行 / 170.5 万条记录 / 4.17s （41.7 us/行）
    nmap         7.5 万行 /  41.3 万条记录 / 1.14s （15.2 us/行）
    nuclei       10 万行 / 130.0 万条记录 / 4.37s （43.7 us/行）

httpx 贵在"一行拆出 17 条记录"，拆解开来是：`json.loads` 0.26s、
构造 170 万条 `Raw*`（含硬线校验）1.26s、其余是字段分派与归一化调用。
构造那一份不是浪费 —— 它是"绝不产出数据库自增 id"这条硬线**在热路径上**的
代价，而把校验改成只在测试里跑，等于让这条硬线在生产里只剩一句注释。

nmap 比 httpx 便宜，而且不是因为它做的事少：XML 由 `XMLPullParser` 的 C 实现
切成事件（不是 Python 逐字段解析 JSON），真正的成本落在 `Raw*` 的构造上
（41.3 万条 / 1.14s ≈ 2.8 us 一条）。**它的行数口径与其他两个不同** ——
`<host>` 一行能带出十几条记录，所以这里给的是"约 7.5 万行 XML"而不是
"10 万行"；契约判的是时间，而时间与行数、记录数都成线性（上面那条
"每行成本不随规模增长"就是它的证据）。

nuclei 与 httpx 同档，而且贵在同一个地方（一行 13 条记录、130 万条 `Raw*`）。
它第一版是 6.41s，降下来的那 2s 来自一处**被算了两遍**的归一化：解析器既要
身份 key 又要资产引用，分两次调 `normalize` 会把同一个 `matched-at` 规范化
两遍（10 万行 = 20 万次域名归一化）。`normalize.vuln_identity` 就是为这次合并
存在 —— 解析是整条管线里唯一按行计费的环节，重复一遍就是 30%。
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

from magpie.core.parsers import domainlist, httpx, nmap, nuclei  # noqa: E402
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


def _nmap_lines(count: int):
    """一份 `nmap -sV -oX` 的 XML，总行数 ≈ `count`。

    形状按真实输出搭：每台主机约 8 行，其中 2 个端口各带 1 条 `<service>` 与
    1 条 `<script>`。8 台主机的重复度**刻意做高**（`i % 8`）—— 真实扫描里同一段
    网段的机器长得几乎一样，去重路径要被压到。
    """
    hosts = max(1, count // 8)
    yield '<?xml version="1.0" encoding="UTF-8"?>'
    yield '<!DOCTYPE nmaprun>'
    yield (f'<nmaprun scanner="nmap" args="nmap -sV -oX out.xml 10.0.0.0/24"'
           f' start="0" version="7.94" xmloutputversion="1.05">')
    yield '<target specification="10.0.0.0/24" status="up" reason="arp-response"/>'
    for i in range(hosts):
        ip = f"10.0.0.{i % 250 + 1}"
        yield f'<host starttime="0" endtime="1"><status state="up" reason="syn-ack"/>'
        yield f'<address addr="{ip}" addrtype="ipv4"/>'
        yield '<ports>'
        for port, product, version, vendor in (
                (80, "nginx", "1.24.0", "igor_sysoev"),
                (8080, "Apache httpd", "2.4.52", "apache")):
            yield (f'<port protocol="tcp" portid="{port}">'
                   f'<state state="open" reason="syn-ack" reason_ttl="64"/>'
                   f'<service name="http" product="{product}" version="{version}"'
                   f' method="probed" conf="10">'
                   f'<cpe>cpe:/a:{vendor}:{product.split()[0].lower()}:{version}</cpe>'
                   f'</service>'
                   f'<script id="http-title" output="Host {i}">{i}</script>'
                   f'</port>')
        yield '</ports></host>'
    yield '<runstats><finished time="0"/></runstats>'
    yield '</nmaprun>'


def _nuclei_lines(count: int):
    """一份 nuclei `-jsonl`，总行数 ≈ `count`（一行一条命中）。

    形状按真实输出搭：`template-id` + `info` + `matched-at` + `request`/`response`
    正文。两处刻意的构造：

      * **重复度做高**（`i % 40` 的模板、`i % 5000` 的主机）—— 真实巡检里同一批
        模板会扫过整份资产清单，去重路径必须被压到；
      * **带 `request` / `response` 正文**，因为它们是这份格式里唯一的大字段，
        而"多大才算大"正是这条吞吐契约要回答的问题（它们**不入库**，但解析器
        仍然要 `json.loads` 整行）。
    """
    severities = ["info", "low", "medium", "high", "critical"]
    for i in range(count):
        host = f"host{i % 5000}.example.com"
        yield json.dumps({
            "template-id": f"CVE-2021-{4000 + i % 40}",
            "template-path": f"cves/2021/CVE-2021-{4000 + i % 40}.yaml",
            "info": {
                "name": f"Template {i % 40}",
                "author": ["pdteam"],
                "tags": ["cve", "rce"],
                "severity": severities[i % len(severities)],
                "classification": {"cve-id": [f"CVE-2021-{4000 + i % 40}"]},
            },
            "type": "http",
            "host": f"https://{host}",
            "port": "443",
            "scheme": "https",
            "url": f"https://{host}/",
            "matched-at": f"https://{host}/?id={i}",
            "request": f"GET /?id={i} HTTP/1.1\r\nHost: {host}\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\nServer: nginx\r\n\r\n",
            "ip": f"203.0.113.{i % 250}",
            "timestamp": "2026-09-19T10:00:00.000000Z",
            "matcher-name": "status-200",
            "matcher-status": True,
        }, ensure_ascii=False)


def _timed(cls, lines) -> tuple[float, int]:
    ctx = OfflineContext()
    t0 = time.perf_counter()
    count = sum(1 for _ in cls().parse(lines, ctx))
    return time.perf_counter() - t0, count


@pytest.mark.parametrize("name,cls,make", [
    ("domainlist", domainlist.DomainListParser, _domain_lines),
    ("httpx", httpx.HttpxParser, _httpx_lines),
    ("nmap", nmap.NmapParser, _nmap_lines),
    ("nuclei", nuclei.NucleiParser, _nuclei_lines),
])
def test_parse_throughput_100k_lines(name, cls, make):
    """`LINES` 对 nmap 是"约多少行 XML"，不是"多少条记录" —— 一行一条记录是
    domainlist 的形状，而 nmap 的 `<host>` 一行能带出十几条记录。契约的口径仍然
    是**行**（它是解析器唯一按行计费的地方），记录数只报不判。
    """
    lines = list(make(LINES))
    elapsed, records = _timed(cls, lines)
    per_line = elapsed / len(lines) * 1e6
    message = (f"{name}: {len(lines)} 行 / {records} 条记录 用时 {elapsed:.2f}s "
               f"（{per_line:.1f} us/行）；预算 {BUDGET_SECONDS:.0f}s")
    print(message)
    if STRICT:
        assert elapsed < BUDGET_SECONDS, message


@pytest.mark.parametrize("name,cls,make", [
    ("domainlist", domainlist.DomainListParser, _domain_lines),
    ("httpx", httpx.HttpxParser, _httpx_lines),
    ("nmap", nmap.NmapParser, _nmap_lines),
    ("nuclei", nuclei.NucleiParser, _nuclei_lines),
])
def test_parse_cost_per_line_does_not_grow(name, cls, make):
    """规模翻 4 倍时每行成本不该涨 —— 涨了说明某个环节是 O(N²) 的。

    这条比绝对秒数更能防退化：换台机器绝对值会变，但"每行成本是否随规模上升"
    不会。阈值取 2.5x 是给噪声留的余量（缓存、GC、预热），不是精度。
    """
    # 用**实际行数**而不是 `LINES`：nmap 那个生成器按"每台主机约 8 行"换算，
    # 它的行数是近似的。拿 `LINES` 当分母会让它的比值偏掉几十个百分点，而这条
    # 断言的分辨率就在几十个百分点上。
    small_lines = list(make(LINES // 4))
    large_lines = list(make(LINES))
    small, _ = _timed(cls, small_lines)
    large, _ = _timed(cls, large_lines)
    ratio = (large / len(large_lines)) / (small / len(small_lines))
    message = (f"{name}: 每行成本 小规模 {small / len(small_lines) * 1e6:.1f} us "
               f"→ 大规模 {large / len(large_lines) * 1e6:.1f} us，比值 {ratio:.2f}x"
               f"（线性=1.00x）")
    print(message)
    if STRICT:
        assert ratio < 2.5, message


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
