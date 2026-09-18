"""一次性 token 鉴权 + 回环绑定（计划书 §7.6）。

威胁模型（说清楚它防什么，比堆算法重要）:

    这个服务只监听 `127.0.0.1`。能连上它的，是**本机上已经能跑任意进程的东西** ——
    对那种攻击者，token 不是防线（它可以直接读同机的进程内存）。token 真正挡的是
    **别的东西顺手用上的那条路径**：另一个网页（浏览器里的 JS 能打 localhost）、
    另一个工具/脚本偶然扫到这个端口、以及日志与截图里被带出去的 URL。

于是三条纪律：

  1. **常量时间比较**（`hmac.compare_digest`）。到这一步了就别再给时序侧信道
     留门缝 —— 代价是一行代码。
  2. **SSE 允许 `?token=`**。浏览器的 `EventSource` **不能**自定义请求头，这是
     Web 标准给的硬约束，不是偷懒。回环 + 一次性 token + URL 不外传（服务自己
     不记访问日志）三者一起才让这条成立 —— 缺一条都不成立。
  3. **失败计数落在对象上**（`failures`），供测试与 `doctor` 观察。不做速率限制：
     本机上锁自己的端口，收益是零，复杂度是真的。
"""

from __future__ import annotations

import hmac
import secrets
from typing import Any, Mapping

__all__ = ["TokenAuth", "new_token", "is_loopback"]

#: `Authorization: Bearer <token>` —— 正常路径（CLI / GUI / MCP）。
HEADER_NAME = "authorization"
#: `?token=<token>` —— 只因为 `EventSource` 发不了自定义头才存在。
QUERY_NAME = "token"


def new_token() -> str:
    """启动时生成的一次性 token（每次 `magpie app` 都不同，进程退出即作废）。"""
    return secrets.token_urlsafe(32)


def is_loopback(host: str) -> bool:
    """只认回环字面量。**不做 DNS 解析** —— `localhost` 的解析结果随 hosts 文件变，
    而"绑定前先解析"等于把安全性交给一个会被改的文件。"""
    return str(host).strip().lower() in ("127.0.0.1", "::1", "localhost")


class TokenAuth:
    """一次性 token 的校验器。线程安全（只读自己 + 一个整数计数器）。"""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or new_token()
        self.failures = 0

    # -- 校验 ---------------------------------------------------------------
    def matches(self, candidate: Any) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        ok = hmac.compare_digest(candidate, self.token)
        if not ok:
            self.failures += 1
        return ok

    def from_headers(self, headers: Mapping[str, str]) -> str | None:
        """取 `Authorization: Bearer <token>` 里的 token；没有就返回 None。

        大小写不敏感地找头名：HTTP 头名本来就不区分大小写，ASGI 给的映射不保证
        大小写（测试客户端给的是小写），按 `==` 找会在某个实现上静默失败。
        """
        for key, value in headers.items():
            if str(key).lower() != HEADER_NAME:
                continue
            text = str(value).strip()
            prefix, _, rest = text.partition(" ")
            if prefix.lower() == "bearer" and rest:
                return rest.strip()
            return text or None
        return None

    def check(self, *, headers: Mapping[str, str],
              query: Mapping[str, str] | None = None) -> bool:
        """请求带没带对 token。头优先；头里没有才看 `?token=`。"""
        candidate = self.from_headers(headers)
        if candidate is None and query:
            candidate = query.get(QUERY_NAME)
        return self.matches(candidate)
