r"""阶段 4.1 的两件**仪器**（LARI-36）。

判据在测试里，这里只回答两个"改之前先看一眼"的问题 —— 与
`_probe_multivalue.py` / `bench/test_perf_ingest.py` 同一条纪律：一个写在文件头里
的测量，必须有一条能把它重跑一遍的命令。

    1.  **改迁移 011 那段表重建时** —— SQLite 的两个坑会不会踩到？
    2.  **改 `flow` 的身份规则时** —— 三处落点（迁移的 `identity_rule`、夹具的
        `identity_rule`、`base.KIND_REGISTRY`）还能逐字一致吗？

跑法（工程解释器，见 `pyproject.toml` 的下限）:

    py -3.13 _probe_flow_41.py                 # 两段都跑，只报告、不写盘
    py -3.13 _probe_flow_41.py --write-fixture  # 用迁移里的正文重写 kind 夹具

第 2 段的 `--write-fixture` 是**唯一会写盘**的那条路（写的是仓库里的夹具文件）：
身份规则有 700 多字，三处落点靠手抄必然分叉，而分叉的症状是
`test_every_registered_kind_has_all_three_parts` 一句"两处不一致" —— 那时候你还得
再抄一遍。这个开关把"抄"变成"重新生成"，抄写错误从**可能发生**变成**不可能**。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import migrate, store  # noqa: E402

FIXTURE = ROOT / "magpie" / "core" / "tests" / "fixtures" / "kinds" / "flow.json"


# ---------------------------------------------------------------------------
# 1. SQLite 的表重建：两个坑
# ---------------------------------------------------------------------------

#: 一个**带 CHECK 的表 + 一个读它的视图** —— 011 要动的那张表的形状。
#: 玩具而不是真 schema：这里要看的机制与列数无关，而 20 列的 DDL 会把三行判据
#: 埋进噪声里。（真 schema 上的那一条实测见下面 A 段的说明。）
_TRAP_SCHEMA = """
CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('a','b')));
CREATE VIEW v AS SELECT id, kind FROM t WHERE kind='a';
INSERT INTO t VALUES (1,'a'), (2,'b');
"""

#: 换了 CHECK 的替身表（多一个取值 `c`）。
_NEW_TABLE = ("CREATE TABLE t_new (id INTEGER PRIMARY KEY,"
              " kind TEXT NOT NULL CHECK (kind IN ('a','b','c')));")
_NEW_TABLE_INPLACE = _NEW_TABLE.replace("t_new", "t")


def probe_sqlite_rebuild() -> None:
    """把一个带 CHECK 的表重建一遍 —— 三种写法，**两种会坏**。

    011 要在 `inbox_items.kind` 的 CHECK 里加第八个取值，而 SQLite **没有**
    `ALTER TABLE … DROP CONSTRAINT`，只能重建（建新表 → 拷行 → 删旧表 → 改名）。
    三条路里只有一条是对的，而另外两条的错误各不相同：

      A. 建新表（**另一个名字**）→ 删旧表 → 把新表改名过来
         → **当场报错**：`ALTER TABLE … RENAME`（3.25 起）会重新解析并改写
         视图正文，而那一刻 `v_integrity_violations` 指向的表刚被删掉 ——
         报错原文是 `error in view v_integrity_violations: no such table:
         main.inbox_items`（**真 schema 上实测，SQLite 3.50**）。
      B. 先把旧表改名 → 建新表 → 拷行 → 删旧表，**不带** `legacy_alter_table`
         → 不报错，但视图正文被**静默改写**成指向 `t_old`；删掉 `t_old` 之后
         那个视图永久指向一张不存在的表。**没有任何信号** —— 直到有人查它。
      C. 同 B，但带上 `PRAGMA legacy_alter_table = ON`（011 走的就是这条）
         → 视图正文一字未动，数据完好。

    这一段把三条都真跑一遍并打印结果：读的人不必相信上面的描述。
    """
    import sqlite3

    def fresh() -> "sqlite3.Connection":
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.executescript(_TRAP_SCHEMA)
        return conn

    print("=== 1 · SQLite 表重建：三条路，两条会坏 ===")

    conn = fresh()
    try:
        conn.executescript(f"""
BEGIN;
{_NEW_TABLE}
DROP TABLE t;
ALTER TABLE t_new RENAME TO t;
COMMIT;
""")
        print("  A 建新/删旧/改名过来        : 通过（**与实测不符，请复核**）")
    except sqlite3.Error as exc:
        print(f"  A 建新/删旧/改名过来        : {type(exc).__name__}: {exc}")
    conn.close()

    conn = fresh()
    conn.executescript(f"""
