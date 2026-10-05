"""G5 出口 Gate：把阶段 5 的两条验收固化成可重复执行的脚本（照 G3/G4 的做法）。

开发实施计划 §4 的 G5 那一行是这两条：

    **G5 = ① MCP 默认只读（专项断言）；② 上下文包零出站。**

（0.21.0 起工具面加了唯一写通道 `add_finding` —— A 段的"只读"断言按
`READONLY_TOOLS ∪ WRITE_TOOLS` 的口径加宽：只读断言只对只读工具成立，
写工具由 A2e 单独证明"只动自己的副本"。）

外加每阶段出口的常规全量复验（C 段接线 + `check.py` / `check.py bench` +
fresh clone —— 后两条跑在本脚本之外，结论附在交付评论里）。

两条专项各答一个"没自动化就会在某天悄悄失效"的问题，本脚本把它们落成
A / B 两段 + C 段接线 + D 段进程内反例自检 + 四个外部反例开关。纪律照抄
G3/G4：**断言不许手抄清单**（一切清单从真注册表 / 真代码算）、**反例开关每个
取值都要真跑红过**（一次性实测证据写在交付评论，不进本脚本）、**扫描器自己写**
（不拿被考的人的实现去验被考的人的结论）。

**专项① · MCP 默认只读 + 唯一写通道（A 段）** —— 六个子断言各堵一种失效方式：

  A1 **工具清单从真实注册表算**：`build_registry(真契约)` ≡
     `READONLY_TOOLS ∪ WRITE_TOOLS`（双向分开报，红的含义不同），外加
     "派生真的跟契约走"的牙齿 —— 喂一份改过分类的契约，注册表必须变
     （含"把 add_finding 的 access 伪造成 read → 必须掉面"，A1e）。
     手抄的清单在那条牙齿下不会变，当场红。
  A2 **只读工具调用前后库快照不变**：全部**只读**注册工具 × 空实参 / 最小
     实参两轮（写工具在循环里被排除 —— 排除的正当性由 A2e 证明），快照 =
     **逐表行数 + 整表内容 sha256**（表和视图都算，审计 / 作业那几张
     表 `activity_log` / `jobs` / `work_journal` 必须在场，否则"审计没变"是
     空转的 0）。非空转：至少一次调用 ok。牙齿：对库副本经**产品自己的写入口**
     （`ops.create_project`，R1 的唯一写入口）写一笔，快照必须变。
     **A2e**：写工具（恰一个）在库副本上真调一次 → 副本快照变 + `ok`，
     证明"排除出 A2b 没藏东西、写通道真的能写"。
  A3 **AST 断言**：`magpie/adapters/**` 无写入口 import（`core.ops` /
     `core.store` / `cli.write` / `sqlite3`）—— 扫描器本脚本自己写（相对
     import 按 PEP 328 解析成绝对名再比），牙齿在 D2。
  A4 **双向清单校验**：实际注册 ≡ 声明（防白名单提前扩张、防声明空转）——
     与 A1 同源但分开报：A1 钉"算得对"，A4 钉"两边对得上"。
  A5 **stdio 全链路抽测**：真起 `python -m magpie.adapters.mcp` 子进程走
     stdin/stdout —— initialize → notifications/initialized → tools/list →
     tools/call。stdout 必须**恰好 3 行、每行可解析 JSON**（帮助文本 /
     traceback 混进协议流当场红），tools/list 的名字集合与本进程的声明清单
     逐项相等，tools/call 回 `ok: true` 的信封。

**专项② · 上下文包零出站（B 段）** —— 四个子断言：

  B1 **socket 封死下的端到端**：`socket.connect / connect_ex /
     create_connection` 全部钉死（一连就炸并记账），再走**完整 HTTP 栈**
     （create_app + TestClient）调 `GET /api/v1/agent/context?entity_id=&depth=1`
     —— 查询串与 GUI 按钮发出去的一字不差 —— 断言 200 + `data` 恰好
     `{json, markdown}` 双形状 + markdown 首行 `# ` + 出站尝试 0。
     静态红线验"没人写出站代码"，这条验"端点跑起来真的没伸手" —— 两层防的
     不是同一件事（同 `test_agent_context_offline` 的分工，这里是出口 Gate
     自己的那一份，谁少了都会在出口那一刻被看见）。
  B2 **R7 四向全验**：跑 `check.py` 自己的 `_r7_audit`（R7 本体住在那 ——
     在这里复刻一份只会制造第三份真相），在**真仓源码 + 真白名单**上逐向
     断言：(a) 白名单结构恰好 `{magpie/cli/ask.py}`（5.4 改造后的现值）、
     (b) 白名单之外零出站点、(c) 声明 ⊆ 实际（ask.py 真有出站点 —— 本脚本
     自己再解析一遍它的 AST，不依赖 `_r7_points` 的口径）、(d) 子进程
     curl/wget 禁令对**谁**生效（白名单文件 / 台架 / 普通产品文件三处牙齿）。
     四个方向各配构造样本反例 + 对照 —— 每次跑都验，不会锈。
  B3 **前端静态扫（R7-ASSETS 的加严）**：红线只扫 `magpie/api/web/**`
     （构建产物），这里把**源码侧** `frontend/src/**` + `frontend/index.html`
     一并扫，外加产物侧 —— 两处落点逐文件搜外部域名（块注释与整行 `//`
     先剥掉：注释里的 `fonts.googleapis.com` 是在解释"为什么不引"，浏览器
     不会去取它；字符串与行尾注释不剥，保守方向宁可误报）。**已知边界**：
     拼接字符串（`"ht" + "tps://…"`）扫不到；`frontend/tests/**` 不在扫描
     范围 —— 那里的 example.com 是夹具数据，不随产品发布（与 R7 不扫
     bench 夹具同一口径）。
  B4 **「复制给 AI」按钮不出本机**：代码面审（`DetailPanel.vue` 走同源相对
     路径 `/agent/context`、`navigator.clipboard.writeText`、全文件零外部
     域名；`api.js` 的 `API_PREFIX = '/api/v1'` 是相对前缀）+ vitest 已有
     断言抽查（`agent_context.test.js` 钉死了端点字符串、Markdown 进剪贴板、
     端点失败时剪贴板不被碰 —— 这里断言那些钉子还在，npm 不进默认执行，
     vitest 实跑作为交付评论的一次性证据）。

**接线（C 段）**：本脚本在 `check.py` 默认执行里；adapters 与 api 两套
pytest 专项在默认执行里；`check.py` 现行 11 条红线一条没动（规则表逐名钉死）。

**外部反例开关**（与 `MAGPIE_VERIFY_G3/G4_BREAK` 同用途；全部**进程内 / 数据
注入**，不碰盘上文件 —— 改真文件再改回来那种反例会在某次跑挂时把仓库留在脏
状态。四个取值各打一段，未知取值拒绝执行）：

    MAGPIE_VERIFY_G5_BREAK=mcp      把 EXCLUDED_READ_COMMANDS 里的 ask 拔掉
                                    （模拟白名单提前扩张）→ A1/A2 必须红
    MAGPIE_VERIFY_G5_BREAK=context  把上下文包渲染器打断 → B1 必须红（500/形状坏）
    MAGPIE_VERIFY_G5_BREAK=r7       往扫描集注入一个白名单外的出站文件 → B2 必须红
    MAGPIE_VERIFY_G5_BREAK=assets   往前端扫描集注入一个 CDN 引用 → B3 必须红

分工：本脚本**不替代** pytest —— `adapters/tests` 是逐工具 / 逐协议的合同，
`test_agent_context_offline` 是端点的行为合同；本段是**出口那一刻的两条专项
断言 + 接线 + 反例牙齿**，与 pytest 谁少了都会在出口那一刻被看见。

运行:  py -3.13 verify_g5.py
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import check as check_mod                                             # noqa: E402
from magpie.adapters import mcp                                       # noqa: E402
from magpie.cli import Context, build_parser                          # noqa: E402
from magpie.cli.db import Database                                    # noqa: E402
from magpie.cli.read import cmd_schema                                # noqa: E402

WORK = ROOT / "_scratch" / "verify_g5"

PASS: list = []

#: 外部反例开关（见模块 docstring）。
BREAK = os.environ.get("MAGPIE_VERIFY_G5_BREAK", "").strip()
_BREAK_VALUES = ("", "mcp", "context", "r7", "assets")

#: 子进程环境：与 CLI 契约测试同一份纪律 —— PYTHONIOENCODING 防 GBK 炸中文，
#: MAGPIE_ASK_* 一律剥掉（开发机的开启态不许渗进验证）。
ENV = {k: v for k, v in os.environ.items() if not k.startswith("MAGPIE_ASK_")}
ENV["PYTHONIOENCODING"] = "utf-8"


def check(title, cond, detail=""):
    PASS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


def note(title, detail=""):
    """报告而不判 —— 一条说清了自己不判的报告，不会训练人忽略红色。"""
    print(f"[note] {title}  {detail}")


def _section(fn, name: str) -> None:
    """一段的异常不算脚本崩 —— 记成该段的 FAIL，让其余段照常跑完。

    反例开关下这一段可能以异常收场（例如服务拒绝启动），出口 Gate 要的是
    「哪一段红了」这种可读的证据，不是一行 traceback。
    """
    print()
    print(f"=== {name} ===")
    try:
        fn()
    except Exception as exc:                                          # noqa: BLE001
        check(f"{name} 段执行异常", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 纯函数零件：清单实算 / 库快照 / 两个扫描器 / 四向审计（数据可注入）
# ---------------------------------------------------------------------------


def load_schema_data(db_path: Path) -> dict:
    """跑一次 `cmd_schema` 拿真契约（只读连接，用完即关）。

    刻意**不走 `McpServer.__init__`**：那个构造函数自带漂移闸（清单与声明不符
    时抛 RuntimeError）。A1 要验的恰恰是"算出来的清单与声明对不对得上" ——
    让闸先炸，断言就没机会说话了。A2 再用 `McpServer`（那时闸该炸就炸）。
    """
    db = Database(db_path, read_only=True)
    try:
        args = argparse.Namespace(project=None)
        ctx = Context(args=args, db=db, parser=build_parser())
        data, _human = cmd_schema(args, ctx)
        return data
    finally:
        db.close()


def derive_manifest(schema_data: dict) -> dict:
    """真契约 → 工具注册表。**纯函数**：清单、描述、枚举全从参数算。"""
    return mcp.build_registry(schema_data)


def db_snapshot(db_path: Path) -> dict:
    """库快照：`{表/视图名: (行数, 全表内容 sha256)}` —— 表**和**视图都算。

    行数回答"有没有多一行 / 少一行"，内容哈希回答"有没有 UPDATE 原地改"。
    审计与作业记录（`activity_log` / `jobs` / `work_journal`）都覆盖在内 ——
    调用方另行断言它们**在场**，否则"审计没变"可能只是没扫到。
    顺序无关（逐行 repr 后排序再哈希）：两次快照比的是内容，不是取行顺序。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        snap: dict = {}
        for row in rows:
            name = row["name"]
            records = conn.execute(f'SELECT * FROM "{name}"').fetchall()  # noqa: S608
            payload = "\n".join(sorted(repr(tuple(r)) for r in records))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            snap[name] = (len(records), digest)
        return snap
    finally:
        conn.close()


