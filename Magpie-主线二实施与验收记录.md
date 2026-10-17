# Magpie 主线二实施与验收记录

更新：2026-10-17。当前版本：0.26.0；工作包 C 首批验收版本为 0.23.0，工作包 E 验收版本为 0.25.0。前置交付：主线一本地提交 `c43ef79`，工作包 C 本地提交 `8992368`。下文第 1–5 节保留首批验收口径，第 6 节记录工作包 E（JS 与 Endpoint 身份/业务详情），第 7 节记录工作包 F（Finding 独立生命周期、人工复测与报告导出）。

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
