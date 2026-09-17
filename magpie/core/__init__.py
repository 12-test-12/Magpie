"""v3.2 core —— 事实层 / 缓存层 / 身份层 / 工作层 / 审计层。

唯一公理: 数据行是缓存，贡献流是事实。

模块:
    store        schema 加载、事实写入口、recompute、GC、不变量检查、快照等价、可注入时钟
    confidence   §8.2 反多数票置信度
    ops          事务型状态迁移 (undo / rekey / merge / unmerge / compact)、coverage、evidence 降级
    manual       INV-12 唯一 manual 写入口（单写队列 + 乐观版本 + 并集合并）
"""

from . import confidence, manual, ops, store          # noqa: F401

__all__ = ["store", "ops", "confidence", "manual"]
