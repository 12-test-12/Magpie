# CHANGELOG

只记**对外契约**的变更：`/api/v1` 的响应形状、`--json` 的字段、CLI 的退出码。
内部重构、注释、测试增补不占篇幅（git log 记那些）。

版本号跟着 `pyproject.toml` / `magpie/__init__.py` 走。

---

## 0.5.0 — 2.6 CLI 写路径（垂直切片闭合）

**纯新增命令**：`project create` / `import` / `undo` / `redo` / `jobs`。
`/api/v1` 一行未改（openapi 快照仍只差 `info.version`）；`magpie/core/**` 的
既有函数签名一个未动 —— 对 core 的改动全是**追加参数且默认值保持既有行为**
（`IngestOptions.report_dir`、`ingest.parser_drift_report`、五处连接守卫）。

至此「文件 → 库 → 命令行」第一次完整闭合：`init` → `project create` →
`import` → `ls` / `diff` → `undo` → `redo`，全程命令行。

### 对外契约（改动需升 minor 并在此登记）

**1 · 新增五个命令**（`magpie schema --json` 里逐条列出，语法从真的 argparse 生成）

| 命令 | 对应 `/api/v1` |
|---|---|
| `magpie project create <名字> [--include…] [--exclude…] [--scope <JSON>]` | `POST /projects` |
| `magpie import <路径…> [--parser] [--source] [--authority] [--archive] [--dry-run]` | `POST /imports` |
| `magpie undo <job> [--dry-run]` | `POST /jobs/{id}/undo` |
| `magpie redo <job> [--dry-run]` | `POST /jobs/{id}/redo` |
| `magpie jobs [<job>] [--state] [--applied] [--limit]` | `GET /jobs`、`GET /jobs/{id}` |

嵌套命令在 `schema --json` 里展平成带空格的键（`"project create"`），与
`RUNNERS` 的键逐字一致。

**2 · 退出码落到每个失败上的具体裁定**（表本身没变，0/1/2/3/4/5 一字未动）

| 情形 | 退出码 | `error.code` |
|---|---|---|
| 项目重名 | `3` ↔ 409 | `E_PROJECT_EXISTS` |
| 文件没有解析器被采用（已进 Inbox） | `3` ↔ 409 | `E_PARSER_AMBIGUOUS`（带 `candidates`） |
| 文件坏了 / 是二进制 / 路径不存在 | `2` ↔ 404 | `E_IMPORT_FAILED` / `E_IMPORT_BINARY` / `E_IMPORT_PATH` |
| 作业不存在 | `2` ↔ 404 | `E_JOB_NOT_FOUND` |
| `<job>` 不是正整数、`--scope` 非法 JSON、`--applied` 不是布尔 | `1` ↔ 400 | `E_BAD_JOB_ID` / `E_BAD_SCOPE` / `E_BAD_APPLIED` |

`redo` **部分成功**（`applied=0` + `impact.blocked` 非空）退出 **0** —— 因为
`/jobs/{id}/redo` 在那种情况下返回 200。那个事实写在 `data.report` 与
`--human` 的 ⚠ 里，不借用一个不存在的退出码。

**3 · 失败的 `import` 仍然交出完整报告**，挂在 `error.report` 上；一批里同时
有多种毛病时退出码只报**最需要人的**那一档（需人工裁决 > 数据错），其余的写在
`error.report.files` 与 `hint` 的追加句里。报告是导入唯一的交付通道，只回一句
错误码会逼调用方重跑一次 —— 而重跑是有副作用的。

成功的 `import` 另有 `data.notes`：一个此刻为空（或全是 `.db` / `.png` 之类
不可认领扩展名）的目录会退出 0 并在这里说清楚，而不是安静地"导了 0 个"。

**4 · `magpie doctor --json` 新增 `data.imports`**：`unknown_fields` /
`bad_lines` / `parser_drift`（2.6 的四项任务之一）。这三项**不进 `healthy`**：
`healthy` 判的是"数据坏没坏"，而格式漂移是"某个上游工具可能改了输出"。

**5 · `GET /openapi.json` 的快照仍只差 `info.version`**（0.4.0 → 0.5.0）。
没有增删任何一条路由。

### 承 2.2 的纪律，仍然是它们

