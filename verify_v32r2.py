"""v3.2-r2 修正的端到端验收脚本（可重复运行）。

覆盖本轮五项修正 + 使其自洽所必需的最小连带改动:
  1) INV-13 重写: effective state 回到初始；事实历史只增不减
  2) INV-16: observation 的 entity 归属**是事实** → 缓存行可正确重建
  3) §4.1 不再 DELETE entity_alias（rekey 的映射事实必须活过 recompute）
  4) compact 时间区间语义: 压实不许把 observed_at 往回拉
  5) parser fingerprint 是已登记事实，并真正参与 coverage 判定
连带: 墓碑行保留（INV-03）、孤儿不自动删除、跨项目 supersession 被拒、GC 不再物理删除

运行:  python verify_v32r2.py
"""
import sqlite3
import sys

sys.path.insert(0, ".")

from magpie.core import ops, store                                        # noqa: E402
from magpie.core.store import (                                           # noqa: E402
    InvariantError, active_contribution_ids, connect, effective_equivalent,
    history_monotone, init_db, integrity_check, scope_hash, snapshot_effective,
    snapshot_history,
)

OK = []


def check(title, cond, detail=""):
    OK.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


def newdb():
    conn = connect(":memory:")
    init_db(conn)
    return conn


def env(conn, name, key="*.example.com", parser=None):
    scope = {"include": [key], "exclude": []}
    if parser:
        scope["parser_fingerprint"] = parser
    pid = ops.create_project(conn, name, scope)
    # v3.2-r3: contributions.source_id 现在有外键，source 必须先登记
    for sid in (1, 2, 3, 4):
        store.register_source(conn, source_id=sid, name=f"src{sid}",
                              authority_level="verified" if sid == 1 else "unverified")
    rf = ops.add_raw_file(conn, project_id=pid, locator_uri=f"file:///raw/{name}",
                          content="x")
    ev = ops.add_evidence(conn, project_id=pid, raw_file_id=rf, locator={"line": 1},
                          snippet="x", quality_class="high")
    return pid, rf, ev


def job(conn, pid, scope, **kw):
    return ops.start_job(conn, project_id=pid, kind="scan",
                         coverage=kw.get("coverage", "full"), coverage_scope=scope,
                         parser_fingerprint=kw.get("pf", "p-1"))


def obs(conn, pid, jid, ev, key="host:a", field="tech", value="nginx", at="2026-01-01T00:00:00Z",
        src=1, authorship="tool"):
    return ops.add_observation(conn, project_id=pid, job_id=jid, evidence_id=ev,
                               source_id=src, entity_key=key, kind="host",
                               semantic_field=field, value=value, authorship=authorship,
                               observed_at=at)


# --- 1) INV-13: effective 回到初始，历史只增不减 -----------------------------
conn = newdb()
pid, rf, ev = env(conn, "p1")
scope = {"include": ["*.example.com"], "exclude": []}
base_eff, base_hist = snapshot_effective(conn), snapshot_history(conn)
base_active = active_contribution_ids(conn)
jobs = [job(conn, pid, scope) for _ in range(4)]
for i, j in enumerate(jobs):
    obs(conn, pid, j, ev, key=f"host:h{i}", src=i + 1)
    ops.finish_job(conn, j)
store.recompute(conn)
for j in reversed(jobs):
    ops.undo(conn, j)
ok_eff, diff = effective_equivalent(conn, base_eff)
ok_hist, _ = history_monotone(conn, base_hist)
check("1a INV-13 effective state 逐列回到初始", ok_eff, str(sorted(diff))[:200])
check("1b INV-13 事实历史只增不减（G1）",
      ok_hist and snapshot_history(conn)["contributions"] > base_hist["contributions"],
      f"contributions {base_hist['contributions']} -> "
      f"{snapshot_history(conn)['contributions']}")
check("1c 活跃贡献集合回到初始", active_contribution_ids(conn) == base_active)
check("1d 无幻影行 / 完整性 ok", integrity_check(conn)["ok"])

