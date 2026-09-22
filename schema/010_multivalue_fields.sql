-- =============================================================================
--  010_multivalue_fields.sql — 多值字段是**声明**，不是特例（缺陷 A）
--
--  001 的 `v_field_conflicts` 只有一条判据:
--
--      GROUP BY project_id, owner_id, field_namespace, semantic_field
--      HAVING COUNT(DISTINCT field_value) > 1
--
--  它缺一个概念: **有些字段天生是集合**。`path` / `cname` / `method` /
--  `status` 这些字段上的多个值不是"两个来源在打架"，而是**这个实体自己的
--  证据**:
--
--      /user/123 ┐
--      /user/456 ├─→ 一个 endpoint（app.example.com/user/{id}）+ 三条 path 观测
--      /user/789 ┘
--
--  3.3（endpoint 折叠）与 3.4（接口来源）各自撞上这堵墙，各自如实报给发起人，
--  两次的结论都是"修它要动 001 的视图建模，在 R4 之外"。**那句结论是错的**:
--  R4 保护的是**已应用迁移文件的校验和**（改 001 会让新库老库看起来一样、
--  实际不一样），而**视图是纯粹的派生，不承载任何数据** —— 新开一个迁移把
--  视图换掉就行。002 换 `v_integrity_violations` 就是这个先例，照此办理。
--
--  为什么是**声明表**，不是把 `path` / `cname` 写死进视图
--  ------------------------------------------------------
--  多值性是 `(field_namespace, semantic_field)` 的属性，写成 `WHERE
--  semantic_field NOT IN ('path','cname',…)` 是**特例**: 下一个 kind、下一个
--  解析器还会再撞一次，而每一次都要改视图 —— 也就是再开一个迁移。声明是
--  **数据**: 新字段落地时往表里追加一行，视图与代码都不必动。
--
--  为什么这不等于"把体温计摔了"
--  ----------------------------
--  声明是**逐字段**的，一个字段一旦被声明就整个退出冲突域 —— 所以这张表必须
--  **窄**。往表里加行时唯一可用的尺子是这一条:
--
--      这个字段的多个值，是**来源各自枚举出来的枚举项**（一个 endpoint 见过
--      哪些真实路径 / 一个端口上有哪几条 CPE / 一次 fuzz 用了哪些载荷），
--      还是**对同一个问题的两个回答**（这个 service 的 webserver 是 nginx
--      还是 apache）？
--
--  前者是证据，声明; 后者是冲突，不声明。
--  反证在 `test_real_conflict_on_a_single_valued_field_is_still_detected`:
--  两个来源对同一个**单值**字段给出不同值，仍然必须开、且只开一条。
--
--  落地实测（`_probe_multivalue.py --dense`，12 份真夹具 + 一份"1000 行 × 10
--  主机"的密集夹具逐份导入）: 迁移前 `v_field_conflicts` 有 16 条，其中 15 条
--  落在下列被声明的字段上（唯一的例外是 `declared_status` —— 它是契约之间的
--  **真实分歧**，见下）。密集夹具又暴露出 `content_length`：**声明按字段名字面
--  匹配，所以换个名字的同一种事实会被漏掉** —— 这正是"下一个 kind 还会再撞
--  一次"的形状，也是把 `status_code` / `content_length` / `final_url` 这三个
--  httpx 的别名一并声明、而不是只声明被真夹具碰巧撞到的那几个的理由。
--
--  那份脚本是这张表**加行时的仪器**（跑一次就知道新字段落在哪一栏），与声明表
--  一起入库；判据在 `magpie/core/tests/test_multivalue_conflicts.py`。
--
--  哪些字段**故意留在冲突域里**（不是遗漏）: `webserver` / `webserver_version`
--  / `tls_version` / `tls_cipher` / `state` / `reason` / `content_type` /
--  `favicon` / `body_hash` / `cdn` / `title` / `product` / `version` /
--  `declared_method` / `declared_status` / `param.*`。它们是"这个实体**是**
--  什么"的回答，两个来源不一致时值得人看一眼。
-- =============================================================================

