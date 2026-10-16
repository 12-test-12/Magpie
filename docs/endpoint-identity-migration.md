# Endpoint 身份迁移方案

更新：2026-10-16。状态：设计经主代理审查，核心身份、v16 迁移和 API 业务详情已交付；主代理全量检查与隔离浏览器验收通过。实际验证及剩余边界见[主线二验收记录](../Magpie-主线二实施与验收记录.md)。本文记录工作包 E 的迁移契约、实现范围与边界。

## 目标身份与规范化

Endpoint v2 的身份为 `(scheme, host, effective_port, exact_path, method)`。scheme 与 host 按 URL 规则规范化（scheme、DNS 主机小写；IP 使用规范文本），端口使用显式端口或该 scheme 已知的默认端口。未知 scheme 且无显式端口时以 null 表示有效端口未知。默认端口仍属于身份，因此 `http://h:80/x` 与 `https://h:443/x` 不同。path 区分大小写并保留具体段；不执行模板折叠、不丢弃路径版本、不把不同具体路径归为同一 Endpoint。查询串和片段不进入 `exact_path`；参数名进入有限的业务结构，原始参数值通过既有原文归档保留，普通样本中的 query 值全部遮蔽。

方法遵循 HTTP token 规范化为大写。缺失或不可解析的方法是 `unknown`，不会替换成 GET，也不会与明确的 GET 合并。为保持解析契约，parser 继续只产生 RawEntity / RawObservation / RawRelation；身份 key 由既有 ingest 规范化与写入入口构造，parser 不写库。

新 key `ep2:` 使用规范 JSON 的 Base64URL 编码，序列化所有五个分量（含 null 与 unknown），不依赖通过分隔符拆分任意 URL path。解码严格校验 canonical Base64 / JSON 与重建后的 key。读侧统一通过 normalize 解码；任何筛选、显示或服务归属逻辑不得自行按字符串猜测 Endpoint 的 host/path/method。界面显示易读的 `METHOD scheme://host[:port]/path`，机器 key 只用于内部唯一性。旧版未带 v2 标记的 Endpoint key 保持原样且被识别为 legacy-folded。

## 历史资料迁移

旧 key 是“主机[:非默认端口] + 路径模板”；在旧模型中 scheme 与 port 被声明为多值观测，method 是实体级观测，path 观察最多 400 字符并可能截断。旧 `observations` 不携带可可靠还原请求样本的贡献到 Endpoint 身份映射；同一 legacy entity 上的 path × method × scheme/port 不能做笛卡尔积。即使从归档重新解析，也不能证明一个历史 contribution 属于哪种组合，也不能覆盖已缺失/截断/不可访问的证据。

迁移不改写旧 entity key、observations、contributions、Evidence、Contribution、job 状态、alias、merge 事件或 supersession，也不把历史实体伪装成已转换。旧实体继续作为 `legacy-folded` 显示，其可见说明是“历史身份按模板折叠；确切路径、方法及请求组合无法从现存事实完整恢复”。只有之后重新导入包含原请求关联的来源，才会以新贡献生成精确身份。所有原始观察与贡献链可照常回查。身份状态可由 key 版本判定，不另建事实库；若实现选择增加持久状态，必须证明它是从既有事实可重建的缓存。

导入新版本时生成 v2 精确身份，新 Endpoint 不自动 alias 到 legacy entity。未能确定 scheme、有效端口或 base 的记录沿用候选/坏行行为，不臆造 origin。历史记录要获得精确身份须由用户之后重新导入包含原请求关联的来源；那是新贡献，有自身 Evidence/Contribution lineage，并可正常撤销。旧任务（含 inactive/undone job）及其原始归档不回写、不重开。普通重导入遵循现有 parser fingerprint 和幂等规则；parser 版本变化生成的新贡献仍按 supersession 规则处理，不能在迁移里伪装为旧贡献。

## 身份 overlay、merge、rekey、undo/redo 与 rebuild

