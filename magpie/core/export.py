"""导出与打包（4.4）—— 默认脱敏的自洽包，`--full` 才带归档原文。

一句话
------
`build_package()` 把一个项目的导出物拼成一份**自洽的包**：`{相对路径: 字节}`。
包里带 `manifest.json`（格式自描述）与 `README.md`（人读说明），所以拿到包的
人**不装本工具也能看懂**。默认包只含结构化数据 + 一个不含正文的原文索引；
`--full` 才额外塞进 `stored` 归档的原文正文。

为什么默认不含原文正文（这是本模块的核心裁定）
----------------------------------------------
计划书 4.4 把"默认泄露"点名为**唯一的失败模式**：默认值错了，用户会把一份带
凭据的包发给别人，而且不会有测试替他发现。所以这里选的是**结构性排除**，不是
"导出后再清洗"：

* headers / cookie / 凭据明文只存在于**原文正文**里（4.2 保证结构化数据里
  没有明文；解析器也从不把 header 存进观测）；
* 默认包**根本不带正文** —— 于是"包里找不到 headers/cookie/明文"是**结构上
  成立**的，不依赖某条清洗正则恰好没漏；
* `--full` 才带正文，并在命令行上给一条**一眼可见的告警**（见 `cli/read.py`
  的 `cmd_export`）。

两道防线叠在一起，而不是二选一：

1. **结构防线**：默认不含正文（上面那条）。
2. **值级防线（defense-in-depth）**：即便某天一个解析器把凭据形状的串写进了
   观测值，`redact_value()` 也会在导出时把它掩掉（`detect_secrets` →
   `mask_secret`）。它在干净数据上是**恒等**的，所以不破坏往返；它只在真的
   出现凭据形状时才动手。

`secret` 实体（4.2 的产物）在**两种模式下都只出哈希 + 掩码前缀**：库里本来
就没有明文（4.2 "明文永不落库"），导出侧的红线是"明文永不出包"，两者在这里
接上。`--full` 出的是**归档里的原文**（一份原始抓包，里面可能带凭据），**不是
被扫出来的凭据值** —— 这条边界写在 `README.md` 与交付说明里。

四条纪律
--------

**① external 正文一个字节都不复制**（4.1 的红线）。`custody='external'` 的
原文在工具之外，库里只有路径 + sha256；把它复制进包就等于把 4.1 那条
"绝不逐包入库"绕过去了。所以 external 在 `raw_index.json` 里**只带路径 +
sha256**，`--full` 也不例外。

**② 包内不放指向本机绝对路径的"活链接"**。external 的路径是**数据**（4.1 要求
带它），不是链接 —— 它以普通字符串落在 JSON 里；`README.md` 里**不**把它渲染
成 markdown 链接、也没有任何 `file://` / 外部 URL。stored 归档的本机路径干脆
**不进包**（包里有 sha256 与 `--full` 的正文，够认人）。

**③ 零出站（R7）**：本模块只用标准库读写本地文件，没有任何网络调用；包里的
说明文件也不引用外部域名/CDN/字体 —— 对照 `R7-ASSETS` 那条教训：查看者打开
说明时**不许被动发起请求**。

**④ 往返**：`entities.json` 是 `query.export_entities()` 的原样（+ 值级掩码），
`read_package()` 能把它读回来，干净数据上与导出逐项相等。刻意不往返的部分
（external 正文、默认模式下的 stored 正文）在 `README.md` 与 manifest 的
`notes` 里写明 —— 判据允许"明确说明哪一部分是刻意不往返的"。

模块边界
--------
SQL 一条都没有（`SQL-FREE` 扫 `cli/read.py` 与 `api/routes.py`，但这里也照样
不写）：取数全部经 `query.export_entities` / `query.timeline` /
`query.raw_file_index`。本模块只做**拼装**（取数 → 掩码 → 渲染 → 落盘/返回）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

from . import normalize, query
from .store import now as _now

__all__ = ["FORMAT_VERSION", "README_NAME", "MANIFEST_NAME",
           "redact_value", "build_package", "write_package", "read_package"]


#: 包格式版本。变了就 +1，并在 README 的格式段里说清 —— 读包的人靠它判断
#: "我这份说明还对不对"。
FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
README_NAME = "README.md"

#: 值级掩码的迭代上限。`detect_secrets` 每条规则每行只报**第一次**命中，所以
#: 一个值里若有两个不同的凭据，要多跑几遍才都盖住。掩码本身不会被再次命中
#: （`ghp_****` 不满足 `github_token` 的 20+ 字母数字），所以循环必然收敛；
#: 上限只是防止某天规则改出一个自匹配的病态形状时死循环。
_MAX_REDACT_ROUNDS = 8


# ---------------------------------------------------------------------------
# 值级掩码（defense-in-depth）
# ---------------------------------------------------------------------------


def redact_value(value: Any) -> Any:
    """把一个字符串里**凭据形状的子串**换成掩码。非字符串 / 无命中 → 原样返回。

    这是导出的第二道防线（第一道是"默认不带正文"）。它**不动** `secret` 实体
    的哈希与掩码前缀 —— 那些本来就是可出的（4.2 只落了哈希 + 掩码，库里没有
    明文可漏），而 `github_token:<hex>` 这种实体键也过不了任何一条规则（见
    模块文件头的裁定④：干净数据上它恒等，不破坏往返）。

    `detect_secrets` 报 `{"rule", "value", "masked"}`；这里把 `value` 在串里
    换成 `masked`。多轮跑到没有新命中为止（或到 `_MAX_REDACT_ROUNDS`）。
    """
    if not isinstance(value, str) or not value:
        return value
    text = value
    for _ in range(_MAX_REDACT_ROUNDS):
        hits = normalize.detect_secrets(text)
        if not hits:
            return text
        new = text
        for hit in hits:
            plain, masked = str(hit["value"]), str(hit["masked"])
            if plain and plain in new:
                new = new.replace(plain, masked)
        if new == text:
            return text          # 没有可替换的（防御：避免空转）
        text = new
    return text


def _redact_entities(entities: list[dict]) -> list[dict]:
    """对导出实体的**每个字段值**做值级掩码。键（身份）不动 —— 动了就破往返。"""
    for entity in entities:
        for field in entity.get("fields", ()):
            field["value"] = redact_value(field["value"])
    return entities


def _redact_timeline(timeline: dict) -> dict:
    """时间线事件的 `value` 同样过一遍掩码（与实体一条口径）。"""
    for bucket in ("events", "lower_bounds"):
        for event in timeline.get(bucket, ()):
            event["value"] = redact_value(event["value"])
    return timeline


# ---------------------------------------------------------------------------
# 原文索引
# ---------------------------------------------------------------------------


def _plain_path(uri: str) -> str:
    """`file:///D:/x` → `D:/x`；`file:///home/u` → `/home/u`。

    只为把 external 的 locator 落成**普通路径字符串**（数据），而不是一个
    `file://` URI —— 后者读起来像个"链接"，而包里不放指向本机绝对路径的活
    链接（纪律②）。URL 解析用纯字符串切片，不引 `urllib`（R7 对 `urllib`
    的判定边界不值得在这儿赌）。
    """
    if not uri.startswith("file://"):
        return uri
    path = uri[len("file://"):]
    # Windows：`/D:/x` → `D:/x`（去掉 URI 里盘符前多出的那个斜杠）
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def _read_stored_body(row: dict) -> bytes | None:
    """读一份 `stored` 归档的正文。找不到（purged / 搬家）→ `None`，不炸。"""
    locator = str(row.get("locator_uri") or "")
    if not locator:
        return None
    try:
        return Path(_plain_path(locator)).read_bytes()
    except OSError:
        return None


def _raw_index(rows: list[dict], *, full: bool) -> tuple[list[dict], dict[str, bytes]]:
    """原文索引 + 要打进包的正文。

    * `external` → **只带路径 + sha256**（4.1 红线），正文永不复制，`--full`
      也不例外；
    * `stored` → 索引带 sha256 + 大小 + 状态；`--full` 时才读正文进
      `raw/<sha256>`（纪律：正文是"归档里的原文"，只在显式选择后出门）。
    """
    index: list[dict] = []
    bodies: dict[str, bytes] = {}
    for row in rows:
        sha = str(row["sha256"])
        custody = str(row["custody"])
        entry = {
            "sha256": sha,
            "byte_count": int(row["byte_count"]),
            "storage_state": str(row["storage_state"]),
            "custody": custody,
        }
        if custody == "external":
            entry["path"] = _plain_path(str(row.get("locator_uri") or ""))
            entry["packaged_as"] = None          # 正文永不进包
        else:
            entry["packaged_as"] = None
            if full:
                body = _read_stored_body(row)
                if body is not None:
                    bodies[f"raw/{sha}"] = body
                    entry["packaged_as"] = f"raw/{sha}"
        index.append(entry)
    return index, bodies


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _dumps(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False,
                       default=str) + "\n").encode("utf-8")


def _entities_csv(entities: list[dict]) -> bytes:
    """实体 → CSV（`entity_id, entity_key, kind, field, value, confidence`）。

    与 `/api/v1/export?format=csv` 的表头一致 —— 同一份导出数据的两种渲染，
    表头分叉会让收包的人对着两列同名不同义的字段发懵。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["entity_id", "entity_key", "kind", "field", "value",
                     "confidence"])
    for entity in entities:
        for field in entity.get("fields", ()):
            writer.writerow([entity["id"], entity["entity_key"], entity["kind"],
                             field["field"], field["value"], field["confidence"]])
    return buf.getvalue().encode("utf-8")


