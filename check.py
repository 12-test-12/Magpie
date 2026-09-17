"""一条命令跑全绿（阶段 0 的可重复入口；2.1 起把静态红线也收了进来）。

    py -3.13 check.py          # 静态红线 + 单元测试 + 两个设计验证脚本
    py -3.13 check.py lint     # 只跑静态红线（秒级）
    py -3.13 check.py bench    # 严格性能契约（MAGPIE_PERF_STRICT=1）
    py -3.13 check.py all      # 上面全部

**一律用 `py -3.13` 显式调用，不要依赖 PATH 顺序** —— 已在运行的进程可能继承
改动前的 PATH，`python` 不一定等于 3.13（本机实测：`python` → 3.9.1，
`py -3.13` → 3.13.15）。check.py 自己也拦这件事：解释器低于 `pyproject.toml`
声明的 `requires-python` 下限时**直接拒绝执行**，而不是给一个无意义的绿。

为什么 `bench` 必须能**单独**触发: `MAGPIE_PERF_STRICT=1` 是 §9.2 性能承诺
唯一可验证的形式（1M 行 ≤ 60s、单 job undo ≤ 1.5s、导入 10 万观测 < 60s）。
把它埋进"跑全部"里，它就再也无法被单独质问"现在到底达不达标"。

`bench` 里那条导入契约（`bench/test_perf_ingest.py`）真跑一次 10 万观测的导入，
约 55s —— 比另外两条贵得多。这是刻意的：性能承诺的代价不该被藏起来，
而且它只在 `bench` / `all` 里跑，不影响秒级的 `lint` 与分钟级的 `unit`。

为什么 `lint` 也必须能**单独**触发: 静态红线是秒级的，单元测试是分钟级的。
红线只有在"改一行就能立刻验"的时候才会真的被用起来；混在一起的结果是没人
为了验一条 import 规则去等半分钟。

为什么子进程一律 `sys.executable`（坑 2）: 写死 `"python"` 会按 PATH 解析，
于是在 `py -3.13 check.py` 之下，测试却跑在 PATH 上的 3.9 上 —— 表面全绿，实际
验的是另一个解释器。**它是"你跑哪个解释器，测试就跑在哪个解释器上"的唯一
保证**，也是本节所有"已在 3.13 上验证"结论成立的前提。`C2` 规则把这条纪律
变成了断言。

静态红线一览（`py -3.13 check.py lint`）:
    C2        子进程一律 sys.executable，不得把字面量解释器名当可执行名（坑 2）
    C3        全仓不再出现顶级 `core` 包导入（坑 3 迁移后的防回流）
    R6        magpie/core/** 顶层 import ⊆ 标准库（core 只用标准库）
    R7        零出站：无 socket 连接点 / urllib.request / requests / Python httpx 库
    PARSE-RO  magpie/core/parsers/** 不得 import 写入口（解析器只读文件）
    PARSE-NORM 解析器只调用 normalize.py，不得内联自己的归一化
    FLOOR     不超出声明下限的语法 / 标准库 API / 运行时求值

关于 PARSE-RO / PARSE-NORM（2.4 的两条硬线）:

  **PARSE-RO** —— 解析器一旦能 import `ops` / `store`，"一切入口走管线"就没了，
  而那是 R1"写入口唯一"的全部价值。顺带禁掉 `sqlite3`：解析器读的是**文件**。

  **PARSE-NORM** —— 计划书 §3.6 要防的是 `Nginx` / `nginx` / `NGINX/1.24` 三种
  写法同时入库。防它的唯一办法是"同一件事只有一种写法"这个入口**真的只有一个**，
  所以这里禁掉的不是某个函数名，而是**重新实现归一化的能力**：`re` / `ipaddress` /
  `idna` / `unicodedata` 一律不许 import，`.lower()` / `.casefold()` 不许调 ——
  项目里只有 `normalize.py` 有资格做这些事。

  两条规则都有**反例自检**（`_selftest_parse_rules`）：没有自检的静态规则很可能是
  "碰巧永远不报"，那和没写没有区别。

关于 FLOOR: 下限取自 `pyproject.toml` 的 `requires-python`（**唯一事实来源**，
check.py 直接读它，不再另写一份 —— 两处各写一个版本号，迟早会分叉）。判定分三层，
因为**一层不够**：

  1. 语法层 —— `ast.parse(feature_version=下限)`，由解释器自己的语法表说了算，
     不手工维护"哪些语法是新的"；
  2. 标准库 API 层 —— 语法表管不到新增的 API，另列一份小清单（`_NEW_APIS`）；
  3. 运行时求值层 —— `isinstance(x, int | str)`、`X = int | str`、缺
     `from __future__ import annotations` 时注解里的 `X | Y`：**语法在任何
     版本都合法**，前两层都看不见它们，只有在旧解释器上求值才炸。

于是 `match` / `tomllib`（≤3.11）放行，PEP 695 泛型 / `type` 语句 /
`itertools.batched`（3.12+）被拦下，而上面第 3 类写法同样被拦下。

⚠️ 第 3 层是**补回来**的一次退化修复：2.1 早先的 `P39` 规则覆盖过它，改写成
FLOOR 时只保留了前两层，使它静默失效了一段时间。删规则时请逐条对照 P39 的
覆盖面，别只对照它的名字。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

UNIT = [
    [sys.executable, "-m", "pytest", "magpie/core/tests", "-q"],
    [sys.executable, "verify_v32r2.py"],
    [sys.executable, "verify_v32r3.py"],
]
BENCH = [
    [sys.executable, "-m", "pytest", "bench/test_perf.py", "-q"],
    # 2.4 兑现的契约项：单文件解析吞吐（10 万行 < 10s 量级，实测约 5s）
    [sys.executable, "-m", "pytest", "bench/test_perf_parse.py", "-q"],
    # 2.5 兑现的契约项：导入 10 万观测 < 60s（实跑一次完整导入，约 55s）
    [sys.executable, "-m", "pytest", "bench/test_perf_ingest.py", "-q"],
]

USAGE = __doc__


# ---------------------------------------------------------------------------
# 静态红线：公共零件
# ---------------------------------------------------------------------------

_PARSE_CACHE: dict = {}
_STDLIB_CACHE = None


def _declared_floor():
    """从 `pyproject.toml` 的 `requires-python` 读语言下限，返回 (3, 11) 这样的元组。

    刻意只做**定点匹配**，不引入 TOML 解析器：check.py 只带 pytest 这一个 dev
    依赖，而 `tomllib` 在 3.11 以下不存在。为一个字段塞一个自制 TOML 解析器不
    划算，读不到就返回 () 让调用方**响亮地失败** —— 宁可报错，也不要静默用一
    个猜出来的下限去判定全仓代码。
    """
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ()
    match = re.search(r'^requires-python\s*=\s*"[^"]*>=\s*(\d+)\.(\d+)"',
                      text, re.MULTILINE)
    if not match:
        return ()
    return (int(match.group(1)), int(match.group(2)))


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

    `sys.stdlib_module_names` 是 3.10+ 才有的 —— 而 `main()` 已经拒绝在下限
    以下的解释器上运行（下限 ≥ 3.11），所以这里可以直接用，不必再兜底枚举
    stdlib 目录。这正是"把下限守在前面"换来的简化。
    """
    global _STDLIB_CACHE
    if _STDLIB_CACHE is None:
        _STDLIB_CACHE = set(sys.stdlib_module_names)
    return _STDLIB_CACHE


