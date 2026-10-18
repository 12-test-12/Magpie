# Magpie 主线二实施与验收记录

更新：2026-10-18。当前版本：0.29.0 / schema v19；工作包 C 首批验收版本为 0.23.0，工作包 E 验收版本为 0.25.0，工作包 F 首批版本为 0.26.0。前置交付：主线一本地提交 `c43ef79`，工作包 C 本地提交 `8992368`。第 1–7 节保留各批验收口径，第 8 节记录 F 的审查修复，第 9 节记录 G，第 10 节记录人工判定写入收口。

## 1. 本次范围与理由

用户已明确要求开始主线二，继续遵循“本地浏览器先验收、代码留在本地、用户自行推送”的约定。本批完成方案中的工作包 C，并接入部分 E/F 的现有数据入口；整个主线二尚未全部完成。

| 改动 | 原因 | 当前结果 |
|---|---|---|
| 固定业务导航与统一页面骨架 | 旧顶栏与左栏重复，研究入口分散 | 总览、资产、服务、API、JS、工具发现、证据、Imports、Notes；高级入口保留 |
| 总览真实摘要与可点击入口 | 总量、风险分和图窗口容易混淆 | 直接进入对应业务页；不把总量称为新增，不编造趋势 |
| 服务端精确主机、规范路径前两段聚合 | 浏览器虚拟滚动无法解决只读前 500 条的问题 | 主机 → 路径 → 筛选结果 → 详情，聚合计数与列表使用同一过滤条件 |
| 稳定游标与有界详情 | 全量拉取会使大量数据下页面失控 | 列表默认 50、上限 100；详情默认 20；支持继续翻页 |
| 项目/筛选绑定游标、取消与序号校验 | 旧请求可能覆盖新项目或新筛选 | 跨项目游标拒绝，前端忽略迟到响应；15 秒读请求超时提示 |
| 证据索引、贡献链与归档预览 | 原有页面难以回到文件位置 | 保留原始定位、片段哈希、有效贡献；受管归档校验后脱敏预览 |
| JS 地址索引与笔记回查 | 先利用现有数据，避免占位页假装完成分析 | JS 缺少内容关联明确提示；笔记复用既有发现通道 |
| 图谱按需载入、辅助抽屉默认收起 | 日常研究列表不应依赖图谱取数或被日志挤占 | 进入画布才取 500 节点窗口，原图谱与编排契约保留 |

## 2. 数据与兼容约束

- 没有 schema 迁移，没有新增资产、证据或笔记真相库。新模块 `magpie/core/workspace.py` 只做派生读取；HTTP 路由不写 SQL。
- 不变更原生解析、Contribution、规范化、身份、合并、Undo / Redo。撤销后有效观测和来源按原生视图变化；历史证据仍可在证据索引中检查。
- 主机分组使用既有规范键主机提取规则，表示精确主机，不推断注册域归属。无法归属的对象单独分组。
- 路径分组基于当前规范键前两段，沿用现有模板/大小写折叠规则。方法仍是观测，不宣称已经完成方案 §6.1 的身份迁移。
- 证据表没有独立持久化 snippet 正文。本批预览来自校验通过的归档文件：最大读取 16 MiB，最多展示脱敏后 8,000 字符；行定位展示附近上下文，不冒充已验证的精确片段。
- 不允许通过任意外部 `file://` 定位读取本机文件。仅解析受管 `raw/` 下、文件名与原文哈希相符且内容校验一致的归档；缺失、二进制、过大或校验失败均明确说明。
- 列表默认包含当前未合并实体中的活跃与退役对象。证据索引包括历史记录；实体来源页仅列有效贡献引用的证据。来源预览最多列 50 条有效贡献并显示截断。
- 工作包 C 首批仅识别已导入的 `.js` / `.mjs` Endpoint 地址；当时没有 JS 内容关联或静态提取。本次内容关联与有限规则提取见第 6 节；仍不能从资源 URL 推断内容已经导入。
- Findings 保留现有工具命中页；未实现确认、误报、复测等新生命周期。笔记明确通过既有 AI 发现通道保存，不新增第二条写路径。

## 3. 本地浏览器运行

本机没有可用 Python 3.13，实际使用 `.venv-dev\Scripts\python.exe`，版本 **3.12.4**；Node **22.19.0**。不声称已在 3.13 验证。

正常联调（数据保留在 `.magpie-dev/`）：

```powershell
.venv-dev\Scripts\python.exe dev.py
```

复现本批合成验收库：

```powershell
.venv-dev\Scripts\python.exe -u -m bench.workspace_demo
.venv-dev\Scripts\python.exe dev.py --workspace mainline-two
```

生成器仅写 `.magpie-dev/mainline-two/`，已存在数据库时拒绝覆盖。应在启动这个命名工作区前生成，避免第二个写者。生成数据通过既有 ops/store，经过 recompute 与完整性检查；包含空项目、124 对象普通项目、100,000 个接口的大数据项目。所有地址均为合成 `.example.test`，未扫描真实目标。生成耗时本机约 124.70 秒，属于合成夹具构建，不是导入吞吐测试。

启动后打开终端打印的 `http://127.0.0.1:5173/web/?token=…`。token 每次启动变化。Vue 热更新；Python 变化须重启。默认工作区与命名验收工作区互相隔离，不影响原有项目库。无需 GitHub 打包。

## 4. 验证结果

执行 `.venv-dev\Scripts\python.exe check.py`：

- 11 条静态红线通过。
- core 613、API 165、CLI 84、adapters 21 项测试通过，共 883 项。
- 六个设计验收脚本分别 23/23、27/27、71/71、26/26、35/35、45/45 通过。
- `npm test -- --reporter=dot`：23 个文件、162 项前端测试通过。
- `npm run build` 成功，静态构建产物同步到 `magpie/api/web/`；未打发布包。

新增用例验证项目隔离与鉴权、分页完整性、筛选/分组计数一致性、游标错误、方法/来源筛选、字面搜索通配符、JS 地址识别、Undo / Redo 后结果、归档脱敏/哈希校验/外部路径拒绝、前端下钻/返回/分页、迟到响应与错误/空状态。OpenAPI 有意新增四条路由，已升级 minor 版本并更新快照。

完整回归日志保留在 `_scratch/mainline-two-check.log`；最终 API 和前端日志分别为 `_scratch/mainline-two-api-final.log`、`_scratch/mainline-two-frontend.log`。这些本机临时日志不进入提交；本记录保存可恢复的结论。

### 浏览器实际操作

1. 空项目显示导入引导。
2. 普通导入项目完成主机 → 路径 → 接口 → 有效证据 → 归档定位与脱敏上下文。
3. 十万接口项目显示 100 主机分组，当前页仅渲染 50 张卡；主机下有 1,000 接口、两组规范路径各 500 条。
4. 主机范围接口列表翻到第 11 页，仍显示 50 条，并可打开观测详情；这验证了能越过原来的 500 条可见范围。切到 JS 再回 API 保留第 11 页。
5. 普通合成项目组合搜索、精确来源与 POST 筛选，结果为 1 条；写入研究线索后在 Notes 页回查到实际笔记。
6. 实际停止开发服务，接口页显示“无法载入结果”、真实连接错误、重试与重新查询入口；随后重新启动独立验收库。
7. 桌面 1440 × 1000 与窄桌面 1000 × 850 实测；宽屏为并排详情，窄屏为可关闭的全高详情浮层，页面没有额外横向溢出。

