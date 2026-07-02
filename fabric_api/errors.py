"""Exception handlers mapping domain errors to RFC 7807 problem responses."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from fabric_services.errors import ServiceError

logger = logging.getLogger("fabric_api")


def _problem(status: int, title: str, code: str, detail: str, instance: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status,
            "code": code,
            "detail": detail,
            "instance": instance,
        },
    )


def _find_throttle(exc: BaseException) -> BaseException | None:
    """Walk the exception cause chain for an upstream throttle (HTTP 429).

    Services wrap Fabric failures as ``UpstreamError(...) from exc``, so the
    original ``FabricThrottledError`` is preserved on ``__cause__``. Detection
    is duck-typed (``retry_after`` / ``retry_after_seconds`` / a 429 status)
    to avoid importing the app-layer client into the API error module.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (
            getattr(current, "retry_after", None) is not None
            or getattr(current, "retry_after_seconds", None) is not None
            or getattr(current, "status_code", None) == 429
            or getattr(current, "status", None) == 429
        ):
            return current
        current = current.__cause__ or current.__context__
    return None


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Translate a :class:`ServiceError` into a problem+json response."""
    throttle = _find_throttle(exc)
    if throttle is not None:
        retry_after = (
            getattr(throttle, "retry_after", None)
            or getattr(throttle, "retry_after_seconds", None)
            or 60
        )
        logger.info(
            "Upstream throttle on %s: retry after %ss", request.url.path, retry_after
        )
        response = _problem(
            status=429,
            title="Throttled",
            code="throttled",
            detail=exc.message,
            instance=str(request.url.path),
        )
        response.headers["Retry-After"] = str(int(retry_after))
        return response
    if exc.status >= 500:
        logger.exception("Service error on %s: %s", request.url.path, exc.message)
    else:
        logger.info("Service error on %s: %s", request.url.path, exc.message)
    return _problem(
        status=exc.status,
        title=exc.code.replace("_", " ").title(),
        code=exc.code,
        detail=exc.message,
        instance=str(request.url.path),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so the API never leaks stack traces to clients."""
    logger.exception("Unhandled error on %s", request.url.path)
    return _problem(
        status=500,
        title="Internal Server Error",
        code="internal_error",
        detail="An unexpected error occurred.",
        instance=str(request.url.path),
    )
