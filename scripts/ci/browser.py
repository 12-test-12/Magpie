"""Run the same isolated dev.py/browser smoke locally and on CI.

Requires existing API/frontend dependencies and an explicitly provisioned Playwright
tool directory. Never installs tools, reuses project data or overwrites a workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def candidate_port():
    # dev.py must perform the real bind/strict-port check. Do not introduce a
    # second networking entry point merely to probe ports from a test launcher.
    return 20000 + secrets.randbelow(40000)


def stop(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def main():
    node = shutil.which('node')
    if not node:
        raise SystemExit('Node.js is required; this command does not install it')
    run_id = uuid.uuid4().hex[:12]
    workspace = 'ci-browser-' + run_id
    output = ROOT / '_scratch' / 'ci-browser' / run_id
    private = ROOT / '_scratch' / 'ci-private' / run_id
    output.mkdir(parents=True); private.mkdir(parents=True)
    sys.path.insert(0, str(ROOT))
    from bench.api_research_demo import build
    build(workspace)
    from bench.research_regression_demo import seed
    fixture = seed(ROOT / '.magpie-dev' / workspace)
    (output / 'research-fixture.json').write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding='utf-8')
    api_port, web_port = candidate_port(), candidate_port()
    while web_port == api_port:
        web_port = candidate_port()
    command = [sys.executable, '-X', 'utf8', '-u', str(ROOT / 'dev.py'), '--workspace', workspace,
               '--api-port', str(api_port), '--web-port', str(web_port)]
    options = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    log_path = private / 'dev.log'  # contains ephemeral auth token; never uploaded
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, **options)
        try:
            deadline = time.monotonic() + 45
            match = None
            while time.monotonic() < deadline and process.poll() is None:
                match = re.search(r'Browser test URL:\s*(http[^\s]+)', log_path.read_text(encoding='utf-8'))
                if match:
                    break
                time.sleep(0.2)
            if not match:
                raise RuntimeError('dev.py did not start; inspect the private local dev log')
            env = {**os.environ, 'MAGPIE_BROWSER_URL': match[1],
                   'MAGPIE_PRODUCTION_URL': match[1].replace(f':{web_port}/', f':{api_port}/'),
                   'MAGPIE_BROWSER_OUTPUT': str(output), 'MAGPIE_RESEARCH_FIXTURE': json.dumps(fixture)}
            with (output / 'browser.log').open('w', encoding='utf-8') as browser_log:
                result = subprocess.run([node, str(ROOT / 'scripts/ci/browser_smoke.cjs')], cwd=ROOT,
                                        env=env, stdout=browser_log, stderr=subprocess.STDOUT, timeout=300)
            if result.returncode:
                raise RuntimeError(f'Browser smoke failed; inspect {output / "browser.log"}')
            metadata = {'source_sha': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
                        'tracked_changes': bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=no'], cwd=ROOT, text=True)),
                        'python':sys.version.split()[0], 'workspace':workspace, 'status':'passed'}
            (output / 'environment.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
            print(f'Browser smoke passed; evidence: {output}')
        finally:
            stop(process)


if __name__ == '__main__':
    main()