### 大数据读侧测量

在同一十万接口合成库直接调用只读模型，本机一次测量：

| 查询 | 结果 | 耗时 |
|---|---|---|
| 主机聚合 | 100 组，返回 50 组 | 0.6971 秒 |
| `h00.example.test` 路径聚合 | 2 组，共 1,000 对象 | 0.6427 秒 |
| 该主机接口列表 | 总数 1,000，返回 50 条 | 1.4459 秒 |

聚合计数和分组窗口改为单次扫描，此前同库分组约 2.3 秒。上述是本机单次读侧测量，不包含浏览器渲染、并发负载或严格性能基准；没有宣称 1M 行、60 秒导入等契约达标。部分筛选仍需扫描规范键，进一步索引优化需另行评估。

## 5. 验收截图

截图均来自本地运行页面，非设计稿：

- [空项目](docs/mainline-two/empty.png)
- [工作台总览](docs/mainline-two/overview.png)
- [普通项目来源证据与归档预览](docs/mainline-two/ordinary-evidence.png)
- [十万接口主机聚合](docs/mainline-two/large-groups.png)
- [第 11 页与主从详情](docs/mainline-two/large-detail.png)
- [真实断线错误](docs/mainline-two/error.png)
- [实际保存后的笔记](docs/mainline-two/notes.png)
- [窄桌面详情浮层](docs/mainline-two/narrow-detail.png)

## 6. JS 增量交付与后续顺序

### 2026-10-16：JS 本地内容关联与确定性候选

- 新增 `js-manifest@1` 本地 JSON 入口：`{"magpie_js":1,"url":"https://…/app.js","content":"…"}`；HAR `response.content.text` 仅在资源 URL 后缀或 MIME 表明是 JS 时处理，支持严格 base64 解码。没有正文的 JS 资源明确记录内容未导入。
- 提取器是标准库 tokenizer 与有限规则，不是完整 JavaScript AST。仅对 `fetch`、`axios.<method>` 的字面量 URL 产生候选；注释、字符串、模板插值、动态 URL 和不确定方法不升级为已观察请求。相对路径保留待确认，不以 JS 资源地址推断运行基址。
- 结果沿用 Endpoint、Observation、Evidence、Contribution 与 Undo/Redo。SHA-256 按解码正文 UTF-8 字节计算；候选保存规则版本、字符偏移、行、脱敏 snippet、content hash 与原始 JSON record path。字符偏移针对解码正文，不是 HAR 原始字节偏移。关联接口保留反向 JS 资源/hash 引用。
- 新增 schema migration 015，仅登记 JS 多值观测和实际 parser producer；不增加事实库，也不变更 Endpoint 身份。索引限 16 MiB、200,000 tokens、500 个候选；超限或未知语法显示部分/限制状态。查询参数值与片段默认脱敏。
- 新增 `bench/e2e/js/app.manifest.json` 与独立合成库命令 `.venv-dev/Scripts/python.exe -m bench.js_demo`；目标库 `.magpie-dev/js-intelligence` 已存在时拒绝覆盖，启动命令为 `.venv-dev/Scripts/python.exe dev.py --workspace js-intelligence`。
- 定向验证：JS parser/提取器及 parser 用例 200 项通过；独立对抗探针覆盖注释/字符串假调用、正则、动态/模板 URL、computed/accessor/spread/dynamic method、Unicode 偏移。额外 v14→v15 回归逐行比较原实体、规范键、观测、原文、证据和贡献，确认迁移不改历史事实且原作业 Undo / Redo 可用。
- 当前局限：不支持任意 JS 语法、变量传播、字符串拼接求值、动态 import、SourceMap、脚本执行或网络请求。有限规则“完成”仅表示规则集遍历结束，不能解释为内容被完整理解或请求实际发送。相对候选需人工确认。

### 冻结代码的最终自动验收

所有代码与夹具修正完成后，主代理独立重新运行 `.venv-dev/Scripts/python.exe -u check.py`，最终 **退出码 0**：

- 静态红线 11 条全部通过。
- core 629、API 165、CLI 84、adapters 21，共 **899 项后端测试**通过。
- 六组设计验收分别 **23/23、27/27、71/71、26/26、35/35、45/45** 通过。
- 前端由实现子代理执行 `npm test`：24 个文件、**163 项**通过（含 JS / API 回链组件回归）；`npm run build` 成功，构建产物已同步到 `magpie/api/web/`。
- 源码、文档的 Git 空白检查通过；构建产物按既有规则排除该检查。

最终完整日志为 `_scratch/js-final-frozen-check.log`，定向结果为 `_scratch/js-focused-tests.log`、`_scratch/js-verify-g3.log`、`_scratch/js-lint.log`。首次沙箱内 pytest 在临时目录清理阶段遇到 WinError 5；最终完整运行经权限工具提升后正常退出。此前开发过程中未通过的日志不作为最终验收依据。

### 本次实际浏览器验收（2026-10-16）

按用户指定，由 GPT-6 Luna 子代理实现，主代理审查并通过本地浏览器实际操作验证；没有推送远程或制作发布包。解释器仍为 Python **3.12.4**，未声称 Python 3.13 验证。

最终冻结代码运行 `.venv-dev/Scripts/python.exe dev.py --workspace js-release-review`，全部数据来自合成 `.example.test` 地址。通过页面新建“JS 冻结代码验收”，实际导入两份同 URL 不同正文的 manifest 与一份 HAR：

1. `app.js` 保留两个不同 SHA-256 的版本；JS 专栏共 6 条版本/候选记录。静态 POST、GET、查询参数名与 Unicode 字符偏移可查，相对路径显示待确认，动态拼接只进入不确定计数。
2. HAR 的无后缀 `bundle` 按 JS MIME 识别，严格 Base64 解码后可查看候选；`missing.js` 显示内容未导入，`invalid.js` 显示解码失败。
3. JS 候选可打开同一 API；API 显示不同版本的 JS 来源哈希并能返回 JS 资源。来源证据可打开受管归档、定位与贡献链。解码正文的 record path 和偏移与原始文件的行定位分别显示，不冒充 HAR 字节偏移。
4. 在隔离库 `js-browser-final` 实际撤销第二版本作业，JS 内容记录从 6 条降至 3 条，第一版本和共有 API 的 JS / HAR 两份证据保留；复原后恢复 6 条记录。
5. 在冻结代码的新库再次导入同一 v1 文件，结果为 `skipped`、0 条记录；导入缺少绝对资源 URL 的清单为 `partial`、0 条记录、1 条坏行，页面明确提示任务结束不代表全部理解。
6. 桌面 1440×1000、窄桌面 1000×850 实际检查，后者文档宽度与视口均为 1000，无额外横向溢出。581×680 小视口的导入报告可滚动到操作区并关闭，详情仍可操作。