BEGIN;
ALTER TABLE t RENAME TO t_old;
{_NEW_TABLE_INPLACE}
INSERT INTO t SELECT id, kind FROM t_old;
DROP TABLE t_old;
COMMIT;
""")
    view_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='v'").fetchone()[0]
    print("  B 先改名（**不带** legacy）  : 不报错，但视图正文变成")
    print(f"      → {view_sql}")
    try:
        conn.execute("SELECT * FROM v").fetchall()
        print("      查它                      : 查得动（表恰好同名时看不出问题）")
    except sqlite3.Error as exc:
        print(f"      查它                      : {type(exc).__name__}: {exc}")
    conn.close()

    conn = fresh()
    conn.executescript(f"""
BEGIN;
PRAGMA legacy_alter_table = ON;
ALTER TABLE t RENAME TO t_old;
{_NEW_TABLE_INPLACE}
INSERT INTO t SELECT id, kind FROM t_old;
DROP TABLE t_old;
PRAGMA legacy_alter_table = OFF;
COMMIT;
""")
    view_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='v'").fetchone()[0]
    rows = conn.execute("SELECT * FROM v").fetchall()
    print("  C 同上 + legacy（011 走这条）: 视图正文一字未动")
    print(f"      → {view_sql}")
    print(f"      数据                      : t={conn.execute('SELECT COUNT(*) FROM t').fetchone()[0]} 行，"
          f"v={len(rows)} 行")
    conn.close()
    print()


# ---------------------------------------------------------------------------
# 2. flow 的身份规则：三处落点
# ---------------------------------------------------------------------------

def probe_flow_identity(write: bool) -> int:
    """把迁移里的 `identity_rule` 与夹具里的逐字比一遍（并可选地同步过去）。

    三处落点：`schema/011_flow_external.sql` 的 `INSERT`（唯一事实来源）→
    `kind_registry`（迁移之后就是库里的那一行）→ `fixtures/kinds/flow.json`。
    第三处由这一段维护：它**不是**手抄的，是从库里的那一行生成出来的。

    夹具里那些 `examples` / `rejects` 也会被**逐条执行一遍**（调
    `normalize.flow_key`），所以"样例算不出声明的 key"在这里就会看见，而不必等
    到 pytest 报一条"夹具引用了白名单之外的规则"。
    """
    print("=== 2 · flow 的身份规则：三处落点 ===")
    conn = store.connect(":memory:")
    store.init_db(conn)
    row = conn.execute(
        "SELECT identity_rule, lens, produced_by FROM kind_registry WHERE kind='flow'"
    ).fetchone()
    if row is None:
        print(f"  !! 库里的 kind_registry 没有 flow —— 迁移链是 {migrate.current_version(conn)}")
        return 1
    print(f"  库里的那一行: lens={row['lens']}  produced_by={row['produced_by']}")
    print(f"  身份规则正文: {len(str(row['identity_rule']))} 字")

    if not FIXTURE.is_file():
        print(f"  !! 夹具不存在：{FIXTURE}")
        return 1
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    same = doc.get("identity_rule") == row["identity_rule"]
    print(f"  夹具 {FIXTURE.name}: identity_rule {'逐字一致' if same else '**不一致**'}")

    from magpie.core import normalize
    bad = 0
    for example in doc.get("examples") or []:
        got = normalize.flow_key(*example["args"])
        if got != example["key"]:
            bad += 1
            print(f"    样例算不出声明的 key: {example['args']} → {got!r}，"
                  f"声明 {example['key']!r}")
    for reject in doc.get("rejects") or []:
        try:
            got = normalize.flow_key(*reject["args"])
        except normalize.NormalizeError:
            continue
        bad += 1
        print(f"    反例**没有被拒**: {reject['args']} → {got!r}")
    print(f"  样例 {len(doc.get('examples') or [])} 条 / 反例 "
          f"{len(doc.get('rejects') or [])} 条，逐条执行："
          f"{'全部通过' if not bad else f'{bad} 条有问题'}")

    if write:
        if same:
            print("  没有要写的：夹具与库里那一行已经逐字一致。")
        else:
            doc["identity_rule"] = str(row["identity_rule"])
            doc["lens"] = str(row["lens"])
            FIXTURE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
            print(f"  已用库里的正文重写 {FIXTURE.name} —— 再跑一次确认「逐字一致」。")
    elif not same:
        print("  （要同步的话加 --write-fixture）")
    conn.close()
    return 0 if (same and not bad) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-fixture", action="store_true",
                        help="用迁移里的正文重写 fixtures/kinds/flow.json")
    args = parser.parse_args()
    probe_sqlite_rebuild()
    return probe_flow_identity(args.write_fixture)


if __name__ == "__main__":
    sys.exit(main())
