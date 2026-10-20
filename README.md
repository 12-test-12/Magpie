# Magpie · 喜鹊

自用的**资源统筹工具** —— SRC / 渗透测试场景的资产与情报整合工作台。

把散落在各处的工具产物（扫描结果、抓包流量、接口定义、URL 列表、目录清单……）收进一个本地数据库，解析成统一的**实体与关系**，支持增量导入、整体撤销 / 重做、风险与攻击面概览。同一份数据同时有四个入口：**GUI 画布、命令行、HTTP API、MCP 适配器**。

> **仅用于自有资产与已获授权的目标**，不用于未授权扫描。
> 工具本身**不主动发起任何网络请求** —— 唯一例外是默认关闭的 `magpie ask`（显式开启才出站，出站前先做敏感信息掩码）。

当前版本 **0.30.0**。

0.30.0 在 API 工作区增加精确参数名/位置、请求样本状态码、MIME、凭据线索和来源性质筛选；组合条件必须在同一份样本成立。请求样本页可跨分页选择两份脱敏摘要对照并回查证据。仅 JS 来源与契约声明不冒充已发送请求，未命中不代表目标不存在该特征。实施边界见[API 研究筛选方案](docs/api-research-filters-plan.md)。

研究工作台提供服务端分组与分页、证据回查、Endpoint 精确身份、有限 JS 静态分析、Finding 研判/人工复测与报告导出。Ctrl+K / ⌘K 可统一检索当前项目的资产、接口、发现、证据文件名和笔记；研究任务支持四种人工状态与实体关联，主机/接口支持标签和 Tier 1–3 重要度。
本地运行、真实验收与保留边界见 [主线二实施与验收记录](Magpie-主线二实施与验收记录.md)。

0.29.0 收紧人工结论写入：新发现默认待核实，关键判定必须填写理由；研判与复测提交必须携带读取时的 `expected_version`，旧版本返回 409，需重新载入后检查。报告区分证据引用和仅有人工说明，历史依据缺口不会被自动补造。修改边界见[研究工作层写入收口方案](docs/research-write-safety-plan.md)。

---

## 它能干什么

### 1. 把乱七八糟的工具产物收进来

- **14 种解析器**：nmap、httpx、nuclei、ffuf、dirsearch、katana、gau、har、curl、fingerprint、openapi、postman、domainlist、tshark（pcap 抓包）。
- 嗅探仲裁**自动选型**，不用告诉它"这是什么文件"；仲裁拿不准的**不猜**，进 Inbox 待人工裁决（退出码 3）。
- 每个文件一个**作业**：增量导入、干跑预览（`--dry-run` 报告与真跑同构、事实一行不写）。

### 2. 统一的实体与关系模型

- 分层数据层：事实层 append-only；`entities / observations / relations` 是 `recompute` 可全量重建的缓存层；身份层（别名 / merge）、工作层、审计层，全在一份 SQLite schema 里。
- 缓存坏了不是灾难，是可验证的中间态。

### 3. 整体撤销 / 重做

- undo / redo 粒度到**文件**；支持 rekey、merge / unmerge、批量撤销。
- 事实层只追加 retract 抑制，不删贡献、不改贡献；undo / redo 严格互逆。

### 4. 看数据：四个入口一份数据

| 入口 | 适合谁 | 说明 |
|------|--------|------|
| **GUI 画布** | 人 | **探索模式**（只读实体关系画布：拖拽 / 折叠 / 筛选 / 标注）+ **编排模式**（可编辑管线：增删步骤、连依赖、批量跑、整体撤销）；另有导入对话框、镜头页、实体详情、作业历史、Inbox、数据体检、风险页 |
| **命令行** | 脚本 / 快查 | 只读 11 条（`ls` `lens` `show` `search` `diff` `doctor` `risk` `schema` `timeline` `export` `init`）+ 写 4 条（`project` `import` `undo` `redo`）+ `jobs` / `ask`；`--json` 信封是默认输出，退出码 0–5 与 HTTP 状态一一对应 |
| **HTTP API** | 集成 | FastAPI + SSE 进度事件；只绑回环、随机端口、一次性 token、不开 CORS 通配；OpenAPI 快照测试钉住契约 |
| **MCP 适配器** | AI agent | stdio 只读工具面（`ls` `lens` `show` `diff` `doctor` `schema` `timeline` `jobs` `risk` `agent_context`），清单从 schema 契约派生，agent 接上即用 |

### 5. 风险与攻击面

- 可解释评分：`sum(因子贡献) == 总分` 是测试钉住的恒等式，每个因子带证据。
- 攻击面按主机汇总服务 / 端点 / 未处置漏洞；`magpie risk` 与 `GET /api/v1/risk` 同一段代码。

### 6. 时间线 / 导出

- `magpie timeline`：按时间语义排序的项目事件线。
- `magpie export`：打包导出，**默认脱敏**（`--full` 才带归档原文并给可见告警）。

### 7. LLM 问答（默认关闭）

- `magpie ask` 是全工具**唯一出站点**；`MAGPIE_ASK_ENABLE=1` + `MAGPIE_ASK_URL` 显式配置才出站，发请求前打横幅告警，载荷先过 detect_secrets 掩码。

