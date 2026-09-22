# CHANGELOG

只记**对外契约**的变更：`/api/v1` 的响应形状、`--json` 的字段、CLI 的退出码。
内部重构、注释、测试增补不占篇幅（git log 记那些）。

版本号跟着 `pyproject.toml` / `magpie/__init__.py` 走。

> **0.6.0 说明**：3.1–3.5 的四步都是纯新增（新 kind / 新解析器 / 新过滤条件 /
> 新模块），没有动过任何既有响应形状，因此没有各自占一节。3.6 是阶段 3 里第一
> 次**动到既有响应**（给 `/doctor` 与 `magpie doctor --json` 各加一个键），
> 按本节开头的规矩在这里登记。

---

## 0.9.0 — 4.1 流量：`flow` 会话摘要 + `storage='external'` 归档路径

**一个新 kind（`flow`）+ 一条新关系动词（`involves`）+ 两条新归档语义 +
迁移 011。** 新增两个来源（`tshark`，以及 `har` 的**第二半**——它从 3.4 起就在
产接口形状，这一步起它同时产会话），外加一张既有表的重建。

本步动到既有契约的地方有四类，逐条登记（按本节开头的规矩）：

### 对外契约（改动需升 minor 并在此登记）

**1 · `magpie import --json` 的报告多一个键 `custody`**（`"stored"` /
`"external"`）。

它不是装饰：`--archive external` 的导入以前在报告里与归档导入**长得一样**
（都是 `storage_state: "present"`），而两者的可恢复能力**不是一件事**（下面第
4 条）。这一个键是把那件事说出来的地方。`storage_state` 的取值域没有变。

**2 · `GET /api/v1/doctor` 的 `raw_files` 每一项多一个键 `custody`**
（并因此**可能多出几行**：分组键从 `storage_state` 变成了
`(storage_state, custody)`）。

体检页那排 chip 的标题是「原文持有」，而"谁持有"是 `custody` 回答的问题 ——
只按 `storage_state` 分组会让外部文件显示成 `present`，一个读起来像"副本在我
手里"的词，而它恰恰不在。GUI 的模板这一轮**没有跟着改**（前端产物没有重新
构建），所以今天看到的是"两种 `present` 各一行、标签相同"；`custody` 已经在
JSON 里，前端下一次构建时带上它即可。

（`magpie doctor --json` **不受影响**：它的信封里本来就没有 `raw_files` 这一项
—— 那一项是 `/doctor` 独有的。CLI 那一侧这一步改的是 human 输出里的措辞
"五类问题" → "六类问题"，数字与分类都跟着 `HEALTH_CATEGORIES` 走。）

**3 · `magpie schema --json` / `/api/v1/schema` 的两个清单各多一项**：类型
（kind）里多了 `flow`（镜头 `traffic`），关系动词里多了 `involves`（逆
`involved_in`）。`inbox` 的 kind 多了一个取值 `unregistered_asset`，体检报告
（`/api/v1/health`、`magpie doctor --json`）因此多出**第六类**
`unregistered`（"未登记资产"）。

这一条是**加法**里的"看得见的那一半"：`flow` 会出现在类型清单里、`involves`
会出现在关系动词清单里、体检页多一张卡片。清单本身是数据驱动的
（`query.HEALTH_CATEGORIES` / `kind_registry`），所以三个消费者（GUI / CLI /
MCP）看到的是同一份。

**4 · 能力判定的取值域多一档 `locator_only`**，且
`evidence_capability_report()` 多两个字段（`custody` / `external_not_deletable`）。

`locator_only` 只出现在 `custody='external'` 的行上：

> 可凭 locator 指向的**外部**路径读取原文并比对 hash；副本不在本工具手里 ——
> 那个路径还在不在，由工具之外的世界决定。

`external` 的行**不沿用** `stored` 的结论（`recoverable`）—— 那是 4.1 点名的
一条：本工具手里没有副本，就不能替那个文件担保。`stored` 那一侧的 3×4 矩阵
**一个字没动**（`verify_v32r3.py` 的 ①b 仍是原来那 12 条）。

### 行为变更（不是新增，是"以前会做错"）

**5 · `ops.purge_raw_file()` 对 `custody='external'` 的行**抛
`InvariantError`，不再更新任何一行。

