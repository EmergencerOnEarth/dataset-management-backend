from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AppError(Exception):
    message: str
    error_code: str
    code: int = 40001
    status_code: int = 400
    details: dict[str, Any] | None = None


class NotFoundError(AppError):
    def __init__(self, message: str = "资源不存在。", details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            error_code="RESOURCE_NOT_FOUND",
            code=40401,
            status_code=404,
            details=details,
        )
