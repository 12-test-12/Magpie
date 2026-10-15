"""Local browser testing: python dev.py. Uses a separate .magpie-dev database."""
from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
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
    url = f"http://127.0.0.1:5173/web/?token={token}"
    children = []
    try:
        children.append(subprocess.Popen([
            sys.executable, "-m", "magpie.cli", "app", "--data-dir", str(root / ".magpie-dev"),
            "--port", "8765", "--token", token, "--no-browser"], cwd=root))
        time.sleep(2)
        if children[0].poll() is not None:
            print("Backend did not start; check dependencies and port 8765 above.", flush=True)
            return 1
        children.append(subprocess.Popen([npm, "run", "dev", "--", "--port", "5173", "--strictPort"],
                                         cwd=root / "frontend"))
        print(f"\nBrowser test URL: {url}\nIndependent data: {root / '.magpie-dev'}", flush=True)
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