合成 HAR 故意没有请求时间，既有流量解析报告 4 条时间相关坏行，因此导入为 `partial`；JS 结果与内容错误均可检查，没有把 HTTP 成功解释成完整理解。裸 `.js` 文件尚无手工资源 URL 关联表单，需使用带显式 URL 的 manifest；不宣称已完成完整 AST 或敏感信息语义分析。

本地浏览器截图：

- [JS 内容版本与候选](docs/js-intelligence/versions-candidates.jpg)
- [API 中的 JS 来源回链](docs/js-intelligence/api-js-backlinks.jpg)
- [撤销第二版本后](docs/js-intelligence/undo-version.jpg)
- [重复导入与错误报告，小视口](docs/js-intelligence/duplicate-error-small.jpg)
- [窄桌面详情](docs/js-intelligence/narrow-detail.jpg)

工作区、临时样例和日志位于 `.magpie-dev/`、`_scratch/`，均未纳入提交；可复用样例与生成器位于 `bench/e2e/js/`、`bench/js_demo.py`。

最终 schema 与解析器代码下已实际运行 `bench.js_demo`，生成 `.magpie-dev/js-intelligence/` 并通过完整性检查；本机可直接运行 `.venv-dev/Scripts/python.exe dev.py --workspace js-intelligence` 查看合成样例。重复运行生成器会拒绝覆盖已有库。本轮临时开发服务已停止，浏览器截图保留在仓库中。

### 工作包 E：Endpoint 身份 v2 与 API 业务详情（最终交付）

- 新增 schema v16 原子迁移，仅更新 endpoint identity rule、producer 版本与 `http_request_sample` 多值语义。旧 Endpoint keys、事实、证据、贡献、alias、merge 和 inactive jobs 不被重写；旧折叠记录显示 legacy，无法恢复的逐请求路径/方法不推造。迁移原子故障注入、调用方事务保护及 v15 合成旧库逐表对照由主代理独立验证通过，详情见 `docs/endpoint-identity-migration.md`。
- v2 machine key 编码 `(scheme, normalized host, effective port, exact case-sensitive path, method)`；方法缺失为 unknown，不等于 GET。查询串与片段不进身份，完整精确 path 不模板化。EndpointSink / ApiSink / HAR / JS manifest / HTTP parsers / OpenAPI / Postman / curl / universal 使用新身份；JS 静态候选只写 `js_static_method`，不伪装 HTTP method observation。generic parser fingerprint 更新并保持 v1 历史 profile 可识别。
- 研究工作区 API Endpoint 详情新增独立请求样本、参数结构、JS 交叉来源分区；样本分页和详情均有上限、Evidence 跳转与显式截断。HAR 参数名合并 request URL 与 queryString；样本不保存 body/header/credential values，query values 遮蔽。Cookie、Bearer、Basic、API key、其他凭据及 unknown 可并列显示；不完整的 HAR / Postman auth inheritance / curl `-b`、`-u` 不标作未观察到凭据，也不据此推断漏洞。
- 用户可读 Endpoint 标题使用 `METHOD scheme://host[:port]/path`；merge 成员列表有界并保留 legacy 身份状态。Endpoint rekey 目标需可解码 v2，merge/rekey collision 限制为同 kind。
- 新增可复用合成 bench：`bench/endpoint_business_demo.py` 与 `bench/e2e/endpoint-business/requests.har`，仅使用 `.example.test` 与合成字符串；数据库生成器拒绝覆盖现有 workspace。
- 子代理最终定向后端 **326 passed**，前端 `npm test -- --run` **166 passed**；`npm run build` 成功并同步生产资源。主代理的全量检查与浏览器结果见下文。实际解释器为 Python `.venv-dev` **3.12.4**，没有声称 Python 3.13 验证。

### 主代理最终自动验收

代码冻结后独立运行 `.venv-dev/Scripts/python.exe -u check.py`，**退出码 0**：core **639**、API **165**、CLI **84**、adapters **21**，合计 **909 项后端测试**通过；11 条静态红线全部通过，六组设计验收为 **23/23、27/27、71/71、26/26、35/35、45/45**。完整日志为 `_scratch/endpoint-final-frozen-check.log`。

新增永久回归覆盖精确身份、未知方法筛选、v15→v16 历史/合并/已撤销作业保留、迁移登记故障原子回滚、逐请求样本分页/脱敏/来源与 Undo/Redo。主代理另外用独立合成旧库逐表比较事实、键、观测、作业、alias 与 merge，确认未改写；故障注入确认 registry 写入失败时整次 v16 回滚，调用方已有事务不会被迁移回滚。独立 HAR 探针覆盖 13 个样本、8 个身份及每页 2 条的无漏重遍历。

全量回归发现并修复了默认打包的兼容问题：HTTP 样本的请求头名称与凭据类别在结构化包中有意省略，摘要和参数保留，并用 `request_metadata_omitted` 及 README/manifest 说明；不改库内样本事实。16 项导出测试通过，完整原文仍只按既有 `--full` 约定包含。风险 golden 只因精确身份使接口数 6→7、对应总分 343→344 更新，评分规则及指纹未改。kind 夹具引用与 G3 可执行身份规则同步新 parser 版本，没有修改旧迁移或放宽静态红线。

### 冻结代码的实际浏览器验收

由用户指定的 **GPT-6 Luna / high** 子代理实施，主代理审查并通过 `dev.py --workspace endpoint-business` 实测。所有数据为 `.example.test` 合成资料；在页面新建“Endpoint E 冻结代码验收”，实际导入独立的 13 请求 HAR、可复用 bench HAR 和三种凭据状态 HAR。

1. 同一路径的 GET、POST、未知方法分立；`/orders/123`、`/orders/456`、`/Orders/123`、HTTP/HTTPS 与 HTTPS:80 分别显示。未知方法筛选实际返回唯一 unknown 对象。
2. 同一 GET 接口显示六次独立样本，Cookie/Bearer 并存、Basic、API key、缺失/畸形请求头及未知状态可区分；参数页列出 query/body 与嵌套参数名，普通业务页不显示参数或凭据原值。另以三个完整/不完整样本确认“未观察到已识别凭据”“凭据状态未知”“观察到 Cookie/Bearer”分别成立。
3. API → 可读 JS 来源与内容哈希 → 候选字符位置 → API 双向操作通过；候选明确说明不等于请求实际发送，并显示同一精确身份是否有 HTTP 观察。修复了跨来源返回后详情分类残留的问题，普通 API 行仍进入请求样本页。
4. 样本可打开受管归档的证据、`record_path`、哈希和有效贡献链。结构化 HAR 的行预览仍是附近上下文，页面明确不保证是精确请求片段；逐请求定位以保存的 JSON record path 为准。
5. 在页面对合成认证状态作业实际执行撤销，`auth-check` 从有效 API 列表退出；复原同一作业后，同一接口及三条样本恢复。原有其他接口仍可研究。未操作真实项目资料。
6. 独立启动 `dev.py --workspace endpoint-legacy-review-final`，确认 v15 旧库的历史模板身份、原合并成员及不可恢复提示可见，未虚构请求样本。修复长合并列表挤压详情内容的问题，通知与成员列表进入滚动区。
7. 1440×1000 桌面与 1000×850 窄桌面实际检查；窄桌面 document width 与 viewport 均为 1000，详情可滚动、关闭并操作来源。验收截图保存在 `docs/endpoint-business/`。

