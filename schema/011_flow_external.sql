-- =============================================================================
--  011_flow_external.sql —— 会话这个 kind、`involves` 这条关系，外加 4.1 的两件
--  结构性的事：`raw_files.custody`（谁持有原文）与 `inbox_items.kind` 的第八个
--  取值（发现未登记资产）。
--
--  与 004–010 同一条纪律：**只增不改**（红线 R4）。已应用的迁移被改动 →
--  校验和不匹配 → `migrate()` 直接报错。
--
--  ---------------------------------------------------------------------------
--  一、`raw_files.custody` —— 为什么不是往 `storage_state` 里加一个 `external`
--  ---------------------------------------------------------------------------
--  计划书 4.1 写的是 `storage='external'`（那是《数据模型修订稿 v3.1》的列名，
--  见 v3.1 §3 的 `storage TEXT CHECK (storage IN ('archived','external','purged'))`）。
--  v3.2 把这一列换成了 `storage_state`，取值是**原文持有状态的四级单调降级**
--  （present → archived → purged → missing，INV-17 的触发器守着它），而
--  "这份原文在谁手里"**不是**这条链上的一级：
--
--      present   = 原文可取得       ┐ 这两级说的是"它还在不在"
--      archived  = 原文已归档       ┘
--      purged    = 本工具放弃了它
--      missing   = 它不见了
--      **custody** = 它从来就不在我的手里，我只有一个路径指着它
--
--  把 `external` 塞进那条链会有两个说不通的后果：它的"降级顺序"排在哪一级后面？
--  而 `purge` 对外部文件**根本没有意义**（下面第三节写透了：本工具从不删除任何
--  文件，而外部文件连"本工具归档的副本"都不是）。所以这里加的是**正交的一列**：
--
--      custody ∈ {stored, external}      -- 原文由谁持有
--      storage_state ∈ {present,…}       -- 它还在不在
--
--  能力（`ops.evidence_capability`）从此是三个因素的函数：谁持有 × 还在不在 ×
--  这一行还留下什么。**这不是把问题复杂化，是把一句假话拆成两句真话** ——
--  一起拆掉的还有"外部文件也是 recoverable"这个结论（见 `ops.py` 的那一节）。
--
--  默认值是 `stored`，而**升级前的行一律留在默认值上**，包括那些用
--  `--archive external` 导进来、被当时那版 `archive_file` 记成 `present` 的行。
--  为什么不回填：库里**没有**记过这件事（外部文件的 locator_uri 就是源文件的
--  路径，归档文件的是内容寻址的 `<data-dir>/raw/<sha 前两位>/<sha256>`），
--  能拿来判的只有 locator 的形状 —— 那是**猜**，而猜错的方向有两边：把真归档
--  标成外部（它的可恢复性被低估），或者反过来（被高估）。这条信息从 011 起
--  才开始被记录，这句话本身比一个猜出来的回填诚实。
--
--  ---------------------------------------------------------------------------
--  二、`inbox_items.kind` 的第八个取值 —— 为什么必须重建整张表
--  ---------------------------------------------------------------------------
--  SQLite **没有** `ALTER TABLE … DROP CONSTRAINT`，改一个 CHECK 只能重建表
--  （建新表 → 拷行 → 删旧表 → 改名）。这一步的代价与风险都不小，所以先问：
--  能不能不新增 kind？
--
--  001 冻结的七个 kind 是一个**划分**（`query.HEALTH_CATEGORIES` 把每个 kind
--  归进体检页的一类，`test_health.py` 钉着"不漏不重"）。"流量里出现了一台此前
--  从未登记过的主机"要人做的裁决是**纳入还是忽略**，而七个里没有一个是这件事：
--
--      value_conflict      两个来源对同一个字段给出不同的值
--      orphan_subject      一个实体从来没有过任何贡献
--      identity_ambiguous  身份归属不唯一
--      merge_conflict      合并之后出现多值
--      evidence_missing    输入里出现了不认识的字段名（§5.6 的格式漂移）
--      scope_drift         资产落在项目 scope 之外
--      manual_required     嗅探仲裁拿不准这份输入该由谁解析（§5.4）
--
--  把它们中任何一个借来表示"发现新资产"，都会让体检页上那一类的数字开始说谎
--  （"低置信度 3"里有两条其实是新资产）。003 的文件头立下的准入纪律是**带齐
--  三件套才登记**，而不是"借一个近似的通道"；这条纪律在这里同样成立。
--
--  重建的**安全性**这次是逐条查过的（不是"应该没问题"）：
--    * 没有任何**表**用 REFERENCES 指向 inbox_items（只有 `reopened_from`
--      这个自引用，以及 views 里对它的读）—— 所以 `DROP TABLE` 不会打断
--      别的表的引用，也就不需要 SQLite 官方配方里"关掉 foreign_keys"那一步的
--      保护作用；两侧的 PRAGMA 仍然照抄，因为"今天没有引用"不是永久事实；
--    * 两个索引（`ux_inbox_open_fingerprint` / `ix_inbox_subject`）在重建里
--      逐字重建 —— 前者是"同一 fingerprint 至多一个 open 项"的**唯一**实现
--      （`_open_inbox` 的 SELECT-then-INSERT 在并发下靠它兜底），丢了它，
--      重复项会悄悄地进库；
--    * **视图不是"查询时才解析"的** —— 这一条是实测出来的，不是照抄文档。
--      三条路都在 `py -3.13 _probe_flow_41.py` 的第 1 段里能重跑一遍，结论：
--
--        A. 把新表建成**另一个名字**、删旧表、再 `ALTER TABLE … RENAME` 过来
--           → 在**改名那一刻**报 `error in view v_integrity_violations:
--           no such table: main.inbox_items`（SQLite 3.50 实测）。坑在改名，
--           **不在** `DROP TABLE` 本身 —— 单独删它是不报错的。
--        B. 先把旧表改名、建新表、拷行、删旧表，**不带** `legacy_alter_table`
--           → 不报错，但 `v_integrity_violations` 的正文被**静默改写**成指向
--           `inbox_items_old`（3.25 起的新行为）；删掉旧表之后那个视图永久指向
--           一张不存在的表 —— 而它正是 INV-01…17 的检查面
--           （`store.integrity_check` 读的就是它）。**没有任何信号**。
--        C. 同 B，但带上 `PRAGMA legacy_alter_table = ON`（下面走的这条）
--           → 视图正文一字未动，数据完好。
--
--      这个开关的作用域是**这一条连接**，所以在 COMMIT 之后关回去。
--    * `PRAGMA foreign_key_check` 放在 COMMIT 之前，是照抄官方配方的那一步
--      （今天必然是空的，留着是为了下一步有人加了 REFERENCES 时它还能兜住）。
--
--  ⚠️  这次是**加法**：第八个取值加进去，七个老取值一个都没动。已开的 777 条
--  待办、它们的 state / fingerprint / occurrence_count 逐行原样搬过去。
--
--  ---------------------------------------------------------------------------
--  三、会话（flow）与 `involves`
--  ---------------------------------------------------------------------------
--  三件套同 004–007：身份规则（正文在下面这一行的 `identity_rule` 里，与
--  `fixtures/kinds/flow.json` 逐字一致）+ 样例夹具 + 镜头归属 `traffic`。
--
--  ⚠️  `traffic` 这个镜头 id 从 001 的 CHECK 起就躺着，**至今没有任何 kind 用
--  过它** —— `flow` 是第一个。镜头只有那六个（`base.LENSES`），新增 lens 会被
--  DB 直接拒（R4 不许改 001），所以"流量镜头"就是它，不是另起一个名字。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. raw_files.custody —— 原文由谁持有（正交于 storage_state）
-- -----------------------------------------------------------------------------

