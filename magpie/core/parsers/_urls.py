"""四个"路径列表"来源共用的落记录逻辑（3.3）。

为什么有这么一个文件
--------------------
dirsearch / katana / gau / ffuf 的**输入形状**完全不同（两种文本、两种 JSON，
见各自的文件头），但它们**产出的东西**是同一个东西：

    endpoint 实体 + 主机 + 服务 + `served_by` 关系 + 证据观测

把这段逻辑抄四遍的代价不是四倍的代码，而是**四份会各自漂移的身份落点**：
其中一份忘了保留原始路径、或者把 scheme 写进了观测之外的地方，接口镜头就会
对四个来源给出四种不同的答案，而不会有任何测试发现。所以这里放一份，
四个解析器只负责"把自己的格式读成一条 URL（或路径 + base）"。

发现机制不会把它当成解析器
--------------------------
`ingest.load_parsers()` 跳过 `_` 开头的模块，`check.py` 的 PARSE-NORM 只对
"定义了 `class X(Parser)` 的模块"生效 —— 这里两条都不沾。但它仍然在
`magpie/core/parsers/**` 里，PARSE-RO（不得 import 写入口）**照常管它**。

两条口径，写在这里免得四个解析器各说各话
----------------------------------------

**一、原始路径必须逐条留证据。** 折叠是**身份**的事，不是事实的事：

    /user/123 ┐
    /user/456 ├─→ 一个 `endpoint` 实体（`app.example.com/user/{id}`）
    /user/789 ┘   三条 `path` 观测（`/user/123` / `/user/456` / `/user/789`）

把这三条也折成一条观测，就等于把"这个 endpoint 到底见过哪些真实路径"永久删掉
—— 而那正是 3.5 的镜头页与 3.6 的体检要拿来回答问题的证据。所以这里去重的是
**实体与关系**（同一个 key 只声明一次），观测只按 (endpoint, 字段, 值) 去重。

⚠️ **已知代价：这三条 `path` 会被数据层记成一条 `value_conflict`。**
`v_field_conflicts` 的口径是"同实体同字段多值即冲突"，它没有"多值字段"这个
概念 —— 所以每一个被折叠过的 endpoint 都会开一条 Inbox 项，而且那条项的主体是
project 而不是 job，`undo` 撤不掉它。这**不是**这里引入的形状：httpx 对多值字段
`cname` 早就是同一个行为（两条 CNAME → 一条冲突）。修它要动 `v_field_conflicts`
或 observations 的建模，属 001 的地界（红线 R4 不改），所以 3.3 的选择是**如实
按验收标准落三条观测**，把缺口原样报给发起人（测试
`test_endpoint_multi_path_landing_in_the_inbox_is_a_pre_existing_model_gap`
把它钉住了）。

**二、方法缺失时**不写 `method` 观测**。** 计划书 §4.3 写"无方法记 `UNKNOWN`"，
指的是接口列表里那一列**不空着**；而"这一条来自一个根本不产出方法的工具"是
**缺观测**就能表达的事实。补一条 `method=UNKNOWN` 是零信息噪声，会实打实地抬高
观测数与存储 —— 与 httpx 对 `failed=False` 的处理是同一条口径（那里的注释写透了）。
接口页面把"没有 method 观测"渲染成 `UNKNOWN` 就够，而那个显示默认值归 GUI。
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from .. import normalize
from .base import ParseContext, RawEntity, RawObservation, RawRelation, Record
from .httpx import service_key

__all__ = ["MAX_VALUE_CHARS", "EndpointSink", "sample_lines"]

#: 观测值的上限（与 `ParseContext.on_bad_line` 的 `raw[:400]` 同一口径）。
MAX_VALUE_CHARS = 400

#: 文本格式的嗅探/解析各看前多少行（与 domainlist 一致：样本不必读全）。
SAMPLE_LINES = 200


def sample_lines(sample: str, *, limit: int = SAMPLE_LINES) -> list[str]:
    """样本 → 非空行列表。NUL 先剥（ingest 会剥，但嗅探可能拿到未剥的样本）。"""
    lines = [ln.strip() for ln in sample.replace("\x00", "").splitlines()]
    return [ln for ln in lines if ln][:limit]


class EndpointSink:
    """把一条 URL / 路径变成记录；实体与关系**在同一份文件内**去重。

    实例是"一份文件一行生命周期"的（与解析器本身的无状态纪律一致）：
    `parse()` 进来时新建一个，跑完就扔 —— 没有跨文件的记忆，因为同一个 key
    在两个文件里各声明一次是**对的**（管线按 (kind, key) 认实体，重复的那次
    记成 `skipped`，一条贡献都不多写）。
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, ...]] = set()

    def _first(self, marker: tuple[str, ...]) -> bool:
        """这个 key 在本文件里第一次出现 → True。"""
        if marker in self._seen:
            return False
        self._seen.add(marker)
        return True

    def emit(self, raw: str, ctx: ParseContext, lineno: int, *,
             base: str | None = None, method: str = "",
             extra: Iterable[tuple[str, Any]] = ()) -> Iterator[Record]:
        """一条 URL（或 `base` 下的相对路径）→ 记录。

        坏输入不抛：`ctx.on_bad_line` 记一句人话后**只跳过这一行**（一行坏掉
        不等于整份文件坏掉 —— 这是 §5.3 的解析器纪律）。
        """
        try:
            identity = normalize.endpoint_identity(raw, base=base)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"不是可识别的 endpoint：{exc}",
                            str(raw)[:MAX_VALUE_CHARS])
            return
        key = str(identity["key"])
        host = str(identity["host"])
        host_kind = str(identity["host_kind"])

        # --- 主机与服务：与 httpx / nmap / nuclei 落**同一个**实体 ---
        # 顺序是"先实体后关系"（domainlist 的注释写透了为什么）。
        if self._first(("entity", host_kind, host)):
            yield RawEntity(kind=host_kind, key=host, display=host)

        svc = service_key(host, identity["port"], str(identity["scheme"]))
        if self._first(("entity", "service", svc)):
            yield RawEntity(kind="service", key=svc, display=svc,
                            subtitle=str(identity["scheme"]))

        if self._first(("entity", "endpoint", key)):
            yield RawEntity(kind="endpoint", key=key, display=key,
                            attrs={"path_template": str(identity["template"])})

        if self._first(("relation", "endpoint", key, svc)):
            yield RawRelation(src=("endpoint", key), dst=("service", svc),
                              rel="served_by")

        # --- 证据观测 ---
        # `path` 是**原样**的路径 + 查询串 + 片段（identity["raw_path"]）；
        # `path_template` 是折叠结果。两条都留：前者回答"见过哪些真实路径"，
        # 后者回答"这条接口被折成了什么"（3.5 镜头按它分组）。
        for field, value in (("path", identity["raw_path"]),
                             ("path_template", identity["template"]),
                             ("scheme", identity["scheme"]),
                             ("port", identity["port"]),
                             ("method", method),
                             *extra):
            if value is None or value == "":
                continue
            text = _bounded(value)
            if not self._first(("obs", key, field, str(text))):
                continue
            yield RawObservation(entity_ref=("endpoint", key), type=field,
                                 value=text)


def _bounded(value: Any) -> Any:
    """字符串截断到 `MAX_VALUE_CHARS`；数字原样保留（端口是 int 就该是 int）。"""
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS]
    return value
