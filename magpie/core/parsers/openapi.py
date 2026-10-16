"""`openapi` —— OpenAPI 3 / Swagger 2 的接口描述（计划书 §4 的「接口来源」行）。

这是 3.4 四个来源里**唯一必做**的一个，也是唯一有两个身体的那个：同一份规范
既可以是 JSON，也可以是 YAML。两者的**产物完全一样**，所以它们是同一个解析器
（`tool = "openapi"`）而不是两个 —— 拆成两个会让"同一份契约换个序列化格式就
变成另一个来源"，而它连字节含义都没变。

YAML 走 extras，而且报错必须是**人话**（本步的头号陷阱）
---------------------------------------------------------
`pyproject.toml` 里 `api-yaml = ["pyyaml"]` 已经就位，而红线 R6 要求
`magpie/core/**` 的**顶层** import ⊆ 标准库。两条合起来只有一种写法：

    YAML 那一个 `import yaml` 写在 `_load_yaml()` 的**函数体**里。

于是 YAML 输入在 extras 未安装时的行为被死钉在三句话上：

* **抛 `MissingExtrasError`**，消息里说清缺哪个 extras、跑哪条命令装；
* **绝不是裸 `ImportError`** —— 那把人引去看 `sys.path`，而这里该做的是
  `pip install`。计划书把这条单列为交付标准，所以它是一条具名异常；
* **绝不安静降级成"不认识这个格式"** —— 那会把一个**环境问题**伪装成
  **格式问题**，使用者会去怀疑自己的文件写错了，而文件没有问题。

⚠️  反向也要验：extras **已装**时必须真的解析成功。两条路径都要有用例，否则
"可读错误"与"其实压根没接上"在测试里长得一模一样（`test_openapi_yaml_*`）。

本解析器**不 import** 任何第三方模块到模块级，也不在 JSON 路径上碰 yaml。

产出
----

| 记录 | 内容 |
|---|---|
| `apischema` 实体 | key = 起源（`servers[].url` / Swagger 2 的 `host`+`basePath`） |
| `endpoint` 实体 + `served_by` | 契约**声明**的每一条路径（与流量观测到的是同一个身份） |
| 关系 | `endpoint --declared_in--> apischema` |
| 声明型观测 | `declared_method`（一组，排序）/ `declared_status`（一组，排序） |
| 文档级观测 | `api_title` / `api_version`（**只在文档级落一次**） |
| 参数树 | `param.<名字>` 一条一个参数（形状的裁定见 `_apis.py`） |

`declared_method` / `declared_status` 为什么不叫 `method` / `status`
---------------------------------------------------------------------
3.3 的 katana / dirsearch / ffuf 写的是 `method` / `status`，说的是
"**这一条请求用的 / 返回的**"。契约说的是另一件事："**它支持哪些 / 可能返回
哪些**"。两件事共用一个字段名会有两个后果：

1. 只要一个接口声明了 `GET` + `POST`，就会落两条 `method` 观测，而
   `v_field_conflicts` 的判据是"同实体同字段多值即冲突" —— 于是**每一个
   REST 资源**都开一条 Inbox 项。那不是在报冲突，那是在报"这个模型没有多值
   字段"这件事，而用假冲突去表达它，会让 Inbox 彻底失去信噪比；
2. "契约说支持 POST、实际一次都没见过"这个**真正值得看**的信号，会被淹没在
   "GET 和 POST 都是 method"的噪声里。

所以声明型的一律带 `declared_` 前缀、值是一个**排序过的列表**（一组码 / 一组
方法就是**一个**事实），观测型的一律是单值。查询写法：

    -- 契约声明过 POST 的接口
    SELECT e.entity_key FROM observations o ... WHERE o.semantic_field='declared_method'
      AND EXISTS (SELECT 1 FROM json_each(o.value) WHERE json_each.value='POST')

这条线是**一句话能说清**的：`declared_*` = 契约声明的一组，其余 = 观测到的一个。

`$ref` 解析：只解**本地**引用，且带环检测
-----------------------------------------
真实规范的 requestBody / response schema 几乎全是 `$ref`。不解析的话参数树
对绝大多数真实文档是空的 —— 那这个功能就是空的。所以这里解
`#/components/schemas/X`（OA3）与 `#/definitions/X`（Swagger 2），**只解本地**：

* 指向外部文件的（`./common.yaml#/...`）**只记下引用名**，不解析 —— 解它要读
  另一个文件，而解析器只被交给一个文件（读了还会踩 PARSE-RO 之外的另一条线：
  解析器只看它拿到的东西）；
* `seen` 集合挡环（`Node.children[] -> Node` 是真实存在的形状）。没有它，
  一份自引用的 schema 会让"展开参数树"变成一次无限递归 —— 那是拒绝服务，
  不是功能。

HAR 的响应体、这里的响应 **schema**：为什么本步不取
----------------------------------------------------
计划书写死了"本步只取接口形状（路径、方法、参数、状态码）"。所以 `responses`
只取**状态码**，`content` / `schema` 一个字都不读 —— 不是偷懒，是范围。响应体
与响应结构是阶段 4 的流量题，提前把它们拖进来会让本步的体积失控，而它们真正
的形状要等到那时才看得清。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .. import normalize
from . import _json
from ._apis import MAX_PARAM_DEPTH, ApiSink
from ._urls import MAX_VALUE_CHARS
from .base import MissingExtrasError, OfflineContext, Parser, ParseContext, Record, \
    extras_install_hint

__all__ = ["KNOWN_FIELDS", "METHOD_NAMES", "OpenapiParser", "parse_text"]

#: 本解析器认识的**顶层**字段。出现别的 key 就报未知字段（§5.6 的漂移信号）。
KNOWN_FIELDS: frozenset[str] = frozenset({
    # OpenAPI 3
    "openapi", "info", "servers", "paths", "components", "security", "tags",
    "externalDocs", "webhooks", "jsonSchemaDialect", "summary", "description",
    # Swagger 2
    "swagger", "host", "basePath", "schemes", "consumes", "produces",
    "definitions", "parameters", "responses", "securityDefinitions",
    # 工具链常见的扩展键（`x-` 开头的按 §5.6 的惯例不报）
    "x-origin", "x-generated",
})

#: 路径项里算"一个操作"的键。**大写形式**，与 `normalize.http_method` 的输出对齐。
METHOD_NAMES: tuple[str, ...] = ("GET", "PUT", "POST", "DELETE", "OPTIONS",
                                 "HEAD", "PATCH", "TRACE")

#: YAML 那一个 extras 的名字（`pyproject.toml` 的 `api-yaml`）与它提供的模块名。
_YAML_EXTRAS = "api-yaml"
_YAML_MODULE = "yaml"


def _load_yaml(text: str) -> Any:
    """YAML 文本 → Python 对象。**extras 缺失时抛可读错误**（文件头讲透了）。

    `import` 在函数体里，不在模块顶层 —— 那是 R6 与 extras 两条约束**唯一**
    的交点（`check.py` 的 R6 只看 `tree.body`）。
    """
    try:
        import yaml                                    # noqa: PLC0415 —— 见上
    except ImportError as exc:                         # pragma: no cover - 分开测
        raise MissingExtrasError(
            f"这份文件是 YAML 格式的 OpenAPI 文档，而 YAML 解析器不在标准库里。"
            f"缺少 extras `{_YAML_EXTRAS}`（pyyaml）—— 装上它："
            f"`{extras_install_hint(_YAML_EXTRAS)}`。"
            f"（core 本身零第三方依赖，所以这个 import 只在真的解析 YAML 时发生。）"
        ) from exc
    try:
        return yaml.safe_load(text)
    except Exception as exc:                           # noqa: BLE001 —— yaml 的异常谱系很宽
        raise ValueError(f"{_YAML_MODULE} 解析失败：{exc}") from exc


class OpenapiParser(Parser):
    tool = "openapi"
    format_version = "2"
    known_fields = KNOWN_FIELDS
    #: 最高档：契约是**签名**，不是猜测 —— 它由接口的提供方自己写下来。
    max_confidence = 1.0
    #: 与另外三个接口描述来源互斥。判据刻意**互不相交**（顶层键），
    #: `test_sniffing_separates_the_formats_cleanly` 守着这一点。
    exclusive_group = "api-schema"

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """靠**顶层键**认：JSON 看 `openapi` / `swagger`，YAML 看首个有效行。

        ⚠️  两个刻意的决定：

        * **不看 yaml 能不能 import**。YAML 文件就是 YAML 文件 —— extras 没装
          是**环境**问题，不是"这不是我的格式"。在这里因为缺依赖而返回 0，
          正好制造出文件头说的那种伪装（环境问题扮成格式问题），而且它会让
          文件掉进人工裁决 —— 一个本该"报一条安装提示"的输入，变成一条待办；
        * **不看文件名**。`swagger.json` / `openapi.yaml` 对仲裁没有证明力
          （`gau` 的文件头写过同一条）。

        JSON 那边用截断容忍的顶层键扫描（`_json.top_level_keys`）而不是
        `json.loads`：样本**必然**被 64 KiB 截断，`json.loads` 在真实文档上
        永远失败（`_json.py` 的文件头把这条讲透了）。
        """
        del filename
        text = sample.replace("\x00", "").lstrip()
        if not text:
            return 0.0
        if text.startswith("{") or text.startswith("["):
            keys = _json.top_level_keys(text)
            return 0.95 if _json.find_key(keys, "openapi", "swagger") else 0.0
        for line in text.splitlines()[:20]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped == "---":
                continue
            if stripped.startswith("openapi:") or stripped.startswith("swagger:"):
                return 0.95
            return 0.0                  # 第一个有内容的行不是顶层键 → 不是它
        return 0.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        text = _json.collect_json(lines, ctx, "这份 OpenAPI 文档")
        if text is None:
            return
        if not text.strip():
            return                      # 空文件不是坏行（与 ffuf 同口径）

        head = text.lstrip()
        if head.startswith("{") or head.startswith("["):
            doc = _json.load_json(text, ctx, "OpenAPI 文档")
        else:
            try:
                doc = _load_yaml(text)
            except MissingExtrasError:
                raise                   # 环境问题 → 一路抛给 ingest 落成可读报告
            except ValueError as exc:
                ctx.on_bad_line(1, str(exc), text[:MAX_VALUE_CHARS])
                return
        if doc is None:
            return
        if not isinstance(doc, dict):
            ctx.on_bad_line(1, f"顶层必须是一个对象（收到 {type(doc).__name__}）"
                               f"—— OpenAPI / Swagger 文档都是对象", "")
            return

        for key in doc:
            if str(key) not in self.known_fields and not str(key).startswith("x-"):
                ctx.on_unknown_field(str(key))

        version = _spec_family(doc)
        if version is None:
            ctx.on_bad_line(
                1, "顶层没有 `openapi` 也没有 `swagger` 键 —— 这不是一份 OpenAPI /"
                   " Swagger 文档（3.0 起是 `openapi`，2.0 是 `swagger`）", "")
            return

        origins = _origins(doc, ctx)
        if not origins:
            return

        sink = ApiSink()
        title, api_version = _doc_meta(doc)
        for origin in origins:
            yield from sink.apischema(origin, ctx, 1)
            yield from sink.apischema_fact(origin, "api_title", title, ctx)
            yield from sink.apischema_fact(origin, "api_version", api_version, ctx)

        paths = doc.get("paths")
        if paths is None:
            ctx.on_bad_line(1, "文档里没有 `paths` —— 一份接口契约至少要声明一条路径", "")
            return
        if not isinstance(paths, dict):
            ctx.on_bad_line(1, f"`paths` 必须是对象（收到 {type(paths).__name__}）", "")
            return

        for raw_path, item in paths.items():
            if ctx.cancelled():
                return
            if not isinstance(item, dict):
                ctx.on_bad_line(1, f"paths[{raw_path!r}] 必须是对象（收到 "
                                   f"{type(item).__name__}）", "")
                continue
            for record in self._path_item(doc, origins, str(raw_path), item, sink, ctx):
                yield record

        # 声明型的观测**整份文件读完后一次落**：同一个 endpoint 上的 `GET` 与
        # `POST` 是**一个**事实（"它支持这两个方法"），逐条落会变成两条同名字段
        # 观测，而 `v_field_conflicts` 会把它们记成一条冲突（`_apis.py` 的文件头）。
        yield from sink.declared()

    # ------------------------------------------------------------------
    # 一条路径
    # ------------------------------------------------------------------

    def _path_item(self, doc: dict, origins: list[str], raw_path: str, item: dict,
                   sink: ApiSink, ctx: ParseContext) -> Iterator[Record]:
        if not raw_path.startswith("/"):
            ctx.on_bad_line(1, f"路径必须以 / 开头：{raw_path!r}", raw_path[:MAX_VALUE_CHARS])
            return
        shared = _param_list(item.get("parameters"))
        for name, operation in item.items():
            if ctx.cancelled():
                return
            try:
                method = normalize.http_method(name)
            except normalize.NormalizeError:
                continue
            if method not in METHOD_NAMES:
                continue                # `$ref` / `summary` / `servers` 这些不是操作
            if not isinstance(operation, dict):
                ctx.on_bad_line(1, f"{raw_path} 的 {method} 必须是对象（收到 "
                                   f"{type(operation).__name__}）", "")
                continue
            params = _merge_params(shared, _param_list(operation.get("parameters")))
            params += list(_body_params(doc, operation))
            for origin in origins:
                url = origin.rstrip("/") + raw_path
                yield from sink.emit_operation(
                    url, ctx, 1, origin=origin, params=params,
                    declared_method=method,
                    declared_status=_declared_status(operation))


# ---------------------------------------------------------------------------
# 规范识别与起源
# ---------------------------------------------------------------------------


def _spec_family(doc: dict) -> str | None:
    """`"openapi"`（3.x）/ `"swagger"`（2.0）/ `None`。"""
    if isinstance(doc.get("openapi"), str) and doc["openapi"].strip():
        return "openapi"
    if isinstance(doc.get("swagger"), str) and doc["swagger"].strip():
        return "swagger"
    return None


def _doc_meta(doc: dict) -> tuple[str, str]:
    """`info.title` / `info.version` → 文档级观测的两个值。

    只在**文档级**取一次。一份规范里只有一个标题却有几十个接口，把它挂在每次
    操作上就是把一句话抄 N 遍（`ApiSink.apischema_fact` 的注释）。
    """
    info = doc.get("info")
    if not isinstance(info, dict):
        return "", ""
    title = info.get("title") if isinstance(info.get("title"), str) else ""
    version = info.get("version") if isinstance(info.get("version"), str) else ""
    return title, version


def _origins(doc: dict, ctx: ParseContext) -> list[str]:
    """这份契约声明的**起源**（可能多于一个，HAR 那种多主机形状同理）。

    两套规范的来源不同：

    * OpenAPI 3：`servers[].url`。**必须能算出绝对起源** —— 只写了相对
      `/v1` 时无法知道它服务在哪台主机上，报一条可读的坏行而不是猜一个
      （与 `endpoint_identity` 对相对路径"必须有 base"是同一条纪律：猜错的
      代价是整份契约挂到错误的主机上，而且不会有任何信号）；
    * Swagger 2：`host` + `basePath` + `schemes`。`basePath` 会**进 URL**
      （它是路径的前缀）但不进起源 —— 起源只看 `host`。
    """
    family = _spec_family(doc)
    if family == "openapi":
        servers = doc.get("servers")
        if not isinstance(servers, list) or not servers:
            ctx.on_bad_line(
                1, "OpenAPI 3 的文档没有 `servers`（或它是空的）—— 没有服务器"
                   "地址就算不出这份契约的起源，而猜一个的代价是整份契约挂到"
                   "错误的主机上。补上 `servers: [{url: https://…}]` 再来", "")
            return []
        out: list[str] = []
        for server in servers:
            url = server.get("url") if isinstance(server, dict) else None
            if not isinstance(url, str) or "://" not in url:
                ctx.on_bad_line(
                    1, f"`servers[].url` 必须是绝对 URL（收到 {url!r}）——"
                       f"相对起源无法知道契约服务在哪台主机上", "")
                continue
            out.append(url)
        return _dedupe(out)

    host = doc.get("host")
    if not isinstance(host, str) or not host.strip():
        ctx.on_bad_line(
            1, "Swagger 2 的文档没有 `host` —— 没有主机名就算不出这份契约的起源"
               "（`basePath` 只是路径前缀，补不出主机）", "")
        return []
    base_path = doc.get("basePath") if isinstance(doc.get("basePath"), str) else ""
    schemes = [s for s in _json.as_list(doc.get("schemes")) if isinstance(s, str)]
    # 优先 https：同一台主机上 http 与 https 折叠成**同一个**起源（scheme 不进
    # 身份），所以取哪个都不影响 apischema；取 https 是为了让落下的 `scheme`
    # 观测说的是"这台主机最可能是怎么被访问的"，而不是随便取到 http。
    scheme = "https" if "https" in schemes else (schemes[0] if schemes else "https")
    return _dedupe([f"{scheme}://{host.strip()}{base_path}"])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _param_list(value: Any) -> list[dict]:
    """`parameters` → 只留字典项（非字典项由调用方在别处报，这里不重复刷屏）。"""
    return [item for item in _json.as_list(value) if isinstance(item, dict)]


def _merge_params(shared: list[dict], own: list[dict]) -> list[tuple[str, dict]]:
    """路径级 + 操作级参数；**操作级的同名参数覆盖路径级的**（OpenAPI 的规矩）。

    判据是 `(name, in)` 二元组 —— 同名但位置不同（`?id` 与 `/id`）是两个参数，
    OpenAPI 自己也是这么规定的。
    """
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for item in [*shared, *own]:
        name = item.get("name")
        where = item.get("in")
        if not isinstance(name, str) or not isinstance(where, str):
            continue
        marker = (name, where)
        if marker not in merged:
            order.append(marker)
        merged[marker] = item
    return [(name, _param_detail(where, merged[(name, where)]))
            for name, where in order]


def _param_detail(where: str, item: dict) -> dict:
    """一个参数声明 → 观测值里的那个小字典（形状见 `_apis.py` 的裁定）。

    `required` **只在为真时写出来**：绝大多数参数是可选的，逐条写一个
    `required: false` 是零信息噪声（与 httpx 对 `failed=False` 的口径一致）。
    缺这个键就是"可选"。
    """
    detail: dict[str, Any] = {"in": where}
    schema = item.get("schema")
    if isinstance(schema, dict):
        kind = schema.get("type")
        if isinstance(kind, str):
            detail["type"] = kind
        ref = schema.get("$ref")
        if isinstance(ref, str):
            detail["ref"] = _ref_name(ref)
    else:
        # Swagger 2 把类型写在参数自己身上（`type` / `format` / `items`）
        kind = item.get("type")
        if isinstance(kind, str):
            detail["type"] = kind
    if item.get("required") is True:
        detail["required"] = True
    return detail


def _body_params(doc: dict, operation: dict) -> Iterator[tuple[str, dict]]:
    """请求体的**顶层属性** → 参数。

    OpenAPI 3 的 `requestBody` 与 Swagger 2 的 `in: body` 是同一个东西的两种
    写法，两边都要认 —— 不认识 Swagger 2 的写法，等于这份工具只支持一半的
    存量文档。
    """
    schema = None
    body = operation.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, dict):
            # 取第一个 media type（顺序即文档顺序）：一份契约里同一个 body 通常
            # 只声明一种结构，多种 media type 时它们说的是同一批字段。
            for media in content.values():
                if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                    schema = media["schema"]
                    break
    if schema is None:
        for item in _param_list(operation.get("parameters")):
            if item.get("in") == "body" and isinstance(item.get("schema"), dict):
                schema = item["schema"]
                break
    if schema is None:
        return
    # 名字**不带** `body.` 前缀 —— 位置那一层由 `param.<位置>.<名字>` 承担
    # （`_apis.param_field`）。两处都写会得到 `param.body.body.name`。
    for name, detail in _schema_params(doc, schema, "", frozenset()):
        yield name, {"in": "body", **detail}


def _schema_params(doc: dict, schema: dict, prefix: str, seen: frozenset[str],
                   depth: int = 0) -> Iterator[tuple[str, dict]]:
    """schema → `(参数名, 描述)` 的**扁平**列表（嵌套用 `.` 连接）。

    深度上限 `MAX_PARAM_DEPTH`（见 `_apis.py`）：一份自引用的 schema 会让"展开"
    变成无限递归，而那是拒绝服务，不是功能。到顶的节点留一条 `type=object` /
    `type=array` 的叶子，**不**默默丢掉 —— 丢掉等于让人以为"这个接口没有更多
    字段了"。
    """
    resolved, ref, seen = _resolve(doc, schema, seen)
    if resolved is None:
        yield prefix, {"type": "ref", "ref": ref}
        return
    kind = resolved.get("type")
    if not isinstance(kind, str):
        kind = "object" if isinstance(resolved.get("properties"), dict) else ""
    detail: dict[str, Any] = {}
    if kind:
        detail["type"] = kind

    properties = resolved.get("properties")
    items = resolved.get("items")
    if depth >= MAX_PARAM_DEPTH:
        if properties or items:
            detail["truncated"] = True
        yield prefix, detail
        return
    if isinstance(properties, dict) and properties:
        required = resolved.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            child_path = f"{prefix}.{name}" if prefix else str(name)
            # 父节点必填 → 整棵子树算必填。这是一处**简化**，不是规范的原文
            # （子字段可以自己可选）：逐层带一个"祖先必填"标志会让值里多出一个
            # 需要解释的维度，而"必填对象的字段"在绝大多数规范里确实都必填。
            # 简化写在明处 —— 需要精确答案时去看 `truncated` 那个键。
            subtree = list(_schema_params(doc, child, child_path, seen, depth + 1))
            if name in required_names:
                for _, child_detail in subtree:
                    child_detail["required"] = True
            yield from subtree
        return
    if isinstance(items, dict):
        yield from _schema_params(doc, items, prefix, seen, depth + 1)
        return
    yield prefix, detail


def _resolve(doc: dict, schema: dict,
             seen: frozenset[str]) -> tuple[dict | None, str, frozenset[str]]:
    """剥掉 `$ref`。返回 `(节点, 未解析的引用名, 走过的引用集合)`。

    `(None, ref, …)` = 没剥成（外部文件引用 / 环 / 指到了不存在的地方）。

    只认**本地** `#/...` 引用（文件头讲了为什么外部引用只记名字不解析）。
    `seen` 沿**这条路径**累积并交回调用方：同一个引用在这条路径上出现第二次
    就是环，停下来。环检测与 `MAX_PARAM_DEPTH` 是两道独立的闸 —— 深度闸挡的是
    "太深"，环闸挡的是"同一个点绕回来"，后者在深度闸之下仍然会无限展开。
    """
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema, "", seen
    while True:
        if not ref.startswith("#/"):
            return None, _ref_name(ref), seen      # 外部文件引用：不读文件
        if ref in seen:
            return None, _ref_name(ref), seen      # 环
        seen = seen | {ref}
        target: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return None, _ref_name(ref), seen
            target = target[part]
        if not isinstance(target, dict):
            return None, _ref_name(ref), seen
        schema = target
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            return schema, "", seen


def _ref_name(ref: str) -> str:
    """`#/components/schemas/Pet` → `Pet`（只取最后一段，够人看就行）。"""
    return ref.rsplit("/", 1)[-1]


def _declared_status(operation: dict) -> list[str]:
    """`responses` 的键 → **排序过的**状态码列表。

    排序是为了确定性：同一组码换个书写顺序必须落成同一个值，否则值成了
    (实体, 字段, 值) 三元组里那个"值"，同一件事会被记成两个值 —— 而那就是一
    条假冲突。`default` / `4XX` 这类非数字码照收：它们是规范里合法的键，丢掉
    等于丢信息。
    """
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return []
    return sorted(str(key) for key in responses)


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(OpenapiParser().parse(text.splitlines(), ctx or OfflineContext()))
