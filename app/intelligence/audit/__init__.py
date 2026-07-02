"""Deterministic auditors for Fabric artifacts.

Semantic-model auditors (each a pure function of a ``SemanticModelSpec``):

* :mod:`usability` — consumer-facing readability review.
* :mod:`design` — structural/relationship correctness.
* :mod:`copilot_prep` — Copilot/Q&A grounding readiness.
* :mod:`dax` — measure-expression verification.

Report auditors (a pure function of a ``ReportSpec``):

* :mod:`report_formatting` — formatting / WCAG accessibility review.

The :func:`audit_semantic_model` helper runs the full semantic-model suite and
returns the per-feature reports plus a merged "health" report.
"""

from __future__ import annotations

from ..report_spec import ReportSpec
from ..spec import SemanticModelSpec
from . import (
    bpa,
    copilot_prep,
    dax,
    dax_patterns,
    design,
    report_agent,
    report_formatting,
    usability,
)
from .base import (
    ERROR,
    INFO,
    WARNING,
    AuditFinding,
    AuditReport,
    merge_reports,
)
from .remediation import (
    load_brand_theme,
    propose_bpa_fixes,
    propose_copilot_prep_fixes,
    propose_theme_remediation,
    propose_usability_fixes,
)
from .report_agent import (
    ReportAuditConfig,
    ReportAuditOutcome,
    audit_report_with_agent,
)

__all__ = [
    "ERROR",
    "WARNING",
    "INFO",
    "AuditFinding",
    "AuditReport",
    "merge_reports",
    "SEMANTIC_MODEL_AUDITORS",
    "BPA_FEATURE",
    "REPORT_AUDITORS",
    "audit_semantic_model",
    "audit_report",
    "audit_report_with_agent",
    "report_agent_available",
    "ReportAuditConfig",
    "ReportAuditOutcome",
    "load_brand_theme",
    "propose_bpa_fixes",
    "propose_copilot_prep_fixes",
    "propose_theme_remediation",
    "propose_usability_fixes",
]

# Ordered registry of semantic-model auditors: feature name -> audit function.
SEMANTIC_MODEL_AUDITORS = {
    usability.FEATURE: usability.audit,
    design.FEATURE: design.audit,
    copilot_prep.FEATURE: copilot_prep.audit,
    dax.FEATURE: dax.audit,
    dax_patterns.FEATURE: dax_patterns.audit,
}

# The Best Practice Analyzer auditor is opt-in (toggled from the UI) because it
# evaluates Microsoft's full BPA rule set, which is stricter and noisier than the
# core review. Kept out of the default registry so existing callers are unchanged.
BPA_FEATURE = bpa.FEATURE

# Registry of report auditors.
REPORT_AUDITORS = {
    report_formatting.FEATURE: report_formatting.audit,
}


def audit_semantic_model(
    spec: SemanticModelSpec,
    *,
    features: list[str] | None = None,
    include_bpa: bool = False,
) -> dict[str, AuditReport]:
    """Run the requested semantic-model auditors over ``spec``.

    Args:
        spec: the model to audit.
        features: optional subset of feature names (keys of
            :data:`SEMANTIC_MODEL_AUDITORS`). Defaults to all core auditors.
        include_bpa: when ``True``, also run the Best Practice Analyzer auditor
            (Microsoft's full BPA rule subset) and fold it into the merged
            health report. Off by default; the UI exposes a toggle.

    Returns:
        A mapping of feature name to :class:`AuditReport`, plus a merged
        ``"semantic-model-health"`` report aggregating every finding.
    """
    selected = features or list(SEMANTIC_MODEL_AUDITORS)
    reports: dict[str, AuditReport] = {}
    for feature in selected:
        auditor = SEMANTIC_MODEL_AUDITORS.get(feature)
        if auditor is not None:
            reports[feature] = auditor(spec)
    if include_bpa:
        reports[bpa.FEATURE] = bpa.audit(spec)
    reports["semantic-model-health"] = merge_reports(
        "semantic-model-health", spec.name, list(reports.values())
    )
    return reports


def audit_report(spec: ReportSpec) -> AuditReport:
    """Run the report formatting/accessibility audit over ``spec``."""
    return report_formatting.audit(spec)


def report_agent_available() -> bool:
    """Return ``True`` when the optional AI report enrichment can run."""
    return report_agent.is_available()
