"""管线编排 —— YAML 定义 + `after` 依赖 + 顺序执行（计划书 §5.2 / 2.5 的任务 3）。

    python -m magpie.core.pipeline run <yaml> [--dry-run] [--db <path>]

这同时是 2.5 的**最小触发入口**：CLI（2.2 / 2.6）已后置，若不给一个能跑的入口，
"导入"这件事做完之后没有任何人能用它，端到端验收无从下手。因此这里只做三件事：

  1. 读一个 YAML 管线定义；
  2. 按 `after` 依赖顺序执行每一步（顺序执行，**不做可视化编辑**）；
  3. 打印一份 JSON 汇总报告（与 ingest 的报告同源）。

**它不是完整 CLI**：没有子命令体系、没有 `--human` 表格、没有退出码表
（那三样是 2.6 的交付物）。2.7 / 2.8 之后导入入口由 GUI 接管，本入口保留为
脚本逃生通道。

YAML 形态（§5.2）:

    name: 常规一轮（域名+指纹）
    project: 某SRC
    scope: {include: ["*.example.com"], exclude: []}     # 项目不存在时按它创建
    data_dir: ./.magpie
    steps:
      - id: dns
        parser: domainlist
        source: {kind: glob, pattern: "recon/*subfinder*.txt"}   # 导入什么
        source_name: subfinder                                   # 谁给的（可选）
      - id: probe
        parser: httpx
        source: {kind: glob, pattern: "recon/*httpx*.jsonl"}
        after: [dns]

`source` 的三种 kind（3.7a 冻结 F1）:

| kind   | 语义             | 匹配 0 个                        | 匹配 >1 个 |
|--------|------------------|----------------------------------|------------|
| file   | 指名道姓的单文件 | **硬错误** `E_SOURCE_NOT_FOUND`  | 不可能     |
| glob   | 通配模式，0..N   | 报告的 problems + dependency_empty | 正常     |
| dir    | 目录，递归发现   | 报告的 problems                  | 正常       |

旧写法（`source_glob` + 字符串 `source`）仍然读得懂，但会被**归一化**成上面的
形态，见 `normalize_definition`。**`source` 从 3.7b 起只表示"导入什么"**，来源
名是 `source_name` —— 这两个键从前互相打架（一个只写了 `source: subfinder` 的
step 会被当成 `glob("subfinder")` 去扫盘，然后报告说"`source_glob
'subfinder'` 没有匹配到任何文件"，报的是一个用户根本没写过的键名）。

`after` 的语义（诚实交代边界）: 今天是**顺序依赖** —— 被依赖的步骤先跑；若它
一个文件都没导入成功，本步骤会在报告里被标 `dependency_empty: true`，但**仍然
执行**（今天的解析器之间没有真实的产物传递；真正的"上一步的产物喂给下一步"
要等阶段 3 的离线指纹引擎）。把它写成硬失败会凭空挡住合法的并行用法。

"一个文件都没导入成功"的判据：见 `_PRODUCED_STATES`。
"""

from __future__ import annotations

import contextlib
import glob as glob_module
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import ingest, ops, store

USAGE = """用法:
  python -m magpie.core.pipeline run <pipeline.yaml> [--dry-run] [--db <path>]
                                 [--data-dir <dir>] [--base-dir <dir>]

  python -m magpie.core.pipeline undo <job_id> [--db <path>]     # 逃生通道
  python -m magpie.core.pipeline check <db_path>                 # 不变量快检

退出码: 0 成功 / 1 失败 / 2 用法错误。完整的退出码表属于 2.6，不在这里。
"""

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 文件状态里，哪些算"这一步真的产出了东西"。`dependency_empty` 只问这一件事。
#:
#: * `done` / `partial`  —— 事实落了库（partial 是"落了但没落全"，仍然是有产出）。
#: * `skipped`           —— 幂等跳过。它**算有产出**：跳过恰恰因为同一 sha256 已被
#:                          同一 fingerprint 导入过，上游的产物真实存在于库里。
#: * `needs_confirmation` —— **不算**。它为"待人工裁决"建了一个 job（所以旧判据
#:                          `job_id is not None` 会把它算成有产出），但这个 job 一条
#:                          事实都没落，下游拿到的是一个空的上游。审核 LARI-21 指出
#:                          的正是这一条：判据当时问的是"建没建作业"，而不是
#:                          "有没有产出"。
#: * `failed` / `cancelled` —— 显然不算。
#:
#: 它只是报告里的一个提示位，不改变执行 —— 判错一个提示位不会毁掉数据，
#: 但会让"下游为什么是空的"这个问题失去唯一的线索。
_PRODUCED_STATES = frozenset({"done", "partial", "skipped"})

#: 规范形态的当前版本（3.7a 冻结 F3）。**只升不改**：读时升级、写时只写新版本。
FORMAT_VERSION = 1

