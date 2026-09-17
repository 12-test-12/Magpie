-- =============================================================================
--  v3.2 schema — 001_init.sql
--  SQLite >= 3.35 (partial index / RETURNING / generated column 视需要)
--
--  分层（禁止跨层写）:
--    A 事实层  : raw_files, evidence, contributions, contribution_supersessions
--    B 缓存层  : entities, observations, relations, entity_identity
--    C 身份层  : entity_keys, entity_alias, entity_merge_events, entity_merge_members
--    D 工作层  : entity_layout, entity_tags, manual_fields, inbox_items
--    E 审计层  : jobs, activity_log, tag_events
--
--  术语冻结:
--    is_current    = 存在未被抑制的 contribution（事实缓存；≠ "网络存活"）
--    supersession  = "某贡献自此不再活跃"这一条事实（可 DELETE，需审计）
--    禁用词: is_live / live / dead / alive / origin（entities 上）
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 0. 项目与 scope 快照（对应审阅点 7）
--    0.0 parser 注册表（v3.2-r2 收紧）:
--        parser fingerprint 是一条**已登记的事实**，不再是自由字符串。
--        coverage 判定依赖它（§5.1），因此它必须落 DDL 并带引用完整性。
-- -----------------------------------------------------------------------------
CREATE TABLE parser_profiles (
    fingerprint     TEXT PRIMARY KEY,     -- 规范指纹
    profile_id      TEXT,                 -- 人类可读 profile 名（如 nmap-service）
    version         TEXT,
    definition_hash TEXT,                 -- 解析器定义 / 配置的 hash
    registered_at   TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- 0.0b source 目录（v3.2-r3）: §8.2 的 authority_term 必须有真实数据源。
--     未登记的 source 无法写贡献（contributions.source_id 有外键）。
-- -----------------------------------------------------------------------------
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    authority_level TEXT NOT NULL DEFAULT 'unverified'
                    CHECK (authority_level IN ('verified','semiautomatic',
                                               'manual','unverified')),
    tool            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE projects (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL UNIQUE,
    scope              TEXT    NOT NULL DEFAULT '{"exclude":[],"include":[]}',  -- canonical JSON
    scope_version      INTEGER NOT NULL DEFAULT 1,
    scope_hash         TEXT    NOT NULL,
    -- 当前解析器指纹（事实）。NULL = 未知 → coverage 判定一律 unknown（§5.1）
    parser_fingerprint TEXT    REFERENCES parser_profiles(fingerprint),
    status             TEXT    NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','archived'))
);

CREATE TABLE project_scope_history (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    scope       TEXT    NOT NULL,
    scope_hash  TEXT    NOT NULL,
    changed_at  TEXT    NOT NULL,
    changed_by  TEXT    NOT NULL,
    reason      TEXT,
    PRIMARY KEY (project_id, version)
);

-- -----------------------------------------------------------------------------
-- 1. 审计层: jobs（历史事实；不参与 domain state equivalence）
-- -----------------------------------------------------------------------------
CREATE TABLE jobs (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    kind               TEXT    NOT NULL,
    state              TEXT    NOT NULL DEFAULT 'running'
                       CHECK (state IN ('running','done','failed','partial','aborted')),
    -- v3.2-r4: 应用状态（生命周期），与执行状态 state **分离**。
    --   1 = 该 job 的效果当前生效；0 = 已被 undo 撤销（可被 redo 重新应用）。
    --   undo/redo 只动这一列，不再把 state 改成 'aborted'
    --   （"跑完了但后来被撤销" 与 "执行失败" 是两件事）。
    applied            INTEGER NOT NULL DEFAULT 1 CHECK (applied IN (0, 1)),
    -- coverage 快照（三元组缺一不可）
    coverage           TEXT    NOT NULL DEFAULT 'unknown'
                       CHECK (coverage IN ('full','partial','unknown')),
    coverage_scope     TEXT,
    scope_version      INTEGER NOT NULL,
    scope_hash         TEXT    NOT NULL,
    -- 该 job 使用的解析器指纹（已登记事实；NULL = 未知 → 不能用于 inactive 判定）
    parser_fingerprint TEXT    REFERENCES parser_profiles(fingerprint),
    actor              TEXT    NOT NULL DEFAULT 'cli',
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    UNIQUE (id, project_id),
    -- 复合 FK 目标（用于 contributions / evidence 的 project 一致性）
    UNIQUE (id, project_id, scope_version)
);

CREATE INDEX ix_jobs_project_state ON jobs(project_id, state);

-- -----------------------------------------------------------------------------
-- 2. 事实层: raw_files → evidence → contributions
-- -----------------------------------------------------------------------------
CREATE TABLE raw_files (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id),
    locator_uri    TEXT    NOT NULL,          -- 原始绝对/相对定位（可失效）
    sha256         TEXT    NOT NULL,
    byte_count     INTEGER NOT NULL,
    -- 原文持有状态。v3.2-r3: 只允许单调降级（present → archived → purged / missing）
    storage_state  TEXT    NOT NULL DEFAULT 'present'
                   CHECK (storage_state IN ('present','archived','purged','missing')),
    purged_at      TEXT,
    created_at     TEXT    NOT NULL,
    UNIQUE (id, project_id)
);

CREATE INDEX ix_raw_files_sha ON raw_files(project_id, sha256);

