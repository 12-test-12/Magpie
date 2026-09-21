# Magpie 编排模式设计冻结（3.7a）

> **这一步只出设计，不写实现。** 3.7b（`LARI-30`）按本文实现；实现期**不得擅自
> 改动下面标了【冻结】的条目** —— 要改先回 `LARI-17` 把理由说清楚再动手。
> 【留给 3.7b】的那些是刻意的空白，见 §7。
>
> 写这份冻结时的事实基线：`151ca87`（3.1–3.6 全部落地，工作区干净，`check.py`
> 10 条红线全绿，版本 `0.6.0`，迁移到 `007`）。设计建立在**现在已经有的东西**
> 上，不建立在一份想象中的代码上。
>
> 本文与 `v3.2-design.md` 同级：那一份冻结的是**数据层**，这一份冻结的是
> **画布的第二模式**。

---

## 0. 为什么 3.7 要拆成 3.7a / 3.7b

3.7 是整个阶段里唯一一处把画布从只读变成可编辑的改动，同时引入一个新的持久化
产物（管线定义）。这两件事如果让实现者边写边定，返工的不是一个文件，而是
**存储 + API 契约 + 前端状态机**三处同时返工，而且发现得最晚 —— 因为三处各自
都"能跑"。

对比：阶段 2 的 2.4 之所以一次做对，是因为词表与 `definition_hash` 在开工前就
被钉成了"同期必交项"。这里用同一个办法：**先把定义格式、模式状态机、写入口对接、
红线收窄方案、undo 语义五件事钉死，再动手。**

这五件事里有一件是硬骨头：`check.py` 的 `READONLY` 红线。加上可编辑画布之后它
必然变红，而**不许删掉它** —— 删掉等于把"探索模式只读"这条不变量丢掉，而那是
2.8 的核心承诺，也是这个工具面向自用场景时唯一的安全边界。所以 §6 给出了它的
**收窄方案**，并且落成一段可执行的规则规格。

---

## 1. 冻结项总表

| # | 冻结项 | 落在哪 | 章 |
|---|---|---|---|
| F1 | 管线定义的规范形态：`format_version` + `source` 定位符对象 + `base_dir` | `core/pipeline.py` 的 `normalize_definition` | §2.2 |
| F2 | 定义存 `pipeline_definitions`（迁移 **008**），canonical JSON + `definition_hash` + `revision` 乐观锁 | `schema/008_*.sql` | §2.4 |
| F3 | 缺 `format_version` 的库内定义 = 错误；版本只升不改，**读时升级、写时只写新版本** | §2.5 | §2.5 |
| F4 | **定义不是事实**：保存定义不建 job、不产生 contribution、不参与 undo/redo | §2.6 | §2.6 |
| F5 | 模式 = `store.view` 的一个取值 `'pipeline'`，**不新增第二套路由状态** | `frontend/src/store.js` | §3.1 |
| F6 | 编排画布是**受控**组件：定义是唯一事实来源，画布不持有自己的图状态 | §3.1 | §3.1 |
| F7 | 画布手势只表达**依赖（边）**；增删步骤是表单动作，不是画布手势 | §3.2 | §3.2 |
| F8 | 切换模式**保留**草稿，不做阻塞确认；换项目**丢弃**草稿 | §3.3 | §3.3 |
| F9 | 批量跑 = **1 个 run + N 个 job**（job 的粒度不变，永远是"一个文件一个 job"） | §4.3 | §4.3 |
| F10 | 干跑的事务边界 = **一条命令**，复用 `ingest.dry_run_scope`；**干跑不写 `pipeline_runs`** | §4.4 | §4.4 |
| F11 | `undo` 的唯一可逆原语仍然是 **job**；`undo_run` = 逆序逐个 undo，一份实现 | §5 | §5 |
| F12 | `pipeline_runs.applied` 与 `jobs.applied` 同一个词、同一个语义（与 `state` 分离） | §5.2 | §5.2 |
| F13 | `READONLY` 改成**文件级 × 名字级**白名单；配三条反向断言 | `check.py` | §6 |
| F14 | `GET/POST /pipelines`、`POST /pipelines/{id}/run` 从 501 变成真端点 | `api/routes.py` | §4.5 |

---

## 2. 管线定义：格式、归一化、存储

### 2.1 先修一个真实存在的歧义

今天 `core/pipeline.py` 的 step 用两个键表达两件事，而它们的名字互相打架：

```python
# magpie/core/pipeline.py:254（现状）
pattern = str(step.get("source_glob") or step.get("source") or "")
matched = sorted(base.glob(pattern)) if pattern else []
```

`source_glob` 是"**导入什么**"，`source` 是"**来源名**"（它被原样传给
`IngestOptions.source_name`，写进 `sources` 表）。但这一行做了 fallback：
**一个只写了 `source: subfinder` 的 step，会被当成 `glob("subfinder")` 去扫盘。**
它扫不出东西，然后报告说"`source_glob 'subfinder'` 没有匹配到任何文件" ——
报的是一个用户根本没写过的键名。而 `load_definition` 是**放行**这种写法的
（它只要求 `source_glob` 和 `source` 里至少有一个非空）。

这不是理论问题：`bench/e2e/pipeline.yaml` 里两个 step 都**同时**写了 `source` 与
`source_glob`，纯粹因为不这么写就不知道会怎样。**一个让人必须猜的格式，定义不了
一条可复用的管线。**

【冻结 F1】`source` 从此**只表示"导入什么"**，来源名是 `source_name`。
`source` 是一个**带 `kind` 的对象**，而不是一个字符串 —— 因为"路径 / glob / 目录"
这三种东西的行为**本来就不一样**，用一个字符串表达它们，就必须靠猜（有没有
`*`？是不是目录？）来区分，而猜错的那一半不会有任何症状。

### 2.2 规范形态（canonical form）

```json
{
  "format_version": 1,
  "name": "常规一轮（域名 + 指纹）",
  "project": "演示项目",
  "base_dir": "D:\\Magpie\\bench\\e2e",
  "data_dir": "_scratch/magpie-data",
  "scope": { "include": ["*.example.com"], "exclude": [] },
  "steps": [
    {
      "id": "dns",
      "source": { "kind": "glob", "pattern": "recon/subfinder.txt" },
      "source_name": "subfinder",
      "parser": "domainlist",
      "after": [],
      "authority": "unverified",
      "archive": "archived",
      "batch_size": 500,
      "dry_run": false
    },
    {
      "id": "probe",
      "source": { "kind": "glob", "pattern": "recon/*httpx*.jsonl" },
      "parser": null,
      "after": ["dns"]
    }
  ]
}
```

**`source.kind` 的三种取值与各自的行为**（这是本节最要紧的一张表 —— 三种东西
在三处（匹配、找不到时怎么办、discover 的递归）都不同）：

| `kind` | 语义 | 匹配 0 个 | 匹配 >1 个 |
|---|---|---|---|
| `file` | **指名道姓的单个文件** | **硬错误**，跑之前就报（`E_SOURCE_NOT_FOUND`） | 不可能（`pattern` 里没有通配符；有就是写错了，报错） |
| `glob` | 通配模式，0..N | 报告里的 `problems` + `dependency_empty`（**保持今天的行为**） | 正常，逐个导入 |
| `dir` | 目录，递归发现 | 报告里的 `problems` | 正常 |

