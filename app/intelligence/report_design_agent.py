"""AI-driven **report design**: the agent proposes the visuals, deterministic
code composes them.

This is the "smart starting point" for report creation. A Microsoft Foundry
model reads a grounded brief of the semantic model — its tables (fact /
dimension / date), columns (keys, hidden, summarisation), measures and
relationships — and proposes a set of *insightful* visuals: which chart types,
which fields, and why. The deterministic compositor
(:func:`app.intelligence.report_builder.compose_report`) then grounds those
ideas against the real model and lays them out into a valid
:class:`~app.intelligence.report_spec.ReportSpec`.

Design rules that keep it trustworthy (mirroring the audit agent):

* **Grounded.** The agent only ever sees — and is told to use — table, column
  and measure names that exist in the model.
* **Validated.** Every suggested field is re-checked against the model and wired
  to the correct visual role; anything ungrounded is dropped.
* **Safe fallback.** If the Agent Framework is unavailable, the model errors, or
  it returns nothing usable, report generation degrades cleanly to the
  deterministic baseline (:func:`deterministic_visual_suggestions`).

All Agent Framework imports are lazy so the deterministic engine keeps working
without ``agent-framework`` installed.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .report_builder import (
    VisualSuggestion,
    compose_report,
    deterministic_visual_suggestions,
    ground_visual_suggestions,
)
from .report_spec import ReportSpec, ReportTheme
from .spec import SemanticModelSpec, dedupe_measure_names

# Reuse the app's Foundry namespace defaults (kept in sync with ``agent.py``).
# Empty by design so the agentic path stays dormant until the operator sets
# ``FOUNDRY_PROJECT_ENDPOINT`` / ``FOUNDRY_MODEL``; the deterministic
# compositor runs regardless.
DEFAULT_PROJECT_ENDPOINT = ""
DEFAULT_MODEL = ""
DEFAULT_AGENT_NAME = "report-design-architect"

# Skills live alongside the model builder's; the report designer follows the
# ``report-authoring-overlay`` skill (the model agent follows its own).
_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# Bound how many visuals we accept from the agent, to keep reports readable.
_MAX_AGENT_VISUALS = 12

_INSTRUCTIONS = """\
You are a senior Power BI / Microsoft Fabric report-design architect.

Always load and follow the `report-authoring-overlay` skill. It defines the
design judgment, hard constraints and the exact JSON output contract you must
return.

You will receive a JSON description of a semantic model: its tables (each marked
as fact, dimension or date), the columns on each table (with flags for key /
hidden / how they summarise), the DAX measures, and the relationships between
tables. Design an insightful single-starting-point report for it.

Think like an analyst telling a story with data:

* Lead with headline KPI cards for the most important measures.
* If a date table exists, include a time trend of the primary measure(s).
* Use the relationships to compare measures across the most meaningful
  dimensions (e.g. a column chart of a measure by a dimension attribute).
* Prefer descriptive dimension attributes over keys/IDs on axes and legends.
* Add a detail table that lets a user drill into the underlying rows.
* Pick chart types that fit the data: lineChart for trends over time, bar/column
  for comparisons across categories, pie/donut only for a few parts-of-a-whole,
  matrix/table for detail.

Hard constraints:

* Use ONLY table, column and measure names that appear in the supplied model.
  Never invent fields. Copy names exactly.
* Bind measures to value roles (Y / Values) and columns to category/legend roles.
* Keep the report focused: 5 to 9 visuals is ideal.

