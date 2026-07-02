"""Workspace discovery routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from fabric_services import ServiceContainer

from ..dependencies import get_container
from ..models import to_jsonable

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("")
def list_workspaces(
    container: ServiceContainer = Depends(get_container),
) -> list[dict]:
    """List all Fabric workspaces visible to the platform identity."""
    workspaces = container.provisioning_service().list_workspaces()
    return [to_jsonable(w) for w in workspaces]