`file` 的"找不到就硬错误"是从 2.7 抄来的同一条纪律：`POST /imports` 对不存在的
路径**在提交之前**就报 `E_PATH_NOT_FOUND`，理由是"路径写错了"不该变成一个通过了
HTTP、却在报告里写'没有匹配到任何文件'的 job。**`file` 的语义就是"我指名道姓要
这个文件"，它不存在是配置错误，不是一次空导入。**

**旧形态的读法**（向后兼容，只有这一处）：

| 旧写法 | 归一化成 |
|---|---|
| `source_glob: "recon/*.txt"` + `source: "subfinder"` | `source: {kind:"glob", pattern:"recon/*.txt"}`、`source_name: "subfinder"` |
| 只有 `source_glob` | 同上，`source_name: null` |
| 只有 `source: "<字符串>"` 且没有 `source_glob` | **报错** |
| `source: {kind, pattern}`（新形态） | 原样 |

最后一行是要点：`source: "subfinder"` 单独出现时，它**可能是来源名、也可能是一个
路径**，两种读法给出两个不同的结果，而"猜"在这里没有依据。报错信息要给出出路：

```
step dns: 只写了 source: "subfinder"，读不出它是什么。
    · 它是"导入什么" → source: {kind: "file"|"glob"|"dir", pattern: "recon/*.txt"}
    · 它是"谁给的"   → source_name: "subfinder"
```

**`base_dir`（定义级，一个新字段）** —— 相对 pattern 的基准。今天它是"定义文件
所在的目录"（`run_pipeline` 的 `base` 缺省值），而**库里的定义没有"所在目录"**。
如果不把它显式化，"同一份定义从 YAML 跑和从库跑会扫两个不同的目录"，而这种分歧
没有任何症状。

【冻结】`base_dir` 写在定义级（不是每个 step 一个）：文件入口缺省 = 定义文件所在
目录（**与今天逐字相同**）；库入口缺省 = 项目 `data_dir`。并且 —— **保存进库时，
`base_dir` 一律解析成绝对路径写进定义**。理由：那时它是"这份定义在说什么目录"的
唯一记录，换一个入口不该换一个答案。绝对路径在这里不是缺陷（`raw_files.locator_uri`
本来就存绝对/相对路径，这个工具本来就是本机的）。

> **被否掉的替代方案**：把 `source` 做成一个**独立的节点**（画布上"来源节点 →
> 解析节点 → 落库节点"三节）。否掉的理由有两条，第二条是决定性的：
> （1）解析器是**仲裁**出来的（`sniff_rank` / `arbitrate`），把它画成一个可以
> 被单独接线的节点，是在暗示"它可以被换掉/重接"，而它其实只是 step 的一个属性；
> （2）**"落库"不能是一个节点** —— 它必须是那条唯一的写入口（R1）。画布上多一个
> 能连到"落库"的节点，就是多一条通往写入口的路，而那正是 R1 要防的东西。
> 所以：**一个 step 就是一个节点**，来源 / 解析 / 落库是这一个 step **内部**的三个
> 阶段（§5.1 的 12 步），不是三种节点。

### 2.3 归一化只有一份实现

`pipeline.normalize_definition(raw, *, project=None, base_dir=None) -> dict`。

两个载入器（YAML 文件、库里的行）**都只调它**，谁都不许自己拼一遍。这条不是洁癖：
"同一个定义、两种读法"和 3.5 收敛掉的"同一份数据、两份 SQL"是同一类缺陷 ——
两边都跑得通，只是答得不一样。3.5 为此立了 `SQL-FREE` 红线，这里配一条**测试**：

> **A6**：同一份定义分别从 YAML 与从库载入，`normalize_definition` 的输出
> `json.dumps(..., sort_keys=True, ensure_ascii=False)` **逐字节相等**。

`normalize_definition` 的输出是**规范形态**（§2.2）。它顺带承担校验：`id` 唯一
（按 `str` 比较，`1` 与 `"1"` 是同一个 —— 这是 2.5 修过一次的坑）、`after` 引用的
步骤存在、无环（`order_steps` 已有，直接调）。**归一化出来的一定是能跑的**，
把"能跑"这件事从运行期提前到载入期。

### 2.4 存储：迁移 008

【冻结 F2】新增 `schema/008_pipeline_definitions.sql`，**只新增不改旧**（R4）。
内容 = 两张表 + 一个 `ALTER TABLE jobs ADD COLUMN` + 一个跨项目触发器。

```sql
-- 008_pipeline_definitions.sql（规格，不是最终文本）

CREATE TABLE pipeline_definitions (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    name            TEXT    NOT NULL,
    format_version  INTEGER NOT NULL CHECK (format_version >= 1),
    definition      TEXT    NOT NULL,      -- canonical JSON（§2.2）
    definition_hash TEXT    NOT NULL,      -- sha256(canonical JSON)
    revision        INTEGER NOT NULL DEFAULT 1,   -- 乐观锁：保存带 revision
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    updated_by      TEXT    NOT NULL,
    archived_at     TEXT,                  -- 软删；**不提供 DELETE**
    UNIQUE (project_id, name)
);
CREATE INDEX ix_pipeline_defs_project ON pipeline_definitions(project_id, archived_at);

CREATE TABLE pipeline_runs (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    pipeline_id     INTEGER NOT NULL REFERENCES pipeline_definitions(id),
    definition_hash TEXT    NOT NULL,      -- 跑的是**哪一版**定义
    format_version  INTEGER NOT NULL,
    applied         INTEGER NOT NULL DEFAULT 1 CHECK (applied IN (0, 1)),  -- F12
    state           TEXT    NOT NULL DEFAULT 'running'
                    CHECK (state IN ('running','done','failed','partial','aborted')),
    step_count      INTEGER NOT NULL DEFAULT 0,
    job_count       INTEGER NOT NULL DEFAULT 0,
    report          TEXT,                  -- 报告原样留一份（见下）
    actor           TEXT    NOT NULL DEFAULT 'gui',
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    UNIQUE (id, project_id)
);
CREATE INDEX ix_pipeline_runs_pipeline ON pipeline_runs(project_id, pipeline_id, id);

ALTER TABLE jobs ADD COLUMN pipeline_run_id INTEGER REFERENCES pipeline_runs(id);
CREATE INDEX ix_jobs_pipeline_run ON jobs(pipeline_run_id);

-- 跨项目兜底：挂在 A 项目的 run 上的 job，必须也是 A 项目的
CREATE TRIGGER trg_job_run_same_project
BEFORE UPDATE OF pipeline_run_id ON jobs
WHEN NEW.pipeline_run_id IS NOT NULL
 AND (SELECT project_id FROM pipeline_runs WHERE id = NEW.pipeline_run_id) <> NEW.project_id
BEGIN
    SELECT RAISE(ABORT, 'job 与 pipeline_run 必须同属一个项目（Project Isolation，v3.2 §2.4）。');
END;
```

