"""一条命令跑全绿（阶段 0 的可重复入口；2.1 起把静态红线也收了进来）。

    python check.py          # 静态红线 + 单元测试 + 两个设计验证脚本
    python check.py lint     # 只跑静态红线（秒级）
    python check.py bench    # 严格性能契约（MAGPIE_PERF_STRICT=1）
    python check.py all      # 上面全部

为什么 `bench` 必须能**单独**触发: `MAGPIE_PERF_STRICT=1` 是 §9.2 性能承诺
唯一可验证的形式（1M 行 ≤ 60s、单 job undo ≤ 1.5s）。把它埋进"跑全部"里，
它就再也无法被单独质问"现在到底达不达标"。

为什么 `lint` 也必须能**单独**触发: 静态红线是秒级的，单元测试是分钟级的。
红线只有在"改一行就能立刻验"的时候才会真的被用起来；混在一起的结果是没人
为了验一条 import 规则去等半分钟。

为什么子进程一律 `sys.executable`（坑 2）: 写死 `"python"` 会按 PATH 解析，
于是在 3.13 里执行 `py -3.13 check.py`，测试却跑在 3.9 上 —— 表面全绿，实际
验的是另一个解释器。本工程已明确钉在 3.9（见 2.1 决策 1），所以这条尤其重要：
**它是"你跑哪个解释器，测试就跑在哪个解释器上"的唯一保证**。没有它，3.9 这个
选择会随环境变化悄悄退化，而没人会发现。`C2` 规则把这条纪律变成断言。

静态红线一览（`python check.py lint`）:
    C2  子进程一律 sys.executable，不得把字面量解释器名当可执行名（坑 2）
    C3  全仓不再出现顶级 `core` 包导入（坑 3 迁移后的防回流）
    R6  magpie/core/** 顶层 import ⊆ 标准库（core 只用标准库）
    R7  零出站：无 socket 连接点 / urllib.request / requests / Python httpx 库
    P39 保持 3.9 可移植：无 match / tomllib / dataclass slots 等 3.10+ 运行时特性

关于 P39 为什么值得有: 现有代码能把"解释器升级成本压到零"，靠的是
`from __future__ import annotations` 这条早期纪律。既然现在选择留在 3.9，就要把
那条纪律变成断言 —— 否则第一个写出 `match` 的人，会让未来某次升级重新变贵。
这是留在 3.9 这个决策唯一需要付的保险费。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent

UNIT = [
    [sys.executable, "-m", "pytest", "magpie/core/tests", "-q"],
    [sys.executable, "verify_v32r2.py"],
    [sys.executable, "verify_v32r3.py"],
]
BENCH = [[sys.executable, "-m", "pytest", "bench/test_perf.py", "-q"]]

USAGE = __doc__


# ---------------------------------------------------------------------------
# 静态红线：公共零件
# ---------------------------------------------------------------------------

_PARSE_CACHE: dict = {}
_STDLIB_CACHE = None


def _iter_sources():
    """(path, rel) —— 全仓 .py，跳过 .git / .idea / .venv / __pycache__ 之类。"""
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        yield path, rel


def _parse(path: Path, rel: Path):
    """(tree, syntax_error)。解析结果缓存 —— 5 条规则共用一次 parse。"""
    key = rel.as_posix()
    if key not in _PARSE_CACHE:
        try:
            _PARSE_CACHE[key] = (
                ast.parse(path.read_text(encoding="utf-8"), filename=key), None)
        except SyntaxError as exc:
            _PARSE_CACHE[key] = (None, exc)
    return _PARSE_CACHE[key]


def _v(rel: Path, lineno: int, msg: str) -> str:
    return f"{rel.as_posix()}:{lineno}: {msg}"


_SUBPROCESS_FUNCS = {"call", "run", "check_call", "check_output", "Popen",
                     "system", "popen"}


def _command_head_literals(tree):
    """产出 (可执行名字符串, 行号)。

    两种形态都要覆盖 —— `subprocess.call(["python", …])`，以及常量表
    `UNIT = [["python", …]]`。坑 2 改造前正藏在后者里：它是模块级常量，
    不是一个 Call，只扫调用点会静默漏掉。
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            head = node.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                yield head.value, head.lineno
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            first = node.args[0]
            if name in _SUBPROCESS_FUNCS and isinstance(first, ast.Constant) \
                    and isinstance(first.value, str):
                yield first.value, first.lineno


