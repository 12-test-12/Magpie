"""Create an isolated synthetic JS intelligence browser workspace.

Run: .venv-dev/Scripts/python.exe -m bench.js_demo
Then: .venv-dev/Scripts/python.exe dev.py --workspace js-intelligence
The database path is refused when it already exists.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from magpie.core import ingest, ops, store

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".magpie-dev" / "js-intelligence"


def build():
    DATA.mkdir(parents=True, exist_ok=True)
    db_path = DATA / "magpie.db"
    if db_path.exists():
        raise SystemExit(f"Fixture database already exists; refusing to overwrite: {db_path}")
    samples = DATA / "samples"
    samples.mkdir(exist_ok=True)
    project_id = None
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        project_id = ops.create_project(conn, "JS 内容关联 · 合成验收", {"include": ["*.example.test"]})
        manifest_source = ROOT / "bench" / "e2e" / "js" / "app.manifest.json"
        manifest = samples / "app.manifest.json"
        manifest.write_bytes(manifest_source.read_bytes())
        har = {
            "log": {"version": "1.2", "creator": {"name": "Magpie synthetic fixture", "version": "1"},
                    "entries": [
                        {"request": {"method": "GET", "url": "https://api.example.test/v1/profile?token=synthetic"},
                         "response": {"status": 200, "content": {"mimeType": "application/json", "text": "{}"}}},
                        {"request": {"method": "GET", "url": "https://cdn.example.test/assets/app.js"},
                         "response": {"status": 200, "content": {"mimeType": "application/javascript",
                             "text": "fetch('https://api.example.test/v1/profile?token=second');"}}},
                        {"request": {"method": "GET", "url": "https://cdn.example.test/assets/bundle"},
                         "response": {"status": 200, "content": {"mimeType": "text/ecmascript", "encoding": "base64",
                             "text": base64.b64encode(b"axios.get('https://api.example.test/v2/items');").decode("ascii")}}},
                        {"request": {"method": "GET", "url": "https://cdn.example.test/assets/missing.js"},
                         "response": {"status": 200, "content": {"mimeType": "application/javascript"}}},
                        {"request": {"method": "GET", "url": "https://cdn.example.test/assets/invalid.js"},
                         "response": {"status": 200, "content": {"mimeType": "application/javascript", "encoding": "base64", "text": "%%%"}}},
                    ]}}
        har_path = samples / "captured-js.har"
        har_path.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        for path in (manifest, har_path):
            report = ingest.ingest_file(conn, ingest.IngestOptions(
                project_id=int(project_id), data_dir=DATA, actor="synthetic-js-demo", verify=True), path)
            if report.get("state") not in ("done", "partial"):
                raise SystemExit(f"Synthetic import failed: {path.name}: {report}")
        store.recompute(conn)
        store.assert_integrity(conn)
        print(f"Synthetic workspace ready: {DATA}")
        print("Project: JS 内容关联 · 合成验收")
        print("Contains local manifest, plain HAR JS, base64 JS, missing-content JS and invalid-base64 JS.")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