独立 13 请求 HAR 为 `done`、0 坏行；bench HAR 故意未给三个请求的时间，因此作业为 `partial`、3 条时间相关坏行。后续三状态夹具复用同一流量时间窗，已有会话聚合产生两项冲突并如实报告，撤销/复原随贡献变化；不解释成认证漏洞。任务结束不等于原文被完整理解。

截图：[接口与样本](docs/endpoint-business/request-samples.jpg)、[窄桌面认证线索](docs/endpoint-business/narrow-samples.jpg)、[参数结构](docs/endpoint-business/parameters.jpg)、[JS 回链](docs/endpoint-business/api-js-source.jpg)、[来源证据](docs/endpoint-business/evidence.jpg)、[历史身份与合并](docs/endpoint-business/legacy-identity.jpg)、[认证状态](docs/endpoint-business/auth-states.jpg)、[撤销](docs/endpoint-business/undo.jpg)、[复原](docs/endpoint-business/redo.jpg)。开发服务验收后停止；重看合成项目可运行 `.venv-dev/Scripts/python.exe dev.py --workspace endpoint-business`。临时数据库、探针与日志不纳入提交。

本批边界：旧折叠实体不能自动精确拆分，需重新导入含明确请求关联的来源生成新贡献。完整请求样本入口目前来自 HAR；OpenAPI/Postman/curl 保留声明、参数与原观察/证据，没有冒充完整请求样本或认证检查。`apischema` 仍沿用既有起源身份，本批只迁移 Endpoint；JS 仍是有限规则分析，未实现完整 AST、变量传播或 SourceMap。严格性能基准与 Python 3.13 验证未执行。

## 7. 工作包 F：Finding 独立生命周期、人工复测与报告导出（0.26.0 交付）

### 范围与核心机制

- **Schema Migration 017**：新增 `finding_triage` 与 `finding_retests` 表。
  - `finding_triage`：记录研判状态（`candidate` 候选线索、`confirmed` 确认漏洞、`false_positive` 误报排除、`duplicate` 重复记录、`resolved` 已修复）、严重级别（`critical`/`high`/`medium`/`low`/`info`）、判定理由、重复指向 `duplicate_of_id`、处置人与处置时间。
  - `finding_retests`：记录人工复测历史（`still_vulnerable` 仍可利用、`resolved` 已修复、`inconclusive` 结论不确定）、说明、关联证据 `evidence_id`、测试人与时间。
  - 工具扫描导入（如 Nuclei、Gitleaks、Semgrep）默认进入 `candidate` 候选线索状态，绝不自动升级为确认漏洞；处置人通过研判显式确认。
- **写路径与事实一致性**：
  - 新增 `ops.create_manual_finding`：支持在资产（Host、Service、Endpoint 等）上人工录入漏洞，自动产生 `vuln` 实体与 `found_on` 关系，自动记录初始手工证据，完整支持 `ops.undo` 与 `ops.redo`。
  - 新增 `ops.triage_finding` 与 `ops.add_retest`：处置研判与人工复测，复测结果自动联动更新 triage 状态；防止自重复引用（`duplicate_of_id != finding_id`）。
  - `store.ACTION_NAMESPACE` 登记 `finding.triage` 与 `finding.retest` 审计动作；`add_relation` 扩展显式 `src_kind` 与 `dst_kind` 参数，避免非 host 关系的类型推断泄露。
- **工作台与 API 路由**：
  - `workspace.browse` 支持 `category="findings"` 列表浏览，以及 `status`（研判状态）和 `severity`（严重度）分面过滤。
  - `workspace.finding_detail` 返回漏洞详情、目标资产锚点、初始证据、研判处置状态、复测历史记录及可选证据关联。
  - `workspace.generate_report` 导出完整安全研究与漏洞发现报告，支持 Markdown 与 JSON 双格式，包含确认漏洞、复测审计追踪、研究笔记及免责声明。
  - Notes 增强：列表附带关联目标跳转锚点与证据索引。
  - HTTP 路由严格遵从 `SQL-FREE` 红线，使用标准 `ctx.writer.submit` 单写队列；OpenAPI 契约快照已刷新并通过校验。
- **前端工作台交互**：
  - 侧边业务导航新增「漏洞与发现 (Findings)」专属入口。
  - 列表支持状态与严重度徽章、分面过滤、有界分页与详情联动展开。
  - 详情页提供「处置研判 (Triage)」表单（状态流转、严重度调整、误报理由、重复指向）与「复测记录 (Retests)」历史时间线。
  - 复测表单显式展示**红线警示**：网络不可达、超时或端口关闭严禁判定为已修复（resolved），必须选择结论不确定（inconclusive）并补充说明。
  - 支持弹窗新建人工发现、弹窗导出 Markdown/JSON 报告并一键复制或下载。

### 数据与安全约束

- **零主动外联**：所有漏洞记录与复测证据均来源于本地导入或人工录入，无任何网络请求、socket 连接或主动扫描。
- **无独立竞争事实库**：人工漏洞与复测基于既有 Entity / Observation / Relation / Evidence / Contribution 体系，受 Undo / Redo 和完整性校验管理。
- **复测判定红线**：前端和文档明确禁止将网络不可达、超时或服务迁移作为“已修复”证据。

### 自动化测试与验证

- 执行 `.venv-dev\Scripts\python.exe check.py`，**退出码 0**：
  - 11 条静态红线全部通过（包含 C2/C3/R6/R7/R7-ASSETS/PARSE-RO/PARSE-NORM/FLOOR/READONLY/READONLY-REVERSE/SQL-FREE）。
  - 后端单元测试与六组设计验收 Gate（G2/G3/G4/G5）全部通过。
- 执行 `npm test`（Vitest）：25 个测试文件，**167 项前端测试全绿**。
- `npm run build` 成功，前端静态资源已完整同步至 `magpie/api/web/`。
- 本地合成工作区：提供 `bench/findings_demo.py`，可通过 `.venv-dev\Scripts\python.exe dev.py --workspace findings-workspace` 启动本地隔离浏览器验收。

### 局限与后续说明

1. 本批交付完成方案工作包 F（研究闭环：Finding 生命周期、人工复测与报告导出）。
2. 不引入 Agent / Planner / Worker 自主探测；复测仍需安全研究员人工发起并导入证据。
3. 严格性能基准（1M 行）与 Python 3.13 验证按规范保留原基线状态；本地开发使用 Python 3.12.4。
4. 代码保留在本地分支，不执行 `git push`，由用户自行审查并推送到远端仓库。

