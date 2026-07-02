"""Agentic routes — Foundry agent status and hosted-agent publishing.

The platform prioritizes agentic capabilities. Agent-assisted *design*,
*suggestion*, and *audit* are reachable through the semantic-model, report, and
audit routes (all agent-first by default). This router exposes the agent
**lifecycle** surface: probing readiness and publishing a reusable *hosted*
Foundry prompt agent so the same model-design capability can be consumed by
other agents/apps outside this platform.
"""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from fabric_services import ServiceContainer
from fabric_services.context import TenantContext
from fabric_services.errors import NotFoundError

from ..agents import (
    AgentOrchestrator,
    ModelingChatService,
    OrchestrationRequest,
    capability_status,
    get_workflow_store,
    resolve_workflow,
)
from ..dependencies import get_container, get_tenant
from ..models import (
    ModelChatRequest,
    OrchestrateRequest,
    PublishModelReportWorkflowRequest,
    PublishModelWorkflowRequest,
    PublishRemoteAgentRequest,
    PublishReportWorkflowRequest,
    WorkflowDecisionRequest,
)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/status")
def agent_status(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Report which agentic capabilities are available in this deployment."""
    return container.intelligence_service().status()


@router.get("/team")
def agent_team_status() -> dict:
    """Report the orchestration team: roles, skills, mode, and PBI MCP bridge."""
    return capability_status()


@router.post("/model-chat")
def model_chat(body: ModelChatRequest) -> dict:
    """Run one natural-language editing turn against an existing model.

    Connects to the chosen workspace/semantic model through the official Power
    BI Modeling MCP server and applies the requested change. Agent-first with a
    deterministic advisory fallback when the live agent/MCP server is
    unavailable, so the surface is always usable.
    """
    return ModelingChatService().chat(
        workspace_name=body.workspace_name,
        model_name=body.model_name,
        message=body.message,
        history=body.history,
        workspace_id=body.workspace_id,
        model_id=body.model_id,
    )


@router.post("/orchestrate")
def orchestrate(
    body: OrchestrateRequest,
    stream: bool = Query(
        False, description="Stream Server-Sent Events instead of a single JSON result."
    ),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
):
    """Run the agentic model + report authoring pipeline.

    Agent-first with deterministic fallback: the pipeline always returns a
    usable transcript and artifacts. With ``stream=true`` the same events are
    delivered as Server-Sent Events for live progress in the UI.
    """
    orchestrator = AgentOrchestrator(container, tenant)
    request = OrchestrationRequest.from_dict(body.model_dump())

    if stream:
        def _sse() -> Iterator[str]:
            for event in orchestrator.stream(request):
                yield f"data: {json.dumps(event.to_dict())}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    return orchestrator.run(request).to_dict()


@router.get("/workflows")
def list_workflows(
    tenant: TenantContext = Depends(get_tenant),
) -> list[dict]:
    """List pending/resolved publish workflows for the current tenant."""
    return [w.to_dict() for w in get_workflow_store().list(tenant=tenant)]


@router.post("/workflows/publish-model")
def create_publish_model_workflow(
    body: PublishModelWorkflowRequest,
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Create an approval-gated semantic-model publish workflow."""
    workflow = get_workflow_store().create(
        "publish-model",
        tenant,
        {
            "workspace_id": body.workspace_id,
            "display_name": body.display_name,
            "spec": body.spec,
            "fmt": body.format,
            "description": body.description,
        },
    )
    return workflow.to_dict()


@router.post("/workflows/publish-report")
def create_publish_report_workflow(
    body: PublishReportWorkflowRequest,
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Create an approval-gated report publish workflow."""
    workflow = get_workflow_store().create(
        "publish-report",
        tenant,
        {
            "workspace_id": body.workspace_id,
            "display_name": body.display_name,
            "spec": body.spec,
            "description": body.description,
            "dataset_id": body.dataset_id,
        },
    )
    return workflow.to_dict()


@router.post("/workflows/publish-model-report")
def create_publish_model_report_workflow(
    body: PublishModelReportWorkflowRequest,
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Create one approval-gated workflow that publishes a model and its report.

    Approval publishes the semantic model first, then binds and publishes the
    report to it — a single human decision covers both side-effecting writes.
    """
    workflow = get_workflow_store().create(
        "publish-model-report",
        tenant,
        {
            "workspace_id": body.workspace_id,
            "model_display_name": body.model_display_name,
            "model_spec": body.model_spec,
            "report_display_name": body.report_display_name,
            "report_spec": body.report_spec,
            "fmt": body.format,
            "description": body.description,
        },
    )
    return workflow.to_dict()


@router.get("/workflows/{workflow_id}")
def get_workflow(
    workflow_id: str,
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Return a single publish workflow by id."""
    workflow = get_workflow_store().get(workflow_id)
    if workflow is None or workflow.tenant_id != tenant.tenant_id:
        raise NotFoundError(f"Workflow '{workflow_id}' was not found.")
    return workflow.to_dict()


@router.post("/workflows/{workflow_id}/decision")
def decide_workflow(
    workflow_id: str,
    body: WorkflowDecisionRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Approve (publish) or reject a pending publish workflow."""
    workflow = get_workflow_store().get(workflow_id)
    if workflow is None or workflow.tenant_id != tenant.tenant_id:
        raise NotFoundError(f"Workflow '{workflow_id}' was not found.")
    resolved = resolve_workflow(
        workflow, approve=body.approve, container=container, tenant=tenant
    )
    return resolved.to_dict()


@router.post("/semantic-model/publish-remote")
def publish_remote_agent(
    body: PublishRemoteAgentRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Publish the semantic-model-builder as a hosted Foundry prompt agent.

    Enables reuse of the design capability by other agents/apps. Requires the
    optional agent dependencies and a reachable Foundry project.
    """
    return container.intelligence_service().publish_remote_semantic_model_agent(
        body.description
    )
