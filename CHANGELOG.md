# CHANGELOG

只记**对外契约**的变更：`/api/v1` 的响应形状、`--json` 的字段、CLI 的退出码。
内部重构、注释、测试增补不占篇幅（git log 记那些）。

版本号跟着 `pyproject.toml` / `magpie/__init__.py` 走。

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
