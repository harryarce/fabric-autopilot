"""Lifecycle service: Git integration and Deployment Pipelines.

Thin orchestrator on top of :class:`~app.fabric_client.FabricClient` that
exposes workspace-Git and deployment-pipeline operations to the API and MCP
layers. Errors are normalized to :class:`UpstreamError` so the surface stays
consistent with the other services.
"""

from __future__ import annotations

from typing import Any

from app.fabric_client import FabricClient

from .errors import UpstreamError


class LifecycleService:
    """Manage Git connection, commits, pulls, and deployment-pipeline runs."""

    def __init__(self, fabric_client: FabricClient) -> None:
        self._fabric = fabric_client

    # -- Git integration -------------------------------------------------

    def connect_git(
        self,
        workspace_id: str,
        *,
        provider: str,
        organization: str,
        project: str,
        repository: str,
        branch: str,
        directory: str = "/",
    ) -> dict[str, Any]:
        """Connect a workspace to a Git repository."""
        try:
            return self._fabric.git_connect(
                workspace_id,
                provider=provider,
                organization=organization,
                project=project,
                repository=repository,
                branch=branch,
                directory=directory,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to connect workspace to Git: {exc}") from exc

    def git_status(self, workspace_id: str) -> dict[str, Any]:
        """Return the current Git connection status for the workspace."""
        try:
            return self._fabric.git_status(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to fetch Git status: {exc}") from exc

    def commit_to_git(
        self,
        workspace_id: str,
        *,
        comment: str,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Commit workspace changes (all or selective) to Git."""
        try:
            status, result = self._fabric.git_commit_to_repo(
                workspace_id, comment=comment, items=items
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to commit to Git: {exc}") from exc
        return {"status": status, "result": result, "scope": "selective" if items else "all"}

    def update_from_git(self, workspace_id: str) -> dict[str, Any]:
        """Pull changes from Git into the workspace."""
        try:
            status, result = self._fabric.git_update_from_repo(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to pull from Git: {exc}") from exc
        return {"status": status, "result": result}

    # -- Deployment pipelines --------------------------------------------

    def list_pipelines(self) -> list[dict[str, Any]]:
        """List deployment pipelines available to the platform identity."""
        try:
            return self._fabric.list_deployment_pipelines()
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to list deployment pipelines: {exc}") from exc

    def deploy(
        self,
        pipeline_id: str,
        *,
        source_stage_id: str,
        target_stage_id: str,
        note: str | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Deploy items from one pipeline stage to the next."""
        try:
            status, result = self._fabric.deploy_to_stage(
                pipeline_id,
                source_stage_id=source_stage_id,
                target_stage_id=target_stage_id,
                note=note,
                items=items,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to deploy: {exc}") from exc
        return {"status": status, "result": result}
