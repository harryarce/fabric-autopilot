"""Data source discovery routes (SQL endpoints & lakehouses)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from fabric_services import ServiceContainer

from ..dependencies import get_container
from ..models import to_jsonable

router = APIRouter(prefix="/workspaces/{workspace_id}/datasources", tags=["datasources"])


@router.get("/sql-endpoints")
def list_sql_endpoints(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> list[dict]:
    """List SQL analytics endpoints / warehouses in a workspace."""
    endpoints = container.schema_service().list_sql_endpoints(workspace_id)
    return [to_jsonable(e) for e in endpoints]


@router.get("/lakehouses")
def list_lakehouses(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> list[dict]:
    """List lakehouses in a workspace."""
    lakehouses = container.schema_service().list_lakehouses(workspace_id)
    return [to_jsonable(lh) for lh in lakehouses]