# --- 2) INV-16: 归属是事实，缓存可正确重建 ----------------------------------
conn = newdb()
pid, rf, ev = env(conn, "p2")
scope = {"include": ["*.example.com"], "exclude": []}
j = job(conn, pid, scope)
obs_a = obs(conn, pid, j, ev, key="host:a", field="tech", value="nginx")
eid_b = ops.upsert_entity(conn, project_id=pid, job_id=j, evidence_id=ev, source_id=1,
                          entity_key="host:b", kind="host", observed_at="2026-01-01T00:00:00Z")
obs_b = obs(conn, pid, j, ev, key="host:a", field="title", value="corp")
store.recompute(conn)
eid_a = store.entity_for_key(conn, pid, "host:a")
row = conn.execute("SELECT owner_entity_id FROM contributions WHERE subject_kind='observation'"
                   " AND subject_id=?", (obs_b,)).fetchone()
check("2a 事实层显式记录 observation→entity 归属", int(row["owner_entity_id"]) == eid_a)
conn.execute("DELETE FROM observations WHERE id=?", (obs_b,))   # 模拟缓存丢失
store.recompute(conn)
restored = conn.execute("SELECT entity_id, semantic_field, value FROM observations"
                        " WHERE id=?", (obs_b,)).fetchone()
check("2b 缓存行从事实层重建到**正确**的 entity",
      restored is not None and int(restored["entity_id"]) == eid_a,
      f"restored={None if restored is None else tuple(restored)}")
check("2c 没有把事实投影到错误的 entity",
      conn.execute("SELECT COUNT(*) FROM observations WHERE entity_id=?",
                   (eid_b,)).fetchone()[0] == 0)
try:
    store.add_contribution(conn, store.NewContribution(
        project_id=pid, job_id=j, source_id=1, evidence_id=ev,
        subject_kind="observation", subject_id=obs_a, owner_entity_id=eid_b,
        contribution_kind="field", field_namespace="observation_field",
        semantic_field="tech", field_value="nginx", value_kind="string",
        observed_at="2026-01-01T00:00:00Z"))
    check("2d 归属错误的贡献必须被拒绝", False)
except InvariantError as exc:
    check("2d 归属错误的贡献必须被拒绝", "INV-16" in str(exc), str(exc))
check("2e 完整性 ok", integrity_check(conn)["ok"])

# --- 3) recompute 不再 DELETE entity_alias（rekey 映射必须留存）-------------
conn = newdb()
pid, rf, ev = env(conn, "p3")
scope = {"include": ["*.example.com"], "exclude": []}
j = job(conn, pid, scope)
obs(conn, pid, j, ev, key="/report/2024", field="status", value="ok")
ops.finish_job(conn, j)
ops.rekey(conn, project_id=pid, mapping={"/report/2024": "/report/{id}"},
          job_id=job(conn, pid, scope))
store.recompute(conn)
alias = conn.execute("SELECT entity_id FROM entity_alias WHERE alias_key=?",
                     ("/report/{id}",)).fetchone()
check("3  recompute 之后 rekey 的 alias 事实仍在（§4.1 已删掉 DELETE 行）",
      alias is not None)
check("3b 完整性 ok", integrity_check(conn)["ok"])

# --- 4) compact 时间区间语义 -------------------------------------------------
conn = newdb()
pid, rf, ev = env(conn, "p4")
scope = {"include": ["*.example.com"], "exclude": []}
stamps = ["2020-01-0%dT00:00:00Z" % i for i in (1, 2, 3, 4)]
jobs = [job(conn, pid, scope) for _ in range(4)]
for j, ts in zip(jobs, stamps):
    obs(conn, pid, j, ev, key="host:iv", value="nginx", at=ts)
    ops.finish_job(conn, j)
store.recompute(conn)
obs_id = conn.execute("SELECT id FROM observations").fetchone()[0]
before = tuple(conn.execute("SELECT observed_at, observed_at_min FROM observations"
                            " WHERE id=?", (obs_id,)).fetchone())
ops.compact(conn, project_id=pid, older_than="2025-01-01T00:00:00Z",
            job_id=job(conn, pid, scope))
after = tuple(conn.execute("SELECT observed_at, observed_at_min FROM observations"
                           " WHERE id=?", (obs_id,)).fetchone())
check("4  compact 时间区间不变（observed_at 上界不许回退）", before == after,
      f"{before} -> {after}")
