"""FastAPI 应用工厂。

启动流程：加载配置 → 打开 SQLite（WAL）→ 建表 → 挂路由与静态页面。
关闭流程：关库连接。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from quality_trace.api.deps import WEB_DIR, load_app_config
from quality_trace.api.routers import admin as admin_router
from quality_trace.api.routers import analytics as analytics_router
from quality_trace.api.routers import admin_pages
from quality_trace.api.routers import auth as auth_router
from quality_trace.api.routers import chat as chat_router
from quality_trace.api.routers import defects as defects_router
from quality_trace.api.routers import genealogy as genealogy_router
from quality_trace.core.repo import db as repo
from quality_trace.core.services.errors import ApiError


def create_app() -> FastAPI:
    """创建应用实例（uvicorn 入口：quality_trace.api.app:app）。"""
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- 启动：配置 + 数据库 ----
        app.state.config = load_app_config()
        db_path = app.state.config.get("chat", {}).get(
            "db_path", str(repo.DEFAULT_DB_PATH)
        )
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)   # data/ 可能不存在
        app.state.db = repo.connect(db_path)
        repo.init_schema(app.state.db)
        yield
        # ---- 关闭 ----
        app.state.db.close()

    app = FastAPI(
        title="quality-trace 问答服务",
        version="0.1.0",
        description="制造业质量追溯与缺陷归因 · 知识库问答（质量工程师入口）",
        lifespan=lifespan,
    )

    # ---- 统一异常：ApiError → {code, message, data} 封装（docs/chat-api.md 第 1 节） ----
    @app.exception_handler(ApiError)
    async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
        # HTTP 状态取错误码前三位（40001→400），body 恒为统一封装
        status = int(str(exc.code)[:3]) if exc.code >= 10000 else 400
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": exc.message, "data": None},
        )

    app.include_router(auth_router.router)
    app.include_router(chat_router.router)
    app.include_router(admin_router.router)        # 主管后台 JSON 接口
    app.include_router(admin_pages.router)         # 主管后台页面（服务端渲染）
    app.include_router(defects_router.router)      # 班组长现场录入接口
    app.include_router(analytics_router.router)    # 统计分析与 SPC 接口
    app.include_router(genealogy_router.router)    # 批次谱系追溯接口

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """单页聊天界面。"""
        return FileResponse(WEB_DIR / "chat.html")

    @app.get("/m", include_in_schema=False)
    async def mobile() -> FileResponse:
        """班组长现场录入端（移动优先单页，登录在页面内完成）。"""
        return FileResponse(WEB_DIR / "mobile.html")

    return app


app = create_app()
