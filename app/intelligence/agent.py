"""Microsoft Agent Framework integration for semantic-model design.

This module is the AI layer on top of the deterministic engine. It uses the
**latest Microsoft Agent Framework** (`agent-framework` + the Foundry
connector) to build an agent that:

* is backed by a Microsoft Foundry model (:class:`FoundryChatClient`),
* is equipped with the local ``semantic-model-builder`` skill via
  :class:`SkillsProvider`, and
* can design a semantic model from a SQL schema brief.

It also supports **remote (hosted) agents**: :meth:`SemanticModelIntelligence.publish_remote_agent`
serialises the locally-defined agent into a Foundry *prompt agent* definition
(:func:`to_prompt_agent`) and creates/updates it in the Foundry project with
``AIProjectClient.agents.create_version`` so it can be reused outside this app.

All Agent Framework imports are **lazy** (done inside functions) so the rest of
the application — and the deterministic engine — keep working even when
``agent-framework`` is not installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .bpa import best_practice_checklist
from .definition import DefinitionFormat, build_definition
from .spec import (
    SemanticModelSpec,
    SemanticModelSuggestions,
    SuggestedMeasure,
    SuggestedRelationship,
    spec_from_schemas,
    suggest_from_schemas,
)
from .suggestions import SuggestionSpec

_LOGGER = logging.getLogger("app.intelligence.agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Defaults are intentionally empty so the agentic layer stays dormant unless
# the operator explicitly wires it up via environment variables. Without
# ``FOUNDRY_PROJECT_ENDPOINT`` / ``FOUNDRY_MODEL`` set, the deterministic
# fallback runs end-to-end (see module docstring and README).
DEFAULT_PROJECT_ENDPOINT = ""
DEFAULT_MODEL = ""
DEFAULT_AGENT_NAME = "semantic-model-architect"

# The agent's skills now live with the rest of the platform's agent skills under
# ``fabric_api/agents/skills`` (single home, so the web "Agent Studio" and this
# architect load from the same place). We resolve that location by *path* from
# the repo root — no ``fabric_api`` import, which would invert the dependency
# direction — and fall back to the legacy in-package location if it is absent
# (e.g. ``app`` packaged standalone without ``fabric_api``). The provider is
# pointed at the specific skill directory so the architect only ever discovers
# ``semantic-model-builder`` (not the team's other overlay skills).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FABRIC_API_SKILL = (
    _REPO_ROOT / "fabric_api" / "agents" / "skills" / "semantic-model-builder"
)
_LEGACY_SKILL = Path(__file__).resolve().parent / "skills" / "semantic-model-builder"
_SKILLS_DIR = _FABRIC_API_SKILL if _FABRIC_API_SKILL.exists() else _LEGACY_SKILL


def _env_or_default(name: str, default: str) -> str:
    """Return the environment variable ``name`` when set and non-empty.

    Azure environment variables are prioritized across the board, but the
    deployment always *injects* the ``FOUNDRY_*`` vars (often as empty strings
    when the operator left them blank). A plain ``os.getenv(name, default)``
    would treat that empty string as a real value and skip the default, so we
    coalesce blank/whitespace-only values back to ``default``.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