#: 写入口 import 的**完整模块名**（相对 import 先按 PEP 328 解析成绝对名）。
#: 扫描器是本脚本自己写的 —— 不复用 `adapters/tests/test_static.py` 的实现
#: 去验它的结论（G4 判据 1 的同一条纪律）。
FORBIDDEN_ADAPTER_IMPORTS = ("sqlite3", "magpie.core.ops", "magpie.core.store",
                             "magpie.cli.write")


def adapter_write_imports(source: str, rel: Path) -> list:
    """源码 → 违规 import 列表 `[(模块名, 行号)]`。**纯函数**（牙齿在 D2）。

    相对 import 的解析基准是 `rel` 这个文件在包里的位置：
    `from ..core import ops`（在 magpie/adapters/mcp.py 里）→ `magpie.core.ops`。
    `from X import name` 额外产出 `X.name` —— name 若恰好是子模块
    （`from magpie.cli import write`），那正是要抓的形态。
    """
    package = list(rel.parent.parts)
    bad = []
    tree = ast.parse(source, filename=rel.as_posix())
    for node in ast.walk(tree):
        candidates: list = []
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                head = ".".join(base)
                full = f"{head}.{node.module}" if node.module else head
            else:
                full = node.module or ""
            if full:
                candidates.append(full)
            if node.module or node.level:
                candidates.extend(f"{full}.{alias.name}" for alias in node.names)
        for module in candidates:
            if any(module == f or module.startswith(f + ".")
                   for f in FORBIDDEN_ADAPTER_IMPORTS):
                bad.append((module, node.lineno))
    return bad


