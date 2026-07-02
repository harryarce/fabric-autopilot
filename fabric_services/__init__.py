"""Service layer for the Fabric semantic-model & report platform.

This package holds the **business logic orchestration** that previously lived
inline in the Streamlit pages. Each service is a plain class with explicit
constructor dependencies (Fabric REST client, SQL client, artifact store,
optional Foundry agents) so it can be driven equally well from:

* the FastAPI backend (:mod:`fabric_api`),
* the MCP server (:mod:`fabric_mcp`), and
* unit tests (with fakes injected).

Services never import Streamlit and never read global state. All Fabric / SQL
authentication flows through the shared :class:`app.auth.TokenProvider`, which
resolves a Managed Identity in Azure and the Azure CLI locally.

The :class:`~fabric_services.context.TenantContext` is threaded through every
service so the platform is multi-tenant ready even while deployed single-tenant:
artifacts are namespaced per tenant and Fabric calls can be scoped later without
touching service code.
"""

from __future__ import annotations

from .container import ServiceContainer, build_container
from .context import TenantContext
from .errors import (
    DependencyUnavailableError,
    FabricAccessError,
    NotFoundError,
    ServiceError,
    ThrottledError,
    ValidationError,
)

__all__ = [
    "ServiceContainer",
    "build_container",
    "TenantContext",
    "ServiceError",
    "NotFoundError",
    "ValidationError",
    "DependencyUnavailableError",
    "FabricAccessError",
    "ThrottledError",
]
