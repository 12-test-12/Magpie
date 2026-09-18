"""打开数据区并把"库能不能读"这件事查清楚（2.2）。

三件事，每一件都有过教训:

 1. **`--db` 与 `--data-dir` 落在同一个基准下**。`core.pipeline` 与
    `api.app` 都从 `data_dir` 推 `magpie.db`，CLI 必须推同一个 —— 否则会出现
    "pipeline 导进 `.magpie`、`magpie ls` 打开另一个文件"这种最容易被读成
    "数据丢了"的分叉。`verify_v32r5.py` 的文件头把这条列为端到端最容易坏的一段。

 2. **只读是数据库自己执行的事实，不是我的自觉**。连接建好后立刻
    `PRAGMA query_only = ON`（与 `api.app` 的 `connect_ro` 同一条纪律）：
    哪天有人在只读命令里顺手写了条 UPDATE，它会**当场报错**，而不是在一次
    并发写里留下一个谁也复现不出来的状态。R1"写入口唯一"的全部价值就在这。

 3. **版本不匹配要在打开之后就判**，而且判的方向有两个。库比程序**新**
    （有本程序不认识的迁移）与库比程序**旧**（有没应用的迁移）都不是"读一下
    看看"能糊过去的：前者意味着有一列的数据语义我不认识，后者意味着我要读的
    表可能还不存在。两种都返回退出码 5，但 code 与 hint 分开 —— agent 要的是
    "我该怎么办"。

⚠️ 这里**刻意不调用 `migrate.applied()` / `migrate.pending()`**：它们内部会
`ensure_registry()`，那是一句 `CREATE TABLE IF NOT EXISTS` —— 在
`query_only = ON` 的连接上会直接炸掉只读命令。版本判断改成"先看
`sqlite_master` 里有没有登记的账本，再读它"，全程只有 SELECT。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..core import migrate, store
from .envelope import EXIT_DATA, EXIT_LOCKED, EXIT_VERSION, CliError

#: 数据区默认名。**与 `api.app.DEFAULT_DATA_DIRNAME` / `core.pipeline` 的
#: `data_dir` 缺省值一致**（三处指同一个目录）。
DEFAULT_DATA_DIR = ".magpie"

#: 版本账本所在的表。名字与 `migrate._REGISTRY_DDL` 一致。
REGISTRY_TABLE = "schema_migrations"


def resolve_db(db: str | None, data_dir: str | None) -> Path:
    """`--db` / `--data-dir` → 数据库文件路径。**唯一一处**做这个推导。"""
    dd = Path(data_dir) if data_dir else Path(DEFAULT_DATA_DIR)
    return Path(db) if db else dd / "magpie.db"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def current_version(conn: sqlite3.Connection) -> int:
    """库里已应用的 schema 版本；账本不存在时返回 0。

    0 与"没有这个库"在**读**这一步是同一件事：一张表都还没建。
    """
    if not _table_exists(conn, REGISTRY_TABLE):
        return 0
    row = conn.execute(f"SELECT MAX(version) FROM {REGISTRY_TABLE}").fetchone()
    return int(row[0] or 0)


def known_versions() -> tuple[int, int]:
    """本程序认识的最低 / 最高迁移版本（从 `migrate.MIGRATIONS` 读，不另抄一份）。"""
    versions = [m.version for m in migrate.MIGRATIONS]
    return min(versions), max(versions)


def check_version(conn: sqlite3.Connection, db: Path) -> int:
    """版本体检。**通过则返回当前版本，不通过则 raise**（退出码 5）。"""
    version = current_version(conn)
    lowest, highest = known_versions()

    if version == 0:
        raise CliError(
            "E_DB_NOT_INITIALIZED",
            f"{db} 存在但一张 schema 表都没有",
            exit_code=EXIT_DATA,
            hint=f"先跑 `magpie init --db {db}` 把 schema 建出来。"
                 f"本步的只读命令不建库 —— 读命令顺手建库会让'库在哪'变成"
                 f"一个没人能回答的问题。")
    if version > highest:
        raise CliError(
            "E_SCHEMA_TOO_NEW",
            f"库的 schema 版本是 {version}，本程序只认识到 {highest}",
            exit_code=EXIT_VERSION,
            hint=f"库比程序新：有 v{version} 的迁移是本程序不认识的，它的列语义"
                 f"我读不懂。升级 magpie（当前版本见 `magpie --version`）后再读，"
                 f"不要手改 schema_migrations。")
    if version < highest:
        pending = [m.version for m in migrate.MIGRATIONS
                   if lowest <= m.version and m.version > version]
        raise CliError(
            "E_SCHEMA_OUTDATED",
            f"库的 schema 版本是 {version}，本程序要求 {highest}",
            exit_code=EXIT_VERSION,
            hint=f"待应用迁移：{pending}。跑 `magpie init` 升级（只读命令不会"
                 f"顺手改 schema）。",
            pending=pending)
    return version


class Database:
    """一次命令要用到的库状态：路径、连接、版本。

    连接是**懒建**的，好让 `magpie init` 与用法错的路径有各自干净的分支。
    """

    def __init__(self, db: Path, *, read_only: bool = True) -> None:
        self.path = Path(db)
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        self.schema_version = 0

    # -- 打开 -----------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """只读连接。**版本体检在这里做完**，命令拿到的一定是一个版本自洽的库。

        与 `api.app.ApiContext.connect_ro` 同一条纪律：`query_only = ON` 让
        "这条连接只读"变成数据库自己执行的事实。
        """
        if not self.read_only:
            raise RuntimeError("connect() 是只读路径；`init` 用 writable()")
        if self._conn is not None:
            return self._conn
        if not self.path.exists():
            raise CliError(
                "E_DB_MISSING", f"数据库不存在：{self.path}", exit_code=EXIT_DATA,
                hint=f"先跑 `magpie init --db {self.path}`；"
                     f"或用 `--data-dir <目录>` / `--db <文件>` 指到你真正的数据区。")
        conn = _open(self.path)
        conn.execute("PRAGMA query_only = ON")
        try:
            self.schema_version = check_version(conn, self.path)
        except CliError:
            conn.close()
            raise
        self._conn = conn
        return conn

    # -- 写路径（只有 `init` 用） ----------------------------------------------
    def writable(self) -> sqlite3.Connection:
        """可写连接。**本步只有 `magpie init` 会走到这里。**

        它写 schema，不写事实 —— 建库是"让只读命令能用"的前置动作，
        与"引入写路径"是两回事。测试里有一条断言钉住这个区别：
        `init` 前后 fact 各表的行数一字不变。

        **刻意不在这里做版本体检**：这里的正常状态恰恰是"版本是 0"
        （库还没建），而 0 在只读路径上是一个错误。
        """
        if self.read_only:                                       # pragma: no cover
            raise RuntimeError("writable() 只对 read_only=False 的库有意义")
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = _open(self.path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def display(self) -> str:
        """信封 `meta.db` 里那串。用绝对路径 —— 相对路径在报错里等于没说。"""
        try:
            return str(self.path.resolve())
        except OSError:                                          # pragma: no cover
            return str(self.path)


def _open(db: Path) -> sqlite3.Connection:
    """建一条连接（`store.connect` 的薄包装，用同一套 PRAGMA）。"""
    try:
        conn = store.connect(db)
    except sqlite3.Error as exc:
        raise _map_sqlite(exc, db) from exc
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _map_sqlite(exc: sqlite3.Error, db: Path) -> CliError:
    """数据库异常 → CLI 错误。

    **锁冲突单列一档**（退出码 4 ↔ HTTP 423）：它和"数据坏了"要用完全不同的
    动作应对 —— 前者等一会儿重试就好，后者要去看体检报告。把两者塞进同一档，
    调用方就只能靠字符串匹配决定重试与否。
    """
    text = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and (
            "locked" in text or "busy" in text):
        return CliError(
            "E_LOCKED", f"数据库被占用：{db}（{text}）", exit_code=EXIT_LOCKED,
            hint="另一个进程正在写（`magpie app` 的写队列，或另一次导入）。"
                 "等它结束再重试 —— 只读命令不重试、不等待，因为脚本需要一个"
                 "确定的答案。")
    if isinstance(exc, sqlite3.DatabaseError):
        return CliError(
            "E_DB_NOT_A_DB", f"{db} 不是一个可读的 SQLite 库：{text}",
            exit_code=EXIT_DATA,
            hint="文件被别的程序占着、拷坏了，或者根本不是库文件。"
                 "`magpie doctor` 能体检的前提正是它还能被读开。")
    return CliError("E_DB", f"数据库错误：{type(exc).__name__}: {text}",
                    exit_code=EXIT_DATA)
