# Magpie（喜鹊）· 数据模型修订稿 v3.1

> **本稿不增加任何功能。** 只做一件事：把 v3 里"设计声明"和"底层模型"之间的裂缝焊死。
> 修订范围锁定你点名的五件事：**Provenance / undo / Evidence / 状态语义 / manual 保护**，顺带收口身份稳定性、路径沙箱、离线强制、依赖分层、跨平台路径。
> 本稿完成后，v3 §3.4 的 DDL 作废，以本稿第 5 节替换。

---

## 0. 先回应你的评审：接受、修正、反驳

| # | 你的判断 | 我的结论 |
|---|---|---|
| ① | undo 不可靠，需要 job provenance | **接受，但你的修法不够**。`created_job_id` 单列解决不了"同一行被多个 job 共同产生"，因为 observation 是 upsert 语义。必须改成**贡献流（contributions）**，见 §2。 |
| ② | relation/observation 的 source 不能多源 | **接受**。但它不是独立问题，是 ① 的同一个解：多源 = 多条 contribution，`source` 单值列直接删掉。 |
| ③ | live/dead 推断过激 | **接受，并且你漏了最关键的一环**：模型里没有"本轮是否全量"这个概念。加 `jobs.coverage` 后才谈得上推断，而且推断结果只能进 Inbox，不能自动落库。 |
| ④ | "无 401/403 = 未鉴权"不成立 | **接受**。并且要注意一个硬约束：Magpie 不发请求，所以 `tested_*` **只能来自"不带凭据的请求 vs 带凭据的请求"两组观测的对比**，不能来自 Magpie 自己测。这反而让设计更清晰，见 §2.3。 |
| ⑤ | secret 只存 hash 牺牲复现能力 | **接受**，但加密库不能进 core（AES 不在标准库）。方案见 §6.3：Windows 用 DPAPI（纯 ctypes，零依赖），其它平台走可选 extras。 |
| ⑥ | manual 保护只覆盖 observation/relation | **接受**。升级为**字段级冻结**（`manual_fields`），而不是新加一张 patch 表 —— 后者会让每次查询都要 merge 两个来源。 |
| ⑦ | pipeline 路径越界风险 | **接受**，并且比你写的更严重：前端页面能调本地 API，所以要同时防 **DNS rebinding**（Host 头白名单），不只是路径。见 §7。 |
| ⑧ | 零出站需要更强验证 | **接受**。三层 + 一条你没提的：Windows 没有 network namespace，L3 必须给可执行替代方案，否则那条 CI 永远只是纸面承诺。见 §8。 |
| ⑨ | "core 纯标准库"工程成本偏高 | **部分反驳**。真正贵的只有两处：**SQLite 特性可用性**和 **YAML**。httpx/nuclei 是 JSONL、nmap 是 XML、HAR/Postman 是 JSON —— 全是标准库能搞定的。所以不该放松纯度，该做的是**分层纯度 + 特性门禁**，见 §9。 |
| ⑩ | FTS5/trigram 兼容性坑 | **接受**，并给出可执行的门禁函数与降级路径（§9.2）。 |
| ⑪ | `%APPDATA%` 与跨平台矛盾 | **接受**，补齐三平台路径（§9.3），并明示 v1 是 Windows 优先但不写死平台。 |
| ⑫ | "五个镜头"实际六个 | **接受**，这是我文档里的错误。统一为 6 个 canonical lens id（§10）。 |
| ⑬ | 画布喧宾夺主 | **完全接受，是我的判断失误**。v3 把渲染性能当硬指标而把正确性当软目标，优先级反了。§11 重排，并把"像 n8n"从核心体验降级为可视化层。 |
| ⑭ | 阶段 0 过早优化 | **接受**。新阶段 0 = 正确性三根钉子（§11）。 |
| ⑮ | endpoint 身份不稳定 | **接受**，并给出一条能挡住 `/report/2024` 误折叠的硬规则（§4）。 |
| ⑯ | 缺独立 Evidence 模型 | **接受**，且它是 ① 的另一条腿：contribution 回答"谁在何时断言"，evidence 回答"凭什么"。两者必须一起做，分开做会得到两个半成品。 |
| ⑰ | 与 BurpMcpEnhance 上下游 | **接受并采纳为产品边界**（附录 B），并给出交换契约 `facts-json`。 |
| ⑱ | 优先级表 | 采纳，微调后见 §1.2。 |
| ⑲ | 不该继续加功能 | **接受**，并落成一张冻结清单（§12）。 |
| ⑳ | 收敛架构主线 | 采纳，只加一个概念：**Coverage（作业覆盖度）**，它是状态推断能否成立的前提。 |

---

## 1. 修订后的总原则

### 1.1 一句话

> **数据行是缓存，贡献流是事实。**

v3 把 `observations` 当事实，于是 `seen_count` 变成可变计数器、`source` 变成单值、undo 无法定位。v3.1 把**贡献（contribution）当事实**，一切计数字段（`source_count` / `is_live` / `first_seen_at` / `last_seen_at` / `confidence`）都是**可重算的派生缓存**。
判据很简单：**任何派生字段，删除后必须能仅凭 contributions + evidence 完整重建。** 这条会变成一条测试。

### 1.2 优先级（采纳你的分级，微调）

| 级别 | 事项 | 为什么是这一级 |
|---|---|---|
| **P0-1** | 贡献流 + Evidence 统一溯源模型 | 其它四件都依赖它；不定死，后面全是补丁 |
| **P0-2** | undo 的真实可回滚（含 reparse / merge 边界） | 直接决定"敢不敢用" |
| **P0-3** | 状态语义：lifecycle / 观测新旧 / coverage 三分；auth_state 重定义 | 决定数据可信度，且会污染 diff |
| **P0-4** | 字段级 manual 保护 | 决定人工劳动会不会被冲掉 |
| **P0-5** | 身份稳定性与规范化版本（endpoint 折叠规则） | 决定去重是否稳定、能否 rekey |
| P1-1 | 时间语义：`observed_at` / `ingested_at` / 来源 | **你漏了**：不区分会导致补导入历史文件时 diff 全线错乱 |
| P1-2 | 路径沙箱 + DNS rebinding 防护 | 本地工具的真实越权面 |
| P1-3 | 离线三层强制（Windows 可执行版） | 承诺要能验证 |
| P1-4 | 依赖分层 + SQLite 特性门禁 + 跨平台目录 | 可移植性 |
| P1-5 | contributions/evidence 增长与压实策略 | **你漏了**：这是唯一会炸存储的地方 |
| P2 | 画布/性能降级为软预算；镜头数与措辞统一 | 文档与优先级修正 |