--  ⚠️  SQLite 没有 `COMMENT ON` —— 这一列的语义（以及它为什么与 storage_state
--  正交）只能写在文件头和 `ops` 的那一节里，没有第三个落点。
--  升级路径（老库里已经有数据与待办）由 `test_flows.py::test_migration_011_
--  rebuilds_the_inbox_table_without_losing_anything` 逐列钉住。
ALTER TABLE raw_files
  ADD COLUMN custody TEXT NOT NULL DEFAULT 'stored'
  CHECK (custody IN ('stored', 'external'));

-- -----------------------------------------------------------------------------
-- 2. inbox_items.kind：第八个取值（SQLite 官方 12 步重建配方的本仓版本）
-- -----------------------------------------------------------------------------

PRAGMA foreign_keys = OFF;

BEGIN;

-- 关掉"改名顺手改写视图正文"（3.25 起的新行为）—— 不关的话
-- `v_integrity_violations` 会在这一句之后指向 `inbox_items_old`，而那张表
-- 马上要被删掉。文件头第二节有实测记录。
PRAGMA legacy_alter_table = ON;

ALTER TABLE inbox_items RENAME TO inbox_items_old;

CREATE TABLE inbox_items (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('value_conflict','orphan_subject','identity_ambiguous',
                                    'merge_conflict','evidence_missing','scope_drift',
                                    'manual_required',
                                    -- 4.1：流量里出现、而项目此前从未登记过的主机。
                                    -- 裁决是"纳入还是忽略"，与上面七个都不是一件事
                                    -- （文件头第二节）。
                                    'unregistered_asset')),
    subject_kind    TEXT    NOT NULL CHECK (subject_kind IN ('entity','observation','relation')),
    subject_id      INTEGER NOT NULL,
    fingerprint     TEXT    NOT NULL,
    severity        TEXT    NOT NULL DEFAULT 'medium'
                    CHECK (severity IN ('low','medium','high')),
    state           TEXT    NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','resolved','dismissed','closed')),
    payload_json    TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    created_by_job_id INTEGER REFERENCES jobs(id),
    auto_generated  INTEGER NOT NULL DEFAULT 1 CHECK (auto_generated IN (0,1)),
    created_at      TEXT    NOT NULL,
    last_seen_at    TEXT,
    resolved_at     TEXT,
    resolved_by     TEXT,
    close_reason    TEXT,
    closed_by_job_id        INTEGER REFERENCES jobs(id),
    opened_by_undo_job_id   INTEGER REFERENCES jobs(id),
    reopened_from   INTEGER REFERENCES inbox_items(id)
);

