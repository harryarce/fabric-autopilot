"""Provisioning service — workspace discovery and Fabric access preflight.

Exposes the platform-identity operations: enumerating the workspaces the
Managed Identity / service principal can see, and a **preflight check** that
turns the most common onboarding failure (the SP not being enabled for Fabric
APIs, or lacking a workspace role) into a single, actionable error instead of an
opaque 401/403 deep in a later call.

This is where future automated workspace provisioning and data-pipeline
orchestration hooks will live.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.fabric_client import FabricApiError, FabricClient, Workspace

from .errors import FabricAccessError, UpstreamError


@dataclass
class FabricAccessStatus:
    """Result of the Fabric access preflight."""

    ok: bool
    workspace_count: int = 0
    message: str = ""


class ProvisioningService:
    """Platform-identity Fabric operations and onboarding preflight."""

    def __init__(self, fabric_client: FabricClient) -> None:
        self._fabric = fabric_client

    def list_workspaces(self) -> list[Workspace]:
        """List all workspaces visible to the platform identity.

        Translates the low-level :class:`FabricApiError` into a domain error so
        the API/MCP layers return a clean problem response instead of a raw 500:
        an authentication/authorization failure (401/403) becomes a
        :class:`FabricAccessError`; any other upstream failure becomes an
        :class:`UpstreamError`.
        """
        try:
            return self._fabric.list_workspaces()
        except FabricAccessError:
            raise
        except FabricApiError as exc:
            if exc.status_code in (401, 403):
                raise FabricAccessError(
                    "The platform identity is not authenticated to call the "
                    "Fabric REST API. Ensure (1) you are signed in (locally: "
                    "'az login'; in Azure: the Managed Identity is assigned), "
                    "(2) the tenant setting 'Service principals can use Fabric "
                    "APIs' is enabled for this identity, and (3) the identity "
                    "has a role on the target workspace(s). See "
                    "docs/deployment.md.",
                    details={"status_code": exc.status_code, "reason": exc.message},
                ) from exc
            raise UpstreamError(
                "The Fabric REST API returned an unexpected error while listing "
                "workspaces.",
                details={"status_code": exc.status_code, "reason": exc.message},
            ) from exc

    def check_fabric_access(self) -> FabricAccessStatus:
        """Verify the platform identity can call Fabric APIs.

        Performs a lightweight ``list_workspaces`` call. A failure almost always
        means one of two onboarding steps is missing:

        1. The tenant setting *"Service principals can use Fabric APIs"* is off,
           or the identity is not in an allowed security group.
        2. The identity has not been granted a role on any workspace.

        Both are documented in ``docs/deployment.md``. The returned status is
        designed to surface as a red/green badge in the UI and a
        ``GET /api/v1/health/fabric`` probe.
        """
        try:
            workspaces = self.list_workspaces()
        except FabricAccessError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to a clear error
            raise FabricAccessError(
                "The platform identity could not call the Fabric REST API. "
                "Ensure (1) the tenant setting 'Service principals can use "
                "Fabric APIs' is enabled for this identity, and (2) the "
                "identity has been granted a role on the target workspace(s). "
                "See docs/deployment.md.",
                details={"reason": str(exc)},
            ) from exc

        return FabricAccessStatus(
            ok=True,
            workspace_count=len(workspaces),
            message=(
                f"Fabric access verified: {len(workspaces)} workspace(s) "
                "visible to the platform identity."
            ),
        )