---

## 2. 核心设计：溯源（Provenance）

### 2.1 为什么 `created_job_id` 不够

你的例子是对的，但结论要再往前一步。observation 的唯一键是 `(entity_id, type, value)`，它是 **upsert**：

```
Job A 写入 observation X（nginx 1.24）     → 创建 X
Job B 再次见到 X（httpx 也报 nginx 1.24）  → 命中 UNIQUE，只更新 last_seen
```

此时 `X.created_job_id = A`。`undo B` 应该做什么？什么都不删，但 B 对 X 的"证实"应当消失。`created_job_id` 表达不了"B 也证实过 X"。反过来 `undo A` 若直接删 X，就把 B 的证据一起删了。

**所以需要的不是"谁创建的"，而是"谁断言过"——一条只增不改的贡献流。**

### 2.2 两条腿：contributions（谁断言）+ evidence（凭什么）

```
job ──< contribution >── subject (entity | observation | relation)
             │
             └── evidence ──> raw_file (+ locator + excerpt)
```

```sql
-- 事实：某作业、用某来源、在某证据处、于某时刻断言了某条 subject
CREATE TABLE contributions (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  subject_kind  TEXT NOT NULL CHECK (subject_kind IN ('entity','observation','relation')),
  subject_id    INTEGER NOT NULL,            -- 多态，见 §2.4 的代价说明
  source        TEXT NOT NULL,               -- 工具名 / 指纹规则名 / 'manual'
  source_version TEXT,
  evidence_id   INTEGER NOT NULL REFERENCES evidence(id),   -- 无证据不入库
  observed_at   TEXT NOT NULL,               -- 观测时间（见 §5.1）
  observed_at_source TEXT NOT NULL
                CHECK (observed_at_source IN ('tool','file_mtime','source_meta','ingest_time','manual')),
  ingested_at   TEXT NOT NULL,
  superseded_by_job_id INTEGER REFERENCES jobs(id),
  superseded_at TEXT,
  UNIQUE (job_id, subject_kind, subject_id, source, evidence_id, observed_at)
);
CREATE INDEX idx_contrib_subject ON contributions(subject_kind, subject_id)
  WHERE superseded_at IS NULL;                        -- 部分索引：只索引活贡献
CREATE INDEX idx_contrib_job     ON contributions(job_id);
CREATE INDEX idx_contrib_evidence ON contributions(evidence_id);
```

**语义收益（直接回答你的 ①②）：**

| 问题 | v3 | v3.1 |
|---|---|---|
| 这条结论被哪些来源证明过？ | `source='httpx'`（丢了 nmap） | `SELECT DISTINCT source FROM contributions WHERE subject_kind='observation' AND subject_id=? AND superseded_at IS NULL` |
| `seen_count` 是什么？ | 一个语义不明的可变计数器 | 删除。改用 `source_count`（独立来源数）与 `job_count`（被多少个作业证实） |
| undo A 会怎样？ | 删掉 B 的证据 | 只删 A 的 contributions，行保留，派生字段重算 |
| 为什么信 nginx 1.24？ | 不知道 | 顺着 evidence 打开 nmap XML 第 1821 行，看到 excerpt |

### 2.3 Evidence：一条结论的"凭什么"

你说得对，`raw_file_id + line` 只是半个证据。完整证据需要**定位器 + 有界摘录 + 脱敏**。

```sql
CREATE TABLE evidence (
  id            INTEGER PRIMARY KEY,
  project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  raw_file_id   INTEGER REFERENCES raw_files(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL
                CHECK (kind IN ('line','json_object','jsonl_line','http_message','xml_node',
                                'certificate','packet','manual','rule_hit')),
  locator       TEXT NOT NULL,      -- JSON：{"line":1821} / {"har_entry":12} / {"byte":10240,"len":900}
  locator_hash  TEXT NOT NULL,      -- sha256(raw_file_id || kind || locator)
  excerpt       TEXT NOT NULL DEFAULT '',   -- ≤2KB，已脱敏；压实后可清空
  excerpt_hash  TEXT NOT NULL,      -- 去重 + 压实后仍可校验"当初摘的是这段"
  excerpt_state TEXT NOT NULL DEFAULT 'full' CHECK (excerpt_state IN ('full','hash_only')),
  created_at    TEXT NOT NULL,
  UNIQUE (raw_file_id, kind, locator_hash, excerpt_hash)
);
CREATE INDEX idx_evidence_rawfile ON evidence(raw_file_id);
```

四条纪律：

1. **无证据不入库**。`contributions.evidence_id` 是 `NOT NULL`；人工录入也要生成一条 `kind='manual'` 的 evidence（`locator='{}'`，`excerpt='人工录入'`）。这让"每个结论都可解释"成为一条**可测断言**，而不是口号。
2. **摘录有界且脱敏**。≤2KB，落库前过敏感串规则（与导出同一套），避免 evidence 表变成第二个明文凭据库。
3. **大对象不搬进 DB**。evidence 只存指针 + 摘录，原文永远在 `raw_files`。
4. **压实可降级**。`magpie compact` 把老 evidence 的 `excerpt` 清空并置 `hash_only`，保留 locator/hash → 仍能定位回归档文件重新取原文，只是不能直接展示。这是 §2.5 增长策略的一部分。

### 2.4 一个必须明说的代价：多态外键

`contributions.subject_id` 是多态引用，SQLite 无法对它建真外键。两个选择：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 单张多态表（本稿采用） | undo 只需一次 `WHERE job_id=?`；一套代码 | 无 FK 完整性，可能产生孤儿贡献 |
| 三张类型化表 | 有真 FK | undo/统计/压实都要写三遍；查询要靠 UNION |

**我选单张表 + 三条补偿措施**（因为 undo 是 P0，它是热路径）：

1. 所有写入必经 `store.py`（CI 断言）。
2. 删除 subject 一律走 `store.delete_subject()`，由它显式删除贡献（不依赖 `ON DELETE CASCADE`）。
3. `magpie doctor` 增加**孤儿贡献检查**，并且测试里断言"任意操作序列结束后孤儿数 == 0"。

### 2.5 派生缓存与增长策略（你漏掉的一点）

contributions 是只增流，规模会是：100 万观测 × 2.5 来源 × 10 个作业 ≈ **2500 万行**。必须提前定策略，否则阶段 3 直接炸盘。