def _readme(*, full: bool, counts: dict, semantics: str) -> bytes:
    """包里的人读说明。**零外部链接、零 `file://`、零本机绝对路径**（纪律②③）。

    它是"拿到包的人不装本工具也能看懂"的那半边：每个文件是什么、字段怎么读、
    时间语义、脱敏边界、以及刻意不往返的部分。
    """
    raw_note = (
        "`raw/` 下是 `stored` 归档的原文正文（**含 headers / cookie / 凭据明文**"
        "—— 这就是 `--full` 那条告警的意思）。`external` 的原文**不在包里**，"
        "索引只带路径 + sha256。"
        if full else
        "默认包**不含任何原文正文** —— headers / cookie / 凭据明文只存在于原文里，"
        "而原文这一版没带，所以包里结构上就找不到它们。要看原文请用 `--full` "
        "重新导出（会有告警）。"
    )
    lines = [
        "# Magpie 导出包",
        "",
        f"格式版本 `{FORMAT_VERSION}`；由 Magpie 导出。",
        "",
        "## 里面有什么",
        "",
        f"- `{MANIFEST_NAME}` — 自描述清单：格式版本、生成时间、脱敏模式、"
        f"每个文件的 sha256、计数、以及下面「刻意不往返」那些的说明。",
        "- `entities.json` — 实体 + 观测字段（机器可读，规范形态；"
        "`read_package()` 读得回）。",
        "- `entities.csv` — 同一份数据的表格形态（给人用电子表格看）。",
        "- `timeline.json` — 项目时间线（时间语义见下）。",
        "- `secrets.json` — `secret` 类实体：**只有 SHA-256 + 规则名 + 位置 + "
        "掩码前缀**，两种模式都不含明文（4.2 的「明文永不落库/不出包」）。",
        "- `raw_files.json` — 原文索引：`stored` 给 sha256 + 大小 + 状态；"
        "`external` 另带**路径**（4.1 只承诺路径 + sha256）。",
    ]
    if full:
        lines.append("- `raw/<sha256>` — `stored` 归档原文的正文（逐字节）。")
    lines += [
        "",
        "## 字段怎么读",
        "",
        "- 实体：`id` / `entity_key`（身份）/ `kind`（类型）/ `lifecycle`；"
        "`fields[]` 是观测：`field` / `value` / `confidence`（§8.2 置信度）。",
        "- 观测值在默认与 `--full` 下都过了**值级掩码**（凭据形状的子串会被换成"
        "`xxxx****`）—— 这是防线之二；防线之一是默认不带正文。",
        "",
        "## 时间线的时间语义（§8.1，未新造）",
        "",
        f"- {semantics}",
        "",
        "## 原文（raw）与脱敏边界",
        "",
        f"- {raw_note}",
        "- `--full` 出的是**归档里的原文**（原始抓包），**不是**被扫出来的凭据值；"
        "`secret` 实体在 `--full` 下也仍然只有哈希 + 掩码。",
        "",
        "## 刻意不往返的部分",
        "",
        "- `external` 原文正文：**从不进包**（4.1 红线），所以不往返 —— 包里只有"
        "它的路径 + sha256。",
        "- 默认模式下的 `stored` 原文正文：**不在包里**，所以也不往返（`--full` "
        "会带上，那时它逐字节往返）。",
        "- 身份/关系/标签等未导出的层：本包是**实体 + 时间线 + 原文索引**，不是库的"
        "全量镜像。",
        "",
        "## 计数",
        "",
    ]
    for key, val in counts.items():
        lines.append(f"- {key}: {val}")
    lines.append("")
    text = "\n".join(lines)
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# 拼装
# ---------------------------------------------------------------------------