以前它会照常把 `storage_state` 置成 `purged` —— 而"purged"在这里是一句假话：
本工具从不删除任何文件，而外部文件连"本工具归档的副本"都不是，它还在原处。
**"清除"这个词对外部文件没有对应的动作**，所以现在拒绝，并在报错里说清真要
放弃跟踪该走 `ops.degrade_evidence(..., to_state='hash_only')`（那一步有真正的
对应动作：丢掉本工具的指针）。

这一条没有 CLI / API 调用方（`gc` 走的是另一条路：`purge_eligible` 判的是
缓存层对象），所以它今天影响的是程序化调用与测试；写进 CHANGELOG 是因为它是
**既有函数的行为变更**。

### 数据库

**6 · 迁移 011**（`schema/011_flow_external.sql`）：登记 `flow` / `involves`、
给 `raw_files` 加一列 `custody`、给 `inbox_items.kind` 加第八个取值
（**重建整张表** —— SQLite 改不了 CHECK，配方与实测踩到的两个坑写在文件头）。

`custody` 的默认值是 `stored`，**升级前的行一律留在默认值上**：那是
`--archive external` 被记成 `present` 的那个年代留下的信息缺口，库里没有记过
"这份原文在谁手里"，能拿来判的只有 locator 的形状，而那是猜。这句话写在迁移
的文件头里，比一个猜出来的回填诚实。

---

## 0.8.0 — 3.8 标签 / 批量改状态 / 合并实体 / 布局持久化

**四件写操作 + 迁移 009 + 前端探索模式补齐。** 对 `magpie/core` 的改动是
**一张新表 + 三处追加**（`ops.set_lifecycle`、`ops` 的 `undo` / `redo` 各多一段
工作层回放、`query` 里三个新的读）。

四条落点各有各的出处，先说清楚，免得读者以为"探索模式怎么多出了写入口"：

| 写什么 | 走哪条路 | 依据 |
|---|---|---|
| 标签（增 / 删） | **不变**，仍是 `PATCH /entities/{id}` | 3.7a §3.2 冻结表 |
| 位置 / 折叠 | **不变**，仍是 `PATCH /graph/layout`（这次只加了可选的按列写入） | 3.7a §3.2 冻结表 |
| 合并 / 拆开 | **新** `POST /entities/{id}/merge`（已有）与 `/unmerge`（新） | LARI-31 的裁定：必须走 `ops.merge` / `ops.unmerge` |
| 批量改状态 | **新** `POST /entities/lifecycle` | LARI-31 的裁定：复用同一套 job/undo |

后两条**明确扩写了 3.7a §3.2 的冻结表**（见 `Magpie-编排模式设计冻结.md` 那一行
的扩写说明）—— 不是偷偷加一个写入口。

### 对外契约（改动需升 minor 并在此登记）

**1 · `POST /jobs/{id}/undo` 的 `impact` 多一个键 `work_reverted`，
`POST /jobs/{id}/redo` 的 `impact` 多一个键 `work_replayed`**（都是整数）。

这是本步唯一一处**改到既有响应形状**的地方。来源：`ops.undo` / `ops.redo` 现在
除了事实层 / 身份层 / inbox，还会把该 job 写下的 `work_journal`（工作层的人工
状态）放回去或重放，而那一半的**明细**在 `activity_log`（`work.revert` /
`work.replay`），`impact` 里只放一个计数。

```json
"impact": { "contributions": 0, "superseded": 0, "…": "…",
            "work_reverted": 3 }
```

判据不变，仍是 2.6 的 B 组：**逐行指纹相等**（`store.effective_equivalent`，
它本来就覆盖 `entity_layout` / `entity_tags`）。

