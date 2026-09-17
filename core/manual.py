"""ManualWriter —— INV-12 的真正实现（v3.2-r3）。

设计 §7.1 的规定:
    所有 manual 写操作（manual.set / manual.clear / tag / layout / inbox resolve）
    必须经**唯一入口** `ManualWriter`，其内部是**单一串行队列 + 乐观版本号**。

为什么"乐观锁"不够:
    乐观锁只能**发现**冲突（`UPDATE ... WHERE version=:expected` 影响行数 = 0），
    不能防止丢失。两个 writer 都读到 version=1，A 写入 ["display_name"]，
    B 写入 ["review_status"] —— B 的提交失败后，如果调用方不重读重试，
    B 的 key 就静默丢了。文档要的是"最终 keys == 所有写入的并集，且无丢失"。

本模块因此提供:
    - 进程内单写队列（`threading.RLock`，串行化所有 manual 写入）
    - 读-改-写-提交的**重试循环**（版本冲突 → 重读 → 合并 → 再提交）
    - 语义是**并集合并**（`add` / `remove` 增量），而不是整串覆盖写

线程与连接: 所有写经同一个 writer 串行执行，因此连接可用
`store.connect(path, check_same_thread=False)` 在多线程间共享。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable, Sequence

from .store import InvariantError, canonical_json, log_activity, now

MAX_RETRIES = 100


def _load_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        raise InvariantError(f"manual_fields.keys_json is not JSON: {raw!r}")
    if not isinstance(data, list):
        raise InvariantError("manual_fields.keys_json must be a JSON array")
    return [str(x) for x in data]


class ManualWriter:
    """唯一 manual 写入口（单写队列 + 乐观版本 + 并集合并）。"""

    def __init__(self, conn: sqlite3.Connection, *, actor: str = "cli",
                 max_retries: int = MAX_RETRIES):
        self.conn = conn
        self.actor = actor
        self.max_retries = max_retries
        self._lock = threading.RLock()
        self.stats = {"writes": 0, "retries": 0, "merges": 0}

    # -- 内部: 串行化 + 版本冲突重试 ---------------------------------------
    def _mutate_keys(self, *, project_id: int, subject_kind: str, subject_id: int,
                     add: Sequence[str], remove: Sequence[str]) -> dict[str, Any]:
        add_set, remove_set = {str(k) for k in add}, {str(k) for k in remove}
        if not add_set and not remove_set:
            raise InvariantError("manual write with no keys")
        with self._lock:
            for attempt in range(self.max_retries):
                row = self.conn.execute(
                    "SELECT keys_json, version FROM manual_fields"
                    " WHERE project_id=? AND subject_kind=? AND subject_id=?",
                    (project_id, subject_kind, subject_id),
                ).fetchone()
                if row is None:
                    merged = sorted(add_set - remove_set)
                    if not merged:
                        # 纯撤销（且撤销对象本来就不存在）→ **不建行**。
                        # `manual_fields` 的**存在性本身**就是 manual ownership 的
                        # 判据（`store.manual_owned`）。建一条 keys_json='[]' 的空行
                        # 等于凭空造出人工作业锚点：该 subject 从此不会被 GC 回收，
                        # 也不会被 coverage 判定为 inactive —— "解除保护"反而变成了
                        # "永久保护"。没有 key 就没有行。
                        return {"keys": [], "version": 0, "attempts": attempt + 1,
                                "created": False}
                    try:
                        self.conn.execute(
                            "INSERT INTO manual_fields(project_id, subject_kind, subject_id,"
                            " keys_json, version, updated_at, updated_by)"
                            " VALUES (?,?,?,?,1,?,?)",
                            (project_id, subject_kind, subject_id, canonical_json(merged),
                             now(), self.actor),
                        )
                    except sqlite3.IntegrityError:
                        continue                      # 并发插入 → 重读重试
                    self.stats["writes"] += 1
                    self._audit(project_id, subject_kind, subject_id, merged,
                                added=sorted(add_set), removed=sorted(remove_set))
                    return {"keys": merged, "version": 1, "attempts": attempt + 1,
                            "created": True}
                current = set(_load_keys(row["keys_json"]))
                merged = sorted((current | add_set) - remove_set)
                if merged == sorted(current):
                    # 幂等：无变化也视作成功（不丢写），但仍要保证 key 都在
                    return {"keys": merged, "version": int(row["version"]),
                            "attempts": attempt + 1, "created": False}
                cur = self.conn.execute(
                    "UPDATE manual_fields SET keys_json=?, version=version+1, updated_at=?,"
                    " updated_by=? WHERE project_id=? AND subject_kind=? AND subject_id=?"
                    " AND version=?",
                    (canonical_json(merged), now(), self.actor, project_id, subject_kind,
                     subject_id, int(row["version"])),
                )
                if cur.rowcount == 1:
                    self.stats["writes"] += 1
                    self._audit(project_id, subject_kind, subject_id, merged,
                                added=sorted(add_set), removed=sorted(remove_set))
                    return {"keys": merged, "version": int(row["version"]) + 1,
                            "attempts": attempt + 1, "created": False}
                self.stats["retries"] += 1                # 版本冲突 → 重读并合并
            raise InvariantError(
                f"ManualWriter: version conflict not resolved after {self.max_retries} retries")

    def _audit(self, project_id: int, subject_kind: str, subject_id: int,
               keys: Iterable[str], *, added: Sequence[str], removed: Sequence[str]) -> None:
        log_activity(self.conn, actor=self.actor,
                     action="manual.set" if added else "manual.clear",
                     target_kind=subject_kind, target_id=subject_id,
                     detail={"project_id": project_id, "keys": sorted(keys),
                             "added": list(added), "removed": list(removed)})

    # -- 公开 API ----------------------------------------------------------
    def set_keys(self, *, project_id: int, subject_kind: str, subject_id: int,
                 add: Sequence[str] = (), remove: Sequence[str] = ()) -> dict[str, Any]:
        """把 `add` 里的字段名并入该 subject 的人工保护集合（并集，不覆盖）。"""
        return self._mutate_keys(project_id=project_id, subject_kind=subject_kind,
                                 subject_id=subject_id, add=list(add), remove=list(remove))

    def clear_keys(self, *, project_id: int, subject_kind: str, subject_id: int,
                   keys: Sequence[str]) -> dict[str, Any]:
        return self._mutate_keys(project_id=project_id, subject_kind=subject_kind,
                                 subject_id=subject_id, add=[], remove=list(keys))

    def read_keys(self, *, project_id: int, subject_kind: str, subject_id: int) -> list[str]:
        row = self.conn.execute(
            "SELECT keys_json FROM manual_fields WHERE project_id=? AND subject_kind=?"
            " AND subject_id=?", (project_id, subject_kind, subject_id)).fetchone()
        return [] if row is None else _load_keys(row["keys_json"])

    # -- tag（add/remove 都写 tag_events + activity_log，§6.1）-------------
    def set_tag(self, *, project_id: int, entity_id: int, tag: str,
                reason: str | None = None, job_id: int | None = None) -> None:
        with self._lock:
            conn = self.conn
            exists = conn.execute(
                "SELECT deleted_at FROM entity_tags WHERE project_id=? AND entity_id=?"
                " AND tag=?", (project_id, entity_id, tag)).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO entity_tags(project_id, entity_id, tag, set_by, set_at)"
                    " VALUES (?,?,?,?,?)", (project_id, entity_id, tag, self.actor, now()))
            elif exists["deleted_at"] is not None:
                conn.execute(
                    "UPDATE entity_tags SET deleted_at=NULL, deleted_by=NULL,"
                    " delete_reason=NULL, set_by=?, set_at=?"
                    " WHERE project_id=? AND entity_id=? AND tag=?",
                    (self.actor, now(), project_id, entity_id, tag))
            else:
                return                                    # 幂等
            conn.execute(
                "INSERT INTO tag_events(project_id, entity_id, tag, action, actor, at,"
                " reason, job_id) VALUES (?,?,?,'add',?,?,?,?)",
                (project_id, entity_id, tag, self.actor, now(), reason, job_id))
            log_activity(conn, actor=self.actor, action="tag.add", target_kind="entity",
                         target_id=entity_id, job_id=job_id, detail={"tag": tag})
            self.stats["writes"] += 1

    def remove_tag(self, *, project_id: int, entity_id: int, tag: str,
                   reason: str | None = None, job_id: int | None = None) -> None:
        with self._lock:
            conn = self.conn
            exists = conn.execute(
                "SELECT deleted_at FROM entity_tags WHERE project_id=? AND entity_id=?"
                " AND tag=?", (project_id, entity_id, tag)).fetchone()
            if exists is None or exists["deleted_at"] is not None:
                return                                    # 幂等
            conn.execute(
                "UPDATE entity_tags SET deleted_at=?, deleted_by=?, delete_reason=?"
                " WHERE project_id=? AND entity_id=? AND tag=?",
                (now(), self.actor, reason, project_id, entity_id, tag))
            conn.execute(
                "INSERT INTO tag_events(project_id, entity_id, tag, action, actor, at,"
                " reason, job_id) VALUES (?,?,?,'remove',?,?,?,?)",
                (project_id, entity_id, tag, self.actor, now(), reason, job_id))
            log_activity(conn, actor=self.actor, action="tag.remove", target_kind="entity",
                         target_id=entity_id, job_id=job_id, detail={"tag": tag})
            self.stats["writes"] += 1

    # -- layout ------------------------------------------------------------
    def set_layout(self, *, project_id: int, entity_id: int, x: float, y: float,
                   collapsed: bool = False) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO entity_layout(project_id, entity_id, x, y, collapsed,"
                " updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(project_id, entity_id) DO UPDATE SET"
                " x=excluded.x, y=excluded.y, collapsed=excluded.collapsed,"
                " updated_at=excluded.updated_at",
                (project_id, entity_id, float(x), float(y), 1 if collapsed else 0, now()))
            log_activity(self.conn, actor=self.actor, action="manual.set",
                         target_kind="entity_layout", target_id=entity_id,
                         detail={"x": x, "y": y, "collapsed": bool(collapsed)})
            self.stats["writes"] += 1

    # -- inbox resolve -----------------------------------------------------
    def resolve_inbox(self, *, item_id: int, resolution: dict[str, Any] | None = None,
                      state: str = "resolved") -> None:
        if state not in ("resolved", "dismissed"):
            raise InvariantError(f"bad inbox state {state!r}")
        with self._lock:
            self.conn.execute(
                "UPDATE inbox_items SET state=?, resolved_at=?, resolved_by=?"
                " WHERE id=? AND state='open'",
                (state, now(), self.actor, item_id))
            log_activity(self.conn, actor=self.actor, action="inbox.close",
                         target_kind="inbox_item", target_id=item_id,
                         detail={"state": state, "resolution": resolution})
            self.stats["writes"] += 1
