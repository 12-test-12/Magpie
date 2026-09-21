"""v3.2-r5 端到端验收：真实文件 → 事实 → undo → redo（G2 出口 Gate ⑧）。

为什么单独一个脚本
------------------
`bench/e2e/pipeline.yaml` 从 2.5 起就在仓库里，但**全仓没有任何测试或脚本引用
它** —— 只有它自己的文件头注释提了一句。于是"真实 subfinder + httpx 夹具 →
事实表 → undo → redo"这条端到端流程，靠人记得才跑。这个脚本就是把它固化下来，
纳入 `check.py` 的 UNIT（默认跑，不是 bench）。

两条入口，同一套判据（2.6 起）
------------------------------
同一段 A/B 流程对**两个入口**各跑一遍：

  * `PipelineEntry` —— 2.5 的最小入口：`py -3.13 -m magpie.core.pipeline run/undo`，
    真子进程。理由不是"更真"，而是进程内 `import run_pipeline` 恰好跳过了最容易
    坏的那几段 —— argv 解析、`data_dir` 与 `db` 落在同一个基准下、退出码。
  * `CliEntry` —— 2.6 的正式入口：`magpie project create` / `magpie import` /
    `magpie undo` / `magpie redo`，同样真子进程。2.6 的交付标准那一行写的是
    "让 CLI 也能跑通同一条流程"，而"同一条"的可核对形式就是**同一段代码、
    同一套逐行指纹**，不是另写一份差不多的。

之所以能共用：`import` 的报告与 `pipeline run` 的报告**同源**（都出自
`ingest.ingest_paths`），差别只在 pipeline 按 step 分组、import 是平铺的一串
文件。`_file_reports()` 把两者折成同一个视图，A 段的断言因此一行都不用分叉。

A2e 为什么逐条命令配对（LARI-34）
---------------------------------
A2e 比的是"干跑的 totals 与真跑逐项相同"，而**一条命令 = 一个事务边界**：

  * `pipeline run` 是**一条**命令（两个 step 在同一个事务里，`ingest.dry_run_scope`），
    所以它的干跑与真跑覆盖的是同一整段流程，聚合比较有定义；
  * `CliEntry` 刻意起**两条**子进程（`--parser` 是整条命令共用的，两条才表达得出
    "一个指定、一个仲裁"）。跨进程的干跑不可能共享事务，于是"两段聚合"的干跑
    从空库出发、真跑的第 2 段从"第 1 段已提交"出发 —— 那个断言的**对象**根本
    不是同一个库。改成逐条配对：第 i 条命令的干跑跑在"前 i−1 条真跑留下的库
    状态"上，与第 i 条命令在同一状态上的真跑逐项比。覆盖面不降反升。

跨进程这一条还带出 A2e 的**牙齿**问题：`updated` 判定退回死分支时，干跑与真跑
会一起把 updated 算成 0，等式照样成立。所以 A2e 里带了一句"重述已有事实必须
见到 updated"，并由 `teeth_flow`（每次跑都验，见下）证明那句话咬得住。

`redo` 在两条路上不对称，写清楚免得后来人以为漏了
--------------------------------------------------
2.5 的最小入口**只登记了 `undo`**（`redo` 在 2.6 / 2.7 的地盘），所以
`PipelineEntry.redo` 是**进程内**调 `ops.redo` —— 这是本脚本与产品入口之间唯一
的进程内调用。`CliEntry.redo` 走真子进程（`magpie redo`），因为 2.6 起它真的
有这个命令了。B 段两条路都要判"精确复原"。

复原怎么判
----------
**逐行指纹**，不是条数。条数相同、内容不同是完全可能的 —— 那正是 2.4 那两条
红线要抓的情形。指纹建在 `store.snapshot_effective()` 上（复用 INV-13(a) 已有
的"有效状态"定义，不另造一套）：每张表把排好序的每一行整行塞进 sha256。

自带反例自检
------------
脚本**每次都跑**三个反例，并且要求它们**必须被判红**：

  ① 把 redo 换成 no-op（复原语义被彻底破坏）→ 必须检出；
  ② 条数一列不动、只改一个字段值 → 必须检出（这一条专门证明"只比条数"会漏）；
  ③ 把 `updated` 判定打回死分支（`_existing_facts` 一律空集，效果与 `key[1]`
     写回 `key` 相同）→ A2e 的牙齿那一半必须检出。而且它顺带证明**只比
     "干跑 == 真跑"咬不住这个坏法**：那是一个对称的比，两边一起坏，等式照样成立。

一个永远绿的 verify 比没有 verify 更糟：它让人更安心。反例跑不过 = 这个脚本
自己失效了，比被测代码变红更严重，所以两条都是硬断言。

另有外部开关 `MAGPIE_VERIFY_V32R5_BREAK=noop`：把真 redo 换成坏 redo，整条
Gate 应当变红。自检证明"比较有牙齿"，这个开关证明"产品坏了 Gate 真的会红"。
它只作用于进程内那一侧（C 段与 `PipelineEntry`），因为 CLI 那一侧是子进程，
外部开关伸不进去 —— 一份诚实的边界比一个假装全面的开关好。

1. 与 2.6 契约测试的分工
------------------------
C 段（负向输入）仍然只跑 2.5 的入口：它验的是**管线的容忍度**。CLI 那一侧的
负向与退出码（空文件 / GBK / 损坏 JSON / zip / 路径不存在 → 各有各的码）在
`magpie/cli/tests/test_write.py` 里，那边能用 `envelope()` 逐条断言 `error.code`
与退出码，比在这里再写一遍更紧。

运行:  py -3.13 verify_v32r5.py
       MAGPIE_VERIFY_V32R5_BREAK=noop py -3.13 verify_v32r5.py    # 应退出 1
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import ingest, ops, store                               # noqa: E402

#: 真实夹具（2.5 的端到端样例管线就指向这两个）。
RECON = ROOT / "bench" / "e2e" / "recon"
PIPELINE_YAML = ROOT / "bench" / "e2e" / "pipeline.yaml"

#: 工作区：可重复运行，每次清空重建。`_scratch/` 不入库（见 .gitignore）。
WORK = ROOT / "_scratch" / "verify_v32r5"

PASS: list[bool] = []


def check(title, cond, detail=""):
    PASS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 逐行指纹 —— 复原的判据
# ---------------------------------------------------------------------------


def fingerprint(conn) -> dict:
    """有效状态的**逐行**指纹：每张表一份 sha256 + 行数。

    用整行（`_snapshot` 取 `SELECT *` 的全部列）而不是挑几列：挑列等于把"哪些
    列算数"变成一份需要人工同步的清单，而漏掉的那一列正是会出问题的那一列。
    """
    arms = store.snapshot_effective(conn)
    out = {}
    for table, rows in arms.items():
        blob = json.dumps(rows, sort_keys=True, default=str)
        out[table] = {"rows": len(rows),
                      "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]}
    return out


def counts(fp: dict) -> dict:
    return {table: cell["rows"] for table, cell in fp.items()}


def diff_keys(before: dict, after: dict) -> list:
    """哪些表的指纹不同。报出名字，别只报 False —— 失败时要能一眼看出坏在哪。"""
    return sorted(t for t in before if before[t] != after.get(t))


def _file_reports(report: dict) -> list:
    """两种入口的报告 → **同一个视图**：一个"文件报告"的列表。

    `pipeline run` 按 step 分组（每个 step 一串 files），`magpie import` 是平铺的
    一串 files。两者底下是同一个 `ingest_file` 的报告对象，所以折平之后 A 段的
    每一条断言都只写一遍 —— 两条路要给出的是同一个答案，那判据就不该有两份。
    """
    if report.get("kind") == "pipeline":
        return [f for step in report.get("steps") or [] for f in step.get("files") or []]
    return list(report.get("files") or [])


# ---------------------------------------------------------------------------
# 真子进程
# ---------------------------------------------------------------------------


def _run(argv: list) -> tuple:
    """跑一个子进程 → (退出码, stdout, stderr)。

    一律 `sys.executable`（lint 红线 C2）：`python` 在 PATH 上是谁，和工程
    解释器是谁，是两件事。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, *argv], cwd=str(ROOT),
                          capture_output=True, env=env)
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _json_of(code: int, out: str, err: str) -> tuple:
    """stdout 当 JSON 读 → (退出码, 报告, 失败时的尾部输出)。

    报告是这两条入口**唯一的交付通道**（人看的是它，不是退出码）。所以
    "stdout 是不是合法 JSON"就等于"进程有没有崩" —— 崩掉的进程打的是 traceback。
    """
    try:
        return code, json.loads(out), ""
    except json.JSONDecodeError:
        tail = ((out or "") + (err or "")).strip()
        return code, None, (tail[-400:] or "(无输出)")


