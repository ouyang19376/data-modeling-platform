"""数据建模中心 — 统一错误模型（V1.0 抽出，供 main/auth 等模块共用）。

错误体统一信封：``{"error": {"code","message","details"}, "request_id"}``
（README.md:135-136）；``X-Request-ID`` 贯穿响应头、错误体、日志、事件与审计。
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details)


def invalid_state(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, "INVALID_STATE", message, details)