**2 · 新增四条路由：**

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/entities/lifecycle` | 批量改状态。`{entity_ids, lifecycle, reason?, dry_run?}` |
| `GET`  | `/api/v1/entities/{id}/merges` | 这个实体参与过的**仍然生效**的合并事件（`unmerge` 的入口） |
| `POST` | `/api/v1/entities/{id}/unmerge` | 拆开一次合并。`{event_id, dry_run?}` |
| `GET`  | `/api/v1/tags` | 标签词表（**只算活标签**）+ 每个标签的实体数 |

`POST /entities/lifecycle` 一次调用 = **一个 job**（不是 N 个）：
`entity_ids` 整批在一个事务里，要么全成要么全不成。为什么是这个形状，
写在 `ops.set_lifecycle` 的段落注释里；它对外的可见后果是**撤销就是那一个
job 的撤销按钮**（`POST /jobs/{id}/undo`），与作业历史页上的其它按钮同一条路。

**3 · `PATCH /graph/layout` 的请求体放宽：`x` / `y` / `collapsed` 都变成可选**
（至少给一个），**响应形状逐字不变**（仍是 `{"stored": N}`）。

旧形状（三个都送、`collapsed` 缺省 `false`）是**整行覆盖写**，而前端拖拽时恰好
固定补一个 `collapsed: false` —— 于是"拖一下已经折叠的节点"会把它悄悄展开。
**老客户端不受影响**（它照旧三个都送，行为逐字不变）。

新增一个 `400 E_LAYOUT_NEEDS_POSITION`：对一条**还不存在**的布局行只写
`collapsed` 会被拒（它需要 `x` / `y`）。放它过去的话那行会带着 `(0,0)` 出生，
读侧把它当成"人把这个节点拖到了原点"，节点下次打开就跳到左上角。它在**排队
之前**就拒（而不是走到写者里抛 `InternalError` → 500）。

**4 · 新增两个错误码**：`E_MERGE_EVENT_NOT_FOUND`（404，路径上的实体没有参与
过这个合并事件）、`E_LAYOUT_NEEDS_POSITION`（400）；批量改状态的词表错误复用
`E_BAD_LIFECYCLE`（400），空批复用 `E_EMPTY_PATCH`（400），跨项目与不存在复用
`E_ENTITY_NOT_FOUND`（404）。

**5 · schema 迁移到 009**（`schema/009_work_journal.sql`：一张新表
`work_journal`）。`magpie schema --json` 的 `schema_version` 与 `applied` 因此
变成 9。

### 数据库层面新增的能力

* `work_journal` —— 工作层写入（今天只有 `entities.lifecycle`）的**撤销日志**。
  它不是审计：`activity_log` 回答"谁在什么时候改了什么"，这张表回答
  "**改之前是什么**"，而后者正是 `ops.undo` 唯一的输入。两者混在一起的话，
  一次审计清理就会让撤销开始说假话。

### 前端（构建产物已随包入库，`magpie app` 不需要用户装 node）

* 详情面板：标签可以**删**了（3.8 之前只有加，而 `tag_events` 的 remove 那一半
  一直没人调用）；标签输入框接 `GET /tags` 的词表；多了"状态"与"合并"两节。
* 画布：**`Shift` + 单击**批量选中 → 右上角出现批量条（标为在用 / 已退休）。
  选中的集合只有**一份**（`store.selection`），合并的"并入这里"用的也是它。
* 实体卡片：多一个**折叠**按钮（`emit` → `GraphCanvas` → `store.toggleCollapse`）。
  折叠是渲染层的事：卡片淡下去、连着它的线也淡下去，一个事实都不动。
* 折叠状态**终于会落库**了。3.8 之前 `toggleCollapse` 只改本地 `_raw.layout`
  就返回 —— 于是"图每次打开都长得一样"（§7.3 第 ③ 条）只在位置上成立。

### 与 3.7b 的关系（一句话）

批量改状态**没有**另造一套批量机制：它调用的仍然是 `ops.undo` / `ops.undo_jobs`
那一份实现，只是它的"一批"是**一个 job 里的 N 个实体**，而不是 N 个 job。

---

## 0.7.0 — 3.7b 编排模式（可编辑管线画布：可保存 / 可复用 / 可批量跑）

**十条新路由 + schema 迁移 008 + 前端第二个模式。** 对 `magpie/core` 的改动
几乎全是追加：新函数（`pipeline.normalize_definition` / `upgrade_definition`、
`ops.save_pipeline` / `start_pipeline_run` / `finish_pipeline_run` / `undo_run`
/ `redo_run`、`query.pipelines` 等），以及 `check.py` 多了一条静态红线
（10 → 11 条）。

### 对外契约（改动需升 minor 并在此登记）

**1 · `/api/v1` 的 `/jobs/{id}` 的 `data` 多一个键 `pipeline_run_id`。**

这是本步唯一一处**改到既有响应形状**的地方。来源是迁移 008 给 `jobs` 加了
`pipeline_run_id` 列，而 `query.job_row` 用的是 `SELECT *` —— 那个函数返回什么
列，接口就多什么键。值多为 `null`（用 `magpie import` / GUI 导入建出来的作业
不属于任何一次批量跑）。

```json
"job": { "id": 3, "project_id": 1, "kind": "ingest", "...": "...",
         "pipeline_run_id": 7 }
