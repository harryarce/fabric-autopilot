"""Agentic orchestration layer for fabric-autopilot.

This package coordinates a team of Microsoft Agent Framework agents over the
existing service layer to deliver end-to-end, *agent-first* model and report
authoring — with deterministic fallback so every capability works with or
without Foundry.

It is deliberately **import-light** (no FastAPI, no network on import) so it can
be shared by every surface: the FastAPI router (:mod:`fabric_api.routers.agents`),
the MCP server (:mod:`fabric_mcp.server`), and — via the API — the Streamlit UI.

Public surface:

* :class:`AgentOrchestrator` / :class:`OrchestrationRequest` — run the pipeline.
* :func:`capability_status` — what's available in this deployment.
* publish workflows — approval-gated, side-effecting Fabric writes.
"""

from __future__ import annotations

from typing import Any

from .config import AgentTeamConfig, PowerBiModelingMcpConfig
from .events import AgentEvent, OrchestrationResult
from .modeling_chat import ModelingChatResult, ModelingChatService
from .orchestrator import AgentOrchestrator, OrchestrationRequest
from .skills import skills_dir
from .team import AgentTeam, agents_available, build_team
from .tools import ToolKit, build_agent_tools
from .workflows import (
    PublishWorkflow,
    PublishWorkflowStore,
    get_workflow_store,
    resolve_workflow,
)

__all__ = [
    "AgentTeamConfig",
    "PowerBiModelingMcpConfig",
    "AgentEvent",
    "OrchestrationResult",
    "AgentOrchestrator",
    "OrchestrationRequest",
    "ModelingChatService",
    "ModelingChatResult",
    "AgentTeam",
    "agents_available",
    "build_team",
    "ToolKit",
    "build_agent_tools",
    "PublishWorkflow",
    "PublishWorkflowStore",
    "get_workflow_store",
    "resolve_workflow",
    "capability_status",
]


def capability_status(config: AgentTeamConfig | None = None) -> dict[str, Any]:
    """Report which agentic capabilities are available in this deployment.

    Safe to call anywhere — performs no network I/O. Used by the API
    ``/agents/team`` route, the MCP ``agent_team_status`` tool, and the UI.
    """
    config = config or AgentTeamConfig.from_env()
    available = agents_available()
    skills_path = skills_dir()
    skill_names: list[str] = []
    if skills_path.exists():
        skill_names = sorted(p.name for p in skills_path.iterdir() if p.is_dir())
    return {
        "agentFramework": available,
        "mode": "foundry" if available else "deterministic",
        "manager": config.manager_name,
        "roles": ["architect", "dax", "auditor", "report"],
        "skills": skill_names,
        "requireApproval": config.require_approval,
        "foundry": {
            "projectEndpoint": config.project_endpoint,
            "model": config.model,
        },
        "powerBiModelingMcp": {
            "enabled": config.powerbi_modeling_mcp.enabled,
            "transport": config.powerbi_modeling_mcp.transport,
            "readonly": config.powerbi_modeling_mcp.readonly,
        },
    }