CREATE TABLE evidence (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    raw_file_id     INTEGER NOT NULL,
    -- v3.2-r3: `locator` 与 `recovery_state` 必须自洽 —— hash_only 就**没有** locator
    locator         TEXT,                      -- JSON: {"line":1821} / {"json_ptr":"/a/b"}
    sha256          TEXT    NOT NULL,          -- 该片段/对象的 hash
    quality_class   TEXT    NOT NULL DEFAULT 'medium'
                    CHECK (quality_class IN ('high','medium','low')),
    -- 三级"本行还留下什么"（审阅点 10；语义见 §5.3）
    --   full      = 片段内容 + locator + hash
    --   locator   = 无片段内容；locator + hash
    --   hash_only = 只剩 hash（locator 已丢弃）
    recovery_state  TEXT    NOT NULL DEFAULT 'full'
                    CHECK (recovery_state IN ('full','locator','hash_only')),
    created_at      TEXT    NOT NULL,
    UNIQUE (id, project_id),
    -- INV-08: evidence.project_id == raw_file.project_id（数据库级强制）
    FOREIGN KEY (raw_file_id, project_id) REFERENCES raw_files(id, project_id),
    CHECK ((recovery_state = 'hash_only') = (locator IS NULL))
);

CREATE INDEX ix_evidence_raw ON evidence(raw_file_id);

-- INV-17: recovery_state 只允许单调降级（full → locator → hash_only）
CREATE TRIGGER trg_evidence_no_upgrade
BEFORE UPDATE OF recovery_state ON evidence
WHEN (OLD.recovery_state = 'hash_only' AND NEW.recovery_state <> 'hash_only')
  OR (OLD.recovery_state = 'locator'   AND NEW.recovery_state = 'full')
BEGIN
    SELECT RAISE(ABORT, 'INV-17 violated: evidence.recovery_state must not be upgraded');
END;

-- INV-17b: raw_files.storage_state 也只允许单调降级
CREATE TRIGGER trg_raw_file_no_upgrade
BEFORE UPDATE OF storage_state ON raw_files
WHEN (OLD.storage_state = 'missing' AND NEW.storage_state <> 'missing')
  OR (OLD.storage_state = 'purged'  AND NEW.storage_state IN ('present','archived'))
BEGIN
    SELECT RAISE(ABORT, 'INV-17 violated: raw_files.storage_state must not be upgraded');
END;

CREATE TABLE contributions (
    id                   INTEGER PRIMARY KEY,
    project_id           INTEGER NOT NULL REFERENCES projects(id),
    subject_kind         TEXT    NOT NULL
                         CHECK (subject_kind IN ('entity','observation','relation')),
    subject_id           INTEGER NOT NULL,      -- 多态；由触发器保证同 project
    -- INV-16 (P0, v3.2-r2): 主体所依附的 entity **是事实**。
    --   subject_kind='observation'          → 该 observation 的归属 entity
    --   contribution_kind='relation'        → 该关系的源 entity（src）
    --   其余主体必须为 NULL。
    --   这条事实补全 contribution → observation → entity（以及 relation → src/dst）
    --   两条链，使缓存行可以真正从事实层重建（否则只能把 subject_id 当 entity_id 用）。
    owner_entity_id      INTEGER,
    source_id            INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    authorship           TEXT    NOT NULL DEFAULT 'tool'
                         CHECK (authorship IN ('tool','manual','imported')),
    contribution_kind    TEXT    NOT NULL
                         CHECK (contribution_kind IN ('entity','field','relation','layout')),

    field_namespace      TEXT
                         CHECK (field_namespace IS NULL
                                OR field_namespace IN ('entity_field','observation_field')),
    semantic_field       TEXT,
    field_value          TEXT,                  -- displayable string；数值/枚举用 text 存 canonical 形式
    value_kind           TEXT
                         CHECK (value_kind IS NULL OR value_kind IN
                                ('string','enum','number','ip','cidr','url','json','bool')),
    relation_kind        TEXT,

    -- 时间语义（审阅点 8 + v3.2-r2 区间语义）
    --   事实时间是一个**闭区间** [observed_at_min, observed_at]：
    --     单点事实  observed_at_min == observed_at
    --     压实事实  observed_at     == MAX(成员)
    --               observed_at_min == MIN(成员)   ← 区间只许细化，不许把上界往回拉
    --   observed_at 仍是排序/展示/§8.1 可信度判定所用的规范时刻。
    observed_at          TEXT    NOT NULL,
    observed_at_min      TEXT    NOT NULL,
    observed_at_source   TEXT    NOT NULL
                         CHECK (observed_at_source IN
                                ('tool','file_mtime','source_meta','ingest_time','manual')),
    observed_at_accuracy TEXT    NOT NULL DEFAULT 'unknown'
                         CHECK (observed_at_accuracy IN ('exact','approximate','unknown')),
    observed_at_precision TEXT   NOT NULL DEFAULT 'unknown'
                         CHECK (observed_at_precision IN
                                ('second','minute','day','month','year','unknown')),

    evidence_id          INTEGER NOT NULL,
    job_id               INTEGER NOT NULL,

    -- compact 语义（审阅点 9）: logical_job_count = job_count + compacted_job_count
    job_count            INTEGER NOT NULL DEFAULT 1 CHECK (job_count >= 1),
    compacted_job_count  INTEGER NOT NULL DEFAULT 0 CHECK (compacted_job_count >= 0),
    synthetic            INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0,1)),
    represents_json      TEXT,                  -- compact 前成员清单（source/job/observed_at/evidence）

    created_at           TEXT    NOT NULL,

    -- 时间区间自洽
    CHECK (observed_at_min <= observed_at),

    -- INV-16 形状: 只有"observation 主体"或"relation 事实"才带走属
    CHECK ((subject_kind = 'observation' OR contribution_kind = 'relation')
           = (owner_entity_id IS NOT NULL)),

    -- 'field' 必须带全字段；其余 kind 必须不带
    CHECK (
        (contribution_kind = 'field'
             AND field_namespace IS NOT NULL AND semantic_field IS NOT NULL
             AND field_value IS NOT NULL AND value_kind IS NOT NULL
             AND relation_kind IS NULL)
        OR
        (contribution_kind = 'relation'
             AND relation_kind IS NOT NULL AND field_namespace IS NULL
             AND semantic_field IS NULL)
        OR
        (contribution_kind = 'entity'
             AND field_namespace IS NULL AND semantic_field IS NULL
             AND field_value IS NULL AND relation_kind IS NULL)
        OR
        (contribution_kind = 'layout'
             AND field_namespace IS NULL AND relation_kind IS NULL)
    ),

    -- INV-07: contribution.project_id == evidence.project_id（数据库级强制）
    FOREIGN KEY (evidence_id, project_id) REFERENCES evidence(id, project_id)
    -- evidence 是事实，禁止级联删除
    ON DELETE RESTRICT ON UPDATE RESTRICT,
    -- INV-16: 归属 entity 必须同 project 存在（残留 NULL 时 FK 不生效，由 CHECK 兜底）
    FOREIGN KEY (owner_entity_id, project_id) REFERENCES entities(id, project_id)
);

