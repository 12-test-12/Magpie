# Magpie 工程流水线实施与本地验收

后续修复：2026-10-22 对 `32eb965` / `8e2feaa` 的 Frontend 产物不一致完成根因复现、LF 输入预检与资源重建，见[专门记录](ci-frontend-reproducibility-fix.md)。本节以下仍保留 2026-10-20 的历史口径。

日期：2026-10-20。实施分支：`codex/ci-delivery-pipeline`；基线 `10648bc1ce0e33a36341c30290cbbb289e089029`。先完成[方案](ci-delivery-plan.md)，再修改工程文件。应用保持 **0.30.0 / schema v19**，不修改业务源码、解析器、原始证据、Contribution、Undo/Redo 或既有迁移；应用依赖和前端锁文件保持不变。

## 已实现

- `Magpie CI`：所有分支 push、main PR、merge queue 和手动触发。Contracts、Linux/Windows Python 3.13 与 Linux Python 3.11 后端、Node 22.19.0 前端、隔离真实浏览器、统一 Required Checks。必要任务失败、跳过或取消均不能通过汇总。
- `Magpie Delivery`：只手动执行。校验成功 main CI、同仓库/完整 SHA/明确本地浏览器声明/源提交内 Markdown 记录；复用既有 spec 和解压后 smoke。双平台版本/SHA/大小/哈希清单，汇总校验后默认保存制品，可选择新标签及草稿，发布前再次校验。保留所有已有标签、edge 和 Release。
- 权限默认只读，Actions 固定完整 SHA，checkout 不保留凭据；仅可选草稿 job 使用写权限。新增 PR 模板、真实 CODEOWNERS 和服务端设置指南，未实际修改 GitHub 设置。
- 浏览器工具在 CI 临时目录安装，本地使用已有 Playwright/Chrome。唯一合成库、随机高位端口、原开发服务不受影响、含 token 的日志与上传证据分开。已修复地址打印早于监听就绪造成的偶发失败：只对连接启动错误进行有限等待，HTTP 错误仍失败。

## 本地实际验证

环境为 Windows、`.venv-dev` **Python 3.12.4**、Node **22.19.0**、已有 Chrome 和 Playwright **1.62.1**。本机没有 Python 3.13，不冒称完成首选解释器验收。

| 检查 | 真实结果 |
|---|---|
| 工程事实与 YAML 结构 | 版本 0.30.0、19 个连续迁移、OpenAPI 版本、固定 Actions SHA、只读默认权限、完整基线差异检查通过 |
| 新增交付回归 | `python -m unittest discover -s scripts/ci/tests -v`：**9 项通过**；覆盖正确来源、失败/跳过/fork/PR、错误 SHA/分支、人工声明、路径越界、真实合成 ZIP/tar、篡改、缺平台、额外内容、拒绝覆盖 |
| 前端 | `npm test`：**28 文件 / 185 项通过**；`npm run build` 成功；`contracts.py --web-only` 确认正式资源无跟踪差异或未跟踪输出 |
| 浏览器 | 最终脚本实际通过，`headless=false`；独立工作区 `ci-browser-122aa22d310f`，记录见下文 |
| 完整后端及设计验收 | 正常本地权限的最终 `check.py` **退出 0**；11 条红线、core **708**、API **170**、CLI **84**、adapters **21**，后端合计 **983 项**；六组设计验收 **23/23、27/27、71/71、26/26、35/35、45/45** |
| 其他 | Node 浏览器脚本语法及 Git 源文件空白检查通过 |

最终浏览器记录位于本机 `_scratch/ci-browser/122aa22d310f/`，包括 `result.json`、`environment.json`、脱敏日志及组合条件/样本对照/1000px 截图。运行时基线为上述 SHA、`tracked_changes=true`，如实表示工程文件尚未提交，不冒称最终提交已运行。结果包括：

1. 同请求组合筛选、分组与下钻计数为 1，24 个样本按 20/4 分页，跨页选择两份对照并打开有效贡献链。
2. 200 + Bearer + redirect 为 0，未拼借另一份请求字段。
3. 空项目无跨项目结果；`key_%` 精确命中 1，1000px 页面无横向溢出。
4. 后端提供的正式静态页面实际执行筛选，命中 1；捕获页面异常与外部 HTTP(S) 请求均为 0。

过程日志：`_scratch/ci-delivery-frontend-final.log`、`_scratch/ci-delivery-build-final.log`、`_scratch/ci-browser-confirmed-final.log`、`_scratch/ci-delivery-check-confirmed.log`。这些均是本机临时证据，不纳入 Git。合成包单元测试使用的占位文件不是可交付冻结包。

## 失败过程与未验证边界

- 一次早期完整检查期间工作区被切到 main，旧分支的通过结果不作为本次最终结论；已回到独立工程分支重跑。另一次受限运行因 Windows `pytest-of-patter` 临时目录访问被拒而失败，不是绿色结果；随后以获准的正常本地权限复验，结果单独记录。
- 浏览器首次通过后，最终复验暴露 Vite 启动竞态；修复后使用新合成库重新完整通过。失败记录保留，不以部分步骤充当全部通过。
- 本地安装浏览器工具及检查 Docker 的授权未获准，因此未安装新软件、未启动 Docker。浏览器改用已有工具；**本地未执行 actionlint**，仅执行 YAML/策略结构检查，不能称作 Actions 语法已完整验证。CI 配置 actionlint 1.7.12，等待首次 GitHub 运行。
- 本地没有执行 Python 3.13/3.11、Linux、CI 覆盖率插件或 PyInstaller 冻结包测试；这些是远程首次运行/后续手动交付的待验项。覆盖率无实测基线，不宣称达到任意百分比。
- 本次没有新性能测量，也没有新业务功能。CHANGELOG 按其只记录对外契约的约定不增加版本条目。
- 未推送、合并、创建远程 PR、运行 GitHub 工作流、创建标签/Release 或更改分支保护/环境配置；未跟踪的 Agent 方案保持原状。

## GitHub 首次实验

用户推送工程分支后自动运行验证；查看 Actions → Magpie CI，按 Contracts / Backend / Frontend / Browser 日志排查，以 Required Checks 为最终结果。仅为验证这批配置不必合并 main。

Delivery 必须先由操作者将工作流纳入默认分支才能显示手动入口。后续实际打包还须选择成功 main CI 的完整 SHA，确认对应本地浏览器记录；默认仅下载 artifacts，可选草稿也不会自动发布。人工声明和文件存在校验不证明验收内容真实。分支规则及 release 环境审批均需 GitHub 服务端另行启用，详见[使用说明](ci-delivery.md)。
