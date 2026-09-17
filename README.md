# Magpie · 喜鹊

自用的资源统筹工具（SRC / 渗透测试场景的资产与情报整合工作台）。
**仅用于自有资产与已获授权的目标**，不用于未授权扫描；工具本身不主动发起任何网络请求。

## 现在有什么

数据层已经落地并且是可验证的：事实层（append-only 的 `contributions`）、缓存层（`recompute`
可全量重建的 `entities/observations/relations`）、身份层（`entity_alias` / merge 事件）、
工作层与审计层，都在 `schema/001_init.sql` 里；事务型操作（undo / redo / rekey / merge /
unmerge / compact）在 `core/ops.py`。

```
core/store.py       唯一写入口 + recompute + 不变量检查 + 快照等价
core/ops.py         事务型状态迁移 T1–T5（含 dry-run）
core/migrate.py     schema 版本化与迁移 runner
core/confidence.py  §8.2 反多数票置信度
core/manual.py      INV-12 唯一 manual 写入口（单写队列）
schema/001_init.sql 全部 DDL + 触发器 + 视图
core/tests/         不变量测试（79 passed）
bench/test_perf.py  性能契约（MAGPIE_PERF_STRICT=1）
```

CLI / API / GUI / 解析器尚未开工 —— 见 `Magpie-项目计划书.md` 的路线图。

## 一条命令验证

```bash
python check.py          # 单元测试 + 两个设计验证脚本
python check.py bench    # 严格性能契约（单独触发，不埋进"跑全部"）
```

## 文档

| 文件 | 作用 |
|---|---|
| `Magpie-项目计划书.md` | 产品蓝图与路线图（**排期以它为准**） |
| `v3.2-design.md` | 数据层设计冻结 + 修正记录（r2/r3/r4） |
| `Magpie-搭建思路.md` | 逐阶段的施工顺序与注意事项 |
| `Magpie-数据模型修订稿-v3.1.md` | 历史稿，已被 v3.2 取代，仅作参照 |

> 纪律：**排期前先跑一遍，别信文档。**（v3.2-design.md 曾同时写错三件事 ——
> redo 未实现、性能未验证、测试数 62 —— 而代码里早已不是那样。）
