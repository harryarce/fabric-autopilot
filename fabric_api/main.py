"""Application factory and middleware wiring for the Fabric SaaS API."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from fabric_services import build_container
from fabric_services.context import TenantContext
from fabric_services.errors import ServiceError

from .errors import service_error_handler, unhandled_error_handler
from .operation_store import OperationStore
from .routers import (
    agents,
    artifacts,
    audits,
    datasources,
    governance,
    health,
    lifecycle,
    operations,
    reports,
    schemas,
    semantic_models,
    workspaces,
)
from .settings import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fabric_api")

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        description=(
            "Stateless REST API over the Fabric semantic-model & report "
            "service layer. Authenticates to Fabric and SQL with Managed "
            "Identity; agentic design/audit powered by Microsoft Foundry."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Build the service container once; reuse across requests.
    app.state.container = build_container()
    app.state.settings = settings
    app.state.operations = OperationStore()

    # Optional Azure Monitor / Application Insights instrumentation.
    if settings.applicationinsights_connection_string:
        try:  # pragma: no cover - optional dependency
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(
                connection_string=settings.applicationinsights_connection_string
            )
            logger.info("Azure Monitor instrumentation enabled.")
        except Exception:  # noqa: BLE001
            logger.warning("Azure Monitor instrumentation unavailable; skipping.")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = request_id
        return response

    @app.middleware("http")
    async def audit_log_middleware(request: Request, call_next):
        """Append every mutating API call to the per-tenant audit log.

        Read-only requests (GET/HEAD/OPTIONS) are skipped to keep the log
        signal-to-noise high. Logging is best-effort: failures never break
        the request.
        """
        response = await call_next(request)
        try:
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return response
            container = request.app.state.container
            tenant = TenantContext.default()
            tenant_header = request.headers.get("X-Tenant-Id")
            if tenant_header:
                tenant = TenantContext(
                    tenant_id=tenant_header,
                    user_id=request.headers.get("X-User-Id"),
                    correlation_id=request.headers.get("X-Correlation-Id"),
                )
            container.audit_log_service(tenant).log(
                action=f"{request.method} {request.url.path}",
                correlation_id=response.headers.get("X-Correlation-Id"),
                status="ok" if response.status_code < 400 else "error",
                details={"status_code": response.status_code},
            )
        except Exception:  # noqa: BLE001 — never fail the request
            logger.warning("audit log middleware failed", exc_info=True)
        return response

    # Domain + catch-all error handlers -> problem+json.
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # Routers. Health is unprefixed for probes; the rest are versioned.
    app.include_router(health.router)
    for module in (
        workspaces,
        datasources,
        schemas,
        semantic_models,
        reports,
        audits,
        artifacts,
        operations,
        agents,
        lifecycle,
        governance,
    ):
        app.include_router(module.router, prefix=API_PREFIX)

    return app


# ASGI entrypoint: ``uvicorn fabric_api.main:app``.
app = create_app()