-- 声明表本身。`why` 不是装饰: "这个字段凭什么算多值"必须与声明**写在一起**，
-- 否则半年后没人能判断一个新字段该不该往这里加（参考 003 的 kind 登记）。
CREATE TABLE multivalue_fields (
    field_namespace TEXT NOT NULL,
    semantic_field  TEXT NOT NULL,
    -- 一句话理由。NOT NULL 是刻意的: 一条说不出理由的声明不该进得来。
    why             TEXT NOT NULL,
    PRIMARY KEY (field_namespace, semantic_field)
);

-- -----------------------------------------------------------------------------
-- 声明: 全部落在 `observation_field`（观测型贡献）。
--
-- 另一半 `entity_field` 今天**一条都没有** —— 这不是遗漏: entity_field 是
-- 管线按 (kind, key) 声明的身份属性，它的值域本来就设计成单值（`path_template`
-- 在一个 endpoint 上只有一个，否则那个 key 就不会是那个 key）。将来若有
-- entity_field 落成集合，它的理由与这一节不同，应当单独论证后再加。
-- -----------------------------------------------------------------------------

-- 一个 endpoint 见过的**真实路径**。折叠是身份的事，不是事实的事: 三条原始
-- 路径折成一个 endpoint 之后仍然要逐条留证据（`_urls.py` 文件头"两条口径"）。
INSERT INTO multivalue_fields VALUES
  ('observation_field', 'path',
   '折叠出来的 endpoint 见过多条真实路径; 折叠是身份的事，路径是证据'),
-- 同一个 endpoint 被不同的方法请求过（`GET` 与 `POST` 打的是同一个 URL）。
  ('observation_field', 'method',
   '同一个 endpoint 被多个方法请求过（契约的一个资源本来就支持多个方法）'),
-- 同一个 endpoint 每次请求返回的状态码。`200` 与 `404` 说的是"这次请求"，
-- 不是"这个接口是哪个"。
  ('observation_field', 'status',
   '同一 endpoint 每次请求的响应状态码; 它是逐次请求的事实，不是接口的属性'),
-- 每一条真实路径各自的重定向目标（dirsearch 的 `redirect`）。与 `path` 同源:
-- `/images` 与 `/images/` 折成一个 endpoint，两个重定向目标都属于它。
  ('observation_field', 'redirect',
   '每条真实路径各自的重定向目标; 与 path 同源（折叠后仍然逐条是真事实）'),
-- ffuf 的逐次响应测量。同一份报告打同一个 endpoint N 次就是 N 个值 —— 那是
-- 这份报告的形状，不是数据有矛盾。
  ('observation_field', 'length',
   'ffuf 逐次响应的长度; 同一 endpoint 多次请求本就不同'),
  ('observation_field', 'duration',
   'ffuf 逐次响应的耗时; 同一 endpoint 多次请求本就不同'),
-- ffuf 用的载荷。一个 endpoint 被 FUZZ=1 与 FUZZ=2 各打过一次 —— 两条都真。
  ('observation_field', 'fuzz_input',
   'ffuf 逐次请求用的载荷; 一个 endpoint 被多个载荷打过就是多个值'),
-- katana 的 `request.source`: "这个 URL 是从哪个页面、哪个属性（href / form /
-- script）里发现的"。katana.py 的注释把声明理由写死了 —— "它是证据（爬虫的
-- 来路），但**不是**身份的一部分: 同一个 URL 从十个页面链接过来是同一件事"。
  ('observation_field', 'source',
   'katana 的发现来路; 同一个 URL 从多个页面链接过来是同一件事（非身份）'),
-- 一个域名有多条 CNAME（httpx 的 `cname` 数组逐条落观测）。CDN 与多级别名
-- 本来就是一串。
  ('observation_field', 'cname',
   '一个域名可以有多条 CNAME（CDN / 多级别名）; httpx 逐条落观测'),
-- 一个端口上有哪几条 CPE。nmap 的 `<service>` 可以有多个 `<cpe>` 子元素 ——
-- `cpe:/o:linux:linux_kernel:5.15`（操作系统）与 `cpe:/h:hp:proliant_dl380`
-- （硬件）说的是同一个端口的两件事，不是两个来源在打架。
  ('observation_field', 'cpe',
   'nmap 的 service 可以有多条 CPE（o: 操作系统与 h:/a: 应用各一条）'),
  ('observation_field', 'cpe_vendor',
   '每条 CPE 各带一个 vendor; 与 cpe 同源，随 cpe 一起多值'),
