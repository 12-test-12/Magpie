"""Magpie 本地 API —— 人（GUI）与 agent（CLI/MCP）共享同一份数据（计划书 §6 / 2.7）。

包边界（与 `magpie.core` 的分工，一条都不许越）:

    magpie.core  事实层。**只用标准库**（红线 R6）。它不知道 HTTP 存在。
    magpie.api   HTTP 外壳。可以依赖 extras.api（FastAPI / uvicorn）。
                 它**不直接写库** —— 一切写操作经 `writer.WriteQueue` 的单写线程。

四条不可谈判的设计（每条对应交付标准表里的一行）:

  1. **写者唯一**。进程内只有一个写线程、一条写队列（`writer.WriteQueue`）。
     服务在跑时，谁都别想绕过它写库 —— 两个并发导入不会各自拿着连接互相踩。
  2. **只绑 127.0.0.1**，随机端口 + 启动时一次性 token。禁止 `0.0.0.0`（`app.serve`
     会在绑定前拒绝），不开 CORS 通配。
  3. **零出站**。静态资源本地打包（`api/web/`），不引 CDN、不引外部字体 ——
     它由 `check.py` 的 R7-ASSETS 静态断言守着，不靠自觉。
  4. **`/openapi.json` 是给 agent 的说明书**，且它的内容被快照测试钉住：
     契约变了测试就红，逼着变更必须是**有意的**（而不是某次升级顺手改掉的）。

这个 `__init__` **刻意不 import** fastapi / app：`import magpie.api` 应该廉价且
无副作用，好让"装了没有"这件事可以被单独问出来（`magpie.api.available()`）。
"""

from __future__ import annotations

import importlib.util

#: 所有业务路由的前缀。**对外契约** —— 改它要升 minor 并写 CHANGELOG。
API_PREFIX = "/api/v1"

#: 给 agent 的说明书（FastAPI 自动生成，被快照测试钉住）。
OPENAPI_PATH = "/openapi.json"

#: 服务默认绑定。**只允许回环地址** —— `app.serve` 会拒绝其它值。
DEFAULT_HOST = "127.0.0.1"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

__all__ = ["API_PREFIX", "OPENAPI_PATH", "DEFAULT_HOST", "LOOPBACK_HOSTS", "available"]


def available() -> bool:
    """extras.api 装了没有。**不 import 它**，只问解释器有没有。"""
    return importlib.util.find_spec("fastapi") is not None
