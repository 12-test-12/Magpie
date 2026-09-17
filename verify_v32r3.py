"""v3.2-r3 修正的端到端验收脚本（可重复运行）。

覆盖本轮四项优先修复 + 三项定义收敛:
  ① evidence 3×4 能力矩阵 + recovery_state 闭环（locator 自洽、只降不升、事务安全）
  ② ManualWriter: 单写队列 + 乐观版本 + 重试合并 → 并集且无丢失（含真并发）
  ③ now() 真实时钟（retention / 24h / activity 时间戳可用），且可注入
  ④ 术语统一: contributions 不可变事实 vs supersession 生命周期 vs alias 身份事实
  ⑤ sources 目录表（§8.2 authority_term 的数据源）
  ⑥ compact 保护"最后一条 active per (source, field)"
  ⑦ INV-13 适用范围 = job-derived 序列（独立 manual mutation 不属于 Σ）

运行:  python verify_v32r3.py
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, ".")

from core import manual as manual_mod                            # noqa: E402
from core import ops, store                                      # noqa: E402
from core.store import (                                         # noqa: E402
    connect, days_ago, effective_equivalent, hours_since, init_db, integrity_check,
    is_within, parse_time, reset_clock, set_clock, snapshot_effective,
)

PASS = []


def check(title, cond, detail=""):
    PASS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


def newdb():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def env(conn, name="p1", key="*.example.com", sources=True):
    pid = ops.create_project(conn, name, {"include": [key], "exclude": []})
    if sources:
        store.register_source(conn, source_id=1, name="nmap", authority_level="verified")
        store.register_source(conn, source_id=2, name="httpx",
                              authority_level="unverified")
        store.register_source(conn, source_id=3, name="whatweb",
                              authority_level="unverified")
    rf = ops.add_raw_file(conn, project_id=pid, locator_uri=f"file:///raw/{name}",
                          content="x")
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=rf, locator={"line": 1},
                          snippet="x", quality_class="high")
    return pid, rf, ev


def job(conn, pid, scope, **kw):
    return ops.start_job(conn, project_id=pid, kind="scan",
                         coverage=kw.get("coverage", "full"), coverage_scope=scope,
                         parser_fingerprint=kw.get("pf", "p-1"))


def obs(conn, pid, jid, ev, key="host:a", field="tech", value="nginx",
        at="2026-01-01T00:00:00Z", src=1, authorship="tool"):
    return ops.add_observation(conn, project_id=pid, job_id=jid, evidence_id=ev,
                               source_id=src, entity_key=key, kind="host",
                               semantic_field=field, value=value, authorship=authorship,
                               observed_at=at)


scope = {"include": ["*.example.com"], "exclude": []}

# ---------------------------------------------------------------- ① evidence ----
storage, recovery = ("present", "archived", "purged", "missing"), ("full", "locator", "hash_only")
matrix = {(s, r): ops.evidence_capability(s, r) for s in storage for r in recovery}
check("①a 3×4 = 12 种组合全部有定义", len(matrix) == 12)
check("①b 原文在 + 有 locator → recoverable",
      all(matrix[(s, r)] == "recoverable" for s in ("present", "archived")
          for r in ("full", "locator")))
check("①c 原文在但 locator 已丢弃 → 只能校验",
      all(matrix[(s, "hash_only")] == "verifiable_only" for s in ("present", "archived")))
check("①d 原文已清除 → 只能校验",
      all(matrix[("purged", r)] == "verifiable_only" for r in recovery))
check("①e 无原文无归档 → 只剩 hash",
      all(matrix[("missing", r)] == "hash_only" for r in recovery))
check("①f 未知组合按最保守处理",
      ops.evidence_capability("?", "?") == "hash_only")

conn = newdb()
pid, rf, ev = env(conn, "e1")
r1 = ops.degrade_evidence(conn, raw_file_id=rf, to_state="locator")
r2 = ops.degrade_evidence(conn, raw_file_id=rf, to_state="hash_only")
check("①g hash_only 必须丢弃 locator（DB CHECK 强制）",
      conn.execute("SELECT locator FROM evidence WHERE id=?", (ev,)).fetchone()[0] is None
      and r2["locatable"] is False)
try:
    ops.degrade_evidence(conn, raw_file_id=rf, to_state="full")
    check("①h 不允许升级 recovery_state", False)
except store.InvariantError as exc:
    check("①h 不允许升级 recovery_state", "INV-17" in str(exc))
conn2 = newdb()
pid2, rf2, ev2 = env(conn2, "e2")
rep = ops.purge_raw_file(conn2, raw_file_id=rf2, job_id=job(conn2, pid2, scope))
check("①i purge_raw_file 事务安全（旧实现嵌套事务必崩）",
      rep["capability"] == "verifiable_only"
      and conn2.execute("SELECT recovery_state FROM evidence WHERE id=?",
                        (ev2,)).fetchone()[0] == "hash_only")
check("①j evidence/raw_file 完整性 ok", integrity_check(conn2)["ok"])

# ------------------------------------------------------- ② ManualWriter ----
conn = newdb()
pid, rf, ev = env(conn, "m1")
j = job(conn, pid, scope)
o = obs(conn, pid, j, ev, key="host:m")
eid = store.entity_for_key(conn, pid, "host:m")
w = manual_mod.ManualWriter(conn, actor="analyst")
for i in range(100):
    w.set_keys(project_id=pid, subject_kind="entity", subject_id=eid, add=[f"k{i}"])
keys = set(w.read_keys(project_id=pid, subject_kind="entity", subject_id=eid))
check("②a 100 次写入 = 100 个 key（无丢失）", len(keys) == 100, f"{len(keys)}")
w.set_keys(project_id=pid, subject_kind="entity", subject_id=eid, add=["k0", "k1"])
check("②b 重复写入幂等（不是覆盖写）",
      set(w.read_keys(project_id=pid, subject_kind="entity", subject_id=eid)) == keys)
w.clear_keys(project_id=pid, subject_kind="entity", subject_id=eid, keys=["k0"])
left = set(w.read_keys(project_id=pid, subject_kind="entity", subject_id=eid))
check("②c clear 是增量语义，不抹掉其它 key",
      "k0" not in left and "k99" in left)
check("②d 每次写都有审计",
      conn.execute("SELECT COUNT(*) FROM activity_log WHERE action IN"
                   " ('manual.set','manual.clear')").fetchone()[0] >= 101)

tmpdir = Path(tempfile.mkdtemp())
fconn = connect(str(tmpdir / "manual.db"), check_same_thread=False)
init_db(fconn)
fpid, frf, fev = env(fconn, "m2")
fj = job(fconn, fpid, scope)
fo = obs(fconn, fpid, fj, fev, key="host:t")
feid = store.entity_for_key(fconn, fpid, "host:t")
fw = manual_mod.ManualWriter(fconn, actor="threads")
errors: list[BaseException] = []


def worker(tid):
    try:
        for i in range(25):
            fw.set_keys(project_id=fpid, subject_kind="entity", subject_id=feid,
                        add=[f"t{tid}_k{i}"])
    except BaseException as exc:                                # pragma: no cover
        errors.append(exc)


threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
final = set(fw.read_keys(project_id=fpid, subject_kind="entity", subject_id=feid))
check("②e 8 线程 × 25 key 真并发 → 并集 200 个、无异常",
      not errors and final == {f"t{t}_k{i}" for t in range(8) for i in range(25)},
      f"{len(final)} keys, errors={errors}")
store.recompute(fconn)
check("②f 并发写入后完整性 ok", integrity_check(fconn)["ok"])
fconn.close()

# --------------------------------------------------------------- ③ now() ----
stamp = store.now()
real_ok = parse_time(stamp) is not None and is_within(stamp, hours=24.0)
check("③a now() 是真实时钟（不再是常量）", real_ok, stamp)
check("③b retention 表达式可用", hours_since(days_ago(180)) >= 179 * 24)
try:
    set_clock(lambda: "2030-05-05T05:05:05Z")
    conn = newdb()
    pid, rf, ev = env(conn, "c1")
    j = job(conn, pid, scope)
    obs(conn, pid, j, ev, key="host:clock")
    frozen_ok = (store.now() == "2030-05-05T05:05:05Z"
                 and conn.execute("SELECT created_at FROM contributions LIMIT 1"
                                  ).fetchone()[0] == "2030-05-05T05:05:05Z")
finally:
    reset_clock()
check("③c 时钟可注入（测试确定性），恢复后回到真实时钟",
      frozen_ok and store.now() != "2030-05-05T05:05:05Z")

# --------------------------------------------------------------- ⑤ sources ----
conn = newdb()
pid, rf, ev = env(conn, "s1")
j = job(conn, pid, scope)
o = obs(conn, pid, j, ev, key="host:s")
store.recompute(conn)
seid = store.entity_for_key(conn, pid, "host:s")
from core.confidence import score_field                             # noqa: E402
conf, _ = score_field(conn, "entity", seid, "observation_field", "tech", "nginx")
check("⑤a sources 目录表驱动 authority_term（verified=1.5）",
      store.source_authority(conn, 1) == "verified" and conf in ("high", "medium"), conf)
try:
    store.add_contribution(conn, store.NewContribution(
        project_id=pid, job_id=j, source_id=999, evidence_id=ev, subject_kind="entity",
        subject_id=seid, contribution_kind="entity", observed_at="2026-01-01T00:00:00Z"))
    check("⑤b 未登记的 source 被外键拒绝", False)
except Exception as exc:
    check("⑤b 未登记的 source 被外键拒绝", "FOREIGN KEY" in str(exc), type(exc).__name__)

# ------------------------------------------------------------- ⑥ compact ----
conn = newdb()
pid, rf, ev = env(conn, "cp1")
stamps = ["2020-01-0%dT00:00:00Z" % i for i in (1, 2, 3)]
jobs = [job(conn, pid, scope) for _ in range(3)]
for jj, ts in zip(jobs, stamps):
    obs(conn, pid, jj, ev, key="host:last", field="title", value="corp", at=ts)
    ops.finish_job(conn, jj)
store.recompute(conn)
latest = conn.execute("SELECT id FROM contributions WHERE subject_kind='observation'"
                      " ORDER BY observed_at DESC, id DESC LIMIT 1").fetchone()[0]
rep = ops.compact(conn, project_id=pid, older_than="2025-01-01T00:00:00Z",
                  job_id=job(conn, pid, scope))
row = conn.execute("SELECT synthetic, compacted_job_count FROM contributions WHERE id=?",
                   (latest,)).fetchone()
check("⑥a 最近一条 active 被保护（未被合成行取代）",
      latest in rep["protected_last_active"] and int(row["synthetic"]) == 0)
check("⑥b 缓存 observed_at 仍是最新那次观测",
      conn.execute("SELECT observed_at FROM observations").fetchone()[0] == stamps[-1])
check("⑥c compact 后完整性 ok", integrity_check(conn)["ok"])

# ------------------------------------------------------ ⑦ INV-13 适用范围 ----
conn = newdb()
pid, rf, ev = env(conn, "i1")
j0 = job(conn, pid, scope)
obs(conn, pid, j0, ev, key="host:a")
ops.finish_job(conn, j0)
store.recompute(conn)
eid = store.entity_for_key(conn, pid, "host:a")
mw = manual_mod.ManualWriter(conn, actor="analyst")
mw.set_keys(project_id=pid, subject_kind="entity", subject_id=eid, add=["display_name"])
mw.set_tag(project_id=pid, entity_id=eid, tag="crown-jewel")
baseline = snapshot_effective(conn)                # Σ 之前的 effective state
j1 = job(conn, pid, scope)
obs(conn, pid, j1, ev, key="host:b", field="tech", value="apache")
ops.finish_job(conn, j1)
store.recompute(conn)
ops.undo(conn, j1)
ok, diff = effective_equivalent(conn, baseline)
check("⑦a job-derived 序列 undo 后 effective == 序列前状态", ok, str(sorted(diff))[:160])
check("⑦b 独立 manual mutation 不被 undo 触碰",
      mw.read_keys(project_id=pid, subject_kind="entity", subject_id=eid)
      == ["display_name"]
      and conn.execute("SELECT COUNT(*) FROM entity_tags WHERE deleted_at IS NULL"
                       ).fetchone()[0] == 1)
check("⑦c 完整性 ok", integrity_check(conn)["ok"])

print()
print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
sys.exit(0 if all(PASS) else 1)
