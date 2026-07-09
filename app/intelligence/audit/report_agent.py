"""Optional AI enrichment for report audits.

The deterministic :mod:`report_formatting` auditor is authoritative on
*objective* facts (WCAG colour-contrast maths, geometry/overlap, structural
presence). This module adds an **optional** agent pass that reaches the
*subjective / semantic* dimensions rules cannot express — chart-type
appropriateness, title/narrative clarity, layout storytelling, field-choice
sensibility and domain appropriateness.

Design rules that keep it trustworthy:

* **Deterministic first.** :func:`audit_report_with_agent` always runs the
  deterministic auditor and returns its findings; the agent only *adds* to them.
* **Grounded.** The agent is given the *real* parsed :class:`ReportSpec` (pages,
  visuals, bound fields) — never asked to imagine a report.
* **Verifiable & traceable.** Agent findings are coerced into the same
  :class:`AuditFinding` shape (severity, code, object_ref, recommendation) with
  an ``RPT_AI_`` code prefix so a human can tell them apart and check each one.
* **Safe fallback.** If the Agent Framework is unavailable or the model errors
  or returns junk, the merged report degrades cleanly to the deterministic
  baseline and the outcome status records why.

All Agent Framework imports are lazy so the deterministic engine keeps working
without ``agent-framework`` installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..report_spec import ReportSpec
from . import report_formatting
from .base import ERROR, INFO, WARNING, AuditFinding, AuditReport

# The merged feature name (deterministic + AI). The deterministic-only feature
# remains ``report_formatting.FEATURE`` ("report-formatting").
FEATURE = "report-formatting-ai"

# Agent findings carry this code prefix so provenance is always obvious.
_AI_CODE_PREFIX = "RPT_AI_"

# Cap how many agent findings we accept, to bound noise and cost.
_MAX_AGENT_FINDINGS = 25

_VALID_SEVERITIES = {ERROR, WARNING, INFO}

# Reuse the app's Foundry namespace defaults (kept in sync with ``agent.py``).
# Empty by design so the AI enrichment stays dormant until the operator sets
# ``FOUNDRY_PROJECT_ENDPOINT`` / ``FOUNDRY_MODEL``; the deterministic auditor
# runs regardless.
DEFAULT_PROJECT_ENDPOINT = ""
DEFAULT_MODEL = ""
DEFAULT_AGENT_NAME = "report-formatting-reviewer"

_INSTRUCTIONS = """\
You are a senior Power BI / Microsoft Fabric report-design reviewer.

You will receive a JSON description of a report: its pages and, for each page,
the visuals with their type, title, position, size and the model fields bound
to each visual role. This is the REAL report — reason only about what is given.

A separate deterministic checker already covers objective issues (colour
contrast, overlap, off-canvas placement, missing titles/values, missing theme).
DO NOT repeat those. Your job is the judgement-based review they cannot do:

* Chart-type appropriateness — e.g. a pie/donut with many categories, a line
  chart without a time axis, a single-value shown as a table.
* Title & narrative clarity — does each title actually describe the visual and
  read well to a business audience? Are titles generic ("Chart", a field name)?
* Layout & storytelling — does the page read in a sensible order (KPIs first,
  detail later)? Is it cluttered or unbalanced?
* Field-choice sensibility — are the bound fields meaningful for the visual, or
  is a key/ID surfaced where a descriptive attribute belongs?
* Domain appropriateness — if the data is risk/insurance (claims, premium,
  IBNR, loss ratio, reserves, TPA, exposure), flag technical jargon shown to
  executives without a friendly label or context.

Respond with a SINGLE JSON object and nothing else:
{
  "findings": [
    {
      "severity": "warning",            // one of: error | warning | info
      "code": "CHART_TYPE",             // SHORT_UPPER_SNAKE topic id
      "message": "What you observed, specific and verifiable.",
      "object_ref": "Page 1 / Revenue by Customer",  // page and/or visual it refers to
      "recommendation": "A concrete, actionable fix."
    }
  ]
}

