"""schema 版本化与迁移 runner（v3.2-r4 · 债 #7）。

**没有迁移机制时会出什么事**：`schema/001_init.sql` 是唯一的，第一次要改 schema
（加一个 kind 的字段、加一张表）时，没有任何东西能回答"这个库现在是第几版、
该不该升级、升到一半失败怎么办"。越晚引入越贵，所以现在引入。

设计（尽量小，够用即可）:

* `schema_migrations(version, name, checksum, applied_at)` —— 已应用版本的事实记录。
* 迁移 = 一个有编号的 `.sql` 文件，**只增不改**。已应用迁移的文件内容被改动
  → 校验和不匹配 → 直接报错（这是最容易犯也最难发现的错误：改了 001 却以为
  新库和老库结构一样）。
* 基线采纳: 由旧版 `init_db`（直接 `executescript`）建出来的库没有 registry，
  但有 `contributions` 表 —— 视为已应用 001，补记一行，不重跑 SQL。
* 升级前自动快照（`store.online_backup`）—— 迁移失败时人还有退路。

**不做什么**（避免过度设计）: 不做 down-migration（自用工具，回退靠快照）、
不做跨版本跳跃（顺序执行，逐个 apply）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# schema/ 在仓库根，不在包内（随 2.1 包布局迁移加深了一层）。
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


# 迁移清单：**只追加，不修改已有条目**。
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "init", SCHEMA_DIR / "001_init.sql"),
    # 收窄 undo_incomplete: redo 的 blocked（部分成功）状态是合法的。
    # 详情与代价见 schema/002_redo_blocked_state.sql 的文件头。
    Migration(2, "redo_blocked_state", SCHEMA_DIR / "002_redo_blocked_state.sql"),
    # 资源类型登记表 + 关系受控词表 + definition_hash 的 DB 级兜底（2.4 同期必交项 A/B）。
    # 词表落成"表 + 拒绝触发器"而不是字面量 CHECK 的理由见该文件头。
    Migration(3, "kind_registry", SCHEMA_DIR / "003_kind_registry.sql"),
    # 登记 `cidr`（第一个真实产出者是 3.1 的 nmap `<target>`），并把 `nmap@1`
    # 追加进 ip / service / product 的 produced_by。**新增迁移，不改 003**
    # （红线 R4：已应用的迁移被改动 → 校验和不匹配 → 直接报错）。
    Migration(4, "kind_cidr", SCHEMA_DIR / "004_kind_cidr.sql"),
    # 登记 `vuln`（第一个真实产出者是 3.2 的 nuclei）与关系动词 `found_on`，
    # 并把 `nuclei@1` 追加进 domain / ip / service 的 produced_by。
    # 同样**新增迁移、不改 003 / 004**（红线 R4）。
    Migration(5, "kind_vuln", SCHEMA_DIR / "005_kind_vuln.sql"),
    # 登记 `endpoint`（第一个真实产出者是 3.3 的四个"路径列表"来源：
    # dirsearch / katana / gau / ffuf）与关系动词 `served_by`，并把四个指纹追加进
    # domain / ip / service 的 produced_by。同样**新增迁移、不改 003–005**（R4）。
    Migration(6, "kind_endpoint", SCHEMA_DIR / "006_kind_endpoint.sql"),
    # 登记 `apischema`（第一个真实产出者是 3.4 的四个"接口描述"来源：
    # openapi / postman / har / curl）与关系动词 `declared_in`，并把四个指纹追加进
    # domain / ip / service / endpoint 的 produced_by。
    # 同样**新增迁移、不改 003–006**（红线 R4）。
    Migration(7, "kind_apischema", SCHEMA_DIR / "007_kind_apischema.sql"),
    # 管线**定义**与管线**运行**（3.7b）: 两张新表 + `jobs.pipeline_run_id`
    # + 跨项目触发器。它与 004–007 不同 —— 那四个是往**词表**里加一行，这一个
    # 是给一个新的持久化产物落表。同样**新增迁移、不改 003–007**（R4）。
    Migration(8, "pipeline_definitions", SCHEMA_DIR / "008_pipeline_definitions.sql"),
    # 工作层写入的**撤销日志**（3.8）。它与 004–007 不同（那些是词表加行），
    # 也与 008 不同（那是新的持久化产物）—— 这一张是给 `ops.undo` / `ops.redo`
    # 补上"工作层那一半"的账本：`entities.lifecycle` 不是事实层，没有贡献可撤，
    # 撤销它需要的恰恰是"改之前的值"。同样**新增迁移、不改 001–008**（R4）。
    Migration(9, "work_journal", SCHEMA_DIR / "009_work_journal.sql"),
    # 多值字段的**声明表** + 重建 `v_field_conflicts`（3.3 / 3.4 报出的缺陷 A）。
    # 它与 004–007 不同（那些是往词表里加行），与 008 / 009 也不同（那些是新的
    # 持久化产物）—— 这一次换的是 001 建的一条**视图**。视图是纯派生、不承载
    # 数据的，所以"不许动 001"（R4）并不禁止改判据: DROP VIEW + CREATE VIEW 即可，
    # 002 换 `v_integrity_violations` 就是先例。详见该文件头。
    Migration(10, "multivalue_fields", SCHEMA_DIR / "010_multivalue_fields.sql"),
    # 登记 `flow`（第一个真实产出者是 4.1 的 har / tshark）与关系动词 `involves`，
    # 外加**两件结构性的事**：`raw_files.custody`（谁持有原文 —— 正交于
    # `storage_state` 的一条新列，v3.1 的 `storage='external'` 在 v3.2 里没有落点）
    # 与 `inbox_items.kind` 的第八个取值 `unregistered_asset`（重建整张表）。
    # 它与 004–007 不同（那些是往词表里加一行），与 008 / 009 也不同（那些是新的
    # 持久化产物）—— 这一个既加词表行、又动既有列与既有 CHECK。同样**新增迁移、
    # 不改 001–010**（R4）。
    Migration(11, "flow_external", SCHEMA_DIR / "011_flow_external.sql"),
    # 登记 `secret`（敏感串扫描，4.2）。它与 004–007 / 011 同形（往词表里加一行
    # + 三件套），但有一处不同：**`produced_by` 是空的** —— 产出者是一条独立的
    # 管线阶段（`magpie/core/scan.py`）而不是某个解析器，理由写在 012 的文件头
    # 第三节。镜头归属 `traffic`（计划书 §3.5 的镜头表里那一行的主语就是
    # `flow / secret`）。同样**新增迁移、不改 001–011**（R4）。
    Migration(12, "kind_secret", SCHEMA_DIR / "012_kind_secret.sql"),
    # 离线指纹引擎（4.3）：把 `fingerprint@1` 追加进 product / service 的
    # produced_by（json_insert，004 的写法）+ `fingerprint` 观测字段的多值声明。
    # 这一步**没有**新 kind —— 命中的落点是已登记的 product + runs，理由写在
    # 013 的文件头第二节。同样**新增迁移、不改 001–012**（R4）。
    Migration(13, "kind_fingerprint", SCHEMA_DIR / "013_kind_fingerprint.sql"),
    # scheme / port 的多值声明（0.20.0 C）：endpoint 身份跨 scheme 折叠，http→https
    # 与 80/443 双探是逐次遭遇的证据 —— 与 010 的 path 同判。真库里 125 条 open
    # 的 value_conflict 全落在这个形状上。只加声明行 + 一次性 Inbox 收口，
    # **视图不重建**（010 的视图每次计算都查声明表，加行即生效）。014 的文件头。
    Migration(14, "scheme_port_multivalue", SCHEMA_DIR / "014_scheme_port_multivalue.sql"),
    # JS 内容版本及静态引用的多值语义；同时登记 har@2 与显式本地 JS manifest 产出者。
    Migration(15, "js_intelligence", SCHEMA_DIR / "015_js_intelligence.sql"),
)


class MigrationError(RuntimeError):
    """schema 版本不一致 / 迁移失败。"""


_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

# 旧版 init_db 建出来的库靠这张表识别（"基线已应用"的判据）
_BASELINE_TABLE = "contributions"


def checksum(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def ensure_registry(conn: sqlite3.Connection) -> None:
    conn.execute(_REGISTRY_DDL)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """{version: (name, checksum)}；registry 不存在时返回空表。"""
    ensure_registry(conn)
    return {int(r[0]): (str(r[1]), str(r[2]))
            for r in conn.execute("SELECT version, name, checksum FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    rows = applied(conn)
    return max(rows) if rows else 0


def pending(conn: sqlite3.Connection) -> list[Migration]:
    done = applied(conn)
    return [m for m in MIGRATIONS if m.version not in done]


def migrate(conn: sqlite3.Connection, *, snapshot: bool = False,
            actor: str = "cli") -> list[dict]:
    """把库升到最新版本，返回本次应用的迁移列表（幂等：无事可做返回 []）。"""
    from .store import InvariantError, log_activity, now, online_backup

    ensure_registry(conn)
    done = applied(conn)

    # --- 基线采纳: 老库没有 registry，但 001 早已生效 ---
    if not done and _table_exists(conn, _BASELINE_TABLE):
        base = MIGRATIONS[0]
        conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at)"
            " VALUES (?,?,?,?)",
            (base.version, base.name, checksum(base.path), now()),
        )
        done = applied(conn)

    # --- 已应用迁移的校验和必须与文件一致（只增不改） ---
    by_version = {m.version: m for m in MIGRATIONS}
    for version, (name, recorded) in sorted(done.items()):
        m = by_version.get(version)
        if m is None:
            raise MigrationError(
                f"数据库里有未知迁移 v{version}（{name}）—— 程序版本低于数据库版本，拒绝继续")
        actual = checksum(m.path)
        if actual != recorded:
            raise MigrationError(
                f"迁移 v{version}（{name}）的文件已被修改（校验和 {recorded[:12]} → "
                f"{actual[:12]}）。已应用的迁移不可修改，请新增一个迁移文件。")

    todo = [m for m in MIGRATIONS if m.version not in done]
    if not todo:
        return []

    snapshot_path = None
    if snapshot:
        try:
            snapshot_path = str(online_backup(conn, tag=f"premigrate-v{todo[-1].version}"))
        except Exception as exc:                       # noqa: BLE001
            raise MigrationError(f"迁移前快照失败，已中止迁移: {exc}") from exc

    results: list[dict] = []
    for m in todo:
        try:
            conn.executescript(m.path.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            raise MigrationError(f"迁移 v{m.version}（{m.name}）执行失败: {exc}") from exc
        conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at)"
            " VALUES (?,?,?,?)", (m.version, m.name, checksum(m.path), now()),
        )
        results.append({"version": m.version, "name": m.name})

    log_activity(conn, actor=actor, action="schema.migrate", target_kind="schema",
                 detail={"applied": results, "snapshot": snapshot_path})
    assert len(applied(conn)) >= len(MIGRATIONS)
    return results
