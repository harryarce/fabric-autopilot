"""FastAPI dependency providers.

Wires the shared :class:`fabric_services.ServiceContainer` and a per-request
:class:`fabric_services.TenantContext` into route handlers. The container is
built once at startup (cheap, stateless collaborators) and stored on
``app.state``; tenancy is resolved per request from a header so the API is
multi-tenant ready while running single-tenant today.
"""

from __future__ import annotations

from fastapi import Header, Request

from fabric_services import ServiceContainer, build_container
from fabric_services.context import TenantContext


def get_container(request: Request) -> ServiceContainer:
    """Return the process-wide service container from application state."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - defensive
        container = build_container()
        request.app.state.container = container
    return container


def get_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> TenantContext:
    """Resolve the tenant context for this request.

    Single-tenant today: when no ``X-Tenant-Id`` header is present the default
    tenant is used. The seam is in place to enforce real isolation later.
    """
    base = TenantContext.default()
    return TenantContext(
        tenant_id=x_tenant_id or base.tenant_id,
        correlation_id=x_correlation_id,
    )
