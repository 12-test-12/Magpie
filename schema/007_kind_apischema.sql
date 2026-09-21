-- =============================================================================
--  007_kind_apischema.sql —— 登记 `apischema`（接口契约）这个 kind 与
--  `declared_in` 这条关系（开发实施计划 3.4 · openapi / postman / har / curl）
--
--  与 005 / 006 同一条纪律：**只增不改**（红线 R4）。已应用的迁移被改动 →
--  校验和不匹配 → `migrate()` 直接报错。往词表里加一行是**新事实**，改旧文件
--  是篡改历史。
--
--  ---------------------------------------------------------------------------
--  为什么这一步要加一个 kind
--  ---------------------------------------------------------------------------
--  003 的种子注释把准入写死了：没有产出者时不登记。3.4 的四个"接口描述"来源
--  （openapi / postman / har / curl）是 `apischema` 的第一个真实产出者，于是
--  三件套在本步**同时**到位：
--
--    * 身份规则 —— `normalize.apischema_key`（起源：主机[:非默认端口]）；
--    * 样例夹具 —— `fixtures/kinds/apischema.json`（可执行：样例真的被规则算成 key）；
--    * 镜头归属 —— `api`（与 `endpoint` 同一个镜头：契约声明的与流量观测到的
--      是同一个镜头里的两半）。
--
--  ---------------------------------------------------------------------------
--  身份为什么锚在**起源**上，而不是文档标题 / 版本
--  ---------------------------------------------------------------------------
--  这是本步最难的一处取舍，判据是验收标准本身：「**同一个 API 定义从四个来源
--  进来，要认出是同一个**」。四个来源能共同给出的东西**只有主机**：
--
--      OpenAPI  → `servers[].url`            （契约声明它服务在哪个起源上）
--      Postman  → `item[].request.url.host`  （集合里的每条请求）
--      HAR      → `entry.request.url`        （每一跳）
--      curl     → 命令末尾的那个 URL
--
--  标题与版本看起来更"像"一份 API 的名字，但它们**过不了这条验收标准**：
--  HAR 与 curl 里根本没有标题，拿标题当身份等于让这两类来源永远落不到另外两类
--  身上 —— 而"认出是同一个"恰恰就是要认这件事。把一个只有两个来源算得出的字段
--  当身份，规则本身就是错的，不是"还不够完善"。
--
--  所以标题与版本**落成观测，不进身份**（与 `endpoint` 那条"折叠的是身份，
--  不是事实"同一条纪律）。代价是一条**真实**的代价，写在明处：
--
--    * 一台主机上并存两份契约（`/v1/swagger.json` 与 `/v2/swagger.json`）时，
--      两者折成**同一个** apischema。这不是丢信息 —— 两份文档的标题与版本各
--      落一条观测，而 `/api/v1/*` 与 `/api/v2/*` 本来就是**两条不同的
--      endpoint**（3.3 的折叠规则刻意不折版本号），"哪些还在用 v1"在 endpoint
--      那一层就有答案，不需要 apischema 再分一层；
--    * 反过来，把版本放进身份会让"同一个 API 出了一版新文档"变成**另一个
--      实体**，而它俩说的还是同一批接口。
--
--  身份的形状与 `endpoint` 的主机部分**逐字相同**（同一套 `WEB_DEFAULT_PORTS`
--  裁定：省略 / 80 / 443 是同一件事，8080 / 8443 保留）—— 一份契约与它描述的
--  那些接口住在同一个起源上。
--
--  ---------------------------------------------------------------------------
--  为什么这一步**只做 INSERT**（连 produced_by 的补登也是）
--  ---------------------------------------------------------------------------
--  词表的唯一事实来源就是这两张表。Python 侧的镜像（`base.KIND_REGISTRY` /
--  `base.RELATION_VOCABULARY`）与它们逐行一致，`test_kind_registry_matches_the_
--  python_mirror` 断言两边分叉即红 —— 所以这里的每一个字都要与那边对得上：
--  `identity_rule` 的正文还要与 `fixtures/kinds/apischema.json` 逐字一致
--  （`test_every_registered_kind_has_all_three_parts` 断言这一点）。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 1. 新 kind：apischema
-- -----------------------------------------------------------------------------

INSERT INTO kind_registry
    (kind, identity_rule, fixture_path, lens, produced_by, registered_at)