```

`query.JOB_COLUMNS`（**列表**用的那一串显式列）**不加**它 —— 作业列表不需要它，
而 `SELECT *` 的顺序不稳定的坑已经记在那个常量上面了。

**2 · 新增十一条路由**（3.7a §4.5 冻结的形状，此前一律 501 `E_NOT_IMPLEMENTED`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET`    | `/api/v1/pipelines` | 定义列表（含各自最近一次 run 的摘要） |
| `POST`   | `/api/v1/pipelines` | 建定义（body = 规范形态的 JSON） |
| `GET`    | `/api/v1/pipelines/{id}` | 定义 + `definition_hash` + `revision` |
| `PUT`    | `/api/v1/pipelines/{id}` | 保存（body 带 `revision`，不匹配 → 409） |
| `POST`   | `/api/v1/pipelines/{id}/archive` | 归档（软删，**不提供 DELETE**） |
| `POST`   | `/api/v1/pipelines/{id}/run` | `{dry_run, base_dir?, data_dir?}` |
| `GET`    | `/api/v1/pipelines/{id}/runs` | 这个定义的运行历史 |
| `GET`    | `/api/v1/runs/{run_id}` | 单个 run（含完整报告） |
| `GET`    | `/api/v1/runs/{run_id}/events` | **SSE**：这次批量跑的进度（流 id 是 `run-{id}`） |
| `POST`   | `/api/v1/runs/{run_id}/undo` | `{dry_run}` |
| `POST`   | `/api/v1/runs/{run_id}/redo` | `{dry_run}` |

两条 `run` 的形状**刻意不同**，因为两件事本来就不一样：

* **真跑 → `202` + `{run_id, state, events}`**，跑在写线程里异步进行，进度走
  SSE。它可能跑几分钟，报告落在 `pipeline_runs.report` 里等着。
* **干跑 → `200` + `{run_id: null, state, report}`**，**同步**返回完整报告。
  干跑的硬性标准是"跑完一行都不留"（冻结 F10：它**不写 `pipeline_runs` 行**），
  所以它没有 run_id 可以订阅、也没有东西可以事后撤销 —— 同步返回是"报告照旧
  交付给调用方"唯一说得通的形状。与 `/jobs/{id}/undo` 的 `dry_run=true` 同形。

**3 · 新增三个错误码**：`E_PIPELINE_NOT_FOUND`（404）、
`E_PIPELINE_RUN_NOT_FOUND`（404）、`E_PIPELINE_CONFLICT`（409）；定义本身的
校验错误一律 `400`，`code` 复用 core 的（`E_PIPELINE_BAD_DEFINITION` /
`E_PIPELINE_BAD_FORMAT` / `E_PIPELINE_FORMAT_TOO_NEW` / `E_SOURCE_NOT_FOUND`）。

**4 · schema 迁移到 008**（`schema/008_pipeline_definitions.sql`：两张新表
`pipeline_definitions` / `pipeline_runs` + `jobs.pipeline_run_id` + 一个跨项目
触发器）。`magpie schema --json` 的 `schema_version` 与 `applied` 因此变成 8。

**5 · 管线定义有了规范形态**（`format_version: 1`）。`source` 从"一个字符串"
变成"一个带 `kind` 的对象"（`file` / `glob` / `dir`），来源名搬到 `source_name`
—— 旧写法（`source_glob` + 字符串 `source`）读得懂并被归一化，但**只写了
`source: "名字"` 这一种会报错**（它有两种读法，而"猜"在这里没有依据）。这条
影响 `magpie pipeline run <yaml>` 的**报告**：每个 step 的 `source_glob` 键变成
`source`（对象），并新增 `source_name` 与顶层 `base_dir`。

### 数据库层面新增的能力

* `pipeline_definitions` —— 一条**可复用的管线定义**。它**不是事实**：保存 /
  改名 / 归档不建 job、不产生贡献、不参与 undo / redo（3.7a 冻结 F4）。
* `pipeline_runs` —— 一次"批量跑"。**1 个 run + N 个 job**（job 的粒度不变，
  永远是一个文件一个 job）：run 是**分组**，不是新的一种 job。

### 前端（构建产物已随包入库，`magpie app` 不需要用户装 node）

* `VIEWS` 多一个 `'pipeline'`（编排模式）—— 它只是 `store.view` 的一个新取值，
  **没有第二套路由状态**。
* 新画布 `PipelineCanvas.vue` 是**受控**的（图完全由 `store.pipeline.definition`
  算出来），且**手势只表达依赖（边）**：增删步骤是表单动作。