## 8. 2026-10-17：最新本地提交审查修复与职责拆分

审查基线为本地提交 `02c853f`（0.26.0），开始时工作区干净。本轮按用户要求审查并修复该提交，同时按职责拆分新增逻辑；没有打包或发布。响应新增复测计数/截断字段及报告元数据，遵循仓库约定将应用版本登记为 **0.27.0**，schema 仍为 17。OpenAPI 快照只有 `info.version` 变化，路由和请求模型不变。

### 已修复的问题

| 问题 | 修复与验证 |
|---|---|
| 纯中文标题经 ASCII slug 处理后碰撞；Endpoint 方法/大小写/数字路径也可能丢失差异 | 默认人工发现采用独立 UUID；显式模板附精确目标身份摘要。中文独立发现、GET/POST/精确路径及撤销互不干扰均有永久回归 |
| Notes 首次查询未注册 `ws_display`，独立读连接会报错 | 所有浏览分支先登记查询函数；首次 Notes 请求与 51 条笔记导出均有回归 |
| 创建、研判、复测校验异常未翻译，错误输入产生 500 | 三个 Finding 写端点共用输入错误翻译，返回 400 `E_BAD_FINDING`；覆盖无效目标、无主漏洞的重复项与跨项目写入 |
| 重复项允许缺少主项/循环引用；复测重新确认后仍残留重复指向 | 校验有效项目内主项及引用链；确认/修复复测清空旧重复链接；拒绝处置已撤销/已合并实体 |
| `with conn` 隐式提交调用方事务 | 研判与复测改为可嵌套保存点；外层回滚同时撤回状态、复测及审计写入 |
| Finding 请求未取消、失败静默、处置页发出不支持的详情请求 | 独立面板沿用有超时的请求槽与代次校验，失败可重试；写请求完成也校验项目/对象；处置不再请求 `section=triage` |
| 保存状态刷新会关闭详情；目标资产沿 Finding 分类错误打开 | 后台刷新保留面板；依据实体 kind 选择详情业务；页面实际验证保存后仍可操作，并可跳转主机 |
| 复测详情全量返回，页面没有实际分页 | 内嵌历史最多 50 条并给出数量/截断；复测页使用已有游标端点，每页 20 条；53 条实际翻页为 20/20/13 |
| 已修复等状态的 Markdown 报告遗漏证据；JSON 笔记未统一脱敏，笔记无提示截断 | 所有状态复用同一报告渲染与详情读取；导出完整有效笔记及复测历史，副本按现有规则脱敏，原文不改；两种格式均校验来源定位、哈希及最后一条复测 |
| Finding 中文标题搜索只查机器键 | 同时搜索当前有效标题观测，保持字面通配符转义 |

### 拆分边界

- `finding_ops.py`：人工发现、研判及复测策略，继续调用既有 ops 写入与 Contribution 链；`ops.py` 保留原签名转发入口。
- `finding_queries.py`：统一 Finding 详情、列表摘要、目标、有效初始证据、有界复测及输出副本脱敏。
- `research_report.py`：完整报告装配与 Markdown 渲染；`workspace.finding_detail/generate_report` 入口继续可用。
- `FindingPanel.vue`：处置/复测表单、加载错误、请求保护和分页；`FindingActions.vue`：新建及导出弹窗，项目切换时清理状态。
- 按职责和依赖拆分，没有为满足固定行数机械分文件。`workspace.py` 903→531 行，`WorkspaceDetail.vue` 287→124 行，`WorkspaceView.vue` 282→179 行（含空行）。既有大型 ops 中与本次无关的职责未迁移。

### 自动验证

- `.venv-dev/Scripts/python.exe -u check.py` 完整执行退出 0：11 条静态红线、core **654**、API **165**、CLI **84**、adapters **21**，六组设计验收 **23/23、27/27、71/71、26/26、35/35、45/45** 通过。日志 `_scratch/findings-review-check-final.log`。
- 随后补充 HTTP 错误处理用例并修复列表/详情复用及标题搜索，最终 API 全套 **166 passed**；生命周期/工作台/HTTP/OpenAPI 定向 **26 passed**。版本元数据更新后重新生成并核对 OpenAPI（仅应用版本），再执行契约验证。
- 前端最终 **26 个文件、171 项测试**通过，包含迟到读取、迟到写入、错误重试、真实复测分页、目标跳转和保存时组件保留；`npm run build` 通过并同步静态资源。
- 最终静态红线通过。首次测试因系统临时目录清理权限失败，不计成功；全量重跑改用 `_scratch/review-temp/`。API 最终日志 `_scratch/findings-review-api-final.log`，前端日志 `_scratch/findings-review-frontend-final.log`。
- 实际解释器 **Python 3.12.4**；本机没有可用 3.13，不声称完成 3.13 验证。未运行严格性能基准。

### 本地浏览器实际验收

使用 `dev.py --workspace findings-review-20261017`；测试库仅在 `.magpie-dev/findings-review-20261017/`，基于 `bench.findings_demo` 构造合成资料并增加 51 条不确定复测，不使用真实项目。可重新打开该命名工作区；当前库已存在，不应重新覆盖生成。

1. 查看普通列表、研判详情及中文标题；无 `section=triage` 错误。
2. 53 条复测依次翻到第二、第三页，末页 13 条，最后第 51 条补充记录可达。
3. 实际保存存疑研判；新建“中文发现甲：浏览器回归”，先提交不存在目标，页面显示具体 `E_BAD_FINDING`，改为合成主机后创建成功。
4. 为新发现录入已修复人工复测并关联证据 #8；保存成功提示和详情均保留，列表同步已修复与复测数量。
5. 打开复测证据，显示定位、片段哈希及“原文未归档或已不可用”。该合成证据只登记原文元数据，不冒充受管归档预览。
6. 实际下载 Markdown 与 JSON，核对已修复项的初始/复测证据，Markdown 包含原 53 条历史的末条记录及证据完整哈希；JSON 有 4 个发现、2 条笔记，状态计数一致。
7. 在 1440×1000 与 1000×850 检查详情与滚动；窄桌面 document/viewport 宽度均为 1000。从发现跳转主机显示主机观测；中文标题筛选返回 1 个对象；Notes 页面显示 2 条真实笔记。

截图：[桌面复测](docs/findings-review/retest-desktop.jpg)、[窄桌面复测](docs/findings-review/retest-narrow.jpg)。截图拍摄时界面仍显示 0.26.0，随后仅按响应加键约定更新版本元数据及 OpenAPI 至 0.27.0；没有通过截图冒充另一版本的额外功能验证。

### 保留边界