# ---------------------------------------------------------------------------
# C2 · 子进程一律 sys.executable（坑 2 的反例断言）
# ---------------------------------------------------------------------------
#
# **已知边界（不是完备保证）**：C2 只认**字面量**命令头。`PY = "python";
# subprocess.call([PY, "-V"])` 这类把可执行名放进变量再用的写法**不会被检出**。
# 堵它需要数据流分析，代价远大于收益。所以 C2 保证的是"没人把字面量解释器名
# 直接写进命令表"——历史上真实发生过的那一种，而不是"子进程一定是
# sys.executable"这个更强的命题。别把它读成后者。

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
# PARSE-RO / PARSE-NORM · 解析器的两条硬线（2.4）
# ---------------------------------------------------------------------------

#: 解析器包。用 parts 前缀而不是字符串前缀：`parsers_bridge.py`（2.5 的过渡实现）
#: **不在**这个包内，它不适用这两条规则 —— 它本来就是为了先跑通管线而生的临时实现。
_PARSER_PACKAGE = ("magpie", "core", "parsers")

#: 解析器不得触碰的东西：两个写入口 + sqlite3。
#: 按**导入名的最后一段**判定，于是 `import magpie.core.ops`、`from .. import ops`、
#: `from ..store import x` 三种写法会被同一条规则抓住。
_PARSER_FORBIDDEN_MODULES = {"ops", "store", "sqlite3"}

