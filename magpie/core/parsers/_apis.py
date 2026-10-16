"""四个"接口描述"来源共用的落记录逻辑（3.4）。

为什么有这么一个文件
--------------------
openapi / postman / har / curl 的**输入形状**毫无共同之处（一份规范文档、
一份集合、一段抓包、一行命令，见各自的文件头），但它们**产出的东西**是同一个：

    apischema 实体 + endpoint 实体 + 主机 + 服务 + `declared_in` / `served_by`
    关系 + 路径 / 方法 / 状态码 / 参数树的证据观测

把这四遍抄下去的代价与 3.3 那次是同一件事（`_urls.py` 的文件头写透了）：
**四份会各自漂移的落点**。其中一份忘了把参数挂到 endpoint 上、或者把标题写进了
身份，接口镜头就会对四个来源给出四种答案，而不会有任何测试发现。

所以这里放一份，四个解析器只负责"把自己的格式读成一次操作"。

与 `_urls.py` 的关系：**不是替代，是叠加**
------------------------------------------
`EndpointSink` 已经做对了 3.3 的那一半（主机 / 服务 / endpoint 实体 + `served_by`
+ path / path_template / scheme / port / method 观测），v2 按完整身份五元组统一
契约与流量记录；只有 scheme、host、有效端口、精确 path、method 全部相同才落到
同一个 endpoint。这里复用 `normalize.endpoint_identity`，避免多份身份规则漂移。

`_` 开头的模块，发现机制不会把它当成解析器（同 `_urls.py`），但
PARSE-RO（不得 import 写入口）照常管它。

参数树的落地形状（**本步先定死的那个裁定**）
--------------------------------------------
计划书 3.4 要求"参数树可展开"，而"展开"有两种落法，本步**选定第二种**：

  (一) 整棵树落成**一个**观测，值是一段结构化 JSON；
  (二) **一个参数一条观测**，字段名 `param.<参数名>`，值是一个小字典。

选 (二)，理由四条，每一条都指向一个具体的下游：

1. **3.5 的查询必须写得出来**。`core/query.py` 的具名查询最终是 SQL ——
   "哪些接口要 `id` 参数"在 (二) 下是 `semantic_field = 'param.id'` 的一行
   `WHERE`；(一) 下只能把每一行的 JSON 读出来在 Python 里解析，于是
   "GUI 与 CLI 共用同一实现"退化成两边各写一遍 JSON 解析。
2. **撤销的粒度是事实的粒度**。参数增删是**逐条事实**。(一) 下改一个参数会让
   整条观测变成"另一个值"，`undo` 撤掉的是**全部**参数；(二) 下每条参数各自
   一条贡献，undo / redo 精确互逆（G2 判据 ② 的口径）。
3. **冲突要能指到具体哪个参数**。(一) 下一个 blob 就是一条冲突，报告里看不出
   是哪个参数变了；(二) 下冲突落在 `param.id` 上，一眼可见。
4. **值的上限**（`MAX_VALUE_CHARS` = 400）。一份真实规范里 requestBody 的
   schema 展开轻松超过 400 字符 —— (一) 要么放宽这条纪律，要么截断，而
   **截断 JSON 等于不可解析**（比不落还糟）。(二) 下每条值是一个小字典。

字段名与值：**位置进名字，细节进属性字段**
-------------------------------------------
一个参数落成**最多三条**观测，粒度是"一条事实一条"：

    param.query.limit            值 = "query"       ← "这个接口有这个参数"
    param.query.limit.type       值 = "integer"     ← 只有知道类型的来源才写
    param.query.limit.required   值 = true          ← 只有声明了必填的来源才写

**位置进字段名**（`param.<位置>.<名字>`），因为**同名不同位是两个参数**
（`?id=` 与 `/{id}` 各自可以有），而 `v_field_conflicts` 的判据是"同实体同字段
多值"—— 少了位置那一层，一个 `id` 写在两处就会变成一个假冲突。

⚠️  **值里只放位置，不放 MIME、不放类型** —— 这是被一次真实的四源导入逼出来的：
第一版把 `{"in": "body:json", "required": true, "type": "string"}` 整个字典当值，
于是**每个被契约与流量同时看到的参数都会开一条 Inbox**：

    param.query.limit  ← 契约说 {"in":"query","type":"integer"}，流量说 {"in":"query"}
    param.body.name    ← JSON / 表单 / 契约三种写法，三份值

一次四源导入开 11 条冲突，其中 **6 条是这么来的**。它们一条都不在说"数据有矛盾"，
说的是"这个模型没有'同一个字段、精度不同的两端'这个概念"—— 而用假冲突去表达它，
代价是 Inbox 彻底失去信噪比（3.6 的体检正是靠它）。所以：

* **MIME 不进参数值**。`application/json` 与 `x-www-form-urlencoded` 是**这一次
  请求**的属性，不是参数的属性 —— 同一个参数换个编码就变成两个值，是拿请求的
  维度去污染参数的维度。HAR / Postman / curl 一律只写 `body`。
  （"这个接口收什么编码"是真实的问题，但它属于请求而不是参数；本步只取接口形状，
  不为它另立观测。）
* **类型与必填只由知道的来源写、且写在自己的字段上**。契约知道类型，流量不知道 ——
  让流量"缺一个键"去和契约的完整值对撞，是把**信息量的差异**误报成**值的冲突**。
  分成 `<参数>.type` / `<参数>.required` 之后，有则有、无则无，两边都不冲突。

代价写在明处：一个参数最多三行、字段名最多四段（`param.query.limit.required`）。

字段名是**动态**的（`param.<位置>.<名字>`），这是选 (二) 必须一起接受的代价，
写在这里免得下游各自发现：

* **3.6 的体检**判"未知字段"时，必须把 `param.` 前缀**排除**掉 —— 它和
  `entity_field` / `observation_field` 是同一类"结构性字段"，不是"这个来源
  冒出来的新字段"。若照单全收，一份 50 个接口的规范会开出几百条假告警；
* "这个接口有几个参数"这类聚合要写成 `COUNT(*) WHERE semantic_field LIKE
  'param.%'`，而不是 `json_length(...)`。

反向的代价（为什么 (一) 不划算）写在上面第一条里：查询层是这套数据的**主要
消费者**，而它读不了 JSON blob。

参数挂在哪
----------
挂在 **endpoint** 上，**不是** apischema 上。apischema 的身份是起源，"这个起源
的契约里有 `id` 这个参数"答不出**哪个接口**要它 —— 把 `/user/{id}` 的 `id` 与
`/order/{id}` 的 `id` 并到一个实体上，参数树当场退化成一张关键词表。

声明型 vs 观测型：两个前缀，两条线
-----------------------------------
`declared_method` / `declared_status`（**一组**，排序过的列表）说的是"契约声明
它支持 / 可能返回哪些"；`method` / `status`（单值，3.3 的 katana / dirsearch /
ffuf 与 3.4 的 har / curl 写的）说的是"这一条请求用的 / 返回的"。

分开有一个**死钉得住**的理由，不是一个审美偏好：只要一个接口声明了 `GET` +
`POST`，共用一个字段名就会落出两条不同的值，而 `v_field_conflicts` 的判据是
"同实体同字段多值即冲突"—— 于是**每一个 REST 资源**都会开一条 Inbox 项。那不是
在报冲突，那是在用假冲突表达"这个模型没有多值字段"这件事，而 Inbox 会因此
彻底失去信噪比。

所以声明型在 `ApiSink` 里**按 endpoint 累积、整份文件结束时落一条**（一组方法
就是**一个**事实），并且带 `declared_` 前缀。观测型仍然单值 —— 那里的多值问题
是 3.3 已经报过的既有缺口（同一个接口被观测到 200 与 404 各一次），本步
**再报一次**、不修它：修它要动 001 的 `v_field_conflicts`（红线 R4 不改），而
"把两个状态码拼成一个字符串"正是 (一) vs (二) 那个裁定否掉的东西。

凭据：一个字都不落
------------------
`curl` 的 `-u user:pass`、`-H "Authorization: Bearer …"`、HAR 的 `Cookie` 头都是
**明文凭据**，G4 要求它们永不落库。本步的口径写在 `curl.py` 的文件头（三条来源
一致的线：header **不进参数树**），并有用例断言那个口令在整张库里**一次都不出现**。
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from .. import normalize
from ._urls import MAX_VALUE_CHARS, EndpointSink
from .base import ParseContext, RawEntity, RawObservation, RawRelation, Record

#: 声明型观测的字段名（`declare` 的取值域）。写成常量而不是散在三个解析器里：
#: "哪些来源写声明型"这件事必须是一处能查的清单，否则第 4.5 步加流量来源时
#: 没人说得清 har 到底写了哪一半。
DECLARED_FIELDS: tuple[str, ...] = ("declared_method", "declared_status")

__all__ = ["DECLARED_FIELDS", "MAX_PARAM_DEPTH", "ApiSink", "param_field"]


def param_field(location: str, name: str) -> str:
    """参数 → 观测字段名：`param.<位置>.<名字>`。

    位置是**粗档**（`query` / `path` / `header` / `cookie` / `body`）：参数树的
    第二层问的是"这个参数填在哪"，不是一张 MIME 类型表。位置那一层的理由与
    "`in` 只取第一段"的理由都见文件头。
    """
    bucket = location.split(":", 1)[0] or "unknown"
    return f"param.{bucket}.{name}"


#: 参数树的展开深度上限。超过这个深度的节点只留一条 `type=object` / `type=array`
#: 的叶子，不再往下展开 —— 一份递归 schema（`components/schemas/Node` 指向自己）
#: 会让"展开"变成一次无限递归，而那是**拒绝服务**，不是功能。三层足够覆盖
#: `body.user.address.city` 这种真实形状。
MAX_PARAM_DEPTH = 3


class ApiSink:
    """把一次"操作"（一个 URL + 方法 + 参数 + 状态码）变成记录。

    实例是"一份文件一次生命周期"的（与解析器本身的无状态纪律一致，同
    `EndpointSink`）：`parse()` 进来时新建一个，跑完就扔。同一个 key 在**两个
    文件**里各声明一次是**对的**（管线按 (kind, key) 认实体，重复的那次记成
    `skipped`，一条贡献都不多写）。
    """

    def __init__(self) -> None:
        self._endpoints = EndpointSink()
        self._seen: set[tuple[str, ...]] = set()
        #: 声明型的累积（`endpoint_key` → 字段 → 值的集合）。**按文件累积、
        #: 结尾一次落一条**：一组方法 / 一组状态码是**一个**事实，逐条落会在
        #: 同一个 endpoint 上落出多条同名字段观测，而那就是一条假冲突
        #: （文件头"声明型 vs 观测型"那一节）。
        self._declared: dict[str, dict[str, set[str]]] = {}
        #: 累积顺序 = 首次出现的顺序（值的集合本身排序，这个只保证**记录顺序**
        #: 可复现 —— 顺序也是夹具的一部分）。
        self._declared_order: list[str] = []

    def _first(self, marker: tuple[str, ...]) -> bool:
        """这个 key 在本文件里第一次出现 → True。"""
        if marker in self._seen:
            return False
        self._seen.add(marker)
        return True

    # ------------------------------------------------------------------
    # apischema：实体 + 文档级的观测
    # ------------------------------------------------------------------

    def apischema(self, origin: str, ctx: ParseContext, lineno: int) -> Iterator[Record]:
        """一个起源 → `apischema` 实体。四个来源都会走到这里，同一份文件里只声明一次。

        起源算不出来时**不抛**：`ctx.on_bad_line` 记一句人话。调用方据此决定
        这一次操作要不要继续（`emit_operation` 里也是同一条纪律）。
        """
        try:
            identity = normalize.apischema_identity(origin)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"算不出接口契约的起源：{exc}",
                            str(origin)[:MAX_VALUE_CHARS])
            return
        key = str(identity["key"])
        if self._first(("entity", "apischema", key)):
            yield RawEntity(kind="apischema", key=key, display=key)

    def apischema_fact(self, origin: str, field: str, value: Any,
                       ctx: ParseContext) -> Iterator[Record]:
        """文档级的观测（`api_title` / `api_version`）。

        **刻意不做成 `emit_operation` 的参数**：一份规范里只有一个标题，却有
        几十个接口；把它挂在每次操作上会变成"抄 N 遍同一句话"。这里由调用方
        在**文档级**调一次。
        """
        if value is None or value == "":
            return
        try:
            key = normalize.apischema_key(origin)
        except normalize.NormalizeError:
            return                      # 起源报错已经在 `apischema()` 里说过一次了
        text = str(value)[:MAX_VALUE_CHARS]
        if not self._first(("fact", key, field, text)):
            return
        yield RawObservation(entity_ref=("apischema", key), type=field, value=text)

    # ------------------------------------------------------------------
    # 一次操作
    # ------------------------------------------------------------------

    def _declare(self, endpoint_key: str, field: str, values: Iterable[str]) -> None:
        bucket = self._declared.setdefault(endpoint_key, {})
        if endpoint_key not in self._declared_order:
            self._declared_order.append(endpoint_key)
        bucket.setdefault(field, set()).update(values)

    def declared(self) -> Iterator[Record]:
        """整份文件读完后，逐个 endpoint 落**声明型**观测。

        值是**排序过的列表**：同一组方法换个书写顺序必须落成同一个值，否则
        `(实体, 字段, 值)` 三元组里的"值"会把同一件事记成两个值 —— 那是一条
        假冲突，而且它会在**重导同一份文件**时凭空出现。
        """
        for key in self._declared_order:
            bucket = self._declared[key]
            for field in DECLARED_FIELDS:
                values = sorted(bucket.get(field) or ())
                if not values:
                    continue
                yield RawObservation(entity_ref=("endpoint", key), type=field,
                                     value=values)

    def emit_operation(self, raw: str, ctx: ParseContext, lineno: int, *,
                       origin: str | None = None, method: str = "",
                       status: Any = None, params: Iterable[tuple[str, dict]] = (),
                       declared_method: str = "",
                       declared_status: Iterable[str] = (),
                       extra: Iterable[tuple[str, Any]] = ()) -> Iterator[Record]:
        """一条 URL → 记录。坏输入不抛，记一句人话后**只跳过这一条**。

        `origin` 缺省时用 `raw` 自己（HAR / curl 这两种"每一跳都自带完整 URL"
        的来源就是这样）—— 四个来源都算得出起源，这正是身份规则能成立的前提。

        `method` / `status` 是**观测型**（单值，直接落观测）；`declared_method`
        / `declared_status` 是**声明型**，只累积、不在这里落 —— 见 `declared()`。
        """
        try:
            identity = normalize.endpoint_identity(raw, method=method or declared_method)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"不是可识别的 endpoint：{exc}",
                            str(raw)[:MAX_VALUE_CHARS])
            return
        key = str(identity["key"])
        api_origin = origin if origin is not None else raw

        # --- 契约实体与 `declared_in` 关系 ---
        # 顺序是"先实体后关系"（`_urls.py` 的注释写透了为什么）。
        try:
            api_key = normalize.apischema_key(api_origin)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"算不出接口契约的起源：{exc}",
                            str(api_origin)[:MAX_VALUE_CHARS])
            return
        if self._first(("entity", "apischema", api_key)):
            yield RawEntity(kind="apischema", key=api_key, display=api_key)

        # --- 接口这一侧：整段交给 `EndpointSink`（3.3 的那一半，一字不差） ---
        yield from self._endpoints.emit(raw, ctx, lineno, method=method,
                                        identity_method=method or declared_method,
                                        extra=(("status", status), *extra))

        if self._first(("relation", "endpoint", key, api_key)):
            yield RawRelation(src=("endpoint", key), dst=("apischema", api_key),
                              rel="declared_in")

        # --- 声明型：只累积（整份文件读完由 `declared()` 统一落） ---
        if declared_method:
            self._declare(key, "declared_method", [declared_method])
        if declared_status:
            self._declare(key, "declared_status", declared_status)

        # --- 参数树 ---
        for name, detail in params:
            yield from self._param(key, name, detail, ctx, lineno)

    def _param(self, endpoint_key: str, name: str, detail: dict,
               ctx: ParseContext, lineno: int) -> Iterator[Record]:
        """一个参数 → **最多三条**观测（存在 / 类型 / 必填），见文件头的裁定。

        `detail` 的形状：`{"in": <位置>, <属性>: <值>, …}`。**位置进字段名**，
        其余键各落成一条 `<参数>.<属性>` 观测 —— 只有知道的来源才写，
        于是"契约知道类型、流量不知道"不再表现为一条假冲突。

        参数名认不出（模板变量、非法字符）时**记一条坏行**：把它清洗成一个
        合法名字会让一条模板变量伪装成一个真实参数（"宁可明确地拒绝"）。
        """
        try:
            canonical = normalize.param_name(name)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"不是可识别的参数名：{exc}",
                            str(name)[:MAX_VALUE_CHARS])
            return
        location = str(detail.get("in") or "unknown").split(":", 1)[0] or "unknown"
        base = param_field(location, canonical)
        yield from self._observation(endpoint_key, base, location)
        for attribute in sorted(detail):
            if attribute == "in":
                continue
            value = detail[attribute]
            if value in (None, "", [], {}):
                continue
            yield from self._observation(endpoint_key, f"{base}.{attribute}", value)

    def _observation(self, endpoint_key: str, field: str,
                     value: Any) -> Iterator[Record]:
        """一条去重过的观测（同一个 key 在本文件里只声明一次）。"""
        marker = ("obs", endpoint_key, field, repr(value))
        if not self._first(marker):
            return
        yield RawObservation(entity_ref=("endpoint", endpoint_key), type=field,
                             value=value)
