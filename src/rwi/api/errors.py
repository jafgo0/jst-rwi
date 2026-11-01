"""Mapping from platform errors to HTTP responses.

The status codes are chosen so that a client can tell apart "you asked wrongly",
"we genuinely do not know", and "an upstream source failed". The dashboard
depends on that distinction to render honest states instead of blanks.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from rwi.core.errors import (
    CapabilityNotSupportedError,
    InvalidCoordinateError,
    ModelConfigurationError,
    NaiveDatetimeError,
    PositionUnavailableError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    RouteError,
    RwiError,
    StaleDataError,
    UnknownVariableError,
)

_STATUS_MAP: dict[type[RwiError], int] = {
    InvalidCoordinateError: status.HTTP_400_BAD_REQUEST,
    NaiveDatetimeError: status.HTTP_400_BAD_REQUEST,
    UnknownVariableError: status.HTTP_400_BAD_REQUEST,
    RouteError: status.HTTP_400_BAD_REQUEST,
    ModelConfigurationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PositionUnavailableError: status.HTTP_409_CONFLICT,
    StaleDataError: status.HTTP_409_CONFLICT,
    ProviderRateLimitError: status.HTTP_429_TOO_MANY_REQUESTS,
    CapabilityNotSupportedError: status.HTTP_501_NOT_IMPLEMENTED,
    ProviderUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def status_for(error: Exception) -> int:
    for error_type, code in _STATUS_MAP.items():
        if isinstance(error, error_type):
            return code
    if isinstance(error, RwiError):
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def error_body(error: Exception, *, detail: str | None = None) -> dict[str, dict[str, str | None]]:
    return {
        "error": {
            "code": type(error).__name__,
            "message": str(error) or type(error).__name__,
            "detail": detail,
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RwiError)
    async def _handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_for(exc), content=error_body(exc))
