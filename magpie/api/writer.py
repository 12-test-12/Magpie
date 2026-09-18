"""写者唯一 —— 服务进程内的**单写线程 + 写队列**（计划书 §6.1 / 2.7 任务 2）。

数据层的 `manual.py`（ManualWriter）已经是这套纪律的第一个实现：单写队列 +
乐观版本 + 并集合并。本模块把它抬到**进程级**：不只是 manual 写，而是
**所有**写（导入、undo、redo、merge、tag、layout、inbox）都排进同一条队列，
由唯一一个线程、唯一一条数据库连接执行。

为什么"每请求一条连接 + 自己开事务"不行:

    SQLite 的写事务是**库级**的。两个请求各自 `BEGIN IMMEDIATE`，第二个只会拿到
    `database is locked`；靠 `busy_timeout` 硬等，结果是"两个导入并发"变成
    "一个导入加一次随机超时" —— 表面能跑，实际是对调用方撒谎。真正的问题是：
    并发写从来没有被**表达**过，所以它既不可控也不可测。单写线程把它变成一句
    可以写进测试的话：**任何时刻只有一个写者在跑**（`WriterStats.concurrent_max`）。

连接的所有权（这是本模块最要紧的一条约束）:

    `WriteQueue` 建的连接**只属于写线程**。读走各自的连接（`app` 每请求一条，
    WAL 下读写不互相阻塞）。谁把这条连接借出去读一次，谁就把"写者唯一"作废了 ——
    因为读会把一个**未提交**的事务看进眼里。

取消: 导入是唯一长到需要中断的写。队列持有 `cancel_requested(job)`，
`ingest` 的 `cancel_check` 回调去问它 —— 取消**不打断**正在跑的写函数，
只是让它在下一个 checkpoint 优雅收尾（已提交部分仍可 undo，这是数据层本来的语义）。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import store

__all__ = ["WriteQueue", "WriteTicket", "WriterStats", "WriterStopped"]

#: 队列最多积压多少个待执行的写。满了 `submit` 直接拒绝（快速失败 > 无限堆积）。
DEFAULT_MAX_PENDING = 256


class WriterStopped(RuntimeError):
    """队列已停（服务正在关闭），或已积压满 —— 不再接受新的写。"""


@dataclass
class WriterStats:
    """写线程的自证材料 —— 测试断言它，而不是断言"看起来没问题"。"""

    tasks: int = 0
    failed: int = 0
    #: 并发写者的峰值。**写者唯一的反例测试就断言它是 1**。
    concurrent_max: int = 0
    #: 执行过任务的线程 id（去重后应当只有一个）。线程唯一性的直接证据。
    thread_ids: set = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {"tasks": self.tasks, "failed": self.failed,
                "concurrent_max": self.concurrent_max,
                "thread_ids": sorted(self.thread_ids)}


@dataclass
class WriteTicket:
    """一个排队中的写。`future` 拿结果，`label` 只为人看得懂。"""

    id: int
    label: str
    future: Future
    #: 真正要执行的东西：`fn(conn) -> Any`。**不属于对外契约**，故不进 repr。
    fn: Callable[[sqlite3.Connection], Any] = field(repr=False, default=None)

    def result(self, timeout: float | None = None) -> Any:
        return self.future.result(timeout)

    def done(self) -> bool:
        return self.future.done()


class WriteQueue:
    """单写线程 + 写队列。`submit` 可从任意线程调用；执行只在写线程里发生。"""

    def __init__(self, db_path: str | Path, *, actor: str = "api",
                 max_pending: int = DEFAULT_MAX_PENDING,
                 on_task: Callable[[WriteTicket], None] | None = None) -> None:
        self.db_path = str(db_path)
        self.actor = actor
        self.max_pending = int(max_pending)
        self.stats = WriterStats()
        #: 建连失败时的可读原因（`/healthz` 会把它露出来，别让写线程静默地死掉）。
        self.error: str | None = None
        #: 任务开始/结束的回调（把"作业阶段"也变成事件）。在**写线程**里调 ——
        #: 它必须够快且不许阻塞，是个发布者不是消费者。
        self.on_task = on_task

        self._queue: queue.Queue = queue.Queue(maxsize=self.max_pending)
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._ready = threading.Event()
        self._stopping = False
        self._lock = threading.Lock()
        self._next_id = 0
        self._active = 0
        self._cancelled: set[str] = set()

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> "WriteQueue":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="magpie-writer",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("写线程 10s 内没起来 —— 数据库打不开？")
        return self

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stopping = True
        # 毒丸要**真的送进去**。早先写成 `put_nowait` + 吞掉 Full，结果是：
        # 关停时队列恰好是满的（两个导入还没跑完就按了 Ctrl-C），毒丸被丢掉，
        # 写线程把队列清空后**永远阻塞在 get() 上**，stop() 只能超时报错。
        # 队列满不是异常情况，是关停时的常态之一 —— 必须等它腾出位置。
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._queue.put(None, timeout=0.2)
                break
            except queue.Full:
                if not self._thread.is_alive() or time.monotonic() > deadline:
                    break
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():               # pragma: no cover
            raise RuntimeError(f"写线程 {timeout}s 内没退出（有写卡住了？）")
        self._thread = None

    def __enter__(self) -> "WriteQueue":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    # -- 提交 ---------------------------------------------------------------
    def submit(self, label: str, fn: Callable[[sqlite3.Connection], Any]) -> WriteTicket:
        """排一个写。`fn(conn)` 在写线程里执行，返回值/异常原样回到 `future`。

        **不阻塞**：提交者不该等写完成（HTTP 请求线程要回去接下一个请求）。
        调用方拿 `ticket.result()` 去等结果，或者干脆不等（导入就是这样 ——
        它的进度走 SSE，不走进 HTTP 响应体）。
        """
        if self._stopping:
            raise WriterStopped("写队列已停止（服务正在关闭）")
        with self._lock:
            if self._stopping:                    # 与 stop() 抢一个锁窗口
                raise WriterStopped("写队列已停止（服务正在关闭）")
            self._next_id += 1
            ticket = WriteTicket(id=self._next_id, label=str(label),
                                 future=Future(), fn=fn)
        try:
            self._queue.put_nowait(ticket)
        except queue.Full:
            raise WriterStopped(
                f"写队列已满（{self.max_pending} 个待执行）—— 拒绝继续排队。"
                f"上一个导入可能还没跑完；等它结束再提交。") from None
        return ticket

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def thread_id(self) -> int | None:
        return None if self._thread is None else self._thread.ident

    # -- 取消登记表 ---------------------------------------------------------
    def request_cancel(self, job: str) -> None:
        with self._lock:
            self._cancelled.add(str(job))

    def cancel_requested(self, job: str | None) -> bool:
        if job is None:
            return False
        with self._lock:
            return str(job) in self._cancelled

    def clear_cancel(self, job: str) -> None:
        with self._lock:
            self._cancelled.discard(str(job))

    def cancelled_jobs(self) -> list[str]:
        with self._lock:
            return sorted(self._cancelled)

    # -- 写线程 -------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = store.connect(self.db_path, check_same_thread=False)
        store.init_db(conn)
        # store.connect 没设 busy_timeout（默认 0 = 撞锁立刻报错）。服务形态下
        # 唯一可能与写线程抢锁的是**另一个进程**（同一个库上再开一个 magpie），
        # 那种情况我们要等，而不是把 "database is locked" 甩给用户。
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _run(self) -> None:
        try:
            self._conn = self._connect()
        except Exception as exc:                  # pragma: no cover —— 建连失败
            self._stopping = True
            self.error = f"写线程建连失败：{type(exc).__name__}: {exc}"
            self._ready.set()
            # 连库都打不开时，把异常交给排队者，而不是让 submit 永远挂着。
            self._fail_pending(self.error)
            return
        self._ready.set()

        while True:
            ticket = self._queue.get()
            if ticket is None:
                break
            self._execute(ticket)

        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _execute(self, ticket: WriteTicket) -> None:
        conn = self._conn
        if conn is None:                          # pragma: no cover
            ticket.future.set_exception(WriterStopped("写线程没有连接"))
            return
        with self._lock:                          # 并发度的**证据**：进一个人加一
            self._active += 1
            self.stats.concurrent_max = max(self.stats.concurrent_max, self._active)
            self.stats.tasks += 1
            self.stats.thread_ids.add(threading.get_ident())
        if self.on_task is not None:
            try:
                self.on_task(ticket)
            except Exception:                     # 旁路回调不该毁掉这次写
                traceback.print_exc()
        try:
            ticket.future.set_result(ticket.fn(conn))
        except BaseException as exc:              # noqa: BLE001 —— 原样回给提交者
            self.stats.failed += 1
            self._rollback_quietly(conn)
            ticket.future.set_exception(exc)
        finally:
            with self._lock:
                self._active -= 1

    def _rollback_quietly(self, conn: sqlite3.Connection) -> None:
        """写函数抛异常后，连接可能停在一个半开的事务里 —— 不清掉它，
        下一个写会撞上 "cannot start a transaction within a transaction"。"""
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:                     # pragma: no cover
            pass

    def _fail_pending(self, message: str) -> None:    # pragma: no cover
        while True:
            try:
                ticket = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(ticket, WriteTicket) and not ticket.done():
                ticket.future.set_exception(WriterStopped(message))
