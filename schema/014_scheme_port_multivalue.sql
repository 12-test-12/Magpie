-- =============================================================================
--  014_scheme_port_multivalue.sql — scheme / port 是逐次遭遇的证据（0.20.0）
--
--  010 建了声明表、换了视图，**但只收了 Inbox，没收观测行**；而且当时的清单
--  里没有 scheme / port。真实库（0.19.0 升级上来的太原工业项目）暴露了两件事:
--
--    1. 125 条 open 的 `value_conflict` Inbox 全部是 endpoint 的
--       `scheme`(http/https) / `port`(80/443) —— 与 path 同一个形状:
--    2. endpoint 的**身份是跨 scheme 折叠的**（`app.example.com/` 这个 key
--       不带 scheme），于是同一个 endpoint 必然同时挂着 http 与 https 的
--       观测、80 与 443 的观测。这不是"两个来源在打架"，是**两次遭遇各自
--       留下的证据** —— 用 010 的尺子量，是前者，声明。
--
--  为什么现在补（而不是等下一次导入 reconcile）: 视图换了判据之后，老库里
--  那些项**在库里已经没有任何东西支撑**（010 同款理由）—— 而升级完只是打开
--  体检页的人，会看到 125 条不存在的冲突。
--
--  顺带豁免的取舍（写死在这里，不留给下一个人猜）: 声明是**逐字段全局**的，
--  会连带豁免 service 上的 scheme / port 分歧。service 的 key 本身含端口
--  （`host:port`），它上面的 port 出现多值的概率极低；scheme 在 service 上
--  同理（一个 service 就是一次探测的协议）。按 010 的"窄表"纪律评估过:
--  接受这层顺带豁免，换来的是不再为 endpoint 的天然成对开 125 条假冲突。
--  若哪天真在 service 上出现值得人看的 scheme 分歧，正确动作是给冲突开
--  **另一个**字段名（如 `declared_scheme`），而不是把这一行撤掉。
--
--  视图**不需要重建**: 010 的 `v_field_conflicts` 是 `WHERE NOT EXISTS
--  (SELECT 1 FROM multivalue_fields …)` —— 它每次计算都查声明表，加行即生效。
--  这正是 010 把声明做成数据（而不是写死进视图）的回报。
-- =============================================================================

INSERT INTO multivalue_fields VALUES
  -- endpoint 身份不带 scheme（006 的折叠规则），所以 http→https 跳转前后的
  -- 两次观测都落在同一个实体上。协议可以逐次变，"这个接口是哪个"不变。
  ('observation_field', 'scheme',
   'endpoint 身份跨 scheme 折叠; http→https 跳转是逐次遭遇的证据（与 path 同判）'),
  -- 身份折叠掉 scheme 之后，80 与 443 成对出现（http 与 https 各探一次）。
  -- 与 scheme 同源: 端口是这次遭遇的端口，不是接口的身份属性。
  ('observation_field', 'port',
   'endpoint 双探 80/443 成对出现（随 scheme）; 端口是逐次遭遇的证据');

-- -----------------------------------------------------------------------------
-- 一次性收口: 升级前就已经开着的、按新声明已经不是冲突的 Inbox 项关掉。
-- SQL 形状与 010 逐字同款（判据 = `payload_json.field`，不是 fingerprint ——
-- 哈希不随判据变；`resolved_at` 用 strftime ISO8601 UTC 与 store.now() 同形）。
--
-- 只关 `value_conflict` + `auto_generated=1`: 人手工开的项（manual_required
-- 等）不碰 —— "该不该关"是人的裁决，迁移替人裁决一次就越界了。
-- -----------------------------------------------------------------------------

UPDATE inbox_items
   SET state       = 'closed',
       resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
       close_reason = 'reconcile:014'
 WHERE kind      = 'value_conflict'
   AND state     = 'open'
   AND auto_generated = 1
   AND json_extract(payload_json, '$.field') IN (
           SELECT semantic_field FROM multivalue_fields);