---

## 怎么启动

### 第 0 步：环境

- **Python ≥ 3.11**；开发与验证一律用 `py -3.13`（本机 PATH 上的 `python` 是另一个版本，别混用）。
- 只用 CLI / MCP？**不需要任何第三方依赖**（core 红线 R6：纯标准库）。
- 起 GUI / API 才需要 extras `api`；跑测试台架再加 `dev`。

### 第 1 步：安装

```bash
py -3.13 -m pip install -e ".[api]"        # 起 GUI / API
py -3.13 -m pip install -e ".[api,dev]"    # 外加测试台架（pytest）
py -3.13 -m pip install -e .               # 只用 CLI / MCP（零第三方依赖）
```

装完后 `magpie` 命令在 PATH 上；不想装也行，`py -3.13 -m magpie.cli …` 完全等价。

### 第 2 步：初始化（一次性）

```bash
magpie init                                 # 建库 / 升级 schema → .magpie/magpie.db（只建 schema，不建项目）
magpie project create 靶场 --include '*.example.com'   # 建项目（scope 决定"什么算在范围里"）
```

### 第 3 步：导数据、看结果

```bash
magpie import ./raw/nmap.xml                # 导文件或目录；解析器自动仲裁
magpie import ./raw --dry-run               # 先干跑看报告：事实一行不写
magpie ls --human                           # 看实体（默认 JSON 信封，--human 转表格）
magpie jobs --human                         # 看作业历史（undo / redo 的凭据在这）
magpie undo <job>                           # 撤销某个文件的导入；magpie redo <job> 复原
```

### 第 4 步：起服务（GUI + HTTP API，同一份数据）

```bash
magpie app                                  # 默认 127.0.0.1 随机端口，打印带 token 的 URL 并自动开浏览器
magpie app --port 8000 --token 固定token --no-browser   # 常用选项
```

- 启动后终端会打印 `URL`（含一次性 token）、`API` 前缀、`DB` 路径；Ctrl-C 停止。
- **只绑回环**：`--host 0.0.0.0` 会被直接拒绝 —— 对外暴露请走 Docker 的 `-p` 显式决定。
- GUI 源码在 `frontend/`，产物**已入库**，`magpie app` **不要求装 node**。

### MCP 接线（给 AI agent 用）

```bash
py -3.13 -m magpie.adapters.mcp --db D:/path/.magpie/magpie.db
```

Claude Desktop 等客户端的 `mcpServers` 示例见该命令的 `--help`；只读，写操作不在其射程。接线建议用绝对路径（`--db` 相对路径按客户端 cwd 解析）。

### 不想装 Python？两条路

**Docker 部署**（数据落在卷里）：

```bash
docker compose up -d
docker compose logs -f magpie            # token 在日志里
# 浏览器打开 http://localhost:8000/?token=<token>
```

固定 token 用 `MAGPIE_TOKEN` 环境变量（不设则每次启动随机生成）。镜像里的产品仍**只绑回环** —— 容器内由 `socat` 转发，对外暴露与否由 `-p` 显式决定。

**分发包**：所有开发分支推送后自动运行 `Magpie CI`，检查代码、前端构建和真实浏览器流程。打包另用手动 `Magpie Delivery`：选取 main 上通过 CI 且完成本地浏览器验收的提交，生成 Windows / Linux 解压即用包及 SHA-256 清单；默认仅保存构建制品，可选择创建草稿 Release。不会自动更新 edge 或发布。首次启用及试跑步骤见[工程流水线使用说明](docs/ci-delivery.md)。

### 前端开发模式（改 GUI 才需要）

本地浏览器联调（无需 GitHub 打包）：安装 API 依赖及 `frontend` 的 npm 依赖后，
用可用的 Python ≥ 3.11 运行 `dev.py`。例如本机已建立的开发环境：

```powershell
.\.venv-dev\Scripts\python.exe dev.py
```

命令打印带随机 token 的本地浏览器地址，前端在 5173 端口热更新，通过同源代理访问
8765 端口的 API。测试数据单独保存到 `.magpie-dev/`；修改 Python 后重启此命令，
Ctrl+C 停止两项服务。正式项目仍按原启动方式运行。

工作包 G 合成验收可先运行 `.venv-dev/Scripts/python.exe -m bench.research_demo`（已有库时拒绝覆盖），
再启动 `.venv-dev/Scripts/python.exe dev.py --workspace research-workflow`。
端口已占用时可加 `--api-port 8876 --web-port 5274`，同源代理会同步调整。
待办与重要度属于人工工作记录，保留审计，不随导入撤销删除；任务关联的已撤销/合并对象会明确标记。
它们尚未接入通用作业 Undo/Redo，可通过编辑恢复状态或清除标记。标签沿用既有工作层与审计入口。