- Endpoint v2 key 通过既有 `entity_keys` / `entity_alias` 解析与身份 overlay 管理，不移动事实。手工 merge 保留原成员与 merge event；详情按上限列出各成员 readable identity 和 legacy 标识。
- legacy 实体不与任意 v2 实体自动 merge。用户显式 merge 时照既有 merge/unmerge 与 undo/redo 生命周期工作，并保留“成员中含不可恢复 legacy 身份”的标识。
- Endpoint rekey 目标只接受可验证的 v2 key；不允许把 legacy 多值观测猜成新身份。merge 与 rekey collision 必须处于同一 project、同一 kind；明确 merge 仍允许 legacy 与 v2 Endpoint 同组，但详情会显示 legacy 成员。
- 新 Endpoint 与样本、参数、JS 交叉引用均是普通贡献事实；undo 通过 contribution supersession 让有效视图撤回，redo 恢复同一贡献，原 Evidence / Contribution lineage 不删除。对同一 URL 的 unknown-method key 与已知 method key 分立。
- rebuild 由活跃贡献和 `entity_keys` 重新投影；v2 key 自含身份，legacy key 原样恢复。迁移不产生缺少原始贡献支撑的新 Endpoint。inactive job 的贡献可在历史中检查，但不参与有效 Endpoint 视图。

schema v16 只改 endpoint identity rule、producer 列表和 `http_request_sample` 多值声明，不回写历史事实。它以显式事务运行，migration ledger 与更新一起提交；runner 保持调用方事务边界保护，v11 既有事务脚本不变。迁移文件追加，不修改 v1–v15。

## API 业务详情与有界样本

Endpoint 详情提供独立、有界的请求样本页，按原始 Evidence/Contribution 定位；当前 HAR 样本展示方法、脱敏 URL、查询与请求体参数名、请求头名称状态、凭据类别、响应 MIME/大小/状态与来源记录位置。样本按单次来源记录，不从实体级多值观测拼合。legacy Endpoint 显示历史样本无法还原；截断和缺字段均显式显示。

凭据事实允许并列：分别记录 Cookie、Bearer、Basic、API key、其他已识别凭据及 unknown 状态。只有导入范围内的 HAR header 与参数名检查完整且没有已识别类别时，才标为“未观察到已识别凭据”；HAR headers 缺失或畸形、字段截断、query/body 参数名未完整读取时一律保留 unknown。Postman auth inheritance 与 curl `-b` / `-u` 未形成完整样本通道，不能标为无凭据。观察到 Basic/API key 等时不得归入“未观察到凭据”。不会从缺少凭据推断未授权或漏洞。

JS 候选与 HTTP 观察并列对照，并保留各自 Evidence、Contribution、JS content hash、规则与偏移。仅当规范化后的完整 v2 identity 五元组一致时联到同一 Endpoint；静态候选不会产生 HTTP method observation，详情会说明是否存在同身份 HTTP method / HAR 样本。来源链接可从 Endpoint 到每个 JS/HTTP 来源，也能反向返回。普通业务页只展示参数名、位置、类型和契约必填声明，不展示参数值或自由文本说明；query 值全部遮蔽。请求样本与片段使用既有脱敏策略，Cookie、Bearer token、疑似 secret 原值不展示。path 中可能带有敏感值的片段按既有 secret 检测与遮蔽策略处理。

列表、详情和原文读取均有明确上限及截断字段；截断后仍可查完整计数与来源。接口响应不因截断标记“已完全理解”。证据原文只经受管归档哈希校验与项目授权读取，不能接受任意本机路径。

## 测试和实现检查点

实施验证覆盖：

1. 规范化对比 scheme、大小写 host、有效端口、精确且大小写敏感 path、query/fragment 分离、未知 scheme/port、unknown method 与显式 GET。
2. v15→v16：旧 key、观察、贡献、证据、合并、alias、inactive/undone job 和原始文件逐行指纹不变；无法恢复的实体呈 legacy 状态；迁移中断后无半迁移。
3. import→undo→redo→rebuild 的 v2 identity、样本与来源引用保持稳定；reimport 幂等；JS/HTTP 不完整身份不错误匹配。
4. endpoint 样本、参数名、并列凭据事实、脱敏、来源跳转、有界读取/截断、项目隔离和 legacy 告知的 API / 前端测试。
5. 审计所有 identity 使用者：EndpointSink、ApiSink（声明方法纳入身份）、HAR、JS、OpenAPI、Postman、curl、universal、workspace host/path、query 过滤、rekey/merge、fixture/registry、版本与 OpenAPI 快照。
6. 更新合成 bench 样例与生成器，所有目标使用 `.example.test`；不接触真实项目库。Python 优先 3.13，否则如实使用 `.venv-dev` 3.12.4。代码测试和前端构建由实现代理记录；隔离浏览器和全量回归由主代理执行。