-- INV-05: contribution.project_id == job.project_id（数据库级强制）
-- 以触发器实现，因为需要自定义 ABORT 文案（比复合 FK 更易诊断）。
CREATE TRIGGER trg_contrib_job_project
BEFORE INSERT ON contributions
WHEN NOT EXISTS (
    SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-05 violated: contribution.project_id != job.project_id');
END;

-- INV-06: contribution.project_id == subject.project_id（多态 → 触发器）
CREATE TRIGGER trg_contrib_subject_project
BEFORE INSERT ON contributions
WHEN NOT EXISTS (
    SELECT 1 FROM entities      x WHERE NEW.subject_kind='entity'      AND x.id=NEW.subject_id AND x.project_id=NEW.project_id
    UNION ALL
    SELECT 1 FROM observations  x WHERE NEW.subject_kind='observation' AND x.id=NEW.subject_id AND x.project_id=NEW.project_id
    UNION ALL
    SELECT 1 FROM relations     x WHERE NEW.subject_kind='relation'    AND x.id=NEW.subject_id AND x.project_id=NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-06 violated: contribution subject missing or cross-project');
END;

-- INV-01: contributions 无 UPDATE 路径（append-only）
CREATE TRIGGER trg_contrib_no_update
BEFORE UPDATE ON contributions
BEGIN
    SELECT RAISE(ABORT, 'INV-01 violated: contributions is append-only');
END;

-- INV-16: 主体的归属与**结构列**必须与缓存行一致（事实 → 缓存的唯一方向）。
--   同一条 observation 的所有贡献必须给出同一 (owner, namespace, field, value)；
--   同一条 relation 的所有贡献必须给出同一 (src=owner, dst_id, rel)。
--   于是 materialize() 里"用哪一条贡献建结构行"不再具有领域含义 ——
--   它只是在等价类里取一个确定性代表（MIN(id)），不是"哪条贡献负责创建"。
CREATE TRIGGER trg_contrib_owner
BEFORE INSERT ON contributions
WHEN (NEW.subject_kind = 'observation'
      AND NOT EXISTS (
        SELECT 1 FROM observations o
         WHERE o.id = NEW.subject_id
           AND o.project_id = NEW.project_id
           AND o.entity_id = NEW.owner_entity_id
           AND o.field_namespace = NEW.field_namespace
           AND o.semantic_field = NEW.semantic_field
           AND o.value = NEW.field_value))
  OR (NEW.contribution_kind = 'relation'
      AND NOT EXISTS (
        SELECT 1 FROM relations r
         WHERE r.id = NEW.subject_id
           AND r.project_id = NEW.project_id
           AND r.src_id = NEW.owner_entity_id
           AND r.rel = NEW.relation_kind
           AND r.dst_id = CAST(json_extract(NEW.field_value,'$.dst_id') AS INTEGER)))
BEGIN
    SELECT RAISE(ABORT, 'INV-16 violated: contribution structure mismatch');
END;

CREATE INDEX ix_contrib_subject ON contributions(subject_kind, subject_id);
CREATE INDEX ix_contrib_job     ON contributions(job_id);
CREATE INDEX ix_contrib_evidence ON contributions(evidence_id);
CREATE INDEX ix_contrib_source  ON contributions(project_id, subject_kind, subject_id, source_id);
CREATE INDEX ix_contrib_field   ON contributions(subject_kind, subject_id, field_namespace, semantic_field);
CREATE INDEX ix_contrib_owner   ON contributions(owner_entity_id);

-- -----------------------------------------------------------------------------
-- 2b. "贡献不再活跃" 本身也是一条事实（审阅点 2 / 3，方案 B）
--     唯一非活跃事实源。允许 DELETE（undo/reparse/rekey/unmerge/compact），
--     每次 DELETE 必须写 activity_log(action='supersession.lift')。
-- -----------------------------------------------------------------------------
CREATE TABLE contribution_supersessions (
    id                   INTEGER PRIMARY KEY,
    project_id           INTEGER NOT NULL REFERENCES projects(id),
    contribution_id      INTEGER NOT NULL REFERENCES contributions(id) ON DELETE RESTRICT,
    superseded_by_job_id INTEGER NOT NULL REFERENCES jobs(id),
    reason               TEXT    NOT NULL
                         CHECK (reason IN ('reparse','redo','contradiction',
                                           'retract','coverage_negative','compact')),
    created_at           TEXT    NOT NULL,
    actor                TEXT    NOT NULL DEFAULT 'cli',
    -- 每个贡献至多被抑制一次（"同一事实是否活跃"必须有唯一答案）
    UNIQUE (contribution_id)
);

CREATE INDEX ix_sup_job ON contribution_supersessions(superseded_by_job_id);

-- 性能（实测驱动，勿删）:
--   v_active_contributions 的 NOT EXISTS 按 contribution_id 过滤，
--   而 UNIQUE(contribution_id) 的隐式索引方向是 (contribution_id) —— 已可命中；
--   这里显式再声明一次是为了让 "活跃性判定" 的查询计划不依赖 UNIQUE 的实现细节。
CREATE INDEX IF NOT EXISTS ix_sup_contribution
    ON contribution_supersessions(contribution_id);

-- 批量 recompute 的聚合分组键: owner(entity) x namespace x field x value。
-- 没有它，FieldScoreBatch 的一次性全库扫描会退化成对每行的回表。
CREATE INDEX IF NOT EXISTS ix_contrib_owner_field
    ON contributions(owner_entity_id, contribution_kind, field_namespace,
                     semantic_field, field_value);

-- INV-09: supersession 与它抑制的贡献必须同 project（旧版这条没有落点）
CREATE TRIGGER trg_supersession_project
BEFORE INSERT ON contribution_supersessions
WHEN NOT EXISTS (
    SELECT 1 FROM contributions c
     WHERE c.id = NEW.contribution_id AND c.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-09 violated: supersession cross-project');
END;

-- observeds.superseded_at 在 v3.2 被删除（审阅点 2）：
--   观察/关系是否活跃，一律由 contribution_supersessions 推导。

-- -----------------------------------------------------------------------------
-- 3. 缓存层: entities / entity_keys / observations / relations
-- -----------------------------------------------------------------------------
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    kind        TEXT    NOT NULL,     -- host / service / url / endpoint / cert / org ...
    lifecycle   TEXT    NOT NULL DEFAULT 'active'
                CHECK (lifecycle IN ('active','retired')),   -- 业务生命周期（人工/流程）
    is_current  INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0,1)),  -- 事实缓存
    merged_into INTEGER,              -- identity overlay 物化结果（NULL = 未合并）
    first_seen  TEXT,
    last_seen   TEXT,
    UNIQUE (id, project_id)
);

