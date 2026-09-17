# Magpie（喜鹊）· 项目计划书 v4

> **把散落的情报叼回一个巢。**
>
> 定位：**单机、个人、离线优先的资源整合与资产档案工作台**（SRC / 渗透测试场景）。
> 核心体验：**打开画布就能看见"我手上有什么、它们怎么连起来、哪一块我还没看"**。
> 视觉与交互蓝本：**n8n 的画布感**（深色、节点卡片、贝塞尔连线、左侧类型面板、右侧参数面板、底部日志抽屉）。
> 客户端形态：**一份数据模型 + 一份 HTTP API，Web GUI 给人看，CLI 给 AI agent 看，两者同时在线、共享同一份数据。**
>
> 本版是 v3 的替换版。**产品蓝图（定位 / 技术栈 / 管线 / GUI / API / 路线图）继承 v3**，
> 但**数据模型一节被重写**：v3 §3.4 的 DDL 已被 `v3.2-design.md` 取代，本版不再复制它。
> 变更清单见 **§0** 与 **附录 A**。

---

## 0. 先读这一节：v3 哪里跟现实脱节了

v3 是在**数据层还没写**的时候写的，所以它描述的是"打算怎么建"。数据层建完之后，
v3 里有一节从"计划"变成了"错误的现状描述"——**照它排期会做错事**。逐条如下：

| v3 的说法 | 实际情况 | 本版处置 |
|---|---|---|
| §2.3 目录结构：`core/db.py`、`schema.py`、`models.py`、`kinds.py`、`rels.py`、`normalize.py`、`store.py`、`ingest.py`…… | 实际落地的是 `core/store.py`（事实写入口 + recompute + 不变量检查）、`core/ops.py`（事务型状态迁移）、`core/confidence.py`、`core/manual.py`、`core/migrate.py`；schema 是单文件 `schema/001_init.sql` | **§2.3 已按现实重写**；未建的模块标注为"计划中" |
| §3.4 DDL：`entities` / `observations` / `relations` 三张 upsert 主表 + `UNIQUE(entity_id,type,value)` | **这份 DDL 已经被 `v3.2-design.md` 整体取代**。实际模型是**五层分离 + append-only 贡献流**：数据行是缓存，贡献流才是事实；`contributions` 无 UPDATE 无 DELETE | **§3 改为"权威来源 + 现状"**，不再复制 DDL |
| §12 迁移与版本：`schema_migrations` + `PRAGMA user_version` | 已实现：`core/migrate.py` + `schema_migrations(version, name, checksum, applied_at)` | §12 标注**已实现**并补校验和纪律 |
| §13 阶段 0「三根钉子」（建表 / 起服务 / 格式摸底） | 三根钉子里**只有数据层那一根被真正钉了**，而且钉得比 v3 设想深得多（不变量、undo/redo、身份迁移、性能契约） | **§13 路线图按真实状态重排** |
| §9 提到的性能目标没有实测依据 | 现在有：`bench/test_perf.py` 实测 **48 us/row**、增长**线性**，`MAGPIE_PERF_STRICT=1` 是它的可执行形式 | §9 换成**实测数字** |

**还有一个必须点名的教训**（它决定了本版怎么维护）：

> `v3.2-design.md` 曾同时写错三件事 ——「redo 未实现」「性能未验证」「62 passed」——
> 而代码里早已不是那样。**文档一旦落后，它就不是中性的：它会主动误导下一个做规划的人。**
>
> 所以本版的第一条纪律是：**排期前先跑一遍，别信文档（包括这一份）。**
> 一条命令：`python check.py`。

---

## 1. 边界（什么做、什么不做）

**做**：

1. 把**各种来源的资源数据**整合进一个统一模型：域名 / IP / 网段 / 服务与中间件版本 / 接口（API）数据 / 流量会话 / 漏洞 / 密钥泄露 / 人工笔记与任务
2. **n8n 式画布**：把实体画成节点、关系画成连线，可缩放、可折叠、可筛选、可布局持久化
3. **多镜头视图**：同一份数据，换五个角度看（资产 / 服务与中间件 / 接口 / 流量 / 漏洞）
4. **导入管线**：所有外部数据从文件系统进入，管线可见、可复跑、**可撤销**、可编排
5. **Web GUI（人）+ CLI（agent）双客户端**，同一 API、同一数据库、可同时在跑
6. **数据体检 + Inbox 裁决队列**：主动暴露混乱（孤儿、冲突、低置信度、未知字段、越界资产）

**不做**：

- ❌ 自动扫描 / 定时任务 / 目录监视轮询（工具永远在 Magpie 外面跑）
- ❌ **任何出站网络请求**（硬约束，CI 断言；Magpie 不主动连任何东西）
- ❌ 云端同步 / 多人协作 / 权限系统
- ❌ 插件系统
- ❌ 原始 pcap 逐包入库（流量只落"会话摘要 + 提取项"，原始文件按外部引用归档）

---

## 2. 技术栈与运行形态

### 2.1 为什么是本地 Web 应用（v3 结论，不变）

| 需求 | 指向 |
|---|---|
| "要 n8n 那种效果" + "要有可观赏性" | 需要浏览器级渲染器 + flow 画布库 |
| "轻量、离线、单机" | 指向原生桌面（PySide6 / QtWidgets） |

**结论**：**本地 Web 应用**。`magpie app` 在本机 `127.0.0.1` 起服务，浏览器打开。
理由不变：QtWidgets 手搓 n8n 的观感约等于写一个前端框架；而 Web 方案里画布、面板、
主题基本是"装配"而非"发明"；**同一份 HTTP API 天然同时服务 GUI 和 AI agent**。

**内存要拆成两笔账**（v3 已经诚实交代过）：

| 进程 | 目标 |
|---|---|
| `magpie app`（Python 服务 + core + SQLite） | **RSS < 120MB** |
| 渲染进程（浏览器 / WebView2） | 200–400MB，浏览器自己的一笔账，不算在 Magpie 头上 |

### 2.2 三层架构

