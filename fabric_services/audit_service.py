"""Audit service — deterministic + agent-enriched analysis.

Since the platform prioritizes agentic capabilities, report auditing defaults to
the **AI-enriched** path (:func:`audit_report_with_agent`) and degrades cleanly
to the deterministic baseline when Foundry is unreachable. Semantic-model audits
run the deterministic rule suite (optionally including the full Best Practice
Analyzer), which is comprehensive on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.intelligence import ReportSpec, SemanticModelSpec
from app.intelligence.audit import (
    AuditReport,
    audit_report,
    audit_report_with_agent,
    audit_semantic_model,
    report_agent_available,
)

logger = logging.getLogger("fabric_services.audit")


@dataclass
class ReportAuditResult:
    """Outcome of a report audit, carrying provenance for the API/UI."""

    report: AuditReport
    status: str  # "ok" | "deterministic" | "error"
    agent_finding_count: int = 0
    error: str | None = None


class AuditService:
    """Run semantic-model and report audits."""

    # -- semantic models --------------------------------------------------

    def audit_semantic_model(
        self,
        spec: SemanticModelSpec,
        *,
        features: list[str] | None = None,
        include_bpa: bool = False,
    ) -> dict[str, AuditReport]:
        """Audit a semantic model, returning per-feature + merged health reports."""
        return audit_semantic_model(
            spec, features=features, include_bpa=include_bpa
        )

    # -- reports ----------------------------------------------------------

    def audit_report(
        self, spec: ReportSpec, *, use_agent: bool = True
    ) -> ReportAuditResult:
        """Audit a report.

        Args:
            spec: the report to audit.
            use_agent: when ``True`` (default) and the Foundry agent is
                available, enrich the deterministic findings with grounded AI
                suggestions; otherwise run deterministic-only.
        """
        if use_agent:
            outcome = audit_report_with_agent(spec)
            if outcome.status != "ok":
                logger.warning(
                    "AGENTIC FALLBACK: report audit agent not used (status=%s%s) "
                    "- served deterministic audit.",
                    outcome.status,
                    f", error={getattr(outcome, 'error', None)}"
                    if getattr(outcome, "error", None)
                    else "",
                )
            return ReportAuditResult(
                report=outcome.report,
                status=outcome.status,
                agent_finding_count=getattr(outcome, "agent_finding_count", 0),
                error=getattr(outcome, "error", None),
            )
        logger.info(
            "Agentic layer disabled by caller (use_agent=False) - serving "
            "deterministic report audit."
        )
        return ReportAuditResult(report=audit_report(spec), status="deterministic")

    # -- capability probe -------------------------------------------------

    def agent_available(self) -> bool:
        """Return ``True`` when AI report enrichment can run."""
        return report_agent_available()