CREATE INDEX ix_entities_project_kind ON entities(project_id, kind);
CREATE INDEX ix_entities_current      ON entities(project_id, is_current);

-- entity_key 是"创建时的身份标签"，一经写入永不 UPDATE（rekey 靠 entity_alias，见 4.5）
CREATE TABLE entity_keys (
    project_id  INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_key  TEXT    NOT NULL,
    canonical   INTEGER NOT NULL DEFAULT 0 CHECK (canonical IN (0,1)),
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (project_id, entity_key),
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

CREATE INDEX ix_entity_keys_entity ON entity_keys(entity_id);

CREATE TABLE observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            INTEGER NOT NULL REFERENCES projects(id),
    entity_id             INTEGER NOT NULL,
    field_namespace       TEXT    NOT NULL DEFAULT 'observation_field'
                          CHECK (field_namespace IN ('observation_field')),
    semantic_field        TEXT    NOT NULL,
    value                 TEXT    NOT NULL,
    value_kind            TEXT    NOT NULL DEFAULT 'string',

    -- 缓存字段（可由 contributions 全量重建）
    is_current            INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0,1)),
    confidence            TEXT    NOT NULL DEFAULT 'none'
                          CHECK (confidence IN ('none','low','medium','high','conflict','manual')),
    source_count          INTEGER NOT NULL DEFAULT 0,
    job_count             INTEGER NOT NULL DEFAULT 0,
    logical_job_count     INTEGER NOT NULL DEFAULT 0,
    origin                TEXT    NOT NULL DEFAULT 'tool'
                          CHECK (origin IN ('tool','manual','imported')),

    -- 时间语义（缓存自活贡献的**区间**；observed_at 是区间上界/规范时刻）
    observed_at           TEXT,
    observed_at_min       TEXT,
    observed_at_source    TEXT
                          CHECK (observed_at_source IS NULL OR observed_at_source IN
                                 ('tool','file_mtime','source_meta','ingest_time','manual')),
    observed_at_accuracy  TEXT
                          CHECK (observed_at_accuracy IS NULL OR observed_at_accuracy IN
                                 ('exact','approximate','unknown')),
    observed_at_precision TEXT
                          CHECK (observed_at_precision IS NULL OR observed_at_precision IN
                                 ('second','minute','day','month','year','unknown')),

    created_at            TEXT    NOT NULL,
    UNIQUE (id, project_id),
    -- RESTRICT（v3.2-r2）：缓存行的删除只能由 GC/vacuum 显式判定，
    -- 绝不允许 DELETE entity 借 CASCADE 顺手带走引用它的历史（v3.1 事故链）。
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE RESTRICT
);

