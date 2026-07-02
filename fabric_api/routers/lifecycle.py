"""Lifecycle routes: Git integration + Deployment Pipelines."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from fabric_services import ServiceContainer

from ..dependencies import get_container

router = APIRouter(prefix="/lifecycle", tags=["lifecycle"])


class GitConnectRequest(BaseModel):
    provider: str = Field(..., description="AzureDevOps | GitHub")
    organization: str
    project: str
    repository: str
    branch: str
    directory: str = "/"


class GitCommitRequest(BaseModel):
    comment: str
    items: list[dict] | None = Field(
        None,
        description=(
            "Optional selective-commit list "
            "(each entry: {objectId, logicalId})."
        ),
    )


class DeployRequest(BaseModel):
    pipeline_id: str
    source_stage_id: str
    target_stage_id: str
    note: str | None = None
    items: list[dict] | None = None


@router.post("/workspaces/{workspace_id}/git/connect")
def connect_git(
    workspace_id: str,
    body: GitConnectRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.lifecycle_service().connect_git(
        workspace_id,
        provider=body.provider,
        organization=body.organization,
        project=body.project,
        repository=body.repository,
        branch=body.branch,
        directory=body.directory,
    )


@router.get("/workspaces/{workspace_id}/git/status")
def git_status(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.lifecycle_service().git_status(workspace_id)


@router.post("/workspaces/{workspace_id}/git/commit")
def commit_to_git(
    workspace_id: str,
    body: GitCommitRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.lifecycle_service().commit_to_git(
        workspace_id, comment=body.comment, items=body.items
    )


@router.post("/workspaces/{workspace_id}/git/update")
def update_from_git(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.lifecycle_service().update_from_git(workspace_id)


@router.get("/deployment-pipelines")
def list_pipelines(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return {"value": container.lifecycle_service().list_pipelines()}


@router.post("/deployment-pipelines/deploy")
def deploy(
    body: DeployRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    return container.lifecycle_service().deploy(
        body.pipeline_id,
        source_stage_id=body.source_stage_id,
        target_stage_id=body.target_stage_id,
        note=body.note,
        items=body.items,
    )
