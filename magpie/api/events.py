"""SSE 事件总线 —— 导入进度从写线程扇出到任意多个订阅者（计划书 §6.2）。

**背压策略（交付标准表里"慢消费者不阻塞导入"那一行就是它）:**

    每个订阅者一条**有界**队列（`deque(maxlen=capacity)`）。写线程 `publish` 只做
    "追加 + 唤醒"，从不等待任何订阅者 —— 队列满了就丢**最旧**的事件，并记一笔
    `dropped`。丢掉的不是静默：下一个批次帧的 `meta.dropped` 会如实报出丢了多少
    条，事件流里还会插一条 `notice`。

为什么不是"满了就阻塞生产者"：那样一个卡住的浏览器标签页就能把导入卡死 ——
把一个**旁观者**变成了**参与者**。为什么不是"无限队列"：那样一个卡住的消费者
会把导入的内存吃光。丢最旧的进度事件是三者里唯一一个「最坏情况明确」的选择：
迟到的消费者看到的进度可能跳号，但**事实层永远是对的**（进度是旁路，不是数据）。

**1024 是刻意的**：一次导入的进度事件量级是 文件数 + 记录数/2000，正常情况下
远远够用 —— 会打满它的只有"消费者已经卡死"这一种情况，而那正是要丢帧的时候。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = ["Event", "Subscription", "EventBroker", "DEFAULT_CAPACITY", "DEFAULT_HISTORY"]

#: 每个订阅者的队列长度。见模块文档：打满它 = 消费者已卡死，此时丢帧是设计。
DEFAULT_CAPACITY = 1024
#: 每个流保留多少条历史，供**迟到**的订阅者回放（POST 拿到 id 与连上 SSE 之间有窗口）。
DEFAULT_HISTORY = 256
#: 历史流最多留几个。导入是偶发动作，留几十个够 GUI 切来切去，再多就是泄漏。
DEFAULT_MAX_STREAMS = 64


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    data: dict[str, Any]
    at: float
    #: 流到这儿就结束了（done / error / closed）。SSE 路由据此收尾 ——
    #: 它让"什么时候可以断开"由**事件本身**说了算，而不是让路由去猜作业状态。
    terminal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "at": round(self.at, 3),
                "terminal": self.terminal, "data": self.data}


@dataclass
class Subscription:
    """一个 SSE 连接对应的消费队列。`push` / `drain` 都加锁 —— 两侧是不同线程。"""

    stream_id: str
    capacity: int = DEFAULT_CAPACITY
    dropped: int = 0
    closed: bool = False
    _buffer: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: 这一批因为队列满而丢了多少（由 `drain` 汇报后清零）—— 见 `_unreported`。
    _unreported: int = 0

    def __post_init__(self) -> None:
        self._buffer = deque(maxlen=self.capacity)

    # -- 生产者侧（写线程调用，**绝不阻塞**）------------------------------
    def push(self, event: Event) -> bool:
        with self._lock:
            if self.closed:
                return False
            if len(self._buffer) == self.capacity:
                self.dropped += 1
                self._unreported += 1
            self._buffer.append(event)
        return True

    # -- 消费者侧（SSE 路由调用）------------------------------------------
    def drain(self, limit: int = 256) -> tuple[list[Event], int]:
        """取走当前积压（最多 `limit` 条）+ 自上次以来丢掉的条数。"""
        with self._lock:
            batch: list[Event] = []
            while self._buffer and len(batch) < limit:
                batch.append(self._buffer.popleft())
            dropped, self._unreported = self._unreported, 0
        return batch, dropped

    def close(self) -> None:
        with self._lock:
            self.closed = True

    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"subscriber_of": self.stream_id, "pending": len(self._buffer),
                    "dropped": self.dropped, "closed": self.closed,
                    "capacity": self.capacity}


class EventBroker:
    """按 `stream_id`（导入请求 id）分组的发布/订阅。线程安全。"""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY,
                 history: int = DEFAULT_HISTORY,
                 max_streams: int = DEFAULT_MAX_STREAMS) -> None:
        self.capacity = int(capacity)
        self.history = int(history)
        self.max_streams = int(max_streams)
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._history: dict[str, deque] = {}
        self._subs: dict[str, list[Subscription]] = {}
        self._terminal: dict[str, bool] = {}
        self._order: deque = deque()          # 流的创建顺序，供 prune 用
        self.published = 0

    # -- 生产 ---------------------------------------------------------------
    def open(self, stream_id: str) -> None:
        """宣告一个流开始（POST /imports 时调）。重复 open 不清空已有历史。"""
        with self._lock:
            self._open_locked(stream_id)

    def _open_locked(self, stream_id: str) -> None:
        """`open` 的**无锁**版本，供已经持锁的调用方（publish / subscribe）使用。

        为什么必须分开：`publish` 要在"流还不存在"时就地建流，于是它持着
        `self._lock` 调 `open` —— 而 `open` 又去拿同一把**非重入**锁。
        结果是首次发布那一条事件**永久挂住**（测试逮到的就是这个：先 publish
        后 subscribe 的路径能过，先 subscribe 后 publish 的路径必死）。
        靠把锁换成 RLock 能遮住症状，但那是把"谁持锁"这件事变模糊 ——
        显式的 `_locked` 后缀让"调用方必须已持锁"成为可读的约定。
        """
        if stream_id in self._history:
            return
        self._history[stream_id] = deque(maxlen=self.history)
        self._seq[stream_id] = 0
        self._subs[stream_id] = []
        self._terminal[stream_id] = False
        self._order.append(stream_id)
        self._prune_locked()

    def publish(self, stream_id: str, type_: str, data: dict[str, Any],
                *, terminal: bool = False) -> Event:
        """追加一条事件并扇出。**返回得快**是它的契约（写线程在等它）。"""
        now = time.monotonic()
        with self._lock:
            self._open_locked(stream_id)
            self._seq[stream_id] += 1
            event = Event(seq=self._seq[stream_id], type=type_, data=data, at=now,
                          terminal=bool(terminal))
            self._history[stream_id].append(event)
            subs = list(self._subs[stream_id])
            if terminal:
                self._terminal[stream_id] = True
            self.published += 1
        for sub in subs:                      # 锁外推送：订阅者的锁是另一把
            sub.push(event)
        return event

    def close(self, stream_id: str) -> None:
        self.publish(stream_id, "closed", {}, terminal=True)

    def close_all(self) -> None:
        """服务关停时把所有流收尾 —— 让正在读的客户端**知道**是服务停了，
        而不是自己猜"是不是卡住了"（断流歧义是 SSE 最常见的排查噩梦）。"""
        for stream_id in list(self.streams()):
            if not self.is_terminal(stream_id):
                self.publish(stream_id, "closed", {"reason": "server_shutdown"},
                             terminal=True)

    def is_terminal(self, stream_id: str) -> bool:
        with self._lock:
            return bool(self._terminal.get(stream_id))

    # -- 消费 ---------------------------------------------------------------
    def subscribe(self, stream_id: str) -> Subscription:
        """订阅并**回放历史**：POST 返回 id 与浏览器连上来之间有一个真实的窗口，
        不回放的话最前面几条（往往是 job_start）会永久丢失。"""
        sub = Subscription(stream_id=stream_id, capacity=self.capacity)
        with self._lock:
            self._open_locked(stream_id)          # 见 `_open_locked` 的说明
            replay = list(self._history[stream_id])
            self._subs[stream_id].append(sub)
        for event in replay:
            sub.push(event)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        with self._lock:
            subs = self._subs.get(sub.stream_id)
            if subs and sub in subs:
                subs.remove(sub)

    def replay(self, stream_id: str) -> list[Event]:
        with self._lock:
            return list(self._history.get(stream_id, ()))

    def forget(self, stream_id: str) -> None:
        """丢掉一个流（登记表回收导入记录时调）。**有订阅者就不丢** —— 那会让
        正在读的 SSE 连接突然看不到自己的流。"""
        with self._lock:
            if self._subs.get(stream_id):
                return
            if stream_id in self._order:
                self._order.remove(stream_id)
            self._history.pop(stream_id, None)
            self._subs.pop(stream_id, None)
            self._seq.pop(stream_id, None)
            self._terminal.pop(stream_id, None)

    # -- 观测 ---------------------------------------------------------------
    def streams(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._history))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"streams": len(self._history), "published": self.published,
                    "subscribers": {sid: len(subs) for sid, subs in self._subs.items()},
                    "history": self.history, "capacity": self.capacity}

    def _prune_locked(self) -> None:
        """只回收**已终结且无订阅者**的老流。调用方必须持锁。"""
        while len(self._history) > self.max_streams:
            for stream_id in list(self._order):
                if self._subs.get(stream_id) or not self._terminal.get(stream_id):
                    continue
                self._order.remove(stream_id)
                self._history.pop(stream_id, None)
                self._subs.pop(stream_id, None)
                self._seq.pop(stream_id, None)
                self._terminal.pop(stream_id, None)
                break
            else:                             # 没有可回收的 → 别死循环
                return
