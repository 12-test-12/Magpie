"""confidence —— 审阅点 17 的实现。

v3.1 的规则表长这样（被本模块废弃）:
    1 source          -> low
    2 source same     -> medium
    2 source same + authority -> high

那实际上是"多数票"，会在 SRC 场景下产生 `3 个弱来源 > 1 个高质量来源` 的错误结论。

v3.2:
    score = agreement_term × evidence_quality_term × authority_term × temporal_term

其中 agreement 在 (subject, semantic_field, value) 三元组上计算 —— 不是 subject 全局投票。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .store import InvariantError, is_within

QUALITY_WEIGHT = {"high": 3.0, "medium": 1.6, "low": 0.6}

# source catalog 的 authority 等级（source 表字段 authority_level）
AUTHORITY_TERM = {
    "verified": 1.5,
    "semiautomatic": 1.0,
    "manual": 2.0,
    "unverified": 0.6,
}
DEFAULT_AUTHORITY = 1.0

# 离散化阈值（形状不可改，参数可调）
T_NONE = 0.5
T_LOW = 1.5
T_MEDIUM = 3.5


def _authority(conn: sqlite3.Connection, source_id: int) -> float:
    """source.authority_level → term（v3.2-r3: 读真实的 sources 目录表）。"""
    try:
        row = conn.execute(
            "SELECT authority_level FROM sources WHERE id=?", (source_id,)
        ).fetchone()
    except sqlite3.OperationalError:   # 目录表缺失（旧库）→ 保守默认
        return DEFAULT_AUTHORITY
    if row is None or row[0] is None:
        return DEFAULT_AUTHORITY
    return AUTHORITY_TERM.get(str(row[0]), DEFAULT_AUTHORITY)


def _quality_term(conn: sqlite3.Connection, evidence_ids: list[int]) -> float:
    if not evidence_ids:
        return QUALITY_WEIGHT["low"] * 0.5
    marks = ",".join("?" for _ in evidence_ids)
    row = conn.execute(
        f"SELECT MAX(CASE quality_class WHEN 'high' THEN 3.0"
        f" WHEN 'medium' THEN 1.6 ELSE 0.6 END) FROM evidence WHERE id IN ({marks})",
        evidence_ids,
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else QUALITY_WEIGHT["low"]


def _temporal_term(rows: list[sqlite3.Row]) -> float:
    """§8.2 temporal_term。

    v3.2-r3: 补上"最近 24h 内刷新 → 1.15"。这要求 `now()` 是真实时钟
    （与 `created_at` 都是 ingest 时间），否则该规则永远不可达。
    """
    term = 1.0
    if any(str(r["observed_at_accuracy"]) == "unknown" for r in rows):
        term *= 0.8
    created = [str(r["created_at"]) for r in rows if r["created_at"]]
    if created and any(is_within(ts, hours=24.0) for ts in created):
        term *= 1.15
    return term


def _agreement_term(rows: list[sqlite3.Row], *, all_values_for_field: int,
                    distinct_sources: int) -> float:
    """agreement 只看"这个值"在全字段里是否唯一，以及来源是否独立。"""
    if all_values_for_field > 1:
        return 0.4                      # 存在未解决冲突
    if distinct_sources >= 2:
        return 1.25                     # ≥2 独立来源且一致
    if len(rows) > 1:
        return 0.7                      # 同源多贡献，非独立
    return 1.0


def discretize(score: float, *, has_conflict: bool, manual: bool) -> str:
    if manual:
        return "manual"
    if has_conflict:
        return "conflict"
    if score < T_NONE:
        return "none"
    if score < T_LOW:
        return "low"
    if score < T_MEDIUM:
        return "medium"
    return "high"


class FieldScoreBatch:
    """O(N) 批量 confidence —— `score_field` 的等价批量实现。

    为什么必须存在（实测动机）:
        `store.recompute` 原本逐 subject 调用 `score_field`，而后者每次都对
        `v_field_owner` / `v_field_conflicts` 这两个**全表聚合视图**求值。
        于是 N 行 → O(N) 次全表聚合 → 整体 O(N²)：
            N=1000 0.92s / N=2000 3.29s / N=4000 9.45s / N=8000 39.76s
        按此外推，"100 万行 ≤ 60s"（§9.2）实际需要约 78 分钟。
        cProfile 证实 `score_field` 独占 recompute 的 90% 时间。

    做法:
        **一次**扫描全部活跃 field 贡献，在内存里按
        (owner_entity, namespace, field[, value]) 分组建索引，
        之后每个 subject 的评分都是字典查找，不再碰数据库。

    等价性（必须与 `score_field` 逐值一致，由 INV-04 交错属性测试守护）:
        - 主体仍是 **entity**（`v_field_owner.owner_id`），不是 observation 行；
        - 冲突域仍是 (owner, namespace, field)，`distinct_values > 1`；
        - `quality_term = max(quality_weight over 该值的 evidence)`，
          evidence 缺失时退回 `QUALITY_WEIGHT['low'] * 0.5`；
        - `authority_term = max(authority_level over 该值的 source)`；
        - `agreement_term` 三分支与 `_agreement_term` 完全一致；
        - `temporal_term` 含"24h 内刷新 ×1.15"与 accuracy=unknown ×0.8。

    内存: 与活跃 field 贡献数同阶（每行 1 个元组）。100 万行约 100–200MB，
    超出 §2.1 的 120MB 红线 —— 因此对外只经由 `recompute_subset()` 使用，
    由调用方按批控制驻留规模。

    `owners` 是**扫描窗口**，不是过滤器（v3.2-r4 修正）: 窗口外的 owner 从未被
    索引，`score()` 对它们只能返回 "none" —— 那会和"真的没有活贡献"混淆。
    因此窗口外的查询一律 `InvariantError` 报错，而不是给一个看起来合法的答案。
    """

    __slots__ = ("_by_value", "_field_values", "_quality", "_authority",
                 "_manual", "_unknown", "_recent", "_owners")

    def __init__(self, conn: sqlite3.Connection,
                 owners: "set[int] | None" = None) -> None:
        """构建批量评分索引。

        owners=None  -> 全库扫描（全量 recompute 用）
        owners={...} -> **只**扫描这些 owner entity 的贡献。
                        增量路径必须走这条：delta 只改了 K 个 subject，
                        对应的 owner 至多 2K 个。否则每次 delta 都要付 O(N)
                        的全库扫描代价，"增量"就名存实亡。
        """
        if owners is not None:
            self._owners = frozenset(int(o) for o in owners)
            if not owners:
                self._by_value = {}
                self._field_values = {}
                self._quality = {}
                self._authority = {}
                self._manual = set()
                self._unknown = set()
                self._recent = set()
                return
            id_list = sorted(int(o) for o in owners)
        else:
            self._owners = None                # 全库窗口：任何 owner 都在窗口内
            id_list = []

        from .store import bind_batch_size, is_within

        base_sql = (
            "SELECT c.owner_entity_id       AS owner_id,"
            "       c.field_namespace        AS ns,"
            "       c.semantic_field         AS field,"
            "       c.field_value            AS val,"
            "       c.source_id              AS source_id,"
            "       c.evidence_id            AS evidence_id,"
            "       c.authorship             AS authorship,"
            "       e.quality_class          AS quality_class,"
            "       c.observed_at_accuracy   AS accuracy,"
            "       c.created_at             AS created_at"
            "  FROM v_active_contributions c"
            "  LEFT JOIN evidence e ON e.id = c.evidence_id"
            " WHERE c.contribution_kind = 'field'"
            "   AND c.owner_entity_id IS NOT NULL"
            "   AND c.field_value IS NOT NULL"
        )

        # 窗口内的 owner 必须**分块**绑定：SQLite 的变量上限（32766；旧版 999）
        # 与 owner 数无关，而一次 delta 可以覆盖上万个 owner（10 万观测的导入就是
        # 这样）。旧实现一次性绑定全部 owner，超过上限即 `too many SQL variables`。
        # 分块大小由连接自己决定（`store.bind_batch_size`），累加器共用，
        # 逐列语义与单次扫描完全一致。
        if self._owners is None:
            row_batches: list[list[sqlite3.Row]] = [conn.execute(base_sql).fetchall()]
        else:
            step = bind_batch_size(conn)
            row_batches = []
            for start in range(0, len(id_list), step):
                chunk = id_list[start:start + step]
                marks = ",".join("?" for _ in chunk)
                row_batches.append(
                    conn.execute(base_sql + f"   AND c.owner_entity_id IN ({marks})",
                                 chunk).fetchall())

        by_value: dict[tuple[int, str, str, str], list[int]] = {}
        field_values: dict[tuple[int, str, str], set[str]] = {}
        quality: dict[tuple[int, str, str, str], float] = {}
        authority: dict[tuple[int, str, str, str], float] = {}
        manual: set[tuple[int, str, str, str]] = set()
        # 时间项只需要两个稀疏集合，不保留整行（省内存）
        unknown_accuracy: set[tuple[int, str, str, str]] = set()
        recent: set[tuple[int, str, str, str]] = set()
        # source_id → authority term。**必须记忆化**：`_authority()` 是一次查询，
        # 逐行调用等于把"一次扫描"重新变回 N 次往返 —— 那正是本类要消灭的东西。
        # 实测（60 行活跃 field 贡献）: 逐行调用 = 61 条 SQL，记忆化后 = 1 条。
        authority_by_source: dict[int, float] = {}
        for r in (row for batch_rows in row_batches for row in batch_rows):
            key = (int(r["owner_id"]), str(r["ns"]), str(r["field"]), str(r["val"]))
            by_value.setdefault(key, []).append(int(r["source_id"]))
            field_values.setdefault(key[:3], set()).add(str(r["val"]))
            if str(r["authorship"]) == "manual":
                manual.add(key)
            qc = r["quality_class"]
            q = QUALITY_WEIGHT.get(str(qc), None) if qc is not None else None
            if q is None:
                q = QUALITY_WEIGHT["low"] * 0.5      # 与 _quality_term 的缺失分支一致
            if q > quality.get(key, -1.0):
                quality[key] = q
            source_id = int(r["source_id"])
            a = authority_by_source.get(source_id)
            if a is None:
                a = _authority(conn, source_id)
                authority_by_source[source_id] = a
            if a > authority.get(key, -1.0):
                authority[key] = a
            if str(r["accuracy"] or "") == "unknown":
                unknown_accuracy.add(key)
            if r["created_at"] and is_within(str(r["created_at"]), hours=24.0):
                recent.add(key)

        self._by_value = by_value
        self._field_values = field_values
        self._quality = quality
        self._authority = authority
        self._manual = manual
        self._unknown = unknown_accuracy
        self._recent = recent

    def score(self, entity_id: int, field_namespace: str, semantic_field: str,
              value: str) -> tuple[str, bool]:
        """与 `score_field(conn, 'entity', entity_id, ns, field, value)` 等价。

        前置条件（v3.2-r4 起强制）: 若构造时传了 `owners`，`entity_id` 必须**在
        窗口内**。窗口外的实体没有被扫描过，它的空索引与"确实没有活贡献"在返回值
        上无法区分 —— 静默返回 "none" 就是让缓存撒谎。这里直接报错。
        """
        owner = int(entity_id)
        if self._owners is not None and owner not in self._owners:
            raise InvariantError(
                f"FieldScoreBatch: owner entity {owner} 不在扫描窗口内"
                f"（窗口 {len(self._owners)} 个 owner）—— 无法给出与 score_field 等价的答案")
        key = (owner, str(field_namespace), str(semantic_field), str(value))
        sources = self._by_value.get(key)
        if not sources:
            return "none", False

        values = self._field_values.get(key[:3]) or ()
        distinct_values = len(values)
        has_conflict = distinct_values > 1

        # manual 判定需要逐行 authorship —— 用"该值是否含 manual 来源"近似会错，
        # 因此单独保留 manual 标记。
        if key in self._manual:
            return "manual", has_conflict

        quality = self._quality[key]
        authority = self._authority[key]
        distinct_sources = len(set(sources))

        if distinct_values > 1:
            agreement = 0.4
        elif distinct_sources >= 2:
            agreement = 1.25
        elif len(sources) > 1:
            agreement = 0.7
        else:
            agreement = 1.0

        temporal = 1.0
        if key in self._unknown:
            temporal *= 0.8
        if key in self._recent:
            temporal *= 1.15

        score = agreement * quality * authority * temporal
        return discretize(score, has_conflict=has_conflict, manual=False), has_conflict


def score_field(conn: sqlite3.Connection, subject_kind: str, subject_id: int,
                field_namespace: str, semantic_field: str,
                value: str) -> tuple[str, bool]:
    """返回 (confidence, has_conflict)。

    v3.2-r2: 主体是 **entity**（`v_field_owner.owner_id`），冲突域是
    (主体, field_namespace, semantic_field) —— 否则以 observation 行（身份里含 value）
    为主体时永远看不到"同字段多值"的冲突，§8.2 的 conflict 档与 P-17b 都不可达。
    """
    rows = list(conn.execute(
        "SELECT * FROM v_field_owner"
        " WHERE owner_id=? AND contribution_kind='field'"
        "   AND field_namespace=? AND semantic_field=? AND field_value=?",
        (subject_id, field_namespace, semantic_field, value),
    ))
    if not rows:
        return "none", False

    conflict_row = conn.execute(
        "SELECT distinct_values FROM v_field_conflicts"
        " WHERE subject_kind='entity' AND subject_id=? AND field_namespace=?"
        "   AND semantic_field=?",
        (subject_id, field_namespace, semantic_field),
    ).fetchone()
    has_conflict = conflict_row is not None and int(conflict_row["distinct_values"]) > 1
    all_values_for_field = int(conflict_row["distinct_values"]) if conflict_row else 1

    manual = any(str(r["authorship"]) == "manual" for r in rows)
    if manual:
        return "manual", has_conflict

    evidence_ids = [int(r["evidence_id"]) for r in rows]
    quality = _quality_term(conn, evidence_ids)
    authority = max(_authority(conn, int(r["source_id"])) for r in rows)
    distinct_sources = len({int(r["source_id"]) for r in rows})
    agreement = _agreement_term(rows, all_values_for_field=all_values_for_field,
                               distinct_sources=distinct_sources)
    temporal = _temporal_term(rows)

    score = agreement * quality * authority * temporal
    return discretize(score, has_conflict=has_conflict, manual=False), has_conflict


def score_relation(conn: sqlite3.Connection, relation_id: int) -> str:
    rows = list(conn.execute(
        "SELECT * FROM v_active_contributions"
        " WHERE subject_kind='relation' AND subject_id=?", (relation_id,),
    ))
    if not rows:
        return "none"
    if any(str(r["authorship"]) == "manual" for r in rows):
        return "manual"
    evidence_ids = [int(r["evidence_id"]) for r in rows]
    quality = _quality_term(conn, evidence_ids)
    authority = max(_authority(conn, int(r["source_id"])) for r in rows)
    distinct_sources = len({int(r["source_id"]) for r in rows})
    agreement = 1.25 if distinct_sources >= 2 else (0.7 if len(rows) > 1 else 1.0)
    score = agreement * quality * authority * _temporal_term(rows)
    return discretize(score, has_conflict=False, manual=False)