- **派生字段**（`source_count` / `is_live` / `first_seen_at` / `last_seen_at` / `confidence`）由触发器增量维护 + `magpie recompute` 全量重建。**两者结果必须一致**（测试断言）。
- **压实** `magpie compact --before 90d`：把每个 `(subject, source)` 在 N 天前的多条贡献合并成一条（保留 MIN/MAX 时间与计数），并顺带把无引用 evidence 转 `hash_only`。
  - 代价明说：压实后**旧的作业不能再逐条 undo**。所以压实只处理 N 天前，近期作业永远可 undo。
- `is_live` 必须物化。否则每次列表查询都要 `EXISTS (SELECT 1 FROM contributions ...)`，这在 2500 万行上是灾难。
  > 这解决了一个隐蔽问题：查询"活着的观测"是**每一条列表 SQL 的隐含条件**，不能靠相关子查询。

---

## 3. undo：真正的可回滚机制

### 3.1 算法（一个事务内）

```
undo(job J):
  1. 前置检查：J.status ∈ {ok, failed}；J.kind ∈ {import, pipeline, reparse}
             若 J.kind = merge  → 提示改用 unmerge
             若 J.kind = undo   → 拒绝（不可撤销的撤销）
             若有作业 superseded_by = J  → 拒绝，要求先撤销那个更晚的作业
  2. 受影响集合 S = SELECT DISTINCT (subject_kind, subject_id)
                    FROM contributions WHERE job_id = J
  3. 若 J.kind = reparse：解除本作业造成的 supersede
       UPDATE contributions SET superseded_at=NULL, superseded_by_job_id=NULL
        WHERE superseded_by_job_id = J
  4. DELETE FROM contributions WHERE job_id = J
  5. 重算 S 中每个 subject 的派生字段（§3.2）
  6. 清理：S 中 is_live=0 且 origin='tool' 的 subject → 物理删除；
           origin='manual' 的 → 保留并置 is_live=0（人工记录永不消失）
  7. 无引用的 evidence → 删除；仍被引用的保留
  8. J.status='undone'；写一条 kind='undo' 的新 job 作为审计载体；
     activity_log 记 actor/action
```

### 3.2 重算规则（唯一定义，触发器与 recompute 共用同一段逻辑）

| 字段 | 定义 |
|---|---|
| `is_live` | 存在 `superseded_at IS NULL` 的贡献 → 1，否则 0 |
| `source_count` | `COUNT(DISTINCT source)`，只看活贡献 |
| `job_count` | `COUNT(DISTINCT job_id)`，只看活贡献 |
| `first_seen_at` | `MIN(observed_at)`，活贡献 |
| `last_seen_at` | `MAX(observed_at)`，活贡献 |
| `confidence` | 由 `source_count` + 权威性 + 一致性派生，见 §3.3 |

**`seen_count` 字段从模型中删除** —— 它的歧义（"见到几次"还是"几个来源见过"）正是 undo 说不清的根源。

### 3.3 confidence 必须是可解释的离散规则

不许拍浮点数。给定同一 `(entity, type)` 下的多个候选值：

```
无活贡献                     → none
仅 1 个来源                  → low
≥2 个独立来源且取值一致       → medium
≥2 个独立来源一致 且 含权威源  → high
存在多个不同取值（跨源不一致） → conflict   （同时产生一条 Inbox 项）
来源含 'manual'              → manual     （人工裁决，最高，且不再提示冲突）
```

- **权威源（离线语境下）= nmap `-sV` 的 CPE / 命中离线指纹且 evidence 摘录非空 / 用户人工确认。** 没有"厂商 API"这种东西（零出站），所以权威性只能来自**可复核的证据质量**。
- v3 的 `confidence REAL` 改为 `confidence TEXT` 枚举。浮点数在"两个来源一致算多高"这种问题上无法自证。
- **一致 vs 冲突的区分是关键**：多源报同一个值 → 提升置信；多源报不同值 → `conflict` + Inbox。这正是你要的"AI 应该知道 confidence=high, evidence: nmap/httpx/fingerprint"。

### 3.4 作业类型与 undo 边界（你没覆盖的边界）

| kind | 可 undo？ | 说明 |
|---|---|---|
| `import` / `pipeline` | ✅ 完全可回滚 | 主路径 |
| `reparse` | ✅ 可回滚 | 靠 §3.1 第 3 步解除 supersede，**不需要快照** |
| `merge` | ⚠️ 用 `unmerge` | 合并时保留 `relations(rel='merged')`，unmerge 按记录反向拆回 |
| `undo` | ❌ 拒绝 | 撤销的撤销 = 重跑导入，语义更清晰 |
| `gc` / `compact` | ❌ 不可逆 | 破坏性，`--dry-run` + 二次确认 + activity_log 强制留痕 |

另外三条硬规矩：

1. **删除的 id 永不复用**（避免悬空引用）。SQLite 默认 `INTEGER PRIMARY KEY` 会复用 `max(rowid)+1`，所以关键表用 `AUTOINCREMENT` 或维护 `db_meta.last_*_id`。这是 v3 没考虑的实际坑。
2. **undo 自身也是一个 job**（`kind='undo'`），所以审计链完整：谁在什么时候撤销了哪次导入。
3. **写者唯一**：undo 必须与服务内写队列串行，禁止与正在运行的作业并发。

---

## 4. 身份稳定性与规范化版本（你的 ⑮）

### 4.1 每条身份决策都要留痕

```sql
-- entities 上新增
identity_rule       TEXT,     -- 例：'domain:punycode_lower_dot' / 'endpoint:path_template_fold'
identity_version    INTEGER,  -- 规则集版本，见 §4.3
identity_confidence REAL,
raw_value           TEXT      -- 原始字符串，用于 explain 与 rekey
```

`magpie explain <entity>` 必须能回答："这两条原始值为什么被归成一个实体"，并列出参与命名的全部 raw_value。

### 4.2 endpoint 折叠：用一条硬规则挡住误折叠

你的例子 `/report/2024` 是对的。规则改为**保守折叠**：

| 规则 | 条件 |
|---|---|
| `{id}` | 纯数字段，**且同一路径位置已观察到 ≥2 个不同数值** |
| `{uuid}` | 标准 UUID 形态（自证性强，单例即可折叠） |
| `{hex}` | ≥16 位十六进制（自证性强） |
| `{date}` | `YYYY-MM-DD` / `YYYYMMDD` 且位置不在末段（末段很可能是业务 ID 而非日期） |
| **其余一律不折叠** | 段数不足、只见过一次、无法归类 → 保留原始段 |

关键点：**"只见过一次"不折叠**。`/report/2024` 第一次出现时 key 就是 `/report/2024`；只有当 `/report/2025` 也出现，才升级折叠为 `/report/{id}` —— 而升级意味着 key 变化，这就是为什么需要 §4.3。

