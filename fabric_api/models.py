"""Serialization helpers and Pydantic request/response models.

Domain objects are plain dataclasses; ``to_jsonable`` converts them (and any
nested dataclasses / enums) into JSON-ready structures so responses stay a thin
projection of the domain without hand-maintaining a parallel model for every
field. Request bodies use explicit Pydantic models for validation and OpenAPI.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from pydantic import BaseModel, Field


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums/containers to JSON-able data."""
    # Prefer a domain object's own faithful serializer when it provides one
    # (SemanticModelSpec, ReportSpec, AuditReport, ... all expose to_dict()).
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, type):
        return to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.repr  # skip fields marked repr=False (e.g. raw payloads)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ExtractSchemaRequest(BaseModel):
    server: str = Field(..., description="SQL endpoint server (FQDN).")
    database: str = Field(..., description="Database / lakehouse SQL name.")


class ExportSchemaRequest(BaseModel):
    format: str = Field("markdown", description="markdown | json | sql")
    server: str
    database: str
    endpoint_name: str = Field("", description="Display name for context headers.")


class DesignModelRequest(BaseModel):
    server: str
    database: str
    model_name: str
    storage_mode: str = "import"
    source_kind: str = "sql"
    use_agent: bool = True
    extra_instructions: str | None = None
    selected_tables: list[str] | None = Field(
        None,
        description=(
            "Fully-qualified object names (schema.name) to include in the model. "
            "When omitted, every extracted table/view is used."
        ),
    )
    # Direct Lake / lakehouse binding (mirrors the legacy Streamlit designer).
    lakehouse_id: str | None = None
    lakehouse_name: str | None = None
    onelake_workspace_id: str | None = None
    onelake_tables_path: str | None = None
    default_schema: str | None = None
    direct_lake_mode: str = Field(
        "auto",
        description="auto | onelake | sql — Direct Lake binding flavour.",
    )


class PublishModelRequest(BaseModel):
    workspace_id: str
    display_name: str
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    format: str = "TMDL"
    description: str | None = None


class AuditModelRequest(BaseModel):
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    include_bpa: bool = False
    features: list[str] | None = None


class ValidateModelRequest(BaseModel):
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")


class BuildModelDefinitionRequest(BaseModel):
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    format: str = Field("TMDL", description="TMDL | TMSL")


class SuggestModelRequest(BaseModel):
    server: str = Field(..., description="SQL endpoint server (FQDN).")
    database: str = Field(..., description="Database / lakehouse SQL name.")
    spec: dict = Field(..., description="Current SemanticModelSpec as JSON.")
    selected_tables: list[str] | None = Field(
        None,
        description=(
            "Fully-qualified object names (schema.name) to scope suggestions to. "
            "When omitted, every extracted table/view is considered."
        ),
    )
    use_agent: bool = True
    extra_instructions: str | None = None


class ApplySuggestionsRequest(BaseModel):
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    relationships: list[dict] = Field(
        default_factory=list,
        description="Accepted SuggestedRelationship objects as JSON.",
    )
    measures: list[dict] = Field(
        default_factory=list,
        description="Accepted SuggestedMeasure objects as JSON.",
    )


class SuggestReportRequest(BaseModel):
    model: dict = Field(..., description="SemanticModelSpec as JSON.")
    report_name: str | None = None
    dataset_id: str | None = None
    dataset_name: str | None = None
    use_agent: bool = True
    extra_instructions: str | None = None


class AuditReportRequest(BaseModel):
    spec: dict = Field(..., description="ReportSpec as JSON.")
    use_agent: bool = True


class PublishReportRequest(BaseModel):
    workspace_id: str
    display_name: str
    spec: dict = Field(..., description="ReportSpec as JSON.")
    description: str | None = None
    dataset_id: str | None = Field(
        None,
        description="Semantic-model id to bind the report to (byConnection). "
        "Required when the spec has no dataset_id.",
    )


