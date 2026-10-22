# 2026-10-22：前端 CI 构建重现修复

## 修改前方案

用户报告最近两个提交 `32eb965`、`8e2feaa` 的 Frontend / Required Checks 失败。已读取两份主线验收记录、工程流水线方案与研究资料完善验收记录。GitHub PR run `39618869064` 的 Frontend job `119286635700` 显示：191 项测试与 Vite 构建成功，只有 `contracts.py --web-only` 失败；Browser 因依赖 Frontend 跳过，汇总因此失败。

本机 `git ls-files --eol frontend` 显示部分文件为 `i/lf w/crlf attr/text eol=lf`。已有行尾规则不追溯改写工作文件；本机重建可复现已提交的旧产物，却不能证明干净检出可复现。

修复限定为：将本地前端文本构建输入恢复 LF；增加构建前的行尾检查与明确路径诊断；覆盖 LF、CRLF 和混合行尾回归；按 LF 重建正式资源；让产物门禁输出差异路径。保持原门禁、Browser 依赖和 Required Checks 规则，业务版本仍为 0.31.0 / schema v20。原始事实、旧迁移与未跟踪 Agent 方案不改，不推送、不合并、不发布，不重写最近两个提交。

验证：对两个历史提交的干净 LF 输入分别重建，核对差异；前端全量测试、正式构建、工程检查与静态红线；通过 `dev.py` 新建隔离合成库并验收开发/正式页面和 1000px 布局；本地提交后再次确认产物一致。实际环境及未验证边界在完成后补充。

## 根因与修复

[GitHub 失败运行](https://github.com/12-test-12/Magpie/actions/runs/39618869064) 在 Ubuntu 使用 Node 22.19.0 / Vite 6.4.3，构建生成 `index-DP7uI_zM.js` 与 `index-BaDSPzQu.css`，仓库却保存 `index-B2CN1MLQ.js` 与 `index-C4AvMQQf.css`。本机旧工作文件可重现仓库产物；仅将实际 CRLF 文件保存为 LF 后，立即生成与远程日志相同的文件。

本地安装的 `@vitejs/plugin-vue` 在 production 下以 `normalizedPath + source` 生成组件 ID。源码行尾改变 ID，再改变 scoped CSS、JS 和 HTML 的内容/引用。Git 的行尾归一化让这类工作文件显示为“无修改”，并不能保证它们与干净检出的构建输入逐字节一致。

使用 `git archive` 将两个历史提交的 `frontend/` 分别提取到全新目录，复用现有锁定依赖构建（没有改历史提交，也没有重装依赖）：

| 提交 | 原提交产物 | 干净 LF 重建产物 |
|---|---|---|
| `32eb965` | `index-B4Awv--Z.js` / `index-HbVUDm_d.css` | `index-Bd4U7iA6.js` / `index-BKLnq7XH.css` |
| `8e2feaa` | `index-B2CN1MLQ.js` / `index-C4AvMQQf.css` | `index-DP7uI_zM.js` / `index-BaDSPzQu.css` |

修复后的 `npm run build` 先运行 `frontend/scripts/check-build-inputs.mjs`，递归检查文本源码及 HTML/Vite 配置，发现 CRLF、混合或孤立 CR 时列出文件并非零退出，不静默修改输入。新增四项测试覆盖正确输入、CRLF、混合行尾、多个受影响入口以及拒绝时不改文件。正式资源按 LF 重建；`contracts.py --web-only` 失败时列出 Git 差异路径，仍拒绝修改/删除/新增产物。工作流门禁和依赖没有放宽。

## 实际验证（2026-10-22）

环境：Windows、本机 Node **22.19.0**、Vite **6.4.3**、`.venv-dev` Python **3.12.4**、已有 Playwright **1.62.1** 与 Chrome。`py -0p` 未列出 Python 3.13，未声称本地验证该版本。

- `npm test -- --reporter=dot`：**30 文件 / 195 项通过**。
- `npm run build`：行尾预检和正式构建通过；生成上述与远程日志相同的 LF 产物。
- 工程事实、两份工作流结构、历史迁移与空白检查通过；`unittest discover -s scripts/ci/tests -v` **9 项通过**；`check.py lint` **11 条红线全部通过**。
- 通过 `scripts/ci/browser.py` 与 `dev.py` 新建 **ci-browser-1372f04d3c49** 隔离合成库，真实可见 Chrome（`headless=false`）验收通过：同请求组合筛选、分组计数、20/4 样本分页与跨页对照、有效贡献证据、禁止借用不同请求字段、空项目隔离、字面通配字符筛选、1000px 布局，以及后端正式静态页面。页面异常和外部 HTTP(S) 请求均为 **0**。
- 浏览器证据在 `_scratch/ci-browser/1372f04d3c49/`，元数据如实记录运行时 `source_sha=8e2feaa...`、`tracked_changes=true`，不将未提交修复冒充该旧提交的已验结果。历史重建证据位于 `_scratch/ci-lf-reproduction/result.json`。临时合成库/日志不纳入提交。
- 将待提交 Git index 中的前端输入与正式资源按 blob 原始字节导出到全新目录，复用已有锁定依赖后构建；HTML、CSS、JS 三个文件的路径集合与 SHA-256 **逐项完全一致**，结果位于 `_scratch/ci-clean-build-3e23350c43/result.json`。重建 JS/CSS 与旧产物比较，统一 Vue `data-v-*` 作用域 ID 后全文相同，未夹带业务变更。

本批未修改后端业务代码，按改动范围运行工程回归和静态红线，没有重复完整后端套件或严格性能/冻结包验收。未在本机模拟 Ubuntu；远程失败的旧记录不会因本地修复自动改变，后续须由用户推送新提交触发 GitHub CI。未推送、重跑远程工作流、合并或发布。