一条写库的 SQL 都没有（`test_cli_source_contains_no_write_sql` 静态扫描
`magpie/cli/**`）；全部经 `magpie/core` 的受控写入口，于是 undo / 身份 / 冲突
折叠**只有一份实现**。非交互（需要人决定时返回结构化错误 + 退出码 3，绝不弹
提示）、`--json` 默认且纯 ASCII、所有写命令 `--dry-run` 与真跑**输出同构**。

### 顺带修掉的两处（不在 2.6 的清单里，但同属"让干跑这句话算数"）

* **`--dry-run` 曾经真的复制原文**：`_JobTxn` 的 ROLLBACK 只回滚数据库，而
  归档复制发生在事务之外 —— 一次被明确要求"先别动"的导入照样能把几个 GB
  写进 `raw/`。现在干跑不落盘（`ingest.archive_file`）。
* **`ingest._fact_key` 的键补上 `field_namespace`**、值过 `_value_text`：
  否则同一实体的 `entity_field` 与同名 `observation_field` 会撞成一个键，
  一条**新观测**会被计成 `updated`（库里不受影响，只错在报告上）。

### 已知技术债

`ls` / `show` 的 SQL 与 `api/routes.py` 的同名查询是**同一份语义的两份实现**；
2.6 起 `jobs` / `_job_counts` / `_decorate_job` 也记在这笔账上。计划书 §3.5 的
`core/query.py` 落地后应收敛到一处。

---

## 0.4.0 — 2.2 CLI 只读骨架（`--json` 信封与退出码表）

**纯新增**：`magpie/cli.py` 长成 `magpie/cli/`（入口仍是 `magpie.cli:main`，
外加 `__main__.py` 让 `-m magpie.cli` 照旧可用）。`magpie/core/**` 一行未动 ——
本步的兼容性约束就是"纯新增文件，不触碰 core"。

命令：`init` / `ls` / `show` / `search` / `diff` / `doctor` / `schema`。
**只有 `init` 写，而且只写 schema** —— 它连一个 project 都不建；`ls` 也不能
顺手建库（那样"库在哪、谁建的"就没有答案了）。

### 对外契约（改动需升 minor 并在此登记）

**1 · JSON 信封**（与 `/api/v1` 逐字同形）

```json
{"ok": true,  "data": ..., "meta": {"db": "...", "took_ms": 12, "schema_version": 3}}
{"ok": false, "error": {"code": "E_AMBIGUOUS", "message": "...", "hint": "...", "candidates": []}}
```

失败信封**不带 `meta`** —— 多一个字段就是契约漂移，而三个消费者（GUI / CLI /
MCP）里只要有一处按"错误里也有 meta"写，另一处就会崩。`--json` 是**默认**输出，
`--human` 才切人读表格。

**2 · 退出码表**（与 2.7 的 HTTP 状态码**一一对应**）

| 退出码 | 含义 | HTTP |
|---|---|---|
| `0` | 成功 | 2xx |
| `1` | 用法错 | 400 |
| `2` | 数据错 | 404 |
| `3` | 需人工裁决 | 409 |
| `4` | 锁冲突 | 423 |
| `5` | 版本不匹配 | 426 |

`E_NOT_IMPLEMENTED` 在表里没有对应档位（2.7 的 501 是真实存在的一个状态），
映射到 **1**：对调用方而言"请求了一个本工具还没有的语义"就是一次用法错误，
而真正的信号在 `error.code` 与 `hint` 里。不新增第 6 档 —— 表是冻结的。

**3 · `--json` 输出是纯 ASCII**（`ensure_ascii=True`）。中文走 `\uXXXX` 转义，
于是"`magpie ls --json | python -c "import json,sys;json.load(sys.stdin)"`"
这条验收**不依赖 `PYTHONIOENCODING`** 也成立（Windows 上消费者的 stdin 默认
按 GBK 解，否则会当场 `UnicodeDecodeError`）。给人读的是 `--human`。

**4 · `magpie schema --json`** 一条命令吐出类型 / 关系动词 / 字段 / 查询语法。
其中**查询语法从真的那个 argparse 生成**，不是第二份手写文档 —— 因此它不会
过期。同一份输出里带信封形状与上面那张退出码表。

