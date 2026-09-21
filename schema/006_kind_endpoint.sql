-- =============================================================================
--  006_kind_endpoint.sql —— 登记 `endpoint`（接口）这个 kind 与 `served_by`
--  这条关系（开发实施计划 3.3 · dirsearch / katana / gau / ffuf）
--
--  与 005 同一条纪律：**只增不改**（红线 R4）。已应用的迁移被改动 → 校验和不
--  匹配 → `migrate()` 直接报错。往词表里加一行是**新事实**，改旧文件是篡改历史。
--
--  ---------------------------------------------------------------------------
--  为什么这一步要加一个 kind
--  ---------------------------------------------------------------------------
--  003 的种子注释把准入写死了：没有产出者时不登记。3.3 的四个"路径列表"来源
--  （dirsearch / katana / gau / ffuf）是 `endpoint` 的第一个真实产出者，于是
--  三件套在本步**同时**到位：
--
--    * 身份规则 —— `normalize.endpoint_key`（主机[:非默认端口] + 路径模板）；
--    * 样例夹具 —— `fixtures/kinds/endpoint.json`（可执行：样例真的被规则算成 key）；
--    * 镜头归属 —— `api`。
--
--  ⚠️  镜头归属这里有一处**已知的文档与库不一致**，必须写下来：
--  《数据模型修订稿 v3.1》§10 把镜头 id 写成 `endpoints` / `flows`，而 003 的
--  `lens` CHECK 冻结的是 `graph` / `assets` / `services` / `api` / `traffic` /
--  `vuln`。**以 003 为准** —— R4 不许改 003，而新增一个 lens id 会被 CHECK 直接
--  拒（迁移当场回滚）。所以 `endpoint` 归 `api`，流量归 `traffic`；GUI 上想显示
--  "接口"就做**显示别名**，不要动 id。
--
--  ---------------------------------------------------------------------------
--  为什么身份是"主机 + 路径模板"，以及折叠规则为什么在 Python 侧
--  ---------------------------------------------------------------------------
--  四条来源给的都是 URL 或路径。把它们原样当身份会让接口列表每抓一次就膨胀
--  一轮（`/user/123` 与 `/user/456` 是同一个接口），所以身份是**路径模板**：
--
--      https://app.example.com/user/123  ┐
--      https://app.example.com/user/456  ├─→  app.example.com/user/{id}
--      https://app.example.com/user/789  ┘
--
--  折叠规则的**正文**（哪个段算模板段、顺序、反例）住在
--  `magpie/core/normalize.py` 的 `SEGMENT_TEMPLATES` 常量表里，**不在这里**：
--  它是一条纯函数，解析器每行都要调它（PARSE-NORM 只允许那一个入口），把正文
--  抄一份到 SQL 里只会多出一份必然分叉的副本。
--
--  ⚠️  两条**必须写在注释里的裁定**（默认值不是裁定，写下来的才是）：
--
--    1. **查询串与片段不进身份**。`/user/123?tab=posts#top` 的 key 是
--       `/user/{id}` —— 查询串常带一次性 token，进身份会让同一个接口每次抓取
--       都变成新实体。但**原始串必须原样留证据**（`path` 观测里 `?` 与 `#`
--       都还在），否则"这个 endpoint 见过哪些真实路径"就永远答不出来。
--    2. **scheme 不进身份**（v3.1 §4.4）：`http://x/a` 与 `https://x/a` 是同一个
--       接口。默认端口同理，而折叠的是 {80, 443} 这两个"默认 web 端口"、
--       **不是**按 scheme 取默认值 —— 按 scheme 取会让 `http://a:80/x` 折成
--       `a/x` 而 `https://a:80/x` 保留成 `a:80/x`，scheme 又从后门进了身份。
--
--  ---------------------------------------------------------------------------
--  为什么这一步**只做 INSERT**（连 produced_by 的更新也是）
--  ---------------------------------------------------------------------------
--  词表的唯一事实来源就是这两张表。Python 侧的镜像（`base.KIND_REGISTRY` /
--  `base.RELATION_VOCABULARY`）与它们逐行一致，`test_kind_registry_matches_the_
--  python_mirror` 断言两边分叉即红 —— 所以这里的每一个字都要与那边对得上：
--  `identity_rule` 的正文还要与 `fixtures/kinds/endpoint.json` 逐字一致
--  （`test_every_registered_kind_has_all_three_parts` 断言这一点）。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 新 kind：endpoint
-- -----------------------------------------------------------------------------

