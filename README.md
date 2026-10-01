# Magpie · 喜鹊

自用的资源统筹工具（SRC / 渗透测试场景的资产与情报整合工作台）。
**仅用于自有资产与已获授权的目标**，不用于未授权扫描；工具本身不主动发起任何网络请求。

## 现在有什么

数据层已经落地并且是可验证的：事实层（append-only 的 `contributions`）、缓存层（`recompute`
可全量重建的 `entities/observations/relations`）、身份层（`entity_alias` / merge 事件）、
工作层与审计层，都在 `schema/001_init.sql` 里；事务型操作（undo / redo / rekey / merge /
unmerge / compact）在 `magpie/core/ops.py`。

```
magpie/core/store.py       唯一写入口 + recompute + 不变量检查 + 快照等价
magpie/core/ops.py         事务型状态迁移 T1–T5（含 dry-run）
magpie/core/migrate.py     schema 版本化与迁移 runner
magpie/core/confidence.py  §8.2 反多数票置信度
magpie/core/manual.py      INV-12 唯一 manual 写入口（单写队列）
magpie/core/ingest.py      导入管线（文件 → 事实，含可选的 on_progress 观察者）
magpie/core/parsers/       解析器（domainlist / httpx）
magpie/core/tests/         不变量与回归测试（232 passed）
magpie/api/                本地 HTTP 接口（FastAPI；extras: api）
magpie/api/writer.py       进程级唯一写者（单写线程 + 写队列）
magpie/api/events.py       SSE 事件总线（有界队列 + 丢帧可数）
magpie/api/web/            GUI 构建产物（Vite 打包，零出站；**入库**，别手改）
frontend/                  GUI 源码（Vue 3 + Vite；改完要 npm run build）
magpie/cli/                命令行入口（2.2 的只读命令 + `magpie app`；写路径见 2.6）
schema/001_init.sql        全部 DDL + 触发器 + 视图
schema/002_*.sql           收窄 undo_incomplete（redo 的 blocked 状态合法化）
bench/test_perf*.py        性能契约（MAGPIE_PERF_STRICT=1）
```

`magpie/core/` **只用标准库**（红线 R6）；对外依赖一律进 `pyproject.toml` 的
extras（`api` / `api-yaml` / `traffic` / `dev`）。工具本身**不主动发起任何网络
请求**（红线 R7，2.7 起连同静态资源一起守）。两条红线都由 `python check.py`
自动守着，不靠自觉。

## 起服务（2.7）

```bash
py -3.13 -m pip install -e ".[api]"     # extras.api：fastapi + uvicorn
py -3.13 -m magpie.cli app             # 或 magpie app（若已装进 PATH）
# → 打印 http://127.0.0.1:<随机端口>/?token=<一次性 token>，浏览器打开即可
```

只绑 `127.0.0.1`（绑 `0.0.0.0` 会被**拒绝**）、随机端口、启动时一次性 token、
不开 CORS 通配、静态资源零外部引用。`GET /openapi.json` 是给 agent 的说明书，
它的内容被快照测试钉住（`magpie/api/tests/openapi_snapshot.json`）—— 契约变了
测试就红，逼着变更是有意的。

`/api/v1` 的响应形状与 `--json` 字段**是对外契约**：改它要升 minor 并写
`CHANGELOG.md`。

## GUI（2.8 只读画布 / 3.7b 编排模式）

打开服务打印的那条 URL 就是界面。**两个模式**，而它们的差别是一条不变量：

* **探索模式**（`view === 'explore'`，只读）：实体 = 节点、关系 = 边，可以拖
  位置 / 折叠 / 筛选 / 标注，**不能增删节点与边** —— 图的内容只能来自
  `GET /graph`。
* **编排模式**（`view === 'pipeline'`，可编辑）：画布上画的是一条**管线定义**
  （步骤 = 节点、`after` 依赖 = 边）。可以增删步骤、连依赖、存下来、换路径
  再跑、一次跑一批，以及把一次批量跑整体撤销。**"可编辑"只意味着一条边**：
  增删步骤是表单动作，画布手势只表达依赖。

顶部：项目切换 + 导入（=Run，走 `POST /imports` 并用 SSE 显示进度/阶段/坏行）
+ `Ctrl+K` + 体检 + Inbox 徽标。左栏镜头与类型面板 + 管线列表、中间画布、右栏
实体详情、底部日志抽屉。还有镜头页（资产 / 服务与中间件）、作业历史（含
undo / redo **干跑预览**与 `explain_job_scope`）、Inbox 页、数据体检页。

CLI 的写路径（2.6）后置之后，这三个入口从 CLI 移到了界面上 —— 没有它们，
计划书 G2 出口 Gate 的「导入 → undo → redo 精确复原」就无法验收。

改前端：

```bash
cd frontend && npm install && npm run build    # 产物落进 magpie/api/web/
py -3.13 -m pytest magpie/api/tests/test_web.py -q
```

产物**入库**是刻意的：`magpie app` 不要求先装 node。前端的两条硬纪律
（零出站、探索模式只读）分别由 `check.py lint` 的 R7-ASSETS 与
READONLY + READONLY-REVERSE 两条静态红线守着，见 `frontend/README.md`。

## 命令行（2.2 只读骨架）

