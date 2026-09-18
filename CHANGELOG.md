# CHANGELOG

只记**对外契约**的变更：`/api/v1` 的响应形状、`--json` 的字段、CLI 的退出码。
内部重构、注释、测试增补不占篇幅（git log 记那些）。

版本号跟着 `pyproject.toml` / `magpie/__init__.py` 走。

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