#: 资产扫描的允许主机。XML 命名空间是标识符，不是取回目标（与 check.py 的
#: R7-ASSETS 同一条裁定）。
ALLOWED_ASSET_HOSTS = frozenset({"www.w3.org"})

#: 外部主机的形状：`https://host` 与协议相对 `//host`（CDN 最省事的写法）。
#: 主机名末尾必须是字母（TLD），`127.0.0.1:8000` 天然不匹配。
_ASSET_URL_RE = re.compile(r"(?:https?:)?//([A-Za-z0-9._-]+\.[A-Za-z]{2,})")

_ASSET_SKIP_SUFFIX = frozenset({".png", ".ico", ".gif", ".jpg", ".jpeg", ".webp",
                               ".woff", ".woff2", ".ttf", ".eot"})

#: 注释剥离：块注释（CSS / JS / Vue / HTML）与**整行** `//`。字符串字面量与
#: 行尾注释**不剥** —— 保守方向：宁可误报，不可漏报一个真引用。
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?m)^\s*//.*$")


def strip_asset_comments(text: str, suffix: str) -> str:
    """剥注释后再搜 —— 注释里的外部域名是**解释**（"为什么不引 CDN"），浏览器不会取它。

    `.html` 只剥 `<!-- -->`：HTML 里没有 `//` 行注释，`//host` 出现在属性里
    恰恰是协议相对 URL 本体，剥了就是漏报。
    """
    if suffix in (".css", ".js", ".vue", ".mjs", ".ts"):
        text = _BLOCK_COMMENT_RE.sub("", text)
        text = _LINE_COMMENT_RE.sub("", text)
    elif suffix == ".html":
        text = _HTML_COMMENT_RE.sub("", text)
    return text


def external_hosts(text: str, suffix: str = "") -> list:
    """文件文本 → 违规外部主机列表（去重排序）。**纯函数**（牙齿在 D3）。"""
    stripped = strip_asset_comments(text, suffix)
    return sorted({host for host in _ASSET_URL_RE.findall(stripped)
                   if host.lower() not in ALLOWED_ASSET_HOSTS})


