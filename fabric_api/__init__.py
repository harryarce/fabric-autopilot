"""FastAPI backend for fabric-autopilot.

A thin, **stateless**, versioned REST API over the service layer. The API owns
no business logic of its own: every route resolves a service from the shared
:class:`fabric_services.ServiceContainer` and translates domain results /
errors into HTTP. Authentication to Fabric and SQL flows through Managed
Identity inside the services, so the API holds no secrets.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app():  # pragma: no cover - thin re-export
    from .main import create_app as _create_app

    return _create_app()