#: `source.kind` 的三个取值。三者在**三处**都不同：怎么匹配、匹配 0 个怎么办、
#: `discover` 要不要递归 —— 见模块文档的那张表。
SOURCE_KINDS = ("file", "glob", "dir")

#: 规范形态里**不属于定义本身**的运行时键。它们由入口带上，用来回答"这份定义
#: 是从哪读来的 / 该把 run 挂到谁身上"，不是定义的内容 —— 写进库的话，同一个
#: 定义换一个入口读就会得到另一个 `definition_hash`。
#:
#:   `_path`        文件入口：这份定义是从哪个 YAML 读来的
#:   `_project_id`  库入口：这次跑要挂到哪个项目
#:   `_pipeline_id` 库入口：这次跑要挂到哪条定义
#:
#: `canonical_definition` 按它过滤，所以 `definition_hash` 算的**永远只是定义
#: 本身** —— 一次运行时的 base_dir 覆盖、或者"从哪读来的"，都不会让 hash 变。
RUNTIME_KEYS = frozenset({"_path", "_project_id", "_pipeline_id"})


class DefinitionError(ValueError):
    """定义读不懂 / 跑不了。**继承 ValueError** —— 既有的调用方（`main` 的
    `except ValueError`、各测试的 `pytest.raises(ValueError)`）照旧成立。

    `code` 与 API / CLI 的错误码表同一套；`hint` 是"下一步该做什么"。
    """

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# 定义归一化（3.7a 冻结 F1 / F3）—— **只有一份实现**
# ---------------------------------------------------------------------------
#
# 两个载入器（YAML 文件、库里的行）都只调 `normalize_definition`，谁都不许自己
# 拼一遍。这条不是洁癖："同一个定义、两种读法"和 3.5 收敛掉的"同一份数据、两份
# SQL"是同一类缺陷 —— 两边都跑得通，只是答得不一样。3.5 为此立了 `SQL-FREE`
# 红线，这里配一条测试（3.7b 的 A6）：
#
#     同一份定义分别从 YAML 与从库载入，`normalize_definition` 的输出
#     `json.dumps(..., sort_keys=True, ensure_ascii=False)` **逐字节相等**。
#
# 它顺带承担校验（id 唯一按 `str` 比较、`after` 引用存在、无环）——
# **归一化出来的一定是能跑的**，把"能跑"从运行期提前到载入期。


def _canonical_scope(scope: Any) -> dict[str, Any] | None:
    """scope 的规范形式：include / exclude 排序。

    与 `ops._canonical_scope` 同一口径（那里是写库前再排一次，幂等）。数组
    排序的理由也一样：否则同一个 scope 的两种书写顺序会存成两种文本。
    其余键**原样保留**（`parser_fingerprint` 可能在里面，丢掉它会让
    `create_project` 拿到一个没有指纹的 scope）。
    """
    if not isinstance(scope, dict) or not scope:
        return None
    out = dict(scope)
    for key in ("include", "exclude"):
        if key in out:
            out[key] = sorted(str(v) for v in (out[key] or []))
    return out


