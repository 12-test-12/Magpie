r"""多值字段声明表的**复现工具**（LARI-35）。

回答两个问题，第二个才是重点：

    1. 把仓库里的真夹具逐份导进去，`v_field_conflicts` 到底剩下什么？
    2. 剩下的那些，是不是都**故意**留下的（"对同一个问题的两个回答"）？

为什么要有它
------------
`schema/010_multivalue_fields.sql` 里那 15 行声明是**判断**，判断需要证据。
这份脚本就是那份证据的可复现形式 —— 与 `bench/test_perf_ingest.py` 把
"55s"变成一条能重跑的命令是同一条纪律。

**加字段时先跑它。** 往声明表里加一行之前，先看这个字段出现在哪一栏：

    [声明区]  值集是**来源各自枚举出来的枚举项** → 该进 `multivalue_fields`
    [真分歧]  是**对同一个问题的两个回答**       → 留在冲突域里，别加

跑法（工程解释器，见 pyproject.toml 的下限）:

    py -3.13 _probe_multivalue.py

它**只读不判**：不 assert、不改库、不进 check.py。判据在
`magpie/core/tests/test_multivalue_conflicts.py`（那边是断言，这边是仪器）。

第三段（`--dense`）造一份"1000 行 × 10 主机"的密集夹具 —— 它是发现
`content_length` 的那把网: httpx 用**另一个名字**写 `length` 这同一种事实，
只盯真夹具的字段名会漏掉它。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import ingest, ops, store  # noqa: E402

FIXTURES = ROOT / "magpie" / "core" / "tests" / "fixtures"
SCRATCH = ROOT / "_scratch" / "_probe_multivalue"


def declared() -> dict[tuple[str, str], str]:
    """当前的声明表（从库结构读，不抄一份）。"""
    conn = store.connect(":memory:")
    store.init_db(conn)
    out = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
        "SELECT field_namespace, semantic_field, why FROM multivalue_fields")}
    conn.close()
    return out


def import_everything(conn, project: int) -> None:
    for path in sorted(p for p in FIXTURES.glob("*/*/*")
                       if p.is_file() and p.name != "expected.json"):
        try:
            ingest.ingest_file(
                conn, ingest.IngestOptions(project_id=project, data_dir=SCRATCH,
                                           actor="probe"), path)
        except Exception as exc:                        # noqa: BLE001
            print(f"  !! {path.parent.name}: {type(exc).__name__}: {exc}")


def _generate(path: Path, n_lines: int, n_hosts: int) -> None:
    """与 bench/test_perf_ingest.py 同形的 httpx JSONL（逐次字段全在变）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(n_lines):
            host = f"h{i % n_hosts}.probe.example.com"
            fh.write(json.dumps({
                "timestamp": "2026-09-17T10:00:00.000Z",
                "port": str(443 if i % 3 else 80), "url": f"https://{host}/",
                "input": host, "title": f"host {i % n_hosts}", "scheme": "https",
                "webserver": "nginx", "content_type": "text/html", "host": host,
                "path": "/", "status_code": 200 + (i % 5),
                "content_length": 1000 + i, "failed": False,
                "tech": ["Nginx:1.24.0"],
                "cname": [f"edge{i % 7}.probe.example.net"],
                "tls": {"version": "tls13"},
                "a": [f"10.{i % 250}.{(i // 250) % 250}.7"],
                "final_url": f"https://{host}/after/{i}",
            }, ensure_ascii=False) + "\n")


def report(conn, project: int, title: str) -> int:
    """打印一个项目的冲突清单，分两栏报。返回"错放在真分歧栏"的条数。"""
    table = declared()
    rows = [dict(r) for r in conn.execute(
        "SELECT c.semantic_field, c.field_namespace, c.values_csv, e.kind"
        "  FROM v_field_conflicts c JOIN entities e ON e.id = c.subject_id"
        " WHERE c.project_id=? ORDER BY e.kind, c.semantic_field", (project,))]
    print(f"\n=== {title} ===（{len(rows)} 条）")
    multivalue = [r for r in rows
                  if (r["field_namespace"], r["semantic_field"]) in table]
    real = [r for r in rows
            if (r["field_namespace"], r["semantic_field"]) not in table]
    print(f"  [声明区] {len(multivalue)} 条 —— 不应存在")
    for r in multivalue:
        print(f"     {r['kind']:10s} {r['semantic_field']:20s} {str(r['values_csv'])[:80]}")
    print(f"  [真分歧] {len(real)} 条 —— 故意留下的，逐条看一眼是否确实是"
          f"『对同一个问题的两个回答』")
    for r in real:
        print(f"     {r['kind']:10s} {r['semantic_field']:20s} {str(r['values_csv'])[:80]}")

    open_items = int(conn.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'"
        " AND kind='value_conflict'", (project,)).fetchone()[0])
    print(f"  Inbox 里的 open value_conflict 项: {open_items}")
    return len(multivalue)


def main(argv: list[str]) -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    table = declared()
    print(f"声明表 {len(table)} 行（schema/010_multivalue_fields.sql）：")
    for (ns, field), why in sorted(table.items()):
        print(f"  {ns:18s} {field:16s} {why}")

    conn = store.connect(":memory:")
    store.init_db(conn)
    project = ops.create_project(conn, "probe", {"include": ["*.probe.example.com"],
                                                 "exclude": []})
    import_everything(conn, project)
    bad = report(conn, project, "真夹具（12 份）")

    if "--dense" in argv:
        dense = SCRATCH / "dense.jsonl"
        _generate(dense, 1000, 10)
        dense_project = ops.create_project(
            conn, "probe-dense", {"include": ["*.probe.example.com"], "exclude": []})
        ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=dense_project, data_dir=SCRATCH, actor="probe"), dense)
        bad += report(conn, dense_project, "密集夹具（1000 行 × 10 主机）")
    conn.close()

    if bad:
        print(f"\n结论: 【声明区有 {bad} 条】—— 应当是 0。"
              f"说明有个多值字段还没进 `multivalue_fields`，"
              f"照上面的尺子判断它该不该进。")
    else:
        print("\n结论: 【声明区为空】—— 声明表覆盖了所有已知的多值字段。"
              "\n      （这不等于『没有假冲突』：真分歧那一栏要逐条看一眼，"
              "确认它确实是『对同一个问题的两个回答』。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