- 不自动拆分历史已经错误归并的人工发现，避免臆造原始身份；必要时由用户检查原证据后重新录入。既有解析器的漏洞身份规则未改。
- 人工发现创建仍支持 job Undo/Redo；研判与复测是独立人工工作层记录，保留审计，并不声称它们各自已接入通用 job Undo/Redo。真实性与处置仍沿 017 的合并状态词表，本轮未做新的状态模型迁移。
- 没有新增自主探索、主动测试、完整全局统一搜索、独立 Notes 生命周期或 Research Tasks。报告完整导出使用项目内存装配，未承诺超大 Finding 项目的导出性能。
- 窄桌面验收下限为 1000 像素；更小窗口可出现头部挤压和列表可用高度不足，未列为本轮通过场景。脱敏仍使用现有确定性规则，不宣称能识别所有秘密文本。

## 9. 2026-10-17：工作包 G——研究工作流（0.28.0）

### 9.1 用户授权范围与实现

用户明确要求升级三个方向：全局统一快捷检索、Research Tasks / Todo、人工标签与资产重要度。本批完成这三个方向；G 原路线中的本地资产补全、分析插件契约整理仍暂缓。

| 功能 | 实际结果 |
|---|---|
| 统一检索 | 顶部入口、Ctrl+K / ⌘K；服务端检索当前项目资产、API、Finding 标题、Evidence 文件名/定位/哈希、Notes 正文与目标键；分组总数、每组默认 5 项、最多 10 项及“查看全部”分页入口 |
| 分组直达 | 打开 Host、Endpoint 请求样本、Finding 研判、归档证据和所选笔记；不依赖画布或前 500 个实体；查看全部保留关键词 |
| 人工研究任务 | 新建/编辑标题和说明；pending、in_progress、done、shelved 四种状态；每项最多关联 20 个当前项目 Host/域名/IP、Endpoint 或 Finding；每页 50 项、最多 100 项 |
| 对象关联 | 从对象详情直接新建待办；用统一检索选择关联目标；保留原实体 ID，导入撤销或身份合并后显示已失效，继续保留任务与原始关联，不自动迁移到另一个实体 |
| 标签与重要度 | 详情维护标签与 Tier 1–3；标签复用既有 `entity_tags` / `ManualWriter` 与 tag 审计；API/资产列表高亮并可精确过滤；总览区分 Tier 1 主机资产与接口计数并进入同一筛选 |
| 请求与冲突 | 读请求有界、可取消、15 秒超时及代次检查；任务与重要度版本冲突返回 409，页面提供重载入口；项目/对象切换后迟到响应不覆盖新上下文 |

快捷检索使用字面子串匹配，`%` / `_` 不作为用户通配符。不搜索原始文件正文，未交付 FTS 或查询 DSL；既有 `/search` 占位契约保留。搜索结果副本使用现有脱敏规则，原文不变。已归档文件的原始名称沿 `contributions → jobs.coverage_scope.file` 读取，不把归档哈希冒充原始文件名。

### 9.2 数据、迁移与兼容

- 新增原子迁移 **018**：`research_tasks`、`research_task_links`、`entity_research`，均为人工工作层。任务关联与重要度使用现有 Entity ID 和复合项目外键；没有资产或证据副本，没有新增实体 kind。
- `research.py` 负责人工任务、重要度与统一检索；写入经 API 单写队列，事务可嵌套保存点，失败或外层回滚不遗留半条任务、标签或审计。版本冲突不会覆盖较新记录。
- 标签保存为增量，不整串覆盖既有标签；重要度只表示人工业务优先级，不改变漏洞严重度或风险评分。
- 任务与重要度保留人工审计；它们不随导入 Undo/Redo 删除，也未接入通用 job Undo/Redo。用户可编辑任务状态、移除关联、清除重要度和标签；不提供永久删除任务操作。
- 关联的已撤销实体或合并成员仍可读出原来的名称与失效状态；GC 保留工作层引用。旧 schema 上的历史 Undo 验收兼容保留，迁移不改写旧事实、身份、证据、作业与贡献。
- 新增四条 HTTP 路径、七个操作：`GET /workspace/search`；`GET/POST /workspace/tasks`；`GET/PUT /workspace/tasks/{id}`；`GET/PUT /workspace/entities/{id}/research`。既有 `browse` 增加标签/重要度筛选和行摘要；summary 增加待办与 Tier 1 计数。OpenAPI、应用 minor 版本与 CHANGELOG 同步至 **0.28.0**。

### 9.3 本地浏览器实际验收

使用 **Python 3.12.4** 的 `.venv-dev`，没有可用的 Python 3.13，不声称完成 3.13 验证。运行：

```powershell
.venv-dev/Scripts/python.exe -m bench.research_demo
.venv-dev/Scripts/python.exe dev.py --workspace research-workflow --api-port 8876 --web-port 5274
```

本机已有服务占用默认 8765/5173；本次为 `dev.py` 增加可配置的回环端口，同源 Vite 代理自动跟随。没有停止原有其他服务。生成器拒绝覆盖现有数据库；复看已有验收库时只执行第二行。

所有测试数据位于 `.magpie-dev/research-workflow/`，由 `bench.research_demo` 构造，只有合成 `.example.test` 资料；没有操作真实项目。初始 54 项待办，浏览器实际新增一项后三种对象关联，共 55 项。另在页面导入 `bench/e2e/endpoint-business/requests.har`，真实报告为 partial / 83 条记录 / 3 条坏行；缺少请求时间的错误按原有规则显示，没有把任务结束当成完全理解。

实际完成：

1. Ctrl+K 呼出，检索 `portal` 并分别直达 Host、POST Endpoint、Finding 处置页和笔记；搜索 `8443` 显示所选笔记正文。
2. 检索 `requests.har` 返回 4 条证据，打开受管归档定位、JSON record path、片段哈希与有效贡献链。
3. 为接口增加 `core-system` 并改为 Tier 1；保存后详情保留，列表立即更新标签和分级。重启服务后仍保留标记。
4. 从接口新建任务，并用搜索再关联 Host 与 Finding；依次保存 pending → in_progress → done → shelved。重启后重新打开记录，三项关联和最后状态仍在；后续并发验收将最终状态保存为 in_progress。
5. 任务列表第 1 页 50 项，第 2 页 5 项，末条浏览器新建任务可达；标题筛选返回 1 项。当时总览真实展示 2 项待处理/进行中任务、1 项 Tier 1 主机资产和 1 项 Tier 1 接口；并发验收将新任务保存为进行中后，待处理/进行中计数为 3。
6. 从总览 Tier 1 接口入口进入同一重要度过滤；再组合 `payment` 标签，主机分组为 1 个对象，列表也为 1 条。
7. 两个独立页面载入同一任务；第一个保存后，第二个旧版本提交显示 `E_STALE_RESEARCH`，可以重新载入，未静默覆盖。
8. 切换“G 空项目”：任务为 0，`portal` 检索的五组均为 0，无跨项目泄漏。
9. 桌面 1440×1000 与窄桌面 1000×850 检查；窄桌面文档宽度与视口均为 1000，详情浮层可滚动、关闭。并发冲突截图为辅助页默认的 1280×720。581×680 也实际操作过任务创建，但不将该尺寸作为整体视觉验收通过下限。

