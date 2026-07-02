"""The specialized agent team.

A small roster of Foundry-backed agents, each with a focused remit, the
platform's read/analysis tools, the team skills, and (optionally) the live
Power BI Modeling MCP tools. The orchestrator coordinates them; this module
just *builds* them.

The whole module is import-safe without the Agent Framework: the heavy imports
happen inside functions and :func:`agents_available` reports readiness so the
orchestrator can choose the deterministic path instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from .config import AgentTeamConfig
from .mcp_clients import build_powerbi_modeling_tool
from .skills import load_skills_provider
from .tools import ToolKit, build_agent_tools


def agents_available() -> bool:
    """True when the Microsoft Agent Framework + Foundry client are importable."""
    try:
        import agent_framework  # noqa: F401
        from agent_framework.foundry import FoundryChatClient  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    return True


# Per-role system instructions. Kept terse — the bundled skills carry the
# detailed playbooks the agents consult on demand.
_ROLES: dict[str, dict[str, str]] = {
    "architect": {
        "name": "semantic-model-architect",
        "description": "Designs Power BI / Fabric semantic models from SQL schemas.",
        "instructions": (
            "You are a senior Power BI semantic-model architect. Turn SQL "
            "schemas into clean star-schema models: clear fact/dimension roles, "
            "well-named tables/columns, hidden keys, friendly measures with "
            "format strings, and complete descriptions. Use the extract_schema "
            "and design_semantic_model tools. Always load and follow the "
            "`powerbi-modeling-mcp-bridge` skill and the Microsoft Fabric "
            "guidance in its references (modeling, DAX, naming conventions, "
            "semantic-model AI-readiness and Direct Lake)."
        ),
    },
    "dax": {
        "name": "dax-specialist",
        "description": "Authors and reviews DAX measures for correctness and clarity.",
        "instructions": (
            "You are a DAX specialist. Produce correct, performant, well-"
            "formatted measures with format strings and short rationales. Use "
            "generate_dax_measure to draft, then refine. Avoid implicit "
            "measures; prefer explicit, reusable patterns. Follow the DAX and "
            "naming guidance in the `powerbi-modeling-mcp-bridge` skill's "
            "references (msfabric-dax-guidelines, msfabric-naming-conventions), "
            "and apply the performance guidance (msfabric-dax-perf-decision-"
            "guide, msfabric-dax-perf-patterns) for measures on hot paths."
        ),
    },
    "auditor": {
        "name": "model-auditor",
        "description": "Audits models and reports for quality and Copilot readiness.",
        "instructions": (
            "You are a quality auditor. Run audit_semantic_model and "
            "audit_report, summarise the most important findings, and propose "
            "safe, additive fixes. Be specific and actionable. Judge models "
            "against the `powerbi-modeling-mcp-bridge` skill (including its DAX "
            "performance references) and reports against the "
            "`fabric-report-design` skill's pre-flight checklist, accessibility "
            "and anti-patterns references."
        ),
    },
    "report": {
        "name": "report-designer",
        "description": "Designs accessible, on-brand reports grounded in a model.",
        "instructions": (
            "You are a report designer. Use suggest_report to draft a starter "
            "report grounded in the semantic model, then audit_report to verify "
            "brand and accessibility. Recommend layout and visual improvements. "
            "Always load and follow the `fabric-report-design` skill and the "
            "Microsoft Fabric guidance in its references: the page archetypes "
            "and archetype-composition, the visual cookbook and chart "
            "selection, layout, typography, colour, interactivity, "
            "accessibility, conditional formatting and anti-patterns."
        ),
    },
}


@dataclass
class AgentTeam:
    """A built team plus its shared toolkit and run context."""

    toolkit: ToolKit
    config: AgentTeamConfig
    agents: dict[str, Any]
    mcp_tool: Any | None = None


def build_team(
    container: ServiceContainer,
    tenant: TenantContext,
    config: AgentTeamConfig | None = None,
) -> AgentTeam:
    """Construct the specialized agent team (requires the Agent Framework).

    Raises:
        RuntimeError: when the Agent Framework / Foundry client is unavailable.
    """
    if not agents_available():
        raise RuntimeError(
            "The Microsoft Agent Framework is not installed. Install the "
            "'agents' extra and configure FOUNDRY_PROJECT_ENDPOINT to enable "
            "the agent team."
        )
    config = config or AgentTeamConfig.from_env()
    toolkit = ToolKit(container, tenant)

    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential
    import os

    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    credential = DefaultAzureCredential(
        managed_identity_client_id=client_id,
        exclude_interactive_browser_credential=True,
    )
    chat_client = FoundryChatClient(
        project_endpoint=config.project_endpoint,
        model=config.model,
        credential=credential,
    )

    skills_provider = load_skills_provider()
    context_providers = [skills_provider] if skills_provider else None
    tools = build_agent_tools(toolkit)
    mcp_tool = build_powerbi_modeling_tool(config.powerbi_modeling_mcp)
    role_tools = list(tools)
    if mcp_tool is not None:
        role_tools.append(mcp_tool)

    agents: dict[str, Any] = {}
    for role, spec in _ROLES.items():
        agents[role] = Agent(
            name=spec["name"],
            client=chat_client,
            instructions=spec["instructions"],
            description=spec["description"],
            tools=role_tools,
            context_providers=context_providers,
        )
    return AgentTeam(toolkit=toolkit, config=config, agents=agents, mcp_tool=mcp_tool)
