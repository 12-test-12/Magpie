# 现有研究能力完善：实施与验收

日期：2026-10-21。本地分支 `codex/research-workflow-completion`，基线 `82c17a4`，交付 **0.31.0 / schema v20**。先完成[方案](research-workflow-completion-plan.md)，再实施。Agent 无限期后置；未修改未跟踪的 Agent 方案。

## 实际实现

1. **JS 本地接入**：选择 UTF-8 裸 JS 或粘贴内容，显式填写绝对资源 URL；生成标准 manifest，保存原始名称与正文哈希，经原有预览/导入、归档、Evidence、Contribution 和 Undo/Redo 处理。多路径使用自动仲裁，避免 JS 输入覆盖整批解析器选择。暂存文本和生成的 manifest 均有 16 MiB 上限；拒绝路径穿越、Windows 设备名和无效编码。
2. **有限分析增强**：静态字符串字面量拼接可形成调用候选；已有敏感串规则和字符串 URL 产生脱敏候选，附规则版本、内容哈希、解码字符位置与来源记录路径。规则 v2、`js-manifest@3`、`har@4` 同步登记；旧版本夹具保留，新夹具逐项核对旧 Endpoint、请求/会话事实与 JS 回链不变。
3. **研究笔记**：原始正文仍为既有 Observation/Evidence/Contribution；人工修订按版本追加，保存修改理由、归档状态和 Evidence ID 历史。跨项目、无效目标、旧版覆盖均被拒绝；相同正文的已有修订不能被“新建并关联证据”隐式覆盖。原始文本、原始笔记证据和历史版本可查；历史每页 20。列表、快捷检索、总览和报告读取同一活动修订口径，归档可显式查询。
4. **资料关联**：任务增加证据关联，旧客户端省略新字段时保留已有引用。对象/证据详情可新建笔记、创建带关联的任务、分页回查笔记及进入同范围任务。失效实体保留原关联，不重定向到其他资产。
5. **旧入口**：HTTP `/search` 和 CLI search 复用工作区字面检索（分组总数及前 5 项），不解析 DSL。HTTP `/diff` 复用有效贡献与作业归属查询，分页默认 50、最多 100；新增 GUI 增量页。`/paste` 实际经原生导入队列执行；GUI 暂存后沿原有导入流程运行。
6. **人工字段**：`PATCH /entities/{id}` 的人工名称/审核状态通过 ManualWriter 和同一事务写入工作层，须带读取版本与理由，冲突 409。原始 key 和解析观测保留；人工名称可检索、列表展示，审核状态只表示人工研究进度。标签与保护标记旧请求继续有效。

## 兼容与验证边界

- 仅新增迁移 020；001–019 未修改。新表是人工修订和既有 ID 引用，不是资产/证据副本。迁移登记失败整体回滚；v19 原事实逐表保留，原有笔记 Undo/Redo 仍可用。
- 人工修订、任务证据关联和人工名称/审核状态保留审计，不宣称已接入通用作业 Undo/Redo。笔记原始作业撤销后不再列作有效笔记，Redo 恢复同一笔记及其修订；人工历史不删除。
- JS 不执行脚本、不推断相对基址、不下载远程资源。完整 AST、跨作用域变量传播、SourceMap 映射和敏感串语义判断仍未交付。静态引用/敏感串候选不等于已发送请求或确认泄露；导入作业 done 也不等于 JS 已完整理解，内容页明确显示 partial 和未支持数量。
- 增量按作业开始时间统计当前有效贡献，同一秒作业属于同一窗口；不把 observed_at 不确定值当精确时间。CLI/MCP 的原有完整事实明细形状保留；HTTP 是有界摘要。
- 未运行严格性能基准、Python 3.13 或冻结包验收；未打包、发布或远程推送。

## 自动检查

实际环境：Windows、`.venv-dev` **Python 3.12.4**、Node **22.19.0**；`py -0p` 未列出 Python 3.13。不将 3.12 验证声称为 3.13。

