"""2.5 · 导入管线的验收回归网（计划书 §5 / 开发实施计划 2.5）。

纪律与既有测试一致：**每一条验收标准都对应一个用例**，标准写在哪，
用例就在哪 —— 验收标准比功能多，是因为这一段（编码、大文件、部分失败、幂等）
恰好是最容易"看起来跑通了"的一段。

    | 幂等      | 同一文件导入两次：第二次 skipped，观测数不变（硬性） |
    | --dry-run | 跑完 fact 表行数完全不变（反例断言）                 |
    | 可撤销    | 导入 → undo → 观测数回到导入前；redo → 精确复原       |
    | 取消      | 中断后已提交部分仍可 undo，不留下不一致状态           |
    | 报告      | 必须含 6 个计数                                        |
    | 人工保护  | manual 归属不被触碰                                    |
    | 编码      | GBK / BOM / CRLF / 孤立 CR / NUL 走可读路径，不抛堆栈  |
    | 大文件    | 流式解析，不 read() 全量                               |
    | 触发入口  | 真实 subfinder + httpx 文件跑通最小入口                |

性能契约（10 万观测 < 60s）在 `bench/test_perf_ingest.py` —— 它属于 bench，
不该拖慢 `check.py` 的单元测试段。
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import sys
import types
import zipfile
from pathlib import Path

import pytest

from magpie.core import ingest, ops, pipeline, store
from magpie.core.store import connect, effective_equivalent, init_db, snapshot_effective

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = REPO_ROOT / "bench" / "e2e"
SUBFINDER = E2E_DIR / "recon" / "subfinder.txt"
HTTPX = E2E_DIR / "recon" / "httpx.jsonl"

DOMAINS = """\
app.example.com
api.example.com
dev-api.example.com
mail.example.com
example.com
"""

HTTPX_LINES = [
    {"port": "443", "url": "https://app.example.com", "input": "app.example.com",
     "title": "Acme Console", "scheme": "https", "webserver": "nginx",
     "content_type": "text/html", "host": "app.example.com", "status_code": 200,
     "content_length": 5213, "failed": False, "tech": ["Vue.js", "Nginx:1.24.0"],
     "cname": ["app.cdn.example.net"], "tls": {"version": "tls13"},
     "a": ["203.0.113.10"], "final_url": "https://app.example.com/"},
    {"port": "443", "url": "https://api.example.com", "input": "api.example.com",
     "title": "", "scheme": "https", "webserver": "nginx/1.24.0", "host": "api.example.com",
     "status_code": 401, "failed": False, "tech": ["Laravel 8.12.0"],
     "a": ["203.0.113.11"]},
]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


_SEQ = [0]


def make_project(conn: sqlite3.Connection, name: str | None = None) -> int:
    _SEQ[0] += 1
    return ops.create_project(conn, name or f"ingest-{_SEQ[0]}",
                              {"include": ["*.example.com"], "exclude": []})


def write(path: Path, text: str, encoding: str = "utf-8", newline: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as fh:
        fh.write(text)
    return path


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_httpx(path: Path, rows: list[dict]) -> Path:
    return write(path, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def opts(conn: sqlite3.Connection, project_id: int, tmp_path: Path, **kw) -> ingest.IngestOptions:
    kw.setdefault("archive", "external")     # 单测不做归档复制，快且不写多余文件
    return ingest.IngestOptions(project_id=project_id, data_dir=tmp_path / "data", **kw)


def obs_current(conn: sqlite3.Connection, project_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM observations WHERE project_id=?"
                            " AND is_current=1", (project_id,)).fetchone()[0])


def rel_current(conn: sqlite3.Connection, project_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM relations WHERE project_id=?"
                            " AND is_current=1", (project_id,)).fetchone()[0])


def assert_clean(conn: sqlite3.Connection) -> None:
    report = store.integrity_check(conn)
    assert report["ok"], f"不变量被破坏：{report}"


# ---------------------------------------------------------------------------
# 触发入口 · 真实文件
# ---------------------------------------------------------------------------


def test_real_subfinder_and_httpx_files_become_facts(db, tmp_path):
    """真实 subfinder + httpx 文件 → domain→ip、service→product 都出现。

    这是本步的最终目的：管子接好之后，**真的能吃进去一次**。
    """
    project_id = make_project(db)
    options = opts(db, project_id, tmp_path)
    assert SUBFINDER.is_file() and HTTPX.is_file(), "样例文件随 2.5 一起入库"
    report = ingest.ingest_paths(db, options, [SUBFINDER, HTTPX])

    assert report["totals"]["added"] > 0
    # 2.4 落地后这里从 `parsers_bridge` 变成了 `parsers` —— 那是**自动退役**
    # （`ingest.load_parsers` 只要在正式包里找到一个解析器就不再加载过渡实现），
    # 不是有人记得来改这一行。过渡实现仍留在仓库里且不可达，见 parsers_bridge.py 文件头。
    assert report["parser_source"] == "magpie.core.parsers"
    assert [f["state"] for f in report["files"]] == ["done", "partial"]

    counts = dict(db.execute(
        "SELECT e.kind, COUNT(*) FROM entities e WHERE e.project_id=? AND e.is_current=1"
        " GROUP BY e.kind", (project_id,)))
    assert counts.get("domain", 0) >= 12
    assert counts.get("ip", 0) >= 10
    assert counts.get("service", 0) >= 10
    assert counts.get("product", 0) >= 5

    verbs = dict(db.execute(
        "SELECT rel, COUNT(*) FROM relations WHERE project_id=? AND is_current=1"
        " GROUP BY rel", (project_id,)))
    assert verbs.get("resolves_to", 0) >= 10        # domain → ip
    assert verbs.get("runs", 0) >= 5                # service → product
    assert verbs.get("member_of", 0) >= 5           # 子域 → 父域

    # 报告与数据层同源：报告说的数必须能被 SQL 独立复算出来。
    assert report["db"]["observations"] == obs_current(db, project_id)
    assert report["db"]["relations"] == rel_current(db, project_id)
    assert_clean(db)


def test_report_carries_six_counts_and_reconciles(db, tmp_path):
    """报告必须含 6 个计数，且 added + updated + skipped == records。"""
    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)

    for name in ("added", "updated", "skipped", "conflicts", "bad_lines", "unknown_fields"):
        assert name in report["counts"], f"报告缺计数 {name}"
    assert report["reconcile"]["ok"], report["reconcile"]
    assert report["counts"]["records"] == (report["counts"]["added"]
                                           + report["counts"]["updated"]
                                           + report["counts"]["skipped"])
    # 报告里的仲裁必须说清"用了谁、多少分、为什么"
    assert report["arbitration"]["reason"]
    assert report["arbitration"]["adopted"]["tool"] == "domainlist"
    assert report["integrity"]["ok"]


# ---------------------------------------------------------------------------
# 幂等（硬性）
# ---------------------------------------------------------------------------


def test_second_import_of_same_file_is_skipped_and_changes_nothing(db, tmp_path):
    """同一文件导入两次：第二次 skipped，**观测数不变**（硬性标准）。"""
    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    http = write_httpx(tmp_path / "h.jsonl", HTTPX_LINES)
    options = opts(db, project_id, tmp_path)

    first = [ingest.ingest_file(db, options, p) for p in (path, http)]
    obs_after_first = obs_current(db, project_id)
    assert [f["state"] for f in first] == ["done", "done"]
    assert obs_after_first > 0

    second = [ingest.ingest_file(db, options, p) for p in (path, http)]
    assert [f["state"] for f in second] == ["skipped", "skipped"]
    assert [f["job_id"] for f in second] == [f["job_id"] for f in first]
    assert all(f["counts"]["records"] == 0 for f in second)
    assert obs_current(db, project_id) == obs_after_first       # 硬性：观测数不变
    assert rel_current(db, project_id) > 0
    assert_clean(db)


def test_undo_then_reimport_is_allowed_not_mistaken_for_duplicate(db, tmp_path):
    """被 undo 过的文件重新导入是正当的 reparse，不能算重复。"""
    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    options = opts(db, project_id, tmp_path)

    first = ingest.ingest_file(db, options, path)
    ops.undo(db, first["job_id"])
    assert obs_current(db, project_id) == 0

    again = ingest.ingest_file(db, options, path)
    assert again["state"] == "done"
    assert again["counts"]["added"] == first["counts"]["added"]
    assert_clean(db)


# ---------------------------------------------------------------------------
# --dry-run（反例断言：fact 表行数完全不变）
# ---------------------------------------------------------------------------


def test_dry_run_leaves_effective_state_identical(db, tmp_path):
    """干跑：跑完全相同的代码路径，只有 COMMIT 变成 ROLLBACK。"""
    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    ingest.ingest_file(db, opts(db, project_id, tmp_path), path)   # 先有一次真导入
    baseline = snapshot_effective(db)

    dry = ingest.ingest_file(db, opts(db, project_id, tmp_path, dry_run=True),
                             write(tmp_path / "d2.txt", DOMAINS + "www.example.com\n"))
    assert dry["dry_run"] is True
    assert dry["counts"]["added"] > 0            # 干跑**确实干了活**（否则断言没有意义）

    same, diff = effective_equivalent(db, baseline)
    assert same, f"干跑动了数据：{diff}"
    assert_clean(db)


def test_pipeline_dry_run_predicts_real_on_a_multi_step_flow(tmp_path):
    """多 step 的 flow：干跑算出的数与真跑**逐项相同**。

    为什么单独立一条：`pipeline run` 是**一条命令、多个 step**，而每个 step 各调
    一次 `ingest_paths`。事务边界要是落在"一次调用"上，第 2 个 step 的干跑就看不见
    第 1 个 step 刚写下的事实 —— 而真跑一定看得见（每个 step 收尾 COMMIT）。于是
    `--dry-run` 在**多 step 的 flow 上低报 `updated`**：那些"重述第 1 个 step 已有
    事实"的记录全被算成新增。干跑唯一的用处就是预测真跑，低报等于说假话。

    这条性质以前只有 `verify_v32r5.py` 盖着 —— 而它要起真子进程、要真实夹具，
    单元测试一条都没有，这正是它能溜进来的原因。
    """
    definition = {
        "_path": str(tmp_path / "pipeline.yaml"),
        "name": "多 step 干跑用例",
        "project": "干跑预演",
        "scope": {"include": ["*.example.com"], "exclude": []},
        # 两个来源**刻意重叠**（httpx.jsonl 里的域名 subfinder.txt 也有），所以
        # 第 2 个 step 一定会重述第 1 个 step 的事实。夹具不重叠时"两边相等"是空的
        # —— 一个退化成死分支的 updated 同样能让两边相等。
        "steps": [
            {"id": "dns", "parser": "domainlist", "source": "subfinder",
             "source_glob": "recon/subfinder.txt"},
            {"id": "probe", "source": "httpx", "source_glob": "recon/*httpx*.jsonl",
             "after": ["dns"]},
        ],
    }
    dry_db, real_db = connect(tmp_path / "dry.db"), connect(tmp_path / "real.db")
    init_db(dry_db)
    init_db(real_db)
    try:
        dry = pipeline.run_pipeline(dry_db, definition, base_dir=E2E_DIR,
                                    data_dir=tmp_path / "dry-data", dry_run=True)
        real = pipeline.run_pipeline(real_db, definition, base_dir=E2E_DIR,
                                     data_dir=tmp_path / "real-data")

        # 逐 step 比，再比聚合 —— 逐 step 才定位得到"是哪个 step 的可见性不对"。
        assert ({s["id"]: s["totals"] for s in dry["steps"]}
                == {s["id"]: s["totals"] for s in real["steps"]}), \
            f"逐 step 不等：dry={[s['totals'] for s in dry['steps']]} " \
            f"real={[s['totals'] for s in real['steps']]}"
        assert dry["totals"] == real["totals"], \
            f"聚合不等：dry={dry['totals']} real={real['totals']}"
        # 上面那个相等**不是空的**：这个夹具里第 2 个 step 真的重述了第 1 个的事实。
        assert real["totals"]["updated"] > 0, \
            "夹具没造出重述 —— 那上面的相等就证明不了『干跑看得见前一个 step』"
        # 干跑的硬性标准：跑完**一行都不留**；真跑那一侧反过来必须有事实。
        assert not any(snapshot_effective(dry_db).values()), "干跑留下了行"
        assert any(snapshot_effective(real_db).values()), "真跑一条事实都没落"
        assert_clean(real_db)
    finally:
        dry_db.close()
        real_db.close()


# ---------------------------------------------------------------------------
# 可撤销 / 精确复原
# ---------------------------------------------------------------------------


def test_undo_restores_and_redo_reproduces_exactly(db, tmp_path):
    """导入 → undo → 观测数回到导入前；再 redo → 精确复原。"""
    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    http = write_httpx(tmp_path / "h.jsonl", HTTPX_LINES)
    options = opts(db, project_id, tmp_path)

    before = snapshot_effective(db)
    jobs = [ingest.ingest_file(db, options, p)["job_id"] for p in (path, http)]
    after_import = snapshot_effective(db)
    assert obs_current(db, project_id) > 0 and rel_current(db, project_id) > 0

    for job_id in jobs:
        ops.undo(db, job_id)
    same, diff = effective_equivalent(db, before)
    assert same, f"undo 没有干净回滚：{diff}"
    assert obs_current(db, project_id) == 0
    assert rel_current(db, project_id) == 0

    for job_id in jobs:
        ops.redo(db, job_id)
    same, diff = effective_equivalent(db, after_import)
    assert same, f"redo 没有精确复原：{diff}"
    assert_clean(db)


def test_cancel_keeps_committed_part_undoable(db, tmp_path):
    """取消：已提交的部分保留、状态一致、仍然可 undo，不留不一致状态。"""
    project_id = make_project(db)
    rows = [dict(HTTPX_LINES[0], host=f"h{i}.example.com", input=f"h{i}.example.com",
                 url=f"https://h{i}.example.com", a=[f"203.0.113.{i % 250 + 1}"])
            for i in range(40)]
    path = write_httpx(tmp_path / "h.jsonl", rows)
    before = snapshot_effective(db)

    seen = [0]

    def cancel_check() -> bool:
        seen[0] += 1
        return seen[0] > 12                       # 第 12 次检查之后中断

    report = ingest.ingest_file(db, opts(db, project_id, tmp_path, batch_size=3,
                                         cancel_check=cancel_check), path)
    assert report["state"] == "cancelled"
    assert report["counts"]["records"] > 0        # 确实写进去了一部分
    assert obs_current(db, project_id) > 0
    assert report["integrity"]["ok"], report["integrity"]   # 中断后状态仍然一致

    job_id = report["job_id"]
    added = obs_current(db, project_id)
    ops.undo(db, job_id)
    assert obs_current(db, project_id) == 0
    assert added > 0
    same, diff = effective_equivalent(db, before)
    assert same, f"中断 + undo 之后没能回到初始状态：{diff}"
    assert_clean(db)


# ---------------------------------------------------------------------------
# 编码 / 二进制 / 坏行（全部走"可读报告"路径，不抛堆栈）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["gbk", "bom", "crlf", "lone-cr", "nul", "empty"])
def test_encodings_are_probed_and_reported(db, tmp_path, case):
    """GBK / BOM / CRLF / 孤立 CR / NUL / 空文件：全部可读，绝不抛堆栈。"""
    project_id = make_project(db)
    name = f"{case}.txt"
    path = tmp_path / name
    if case == "gbk":
        write(path, "# 中文注释：这是子域清单\napp.example.com\napi.example.com\n",
              encoding="gbk")
    elif case == "bom":
        write_bytes(path, b"\xef\xbb\xbf" + DOMAINS.encode("utf-8"))
    elif case == "crlf":
        write(path, DOMAINS, newline="\r\n")
    elif case == "lone-cr":
        write(path, DOMAINS, newline="\r")
    elif case == "nul":
        write_bytes(path, b"app.example.com\x00\napi.example.com\n")
    else:
        write(path, "")

    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)

    assert report["state"] != "failed", report.get("reason")
    assert "encoding" in report and report["encoding"]["encoding"]
    if case == "empty":
        assert report["encoding"]["notes"] == ["空文件"]
        assert report["counts"]["records"] == 0
        return

    # 除空文件外，每种编码都要**真的读出域名**，而不是"没崩就行"
    domains = {r[0] for r in db.execute(
        "SELECT alias_key FROM entity_alias WHERE project_id=? AND revoked_by_job_id IS NULL",
        (project_id,))}
    assert {"app.example.com", "api.example.com"} <= domains
    assert report["counts"]["added"] > 0
    assert_clean(db)


def test_gbk_is_detected_not_mangled(db, tmp_path):
    """GBK 文件必须按 gb18030 解出来（而不是 utf-8 + replace 出一堆乱码）。"""
    project_id = make_project(db)
    path = write(tmp_path / "gbk.txt",
                 "# 中文注释：这是子域清单\napp.example.com\n", encoding="gbk")
    probe = ingest.probe_file(path)
    assert probe.encoding == "gb18030"
    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    assert report["encoding"]["encoding"] == "gb18030"
    assert any("gb18030" in note for note in report["encoding"]["notes"])


def test_zip_is_archived_but_not_parsed(db, tmp_path):
    """zip：识别为二进制 → 跳过并给可读理由，不是 Python 堆栈。"""
    project_id = make_project(db)
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", DOMAINS)

    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    assert report["state"] == "skipped"
    assert report["encoding"]["binary"] == "zip"
    assert "二进制" in report["skip_reason"]
    assert report["counts"]["records"] == 0
    assert int(db.execute("SELECT COUNT(*) FROM raw_files").fetchone()[0]) == 0
    assert_clean(db)


def test_bad_lines_and_unknown_fields_are_reported(db, tmp_path):
    """坏行明细落 `<data-dir>/reports/<job_id>.badlines.txt`；未知字段是漂移信号。"""
    project_id = make_project(db)
    rows = list(HTTPX_LINES)
    rows.append({"host": "broken.example.com", "tech": ["nginx"],
                 "cname_chain": "a,b,c"})                   # 未知字段（§5.6 的原文例子）
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    text += '\n{"host": "trunc.example.com", "port": "443"'   # 截断的 JSONL
    path = write(tmp_path / "h.jsonl", text)

    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    assert report["counts"]["bad_lines"] == 1
    assert report["unknown_fields_detail"] == {"cname_chain": 1}

    badlines = Path(report["badlines_report"])
    assert badlines.is_file()
    body = badlines.read_text(encoding="utf-8")
    assert "trunc.example.com" in body
    assert f"job {report['job_id']}" in body
    assert_clean(db)


# ---------------------------------------------------------------------------
# 嗅探仲裁（§5.4：最高分 ≥ 0.8 且与第二名差 ≥ 0.15）
# ---------------------------------------------------------------------------


class _FakeParser:
    """只用于仲裁单测的假解析器（不需要真解析）。"""

    def __init__(self, tool: str, score: float) -> None:
        self.tool = tool
        self.format_version = "1"
        self._score = score
        self.parse = lambda lines, ctx: iter(())

    def sniff(self, sample, filename):                      # noqa: ANN001
        return self._score


@pytest.mark.parametrize("scores,adopted", [
    ([0.90], True),                     # 只有一个候选，差距 = 0.90
    ([0.80], True),                     # 边界：恰好等于 0.8
    ([0.79], False),                    # 边界下：低于 0.8
    ([0.90, 0.75], True),               # 差距 0.15 恰好达标
    ([0.90, 0.76], False),              # 差距 0.14 不达标
    ([0.85, 0.84], False),              # 两个都高但分不开 → 不许猜
    ([], False),                        # 没有可用解析器
])
def test_arbitration_thresholds(scores, adopted):
    ranked = ingest.sniff_rank("", "x", [_FakeParser(f"t{i}", s) for i, s in enumerate(scores)])
    result = ingest.arbitrate(ranked)
    assert (result["adopted"] is not None) is adopted, result["reason"]
    assert result["reason"]


def test_ambiguous_input_goes_to_inbox_for_confirmation(db, tmp_path, monkeypatch):
    """分不开时**不猜**：不导入任何东西，进 Inbox 待人工确认。"""
    project_id = make_project(db)
    path = write(tmp_path / "ambiguous.txt", DOMAINS)
    monkeypatch.setattr(ingest, "load_parsers",
                        lambda: ([_FakeParser("a", 0.82), _FakeParser("b", 0.80)], "test"))

    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    assert report["state"] == "needs_confirmation"
    assert report["counts"]["records"] == 0
    assert obs_current(db, project_id) == 0

    row = db.execute("SELECT kind, state, severity, payload_json, created_by_job_id"
                     " FROM inbox_items").fetchone()
    assert row is not None and row["kind"] == "manual_required" and row["state"] == "open"
    assert row["created_by_job_id"] == report["job_id"]
    assert "ranking" in json.loads(row["payload_json"])
    assert_clean(db)


def test_sniff_picks_the_right_parser_on_real_files():
    """真实文件上，仲裁结果必须就是那个解析器（不是"碰巧能跑"）。"""
    parsers, _ = ingest.load_parsers()
    assert {p.tool for p in parsers} >= {"domainlist", "httpx"}

    txt = SUBFINDER.read_text(encoding="utf-8")[:4096]
    best = ingest.arbitrate(ingest.sniff_rank(txt, "subfinder.txt", parsers))
    assert best["adopted"]["tool"] == "domainlist"
    assert best["margin"] >= ingest.SNIFF_ADOPT_MARGIN

    js = HTTPX.read_text(encoding="utf-8")[:4096]
    best = ingest.arbitrate(ingest.sniff_rank(js, "httpx.jsonl", parsers))
    assert best["adopted"]["tool"] == "httpx"
    assert best["margin"] >= ingest.SNIFF_ADOPT_MARGIN


# ---------------------------------------------------------------------------
# 人工保护（R3 回归）
# ---------------------------------------------------------------------------


def test_import_never_touches_manual_ownership(db, tmp_path):
    """导入只写 `tool` 归属；人工保护集合逐列不变（R3）。"""
    from magpie.core import manual

    project_id = make_project(db)
    path = write(tmp_path / "d.txt", DOMAINS)
    first = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)

    entity_id = store.entity_for_key(db, project_id, "app.example.com")
    writer = manual.ManualWriter(db)
    writer.set_keys(project_id=project_id, subject_kind="entity", subject_id=entity_id,
                    add=["display_name"])
    before = [tuple(r) for r in db.execute(
        "SELECT subject_kind, subject_id, keys_json, version FROM manual_fields")]

    again = ingest.ingest_file(db, opts(db, project_id, tmp_path),
                               write(tmp_path / "d2.txt", DOMAINS + "new.example.com\n"))
    assert again["counts"]["added"] > 0

    after = [tuple(r) for r in db.execute(
        "SELECT subject_kind, subject_id, keys_json, version FROM manual_fields")]
    assert after == before, "导入动了人工作业层"
    assert store.manual_owned(db, "entity", entity_id) is True

    authored = {r[0] for r in db.execute(
        "SELECT DISTINCT authorship FROM contributions WHERE job_id=?", (again["job_id"],))}
    assert authored == {"tool"}, "导入写出了非 tool 归属的贡献"
    assert first["job_id"] != again["job_id"]
    assert_clean(db)


# ---------------------------------------------------------------------------
# 大文件：流式（不 read() 全量）
# ---------------------------------------------------------------------------


def test_parsing_never_reads_the_whole_file_at_once(db, tmp_path, monkeypatch):
    """反例断言：整条管线里不允许出现**无参数**的 `f.read()`。

    `read()`（无参）= 把文件整个吞进内存。流式解析的验收标准只有这一条能被
    自动验证 —— 内存曲线在单测里没法稳定地量，但"有没有全量读"可以。
    """
    project_id = make_project(db)
    rows = [dict(HTTPX_LINES[0], host=f"h{i}.example.com", input=f"h{i}.example.com",
                 url=f"https://h{i}.example.com") for i in range(2000)]
    path = write_httpx(tmp_path / "big.jsonl", rows)
    assert path.stat().st_size > ingest.SAMPLE_BYTES      # 比探针样本大，否则测不出东西

    real_open = builtins.open
    offenders: list[str] = []

    class _Spy:
        def __init__(self, fh):                            # noqa: D107
            self._fh = fh

        def read(self, size=-1):                           # noqa: D102
            if size is None or size < 0:
                offenders.append(str(getattr(self._fh, "name", "?")))
            return self._fh.read(size)

        def __iter__(self):                                # noqa: D105
            return iter(self._fh)

        def __getattr__(self, name):                       # noqa: D105
            return getattr(self._fh, name)

        def __enter__(self):                               # noqa: D105
            return self

        def __exit__(self, *exc):                          # noqa: D105
            return self._fh.__exit__(*exc)

    def spy_open(file, mode="r", *args, **kw):
        handle = real_open(file, mode, *args, **kw)
        if mode in ("rb", "r") and str(file).endswith(".jsonl"):
            return _Spy(handle)
        return handle

    monkeypatch.setattr(builtins, "open", spy_open)
    report = ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    monkeypatch.undo()

    assert report["counts"]["added"] > 0
    assert offenders == [], f"这些地方全量读了文件：{offenders}"


def test_sniff_sample_is_bounded(db, tmp_path, monkeypatch):
    """嗅探样本有上界：文件再大，解析器拿到的样本也就 SAMPLE_BYTES。"""
    project_id = make_project(db)
    seen: list[int] = []
    parsers, _ = ingest.load_parsers()
    for cls in parsers:
        original = cls.sniff.__func__ if hasattr(cls.sniff, "__func__") else cls.sniff

        def wrapper(sample, filename, _orig=original):      # noqa: ANN001
            seen.append(len(sample))
            return _orig(sample, filename)

        monkeypatch.setattr(cls, "sniff", staticmethod(wrapper))

    path = write_httpx(tmp_path / "big.jsonl",
                       [dict(HTTPX_LINES[0], host=f"h{i}.example.com",
                             input=f"h{i}.example.com") for i in range(3000)])
    ingest.ingest_file(db, opts(db, project_id, tmp_path), path)
    assert seen and max(seen) <= ingest.SAMPLE_BYTES * 2     # 样本按字符计，留一点余量


# ---------------------------------------------------------------------------
# 大集合的增量重算（首次大文件导入踩到的真实缺陷）
# ---------------------------------------------------------------------------


def test_delta_recompute_survives_more_subjects_than_sqlite_bind_limit(db, tmp_path,
                                                                       monkeypatch):
    """一次 delta 覆盖的 subject 数超过 SQLite 变量上限时也不能炸。

    这不是假想：2.5 第一次真跑 10 万观测的导入时，`recompute_subset` 的重置
    UPDATE 分了块、聚合查询却没分块，直接 `too many SQL variables`。
    这里把 batch 与连接上限都调小来复现同一形状（不真造 3 万行，跑得快）。
    """
    project_id = make_project(db)
    rows = [dict(HTTPX_LINES[0], host=f"h{i}.example.com", input=f"h{i}.example.com",
                 url=f"https://h{i}.example.com") for i in range(600)]
    ingest.ingest_file(db, opts(db, project_id, tmp_path), write_httpx(tmp_path / "h.jsonl", rows))

    subjects = [("observation", int(r[0]))
                for r in db.execute("SELECT id FROM observations")]
    assert len(subjects) > 400

    monkeypatch.setattr(store, "_BATCH", 100)
    original = db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 200)   # 远小于 subject 数
        store.recompute(db, delta=subjects)                      # 修复前：OperationalError
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original)
    assert_clean(db)


# ---------------------------------------------------------------------------
# 与 2.4 的接缝：过渡解析器自动退役
# ---------------------------------------------------------------------------


def test_bridge_parsers_retire_once_the_official_package_exists(monkeypatch):
    """`magpie.core.parsers` 一旦可用，`parsers_bridge` 就不再被加载。"""
    fake = types.ModuleType("magpie.core.parsers")

    class OfficialParser:
        tool = "official"
        format_version = "1"

        @classmethod
        def sniff(cls, sample, filename):                   # noqa: ANN001
            return 1.0

        def parse(self, lines, ctx):                        # noqa: ANN001
            return iter(())

    OfficialParser.__module__ = "magpie.core.parsers"
    fake.OfficialParser = OfficialParser
    monkeypatch.setitem(sys.modules, "magpie.core.parsers", fake)

    parsers, source = ingest.load_parsers()
    assert source == "magpie.core.parsers"
    assert [p.tool for p in parsers] == ["official"]
