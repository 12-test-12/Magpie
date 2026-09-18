"""FastAPI 装配与启动（计划书 §6 / §7.6 / 2.7）。

    magpie app [--db <path>] [--data-dir <dir>] [--port N] [--no-browser]
               [--host 127.0.0.1]

三件在装配层（而不是路由层）解决的事，每一件都有理由:

  1. **只绑回环**。`serve()` 在**绑定之前**拒绝非回环 host —— 校验放在"请求进来
     之后"是没有意义的：那时端口已经对全网敞开过了。

  2. **`/docs` 与 `/redoc` 关掉**。FastAPI 的 Swagger UI / ReDoc 默认从
     `cdn.jsdelivr.net` 拉 JS 与 CSS —— 打开一次文档页就是一次**出站请求**，
     而零出站是这个项目的红线（R7）。要读接口去 `GET /openapi.json`（纯 JSON，
     零外部资源），或者用本地页 `/`。

  3. **`/openapi.json` 不要 token**。它描述的是**形状**（路由名、参数、模型），
     不是数据；数据仍然要 token。给 GUI 的自助文档加 token 只会催生"把 token
     写进 URL 再泄露出去"这种做法。这个取舍写在这里，别让它变成一条没人知道的规则。

鉴权本身在 `routes` 的 router 级依赖上（`require_token`）—— 路由**不可能**
忘记加，因为它不在每个函数里，而在 router 上。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core import store
from . import API_PREFIX, DEFAULT_HOST, OPENAPI_PATH
from .auth import TokenAuth, is_loopback
from .errors import ApiError
from .events import EventBroker
from .imports import ImportRegistry
from .routes import router
from .writer import WriteQueue

__all__ = ["ApiContext", "create_app", "serve", "WEB_DIR"]

#: 静态资源目录（2.8 的 Vite 构建产物将来落在这里）。**本地打包，不引 CDN。**
WEB_DIR = Path(__file__).resolve().parent / "web"

#: 数据区默认名。**与 `core.pipeline` 的 `data_dir` 缺省值一致** —— 两个入口
#: 指同一个目录，才不会出现"pipeline 导进 .magpie、app 打开 magpie-data"这种
#: 最容易被当成"数据丢了"的分叉。
DEFAULT_DATA_DIRNAME = ".magpie"


@dataclass
class ApiContext:
    """一个服务实例的全部状态。挂在 `app.state.ctx` 上，路由从那里取。"""

    db_path: Path
    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = 0
    token: TokenAuth = field(default_factory=TokenAuth)
    broker: EventBroker = field(default_factory=EventBroker)
    writer: WriteQueue | None = None
    imports: ImportRegistry | None = None
    started_at: float = 0.0

    def connect_ro(self) -> sqlite3.Connection:
        """每请求一条**查询专用**连接。

        `query_only = ON` 让"这条连接只读"变成数据库自己执行的事实：哪天有人在
        路由里顺手写了条 UPDATE，它会当场报错，而不是在一次并发写里留下一个
        谁也复现不出来的状态。**写者唯一**靠的就是这条把侥幸堵死的。

        `check_same_thread=False` 是**必须**的，理由不是"要跨线程共享"，而是
        FastAPI 的同步依赖与同步端点**各自**被丢进线程池：创建这条连接的线程
        与使用它的线程不保证是同一个。它们仍然是**先后**发生（依赖 → 端点 →
        依赖清理），所以"不被并发使用"这条真正要紧的约束没有被放松 ——
        `store.connect` 的文档说的"只应由单写队列使用"，指的是**共享**，
        不是"跨线程用一次"。把它关上会让并发请求随机 500（已实测）。
        """
        conn = store.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA query_only = ON")
        return conn

    def info(self) -> dict[str, Any]:
        return {
            "host": self.host, "port": self.port, "db": str(self.db_path),
            "data_dir": str(self.data_dir), "api_prefix": API_PREFIX,
            "auth_failures": self.token.failures,
            "uptime_s": round(time.monotonic() - self.started_at, 3) if self.started_at else 0,
            "writer": None if self.writer is None else {
                **self.writer.stats.as_dict(), "error": self.writer.error},
            "events": self.broker.stats(),
            "imports": None if self.imports is None else self.imports.stats(),
        }


def _require_token(request: Request) -> None:
    """router 级鉴权 —— 路由**不可能**忘记加（它不在每个函数里，而在 router 上）。"""
    ctx: ApiContext = request.app.state.ctx
    if not ctx.token.check(headers=request.headers, query=request.query_params):
        raise ApiError(401, "E_UNAUTHORIZED", "缺少或错误的 token",
                       hint="带 Authorization: Bearer <token>；SSE（EventSource）"
                            "发不了自定义头，那条路由接受 ?token=<token>")


def create_app(*, db_path: str | Path | None = None,
               data_dir: str | Path | None = None,
               token: str | None = None,
               host: str = DEFAULT_HOST,
               port: int = 0,
               actor: str = "api") -> FastAPI:
    """装配一个 app。**不启动写线程** —— 那是 lifespan 的活（见下）。"""
    if not is_loopback(host):
        raise ValueError(
            f"拒绝在 {host!r} 上服务：本服务只允许回环地址"
            f"（{DEFAULT_HOST} / ::1）。绑 0.0.0.0 等于把这台机器上的资产数据"
            f"放到局域网上 —— 计划书 §7.6 的硬约束，不是可调参数。")

    db = Path(db_path) if db_path else Path(DEFAULT_DATA_DIRNAME) / "magpie.db"
    dd = Path(data_dir) if data_dir else db.parent
    # 目录先建出来。SQLite 的 "unable to open database file" 是它最难查的一类
    # 报错：路径里**任何一段**不存在都会得到同一句话，而它不说清是哪一段。
    db.resolve().parent.mkdir(parents=True, exist_ok=True)
    dd.resolve().mkdir(parents=True, exist_ok=True)
    ctx = ApiContext(db_path=db.resolve(), data_dir=dd.resolve(), host=host,
                     port=int(port), token=TokenAuth(token))
    ctx.writer = WriteQueue(ctx.db_path, actor=actor)
    ctx.imports = ImportRegistry(broker=ctx.broker, writer=ctx.writer,
                                 data_dir=ctx.data_dir, actor=actor)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.started_at = time.monotonic()
        ctx.writer.start()                 # 建库 / 迁移也在这里完成（写线程的活）
        try:
            yield
        finally:
            ctx.broker.close_all()
            ctx.writer.stop()

    app = FastAPI(
        title="Magpie 本地 API",
        version=__version__,
        description="人（GUI）与 agent（CLI/MCP）共享同一份数据的 HTTP 接口。"
                    "只监听回环地址；写者唯一（单写线程 + 写队列）。",
        lifespan=lifespan,
        # 见模块文档第 2 条：Swagger UI 会从 CDN 拉资源，与零出站冲突。
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_PATH,
    )
    app.state.ctx = ctx

    app.include_router(router, dependencies=[Depends(_require_token)])

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.envelope())

    @app.exception_handler(sqlite3.Error)
    async def _db_error(_request: Request, exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse(status_code=500, content=ApiError(
            500, "E_DB", f"数据库错误：{type(exc).__name__}: {exc}",
            hint="这是**服务端**的问题，不是你的参数。GET /api/v1/doctor 看数据体检。"
        ).envelope())

    # -- 静态资源（本地打包）-----------------------------------------------
    if WEB_DIR.is_dir():
        app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:               # noqa: ANN202
            return FileResponse(str(WEB_DIR / "index.html"))
    else:                                                # pragma: no cover
        @app.get("/", include_in_schema=False)
        async def index_missing() -> JSONResponse:       # noqa: ANN202
            return JSONResponse(status_code=500, content={
                "ok": False, "error": {"code": "E_NO_WEB",
                                       "message": f"静态资源目录不存在：{WEB_DIR}"}})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:              # noqa: ANN202
        return {"ok": True, "data": ctx.info(), "meta": {}}

    return app


def serve(*, db_path: str | Path | None = None, data_dir: str | Path | None = None,
          host: str = DEFAULT_HOST, port: int = 0, token: str | None = None,
          open_browser: bool = True, log_level: str = "warning") -> int:
    """起服务，直到 Ctrl-C。返回进程退出码。

    用 `uvicorn.Server` + 一条线程（而不是 `uvicorn.run`），只为了一件事：
    **在真正监听到端口之后再打印 URL**。端口是随机的时候（`--port 0`），
    先打印后绑定的做法会印出一个错端口，而人不会怀疑它。
    """
    import uvicorn

    if not is_loopback(host):
        raise ValueError(f"拒绝在 {host!r} 上服务：本服务只允许回环地址。")

    # Windows 控制台默认 GBK：不改的话，被管道/重定向接住时这几行中文会变成
    # 乱码或直接 UnicodeEncodeError，而**端口和 token 都在里面**。
    # 顺带 flush —— 被当成子进程跑时（bench、GUI 拉起），行缓冲是不保证的。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):                  # pragma: no cover
        pass

    app = create_app(db_path=db_path, data_dir=data_dir, token=token,
                     host=host, port=port)
    config = uvicorn.Config(app, host=host, port=int(port), log_level=log_level,
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="magpie-uvicorn", daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:                                # pragma: no cover
        raise RuntimeError("服务 15s 内没有起来")

    bound = server.servers[0].sockets[0].getsockname()[1] if server.servers else port
    url = f"http://{host}:{bound}/?token={app.state.ctx.token.token}"
    for line in (
        "magpie app",
        f"  URL: {url}",
        f"  API: http://{host}:{bound}{API_PREFIX}   （token 同上，走 Authorization 头）",
        f"  DB : {app.state.ctx.db_path}",
        "  停止：Ctrl-C",
    ):
        print(line, flush=True)
    if open_browser:
        webbrowser.open(url)

    try:
        while thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:                             # pragma: no cover
        server.should_exit = True
        thread.join(timeout=10)
        print("\n已停止。")
    return 0
