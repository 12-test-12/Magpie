-- JS 内容与静态引用在同一资源/endpoint 上天然有多个版本和候选。
-- 只扩充多值声明及 parser producer；事实、Evidence 与撤销仍由现有管线维护。
INSERT INTO multivalue_fields VALUES
  ('observation_field', 'js_content_version', '同一 JS 资源可导入多个内容版本；每个版本以解码正文 SHA-256 区分'),
  ('observation_field', 'js_candidate', '同一资源可含多个静态调用；逐条保留候选及其内容版本与字符位置'),
  ('observation_field', 'js_content_status', '重复导入可有不同内容可用状态；属于来源观察'),
  ('observation_field', 'js_static_method', '静态代码可引用多个方法；不代表浏览器实际请求'),
  ('observation_field', 'js_static_param', '静态代码可引用多个参数名；不代表浏览器实际请求');
INSERT INTO multivalue_fields VALUES
  ('observation_field', 'js_source_ref', '同一接口可被多个 JS 内容版本引用；每条引用保留来源资源键、hash 与记录位置');

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'har@2')
 WHERE kind = 'endpoint'
   AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value = 'har@2')
;
UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'js-manifest@1')
 WHERE kind = 'endpoint' AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value = 'js-manifest@1');

UPDATE kind_registry SET produced_by = json_insert(produced_by, '$[#]', 'har@2')
 WHERE kind IN ('domain','ip','service','flow') AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value = 'har@2');
UPDATE kind_registry SET produced_by = json_insert(produced_by, '$[#]', 'js-manifest@1')
 WHERE kind IN ('domain','ip','service') AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value = 'js-manifest@1');

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'har@2')
 WHERE kind = 'apischema' AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value = 'har@2');