浏览器发现并修复了复用 WorkspaceView 切换业务分类时，页面恢复逻辑清空搜索目标的问题；新增永久回归，并复验 Notes / Finding / Host 跨分类直达。没有只凭组件测试声明浏览器操作完成。

完成构建后，另外直接打开后端 `8876/web/` 托管的生产静态页面，确认 0.28.0 总览与任务搜索入口可用、关联标题正常，控制台没有 error；不依赖 Vite 热更新。临时服务在验收后已关闭，合成库保留。

真实页面截图：[统一检索](docs/research-workflow/search.jpg)、[研究任务与关联](docs/research-workflow/tasks.jpg)、[标签与窄桌面详情](docs/research-workflow/tags-narrow.jpg)、[并发版本冲突](docs/research-workflow/task-conflict.jpg)、[空项目隔离](docs/research-workflow/empty.jpg)。

### 9.4 搜索窗口与保留边界

只读打开既有十万接口**合成**验收库，使用 SQLite backup 复制到内存，再在副本迁移并检索一个主机。100,000 个实体中命中 **1,000** 个 API，返回 **5** 条，序列化 UTF-8 响应 **2,814 字节**，本机单次核心查询 **0.4777 秒**。源库没有迁移或写入。此测量说明查询不受画布 500 条窗口限制；它是内存副本上的一次查询，不是 HTTP、冷磁盘、并发、浏览器内存或严格性能基准。

- 未增加自动扫描、任务调度、Agent / Planner / Worker 或主动复测。研究任务是人工待办。
- 全局搜索限定当前项目的五类对象；任务另有标题/说明检索。Evidence 包含历史索引，其余使用有效视图；原文正文、外部文件内容、FTS 排序与跨项目聚合不在本批范围。
- Notes 继续沿既有贡献通道与撤销机制，没有新增笔记生命周期。任务/重要度也未加入 Finding 报告或结构化研究报告导出；数据库原有备份可完整保留它们。
- 本批没有发布、远程推送或生成 PyInstaller 分发包；前端生产静态资源按仓库约定同步。严格性能基准与 Python 3.13 验证仍未执行。

### 9.5 最终自动验证

- 冻结代码下执行 `.venv-dev/Scripts/python.exe -u check.py`，**退出码 0**：11 条静态红线、core **660**、API **168**、CLI **84**、adapters **21**，共 **933 项后端测试**通过；六组设计验收 **23/23、27/27、71/71、26/26、35/35、45/45** 全部通过。
- 最终 `npm test -- --reporter=dot`：27 个文件、**178 项**前端测试通过；`npm run build` 成功，产物同步 `magpie/api/web/`。OpenAPI 快照验证通过；源文件与文档的 `git diff --check` 通过（按原约定排除第三方生成模板的行尾空白）。
- 新用例覆盖任务四种状态、关联及分页、字面通配符、跨项目读取/写入拒绝、缺失对象、并发版本冲突、外层事务回滚、标签复用与组合计数、原始文件名/中文标题/Notes 检索、导入 Undo/Redo 及合并/拆分后的原目标保留、v17→v18 事实逐表不变、迟到读写和跨分类搜索跳转。
- 首次完整检查发现历史 v15 验收执行 Undo 时尚无新工作表，已修复 GC 引用检查并重新完整执行；之后补充合并成员关联回归，又在最终代码上重跑完整检查。最终成功日志为 `_scratch/research-delivery-check.log`，前端与构建日志为 `_scratch/research-frontend-final.log`、`_scratch/research-build.log`。临时日志和数据库不纳入提交。
- 真实解释器为 **Python 3.12.4**，Node **22.19.0**。本批代码、文档、测试和静态资源整理为本地 Git 提交；远程推送继续由用户执行。

## 10. 2026-10-18：先写方案，再收紧人工结论写入（0.29.0）

### 10.1 用户约定与实际改动

用户要求先写完修改方案再修改，优先防止已有资源和人工结论被随意写入，Agent 等继续后置。业务代码修改前已写入[研究工作层写入收口方案](./docs/research-write-safety-plan.md)，并更新实施路线和施工计划。本批只交付其中的写入保护、报告依据及摘要跳转。

| 内容 | 已交付行为 |
|---|---|
| Finding 版本 | `triage.version`；工具候选未研判为 0，已有研判行迁移为 1；成功研判/复测递增，含不确定复测 |
| 写入前置条件 | HTTP 与 Python ops 入口必须携带 `expected_version`，禁止隐式读取最新版本代写；缺失/类型错误 422，过期 409 `E_STALE_FINDING` |
| 事务与归属 | 同一保存点内检查当前项目/有效对象/版本和重复引用，保存状态、历史与审计；失败或外层回滚不遗留半条工作记录 |
| 前端冲突 | 保留当前草稿并提示本次未写入，禁用重提；用户显式重新载入最新记录后检查再保存，不自动重试 |
| 人工依据 | 人工创建默认 candidate；新建及研判的非 candidate 状态必须填写非空理由；复测必须填写说明 |
| 报告与页面 | 明示“未关联复测证据，仅有人工说明”；有证据引用也不宣称系统验证了真实性或充分性；历史缺理由仅提示，不改原记录 |
| 总览筛选 | 已确认/待核实分别传 confirmed/candidate；开放任务传只读组合过滤 open，服务端对应 pending/in_progress；分页与总览同范围 |

新增原子迁移 019 仅为 `finding_triage` 添加版本列，001–018 不改；历史身份、原文、Evidence、Contribution、判定和复测不改写。人工 Finding 创建继续使用既有贡献通道，Undo/Redo 保留；研判/复测仍是独立工作层，未宣称接入通用 job Undo/Redo。OpenAPI、版本与 CHANGELOG 同步至 **0.29.0**。旧客户端写请求需要升级以传版本，旧页面须重新载入；本批有意拒绝无版本的覆盖写入。

### 10.2 自动验证

- 实际解释器为 `.venv-dev` **Python 3.12.4**，没有声称验证 Python 3.13。临时目录使用仓库 `_scratch/write-safety-temp/`。
- `.venv-dev/Scripts/python.exe -X utf8 -u check.py` **退出 0**：静态红线 11 条、core **682**、API **169**、CLI **84**、adapters **21**，后端合计 **956 项**；六组设计验收 **23/23、27/27、71/71、26/26、35/35、45/45** 全部通过。日志 `_scratch/write-safety-check.log`。
- 全量完成后补充 3 个“首次人工研判/复测从版本 0 创建版本 1”回归；最终相关四个测试文件 **48 passed**，日志 `_scratch/write-safety-targeted-final.log`。没有将这 3 个新增测试冒充为前一轮全量检查的计数。
- 前端完整测试 **27 文件 / 181 项**通过；`npm run build` 成功，静态资源正常生成至 `magpie/api/web/`，日志 `_scratch/write-safety-build.log`。OpenAPI 快照已确认并验证；快照变化仅为版本、创建默认状态和两个写请求的必填版本。
- 新增回归覆盖研判与复测的六种先后交错、无依据/无效版本/跨项目证据拒绝、事务版本与审计一起回滚、v18→v19 逐表保留及迁移登记故障完整回滚、历史依据缺口、组合任务分页与游标绑定、前端冲突草稿/显式重载和摘要范围。
- 实施过程中新增迁移曾错误提前 COMMIT，首次定向检查未通过；已修正为沿用 runner 的原子登记提交，并通过故障回滚回归。前端新增测试曾使用不完整模拟数据，修正后完整重跑通过。以上失败均不作为验收成功依据。