#: 内联归一化的两种形态：靠这些模块自己动手，或直接调这些字符串方法。
_NORM_FORBIDDEN_MODULES = {"re", "ipaddress", "idna", "unicodedata"}
_NORM_FORBIDDEN_METHODS = {"lower", "casefold"}


def _is_parser_module(rel) -> bool:
    return tuple(rel.parts[:3]) == _PARSER_PACKAGE


def _import_names(tree):
    """产出 (解析用的模块名, 行号)。

    相对导入拼成带点的形态：`from ..store import x` → `..store`，
    `from .. import ops` → `..ops`。调用方只看**最后一段**，两种形态因此等价。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            if node.module is None:                 # `from .. import ops`
                for alias in node.names:
                    yield prefix + alias.name, node.lineno
            else:
                yield prefix, node.lineno


def _defines_a_parser(tree) -> bool:
    """本模块有没有定义一个具体的解析器（`class X(Parser)`）。

    PARSE-NORM 只对**格式解析器**生效：`base.py`（契约本身）与 `__init__.py`
    （包的说明）不产出任何记录，也就没有归一化可内联。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if any(isinstance(b, ast.Name) and b.id == "Parser" for b in node.bases):
                return True
    return False


def _parse_ro_problems(rel, tree) -> list:
    bad = []
    for module, lineno in _import_names(tree):
        if module.split(".")[-1] in _PARSER_FORBIDDEN_MODULES:
            bad.append(_v(rel, lineno,
                          f"import {module!r} —— PARSE-RO：解析器只读文件，"
                          f"写入是 ingest 的职责（R1 的全部价值靠这条守着）"))
    return bad