测试通用导入：新建测试项目 → 导入 → 选择或填入文件 → 勾选“陌生工具 / 自定义字段”
→ 预览 → 修改映射 → 再预览 → 确认映射 → 开始导入。样例见 `bench/e2e/universal/`。
通用处理目前限制 16 MiB，支持 JSON/JSONL/CSV/TSV/Text/XML；YAML、原始 HTTP
与压缩包不在通用解码范围内。已知工具继续使用原生解析器，模糊情况仍需人工判断。

```bash
cd frontend
# 需要 PATH 上有真实 node 目录（Windows 下 cmd 跑 postinstall 时尤其注意）
npm install
npm run dev              # Vite 开发服务器
npm run build            # 构建；产物入 magpie/api/web/，改完必须一并提交
```

### 一条命令验证

```bash
py -3.13 check.py        # 11 条静态红线 + 全量单测 + 各阶段设计验证脚本，一条命令出结论
```

---

## 技术架构

```
frontend (Vue 3 + Vite)  ──构建产物入库──▶  magpie/api/web/
        │                                        ▲
        │ REST / SSE                             │ 静态资源（零外部引用）
        ▼                                        │
magpie CLI（标准库）    magpie adapters/mcp（stdio JSON-RPC）
        └──────────────┬─────────────────────────┘
                       ▼
        magpie/api   FastAPI · 单写线程 · SSE 事件总线
                       │
                       ▼
        magpie/core   纯标准库（红线 R6）· 唯一写入口 · 事务 ops · 解析器
                       │
                       ▼
        SQLite        .magpie/magpie.db（schema/*.sql 版本化迁移）
```

| 模块 | 技术 | 职责 |
|------|------|------|
| `magpie/core/` | **仅标准库**（红线 R6） | 数据层、解析器、导入管线、事务操作、风险评分、导出 |
| `magpie/api/` | FastAPI · uvicorn（extras: `api`） | 本地 HTTP 接口 + SSE + GUI 静态资源 |
| `magpie/cli/` | 标准库 argparse | 命令行入口，默认直连 core，**不需要服务在跑** |
| `magpie/adapters/` | 标准库 | MCP stdio 只读适配器（零第三方依赖） |
| `frontend/` | Vue 3 · Vite | GUI 源码；构建产物**入库**，`magpie app` 不要求装 node |
| `schema/` | SQL | 全部 DDL + 版本化迁移 runner |
| `check.py` | — | 静态红线 + 测试 + 验证脚本的唯一入口 |

依赖边界：`pyproject.toml` 的 `dependencies` **必须保持为空**；第三方一律进 extras（`api` / `api-yaml` / `traffic` / `dev`），且只在 parser / api 内部 lazy import。

---

## 仓库结构

```
magpie/core/            数据层与业务逻辑（store / ops / ingest / risk / export / parsers…）
magpie/core/tests/      不变量与回归测试
magpie/api/             HTTP API + SSE + GUI 构建产物（web/，入库，别手改）
magpie/cli/             命令行（read.py / write.py；envelope.py 是对外契约）
magpie/adapters/mcp.py  MCP 只读适配器
frontend/               GUI 源码（改完 npm run build 并提交产物）
schema/                 001_init.sql 起的版本化迁移
bench/                  性能契约（MAGPIE_PERF_STRICT=1）
packaging/              PyInstaller 打包与解压冒烟
docker/ · Dockerfile · docker-compose.yml
check.py · verify_g*.py 验证入口与各阶段出口 Gate
Magpie-*.md · v3.2-design.md · CHANGELOG.md   设计与计划文档
```

---

## 核心设计要点

**两条红线，`check.py` 自动守，不靠自觉**

- **R6**：`magpie/core` 只用标准库 —— 第三方依赖一律进 extras，绝不出现在 core 顶层。
- **R7**：零出站 —— 全仓（含前端静态资源）不发起任何网络请求；白名单**恰好一个文件** `magpie/cli/ask.py`，且 ask 默认关闭，`MAGPIE_ASK_ENABLE=1` + `MAGPIE_ASK_URL` 显式配置才出站。

**数据可信**

- 事实层 append-only，一切缓存都可 `recompute` 全量重建。
- 单写者：core 唯一写入口 + API 进程级单写线程；SSE 有界队列、丢帧可数。
- 导入按文件成作业、undo 粒度到文件；解析器仲裁拿不准的进 Inbox，人工裁决（退出码 3）。

**对外契约，改动必须显式**

- `/api/v1` 响应形状、`--json` 字段、退出码表是对外契约 —— 改它要升 minor 并写 `CHANGELOG.md`；OpenAPI 由快照测试钉住，契约漂移当场变红。

**安全姿态**

- 服务只绑 `127.0.0.1`（绑 `0.0.0.0` 会被**拒绝**）、随机端口、一次性 token、静态资源零外部引用。
- 导出默认脱敏、ask 出站前掩码 —— 明文永不外传，与"明文永不落库"是同一道防线。

---

## 声明

- 仅用于自有资产与已获授权的目标；工具本身不主动发起网络请求（唯一例外是默认关闭的 `magpie ask`）。
- 自用工具，处于持续迭代中；对外契约（API 形状 / JSON 字段 / 退出码）受 CHANGELOG 纪律约束，其余接口与功能仍可能调整。
- 与任何商业产品无官方从属关系。