def _normalize_source(step: dict[str, Any], sid: str) -> tuple[dict[str, str], str | None]:
    """把 `source` / `source_glob` 的四种写法归一成 `({kind, pattern}, source_name)`。

    | 旧写法                                        | 归一化成                        |
    |-----------------------------------------------|---------------------------------|
    | `source_glob: "recon/*.txt"` + `source: "subfinder"` | source={glob,pattern}、source_name=subfinder |
    | 只有 `source_glob`                            | 同上，source_name=null          |
    | 只有 `source: "<字符串>"` 且没有 `source_glob` | **报错**（见下）                |
    | `source: {kind, pattern}`（新形态）           | 原样                            |

    最后那一行是要点：`source: "subfinder"` 单独出现时，它**可能是来源名、也可能
    是一个路径**，两种读法给出两个不同的结果，而"猜"在这里没有依据。所以报错，
    并且报错信息里给出两条出路 —— 一条会告诉你怎么写的错误，比一条猜一半的
    宽容有用得多。
    """
    raw = step.get("source")
    source_glob = step.get("source_glob")
    declared_name = step.get("source_name")

    if isinstance(raw, dict):
        if source_glob:
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: 同时写了 source 对象与 source_glob —— "
                f"一个 step 只能有一个「导入什么」。",
                hint='留着 source: {kind: ..., pattern: ...}，删掉 source_glob。')
        kind = str(raw.get("kind") or "").strip()
        pattern = raw.get("pattern")
        if kind not in SOURCE_KINDS:
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: source.kind 只能是 {' / '.join(SOURCE_KINDS)}，收到 {kind!r}",
                hint='如 source: {kind: "glob", pattern: "recon/*.txt"}')
        if not pattern:
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: source 缺少 pattern（要导入什么）")
        pattern = str(pattern)
        if kind == "file" and any(ch in pattern for ch in "*?["):
            # `file` 的语义是"我指名道姓要这个文件"。带通配符就不是指名道姓了，
            # 而"扫出来 3 个文件"这件事在 file 语义下没有正确的说法。
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: source.kind 是 file，pattern {pattern!r} 里却带通配符。",
                hint='要匹配多个文件就用 kind: "glob"；要一个目录就 kind: "dir"。')
        name = None if declared_name is None else str(declared_name)
        return {"kind": kind, "pattern": pattern}, name

    if source_glob:
        if raw is not None and not isinstance(raw, (str, int)):
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: source 只能是字符串（来源名）或 source 对象，"
                f"收到 {type(raw).__name__}")
        name = None if raw is None else str(raw)
        if declared_name is not None and name is not None and str(declared_name) != name:
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"step {sid}: source={name!r} 与 source_name={declared_name!r} 说的是"
                f"两件事（source 从 3.7b 起只表示「导入什么」）。")
        if name is None:
            name = None if declared_name is None else str(declared_name)
        return {"kind": "glob", "pattern": str(source_glob)}, name

    if raw is not None:
        shown = json.dumps(raw, ensure_ascii=False)
        raise DefinitionError(
            "E_PIPELINE_BAD_DEFINITION",
            f"step {sid}: 只写了 source: {shown}，读不出它是什么。\n"
            f'    · 它是"导入什么" → source: {{kind: "file"|"glob"|"dir", pattern: "recon/*.txt"}}\n'
            f'    · 它是"谁给的"   → source_name: {shown}',
            hint='两种读法给出两个不同的结果，而"猜"在这里没有依据，所以这里报错。')

    raise DefinitionError(
        "E_PIPELINE_BAD_DEFINITION",
        f"step {sid}: 没有 source（要导入什么）",
        hint='如 source: {kind: "glob", pattern: "recon/*.txt"}')


def _normalize_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise DefinitionError("E_PIPELINE_BAD_DEFINITION", f"steps[{index}] 必须是映射")
    sid_raw = step.get("id")
    if sid_raw is None or sid_raw == "":
        raise DefinitionError("E_PIPELINE_BAD_DEFINITION", f"steps[{index}] 缺少 id")
    # 判重按**归一化后的 id**（`str`），与 `order_steps` 的 `by_id` 同一口径。
    # 旧实现拿 YAML 的原始值去和 `ids`（装着 str）比：`id: 1` 与 `id: "1"` 因为
    # `1 in ["1"]` 为假而**判不出来**，于是 `by_id = {str(id): step}` 把后者覆盖
    # 前者，拓扑排序只剩一步 —— 两步的管线静默跑成一步。
    sid = str(sid_raw)
    source, source_name = _normalize_source(step, sid)

    after = step.get("after") or []
    if not isinstance(after, list):
        raise DefinitionError("E_PIPELINE_BAD_DEFINITION", f"step {sid}: after 必须是列表")
    # 排序 + 去重：`after` 是**依赖集合**，顺序没有语义。排序之后"同一份依赖写
    # 成两种顺序"是同一份定义（A6 要的逐字节相等建立在这一点上），而
    # `order_steps` 的输出也因此与作者的书写顺序无关。
    deps = sorted({str(d) for d in after})

    parser = step.get("parser")
    return {
        "id": sid,
        "source": source,
        "source_name": source_name,
        "parser": None if parser is None else str(parser),
        "after": deps,
        "authority": str(step.get("authority") or "unverified"),
        "archive": str(step.get("archive") or "archived"),
        "batch_size": int(step.get("batch_size") or ingest.DEFAULT_BATCH),
        "dry_run": bool(step.get("dry_run")),
    }


