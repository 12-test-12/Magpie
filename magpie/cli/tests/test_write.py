"""CLI 写路径的验收网（2.6）：退出码 / 干跑同构 / 负向输入 / 可消费性。

这个文件与 `test_contract.py` 的分工：那边管**信封的形状**（每个命令至少一条
成功 + 一条失败），这边管**写命令才有的那几件事** —— 它们每一条都有一个
"看起来通过了、其实什么都没验到"的失败形态：

  * **退出码全表**：0/1/2/3/4/5 各要有一个用例，尤其 `3`（需人工裁决）与
    `4`（锁冲突）。这不是覆盖率洁癖：那两档正是"agent 该换个动作"的信号，
    而它们最容易被顺手写成"反正也是失败"的一档。`4` 更是只能真造一把锁才
    验得到 —— 它没有别的触发方式。
  * **干跑同构**：`--dry-run` 与真跑的报告必须**逐字段相同**（只有 `dry_run`
    之差），而且库要一行不动。只断言"干跑退出码 0"的测试，在干跑其实是空转
    时也全绿。
  * **负向输入**：空文件 / GBK / 损坏 JSON / zip → 不崩 + 可读提示 + 正确退出码。
    判据是 stdout 仍然是**合法 JSON**（崩掉的进程打的是 traceback，退出码一样
    是 1）—— 与 `verify_v32r5.py` 的 C 段同一条理由。
  * **可消费性**：真的架一条管道，而不是在测试进程里 `json.loads(stdout)`。

另外两条**只在这里**能做的断言：

  * `undo` → `redo` 之后**逐行指纹**回到原样（不是只比条数）。指纹建在
    `store.snapshot_effective` 上 —— 与 `verify_v32r5.py` 同一条口径，同一个
    函数。条数相同、内容不同是完全可能的，而那正是要抓的情形。
  * 写命令**不读 stdin**（与 2.2 的非交互纪律同一条），且 `--json` 是纯 ASCII。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from conftest import ENV                                              # noqa: E402
from magpie.core import migrate, store                                # noqa: E402

RECON = ROOT / "bench" / "e2e" / "recon"
SUB = RECON / "subfinder.txt"
HTTPX = RECON / "httpx.jsonl"


# ---------------------------------------------------------------------------
# 跑 CLI 与读信封
# ---------------------------------------------------------------------------


def cli(*args: str, env: dict | None = None, stdin=None) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, "-m", "magpie.cli", *args],
                          cwd=str(ROOT), capture_output=True, env=env or ENV,
                          stdin=stdin)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def envelope(*args: str) -> tuple[int, dict]:
    code, out, err = cli(*args)
    assert out.strip(), f"stdout 为空（退出码 {code}）；stderr={err[:400]}"
    try:
        return code, json.loads(out)
    except ValueError:
        pytest.fail(f"stdout 不是合法 JSON（退出码 {code}）—— 进程多半崩了："
                    f"{(out + err)[-400:]}")


def ok_data(*args: str) -> dict:
    code, payload = envelope(*args)
    assert payload["ok"] is True, payload
    assert code == 0, f"信封说成功，退出码却是 {code}：{payload}"
    return payload["data"]


def err_of(*args: str) -> tuple[int, dict]:
    code, payload = envelope(*args)
    assert payload["ok"] is False, payload
    assert "meta" not in payload and "data" not in payload, payload
    assert payload["error"]["message"], payload
    return code, payload["error"]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def work(tmp_path) -> dict:
    """一个建好 schema、建好项目、**一条事实都没有**的数据区。

    刻意不用 `conftest.loaded_db`：那条夹具是"别人的入口写出来的库"，适合
    验读命令；写路径要验的是**从零开始**（`init` → `project create` → `import`），
    用现成的数据反而会把"起步"那一段跳过 —— 而"文件 → 库 → 命令行这条链路
    第一次完整闭合"恰恰就是这个任务本身。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "magpie.db"
    code, payload = envelope("init", "--db", str(db), "--json")
    assert code == 0 and payload["ok"], payload
    return {"dir": data_dir, "db": db,
            "base": ("--data-dir", str(data_dir), "--db", str(db))}


@pytest.fixture()
def project(work) -> dict:
    """在 `work` 上再建一个项目，并把它的 id 带上。"""
    data = ok_data("project", "create", "demo", "--include", "*.example.com",
                   *work["base"], "--json")
    return {**work, "project": data["id"], "proj": ("--project", str(data["id"]))}