**5 · `GET /openapi.json` 的快照只差 `info.version`**（0.3.0 → 0.4.0）。没有
增删任何一条路由；`/api/v1` 本身一行未改。

### 与 2.7 的关系

CLI **适配**既有契约，不重新定义它：过滤条件、分页游标、`E_UNSUPPORTED_FILTER`
的"响亮拒绝"、`E_PROJECT_REQUIRED` 的 400/退出码 1，全部与 `/api/v1` 对齐。
`search` 刻意**不实现** —— 2.7 的 `/search` 也是 501，若 CLI 自己发明一套查询
语义，就出现了"两条路、一份数据、两种答案"（§3.5 的"GUI 与 CLI 共用同一实现"）。

### 已知技术债

`ls` / `show` 的 SQL 与 `api/routes.py` 的同名查询是**同一份语义的两份实现**。
计划书 §3.5 的 `core/query.py` 落地后应收敛到一处。

---

## 0.3.0 — 2.8 GUI 只读画布（探索模式）

**纯新增**：前端目录 `frontend/`（Vue 3 + Vite）+ 四个 `/api/v1` 端点。
数据层（`magpie/core`）一行未动 —— 四个端点是 `ops.undo` / `ops.redo` /
`ops.explain_job_scope` 的 HTTP 外壳，能力本来就在那里。

### 新增

- `frontend/`：Vue 3 + Vite 的 GUI 源码。构建产物落进 `magpie/api/web/` 并
  **入库**（`magpie app` 不要求先装 node）。挂载点没变，仍是 2.7 的
  `/web` + `/` 那一处。
- `/api/v1` 四个新端点（**对外契约**，见下）。
- `check.py` 的 **READONLY** 静态红线：`frontend/src/**` 不得出现增删节点与边
  的名字（探索模式只读，计划书 §7.2）。
- `bench/test_perf_web.py`：首屏服务端交付 < 1.5s + 产物体积护栏。

### 对外契约（改动需升 minor 并在此登记）

| 端点 | 说明 |
|---|---|
| `GET /jobs` | 作业历史。`?project= ?state= ?applied= ?limit=`；每条带 `applied` / `counts.contributions` / `counts.retracted` / `can_undo` / `can_redo` |
| `GET /jobs/{id}` | 作业详情：`job` + **`scope`（`ops.explain_job_scope` 原样）** + `activity` |
| `POST /jobs/{id}/undo` | `{dry_run}`。**干跑与真跑输出同构**，只有 COMMIT→ROLLBACK 之差 |
| `POST /jobs/{id}/redo` | 同上 |

`GET /openapi.json` 的快照已同步（新增四条路由，无改动、无删除）。

### 为什么这四个端点属于 2.8

原设计里「导入 / undo / redo」由 `magpie import` / `magpie undo` /
`magpie redo`（2.6 的 CLI 写路径）承担，而 2.6 已后置。CLI 后置之后图形界面
成了唯一的用户界面 —— **没有它们，计划书 G2 出口 Gate 的「导入 → undo →
redo 精确复原」在界面上无法验收**。干跑走写队列而不是请求连接：`api` 的读
连接带 `PRAGMA query_only=ON`，而干跑是在事务里真的写一遍再 ROLLBACK。

### GUI 的交付标准落点

| 项 | 怎么守 |
|---|---|
| 只读 | 三层：`check.py` 的 READONLY 红线 / `frontend/tests/readonly.test.js` / 组件里不存在那条入口 |
| 零出站 | `check.py` 的 R7-ASSETS 扫 `magpie/api/web/**`（含构建产物）；`bench/test_perf_web.py` 再从 HTTP 交出去的字节上验一次 |
| 防视觉混乱 | 卡片尺寸是常量；> 200 节点自动按类型聚合（`src/graph.js` 的 `buildAggregate`）；位置写回 `entity_layout` |
| 空状态 | 三种空状态分开处理（无项目 / 有项目无数据 / 筛选没匹配），见 `EmptyGuide.vue` |
| 首屏 < 1.5s | 实测服务端交付 **35ms**，产物 **304KB**（含浏览器解析的那一半没量，口径写在 `bench/test_perf_web.py`） |
| 1000 节点 55fps | 支点是**渲染多少个元素**：聚合后 1000 个节点渲染 3 张卡片。`frontend/tests/graph.test.js` 断言这一点 |