INSERT INTO kind_registry
    (kind, identity_rule, fixture_path, lens, produced_by, registered_at)
VALUES
    ('endpoint',
     '接口的身份是 `主机[:非默认端口] + 路径模板`（normalize.endpoint_key）：路径模板由 normalize.path_template 按 SEGMENT_TEMPLATES 常量表折叠（UUID / 日期 / 纯数字 / 长 hex 段换成 {uuid} / {date} / {id} / {hash}，其余段原样保留 —— `/user/abc` 不折、`/api/v1` 与 `/api/v2` 不折）。**查询串与片段不进身份**（一次性 token 会让同一接口每次抓取都变成新实体），但**原始路径必须原样留在 path 观测里** —— 折叠的是身份，不是事实。**scheme 也不进身份**（v3.1 §4.4），默认端口折掉的是 {80, 443} 而不是按 scheme 取默认值。',
     'fixtures/kinds/endpoint.json', 'api',
     '["dirsearch@1","ffuf@1","gau@1","katana@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 2. 新关系动词：served_by
-- -----------------------------------------------------------------------------
--  方向是 `endpoint --served_by--> service`。逆动词 `serves` 让"这个服务上暴露
--  了哪些接口"不必另立一列 —— 接口镜头按服务筛选走的就是它。
--
--  为什么端点是 service 而不是 host：3.1 / 3.2 之后 `service` 已经是这台机器上
--  "某个端口上跑着的那个东西"（`httpx.service_key`，nuclei 的 `matched-at` 也
--  落同一个实体）。接口挂在 host 上会丢掉端口这一维，而"同一台机器的 443 与
--  8443 各暴露了什么"正是一个接口清单该回答的问题。
--
--  为什么不用 `exposes`：那条动词在 2.4 就被用作**反例**
--  （`test_unregistered_relation_verb_is_rejected_by_the_database` 断言它写不
--  进去）。反例只有"仍然未登记"时才是一条约束，把一个反例转正等于把它删掉。

INSERT INTO relation_vocabulary (verb, description, inverse, registered_at)
VALUES
    ('served_by',
     '接口归属它所在的服务（app.example.com/user/{id} --served_by--> app.example.com:443）。端点 kind 是 endpoint 与 service —— 3.3 的四个来源都给出 scheme，所以有效端口总能补齐，service 与 httpx / nmap / nuclei 落的是同一个实体。',
     'serves', strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 3. 既有 kind 的产出者补登
-- -----------------------------------------------------------------------------
--  3.3 之后 `domain` / `ip` / `service` 各自多了四个真实产出者：接口的 URL 里
--  既给出主机（domain / ip）也给出端口（service）。
--
--  这一列不是装饰：`test_every_registered_kind_has_a_real_producer` 会拿它去
--  **夹具里反查**这个 kind 是否真的出现过 —— 声明了却不产出的那一条会立刻红。
--
--  用 `json_insert` 而不是字符串拼接的理由同 005：`produced_by` 是 canonical
--  JSON 数组，拼接遇到坏数据会静默产出畸形数组，而 json 函数让迁移当场回滚。
--  `NOT EXISTS` 是幂等保险（重复执行不叠加）。
--
--  ⚠️  四个来源在**一个** UPDATE 里补：拆成四条时，任何一条的 NOT EXISTS 写错
--  都只会让数组多一个重复项，而 `test_kind_registry_matches_the_python_mirror`
--  那时报的是"两边不一致"，不会告诉你是哪一条 —— 一条一条对着 Python 镜像写
--  反而更容易看错。

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'dirsearch@1',
                                 '$[#]', 'ffuf@1',
                                 '$[#]', 'gau@1',
                                 '$[#]', 'katana@1')
 WHERE kind IN ('domain', 'ip', 'service')
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE json_each.value = 'katana@1')
   AND json_valid(produced_by);
