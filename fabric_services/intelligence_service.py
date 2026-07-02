"""Agentic capability service.

Centralises the platform's *agent lifecycle* surface: reporting which
Foundry-backed agentic capabilities are available, and publishing the
semantic-model builder as a reusable hosted Foundry prompt agent.

This logic previously lived inline in the FastAPI ``agents`` router and was
duplicated in the MCP server's ``agent_status`` tool. Both surfaces now delegate
here so the capability is defined exactly once in the service layer, alongside
every other piece of core functionality.
"""

from __future__ import annotations

from app.intelligence import agent as agent_module
from app.intelligence.audit import report_agent_available
from app.intelligence.report_design_agent import report_design_available

from .errors import DependencyUnavailableError


class IntelligenceService:
    """Report and publish the platform's agentic (Foundry) capabilities."""

    def status(self) -> dict:
        """Return which agentic capabilities are available in this deployment."""
        config = agent_module.IntelligenceConfig.from_env()
        return {
            "semanticModelDesign": agent_module.is_available(),
            "reportDesign": report_design_available(),
            "reportAudit": report_agent_available(),
            "foundry": {
                "projectEndpoint": config.project_endpoint,
                "model": config.model,
                "agentName": config.agent_name,
            },
        }

    def publish_remote_semantic_model_agent(
        self, description: str | None = None
    ) -> dict:
        """Publish the semantic-model-builder as a hosted Foundry prompt agent.

        Enables reuse of the design capability by other agents/apps. Requires the
        optional agent dependencies and a reachable Foundry project.

        Raises:
            DependencyUnavailableError: when the Microsoft Agent Framework is not
                installed in this deployment.
        """
        if not agent_module.is_available():
            raise DependencyUnavailableError(
                "The Microsoft Agent Framework is not installed in this "
                "deployment. Install the 'agents' extra and configure "
                "FOUNDRY_PROJECT_ENDPOINT.",
            )
        return agent_module.SemanticModelIntelligence().publish_remote_agent_sync(
            description
        )
