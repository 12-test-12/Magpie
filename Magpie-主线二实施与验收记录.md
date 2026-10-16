# Magpie 主线二实施与验收记录

更新：2026-10-16。当前版本：0.24.0；工作包 C 首批验收版本为 0.23.0。前置交付：主线一本地提交 `c43ef79`，工作包 C 本地提交 `8992368`。下文第 1–5 节保留首批验收口径，第 6 节记录本次 JS 增量。

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

1. 工作包 E：先设计 Endpoint 身份迁移与历史兼容，再做方法、样本、参数和认证线索的完整业务组织。目前仍为既有观测字段和证据回查。
2. 工作包 E：本批 JS 本地内容关联、版本、有限规则提取与偏移证据已通过本地浏览器验收；裸 JS 手工关联、完整 AST / SourceMap 和更广的静态语法分析仍待后续。
3. 工作包 F：独立 Finding / Note 的生命周期、关联、人工复测与报告。工具命中不自动升级为确认漏洞。
4. 既有 Jobs / Inbox / 风险等高级页保留原行为与页面容量；本批有界查询覆盖的是新工作台页面，不声称旧高级页全部已经分页重做。
5. 严格性能基准与 Python 3.13 验证尚未执行。后续按真实规模评估索引；不引入主动扫描、自动复测或 Agent / Planner / Worker。

代码直接保存在 `D:\Magpie`，整理为本地提交；远程推送由用户执行。后续会话先读 AGENTS、优化方案与两份验收记录，避免依赖对话记忆。
