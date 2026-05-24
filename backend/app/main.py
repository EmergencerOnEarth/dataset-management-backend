from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from backend.app.api.dataset import router as dataset_router
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from backend.app.core.responses import error_response
from backend.app.db.session import init_db
from backend.app.services.seed_demo import ensure_demo_seed
from backend.app.workers.import_recovery import recover_stalled_import_tasks_on_startup

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_INDEX = _REPO_ROOT / "static" / "demo" / "index.html"

settings = get_settings()
_logger = logging.getLogger(__name__)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
_IMPORT_RECOVERY_INTERVAL_S = 30.0


async def _import_recovery_loop() -> None:
    while True:
        await asyncio.sleep(_IMPORT_RECOVERY_INTERVAL_S)
        try:
            await asyncio.to_thread(recover_stalled_import_tasks_on_startup)
        except Exception:
            _logger.exception("periodic import recovery failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create schema on startup; demo seed is idempotent."""
    init_db()
    ensure_demo_seed()
    recover_stalled_import_tasks_on_startup()
    recovery_task = asyncio.create_task(_import_recovery_loop())
    try:
        yield
    finally:
        recovery_task.cancel()
        try:
            await recovery_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="眼科科研平台数据集管理后端",
    version="0.2.0",
    description="数据集管理 API：上传、导入、浏览、导出（设计 V0.2.0）。",
    debug=settings.app_debug,
    lifespan=lifespan,
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Trace-Id"] = request.state.trace_id
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if settings.app_auth_disabled or path in {"/health", "/docs", "/openapi.json", "/redoc"}:
        return await call_next(request)
    if path == "/demo" or path.startswith("/demo/"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return error_response(
            request,
            status_code=401,
            code=40101,
            message="未认证。",
            error_code="UNAUTHORIZED",
        )
    token = auth.removeprefix("Bearer ").strip()
    allowed = {t.strip() for t in (settings.auth_allowed_tokens or "").split(",") if t.strip()}
    if allowed and token not in allowed:
        return error_response(
            request,
            status_code=401,
            code=40101,
            message="访问令牌无效。",
            error_code="INVALID_TOKEN",
        )
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        error_code=exc.error_code,
        details=exc.details,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        request,
        status_code=422,
        code=42201,
        message="请求参数校验失败。",
        error_code="DATASET_VALIDATION_ERROR",
        details={"errors": exc.errors()},
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "dataset-backend", "env": settings.app_env}


@app.get("/demo")
@app.get("/demo/")
@app.get("/demo/index.html")
def demo_ui():
    """极简联调页（避免 Mount /demo 抢走 /demo/ 导致 StaticFiles 目录 404）。"""
    if not _DEMO_INDEX.is_file():
        raise HTTPException(status_code=404, detail="static/demo/index.html 不存在，请检查仓库文件。")
    return FileResponse(_DEMO_INDEX, media_type="text/html; charset=utf-8")


app.include_router(dataset_router)