def _stdlib_names() -> set:
    """当前解释器的标准库顶层模块名。

    刻意**不用** `sys.stdlib_module_names` —— 那是 3.10+ 才有的，而本工程钉在
    3.9。改为从解释器自己的 stdlib 目录枚举，任何版本都对；3.10+ 上也取并集，
    免得只靠目录枚举漏掉什么。
    """
    global _STDLIB_CACHE
    if _STDLIB_CACHE is not None:
        return _STDLIB_CACHE
    names = set(sys.builtin_module_names)
    names.update(getattr(sys, "stdlib_module_names", ()))
    for base in (sysconfig.get_paths()["stdlib"], str(Path(sys.base_prefix) / "Lib")):
        root = Path(base)
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.name in ("site-packages", "__pycache__", "lib-dynload"):
                continue
            if entry.suffix == ".py" and entry.stem != "__init__":
                names.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").exists():
                names.add(entry.name)
    _STDLIB_CACHE = names
    return names


# ---------------------------------------------------------------------------
# C2 · 子进程一律 sys.executable（坑 2 的反例断言）
# ---------------------------------------------------------------------------

_BARE_INTERPRETER_STEMS = {"python", "python3", "py"}


def _is_bare_interpreter(name: str) -> bool:
    """按可执行名判定 —— 钉绝对路径（D:\\…\\python.exe）同样是把解释器写死。"""
    return Path(name).stem.lower() in _BARE_INTERPRETER_STEMS


def _bare_interpreters_in_source(src: str) -> list:
    return [(n, ln) for n, ln in _command_head_literals(ast.parse(src))
            if _is_bare_interpreter(n)]


def _selftest_c2() -> list:
    """反例自检：该报的报、不该报的不报。

    没有它，上面那条断言可能只是"碰巧永远不报"—— 那和没写没有区别。
    """
    proof = 'UNIT = [["python", "-m", "pytest"]]\n'           # 必须被检出
    control = 'import sys\nUNIT = [[sys.executable, "x"]]\n'  # 不得被检出
    problems = []
    if not _bare_interpreters_in_source(proof):
        problems.append('C2 自检失败：反例 ["python", …] 未被检出（断言形同虚设）')
    if _bare_interpreters_in_source(control):
        problems.append("C2 自检失败：sys.executable 被误报")
    return problems


def _rule_c2() -> list:
    bad = _selftest_c2()
    for path, rel in _iter_sources():
        tree, _ = _parse(path, rel)
        if tree is None:
            continue
        for name, lineno in _command_head_literals(tree):
            if _is_bare_interpreter(name):
                bad.append(_v(rel, lineno,
                              f"把字面量 {name!r} 当可执行名 —— 子进程必须用 sys.executable，"
                              f"否则换个解释器执行时会静默跑回另一个解释器"))
    return bad


# ---------------------------------------------------------------------------
# C3 · 不再出现顶级 `core` 包导入（坑 3 的防回流）
# ---------------------------------------------------------------------------

def _rule_c3() -> list:
    bad = []
    for path, rel in _iter_sources():
        tree, _ = _parse(path, rel)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core" or alias.name.startswith("core."):
                        bad.append(_v(rel, node.lineno,
                                      f"import {alias.name!r} —— 顶级 `core` 包已迁到 "
                                      f"`magpie.core`（坑 3）"))
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                if module == "core" or module.startswith("core."):
                    bad.append(_v(rel, node.lineno,
                                  f"from {module} import … —— 顶级 `core` 包已迁到 "
                                  f"`magpie.core`（坑 3）"))
    return bad