def asset_files() -> dict:
    """前端两处落点的全部文本文件：`{相对路径: 文本}`。

      * `frontend/src/**` + `frontend/index.html` —— 源码侧（R7-ASSETS 红线
        **不扫**这里，本 Gate 补上）；
      * `magpie/api/web/**` —— 产物侧（红线的既有范围，出口再走一遍）。

    `node_modules` / `dist` / 二进制后缀跳过；`frontend/tests/**` 不在范围
    （夹具数据不随产品发布，与 R7 不扫 bench 夹具同一口径）。
    """
    files: dict = {}

    def add(path: Path) -> None:
        if path.suffix.lower() in _ASSET_SKIP_SUFFIX:
            return
        try:
            files[path.relative_to(ROOT).as_posix()] = path.read_text(
                encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return                                    # 二进制 / 读不了的文件

    src = ROOT / "frontend" / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*")):
            if path.is_file() and "node_modules" not in path.parts:
                add(path)
    index = ROOT / "frontend" / "index.html"
    if index.is_file():
        add(index)
    web = ROOT / "magpie" / "api" / "web"
    if web.is_dir():
        for path in sorted(web.rglob("*")):
            if path.is_file():
                add(path)
    return files


# ---------------------------------------------------------------------------
# A · 专项① MCP 默认只读
# ---------------------------------------------------------------------------

#: 第二轮调用的**最小实参**（按属性名给，不按工具名手抄清单）：只喂进
#: inputSchema 的 required 属性，缺探针的属性留空 —— 空实参 / 用法错那一轮
#: 同样不许碰库（"失败"和"写库"是两回事）。
_ARG_PROBES = {"ref": "1", "job": "1", "entity_id": 1}


def _round2_args(tool) -> dict:
    schema = tool.input_schema
    return {name: _ARG_PROBES[name]
            for name in schema.get("required", ())
            if name in _ARG_PROBES}


def _stdio_chain(db_path: Path) -> tuple:
    """真起 stdio 子进程：initialize → 通知 → tools/list → tools/call。

    返回 `(proc, [应答行的解析结果], [解析失败的行)`。stdout 必须恰好 3 行
    （通知永不回）、每行可解析 JSON（帮助文本 / traceback 混进协议流当场炸）。
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "verify_g5", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "ls", "arguments": {"limit": 1}}},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "magpie.adapters.mcp", "--db", str(db_path)],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        timeout=180, cwd=str(ROOT), env=ENV)
    lines = proc.stdout.splitlines()
    parsed, errors = [], []
    for index, line in enumerate(lines):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"第 {index + 1} 行不是 JSON：{line[:80]!r}（{exc}）")
    return proc, parsed, errors


def section_a() -> None:
    data_dir = WORK / "a" / "data"
    db_path = data_dir / "magpie.db"
    yaml_path = WORK / "a" / "pipeline.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        "name: g5 gate\n"
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
        cwd=str(ROOT), capture_output=True, env=ENV, timeout=300)
    check("A0 夹具库经 2.5 最小入口（pipeline run）真导入出来",
          proc.returncode == 0 and db_path.exists(),
          proc.stderr.decode("utf-8", "replace")[-200:] if proc.returncode else
          f"db={db_path.name}")

    if BREAK == "mcp":
        # 反例开关：把 ask 从明文剔除清单里拔掉 —— 「白名单提前扩张」的形状。
        # 进程内改 dict，不碰盘。
        mcp.EXCLUDED_READ_COMMANDS.pop("ask", None)
        note("反例开关 MAGPIE_VERIFY_G5_BREAK=mcp 已生效：EXCLUDED 里的 ask 被拔掉"
             " —— A1 / A2 下面应当有 FAIL")

    schema_data = load_schema_data(db_path)

    # --- A1 清单从真实注册表算（不手抄）+ 派生跟契约走的牙齿 ---
    derived = derive_manifest(schema_data)
    declared = set(mcp.READONLY_TOOLS) | set(mcp.WRITE_TOOLS)
    check("A1a 工具清单从真实注册表算：build_registry(真契约) ≡ 声明"
          "（READONLY ∪ WRITE，实际 ⊆ 声明 —— 防白名单提前扩张）",
          set(derived) <= declared,
          f"多出来的：{sorted(set(derived) - declared)}")
    check("A1b 反方向：声明 ⊆ 实际（防声明空转 —— 死声明同样红）",
          declared <= set(derived),
          f"声明了却没实现：{sorted(declared - set(derived))}")
    # 牙齿①：契约把 ls 标成 write → 派生必须丢掉 ls（手抄的清单不会变）
    doctored = json.loads(json.dumps(schema_data))
    doctored["commands"]["ls"]["access"] = "write"
    check("A1c 牙齿①：喂改过分类的契约（ls→write），派生清单必须跟着变 —— "
          "手抄的清单在这一条下不会变",
          "ls" not in derive_manifest(doctored),
          f"派生仍含 ls：{'ls' in derive_manifest(doctored)}")
    # 牙齿②：契约把 import 标成 read → 派生必须长出 import（且与声明撞红）
    doctored2 = json.loads(json.dumps(schema_data))
    doctored2["commands"]["import"]["access"] = "read"
    doctored2["commands"]["import"]["mutates_db"] = False
    grown = derive_manifest(doctored2)
    check("A1d 牙齿②：契约把 import 标成 read，派生必须长出 import —— "
          "「新增命令进面」必须是显式决定（撞上声明即红的机制在 A1a/b）",
          "import" in grown and set(grown) != set(mcp.READONLY_TOOLS),
          f"import 进面={'import' in grown}")
    # 牙齿③（0.21.0）：把 add_finding 的 access 伪造成 read → 写工具名只从
    # 写分支进面，读分支必须丢弃它；两边都进不来 = 注册表缺它 = 漂移闸红。
    doctored3 = json.loads(json.dumps(schema_data))
    doctored3["commands"]["add_finding"]["access"] = "read"
    doctored3["commands"]["add_finding"]["mutates_db"] = False
    disguised = derive_manifest(doctored3)
    check("A1e 牙齿③：把 add_finding 的 access 伪造成 read，派生必须丢掉它 —— "
          "改一张契约收买不了写命令的分类",
          "add_finding" not in disguised,
          f"伪装后仍进面={'add_finding' in disguised}")

    # --- A2 逐工具调用前后库快照不变（各表行数 + 审计/作业记录） ---
    try:
        server = mcp.McpServer(db_path)
        before = db_snapshot(db_path)
        audit_tables = {"activity_log", "jobs", "work_journal"}
        check("A2a 快照口径在场：逐表（表+视图）行数+内容哈希，且审计/作业表 "
              "activity_log / jobs / work_journal 都被扫到",
              audit_tables <= set(before) and len(before) >= 20,
              f"表数={len(before)} 缺={sorted(audit_tables - set(before))}")
        envelopes, failures = [], []
        # **写工具在循环里被排除（0.21.0）** —— 它拿读写连接，跑了就会动快照。
        # 排除的正当性由 A2e 证明（写工具真能写，且只写自己的副本）。
        readonly_names = [name for name in sorted(server.registry)
                          if name not in mcp.WRITE_TOOLS]
        for name in readonly_names:
            tool = server.registry[name]
            for arguments in ({}, _round2_args(tool)):
                response = server.handle(
                    {"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})
                if response is None or "result" not in response:
                    failures.append(f"{name}{arguments} → 协议级无应答：{response}")
                    continue
                try:
                    envelopes.append(json.loads(
                        response["result"]["content"][0]["text"]))
                except (KeyError, IndexError, json.JSONDecodeError) as exc:
                    failures.append(f"{name}{arguments} → 信封解析失败：{exc}")
        after = db_snapshot(db_path)
        check("A2b 全部**只读**注册工具 × 两轮调用后，库快照（行数+内容哈希）"
              "逐表不变 —— 写工具除外（其边界由 A2e 单独证），审计与作业记录"
              "一个字节没动",
              not failures and before == after,
              "；".join(failures[:3]) if failures else
              (f"快照变了：{sorted(k for k in before if before[k] != after.get(k))}"
               if before != after else ""))
        ok_count = sum(1 for env in envelopes if env.get("ok"))
        check("A2c 非空转：至少一次 tools/call 真的读到了数据（全员失败的"
              "「只读」是空转绿）",
              ok_count >= 1 and len(envelopes) == 2 * len(readonly_names),
              f"ok={ok_count}/{len(envelopes)}"
              f"（只读 {len(readonly_names)} 个 / 注册 {len(server.registry)} 个）")
        # 牙齿：对库**副本**经产品自己的写入口写一笔，快照必须变。
        from magpie.core import ops                                  # noqa: PLC0415
        teeth_db = WORK / "a" / "teeth.db"
        shutil.copy2(db_path, teeth_db)
        pre = db_snapshot(teeth_db)
        conn = sqlite3.connect(str(teeth_db))
        try:
            ops.create_project(conn, "g5-teeth", {"include": [], "exclude": []})
            conn.commit()
        finally:
            conn.close()
        check("A2d 牙齿：经产品唯一写入口（ops.create_project）写一笔，快照必须变"
              " —— 没检出写=上面的只读断言是假的",
              db_snapshot(teeth_db) != pre,
              "写入被快照检出" if db_snapshot(teeth_db) != pre else "快照没变")
        # A2e（0.21.0）：写工具（恰一个）在**库副本**上走完整协议真调一次 ——
        # 副本快照必须变（它真能写，A2b 的排除不是把"写不进去"藏进了排除里）、
        # 主库快照必须不变（它只动自己拿到的那个库）。
        write_db = WORK / "a" / "write-tool.db"
        shutil.copy2(db_path, write_db)
        write_pre = db_snapshot(write_db)
        main_pre = db_snapshot(db_path)
        write_server = mcp.McpServer(write_db)
        listed = write_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "ls", "arguments": {"limit": 1}}})
        ref = ""
        try:
            listed_env = json.loads(listed["result"]["content"][0]["text"])
            ref = str(listed_env["data"]["entities"][0]["id"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            ref = ""
        write_resp = write_server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "add_finding",
                        "arguments": {"ref": ref, "text": "G5 出口的写通道探针"}}})
        write_env: dict = {}
        try:
            write_env = json.loads(write_resp["result"]["content"][0]["text"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            pass
        write_after = db_snapshot(write_db)
        main_after = db_snapshot(db_path)
        check("A2e 写工具 add_finding 在库副本上真调：ok 信封 + 副本快照变 + "
              "主库不变 —— 排除出 A2b 没藏东西，写通道只动自己的库",
              bool(ref) and write_env.get("ok") is True
              and bool((write_env.get("data") or {}).get("job_id"))
              and write_after != write_pre and main_after == main_pre,
              f"ref={ref!r} ok={write_env.get('ok')} "
              f"副本变={write_after != write_pre} 主库变={main_after != main_pre}")
    except Exception as exc:                                          # noqa: BLE001
        check("A2 工具面快照断言（McpServer 启动）", False,
              f"{type(exc).__name__}: {exc}")
        server = None

    # --- A3 AST：adapters 无写入口 import ---
    adapters_dir = ROOT / "magpie" / "adapters"
    found: list = []
    for path in sorted(adapters_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        violations = adapter_write_imports(path.read_text(encoding="utf-8"), rel)
        found.extend(f"{rel.as_posix()}:{lineno} import {module}"
                     for module, lineno in violations)
    check("A3 AST 断言：magpie/adapters/** 无写入口 import"
          "（core.ops / core.store / cli.write / sqlite3）—— 扫描器自写，牙齿在 D2",
          not found, "；".join(found[:5]))

    # --- A4 双向清单：实际注册 ≡ 声明（McpServer 层，含漂移闸） ---
    try:
        live = mcp.McpServer(db_path)
        actual = frozenset(live.registry)
        declared_live = mcp.READONLY_TOOLS | mcp.WRITE_TOOLS
        check("A4 双向清单校验（McpServer 真启动）：实际注册 ≡ 声明"
              "（READONLY ∪ WRITE）—— 两个方向各一条，红的含义不同",
              actual <= declared_live and declared_live <= actual
              and mcp.READONLY_TOOLS.isdisjoint(mcp.WRITE_TOOLS),
              f"多={sorted(actual - declared_live)} "
              f"缺={sorted(declared_live - actual)}")
    except Exception as exc:                                          # noqa: BLE001
        check("A4 双向清单校验（McpServer 真启动）", False,
              f"{type(exc).__name__}: {exc}")

    # --- A5 stdio 全链路抽测（真子进程） ---
    proc, parsed, errors = _stdio_chain(db_path)
    check("A5a stdio 全链路：子进程退出码 0，stdout 恰好 3 行且逐行可解析 JSON"
          "（通知不回、帮助文本/traceback 不许混进协议流）",
          proc.returncode == 0 and len(parsed) == 3 and not errors,
          f"rc={proc.returncode} 行数={len(parsed)} "
          f"{errors[:2] or (proc.stderr or '')[-200:]}")
    if len(parsed) == 3:
        init, listing, call = parsed
        check("A5b initialize 应答：serverInfo= magpie / 版本= 契约 tool_version，"
              "协议版本按协商回",
              init.get("result", {}).get("serverInfo", {}).get("name") == "magpie"
              and init.get("result", {}).get("serverInfo", {}).get("version")
              == schema_data["tool_version"]
              and init.get("result", {}).get("protocolVersion") == "2025-06-18",
              json.dumps(init.get("result", {}).get("serverInfo", {}),
                         ensure_ascii=False))
        live_names = [t.get("name") for t in
                      listing.get("result", {}).get("tools", [])]
        check("A5c tools/list 的名字集合 ≡ 本进程从真契约实算的清单"
              "（跨进程同一份事实；0.21.0 起 = 只读 ∪ 唯一写通道）",
              live_names == sorted(set(derived))
              == sorted(mcp.READONLY_TOOLS | mcp.WRITE_TOOLS),
              f"live={live_names}")
        result = call.get("result", {})
        envelope = {}
        try:
            envelope = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError):
            pass
        check("A5d tools/call（ls --limit 1）：isError=false + ok 信封 + meta 三键",
              result.get("isError") is False and envelope.get("ok") is True
              and set(envelope.get("meta") or {}) ==
              {"db", "took_ms", "schema_version"}
              and envelope.get("data", {}).get("count") == 1,
              f"isError={result.get('isError')} ok={envelope.get('ok')} "
              f"count={(envelope.get('data') or {}).get('count')}")
    else:
        check("A5b–d initialize / tools/list / tools/call", False,
              f"应答行数 {len(parsed)} ≠ 3")


# ---------------------------------------------------------------------------
# B · 专项② 上下文包零出站
# ---------------------------------------------------------------------------


def _block_outbound():
    """把出站能力钉死：任何一条真连接当场炸并记账。返回 (attempts, restore)。"""
    import socket as sock                                              # noqa: PLC0415
    attempts: list = []
    original = (sock.socket.connect, sock.socket.connect_ex,
                sock.create_connection)

    def _blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("零出站被破：运行期间试图建立出站连接")

    sock.socket.connect = _blocked
    sock.socket.connect_ex = _blocked
    sock.create_connection = _blocked

    def restore():
        sock.socket.connect, sock.socket.connect_ex, sock.create_connection = original
    return attempts, restore


def _context_shape_problems(body: dict) -> list:
    """`data` 载荷 → 形状问题列表。**纯函数**（BREAK=context 打断渲染时它报红）。"""
    problems = []
    if set(body) != {"json", "markdown"}:
        problems.append(f"data 的键是 {sorted(body)}，应恰好 [json, markdown]")
        return problems
    payload, markdown = body["json"], body["markdown"]
    if not isinstance(markdown, str) or not markdown.startswith("# "):
        problems.append("markdown 不是以 '# ' 开头的 Markdown")
    if "## 观测" not in (markdown or ""):
        problems.append("markdown 缺 '## 观测' 小节")
    if not isinstance(payload, dict) or "entity" not in payload:
        problems.append("json 缺 entity")
    elif not payload["observations"]:
        problems.append("json.observations 为空 —— 形状对了但内容是空的")
    return problems


def section_b() -> None:
    # --- B1 socket 封死下的端到端 GET /api/v1/agent/context ---
    if BREAK == "context":
        from magpie.api import routes as routes_mod                   # noqa: PLC0415

        def _boom(_payload):
            raise RuntimeError("反例开关：上下文包渲染器被打断")

        original_renderer = routes_mod._context_markdown
        routes_mod._context_markdown = _boom
        note("反例开关 MAGPIE_VERIFY_G5_BREAK=context 已生效：渲染器会抛错 —— "
             "B1 下面应当有 FAIL")
    else:
        original_renderer = None

    try:
        try:
            from fastapi.testclient import TestClient                 # noqa: PLC0415
            from magpie.api.app import create_app                     # noqa: PLC0415
        except ImportError as exc:
            check("B1 端到端（fastapi 可用）", False, f"extras.api 缺失：{exc}")
        else:
            db_path = WORK / "a" / "data" / "magpie.db"
            app = create_app(db_path=db_path, data_dir=db_path.parent,
                             token="g5-token")
            headers = {"Authorization": "Bearer g5-token"}
            # TestClient 先**进场**再封死：Windows 上事件循环的 socketpair 是
            # 一次回环 connect（stdlib 在 Windows 的实现），循环创建不属于被测
            # 行为 —— `test_agent_context_offline` 同样先建 client 再封（照抄
            # 它的时序，封死窗口只盖被测的那次 GET）。
            with TestClient(app, raise_server_exceptions=False) as client:
                listing = client.get("/api/v1/entities?limit=500",
                                     headers=headers).json()["data"]
                found = next((e for e in listing if e["observation_count"] > 0),
                             None)
                check("B1a 夹具里选得出带观测的实体（否则形状断言是空转的 0）",
                      found is not None, f"实体数={len(listing)}")
                if found is not None:
                    attempts, restore = _block_outbound()
                    try:
                        # 查询串与 GUI 按钮发出去的一字不差（DetailPanel.vue 同款）
                        response = client.get(
                            f"/api/v1/agent/context?entity_id={found['id']}&depth=1",
                            headers=headers)
                    finally:
                        restore()
                    shape_bad = []
                    if response.status_code != 200:
                        shape_bad.append(f"状态码 {response.status_code}："
                                         f"{response.text[:160]}")
                    else:
                        body = response.json().get("data") or {}
                        shape_bad = _context_shape_problems(body)
                        if not shape_bad and \
                                body["json"]["entity"]["id"] != found["id"]:
                            shape_bad.append("entity_id 回显不对")
                    check("B1b socket 全封死下端到端调 GET /api/v1/agent/context → "
                          "200 + {json, markdown} 双形状（断言与 GUI 按钮同一条查询串）",
                          not shape_bad, "；".join(shape_bad))
                    check("B1c 封死期间出站尝试为 0（谁伸手谁当场炸）",
                          not attempts, f"attempts={attempts[:2]}")
    finally:
        if original_renderer is not None:
            from magpie.api import routes as routes_mod               # noqa: PLC0415
            routes_mod._context_markdown = original_renderer

    # --- B2 R7 四向全验（跑 check.py 自己的 R7 本体 + 构造样本牙齿） ---
    sources: dict = {}
    for path, rel in check_mod._iter_sources():
        tree, _ = check_mod._parse(path, rel)
        if tree is not None:
            sources[rel] = tree
    if BREAK == "r7":
        # 数据注入：往扫描集塞一个白名单外的出站文件（进程内，不碰盘）
        sources = dict(sources)
        sources[Path("magpie/cli/_g5_break_ghost.py")] = ast.parse(
            "import requests\n")
        note("反例开关 MAGPIE_VERIFY_G5_BREAK=r7 已生效：扫描集注入了白名单外的"
             "出站文件 —— B2b 下面应当有 FAIL")

    whitelist = check_mod._OUTBOUND_WHITELIST
    audit = check_mod._r7_audit(sources, whitelist)
    ask_rel = Path("magpie") / "cli" / "ask.py"

    check("B2a 方向(a) 白名单结构：恰好一个条目 = magpie/cli/ask.py（5.4 改造后的"
          "现值），且条目真的在扫描集里 —— 想扩第二个，停下来先问",
          not audit["whitelist_bad"] and whitelist == frozenset({ask_rel})
          and ask_rel in sources,
          f"whitelist={sorted(p.as_posix() for p in whitelist)} "
          f"{audit['whitelist_bad']}")
    subprocess_hits = [item for item in audit["violations"]
                       if "子进程" in item[2]]
    outbound_hits = [item for item in audit["violations"]
                     if "子进程" not in item[2]]
    check("B2b 方向(b) 实际 ⊆ 声明：白名单**之外**零出站点（import 与 socket "
          "调用点都不许有）",
          not outbound_hits,
          "；".join(f"{rel}:{ln} {text[:60]}" for rel, ln, text in outbound_hits[:3]))
    # 声明 ⊆ 实际：ask.py 真有出站点 —— 本脚本自己解析它的 AST，不借
    # `_r7_points` 的口径（两套口径都看见了，才算非空转）。
    ask_tree = ast.parse((ROOT / ask_rel).read_text(encoding="utf-8"))
    own_outpoints = [
        node for node in ast.walk(ask_tree)
        if isinstance(node, ast.Import)
        and any(a.name == "urllib.request" for a in node.names)
    ]
    check("B2c 方向(c) 声明 ⊆ 实际：白名单文件 ask.py 真有出站点"
          "（本脚本自解析 AST 见到 import urllib.request），且规则侧 no_outpoint 为空",
          bool(own_outpoints) and not audit["no_outpoint"],
          f"own_outpoints={len(own_outpoints)} no_outpoint={audit['no_outpoint']}")
    check("B2d 方向(d) 子进程禁令全仓干净：curl/wget/nc 谁都不豁免（含白名单与"
          "台架），真仓 0 命中",
          not subprocess_hits,
          "；".join(f"{rel}:{ln}" for rel, ln, _ in subprocess_hits[:3]))

    # 四向的牙齿（构造样本，不读盘）—— 每次跑都验，不会锈。
    ask = ask_rel
    product = Path("magpie") / "cli" / "db.py"
    bench_file = Path("bench") / "test_perf.py"

    def audit_of(sample: dict, wl=frozenset({ask})) -> dict:
        return check_mod._r7_audit(
            {rel: ast.parse(text) for rel, text in sample.items()}, wl)

    case = audit_of({ask: "import urllib.request\n",
                     product: "import requests\n"})
    check("B2e 牙齿(b)：白名单外的产品文件写出站 import → 必须红",
          bool(case["violations"]),
          f"检出 {len(case['violations'])} 处" if case["violations"]
          else "没检出 —— 断言没牙")
    case = audit_of({ask: "import socket\nconnect = socket.connect\n"})
    check("B2f 牙齿(c)：白名单文件被拔掉出站点 → 必须红（声明⊆实际的反例）",
          bool(case["no_outpoint"]),
          f"检出 {case['no_outpoint']}" if case["no_outpoint"]
          else "没检出 —— 断言没牙")
    case = audit_of({ask: "import urllib.request\n"},
                    wl=frozenset({ask, product}))
    check("B2g 牙齿(a)：白名单扩到第二个文件 → 必须红（不许自行扩洞）",
          bool(case["whitelist_bad"]),
          f"检出 {len(case['whitelist_bad'])} 处" if case["whitelist_bad"]
          else "没检出 —— 断言没牙")
    case = audit_of({ask: "import urllib.request\n"
                          "subprocess.run(['curl', 'http://x'])\n"})
    curl_caused = any("curl" in text for _, _, text in case["violations"])
    case2 = audit_of({bench_file: "subprocess.run(['wget', 'http://x'])\n"})
    wget_caused = any("wget" in text for _, _, text in case2["violations"])
    case3 = audit_of({product: "subprocess.run(['nc', 'host', '1'])\n"})
    nc_caused = any("nc" in text for _, _, text in case3["violations"])
    check("B2h 牙齿(d)：curl（白名单文件内）/ wget（台架内）/ nc（普通产品文件内）"
          "三个位置都必须红 —— 子进程禁令对谁生效",
          curl_caused and wget_caused and nc_caused,
          f"curl={curl_caused} wget={wget_caused} nc={nc_caused}")
    control = audit_of({bench_file: "import requests\n"})
    check("B2i 对照：台架的出站客户端 **import** 豁免照旧（现状不放松，不误报）",
          not control["violations"],
          f"误报 {len(control['violations'])} 处" if control["violations"] else "")
    control = audit_of({ask: "import urllib.request\n"})
    check("B2j 对照：白名单文件的合法出站点不报、不算 no_outpoint（不误报）",
          not control["violations"] and not control["no_outpoint"],
          f"violations={control['violations']} no_outpoint={control['no_outpoint']}"
          if (control["violations"] or control["no_outpoint"]) else "")

    # --- B3 前端静态扫（源码侧 + 产物侧，注释剥离后 0 外部域名） ---
    files = asset_files()
    if BREAK == "assets":
        files = dict(files)
        files["frontend/src/_g5_break.js"] = \
            'import x from "https://cdn.jsdelivr.net/npm/x.js"\n'
        note("反例开关 MAGPIE_VERIFY_G5_BREAK=assets 已生效：扫描集注入了 CDN 引用"
             " —— B3 下面应当有 FAIL")
    hits = []
    for name, text in files.items():
        for host in external_hosts(text, Path(name).suffix):
            hits.append(f"{name} → {host}")
    check("B3 前端两处落点（frontend/src 源码 + magpie/api/web 产物）逐文件扫外部"
          "域名：0 命中（块注释与整行 // 先剥；字符串与行尾注释不剥，保守方向）",
          not hits, "；".join(hits[:5]))
    check("B3a 非空转：扫描集真的装着两处落点的关键文件",
          "frontend/src/components/DetailPanel.vue" in files
          and "frontend/src/styles/tokens.css" in files
          and any(n.startswith("magpie/api/web/") for n in files)
          and len(files) >= 10,
          f"文件数={len(files)}")

    # --- B4 「复制给 AI」按钮：代码面审 + vitest 已有断言抽查 ---
    panel = (ROOT / "frontend" / "src" / "components" / "DetailPanel.vue") \
        .read_text(encoding="utf-8")
    api_js = (ROOT / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    missing = [name for name, present in (
        ("/agent/context?entity_id=", "/agent/context?entity_id=" in panel),
        ("depth=1", "depth=1" in panel),
        ("navigator.clipboard+writeText",
         "navigator.clipboard" in panel and "writeText(" in panel),
    ) if not present]
    if external_hosts(panel, ".vue"):
        missing.append(f"外部主机 {external_hosts(panel, '.vue')}")
    check("B4a 代码面审：按钮走同源相对路径 `/agent/context?entity_id=…&depth=1`，"
          "落到本机剪贴板 navigator.clipboard.writeText —— 没有'发给某个 AI 服务'的一步",
          not missing, f"缺/违规：{missing}")
    check("B4b api.js 的 API_PREFIX 是相对前缀（同源），fetch 拼在它后面",
          "API_PREFIX = '/api/v1'" in api_js
          and "fetch(API_PREFIX + path" in api_js,
          "前缀或 fetch 拼接形态不符")
    vtest = ROOT / "frontend" / "tests" / "agent_context.test.js"
    vsrc = vtest.read_text(encoding="utf-8") if vtest.is_file() else ""
    check("B4c vitest 已有断言抽查：端点字符串钉死、Markdown 进剪贴板、端点失败时"
          "剪贴板不被碰 —— 钉子都还在（npm 不进默认执行，实跑证据附交付评论）",
          vtest.is_file()
          and "'/agent/context?entity_id=42&depth=1'" in vsrc
          and "toHaveBeenCalledWith(MARKDOWN)" in vsrc
          and "expect(writeText).not.toHaveBeenCalled()" in vsrc,
          vtest.name if vtest.is_file() else "文件不存在")


# ---------------------------------------------------------------------------
# C · 接线（谁少了都会在出口那一刻被看见）
# ---------------------------------------------------------------------------


def section_c() -> None:
    text = (ROOT / "check.py").read_text(encoding="utf-8")
    check("C1 verify_g5.py 在 check.py 的 UNIT 执行表里（本 Gate 默认就跑）",
          '"verify_g5.py"' in text)
    check("C2 check.py 默认跑 pytest magpie/adapters/tests"
          "（MCP 只读的逐工具 / AST / 协议合同都在里面）",
          '"magpie/adapters/tests"' in text)
    offline = ROOT / "magpie" / "api" / "tests" / "test_agent_context_offline.py"
    osrc = offline.read_text(encoding="utf-8") if offline.is_file() else ""
    check("C3 check.py 默认跑 pytest magpie/api/tests，且零出站专项还在："
          "test_agent_context_zero_outbound（默认 depth 与 GUI 同款两条）",
          '"magpie/api/tests"' in text
          and "def test_agent_context_zero_outbound" in osrc
          and "def test_agent_context_zero_outbound_depth_default" in osrc,
          offline.name if offline.is_file() else "文件不存在")
    check("C4 check.py 现行 11 条红线一条没动（规则表逐名钉死）",
          len(check_mod.RULES) == 11
          and {code for code, _desc, _fn in check_mod.RULES} == {
              "C2", "C3", "R6", "R7", "R7-ASSETS", "PARSE-RO", "PARSE-NORM",
              "FLOOR", "READONLY", "READONLY-REVERSE", "SQL-FREE"},
          f"实得：{sorted(code for code, _d, _f in check_mod.RULES)}")
    check("C5 R7 红线本体仍在接线上（四向审计由 _rule_r7 调用，B2 跑的就是它）",
          check_mod.RULES[3][0] == "R7" and check_mod.RULES[3][2]
          is check_mod._rule_r7)


# ---------------------------------------------------------------------------
# D · 反例自检：每个扫描器 / 比较器都拿构造样本验一遍牙齿 + 对照
# ---------------------------------------------------------------------------


def section_d() -> None:
    # --- D1 库快照器 ---
    probe = WORK / "d" / "snap.db"
    probe.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(probe))
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("CREATE VIEW v AS SELECT v FROM t")
    conn.execute("INSERT INTO t VALUES ('干净值')")
    conn.commit()
    conn.close()
    first = db_snapshot(probe)
    check("D1 对照①：同库再拍一次快照 → 相等（不是每次都变红的假断言）",
          db_snapshot(probe) == first)
    conn = sqlite3.connect(str(probe))
    conn.execute("INSERT INTO t VALUES (?)", ("带观测的行",))
    conn.commit()
    conn.close()
    second = db_snapshot(probe)
    check("D1 反例①：多一行 → 行数与内容哈希都必须检出（表和视图两条路都在）",
          second != first and second["t"][0] == first["t"][0] + 1
          and second["v"] != first["v"],
          f"t={first['t']}→{second['t']}")
    conn = sqlite3.connect(str(probe))
    conn.execute("UPDATE t SET v = '原地改的行' WHERE v = '干净值'")
    conn.commit()
    conn.close()
    check("D1 反例②：**原地 UPDATE**（行数不变）→ 内容哈希必须检出 —— "
          "只数行数会漏掉审计被改写的那一种",
          db_snapshot(probe) != second and db_snapshot(probe)["t"][0] == second["t"][0],
          "行数相同，哈希变了" if db_snapshot(probe)["t"][0] == second["t"][0]
          and db_snapshot(probe) != second else "没检出")

    # --- D2 adapters 写入口扫描器 ---
    mcp_rel = Path("magpie") / "adapters" / "mcp.py"      # 相对 import 的解析基准
    proofs = [
        ("import sqlite3", True),
        ("from ..core import ops", True),
        ("import magpie.core.store", True),
        ("from magpie.cli import write", True),
        ("from magpie.core import ops", True),            # 绝对形态同样要抓住
        ("from ..core import query", False),              # 读侧唯一实现，合法
        ("from ..cli.read import RUNNERS", False),
        ("from ..cli.db import Database, map_db_error", False),
    ]
    wrong = [src for src, must in proofs
             if bool(adapter_write_imports(src, mcp_rel)) is not must]
    check("D2 牙齿+对照：四种写法必须报、读侧合法 import 不许误报（逐条）",
          not wrong, f"判错的：{wrong}")

    # --- D3 资产扫描器 ---
    cases = [
        ('<link href="https://cdn.jsdelivr.net/npm/x.css">', ".html", True),
        ('<script src="//fonts.gstatic.com/x.js"></script>', ".html", True),
        ('@import url("https://fonts.googleapis.com/css2");', ".css", True),
        ('const url = "http://127.0.0.1:8000/api/v1/meta";', ".js", False),
        ('fetch("/api/v1/meta")', ".js", False),
        ('<svg xmlns="http://www.w3.org/2000/svg"></svg>', ".html", False),
        ("/* 这里解释为什么不引 https://fonts.googleapis.com */", ".css", False),
        ("// 这是整行注释里的 https://evil.example.org", ".js", False),
        ('const u = "https://evil.example.org/x"; // 行尾注释也扫', ".js", True),
    ]
    wrong = [f"{text!r}→{external_hosts(text, suf)}"
             for text, suf, must in cases
             if bool(external_hosts(text, suf)) is not must]
    check("D3 牙齿+对照：CDN / 协议相对 / 注释内外 / 本机地址 / w3.org 命名空间"
          " 九种形态逐条判定",
          not wrong, f"判错的：{wrong}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    if BREAK not in _BREAK_VALUES:
        print(f"未知的反例开关 MAGPIE_VERIFY_G5_BREAK={BREAK!r}"
              f"（可选：{'/'.join(v for v in _BREAK_VALUES if v)}）")
        return 1

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    _section(section_a, "A · 专项① MCP 默认只读（清单实算 / 快照不变 / AST / "
                        "双向清单 / stdio 全链路）")
    _section(section_b, "B · 专项② 上下文包零出站（封死端到端 / R7 四向 / "
                        "前端静态扫 / 复制按钮）")
    _section(section_c, "C · 接线（本 Gate 与既有专项都在 check.py 默认执行里）")
    _section(section_d, "D · 反例自检（造一个反例就必须红；再造对照证明不误报）")

    print()
    if BREAK:
        print(f"!! 反例开关已打开: MAGPIE_VERIFY_G5_BREAK={BREAK}"
              " —— 上面应当有 FAIL（否则这个开关是装饰）")
    print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