四条设计说明：

1. **`definition_hash` 与 `revision` 是两个东西。** `revision` 是乐观锁（防两个
   编辑器互相覆盖），`definition_hash` 是**事实**（"这次跑的是哪一版"）。只留
   `revision` 的话，一个 run 说不出它跑的定义长什么样；只留 hash 的话，保存冲突
   检测不了。两个都留，且**都不删**。
2. **`pipeline_runs` 持久化，而不是像 `ImportRegistry` 那样放内存。** 导入请求
   内存化是对的（它是个进行中的东西，进程没了就该没）；**run 不是** —— 它挂着的
   job 是持久的，"撤销这次批量跑"必须在**重启之后**还能按得下去。一个重启就消失
   的撤销入口，恰好会在你最需要它的时候消失。
3. **`step_count` / `job_count` 是冗余列，但留着。** 作业历史页要在不解析 JSON 的
   前提下排序与显示"N 个步骤 / M 个作业"。这与 `projects` 上的 `scope_hash` 同一
   性质：能被重算出来，但每次重算的代价落在最热的那个查询上。
4. **`report` 一列存整份报告**，与 `ImportRecord.report` 同一条理由：ingest 的
   原话是"报告是唯一的交付通道"，API 层不另造一份。步骤 → job 的映射就活在
   `report.steps[].job_ids` 里，**不另开一张关联表** —— 少一张表就少一处可以对不上。
   唯一的例外是 `jobs.pipeline_run_id`：作业历史要按 run 分组，那是**索引查询**，
   不能靠 JSON 扫描。

**迁移落地的连带改动**（3.7b 必做，漏一个就红）：
`magpie/core/migrate.py` 的 `MIGRATIONS` 追加一条；
`magpie/cli/tests/test_contract.py` 的 `MIGRATIONS = 7` → `8`（那条断言存在的
唯一理由就是防"迁移与描述分叉"）。

### 2.5 格式版本：怎么留、怎么升

【冻结 F3】

* **库里存的 `format_version` 是 NOT NULL**，读不到就是错误（`E_PIPELINE_BAD_FORMAT`）。
  文件入口的 `normalize_definition` 负责给旧 YAML **盖上 `format_version: 1`** ——
  于是"没有版本字段的那一天"永远不会到来，而这一天正是这个字段存在的理由。
* **写时只写新版本**：保存动作把定义升到当前版本再落库。
* **读时升级，不落库改写**：`format_version < CURRENT` 的定义在内存里升级后使用，
  **不动库里的那行**。已发生的 run 的 `definition_hash` 必须继续对得上它当时跑的
  那一版 —— 这跟 `scope_version` / `parser_fingerprint` 的快照纪律是同一条。
* **`format_version > CURRENT` → 拒绝继续**，报 `E_PIPELINE_FORMAT_TOO_NEW`
  （"程序版本低于定义版本"）。照抄 `migrate.MigrationError` 那条：
  > 数据库里有未知迁移 vN —— 程序版本低于数据库版本，拒绝继续
  不认识的格式必须**响亮地**拒绝，而不是"尽力而为地读一半"。读了一半的定义跑出来
  的是一份**看着成功、实际漏了一段**的导入，那比失败坏得多。
* 版本升级实现为 `pipeline.upgrade_definition(defn) -> dict`，一级一级往上走，
  每一级都要有一个"旧形态样例 → 新形态"的用例。

### 2.6 定义不是事实

【冻结 F4】**保存 / 改名 / 归档一个管线定义，不建立 job、不产生 contribution、
不参与 undo / redo。**

理由：`undo`/`redo` 的口径是"**贡献的活跃性**"（`contribution_supersessions` 上
的 retract 边），B 组用例的判据是**逐行指纹相等**。定义不产生贡献，把它塞进 job
会让那条判据多出一个与它无关的维度，而"撤到哪"这个问题会同时有两个答案。定义
的审计走 `activity_log`（`pipeline.save` / `pipeline.archive`，与 `parser.set` /
`rekey` 同一形状）与 `revision` 乐观锁 —— 那两个机制已经够回答"谁在什么时候改了
什么"。

反过来同样冻结：**跑一次管线不修改定义**（`pipeline_definitions` 的行数、`revision`、
`updated_at` 都不变）。跑是读定义、写事实。

> **A7（双向断言）**：保存定义 → `jobs` 行数不变、`activity_log` 多一条
> `pipeline.save`；跑一次 → `pipeline_definitions` 逐行不变、`jobs` 多 N 行。

---

## 3. 模式状态机

### 3.1 模式的落点：`store.view`，不造第二套

今天的路由状态**只有一个**：`store.view`（一个 `ref('explore')`），`App.vue` 直接
`v-if` / `v-else-if` 它，`store.setView(key)` 是唯一的写入口，`VIEWS` 是"有哪几页"的
唯一声明处。仓库里**没有 router**。

【冻结 F5】编排模式 = `store.view` 取值集合里新增的一个 `'pipeline'`：

```js
// store.js 的 VIEWS 追加一行（与既有的并列，不新开一套）
{ key: 'pipeline', label: '编排', hint: '管线编排（可编辑）' },
```

"当前打开的是哪条定义"挂在 **`store.pipeline.id`**（与 `filter.focusId` 同级的一个
"选中的东西"），不新增 `store.mode` / `store.route`。

**深链接怎么表达**：将来加 URL 同步时，序列化的是 `view` + `project` +
`pipeline.id` **这三个键**，不新增路由状态。第二套路由状态的表现是"刷新之后回到
另一个页面"，而那在本地工具里会被当成"偶发"。

**注意一件既有的事**：`App.vue` 的 `showDetail = computed(() => store.view === 'explore')`
—— 右栏详情只在探索模式出现。加了 `'pipeline'` 之后这行**不用改**，它已经是对的。

### 3.2 两种模式各允许什么

| | 探索模式（`view === 'explore'`） | 编排模式（`view === 'pipeline'`） |
|---|---|---|
| 图的内容来自 | `GET /graph`（2.8 起就没变过） | `store.pipeline.definition`（一份定义文档） |
| 拖位置 | 允许（`moveEntity` + `entity_layout`） | 允许（改的是**草稿里的**节点坐标） |
| 折叠 / 筛选 / 标注 | 允许 | 不适用 |
| 增删节点 | **不允许**（`READONLY` 红线 + 非受控入口都不存在） | 允许，但**是表单动作**：`addStep()` / `removeStep()` |
| 增删边 | **不允许** | 允许，**是画布手势**：拖 Handle 连线 → `connectSteps(src, dst)` |
| 落库 | 只能经 `PATCH /entities/{id}`（标签）+ `PATCH /graph/layout`（位置 / 折叠）+ **下面扩写的两行** | 只能经 `PUT /pipelines/{id}`（**写定义，不写事实**） |

