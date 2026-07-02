"""Tenant context — the multi-tenancy seam.

The platform deploys single-tenant first, but every service accepts a
:class:`TenantContext` so isolation can be turned on later without reworking
business logic. Today the context drives:

* the **artifact storage prefix** (``tenants/<tenant_id>/``), keeping each
  tenant's definitions and audits in a separate blob namespace, and
* a place to carry per-request identity / correlation data.

When multi-tenancy is fully enabled, workspace scoping and per-tenant Fabric
credentials hang off this same object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Sentinel tenant used when the platform runs single-tenant. Chosen so the
# default blob prefix is stable and human-recognisable.
DEFAULT_TENANT_ID = "default"


@dataclass(frozen=True)
class TenantContext:
    """Identifies the tenant (and optionally the user) for a unit of work."""

    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str | None = None
    correlation_id: str | None = None
    # Free-form claims carried from the API/MCP edge for auditing.
    claims: dict = field(default_factory=dict)

    @classmethod
    def default(cls) -> "TenantContext":
        """Return the single-tenant default context.

        ``FABRIC_DEFAULT_TENANT_ID`` overrides the tenant id so a single-tenant
        deployment can still pick a meaningful storage namespace.
        """
        return cls(
            tenant_id=os.environ.get("FABRIC_DEFAULT_TENANT_ID", DEFAULT_TENANT_ID)
        )

    @property
    def storage_prefix(self) -> str:
        """Blob/key prefix that isolates this tenant's artifacts."""
        safe = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in (self.tenant_id or "")
        )
        return f"tenants/{safe or DEFAULT_TENANT_ID}"
