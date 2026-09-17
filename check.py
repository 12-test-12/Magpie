"""一条命令跑全绿（阶段 0 的可重复入口）。

    python check.py          # 单元测试 + 两个设计验证脚本
    python check.py bench    # 严格性能契约（MAGPIE_PERF_STRICT=1）
    python check.py all      # 上面两件事都做

为什么 `bench` 必须能**单独**触发: `MAGPIE_PERF_STRICT=1` 是 §9.2 性能承诺
唯一可验证的形式（1M 行 ≤ 60s、单 job undo ≤ 1.5s）。把它埋进"跑全部"里，
它就再也无法被单独质问"现在到底达不达标"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

UNIT = [
    ["python", "-m", "pytest", "core/tests", "-q"],
    ["python", "verify_v32r2.py"],
    ["python", "verify_v32r3.py"],
]
BENCH = [["python", "-m", "pytest", "bench/test_perf.py", "-q"]]

USAGE = __doc__


def run(steps: list[list[str]], label: str) -> int:
    print(f"=== {label} ===", flush=True)
    for step in steps:
        print(f"$ {' '.join(step)}", flush=True)
        code = subprocess.call(step, cwd=str(ROOT))
        if code != 0:
            print(f"!! 失败（退出码 {code}）: {' '.join(step)}", flush=True)
            return code
    print(f"=== {label} 通过 ===", flush=True)
    return 0


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "unit"
    if what in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if what == "bench":
        os.environ["MAGPIE_PERF_STRICT"] = "1"
        return run(BENCH, "性能契约（严格模式）")
    if what == "all":
        code = run(UNIT, "单元测试 + 设计验证")
        if code != 0:
            return code
        os.environ["MAGPIE_PERF_STRICT"] = "1"
        return run(BENCH, "性能契约（严格模式）")
    if what == "unit":
        return run(UNIT, "单元测试 + 设计验证")
    print(f"未知参数 {what!r}\n\n{USAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
