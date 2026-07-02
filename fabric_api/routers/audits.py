"""Audit routes — semantic-model and report analysis."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.intelligence import ReportSpec, SemanticModelSpec
from fabric_services import ServiceContainer

from ..dependencies import get_container
from ..models import AuditModelRequest, AuditReportRequest, to_jsonable

router = APIRouter(prefix="/audits", tags=["audits"])


@router.post("/semantic-model")
def audit_semantic_model(
    body: AuditModelRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Run the deterministic auditor suite (optionally including full BPA)."""
    spec = SemanticModelSpec.from_dict(body.spec)
    reports = container.audit_service().audit_semantic_model(
        spec, features=body.features, include_bpa=body.include_bpa
    )
    return {feature: to_jsonable(report) for feature, report in reports.items()}


@router.post("/report")
def audit_report(
    body: AuditReportRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Audit a report — agent-enriched by default, deterministic fallback."""
    spec = ReportSpec.from_dict(body.spec)
    result = container.audit_service().audit_report(spec, use_agent=body.use_agent)
    return {
        "report": to_jsonable(result.report),
        "status": result.status,
        "agentFindingCount": result.agent_finding_count,
        "error": result.error,
    }