### 4.3 规则变更必须可迁移（rekey）

身份规则会演进（今天的保守规则明天可能放开）。所以：

- `db_meta.identity_version` 记录建库时的规则版本；`core/identity.py` 内置版本号。
- 版本不一致时：`magpie doctor` 报警，`magpie rekey --dry-run` 输出"将合并 X 组、拆分 Y 组"，确认后执行迁移（先自动备份）。
- **禁止静默重算 key**。改了规则却不 rekey，会让同一资产在库里以两个身份并存 —— 这是最难排查的一类脏数据。

### 4.4 host 归属：不要把身份交给 Host 头

endpoint 的宿主一律用 `entity_key(domain|ip)`，**不用原始 Host 头**。当出现 `Host` / `X-Forwarded-Host` / SNI 三者不一致时：

- 不改变身份；
- 生成 `observations(type='host_mismatch')`；
- 进 Inbox（这本身就是高价值信号：可能是反代、可能是 SSRF 线索）。

方案（scheme）不进身份：`http://x/a` 与 `https://x/a` 是同一接口，`observed_schemes` 记成观测。

---

## 5. 状态语义（P0-3）

### 5.1 时间字段必须分清（你的 ⑪/我补的 P1-1）

diff 全线错乱的根源是只有一个时间。定死四个：

| 字段 | 含义 | 来源 |
|---|---|---|
| `observed_at` | 数据**被观测**的时间 | 工具输出里的时间戳 > 源文件 mtime > 导入时刻（并把用了哪个记进 `observed_at_source`） |
| `ingested_at` | 数据**落库**的时间 | 作业运行时刻 |
| `first_seen_at` / `last_seen_at` | 由 `observed_at` 聚合派生 | §3.2 |

意义：你**补导入三个月前的扫描结果**时，diff 不该判定"今天新增了 500 个资产"。v3 只有导入时间，这个场景必然错。

### 5.2 三个正交概念，不再用一个 `status` 表达

v3 的 `status ∈ {unknown, live, dead, merged, orphan}` 把三件事挤在一个字段里。拆开：

| 概念 | 字段 | 取值 | 谁能改 |
|---|---|---|---|
| 生命周期 | `entities.lifecycle` | `unknown` / `active` / `inactive` / `merged` / `archived` / `orphan` | **人工确认**，或"全量作业 + Inbox 提议" |
| 观测新旧 | `last_seen_at`（派生） | 时间戳 | 系统 |
| 作业覆盖度 | `jobs.coverage` + `coverage_scope` | `full` / `partial` / `unknown` | 管线声明 |

**推断规则（写死）：**

```
候选 inactive 仅当：
  jobs.coverage = 'full'
  且 jobs.coverage_scope 与资产的 scope 匹配
  且该资产在本作业中零贡献
→ 生成 Inbox 项「候选 inactive」，附证据（上次观测时间、本作业的覆盖声明）
→ 人确认后才改 lifecycle
```

**默认永不自动置死。** `last_seen 3 天前` 只表达"最近一次观测在 3 天前"，UI 就显示这个，不显示"dead"。

`coverage` 必须是**管线显式声明**（`pipeline` YAML 里 `coverage: full` + `scope:`），不能猜——猜出来的覆盖度会让整个状态推断不可信。

### 5.3 auth_state 重定义（你的 ④，加上离线约束的推论）

Magpie 不发请求 ⇒ **`tested_*` 不可能由 Magpie 产生**。它只能来自两组观测的对比：

```
同一 endpoint 上：
  无凭据请求 → 200，且有凭据请求 → 200   → observed_public
  有凭据请求 → 200，无凭据请求 → 401/403 → observed_authenticated
  无凭据 200 与 有凭据 401 同时存在        → inconsistent   （最值得看的一种）
  只有带凭据的请求                        → unknown
  外部工具明确做了匿名测试                 → tested_unauthenticated / tested_authenticated
  没有任何请求证据                        → unknown
```

取值集合：`unknown | observed_public | observed_authenticated | tested_unauthenticated | tested_authenticated | inconsistent`。

两条硬规矩：

1. **`observed_public` 的前置条件是"该请求可证明未携带任何凭据"**（无 Cookie / 无 Authorization / 无自定义 session 头）。否则只能是 `unknown`。这条把 v3 那句"无 401/403 = 未鉴权"从根上修掉。
2. **UI 与导出禁止出现"未鉴权"字样**，只允许显示：`未知` / `观察到无需凭据` / `观察到需认证` / `匿名测试通过` / `已认证访问` / `结果不一致`。文案写进 i18n 词条，用一个测试断言禁止旧措辞回归。

`tested_*` 的来源正是 Burp 系（Autorize / 两条 HTTP History 对比）与 nuclei 的鉴权类模板 —— 这跟你 ⑰ 的上下游判断自然对齐。

### 5.4 范围（scope）也属于状态

`in_scope` 由 `projects.scope_patterns` 计算，属于**系统管理字段**：改 scope 时必须提供 `magpie scope resync`（先 `--dry-run` 展示将变化的资产数）。不要让 `in_scope` 变成"导入时随手写、之后永远陈旧"的字段。

---

## 6. manual 保护（P0-4）

### 6.1 字段级冻结，而不是新表

```sql
-- entities / observations / relations 三张表统一新增
manual_fields TEXT NOT NULL DEFAULT '[]',   -- JSON 数组：["review_status","display_name"]
```

三条规则：

1. **`store.upsert_*()` 永不写 `manual_fields` 里的字段**（导入、重解析、reparse、merge 都受此约束）。
2. 人工操作（CLI / GUI / agent 通过 API）改字段时，`store.set_manual()` 自动把字段加入 `manual_fields` 并写 `activity_log`。
3. `magpie reset <entity> <field>` 释放冻结并从工具侧恢复（释放动作本身也进 activity_log）。

> 为什么不用 `entity_manual_patch` 表？因为它会让每一次读都要 merge 两张表，并且"当前值是什么"不再唯一 —— 对一个强调"事实唯一"的库，这是负收益。冻结字段列表以极低成本换到了同样的效果。

### 6.2 字段归属三分表（写进代码注释与文档，测试强制）

| 归属 | 字段 |
|---|---|
| **tool** | `subtitle`、`attrs.*`、`auth_state`、`confidence`、派生时间字段 |
| **manual** | `display_name`、`review_status`、`lifecycle`（人工确认）、`entity_tags.*`、vuln 的 `severity` / `state`、`note` |
| **system** | `id`、`project_id`、`kind`、`entity_key`、`identity_*`、`in_scope`、`created_at` |

