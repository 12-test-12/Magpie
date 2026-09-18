"""CLI 契约测试的夹具。**两个文件共用一份，不各写各的。**

理由不是省几行：`test_contract.py`（信封）与 `test_readonly.py`（只读 / 非交互）
必须跑在**同一个**库上。两份各自搭一次夹具，等它们哪天搭得不一样了，
"只读断言绿了"这件事就不再说明"信封那套命令是只读的"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from magpie.core import store                                    # noqa: E402

#: 子进程的公共环境。**`PYTHONIOENCODING` 是必须的**：Windows 上
#: `sys.executable` 的 stdout 默认按 GBK 编码，`--human` 里的中文会当场
#: UnicodeEncodeError。（`--json` 不靠它 —— 见 `test_readonly` 的那两条断言。）
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


@pytest.fixture()
def empty_db(tmp_path) -> Path:
    """只有 schema、一条事实都没有的库。"""
    db = tmp_path / "empty.db"
    conn = store.connect(db)
    store.init_db(conn)
    conn.close()
    return db


@pytest.fixture()
def loaded_db(tmp_path) -> Path:
    """一个有真实数据的库：走 2.5 的最小入口（`pipeline`）**真导入一次**。

    刻意复用 2.5 的入口而不是自己拼 `ops` 调用：CLI 的读命令要读的是
    "别的入口真的写出来的库"，而不是"我按我的理解写出来的库"。前者能发现
    入口之间的契约不一致，后者只能发现自己的理解自洽。
    """
    data_dir = tmp_path / "data"
    yaml_path = tmp_path / "pipeline.yaml"
    yaml_path.write_text(
        "name: cli contract\n"
        "project: demo\n"
        "scope:\n"
        '  include: ["*.example.com"]\n'
        "  exclude: []\n"
        f'data_dir: "{data_dir.as_posix()}"\n'
        "steps:\n"
        "  - id: dns\n"
        "    parser: domainlist\n"
        "    source: subfinder\n"
        '    source_glob: "recon/subfinder.txt"\n'
        "  - id: probe\n"
        "    source: httpx\n"
        '    source_glob: "recon/*httpx*.jsonl"\n'
        "    after: [dns]\n",
        encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "magpie.core.pipeline", "run", str(yaml_path),
         "--base-dir", str(ROOT / "bench" / "e2e")],
        cwd=str(ROOT), capture_output=True, env=ENV)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    db = data_dir / "magpie.db"
    assert db.exists(), "夹具导入没有产出数据库"
    return db
