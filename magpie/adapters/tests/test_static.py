"""判据 2：`magpie/adapters/**` 源码不得 import 写入口 —— AST 断言（有反例自检）。

静态断言而不是运行期断言，理由与 `test_cli_source_contains_no_write_sql` 相同：
运行期只能验"这一次没写"；这条纪律要的是"这条路上**不可能**写"—— 而最便宜的
不可能，就是适配器源码里连写入口的名字都不出现。

判据（任务书原文）：`core.ops` / `core.store` / `cli.write` / `sqlite3` 直连
一个都不许 import。`sqlite3` 在列的理由与 PARSE-RO 相同：直连它就绕开了
`Database(read_only=True)` 的 `PRAGMA query_only`，"只读是数据库自己执行的
事实"就不成立了一半。

扫描范围是 `magpie/adapters/**` **含 tests**：本套件自己的文件也过这条规则
（连测量需要的连接都复用 `magpie.cli.tests.test_readonly._fingerprint` ——
store 的 import 住在那里，不搬进本包），所以这不是一条带豁免的规则。
反例自检保证"该报的报、不该报的不报" —— 没有自检的静态规则很可能碰巧永远不报。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTERS = ROOT / "magpie" / "adapters"

#: 写入口的**完整模块名**（相对 import 先解析成绝对名再比，两种写法等价）。
FORBIDDEN_MODULES = ("sqlite3", "magpie.core.ops", "magpie.core.store",
                     "magpie.cli.write")


def _sources():
    for path in sorted(ADAPTERS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imports(tree: ast.AST, path: Path):
    """产出 (完整模块名, 行号)。

    相对 import 按文件所在包展开（`from ..core import ops` in
    `magpie/adapters/mcp.py` → `magpie.core.ops` / `magpie.core.ops`），
    `from X import name` 还会额外产出 `X.name` —— name 若恰好是子模块
    （`from magpie.cli import write`），那就是本规则要抓的形态。
    """
    package = list(path.relative_to(ROOT).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                head = ".".join(base)
                full = f"{head}.{node.module}" if node.module else head
            else:
                full = node.module or ""
            if full:
                yield full, node.lineno
            if node.module or node.level:          # 被 import 进来的名字
                for alias in node.names:
                    yield f"{full}.{alias.name}", node.lineno


def _is_forbidden(module: str) -> bool:
    return any(module == f or module.startswith(f + ".")
               for f in FORBIDDEN_MODULES)


def test_adapters_import_no_write_entry_points():
    """`magpie/adapters/**`（含测试）的任何 import 都不许落在写入口上。"""
    bad = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module, lineno in _imports(tree, path):
            if _is_forbidden(module):
                bad.append(f"{path.relative_to(ROOT)}:{lineno} import {module}")
    assert not bad, "adapters 引入了写入口（AST 断言）：" + "; ".join(bad)


def test_the_write_import_scanner_has_teeth():
    """反例自检：四种写法要报、三种合法读侧 import 不许误报。

    直连（`import sqlite3`）、包内相对（`from ..core import ops`）、绝对
    （`import magpie.core.store`）、借包根之名（`from magpie.cli import write`）
    全都得被抓住 —— 漏掉任何一种写法，上面那条断言就只是"碰巧没被绕过"。
    """
    mcp_path = ADAPTERS / "mcp.py"                # 相对 import 的解析基准
    proofs = [
        ("import sqlite3", True),
        ("from ..core import ops", True),
        ("import magpie.core.store", True),
        ("from magpie.cli import write", True),
        ("from ..core import query", False),       # 读侧唯一实现，合法
        ("from ..cli.read import RUNNERS", False),  # 只读 runner 表，合法
        ("from ..cli.db import Database, map_db_error", False),
        ("from ..cli.envelope import dump_json", False),
    ]
    for source, must_flag in proofs:
        found = any(_is_forbidden(module)
                    for module, _ in _imports(ast.parse(source), mcp_path))
        assert found is must_flag, f"扫描器对 {source!r} 的判定错了（应为 {must_flag}）"


def test_adapters_contain_no_write_sql():
    """`magpie/adapters/**` 源码里不许出现写 SQL —— SQL-FREE 红线的精神覆盖这里。

    口径照抄 `test_cli_source_contains_no_write_sql`：只扫非文档字符串的字符串
    常量（docstring 里出现 `INSERT INTO` 是在解释这件事本身），注释不进 AST
    天然不计。**tests 目录跳过** —— 本文件的动词清单自身就是常量，扫进去等于
    自己举报自己（CLI 那份同款断言同此处置）。
    """
    write_verbs = ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE",
                   "ALTER TABLE", "CREATE TABLE")
    offences = []
    for path in _sources():
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            for verb in write_verbs:
                if verb in node.value.upper():
                    offences.append(f"{path.relative_to(ROOT)}:{node.lineno} {verb}")
    assert not offences, "adapters 里出现了写 SQL：" + "; ".join(offences)
