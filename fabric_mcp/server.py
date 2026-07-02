"""MCP server exposing the Fabric platform to AI agents.

This is a **primary, agent-first surface**: every capability of the service
layer (schema extraction, agentic model *design*, audit, report *suggestion*,
and publish) is exposed as a typed MCP tool. Tools call
:mod:`fabric_services` **directly** (in-process) rather than over HTTP, so an
agent host gets the full platform with no extra network hop.

Authentication uses the same Managed Identity / ``DefaultAzureCredential`` path
as the rest of the platform; agentic design/suggestion/audit run Foundry-first
with deterministic fallback, identical to the REST API.

Run with::

    python -m fabric_mcp            # stdio transport (default)

The shared :class:`fabric_services.ServiceContainer` is built lazily on first
tool use so importing this module never touches the network.
"""

from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.intelligence import ReportSpec, SemanticModelSpec, SuggestionSpec
from fabric_services import ServiceContainer, build_container
from fabric_services.context import TenantContext

from fabric_api.agents import (
    AgentOrchestrator,
    OrchestrationRequest,
    ToolKit,
    capability_status,
)

from .serialize import to_jsonable

mcp = FastMCP(
    "fabric-saas",
    instructions=(
        "Tools for Microsoft Fabric semantic models and reports. Typical agent "
        "workflow: list_workspaces -> list_sql_endpoints -> extract_schema -> "
        "design_semantic_model (agentic) -> audit_semantic_model -> "
        "publish_semantic_model -> suggest_report -> publish_report. Specs "
        "returned by design/suggest tools are passed verbatim into build/audit/"
        "publish tools."
    ),
)

_container: ServiceContainer | None = None


def get_container() -> ServiceContainer:
    """Build (once) and return the shared service container."""
    global _container
    if _container is None:
        _container = build_container()
    return _container


def _tenant() -> TenantContext:
    return TenantContext.default()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@mcp.tool()
def list_workspaces() -> list[dict]:
    """List all Fabric workspaces visible to the platform identity."""
    return [to_jsonable(w) for w in get_container().provisioning_service().list_workspaces()]


@mcp.tool()
def check_fabric_access() -> dict:
    """Verify the platform identity can reach Fabric; returns onboarding hints."""
    status = get_container().provisioning_service().check_fabric_access()
    return to_jsonable(status)


@mcp.tool()
def list_sql_endpoints(workspace_id: str) -> list[dict]:
    """List SQL analytics endpoints / warehouses in a workspace."""
    return [to_jsonable(e) for e in get_container().schema_service().list_sql_endpoints(workspace_id)]


@mcp.tool()
def list_lakehouses(workspace_id: str) -> list[dict]:
    """List lakehouses in a workspace."""
    return [to_jsonable(lh) for lh in get_container().schema_service().list_lakehouses(workspace_id)]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@mcp.tool()
def extract_schema(server: str, database: str) -> list[dict]:
    """Extract table/column/key metadata from a SQL endpoint (server + database)."""
    return [to_jsonable(s) for s in get_container().schema_service().extract_schemas(server, database)]


# ---------------------------------------------------------------------------
# Semantic models (agent-first design)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_semantic_models(workspace_id: str) -> list[dict]:
    """List semantic models in a workspace."""
    svc = get_container().model_service(_tenant())
    return [to_jsonable(m) for m in svc.list_models(workspace_id)]


@mcp.tool()
def design_semantic_model(
    server: str,
    database: str,
    model_name: str,
    storage_mode: str = "import",
    source_kind: str = "sql",
    use_agent: bool = True,
    extra_instructions: Optional[str] = None,
) -> dict:
    """Design a semantic model from a SQL schema (Foundry agent-first).

    Extracts the schema, then designs a model — using the Foundry agent when
    available (``use_agent``), with a deterministic mapping as fallback. Returns
    ``{"spec": <SemanticModelSpec>, "usedAgent": bool}``. Pass ``spec`` to
    ``audit_semantic_model`` or ``publish_semantic_model``.
    """
    container = get_container()
    schemas = container.schema_service().extract_schemas(server, database)
    result = container.model_service(_tenant()).design_model(
        schemas,
        model_name=model_name,
        source_server=server,
        source_database=database,
        storage_mode=storage_mode,
        source_kind=source_kind,
        use_agent=use_agent,
        extra_instructions=extra_instructions,
    )
    return {"spec": to_jsonable(result.spec), "usedAgent": result.used_agent}


