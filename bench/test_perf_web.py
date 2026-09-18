r"""2.8 契约项 · GUI **首屏 < 1.5s**。

    py -3.13 -m pytest bench/test_perf_web.py -q              # 只报告实测值
    MAGPIE_PERF_STRICT=1 py -3.13 -m pytest bench/test_perf_web.py -q   # 当契约

**口径说清楚，因为它决定了这条测试证明得了什么、证明不了什么**:

    量的是"**服务端把首屏需要的东西交出去**"这一段 ——

        GET /                        → index.html
        GET 它引用的每一个静态资源   → JS / CSS

    起的是**真子进程**（`magpie app`），走的是**真 HTTP**，不是 `create_app`
    加 TestClient。理由与 `test_perf_app.py` 同：那段路才是用户双击之后走的路。

    **它不包含浏览器解析、执行与绘制的时间** —— 那需要一个真浏览器，而这个
    仓库不带浏览器。补上这一段的办法是给一个**能支撑它的规模**：产物的字节数
    （下面一起打印）。一个 300KB 的本地同源产物与 3MB 的产物，浏览器那一段的
    差距是数量级的；把字节数写在同一条测试里，是为了让"没量到的那一半"有一个
    可看、可比的数字，而不是一句"反正很快"。

    本地回环上，服务端这一段通常在 10ms 量级；1.5s 的预算实际上全部留给浏览器。
    所以**这条测试在接近预算时才会红** —— 它是一道护栏，不是一把尺子。
"""

from __future__ import annotations

import os
import re
import time

import pytest

from test_perf_app import ROOT, Server, _wait_for_ready, _wait_for_url  # noqa: F401

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

#: 2.8 交付标准表：首屏 < 1.5s（本地）。
FIRST_PAINT_BUDGET_SECONDS = 1.5

#: 产物字节数的护栏。不是"美观"标准 —— 它守的是"没被量到的那一半"（浏览器
#: 解析 + 执行）。2.8 的实测约 311KB（JS 289KB + CSS 22KB）。
BUNDLE_BUDGET_BYTES = 900 * 1024

_ASSET_RE = re.compile(r'(?:src|href)="(/web/[^"]+)"')
_URL_RE = re.compile(r"URL: (http://127\.0\.0\.1:\d+)/\?token=(\S+)")


import subprocess  # noqa: E402  （放在 import 段之后只为让上面的说明连成一段）
import sys  # noqa: E402


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """一个真的 `magpie app` 子进程。

    刻意**不复用** `test_perf_app.py` 的 fixture：那个 fixture 会跑一次真实导入
    （量 RSS 峰值用），而首屏契约与导入无关 —— 把它拖进来只会让这条最便宜的
    测试变成最贵的之一。起进程的零件仍然共用（`Server` / `_wait_for_url`）。
    """
    workdir = tmp_path_factory.mktemp("magpie-web")
    data_dir = workdir / "data"
    proc = subprocess.Popen(
        [sys.executable, "-m", "magpie.cli", "app",
         "--db", str(data_dir / "magpie.db"), "--data-dir", str(data_dir),
         "--host", "127.0.0.1", "--port", "0", "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base, token = _wait_for_url(proc)
        _wait_for_ready(base, token, deadline=time.monotonic() + 30)
    except Exception:
        proc.terminate()
        raise
    instance = Server(proc, base, token, 0.0)
    yield instance
    instance.stop()


def _raw_get(server: Server, path: str) -> bytes:
    """取**原始字节**。`Server.get` 会顺手 `json.loads`，而首屏要的是 HTML/CSS/JS。"""
    import urllib.request
    request = urllib.request.Request(
        server.base + path, headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def test_first_paint_under_1_5s(server):
    """服务端交付首屏的全部字节要多久 —— 外加产物规模。"""
    started = time.perf_counter()
    html = _raw_get(server, "/").decode("utf-8")
    assets = _ASSET_RE.findall(html)
    assert assets, ("index.html 没有引用 /web/ 下的任何资源 —— 说明静态资源没被"
                    "本地打包，或者 `base` 没配成 /web/")
    total = len(html)
    for asset in assets:
        total += len(_raw_get(server, asset))
    elapsed = time.perf_counter() - started

    msg = (f"首屏服务端交付 {elapsed * 1000:.1f}ms"
           f"（契约 {FIRST_PAINT_BUDGET_SECONDS * 1000:.0f}ms；"
           f"口径：GET / + {len(assets)} 个资源，不含浏览器解析/渲染）；"
           f"产物共 {total / 1024:.1f}KB")
    print(msg)
    if STRICT:
        assert elapsed < FIRST_PAINT_BUDGET_SECONDS, f"2.8 契约未达标：{msg}"


def test_bundle_size_stays_in_the_same_order_of_magnitude(server):
    """产物体积的护栏。**它守的是"没被量到的那一半"**（浏览器解析与执行）。"""
    html = _raw_get(server, "/").decode("utf-8")
    total = len(html) + sum(len(_raw_get(server, a)) for a in _ASSET_RE.findall(html))
    msg = f"首屏产物 {total / 1024:.1f}KB（护栏 {BUNDLE_BUDGET_BYTES / 1024:.0f}KB）"
    print(msg)
    if STRICT:
        assert total < BUNDLE_BUDGET_BYTES, msg


def test_served_page_references_nothing_external(server):
    """**从服务真正交出去的那份字节**上再验一次零出站。

    `check.py` 的 R7-ASSETS 扫的是磁盘上的文件；这条验的是 HTTP 真的交出去的
    那一份。两者本该一致 —— 而"本该一致"正是值得写一条断言的地方（中间可能
    插进一层重写、一个代理、一次误配的 `base`）。
    """
    html = _raw_get(server, "/").decode("utf-8")
    hosts = set()
    for path in ["/"] + _ASSET_RE.findall(html):
        text = _raw_get(server, path).decode("utf-8", "replace")
        hosts |= set(re.findall(r"(?:https?:)?//([A-Za-z0-9._-]+\.[A-Za-z]{2,})", text))
    hosts -= {"www.w3.org"}                      # XML 命名空间：是标识符，不是取回目标
    assert not hosts, f"静态页面引用了外部域名：{sorted(hosts)}"