def normalize_definition(raw: Any, *, project: str | int | None = None,
                         base_dir: str | Path | None = None,
                         require_version: bool = False) -> dict[str, Any]:
    """把一份定义（YAML 读来的 / 库里读来的）归一成**规范形态**。

    `require_version=True` 是**库入口**：3.7a 冻结 F3 规定库里存的
    `format_version` 是 NOT NULL，读不到就是错误（`E_PIPELINE_BAD_FORMAT`）。
    文件入口不用它 —— 那一侧的职责恰恰是给旧 YAML **盖上 `format_version: 1`**，
    于是"没有版本字段的那一天"永远不会到来，而那一天正是这个字段存在的理由。

    `base_dir` 的缺省（3.7a 冻结）：

      * 定义里显式写了 `base_dir` → 用它（保存进库时它一律是绝对路径）；
      * 否则用调用方给的 `base_dir`：**文件入口 = 定义文件所在目录**（与今天
        逐字相同），**库入口 = 项目 data_dir**；
      * 都没有时退回 `_path` 的父目录。相对值一律相对定义文件所在目录解析 ——
        不相对 cwd：`pipeline run` 从哪个目录敲的，不该改变它读到什么。
    """
    if not isinstance(raw, dict):
        raise DefinitionError("E_PIPELINE_BAD_DEFINITION",
                              "管线定义的顶层必须是映射（name / project / steps）")

    version_raw = raw.get("format_version")
    if version_raw is None:
        if require_version:
            raise DefinitionError(
                "E_PIPELINE_BAD_FORMAT",
                "库里的管线定义没有 format_version —— 读不出它是哪一版。",
                hint="文件入口（YAML）会补上这个字段；库里出现这一行说明它绕过了"
                     "normalize_definition。")
        version = FORMAT_VERSION
    else:
        try:
            version = int(version_raw)
        except (TypeError, ValueError) as exc:
            raise DefinitionError(
                "E_PIPELINE_BAD_FORMAT",
                f"format_version 必须是整数，收到 {version_raw!r}") from exc
    if version > FORMAT_VERSION:
        raise DefinitionError(
            "E_PIPELINE_FORMAT_TOO_NEW",
            f"定义是 v{version}，本程序只认识到 v{FORMAT_VERSION} —— "
            f"程序版本低于定义版本，拒绝继续。",
            hint="升级 magpie，或把定义降到本程序认得的版本。"
                 "不认识的格式必须响亮地拒绝，而不是尽力而为地读一半 —— "
                 "读了一半的定义跑出来的是一份看着成功、实际漏了一段的东西。")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise DefinitionError("E_PIPELINE_BAD_DEFINITION", "steps 必须是非空列表")
    steps = [_normalize_step(step, i) for i, step in enumerate(steps_raw)]

    ids = [s["id"] for s in steps]
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            raise DefinitionError(
                "E_PIPELINE_BAD_DEFINITION",
                f"步骤 id {sid!r} 重复（id 按字符串比较，`1` 与 `\"1\"` 视为同一个）")
        seen.add(sid)
    for step in steps:
        for dep in step["after"]:
            if dep not in seen:
                raise DefinitionError(
                    "E_PIPELINE_BAD_DEFINITION",
                    f"step {step['id']}: after 引用了不存在的步骤 {dep!r}；"
                    f"已定义的步骤：{ids}")
    order_steps(steps)                                  # 顺带做一次环检测

    # base_dir：定义里写的 > 调用方给的 > 定义文件所在目录
    explicit = raw.get("base_dir")
    anchor = Path(str(raw["_path"])).parent if raw.get("_path") else Path(".")
    chosen = Path(str(explicit)) if explicit else (
        Path(str(base_dir)) if base_dir is not None else anchor)
    if not chosen.is_absolute():
        chosen = anchor / chosen
    base = chosen.resolve()

    project_value = raw.get("project") if project is None else project

    out: dict[str, Any] = {
        "format_version": version,
        "name": str(raw.get("name") or ""),
        "project": None if project_value is None else str(project_value),
        "base_dir": str(base),
        "data_dir": None if raw.get("data_dir") in (None, "") else str(raw["data_dir"]),
        "scope": _canonical_scope(raw.get("scope")),
        "steps": steps,
    }
    # `name` **可以缺**：文件入口从 2.5 起就允许不写它（`run_pipeline` 退回
    # YAML 的文件名）。库入口必须有 —— 那一侧的纪律由 `pipeline_definitions`
    # 的 `name NOT NULL` 与 `ops.save_pipeline` 守着，不在这里再判一次：
    # "同一份定义两种读法答得不一样"正是本节要收敛的东西，把一条只在库入口成立
    # 的规则写进公共的归一化，就是把那个分歧请回来。
    for key in RUNTIME_KEYS:                            # 运行期键原样留着给入口用
        if key in raw:
            out[key] = raw[key]
    return out


#: 逐级升级函数：`_UPGRADES[n]` 把 `format_version == n` 的定义升到 `n + 1`。
#:
#: 今天是空的 —— 只有 v1，没有"旧版本"可升。它留在这里是因为**机制**比版本号
#: 重要：F3 规定"读时升级、不落库改写"，而一旦第一个 v2 出现，这条纪律唯一
#: 可执行的形态就是这张表。有一条测试钉住 `set(_UPGRADES) == {1..FORMAT_VERSION-1}`
#: （`magpie/core/tests/test_pipeline_definitions.py`）—— 所以"加一个新版本"
#: 与"写一个它的升级步骤 + 用例"是同一个动作，不可能只做前一半。
_UPGRADES: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def upgrade_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """把定义**在内存里**升到当前版本。库里那行不动。

    已发生的 run 的 `definition_hash` 必须继续对得上它当时跑的那一版 —— 这与
    `scope_version` / `parser_fingerprint` 的快照纪律是同一条：历史说的是当时
    的事实，不能被今天的一次读取改写。
    """
    version_raw = definition.get("format_version")
    if version_raw is None:
        raise DefinitionError("E_PIPELINE_BAD_FORMAT",
                              "定义没有 format_version，无法升级")
    version = int(version_raw)
    if version > FORMAT_VERSION:
        raise DefinitionError(
            "E_PIPELINE_FORMAT_TOO_NEW",
            f"定义是 v{version}，本程序只认识到 v{FORMAT_VERSION} —— 拒绝继续。")
    out = dict(definition)
    while version < FORMAT_VERSION:
        step = _UPGRADES.get(version)
        if step is None:                                # pragma: no cover —— 由测试钉住
            raise DefinitionError(
                "E_PIPELINE_BAD_FORMAT",
                f"没有从 v{version} 升到 v{version + 1} 的升级步骤 —— "
                f"迁移表与 FORMAT_VERSION 分叉了")
        out = step(out)
        version += 1
        out["format_version"] = version
    return out