@mcp.tool()
def build_model_definition(spec: dict, fmt: str = "TMDL") -> dict:
    """Render a semantic-model spec to a TMDL/TMSL definition (files + parts)."""
    model_spec = SemanticModelSpec.from_dict(spec)
    definition = get_container().model_service(_tenant()).build_definition(model_spec, fmt=fmt)
    return {"format": definition.format.value, "files": definition.files}


@mcp.tool()
def audit_semantic_model(
    spec: dict, include_bpa: bool = False, features: Optional[list[str]] = None
) -> dict:
    """Audit a semantic-model spec; returns per-feature + merged health reports."""
    model_spec = SemanticModelSpec.from_dict(spec)
    reports = get_container().audit_service().audit_semantic_model(
        model_spec, features=features, include_bpa=include_bpa
    )
    return {feature: to_jsonable(report) for feature, report in reports.items()}


@mcp.tool()
def publish_semantic_model(
    workspace_id: str,
    display_name: str,
    spec: dict,
    fmt: str = "TMDL",
    description: Optional[str] = None,
) -> dict:
    """Create a semantic model in Fabric from a spec. Returns the created item."""
    model_spec = SemanticModelSpec.from_dict(spec)
    created = get_container().model_service(_tenant()).publish_model(
        workspace_id, display_name, model_spec, fmt=fmt, description=description
    )
    return to_jsonable(created)


@mcp.tool()
def update_semantic_model(
    workspace_id: str,
    model_id: str,
    spec: dict,
    fmt: str = "TMDL",
    item_name: str = "",
) -> dict:
    """Write a spec back to a published model via Fabric ``updateDefinition``.

    The Fabric LRO is polled to completion, so the returned ``status`` is
    terminal. The freshly written TMDL/TMSL is also persisted to the artifact
    store under the current tenant.
    """
    model_spec = SemanticModelSpec.from_dict(spec)
    return get_container().model_service(_tenant()).update_model(
        workspace_id, model_id, model_spec, fmt=fmt, item_name=item_name
    )


