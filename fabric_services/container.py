"""Service container — composition root for the service layer.

Builds and caches the shared, stateless collaborators (Fabric REST client, SQL
client, token provider) and constructs per-tenant services on demand. The
FastAPI app and the MCP server each create exactly one
:class:`ServiceContainer` at startup and resolve services from it per request.

Keeping construction here (rather than inside services) means services stay
trivially unit-testable: tests build a container with fakes, or instantiate a
service directly with hand-made dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.artifacts import ArtifactStore, get_artifact_store
from app.auth import TokenProvider, get_token_provider
from app.fabric_client import FabricClient
from app.sql_client import SqlEndpointClient

from .context import TenantContext


@dataclass
class ServiceContainer:
    """Holds process-wide collaborators and builds tenant-scoped services."""

    token_provider: TokenProvider
    fabric_client: FabricClient
    sql_client: SqlEndpointClient

    # -- per-tenant artifact store ---------------------------------------

    def artifact_store(self, tenant: TenantContext) -> ArtifactStore:
        """Return an artifact store namespaced for ``tenant``.

        With the blob backend this maps to a ``tenants/<id>/`` prefix; with the
        local backend it maps to a per-tenant subdirectory. The store is cheap
        to construct, so it is not cached across tenants.
        """
        return get_artifact_store(prefix=tenant.storage_prefix)

    # -- service factories ------------------------------------------------

    def schema_service(self):
        from .schema_service import SchemaService

        return SchemaService(self.fabric_client, self.sql_client)

    def model_service(self, tenant: TenantContext | None = None):
        from .model_service import ModelService

        tenant = tenant or TenantContext.default()
        return ModelService(
            self.fabric_client, self.artifact_store(tenant), tenant
        )

    def report_service(self, tenant: TenantContext | None = None):
        from .report_service import ReportService

        tenant = tenant or TenantContext.default()
        return ReportService(
            self.fabric_client, self.artifact_store(tenant), tenant
        )

    def audit_service(self):
        from .audit_service import AuditService

        return AuditService()

    def artifact_service(self, tenant: TenantContext | None = None):
        from .artifact_service import ArtifactService

        tenant = tenant or TenantContext.default()
        return ArtifactService(self.artifact_store(tenant), tenant)

    def provisioning_service(self):
        from .provisioning_service import ProvisioningService

        return ProvisioningService(self.fabric_client)

    def intelligence_service(self):
        from .intelligence_service import IntelligenceService

        return IntelligenceService()

    def lifecycle_service(self):
        from .lifecycle_service import LifecycleService

        return LifecycleService(self.fabric_client)

    def governance_service(self):
        from .governance_service import GovernanceService

        return GovernanceService(self.fabric_client)

    def audit_log_service(self, tenant: TenantContext | None = None):
        from .audit_log_service import AuditLogService

        tenant = tenant or TenantContext.default()
        return AuditLogService(self.artifact_store(tenant), tenant)


def build_container() -> ServiceContainer:
    """Construct the default container using Managed Identity-backed auth."""
    token_provider = get_token_provider()
    return ServiceContainer(
        token_provider=token_provider,
        fabric_client=FabricClient(token_provider),
        sql_client=SqlEndpointClient(token_provider),
    )
