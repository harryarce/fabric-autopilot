"""Domain error types for the service layer.

These are transport-agnostic: the FastAPI layer maps them to RFC 7807 problem
responses and the MCP layer maps them to tool errors. Services raise these
instead of leaking ``requests`` / ``pyodbc`` exceptions to callers.
"""

from __future__ import annotations


class ServiceError(RuntimeError):
    """Base class for all service-layer errors.

    Attributes:
        message: human-readable description.
        code: stable, machine-readable error code (snake/kebab style).
        status: suggested HTTP status code for the API layer.
        details: optional structured context.
    """

    code: str = "service_error"
    status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}


class NotFoundError(ServiceError):
    """A requested resource (workspace, item, artifact) does not exist."""

    code = "not_found"
    status = 404


class ValidationError(ServiceError):
    """The request was understood but is semantically invalid."""

    code = "validation_error"
    status = 422


class DependencyUnavailableError(ServiceError):
    """An optional dependency (e.g. the Foundry agent, ODBC driver) is missing.

    Used when the requested capability requires a component that is not
    installed or configured. Callers can degrade gracefully.
    """

    code = "dependency_unavailable"
    status = 503


class FabricAccessError(ServiceError):
    """The platform identity cannot access the requested Fabric resource.

    Typically raised by the provisioning preflight when the Managed Identity /
    service principal has not been enabled for Fabric APIs or has not been
    granted a role on the target workspace.
    """

    code = "fabric_access_denied"
    status = 403


class UpstreamError(ServiceError):
    """An upstream call (Fabric REST, SQL endpoint) failed unexpectedly."""

    code = "upstream_error"
    status = 502


class ThrottledError(ServiceError):
    """An upstream service is rate-limiting or blocking the caller (HTTP 429).

    Raised (or surfaced via an upstream cause) when Fabric returns
    ``RequestBlocked`` / 429. ``retry_after`` is the suggested cool-off in
    seconds, which the API layer echoes as a ``Retry-After`` response header.
    """

    code = "throttled"
    status = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        code: str | None = None,
        status: int | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, details=details)
        self.retry_after = retry_after