### 明确未做（界面里写出来了，不是没提）

编排模式（2.6）、管线列表（`GET /pipelines` 仍 501）、搜索（`GET /search`
仍 501，`Ctrl+K` 只在**已载入的**实体里搜并如实说明）、人工写字段的值
（`PATCH /entities/{id}` 对它仍 501）。体检页把「尚未提供」的六项**列出来**，
而不是用近似指标顶上去。

---

## 0.2.0 — 2.7 `api` + SSE：人机共享同一份数据

**纯新增**（没有破坏任何既有行为）。包边界从 `core` / `cli` 扩到 `core` / `cli` / `api`。

### 新增

- `magpie/api/`：FastAPI 本地服务（extras: `pip install -e ".[api]"`）。
  - **写者唯一**：进程内单写线程 + 写队列（`api/writer.py`）。所有写（建项目、
    导入、undo、tag、layout、inbox 裁决）都排进这一条队列。
  - **SSE**：`GET /api/v1/imports/{import_id}/events`，每 100ms 批量推送
    进度 / 阶段 / 日志 / 坏行。背压策略 = 每订阅者一条有界队列 + 丢最旧 +
    帧内 `dropped` 计数（慢消费者不阻塞导入，丢帧不静默）。
  - 安全：只绑 `127.0.0.1`（绑其它地址在**绑定之前**就被拒绝）、随机端口 +
    启动时一次性 token、不开 CORS 通配。
  - 静态资源本地打包（`magpie/api/web/`），不引 CDN；`/docs` 与 `/redoc` 关闭
    （FastAPI 的 Swagger UI 默认从 CDN 拉资源，与零出站冲突）。
- `magpie app [--db] [--data-dir] [--host] [--port] [--token] [--no-browser]` ——
  唯一的 CLI 新命令。其余八个命令仍未实现（2.2 / 2.6）。
- `magpie.core.ingest.IngestOptions.on_progress` —— **追加参数，默认 `None`**：
  不传它时 `ingest` 的行为与从前逐字节一致（core 的 232 个既有测试是证据）。
  它是 SSE 进度的**唯一**来源，API 层不另起一套计数去猜。

### 对外契约（改动需升 minor 并在此登记）

- `/api/v1` 的响应信封：`{"ok":true,"data":...,"meta":{...}}` /
  `{"ok":false,"error":{"code","message","hint",...}}`。
- `GET /openapi.json` 的内容由快照测试
  （`magpie/api/tests/openapi_snapshot.json`）钉住 —— 变了就红。
- 端点：`meta` / `projects`（含 `POST`）/ `entities`（列表·详情·`PATCH`·`merge`）/
  `graph`（含 `neighbors`、`layout`）/ `lenses/{lens}` / `imports`（提交·查询·
  **events(SSE)**·cancel·undo）/ `doctor` / `inbox` / `activity` / `export` /
  `agent/context` / `healthz`。

### 明确未实现（返回 501 `E_NOT_IMPLEMENTED`，不是空数组）

| 端点 | 缺它属于哪一步 |
|---|---|
| `GET /search` | 计划书 §8（查询与搜索：结构化过滤 + FTS5 + 查询语法） |
| `GET /diff` | 计划书 §8 的增量 diff |
| `POST /paste` | 2.6 的写路径 + 阶段 3 的解析器扩张 |
| `GET/POST /pipelines`、`POST /pipelines/{id}/run` | 2.6（今天只有 `core.pipeline` 的 YAML 入口） |

另有一处**行为边界**（不是 501）：`PATCH /entities/{id}` 只写标签与人工保护标记，
**不写字段的值**。人工写值需要 `ManualWriter` 有一条值写入路径（2.6），在 HTTP
层临时拼一个 job + 贡献就是第二条写值的路 —— 那正是 R1"写入口唯一"要防的。

### 兑现的性能契约（计划书 §9.2 里长期是 ⬜ 的两项）

- `magpie app` RSS < 120MB —— 实测 **54.3MB**（含一次真实导入的峰值）。
- 冷启动 < 1s —— 实测 **676ms**（口径：进程启动 → 第一次 `/api/v1/meta` 返回 200）。

复验：`MAGPIE_PERF_STRICT=1 py -3.13 check.py bench`。
