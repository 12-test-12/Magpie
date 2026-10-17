"""Local browser testing: python dev.py. Uses a separate .magpie-dev database."""
from __future__ import annotations

import secrets
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default='', help='Optional isolated workspace name inside .magpie-dev')
    parser.add_argument('--api-port', type=int, default=8765, help='Loopback API port')
    parser.add_argument('--web-port', type=int, default=5173, help='Loopback Vite port')
    args = parser.parse_args()
    if not (1 <= args.api_port <= 65535 and 1 <= args.web_port <= 65535) or args.api_port == args.web_port:
        parser.error('api-port and web-port must be different valid TCP ports')
    if args.workspace and not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', args.workspace):
        parser.error('workspace must contain only letters, digits, underscores or hyphens')
    data_dir = root / '.magpie-dev'
    if args.workspace:
        data_dir /= args.workspace
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm or not (root / "frontend/node_modules/vite").exists():
        print("Install frontend dependencies first: cd frontend && npm install", flush=True)
        return 1
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print('Install API dependencies: python -m pip install -e ".[api]"', flush=True)
        return 1
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{args.web_port}/web/?token={token}"
    children = []
    try:
        children.append(subprocess.Popen([
            sys.executable, "-m", "magpie.cli", "app", "--data-dir", str(data_dir),
            "--port", str(args.api_port), "--token", token, "--no-browser"], cwd=root))
        time.sleep(2)
        if children[0].poll() is not None:
            print(f"Backend did not start; check dependencies and port {args.api_port} above.", flush=True)
            return 1
        children.append(subprocess.Popen([npm, "run", "dev", "--", "--port", str(args.web_port), "--strictPort"],
                                         cwd=root / "frontend", env={**os.environ, 'MAGPIE_DEV_API_PORT': str(args.api_port)}))
        print(f"\nBrowser test URL: {url}\nIndependent data: {data_dir}", flush=True)
        print("Vue edits reload automatically. Restart this command after Python edits. Ctrl+C stops both servers.", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
                else:
                    child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