> **§3.2 冻结表「落库」那一行的扩写（3.8，LARI-31 的裁定）**
>
> 3.7b 交付时这一行只列了两条落点：标签与布局。**3.8 明确扩写它，多列两行** ——
> 这两件事操作的是**实体**、不是管线定义，所以它们的 UI 落在探索模式：
>
> | 扩写的行 | 走哪条路 | 为什么是它 |
> |---|---|---|
> | **合并 / 拆开实体** | `POST /entities/{id}/merge`（3.7b 已有）与 `POST /entities/{id}/unmerge`（3.8 新增），两者都直通 `ops.merge` / `ops.unmerge` | 合并是**可逆的 identity overlay + 冲突登记**。在 `api/routes.py` 或前端拼一次 `UPDATE entities SET merged_into=…` 会**同时丢掉**可逆性（`entity_merge_members.revoked_by_job_id` 那条生命周期边）与冲突登记，而症状要等到有人想拆开时才出现 |
> | **批量改状态** | `POST /entities/lifecycle`（3.8 新增）→ `ops.set_lifecycle` | 复用 3.7b 建立的 job/undo 通路（`ops.undo` / `ops.undo_jobs`）。**一批 = 一个 job**（一次事务，要么全成要么全不成）；撤销就是作业历史里那一个 job 的撤销按钮。理由与取舍见 `ops.set_lifecycle` 的段落注释 |
>
> **扩写而不是"偷偷加一个写入口"**：上面两条都是**有记录的决定**，落在这里。
> 两条都**不是**"往图里加节点或边"的动作，所以按设计**不触发** `READONLY` ——
> 真触发了就说明位置写错了，那时要停下来问，不是顺手扩白名单。
>
> 三条**没有**扩写、仍然照旧的（它们的落点就是既有那两条）：
> 标签的增删仍走 `PATCH /entities/{id}`；位置与折叠仍走 `PATCH /graph/layout`
> （3.8 只是把它从"整行覆盖写"改成"按列部分写入"—— `x` / `y` / `collapsed`
> 都变成可选，响应形状逐字不变）。

【冻结 F6】编排画布是一个**受控组件**，与 `GraphCanvas.vue` 同构：

```
definition（唯一事实来源，在 store 里）
      │  computed
      ▼
  flowNodes / flowEdges  ──props──▶  PipelineCanvas.vue（不持有自己的图状态）
      ▲                                      │ 手势回调
      └──────────── store.connectSteps() ◀────┘
```

理由：探索模式的 `GraphCanvas.vue` 就是这么做的（`:nodes="flowNodes"` 是 computed），
而它的收益是实打实的 —— **`dirty` 可以退化成一次深比较**（草稿 vs 已保存），
保存 / 放弃 / 撤销都不需要一套专门的"变更日志"。让 Vue Flow 自己持图状态
（`addNodes` / `applyNodeChanges` 那一套）会引入**第二个事实来源**，而两份状态
之间同步的 bug 没有任何症状，直到有人保存了一份少了一块的管线。

【冻结 F7】**画布手势只表达依赖（边）。** 增删步骤走左侧面板的"＋ 步骤"与步骤卡片
上的删除按钮 —— 它们是**表单动作**，不是画布手势。

这条看起来是交互细节，其实是 §6 那根红线能收到很小的原因：**编排模式里真正需要
"图的 API"的只有一个动作 —— 拖一条线**。所以 `READONLY` 的洞只需要放开与**边**有关
的**一个**名字，而不是 18 个名字全开。**一个手势换一个洞，这笔账划得来。**
（顺带：不做拖拽新增节点，也让"节点集合"的变化永远是显式的、可撤销的、能进
`activity_log` 的。）

### 3.3 切换模式时未保存的编辑：保留

【冻结 F8】

1. **切换 `view` 不丢草稿。** 草稿留在 `store.pipeline` 里，切回编排模式时还在。
2. **`dirty` 是一次深比较**：`canonical(draft) !== canonical(saved)`。不用手维护的
   布尔位 —— 那种位会在某个分支里忘记翻转，然后"未保存"标记开始说谎。
3. **换项目（`selectProject`）→ 丢弃草稿并把 `pipeline.id` 清空。** 这是唯一真正
   危险的情况：草稿里的 `project` 与新的项目不是同一个，把它带过去就是在跨项目
   写事实。**跨项目污染是这套东西里唯一不能靠"小心一点"避免的错误。**
4. **不做阻塞式确认弹窗。** 本地工具里的阻塞弹窗会训练人无脑点"确定"，而它保护的
   东西（草稿）本来就不该丢。改成两处**非阻塞**的提示：保存按钮上的"未保存"标记，
   与日志抽屉里的一行。
5. 刷新页面会丢草稿。可接受（草稿不是事实）；"恢复上次草稿"（localStorage）留给
   3.7b 决定（§7）。

### 3.4 视觉上必须可区分

计划书的交付标准里有这一条，`GraphCanvas.vue` 的文件头已经为它留了位置：探索模式
常驻"🔒 探索模式 · 只读"徽标 + 中性色边框；编排模式换成强调色边框 + "✎ 编排模式 ·
可编辑"徽标。

【冻结】这个区分**必须是一条可断言的属性**，不只是两段 CSS：画布根元素带
`data-mode="explore" | "pipeline"`，用例据此断言（"靠一个徽标区分听起来很薄，但它
的作用是让人在拖完一个节点之后不产生'我刚才是不是改了数据'的疑问"）。同时
`GraphCanvas.vue` 的 `nodes-connectable=false` 与 `PipelineCanvas.vue` 的
`nodes-connectable=true` 各有一条断言 —— 一个负责"探索模式确实只读"，一个负责
"编排模式确实可编辑"，两条一起才说明这两个模式真的是两个模式。

---

## 4. 与唯一写入口的对接

### 4.1 调用链

```
PipelineCanvas.vue            手势 / 按钮
      │ store.runPipeline(id, {dryRun})
      ▼
frontend/src/store.js         只发请求，不写库
      │ POST /api/v1/pipelines/{id}/run
      ▼
magpie/api/routes.py          ★ 一条 SQL 都没有（SQL-FREE 红线扫这个文件）
      │ ctx.writer.submit("run pipeline P", task)     ← 单写队列（R1）
      ▼
magpie/api/writer.py          唯一一个写线程，唯一一条写连接
      │ task(conn)
      ▼
magpie/core/ops.py            ★ 事实的唯一写入口
      │ ① ops.start_pipeline_run(...)     建 run 行（写的是 definitions/runs，不是事实）
      │ ② core.pipeline.run_pipeline(conn, definition, ...)
      │        └─ ingest.dry_run_scope(conn, dry_run)        ← 干跑的命令级边界
      │        └─ 每个 step:
      │             ingest.ingest_paths(conn, opts, matched, _shared_txn=command_txn)
      │                └─ ops.start_job(kind="ingest") / store.add_contribution / …
      │        └─ 报告里带上每个 step 的 job_ids
      │ ③ ops.finish_pipeline_run(conn, run_id, state, report, job_ids)
      ▼
magpie/core/ingest.py         ★ 与 `magpie import`、GUI 导入**同一条**代码路径
```

**这张图里没有任何一个新盒子。** 3.7b 要做的全部是"把已有的东西接起来"，而不是
新建一条路。

### 4.2 三条不变式与各自的断言方式

