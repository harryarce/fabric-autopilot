"""Semantic-model **Copilot readiness** audit.

Implements the "Copilot preparation" requirement: deterministic checks that a
model is well-grounded for Power BI Copilot / Q&A — descriptions on tables and
measures (Copilot's primary grounding signal), natural-language-friendly names,
explicit measures, and glossary coverage for domain jargon.

Carries lightweight awareness of the **risk / insurance** domain (RMIS, IBNR,
loss runs, TPA, …): cryptic domain acronyms without descriptions are the single
biggest cause of poor Copilot answers, so they are flagged specifically.

Pure function of a :class:`~app.intelligence.spec.SemanticModelSpec`.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from ..spec import SemanticModelSpec
from .base import WARNING, AuditReport

FEATURE = "semantic-model-copilot-prep"

# Risk/insurance domain acronyms and terms that benefit from an explicit
# description or synonym so Copilot can answer natural-language questions.
# Externalized to ``app/intelligence/resources/risk_domain_glossary.json`` so
# operators (and per-tenant overrides) can curate the vocabulary without
# touching code.
def _load_domain_terms() -> dict[str, str]:
    try:
        with resources.files("app.intelligence.resources").joinpath(
            "risk_domain_glossary.json"
        ).open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k).lower(): str(v) for k, v in payload.get("terms", {}).items()}


_DOMAIN_TERMS = _load_domain_terms()

_WORD_RE = re.compile(r"[A-Za-z]+")


def _domain_hits(name: str) -> list[str]:
    hits: list[str] = []
    lowered = name.lower()
    for token in _WORD_RE.findall(lowered):
        if token in _DOMAIN_TERMS:
            hits.append(token)
    return hits


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the Copilot-readiness rule set over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)

    if not spec.description:
        report.add(
            WARNING,
            "SM_COPILOT_MODEL_NO_DESCRIPTION",
            "The model has no description for Copilot to ground on.",
            object_ref=spec.name,
            recommendation="Add a concise description of the subject area.",
        )

    total_measures = sum(len(t.measures) for t in spec.tables)
    if total_measures == 0:
        report.add(
            WARNING,
            "SM_COPILOT_NO_MEASURES",
            "The model defines no explicit measures.",
            recommendation="Copilot answers are far better with named measures "
            "than with implicit column aggregation; add key measures.",
        )

    for table in spec.tables:
        _audit_object_for_copilot(
            report, table.name, table.description, kind="table"
        )
        for col in table.columns:
            if col.is_hidden:
                continue
            ref = f"{table.name}[{col.name}]"
            for hit in _domain_hits(col.name):
                if not col.description:
                    report.add(
                        WARNING,
                        "SM_COPILOT_DOMAIN_TERM_UNDESCRIBED",
                        f"Column {ref} uses domain term "
                        f"'{hit.upper()}' without a description.",
                        object_ref=ref,
                        recommendation=f"Add a description/synonym "
                        f"(e.g. '{_DOMAIN_TERMS[hit]}') so Copilot understands it.",
                    )
                    break
        for measure in table.measures:
            ref = f"{table.name}[{measure.name}]"
            if not measure.description:
                report.add(
                    WARNING,
                    "SM_COPILOT_MEASURE_NO_DESCRIPTION",
                    f"Measure {ref} has no description.",
                    object_ref=ref,
                    recommendation="Describe what the measure computes; Copilot "
                    "surfaces this when answering.",
                )

    return report


def _audit_object_for_copilot(
    report: AuditReport, name: str, description: str | None, *, kind: str
) -> None:
    hits = _domain_hits(name)
    if hits and not description:
        report.add(
            WARNING,
            "SM_COPILOT_DOMAIN_TERM_UNDESCRIBED",
            f"{kind.capitalize()} '{name}' uses domain term "
            f"'{hits[0].upper()}' without a description.",
            object_ref=name,
            recommendation=f"Add a description (e.g. "
            f"'{_DOMAIN_TERMS[hits[0]]}').",
        )