class UpdateModelRequest(BaseModel):
    """Replace a published semantic model's definition (Fabric updateDefinition)."""

    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    format: str = Field("TMDL", description="TMDL | TMSL.")
    item_name: str = Field("", description="Optional display name for artifact metadata.")
    verify: bool = Field(
        True,
        description=(
            "Re-fetch the model after write-back, re-run the audit suite, and "
            "include a verification diff (codes closed/introduced) in the response."
        ),
    )


class UpdateReportRequest(BaseModel):
    """Replace a published report's definition (Fabric updateDefinition)."""

    spec: dict = Field(..., description="ReportSpec as JSON.")
    item_name: str = Field("", description="Optional display name for artifact metadata.")
    verify: bool = Field(
        True,
        description=(
            "Re-fetch the report after write-back, re-run the audit, and "
            "include a verification diff in the response."
        ),
    )


class ProposeModelSuggestionsRequest(BaseModel):
    """Generate write-back suggestions for a semantic model spec."""

    spec: dict = Field(..., description="SemanticModelSpec as JSON.")


class ProposeReportSuggestionsRequest(BaseModel):
    """Generate write-back suggestions (theme remediation) for a report spec."""

    spec: dict = Field(..., description="ReportSpec as JSON.")


class ApplyAuditSuggestionsRequest(BaseModel):
    """Apply accepted audit suggestions to a spec, returning the updated spec."""

    spec: dict = Field(..., description="SemanticModelSpec or ReportSpec as JSON.")
    suggestions: list[dict] = Field(
        default_factory=list,
        description="Accepted SuggestionSpec objects as JSON (status must be 'accepted').",
    )


class PersistSuggestionsRequest(BaseModel):
    """Persist a suggestion bundle alongside a stored artifact."""

    workspace_id: str
    item_id: str
    workspace_name: str = ""
    item_name: str = ""
    suggestions: list[dict] = Field(
        default_factory=list, description="SuggestionSpec list."
    )


class SuggestionStatusRequest(BaseModel):
    """Patch one suggestion's lifecycle status (accept / skip)."""

    workspace_id: str
    item_id: str
    workspace_name: str = ""
    item_name: str = ""
    suggestion_id: str
    status: str = Field(
        ..., description="proposed | accepted | skipped | applied | failed"
    )


class PublishRemoteAgentRequest(BaseModel):
    description: str | None = Field(
        None, description="Optional description for the hosted Foundry prompt agent."
    )


class GenerateDaxMeasureRequest(BaseModel):
    """Generate a DAX measure for a natural-language intent."""

    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    intent: str = Field(
        ...,
        description="Natural-language description (e.g. 'YoY growth of paid amount').",
    )
    sample_rows: list[dict] = Field(
        default_factory=list,
        description="Optional sample rows (currently used as hints only).",
    )


class OrchestrateRequest(BaseModel):
    """Run the agentic model + report authoring pipeline.

    Provide ``server``/``database``/``model_name`` to run the full grounded
    pipeline; omit them for an advisory plan over ``objective`` only.
    """

    objective: str = Field(
        "", description="Natural-language goal for the run (optional when grounded)."
    )
    server: str | None = Field(None, description="SQL endpoint server (FQDN).")
    database: str | None = Field(None, description="Database / lakehouse SQL name.")
    model_name: str | None = Field(None, description="Name for the designed model.")
    workspace_id: str | None = Field(
        None, description="Target workspace (used only by publish workflows)."
    )
    storage_mode: str = Field("import", description="import | directQuery | directLake.")
    source_kind: str = Field("sql", description="sql | lakehouse.")
    lakehouse_id: str | None = Field(
        None, description="Lakehouse item id (Direct Lake on OneLake binding)."
    )
    lakehouse_name: str | None = Field(
        None, description="Lakehouse display name (Direct Lake source label)."
    )
    onelake_workspace_id: str | None = Field(
        None, description="Workspace id that owns the lakehouse in OneLake."
    )
    onelake_tables_path: str | None = Field(
        None, description="OneLake DFS Tables path for the lakehouse."
    )
    default_schema: str | None = Field(
        None, description="Default schema for a schema-enabled lakehouse."
    )
    direct_lake_mode: str = Field(
        "auto", description="auto | onelake | sql — Direct Lake binding flavour."
    )
    use_agent: bool = Field(True, description="Prefer the Foundry agent path.")
    include_report: bool = Field(True, description="Also design + audit a report.")
    extra_instructions: str | None = Field(
        None, description="Extra requirements applied to every step."
    )