Respond with a SINGLE JSON object and nothing else:
{
  "visuals": [
    {
      "visual_type": "clusteredColumnChart",   // one of: card, multiRowCard,
      // kpi, gauge, table, tableEx, matrix, columnChart, clusteredColumnChart,
      // barChart, clusteredBarChart, lineChart, pieChart, donutChart, slicer
      "title": "Total Sales by Segment",
      "fields": {
        "Category": [{"kind": "column", "entity": "Customer", "property": "Segment"}],
        "Y": [{"kind": "measure", "entity": "Sales", "property": "Total Sales"}]
      },
      "rationale": "Why this visual is insightful for this model.",
      "importance": 80                         // 0-100; higher shows first
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# Availability + configuration
# ---------------------------------------------------------------------------


class ReportDesignUnavailableError(RuntimeError):
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
class ReportDesignConfig:
    """Connection settings for the Foundry-backed report designer."""

    project_endpoint: str = ""
    model: str = ""
    agent_name: str = DEFAULT_AGENT_NAME

    @classmethod
    def from_env(cls) -> "ReportDesignConfig":
        return cls(
            project_endpoint=os.getenv(
                "FOUNDRY_PROJECT_ENDPOINT", DEFAULT_PROJECT_ENDPOINT
            ),
            model=os.getenv("FOUNDRY_MODEL", DEFAULT_MODEL),
            agent_name=os.getenv("FOUNDRY_REPORT_DESIGN_AGENT_NAME", DEFAULT_AGENT_NAME),
        )


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class ReportBuildOutcome:
    """A composed report plus diagnostics about the AI design step.

    ``report`` is always usable: it is composed from the agent's grounded
    visuals when available, otherwise from the deterministic baseline.
    ``status`` is one of:

    * ``"ok"``            – the agent replied and contributed grounded visuals.
    * ``"error"``         – the agent errored or returned nothing usable; the
                            deterministic baseline was used and ``error`` says why.
    * ``"deterministic"`` – the agent was not invoked (not available).
    """

    report: ReportSpec
    status: str = "deterministic"
    error: str | None = None
    agent_visual_count: int = 0
    used_agent: bool = False
    suggestions: list[VisualSuggestion] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Grounded model brief
# ---------------------------------------------------------------------------


def build_model_brief(model: SemanticModelSpec) -> dict[str, Any]:
    """Build a compact, grounded description of the model for the agent."""
    tables: list[dict[str, Any]] = []
    for t in model.tables:
        if t.is_date_table:
            role = "date"
        elif t.measures or any(c.summarize_by != "none" for c in t.columns):
            role = "fact"
        else:
            role = "dimension"
        tables.append(
            {
                "name": t.name,
                "role": role,
                "is_date_table": t.is_date_table,
                "is_hidden": t.is_hidden,
                "description": t.description,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "is_key": c.is_key,
                        "is_hidden": c.is_hidden,
                        "summarize_by": c.summarize_by,
                        "data_category": c.data_category,
                    }
                    for c in t.columns
                ],
                "measures": [
                    {
                        "name": m.name,
                        "format_string": m.format_string,
                        "description": m.description,
                    }
                    for m in t.measures
                ],
            }
        )
    relationships = [
        {
            "from_table": r.from_table,
            "from_column": r.from_column,
            "to_table": r.to_table,
            "to_column": r.to_column,
            "is_active": r.is_active,
        }
        for r in model.relationships
    ]
    return {"model": model.name, "tables": tables, "relationships": relationships}


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class ReportDesignIntelligence:
    """Reusable facade that asks a Foundry model to design report visuals."""

    def __init__(self, config: ReportDesignConfig | None = None) -> None:
        self.config = config or ReportDesignConfig.from_env()
        if not is_available():
            raise ReportDesignUnavailableError(
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

    def _skills_provider(self):
        from agent_framework import SkillsProvider

        from .agent import _subprocess_script_runner

        # Skills are experimental; silence the FutureWarning banner.
        warnings.filterwarnings(
            "ignore", message=r"\[SKILLS\].*", category=FutureWarning
        )
        return SkillsProvider.from_paths(
            skill_paths=str(_SKILLS_DIR),
            script_runner=_subprocess_script_runner,
        )

    def _build_agent(self, client):
        from agent_framework import Agent

        return Agent(
            name=self.config.agent_name,
            client=client,
            instructions=_INSTRUCTIONS,
            context_providers=[self._skills_provider()],
            description="Designs insightful Power BI / Fabric report visuals from a semantic model.",
        )

    # -- design -----------------------------------------------------------

    async def suggest_visuals(
        self, model: SemanticModelSpec, *, extra_instructions: str | None = None
    ) -> list[VisualSuggestion]:
        """Return the agent's proposed visuals for ``model`` (may be empty)."""
        brief = build_model_brief(model)
        prompt = (
            "Design an insightful starter report for this semantic model and "
            "return ONLY the JSON object of visuals described in your "
            "instructions.\n\n"
            f"MODEL:\n{json.dumps(brief, indent=2)}"
        )
        if extra_instructions:
            prompt += f"\n\nADDITIONAL REQUIREMENTS:\n{extra_instructions}"

        credential = self._credential()
        async with credential:
            client = self._chat_client(credential)
            async with self._build_agent(client) as agent:
                result = await agent.run(prompt)
                text = _result_text(result)
        return _parse_visual_suggestions(text)

    def suggest_visuals_sync(self, *args: Any, **kwargs: Any) -> list[VisualSuggestion]:
        """Synchronous wrapper around :meth:`suggest_visuals` (for Streamlit)."""
        from .agent import _run_async

        return _run_async(self.suggest_visuals(*args, **kwargs))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def report_design_available() -> bool:
    """Public alias for :func:`is_available` (used by the UI)."""
    return is_available()


def suggest_report_with_agent(
    model: SemanticModelSpec,
    *,
    report_name: str | None = None,
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    theme: ReportTheme | None = None,
    config: ReportDesignConfig | None = None,
    extra_instructions: str | None = None,
) -> ReportBuildOutcome:
    """Design a report whose visuals are proposed by the AI agent.

    The agent suggests *what* to show; :func:`compose_report` deterministically
    grounds and lays it out. Falls back to the deterministic baseline whenever
    the agent is unavailable, errors, or returns nothing usable, so callers
    always get a valid, renderable report.
    """
    model, _ = dedupe_measure_names(model)
    baseline = deterministic_visual_suggestions(model)

    def _compose(suggestions: list[VisualSuggestion]) -> ReportSpec:
        return compose_report(
            suggestions,
            model,
            report_name=report_name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            theme=theme,
        )

    if not is_available():
        return ReportBuildOutcome(
            report=_compose(baseline),
            status="deterministic",
            suggestions=ground_visual_suggestions(baseline, model),
        )

    try:
        raw = ReportDesignIntelligence(config).suggest_visuals_sync(
            model, extra_instructions=extra_instructions
        )
    except Exception as exc:  # noqa: BLE001 - reported via outcome status
        return ReportBuildOutcome(
            report=_compose(baseline),
            status="error",
            error=str(exc),
            used_agent=True,
            suggestions=ground_visual_suggestions(baseline, model),
        )

    grounded = ground_visual_suggestions(raw[:_MAX_AGENT_VISUALS], model)
    if not grounded:
        return ReportBuildOutcome(
            report=_compose(baseline),
            status="error",
            error="The agent returned no visuals grounded in the model; "
            "showing the deterministic baseline instead.",
            used_agent=True,
            suggestions=ground_visual_suggestions(baseline, model),
        )

    return ReportBuildOutcome(
        report=_compose(grounded),
        status="ok",
        agent_visual_count=len(grounded),
        used_agent=True,
        suggestions=grounded,
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


def _parse_visual_suggestions(text: str) -> list[VisualSuggestion]:
    """Parse the agent's visuals JSON into :class:`VisualSuggestion` items.

    Tolerant: malformed entries are skipped, and every entry is tagged
    ``source="agent"`` regardless of what the model claimed.
    """
    payload = _extract_json_object(text)
    if payload is None:
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    raw_visuals = data.get("visuals") if isinstance(data, dict) else None
    if not isinstance(raw_visuals, list):
        return []
    out: list[VisualSuggestion] = []
    for item in raw_visuals:
        if not isinstance(item, dict):
            continue
        try:
            suggestion = VisualSuggestion.from_dict(item)
        except Exception:  # noqa: BLE001 - skip unparseable visuals
            continue
        suggestion.source = "agent"
        if suggestion.role_bindings:
            out.append(suggestion)
    return out


__all__ = [
    "DEFAULT_AGENT_NAME",
    "ReportBuildOutcome",
    "ReportDesignConfig",
    "ReportDesignIntelligence",
    "ReportDesignUnavailableError",
    "build_model_brief",
    "is_available",
    "report_design_available",
    "suggest_report_with_agent",
]