INSERT INTO inbox_items
    (id, project_id, kind, subject_kind, subject_id, fingerprint, severity, state,
     payload_json, occurrence_count, created_by_job_id, auto_generated, created_at,
     last_seen_at, resolved_at, resolved_by, close_reason, closed_by_job_id,
     opened_by_undo_job_id, reopened_from)
SELECT id, project_id, kind, subject_kind, subject_id, fingerprint, severity, state,
       payload_json, occurrence_count, created_by_job_id, auto_generated, created_at,
       last_seen_at, resolved_at, resolved_by, close_reason, closed_by_job_id,
       opened_by_undo_job_id, reopened_from
  FROM inbox_items_old;

DROP TABLE inbox_items_old;

CREATE UNIQUE INDEX ux_inbox_open_fingerprint
    ON inbox_items(project_id, kind, subject_kind, subject_id, fingerprint)
    WHERE state = 'open';

CREATE INDEX ix_inbox_subject ON inbox_items(project_id, subject_kind, subject_id, state);

PRAGMA legacy_alter_table = OFF;

PRAGMA foreign_key_check;

COMMIT;

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 3. 新 kind：flow（会话摘要）
-- -----------------------------------------------------------------------------
--  身份的正文与 `fixtures/kinds/flow.json` 的 `identity_rule` **逐字一致**
--  （`test_every_registered_kind_has_all_three_parts` 断言这一点），并与
--  `base.KIND_REGISTRY` 的镜像逐行一致（`test_kind_registry_matches_the_python_
--  mirror` 断言那一半）。所以下面这一个字面量是**三个落点**的共同来源。
--
--  ⚠️  这段正文 729 个字，**手抄必错**。改它的时候跑
--  `py -3.13 _probe_flow_41.py --write-fixture`：它拿这一行（迁移之后就是库里
--  那一行）的正文**生成**夹具，抄写错误从"可能发生"变成"不可能"；不加参数时
--  它只报告三处落点是否逐字一致、并把夹具里的每条样例/反例执行一遍。

INSERT INTO kind_registry
    (kind, identity_rule, fixture_path, lens, produced_by, registered_at)