class ModelChatRequest(BaseModel):
    """Run one natural-language editing turn against an existing model.

    The chosen workspace + semantic model identify the connection the Power BI
    Modeling MCP server opens; ``message`` is the user's request and ``history``
    carries the prior turns so the agent has conversational context.
    """

    workspace_name: str = Field(..., description="Fabric workspace display name.")
    model_name: str = Field(..., description="Semantic model display name.")
    message: str = Field(..., description="Natural-language modelling request.")
    workspace_id: str | None = Field(None, description="Fabric workspace id (context).")
    model_id: str | None = Field(None, description="Semantic model item id (context).")
    history: list[dict] = Field(
        default_factory=list,
        description="Prior turns as {role, content} dicts (oldest first).",
    )


class AskModelRequest(BaseModel):
    """Ask a natural-language question against a Fabric semantic model.

    The server translates the question to DAX (grounded in the model's TMDL
    spec) and executes it against the live model via the official Power BI
    Modeling MCP server's ``dax_query_operations`` tool. The response returns
    the generated DAX, the result table, and the full MCP tool transcript so
    the caller can see exactly which MCP tools were invoked.
    """

    workspace_id: str = Field(..., description="Fabric workspace id.")
    workspace_name: str = Field(..., description="Fabric workspace display name.")
    model_id: str = Field(..., description="Semantic model item id.")
    model_name: str = Field(..., description="Semantic model display name.")
    question: str = Field(..., description="Natural-language question.")
    top_n: int = Field(
        50,
        ge=1,
        le=10000,
        description="Default row cap when the question implies a top-N or a browse.",
    )
    auto_import: bool = Field(
        False,
        description=(
            "When true, if the semantic model has not been imported yet, "
            "fetch it from Fabric on the fly. Defaults to false so the API "
            "asks the caller to import explicitly — that way the DAX is "
            "built against a definition the user has reviewed."
        ),
    )
    refresh: bool = Field(
        False,
        description=(
            "When true, re-import the model definition from Fabric even if a "
            "cached copy exists. Use this after the model has changed."
        ),
    )


class PublishModelWorkflowRequest(BaseModel):
    """Create an approval-gated semantic-model publish workflow."""

    workspace_id: str = Field(..., description="Target Fabric workspace id.")
    display_name: str = Field(..., description="Display name for the model item.")
    spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    format: str = Field("TMDL", description="TMDL | TMSL.")
    description: str | None = Field(None, description="Optional item description.")


class PublishReportWorkflowRequest(BaseModel):
    """Create an approval-gated report publish workflow."""

    workspace_id: str = Field(..., description="Target Fabric workspace id.")
    display_name: str = Field(..., description="Display name for the report item.")
    spec: dict = Field(..., description="ReportSpec as JSON.")
    description: str | None = Field(None, description="Optional item description.")
    dataset_id: str | None = Field(None, description="Bound dataset id, if any.")


class PublishModelReportWorkflowRequest(BaseModel):
    """Create a single approval that publishes a model *and* its report.

    One approval publishes the semantic model first, then binds and publishes
    the report to the freshly created model.
    """

    workspace_id: str = Field(..., description="Target Fabric workspace id.")
    model_display_name: str = Field(
        ..., description="Display name for the semantic-model item."
    )
    model_spec: dict = Field(..., description="SemanticModelSpec as JSON.")
    report_display_name: str = Field(
        ..., description="Display name for the report item."
    )
    report_spec: dict = Field(..., description="ReportSpec as JSON.")
    format: str = Field("TMDL", description="Model definition format: TMDL | TMSL.")
    description: str | None = Field(None, description="Optional item description.")


class WorkflowDecisionRequest(BaseModel):
    """Approve or reject a pending publish workflow."""

    approve: bool = Field(..., description="True to publish, False to discard.")


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class ProblemDetail(BaseModel):
    """RFC 7807 problem response."""

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None