"应该会走对"不算断言。下面每一条都指名一条**会变红**的机制：

| 不变式 | 靠什么守 |
|---|---|
| **写库只经单写队列**（R1） | 现成用例 `magpie/api/tests/test_writer.py` 的形状 + **新用例** `test_pipeline_run_goes_through_the_single_write_queue`：一次 run 之后断言 `writer.stats.concurrent_max == 1` 且 `len(thread_ids) == 1` |
| **不新增旁路写入口** | **现成 lint** `test_no_module_bypasses_the_contribution_write_entry` —— 它 `rglob("*.py")` 全仓扫 `INSERT INTO contributions`，只允许出现在 `store.py`，3.7b 新增的代码**自动被覆盖**。【冻结】pipeline 的新代码里出现 `INSERT INTO contributions`，意思是它绕开了 `ingest` |
| **读侧不新增第二份 SQL** | **现成红线 `SQL-FREE`** 已经扫 `api/routes.py` 与 `cli/read.py`。【冻结】`GET /pipelines` / `GET /runs/{id}` 的查询写进 `magpie/core/query.py`（`query.pipelines` / `query.pipeline_detail`），不是写在路由里 |

再加两条本步专有的：

> **A4（批量跑 = ingest）**：一次 run 之后，
> `SELECT kind FROM jobs WHERE pipeline_run_id = :run` 的结果**全部是 `'ingest'`**，
> 且它的**集合**与 `pipeline_runs.report` 里的 `job_ids` **相等**。
> 这条断言防的是"3.7b 顺手新开一个 `kind='pipeline_run'` 的 job"—— 那会让
> `explain_job_scope` / `job_counts` / GUI 的 undo 按钮在三处同时失去对一个 job 的
> 解释能力。

> **A5（干跑 == 真跑）**：同一个库状态、同一份定义，`dry_run=1` 与 `dry_run=0` 的
> `totals` **逐字段相等**；且干跑前后 `pipeline_runs` / `jobs` / `contributions`
> 的行数都不变。这是 LARI-34 在 A2e 上建立的那条断言的**同形复用**，不是新发明。

### 4.3 批量跑 = 1 个 run + N 个 job

【冻结 F9】**不改 job 的粒度。** 一个文件一个 job —— 这条从 2.5 起就是数据层的
纪律（`ingest` 的注释：这样 undo 的粒度才是文件），`raw_files` 的
`UNIQUE(project_id, sha256)`、`evidence` 的 per-file 归属、`job_counts`、
`explain_job_scope`、GUI 的 `can_undo`/`can_redo` 全部建立在它上面。为了"批量跑"
把它改成一个 job，等于让这些东西同时换一套语义。

所以一次"批量跑" = **1 行 `pipeline_runs` + N 行 `jobs`（每行带 `pipeline_run_id`）**。
run 是**分组**，不是新的一种 job。

顺带定义 **"可复用"的语义**：复用 = 同一份定义**再跑一次**。定义里**不存运行
结果**，也不做缓存层 —— 幂等来自 2.5 已有的那一条（同一 `sha256` + 同一
`parser_fingerprint` → 跳过并如实报告），不由定义层再实现一遍。第二次跑一份没变
的输入，得到的是 N 个 `skipped`，那是**对的**答案。

### 4.4 干跑：复用 `dry_run_scope`，且不留 run 行

【冻结 F10】

* 干跑的事务边界 = **一条命令**，复用 `ingest.dry_run_scope`。`pipeline run` 是一条
  命令、多个 step，每个 step 各自开边界的话第 2 个 step 看不见第 1 个 —— 那正是
  LARI-34 修掉的 `updated` 低报。`run_pipeline` 已经在这么做了，**新入口照抄即可，
  不许另立一套**。
* **干跑不写 `pipeline_runs` 行。** 理由：干跑的硬性标准是"跑完一行都不留"，
  而一份"预演发生过"的持久记录不是事实。报告照旧交付（返回给调用方 + 与 ingest
  一样落一份到 `reports/`），但库里没有它的位置。
  这也是 `pipeline_runs` 上**没有 `dry_run` 列**的原因 —— 一列永远等于 0 的数字
  比没有这一列更糟：它会被读成"查过了，没有干跑"。

### 4.5 API 契约（3.7b 必须落地的形状）

【冻结 F14】`GET/POST /pipelines`、`POST /pipelines/{id}/run` 从 **501 变成真端点**。
完整的路由表：

| 方法 | 路径 | 说明 | 3.7b 必做？ |
|---|---|---|---|
| `GET` | `/api/v1/pipelines` | 定义列表（含各自最近一次 run 的摘要） | ✅ |
| `POST` | `/api/v1/pipelines` | 建定义（body = §2.2 的 JSON） | ✅ |
| `GET` | `/api/v1/pipelines/{id}` | 取定义 + `definition_hash` + `revision` | ✅ |
| `PUT` | `/api/v1/pipelines/{id}` | 保存（body 带 `revision`，不匹配 → 409 `E_PIPELINE_CONFLICT`） | ✅ |
| `POST` | `/api/v1/pipelines/{id}/archive` | 归档（软删，**不提供 DELETE**） | ✅ |
| `POST` | `/api/v1/pipelines/{id}/run` | `{dry_run}` → 202 + `run_id` + `events` | ✅ |
| `GET` | `/api/v1/pipelines/{id}/runs` | 这个定义的运行历史 | ✅ |
| `GET` | `/api/v1/runs/{run_id}` | 单个 run（含完整报告） | ✅ |
| `POST` | `/api/v1/runs/{run_id}/undo` | `{dry_run}` | ✅（§5） |
| `POST` | `/api/v1/runs/{run_id}/redo` | `{dry_run}` | ✅（§5） |

**进度走 SSE，且复用 `EventBroker`。** 它本来就是按 id 字符串订阅的（
`broker.open(record.id)`），run 用 `run-1` 这样的前缀 id 即可（与 `imp-1` 同一条
纪律：**带前缀是为了让人一眼看出它不是 job_id**）。但 `run_pipeline` 目前**没有**
把 `on_progress` 传给 `ingest_paths` —— 3.7b 必须补上这一根线：

```
run_pipeline(..., on_progress=cb)  →  ingest.IngestOptions(on_progress=cb)
```

理由抄 `imports.py` 的原文：**"API 层不自己数数 —— 那样迟早会出现'进度条说 80%，
报告说 100%'。"** 进度只有一个来源，就是 ingest 的回调。

**落地的连带改动**（漏一个就红）：`magpie/api/tests/test_routes.py` 的
`test_unimplemented_endpoints_are_loud` 参数化里那三行 pipeline 要删掉；
`magpie/api/tests/openapi_snapshot.json` 重新生成；`CHANGELOG.md` 开 `0.7.0`
一节（这是阶段 3 里第二次动到既有响应形状 —— `/jobs` 多一个键，见 §5.2）。

---

## 5. 批量跑的 undo 语义

### 5.1 唯一可逆原语仍然是 job

【冻结 F11】**`undo` 的单位永远是 job。** 一次"批量跑"的撤销 = **把这一批的 N 个
job 逐个 undo**，不是发明一个"撤销一个 run"的新原语。