### 10.3 本地浏览器实际验收

桌面 computer-use 工具两次报告 native pipe unavailable，没有控制用户已有窗口。改用运行时自带的 Playwright 与本机已有 Chrome，实际启动 Chromium 打开本地应用；未安装新依赖、未修改用户浏览器配置。以下均为真实浏览器页面操作，不以组件测试替代。

最终验收使用新合成库 `.magpie-dev/write-safety-20261018-final/`，通过现有 `bench.research_demo` 构造，没有覆盖旧验收库或真实项目。开发服务由下列入口启动：

```powershell
.venv-dev/Scripts/python.exe dev.py --workspace write-safety-20261018-final --api-port 8882 --web-port 5282
```

本机日志为 `_scratch/write-safety-dev-final.log`。开发服务停止后可用同一命令重新打开已有合成库，不应再运行生成器覆盖它。

实际操作结果：

1. 总览开放任务计数为 2，点击后筛选为“待处理与进行中”，列表真实返回 2 项；已确认计数为 1，点击后同状态返回 1 条；待核实为 0，点击后为空且筛选为 candidate。
2. 页面新建“写入保护：浏览器合成验收”，默认 candidate，返回 Finding #6 / version 1；未自动确认为漏洞。
3. 两个独立页面同时打开同一发现。A 填理由后保存 confirmed / version 2；B 提交旧版本，真实 HTTP 409，草稿仍在、保存按钮禁用，没有自动重试。
4. B 点击重新载入后读到 A 的理由和状态；再切人工复测，填说明、不关联证据，登记 resolved / version 3。页面明确显示仅有人工说明。
5. 实际下载 Markdown 与 JSON；校验新发现保留初始来源、人工复测说明和 `manual_statement_only`，Markdown 不再出现“经后续导入的复测证据验证修复”。下载文件在 `_scratch/write-safety-report.md`、`_scratch/write-safety-report.json`。
6. 1440×1000 与 1000×850 实际检查，窄桌面文档宽度为 1000，详情可滚动，操作与关闭入口可达。空项目没有其他项目的发现。
7. 直接打开后端 `8882/web/` 的生产静态资源，显示 0.29.0，总览可用；浏览器捕获的 pageerror 为 0。最终脚本结果 `_scratch/write-safety-browser-result.json` / `_scratch/write-safety-browser-final.log`。
8. 为让截图实际显示冲突提示，再用两个页面打开同一合成发现：A 重新确认到 version 4，B 旧版提交仍被拒绝，滚动到冲突提示拍照。因此当前保留库中 #6 最终是 confirmed / version 4；第 5 项导出文件保留当时 resolved / version 3 的真实结果。

截图：[冲突拒绝与显式重载](docs/research-write-safety/finding-conflict.jpg)、[无关联证据复测](docs/research-write-safety/manual-retest.jpg)、[窄桌面](docs/research-write-safety/manual-retest-narrow.jpg)、[同范围开放任务](docs/research-write-safety/open-tasks.jpg)。初次浏览器脚本遇到标签精确匹配超时，修正脚本选择器后在新命名库完整重跑；未用初次部分执行冒充整体验收通过。

### 10.4 保留边界与交付

- 本批收紧的是人工结论的写入与输出，不声称所有历史数据或所有入口都已完成全面写入审计。保留原生解析、统一归一化、身份、Contribution、Undo/Redo 契约，不重写历史事实。
- 非空理由不等于正确结论；现有合并状态词表保持，真实性/处置/复测三维状态仍待独立设计迁移。JS/Notes 扩展、任务报告、性能索引优化与严格性能基准继续后置。
- Agent / Planner / Worker、自主探索、主动测试与自动复测均未增加。
- 工作区原有 `dev.py` 行尾空白改动保留且不纳入本批提交；没有操作真实项目、推送远程、发布或制作分发包。前端生成资源随源码同步，本批整理为本地提交供用户检查。
- 最终源码与文档 `git diff --check` 通过（排除用户原有 `dev.py` 和第三方生成资源）；001–018、normalize/store/ingest 的 Git 差异为空。本轮两个开发服务均已停止，8881/5281 与 8882/5282 不再监听，合成资料与截图保留。

### 10.5 用户要求的带窗口浏览器复验（2026-10-18）

用户再次要求在浏览器实际操作。本次另建 `.magpie-dev/browser-recheck-20261018/` 合成库，用 `dev.py --workspace browser-recheck-20261018 --api-port 8883 --web-port 5283` 启动；没有操作真实项目或覆盖原验收库，没有修改业务代码。

桌面控制连接仍报告 native pipe unavailable，改用本机已有 Chrome，以 **headless=false 的可见窗口**实际执行页面点击、填写、提交、切换页面与下载。原计划录像因缺少 Playwright ffmpeg 编码器而未生成；没有安装编码器，改存带截图与 DOM 快照的操作 trace，不将 trace 宣称为视频。

本次 11 个检查点全部通过：开放任务 2 项、已确认 1 项、初始待核实 0 项均从总览进入同范围；新建默认 candidate/version 1；确认时空理由触发浏览器必填校验且没有发出写请求；双页面旧版本提交真实返回 409、草稿保留且禁用重提；显式重载后可继续保存无附件复测到 resolved/version 3；1000×850 无横向溢出；实际下载 Markdown/JSON 均明确仅人工说明；空项目不串数据；直接打开后端生产静态页面显示 0.29.0。捕获 pageerror 为 0。

页面新增 Finding #6“浏览器复验：人工判定写入保护”，最终 resolved/version 3。新截图：[总览](docs/browser-recheck-20261018/overview.jpg)、[版本冲突](docs/browser-recheck-20261018/finding-conflict.jpg)、[人工说明复测](docs/browser-recheck-20261018/manual-retest.jpg)、[窄桌面](docs/browser-recheck-20261018/manual-retest-narrow.jpg)、[开放任务](docs/browser-recheck-20261018/open-tasks.jpg)。完整操作记录为 `_scratch/browser-recheck-trace.zip`，结果与日志为 `_scratch/browser-recheck-result.json`、`_scratch/browser-recheck-visible.log`，实际下载为 `_scratch/browser-recheck-report.md/json`。

本次服务保留给用户继续试用，浏览器地址见 `_scratch/browser-recheck-dev.log`；token 不写入本记录。源码与既有两个用户工作区改动均未修改，未新增提交、推送或发布。