def entry(*args: str) -> tuple:
    """`py -3.13 -m magpie.core.pipeline …` → (退出码, stdout, stderr)。"""
    return _run(["-m", "magpie.core.pipeline", *args])


def entry_json(*args: str) -> tuple:
    return _json_of(*entry(*args))


def cli(*args: str) -> tuple:
    """`py -3.13 -m magpie.cli …` → (退出码, stdout, stderr)。"""
    return _run(["-m", "magpie.cli", *args])


def cli_json(*args: str) -> tuple:
    return _json_of(*cli(*args))


def _undo_impact(payload) -> dict | None:
    """两种入口的 undo 返回 → 那个 `impact` 字典。

    `pipeline undo` 直接打印 `ops.undo` 的报告；`magpie undo` 把它套在信封的
    `data.report` 里（与 `POST /jobs/{id}/undo` 同形）。折成同一个视图，判据
    才只需要写一遍 —— 与 `_file_reports()` 是同一条理由。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if "data" in payload else payload
    if isinstance(data, dict) and "report" in data:
        data = data["report"]
    return data.get("impact") if isinstance(data, dict) else None


def open_db(db_path) -> "store.sqlite3.Connection":
    """连上子进程刚写过的库 —— 指纹是**直接读库**算的，不走任何报告。

    `data_dir` 由**子进程**建（`pipeline.main` 自己 mkdir），所以这里补一次 ——
    A 段现在在第一条命令之前就要连上库拿指纹，那时目录还不存在。与
    `pipeline.main` 的同名一步逐字一致，不是新规则。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(db_path)
    store.init_db(conn)
    return conn


