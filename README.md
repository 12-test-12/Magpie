# Magpie · 喜鹊

自用的**资源统筹工具** —— SRC / 渗透测试场景的资产与情报整合工作台。

把散落在各处的工具产物（扫描结果、抓包流量、接口定义、URL 列表、目录清单……）收进一个本地数据库，解析成统一的**实体与关系**，支持增量导入、整体撤销 / 重做、风险与攻击面概览。同一份数据同时有四个入口：**GUI 画布、命令行、HTTP API、MCP 适配器**。

> **仅用于自有资产与已获授权的目标**，不用于未授权扫描。
> 工具本身**不主动发起任何网络请求** —— 唯一例外是默认关闭的 `magpie ask`（显式开启才出站，出站前先做敏感信息掩码）。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 分层数据层 | 事实层 append-only；`entities / observations / relations` 是 `recompute` 可全量重建的缓存层；身份层（别名 / merge）、工作层、审计层，全部在一份 SQLite schema 里 |
| 解析导入 | 14 种解析器：nmap、httpx、nuclei、ffuf、dirsearch、katana、gau、har、curl、fingerprint、openapi、postman、domainlist、tshark（pcap 抓包）。嗅探仲裁自动选型；仲裁不出结果的文件**不猜**，进 Inbox 待人工裁决 |
| 撤销 / 重做 | 每个文件一个作业，undo / redo 粒度到文件；支持干跑预览（`--dry-run` 报告与真跑同构、事实一行不写）、rekey、merge / unmerge、批量撤销 |
| GUI 双模式 | **探索模式**（只读实体关系画布：拖拽 / 折叠 / 筛选 / 标注）+ **编排模式**（可编辑管线定义：增删步骤、连依赖、存取复用、批量跑、整体撤销）；另有导入对话框（服务端目录点选）、镜头页、实体详情、作业历史（含 undo / redo 干跑预览）、Inbox、数据体检、风险页 |
| 命令行 | 只读 10 条（`ls` `lens` `show` `diff` `doctor` `schema` `timeline` `export` `risk` `init`）+ 写 4 条（`project` `import` `undo` `redo`）；`--json` 信封是默认输出，退出码表 0–5 与 HTTP 状态一一对应 |
| HTTP API | FastAPI + SSE 进度事件；只绑回环、随机端口、一次性 token、不开 CORS 通配；OpenAPI 快照测试钉住契约，改了形状测试当场红 |
| MCP 适配器 | stdio 只读工具面（`ls` `lens` `show` `diff` `doctor` `schema` `timeline` `jobs` `risk` `agent_context`），清单从 schema 契约派生，agent 接上即用 |
| 风险评分 | 可解释评分：`sum(因子贡献) == 总分` 是测试钉住的恒等式，每个因子带证据；攻击面按主机汇总服务 / 端点 / 未处置漏洞 |
| 时间线 / 导出 | `magpie timeline` 按时间语义排序的项目事件线；`magpie export` 打包导出，**默认脱敏**（`--full` 才带归档原文并给可见告警） |
| LLM 问答 | `magpie ask` —— 全工具**唯一出站点，默认关闭**；开启后发请求前打横幅告警，载荷先过 detect_secrets 掩码 |
| 一条命令验证 | `py -3.13 check.py`：11 条静态红线 + 全量单测 + 各阶段设计验证脚本，一条命令出结论 |

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
Magpie-*.md · v3.2-design.md · CHANGELOG.md   设计与计划文档（见文末索引）
```

---

## 快速开始

### 安装

```bash
py -3.13 -m pip install -e ".[api]"        # 起 GUI / API
py -3.13 -m pip install -e ".[api,dev]"    # 外加测试台架（pytest）
```


### Docker 部署

别人拿去就能跑（不需要 Python / node，数据落在卷里）：

```bash
docker compose up -d
docker compose logs -f magpie            # token 在日志里
# 浏览器打开 http://localhost:8000/?token=<token>
```

固定 token 用 `MAGPIE_TOKEN` 环境变量（不设则每次启动随机生成）。镜像里的产品仍**只绑回环** —— 容器内由 `socat` 转发，对外暴露与否由 `-p` 显式决定。

### 分发包

推 `main` 触发 GitHub Actions，Release 页的滚动 prerelease **`edge`** 永远是最新一份双平台（Windows / Linux）PyInstaller 包，解压即用、不装 Python 也能跑；`v*` tag 挂正式 Release。

---

## 核心设计要点

**两条红线，`check.py` 自动守，不靠自觉**

- **R6**：`magpie/core` 只用标准库 —— 第三方依赖一律进 extras，绝不出现在 core 顶层。
- **R7**：零出站 —— 全仓（含前端静态资源）不发起任何网络请求；白名单**恰好一个文件** `magpie/cli/ask.py`，且 ask 默认关闭，`MAGPIE_ASK_ENABLE=1` + `MAGPIE_ASK_URL` 显式配置才出站。

**数据可信**

- 事实层 append-only，一切缓存都可 `recompute` 全量重建 —— 缓存坏了不是灾难，是可验证的中间态。
- 单写者：core 唯一写入口 + API 进程级单写线程；SSE 有界队列、丢帧可数。
- 导入按文件成作业、undo 粒度到文件；解析器仲裁拿不准的进 Inbox，人工裁决（退出码 3）。

**对外契约，改动必须显式**

- `/api/v1` 响应形状、`--json` 字段、退出码表是对外契约 —— 改它要升 minor 并写 `CHANGELOG.md`；OpenAPI 由快照测试钉住，契约漂移当场变红。

**安全姿态**

- 服务只绑 `127.0.0.1`（绑 `0.0.0.0` 会被**拒绝**）、随机端口、一次性 token、静态资源零外部引用。
- 导出默认脱敏、ask 出站前掩码 —— 明文永不外传，与"明文永不落库"是同一道防线。

---


## 文档索引

| 文件 | 作用 |
|------|------|
| `Magpie-项目计划书.md` | 产品蓝图与路线图（**排期以它为准**） |
| `v3.2-design.md` | 数据层设计冻结 + 修正记录（r2/r3/r4） |
| `Magpie-搭建思路.md` | 逐阶段的施工顺序与注意事项 |
| `Magpie-编排模式设计冻结.md` | 编排模式（管线定义）的设计 |
| `CHANGELOG.md` | 对外契约变更史（当前 0.16.0） |
| `frontend/README.md` | 前端两条硬纪律（零出站、探索模式只读） |

> 纪律：**排期前先跑一遍，别信文档。**

---

## 声明

- 仅用于自有资产与已获授权的目标；工具本身不主动发起网络请求（唯一例外是默认关闭的 `magpie ask`）。
- 自用工具，处于持续迭代中；对外契约（API 形状 / JSON 字段 / 退出码）受 CHANGELOG 纪律约束，其余接口与功能仍可能调整。
- 与任何商业产品无官方从属关系。