这不是偷懒，是唯一说得通的选法：2.6 冻结的 `undo`/`redo` 精确互逆，其证据是
**逐行指纹相等**（B 组用例），而逐行指纹是**每个 job 的贡献**算出来的。任何"批量
撤销"如果自己实现一套状态迁移，就必须独立地再证一遍它与 redo 互逆 —— 而那份证明
会与 job 的那份分叉。

这个形状**仓库里已经有先例**，`magpie/api/imports.py`：

```python
def undo(self, import_id: str):
    job_ids = sorted(record.job_ids, reverse=True)      # ← 后写的先撤
    def task(conn):
        reports = [ops.undo(conn, jid, actor=self.actor) for jid in job_ids]
        return {"import_id": record.id, "undone": job_ids, "reports": reports}
    return self.writer.submit(f"undo {record.id}", task)
```

【冻结】**把它提成一份实现**：`ops.undo_run(conn, run_id, *, actor, dry_run=False,
_shared_txn=False)`，`/imports/{id}/undo` 与 `/runs/{id}/undo` **都调它**。
理由与 3.5 把两条 SQL 收敛成 `core/query.py` 逐字相同：两份实现不会同时出错，
也不会以同样的方式出错，而"同一批作业、两种撤销结果"没有任何症状。

**顺序冻结**：`undo_run` **逆序**（job id 从大到小 —— 后写的先撤，从栈顶往下拆）；
`redo_run` **正序**（从小到大 —— 按原来的顺序重放）。撤销是拆栈，重放是重放，
两个方向各有各的对，都写死。

**部分失败冻结**：真跑里第 k 个 job 失败 → **停下**，不继续，返回
`{"op": "undo.run", "undone": [...], "stopped_at": job_id, "error": ...}`。
**不假装原子** —— 数据层从不假装（"取消：已提交部分保留，仍可 undo"是同一条
哲学），它做的是把状态**说清楚**：哪几个撤了、卡在哪一个、为什么。
`run.applied` 保持 1（这一批还没整体失效），下一个 `undo_run` 会从剩下的接着撤 ——
**幂等且可重入**。

**已经单独撤过的 job**：`undo_run` 只撤 `applied = 1` 的那些，已撤的列进
`already_undone` 并在报告里如实列出。这样"撤一次撤到哪"的答案是稳定的 ——
**撤到这次跑之前**，即使中间有人单独撤过几个。

### 5.2 `applied` 与 `state` 分离

【冻结 F12】`pipeline_runs.applied` 与 `jobs.applied` 是**同一个词、同一个语义**：
`1` = 这一批的效果当前生效，`0` = 已被撤销（可 redo）。`state` 说的是**执行**
结果（`running`/`done`/`failed`/`partial`/`aborted`），`applied` 说的是**应用**
状态。两者分离的理由 `schema/001_init.sql` 已经写过一遍：

> "跑完了但后来被撤销" 与 "执行失败" 是两件事。

`undo_run` 只动 `applied`，**不把 `state` 改成 `aborted`**。GUI 的"能撤 / 能复原"
从 `applied` 推导，与 `_decorate_job` 的 `can_undo`/`can_redo` 同构。

**这是本步唯一一处改到既有响应形状的地方**：`jobs` 多了一列 `pipeline_run_id`，
而 `query.job_row` 用的是 `SELECT *`，`/api/v1/jobs/{id}` 的 `data` 因此多一个键
（值多为 `null`）。按 `CHANGELOG.md` 开头的规矩，**要升 minor 并在 `0.7.0` 一节
登记**。`query.JOB_COLUMNS`（列表用的那一串显式列）**不加**它 —— 列表不需要它，
而 `SELECT *` 的顺序不稳定的坑已经在那个常量上面记着了。

### 5.3 预演的事务边界（这一节是 LARI-34 的教训）

【冻结】`undo_run(dry_run=True)` 必须把整批跑在**一个**事务里。

`ops.undo` 的实现是 `with transaction(conn, dry_run=dry_run):`，而
`store.transaction` 无条件 `BEGIN IMMEDIATE` —— 嵌套会当场炸
（`cannot start a transaction within a transaction`）。所以要把
`ingest._JobTxn` 那一套 `shared` 布尔原样搬过来：

```python
# 入口（API 的写任务 / CLI）
with ingest.dry_run_scope(conn, dry_run) as command_txn:     # ← 已存在的那个
    ops.undo_run(conn, run_id, dry_run=dry_run, _shared_txn=command_txn)
```

`_shared_txn` 为真时 `undo` / `undo_run` **不碰事务边界**，于是：

* 第 2 个 job 的预演**看得见**第 1 个 job 的撤销效果 —— 与真跑一致；
* 末尾一次 ROLLBACK，一行不留。

**如果不这么做**：每个 job 各自 `BEGIN`/`ROLLBACK`，那么"预演说撤掉 12 条贡献、
真跑撤掉 15 条"就成了一条会说假话的预览 —— 而 LARI-34 的结论说得很直白：
**一个会说假话的干跑比没有干跑更糟，人会照着它的数做判断。**

顺带把 `dry_run_scope` 的既有纪律照抄一遍：**yield 出来的布尔必须原样往下传，
别在两个地方各判一次**（`run_pipeline` 与 `ingest_paths` 之间已经是这么接的）。

**这条的断言**：

> **A8**：同一个库状态上连续预演两次，两份报告**逐字段相同**；预演之后
> `SELECT COUNT(*) FROM jobs WHERE applied = 0` 与
> `SELECT COUNT(*) FROM contribution_supersessions WHERE reason = 'retract'`
> 都不变。

### 5.4 与 2.6 已冻结口径的关系（一句话）

`undo_run` / `redo_run` **不新增任何状态迁移语义**，它只是"把 N 次已冻结的
`ops.undo` / `ops.redo` 按一个写死的顺序连起来，并记一个 `applied`"。所以
B 组的逐行指纹相等**逐字照旧**成立 —— 一次 `undo_run` 之后再 `redo_run`，
库状态与跑之前**逐行相同**，因为每一个 job 的 undo/redo 本来就是这样。

> **A9**：真跑一次 3 个文件的批量跑 → 快照 → `undo_run` → `redo_run` → 断言
> 与快照**逐行指纹相等**（复用 `store.effective_equivalent`，与 2.6 的
> `undo`/`redo` 用例同一套判据）。

---

## 6. `READONLY` 红线的收窄方案

### 6.1 为什么不能删，只能收窄

`READONLY` 守的是计划书 §7.2 的那条分界，也是 2.8 的核心承诺：

> **看数据的时候不会误改数据。**

删掉它，等于把这条承诺降级成"我们记得不要调用那几个函数"。而这个仓库的基本原则
是：**真正活下来的约束都有机制守着。** 所以：**只收窄，不删除，并且收窄之后要
比原来多一条反向断言。**

### 6.2 洞有多大：一张表

【冻结 F13】先把洞算清楚，再决定怎么挖。编排画布**真正需要**的名字，在
§3.2 的"受控组件 + 画布只表达边"两条冻结之下，只有：

