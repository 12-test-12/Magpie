"""导入请求的登记表 —— `POST /imports` 提交的东西，以及它的事件流（2.7 任务 3）。

一个容易踩的坑，先说清楚：**`/imports/{id}` 里的 id 不是 `jobs.id`。**

    一次 `POST /imports {paths:[...]}` 会导入**多个文件**，而数据层的纪律是
    "每个文件一个 job"（`ingest` 的注释写得很明白：这样 undo 的粒度才是文件）。
    于是两者的基数根本不同：1 个导入请求 → N 个 job。

    所以这里给导入请求自己的 id（`imp-1`、`imp-2`……），SSE 的 `/imports/{id}/events`
    按它订阅；事件体里带**真实**的 `job_id`，`/imports/{id}/undo` 撤销这一批的全部
    job。把 `{id}` 说成 job 会更顺口，但那是错的 —— 顺口的错误最难发现。

取消的语义（与数据层对齐）:
    `cancel` 只是把 id 记进 `WriteQueue` 的取消表，正在跑的 `ingest` 在下一个
    checkpoint 收尾 —— **不打断**，因为"已提交部分仍可 undo"靠的就是每个 job
    被干净地收尾（`state='partial'`）。硬中断会留下一个没人收尾的作业。

进度从哪来:
    全部来自 `ingest` 的 `on_progress` 回调（2.7 顺带给 core 加的那一个可选项）。
    API 层**不自己数数** —— 那样迟早会出现"进度条说 80%，报告说 100%"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import ingest, ops
from .events import EventBroker
from .writer import WriteQueue, WriteTicket, WriterStopped

__all__ = ["ImportRecord", "ImportRegistry", "IMPORT_ID_PREFIX"]

#: 导入请求 id 的前缀。带前缀是为了让人一眼看出它不是 job_id。
IMPORT_ID_PREFIX = "imp-"

#: 登记表最多留多少个导入请求（超出后回收最老的**已终结**记录）。
DEFAULT_KEEP = 100

#: 每个导入请求的文件状态里，哪些算"这一轮没成功"。
_BAD_FILE_STATES = frozenset({"failed"})


@dataclass
class ImportRecord:
    id: str
    project_id: int
    paths: list[str]
    parser: str | None = None
    source: str | None = None
    dry_run: bool = False
    archive: str = "archived"
    state: str = "queued"          # queued | running | done | failed | cancelled
    job_ids: list[int] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    ticket_id: int | None = None
    #: 报告原样留一份 —— "报告是唯一的交付通道"（ingest 的原话），API 不另造一份。
    report: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    def as_dict(self, *, include_report: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "import_id": self.id, "project_id": self.project_id,
            "state": self.state, "paths": list(self.paths),
            "parser": self.parser, "source": self.source, "dry_run": self.dry_run,
            "archive": self.archive, "job_ids": list(self.job_ids),
            "totals": dict(self.totals), "problems": list(self.problems),
            "error": self.error, "ticket_id": self.ticket_id,
        }
        if include_report:
            out["report"] = self.report
        return out


class ImportRegistry:
    """导入请求的登记表 + 它到写队列 / 事件总线的桥。线程安全。"""

    def __init__(self, *, broker: EventBroker, writer: WriteQueue,
                 data_dir: Path, actor: str = "api", keep: int = DEFAULT_KEEP) -> None:
        self.broker = broker
        self.writer = writer
        self.data_dir = Path(data_dir)
        self.actor = actor
        self.keep = int(keep)
        self._lock = threading.Lock()
        self._records: dict[str, ImportRecord] = {}
        self._order: list[str] = []
        self._next_id = 0

    # -- 查询 ---------------------------------------------------------------
    def get(self, import_id: str) -> ImportRecord | None:
        with self._lock:
            return self._records.get(str(import_id))

    def list(self) -> list[ImportRecord]:
        with self._lock:
            return [self._records[i] for i in reversed(self._order)
                    if i in self._records]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            states: dict[str, int] = {}
            for record in self._records.values():
                states[record.state] = states.get(record.state, 0) + 1
        return {"imports": len(self._records), "states": states,
                "writer": self.writer.stats.as_dict()}

    # -- 提交 ---------------------------------------------------------------
    def create(self, *, project_id: int, paths: list[str], parser: str | None = None,
               source: str | None = None, dry_run: bool = False,
               archive: str = "archived") -> ImportRecord:
        """登记一个导入请求并立刻排队。**不等它跑完** —— 进程不在这儿。"""
        record = self._register(project_id=project_id, paths=paths, parser=parser,
                                source=source, dry_run=dry_run, archive=archive)
        self.broker.publish(record.id, "queued", {
            "import_id": record.id, "project_id": project_id,
            "paths": list(record.paths), "dry_run": dry_run,
        })
        try:
            ticket = self.writer.submit(f"import {record.id}", self._task(record))
        except WriterStopped as exc:
            self._finish(record, state="failed", error=str(exc))
            self.broker.publish(record.id, "error",
                                {"import_id": record.id, "error": str(exc)},
                                terminal=True)
            raise
        record.ticket_id = ticket.id
        return record

    def cancel(self, import_id: str) -> ImportRecord | None:
        """登记取消。**不打断**正在跑的写 —— 见模块文档。"""
        record = self.get(import_id)
        if record is None:
            return None
        if record.terminal:
            return record
        self.writer.request_cancel(record.id)
        self.broker.publish(record.id, "cancel_requested", {"import_id": record.id})
        return record

    def undo(self, import_id: str) -> WriteTicket | None:
        """撤销这个导入请求的**全部** job（按 id 倒序：后写的先撤）。"""
        record = self.get(import_id)
        if record is None:
            return None
        job_ids = sorted(record.job_ids, reverse=True)

        def task(conn):
            reports = [ops.undo(conn, jid, actor=self.actor) for jid in job_ids]
            return {"import_id": record.id, "undone": job_ids, "reports": reports}

        return self.writer.submit(f"undo {record.id}", task)

    # -- 内部 ---------------------------------------------------------------
    def _register(self, **kwargs) -> ImportRecord:
        with self._lock:
            self._next_id += 1
            record = ImportRecord(id=f"{IMPORT_ID_PREFIX}{self._next_id}", **kwargs)
            self._records[record.id] = record
            self._order.append(record.id)
            self._prune_locked()
        self.broker.open(record.id)
        return record

    def _prune_locked(self) -> None:
        while len(self._order) > self.keep:
            for import_id in list(self._order):
                record = self._records.get(import_id)
                if record is None or record.terminal:
                    self._order.remove(import_id)
                    self._records.pop(import_id, None)
                    self.broker.forget(import_id)
                    break
            else:
                return

    def _finish(self, record: ImportRecord, *, state: str,
                error: str | None = None) -> None:
        record.state = state
        if error is not None:
            record.error = error

    def _task(self, record: ImportRecord):
        """把一次导入包成一个写任务。**这个函数体在写线程里跑。**"""
        def run(conn):
            record.state = "running"
            self.broker.publish(record.id, "started", {
                "import_id": record.id, "project_id": record.project_id,
                "paths": list(record.paths),
            })
            opts = ingest.IngestOptions(
                project_id=record.project_id, data_dir=self.data_dir,
                parser=record.parser, source_name=record.source,
                archive=record.archive, dry_run=record.dry_run, actor=self.actor,
                # 与 CLI / pipeline 同一条：报告副本落进 `reports/`。GUI 现在是
                # 主入口，若只有 CLI 落盘，`magpie doctor` 的"未知字段"就会在
                # 一个纯 GUI 的数据区里静默为空 —— 那正是它要防的那种静默。
                report_dir=self.data_dir / "reports",
                cancel_check=lambda: self.writer.cancel_requested(record.id),
                on_progress=lambda payload: self.broker.publish(
                    record.id, str(payload.get("event") or "progress"), payload),
            )
            try:
                report = ingest.ingest_paths(conn, opts, list(record.paths))
            except Exception as exc:                       # 整轮崩了（单文件的失败在报告里）
                message = f"{type(exc).__name__}: {exc}"
                self._finish(record, state="failed", error=message)
                self.broker.publish(record.id, "error",
                                    {"import_id": record.id, "error": message},
                                    terminal=True)
                raise
            record.report = report
            record.job_ids = list(report.get("job_ids") or [])
            record.totals = dict(report.get("totals") or {})
            record.problems = list(report.get("problems") or [])
            states = [str(f.get("state")) for f in report.get("files") or []]
            if self.writer.cancel_requested(record.id):
                state = "cancelled"
            elif states and all(s in _BAD_FILE_STATES for s in states):
                state = "failed"
            else:
                state = "done"
            self._finish(record, state=state)
            self.broker.publish(record.id, "done", {
                "import_id": record.id, "state": state,
                "job_ids": list(record.job_ids), "totals": dict(record.totals),
                "file_states": states, "problems": list(record.problems),
                "dry_run": record.dry_run,
            }, terminal=True)
            self.writer.clear_cancel(record.id)
            return record.as_dict(include_report=True)

        return run