def canonical_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """规范形态**去掉运行时键** —— 存库与 `definition_hash` 都用它。

    `_path` 不进库：它说的是"这份定义是从哪个文件读来的"，那是入口的信息。
    把它写进库，同一个定义换一个入口读就会得到另一个 hash。
    """
    return {k: v for k, v in definition.items() if k not in RUNTIME_KEYS}


def definition_hash(definition: dict[str, Any]) -> str:
    """`sha256(canonical JSON)`。**它是事实**（"这次跑的是哪一版"），
    与 `revision`（乐观锁）是两个东西 —— 理由见 `schema/008` 的注释。"""
    return store.sha256_hex(store.canonical_json(canonical_definition(definition)))


# ---------------------------------------------------------------------------
# 定义加载与校验（YAML 一律 lazy import —— core 只用标准库，红线 R6）
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    try:
        import yaml                                        # noqa: PLC0415
    except ImportError as exc:                             # pragma: no cover
        raise RuntimeError(
            "读 YAML 管线定义需要 PyYAML，但它不在当前解释器里。\n"
            "  pip install pyyaml   （或 py -3.13 -m pip install pyyaml）\n"
            "它只在需要 YAML 时才 import，所以核心功能不受影响（core 仅标准库）。"
        ) from exc
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_definition(path: str | Path) -> dict[str, Any]:
    """读入并校验管线定义，返回**规范形态**（§2.2）。任何问题都抛可读的 ValueError。

    这是**文件入口**，它给旧 YAML 盖上 `format_version: 1` —— 于是"没有版本
    字段的那一天"永远不会到来，而那一天正是这个字段存在的理由（F3）。
    校验（id 唯一 / after 存在 / 无环）与归一化**同一份实现**：两个载入器谁都不
    许自己拼一遍，否则会落到"同一个定义、两种读法"那类没有症状的缺陷上（A6）。
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"管线定义不存在：{path}")
    try:
        raw = _load_yaml(path)
    except Exception as exc:
        raise ValueError(f"{path} 不是合法 YAML：{type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 的顶层必须是映射（name / project / steps）")
    raw = dict(raw)
    raw["_path"] = str(path)
    try:
        return normalize_definition(raw)
    except DefinitionError as exc:
        raise DefinitionError(exc.code, f"{path}: {exc}",
                              hint=exc.hint) from None


def order_steps(steps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `after` 依赖做稳定拓扑排序。有环 → ValueError（并指出环上的步骤）。

    这个函数**必须**拒绝重复 id: `by_id` 是 `{str(id): step}`，两个步骤归一化到
    同一个 id 时后者会覆盖前者，被覆盖的那一步从此不在 `ordered` 里 —— 静默丢步。
    `load_definition` 已经判过一次重（带下标，报错更好读），这里再判一次是因为
    "丢"就发生在本函数：直接调用本函数的调用方不该拿到一个少了一步的结果。
    """
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        key = str(step["id"])
        if key in by_id:
            raise ValueError(f"steps 里有重复的 id {key!r}（后一个会覆盖前一个 → 静默丢步）")
        by_id[key] = step
    ordered: list[dict[str, Any]] = []
    done: set[str] = set()
    visiting: list[str] = []

    def visit(sid: str) -> None:
        if sid in done:
            return
        if sid in visiting:
            cycle = " → ".join(visiting[visiting.index(sid):] + [sid])
            raise ValueError(f"after 依赖成环：{cycle}")
        if sid not in by_id:
            raise ValueError(f"after 引用了不存在的步骤 {sid!r}；已定义的步骤：{sorted(by_id)}")
        visiting.append(sid)
        for dep in by_id[sid].get("after") or []:
            visit(str(dep))
        visiting.pop()
        done.add(sid)
        ordered.append(by_id[sid])

    for step in steps:                                      # 就地顺序优先，结果可复现
        visit(str(step["id"]))
    return ordered


# ---------------------------------------------------------------------------
# 项目解析
# ---------------------------------------------------------------------------


