"""写者唯一：两个并发导入不破坏 INV（2.7 交付标准表的"并发"那一行）。

这条标准的反面才是它的价值所在 —— "并发写不出问题"在没有单写队列时**也**能
偶发地成立（时间窗口小、机器快），所以它必须靠**证据**而不是靠"跑一遍没炸"：

  * `concurrent_max == 1`：写线程自己记的并发峰值。它是**运行时事实**，不是
    代码走查的结论。任何绕开队列的写路径都会把它顶到 2。
  * `thread_ids` 去重后只有一个：所有写的执行线程是同一个。
  * 并发导入后 `integrity_check` 全绿 + 事实计数与**串行**执行逐项相等。
  * 反例：两条各自 `BEGIN IMMEDIATE` 的连接当场撞锁 —— 这就是队列在挡的东西。
    没有这一段，"为什么要有队列"就只是一句设计口号。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app
from magpie.api.writer import WriteQueue, WriterStopped
from magpie.core import store

ROOT = Path(__file__).resolve().parents[3]
SUBFINDER = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"
HTTPX = ROOT / "bench" / "e2e" / "recon" / "httpx.jsonl"


# ---------------------------------------------------------------------------
# 队列本身
# ---------------------------------------------------------------------------


def test_all_writes_run_on_one_thread(tmp_path):
    queue = WriteQueue(tmp_path / "magpie.db")
    with queue:
        threads = set()

        def task(conn):                                # noqa: ANN001
            threads.add(threading.get_ident())
            return threading.get_ident()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [pool.submit(lambda: queue.submit("t", task).result(timeout=30))
                       for _ in range(32)]
        executed_on = {future.result() for future in results}

    assert len(executed_on) == 1, f"写跑了不止一个线程：{executed_on}"
    assert queue.stats.concurrent_max == 1, (
        f"并发峰值 {queue.stats.concurrent_max} —— 写者唯一被破坏了")
    assert queue.stats.thread_ids == {queue.thread_id} or len(queue.stats.thread_ids) == 1


def test_submit_returns_a_future_with_the_result(tmp_path):
    queue = WriteQueue(tmp_path / "magpie.db")
    with queue:
        ticket = queue.submit("answer", lambda conn: 42)
        assert ticket.result(timeout=10) == 42
        assert ticket.done()


def test_a_failed_write_does_not_poison_the_next_one(tmp_path):
    """写函数抛异常后连接可能停在半开事务里 —— 下一次写必须仍然能跑。

    这是最容易在真实使用中咬人的一类 bug：第一个导入失败之后，**之后每一次**
    写都报 "cannot start a transaction within a transaction"。
    """
    queue = WriteQueue(tmp_path / "magpie.db")
    with queue:
        def boom(conn):                                # noqa: ANN001
            raise RuntimeError("人工失败")

        with pytest.raises(RuntimeError):
            queue.submit("boom", boom).result(timeout=10)
        assert queue.submit("ok", lambda conn: "fine").result(timeout=10) == "fine"
        assert queue.stats.failed == 1


def test_queue_refuses_when_full(tmp_path):
    """队列满了要**快速失败**，不是无限堆积（无限堆积 = 把内存当队列用）。

    顺带覆盖关停时**队列恰好是满的**那条路：这个用例第一版就是这么写出来的，
    而它当场逮到一个真 bug —— `stop()` 把毒丸 `put_nowait` 进去、满了就吞掉，
    于是写线程清空队列后永远阻塞在 `get()` 上，stop() 只能超时。
    """
    queue = WriteQueue(tmp_path / "magpie.db", max_pending=1)
    queue.start()
    started, release = threading.Event(), threading.Event()
    try:
        queue.submit("hold", lambda conn: (started.set(), release.wait(10))[0])
        assert started.wait(10), "写线程没有开始执行第一个任务"
        queue.submit("hold2", lambda conn: release.wait(10))   # 占满队列
        with pytest.raises(WriterStopped):
            queue.submit("overflow", lambda conn: None)
    finally:
        release.set()
        queue.stop()                                           # 满队列下关停


def test_two_raw_writers_collide(tmp_path):
    """**反例**：没有队列时，两个写者会撞锁。这就是队列存在的理由。

    不开 busy_timeout（数据层的默认值就是 0）：撞上就是立刻报错，
    而不是"等一会儿说不定就好了"—— 后者会把一个确定的设计缺陷
    伪装成一个偶发的超时。
    """
    db = tmp_path / "magpie.db"
    first = store.connect(db)
    store.init_db(first)
    second = store.connect(db)
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            second.execute("BEGIN IMMEDIATE")
        assert "locked" in str(excinfo.value).lower()
    finally:
        first.execute("ROLLBACK")
        first.close()
        second.close()


# ---------------------------------------------------------------------------
# 端到端：并发导入
# ---------------------------------------------------------------------------


def _counts(db_path: Path) -> dict:
    """**有效状态**（缓存投影），不是事实表的行数。

    刻意不含 `contributions`：undo 是**追加** retract 抑制事实，不删贡献
    （INV-01 无例外 —— 这正是"精确复原"能成立的原因，而不是它的反例）。
    拿事实表行数当"复原"的判据，会把一个正确的 undo 判成失败。
    """
    conn = store.connect(db_path)
    try:
        return {
            "entities": conn.execute(
                "SELECT COUNT(*) FROM entities WHERE is_current=1").fetchone()[0],
            "observations": conn.execute(
                "SELECT COUNT(*) FROM observations WHERE is_current=1").fetchone()[0],
            "relations": conn.execute(
                "SELECT COUNT(*) FROM relations WHERE is_current=1").fetchone()[0],
        }
    finally:
        conn.close()


def _run_imports(db_path: Path, data_dir: Path, batches: list[list[str]],
                 *, parallel: bool) -> dict:
    app = create_app(db_path=db_path, data_dir=data_dir, token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers, json={"name": "demo"})

        def submit(paths: list[str]) -> str:
            response = client.post("/api/v1/imports", headers=headers,
                                   json={"paths": paths, "project": "demo"})
            assert response.status_code == 202, response.text
            return response.json()["data"]["import_id"]

        if parallel:
            with ThreadPoolExecutor(max_workers=len(batches)) as pool:
                import_ids = list(pool.map(submit, batches))
        else:
            import_ids = [submit(paths) for paths in batches]

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            records = [app.state.ctx.imports.get(i) for i in import_ids]
            if all(r is not None and r.terminal for r in records):
                break
            time.sleep(0.05)
        states = [r.state for r in records if r is not None]
        assert states and all(s == "done" for s in states), f"导入没跑完：{states}"
        stats = app.state.ctx.writer.stats.as_dict()
    return stats


def test_concurrent_imports_do_not_break_invariants(tmp_path):
    """两个导入并发 → 事实层与**串行执行**逐项相等，且写者峰值恒为 1。"""
    parallel_root, serial_root = tmp_path / "parallel", tmp_path / "serial"
    batches = [[str(SUBFINDER)], [str(HTTPX)]]

    parallel_stats = _run_imports(parallel_root / "magpie.db",
                                  parallel_root / "data", batches, parallel=True)
    _run_imports(serial_root / "magpie.db", serial_root / "data", batches,
                 parallel=False)

    assert parallel_stats["concurrent_max"] == 1, (
        f"并发峰值 {parallel_stats['concurrent_max']} —— 写者唯一被破坏了")
    assert len(parallel_stats["thread_ids"]) == 1

    parallel_counts = _counts(parallel_root / "magpie.db")
    serial_counts = _counts(serial_root / "magpie.db")
    assert parallel_counts == serial_counts, (
        f"并发导入与串行导入的结果不一致：\n  并发 {parallel_counts}\n"
        f"  串行 {serial_counts}\n两条路本该写到同一个状态上。")

    # 不变量本身（数据层的体检）才是最终判决
    conn = store.connect(parallel_root / "magpie.db")
    try:
        report = store.integrity_check(conn)
        assert report["ok"], f"并发导入破坏了不变量：{report['violations']}"
    finally:
        conn.close()


def test_undo_after_concurrent_import_is_exact(tmp_path):
    """并发导入之后 undo 仍然精确 —— 队列保证了每个 job 都是干净的收尾。"""
    db, data = tmp_path / "magpie.db", tmp_path / "data"
    app = create_app(db_path=db, data_dir=data, token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers, json={"name": "demo"})
        before = _counts(db)

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda p: client.post(
                "/api/v1/imports", headers=headers,
                json={"paths": [p], "project": "demo"}).json()["data"]["import_id"],
                [str(SUBFINDER), str(HTTPX)]))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            records = [app.state.ctx.imports.get(i) for i in ids]
            if all(r is not None and r.terminal for r in records):
                break
            time.sleep(0.05)
        after = _counts(db)
        assert after != before, "导入没有产生任何事实（测不出 undo）"

        for import_id in ids:
            response = client.post(f"/api/v1/imports/{import_id}/undo", headers=headers)
            assert response.status_code == 200, response.text

        restored = _counts(db)
        assert restored == before, (
            f"undo 没有精确复原：\n  导入前 {before}\n  撤销后 {restored}")


def test_reads_cannot_write(tmp_path):
    """路由用的读连接带 `query_only` —— 顺手写一条会**当场报错**。

    这条断言守的是"路由里没有写"这件事本身：即使有人绕过队列写了条 UPDATE，
    它也跑不通，并且是立刻、确定地跑不通（而不是在并发里偶发地坏掉）。
    """
    app = create_app(db_path=tmp_path / "magpie.db", token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers, json={"name": "demo"})
        conn = app.state.ctx.connect_ro()
        try:
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                conn.execute("UPDATE projects SET name='x'")
            assert "readonly" in str(excinfo.value).lower()
        finally:
            conn.close()