Rules: reference real pages/visuals from the input in ``object_ref``. Prefer
``info`` for stylistic nudges and ``warning`` for genuine usability problems;
reserve ``error`` for choices that actively mislead. If the report is sound,
return {"findings": []}.
"""


# ---------------------------------------------------------------------------
# Availability + configuration
# ---------------------------------------------------------------------------


class ReportAuditUnavailableError(RuntimeError):
    """Raised when the Microsoft Agent Framework is not installed."""


def is_available() -> bool:
    """Return ``True`` when the Agent Framework + Foundry connector import."""
    try:  # pragma: no cover - trivial import probe
        import agent_framework  # noqa: F401
        from agent_framework.foundry import FoundryChatClient  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class ReportAuditConfig:
    """Connection settings for the Foundry-backed report reviewer."""

    project_endpoint: str = ""
    model: str = ""
    agent_name: str = DEFAULT_AGENT_NAME

    @classmethod
    def from_env(cls) -> "ReportAuditConfig":
        return cls(
            project_endpoint=os.getenv(
                "FOUNDRY_PROJECT_ENDPOINT", DEFAULT_PROJECT_ENDPOINT
            ),
            model=os.getenv("FOUNDRY_MODEL", DEFAULT_MODEL),
            agent_name=os.getenv("FOUNDRY_REPORT_AGENT_NAME", DEFAULT_AGENT_NAME),
        )


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class ReportAuditOutcome:
    """A report audit plus diagnostics about the AI enrichment.

    ``report`` is always usable: it contains the deterministic findings at a
    minimum, with any accepted agent findings merged in. ``status`` is one of:

    * ``"ok"``            – the agent replied and added (>=0) grounded findings.
    * ``"error"``         – the agent errored or returned unusable output; the
                            deterministic baseline is returned and ``error``
                            holds the message.
    * ``"deterministic"`` – the agent was not invoked (not requested/available).
    """

    report: AuditReport
    status: str = "deterministic"
    error: str | None = None
    agent_finding_count: int = 0


# ---------------------------------------------------------------------------
# Grounded brief
# ---------------------------------------------------------------------------


def build_report_brief(spec: ReportSpec) -> dict[str, Any]:
    """Build a compact, grounded description of the report for the agent."""
    pages: list[dict[str, Any]] = []
    for page in spec.pages:
        visuals: list[dict[str, Any]] = []
        for visual in page.visuals:
            visuals.append(
                {
                    "name": visual.name,
                    "type": visual.visual_type,
                    "title": visual.title,
                    "position": {"x": visual.x, "y": visual.y},
                    "size": {"width": visual.width, "height": visual.height},
                    "fields": [
                        {"role": role, "ref": f.query_ref, "kind": f.kind}
                        for role, fields in visual.projections.items()
                        for f in fields
                    ],
                }
            )
        pages.append(
            {
                "name": page.name,
                "display_name": page.display_name,
                "size": {"width": page.width, "height": page.height},
                "visuals": visuals,
            }
        )
    return {
        "report": spec.name,
        "dataset": spec.dataset_name or spec.dataset_id,
        "theme": spec.theme.name if spec.theme else None,
        "pages": pages,
    }


def _valid_object_refs(spec: ReportSpec) -> set[str]:
    """Tokens the agent may legitimately reference, lower-cased for matching."""
    tokens: set[str] = set()
    for page in spec.pages:
        for value in (page.name, page.display_name):
            if value:
                tokens.add(value.casefold())
        for visual in page.visuals:
            for value in (visual.name, visual.title):
                if value:
                    tokens.add(value.casefold())
            for f in visual.all_fields():
                tokens.add(f.query_ref.casefold())
                tokens.add(f.property.casefold())
    return tokens


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class ReportAuditIntelligence:
    """Reusable facade that enriches a deterministic report audit with AI."""

    def __init__(self, config: ReportAuditConfig | None = None) -> None:
        self.config = config or ReportAuditConfig.from_env()
        if not is_available():
            raise ReportAuditUnavailableError(
                "Microsoft Agent Framework is not installed. Install it with: "
                "pip install agent-framework agent-framework-foundry"
            )

    # -- internal builders ------------------------------------------------

    def _credential(self):
        import os

        from azure.identity.aio import DefaultAzureCredential

        from app.auth import cli_process_timeout

        client_id = os.environ.get("AZURE_CLIENT_ID") or None
        return DefaultAzureCredential(
            managed_identity_client_id=client_id,
            exclude_interactive_browser_credential=True,
            process_timeout=cli_process_timeout(),
        )

    def _chat_client(self, credential):
        from agent_framework.foundry import FoundryChatClient

        return FoundryChatClient(
            project_endpoint=self.config.project_endpoint,
            model=self.config.model,
            credential=credential,
        )

    def _build_agent(self, client):
        from agent_framework import Agent

        return Agent(
            name=self.config.agent_name,
            client=client,
            instructions=_INSTRUCTIONS,
            description="Reviews Power BI / Fabric report design quality.",
        )

    # -- enrichment -------------------------------------------------------

    async def enrich(self, spec: ReportSpec) -> list[AuditFinding]:
        """Return grounded agent findings for ``spec`` (may be empty)."""
        brief = build_report_brief(spec)
        prompt = (
            "Review this report's design quality and return ONLY the JSON "
            "object of findings described in your instructions.\n\n"
            f"REPORT:\n{json.dumps(brief, indent=2)}"
        )
        credential = self._credential()
        async with credential:
            client = self._chat_client(credential)
            async with self._build_agent(client) as agent:
                result = await agent.run(prompt)
                text = _result_text(result)
        return _parse_findings(text, valid_refs=_valid_object_refs(spec))

    def enrich_sync(self, spec: ReportSpec) -> list[AuditFinding]:
        """Synchronous wrapper around :meth:`enrich` (for Streamlit)."""
        from ..agent import _run_async

        return _run_async(self.enrich(spec))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def audit_report_with_agent(
    spec: ReportSpec,
    *,
    config: ReportAuditConfig | None = None,
) -> ReportAuditOutcome:
    """Run the deterministic report audit, then (optionally) enrich it with AI.

    The deterministic findings are always present. When the agent is available
    and succeeds, its grounded findings are merged in under the ``RPT_AI_`` code
    prefix. Any failure degrades cleanly to the deterministic baseline.
    """
    deterministic = report_formatting.audit(spec)

    if not is_available():
        # Return a baseline report under the merged feature name so callers can
        # treat the output uniformly.
        baseline = AuditReport(feature=FEATURE, target=spec.name)
        baseline.findings.extend(deterministic.findings)
        return ReportAuditOutcome(report=baseline, status="deterministic")

    merged = AuditReport(feature=FEATURE, target=spec.name)
    merged.findings.extend(deterministic.findings)
    try:
        agent_findings = ReportAuditIntelligence(config).enrich_sync(spec)
    except Exception as exc:  # noqa: BLE001 - reported via outcome status
        return ReportAuditOutcome(
            report=merged, status="error", error=str(exc)
        )

    merged.findings.extend(agent_findings)
    return ReportAuditOutcome(
        report=merged, status="ok", agent_finding_count=len(agent_findings)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_text(result: Any) -> str:
    """Extract the assistant text from an Agent Framework run result."""
    if result is None:
        return ""
    for attr in ("text", "content", "output_text"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return str(result)


def _extract_json_object(text: str) -> str | None:
    """Pull a JSON object out of possibly-fenced agent text."""
    if not text:
        return None
    cleaned = text.strip()
    if "```" in cleaned:
        start = cleaned.find("```")
        fence_end = cleaned.find("\n", start)
        end = cleaned.find("```", fence_end + 1)
        if fence_end != -1 and end != -1:
            cleaned = cleaned[fence_end + 1 : end].strip()
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first == -1 or last == -1:
            return None
        cleaned = cleaned[first : last + 1]
    return cleaned


def _normalise_code(raw: str | None) -> str:
    """Coerce an agent topic id into a stable, prefixed code."""
    base = (raw or "FINDING").strip().upper()
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in base)
    safe = safe.strip("_") or "FINDING"
    if safe.startswith(_AI_CODE_PREFIX):
        return safe
    return f"{_AI_CODE_PREFIX}{safe}"


def _parse_findings(
    text: str, *, valid_refs: set[str]
) -> list[AuditFinding]:
    """Parse and validate the agent's findings JSON into ``AuditFinding`` items.

    Findings with an invalid severity are dropped. ``object_ref`` is kept as the
    agent's human-readable locator; when it clearly matches a real page/visual
    token we leave it, otherwise we still keep the finding but mark the ref as
    unverified so the reviewer knows to check it.
    """
    payload = _extract_json_object(text)
    if payload is None:
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    raw_findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw_findings, list):
        return []

    findings: list[AuditFinding] = []
    for item in raw_findings[:_MAX_AGENT_FINDINGS]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).strip().lower()
        if severity not in _VALID_SEVERITIES:
            severity = INFO
        message = str(item.get("message", "")).strip()
        if not message:
            continue
        object_ref = item.get("object_ref")
        object_ref = str(object_ref).strip() if object_ref else None
        if object_ref and not _ref_is_grounded(object_ref, valid_refs):
            object_ref = f"{object_ref} (unverified)"
        recommendation = item.get("recommendation")
        recommendation = (
            str(recommendation).strip() if recommendation else None
        )
        findings.append(
            AuditFinding(
                severity=severity,
                code=_normalise_code(item.get("code")),
                message=message,
                object_ref=object_ref,
                recommendation=recommendation,
            )
        )
    return findings


def _ref_is_grounded(object_ref: str, valid_refs: set[str]) -> bool:
    """True when any known page/visual/field token appears in ``object_ref``."""
    haystack = object_ref.casefold()
    return any(token and token in haystack for token in valid_refs)