| 名字 | 为什么需要 | 为什么别的都不需要 |
|---|---|---|
| `onConnect` | 用户从一个步骤的 `Handle` 拖到另一个步骤 —— 这是**唯一**一个"必须由画布手势产生一条边"的动作 | 连线本身不走 `addEdges`：受控画布把 `connect` 事件翻译成 `store.connectSteps(src, dst)`，改的是**定义**，节点/边由 `computed` 重新算出来 |
| （无） | — | `addNode(s)` / `removeNode(s)`：增删步骤是**表单动作**（`store.addStep` / `removeStep`），不经过 Vue Flow 的可变 API |
| （无） | — | `setNodes` / `setEdges` / `applyNodeChanges` / `applyEdgeChanges`：受控组件不需要它们 —— 这正是 §3.2 选受控的收益 |
| （无） | — | `updateNode` / `updateEdge` / `connectNodes` / `createNode` / `createEdge`：不存在对应的交互 |

**所以白名单是：一个文件 × 一个名字。**

```python
Path("frontend/src/components/PipelineCanvas.vue"): frozenset({"onConnect"})
```

这不是抠门，是**算出来的**。而它带来的直接好处是：这条红线的**语义没有被削弱** ——
它从"`frontend/src/**` 里不许出现 18 个名字"变成"`frontend/src/**` 里不许出现 18 个
名字，**除了**一个文件里的一个名字，而那一个名字是被连线的唯一入口"。

【冻结】如果 3.7b 发现还需要边族的别的名字（比如 `applyEdgeChanges`），**回
`LARI-17` 扩这一行**。扩一行是**一次有记录的决定**；一开始就放开一整族，是把
决定权交了出去 —— 而"开一个洞，然后所有新代码都搬进洞里"正是这个洞唯一的下场。

### 6.3 三条规则的可执行规格

`check.py` 现有 `_GRAPH_MUTATION_NAMES`（18 个名字）与 `_readonly_violations()`
（按行剔注释后做**整词**匹配）**一行都不改** —— 改的是"扫哪些文件、允许哪些名字"。
新增两个模块级常量：

```python
#: 编排模式画布的**文件级 × 名字级**白名单。两条纪律，都不是风格：
#:
#:   * **文件级，不是目录级** —— 目录级白名单等于把洞挖成一个房间：任何新文件
#:     放进那个目录就自动免检。文件级让每一次扩张都必须改下面这一行，而那是一次
#:     要过 review 的 diff。"洞"的代价应该落在**写代码的人**身上，不是落在
#:     下一个读这条规则的人身上。
#:   * **名字级，不是整表放开** —— 只放开该文件真的用到的那几个名字。放开整表
#:     之后，"这个文件为什么需要 addNodes"这个问题就再也没有地方问了。
#:
#: 已知边界：把 onConnect 存进变量再间接调用、或从 Vue Flow 的 composable 里
#: 换一个没列进清单的别名，都绕得过去（见 _GRAPH_MUTATION_NAMES 上方那段）。
#: 这条收窄没有、也不可能提高那个上限 —— 它只把"哪里允许出现什么"写清楚。
_EDITABLE_CANVAS: dict[Path, frozenset] = {
    Path("frontend") / "src" / "components" / "PipelineCanvas.vue": frozenset({"onConnect"}),
}

#: **探索模式那一路的文件清单**（不是目录）—— 反向断言的锚点。
#: 它们是"看数据"这条路的全部入口：图的状态、纯逻辑、两个组件、外壳。
#: 这份清单钉的是"这条路必须一直是干净的"，而不是"某个目录必须干净"。
_EXPLORE_CANVAS_FILES = (
    Path("frontend") / "src" / "store.js",
    Path("frontend") / "src" / "graph.js",
    Path("frontend") / "src" / "components" / "GraphCanvas.vue",
    Path("frontend") / "src" / "components" / "EntityNode.vue",
    Path("frontend") / "src" / "App.vue",
)
```

**规则一 `READONLY`**（判据不变，只加一个 per-file 的豁免）：

```python
def _rule_readonly() -> list:
    bad = _selftest_readonly()                       # ← 既有自检，不动
    if not _FRONTEND_SRC.is_dir():                   # ← 既有守卫，不动
        return bad + [...]
    for path in sorted(_FRONTEND_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in (".js", ".vue"):
            continue
        rel = path.relative_to(ROOT)
        allowed = _EDITABLE_CANVAS.get(rel, frozenset())      # ← 唯一的改动
        for lineno, name in _readonly_violations(text):
            if name in allowed:
                continue
            bad.append(_v(rel, lineno, f"{name} —— READONLY：…（原报文不变）"))
    return bad
```

**规则二 `READONLY-REVERSE`**（新的反向断言，四组、全部会变红）：

```python
def _rule_readonly_reverse() -> list:
    """白名单不是"这里随便写"，是"这里只允许这几个名字"。四组断言：
       (a) 白名单每一格都指向一个**真的存在**的 frontend/src 下的文件
       (b) 白名单文件实际命中的名字 ⊆ 声明的名字   —— 洞没有偷偷变大
       (c) 声明的名字**都真的被用到**             —— 白名单不能提前扩张
       (d) 探索模式清单逐个：存在、不在白名单里、命中 0 个
    """
```

四组各自的**为什么**：

* **(a) 存在性**：一个指向不存在文件的白名单条目是**静默的洞** ——
  `path.rglob` 扫不到它，规则永远绿。这与 `_rule_readonly` 里那句"目录没了规则就
  静默变绿"的守卫是同一条理由。同时禁止白名单指向 `frontend/src` 之外
  （否则它根本不参与扫描，白名单就成了装饰）。
* **(b) 实际 ⊆ 声明**：这条**防的是洞在实现期自己长大**。没有它，白名单文件里
  写 `addNodes` 不会有任何反应 —— 而"反正这个文件已经豁免了"正是最自然的一步。
* **(c) 声明 ⊆ 实际**：**这是防"开一个洞，然后所有新代码都搬进洞里"的那一条。**
  一个"留着以后用"的白名单条目就是洞的**预授权**：它让下一次加写操作变成
  "只是用了已经允许的名字"。要求每个声明的名字都真的被用到，等于要求白名单
  **每一条都有当下的理由**；一个名字不再用了，规则会红，然后它被删掉。
  （`{"onConnect"}` 之所以敢这么小，正是因为有这一条兜着。）
* **(d) 探索模式清单**：这是"白名单之外仍不得出现写操作名"的**具体化**。
  它不依赖目录布局（把 `GraphCanvas.vue` 改名或搬家都不会让它静默失效），
  钉的是"看数据这条路的那几个文件"。它同时挡住"把探索模式的组件搬进白名单"
  这个动作 —— 两个清单求交集，非空即红。

**规则三 自检** `_selftest_readonly_scope()`（没有自检的静态规则很可能是"碰巧
永远不报"，那和没写没有区别）：

