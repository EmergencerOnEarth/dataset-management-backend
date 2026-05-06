from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def ok(request: Request, data: Any = None) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "OK",
        "data": {} if data is None else data,
        "traceId": trace_id(request),
    }


def error_response(
    request: Request,
    *,
    status_code: int,
    code: int,
    message: str,
    error_code: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "errorCode": error_code,
            "details": details or {},
            "traceId": trace_id(request),
        },
    )
