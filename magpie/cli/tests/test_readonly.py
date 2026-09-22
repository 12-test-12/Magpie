"""只读纪律与非交互纪律（2.2 交付标准的两行）。

两件事各有一组断言，而两组的**形状是相反的**：

  * **只读**：`ls` / `doctor` / `show` / `diff` / `schema` 跑前跑后，库里每一个
    字节都不许变。这组断言的价值全在"有牙齿"上 —— 一条永远绿的只读断言比没有
    更糟，所以末尾有一条**反例**：真的写一次，指纹必须变。
  * **非交互**：不读 stdin、不弹提示。这条不能靠"我看过代码"来交付，因为
    "有没有读 stdin"在代码里看不出来（一次 `input()`、一次 `sys.stdin.read()`、
    一次 argparse 的交互式补全都算）。这里的做法是**把 stdin 挂住不给**：
    命令若真去读，它会在 `wait(timeout)` 里卡住，而卡住就是失败。

第三件是**可消费性**：`magpie ls --json | python -c "import json,sys;json.load(sys.stdin)"`
必须成功 —— 交付标准里写着的那一条，这里**真的架了一条管道**跑它，而不是
在测试进程里 `json.loads(stdout)` 假装。

第四件是**依赖边界**：CLI 不新增任何第三方依赖（`argparse` 是标准库）。
`check.py` 的 R6 只守 `magpie/core/**`，CLI 这一侧此前没有任何东西守着 ——
而"顺手 import 一个 pyyaml 来读配置"正是最不需要理由的那一步。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from conftest import ENV                                              # noqa: E402
from magpie.cli.envelope import EXIT_USAGE                            # noqa: E402
from magpie.core import store                                         # noqa: E402

CLI_DIR = ROOT / "magpie" / "cli"

#: 读命令的样本。`--db` 由各条用例补上。
READ_INVOCATIONS = (
    ("ls", "--limit", "3", "--json"),
    ("ls", "--human"),
    # §3.5 的具名查询走的是同一条读路径（`ls` 的过滤条件），所以只读纪律
    # 在这里一并适用：它们**没有**任何写事实的理由。`lens` 同理 —— 它是
    # 一条新命令，而"新命令是不是只读"正是这组断言该覆盖的新风险。
    ("ls", "--product", "nginx@1.24.0", "--json"),
    ("ls", "--in-cidr", "203.0.113.0/24", "--json"),
    ("ls", "--severity", "critical", "--json"),
    ("lens", "--json"),
    ("lens", "services", "--human"),
    ("show", "1", "--json"),
    ("diff", "--since", "last", "--json"),
    ("doctor", "--json"),
    ("doctor", "--human"),
    ("schema", "--json"),
    ("timeline", "--json"),              # 4.4：纯读，不许碰库
    ("timeline", "--human"),
    ("init", "--dry-run", "--json"),
    ("search", "x", "--json"),          # 未实现，但同样不许碰库
)


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------


def _fingerprint(db: Path) -> dict[str, dict]:
    """全库逐表指纹：行数 + 整表内容的 sha256。

    **逐行内容，不是条数。** 条数相同、内容不同是完全可能的，而"改了一列、
    行数不动"恰恰是最容易发生的一种写。这个口径与 `verify_v32r5.py` 的
    `fingerprint()` 是同一条理由（那里复用的是 `store.snapshot_effective`，
    这里要的是**全表**，所以另写一份，并接受这份重复）。

    只读 `PRAGMA query_only` 的独立连接读 —— 量的人自己不能变成写的人。
    """
    conn = store.connect(db)
    try:
        tables = [str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()  # noqa: S608
            blob = json.dumps([list(r) for r in rows], sort_keys=True, default=str)
            out[table] = {"rows": len(rows),
                          "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()}
        return out
    finally:
        conn.close()


def _cli(args, *, timeout: int = 120) -> int:
    proc = subprocess.run([sys.executable, "-m", "magpie.cli", *args],
                          cwd=str(ROOT), capture_output=True, env=ENV, timeout=timeout)
    return proc.returncode


def test_read_commands_do_not_touch_a_byte(loaded_db):
    """`ls` / `doctor` / `show` / `diff` / `schema` 前后，**整库逐表指纹不变**。

    交付标准写的是"`doctor` 与 `ls` 前后 fact 表行数不变"。这里收紧到
    "每一个字节"：行数不变而某一个字段被改了，是完全可能的一种写 ——
    而那正是这条纪律要防的事。
    """
    before = _fingerprint(loaded_db)
    assert before["entities"]["rows"] > 0, "夹具是空库，这条断言什么都没验到"

    for args in READ_INVOCATIONS:
        _cli([*args, "--db", str(loaded_db)])

    assert _fingerprint(loaded_db) == before


def test_init_on_a_populated_db_does_not_touch_facts(loaded_db):
    """`init` 是本步唯一的写命令，而它**只写 schema**。

    这条断言钉的就是那个区别：建库（让只读命令能用）与"引入写路径"是两回事。
    它跑在一个已经有 52 个实体、98 条观测的库上，事后逐字节比对。
    """
    before = _fingerprint(loaded_db)
    assert _cli(["init", "--db", str(loaded_db), "--json"]) == 0
    after = _fingerprint(loaded_db)
    assert after == before, "init 动了事实表 —— 它只该动 schema_migrations"


def test_the_fingerprint_has_teeth(loaded_db):
    """反例：真的写一次，指纹**必须**变。

    没有这一条，上面两个断言在"指纹函数坏了（比如永远返回同一个值）"时
    仍然全绿 —— 而一个永远绿的只读断言比没有更糟：它让人更安心。
    这里用 `store.log_activity` 写一行审计（最轻的一次真写，而且走**产品自己的
    写入口**，不是一句裸 SQL），并断言它被检出。
    """
    before = _fingerprint(loaded_db)
    conn = store.connect(loaded_db)
    try:
        store.log_activity(conn, actor="selftest", action="gc.retained")
    finally:
        conn.close()
    assert _fingerprint(loaded_db) != before


def test_cli_source_contains_no_write_sql():
    """设计断言：`magpie/cli/**` 里**没有一句 SQL 在写事实**。

    静态扫描而不是运行期断言，理由是运行期只能验"这一次没写"；而这条纪律
    要的是"这条路上不可能写"。核心的写 SQL 全在 `magpie/core/**` 与
    `schema/*.sql` 里，CLI 一个都不该有。

    只扫**非文档字符串**的字符串常量：docstring 里出现 `INSERT` 是在解释
    这件事本身（本模块就是），那不是违规。注释不在 AST 里，天然不计。
    """
    write_verbs = ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE",
                   "ALTER TABLE", "CREATE TABLE")
    offences = []
    for path in sorted(CLI_DIR.rglob("*.py")):
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
    assert not offences, "CLI 里出现了写 SQL：" + "; ".join(offences)


# ---------------------------------------------------------------------------
# 非交互
# ---------------------------------------------------------------------------


def test_no_command_reads_stdin(tmp_path, loaded_db):
    """**stdin 挂住不给**：谁去读它，谁就卡住 —— 而卡住即失败。

    这比"我读过代码"强的地方在于它验的是**行为**：一次 `input()`、一次
    `sys.stdin.read()`、一次 argparse 的交互式补全，在这里都会露出来。
    标准做法（`stdin=DEVNULL`）会立刻给 EOF，读与不读看不出区别 ——
    所以这里刻意留着一个**打开的、永远不写**的 stdin 管道。
    """
    missing = tmp_path / "missing.db"          # 也覆盖"库不存在"这条路径
    invocations = list(READ_INVOCATIONS) + [("ls", "--db", str(missing), "--json")]
    for args in invocations:
        full = [*args, "--db", str(loaded_db)] if "--db" not in args else list(args)
        with open(tmp_path / "out.txt", "wb") as out, \
                open(tmp_path / "err.txt", "wb") as err:
            proc = subprocess.Popen([sys.executable, "-m", "magpie.cli", *full],
                                    cwd=str(ROOT), stdin=subprocess.PIPE,
                                    stdout=out, stderr=err, env=ENV)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail(f"{full} 卡住了 —— 它在等 stdin，而非交互纪律要求它"
                            f"永远不等（需要人决定时应当返回退出码 3）")
            finally:
                proc.stdin.close()


def test_output_does_not_depend_on_a_tty(loaded_db):
    """同一命令在"stdout 是文件"与"stdout 是管道"下必须给**同一个答案**。

    非交互还有一半是"输出不因终端而变"：一个按 `isatty()` 分叉的实现，会让
    脚本拿到的与人看到的**不是同一个东西** —— 而脚本不会告诉你它看到了什么。

    比较的是除 `meta.took_ms` 之外的全部内容。`took_ms` 是一次真实耗时的测量，
    两次运行本来就不同；把它一起比，这条断言就会变成一条随机的红 —— 而一条
    会随机变红的断言，最后一定会被人加一个 `x` 关掉。
    """
    def run_to(stream) -> dict:
        subprocess.run([sys.executable, "-m", "magpie.cli", "ls", "--db",
                        str(loaded_db), "--json", "--limit", "2"],
                       cwd=str(ROOT), stdout=stream, stderr=subprocess.DEVNULL,
                       env=ENV, check=True)

    out_file = loaded_db.parent / "tty_probe.json"
    with open(out_file, "wb") as handle:
        run_to(handle)
    piped = subprocess.run([sys.executable, "-m", "magpie.cli", "ls", "--db",
                            str(loaded_db), "--json", "--limit", "2"],
                           cwd=str(ROOT), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=ENV, check=True)

    to_file = json.loads(out_file.read_bytes().decode("utf-8"))
    to_pipe = json.loads(piped.stdout.decode("utf-8"))
    for payload in (to_file, to_pipe):
        payload["meta"].pop("took_ms")
    assert to_file == to_pipe


def test_invalid_command_with_stdin_closed_still_answers(loaded_db):
    """stdin 关闭 + 无 TTY 时，用法错也要给出信封与正确的退出码。"""
    proc = subprocess.run([sys.executable, "-m", "magpie.cli", "nope"],
                          cwd=str(ROOT), stdin=subprocess.DEVNULL,
                          capture_output=True, env=ENV)
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert proc.returncode == EXIT_USAGE and payload["ok"] is False
    assert payload["error"]["code"] == "E_USAGE"


# ---------------------------------------------------------------------------
# 依赖边界
# ---------------------------------------------------------------------------


def _stdlib_names() -> frozenset[str]:
    """标准库顶层模块名。与 `check.py` 同一条思路：从解释器自己问，不手抄一份。"""
    return frozenset(sys.stdlib_module_names)


def test_cli_adds_no_dependencies():
    """`magpie/cli/**` 的顶层 import ⊆ 标准库 + 自己人。

    交付标准那一行是"CLI 亦不新增（`argparse` 是标准库）"。它值得一条**测试**
    而不是一句注释，因为破坏它的方式极其顺手 —— 而一旦破了，`magpie --help`
    在没有那套 extras 的环境里就不再能用，那正是 2.1 立 `console_scripts` 时
    要保住的东西。

    与 `check.py` 的 R6 是同一个形状，只是守的目录不同（那条守 core，它守 CLI）。
    两份不合并的理由：core 的规则要额外放行测试目录的 pytest，而 CLI 这条
    一个例外都没有 —— 合成一条会得到一个带开关的规则，带开关的规则迟早被关掉。
    """
    stdlib = _stdlib_names()
    bad = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:                       # 只看**模块级** import
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                       # 包内相对 import
                    continue
                names = [node.module or ""]
            else:
                continue
            for module in names:
                top = module.split(".")[0]
                if top in stdlib or top == "magpie":
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno} import {module}")
    assert not bad, "CLI 引入了非标准库依赖：" + "; ".join(bad)


# ---------------------------------------------------------------------------
# 可消费性
# ---------------------------------------------------------------------------


def test_json_pipe_is_consumable_without_pythonioencoding(loaded_db):
    """交付标准那一条，**真的架一条管道**跑它。

        magpie ls --json | python -c "import json,sys;json.load(sys.stdin)"

    刻意**不带** `PYTHONIOENCODING`：Windows 上消费者的 stdin 默认按 GBK 解，
    只要信封里有一个中文（项目名、identity_rule 的正文都是中文），
    UTF-8 字节被按 GBK 解就会当场 UnicodeDecodeError。`--json` 输出纯 ASCII
    （`ensure_ascii=True`）就是为了让这一条**不依赖环境变量**也能成立 ——
    下一条断言直接把那个性质本身钉住。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    produced = subprocess.run(
        [sys.executable, "-m", "magpie.cli", "ls", "--db", str(loaded_db), "--json"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert produced.returncode == 0, produced.stderr.decode("utf-8", "replace")

    consumer = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; d=json.load(sys.stdin); print(d['data']['count'])"],
        input=produced.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert consumer.returncode == 0, consumer.stderr.decode("utf-8", "replace")
    assert int(consumer.stdout.strip()) > 0


def test_json_output_is_pure_ascii(loaded_db):
    """`--json` 的输出**一个非 ASCII 字节都没有**（`ensure_ascii=True`）。

    这不是保守，是上面那条可消费性的**实现**：中文项目名走的是
    `\\uXXXX` 转义，任何按 GBK / UTF-8 / Latin-1 解它的消费者都读得对。
    代价是终端里不可读 —— 而给人读的是 `--human`，两者各管一头。
    """
    for args in (("ls", "--json"), ("schema", "--json"), ("doctor", "--json")):
        out = subprocess.run([sys.executable, "-m", "magpie.cli", *args,
                              "--db", str(loaded_db)],
                             cwd=str(ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=ENV).stdout
        assert out.decode("ascii"), f"{args} 的 --json 输出不是纯 ASCII"