典型回归（你举的例子）：手工设 `service A.review_status = done` → 连续导入 3 次 + 重解析 1 次 → 仍为 `done`。

### 6.3 secret：hash 不是终点（你的 ⑤）

保留 v3 的安全默认，但补上**受控复现路径**：

```
Magpie DB（默认，永远存在）
  secret 实体：value_hash / masked / rule / locator / secret_ref
  明文：不含

Secret Vault（可选，默认关闭，独立文件）
  <data-dir>/vault/secrets.enc
  加密：Windows → DPAPI（ctypes，零依赖，绑定当前用户）
        其它平台 → magpie[vault]（cryptography，AES-GCM）
  解锁：会话级，超时自动上锁
  每次 reveal：写 activity_log（谁、何时、看了哪条）
```

配套规矩：

- **导出 / agent context 默认只给 `masked` + `secret_ref`**，明文必须显式 `--reveal`。
- `--reveal` 的产物**不进终端历史**（CLI 走文件或剪贴板，并提示）。
- `secret` 实体只保留 hash 时，"两条记录是不是同一个 token"依然可查（hash 相等）—— 这是 keep-hash 的正当价值，也是它能单独存在的原因。

---

## 7. 不被信任的输入（P1-2）

### 7.1 路径沙箱

```sql
CREATE TABLE input_roots (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,        -- 规范化后的绝对路径
  label TEXT,
  added_at TEXT NOT NULL, added_by TEXT NOT NULL
);
```

规则（`core/sandbox.py`，唯一入口 `resolve_under_root()`）：

1. 所有外部路径（CLI `--paths`、API `paths/dir`、pipeline `source_glob`、`--out`）必须先 `resolve(strict=False)`，再断言位于某个 root 之下。
2. 默认 roots = `<data-dir>`；用户需显式 `magpie root add <path>`（记 activity_log）。
3. pipeline 里的 `source_glob` 相对 **root 声明**解析，不相对 cwd。
4. 拒收：UNC（除非 root 本身是 UNC）、`\\?\` / `\\.\` 设备路径、NTFS ADS（`file.txt:stream`）、解析后逃出 root 的符号链接、Windows 下用 `normcase` 做大小写不敏感比较。
5. **agent 生成的 pipeline YAML 与用户手写的一视同仁**（走同一校验）；YAML 在**加载时与运行时各校验一次**。
6. `magpie doctor` 的 `--json` 输出里显式列出当前 roots，让越权面可见。

### 7.2 本地服务的额外两防（你漏了）

| 风险 | 防护 |
|---|---|
| DNS rebinding：恶意网页解析到自己域名 → 打 `127.0.0.1:port` | 校验 `Host` / `Origin` 必须是 `127.0.0.1[:port]` / `localhost[:port]`，否则 403 |
| token 泄漏进 Referer / 代理日志 | 一次性 token 通过 URL **fragment**（`#t=...`）传给前端，前端读后立即用 header 携带；fragment 不进 Referer |
| 任意文件读 | API 不接受绝对路径，只接受 root 内的相对路径 + root id |

---

## 8. 离线保证的三层强制（P1-3，含 Windows 现实）

| 层 | 手段 | 覆盖什么 |
|---|---|---|
| **L0 依赖审查** | 每个 Tier C 依赖人工过一遍，禁止引入会发起网络的设计；CI 输出依赖清单 | 供应链 |
| **L1 静态** | AST 扫描：`magpie/core/**` 与 `magpie/adapters/**` 禁止 `socket` / `urllib.request` / `http.client` / `requests` / `ftplib` / `smtplib` / **`subprocess`**（防止"偷偷调 curl/tshark"） | 直接调用 |
| **L2 运行时** | pytest 全局 fixture 猴补 `socket.socket.connect` / `create_connection` / `getaddrinfo`：只允许 `127.0.0.1`/`::1`/AF_UNIX，其余直接抛 `NetworkAccessDenied` | **间接/传递依赖** |
| **L3 隔离** | Linux/macOS：`unshare -n`（或容器 `--network=none`）跑全量集成测试<br>**Windows：`unshare` 不存在** → 用 `netsh advfirewall` 临时出站阻断规则包裹测试进程，或要求在 WSL2 里跑 L3；两者都不可用时，**L3 标记为"需人工验证"并写明配方**，不许假装通过 | 运行环境 |

> 这一层之所以必须写清 Windows 的替代方案：否则"CI 断言零网络"会退化成一句永远为真的空话 —— 你在 ⑧ 里担心的正是这个。

---

## 9. 依赖、SQLite 门禁与跨平台（P1-4）

### 9.1 分层纯度（对 ⑨ 的修正）

我不放松"纯标准库"，而是**按压力点分层**，因为真正的成本只集中在两处：

| 层 | 范围 | 依赖 |
|---|---|---|
| **A（必须纯标准库）** | `core/model`、`identity`、`normalize`、`provenance`、`evidence`、`query`、`diff`、`scope`、`sandbox` | 仅标准库 |
| **B（标准库，但真实依赖是 SQLite 特性）** | `core/storage`、`core/search` | `sqlite3` + **特性门禁** |
| **C（可选 extras，lazy import）** | YAML 版 OpenAPI、pcap、mitmproxy flows、vault 加密 | `magpie[api-yaml]` / `[traffic]` / `[vault]` |

事实依据：**httpx=JSONL、nuclei=JSONL、nmap=XML、HAR=JSON、Postman=JSON、tshark 导出=JSON/CSV、fingerprints=YAML（可换 JSON）** —— 主流格式全在标准库射程内。所以"纯标准库"的代价远小于你估计的；真正会绊倒你的是 SQLite 编译特性（⑩）。

### 9.2 `check_sqlite_features()`：启动即门禁

```python
REQUIRED = {           # 硬下限
  "sqlite": (3, 35),   # trigram 分词器需要 3.34+，取 3.35 留余量
  "json1": True,       # 3.38+ 内置
  "wal": True,
}
OPTIONAL = {
  "fts5": True,        # 检测法：CREATE VIRTUAL TABLE temp.t USING fts5(x) 后 DROP
  "trigram": True,     # 检测法：同上加 tokenize='trigram'
}
```

- 结果写入 `db_meta.search_impl`（`'fts5-trigram'` / `'like'`），**保证同一库在不同机器上行为可复现**。
- FTS 是派生索引：换了环境检测不到就 `DROP` 重建或降级 `LIKE`，绝不因为缺 FTS5 而拒绝启动（缺 WAL / json1 则明确报错并给出升级指引）。
- `magpie doctor` 打印特性矩阵与三平台最小版本建议。

