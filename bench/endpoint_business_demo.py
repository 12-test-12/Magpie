"""Create an isolated synthetic Endpoint identity/business-detail workspace.

Run: .venv-dev/Scripts/python.exe -m bench.endpoint_business_demo
Then: .venv-dev/Scripts/python.exe dev.py --workspace endpoint-business
The generator refuses to overwrite an existing workspace.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from magpie.core import ingest, ops, store

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".magpie-dev" / "endpoint-business"
FIXTURE = ROOT / "bench" / "e2e" / "endpoint-business" / "requests.har"


def build() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    db_path = DATA / "magpie.db"
    if db_path.exists():
        raise SystemExit(f"Fixture database already exists; refusing to overwrite: {db_path}")
    samples = DATA / "samples"
    samples.mkdir(exist_ok=True)
    source = samples / FIXTURE.name
    shutil.copyfile(FIXTURE, source)
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        project = ops.create_project(conn, "Endpoint 身份与业务详情 · 合成验收",
                                     {"include": ["*.example.test"]})
        report = ingest.ingest_file(conn, ingest.IngestOptions(
            project_id=project, data_dir=DATA, actor="synthetic-endpoint-demo", verify=True), source)
        if report.get("state") not in ("done", "partial"):
            raise SystemExit(f"Synthetic import failed: {report}")
        store.recompute(conn)
        store.assert_integrity(conn)
        print(f"Synthetic workspace ready: {DATA}")
        print("Project: Endpoint 身份与业务详情 · 合成验收")
        print("Contains GET/POST identities, query/body parameter names, Cookie+Bearer clues, and a JS candidate.")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
