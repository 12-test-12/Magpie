# Magpie（喜鹊）· 项目计划书 v3

> **把散落的情报叼回一个巢。**
>
> 定位：**单机、个人、离线优先的资源整合与资产档案工作台**（SRC / 渗透测试场景）。
> 核心体验：**打开画布就能看见"我手上有什么、它们怎么连起来、哪一块我还没看"**。
> 视觉与交互蓝本：**n8n 的画布感**（深色、节点卡片、贝塞尔连线、左侧类型面板、右侧参数面板、底部日志抽屉）。
> 客户端形态：**一份数据模型 + 一份 HTTP API，Web GUI 给人看，CLI 给 AI agent 看，两者同时在线、共享同一份数据。**
>
> 本文件是 v2 的替换版。v2 的数据层严谨性全部保留，本版重写了**技术栈、GUI、资源类型模型、路线图**，并新增"反混乱机制"。变更清单见 **附录 A**。

---

## 0. 边界（什么做、什么不做）

**做**：

1. 把**各种来源的资源数据**整合进一个统一模型：域名 / IP / 网段 / 服务与中间件版本 / 接口（API）数据 / 流量会话 / 漏洞 / 密钥泄露 / 人工笔记与任务
2. **n8n 式画布**：把实体画成节点、关系画成连线，可缩放、可折叠、可筛选、可布局持久化
3. **多镜头视图**：同一份数据，换五个角度看（资产 / 服务与中间件 / 接口 / 流量 / 漏洞）
4. **导入管线**：所有外部数据从文件系统进入，管线可见、可复跑、可撤销、可编排
5. **Web GUI（人）+ CLI（agent）双客户端**，同一 API、同一数据库、可同时在跑
6. **数据体检 + Inbox 裁决队列**：主动暴露混乱（孤儿、冲突、低置信度、未知字段、越界资产）

**不做**：

- ❌ 自动扫描 / 定时任务 / 目录监视轮询（工具永远在 Magpie 外面跑）
- ❌ **任何出站网络请求**（硬约束，CI 断言；Magpie 不主动连任何东西）
- ❌ 云端同步 / 多人协作 / 权限系统
- ❌ 插件系统
- ❌ 原始 pcap 逐包入库（流量只落"会话摘要 + 提取项"，原始文件按外部引用归档）

---

## 1. 命名落地