### 9.3 跨平台数据目录（③）

| 平台 | 默认 |
|---|---|
| Windows | `%APPDATA%\Magpie` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/magpie` |
| macOS | `~/Library/Application Support/Magpie` |

优先级：`--data-dir` > `MAGPIE_DATA_DIR` > 平台默认。用 ~20 行标准库实现，不引 `platformdirs`。

**v1 目标平台是 Windows**（打包 + WebView2），但路径、编码、路径分隔、大小写敏感性都不得写死平台 —— 否则未来移植要重写存储层。

---

## 10. v3 文档需修正的措辞与数字

| v3 位置 | 问题 | 修正 |
|---|---|---|
| §0 / §7 标题 | "五个镜头"，表里 6 个 | 统一的 6 个 canonical id：`graph` / `assets` / `services` / `endpoints` / `flows` / `vulns`。文档、路由、前端菜单、OpenAPI 全部用 id，不再写中文计数 |
| §7.5 | "1000 节点 55fps"当硬指标 | 移入**软预算**；硬指标改为正确性指标（§11.1） |
| §5.5 | "本轮没看到 → live→dead" | 删除。改为 coverage + Inbox 提议（§5.2） |
| §4.3 | "未鉴权（无 401/403）" | 删除该措辞与判定，改 auth_state（§5.3） |
| §3.4 | `status` 五值混装 | 拆为 `lifecycle` + 派生 `last_seen_at` + `jobs.coverage` |
| §3.4 | `entity_layout.view` 注释里的 lens 名 | 与 §10 的 6 个 id 对齐 |
| §1 / §9 | `%APPDATA%` 单平台 | 三平台（§9.3） |
| §13 | 阶段 0 = 性能验证 | 改为正确性三根钉子（§11.2） |
| 全文 | "GUI 常驻 < 150MB" 与"服务 < 120MB"并存 | 只保留后者的拆分口径 |
| §3.4 | `observations.source` / `relations.source` | 删除（多源由 contributions 承载） |
| §3.4 | `seen_count` | 删除（歧义字段） |
| §3.4 | `confidence REAL` | 改 `confidence TEXT` 枚举（§3.3） |

---

## 11. 路线图与指标修订

### 11.1 指标分类重排

| 类别 | 指标 | 性质 |
|---|---|---|
| **正确性（硬）** | ① 任意作业序列 + 全部 undo 后，逐行状态 == 初始状态<br>② 100% 的 tool 写入行都有 ≥1 活贡献且 `evidence_id` 非空<br>③ 触发器与 `recompute` 结果逐字段一致<br>④ 孤儿贡献数 == 0<br>⑤ 连续导入 N 次观测行数不变<br>⑥ manual 字段在导入/重解析后逐字段不变 | **不达标不许进阶段 2** |
| 性能（次硬） | 导入 10 万观测 < 60s；100 万观测可用；`magpie app` RSS < 120MB | 软预算，超标先记录再优化 |
| UI（软） | 画布 1000 节点可交互（≥30fps 即可）；首屏 < 2s | 不影响发布，只影响体验 |

### 11.2 阶段 0 改为「正确性三根钉子」（3–5 天）

1. **模型正确性**：造两个作业交叉 upsert 同一 observation，undo 其一 → 断言行保留且 `source_count` 正确；全部 undo → 断言回到空库。
2. **溯源完整性**：随机作业序列（属性测试）→ 断言 §11.1 的 ②③④ 恒成立。
3. **真实数据闭环**：真实 subfinder + httpx 输出 → 导入 → diff → `reparse` → `undo` → 再导入，逐项人工核对（**这一步做完才算验证了 v3.1 的模型**）。

画布性能验证移到阶段 1 末尾，作为软预算测量。

### 11.3 冻结清单（呼应你的 ⑲）

在 P0-1 ~ P0-5 全绿之前，**冻结**：

- 新增 entity kind（14 种不再增加）
- 新增 lens / 页面 / 图表
- 新增 parser（除 `domainlist` / `httpx` 外）
- 新增 MCP tool
- 画布的任何编辑能力（编排模式的 UI 也冻结，先只留 YAML）

---

## 12. 完整 DDL v3.1（替换 v3 §3.4）

```sql
PRAGMA foreign_keys=ON;  PRAGMA journal_mode=WAL;  PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;  PRAGMA temp_store=MEMORY;

-- ========== 0. 元信息 ==========
CREATE TABLE db_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
-- 约定键：schema_version / identity_version / search_impl / app_version_at_creation
--         sqlite_version_at_creation / coverage_policy_version

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY, app_version TEXT NOT NULL, applied_at TEXT NOT NULL
);

-- ========== 1. 输入根白名单 ==========
CREATE TABLE input_roots (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  label TEXT,
  added_at TEXT NOT NULL, added_by TEXT NOT NULL
);

-- ========== 2. 项目 ==========
CREATE TABLE projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  scope TEXT,
  scope_patterns TEXT NOT NULL DEFAULT '[]',
  scope_resynced_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ========== 3. 原始材料 ==========
CREATE TABLE raw_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  encoding TEXT,
  format TEXT,
  storage TEXT NOT NULL CHECK (storage IN ('archived','external','purged')),
  stored_path TEXT,
  head_sample TEXT,                       -- 前 64KB，仅供嗅探
  observed_at TEXT,                       -- 文件 mtime 或工具内时间
  ingested_at TEXT NOT NULL,
  UNIQUE (project_id, sha256)
);

-- ========== 4. 作业（含 coverage） ==========
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('import','pipeline','reparse','undo','merge','unmerge','gc','compact','recompute')),
  pipeline TEXT,
  pipeline_version TEXT,
  coverage TEXT NOT NULL DEFAULT 'unknown' CHECK (coverage IN ('full','partial','unknown')),
  coverage_scope TEXT,                    -- JSON：{"patterns":["*.a.com"],"note":"全量子域扫描"}
  status TEXT NOT NULL DEFAULT 'running'
         CHECK (status IN ('running','ok','failed','cancelled','undone')),
  started_at TEXT NOT NULL, finished_at TEXT,
  stats TEXT, error TEXT,
  actor TEXT NOT NULL DEFAULT 'cli',
  superseded_by INTEGER REFERENCES jobs(id)   -- 旧作业（如 reparse 的上一轮）指向新作业
);

CREATE TABLE job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  at TEXT NOT NULL, level TEXT NOT NULL, phase TEXT, message TEXT NOT NULL,
  raw_file_id INTEGER, lineno INTEGER, payload TEXT
);