def fp_of(db_path) -> dict:
    """连上、算指纹、断开。

    A 段现在要在**两次子进程调用之间**算指纹（逐条命令配对），所以不能像以前
    那样把一条连接从头拿到尾晾在那里 —— 拿一次算一次、算完就断，读与写之间
    不留任何悬着的锁。
    """
    conn = open_db(db_path)
    try:
        return fingerprint(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 两条入口
# ---------------------------------------------------------------------------


class PipelineEntry:
    """2.5 的最小入口：`pipeline run` / `pipeline undo`（真子进程）。"""

    label = "2.5 最小入口（pipeline run / undo）"
    redo_note = "redo 走进程内 ops.redo（最小入口没有 redo，它属于 2.6 / 2.7）"

    def __init__(self, yaml_path: Path) -> None:
        self.yaml = yaml_path
        self._conn = None

    def commands(self) -> list:
        """导入命令序列：`pipeline run` 是**一条**命令。

        YAML 里的两个 step **不是**两条命令行 —— 同一个进程、同一条命令、同一个
        事务（2.5 起 `dry_run_scope` 把边界提到命令上）。所以这条入口的 A2e 比的
        仍是整条命令的聚合，而那是"干跑预演真跑"最完整的形式：一个 dry 报告对
        同一份库状态下的一个 real 报告。
        """
        return [("pipeline run", self.import_files)]

    def import_files(self, db, data_dir, *, dry_run: bool) -> tuple:
        """导入夹具里的两个文件（这一步同时把项目建出来 —— YAML 的 scope 就是它）。"""
        args = ["run", str(self.yaml)]
        if dry_run:
            args.append("--dry-run")
        args += ["--db", str(db), "--data-dir", str(data_dir)]
        return entry_json(*args)

    def undo(self, db, job_id: int) -> tuple:
        return entry_json("undo", str(job_id), "--db", str(db))

    def redo(self, db, job_id: int) -> tuple:
        """进程内 —— 见模块 docstring 与 `redo_note`。"""
        if self._conn is None:
            self._conn = store.connect(db)
        report = active_redo()(self._conn, job_id)
        return 0, report, ""

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class CliEntry:
    """2.6 的正式入口：`import` / `undo` / `redo`（全是真子进程）。

    与 `PipelineEntry` 的一处结构性差异：CLI **没有一个 YAML 帮它建项目**，
    项目的创建是一个独立的、显式的动作（`magpie project create`）。这不是
    多出来的一步 —— 它是"文件的落点必须先存在"这件事在命令行上的样子。
    """

    label = "2.6 CLI（project create / import / undo / redo）"
    redo_note = "redo 走真子进程 magpie redo"

    #: 与 `bench/e2e/pipeline.yaml` 的 scope 逐字一致：两条路的**库状态**要能
    #: 直接对拍，而 scope 进的是每个 job 的 `scope_hash`。
    SCOPE = {"include": ["*.example.com"], "exclude": []}

    #: 两个来源。与 YAML 的两个 step 一一对应 —— **刻意走两条不同的选解析器
    #: 路径**：dns 显式指定，probe 交给嗅探仲裁。
    SOURCES = (
        ("subfinder.txt", ["--parser", "domainlist", "--source", "subfinder"]),
        ("httpx.jsonl",   ["--source", "httpx"]),
    )

    def __init__(self) -> None:
        self.project_id: int | None = None

    def project(self) -> list:
        return ["--project", str(self.project_id)]

    def prepare(self, db, data_dir) -> list:
        """`init` → `project create`。**这条链路的起点，两步都不能省。**

        `init` 必须显式跑：写命令刻意**不顺手建库**（与只读命令拒绝顺手建库
        同一条理由 —— 建库是一个显式的、可以被记一笔的动作）。而 CLI 也没有
        一个 YAML 帮它建项目，项目的创建同样是独立的、显式的一步。

        返回 [(标题, 退出码, 报告, 尾部输出)] 供上层逐条判。
        """
        out = []
        code, data, bad = cli_json("init", "--db", str(db),
                                   "--data-dir", str(data_dir), "--json")
        out.append(("init 建库", code, data, bad))
        code, data, bad = cli_json(
            "project", "create", "演示项目", "--scope", json.dumps(self.SCOPE),
            "--db", str(db), "--data-dir", str(data_dir), "--json")
        if data and data.get("ok"):
            self.project_id = int(data["data"]["id"])
        out.append(("project create", code, data, bad))
        return out

    def commands(self) -> list:
        """导入命令序列：**一个来源一条 `magpie import`**。

        **一次一个文件**（而不是一次给两个路径）是为了与 YAML 的两个 step 对齐：
        `--parser` 是整条命令共用的，合起来就表达不出"一个指定、一个仲裁"。

        这也正是这条入口的 A2e 必须**逐条命令配对**的原因：两条命令是两条真实
        子进程，跨进程的干跑**不可能**共享事务（`magpie import` 一条命令一个
        事务，ROLLBACK 之后就没了）——"两段聚合"的干跑对照在这条入口上没有
        定义。`pipeline run` 那条入口恰好多一个维度：两个 step 在同一个进程、
        同一个事务里，所以它的聚合比较有定义，保持原样。
        """
        return [(f"import {name}", functools.partial(self._import_one, name=name, extra=extra))
                for name, extra in self.SOURCES]

    def _import_one(self, db, data_dir, *, dry_run: bool, name: str, extra: list) -> tuple:
        """跑**一条** `magpie import` → (退出码, 该命令的报告, 尾部输出)。"""
        code, payload, bad = cli_json(
            "import", str(RECON / name), *extra,
            *self.project(), "--db", str(db), "--data-dir", str(data_dir),
            *(["--dry-run"] if dry_run else []), "--json")
        if payload is None:
            return code, None, bad
        if not payload.get("ok"):
            # 失败的导入仍然带着完整报告（挂在 error.report 上）—— 用它继续，
            # 让 A 段的断言自己去判"哪里不对"，而不是在这里提前下结论。
            report = dict(payload["error"].get("report") or {})
            report["problems"] = [payload["error"].get("message", "")]
            return code, report, bad
        return code, payload["data"]["report"], bad

    def import_files(self, db, data_dir, *, dry_run: bool) -> tuple:
        """**整轮**：顺序跑完命令序列，折成与 `pipeline run` 同形的报告。

        A 段的主体不走这里（它逐条命令跑，见 `ab_flow`）—— 这是幂等那一条
        （A3：同一批文件再导一次）用的聚合视图。
        """
        merged = {"kind": "import", "dry_run": bool(dry_run), "files": [],
                  "job_ids": [], "totals": {}, "problems": []}
        codes = []
        for _title, run in self.commands():
            code, report, bad = run(db, data_dir, dry_run=dry_run)
            codes.append(code)
            if report is None:
                return code, None, bad
            merged["files"].extend(report.get("files") or [])
            merged["job_ids"].extend(report.get("job_ids") or [])
            merged["problems"].extend(report.get("problems") or [])
            if code == 0:                  # 失败的报告里没有可加的数，别拿它凑
                for key, value in (report.get("totals") or {}).items():
                    merged["totals"][key] = merged["totals"].get(key, 0) + int(value)
        return (0 if all(c == 0 for c in codes) else max(codes)), merged, ""

    def undo(self, db, job_id: int) -> tuple:
        return cli_json("undo", str(job_id), "--db", str(db), "--json")

    def redo(self, db, job_id: int) -> tuple:
        return cli_json("redo", str(job_id), "--db", str(db), "--json")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 反例：故意坏的 redo
# ---------------------------------------------------------------------------


def _noop_redo(conn, job_id, **kw):
    """复原语义被彻底破坏的 redo —— 什么都不做，只回一个像模像样的报告。"""
    return {"job_id": job_id, "applied": True, "impact": {"contributions": []}}


#: 反例的**外部**开关。`MAGPIE_VERIFY_V32R5_BREAK=noop` 时，真 redo 被换成上面
#: 那个坏 redo，整条 Gate 应当变红（退出码 1）。
#:
#: 它和 B3 那条自检分工不同：B3 是脚本**自己**的牙齿（每次跑都验，所以不会锈）；
#: 这个开关是"把产品弄坏会怎样"的可复现证据 —— 光有一句"我会报警"不算证据，
#: 得能真把它跑红一次。默认不设，正常跑不受影响。
BREAK = os.environ.get("MAGPIE_VERIFY_V32R5_BREAK", "").strip()


def active_redo():
    """当前该用的 redo —— 默认产品的，BREAK=noop 时是坏的。"""
    return _noop_redo if BREAK == "noop" else ops.redo


def restore(conn, entry, db, job_ids, baseline: dict, *, redo=None) -> tuple:
    """undo 之后逐个 redo，返回 (是否逐行复原, 复原后的指纹)。

    `redo` 刻意不在默认参数里绑定 `ops.redo`：默认参数在**函数定义时**求值，
    那样一来 `BREAK` 换掉的 redo 就永远轮不到，这个开关会静默失效。默认值是
    `None`，真值在调用时从 `entry` 上取 —— 于是"哪条入口复原的"也一并可查。
    """
    redo = redo or entry.redo
    for jid in job_ids:
        redo(db, jid)
    after = fingerprint(conn)
    return after == baseline, after


# ---------------------------------------------------------------------------
# A · 真实夹具 → 事实    B · undo → redo 精确复原
# ---------------------------------------------------------------------------


def ab_flow(entry, work_root: Path, label: str) -> None:
    """A / B 两段。**入口无关** —— 判据全是逐行指纹与报告里的数。

    两条入口各调一次本函数，用的是同一段代码：任何一个断言在两条路上给出不同
    答案，红的都是"入口之间不一致"，而那正是端到端验收最该发现的东西。
    """
    shutil.rmtree(work_root, ignore_errors=True)
    data_dir = work_root / "data"
    db_path = data_dir / "magpie.db"

    print()
    print(f"=== A · 真实夹具 → 事实（{label}）===")
    if isinstance(entry, CliEntry):
        # CLI 的起点要多两步（`init` + `project create`）—— 那两步本身就是
        # "文件 → 库 → 命令行"这条链路的前半段，不是准备动作。
        for title, code, payload, bad in entry.prepare(db_path, data_dir):
            check(f"A0  {label}：{title} 退出码 0",
                  code == 0 and bool(payload) and payload.get("ok"), f"rc={code} {bad}")

    # A1 干跑 / A2 真跑 —— **逐条命令配对**（LARI-34 的口径裁定②）。
    #
    # 一条命令 = 一次子进程调用 = 一个事务边界。第 i 条命令的干跑跑在"前 i−1 条
    # 命令**真跑**留下的库状态"上，紧接着让第 i 条命令真跑在**同一份状态**上 ——
    # 两边看到同一个库，totals 才逐项可比。
    #
    # 为什么要改：原来比的是"整轮聚合"。`CliEntry` 刻意起两条子进程，干跑的两条
    # 都从空库出发、真跑的第二条却从"第一条已提交"出发，聚合数字天然对不上 ——
    # 那个断言在跨进程的入口上**没有定义**。改成逐条配对之后断言没有变松：原来
    # 比的是两段聚合（2 段），现在每一段都比（还是 2 段），而且第 2 段是在真实
    # 的前序状态上比的。
    #
    # 干跑仍会建项目（`resolve_project` / `magpie project create` 无条件跑），那
    # 不是事实 —— 所以 A1c 判的是**事实**表，与 §2.6 那行的措辞一致。
    aggregate = {"files": [], "job_ids": [], "totals": {}, "problems": []}
    failed: list = []
    for index, (title, run) in enumerate(entry.commands()):
        before = fp_of(db_path)

        code, dry, bad = run(db_path, data_dir, dry_run=True)
        check(f"A1a {label}：第 {index+1} 条命令（{title}）干跑退出码 0"
              f"（无 needs_confirmation 文件）", code == 0, f"rc={code} {bad}")
        check(f"A1b {label}：第 {index+1} 条命令的干跑报告是合法 JSON 且标了 dry_run",
              bool(dry) and dry.get("dry_run") is True)
        # 硬性标准：干跑跑完事实表**一行都不留**。第 1 条命令上它就是原来的
        # "有效状态逐表为 0"（起点是空库）；第 2 条起判的是"逐行指纹不变"，比
        # 只判归零更强 —— 干跑既不准留新行，也不准动已有的事实。
        after = fp_of(db_path)
        check(f"A1c {label}：第 {index+1} 条命令干跑后有效状态逐行不变（一行不留）",
              after == before, f"变了的表: {diff_keys(before, after)}")
        dry_totals = dict((dry or {}).get("totals") or {})

        # A2 真跑。这是"文件到事实"那一步。
        code, real, bad = run(db_path, data_dir, dry_run=False)
        check(f"A2a {label}：第 {index+1} 条命令真跑退出码 0", code == 0, f"rc={code} {bad}")
        real_totals = dict((real or {}).get("totals") or {})

        # 干跑与真跑算出来的数必须**一模一样** —— 否则"干跑"就没在预测真跑，
        # 只是走了个过场。干跑照样算全量、只是不落库，所以它的 totals 当然不是 0。
        #
        # 但只比"两边相等"是一条**对称**的比：`updated` 判定退回死分支时（`key[1]`
        # 写回 `key`），干跑与真跑会**一起**把 updated 算成 0，等式照样成立 ——
        # 一条没有牙齿的绿。所以这条断言带上它的牙齿：**只要这次真跑跑在"库里
        # 已经有这批事实"的状态上**（第 2 条及以后的命令；或者一条命令里的第 2 个
        # step），就必须至少算出一条 `updated`。判定坏掉时红的是这一半，
        # `teeth_flow` 每次跑都证明这一半咬得住、而那半条等式咬不住。
        restates = index > 0 or len((real or {}).get("steps") or []) > 1
        check(f"A2e {label}：第 {index+1} 条命令干跑 totals 与真跑逐项相同，"
              f"且重述已有事实时 updated 不为 0（干跑真的在预演）",
              dry_totals == real_totals
              and (not restates or int(real_totals.get("updated") or 0) > 0),
              f"dry={dry_totals} real={real_totals}")

        # 聚合视图：A2b 之后那一串判的是"整批导入真的变成了事实"，逐条命令摊开比
        # 反而看不清，所以照旧折成一份。
        aggregate["files"].extend(_file_reports(real or {}))
        aggregate["job_ids"].extend((real or {}).get("job_ids") or [])
        aggregate["problems"].extend((real or {}).get("problems") or [])
        for key, value in real_totals.items():
            aggregate["totals"][key] = aggregate["totals"].get(key, 0) + int(value)
        if code != 0:
            failed.append((title, code, bad))

    real = aggregate
    job_ids = list(real["job_ids"])
    check(f"A2b {label}：两个来源各落一个 job（夹具真的被导入了）",
          len(job_ids) == 2, f"job_ids={job_ids}")
    check(f"A2c {label}：整轮报告里没有 problems（路径 / 编码都干净）",
          not failed and not real["problems"], f"{failed} {real['problems']}")

    conn = open_db(db_path)
    full = fingerprint(conn)
    facts = counts(full)
    check(f"A2d {label}：事实表非空（真实文件确实变成了事实）",
          facts.get("entities", 0) > 0 and facts.get("relations", 0) > 0, str(facts))

    files_by_name = {Path(str(f.get("path"))).name: f for f in _file_reports(real or {})}
    check(f"A2f {label}：两份来源都落了盘：subfinder=done，httpx=partial",
          [files_by_name.get(n, {}).get("state") for n in ("subfinder.txt", "httpx.jsonl")]
          == ["done", "partial"],
          str({n: f.get("state") for n, f in files_by_name.items()}))
    # 夹具里**故意**有一行坏 JSON 和一个未知字段（`cname_chain`）。它们是这套
    # 端到端验收真正的价值所在：坏行必须被隔离，而不是让整批失败或静默消失。
    probe = files_by_name.get("httpx.jsonl") or {}
    probe_counts = probe.get("counts") or {}
    badlines = Path(str(probe.get("badlines_report") or ""))
    check(f"A2g {label}：坏行被隔离成报告文件（1 行 JSON 解析失败），整批没有失败",
          probe_counts.get("bad_lines") == 1 and badlines.is_file()
          and "JSON 解析失败" in badlines.read_text(encoding="utf-8"),
          f"bad_lines={probe_counts.get('bad_lines')} 报告={badlines}")
    check(f"A2h {label}：未知字段被记名而不是被丢掉（cname_chain）",
          (probe.get("unknown_fields_detail") or {}).get("cname_chain") == 1,
          str(probe.get("unknown_fields_detail")))
    check(f"A2i {label}：两个来源的对账都平（records == counted）",
          all((f.get("reconcile") or {}).get("ok") for f in _file_reports(real or {})),
          str({n: f.get("reconcile") for n, f in files_by_name.items()}))
    check(f"A2j {label}：导入后完整性 ok", store.integrity_check(conn)["ok"],
          str(store.integrity_check(conn).get("violations")))

    # A3 幂等：同一批文件再导一次，不该产生任何新观测。
    code, again, bad = entry.import_files(db_path, data_dir, dry_run=False)
    second = fingerprint(conn)
    added = int((again or {}).get("totals", {}).get("added", -1))
    check(f"A3a {label}：第二次导入退出码 0", code == 0, f"rc={code} {bad}")
    check(f"A3b {label}：第二次导入 added=0（没有新观测）", added == 0, f"added={added}")
    check(f"A3c {label}：第二次导入后逐行指纹不变（第二次不留痕）",
          second == full, f"变了的表: {diff_keys(full, second)}")

    # ============================================== B · undo → redo 精确复原 ===
    print()
    print(f"=== B · undo 干净回滚 → redo 精确复原（{label}）===")

    # B1 undo 全部 job —— 走这条入口自己的 undo 命令。
    failures = []
    for jid in job_ids:
        code, payload, bad = entry.undo(db_path, jid)
        if code != 0:
            failures.append((jid, code, bad))
        # 退出码 0 还不够：**报告必须来自数据层**。一条只会"不报错"的 undo
        # （impact 是空的）在退出码上与真撤了一样，而它什么都没撤 —— B1b 的
        # 指纹会抓到这个，但在这里报出"是哪个 job"，定位快得多。
        impact = _undo_impact(payload)
        # 只报三个数：impact 里的 `purge_retained` 是一份很长的清单，把它整份
        # 打进日志会把这一行埋掉 —— 而这条断言的读者要的是"撤了多少"。
        check(f"B1a {label}：undo job {jid} 真的撤了东西（impact 来自数据层）",
              bool(impact) and int(impact.get("contributions", 0)) > 0,
              "" if not impact else
              f"contributions={impact.get('contributions')} "
              f"superseded={impact.get('superseded')} "
              f"inbox_closed={impact.get('inbox_closed')}")
    check(f"B1a {label}：undo 全部退出码 0", not failures, str(failures))
    undone = fingerprint(conn)
    check(f"B1b {label}：undo 后有效状态逐表归零（干净回滚，不留幻影行）",
          all(cell["rows"] == 0 for cell in undone.values()), str(counts(undone)))
    check(f"B1c {label}：undo 后无待决 inbox 项", not store.integrity_check(conn)["ok"]
          or counts(undone).get("inbox_items") == 0,
          str(counts(undone).get("inbox_items")))

    # B2 redo 精确复原 —— 逐行指纹比。
    ok, redone = restore(conn, entry, db_path, job_ids, full)
    check(f"B2  {label}：redo 精确复原（逐行指纹相等，不是只比条数）", ok,
          f"变了的表: {diff_keys(full, redone)}")

    # B3 反例自检①：把 redo 换成 no-op，脚本必须报出来。
    # 这一条验的是**脚本自己的牙齿**（进程内），与走哪条入口无关。
    for jid in job_ids:
        ops.undo(conn, jid)
    ok_broken, fp_broken = restore(conn, entry, db_path, job_ids, full, redo=_noop_redo)
    check(f"B3  {label}：反例自检①：no-op redo 必须被判红（否则本脚本没有牙齿）",
          not ok_broken and fp_broken != full,
          f"变了的表: {diff_keys(full, fp_broken)}")

    # 反例没留下痕迹 —— 真的 redo 仍然复原得回来。
    ok, redone = restore(conn, entry, db_path, job_ids, full)
    check(f"B4  {label}：反例跑完后真 redo 仍然精确复原（自检不污染被测状态）", ok,
          f"变了的表: {diff_keys(full, redone)}")

    # B5 反例自检②：**条数一列不动**，只改一个字段值。
    # 这一条专门回答"为什么不能只比条数"：只比条数时它必须**看不见**，
    # 而逐行指纹必须看见。
    victim = conn.execute(
        "SELECT id, value FROM observations WHERE is_current=1 ORDER BY id LIMIT 1"
    ).fetchone()
    check(f"B5a {label}：找得到一行可改的有效观测（反例②的前提）", victim is not None)
    if victim is not None:
        original = victim["value"]
        conn.execute("UPDATE observations SET value=? WHERE id=?",
                     (str(original) + "__反例__", victim["id"]))
        perturbed = fingerprint(conn)
        check(f"B5b {label}：反例自检②：条数完全相同，但逐行指纹不等（只比条数会漏）",
              counts(perturbed) == counts(full) and perturbed != full,
              f"条数{'相同' if counts(perturbed) == counts(full) else '不同'}，"
              f"变了的表: {diff_keys(full, perturbed)}")
        conn.execute("UPDATE observations SET value=? WHERE id=?",
                     (original, victim["id"]))
        conn.commit()
        check(f"B5c {label}：改回去 → 指纹重新相等（比对本身不假阳性）",
              fingerprint(conn) == full)

    conn.close()
    entry.close()


# ---------------------------------------------------------------------------
# A2e 的牙齿自检：把 updated 打回死分支
# ---------------------------------------------------------------------------


#: 两条"命令"与 `CliEntry.SOURCES` 一一对应（同一个文件、同一种选解析器方式）。
TEETH_SOURCES = (("subfinder.txt", "domainlist", "subfinder"),
                 ("httpx.jsonl", None, "httpx"))


def _teeth_command(conn, project_id: int, data_dir: Path, source, *, dry_run: bool) -> dict:
    """跑**一条**导入命令（进程内，与 CLI 的一条 `magpie import` 同形）。"""
    name, parser, source_name = source
    return ingest.ingest_paths(
        conn, ingest.IngestOptions(
            project_id=project_id, data_dir=data_dir, parser=parser,
            source_name=source_name, archive="external", dry_run=dry_run),
        [RECON / name])


def teeth_flow(work_root: Path) -> None:
    """A2e 的牙齿自检：`updated` 判定退回死分支时，A2e 必须变红。

    与 B3 / B5 的分工：那两条验的是"redo 坏了，逐行指纹会不会响"；这一条验的是
    "`added` / `updated` 的判定坏了，A2e 会不会响"。为什么要专门验一次 —— A2e 的
    核心是**干跑 == 真跑**，而这是一个**对称**的比：判定坏掉时干跑与真跑**一起**
    把 updated 算成 0，等式照样成立。所以 A2e 里带了一句"重述已有事实必须见到
    updated"；这一条自检就是证明那句话不是装饰：

      * 判定完好（`key[1]`）：等式成立 **且** updated > 0 —— A2e 绿；
      * 判定打回死分支：等式**仍然**成立（那条假绿就是这么来的），而 updated 归零
        —— A2e 的那一半牙齿必须响。

    "打回死分支"用 `_existing_facts` 一律返回空集来模拟：与把 `key[1]` 写回 `key`
    的效果**逐字节相同** —— 库里明明有这批事实，判定却一条都认不出来。于是
    `updated` 成了走不到的分支，每一次重导都被算成 `added`（`LARI-33` 修的就是
    这个）。

    进程内跑，不进子进程、不读外部开关；两个文件当两条命令（第 2 条命令跑在
    第 1 条真跑留下的状态上），与 CLI 入口的配对逐字同形。所以它**每次跑都验**，
    不会像一次性的手工实验那样锈掉。
    """
    print()
    print("=== A2e 的牙齿自检 · updated 打回死分支 ===")

    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)
    data_dir = work_root / "data"
    scope = {"include": ["*.example.com"], "exclude": []}
    origin = ingest._existing_facts

    #: broken → (第 2 条命令的干跑 totals, 第 2 条命令的真跑 totals)
    observed: dict[bool, tuple] = {}
    for broken in (False, True):
        # 死分支：库里的事实一条都认不出来（== 判定写成了 `key`）
        ingest._existing_facts = (lambda conn, project_id: (set(), set())) \
            if broken else origin
        try:
            got = {}
            for mode in ("dry", "real"):
                db_path = work_root / f"{'dead' if broken else 'live'}-{mode}.db"
                conn = store.connect(db_path)
                store.init_db(conn)
                try:
                    project_id = ops.create_project(conn, f"teeth-{mode}-{broken}", scope)
                    _teeth_command(conn, project_id, data_dir, TEETH_SOURCES[0],
                                   dry_run=False)          # 第 1 条命令：真跑
                    got[mode] = _teeth_command(       # 第 2 条命令：同一状态上干跑 / 真跑
                        conn, project_id, data_dir, TEETH_SOURCES[1],
                        dry_run=(mode == "dry"))["totals"]
                finally:
                    conn.close()
            observed[broken] = (got["dry"], got["real"])
        finally:
            ingest._existing_facts = origin

    equal_live = observed[False][0] == observed[False][1]
    equal_dead = observed[True][0] == observed[True][1]
    updated_live = int(observed[False][1].get("updated") or 0)
    updated_dead = int(observed[True][1].get("updated") or 0)
    check("A2e 牙齿自检①：判定完好（key[1]）时干跑 == 真跑，且 updated 不为 0"
          "（这才是 A2e 要的绿）", equal_live and updated_live > 0,
          f"相等={equal_live} updated={updated_live} real={observed[False][1]}")
    check("A2e 牙齿自检②：打回死分支后，等式**仍然**成立"
          "（所以只比'干跑 == 真跑'是一条假绿，检测不到这个坏法）", equal_dead,
          f"相等={equal_dead} dry={observed[True][0]} real={observed[True][1]}")
    check("A2e 牙齿自检③：同一个反例下 updated 归零 —— A2e 里那半条牙齿必须响，"
          "否则这一条断言没有牙齿", updated_dead == 0, f"updated={updated_dead}")


