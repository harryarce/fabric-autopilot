"""Platform capabilities exposed as agent-callable tools.

Every tool is a thin, **JSON-in/JSON-out** wrapper over the existing
:mod:`fabric_services` service layer — the single source of truth for business
logic. Because those services are already *agent-first with deterministic
fallback*, these tools inherit that behaviour for free: a tool call works with
or without Foundry, returning the best available result.

Two consumers:

* :class:`ToolKit` — used by the deterministic orchestration pipeline and the
  MCP convenience tools (direct, typed method calls).
* :func:`build_agent_tools` — the same capabilities as bare callables with rich
  docstrings/signatures, which the Microsoft Agent Framework turns into a tool
  schema for a Foundry-backed agent to invoke autonomously.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.intelligence import ReportSpec, SemanticModelSpec, SuggestionSpec
from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from ..models import to_jsonable


class ToolKit:
    """Typed facade over the service layer for orchestration.

    Holds a process-wide :class:`ServiceContainer` and a
    :class:`TenantContext`, resolving per-call services exactly like the REST
    routers and MCP tools do. Methods return plain JSON-able dicts/lists.
    """

    def __init__(self, container: ServiceContainer, tenant: TenantContext) -> None:
        self.container = container
        self.tenant = tenant

    # -- discovery & schema ----------------------------------------------

    def list_workspaces(self) -> list[dict]:
        """List Fabric workspaces visible to the platform identity."""
        svc = self.container.provisioning_service()
        return [to_jsonable(w) for w in svc.list_workspaces()]

    def extract_schema(self, server: str, database: str) -> list[dict]:
        """Extract table/column/key metadata from a Fabric SQL endpoint."""
        svc = self.container.schema_service()
        return [to_jsonable(s) for s in svc.extract_schemas(server, database)]

    # -- semantic model ---------------------------------------------------

    def design_model(
        self,
        *,
        server: str,
        database: str,
        model_name: str,
        storage_mode: str = "import",
        source_kind: str = "sql",
        lakehouse_id: Optional[str] = None,
        lakehouse_name: Optional[str] = None,
        onelake_workspace_id: Optional[str] = None,
        onelake_tables_path: Optional[str] = None,
        default_schema: Optional[str] = None,
        direct_lake_mode: str = "auto",
        use_agent: bool = True,
        extra_instructions: Optional[str] = None,
    ) -> dict:
        """Design a semantic model from a SQL schema (Foundry agent-first)."""
        schemas = self.container.schema_service().extract_schemas(server, database)
        result = self.container.model_service(self.tenant).design_model(
            schemas,
            model_name=model_name,
            source_server=server,
            source_database=database,
            storage_mode=storage_mode,
            source_kind=source_kind,
            lakehouse_id=lakehouse_id,
            lakehouse_name=lakehouse_name,
            onelake_workspace_id=onelake_workspace_id,
            onelake_tables_path=onelake_tables_path,
            default_schema=default_schema,
            direct_lake_mode=direct_lake_mode,
            use_agent=use_agent,
            extra_instructions=extra_instructions,
        )
        return {"spec": to_jsonable(result.spec), "usedAgent": result.used_agent}

    def audit_model(
        self,
        spec: dict,
        *,
        include_bpa: bool = False,
        features: Optional[list[str]] = None,
    ) -> dict:
        """Audit a semantic-model spec; returns per-feature health reports."""
        model_spec = SemanticModelSpec.from_dict(spec)
        reports = self.container.audit_service().audit_semantic_model(
            model_spec, features=features, include_bpa=include_bpa
        )
        return {feature: to_jsonable(report) for feature, report in reports.items()}

    def build_model_definition(self, spec: dict, *, fmt: str = "TMDL") -> dict:
        """Render a semantic-model spec to a TMDL/TMSL item definition."""
        model_spec = SemanticModelSpec.from_dict(spec)
        definition = self.container.model_service(self.tenant).build_definition(
            model_spec, fmt=fmt
        )
        return {"format": definition.format.value, "files": definition.files}

    def generate_dax(
        self, spec: dict, intent: str, sample_rows: Optional[list[dict]] = None
    ) -> dict:
        """Generate a candidate DAX measure for a natural-language intent."""
        model_spec = SemanticModelSpec.from_dict(spec)
        generated = self.container.model_service(self.tenant).generate_dax_measure(
            model_spec, intent, sample_rows=sample_rows or None
        )
        return generated.to_dict()

    def propose_model_fixes(self, spec: dict, kind: str = "usability") -> dict:
        """Propose model write-back fixes (``usability`` | ``copilot``)."""
        model_spec = SemanticModelSpec.from_dict(spec)
        svc = self.container.model_service(self.tenant)
        if kind == "copilot":
            suggestions = svc.propose_copilot_prep(model_spec)
        else:
            suggestions = svc.propose_usability_fixes(model_spec)
        return {"suggestions": [s.to_dict() for s in suggestions]}

    def apply_model_suggestions(self, spec: dict, suggestions: list[dict]) -> dict:
        """Apply accepted audit suggestions to a model spec."""
        model_spec = SemanticModelSpec.from_dict(spec)
        parsed = [SuggestionSpec.from_dict(s) for s in suggestions]
        updated_spec, updated = self.container.model_service(
            self.tenant
        ).apply_audit_suggestions(model_spec, parsed)
        return {
            "spec": to_jsonable(updated_spec),
            "suggestions": [s.to_dict() for s in updated],
        }

    def publish_model(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        fmt: str = "TMDL",
        description: Optional[str] = None,
    ) -> dict:
        """Create a semantic model in Fabric (side-effecting)."""
        model_spec = SemanticModelSpec.from_dict(spec)
        created = self.container.model_service(self.tenant).publish_model(
            workspace_id, display_name, model_spec, fmt=fmt, description=description
        )
        return to_jsonable(created)

    # -- report -----------------------------------------------------------

    def suggest_report(
        self,
        model: dict,
        *,
        report_name: Optional[str] = None,
        dataset_id: Optional[str] = None,
        dataset_name: Optional[str] = None,
        use_agent: bool = True,
        extra_instructions: Optional[str] = None,
    ) -> dict:
        """Generate a starter report grounded in a semantic model (agent-first)."""
        model_spec = SemanticModelSpec.from_dict(model)
        result = self.container.report_service(self.tenant).suggest_report(
            model_spec,
            report_name=report_name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            use_agent=use_agent,
            extra_instructions=extra_instructions,
        )
        return {
            "spec": to_jsonable(result.spec),
            "status": result.status,
            "usedAgent": result.used_agent,
            "agentVisualCount": result.agent_visual_count,
            "error": result.error,
        }

    def audit_report(self, spec: dict, *, use_agent: bool = True) -> dict:
        """Audit a report spec — agent-enriched, deterministic fallback."""
        report_spec = ReportSpec.from_dict(spec)
        result = self.container.audit_service().audit_report(
            report_spec, use_agent=use_agent
        )
        return {
            "report": to_jsonable(result.report),
            "status": result.status,
            "agentFindingCount": result.agent_finding_count,
            "error": result.error,
        }

    def propose_report_theme_fixes(self, spec: dict) -> dict:
        """Propose brand/WCAG theme remediation for a report spec."""
        report_spec = ReportSpec.from_dict(spec)
        suggestions = self.container.report_service(
            self.tenant
        ).generate_theme_remediation(report_spec)
        return {"suggestions": [s.to_dict() for s in suggestions]}

    def apply_report_suggestions(self, spec: dict, suggestions: list[dict]) -> dict:
        """Apply accepted theme/formatting suggestions to a report spec."""
        report_spec = ReportSpec.from_dict(spec)
        parsed = [SuggestionSpec.from_dict(s) for s in suggestions]
        updated_spec, updated = self.container.report_service(
            self.tenant
        ).apply_audit_suggestions(report_spec, parsed)
        return {
            "spec": to_jsonable(updated_spec),
            "suggestions": [s.to_dict() for s in updated],
        }

    def publish_report(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        description: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> dict:
        """Create a report in Fabric (side-effecting)."""
        report_spec = ReportSpec.from_dict(spec)
        created = self.container.report_service(self.tenant).publish_report(
            workspace_id,
            display_name,
            report_spec,
            description=description,
            dataset_id=dataset_id,
        )
        return to_jsonable(created)


def build_agent_tools(toolkit: ToolKit) -> list[Callable[..., Any]]:
    """Return read/analysis capabilities as bare callables for an agent.

    Only **non-side-effecting** tools are exposed to autonomous agents; publish
    / update flow exclusively through the approval-gated workflows. Each callable
    carries a docstring + annotations so the Agent Framework can synthesise a
    tool schema.
    """

    def extract_schema(server: str, database: str) -> list[dict]:
        """Extract table/column/key metadata from a Fabric SQL endpoint."""
        return toolkit.extract_schema(server, database)

    def design_semantic_model(
        server: str,
        database: str,
        model_name: str,
        storage_mode: str = "import",
        extra_instructions: str = "",
    ) -> dict:
        """Design a semantic model from a SQL schema. Returns {spec, usedAgent}."""
        return toolkit.design_model(
            server=server,
            database=database,
            model_name=model_name,
            storage_mode=storage_mode,
            extra_instructions=extra_instructions or None,
        )

    def audit_semantic_model(spec: dict) -> dict:
        """Audit a semantic-model spec; returns per-feature health reports."""
        return toolkit.audit_model(spec)

    def generate_dax_measure(spec: dict, intent: str) -> dict:
        """Generate a candidate DAX measure for a natural-language intent."""
        return toolkit.generate_dax(spec, intent)

    def suggest_report(model: dict, report_name: str = "") -> dict:
        """Generate a starter report grounded in a semantic-model spec."""
        return toolkit.suggest_report(model, report_name=report_name or None)

    def audit_report(spec: dict) -> dict:
        """Audit a report spec for brand/accessibility/quality findings."""
        return toolkit.audit_report(spec)

    return [
        extract_schema,
        design_semantic_model,
        audit_semantic_model,
        generate_dax_measure,
        suggest_report,
        audit_report,
    ]
