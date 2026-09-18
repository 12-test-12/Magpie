r"""§9.2 契约项 · `magpie app` 的 **RSS < 120MB** 与 **冷启动 < 1s**（2.7 兑现）。

这两项从阶段 0 起就在计划书 §9.2 的表里挂着 ⬜（"服务未建"）。2.7 把服务建起来了，
所以它们从"没法问"变成"可以单独质问"：

    py -3.13 -m pytest bench/test_perf_app.py -q              # 只报告实测值
    MAGPIE_PERF_STRICT=1 py -3.13 -m pytest bench/test_perf_app.py -q   # 当契约

**为什么必须起真子进程，而不是在测试进程里 create_app**:

    RSS 是**整个进程**的数字。在 pytest 进程里量，量到的是 pytest + 插件 +
    测试夹具 + 被测服务四样东西的和 —— 那个数字对"用户双击 magpie app 之后
    占多少内存"什么也没说。测试进程的基线本身就有几十 MB，足够把一个达标的
    服务量成不达标。

**冷启动的口径**: 从 `Popen` 到**第一次 `/api/v1/meta` 返回 200**。
    不是"到 URL 打印出来" —— 那之后 uvicorn 还没开始 accept。取"能被真正用上"
    的那一刻，才对应"双击之后多久能用"。

**RSS 的口径**: 整轮场景（启动 → 读几次 → 导入一个文件 → 再读）里的**峰值**。
    只量启动后那一瞬间会漏掉最贵的那段（导入路径里的解析器与归档）。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRICT = os.environ.get("MAGPIE_PERF_STRICT") == "1"

#: §9.2 契约
RSS_BUDGET_BYTES = 120 * 1024 * 1024
COLD_START_BUDGET_SECONDS = 1.0

#: 子进程启动后等 URL 的上限（超过它说明起不来，不是"慢"）
STARTUP_TIMEOUT = 30.0

_URL_RE = re.compile(r"URL: (http://127\.0\.0\.1:\d+)/\?token=(\S+)")


# ---------------------------------------------------------------------------
# RSS：跨平台取子进程的工作集
# ---------------------------------------------------------------------------


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _rss_bytes(pid: int) -> int:
    """子进程当前的工作集（Windows）/ maxrss（POSIX），单位字节。

    Windows 上用 `GetProcessMemoryInfo`（psapi）而不是 `resource` —— 后者在
    Windows 上根本没有。POSIX 上 `ru_maxrss` 是**峰值**（KB），与 Windows 的
    WorkingSetSize（瞬时）口径略有不同，但两者都是"RSS 量级"的保守近似，
    对 120MB 这个量级的判定足够。
    """
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:                                   # pragma: no cover
            raise OSError(f"OpenProcess 失败：{ctypes.get_last_error()}")
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                              counters.cb):   # pragma: no cover
                raise OSError(f"GetProcessMemoryInfo 失败：{ctypes.get_last_error()}")
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    import resource                                        # pragma: no cover
    return int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * 1024


# ---------------------------------------------------------------------------
# 起服务
# ---------------------------------------------------------------------------


class Server:
    """一个真的 `magpie app` 子进程 + 拿在手里的 URL / token / PID。"""

    def __init__(self, proc: subprocess.Popen, base: str, token: str, cold_start: float):
        self.proc = proc
        self.base = base
        self.token = token
        self.cold_start = cold_start
        self.peak_rss = 0

    def sample_rss(self) -> int:
        self.peak_rss = max(self.peak_rss, _rss_bytes(self.proc.pid))
        return self.peak_rss

    def get(self, path: str, timeout: float = 10.0):
        request = urllib.request.Request(
            self.base + path, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, payload: dict, timeout: float = 10.0):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:             # pragma: no cover
                self.proc.kill()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("magpie-app")
    data_dir = workdir / "data"
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "magpie.cli", "app",
         "--db", str(data_dir / "magpie.db"), "--data-dir", str(data_dir),
         "--host", "127.0.0.1", "--port", "0", "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base, token = _wait_for_url(proc)
        _wait_for_ready(base, token, deadline=started + STARTUP_TIMEOUT)
    except Exception:
        proc.terminate()
        raise
    server = Server(proc, base, token, time.perf_counter() - started)
    server.sample_rss()
    yield server
    server.stop()


def _wait_for_url(proc: subprocess.Popen) -> tuple[str, str]:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    lines: list[str] = []
    while time.monotonic() < deadline:
        raw = proc.stdout.readline()
        if not raw:
            if proc.poll() is not None:                   # pragma: no cover
                raise AssertionError(
                    "magpie app 没打印 URL 就退出了：\n" + "".join(lines))
            continue
        line = raw.decode("utf-8", "replace").rstrip()
        lines.append(line)
        match = _URL_RE.search(line)
        if match:
            return match.group(1), match.group(2)
    raise AssertionError(f"{STARTUP_TIMEOUT}s 内没等到 URL：\n" + "".join(lines))


def _wait_for_ready(base: str, token: str, *, deadline: float) -> None:
    request = urllib.request.Request(base + "/api/v1/meta",
                                     headers={"Authorization": f"Bearer {token}"})
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = exc
            time.sleep(0.02)
    raise AssertionError(f"服务起来了但不接受请求：{last}")


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


def test_cold_start_under_1s(server):
    elapsed = server.cold_start
    msg = (f"冷启动 {elapsed * 1000:.0f}ms（契约 {COLD_START_BUDGET_SECONDS * 1000:.0f}ms；"
           f"口径：Popen → 第一次 /api/v1/meta 返回 200）")
    print(msg)
    if STRICT:
        assert elapsed < COLD_START_BUDGET_SECONDS, f"§9.2 契约未达标：{msg}"


def test_rss_under_120mb_across_a_full_scenario(server):
    """启动 → 读几次 → 真导入一个文件 → 再读，量**峰值**。"""
    server.get("/api/v1/meta")
    server.post("/api/v1/projects", {"name": "perf", "scope": {"include": ["*.example.com"]}})
    server.sample_rss()

    source = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"
    assert source.is_file(), f"缺少夹具 {source}"
    submitted = server.post("/api/v1/imports",
                            {"paths": [str(source)], "project": "perf"})
    import_id = submitted["data"]["import_id"]
    deadline = time.monotonic() + 60
    state = "queued"
    while time.monotonic() < deadline:
        record = server.get(f"/api/v1/imports/{import_id}")["data"]
        state = record["state"]
        if state not in ("queued", "running"):
            break
        server.sample_rss()
        time.sleep(0.05)
    assert state == "done", f"导入没跑完（state={state}）"
    server.get("/api/v1/entities")
    server.get("/api/v1/doctor")
    peak = server.sample_rss()

    msg = (f"`magpie app` 峰值 RSS {peak / 1024 / 1024:.1f}MB"
           f"（契约 {RSS_BUDGET_BYTES / 1024 / 1024:.0f}MB；场景含一次真实导入）")
    print(msg)
    if STRICT:
        assert peak < RSS_BUDGET_BYTES, f"§9.2 契约未达标：{msg}"


def test_loopback_only_and_token_required(server):
    """顺带把两条安全红线放在**真进程**上验一遍（不是只验 create_app 的入参）。"""
    # 1) 只监听回环：从本机非回环地址连不上（这里退而求其次，断言绑定地址本身）
    base_host = server.base.split("//", 1)[1].split(":", 1)[0]
    assert base_host == "127.0.0.1", f"服务绑在了 {base_host}"

    # 2) 不带 token → 401
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(server.base + "/api/v1/meta", timeout=5)
    assert excinfo.value.code == 401
