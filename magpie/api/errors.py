"""API 错误信封 —— 一个错误只有一种形状（计划书 §6.3）。

    {"ok": false, "error": {"code": "E_...", "message": "...", "hint": "..."}}

为什么错误码是**字符串**而不是 HTTP 状态码：
    agent 需要的是"我该怎么办"。`404` 说不清"项目名写错了"还是"实体被 merge 走了"，
    而 `E_PROJECT_NOT_FOUND` + `hint` 说得清。HTTP 状态码照旧给（GUI / 代理要看它），
    但它是**传输层**的信号，不是调用方的判决书。

`E_NOT_IMPLEMENTED` 是这套信封里最要紧的一个：2.7 只交付了 §6.2 的一部分端点，
没交付的那些**必须响亮地说"还没做"**，而不是返回一个空数组。空数组对 agent 是
一个确定的断言（"这里就是没有"），而事实是"这条路由还不存在" —— 两者混起来，
agent 会基于一个假事实继续往下走。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ApiError", "E_NOT_IMPLEMENTED"]


class ApiError(Exception):
    """可读的 API 错误。业务代码 `raise ApiError(...)`，由 `app` 统一转成信封。"""

    def __init__(self, status: int, code: str, message: str, *,
                 hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.hint = hint
        #: 结构化补充（candidates / supported / ...）—— §6.3 的 "candidates": [...] 就落在这里。
        self.extra = extra

    def envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        error.update(self.extra)
        return {"ok": False, "error": error}

    def __repr__(self) -> str:                                # pragma: no cover
        return f"ApiError({self.status}, {self.code!r}, {self.message!r})"


E_NOT_IMPLEMENTED = "E_NOT_IMPLEMENTED"


def not_implemented(what: str, *, plan: str) -> ApiError:
    """501 —— "这条路由还在计划里"。**故意比 404 更响**：404 会被读成"没有这个资源"。"""
    return ApiError(501, E_NOT_IMPLEMENTED,
                    f"{what} 尚未实现",
                    hint=f"它属于 {plan}；本步（2.7）只交付已能兑现的端点。"
                         f"在它落地之前，请按 hint 里的模块名直连 core。",
                    plan=plan)
