-- =============================================================================
--  003_kind_registry.sql —— 资源类型登记表 + 关系受控词表（开发实施计划 2.4 · 同期必交项 A）
--
--  为什么是**新增迁移**而不是改 001：红线 R4 —— 已应用的迁移文件被改动 → 校验和
--  不匹配 → 直接报错。改 001 会让"新库"和"老库"结构看起来一样、实际不一样，
--  这是最容易犯也最难发现的错误。
--
--  ---------------------------------------------------------------------------
--  为什么是"登记表 + 触发器"而不是字面量的 CHECK 清单
--  ---------------------------------------------------------------------------
--  SQLite **没有** `ALTER TABLE … ADD CONSTRAINT`，所以给已存在的
--  `entities.kind` / `relations.rel` 加一个 `CHECK (… IN (…))` 只能靠重建整张表
--  （建新表 → 拷数据 → 删旧表 → 改名），而 `entities` 被 observations / relations /
--  entity_keys / 五张工作层表上的外键指着 —— 重建的代价与风险都不成比例。
--
--  于是这里用**表驱动的 BEFORE 触发器**，它比硬编码的 CHECK 更强：
--
--    * 词表在表里，加一个 kind 是 INSERT 一行，不是重写约束、重建表；
--    * 拒绝信息是**可读的**（`RAISE(ABORT, …)`），CHECK 只会说 "constraint failed"；
--    * 三件套（身份规则 / 样例夹具 / 镜头归属）本身就是表上的 NOT NULL +
--      CHECK，于是"缺一件套"在**登记那一刻**就被拒 —— 而不是等到有人用上它。
--
--  代价（诚实交代）：`entities.kind` 的类型信息不在 `PRAGMA table_info` 里，
--  一个只看 DDL 的人看不出它受约束。所以这段注释是必要的，不是客套。
--
--  ⚠️ 写这个文件时的一条 SQL 纪律：SQLite **不支持** Python 那种相邻字符串
--  字面量自动拼接。多行文案必须写成**一个**单行字面量，或者用 `||` 显式拼接 ——
--  写成两行相邻字面量会在 `executescript` 时报 `near "…": syntax error`，
--  而且报错位置指向第二行，很容易看错方向。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 资源类型登记表（计划书 §3.3）
-- -----------------------------------------------------------------------------
CREATE TABLE kind_registry (
    kind           TEXT PRIMARY KEY,
    -- 三件套之一：**身份规则** —— 什么样的 key 才指向同一个实体。
    -- 这是 kind 之间唯一的实质差别，也是把两个来源的同一条事实认成同一条的依据。
    identity_rule  TEXT NOT NULL CHECK (length(trim(identity_rule)) > 0),
    -- 三件套之二：**样例夹具** —— 可执行的证据，不是一段描述。
    -- 指向 `magpie/core/tests/` 下的相对路径；测试会断言它存在，且里面的样例
    -- 真的被身份规则算成声明的 key。
    fixture_path   TEXT NOT NULL CHECK (length(trim(fixture_path)) > 0),
    -- 三件套之三：**镜头归属** —— 没有归属的 kind 等于没有落点（计划书 §3.5）。
    lens           TEXT NOT NULL
                   CHECK (lens IN ('graph','assets','services','api','traffic','vuln')),
    -- 产出它的解析器（canonical JSON 数组，如 ["httpx@1"]）。空数组 = 目前只有
    -- 数据层在用（`ops.add_relation` 的兜底 kind 就是这种情况）。
    produced_by    TEXT NOT NULL DEFAULT '[]',
    registered_at  TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- 2. 关系受控词表（计划书 §3.4）
-- -----------------------------------------------------------------------------
CREATE TABLE relation_vocabulary (
    verb          TEXT PRIMARY KEY,
    description   TEXT NOT NULL CHECK (length(trim(description)) > 0),
    -- 逆动词（可空：自反或无可逆表达）。它让"反向查询"不必另立一列。
    inverse       TEXT,
    registered_at TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- 3. 拒绝触发器 —— 未登记的 kind / 动词**必然被拒**（反例红→绿的那条）
-- -----------------------------------------------------------------------------
--  INSERT 与 UPDATE 各一条：只守 INSERT 的话，一个 `UPDATE entities SET kind=…`
--  就能绕过去，而 rekey / merge 那些路径恰恰是 UPDATE 密集的。

CREATE TRIGGER trg_entities_kind_registered
BEFORE INSERT ON entities
WHEN NOT EXISTS (SELECT 1 FROM kind_registry k WHERE k.kind = NEW.kind)
BEGIN
    SELECT RAISE(ABORT, '未登记的 kind：entities.kind 必须在 kind_registry 里（v3.2 §12 / 计划书 §3.3）。新增 kind 必须同时提供身份规则 + 样例夹具 + 镜头归属，并走 schema 迁移。');
END;

CREATE TRIGGER trg_entities_kind_registered_update
BEFORE UPDATE OF kind ON entities
WHEN NOT EXISTS (SELECT 1 FROM kind_registry k WHERE k.kind = NEW.kind)
BEGIN
    SELECT RAISE(ABORT, '未登记的 kind：entities.kind 必须在 kind_registry 里（v3.2 §12 / 计划书 §3.3）。');
END;

CREATE TRIGGER trg_relations_verb_registered
BEFORE INSERT ON relations
WHEN NOT EXISTS (SELECT 1 FROM relation_vocabulary v WHERE v.verb = NEW.rel)
BEGIN
    SELECT RAISE(ABORT, '未登记的关系动词：relations.rel 必须在 relation_vocabulary 里（计划书 §3.4）。新增动词同样要走 schema 迁移。');
END;

CREATE TRIGGER trg_relations_verb_registered_update
BEFORE UPDATE OF rel ON relations
WHEN NOT EXISTS (SELECT 1 FROM relation_vocabulary v WHERE v.verb = NEW.rel)
BEGIN
    SELECT RAISE(ABORT, '未登记的关系动词：relations.rel 必须在 relation_vocabulary 里（计划书 §3.4）。');
END;

-- -----------------------------------------------------------------------------
-- 4. definition_hash 的 DB 级兜底（同期必交项 B）
-- -----------------------------------------------------------------------------
--  应用层（`store.register_parser`）已经把 `definition_hash` 收紧成必填，
--  这里是**数据库层的兜底**：任何绕过应用层的写入（手写 SQL、以后的迁移脚本、
--  别的语言写的导入器）同样会被拒。
--
--  为什么要两层：应用层的检查是"调用者守规矩"，DB 层的检查是"这件事不可能发生"。
--  v3.2 §16.4 第 2 条的债之所以存在，正是因为之前只有前者、而且前者也没做。
--
--  注意这是 **BEFORE INSERT**：老库里那些 definition_hash 为空的早期行不会被
--  这条触发器碰到，它们由 `register_parser` 在下次被触碰时**回填**。
--  给 UPDATE 加同样的约束会让回填本身变成不可能。
CREATE TRIGGER trg_parser_requires_definition_hash
BEFORE INSERT ON parser_profiles
WHEN NEW.definition_hash IS NULL OR length(trim(NEW.definition_hash)) = 0
BEGIN
    SELECT RAISE(ABORT, 'parser_profiles.definition_hash 不得为空：新登记的解析器指纹必须带着实现定义的 hash，否则「同一 fingerprint 背后的实现有没有变过」无法回答（v3.2 §16.4 第 2 条）。');
END;

-- -----------------------------------------------------------------------------
-- 5. 种子：本步真的落地了的 kind 与关系动词
-- -----------------------------------------------------------------------------
--  计划书 §3.3 列出 14 种 kind，但 `endpoint` / `vuln` / `apischema` 的身份规则
--  只有在阶段 3 的解析器里才有真实形状。在没有产出者的时候把它们登记进来，
--  等于凭空设计一套必然要返工的约束 —— 那正是 §3.3 交代过的"有意延后"。
--  准入没有变松：**下一个 kind 要进来，同样必须带齐三件套**。
--
--  这份种子与 `magpie/core/parsers/base.py` 的 `KIND_REGISTRY` /
--  `RELATION_VOCABULARY` 是同一份词表的两个落点，测试断言两边逐行一致 ——
--  分叉立刻红，而不是等到某天有人发现"库里能写的 kind 解析器不认"。

INSERT INTO kind_registry
    (kind, identity_rule, fixture_path, lens, produced_by, registered_at)
VALUES
    ('domain',
     '主机的注册域名字面量：小写、去尾点、去端口、IDN→punycode 之后的整串（normalize.normalize_domain）。同一域名的大小写/尾点写法是同一个实体；单标签（localhost）默认不成实体。',
     'fixtures/kinds/domain.json', 'assets', '["domainlist@1","httpx@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('ip',
     '单个 IP 地址的规范写法（normalize.normalize_ip）；IPv4-mapped IPv6（::ffff:1.2.3.4）折成 IPv4。**网段是另一种 kind**，不在这里折叠 —— 把 1.2.3.4 与 1.2.3.0/24 认成同一个实体，会让「哪台机器」的答案变成「哪一段」。',
     'fixtures/kinds/ip.json', 'assets', '["httpx@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('host',
     '通用主机端点：来源没有把「域名 / IP / 服务」分得更细时，用它的原始主机串作为 key。它是 ops.add_relation 的兜底 kind —— 关系端点没有被显式的 RawEntity 建过时落到这里。',
     'fixtures/kinds/host.json', 'assets', '[]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('service',
     'host:port（无端口时退化为 host:scheme）。**同一主机上的 80 与 443 是两个服务** —— 它们的标题、状态码、中间件可以完全不同，合并会丢掉这个区别。',
     'fixtures/kinds/service.json', 'services', '["httpx@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('product',
     '产品名[@版本]：产品名走 normalize.product_name（别名表折叠：Apache httpd / apache2 → httpd），版本走 normalize.version_tuple。**带版本与不带版本是两个实体** ——「哪些还在用 Nginx 1.18」问的是后者。',
     'fixtures/kinds/product.json', 'services', '["httpx@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

INSERT INTO relation_vocabulary (verb, description, inverse, registered_at)
VALUES
    ('member_of',
     '子域隶属（a.b.example.com --member_of--> b.example.com）。由域名后缀推导，不来自文件里的显式字段；上推在顶级两段停住。',
     'has_member', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('resolves_to',
     '域名解析到某个 IP（example.com --resolves_to--> 203.0.113.10）。端点 kind 分别是 domain 与 ip。',
     'resolved_by', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('runs',
     '服务上跑着某个产品（example.com:443 --runs--> nginx@1.24.0）。计划书 §4.2 的核心目标：中间件版本的主关系。',
     'runs_on', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