VALUES
    ('apischema',
     '接口契约（apischema）的身份是**这份契约所属的那个起源**：`主机[:非默认端口]`（normalize.apischema_key）。四个来源能共同算出来的只有主机 —— OpenAPI 的 `servers[].url`、Postman 的 `item[].request.url.host`、HAR 的 `entry.request.url`、curl 的目标 URL 都给出它，而标题与版本只有前两个来源有，所以**标题与版本不进身份、只落观测**（否则 HAR 与 curl 永远落不到另外两类身上，而「同一个 API 定义从四个来源进来要认出是同一个」正是本 kind 的全部意义）。**路径不进身份**：`https://api.example.com/v2` 与 `https://api.example.com/openapi.json` 是同一个起源 —— 后者是这份契约被取回来的地址，换个位置不该让它变成另一个实体。端口折的是 {80, 443}（与 endpoint 同一条裁定），8080 / 8443 保留。**代价**：一台主机上并存两份契约时折成同一个 apischema，而「哪些还在用 v1」由 endpoint 那一层回答 —— 路径里的版本号本来就不折。',
     'fixtures/kinds/apischema.json', 'api',
     '["curl@1","har@1","openapi@1","postman@1"]',
     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 2. 新关系动词：declared_in
-- -----------------------------------------------------------------------------
--  方向是 `endpoint --declared_in--> apischema`。逆动词 `declares` 让"这份契约
--  声明了哪些接口"不必另立一列 —— 接口镜头按契约筛选走的就是它。
--
--  为什么端点两端是 endpoint 与 apischema（而不是另建一套"契约里的接口"）：
--  3.4 的四个来源算出的接口身份（`主机[:非默认端口] + 路径模板`）与 dirsearch /
--  gau 观测到的真实路径落的是**同一个** endpoint 实体。这条关系因此能回答两个
--  方向相反的问题，而它们都需要这一半：「契约声明了但没人访问过」（可能是死代码，
--  也可能是漏扫）与「访问过但没进契约」（可能是影子接口 —— 3.6 的体检要看的
--  那一类）。若契约自己建一套接口列表，这两个问题就永远问不出来。
--
--  为什么不用 `exposes`：那条动词在 2.4 就被用作**反例**
--  （`test_unregistered_relation_verb_is_rejected_by_the_database` 断言它写不
--  进去）。反例只有"仍然未登记"时才是一条约束，把一个反例转正等于把它删掉。

INSERT INTO relation_vocabulary (verb, description, inverse, registered_at)
VALUES
    ('declared_in',
     '接口被某份 API 契约声明，或属于某个起源上已经见过的 API（app.example.com/user/{id} --declared_in--> api.example.com）。端点 kind 是 endpoint 与 apischema —— 3.4 的四个来源算出的接口身份与 dirsearch / gau 观测到的真实路径落的是**同一个** endpoint 实体，所以「契约声明了但没人访问过」与「访问过但没进契约」都能一眼看出，这正是把契约挂到接口而不是另建一套接口列表的理由。方向是 endpoint → apischema：逆动词 declares 让「这个起源的 API 有哪些接口」不必另立一列。',
     'declares', strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- -----------------------------------------------------------------------------
-- 3. 既有 kind 的产出者补登
-- -----------------------------------------------------------------------------
--  3.4 之后 `domain` / `ip` / `service` / `endpoint` 各自多了四个真实产出者：
--  接口描述里的 URL 既给出主机（domain / ip）、端口（service），也给出路径
--  （endpoint —— 契约**声明**的那条路径与流量**观测**到的那条是同一个接口）。
--
--  这一列不是装饰：`test_every_registered_kind_has_a_real_producer` 会拿它去
--  **夹具里反查**这个 kind 是否真的出现过 —— 声明了却不产出的那一条会立刻红。
--
--  用 `json_insert` 而不是字符串拼接的理由同 005 / 006：`produced_by` 是
--  canonical JSON 数组，拼接遇到坏数据会静默产出畸形数组，而 json 函数让迁移
--  当场回滚。`NOT EXISTS` 是幂等保险（重复执行不叠加），判据取**最后一个**追加项。
--
--  ⚠️  四个来源在**一个** UPDATE 里补：拆成四条时，任何一条的 NOT EXISTS 写错
--  都只会让数组多一个重复项，而 `test_kind_registry_matches_the_python_mirror`
--  那时报的是"两边不一致"，不会告诉你是哪一条。

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'curl@1',
                                 '$[#]', 'har@1',
                                 '$[#]', 'openapi@1',
                                 '$[#]', 'postman@1')
 WHERE kind IN ('domain', 'ip', 'service', 'endpoint')
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE json_each.value = 'postman@1')
   AND json_valid(produced_by);
