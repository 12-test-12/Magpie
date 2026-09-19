"""管线编排 —— YAML 定义 + `after` 依赖 + 顺序执行（计划书 §5.2 / 2.5 的任务 3）。

    python -m magpie.core.pipeline run <yaml> [--dry-run] [--db <path>]

这同时是 2.5 的**最小触发入口**：CLI（2.2 / 2.6）已后置，若不给一个能跑的入口，
"导入"这件事做完之后没有任何人能用它，端到端验收无从下手。因此这里只做三件事：

  1. 读一个 YAML 管线定义；
  2. 按 `after` 依赖顺序执行每一步（顺序执行，**不做可视化编辑**）；
  3. 打印一份 JSON 汇总报告（与 ingest 的报告同源）。

**它不是完整 CLI**：没有子命令体系、没有 `--human` 表格、没有退出码表
（那三样是 2.6 的交付物）。2.7 / 2.8 之后导入入口由 GUI 接管，本入口保留为
脚本逃生通道。

YAML 形态（§5.2）:

    name: 常规一轮（域名+指纹）
    project: 某SRC
    scope: {include: ["*.example.com"], exclude: []}     # 项目不存在时按它创建
    data_dir: ./.magpie
    steps:
      - id: dns
        parser: domainlist
        source_glob: "recon/*subfinder*.txt"
        source: subfinder                                # 可选：来源名
      - id: probe
        parser: httpx
        source_glob: "recon/*httpx*.jsonl"
        after: [dns]

`after` 的语义（诚实交代边界）: 今天是**顺序依赖** —— 被依赖的步骤先跑；若它
一个文件都没导入成功，本步骤会在报告里被标 `dependency_empty: true`，但**仍然
执行**（今天的解析器之间没有真实的产物传递；真正的"上一步的产物喂给下一步"
要等阶段 3 的离线指纹引擎）。把它写成硬失败会凭空挡住合法的并行用法。

"一个文件都没导入成功"的判据：见 `_PRODUCED_STATES`。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import ingest, ops, store

USAGE = """用法:
  python -m magpie.core.pipeline run <pipeline.yaml> [--dry-run] [--db <path>]
                                 [--data-dir <dir>] [--base-dir <dir>]

  python -m magpie.core.pipeline undo <job_id> [--db <path>]     # 逃生通道
  python -m magpie.core.pipeline check <db_path>                 # 不变量快检

退出码: 0 成功 / 1 失败 / 2 用法错误。完整的退出码表属于 2.6，不在这里。
"""

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 文件状态里，哪些算"这一步真的产出了东西"。`dependency_empty` 只问这一件事。
#:
#: * `done` / `partial`  —— 事实落了库（partial 是"落了但没落全"，仍然是有产出）。
#: * `skipped`           —— 幂等跳过。它**算有产出**：跳过恰恰因为同一 sha256 已被
#:                          同一 fingerprint 导入过，上游的产物真实存在于库里。
#: * `needs_confirmation` —— **不算**。它为"待人工裁决"建了一个 job（所以旧判据
#:                          `job_id is not None` 会把它算成有产出），但这个 job 一条
#:                          事实都没落，下游拿到的是一个空的上游。审核 LARI-21 指出
#:                          的正是这一条：判据当时问的是"建没建作业"，而不是
#:                          "有没有产出"。
#: * `failed` / `cancelled` —— 显然不算。
#:
#: 它只是报告里的一个提示位，不改变执行 —— 判错一个提示位不会毁掉数据，
#: 但会让"下游为什么是空的"这个问题失去唯一的线索。
_PRODUCED_STATES = frozenset({"done", "partial", "skipped"})


# ---------------------------------------------------------------------------
# 定义加载与校验（YAML 一律 lazy import —— core 只用标准库，红线 R6）
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    try:
        import yaml                                        # noqa: PLC0415
    except ImportError as exc:                             # pragma: no cover
        raise RuntimeError(
            "读 YAML 管线定义需要 PyYAML，但它不在当前解释器里。\n"
            "  pip install pyyaml   （或 py -3.13 -m pip install pyyaml）\n"
            "它只在需要 YAML 时才 import，所以核心功能不受影响（core 仅标准库）。"
        ) from exc
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_definition(path: str | Path) -> dict[str, Any]:
    """读入并校验管线定义。任何问题都抛**可读**的 ValueError。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"管线定义不存在：{path}")
    try:
        raw = _load_yaml(path)
    except Exception as exc:
        raise ValueError(f"{path} 不是合法 YAML：{type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 的顶层必须是映射（name / project / steps）")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path}: steps 必须是非空列表")
    ids: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{index}] 必须是映射")
        sid = step.get("id")
        if not sid:
            raise ValueError(f"steps[{index}] 缺少 id")
        # 判重必须按**归一化后的 id**（`str`），与 `order_steps` 的 `by_id` 同一口径。
        # 旧实现拿 YAML 的原始值去和 `ids`（装着 str）比：`id: 1` 与 `id: 1` 因为
        # `1 in ["1"]` 为假而**判不出来**，于是 `by_id = {str(id): step}` 把后者覆盖
        # 前者，拓扑排序只剩一步 —— 两步的管线静默跑成一步，报告里也少一步。
        key = str(sid)
        if key in ids:
            raise ValueError(
                f"steps[{index}] 的 id {sid!r} 重复（已有一个 {key!r}；"
                f"id 按字符串比较，`1` 与 `\"1\"` 视为同一个）")
        ids.append(key)
        if not step.get("source_glob") and not step.get("source"):
            raise ValueError(f"step {sid}: 至少要给 source_glob（要导入什么）")
        after = step.get("after") or []
        if not isinstance(after, list):
            raise ValueError(f"step {sid}: after 必须是列表")
    for step in steps:
        for dep in step.get("after") or []:
            if str(dep) not in ids:                          # 同上：按归一化 id 比
                raise ValueError(
                    f"step {step['id']}: after 引用了不存在的步骤 {dep!r}；"
                    f"已定义的步骤：{ids}")
    order_steps(steps)                                      # 顺带做一次环检测
    raw["_path"] = str(path)
    return raw