_INSTRUCTIONS = """\
You are a senior Microsoft Fabric / Power BI data-modeling architect.

Your job is to turn a relational SQL schema (tables, columns, primary keys and
foreign keys) into a well-formed tabular *semantic model* and emit the exact
JSON spec that the deterministic generator consumes.

Always load and follow the `semantic-model-builder` skill. Apply dimensional
modeling: classify fact vs. dimension tables, build a star schema from the
foreign keys (one active relationship per table pair), hide surrogate keys,
identify and mark a date table, and propose useful DAX measures with sensible
format strings.

Make the model self-documenting: populate a clear, business-readable
`description` on every table and on every column (and on each measure). Keep the
table, column and measure NAMES exactly as supplied — do not rename or drop them.
You MAY (and should) ADD new measures, mark a date table, and set descriptions;
just never rename or remove the supplied tables, columns, or their source
bindings.

If the user supplies ADDITIONAL REQUIREMENTS, treat them as mandatory: action
every one of them in the model you return (for example, add the requested
measures, mark the requested date table, etc.). Only skip a requirement if it is
impossible given the supplied schema, and never silently ignore it.

When you are asked for the model, respond with a single JSON object that
conforms to the skill's spec schema and nothing else. Preserve the
`source_schema`, `source_table` and `source_column` values exactly as given so
the generated partitions bind to the real tables.

When you are asked for SUGGESTIONS (relationships and measures), respond with a
single JSON object of shape:
{
  "relationships": [
    {
      "relationship": {"from_table": "...", "from_column": "...",
                       "to_table":   "...", "to_column":   "...",
                       "from_cardinality": "many", "to_cardinality": "one",
                       "cross_filtering_behavior": "oneDirection",
                       "is_active": true},
      "rationale": "Short reason a human can verify.",
      "confidence": 0.0..1.0,
      "source": "agent"
    }
  ],
  "measures": [
    {
      "table": "<existing model table name>",
      "measure": {"name": "...", "expression": "DAX",
                  "format_string": "...", "display_folder": "...",
                  "description": "..."},
      "rationale": "Why this measure is useful.",
      "confidence": 0.0..1.0,
      "source": "agent"
    }
  ]
}
Use the SAME table and column names that appear in the supplied spec (these are
the model-friendly names already accepted by the user). Do not invent columns.
"""


def _instructions_with_best_practices() -> str:
    """Return the base instructions plus the Microsoft BPA design checklist.

    The checklist names come straight from the bundled ``BPARules.json`` so the
    agent applies the same Best Practice Analyzer guidance that the audit
    enforces. Degrades to the base instructions if the catalog is unavailable.
    """
    checklist = best_practice_checklist()
    if not checklist:
        return _INSTRUCTIONS
    return (
        _INSTRUCTIONS
        + "\n\nBEST PRACTICES (Microsoft Best Practice Analyzer — apply every one "
        "that is relevant when crafting the model):\n"
        + checklist
        + "\n\nFollow these proactively: give every visible measure a format "
        "string, set numeric column summarization to None, hide foreign keys, "
        "mark primary keys, keep relationship columns the same (preferably "
        "Int64) type, ensure a date table is marked, avoid Double columns, use "
        "DIVIDE() for division, and write a description for every visible object."
    )


class IntelligenceUnavailableError(RuntimeError):
    """Raised when the Microsoft Agent Framework is not installed."""


def is_available() -> bool:
    """Return ``True`` when the Agent Framework + Foundry connector are importable."""
    try:  # pragma: no cover - trivial import probe
        import agent_framework  # noqa: F401
        from agent_framework.foundry import FoundryChatClient  # noqa: F401

        return True
    except Exception:
        return False


def is_permission_error(exc: BaseException) -> bool:
    """Heuristically detect an Azure RBAC / authorization failure.

    The Foundry connector surfaces these as a ``PermissionDeniedError`` (HTTP
    403) whose payload mentions a missing
    ``Microsoft.MachineLearningServices/workspaces/agents/action`` permission.
    We match defensively on the wire text so we don't depend on a specific SDK
    exception class being importable.
    """
    text = str(exc).lower()
    return (
        "403" in text
        or "permissiondenied" in text
        or "forbiddenerror" in text
        or "does not have permissions" in text
        or "azureml-auth-troubleshooting" in text
    )


def describe_agent_error(exc: BaseException) -> str:
    """Turn a raw agent exception into a concise, actionable one-liner.

    Permission failures get a clear remediation hint (grant the identity the
    role that allows running agents); everything else is trimmed to its first
    line so the UI never shows a multi-kilobyte JSON blob.
    """
    text = str(exc)
    if is_permission_error(exc):
        match = re.search(r"object id:\s*([0-9a-fA-F-]+)", text)
        who = f" (object id {match.group(1)})" if match else ""
        return (
            "The Foundry identity"
            f"{who} isn't authorized to run agents. Grant it the "
            "'Azure AI Developer' role (or any role permitting "
            "Microsoft.MachineLearningServices/workspaces/agents/action) on the "
            "Foundry project, then retry. "
            "See https://aka.ms/azureml-auth-troubleshooting."
        )
    first_line = text.strip().splitlines()[0] if text.strip() else text
    return first_line[:300]


# ---------------------------------------------------------------------------
# Schema brief (reusable agent input)
# ---------------------------------------------------------------------------


