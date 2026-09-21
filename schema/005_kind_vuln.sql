-- =============================================================================
--  005_kind_vuln.sql —— 登记 `vuln`（漏洞）这个 kind 与 `found_on` 这条关系
--  （开发实施计划 3.2 · nuclei）
--
--  为什么必须是**新增迁移**而不是改 003：红线 R4 —— 已应用的迁移文件被改动 →
--  校验和不匹配 → `migrate()` 直接报错（见 schema/003 的文件头与
--  `magpie/core/migrate.py`）。`kind_registry` 是一份"当时被确认过的词表"；
--  往它里面加一行是**新事实**，改旧文件是篡改历史。
--
--  ---------------------------------------------------------------------------
--  为什么这一步要加一个 kind
--  ---------------------------------------------------------------------------
--  003 的种子注释把准入写死了：`endpoint` / `vuln` / `apischema` 的身份规则
--  只有在阶段 3 各自的解析器里才有真实形状，**没有产出者时不登记**。
--  3.2 的 nuclei 是 `vuln` 的第一个真实产出者，于是三件套在本步**同时**到位：
--
--    * 身份规则 —— `normalize.vuln_key`（`template-id@规范化目标`）；
--    * 样例夹具 —— `fixtures/kinds/vuln.json`（可执行：样例真的被规则算成 key）；
--    * 镜头归属 —— `vuln`（计划书 §3.5 的六个镜头之一，**不是新建的**：
--      003 的 `CHECK` 是冻结的，而 R4 不许改 003，所以新增 lens id 事实上
--      不可能。`vuln` 恰好在里面）。
--
--  ---------------------------------------------------------------------------
--  为什么身份是 (template-id, 目标) 而不是 template-id 一个人
--  ---------------------------------------------------------------------------
--  只用 template-id 的话，"这个模板命中了几个地方"就答不出来，唯一的补救是
--  给实体挂一个**计数器** —— 那正是本步点名要防的写法：计数器绕开 contributions
--  模型，`undo` 之后不会跟着回退，留下一行幻影数据，而且现有测试只断言它读得
--  对、不会替你发现。
--
--  身份里带上目标之后，"同一个模板命中两个不同资产"是**两个实体**（它们确实是
--  两件事），而"同一个模板命中同一个目标两次"两次算出同一个 key、第二条的 facts
--  完全重复 → 管线记 `skipped`，一条贡献都不写。"同模板重复命中不重复计数"
--  因此是身份规则的**推论**，不是另造的一个机制。
--
--  目标规范化之后连**查询串与 userinfo 都要丢掉**：前者带一次性 token（留着会
--  让重导变成一条新观测，`added` 不再是 0），后者是凭据（§4.5：明文凭据永不落库）。
--
--  ---------------------------------------------------------------------------
--  为什么 `state` 是观测而不是新列
--  ---------------------------------------------------------------------------
--  计划书 §4.5 要求 `vuln` 带 `severity` + `state`，但库里没有这两列，而本迁移
--  按 R4 只能 INSERT、不能改表结构。于是它们落成 vuln 实体上的**两条观测**
--  （`semantic_field` 分别是 `severity` / `state`）。这不是退而求其次：severity
--  与 state 是**事实**（谁说的、哪次作业说的），而事实的家就是 observations。
--  nuclei 的文件里没有 triage 状态，所以导入的一律落 `open` —— 它是漏洞镜头页
--  按 state 筛选的唯一来源，而 3.8 的批量改状态才会让这一维真正分层。
--
--  注意这次迁移**只做 INSERT**：`produced_by` 的更新（把 `nuclei@1` 追加到
--  `domain` / `ip` / `service` 三行上）也在这里做，理由是词表的唯一事实来源
--  就是这张表 —— Python 侧的镜像（`base.KIND_REGISTRY`）与它逐行一致，测试
--  断言两边分叉即红。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 新 kind：vuln
-- -----------------------------------------------------------------------------
--  identity_rule 的正文**必须与** `fixtures/kinds/vuln.json` 的 `identity_rule`
--  逐字一致（`test_every_registered_kind_has_all_three_parts` 断言这一点）——
--  两份描述一旦分叉，"哪个才是身份规则"就没有答案了。

INSERT INTO kind_registry
    (kind, identity_rule, fixture_path, lens, produced_by, registered_at)
VALUES
    ('vuln',
     '漏洞的身份是 `template-id@规范化目标`（normalize.vuln_key）：目标是 normalize.vuln_target_parts 算出的规范写法（scheme 与小写主机名，显式端口，丢掉查询串、片段与 userinfo）。**身份必须带目标** —— 只用 template-id 就答不出"命中了几个地方"，唯一的补救是加计数器，而计数器绕开 contributions 模型、undo 之后留下幻影行。同一个模板命中不同资产是两个实体（两件事）；命中同一个目标两次算出同一个 key，管线的去重把它记成 skipped。',
     'fixtures/kinds/vuln.json', 'vuln', '["nuclei@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 2. 新关系动词：found_on
-- -----------------------------------------------------------------------------
--  方向是 `vuln --found_on--> 资产`。逆动词 `has_finding` 让"这个服务上有哪些
--  漏洞"不必另立一列。
--
--  为什么不用 `exposes`：那条动词在 2.4 就被用作**反例**（
--  `test_unregistered_relation_verb_is_rejected_by_the_database` 断言它写不
--  进去）。反例只有"仍然未登记"时才是一条约束，把一个反例转正等于把它删掉。

INSERT INTO relation_vocabulary (verb, description, inverse, registered_at)
VALUES
    ('found_on',
     '漏洞落在某个资产上（CVE-2021-44228@https://app.example.com:443/ --found_on--> app.example.com:443）。端点 kind 是 vuln 与 service（目标里没有 scheme 也没有端口时退化为 domain / ip）。',
     'has_finding', strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 3. 既有 kind 的产出者补登
-- -----------------------------------------------------------------------------
--  3.2 之后，`domain` / `ip` / `service` 各自多了一个真实产出者：nuclei 的
--  `matched-at` 既给出主机（domain / ip）也给出端口（service）。
--  这一列不是装饰：`test_every_registered_kind_has_a_real_producer` 会拿它去
--  **夹具里反查**这个 kind 是否真的出现过 —— 声明了却不产出的那一条会立刻红。
--
--  用 `json_insert` 而不是字符串拼接：`produced_by` 是 canonical JSON 数组
--  （`[]` / `["x"]`），字符串拼接在遇到 `["a","b"]` 时也一样对，但它遇到**坏数据**
--  时会静默产出一个畸形数组，而 json 函数会让整个迁移当场回滚。两个 NOT EXISTS
--  是双保险（重复执行不叠加）：一个挡"已经追加过"，一个挡"原来的值里本来就有"。

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'nuclei@1')
 WHERE kind IN ('domain', 'ip', 'service')
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE json_each.value = 'nuclei@1')
   AND json_valid(produced_by);