# ---------------------------------------------------------------------------
# R6 · magpie/core/** 顶层 import ⊆ 标准库
# ---------------------------------------------------------------------------

# R6 的保护对象。写全名而不是 "magpie"：从 core 里 `from magpie.core import …`
# 是自己人，`from magpie.api import …` 就可能把 FastAPI 拖进 core。
_CORE_PACKAGE = "magpie.core"
# 只有 magpie/core/tests/** 能引用这些：测试用 pytest 是正常的，而 core 的
# **运行时**模块一旦引入第三方依赖，R6 就破了。
_CORE_TEST_EXTRA_IMPORTS = {"pytest", "hypothesis"}


def _top_level_imports(tree):
    """模块级 import 的**完整**模块名 + 行号。包内相对 import 跳过（它是自己人）。

    刻意给完整名而不是顶层段：`magpie.core.*`（自己人）与 `magpie.api`（可能拖进
    第三方依赖）顶层段都是 `magpie`，只看顶层段就分不开了。
    """
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module, node.lineno


def _is_self_reference(module: str) -> bool:
    """`magpie.core` 自己引用自己，不算"引入了一个依赖"。"""
    return module == _CORE_PACKAGE or module.startswith(_CORE_PACKAGE + ".")


def _rule_r6() -> list:
    stdlib = _stdlib_names()
    core = ROOT / "magpie" / "core"
    if not core.is_dir():
        return [f"未找到 {core} —— R6 断言无从谈起"]
    bad = []
    for path in sorted(core.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part == "__pycache__" for part in rel.parts):
            continue
        tree, _ = _parse(path, rel)
        if tree is None:
            continue
        allowed = set(stdlib)
        if "tests" in rel.parts:
            allowed |= _CORE_TEST_EXTRA_IMPORTS
            allowed |= {s.stem for s in path.parent.glob("*.py")}   # 同目录测试模块
        for module, lineno in _top_level_imports(tree):
            if _is_self_reference(module):
                continue
            if module.split(".")[0] not in allowed:
                bad.append(_v(rel, lineno,
                              f"顶层 import 了非标准库模块 {module!r} —— R6：core 只用标准库；"
                              f"新依赖一律进 extras，且 parser 内部 lazy import"))
    return bad


# ---------------------------------------------------------------------------
# R7 · 零出站网络
# ---------------------------------------------------------------------------

# 出站客户端模块。**按 import 名判定，不按字符串** —— 这样 2.4 的解析器里出现
# "httpx" 这个工具名（ProjectDiscovery 的 httpx，读它的 JSONL 输出）不会误报，
# 只有 `import httpx`（Python 的 httpx 库）才报。
_OUTBOUND_MODULES = ("httpx", "requests", "http.client", "urllib.request", "urllib3")
# socket 模块上真正会建立连接的名字。含 socket() 本身：先建套接字再连，拆开写
# 也绕不过 —— 任一步被禁，链路就断。
_SOCKET_CONNECT_ATTRS = {"connect", "connect_ex", "create_connection", "socket"}
# 子进程形态的出站：按命令名判定。
_NET_CLIENT_COMMANDS = {"curl", "wget", "nc", "ncat", "netcat", "telnet"}


def _is_outbound_module(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in _OUTBOUND_MODULES)


