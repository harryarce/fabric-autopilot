"""Health, readiness, and Fabric-access probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from fabric_services import ServiceContainer
from fabric_services.errors import FabricAccessError

from ..dependencies import get_container

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Liveness probe — process is up."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz() -> dict:
    """Readiness probe — process can serve traffic."""
    return {"status": "ready"}


@router.get("/api/v1/health/fabric")
def fabric_health(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Preflight: can the platform identity call the Fabric REST API?

    Returns a green/red status suitable for a UI badge. A failure indicates the
    Managed Identity / service principal is not enabled for Fabric APIs or lacks
    a workspace role (see docs/deployment.md).
    """
    try:
        status = container.provisioning_service().check_fabric_access()
    except FabricAccessError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
    return {
        "ok": status.ok,
        "workspaceCount": status.workspace_count,
        "message": status.message,
    }