def resolve_project(conn: sqlite3.Connection, definition: dict[str, Any]) -> dict[str, Any]:
    """`project:` 可以是 id、名字；不存在且给了 scope 时就地创建。"""
    spec = definition.get("project")
    if spec is None:
        raise ValueError("管线定义缺少 project:（项目名或 id）")
    row = None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        row = conn.execute("SELECT id, name FROM projects WHERE id=?", (int(spec),)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, name FROM projects WHERE name=?", (str(spec),)).fetchone()
    if row is not None:
        return {"id": int(row["id"]), "name": str(row["name"]), "created": False}

    scope = definition.get("scope")
    if not isinstance(scope, dict) or not scope:
        raise ValueError(
            f"项目 {spec!r} 不存在，且管线定义里没有 scope —— 无法安全地凭空建项目。\n"
            f'加一段 scope: {{include: ["*.example.com"], exclude: []}} 即可。')
    scope = dict(scope)
    if definition.get("parser_fingerprint"):
        # 项目的当前 parser 随 scope 一起版本化（§5.1）；create_project 会登记它。
        scope.setdefault("parser_fingerprint", str(definition["parser_fingerprint"]))
    project_id = ops.create_project(conn, str(spec), scope)
    return {"id": project_id, "name": str(spec), "created": True,
            "scope_version": 1}


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def resolve_sources(base: str | Path, step: dict[str, Any]
                    ) -> tuple[list[Path], list[str]]:
    """把一个 step 的 `source` 展开成要导入的路径列表。返回 (paths, problems)。

    三种 kind 的行为**本来就不一样**（3.7a 冻结 F1），用一个字符串表达它们就
    必须靠猜（有没有 `*`？是不是目录？），而猜错的那一半不会有任何症状：

      * `file` —— 指名道姓的单个文件。找不到是**配置错误**，不是一次空导入，
        所以它抛 `E_SOURCE_NOT_FOUND`（与 `POST /imports` 对不存在的路径在
        提交之前就报 `E_PATH_NOT_FOUND` 同一条纪律）。
      * `glob` —— 通配模式，0..N。0 个是**报告的 problems**（保持今天的行为），
        不是硬错误：一条"今天的上游还没产出"的管线不该跑不起来。
      * `dir`  —— 目录，交给 `ingest.discover` 递归发现（它按扩展名过滤）。
    """
    base = Path(base)
    kind = str(step["source"]["kind"])
    pattern = str(step["source"]["pattern"])
    path = Path(pattern)
    if not path.is_absolute():
        path = base / pattern

    if kind == "file":
        if not path.is_file():
            raise DefinitionError(
                "E_SOURCE_NOT_FOUND",
                f"step {step['id']}: source 指名的文件不存在：{path}\n"
                f"  （base_dir = {base}）",
                hint="file 的语义是「我指名道姓要这个文件」，它不存在是配置错误，"
                     "不是一次空导入。要匹配 0..N 个文件请用 kind: \"glob\"。")
        return [path], []

    if kind == "dir":
        if not path.is_dir():
            return [], [f"source 目录不存在：{path}（base_dir = {base}）"]
        return [path], []

    if Path(pattern).is_absolute():
        # `Path.glob` 不接受绝对模式（NotImplementedError）。绝对 pattern 在
        # 库里是合法的（保存时 base_dir 已经是绝对的），所以这里单独走 glob 模块。
        matched = sorted(Path(p) for p in glob_module.glob(pattern, recursive=True))
    else:
        matched = sorted(base.glob(pattern))
    problems = [] if matched else [
        f"source {pattern!r} 没有匹配到任何文件（基准目录 {base}）"]
    return matched, problems


def run_pipeline(conn: sqlite3.Connection, definition: dict[str, Any], *,
                 base_dir: str | Path | None = None,
                 data_dir: str | Path | None = None,
                 dry_run: bool = False, archive: str = "archived",
                 actor: str = "pipeline",
                 on_progress: Callable[[dict[str, Any]], None] | None = None
                 ) -> dict[str, Any]:
    """顺序执行整条管线，返回汇总报告（可直接 json.dumps）。

    `definition` 可以是**规范形态**（`normalize_definition` 的产物），也可以是
    一份刚读来的原始映射 —— 本函数入口会归一化一次（幂等）。这条宽容是刻意的：
    `run_pipeline` 是"执行"的入口，不该要求每个调用方先自己走一遍载入器 ——
    两个入口各拼一次定义，正是 3.7a 要收敛掉的那种分叉。

    `on_progress` 原样传给每一步的 `ingest.IngestOptions`。**API 层不自己数数**
    —— 那样迟早会出现"进度条说 80%，报告说 100%"（`imports.py` 的原话）。进度
    只有一个来源，就是 ingest 的回调。
    """
    started = time.perf_counter()
    definition = normalize_definition(definition, base_dir=base_dir)
    definition_path = Path(str(definition.get("_path") or "."))
    base = Path(str(definition["base_dir"]))
    dd = Path(data_dir) if data_dir is not None else Path(
        definition.get("data_dir") or (base / ".magpie"))
    if not dd.is_absolute():
        dd = base / dd
    dd = dd.resolve()

    steps = order_steps(definition["steps"])

    # `file` 的"找不到就硬错误"在**跑之前**做完，不在第一个 step 跑了一半之后
    # —— 与 `POST /imports` 在提交之前校验路径同一条理由：一个写错路径的 step
    # 不该只让自己失败，然后留下半条管线的效果让人去收拾。
    for step in steps:
        resolve_sources(base, step)

    project = resolve_project(conn, definition)
    step_reports: list[dict[str, Any]] = []
    produced: dict[str, int] = {}

    # 干跑的事务边界是**整条命令**，不是一次 `ingest_paths` —— 见 `dry_run_scope`。
    # 修之前这条命令的干跑是"每个 step 各自一个事务、各自 ROLLBACK"，第 2 个 step
    # 看不见第 1 个 step 刚写下的事实，于是多 step 的 flow 上 `--dry-run` **低报
    # `updated`**（重述已有事实的记录被算成新增）。真跑那一边每个 step 收尾 COMMIT，
    # 第 2 个 step 看得见 —— 同一条命令行，两端的库状态本就不同，干跑说的不是真话。
    #
    # 边界开在 `resolve_project` **之后**：干跑仍然建项目（"文件的落点必须先存在"
    # 是这条链路的前半段，不是事实），那部分照旧提交；被回滚的只有事实。
    with ingest.dry_run_scope(conn, dry_run) as command_txn:
        for step in steps:
            sid = str(step["id"])
            matched, problems = resolve_sources(base, step)
            deps = [str(d) for d in (step.get("after") or [])]
            dependency_empty = bool(deps) and all(produced.get(d, 0) == 0 for d in deps)

            opts = ingest.IngestOptions(
                project_id=project["id"], data_dir=dd,
                parser=step.get("parser"), source_name=step.get("source_name"),
                authority_level=str(step.get("authority") or "unverified"),
                archive=str(step.get("archive") or archive),
                dry_run=bool(dry_run or step.get("dry_run")), actor=actor,
                # 报告副本落进 `reports/`（坏行明细本来就在那儿）。doctor 报"未知
                # 字段"只能靠它 —— 那个信号只活在一次解析里，事后无迹可查。
                report_dir=dd / "reports",
                batch_size=int(step.get("batch_size") or ingest.DEFAULT_BATCH),
                on_progress=on_progress)
            # `command_txn` 为真（整条命令干跑）时本 step 不自持边界。step 自己写
            # 了 `dry_run: true` 而命令没开 `--dry-run` 时它是假 —— 那种 step 仍
            # 自持边界，与以前一模一样。
            report = ingest.ingest_paths(conn, opts, matched,
                                         _shared_txn=command_txn)
            produced[sid] = sum(1 for f in report["files"]
                                if f.get("state") in _PRODUCED_STATES)
            step_reports.append({
                "id": sid,
                "parser": step.get("parser"),
                "source": dict(step["source"]),
                "source_name": step.get("source_name"),
                "after": deps,
                "dependency_empty": dependency_empty,
                "matched": len(matched),
                "problems": problems + list(report.get("problems") or []),
                "totals": report["totals"],
                "job_ids": report["job_ids"],
                "files": [{k: f.get(k) for k in
                           ("path", "state", "job_id", "inbox_item_id", "sha256",
                            "parser", "counts", "reconcile", "integrity",
                            "unknown_fields_detail", "skip_reason", "reason",
                            "parse_error", "badlines_report", "encoding",
                            "arbitration")}
                          for f in report["files"]],
            })

    totals: dict[str, int] = {}
    for step in step_reports:
        for name, value in step["totals"].items():
            totals[name] = totals.get(name, 0) + int(value)

    return {
        "kind": "pipeline",
        "pipeline": str(definition.get("name") or definition_path.stem),
        # 库入口的定义**没有**"所在文件"，这里如实给 null。给 "." 会让报告里
        # 出现一个看着像路径、其实指向当前目录的东西 —— 报告是唯一的交付通道，
        # 它里面不该有一个会被读成事实的占位符。
        "definition": None if not definition.get("_path") else str(definition_path),
        "definition_hash": definition_hash(definition),
        "dry_run": bool(dry_run),
        "project": project,
        "base_dir": str(base),
        "data_dir": str(dd),
        "steps": step_reports,
        "totals": totals,
        "job_ids": [jid for step in step_reports for jid in step["job_ids"]],
        "db": {
            "observations": _scalar(conn, "SELECT COUNT(*) FROM observations"
                                          " WHERE project_id=? AND is_current=1",
                                    project["id"]),
            "relations": _scalar(conn, "SELECT COUNT(*) FROM relations"
                                       " WHERE project_id=? AND is_current=1",
                                 project["id"]),
            "entities": _scalar(conn, "SELECT COUNT(*) FROM entities"
                                      " WHERE project_id=? AND is_current=1",
                                project["id"]),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _scalar(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = store.connect(str(db_path))
    store.init_db(conn)
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, file=sys.stderr)
        return EXIT_OK if args else EXIT_USAGE

    command, rest = args[0], args[1:]
    flags, positional = _parse_flags(rest)

    if command == "run":
        if not positional:
            print("run 需要 <pipeline.yaml>\n\n" + USAGE, file=sys.stderr)
            return EXIT_USAGE
        try:
            definition = load_definition(positional[0])
        except ValueError as exc:
            print(f"!! {exc}", file=sys.stderr)
            return EXIT_USAGE
        # data_dir 只解析一次，db 与归档目录必须落在**同一个**基准下 ——
        # 分开算过一次，结果 db 建在 CWD 而归档在 YAML 同级目录，报告里的数字
        # 是真的，但人打开那个 .db 却是空的。
        #
        # `--base-dir` 是**显式的运行时覆盖**，所以它直接改写定义里的 base_dir：
        # 归一化的优先级是"定义里写的 > 调用方给的"，因为库入口的调用方不该
        # 用一个参数悄悄改掉一份存下来的定义说的是哪个目录。CLI 这里表达的
        # 正是"把这份定义当成在 X 下跑"，那是一次改写，就写成一次改写。
        base = Path(flags["base-dir"]) if flags.get("base-dir") else Path(
            str(definition.get("_path") or ".")).parent
        base = base.resolve()
        if flags.get("base-dir"):
            definition = dict(definition, base_dir=str(base))
        dd = Path(flags.get("data-dir") or definition.get("data_dir") or ".magpie")
        if not dd.is_absolute():
            dd = base / dd
        dd = dd.resolve()
        db_path = Path(flags["db"]) if flags.get("db") else dd / "magpie.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open_db(db_path)
        try:
            report = run_pipeline(conn, definition, base_dir=base,
                                  data_dir=dd, dry_run=bool(flags.get("dry-run")))
        except (ValueError, store.InvariantError) as exc:
            print(f"!! {exc}", file=sys.stderr)
            conn.close()
            return EXIT_FAIL
        _emit(report)
        failed = [f for s in report["steps"] for f in s["files"]
                  if f.get("state") in ("failed", "needs_confirmation")]
        conn.close()
        return EXIT_FAIL if failed else EXIT_OK

    if command == "undo":
        if not positional or not positional[0].isdigit():
            print("undo 需要 <job_id>\n\n" + USAGE, file=sys.stderr)
            return EXIT_USAGE
        db_path = flags.get("db")
        if not db_path:
            print("undo 需要 --db <path>", file=sys.stderr)
            return EXIT_USAGE
        conn = _open_db(db_path)
        try:
            report = ops.undo(conn, int(positional[0]), dry_run=bool(flags.get("dry-run")))
        except store.InvariantError as exc:
            print(f"!! {exc}", file=sys.stderr)
            return EXIT_FAIL
        finally:
            conn.close()
        _emit(report)
        return EXIT_OK

    if command == "check":
        if not positional:
            print("check 需要 <db_path>", file=sys.stderr)
            return EXIT_USAGE
        conn = _open_db(positional[0])
        try:
            report = store.integrity_check(conn)
        finally:
            conn.close()
        _emit({"kind": "check", "ok": bool(report.get("ok")),
               "violations": report.get("violations") or [],
               "foreign_key_check": report.get("foreign_key_check") or [],
               "sqlite_integrity": report.get("sqlite_integrity")})
        return EXIT_OK if report.get("ok") else EXIT_FAIL

    print(f"未知子命令 {command!r}\n\n{USAGE}", file=sys.stderr)
    return EXIT_USAGE


def _parse_flags(args: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """极简参数解析：只要 `--flag` / `--key value`。

    刻意不引 argparse：入口只有十几行的体量，而 argparse 的子命令 + 全局选项
    组合会把"最小入口"变成半个 CLI（那是 2.6 的活）。
    """
    flags: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item.startswith("--"):
            name = item[2:]
            if name in ("dry-run", "force", "quiet"):
                flags[name] = True
            elif index + 1 < len(args):
                flags[name] = args[index + 1]
                index += 1
            else:
                flags[name] = True
        else:
            positional.append(item)
        index += 1
    return flags, positional


def _emit(report: dict[str, Any]) -> None:
    """打印 JSON 汇总报告。**报告是唯一的交付通道**（人看的是它，不是退出码）。"""
    text = json.dumps(report, ensure_ascii=False, indent=2)
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8")           # Windows 控制台默认 GBK
    except (AttributeError, ValueError):               # pragma: no cover
        pass
    print(text, file=stream)


if __name__ == "__main__":                             # pragma: no cover
    sys.exit(main())
