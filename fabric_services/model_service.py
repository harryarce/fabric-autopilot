"""Semantic model service.

Orchestrates the full semantic-model lifecycle that previously lived in the
Streamlit *Semantic Models* and *Schema Explorer* pages:

* list / import existing models from Fabric (persisting them to the artifact
  store),
* **design** a model from a SQL schema — agent-first (Foundry) with a
  deterministic fallback,
* render the model to a TMDL/TMSL definition, and
* publish (create) it in Fabric.

The agentic path is the default because the platform prioritizes AI-assisted
design; it degrades transparently to the deterministic mapping when the Foundry
agent is unavailable or errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.artifacts import ArtifactRef, ArtifactStore
from app.fabric_client import CreatedItem, FabricClient, FabricItem
from app.intelligence import (
    DefinitionFormat,
    GeneratedMeasure,
    SemanticModelDefinition,
    SemanticModelSpec,
    SemanticModelSuggestions,
    SemanticModelValidationResult,
    SuggestedMeasure,
    SuggestedRelationship,
    SuggestionSpec,
    apply_model_suggestions,
    apply_suggestions,
    build_definition,
    dedupe_measure_names,
    drop_unresolved_measures,
    generate_dax_measure,
    parse_semantic_model,
    spec_from_schemas,
    suggest_from_schemas,
    validate_semantic_model_spec,
)
from app.intelligence import agent as agent_module
from app.intelligence.audit import (
    audit_semantic_model,
    propose_bpa_fixes,
    propose_copilot_prep_fixes,
    propose_usability_fixes,
)

from .context import TenantContext
from .errors import NotFoundError, UpstreamError, ValidationError

logger = logging.getLogger("fabric_services.model")


@dataclass
class DesignResult:
    """Outcome of designing a model from a schema."""

    spec: SemanticModelSpec
    used_agent: bool


@dataclass
class SuggestionResult:
    """Outcome of generating relationship/measure suggestions."""

    suggestions: SemanticModelSuggestions
    status: str  # "ok" | "deterministic" | "error"
    agent_contributed: int = 0
    error: str | None = None


@dataclass
class RenderResult:
    """A rendered definition plus any non-fatal warnings (dropped edges)."""

    definition: SemanticModelDefinition
    warnings: list[str]


def _all_finding_codes(reports: dict) -> set[str]:
    """Flatten finding codes across a multi-feature audit result."""
    codes: set[str] = set()
    for feature, report in reports.items():
        if feature == "semantic-model-health":  # merged view, avoid double-count
            continue
        for finding in report.findings:
            codes.add(finding.code)
    return codes


class ModelService:
    """Manage semantic models across discovery, design, render, and publish."""

    def __init__(
        self,
        fabric_client: FabricClient,
        artifact_store: ArtifactStore,
        tenant: TenantContext,
    ) -> None:
        self._fabric = fabric_client
        self._store = artifact_store
        self._tenant = tenant

    # -- discovery / import ----------------------------------------------

    def list_models(self, workspace_id: str) -> list[FabricItem]:
        try:
            return self._fabric.list_semantic_models(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to list semantic models: {exc}") from exc

    def import_model(
        self,
        workspace_id: str,
        model_id: str,
        *,
        fmt: str = "TMDL",
        workspace_name: str = "",
        item_name: str = "",
    ) -> dict[str, Any]:
        """Fetch a model definition from Fabric, persist it, and parse it.

        Returns a dict with the parsed ``spec``, the raw ``files``, and the
        ``format`` so callers can render, audit, or display it.
        """
        try:
            definition = self._fabric.get_semantic_model_definition(
                workspace_id, model_id, fmt=fmt
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to fetch semantic model definition: {exc}"
            ) from exc

        ref = ArtifactRef(
            kind="semanticModels",
            workspace_id=workspace_id,
            item_id=model_id,
            workspace_name=workspace_name,
            item_name=item_name,
        )
        self._store.save_definition(
            ref, definition.files, fmt=definition.format, source="imported"
        )
        spec = parse_semantic_model(definition.files, name=item_name or None)
        return {
            "spec": spec,
            "files": definition.files,
            "format": definition.format,
        }

    def get_cached_spec(
        self,
        workspace_id: str,
        model_id: str,
        *,
        workspace_name: str = "",
        item_name: str = "",
    ) -> SemanticModelSpec | None:
        """Return the parsed spec for a previously-imported model, or None.

        Loads the TMDL/TMSL files persisted by :meth:`import_model` from the
        artifact store and parses them into a :class:`SemanticModelSpec`.
        Returns ``None`` when the model has never been imported for this
        tenant — callers should ask the user to import it first so queries
        are built against a known, up-to-date definition.
        """
        ref = ArtifactRef(
            kind="semanticModels",
            workspace_id=workspace_id,
            item_id=model_id,
            workspace_name=workspace_name,
            item_name=item_name,
        )
        stored = self._store.load_definition(ref)
        if stored is None or not stored.files:
            return None
        try:
            return parse_semantic_model(stored.files, name=item_name or None)
        except Exception as exc:  # noqa: BLE001 - cached but corrupt: force re-import
            logger.warning(
                "Cached semantic model %s/%s failed to parse (%s); re-import required.",
                workspace_id,
                model_id,
                exc,
            )
            return None

    # -- design (agent-first) --------------------------------------------

    def design_model(
        self,
        schemas: list[Any],
        *,
        model_name: str,
        source_server: str,
        source_database: str,
        storage_mode: str = "import",
        source_kind: str = "sql",
        use_agent: bool = True,
        extra_instructions: str | None = None,
        **kwargs: Any,
    ) -> DesignResult:
        """Design a model from a SQL schema.

        Tries the Foundry agent first (when ``use_agent`` and available),
        falling back to the deterministic mapping. The agent itself also falls
        back internally, so this never fails just because AI is unavailable.
        """
        if not schemas:
            raise ValidationError("At least one table/view schema is required.")

        if use_agent and agent_module.is_available():
            try:
                intel = agent_module.SemanticModelIntelligence()
                spec = intel.design_spec_sync(
                    schemas,
                    model_name=model_name,
                    source_server=source_server,
                    source_database=source_database,
                    storage_mode=storage_mode,
                    source_kind=source_kind,
                    extra_instructions=extra_instructions,
                    **kwargs,
                )
                spec, _ = dedupe_measure_names(spec)
                self._prune_unresolved_measures(spec, model_name=model_name)
                return DesignResult(spec=spec, used_agent=True)
            except Exception as exc:  # noqa: BLE001 - degrade to deterministic
                logger.warning(
                    "AGENTIC FALLBACK: semantic-model design agent errored "
                    "(%s) - serving deterministic model %r.",
                    exc,
                    model_name,
                )
        elif use_agent:
            logger.warning(
                "AGENTIC FALLBACK: Foundry design agent unavailable - serving "
                "deterministic model %r. Configure FOUNDRY_* to enable the "
                "agentic layer.",
                model_name,
            )
        else:
            logger.info(
                "Agentic layer disabled by caller (use_agent=False) - serving "
                "deterministic model %r.",
                model_name,
            )

        spec = spec_from_schemas(
            schemas,
            model_name=model_name,
            source_server=source_server,
            source_database=source_database,
            storage_mode=storage_mode,
            source_kind=source_kind,
            **kwargs,
        )
        spec, _ = dedupe_measure_names(spec)
        self._prune_unresolved_measures(spec, model_name=model_name)
        return DesignResult(spec=spec, used_agent=False)

    def _prune_unresolved_measures(
        self, spec: SemanticModelSpec, *, model_name: str
    ) -> None:
        """Drop measures whose DAX references objects absent from the model.

        Keeps the generated model query-clean by construction (the auditor would
        otherwise flag ``measure-references-unknown-object``) and logs whatever
        was removed so the elision is visible.
        """
        _, dropped = drop_unresolved_measures(spec)
        for object_ref, reason in dropped:
            logger.warning(
                "Dropped unresolved measure %s from model %r: %s.",
                object_ref,
                model_name,
                reason,
            )

    # -- render -----------------------------------------------------------

    def build_definition(
        self, spec: SemanticModelSpec, *, fmt: str = "TMDL"
    ) -> SemanticModelDefinition:
        """Render a spec to a Fabric definition (TMDL or TMSL)."""
        try:
            definition_format = DefinitionFormat(fmt.upper())
        except ValueError as exc:
            raise NotFoundError(
                f"Unknown definition format '{fmt}'. Use TMDL or TMSL."
            ) from exc
        return build_definition(spec, definition_format)

    def render_definition(
        self, spec: SemanticModelSpec, *, fmt: str = "TMDL"
    ) -> RenderResult:
        """Render a definition and capture dropped-relationship warnings.

        ``build_definition`` removes relationships that reference unknown
        tables/columns (which would make Fabric reject the dataset) and emits a
        Python warning. We capture those so the API/UI can surface them instead
        of letting an expected edge vanish silently.
        """
        import warnings as _warnings

        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            definition = self.build_definition(spec, fmt=fmt)
        messages = [
            str(w.message)
            for w in caught
            if "Dropped" in str(w.message)
            and ("relationship" in str(w.message) or "column" in str(w.message))
        ]
        return RenderResult(definition=definition, warnings=messages)

    # -- validate ---------------------------------------------------------

    def validate_model(
        self, spec: SemanticModelSpec
    ) -> SemanticModelValidationResult:
        """Run the deterministic consistency checks used by the UI preflight."""
        return validate_semantic_model_spec(spec)

    # -- suggestions ------------------------------------------------------

    def suggest_additions(
        self,
        schemas: list[Any],
        spec: SemanticModelSpec,
        *,
        use_agent: bool = True,
        extra_instructions: str | None = None,
    ) -> SuggestionResult:
        """Suggest extra relationships and measures for ``spec``.

        Agent-first (when available), degrading to the deterministic engine. The
        returned status mirrors the legacy app so the UI can explain whether the
        agent contributed, added nothing, or errored and fell back.
        """
        if use_agent and agent_module.is_available():
            try:
                intel = agent_module.SemanticModelIntelligence()
                outcome = intel.design_suggestions_detailed_sync(
                    schemas, spec, extra_instructions=extra_instructions
                )
                deterministic = suggest_from_schemas(schemas, spec=spec)
                baseline = len(deterministic.relationships) + len(
                    deterministic.measures
                )
                total = len(outcome.suggestions.relationships) + len(
                    outcome.suggestions.measures
                )
                contributed = max(0, total - baseline)
                return SuggestionResult(
                    suggestions=outcome.suggestions,
                    status=outcome.status,
                    agent_contributed=contributed,
                    error=outcome.error,
                )
            except Exception as exc:  # noqa: BLE001 - degrade to deterministic
                return SuggestionResult(
                    suggestions=suggest_from_schemas(schemas, spec=spec),
                    status="error",
                    error=agent_module.describe_agent_error(exc),
                )
        return SuggestionResult(
            suggestions=suggest_from_schemas(schemas, spec=spec),
            status="deterministic",
        )

    def apply_model_suggestions(
        self,
        spec: SemanticModelSpec,
        *,
        relationships: list[SuggestedRelationship] | None = None,
        measures: list[SuggestedMeasure] | None = None,
    ) -> SemanticModelSpec:
        """Return a new spec with the accepted suggestions merged in.

        Newly accepted measures share the model-wide measure namespace and must
        not collide with an existing measure or a column on the same table, so
        the merged spec is deduplicated before it is returned. This keeps the
        spec deployable (Fabric rejects a measure named like a column) without
        the caller having to re-run validation.
        """
        merged = apply_suggestions(
            spec, relationships=relationships, measures=measures
        )
        merged, _ = dedupe_measure_names(merged)
        return merged

    # -- publish ----------------------------------------------------------

    def _ensure_publishable(self, spec: SemanticModelSpec) -> None:
        """Refuse to publish a spec that fails the deterministic preflight.

        Fabric reports structural problems — a model with no tables, an empty
        name, a missing source connection, relationships referencing unknown
        columns — late and often opaquely (e.g. a 500 at import time, or a
        published-but-broken dataset). Running the same checks the UI preflight
        uses here guarantees the model is consistent and complete *before* the
        side-effecting create/update call, so Agent Studio can never publish an
        empty or non-functional semantic model.
        """
        result = validate_semantic_model_spec(spec)
        errors = result.errors
        if errors:
            summary = "; ".join(f"{i.code}: {i.message}" for i in errors)
            raise ValidationError(
                "The semantic model failed preflight validation and was not "
                f"published: {summary}",
                details={
                    "issues": [
                        {
                            "code": i.code,
                            "message": i.message,
                            "object": i.object_ref,
                        }
                        for i in errors
                    ]
                },
            )

    def publish_model(
        self,
        workspace_id: str,
        display_name: str,
        spec: SemanticModelSpec,
        *,
        fmt: str = "TMDL",
        description: str | None = None,
        persist: bool = True,
    ) -> CreatedItem:
        """Create the model in Fabric from ``spec`` and optionally persist it."""
        self._ensure_publishable(spec)
        definition = self.build_definition(spec, fmt=fmt)
        try:
            created = self._fabric.create_semantic_model(
                workspace_id,
                display_name,
                definition.definition_payload(),
                description=description,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to create semantic model: {exc}") from exc

        if persist and created.id:
            ref = ArtifactRef(
                kind="semanticModels",
                workspace_id=workspace_id,
                item_id=created.id,
                item_name=display_name,
            )
            self._store.save_definition(
                ref, definition.files, fmt=definition.format.value, source="generated"
            )
        return created

    # -- update (write-back) ---------------------------------------------

    def update_model(
        self,
        workspace_id: str,
        model_id: str,
        spec: SemanticModelSpec,
        *,
        fmt: str = "TMDL",
        item_name: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Replace a published model's definition with ``spec`` (write-back).

        Renders ``spec`` to a TMDL/TMSL definition and calls Fabric's
        ``updateDefinition`` endpoint, polling the long-running operation to
        completion just like :meth:`publish_model`. When ``persist`` is true
        the freshly written definition is also saved to the artifact store so
        subsequent reads see the user's edits.
        """
        self._ensure_publishable(spec)
        definition = self.build_definition(spec, fmt=fmt)
        try:
            status = self._fabric.update_semantic_model_definition(
                workspace_id, model_id, definition.definition_payload()
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to update semantic model: {exc}") from exc

        if persist:
            ref = ArtifactRef(
                kind="semanticModels",
                workspace_id=workspace_id,
                item_id=model_id,
                item_name=item_name or spec.name,
            )
            self._store.save_definition(
                ref, definition.files, fmt=definition.format.value, source="exported"
            )
        return {
            "status": status,
            "format": definition.format.value,
            "fileCount": len(definition.files),
        }

    # -- audit suggestion remediation -------------------------------------

    def propose_usability_fixes(
        self, spec: SemanticModelSpec
    ) -> list[SuggestionSpec]:
        """Generate deterministic usability write-back suggestions for ``spec``."""
        return propose_usability_fixes(spec)

    def propose_copilot_prep(
        self, spec: SemanticModelSpec
    ) -> list[SuggestionSpec]:
        """Generate Copilot-readiness write-back suggestions for ``spec``."""
        return propose_copilot_prep_fixes(spec)

    def propose_bpa_fixes(
        self, spec: SemanticModelSpec
    ) -> list[SuggestionSpec]:
        """Generate Best Practice Analyzer write-back suggestions for ``spec``."""
        return propose_bpa_fixes(spec)

    def propose_ai_fixes(
        self, spec: SemanticModelSpec
    ) -> tuple[list[SuggestionSpec], str, str | None]:
        """Generate AI-enhanced description / synonym suggestions for ``spec``.

        Uses the Foundry agent to write safe, additive business descriptions and
        Q&A synonyms for objects that lack them. Returns
        ``(suggestions, status, error)`` where ``status`` is ``"ok"`` (agent
        replied), ``"error"`` (agent failed; ``error`` carries the reason) or
        ``"unavailable"`` (the Agent Framework is not installed). Never raises.
        """
        if not agent_module.is_available():
            return (
                [],
                "unavailable",
                "The Microsoft Agent Framework is not installed, so AI-enhanced "
                "fixes are unavailable. Deterministic fixes are still offered.",
            )
        try:
            intel = agent_module.SemanticModelIntelligence()
            outcome = intel.enrich_audit_fixes_detailed_sync(spec)
            return outcome.suggestions, outcome.status, outcome.error
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            return [], "error", agent_module.describe_agent_error(exc)

    def apply_audit_suggestions(
        self,
        spec: SemanticModelSpec,
        suggestions: list[SuggestionSpec],
    ) -> tuple[SemanticModelSpec, list[SuggestionSpec]]:
        """Apply accepted audit suggestions to ``spec`` (non-destructive)."""
        return apply_model_suggestions(spec, suggestions)

    def generate_dax_measure(
        self,
        spec: SemanticModelSpec,
        intent: str,
        *,
        sample_rows: list[dict[str, Any]] | None = None,
    ) -> GeneratedMeasure:
        """Generate a candidate DAX measure for a natural-language intent."""
        return generate_dax_measure(spec, intent, sample_rows=sample_rows)

    def verify_update(
        self,
        workspace_id: str,
        model_id: str,
        *,
        before: SemanticModelSpec | None = None,
        features: list[str] | None = None,
        fmt: str = "TMDL",
    ) -> dict[str, Any]:
        """Re-fetch the published model and produce a verification diff.

        Runs the audit suite on the re-fetched spec, and (when ``before`` is
        supplied) the same suite on the pre-update spec, then returns the
        delta of finding codes closed vs introduced. Lets callers confirm a
        write-back actually moved the needle.
        """
        try:
            definition = self._fabric.get_semantic_model_definition(
                workspace_id, model_id, fmt=fmt
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to fetch semantic model for verification: {exc}"
            ) from exc

        after_spec = parse_semantic_model(definition.files, name=None)
        after_reports = audit_semantic_model(after_spec, features=features)
        after_codes = _all_finding_codes(after_reports)

        result: dict[str, Any] = {
            "after": {
                feature: report.to_dict() for feature, report in after_reports.items()
            },
            "after_finding_count": len(after_codes),
        }
        if before is not None:
            before_reports = audit_semantic_model(before, features=features)
            before_codes = _all_finding_codes(before_reports)
            result["before_finding_count"] = len(before_codes)
            result["closed_codes"] = sorted(before_codes - after_codes)
            result["introduced_codes"] = sorted(after_codes - before_codes)
        return result
