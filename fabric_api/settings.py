"""Typed application settings, loaded from environment variables.

All configuration is environment-driven (12-factor) so the same image runs in
dev/test/prod with no code changes. Secrets are never part of settings — the
platform authenticates with Managed Identity.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration resolved from the environment."""

    model_config = SettingsConfigDict(env_prefix="FABRIC_", extra="ignore")

    # -- service metadata -------------------------------------------------
    api_title: str = "fabric-autopilot API"
    api_version: str = "v1"
    environment: str = "dev"  # dev | test | prod

    # -- CORS -------------------------------------------------------------
    # Comma-separated origins allowed to call the API (the Streamlit frontend).
    cors_allow_origins: str = "*"

    # -- artifact storage -------------------------------------------------
    # Mirrors app.artifacts.get_artifact_store() env contract.
    artifact_store: str = "local"  # local | blob
    blob_account_url: str = ""
    blob_container: str = "artifacts"

    # -- multi-tenancy ----------------------------------------------------
    default_tenant_id: str = "default"

    # -- agentic / Foundry (current connection mechanism) -----------------
    # These map onto the agent layer's FOUNDRY_* variables; surfaced here so
    # the API can report agent readiness. Empty means "use the baked-in
    # defaults from app.intelligence.agent".
    foundry_project_endpoint: str = ""
    foundry_model: str = ""
    foundry_agent_name: str = ""

    # -- observability ----------------------------------------------------
    applicationinsights_connection_string: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if raw in ("", "*"):
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
