"""Artifact browsing routes (tenant-scoped store)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from ..dependencies import get_container, get_tenant
from ..models import to_jsonable

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("")
def list_artifacts(
    kind: str | None = Query(None, description="semanticModels | reports"),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> list[dict]:
    """List stored artifacts for the current tenant."""
    items = container.artifact_service(tenant).list_items(kind=kind)  # type: ignore[arg-type]
    return [to_jsonable(ref) for ref in items]


@router.get("/manifest")
def read_manifest(
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Return the full artifact manifest index for the current tenant."""
    return container.artifact_service(tenant).read_manifest()


@router.get("/definition")
def read_definition(
    kind: str = Query(..., description="semanticModels | reports"),
    workspace_id: str = Query(..., description="Owning workspace id."),
    item_id: str = Query(..., description="Stored item id."),
    workspace_name: str = Query("", description="Workspace display name."),
    item_name: str = Query("", description="Item display name."),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Load a stored artifact's definition files and metadata.

    Powers the compact file viewer in the *Artifacts* tab; returns
    ``{"files": {path: text}, "metadata": {...}}`` and 404s when the item has
    no stored definition.
    """
    from app.artifacts import ArtifactRef

    ref = ArtifactRef(
        kind=kind,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        item_id=item_id,
        workspace_name=workspace_name,
        item_name=item_name,
    )
    return container.artifact_service(tenant).load_definition(ref)