-- 一个主机 / 端口上的**多条 NSE 脚本**（nmap 的 `for script in <script>` 逐条落
-- 观测）。脚本 id 不同就是不同的脚本，`script_id + 正文骨架` 是它们各自的身份。
  ('observation_field', 'script',
   'nmap 逐条落每个 <script> 元素; 脚本 id 不同就是不同的脚本'),

-- httpx 服务上的三个**逐次遭遇**字段。它们与上面前缀不同的字段**是同一个事实，
-- 只是名字不同** —— 声明了 `status` 却不声明 `status_code`，是命名上的偶然，
-- 不是判断上的区别（审查者第一眼就会问"为什么这两个不一样"）:
  ('observation_field', 'status_code',
   'httpx 逐次响应状态码; 与 ffuf / har 的 status 是同一个事实'),
  ('observation_field', 'content_length',
   'httpx 逐次响应长度; 与 ffuf 的 length 是同一个事实（实测: 1000 行 × 10 主机的密集夹具上开了 20 条假冲突）'),
  ('observation_field', 'final_url',
   'httpx 每条真实路径的重定向终点; 与 dirsearch 的 redirect 是同一个事实');

-- -----------------------------------------------------------------------------
-- 重建冲突视图: 把**被声明的字段**整个移出冲突域，其余一字不改。
--
-- 为什么整个文件重述视图（而不是 `CREATE OR REPLACE`）: SQLite 没有 ALTER
-- VIEW，改一条规则也只能 DROP + CREATE 整个视图 —— 与 002 同一条纪律。
-- 将来再改请照此新开一个迁移，不要回头改本文件（已应用的迁移改了 → 校验和
-- 不匹配 → `migrate` 直接报错）。
--
-- 分组口径与 001 逐字一致（主体是 entity，不是观测行 —— 见 001 §9 的注释）:
-- 只多了 `WHERE NOT EXISTS` 这一层过滤。
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS v_field_conflicts;

CREATE VIEW v_field_conflicts AS
    SELECT project_id, 'entity' AS subject_kind, owner_id AS subject_id,
           field_namespace, semantic_field,
           COUNT(DISTINCT field_value) AS distinct_values,
           GROUP_CONCAT(DISTINCT field_value) AS values_csv
      FROM v_field_owner
     WHERE NOT EXISTS (
            SELECT 1 FROM multivalue_fields m
             WHERE m.field_namespace = v_field_owner.field_namespace
               AND m.semantic_field  = v_field_owner.semantic_field)
     GROUP BY project_id, owner_id, field_namespace, semantic_field
    HAVING COUNT(DISTINCT field_value) > 1;

-- -----------------------------------------------------------------------------
-- 一次性收口：把升级前就已经开着的、现在**按定义已经不是冲突**的 Inbox 项关掉。
--
-- 为什么放在迁移里做（而不是只靠 import / undo 时的那次 reconcile）: 视图换了
-- 判据之后，老库里那些 `value_conflict` 项**在库里已经没有任何东西支撑**了 ——
-- 而它们要等到"这个项目下一次导入或撤销"才会被 reconcile 收掉。一个升级完就
-- 只是打开体检页的人，会看到 16 条不存在的冲突。迁移是这个状态变化发生的地方，
-- 所以在这里收口。
--
-- 判据用的是 `payload_json.field`（`refresh_observation_conflicts` 落的那一列）
-- 而不是 fingerprint: fingerprint 是值的哈希，视图换了判据**哈希不会变** ——
-- 拿它判断"还是不是冲突"必然判错。而"字段被声明成多值"恰恰就是被作废的全部
-- 理由，所以按字段关是对的。
--
-- `resolved_at` 用 `strftime('%Y-%m-%dT%H:%M:%SZ','now')` 而不是 `datetime('now')`:
-- 前者与 `store.now()`（ISO8601, UTC, 秒精度）逐字同形，后者多一个空格、少一个
-- Z —— 两种格式混在一列里，下游按字符串比较时间的查询会静默错。
-- -----------------------------------------------------------------------------

UPDATE inbox_items
   SET state       = 'closed',
       resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
       close_reason = 'reconcile:010-multivalue'
 WHERE kind      = 'value_conflict'
   AND state     = 'open'
   AND auto_generated = 1
   AND json_extract(payload_json, '$.field') IN (
           SELECT semantic_field FROM multivalue_fields);
