import sys, time
sys.path.insert(0, ".")
from magpie.core import ops, store

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
conn = store.connect(":memory:")
store.init_db(conn)
pid = ops.create_project(conn, "perf", {"include": ["*.example.com"], "exclude": []})
sid = store.register_source(conn, name="subfinder", authority_level="verified", tool="subfinder")
store.register_parser(conn, "subfinder@1", profile_id="subfinder", version="1", definition_hash="dh")
ops.set_parser(conn, pid, "subfinder@1")
rf = ops.add_raw_file(conn, project_id=pid, locator_uri="file:///x.txt", content="x\n")
ev = ops.add_evidence(conn, project_id=pid, raw_file_id=rf, locator={"line": 1}, snippet="x")

t0 = time.perf_counter()
with store.transaction(conn):
    job = ops.start_job(conn, project_id=pid, kind="ingest", coverage="full",
                        coverage_scope={"include": ["*.example.com"], "exclude": []},
                        parser_fingerprint="subfinder@1")
    for i in range(N):
        ops.add_observation(conn, project_id=pid, job_id=job, evidence_id=ev, source_id=sid,
                            entity_key=f"h{i % 5000}.example.com", kind="host",
                            semantic_field="status_code", value=str(200 + (i % 300)))
    ops.finish_job(conn, job, state="done")
t1 = time.perf_counter()
subjects = [("observation", r["id"]) for r in conn.execute("SELECT id FROM observations")]
t2 = time.perf_counter()
store.recompute(conn, subjects)
t3 = time.perf_counter()
print(f"N={N} write={t1-t0:.2f}s ({N/(t1-t0):.0f}/s) recompute_delta={t3-t2:.2f}s total={t3-t0:.2f}s")