| 反例 / 对照 | 期望 |
|---|---|
| 白名单文件里出现 **未声明**的名字（`PipelineCanvas.vue` 里的 `addNodes`） | **报** |
| 白名单文件里出现 **已声明**的名字（`PipelineCanvas.vue` 里的 `onConnect`） | 不报 |
| 非白名单文件里出现任意一个名字（`store.js` 里的 `addNodes`） | **报** |
| 白名单条目指向一个不存在的文件 | **报** |
| 白名单条目落在 18 个名字之外（比如写个 `doThing`） | **报**（声明里出现了不存在的名字，等于白名单在说谎） |

最后一行顺带把"白名单只能写 `_GRAPH_MUTATION_NAMES` 里的名字"这条钉住 ——
否则它可以被写成一份谁都看不懂的清单。

**规则表**：`RULES` 从 10 条变 **11 条**（`READONLY` 的描述改一个字：
`frontend/src/**` → `frontend/src/**（编排画布的白名单见 _EDITABLE_CANVAS）`），
`check.py` 文件头第 44 行的规则一览同步。

### 6.4 这条收窄没有解决的事（诚实交代）

按这个仓库的惯例，边界要写出来，不能只写它管什么：

1. **名字级扫描不是数据流分析。** 汇编级的绕法（别名、间接调用、`eval`）本来就
   绕得过去，这条收窄一个字都没改善它。它保证的是"**没人把那些众所周知的名字
   直接写进源码**"——包括"顺手把 `addNodes` 接上"那次改动会在 CI 前变红。
2. **白名单挡不住语义层面的挪用**：有人可以在 `PipelineCanvas.vue` 里渲染数据
   图谱。这一层由**形状**守：探索模式的图状态（`store` 的 `_raw`）与编排模式的
   草稿（`store.pipeline.definition`）是**两个对象**，`PipelineCanvas.vue` 只拿得到
   后者（组件 props 可以断言这一点）。
3. **这条红线只是三层里的第一层。** 第二层是 `frontend/tests/readonly.test.js`
   （store 导出面上不存在这些能力 + 挂载后连线入口关死），第三层是组件属性
   （`nodes-connectable` / 没有 `Handle`）。3.7b 要**再加两条**：`PipelineCanvas.vue`
   挂载后 `nodesConnectable === true`、`GraphCanvas.vue` 仍然 `false`。
   **红线管"有没有人写下这个能力"，用例管"写出来的东西确实是那个模式"。**

---

## 7. 留给 3.7b 决定项

这些是**刻意的空白** —— 现在钉死它们的收益低于成本（要么没有可对比的取舍，要么
一做就有真机手感）：

1. 步骤卡片的视觉：色带 / 图标 / 字段怎么排、几个徽标（§7.3 的 token 表已经给了
   配色与半径，照着用）。
2. 编排图的自动布局：节点数是个位数，直接复用 `graph.js` 的确定性网格即可；
   §7.3 提到的 dagre/elk 是**探索模式**的自动布局，本步不要引新依赖。
3. 步骤参数面板的控件形态。**核对过的事实**：`GET /meta` 已经带 `parsers`
   （`{tool, format_version}` 的列表），所以 parser 下拉直接用它；但 `authority`
   的四个取值（`verified` / `semiautomatic` / `manual` / `unverified`）目前**只在
   `schema/001_init.sql` 的 CHECK 与 `sources.authority_level` 里**，`/meta` 不返回
   它们。两条路：给 `/meta` 加一个 `sources` 键（那是**动既有响应形状**，要按
   `CHANGELOG.md` 的规矩登记），或者在 UI 上写死四个值。**建议前者** —— 写死在
   界面上的词表会在加第五个取值的那天开始静默漏项。
4. 定义列表的排序 / 搜索（`LeftPanel` 的"管线列表"那一格，目前写着"501，不放假按钮"，
   3.7b 把那句话换成真列表）。
5. `magpie pipeline export`（库 → YAML）做不做。**建议做**（它是"可复用"的最后一
   公里：把一份定义发给未来的自己），但它是新的 CLI 命令，属 3.8 的量级。
6. 步骤级 `dry_run` / `batch_size` 是否在 UI 上暴露。**建议暴露 `dry_run`，不暴露
   `batch_size`**（它是性能参数，不是语义参数）。
7. run 历史的呈现方式：复用 `JobsView` 加一层分组，还是新开一页。
8. `localStorage` 草稿恢复（§3.3 第 5 条的延伸）。
9. SSE 进度的粒度（复用 `ingest` 的 `file_start` / `file_done` 事件，还是加
   step 级的事件 —— 建议**不加**：进度条的分母说"文件"比说"步骤"更容易对上）。

---

## 8. 3.7b 落地清单（正序，每步配一条断言）

| # | 动作 | 断言 |
|---|---|---|
| 1 | `schema/008_pipeline_definitions.sql` + `migrate.MIGRATIONS` + `test_contract.MIGRATIONS = 8` | `test_schema_migrations_records_version_and_is_idempotent`（自动覆盖新迁移）+ CLI 契约网的迁移数 |
| 2 | `pipeline.normalize_definition` / `upgrade_definition` + 旧 YAML 归一化 | **A6**：两个载入器逐字节相等；旧形态样例逐条升级 |
| 3 | `query.pipelines` / `query.pipeline_detail` / `query.runs` | `SQL-FREE` 红线（`routes.py` 里不许出现 SQL） |
| 4 | `ops.save_pipeline` / `ops.archive_pipeline` / `ops.start_pipeline_run` / `ops.finish_pipeline_run` | **A7** 双向：保存不建 job、跑不改定义 |
| 5 | `ops.undo_run` / `ops.redo_run`（含 `_shared_txn`），`/imports/{id}/undo` 改调它 | **A9** 逐行指纹相等；**A8** 预演两次报告相同 |
| 6 | `run_pipeline` 补 `on_progress` 线程 → `ingest.IngestOptions` | SSE 报告的百分比与报告里的 `job_ids` 数**逐次一致** |
| 7 | `api/routes.py` 的十条路由（§4.5）+ `test_routes.py` 删三行 501 + 重生成 openapi 快照 | `test_pipeline_run_goes_through_the_single_write_queue`（`concurrent_max == 1`） |
| 8 | 前端：`VIEWS` 加 `'pipeline'`、`store.pipeline` 草稿、`PipelineCanvas.vue`（含 `data-mode`） | `readonly.test.js` 的两条模式断言（`true` / `false` 各一） |
| 9 | `check.py`：`_EDITABLE_CANVAS` / `_EXPLORE_CANVAS_FILES` / `_rule_readonly_reverse` / `_selftest_readonly_scope` | `check.py lint` 11 条全绿；自检的五个反例逐条验 |
| 10 | `CHANGELOG.md` 开 `0.7.0`（`/jobs/{id}` 多 `pipeline_run_id`）；`pyproject.toml` / `magpie/__init__.py` 版本 | `py -3.13 check.py all` 全绿 |

**最后一条纪律**：`py -3.13 check.py` 与 `check.py bench` 必须全绿且**无性能退化**
（阶段 3 的总交付标准）。`check.py` 的解释器守卫与 `bench` 的独立可触发入口是
这条纪律唯一可验证的形式 —— 3.7b 交付时把这两条命令的**原始输出**贴进结论评论。