def order_steps(steps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `after` 依赖做稳定拓扑排序。有环 → ValueError（并指出环上的步骤）。

    这个函数**必须**拒绝重复 id: `by_id` 是 `{str(id): step}`，两个步骤归一化到
    同一个 id 时后者会覆盖前者，被覆盖的那一步从此不在 `ordered` 里 —— 静默丢步。
    `load_definition` 已经判过一次重（带下标，报错更好读），这里再判一次是因为
    "丢"就发生在本函数：直接调用本函数的调用方不该拿到一个少了一步的结果。
    """
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        key = str(step["id"])
        if key in by_id:
            raise ValueError(f"steps 里有重复的 id {key!r}（后一个会覆盖前一个 → 静默丢步）")
        by_id[key] = step
    ordered: list[dict[str, Any]] = []
    done: set[str] = set()
    visiting: list[str] = []

    def visit(sid: str) -> None:
        if sid in done:
            return
        if sid in visiting:
            cycle = " → ".join(visiting[visiting.index(sid):] + [sid])
            raise ValueError(f"after 依赖成环：{cycle}")
        if sid not in by_id:
            raise ValueError(f"after 引用了不存在的步骤 {sid!r}；已定义的步骤：{sorted(by_id)}")
        visiting.append(sid)
        for dep in by_id[sid].get("after") or []:
            visit(str(dep))
        visiting.pop()
        done.add(sid)
        ordered.append(by_id[sid])

    for step in steps:                                      # 就地顺序优先，结果可复现
        visit(str(step["id"]))
    return ordered


# ---------------------------------------------------------------------------
# 项目解析
# ---------------------------------------------------------------------------


def resolve_project(conn: sqlite3.Connection, definition: dict[str, Any]) -> dict[str, Any]:
    """`project:` 可以是 id、名字；不存在且给了 scope 时就地创建。"""
    spec = definition.get("project")
    if spec is None:
        raise ValueError("管线定义缺少 project:（项目名或 id）")
    row = None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        row = conn.execute("SELECT id, name FROM projects WHERE id=?", (int(spec),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, name FROM projects WHERE name=?", (str(spec),)).fetchone()
    if row is not None:
        return {"id": int(row["id"]), "name": str(row["name"]), "created": False}

    scope = definition.get("scope")
    if not isinstance(scope, dict) or not scope:
        raise ValueError(
            f"项目 {spec!r} 不存在，且管线定义里没有 scope —— 无法安全地凭空建项目。\n"
            f'加一段 scope: {{include: ["*.example.com"], exclude: []}} 即可。')
    scope = dict(scope)
    if definition.get("parser_fingerprint"):
        # 项目的当前 parser 随 scope 一起版本化（§5.1）；create_project 会登记它。
        scope.setdefault("parser_fingerprint", str(definition["parser_fingerprint"]))
    project_id = ops.create_project(conn, str(spec), scope)
    return {"id": project_id, "name": str(spec), "created": True,
            "scope_version": 1}


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def run_pipeline(conn: sqlite3.Connection, definition: dict[str, Any], *,
                 base_dir: str | Path | None = None,
                 data_dir: str | Path | None = None,
                 dry_run: bool = False, archive: str = "archived",
                 actor: str = "pipeline") -> dict[str, Any]:
    """顺序执行整条管线，返回汇总报告（可直接 json.dumps）。"""
    started = time.perf_counter()
    definition_path = Path(str(definition.get("_path") or "."))
    base = Path(base_dir) if base_dir is not None else definition_path.parent
    dd = Path(data_dir) if data_dir is not None else Path(
        definition.get("data_dir") or (base / ".magpie"))
    if not dd.is_absolute():
        dd = base / dd
    dd = dd.resolve()

    project = resolve_project(conn, definition)
    steps = order_steps(definition["steps"])
    step_reports: list[dict[str, Any]] = []
    produced: dict[str, int] = {}

    for step in steps:
        sid = str(step["id"])
        pattern = str(step.get("source_glob") or step.get("source") or "")
        matched = sorted(base.glob(pattern)) if pattern else []
        deps = [str(d) for d in (step.get("after") or [])]
        dependency_empty = bool(deps) and all(produced.get(d, 0) == 0 for d in deps)

        problems: list[str] = []
        if not matched:
            problems.append(f"source_glob {pattern!r} 没有匹配到任何文件（基准目录 {base}）")

        opts = ingest.IngestOptions(
            project_id=project["id"], data_dir=dd,
            parser=step.get("parser"), source_name=step.get("source"),
            authority_level=str(step.get("authority") or "unverified"),
            archive=str(step.get("archive") or archive),
            dry_run=bool(dry_run or step.get("dry_run")), actor=actor,
            # 报告副本落进 `reports/`（坏行明细本来就在那儿）。doctor 报"未知
            # 字段"只能靠它 —— 那个信号只活在一次解析里，事后无迹可查。
            report_dir=dd / "reports",
            batch_size=int(step.get("batch_size") or ingest.DEFAULT_BATCH))
        report = ingest.ingest_paths(conn, opts, matched)
        produced[sid] = sum(1 for f in report["files"]
                            if f.get("state") in _PRODUCED_STATES)
        step_reports.append({
            "id": sid,
            "parser": step.get("parser"),
            "source_glob": pattern,
            "after": deps,
            "dependency_empty": dependency_empty,
            "matched": len(matched),
            "problems": problems + list(report.get("problems") or []),
            "totals": report["totals"],
            "job_ids": report["job_ids"],
            "files": [{k: f.get(k) for k in
                       ("path", "state", "job_id", "inbox_item_id", "sha256", "parser",
                        "counts", "reconcile", "integrity", "unknown_fields_detail",
                        "skip_reason", "reason", "parse_error", "badlines_report",
                        "encoding", "arbitration")}
                      for f in report["files"]],
        })

    totals: dict[str, int] = {}
    for step in step_reports:
        for name, value in step["totals"].items():
            totals[name] = totals.get(name, 0) + int(value)

    return {
        "kind": "pipeline",
        "pipeline": str(definition.get("name") or definition_path.stem),
        "definition": str(definition_path),
        "dry_run": bool(dry_run),
        "project": project,
        "data_dir": str(dd),
        "steps": step_reports,
        "totals": totals,
        "job_ids": [jid for step in step_reports for jid in step["job_ids"]],
        "db": {
            "observations": _scalar(conn, "SELECT COUNT(*) FROM observations"
                                          " WHERE project_id=? AND is_current=1",
                                    project["id"]),
            "relations": _scalar(conn, "SELECT COUNT(*) FROM relations"
                                       " WHERE project_id=? AND is_current=1",
                                 project["id"]),
            "entities": _scalar(conn, "SELECT COUNT(*) FROM entities"
                                      " WHERE project_id=? AND is_current=1",
                                project["id"]),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = store.connect(str(db_path))
    store.init_db(conn)
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, file=sys.stderr)
        return EXIT_OK if args else EXIT_USAGE

    command, rest = args[0], args[1:]
    flags, positional = _parse_flags(rest)

    if command == "run":
        if not positional:
            print("run 需要 <pipeline.yaml>\n\n" + USAGE, file=sys.stderr)
            return EXIT_USAGE
        try:
            definition = load_definition(positional[0])
        except ValueError as exc:
            print(f"!! {exc}", file=sys.stderr)
            return EXIT_USAGE
        # data_dir 只解析一次，db 与归档目录必须落在**同一个**基准下 ——
        # 分开算过一次，结果 db 建在 CWD 而归档在 YAML 同级目录，报告里的数字
        # 是真的，但人打开那个 .db 却是空的。
        base = Path(flags["base-dir"]) if flags.get("base-dir") else Path(
            str(definition.get("_path") or ".")).parent
        base = base.resolve()
        dd = Path(flags.get("data-dir") or definition.get("data_dir") or ".magpie")
        if not dd.is_absolute():
            dd = base / dd
        dd = dd.resolve()
        db_path = Path(flags["db"]) if flags.get("db") else dd / "magpie.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open_db(db_path)
        try:
            report = run_pipeline(conn, definition, base_dir=base,
                                  data_dir=dd, dry_run=bool(flags.get("dry-run")))
        except (ValueError, store.InvariantError) as exc:
            print(f"!! {exc}", file=sys.stderr)
            conn.close()
            return EXIT_FAIL
        _emit(report)
        failed = [f for s in report["steps"] for f in s["files"]
                  if f.get("state") in ("failed", "needs_confirmation")]
        conn.close()
        return EXIT_FAIL if failed else EXIT_OK

    if command == "undo":
        if not positional or not positional[0].isdigit():
            print("undo 需要 <job_id>\n\n" + USAGE, file=sys.stderr)
            return EXIT_USAGE
        db_path = flags.get("db")
        if not db_path:
            print("undo 需要 --db <path>", file=sys.stderr)
            return EXIT_USAGE
        conn = _open_db(db_path)
        try:
            report = ops.undo(conn, int(positional[0]), dry_run=bool(flags.get("dry-run")))
        except store.InvariantError as exc:
            print(f"!! {exc}", file=sys.stderr)
            return EXIT_FAIL
        finally:
            conn.close()
        _emit(report)
        return EXIT_OK

    if command == "check":
        if not positional:
            print("check 需要 <db_path>", file=sys.stderr)
            return EXIT_USAGE
        conn = _open_db(positional[0])
        try:
            report = store.integrity_check(conn)
        finally:
            conn.close()
        _emit({"kind": "check", "ok": bool(report.get("ok")),
               "violations": report.get("violations") or [],
               "foreign_key_check": report.get("foreign_key_check") or [],
               "sqlite_integrity": report.get("sqlite_integrity")})
        return EXIT_OK if report.get("ok") else EXIT_FAIL

    print(f"未知子命令 {command!r}\n\n{USAGE}", file=sys.stderr)
    return EXIT_USAGE


def _parse_flags(args: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """极简参数解析：只要 `--flag` / `--key value`。

    刻意不引 argparse：入口只有十几行的体量，而 argparse 的子命令 + 全局选项
    组合会把"最小入口"变成半个 CLI（那是 2.6 的活）。
    """
    flags: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item.startswith("--"):
            name = item[2:]
            if name in ("dry-run", "force", "quiet"):
                flags[name] = True
            elif index + 1 < len(args):
                flags[name] = args[index + 1]
                index += 1
            else:
                flags[name] = True
        else:
            positional.append(item)
        index += 1
    return flags, positional


def _emit(report: dict[str, Any]) -> None:
    """打印 JSON 汇总报告。**报告是唯一的交付通道**（人看的是它，不是退出码）。"""
    text = json.dumps(report, ensure_ascii=False, indent=2)
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8")           # Windows 控制台默认 GBK
    except (AttributeError, ValueError):               # pragma: no cover
        pass
    print(text, file=stream)


if __name__ == "__main__":                             # pragma: no cover
    sys.exit(main())
