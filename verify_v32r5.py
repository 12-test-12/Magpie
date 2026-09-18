"""v3.2-r5 端到端验收：真实文件 → 事实 → undo → redo（G2 出口 Gate ⑧）。

为什么单独一个脚本
------------------
`bench/e2e/pipeline.yaml` 从 2.5 起就在仓库里，但**全仓没有任何测试或脚本引用
它** —— 只有它自己的文件头注释提了一句。于是"真实 subfinder + httpx 夹具 →
事实表 → undo → redo"这条端到端流程，靠人记得才跑。这个脚本就是把它固化下来，
纳入 `check.py` 的 UNIT（默认跑，不是 bench）。

走哪条入口
----------
主流程走 **2.5 的最小入口**：`py -3.13 -m magpie.core.pipeline run/undo`，
**真子进程**。理由不是"更真"，而是进程内 `import run_pipeline` 恰好跳过了最
容易坏的那几段 —— argv 解析、`data_dir` 与 `db` 落在同一个基准下、退出码。
2.7 的 HTTP 入口已被 `_scratch/gate2_api.py` 独立验过（那是画布视角），这里
换成脚本视角，两条路互不替代。

`redo` 没有最小入口（`pipeline` 只登记了 `undo`，redo 在 2.6 / 2.7 的地盘），
故这一跳走 `ops.redo`。这是本脚本与产品入口之间**唯一**的进程内调用，写清楚
免得后来人以为漏了。

复原怎么判
----------
**逐行指纹**，不是条数。条数相同、内容不同是完全可能的 —— 那正是 2.4 那两条
红线要抓的情形。指纹建在 `store.snapshot_effective()` 上（复用 INV-13(a) 已有
的"有效状态"定义，不另造一套）：每张表把排好序的每一行整行塞进 sha256。

自带反例自检
------------
脚本**每次都跑**两个反例，并且要求它们**必须被判红**：

  ① 把 redo 换成 no-op（复原语义被彻底破坏）→ 必须检出；
  ② 条数一列不动、只改一个字段值 → 必须检出（这一条专门证明"只比条数"会漏）。

一个永远绿的 verify 比没有 verify 更糟：它让人更安心。反例跑不过 = 这个脚本
自己失效了，比被测代码变红更严重，所以两条都是硬断言。

另有外部开关 `MAGPIE_VERIFY_V32R5_BREAK=noop`：把真 redo 换成坏 redo，整条
Gate 应当变红。自检证明"比较有牙齿"，这个开关证明"产品坏了 Gate 真的会红"。

运行:  py -3.13 verify_v32r5.py
       MAGPIE_VERIFY_V32R5_BREAK=noop py -3.13 verify_v32r5.py    # 应退出 1
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import ops, store                                       # noqa: E402

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


# ---------------------------------------------------------------------------
# 走 2.5 的最小入口（真子进程）
# ---------------------------------------------------------------------------


def entry(*args: str) -> tuple:
    """`py -3.13 -m magpie.core.pipeline …` → (退出码, stdout)。

    子进程一律 `sys.executable`（lint 红线 C2）：`python` 在 PATH 上是谁，
    和工程解释器是谁，是两件事。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "magpie.core.pipeline", *args],
                          cwd=str(ROOT), capture_output=True, env=env)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
        proc.stderr.decode("utf-8", "replace")


def entry_json(*args: str) -> tuple:
    """同 `entry`，并把 stdout 当 JSON 读 → (退出码, 报告, 失败时的尾部输出)。

    报告是这条入口**唯一的交付通道**（人看的是它，不是退出码）。所以"stdout
    是不是合法 JSON"就等于"进程有没有崩" —— 崩掉的进程打的是 traceback。
    """
    code, out, err = entry(*args)
    try:
        return code, json.loads(out), ""
    except json.JSONDecodeError:
        tail = ((out or "") + (err or "")).strip()
        return code, None, (tail[-400:] or "(无输出)")


def open_db(db_path) -> "store.sqlite3.Connection":
    """连上子进程刚写过的库 —— 指纹是**直接读库**算的，不走任何报告。"""
    conn = store.connect(db_path)
    store.init_db(conn)
    return conn


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


