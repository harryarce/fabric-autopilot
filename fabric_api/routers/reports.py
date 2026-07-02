"""Report lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.artifacts import ArtifactRef
from app.intelligence import ReportSpec, SemanticModelSpec, SuggestionSpec
from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from ..dependencies import get_container, get_tenant
from ..models import (
    ApplyAuditSuggestionsRequest,
    PersistSuggestionsRequest,
    ProposeReportSuggestionsRequest,
    PublishReportRequest,
    SuggestionStatusRequest,
    SuggestReportRequest,
    UpdateReportRequest,
    to_jsonable,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{workspace_id}")
def list_reports(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> list[dict]:
    """List reports in a workspace."""
    reports = container.report_service(tenant).list_reports(workspace_id)
    return [to_jsonable(r) for r in reports]


@router.post("/{workspace_id}/{report_id}/import")
def import_report(
    workspace_id: str,
    report_id: str,
    workspace_name: str = Query(""),
    item_name: str = Query(""),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Fetch, persist, and parse a report's PBIR definition from Fabric."""
    result = container.report_service(tenant).import_report(
        workspace_id,
        report_id,
        workspace_name=workspace_name,
        item_name=item_name,
    )
    return {"spec": to_jsonable(result["spec"]), "files": result["files"]}


@router.post("/suggest")
def suggest_report(
    body: SuggestReportRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate a starter report grounded in a semantic model (agent-first)."""
    model = SemanticModelSpec.from_dict(body.model)
    result = container.report_service(tenant).suggest_report(
        model,
        report_name=body.report_name,
        dataset_id=body.dataset_id,
        dataset_name=body.dataset_name,
        use_agent=body.use_agent,
        extra_instructions=body.extra_instructions,
    )
    return {
        "spec": to_jsonable(result.spec),
        "status": result.status,
        "usedAgent": result.used_agent,
        "agentVisualCount": result.agent_visual_count,
        "error": result.error,
    }


@router.post("/build-definition")
def build_definition(
    spec: dict,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Render a report spec to a PBIR definition (files + base64 parts)."""
    report_spec = ReportSpec.from_dict(spec)
    definition = container.report_service(tenant).build_definition(report_spec)
    return {"files": definition.files, "definition": definition.definition_payload()}


@router.post("/publish", status_code=201)
def publish_report(
    body: PublishReportRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Create a report in Fabric from a spec."""
    report_spec = ReportSpec.from_dict(body.spec)
    created = container.report_service(tenant).publish_report(
        body.workspace_id,
        body.display_name,
        report_spec,
        description=body.description,
        dataset_id=body.dataset_id,
    )
    payload = to_jsonable(created)
    op = request.app.state.operations.record("publish_report", created.status, payload)
    return {"operationId": op.id, "status": created.status, "item": payload}


@router.put("/{workspace_id}/{report_id}")
def update_report(
    workspace_id: str,
    report_id: str,
    body: UpdateReportRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Replace a published report's definition (Fabric ``updateDefinition``)."""
    report_spec = ReportSpec.from_dict(body.spec)
    service = container.report_service(tenant)
    outcome = service.update_report(
        workspace_id, report_id, report_spec, item_name=body.item_name
    )
    verification: dict | None = None
    if body.verify:
        try:
            verification = service.verify_update(
                workspace_id, report_id, before=report_spec
            )
        except Exception as exc:  # pragma: no cover - verification best-effort
            verification = {"error": str(exc)}
    op = request.app.state.operations.record(
        "update_report", outcome["status"], outcome
    )
    response = {
        "operationId": op.id,
        "status": outcome["status"],
        "fileCount": outcome["fileCount"],
    }
    if verification is not None:
        response["verification"] = verification
    return response


# -- theme remediation suggestions ------------------------------------------


@router.post("/suggestions/propose-theme")
def propose_theme_suggestions(
    body: ProposeReportSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate brand/WCAG theme remediation suggestions for a report spec."""
    report_spec = ReportSpec.from_dict(body.spec)
    suggestions = container.report_service(tenant).generate_theme_remediation(report_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.post("/suggestions/apply")
def apply_audit_suggestions(
    body: ApplyAuditSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Apply accepted theme/formatting suggestions to a report spec."""
    report_spec = ReportSpec.from_dict(body.spec)
    suggestions = [SuggestionSpec.from_dict(s) for s in body.suggestions]
    updated_spec, updated_suggestions = container.report_service(
        tenant
    ).apply_audit_suggestions(report_spec, suggestions)
    return {
        "spec": to_jsonable(updated_spec),
        "suggestions": [s.to_dict() for s in updated_suggestions],
    }


@router.post("/suggestions/persist")
def persist_suggestions(
    body: PersistSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Persist a suggestion bundle alongside the stored report artifact."""
    ref = ArtifactRef(
        kind="reports",
        workspace_id=body.workspace_id,
        item_id=body.item_id,
        workspace_name=body.workspace_name,
        item_name=body.item_name,
    )
    suggestions = [SuggestionSpec.from_dict(s) for s in body.suggestions]
    key = container.artifact_service(tenant).save_suggestions(ref, suggestions)
    return {"key": key, "count": len(suggestions)}


@router.get("/suggestions/load")
def load_suggestions(
    workspace_id: str = Query(...),
    item_id: str = Query(...),
    workspace_name: str = Query(""),
    item_name: str = Query(""),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Return the persisted suggestion bundle for a stored report."""
    ref = ArtifactRef(
        kind="reports",
        workspace_id=workspace_id,
        item_id=item_id,
        workspace_name=workspace_name,
        item_name=item_name,
    )
    suggestions = container.artifact_service(tenant).load_suggestions(ref)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.patch("/suggestions/status")
def patch_suggestion_status(
    body: SuggestionStatusRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Update one suggestion's lifecycle status (accept / skip / ...)."""
    ref = ArtifactRef(
        kind="reports",
        workspace_id=body.workspace_id,
        item_id=body.item_id,
        workspace_name=body.workspace_name,
        item_name=body.item_name,
    )
    updated = container.artifact_service(tenant).update_suggestion_status(
        ref, body.suggestion_id, body.status
    )
    return {"suggestion": updated.to_dict()}
