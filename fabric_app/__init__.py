"""Decoupled Streamlit frontend package.

The frontend talks to the platform exclusively through the versioned REST API
(:mod:`fabric_api`) via :class:`fabric_app.api_client.FabricApiClient`. No
business logic, Fabric SDK, or SQL driver lives in this layer — it is a thin,
deployable presentation tier that can run in its own container with only the
API base URL as configuration.
"""

from __future__ import annotations

from .api_client import ApiError, FabricApiClient, get_api_client

__all__ = ["ApiError", "FabricApiClient", "get_api_client"]
