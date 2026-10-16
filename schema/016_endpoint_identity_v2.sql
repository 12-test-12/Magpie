-- Endpoint v2 identity contract. The migration runner records v16 and commits this
-- still-open transaction together; on any error it rolls back the whole migration.
BEGIN IMMEDIATE;

UPDATE kind_registry
   SET identity_rule = 'Endpoint v2 identity is (scheme, normalized host, effective port, exact case-sensitive path, method). Effective port uses the explicit port or a known scheme default; unknown remains NULL. Query and fragment are sample data, not identity. Method is normalized uppercase; missing/invalid method is the distinct unknown value, never GET. No path template folding. Historical pre-v2 keys remain legacy-folded and are not reconstructed.'
 WHERE kind = 'endpoint';

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'dirsearch@2', '$[#]', 'ffuf@2',
                                 '$[#]', 'gau@2', '$[#]', 'katana@2', '$[#]', 'curl@2',
                                 '$[#]', 'openapi@2', '$[#]', 'postman@2', '$[#]', 'har@3',
                                 '$[#]', 'js-manifest@2')
 WHERE kind IN ('domain','ip','service','endpoint')
   AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value='dirsearch@2');

UPDATE kind_registry
   SET produced_by = json_insert(produced_by, '$[#]', 'curl@2', '$[#]', 'openapi@2',
                                 '$[#]', 'postman@2', '$[#]', 'har@3')
 WHERE kind = 'apischema' AND json_valid(produced_by)
   AND NOT EXISTS (SELECT 1 FROM json_each(produced_by) WHERE value='curl@2');

INSERT INTO multivalue_fields(field_namespace, semantic_field, why)
VALUES ('observation_field', 'http_request_sample',
        '同一个 Endpoint 可有多次不同 HTTP 样本；样本保留各自的 evidence 与 contribution 来源');