```bash
py -3.13 -m magpie.cli init  --db .magpie/magpie.db   # 建库（只建 schema，不建项目）
py -3.13 -m magpie.cli ls    --json --limit 20        # 实体列表（键集游标分页）
py -3.13 -m magpie.cli show  api.example.com --json   # 实体详情（id 或规范 key）
py -3.13 -m magpie.cli diff  --since last --json      # 自上次导入起，库里多了什么
py -3.13 -m magpie.cli doctor --json                  # 数据体检
py -3.13 -m magpie.cli schema --json                  # 类型 / 关系动词 / 字段 / 查询语法
py -3.13 -m magpie.cli risk --human                   # 风险评分 + 攻击面概览（零出站）
py -3.13 -m magpie.cli ask "这台主机开了哪些端口？"      # 出站问答 —— 默认关闭，
                                                       # 需 MAGPIE_ASK_ENABLE=1 +
                                                       # MAGPIE_ASK_URL 显式开启
```

默认**直连 core**，不需要服务在跑。`--json` 是默认输出（`--human` 才切人读表格），
信封与 `/api/v1` 逐字同形；`magpie schema --json` 一条命令给出全部命令的语法，
agent 不必读源码。

退出码表 **0/1/2/3/4/5**（成功 / 用法错 / 数据错 / 需人工裁决 / 锁冲突 / 版本不匹配）
与 2.7 的 HTTP 状态码 **2xx/400/404/409/423/426 一一对应**。它是**对外契约**：
改它要升 minor 并写 `CHANGELOG.md`。

**本步只做读**。`search` 与 `import` / `undo` / `redo` / `jobs` 会以
`E_NOT_IMPLEMENTED` **响亮失败**（分别是 §8 与 2.6 的地盘）—— 不返回空结果，
因为空结果对 agent 是一个确定的断言，而事实是"这条命令还不存在"。
写路径见 2.6，整体排期见 `Magpie-项目计划书.md`。

## MCP 适配器（5.1 · 只读）

让 agent 经 Model Context Protocol 只读地使用这份数据：**stdio 传输**
（本地子进程、换行分帧的 JSON-RPC 2.0），零第三方依赖，不发起任何网络请求
（HTTP / SSE 传输刻意不实现 —— 红线 R7）。

```bash
py -3.13 -m magpie.adapters.mcp --db D:/path/to/.magpie/magpie.db
```

MCP 客户端（Claude Desktop 等）的接线示例（`--help` 里也有）：

```json
{
  "mcpServers": {
    "magpie": {
      "command": "py",
      "args": ["-3.13", "-m", "magpie.adapters.mcp",
               "--db", "D:/path/to/.magpie/magpie.db"]
    }
  }
}
```

工具面（`magpie/adapters/mcp.py` 的 `READONLY_TOOLS`）：`ls` / `lens` / `show` /
`diff` / `doctor` / `schema` / `timeline` / `jobs` / `risk` / `agent_context` ——
**全部只读**。清单不是手抄的：从 `magpie schema --json` 的 `commands` 键派生
（`access == "read"` 的一半，明文剔除 `export` / `search` / `ask` —— 最后这个是
出站命令，MCP 只读、不进面），描述 / 参数枚举 /
版本分别来自契约的 `query_syntax` / `enums` / `tool_version`。导入、undo/redo、
合并、裁决 Inbox、管线、导出落盘等写操作**不在工具面**（G5 之后另议）。
`tools/call` 的结果与 `--json` / `/api/v1` 逐字同形（成功带 `meta`、失败不带，
分叉按 `error.code`）。自动化断言在 `magpie/adapters/tests/`：逐工具跑完
整库指纹不变（带反例）、实际 ≡ 声明的双向清单、写入口 AST 静态断言。

## 一条命令验证

```bash
py -3.13 check.py          # 静态红线 + 单元测试 + 两个设计验证脚本
py -3.13 check.py lint     # 只跑静态红线（秒级，可单独触发）
py -3.13 check.py bench    # 严格性能契约（单独触发，不埋进"跑全部"）
cd frontend && npm test    # 前端单测（聚合 / 布局 / 只读断言）—— 需要 node
```

**一律用 `py -3.13` 显式调用，不要依赖 PATH 顺序。** 工程解释器是 **3.13**，
而本机 `python` 是 3.9.1 —— 两者不是一个东西，已运行的进程还可能继承改动前的
PATH。`check.py` 会自己拦这件事：解释器低于 `pyproject.toml` 声明的
`requires-python` 下限时**拒绝执行**，而不是给一个无意义的绿。

`check.py` 的子进程一律用 `sys.executable`，所以你用哪个解释器跑它，测试就跑在
哪个解释器上 —— 这也是所有"已在 3.13 上验证"结论成立的前提。

## 文档

| 文件 | 作用 |
|---|---|
| `Magpie-项目计划书.md` | 产品蓝图与路线图（**排期以它为准**） |
| `v3.2-design.md` | 数据层设计冻结 + 修正记录（r2/r3/r4） |
| `Magpie-搭建思路.md` | 逐阶段的施工顺序与注意事项 |
| `Magpie-数据模型修订稿-v3.1.md` | 历史稿，已被 v3.2 取代，仅作参照 |

> 纪律：**排期前先跑一遍，别信文档。**（v3.2-design.md 曾同时写错三件事 ——
> redo 未实现、性能未验证、测试数 62 —— 而代码里早已不是那样。）
