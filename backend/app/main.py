from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.api.dataset import router as dataset_router
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from backend.app.core.responses import error_response


settings = get_settings()

app = FastAPI(
    title="眼科科研平台数据集管理后端",
    version="0.1.0",
    description="数据集管理 mock API 服务，接口契约对齐 V0.2.0 设计文档。",
    debug=settings.app_debug,
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Trace-Id"] = request.state.trace_id
    return response


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
        error_code="REQUEST_VALIDATION_FAILED",
        details={"errors": exc.errors()},
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "dataset-backend", "env": settings.app_env}


app.include_router(dataset_router)

