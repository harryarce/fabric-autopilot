"""Bridge to the official Power BI Modeling MCP server.

The platform's own service layer covers schema → spec → TMDL/TMSL → publish.
For *live* Tabular Object Model editing (measures, relationships, calculation
groups, RLS) against a running model, Microsoft ships
``@microsoft/powerbi-modeling-mcp``. When enabled, this module hands the agent
team an Agent Framework MCP tool wired to that server so the same agents can
operate directly on a model in addition to the platform's own tools.

The connection is **opt-in and lazy**: disabled by default, constructed only
when :class:`~fabric_api.agents.config.PowerBiModelingMcpConfig.enabled` is set,
and any import/launch failure degrades to ``None`` (the team simply runs without
the live-modeling tools).
"""

from __future__ import annotations

import os
from typing import Any

from .config import PowerBiModelingMcpConfig

# Service-principal / user credential variables that the .NET server's Azure
# Identity chain (``EnvironmentCredential``) will pick up automatically. On a
# workstation these frequently belong to an unrelated app registration in a
# different tenant with NO Power BI access; if inherited by the server they make
# ``ConnectFabric`` fail with "Authentication failed for all authenticators",
# overriding both the injected bearer token and interactive sign-in. They are
# stripped from the subprocess environment so the server authenticates cleanly.
_AZURE_CREDENTIAL_ENV_VARS = (
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_CLIENT_CERTIFICATE_PASSWORD",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
)

# Managed-identity / Azure Arc discovery variables. The .NET server's
# ``DefaultAzureCredential`` finds a managed identity through THESE (not the
# ``AZURE_*`` vars above): App Service / Functions / Service Fabric expose
# ``IDENTITY_ENDPOINT``/``IDENTITY_HEADER``, Azure Arc adds ``IMDS_ENDPOINT``,
# Cloud Shell uses ``MSI_ENDPOINT``/``MSI_SECRET``, and AKS pod identity uses
# ``AZURE_POD_IDENTITY_AUTHORITY_HOST``. On a workstation an ambient Azure Arc
# managed identity often belongs to a principal with NO Power BI access and is
# flaky; if the server reaches for it, ``ConnectFabric`` fails intermittently
# with "Authentication failed for all authenticators" even though a valid bearer
# token was injected. When we hand the server an explicit token these are
# stripped so it authenticates with that token and nothing else.
_MANAGED_IDENTITY_ENV_VARS = (
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IDENTITY_SERVER_THUMBPRINT",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
    "AZURE_POD_IDENTITY_AUTHORITY_HOST",
)


def build_powerbi_modeling_tool(
    config: PowerBiModelingMcpConfig,
    *,
    readonly: bool | None = None,
    skip_confirmation: bool | None = None,
    access_token: str | None = None,
    auth_mode: str | None = None,
    name: str = "powerbi-modeling",
) -> Any | None:
    """Return an Agent Framework MCP tool for the PBI Modeling server, or None.

    * ``transport="stdio"`` launches the configured ``npx`` command locally.
    * ``transport="http"`` connects to a streamable-http endpoint (``url``).

    ``readonly`` / ``skip_confirmation`` override the config defaults so a
    specific caller (e.g. the natural-language model-editing surface) can opt
    into read-write, headless operation while the team-wide default stays safe.

    ``access_token`` overrides ``config.access_token``. A Power BI / XMLA bearer
    token lets the **headless** server open the model connection without an
    interactive sign-in: it is forwarded as an ``Authorization`` header for the
    http transport, and exported as ``PBI_MODELING_MCP_ACCESS_TOKEN`` in the
    stdio subprocess environment.

    ``auth_mode`` overrides ``config.auth_mode`` and is forwarded to the server
    as ``--authmode`` (e.g. ``interactive``). For the stdio transport the
    subprocess environment is also sanitized of ambient ``AZURE_*`` credential
    variables (see ``_AZURE_CREDENTIAL_ENV_VARS``) so they cannot hijack the
    server's authentication.
    """
    if not config.enabled:
        return None
    read_only = config.readonly if readonly is None else readonly
    skip = config.skip_confirmation if skip_confirmation is None else skip_confirmation
    token = access_token if access_token is not None else config.access_token
    mode = (auth_mode if auth_mode is not None else config.auth_mode) or ""
    mode = mode.strip()
    try:
        from agent_framework import MCPStdioTool, MCPStreamableHTTPTool
    except Exception:  # noqa: BLE001 - optional dependency
        return None

    try:
        if config.transport == "http":
            if not config.url:
                return None
            headers = (
                {"Authorization": f"Bearer {token}"} if token else None
            )
            return MCPStreamableHTTPTool(
                name=name,
                url=config.url,
                headers=headers,
            )
        argv = list(config.command_argv)
        # Apply the read/write mode and (optionally) suppress the interactive
        # elicitation prompts the server raises before its first edit/query.
        if read_only:
            if "--readonly" not in argv:
                argv.append("--readonly")
        elif "--readwrite" not in argv:
            argv.append("--readwrite")
        if skip and "--skipconfirmation" not in argv:
            argv.append("--skipconfirmation")
        if mode and not any(a.startswith("--authmode") for a in argv):
            argv.append(f"--authmode={mode}")
        if not argv:
            return None
        # Start from a *sanitized* copy of the environment: drop the ambient
        # AZURE_* credential variables that would otherwise hijack the server's
        # own auth chain (see ``_AZURE_CREDENTIAL_ENV_VARS``). Then, in headless
        # mode, add the bearer token the server uses to open the XMLA
        # connection without an interactive sign-in.
        stripped = set(_AZURE_CREDENTIAL_ENV_VARS)
        if token:
            # With an explicit token, also hide managed-identity / Azure Arc
            # discovery so the server authenticates with the token alone and
            # cannot fall back to a flaky / wrong-identity managed identity.
            stripped.update(_MANAGED_IDENTITY_ENV_VARS)
        env: dict[str, str] = {
            k: v for k, v in os.environ.items() if k not in stripped
        }
        if token:
            env["PBI_MODELING_MCP_ACCESS_TOKEN"] = token
        return MCPStdioTool(
            name=name,
            command=argv[0],
            args=argv[1:],
            env=env,
        )
    except Exception:  # noqa: BLE001 - never let the bridge break a run
        return None