- 前端完整 **29 文件 / 190 项通过**；正式构建通过。日志 `_scratch/materials-frontend-final.log`、`_scratch/materials-build-final.log`。
- 最终 `.venv-dev/Scripts/python.exe -X utf8 -u check.py` **退出 0**：11 条静态红线、core **715**、API **170**、CLI **84**、adapters **21**，后端合计 **990 项**；六组设计验收 **23/23、27/27、71/71、26/26、35/35、45/45** 全部通过。日志 `_scratch/materials-check-delivery.log`。核心新增回归涵盖笔记历史/归档/报告一致、版本冲突、跨项目、外层回滚、任务证据引用兼容、原事实不变、v19→v20 登记失败回滚和历史 Undo/Redo；HTTP 覆盖原生粘贴队列和本地 JS 导入。
- 首轮检查发现新解析器指纹缺少新版本夹具/登记镜像，随后补齐并保留旧夹具；G3 又发现 kind 夹具仍指向旧的当前解析器版本，修正当前版本指针后 **26/26** 通过。静态资源检查还发现示例 URL 被严格红线识别为外链，已改为无外域占位文字并正常重建，未放宽红线。
- 一个早期测试集合因不同目录同名测试模块而收集失败；新 HTTP 文件改为唯一名称。一次直接跨全部套件调用也碰到仓库原有 CLI/adapters 同名测试，最终依照 `check.py` 的分套件入口验证。失败记录不充当通过结果。

## 本地浏览器实际验收

使用 computer-use 的浏览器控制工具（真实 Chromium 页面），未操作用户真实项目。合成生成器拒绝覆盖已有库：

```powershell
.venv-dev/Scripts/python.exe -m bench.research_materials_demo
.venv-dev/Scripts/python.exe dev.py --workspace research-materials-20261021-final --api-port 8887 --web-port 5287
```

最终库已存在，复看只执行第二条。地址/token 见本机 `_scratch/materials-dev-final.log`，不把 token 写入记录。早期非 final 工作区仅保留为迭代样例，交付复看使用 final。合成地址均为 `.example.test`，敏感串为固定假样例。

已实际操作：

1. 打开既有笔记，23 次修订按 20/3 翻到末页；原始文本和研究证据 #1 可查。
2. 最终库两个独立页同读 version 23；A 保存至 24，B 旧版提交显示 `E_STALE_RESEARCH`，草稿保留且保存禁用。B 显式重载后归档至 25，归档筛选实际返回 1 条。
3. 从证据 #1 创建“最终库：浏览器证据待办”，保存后仍关联原证据，并可查看同证据范围的任务。
4. 主机人工名称保存为“最终库核心门户”、审核为 needs_review；原始 `portal.example.test` 身份和原观测保留。早期库另实际验证人工名称搜索结果与分组均为 1。
5. 通过真实文件选择器选 `bench/e2e/research-materials/local.js`，填写资源 URL，再暂存/导入。原生解析器 `js-manifest@3`，作业 #8、34 条记录、0 坏行；JS 内容页显示 7 条记录、2 个调用候选、1 个不确定调用。绝对拼接形成 GET 候选，相对 POST 保留待确认，敏感串仅显示掩码。API 的 JS 交叉来源可以回到同一资源/hash。
6. 最终视口实际为 1000px，页面实际宽度 1000px；前台嵌入页的实际视口只有 467px，未将它冒充为 1000px 验收结果。后续截图使用受控后台真实页。
7. 受管证据 #10 预览中实际看到原始 `local.js` 名称、正文哈希、JSON 内容路径和有效贡献链；敏感串已掩码。JS → API → JS 双向回查均实际完成。
8. 新增量页在 JS 导入后显示新增 6 / 变化 0 / 有效贡献 66。通过正式静态页面撤销 job #8，增量变成 0/0/0；复原后恢复 6/0/66。切换 G 空项目为 0/0/0，无跨项目结果。
9. 直接打开后端 `8887/web/` 的正式资源，实际用统一搜索查询“最终库核心门户”，资产组命中 1；增量页仍返回 6/0/66。正式页实测 **1440×1000**，文档宽度 1440；窄页实测 **1000×850**，宽度 1000。最终读取的控制台 error 列表为空。
10. 为使冲突提示和草稿同时进入验收截图，再用最终库的归档笔记重复两轮版本冲突；当前库该笔记为 **version 27 / 已归档**。最终冲突截图来自旧窗口 version 26，提交被拒绝且草稿保留；没有将截图中的旧版本冒充库中最新值。

实际截图：[修订历史](research-materials/note-history.jpg)、[版本冲突](research-materials/note-conflict.jpg)、[归档筛选](research-materials/note-archive.jpg)、[证据待办](research-materials/evidence-task.jpg)、[人工字段](research-materials/manual-values.jpg)、[JS 候选](research-materials/js-candidates.jpg)、[增量](research-materials/changes.jpg)、[撤销后增量](research-materials/changes-undo.jpg)、[空项目](research-materials/empty-changes.jpg)、[正式页搜索](research-materials/production-search.jpg)、[正式页桌面增量](research-materials/production-changes.jpg)。源码/文档空白检查通过；本地提交后再核对正式资源与提交一致性。测试库、运行日志和完整浏览器结果保存在 `.magpie-dev/`、`_scratch/`，不纳入提交。
