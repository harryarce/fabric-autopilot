"""Deterministic audit framework for Fabric artifacts.

Each auditor consumes an in-memory IR (a
:class:`~app.intelligence.spec.SemanticModelSpec` or
:class:`~app.intelligence.report_spec.ReportSpec`) and returns an
:class:`AuditReport` — a structured list of :class:`AuditFinding` items plus a
score. Auditors are **deterministic and dependency-free** (no network, no AI);
an optional AI layer may *enrich* a report afterwards, but the rules here always
stand on their own so every feature has a working baseline.

This module defines only the shared vocabulary; concrete rule sets live in the
sibling modules (``usability``, ``design``, ``copilot_prep``, ``dax``,
``report_formatting``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Severity levels, ordered most-to-least serious.
ERROR = "error"
WARNING = "warning"
INFO = "info"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}
_SEVERITY_WEIGHT = {ERROR: 10, WARNING: 3, INFO: 1}


@dataclass(frozen=True)
class AuditFinding:
    """One audit observation with an actionable recommendation."""

    severity: str  # ERROR | WARNING | INFO
    code: str  # stable, machine-readable rule id, e.g. "SM_USAB_NO_DESCRIPTION"
    message: str  # what was observed
    object_ref: str | None = None  # e.g. "Sales[Amount]"
    recommendation: str | None = None  # how to fix it

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.object_ref:
            data["object_ref"] = self.object_ref
        if self.recommendation:
            data["recommendation"] = self.recommendation
        return data


@dataclass
class AuditReport:
    """The result of running one auditor over one artifact."""

    feature: str  # e.g. "semantic-model-usability"
    target: str  # the artifact name audited
    findings: list[AuditFinding] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )

    # -- mutation ---------------------------------------------------------

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        object_ref: str | None = None,
        recommendation: str | None = None,
    ) -> None:
        self.findings.append(
            AuditFinding(
                severity=severity,
                code=code,
                message=message,
                object_ref=object_ref,
                recommendation=recommendation,
            )
        )

    # -- queries ----------------------------------------------------------

    @property
    def errors(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def infos(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == INFO]

    @property
    def ok(self) -> bool:
        """True when there are no error-level findings."""
        return not self.errors

    @property
    def score(self) -> int:
        """A 0-100 health score (100 = clean), weighted by severity.

        Heuristic and bounded: each finding subtracts a severity-weighted
        amount, so a model with many warnings still scores above one with
        blocking errors.
        """
        penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 1) for f in self.findings)
        return max(0, 100 - penalty)

    def sorted_findings(self) -> list[AuditFinding]:
        return sorted(
            self.findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.code)
        )

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "target": self.target,
            "generatedAt": self.generated_at,
            "score": self.score,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "infos": len(self.infos),
            },
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        lines = [
            f"# Audit: {self.feature}",
            "",
            f"**Target:** {self.target}  ",
            f"**Score:** {self.score}/100  ",
            f"**Errors:** {len(self.errors)} · "
            f"**Warnings:** {len(self.warnings)} · "
            f"**Info:** {len(self.infos)}",
            "",
        ]
        if not self.findings:
            lines.append("No issues found. ✅")
            return "\n".join(lines)
        icon = {ERROR: "❌", WARNING: "⚠️", INFO: "ℹ️"}
        for finding in self.sorted_findings():
            head = f"- {icon.get(finding.severity, '•')} **{finding.code}**"
            if finding.object_ref:
                head += f" (`{finding.object_ref}`)"
            lines.append(f"{head}: {finding.message}")
            if finding.recommendation:
                lines.append(f"  - *Fix:* {finding.recommendation}")
        return "\n".join(lines)


def merge_reports(feature: str, target: str, reports: list[AuditReport]) -> AuditReport:
    """Combine several reports' findings into one (e.g. a full SM health check)."""
    combined = AuditReport(feature=feature, target=target)
    for report in reports:
        combined.findings.extend(report.findings)
    return combined