syn = conn.execute("SELECT observed_at, observed_at_min, observed_at_accuracy"
                   " FROM contributions WHERE synthetic=1 AND contribution_kind='field'"
                   ).fetchone()
# v3.2-r3: 最近一条（stamps[-1]）被 protected_last_active 保护，不参与压实，
# 因此合成行的上界是"剩下来的那批"的最大值 = stamps[-2]，下界仍是 stamps[0]。
check("4b 压实行携带区间（上界=被压实成员 MAX，下界=成员 MIN）",
      tuple(syn)[:2] == (stamps[-2], stamps[0]) and syn["observed_at_accuracy"] == "approximate",
      str(tuple(syn)))
check("4c 最近一次观测被保护，未被合成行取代",
      conn.execute("SELECT COUNT(*) FROM contributions WHERE synthetic=0"
                   " AND contribution_kind='field' AND observed_at=?",
                   (stamps[-1],)).fetchone()[0] == 1)

# --- 5) parser fingerprint 是事实 + 真正参与 coverage 判定 ------------------
parser = "nmap-svc@1"
conn = newdb()
pid, rf, ev = env(conn, "p5", parser=parser)
scope5 = {"include": ["*.example.com"], "exclude": [], "parser_fingerprint": parser}
j1 = job(conn, pid, scope5, pf=parser)
o = obs(conn, pid, j1, ev, key="host:p", value="nginx")
ops.finish_job(conn, j1)
store.recompute(conn)
check("5a parser 指纹已登记为事实",
      conn.execute("SELECT COUNT(*) FROM parser_profiles WHERE fingerprint=?",
                   (parser,)).fetchone()[0] == 1)
j2 = job(conn, pid, scope5, pf=parser)
ops.finish_job(conn, j2)
check("5b 同 scope+同 parser → 允许判 inactive",
      ops.candidate_inactive(conn, observation_id=o, job_id=j2) == (True, "candidate_inactive"),
      str(ops.candidate_inactive(conn, observation_id=o, job_id=j2)))
ops.set_parser(conn, pid, "nmap-svc@2")
check("5c 换 parser 必须 bump scope_version 并写历史",
      conn.execute("SELECT scope_version FROM projects WHERE id=?",
                   (pid,)).fetchone()[0] == 2)
check("5d 旧 job 只能用历史 scope 解释",
      ops.explain_job_scope(conn, j2)["interpret_with"] == "historical_scope")
check("5e 版本漂移后不得判 inactive",
      ops.candidate_inactive(conn, observation_id=o, job_id=j2)[1] == "scope_version_drift")

# --- 6) 连带: 墓碑/孤儿/跨项目 supersession --------------------------------
conn = newdb()
pid, rf, ev = env(conn, "p6")
scope = {"include": ["*.example.com"], "exclude": []}
j = job(conn, pid, scope)
obs(conn, pid, j, ev, key="host:t", value="nginx")
ops.finish_job(conn, j)
store.recompute(conn)
eid = store.entity_for_key(conn, pid, "host:t")
report = ops.undo(conn, j)
check("6a 默认 GC 不物理删除任何缓存行（墓碑保留 → INV-03 不悬空）",
      report["impact"]["purge_eligible"] == [])
orphan = store.new_entity(conn, project_id=pid, kind="host", entity_key="host:leftover")
store.recompute(conn)
plan = store.purge_eligible(conn)
check("6b 从未有贡献的孤儿不自动清理，只进 inbox 候选",
      f"entity:{orphan}" in plan["orphan_subjects"] and plan["purge"] == [])
p2, rf2, ev2 = env(conn, "p7", key="*.other.net")
j2 = job(conn, p2, {"include": ["*.other.net"], "exclude": []})
try:
    store.supersede(conn, conn.execute("SELECT id FROM contributions LIMIT 1").fetchone()[0],
                    by_job_id=j2, reason="retract")
    check("6c 跨项目 supersession 必须被拒绝", False)
except InvariantError as exc:
    check("6c 跨项目 supersession 必须被拒绝", "INV-09" in str(exc), str(exc))
check("6d 完整性 ok", integrity_check(conn)["ok"])

print()
print(f"结果: {sum(OK)}/{len(OK)} 通过")
sys.exit(0 if all(OK) else 1)
