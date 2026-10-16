"""Endpoint URL 来源共用的落记录逻辑。

为什么有这么一个文件
--------------------
dirsearch / katana / gau / ffuf 的**输入形状**完全不同，但它们共用同一套 Endpoint
v2 身份生成与证据写入：

    endpoint 实体 + 主机 + 服务 + `served_by` 关系 + 证据观测

身份为 `(scheme, normalized host, effective port, exact path, method)`。具体路径和
方法不同就属于不同 endpoint；path 与 query/fragment 的原始值仍按脱敏和上限策略
写入来源观察。`path_template` 仅是兼容/分组提示，不折叠身份。这里放一份共用逻辑，
各解析器只负责将自己的格式读成 URL（或路径 + base）。

发现机制不会把它当成解析器
--------------------------
`ingest.load_parsers()` 跳过 `_` 开头的模块，`check.py` 的 PARSE-NORM 只对
"定义了 `class X(Parser)` 的模块"生效 —— 这里两条都不沾。但它仍然在
`magpie/core/parsers/**` 里，PARSE-RO（不得 import 写入口）**照常管它**。

两条口径，写在这里免得四个解析器各说各话
----------------------------------------

**一、路径严格区分。** `/user/123`、`/user/456` 与 `/User/123` 是不同 Endpoint
身份；query 与 fragment 不进身份，但单条请求样本保留各自的脱敏摘要与来源。

**二、未知方法单独建身份。** 未观察到 method 时 key 的方法分量为 `unknown`，不
补造 GET 或 `method` observation；界面可明确显示“方法未知”。
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
             identity_method: str | None = None,
             extra: Iterable[tuple[str, Any]] = ()) -> Iterator[Record]:
        """一条 URL（或 `base` 下的相对路径）→ 记录。

        坏输入不抛：`ctx.on_bad_line` 记一句人话后**只跳过这一行**（一行坏掉
        不等于整份文件坏掉 —— 这是 §5.3 的解析器纪律）。
        """
        try:
            identity = normalize.endpoint_identity(
                raw, base=base, method=method if identity_method is None else identity_method)
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
            yield RawEntity(kind="endpoint", key=key, display=normalize.endpoint_display(identity),
                            attrs={"path_template": str(identity["template"])})

        if self._first(("relation", "endpoint", key, svc)):
            yield RawRelation(src=("endpoint", key), dst=("service", svc),
                              rel="served_by")

        # --- 证据观测 ---
        # `path` 是**原样**的路径 + 查询串 + 片段（identity["raw_path"]）；
        # `path_template` 只是兼容/分组提示，不参与 v2 身份，也不把端点合并。
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