-- ========== 5. 证据 ==========
CREATE TABLE evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  raw_file_id INTEGER REFERENCES raw_files(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK (kind IN ('line','json_object','jsonl_line','http_message','xml_node',
                                     'certificate','packet','manual','rule_hit')),
  locator TEXT NOT NULL,
  locator_hash TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  excerpt_hash TEXT NOT NULL,
  excerpt_state TEXT NOT NULL DEFAULT 'full' CHECK (excerpt_state IN ('full','hash_only')),
  created_at TEXT NOT NULL,
  UNIQUE (raw_file_id, kind, locator_hash, excerpt_hash)
);
CREATE INDEX idx_evidence_rawfile ON evidence(raw_file_id);

-- ========== 6. 实体 ==========
CREATE TABLE entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  raw_value TEXT,
  display_name TEXT,
  subtitle TEXT,
  lifecycle TEXT NOT NULL DEFAULT 'unknown'
    CHECK (lifecycle IN ('unknown','active','inactive','merged','archived','orphan')),
  auth_state TEXT CHECK (auth_state IS NULL OR auth_state IN
    ('unknown','observed_public','observed_authenticated',
     'tested_unauthenticated','tested_authenticated','inconsistent')),
  review_status TEXT NOT NULL DEFAULT 'todo'
    CHECK (review_status IN ('todo','testing','done','skipped','submitted')),
  in_scope INTEGER,
  identity_rule TEXT,
  identity_version INTEGER,
  identity_confidence REAL,
  attrs TEXT NOT NULL DEFAULT '{}',
  manual_fields TEXT NOT NULL DEFAULT '[]',
  -- 派生缓存（§3.2；禁止手写）
  is_live INTEGER NOT NULL DEFAULT 1,
  source_count INTEGER NOT NULL DEFAULT 0,
  job_count INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'none'
    CHECK (confidence IN ('none','low','medium','high','conflict','manual')),
  first_seen_at TEXT, last_seen_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (project_id, kind, entity_key)
);
CREATE INDEX idx_ent_kind    ON entities(project_id, kind, lifecycle) WHERE is_live=1;
CREATE INDEX idx_ent_review  ON entities(project_id, review_status)   WHERE is_live=1;
CREATE INDEX idx_ent_key     ON entities(entity_key);
CREATE INDEX idx_ent_lifecycle ON entities(project_id, lifecycle);

CREATE TABLE entity_tags (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  set_by TEXT NOT NULL, set_at TEXT NOT NULL,
  PRIMARY KEY (entity_id, tag)
);
CREATE INDEX idx_tags_tag ON entity_tags(tag);

-- ========== 7. 观测 ==========
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  value TEXT NOT NULL,
  severity TEXT,
  state TEXT CHECK (state IS NULL OR state IN
    ('suspected','confirmed','false_positive','reported','fixed')),
  auth_state TEXT,
  extra TEXT,
  origin TEXT NOT NULL DEFAULT 'tool' CHECK (origin IN ('tool','manual')),
  manual_fields TEXT NOT NULL DEFAULT '[]',
  is_live INTEGER NOT NULL DEFAULT 1,
  source_count INTEGER NOT NULL DEFAULT 0,
  job_count INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'none',
  first_seen_at TEXT, last_seen_at TEXT,
  superseded_at TEXT,
  UNIQUE (entity_id, type, value)
);
CREATE INDEX idx_obs_entity_type ON observations(entity_id, type) WHERE is_live=1;
CREATE INDEX idx_obs_type_value  ON observations(type, value)     WHERE is_live=1;
CREATE INDEX idx_obs_type_sev    ON observations(type, severity)  WHERE is_live=1;

-- ========== 8. 关系 ==========
CREATE TABLE relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel TEXT NOT NULL,
  extra TEXT,
  origin TEXT NOT NULL DEFAULT 'tool' CHECK (origin IN ('tool','manual')),
  manual_fields TEXT NOT NULL DEFAULT '[]',
  is_live INTEGER NOT NULL DEFAULT 1,
  source_count INTEGER NOT NULL DEFAULT 0,
  job_count INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'none',
  first_seen_at TEXT, last_seen_at TEXT,
  superseded_at TEXT,
  UNIQUE (src_id, dst_id, rel)
);
CREATE INDEX idx_rel_src ON relations(src_id, rel) WHERE is_live=1;
CREATE INDEX idx_rel_dst ON relations(dst_id, rel) WHERE is_live=1;

-- ========== 9. ★ 贡献流 ==========
CREATE TABLE contributions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('entity','observation','relation')),
  subject_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  source_version TEXT,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  observed_at TEXT NOT NULL,
  observed_at_source TEXT NOT NULL
    CHECK (observed_at_source IN ('tool','file_mtime','source_meta','ingest_time','manual')),
  ingested_at TEXT NOT NULL,
  superseded_by_job_id INTEGER REFERENCES jobs(id),
  superseded_at TEXT,
  UNIQUE (job_id, subject_kind, subject_id, source, evidence_id, observed_at)
);
CREATE INDEX idx_contrib_subject ON contributions(subject_kind, subject_id) WHERE superseded_at IS NULL;
CREATE INDEX idx_contrib_job     ON contributions(job_id);
CREATE INDEX idx_contrib_evidence ON contributions(evidence_id);

-- ========== 10. 画布布局 ==========
CREATE TABLE entity_layout (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  view TEXT NOT NULL CHECK (view IN ('graph','assets','services','endpoints','flows','vulns')),
  x REAL NOT NULL, y REAL NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  collapsed INTEGER NOT NULL DEFAULT 0,
  parent_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY (entity_id, view)
);

-- ========== 11. 类型注册表与审计 ==========
CREATE TABLE kind_registry (
  kind TEXT PRIMARY KEY, label TEXT NOT NULL, category TEXT NOT NULL,
  icon TEXT, color TEXT, display_template TEXT, identity_rule TEXT,
  facet_types TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE activity_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,                 -- gui | cli | mcp | system
  action TEXT NOT NULL,                -- set_manual | reset_manual | merge | unmerge |
                                       -- undo | root_add | reveal_secret | export_full | gc | compact
  target_kind TEXT, target_id INTEGER,
  detail TEXT
);
CREATE INDEX idx_activity_at ON activity_log(at);