def fingerprint(conn) -> dict:
    """**有效状态**的逐行指纹（与 `verify_v32r5.py` 的 `fingerprint()` 同源）。

    用 `store.snapshot_effective()` 而不是全表：`undo` / `redo` **本来就要**
    改 `jobs.applied` 与 `activity_log`（那是审计与生命周期，不是事实）。
    拿全表比会得到一条永远红的断言，而一条永远红的断言最后一定会被加个
    skip 关掉。这里要的是"事实层恰好回到原样"。
    """
    import hashlib
    out = {}
    for table, rows in store.snapshot_effective(conn).items():
        blob = json.dumps(rows, sort_keys=True, default=str)
        out[table] = {"rows": len(rows),
                      "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()}
    return out


def effective(db: Path) -> dict:
    conn = store.connect(db)
    try:
        return fingerprint(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# project create
# ---------------------------------------------------------------------------


def test_project_create_is_the_first_thing_on_an_empty_db(work):
    """空库 → `project create` → 项目真的在（这是整条链路的起点）。

    `init` 之后一个项目都没有，而**没有项目就没有地方导入** —— 少了这条命令，
    「文件 → 库 → 命令行」在空库上起不了步。这一条钉的就是那个起点。
    """
    assert ok_data("schema", *work["base"], "--json")["query_syntax"]  # 空库可读
    data = ok_data("project", "create", "demo", "--include", "*.example.com",
                   "--exclude", "*.internal", *work["base"], "--json")
    assert data["id"] == 1 and data["name"] == "demo"
    assert data["scope"]["include"] == ["*.example.com"]
    assert data["scope"]["exclude"] == ["*.internal"]
    assert data["scope_version"] == 1 and data["scope_hash"]

    # `ls` 现在能用了 —— 这是"建项目"真的生效了的独立佐证（不是读回自己的输出）
    listed = ok_data("ls", *work["base"], "--json")
    assert listed["count"] == 0 and listed["entities"] == []


def test_project_create_duplicate_name_is_exit_3(project):
    """重名 → **退出码 3**（需人工裁决 ↔ 409），与 `POST /projects` 同义。

    这是写路径上最干净的一个"需人工裁决"用例：重名不是数据坏了（2）也不是
    参数写错了（1）—— 它要的是一个人决定"换个名字，还是就用那一个"。
    """
    code, error = err_of("project", "create", "demo", *project["base"], "--json")
    assert code == 3, error
    assert error["code"] == "E_PROJECT_EXISTS"
    assert error["name"] == "demo"
    # 库里仍然只有一个 demo —— 失败的那次**没有**留下半个项目。
    # `schema_version` 取 `migrate.MIGRATIONS` 的长度而不是写死一个数：
    # 3.1 新增 `004_kind_cidr.sql` 时，这条与业务无关的断言红了，而它红的样子
    # 跟真的重名回归一模一样 —— 一个会随迁移数变红的常量，只会训练人改数字。
    projects = ok_data("schema", *project["base"], "--json")
    assert projects["schema_version"] == len(migrate.MIGRATIONS)


def test_project_create_rejects_an_unknown_scope_key(work):
    """scope 里多一个键 → 拒绝，而不是存进去一个**永远不生效**的 scope。

    "存进去了、看起来也对、但不约束任何东西"是这一档最坏的形态：它没有任何
    症状，直到某天有人发现一批本该被排除的资产一直在库里。
    """
    code, error = err_of("project", "create", "x", "--scope",
                         '{"include": ["*.a"], "incldue": ["*.b"]}',
                         *work["base"], "--json")
    assert code == 1 and error["code"] == "E_BAD_SCOPE"
    assert error["rejected"] == ["incldue"]
    # 反例的另一半：合法的 scope 必须能过（否则上面那条只是"永远报错"）
    assert ok_data("project", "create", "y", "--scope",
                   '{"include": ["*.a"], "exclude": []}',
                   *work["base"], "--json")["scope"]["include"] == ["*.a"]


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def _imported(project, *paths) -> tuple[int, dict]:
    return envelope("import", *[str(p) for p in paths], *project["proj"],
                    *project["base"], "--json")


def test_import_closes_the_file_to_database_to_command_line_loop(project):
    """真实 subfinder + httpx 输出 → 事实 → `ls` 看得见 → `diff --since last`。

    这是本任务那句"垂直切片在此闭合"的最小可执行形式。**判据取的是别的命令
    的输出**（`ls` / `diff`），不是 `import` 自己的报告 —— 一份自己说成功的
    报告，在它其实一行没写时也照样说自己成功。
    """
    code, payload = _imported(project, SUB, HTTPX)
    assert code == 0, payload
    data = payload["data"]
    assert data["state"] == "done"
    assert [f["state"] for f in data["report"]["files"]] == ["done", "partial"]
    job_ids = data["report"]["job_ids"]
    assert len(job_ids) == 2, job_ids            # 每个文件一个作业（undo 的粒度）

    listed = ok_data("ls", *project["proj"], *project["base"], "--json")
    assert listed["count"] > 0, listed
    keys = [e["entity_key"] for e in listed["entities"]]
    assert len(keys) == len(set(keys)), f"ls 里出现了重复 key：{keys}"

    diffed = ok_data("diff", "--since", "last", *project["proj"],
                     *project["base"], "--json")
    assert diffed["counts"]["new"] > 0 and diffed["counts"]["facts"] > 0, diffed


def test_import_is_idempotent_and_reports_the_skip(project):
    """同一个文件再导一次：`added=0`，文件报 `skipped`，退出码仍然是 0。

    `skipped` 在这里是**成功**（事实已经在库里，一行未写），所以它绝不能
    被归进失败档 —— 否则每一个把它当成"重复导入"用的脚本都会开始报错。
    """
    assert _imported(project, SUB)[0] == 0
    before = effective(project["db"])
    code, payload = _imported(project, SUB)
    assert code == 0, payload
    files = payload["data"]["report"]["files"]
    assert [f["state"] for f in files] == ["skipped"], files
    assert files[0]["job_id"], "幂等跳过要指向**原来那个**作业，否则无法追溯"
    assert payload["data"]["report"]["totals"].get("added", 0) == 0
    assert effective(project["db"]) == before, "幂等跳过不该留下任何痕迹"


def test_import_dry_run_is_isomorphic_with_the_real_run_and_writes_nothing(project):
    """干跑与真跑的报告**逐字段相同**（只有 `dry_run` 之差），且库一行不动。

    只断言"干跑退出码 0"是不够的：一个空转的干跑也退出 0。这里比两件事：

      ① **预测力** —— 空库上干跑算出的 totals 与真跑算出的必须逐项相同
         （`verify_v32r5.py` 的 A2e 为同一条理由做过同一件事）。干跑走的是
         同一条代码路径，只是不 COMMIT，所以它本来就算得出来。
      ② **同构** —— 在**同一个库状态**上，干跑与真跑的报告必须一模一样
         （只有 `dry_run` 那个布尔之差，而它正是 COMMIT→ROLLBACK 在报告里的
         可观察形式）。比"同一状态"而不是"先干后真"：后者拿一个 all-skipped
         的报告去比一个 all-done 的，差的是**幂等**，不是干跑。
    """
    before = effective(project["db"])

    # ① 干跑先来（库还是空的），随后真跑 —— 两者的计数必须一致
    code, dry = envelope("import", str(SUB), str(HTTPX), "--dry-run",
                         *project["proj"], *project["base"], "--json")
    assert code == 0, dry
    assert dry["data"]["dry_run"] is True
    assert effective(project["db"]) == before, "干跑动了库"
    code, real = _imported(project, SUB, HTTPX)
    assert code == 0, real
    assert dry["data"]["report"]["totals"] == real["data"]["report"]["totals"], \
        "干跑没在预演真跑（totals 对不上）"

    # ② 同一个库状态上再各来一次（这次两边都是 all-skipped）
    code, dry2 = envelope("import", str(SUB), str(HTTPX), "--dry-run",
                          *project["proj"], *project["base"], "--json")
    code2, real2 = _imported(project, SUB, HTTPX)
    assert code == 0 and code2 == 0
    dry_report = dict(dry2["data"]["report"])
    real_report = dict(real2["data"]["report"])
    assert dry_report.pop("dry_run") is True
    assert real_report.pop("dry_run") is False
    assert dry_report == real_report, "干跑与真跑的报告不同构"


def test_import_negative_inputs_do_not_crash_and_exit_meaningfully(project, tmp_path):
    """空文件 / GBK / 损坏 JSON / zip / 不存在的路径 → 不崩 + 可读提示 + 退出码。

    退出码的裁定（见 `write._IMPORT_VERDICTS`）：空文件与损坏 JSON 都仲裁不出
    解析器、已进 Inbox 等人工裁决 → **3**；zip 是二进制，按设计不进解析 →
    **2**；路径不存在 → **2**；GBK 是**能读的**（gb18030 解码）→ **0**。

    "不崩"的判据是 stdout 仍然是合法 JSON（`envelope` 里已经断言），不是
    退出码 —— 崩掉的进程退出码同样是 1，但打的是 traceback。
    """
    cases = {
        "empty.txt": (b"", 3, "E_PARSER_AMBIGUOUS"),
        "gbk.txt": ("app.example.com\n测试.example.com\n".encode("gbk"), 0, None),
        "broken.json": (b'{"a": 1, "b": [}\n{"c": ', 3, "E_PARSER_AMBIGUOUS"),
        "bin.zip": (b"PK\x03\x04" + bytes(range(256)) * 8, 2, "E_IMPORT_BINARY"),
    }
    for name, (blob, want_code, want_error) in cases.items():
        path = tmp_path / name
        path.write_bytes(blob)
        code, payload = envelope("import", str(path), *project["proj"],
                                 *project["base"], "--json")
        if want_error is None:
            assert payload["ok"] is True and code == 0, (name, payload)
            continue
        assert code == want_code, (name, code, payload)
        assert payload["error"]["code"] == want_error, (name, payload)
        # 可读提示：`hint` 说该怎么办，`message` 说哪里不对
        assert payload["error"]["hint"], (name, payload)
        assert name in payload["error"]["message"], (name, payload)

    # 路径不存在 —— **静默成功**是最坏的一种结果（会被读成"那个目录里本来
    # 就没东西"），所以它必须响亮。
    code, payload = envelope("import", str(tmp_path / "nope.txt"), *project["proj"],
                             *project["base"], "--json")
    assert code == 2 and payload["error"]["code"] == "E_IMPORT_PATH", payload
    assert payload["error"]["problems"], payload


def test_import_of_a_directory_with_nothing_to_import_says_so(project, tmp_path):
    """给了一个一个可导入文件都没有的目录：**退出 0，但报告里说清楚**。

    "0 个文件"是合法结果（一个此刻为空的目录就是这个），所以它不是错误；
    但它也绝不能**安静** —— 脚本拿着退出 0 往下走时，"导了 0 个"与"导了一批"
    的唯一区别就在报告里这一条。目录按扩展名过滤这件事尤其要说：一个
    `notes.csv.bak` 不会被认领，而它只报"0 个文件"的话，人会去找很久。
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "notes.csv.bak").write_text("a,b\n", encoding="utf-8")

    code, payload = envelope("import", str(empty_dir), *project["proj"],
                             *project["base"], "--json")
    assert code == 0 and payload["ok"] is True, payload
    data = payload["data"]
    assert data["report"]["files"] == [] and data["state"] == "done"
    assert data["notes"] and "扩展名" in data["notes"][0], data["notes"]


def test_import_failure_still_hands_back_the_full_report(project, tmp_path):
    """失败的导入**仍然**把报告交出去（挂在 `error.report` 上）。

    报告是导入唯一的交付通道，而"3 个文件里 2 个坏了"的全部细节只能从它读出来。
    只回一句错误码会让调用方为了看细节而重跑一次 —— 而重跑一次导入是有副作用的。
    """
    broken = tmp_path / "broken.json"
    broken.write_bytes(b'{"a": 1, "b": [}')
    code, payload = envelope("import", str(broken), str(SUB), *project["proj"],
                             *project["base"], "--json")
    assert code == 3, payload
    summary = payload["error"]["report"]
    states = {Path(f["path"]).name: f["state"] for f in summary["files"]}
    assert states["broken.json"] == "needs_confirmation", states
    assert states["subfinder.txt"] == "done", states       # 好的那个照旧导进去了
    assert summary["job_ids"], "报告里要给出真的落了库的那些作业"
    assert payload["error"]["candidates"], "待裁决的必须带上候选排名"


def test_import_reports_every_problem_in_the_batch(project, tmp_path):
    """一批里同时有多种毛病时，退出码只报**最需要人的**那一档，但提示要说全。

    只讲一档的话，"它报了 3，所以其余的都还好"会变成一个读得通但错的结论 ——
    而那个结论正是调用方会据以继续往下走的东西。
    """
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    blob = tmp_path / "bin.zip"
    blob.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 8)

    code, payload = envelope("import", str(empty), str(blob), str(tmp_path / "nope.txt"),
                             *project["proj"], *project["base"], "--json")
    assert code == 3, payload                       # 待人工裁决优先于数据错
    error = payload["error"]
    assert error["code"] == "E_PARSER_AMBIGUOUS"
    # 逐文件状态一个不少：好的那个、坏的两个、路径没展开的那个
    states = {Path(f["path"]).name: f["state"] for f in error["report"]["files"]}
    assert states["empty.txt"] == "needs_confirmation", states
    assert states["bin.zip"] == "skipped", states   # 二进制：也是 skipped，但是拒绝
    assert error["report"]["problems"], error["report"]
    # 而提示必须把"还有别的"说出来
    assert "bin.zip" in error["hint"] and "还有" in error["hint"], error["hint"]
    # 二进制单独出现时是**它自己那一档**（2 ↔ 404），不是被顺着报成 3
    code, payload = envelope("import", str(blob), *project["proj"],
                             *project["base"], "--json")
    assert code == 2 and payload["error"]["code"] == "E_IMPORT_BINARY", payload


# ---------------------------------------------------------------------------
# undo / redo —— 精确互逆
# ---------------------------------------------------------------------------


def test_undo_then_redo_restores_line_by_line(project):
    """`undo` 干净回滚 → `redo` **逐行**精确复原（不是只比条数）。

    指纹建在 `store.snapshot_effective` 上 —— 与 `verify_v32r5.py` 的 B 段
    同一个函数、同一条口径。这里**再验一遍**不是重复：那边走的是 2.5 的最小
    入口，这边走 CLI，两条路要给出同一个答案；而"只比条数"会漏掉的情形
    （行数不动、某一列变了）正是 §2.4 那两条红线要抓的。
    """
    _imported(project, SUB, HTTPX)
    conn = store.connect(project["db"])
    try:
        baseline = fingerprint(conn)
        job_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM jobs ORDER BY id")]
    finally:
        conn.close()
    assert baseline["entities"]["rows"] > 0 and baseline["relations"]["rows"] > 0

    for job_id in job_ids:
        data = ok_data("undo", str(job_id), *project["base"], "--json")
        assert data["op"] == "undo" and data["report"]["integrity"]["ok"]
    undone = effective(project["db"])
    assert all(cell["rows"] == 0 for cell in undone.values()), \
        f"undo 之后仍有活跃行：{ {k: v['rows'] for k, v in undone.items()} }"

    for job_id in job_ids:
        data = ok_data("redo", str(job_id), *project["base"], "--json")
        assert data["report"]["applied"] == 1, data
    assert effective(project["db"]) == baseline, "redo 没有精确复原"


def test_undo_redo_dry_run_matches_the_real_thing_and_writes_nothing(project):
    """`undo --dry-run`：报告与真跑同构，库一行不动。

    这也是"把数据层已有的 `--dry-run` 直接透出去"那句交付标准的可执行形式 ——
    `ops.undo(dry_run=True)` 是在事务里真的 INSERT/UPDATE 之后再 ROLLBACK，
    所以它的报告本来就与真跑一样；CLI 若自己另写一个"预览"，这里立刻会红。
    """
    _imported(project, SUB)
    before = effective(project["db"])

    code, dry_payload = envelope("undo", "1", "--dry-run", *project["base"], "--json")
    assert code == 0, dry_payload
    assert dry_payload["data"]["dry_run"] is True
    assert effective(project["db"]) == before, "干跑把库改了"

    real = ok_data("undo", "1", *project["base"], "--json")
    dry = dict(dry_payload["data"])
    real = dict(real)
    assert dry["job_id"] == real["job_id"] and dry["op"] == real["op"]
    assert dry.pop("dry_run") is True and real.pop("dry_run") is False
    assert dry["report"].pop("dry_run") is True
    assert real["report"].pop("dry_run") is False
    assert dry == real, "干跑与真跑的报告不同构"
    assert effective(project["db"]) != before, "真跑必须真的改了库（否则上面那条没意义）"

    # redo 也走同一条路：先真跑一次复原，再干跑一次看它是否同构
    ok_data("redo", "1", *project["base"], "--json")
    code, dry_redo = envelope("redo", "1", "--dry-run", *project["base"], "--json")
    assert code == 0 and dry_redo["data"]["dry_run"] is True
    assert dry_redo["data"]["report"]["noop"] is True, \
        "已经生效的作业再 redo 是幂等的（noop），干跑照样要看得到这一点"
    assert effective(project["db"]) == before, "这几次干跑里有哪次动了库"


def test_redo_is_idempotent_after_undo(project):
    """`redo` 一个从未被撤销的作业 → 幂等的 `noop`，退出码 0（↔ HTTP 200）。

    幂等不是"错误"：脚本可以无脑重放 undo/redo 而不必先查状态。把它做成
    错误码，调用方就得自己维护一份状态机 —— 而那份状态机迟早与库分叉。
    """
    _imported(project, SUB)
    report = ok_data("redo", "1", *project["base"], "--json")["report"]
    assert report["noop"] is True and report["integrity"]["ok"]
    # noop 分支刻意**不带** `applied` 键（没有什么被改过，也就无所谓"应用了没"）；
    # 库里那一列的真相照旧读得到
    assert "applied" not in report
    job = ok_data("jobs", "1", *project["base"], "--json")["job"]
    assert job["applied"] is True


def test_undo_rejects_a_bad_or_missing_job(project):
    """`<job>` 不是一个正整数 → 用法错（1）；库里的作业不存在 → 数据错（2）。"""
    code, error = err_of("undo", "abc", *project["base"], "--json")
    assert code == 1 and error["code"] == "E_BAD_JOB_ID"
    code, error = err_of("redo", "-1", *project["base"], "--json")
    assert code == 1 and error["code"] == "E_BAD_JOB_ID"
    code, error = err_of("undo", "99999", *project["base"], "--json")
    assert code == 2 and error["code"] == "E_JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def test_jobs_lists_history_with_explain_job_scope(project):
    """`magpie jobs`：作业历史，**每条都带 `explain_job_scope` 的输出**。

    计划书把"含 `explain_job_scope` 的输出"写成本命令的交付项，理由很实在：
    历史作业必须用**当时的** scope 解释。用当前 scope 去解释老作业，会得出
    "这条覆盖已经失效"的假结论 —— 而那个结论看起来完全说得通。
    """
    _imported(project, SUB, HTTPX)
    data = ok_data("jobs", *project["proj"], *project["base"], "--json")
    assert data["count"] == 2 and len(data["jobs"]) == 2
    for job in data["jobs"]:
        scope = job["scope"]
        assert scope["job_id"] == job["id"]
        assert scope["interpret_with"] in ("current_scope", "historical_scope")
        assert "coverage_usable_for_inactive" in scope
        # `can_undo` / `can_redo` 必须严格互补 —— 它们由 `applied` 一处推出
        assert job["can_undo"] is not job["can_redo"]
        assert job["counts"]["contributions"] > 0

    detail = ok_data("jobs", str(data["jobs"][0]["id"]), *project["base"], "--json")
    assert detail["job"]["id"] == data["jobs"][0]["id"]
    assert detail["scope"]["job_id"] == detail["job"]["id"]
    assert detail["activity"], "详情里要带活动日志（谁在什么时候动了它）"


def test_jobs_reflects_undo_and_filters(project):
    """undo 之后 `applied` / `can_undo` / `retracted` 三个信号同时翻面。"""
    _imported(project, SUB)
    ok_data("undo", "1", *project["base"], "--json")

    undone = ok_data("jobs", "1", *project["base"], "--json")["job"]
    assert undone["applied"] is False and undone["can_redo"] is True
    assert undone["counts"]["retracted"] == undone["counts"]["contributions"] > 0

    only_undone = ok_data("jobs", "--applied", "false", *project["base"], "--json")
    assert [j["id"] for j in only_undone["jobs"]] == [1]
    assert ok_data("jobs", "--applied", "true", *project["base"], "--json")["count"] == 0
    assert ok_data("jobs", "--state", "done", *project["base"], "--json")["count"] == 1
    assert ok_data("jobs", "--state", "failed", *project["base"], "--json")["count"] == 0

    code, error = err_of("jobs", "--state", "bogus", *project["base"], "--json")
    assert code == 1 and error["code"] == "E_BAD_STATE"
    assert error["supported"] == ["running", "done", "failed", "partial", "aborted"]
    # `--applied` 收 "false" 必须真的当 False —— `type=bool` 会把非空字符串
    # 判成 True，那是一个**静默的相反结论**
    code, error = err_of("jobs", "--applied", "maybe", *project["base"], "--json")
    assert code == 1 and error["code"] == "E_BAD_APPLIED"


# ---------------------------------------------------------------------------
# doctor 的导入体检
# ---------------------------------------------------------------------------


def test_doctor_reports_import_health(project):
    """`doctor` 的 `imports` 段：未知字段 / 坏行 / 解析器漂移。"""
    _imported(project, SUB, HTTPX)
    imports = ok_data("doctor", *project["proj"], *project["base"],
                      "--json")["imports"]

    # 坏行：夹具里**故意**有一行坏 JSON（`verify_v32r5.py` 的 A2g 用的是同一份）
    assert imports["bad_lines"], "坏行明细没被体检看到"
    assert imports["bad_lines_total"] == 1, imports["bad_lines"]

    # 未知字段：夹具里**故意**有一个 `cname_chain`。它只活在那一次解析里，
    # 所以 doctor 只能从导入报告副本读出来 —— 那条链路断了这里就是空的。
    assert imports["unknown_fields"] == {"cname_chain": 1}, imports["unknown_fields"]
    reported = [r for r in imports["reports"] if r["unknown_fields_detail"]]
    assert reported and reported[0]["job_id"], imports["reports"]

    # 解析器漂移：这次导入的指纹刚刚登记过，不该报漂移（否则是个假警）
    assert imports["parser_drift"] == [], imports["parser_drift"]
    assert imports["jobs_with_parser_fingerprint"] == 2

    # 报告目录确实在数据区里，而且报告是**人可读的 UTF-8**
    reports_dir = Path(imports["reports_dir"])
    assert reports_dir.is_dir()
    assert (reports_dir / f"{reported[0]['job_id']}.json").is_file()


def test_doctor_flags_parser_implementation_drift(project):
    """同一指纹背后的实现变过 → 报出来。**这是三个漂移信号里最要命的一个**：
    `_already_imported` 的幂等短路只认指纹，于是"实现变了"不会让任何文件被
    重导 —— 数据停在旧语义上，而报告一切正常。
    """
    _imported(project, SUB)
    conn = store.connect(project["db"])
    try:
        row = conn.execute("SELECT fingerprint FROM parser_profiles LIMIT 1").fetchone()
        fingerprint_value = str(row["fingerprint"])
        conn.execute("UPDATE parser_profiles SET definition_hash='deadbeef'"
                     " WHERE fingerprint=?", (fingerprint_value,))
    finally:
        conn.close()

    drift = ok_data("doctor", *project["proj"], *project["base"],
                    "--json")["imports"]["parser_drift"]
    assert [d["drift"] for d in drift] == ["implementation"], drift
    assert drift[0]["fingerprint"] == fingerprint_value
    assert "definition_hash" in drift[0]["detail"]


def test_doctor_keeps_import_signals_out_of_healthy(project):
    """格式漂移**不进** `healthy` —— 否则 `healthy=false` 会同时意味着
    "库坏了"和"某个上游工具加了个字段"，于是没人再认真对待那个布尔值。"""
    _imported(project, SUB, HTTPX)
    report = ok_data("doctor", *project["proj"], *project["base"], "--json")
    assert report["healthy"] is True
    assert report["imports"]["bad_lines_total"] > 0     # 有坏行，但库是健康的


# ---------------------------------------------------------------------------
# 退出码全表 —— 0/1/2/3/4/5 各有一个用例
# ---------------------------------------------------------------------------


def test_lock_conflict_is_exit_4(project):
    """`4`（锁冲突 ↔ 423）**只能靠真造一把锁**验到 —— 它没有别的触发方式。

    锁与"数据坏了"要用完全不同的动作应对：前者等一会儿重试就好，后者要去看
    体检报告。把两者塞进同一档，调用方就只能靠字符串匹配决定重试与否。
    """
    _imported(project, SUB)
    holder = store.connect(project["db"])
    try:
        holder.execute("BEGIN IMMEDIATE")               # 拿到写锁，攥着不放
        holder.execute("UPDATE projects SET status='active' WHERE id=1")
        code, payload = envelope("undo", "1", *project["base"], "--json")
        assert code == 4, (code, payload)
        assert payload["error"]["code"] == "E_LOCKED", payload
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_the_whole_exit_code_table_has_a_case():
    """这张表被**逐档**钉住：0/1/2/3/4/5 各对应本轮的一条用例。

    写成一张显式的名单而不是一句注释：它是这份文件与计划书"全表各有用例，
    尤其 3 与 4"那条交付标准之间的**可核对**连接。少一档时，红的不是某条
    业务用例（它可能刚好被重构掉），而是这一条。
    """
    from magpie.cli.envelope import EXIT_TO_HTTP

    covered = {
        0: "test_import_closes_the_file_to_database_to_command_line_loop",
        1: "test_project_create_rejects_an_unknown_scope_key",
        2: "test_undo_rejects_a_bad_or_missing_job",
        3: "test_project_create_duplicate_name_is_exit_3",
        4: "test_lock_conflict_is_exit_4",
        5: "test_schema_version_mismatch_is_exit_5",
    }
    assert sorted(covered) == sorted(EXIT_TO_HTTP) == [0, 1, 2, 3, 4, 5]
    for name in covered.values():
        assert name in globals(), f"{name} 不存在 —— 退出码表的这一档没人守"


def test_schema_version_mismatch_is_exit_5(project):
    """库比程序**新** → 退出码 5（版本不匹配 ↔ 426）。

    写路径上这一条比只读路径更要紧：**读到**不认识的 schema 只是可能读错，
    **写进**去是在一个自己读不懂的库上留下不可回退的痕迹。
    """
    conn = store.connect(project["db"])
    try:
        conn.execute("INSERT INTO schema_migrations(version, name, checksum, applied_at)"
                     " VALUES (999, 'from-the-future', 'future', '2099-01-01T00:00:00Z')")
    finally:
        conn.close()
    for args in (("undo", "1"), ("import", str(SUB)), ("jobs",),
                 ("project", "create", "p")):
        code, payload = envelope(*args, *project["base"], "--json")
        assert code == 5, (args, payload)
        assert payload["error"]["code"] == "E_SCHEMA_TOO_NEW", payload


# ---------------------------------------------------------------------------
# 写命令的横切纪律（与 2.2 的只读纪律成对）
# ---------------------------------------------------------------------------


def test_write_commands_do_not_read_stdin(project, tmp_path):
    """**stdin 挂住不给**：谁去读它，谁就卡住 —— 而卡住即失败。

    `import` 收的是一串路径，`undo` 收的是一个作业号，没有一处需要问人。
    需要人决定时（项目重名 / 文件待裁决）返回的是**结构化错误 + 退出码**，
    绝不是一句 "请选择"。
    """
    invocations = [
        ("project", "create", "p2"),
        ("import", str(SUB)),
        ("undo", "99999"),
        ("redo", "99999"),
        ("jobs",),
    ]
    for args in invocations:
        with open(tmp_path / "out.txt", "wb") as out, \
                open(tmp_path / "err.txt", "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "magpie.cli", *args, *project["base"], "--json"],
                cwd=str(ROOT), stdin=subprocess.PIPE, stdout=out, stderr=err, env=ENV)
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail(f"{args} 卡住了 —— 它在等 stdin")
            finally:
                proc.stdin.close()


def test_write_json_is_ascii_and_pipe_consumable(project):
    """真的架一条管道，且**不带 `PYTHONIOENCODING`**。

    `magpie import … --json | python -c "import json,sys;json.load(sys.stdin)"`
    必须成功 —— 交付标准里"全部写命令的 JSON 输出可被脚本直接消费"那一条的
    可执行形式。不带 `PYTHONIOENCODING` 是刻意的：Windows 上消费者的 stdin
    默认按 GBK 解，而报告里有中文（`skip_reason`、项目名）。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    produced = subprocess.run(
        [sys.executable, "-m", "magpie.cli", "import", str(HTTPX),
         *project["proj"], *project["base"], "--json"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert produced.returncode == 0, produced.stderr.decode("utf-8", "replace")
    assert produced.stdout.decode("ascii"), "--json 的输出里有非 ASCII 字节"

    consumer = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; d=json.load(sys.stdin); print(d['data']['report']['totals']['records'])"],
        input=produced.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert consumer.returncode == 0, consumer.stderr.decode("utf-8", "replace")
    assert int(consumer.stdout.strip()) > 0


def test_write_commands_refuse_to_make_the_database_for_you(work):
    """库不存在时写命令**明确失败**，而不是顺手建一个。

    顺手建库会让"库在哪、谁建的"变成一个没人能回答的问题（与 `read.cmd_init`
    拒绝在只读命令里建库是同一条理由）。而且它会让一次打错 `--db` 的
    `magpie import` 在一个陌生的路径上留下一整套 schema 与归档副本。
    """
    missing = work["dir"] / "nope" / "magpie.db"
    for args in (("import", str(SUB)), ("undo", "1"), ("redo", "1"),
                 ("project", "create", "p")):
        code, payload = envelope(*args, "--db", str(missing), "--json")
        assert code == 2, (args, payload)
        assert payload["error"]["code"] == "E_DB_MISSING", (args, payload)
    assert not missing.exists(), "写命令把库建出来了"


def test_dry_run_never_creates_the_archive_directory(project, tmp_path):
    """干跑连**归档目录**都不建 —— 一个空目录也是真的副作用。

    与 `init --dry-run` 不建 0 字节的 magpie.db 是同一条：干跑的全部价值在于
    "什么都没发生"这句话可以被无条件相信。少一个前提，它就不再是一句可以
    据以行动的话。
    """
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    db = fresh / "magpie.db"
    assert envelope("init", "--db", str(db), "--json")[0] == 0
    assert envelope("project", "create", "d", "--db", str(db), "--json")[0] == 0

    code, payload = envelope("import", str(SUB), "--dry-run", "--data-dir",
                             str(fresh), "--db", str(db), "--json")
    assert code == 0, payload
    assert not (fresh / "raw").exists(), "干跑建了归档目录"
    assert not (fresh / "reports").exists(), "干跑落了报告副本"


# ---------------------------------------------------------------------------
# add_finding（0.21.0 · AI 发现通道）
# ---------------------------------------------------------------------------


def test_add_finding_lands_in_one_undoable_job(project):
    """happy path：导入 → 发现落到实体上 → kind=ai_note → `undo` 一步撤干净。"""
    _imported(project, SUB)
    # 拿一个真实存在的实体 key（`ls` 的输出是别的命令的判据，
    # 与本文件 import 用例同一条纪律）。
    listed = ok_data("ls", *project["proj"], *project["base"], "--json")
    assert listed["count"] > 0, listed
    entity_ref = listed["entities"][0]["entity_key"]

    data = ok_data("add_finding", entity_ref, "子域 example.org 302 跳转到 www",
                   *project["proj"], *project["base"], "--json")
    assert data["op"] == "ai_note.add" and data["job_id"] is not None
    assert data["semantic_field"].startswith("ai_note:")
    assert data["source"] == "ai:add_finding"

    job = ok_data("jobs", str(data["job_id"]), *project["base"], "--json")["job"]
    assert job["kind"] == "ai_note" and job["state"] == "done" and job["applied"] == 1

    # 页面链路：`show` 必须看得见，origin=manual（GUI「人工写入」分组的来源）。
    detail = ok_data("show", entity_ref, *project["proj"],
                     *project["base"], "--json")
    hit = [o for o in detail["observations"]
           if int(o["id"]) == int(data["observation_id"])]
    assert hit and hit[0]["origin"] == "manual", hit

    # 撤销与导入作业同一条通路。
    undone = ok_data("undo", str(data["job_id"]), *project["base"], "--json")
    assert undone["report"]["integrity"]["ok"]


def test_add_finding_dry_run_writes_nothing(project):
    """干跑同构：报告照出、`job_id` 为 None、库一行不动。"""
    _imported(project, SUB)
    listed = ok_data("ls", *project["proj"], *project["base"], "--json")
    entity_ref = listed["entities"][0]["entity_key"]
    before = effective(project["db"])

    code, payload = envelope("add_finding", entity_ref, "干跑的发现",
                             "--dry-run", *project["proj"], *project["base"], "--json")
    assert code == 0 and payload["ok"], payload
    data = payload["data"]
    assert data["dry_run"] is True and data["job_id"] is None
    assert data["op"] == "ai_note.add"
    assert effective(project["db"]) == before, "干跑把库改了"


def test_add_finding_empty_text_is_exit_1(project):
    """空文本 = 用法错（退出码 1 ↔ 400），不是数据错也不是静默接受。"""
    # 文本校验先于 ref 解析 —— 空文本在任何库状态上都是用法错（退出码 1）。
    code, error = err_of("add_finding", "1", "   ",
                         *project["proj"], *project["base"], "--json")
    assert code == 1 and error["code"] == "E_USAGE", error


def test_add_finding_unknown_entity_is_exit_2(project):
    """实体不存在 = 数据错（退出码 2 ↔ 404），且一个字都不写。"""
    before = effective(project["db"])
    code, error = err_of("add_finding", "99999", "找不到的实体",
                         *project["proj"], *project["base"], "--json")
    assert code == 2 and error["code"] == "E_ENTITY_NOT_FOUND", error
    assert effective(project["db"]) == before, "失败还动了库"