* 探索模式**一个字没改**：`GraphCanvas.vue` 仍然 `nodes-connectable=false`、
  没有 `Handle`、没有连线回调。它的根元素与编排画布一样带 `data-mode`，两条
  一起构成"这两种模式真的是两个模式"的可断言形式。

### `check.py` 的红线（10 → 11 条）

`READONLY` 从"`frontend/src/**` 里不许出现 18 个名字"**收窄**成"……**除了**
一个文件里的一个名字"（`_EDITABLE_CANVAS`），并配一条新的
`READONLY-REVERSE`：白名单每一格必须指向一个真存在的文件、实际命中的名字必须
⊆ 声明的名字、**声明的名字必须都真的被用到**（这一条防的是"开一个洞，然后把
新代码都搬进洞里"）、探索模式那一路的文件清单逐个存在且命中 0 个。**只收窄，
不删除** —— 删掉 READONLY 等于把"探索模式只读"这条不变量丢掉。

---

## 0.6.0 — 3.6 数据体检页 + Inbox 页（五类问题可见）

**纯新增**：`/api/v1` 没有任何既有字段被改名、改类型或删除；`magpie doctor`
同理。对 core 的改动全是追加（新模块 `magpie/core/scope.py`、`query` 里新增
具名查询与生产者）。

### 对外契约（改动需升 minor 并在此登记）

**1 · `GET /api/v1/doctor` 的 `data` 多一个 `health` 键**（只在 `?project=` 解析
成功时出现；解析不了时**整个键不出现**，与既有的 `project_id` /
`untested_entities` 同一条规则 —— 让"没算"和"算出来是 0"分得开）。

```json
"health": {
  "query": "health", "project_id": 1,
  "categories": [ { "query": "health.orphan", "key": "orphan", "label": "孤儿",
                    "why": "...", "kinds": ["orphan_subject"],
                    "count": 2, "items": [ /* inbox 项 */ ], "truncated": false } ],
  "claimed": 2, "open_inbox": 2, "unclaimed": [], "notes": []
}
```

五类是**未决 Inbox 项按 kind 的划分**（`magpie/core/query.py` 的
`HEALTH_CATEGORIES`，唯一声明处）：**清空 Inbox ⟺ 五类全部归零**，这就是
§7.4 的"清空即干净"。

**2 · `magpie doctor --json` 的 `data` 多一个同形的 `health` 键**，规则同上
（`--project` 解析不了时键不出现；`--human` 会多一节"五类问题"）。

**3 · 新增两个 Inbox 生产者**（这是"未知字段"与"越界资产"两类**第一次**有
东西可数 —— 在此之前 `evidence_missing` / `scope_drift` 两个 kind 在 001 的
CHECK 里躺着但没有任何一处写它们，那两个计数因此恒为 0）：

| 何时开 | kind | payload | severity |
|---|---|---|---|
| 解析器报出未知字段名（§5.6） | `evidence_missing` | `{reason:"unknown_field", field, count, file, parser}` | `low` |
| 资产落在项目 scope 之外 | `scope_drift` | `{reason:"out_of_scope", entity_key, kind, scope}` | `medium` |

两者都只在**真的有东西要报**时才写：干净的夹具上一条不落。它们与既有
`value_conflict` / `orphan_subject` / `manual_required` / `merge_conflict`
一样是 `created_by_job_id` 锚定的自动项，所以 `undo` 会连同它们一起关掉
（`verify_v32r5` 的 B1 段在验这一条）。

**4 · scope 求值语义**（`magpie/core/scope.py`，`?in_scope=` **仍然拒绝** ——
本步只把它用在体检与 Inbox 上，没有开过滤条件）：

  * `*.example.com` 读作 **`example.com` 及其全部子域**（故意不同于 fnmatch 的
    字面语义：解析器会从子域自动推出 apex，字面读法会让每次导入都多报一条
    apex 越界 —— 那是噪音，不是发现）；
  * 名单里**一个 CIDR 都没有**时，`ip` / `cidr` 实体**不判**（`in_scope` 回
    `None`）—— 一个只写了域名的 scope 并没有对地址表态，而 httpx 会为每台在
    范围内的主机写一条 A 记录；
  * include 非空即白名单，为空表示没有约束；exclude 优先于 include；
  * 大小写折叠后再比，且**不用 `fnmatch.fnmatch`**（它在 Windows 上会两边
    casefold，让同一份 scope 换台机器给出两套答案）。

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
