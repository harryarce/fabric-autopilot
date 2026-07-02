"""Governance routes: item permissions, sensitivity labels, tags, workspace RBAC."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from fabric_services import ServiceContainer

from ..dependencies import get_container

router = APIRouter(prefix="/governance", tags=["governance"])


class ItemPermissionRequest(BaseModel):
    principal_id: str
    principal_type: str = Field(
        ..., description="User | Group | ServicePrincipal | ManagedIdentity"
    )
    role: str = Field(
        ..., description="Read | ReadAll | ReadWrite | Owner | Member"
    )


class SensitivityLabelRequest(BaseModel):
    label_id: str
    assignment_method: str = Field("Standard", description="Standard | Privileged")


class TagsRequest(BaseModel):
    tag_ids: list[str]


class WorkspaceRoleRequest(BaseModel):
    principal_id: str
    principal_type: str
    role: str = Field(..., description="Admin | Member | Contributor | Viewer")


@router.get("/workspaces/{workspace_id}/items/{item_id}/permissions")
def list_item_permissions(
    workspace_id: str,
    item_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return {
        "value": container.governance_service().list_item_permissions(
            workspace_id, item_id
        )
    }


@router.post("/workspaces/{workspace_id}/items/{item_id}/permissions")
def set_item_permission(
    workspace_id: str,
    item_id: str,
    body: ItemPermissionRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.governance_service().set_item_permission(
        workspace_id,
        item_id,
        principal_id=body.principal_id,
        principal_type=body.principal_type,
        role=body.role,
    )


@router.post("/workspaces/{workspace_id}/items/{item_id}/sensitivity-label")
def apply_sensitivity_label(
    workspace_id: str,
    item_id: str,
    body: SensitivityLabelRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.governance_service().apply_sensitivity_label(
        workspace_id,
        item_id,
        label_id=body.label_id,
        assignment_method=body.assignment_method,
    )


@router.delete("/workspaces/{workspace_id}/items/{item_id}/sensitivity-label")
def remove_sensitivity_label(
    workspace_id: str,
    item_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    container.governance_service().remove_sensitivity_label(workspace_id, item_id)
    return {"status": "removed"}


@router.post("/workspaces/{workspace_id}/items/{item_id}/tags")
def apply_tags(
    workspace_id: str,
    item_id: str,
    body: TagsRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.governance_service().apply_tags(
        workspace_id, item_id, tag_ids=body.tag_ids
    )


@router.get("/workspaces/{workspace_id}/roles")
def list_workspace_roles(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return {
        "value": container.governance_service().list_workspace_roles(workspace_id)
    }


@router.post("/workspaces/{workspace_id}/roles")
def add_workspace_role(
    workspace_id: str,
    body: WorkspaceRoleRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.governance_service().add_workspace_role(
        workspace_id,
        principal_id=body.principal_id,
        principal_type=body.principal_type,
        role=body.role,
    )
