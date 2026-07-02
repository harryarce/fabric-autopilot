"""Configuration for the agentic orchestration layer.

The agent team is *configured by environment* so the same code runs locally
(deterministic fallback) and in Azure (Foundry-backed) without edits. Every
value has a safe default; nothing here reaches the network on import.

Two families of settings:

* **Foundry** — reuses the platform's existing ``FOUNDRY_*`` namespace so the
  agent team shares one project/model with the semantic-model architect.
* **Power BI Modeling MCP** — bridge to the official
  ``@microsoft/powerbi-modeling-mcp`` server (stdio or streamable-http). Enabled
  by default for live Tabular Object Model editing wherever the host can launch
  it (Node/``npx`` for stdio, or a reachable streamable-http endpoint). The
  default stdio launch + interactive auth is a local-dev surface; the Azure
  deployment sets ``POWERBI_MODELING_MCP_ENABLED=false`` (see
  ``infra/resources.bicep``) so the headless containers degrade cleanly instead
  of spawning a server they cannot authenticate.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

# Defaults mirror ``app.intelligence.agent``: both stay empty so the orchestration
# layer is dormant until the operator wires up ``FOUNDRY_PROJECT_ENDPOINT`` and
# ``FOUNDRY_MODEL``. Without them the team falls back to the deterministic
# pipeline (see README "Agentic capabilities").
DEFAULT_PROJECT_ENDPOINT = ""
DEFAULT_MODEL = ""
DEFAULT_MANAGER_NAME = "fabric-orchestrator"


def _env(name: str, default: str = "") -> str:
    """Return a non-empty env var, else ``default`` (treats "" as unset)."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


@dataclass
class PowerBiModelingMcpConfig:
    """Connection settings for the official Power BI Modeling MCP server.

    Activated by default so the natural-language model-editing surface is
    available wherever the host can launch the server (Node/``npx`` for stdio,
    or a reachable streamable-http endpoint). When the server cannot be
    launched the consumers degrade gracefully to a deterministic advisory.

    NOTE: the default stdio launch needs a Node runtime and the default
    ``interactive`` auth mode needs a browser, so it targets local dev. The
    Azure containers (no Node, headless) set ``POWERBI_MODELING_MCP_ENABLED=
    false`` in ``infra/resources.bicep`` to skip the doomed launch; point them
    at a hosted server via ``POWERBI_MODELING_MCP_TRANSPORT=http`` +
    ``POWERBI_MODELING_MCP_URL`` to re-enable it there.
    """

    enabled: bool = True
    # "stdio" launches the npx command locally; "http" connects to a URL.
    transport: str = "stdio"
    command: str = (
        "npx --silent -y @microsoft/powerbi-modeling-mcp@latest --start"
    )
    url: str = ""
    access_token: str = ""
    # Team-wide default stays safe (read-only); the model-chat surface opts into
    # read-write explicitly so live edits are always a deliberate choice.
    readonly: bool = True
    # Skip the MCP elicitation confirmation prompts. Required for headless,
    # server-side agents (there is no interactive client to approve edits); the
    # platform gates risky actions at its own layer instead.
    skip_confirmation: bool = True
    # Authentication mode forwarded to the server (`--authmode`). "interactive"
    # lets the server sign the local user in (and use the injected bearer token
    # when present); "serviceprincipal" uses AZURE_* credentials. Interactive is
    # the most reliable for local/dev where a workstation user owns the model.
    auth_mode: str = "interactive"
    # Headless operation for non-interactive hosts (the Azure containers). When
    # True the chat surface acquires a Power BI / XMLA bearer token from the
    # platform identity (managed identity in Azure) and injects it, instead of
    # relying on the server's interactive browser sign-in (impossible in a
    # container). Local dev leaves this False so the existing interactive path
    # is unchanged. Set ``POWERBI_MODELING_MCP_HEADLESS=true`` in the cloud.
    headless: bool = False

    @classmethod
    def from_env(cls) -> "PowerBiModelingMcpConfig":
        return cls(
            enabled=_flag("POWERBI_MODELING_MCP_ENABLED", True),
            transport=_env("POWERBI_MODELING_MCP_TRANSPORT", "stdio").lower(),
            command=_env(
                "POWERBI_MODELING_MCP_COMMAND",
                "npx --silent -y @microsoft/powerbi-modeling-mcp@latest --start",
            ),
            url=_env("POWERBI_MODELING_MCP_URL", ""),
            access_token=_env("PBI_MODELING_MCP_ACCESS_TOKEN", ""),
            readonly=_flag("POWERBI_MODELING_MCP_READONLY", True),
            skip_confirmation=_flag("POWERBI_MODELING_MCP_SKIP_CONFIRMATION", True),
            auth_mode=_env("POWERBI_MODELING_MCP_AUTHMODE", "interactive").lower(),
            headless=_flag("POWERBI_MODELING_MCP_HEADLESS", False),
        )

    @property
    def command_argv(self) -> list[str]:
        """The launch command split into argv for stdio transport."""
        return shlex.split(self.command)


@dataclass
class AgentTeamConfig:
    """Top-level configuration for the orchestration team."""

    project_endpoint: str = DEFAULT_PROJECT_ENDPOINT
    model: str = DEFAULT_MODEL
    manager_name: str = DEFAULT_MANAGER_NAME
    # Cap the number of coordinated steps so a runaway plan can't loop forever.
    max_steps: int = 8
    # When true, a manager agent narrates the run (best-effort, Foundry-backed).
    narrate: bool = True
    # Side-effecting operations (publish/update) require explicit approval.
    require_approval: bool = True
    powerbi_modeling_mcp: PowerBiModelingMcpConfig = field(
        default_factory=PowerBiModelingMcpConfig
    )

    @classmethod
    def from_env(cls) -> "AgentTeamConfig":
        return cls(
            project_endpoint=_env("FOUNDRY_PROJECT_ENDPOINT", DEFAULT_PROJECT_ENDPOINT),
            model=_env("FOUNDRY_MODEL", DEFAULT_MODEL),
            manager_name=_env("FABRIC_AGENT_MANAGER_NAME", DEFAULT_MANAGER_NAME),
            max_steps=int(_env("FABRIC_AGENT_MAX_STEPS", "8") or "8"),
            narrate=_flag("FABRIC_AGENT_NARRATE", True),
            require_approval=_flag("FABRIC_AGENT_REQUIRE_APPROVAL", True),
            powerbi_modeling_mcp=PowerBiModelingMcpConfig.from_env(),
        )
