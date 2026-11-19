"""Uniform error envelope: ``{"error": {"code", "message", "details", "request_id"}}``.

Unhandled exceptions are logged with a stack trace but the client only ever sees a generic message
and the request id (secure error handling — REQUIREMENTS §16).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, RateLimitedError

logger = structlog.get_logger("errors")


def _request_id(request: Request) -> str | None:
    value = request.scope.get("state", {}).get("request_id")
    return value if isinstance(value, str) else None


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": jsonable_encoder(details) if details else {},
            "request_id": _request_id(request),
        }
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after)}
        if exc.status_code >= 500:
            logger.error("app_error", code=exc.code, message=exc.message, exc_info=exc)
        return _envelope(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo request bodies back (they may contain secrets); report field locations only.
        issues = [
            {"loc": [str(p) for p in err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return _envelope(
            request,
            status_code=422,
            code="validation_failed",
            message="Request validation failed",
            details={"issues": issues},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }
        return _envelope(
            request,
            status_code=exc.status_code,
            code=codes.get(exc.status_code, "http_error"),
            message=str(exc.detail),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=exc)
        return _envelope(
            request,
            status_code=500,
            code="internal_error",
            message="An unexpected error occurred. Quote the request id when reporting it.",
        )