-- ========== 12. 待裁决队列（冲突/提议都落这里） ==========
CREATE TABLE inbox_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN
    ('parser_ambiguous','value_conflict','candidate_inactive','out_of_scope',
     'unregistered_host','possible_merge','host_mismatch','auth_inconsistent')),
  subject_kind TEXT, subject_id INTEGER,
  payload TEXT NOT NULL,               -- JSON：候选、证据 id 列表、提议值
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved','dismissed')),
  created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, resolution TEXT
);
CREATE INDEX idx_inbox_open ON inbox_items(project_id, state, kind);
```

> `kind_registry` 的 kind 白名单、`relations.rel` 的动词白名单、`observations.type` 的白名单仍以 `core/kinds.py` / `core/rels.py` 为权威，由 CHECK + CI 断言双重约束。

---

## 13. 针对五个 P0 的可执行验收（写测试用）

| # | 断言 | 类型 |
|---|---|---|
| 1 | 作业 A、B 交叉 upsert 同一 observation → `undo A` 后该行仍在，`source_count` 递减，`last_seen_at` 回到 B 的时间 | 单元 |
| 2 | 随机作业序列（含 reparse / merge / unmerge）后全部 undo → 全表逐行等价于初始状态 | 属性测试 |
| 3 | 任意时刻：`SELECT COUNT(*) FROM contributions c WHERE NOT EXISTS(对应 subject)` == 0 | 属性测试 |
| 4 | `SELECT COUNT(*) FROM observations WHERE origin='tool' AND id NOT IN (SELECT subject_id FROM contributions WHERE subject_kind='observation' AND superseded_at IS NULL)` == 0 | 属性测试 |
| 5 | 手动触发器维护的派生字段 vs `magpie recompute` 的结果：逐字段相等 | 属性测试 |
| 6 | 同一文件连续导入 3 次：observations / relations 行数不变，`job_count` == 3 | 单元 |
| 7 | 设 `review_status=done` + `display_name=X` → 导入 3 次 + reparse 1 次后仍为原值；`reset` 后回到工具值 | 回归 |
| 8 | `coverage='partial'` 的作业中未观测到的资产 → `lifecycle` 不变且**无** Inbox 项；`coverage='full'` 且 scope 匹配 → 恰好 1 条 `candidate_inactive` | 单元 |
| 9 | 只有带凭据的请求 → `auth_state='unknown'`；无凭据 200 + 带凭据 401 → `inconsistent` | 单元 |
| 10 | 无凭据请求携带了 Cookie → 不得判为 `observed_public` | 单元 |
| 11 | pipeline `source_glob` 指向 root 外 / `..` / UNC / ADS → 拒绝且不读取任何文件 | 安全 |
| 12 | 恶意页面式请求（`Host: evil.com`）打本地 API → 403 | 安全 |
| 13 | L2 网络守卫下跑全量测试：除 loopback 外零连接 | CI |
| 14 | 仅在单条观测上出现的 `/report/2024` 不被折叠为 `/report/{id}` | 单元 |
| 15 | `identity_version` 变更但未 rekey → `doctor` 报警且拒绝写入新 key | 单元 |
| 16 | `compact` 后：旧作业拒绝 undo 并给出明确错误；近期作业仍可 undo | 单元 |
| 17 | UI 词条不得包含"未鉴权"字样 | 文案测试 |

---

## 14. 修订后的架构主线

```
                     外部安全工具（用户在外部运行，Magpie 不调用）
        subfinder / httpx / nmap / nuclei / dirsearch / HAR / Burp / tshark 导出
                                   │  文件
                                   ▼
                         ┌──────────────────┐
                         │  Sandbox + Sniff │   路径白名单 / 嗅探仲裁
                         └────────┬─────────┘
                                  ▼
                              Normalize           身份规则 + 版本
                                  │               （identity_version 可迁移）
                                  ▼
                    ┌──────────────────────────┐
                    │  Ingest（写者唯一）        │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  Provenance + Evidence    │  ← v3.1 的地基
                    │  contributions / evidence │
                    └────────────┬─────────────┘
                                 ▼
        ┌────────────────────────────────────────────────┐
        │                Security DB（SQLite）            │
        │  Entity  Observation  Relation  Evidence        │
        │  Contribution  Job(coverage)  Inbox  Activity    │
        │  派生缓存：is_live / source_count / confidence    │
        └───────────────────────┬────────────────────────┘
                                ▼
                 Query / Diff / Lens / Context / Doctor
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
            GUI               CLI            Agent / MCP
        （可视化层）      （agent 面）      （只读为默认）
```

---

## 15. 下一步

1. 你确认本稿的五个 P0 设计（尤其 §2 的 contributions + evidence 与 §3 的 undo 算法）。
2. 我据此**回改 v3 正文**（§10 的修正清单逐条落地），产出 v3.2 正式版计划书。
3. 然后才写代码：阶段 0 的「正确性三根钉子」——先 `schema` + `store` + `provenance` + 属性测试，GUI 一行都不写。

---

## 附录 A · 本稿新增但 v3 与你的评审都没有的三点

1. **时间语义三分离**（`observed_at` / `ingested_at` / `observed_at_source`）：不区分则补导入历史数据时 diff 全错。
2. **contributions 增长与压实策略**（`is_live` 物化、`compact --before 90d`、evidence 降级为 `hash_only`）：这是唯一会炸存储的地方，2500 万行级别必须有策略。
3. **`inbox_items` 独立成表**：v3 只说"Inbox 页"，没定义它是什么。冲突、候选 inactive、未知 host、host mismatch、auth inconsistent、越界资产都必须落在同一张队列表上，否则每类各写一套逻辑，很快就乱。

## 附录 B · 与 BurpMcpEnhance 的边界（采纳你的 ⑰）

**Magpie 的定位：可查询的安全事实库。** 它不产生流量、不发起请求、不跑扫描器。

```
                    AI Agent
                        │  MCP / CLI --json
                        ▼
              ┌───────────────────┐
              │      Magpie       │   统一事实 · 关联 · 证据 · 差异 · 上下文
              └─────────┬─────────┘
                        │  facts-json（导入契约，append-only）
      ┌─────────────────┼───────────────────┐
      ▼                 ▼                   ▼
 BurpMcpEnhance      Nuclei              Recon 工具
 (流量 / 请求响应)   (漏洞)              (资产 / 端口 / 指纹)
```

- **交换格式**：`magpie import --format facts-json`，接受一个 append-only 的 JSON 数组，每项含 `kind / key / observations / relations / evidence / observed_at / source`。这是与 BurpMcpEnhance 的稳定契约，比共享 SQLite 安全（保持 Magpie 写者唯一）。
- **对称性**：入口有 `facts-json`，出口有 `--json` 与 `/agent/context`。两端都是契约，都做快照测试。
- **不共享数据库**：BurpMcpEnhance 有它自己的库与 MCP；两边通过文件交换事实，谁也不改对方的数据。
