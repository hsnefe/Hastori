"""One error format for the whole API: {"error": {"code": ..., "message": ...}}."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("api")

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "too_many_requests",
    503: "unavailable",
}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or _CODES.get(status, "error")
        self.headers = headers


def unauthorized(message: str = "Authentication required") -> ApiError:
    return ApiError(401, message, headers={"WWW-Authenticate": "Bearer"})


def forbidden(message: str = "Your role does not allow this") -> ApiError:
    return ApiError(403, message)


def not_found(message: str = "Not found") -> ApiError:
    return ApiError(404, message)


def conflict(message: str) -> ApiError:
    return ApiError(409, message)


def _response(
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body = ErrorBody(code=code, message=message, details=details)
    return JSONResponse(
        {"error": body.model_dump(exclude_none=True)}, status_code=status, headers=headers
    )


# What every endpoint can answer besides its own success; shown in Swagger.
COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "The role does not allow this"},
    404: {"model": ErrorResponse, "description": "Not found, or not in one of your sites"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODES.get(exc.status_code, "error")
        return _response(exc.status_code, code, str(exc.detail), dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": [str(p) for p in e["loc"]], "msg": str(e["msg"])} for e in exc.errors()]
        return _response(422, "validation_error", "Invalid request", details=details)

    # A dependency that is down is not a bug in a request: say so (503) and when to try again,
    # instead of an anonymous 500. Only connection-level failures are mapped; an error in a query
    # or a script is still a 500 and shows up in the log.
    @app.exception_handler(RedisConnectionError)
    @app.exception_handler(RedisTimeoutError)
    @app.exception_handler(BusyLoadingError)
    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(PoolTimeoutError)
    async def dependency_down(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "dependency unavailable", extra={"path": request.url.path, "error": str(exc)[:200]}
        )
        return _response(
            503, "unavailable", "A backing service is unavailable; try again shortly",
            headers={"Retry-After": "5"},
        )  # fmt: skip

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; the log has the stack.
        log.error("unhandled error", exc_info=exc, extra={"path": request.url.path})
        return _response(500, "internal_error", "Something went wrong")