# ---------------------------------------------------------------------------
# C · 负向输入不崩（2.5 的最小入口）
# ---------------------------------------------------------------------------


def negative_flow(work_root: Path) -> None:
    """四种负向输入钉在**独立的数据区**里，免得它们生成的 inbox 待确认项混进
    A/B 的有效状态。夹具现造 —— 真实世界里收到的就是这种文件。

    YAML 本身**不放在夹具目录里**：`source_glob: "*"` 会把定义文件自己也扫
    进去（第一版就是这么写的，于是报告里多出一个 negative.yaml）。`--base-dir`
    正是为这种"定义与素材不同处"准备的。

    只跑 2.5 的入口：这一段验的是**管线的容忍度**；CLI 那一侧的负向与退出码在
    `magpie/cli/tests/test_write.py`（见模块 docstring 第 1 条）。
    """
    print()
    print("=== C · 负向输入：空文件 / GBK / 损坏 JSON / 任意二进制 ===")

    neg_dir, neg_data = work_root / "neg", work_root / "neg-data"
    cases = {
        "empty.txt": b"",
        "gbk.txt": "app.example.com\n测试.example.com\n".encode("gbk"),
        "broken.json": b'{"a": 1, "b": [}\n{"c": ',
        "binary.bin": bytes(range(256)) * 8,
    }
    neg_dir.mkdir(parents=True, exist_ok=True)
    for name, blob in cases.items():
        (neg_dir / name).write_bytes(blob)
    neg_yaml = work_root / "negative.yaml"
    neg_yaml.write_text(
        "name: 负向输入\n"
        "project: 负向样例\n"
        "scope: {include: ['*.example.com'], exclude: []}\n"
        "steps:\n"
        "  - id: junk\n"
        "    source_glob: \"*\"\n",
        encoding="utf-8")

    code, neg, bad = entry_json("run", str(neg_yaml), "--base-dir", str(neg_dir),
                                "--db", str(neg_data / "magpie.db"),
                                "--data-dir", str(neg_data))
    # "不崩"的判据是 **stdout 仍是合法 JSON 报告**，不是退出码 —— 崩掉的进程
    # 退出码同样是 1，但打的是 traceback。退出码另有约定：负向输入必须**响亮
    # 地失败**（rc=1），静默成功（rc=0）才是真正难查的那种 bug。
    check("C1  含四种负向输入的管线不崩：stdout 仍是合法 JSON 报告，且 rc=1（响亮失败）",
          neg is not None and code == 1, f"rc={code} 尾部输出: {bad}")
    files = {Path(f["path"]).name: f for step in (neg or {}).get("steps", [])
             for f in step["files"]}
    check("C2  四个文件全都出现在报告里（没有谁被静默吞掉）",
          set(files) == set(cases), f"报告里: {sorted(files)}")

    def hint(f):
        """可读提示 —— 从报告真正会带的几个字段里找，不另编一套文案。"""
        enc = f.get("encoding") or {}
        return [x for x in (f.get("skip_reason"), f.get("reason"),
                            f.get("parse_error")) if x] or list(enc.get("notes") or [])

    for name in ("empty.txt", "broken.json", "binary.bin"):
        f = files.get(name) or {}
        counts_ = f.get("counts") or {}
        check(f"C3   {name}: 不崩、有可读提示、一条事实都没落",
              f.get("state") in ("needs_confirmation", "failed", "skipped")
              and bool(hint(f)) and int(counts_.get("records", -1)) == 0,
              f"state={f.get('state')} records={counts_.get('records')} "
              f"提示={str(hint(f))[:90]}")
    gbk = files.get("gbk.txt") or {}
    check("C4   gbk.txt: 按 gb18030 成功解码（不是被拒，也不是被静默改名）",
          gbk.get("state") == "done"
          and (gbk.get("encoding") or {}).get("encoding") == "gb18030"
          and int((gbk.get("counts") or {}).get("records", 0)) > 0,
          f"state={gbk.get('state')} enc={(gbk.get('encoding') or {}).get('encoding')} "
          f"records={(gbk.get('counts') or {}).get('records')}")

    neg_conn = open_db(neg_data / "magpie.db")
    neg_fp = fingerprint(neg_conn)
    check("C5  负向输入只产生 inbox 待确认项，不产生观测",
          counts(neg_fp).get("observations", -1) == 0,
          str(counts(neg_fp)))
    neg_conn.close()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # 两条入口各跑一遍完整的 A/B。第二条（CLI）**不是可有可无的重复**：2.6 的
    # 交付标准要的正是"命令行这条路径真人跑得通"，而这里的每一条判据都是可以与
    # 第一条逐项对拍的。
    ab_flow(PipelineEntry(PIPELINE_YAML), WORK / "pipeline", PipelineEntry.label)
    ab_flow(CliEntry(), WORK / "cli", CliEntry.label)

    teeth_flow(WORK / "teeth")
    negative_flow(WORK)

    print()
    if BREAK:
        print(f"!! 反例开关已打开: MAGPIE_VERIFY_V32R5_BREAK={BREAK}"
              " —— 复原语义被故意破坏，上面应当有 FAIL。")
    print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