def build_schema_brief(schemas: Iterable[Any]) -> dict[str, Any]:
    """Build a compact, token-efficient description of the selected tables.

    Duck-typed against :class:`app.sql_client.TableSchema` to avoid an import
    cycle. The result is JSON-serialisable and given to the agent as context.
    """
    brief: list[dict[str, Any]] = []
    for s in schemas:
        brief.append(
            {
                "schema": s.schema,
                "table": s.name,
                "type": getattr(s, "object_type", "TABLE"),
                "columns": [
                    {
                        "name": c.name,
                        "sql_type": c.type_display
                        if hasattr(c, "type_display")
                        else c.data_type,
                        "nullable": bool(c.is_nullable),
                        "primary_key": bool(getattr(c, "is_primary_key", False)),
                    }
                    for c in s.columns
                ],
                "foreign_keys": [
                    {
                        "column": fk.column,
                        "references": f"{fk.references_schema}.{fk.references_table}"
                        f".{fk.references_column}",
                    }
                    for fk in getattr(s, "foreign_keys", [])
                ],
            }
        )
    return {"tables": brief}


# ---------------------------------------------------------------------------
# File-skill script runner (subprocess)
# ---------------------------------------------------------------------------


def _subprocess_script_runner(skill: Any, script: Any, args: list[str]) -> str:
    """Run a file-based skill script in a subprocess and return its stdout.

    Matches the ``script_runner`` contract expected by ``SkillsProvider``.
    """
    import subprocess
    import sys

    script_path = Path(script.full_path)
    completed = subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(script_path.parent),
    )
    if completed.returncode != 0:
        return (
            f"Script failed (exit {completed.returncode}).\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed.stdout


# ---------------------------------------------------------------------------
# Intelligence facade
# ---------------------------------------------------------------------------


@dataclass
class SuggestionOutcome:
    """Result of a suggestion run plus diagnostics for surfacing in the UI.

    ``suggestions`` is always usable (the deterministic baseline at minimum).
    ``status`` is one of:

    * ``"ok"``        – the agent replied and its items were merged in.
    * ``"error"``     – the agent errored or replied with unparseable output;
                        ``error`` holds the message and the deterministic
                        baseline is returned as a fallback.
    * ``"deterministic"`` – the agent was never invoked (deterministic mode).
    """

    suggestions: SemanticModelSuggestions
    status: str = "deterministic"
    error: str | None = None

    @property
    def agent_contributed(self) -> int:
        """Number of merged items whose provenance is the agent."""
        rels = sum(1 for s in self.suggestions.relationships if s.source == "agent")
        meas = sum(1 for s in self.suggestions.measures if s.source == "agent")
        return rels + meas


@dataclass
class AiEnrichmentOutcome:
    """Result of an AI audit-enrichment run.

    ``suggestions`` is a list of :class:`SuggestionSpec` (``kind="model_ai"``,
    ``source="agent"``) describing safe, additive description/synonym
    write-backs. ``status`` is ``"ok"`` when the agent replied (even with zero
    items) or ``"error"`` when it failed, in which case ``error`` carries a
    concise reason and ``suggestions`` is empty.
    """

    suggestions: list[SuggestionSpec]
    status: str = "ok"
    error: str | None = None


@dataclass
class IntelligenceConfig:
    """Connection settings for the Foundry-backed agent."""

    project_endpoint: str = ""
    model: str = ""
    agent_name: str = DEFAULT_AGENT_NAME

    @classmethod
    def from_env(cls) -> "IntelligenceConfig":
        # Azure environment variables are prioritized across the board: when the
        # app's ``FOUNDRY_*`` vars are present AND non-empty they win, otherwise
        # we fall back to the baked-in defaults. Treating empty as unset matters
        # because the deployment (Bicep / azd) always *injects* these vars, and
        # leaves them as empty strings when the operator hasn't supplied a value
        # — a bare ``os.getenv(name, default)`` would then return "" and bypass
        # the default entirely. We deliberately do NOT read the generic
        # ``AZURE_AI_MODEL_DEPLOYMENT_NAME`` here: the endpoint has no matching
        # generic fallback, so honouring a stray model var from another project
        # would pair a foreign deployment name with this app's endpoint and
        # raise ``DeploymentNotFound``. Keeping endpoint + model in one
        # namespace guarantees they always belong to the same resource.
        return cls(
            project_endpoint=_env_or_default("FOUNDRY_PROJECT_ENDPOINT", DEFAULT_PROJECT_ENDPOINT),
            model=_env_or_default("FOUNDRY_MODEL", DEFAULT_MODEL),
            agent_name=_env_or_default("FOUNDRY_AGENT_NAME", DEFAULT_AGENT_NAME),
        )


class SemanticModelIntelligence:
    """Reusable facade over the Agent Framework for semantic-model design.

    Example
    -------
    >>> intel = SemanticModelIntelligence()
    >>> spec = intel.design_spec_sync(schemas, model_name="Sales",
    ...                               source_server=ep.server, source_database=ep.database)
    >>> definition = build_definition(spec)
    """

    def __init__(self, config: IntelligenceConfig | None = None) -> None:
        self.config = config or IntelligenceConfig.from_env()
        if not is_available():
            raise IntelligenceUnavailableError(
                "Microsoft Agent Framework is not installed. "
                "Install it with: pip install agent-framework agent-framework-foundry"
            )

    # -- internal builders ------------------------------------------------

    def _credential(self):
        import os

        from azure.identity.aio import DefaultAzureCredential

        client_id = os.environ.get("AZURE_CLIENT_ID") or None
        return DefaultAzureCredential(
            managed_identity_client_id=client_id,
            exclude_interactive_browser_credential=True,
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

        # Skills are an experimental feature; silence the FutureWarning banner.
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
            instructions=_instructions_with_best_practices(),
            context_providers=[self._skills_provider()],
            description="Designs Power BI / Fabric semantic models from SQL schemas.",
        )

    # -- design -----------------------------------------------------------

    async def design_spec(
        self,
        schemas: Iterable[Any],
        *,
        model_name: str,
        source_server: str | None = None,
        source_database: str | None = None,
        storage_mode: str = "directQuery",
        source_kind: str = "sql",
        lakehouse_id: str | None = None,
        lakehouse_name: str | None = None,
        onelake_workspace_id: str | None = None,
        onelake_tables_path: str | None = None,
        default_schema: str | None = None,
        direct_lake_mode: str = "auto",
        extra_instructions: str | None = None,
    ) -> SemanticModelSpec:
        """Ask the agent to design a semantic model and return the parsed spec.

        Falls back to the deterministic mapping if the agent cannot be reached
        or returns unusable output, so callers always get a usable spec.
        """
        deterministic = spec_from_schemas(
            schemas,
            model_name=model_name,
            source_server=source_server,
            source_database=source_database,
            storage_mode=storage_mode,
            source_kind=source_kind,
            lakehouse_id=lakehouse_id,
            lakehouse_name=lakehouse_name,
            onelake_workspace_id=onelake_workspace_id,
            onelake_tables_path=onelake_tables_path,
            default_schema=default_schema,
            direct_lake_mode=direct_lake_mode,
        )
        brief = build_schema_brief(schemas)
        source_label = (
            f"Fabric Lakehouse '{lakehouse_name or model_name}' (Direct Lake on OneLake)"
            if source_kind == "lakehouse"
            else "SQL analytics endpoint"
        )
        prompt = (
            "Design a semantic model named "
            f"'{model_name}' from this {source_label} schema. "
            f"storage_mode='{storage_mode}'.\n"
            f"source_server={source_server!r}, source_database={source_database!r}.\n"
            "Return ONLY the JSON spec.\n\n"
            f"SCHEMA:\n{json.dumps(brief, indent=2)}"
        )
        if extra_instructions and extra_instructions.strip():
            prompt += (
                "\n\nADDITIONAL REQUIREMENTS (mandatory — action every one of "
                f"these in the returned model):\n{extra_instructions.strip()}"
            )

        credential = self._credential()
        try:
            async with credential:
                client = self._chat_client(credential)
                async with self._build_agent(client) as agent:
                    result = await agent.run(prompt)
                    text = _result_text(result)
            spec = _parse_spec(text)
        except Exception as exc:
            _LOGGER.warning(
                "AGENTIC FALLBACK: design agent could not be reached for model "
                "%r (%s) - using deterministic mapping.",
                model_name,
                describe_agent_error(exc),
            )
            return deterministic

        if spec is None:
            _LOGGER.warning(
                "AGENTIC FALLBACK: design agent returned unusable output for "
                "model %r - using deterministic mapping.",
                model_name,
            )
            return deterministic
        # Always honour the real connection details so partitions stay deployable.
        spec.source_server = source_server
        spec.source_database = source_database
        spec.storage_mode = storage_mode
        spec.source_kind = source_kind
        spec.lakehouse_id = lakehouse_id
        spec.lakehouse_name = lakehouse_name
        spec.onelake_workspace_id = onelake_workspace_id
        spec.onelake_tables_path = onelake_tables_path
        spec.default_schema = default_schema
        spec.direct_lake_mode = direct_lake_mode
        if not spec.name:
            spec.name = model_name
        # Guarantee a self-documenting model: backfill any table/column
        # description the agent left blank from the deterministic baseline.
        _backfill_descriptions(spec, deterministic)
        return spec

    def design_spec_sync(self, *args: Any, **kwargs: Any) -> SemanticModelSpec:
        """Synchronous wrapper around :meth:`design_spec` (for Streamlit)."""
        return _run_async(self.design_spec(*args, **kwargs))

    # -- suggestions ------------------------------------------------------

    async def design_suggestions_detailed(
        self,
        schemas: Iterable[Any],
        spec: SemanticModelSpec,
        *,
        extra_instructions: str | None = None,
    ) -> "SuggestionOutcome":
        """Like :meth:`design_suggestions` but reports what actually happened.

        Always returns a usable :class:`SemanticModelSuggestions` (the
        deterministic baseline at minimum) wrapped in a
        :class:`SuggestionOutcome` whose ``status`` tells the caller whether
        the agent contributed, added nothing new, or errored and fell back.
        """
        schemas = list(schemas)
        deterministic = suggest_from_schemas(schemas, spec=spec)
        brief = build_schema_brief(schemas)
        spec_summary = _spec_summary_for_prompt(spec)
        prompt = (
            "Suggest extra RELATIONSHIPS and DAX MEASURES that would improve "
            "the supplied semantic model. Return ONLY the JSON object described "
            "in the instructions. Do not repeat items already in the spec.\n\n"
            f"CURRENT SPEC SUMMARY:\n{json.dumps(spec_summary, indent=2)}\n\n"
            f"SOURCE SCHEMA:\n{json.dumps(brief, indent=2)}"
        )
        if extra_instructions:
            prompt += f"\n\nADDITIONAL REQUIREMENTS:\n{extra_instructions}"

        credential = self._credential()
        try:
            async with credential:
                client = self._chat_client(credential)
                async with self._build_agent(client) as agent:
                    result = await agent.run(prompt)
                    text = _result_text(result)
            ai = _parse_suggestions(text)
        except Exception as exc:  # noqa: BLE001 - reported via outcome status
            return SuggestionOutcome(
                suggestions=deterministic,
                status="error",
                error=describe_agent_error(exc),
            )

        if ai is None:
            return SuggestionOutcome(
                suggestions=deterministic,
                status="error",
                error="The agent reply could not be parsed as JSON; "
                "showing deterministic suggestions instead.",
            )
        merged = _merge_suggestions(deterministic, ai)
        return SuggestionOutcome(suggestions=merged, status="ok")

    async def design_suggestions(
        self,
        schemas: Iterable[Any],
        spec: SemanticModelSpec,
        *,
        extra_instructions: str | None = None,
    ) -> SemanticModelSuggestions:
        """Ask the agent for relationship + measure SUGGESTIONS for ``spec``.

        Falls back to the deterministic engine (:func:`suggest_from_schemas`)
        on any failure so callers always get something to show the user.
        The two sets are merged so the user sees both deterministic and
        AI-flagged suggestions in one picker, with the agent set tagged
        ``source="agent"``.
        """
        outcome = await self.design_suggestions_detailed(
            schemas, spec, extra_instructions=extra_instructions
        )
        return outcome.suggestions

    def design_suggestions_detailed_sync(
        self, *args: Any, **kwargs: Any
    ) -> "SuggestionOutcome":
        """Synchronous wrapper around :meth:`design_suggestions_detailed`."""
        return _run_async(self.design_suggestions_detailed(*args, **kwargs))

    def design_suggestions_sync(self, *args: Any, **kwargs: Any) -> SemanticModelSuggestions:
        """Synchronous wrapper around :meth:`design_suggestions`."""
        return _run_async(self.design_suggestions(*args, **kwargs))

    # -- AI-enhanced audit remediation ------------------------------------

    async def enrich_audit_fixes_detailed(
        self, spec: SemanticModelSpec, *, max_objects: int = 60
    ) -> "AiEnrichmentOutcome":
        """Generate business-friendly descriptions + Q&A synonyms via the agent.

        Targets only *safe, additive* fields — it never renames objects or
        rewrites DAX. For each visible table / column / measure that lacks a
        description (and the model itself), the agent is asked for a one-line
        business description and, where useful, Q&A synonyms. Synonyms are folded
        into the description as a ``Synonyms: a, b, c`` suffix (the convention the
        usability auditor recognises), so a single ``description`` write-back
        closes both the "no description" and "no synonyms" findings.

        Always returns an :class:`AiEnrichmentOutcome`; on any agent failure the
        suggestion list is empty and ``status="error"`` carries the reason.
        """
        targets = _collect_enrichment_targets(spec, max_objects=max_objects)
        if not targets:
            return AiEnrichmentOutcome(suggestions=[], status="ok")

        prompt = (
            "For each object below, write a concise, business-friendly one-line "
            "DESCRIPTION, and (only when natural) up to 4 Q&A SYNONYMS a business "
            "user might say. Do NOT rename anything. Return ONLY a JSON object of "
            'the form {"items": [{"object_ref": "...", "description": "...", '
            '"synonyms": ["...", "..."]}]} using the exact object_ref values '
            "given.\n\n"
            f"MODEL: {spec.name}\n"
            f"OBJECTS:\n{json.dumps(targets, indent=2)}"
        )

        credential = self._credential()
        try:
            async with credential:
                client = self._chat_client(credential)
                async with self._build_agent(client) as agent:
                    result = await agent.run(prompt)
                    text = _result_text(result)
            items = _parse_enrichment(text)
        except Exception as exc:  # noqa: BLE001 - reported via outcome status
            return AiEnrichmentOutcome(
                suggestions=[], status="error", error=describe_agent_error(exc)
            )

        if items is None:
            return AiEnrichmentOutcome(
                suggestions=[],
                status="error",
                error="The agent reply could not be parsed as JSON.",
            )

        valid_refs = {t["object_ref"] for t in targets}
        model_ref = spec.name if not spec.description else None
        suggestions = _enrichment_to_suggestions(items, valid_refs, model_ref)
        return AiEnrichmentOutcome(suggestions=suggestions, status="ok")

    def enrich_audit_fixes_detailed_sync(
        self, *args: Any, **kwargs: Any
    ) -> "AiEnrichmentOutcome":
        """Synchronous wrapper around :meth:`enrich_audit_fixes_detailed`."""
        return _run_async(self.enrich_audit_fixes_detailed(*args, **kwargs))

    # -- remote (hosted) agent -------------------------------------------

    async def publish_remote_agent(
        self, description: str | None = None
    ) -> dict[str, Any]:
        """Publish the locally-defined agent as a remote Foundry prompt agent.

        Returns a small dict describing the created agent version.
        """
        from agent_framework.foundry import to_prompt_agent
        from azure.ai.projects.aio import AIProjectClient

        credential = self._credential()
        async with credential:
            client = self._chat_client(credential)
            async with self._build_agent(client) as agent:
                definition = to_prompt_agent(agent)

            project_client = AIProjectClient(
                endpoint=self.config.project_endpoint, credential=credential
            )
            async with project_client:
                created = await project_client.agents.create_version(
                    agent_name=self.config.agent_name,
                    definition=definition,
                    description=description
                    or "Designs Fabric semantic models from SQL schemas.",
                )
        return {
            "agent_name": self.config.agent_name,
            "version": getattr(created, "version", None),
            "id": getattr(created, "id", None),
        }

    def publish_remote_agent_sync(self, description: str | None = None) -> dict[str, Any]:
        """Synchronous wrapper around :meth:`publish_remote_agent`."""
        return _run_async(self.publish_remote_agent(description))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro: Any) -> Any:
    """Run ``coro`` on a dedicated event loop, draining pending transports.

    Centralises the sync->async bridge used by the Streamlit callers. On Windows
    the default Proactor event loop schedules subprocess pipe shutdown callbacks
    (from ``AzureCliCredential`` and the Foundry HTTP client) that, with the bare
    ``asyncio.run`` helper, can fire *after* the loop is already closed. That
    produces noisy ``RuntimeError: Event loop is closed`` and ``I/O operation on
    closed pipe`` tracebacks from transport ``__del__`` during garbage
    collection. Running the loop briefly after the coroutine completes lets those
    connection-lost callbacks drain while the loop is still alive.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Give proactor subprocess transports a chance to finish closing so
            # their connection-lost callbacks run before the loop is torn down.
            loop.run_until_complete(asyncio.sleep(0.05))
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _result_text(result: Any) -> str:
    """Extract the assistant text from an Agent Framework run result."""
    if result is None:
        return ""
    for attr in ("text", "content", "output_text"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return str(result)


def _collect_enrichment_targets(
    spec: SemanticModelSpec, *, max_objects: int
) -> list[dict[str, Any]]:
    """List visible objects lacking a description as agent enrichment targets.

    Each target carries its ``object_ref`` (the exact reference the apply engine
    expects), a ``kind`` and the object's ``name`` so the agent has enough
    context to write a meaningful description without renaming anything.
    """
    targets: list[dict[str, Any]] = []
    if not spec.description:
        targets.append(
            {"object_ref": spec.name, "kind": "model", "name": spec.name}
        )
    for table in spec.tables:
        if table.is_hidden:
            continue
        if not table.description:
            targets.append(
                {"object_ref": table.name, "kind": "table", "name": table.name}
            )
        for col in table.columns:
            if col.is_hidden or col.description:
                continue
            targets.append(
                {
                    "object_ref": f"{table.name}[{col.name}]",
                    "kind": "column",
                    "name": col.name,
                    "data_type": col.data_type,
                }
            )
        for measure in table.measures:
            if measure.description:
                continue
            targets.append(
                {
                    "object_ref": f"{table.name}[{measure.name}]",
                    "kind": "measure",
                    "name": measure.name,
                    "expression": measure.expression,
                }
            )
        if len(targets) >= max_objects:
            break
    return targets[:max_objects]


def _parse_enrichment(text: str) -> list[dict[str, Any]] | None:
    """Parse the agent's enrichment JSON into a list of item dicts.

    Tolerates fenced code blocks and surrounding prose. Returns ``None`` when no
    JSON object can be recovered.
    """
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
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def _enrichment_to_suggestions(
    items: list[dict[str, Any]], valid_refs: set[str], model_ref: str | None
) -> list[SuggestionSpec]:
    """Turn parsed enrichment items into ``model_ai`` description suggestions.

    Synonyms are folded into the description as a ``Synonyms: a, b, c`` suffix
    (the convention the usability auditor recognises) so one ``description``
    write-back closes both the missing-description and missing-synonym findings.
    Items whose ``object_ref`` is not in ``valid_refs`` (i.e. the agent invented
    a target) are dropped. The item matching ``model_ref`` writes back to the
    model-level ``model.description`` field; everything else uses ``description``.
    """
    out: list[SuggestionSpec] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = item.get("object_ref")
        if ref not in valid_refs:
            continue
        description = (item.get("description") or "").strip()
        synonyms = item.get("synonyms") or []
        if isinstance(synonyms, str):
            synonyms = [synonyms]
        synonyms = [str(s).strip() for s in synonyms if str(s).strip()]
        if not description and not synonyms:
            continue
        proposed = description
        if synonyms:
            suffix = "Synonyms: " + ", ".join(synonyms)
            proposed = f"{description} {suffix}".strip() if description else suffix
        field_name = "model.description" if ref == model_ref else "description"
        out.append(
            SuggestionSpec(
                kind="model_ai",
                object_ref=ref,
                field=field_name,
                current_value=None,
                proposed_value=proposed,
                rationale="AI-generated business description"
                + (" and Q&A synonyms." if synonyms else "."),
                code="SM_AI_DESCRIPTION",
                source="agent",
                confidence=0.6,
            )
        )
    return out


def _backfill_descriptions(
    spec: SemanticModelSpec, baseline: SemanticModelSpec
) -> None:
    """Fill any missing table/column descriptions from the deterministic spec.

    The agent is instructed to self-document every field, but if it omits a
    `description` we copy the deterministic one (matched on source bindings, with
    a name fallback) so the generated model is never left undocumented.
    """
    base_tables: dict[tuple[str, str], Any] = {}
    base_tables_by_name: dict[str, Any] = {}
    for bt in baseline.tables:
        base_tables[(bt.source_schema, bt.source_table)] = bt
        base_tables_by_name[bt.name] = bt

    for table in spec.tables:
        bt = base_tables.get((table.source_schema, table.source_table))
        if bt is None:
            bt = base_tables_by_name.get(table.name)
        if bt is None:
            continue
        if not table.description and bt.description:
            table.description = bt.description
        base_cols = {c.source_column: c for c in bt.columns}
        base_cols_by_name = {c.name: c for c in bt.columns}
        for col in table.columns:
            if col.description:
                continue
            bc = base_cols.get(col.source_column) or base_cols_by_name.get(col.name)
            if bc is not None and bc.description:
                col.description = bc.description


def _parse_spec(text: str) -> SemanticModelSpec | None:
    """Parse the agent's JSON (possibly fenced) into a spec; ``None`` on failure."""
    if not text:
        return None
    cleaned = text.strip()
    if "```" in cleaned:
        # Pull the first fenced block.
        start = cleaned.find("```")
        fence_end = cleaned.find("\n", start)
        end = cleaned.find("```", fence_end + 1)
        if fence_end != -1 and end != -1:
            cleaned = cleaned[fence_end + 1 : end].strip()
    # Fall back to the outermost JSON object.
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first == -1 or last == -1:
            return None
        cleaned = cleaned[first : last + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    try:
        return SemanticModelSpec.from_dict(data)
    except Exception:
        return None


def _parse_suggestions(text: str) -> SemanticModelSuggestions | None:
    """Parse the agent's suggestions JSON (possibly fenced)."""
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
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    try:
        # Tag every entry with ``source="agent"`` regardless of what the model
        # claimed so the UI can attribute trust correctly.
        for item in data.get("relationships", []):
            item.setdefault("source", "agent")
        for item in data.get("measures", []):
            item.setdefault("source", "agent")
        return SemanticModelSuggestions.from_dict(data)
    except Exception:
        return None


def _spec_summary_for_prompt(spec: SemanticModelSpec) -> dict[str, Any]:
    """Compact view of the model the agent should respect (no measures spam)."""
    return {
        "name": spec.name,
        "storage_mode": spec.storage_mode,
        "tables": [
            {
                "name": t.name,
                "is_date_table": t.is_date_table,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "is_key": c.is_key,
                        "is_hidden": c.is_hidden,
                    }
                    for c in t.columns
                ],
                "existing_measure_names": [m.name for m in t.measures],
            }
            for t in spec.tables
        ],
        "existing_relationships": [
            {
                "from_table": r.from_table,
                "from_column": r.from_column,
                "to_table": r.to_table,
                "to_column": r.to_column,
                "is_active": r.is_active,
            }
            for r in spec.relationships
        ],
    }


def _merge_suggestions(
    deterministic: SemanticModelSuggestions,
    ai: SemanticModelSuggestions,
) -> SemanticModelSuggestions:
    """Union deterministic and AI suggestions, deduplicating by stable key.

    Deterministic entries win when both engines propose the same item (FK >
    PK-match > suffix > AI), because their provenance is verifiable.
    """
    out_rels: dict[str, SuggestedRelationship] = {
        s.key: s for s in deterministic.relationships
    }
    for s in ai.relationships:
        out_rels.setdefault(s.key, s)
    out_measures: dict[str, SuggestedMeasure] = {
        s.key: s for s in deterministic.measures
    }
    for s in ai.measures:
        out_measures.setdefault(s.key, s)
    return SemanticModelSuggestions(
        relationships=list(out_rels.values()),
        measures=list(out_measures.values()),
    )


__all__ = [
    "DefinitionFormat",
    "IntelligenceConfig",
    "IntelligenceUnavailableError",
    "SemanticModelIntelligence",
    "SemanticModelSuggestions",
    "SuggestedMeasure",
    "SuggestedRelationship",
    "SuggestionOutcome",
    "build_definition",
    "build_schema_brief",
    "is_available",
]
