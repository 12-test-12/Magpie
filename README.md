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
magpie/api/web/            静态页（本地打包，零出站）
magpie/cli.py              命令行入口（`magpie app` 可用；其余见 2.2 / 2.6）
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

GUI（2.8）与 CLI 的读写命令（2.2 / 2.6）尚未开工 —— 见 `Magpie-项目计划书.md`
的路线图。

## 一条命令验证

```bash
py -3.13 check.py          # 静态红线 + 单元测试 + 两个设计验证脚本
py -3.13 check.py lint     # 只跑静态红线（秒级，可单独触发）
py -3.13 check.py bench    # 严格性能契约（单独触发，不埋进"跑全部"）
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
