"""Report service.

Orchestrates the report lifecycle from the Streamlit *Reports* page:

* list / import existing reports (persisting the PBIR definition),
* **suggest** a starter report from a semantic model — agent-first with a
  deterministic fallback,
* render a report spec to a PBIR definition, and
* publish (create) it in Fabric.

As with the model service, the agentic design path is the default and degrades
cleanly to the deterministic baseline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from app.artifacts import ArtifactRef, ArtifactStore
from app.fabric_client import CreatedItem, FabricClient, FabricItem
from app.intelligence import (
    ReportDefinition,
    ReportSpec,
    SemanticModelSpec,
    SuggestionSpec,
    apply_report_suggestions,
    build_report_definition,
    parse_report,
    suggest_report,
    suggest_report_with_agent,
)
from app.intelligence.audit import (
    audit_report,
    load_brand_theme,
    propose_theme_remediation,
)

from .context import TenantContext
from .errors import UpstreamError, ValidationError

logger = logging.getLogger("fabric_services.report")


@dataclass
class SuggestResult:
    """Outcome of suggesting a report from a model."""

    spec: ReportSpec
    status: str  # "ok" | "deterministic" | "error"
    used_agent: bool = False
    agent_visual_count: int = 0
    error: str | None = None


class ReportService:
    """Manage reports across discovery, suggestion, render, and publish."""

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

    def list_reports(self, workspace_id: str) -> list[FabricItem]:
        try:
            return self._fabric.list_reports(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to list reports: {exc}") from exc

    def import_report(
        self,
        workspace_id: str,
        report_id: str,
        *,
        workspace_name: str = "",
        item_name: str = "",
    ) -> dict[str, Any]:
        """Fetch a report's PBIR definition, persist it, and parse it."""
        try:
            definition = self._fabric.get_report_definition(workspace_id, report_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to fetch report definition: {exc}"
            ) from exc

        ref = ArtifactRef(
            kind="reports",
            workspace_id=workspace_id,
            item_id=report_id,
            workspace_name=workspace_name,
            item_name=item_name,
        )
        self._store.save_definition(
            ref, definition.files, fmt=definition.format, source="imported"
        )
        spec = parse_report(definition.files, name=item_name or None)
        return {"spec": spec, "files": definition.files}

    # -- suggest (agent-first) -------------------------------------------

    def suggest_report(
        self,
        model: SemanticModelSpec,
        *,
        report_name: str | None = None,
        dataset_id: str | None = None,
        dataset_name: str | None = None,
        use_agent: bool = True,
        extra_instructions: str | None = None,
    ) -> SuggestResult:
        """Generate a starter report grounded in ``model``.

        Tries the Foundry design agent first; the agent itself falls back to the
        deterministic baseline, so a valid report is always returned.
        """
        if not model.tables:
            raise ValidationError("The semantic model has no tables to report on.")

        if use_agent:
            outcome = suggest_report_with_agent(
                model,
                report_name=report_name,
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                extra_instructions=extra_instructions,
            )
            if not getattr(outcome, "used_agent", False):
                logger.warning(
                    "AGENTIC FALLBACK: report suggestion agent not used "
                    "(status=%s%s) - served deterministic report for model %r.",
                    getattr(outcome, "status", "deterministic"),
                    f", error={outcome.error}" if getattr(outcome, "error", None) else "",
                    model.name,
                )
            return SuggestResult(
                spec=outcome.report,
                status=outcome.status,
                used_agent=getattr(outcome, "used_agent", False),
                agent_visual_count=getattr(outcome, "agent_visual_count", 0),
                error=getattr(outcome, "error", None),
            )

        logger.info(
            "Agentic layer disabled by caller (use_agent=False) - serving "
            "deterministic report for model %r.",
            model.name,
        )
        spec = suggest_report(
            model,
            report_name=report_name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
        )
        return SuggestResult(spec=spec, status="deterministic")

    # -- render -----------------------------------------------------------

    def build_definition(self, spec: ReportSpec) -> ReportDefinition:
        """Render a report spec to a Fabric PBIR definition."""
        return build_report_definition(spec)

    # -- publish ----------------------------------------------------------

    @staticmethod
    def _ensure_report_complete(spec: ReportSpec) -> None:
        """Refuse to publish a report that has no content to render.

        A report with no pages, or pages that hold no visuals, publishes to
        Fabric as a blank artifact the user cannot use. Catching it here keeps
        Agent Studio from shipping an empty, non-functional report.
        """
        if not spec.pages:
            raise ValidationError(
                "The report has no pages, so it would publish as a blank, "
                "non-functional artifact. Generate at least one page with "
                "visuals before publishing."
            )
        if not any(page.visuals for page in spec.pages):
            raise ValidationError(
                "The report's pages contain no visuals, so it would publish as "
                "a blank, non-functional artifact. Add at least one visual "
                "before publishing."
            )

    def publish_report(
        self,
        workspace_id: str,
        display_name: str,
        spec: ReportSpec,
        *,
        description: str | None = None,
        dataset_id: str | None = None,
        persist: bool = True,
    ) -> CreatedItem:
        """Create the report in Fabric from ``spec`` and optionally persist it.

        The report must bind to a *published* semantic model by id: the Fabric
        REST API only accepts ``byConnection`` dataset references. A spec with no
        ``dataset_id`` renders a relative ``byPath`` reference, which Fabric
        rejects with "Fabric REST API only supports byConnection references".
        Pass ``dataset_id`` (or set it on ``spec``) to bind the report — e.g.
        the id of the semantic model after it has been published to the same
        workspace.
        """
        if dataset_id:
            spec = replace(spec, dataset_id=dataset_id)
        if not spec.dataset_id:
            raise ValidationError(
                "This report is not bound to a published semantic model, so "
                "Fabric would reject it (the REST API only accepts "
                "'byConnection' dataset references, not a relative path). "
                "Publish the semantic model first, then publish the report "
                "with its semantic-model id so it can connect to it."
            )
        self._ensure_report_complete(spec)
        definition = self.build_definition(spec)
        try:
            created = self._fabric.create_report(
                workspace_id,
                display_name,
                definition.definition_payload(),
                description=description,
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to create report: {exc}") from exc

        if persist and created.id:
            ref = ArtifactRef(
                kind="reports",
                workspace_id=workspace_id,
                item_id=created.id,
                item_name=display_name,
            )
            self._store.save_definition(
                ref, definition.files, source="generated"
            )
        return created

    # -- update (write-back) ---------------------------------------------

    def update_report(
        self,
        workspace_id: str,
        report_id: str,
        spec: ReportSpec,
        *,
        item_name: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Replace a published report's definition with ``spec`` (write-back)."""
        self._ensure_report_complete(spec)
        definition = self.build_definition(spec)
        try:
            status = self._fabric.update_report_definition(
                workspace_id, report_id, definition.definition_payload()
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to update report: {exc}") from exc

        if persist:
            ref = ArtifactRef(
                kind="reports",
                workspace_id=workspace_id,
                item_id=report_id,
                item_name=item_name or spec.name,
            )
            self._store.save_definition(
                ref, definition.files, source="exported"
            )
        return {
            "status": status,
            "fileCount": len(definition.files),
        }

    # -- theme remediation -----------------------------------------------

    def generate_theme_remediation(
        self, spec: ReportSpec
    ) -> list[SuggestionSpec]:
        """Generate deterministic theme + visual-title write-back suggestions."""
        return propose_theme_remediation(spec, brand_theme=load_brand_theme())

    def apply_audit_suggestions(
        self,
        spec: ReportSpec,
        suggestions: list[SuggestionSpec],
    ) -> tuple[ReportSpec, list[SuggestionSpec]]:
        """Apply accepted theme / formatting suggestions to ``spec``."""
        return apply_report_suggestions(spec, suggestions)

    def verify_update(
        self,
        workspace_id: str,
        report_id: str,
        *,
        before: ReportSpec | None = None,
    ) -> dict[str, Any]:
        """Re-fetch the published report and produce a verification diff.

        Runs the deterministic report audit on the re-fetched spec and (when
        ``before`` is supplied) on the pre-update spec, returning the delta of
        finding codes closed vs introduced.
        """
        try:
            definition = self._fabric.get_report_definition(
                workspace_id, report_id
            )
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to fetch report for verification: {exc}"
            ) from exc

        after_spec = parse_report(definition.files, name=None)
        after_report = audit_report(after_spec)
        after_codes = {f.code for f in after_report.findings}

        result: dict[str, Any] = {
            "after": after_report.to_dict(),
            "after_finding_count": len(after_codes),
        }
        if before is not None:
            before_report = audit_report(before)
            before_codes = {f.code for f in before_report.findings}
            result["before_finding_count"] = len(before_codes)
            result["closed_codes"] = sorted(before_codes - after_codes)
            result["introduced_codes"] = sorted(after_codes - before_codes)
        return result
