"""SSE：不断流 + 慢消费者不阻塞导入（2.7 交付标准表里最难的一条）。

这条标准难在它**没法靠"跑一遍看着没问题"验**：正常路径下一切正常是必然的 ——
只有消费者卡住时才分得出来"丢帧"与"卡死生产者"的区别。所以分两层测：

  1. **总线层**（`EventBroker` / `Subscription`）：拿一个**从不 drain** 的订阅者
     当反例，量"发布 N 条事件要多久"。阻塞式背压在这里会**直接挂住**，而不是
     慢一点 —— 这就是那条断言真正在守的东西。
  2. **端到端**（真 HTTP + TestClient 的流式响应）：一次真导入的事件要能按
     100ms 批次推出来，并且以 `done` / `closed` 收尾（不断流，也不永远挂着）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from magpie.api.app import create_app
from magpie.api.events import EventBroker

ROOT = Path(__file__).resolve().parents[3]
SUBFINDER = ROOT / "bench" / "e2e" / "recon" / "subfinder.txt"


# ---------------------------------------------------------------------------
# 1. 总线层：背压策略
# ---------------------------------------------------------------------------


def test_slow_consumer_never_blocks_publisher():
    """一个永不消费的订阅者，不能拖慢（更不能拖死）发布者。

    20_000 条事件 × 队列容量 8：阻塞式背压在第一条就会挂住。这里量的不是"快"，
    而是"**有界**" —— 上限取 2s（真跑到这个数早就说明设计错了）。
    """
    broker = EventBroker(capacity=8, history=4)
    stuck = broker.subscribe("imp-1")              # 订阅了，但一条都不取

    started = time.perf_counter()
    for i in range(20_000):
        broker.publish("imp-1", "progress", {"records": i})
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"发布被慢消费者拖住了：20k 条用了 {elapsed:.2f}s"
    assert stuck.pending() == 8, "队列没有被限制在容量内"
    assert stuck.dropped == 20_000 - 8, (
        f"丢帧计数不对：{stuck.dropped} != {20_000 - 8} —— 丢帧必须**可数**，"
        f"否则消费者无法判断自己看到的进度是不是连续的")


def test_dropped_is_reported_to_the_consumer():
    """丢帧不是静默的：下一次 drain 会如实报出丢了多少条。"""
    broker = EventBroker(capacity=4, history=2)
    sub = broker.subscribe("imp-1")
    for i in range(10):
        broker.publish("imp-1", "progress", {"records": i})
    batch, dropped = sub.drain()
    assert len(batch) == 4
    assert dropped == 6
    assert [e.data["records"] for e in batch] == [6, 7, 8, 9], "丢的必须是最旧的"
    # 报过一次就清零 —— 否则消费者会以为每批都在丢。
    _, again = sub.drain()
    assert again == 0


def test_late_subscriber_replays_history():
    """POST 拿到 import_id 与浏览器连上 SSE 之间有一个真实窗口，历史必须回放。"""
    broker = EventBroker(capacity=16, history=16)
    broker.open("imp-9")
    for i in range(5):
        broker.publish("imp-9", "progress", {"records": i})
    late = broker.subscribe("imp-9")               # 事件早就发完了才订阅
    batch, _ = late.drain()
    assert [e.data["records"] for e in batch] == [0, 1, 2, 3, 4]


def test_terminal_event_is_flagged():
    """流什么时候算结束，由**事件本身**说了算（不是让路由去猜作业状态）。"""
    broker = EventBroker()
    broker.subscribe("imp-1")
    broker.publish("imp-1", "progress", {})
    event = broker.publish("imp-1", "done", {"state": "done"}, terminal=True)
    assert event.terminal is True
    assert broker.is_terminal("imp-1") is True


def test_history_and_streams_are_bounded():
    """流不回收 = 内存随导入次数无限增长。这条断言守的是回收策略本身。"""
    broker = EventBroker(capacity=4, history=4, max_streams=3)
    for i in range(10):
        broker.open(f"imp-{i}")
        broker.publish(f"imp-{i}", "done", {}, terminal=True)
    assert len(list(broker.streams())) <= 3, "已终结的流没有被回收"
    assert "imp-9" in set(broker.streams()), "回收的必须是**最老**的流"


# ---------------------------------------------------------------------------
# 2. 端到端：真导入的进度流
# ---------------------------------------------------------------------------


def _frame_events(client, url: str, *, timeout_s: float = 60.0) -> list[dict]:
    """把 SSE 帧拆成事件列表，遇到 terminal 就停（流本来就该在那儿收尾）。"""
    events: list[dict] = []
    with client.stream("GET", url, timeout=timeout_s) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            for event in payload.get("events", []):
                events.append(event)
            if any(e.get("terminal") for e in payload.get("events", [])):
                break
    return events


def test_import_events_stream_end_to_end(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers,
                    json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        submitted = client.post("/api/v1/imports", headers=headers,
                                json={"paths": [str(SUBFINDER)], "project": "demo"})
        assert submitted.status_code == 202
        import_id = submitted.json()["data"]["import_id"]

        events = _frame_events(client, f"/api/v1/imports/{import_id}/events?token=t")

    types = [e["type"] for e in events]
    assert "queued" in types, f"没有提交事件：{types}"
    assert "job_done" in types, f"没有作业收尾事件：{types}"
    assert "done" in types, f"没有导入收尾事件：{types}"
    assert types[-1] == "done", f"收尾事件必须是最后一条（不断流也不拖尾）：{types}"
    assert events[-1]["data"]["job_ids"], "done 事件必须带上真实 job_id"
    # 每条事件都得有 seq：消费者据此判断"我看到的进度连不连续"。
    assert all(isinstance(e["seq"], int) and e["seq"] > 0 for e in events)


def test_progress_events_carry_counts_and_bad_lines(tmp_path):
    """**进度**那条事件真的会出现，且带着计数与坏行。

    为什么单列一条：交付标准写的是"每 100ms 批量推送**进度**/阶段/日志/坏行"，
    而 `bench/e2e` 的夹具只有几百行 —— 小于 `ingest` 的批大小（2000），
    于是**永远不会有 progress 事件**，前一条端到端用例因此只覆盖了阶段事件。
    造一个 5000 行（≥ 3 个 checkpoint）的输入，才真的把这条标准量到。
    """
    source = tmp_path / "big-domains.txt"
    lines = [f"h{i}.perf.example.com" for i in range(5000)]
    lines[100] = "这不是一个域名\t带有坏字符"          # 至少一条坏行
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers,
                    json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        import_id = client.post("/api/v1/imports", headers=headers,
                                json={"paths": [str(source)], "project": "demo"}
                                ).json()["data"]["import_id"]
        events = _frame_events(
            client, f"/api/v1/imports/{import_id}/events?token=t", timeout_s=120)

    progress = [e for e in events if e["type"] == "progress"]
    assert progress, f"没有进度事件（批大小 2000，5000 行应当至少 2 条）：" \
                     f"{[e['type'] for e in events]}"
    last = progress[-1]["data"]
    assert last["records"] >= 2000
    assert set(last["counts"]) >= {"added", "updated", "skipped", "bad_lines",
                                   "unknown_fields", "records"}
    assert last["line"] > 0, "进度里必须带当前行号（否则 GUI 画不出进度）"
    assert last["counts"]["records"] == last["records"], \
        "进度里的记录数必须与计数同源 —— 两个数字对不上就是两套计数"
    # 坏行随进度一起推（交付标准里的"坏行"那半句）
    assert last["bad_lines_total"] >= 1, "坏行没有被汇报"
    assert isinstance(last["bad_lines"], list)


def test_sse_requires_token(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers, json={"name": "demo"})
        import_id = client.post("/api/v1/imports", headers=headers,
                                json={"paths": [str(SUBFINDER)],
                                      "project": "demo"}).json()["data"]["import_id"]
        # 头里不带 token、查询串里也不带 → 401（EventSource 走的是 ?token=）
        assert client.get(
            f"/api/v1/imports/{import_id}/events").status_code == 401
        assert client.get(
            f"/api/v1/imports/{import_id}/events?token=wrong").status_code == 401


def test_stream_survives_a_slow_reader(tmp_path):
    """慢读者：连上了但读得很慢 —— 流不该断，导入也不该等它。

    这条是**端到端**版本的反例：先订阅、晾着不读，等导入跑完再读，
    仍然必须拿到收尾事件（历史回放 + 丢帧计数一起保证这件事）。
    """
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token="t")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        client.post("/api/v1/projects", headers=headers,
                    json={"name": "demo", "scope": {"include": ["*.example.com"]}})
        import_id = client.post("/api/v1/imports", headers=headers,
                                json={"paths": [str(SUBFINDER)],
                                      "project": "demo"}).json()["data"]["import_id"]
        # 一个从不 drain 的订阅者（模拟连上就卡的浏览器标签页）
        stuck = app.state.ctx.broker.subscribe(import_id)
        record = app.state.ctx.imports.get(import_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = app.state.ctx.imports.get(import_id)
            if record is not None and record.terminal:
                break
            time.sleep(0.05)
        # 断言的是**导入跑完了**这件事本身：一个不读的消费者若能让导入停下，
        # 这里会超时——那就是"旁观者变成参与者"，而这是设计上明确不要的。
        assert record is not None and record.terminal, "慢读者把导入拖住了"

        events = _frame_events(client, f"/api/v1/imports/{import_id}/events?token=t")
        assert events[-1]["type"] == "done", "慢读者连上后仍然必须收到收尾事件"
        assert stuck.pending() <= stuck.capacity


def test_no_busy_wait_loop_in_the_broker():
    """发布是 O(订阅者)，不是 O(队列长度) —— 抖一下这个数字。

    2000 个订阅者的发布不该退化成扫描所有历史。这里只做一个**量级**断言：
    发布 100 条（每条广播给 2000 个订阅者 = 20 万次 push）在 2s 内完成。
    """
    broker = EventBroker(capacity=4, history=4)
    for _ in range(2000):
        broker.subscribe("imp-1")
    started = time.perf_counter()
    for i in range(100):
        broker.publish("imp-1", "progress", {"records": i})
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"扇出太慢：{elapsed:.2f}s（可能有 O(n²) 的写法）"


def test_publish_is_thread_safe():
    """写线程发布 + 路由线程 drain，同时发生。丢掉任何一条都是 bug。"""
    broker = EventBroker(capacity=100_000, history=100_000)
    sub = broker.subscribe("imp-1")
    total = 5_000

    def produce():
        for i in range(total):
            broker.publish("imp-1", "progress", {"records": i})

    thread = threading.Thread(target=produce)
    thread.start()
    seen = 0
    while thread.is_alive() or sub.pending():
        batch, _ = sub.drain()
        seen += len(batch)
    thread.join()
    assert seen == total, f"并发下丢了事件：{seen} != {total}"