@mcp.tool()
def propose_model_usability_fixes(spec: dict) -> dict:
    """Generate deterministic usability write-back suggestions for a model spec."""
    model_spec = SemanticModelSpec.from_dict(spec)
    suggestions = get_container().model_service(_tenant()).propose_usability_fixes(model_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@mcp.tool()
def propose_model_copilot_fixes(spec: dict) -> dict:
    """Generate Copilot-readiness write-back suggestions for a model spec."""
    model_spec = SemanticModelSpec.from_dict(spec)
    suggestions = get_container().model_service(_tenant()).propose_copilot_prep(model_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@mcp.tool()
def apply_model_audit_suggestions(spec: dict, suggestions: list[dict]) -> dict:
    """Apply accepted audit suggestions to a model spec; returns the updated spec.

    Only suggestions whose ``status`` is ``"accepted"`` are applied; all others
    are passed through. Applied suggestions are flipped to ``"applied"``;
    suggestions targeting missing objects become ``"failed"``.
    """
    model_spec = SemanticModelSpec.from_dict(spec)
    parsed = [SuggestionSpec.from_dict(s) for s in suggestions]
    updated_spec, updated = get_container().model_service(
        _tenant()
    ).apply_audit_suggestions(model_spec, parsed)
    return {
        "spec": to_jsonable(updated_spec),
        "suggestions": [s.to_dict() for s in updated],
    }


# ---------------------------------------------------------------------------
# Reports (agent-first suggestion)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_reports(workspace_id: str) -> list[dict]:
    """List reports in a workspace."""
    svc = get_container().report_service(_tenant())
    return [to_jsonable(r) for r in svc.list_reports(workspace_id)]


@mcp.tool()
def suggest_report(
    model: dict,
    report_name: Optional[str] = None,
    dataset_id: Optional[str] = None,
    dataset_name: Optional[str] = None,
    use_agent: bool = True,
    extra_instructions: Optional[str] = None,
) -> dict:
    """Generate a starter report grounded in a semantic model (agent-first).

    ``model`` is a SemanticModelSpec (e.g. from ``design_semantic_model``).
    Returns ``{"spec": <ReportSpec>, "status", "usedAgent", ...}``. Pass ``spec``
    to ``audit_report`` or ``publish_report``.
    """
    model_spec = SemanticModelSpec.from_dict(model)
    result = get_container().report_service(_tenant()).suggest_report(
        model_spec,
        report_name=report_name,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        use_agent=use_agent,
        extra_instructions=extra_instructions,
    )
    return {
        "spec": to_jsonable(result.spec),
        "status": result.status,
        "usedAgent": result.used_agent,
        "agentVisualCount": result.agent_visual_count,
        "error": result.error,
    }


@mcp.tool()
def audit_report(spec: dict, use_agent: bool = True) -> dict:
    """Audit a report spec — agent-enriched by default, deterministic fallback."""
    report_spec = ReportSpec.from_dict(spec)
    result = get_container().audit_service().audit_report(report_spec, use_agent=use_agent)
    return {
        "report": to_jsonable(result.report),
        "status": result.status,
        "agentFindingCount": result.agent_finding_count,
        "error": result.error,
    }


@mcp.tool()
def publish_report(
    workspace_id: str,
    display_name: str,
    spec: dict,
    description: Optional[str] = None,
) -> dict:
    """Create a report in Fabric from a spec. Returns the created item."""
    report_spec = ReportSpec.from_dict(spec)
    created = get_container().report_service(_tenant()).publish_report(
        workspace_id, display_name, report_spec, description=description
    )
    return to_jsonable(created)


@mcp.tool()
def update_report(
    workspace_id: str,
    report_id: str,
    spec: dict,
    item_name: str = "",
) -> dict:
    """Write a spec back to a published report via Fabric ``updateDefinition``."""
    report_spec = ReportSpec.from_dict(spec)
    return get_container().report_service(_tenant()).update_report(
        workspace_id, report_id, report_spec, item_name=item_name
    )


@mcp.tool()
def propose_report_theme_fixes(spec: dict) -> dict:
    """Generate deterministic brand/WCAG theme remediation suggestions."""
    report_spec = ReportSpec.from_dict(spec)
    suggestions = get_container().report_service(_tenant()).generate_theme_remediation(report_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@mcp.tool()
def apply_report_audit_suggestions(spec: dict, suggestions: list[dict]) -> dict:
    """Apply accepted theme/formatting suggestions to a report spec."""
    report_spec = ReportSpec.from_dict(spec)
    parsed = [SuggestionSpec.from_dict(s) for s in suggestions]
    updated_spec, updated = get_container().report_service(
        _tenant()
    ).apply_audit_suggestions(report_spec, parsed)
    return {
        "spec": to_jsonable(updated_spec),
        "suggestions": [s.to_dict() for s in updated],
    }


@mcp.tool()
def generate_dax_measure(
    spec: dict, intent: str, sample_rows: Optional[list[dict]] = None
) -> dict:
    """Generate a candidate DAX measure for a natural-language intent.

    Returns ``{table, measure: {name, expression, format_string, ...},
    rationale, intent_kind, confidence}``. Deterministic — safe to call
    without Foundry.
    """
    model_spec = SemanticModelSpec.from_dict(spec)
    generated = get_container().model_service(_tenant()).generate_dax_measure(
        model_spec, intent, sample_rows=sample_rows or None
    )
    return generated.to_dict()


# ---------------------------------------------------------------------------
# Artifacts & agent status
# ---------------------------------------------------------------------------


@mcp.tool()
def list_artifacts(kind: Optional[str] = None) -> list[dict]:
    """List stored artifacts for the current tenant (kind: semanticModels|reports)."""
    items = get_container().artifact_service(_tenant()).list_items(kind=kind)  # type: ignore[arg-type]
    return [to_jsonable(ref) for ref in items]


@mcp.tool()
def agent_status() -> dict:
    """Report which agentic (Foundry) capabilities are available in this deployment."""
    return get_container().intelligence_service().status()


# ---------------------------------------------------------------------------
# Agentic orchestration (multi-agent team)
# ---------------------------------------------------------------------------


@mcp.tool()
def agent_team_status() -> dict:
    """Report the orchestration team: roles, skills, mode, and PBI MCP bridge."""
    return capability_status()


@mcp.tool()
def run_agent_team(
    objective: str = "",
    server: Optional[str] = None,
    database: Optional[str] = None,
    model_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
    storage_mode: str = "import",
    source_kind: str = "sql",
    include_report: bool = True,
    use_agent: bool = True,
    extra_instructions: Optional[str] = None,
) -> dict:
    """Run the end-to-end agentic pipeline (schema -> model -> audit -> report).

    Agent-first with deterministic fallback: always returns a usable transcript
    plus artifacts (``model``, ``modelAudit``, ``report``, ``reportAudit``).
    Provide ``server``/``database``/``model_name`` to run the grounded pipeline;
    omit them for an advisory plan over ``objective``. This tool never publishes
    — use ``design_and_publish_model`` (with ``auto_approve``) for that.
    """
    orchestrator = AgentOrchestrator(get_container(), _tenant())
    request = OrchestrationRequest(
        objective=objective,
        server=server,
        database=database,
        model_name=model_name,
        workspace_id=workspace_id,
        storage_mode=storage_mode,
        source_kind=source_kind,
        include_report=include_report,
        use_agent=use_agent,
        extra_instructions=extra_instructions,
    )
    return orchestrator.run(request).to_dict()


@mcp.tool()
def design_and_publish_model(
    server: str,
    database: str,
    model_name: str,
    workspace_id: str,
    display_name: str = "",
    fmt: str = "TMDL",
    storage_mode: str = "import",
    source_kind: str = "sql",
    extra_instructions: Optional[str] = None,
    auto_approve: bool = False,
) -> dict:
    """Design + audit a model, then publish it when ``auto_approve`` is set.

    Runs the orchestration pipeline (report step skipped), then — only if
    ``auto_approve`` is True — performs the side-effecting Fabric publish via the
    service layer. Without ``auto_approve`` the model is returned for review and
    nothing is written (the safe default).
    """
    container = get_container()
    tenant = _tenant()
    orchestrator = AgentOrchestrator(container, tenant)
    result = orchestrator.run(
        OrchestrationRequest(
            objective=f"Design and publish model '{model_name}'",
            server=server,
            database=database,
            model_name=model_name,
            workspace_id=workspace_id,
            storage_mode=storage_mode,
            source_kind=source_kind,
            include_report=False,
            extra_instructions=extra_instructions,
        )
    )
    out = result.to_dict()
    model_spec = result.artifacts.get("model")
    if not auto_approve or model_spec is None:
        out["published"] = None
        out["approvalRequired"] = True
        return out
    created = ToolKit(container, tenant).publish_model(
        workspace_id=workspace_id,
        display_name=display_name or model_name,
        spec=model_spec,
        fmt=fmt,
    )
    out["published"] = created
    out["approvalRequired"] = False
    return out


@mcp.tool()
def improve_report(spec: dict) -> dict:
    """Audit a report and apply safe brand/accessibility theme fixes.

    Returns ``{"spec": <improved ReportSpec>, "auditBefore", "auditAfter",
    "applied": [...]}``. Deterministic and non-publishing — safe to call freely.
    """
    toolkit = ToolKit(get_container(), _tenant())
    audit_before = toolkit.audit_report(spec)
    proposed = toolkit.propose_report_theme_fixes(spec)["suggestions"]
    for suggestion in proposed:
        suggestion["status"] = "accepted"
    applied = toolkit.apply_report_suggestions(spec, proposed)
    audit_after = toolkit.audit_report(applied["spec"])
    return {
        "spec": applied["spec"],
        "auditBefore": audit_before,
        "auditAfter": audit_after,
        "applied": applied["suggestions"],
    }


# ---------------------------------------------------------------------------
# Lifecycle (Git + Deployment Pipelines)
# ---------------------------------------------------------------------------


@mcp.tool()
def connect_git(
    workspace_id: str,
    provider: str,
    organization: str,
    project: str,
    repository: str,
    branch: str,
    directory: str = "/",
) -> dict:
    """Connect a Fabric workspace to a Git repository (AzureDevOps | GitHub)."""
    return to_jsonable(
        get_container().lifecycle_service().connect_git(
            workspace_id,
            provider=provider,
            organization=organization,
            project=project,
            repository=repository,
            branch=branch,
            directory=directory,
        )
    )


@mcp.tool()
def git_status(workspace_id: str) -> dict:
    """Show the Git sync status for a workspace."""
    return to_jsonable(get_container().lifecycle_service().git_status(workspace_id))


@mcp.tool()
def commit_to_git(
    workspace_id: str, comment: str, items: Optional[list[dict]] = None
) -> dict:
    """Commit workspace changes (or selected items) to the connected Git repo."""
    return to_jsonable(
        get_container().lifecycle_service().commit_to_git(
            workspace_id, comment=comment, items=items
        )
    )


@mcp.tool()
def update_from_git(workspace_id: str) -> dict:
    """Pull the latest changes from the connected Git repo into the workspace."""
    return to_jsonable(
        get_container().lifecycle_service().update_from_git(workspace_id)
    )


@mcp.tool()
def list_deployment_pipelines() -> list[dict]:
    """List Fabric deployment pipelines visible to the platform identity."""
    return [
        to_jsonable(p)
        for p in get_container().lifecycle_service().list_pipelines()
    ]


@mcp.tool()
def deploy_to_stage(
    pipeline_id: str,
    source_stage_id: str,
    target_stage_id: str,
    note: Optional[str] = None,
    items: Optional[list[dict]] = None,
) -> dict:
    """Deploy from one pipeline stage to the next."""
    return to_jsonable(
        get_container().lifecycle_service().deploy(
            pipeline_id,
            source_stage_id=source_stage_id,
            target_stage_id=target_stage_id,
            note=note,
            items=items,
        )
    )


# ---------------------------------------------------------------------------
# Governance (permissions, labels, tags, workspace roles)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_item_permissions(workspace_id: str, item_id: str) -> list[dict]:
    """List role assignments on a single Fabric item."""
    return [
        to_jsonable(r)
        for r in get_container().governance_service().list_item_permissions(
            workspace_id, item_id
        )
    ]


@mcp.tool()
def set_item_permissions(
    workspace_id: str,
    item_id: str,
    principal_id: str,
    principal_type: str,
    role: str,
) -> dict:
    """Grant a role on one item to a principal."""
    return to_jsonable(
        get_container().governance_service().set_item_permission(
            workspace_id,
            item_id,
            principal_id=principal_id,
            principal_type=principal_type,
            role=role,
        )
    )


@mcp.tool()
def apply_sensitivity_label(
    workspace_id: str,
    item_id: str,
    label_id: str,
    assignment_method: str = "Standard",
) -> dict:
    """Apply an MIP sensitivity label to a Fabric item."""
    return to_jsonable(
        get_container().governance_service().apply_sensitivity_label(
            workspace_id,
            item_id,
            label_id=label_id,
            assignment_method=assignment_method,
        )
    )


@mcp.tool()
def tag_item(workspace_id: str, item_id: str, tag_ids: list[str]) -> dict:
    """Apply one or more tags to a Fabric item."""
    return to_jsonable(
        get_container().governance_service().apply_tags(
            workspace_id, item_id, tag_ids=tag_ids
        )
    )


@mcp.tool()
def add_workspace_role(
    workspace_id: str,
    principal_id: str,
    principal_type: str,
    role: str,
) -> dict:
    """Assign a workspace-scope role to a principal."""
    return to_jsonable(
        get_container().governance_service().add_workspace_role(
            workspace_id,
            principal_id=principal_id,
            principal_type=principal_type,
            role=role,
        )
    )


def main() -> None:
    """Entrypoint: run the MCP server.

    Transport is selected by ``FABRIC_MCP_TRANSPORT`` (default ``stdio`` for
    local agent hosts). In a container set it to ``streamable-http`` (or
    ``sse``) so the server is reachable over the network; ``FABRIC_MCP_HOST``
    and ``FABRIC_MCP_PORT`` tune the HTTP bind.
    """
    transport = os.environ.get("FABRIC_MCP_TRANSPORT", "stdio")
    if transport in {"streamable-http", "sse"}:
        mcp.settings.host = os.environ.get("FABRIC_MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("FABRIC_MCP_PORT", "8000"))
    mcp.run(transport=transport)


if __name__ == "__main__":  # pragma: no cover
    main()