def restore(conn, job_ids, baseline: dict, *, redo=None) -> tuple:
    """undo 之后逐个 redo，返回 (是否逐行复原, 复原后的指纹)。

    `redo` 刻意不在默认参数里绑定 `ops.redo`：默认参数在**函数定义时**求值，
    那样一来 `BREAK` 换掉的 redo 就永远轮不到，这个开关会静默失效。
    """
    redo = redo or active_redo()
    for jid in job_ids:
        redo(conn, jid)
    after = fingerprint(conn)
    return after == baseline, after


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "neg").mkdir(parents=True)
    data_dir = WORK / "main-data"
    db_path = data_dir / "magpie.db"

    # ================================================== A · 真实夹具 → 事实 ===
    print("=== A · 真实夹具 → 事实（走 2.5 最小入口）===")

    # A1 干跑。先看一眼：这个入口在**冷启动**下到底跑不跑得起来。
    # 注意 dry-run 仍会按 YAML 的 scope 建项目（`resolve_project` 无条件调用），
    # 这不是事实 —— 所以判据是"**事实表**一行不动"，与 §2.6 那行的措辞一致。
    code, dry, bad = entry_json("run", str(PIPELINE_YAML), "--dry-run",
                                "--db", str(db_path), "--data-dir", str(data_dir))
    check("A1a 干跑退出码 0（无 needs_confirmation 文件）", code == 0, f"rc={code} {bad}")
    check("A1b 干跑的报告是合法 JSON 且标了 dry_run", bool(dry) and dry.get("dry_run") is True)
    cold = fingerprint(open_db(db_path))
    check("A1c 干跑后事实表一行不多（有效状态逐表为 0）",
          all(cell["rows"] == 0 for cell in cold.values()), str(counts(cold)))
    dry_totals = dict((dry or {}).get("totals") or {})

    # A2 真跑。这是"文件到事实"那一步。
    code, real, bad = entry_json("run", str(PIPELINE_YAML),
                                 "--db", str(db_path), "--data-dir", str(data_dir))
    check("A2a 真跑退出码 0", code == 0, f"rc={code} {bad}")
    job_ids = list((real or {}).get("job_ids") or [])
    check("A2b 两个步骤各落一个 job（夹具真的被导入了）",
          len(job_ids) == 2, f"job_ids={job_ids}")
    check("A2c 报告里没有 problems（路径 / 编码都干净）",
          not [p for step in (real or {}).get("steps", []) for p in step.get("problems", [])],
          str([p for step in (real or {}).get("steps", []) for p in step.get("problems", [])]))

    conn = open_db(db_path)
    full = fingerprint(conn)
    facts = counts(full)
    check("A2d 事实表非空（真实文件确实变成了事实）",
          facts.get("entities", 0) > 0 and facts.get("relations", 0) > 0, str(facts))
    # 干跑与真跑算出来的数必须**一模一样** —— 否则"干跑"就没在预测真跑，
    # 只是走了个过场。这一条比"干跑 totals 是 0"有意义得多：干跑照样算全量，
    # 只是不落库，所以它的 totals 当然不是 0。
    check("A2e 干跑算出的 totals 与真跑逐项相同（干跑真的在预演，不是空转）",
          dry_totals == (real or {}).get("totals"),
          f"dry={dry_totals} real={(real or {}).get('totals')}")

    steps = {s["id"]: s for s in (real or {}).get("steps", [])}
    # `or [{}]`：某个步骤一个文件都没匹配到时，这里必须变成一条 FAIL 而不是
    # IndexError —— 验收脚本自己崩掉，等于把"红"和"没跑"混成一种表现。
    files_by_step = {sid: (s.get("files") or [{}])[0] for sid, s in steps.items()}
    check("A2f 两份来源都落了盘：dns=done，probe=partial",
          [s["state"] for s in files_by_step.values()] == ["done", "partial"],
          str({sid: f.get("state") for sid, f in files_by_step.items()}))
    # 夹具里**故意**有一行坏 JSON 和一个未知字段（`cname_chain`）。它们是这套
    # 端到端验收真正的价值所在：坏行必须被隔离，而不是让整批失败或静默消失。
    probe = files_by_step.get("probe") or {}
    probe_counts = probe.get("counts") or {}
    badlines = Path(str(probe.get("badlines_report") or ""))
    check("A2g 坏行被隔离成报告文件（1 行 JSON 解析失败），整批没有失败",
          probe_counts.get("bad_lines") == 1 and badlines.is_file()
          and "JSON 解析失败" in badlines.read_text(encoding="utf-8"),
          f"bad_lines={probe_counts.get('bad_lines')} 报告={badlines}")
    check("A2h 未知字段被记名而不是被丢掉（cname_chain）",
          (probe.get("unknown_fields_detail") or {}).get("cname_chain") == 1,
          str(probe.get("unknown_fields_detail")))
    check("A2i 两步的对账都平（records == counted）",
          all((f.get("reconcile") or {}).get("ok") for f in files_by_step.values()),
          str({sid: f.get("reconcile") for sid, f in files_by_step.items()}))
    check("A2j 导入后完整性 ok", store.integrity_check(conn)["ok"],
          str(store.integrity_check(conn).get("violations")))

    # A3 幂等：同两个文件再导一次，不该产生任何新观测。
    code, again, bad = entry_json("run", str(PIPELINE_YAML),
                                  "--db", str(db_path), "--data-dir", str(data_dir))
    second = fingerprint(conn)
    added = int((again or {}).get("totals", {}).get("added", -1))
    check("A3a 第二次导入退出码 0", code == 0, f"rc={code} {bad}")
    check("A3b 第二次导入 added=0（没有新观测）", added == 0, f"added={added}")
    check("A3c 第二次导入后逐行指纹不变（第二次不留痕）",
          second == full, f"变了的表: {diff_keys(full, second)}")

    # ============================================== B · undo → redo 精确复原 ===
    print()
    print("=== B · undo 干净回滚 → redo 精确复原 ===")

    # B1 undo 全部 job —— 走最小入口（一次一个 job，它就是那个签名）。
    codes = []
    for jid in job_ids:
        code, _undo, bad = entry_json("undo", str(jid), "--db", str(db_path))
        codes.append((jid, code, bad))
    check("B1a undo 全部退出码 0", all(c == 0 for _, c, _ in codes), str(codes))
    undone = fingerprint(conn)
    check("B1b undo 后有效状态逐表归零（干净回滚，不留幻影行）",
          all(cell["rows"] == 0 for cell in undone.values()), str(counts(undone)))
    check("B1c undo 后无待决 inbox 项", not store.integrity_check(conn)["ok"]
          or counts(undone).get("inbox_items") == 0,
          str(counts(undone).get("inbox_items")))

    # B2 redo 精确复原 —— 逐行指纹比。
    ok, redone = restore(conn, job_ids, full)
    check("B2  redo 精确复原（逐行指纹相等，不是只比条数）", ok,
          f"变了的表: {diff_keys(full, redone)}")

    # B3 反例自检①：把 redo 换成 no-op，脚本必须报出来。
    for jid in job_ids:
        ops.undo(conn, jid)
    ok_broken, fp_broken = restore(conn, job_ids, full, redo=_noop_redo)
    check("B3  反例自检①：no-op redo 必须被判红（否则本脚本没有牙齿）",
          not ok_broken and fp_broken != full,
          f"变了的表: {diff_keys(full, fp_broken)}")

    # 反例没留下痕迹 —— 真的 redo 仍然复原得回来。
    ok, redone = restore(conn, job_ids, full)
    check("B4  反例跑完后真 redo 仍然精确复原（自检不污染被测状态）", ok,
          f"变了的表: {diff_keys(full, redone)}")

    # B5 反例自检②：**条数一列不动**，只改一个字段值。
    # 这一条专门回答"为什么不能只比条数"：只比条数时它必须**看不见**，
    # 而逐行指纹必须看见。
    victim = conn.execute(
        "SELECT id, value FROM observations WHERE is_current=1 ORDER BY id LIMIT 1"
    ).fetchone()
    check("B5a 找得到一行可改的有效观测（反例②的前提）", victim is not None)
    if victim is not None:
        original = victim["value"]
        conn.execute("UPDATE observations SET value=? WHERE id=?",
                     (str(original) + "__反例__", victim["id"]))
        perturbed = fingerprint(conn)
        check("B5b 反例自检②：条数完全相同，但逐行指纹不等（只比条数会漏）",
              counts(perturbed) == counts(full) and perturbed != full,
              f"条数{'相同' if counts(perturbed) == counts(full) else '不同'}，"
              f"变了的表: {diff_keys(full, perturbed)}")
        conn.execute("UPDATE observations SET value=? WHERE id=?",
                     (original, victim["id"]))
        conn.commit()
        check("B5c 改回去 → 指纹重新相等（比对本身不假阳性）",
              fingerprint(conn) == full)

    # ================================================= C · 负向输入不崩 =======
    print()
    print("=== C · 负向输入：空文件 / GBK / 损坏 JSON / 任意二进制 ===")

    # 四种负向输入钉在**独立的数据区**里，免得它们生成的 inbox 待确认项混进
    # A/B 的有效状态。夹具现造 —— 真实世界里收到的就是这种文件。
    # YAML 本身**不放在夹具目录里**：`source_glob: "*"` 会把定义文件自己也扫
    # 进去（第一版就是这么写的，于是报告里多出一个 negative.yaml）。`--base-dir`
    # 正是为这种"定义与素材不同处"准备的。
    neg_dir, neg_data = WORK / "neg", WORK / "neg-data"
    cases = {
        "empty.txt": b"",
        "gbk.txt": "app.example.com\n测试.example.com\n".encode("gbk"),
        "broken.json": b'{"a": 1, "b": [}\n{"c": ',
        "binary.bin": bytes(range(256)) * 8,
    }
    for name, blob in cases.items():
        (neg_dir / name).write_bytes(blob)
    neg_yaml = WORK / "negative.yaml"
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

    print()
    if BREAK:
        print(f"!! 反例开关已打开: MAGPIE_VERIFY_V32R5_BREAK={BREAK}"
              " —— 复原语义被故意破坏，上面应当有 FAIL。")
    print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