def _rule_r7() -> list:
    bad = []
    for path, rel in _iter_sources():
        tree, _ = _parse(path, rel)
        if tree is None:
            continue

        socket_names = set()      # 绑定到 socket 模块的本地名
        socket_calls = {}         # 从 socket 直接 import 进来的连接函数

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "socket":
                        socket_names.add(alias.asname or "socket")
                    elif _is_outbound_module(alias.name):
                        bad.append(_v(rel, node.lineno,
                                      f"import 了出站客户端模块 {alias.name!r} —— "
                                      f"R7：零出站网络"))
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                if module == "socket":
                    for alias in node.names:
                        if alias.name in _SOCKET_CONNECT_ATTRS:
                            socket_calls[alias.asname or alias.name] = alias.name
                elif _is_outbound_module(module):
                    bad.append(_v(rel, node.lineno,
                                  f"from {module} import … —— R7：零出站网络"))
                elif module == "urllib" and any(a.name == "request" for a in node.names):
                    bad.append(_v(rel, node.lineno,
                                  "from urllib import request —— R7：零出站网络"
                                  "（urllib.parse 不受限）"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id in socket_names \
                    and func.attr in _SOCKET_CONNECT_ATTRS:
                bad.append(_v(rel, node.lineno,
                              f"{func.value.id}.{func.attr}(…) 是出站调用点 —— "
                              f"R7：零出站网络"))
            elif isinstance(func, ast.Name) and func.id in socket_calls:
                bad.append(_v(rel, node.lineno,
                              f"{func.id}(…) 是出站调用点"
                              f"（from socket import {socket_calls[func.id]}）—— "
                              f"R7：零出站网络"))
        for name, lineno in _command_head_literals(tree):
            if Path(name).stem.lower() in _NET_CLIENT_COMMANDS:
                bad.append(_v(rel, lineno,
                              f"以 {name!r} 起子进程拉取远端 —— R7：零出站网络"))
    return bad


# ---------------------------------------------------------------------------
# P39 · 保持 3.9 可移植（本决策的配套保险）
# ---------------------------------------------------------------------------

_PY310_DATACLASS_KWARGS = {"slots", "kw_only", "match_args", "weakref_slot"}
_BUILTIN_TYPE_NAMES = {"int", "str", "bytes", "bytearray", "float", "bool", "complex",
                       "list", "dict", "set", "frozenset", "tuple", "object", "type"}


def _has_bitor(node) -> bool:
    """子树里有没有 `X | Y`。"""
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
               for n in ast.walk(node))


def _bitor_touches_a_builtin_type(node) -> bool:
    """`int | str` 会真的在 3.9 上求值；`flags | others` 不会。

    用"操作数里出现内置类型名"把两者分开 —— 是启发式，但对现有代码零误报，
    而对真正危险的那种写法（`X = int | str`）是准的。
    """
    for n in ast.walk(node):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
            for side in (n.left, n.right):
                if isinstance(side, ast.Name) and side.id in _BUILTIN_TYPE_NAMES:
                    return True
    return False


def _annotation_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.returns is not None:
            yield node.returns


def _py39_violations(tree, rel: Path, has_future_annotations: bool) -> list:
    bad = []
    match_node = getattr(ast, "Match", None)      # 3.9 上不存在这个节点

    for node in ast.walk(tree):
        if match_node is not None and isinstance(node, match_node):
            bad.append(_v(rel, node.lineno,
                          "match 语句是 3.10+ 语法 —— 3.9 上直接 SyntaxError"))
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "tomllib":
                    bad.append(_v(rel, node.lineno,
                                  "tomllib 是 3.11+ —— 读 TOML 请用 tomli（走 extras）"
                                  "或自己解析"))
        elif isinstance(node, ast.ImportFrom) and not node.level \
                and (node.module or "").split(".")[0] == "tomllib":
            bad.append(_v(rel, node.lineno,
                          "tomllib 是 3.11+ —— 读 TOML 请用 tomli（走 extras）或自己解析"))
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name in ("dataclass", "field"):
                for kw in node.keywords:
                    if kw.arg in _PY310_DATACLASS_KWARGS:
                        bad.append(_v(rel, kw.value.lineno,
                                      f"{name}({kw.arg}=…) 是 3.10+ 的参数 —— "
                                      f"3.9 上直接 TypeError"))
            if name in ("isinstance", "issubclass"):
                for arg in node.args[1:]:
                    if _has_bitor(arg):
                        bad.append(_v(rel, arg.lineno,
                                      f"{name}() 的实参里出现 `X | Y` —— 这是**运行时求值**的 "
                                      f"PEP 604，3.9 上会 TypeError（注解位置才安全）"))

    if not has_future_annotations:
        for annotation in _annotation_nodes(tree):
            if _has_bitor(annotation):
                bad.append(_v(rel, annotation.lineno,
                              "注解里出现 `X | Y`，但本文件缺 "
                              "`from __future__ import annotations` —— 3.9 上导入即 TypeError"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _bitor_touches_a_builtin_type(node.value):
            bad.append(_v(rel, node.value.lineno,
                          "赋值里出现内置类型名的 `X | Y` —— 运行时求值，3.9 上 TypeError"
                          "（注解位置才由 `from __future__ import annotations` 兜住）"))
    return bad


def _rule_p39() -> list:
    bad = []
    for path, rel in _iter_sources():
        tree, syntax_error = _parse(path, rel)
        if tree is None:
            # 3.9 的解析器根本读不懂 match/case —— 解析失败本身就是最直接的证据。
            bad.append(_v(rel, syntax_error.lineno or 0,
                          f"3.9 无法解析：{syntax_error.msg} —— 若为 match/case 等 3.10+ "
                          f"语法请改写，否则是真实语法错误"))
            continue
        has_future = any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body)
        bad.extend(_py39_violations(tree, rel, has_future))
    return bad


# ---------------------------------------------------------------------------
# 规则表与执行
# ---------------------------------------------------------------------------

RULES = (
    ("C2", "子进程一律 sys.executable（不得把字面量解释器名当可执行名）", _rule_c2),
    ("C3", "全仓不再出现顶级 `core` 包导入", _rule_c3),
    ("R6", "magpie/core/** 顶层 import ⊆ 标准库", _rule_r6),
    ("R7", "零出站网络（socket 连接点 / urllib.request / requests / Python httpx 库）",
     _rule_r7),
    ("P39", "保持 3.9 可移植（无 match / tomllib / dataclass slots / 运行时 PEP 604）",
     _rule_p39),
)


def lint() -> int:
    print("=== 静态红线（lint） ===", flush=True)
    failed = 0
    for code, desc, rule in RULES:
        problems = rule()
        if problems:
            failed += 1
            print(f"!! [{code}] {desc}", flush=True)
            for problem in problems:
                print(f"     {problem}", flush=True)
        else:
            print(f"   [{code}] {desc} —— 通过", flush=True)
    if failed:
        print(f"=== 静态红线：{failed} 条不通过 ===", flush=True)
        return 1
    print(f"=== 静态红线：{len(RULES)} 条全部通过 ===", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 动态部分
# ---------------------------------------------------------------------------

def run(steps: list, label: str) -> int:
    print(f"=== {label} ===", flush=True)
    for step in steps:
        print(f"$ {' '.join(step)}", flush=True)
        code = subprocess.call(step, cwd=str(ROOT))
        if code != 0:
            print(f"!! 失败（退出码 {code}）: {' '.join(step)}", flush=True)
            return code
    print(f"=== {label} 通过 ===", flush=True)
    return 0


def main(argv: list) -> int:
    what = argv[1] if len(argv) > 1 else "unit"
    if what in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if what not in ("lint", "unit", "bench", "all"):
        print(f"未知参数 {what!r}\n\n{USAGE}")
        return 1

    # 把解释器打出来：换解释器时，它让"这次到底验的是谁"一眼可见。
    print(f"解释器: {sys.executable}  ({sys.version.split()[0]})", flush=True)

    if what == "lint":
        return lint()
    if what == "bench":
        os.environ["MAGPIE_PERF_STRICT"] = "1"
        return run(BENCH, "性能契约（严格模式）")

    code = lint()
    if code != 0:
        return code
    if what == "all":
        code = run(UNIT, "单元测试 + 设计验证")
        if code != 0:
            return code
        os.environ["MAGPIE_PERF_STRICT"] = "1"
        return run(BENCH, "性能契约（严格模式）")
    return run(UNIT, "单元测试 + 设计验证")     # unit


if __name__ == "__main__":
    sys.exit(main(sys.argv))