```
┌──────────────────────────── 客户端层 ────────────────────────────┐
│  Web GUI（人）              CLI（agent）         MCP（阶段 4）      │
│  Vue 3 + flow 画布          magpie ...           magpie mcp       │
└───────────────┬───────────────────┬───────────────────┬──────────┘
                │  同一份 HTTP API（127.0.0.1，OpenAPI 自动生成）
┌───────────────▼───────────────────▼───────────────────▼──────────┐
│  magpie/api     FastAPI/uvicorn：路由 + SSE 作业流 + 静态资源      │
│                 单写线程 + 写队列（写者唯一）；默认只读             │
├──────────────────────────────────────────────────────────────────┤
│  magpie/core    纯标准库：事实层 / 缓存层 / 身份层 / 操作 / 置信度  │
│                 ★ 零 GUI、零网络、零第三方依赖、自身不起线程        │
├──────────────────────────────────────────────────────────────────┤
│  SQLite（WAL）  magpie.db  +  raw/ 归档  +  pipelines/            │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 目录结构（**已按 2026-09-17 的实际内容重写**）

标注说明：**✅ 已存在** / **⬜ 计划中**。

```
D:\Magpie\
├── core/                          ✅ 数据层（已落地，零 GUI / 零网络 / 仅标准库）
│   ├── store.py          ✅ 唯一写入口 add_contribution + recompute + GC + 不变量检查
│   │                        + 快照等价 + Online Backup + display_key
│   ├── ops.py            ✅ 事务型状态迁移 T1：undo / redo / rekey / merge / unmerge /
│   │                        compact，全部支持 --dry-run，全部走同一模板
│   ├── confidence.py     ✅ §8.2 反多数票置信度
│   ├── manual.py         ✅ INV-12 唯一 manual 写入口（单写队列 + 乐观版本 + 并集合并）
│   ├── migrate.py        ✅ schema 版本化与迁移 runner（v3.2-r4 新增）
│   ├── store/ops 之外的数据层模块                    ⬜ models.py / query.py / doctor.py /
│   │                                                    graph.py / scope.py / normalize.py
│   └── parsers/                                      ⬜ 一个格式一个文件（阶段 2）
├── schema/
│   └── 001_init.sql      ✅ 全部 DDL + 触发器 + 视图（936 行）
├── core/tests/
│   ├── test_invariants.py    ✅ 不变量与属性测试（1000 条随机序列）
│   └── test_v32r4_debts.py   ✅ 债务收口的反例测试（含写入口 lint）
├── bench/test_perf.py    ✅ 性能契约（MAGPIE_PERF_STRICT=1 时转为严格断言）
├── verify_v32r2.py       ✅ 端到端验收脚本 23/23
├── verify_v32r3.py       ✅ 端到端验收脚本 27/27
├── check.py              ✅ 一条命令跑全绿的可重复入口
├── api/                                              ⬜ FastAPI：路由 / SSE / 静态资源
├── cli.py                                            ⬜ agent 面向：全程 --json，退出码表
├── adapters/mcp.py                                   ⬜ 阶段 4
├── pipelines/*.yaml                                  ⬜ 管线定义
├── fingerprints/*.yaml                               ⬜ 离线指纹规则（阶段 3）
├── tests/fixtures/                                   ⬜ 各来源真实样本（脱敏）+ 期望结果
├── requirements-core.txt                             ⬜ 空 + 注释：core 只用标准库
├── requirements-api.txt                              ⬜ fastapi / uvicorn
├── pyproject.toml                                    ⬜ requires-python >= 3.11；extras
├── .gitignore            ✅ 先落再提交（否则 __pycache__ 进历史）
├── README.md             ✅ 安全声明 + 三行导览 + 一条命令验证
└── *.md                  ✅ 计划书 / v3.2-design / 搭建思路 / 历史数据模型稿
```

> **与 v3 的一处实质差别**：v3 把数据层拆成 `db.py / schema.py / models.py / store.py`
> 四个文件。实际落地时合成 `store.py` + `ops.py` 两个模块，理由是数据层的复杂度
> 集中在两件事上——**"事实如何投影成缓存"**（store）与**"状态如何被事务地迁移"**（ops）。
> 按这两条缝切，比按"db/schema/models"切更贴合真实改动的位置。这不是简化，是收敛。

### 2.4 可选依赖纪律（不破坏"仅标准库"）

- 少数格式需要第三方解析（YAML 版 OpenAPI、mitmproxy flows、pcap）：一律放进
  `pyproject.toml` 的 **extras**，**core 顶层绝不 import**，parser 内部 **lazy import**，
  缺失时抛可读错误。
- CI 断言：`magpie/core/**` 顶层 import 只有标准库。
- **pcap 走"外部工具导出"路线**（`tshark -T json/fields`），core 不需要任何抓包库。

---

## 3. 数据模型：权威来源与现状

> **本节的写法与 v3 不同，这是有意的。** v3 在这里贴了一份 300 行 DDL，而那份 DDL
> 已被 `v3.2-design.md` 取代。**计划书不再是数据模型的权威来源** —— 权威是
> `v3.2-design.md`（冻结文档，带修正记录 r2/r3/r4）与 `schema/001_init.sql`（唯一 DDL）。
> 本节只回答"模型长什么样、哪些已经落地、剩下什么"。

### 3.1 一句话模型（v3.2 版，已落地）

> **数据行是缓存，贡献流是事实。**

一次数据进入 = 一次 **job**；job 写下的每一条 **contribution** 是不可变事实；
`entities` / `observations` / `relations` 这些"当前状态"是**由事实投影出来的缓存**，
可以随时全量重建。**身份**（哪些 key 指向同一个实体）是**事实**，不是缓存。

这条公理是 v3.2 相对 v3 最大的变化，也是"可撤销导入"能成立的原因 ——
撤销不是"把数据删回去"，而是**追加一条"自此不再活跃"的事实**，然后重新投影。

### 3.2 五层分层（已落地）

| 层 | 表 | 能否重建 | 规矩 |
|---|---|---|---|
| **A 事实层** | `raw_files` `evidence` `contributions` `contribution_supersessions` | 否 | append-only；**唯一写入口** `add_contribution` |
| **B 缓存层** | `entities` `observations` `relations` `entity_identity` | 是 | 只能由 `recompute` 覆盖，**谁都不许手改** |
| **C 身份层** | `entity_alias` `entity_merge_events` `entity_merge_members` | 否 | identity overlay；**recompute 不得重建或覆盖** |
| **D 工作层** | `entity_layout` `entity_tags` `manual_fields` `inbox_items` | 否 | 人工状态，GC 锚点，永不被自动清理 |
| **E 审计层** | `jobs` `activity_log` `tag_events` | 否 | 永不回滚 |

**三条硬线**（都由机制守着，不靠自觉）：

1. **写入口唯一性**：任何模块不得直写 `contributions`。
   守着它的是 `core/tests/test_v32r4_debts.py` 里的一条 **lint 测试** ——
   全仓扫描，`INSERT INTO contributions` 出现在 `store.py` 之外即失败。
   （compact 曾是唯一的违规者，v3.2-r4 已抓回。）
2. **缓存不可手改**：缓存层与事实层的差异由 `assert_integrity` 在每次操作边界检查，
   并有 `v_integrity_violations` 视图兜底。
3. **人工数据不可被导入触碰**：`manual` 归属的贡献不参与压实、不被重解析覆盖。

### 3.3 资源类型登记表（**计划中，尚未落地**）

v3 §3.2 设计了一张 14 种 kind 的登记表（`org` / `domain` / `ip` / `cidr` / `service` /
`product` / `endpoint` / `param` / `apischema` / `flow` / `vuln` / `secret` / `note` / `task`），
要求"未登记的 kind 无法入库"。

**诚实交代现状**：数据层**故意**没有把这份词表写死 —— `entities.kind` 目前是自由文本
（有索引，无 `CHECK`）。原因是：**kind 的身份规则只有在解析器落地时才第一次有真实形状**
（`product` 的身份规则要处理别名与版本元组，`endpoint` 要处理路径模板折叠）。
在没有解析器的时候把词表写死，等于凭空设计一套必然要返工的约束。

**因此这是一项有意的延后，不是遗漏。** 它的落点是**阶段 2 的第 2 步（解析器）**，
验收标准写在那里：**每个 kind 必须同时提供身份规则、样例夹具、镜头归属，缺一不可**。
在此之前，`kind` 的正确性由"谁写进来的"负责 —— 也就是由还没有的解析器负责。

> ⚠️ 如果你正在阶段 2 之前写任何直接 `INSERT` 实体缓存的代码，这条延后**不会**保护你。

### 3.4 关系受控词表（**计划中，尚未落地**）

v3 §3.3 的 18 个关系动词（`resolves_to` / `cname` / `member_of` / `runs` /
`exposes` / `affects` / …）同样**尚未落成 `CHECK`**，原因与 §3.3 相同：
关系动词由解析器产出，词表随解析器一起落。`relations.rel` 现在是自由文本。

### 3.5 五个镜头（Lens）—— 同一份数据，不新增表

| 镜头 | 看什么 | 主语 kind | 典型问题 |
|---|---|---|---|
| **图谱** | 全貌与连接 | 全部 | "这个域名的 CNAME 链最后落到哪台机器？" |
| **资产** | 域名/IP/网段清单 | domain / ip / cidr | "还有哪些没测？" |
| **服务与中间件** | 服务、产品、版本分布 | service / product | "哪几个目标还在用 Laravel 8 / Nginx 1.18？" |
| **接口** | API 端点与参数树 | endpoint / param / apischema | "这个系统暴露了几个未鉴权接口？" |
| **流量** | 会话时间轴与提取项 | flow / secret | "这段时间有哪些 host 被访问过？有 token 泄漏吗？" |
| **漏洞** | 风险看板 | vuln | "高危里哪些还没确认？" |

镜头是**具名查询**（计划落在 `core/query.py`），GUI 与 CLI 共用同一份实现，
agent 也能直接调用（`GET /api/v1/lenses/products`）。**⬜ 未落地。**

### 3.6 反混乱机制（十二条，更新为"谁在守"）

| # | 机制 | 现在由谁守 |
|---|---|---|
| 1 | 类型必须登记 | ⬜ 计划中（见 §3.3；解析器落地时补） |
| 2 | 每个 kind 一套身份规则 | ⬜ 同上 |
| 3 | 关系动词受控 | ⬜ 同上（见 §3.4） |
| 4 | 共享物必须外提为实体 | ⬜ 解析器的责任 |
| 5 | 一切入口走管线，无路径能绕过写入口 | ✅ **lint 测试**（`INSERT INTO contributions` 只在 `store.py`） |
| 6 | 原始材料只归档、不在主界面展示 | ✅ `raw_files` + `evidence`（含三级降级）已落地 |
| 7 | 冲突进 Inbox 人工裁决，不静默合并 | ✅ `inbox_items` + `_merge_conflicts` + `v_field_conflicts` 已落地 |
| 8 | 布局持久化 + 聚类折叠 | 🟡 表在（`entity_layout`），画布未建 |
| 9 | 数据体检页 | 🟡 `integrity_check` / `v_integrity_violations` / `orphan_subjects` / `purge_eligible` 在，页面未建 |
| 10 | Inbox 裁决队列 | ✅ 表与开启逻辑已落地，GUI 未建 |
| 11 | 审计一切写操作 | ✅ `activity_log` + **冻结的 action 命名空间**（写错 action 名直接报错） |
| 12 | 类型不许随手加 | ⬜ 见 §3.3 |

> 这张表的用法：**别把 ⬜ 当成"已经有的约定"**。v3 把它们都写成已成立的状态，
> 而其中一半要靠解析器落地后才谈得上成立。

---

## 4. 资源整合：各来源怎么进来

> 统一原则：**Magpie 不主动取数据**。所有数据都由外部工具产出文件，Magpie 读文件。
> 所以"能整合什么" = "你能导出什么格式"。**本节整体继承 v3，尚未开工。**

### 4.1 域名 / IP / 网段

| 格式 | 来源 | 解析结果 |
|---|---|---|
| 每行一个域名 | subfinder / assetfinder / findomain / crt.sh | `domain` 实体（`member_of` 关系由后缀推导） |
| httpx JSONL | httpx `-json` | `domain`/`ip` + `service` + observations（status_code/title/tech/webserver/cname/tls/favicon）+ 关系 |
| nmap XML | nmap `-oX` | `ip`/`cidr` + `service` + `product`（`-sV` 的 service/version/cpe）+ observations（banner/port/tls） |
| naabu / masscan JSON/CSV | 端口扫描 | `service` |

### 4.2 中间件 / 组件 / 版本（重点）

**目标**：把"这个资产有 Nginx"这种自由文本，变成 **`product(nginx, 1.24.0)` 实体 +
`service --runs--> product` 关系**。

| 来源 | 拿到什么 |
|---|---|
| nmap `-sV --version-all` | `service name`、`product`、`version`、`cpe`（最可靠） |
| httpx `-tech-detect -json` | `tech[]`、`webserver` |
| WhatWeb `--log-json` | 插件名 + 版本 |
| Wappalyzer 导出 JSON | 技术栈 + 版本 + 置信度 |
| TLS 证书（tlsx / openssl 导出） | 证书里的组织、SNI、有效期（弱指纹） |
| **离线指纹引擎**（阶段 3） | 对**已归档的响应头/响应体**跑规则 |

**离线指纹引擎设计**（因为不联网，这点必须讲清楚）：规则是 `fingerprints/*.yaml`
（`name` / `product` / `match` / `version_from`）；输入是**已落盘的响应**；命中后写
`product` + `runs` + `observations(source='规则名')`。好处：规则可版本化、可回归、可解释。

**版本归一化（防混乱核心）**：`product.name` 小写 + 去厂商前缀 + 别名表；
`version` 解析成元组存 `attrs`（`1.24.0` → `[1,24,0]`）；`cpe` 存 `attrs.cpe`；
**同名不同版本 = 不同 `product` 实体**（有意的：版本变化必须可见），
用 `attrs.family` 表达产品家族，避免实体数量爆炸。

### 4.3 接口（API）数据

| 来源 | 拿到什么 |
|---|---|
| **OpenAPI 3 / Swagger 2**（JSON 必支持；YAML 走 extras） | servers / paths / methods / parameters / responses / components（→ `apischema`） |
| **Postman Collection v2** | 请求树、URL、method、headers、body 示例 |
| **HAR**（Chrome / Charles / Fiddler / mitmproxy） | 实际请求响应、参数、状态码、耗时、set-cookie 名 |
| **Burp Suite XML** | 站点树 + 请求/响应 |
| curl 命令（**粘贴导入**） | 单条 endpoint + headers/params |
| dirsearch / katana / gau / ffuf | 路径列表 → `endpoint`（无方法记 `UNKNOWN`） |

**路径模板折叠**（必须做，否则接口列表会爆炸）：`/user/123/profile`、`/user/456/profile`
→ `/user/{id}/profile`；纯数字段 / UUID / 长十六进制 / 日期 → `{id}` / `{uuid}` / `{hex}` /
`{date}`；原始路径存 observation 保留证据。

### 4.4 流量（重点讲清"不混乱"）

**绝不逐包入库。** 三层：入口（HAR / `tshark -T json` 导出 / mitmproxy 可选）→
聚合（5 元组 + 时间窗 → `flow` 会话摘要）→ 提取（`endpoint` / `param` / `host` /
`status_code` / `set-cookie` 名 / `secret_hint`：**只存值的 SHA-256 + 规则名 + 位置，绝不存明文**）。

原始流量文件按 `storage='external'` 归档（只记路径 + sha256 + 大小，不复制）。

> 这条设计顺带解决一个隐性需求：**流量是最好的"遗漏发现器"** ——
> 会话里出现的新 host 直接进 Inbox 提示"发现未登记资产"。

### 4.5 漏洞 / 密钥 / 笔记任务

- `vuln`：nuclei JSONL、离线指纹规则命中的风险项、手工录入。带 `severity` + `state`。
- `secret`：只存哈希 + 规则名 + 位置 + 长度 + 前缀掩码（`ghp_****abcd`）。**硬规则**：
  本地库被盗时，明文凭据是最严重的二次事故。
- `note` / `task`：人工，任何导入/重解析都不得触碰。

---

## 5. 导入管线（可见、可复跑、可撤销、可编排）⬜ 未开工

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

> **这一节与数据层的接口已经就绪**：解析器产出的每条事实最终都经 `core/ops.py` 的
> `add_observation` / `add_relation` / `add_entity_field` 写入，而这些入口已经带着
> INV-16 的结构校验与 `atomic()` 保存点。**解析器不需要（也不允许）直接碰缓存表。**

### 5.2 管线可编排（"n8n 化"的正确落点）

**n8n 的本质是"把数据流画成图、然后重复执行"。** Magpie 把这个能力放在**导入管线**上，
而不是放在数据上：

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
  - id: finger
    parser: response-fingerprint      # 对 probe 归档的响应跑离线指纹
    after: [probe]
```

画布的**编排模式**可视化编辑这张图；**探索模式**才是看数据的图谱。

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

**关键**：解析器只产出 `(kind, key)` 引用，**绝不产出数据库自增 id**。
`ParseContext` 提供 `project_id`、`source_override`、`on_bad_line`、`on_unknown_field`、
`cancelled()`、`batch()`。

**并且**（v3.2 追加的要求）：解析器必须带着 `parser_fingerprint` 与 `definition_hash`
注册进 `parser_profiles`，每个 job 都冻结自己跑时的指纹 —— 这是"这个结论是哪个
parser 版本产出的"能被回答的唯一机制，也是回滚（`ops.rollback_parser`）的前提。

### 5.4 嗅探仲裁

最高分 ≥ 0.8 且与第二名差 ≥ 0.15 → 采用；否则 → **Inbox 待确认**。
`exclusive_group` 保证同类解析器只跑一个。报告必须回显"用了哪个解析器、置信度、为什么选它"。

### 5.5 人工数据保护 + 幂等 + 撤销

| 规则 | 行为 | 数据层现状 |
|---|---|---|
| 冲突保护 | 重解析只碰 `tool` 记录，**`manual` 永不被任何导入触碰** | ✅ 已落地（`manual` 贡献不参与压实、有专项测试） |
| 同文件重复导入 | `sha256` + `parser_version` 相同 → 跳过并报告 | 🟡 `raw_files` 有 `UNIQUE(project_id, sha256)` |
| 解析器升级 | 提示"可用新解析器重跑" | 🟡 `explain_job_scope` 已能报 `parser_drift` |
| 内容相同来源不同 | 正常导入，`seen_count += 1` | ✅ 缓存层 `source_count` / `logical_job_count` 已在算 |
| **撤销** | 该 job 的记录不再活跃，重算缓存 | ✅ **已落地且可逆**：`undo` → `redo` 严格互逆 |
| 取消 | 已提交部分保留，仍可 `undo` | ⬜ 随管线落地 |

> **v3 在这里写的是"删该 job 的记录"**。v3.2 之后**没有任何删除** ——
> 撤销是追加一条"自此不再活跃"的抑制事实（`contribution_supersessions`），
> 缓存重新投影。这是"撤销干净"与"历史不可篡改"能同时成立的原因。

### 5.6 失败行与格式漂移（必须可见）

坏行明细 → `<data-dir>/reports/<job_id>.badlines.txt`。
**未知字段探测**：parser 声明 `known_fields`，发现未知 key → 累计并提示
"检测到新字段 `cname_chain`（120 行），该工具可能已更新输出格式"。
> 这是让"格式漂移只改一个解析器"这句承诺真正成立的唯一机制：**先能发现漂移，才谈得上一处修改。**

---

## 6. API 与 CLI：一份接口，三个消费者 ⬜ 未开工

### 6.1 为什么这样设计

- **GUI（人）**：`magpie app` 起服务 → 浏览器调 `/api/v1/*`。
- **CLI（agent）**：默认**直连 core**（无服务也能用）；检测到服务存活时自动走 HTTP，`--remote` 可强制。
- **MCP（阶段 4）**：把同一份 API 包成工具，默认只读。

**写者唯一**：服务进程内单写线程 + 队列；服务在跑时 CLI 走 HTTP；MCP 默认只读。
（数据层的 `manual.py` 已经是这套纪律的第一个实现：单写队列 + 乐观版本 + 并集合并。）

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
GET    /api/v1/lenses/{lens}             # assets|products|endpoints|flows|vulns
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

- **非交互**：一切操作可用参数表达，绝不弹提示（需要人工裁决时返回结构化错误 + 退出码）。
- **全程 `--json`**：`{"ok":true,"data":...,"meta":{...}}`；错误
  `{"ok":false,"error":{"code":"E_PARSER_AMBIGUOUS","message":"...","hint":"...","candidates":[...]}}`。
- **退出码表**：`0` 成功 / `1` 用法错 / `2` 数据错 / `3` 需人工裁决 / `4` 锁冲突 / `5` 版本不匹配。
- **`--dry-run` 到处可用**：数据层的 `undo/rekey/merge/compact` **已经**全部支持 dry-run
  且与真实执行走同一代码路径（只有 COMMIT 变 ROLLBACK）—— CLI 只需把它透出去。
- **`magpie schema --json`**：一条命令吐出全部类型、关系动词、字段与查询语法。

### 6.4 给 agent 的上下文包

`GET /api/v1/agent/context?entity_id=...&depth=2` 返回一段 Markdown + JSON：选中实体及
其邻居、观测、漏洞、时间线、未决冲突、以及"这个项目里还有什么没测"。GUI 上有"复制给 AI"
按钮 —— **这样零出站网络和 AI 接入可以同时成立**。

---

## 7. GUI 设计（n8n 化）⬜ 未开工

### 7.1 外壳映射

| n8n | Magpie | 说明 |
|---|---|---|
| 顶部：工作流名 + Save + Run | 顶部：项目切换 + **导入（=Run）** + 搜索 `Ctrl+K` + 体检 + **Inbox 徽标** | Run 的位置放"导入"，因为它就是唯一的"执行"动作 |
| 左侧：节点面板 | 左侧：**类型面板**（按 kind 分组 + 数量徽标）+ **镜头切换** + 管线列表 | 双击类型 = 按该类型过滤画布 |
| 中间：画布 | 中间：**图谱画布**（探索）/ **管线编排**（编排） | 见 §7.2 |
| 右侧：节点参数面板 | 右侧：**实体详情面板**（概况/观测/关系/时间线/标签） | 观测按 type 分组折叠 |
| 底部：执行日志 | 底部：**日志抽屉**（导入作业 / agent 活动 / 坏行明细） | agent 干了什么，人在这里看得到 |
| 节点卡片 | **实体卡片**（图标 + 名称 + 副标题 + 状态点 + 徽标） | 徽标：漏洞数、端口数、未测标记、越界警告 |
| 连线（执行流） | **关系连线**（贝塞尔 + 关系动词标签） | hover 高亮上下游；新增边有流动动画 |
| 执行历史列表 | **作业历史**（可 undo / redo / reparse / 看报告） | 数据层的 undo/redo 已经是精确互逆的，GUI 只是把已有的东西露出来 |

### 7.2 两种画布模式（最需要讲清的取舍）

n8n 的画布是**编辑器**（画流程 → 执行）；Magpie 的数据图谱是**派生物**（图是算出来的）。
如果只照抄外观，用户会误以为可以拖拽"创造数据"，那才是真正的混乱源。所以分两种模式：

| 模式 | 语义 | 可编辑？ |
|---|---|---|
| **探索模式**（默认） | 数据图谱：实体=节点，关系=边 | 只能拖位置/折叠/筛选/标注，**不能增删节点与边** |
| **编排模式** | 导入管线图：解析器=节点，依赖=边 | **可编辑**，可保存、可复用、可批量跑 |

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

交互：滚轮缩放、空格拖拽、双击聚焦并展开邻居、`Shift` 框选、`F` 适应全屏、
`L` 自动布局（首次布局用 dagre/elk，之后**永不动**用户拖过的节点）。

**防视觉混乱三板斧**：① 卡片尺寸固定；② 超过 200 节点自动进"聚合视图"；
③ 布局持久化（图不会每次打开都乱跑）。

### 7.4 镜头页 + 两个治理页

- 镜头页：资产表格 / 产品与版本分布 / 接口树 / 流量时间轴 / 漏洞看板
- **数据体检页**：孤儿实体、冲突数、低置信度、未知字段、越界资产、归档体积、
  距上次备份天数、解析器夹具健康度
  （→ 数据层已提供 `integrity_check` / `orphan_subjects` / `purge_eligible` / `v_integrity_violations`）
- **Inbox 页**：嗅探不确定、版本冲突、疑似重复实体、未登记 host、越界资产 —— 清空即"干净"
- **首次打开的空状态**是这个 App 的门面：一张"从这里开始"的引导卡，而不是空白表格

### 7.5 性能预算

| 项 | 目标 |
|---|---|
| 画布节点数 | 默认渲染 ≤ 300；超过则聚合/分页；1000 节点保持 55fps |
| 表格 | 虚拟滚动，页大小 200，排序过滤在 SQL |
| 首屏 | < 1.5s（本地） |
| SSE 日志 | 每 100ms 批量推送 |
| `magpie app` RSS | < 120MB |

### 7.6 本地服务安全

- 只监听 `127.0.0.1`（**禁止 `0.0.0.0`**），随机端口 + 启动时生成一次性 token。
- 不开 CORS 通配；静态资源全部本地打包（**不引 CDN，保证零出站**）。
- `magpie app --no-browser` 供只想用 API 的场景。

---

## 8. 查询与搜索 ⬜ 未开工

1. **结构化过滤**（走索引）：`kind / status / review_status / in_scope / tag / severity / 时间范围`
2. **精确与前缀**（走索引）：域名、IP、端口、URL 前缀
3. **自由文本子串**：FTS5 trigram，3 字符以下降级 `LIKE` 并提示
4. **搜索语法**（GUI 与 CLI 共用同一解析器）：
   ```
   kind:product product:nginx version:<1.20 in:scope
   type:vuln sev:high state:confirmed
   lens:endpoints service:api.example.com method:POST
   ```
5. **增量 diff**：`since=last` 输出新增实体 / 新增观测 / 新增漏洞 / 失效 /
   新增 host（来自流量）

---

## 9. 性能与存储

### 9.1 实测基线（**2026-09-17 现场跑出，不是目标值**）

| 命令 | 结果 |
|---|---|
| `python check.py` | 79 passed + 23/23 + 27/27 |
| `MAGPIE_PERF_STRICT=1 python check.py bench` | 3 passed |

全量 recompute（`bench/test_perf.py`）：

```
N=1000: 0.046s   N=2000: 0.090s   N=4000: 0.188s   比值 = 1.95x, 2.08x（线性基准 2.00x）
48 us/row @N=2000     delta(K=500) = 0.069s @N=4000
```

对照 §9.2：1M 行 ≤ 60s 即 ≤ 60 us/row → 当前 **48 us/row 达标**；
单 job undo（K≤5000）≤ 1.5s → 当前 K=500 用 **0.069s 达标**。

> `bench/test_perf.py` 自己记录的旧基线是 N=4000 全量 **9.05s**（近似 O(N²)，
> 外推 1M 需 ~78 分钟）。现在同一规模 **0.188s**，约 **48 倍**提速，增长曲线已是线性。

### 9.2 契约（可验收的承诺）

| 指标 | 目标 | 现在 |
|---|---|---|
| 1M 行全量 recompute | ≤ 60s（≤ 60 us/row） | ✅ 48 us/row @N=2000（线性外推） |
| 单 job undo（K ≤ 5000） | ≤ 1.5s | ✅ 0.069s @K=500 |
| core 仅标准库 | 硬约束 | ✅ |
| `magpie app` RSS | < 120MB | ⬜ 服务未建 |
| 冷启动 | < 1s | ⬜ |
| 导入 10 万观测 | < 60s | ⬜ 管线未建 |

> **唯一的可验证形式是 `MAGPIE_PERF_STRICT=1`**（严格模式下性能断言不再"允许失败"）。
> 它现在的状态是**通过**，且能**单独触发**（`python check.py bench`）——
> 别把它埋进"跑全部"，否则"现在到底达不达标"就再也问不出来。

### 9.3 存储策略

- 归档策略：`archived`（小文件 gzip 进 `raw/`）/ `external`（大文件只记路径 + sha256）/ `purged`。
- **evidence 三级降级**（`full` / `locator` / `hash_only`）与能力矩阵已落地：
  "原文是否仍可取得" × "片段是否仍可定位" 的 12 种组合写死，未知组合按最保守处理。
- `magpie gc --raw-older-than 180d`；`doctor` 显示 `raw/` 占比。
- **备份**：`store.online_backup()`（`Connection.backup()`）已实现，`rekey` 已经用它；
  **WAL 下绝不要用复制文件的方式备份**（复制出来的库看起来正常但缺数据）。

---

## 10. 安全与合规

1. `.gitignore` 必须排除 `*.db` / `*.db-wal` / `*.db-shm` / `raw/` / `reports/`；fixtures 必须脱敏。
   ✅ 已落地（连迁移快照 `*.premigrate-*.db` 一起排除）。
2. **私密数据只存哈希**：`secret` 存值的 SHA-256 + 规则名 + 位置 + 掩码前缀，永不存明文。
3. **敏感串扫描**：导入时对归档内容做正则（`Authorization` / `Bearer` / `password=` /
   PEM 私钥 / 云 AK），**只计数、不回显原文**。
4. **导出默认脱敏**：`export` 默认剔除 headers/cookie 类字段，`--full` 才含原文。
5. **零出站网络**（CI 断言）+ 本地服务只绑 `127.0.0.1` + 一次性 token。
6. 建议数据库与 `raw/` 放加密卷；静态加密（SQLCipher）列为可选，不进 v1。
7. README 首段声明"仅用于自有或已获授权的目标"；`scope` 越界告警是它的技术配套。
   ✅ 已落地（`projects.scope` + `project_scope_history` + 越界进 Inbox）。

---

## 11. 测试策略

**已经在用的做法**（不是计划，是现状）：

| 层 | 内容 | 现状 |
|---|---|---|
| 不变量 | INV-01…17 逐条 + 跨项目写入必须被拒 + 复合 FK/触发器 | ✅ |
| 属性测试 | **1000 条固定种子随机操作序列**（INV-09/13/14/15，每步完整性检查 + 交错 merge/unmerge + 逆序 undo）+ 500 例 INV-04 等价性 | ✅ **不用 hypothesis**（离线装不上），改用固定种子 PRNG → 不 skip、失败可精确复现 |
| 端到端验收 | `verify_v32r2.py`（23 项）/ `verify_v32r3.py`（27 项） | ✅ |
| 性能 | `bench/test_perf.py`；`MAGPIE_PERF_STRICT=1` 转严格断言 | ✅ |
| 债务反例 | 每还一笔债先写一条**会失败**的反例，再修到绿 | ✅ `test_v32r4_debts.py` |
| 写入口 lint | 全仓扫描 `INSERT INTO contributions` 只允许在 `store.py` | ✅ |
| 迁移 | 幂等 / 老库采纳 / 已应用迁移被改动必须报错 | ✅ |

**还缺的**（随对应模块落地）：

- `kinds` / `rels`：每个 kind 的身份规则幂等、未登记 kind 必然被拒（依赖 §3.3 落地）
- `parsers`：黄金夹具 `tests/fixtures/<source>/<format_version>/` + `expected.json`
- `ingest`：两次导入观测数不变；`--dry-run` 不写库；取消后 `undo` 干净
- **负向夹具**：空文件 / 只有 BOM / GBK / CRLF+孤立 CR / 超长行 / NUL 字节 / zip /
  同名不同内容 / 损坏 JSON / HAR 里超大 body
- CI 断言：core 顶层不 import 第三方与 GUI
- API 契约：`openapi.json` 快照测试
- GUI：画布 1000 节点帧率、表格虚拟滚动、SSE 不断流（Playwright）

---

## 12. 迁移与版本 ✅ 已实现

- `schema_migrations(version, name, checksum, applied_at)` + `core/migrate.py`。
  `init_db()` 走迁移 runner，不再是直接 `executescript`。
- **已应用的迁移文件不可修改**：文件内容被改动 → 校验和比对失败 → 直接报错。
  （这是最容易犯也最难发现的错误：改了 `001_init.sql` 却以为新库和老库结构一样。
  真要改 schema，**新增一个迁移文件**。）
- **基线采纳**：由更早版本建出来的库（有 `contributions` 但没有 registry）会被
  自动登记为 v1，**不重跑 001**。
- **迁移前自动快照**：`migrate(snapshot=True)` → `online_backup(tag='premigrate-vN')`。
- 低版本 DB + 高版本程序 → 提示并迁移；高版本 DB + 低版本程序 → **拒绝启动**
  （registry 里出现未知版本即报错）。
- `kind_registry` 随迁移更新（待 §3.3 落地）。
- 语义化版本；`/api/v1` 与 `--json` 字段视为对外契约，变更需升 minor 并写 CHANGELOG。

---

## 13. 路线图（按 2026-09-17 的真实状态重排）

> **v3 的阶段 0"三根钉子"已经作废**：那三根钉子（建表 / 起服务渲染 1000 节点 /
> 格式摸底）里，**数据层那一根被钉得比设想深得多**，而另两根一根都没钉 ——
> 因为没有服务可用，格式摸底也没有解析器可以落地。
> 这不是失败，是顺序本来就该反过来：**数据层是唯一必须先立住的东西**（它决定
> 后面所有模块能不能"撤销、可解释、不丢数据"），而前端技术验证可以晚做 ——
> 它失败的代价只是"换回 Qt"，不影响已经写好的数据层。

### ✅ 阶段 0 · 工程收口（已完成）

`git init`（`.gitignore` 先落再提交）+ `README.md`（安全声明）+ `check.py`（单入口）。
**验收**：`python check.py` 一条命令全绿；`git log` 有首个提交；随便改一行能 `git checkout -- .` 回滚。

### ✅ 阶段 1 · 数据层（已完成，且超出 v3 的设想）

事实层 / 缓存层 / 身份层 / 工作层 / 审计层五层分离；`undo / redo / rekey / merge /
unmerge / compact` 六个事务型操作（全部支持 dry-run）；INV-01…17 全绿；
1000 条随机序列回归网；性能契约达标；迁移机制；**79 passed**。

**验收**：`python check.py` + `python check.py bench` 全绿。

### ▶ 阶段 2 · 垂直切片（**下一步，可以立即开始**）

> 闸门已开：`v3.2-design.md:1447` 的冻结声明要求 P0 全绿才解冻 P0 之外的能力。
> P0 已全绿，冻结期间点名"下一批第一项"的 redo 与性能**都已清**。
> 但**写入口唯一性不随解冻放开** —— 新解析器、新操作都只能经 `core` 的受控写入口。

1. **`cli.py`**：`init / import / ls / show / search / diff / undo / redo / doctor`，
   **全程 `--json`、非交互、有退出码表**。先做 CLI 而不是 GUI：它是 agent 的入口，
   也是最快能让数据层"被人用上"的路径。
2. **解析器**：先只做 `domainlist` + `httpx`（含 tech → product 实体 + runs 关系）。
   **同期必须补完两件事**（否则它们是凭空设计）：
   - **债 #6 的收尾**：`parser_profiles.definition_hash` 强制填写
     （"同一个 fingerprint 背后的实现有没有变过"目前答不出来）；
   - **§3.3 / §3.4 的受控词表**：kind 登记表 + 关系动词词表落成 `CHECK` + 身份规则 + 夹具。
3. **`ingest` + `pipeline`**（先跑通 YAML，不做可视化编辑）。
4. **`api`**：核心端点 + `/imports/{job}/events` SSE。
5. **GUI**：n8n 式外壳 + 资产表格 + 实体详情 + **只读图谱画布**（探索模式）。

**验收（正向）**：拖入真实 subfinder + httpx 输出 → 表格无重复 →
画布出现 domain→ip、service→product → `diff --since last` 正确 →
`undo` 干净回滚（→ `redo` 精确复原）→ CLI 全 `--json` 可被脚本消费。
**验收（负向）**：空文件 / GBK / 损坏 JSON / zip / 越界资产都不崩且有可读提示。

### 阶段 3 · 资源扩张

`nmap`（端口 + CIDR + CPE → product，中间件版本的主来源）→ `nuclei`（漏洞看板）→
`urls`（路径模板折叠）→ 接口（OpenAPI / Postman / HAR / curl 粘贴）→
服务与中间件镜头 → 体检页 + Inbox 页 → 编排模式 → 标签 / 批量改状态 / 合并实体 / 布局持久化。

### 阶段 4 · 深度整合

流量（HAR / tshark → 会话聚合 → 提取 endpoint/param/secret）→ 离线指纹引擎 +
`fingerprints/*.yaml` + 回归夹具 → 时间线 / 导出 / 打包 → 可选 extras。

### 阶段 5 · agent 整合

MCP（默认只读）+ `magpie schema --json` → "复制给 AI"上下文包 + `magpie ask`（可选、
显式开启出站）→ 知识库 → 风险评分与攻击面概览。

---

## 14. 风险与对策

| 风险 | 对策 | 现状 |
|---|---|---|
| **文档落后于代码 → 误导排期** | 排期前跑 `python check.py`；测试数与性能以命令输出为准；设计文档按 rN 追加修正记录 | ⚠️ **已经发生过一次**（见 §0），已修正并写进纪律 |
| 资源类型越加越多 → 模型与界面失控 | kind/rel 受控词表 + 登记流程 + 一份模型多镜头 | ⬜ 词表未落（§3.3），是阶段 2 的必交项 |
| 中间件版本以自由文本堆积 | 版本提升为 `product` 实体 + 别名/版本元组归一化 | ⬜ 随解析器落地 |
| 接口列表爆炸 | 路径模板折叠 | ⬜ |
| 流量把库撑爆 | 只存会话摘要 + 提取项；原始文件外部引用；明文凭据只存哈希 | 🟡 存储策略与 evidence 降级已落地 |
| 图谱变成一团毛线 | 固定卡片尺寸 + 200 节点以上自动聚合 + 布局持久化 + 默认按镜头过滤 | ⬜ |
| Web UI 内存超预算 | 预算拆两笔账（服务 < 120MB，浏览器另算）；可选 WebView2 薄壳 | ⬜ |
| 编辑画布导致脏数据 | 探索模式只读、编排模式才可编辑 | ⬜ |
| 解析器格式漂移静默缺数据 | 黄金夹具 + 未知字段探测 + 坏行明细 | ⬜ |
| 重解析冲掉人工成果 | `manual` 保护 + 回归测试 | ✅ 数据层已守 |
| 撤销撤销不干净 | 事实层无 DELETE，撤销=追加抑制事实；undo/redo 严格互逆 | ✅ 已有专项测试 |
| GUI/CLI/MCP 争写锁 | 写者唯一 + 服务内写队列 + `busy_timeout` | 🟡 `manual.py` 已是第一个实现 |
| agent 行为不可见 | `activity_log` + 冻结 action 命名空间 + 底部日志抽屉 | ✅ 数据层已守 |
| 前端越写越重 | 不引 CDN、不用 Electron、不用 WebEngine；静态资源本地打包 | ⬜ |
| **一次误操作毁掉全部数据** | 版本控制 + 迁移前快照 + WAL 下用 Online Backup 而非复制文件 | ✅ 已落地 |

---

## 15. 立即可开工的最小闭环

```
1. pyproject.toml（>=3.11, extras: api/api-yaml/traffic）
2. cli.py → init / import / ls / show / diff / undo / redo / doctor（全程 --json）
3. core/parsers/domainlist.py + httpx.py + tests/fixtures/
      └ 同期: parser_profiles.definition_hash 强制填写
      └ 同期: kind 登记表 + 关系动词词表落 CHECK + 身份规则 + 夹具
4. core/normalize.py（域名/IP/路径模板/版本，唯一入口）+ 幂等测试
5. core/ingest.py + core/pipeline.py（YAML 定义）
6. api/app.py + routes + SSE → magpie app
7. api/web：n8n 式外壳 → 表格 → 详情 → 画布（探索模式）
```

> 注意第 1 项已经是**唯一**还欠的工程前提（`check.py` 已经先顶上了"一条命令验证"）。

---

## 附录 A · 相对 v3 的变更

**修正（v3 与现实不符的部分）**

1. §2.3 目录结构按 2026-09-17 的实际内容重写；未建的模块明确标注"计划中"
2. §3 由"贴一份 DDL"改为"权威来源 + 现状"：**数据模型的权威是 `v3.2-design.md`**，
   计划书不再复制（v3 §3.4 那份 DDL 已被 v3.2 取代，留着只会制造第二份真相）
3. §9 由"目标值"改为"实测值 + 契约"，并写明唯一可验证形式是 `MAGPIE_PERF_STRICT=1`
4. §11 测试策略改为"已经在用的做法"与"还缺的"两栏
5. §12 迁移与版本标注为**已实现**，并补校验和与基线采纳纪律
6. §13 路线图按真实状态重排：阶段 0/1 已完成，阶段 2 是下一步；
   作废 v3 的"三根钉子"阶段 0 并说明理由
7. §3.3 / §3.4 明确标注 kind 词表与关系词表**尚未落地**，以及为什么延后到解析器

**新增**

8. §0「先读这一节」：逐条列出 v3 与现实脱节之处，以及**文档纪律**（排期前先跑一遍）
9. §5.5 撤销语义改写：v3 的"删该 job 的记录"→ **追加抑制事实**（事实层无 DELETE）
10. §5.3 解析器接口追加 `parser_fingerprint` / `definition_hash` 注册要求
11. §3.2 五层分层与三条硬线（含写入口 lint 测试）
12. §3.6 反混乱机制改成"谁在守"表，明确区分 ✅ 已落地 / 🟡 部分 / ⬜ 计划中
13. §14 新增两条风险：「文档落后于代码」「一次误操作毁掉全部数据」

**保留（v3 的产品蓝图部分，未改动）**

14. 定位与边界（§1）、技术栈与运行形态（§2.1/2.2/2.4）、资源整合逐类落地（§4）、
    导入管线（§5.1–5.4/5.6）、API/CLI 契约（§6）、GUI 设计（§7）、查询（§8）、
    安全合规（§10）、明确不做（附录 B）

## 附录 B · 明确不做（防膨胀）

Electron / QtWebEngine / QML / 引 CDN 资源 / 云同步 / 多人协作 / 权限体系 /
自动扫描与定时任务 / 目录监视轮询 / 插件系统 / 逐包流量入库 / 明文凭据入库 /
分布式多 writer / 跨项目共享实体 / 在 core 里发任何网络请求。

新功能要进来，先回答三问：**它会不会让 core 多一个依赖？让空跑多一个进程？
让网络多一次请求？** 再加一问（v3.2 起）：**它属于哪一层？破坏哪条 INV？如何补偿？**

## 附录 C · 文档纪律（本版新增）

1. **排期前先跑一遍**：`python check.py`。文档里的任何数字都不如命令输出可信。
2. **设计文档只增不改**：修正以 `§N. vX.Y-rN 修正记录` 追加；失效的旧陈述
   **就地标注"这是历史值"**，而不是删掉 —— 删掉就没法复盘"当时为什么那么排期"。
3. **计划书不复制权威内容**：数据模型看 `v3.2-design.md` 与 `schema/001_init.sql`，
   施工顺序看 `Magpie-搭建思路.md`，计划书只管**产品蓝图与路线图**。
4. **每还一笔债，先写一条会失败的反例**，再修到绿。反例是这笔债"真的还了"的唯一证据。