CREATE INDEX ix_obs_entity_current ON observations(entity_id, is_current);
CREATE INDEX ix_obs_field          ON observations(entity_id, field_namespace, semantic_field);
CREATE INDEX ix_obs_current        ON observations(project_id, is_current);

CREATE TABLE relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    src_id      INTEGER NOT NULL,
    dst_id      INTEGER NOT NULL,
    rel         TEXT    NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0,1)),
    confidence  TEXT    NOT NULL DEFAULT 'none'
                CHECK (confidence IN ('none','low','medium','high','conflict','manual')),
    source_count INTEGER NOT NULL DEFAULT 0,
    job_count    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (src_id, project_id) REFERENCES entities(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (dst_id, project_id) REFERENCES entities(id, project_id) ON DELETE RESTRICT
);

CREATE INDEX ix_rel_src ON relations(src_id, is_current);
CREATE INDEX ix_rel_dst ON relations(dst_id, is_current);

-- 观测/关系所属 entity 的 project 一致性（UPDATE 也要守）
CREATE TRIGGER trg_obs_entity_project
BEFORE UPDATE OF entity_id ON observations
WHEN NOT EXISTS (
    SELECT 1 FROM entities e WHERE e.id = NEW.entity_id AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-06 violated: observation.entity_id cross-project');
END;

CREATE TRIGGER trg_rel_src_project
BEFORE UPDATE OF src_id ON relations
WHEN NOT EXISTS (
    SELECT 1 FROM entities e WHERE e.id = NEW.src_id AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-06 violated: relation.src_id cross-project');
END;

CREATE TRIGGER trg_rel_dst_project
BEFORE UPDATE OF dst_id ON relations
WHEN NOT EXISTS (
    SELECT 1 FROM entities e WHERE e.id = NEW.dst_id AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'INV-06 violated: relation.dst_id cross-project');
END;

-- -----------------------------------------------------------------------------
-- 4. 身份层: alias / merge overlay / 解析缓存（审阅点 11, 12）
--    铁律: entities.entity_key 永不 UPDATE；rekey/merge 只写这一层；
--          merge/unmerge 前后事实层逐行不变（INV-10）。
--
--    v3.2-r4: 身份事实**不做物理删除**，撤销走"生命周期边"（revoked_by_job_id /
--    unmerged_by_job_id）。这样 undo 才是**可逆**的 —— 否则 alias 的行内容一旦
--    DELETE 掉，redo 就再也无法重建（key 映射不在事实层里）。
-- -----------------------------------------------------------------------------
CREATE TABLE entity_alias (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES projects(id),
    alias_key         TEXT    NOT NULL,
    entity_id         INTEGER NOT NULL,   -- 该 alias 解析到的实体
    reason            TEXT    NOT NULL
                      CHECK (reason IN ('rekey','merge_member','import','normalize','manual')),
    created_at        TEXT    NOT NULL,
    created_by_job_id INTEGER REFERENCES jobs(id),
    -- 生命周期边: 被哪个 job 的 undo 撤销（NULL = 生效）
    revoked_by_job_id INTEGER REFERENCES jobs(id),
    revoked_at        TEXT,
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

-- 生效 alias 的唯一性（被撤销的行不参与 —— 因此 undo/redo 可以反复切换）
CREATE UNIQUE INDEX ux_alias_live
    ON entity_alias(project_id, alias_key) WHERE revoked_by_job_id IS NULL;

CREATE INDEX ix_alias_entity ON entity_alias(entity_id);

CREATE TABLE entity_merge_events (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    survivor_id   INTEGER NOT NULL,
    state         TEXT    NOT NULL DEFAULT 'merged'
                  CHECK (state IN ('merged','unmerged')),
    reason        TEXT,
    created_at    TEXT    NOT NULL,
    created_by_job_id INTEGER REFERENCES jobs(id),
    unmerged_at   TEXT,
    -- 生命周期边: 被哪个 job 的 undo 重新合并 / 被哪个 job 的 unmerge 解除
    unmerged_by_job_id INTEGER REFERENCES jobs(id),
    FOREIGN KEY (survivor_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

CREATE TABLE entity_merge_members (
    merge_event_id INTEGER NOT NULL REFERENCES entity_merge_events(id) ON DELETE CASCADE,
    entity_id      INTEGER NOT NULL,
    project_id     INTEGER NOT NULL,
    -- 生命周期边: 被哪个 job 的 undo 撤销（行保留，便于 redo 精确复原）
    revoked_by_job_id INTEGER REFERENCES jobs(id),
    revoked_at     TEXT,
    PRIMARY KEY (merge_event_id, entity_id),
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

-- 解析缓存（可 DELETE 后全量重建）: 任何引用/展示都用这一层折叠 identity
CREATE TABLE entity_identity (
    entity_id            INTEGER PRIMARY KEY,
    canonical_entity_id  INTEGER NOT NULL,
    canonical_key        TEXT,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX ix_identity_canonical ON entity_identity(canonical_entity_id);

-- -----------------------------------------------------------------------------
-- 5. 工作层（无法由事实重建 → GC 锚点，永不被自动清理）
-- -----------------------------------------------------------------------------
CREATE TABLE entity_layout (
    project_id  INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    x           REAL    NOT NULL DEFAULT 0,
    y           REAL    NOT NULL DEFAULT 0,
    collapsed   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (project_id, entity_id),
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

CREATE TABLE entity_tags (
    project_id    INTEGER NOT NULL,
    entity_id     INTEGER NOT NULL,
    tag           TEXT    NOT NULL,
    set_by        TEXT    NOT NULL,
    set_at        TEXT    NOT NULL,
    deleted_at    TEXT,          -- 软删: 保留 set_by/set_at 的历史（审阅点 14）
    deleted_by    TEXT,
    delete_reason TEXT,
    PRIMARY KEY (project_id, entity_id, tag),
    FOREIGN KEY (entity_id, project_id) REFERENCES entities(id, project_id) ON DELETE CASCADE
);

CREATE INDEX ix_tags_live ON entity_tags(project_id, tag) WHERE deleted_at IS NULL;

-- manual_fields: JSON 数组 + 乐观版本号（INV-12 单写队列）
CREATE TABLE manual_fields (
    project_id   INTEGER NOT NULL,
    subject_kind TEXT    NOT NULL CHECK (subject_kind IN ('entity','observation','relation')),
    subject_id   INTEGER NOT NULL,
    keys_json    TEXT    NOT NULL DEFAULT '[]',   -- 受人工保护的目标字段名 JSON 数组
    version      INTEGER NOT NULL DEFAULT 1,      -- UPDATE ... WHERE version=:expected
    updated_at   TEXT    NOT NULL,
    updated_by   TEXT    NOT NULL,
    PRIMARY KEY (project_id, subject_kind, subject_id)
);

CREATE TABLE inbox_items (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('value_conflict','orphan_subject','identity_ambiguous',
                                    'merge_conflict','evidence_missing','scope_drift',
                                    'manual_required')),
    subject_kind    TEXT    NOT NULL CHECK (subject_kind IN ('entity','observation','relation')),
    subject_id      INTEGER NOT NULL,
    fingerprint     TEXT    NOT NULL,   -- canonical hash（禁止 NULL，否则绕过唯一性）
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
    -- 生命周期边（v3.2-r4）:
    --   closed_by_job_id         : 被哪个 job 的 undo 关闭（redo 会重开）
    --   opened_by_undo_job_id    : 由哪个 job 的 undo 打开（redo 会关闭）
    closed_by_job_id        INTEGER REFERENCES jobs(id),
    opened_by_undo_job_id   INTEGER REFERENCES jobs(id),
    reopened_from   INTEGER REFERENCES inbox_items(id)
);

-- 审阅点 16: 同一 subject + 同一 kind + 同一 fingerprint 至多一个 open item
CREATE UNIQUE INDEX ux_inbox_open_fingerprint
    ON inbox_items(project_id, kind, subject_kind, subject_id, fingerprint)
    WHERE state = 'open';

CREATE INDEX ix_inbox_subject ON inbox_items(project_id, subject_kind, subject_id, state);

-- -----------------------------------------------------------------------------
-- 6. 审计层: activity_log / tag_events（append-only，永不回滚）
-- -----------------------------------------------------------------------------
CREATE TABLE activity_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    action      TEXT    NOT NULL,   -- 冻结命名空间, 见 §6.2
    target_kind TEXT,
    target_id   INTEGER,
    job_id      INTEGER REFERENCES jobs(id),   -- 审阅点 15: 打通 activity→job→contribution→evidence
    detail      TEXT                            -- JSON
);

CREATE INDEX ix_activity_job    ON activity_log(job_id);
CREATE INDEX ix_activity_action ON activity_log(action, at);
CREATE INDEX ix_activity_target ON activity_log(target_kind, target_id);

CREATE TABLE tag_events (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    entity_id   INTEGER NOT NULL,
    tag         TEXT    NOT NULL,
    action      TEXT    NOT NULL CHECK (action IN ('add','remove')),
    actor       TEXT    NOT NULL,
    at          TEXT    NOT NULL,
    reason      TEXT,
    job_id      INTEGER REFERENCES jobs(id)
);

CREATE INDEX ix_tag_events_entity ON tag_events(entity_id, at);

-- -----------------------------------------------------------------------------
-- 7. 完整性检查视图（INV-03 / INV-15）
--    用法: SELECT * FROM v_integrity_violations;  → 必须为空
-- -----------------------------------------------------------------------------
CREATE VIEW v_integrity_violations AS
    -- INV-03: contribution 指向不存在的 subject（悬空）
    SELECT 'orphan_contribution' AS violation, CAST(c.id AS TEXT) AS ref, c.project_id
      FROM contributions c
     WHERE NOT EXISTS (
            SELECT 1 FROM entities      e WHERE c.subject_kind='entity'      AND e.id=c.subject_id
            UNION ALL
            SELECT 1 FROM observations  o WHERE c.subject_kind='observation' AND o.id=c.subject_id
            UNION ALL
            SELECT 1 FROM relations     r WHERE c.subject_kind='relation'    AND r.id=c.subject_id)
UNION ALL
    -- INV-06: contribution 与 subject 跨 project
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN entities e ON c.subject_kind='entity' AND e.id=c.subject_id
     WHERE e.project_id <> c.project_id
UNION ALL
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN observations o ON c.subject_kind='observation' AND o.id=c.subject_id
     WHERE o.project_id <> c.project_id
UNION ALL
    SELECT 'cross_project_contribution', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN relations r ON c.subject_kind='relation' AND r.id=c.subject_id
     WHERE r.project_id <> c.project_id
UNION ALL
    -- INV-15: supersession 指向不存在 / 被删的 contribution
    SELECT 'orphan_supersession', CAST(s.id AS TEXT), s.project_id
      FROM contribution_supersessions s
     WHERE NOT EXISTS (SELECT 1 FROM contributions c WHERE c.id = s.contribution_id)
UNION ALL
    -- INV-08: evidence 与 raw_file 跨 project
    SELECT 'cross_project_evidence', CAST(ev.id AS TEXT), ev.project_id
      FROM evidence ev JOIN raw_files rf ON rf.id = ev.raw_file_id
     WHERE rf.project_id <> ev.project_id
UNION ALL
    -- INV-07: contribution 与 evidence 跨 project
    SELECT 'cross_project_contribution_evidence', CAST(c.id AS TEXT), c.project_id
      FROM contributions c JOIN evidence ev ON ev.id = c.evidence_id
     WHERE ev.project_id <> c.project_id
UNION ALL
    -- 缓存一致性: is_current 有 active contribution 却为 0
    SELECT 'cache_understated_observation', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- 缓存一致性: is_current = 1 却无 active contribution
    SELECT 'cache_overstated_observation', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_understated_relation', CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE r.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='relation' AND c.subject_id=r.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_overstated_relation', CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE r.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='relation' AND c.subject_id=r.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_understated_entity', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.is_current = 0 AND EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='entity' AND c.subject_id=e.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    SELECT 'cache_overstated_entity', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.is_current = 1 AND NOT EXISTS (
            SELECT 1 FROM contributions c
             WHERE c.subject_kind='entity' AND c.subject_id=e.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- 命名冲突: 同一 project 内 canonical key 必须唯一（身份层解析结果）
    --   v3.2-r2: 必须按 project 分组；canonical_key 只在 project 内可比（原实现全库分组 → 多项目误报）
    SELECT 'duplicate_canonical_key', ii.canonical_key, ek.project_id
      FROM entity_identity ii
      JOIN entity_keys ek ON ek.entity_id = ii.canonical_entity_id
     WHERE ii.canonical_key IS NOT NULL
     GROUP BY ek.project_id, ii.canonical_key
    HAVING COUNT(DISTINCT ii.canonical_entity_id) > 1
UNION ALL
    -- INV-16: 事实里的主体归属/结构必须与缓存行一致
    SELECT 'observation_owner_mismatch', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN observations o ON o.id = c.subject_id
     WHERE c.subject_kind = 'observation'
       AND (o.entity_id <> c.owner_entity_id
         OR o.field_namespace <> c.field_namespace
         OR o.semantic_field <> c.semantic_field
         OR o.value <> c.field_value)
UNION ALL
    SELECT 'relation_owner_mismatch', CAST(c.id AS TEXT), c.project_id
      FROM contributions c
      JOIN relations r ON r.id = c.subject_id
     WHERE c.contribution_kind = 'relation'
       AND (r.src_id <> c.owner_entity_id
         OR r.rel <> c.relation_kind
         OR r.dst_id <> CAST(json_extract(c.field_value,'$.dst_id') AS INTEGER))
UNION ALL
    -- INV-17: evidence 的 locator 与 recovery_state 必须自洽
    SELECT 'evidence_locator_state_mismatch', CAST(e.id AS TEXT), e.project_id
      FROM evidence e
     WHERE (e.recovery_state = 'hash_only') <> (e.locator IS NULL)
UNION ALL
    -- 时间区间缓存一致性: observed_at / observed_at_min 必须等于活贡献区间的上/下界
    SELECT 'observed_at_interval_mismatch', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.is_current = 1
       AND (o.observed_at     <> (SELECT MAX(c.observed_at)
                                    FROM v_active_contributions c
                                   WHERE c.subject_kind='observation' AND c.subject_id=o.id)
         OR o.observed_at_min <> (SELECT MIN(c.observed_at_min)
                                    FROM v_active_contributions c
                                   WHERE c.subject_kind='observation' AND c.subject_id=o.id))
UNION ALL
    -- INV-13(b) 幻影行: 缓存行不得"无法被任何贡献解释"
    --   observation / relation: 一条贡献都没有 → 只能由 vacuum 回收，不允许残留
    SELECT 'phantom_cache_row', 'observation:' || CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='observation' AND c.subject_id=o.id)
UNION ALL
    SELECT 'phantom_cache_row', 'relation:' || CAST(r.id AS TEXT), r.project_id
      FROM relations r
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='relation' AND c.subject_id=r.id)
UNION ALL
    --   entity: 自己无贡献、却仍被其它缓存行引用 → 悬空解释（墓碑行必须由贡献解释）
    SELECT 'phantom_cache_row', 'entity:' || CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE NOT EXISTS (SELECT 1 FROM contributions c
                        WHERE c.subject_kind='entity' AND c.subject_id=e.id)
       AND (EXISTS (SELECT 1 FROM observations o WHERE o.entity_id=e.id)
         OR EXISTS (SELECT 1 FROM relations r WHERE r.src_id=e.id OR r.dst_id=e.id)
         OR EXISTS (SELECT 1 FROM entity_identity i
                     WHERE i.canonical_entity_id=e.id AND i.entity_id<>e.id))
UNION ALL
    -- 每个 entity_key 必须有**生效**的 alias（否则 key 无法解析）
    SELECT 'key_without_alias', ek.entity_key, ek.project_id
      FROM entity_keys ek
     WHERE NOT EXISTS (SELECT 1 FROM entity_alias a
                        WHERE a.project_id = ek.project_id
                          AND a.alias_key = ek.entity_key
                          AND a.revoked_by_job_id IS NULL)
UNION ALL
    -- v3.2-r4: 生命周期边必须与 job 的 applied 一致
    --   （撤销边存在 ⇔ 该 job 当前处于"未应用"状态）
    SELECT 'revocation_edge_inconsistent', 'alias:' || CAST(a.id AS TEXT), a.project_id
      FROM entity_alias a JOIN jobs j ON j.id = a.revoked_by_job_id
     WHERE a.revoked_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'merge_member:' || CAST(m.entity_id AS TEXT),
           m.project_id
      FROM entity_merge_members m JOIN jobs j ON j.id = m.revoked_by_job_id
     WHERE m.revoked_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'merge_event:' || CAST(ev.id AS TEXT), ev.project_id
      FROM entity_merge_events ev JOIN jobs j ON j.id = ev.unmerged_by_job_id
     WHERE ev.unmerged_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'inbox_closed:' || CAST(i.id AS TEXT), i.project_id
      FROM inbox_items i JOIN jobs j ON j.id = i.closed_by_job_id
     WHERE i.closed_by_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    SELECT 'revocation_edge_inconsistent', 'inbox_opened:' || CAST(i.id AS TEXT), i.project_id
      FROM inbox_items i JOIN jobs j ON j.id = i.opened_by_undo_job_id
     WHERE i.opened_by_undo_job_id IS NOT NULL AND j.applied <> 0
UNION ALL
    -- v3.2-r4: 未应用的 job 必须真的把它的贡献全部变为非活跃
    --   （不要求由它自己抑制：可能更早被 compact 吞并过）
    SELECT 'undo_incomplete', 'job:' || CAST(j.id AS TEXT), j.project_id
      FROM jobs j
     WHERE j.applied = 0
       AND EXISTS (SELECT 1 FROM contributions c
                    WHERE c.job_id = j.id
                      AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                       WHERE s.contribution_id = c.id))
UNION ALL
    -- compact 语义守恒: logical_job_count 缓存必须等于 job_count + compacted_job_count
    SELECT 'logical_job_count_mismatch', CAST(o.id AS TEXT), o.project_id
      FROM observations o
     WHERE o.logical_job_count <> (
            SELECT COALESCE(SUM(c.job_count + c.compacted_job_count), 0)
              FROM contributions c
             WHERE c.subject_kind='observation' AND c.subject_id=o.id
               AND NOT EXISTS (SELECT 1 FROM contribution_supersessions s
                                WHERE s.contribution_id=c.id))
UNION ALL
    -- merge overlay 体检: merged_into 指向不存在的实体
    SELECT 'dangling_merged_into', CAST(e.id AS TEXT), e.project_id
      FROM entities e
     WHERE e.merged_into IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM entities t WHERE t.id = e.merged_into);

-- -----------------------------------------------------------------------------
-- 8. 活跃性判定：唯一口径（供 recompute / coverage / undo 共用）
-- -----------------------------------------------------------------------------
CREATE VIEW v_active_contributions AS
    SELECT c.*
      FROM contributions c
     WHERE NOT EXISTS (
            SELECT 1 FROM contribution_supersessions s WHERE s.contribution_id = c.id);

-- -----------------------------------------------------------------------------
-- 9. agreement 作用域（审阅点 17 + v3.2-r2 修正）
--
--    v3.2-r2 关键修正: agreement / conflict 的"主体"是 **entity**，不是 observation 缓存行。
--    理由: observation 行的身份是 (entity, namespace, field, value)，即"值"本身就进了身份，
--    因此以 observation 为主体永远不可能观测到"同字段多值"的冲突 —— §8.2 的
--    "存在未解决冲突 → conflict" 与 P-17b 都不可达。
--    正确口径: owner = entity_field 贡献的 subject_id，或 observation_field 贡献的
--    subject_entity_id（INV-16 提供的事实）。这样 contribution → observation → entity
--    链才真正闭环。
-- -----------------------------------------------------------------------------
CREATE VIEW v_field_owner AS
    SELECT c.*,
           CASE WHEN c.field_namespace = 'observation_field'
                THEN c.owner_entity_id ELSE c.subject_id END AS owner_id
      FROM v_active_contributions c
     WHERE c.contribution_kind = 'field';

CREATE VIEW v_field_agreement AS
    SELECT project_id,
           'entity'                                                   AS subject_kind,
           owner_id                                                   AS subject_id,
           field_namespace,
           semantic_field,
           field_value,
           COUNT(DISTINCT source_id)                                  AS source_count,
           COUNT(DISTINCT job_id)                                     AS job_count,
           SUM(job_count + compacted_job_count)                       AS logical_job_count
      FROM v_field_owner
     GROUP BY project_id, owner_id, field_namespace, semantic_field, field_value;

CREATE VIEW v_field_conflicts AS
    SELECT project_id, 'entity' AS subject_kind, owner_id AS subject_id,
           field_namespace, semantic_field,
           COUNT(DISTINCT field_value) AS distinct_values,
           GROUP_CONCAT(DISTINCT field_value) AS values_csv
      FROM v_field_owner
     GROUP BY project_id, owner_id, field_namespace, semantic_field
    HAVING COUNT(DISTINCT field_value) > 1;