def build_package(conn, project_id: int, *, full: bool = False,
                  tool_version: str = "", project_name: str = "") -> dict[str, bytes]:
    """把一个项目的导出物拼成包：`{相对路径: 字节}`。

    纯函数式（除了读 DB 与 `--full` 时读归档正文），不落盘 —— 落盘由
    `write_package()` 负责。返回的 dict 就是"包内所有文件"，测试直接扫它或扫
    落盘后的目录都行。

    `tool_version` / `project_name` 由调用方（CLI）传入 —— core 不 import 包根，
    免得 R6 的顶层 import 判定又要多认一个名字。
    """
    pid = int(project_id)
    entities = _redact_entities(query.export_entities(conn, pid))
    timeline = _redact_timeline(query.timeline(conn, pid))
    raw_rows = query.raw_file_index(conn, pid)
    raw_index, bodies = _raw_index(raw_rows, full=full)

    secrets = [e for e in entities if e.get("kind") == "secret"]
    counts = {
        "entities": len(entities),
        "secrets": len(secrets),
        "timeline_ranked": timeline["counts"]["ranked"],
        "timeline_lower_bounds": timeline["counts"]["lower_bounds"],
        "raw_files": len(raw_index),
        "raw_files_stored": sum(1 for r in raw_index if r["custody"] == "stored"),
        "raw_files_external": sum(1 for r in raw_index if r["custody"] == "external"),
        "raw_bodies_included": len(bodies),
    }

    files: dict[str, bytes] = {
        "entities.json": _dumps(entities),
        "entities.csv": _entities_csv(entities),
        "timeline.json": _dumps(timeline),
        "secrets.json": _dumps(secrets),
        "raw_files.json": _dumps(raw_index),
    }
    files.update(bodies)

    files[README_NAME] = _readme(full=full, counts=counts,
                                  semantics=str(timeline.get("semantics") or ""))

    # manifest 最后拼（要把其它每个文件的 sha256 收进去，唯独不含它自己 ——
    # 自己的哈希放不进自己里，这是清单的常态，不是缺陷）。
    manifest = {
        "format": "magpie-export",
        "format_version": FORMAT_VERSION,
        "tool_version": tool_version,
        "generated_at": _now(),
        "redaction": "full" if full else "default",
        "project": {"id": pid, "name": project_name},
        "counts": counts,
        "semantics": timeline.get("semantics"),
        "notes": [
            "默认包不含原文正文（headers/cookie/凭据只活在原文里）；--full 才带"
            " stored 原文。",
            "external 原文只带路径 + sha256，正文永不进包（4.1 红线）。",
            "secret 实体两种模式都只有哈希 + 掩码前缀（明文永不出包）。",
            "包内无外部资源引用、无本地文件链接（R7 / R7-ASSETS：查看者不被动"
            "发起请求）。",
        ],
        "files": [
            {"name": name,
             "sha256": hashlib.sha256(content).hexdigest(),
             "bytes": len(content)}
            for name, content in sorted(files.items())
        ],
    }
    files[MANIFEST_NAME] = _dumps(manifest)
    return files


def write_package(files: dict[str, bytes], out_dir: str | os.PathLike) -> Path:
    """把包落到磁盘目录。已存在的同名文件被覆盖（导出是幂等的重跑）。"""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def read_package(out_dir: str | os.PathLike) -> dict[str, Any]:
    """把一个包**读回来**（往返的读侧）。至少覆盖判据要的那一份 `entities.json`。

    返回 `{"manifest", "entities", "timeline", "secrets", "raw_files"}`，各自是
    对应 JSON 文件的内容；缺哪个文件就哪个键为 `None`（老包/半包不炸）。
    """
    root = Path(out_dir)

    def _load(name: str):
        path = root / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    return {
        "manifest": _load(MANIFEST_NAME),
        "entities": _load("entities.json"),
        "timeline": _load("timeline.json"),
        "secrets": _load("secrets.json"),
        "raw_files": _load("raw_files.json"),
    }
