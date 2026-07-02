"""Governance service: permissions, sensitivity labels, tags, workspace roles.

Wraps the Fabric REST surface for item-level RBAC, MIP sensitivity labels,
tag application, and workspace-scope role assignments. Errors are normalized
to :class:`UpstreamError`.
"""

from __future__ import annotations

from typing import Any

from app.fabric_client import FabricClient

from .errors import UpstreamError, ValidationError

# Roles accepted by the Fabric item RBAC surface. We keep this list in code
# (rather than calling out for it) so the service can pre-validate inputs and
# return a clear ``ValidationError`` instead of opaque 400s from Fabric.
_VALID_ITEM_ROLES = {"Read", "ReadAll", "ReadWrite", "Owner", "Member"}
_VALID_WORKSPACE_ROLES = {"Admin", "Member", "Contributor", "Viewer"}
_VALID_PRINCIPAL_TYPES = {
    "User",
    "Group",
    "ServicePrincipal",
    "ManagedIdentity",
}


class GovernanceService:
    """Manage item permissions, labels, tags, and workspace role assignments."""

    def __init__(self, fabric_client: FabricClient) -> None:
        self._fabric = fabric_client

    # -- Item permissions ------------------------------------------------

    def list_item_permissions(
        self, workspace_id: str, item_id: str
    ) -> list[dict[str, Any]]:
        """List role assignments on a single item."""
        try:
            return self._fabric.list_item_role_assignments(workspace_id, item_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to list item role assignments: {exc}"
            ) from exc

    def set_item_permission(
        self,
        workspace_id: str,
        item_id: str,
        *,
        principal_id: str,
        principal_type: str,
        role: str,
    ) -> dict[str, Any]:
        """Grant a role on one item to a principal (User/Group/SP/MI)."""
        if principal_type not in _VALID_PRINCIPAL_TYPES:
            raise ValidationError(
                f"principal_type must be one of {sorted(_VALID_PRINCIPAL_TYPES)}"
            )
        if role not in _VALID_ITEM_ROLES:
            raise ValidationError(
                f"role must be one of {sorted(_VALID_ITEM_ROLES)}"
            )
        try:
            return self._fabric.set_item_permissions(
                workspace_id,
                item_id,
                principal_id=principal_id,
                principal_type=principal_type,
                role=role,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to set item permission: {exc}") from exc

    # -- Sensitivity labels ----------------------------------------------

    def apply_sensitivity_label(
        self,
        workspace_id: str,
        item_id: str,
        *,
        label_id: str,
        assignment_method: str = "Standard",
    ) -> dict[str, Any]:
        """Apply a Microsoft Information Protection label to an item."""
        if assignment_method not in {"Standard", "Privileged"}:
            raise ValidationError(
                "assignment_method must be 'Standard' or 'Privileged'"
            )
        try:
            return self._fabric.apply_sensitivity_label(
                workspace_id,
                item_id,
                label_id=label_id,
                assignment_method=assignment_method,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to apply sensitivity label: {exc}"
            ) from exc

    def remove_sensitivity_label(
        self, workspace_id: str, item_id: str
    ) -> None:
        """Remove the sensitivity label from an item."""
        try:
            self._fabric.remove_sensitivity_label(workspace_id, item_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to remove sensitivity label: {exc}"
            ) from exc

    # -- Tags ------------------------------------------------------------

    def apply_tags(
        self, workspace_id: str, item_id: str, *, tag_ids: list[str]
    ) -> dict[str, Any]:
        """Apply one or more tags to an item."""
        if not tag_ids:
            raise ValidationError("tag_ids must be a non-empty list")
        try:
            return self._fabric.apply_tags(workspace_id, item_id, tag_ids=tag_ids)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to apply tags: {exc}") from exc

    # -- Workspace role assignments --------------------------------------

    def list_workspace_roles(self, workspace_id: str) -> list[dict[str, Any]]:
        try:
            return self._fabric.list_workspace_role_assignments(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to list workspace role assignments: {exc}"
            ) from exc

    def add_workspace_role(
        self,
        workspace_id: str,
        *,
        principal_id: str,
        principal_type: str,
        role: str,
    ) -> dict[str, Any]:
        if principal_type not in _VALID_PRINCIPAL_TYPES:
            raise ValidationError(
                f"principal_type must be one of {sorted(_VALID_PRINCIPAL_TYPES)}"
            )
        if role not in _VALID_WORKSPACE_ROLES:
            raise ValidationError(
                f"role must be one of {sorted(_VALID_WORKSPACE_ROLES)}"
            )
        try:
            return self._fabric.add_workspace_role_assignment(
                workspace_id,
                principal_id=principal_id,
                principal_type=principal_type,
                role=role,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to assign workspace role: {exc}"
            ) from exc