VALUES
    ('flow',
     '会话（flow）的身份是**一段会话**：`协议|源|目的|会话起点`（normalize.flow_key）。协议取传输层（抓包里的 tcp / udp / sctp / icmp；HAR 里没有传输层，取的是 scheme），源与目的取 `地址:端口`：整段未知写 `-`、端口未知写 `*`、**两端都未知直接拒绝**（那样的「会话」指向不了任何主机），地址按 IP / 域名各自归一化，IPv6 带方括号。**时间窗必须进身份**：一个 5 元组不是一个会话 —— `10.0.0.5:51234 → 93.184.216.34:443` 今天上午用过一次、下午还会再用一次，只用 5 元组当身份会把一周的抓包折成一条「会话」，而它的包数是七天里所有连线的和，那个数字不描述任何一次真实的会话。起点是解析器按空闲阈值（normalize.FLOW_IDLE_SECONDS，默认 120 秒）真实切出来的边界：同一份文件重导一次算出同一个 key（幂等），两段真实会话算出两个 key（不折），两个工具抓到同一段会话时也算出同一个 key。**刻意不把起点取整到固定桶**（比如按小时）：取整会把一小时里的两段独立会话折成一个实体，而它们的摘要（包数 / 字节数 / 起止时间）会互相打架 —— 那是一类假冲突，也正是 010 那次花了一整个迁移去修的东西。**代价写在明处**：一个从会话中途开始的抓包会算出与完整抓包不同的起点，于是落成另一个 flow 实体；那是诚实的，我们只报我们看到的那一段。方向取文件里第一个包的那一侧（发起方），反向的包折进同一段会话 —— 一个 TCP 连接是双向的，只按「谁发给谁」分组会把每一次请求/响应拆成两段。',
     'fixtures/kinds/flow.json', 'traffic',
     '["har@1","tshark@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 4. 新关系动词：involves
-- -----------------------------------------------------------------------------
--  方向是 `flow --involves--> host`（host = `domain` 或 `ip`）。它存在的理由
--  有两条，都不是"顺手挂个边"：
--
--    * 4.1 的「会话里出现的新 host 自动进 Inbox」需要一个**待办主体** —— 而
--      `_open_inbox` 的 `subject_id` 是数据库里的实体 id，不是一段字符串。
--      有了这条边，"这段会话碰了哪些主机"是一句 SQL，而不是"把身份串拆开再
--      猜一遍"（身份串是写给**人**看的，不是给 `ops` 解析的）；
--    * 它同时让流量镜头能回答两个方向相反的问题：「这台主机出现在哪些会话里」
--      与「这段会话碰了哪些主机」。
--
--  只挂**目的**那一侧：源端在抓包里是自己的客户端（内网地址、临时端口），
--  把它挂进图会让上面第一个问题被本机的临时端口淹没，而"这段会话碰了哪个
--  资产"才是这张图要回答的。源端没有丢 —— 它以 `client` 观测的形式留在 flow
--  实体上。

INSERT INTO relation_vocabulary (verb, description, inverse, registered_at)
VALUES
    ('involves',
     '会话涉及某个主机（tcp|10.0.0.5:51234|93.184.216.34:443|2026-01-01T00:00:12Z --involves--> 93.184.216.34）。端点 kind 是 flow 与 host（domain / ip）。只挂**目的**那一侧：源端在抓包里是自己的客户端（内网地址、临时端口），挂进图会让「这台主机出现在哪些会话里」被本机的临时端口淹没，而「这段会话碰了哪个资产」才是这张图要回答的；源端以 flow 上的 `client` 观测留下。这条边是 4.1 的「会话里出现的新 host 自动进 Inbox」找到待办主体的那条路 —— 待办主体的 `subject_id` 是实体 id，而不是让人再去拆一遍身份串。',
     'involved_in', strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 5. 既有 kind 的产出者补登：tshark 产出 ip
-- -----------------------------------------------------------------------------
--  一段会话的两端落在 `ip` 上（`involves` 指向它），所以抓包是 `ip` 的一个真实
--  产出者。**domain 不补**：本步不做 DNS 名字提取 —— `frame` 里的 `dns.qry.name`
--  是一条**证据**，把它当成"这台 IP 叫什么"是一次需要人确认的推断，而不是一次
--  解析（4.3 的指纹引擎才是做推断的地方）。
--
--  用 `json_insert` 而不是字符串拼接的理由同 005 / 006 / 007：`produced_by` 是
--  canonical JSON 数组，拼接遇到坏数据会静默产出畸形数组，而 json 函数让迁移
--  当场回滚。`NOT EXISTS` 是幂等保险。

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'tshark@1')
 WHERE kind = 'ip'
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE json_each.value = 'tshark@1')
   AND json_valid(produced_by);
