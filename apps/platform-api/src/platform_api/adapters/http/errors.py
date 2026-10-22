import logging
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from platform_contracts.v1 import ErrorCode, ErrorEnvelope
from platform_core.application.errors import RuleViolation
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
                if scope["path"].startswith("/api/"):
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def error_response(
    request: Request, status: int, code: ErrorCode, message: str, retry: str = "NONE"
) -> JSONResponse:
    envelope = ErrorEnvelope(
        code=code, message=message, request_id=request.state.request_id, retry_mode=retry
    )
    # The outer server-error handler bypasses user middleware on an unhandled exception.
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json"),
        headers={"X-Request-ID": envelope.request_id},
    )


async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    errors: dict[int, tuple[ErrorCode, str]] = {
        404: ("RESOURCE_NOT_FOUND", "Resource not found"),
        405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    }
    code, message = errors.get(exc.status_code, ("INTERNAL_ERROR", "Internal server error"))
    response = error_response(
        request, exc.status_code if exc.status_code in errors else 500, code, message
    )
    if exc.status_code == 405 and exc.headers and "Allow" in exc.headers:
        response.headers["Allow"] = exc.headers["Allow"]
    return response


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation errors may contain raw input, arbitrary keys or exception context.
    # Never reflect input values or arbitrary keys from authenticated requests either.
    return error_response(request, 400, "MALFORMED_REQUEST", "Request validation failed")


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled request error request_id=%s category=%s",
        request.state.request_id,
        type(exc).__name__,
    )
    return error_response(request, 500, "INTERNAL_ERROR", "Internal server error")


async def rule_error(request: Request, exc: RuleViolation) -> JSONResponse:
    response = error_response(request, exc.status, exc.code, exc.message, exc.retry)
    if exc.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


async def dependency_error(request: Request, exc: Exception) -> JSONResponse:
    logger.warning(
        "Database request failed request_id=%s type=%s",
        request.state.request_id,
        type(exc).__name__,
    )
    return error_response(
        request, 503, "DEPENDENCY_UNAVAILABLE", "Database temporarily unavailable", "SAME_KEY"
    )