def _parse_norm_problems(rel, tree) -> list:
    bad = []
    for module, lineno in _import_names(tree):
        if module.split(".")[-1] in _NORM_FORBIDDEN_MODULES:
            bad.append(_v(rel, lineno,
                          f"import {module!r} —— PARSE-NORM：归一化只能调 "
                          f"magpie/core/normalize.py，不得自己动手（否则 "
                          f"`Nginx` / `nginx` / `NGINX/1.24` 会同时入库）"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _NORM_FORBIDDEN_METHODS:
                bad.append(_v(rel, node.lineno,
                              f".{node.func.attr}() —— PARSE-NORM：大小写折叠是"
                              f"归一化的活，请调 normalize.py"))
    return bad


def _selftest_parse_rules() -> list:
    """反例自检：该报的报、不该报的不报。"""
    problems = []

    ro_proof = "from .. import ops\n"
    ro_control = "from .. import normalize\n"
    if not _parse_ro_problems(Path("magpie/core/parsers/x.py"),
                              ast.parse(ro_proof)):
        problems.append("PARSE-RO 自检失败：反例 `from .. import ops` 未被检出")
    if _parse_ro_problems(Path("magpie/core/parsers/x.py"), ast.parse(ro_control)):
        problems.append("PARSE-NORM/RO 自检失败：`from .. import normalize` 被误报")

    if not _parse_ro_problems(Path("magpie/core/parsers/x.py"),
                              ast.parse("import sqlite3\n")):
        problems.append("PARSE-RO 自检失败：`import sqlite3` 未被检出")

    norm_proof = 'def f(x):\n    return x.lower()\n'
    if not _parse_norm_problems(Path("magpie/core/parsers/x.py"),
                                ast.parse(norm_proof)):
        problems.append("PARSE-NORM 自检失败：反例 `.lower()` 未被检出")
    if not _parse_norm_problems(Path("magpie/core/parsers/x.py"),
                                ast.parse("import ipaddress\n")):
        problems.append("PARSE-NORM 自检失败：反例 `import ipaddress` 未被检出")
    norm_control = ("from .. import normalize\n\n"
                    "def f(x):\n    return normalize.normalize_domain(x)\n")
    if _parse_norm_problems(Path("magpie/core/parsers/x.py"),
                            ast.parse(norm_control)):
        problems.append("PARSE-NORM 自检失败：规范的归一化调用被误报")
    return problems


def _rule_parse_ro() -> list:
    bad = _selftest_parse_rules()
    for path, rel in _iter_sources():
        if not _is_parser_module(rel):
            continue
        tree, _ = _parse(path, rel)
        if tree is not None:
            bad.extend(_parse_ro_problems(rel, tree))
    return bad


def _rule_parse_norm() -> list:
    bad = []
    for path, rel in _iter_sources():
        if not _is_parser_module(rel):
            continue
        tree, _ = _parse(path, rel)
        if tree is None:
            continue
        if not _defines_a_parser(tree):
            continue
        if not any(m.split(".")[-1] == "normalize"
                   for m, _ in _import_names(tree)):
            bad.append(_v(rel, 1, "解析器没有 import normalize —— PARSE-NORM："
                                  "归一化必须走唯一入口，不得内联"))
        bad.extend(_parse_norm_problems(rel, tree))
    return bad


# ---------------------------------------------------------------------------
# FLOOR · 不超出声明下限的语法与标准库 API
# ---------------------------------------------------------------------------

# `feature_version` 管得住**语法**，管不住**标准库新增的 API** —— 后者只能列
# 清单。这里刻意只收"这个项目真有可能用上、且一旦用上会在旧解释器上直接炸"
# 的那些，不追求穷尽：一个短清单能被认真读完，一份穷尽清单只会被跳过。
# 值是该 API 的引入版本，写进报错信息里，省得再去查。
_NEW_APIS = {
    "itertools.batched": "3.12",
    "typing.override": "3.12",
    "typing.TypeAliasType": "3.12",
    "typing.ReadOnly": "3.13",
    "sys.last_exc": "3.12",
    "copy.replace": "3.13",
    "collections.abc.Buffer": "3.12",
    "os.path.isjunction": "3.12",
    "os.path.splitroot": "3.12",
    "os.path.isreserved": "3.13",
    "pathlib.Path.walk": "3.12",
    "pathlib.Path.full_match": "3.13",
}


# 运行时求值的 PEP 604 检测（`isinstance(x, int | str)` / `X = int | str`）。
# 为什么单列一层：这些写法的**语法**在任何版本都合法，`feature_version` 压根
# 看不见它们 —— 它们是在旧解释器上**求值**时才炸。
_BUILTIN_TYPE_NAMES = {"int", "str", "bytes", "bytearray", "float", "bool", "complex",
                       "list", "dict", "set", "frozenset", "tuple", "object", "type"}


def _has_bitor(node) -> bool:
    """子树里有没有 `X | Y`。"""
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
               for n in ast.walk(node))


def _bitor_touches_a_builtin_type(node) -> bool:
    """`int | str` 会真的求值；`flags | others` 不会。

    用"操作数里出现内建类型名"把两者分开 —— 是启发式，但对现有代码零误报，
    而对真正危险的那种写法是准的。
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


def _has_future_annotations(tree) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body)


def _pep604_runtime_violations(tree, rel: Path) -> list:
    """`X | Y` 在**运行时被求值**的位置 —— 注解位置由 future import 赦免。

    3.9 上 `int | str` 求值即 TypeError；10 岁的老解释器上跑不了的就是这种。
    """
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name in ("isinstance", "issubclass"):
                for arg in node.args[1:]:
                    if _has_bitor(arg):
                        bad.append(_v(rel, arg.lineno,
                                      f"{name}() 的实参里出现 `X | Y` —— 这是**运行时求值**的 "
                                      f"PEP 604，旧解释器上会直接 TypeError"))
        if isinstance(node, ast.Assign) and _bitor_touches_a_builtin_type(node.value):
            bad.append(_v(rel, node.value.lineno,
                          "赋值里出现内建类型名的 `X | Y` —— 运行时求值，旧解释器上 TypeError"))

    if not _has_future_annotations(tree):
        for annotation in _annotation_nodes(tree):
            if _has_bitor(annotation):
                bad.append(_v(rel, annotation.lineno,
                              "注解里出现 `X | Y`，但本文件缺 "
                              "`from __future__ import annotations` —— 旧解释器上导入即 TypeError"))
    return bad


def _qualified_bindings(tree) -> dict:
    """本地名 → 全限定名。`import os` → {"os": "os"}；`import a.b as c` → {"c": "a.b"}；
    `from pathlib import Path` → {"Path": "pathlib.Path"}。"""
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                bindings[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _dotted(node):
    """还原 `os.path.isjunction` / `Path("x").walk` 这样的链；认不出就返回 None。

    刻意只做这一层浅解析：把 `Call` 的 `func` 当作它的"类型"
    （`Path("x").walk` → `pathlib.Path.walk`）。再深就是数据流分析，不划算。
    **已知边界**：新 API 挂在变量上时漏检（`p = Path(x); p.walk()`）——
    这类只能靠 code review，规则不假装能覆盖。
    """
    parts = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        else:
            return None


def _rule_floor() -> list:
    floor = _declared_floor()
    if not floor:
        return ["读不到 pyproject.toml 的 requires-python —— FLOOR 规则无从判定"]
    bad = []
    for path, rel in _iter_sources():
        source = path.read_text(encoding="utf-8")

        # 语法层：让解释器自己的语法表来判，不手工维护"哪些语法是新的"。
        try:
            ast.parse(source, filename=rel.as_posix(), feature_version=floor)
        except SyntaxError as exc:
            # 失败有两种可能：真的用了更新的语法，或者文件本身就写坏了。
            # 用本解释器的默认语法再解一次就能分辨 —— 别把普通语法错误也说成
            # "超出下限"，那会把人引到错的方向上去。
            try:
                ast.parse(source, filename=rel.as_posix())
            except SyntaxError as plain:
                bad.append(_v(rel, plain.lineno or 0, f"语法错误：{plain.msg}"))
            else:
                bad.append(_v(rel, exc.lineno or 0,
                              f"语法超出声明下限 {floor[0]}.{floor[1]}：{exc.msg} "
                              f"—— 要么改写，要么在确认过上限之后把 requires-python 抬上去"))
            continue

        tree, _ = _parse(path, rel)
        if tree is None:
            continue

        bindings = _qualified_bindings(tree)
        for node in ast.walk(tree):
            # API 层：属性链形态（itertools.batched(…) / Path.walk(…) / os.path.isjunction）
            if isinstance(node, ast.Attribute):
                dotted = _dotted(node)
                if dotted:
                    head, _, tail = dotted.partition(".")
                    full = f"{bindings.get(head, head)}.{tail}" if tail else dotted
                    if full in _NEW_APIS:
                        bad.append(_v(rel, node.lineno,
                                      f"{full} 是 {_NEW_APIS[full]}+ 的 API，"
                                      f"超出声明下限 {floor[0]}.{floor[1]}"))
            # API 层：from X import Y 直接绑定（你不可能不 import 就用上它）
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    if full in _NEW_APIS:
                        bad.append(_v(rel, node.lineno,
                                      f"from {node.module} import {alias.name} —— "
                                      f"{full} 是 {_NEW_APIS[full]}+ 的 API，"
                                      f"超出声明下限 {floor[0]}.{floor[1]}"))

        # 运行时层：语法合法、到旧解释器上**求值**时才炸的 PEP 604。语法表看不见
        # 它们，API 清单也收不住它们 —— 只能单独看"求值位置"。
        bad.extend(_pep604_runtime_violations(tree, rel))
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
    ("PARSE-RO", "magpie/core/parsers/** 不得 import 写入口（ops / store / sqlite3）",
     _rule_parse_ro),
    ("PARSE-NORM", "解析器只调用 normalize.py，不得内联自己的归一化",
     _rule_parse_norm),
    ("FLOOR", "不超出声明下限（pyproject.toml 的 requires-python）的语法 / 标准库 API / "
              "运行时求值",
     _rule_floor),
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

    # 下限守卫 —— 这一条比 C2 更靠前，因为它挡的是"整轮验证跑错了运行时"。
    # 在 3.9 上跑出来的 84 passed，对"这段代码在 3.11+ 上对不对"什么也没证明。
    floor = _declared_floor()
    if not floor:
        print("!! pyproject.toml 里读不到 requires-python —— 无法判定语言下限，拒绝执行。",
              flush=True)
        return 1
    if sys.version_info[:2] < floor:
        print(f"!! 解释器 {sys.version.split()[0]} 低于 pyproject.toml 声明的下限 "
              f"{floor[0]}.{floor[1]} —— 在它上面跑出来的绿是假的，拒绝执行。", flush=True)
        print(f"   当前解释器: {sys.executable}", flush=True)
        print(f"   请显式用工程解释器调用: py -3.13 check.py ...", flush=True)
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