| 用途 | 名称 |
|---|---|
| 产品名 | **Magpie · 喜鹊** |
| CLI | `magpie` |
| Python 包 | `magpie`（核心 `magpie.core`，服务 `magpie.api`，适配 `magpie.adapters`） |
| Web GUI | `magpie app` → `http://127.0.0.1:<port>/` |
| 数据库 | `magpie.db`（+`-wal`/`-shm`） |
| 数据根 | `%APPDATA%\Magpie\`（`--data-dir` 可覆盖） |
| 原始归档 | `<data-dir>\raw\<sha256>.gz`（或外部引用） |
| 管线定义 | `pipelines/*.yaml`（也可存 DB） |

---

## 2. 技术栈与运行形态（v2 的 PySide6 方案在此废弃）

### 2.1 为什么必须换：两个需求互相拉扯

| 需求 | 指向 |
|---|---|
| "要 n8n 那种效果" + "要有可观赏性" | 需要一个浏览器级渲染器 + flow 画布库。n8n 的编辑器就是 Vue 3 + flow 画布（React Flow / vue-flow 这一系） |
| "轻量、离线、单机" | 指向原生桌面（PySide6 / QtWidgets） |

**结论（推荐）**：**本地 Web 应用**。`magpie app` 在本机 `127.0.0.1` 起一个服务，用浏览器（后续可选 WebView2 薄壳）打开。

- 用 QtWidgets 手搓出 n8n 的观感，工作量约等于写一个前端框架；而 Web 方案里画布、面板、主题、动画基本是"装配"而非"发明"。
- **同一份 HTTP API 天然同时服务 GUI 和 AI agent** —— 这正好是"一个给人看、一个给 agent 看"的最优解：不是两套系统，是**一套 API 的两个客户端**。
- 写 GUI 的时间省下来，能多做两个资源类型的整合。

**必须诚实交代的代价**：内存红线要改。v2 写的"GUI 常驻 < 150MB"在 Web 方案下不成立，必须拆成两笔账：

| 进程 | 目标 | 说明 |
|---|---|---|
| `magpie app`（Python 服务 + core + SQLite） | **RSS < 120MB** | 与 v2 同级别，甚至更低（没有 Qt） |
| 渲染进程（浏览器 / WebView2） | 200–400MB | 浏览器自己的一笔账，不算在 Magpie 头上 |

如果要"看起来像个 App 而不是浏览器标签页"，后续加一层 **WebView2 / pywebview 薄壳**（Windows 自带 Edge 运行时，不新增体积）。再重的壳（Electron）**明确不用**。

**备选（若坚持单进程桌面）**：PySide6 + `QGraphicsScene` 画布。内存更可控，但观感与开发速度都要打折，且"n8n 效果"基本不可能达到。此路线在 v2 里已经写过，本版不再展开。

### 2.2 三层架构

```
┌──────────────────────────── 客户端层 ────────────────────────────┐
│  Web GUI（人）              CLI（agent）         MCP（阶段4）      │
│  Vue 3 + flow 画布          magpie ...           magpie mcp       │
└───────────────┬───────────────────┬───────────────────┬──────────┘
                │  同一份 HTTP API（127.0.0.1，OpenAPI 自动生成）
┌───────────────▼───────────────────▼───────────────────▼──────────┐
│  magpie/api     FastAPI/uvicorn：路由 + SSE 作业流 + 静态资源      │
│                 单写线程 + 写队列（写者唯一）；默认只读             │
├──────────────────────────────────────────────────────────────────┤
│  magpie/core    纯标准库：模型 / 规范化 / 管线 / 解析器 / 查询     │
│                 ★ 零 GUI、零网络、零第三方依赖、自身不起线程        │
├──────────────────────────────────────────────────────────────────┤
│  SQLite（WAL）  magpie.db  +  raw/ 归档  +  pipelines/            │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 目录结构

```
magpie/
├── core/                     # 零 GUI / 零网络 / 仅标准库
│   ├── db.py  schema.py  migrations/
│   ├── models.py             # dataclass：Entity / Observation / Relation / Job / Report
│   ├── kinds.py              # ★ 实体类型注册表（受控词表）
│   ├── rels.py               # ★ 关系动词受控词表
│   ├── normalize.py          # 域名/IP/URL/路径/版本 规范化（唯一入口）
│   ├── store.py              # 唯一写入口
│   ├── ingest.py             # 导入管线执行器
│   ├── pipeline.py           # 管线定义解析 + 校验（可编排）
│   ├── query.py              # 搜索 / 聚合 / diff / 镜头查询
│   ├── doctor.py             # 数据体检
│   ├── graph.py              # 子图抽取 / 聚类 / 自动布局输入
│   ├── scope.py              # 授权范围匹配
│   └── parsers/              # 一个格式一个文件 + 可选依赖 lazy import
├── api/                      # FastAPI：路由 / SSE / 静态资源 / 鉴权 token
│   ├── app.py  routes/  sse.py  security.py
│   └── web/                  # 前端构建产物（Vue 3 + flow 画布）
├── cli.py                    # ★ agent 面向：全程 --json，退出码表，非交互
├── adapters/mcp.py           # 阶段 4
├── pipelines/*.yaml          # 管线定义（画布"编排模式"的落地形态）
├── fingerprints/*.yaml       # 离线指纹规则（阶段 3）
├── tests/fixtures/           # 各来源真实样本（脱敏）+ 期望结果
├── bench/                    # 基准脚本
├── requirements-core.txt     # 空 + 注释：core 只用标准库
├── requirements-api.txt      # fastapi / uvicorn
├── pyproject.toml            # requires-python >= 3.11；extras: api, api-yaml, traffic
└── README.md
```

### 2.4 可选依赖纪律（不破坏"仅标准库"）

少数格式需要第三方解析（YAML 版 OpenAPI、mitmproxy flows、pcap）：

- 一律放进 `pyproject.toml` 的 **extras**（`magpie[api-yaml]`、`magpie[traffic]`），**core 顶层绝不 import**。
- parser 内部 **lazy import**，缺失时抛可读错误："需要 `pip install magpie[api-yaml]` 才能解析 YAML 版 OpenAPI；或先用 `swagger-cli convert` 转成 JSON"。
- CI 断言：`magpie/core/**` 顶层 import 只有标准库。
- **pcap 走"外部工具导出"路线**（`tshark -T json/fields`），core 不需要任何抓包库 —— 这也符合"工具永远在 Magpie 外面"的哲学。

---

## 3. 统一数据模型（"整合很多资源但不混乱"的地基）

### 3.1 一句话模型

> **一切资源都是「实体（entity）」，实体之间是「关系（relation）」，实体身上挂「观测（observation）」，一次数据进入是一次「作业（job）」。**

只有 4 张主表 + 3 张辅助表，**新增一种资源类型不需要新增表**。

```
projects
  └─ entities ──< observations        （事实/字段：带来源、时间、人工/工具）
       │    └──< entity_tags
       ├──< relations >── entities     （有向、带关系动词）
       └──< entity_layout              （画布坐标、折叠、固定）
jobs ──< job_events                     （导入/重解析/撤销，SSE 流式日志）
raw_files                               （原始材料归档或外部引用）
activity_log                            （人 / CLI / agent 的所有写操作审计）
```

### 3.2 资源类型登记表（这是"不混乱"的第一道闸）

**规则：`kind` 必须在此表登记，未登记的 kind 无法入库**（`CHECK` 约束 + CI 断言 + 运行时校验）。

| kind | 中文 | **身份规则（唯一）** | 主要来源 | 关键关系 |
|---|---|---|---|---|
| `org` | 组织/主体 | 归一化名称 | 手工、scope | `owns`→domain/ip |
| `domain` | 域名 | punycode、小写、去尾点 | subfinder / assetfinder / crt.sh / httpx | `resolves_to`→ip、`cname`→domain、`member_of`→domain |
| `ip` | IP | 压缩写法（IPv4/IPv6 统一） | httpx / nmap / tshark | `member_of`→cidr |
| `cidr` | 网段 | 网络地址/前缀长度 | nmap / 手工 | `contains`→ip |
| `service` | 服务（host:port/proto） | `host` + `port` + `proto` | nmap / naabu / httpx | `runs_on`→domain/ip、`runs`→product、`exposes`→endpoint |
| `product` | **中间件/组件/版本** | 规范化产品名 + 版本元组（`nginx` + `1.24.0`） | nmap `-sV`(含 CPE) / httpx tech / WhatWeb / Wappalyzer / TLS 证书 / **离线指纹** | `service --runs--> product`；`vuln --affects--> product` |
| `endpoint` | 接口 | HTTP 方法 + **路径模板**（`/user/{id}` 折叠数字/UUID）+ host | OpenAPI / Postman / HAR / Burp XML / mitmproxy / dirsearch / katana / ffuf / curl | `belongs_to`→service、`has_param`→param、`returns`→apischema |
| `param` | 参数 | 名称 + 位置（query/body/header/path/cookie） | 同上 + paramspider / arjun | `endpoint --has_param--> param` |
| `apischema` | 数据模型 | OpenAPI components 里的 schema 名 | OpenAPI | `endpoint --returns--> apischema`、`refs`→apischema |
| `flow` | 流量会话 | 会话摘要指纹（5 元组 + 时间片 + 源文件偏移） | HAR / tshark 导出 / mitmproxy（可选） | `contacts`→endpoint/service、`carries`→secret |
| `vuln` | 漏洞/风险 | 模板 id + 命中位置 | nuclei / 手工 / 指纹规则 | `affects`→service/product/endpoint/domain |
| `secret` | 疑似凭据 | **值的 SHA-256**（绝不存明文） | 流量 / HAR / 响应头 / 归档响应体 | `carried_by`→flow、`exposed_at`→endpoint |
| `note` | 笔记 | 人工，不参与身份去重 | 手工 | `about`→任意实体 |
| `task` | 任务/待办 | 人工 | 手工 / 知识库 | `about`→任意实体 |

**三条硬规矩**（否则一定会乱）：

1. **禁止用自由文本当身份**。看到一个中间件，不能写成 `service` 上的一条 `tech=Nginx/1.24.0` 字符串就完事，必须**提升为 `product` 实体 + `runs` 关系**。这样"哪些资产在用 Laravel 8"是一次关系查询，"这个版本有几个目标在用"是一个计数，版本升级可对比，漏洞可按版本区间匹配。
2. **共享的东西必须外提**。同一台 Nginx 1.24.0 被 300 个 host 使用 → **一个** `product` 实体 + 300 条 `runs` 关系，而不是 300 条重复字符串。
3. **原始文本仍然保留**（`observation.type='banner'` / `'raw_tech'`），用于回溯和调指纹；但它只是证据，不是身份。

### 3.3 关系受控词表（第二道闸）

`relations.rel` 必须是以下之一（`CHECK` + CI 断言）：

| 动词 | 语义 | 例 |
|---|---|---|
| `resolves_to` | 名称解析 | domain → ip |
| `cname` | CNAME 链 | domain → domain |
| `member_of` | 从属/包含 | domain → domain（子域）、ip → cidr |
| `contains` | 反向包含 | cidr → ip |
| `owns` | 归属主体 | org → domain |
| `runs_on` | 运行于 | service → domain/ip |
| `runs` | 运行着某产品 | service → product |
| `exposes` | 暴露接口 | service → endpoint |
| `belongs_to` | 从属 | endpoint → service |
| `has_param` | 带参数 | endpoint → param |
| `returns` | 返回结构 | endpoint → apischema |
| `refs` | 结构引用 | apischema → apischema |
| `contacts` | 会话触达 | flow → endpoint/service |
| `carries` | 会话携带 | flow → secret |
| `exposed_at` | 出现在 | secret → endpoint |
| `affects` | 影响 | vuln → service/product/endpoint |
| `about` | 关于 | note/task → 任意 |
| `merged` | 合并溯源 | entity → entity（合并后保留） |

关系带 `source` / `first_seen_at` / `last_seen_at` / `seen_count` / `origin`，与 observation 同规格 —— **关系也是事实，也有历史和来源**。

### 3.4 DDL（v2 的 `assets` 泛化为 `entities`）

```sql
PRAGMA foreign_keys=ON;  PRAGMA journal_mode=WAL;  PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;  PRAGMA temp_store=MEMORY;

CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, app_version TEXT NOT NULL, applied_at TEXT NOT NULL);

CREATE TABLE projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  scope TEXT,
  scope_patterns TEXT NOT NULL DEFAULT '[]',          -- ["*.example.com","10.0.0.0/8"]
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ★ 实体：所有资源类型的统一载体
CREATE TABLE entities (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,                        -- §3.2 登记表；CI 断言
  entity_key    TEXT NOT NULL,                        -- 按 kind 的身份规则规范化后的唯一标识
  display_name  TEXT,                                 -- 原始写法，给人看
  subtitle      TEXT,                                 -- 画布卡片第二行（如 "nginx 1.24.0"）
  status        TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown','live','dead','merged','orphan')),
  review_status TEXT NOT NULL DEFAULT 'todo'
                CHECK (review_status IN ('todo','testing','done','skipped','submitted')),
  in_scope      INTEGER,                              -- 1/0/NULL
  confidence    REAL,                                 -- 0~1，多源冲突时用于排序
  attrs         TEXT NOT NULL DEFAULT '{}',           -- 类型特有的轻量字段（画布卡片/筛选用）
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (project_id, kind, entity_key)
);
CREATE INDEX idx_ent_kind   ON entities(project_id, kind, status);
CREATE INDEX idx_ent_review ON entities(project_id, review_status);
CREATE INDEX idx_ent_key    ON entities(entity_key);

-- 观测：upsert 语义（不是 append-only），人工记录受保护
CREATE TABLE observations (
  id            INTEGER PRIMARY KEY,
  entity_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  type          TEXT NOT NULL,        -- product/version/banner/port/header/tls/param/status_code/vuln/secret_hint/note...
  value         TEXT NOT NULL,
  severity      TEXT,                 -- vuln
  state         TEXT,                 -- vuln/task：suspected|confirmed|false_positive|reported|fixed
  source        TEXT,                 -- 工具名或指纹规则名
  origin        TEXT NOT NULL DEFAULT 'tool' CHECK (origin IN ('tool','manual')),
  confidence    REAL,
  extra         TEXT,                 -- JSON，仅放不适合建列的字段
  raw_file_id   INTEGER REFERENCES raw_files(id) ON DELETE SET NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE (entity_id, type, value)
);
CREATE INDEX idx_obs_entity_type ON observations(entity_id, type);
CREATE INDEX idx_obs_type_value  ON observations(type, value);
CREATE INDEX idx_obs_type_sev    ON observations(type, severity);
CREATE INDEX idx_obs_rawfile     ON observations(raw_file_id);

-- 关系：也是事实，也有历史与来源
CREATE TABLE relations (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  src_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel           TEXT NOT NULL,                        -- §3.3 受控词表
  source        TEXT,
  origin        TEXT NOT NULL DEFAULT 'tool' CHECK (origin IN ('tool','manual')),
  extra         TEXT,
  raw_file_id   INTEGER REFERENCES raw_files(id) ON DELETE SET NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE (src_id, dst_id, rel)
);
CREATE INDEX idx_rel_src ON relations(src_id, rel);
CREATE INDEX idx_rel_dst ON relations(dst_id, rel);

CREATE TABLE entity_tags (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  tag TEXT NOT NULL, PRIMARY KEY (entity_id, tag)
);
CREATE INDEX idx_tags_tag ON entity_tags(tag);

-- ★ 画布布局持久化：图不能每次打开都乱跑
CREATE TABLE entity_layout (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  view      TEXT NOT NULL,        -- graph / assets / products / endpoints / flows / vulns
  x REAL NOT NULL, y REAL NOT NULL,
  pinned    INTEGER NOT NULL DEFAULT 0,
  collapsed INTEGER NOT NULL DEFAULT 0,
  parent_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,   -- 折叠簇归属
  PRIMARY KEY (entity_id, view)
);

CREATE TABLE raw_files (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  encoding TEXT, format TEXT,
  storage TEXT NOT NULL CHECK (storage IN ('archived','external','purged')),
  stored_path TEXT,          -- archived: <data-dir>/raw/<sha256>.gz；external: 用户原文件路径
  head_sample TEXT,          -- 前 64KB，供嗅探
  imported_at TEXT NOT NULL,
  UNIQUE (project_id, sha256)
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('import','reparse','undo','pipeline','merge')),
  pipeline TEXT,                                     -- 用了哪条管线（§5.2）
  status TEXT NOT NULL DEFAULT 'running'
         CHECK (status IN ('running','ok','failed','cancelled','undone')),
  started_at TEXT NOT NULL, finished_at TEXT,
  stats TEXT,                                        -- JSON：added/updated/skipped/conflicts/bad_lines/unknown_fields
  error TEXT, actor TEXT NOT NULL DEFAULT 'cli'      -- gui | cli | mcp
);
CREATE INDEX idx_jobs_project ON jobs(project_id, started_at);

CREATE TABLE job_events (                             -- SSE 流 + 事后可回放
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  at TEXT NOT NULL, level TEXT NOT NULL, phase TEXT, message TEXT NOT NULL,
  raw_file_id INTEGER, lineno INTEGER, payload TEXT
);

CREATE TABLE activity_log (                           -- 人机同库的审计：agent 做了什么一眼可见
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL, actor TEXT NOT NULL,              -- gui | cli | mcp
  action TEXT NOT NULL, target_kind TEXT, target_id INTEGER,
  detail TEXT
);

-- 类型注册表落库一份（供 GUI 渲染与 agent 自描述；权威来源是 core/kinds.py）
CREATE TABLE kind_registry (
  kind TEXT PRIMARY KEY, label TEXT NOT NULL, category TEXT NOT NULL,
  icon TEXT, color TEXT, display_template TEXT, identity_rule TEXT,
  facet_types TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]'
);
```

### 3.5 五个镜头（Lens）—— 同一份数据，不新增表

"整合很多资源"最容易变成"每种资源一个页面、一套字段、一堆重复逻辑"。Magpie 的做法是：**只有一份模型，界面提供五个镜头**，每个镜头就是一组预设查询 + 一套卡片渲染。

| 镜头 | 看什么 | 主语 kind | 典型问题 |
|---|---|---|---|
| **图谱** | 全貌与连接 | 全部 | "这个域名的 CNAME 链最后落到哪台机器？" |
| **资产** | 域名/IP/网段清单 | domain / ip / cidr | "还有哪些没测？" |
| **服务与中间件** | 服务、产品、版本分布 | service / product | "哪几个目标还在用 Laravel 8 / Nginx 1.18？" |
| **接口** | API 端点与参数树 | endpoint / param / apischema | "这个系统暴露了几个未鉴权接口？" |
| **流量** | 会话时间轴与提取项 | flow / secret | "这段时间有哪些 host 被访问过？有 token 泄漏吗？" |
| **漏洞** | 风险看板 | vuln | "高危里哪些还没确认？" |

镜头是 **core/query.py 里的具名查询**，GUI 与 CLI 共用同一份实现，agent 也能直接调用（`GET /api/v1/lenses/products`）。

### 3.6 反混乱机制（Anti-Chaos）—— 十二条

1. **类型必须登记**：`kind` 不在登记表 → 拒绝入库（不是警告，是拒绝）。
2. **每个 kind 只有一套身份规则**，写在 `core/kinds.py`，并有幂等测试。
3. **关系动词受控**：不在词表 → 拒绝入库。
4. **共享物必须外提**为实体（product / param / apischema / secret），禁止逐资产复制字符串。
5. **一切入口走管线**：没有任何代码路径能绕过管线直接写实体（CI 断言 `INSERT INTO entities` 只出现在 `store.py`）。
6. **原始材料只归档、不在主界面展示**：需要追溯时才打开 `raw_files`（避免"把 txt 又搬进来看一遍"的倒退）。
7. **冲突进 Inbox 人工裁决**，不静默合并：同一 service 报出两个版本 → 两条 observation + 一条冲突项。
8. **布局持久化 + 聚类折叠**：节点位置存 `entity_layout`；同一 IP 下 50 个域名可以折叠成一个簇节点。
9. **数据体检页**（`doctor` 的 GUI 版）：孤儿实体、冲突、低置信度、未知字段、越界资产、归档体积、距上次备份天数，全部可视化。
10. **Inbox**：嗅探不确定、版本冲突、疑似同源合并、范围外资产、流量里发现的未登记 host，都进待裁决队列，处理完才算"干净"。
11. **审计一切写操作**（`activity_log`）：GUI、CLI、agent 谁改了什么，底部抽屉可查。
12. **类型不许随手加**：登记表控制在 ~14 种；新增类型要同时提供身份规则、样例夹具、镜头归属，缺一不可。

---

## 4. 资源整合：各来源怎么进来（逐类落地）

> 统一原则：**Magpie 不主动取数据**。所有数据都由外部工具产出文件，Magpie 读文件。所以"能整合什么"= "你能导出什么格式"。

### 4.1 域名 / IP / 网段

| 格式 | 来源 | 解析结果 |
|---|---|---|
| 每行一个域名 | subfinder / assetfinder / findomain / crt.sh | `domain` 实体（`member_of` 关系由后缀推导） |
| httpx JSONL | httpx `-json` | `domain`/`ip` + `service` + `observations`（status_code/title/tech/webserver/cname/tls/favicon）+ 关系 |
| nmap XML | nmap `-oX` | `ip`/`cidr` + `service` + `product`（`-sV` 的 service/version/cpe）+ `observations`（banner/port/tls） |
| naabu / masscan JSON/CSV | 端口扫描 | `service` |

### 4.2 中间件 / 组件 / 版本（重点）

**目标**：把"这个资产有 Nginx"这种自由文本，变成 **`product(nginx, 1.24.0)` 实体 + `service --runs--> product` 关系**。

入口：

| 来源 | 拿到什么 |
|---|---|
| nmap `-sV --version-all` | `service name`、`product`、`version`、`cpe`（最可靠，直接给 CPE） |
| httpx `-tech-detect -json` | `tech[]`、`webserver` |
| WhatWeb `--log-json` | 插件名 + 版本 |
| Wappalyzer 导出的 JSON | 技术栈 + 版本 + 置信度 |
| TLS 证书（tlsx / openssl 导出） | 证书里的组织、SNI、有效期（弱指纹） |
| **离线指纹引擎**（阶段 3） | 对**已归档的响应头/响应体**跑规则 |

**离线指纹引擎设计**（因为 Magpie 不联网，这一点必须讲清楚）：
- 指纹规则是 `fingerprints/*.yaml`：`name`、`product`、`match`（header/body/status/favicon_sha256/title 正则）、`version_from`（从哪个捕获组或 header 提取版本）。
- 输入是**已经落盘的响应**：httpx `-store-response`、HAR、mitmproxy dump。Magpie 只在归档数据上做匹配，**永不发请求**。
- 命中后：`product` 实体 + `runs` 关系 + `observations(type='fingerprint_hit', source='规则名')`。
- 好处：规则可版本化、可回归测试（黄金夹具）、可解释（"为什么判断它是 Dcat-Admin"→ 展示命中的规则与证据片段）。

**版本归一化（防混乱核心）**：
- `product.name` 规范化：小写、去厂商前缀、别名表（`nginx` / `Nginx` / `nginx/1.24.0` → `nginx`；`Apache Tomcat` / `tomcat` → `tomcat`）。
- `product.version` 解析成元组存 `attrs`（`1.24.0` → `[1,24,0]`），便于区间匹配与排序；无法解析的保留原串。
- `cpe` 存在 `attrs.cpe`，优先用于漏洞匹配。
- **同名不同版本 = 不同 `product` 实体**（这是有意的：版本变化必须可见），但用 `attrs.family` 表达"产品家族"，避免产品实体数量爆炸。

### 4.3 接口（API）数据

| 来源 | 拿到什么 |
|---|---|
| **OpenAPI 3 / Swagger 2**（JSON 必支持；YAML 走 `magpie[api-yaml]`） | servers、paths、methods、parameters、requestBody schema、responses、components（→ `apischema`） |
| **Postman Collection v2**（JSON） | 请求树、URL、method、headers、body 示例、环境变量 |
| **HAR**（Chrome DevTools / Charles / Fiddler / mitmproxy 都能导出） | 实际请求/响应、query/body 参数、状态码、content-type、耗时、set-cookie 名 |
| **Burp Suite XML** | 站点树 + 请求/响应 |
| curl 命令（**粘贴导入**） | 单条 endpoint + headers/params |
| dirsearch / katana / gau / ffuf | 路径列表 → `endpoint`（无方法则记 `UNKNOWN`） |

**路径模板折叠**（必须做，否则接口列表会爆炸）：
`/user/123/profile`、`/user/456/profile`、`/user/8f3a-.../profile` → 统一折叠成 `/user/{id}/profile`。
规则：纯数字段、UUID、长十六进制、日期 → `{id}` / `{uuid}` / `{hex}` / `{date}`；原始路径存 observation 保留证据。

**接口镜头要回答的问题**：哪些接口未鉴权（无 401/403 记录）、哪些接口返回了敏感字段、哪些参数可能注入、这个系统一共几个接口、相比上次扫描新增了哪些接口。

### 4.4 流量（重点讲清"不混乱"）

**绝不逐包入库**。三层处理：

1. **入口**：HAR（标准 JSON，首选）/ `tshark -T json|fields` 导出 / mitmproxy（可选 extras）。
2. **聚合**：按 5 元组 + 时间窗聚合成**会话**（`flow` 实体），只落摘要：起止时间、字节数、协议、源/目标、请求数。
3. **提取**：从会话里抽出 `endpoint`、`param`、`host`、`status_code`、`set-cookie` 名、`secret_hint`（命中敏感串正则时只存值的 SHA-256 + 规则名 + 位置，**绝不存明文**）。

原始流量文件按 `storage='external'` 归档（只记路径 + sha256 + 大小，不复制），需要追溯时按偏移打开 —— 因为一份 pcap/HAR 动辄几百 MB。

**流量镜头要回答的问题**：这段时间访问了哪些 host（尤其是**不在资产列表里的**，这是发现遗漏资产的关键）、有哪些疑似 token、哪些接口实际被调用了（对比 OpenAPI 的"文档里有但没人调"）。

> 这条设计顺带解决了一个隐性需求：**流量是最好的"遗漏发现器"** —— 会话里出现的新 host 直接进 Inbox 提示"发现未登记资产"。

### 4.5 漏洞 / 密钥 / 笔记任务

- `vuln`：nuclei JSONL（`template-id` / `info.severity` / `matched-at`）、离线指纹规则命中的风险项、手工录入。带 `severity` + `state`（suspected/confirmed/false_positive/reported/fixed）。
- `secret`：只存哈希 + 规则名 + 位置 + 长度 + 前缀掩码（`ghp_****abcd`）。**这是硬规则**：本地库被盗时，明文凭据是最严重的二次事故。
- `note` / `task`：人工，`origin='manual'`，任何导入/重解析都不得触碰（§5.5）。

---

## 5. 导入管线（可见、可复跑、可撤销、可编排）

### 5.1 管线阶段

```
发现文件 → 计算 sha256 → 归档（或外部引用）
  → 编码/格式探测 → 嗅探仲裁 → 选解析器
  → 逐行/流式解析 → 规范化（域名/IP/路径/版本）
  → 实体 upsert → 观测 upsert → 关系 upsert
  → 冲突检测（同实体同 type 多值 / 跨源版本不一致）→ 进 Inbox
  → 汇总报告（added/updated/skipped/conflicts/bad_lines/unknown_fields）
  → 作业收尾（SSE 广播 + activity_log）
```

### 5.2 管线可编排（"n8n 化"的正确落点）

**n8n 的本质是"把数据流画成图、然后重复执行"。** Magpie 把这个能力放在**导入管线**上，而不是放在数据上：

- 一条管线 = 一个 YAML（也可存 DB）：

```yaml
name: 常规一轮（域名+指纹+端口）
project: 某SRC
steps:
  - id: dns
    parser: domainlist
    source_glob: "recon/*subfinder*.txt"
  - id: probe
    parser: httpx
    source_glob: "recon/*httpx*.jsonl"
    after: [dns]
  - id: ports
    parser: nmap
    source_glob: "recon/*.xml"
  - id: finger
    parser: response-fingerprint      # 对 probe 归档的响应跑离线指纹
    after: [probe]
```

- 画布的**编排模式**可以可视化编辑这张图并执行；**探索模式**才是看数据的图谱。
- 这样"很多资源的整合"就变成**一组可复用、可批量跑的管线**，而不是每次手动拖文件 —— 这是把"多"变成"有序"的关键，也是 n8n 参考点里最有价值的部分。

### 5.3 解析器接口

```python
@dataclass(frozen=True)
class RawEntity:     kind: str; key: str; display: str | None; subtitle: str | None; attrs: dict
@dataclass(frozen=True)
class RawObservation:
    entity_ref: tuple[str, str]     # (kind, key) —— 绝不是 DB id
    type: str; value: str
    severity: str | None = None; state: str | None = None
    confidence: float | None = None; extra: dict | None = None
@dataclass(frozen=True)
class RawRelation:  src: tuple[str, str]; dst: tuple[str, str]; rel: str; extra: dict | None = None

class Parser(ABC):
    tool: str
    format_version: str
    known_fields: frozenset[str]          # 未知字段探测（格式漂移信号）
    max_confidence: float

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float: ...
    def parse(self, lines: Iterable[str], ctx: ParseContext
              ) -> Iterator[RawEntity | RawObservation | RawRelation]: ...
```

**关键**：解析器只产出 `(kind, key)` 引用，**绝不产出数据库自增 id** —— v1/v2 都栽在这个点上。
`ParseContext` 提供 `project_id`、`source_override`、`on_bad_line(lineno, raw, reason)`、`on_unknown_field(name, n)`、`cancelled()`、`batch()`。

### 5.4 嗅探仲裁

- 收集 `sniff > 0.5` 的候选；最高分 ≥ 0.8 且与第二名差 ≥ 0.15 → 采用；否则 → **Inbox 待确认**（GUI 弹候选、CLI 需 `--parser`）。
- `exclusive_group` 保证同类解析器只跑一个（域名列表类共用 `DomainListParser`，来源靠文件名/`--source` 推断）。
- 报告必须回显"用了哪个解析器、置信度、为什么选它"。

### 5.5 人工数据保护 + 幂等 + 撤销

| 规则 | 行为 |
|---|---|
| 冲突保护 | 重解析只删 `origin='tool'` 的记录，**`manual` 永不被任何导入触碰** |
| 同文件重复导入 | `sha256` + `parser_version` 相同 → 跳过并报告 |
| 解析器升级 | 提示"可用新解析器重跑"，`reparse` 才执行 |
| 内容相同来源不同 | 正常导入，`seen_count += 1`、`last_seen_at` 更新 |
| 撤销 | `magpie undo <job_id>`：删该 job 的 tool 记录，重算 `seen_count`，空实体标 `orphan` 或 `--purge` |
| 取消 | 每批一提交，已提交部分保留，`status='cancelled'`，仍可 `undo` |

### 5.6 失败行与格式漂移（必须可见）

- 坏行明细 → `<data-dir>/reports/<job_id>.badlines.txt`，GUI 日志抽屉可直接打开。
- **未知字段探测**：parser 声明 `known_fields`，解析时发现未知 key → 累计并提示"检测到新字段 `cname_chain`（120 行），该工具可能已更新输出格式"。
  > 这是让"格式漂移只改一个解析器"这句承诺真正成立的唯一机制：**先能发现漂移，才谈得上一处修改。**

---

## 6. API：一份接口，三个消费者

### 6.1 为什么这样设计

GUI 和 CLI 不是两套实现，而是**同一份 HTTP API 的两个客户端**：

- **GUI（人）**：`magpie app` 起服务 → 浏览器打开 → 前端调 `/api/v1/*`。
- **CLI（agent）**：默认**直连 core**（无服务也能用，适合脚本与管道）；检测到服务存活时自动走 HTTP（避免写入冲突），`--remote` 可强制。
- **MCP（阶段 4）**：把同一份 API 包成工具，默认只读。

**写者唯一**：服务进程内单写线程 + 队列；服务在跑时 CLI 走 HTTP；MCP 默认只读。这是 v2 里"阶段 4 必踩的 database is locked"的提前解。

### 6.2 端点草案

```
GET    /api/v1/meta                      # 版本 / schema 版本 / 类型登记表 / 可用解析器
GET    /api/v1/projects
GET    /api/v1/entities?kind=&status=&review_status=&in_scope=&tag=&q=&cursor=&limit=
GET    /api/v1/entities/{id}             # 详情 + 观测分组 + 关系 + 时间线
PATCH  /api/v1/entities/{id}             # review_status / tags / display_name
POST   /api/v1/entities/{id}/merge       # 合并
GET    /api/v1/graph?lens=&filter=&depth=&cursor=      # 节点+边（含布局）
GET    /api/v1/graph/{id}/neighbors?depth=2
PATCH  /api/v1/graph/layout              # 布局持久化（批量）
POST   /api/v1/graph/cluster             # 折叠/展开簇
GET    /api/v1/lenses/{lens}             # assets|products|endpoints|flows|vulns 的聚合数据
GET    /api/v1/search?q=<DSL>
GET    /api/v1/diff?since=<job_id|ISO|last>
POST   /api/v1/imports                   # {paths|dir, parser?, source?, dry_run?} → job
GET    /api/v1/imports/{job}/events      # ★ SSE：进度/阶段/日志/坏行
POST   /api/v1/imports/{job}/cancel | /undo
POST   /api/v1/paste                     # 粘贴导入：curl / JSON / URL 列表
GET    /api/v1/pipelines  POST /api/v1/pipelines  POST /api/v1/pipelines/{id}/run
GET    /api/v1/doctor                    # 数据体检
GET    /api/v1/inbox   POST /api/v1/inbox/{id}/resolve
GET    /api/v1/activity
GET    /api/v1/export?format=csv|json|domains|markdown
GET    /api/v1/agent/context?entity_id=&depth=   # ★ 给 agent 的上下文包
GET    /openapi.json                     # ★ agent 的说明书，自动生成
```

### 6.3 CLI 面向 agent 的纪律

- **非交互**：一切操作可用参数表达，绝不弹提示（需要人工裁决时返回结构化错误 + 退出码，让 agent 决定）。
- **全程 `--json`**：`{"ok":true,"data":...,"meta":{...}}`；错误 `{"ok":false,"error":{"code":"E_PARSER_AMBIGUOUS","message":"...","hint":"...","candidates":[...]}}`。
- **退出码表**：`0` 成功 / `1` 用法错 / `2` 数据错（坏行超阈值）/ `3` 需人工裁决 / `4` 锁冲突 / `5` 版本不匹配。
- **`--dry-run` 到处可用**：agent 先预演再落库。
- **`magpie schema --json`**：一条命令吐出全部类型、关系动词、字段与查询语法，agent 无需读文档。
- **`magpie ask`**（阶段 4，可选）：把上下文包交给外部 LLM，**默认关闭**；开启即明确告知破坏了"零出站网络"承诺。

### 6.4 给 agent 的上下文包

`GET /api/v1/agent/context?entity_id=...&depth=2` 返回一段 Markdown + JSON：选中实体及其邻居、观测、漏洞、时间线、未决冲突、以及"这个项目里还有什么没测"。GUI 上有"复制给 AI"按钮 —— **这样零出站网络和 AI 接入可以同时成立**。

---

## 7. GUI 设计（n8n 化）

### 7.1 外壳映射

| n8n | Magpie | 说明 |
|---|---|---|
| 顶部：工作流名 + Save + Run | 顶部：项目切换 + **导入（=Run）** + 搜索 `Ctrl+K` + 体检 + **Inbox 徽标** | Run 的位置放"导入"，因为它就是唯一的"执行"动作 |
| 左侧：节点面板（分类+搜索） | 左侧：**类型面板**（按 kind 分组 + 数量徽标 + 搜索）+ **镜头切换** + 管线列表 | 双击类型节点 = 按该类型过滤画布 |
| 中间：画布 | 中间：**图谱画布**（探索模式）/ **管线编排**（编排模式） | 见 §7.2 |
| 右侧：节点参数面板 | 右侧：**实体详情面板**（概况 / 观测 / 关系 / 时间线 / 标签） | 观测按 type 分组折叠 |
| 底部：执行日志 | 底部：**日志抽屉**（导入作业 / agent 活动 / 坏行明细） | agent 干了什么，人在这里看得到 |
| 节点卡片 | **实体卡片**（图标 + 名称 + 副标题 + 状态点 + 徽标） | 徽标：漏洞数、端口数、未测标记、越界警告 |
| 连线（执行流） | **关系连线**（贝塞尔 + 关系动词标签） | hover 高亮上下游；最近导入新增的边有流动动画 |
| 执行历史列表 | **作业历史**（可 undo / reparse / 看报告） | |
| 便签节点 | **画布便签**（自由文本批注） | 手工，不进数据层 |
| 缩放/小地图 | 同 | |

### 7.2 两种画布模式（最需要讲清的取舍）

n8n 的画布是**编辑器**（画流程 → 执行）；Magpie 的数据图谱是**派生物**（数据来自文件，图是算出来的）。如果只照抄外观，用户会误以为可以拖拽"创造数据"，那才是真正的混乱源。所以分两种模式：

| 模式 | 语义 | 可编辑？ |
|---|---|---|
| **探索模式**（默认） | 数据图谱：实体=节点，关系=边 | 只能拖位置/折叠/筛选/标注，**不能增删节点与边**（数据只能由导入产生） |
| **编排模式** | 导入管线图：解析器=节点，依赖=边 | **可编辑**，可保存、可复用、可批量跑（§5.2） |

这样"n8n 式可编排"落在管线（真正该被编排的地方），"n8n 式好看"落在图谱（真正该被看的地方），两者都不越界。

### 7.3 视觉规范（直接可实现的 token）

```
画布底       #0E0E11  + 24px 点阵网格 #1B1B20
卡片         #1A1A1F  圆角 10px  边框 #2A2A32  顶部 3px 类型色带
选中态       border #4F8DF7 + 6px 外发光
字体         Inter（UI） / JetBrains Mono（域名、IP、版本、参数）
连线         贝塞尔，默认 #3A3A45；按关系着色；新增边有流动虚线动画
```

| kind | 色 | | kind | 色 |
|---|---|---|---|---|
| domain | `#4F8DF7` | | endpoint | `#F59F00` |
| ip / cidr | `#22B8CF` | | param | `#FFA94D` |
| service | `#37B24D` | | flow | `#748FFC` |
| product | `#9775FA` | | secret | `#E64980` |
| vuln | `#F03E3E`（按 severity 深浅） | | note / task | `#FAB005` |

交互细节：滚轮缩放、空格拖拽、双击聚焦并展开邻居、`Shift` 框选、`F` 适应全屏、`L` 自动布局（dagre/elk 首次布局，之后永不动用户拖过的节点）。

**防视觉混乱三板斧**：① 卡片尺寸固定（信息量靠副标题与徽标，不靠卡片大小）；② 超过 200 节点自动进"聚合视图"（按 IP/产品/路径前缀折叠为簇）；③ 布局持久化 —— 图不会每次打开都乱跑。

### 7.4 五个镜头页 + 两个治理页

- 镜头页：资产表格 / 产品与版本分布（可点击"这个版本有哪些目标"）/ 接口树（按 service 分组 + 方法色标）/ 流量时间轴 / 漏洞看板（按 severity × state 泳道）
- **数据体检页**：孤儿实体、冲突数、低置信度、未知字段、越界资产、归档体积、距上次备份、解析器夹具健康度
- **Inbox 页**：嗅探不确定、版本冲突、疑似重复实体、未登记 host、越界资产 —— 逐条裁决，清空即"干净"
- **首次打开的空状态**是这个 App 的门面：一张"从这里开始"的引导卡（拖文件 / 粘贴 curl / 跑管线），而不是空白表格。

### 7.5 性能预算

| 项 | 目标 |
|---|---|
| 画布节点数 | 默认渲染 ≤ 300；超过则聚合/分页；1000 节点保持 55fps |
| 表格 | 虚拟滚动，页大小 200，排序过滤在 SQL |
| 首屏 | < 1.5s（本地） |
| SSE 日志 | 每 100ms 批量推送，避免刷爆 DOM |
| `magpie app` RSS | < 120MB |

### 7.6 本地服务安全

- 只监听 `127.0.0.1`（**禁止 `0.0.0.0`**），随机端口 + 启动时生成一次性 token，前端通过 header 携带。
- 不开 CORS 通配；静态资源全部本地打包（**不引 CDN，保证零出站**）。
- `magpie app --no-browser` 供只想用 API 的场景。

---

## 8. 查询与搜索

1. **结构化过滤**（走索引，永远快）：`kind / status / review_status / in_scope / tag / severity / state / 时间范围`
2. **精确与前缀**（走索引）：域名、IP、端口、URL 前缀
3. **自由文本子串**：FTS5 trigram（`value` 与 `display_name`），3 字符以下降级 `LIKE` 并提示
4. **搜索语法**（GUI 与 CLI 共用同一解析器）：
   ```
   kind:product product:nginx version:<1.20 in:scope
   type:vuln sev:high state:confirmed
   lens:endpoints service:api.example.com method:POST
   flow:since:2024-06-01 host:*.internal
   ```
5. **增量 diff**：`since=last` 输出新增实体 / 新增观测 / 新增漏洞 / 失效（live→dead、本轮未再见到）/ 新增 host（来自流量）

---

## 9. 性能与存储

- PRAGMA 基线同 v2（WAL / NORMAL / foreign_keys / busy_timeout / temp_store），批处理 5k–20k 行/事务，`executemany`。
- 接口视角/产品分布的聚合查询若变慢，再加物化汇总表（阶段 3 视实测决定，不提前优化）。
- 归档策略：`archived`（小文件，gzip 进 `raw/`）/ `external`（大文件，只记路径 + sha256）/ `purged`。
- `magpie gc --raw-older-than 180d`；`doctor` 显示 `raw/` 占比。
- 备份：`magpie backup`（`Connection.backup()`）+ **迁移前自动快照**；WAL 下不要用复制文件的方式备份。
- 硬指标（可验收）：core 仅标准库 / `magpie app` RSS < 120MB / 冷启动 < 1s / 导入 10 万观测 < 60s / 100 万观测可用。**`bench/` 必须在阶段 1 就存在**，否则"轻量"无法验收。

---

## 10. 安全与合规

1. `.gitignore`：`*.db`、`*.db-wal`、`*.db-shm`、`raw/`、`reports/`；fixtures 必须脱敏。
2. **私密数据只存哈希**：`secret` 实体存值的 SHA-256 + 规则名 + 位置 + 掩码前缀，永不存明文。
3. **敏感串扫描**：导入时对 `extra` 与归档内容做正则（`Authorization` / `Bearer` / `password=` / PEM 私钥 / 云 AK），**只计数、不回显原文**。
4. **导出默认脱敏**：`export` 默认剔除 headers/cookie 类字段，`--full` 才含原文。
5. **零出站网络**（CI 断言）+ 本地服务只绑 `127.0.0.1` + 一次性 token。
6. 建议数据库与 `raw/` 放加密卷；静态加密（SQLCipher）列为可选，不进 v1（会引入非标准库二进制）。
7. README 首段声明"仅用于自有或已获授权的目标"；`scope` 越界告警是它的技术配套。

---

## 11. 测试策略

| 层 | 内容 |
|---|---|
| `normalize` | 参数化 + 幂等属性测试：域名/IPv6/IDN/尾点/路径模板折叠/版本解析 |
| `kinds`/`rels` | 每个 kind 的身份规则幂等；关系动词全覆盖；未登记 kind 必然被拒 |
| `parsers` | 黄金夹具 `tests/fixtures/<source>/<format_version>/` + `expected.json` |
| `pipeline` | YAML 校验、步骤依赖拓扑、缺失文件降级 |
| `ingest` | 两次导入观测数不变；`--dry-run` 不写库；取消后 `undo` 干净 |
| 人工数据保护 | 重解析后 `origin='manual'` 记录逐条不变（回归） |
| 冲突检测 | 同实体同 type 多值 → 恰好产生一条 Inbox 项 |
| 迁移 | v0→vN 全量跑通 + 失败自动回滚 |
| 负向夹具 | 空文件 / 只有 BOM / GBK / CRLF+孤立 CR / 超长行 / NUL 字节 / zip / 同名不同内容 / 损坏 JSON / HAR 里超大 body |
| CI 断言 | core 顶层不 import 第三方与 GUI；`INSERT INTO entities` 只在 `store.py`；kind/rel 词表 |
| API 契约 | `openapi.json` 快照测试（破坏性变更必须显式确认） |
| GUI | 画布 1000 节点帧率、表格虚拟滚动、SSE 不断流（Playwright，阶段 2 起） |

---

## 12. 迁移与版本

- `schema_migrations` + `PRAGMA user_version` 双写；迁移前自动备份。
- 低版本 DB + 高版本程序 → 提示并迁移；高版本 DB + 低版本程序 → **拒绝启动**。
- `kind_registry` 随迁移更新；新增 kind 必须配套夹具与镜头归属。
- 语义化版本；`/api/v1` 与 `--json` 字段视为对外契约，变更需升 minor 并写 CHANGELOG。

---

## 13. 路线图（垂直切片优先，每阶段都有可看的东西）

> 真实工作量比 v2 大得多（多了一个完整前端），诚实估计：**阶段 1–3 合计 8–14 周（业余）**。

### 阶段 0 · 三根钉子（2–3 天，做完就能决策）

1. **SQLite**：建 §3.4 全部表，插 10 万观测，测导入耗时与库体积。
2. **本地服务 + 前端骨架**：`magpie app` 起 FastAPI + Vue 3 + flow 画布，渲染 **1000 个节点**，测帧率与两侧内存。
3. **格式摸底**：用**真实** httpx/nmap/nuclei 输出各跑一遍嗅探 + 手工解析，确认格式假设（尤其 `-sV`、`-store-response`、HAR 导出）。

**验收**：三条都过 → 按本计划走；第 2 条不达标 → 回到 PySide6 桌面路线（观感打折）。

### 阶段 1 · 垂直切片（3–5 周）：**一条链路端到端通**

> 先打通"从文件到画布"的**一条**完整链路，再扩资源类型。这样第 3 周就有能看能用的东西，而不是第 5 周才第一次见到界面。

1. core：`db/schema/migrations` + `kinds/rels/normalize/store`
2. parsers：`domainlist` + `httpx`（含 tech → **product 实体 + runs 关系**）
3. `ingest` + `pipeline`（先跑通 YAML 定义，不做可视化编辑）
4. `cli.py`：`init / import / ls / show / search / diff / undo / doctor`（全程 `--json`）
5. `api`：上表核心端点 + `/imports/{job}/events` SSE
6. GUI：n8n 式外壳（顶栏/左栏/右栏/底部抽屉）+ 资产表格 + 实体详情 + 导入进度
7. GUI：**图谱画布（探索模式，只读）** —— 交付"观感"这一项

**验收（正向）**：拖入真实 subfinder + httpx 输出 → 表格无重复 → 详情能看到状态码/技术栈/IP → 画布出现 domain→ip、service→product 的图 → `diff --since last` 正确 → `undo` 干净回滚 → CLI 全 `--json` 可被脚本消费。
**验收（负向）**：空文件 / GBK / 坏行明细 / 越界告警 / zip / 损坏 JSON 都不崩且有可读提示。

### 阶段 2 · 资源扩张（3–5 周）

1. `nmap`（端口 + CIDR + CPE → product，最高优先级：中间件版本的主来源）
2. `nuclei`（vuln + severity + state）+ 漏洞看板
3. `urls`（dirsearch/katana/gau/ffuf，路径模板折叠）
4. **接口**：OpenAPI(JSON) + Postman + HAR + curl 粘贴导入 → 接口镜头
5. **服务与中间件镜头**：产品/版本分布 + "这个版本有哪些目标"
6. 数据体检页 + Inbox 页
7. 编排模式（可视化编辑管线）+ 作业历史
8. 标签 / 批量改 `review_status` / 合并实体 / 布局持久化与聚类

### 阶段 3 · 深度整合（3–4 周）

1. **流量**：HAR / tshark 导出 → 会话聚合 → 提取 endpoint/param/secret → 流量镜头
2. **离线指纹引擎** + `fingerprints/*.yaml` + 回归夹具
3. 时间线视图、导出（CSV/Markdown 报告）、PyInstaller/WebView2 打包
4. 可选 extras：`api-yaml`、`traffic`

### 阶段 4 · agent 整合

1. MCP（默认只读）+ `magpie schema --json` 自描述
2. 画布上的"复制给 AI"上下文包 + `magpie ask`（可选、显式开启出站）
3. 知识库（Laravel / Dcat-Admin → 检查清单 → 生成 task）
4. 风险评分、攻击面概览（数据已在模型里，只是换视图）

---

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| 资源类型越加越多 → 模型与界面失控 | kind/rel 受控词表 + 登记流程 + 一份模型多镜头（§3.6） |
| 中间件版本以自由文本堆积 | 版本提升为 `product` 实体 + 别名/版本元组归一化（§4.2） |
| 接口列表爆炸（同一接口几千条） | 路径模板折叠（§4.3） |
| 流量把库撑爆 | 只存会话摘要 + 提取项；原始文件外部引用；明文凭据只存哈希（§4.4） |
| 图谱变成一团毛线 | 固定卡片尺寸 + 200 节点以上自动聚合 + 布局持久化 + 默认按镜头过滤（§7.3） |
| Web UI 内存超预算 | 预算拆两笔账（服务 < 120MB，浏览器另算）；可选 WebView2 薄壳（§2.1） |
| 编辑画布导致脏数据 | 探索模式只读、编排模式才可编辑（§7.2） |
| 解析器格式漂移静默缺数据 | 黄金夹具 + 未知字段探测 + 坏行明细（§5.6） |
| 重解析冲掉人工成果 | `origin='manual'` 保护 + 回归测试（§5.5） |
| GUI/CLI/MCP 争写锁 | 写者唯一 + 服务内写队列 + `busy_timeout`（§6.1） |
| agent 行为不可见 | `activity_log` + 底部日志抽屉（§3.6-11） |
| 前端越写越重 | 不引 CDN、不用 Electron、不用 WebEngine；静态资源本地打包（§7.6） |

---

## 15. 立即可开工的最小闭环

```
1. pyproject.toml（>=3.11, extras: api/api-yaml/traffic）+ .gitignore
2. core/schema.py + migrations/0001_init.sql        ← §3.4 直接抄
3. core/kinds.py + core/rels.py + core/normalize.py + 幂等测试
4. core/parsers/domainlist.py + httpx.py + tests/fixtures/
5. core/store.py + core/ingest.py + core/pipeline.py
6. cli.py → init / import / ls / show / doctor（全程 --json）
7. api/app.py + routes + SSE → magpie app
8. api/web：n8n 式外壳 → 表格 → 详情 → 画布（探索模式）
```

---

## 附录 A · 相对 v2 的变更

**架构与形态**

1. GUI 技术栈：**PySide6 桌面 → 本地 Web（FastAPI + Vue 3 + flow 画布）**；内存红线拆成"服务 < 120MB"与"浏览器另计"两笔账（§2.1）
2. 新增 `magpie/api` 层与"一份 API、三个消费者（GUI/CLI/MCP）"模型；CLI 默认直连 core，服务在跑时自动走 HTTP（§6.1）
3. 新增可选 extras 纪律（`api-yaml` / `traffic`），core 仍严格仅标准库（§2.4）
4. 本地服务安全：只绑 `127.0.0.1` + 一次性 token + 不引 CDN（§7.6）

**数据模型**

5. `assets` → **`entities`**（通用实体 + `kind`）；新增**资源类型登记表**与**关系受控词表**（§3.2、§3.3）
6. 新增实体类型：`org` / `service` / **`product`** / **`endpoint`** / `param` / `apischema` / **`flow`** / `secret`（v2 只有域名/IP/网段）
7. **中间件版本提升为 `product` 实体 + `runs` 关系**（v2 是 assets 上的自由文本），含别名表与版本元组归一化（§4.2）
8. **接口以路径模板折叠**成 `endpoint` 实体（§4.3）
9. **流量只落会话摘要 + 提取项**，原始文件外部引用；`secret` 只存哈希（§4.4）
10. `relations` 独立成表，带来源/时间/`seen_count`/`origin`（关系也是事实）
11. 新增 `entity_layout`（画布布局持久化）、`kind_registry`、`activity_log`、`job_events`
12. `entities.attrs` JSON 字段用于卡片/筛选的轻量类型字段

**功能**

13. **n8n 式画布**：探索模式（只读图谱）+ 编排模式（可编辑管线）（§7.2）
14. **五个镜头**（图谱/资产/服务与中间件/接口/流量/漏洞）+ 作业历史（§3.5、§7.4）
15. **编排式导入管线**（YAML + 可视化编辑 + 可复用可批量跑）（§5.2）
16. **离线指纹引擎**（对已归档响应做规则匹配，永不发请求）（§4.2）
17. **数据体检页 + Inbox 裁决队列**（§7.4）
18. **给 agent 的上下文包** `GET /agent/context` + GUI"复制给 AI"（§6.4）
19. **粘贴导入**（curl / JSON / URL 列表）（§6.2）
20. CLI 面向 agent 的纪律：非交互、全程 `--json`、错误码表、`magpie schema --json`（§6.3）
21. 搜索语法扩展（`kind:` / `product:` / `version:` / `method:` / `lens:`）（§8）
22. 前端测试（Playwright：画布帧率、虚拟滚动、SSE 不断流）（§11）

**路线图**

23. 阶段 0 改为"三根钉子"（含**画布渲染 1000 节点**的前端技术验证）；阶段 1 改为**垂直切片**（先一条链路端到端通）；新增资源扩张、深度整合两个阶段；总工期诚实估到 8–14 周（§13）

## 附录 B · 明确不做（防膨胀）

Electron / QtWebEngine / QML / 引 CDN 资源 / 云同步 / 多人协作 / 权限体系 / 自动扫描与定时任务 / 目录监视轮询 / 插件系统 / 逐包流量入库 / 明文凭据入库 / 攻击面图与风险评分（阶段 4 再议）/ 在 core 里发任何网络请求。

新功能要进来，先回答三问：**它会不会让 core 多一个依赖？让空跑多一个进程？让网络多一次请求？**
